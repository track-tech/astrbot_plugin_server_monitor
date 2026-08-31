"""LLM 调用统计：实时（每分钟桶 + 最近调用）与按日历史（可持久化）。

由插件 main.py 的 on_llm_request / on_llm_response 钩子驱动：
    stats.on_request(model, session_id)
    stats.on_response(LLMResponse)
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional

MINUTE = 60
HISTORY_DAYS = 90


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _new_bucket(ts: float) -> dict:
    return {"m": int(ts // MINUTE), "calls": 0, "ok": 0, "err": 0,
            "input": 0, "cached": 0, "output": 0, "dur": 0.0, "n": 0}


def _new_daily() -> dict:
    return {"calls": 0, "ok": 0, "err": 0, "input": 0, "cached": 0, "output": 0,
            "dur": 0.0, "n": 0}


class LLMStats:
    """线程不安全（仅事件循环内访问）。"""

    def __init__(self, persist_path: Optional[str] = None,
                 max_recent: int = 50, max_minutes: int = 180):
        self.persist_path = persist_path
        self.minute: deque = deque(maxlen=max_minutes)
        self.minute_map: dict = {}
        self.recent: deque = deque(maxlen=max_recent)
        self.daily: dict = {}          # date -> model -> agg
        self.inflight: deque = deque(maxlen=64)  # (ts, model, session)
        self._last_save = 0.0
        self.started_at = time.time()
        self._load()

    # ------------------------------------------------------------------
    # 钩子入口
    # ------------------------------------------------------------------

    def on_request(self, model: Optional[str], session_id: Optional[str] = None) -> None:
        now = time.time()
        self.inflight.append((now, model or "", session_id or ""))
        b = self._bucket(now)
        b["calls"] += 1

    def on_response(self, resp: Any) -> None:
        now = time.time()
        usage = getattr(resp, "usage", None)
        u_in = int(getattr(usage, "input_other", 0) or 0)
        u_cached = int(getattr(usage, "input_cached", 0) or 0)
        u_out = int(getattr(usage, "output", 0) or 0)

        is_err = (getattr(resp, "role", "assistant") == "err")

        # 与最早的未完成请求配对，估算耗时
        model, session = "", ""
        dur = None
        if self.inflight:
            t0, model, session = self.inflight.popleft()
            dur = max(0.0, now - t0)
            # 请求发出超过 10 分钟才返回视为陈旧配对，不计时长
            if now - t0 > 600:
                dur = None
        else:
            raw = getattr(resp, "raw_completion", None)
            model = str(getattr(raw, "model", "") or "")

        b = self._bucket(now)
        b["input"] += u_in + u_cached
        b["cached"] += u_cached
        b["output"] += u_out
        if is_err:
            b["err"] += 1
        else:
            b["ok"] += 1
        if dur is not None:
            b["dur"] += dur
            b["n"] += 1

        self._daily_add(_today(), model or "unknown", u_in, u_cached, u_out, is_err, dur)

        self.recent.append({
            "ts": now, "model": model or "unknown", "session": session,
            "ok": not is_err,
            "input": u_in + u_cached, "cached": u_cached, "output": u_out,
            "dur": round(dur, 2) if dur is not None else None,
        })
        self._maybe_save()

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    def _bucket(self, ts: float) -> dict:
        m = int(ts // MINUTE)
        b = self.minute_map.get(m)
        if b is None:
            b = _new_bucket(ts)
            self.minute_map[m] = b
            self.minute.append(b)
            # 清理过期桶
            while self.minute and self.minute[0]["m"] < m - self.minute.maxlen:
                old = self.minute.popleft()
                self.minute_map.pop(old["m"], None)
        return b

    def _daily_add(self, day: str, model: str, u_in: int, u_cached: int,
                   u_out: int, is_err: bool, dur: Optional[float]) -> None:
        day_data = self.daily.setdefault(day, {})
        agg = day_data.setdefault(model, _new_daily())
        agg["calls"] += 1
        agg["input"] += u_in + u_cached
        agg["cached"] += u_cached
        agg["output"] += u_out
        if is_err:
            agg["err"] += 1
        else:
            agg["ok"] += 1
        if dur is not None:
            agg["dur"] += dur
            agg["n"] += 1
        # 只保留最近 N 天
        if len(self.daily) > HISTORY_DAYS:
            for k in sorted(self.daily.keys())[: len(self.daily) - HISTORY_DAYS]:
                self.daily.pop(k, None)

    # ------------------------------------------------------------------
    # 导出 / 持久化
    # ------------------------------------------------------------------

    def export(self) -> dict:
        now = time.time()
        today = _today()
        # 今日 + 全模型合计
        day_data = self.daily.get(today, {})
        total = _new_daily()
        for agg in day_data.values():
            for k in ("calls", "ok", "err", "input", "cached", "output", "dur", "n"):
                total[k] += agg[k]
        inp = total["input"]
        hit = round(100.0 * total["cached"] / inp, 1) if inp > 0 else None
        avg_ms = round(1000.0 * total["dur"] / total["n"]) if total["n"] else None

        # 实时：最近 5 分钟窗口
        cur_m = int(now // MINUTE)
        win_calls = win_err = 0
        for m in range(cur_m - 4, cur_m + 1):
            b = self.minute_map.get(m)
            if b:
                win_calls += b["calls"]
                win_err += b["err"]

        minute_series = [self.minute_map.get(m, {"calls": 0})["calls"]
                         for m in range(cur_m - 59, cur_m + 1)]

        models_out = {}
        for mname, agg in sorted(day_data.items(), key=lambda kv: -kv[1]["calls"]):
            inp2 = agg["input"]
            models_out[mname] = {
                "calls": agg["calls"], "ok": agg["ok"], "err": agg["err"],
                "input": inp2, "cached": agg["cached"], "output": agg["output"],
                "cache_hit": round(100.0 * agg["cached"] / inp2, 1) if inp2 > 0 else None,
                "avg_ms": round(1000.0 * agg["dur"] / agg["n"]) if agg["n"] else None,
            }

        return {
            "today": {
                "calls": total["calls"], "ok": total["ok"], "err": total["err"],
                "input": inp, "cached": total["cached"], "output": total["output"],
                "cache_hit": hit, "avg_ms": avg_ms,
            },
            "rpm5": win_calls,
            "inflight": len(self.inflight),
            "minute_calls": minute_series,
            "models": models_out,
            "recent": list(self.recent),
            "days": {k: {m: a for m, a in v.items()} for k, v in list(self.daily.items())[-7:]},
        }

    def _maybe_save(self) -> None:
        if not self.persist_path:
            return
        if time.time() - self._last_save < 300:
            return
        self._last_save = time.time()
        try:
            self.save()
        except Exception:
            pass

    def save(self) -> None:
        if not self.persist_path:
            return
        os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
        tmp = self.persist_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"daily": self.daily}, f, ensure_ascii=False)
        os.replace(tmp, self.persist_path)

    def _load(self) -> None:
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            daily = data.get("daily") or {}
            cutoff = _today()
            for day, models in daily.items():
                if day <= cutoff:  # 只恢复今天及更早，且顺带清理超期
                    self.daily[day] = models
            if len(self.daily) > HISTORY_DAYS:
                for k in sorted(self.daily.keys())[: len(self.daily) - HISTORY_DAYS]:
                    self.daily.pop(k, None)
        except Exception:
            pass
