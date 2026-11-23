"""Search-only Codex lookup, with a fake role client (no provider I/O)."""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import arti_codex_role as cr
import arti_web_lookup as wl
from arti_brain_contract import FlagCancellation
from arti_codex_lookup_policy import LookupToolPolicy

BASE = {"codex_agent_enabled": True, "codex_role": "lookup", "telemetry_enabled": False,
        "web_lookup_codex_enabled": True,
        "web_lookup_provider_chain": ["codex", "groq_compound", "cursor"]}
ANSWER = json.dumps({"answer": "Emas hari ini Rp 2 juta per gram.",
                     "sources": [{"title": "Antam", "url": "https://example.com/gold"}]})


@pytest.fixture
def fake(monkeypatch):
    cr.shutdown()
    cr.reset_breakers()
    state = NS(kinds=["webSearch", "agentMessage"], starts=[], runs=[], clients=[], gate=None)
    class Client:
        closed = False
        lookup_tool_policy = LookupToolPolicy(("web.run",), "a" * 64, ("open", "search_query"))
        def thread_start(self, **kw):
            state.starts.append(kw)
            def run(prompt, **kwargs):
                state.runs.append(kwargs)
                if state.gate:
                    state.gate.wait(3)
                return NS(status="completed", final_response=ANSWER, usage=None,
                          items=[NS(type=k) for k in state.kinds])
            return NS(run=run)
        def close(self):
            self.closed = True
    def factory(**kw):
        c = Client()
        state.clients.append(c)
        return c
    monkeypatch.setattr(cr, "_client_factory", factory)
    yield state
    if state.gate:
        state.gate.set()
    cr.shutdown()
    cr.reset_breakers()


def test_lookup_search_allowed_and_sources(fake):
    r = wl.call_codex_search("harga emas hari ini", BASE)
    assert r.status == "ok" and r.search_performed
    assert r.sources[0].url == "https://example.com/gold"
    assert fake.starts[0]["config"] == {"web_search": "live"}
    assert fake.starts[0]["ephemeral"] is True
    cr.call("p", BASE)
    assert len(fake.starts) == 2
    assert fake.runs[0] == {"model": "gpt-6-luna", "effort": "low"}


def test_live_profile_dispatches_through_the_audited_codex_lane(fake, monkeypatch, tmp_path):
    from scripts.livepath.mabar import mabar_bridge_config

    cfg = mabar_bridge_config(tmp_path / "r")
    for provider in ("groq_compound", "cursor"):
        monkeypatch.setitem(wl._PROVIDERS, provider, lambda *args: pytest.fail("non-Codex search"))
    fact = wl.lookup_block("harga emas hari ini", cfg)
    assert "[INFO INTERNET" in fact and "Emas hari ini" in fact
    start, = fake.starts
    assert start["config"] == {"web_search": "live"} and start["ephemeral"]
    assert fake.clients[0].lookup_tool_policy.tool_names == ("web.run",)

    fake.kinds.append("commandExecution")
    assert wl.lookup_block("harga emas hari ini", cfg) == ""


@pytest.mark.parametrize("role", ["scout", "observer", "catchup", "diary"])
def test_other_roles_still_reject_search(fake, role):
    with pytest.raises(cr.CodexRoleError, match="unexpected tool event"):
        cr.call("p", {**BASE, "codex_role": role})
    assert "config" not in fake.starts[0]


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange", "mcpToolCall",
                                  "dynamicToolCall", "imageGeneration", "unknown", None])
def test_lookup_rejects_other_items(fake, kind):
    fake.kinds.append(kind)
    with pytest.raises(cr.CodexRoleError, match="unexpected tool event"):
        wl.call_codex_search("q", BASE)


def test_unsearched_answer_rejected(fake):
    fake.kinds = ["agentMessage"]
    with pytest.raises(cr.CodexRoleError, match="search_unconfirmed"):
        wl.call_codex_search("q", BASE)


def test_lookup_model_and_effort_are_independent(fake):
    cr.call("p", {**BASE, "web_lookup_codex_model": "custom-luna",
                  "web_lookup_codex_effort": "medium", "codex_role_model": "other"})
    assert fake.runs[0] == {"model": "custom-luna", "effort": "medium"}


def test_lookup_process_policy_never_leaks_to_other_roles(fake):
    assert set(cr.role_overrides("lookup")) == (set(cr.OVERRIDES) - {
        'web_search="disabled"', 'features.standalone_web_search=false'}) | {
        'web_search="live"', 'features.standalone_web_search=true',
        'features.multi_agent_v2=false', 'features.collaboration_modes=false',
        'features.default_mode_request_user_input=false',
        'tools.experimental_request_user_input.enabled=false', 'tools.update_plan.enabled=false'}
    assert cr.role_overrides("scout") == cr.OVERRIDES
    cr.call("p", BASE)
    search_client = fake.clients[-1]
    fake.kinds = ["agentMessage"]
    cr.call("p", {**BASE, "codex_role": "scout"})
    assert fake.clients[-1] is not search_client
    assert "config" not in fake.starts[-1]


def test_lookup_catalog_changes_only_scoped_tool_metadata(tmp_path):
    cache = tmp_path / "cache.json"
    original = {"models": [{"slug": "gpt-6-luna", "tool_mode": "code_mode_only",
                            "use_responses_lite": True, "experimental_supported_tools": ["clock"],
                            "context_window": 12345, "apply_patch_tool_type": "freeform",
                            "multi_agent_version": "v2"}, {"slug": "other"}]}
    cache.write_text(json.dumps(original), encoding="utf-8")
    override = cr._lookup_model_catalog(str(tmp_path), "gpt-6-luna", cache)
    result = json.loads((tmp_path / "lookup-models.json").read_text(encoding="utf-8"))
    entry, = result["models"]
    assert entry == {"slug": "gpt-6-luna", "use_responses_lite": False,
                     "experimental_supported_tools": [], "context_window": 12345,
                     "apply_patch_tool_type": None}
    assert json.loads(cache.read_text(encoding="utf-8")) == original
    assert override.startswith("model_catalog_json=")
    with pytest.raises(cr.CodexRoleError, match="absent"):
        cr._lookup_model_catalog(str(tmp_path), "missing", cache)


def test_timeout_falls_through_to_groq(fake, monkeypatch):
    fake.gate = threading.Event()
    calls = []
    monkeypatch.setitem(wl._PROVIDERS, "groq_compound", lambda q, c: calls.append("groq") or "fallback")
    t0 = time.monotonic()
    assert wl.lookup("q", {**BASE, "web_lookup_codex_timeout_sec": .1}) == ("fallback", "groq_compound")
    assert time.monotonic() - t0 < 1
    assert calls == ["groq"]
    assert cr.breaker_state()["lookup"]["failures"] == 1


def test_chain_order_is_configurable(monkeypatch):
    calls = []
    def fn(name):
        def run(q, c):
            calls.append(name)
            if name != "cursor":
                raise RuntimeError("offline")
            return "ok"
        return run
    for name in BASE["web_lookup_provider_chain"]:
        monkeypatch.setitem(wl._PROVIDERS, name, fn(name))
    assert wl.lookup("q", BASE) == ("ok", "cursor")
    assert calls == BASE["web_lookup_provider_chain"]


def test_registry_uses_config_order():
    from arti_web_backends import build_web_backend_registry
    assert [b.backend_id for b in build_web_backend_registry(BASE)] == ["codex", "groq_compound", "cursor_lookup"]


def test_cancel_stops_lookup_and_no_fallback(fake, monkeypatch):
    fake.gate = threading.Event()
    cancel = FlagCancellation()
    monkeypatch.setitem(wl._PROVIDERS, "groq_compound", lambda *a: pytest.fail("cancelled fallback"))
    timer = threading.Timer(.1, cancel.cancel)
    timer.start()
    try:
        assert wl.lookup("q", {**BASE, "_web_lookup_cancel": cancel})[0] is None
        assert cr._client is None
    finally:
        timer.cancel()


def test_async_backend(fake):
    from arti_web_backends import CodexLookupWebSearchBackend
    result = asyncio.run(CodexLookupWebSearchBackend(config=BASE).search(
        "q", deadline=time.monotonic() + 1, cancel=FlagCancellation()))
    assert result.status == "ok" and result.provider == "codex"


def test_disabled_lookup_does_not_start_a_factory_or_accept_config_proof(monkeypatch):
    cr.shutdown()
    cr.reset_breakers()
    monkeypatch.setattr(cr, "_client_factory", lambda **kw: pytest.fail("blocked lookup spawned"))
    cfg = {**BASE, "web_lookup_codex_enabled": False,
           "lookup_tool_isolation_verified": True}
    assert not cr.is_available(cfg)
    with pytest.raises(cr.CodexRoleError, match="disabled"):
        cr.call("q", cfg)
    assert cr.session_calls() == {}
    assert cr._client is None


def test_disabled_lookup_falls_through_to_groq(monkeypatch):
    monkeypatch.setattr(cr, "_client_factory", lambda **kw: pytest.fail("blocked lookup spawned"))
    monkeypatch.setitem(wl._PROVIDERS, "groq_compound", lambda *a: "fallback")
    assert wl.lookup("harga emas hari ini", {**BASE, "web_lookup_codex_enabled": False}) == ("fallback", "groq_compound")


@pytest.mark.parametrize("proof", [None, object(), LookupToolPolicy(("web.run", "read_file"), "a" * 64, ())])
def test_client_without_a_valid_startup_proof_is_never_published(fake, monkeypatch, proof):
    closed = []
    client = NS(lookup_tool_policy=proof, close=lambda: closed.append(True),
                thread_start=lambda **kw: pytest.fail("unverified client used"))
    monkeypatch.setattr(cr, "_client_factory", lambda **kw: client)
    with pytest.raises(cr.CodexRoleError, match="startup tool proof"):
        cr.call("query", BASE)
    assert closed and cr._client is None


def test_lookup_flag_defaults_off_without_affecting_other_roles(fake, monkeypatch):
    cfg = {**BASE}
    del cfg["web_lookup_codex_enabled"]
    assert not cr.is_enabled(cfg) and not cr.is_available(cfg)
    with pytest.raises(cr.CodexRoleError, match="disabled"):
        cr.call("query", cfg)
    assert fake.clients == []
    assert cr.is_enabled({**cfg, "codex_role": "scout"})
