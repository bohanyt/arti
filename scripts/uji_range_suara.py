"""Uji dengar PELEBARAN RANGE intonasi (bukan transposisi) — lanjutan uji_pitch.

Observasi Bohan (16 Agu 2026) atas berkas A_pitch_*: menaikkan pitch lewat
resample itu "[recorded remark omitted]", bukan "[recorded remark omitted]" — kontur intonasinya
tetap sedatar pembawa berita, cuma lebih tinggi. Yang dia mau: naik-turunnya
DILEBARKAN.

Resep Praat klasik via parselmouth (PSOLA):  F0' = median + k * (F0 - median)
— nada di atas median makin naik, di bawah median makin turun, titik tengah
tidak bergeser. k = 1.0 berarti tidak berubah.

Prasyarat: jalankan scripts/uji_pitch_suara.py dulu (butuh baseline
A_pitch_plus0_semitone.wav). Paket: praat-parselmouth (terpasang 16 Agu).

Jalankan:  ./venv/Scripts/python.exe scripts/uji_range_suara.py
Hasil  :  dump/uji_pitch/D_*.wav
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import parselmouth
from parselmouth.praat import call

ROOT = Path(__file__).resolve().parents[1]
HASIL = ROOT / "dump" / "uji_pitch"
BASELINE = HASIL / "A_pitch_plus0_semitone.wav"


def lebarkan_range(data: np.ndarray, sr: int, k: float,
                   transpose_semitone: float = 0.0) -> np.ndarray:
    """PSOLA: lebarkan simpangan F0 sekitar median sebesar k; opsional
    transposisi ringan sesudahnya (dua kenop itu independen)."""
    snd = parselmouth.Sound(data.astype(np.float64), sampling_frequency=sr)
    # Rentang pencarian F0 75-600 Hz aman untuk suara F1.
    manip = call(snd, "To Manipulation", 0.01, 75, 600)
    tier = call(manip, "Extract pitch tier")
    median = call(snd.to_pitch(), "Get quantile", 0, 0, 0.5, "Hertz")
    call(tier, "Formula", f"{median} + (self - {median}) * {k}")
    if transpose_semitone:
        faktor = 2 ** (transpose_semitone / 12)
        call(tier, "Multiply frequencies", snd.xmin, snd.xmax, faktor)
    call([tier, manip], "Replace pitch tier")
    out = call(manip, "Get resynthesis (overlap-add)")
    return out.values[0].astype(np.float32)


def main() -> int:
    if not BASELINE.exists():
        print(f"baseline tidak ada: {BASELINE}\n"
              "jalankan dulu: ./venv/Scripts/python.exe scripts/uji_pitch_suara.py")
        return 1
    data, sr = sf.read(str(BASELINE))
    data = np.asarray(data, dtype=np.float64)
    if data.ndim > 1:
        data = data[:, 0]

    varian = [
        ("D_range_x13.wav", 1.3, 0.0),
        ("D_range_x15.wav", 1.5, 0.0),
        ("D_range_x18.wav", 1.8, 0.0),
        # kombinasi: range lebar + transposisi ringan (arah Neuro tapi hidup)
        ("D_kombo_range_x14_plus2.wav", 1.4, 2.0),
        ("D_kombo_range_x14_plus3.wav", 1.4, 3.0),
    ]
    for nama, k, st in varian:
        hasil = lebarkan_range(data, sr, k, st)
        sf.write(str(HASIL / nama), hasil, sr)
        print(f"  {nama}")

    print(f"""
SELESAI — {HASIL}

Cara dengar (bandingkan dengan A_pitch_plus0 = suara sekarang):
  D_range_x13/x15/x18        -> range makin lebar, nada dasar TETAP
  D_kombo_range_x14_plus2/3  -> range lebar + dinaikkan dikit (arah Neuro)
Kalau x18 terdengar lebay/artefak, itu batas atasnya — wajar untuk PSOLA.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
