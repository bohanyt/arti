"""Tests MENARUH BLOK (Phase 2, potongan terakhir sebelum menyerang).

SATU blok di depan kakinya — bukan membangun. Membangun rumah itu urutan
panjang dan tetap "belum bisa"; ini pondasinya, dan sudah cukup untuk yang
benar-benar sering dia butuhkan: menaruh meja craft, peti, obor.

Bedanya dijaga di prompt: "menaruh blok" naik ke BISA, "MEMBANGUN bangunan
utuh" tetap di BELUM BISA. Kalau batas itu kabur, Arti akan mengaku sudah
bikin rumah padahal dia baru menaruh satu batu.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")

CFG = {"minecraft_place_allowlist": ["torch", "crafting_table", "chest"],
       "minecraft_mine_allowlist": ["stone", "coal_ore"]}


def _blok_place() -> str:
    i = BOT_JS.index("async place (c)")
    return BOT_JS[i:BOT_JS.index("\n  kabur()", i)]


def _blok_naruh_satu() -> str:
    """`place` dan `bangun` sama-sama lewat sini, jadi jaminannya dijaga
    di satu tempat — bukan dua salinan yang bisa menyimpang."""
    i = BOT_JS.index("async function naruhSatu")
    return BOT_JS[i:BOT_JS.index(chr(10) + '}', i)]


# --- protokol --------------------------------------------------------------

def test_place_uses_its_own_allowlist_not_the_mining_one():
    """Yang ditambang itu bijih dan batu, yang ditaruh itu meja craft, peti,
    obor. Menyatukan daftarnya berarti dia bisa "menaruh coal_ore" dan TIDAK
    bisa menaruh obor."""
    _, cmds = am.parse_mc_tags("[MC: place torch]", CFG)
    assert cmds == [{"cmd": "place", "block": "torch", "count": 1}]
    assert am.parse_mc_tags("[MC: place stone]", CFG)[1] == [], "blok tambang lolos"
    assert am.parse_mc_tags("[MC: mine torch]", CFG)[1] == [], "blok taruh lolos ke tambang"


def test_place_without_a_block_is_ignored():
    assert am.parse_mc_tags("[MC: place]", CFG)[1] == []


def test_place_is_an_action_and_reaches_the_bot():
    import json
    assert am.TAG_CATEGORIES["place"] == "action"
    assert am.is_owner_only({"cmd": "place", "block": "torch"}) is False
    d = json.loads(am.encode_command({"cmd": "place", "block": "torch", "count": 1}))
    assert d["cmd"] == "place" and d["block"] == "torch"


def test_place_encode_refuses_a_bogus_block_name():
    for jahat in ("", "torch; kill @a", "TORCH", "a" * 60):
        try:
            am.encode_command({"cmd": "place", "block": jahat})
        except ValueError:
            continue
        raise AssertionError(f"nama lolos: {jahat!r}")


# --- reaksi & konteks ------------------------------------------------------

def test_placing_becomes_a_reaction_and_context():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction({"ev": "placed", "block": "torch"}, lim, 100.0, {})
    assert r and "torch" in r
    out = am.format_context(None, [(99.0, {"ev": "placed", "block": "chest"})],
                            ttl_sec=120.0, now=100.0)
    assert "chest" in out and "menaruh" in out


def test_every_place_failure_has_a_human_label():
    for alasan in ("blok_tak_dikenal", "tidak_punya_blok", "tempatnya_terisi",
                   "tidak_ada_pijakan", "gagal_naruh"):
        out = am.format_context(
            None, [(99.0, {"ev": "task_failed", "task": "place", "reason": alasan})],
            ttl_sec=120.0, now=100.0)
        assert alasan not in out, f"jargon bocor: {alasan}"
        assert out.strip(), alasan
    assert "menaruh" in am.task_label("naruh")


# --- bot.js ----------------------------------------------------------------

def test_she_stops_moving_before_the_target_is_computed():
    """REGRESI TERUKUR (server nyata 2026-08-07): sasaran dihitung sambil dia
    masih melangkah, dia bergeser 3,9 blok sebelum paketnya sampai, dan
    hasilnya dia melapor BERHASIL untuk blok yang tidak pernah ada. Melepas
    goal pathfinder saja tidak cukup — momentumnya tidak hilang seketika."""
    b = _blok_place()
    assert "await tungguDiam()" in b
    assert b.index("tungguDiam") < b.index("arahDepan()"), "sasaran dihitung sebelum diam"
    assert b.index("setGoal(null)") < b.index("tungguDiam")


def test_the_placement_is_verified_in_the_world_before_claiming_success():
    """`placeBlock` menunggu SATU blockUpdate, dan update itu bisa saja bukan
    blok kita. Yang dilaporkan ke penonton harus yang benar-benar terjadi —
    pelajaran yang sama dengan menghitung hasil tambang dari selisih isi tas."""
    nn = _blok_naruh_satu()
    i = nn.index("await bot.placeBlock(")
    assert "bot.blockAt(target)" in nn[i:], "tidak diperiksa ulang di dunia"
    assert "gagal_naruh" in nn[i:], "tidak ada jalan keluar kalau bloknya tak ada"
    b = _blok_place()
    assert b.index("const gagal = await naruhSatu(") < b.index("ev: 'placed'")
    assert "if (gagal)" in b


def test_all_six_faces_are_searched_for_a_reference():
    """Dia sering berhenti di TEPI blok — posisi x=0.09 berarti kotak tubuhnya
    ditopang kolom sebelah, jadi "[recorded remark omitted]" bisa saja udara.
    Terukur: tiga kandidat tetap gagal 3x berturut-turut dengan
    'tidak_ada_pijakan' padahal dia berdiri di tanah rata."""
    nn = _blok_naruh_satu()
    assert "const SISI = [" in nn
    sisi = nn[nn.index("const SISI = ["):nn.index("]", nn.index("const SISI = [")) + 400]
    for arah in ("Vec3(0, -1, 0)", "Vec3(0, 1, 0)", "Vec3(1, 0, 0)",
                 "Vec3(-1, 0, 0)", "Vec3(0, 0, 1)", "Vec3(0, 0, -1)"):
        assert arah in sisi, arah


def test_placing_is_not_mistaken_for_being_stuck():
    """Menaruh blok itu berdiri diam — pelajaran yang sama dengan craft, di
    mana detektor nyangkut membatalkan aksinya sendiri."""
    i = BOT_JS.index("Date.now() - lastMoveAt > STUCK_MS")
    dikecualikan = BOT_JS[max(0, i - 1400):i]
    assert "currentTask !== 'naruh' &&" in dikecualikan


def test_every_failure_path_reports_and_restores():
    b, nn = _blok_place(), _blok_naruh_satu()
    for alasan in ("blok_tak_dikenal", "tidak_punya_blok"):
        assert alasan in b, alasan
    for alasan in ("tempatnya_terisi", "tidak_ada_pijakan", "gagal_naruh"):
        assert alasan in nn, alasan
    assert "reason: gagal" in b, "alasan dari naruhSatu tidak diteruskan"
    assert "finally" in b and "setFollow()" in b


def test_she_must_actually_hold_the_block():
    """mineflayer melempar "must be holding an item to place" kalau tangannya
    kosong; cek isi tas duluan supaya alasannya bisa dijelaskan ke penonton."""
    b = _blok_place()
    assert "jumlahDiTas(nama)" in b, "isi tas tidak dicek sebelum mencoba"
    assert b.index("jumlahDiTas(nama)") < b.index("naruhSatu(")
    assert "await bot.equip(" in _blok_naruh_satu()


def test_water_and_lava_are_not_treated_as_free_space():
    """`boundingBox === 'empty'` juga benar untuk air dan lava; menaruh blok
    di lava = blok hilang dan barangnya habis."""
    i = BOT_JS.index("function bolehDitimpa")
    blok = BOT_JS[i:BOT_JS.index("\n}", i)]
    assert "water" in blok and "lava" in blok


# --- prompt & config -------------------------------------------------------

def test_prompt_offers_placing_but_still_denies_building():
    """Batas yang gampang kabur: menaruh SATU blok bisa, menyusun rumah belum.
    Kalau kabur, Arti mengaku sudah bikin rumah padahal baru taruh satu batu."""
    assert "[MC: place" in BRIDGE
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 1400]
    bisa = blok[blok.index("BISA:"):blok.index("BELUM BISA:")]
    belum = blok[blok.index("BELUM BISA:"):blok.index("Jadi menambang")]
    assert "MENARUH SATU BLOK" in bisa
    assert "membangun bangunan berbentuk" in belum, "bangunan bebas harus tetap belum bisa"


def test_place_allowlist_is_separate_from_the_mining_one():
    i = BRIDGE.index('"minecraft_place_allowlist"')
    blok = BRIDGE[i:i + 400]
    assert "crafting_table" in blok and "torch" in blok
    assert '"minecraft_mine_allowlist"' not in blok


# --- MABAR: chat in-game & kasih barang (2026-08-07) ----------------------

class _Runner:
    """MinecraftRunner tanpa subprocess — cukup untuk menguji jalur event."""

    def __new__(cls, cfg, hooks):
        return am.MinecraftRunner(cfg, hooks)


def _jalankan_chat(baris, cfg=None, gap=0.0):
    keluar = []
    c = {"minecraft_streamer_name": "examplemcplayer", "minecraft_chat_reply_gap_sec": gap}
    c.update(cfg or {})
    # Hook dua-argumen (16 Agu): nama None = streamer, selain itu pemain lain.
    r = am.MinecraftRunner(c, {"add_history": lambda *a: None,
                               "streamer_chat":
                               lambda teks, nama=None: keluar.append((nama, teks))})
    import json as _j
    for asal, teks in baris:
        r._handle_line(_j.dumps({"ev": "chat", "from": asal, "text": teks}))
    return keluar


def test_bohan_ingame_chat_actually_gets_a_reply():
    """Sebelum ini chat-nya cuma masuk INGATAN — dia mengingatnya tapi tidak
    pernah membalas, jadi satu-satunya cara ngobrol sambil mabar adalah pegang
    mic. Itu yang bikin mabar terasa pincang."""
    assert _jalankan_chat([("examplemcplayer", "arti tolong tambang batu")]) == \
        [(None, "arti tolong tambang batu")]


def test_pemain_lain_dijawab_TAPI_bukan_sebagai_pemilik():
    """Invariant lama "pemain lain tidak boleh MENYETIR gilirannya" tetap
    hidup tapi pindah lapis (16 Agu, mabar e4mc): chat teman kini MEMICU
    balasan — mereka tidak dengar TTS, chat game satu-satunya kanal — dengan
    NAMA, supaya bridge memberinya trigger_type mc_chat_pemain yang oleh
    is_owner_turn dinilai BUKAN pemilik: tag lifecycle/goal/mode tertolak di
    gate yang sudah ada. Ngobrol boleh, menyetir tetap tidak."""
    hasil = _jalankan_chat([("orang_lain", "arti keluar dari game")])
    assert hasil == [("orang_lain", "arti keluar dari game")]
    assert hasil[0][0] is not None, "nama wajib ikut — tanpa nama dia menyamar jadi Bohan"


def test_blank_chat_is_ignored():
    assert _jalankan_chat([("examplemcplayer", "   "), ("examplemcplayer", "")]) == []


def test_a_burst_of_lines_does_not_become_a_burst_of_turns():
    """Satu balasan = satu panggilan LLM + TTS. Lima baris beruntun tanpa jeda
    jadi lima giliran yang saling menyusul selama setengah menit."""
    keluar = _jalankan_chat([("examplemcplayer", f"baris {i}") for i in range(5)], gap=60.0)
    assert len(keluar) == 1


def test_wake_mode_only_answers_when_she_is_named():
    cfg = {"minecraft_chat_reply": "wake"}
    assert _jalankan_chat([("examplemcplayer", "arti sini dong")], cfg) == [(None, "arti sini dong")]
    assert _jalankan_chat([("examplemcplayer", "gue mau nambang dulu")], cfg) == []


def test_chat_replies_can_be_switched_off():
    assert _jalankan_chat([("examplemcplayer", "arti halo")],
                          {"minecraft_chat_reply": "mati"}) == []


def test_ingame_chat_counts_as_an_owner_turn():
    """Bohan harus bisa menyuruh keluar/ganti misi lewat chat game sama
    seperti lewat mic — pengirimnya sudah disaring nama streamer di runner."""
    import arti_session_mode as sm
    assert sm.is_owner_turn("mc_chat", None, {}) is True
    assert sm.is_owner_turn("yt_chat", "orang_asing", {}) is False


def test_ingame_chat_is_routed_as_mc_chat_not_game():
    """Trigger tipe "game" SENGAJA dibuang saat Arti sibuk (reaksi basi tidak
    layak antre). Pertanyaan langsung dari Bohan tidak boleh ikut hilang."""
    i = BRIDGE.index("def _queue_minecraft_chat_reply")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert 'trigger_type="mc_chat"' in blok
    # Sejak 16 Agu hook-nya lambda dua-argumen: nama None -> jalur Bohan
    # (mc_chat, pemilik), nama terisi -> jalur pemain (mc_chat_pemain, BUKAN
    # pemilik). Dua-duanya wajib masih terhubung.
    assert "_queue_minecraft_chat_pemain(teks, nama)" in BRIDGE
    assert "else _queue_minecraft_chat_reply(teks)" in BRIDGE
    i2 = BRIDGE.index("def _queue_minecraft_chat_pemain")
    blok2 = BRIDGE[i2:BRIDGE.index("\ndef ", i2 + 10)]
    assert 'trigger_type="mc_chat_pemain"' in blok2


def test_ingame_chat_wakes_the_life_detector_and_ends_host_mode():
    """Bohan yang mengetik DI DALAM GAME jelas hadir — mode host harus mundur
    persis seperti kalau dia bersuara."""
    i = BRIDGE.index("def queue_voice_trigger")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert blok.count('"mc_chat"') >= 2, "mc_chat tidak membangunkan kedua detektor"


# --- give ------------------------------------------------------------------

def _blok_give() -> str:
    i = BOT_JS.index("async give (c)")
    return BOT_JS[i:BOT_JS.index("\n  // Naruh blok", i)]


def test_give_accepts_anything_she_can_obtain_not_just_ores():
    """Dulu dipagari daftar NAMBANG saja, jadi obor yang baru dia craft
    sendiri ditolak — padahal itu justru yang wajar dia berikan."""
    cfg = {"minecraft_mine_allowlist": ["stone"],
           "minecraft_place_allowlist": ["torch"],
           "minecraft_craft_allowlist": ["stone_pickaxe"]}
    for barang in ("stone", "torch", "stone_pickaxe"):
        _, cmds = am.parse_mc_tags(f"[MC: give examplemcplayer {barang}]", cfg)
        assert cmds and cmds[0]["item"] == barang, barang
    assert am.parse_mc_tags("[MC: give examplemcplayer diamond_sword]", cfg)[1] == []


def test_give_walks_to_the_person_before_tossing():
    """Minecraft tidak punya serah-terima; yang ada cuma melempar ke tanah.
    Kalau dia melempar dari jauh, barangnya jatuh di tempat dia berdiri."""
    b = _blok_give()
    assert "pathfinder.goto" in b
    assert b.index("pathfinder.goto") < b.index("bot.toss(")


def test_give_reports_what_actually_left_the_bag():
    """Pelajaran yang sama dengan hasil tambang: jumlah dari SELISIH ISI TAS,
    bukan dari yang diminta."""
    b = _blok_give()
    assert "sebelum - jumlahDiTas(nama)" in b
    i = b.index("const lepas")
    assert "gagal_kasih" in b[i:i + 300], "nol barang lepas tetap diklaim berhasil"


def test_give_faces_the_person_so_it_reads_as_handing_over():
    b = _blok_give()
    assert "bot.lookAt(" in b and "1.6" in b


def test_give_reports_every_failure_and_restores():
    b = _blok_give()
    for alasan in ("item_tak_dikenal", "tidak_punya_barang", "orang_tak_kelihatan",
                   "gagal_kasih", "tak_terjangkau", "dibatalkan"):
        assert alasan in b, alasan
    assert "finally" in b and "setFollow()" in b


def test_giving_is_not_mistaken_for_being_stuck():
    i = BOT_JS.index("Date.now() - lastMoveAt > STUCK_MS")
    assert "currentTask !== 'kasih' &&" in BOT_JS[max(0, i - 1400):i]


def test_give_has_labels_and_a_reaction():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction(
        {"ev": "gave", "item": "torch", "player": "examplemcplayer", "count": 3}, lim, 100.0, {})
    assert r and "torch" in r and "examplemcplayer" in r
    for alasan in ("tidak_punya_barang", "orang_tak_kelihatan", "tak_terjangkau",
                   "gagal_kasih"):
        out = am.format_context(
            None, [(99.0, {"ev": "task_failed", "task": "give", "reason": alasan})],
            ttl_sec=120.0, now=100.0)
        assert alasan not in out and out.strip(), alasan


def test_prompt_offers_give():
    assert "[MC: give" in BRIDGE
