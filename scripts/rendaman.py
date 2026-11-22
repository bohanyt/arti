"""Rendaman: protokol uji bertahan-hidup 10 menit yang SERAGAM.

Dibuat 2026-08-08 sesudah empat rendaman ad-hoc. Angkanya (3/2/3/2 mati) cuma
bisa dibandingkan karena kebetulan perlengkapan dan titiknya kutulis sama tiap
kali — protokol ini mengunci kesamaan itu jadi kode supaya A/B berikutnya tidak
bergantung pada disiplin manual.

Protokol: malam (13500), difficulty normal, keepInventory false, titik tetap
(9 82 -32), bekal iron sword + 64 cobblestone TANPA makanan, perintah `roam`,
lalu dibiarkan N menit. Semua angka dihitung dari log harness — jalankan
`scripts/tes_minecraft_manual.py` dulu dan beri path lognya.

    ./venv/Scripts/python.exe scripts/rendaman.py --log <path_log_harness>
    ./venv/Scripts/python.exe scripts/rendaman.py --log ... --menit 10

Keluaran: ringkasan mati/refleks/kabur/nyangkut/error + baris pembanding
rendaman terdahulu. TIDAK menyalakan server/harness sendiri — dua-duanya
harus sudah hidup (disengaja: skrip ukur tidak boleh ikut mengatur lingkungan
yang dia ukur).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from mc_rcon import rcon  # noqa: E402

BOT = "arti_berarti"
# Titik & bekal DIBEKUKAN. Mengubahnya = angka tidak lagi sebanding dengan
# baseline 3/2/3/2 — kalau memang perlu diubah, catat sebagai protokol v2.
TITIK = "9 82 -32"
BEKAL = ["minecraft:iron_sword 1", "minecraft:cobblestone 64"]

# Pembanding terdahulu, protokol identik (2026-08-08).
BASELINE = [
    ("tanpa refleks", 3),
    ("refleks terkunci 'kabur'", 2),
    ("refleks ambang 7", 3),
    ("refleks ambang 12", 2),
]


def siapkan() -> None:
    rcon([
        "difficulty normal", "gamerule keepInventory false", "time set 13500",
        "weather clear",
        f"kill @e[type=zombie,distance=..80]", f"kill @e[type=skeleton,distance=..80]",
        f"kill @e[type=spider,distance=..80]", f"kill @e[type=creeper,distance=..80]",
        f"attribute {BOT} minecraft:max_health base set 20",
        f"effect clear {BOT}",
        f"tp {BOT} {TITIK}", f"clear {BOT}",
    ] + [f"give {BOT} {b}" for b in BEKAL])


def hitung(teks: str) -> dict[str, int]:
    def n(pola: str) -> int:
        return len(re.findall(pola, teks))
    return {
        "mati": n(r"\[event\] death"),
        "refleks_tembok": n(r"'tembok_panah'"),
        "refleks_berlindung": n(r"'berlindung'"),
        "refleks_makan": n(r"'jenis': 'makan'"),
        "tembok_done": n(r"tembok_done"),
        "kabur": n(r"flee_start"),
        "nyangkut": n(r"stuck_timeout"),
        "swim_start": n(r"'swim_start'"),
        "error": n(r"(?i)error|exception"),
    }


def utama() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="log harness tes_minecraft_manual.py")
    ap.add_argument("--menit", type=float, default=10.0)
    ap.add_argument("--label", default="rendaman")
    a = ap.parse_args()
    log = pathlib.Path(a.log)
    if not log.is_file():
        print(f"log tidak ada: {log}")
        return 1

    siapkan()
    suruh = pathlib.Path(os.environ.get("CLAUDE_JOB_DIR", ".")) / "tmp" / "perintah.txt"
    suruh.write_text(json.dumps({"cmd": "roam"}), encoding="utf-8")
    awal = len(log.read_text(encoding="utf-8", errors="replace"))
    print(f"[{a.label}] mulai — {a.menit:g} menit, protokol beku (malam, {TITIK}, "
          "iron sword + 64 cobblestone, tanpa makanan)")

    habis = time.time() + a.menit * 60
    while time.time() < habis:
        time.sleep(15)

    angka = hitung(log.read_text(encoding="utf-8", errors="replace")[awal:])
    print(f"\n=== {a.label}: {a.menit:g} menit ===")
    for k, v in angka.items():
        print(f"  {k:20} {v}")
    print("\n=== pembanding (protokol identik) ===")
    for nama, mati in BASELINE:
        print(f"  {nama:28} MATI {mati}")
    print(f"  {a.label:28} MATI {angka['mati']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(utama())
