"""AstrBot 服务器监控插件
========================

- 本机监控（psutil）+ 远程服务器监控（SSH / HTTP Agent）
- AstrBot WebUI 插件 Pages 实时看板（pages/monitor/index.html）
- 聊天指令：/server [detail|bind|unbind|test]（中文别名：服务器状态）
- 阈值告警推送（CPU / 内存 / 磁盘 / 离线），支持恢复通知与冷却

Web API：
    GET /api/v1/plugins/extensions/astrbot_plugin_server_monitor/overview
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# MessageChain 在不同版本中的导出位置略有差异，做兼容导入
try:
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover
    from astrbot.core.message.message_event_result import MessageChain

# astrbot.api.web 自较新版本开始提供，缺失时页面仍可用（降级为原始返回）
try:
    from astrbot.api.web import error_response, json_response, stream_response

    _HAS_WEB_API = True
except ImportError:  # pragma: no cover
    _HAS_WEB_API = False

PLUGIN_NAME = "astrbot_plugin_server_monitor"
PLUGIN_VERSION = "1.10.0"

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 关键：清除本插件子模块的导入缓存。AstrBot 热重载插件时只重新执行 main.py，
# 而 sys.modules 里缓存的旧版 srvmon_core.* 不会被刷新，导致新旧代码混用
# （表现为 __init__() got an unexpected keyword argument 之类的版本错位错误）。
for _mod in [m for m in sys.modules if m == "srvmon_core" or m.startswith("srvmon_core.")]:
    del sys.modules[_mod]

from srvmon_core.format import text_status  # noqa: E402
from srvmon_core.llmstats import LLMStats  # noqa: E402
from srvmon_core.service import MonitorService  # noqa: E402


def _llm_persist_path() -> Optional[str]:
    """AstrBot 数据目录下的持久化文件；拿不到则仅在内存统计。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME, "llm_daily.json")
    except Exception:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return os.path.join(
                get_astrbot_data_path(), "plugin_data", PLUGIN_NAME, "llm_daily.json"
            )
        except Exception:
            return None


def _ui_state_path() -> Optional[str]:
    """看板布局/刷新频率的服务端持久化文件。

    优先 AstrBot 数据目录；拿不到时兜底到插件安装目录自身，
    避免某些版本/部署形态下两个路径 API 都不可用导致保存被静默丢弃。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME, "ui_state.json")
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return os.path.join(
            get_astrbot_data_path(), "plugin_data", PLUGIN_NAME, "ui_state.json"
        )
    except Exception:
        pass
    try:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_state.json")
    except Exception:
        return None


@register(PLUGIN_NAME, "track-tech", "本地/云服务器状态实时监控看板与告警", PLUGIN_VERSION)
class ServerMonitorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service: Optional[MonitorService] = None
        self._boot_error = ""
        self.llmstats = None
        try:
            self.llmstats = LLMStats(persist_path=_llm_persist_path())
        except Exception as e:
            logger.error(f"[server_monitor] 初始化模型统计失败（已停用该功能）: {e}\n{traceback.format_exc()}")
        self._api_route = f"/{PLUGIN_NAME}/overview"
        self._boot_task: Optional[asyncio.Task] = None
        self._booted = False
        self._schedule_boot()

    # ------------------------------------------------------------------
    # 启动引导（注册 Web API + 启动采集循环）
    # ------------------------------------------------------------------

    def _schedule_boot(self) -> None:
        if self._booted:
            return
        try:
            self._boot_task = asyncio.create_task(self._boot())
            self._booted = True
        except RuntimeError:
            # 事件循环尚未运行（极少见），首次指令/页面访问时会再次尝试
            pass

    async def _boot(self) -> None:
        await self._register_api()
        logger.info(f"[server_monitor] 看板状态持久化文件: {_ui_state_path() or '不可用（布局将无法保存）'}")
        self._try_start_service()

    async def _register_api(self) -> None:
        """兼容同步/异步、不同签名的 register_web_api。"""
        fn = getattr(self.context, "register_web_api", None)
        if fn is None:
            logger.error(
                "[server_monitor] 当前 AstrBot 版本缺少 register_web_api，"
                "WebUI 页面将无法获取数据，请升级 AstrBot 至 v4.10+"
            )
            return
        routes = [
            (f"/{PLUGIN_NAME}/overview", self.api_overview, ["GET"], "获取服务器监控概览与历史数据"),
            (f"/{PLUGIN_NAME}/terminal/targets", self.api_term_targets, ["GET"], "获取终端可连接目标"),
            (f"/{PLUGIN_NAME}/terminal/open", self.api_term_open, ["POST"], "建立终端会话"),
            (f"/{PLUGIN_NAME}/terminal/stream", self.api_term_stream, ["GET"], "终端输出流（SSE）"),
            (f"/{PLUGIN_NAME}/terminal/input", self.api_term_input, ["POST"], "写入终端输入"),
            (f"/{PLUGIN_NAME}/terminal/resize", self.api_term_resize, ["POST"], "调整终端尺寸"),
            (f"/{PLUGIN_NAME}/terminal/close", self.api_term_close, ["POST"], "关闭终端会话"),
            (f"/{PLUGIN_NAME}/ui/save", self.api_ui_save, ["POST"], "保存看板布局与刷新频率"),
            (f"/{PLUGIN_NAME}/ui/load", self.api_ui_load, ["GET"], "读取看板布局与刷新频率"),
        ]
        for route, handler, methods, desc in routes:
            last_err = None
            for args in ((route, handler, methods, desc), (route, handler, methods)):
                try:
                    ret = fn(*args)
                    if asyncio.iscoroutine(ret):
                        await ret
                    logger.info(f"[server_monitor] Web API 已注册: {methods[0]} {route}")
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
            if last_err is not None:
                logger.error(f"[server_monitor] 注册 Web API 失败 {route}: {last_err}")

    def _try_start_service(self) -> None:
        if self.service is not None:
            return
        try:
            try:
                self.service = MonitorService(
                    self.config, alert_sender=self._alert_sender, llm_stats=self.llmstats
                )
            except TypeError:
                # 兜底：万一 srvmon_core 仍是旧版缓存（无 llm_stats 参数），降级启动
                self.service = MonitorService(self.config, alert_sender=self._alert_sender)
            self.service.start()
            self._boot_error = ""
            modes = [f"{n}({m['mode']})" for n, m in self.service.display.items()]
            logger.info(f"[server_monitor] 监控服务已启动，目标: {', '.join(modes) or '无'}")
        except Exception as e:
            self._boot_error = f"{type(e).__name__}: {e}"
            logger.error(
                f"[server_monitor] 启动监控服务失败: {e}\n{traceback.format_exc()}"
            )

    async def _alert_sender(self, text: str) -> None:
        umo = ""
        try:
            umo = (self.config.get("alerts") or {}).get("alert_umo") or ""
        except Exception:
            pass
        if not umo:
            logger.debug("[server_monitor] 未绑定告警会话，忽略本次告警")
            return
        await self.context.send_message(umo, MessageChain().message(text))

    # ------------------------------------------------------------------
    # Web API（供插件 Pages 页面调用）
    # ------------------------------------------------------------------

    @staticmethod
    def _json(data: dict, status: int = 200):
        if not _HAS_WEB_API:
            return data
        try:  # 当前版本 status 为 keyword-only；兼容可能的位置参数旧形态
            return json_response(data, status_code=status)
        except TypeError:
            return json_response(data, status)

    @staticmethod
    def _err(message: str, status: int = 500):
        if not _HAS_WEB_API:
            return {"status": "error", "message": message}
        try:
            return error_response(message, status_code=status)
        except TypeError:
            return error_response(message, status)

    async def api_overview(self, request: Any = None):
        """GET overview: 所有服务器的当前快照 + 历史序列。"""
        request = self._get_request(request)
        try:
            if self.service is None:  # 自愈：启动任务失败时按需再试
                self._try_start_service()
            if self.service is None:
                msg = "监控服务尚未就绪"
                if self._boot_error:
                    msg += f"（启动失败: {self._boot_error}），请检查日志或反馈此信息"
                return self._json({"ok": False, "message": msg}, 503)
            points = 0
            try:
                points = int((getattr(request, "query", {}) or {}).get("points") or 0)
            except Exception:
                points = 0
            data = self.service.build_overview(points or None)
            try:
                data["plugin_version"] = PLUGIN_VERSION
            except Exception:
                pass
            return self._json(data)
        except Exception as e:
            logger.error(f"[server_monitor] overview 接口异常: {e}\n{traceback.format_exc()}")
            return self._err(f"获取监控数据失败: {e}")

    # ------------------------------------------------------------------
    # LLM 调用统计钩子
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        """记录 LLM 请求（模型、会话），用于实时调用统计。"""
        try:
            self.llmstats.on_request(getattr(req, "model", None),
                                     getattr(req, "session_id", None))
        except Exception:
            pass

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: Any):
        """记录 LLM 响应（tokens/缓存命中/耗时/成败）。"""
        try:
            self.llmstats.on_response(resp)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Web API（终端）
    # ------------------------------------------------------------------

    @staticmethod
    def _get_request(request: Any):
        if request is not None:
            return request
        try:
            from astrbot.api.web import request as proxy

            return proxy
        except Exception:
            return None

    @staticmethod
    def _username(request: Any) -> str:
        try:
            return str(getattr(request, "username", "") or "")
        except Exception:
            return ""

    async def api_term_targets(self, request: Any = None):
        request = self._get_request(request)
        try:
            if self.service is None:
                self._try_start_service()
            if self.service is None:
                return self._json({"ok": False, "message": "监控服务尚未就绪"}, 503)
            return self._json(self.service.terminal.targets())
        except Exception as e:
            logger.error(f"[server_monitor] terminal/targets 异常: {e}")
            return self._err(f"获取终端目标失败: {e}")

    async def _term_body(self, request: Any) -> dict:
        try:
            try:
                body = await request.json(default={})
            except TypeError:
                body = await request.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    def _term_ready(self):
        if self.service is None:
            self._try_start_service()
        if self.service is None:
            return None
        return self.service.terminal

    async def api_ui_save(self, request: Any = None):
        """POST ui/save：保存看板布局与全局刷新频率（服务端持久化）。

        与已有状态文件做字段级合并：items/auto/gint 与 saved（自定义布局预设）
        可分开保存，互不覆盖。"""
        try:
            request = self._get_request(request)
            body = {}
            try:
                try:
                    body = await request.json(default={}) or {}
                except TypeError:
                    body = await request.json() or {}
            except Exception as e:
                logger.warning(f"[server_monitor] ui/save 请求体解析失败: {e}")
                body = {}
            items = body.get("items")
            saved = body.get("saved")
            if not isinstance(items, list) and not isinstance(saved, list):
                return self._json({"ok": False, "error": "布局数据无效"}, 400)
            path = _ui_state_path()
            if not path:
                logger.error("[server_monitor] ui/save 无可用持久化路径，看板布局未能保存")
                return self._json({"ok": False, "error": "服务端无可用持久化路径"})
            state: dict = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if isinstance(existing, dict):
                        state = existing
                except Exception:
                    state = {}
            if isinstance(items, list):
                state["auto"] = bool(body.get("auto"))
                state["gint"] = body.get("gint")
                state["items"] = items
                state["tas"] = bool(body.get("tas", True))
                state["ts"] = time.time()
            if isinstance(saved, list):
                state["saved"] = saved
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, path)
            return self._json({"ok": True, "persisted": True})
        except Exception as e:
            logger.error(f"[server_monitor] ui/save 异常: {e}")
            return self._err(f"保存布局失败: {e}")

    async def api_ui_load(self, request: Any = None):
        """GET ui/load：读取看板布局与全局刷新频率。"""
        try:
            path = _ui_state_path()
            if not path or not os.path.exists(path):
                return self._json({"ok": True, "empty": True})
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return self._json({"ok": True, **state})
        except Exception as e:
            logger.error(f"[server_monitor] ui/load 异常: {e}")
            return self._json({"ok": True, "empty": True})

    async def api_term_open(self, request: Any = None):
        request = self._get_request(request)
        term = self._term_ready()
        if term is None:
            return self._json({"ok": False, "error": "监控服务尚未就绪"}, 503)
        body = await self._term_body(request)
        try:
            return self._json(await term.open(
                str(body.get("target") or "local"),
                int(body.get("cols") or 100), int(body.get("rows") or 28),
                username=self._username(request),
            ))
        except Exception as e:
            logger.error(f"[server_monitor] terminal/open 失败: {e}")
            return self._json({"ok": False, "error": f"连接失败: {e}"})

    async def api_term_stream(self, request: Any = None):
        request = self._get_request(request)
        term = self._term_ready()
        if term is None:
            return self._json({"ok": False, "error": "监控服务尚未就绪"}, 503)
        sid = ""
        try:
            sid = str((getattr(request, "query", {}) or {}).get("sid") or "")
        except Exception:
            pass
        if not _HAS_WEB_API or stream_response is None:
            return self._json({"ok": False, "error": "当前 AstrBot 版本不支持 SSE 流"}, 503)
        return stream_response(
            term.stream_events(sid),
            headers={
                "X-Accel-Buffering": "no",  # 反代（nginx 等）禁用缓冲
                "Cache-Control": "no-cache",
            },
        )

    async def api_term_input(self, request: Any = None):
        request = self._get_request(request)
        term = self._term_ready()
        if term is None:
            return self._json({"ok": False, "error": "监控服务尚未就绪"}, 503)
        body = await self._term_body(request)
        return self._json(await term.write(str(body.get("sid") or ""), str(body.get("data") or "")))

    async def api_term_resize(self, request: Any = None):
        request = self._get_request(request)
        term = self._term_ready()
        if term is None:
            return self._json({"ok": False, "error": "监控服务尚未就绪"}, 503)
        body = await self._term_body(request)
        return self._json(await term.resize(
            str(body.get("sid") or ""), int(body.get("cols") or 100), int(body.get("rows") or 28)
        ))

    async def api_term_close(self, request: Any = None):
        request = self._get_request(request)
        term = self._term_ready()
        if term is None:
            return self._json({"ok": False, "error": "监控服务尚未就绪"}, 503)
        body = await self._term_body(request)
        await term.close(str(body.get("sid") or ""))
        return self._json({"ok": True})

    # ------------------------------------------------------------------
    # 聊天指令
    # ------------------------------------------------------------------

    @filter.command("server")
    async def server_cmd(self, event: AstrMessageEvent):
        """查询服务器状态：/server [detail|bind|unbind|test]"""
        async for r in self._server_flow(event):
            yield r

    @filter.command("服务器状态")
    async def server_cmd_cn(self, event: AstrMessageEvent):
        """查询服务器状态（/server 的中文别名）"""
        async for r in self._server_flow(event):
            yield r

    @filter.command("model")
    async def model_cmd(self, event: AstrMessageEvent):
        """查看模型调用统计：/model [history]"""
        self._schedule_boot()
        tokens = (event.message_str or "").strip().split()
        sub = tokens[1].lower() if len(tokens) > 1 else ""
        if sub in ("history", "历史"):
            yield event.plain_result(self._llm_history_text())
        else:
            yield event.plain_result(self._llm_stats_text())

    def _llm_stats_text(self) -> str:
        ex = self.llmstats.export()
        t = ex.get("today") or {}
        lines = [
            "🤖 模型调用统计（今日）",
            f"   调用 {t.get('calls', 0)} 次（成功 {t.get('ok', 0)} / 失败 {t.get('err', 0)}）",
            f"   Tokens 输入 {t.get('input', 0)}（缓存 {t.get('cached', 0)}）· 输出 {t.get('output', 0)}",
            f"   缓存命中率 {t.get('cache_hit') if t.get('cache_hit') is not None else '-'}%"
            f" · 平均耗时 {t.get('avg_ms') if t.get('avg_ms') is not None else '-'} ms",
            f"   近 5 分钟调用 {ex.get('rpm5', 0)} 次",
        ]
        models = ex.get("models") or {}
        if models:
            lines.append("   —— 按模型 ——")
            for m, a in list(models.items())[:5]:
                lines.append(
                    f"   {m}: {a['calls']} 次 · {a['output']} out"
                    f" · 命中 {a['cache_hit'] if a['cache_hit'] is not None else '-'}%"
                )
        return "\n".join(lines)

    def _llm_history_text(self) -> str:
        days = self.llmstats.export().get("days") or {}
        if not days:
            return "暂无历史数据。"
        lines = ["🤖 模型调用历史（近 7 天）"]
        for day in sorted(days.keys())[-7:]:
            total_calls = sum(a.get("calls", 0) for a in days[day].values())
            total_out = sum(a.get("output", 0) for a in days[day].values())
            lines.append(f"   {day}: {total_calls} 次 · {total_out} out")
        return "\n".join(lines)

    async def _server_flow(self, event: AstrMessageEvent):
        self._schedule_boot()
        tokens = (event.message_str or "").strip().split()
        if tokens and tokens[0].lstrip("/").lower() in ("server", "服务器状态", "srv"):
            tokens = tokens[1:]
        sub = tokens[0].lower() if tokens else ""

        if self.service is None and sub != "bind":
            yield event.plain_result("⏳ 监控服务尚未就绪，请稍后再试；若持续出现请检查插件配置与依赖。")
            return

        if sub in ("", "status"):
            yield event.plain_result(text_status(self.service, detail=False))
        elif sub == "detail":
            yield event.plain_result(text_status(self.service, detail=True))
        elif sub == "bind":
            alerts = self.config.get("alerts") or {}
            alerts["alert_umo"] = event.unified_msg_origin
            alerts["enabled"] = True
            self.config["alerts"] = alerts
            self.config.save_config()
            yield event.plain_result(
                "✅ 已将当前会话绑定为告警推送目标，并自动开启告警。\n"
                f"会话: {event.unified_msg_origin}\n"
                "发送 /server test 可测试推送，/server unbind 解绑。"
            )
        elif sub == "unbind":
            alerts = self.config.get("alerts") or {}
            alerts["alert_umo"] = ""
            self.config["alerts"] = alerts
            self.config.save_config()
            yield event.plain_result("✅ 已解除告警绑定。")
        elif sub == "test":
            yield event.plain_result(await self._send_test_alert())
        else:
            yield event.plain_result(self._usage())

    async def _send_test_alert(self) -> str:
        umo = ""
        try:
            umo = (self.config.get("alerts") or {}).get("alert_umo") or ""
        except Exception:
            pass
        if not umo:
            return "⚠️ 尚未绑定告警会话，请先在目标聊天中发送 /server bind。"
        text = f"🔔 [服务器监控] 这是一条测试消息 ({time.strftime('%H:%M:%S')})"
        try:
            await self.context.send_message(umo, MessageChain().message(text))
            return f"✅ 测试消息已发送到 {umo}"
        except Exception as e:
            return f"❌ 发送失败: {e}"

    @staticmethod
    def _usage() -> str:
        return (
            "📊 服务器监控指令\n"
            "/server（或 服务器状态）— 状态概览\n"
            "/server detail — 详细信息（含 Top 进程）\n"
            "/server bind — 绑定当前会话接收告警\n"
            "/server unbind — 解除告警绑定\n"
            "/server test — 发送测试告警\n"
            "/model — 模型调用统计（/model history 查看近 7 天）"
        )

    # ------------------------------------------------------------------
    # 卸载 / 停用
    # ------------------------------------------------------------------

    async def terminate(self):
        if self._boot_task:
            self._boot_task.cancel()
        try:
            self.llmstats.save()  # 落盘模型统计
        except Exception:
            pass
        if self.service:
            try:
                await self.service.stop()
            except Exception as e:
                logger.warning(f"[server_monitor] 停止监控服务异常: {e}")
        try:
            fn = getattr(self.context, "unregister_web_api", None)
            if fn is not None:
                for route in (
                    self._api_route,
                    f"/{PLUGIN_NAME}/terminal/targets",
                    f"/{PLUGIN_NAME}/terminal/open",
                    f"/{PLUGIN_NAME}/terminal/stream",
                    f"/{PLUGIN_NAME}/terminal/input",
                    f"/{PLUGIN_NAME}/terminal/resize",
                    f"/{PLUGIN_NAME}/terminal/close",
                    f"/{PLUGIN_NAME}/ui/save",
                    f"/{PLUGIN_NAME}/ui/load",
                ):
                    ret = fn(route)
                    if asyncio.iscoroutine(ret):
                        await ret
        except Exception:
            pass
        logger.info("[server_monitor] 插件已卸载，监控任务已停止")
