"""Voice providers must retain current Live7 facts through fallback and trims."""

import asyncio
from types import SimpleNamespace

import pytest

import arti_bridge as bridge
import arti_voice_pipeline as pipeline
from arti_live7_status_facts import StatusFactsRelay, parse_event


@pytest.fixture
def status():
    relay = StatusFactsRelay({}, None, clock=lambda: 100)
    relay._accept(parse_event({"ev": "cycle_stopped"}), 100)
    return relay.status_block()


@pytest.mark.parametrize("optional", [False, True])
def test_normal_trim_preserves_complete_status(status, optional):
    prefix = "identity " * 1500
    if optional:
        prefix = "identity" + "\n\n[HARI INI]\n" + prefix
    trimmed = bridge.trim_system_prompt_for_llm(
        prefix + status, {"llm_system_prompt_max_chars": 1000})
    assert status.strip() in trimmed
    assert len(trimmed) <= 1000


def test_413_trim_preserves_complete_status(status):
    system, user = bridge._trim_llm_payload("identity " * 3000 + status, "question")
    assert status.strip() in system
    assert user == "question"
    assert len(system) + len(user) <= 14000


def test_status_wins_over_misconfigured_tiny_cap(status):
    trimmed = bridge.trim_system_prompt_for_llm(
        "identity " * 100 + status, {"llm_system_prompt_max_chars": 10})
    assert trimmed.strip() == status.strip()


def test_stopped_status_names_arti_as_subject(status):
    # The normal role block is absent once _stardew_live() becomes false.
    assert "ARTI" in status and "'aku'" in status
    assert "Bohan menghentikan sesi Stardew" in status


@pytest.mark.parametrize("retry_413", [False, True])
def test_codex_failure_groq_wire_keeps_same_status(monkeypatch, status, retry_413):
    async def run(_lane, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(bridge.arti_executors, "run", run)
    monkeypatch.setattr(bridge.arti_codex_agent, "prewarm", lambda _: True)
    monkeypatch.setattr(bridge.arti_agy_agent, "prewarm", lambda _: None)
    import arti_cursor_agent
    monkeypatch.setattr(arti_cursor_agent, "prewarm", lambda _: False)
    monkeypatch.setattr(bridge.arti_provider_health, "enabled", lambda: False)
    primary = []

    def codex(system, user, _cfg):
        primary.append((system, user))
        return None  # Same contract as a timed-out send_turn.

    monkeypatch.setattr(bridge.arti_codex_agent, "send_turn", codex)
    monkeypatch.setattr(bridge, "pick_groq_model", lambda *_a, **_kw: "test-primary")
    wire = []

    def post(_url, *, headers, json, timeout):
        messages = [dict(message) for message in json["messages"]]
        wire.append(messages)
        code = (413 if retry_413 else 404) if len(wire) == 1 else 200
        return SimpleNamespace(status_code=code, text="model_not_found", json=lambda: {
            "error": {"code": "model_not_found"},
            "choices": [{"message": {"content": "Sesi Stardew aku berhenti."},
                         "finish_reason": "stop"}]})

    monkeypatch.setattr(bridge.requests, "post", post)
    cfg = {**bridge.CONFIG, "codex_primary_voice": True, "codex_agent_enabled": True,
           "_codex_primary_tes": True, "agy_agent_enabled": False,
           "agy_primary_voice": False, "groq_api_key": "unit-test-only",
           "groq_models": ["test-primary", "test-next"],
           "groq_model_fast": "test-next", "groq_roll_all_models_on_limit": True,
           "llm_system_prompt_max_chars": 20000, "vault_rag_live_enabled": False,
           "web_lookup_enabled": False}
    ctx = asyncio.run(pipeline.prepare_turn_context(
        "Arti, status game sekarang bagaimana?", [], "identity " * 1700 + status, cfg,
        trim_system_prompt=bridge.trim_system_prompt_for_llm,
        append_watch_party_context=lambda text: text,
        get_categorized_history=lambda: "", extract_trigger_message=lambda text: text))
    reply, _, source = asyncio.run(bridge._cursor_reply_with_fallback(
        ctx.llm_system, ctx.prompt_content, ctx.speech, cfg, "mic"))
    assert reply == "Sesi Stardew aku berhenti." and source.startswith("groq:")
    assert len(primary) == 1 and len(wire) == 2
    assert wire[0][0]["content"] == primary[0][0]
    assert wire[0][1]["content"] == primary[0][1]
    for messages in wire:
        assert status.strip() in messages[0]["content"]
