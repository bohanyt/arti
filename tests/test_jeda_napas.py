"""Jeda napas antar kalimat + ekor pasif pendek (17 Agu 2026).

Dua permintaan Bohan dari sesi ngobrol:
1. "[recorded remark omitted]" — tiap kalimat jadi beat obrolan dengan
   celah untuk nimpali.
2. "[recorded remark omitted]" — ekor sunyi 5 dtk cuma perlu saat
   toggle ON; jalur pasif dapat ekor pendek sendiri.
"""

from __future__ import annotations

from pathlib import Path

import pipeline_timer
import arti_bridge as bridge

_SRC = (Path(__file__).resolve().parent.parent / "arti_bridge.py"
        ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Jeda antar kalimat
# --------------------------------------------------------------------------

def test_jeda_hanya_di_antara_kalimat_bukan_sebelum_pertama():
    """`if _i and _jeda > 0` — kalimat PERTAMA tidak boleh menunggu."""
    blok = _SRC.split("JEDA NAPAS antar kalimat")[1][:900]
    assert "if _i and _jeda > 0:" in blok
    assert "await asyncio.sleep(_jeda)" in blok


def test_jeda_default_nol_di_source_config():
    """Default SOURCE 0 = perilaku lama; mode ngobrol milik config_local
    (di mesin uji di-set 4.0 — CONFIG hasil overlay memuat nilai itu)."""
    assert '"tts_jeda_antar_kalimat_sec": 0.0,' in _SRC
    assert float(bridge.CONFIG["tts_jeda_antar_kalimat_sec"]) >= 0.0


def test_jeda_dicatat_terpisah_di_latency():
    pipeline_timer.consume_tts_stages()
    pipeline_timer.note_tts_synth_ms(100)
    pipeline_timer.note_tts_play_ms(200)
    pipeline_timer.note_tts_jeda_ms(4000)
    pipeline_timer.note_tts_jeda_ms(4000)

    stages = pipeline_timer.consume_tts_stages()

    assert stages["tts_jeda_ms"] == 8000
    assert stages["tts_synth_ms"] == 100  # jeda TIDAK menyamar jadi synth/play
    assert stages["tts_play_ms"] == 200


def test_jeda_masuk_rekonsiliasi_bukan_lain():
    """Hening yang disengaja tidak boleh menggelembungkan `lain` — misteri
    residu 12 Agu jangan lahir lagi lewat pintu ini."""
    import time as _t
    pipeline_timer.consume_tts_stages()
    timer = pipeline_timer.PipelineTimer()
    timer.mark("after_llm")
    pipeline_timer.note_tts_synth_ms(1)
    pipeline_timer.note_tts_play_ms(1)
    _t.sleep(0.05)
    pipeline_timer.note_tts_jeda_ms(50)   # jeda nyata yang ikut berlalu
    timer.mark("after_tts")

    stages = timer.stages_ms()

    assert stages["tts_jeda_ms"] == 50
    assert stages["lain_ms"] <= 15, "jeda bocor ke lain"


def test_format_latency_menampilkan_jeda():
    baris = pipeline_timer.format_latency_line(
        {"tts_ms": 5000, "tts_jeda_ms": 8000, "total_ms": 20000}
    )
    assert "tts_jeda=8000ms" in baris


# --------------------------------------------------------------------------
# Ekor pasif pendek
# --------------------------------------------------------------------------

def test_ekor_pasif_pendek_di_config_produksi():
    assert float(bridge.CONFIG["asr_pasif_silence_tail_sec"]) == 2.0
    assert float(bridge.CONFIG["asr_ptt_silence_tail_sec"]) == 5.0


def test_ekor_dipilih_berdasar_hotkey():
    """Toggle ON = ekor sabar 5 dtk; pasif = ekor pendek. Dua-duanya di cabang
    push_to_talk yang sama."""
    # Anchor unik — 'if trigger_mode == "push_to_talk":' muncul 2x di bridge.
    blok = _SRC.split("Ekor 5 dtk hanya perlu saat TOGGLE ON")[1][:1300]
    assert "if hotkey_active:" in blok
    assert "asr_ptt_silence_tail_sec" in blok
    assert "asr_pasif_silence_tail_sec" in blok
