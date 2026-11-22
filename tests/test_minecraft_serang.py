"""Tests MENYERANG (Phase 2, potongan terakhir).

Tanpa serang, Arti bisa mati berkali-kali ke zombie tanpa sekali pun
melawan — yang dia punya cuma `kabur`. Untuk siaran solo itu berarti penonton
melihat karakter yang tidak berdaya; untuk mabar, teman mainnya bertarung sendirian.

DITULIS SENDIRI, bukan mineflayer-pvp. Paket itu rilis terakhir 2022-07-03,
menyeret mineflayer-utils dari 2020 yang masih menyatakan butuh mineflayer v2
(di sini v4.37.1), tabel kecepatan serangnya tidak kenal item 1.21, dan dia
merebut goal pathfinder — yang akan bertabrakan dengan logika kabur dan mesin
`currentTask` di bot ini.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


def _blok() -> str:
    i = BOT_JS.index("async serang (c)")
    return BOT_JS[i:BOT_JS.index("\n  // Kasih barang", i)]


# --- protokol --------------------------------------------------------------

def test_attack_tag_with_and_without_a_target():
    assert am.parse_mc_tags("[MC: serang]", {})[1] == [{"cmd": "serang", "target": ""}]
    assert am.parse_mc_tags("[MC: serang zombie]", {})[1] == \
        [{"cmd": "serang", "target": "zombie"}]


def test_attack_target_name_is_sanitised():
    """Nama mob masuk protokol bot; regex tetap ketat walau tanpa allowlist."""
    for jahat in ("aku;kill", "@e", "a" * 40, "Zombie!"):
        assert am.parse_mc_tags(f"[MC: serang {jahat}]", {})[1] == [], jahat
    try:
        am.encode_command({"cmd": "serang", "target": "zombie; kill @a"})
    except ValueError:
        pass
    else:
        raise AssertionError("nama mob berbahaya lolos encode")


def test_attack_is_an_action_not_owner_only():
    assert am.TAG_CATEGORIES["serang"] == "action"
    assert am.is_owner_only({"cmd": "serang", "target": ""}) is False


# --- kejujuran laporan -----------------------------------------------------

def test_a_kill_is_only_claimed_if_our_hit_landed():
    """`entityDead` saja TIDAK cukup: mob bisa mati kena jatuh, lava, atau
    dibunuh orang lain di tengah kita bertarung. Paket `damage_event` (1.20+)
    membawa SUMBER kerusakan, jadi klaim bunuh hanya sah kalau pukulan KITA
    yang terakhir mendarat."""
    b = _blok()
    assert "entityHurt" in b and "entityDead" in b
    assert "sumber.id === bot.entity.id" in b, "sumber kerusakan tidak dicek"
    assert "mati_bukan_olehmu" in b


def test_a_kill_by_someone_else_is_reported_as_such():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction(
        {"ev": "fight_end", "kind": "zombie", "reason": "mati_bukan_olehmu"},
        lim, 100.0, {})
    assert r and "jangan mengaku" in r.lower()


def test_prompt_forbids_claiming_someone_elses_kill():
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 1400]
    assert "BUKAN kena pukulanmu" in blok


def test_listeners_are_removed_afterwards():
    """Satu episode tarung memasang dua listener. Tanpa dilepas, seratus
    pertarungan = dua ratus listener yang menumpuk."""
    b = _blok()
    assert b.count("removeListener") == 2
    assert "finally" in b


# --- perilaku tarung -------------------------------------------------------

def test_creepers_are_refused_not_fought():
    """Memukul creeper jarak dekat = meledak di mukanya. Menghindar itu
    jawaban yang benar, bukan keberanian."""
    b = _blok()
    assert "jangan_creeper" in b
    assert b.index("jangan_creeper") < b.index("senjataTerbaik()")
    out = am.format_context(
        None, [(99.0, {"ev": "task_failed", "task": "serang", "reason": "jangan_creeper"})],
        ttl_sec=120.0, now=100.0)
    assert "jangan_creeper" not in out and "meledak" in out


def test_she_switches_to_the_best_weapon_by_dps_not_raw_damage():
    """Kapak besi memukul 9 tapi cuma 0,9x/dtk (8,1 dps); pedang besi 6 x 1,6
    = 9,6 dps. Memilih dari kerusakan mentah membuatnya memakai senjata yang
    lebih lemah."""
    i = BOT_JS.index("function senjataTerbaik")
    blok = BOT_JS[i:BOT_JS.index("\n}", i)]
    assert "st[0] * st[1]" in blok, "dipilih dari kerusakan saja, bukan dps"
    b = _blok()
    assert b.index("senjataTerbaik()") < b.index("fight_start")


def test_attack_cooldown_is_respected():
    """Memukul sebelum cooldown penuh cuma memberi sebagian kerusakan sejak
    1.9 — spam klik justru MELEMAHKAN dia."""
    i = BOT_JS.index("function jedaSerang")
    assert "1000 /" in BOT_JS[i:i + 200]
    b = _blok()
    assert "pukulBerikutnya" in b and "jedaSerang(bot.heldItem)" in b


def test_she_runs_when_the_fight_is_lost():
    b = _blok()
    assert "FLEE_HP" in b and "fight_lost" in b and "mulaiKabur(" in b


def test_the_fight_cannot_last_forever():
    """Tanpa batas dia mengejar mob yang lari selamanya."""
    b = _blok()
    assert "SERANG_BATAS_MS" in b
    assert "SERANG_BATAS_MS = " in BOT_JS


def test_fighting_is_not_mistaken_for_being_stuck():
    """Bertarung di tempat sempit = berdiri diam. Pelajaran yang sama sudah
    tiga kali menggigit: craft, naruh, kasih."""
    i = BOT_JS.index("Date.now() - lastMoveAt > STUCK_MS")
    assert "currentTask !== 'serang' &&" in BOT_JS[max(0, i - 1600):i]


def test_fleeing_is_not_undone_by_the_cleanup():
    """`finally` mengembalikan tugas lama dan memanggil setFollow(). Kalau itu
    jalan sesudah dia memutuskan kabur, goal kaburnya langsung ditimpa."""
    b = _blok()
    i = b.index("finally")
    assert "hasil !== 'kabur'" in b[i:], "pembersihan menimpa keputusan kabur"


def test_prompt_moves_fighting_into_can_do():
    assert "[MC: serang" in BRIDGE
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 1400]
    bisa = blok[blok.index("BISA:"):blok.index("BELUM BISA:")]
    belum = blok[blok.index("BELUM BISA:"):blok.index("Jadi menambang")]
    assert "MELAWAN musuh" in bisa
    assert "menyerang" not in belum
    assert "membangun bangunan berbentuk" in belum, "membangun bebas harus TETAP belum bisa"


# --- armor & diagnostik meja (2026-08-08) ---------------------------------

def test_armor_goes_on_the_body_not_in_the_hand():
    """Kalau helm cuma dipegang, dari kamera dia kelihatan MENGGENGGAM helm —
    dan pelindungnya tidak berfungsi sama sekali."""
    i = BOT_JS.index("function slotArmor")
    blok = BOT_JS[i:BOT_JS.index(chr(10) + "}", i)]
    for akhiran, slot in (("_helmet", "head"), ("_chestplate", "torso"),
                          ("_leggings", "legs"), ("_boots", "feet")):
        assert akhiran in blok and f"'{slot}'" in blok, akhiran
    assert "'off-hand'" in blok, "shield harus ke tangan kiri"
    # dan craft memakainya, bukan 'hand' terus
    c = BOT_JS[BOT_JS.index("async craft (c)"):]
    c = c[:c.index(chr(10) + "  // Kasih barang")]
    assert "slotArmor(nama)" in c


def test_armor_is_craftable():
    import arti_bridge as b
    daftar = b.CONFIG["minecraft_craft_allowlist"]
    for x in ("iron_helmet", "iron_chestplate", "leather_boots"):
        assert x in daftar, x


def test_craft_records_the_table_distance():
    """Dilaporkan dia craft padahal jauh dari meja. Dicoba
    direproduksi dalam isolasi terverifikasi (0 meja dalam 50 blok) dan dia
    MENOLAK dengan 'tidak_ada_meja'. Tapi tanpa angka jarak di event, kejadian
    berikutnya tetap tidak bisa didiagnosis."""
    c = BOT_JS[BOT_JS.index("async craft (c)"):]
    c = c[:c.index(chr(10) + "  // Kasih barang")]
    i = c.index("ev: 'craft_start'")
    assert "table_dist" in c[i:i + 400]
    assert "distanceTo(meja.position)" in c[i:i + 400]


# --- jelajah: tujuan diperiksa dulu (2026-08-08) --------------------------

def test_roam_targets_are_path_checked_before_being_used():
    """TERUKUR di dua sesi live: 110 dan 101 kali dinyatakan nyangkut, plus 45
    dan 23 kali kecebur. Sebabnya satu — tujuan acak di medan alami sering
    tidak terjangkau, jadi dia mematung 20 dtk, dinyatakan nyangkut, pilih
    tujuan acak baru, ulangi. Akibatnya 51 dari 152 tag-nya cuma `roam`.

    A/B 5 menit jelajah nonstop dari titik identik: 10 nyangkut tanpa
    pemeriksaan, 7 dengan (12 tujuan ditolak). Satu ronde per lengan, jadi
    arahnya jelas tapi kekuatan buktinya sedang."""
    i = BOT_JS.index("function terjangkau")
    blok = BOT_JS[i:BOT_JS.index(chr(10) + "}", i)]
    assert "getPathTo" in blok, "jalur tidak dihitung, cuma diasumsikan"
    assert "'success'" in blok
    j = BOT_JS.index("function pilihTujuanTerjangkau")
    pilih = BOT_JS[j:BOT_JS.index(chr(10) + "}", j)]
    assert "terjangkau(t)" in pilih
    assert "ROAM_COBA" in pilih, "cuma satu kandidat = tidak ada yang bisa dipilih"


def test_the_check_can_be_switched_off_for_A_B():
    """"Kelihatannya membantu" bukan bukti. Saklar ini yang memungkinkan A/B
    tadi, dan perubahan tanpa bukti sudah sekali dibatalkan hari ini."""
    assert "ROAM_TANPA_CEK" in BOT_JS
    i = BOT_JS.index("function terjangkau")
    assert "ROAM_CEK_AKTIF" in BOT_JS[i:i + 200]


def test_a_boxed_in_bot_digs_out_or_keeps_moving():
    """Semua kandidat ditolak -> galiKeluar dulu (2026-08-10, lapis kedua
    epidemi nyangkut). Tapi pelajaran A/B 2026-08-08 tetap dihormati: kalau
    galinya GAGAL dia harus TETAP JALAN — versi yang berdiri diam terukur
    LEBIH BURUK (15 nyangkut vs 10), karena diam dengan task 'roam' cuma
    memancing detektor nyangkut."""
    j = BOT_JS.index("function pilihTujuanTerjangkau")
    pilih = BOT_JS[j:BOT_JS.index(chr(10) + "}", j)]
    assert "return null" in pilih, "buntu harus jujur dilaporkan ke startRoam"
    k = BOT_JS.index("function startRoam")
    roam = BOT_JS[k:BOT_JS.index(chr(10) + "}", k)]
    assert "galiKeluar('terkurung')" in roam
    assert roam.count("pickRoamTarget()") >= 1, (
        "gali gagal -> tetap jalan ke kandidat mentah, jangan mematung")


def test_roam_distance_range_is_adjustable():
    i = BOT_JS.index("function pickRoamTarget")
    blok = BOT_JS[i:BOT_JS.index(chr(10) + "}", i)]
    assert "minJarak" in blok and "maksJarak" in blok


# --- ambang kabur tidak berlaku untuk hewan pasif (2026-08-08) -----------

def test_ambang_kabur_tidak_berlaku_untuk_hewan_pasif():
    """TERUKUR di server nyata 2026-08-08: dengan darah di bawah FLEE_HP dia
    memancarkan fight_start lalu LANGSUNG fight_lost dan "[recorded remark omitted]".

    Itu jebakan bertahan hidup, bukan cuma lucu: saat dia lemah DAN lapar,
    berburu justru yang paling dia butuhkan — tapi aturan kabur memblokirnya,
    jadi dia tidak akan pernah bisa makan dan lingkarannya tidak bisa ditutup.
    Sapi tidak bisa melukainya, jadi tidak ada yang perlu dihindari.
    """
    i = BOT_JS.index("async serang (c)")
    blok = BOT_JS[i:i + 6000]
    assert "const pasif = HEWAN_MAKANAN.has(" in blok
    assert "if (!pasif && FLEE_HP > 0" in blok, (
        "ambang kabur harus dipagari `!pasif`")


def test_musuh_asli_tetap_bisa_bikin_dia_kabur():
    """Pagar di atas tidak boleh kebablasan: yang BISA melukainya harus tetap
    memicu kabur waktu darahnya tipis."""
    i = BOT_JS.index("async serang (c)")
    blok = BOT_JS[i:i + 6000]
    assert "hasil = 'kabur'" in blok
    assert "FLEE_HP" in blok

