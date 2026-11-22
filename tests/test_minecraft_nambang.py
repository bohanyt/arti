"""Tests MENAMBANG (Phase 2) lewat mineflayer-collectblock.

Dipilih duluan sebelum craft karena craft mengubah barang jadi barang lain,
sementara Arti belum punya cara MENDAPATKAN bahan sama sekali — isi tasnya
cuma yang Bohan berikan. Nambang membuka dua-duanya sekaligus.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
CFG = {"minecraft_mine_allowlist": ["oak_log", "stone", "coal_ore"]}


def _kode(blok: str) -> str:
    return "\n".join(b for b in blok.splitlines()
                     if b.strip() and not b.strip().startswith("//"))


# --- protokol (verb `mine` sudah ada, sekarang PUNYA handler) -------------

def test_mine_tag_respects_allowlist():
    _, cmds = am.parse_mc_tags("[MC: mine oak_log 3]", CFG)
    assert cmds == [{"cmd": "mine", "block": "oak_log", "count": 3}]
    # di luar allowlist -> tag dibuang diam-diam (pagar anti-halusinasi nama)
    assert am.parse_mc_tags("[MC: mine diamond_block 1]", CFG)[1] == []


def test_mine_count_is_clamped():
    _, cmds = am.parse_mc_tags("[MC: mine stone 999]", CFG)
    assert cmds[0]["count"] == am.COUNT_MAX
    _, cmds = am.parse_mc_tags("[MC: mine stone 0]", CFG)
    assert cmds[0]["count"] == am.COUNT_MIN


def test_mine_encodes_with_block_and_count():
    baris = json.loads(am.encode_command({"cmd": "mine", "block": "oak_log", "count": 2}))
    assert baris == {"cmd": "mine", "block": "oak_log", "count": 2}


# --- event & reaksi -------------------------------------------------------

def test_result_is_announced_start_is_not():
    """Yang menarik hasilnya, bukan niatnya — kalau mulai & selesai dua-duanya
    bicara, satu aktivitas jadi dua komentar."""
    lim = am.ReactionLimiter()
    assert am.map_event_to_reaction(
        {"ev": "collect_start", "block": "oak_log", "count": 3}, lim, 100.0, {}) is None
    r = am.map_event_to_reaction(
        {"ev": "collect_done", "block": "oak_log", "count": 3}, lim, 101.0, {})
    assert r and "oak_log" in r and "jangan sebut angka" in r.lower()
    assert "3" not in r


def test_empty_haul_is_admitted_not_hidden():
    """Nambang tapi tidak dapat apa-apa (keburu jatuh / diambil orang) harus
    tetap diceritakan, kalau tidak dia mengaku dapat padahal tidak."""
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction(
        {"ev": "collect_done", "block": "stone", "count": 0}, lim, 100.0, {})
    assert r and "tidak dapat" in r


def test_mine_failures_have_human_labels():
    for alasan in ("blok_tak_dikenal", "tidak_ketemu", "gagal_nambang"):
        out = am.format_context(
            None, [(99.0, {"ev": "task_failed", "task": "mine", "reason": alasan})],
            ttl_sec=120.0, now=100.0)
        assert alasan not in out and out.strip(), alasan


def test_mining_appears_in_context():
    out = am.format_context(
        None, [(99.0, {"ev": "collect_start", "block": "oak_log"}),
               (99.5, {"ev": "collect_done", "block": "oak_log", "count": 3})],
        ttl_sec=120.0, now=100.0)
    assert "menambang oak_log" in out and "dapat oak_log" in out
    assert "menambang" in am.task_label("nambang")


# --- bot.js ---------------------------------------------------------------

def test_plugin_loaded_and_used():
    assert "mineflayer-collectblock" in BOT_JS
    assert "bot.loadPlugin(collectBlock)" in BOT_JS
    assert "bot.collectBlock.collect(" in BOT_JS
    pkg = json.loads((ROOT / "mc-bot" / "package.json").read_text(encoding="utf-8"))
    assert "mineflayer-collectblock" in pkg["dependencies"]


def test_block_name_is_verified_against_the_real_world():
    """Allowlist Python cuma menyaring NAMA. Nama yang lolos regex belum tentu
    blok nyata di versi ini — halusinasi seperti "copper_log" harus dilaporkan,
    bukan bikin bot melempar."""
    i = BOT_JS.index("async mine(c)")
    blok = _kode(BOT_JS[i:BOT_JS.index("\n  kabur()", i)])
    assert "bot.registry.blocksByName[nama]" in blok
    assert "blok_tak_dikenal" in blok


def test_every_failure_path_reports():
    i = BOT_JS.index("async mine(c)")
    blok = _kode(BOT_JS[i:BOT_JS.index("\n  kabur()", i)])
    for alasan in ("blok_tak_dikenal", "tidak_ketemu", "gagal_nambang"):
        assert alasan in blok, alasan
    assert "try {" in blok and "catch" in blok and "finally {" in blok


def test_haul_is_counted_from_the_inventory_not_assumed():
    """Jumlah yang diklaim harus dari isi tas SEBELUM vs SESUDAH — mengasumsikan
    "diminta 3 berarti dapat 3" bikin dia berbohong saat barangnya jatuh."""
    i = BOT_JS.index("async mine(c)")
    blok = _kode(BOT_JS[i:BOT_JS.index("\n  kabur()", i)])
    assert "jumlahDiTas(nama)" in blok
    assert "jumlahDiTas(nama) - sebelum" in blok


def test_digging_is_not_mistaken_for_being_stuck():
    """Menggali memang diam di tempat; tanpa pengecualian ini deteksi nyangkut
    menyuruhnya ngeloyor pergi di tengah menambang."""
    i = BOT_JS.index("currentTask !== 'idle' &&")
    assert "currentTask !== 'nambang' &&" in BOT_JS[i:i + 200]


def test_returns_to_previous_task_afterwards():
    i = BOT_JS.index("async mine(c)")
    blok = _kode(BOT_JS[i:BOT_JS.index("\n  kabur()", i)])
    assert "tugasLama" in blok and "setFollow()" in blok


# --- prompt ---------------------------------------------------------------

def test_prompt_is_consistent_about_what_she_can_do():
    """Blok batas kemampuan sempat bertentangan sendiri: bilang bisa nambang
    lalu melarang mengaku menambang."""
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 1400]   # blok kemampuan tumbuh tiap fitur baru
    assert "MENAMBANG blok" in blok
    assert "menambang dan bikin barang boleh kamu ceritakan" in blok
    # Yang masih dilarang tetap dilarang; nambang dan craft TIDAK ikut disebut
    # (craft naik dari "belum bisa" ke "bisa" 2026-08-07).
    larangan = blok[blok.index("Jangan pernah"):blok.index("Untuk yang belum bisa")]
    assert "membangun bangunan berbentuk bebas" in larangan
    # menaruh SATU blok sudah bisa (2026-08-07), jadi tidak boleh ikut dilarang
    for sudah in ("menambang", "craft", "menaruh blok"):
        assert sudah not in larangan, f"masih melarang hal yang sudah bisa: {sudah}"
    assert "[MC: mine" in BRIDGE


# --- isi tas ikut status (2026-08-07) ------------------------------------

def test_status_carries_the_inventory():
    """Panel tas melayang butuh isi tas, dan RCON TIDAK bisa memberikannya:
    server memotong `data get entity <pemain> Inventory` di ~128 karakter
    dengan "..." (dicoba di server nyata 2026-08-07), jadi lewat sana cuma 1-2
    slot pertama yang terbaca. Bot yang memang pemilik datanya."""
    i = BOT_JS.index("function statusEvent")
    blok = _kode(BOT_JS[i:BOT_JS.index("\n// ---------- events", i)])
    assert "inv:" in blok and "bot.inventory.items()" in blok
    assert "held:" in blok, "item yang dipegang juga perlu — itu yang terlihat"


def test_inventory_payload_is_capped():
    """Status dikirim tiap beberapa detik dan masuk ring event; tas penuh
    tanpa batas bikin satu baris NDJSON membengkak terus-menerus."""
    i = BOT_JS.index("inv: bot.inventory.items()")
    assert ".slice(0, 40)" in BOT_JS[i:i + 120]


def test_inventory_entries_are_minimal():
    """Objek Item mineflayer punya puluhan field (nbt, components, dst).
    Mengirim mentah-mentah = ratusan KB per status."""
    i = BOT_JS.index("inv: bot.inventory.items()")
    blok = BOT_JS[i:i + 200]
    assert "name: it.name" in blok and "count: it.count" in blok and "slot: it.slot" in blok
    assert "nbt" not in blok and "components" not in blok
