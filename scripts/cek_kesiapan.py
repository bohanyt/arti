"""Cek kesiapan sebelum sesi live — semua yang bisa diperiksa tanpa server.

Dibuat 2026-08-07 sesudah sebuah edit merusak `arti_bridge.py` dan
dipulihkan lewat `git checkout`. Pemulihan seperti itu bisa diam-diam membuang
perubahan lain, dan suite hijau TIDAK membuktikan semua fitur masih tersambung
ujung ke ujung — banyak tes membaca teks sumber, bukan menjalankan jalurnya.

Yang diperiksa: modul bisa diimpor, kunci CONFIG ada, fungsi bridge ada, tiap
tag `[MC: ...]` benar-benar diurai, tiap handler ada di bot.js, dan semua tugas
yang BERDIRI DIAM dikecualikan dari detektor nyangkut — pola terakhir itu sudah
empat kali membatalkan aksi Arti dengan alasan yang bohong.

    ./venv/Scripts/python.exe scripts/cek_kesiapan.py

Keluar 0 kalau semua lulus, 1 kalau ada yang gagal.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

GAGAL: list[str] = []


def cek(nama: str, ok: bool, catatan: str = "") -> None:
    tanda = "OK   " if ok else "GAGAL"
    print(f"  {tanda} {nama}" + (f"   <- {catatan}" if not ok and catatan else ""))
    if not ok:
        GAGAL.append(nama)


def utama() -> int:
    print("=== modul ===")
    import arti_craft_panel  # noqa: F401
    import arti_minecraft as am
    import arti_session_mode  # noqa: F401
    import arti_spectator as sp
    import arti_bridge as b
    cek("semua modul Minecraft bisa diimpor", True)

    print("=== kunci CONFIG ===")
    for k in ("minecraft_craft_allowlist", "minecraft_place_allowlist",
              "minecraft_craft_panel_enabled", "minecraft_craft_panel_linger_sec",
              "minecraft_craft_panel_max_sec", "minecraft_invsee_enabled",
              "minecraft_invsee_sec", "minecraft_chat_reply",
              "minecraft_chat_reply_gap_sec", "minecraft_spectator_heartbeat_sec"):
        cek(k, k in b.CONFIG, "hilang dari CONFIG")

    print("=== fungsi bridge ===")
    for f in ("_craft_panel_on_event", "_craft_panel_follow", "_invsee_show",
              "_invsee_close", "_spectator_check", "_queue_minecraft_chat_reply"):
        cek(f, hasattr(b, f))

    print("=== tag diurai jadi perintah ===")
    cfg = dict(b.CONFIG)
    contoh = [("[MC: craft stone_pickaxe]", "craft"), ("[MC: place torch]", "place"),
              ("[MC: bangun cobblestone]", "bangun"), ("[MC: serang zombie]", "serang"),
              ("[MC: give examplemcplayer torch 3]", "give"), ("[MC: buka_tas]", "buka_tas"),
              ("[MC: mine oak_log 3]", "mine"), ("[MC: eat]", "eat"),
              ("[MC: kabur]", "kabur")]
    for tag, harap in contoh:
        cmds = am.parse_mc_tags(tag, cfg)[1]
        cek(harap, bool(cmds) and cmds[0]["cmd"] == harap, f"{tag} -> {cmds}")

    js = (pathlib.Path(__file__).resolve().parents[1] / "mc-bot" / "bot.js").read_text(
        encoding="utf-8")

    print("=== handler di bot.js ===")
    i = js.index("const handlers = {")
    blok = js[i:js.index("\n}\n", i)]
    for h in ("craft", "place", "bangun", "serang", "give", "eat", "mine",
              "kabur", "follow", "roam", "come", "stop", "say", "status",
              "turun", "masak", "tidur"):
        cek(f"handler {h}", re.search(rf"^\s+(?:async )?{h} ?\(", blok, re.M) is not None)

    print("=== tugas diam dikecualikan dari detektor nyangkut ===")
    # Pola ini sudah EMPAT KALI membatalkan aksi Arti dengan alasan bohong:
    # craft, naruh, kasih, serang. Tiap tugas baru yang berdiri di tempat
    # WAJIB masuk daftar ini.
    i = js.index("Date.now() - lastMoveAt > STUCK_MS")
    dikecualikan = js[max(0, i - 2000):i]
    for t in ("nambang", "renang", "craft", "naruh", "kasih", "serang", "bangun",
              "turun", "masak", "tidur"):
        cek(f"dikecualikan: {t}", f"currentTask !== '{t}'" in dikecualikan)

    print("=== kamera siaran ===")
    kam = {"minecraft_spectator_name": "CamBot", "minecraft_bot_name": "arti_berarti"}
    cek("kunci mengosongkan tas kamera",
        sp.build_commands(kam)[0].startswith("clear "))
    cek("detak jantung nyala",
        float(b.CONFIG.get("minecraft_spectator_heartbeat_sec", 0) or 0) > 0)
    cek("relog kamera memicu kunci ulang",
        sp.should_resync({"ev": "player_join", "name": "CamBot"}, "overworld", kam)[0])

    print("=== rantai bertahan hidup ===")
    # Lingkaran lapar -> berburu -> makan diverifikasi di server nyata
    # 2026-08-08 (perut 12 -> berburu sapi -> makan -> perut 15). Dua invarian
    # di bawah yang membuatnya mungkin, dan dua-duanya pernah patah.
    i = js.index("async serang (c)")
    serang = js[i:i + 6000]
    cek("hewan pasif tidak bikin dia kabur", "if (!pasif && FLEE_HP > 0" in serang,
        "darah tipis + sasaran sapi = kabur dari sapi, jadi dia tak pernah makan")
    cek("misi arah-tetap dikenali", am.misi_tanpa_finis("survive"))
    cek("misi biasa tetap punya garis finis", not am.misi_tanpa_finis("cari stronghold"))
    # Tangga "aman" (spek Bohan 2026-08-08). Yang diperiksa BUKAN teksnya tapi
    # URUTANNYA: satu anak tangga sekali, darurat mendahului kemajuan, dan diam
    # kalau semuanya sudah terpenuhi.
    _pick = [{"name": "stone_pickaxe", "count": 1}]
    _iron = ["iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots"]
    _lengkap = {"health": 20, "food": 20, "armor": _iron, "inv": _pick + [
        {"name": "beef", "count": 2}, {"name": "shield", "count": 1},
        {"name": "white_bed", "count": 1},
        # "aman total" per busur jelajah: sudah termasuk bekal diamond.
        {"name": "iron_pickaxe", "count": 1}, {"name": "diamond", "count": 3}]}
    cek("tangga aman: mulai dari pickaxe",
        (am.tangga_aman({"health": 20, "food": 20, "inv": [], "armor": []}) or [""])[0]
        .find("pickaxe") >= 0)
    cek("tangga aman: satu anak tangga saja",
        len(am.tangga_aman({"health": 20, "food": 20, "inv": _pick, "armor": []})) <= 1)
    cek("tangga aman: diam kalau sudah aman", am.tangga_aman(_lengkap) == [])
    cek("tangga aman: darurat menang",
        "DARAHMU" in (am.tangga_aman({"health": 3, "inv": [], "armor": []}) or [""])[0])
    cek("tangga aman: tenggelam ditangkap",
        "NAPAS" in (am.tangga_aman(
            {"health": 20, "in_water": True, "oxygen": 3, "inv": [], "armor": []}
        ) or [""])[0].upper())
    cek("status bawa napas/air/armor",
        all(k in js for k in ("oxygen:", "in_water:", "armor: armorDipakai()")))
    cek("masak & tidur cari titik kosong + berhenti dulu",
        "function titikKosong (" in js
        and js.count("await tungguDiam()") >= 3)
    cek("gelap dipakai dari underground, bukan light",
        "underground: langitTerhalang()" in js
        and "coal_ore" in (am.tangga_aman({
            "health": 20, "food": 20, "underground": True, "light": 15,
            "armor": [], "inv": [{"name": "stone_pickaxe", "count": 1},
                                 {"name": "beef", "count": 1}]}) or [""])[0],
        "mineflayer melapor light=15 di dalam batu -> perilaku gelap mati di cave")
    cek("malam + bed -> tidur",
        "tidur" in (am.tangga_aman({
            "health": 20, "food": 20, "is_night": True, "armor": [],
            "inv": [{"name": "white_bed", "count": 1}]}) or [""])[0])
    cek("pengumuman berenang diredam per episode",
        "SWIM_EPISODE_TUTUP_MS" in js and "renangKeluarSejak" in js,
        "tanpa ini satu sesi uji memancarkan 64 swim_start")
    cek("refleks bertahan hidup bisa dimatikan",
        "minecraft_refleks_bertahan" in b.CONFIG and "REFLEKS_AKTIF" in js,
        "perilaku otonom yang belum terbukti wajib punya saklar")
    cek("kabur tidak mengunci refleks",
        "'kabur'" not in js[js.index("const TUGAS_TERLINDUNG"):][:300],
        "kalau terkunci, refleks mati persis saat ditembak skeleton")
    cek("penembak dapat aturan sendiri (jangan lari)",
        "mundur_tembok" in (am.saran_taktis({
            "health": 20, "food": 20, "inv": [], "armor": [],
            "nearby_hostiles": [{"kind": "skeleton", "distance": 9}]}) or [""])[0]
        and "handlers.mundur_tembok" in js,
        "4 dari 8 kematian rendaman oleh skeleton; lari = panah di punggung")
    _gawat = {"health": 6, "food": 7, "is_night": True, "underground": True,
              "armor": [], "inv": [{"name": "stone_pickaxe", "count": 1},
                                   {"name": "beef", "count": 2},
                                   {"name": "cobblestone", "count": 40}],
              "nearby_hostiles": [{"kind": "skeleton", "distance": 9}]}
    cek("nasihat diwasiti maksimal 2 baris", len(am.nasihat(_gawat)) <= 2,
        "4 perintah serentak = pola menu-segalanya yang terbukti gagal")
    cek("misi bawaan arah-tetap terpasang",
        bool(b.CONFIG.get("minecraft_default_goal"))
        and am.misi_tanpa_finis(b.CONFIG["minecraft_default_goal"]),
        "tanpa ini dia masuk dunia tanpa arah, atau bisa keluar sendiri")
    cek("busur jelajah: diamond sesudah aman",
        "DIAMOND" in (am.tangga_aman({
            "health": 20, "food": 20,
            "armor": ["iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots"],
            "inv": [{"name": "stone_pickaxe", "count": 1}, {"name": "beef", "count": 2},
                    {"name": "shield", "count": 1}, {"name": "white_bed", "count": 1},
                    {"name": "iron_pickaxe", "count": 1}]}) or [""])[0])
    cek("alat ukur jelajah di status",
        "jelajah: jarakJelajah()" in js and "bot.world.getBiome" in js)
    for k2 in ("yt_viewer_cooldown_sec", "yt_greet_new_viewers"):
        cek(k2, k2 in b.CONFIG, "anti-spam chat / sapa penonton baru")
    cek("obor kiri saat gelap", "GENGGAM_JEDA_MS" in js)
    for h2 in ("portal", "masuk_portal", "menara"):
        cek(f"handler {h2}",
            f"async {h2} (c)" in js and f'"{h2}"' in
            (pathlib.Path(__file__).resolve().parents[1] / "arti_minecraft.py"
             ).read_text(encoding="utf-8"))
    for k3 in ("minecraft_takdir_enabled", "minecraft_takdir_jeda_sec",
               "minecraft_pembukaan_sec"):
        cek(k3, k3 in b.CONFIG, "mesin takdir")
    cek("kolam takdir valid", all(
        t["id"] and 0 <= t["tier"] <= 6 and isinstance(t["awal"]({}), dict)
        for t in am.TAKDIR_POOL))
    cek("hub event terpasang (takdir+kamera)",
        '"spectator": _mc_event_hub,' in b_src())
    cek("statistik takdir di status bot",
        "statistik: { bunuh:" in js and "biome_n: biomeDikenal.size" in js)
    cek("identitas Bohan & kamera dijelaskan",
        "[SIAPA DI DUNIA INI]" in b_src())

    cek("goal_done dipagari untuk arah tetap",
        "_minecraft_goal_terus" in b_src().split("if verb == \"goal_done\"")[1][:400],
        "tanpa pagar ini misi survive bisa bikin dia keluar dunia di tengah siaran")

    print("=== menu aksi yang dibaca model ===")
    i = b_src().index("[AKSI MINECRAFT]")
    menu = b_src()[i:b_src().index("membaca tag di kalimatmu", i)]
    cek("dipecah jadi baris berlabel", all(
        x in menu for x in ("- gerak :", "- badan :", "- kerja :", "- lain  :")))
    cek("daftar blok tidak diulang", menu.count("_bisa_ditaruh") == 1)

    print()
    if GAGAL:
        print(f"HASIL: {len(GAGAL)} GAGAL -> {GAGAL}")
        return 1
    print("HASIL: SEMUA LULUS")
    return 0


def b_src() -> str:
    return (pathlib.Path(__file__).resolve().parents[1]
            / "arti_bridge.py").read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(utama())
