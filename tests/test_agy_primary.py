import asyncio
from pathlib import Path

import pytest

import arti_agy_agent
import arti_cursor_agent
import arti_bridge as bridge


ROOT = Path(__file__).resolve().parents[1]


def _cfg(**overrides):
    cfg = {
        **bridge.CONFIG,
        "agy_agent_enabled": True,
        "agy_primary_voice": True,
        "agy_trigger_types": ["mic", "ptt", "yt_chat", "curious"],
        "codex_agent_enabled": True,
        "codex_primary_voice": False,
        "cursor_agent_enabled": False,
        "cursor_trigger_types": [],
        "_agy_primary_tes": True,
        "_codex_primary_tes": True,
    }
    cfg.update(overrides)
    return cfg


def _run(coro):
    return asyncio.run(coro)


def _patch_common(monkeypatch):
    monkeypatch.setattr(arti_cursor_agent, "prewarm", lambda _cfg: False)
    monkeypatch.setattr(bridge.arti_codex_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(bridge, "pick_groq_model", lambda *_a, **_k: "groq-test")
    monkeypatch.setattr(
        bridge,
        "groq_chat_completion",
        lambda *_a, **_k: ("jawaban Groq", "groq-test"),
    )


def test_default_agy_di_source_mati_dan_safe_flags_bukan_config():
    source = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert '"agy_agent_enabled": False,' in source
    assert '"agy_primary_voice": False,' in source
    assert '"agy_trigger_types": ["mic", "ptt", "yt_chat", "curious"],' in source
    for forbidden_key in (
        "agy_mode", "agy_sandbox", "agy_disable_slash_commands",
        "agy_dangerously_skip_permissions",
    ):
        assert f'"{forbidden_key}"' not in source


@pytest.mark.parametrize("trigger", ["mic", "ptt", "yt_chat", "curious"])
def test_routing_agy_hanya_empat_trigger_percakapan(trigger):
    assert bridge._should_route_to_cursor(trigger, _cfg()) is True


@pytest.mark.parametrize("trigger", ["donation", "video", "game"])
def test_routing_agy_menolak_trigger_berharga_dan_game(trigger):
    assert bridge._should_route_to_cursor(trigger, _cfg()) is False


def test_agy_sukses_menang_sebelum_luna_dan_groq(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(arti_agy_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(arti_agy_agent, "is_warm", lambda: True)
    monkeypatch.setattr(
        arti_agy_agent,
        "send_turn",
        lambda *_a, **_k: arti_agy_agent.AgyResult(ok=True, text="jawaban agy"),
    )
    monkeypatch.setattr(
        bridge.arti_codex_agent, "send_turn",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Luna terpanggil")),
    )

    reply, _chunks, source = _run(
        bridge._cursor_reply_with_fallback("sys", "prompt penuh", "ucapan", _cfg(), "mic")
    )
    assert reply == "jawaban agy" and source == "agy"


def test_agy_gagal_lalu_luna(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(arti_agy_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(
        arti_agy_agent,
        "send_turn",
        lambda *_a, **_k: arti_agy_agent.AgyResult(ok=False, reason="timeout"),
    )
    monkeypatch.setattr(
        bridge.arti_codex_agent, "send_turn", lambda *_a, **_k: "jawaban Luna"
    )
    reply, _chunks, source = _run(
        bridge._cursor_reply_with_fallback("sys", "prompt", "ucapan", _cfg(), "yt_chat")
    )
    assert reply == "jawaban Luna" and source == "codex"


def test_agy_luna_gagal_lalu_groq(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(arti_agy_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(
        arti_agy_agent,
        "send_turn",
        lambda *_a, **_k: arti_agy_agent.AgyResult(ok=False, reason="crash"),
    )
    monkeypatch.setattr(bridge.arti_codex_agent, "send_turn", lambda *_a, **_k: None)
    reply, _chunks, source = _run(
        bridge._cursor_reply_with_fallback("sys", "prompt", "ucapan", _cfg(), "curious")
    )
    assert reply == "jawaban Groq" and source.startswith("groq")


@pytest.mark.parametrize("trigger", ["donation", "video", "game"])
def test_trigger_non_agy_tetap_bisa_memakai_luna(monkeypatch, trigger):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        arti_agy_agent,
        "send_turn",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("agy terpanggil")),
    )
    monkeypatch.setattr(
        bridge.arti_codex_agent, "send_turn", lambda *_a, **_k: "jawaban Luna"
    )
    cfg = _cfg(codex_primary_voice=True, cursor_trigger_types=[trigger])
    reply, _chunks, source = _run(
        bridge._cursor_reply_with_fallback("sys", "prompt", "ucapan", cfg, trigger)
    )
    assert reply == "jawaban Luna" and source == "codex"


def test_cancel_barge_in_mengaborsi_agy_dan_naik_lagi(monkeypatch):
    _patch_common(monkeypatch)
    aborted = []
    monkeypatch.setattr(arti_agy_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(arti_agy_agent, "abort_turn", lambda reason: aborted.append(reason))

    def cancelled(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(arti_agy_agent, "send_turn", cancelled)
    with pytest.raises(asyncio.CancelledError):
        _run(bridge._cursor_reply_with_fallback("sys", "prompt", "u", _cfg(), "ptt"))
    assert aborted == ["cancelled"]
