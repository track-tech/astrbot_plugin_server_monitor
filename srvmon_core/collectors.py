"""指标采集器：本机（psutil）、远程 SSH（asyncssh + /proc）、远程 HTTP Agent（aiohttp）。

三种采集器统一产出 snapshot 字典：

    {
        "ts": float,                # 采样时间戳
        "online": bool,
        "error": str | None,        # offline 时的人类可读原因
        "hostname": str, "os": str, "arch": str,
        "uptime": int,              # 秒
        "cpu_percent": float, "cpu_cores": int,
        "load": [f, f, f] | None,
        "mem": {"total": int, "used": int, "percent": float,
                "swap_total": int, "swap_used": int, "swap_percent": float},
        "disks": [{"mount": str, "total": int, "used": int, "percent": float}],
        "net": {"rx_rate": float, "tx_rate": float, "rx_total": int, "tx_total": int},
        "temp": float | None,
        "proc": {"cpu": float, "mem": float, "rss": int} | None,  # 仅本机
        "top_procs": [...] | None,  # 仅本机
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
import time
from typing import Any, Optional

logger = logging.getLogger("astrbot")

# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------


class NetRate:
    """根据累计字节数计算速率（首次调用返回 0，之后按时间差计算）。"""

    __slots__ = ("_last",)

    def __init__(self):
        self._last = None

    def rates(self, rx_total: float, tx_total: float) -> tuple:
        now = time.time()
        if self._last is not None:
            t0, r0, s0 = self._last
            dt = now - t0
            if dt > 0:
                self._last = (now, rx_total, tx_total)
                return (
                    max(0.0, (rx_total - r0) / dt),
                    max(0.0, (tx_total - s0) / dt),
                )
        self._last = (now, rx_total, tx_total)
        return 0.0, 0.0


def offline_snap(error: str) -> dict:
    return {
        "ts": time.time(),
        "online": False,
        "error": str(error)[:300],
        "cpu_percent": None,
        "mem": None,
        "disks": [],
        "net": None,
        "load": None,
        "uptime": None,
    }


def _round(v, n=1) -> float:
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 本机采集（psutil）
# ---------------------------------------------------------------------------


class LocalCollector:
    mode = "local"

    def __init__(self):
        import psutil

        self._psutil = psutil
        self._net = NetRate()
        self._diskio = NetRate()
        try:
            self._proc = psutil.Process()
            self._proc.cpu_percent(interval=None)  # 预热，之后才是真实增量
        except Exception:
            self._proc = None
        try:
            self._cpu_count = psutil.cpu_count(logical=True) or 1
        except Exception:
            self._cpu_count = 1

    def _disks(self) -> list:
        disks = []
        try:
            for p in self._psutil.disk_partitions(all=False):
                if not p.fstype or "cdrom" in (p.opts or "").lower():
                    continue
                try:
                    u = self._psutil.disk_usage(p.mountpoint)
                except Exception:  # 分区不可访问 / 个别 psutil 平台异常
                    continue
                disks.append(
                    {
                        "mount": p.mountpoint,
                        "total": u.total,
                        "used": u.used,
                        "percent": _round(u.percent),
                    }
                )
        except Exception as e:
            logger.debug(f"[server_monitor] 枚举磁盘分区失败: {e}")
        return disks

    def _temps(self) -> Optional[float]:
        fn = getattr(self._psutil, "sensors_temperatures", None)
        if not fn:
            return None
        try:
            data = fn() or {}
            vals = [t.current for entries in data.values() for t in entries
                    if getattr(t, "current", None)]
            return _round(max(vals)) if vals else None
        except Exception:
            return None

    def _top_procs(self, n=8) -> list:
        if not self._proc:
            return []
        procs = []
        for p in self._psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = p.info
                procs.append(
                    {
                        "pid": info["pid"],
                        "name": (info["name"] or "?")[:32],
                        "cpu": _round(info["cpu_percent"] or 0.0),
                        "mem": _round(info["memory_percent"] or 0.0),
                    }
                )
            except Exception:
                continue
        procs.sort(key=lambda x: (x["cpu"], x["mem"]), reverse=True)
        return procs[:n]

    def sample(self) -> dict:
        psutil = self._psutil
        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
            swap = {
                "swap_total": sw.total,
                "swap_used": sw.used,
                "swap_percent": _round(sw.percent),
            }
        except Exception:
            swap = {"swap_total": 0, "swap_used": 0, "swap_percent": 0.0}

        disks = self._disks()
        net = psutil.net_io_counters()
        rx_rate, tx_rate = self._net.rates(net.bytes_recv, net.bytes_sent)
        rx2, tx2 = 0.0, 0.0
        try:
            dio = psutil.disk_io_counters()
            if dio:
                rx2, tx2 = self._diskio.rates(dio.read_bytes, dio.write_bytes)
        except Exception:
            pass

        load = None
        try:
            load = [_round(x, 2) for x in psutil.getloadavg()]
        except Exception:
            pass

        proc_info = None
        if self._proc:
            try:
                with self._proc.oneshot():
                    proc_info = {
                        "cpu": _round(self._proc.cpu_percent(interval=None)),
                        "mem": _round(self._proc.memory_percent()),
                        "rss": self._proc.memory_info().rss,
                    }
            except Exception:
                pass

        return {
            "ts": time.time(),
            "online": True,
            "error": None,
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "uptime": int(time.time() - psutil.boot_time()),
            "cpu_percent": _round(psutil.cpu_percent(interval=None)),
            "cpu_cores": self._cpu_count,
            "load": load,
            "mem": {
                "total": vm.total,
                "used": vm.used,
                "percent": _round(vm.percent),
                **swap,
            },
            "disks": disks,
            "net": {
                "rx_rate": _round(rx_rate),
                "tx_rate": _round(tx_rate),
                "rx_total": net.bytes_recv,
                "tx_total": net.bytes_sent,
                "disk_read_rate": _round(rx2),
                "disk_write_rate": _round(tx2),
            },
            "temp": self._temps(),
            "proc": proc_info,
            "top_procs": self._top_procs(),
        }


# ---------------------------------------------------------------------------
# 远程 SSH 采集（asyncssh，远端为 Linux）
# ---------------------------------------------------------------------------

# 只读采集脚本：在远端 shell 中执行，输出一行 JSON。约 1 秒（内部 sleep 1 计算 CPU/网络增量）。
SSH_SCRIPT = r'''
if [ ! -r /proc/stat ]; then
  printf '{"ok":0,"error":"no /proc found, SSH mode supports Linux only"}\n'
  exit 0
fi
hn=$(hostname 2>/dev/null || echo unknown)
os=$(awk -F= '$1=="PRETTY_NAME"{gsub(/"/,"",$2);print $2;exit}' /etc/os-release 2>/dev/null)
[ -n "$os" ] || os=$(uname -sr 2>/dev/null || echo unknown)
arch=$(uname -m 2>/dev/null || echo unknown)
up=$(awk '{printf "%d", $1}' /proc/uptime 2>/dev/null)
cores=$(awk '/^cpu[0-9]/{n++}END{print n+0}' /proc/stat 2>/dev/null)
set -- $(awk '/^cpu /{print $2+$3+$4+$6+$7+$8, $5+$6}' /proc/stat)
b1=$1; i1=$2
rx1=$(sed 's/^ *//' /proc/net/dev | awk -F: 'NR>2 && $1!="lo"{split($2,f," ");rx+=f[1]}END{printf "%d",rx+0}')
tx1=$(sed 's/^ *//' /proc/net/dev | awk -F: 'NR>2 && $1!="lo"{split($2,f," ");tx+=f[9]}END{printf "%d",tx+0}')
sleep 1
set -- $(awk '/^cpu /{print $2+$3+$4+$6+$7+$8, $5+$6}' /proc/stat)
b2=$1; i2=$2
rx2=$(sed 's/^ *//' /proc/net/dev | awk -F: 'NR>2 && $1!="lo"{split($2,f," ");rx+=f[1]}END{printf "%d",rx+0}')
tx2=$(sed 's/^ *//' /proc/net/dev | awk -F: 'NR>2 && $1!="lo"{split($2,f," ");tx+=f[9]}END{printf "%d",tx+0}')
cpu=$(awk -v b1="$b1" -v i1="$i1" -v b2="$b2" -v i2="$i2" 'BEGIN{db=b2-b1;di=i2-i1;t=db+di;v=(t>0)?100*db/t:0;if(v<0)v=0;if(v>100)v=100;printf "%.1f",v}')
rx=$(awk -v a="$rx1" -v b="$rx2" 'BEGIN{printf "%d",b-a}')
tx=$(awk -v a="$tx1" -v b="$tx2" 'BEGIN{printf "%d",b-a}')
mt=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
ma=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
if [ -z "$ma" ]; then
  ma=$(awk '/^(MemFree|Buffers|Cached|SReclaimable):/{s+=$2}END{print s+0}' /proc/meminfo)
fi
st=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)
sf=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
load=$(awk '{printf "%s %s %s", $1, $2, $3}' /proc/loadavg 2>/dev/null)
disks=$(df -kP 2>/dev/null | sed 1d | awk '
$2+0>0 && $1 !~ /^(tmpfs|devtmpfs|udev|overlay|squashfs|none|cgroup|cgroup2|shm|devpts|proc|sysfs|securityfs|debugfs|tracefs|configfs|fusectl|hugetlbfs|pstore|bpf|binfmt_misc|ramfs|nsfs|efivarfs|autofs|mqueue|squashfs)/ {
  m=$6; gsub(/\\040/," ",m); gsub(/"/,"",m);
  printf "%s{\"mount\":\"%s\",\"total\":%d,\"used\":%d,\"percent\":%d}", sep, m, $2*1024, $3*1024, $5+0; sep=","
} END{print ""}')
procs=$(ps aux 2>/dev/null | awk 'NR>1{print $3+0"|"$4+0"|"$2"|"$11}' | sort -t"|" -k1,1rn | head -8 | awk -F"|" '
{gsub(/"/,"",$4); printf "%s{\"pid\":%d,\"name\":\"%.48s\",\"cpu\":%s,\"mem\":%s}", sep, $3, $4, $1, $2; sep=","} END{print ""}')
[ -n "$procs" ] && procs="[$procs]" || procs="[]"
docker="[]"; docker_err=""
if ! command -v docker >/dev/null 2>&1; then
  docker_err="docker CLI not found"
else
  derr=$(timeout 8 docker ps -a --format '{{.Names}}' 2>&1 >/dev/null | tr -d '"' | head -c 120)
  dl=$(timeout 8 docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null | head -24 | awk -F"|" '
  {gsub(/"/,"",$1);gsub(/"/,"",$2);gsub(/"/,"",$3);printf "%s{\"name\":\"%.64s\",\"state\":\"%.16s\",\"status\":\"%.40s\"}", sep, $1, $2, $3; sep=","} END{print ""}')
  if [ -n "$derr" ]; then docker_err="$derr"; fi
  [ -n "$dl" ] && docker="[$dl]"
fi
: ${up:=0}; : ${cores:=0}; : ${cpu:=0}; : ${rx:=0}; : ${tx:=0}
: ${mt:=0}; : ${ma:=0}; : ${st:=0}; : ${sf:=0}
[ -n "$load" ] || load="0 0 0"
printf '{"ok":1,"hostname":"%s","os":"%s","arch":"%s","uptime":%s,"cpu":%s,"cores":%s,"mem_total":%s,"mem_avail":%s,"swap_total":%s,"swap_free":%s,"load":"%s","net_rx":%s,"net_tx":%s,"disks":[%s],"procs":%s,"docker":%s,"docker_err":"%s"}\n' \
"$hn" "$os" "$arch" "$up" "$cpu" "$cores" "$mt" "$ma" "$st" "$sf" "$load" "$rx" "$tx" "$disks" "$procs" "$docker" "$docker_err"
'''


def parse_ssh_output(out: str) -> dict:
    lines = [x for x in (out or "").strip().splitlines() if x.strip()]
    if not lines:
        raise RuntimeError("远端脚本无输出")
    data = json.loads(lines[-1])
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "远端采集失败")

    mt = float(data.get("mem_total") or 0) * 1024
    ma = float(data.get("mem_avail") or 0) * 1024
    mem_total = int(mt)
    mem_used = int(max(0.0, mt - ma))
    mem_pct = _round(100.0 * mem_used / mt) if mt > 0 else 0.0

    st = float(data.get("swap_total") or 0) * 1024
    sf = float(data.get("swap_free") or 0) * 1024
    swap_used = int(max(0.0, st - sf))
    swap_pct = _round(100.0 * swap_used / st) if st > 0 else 0.0

    try:
        load = [_round(x, 2) for x in str(data.get("load", "")).split()]
    except Exception:
        load = []
    load = (load + [0.0, 0.0, 0.0])[:3] if load else None

    disks = []
    for d in data.get("disks") or []:
        try:
            disks.append(
                {
                    "mount": str(d.get("mount", "?")),
                    "total": int(d.get("total", 0)),
                    "used": int(d.get("used", 0)),
                    "percent": _round(d.get("percent", 0)),
                }
            )
        except Exception:
            continue

    def _norm_procs(raw) -> list:
        out = []
        for p in raw or []:
            try:
                out.append(
                    {
                        "pid": int(float(p.get("pid", 0))),
                        "name": str(p.get("name", "?"))[:48],
                        "cpu": _round(p.get("cpu")),
                        "mem": _round(p.get("mem")),
                    }
                )
            except Exception:
                continue
        return out

    docker = []
    for c in data.get("docker") or []:
        try:
            docker.append(
                {
                    "name": str(c.get("name", "?"))[:64],
                    "state": str(c.get("state", "?")),
                    "status": str(c.get("status", ""))[:40],
                }
            )
        except Exception:
            continue

    return {
        "ts": time.time(),
        "online": True,
        "error": None,
        "hostname": str(data.get("hostname", "unknown")),
        "os": str(data.get("os", "Linux")),
        "arch": str(data.get("arch", "")),
        "uptime": int(float(data.get("uptime") or 0)),
        "cpu_percent": _round(data.get("cpu")),
        "cpu_cores": int(float(data.get("cores") or 0)),
        "load": load,
        "mem": {
            "total": mem_total,
            "used": mem_used,
            "percent": mem_pct,
            "swap_total": int(st),
            "swap_used": swap_used,
            "swap_percent": swap_pct,
        },
        "disks": disks,
        "net": {
            "rx_rate": 0.0,
            "tx_rate": 0.0,
            "rx_total": int(float(data.get("net_rx") or 0)),
            "tx_total": int(float(data.get("net_tx") or 0)),
        },
        "temp": None,
        "proc": None,
        "top_procs": _norm_procs(data.get("procs")),
        "docker": docker,
        "docker_error": str(data.get("docker_err"))[:160] if data.get("docker_err") else None,
    }


class SSHCollector:
    mode = "ssh"

    def __init__(self, host: str, port: int = 22, username: str = "root",
                 password: Optional[str] = None, key_path: Optional[str] = None):
        self.host = host
        self.port = int(port or 22)
        self.username = username or "root"
        self.password = password or None
        self.key_path = key_path or None
        self._net = NetRate()

    async def run_command(self, cmd: str, timeout: int = 30) -> dict:
        """在远端执行一条命令（供 WebUI 终端使用），返回 stdout/stderr/退出码。"""
        import asyncssh

        async def _run():
            kw: dict[str, Any] = {
                "port": self.port,
                "username": self.username,
                "known_hosts": None,
                "login_timeout": 12,
            }
            if self.key_path:
                kw["client_keys"] = [self.key_path]
            elif self.password:
                kw["password"] = self.password
            conn = await asyncssh.connect(self.host, **kw)
            try:
                return await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        t0 = time.time()
        try:
            res = await asyncio.wait_for(_run(), timeout=timeout + 6)
        except asyncio.TimeoutError:
            return {
                "ok": True, "code": None, "stdout": "", "truncated": False,
                "stderr": f"命令超时（>{timeout}s），已终止",
                "duration": round(time.time() - t0, 2),
            }
        except Exception as e:
            return {"ok": False, "error": f"SSH 连接失败: {e}"}
        from .terminal import _cap, _dec

        stdout, t1 = _cap(_dec((res.stdout or "").encode("utf-8", "ignore")))
        stderr, t2 = _cap(_dec((res.stderr or "").encode("utf-8", "ignore")))
        return {
            "ok": True, "code": res.exit_status,
            "stdout": stdout, "stderr": stderr,
            "truncated": t1 or t2,
            "duration": round(time.time() - t0, 2),
        }

    async def sample(self) -> dict:
        try:
            import asyncssh
        except ImportError:
            return offline_snap("未安装 asyncssh，请检查插件依赖（requirements.txt）")

        async def _run() -> str:
            kw: dict[str, Any] = {
                "port": self.port,
                "username": self.username,
                "known_hosts": None,  # 跳过 host key 校验，见 README 安全说明
                "login_timeout": 15,
            }
            if self.key_path:
                kw["client_keys"] = [self.key_path]
            elif self.password:
                kw["password"] = self.password
            conn = await asyncssh.connect(self.host, **kw)
            try:
                res = await conn.run(SSH_SCRIPT, check=False)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            out = res.stdout or ""
            if not out.strip():
                status = res.exit_status
                stderr = (res.stderr or "").strip()[:200]
                raise RuntimeError(f"远端脚本无输出(exit={status}) {stderr}".strip())
            return out

        try:
            out = await asyncio.wait_for(_run(), timeout=40)
            snap = parse_ssh_output(out)
        except asyncio.TimeoutError:
            return offline_snap("SSH 采集超时（40s）")
        except Exception as e:
            return offline_snap(f"SSH 采集失败: {e}")

        net = snap.get("net") or {}
        rx_rate, tx_rate = self._net.rates(net.get("rx_total", 0), net.get("tx_total", 0))
        net["rx_rate"] = _round(rx_rate)
        net["tx_rate"] = _round(tx_rate)
        return snap


# ---------------------------------------------------------------------------
# 远程 HTTP Agent 采集（agent/astrbot_srv_agent.py）
# ---------------------------------------------------------------------------


class AgentCollector:
    mode = "agent"

    def __init__(self, url: str, token: str = ""):
        self.url = url
        self.token = token or ""
        self._net = NetRate()

    async def sample(self) -> dict:
        try:
            import aiohttp
        except ImportError:
            return offline_snap("未安装 aiohttp（AstrBot 运行环境异常）")

        headers = {"X-Token": self.token} if self.token else {}
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(self.url, headers=headers) as resp:
                    if resp.status != 200:
                        text = (await resp.text())[:200]
                        return offline_snap(f"Agent 返回 HTTP {resp.status}: {text}")
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return offline_snap("Agent 请求超时（12s）")
        except Exception as e:
            return offline_snap(f"Agent 请求失败: {e}")

        net = data.get("net") or {}
        rx_rate, tx_rate = self._net.rates(net.get("rx_total", 0), net.get("tx_total", 0))
        mem = data.get("mem") or {}
        return {
            "ts": time.time(),
            "online": True,
            "error": None,
            "hostname": str(data.get("hostname", "unknown")),
            "os": str(data.get("os", "unknown")),
            "arch": str(data.get("arch", "")),
            "uptime": int(data.get("uptime") or 0),
            "cpu_percent": _round(data.get("cpu_percent")),
            "cpu_cores": int(data.get("cpu_cores") or 0),
            "load": data.get("load"),
            "mem": {
                "total": int(mem.get("total", 0)),
                "used": int(mem.get("used", 0)),
                "percent": _round(mem.get("percent")),
                "swap_total": int(mem.get("swap_total", 0)),
                "swap_used": int(mem.get("swap_used", 0)),
                "swap_percent": _round(mem.get("swap_percent")),
            },
            "disks": data.get("disks") or [],
            "net": {
                "rx_rate": _round(rx_rate),
                "tx_rate": _round(tx_rate),
                "rx_total": int(net.get("rx_total", 0)),
                "tx_total": int(net.get("tx_total", 0)),
            },
            "temp": data.get("temp"),
            "proc": None,
            "top_procs": data.get("procs") or data.get("top_procs") or None,
            "docker": data.get("docker") or [],
            "docker_error": str(data.get("docker_err"))[:160] if data.get("docker_err") else None,
        }
