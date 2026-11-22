"""Tests kemampuan CRAFT + panel craft melayang (Phase 2).

Urutan yang disepakati Bohan: makan -> nyerang -> pegang item -> craft ->
nambang. Craft dikerjakan belakangan karena butuh dua hal sekaligus: bot yang
benar-benar bisa meng-craft, DAN cara memperlihatkannya. Permintaannya jelas:
"[recorded remark omitted]".
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_craft_panel as cp  # noqa: E402
import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")

CFG = {"minecraft_craft_allowlist": ["wooden_pickaxe", "stick", "oak_planks"]}
GRID_PICKAXE = [["oak_planks"] * 3, [None, "stick", None], [None, "stick", None]]


# --- protokol tag ---------------------------------------------------------

def test_craft_tag_parses_with_item():
    bersih, cmds = am.parse_mc_tags("Bikin dulu ah [MC: craft wooden_pickaxe]", CFG)
    assert cmds == [{"cmd": "craft", "item": "wooden_pickaxe", "count": 1}]
    assert "[MC:" not in bersih


def test_craft_tag_accepts_a_count():
    _, cmds = am.parse_mc_tags("[MC: craft stick 4]", CFG)
    assert cmds[0]["count"] == 4


def test_craft_rejects_items_outside_the_allowlist():
    """Model suka mengarang nama barang. Nama karangan yang lolos ke bot cuma
    jadi 'item_tak_dikenal' beberapa detik kemudian; lebih baik dibuang di sini."""
    _, cmds = am.parse_mc_tags("[MC: craft diamond_stick]", CFG)
    assert cmds == []


def test_craft_without_an_item_is_ignored():
    _, cmds = am.parse_mc_tags("[MC: craft]", CFG)
    assert cmds == []


def test_craft_is_an_action_not_owner_only():
    """Bikin barang bukan perubahan sesi: Arti boleh memutuskannya sendiri."""
    assert am.TAG_CATEGORIES["craft"] == "action"
    assert am.is_owner_only({"cmd": "craft", "item": "stick"}) is False


def test_craft_cannot_collide_with_another_action():
    _, cmds = am.parse_mc_tags("[MC: craft stick] terus [MC: roam]", CFG)
    assert cmds == [{"cmd": "craft", "item": "stick", "count": 1}]


def test_craft_encodes_for_the_bot():
    import json
    d = json.loads(am.encode_command({"cmd": "craft", "item": "stick", "count": 2}))
    assert d == {"cmd": "craft", "item": "stick", "count": 2}


def test_craft_encode_refuses_a_bogus_item_name():
    """Nama item masuk perintah RCON dan protokol bot mentah-mentah."""
    for jahat in ("", "stick; kill @a", "STICK", "a" * 60):
        try:
            am.encode_command({"cmd": "craft", "item": jahat})
        except ValueError:
            continue
        raise AssertionError(f"nama lolos: {jahat!r}")


# --- reaksi & konteks -----------------------------------------------------

def test_craft_start_and_crafted_become_reactions():
    lim = am.ReactionLimiter()
    mulai = am.map_event_to_reaction(
        {"ev": "craft_start", "item": "wooden_pickaxe", "table": True}, lim, 100.0, {})
    assert mulai and "wooden_pickaxe" in mulai
    jadi = am.map_event_to_reaction(
        {"ev": "crafted", "item": "wooden_pickaxe", "count": 1}, lim, 200.0, {})
    assert jadi and "wooden_pickaxe" in jadi


def test_crafted_reaction_hides_raw_numbers():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction(
        {"ev": "crafted", "item": "stick", "count": 4}, lim, 100.0, {})
    assert "4" not in r and "jangan sebut angka" in r.lower()


def test_crafted_but_empty_is_not_reported_as_success():
    """Kalau hasilnya tidak masuk tas, dia tidak boleh bercerita sudah dapat."""
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction({"ev": "crafted", "item": "stick", "count": 0},
                                 lim, 100.0, {})
    assert "berhasil" not in r.lower()


def test_craft_events_appear_in_context_summary():
    for ev, kata in (({"ev": "craft_walk", "item": "chest"}, "meja craft"),
                     ({"ev": "craft_start", "item": "chest"}, "mulai bikin"),
                     ({"ev": "crafted", "item": "chest"}, "selesai bikin")):
        out = am.format_context(None, [(99.0, ev)], ttl_sec=120.0, now=100.0)
        assert kata in out, ev


def test_every_craft_failure_has_a_human_label():
    """Alasan mentah seperti 'meja_tak_terjangkau' bisa terucap apa adanya."""
    for alasan in ("item_tak_dikenal", "tidak_ada_resep", "tidak_ada_meja",
                   "meja_tak_terjangkau", "bahan_kurang", "gagal_craft"):
        out = am.format_context(
            None, [(99.0, {"ev": "task_failed", "task": "craft", "reason": alasan})],
            ttl_sec=120.0, now=100.0)
        assert alasan not in out, f"jargon bocor: {alasan}"
        assert out.strip(), alasan


# --- geometri panel -------------------------------------------------------

def test_up_vector_points_up_at_every_yaw():
    """REGRESI TERUKUR (2026-08-07): 'atas' dihitung sebagai kanan x depan,
    yang menghasilkan (0,-1,0). Panelnya terpasang TERBALIK di server uji:
    judul jatuh ke dasar layar dan stik baris bawah melayang di atas. Urutan
    cross product-nya yang salah, bukan angkanya."""
    for yaw in (0.0, 45.0, 90.0, -43.2, 180.0, -179.9):
        _, _, u = cp.basis(yaw, 0.0)
        assert abs(u[1] - 1.0) < 1e-9, (yaw, u)


def test_panel_stays_upright_and_in_front_when_looking_down():
    """Saat crafting dia PASTI menunduk (mineflayer memanggil lookAt ke meja
    yang ada di bawah setinggi mata), jadi kasus inilah yang penting."""
    for pitch in (0.0, 20.0, 45.0, 89.0):
        f, r, u = cp.basis(30.0, pitch)
        assert u[1] > 0.0, pitch                        # tetap ke atas layar
        assert abs(sum(a * b for a, b in zip(f, u))) < 1e-9   # tegak lurus pandangan
        assert abs(sum(a * b for a, b in zip(f, r))) < 1e-9


def test_panel_body_sits_on_the_eye_line():
    """Keluhan Bohan '[recorded remark omitted]': panel dulu dipatok ke ketinggian tetap
    dari KAKI, jadi pusatnya melayang di atas garis mata."""
    pose = (10.0, 64.0, -20.0, 0.0, 0.0)
    _, y, _ = cp.titik(pose, 0.0, 0.0, 0.0)
    assert abs(y - (64.0 + cp.MATA)) < 1e-9


def test_panel_is_in_front_of_her_not_inside_her_head():
    pose = (10.0, 64.0, -20.0, 0.0, 0.0)
    x, _, z = cp.titik(pose, 0.0, 0.0, 0.0)
    assert math.dist((x, z), (10.0, -20.0)) > 1.0


# --- resep -> tata letak --------------------------------------------------

def test_small_recipes_are_centred_not_stuck_in_the_corner():
    """`inShape` datang apa adanya: papan kayu 1x1, obor 1x2. Tanpa penengahan
    resep kecil menempel di pojok kiri atas dan terlihat seperti bug."""
    g = cp.normalize_grid([["coal"], ["stick"]], 3)      # obor: 1 kolom, 2 baris
    terisi = [(b, k) for b in range(3) for k in range(3) if g[b][k]]
    assert len(terisi) == 2, "bahan hilang atau berlipat"
    assert {k for _, k in terisi} == {1}, "resep 1 kolom tidak di kolom tengah"
    assert g[0][0] is None and g[0][2] is None, "masih menempel di pojok"
    # urutannya tetap: bara di atas, stik di bawah
    assert [g[b][1] for b, _ in terisi] == ["coal", "stick"]


def test_grid_keeps_the_recipe_shape():
    g = cp.normalize_grid(GRID_PICKAXE, 3)
    assert g[0] == ["oak_planks"] * 3
    assert g[1][1] == "stick" and g[2][1] == "stick"
    assert g[1][0] is None and g[1][2] is None


def test_centre_slot_of_the_grid_is_the_centre_of_the_screen():
    for size in (2, 3):
        tengah = (size - 1) / 2.0
        # kotak persis di tengah grid -> offset (0,0)
        dx, dy = cp.slot_offset(tengah, tengah, size)
        assert abs(dx) < 1e-9 and abs(dy) < 1e-9


def test_slot_offsets_read_left_to_right_and_top_to_bottom():
    """Kalau terbalik, resepnya terbaca ngawur oleh penonton."""
    assert cp.slot_offset(0, 0, 3)[0] < cp.slot_offset(0, 2, 3)[0]
    assert cp.slot_offset(0, 0, 3)[1] > cp.slot_offset(2, 0, 3)[1]


def test_two_by_two_recipes_take_less_room_than_three_by_three():
    """Resep 2x2 dikerjakan tanpa meja. Sebarannya harus ikut mengecil, bukan
    kotak 3x3 dengan dua baris kosong."""
    rentang = lambda n: (max(cp.slot_offset(0, k, n)[0] for k in range(n))  # noqa: E731
                         - min(cp.slot_offset(0, k, n)[0] for k in range(n)))
    assert rentang(2) < rentang(3)


def test_there_is_no_background_slab():
    """Bohan 2026-08-07: "[recorded remark omitted]". Lempeng latar juga yang paling kentara menembus
    blok dunia."""
    for c in cp.build_show((0, 64, 0, 0, 0), GRID_PICKAXE, "wooden_pickaxe", 3):
        assert "block_display" not in c


def test_the_panel_sits_inside_her_own_body_space_not_out_in_the_world():
    """REGRESI TERUKUR (2026-08-07): jarak 2.0 blok menaruh panel TEPAT di
    dalam meja craft — dia berhenti ~2 blok dari meja sambil menghadapinya.
    Keluhan Bohan: "[recorded remark omitted]". Di bawah ~1,3 blok itu ruang yang ditempati badannya sendiri,
    dan di situ hampir tidak pernah ada blok."""
    assert cp.JARAK < 1.3


def test_moving_the_panel_closer_does_not_make_it_look_bigger():
    """Ukuran ditulis untuk jarak 2.0 lalu diskalakan. Tanpa itu,
    mendekatkan panel justru membesarkannya dan makin bertabrakan."""
    assert abs(cp._K - cp.JARAK / 2.0) < 1e-12
    assert cp.SLOT < 0.42 and cp.SKALA_ITEM < 0.36


# --- perintah panel -------------------------------------------------------

def test_show_summons_one_entity_per_filled_slot_only():
    """Kotak kosong yang jadi entity = item hantu melayang di udara."""
    cmds = cp.build_show((0, 64, 0, 0, 0), GRID_PICKAXE, "wooden_pickaxe", 3)
    item = [c for c in cmds if c.startswith("summon item_display")]
    assert len(item) == 5 + 1          # 5 bahan + hasil


def test_show_clears_the_previous_panel_first():
    """Craft beruntun tanpa ini = panel menumpuk sampai server penuh entity."""
    assert cp.build_show((0, 64, 0, 0, 0), GRID_PICKAXE, "x", 3)[0].startswith("kill")


def test_every_entity_is_tagged_so_it_can_be_cleaned_up():
    for c in cp.build_show((0, 64, 0, 0, 0), GRID_PICKAXE, "x", 3)[1:]:
        assert f'Tags:["{cp.TAG}"' in c
    assert cp.build_clear() == [f"kill @e[tag={cp.TAG}]"]


def test_ingredients_are_born_bottom_right_so_they_slide_in():
    """Permintaan Bohan: barang meluncur dari kanan bawah ke kotaknya.
    Caranya: lahir di titik masuk, lalu `build_follow` menariknya ke slot;
    `teleport_duration` yang mengubah loncatan jadi luncuran."""
    pose = (0.0, 64.0, 0.0, 0.0, 0.0)
    cmds = cp.build_show(pose, GRID_PICKAXE, "wooden_pickaxe", 3)
    masuk = cp.titik(pose, cp.MASUK_DX, cp.MASUK_DY, -0.10)
    for c in cmds:
        if "item_display" in c and "wooden_pickaxe" not in c:
            xyz = tuple(float(v) for v in
                        re.search(r"item_display (\S+) (\S+) (\S+) ", c).groups())
            # koordinat dicetak 3 desimal -> toleransi harus di atas 0,0005/sumbu
            assert math.dist(xyz, masuk) < 2e-3, "bahan tidak lahir di kanan bawah"
    assert cp.MASUK_DX > 0 and cp.MASUK_DY < 0


def test_the_result_does_not_slide_it_just_appears():
    pose = (0.0, 64.0, 0.0, 0.0, 0.0)
    c = next(x for x in cp.build_show(pose, GRID_PICKAXE, "wooden_pickaxe", 3)
             if "wooden_pickaxe" in x and "item_display" in x)
    xyz = tuple(float(v) for v in
                re.search(r"item_display (\S+) (\S+) (\S+) ", c).groups())
    assert math.dist(xyz, cp.titik(pose, cp.MASUK_DX, cp.MASUK_DY, -0.10)) > 0.5


def test_everything_interpolates_so_the_panel_does_not_stutter():
    """Panel di-tp ~3x/detik. Tanpa teleport_duration dia melangkah patah-patah
    - persis keluhan '[recorded remark omitted]' di renderer web dulu."""
    for c in cp.build_show((0, 64, 0, 0, 0), GRID_PICKAXE, "x", 3)[1:]:
        assert "teleport_duration:" in c


def test_follow_moves_every_part_that_show_created():
    grid = cp.normalize_grid(GRID_PICKAXE, 3)
    dibuat = {n for n, _, _, _ in cp.bagian(grid, 3)}
    digerakkan = {re.search(rf"{cp.TAG}_(\w+)", c).group(1)
                  for c in cp.build_follow((0, 64, 0, 0, 0), GRID_PICKAXE, 3)}
    assert dibuat == digerakkan


def test_title_and_result_stack_above_the_grid():
    grid = cp.normalize_grid(GRID_PICKAXE, 3)
    posisi = {n: dy for n, _, dy, _ in cp.bagian(grid, 3)}
    tertinggi = max(dy for n, dy in posisi.items() if n.startswith("s"))
    assert posisi["hasil"] > tertinggi
    assert posisi["judul"] > posisi["hasil"]


def test_title_text_cannot_break_out_of_the_nbt():
    """Judul dirakit dari nama item lalu masuk NBT JSON. Kutip atau backslash
    liar merusak perintahnya dan jadi celah injeksi."""
    j = cp.judul_untuk('nakal" ,extra:1, "x\\y')
    assert '"' not in j and "\\" not in j and "'" not in j
    c = next(x for x in cp.build_show((0, 64, 0, 0, 0), [["stick"]], 'a"b', 2)
             if "text_display" in x)
    assert c.count("'") == 2, "kutip tunggal pembungkus NBT bocor"


def test_grid_validation_rejects_anything_that_is_not_an_item_name():
    assert cp.grid_valid(GRID_PICKAXE, 3)
    assert cp.grid_valid([["stick", None], [None, "stick"]], 2)
    assert not cp.grid_valid([["stick; kill @a"]], 3)
    assert not cp.grid_valid([["A"] * 4], 3), "grid lebih lebar dari panel"
    assert not cp.grid_valid([], 3)
    assert not cp.grid_valid("stick", 3)


def test_pose_parser_reads_the_actual_server_reply():
    """Balasan sukses berbunyi 'has the following entity data'; kata 'Pos'
    TIDAK muncul. Pengecekan lama yang mencari 'Pos' selalu gagal."""
    x, y, z, yaw, pitch = cp.parse_pose(
        "Arti has the following entity data: [53.5d, 78.0d, -13.5d]",
        "Arti has the following entity data: [-43.185234f, 12.5f]")
    assert (x, y, z) == (53.5, 78.0, -13.5)
    assert abs(yaw + 43.185234) < 1e-4 and pitch == 12.5


# --- bot.js ---------------------------------------------------------------

def _blok(nama: str) -> str:
    i = BOT_JS.index(nama)
    return BOT_JS[i:BOT_JS.index("\n  },", i)]


def test_bot_tries_the_2x2_recipe_before_hunting_for_a_table():
    """Stik dan papan muat di inventory. Menyeret dia ke meja untuk itu bikin
    dia jalan jauh tanpa alasan."""
    b = _blok("async craft (c)")
    i2 = b.index("recipesFor")
    assert "null, minta, null" in b[i2:i2 + 60], "panggilan 2x2 tidak tanpa meja"
    assert b.index("recipesFor") < b.index("findBlock")


def test_bot_walks_to_the_table_and_reports_if_it_cannot():
    b = _blok("async craft (c)")
    assert "pathfinder.goto" in b
    for alasan in ("tidak_ada_meja", "meja_tak_terjangkau", "bahan_kurang",
                   "item_tak_dikenal", "gagal_craft", "tidak_ada_resep"):
        assert alasan in b, alasan


def test_bot_stops_walking_before_crafting():
    """Orang crafting itu berhenti. Keluhan Bohan waktu purwarupa panel:
    '[recorded remark omitted]'."""
    b = _blok("async craft (c)")
    assert "bot.pathfinder.setGoal(null)" in b
    assert b.index("setGoal(null)") < b.index("craft_start")


def test_bot_sends_the_recipe_before_crafting_not_after():
    """Panel butuh resepnya di depan; kalau dikirim belakangan penonton cuma
    lihat hasilnya muncul tanpa tahu bahannya apa."""
    b = _blok("async craft (c)")
    assert b.index("craft_start") < b.index("await bot.craft(")
    assert "grid: bentukResep(resep)" in b


def test_bot_holds_the_result_so_the_camera_can_see_it():
    b = _blok("async craft (c)")
    assert "bot.equip(" in b and b.index("bot.equip(") > b.index("await bot.craft(")


def test_equip_failure_does_not_turn_a_success_into_a_failure():
    """Tangan penuh bukan berarti craft-nya gagal."""
    b = _blok("async craft (c)")
    i = b.index("bot.equip(")
    assert "catch" in b[i - 60:i + 120]


def test_bot_restores_whatever_it_was_doing_afterwards():
    b = _blok("async craft (c)")
    assert "finally" in b and "setFollow()" in b


def test_standing_still_while_crafting_is_not_mistaken_for_being_stuck():
    """REGRESI TERUKUR (server nyata 2026-08-07): detektor nyangkut
    mengecualikan idle/wait_streamer/nambang/renang tapi TIDAK craft. Karena
    Arti sudah berdiri diam sejak craft sebelumnya, timer nyangkutnya sudah
    jatuh tempo, jadi 2 ms sesudah dia mulai jalan ke meja detektor memanggil
    startRoam('unstuck'), goal-nya ganti, dan craft-nya batal. Berdiri diam
    ITU perilaku yang benar saat crafting."""
    i = BOT_JS.index("Date.now() - lastMoveAt > STUCK_MS")
    # Jendela sampai 3600: daftar pengecualian tugas-diam TUMBUH tiap
    # kemampuan baru dan sudah beberapa kali mendorong baris keluar jendela.
    dikecualikan = BOT_JS[max(0, i - 3600):i]
    for tugas in ("nambang", "craft"):
        assert f"currentTask !== '{tugas}'" in dikecualikan, tugas


def test_an_interrupted_walk_is_not_reported_as_an_unreachable_table():
    """`stop`, kabur dari mob, atau deteksi nyangkut bisa mengambil alih
    tujuannya di tengah jalan. Itu bukan salah mejanya, dan Arti tidak boleh
    menceritakannya begitu ke penonton."""
    b = _blok("async craft (c)")
    assert "dibatalkan" in b
    i = b.index("dibatalkan")
    assert "goal was changed" in b[max(0, i - 200):i + 200]


def test_the_cancelled_reason_has_a_human_label():
    """Tanpa label, alasan mentah masuk prompt dan bisa terucap apa adanya."""
    assert "dibatalkan" in am._REASON_LABEL
    out = am.format_context(
        None, [(99.0, {"ev": "task_failed", "task": "craft", "reason": "dibatalkan"})],
        ttl_sec=120.0, now=100.0)
    assert out.strip() and am._REASON_LABEL["dibatalkan"] in out
    # dan yang penting: tidak menuduh mejanya
    assert "terjangkau" not in out


def test_recipe_shape_marks_empty_slots_as_empty():
    """Slot kosong di `inShape` bertanda id -1, BUKAN null. Salah baca = lima
    bahan berubah jadi sembilan dan panelnya bohong."""
    b = BOT_JS[BOT_JS.index("function bentukResep"):]
    b = b[:b.index("\n}\n")]
    assert "id < 0" in b or "id === -1" in b
    assert "ingredients" in b, "resep bebas bentuk (papan kayu) tidak tertangani"


# --- wiring bridge --------------------------------------------------------

def test_bridge_registers_the_panel_hook():
    assert '"craft_panel": _craft_panel_on_event' in BRIDGE


def test_panel_never_calls_rcon_on_the_event_reader_thread():
    """RCON bisa menggantung, dan pemanggil hook itu thread pembaca event bot.
    Menahannya = menahan SELURUH aliran event game. Panel itu hiasan."""
    i = BRIDGE.index("def _craft_panel_on_event")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "_craft_panel_rcon" not in blok, "RCON dipanggil di jalur pembaca"
    assert "threading.Thread" in blok


def test_panel_clears_itself_even_when_the_follow_loop_dies():
    i = BRIDGE.index("def _craft_panel_follow")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "finally" in blok and "build_clear" in blok


def test_panel_survives_arti_disappearing_mid_craft():
    """Mati/respawn di tengah craft: RCON gagal membaca posisinya. Panel tidak
    boleh bubar karena satu bacaan gagal, dia keburu balik."""
    i = BRIDGE.index("def _craft_panel_follow")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "continue" in blok


def test_panel_validates_the_recipe_before_building_commands():
    """Nama item dari bot masuk perintah RCON mentah-mentah."""
    i = BRIDGE.index("def _craft_panel_on_event")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "grid_valid" in blok


def test_panel_goes_away_on_success_failure_and_death():
    i = BRIDGE.index("def _craft_panel_on_event")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    for kind in ("crafted", "task_failed", "death"):
        assert f'"{kind}"' in blok, kind


def test_a_second_craft_replaces_the_first_panel():
    i = BRIDGE.index("def _craft_panel_on_event")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    j = blok.index("craft_start")
    assert "_craft_panel_stop.set()" in blok[j:], "panel lama tidak dibubarkan"


def test_prompt_offers_crafting_and_no_longer_denies_it():
    assert "[MC: craft" in BRIDGE or "craft <" in BRIDGE
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 900]
    bisa = blok[blok.index("BISA:"):blok.index("BELUM BISA:")]
    assert "craft" in bisa.lower(), "batas kemampuan masih bilang dia tidak bisa craft"
    belum = blok[blok.index("BELUM BISA:"):blok.index("Jadi menambang")]
    assert "craft" not in belum.lower(), "craft masih terdaftar sebagai belum bisa"


def test_craft_allowlist_is_items_not_blocks():
    """Daftar nambang berisi blok; pickaxe tidak akan pernah lolos di situ."""
    i = BRIDGE.index('"minecraft_craft_allowlist"')
    blok = BRIDGE[i:i + 500]
    assert "wooden_pickaxe" in blok
    assert '"minecraft_mine_allowlist"' not in blok


# --- momen "[recorded remark omitted]" (invsee di layar kamera) -------------------------------

import arti_spectator as sp  # noqa: E402

KAM = {"minecraft_spectator_name": "CamBot", "minecraft_bot_name": "arti_berarti"}


def test_buka_tas_tag_parses_and_is_an_action():
    bersih, cmds = am.parse_mc_tags("Bentar cek tas dulu [MC: buka_tas]", {})
    assert cmds == [{"cmd": "buka_tas"}]
    assert "[MC:" not in bersih
    assert am.TAG_CATEGORIES["buka_tas"] == "action"


def test_buka_tas_is_never_sent_to_the_bot():
    """Bot tidak melakukan apa pun — ini murni efek di layar kamera lewat
    RCON. Kalau ikut dikirim, bot cuma membalas 'unknown cmd'."""
    assert "buka_tas" not in am.SEND_VERBS
    assert "buka_tas" in am.TAG_VERBS


def test_opening_always_closes_whatever_was_left_open_first():
    """REGRESI TERUKUR: tidak ada perintah vanilla yang menutup GUI pemain
    lain (diuji di server 2026-08-07: tp, gamemode, spectate, blindness, naik
    entitas, damage, clear — semua gagal; cuma pindah dimensi & mati yang
    berhasil). Jadi kalau sesi sebelumnya berhenti di tengah jalan, GUI lama
    masih menempel di layar penonton dan harus disapu duluan."""
    cmds = sp.invsee_open_commands(KAM)
    sapu = next(i for i, c in enumerate(cmds) if c.startswith("tutuptas "))
    buka = next(i for i, c in enumerate(cmds) if "invsee" in c)
    assert sapu < buka, "menyapu GUI lama SESUDAH membuka yang baru"


def test_opening_uses_sudo_not_execute_as():
    """`execute as <p> run invsee` DITOLAK server: semua plugin invsee
    mendaftarkan perintahnya lewat plugin.yml gaya lama dan menolak pengirim
    yang bukan Player. Dibuktikan di bytecode InvSee++ & OpenInv, lalu
    dikonfirmasi langsung ("This command can only be used by players!")."""
    cmds = sp.invsee_open_commands(KAM)
    buka = next(c for c in cmds if "invsee" in c)
    assert buka.startswith("sudo player CamBot invsee ")
    assert "execute as" not in buka


def test_camera_and_target_are_never_the_same_player():
    assert sp.invsee_open_commands(
        {"minecraft_spectator_name": "Arti", "minecraft_bot_name": "Arti"}) == []


def test_invsee_is_off_when_there_is_no_camera():
    """Momen ini terjadi DI LAYAR kamera — tanpa kamera tidak ada yang lihat."""
    assert sp.invsee_open_commands({"minecraft_bot_name": "Arti"}) == []
    assert sp.invsee_close_commands({}) == []
    assert sp.invsee_enabled({"minecraft_bot_name": "Arti"}) is False


def test_invsee_can_be_switched_off_while_the_camera_stays_on():
    mati = dict(KAM, minecraft_invsee_enabled=False)
    assert sp.invsee_open_commands(mati) == []
    assert sp.is_enabled(mati) is True, "kamera ikut mati padahal cuma invsee"


def test_display_duration_is_clamped():
    """Salah setel = GUI menutupi siaran berjam-jam."""
    assert sp.invsee_seconds(dict(KAM, minecraft_invsee_sec=9999)) == sp.INVSEE_SEC_MAX
    assert sp.invsee_seconds(dict(KAM, minecraft_invsee_sec=0)) == sp.INVSEE_SEC_MIN
    assert sp.invsee_seconds(dict(KAM, minecraft_invsee_sec="bukan angka")) > 0


def test_bridge_always_schedules_a_close():
    """Tanpa penutup, GUI menempel di layar penonton SELAMANYA."""
    i = BRIDGE.index("def _invsee_show")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "threading.Timer" in blok and "_invsee_close" in blok


def test_bridge_closes_the_screen_when_arti_dies():
    i = BRIDGE.index("def _craft_panel_on_event")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "_invsee_close" in blok


def test_a_second_request_does_not_stack_two_closers():
    """Dua timer yang saling mendahului = GUI ditutup lebih cepat dari yang
    diminta, atau ditutup dua kali."""
    i = BRIDGE.index("def _invsee_show")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "_invsee_timer.cancel()" in blok


def test_bridge_never_calls_rcon_on_the_caller_thread():
    for nama in ("def _invsee_show", "def _invsee_close"):
        i = BRIDGE.index(nama)
        blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
        assert "threading.Thread" in blok, nama


def test_arti_is_told_so_she_can_mention_it():
    """Tanpa ini isi tasnya nongol di layar penonton tanpa dia menyinggungnya."""
    i = BRIDGE.index("def _invsee_show")
    blok = BRIDGE[i:BRIDGE.index("\ndef ", i + 10)]
    assert "inventory_shown" in blok
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction({"ev": "inventory_shown"}, lim, 100.0, {})
    assert r and "tas" in r.lower()


def test_prompt_offers_the_tag():
    assert "[MC: buka_tas]" in BRIDGE


# --- audit 2026-08-07: cacat yang ditemukan dengan MENJALANKAN thread-nya ---

import threading  # noqa: E402
import time as _time  # noqa: E402

import pytest  # noqa: E402

POSE = ["Arti has the following entity data: [10.0d, 64.0d, -20.0d]",
        "Arti has the following entity data: [0.0f, 0.0f]"]


@pytest.fixture
def panel(monkeypatch):
    """Bridge dengan RCON palsu — panel benar-benar dijalankan, tanpa server."""
    import arti_bridge as b
    jejak = []

    def _rcon(cmds):
        jejak.append(list(cmds))
        return POSE if any("data get entity" in c for c in cmds) else [""] * len(cmds)

    monkeypatch.setattr(b, "_craft_panel_rcon", _rcon)
    monkeypatch.setitem(b.CONFIG, "minecraft_craft_panel_enabled", True)
    monkeypatch.setitem(b.CONFIG, "minecraft_craft_panel_linger_sec", 5.0)
    monkeypatch.setattr(b, "_craft_panel_stop", None, raising=False)
    monkeypatch.setattr(b, "_craft_panel_timer", None, raising=False)
    yield b, jejak
    if getattr(b, "_craft_panel_timer", None) is not None:
        b._craft_panel_timer.cancel()
    if b._craft_panel_stop is not None:
        b._craft_panel_stop.set()
    _time.sleep(0.5)


def _pengikut_hidup():
    return [t for t in threading.enumerate()
            if t.name == "craft-panel" and t.is_alive()]


def _mulai(b, hasil="stick"):
    b._craft_panel_on_event({"ev": "craft_start", "size": 2, "grid": [["oak_planks"]],
                             "result": hasil, "item": hasil})


def test_back_to_back_crafts_never_leave_two_followers(panel):
    """REGRESI TERUKUR (audit 2026-08-07): pada `crafted` acuan stop dibuang
    padahal thread-nya masih hidup selama linger 5 dtk. Craft berikutnya di
    dalam jendela itu tidak punya cara menghentikannya, jadi DUA pengikut
    berebut entity yang sama. Terjadi betulan di uji live sesi ini: papan lalu
    peti berjarak 4 detik."""
    b, _ = panel
    _mulai(b, "stick")
    _time.sleep(0.4)
    assert len(_pengikut_hidup()) == 1
    b._craft_panel_on_event({"ev": "crafted", "item": "stick", "count": 4})
    _mulai(b, "chest")           # masih di dalam linger 5 dtk
    _time.sleep(0.6)
    assert len(_pengikut_hidup()) == 1, "pengikut yatim dari craft sebelumnya"


def test_the_pending_closer_does_not_kill_the_next_panel(panel):
    """Timer penutup craft LAMA tidak boleh membubarkan panel craft BARU."""
    b, _ = panel
    monkey_linger = 0.4
    b.CONFIG["minecraft_craft_panel_linger_sec"] = monkey_linger
    _mulai(b, "stick")
    _time.sleep(0.3)
    b._craft_panel_on_event({"ev": "crafted", "item": "stick", "count": 4})
    _mulai(b, "chest")                       # panel baru
    _time.sleep(monkey_linger + 0.5)         # penutup lama sudah lewat jatuh tempo
    assert len(_pengikut_hidup()) == 1, "panel baru ikut terbunuh penutup lama"


def test_panel_is_cleared_after_the_linger(panel):
    b, jejak = panel
    b.CONFIG["minecraft_craft_panel_linger_sec"] = 0.3
    _mulai(b)
    _time.sleep(0.4)
    b._craft_panel_on_event({"ev": "crafted", "item": "stick", "count": 4})
    _time.sleep(1.0)
    assert not _pengikut_hidup()
    assert any(any("kill @e[tag=" in c for c in cmds) for cmds in jejak[-2:]), \
        "panel tidak dibersihkan"


def test_closer_timer_never_holds_the_bridge_open(panel):
    """threading.Timer defaultnya BUKAN daemon: linger 5 dtk akan menunda
    keluarnya bridge selama itu."""
    b, _ = panel
    _mulai(b)
    _time.sleep(0.3)
    b._craft_panel_on_event({"ev": "crafted", "item": "stick", "count": 4})
    t = getattr(b, "_craft_panel_timer", None)
    assert t is not None, "tidak ada timer penutup yang tersimpan"
    assert t.daemon, "timer menahan proses keluar"


def test_panel_gives_up_if_the_closing_event_never_arrives(panel):
    """`crafted`/`task_failed`/`death` datang DARI BOT. Kalau prosesnya mati
    mendadak tidak ada yang datang, dan panelnya menggantung di dunia."""
    b, _ = panel
    b.CONFIG["minecraft_craft_panel_max_sec"] = 0.5
    _mulai(b)
    _time.sleep(1.4)
    assert not _pengikut_hidup(), "panel menggantung tanpa batas waktu"


def test_death_closes_the_camera_screen_even_with_the_panel_off(monkeypatch):
    """Panel craft dan momen "[recorded remark omitted]" itu dua fitur berbeda. Mematikan panel
    tidak boleh diam-diam meninggalkan GUI invsee menempel di layar penonton."""
    import arti_bridge as b
    dipanggil = []
    monkeypatch.setattr(b, "_invsee_close", lambda *a, **k: dipanggil.append(1))
    monkeypatch.setitem(b.CONFIG, "minecraft_craft_panel_enabled", False)
    b._craft_panel_on_event({"ev": "death"})
    assert dipanggil, "GUI kamera tidak ditutup saat Arti mati"


def test_materials_are_checked_before_she_walks_to_the_table():
    """TERUKUR di audit 2026-08-07: dia jalan 25 blok ke meja lalu baru lapor
    'bahan_kurang'. Buang waktu, dan di siaran terlihat seperti dia tidak tahu
    isi tasnya sendiri. Cek bahan tidak butuh dia berdiri di sebelah mejanya."""
    b = _blok("async craft (c)")
    i_cek = b.index("recipesFor(jenis.id, null, minta, posMeja)")
    i_jalan = b.index("craft_walk")
    assert i_cek < i_jalan, "masih jalan dulu baru cek bahan"
    assert b.index("bahan_kurang") < i_jalan, "laporan bahan kurang datang terlambat"


def test_recipe_is_rechecked_after_arriving():
    """Dia bisa kehilangan bahan di perjalanan (mati, jatuh ke lava), dan
    mejanya bisa hilang (chunk dimuat ulang, ada yang menghancurkannya)."""
    b = _blok("async craft (c)")
    sesudah = b[b.index("await bot.pathfinder.goto"):]
    assert "recipesFor" in sesudah, "resep tidak dipilih ulang sesudah sampai"
    assert "meja_hilang" in sesudah


def test_the_missing_table_reason_has_a_human_label():
    out = am.format_context(
        None, [(99.0, {"ev": "task_failed", "task": "craft", "reason": "meja_hilang"})],
        ttl_sec=120.0, now=100.0)
    assert "meja_hilang" not in out and out.strip()


def test_she_stands_still_while_the_panel_is_up():
    """Keluhan Bohan 2026-08-07 saat meninjau: "[recorded remark omitted]". Craft-nya cuma ~40 ms lalu dia langsung ngeloyor, jadi panel
    resepnya ikut meluncur sambil dia jalan. Jedanya diambil dari durasi panel
    supaya dua-duanya tidak bisa lepas sinkron."""
    b = _blok("async craft (c)")
    assert "CRAFT_PAUSE_MS" in b
    assert b.index("emit({ ev: 'crafted'") < b.index("CRAFT_PAUSE_MS")
    assert "--craft-pause-sec" in (ROOT / "arti_minecraft.py").read_text(encoding="utf-8")
    src = (ROOT / "arti_minecraft.py").read_text(encoding="utf-8")
    i = src.index("--craft-pause-sec")
    assert "minecraft_craft_panel_linger_sec" in src[i:i + 200], \
        "jeda tidak diikat ke durasi panel — dua angka yang bisa lepas sinkron"


def test_the_pause_is_clamped_and_skipped_on_failure():
    b = _blok("async craft (c)")
    assert "Math.min(20" in BOT_JS[BOT_JS.index("CRAFT_PAUSE_MS"):][:200], "jeda tak dibatasi"
    # Jeda ada di jalur SUKSES, bukan di penanganan gagal: craft yang gagal
    # tidak boleh ikut membekukan dia. (Jangan cari "} catch (e) {" terdekat —
    # yang ketemu duluan itu catch milik `equip`, bukan catch luarnya.)
    assert b.index("CRAFT_PAUSE_MS") < b.index("reason: 'gagal_craft'")


def test_the_camera_bag_is_emptied_before_showing():
    """Resource pack `ZZ-Kamera-UI` membuat kotak inventory bawah TRANSPARAN
    supaya layarnya tidak terlihat seperti double chest. Tapi slotnya masih
    ada: barang apa pun yang dipegang akun kamera akan tampil sebagai ikon
    MELAYANG di udara tanpa kotak. Pertanyaan Bohan 2026-08-07: "[recorded remark omitted]"."""
    cmds = sp.invsee_open_commands(KAM)
    assert cmds[0] == "clear CamBot"
    assert cmds.index("clear CamBot") < next(
        i for i, c in enumerate(cmds) if "invsee" in c)


def test_clearing_only_ever_targets_the_camera_never_arti():
    """Salah sasaran di sini = seluruh isi tas Arti hilang di depan penonton."""
    for c in sp.invsee_open_commands(KAM) + sp.invsee_close_commands(KAM):
        if c.startswith("clear "):
            assert c.split()[1] == "CamBot", c
            assert "arti" not in c.lower(), c
