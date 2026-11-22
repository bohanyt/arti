"""Tests MESIN TAKDIR — misi kecil terukur, terkunci progresi.

Desain final Bohan 2026-08-09/10 (memori arti-takdir-desain). Tiga hukum:
selesai HANYA dari keadaan terukur; tier <= tier dia + 1; ganjaran narasi
tanpa barang. Diverifikasi hidup: pembukaan 90 dtk -> pancingan "mau
ngapain?" -> kayu8 diumumkan -> dipenuhi -> TUNTAS oleh SISTEM -> jeda ->
takdir berikutnya (tier ikut naik).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402


def st(**k):
    d = {"health": 20, "food": 20, "inv": [], "armor": [], "statistik": {}}
    d.update(k)
    return d


# --- kolam ---------------------------------------------------------------

def test_semua_takdir_valid_dan_terukur():
    """Tiap takdir harus punya kelima fungsi yang bisa dipanggil dengan status
    kosong tanpa melempar — predikat yang meledak = mesin macet diam-diam."""
    kosong = st()
    for t in am.TAKDIR_POOL:
        assert t["id"] and 0 <= t["tier"] <= 6 and t["judul"]
        awal = t["awal"](kosong)
        assert isinstance(awal, dict)
        assert isinstance(t["syarat"](kosong), bool)
        assert isinstance(t["selesai"](kosong, awal), bool)
        n, m = t["kemajuan"](kosong, awal)
        assert 0 <= n <= m and m > 0


def test_id_takdir_unik():
    ids = [t["id"] for t in am.TAKDIR_POOL]
    assert len(ids) == len(set(ids))


# --- tier ----------------------------------------------------------------

def test_tier_naik_mengikuti_gear():
    assert am.takdir_tier(st()) == 0
    assert am.takdir_tier(st(inv=[{"name": "wooden_pickaxe", "count": 1}])) == 1
    assert am.takdir_tier(st(inv=[{"name": "stone_pickaxe", "count": 1}])) == 2
    assert am.takdir_tier(st(inv=[{"name": "stone_pickaxe", "count": 1},
                                  {"name": "shield", "count": 1}])) == 3
    assert am.takdir_tier(st(inv=[{"name": "diamond_pickaxe", "count": 1}])) == 5
    assert am.takdir_tier(st(dim="the_nether")) == 6


def test_terkunci_progresi():
    """Pemula tidak boleh ditawari takdir nether — tier <= tier + 1."""
    layak = am.takdir_layak(st())
    assert layak, "pemula harus selalu punya takdir"
    assert all(t["tier"] <= 1 for t in layak)


def test_takdir_hampa_tidak_ditawarkan():
    """Yang sudah selesai sebelum dipilih itu hampa — bukan pencapaian."""
    layak = am.takdir_layak(st(inv=[{"name": "oak_log", "count": 9}]))
    assert "kayu8" not in [t["id"] for t in layak]


def test_riwayat_mencegah_pengulangan():
    layak = am.takdir_layak(st(), riwayat=["kayu8", "obor4", "pickaxe_pertama"])
    ids = [t["id"] for t in layak]
    assert "kayu8" not in ids and "obor4" not in ids


# --- baseline penghitung kumulatif ---------------------------------------

def test_penghitung_pakai_baseline_aktivasi():
    """bunuh3 harus menghitung SEJAK aktivasi, bukan seumur sesi — tanpa
    baseline, takdir langsung tuntas gratis dari bunuh yang lampau."""
    t = am.takdir_dari_id("bunuh3")
    s7 = st(statistik={"bunuh": 7})
    awal = t["awal"](s7)
    assert not t["selesai"](s7, awal)
    assert t["selesai"](st(statistik={"bunuh": 10}), awal)
    n, m = t["kemajuan"](st(statistik={"bunuh": 9}), awal)
    assert (n, m) == (2, 3)


def test_statistik_dipancarkan_bot():
    js = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
    assert "statistik: { bunuh:" in js
    assert js.count("statistik.bunuh += 1") == 2, "serang & panah dua-duanya"
    assert "statistik.simpan_n += pindah" in js
    assert "biome_n: biomeDikenal.size" in js


# --- baris prompt --------------------------------------------------------

def test_baris_prompt_membawa_kemajuan_dan_larangan_klaim():
    line = am.takdir_line("kayu8", st(inv=[{"name": "oak_log", "count": 5}]), {})
    assert "5/8" in line
    assert "jangan mengaku selesai" in line.lower()
    assert am.takdir_line("tidak_ada", st(), {}) == ""


# --- mesin bridge (siklus penuh, seperti yang diverifikasi live) ---------

def test_siklus_mesin_penuh(monkeypatch):
    import arti_bridge as b
    pesan = []
    monkeypatch.setattr(b, "_queue_game_reaction", lambda t: pesan.append(t))
    monkeypatch.setitem(b.CONFIG, "minecraft_pembukaan_sec", 0.2)
    monkeypatch.setitem(b.CONFIG, "minecraft_takdir_jeda_sec", 0.05)
    b._takdir_reset_sesi()
    s0 = st(pos={"x": 0, "y": 64, "z": 0})
    s0["ev"] = "status"
    b._takdir_on_status(s0)
    assert pesan == [], "pemanasan harus hening"
    time.sleep(0.25)
    b._takdir_on_status(s0)
    assert "TANYA" in pesan[-1], "pancingan 'mau ngapain?' sesudah pemanasan"
    b._takdir_jeda_sampai = 0
    b._takdir_on_status(s0)
    assert b._takdir_aktif is not None and "TAKDIR BARU" in pesan[-1]
    tid = b._takdir_aktif["id"]
    s1 = dict(s0)
    s1["inv"] = [{"name": "oak_log", "count": 8},
                 {"name": "torch", "count": 4},
                 {"name": "wooden_pickaxe", "count": 1}]
    b._takdir_on_status(s1)
    assert "TUNTAS" in pesan[-1] and b._takdir_aktif is None
    assert b._takdir_streak >= 1
    time.sleep(0.1)
    b._takdir_on_status(s1)
    assert b._takdir_aktif is not None and b._takdir_aktif["id"] != tid


def test_takdir_tidak_dibagikan_saat_baku_hantam(monkeypatch):
    import arti_bridge as b
    monkeypatch.setattr(b, "_queue_game_reaction", lambda t: None)
    monkeypatch.setitem(b.CONFIG, "minecraft_pembukaan_sec", 0.0)
    b._takdir_reset_sesi()
    b._takdir_pembukaan_dipancing = True
    b._takdir_jeda_sampai = 0
    s0 = st(nearby_hostiles=[{"kind": "zombie", "distance": 4}])
    s0["ev"] = "status"
    b._takdir_on_status(s0)
    assert b._takdir_aktif is None
    # Musuh JAUH (radius pindai 24) tidak boleh memblokir — live 21.54:
    # di malam hari selalu ada mob terdeteksi, takdir tak pernah turun.
    s1 = st(nearby_hostiles=[{"kind": "zombie", "distance": 20}])
    s1["ev"] = "status"
    b._takdir_on_status(s1)
    assert b._takdir_aktif is not None


def test_hub_terpasang_dan_prompt_terhubung():
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert '"spectator": _mc_event_hub,' in src
    assert "_takdir_reset_sesi()" in src
    assert "takdir_line(" in src
