"""Uji dengar pitch & panjang-chunk Supertone — TANPA menyentuh produksi.

Latar: suara F1 terdengar datar. Supertonic TIDAK punya parameter pitch (kenop
cuma voice/speed/steps — dan F1 + speed 1.1 + steps SUDAH dites, TERKUNCI,
jangan diutak-atik). Jalan yang tersisa = post-process, seperti suara VTuber AI
lain yang di-pitch-up.

Triknya tanpa dependensi baru: sintesis di speed (1.1 / faktor) lalu resample
naik (faktor) -> pitch naik, durasi bersih kembali setara speed 1.1 yang
sudah pas di kuping Bohan.

Sekalian menguji hipotesis Bohan: jawaban 3 kalimat disintesis SEKALIGUS
terdengar lebih hidup daripada per kalimat (kontur intonasi Supertonic
dibentuk dari konteks teks; potongan pendek = prosodi reset tiap potong).

Jalankan:  ./venv/Scripts/python.exe scripts/uji_pitch_suara.py
Hasil  :  dump/uji_pitch/*.wav  — puter, pilih pakai kuping.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
HASIL = ROOT / "dump" / "uji_pitch"
PY312 = ROOT / "venv312" / "Scripts" / "python.exe"
ENGINE = ROOT / "supertone_engine.py"

# Kalimat khas Arti — satu jawaban utuh 3 kalimat + satu celetukan pendek.
TIGA_KALIMAT = (
    "Eh, jalan ke tambang tadi licin banget. Aku hampir jatuh dua kali "
    "gara-gara batu kecil. Besok aku bawa obor lebih banyak deh, biar "
    "kelihatan jalannya."
)
CELETUKAN = "Hah? Ya kali aku takut sama zombie, aku kan cuma takut skeleton."

# Faktor pitch: 2^(semitone/12). 0 = baseline produksi persis.
SEMITONE = [0, 2, 3, 4]
SPEED_PRODUKSI = 1.1  # TERKUNCI oleh Bohan — net durasi semua varian setara ini


def kirim(proc, req: dict) -> dict:
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    while True:
        baris = proc.stdout.readline()
        if not baris:
            raise RuntimeError("engine mati sebelum menjawab")
        try:
            resp = json.loads(baris)
        except json.JSONDecodeError:
            continue  # baris log engine, bukan respons protokol
        if resp.get("id") == req["id"]:
            return resp


def sintesis(proc, rid: str, text: str, speed: float) -> tuple[np.ndarray, int]:
    resp = kirim(proc, {
        "v": 1, "id": rid, "type": "synthesize",
        "text": text, "voice": "F1", "speed": speed,
        "lang": "id", "total_steps": 10,
    })
    if not resp.get("ok"):
        raise RuntimeError(f"sintesis gagal: {resp}")
    # Engine memangkas temp_*.wav (sisakan 3) — salin SEGERA.
    data, sr = sf.read(resp["wav_path"])
    return np.asarray(data, dtype=np.float32), sr


def naikkan_pitch(data: np.ndarray, faktor: float) -> np.ndarray:
    """Resample memendek = pitch naik. Durasi sudah dikompensasi di speed synth."""
    if faktor == 1.0:
        return data
    n_baru = int(len(data) / faktor)
    xs_lama = np.linspace(0.0, 1.0, len(data))
    xs_baru = np.linspace(0.0, 1.0, n_baru)
    return np.interp(xs_baru, xs_lama, data).astype(np.float32)


def main() -> int:
    if not PY312.exists():
        print(f"venv312 tidak ketemu: {PY312}")
        return 1
    HASIL.mkdir(parents=True, exist_ok=True)

    print("menyalakan engine Supertone (load model ~beberapa detik)...")
    proc = subprocess.Popen(
        [str(PY312), str(ENGINE)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    try:
        kirim(proc, {"v": 1, "id": "ping0", "type": "ping"})
        print("engine siap.\n")

        # --- A. Varian pitch (jawaban 3 kalimat, sintesis SEKALIGUS) -------- #
        for st in SEMITONE:
            faktor = 2 ** (st / 12)
            speed = SPEED_PRODUKSI / faktor  # kompensasi: net durasi = 1.1
            t0 = time.perf_counter()
            data, sr = sintesis(proc, f"pitch{st}", TIGA_KALIMAT, speed)
            data = naikkan_pitch(data, faktor)
            nama = HASIL / f"A_pitch_plus{st}_semitone.wav"
            sf.write(str(nama), data, sr)
            print(f"  {nama.name:34} ({time.perf_counter()-t0:.1f}s)")

        # --- B. Celetukan pendek, varian pitch --------------------------- #
        for st in (0, 3):
            faktor = 2 ** (st / 12)
            data, sr = sintesis(proc, f"cel{st}", CELETUKAN, SPEED_PRODUKSI / faktor)
            data = naikkan_pitch(data, faktor)
            nama = HASIL / f"B_celetukan_plus{st}.wav"
            sf.write(str(nama), data, sr)
            print(f"  {nama.name}")

        # --- C. Hipotesis Bohan: utuh vs per kalimat ---------------------- #
        # C1 = tiga kalimat SEKALIGUS (sudah ada: A_pitch_plus0). C2 = per
        # kalimat lalu disambung — persis yang dilakukan bridge sekarang.
        potongan = []
        sr = 48000
        for i, kal in enumerate(
            [k.strip() + "." for k in TIGA_KALIMAT.split(". ") if k.strip()]
        ):
            data, sr = sintesis(proc, f"chunk{i}", kal.rstrip(".") + ".", SPEED_PRODUKSI)
            potongan.append(data)
            potongan.append(np.zeros(int(sr * 0.12), dtype=np.float32))
        sf.write(str(HASIL / "C_per_kalimat_disambung.wav"),
                 np.concatenate(potongan), sr)
        shutil.copy(str(HASIL / "A_pitch_plus0_semitone.wav"),
                    str(HASIL / "C_utuh_sekali_sintesis.wav"))
        print("  C_per_kalimat_disambung.wav vs C_utuh_sekali_sintesis.wav")

        kirim_shutdown = {"v": 1, "id": "bye", "type": "shutdown"}
        proc.stdin.write(json.dumps(kirim_shutdown) + "\n")
        proc.stdin.flush()
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    print(f"""
SELESAI — {HASIL}

Cara dengar:
  A_pitch_plus0/2/3/4  -> pilih pitch (0 = suara sekarang persis;
                          Neuro-sama kira-kira setara +4)
  B_celetukan_*        -> cek pitch di kalimat pendek/emosional
  C_utuh vs C_per_kalimat -> bukti hipotesismu soal chunk: sama-sama +0,
                          bedanya cuma disintesis sekaligus atau dipotong-potong

Semua varian net-durasinya setara speed 1.1 (setelanmu tidak berubah).
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
