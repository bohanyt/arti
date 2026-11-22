"""Dua bug log tes 18 Agu 22.41: RAG pertama timeout (cold load Qwen3) +
spam retry token YouTube saat ID salah/stream mati."""

from __future__ import annotations

from pathlib import Path

import arti_vault_rag as rag

_SRC = (Path(__file__).resolve().parent.parent / "arti_bridge.py"
        ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Pemanasan embedding
# --------------------------------------------------------------------------

def test_ping_pemanasan_pakai_timeout_longgar(monkeypatch):
    """Cold load LM Studio 10-16 dtk; ping WAJIB diberi timeout > 8 dtk
    default query — kalau tidak, pemanasnya sendiri ikut timeout."""
    terekam = {}

    def _palsu(texts, cfg, **kwargs):
        terekam["timeout"] = cfg.get("lmstudio_embedding_timeout_sec")
        terekam["texts"] = texts
        return [[0.0]]

    monkeypatch.setattr(rag, "embed_texts", _palsu)
    rag._prewarm_ping({**rag.DEFAULT_CONFIG, "lmstudio_embedding_timeout_sec": 90})
    assert terekam["timeout"] == 90
    assert terekam["texts"]


def test_ping_pemanasan_tidak_meledak_saat_lmstudio_mati(monkeypatch, capsys):
    def _mati(*a, **k):
        raise ConnectionError("LM Studio mati")

    monkeypatch.setattr(rag, "embed_texts", _mati)
    rag._prewarm_ping(dict(rag.DEFAULT_CONFIG))  # tidak boleh raise
    assert "Pemanasan embedding gagal" in capsys.readouterr().out


def test_wizard_memanaskan_embedding_di_awal():
    blok = _SRC.split("ARTI BRIDGE — Startup Checklist")[1][:600]
    assert "prewarm_embedding" in blok, \
        "pemanasan harus mulai di awal wizard, selagi Bohan menjawab checklist"


# --------------------------------------------------------------------------
# Backoff token YouTube
# --------------------------------------------------------------------------

def test_retry_token_yt_punya_backoff():
    blok = _SRC.split("Gagal ambil token (")[0][-1500:]
    assert "gagal_token += 1" in blok
    assert "300" in blok, "backoff harus mencapai 5 menit, bukan 15 dtk selamanya"


def test_diagnosis_keras_setelah_4_gagal():
    # Pesan harus menyebut dua kemungkinan akar + ID yang sedang dipakai.
    assert "SUDAH BERAKHIR atau Video ID salah" in _SRC
    blok = _SRC.split("SUDAH BERAKHIR atau Video ID salah")[1][:300]
    assert "youtube_video_id" in blok


def test_sukses_mereset_hitungan_gagal():
    blok = _SRC.split("Terhubung! {len(initial_msgs)} pesan awal")[0][-300:]
    assert "gagal_token = 0" in blok, \
        "tanpa reset, koneksi putus-nyambung langsung kena backoff 5 menit"
