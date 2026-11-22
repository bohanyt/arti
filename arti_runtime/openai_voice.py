"""Opt-in Responses API voice lane. Codex/Cursor stay the fallback.

Reuses openai_decisions transport, key file, prices and cap accounting.
Never prints, logs, or copies the key. Exceptions do not escape the public
async entry. Session USD cap is separate from Stardew decisions.
"""
from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from arti_runtime.openai_decisions import (
    MODEL_PRICES, TIER_MULTIPLIERS, _NoRedirect, _decimal, _read_key, http_call,
)

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_EFFORT = "low"
DEFAULT_TIER = "default"
DEFAULT_CAP_USD = "0.50"
DEFAULT_TIMEOUT_SEC = 20.0
DEFAULT_MAX_OUTPUT = 512
SCRIPT_CAP_USD = Decimal("1.00")
MODELS_URL = "https://api.openai.com/v1/models"
MODEL_NAME_FILTER = re.compile(r"audio|realtime|vision|transcribe|tts", re.I)
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _meta(**fields):
    row = dict(latency_sec=None, input_tokens=None, cached_input_tokens=None,
               output_tokens=None, cost_usd=None, model=None, outcome="error")
    row.update(fields)
    return row


def voice_settings(cfg):
    model = cfg.get("voice_api_model", DEFAULT_MODEL)
    effort = cfg.get("voice_api_effort", DEFAULT_EFFORT)
    tier = cfg.get("voice_api_service_tier", DEFAULT_TIER)
    if (not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,100}", model)
            or effort not in {"low", "medium"}):
        raise ValueError("openai_model_or_effort_invalid")
    if tier not in {"default", "priority"}:
        raise ValueError("openai_service_tier_invalid")
    prices = cfg.get("voice_api_prices", MODEL_PRICES)
    if not isinstance(prices, dict) or model not in prices:
        raise ValueError("openai_model_price_missing")
    price = prices[model]
    if not isinstance(price, (list, tuple)) or len(price) != 3:
        raise ValueError("openai_model_price_invalid")
    price = tuple(_decimal(v) for v in price)
    if price[0] <= 0 or price[2] <= 0 or price[1] > price[0]:
        raise ValueError("openai_model_price_invalid")
    cap = _decimal(cfg.get("voice_api_session_cap_usd", DEFAULT_CAP_USD))
    return model, effort, tier, price, cap


def _key(cfg):
    path = cfg.get("voice_api_env_file") or cfg.get("stardew_openai_env_file")
    return _read_key(dict(cfg, stardew_openai_env_file=path) if path else cfg)


def _timeout(cfg):
    timeout = float(cfg.get("voice_api_timeout_sec", DEFAULT_TIMEOUT_SEC))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("openai_timeout_invalid")
    return timeout


def _max_output(cfg):
    value = int(cfg.get("voice_api_max_output_tokens", DEFAULT_MAX_OUTPUT))
    if value < 1 or value > 4000:
        raise ValueError("openai_max_output_invalid")
    return value


class _Cap:
    def __init__(self, path, cap):
        self.path = Path(path) if path else None
        self.cap = cap
        self.lock = threading.RLock()
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self.uncertain = Decimal(0)
        self.errors = 0
        self.fallback = None
        self._load()

    def _load(self):
        if not (self.path and self.path.exists()):
            return
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self.spent = _decimal(state["spent_usd"])
            reserved = _decimal(state["reserved_usd"])
            self.uncertain = _decimal(state["uncertain_usd"]) + reserved
            self.errors = int(state["errors"])
            if state.get("fallback"):
                self.fallback = "previous_session_fallback"
            elif reserved:
                self.fallback = "unsettled_api_call"
        except Exception:
            self.fallback = "session_state_invalid"

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(dict(
                spent_usd=str(self.spent), reserved_usd=str(self.reserved),
                uncertain_usd=str(self.uncertain), errors=self.errors,
                fallback=bool(self.fallback))), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            self.fallback = "session_state_unwritable"

    def admit(self, reserve):
        with self.lock:
            if self.fallback:
                return False
            if self.spent + self.reserved + self.uncertain + reserve > self.cap:
                self.fallback = "usd_cap"
                self._save()
                return False
            self.reserved += reserve
            self._save()
            if self.fallback:
                self.reserved -= reserve
                return False
            return True

    def settle(self, reserve, usage, billed, price, failure):
        pin, pcache, pout = price
        cost = Decimal(0)
        with self.lock:
            self.reserved -= reserve
            if usage:
                tin, cached, tout = usage
                cost = ((tin - cached) * pin + cached * pcache + tout * pout) * Decimal(
                    TIER_MULTIPLIERS[billed]) / 1_000_000
                self.spent += cost
            else:
                self.uncertain += reserve
            self.errors = self.errors + 1 if failure else 0
            if self.spent + self.uncertain >= self.cap:
                self.fallback = "usd_cap"
            self._save()
        return cost


def _session(cfg, cap):
    path = cfg.get("voice_api_session_state_file") or None
    key = str(Path(path).resolve()) if path else None
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(key)
        if sess is None:
            sess = _Cap(path, cap)
            _SESSIONS[key] = sess
        return sess


def _output_text(data):
    return "".join(
        c.get("text", "") for o in data.get("output", []) if o.get("type") == "message"
        for c in o.get("content", []) if c.get("type") == "output_text"
    ).strip()


def _usage(data):
    u = data["usage"]
    counts = (u["input_tokens"], (u.get("input_tokens_details") or {}).get("cached_tokens", 0),
              u["output_tokens"])
    if any(type(n) is not int or n < 0 for n in counts) or counts[1] > counts[0]:
        raise ValueError("openai_usage_invalid")
    return counts


def _reserve(body, price, tier):
    pin, _pcache, pout = price
    max_out = int(body.get("max_output_tokens") or 0)
    multiplier = Decimal(TIER_MULTIPLIERS[tier])
    return ((len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 1024) * pin
            + max_out * pout) * multiplier / 1_000_000


def responses_post(body, cfg):
    """One Responses call with the voice session cap. Returns (data|None, meta)."""
    start = time.perf_counter()
    try:
        requested_model, _effort, tier, price, cap = voice_settings(cfg)
        timeout = _timeout(cfg)
        session = _session(cfg, cap)
        reserve = _reserve(body, price, tier)
        if not session.admit(reserve):
            return None, _meta(outcome="usd_cap", error_class="usd_cap")
        try:
            http = cfg.get("_voice_api_http") or http_call
            key = _key(cfg)
        except Exception:
            session.settle(reserve, None, tier, price, True)
            raise
    except Exception as exc:
        return None, _meta(error_class=type(exc).__name__,
                           latency_sec=round(time.perf_counter() - start, 4))
    data, usage, billed, failure = None, None, tier, None
    response_status, http_status, error_class = None, None, None
    try:
        data = http(body, key, timeout)
        response_status = data.get("status")
        if response_status not in {"completed", "incomplete", "failed", "cancelled"}:
            response_status = "unknown"
        usage = _usage(data)
        billed = data.get("service_tier")
        if billed not in TIER_MULTIPLIERS:
            billed = tier
        if response_status != "completed":
            failure, error_class, data = "api_error", "api_error", None
    except urllib.error.HTTPError as exc:
        http_status, failure, error_class, data = exc.code, "api_error", "HTTPError", None
    except Exception as exc:
        failure, error_class, data = "api_error", type(exc).__name__, None
    cost = session.settle(reserve, usage, billed, price, failure)
    tin = cached = tout = None
    if usage:
        tin, cached, tout = usage
    reported = None if data is None else (data.get("model") or requested_model)
    if data is not None:
        outcome = "completed"
    elif session.fallback == "usd_cap" and usage is None:
        outcome = "usd_cap"
    else:
        outcome = "error"
    return data, _meta(
        latency_sec=round(time.perf_counter() - start, 4),
        input_tokens=tin, cached_input_tokens=cached, output_tokens=tout,
        cost_usd=float(cost) if usage else None, model=reported,
        outcome=outcome, error_class=error_class,
        http_status=http_status, response_status=response_status)


def voice_body(llm_system, prompt_content, cfg):
    model, effort, tier, _price, _cap = voice_settings(cfg)
    if not isinstance(llm_system, str) or not isinstance(prompt_content, str):
        raise ValueError("openai_voice_text_invalid")
    return dict(
        model=model, instructions=llm_system, store=False,
        max_output_tokens=_max_output(cfg),
        reasoning=dict(effort=effort), service_tier=tier,
        input=prompt_content)


def _voice_reply_sync(llm_system, prompt_content, cfg):
    body = voice_body(llm_system, prompt_content, cfg)
    data, meta = responses_post(body, cfg)
    if data is None:
        return None, meta
    text = _output_text(data)
    if not text:
        meta = dict(meta, outcome="error", error_class="empty")
        return None, meta
    return text, meta


async def openai_voice_reply(llm_system, prompt_content, cfg):
    """Persona in instructions only. None on error or cap; meta always a dict."""
    import asyncio
    try:
        return await asyncio.to_thread(_voice_reply_sync, llm_system, prompt_content, cfg)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, _meta(error_class=type(exc).__name__)


def http_get(url, key, timeout):
    if "pytest" in sys.modules:
        raise RuntimeError("openai_disabled_under_pytest")
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + key}, method="GET")
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
        return json.loads(response.read())


def list_model_names(cfg):
    """Account model ids whose names match audio|realtime|vision|transcribe|tts."""
    try:
        getter = cfg.get("_voice_api_get") or http_get
        data = getter(MODELS_URL, _key(cfg), _timeout(cfg))
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None, "invalid"
        names = sorted({
            mid for row in rows if isinstance(row, dict)
            for mid in [row.get("id")]
            if isinstance(mid, str) and MODEL_NAME_FILTER.search(mid)
        })
        return names, None
    except Exception as exc:
        return None, type(exc).__name__
