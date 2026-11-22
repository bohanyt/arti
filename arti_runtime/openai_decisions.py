"""Opt-in Responses transport for Stardew decisions only. ADR 151.

No environment mutation, SDK, child credentials or raw errors. Text evidence
uses the existing approved, redacted trial archive.
The inherited scheduler/decoder/fences stay authoritative. Reflection uses Codex.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from arti_runtime import ipc
from arti_stardew_codex import CodexSubscriptionDecisionProvider, INSTRUCTIONS, decision_prompt

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_ENV_FILE = os.environ.get("ARTI_SECRETS_ENV") or ".env"  # config stardew_openai_env_file overrides it
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_EFFORT = "low"
DEFAULT_TIER = "priority"
DEFAULT_CAP_USD = "2.00"
MAX_OUTPUT_TOKENS = 4000  # proven benchmark shape, includes reasoning
ERROR_LIMIT = 2
DEVELOPER = "Use only the current request; never act outside JSON."
# USD / million: (input, cached input, output), STANDARD tier. Editable estimates,
# NOT a verified invoice. Owner's modelbench report lists these third-party rates.
PRICE_SOURCES = (
    "https://www.eesel.ai/blog/gpt-6-luna-pricing",
    "https://pricepertoken.com/pricing-page/model/openai-gpt-6-luna",
    "https://anotherwrapper.com/tools/llm-pricing/gpt-5.6-luna/gpt-5.6-terra",
)
MODEL_PRICES = {"gpt-6-luna": ("0.10", "0.01", "0.50"),
                "gpt-5.6-luna": ("0.20", "0.02", "1.20"),
                "gpt-5.6-terra": ("2.00", "0.20", "12.00")}
TIER_MULTIPLIERS = {"default": "1", "priority": "2", "fast": "2"}
_API_PREPARED = object()
# ADR 229: the goal ladder's planning schema on the API lane. Strict structured output
# enforces structure, types, enums and required keys; these bound keywords are not
# all accepted there, so length/count bounds stay with the ladder's per-field check.
PLANNING_SCHEMA_NAME = "GoalPlanning_v1"
_UNBOUNDED = ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum")


def strict_schema(schema):
    """A copy of ``schema`` for strict structured output, without the bound keywords above."""
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in _UNBOUNDED:
            continue
        out[key] = ({name: strict_schema(sub) for name, sub in value.items()}
                    if key == "properties" and isinstance(value, dict) else strict_schema(value))
    return out


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("openai_invalid_price_or_cap")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("openai_invalid_price_or_cap") from None
    if not result.is_finite() or result < 0:
        raise ValueError("openai_invalid_price_or_cap")
    return result


def settings(config):
    model = config.get("stardew_decision_model", DEFAULT_MODEL)
    effort = config.get("stardew_decision_effort", DEFAULT_EFFORT)
    tier = config.get("stardew_decision_service_tier", DEFAULT_TIER)
    if (not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,100}", model)
            or effort not in {"low", "medium", "high"}):
        raise ValueError("openai_model_or_effort_invalid")
    if tier not in {"default", "priority"}:
        raise ValueError("openai_service_tier_invalid")
    prices = config.get("stardew_openai_prices", MODEL_PRICES)
    if not isinstance(prices, dict) or model not in prices:
        raise ValueError("openai_model_price_missing")
    price = prices[model]
    if not isinstance(price, (list, tuple)) or len(price) != 3:
        raise ValueError("openai_model_price_invalid")
    price = tuple(_decimal(v) for v in price)
    if price[0] <= 0 or price[2] <= 0 or price[1] > price[0]:
        raise ValueError("openai_model_price_invalid")
    cap = _decimal(config.get("stardew_openai_session_cap_usd", DEFAULT_CAP_USD))
    return model, effort, tier, price, cap


def _read_key(config):
    # File only. Inherited OPENAI_API_KEY is deliberately ignored.
    path = Path(config.get("stardew_openai_env_file", DEFAULT_ENV_FILE))
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ValueError("openai_env_file_missing") from None
    except (OSError, UnicodeError):
        raise ValueError("openai_env_file_unreadable") from None
    for line in contents.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == "OPENAI_API_KEY":
            key = value.strip().strip('"').strip("'")
            if key and not any(c.isspace() for c in key):
                return key
    raise ValueError("openai_env_file_key_missing")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # bearer credentials must never follow another origin


def http_call(body, key, timeout):
    if "pytest" in sys.modules:
        raise RuntimeError("openai_disabled_under_pytest")
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    # No HTTP error body/message is read: either may contain credentials or input.
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
        return json.loads(response.read())


class OpenAIDecisionProvider(CodexSubscriptionDecisionProvider):
    def __init__(self, policy, *, config, http_client=None, **kwargs):
        self._api_model, self._api_effort, self._api_tier, self._api_price, self._api_cap = settings(config)
        self._api_key = _read_key(config)
        # Existing reviewed subscription settings; never pass an arbitrary API
        # model/tier into the Codex fallback or the bedtime reflection lane.
        fallback = dict(config, stardew_decision_model="gpt-6-luna",
                        stardew_decision_effort="medium", stardew_decision_service_tier="priority")
        super().__init__(policy, config=fallback, **kwargs)
        self.transport_provenance = "injected" if http_client else "configured_external_transport"
        self._http = http_client or http_call
        self._api_lock = threading.RLock()
        self._health_lock = threading.Lock()
        self._journal_sink = None
        self._pending_rows = []
        self._api_spent = Decimal(0)
        self._api_reserved = Decimal(0)
        self._api_uncertain = Decimal(0)
        self._api_errors = 0
        self._api_fallback = None
        self._health_checked = False
        self.last_planning_lane = None  # ADR 229: the lane the last goal-ladder planning call used
        state = config.get("stardew_openai_session_state_file")
        self._api_state_path = Path(state) if state else None
        self._state_writes = 0
        self._state_leftovers = set()  # our .tmp records not yet folded into the .json
        self._state_failed_writes = 0  # >0 while the .json is behind memory (one FAULT)
        self._load_state()
        self._committed_cost_usd = self._api_spent
        self._reserved_cost_usd = self._api_reserved + self._api_uncertain

    @property
    def quota_kind(self):
        # Preserve the subscription scheduler's count/headroom. Dollar admission
        # lives below and settles API usage, not the legacy Groq fixed reserve.
        return "codex_subscription"

    @property
    def running_usd(self):
        with self._api_lock:
            return float(self._api_spent)

    def set_journal_sink(self, sink):
        with self._api_lock:
            self._journal_sink = sink
            for row in self._pending_rows:
                sink(row)
            self._pending_rows.clear()

    def _emit(self, row):
        # Caller owns _api_lock. All fields are local metadata or validated counts.
        row = dict(row, at=time.time())
        if self._journal_sink:
            self._journal_sink(row)
        else:
            self._pending_rows.append(row)

    def _state_records(self):
        """The .json plus every leftover .tmp record (ADR 201). A torn .tmp is ignored:
        no call was admitted on a write that did not finish."""
        path = self._api_state_path
        records, leftovers = [], []
        if path.exists():
            records.append(json.loads(path.read_text(encoding="utf-8")))  # invalid .json raises
        prefix = path.stem + "."
        try:
            names = [e.name for e in os.scandir(path.parent)
                     if e.name.endswith(".tmp") and e.name.startswith(prefix)]
        except OSError:
            names = []
        for name in sorted(names):
            leftover = path.with_name(name)
            leftovers.append(leftover)
            try:
                records.append(json.loads(leftover.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return records, leftovers

    def _load_state(self):
        if not self._api_state_path:
            return
        try:
            records, leftovers = self._state_records()
            if not records:
                return
            spent = total = reserved_any = Decimal(0)
            errors, fallback = 0, False
            for state in records:
                s_spent, s_reserved = _decimal(state["spent_usd"]), _decimal(state["reserved_usd"])
                spent = max(spent, s_spent)
                total = max(total, s_spent + s_reserved + _decimal(state["uncertain_usd"]))
                reserved_any = max(reserved_any, s_reserved)
                errors = max(errors, int(state["errors"]))
                fallback = fallback or bool(state.get("fallback"))
        except Exception:
            self._fallback("session_state_invalid")
            return
        # A reserve or a newer .tmp from a dead process is settled as uncertain
        # spend: it counts against the cap but does not end the lane by itself.
        self._api_spent, self._api_uncertain, self._api_errors = spent, total - spent, errors
        self._state_leftovers.update(leftovers)
        # Reasons are our enum, never echo arbitrary file text.
        if fallback:
            self._api_fallback = "previous_session_fallback"
        elif reserved_any or leftovers:
            with self._api_lock:
                self._emit(dict(ev="decision_state_settled", provider="openai_api",
                    settled_reserve_usd=float(reserved_any), leftover_records=len(leftovers),
                    running_usd=float(self._api_spent), uncertain_usd=float(self._api_uncertain)))
                if self._api_spent + self._api_uncertain >= self._api_cap:
                    self._fallback("usd_cap")
                else:
                    self._save_state()

    def _write_state(self):
        """One record: a fresh .tmp, then the IPC contract's POSIX replace with its
        bounded retries (ADR 201). Returns (persisted, recorded, OSError). recorded
        means a full record is on disk: the .json, or a .tmp the next start folds in."""
        path = self._api_state_path
        body = json.dumps(dict(spent_usd=str(self._api_spent),
            reserved_usd=str(self._api_reserved), uncertain_usd=str(self._api_uncertain),
            errors=self._api_errors, fallback=bool(self._api_fallback)))
        # A unique name per write: no other writer or scanner can hold it yet.
        self._state_writes += 1
        temp = path.with_name(f"{path.stem}.{os.getpid()}-{self._state_writes}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            ipc.retry(lambda: temp.write_text(body, encoding="utf-8"))
        except OSError as exc:
            self._state_leftovers.add(temp)  # torn or absent; removed after a good write
            return False, False, exc
        try:
            ipc.replace(temp, path)  # succeeds past delete-sharing readers and scanners
            return True, True, None
        except OSError as exc:
            self._state_leftovers.add(temp)
            return False, True, exc

    def _save_state(self):
        """Persist the in-memory accounting. Caller owns _api_lock. Returns False only
        when no full record could be written anywhere; a failed replace alone keeps
        the lane, raises one FAULT per outage and retries at the next write (ADR 201)."""
        # Existing Live7 reports read these inherited counters. API usage settles
        # them here; subscription scheduling never adds the legacy fixed reserve.
        self._committed_cost_usd = self._api_spent
        self._reserved_cost_usd = self._api_reserved + self._api_uncertain
        if not self._api_state_path:
            return True
        persisted, recorded, error = self._write_state()
        if persisted:
            if self._state_failed_writes:
                self._emit(dict(ev="decision_state_write_recovered", provider="openai_api",
                    failed_writes=self._state_failed_writes, running_usd=float(self._api_spent)))
                self._state_failed_writes = 0
            for leftover in list(self._state_leftovers):
                try:
                    leftover.unlink(missing_ok=True)
                    self._state_leftovers.discard(leftover)
                except OSError:
                    pass
            return True
        if not self._state_failed_writes:
            # OS error class and numbers only, never a path or message text.
            self._emit(dict(ev="FAULT", type="decision_state_unwritable", provider="openai_api",
                cause="replace_failed" if recorded else "write_failed",
                os_error=type(error).__name__, errno=getattr(error, "errno", None),
                winerror=getattr(error, "winerror", None),
                running_usd=float(self._api_spent), uncertain_usd=float(self._api_uncertain)))
        self._state_failed_writes += 1
        return recorded

    def _fallback(self, reason, *, persist=True):
        with self._api_lock:
            if self._api_fallback is None:
                self._api_fallback = reason
                self._emit(dict(ev="decision_provider_fallback", provider="openai_api",
                    fallback_provider="codex_subscription", reason=reason, running_usd=float(self._api_spent)))
                if persist:
                    self._save_state()

    def _body(self, prompt, *, probe=False):
        return dict(model=self._api_model, instructions="Reply OK." if probe else INSTRUCTIONS, store=False,
            max_output_tokens=64 if probe else MAX_OUTPUT_TOKENS,
            reasoning=dict(effort=self._api_effort), service_tier=self._api_tier,
            input=[dict(role="developer", content=DEVELOPER), dict(role="user", content=prompt)])

    def complete_planning(self, messages, schema, *, timeout_sec: float = 120.0) -> str:
        """ADR 229: the goal ladder's planning call on this session's decision lane.

        The same API model, effort, tier, per-session USD cap, state file,
        ``decision_provider_call`` row (``purpose: planning``) and trial archive
        as the decision calls, with the planning schema in ``text.format``.
        While this lane is in fallback (cap, timeout, repeated errors) the
        session's decisions run on the subscription lane, and so does planning:
        the existing reflection lane, unchanged. A planning failure is the
        planning call's own: it never moves the decision lane by itself.
        """
        with self._api_lock:
            on_api = self._api_fallback is None
        if not on_api:
            self.last_planning_lane = "codex_subscription"
            return self.complete_reflection(messages, schema, timeout_sec=timeout_sec)
        with self._lock:
            if self._closed:
                raise RuntimeError("provider_closed")
            now = self._clock()
            self._request_times = [t for t in self._request_times if now - t < 3600]
            if len(self._request_times) >= 239:
                raise RuntimeError("provider_hourly_limit")
            if self.physical_start_guard is not None:
                self.physical_start_guard("provider")
            self._physical_requests += 1
            index = self._physical_requests
            self._request_times.append(now)
        token = "goal-planning-%d" % time.monotonic_ns()
        archive = (self._trial_provider_archive.step(token, "reflection", index)
                   if self.trial_owner_authorized is True and self._trial_provider_archive is not None else None)
        system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        user = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") != "system")
        body = dict(model=self._api_model, instructions=system, store=False, max_output_tokens=MAX_OUTPUT_TOKENS,
            reasoning=dict(effort=self._api_effort), service_tier=self._api_tier,
            input=[dict(role="developer", content=DEVELOPER), dict(role="user", content=user)],
            text=dict(format=dict(type="json_schema", name=PLANNING_SCHEMA_NAME,
                                  schema=strict_schema(schema), strict=True)))
        self.last_planning_lane = "openai_api"
        text = self._call(user, archive=archive, body=body, purpose="planning", timeout=timeout_sec)
        if text is None:  # the cap (or an unwritable state) moved this session to the subscription lane
            self.last_planning_lane = "codex_subscription"
            return self.complete_reflection(messages, schema, timeout_sec=timeout_sec)
        return text

    def _call(self, prompt, *, probe=False, archive=None, body=None, purpose=None, timeout=None):
        body = self._body(prompt, probe=probe) if body is None else body
        multiplier = Decimal(TIER_MULTIPLIERS[self._api_tier])
        pin, pcache, pout = self._api_price
        # UTF-8 byte count upper-bounds text tokens; overhead covers role/framing.
        reserve = ((len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 1024) * pin
                   + body["max_output_tokens"] * pout) * multiplier / 1_000_000
        with self._api_lock:
            if self._api_fallback:
                return None
            if self._api_spent + self._api_reserved + self._api_uncertain + reserve > self._api_cap:
                self._fallback("usd_cap")
                return None
            self._api_reserved += reserve
            if not self._save_state():
                # This reserve is on no disk record, so a later process could not
                # count the call against the cap: it is not sent (ADR 201).
                self._api_reserved -= reserve
                self._fallback("session_state_unwritable", persist=False)
                return None
        start = time.perf_counter()
        usage, text, billed, failure, response_status = None, None, None, None, None
        http_status = None
        try:
            if archive is not None:
                archive("request", dict(transport="openai_api", prompt=prompt,
                                        base_instructions=body["instructions"], developer_instructions=DEVELOPER))
            data = self._http(body, self._api_key, self._logical_timeout_sec if timeout is None else timeout)
            if archive is not None:
                # Text only, before validation/strip; never headers, credentials,
                # reasoning or an HTTP error body. Failed decisions are evidence too.
                archive("response", dict(raw_response="".join(
                    c.get("text", "") for o in data.get("output", []) if o.get("type") == "message"
                    for c in o.get("content", []) if c.get("type") == "output_text")))
            response_status = data.get("status")
            if response_status not in {"completed", "incomplete", "failed", "cancelled"}:
                response_status = "unknown"
            u = data["usage"]
            counts = (u["input_tokens"], (u.get("input_tokens_details") or {}).get("cached_tokens", 0),
                      u["output_tokens"], (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0))
            if any(type(n) is not int or n < 0 for n in counts) or counts[1] > counts[0] or counts[3] > counts[2]:
                raise ValueError()
            usage = counts
            billed = data.get("service_tier")
            if billed not in TIER_MULTIPLIERS:
                billed = self._api_tier
            text = "".join(c.get("text", "") for o in data.get("output", []) if o.get("type") == "message"
                           for c in o.get("content", []) if c.get("type") == "output_text").strip()
            # A tiny probe may exhaust its output allowance before emitting text.
            # Accepted inference + usage proves auth/model/tier health; it is not
            # a GameDecision and must never be fed into the decision decoder.
            if (probe and response_status not in {"completed", "incomplete"}
                    or not probe and (response_status != "completed" or not text)):
                raise ValueError()
        except urllib.error.HTTPError as exc:
            http_status = exc.code
            failure = "api_error"
        except (TimeoutError, urllib.error.URLError) as exc:
            failure = "timeout" if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError) else "api_error"
        except Exception:
            failure = "api_error"
        with self._api_lock:
            self._api_reserved -= reserve
            if usage:
                tin, cached, tout, reasoning = usage
                self._api_spent += ((tin - cached) * pin + cached * pcache + tout * pout) * Decimal(TIER_MULTIPLIERS[billed]) / 1_000_000
            else:
                # Unknown billing is retained conservatively; never treated as free.
                self._api_uncertain += reserve
                tin = cached = tout = reasoning = None
            if purpose is None:  # ADR 229: a planning failure never counts toward the decision lane's error limit
                self._api_errors = self._api_errors + 1 if failure else 0
            self._emit(dict(ev="decision_provider_call", provider="openai_api", model=self._api_model,
                effort=self._api_effort, tier=self._api_tier, billed_tier=billed,
                latency_ms=round((time.perf_counter() - start) * 1000, 3), probe=probe,
                input_tokens=tin, cached_input_tokens=cached, output_tokens=tout, reasoning_tokens=reasoning,
                running_usd=float(self._api_spent), uncertain_usd=float(self._api_uncertain),
                response_status=response_status, http_status=http_status, outcome=failure or "completed",
                **({"purpose": purpose} if purpose else {})))
            if purpose is not None:
                if self._api_spent + self._api_uncertain >= self._api_cap:
                    self._fallback("usd_cap")  # the cap is shared; timeouts/errors stay the planning call's own
            elif failure and probe:
                self._fallback("startup_probe_failed")
            elif failure == "timeout":
                self._fallback("timeout")
            elif self._api_errors >= ERROR_LIMIT:
                self._fallback("repeated_api_errors")
            elif self._api_spent + self._api_uncertain >= self._api_cap:
                self._fallback("usd_cap")
            self._save_state()
        if failure:
            if failure == "timeout":
                raise TimeoutError("openai_api_timeout") from None
            raise RuntimeError("openai_api_error") from None
        return text

    def _prepare_physical(self, background):
        with self._health_lock:
            if not self._health_checked and not self._api_fallback:
                self._health_checked = True
                if self.physical_start_guard:
                    self.physical_start_guard("provider")
                try:
                    self._call("Reply OK.", probe=True)
                except (RuntimeError, TimeoutError):
                    self._fallback("startup_probe_failed")
        return super()._prepare_physical(background) if self._api_fallback else _API_PREPARED

    def _physical_text(self, messages, extra_payload, background, prepared=None, *, trace_phase=None, archive=None):
        if prepared is _API_PREPARED:
            text = self._call(decision_prompt(messages, extra_payload), archive=archive)
            if text is not None:
                return text
            # The current physical slot is already owned. The runtime's bounded
            # refresh rebuilds a fresh start fence before entering Codex.
            raise RuntimeError("openai_provider_fallback")
        if extra_payload.get("response_format", {}).get("json_schema", {}).get("name") == "MindReflection_v1":
            return super()._physical_text(messages, extra_payload, background, prepared,
                                          trace_phase=trace_phase, archive=archive)
        # Journal fallback decisions too, without changing subscription transport.
        # SDK counters are optional; absence remains unknown, never fabricated zero.
        owner = self
        class Metered:
            def run(self, *args, **kwargs):
                started, usage, outcome = time.perf_counter(), None, "error"
                try:
                    result = prepared.run(*args, **kwargs)
                    usage = getattr(getattr(result, "usage", None), "last", None)
                    outcome = "returned"
                    return result
                finally:
                    def count(name):
                        n = getattr(usage, name, None)
                        return n if type(n) is int and n >= 0 else None
                    with owner._api_lock:
                        owner._emit(dict(ev="decision_provider_call", provider="codex_subscription",
                            model=owner._model, effort=owner._effort, tier=owner._service_tier,
                            latency_ms=round((time.perf_counter() - started) * 1000, 3), probe=False,
                            input_tokens=count("input_tokens"), cached_input_tokens=count("cached_input_tokens"),
                            output_tokens=count("output_tokens"), reasoning_tokens=count("reasoning_output_tokens"),
                            running_usd=float(owner._api_spent), outcome=outcome))
        return super()._physical_text(messages, extra_payload, background, Metered(),
                                     trace_phase=trace_phase, archive=archive)

    def complete(self, request, *, start_fence=None):
        try:
            return super().complete(request, start_fence=start_fence)
        except TimeoutError:
            self._fallback("timeout")
            raise
