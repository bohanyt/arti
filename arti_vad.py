"""Gerbang ucapan telinga desktop — silero-vad v6, numpang faster-whisper.

Masalah (19 Agu 2026): telinga desktop cuma bergerbang RMS, dan musik itu
TIDAK sunyi — selama Bohan muter lagu, tiap 5 detik satu potongan terkirim
ke Whisper (log 18 Agu: "[Dengar] Музыка", subtitle Norwegia, lirik video
masak) = kuota kebuang + konteks sampah.

Solusi: VAD lokal (model silero v6 yang SUDAH dibundel faster-whisper —
tanpa torch, tanpa unduhan baru, CPU ~milidetik) menyaring potongan
sebelum dikirim. Batas yang diterima Bohan ("lagu vokal tetep lolos
gapapa"): lirik terdengar sebagai ucapan oleh VAD — lapis keduanya filter
halusinasi teks yang sudah ada + saklar runtime `dengar off`.

Fail-open: VAD tidak tersedia/meledak -> return None -> pemanggil
meneruskan seperti sebelum ada gerbang (telinga tidak boleh mati gara-gara
penyaringnya sakit).
"""

from __future__ import annotations

_dicoba = False
_siap = False


def tersedia() -> bool:
    global _dicoba, _siap
    if not _dicoba:
        _dicoba = True
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: F401

            _siap = True
        except Exception as e:  # noqa: BLE001
            print(f"[VAD] faster-whisper VAD tidak tersedia ({type(e).__name__}) "
                  "— telinga jalan tanpa gerbang ucapan")
            _siap = False
    return _siap


def ada_ucapan(audio, sr: int = 16000, threshold: float | None = None):
    """True = ada ucapan manusia; False = musik/derau (skip Whisper);
    None = VAD tak tersedia/error (fail-open, kirim seperti biasa)."""
    if not tersedia():
        return None
    try:
        import numpy as np
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        data = np.asarray(audio, dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        opsi = VadOptions(threshold=float(threshold)) if threshold else VadOptions()
        return len(get_speech_timestamps(data, opsi, sampling_rate=sr)) > 0
    except Exception as e:  # noqa: BLE001
        print(f"[VAD] error ({type(e).__name__}) — potongan diteruskan tanpa saring")
        return None
