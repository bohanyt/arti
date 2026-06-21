"""Fase 2 offline tests (no live stream / no API calls)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_openrouter
import session_transcript


def test_filter_meta_history_talk():
    import hermes_vtuber_bridge as bridge

    bad = "Halo! Aku membaca 12 catatan sejarah stream kamu, dan kamu memanggil aku?"
    cleaned, n = bridge.filter_meta_history_talk(bad)
    assert n >= 1
    assert "membaca" not in cleaned.lower()
    narrator = (
        "Co-host dipanggil oleh Streamer. Pertanyaan atau panggilan sekarang adalah: oke dengar. "
        "Aku harus merespons dengan gaya yang sesuai."
    )
    assert bridge.is_narrator_reply(narrator)
    out = bridge.post_process_response(narrator, "Oke artinya kamu dengar")
    assert "dengar" in out.lower() and "Arti ini" not in out
    ok = "Halo juga! Ada apa nih?"
    cleaned_ok, n2 = bridge.filter_meta_history_talk(ok)
    assert n2 == 0 and "Halo" in cleaned_ok


def test_incharacter_fallback_viewers():
    import hermes_vtuber_bridge as bridge

    fb = bridge.incharacter_fallback_reply("Kamu ingat viewer kita siapa aja")
    assert "ulang" in fb.lower() or "inget" in fb.lower() or len(fb) > 8


def test_parse_json_blob():
    text = 'bla bla {"summary": "ok", "emotion": "neutral"}'
    data = arti_openrouter._parse_json_blob(text)
    assert data["summary"] == "ok"


def test_openrouter_live_chain_fast_only():
    chain = arti_openrouter.openrouter_live_model_chain(
        {
            "openrouter_live_fast_only": True,
            "openrouter_live_model": "poolside/laguna-xs.2:free",
            "openrouter_live_last_resort": "owl-alpha",
        }
    )
    assert chain == ["poolside/laguna-xs.2:free", "owl-alpha"]


def test_groq_single_attempt_then_openrouter(monkeypatch):
    import hermes_vtuber_bridge as bridge

    cfg = {
        "groq_api_key": "gsk_test",
        "groq_roll_all_models_on_limit": False,
        "groq_models": ["qwen/qwen3-32b", "llama-3.1-8b-instant"],
        "groq_model_fast": "llama-3.1-8b-instant",
        "openrouter_live_fallback_enabled": True,
        "openrouter_api_key": "sk-or-test",
    }
    posts = {"n": 0}

    def fake_post(url, *args, **kwargs):
        posts["n"] += 1
        class R:
            status_code = 429
            text = "rate limit"
        return R()

    def fake_or_live(system, user, config):
        return "ok", "openrouter/poolside/laguna-xs.2:free"

    monkeypatch.setattr(bridge.requests, "post", fake_post)
    monkeypatch.setattr(bridge.arti_openrouter, "openrouter_live_completion", fake_or_live)

    reply, model = bridge.groq_chat_completion("qwen/qwen3-32b", "sys", "user", cfg)
    assert reply == "ok"
    assert posts["n"] == 1


def test_groq_falls_back_to_openrouter(monkeypatch):
    import hermes_vtuber_bridge as bridge

    cfg = {
        "groq_api_key": "gsk_test",
        "groq_models": ["llama-3.1-8b-instant"],
        "groq_model_fast": "llama-3.1-8b-instant",
        "openrouter_live_fallback_enabled": True,
        "openrouter_api_key": "sk-or-test",
        "openrouter_live_model": "owl-alpha",
    }

    def fake_post(url, *args, **kwargs):
        class R:
            status_code = 429
            text = "rate limit"

        return R()

    def fake_or_live(system, user, config):
        return "Halo dari OpenRouter!", "openrouter/owl-alpha"

    monkeypatch.setattr(bridge.requests, "post", fake_post)
    monkeypatch.setattr(bridge.arti_openrouter, "openrouter_live_completion", fake_or_live)

    reply, model = bridge.groq_chat_completion(
        "llama-3.1-8b-instant", "sys", "user", cfg
    )
    assert reply == "Halo dari OpenRouter!"
    assert model == "openrouter/owl-alpha"


def test_groq_fallback_chain():
    import hermes_vtuber_bridge as bridge

    cfg = {
        "groq_models": ["qwen/qwen3-32b", "llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
        "groq_model_fast": "llama-3.1-8b-instant",
    }
    chain = bridge._groq_fallback_chain("qwen/qwen3-32b", cfg)
    assert chain[0] == "qwen/qwen3-32b"
    assert "llama-3.1-8b-instant" in chain[1:]
    assert len(chain) == len(set(chain))


def test_pick_groq_model_routing():
    import hermes_vtuber_bridge as bridge

    cfg = {
        "smart_groq_routing": True,
        "groq_models": [
            "llama-3.1-8b-instant",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "qwen/qwen3-32b",
            "llama-3.3-70b-versatile",
        ],
        "groq_model_fast": "llama-3.1-8b-instant",
        "groq_model_medium": "meta-llama/llama-4-scout-17b-16e-instruct",
        "groq_model_strong": "qwen/qwen3-32b",
        "groq_model_rare": "llama-3.3-70b-versatile",
    }
    assert bridge.pick_groq_model("halo arti", cfg) == "llama-3.1-8b-instant"
    assert bridge.pick_groq_model("kenapa stream lag banget dan gimana fixnya?", cfg) == "qwen/qwen3-32b"
    long_q = "kenapa " * 30 + "jelaskan detail " * 5
    assert bridge.pick_groq_model(long_q, cfg) == "llama-3.3-70b-versatile"


def test_pick_groq_model_for_turn_by_trigger():
    import hermes_vtuber_bridge as bridge

    cfg = {
        "smart_groq_routing": True,
        "groq_models": [
            "llama-3.1-8b-instant",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "qwen/qwen3-32b",
            "llama-3.3-70b-versatile",
        ],
        "groq_model_fast": "llama-3.1-8b-instant",
        "groq_model_medium": "meta-llama/llama-4-scout-17b-16e-instruct",
        "groq_model_strong": "qwen/qwen3-32b",
        "groq_model_rare": "llama-3.3-70b-versatile",
    }
    assert (
        bridge.pick_groq_model_for_turn("halo", cfg, trigger_type="curious")
        == "llama-3.1-8b-instant"
    )
    assert (
        bridge.pick_groq_model_for_turn("halo", cfg, trigger_type="yt_chat")
        == "qwen/qwen3-32b"
    )


def test_get_categorized_history_strips_timestamps():
    import hermes_vtuber_bridge as bridge

    with bridge.history_lock:
        bridge.stream_history.clear()
        bridge.stream_history.append("[11:04:23] [Streamer] coba")
        bridge.stream_history.append("[11:04:24] [Arti (VTuber)] ini lo")
        bridge.stream_history.append("[11:04:25] [Viewer Alice (YouTube)] halo")

    out = bridge.get_categorized_history()
    assert "[11:04:23]" not in out
    assert "[11:04:24]" not in out
    assert "Streamer: coba" in out
    assert "Arti: ini lo" in out
    assert "Alice: halo" in out


def test_viewer_scoped_context_filters(monkeypatch):
    import hermes_vtuber_bridge as bridge

    with bridge.history_lock:
        bridge.stream_history.clear()
        bridge.stream_history.append("[12:00:01] [Viewer Alice (YouTube)] halo")
        bridge.stream_history.append("[12:00:02] [Viewer Bob (YouTube)] test")
        bridge.stream_history.append("[12:00:03] [Streamer] eh arti")
        bridge.stream_history.append("[12:00:04] [Arti (VTuber)] hai Alice")

    ctx = bridge.get_viewer_scoped_context("Alice", bridge.CONFIG)
    assert "Alice" in ctx
    assert "Bob" not in ctx
    assert "Streamer" in ctx or "STREAMER" in ctx


def test_finalize_session_artifacts_smoke(tmp_path, monkeypatch):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    sid = "2099-01-01-test"
    tx = tdir / f"{sid}.jsonl"
    tx.write_text(
        json.dumps({"ts": "12:00:00", "kind": "viewer", "name": "x", "text": "arti halo"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(session_transcript, "_ROOT", tmp_path)
    session_transcript._session_id = sid
    session_transcript._transcript_path = tx
    session_transcript._manifest_path = tmp_path / "data" / "manifest.json"
    session_transcript._manifest_path.parent.mkdir(parents=True, exist_ok=True)
    session_transcript._manifest_path.write_text("{}", encoding="utf-8")

    cfg = {
        "stream_session_id": sid,
        "transcript_dir": "transcripts",
        "active_profile": "test",
        "groq_api_key": "",
    }

    def fake_groq_summarize(*_a, **_k):
        return "Ringkasan uji otomatis."

    monkeypatch.setattr(session_transcript, "summarize_session_for_vault", fake_groq_summarize)
    session_transcript.finalize_session_artifacts(cfg, None)

    vault = tmp_path / "vault" / "sessions" / f"{sid}.md"
    assert vault.is_file()
    assert "Ringkasan uji otomatis" in vault.read_text(encoding="utf-8")


def test_incharacter_fallback_nyala():
    import hermes_vtuber_bridge as bridge

    yt = '[Pesan Live Chat dari Viewer TestViewer (YouTube)]: "co-host kamu nyala gk??"'
    fb = bridge.incharacter_fallback_reply(yt)
    assert "nyala" in fb.lower()


def test_compact_llm_context_limits(monkeypatch):
    import hermes_vtuber_bridge as bridge

    with bridge.history_lock:
        bridge.stream_history.clear()
        for i in range(10):
            bridge.stream_history.append(f"[12:00:{i:02d}] [Streamer] line {i}")
        for i in range(10):
            bridge.stream_history.append(
                f"[12:01:{i:02d}] [Viewer Alice (YouTube)] chat {i}"
            )
        for i in range(5):
            bridge.stream_history.append(f"[12:02:0{i}] [Arti (VTuber)] reply {i}")

    cfg = {
        **bridge.CONFIG,
        "llm_history_streamer_max": 2,
        "llm_history_viewer_max": 2,
        "llm_history_arti_max": 1,
    }
    ctx = bridge.get_compact_llm_context(None, cfg)
    assert ctx.count("[Streamer]") == 2
    assert ctx.count("Viewer Alice") == 2
    assert ctx.count("[Arti (VTuber)]") == 1


def test_strip_reflection_reasoning_preamble():
    from arti_openrouter import strip_reflection_reasoning_preamble

    raw = (
        "We need to produce analysis in Indonesian.\n"
        "Let's craft.\n\n"
        "**Analisis Bahasa Indonesia**\n\n"
        "Dalam sesi stream ini, Arti responsif."
    )
    out = strip_reflection_reasoning_preamble(raw)
    assert "We need to produce" not in out
    assert "Analisis Bahasa Indonesia" in out
    assert "Dalam sesi stream" in out


def test_trim_system_prompt_for_llm():
    import hermes_vtuber_bridge as bridge

    base = "X" * 100
    bloated = (
        base
        + "\n\n[RINGKASAN KONTEKS TERAKHIR]\n"
        + ("S" * 3000)
        + "\n\n[VIEWER YANG DIKETAHUI:]\n"
        + ("Y" * 200)
    )
    trimmed = bridge.trim_system_prompt_for_llm(
        bloated, {"llm_system_prompt_max_chars": 400}
    )
    assert len(trimmed) <= 400
    assert "VIEWER YANG DIKETAHUI" in trimmed
    assert "RINGKASAN KONTEKS TERAKHIR" not in trimmed


def test_post_process_truncates_not_fallback_on_long_reply():
    import hermes_vtuber_bridge as bridge

    long_ok = (
        "Halo Streamer! Stream malam ini seru banget sih, penontonnya rame dan banyak yang lucu. "
        "Aku suka banget pas kalian ngobrolin game, rasanya kayak temen nongkrong. "
        "Tapi ya, jangan lupa istirahat juga ya, jangan sampe burnout!"
    )
    out = bridge.post_process_response(long_ok, "ngobrol dong guys")
    assert "otakku ngelag" not in out.lower()
    assert "stream malam" in out.lower()
    assert len(out) <= bridge.CONFIG["arti_reply_max_chars"] + 3


def test_get_arti_reply_limits_yt_vs_ptt():
    import hermes_vtuber_bridge as bridge

    yt_hi = "[Pesan Live Chat dari Viewer @x (YouTube)]: halo"
    yt_deep = (
        "[Pesan Live Chat dari Viewer @x (YouTube)]: arti menurut kamu "
        "kenapa RAG embedding lokal lebih bagus untuk live stream?"
    )
    ptt = "Streamer bilang halo co-host"
    assert bridge.get_arti_reply_limits(yt_hi)[0] <= 2
    assert bridge.get_arti_reply_limits(yt_deep)[0] >= 3
    assert bridge.get_arti_reply_limits(ptt)[0] == 5


def test_strip_tts_expression_tags():
    import hermes_vtuber_bridge as bridge

    assert "<laugh>" not in bridge.strip_tts_expression_tags(
        "Ya kali sih <laugh> masa gitu aja bingung"
    )
    assert "haha" in bridge.strip_tts_expression_tags(
        "Ya kali sih <laugh> masa gitu aja bingung"
    ).lower()
    out = bridge.strip_tts_expression_tags("Capek banget <sigh> aduh <breath> ya")
    assert "<" not in out
    assert "sigh" not in out.lower()
    assert "breath" not in out.lower()
    assert "Capek" in out
    pp = bridge.post_process_response("Oke deh <laugh>!", "halo")
    assert "<laugh>" not in pp
