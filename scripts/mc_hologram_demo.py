"""Purwarupa UI craft melayang — display entity VANILLA, tanpa plugin/mod.

Ide Bohan 2026-08-07: "[recorded remark omitted]". Tidak ada plugin jadi untuk itu, tapi sejak
1.19.4 Minecraft punya `item_display` dan `text_display` — entity yang
menampilkan item/teks melayang di dunia, terlihat SEMUA pemain, dan bisa
disummon lewat RCON. Jadi tidak perlu bikin UI dari nol, tidak perlu overlay
OBS, dan tidak perlu mod di klien penonton.

Diverifikasi di server uji 2026-08-07:
  * `summon item_display` & `summon text_display` -> jalan
  * isinya bisa diubah tanpa summon ulang (`data merge`) -> jalan
  * TIDAK bisa ditempelkan ke pemain (`/ride ... mount <pemain>` dijawab
    "Players can't be ridden"). Tidak masalah untuk craft: orang berdiri diam
    saat crafting, jadi hologram statis di tempatnya sudah cukup.

Skrip ini cuma untuk MELIHAT hasilnya dan menilai ukuran/tata letaknya:

    ./venv/Scripts/python.exe scripts/mc_hologram_demo.py --di CamBot
    ./venv/Scripts/python.exe scripts/mc_hologram_demo.py --di CamBot --hapus
"""

from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mc_rcon import rcon  # noqa: E402

TAG = "arti_ui"

# Contoh resep pickaxe kayu: 3 papan di baris atas, 2 stik di tengah bawah.
# None = kotak kosong.
GRID_CONTOH = [
    ["oak_planks", "oak_planks", "oak_planks"],
    [None, "stick", None],
    [None, "stick", None],
]
HASIL_CONTOH = "wooden_pickaxe"


def _display_item(x: float, y: float, z: float, item: str, skala: float) -> str:
    return (
        f'summon item_display {x:.3f} {y:.3f} {z:.3f} '
        f'{{Tags:["{TAG}"],item:{{id:"minecraft:{item}",count:1}},'
        f'billboard:"center",brightness:{{sky:15,block:15}},'
        f'transformation:{{scale:[{skala}f,{skala}f,{skala}f],'
        f'translation:[0f,0f,0f],left_rotation:[0f,0f,0f,1f],'
        f'right_rotation:[0f,0f,0f,1f]}}}}'
    )


def _display_text(x: float, y: float, z: float, teks: str) -> str:
    aman = teks.replace("\\", "").replace('"', "")
    return (
        f'summon text_display {x:.3f} {y:.3f} {z:.3f} '
        f'{{Tags:["{TAG}"],text:\'{{"text":"{aman}"}}\',billboard:"center",'
        f'background:1593835520,alignment:"center",see_through:0b,'
        f'transformation:{{scale:[0.8f,0.8f,0.8f],translation:[0f,0f,0f],'
        f'left_rotation:[0f,0f,0f,1f],right_rotation:[0f,0f,0f,1f]}}}}'
    )


def perintah_pasang(x: float, y: float, z: float, grid, hasil: str,
                    judul: str = "MEMBUAT") -> list[str]:
    """Semua perintah untuk memasang satu papan craft melayang.

    Tata letak: judul di atas, grid 3x3 di bawahnya, hasilnya di kanan grid.
    Jarak antar kotak 0,4 blok supaya rapat seperti GUI aslinya.
    """
    JARAK, SKALA = 0.4, 0.32
    cmds = [_display_text(x, y + 1.55, z, judul)]
    for baris in range(3):
        for kolom in range(3):
            item = grid[baris][kolom]
            if not item:
                continue
            cmds.append(_display_item(
                x + (kolom - 1) * JARAK, y + 1.1 - baris * JARAK, z, item, SKALA))
    # hasil: sedikit lebih besar, digeser ke kanan grid
    cmds.append(_display_item(x + 2.2 * JARAK, y + 1.1 - JARAK, z, hasil, SKALA * 1.6))
    return cmds


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--di", required=True,
                   help="username yang jadi patokan posisi hologram")
    p.add_argument("--jarak", type=float, default=2.5,
                   help="berapa blok di DEPAN kamu (default 2.5)")
    p.add_argument("--hapus", action="store_true")
    a = p.parse_args()

    if a.hapus:
        print(rcon([f"kill @e[tag={TAG}]"])[0].strip() or "dihapus")
        return 0

    # Ambil posisi pemain lewat NBT — tidak perlu plugin apa pun.
    try:
        keluar = rcon([f"data get entity {a.di} Pos"])[0]
    except Exception as e:  # noqa: BLE001
        print(f"RCON gagal: {e}")
        return 1
    # Balasan sukses: "<nama> has the following entity data: [x, y, z]".
    # Kata "Pos" TIDAK muncul di balasan — pengecekan lama selalu gagal.
    if "[" not in keluar:
        print(f"'{a.di}' tidak ketemu — sudah join server? (balasan: {keluar.strip()[:90]})")
        return 1
    angka = keluar.split("[", 1)[1].split("]", 1)[0].replace("d", "")
    x, y, z = (float(v.strip()) for v in angka.split(","))

    # Digeser ke DEPAN wajahmu — kalau tepat di posisimu, hologramnya berada
    # di dalam kepalamu sendiri dan kamu harus mundur dulu untuk melihatnya.
    try:
        rot = rcon([f"data get entity {a.di} Rotation"])[0]
        yaw = float(rot.split("[", 1)[1].split(",", 1)[0].replace("f", ""))
        rad = math.radians(yaw)
        x -= math.sin(rad) * a.jarak
        z += math.cos(rad) * a.jarak
    except Exception:  # noqa: BLE001 — gagal baca arah: taruh di tempatnya saja
        pass

    rcon([f"kill @e[tag={TAG}]"])
    cmds = perintah_pasang(x, y, z, GRID_CONTOH, HASIL_CONTOH)
    balasan = rcon(cmds)
    gagal = [b for b in balasan if "Summoned" not in b]
    print(f"{len(cmds) - len(gagal)}/{len(cmds)} bagian terpasang "
          f"{a.jarak:.1f} blok di depan {a.di}")
    for b in gagal:
        print("  GAGAL:", b.strip()[:110])
    print("\nLihat di game. Kalau ukuran/jaraknya kurang pas, bilang saja —")
    print("JARAK dan SKALA ada di atas fungsi perintah_pasang().")
    print(f"Hapus lagi: --di {a.di} --hapus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
