"""POV Arti dengan grafis Minecraft SUNGGUHAN — lewat klien spectator.

Kenapa ada dua jalur POV:

  * `mc-bot/pov.js` (prismarine-viewer) — otomatis, nol setup, ringan, tapi
    renderernya sederhana: tanpa kabut, tanpa smooth lighting, tanpa bayangan.
    Bohan 2026-08-06: "[recorded remark omitted]". Itu memang atapnya.
  * Skrip ini — kamu jalankan klien Minecraft asli (profil Sodium yang sudah
    ada), lalu skrip ini menyuruhnya mengunci kamera ke mata Arti. Yang
    terlihat = Minecraft beneran, lengkap dengan mod/shader-mu. OBS tinggal
    menangkap jendelanya.

Servernya offline-mode, jadi klien kedua boleh memakai nama apa pun — tidak
perlu akun tambahan.

CARA PAKAI
    1. Arti sudah masuk dunia (`mc join` di console bridge).
    2. Jalankan klien Minecraft dengan USERNAME BERBEDA, join ke localhost.
    3. ./venv/Scripts/python.exe scripts/mc_pov_spectate.py --penonton CamBot

    Keluar dari mode kamera:
       ./venv/Scripts/python.exe scripts/mc_pov_spectate.py --penonton CamBot --lepas

CATATAN
    `spectate` HARUS dijalankan atas nama si penonton (`execute as ...`) —
    kalau dikirim polos dari console, server menolak karena console bukan
    entitas yang bisa menonton.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mc_rcon import rcon  # noqa: E402


def _nama_bot_default() -> str:
    """Ambil nama bot dari config_local kalau ada — jangan paksa Bohan ingat."""
    akar = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(akar, "config_local.json"), encoding="utf-8") as f:
            return str(json.load(f).get("minecraft_bot_name") or "Arti")
    except (OSError, ValueError):
        return "Arti"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--penonton", required=True,
                   help="username klien Minecraft yang jadi kamera")
    p.add_argument("--target", default=_nama_bot_default(),
                   help="username bot Arti (default dari config_local)")
    p.add_argument("--lepas", action="store_true",
                   help="lepas kamera dan kembalikan ke survival")
    a = p.parse_args()

    if a.penonton == a.target:
        print("Penonton dan target tidak boleh sama — pakai username berbeda.")
        return 2

    if a.lepas:
        perintah = [
            f"execute as {a.penonton} run spectate",
            f"gamemode survival {a.penonton}",
        ]
    else:
        perintah = [
            # Spectator dulu: `spectate` diabaikan diam-diam kalau pemainnya
            # belum dalam mode itu.
            f"gamemode spectator {a.penonton}",
            f"execute as {a.penonton} run spectate {a.target}",
        ]

    try:
        balasan = rcon(perintah)
    except FileNotFoundError:
        print("Password RCON tidak ketemu "
              "(<folder server>/.rcon_password, folder server: ARTI_MC_SERVER_DIR).")
        return 1
    except (OSError, PermissionError) as e:
        print(f"RCON gagal: {type(e).__name__}: {e}")
        print("Server hidup? enable-rcon=true di server.properties?")
        return 1

    gagal = False
    for cmd, out in zip(perintah, balasan):
        teks = (out or "").strip()
        print(f"> {cmd}\n  {teks or '(tanpa balasan)'}")
        # Server membalas kalimat error dengan status 200 yang sama — satu-
        # satunya penanda adalah isinya.
        if "No player was found" in teks or "Unknown" in teks:
            gagal = True

    if gagal:
        print("\nAda yang tidak ketemu. Cek: klien penonton sudah JOIN? "
              "Nama persis (huruf besar-kecil berpengaruh)? Arti sudah masuk?")
        return 1

    if a.lepas:
        print("\nKamera dilepas.")
    else:
        print(f"\nKamera terkunci ke {a.target}. Di OBS: Window/Game Capture "
              "ke jendela Minecraft itu.")
        print("Kalau kamera lepas sendiri (Arti ganti dimensi/mati), "
              "jalankan skrip ini lagi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
