# -*- coding: utf-8 -*-
"""Probe hot-swap perangkat audio (colok perangkat audio saat bridge nyala).

Pertanyaan yang dijawab dengan BUKTI, bukan tebakan:
  1. Apakah PortAudio yang SUDAH hidup melihat perangkat baru tanpa restart?
  2. Apakah NOMOR device bergeser saat perangkat dicolok/dicabut?
  3. Apakah `sd._terminate()/_initialize()` membuat daftarnya segar lagi?
  4. Apakah device default Windows (in/out) ikut berpindah?

Jawaban tiga pertanyaan itu menentukan bentuk hot-swap yang benar:
kalau daftar TIDAK segar tanpa re-init -> hot-swap wajib re-init PortAudio
(dan itu berarti semua stream harus ditutup dulu — desainnya beda).

Pakai: ./venv/Scripts/python.exe scripts/probe_hotswap_audio.py [--detik 120]
Lalu COLOK/CABUT examplebuds saat probe berjalan. Aman dijalankan saat bridge
live: hanya membaca daftar perangkat, tidak membuka stream apa pun.
"""

from __future__ import annotations

import argparse
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")


def _potret(sd) -> dict:
    dev = sd.query_devices()
    return {
        "n": len(dev),
        "nama": [f"{i}:{d['name'][:38]}" for i, d in enumerate(dev)],
        "default_in": sd.default.device[0],
        "default_out": sd.default.device[1],
    }


def _ringkas(p: dict) -> str:
    try:
        import sounddevice as sd

        din = sd.query_devices(p["default_in"])["name"][:28] if p["default_in"] is not None else "-"
        dout = sd.query_devices(p["default_out"])["name"][:28] if p["default_out"] is not None else "-"
    except Exception:  # noqa: BLE001
        din = dout = "?"
    return f"{p['n']} device | default in={p['default_in']} ({din}) out={p['default_out']} ({dout})"


def _beda(lama: dict, baru: dict) -> list[str]:
    out = []
    if lama["n"] != baru["n"]:
        out.append(f"JUMLAH device {lama['n']} -> {baru['n']}")
    if lama["default_in"] != baru["default_in"]:
        out.append(f"default INPUT {lama['default_in']} -> {baru['default_in']}")
    if lama["default_out"] != baru["default_out"]:
        out.append(f"default OUTPUT {lama['default_out']} -> {baru['default_out']}")
    hilang = [x for x in lama["nama"] if x not in baru["nama"]]
    muncul = [x for x in baru["nama"] if x not in lama["nama"]]
    for x in hilang[:6]:
        out.append(f"  - hilang/geser: {x}")
    for x in muncul[:6]:
        out.append(f"  + muncul/geser: {x}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--detik", type=float, default=120.0)
    ap.add_argument("--jeda", type=float, default=2.0)
    args = ap.parse_args()

    import sounddevice as sd

    awal = _potret(sd)
    print("=" * 70)
    print("PROBE HOT-SWAP AUDIO — COLOK / CABUT EXAMPLEBUDS SEKARANG")
    print("=" * 70)
    print(f"[awal    ] {_ringkas(awal)}")
    print(f"[info    ] pantau {args.detik:.0f} dtk, cek tiap {args.jeda:.0f} dtk\n")

    terakhir = awal
    perubahan_tanpa_reinit = 0
    t0 = time.time()
    while time.time() - t0 < args.detik:
        time.sleep(args.jeda)
        sekarang = _potret(sd)
        d = _beda(terakhir, sekarang)
        if d:
            perubahan_tanpa_reinit += 1
            print(f"[{time.time() - t0:5.0f}s] PORTAUDIO LAMA MELIHAT PERUBAHAN:")
            for baris in d:
                print(f"          {baris}")
            terakhir = sekarang

    print("\n" + "=" * 70)
    print("HASIL 1 — PortAudio yang sudah hidup:")
    if perubahan_tanpa_reinit:
        print(f"  MELIHAT perubahan ({perubahan_tanpa_reinit}x) tanpa perlu restart.")
        print("  -> hot-swap cukup: resolve ulang device BY NAME per pemakaian.")
    else:
        print("  BUTA — daftar perangkat beku sejak proses start.")
        print("  -> hot-swap WAJIB re-init PortAudio (tutup stream dulu).")

    print("\nHASIL 2 — sesudah sd._terminate() + sd._initialize():")
    try:
        sd._terminate()
        sd._initialize()
        segar = _potret(sd)
        print(f"  {_ringkas(segar)}")
        d = _beda(terakhir, segar)
        if d:
            print("  RE-INIT MENYEGARKAN daftar:")
            for baris in d:
                print(f"    {baris}")
        else:
            print("  (sama dengan sebelum re-init)")
    except Exception as e:  # noqa: BLE001
        print(f"  re-init GAGAL: {type(e).__name__}: {e}")

    print("\nHASIL 3 — dibanding potret paling awal:")
    for baris in _beda(awal, _potret(sd)) or ["  (tidak ada perubahan sama sekali —"
                                              " examplebuds belum sempat dicolok?)"]:
        print(f"  {baris}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
