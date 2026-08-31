"""WebUI 网页 SSH：持久终端会话（本机持久 shell / SSH 远端 PTY）+ SSE 实时输出流。

安全说明：
- 仅 AstrBot 管理面板（JWT 鉴权）可建立会话；
- 可在插件配置中整体停用（terminal_enabled）；
- 会话数量上限（8 个）与闲置自动回收（10 分钟）；
- 会话建立/关闭写入审计日志（执行者、目标）。
"""

from __future__ import annotations

import asyncio
import os
import json
import time
import uuid
from typing import Any, Dict, Optional

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("server_monitor")

MAX_SESSIONS = 8
IDLE_TTL = 600          # 会话闲置回收（秒）
READ_CHUNK = 4096
HEARTBEAT = 15          # SSE 心跳间隔（秒）


def _clamp(v, lo, hi, default) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _dec(b: bytes) -> str:
    """终端输出解码：UTF-8 优先，GBK 兜底（中文 Windows 控制台）。"""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return b.decode("gbk", "replace")
        except Exception:
            return b.decode("utf-8", "replace")


class TerminalSession:
    __slots__ = ("sid", "target", "target_name", "kind", "conn", "proc",
                 "queue", "dec", "last_active", "cols", "rows", "closed", "_lbuf",
                 "pid", "master", "pty")

    def __init__(self, sid: str, target: str, target_name: str, kind: str,
                 cols: int, rows: int):
        self.sid = sid
        self.target = target
        self.target_name = target_name
        self.kind = kind                    # "ssh" | "local"
        self.conn = None                    # asyncssh SSHClientConnection
        self.proc: Any = None               # SSHClientProcess / asyncio subprocess
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self.dec = codecs_incremental()
        self.last_active = time.time()
        self.cols = cols
        self.rows = rows
        self.closed = False
        self.pid = None
        self.master = None
        self.pty = False
        self._lbuf = ""

    async def emit(self, text: str) -> None:
        if not text or self.closed:
            return
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:  # 丢弃最旧的，保留最新输出
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(text)
            except Exception:
                pass


def codecs_incremental():
    import codecs

    return codecs.getincrementaldecoder("utf-8")()


class TerminalService:
    """持有 collectors/display 的引用（由 MonitorService 构建后传入）。"""

    def __init__(self, config: dict, collectors: Dict[str, Any], display: Dict[str, dict]):
        self.enabled = bool(config.get("terminal_enabled", True))
        self.timeout = _clamp(config.get("terminal_timeout"), 3, 300, 30)
        self.collectors = collectors  # 引用，随 MonitorService 更新
        self.display = display
        self.sessions: Dict[str, TerminalSession] = {}
        self._reaper: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="srvmon-term-reaper")

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper = None
        for sid in list(self.sessions.keys()):
            try:
                await self.close(sid)
            except Exception:
                pass

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for sid, sess in list(self.sessions.items()):
                if now - sess.last_active > IDLE_TTL:
                    logger.info(f"[server_monitor][终端] 回收闲置会话 {sid[:8]} @ {sess.target_name}")
                    try:
                        await self.close(sid)
                    except Exception:
                        pass

    # ------------------------------------------------------------------

    def targets(self) -> dict:
        out: list = []
        if "local" in self.collectors:
            meta = self.display.get("local") or {}
            out.append({"name": "local", "display_name": meta.get("display_name", "本机"),
                        "mode": "local", "supported": True})
        for name, meta in self.display.items():
            if name == "local":
                continue
            mode = meta.get("mode", "")
            out.append({"name": name, "display_name": meta.get("display_name", name),
                        "mode": mode, "supported": mode == "ssh"})
        return {"enabled": self.enabled, "timeout": self.timeout,
                "sessions": len(self.sessions), "targets": out}

    async def open(self, target: str, cols: int, rows: int, username: str = "") -> dict:
        if not self.enabled:
            return {"ok": False, "error": "终端功能已在插件配置中停用"}
        if target != "local" and target not in self.display:
            return {"ok": False, "error": f"目标不存在: {target}"}
        meta = self.display.get(target) or {}
        mode = meta.get("mode", "")
        if target != "local" and mode != "ssh":
            return {"ok": False, "error": "该目标不支持远程终端（仅本机与 SSH 模式；Agent 模式请登录目标机操作）"}

        # 会话数上限：关最旧
        while len(self.sessions) >= MAX_SESSIONS:
            oldest = min(self.sessions.values(), key=lambda x: x.last_active)
            await self.close(oldest.sid)

        cols = _clamp(cols, 40, 500, 100)
        rows = _clamp(rows, 12, 200, 28)
        sid = uuid.uuid4().hex[:12]
        sess = TerminalSession(sid, target, meta.get("display_name", target),
                               "ssh" if target != "local" else "local", cols, rows)

        if target == "local":
            import platform as _pf

            if _pf.system() == "Windows":
                # Windows 无 stdlib PTY：持久 PowerShell 管道（前端做 EOL 转换）
                sess.proc = await asyncio.create_subprocess_exec(
                    "powershell", "-NoLogo", "-NoProfile", "-Command", "-",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                sess.kind = "local"
                sess.pty = False
            else:
                # Linux/macOS：真 PTY（bash 颜色/任务控制/ioctl 完整可用）
                import pty as _pty

                pid, master = _pty.fork()
                if pid == 0:  # 子进程：成为会话首进程并 exec shell
                    try:
                        os.environ["TERM"] = "xterm-256color"
                        os.environ["COLORTERM"] = "truecolor"
                        os.execvp("bash", ["bash", "--login", "-i"])
                    except Exception:
                        try:
                            os.execvp("sh", ["sh", "-i"])
                        except Exception:
                            os._exit(127)
                sess.pid = pid
                sess.master = master
                try:  # 立即设置窗口尺寸，否则 btop/htop 等拿不到行列数
                    import fcntl
                    import struct
                    import termios

                    fcntl.ioctl(master, termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0))
                except Exception:
                    pass
                os.set_blocking(master, False)
                sess.kind = "local"
                sess.pty = True
                asyncio.get_running_loop().add_reader(
                    master, self._pty_readable, sess
                )
        else:
            import asyncssh

            coll = self.collectors.get(target)
            if coll is None:
                return {"ok": False, "error": f"目标不存在: {target}"}
            kw: dict[str, Any] = {
                "port": coll.port, "username": coll.username,
                "known_hosts": None, "login_timeout": 12,
            }
            if coll.key_path:
                kw["client_keys"] = [coll.key_path]
            elif coll.password:
                kw["password"] = coll.password
            sess.conn = await asyncssh.connect(coll.host, **kw)
            sess.proc = await sess.conn.create_process(
                term_type="xterm-256color", width=cols, height=rows
            )
            sess.kind = "ssh"

        self.sessions[sid] = sess
        sess.last_active = time.time()
        asyncio.create_task(self._pump(sess))
        logger.info(
            f"[server_monitor][终端] 会话建立 {sid[:8]} @ {sess.target_name} "
            f"({sess.kind}, {cols}x{rows}) by {username or '未知用户'}"
        )
        await sess.emit(f"\x1b[36m-- 已连接 {sess.target_name} ({sess.kind}) --\x1b[0m\r\n")
        return {"ok": True, "sid": sid, "kind": sess.kind}

    _NOISE = ("无法设定终端进程组", "对设备不适当的 ioctl",
            "此 shell 中无任务控制", "cannot set terminal process group",
            "inappropriate ioctl for device", "no job control",
            "there is no job control")

    @staticmethod
    def _is_noise(line: str) -> bool:
        low = line.lower()
        return any(p in low for p in TerminalService._NOISE)

    @staticmethod
    def _filter_noise(sess: "TerminalSession", text: str) -> str:
        """行级过滤本机 shell 的无 PTY 启动噪音（跨块行缓冲）。"""
        buf = sess._lbuf + text
        parts = buf.split("\n")
        tail = parts.pop()  # 不完整行残段（含结尾换行时为空）
        kept = ""
        for line in parts:
            if not TerminalService._is_noise(line):
                kept += line + "\n"
        sess._lbuf = ""  # 残行立即显示后即消费，避免重复
        return kept + tail

    def _pty_readable(self, sess: TerminalSession) -> None:
        """PTY 主端可读回调（add_reader）。"""
        if sess.closed:
            return
        try:
            data = os.read(sess.master, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            loop = asyncio.get_running_loop()
            try:
                loop.remove_reader(sess.master)
            except Exception:
                pass
            asyncio.create_task(self._on_pty_exit(sess))
            return
        text = sess.dec.decode(data)
        asyncio.create_task(sess.emit(text))

    async def _on_pty_exit(self, sess: TerminalSession) -> None:
        """监视本机 PTY shell 进程退出。"""
        try:
            while not sess.closed:
                pid, _ = os.waitpid(sess.pid, os.WNOHANG)
                if pid != 0:
                    await sess.emit("\x1b[33m\r\n-- shell 已退出 --\x1b[0m\r\n")
                    return
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except ChildProcessError:
            pass
        except Exception:
            pass

    async def _pump(self, sess: TerminalSession) -> None:
        """读取进程输出 → 会话队列（SSE 消费）。本机会话过滤无 PTY 的启动噪音。"""
        if sess.pty:
            return  # PTY 会话走 add_reader 回调，不经 _pump
        try:
            reader = sess.proc.stdout
            while not sess.closed:
                data = await reader.read(READ_CHUNK)
                if not data:
                    break
                text = sess.dec.decode(data)
                if sess.kind == "local":
                    text = TerminalService._filter_noise(sess, text)
                    if not text:
                        continue
                await sess.emit(text)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        finally:
            if not sess.closed:
                await sess.emit("\x1b[33m\r\n-- 会话已结束 --\x1b[0m\r\n")

    def _get(self, sid: str) -> Optional[TerminalSession]:
        sess = self.sessions.get(sid or "")
        if sess is None or sess.closed:
            return None
        sess.last_active = time.time()
        return sess

    async def write(self, sid: str, data: str) -> dict:
        sess = self._get(sid)
        if sess is None:
            return {"ok": False, "error": "会话不存在或已关闭，请重新打开终端"}
        if sess.kind == "local" and sess.pty:
            # 本机 PTY：直接写主端（UTF-8，现代 Linux 终端默认）
            try:
                os.write(sess.master, data.encode("utf-8"))
            except OSError:
                return {"ok": False, "error": "会话已断开，请重新打开终端"}
            return {"ok": True}
        if sess.kind == "local":
            # Windows 管道模式：按系统 locale 编码以兼容中文控制台（PowerShell）
            import locale

            try:
                data = data.encode(locale.getpreferredencoding(False) or "utf-8", "replace")
            except Exception:
                data = data.encode("utf-8", "replace")
        try:
            sess.proc.stdin.write(data)
            if sess.kind == "local":
                await sess.proc.stdin.drain()
        except Exception as e:
            return {"ok": False, "error": f"写入失败: {e}"}
        return {"ok": True}

    async def resize(self, sid: str, cols: int, rows: int) -> dict:
        sess = self._get(sid)
        if sess is None:
            return {"ok": False}
        cols = _clamp(cols, 40, 500, 100)
        rows = _clamp(rows, 12, 200, 28)
        sess.cols, sess.rows = cols, rows
        if sess.kind == "ssh":
            try:
                await sess.proc.change_term_size(cols, rows)
            except Exception:
                pass
        elif sess.kind == "local" and sess.pty and sess.master is not None:
            try:
                import fcntl
                import struct
                import termios

                fcntl.ioctl(sess.master, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:
                pass
        return {"ok": True}

    async def close(self, sid: str) -> None:
        sess = self.sessions.pop(sid or "", None)
        if sess is None or sess.closed:
            return
        sess.closed = True
        try:
            if sess.kind == "ssh":
                try:
                    sess.proc.close()
                except Exception:
                    pass
                if sess.conn:
                    sess.conn.close()
            elif sess.pty and sess.master is not None:
                loop = asyncio.get_running_loop()
                try:
                    loop.remove_reader(sess.master)
                except Exception:
                    pass
                try:
                    os.close(sess.master)
                except Exception:
                    pass
                try:
                    os.kill(sess.pid, 9)
                except Exception:
                    pass
            else:
                try:
                    sess.proc.kill()
                except Exception:
                    pass
        finally:
            logger.info(f"[server_monitor][终端] 会话关闭 {sid[:8]} @ {sess.target_name}")

    # ------------------------------------------------------------------
    # SSE 输出流
    # ------------------------------------------------------------------

    async def stream_events(self, sid: str):
        """SSE 事件流：输出块（JSON 转义）+ 心跳。客户端断开时自动关闭会话。

        注意：stream_response 是裸的 Starlette StreamingResponse，
        必须产出 *SSE 文本帧*（"data: ...\\n\\n"），而不是 dict。
        """
        sess = self._get(sid)
        if sess is None:
            yield 'data: {"e": "会话不存在或已关闭"}\n\n'
            return
        try:
            while True:
                if sess.closed:
                    yield 'data: {"e": "closed"}\n\n'
                    return
                try:
                    chunk = await asyncio.wait_for(sess.queue.get(), timeout=HEARTBEAT)
                    yield "data: " + json.dumps({"d": chunk}) + "\n\n"
                except asyncio.TimeoutError:
                    yield "data: " + json.dumps({"h": 1}) + "\n\n"  # 心跳
        except asyncio.CancelledError:
            # 客户端断开（页面刷新/面板关闭/网络中断）：回收会话
            await self.close(sid)
            raise
