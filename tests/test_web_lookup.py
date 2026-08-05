"""Fitur C (v0.7): internet fast lookup.

Terukur spike 2026-08-02: groq/compound-mini 6,8 dtk (utama), cursor
grok-4.5/low + web 17,6 dtk (fallback). Kunci desain:
1. Pemicu KONSERVATIF — "kamu ngapain sekarang?" bukan pertanyaan web.
2. Chain gagal total = string kosong, Arti jawab biasa (jangan pernah bisu).
3. Role cursor "lookup" = SATU-SATUNYA yang boleh tool call (web search).
Semua tanpa jaringan (provider di-monkeypatch).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_cursor_agent as ca
import arti_web_lookup as wl

ON = {"web_lookup_enabled": True}


# --- pemicu --------------------------------------------------------------------


def test_disabled_never_triggers():
    assert wl.needs_web_lookup("berita hari ini apa?", {}) is False


def test_explicit_search_requests_trigger():
    for t in ("arti cek google dong harga iphone", "coba googling deh",
              "tolong cari di internet soal itu"):
        assert wl.needs_web_lookup(t, ON) is True, t


def test_timebound_topics_trigger():
    for t in ("berita teknologi hari ini apa?", "harga bitcoin berapa ya",
              "skor bola semalam gimana", "kurs dolar sekarang",
              "update terbaru genshin apa"):
        assert wl.needs_web_lookup(t, ON) is True, t


def test_ordinary_chat_does_not_trigger():
    for t in ("kamu lagi ngapain sekarang?", "halo arti", "arti kamu bisa sedih ga",
              "menurutmu bohan gimana", "ceritain dong pengalaman kamu"):
        assert wl.needs_web_lookup(t, ON) is False, t


# --- chain ----------------------------------------------------------------------


def test_chain_falls_through_to_cursor(monkeypatch):
    def _boom(q, c):
        raise RuntimeError("groq mati")

    monkeypatch.setitem(wl._PROVIDERS, "groq_compound", _boom)
    monkeypatch.setitem(wl._PROVIDERS, "cursor", lambda q, c: "hasil dari cursor (JawaPos)")
    text, src = wl.lookup("berita hari ini", ON)
    assert text == "hasil dari cursor (JawaPos)"
    assert src == "cursor"


def test_chain_total_failure_returns_none_and_block_empty(monkeypatch):
    def _boom(q, c):
        raise RuntimeError("mati")

    monkeypatch.setitem(wl._PROVIDERS, "groq_compound", _boom)
    monkeypatch.setitem(wl._PROVIDERS, "cursor", _boom)
    text, _why = wl.lookup("berita hari ini", ON)
    assert text is None
    assert wl.lookup_block("berita hari ini", ON) == "", "gagal = kosong, bukan exception"


def test_result_capped_and_block_shape(monkeypatch):
    monkeypatch.setitem(wl._PROVIDERS, "groq_compound",
                        lambda q, c: "kata " * 300)
    cfg = {**ON, "web_lookup_max_chars": 100}
    text, _ = wl.lookup("berita hari ini", cfg)
    assert len(text) <= 101  # +ellipsis
    blk = wl.lookup_block("berita hari ini", cfg)
    assert "[INFO INTERNET" in blk
    assert "JANGAN menyebut nama mesin" in blk


# --- role cursor "lookup" --------------------------------------------------------


def test_lookup_role_resolves_grok_low_and_allows_tools():
    assert ca.resolve_role_model("lookup", {}) == ("grok-4.5", "low")
    assert ca.role_allows_tools("lookup", {}) is True
    for role in ("voice", "scout", "vision"):
        assert ca.role_allows_tools(role, {}) is False, (
            f"role {role} tidak boleh tool call — hanya lookup"
        )


def test_scout_and_vision_role_defaults_unchanged():
    # Revisi 2026-08-03: scout turun ke composer (grok tiap menit kemahalan);
    # observer yang mewarisi grok-high.
    assert ca.resolve_role_model("scout", {}) == ("composer-2.5", None)
    assert ca.resolve_role_model("observer", {}) == ("grok-4.5", "high")
    assert ca.resolve_role_model("vision", {}) == ("composer-2.5", None)


# --- wiring & default shipped -----------------------------------------------------


def test_shipped_default_off_and_keys_exist():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"web_lookup_enabled": False,' in src, "kill switch wajib default OFF"
    for key in ("web_lookup_provider_chain", "web_lookup_groq_model",
                "web_lookup_turn_budget_sec", "cursor_lookup_model",
                "cursor_lookup_allow_tools"):
        assert f'"{key}"' in src


def test_pipeline_runs_lookup_parallel_with_rag():
    src = (ROOT / "arti_voice_pipeline.py").read_text(encoding="utf-8")
    assert "_load_web_lookup()" in src
    gather_at = src.index("asyncio.gather(")
    assert "_load_web_lookup()" in src[gather_at:gather_at + 240], (
        "lookup harus di gather yang sama dengan RAG — paralel, bukan serial"
    )


def test_pipeline_injects_block_into_system(monkeypatch):
    import asyncio

    import arti_voice_pipeline as vp

    monkeypatch.setattr(wl, "needs_web_lookup", lambda t, c: True)
    monkeypatch.setattr(wl, "lookup_block",
                        lambda q, c: "\n\n[INFO INTERNET — dicek barusan:]\nhasil uji")
    ctx = asyncio.run(
        vp.prepare_turn_context(
            "[Pesan Live Chat dari Viewer @x (YouTube)]: berita hari ini apa?",
            [],
            "system base",
            {"vault_rag_live_enabled": False, "web_lookup_enabled": True},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "[history]",
            extract_trigger_message=lambda s: s,
        )
    )
    assert "[INFO INTERNET" in ctx.llm_system
    assert "hasil uji" in ctx.llm_system
