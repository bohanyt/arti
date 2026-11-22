"""Tests BANGUN tempat berlindung — potongan terakhir Phase 2.

Bukan "membangun apa saja": bentuk bebas butuh perencanaan ruang, dan prompt
tetap jujur bahwa dia belum bisa itu. Yang dibuat di sini adalah hal yang
benar-benar dilakukan pemain sungguhan waktu malam keburu datang — berdiri di
satu titik lalu menembok diri sendiri. Semua sasaran ada dalam jangkauan
tangan, jadi tidak perlu navigasi sama sekali.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
CFG = {"minecraft_place_allowlist": ["cobblestone", "dirt"]}


def _blok() -> str:
    i = BOT_JS.index("async bangun (c)")
    return BOT_JS[i:BOT_JS.index(chr(10) + "  // NYERANG", i)]


def test_tag_defaults_to_cobblestone_and_respects_the_place_allowlist():
    assert am.parse_mc_tags("[MC: bangun]", CFG)[1] == \
        [{"cmd": "bangun", "block": "cobblestone"}]
    assert am.parse_mc_tags("[MC: bangun dirt]", CFG)[1] == \
        [{"cmd": "bangun", "block": "dirt"}]
    assert am.parse_mc_tags("[MC: bangun diamond_block]", CFG)[1] == []


def test_the_roof_ring_is_built_before_the_roof_itself():
    """REGRESI TERUKUR (server nyata 2026-08-07): versi pertama bertembok dua
    tingkat lalu menaruh atap di dy=2. Atap itu TIDAK PERNAH bisa dipasang —
    keenam tetangganya udara semua, tidak ada permukaan untuk menempelkannya.
    Cincin dy=2 yang memberinya pijakan, jadi urutannya menentukan."""
    b = _blok()
    assert "[0, 1, 2]" in b, "dinding tidak sampai tingkat atap"
    assert b.index("[0, 1, 2]") < b.index("penutup atap")


def test_holes_are_counted_from_the_world_not_from_bookkeeping():
    """REGRESI TERUKUR: pembukuan per langkah bilang 0 bolong padahal satu
    sisi masih menganga — satu posisi tercatat "[recorded remark omitted]" padahal udara.
    Yang dilaporkan ke penonton harus dibaca ulang dari dunia."""
    b = _blok()
    i = b.index("let bolong = 0")
    j = b.index("ev: 'build_done'")
    assert i < j
    assert "bot.blockAt(t)" in b[i:j], "lubang tidak diperiksa ke dunia"
    assert "boundingBox !== 'block'" in b[i:j]


def test_failed_spots_get_one_retry():
    """Sebagian kegagalan cuma soal urutan: blok tetangganya belum ada waktu
    giliran pertama lewat."""
    b = _blok()
    assert "const ulang = []" in b and b.count("naruhSatu(t, nama, jenis)") == 2


def test_it_uses_the_same_placement_path_as_place():
    """Dua salinan logika penempatan akan menyimpang. `place` dan `bangun`
    sama-sama lewat naruhSatu, termasuk pemeriksaan ulang di dunia."""
    assert "naruhSatu(t, nama, jenis)" in _blok()
    i = BOT_JS.index("async place (c)")
    assert "naruhSatu(" in BOT_JS[i:BOT_JS.index(chr(10) + "  kabur()", i)]


def test_she_stands_still_and_is_not_mistaken_for_stuck():
    b = _blok()
    assert "await tungguDiam()" in b and "setGoal(null)" in b
    i = BOT_JS.index("Date.now() - lastMoveAt > STUCK_MS")
    assert "currentTask !== 'bangun' &&" in BOT_JS[max(0, i - 1800):i]


def test_running_out_of_blocks_stops_cleanly():
    b = _blok()
    assert "if (!jumlahDiTas(nama)) break" in b
    assert "tidak_punya_blok" in b


def test_reactions_never_claim_a_finished_shelter_when_it_is_holed():
    lim = am.ReactionLimiter()
    bolong = am.map_event_to_reaction(
        {"ev": "build_done", "block": "cobblestone", "placed": 20, "missing": 3},
        lim, 100.0, {})
    assert bolong and "bolong" in bolong.lower()
    kosong = am.map_event_to_reaction(
        {"ev": "build_done", "block": "cobblestone", "placed": 0, "missing": 25},
        am.ReactionLimiter(), 100.0, {})
    assert kosong and "jangan mengaku sudah jadi" in kosong.lower()
    rapat = am.map_event_to_reaction(
        {"ev": "build_done", "block": "cobblestone", "placed": 25, "missing": 0},
        am.ReactionLimiter(), 100.0, {})
    assert rapat and "rapat" in rapat.lower()


def test_reactions_hide_raw_numbers():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction(
        {"ev": "build_done", "block": "cobblestone", "placed": 21, "missing": 0},
        lim, 100.0, {})
    assert "21" not in r and "jangan sebut angka" in r.lower()


def test_prompt_allows_the_shelter_but_still_denies_free_form_building():
    assert "[MC: bangun" in BRIDGE
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 1400]
    bisa = blok[blok.index("BISA:"):blok.index("BELUM BISA:")]
    belum = blok[blok.index("BELUM BISA:"):blok.index("Jadi menambang")]
    assert "TEMPAT BERLINDUNG" in bisa
    # sumbernya string yang disambung antar baris, jadi frasanya bisa
    # terpotong — dicek dari potongan yang pasti utuh dalam satu baris
    assert "bebas (rumah bertingkat" in belum, "bangunan bebas harus TETAP belum bisa"


# --- menu aksi: dibaca model tiap giliran, jadi bentuknya penting ---------

def test_the_block_list_appears_once_not_per_tag():
    """`place` dan `bangun` memakai daftar blok yang SAMA PERSIS. Versi
    sebelumnya menempelkannya dua kali di dalam satu kalimat 1428 karakter
    tanpa jeda — untuk model yang cuma perlu memilih satu tag di ujung
    jawaban, itu resep diabaikan."""
    i = BRIDGE.index("[AKSI MINECRAFT]")
    menu = BRIDGE[i:BRIDGE.index("membaca tag di kalimatmu", i)]
    assert menu.count("_bisa_ditaruh") == 1, "daftar blok masih diulang"
    assert menu.count("_bisa_dibikin") == 1


def test_the_action_menu_is_broken_into_lines():
    """Satu paragraf panjang menyembunyikan pilihannya. Baris berlabel bikin
    modelnya bisa memindai."""
    i = BRIDGE.index("[AKSI MINECRAFT]")
    menu = BRIDGE[i:BRIDGE.index("membaca tag di kalimatmu", i)]
    for label in ("- gerak :", "- badan :", "- kerja :", "- lain  :"):
        assert label in menu, label


def test_no_capability_was_dropped_while_shortening():
    """Memendekkan menu tidak boleh diam-diam menghapus kemampuan."""
    # mulai dari `_gerak` — tag gerak dirakit di variabel terpisah karena
    # isinya beda saat Arti pegang siaran sendiri
    i = BRIDGE.index("    _gerak = (")
    menu = BRIDGE[i:BRIDGE.index("membaca tag di kalimatmu", i)]
    for tag in ("follow", "roam", "come", "stop", "eat", "kabur", "serang",
                "mine", "craft", "place", "bangun", "say", "give", "buka_tas",
                "status", "leave"):
        assert f"[MC: {tag}" in menu, tag
