# -*- coding: utf-8 -*-
"""Mode Luna-utama (sebuah eksperimen: Codex Luna jadi suara utama).

Kontrak: codex_primary_voice menempatkan Codex SEBELUM composer di jalur
suara premium; gagal apa pun jatuh mulus ke alur composer lama; default
SOURCE tetap OFF (kenop milik Bohan di config_local â€” pelajaran
codex_effort: tes jangan mengunci nilai runtime).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_bridge as b  # noqa: E402

SRC = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


def _fallback_source():
    import ast

    function = next(node for node in ast.parse(SRC).body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_cursor_reply_with_fallback")
    return ast.get_source_segment(SRC, function)


def test_source_default_off():
    assert '"codex_primary_voice": False' in SRC


def test_luna_tidak_tersandera_cursor(monkeypatch):
    """Audit 20 Agu: _should_route_to_cursor balik False saat Cursor tak
    tersedia -> jalur (yang memuat blok Luna) tak pernah dipanggil -> siaran
    turun SENYAP ke Groq walau Luna sehat. Mode Luna-utama harus lolos gerbang
    tanpa bergantung Cursor."""
    import arti_cursor_agent as aca

    monkeypatch.setattr(aca, "is_available", lambda cfg: (False, "cursor mati"))
    cfg = {**b.CONFIG, "codex_primary_voice": True, "codex_agent_enabled": True,
           "cursor_agent_enabled": False, "cursor_trigger_types": ["yt_chat"],
           # agy DIPIN mati: kalau agy-primary nyala di config_local, gerbang
           # balik True untuk semua trigger dan tes Luna ini kehilangan makna.
           "agy_agent_enabled": False, "agy_primary_voice": False}
    assert b._should_route_to_cursor("yt_chat", cfg) is True
    # Trigger di luar daftar tetap TIDAK dirutekan (pagar lama utuh)
    assert b._should_route_to_cursor("curious", cfg) is False
    # Tanpa mode Luna-utama, Cursor mati = jalur mati (perilaku lama)
    cfg_lama = {**cfg, "codex_primary_voice": False, "cursor_agent_enabled": True}
    assert b._should_route_to_cursor("yt_chat", cfg_lama) is False


def test_urutan_codex_sebelum_composer():
    """Blok Luna-utama harus SETELAH prewarm (dua kolam tetap hangat) dan
    SEBELUM tunggu-pemanasan trigger berharga / alur composer."""
    blok = _fallback_source()
    i_prewarm = blok.index("arti_codex_agent.prewarm(cfg)")
    i_primary = blok.index('cfg.get("codex_primary_voice"')
    i_precious = blok.index("cursor_warmup_wait_precious_sec")
    assert i_prewarm < i_primary < i_precious, (
        "urutan wajib: prewarm -> blok Luna-utama -> alur composer")


def test_luna_utama_menjawab_tanpa_composer(monkeypatch):
    """Eksekusi nyata jalur: codex_primary + send_turn sukses -> sumber
    'codex', composer & Groq tidak pernah disentuh."""
    monkeypatch.setattr(b.arti_codex_agent, "prewarm", lambda cfg: True)
    monkeypatch.setattr(
        b.arti_codex_agent, "send_turn",
        lambda sys_p, prompt, cfg: "Jawaban Luna tes.",
    )
    import arti_cursor_agent as aca
    monkeypatch.setattr(aca, "prewarm", lambda cfg: True)

    def _meledak(*a, **kw):  # composer/groq tidak boleh tersentuh
        raise AssertionError("jalur composer/groq ikut terpanggil")

    monkeypatch.setattr(aca, "chat", _meledak, raising=False)
    monkeypatch.setattr(b, "groq_chat_completion", _meledak)

    cfg = {**b.CONFIG, "codex_primary_voice": True, "codex_agent_enabled": True, "_codex_primary_tes": True}
    reply, kalimat, sumber = asyncio.run(
        b._cursor_reply_with_fallback("sys", "halo", "halo", cfg, "mic")
    )
    assert reply == "Jawaban Luna tes."
    assert sumber == "codex"


def test_luna_gagal_langsung_groq_tanpa_kaskade():
    """Revisi 20 Agu (log 16.34: llm p90 27 dtk = pagar Luna 8s + kapak
    composer 16s menumpuk): Luna gagal harus LANGSUNG Groq, composer cuma
    jaring terakhir. Cek source: blok primary memuat Groq sebelum jatuh."""
    blok = _fallback_source()
    i_primary = blok.index('cfg.get("codex_primary_voice"')
    i_groq_primer = blok.index("mencoba fallback Groq (composer dilewati)")
    i_precious = blok.index("cursor_warmup_wait_precious_sec")
    assert i_primary < i_groq_primer < i_precious, (
        "Groq pasca-Luna harus di dalam blok primary, sebelum alur composer")


def test_luna_gagal_jatuh_ke_groq(monkeypatch):
    """send_turn meledak -> Groq menjawab (eksekusi nyata jalur baru)."""
    monkeypatch.setattr(b.arti_codex_agent, "prewarm", lambda cfg: True)

    def _gagal(*a, **kw):
        raise RuntimeError("Luna tumbang")

    monkeypatch.setattr(b.arti_codex_agent, "send_turn", _gagal)
    import arti_cursor_agent as aca
    monkeypatch.setattr(aca, "prewarm", lambda cfg: False)
    monkeypatch.setattr(aca, "is_warm", lambda: False)
    monkeypatch.setattr(
        b, "pick_groq_model", lambda msg, cfg: "model-tes", raising=False
    )
    monkeypatch.setattr(
        b, "groq_chat_completion",
        lambda model, sys_p, prompt, cfg: ("Jawaban Groq tes.", "model-tes"),
    )
    cfg = {**b.CONFIG, "codex_primary_voice": True, "codex_agent_enabled": True,
           "_codex_primary_tes": True, "cursor_warmup_wait_precious_sec": 0.0}
    reply, _kal, sumber = asyncio.run(
        b._cursor_reply_with_fallback("sys", "halo", "halo", cfg, "mic")
    )
    assert reply == "Jawaban Groq tes."
    assert sumber.startswith("groq")
