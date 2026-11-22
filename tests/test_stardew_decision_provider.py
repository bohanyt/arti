"""Issue #170: production Stardew decision provider, no-network only."""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

import arti_api_telemetry
import arti_stardew_runtime as stardew_runtime
from arti_stardew_runtime import (
    STARDEW_DECISION_ENDPOINT,
    STARDEW_DECISION_MODEL,
    STARDEW_DECISION_PROFILE,
    STARDEW_DECISION_PROVIDER_KIND,
    ProductionDecisionProvider,
    construct_if_enabled,
    construct_stardew_decision_provider,
    stardew_decision_provider_admission,
)

pytestmark = pytest.mark.timeout(30)


def test_aprime_foreground_completes_while_background_http_is_blocked(tmp_path):
    from dataclasses import replace
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def chat(*args, **kwargs):
        reason = json.loads(args[3][1]["content"])["reason"]
        calls.append(reason)
        if reason.startswith("continuation:"):
            entered.set()
            assert release.wait(5)
        return "synthetic", 0

    policy, _ = stardew_decision_provider_admission(BASE)
    provider = ProductionDecisionProvider(policy, session=object(), text_chat_fn=chat,
                                          continuation_enabled=True)
    rt = stardew_runtime.StardewGoalRuntime(config=BASE, journal_dir=tmp_path,
                                           start_listener=False, start_worker=False)
    req = rt._begin_generation(text="goal", trigger_type="ptt", viewer_name=None,
                               reason="owner_turn", replace_goal=False)
    bg = replace(req, reason="continuation:inspect", generation_id="bg",
                 superseded=threading.Event(), wakeup=threading.Event())
    def background():
        try:
            provider.complete(bg, start_fence=lambda start: start())
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=background)
    worker.start()
    try:
        assert entered.wait(2)
        assert provider.complete(req) == "synthetic"
        assert not release.is_set()
        assert provider.lane_owners == {"foreground": None, "background": "bg"}
        assert provider.physical_requests == 2
        assert provider.committed_cost_usd == policy.worst_case_cost_usd
        assert provider.reserved_cost_usd == policy.worst_case_cost_usd
    finally:
        release.set()
        worker.join(3)
    assert not errors and not worker.is_alive()
    assert calls == ["continuation:inspect", "owner_turn"]
    assert provider.lane_owners == {"foreground": None, "background": None}
    assert provider.committed_cost_usd == 2 * policy.worst_case_cost_usd
    assert provider.reserved_cost_usd == 0


def _aprime_request(tmp_path, reason="owner_turn", generation="fg"):
    from dataclasses import replace
    rt = stardew_runtime.StardewGoalRuntime(config=BASE, journal_dir=tmp_path,
                                           start_listener=False, start_worker=False)
    req = rt._begin_generation(text="goal", trigger_type="ptt", viewer_name=None,
                               reason="owner_turn", replace_goal=False)
    return replace(req, reason=reason, generation_id=generation)


@pytest.mark.parametrize("reason", ["continuation:inspect", "continuation:act", "continuation:finish"])
def test_aprime_foreground_cost_floor_and_reason_admission(tmp_path, reason):
    from dataclasses import replace
    policy, _ = stardew_decision_provider_admission(BASE)
    policy = replace(policy, budget_usd=policy.worst_case_cost_usd)
    calls = []
    provider = ProductionDecisionProvider(policy, session=object(), continuation_enabled=True,
        text_chat_fn=lambda *a, **k: (calls.append(True) or "ok", 0))
    bg = _aprime_request(tmp_path, reason, "bg")
    with pytest.raises(RuntimeError, match="budget_exhausted"):
        provider.complete(bg, start_fence=lambda start: start())
    assert provider.physical_requests == 0 and calls == []
    assert provider.complete(_aprime_request(tmp_path)) == "ok"
    assert provider.physical_requests == 1 and provider.reserved_cost_usd == 0


@pytest.mark.parametrize("reason,enabled,fenced", [
    ("game:state", True, True), ("continuation:unknown", True, True),
    ("continuation:inspect", False, True), ("continuation:inspect", True, False)])
def test_aprime_unadmitted_reasons_never_start(tmp_path, reason, enabled, fenced):
    policy, _ = stardew_decision_provider_admission(BASE)
    provider = ProductionDecisionProvider(policy, session=object(), continuation_enabled=enabled,
        text_chat_fn=lambda *a, **k: pytest.fail("unadmitted physical call"))
    with pytest.raises(RuntimeError, match="autonomous_inference_off"):
        provider.complete(_aprime_request(tmp_path, reason),
                          start_fence=(lambda start: start()) if fenced else None)
    assert not provider.autonomous_event_inference_admitted
    assert provider.physical_requests == 0


@pytest.mark.parametrize("first_lane", ["foreground", "background"])
def test_aprime_lane_exclusion_and_generation_keyed_out_of_order_release(tmp_path, first_lane):
    policy, _ = stardew_decision_provider_admission(BASE)
    entered = {lane: threading.Event() for lane in ("foreground", "background")}
    release = {lane: threading.Event() for lane in entered}
    terminal = {lane: threading.Event() for lane in entered}
    errors = []
    def chat(*a, **k):
        lane = "foreground" if json.loads(a[3][1]["content"])["reason"] == "owner_turn" else "background"
        entered[lane].set()
        assert release[lane].wait(5)
        return "ok", 0
    provider = ProductionDecisionProvider(policy, session=object(), continuation_enabled=True, text_chat_fn=chat)
    def call(lane):
        try:
            provider.complete(_aprime_request(tmp_path, "owner_turn" if lane == "foreground"
                else "continuation:inspect", lane), start_fence=lambda start: start())
        except Exception as exc:
            errors.append(exc)
        finally:
            terminal[lane].set()
    bg = threading.Thread(target=call, args=("background",))
    fg = threading.Thread(target=call, args=("foreground",))
    bg.start()
    try:
        assert entered["background"].wait(2)
        fg.start()
        assert entered["foreground"].wait(2)
        assert provider.lane_owners == {"foreground": "foreground", "background": "background"}
        # Even a repeated generation id cannot release the already-owned lane.
        with pytest.raises(RuntimeError, match="physical_inflight"):
            provider.complete(_aprime_request(tmp_path, "continuation:act", "background"),
                              start_fence=lambda start: start())
        assert provider.physical_requests == 2
        release[first_lane].set()
        assert terminal[first_lane].wait(2)
        other = "foreground" if first_lane == "background" else "background"
        assert provider.lane_owners == {first_lane: None, other: other}
        assert provider.reserved_cost_usd == policy.worst_case_cost_usd
        if first_lane == "background":
            with pytest.raises(RuntimeError, match="physical_inflight"):
                provider.complete(_aprime_request(tmp_path, "continuation:finish", "bg-new"),
                                  start_fence=lambda start: start())
            assert provider.lane_owners["foreground"] == "foreground"
    finally:
        for event in release.values():
            event.set()
        bg.join(3)
        if fg.ident is not None:
            fg.join(3)
    assert not errors and provider.physical_requests == 2
    assert provider.reserved_cost_usd == 0
    assert provider.committed_cost_usd == 2 * policy.worst_case_cost_usd


def test_aprime_shared_hourly_limit_preserves_one_owner_slot(tmp_path):
    from dataclasses import replace
    now = [1000.0]
    policy, _ = stardew_decision_provider_admission(BASE)
    policy = replace(policy, budget_usd=policy.worst_case_cost_usd * 300)
    provider = ProductionDecisionProvider(policy, session=object(), continuation_enabled=True,
        clock=lambda: now[0], text_chat_fn=lambda *a, **k: ("ok", 0))
    base = _aprime_request(tmp_path)
    def req(i, reason="owner_turn"):
        return replace(base, generation_id=str(i), reason=reason,
                       superseded=threading.Event(), wakeup=threading.Event())
    for i in range(239):
        provider.complete(req(i, "continuation:inspect"), start_fence=lambda start: start())
    with pytest.raises(RuntimeError, match="hourly_limit"):
        provider.complete(req(239, "continuation:inspect"), start_fence=lambda start: start())
    assert provider.complete(req(240)) == "ok"
    with pytest.raises(RuntimeError, match="hourly_limit"):
        provider.complete(req(241))
    assert provider.physical_requests == 240
    now[0] += 3600
    assert provider.complete(req(242)) == "ok"
    assert provider.committed_cost_usd == 241 * policy.worst_case_cost_usd


def test_aprime_config_constructs_separate_transports_under_one_provider_policy():
    made = []
    def factory():
        made.append(object())
        return made[-1]
    provider = construct_stardew_decision_provider({**BASE, "stardew_continuation_enabled": True},
        session_factory=factory, text_chat_fn=lambda *a, **k: pytest.fail("construction called HTTP"))
    assert len(made) == 2
    assert provider._session is made[0] and provider._background_session is made[1]
    assert provider._session is not provider._background_session
    assert provider.physical_requests == 0 and provider.reserved_cost_usd == 0


BASE = {
    "stardew_runtime_enabled": True,
    "stardew_provider_enabled": True,
    "stardew_spend_enabled": True,
    "stardew_decision_profile": STARDEW_DECISION_PROFILE,
    "stardew_decision_provider_kind": STARDEW_DECISION_PROVIDER_KIND,
    "stardew_decision_endpoint": STARDEW_DECISION_ENDPOINT,
    "stardew_decision_model": STARDEW_DECISION_MODEL,
    "groq_api_key": "synthetic-test-key",
    # Synthetic policy values only. They are deliberately not market prices.
    "stardew_decision_budget_usd": "1.00",
    "stardew_decision_input_usd_per_million": "1.25",
    "stardew_decision_output_usd_per_million": "2.50",
    "stardew_decision_profile_free": False,
    "stardew_receiver_port": 0,
    "owner_yt_handles": ["@bohanyt"],
}


class _Response:
    def __init__(self, status_code: int, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *, content=None, status=200, entered=None, release=None) -> None:
        self.calls = []
        self.content = content
        self.status = status
        self.entered = entered
        self.release = release

    def post(self, url, *, headers, json: dict, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(3), "synthetic HTTP boundary was not released"
        if self.status != 200:
            return _Response(self.status, text="synthetic provider error")
        if callable(self.content):
            content = self.content(json)
        elif self.content is not None:
            content = self.content
        else:
            request = json_module_loads(json["messages"][1]["content"])
            echo = request["echo"]
            content = json_module_dumps(
                {
                    "v": 1,
                    "request_nonce": echo["request_nonce"],
                    "goal_id": echo["goal_id"],
                    "goal_revision": echo["goal_revision"],
                    "context_ref": echo["context_ref"],
                    "speech": "Provider kebun siap.",
                    "choice": {"kind": "speak"},
                }
            )
        return _Response(200, {"choices": [{"message": {"content": content}}]})


def json_module_loads(value: str):
    return json.loads(value)


def json_module_dumps(value: dict):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fake_text_chat(calls):
    """Dependency-free fake of the injected text_chat boundary, including HTTP shape."""
    def _call(url, api_key, model, messages, **kwargs):
        calls.append(
            {
                "args": (url, api_key, model, messages),
                "kwargs": dict(kwargs),
            }
        )
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", 300),
            "temperature": kwargs.get("temperature", 0.2),
        }
        payload.update(kwargs.get("extra_payload") or {})
        session = kwargs["session"]
        response = session.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=kwargs.get("timeout", 60.0),
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:400]}")
        body = response.json()
        text = str(body["choices"][0]["message"]["content"]).strip()
        arti_api_telemetry.record_openai_response(
            subsystem=kwargs.get("telemetry_subsystem", "scouter"),
            provider="openai_compat",
            model=model,
            body=body,
            latency_ms=0,
            config=None,
        )
        return text, 0

    return _call


def _runtime(tmp_path: Path, cfg: dict, session: FakeSession, *, logical_timeout=None):
    return construct_if_enabled(
        cfg,
        provider_session=session,
        provider_text_chat_fn=_fake_text_chat([]),
        provider_logical_timeout_sec=logical_timeout,
        journal_dir=tmp_path,
        start_listener=False,
        start_worker=False,
    )


def _encoded_provider_request(provider: ProductionDecisionProvider, request) -> bytes:
    messages = provider._render_messages(request)
    extra_payload = stardew_runtime._decision_provider_extra_payload()
    return stardew_runtime._encoded_provider_request(
        provider.policy.model,
        messages,
        extra_payload,
    )


def test_shipped_off_or_missing_gate_constructs_no_provider_or_session():
    made = []

    def session_factory():
        made.append(True)
        raise AssertionError("session must not be constructed before all gates pass")

    off = {**BASE, "stardew_runtime_enabled": False}
    policy, reason = stardew_decision_provider_admission(off)
    assert policy is None and reason == "runtime_disabled"
    assert construct_stardew_decision_provider(off, session_factory=session_factory) is None
    assert made == []


@pytest.mark.parametrize("wiki_gate", [None, False, "true", True])
def test_wiki_transport_requires_explicit_boolean_gate_and_no_startup_io(tmp_path, monkeypatch, wiki_gate):
    import arti_stardew_wiki_http
    made = []
    class Transport:
        def __init__(self): made.append(True)
        def __call__(self, url): pytest.fail("startup must not read wiki")
    monkeypatch.setattr(arti_stardew_wiki_http, "WikiHTTPFetcher", Transport)
    session = FakeSession()
    rt = _runtime(tmp_path, {**BASE, "stardew_wiki_enabled": wiki_gate}, session)
    assert (rt._wiki_fetcher is not None) == (wiki_gate is True)
    assert made == ([True] if wiki_gate is True else []) and session.calls == []
    rt.stop()


@pytest.mark.parametrize("gate", ["stardew_runtime_enabled", "stardew_provider_enabled", "stardew_spend_enabled"])
def test_wiki_gate_cannot_bypass_existing_provider_gates(tmp_path, monkeypatch, gate):
    import arti_stardew_wiki_http
    monkeypatch.setattr(arti_stardew_wiki_http, "WikiHTTPFetcher", lambda: pytest.fail("bypassed gate"))
    session = FakeSession()
    rt = _runtime(tmp_path, {**BASE, "stardew_wiki_enabled": True, gate: False}, session)
    assert rt is None or rt._wiki_fetcher is None
    assert session.calls == []
    if rt is not None:
        rt.stop()


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("stardew_provider_enabled", False, "provider_disabled"),
        ("stardew_spend_enabled", False, "external_inference_disabled"),
        ("stardew_decision_profile", "other", "profile_not_approved"),
        ("stardew_decision_provider_kind", "other", "provider_kind_not_approved"),
        ("stardew_decision_endpoint", "https://example.invalid", "endpoint_not_approved"),
        ("stardew_decision_model", "other/model", "model_not_approved"),
        ("groq_api_key", "", "credential_missing"),
        ("stardew_decision_budget_usd", "0", "budget_missing"),
        ("stardew_decision_input_usd_per_million", None, "pricing_missing"),
        ("stardew_decision_output_usd_per_million", None, "pricing_missing"),
    ],
)
def test_each_required_gate_fails_closed_before_http(key, value, reason):
    cfg = {**BASE, key: value}
    policy, got = stardew_decision_provider_admission(cfg)
    assert policy is None and got == reason
    session = FakeSession()
    assert construct_stardew_decision_provider(cfg, session=session) is None
    assert session.calls == []


def test_unknown_unpriced_paid_profile_is_not_treated_as_free():
    cfg = {
        **BASE,
        "stardew_decision_input_usd_per_million": None,
        "stardew_decision_output_usd_per_million": None,
        "stardew_decision_profile_free": False,
    }
    policy, reason = stardew_decision_provider_admission(cfg)
    assert policy is None and reason == "pricing_missing"
    free = {**cfg, "stardew_decision_profile_free": True, "stardew_decision_budget_usd": 0}
    policy, reason = stardew_decision_provider_admission(free)
    assert reason == "ok" and policy is not None and policy.worst_case_cost_usd == Decimal("0")


def test_policy_repr_suppresses_api_credential():
    policy, reason = stardew_decision_provider_admission(BASE)
    assert reason == "ok" and policy is not None
    rendered = repr(policy)
    assert BASE["groq_api_key"] not in rendered
    assert "api_key=" not in rendered


@pytest.mark.parametrize('play', [False, True])
def test_provider_inspect_contract_distinguishes_domain_from_question(play):
    choices = stardew_runtime._game_decision_schema(play)['properties']['choice']['oneOf']
    state = [c for c in choices if c['properties']['kind']['enum'] == ['inspect']
             and 'state' in c['properties']['source']['enum']]
    assert len(state) == 1
    assert state[0]['properties']['source']['enum'] == ['state', 'text']
    scope = state[0]['properties']['scope']
    assert scope['type'] == ['string', 'null']  # text retains dynamic family scopes
    assert 'State: null/state or exact observation domain' in scope['description']
    assert 'natural language goes in question' in scope['description']


def test_valid_config_one_owner_turn_one_request_strict_profile_and_schema(tmp_path, monkeypatch):
    telemetry = []
    monkeypatch.setattr(
        arti_api_telemetry,
        "record_openai_response",
        lambda **kwargs: telemetry.append(kwargs),
    )
    http = FakeSession()
    text_calls = []
    rt = construct_if_enabled(
        BASE,
        provider_session=http,
        provider_text_chat_fn=_fake_text_chat(text_calls),
        journal_dir=tmp_path,
        start_listener=False,
        start_worker=False,
    )
    assert rt is not None
    assert isinstance(rt.decision_provider, ProductionDecisionProvider)
    result = rt.decide_owner_turn(text="lihat kebun", trigger_type="ptt", viewer_name=None)
    assert result.admitted and result.applied and result.reason == "speak"
    assert result.speech == "Provider kebun siap."
    assert len(http.calls) == 1 and len(text_calls) == 1
    call = http.calls[0]
    payload = call["json"]
    assert call["url"] == STARDEW_DECISION_ENDPOINT
    assert call["timeout"] == (3.0, 12.0)
    assert payload["model"] == STARDEW_DECISION_MODEL
    assert payload["max_tokens"] == 2048
    assert payload["temperature"] == 0.2
    assert payload["reasoning_effort"] == "low"
    assert payload["stream"] is False
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "GameDecision_v1"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "v", "request_nonce", "goal_id", "goal_revision", "context_ref", "speech", "choice"
    }
    assert text_calls[0]["kwargs"]["telemetry_subsystem"] == "stardew_decision"
    assert telemetry and telemetry[-1]["subsystem"] == "stardew_decision"
    rendered = json.loads(payload["messages"][1]["content"])
    assert rendered["contract"] == "GameDecision.v1"
    assert rendered["echo"]["request_nonce"]
    assert rendered["owner_input"] == "lihat kebun"
    assert rt.inbox_ops == 0


def test_production_owner_turn_only_native_events_cause_zero_additional_provider_calls(tmp_path):
    http = FakeSession()
    text_calls = []
    rt = construct_if_enabled(
        BASE,
        provider_session=http,
        provider_text_chat_fn=_fake_text_chat(text_calls),
        journal_dir=tmp_path,
        start_listener=False,
        start_worker=True,
    )
    assert rt is not None
    provider = rt.decision_provider
    assert isinstance(provider, ProductionDecisionProvider)
    try:
        first = rt.decide_owner_turn(text="rawat kebun", trigger_type="ptt", viewer_name=None)
        assert first.applied and first.reason == "speak"
        assert rt.active_goal is not None
        assert rt.active_goal.progress_marker != "terminal"
        before_physical = provider.physical_requests
        before_http = len(http.calls)
        before_text = len(text_calls)

        for index in range(64):
            rt._on_native_event({"ev": f"native-test-{index}"})

        # Exercise both the worker wakeup path and a direct drain attempt. Production
        # remains owner-turn-only until a separately admitted quota/isolation gate exists.
        time.sleep(0.25)
        assert rt.process_pending_events() == 0
        assert provider.physical_requests == before_physical
        assert len(http.calls) == before_http
        assert len(text_calls) == before_text
        assert rt.inbox_ops == 0
    finally:
        rt.stop()


def test_max_size_request_cost_bound_fails_closed_before_http(tmp_path):
    http = FakeSession()
    rt = _runtime(tmp_path, BASE, http)
    assert rt is not None
    provider = rt.decision_provider
    assert isinstance(provider, ProductionDecisionProvider)

    # Find the largest owner request whose deterministic full JSON request body
    # (messages + strict response schema + request-shaping fields) fits the approved
    # 32 KiB request envelope. UUID/context lengths are fixed, so binary search is stable.
    best = None
    lo, hi = 0, 40_000
    while lo <= hi:
        size = (lo + hi) // 2
        with rt._lock:
            request = rt._begin_generation(
                text="x" * size,
                trigger_type="ptt",
                viewer_name=None,
                reason="owner_turn",
                replace_goal=True,
            )
        encoded = _encoded_provider_request(provider, request)
        if len(encoded) <= stardew_runtime._STARDEW_DECISION_REQUEST_MAX_BYTES:
            best = (request, encoded, size)
            lo = size + 1
        else:
            hi = size - 1

    assert best is not None
    request, encoded, text_size = best
    assert len(encoded) <= stardew_runtime._STARDEW_DECISION_REQUEST_MAX_BYTES
    assert (
        stardew_runtime._provider_input_token_upper_bound(encoded)
        > stardew_runtime._STARDEW_DECISION_INPUT_TOKEN_RESERVE
    )
    wire = json.loads(encoded.decode("utf-8"))
    assert wire["messages"][1]["content"]
    assert wire["response_format"]["json_schema"]["strict"] is True
    assert wire["response_format"]["json_schema"]["schema"]["additionalProperties"] is False

    with rt._lock:
        next_request = rt._begin_generation(
            text="x" * (text_size + 1),
            trigger_type="ptt",
            viewer_name=None,
            reason="owner_turn",
            replace_goal=True,
        )
    assert len(_encoded_provider_request(provider, next_request)) > (
        stardew_runtime._STARDEW_DECISION_REQUEST_MAX_BYTES
    )

    with pytest.raises(ValueError, match="provider_input_bound_exceeded"):
        provider.complete(request)
    assert http.calls == []
    assert provider.physical_requests == 0
    assert provider.reserved_cost_usd == Decimal("0")
    assert provider.committed_cost_usd == Decimal("0")


@pytest.mark.parametrize(
    "content,status,expected",
    [
        ("not json", 200, "malformed_json"),
        ("x" * (16 * 1024 + 1), 200, "too_large"),
        (None, 500, "provider_error"),
    ],
)
def test_malformed_oversize_and_http_error_are_zero_action(tmp_path, content, status, expected):
    http = FakeSession(content=content, status=status)
    rt = _runtime(tmp_path, BASE, http)
    assert rt is not None
    result = rt.decide_owner_turn(text="jalan", trigger_type="ptt", viewer_name=None)
    assert result.admitted is True
    assert result.applied is False
    assert result.speech == ""
    assert result.reason == expected
    assert rt.inbox_ops == 0
    assert len(http.calls) == 1


def test_production_missing_provider_gate_claims_owner_turn_without_voice_fallback(tmp_path):
    cfg = {**BASE, "stardew_provider_enabled": False}
    rt = construct_if_enabled(
        cfg,
        journal_dir=tmp_path,
        start_listener=False,
        start_worker=False,
    )
    assert rt is not None and rt.decision_provider is None
    result = rt.decide_owner_turn(text="goal kebun", trigger_type="ptt", viewer_name=None)
    assert result.admitted is True
    assert result.skip_reply_tags is True
    assert result.applied is False
    assert result.reason == "provider_off"
    assert rt.active_goal is None


def test_logical_timeout_keeps_physical_slot_and_reservation_until_terminal(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    http = FakeSession(entered=entered, release=release)
    rt = _runtime(tmp_path, BASE, http, logical_timeout=0.03)
    assert rt is not None
    provider = rt.decision_provider
    assert isinstance(provider, ProductionDecisionProvider)

    first = rt.decide_owner_turn(text="pertama", trigger_type="ptt", viewer_name=None)
    assert entered.is_set()
    assert first.reason == "provider_error" and not first.applied
    assert provider.physical_inflight is True
    assert provider.reserved_cost_usd == provider.worst_case_cost_usd
    assert len(http.calls) == 1

    second = rt.decide_owner_turn(text="kedua", trigger_type="ptt", viewer_name=None)
    assert second.reason == "provider_error" and not second.applied
    assert len(http.calls) == 1, "logical timeout must not create a second paid request"
    assert provider.physical_inflight is True
    assert provider.reserved_cost_usd == provider.worst_case_cost_usd

    release.set()
    deadline = time.time() + 2
    while provider.physical_inflight and time.time() < deadline:
        time.sleep(0.005)
    assert provider.physical_inflight is False
    assert provider.reserved_cost_usd == Decimal("0")
    assert provider.committed_cost_usd == provider.worst_case_cost_usd
    assert rt.inbox_ops == 0


def test_budget_reservation_is_deterministic_and_exhaustion_blocks_before_http(tmp_path):
    policy, reason = stardew_decision_provider_admission(BASE)
    assert reason == "ok" and policy is not None
    cfg = {**BASE, "stardew_decision_budget_usd": str(policy.worst_case_cost_usd)}
    http = FakeSession()
    rt = _runtime(tmp_path, cfg, http)
    provider = rt.decision_provider
    first = rt.decide_owner_turn(text="satu", trigger_type="ptt", viewer_name=None)
    assert first.applied and len(http.calls) == 1
    assert provider.committed_cost_usd == provider.worst_case_cost_usd

    second = rt.decide_owner_turn(text="dua", trigger_type="ptt", viewer_name=None)
    assert second.reason == "provider_error" and not second.applied
    assert len(http.calls) == 1
    assert provider.committed_cost_usd == provider.worst_case_cost_usd
    assert rt.inbox_ops == 0
