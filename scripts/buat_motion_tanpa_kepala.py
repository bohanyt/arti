"""Buat SALINAN motion3 tanpa kurva kepala — bahan uji Fase 2 rombak animasi.

Kenapa: motion menulis `ParamAngleY` (keluaran Live2D) sedangkan angguk
menyuntik `FaceAngleY` (masukan tracking). Motion menang, jadi selama motion
memegang kepala, angguk TIDAK AKAN PERNAH terlihat — itu sebabnya dulu Bohan
terpaksa mematikan seluruh motion tiap kali Arti bicara.

Bonusnya: dari 46 parameter ekspresi, cuma TIGA yang berbeda antar-pose
(`ParamAngleX`, `ParamAngleZ`, `ParamEyeBallX`) — dan dua di antaranya juga
digerakkan motion. Jadi motion bukan cuma menelan anggukan, tapi juga
menelan variasi 50 ekspresi itu.

Skrip ini TIDAK PERNAH menyentuh berkas asli. Dia menulis berkas BARU, dan
menolak jalan kalau berkas tujuan sudah ada.

    ./venv/Scripts/python.exe scripts/buat_motion_tanpa_kepala.py

Sesudah itu Bohan perlu mendaftarkan hotkey baru di UI VTS yang menunjuk ke
berkas hasil (VTS API tidak bisa membuat hotkey).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

MODEL_DIR = os.environ.get(
    "VTS_MODEL_DIR",
    r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio"
    r"\VTube Studio_Data\StreamingAssets\Live2DModels\MyModel_vts",
)
MOTION_DIR = os.path.join(MODEL_DIR, "motion")

# Segmen motion3: [t0, v0] lalu berulang <tipe, ...>.
# 0 Linear / 2 Stepped / 3 InverseStepped = 3 angka, 1 titik baru.
# 1 Bezier = 7 angka, 3 titik baru.
_KONSUMSI = {0: 3, 1: 7, 2: 3, 3: 3}
_TITIK = {0: 1, 1: 3, 2: 1, 3: 1}


def hitung_segmen(segments: list) -> tuple[int, int]:
    """Kembalikan (jumlah_segmen, jumlah_titik) untuk satu kurva."""
    if len(segments) < 2:
        return 0, 0
    titik = 1  # titik awal (t0, v0)
    segmen = 0
    i = 2
    while i < len(segments):
        tipe = int(segments[i])
        if tipe not in _KONSUMSI:
            raise ValueError(f"tipe segmen tak dikenal: {tipe} pada indeks {i}")
        segmen += 1
        titik += _TITIK[tipe]
        i += _KONSUMSI[tipe]
    if i != len(segments):
        raise ValueError(f"segmen tidak habis rapi: berhenti di {i}/{len(segments)}")
    return segmen, titik


def hitung_meta(kurva: list) -> dict:
    seg = tit = 0
    for c in kurva:
        s, t = hitung_segmen(c.get("Segments", []))
        seg += s
        tit += t
    return {"CurveCount": len(kurva), "TotalSegmentCount": seg, "TotalPointCount": tit}


def swauji(data: dict, nama: str) -> None:
    """Dua bukti sebelum menulis apa pun.

    TEMUAN 25 Agu: Meta bawaan VTS TIDAK persis. Pada ArtiIdle1 tercatat
    4368 segmen padahal isinya 4364, dan 14340 titik padahal 13393. Jadi
    menuntut hitung-ulang SAMA PERSIS itu salah kaprah — dan versi pertama
    skrip ini memang langsung berhenti karenanya (bagus: nol berkas ditulis).

    Yang benar: Meta di berkas sumber adalah BATAS ATAS. Karena kita cuma
    MEMBUANG kurva, mempertahankan angka sumber tetap jadi batas atas yang
    sah. Yang WAJIB tepat cuma CurveCount, karena itu yang menentukan
    berapa kurva dibaca.

    1. Tiap Segments habis dibaca rapi -> tabel tipe segmen benar.
       (hitung_segmen sendiri yang melempar kalau tidak.)
    2. Hitung-ulang <= Meta -> konvensi batas-atas itu memang berlaku.
    """
    ulang = hitung_meta(data["Curves"])
    meta = data["Meta"]
    if ulang["CurveCount"] != meta.get("CurveCount"):
        raise SystemExit(
            f"SWAUJI GAGAL di {nama}: CurveCount {ulang['CurveCount']} != "
            f"{meta.get('CurveCount')} — JANGAN tulis berkas apa pun."
        )
    for k in ("TotalSegmentCount", "TotalPointCount"):
        if ulang[k] > meta.get(k, 0):
            raise SystemExit(
                f"SWAUJI GAGAL di {nama}: {k} hitung-ulang={ulang[k]} MELEBIHI "
                f"Meta={meta.get(k)} — parser segmen kemungkinan salah."
            )
    print(f"  swauji {nama}: {ulang['CurveCount']} kurva terbaca rapi; "
          f"segmen {ulang['TotalSegmentCount']}<={meta['TotalSegmentCount']}, "
          f"titik {ulang['TotalPointCount']}<={meta['TotalPointCount']}")


def buat(sumber: str, tujuan: str, buang: set[str], timpa: bool) -> None:
    src = os.path.join(MOTION_DIR, sumber)
    dst = os.path.join(MOTION_DIR, tujuan)
    if not os.path.exists(src):
        raise SystemExit(f"sumber tidak ada: {src}")
    if os.path.exists(dst) and not timpa:
        raise SystemExit(f"tujuan sudah ada, tidak ditimpa: {dst}")

    with io.open(src, encoding="utf-8") as f:
        data = json.load(f)

    swauji(data, sumber)

    sisa = [
        c for c in data["Curves"]
        if not (c.get("Target") == "Parameter" and c.get("Id") in buang)
    ]
    dibuang = len(data["Curves"]) - len(sisa)
    if dibuang != len(buang):
        print(f"  CATATAN: {dibuang} kurva dibuang, diminta {len(buang)} "
              "(sebagian mungkin memang tidak ada di motion ini)")

    baru = dict(data)
    baru["Curves"] = sisa
    # HANYA CurveCount yang diperbarui. TotalSegment/PointCount dibiarkan
    # apa adanya dari sumber: kita cuma membuang kurva, jadi angka lama tetap
    # jadi batas atas yang sah — dan berkas sumber membuktikan VTS menerima
    # Meta yang lebih besar dari isinya.
    meta = dict(data["Meta"])
    meta["CurveCount"] = len(sisa)
    baru["Meta"] = meta

    with io.open(dst, "w", encoding="utf-8") as f:
        json.dump(baru, f, ensure_ascii=False)

    print(f"  {sumber} -> {tujuan}")
    print(f"     kurva {data['Meta']['CurveCount']} -> {meta['CurveCount']}"
          f" | segmen {data['Meta']['TotalSegmentCount']} -> {meta['TotalSegmentCount']}"
          f" | dibuang: {sorted(buang)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sumber", default="ArtiIdle1.motion3.json")
    ap.add_argument("--timpa", action="store_true", help="izinkan menimpa hasil lama")
    a = ap.parse_args()

    print(f"folder motion: {MOTION_DIR}")
    if not os.path.isdir(MOTION_DIR):
        raise SystemExit("folder motion tidak ketemu — set VTS_MODEL_DIR")

    # Varian A: minimal — cuma sumbu yang dipakai angguk.
    buat(a.sumber, "ArtiUjiTanpaAngguk.motion3.json",
         {"ParamAngleY"}, a.timpa)
    # Varian B: pemisahan penuh — kepala & arah pandang milik angguk+ekspresi,
    # badan milik motion.
    buat(a.sumber, "ArtiUjiTanpaKepala.motion3.json",
         {"ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamEyeBallX"}, a.timpa)

    print()
    print("BERKAS ASLI TIDAK DISENTUH. Langkah Bohan berikutnya:")
    print("  1. VTS -> hotkey baru -> Play Animation (motion3) -> pilih")
    print("     ArtiUjiTanpaKepala.motion3.json, beri nama 'UjiTanpaKepala'")
    print("  2. Bilang ke agen; motion-nya dipicu lewat API dan dilihat bareng.")


if __name__ == "__main__":
    sys.exit(main())
