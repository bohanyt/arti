"""Spike GO/NO-GO: loopback default output -> rekam N dtk -> RMS -> transkrip lokal.

Jalankan SAMBIL memutar video/audio apa pun:
    python scripts/spike_desktop_loopback.py [detik] [nama_device]
Tanpa nama_device = loopback default speaker (ikut yang kamu dengar).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    device_name = sys.argv[2] if len(sys.argv) > 2 else ""

    import soundcard as sc

    if device_name:
        mic = sc.get_microphone(device_name, include_loopback=True)
    else:
        spk = sc.default_speaker()
        print(f"Default speaker: {spk.name}")
        mic = sc.get_microphone(str(spk.name), include_loopback=True)
    print(f"Capture via: {mic.name} (loopback={getattr(mic, 'isloopback', '?')})")

    sr = 16000
    t0 = time.time()
    with mic.recorder(samplerate=sr, channels=1) as rec:
        data = rec.record(numframes=int(seconds * sr))
    took = time.time() - t0
    mono = np.asarray(data, dtype=np.float32).reshape(-1)
    rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
    print(f"Rekam {seconds:.0f}s selesai ({took:.1f}s wall) — RMS={rms:.5f}")

    wav_path = Path(tempfile.gettempdir()) / "spike_desktop_loopback.wav"
    import soundfile as sf

    sf.write(str(wav_path), mono, sr)
    print(f"WAV: {wav_path}")

    if rms < 0.001:
        print("RMS nyaris nol — tidak ada audio yang terdengar. Putar video lalu ulangi.")
        return 1

    print("Transkrip lokal (faster-whisper small)...")
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel("small", device="cuda", compute_type="float16")
    except Exception:
        model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(wav_path), vad_filter=True)
    print(f"Bahasa terdeteksi: {info.language} (p={info.language_probability:.2f})")
    text = " ".join(s.text.strip() for s in segments)
    print(f"TEKS: {text!r}")
    return 0 if text.strip() else 2


if __name__ == "__main__":
    raise SystemExit(main())
