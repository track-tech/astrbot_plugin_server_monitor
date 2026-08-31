"""聊天端文本格式化：/server 指令输出的状态文本。"""

from __future__ import annotations

import time
from typing import Optional

from .collectors import offline_snap


def fmt_bytes(n: Optional[float]) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_rate(n: Optional[float]) -> str:
    return f"{fmt_bytes(n)}/s"


def fmt_uptime(seconds: Optional[int]) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "未知"
    if seconds <= 0:
        return "未知"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}天{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟"


def bar(pct: Optional[float], width: int = 10) -> str:
    try:
        pct = max(0.0, min(100.0, float(pct or 0)))
    except (TypeError, ValueError):
        pct = 0.0
    filled = int(round(pct / 100 * width))
    return "▇" * filled + "░" * (width - filled)


def _load_str(load) -> str:
    if not load:
        return "未知"
    return " / ".join(f"{x:.2f}" for x in load[:3])


def _server_block(meta: dict, snap: dict, detail: bool) -> str:
    icon = {"local": "💻", "ssh": "☁️", "agent": "🛰️"}.get(meta.get("mode", ""), "🖥️")
    if not snap.get("online"):
        return (
            f"{icon} {meta.get('display_name', '?')} ❌ 离线\n"
            f"   原因: {snap.get('error') or '未知'}"
        )

    mem = snap.get("mem") or {}
    net = snap.get("net") or {}
    disks = snap.get("disks") or []
    cpu = snap.get("cpu_percent") or 0.0
    mem_pct = mem.get("percent") or 0.0

    lines = [
        f"{icon} {meta.get('display_name', '?')} · {snap.get('os') or '未知系统'}"
        + (f" ({snap.get('cpu_cores')}核)" if snap.get("cpu_cores") else ""),
        f"   CPU {bar(cpu)} {cpu:.1f}%",
        f"   内存 {bar(mem_pct)} {mem_pct:.1f}%"
        f"（{fmt_bytes(mem.get('used'))} / {fmt_bytes(mem.get('total'))}）",
    ]
    if mem.get("swap_total"):
        swap_pct = mem.get("swap_percent") or 0.0
        lines.append(f"   Swap {bar(swap_pct)} {swap_pct:.1f}%")

    if disks:
        if detail or len(disks) <= 3:
            for d in disks[:8]:
                dp = d.get("percent") or 0.0
                lines.append(
                    f"   磁盘 {d.get('mount', '?')} {bar(dp)} {dp:.1f}%"
                    f"（{fmt_bytes(d.get('used'))} / {fmt_bytes(d.get('total'))}）"
                )
        else:
            worst = max(disks, key=lambda d: d.get("percent", 0) or 0)
            total_pct = sum(d.get("percent", 0) or 0 for d in disks) / len(disks)
            lines.append(
                f"   磁盘 {len(disks)} 个分区，最大 {worst.get('mount', '?')} "
                f"{worst.get('percent', 0):.1f}%，平均 {total_pct:.1f}%（详情: /server detail）"
            )

    lines.append(
        f"   网络 ↓ {fmt_rate(net.get('rx_rate'))}  ↑ {fmt_rate(net.get('tx_rate'))}"
    )
    if snap.get("load"):
        lines.append(f"   负载 {_load_str(snap.get('load'))}")
    lines.append(f"   ⏱️ 已运行 {fmt_uptime(snap.get('uptime'))}")

    if detail:
        if snap.get("temp") is not None:
            lines.append(f"   🌡️ 温度 {snap.get('temp')}°C")
        proc = snap.get("proc")
        if proc:
            lines.append(
                f"   AstrBot 进程: CPU {proc.get('cpu', 0):.1f}% · "
                f"内存 {fmt_bytes(proc.get('rss'))}（{proc.get('mem', 0):.1f}%）"
            )
        tops = snap.get("top_procs")
        if tops:
            lines.append("   CPU 占用 Top:")
            for p in tops[:5]:
                lines.append(
                    f"     {p.get('pid', '?')} {p.get('name', '?')} "
                    f"CPU {p.get('cpu', 0):.1f}% · MEM {p.get('mem', 0):.1f}%"
                )
    return "\n".join(lines)


def text_status(svc, detail: bool = False) -> str:
    """生成 /server 指令回复文本。svc: MonitorService"""
    header = f"📊 服务器状态概览  ⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
    blocks = []
    for name, meta in svc.display.items():
        snap = svc.latest.get(name)
        if snap is None:
            snap = offline_snap("尚未完成首次采集")
        blocks.append(_server_block(meta, snap, detail))
    if not blocks:
        return "⚠️ 未启用任何监控目标，请在插件配置中开启本机监控或添加远程服务器。"
    body = "\n\n".join(blocks)
    tip = "" if detail else "\n\n💡 详情: /server detail | 绑定告警: /server bind"
    return f"{header}\n\n{body}{tip}"
