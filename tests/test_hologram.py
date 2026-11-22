"""Tests purwarupa UI craft melayang (display entity vanilla).

Ide Bohan 2026-08-07: UI craft ngambang yang kelihatan pemain lain, tanpa
bikin UI dari nol. Riset: tidak ada plugin jadi untuk itu, TAPI Minecraft
1.19.4+ punya `item_display`/`text_display` — jadi tidak perlu plugin, mod,
maupun overlay OBS.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.mc_hologram_demo import (  # noqa: E402
    GRID_CONTOH, HASIL_CONTOH, TAG, perintah_pasang,
)


def _cmds():
    return perintah_pasang(10.0, 64.0, -20.0, GRID_CONTOH, HASIL_CONTOH)


def test_only_filled_slots_become_entities():
    """Kotak kosong tidak boleh jadi entity — 9 entity untuk resep 5 bahan
    berarti empat item hantu melayang di udara."""
    isi = sum(1 for baris in GRID_CONTOH for x in baris if x)
    # judul + bahan + hasil
    assert len(_cmds()) == 1 + isi + 1


def test_every_entity_is_tagged_for_cleanup():
    """Tanpa tag, hologram tidak bisa dihapus dan menumpuk tiap craft."""
    for c in _cmds():
        assert f'Tags:["{TAG}"]' in c


def test_grid_is_centred_and_ordered_top_left_first():
    """Kolom kiri harus di x lebih kecil, baris atas di y lebih besar —
    kalau terbalik, resepnya terbaca ngawur."""
    import re

    titik = []
    for c in _cmds():
        if "item_display" not in c:
            continue
        m = re.search(r"item_display (-?[\d.]+) (-?[\d.]+) (-?[\d.]+)", c)
        titik.append((float(m.group(1)), float(m.group(2))))
    # tiga papan di baris atas: y sama, x menaik
    atas = sorted([t for t in titik if abs(t[1] - max(p[1] for p in titik)) < 1e-6])
    assert len(atas) == 3
    assert atas[0][0] < atas[1][0] < atas[2][0]
    assert atas[1][0] == 10.0, "grid tidak terpusat di pemain"


def test_result_sits_apart_from_the_grid():
    import re

    xs = [float(re.search(r"item_display (-?[\d.]+)", c).group(1))
          for c in _cmds() if "item_display" in c]
    assert max(xs) > 10.0 + 0.4, "hasil menempel di grid, tidak terbaca"


def test_title_is_above_everything():
    import re

    judul = next(c for c in _cmds() if "text_display" in c)
    y_judul = float(re.search(r"text_display -?[\d.]+ (-?[\d.]+)", judul).group(1))
    y_item = [float(re.search(r"item_display -?[\d.]+ (-?[\d.]+)", c).group(1))
              for c in _cmds() if "item_display" in c]
    assert y_judul > max(y_item)


def test_billboard_faces_the_viewer_and_is_lit():
    """Tanpa billboard, hologram cuma terbaca dari satu sisi; tanpa brightness
    dia gelap gulita saat malam — dua-duanya bikin fitur ini percuma."""
    for c in _cmds():
        assert 'billboard:"center"' in c
    for c in _cmds():
        if "item_display" in c:
            assert "brightness:{sky:15,block:15}" in c


def test_text_is_sanitised():
    """Judul masuk ke NBT JSON — kutip atau backslash liar merusak perintahnya
    dan bisa jadi celah injeksi."""
    c = perintah_pasang(0, 0, 0, [[None] * 3] * 3, "stone",
                        'nakal" ,extra:1, "x')[0]
    assert c.count("'") == 2, "kutip tunggal pembungkus NBT bocor"
    assert chr(92) not in c, "backslash lolos ke NBT"
