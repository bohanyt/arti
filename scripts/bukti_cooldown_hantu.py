"""Bukti terhitung: pesan chat yang DIBUANG tetap menstempel cooldown.

Dipakai sebagai dasar TUGAS 1A (docs/handoff/2026-08-28-TUGAS-1-ANTREAN-CHAT.md).

Akar bugnya, arti_bridge.py:5936 —

    queue_voice_trigger(label, trigger_type="yt_chat", viewer_name=viewer)
    _last_yt_trigger_by_viewer[viewer] = current_time
    last_chat_trigger_time = current_time

`queue_voice_trigger` bisa MEMBUANG pesannya diam-diam (baris 1530, saat
_brain_busy atau tts_is_playing) dan tidak mengembalikan apa pun, jadi kedua
stempel di bawahnya tetap terpasang. Penonton lalu didiamkan 45 detik karena
Arti "sudah menjawabnya" — padahal tidak pernah.

Cara mendeteksinya dari log: cari "kena cooldown pribadi" untuk penonton X
di mana kejadian TERAKHIR untuk X adalah "Skip trigger", bukan "Antri jawab".

    ./venv/Scripts/python.exe scripts/bukti_cooldown_hantu.py
    ./venv/Scripts/python.exe scripts/bukti_cooldown_hantu.py path/ke/log.log

Hasil di log siaran 27-28 Agu 2026 (9 jam): 105 dari 251 = 42%.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# paksa_stdout_utf8() dipanggil saat modul ini diimpor — jangan membungkus
# stdout lagi di sini, pembungkus ganda menutup yang pertama lewat GC.
from analisa_log_siaran import _HANDLE, log_terbaru  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", help="path log; kosong = terbaru")
    a = ap.parse_args()
    path = a.log or log_terbaru()
    with open(path, encoding="utf-8", errors="replace") as fh:
        L = fh.read().splitlines()

    rx_jawab = re.compile(r"\[YT Chat\] Antri jawab (" + _HANDLE + r")")
    rx_drop = re.compile(
        r"Skip trigger \(yt_chat\).*?(?:Viewer|bernama) (" + _HANDLE + r")")
    rx_cd = re.compile(r"(" + _HANDLE + r") kena cooldown pribadi")

    drop_terakhir: dict[str, int] = {}
    jawab_terakhir: dict[str, int] = {}
    kasus: list[tuple[str, int, int]] = []
    total_cd = 0

    for i, baris in enumerate(L):
        m = rx_jawab.search(baris)
        if m:
            jawab_terakhir[m.group(1)] = i
            continue
        m = rx_drop.search(baris)
        if m:
            drop_terakhir[m.group(1)] = i
            continue
        m = rx_cd.search(baris)
        if m:
            total_cd += 1
            v = m.group(1)
            # Kejadian terakhir untuk penonton ini adalah DROP, bukan jawaban
            # -> cooldown yang sedang menghukumnya berasal dari pesan yang
            #    tidak pernah dijawab.
            if drop_terakhir.get(v, -1) > jawab_terakhir.get(v, -1):
                kasus.append((v, drop_terakhir[v], i))

    print(f"  berkas: {path}\n")
    print(f"  cooldown yang dipicu pesan DIBUANG : {len(kasus)}")
    print(f"  total 'kena cooldown pribadi'      : {total_cd}")
    if total_cd:
        print(f"  porsi                              : {len(kasus)/total_cd*100:.0f}%")

    print("\n  contoh (baris log asli):")
    for v, d, i in kasus[:5]:
        print(f"\n  {v}")
        print(f"    {d}: {L[d].strip()[:104]}")
        print(f"    {i}: {L[i].strip()[:104]}")

    baru = [k for k in kasus if "bernama" in L[k[1]]]
    if baru:
        print(f"\n  DI ANTARANYA {len(baru)} adalah PENONTON BARU yang sapaan"
              " pertamanya dibuang,")
        print("  lalu didiamkan oleh cooldown untuk jawaban yang tidak pernah ada:")
        for v, d, _ in baru[:5]:
            print(f"    {v}")

    print("\n  Perbaikan: queue_voice_trigger mengembalikan bool; stempel"
          " cooldown\n  hanya dipasang kalau trigger benar-benar diterima."
          "  Lihat TUGAS 1A.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
