"""监控服务：调度采集循环、维护历史与快照、评估告警阈值。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable, Dict, Optional

from .balance import BalanceService
from .collectors import AgentCollector, LocalCollector, SSHCollector, offline_snap
from .history import History
from .llmstats import LLMStats
from .probes import ProbeService
from .terminal import TerminalService, _dec

try:  # AstrBot 环境下使用其 logger；独立测试时退回标准 logging
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("server_monitor")


def _clamp(v, lo, hi, default) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


class MonitorService:
    """采集调度与数据中枢。所有方法仅在事件循环内调用。"""

    def __init__(
        self,
        config: dict,
        alert_sender: Optional[Callable[[str], Awaitable[None]]] = None,
        llm_stats: Optional[LLMStats] = None,
    ):
        self.local_enabled = bool(config.get("local_monitor", True))
        self.local_interval = _clamp(config.get("local_interval"), 2, 3600, 5)
        self.remote_interval = _clamp(config.get("remote_interval"), 10, 86400, 60)
        self.points = _clamp(config.get("history_points"), 30, 10000, 360)
        self.alert_cfg: dict = config.get("alerts") or {}
        self.alert_sender = alert_sender
        self.collect_docker = bool(config.get("collect_docker", True))
        self.collect_procs = bool(config.get("collect_procs", True))
        self.llm_stats = llm_stats

        # name -> {"display_name": str, "mode": str, "host": str}
        self.display: Dict[str, dict] = {}
        self.collectors: Dict[str, Any] = {}
        self.latest: Dict[str, dict] = {}
        self.history: Dict[str, History] = {}
        self._alert_state: Dict[str, dict] = {}
        self.alert_history: deque = deque(maxlen=50)  # 最近告警/恢复事件（最新在末尾）
        self._tasks: list = []
        self._stopping = False
        self._docker_ok = True

        try:
            self.probes = ProbeService(config)
        except Exception as e:
            logger.error(f"[server_monitor] 初始化探活服务失败（已停用探活）: {e}")
            self.probes = ProbeService({})
        try:
            self.balances = BalanceService(config)
        except Exception as e:
            logger.error(f"[server_monitor] 初始化余额服务失败（已停用余额查询）: {e}")
            self.balances = BalanceService({})
        try:
            self.terminal = TerminalService(config, self.collectors, self.display)
        except Exception as e:
            logger.error(f"[server_monitor] 初始化终端服务失败（已停用终端）: {e}")
            self.terminal = TerminalService({"terminal_enabled": False}, self.collectors, self.display)
        self._build_collectors(config)

    # ------------------------------------------------------------------
    # 采集器构建
    # ------------------------------------------------------------------

    def _build_collectors(self, config: dict) -> None:
        if self.local_enabled:
            try:
                self.collectors["local"] = LocalCollector()
                self.display["local"] = {
                    "display_name": "本机",
                    "mode": "local",
                    "host": "AstrBot 所在机器",
                }
            except Exception as e:
                logger.error(f"[server_monitor] 初始化本机采集失败: {e}")

        servers = config.get("remote_servers") or []
        used_names = set(self.collectors.keys())
        for i, item in enumerate(servers):
            if not isinstance(item, dict):
                continue
            tpl = str(item.get("template") or "ssh").lower()
            host = str(item.get("host") or item.get("url") or "").strip()
            name = str(item.get("name") or "").strip() or (host.split("//")[-1] or f"server{i + 1}")
            base = name
            n = 2
            while name in used_names:
                name = f"{base}-{n}"
                n += 1
            used_names.add(name)

            try:
                if tpl == "agent":
                    url = str(item.get("url") or "").strip()
                    if not url:
                        logger.warning(f"[server_monitor] 远程服务器 {name}: Agent 模式缺少 url，已跳过")
                        continue
                    self.collectors[name] = AgentCollector(url, str(item.get("token") or ""))
                    host = url
                else:
                    if not host:
                        logger.warning(f"[server_monitor] 远程服务器 {name}: SSH 模式缺少 host，已跳过")
                        continue
                    self.collectors[name] = SSHCollector(
                        host=host,
                        port=item.get("port") or 22,
                        username=str(item.get("username") or "root"),
                        password=str(item.get("password") or ""),
                        key_path=str(item.get("key_path") or ""),
                    )
                self.display[name] = {
                    "display_name": name,
                    "mode": "agent" if tpl == "agent" else "ssh",
                    "host": host,
                }
            except Exception as e:
                logger.error(f"[server_monitor] 远程服务器 {name} 初始化失败: {e}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._tasks) and not self._stopping

    def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        if "local" in self.collectors:
            self._tasks.append(asyncio.create_task(self._local_loop(), name="srvmon-local"))
        for name in list(self.collectors.keys()):
            if name == "local":
                continue
            self._tasks.append(
                asyncio.create_task(self._remote_loop(name), name=f"srvmon-{name}")
            )
        self.probes.start()
        self.balances.start()
        self.terminal.start()
        if not self._tasks and not self.probes.enabled and not self.balances.enabled:
            logger.warning("[server_monitor] 未启用任何监控目标（本机已禁用且无远程服务器）")

    async def stop(self) -> None:
        self._stopping = True
        await self.probes.stop()
        await self.balances.stop()
        await self.terminal.stop()
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # 采集循环
    # ------------------------------------------------------------------

    async def _local_loop(self) -> None:
        coll = self.collectors["local"]
        try:  # 预热一次，让 cpu_percent / 速率有增量基准
            coll.sample()
        except Exception as e:
            logger.warning(f"[server_monitor] 本机预热采样失败: {e}")
        while not self._stopping:
            try:  # 立即先采一次，页面无需等待一个间隔才有数据
                snap = coll.sample()
                docker, docker_err = await self._local_docker()
                snap["docker"] = docker
                if docker_err:
                    snap["docker_error"] = docker_err
                self.record("local", snap)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[server_monitor] 本机采样失败: {e}")
            await asyncio.sleep(self.local_interval)

    async def _local_docker(self):
        """本机 Docker 容器列表。返回 (items, error)；error 非空时面板显示具体原因。"""
        if not self.collect_docker:
            return [], None
        if not self._docker_ok:
            return [], self._docker_err
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError):
            self._docker_ok = False  # 本机无 docker，不再重复尝试
            self._docker_err = "未找到 docker 命令（未安装 Docker CLI，或 AstrBot 运行在容器内且未挂载 docker）"
            return [], self._docker_err
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=8)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            self._docker_ok = False
            self._docker_err = "docker ps 超时（8s），Docker 守护进程可能无响应"
            return [], self._docker_err
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._docker_ok = False
            self._docker_err = f"docker ps 执行失败: {e}"
            return [], self._docker_err
        if proc.returncode != 0:
            err = _dec(err_b).strip() or f"exit {proc.returncode}"
            self._docker_ok = False  # 权限/守护进程问题每轮相同，避免重复执行
            self._docker_err = f"docker ps 失败: {err[:150]}"
            if "permission" in err.lower() or "denied" in err.lower():
                self._docker_err += (
                    " | 修复: sudo usermod -aG docker <运行AstrBot的用户> 后重新登录或重启 AstrBot;"
                    " 若 AstrBot 跑在容器内, 需挂载 /var/run/docker.sock 并安装 docker CLI"
                )
            return [], self._docker_err
        items = []
        for line in out_b.decode("utf-8", "ignore").strip().splitlines()[:24]:
            parts = (line.split("|") + ["", "", ""])[:3]
            if parts[0]:
                items.append(
                    {"name": parts[0][:64], "state": parts[1][:16], "status": parts[2][:40]}
                )
        return items, None

    async def _remote_loop(self, name: str) -> None:
        coll = self.collectors[name]
        while not self._stopping:
            t0 = time.monotonic()
            try:
                self.record(name, await coll.sample())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[server_monitor] {name} 采样异常: {e}")
                self.record(name, offline_snap(str(e)))
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(2.0, self.remote_interval - elapsed))

    # ------------------------------------------------------------------
    # 数据记录与告警
    # ------------------------------------------------------------------

    def record(self, name: str, snap: dict) -> None:
        if not snap:
            return
        if not self.collect_docker:
            snap.pop("docker", None)
        if not self.collect_procs:
            snap.pop("top_procs", None)
        self.latest[name] = snap
        if snap.get("online"):
            hist = self.history.get(name)
            if hist is None:
                hist = self.history[name] = History(self.points)
            hist.append(snap)
        self._check_alerts(name, snap)

    def _display_name(self, name: str) -> str:
        meta = self.display.get(name)
        return meta["display_name"] if meta else name

    def _send_alert(self, text: str) -> None:
        self.alert_history.append(
            {
                "ts": time.time(),
                "level": "recovery" if text.startswith("🟢") else "alert",
                "text": text,
            }
        )
        if not self.alert_sender:
            return

        async def _do():
            try:
                await self.alert_sender(text)
            except Exception as e:
                logger.error(f"[server_monitor] 告警消息发送失败: {e}")

        asyncio.create_task(_do())

    _RECOVERY_TEXT = {
        "cpu": "CPU 使用率已恢复正常",
        "mem": "内存使用率已恢复正常",
        "disk": "磁盘使用率已恢复正常",
        "offline": "服务器已恢复在线",
    }

    def _check_alerts(self, name: str, snap: dict) -> None:
        cfg = self.alert_cfg or {}
        if not cfg.get("enabled") or not self.alert_sender:
            return

        display = self._display_name(name)
        cooldown = _clamp(cfg.get("cooldown"), 0, 86400 * 7, 1800)
        sustain = _clamp(cfg.get("sustain_times"), 1, 1000, 3)
        now = time.time()
        breaches = []

        if snap.get("online"):
            cpu = snap.get("cpu_percent") or 0.0
            mem = snap.get("mem") or {}
            disks = snap.get("disks") or []
            worst_disk = max(disks, key=lambda d: d.get("percent", 0) or 0) if disks else None

            if cpu >= float(cfg.get("cpu_threshold", 85) or 0):
                breaches.append(("cpu", f"CPU 使用率 {cpu:.1f}%"))
            if (mem.get("percent") or 0) >= float(cfg.get("mem_threshold", 90) or 0):
                breaches.append(("mem", f"内存使用率 {mem.get('percent', 0):.1f}%"))
            if worst_disk and (worst_disk.get("percent") or 0) >= float(
                cfg.get("disk_threshold", 90) or 0
            ):
                breaches.append(
                    ("disk", f"磁盘 {worst_disk.get('mount', '?')} 使用率 {worst_disk.get('percent', 0):.1f}%")
                )
        elif cfg.get("notify_offline", True):
            breaches.append(("offline", f"服务器离线（{snap.get('error') or '原因未知'}）"))

        for key, desc in breaches:
            st = self._alert_state.setdefault(
                f"{name}:{key}", {"count": 0, "fired": False, "last": 0.0}
            )
            st["count"] += 1
            if st["count"] >= sustain and not st["fired"] and now - st["last"] >= cooldown:
                st["fired"] = True
                st["last"] = now
                self._send_alert(f"🔴 [服务器监控] {display} {desc}")

        # 未超标的指标：重置计数并（可选）发送恢复通知
        breach_keys = {k for k, _ in breaches}
        for full_key in list(self._alert_state.keys()):
            if not full_key.startswith(f"{name}:"):
                continue
            key = full_key.split(":", 1)[1]
            if key in breach_keys:
                continue
            st = self._alert_state[full_key]
            if st["fired"]:
                st["fired"] = False
                if cfg.get("notify_recovery", True):
                    self._send_alert(
                        f"🟢 [服务器监控] {display} {self._RECOVERY_TEXT.get(key, key + ' 已恢复')}"
                    )
            st["count"] = 0

    # ------------------------------------------------------------------
    # 对外数据
    # ------------------------------------------------------------------

    def maybe_refresh_local(self) -> None:
        """页面拉取数据时，若本机快照明显过期则立即补采一次（自愈，防采集循环意外停止）。"""
        coll = self.collectors.get("local")
        if coll is None:
            return
        snap = self.latest.get("local")
        if snap is None or time.time() - (snap.get("ts") or 0) > self.local_interval * 2:
            try:
                self.record("local", coll.sample())
            except Exception as e:
                logger.debug(f"[server_monitor] 按需补采本机失败: {e}")

    def build_overview(self, points: Optional[int] = None) -> dict:
        self.maybe_refresh_local()
        servers = {}
        for name, meta in self.display.items():
            hist = self.history.get(name)
            snap = self.latest.get(name)
            if snap is None:
                snap = offline_snap("尚未完成首次采集")
            servers[name] = {
                "name": name,
                "display_name": meta["display_name"],
                "mode": meta["mode"],
                "host": meta["host"],
                "snapshot": snap,
                "history": hist.export(points) if hist else None,
            }
        return {
            "ok": True,
            "now": time.time(),
            "local_interval": self.local_interval,
            "remote_interval": self.remote_interval,
            "servers": servers,
            "probes": self.probes.export(),
            "balances": self.balances.export(),
            "llm": self.llm_stats.export() if self.llm_stats else None,
            "alerts": list(reversed(self.alert_history)) if self.alert_history else [],
        }
