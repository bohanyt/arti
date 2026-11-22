"""Penglihatan sumber daya + berlindung malam (Bohan 2026-08-12).

Dua keluhan yang saling menyambung dari live 22.10-22.50 (mati 10x dalam 40
menit, telanjang di malam pertama):

1. "[recorded remark omitted]" — status
   dulu hanya memuat entity (hewan cuma 16 blok) + isi tas. Arti BUTA sumber
   daya, jadi saat disuruh cari kayu dia menjawab jujur "[recorded remark omitted]" padahal hutan bisa 25 blok di sebelahnya.
2. "[recorded remark omitted]" — refleks berlindung
   menunggu darah <=12, padahal tanpa senjata/armor dia habis dari darah
   PENUH; dan `bangun` butuh ~20 blok yang tidak dia punya.
"""
from pathlib import Path

import arti_minecraft as am

ROOT = Path(__file__).resolve().parent.parent
BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")


def _st(**over):
    st = {"inv": [], "health": 20, "food": 20, "task": "roam",
          "dim": "overworld", "pos": {"x": 0, "y": 70, "z": 0},
          "is_night": True, "armor": [], "nearby_players": [],
          "nearby_hostiles": [], "nearby_animals": []}
    st.update(over)
    return st


# --- penglihatan -----------------------------------------------------------

def test_penglihatan_menyebut_jenis_dan_jarak():
    st = _st(terlihat={"pohon": {"kind": "spruce_log", "distance": 23},
                       "batu": {"kind": "stone", "distance": 9},
                       "batu_bara": {"kind": "coal_ore", "distance": 14},
                       "besi": {"kind": "iron_ore", "distance": 20}})
    line = am.penglihatan_line(st)
    assert "spruce" in line and "23 blok" in line
    assert "batu 9 blok" in line and "batu bara 14 blok" in line
    assert "bijih besi 20 blok" in line
    assert "jangan bilang tidak ada" in line


def test_penglihatan_kosong_tidak_mengarang():
    assert am.penglihatan_line(_st()) == ""
    assert am.penglihatan_line(_st(terlihat={})) == ""
    assert am.penglihatan_line(None) == ""


def test_penglihatan_masuk_konteks_prompt():
    st = _st(terlihat={"pohon": {"kind": "oak_log", "distance": 12}})
    ctx = am.format_context(st, [], ttl_sec=120.0, now=100.0)
    assert "KELIHATAN" in ctx and "12 blok" in ctx


def test_nudge_kayu_memakai_jarak_yang_dilihat():
    """Dulu: "tebang pohon terdekat" tanpa memberi tahu DI MANA."""
    st = _st(terlihat={"pohon": {"kind": "birch_log", "distance": 18}})
    s = am.tangga_aman(st)[0]
    assert "MELIHAT birch_log 18 blok" in s
    assert "[MC: mine birch_log 6]" in s
    # tanpa penglihatan, nudge lama tetap dipakai (jangan mengarang jarak)
    assert "MELIHAT" not in am.tangga_aman(_st())[0]


def test_radius_scan_entity_diperbesar():
    i = BOT_JS.index("function nearbyEntities")
    blok = BOT_JS[i:i + 2400]
    assert "if (d > 32) continue" in blok, "hewan pada 16 blok = buta makanan"
    assert "hostiles.slice(0, 6)" in blok, "batasi jumlah, jangan status bengkak"
    assert "hewan.slice(0, 4)" in blok


def test_pandangan_di_cache_agar_tidak_boros():
    i = BOT_JS.index("function pandangan")
    blok = BOT_JS[i - 900:i + 900]
    assert "PANDANG_CACHE_MS" in blok, "findBlock radius 40 tiap status = sampah GC"
    assert "PANDANG_JAUH = 40" in BOT_JS
    assert "terlihat: pandangan()" in BOT_JS


# --- berlindung malam ------------------------------------------------------

def test_refleks_sembunyi_malam_sebelum_darah_tipis():
    i = BOT_JS.index("MALAM TELANJANG")
    blok = BOT_JS[i:i + 1600]
    assert "timeOfDay > 12500" in blok, "hanya malam"
    assert "adaSenjata" in blok, "yang bahaya itu telanjang, bukan malam saja"
    assert "handlers.lubang_aman()" in blok
    assert "handlers.bangun" in blok, "punya blok banyak -> shelter penuh"
    assert "lubangTerakhir > 90000" in blok, "jangan menggali lubang tiap detik"


def test_lubang_aman_tanpa_modal_dan_jujur():
    i = BOT_JS.index("async lubang_aman")
    blok = BOT_JS[i:i + 3000]
    assert "bot.dig(bawah)" in blok
    assert "CAIRAN.has(bawah.name)" in blok, "jangan menggali ke air/lava"
    assert "tertutup" in blok and "boundingBox === 'block'" in blok, (
        "tutup atas diverifikasi DARI DUNIA, bukan diasumsikan")
    assert "yAwal - Math.round(bot.entity.position.y)" in blok, (
        "kedalaman dibaca dari dunia")
    assert "bertahanSampai" in blok, "sesudah aman: diam di dalam, jangan ngeloyor"


def test_lubang_dilindungi_dari_detektor_nyangkut():
    assert "'lubang',\n])" in BOT_JS or "'lubang'," in BOT_JS
    assert "currentTask !== 'lubang'" in BOT_JS


def test_verb_lubang_aman_terdaftar_untuk_llm():
    _, cmds = am.parse_mc_tags("aman dulu deh [MC: lubang_aman]", {})
    assert cmds == [{"cmd": "lubang_aman"}]
    assert am.task_label("lubang").startswith("lagi menggali lubang")


def test_reaksi_lubang_diumumkan_jujur():
    lim = am.ReactionLimiter()
    r1 = am.map_event_to_reaction({"ev": "lubang_start"}, lim, 100.0, {})
    assert r1 and "sembunyi" in r1.lower()
    r2 = am.map_event_to_reaction(
        {"ev": "lubang_done", "dalam": 2, "tertutup": True}, lim, 200.0, {})
    assert r2 and "menutup atasnya" in r2
    r3 = am.map_event_to_reaction(
        {"ev": "lubang_done", "dalam": 2, "tertutup": False}, lim, 300.0, {})
    assert r3 and "masih terbuka" in r3, "jangan mengaku aman kalau bolong"
