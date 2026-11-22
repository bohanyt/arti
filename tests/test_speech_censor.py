"""Sensor kata keluaran Arti — murni, lokal, dan sebelum semua permukaan publik."""

from __future__ import annotations

from pathlib import Path

import arti_speech_censor as censor


ROOT = Path(__file__).resolve().parent.parent
BRIDGE_SOURCE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


def test_contoh_bohan_diswap_tanpa_mengubah_kalimat_lain():
    assert censor.censor_text("kamu kok tai sih?") == "kamu kok sensor sih?"


def test_case_tanda_baca_dan_beberapa_kata():
    assert censor.censor_text("TAI! Dasar goblok.") == "sensor! Dasar sensor."


def test_tidak_makan_substring_kata_biasa():
    assert censor.censor_text("santai dulu, pantainya bagus") == (
        "santai dulu, pantainya bagus"
    )


def test_akhiran_indonesia_tetap_terdengar_alami():
    assert censor.censor_text("tainya kelihatan, gobloknya kebangetan") == (
        "sensornya kelihatan, sensornya kebangetan"
    )


def test_bisa_dimatikan_dan_daftar_bisa_ditambah_dari_config():
    cfg = {
        "speech_censor_enabled": False,
        "speech_censor_words": ["umpatan rahasia"],
    }
    assert censor.censor_from_config("umpatan rahasia", cfg) == "umpatan rahasia"
    cfg["speech_censor_enabled"] = True
    assert censor.censor_from_config("itu umpatan   rahasia!", cfg) == "itu sensor!"


def test_bridge_menyensor_sebelum_chunk_log_history_dan_chat_game():
    """Raw reply tidak boleh lolos ke satu pun permukaan publik utama."""
    anchor = BRIDGE_SOURCE.index("arti_expression_runtime.parse_reply_emotion")
    # The rest of the same function (up to the next top-level definition), not a fixed byte count: flag-guarded
    # additions between the censor and the surfaces must not push a marker out of a window and fail the guard.
    end = min(i for i in (BRIDGE_SOURCE.find("\nasync def ", anchor), BRIDGE_SOURCE.find("\ndef ", anchor)) if i > 0)
    block = BRIDGE_SOURCE[anchor:end]
    i_censor = block.index("arti_speech_censor.censor_from_config")
    for marker in (
        "tts_sentence_chunks = _sentences_or_empty(ai_reply)",
        'print(f"Arti menjawab:',
        'add_to_history("Arti (VTuber)", ai_reply',
        "_cermin_ke_chat_game(ai_reply)",
    ):
        assert i_censor < block.index(marker), marker


def test_tts_entrypoint_memiliki_pagar_terakhir_untuk_semua_jalur_suara():
    anchor = BRIDGE_SOURCE.index("async def speak(self, text: str):")
    block = BRIDGE_SOURCE[anchor:anchor + 1800]
    assert block.index("arti_speech_censor.censor_from_config") < block.index(
        'engine = CONFIG.get("tts_engine"'
    )
