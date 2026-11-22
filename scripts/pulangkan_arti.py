#!/usr/bin/env python3
"""Pulangkan Arti dari arena UJI ke dunia yang bisa dimainkan.

Kenapa perlu: posisi pemain tersimpan di playerdata server, jadi bot yang
terakhir logout di arena uji (platform batu melayang, mis. x3588 y150 z3010
dari uji portal_cor 11 Agu) akan JOIN DI SANA lagi — dan di sana tidak ada
pohon, tanah, atau apa pun. Terukur di sesi tes Bohan 11 Agu 21.38: Arti
"terkurung" 27x, nol pungut kayu, nol rakit alat, karena dia berdiri di
pelat 25x25 di tengah langit. Bersihkan blok arena saja TIDAK cukup.

Yang dilakukan:
  1. menunggu `arti_berarti` online (bridge dinyalakan / bot join),
  2. `spreadplayers` ke permukaan aman dekat spawn dunia (bukan udara),
  3. me-restart proses bot supaya `homePos` (pusat jelajah & tujuan
     `pulang`) ikut pindah — tanpa ini dia mencoba jalan balik 3,5 km.

Pakai:
    ./venv/Scripts/python.exe scripts/pulangkan_arti.py            # tunggu maks 20 mnt
    ./venv/Scripts/python.exe scripts/pulangkan_arti.py --sekarang # gagal kalau offline
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scripts.mc_rcon import rcon  # noqa: E402

NAMA = os.environ.get("ARTI_BOT_NAME", "arti_berarti")
TUNGGU_MAKS = 1200.0
JAUH_DARI_SPAWN = 40      # spreadplayers maxRange


def online() -> bool:
    try:
        return NAMA in (rcon(["list"])[0] or "")
    except Exception:
        return False


def restart_bot() -> None:
    """Kill proses bot Node; runner bridge menghidupkannya kembali (dan
    membaca ulang bot.js + menyetel homePos di posisi baru)."""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "Where-Object { $_.CommandLine -match 'mc-bot' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def main() -> int:
    if "--sekarang" in sys.argv and not online():
        print(f"[Pulang] {NAMA} tidak online — jalankan tanpa --sekarang "
              "untuk menunggu.")
        return 1
    batas = time.time() + TUNGGU_MAKS
    print(f"[Pulang] menunggu {NAMA} online…", flush=True)
    while not online():
        if time.time() > batas:
            print("[Pulang] menyerah — dia tidak pernah join.")
            return 1
        time.sleep(5.0)
    time.sleep(4.0)   # beri waktu chunk & entity termuat
    sebelum = rcon([f"data get entity {NAMA} Pos"])[0]
    print(f"[Pulang] sebelum: {sebelum[:110]}")
    print(rcon([f"spreadplayers 0 0 8 {JAUH_DARI_SPAWN} false {NAMA}"])[0][:110])
    time.sleep(1.0)
    print(f"[Pulang] sesudah: {rcon([f'data get entity {NAMA} Pos'])[0][:110]}")
    restart_bot()
    print("[Pulang] bot di-restart — homePos akan diset di dunia normal. "
          "Kamera mengunci ulang sendiri.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
