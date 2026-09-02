"""Screenshot capture for vision chain (mss + JPEG resize)."""

from __future__ import annotations

import base64
import io
from typing import Any

from PIL import Image


def capture_jpeg_b64(config: dict | None = None) -> tuple[str, bytes]:
    """Grab primary monitor; return (base64 str, raw jpeg bytes)."""
    import mss

    cfg = config or {}
    max_width = int(cfg.get("vision_capture_max_width", 1280))
    quality = int(cfg.get("vision_capture_jpeg_quality", 75))

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    jpeg = buf.getvalue()
    return base64.b64encode(jpeg).decode("ascii"), jpeg


# --------------------------------------------------------------------------- #
# Sidik jari layar ([date removed]) — operator: "dia liatin layar terus... ngetrigger
# vision terus jadi komentarnya berdasarkan itu mulu... bikin threshold, kalau
# perubahannya cuma berapa persen gak perlu dikomentarin".
#
# Arti MENONTON DIRINYA SENDIRI di layar: sebagian besar frame nyaris identik
# (model VTuber diam + overlay statis), tapi tiap giliran vision tetap dipanggil
# dan hasilnya disuntik ke prompt -> topik muter di situ (log [time removed]: 157 kali
# auto-vision, 49/211 jawaban menyebut layar). Sidik jari 32x18 abu-abu cukup
# untuk memutuskan "layar ini sudah pernah dikomentari" dengan biaya ~1 ms —
# JAUH lebih murah daripada satu panggilan provider vision.
# --------------------------------------------------------------------------- #

_SIDIK_W, _SIDIK_H = 32, 18


def sidik_layar(img: "Image.Image") -> bytes:
    """Sidik jari kecil: 32x18 grayscale (576 byte)."""
    kecil = img.convert("L").resize((_SIDIK_W, _SIDIK_H), Image.BILINEAR)
    return kecil.tobytes()


def beda_persen(a: bytes | None, b: bytes | None) -> float:
    """Beda rata-rata dua sidik dalam persen (0-100). None = 100 (anggap baru)."""
    if not a or not b or len(a) != len(b):
        return 100.0
    total = sum(abs(x - y) for x, y in zip(a, b))
    return (total / (len(a) * 255.0)) * 100.0


def sel_berubah_persen(a: bytes | None, b: bytes | None, delta: int = 12) -> float:
    """Persen SEL yang berubah lebih dari `delta` level (0-100).

    Metrik UTAMA gerbang. Beda rata-rata saja terbukti salah alat: diukur di
    layar live Bohan 20 Agu, subtitle satu baris cuma menggeser rata-rata 2,0%
    (di bawah ambang 6% yang sempat kupakai — teks penting akan DIBUNGKAM),
    sedangkan sel-berubahnya 5,2% — jauh di atas derau layar diam (0,0-0,9%).
    Perubahan kecil-tapi-berarti itu LOKAL: sedikit sel, tapi berubah tajam.
    """
    if not a or not b or len(a) != len(b):
        return 100.0
    return 100.0 * sum(1 for x, y in zip(a, b) if abs(x - y) > delta) / len(a)


def layar_gelap(sidik: bytes | None, luma_max: float = 12.0,
                sebar_min: float = 6.0) -> bool:
    """Frame gelap/nyaris seragam = bukan bahan obrolan (halusinasi provider).

    Log 20 Agu pagi: frame idle dideskripsikan "Sistem sedang mendeteksi
    keheningan di stream" dan "menjalankan skrip otomatisasi" — model MENGARANG
    makna dari layar kosong. Menyaring dari TEKS-nya selalu kejar-kejaran;
    menyaringnya dari piksel selesai sekali.
    """
    if not sidik:
        return False
    n = len(sidik)
    rata = sum(sidik) / n
    if rata > luma_max:
        return False
    sebar = (sum((v - rata) ** 2 for v in sidik) / n) ** 0.5
    return sebar < sebar_min


def capture_dengan_sidik(config: dict | None = None) -> tuple[str, bytes, bytes]:
    """capture_jpeg_b64 + sidik jari, satu kali tangkap (bukan dua)."""
    import mss

    cfg = config or {}
    max_width = int(cfg.get("vision_capture_max_width", 1280))
    quality = int(cfg.get("vision_capture_jpeg_quality", 75))

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    sidik = sidik_layar(img)
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    jpeg = buf.getvalue()
    return base64.b64encode(jpeg).decode("ascii"), jpeg, sidik


def probe_capture(config: dict | None = None) -> tuple[bool, str]:
    """Lightweight health probe — non-empty JPEG."""
    try:
        b64, raw = capture_jpeg_b64(config)
        if not b64 or len(raw) < 100:
            return False, "empty capture"
        return True, f"{len(raw)} bytes"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
