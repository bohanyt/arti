"""Gerbang ucapan telinga desktop (19 Agu 2026) — VAD silero via
faster-whisper. Masalah: musik di desktop Bohan bikin telinga kirim
potongan ke Whisper terus (RMS tak menolong). Trade yang diterima Bohan:
lagu bervokal tetap lolos."""

from __future__ import annotations

import glob

import numpy as np
import pytest

import arti_desktop_audio as da
import arti_vad
import arti_bridge as bridge


# --------------------------------------------------------------------------
# arti_vad — model sungguhan (bundel faster-whisper, lokal, cepat)
# --------------------------------------------------------------------------

def test_derau_bukan_ucapan():
    if not arti_vad.tersedia():
        pytest.skip("faster-whisper VAD tidak tersedia")
    rng = np.random.default_rng(7)
    derau = (rng.standard_normal(80000) * 0.1).astype("float32")
    assert arti_vad.ada_ucapan(derau) is False


def test_suara_arti_asli_terdeteksi_ucapan():
    """Kontrol positif: wav cache refleks = suara Arti sungguhan."""
    if not arti_vad.tersedia():
        pytest.skip("faster-whisper VAD tidak tersedia")
    wavs = sorted(glob.glob("data/reflex/*.wav"))
    if not wavs:
        pytest.skip("cache refleks belum dibangun di mesin ini")
    import soundfile as sf

    audio, sr = sf.read(wavs[0], dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    t = np.linspace(0, 1, int(len(audio) * 16000 / sr), endpoint=False)
    audio16 = np.interp(
        t, np.linspace(0, 1, len(audio), endpoint=False), audio
    ).astype("float32")
    assert arti_vad.ada_ucapan(audio16) is True


def test_vad_gagal_fail_open(monkeypatch):
    """VAD sakit tidak boleh mematikan telinga — return None (teruskan)."""
    monkeypatch.setattr(arti_vad, "_dicoba", True)
    monkeypatch.setattr(arti_vad, "_siap", False)
    assert arti_vad.ada_ucapan(np.zeros(1600, dtype="float32")) is None


# --------------------------------------------------------------------------
# Gerbang di worker — EKSEKUSI loop asli dengan VAD palsu
# --------------------------------------------------------------------------

def _jalankan(vonis_vad, tr_calls):
    cfg = {
        "desktop_audio_enabled": True,
        "desktop_audio_min_rms": 0.01,
        "desktop_audio_post_tts_cooldown_sec": 0.0,
        "desktop_audio_chunk_sec": 0.1,
    }
    keras = np.ones(800, dtype="float32") * 0.3
    antrian = [keras]

    def _rekam():
        if antrian:
            return antrian.pop(0)
        cfg["desktop_audio_enabled"] = False
        return None

    da.desktop_audio_worker(
        cfg,
        get_tts_is_playing=lambda: False,
        get_last_tts_end=lambda: None,
        is_echo_of_arti=lambda t: False,
        record_chunk=_rekam,
        transcribe_chunk=lambda a: (tr_calls.append(1) or "halo dunia nyata"),
        filter_text=lambda t: t,
        is_speech=lambda a: vonis_vad,
        sleep_sec=0.01,
    )


def test_vad_false_tidak_bayar_whisper():
    da.dialogue_ring.clear()
    tr = []
    _jalankan(False, tr)
    assert tr == [], "musik/derau tidak boleh bayar transkripsi"


def test_vad_true_dan_none_tetap_transkrip():
    for vonis in (True, None):  # None = fail-open
        da.dialogue_ring.clear()
        tr = []
        _jalankan(vonis, tr)
        assert tr == [1], f"vonis {vonis} harus tetap transkrip"


def test_config_produksi_vad_nyala():
    assert bridge.CONFIG["desktop_audio_vad_enabled"] is True
    assert float(bridge.CONFIG["desktop_audio_vad_threshold"]) == 0.5
