"""Polesan suara (range + pitch) — resep kuping Bohan 16 Agu 2026.

Pemenang uji dengar: range ×1,4 + warna +2 semitone (berkas
E3_range_x14_plus2). Dua sifat yang WAJIB dijaga:

1. Kompensasi speed di bridge dan pemolesnya lewat SATU gerbang (aktif()) —
   kompensasi tanpa polesan = suara melambat DAN turun (lebih buruk dari
   tidak dipoles sama sekali).
2. Polesan tidak boleh pernah membisukan Arti: gagal apa pun -> audio asli.
"""

from __future__ import annotations

import numpy as np
import pytest

import arti_voice_dsp as dsp


def _nada(sr=48000, dur=0.6, f0=210.0):
    """Nada uji sederhana dengan F0 jelas supaya pelacak pitch PSOLA bekerja."""
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    return (0.4 * np.sin(2 * np.pi * f0 * t)).astype(np.float32), sr


def test_parselmouth_terpasang_di_venv():
    """requirements.txt memuat praat-parselmouth — kalau tes ini merah,
    polesan mati senyap di produksi (gerbang tersedia() menutup semuanya)."""
    assert dsp.tersedia() is True


def test_setelan_netral_tidak_menyentuh_audio():
    data, sr = _nada()
    hasil = dsp.poles_suara(data, sr, {"supertonic_pitch_semitone": 0,
                                       "supertonic_range_factor": 1.0})
    assert hasil is data  # bypass sejati, bukan salinan yang "kebetulan sama"


def test_config_produksi_memuat_resep_final():
    import arti_bridge as hb
    assert hb.CONFIG["supertonic_pitch_semitone"] == 2.0
    assert hb.CONFIG["supertonic_range_factor"] == 1.4


def test_pitch_memendekkan_sebesar_faktor():
    """+2 semitone -> resample memendek 2^(2/12); durasi net dikembalikan oleh
    speed sintesis yang diperlambat (diuji terpisah di gerbang bridge)."""
    data, sr = _nada()
    hasil = dsp.poles_suara(data, sr, {"supertonic_pitch_semitone": 2.0,
                                       "supertonic_range_factor": 1.0})
    rasio = len(data) / len(hasil)
    assert abs(rasio - dsp.faktor_pitch(2.0)) < 0.01


def test_range_tidak_mengubah_durasi():
    """PSOLA range murni tidak boleh menyentuh durasi — trik durasi di PSOLA
    adalah sumber artefak 'radio buzzing' yang ditemukan di uji E2."""
    data, sr = _nada()
    hasil = dsp.poles_suara(data, sr, {"supertonic_pitch_semitone": 0.0,
                                       "supertonic_range_factor": 1.4})
    assert abs(len(hasil) - len(data)) / len(data) < 0.02


def test_gagal_poles_mengembalikan_audio_asli(monkeypatch):
    data, sr = _nada()
    monkeypatch.setattr(dsp, "_lebarkan_range",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    hasil = dsp.poles_suara(data, sr, {"supertonic_range_factor": 1.4})
    assert hasil is data


def test_bridge_kompensasi_lewat_gerbang_yang_sama():
    """Sumber: kompensasi speed WAJIB di balik arti_voice_dsp.aktif() —
    bukan cek config mentah — supaya parselmouth hilang berarti kompensasi
    ikut mati, bukan cuma polesannya."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "arti_bridge.py"
           ).read_text(encoding="utf-8")
    anchor = "_speed = _speed / arti_voice_dsp.faktor_pitch("
    assert src.count(anchor) == 1
    blok = src.split("_speed = CONFIG[\"supertonic_speed\"]")[1].split("req = {")[0]
    assert "arti_voice_dsp.aktif(CONFIG)" in blok


def test_faktor_pitch():
    assert dsp.faktor_pitch(0) == 1.0
    assert abs(dsp.faktor_pitch(2) - 1.1225) < 0.001
    assert abs(dsp.faktor_pitch(12) - 2.0) < 1e-9


def test_aktif_butuh_setelan_dan_parselmouth():
    assert dsp.aktif({"supertonic_pitch_semitone": 0, "supertonic_range_factor": 1.0}) is False
    assert dsp.aktif({"supertonic_pitch_semitone": 2.0}) is True


def test_none_di_config_dianggap_netral():
    """config_local bisa menaruh None; jangan meledak, jangan aktif."""
    assert dsp.aktif({"supertonic_pitch_semitone": None,
                      "supertonic_range_factor": None}) is False
