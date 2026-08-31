"""滚动历史序列，用于 WebUI 历史曲线与聊天端趋势展示。"""

from __future__ import annotations

import time
from collections import deque
from typing import Optional

SERIES = ("ts", "cpu", "mem", "swap", "net_rx", "net_tx", "disk")


class History:
    """按服务器保存的定点数历史序列，仅事件循环内访问（非线程安全）。"""

    __slots__ = ("maxlen", "_d")

    def __init__(self, maxlen: int = 360):
        self.maxlen = max(10, int(maxlen))
        self._d = {k: deque(maxlen=self.maxlen) for k in SERIES}

    def append(self, snap: dict) -> None:
        mem = snap.get("mem") or {}
        net = snap.get("net") or {}
        disks = snap.get("disks") or []
        vals = {
            "ts": snap.get("ts") or time.time(),
            "cpu": _f(snap.get("cpu_percent")),
            "mem": _f(mem.get("percent")),
            "swap": _f(mem.get("swap_percent")),
            "net_rx": _f(net.get("rx_rate")),
            "net_tx": _f(net.get("tx_rate")),
            "disk": max((_f(x.get("percent")) for x in disks), default=0.0),
        }
        for k, v in vals.items():
            self._d[k].append(v)

    def export(self, points: Optional[int] = None) -> dict:
        out = {}
        for k in SERIES:
            dq = self._d[k]
            if points is not None and points > 0 and points < len(dq):
                out[k] = list(dq)[len(dq) - points:]
            else:
                out[k] = list(dq)
        return out


def _f(v, default=0.0) -> float:
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return default
