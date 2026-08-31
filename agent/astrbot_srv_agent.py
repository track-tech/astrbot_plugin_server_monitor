#!/usr/bin/env python3
"""AstrBot 服务器监控 Agent（部署在被监控的云服务器上）
=====================================================

一个零配置的单文件 HTTP 指标端点：AstrBot 插件 astrbot_plugin_server_monitor
通过 GET /metrics 拉取本机状态（CPU / 内存 / Swap / 磁盘 / 网络 / 负载 / 运行时长）。

依赖：Python 3.7+ 与 psutil（pip3 install psutil），支持 Linux / Windows / macOS。

用法：
    python3 astrbot_srv_agent.py --port 9122 --token my-secret
    # 建议配合 systemd 常驻，见插件 README.md

之后在 AstrBot 插件配置「远程服务器列表」里新增一个「HTTP Agent 服务器」模板，
url 填 http://<服务器IP>:9122/metrics，token 与 --token 一致即可。
"""

import argparse
import json
import platform
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    raise SystemExit("缺少 psutil：请先执行  pip3 install psutil")

NET = {"last": None}


def _round(v, n=1):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return 0.0


def net_rates():
    now = time.time()
    c = psutil.net_io_counters()
    if NET["last"] is None:
        NET["last"] = (now, c.bytes_recv, c.bytes_sent)
        return 0.0, 0.0
    t0, r0, s0 = NET["last"]
    dt = now - t0
    if dt <= 0:
        return 0.0, 0.0
    NET["last"] = (now, c.bytes_recv, c.bytes_sent)
    return (
        _round(max(0.0, (c.bytes_recv - r0) / dt)),
        _round(max(0.0, (c.bytes_sent - s0) / dt)),
    )


def docker_list() -> list:
    """枚举 Docker 容器（可选，未安装 docker 时返回空列表）。"""
    import shutil
    import subprocess

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return [], "docker CLI not found"
    try:
        r = subprocess.run(
            [docker_bin, "ps", "-a",
             "--format", "{{.Names}}|{{.State}}|{{.Status}}"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0:
            return [], "docker ps failed: " + (r.stderr or "").strip()[:120]
        items = []
        for line in r.stdout.strip().splitlines()[:24]:
            parts = (line.split("|") + ["", "", ""])[:3]
            if parts[0]:
                items.append({"name": parts[0][:64], "state": parts[1][:16], "status": parts[2][:40]})
        return items, None
    except Exception as e:
        return [], "docker ps failed: %s" % e


def top_procs(n: int = 8) -> list:
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            procs.append({
                "pid": p.info["pid"],
                "name": (p.info["name"] or "?")[:48],
                "cpu": _round(p.info["cpu_percent"] or 0.0),
                "mem": _round(p.info["memory_percent"] or 0.0),
            })
        except Exception:
            continue
    procs.sort(key=lambda x: (x["cpu"], x["mem"]), reverse=True)
    return procs[:n]


def snapshot() -> dict:
    docker_items, docker_err = docker_list()
    vm = psutil.virtual_memory()
    try:
        sw = psutil.swap_memory()
        swap = {"swap_total": sw.total, "swap_used": sw.used, "swap_percent": _round(sw.percent)}
    except Exception:
        swap = {"swap_total": 0, "swap_used": 0, "swap_percent": 0.0}

    disks = []
    for p in psutil.disk_partitions(all=False):
        if not p.fstype or "cdrom" in (p.opts or "").lower():
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
            disks.append({
                "mount": p.mountpoint,
                "total": u.total,
                "used": u.used,
                "percent": _round(u.percent),
            })
        except Exception:  # 分区不可访问 / 个别 psutil 平台异常
            continue

    rx_rate, tx_rate = net_rates()
    load = None
    try:
        load = [_round(x, 2) for x in psutil.getloadavg()]
    except Exception:
        pass

    temp = None
    try:
        data = psutil.sensors_temperatures() or {}
        vals = [t.current for e in data.values() for t in e if getattr(t, "current", None)]
        temp = _round(max(vals)) if vals else None
    except Exception:
        pass

    return {
        "ts": time.time(),
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "uptime": int(time.time() - psutil.boot_time()),
        "cpu_percent": _round(psutil.cpu_percent(interval=None)),
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "load": load,
        "mem": {
            "total": vm.total,
            "used": vm.used,
            "percent": _round(vm.percent),
            **swap,
        },
        "disks": disks,
        "net": {
            "rx_rate": rx_rate,
            "tx_rate": tx_rate,
            "rx_total": psutil.net_io_counters().bytes_recv,
            "tx_total": psutil.net_io_counters().bytes_sent,
        },
        "temp": temp,
        "procs": top_procs(),
        "docker": docker_items,
        "docker_err": docker_err,
    }


class Handler(BaseHTTPRequestHandler):
    agent_token = ""
    server_version = "SrvMonAgent/1.0"

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path not in ("/", "/metrics"):
            self._reply(404, {"error": "not found, use GET /metrics"})
            return
        if self.agent_token:
            token = self.headers.get("X-Token", "")
            for kv in query.split("&"):
                if kv.startswith("token="):
                    token = kv[6:]
                    break
            if token != self.agent_token:
                self._reply(403, {"error": "invalid token"})
                return
        self._reply(200, snapshot())

    def _reply(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 安静模式
        pass


def main():
    ap = argparse.ArgumentParser(description="AstrBot 服务器监控 Agent")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    ap.add_argument("--port", type=int, default=9122, help="监听端口（默认 9122）")
    ap.add_argument("--token", default="", help="访问令牌；设置后请求需携带 X-Token 头或 ?token=")
    args = ap.parse_args()

    Handler.agent_token = args.token
    psutil.cpu_percent(interval=None)  # 预热
    print(f"[SrvMonAgent] listening on http://{args.host}:{args.port}/metrics"
          f"  (token: {'on' if args.token else 'off'})")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
