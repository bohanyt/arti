"""Fase BERTAHAN di balik tembok (2026-08-10).

Konflik yang terlihat di harness & live 13.48 (keluhan "[recorded remark omitted]"): refleks menembok diri -> `bangun` selesai -> setFollow -> roam
LANGSUNG jalan -> semua kandidat tertolak (dia DI DALAM tembok sendiri) ->
dicap terkurung -> gali keluar dari perlindungannya sendiri -> dipukuli ->
menembok lagi... muter tanpa akhir.

Aturan baru: tembok berdiri UTUH + musuh masih dekat = fase `bertahan` —
diam di dalam, perpanjang selama musuh <=12 blok, keluar begitu aman
(diumumkan sistem, jujur dari keadaan dunia).
"""
from pathlib import Path

import arti_minecraft as am

ROOT = Path(__file__).resolve().parent.parent
BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")


def test_setfollow_menghormati_fase_bertahan():
    i = BOT_JS.index("function setFollow()")
    badan = BOT_JS[i:BOT_JS.index("\n}", i)]
    assert "bertahanSampai" in badan
    j = badan.index("bertahanSampai")
    assert "player(STREAMER)" not in badan[:j], (
        "cek bertahan harus SEBELUM logika follow/roam — kalau tidak, roam "
        "tetap menang dan dia menggali keluar dari temboknya sendiri")
    assert "'bertahan'" in badan


def test_bangun_memulai_bertahan_hanya_kalau_utuh_dan_terancam():
    i = BOT_JS.index("ev: 'build_done'")
    blok = BOT_JS[i:i + 700]
    assert "bolong === 0 && hostileTerdekat()" in blok, (
        "tembok bolong = belum melindungi; tanpa musuh = tidak perlu meringkuk")
    assert "bertahan_start" in blok


def test_bertahan_diperpanjang_dan_dilepas_dengan_jujur():
    i = BOT_JS.index("currentTask === 'bertahan'")
    blok = BOT_JS[i:i + 900]
    assert "BERTAHAN_MUSUH_DEKAT" in blok
    assert "bertahan_selesai" in blok, "keluarnya DIUMUMKAN, bukan diam-diam"
    assert "setFollow()" in blok
    assert "lastMoveAt = Date.now()" in blok, (
        "diam di balik tembok itu SEHAT — jangan dicap nyangkut")


def test_bertahan_dikecualikan_detektor_nyangkut():
    assert "currentTask !== 'bertahan'" in BOT_JS


def test_reaksi_bertahan():
    lim = am.ReactionLimiter()
    assert am.map_event_to_reaction(
        {"ev": "bertahan_start"}, lim, 100.0, {}) is None, (
        "reaksi build_done barusan sudah bicara — jangan dobel")
    r = am.map_event_to_reaction({"ev": "bertahan_selesai"}, lim, 100.0, {})
    assert r and "AMAN" in r


def test_label_tugas_bertahan():
    src = (ROOT / "arti_minecraft.py").read_text(encoding="utf-8")
    assert '"bertahan": "lagi bertahan di balik tembok' in src
