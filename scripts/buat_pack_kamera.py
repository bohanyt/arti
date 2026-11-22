"""Bangun resource pack UI untuk klien kamera siaran (instance launcher contoh "CamInstance").

Momen "[recorded remark omitted]" memakai `invsee`, dan itu membuka layar KONTAINER, bukan layar
inventory. Dua bagian yang tidak diminta ikut tergambar dan tidak bisa
dihilangkan dari sisi server:

  1. Kotak "Inventory" di bawah = inventory KAMERA sendiri. Minecraft selalu
     menggambarnya di setiap layar kontainer.
  2. Tiga belas slot yang tidak pernah terisi. Jendelanya tetap 9x6 = 54 slot
     berapa pun isi templatenya (memperkecilnya bikin InvSee++ melempar
     ArrayIndexOutOfBounds dan jendelanya gagal terbuka), sedangkan Arti cuma
     punya 41 slot nyata: 36 inventory + 4 armor + 1 offhand. Sisanya kursor,
     saddle, body, dan grid crafting — semuanya selalu kosong.

Keduanya diselesaikan di KLIEN, bukan server: pack ini menimpa tekstur
`generic_54.png` dan mengosongkan label teks `container.inventory`.

Kenapa ini ada sebagai skrip, bukan zip yang disimpan: zip-nya cuma tinggal di
disk mesin uji dan hilang kalau instance-nya disetel ulang. Skrip ini
membangunnya kembali dari jar Minecraft yang memang sudah ada di sana.

    ./venv/Scripts/python.exe scripts/buat_pack_kamera.py
    ./venv/Scripts/python.exe scripts/buat_pack_kamera.py --keluar suatu/tempat.zip

Sesudah dibuat, pack HARUS berada di prioritas TERTINGGI (paling akhir di
`resourcePacks` di options.txt) supaya tidak ditimpa Bare Bones.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import zipfile

# Tata letak layar peti vanilla, dipakai untuk menutup slot yang tidak terpakai.
SLOT_X0, SLOT_Y0, SLOT = 7, 17, 18
# Minecraft menggambar layar peti dari DUA potongan tekstur yang sama:
#   bagian kontainer     -> (0, 0)   ukuran 176 x (baris*18 + 17)
#   inventory PEMAIN     -> (0, 126) ukuran 176 x 96
# Jadi menghapus baris 126 ke bawah menghilangkan kotak bawah tanpa menyentuh
# kotak atas (6 baris berakhir di y=124).
BATAS_INVENTORY_PEMAIN = 126
LEBAR_GUI = 176

# Slot yang template kita biarkan kosong: baris ke-5 kolom 5-8 (di kirinya ada
# 4 armor + offhand) dan seluruh baris ke-6. Cocokkan dengan `templates.inventory`
# di plugins/InvSeePlusPlus/config.yml.
SLOT_KOSONG = [(4, k) for k in range(5, 9)] + [(5, k) for k in range(9)]

TEKSTUR = "assets/minecraft/textures/gui/container/generic_54.png"


def _cari_client_jar(versi: str = "1.21.4") -> pathlib.Path:
    dasar = (pathlib.Path(os.path.expanduser("~")) / "AppData/Roaming/PrismLauncher"
             / "libraries/com/mojang/minecraft" / versi)
    for nama in (f"minecraft-{versi}-client.jar", f"{versi}-client.jar"):
        p = dasar / nama
        if p.is_file():
            return p
    cocok = sorted(dasar.glob("*client*.jar"))
    if not cocok:
        raise FileNotFoundError(f"jar klien {versi} tidak ketemu di {dasar}")
    return cocok[0]


def bangun(keluar: pathlib.Path, versi: str = "1.21.4") -> pathlib.Path:
    from PIL import Image  # lokal: skrip ini tidak dipakai bridge

    with zipfile.ZipFile(_cari_client_jar(versi)) as jar:
        im = Image.open(io.BytesIO(jar.read(TEKSTUR))).convert("RGBA")
    px = im.load()

    for y in range(BATAS_INVENTORY_PEMAIN, im.height):
        for x in range(LEBAR_GUI):
            if px[x, y][3]:
                px[x, y] = (0, 0, 0, 0)

    for baris, kolom in SLOT_KOSONG:
        x0, y0 = SLOT_X0 + kolom * SLOT, SLOT_Y0 + baris * SLOT
        # Warna diambil dari pinggir panel pada BARIS YANG SAMA, bukan
        # dihardcode — supaya tambalannya menyatu kalau tekstur vanilla berubah.
        latar = px[3, y0 + SLOT // 2]
        for y in range(y0, y0 + SLOT):
            for x in range(x0, x0 + SLOT):
                px[x, y] = latar

    buf = io.BytesIO()
    im.save(buf, "PNG")
    keluar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(keluar, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.mcmeta", json.dumps({"pack": {
            "pack_format": 46,
            "supported_formats": {"min_inclusive": 15, "max_inclusive": 999},
            "description": "Kamera siaran Arti: sembunyikan inventory kamera & slot kosong",
        }}, indent=1))
        z.writestr(TEKSTUR, buf.getvalue())
        # Label "Inventory" itu TEKS yang digambar game, bukan bagian tekstur.
        for lang in ("en_us", "id_id"):
            z.writestr(f"assets/minecraft/lang/{lang}.json",
                       json.dumps({"container.inventory": ""}))
    return keluar


def main() -> int:
    bawaan = (pathlib.Path(os.path.expanduser("~")) / "AppData/Roaming/PrismLauncher"
              / "instances/CamInstance/minecraft/resourcepacks/ZZ-Kamera-UI.zip")
    p = argparse.ArgumentParser()
    p.add_argument("--keluar", default=str(bawaan))
    p.add_argument("--versi", default="1.21.4")
    a = p.parse_args()
    hasil = bangun(pathlib.Path(a.keluar), a.versi)
    print(f"pack dibuat: {hasil} ({hasil.stat().st_size} byte)")
    print("Pastikan dia PALING AKHIR di resourcePacks (prioritas tertinggi).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
