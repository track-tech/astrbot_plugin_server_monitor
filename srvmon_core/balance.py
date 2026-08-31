"""模型余额查询：支持 DeepSeek / Moonshot(Kimi) / SiliconFlow / OpenAI Billing 兼容端点，
以及 one-api / new-api 系中转站（/api/user/self + 系统访问令牌）。

余额来源在插件配置 `llm_balance_sources` 中以模板列表配置，周期查询后由
MonitorService 汇总进 /overview 响应。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("server_monitor")

DEFAULT_BASE = {
    "deepseek": "https://api.deepseek.com",
    "moonshot": "https://api.moonshot.cn",
    "siliconflow": "https://api.siliconflow.cn",
    "openai_billing": "https://api.openai.com/v1",
    "oneapi": "",
}
TYPE_NAMES = {
    "deepseek": "DeepSeek",
    "moonshot": "Moonshot (Kimi)",
    "siliconflow": "SiliconFlow",
    "openai_billing": "OpenAI Billing",
    "oneapi": "One-API / New-API 中转",
}
ONEAPI_QUOTA_PER_UNIT = 500000  # one-api 系默认：50 万 quota = 1 USD


class _NotJson(Exception):
    """HTTP 200 但返回体不是 JSON（HTML 页面 / 空响应），当作路径不可用处理。"""


def _clamp(v, lo, hi, default) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _norm_base(base: str, btype: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE.get(btype, "")
    return base


def parse_balance(btype: str, data: Any, *, oneapi_unit: float = 500000.0,
                  oneapi_rate: float = 1.0, oneapi_display: str = "USD") -> dict:
    """从各类型端点的 JSON 响应中提取余额字段（纯函数，便于测试）。

    oneapi 类型的换算参数来自站点 /api/status 公开配置：
      金额基准 = quota / quota_per_unit；站点按 CNY 展示时再乘 usd_exchange_rate。
    """
    out = {"currency": None, "total": None, "granted": None, "used": None}
    if btype == "deepseek":
        infos = (data or {}).get("balance_infos") or [{}]
        info = infos[0]
        out["currency"] = info.get("currency") or "CNY"
        out["total"] = _f(info.get("total_balance"))
        out["granted"] = _f(info.get("granted_balance"))
    elif btype == "moonshot":
        d = (data or {}).get("data") or {}
        out["currency"] = "CNY"
        out["total"] = _f(d.get("available_balance"))
        out["granted"] = _f(d.get("voucher_balance"))
    elif btype == "siliconflow":
        d = (data or {}).get("data") or {}
        out["currency"] = "CNY"
        out["total"] = _f(d.get("balance"))
    elif btype == "openai_billing":
        out["currency"] = "USD"
        hard = _f((data or {}).get("hard_limit_usd"))
        out["total"] = hard
        out["_hard"] = hard
    elif btype == "oneapi":
        d = (data or {}).get("data") or {}
        quota = _f(d.get("quota"))
        used_q = _f(d.get("used_quota"))
        unit = _f(oneapi_unit) or 500000.0
        rate = _f(oneapi_rate)
        if rate is None or rate <= 0:
            rate = 1.0
        if oneapi_display == "CNY":
            out["currency"] = "CNY"
            out["total"] = round(quota / unit * rate, 4) if quota is not None else None
            out["used"] = round(used_q / unit * rate, 4) if used_q is not None else None
        else:
            out["currency"] = "USD"
            out["total"] = round(quota / unit, 4) if quota is not None else None
            out["used"] = round(used_q / unit, 4) if used_q is not None else None
    return {k: v for k, v in out.items() if v is not None or k in ("currency",)}


def _f(v):
    try:
        f = float(v)
        return round(f, 4)
    except (TypeError, ValueError):
        return None


class BalanceService:
    def __init__(self, config: dict):
        cfg = config or {}
        self.interval = _clamp(cfg.get("llm_balance_interval"), 60, 86400, 600)
        self.sources: List[dict] = []
        used_names = set()
        for i, item in enumerate(cfg.get("llm_balance_sources") or []):
            if not isinstance(item, dict):
                continue
            btype = str(item.get("template") or "deepseek").lower()
            if btype not in DEFAULT_BASE:
                btype = "deepseek"
            name = str(item.get("name") or "").strip() or TYPE_NAMES[btype]
            base = _norm_base(str(item.get("base_url") or ""), btype)
            key = str(item.get("api_key") or item.get("access_token") or "").strip()
            user_id = str(item.get("user_id") or "").strip()
            if not key:
                continue  # 无密钥的条目没有意义
            if not base:
                logger.warning(f"[server_monitor] 余额来源 {name}: 未填写 API 地址，已跳过")
                continue
            n, k = 2, name
            while name in used_names:
                name = f"{k}-{n}"
                n += 1
            used_names.add(name)
            self.sources.append(
                {"name": name, "type": btype, "base": base, "key": key, "user_id": user_id}
            )
        self.results: Dict[str, dict] = {}
        self._root_cache: Dict[str, str] = {}
        self._site_cfg: Dict[str, tuple] = {}  # name -> 已验证可用的根地址
        self._stopping = False
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return bool(self.sources)

    def start(self) -> None:
        if self.enabled and not self._task:
            self._task = asyncio.create_task(self._loop(), name="srvmon-balance")

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
                logger.debug(f"[server_monitor] 余额查询轮次异常: {e}")
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(30.0, self.interval - elapsed))

    async def sample_all(self) -> None:
        jobs = [self._query_one(s) for s in self.sources]
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    def _candidate_roots(self, s: dict) -> List[str]:
        """生成候选根地址：带 /v1、不带 /v1 都试一遍；成功的会被记忆并优先。"""
        b = (s.get("base") or "").rstrip("/")
        roots = []
        if s.get("name") in self._root_cache:
            roots.append(self._root_cache[s["name"]])
        if b.endswith("/v1"):
            roots += [b, b[:-3].rstrip("/")]
        else:
            roots += [b, b + "/v1"]
        out = []
        for r in roots:
            if r and r not in out:
                out.append(r)
        return out

    async def _get_json(self, sess, url: str, headers: dict):
        """返回 (status, json或文本)。

        HTTP 200 但响应体不是 JSON（中转站前端页面 / 空响应）时抛 _NotJson，
        由候选循环当作路径不可用继续尝试下一个根地址。
        """
        async with sess.get(url, headers=headers) as resp:
            text = await resp.text()
            if resp.status == 200:
                try:
                    return 200, json.loads(text)
                except ValueError:
                    snippet = text.strip()[:80]
                    raise _NotJson(
                        "返回非 JSON 内容"
                        + (f"：{snippet}" if snippet else "（空响应）")
                        + f" ({'GET ' + url})"
                    )
            return resp.status, text[:200]

    async def _oneapi_site_cfg(self, sess, root: str) -> dict:
        """读取站点 /api/status 公开配置（quota_per_unit / 展示币种 / 汇率），缓存 1 小时。"""
        now = time.time()
        cached = self._site_cfg.get(root)
        if cached and now - cached[1] < 3600:
            return cached[0]
        cfg = {"unit": 500000.0, "rate": 1.0, "display": "USD"}
        try:
            status, data = await self._get_json(sess, root + "/api/status", {})
            if status == 200 and isinstance(data, dict):
                d = data.get("data") or {}
                unit = _f(d.get("quota_per_unit"))
                rate = _f(d.get("usd_exchange_rate"))
                if unit and unit > 0:
                    cfg["unit"] = unit
                if d.get("quota_display_type") == "CNY":
                    cfg["display"] = "CNY"
                    if rate and rate > 0:
                        cfg["rate"] = rate
        except Exception:
            pass
        self._site_cfg[root] = (cfg, now)
        return cfg

    async def _query_one(self, s: dict) -> None:
        try:
            import aiohttp

            if s["type"] == "oneapi":
                headers = {"Authorization": f"Bearer {s['key']}"}
                if s.get("user_id"):
                    headers["New-Api-User-Id"] = s["user_id"]
            else:
                headers = {"Authorization": f"Bearer {s['key']}"}
            timeout = aiohttp.ClientTimeout(total=12)
            btype = s["type"]
            used = None
            last_err = ""
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                paths = {
                    "deepseek": "/user/balance",
                    "moonshot": "/v1/users/me/balance",
                    "siliconflow": "/v1/user/info",
                    "openai_billing": "/dashboard/billing/subscription",
                    "oneapi": "/api/user/self",
                }
                for root in self._candidate_roots(s):
                    base_url = root + paths[btype]
                    site_cfg = None
                    if btype == "oneapi":
                        site_cfg = await self._oneapi_site_cfg(sess, root)
                    try:
                        status, data = await self._get_json(sess, base_url, headers)
                    except _NotJson as e:
                        last_err = str(e)
                        continue  # 非 JSON（HTML 页面），换下一个根地址
                    if status in (404, 405):
                        last_err = f"HTTP {status}: 余额接口不存在 ({'GET ' + base_url})"
                        continue  # 路径不对，换下一个根地址
                    if status != 200:
                        # 401/403 等说明 URL 有效、鉴权失败，无需再尝试其他根地址
                        last_err = f"HTTP {status}: {str(data)[:160]} ({'GET ' + base_url})"
                        break
                    if btype == "oneapi" and isinstance(data, dict) and data.get("success") is False:
                        last_err = f"站点返回失败: {data.get('message') or '未知原因'} ({'GET ' + base_url})"
                        break
                    if btype == "oneapi":
                        parsed = parse_balance(
                            btype, data if isinstance(data, dict) else {},
                            oneapi_unit=(site_cfg or {}).get("unit", 500000.0),
                            oneapi_rate=(site_cfg or {}).get("rate", 1.0),
                            oneapi_display=(site_cfg or {}).get("display", "USD"),
                        )
                    else:
                        parsed = parse_balance(btype, data if isinstance(data, dict) else {})

                    if btype == "openai_billing":
                        today = time.strftime("%Y-%m-01")
                        nxt_y, nxt_m = int(time.strftime("%Y")), int(time.strftime("%m")) + 1
                        if nxt_m > 12:
                            nxt_y, nxt_m = nxt_y + 1, 1
                        end = f"{nxt_y:04d}-{nxt_m:02d}-01"
                        usage_url = (root + "/dashboard/billing/usage?"
                                     + urllib.parse.urlencode({"start_date": today, "end_date": end}))
                        try:
                            st2, usage = await self._get_json(sess, usage_url, headers)
                        except _NotJson as e:
                            last_err = str(e)
                            continue
                        if st2 != 200:
                            last_err = f"HTTP {st2}: {str(usage)[:160]} ({'GET ' + usage_url})"
                            continue
                        used = round(_f((usage or {}).get("total_usage") or 0) / 100.0, 4)

                    self._root_cache[s["name"]] = root  # 记住可用的根地址
                    result = {
                        "name": s["name"], "type": btype,
                        "type_name": TYPE_NAMES[btype],
                        "ok": True, "error": None,
                        "used": used, "ts": time.time(),
                        **parsed,
                    }
                    if btype == "openai_billing" and used is not None and parsed.get("total") is not None:
                        result["total"] = round(parsed["total"] - used, 4)
                        result["used"] = used
                    self.results[s["name"]] = result
                    return
                msg = last_err or "所有候选地址均不可用"
                if btype != "oneapi" and ("余额接口不存在" in msg or "非 JSON" in msg):
                    # One-API/New-API 系中转站自动兜底：尝试 /api/user/self（站点访问令牌）
                    auto = await self._try_oneapi_fallback(sess, s)
                    if auto:
                        self.results[s["name"]] = auto
                        return
                    msg += "；该地址可能是 One-API/New-API 系中转站，请改用「One-API / New-API 中转」类型并填入系统访问令牌"
                raise RuntimeError(msg)
        except asyncio.TimeoutError:
            self.results[s["name"]] = self._err_result(s, "查询超时（12s）")
        except Exception as e:
            self.results[s["name"]] = self._err_result(s, str(e)[:240])

    async def _try_oneapi_fallback(self, sess, s: dict) -> Optional[dict]:
        """DeepSeek 类型在 One-API/New-API 系中转站上的自动兜底。

        中转站没有 /user/balance 接口时，尝试 /api/user/self（同一密钥，
        new-api 接受接口 Key 或系统访问令牌）。成功则返回结果，失败返回 None。
        """
        headers = {"Authorization": f"Bearer {s['key']}"}
        if s.get("user_id"):
            headers["New-Api-User-Id"] = s["user_id"]
        for root in self._candidate_roots(s):
            url = root + "/api/user/self"
            try:
                status, data = await self._get_json(sess, url, headers)
            except _NotJson:
                continue
            if status != 200:
                continue
            if not isinstance(data, dict) or data.get("success") is False:
                continue
            parsed = parse_balance("oneapi", data)
            if parsed.get("total") is None:
                continue
            return {
                "name": s["name"], "type": "oneapi",
                "type_name": TYPE_NAMES["oneapi"] + "（自动识别）",
                "ok": True, "error": None,
                "used": parsed.get("used"), "ts": time.time(),
                **parsed,
            }
        return None

    def _err_result(self, s: dict, err: str) -> dict:
        return {
            "name": s["name"], "type": s["type"], "type_name": TYPE_NAMES[s["type"]],
            "ok": False, "error": err, "currency": None, "total": None,
            "granted": None, "used": None, "ts": time.time(),
        }

    def export(self) -> list:
        return sorted(self.results.values(), key=lambda r: r["name"])
