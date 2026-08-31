"""运维探活服务：TCP 端口探测与 HTTP 服务健康检查。

结果缓存在内存中，由 MonitorService 汇总进 /overview 响应。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

try:
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


def _parse_probes(raw: Any, kind: str) -> List[dict]:
    out = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        tpl = str(item.get("template") or kind).lower()
        if tpl != kind:
            continue
        name = str(item.get("name") or "").strip()
        if kind == "tcp":
            host = str(item.get("host") or "").strip()
            port = _clamp(item.get("port"), 1, 65535, 0)
            if not host or not port:
                continue
            key = f"{host}:{port}"
            out.append({"key": key, "name": name or key, "host": host, "port": port})
        else:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            method = str(item.get("method") or "GET").upper()
            if method not in ("GET", "HEAD"):
                method = "GET"
            expected = _clamp(item.get("expected_status"), 100, 599, 200)
            key = url
            out.append(
                {"key": key, "name": name or url, "url": url,
                 "method": method, "expected": expected}
            )
    # 去重
    seen, uniq = set(), []
    for it in out:
        if it["key"] in seen:
            continue
        seen.add(it["key"])
        uniq.append(it)
    return uniq


class ProbeService:
    """TCP / HTTP 探活，独立循环，独立刷新节奏。"""

    def __init__(self, config: dict):
        cfg = config or {}
        self.interval = _clamp(cfg.get("probe_interval"), 5, 3600, 30)
        self.tcp_items = _parse_probes(cfg.get("tcp_probes"), "tcp")
        self.http_items = _parse_probes(cfg.get("http_probes"), "http")
        self.tcp_results: Dict[str, dict] = {}
        self.http_results: Dict[str, dict] = {}
        self._stopping = False
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return bool(self.tcp_items or self.http_items)

    def start(self) -> None:
        if self.enabled and not self._task:
            self._task = asyncio.create_task(self._loop(), name="srvmon-probes")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            t0 = time.monotonic()
            try:
                await self.sample_all()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[server_monitor] 探活轮次异常: {e}")
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(2.0, self.interval - elapsed))

    async def sample_all(self) -> None:
        jobs = [self._probe_tcp(it) for it in self.tcp_items]
        jobs += [self._probe_http(it) for it in self.http_items]
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _probe_tcp(self, it: dict) -> None:
        t0 = time.perf_counter()
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(it["host"], it["port"]), timeout=5.0
            )
            self.tcp_results[it["key"]] = {
                "name": it["name"], "host": it["host"], "port": it["port"],
                "ok": True, "error": None,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "ts": time.time(),
            }
        except asyncio.TimeoutError:
            self.tcp_results[it["key"]] = {
                "name": it["name"], "host": it["host"], "port": it["port"],
                "ok": False, "error": "连接超时（5s）", "latency_ms": None, "ts": time.time(),
            }
        except Exception as e:
            self.tcp_results[it["key"]] = {
                "name": it["name"], "host": it["host"], "port": it["port"],
                "ok": False, "error": str(e)[:200], "latency_ms": None, "ts": time.time(),
            }
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _probe_http(self, it: dict) -> None:
        t0 = time.perf_counter()
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.request(it["method"], it["url"], allow_redirects=True) as resp:
                    await resp.content.read(1024)  # 只取首块，够判定即可
                    code = resp.status
            latency = round((time.perf_counter() - t0) * 1000, 1)
            self.http_results[it["key"]] = {
                "name": it["name"], "url": it["url"], "method": it["method"],
                "expected": it["expected"], "code": code,
                "ok": code == it["expected"],
                "error": None if code == it["expected"] else f"状态码 {code} ≠ 期望 {it['expected']}",
                "latency_ms": latency, "ts": time.time(),
            }
        except asyncio.TimeoutError:
            self.http_results[it["key"]] = {
                "name": it["name"], "url": it["url"], "method": it["method"],
                "expected": it["expected"], "code": None, "ok": False,
                "error": "请求超时（10s）", "latency_ms": None, "ts": time.time(),
            }
        except Exception as e:
            self.http_results[it["key"]] = {
                "name": it["name"], "url": it["url"], "method": it["method"],
                "expected": it["expected"], "code": None, "ok": False,
                "error": str(e)[:200], "latency_ms": None, "ts": time.time(),
            }

    def export(self) -> dict:
        return {
            "interval": self.interval,
            "tcp": list(self.tcp_results.values()),
            "http": list(self.http_results.values()),
        }
