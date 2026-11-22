"""Gate 5: core world state — skema, isolasi P1/P2, dan staleness.

Yang dijaga di sini bukan "apakah datanya sampai", melainkan **apakah motor
boleh mempercayainya**. Tiga cara state bisa berbohong ke motor:

1. payload rusak diterima seolah sah -> motor memverifikasi gerakan dengan
   angka sampah;
2. state P1 menimpa state P2 -> motor menggerakkan farmer yang salah;
3. state basi dipakai seolah baru -> motor menekan tombol berdasarkan
   posisi masa lalu.

Ketiganya harus gagal TERTUTUP, bukan gagal diam.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_stardew import (  # noqa: E402
    StardewStateStore,
    decode_event,
)

P1 = 1100000000000000002
P2 = -100000000000000001


def _payload(**timpa):
    p = {
        "location": "FarmHouse",
        "tile": {"x": 3, "y": 8},
        "pixel": {"x": 192.0, "y": 512.0},
        "facing": 2,
        "time_of_day": 610,
        "date": {"season": "spring", "day": 1, "year": 1},
        "energy": 270.0,
        "health": 100,
        "is_player_free": True,
        "can_player_move": True,
        "active_menu": None,
    }
    p.update(timpa)
    return p


def _state(player_id=P2, *, seq=2, screen_id=1, **payload_timpa):
    return {
        "protocol": 1, "ev": "state", "seq": seq,
        "player_id": player_id, "screen_id": screen_id,
        "payload": _payload(**payload_timpa),
    }


def _hello(player_id=P2, *, seq=1, screen_id=1, name="artihand", main=False):
    return {
        "protocol": 1, "ev": "hello", "seq": seq,
        "player_id": player_id, "screen_id": screen_id,
        "player_name": name, "is_main_player": main, "is_split_screen": True,
    }


class JamPalsu:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def maju(self, d): self.t += d


# --- SKEMA -----------------------------------------------------------------

def test_state_sah_diterima():
    assert decode_event(json.dumps(_state())) is not None


def test_field_wajib_hilang_ditolak():
    for hilang in ("location", "tile", "pixel", "facing", "time_of_day",
                   "date", "energy", "health", "is_player_free",
                   "can_player_move"):
        ev = _state()
        del ev["payload"][hilang]
        assert decode_event(json.dumps(ev)) is None, f"{hilang} lolos"


def test_tipe_salah_ditolak():
    for kunci, nilai in [("location", 5), ("facing", "dua"), ("health", "100"),
                         ("energy", "banyak"), ("is_player_free", 1),
                         ("can_player_move", "ya"), ("tile", [3, 8]),
                         ("date", "spring"), ("active_menu", 7)]:
        ev = _state(**{kunci: nilai})
        assert decode_event(json.dumps(ev)) is None, f"{kunci}={nilai!r} lolos"


def test_field_masa_depan_ditoleransi():
    """Mod yang lebih baru tidak boleh mematikan receiver lama."""
    ev = _state(fitur_baru_2027={"apa pun": True})
    assert decode_event(json.dumps(ev)) is not None


def test_payload_rusak_TIDAK_mengganti_last_known_good():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(seq=2, tile={"x": 3, "y": 8}))))
    baik = store.latest(P2)
    assert baik is not None and baik.tile == (3, 8)

    rusak = decode_event(json.dumps(_state(seq=3, health="rusak")))
    assert rusak is None, "payload rusak seharusnya ditolak decode"
    if rusak is not None:  # pragma: no cover
        store.catat(rusak)
    assert store.latest(P2).tile == (3, 8), "snapshot baik tertimpa sampah"


# --- ISOLASI P1/P2 ---------------------------------------------------------

def test_P1_dan_P2_tidak_saling_menimpa():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(
        _state(P1, seq=2, screen_id=0, tile={"x": 10, "y": 10}))))
    store.catat(decode_event(json.dumps(
        _state(P2, seq=2, screen_id=1, tile={"x": 99, "y": 99}))))
    assert store.latest(P1).tile == (10, 10)
    assert store.latest(P2).tile == (99, 99)


def test_screen_id_berubah_tidak_menggeser_identitas():
    """Screen ID cuma routing runtime; identitas stabil = player_id."""
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(P2, seq=2, screen_id=1))))
    store.catat(decode_event(json.dumps(
        _state(P2, seq=2, screen_id=2, tile={"x": 7, "y": 7}))))
    assert store.latest(P2).screen_id == 2
    assert store.latest(P2).tile == (7, 7)
    assert store.latest(P1) is None


def test_reconnect_seq_1_sah_di_epoch_baru():
    """Sesudah reconnect mod mulai lagi dari seq rendah. Store TIDAK boleh
    menolaknya — gerbang duplicate/stale ada di receiver, per koneksi."""
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(seq=500, tile={"x": 1, "y": 1}))))
    store.catat(decode_event(json.dumps(_state(seq=1, tile={"x": 2, "y": 2}))))
    snap = store.latest(P2)
    assert snap.seq == 1 and snap.tile == (2, 2)


# --- STALENESS -------------------------------------------------------------

def test_belum_pernah_ada_state_dihitung_stale():
    """Motor tidak boleh bergerak berdasarkan ketiadaan data."""
    assert StardewStateStore().is_stale(P2) is True


def test_timer_stale_bekerja():
    jam = JamPalsu()
    store = StardewStateStore(jam=jam)
    store.catat(decode_event(json.dumps(_state())))
    assert store.is_stale(P2, max_age=0.5) is False
    jam.maju(0.4)
    assert store.is_stale(P2, max_age=0.5) is False
    jam.maju(0.2)
    assert store.is_stale(P2, max_age=0.5) is True


def test_state_baru_menyegarkan_kembali():
    jam = JamPalsu()
    store = StardewStateStore(jam=jam)
    store.catat(decode_event(json.dumps(_state(seq=2))))
    jam.maju(5.0)
    assert store.is_stale(P2) is True
    store.catat(decode_event(json.dumps(_state(seq=3))))
    assert store.is_stale(P2) is False


def test_flag_gerak_dipertahankan_apa_adanya():
    """is_player_free / can_player_move adalah GERBANG motor. Kalau nilainya
    dipelintir jadi True, motor menekan tombol saat menu/cutscene terbuka."""
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(
        is_player_free=False, can_player_move=False, active_menu="ShopMenu"))))
    snap = store.latest(P2)
    assert snap.is_player_free is False
    assert snap.can_player_move is False
    assert snap.active_menu == "ShopMenu"


# --- PEMILIHAN TARGET ------------------------------------------------------

def test_target_memilih_farmhand_bukan_pemain_utama():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_hello(P1, screen_id=0, name="host1", main=True))))
    store.catat(decode_event(json.dumps(_hello(P2, screen_id=1, name="artihand"))))
    assert store.pilih_target() == P2


def test_target_ambigu_FAIL_CLOSED():
    """Dua farmhand tanpa nama penentu: menebak berarti motor bisa
    menggerakkan farmer orang lain, dan itu tidak bisa dibatalkan."""
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_hello(P1, screen_id=0, name="host1", main=True))))
    store.catat(decode_event(json.dumps(_hello(P2, screen_id=1, name="artihand"))))
    store.catat(decode_event(json.dumps(_hello(999, screen_id=2, name="temanlain"))))
    assert store.pilih_target() is None
    assert store.pilih_target("artihand") == P2


def test_target_tanpa_kandidat_balik_None():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_hello(P1, screen_id=0, name="host1", main=True))))
    assert store.pilih_target() is None
    assert store.target is None


def test_target_terkunci_untuk_sesi():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_hello(P2, name="artihand"))))
    assert store.pilih_target() == P2
    store.catat(decode_event(json.dumps(_hello(999, screen_id=2, name="artihand2"))))
    assert store.pilih_target() == P2, "target tidak boleh berpindah diam-diam"
    store.lepas_target()
    assert store.pilih_target() is None, "sesudah dilepas, ambiguitas fail closed"


def test_nama_tidak_peka_huruf_besar():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_hello(P2, name="artihand"))))
    assert store.pilih_target("artihand") == P2


# --- wait_next -------------------------------------------------------------

def test_wait_next_mengembalikan_snapshot_lebih_baru():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(seq=2))))
    awal = store.latest(P2).revisi

    def kirim():
        store.catat(decode_event(json.dumps(_state(seq=3, tile={"x": 4, "y": 4}))))

    threading.Timer(0.05, kirim).start()
    snap = store.wait_next(P2, after_revisi=awal, timeout=2.0)
    assert snap is not None and snap.tile == (4, 4)


def test_wait_next_timeout_balik_None():
    jam = JamPalsu()
    store = StardewStateStore(jam=jam)
    store.catat(decode_event(json.dumps(_state())))
    awal = store.latest(P2).revisi
    assert store.wait_next(P2, after_revisi=awal, timeout=0.0) is None


def test_revisi_selalu_naik_walau_seq_mundur():
    """Dasar wait_next: seq game bisa reset saat reconnect, revisi tidak."""
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(seq=500))))
    r1 = store.latest(P2).revisi
    store.catat(decode_event(json.dumps(_state(seq=1))))
    assert store.latest(P2).revisi > r1


# --- GERBANG MOTOR ---------------------------------------------------------
#
# Ditulis SEBELUM motornya ada. Kalau gerbang baru dibuat bersamaan dengan
# tangan, ada jendela waktu di mana input sudah bisa dikirim tapi
# pemeriksaannya belum lengkap.

def _siap(store, jam=None):
    store.catat(decode_event(json.dumps(_hello(P1, screen_id=0, name="host1", main=True))))
    store.catat(decode_event(json.dumps(_hello(P2, screen_id=1, name="artihand"))))
    store.pilih_target("artihand")
    store.catat(decode_event(json.dumps(_state(P2))))
    return store


def test_gerbang_meloloskan_state_segar_farmhand():
    snap, alasan = _siap(StardewStateStore()).state_layak_motor(P2)
    assert snap is not None and alasan == ""


def test_gerbang_menolak_state_BASI():
    jam = JamPalsu()
    store = _siap(StardewStateStore(jam=jam), jam)
    jam.maju(1.0)
    snap, alasan = store.state_layak_motor(P2, max_age=0.5)
    assert snap is None and "basi" in alasan


def test_gerbang_menolak_saat_menu_terbuka():
    """is_player_free/can_player_move adalah gerbang, bukan hiasan."""
    store = _siap(StardewStateStore())
    store.catat(decode_event(json.dumps(
        _state(P2, seq=9, is_player_free=False, active_menu="ShopMenu"))))
    snap, alasan = store.state_layak_motor(P2)
    assert snap is None and "is_player_free" in alasan

    store.catat(decode_event(json.dumps(
        _state(P2, seq=10, can_player_move=False))))
    snap, alasan = store.state_layak_motor(P2)
    assert snap is None and "can_player_move" in alasan


def test_gerbang_menolak_pemain_utama():
    store = _siap(StardewStateStore())
    store.catat(decode_event(json.dumps(_state(P1, screen_id=0))))
    snap, alasan = store.state_layak_motor(P1)
    assert snap is None and "bukan farmer target" in alasan


def test_gerbang_menolak_farmer_yang_bukan_target():
    store = _siap(StardewStateStore())
    store.catat(decode_event(json.dumps(_hello(999, screen_id=2, name="orang"))))
    store.catat(decode_event(json.dumps(_state(999, screen_id=2))))
    snap, alasan = store.state_layak_motor(999)
    assert snap is None and "bukan farmer target" in alasan


def test_gerbang_menolak_tanpa_hello():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(P2))))
    snap, alasan = store.state_layak_motor(P2)
    assert snap is None and "hello" in alasan


def test_gerbang_menolak_tanpa_state():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_hello(P2, name="artihand"))))
    snap, alasan = store.state_layak_motor(P2)
    assert snap is None and "belum pernah ada state" in alasan


def test_gerbang_selalu_memberi_ALASAN():
    """Motor harus bisa mencatat kenapa dia menolak — gagal diam itu bug."""
    store = StardewStateStore()
    snap, alasan = store.state_layak_motor(P2)
    assert snap is None and alasan.strip()


# ===========================================================================
# Inventaris (ditambahkan 29 Agu 2026) — OPSIONAL, tapi kalau ada wajib benar
# ===========================================================================

def _inv(**timpa):
    inv = {
        "active_slot": 0,
        "hotbar": [
            {"slot": 0, "name": "Hoe", "stack": 1, "is_tool": True},
            {"slot": 3, "name": "Parsnip Seeds", "stack": 15, "is_tool": False},
        ],
        "slots_used": 7,
        "items_total": 143,
    }
    inv.update(timpa)
    return inv


def _menu(**timpa):
    menu = {
        "name": "GameMenu",
        "snap_id": 103,
        "snap_label": "Right Ring",
        "snap_bounds": {"x": 700, "y": 420, "w": 64, "h": 64},
        "close_id": 9175502,
    }
    menu.update(timpa)
    return menu


def test_menu_boleh_hilang_untuk_kompatibilitas_dll_lama():
    assert decode_event(json.dumps(_state(active_menu="GameMenu"))) is not None


def test_menu_non_null_terbaca_utuh():
    ev = decode_event(json.dumps(_state(
        active_menu="GameMenu", menu=_menu(),
    )))
    assert ev is not None
    store = StardewStateStore()
    store.catat(ev)
    snap = store.latest(P2)
    assert snap is not None
    assert snap.menu_snap_id == 103
    assert snap.menu_snap_label == "Right Ring"
    assert snap.menu_snap_bounds == (700, 420, 64, 64)
    assert snap.menu_close_id == 9175502


def test_structural_ui_snapshot_terbaca_dengan_semantic_key_dan_neighbor_hint():
    menu = _menu(
        menu_type="GameMenu",
        page_type="InventoryPage",
        focus_key="inventory.slot.15",
        components=[
            {
                "key": "game.tab.inventory",
                "native_id": 12340,
                "source": "frame",
                "role": "tab",
                "name": "Inventory",
                "label": "Inventory",
                "bounds": {"x": 64, "y": 64, "w": 64, "h": 64},
                "neighbors": {"up": -99998, "right": 12341,
                              "down": 0, "left": -99999},
            },
            {
                "key": "inventory.slot.15",
                "native_id": 15,
                "source": "page",
                "role": "inventory_slot",
                "name": "15",
                "label": "Right Ring",
                "bounds": {"x": 700, "y": 420, "w": 64, "h": 64},
                "neighbors": {"up": 3, "right": 16, "down": 27, "left": 14},
            },
        ],
    )
    ev = decode_event(json.dumps(_state(active_menu="GameMenu", menu=menu)))
    assert ev is not None
    store = StardewStateStore()
    store.catat(ev)
    snap = store.latest(P2)
    assert snap is not None
    assert snap.menu_type == "GameMenu"
    assert snap.menu_page_type == "InventoryPage"
    assert snap.menu_focus_key == "inventory.slot.15"
    assert [c.key for c in snap.menu_components] == [
        "game.tab.inventory", "inventory.slot.15",
    ]
    slot = snap.menu_components[1]
    assert slot.native_id == 15
    assert slot.role == "inventory_slot"
    assert slot.neighbors["right"] == 16
    assert snap.menu_components[0].neighbors["up"] == -99998, (
        "sentinel sah sebagai hint"
    )


def test_structural_ui_malformed_fail_closed():
    dasar = {
        "key": "inventory.slot.0",
        "native_id": 0,
        "source": "page",
        "role": "inventory_slot",
        "name": "0",
        "label": "Hoe",
        "bounds": {"x": 64, "y": 128, "w": 64, "h": 64},
        "neighbors": {"up": None, "right": 1, "down": 12, "left": None},
    }
    rusak = [
        _menu(menu_type=7),
        _menu(menu_type="ShopMenu"),
        _menu(page_type=4),
        _menu(focus_key=True),
        _menu(components="bukan list"),
        _menu(components=[{**dasar, "key": ""}]),
        _menu(components=[{**dasar, "native_id": True}]),
        _menu(components=[{**dasar, "neighbors": {"right": False}}]),
        _menu(components=[{**dasar, "bounds": {"x": 0, "y": 0, "w": -1, "h": 64}}]),
    ]
    for menu in rusak:
        assert decode_event(json.dumps(_state(
            active_menu="GameMenu", menu=menu,
        ))) is None, f"lolos: {menu!r}"


def test_ui_revision_opsional_tetapi_bila_ada_wajib_int_biasa_nonnegatif():
    assert decode_event(json.dumps(_state(ui_revision=42))) is not None
    for nilai in (True, -1, 1.5, "42"):
        assert decode_event(json.dumps(_state(ui_revision=nilai))) is None

    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(ui_revision=42))))
    assert store.latest(P2).ui_revision == 42


def test_menu_rusak_menolak_seluruh_state():
    rusak = [
        "bukan dict",
        _menu(name=7),
        _menu(snap_id=True),
        _menu(close_id=False),
        _menu(snap_label=9),
        _menu(snap_bounds=[700, 420, 64, 64]),
        _menu(snap_bounds={"x": 700, "y": 420, "w": True, "h": 64}),
        _menu(snap_bounds={"x": 700, "y": 420, "w": 0, "h": 64}),
    ]
    for menu in rusak:
        ev = _state(active_menu="GameMenu", menu=menu)
        assert decode_event(json.dumps(ev)) is None, f"lolos: {menu!r}"


def test_menu_dan_active_menu_harus_konsisten_bila_blok_tersedia():
    assert decode_event(json.dumps(_state(
        active_menu=None, menu=_menu(),
    ))) is None
    assert decode_event(json.dumps(_state(
        active_menu="ShopMenu", menu=_menu(name="GameMenu"),
    ))) is None


def test_field_asing_menu_tetap_ditoleransi():
    assert decode_event(json.dumps(_state(
        active_menu="GameMenu", menu=_menu(fitur_masa_depan={"ok": True}),
    ))) is not None


def test_inventaris_boleh_tidak_ada_sama_sekali():
    """DLL sebelum 29 Agu belum mengirim inventaris. Receiver baru HARUS
    tetap jalan dengannya — kalau tidak, memperbarui satu sisi saja
    menghapus seluruh state."""
    ev = decode_event(json.dumps(_state()))
    assert ev is not None
    store = StardewStateStore()
    store.catat(ev)
    snap = store.latest(P2)
    assert snap is not None
    assert not snap.punya_inventaris
    assert snap.dipegang is None
    assert snap.hotbar == ()


def test_inventaris_null_diperlakukan_sama_dengan_tidak_ada():
    """Serializer C# mengirim `"inventory": null` saat kosong, bukan
    menghilangkan kuncinya. Keduanya harus sama artinya."""
    ev = decode_event(json.dumps(_state(inventory=None)))
    assert ev is not None
    store = StardewStateStore()
    store.catat(ev)
    snap = store.latest(P2)
    assert snap is not None and not snap.punya_inventaris


def test_inventaris_terisi_terbaca_utuh():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(inventory=_inv()))))
    snap = store.latest(P2)
    assert snap is not None
    assert snap.punya_inventaris
    assert snap.items_total == 143
    assert snap.slots_used == 7
    assert len(snap.hotbar) == 2
    assert snap.hotbar[0].nama == "Hoe" and snap.hotbar[0].alat
    assert snap.hotbar[1].stack == 15 and not snap.hotbar[1].alat


def test_dipegang_mengikuti_slot_aktif():
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(inventory=_inv(active_slot=3)))))
    snap = store.latest(P2)
    assert snap is not None
    dipegang = snap.dipegang
    assert dipegang is not None
    assert dipegang.nama == "Parsnip Seeds"
    assert not dipegang.alat


def test_slot_aktif_kosong_berarti_tangan_kosong():
    """Slot aktif yang tidak ada di hotbar = tidak memegang apa-apa. Bukan
    error, dan bukan alasan menolak state."""
    store = StardewStateStore()
    store.catat(decode_event(json.dumps(_state(inventory=_inv(active_slot=9)))))
    snap = store.latest(P2)
    assert snap is not None
    assert snap.punya_inventaris
    assert snap.dipegang is None


def test_inventaris_rusak_menolak_SELURUH_state():
    """Fail closed. Motor memverifikasi pengambilan barang dari `items_total`;
    angka sampah di situ membuat Arti mengumumkan 'udah aku ambil' untuk
    sesuatu yang tidak pernah terjadi."""
    rusak = [
        _inv(items_total="banyak"),
        _inv(active_slot="dua"),
        _inv(slots_used=None),
        _inv(hotbar="Hoe"),
        _inv(hotbar=[{"slot": 0, "name": "Hoe", "stack": 1}]),          # is_tool hilang
        _inv(hotbar=[{"slot": 0, "name": 5, "stack": 1, "is_tool": True}]),
        _inv(hotbar=[{"slot": "0", "name": "Hoe", "stack": 1, "is_tool": True}]),
        _inv(hotbar=[{"slot": 0, "name": "Hoe", "stack": 1, "is_tool": "ya"}]),
        "bukan dict",
    ]
    for inv in rusak:
        ev = _state(inventory=inv)
        assert decode_event(json.dumps(ev)) is None, f"lolos: {inv!r}"


def test_bool_bukan_int_yang_sah_untuk_angka_inventaris():
    """bool subclass int di Python. `items_total=True` bukan hitungan."""
    ev = _state(inventory=_inv(items_total=True))
    assert decode_event(json.dumps(ev)) is None


def test_field_asing_di_dalam_slot_tetap_ditoleransi():
    """Aturan yang sama dengan payload luar: mod yang lebih baru tidak boleh
    mematikan receiver lama."""
    inv = _inv(hotbar=[
        {"slot": 0, "name": "Hoe", "stack": 1, "is_tool": True,
         "quality": 2, "category": -99},
    ])
    ev = decode_event(json.dumps(_state(inventory=inv)))
    assert ev is not None


def test_kontrak_skema_dari_csharp():
    """Kontrak DUA BAHASA: skema state hidup di C#, divalidasi di Python, dan
    tidak ada compiler yang menjaga sambungannya.

    `tests/data/stardew_state_contract.json` dihasilkan oleh serializer C#
    yang SUNGGUHAN (lewat proyek tes C# yang tidak ada di repositori ini). Tanpa berkas bersama ini, mengganti nama
    satu field di C# lolos semua tes C#, lolos semua tes Python, lalu gagal DI
    GAME HIDUP — tempat paling mahal untuk menemukannya.

    Sudah membayar dirinya sendiri saat dibuat: C# menyerialisasi
    `energy: 264` dan `pixel: 4096` sebagai INTEGER, bukan float. Validator
    yang menuntut float akan menolak state asli.
    """
    berkas = Path(__file__).parent / "data" / "stardew_state_contract.json"
    payload = json.loads(berkas.read_text(encoding="utf-8"))

    ev = decode_event(json.dumps({
        "protocol": 1, "ev": "state", "seq": 2,
        "player_id": P2, "screen_id": 1, "payload": payload,
    }))
    assert ev is not None, "payload asli dari C# DITOLAK validator Python"

    store = StardewStateStore()
    store.catat(ev)
    snap = store.latest(P2)
    assert snap is not None
    assert snap.tile == (64, 17)
    assert snap.pixel == (4096.0, 1088.0)
    assert snap.energy == 264.0
    assert snap.ui_revision == 42
    assert snap.active_menu == "GameMenu"
    assert snap.menu_snap_id == 103
    assert snap.menu_snap_label == "Right Ring"
    assert snap.menu_snap_bounds == (700, 420, 64, 64)
    assert snap.menu_close_id == 9175502
    assert snap.menu_page_type == "InventoryPage"
    assert snap.menu_focus_key == "inventory.slot.15"
    assert [c.key for c in snap.menu_components] == [
        "game.tab.inventory", "inventory.slot.15",
    ]
    assert snap.menu_components[0].text == "Inventory"
    assert snap.menu_components[0].selected
    assert snap.menu_components[1].text == "Parsnip Seeds"
    assert snap.menu_components[1].count == 15
    assert snap.punya_inventaris
    assert snap.items_total == 143
    assert snap.slots_used == 7
    assert snap.active_slot == 0
    dipegang = snap.dipegang
    assert dipegang is not None and dipegang.nama == "Hoe" and dipegang.alat
    assert [s.slot for s in snap.hotbar] == [0, 3]


def test_nan_dan_inf_ditolak():
    """Ditemukan lewat fuzz 29 Agu 2026 (19.048 baris sampah, nol crash, tapi
    NaN/inf LOLOS validator).

    Motor tidak menggerbangi apa pun dengan energy/pixel, jadi tidak ada
    salah-langkah hari ini — tapi `energy` masuk ke `HasilAksi.delta_energi`,
    dan angka itulah yang nanti dibaca lapis otak untuk diceritakan ke
    penonton. NaN yang lolos = Arti melaporkan sesuatu yang bukan angka, dan
    setiap perbandingan dengannya diam-diam False.
    """
    for nilai in (float("nan"), float("inf"), float("-inf")):
        assert decode_event(json.dumps(_state(energy=nilai))) is None, nilai
        assert decode_event(json.dumps(
            _state(pixel={"x": nilai, "y": 0.0})
        )) is None, nilai

    # Angka berhingga tetap lolos, termasuk int (C# mengirim energy sebagai
    # int kalau nilainya bulat).
    assert decode_event(json.dumps(_state(energy=264))) is not None
    assert decode_event(json.dumps(_state(energy=0.0))) is not None
    assert decode_event(json.dumps(_state(energy=-5.5))) is not None
