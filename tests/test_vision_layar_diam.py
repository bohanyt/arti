# -*- coding: utf-8 -*-
"""Gerbang layar-diam.

Arti menonton dirinya sendiri -> frame nyaris identik tiap giliran, tapi
provider vision tetap dipanggil dan [LAYAR:] tetap disuntik, jadi banyak
jawaban menyebut layar. Gerbang memutus di HULU (piksel), bukan dengan
menyaring teks hasil halusinasi provider.

Tes memakai GAMBAR SUNGGUHAN (PIL) — bukan mock — sesuai aturan #11.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_vision_capture as avc  # noqa: E402
import arti_vision_client as avci  # noqa: E402


def _layar(warna=(30, 30, 40), kotak=None) -> Image.Image:
    img = Image.new("RGB", (640, 360), warna)
    if kotak:
        ImageDraw.Draw(img).rectangle(kotak, fill=(240, 200, 60))
    return img


def test_sidik_ukuran_tetap():
    s = avc.sidik_layar(_layar())
    assert len(s) == 32 * 18


def test_frame_identik_beda_nol():
    a = avc.sidik_layar(_layar(kotak=(10, 10, 100, 100)))
    b = avc.sidik_layar(_layar(kotak=(10, 10, 100, 100)))
    assert avc.beda_persen(a, b) == 0.0


def test_gerakan_kecil_di_bawah_ambang():
    """Model VTuber gerak sedikit (kedip/napas) — jangan dianggap layar baru."""
    a = avc.sidik_layar(_layar(kotak=(300, 150, 340, 190)))
    b = avc.sidik_layar(_layar(kotak=(302, 152, 342, 192)))
    assert avc.beda_persen(a, b) < 6.0


def test_subtitle_kecil_TETAP_lolos_gerbang():
    """Regresi paling mahal yang nyaris lolos: ambang rata-rata SAJA (6%)
    membungkam subtitle. Ukur nyata di layar uji — subtitle satu
    baris: rata-rata 2,0% (di bawah 6% = dibungkam) tapi sel-berubah 5,2%.
    Metrik sel wajib menyelamatkannya, kalau tidak game cerita jadi buta."""
    dasar = _layar(warna=(60, 60, 70))
    dgn_teks = dasar.copy()
    W, H = dasar.size
    ImageDraw.Draw(dgn_teks).rectangle(
        (int(W * .2), int(H * .82), int(W * .8), int(H * .87)), fill=(255, 255, 255)
    )
    a, b = avc.sidik_layar(dasar), avc.sidik_layar(dgn_teks)
    sel = avc.sel_berubah_persen(a, b)
    assert sel >= 2.0, f"subtitle harus melewati ambang sel, dapat {sel:.2f}%"


def test_metrik_sel_memisahkan_derau_dari_perubahan():
    """Derau gerakan kecil harus tetap di bawah ambang sel (2%)."""
    a = avc.sidik_layar(_layar(kotak=(300, 150, 340, 190)))
    b = avc.sidik_layar(_layar(kotak=(302, 152, 342, 192)))
    assert avc.sel_berubah_persen(a, b) < 2.0


def test_perubahan_besar_di_atas_ambang():
    """Ganti scene (mis. buka game) HARUS lolos gerbang."""
    a = avc.sidik_layar(_layar())
    b = avc.sidik_layar(_layar(warna=(240, 240, 240)))
    assert avc.beda_persen(a, b) > 6.0


def test_sidik_none_dianggap_baru():
    assert avc.beda_persen(None, avc.sidik_layar(_layar())) == 100.0


def test_layar_gelap_terdeteksi():
    gelap = avc.sidik_layar(Image.new("RGB", (640, 360), (3, 3, 3)))
    terang = avc.sidik_layar(_layar(warna=(120, 120, 120)))
    assert avc.layar_gelap(gelap) is True
    assert avc.layar_gelap(terang) is False


def test_gelap_dengan_isi_bukan_gelap():
    """Layar gelap TAPI ada konten terang (subtitle/game horor) = tetap bahan."""
    img = _layar(warna=(4, 4, 4), kotak=(200, 300, 440, 340))
    assert avc.layar_gelap(avc.sidik_layar(img)) is False


def test_gerbang_melewatkan_provider_saat_diam(monkeypatch):
    """Eksekusi jalur: dua tangkapan identik -> tangkapan kedua TIDAK
    mengembalikan gambar (provider tak dipanggil) dan hitungan diam naik."""
    avci.reset_gerbang_layar()
    img = _layar(kotak=(50, 50, 200, 200))
    monkeypatch.setattr(
        avc, "capture_dengan_sidik",
        lambda cfg: ("b64palsu", b"", avc.sidik_layar(img)),
    )
    cfg = {"vision_beda_min_persen": 6.0, "vision_sel_berubah_min_persen": 2.0,
           "vision_diam_stop_inject": 2}
    b64_1, _ = avci._tangkap_bergerbang(cfg)
    assert b64_1 == "b64palsu", "tangkapan pertama harus lolos (belum ada acuan)"
    assert not avci.layar_sedang_diam(cfg)
    for _ in range(2):
        b64_n, _ = avci._tangkap_bergerbang(cfg)
        assert b64_n is None, "layar sama harus memutus sebelum provider"
    assert avci.layar_sedang_diam(cfg), "bridge harus berhenti menyuntik [LAYAR:]"


def test_gerbang_membuka_lagi_saat_layar_berubah(monkeypatch):
    avci.reset_gerbang_layar()
    kotak = {"v": (50, 50, 200, 200)}
    monkeypatch.setattr(
        avc, "capture_dengan_sidik",
        lambda cfg: ("b64", b"", avc.sidik_layar(_layar(kotak=kotak["v"]))),
    )
    cfg = {"vision_beda_min_persen": 6.0, "vision_sel_berubah_min_persen": 2.0,
           "vision_diam_stop_inject": 2}
    avci._tangkap_bergerbang(cfg)
    avci._tangkap_bergerbang(cfg)
    avci._tangkap_bergerbang(cfg)
    assert avci.layar_sedang_diam(cfg)
    kotak["v"] = (0, 0, 640, 360)  # scene berganti total
    b64, _ = avci._tangkap_bergerbang(cfg)
    assert b64 == "b64", "perubahan besar harus lolos"
    assert not avci.layar_sedang_diam(cfg), "hitungan diam wajib reset"


def test_bridge_berhenti_suntik_saat_diam():
    """Sisi bridge: gerbang dicek SEBELUM format_screen_context."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    i_gerbang = src.index("layar_sedang_diam(CONFIG)")
    i_format = src.index("arti_screen_context.format_screen_context(", i_gerbang - 2000)
    assert i_gerbang < i_format
    assert '"vision_beda_min_persen"' in src
    assert '"vision_diam_stop_inject"' in src


def test_config_default_waras():
    import arti_bridge as b

    assert 0 < float(b.CONFIG["vision_beda_min_persen"]) < 50
    assert int(b.CONFIG["vision_diam_stop_inject"]) >= 0
