"""Pemoles suara Arti — pelebaran range intonasi + kenaikan pitch berwarna.

Lahir 16 Agu 2026 dari sesi uji dengar streamer (dump/uji_pitch, skrip
scripts/uji_pitch_suara.py + uji_range_suara.py). Latar: suara F1 Supertone
"kayak pembawa berita" — datar; penonton bilang robotik. Supertonic TIDAK
punya parameter pitch/prosodi, dan kenop yang ada (voice/speed/steps) SUDAH
dites streamer sampai terkunci. Jalan yang tersisa: post-process.

Resep FINAL pilihan kuping streamer (berkas E3_range_x14_plus2):

1. RANGE ×1,4 — PSOLA murni pitch: F0' = median + 1,4 × (F0 − median).
   Naik-turun dilebarkan, nada tengah diam, DURASI TIDAK DISENTUH
   (menyentuh durasi di PSOLA = artefak "radio buzzing", terbukti di uji E2).
2. WARNA +2 SEMITONE — resample sinc presisi-50 Praat. Resample menggeser
   formant juga ("warna ikut naik") — justru itu yang dipilih kupingnya;
   transposisi PSOLA murni (F0 saja) TIDAK terasa naik (uji E_FINAL).
   Durasi dikompensasi DI SINTESIS: bridge meminta Supertone bicara di
   speed/faktor, resample mengembalikannya — net tetap speed setelan streamer.

Ongkos terukur: ~23 ms (kalimat pendek) / ~92 ms (13,7 dtk audio) — tidak
terasa dibanding sintesis ~500-800 ms.

Butuh praat-parselmouth (wheel murni + numpy). Kalau tidak terpasang, semua
fungsi jatuh ke perilaku lama TANPA merusak jalur suara — dan kompensasi
speed di bridge ikut mati lewat gerbang `tersedia()` yang sama, supaya tidak
pernah terjadi "disintesis lambat tapi tidak di-resample balik" (= suara
melambat DAN turun).
"""

from __future__ import annotations

import numpy as np

_parselmouth = None
_parselmouth_dicoba = False


def tersedia() -> bool:
    """Parselmouth siap? Dicek sekali, di-cache. Gerbang untuk SEMUA jalur:
    kompensasi speed di bridge memakai gerbang yang sama dengan pemrosesan."""
    global _parselmouth, _parselmouth_dicoba
    if not _parselmouth_dicoba:
        _parselmouth_dicoba = True
        try:
            import parselmouth  # noqa: PLC0415
            _parselmouth = parselmouth
        except Exception as e:  # noqa: BLE001
            print(f"[VoiceDSP] parselmouth tidak tersedia ({type(e).__name__}) "
                  "— suara tampil tanpa polesan")
            _parselmouth = None
    return _parselmouth is not None


def aktif(config: dict) -> bool:
    """Polesan menyala? (setelan non-netral DAN parselmouth ada)."""
    semitone = float(config.get("supertonic_pitch_semitone", 0.0) or 0.0)
    rng = float(config.get("supertonic_range_factor", 1.0) or 1.0)
    return (semitone != 0.0 or rng != 1.0) and tersedia()


def faktor_pitch(semitone: float) -> float:
    return float(2.0 ** (float(semitone) / 12.0))


def _lebarkan_range(data: np.ndarray, sr: int, k: float):
    from parselmouth.praat import call  # noqa: PLC0415
    snd = _parselmouth.Sound(data.astype(np.float64), sampling_frequency=sr)
    manip = call(snd, "To Manipulation", 0.01, 75, 600)
    tier = call(manip, "Extract pitch tier")
    median = call(snd.to_pitch(), "Get quantile", 0, 0, 0.5, "Hertz")
    call(tier, "Formula", f"{median} + (self - {median}) * {k}")
    call([tier, manip], "Replace pitch tier")
    return call(manip, "Get resynthesis (overlap-add)").values[0]


def _naikkan_warna(data: np.ndarray, sr: int, faktor: float):
    """Pitch+formant naik lewat sinc presisi-50 (interp linear = buzzing)."""
    from parselmouth.praat import call  # noqa: PLC0415
    snd = _parselmouth.Sound(
        data.astype(np.float64), sampling_frequency=int(sr * faktor)
    )
    return call(snd, "Resample", sr, 50).values[0]


def poles_suara(data: np.ndarray, sr: int, config: dict) -> np.ndarray:
    """Terapkan resep final pada WAV keluaran Supertone. AMAN: kegagalan apa
    pun mengembalikan audio apa adanya — polesan tidak boleh membisukan Arti."""
    if not aktif(config):
        return data
    try:
        mono = np.asarray(data, dtype=np.float64)
        if mono.ndim > 1:
            mono = mono[:, 0]
        k = float(config.get("supertonic_range_factor", 1.0) or 1.0)
        st = float(config.get("supertonic_pitch_semitone", 0.0) or 0.0)
        if k != 1.0:
            mono = _lebarkan_range(mono, sr, k)
        if st != 0.0:
            mono = _naikkan_warna(mono, sr, faktor_pitch(st))
        return mono.astype(np.float32)
    except Exception as e:  # noqa: BLE001
        print(f"[VoiceDSP] polesan gagal ({type(e).__name__}: {e}) — "
              "pakai audio asli")
        return data
