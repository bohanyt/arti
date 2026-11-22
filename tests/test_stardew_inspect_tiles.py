"""Cloud 51: satu pemanggilan Python -> satu inspect_tiles read-only.

Memakai kotak masuk berkas yang sama. Yang dijaga:

1. permintaan sah membawa jendela + identitas sesi/pemain/lokasi;
2. argumen di luar batas tidak pernah menulis berkas;
3. gerbang inbox/sesi menolak sebelum tulis; gerbang motor dan input_busy
   tidak mewarisi ke query read-only;
4. sukses hanya Inspected; facts menyalin DTO kanonik termasuk crop.watered;
5. tidak ada field walkable agregat;
6. menu_step/water_once/world_press tidak bergeser.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import arti_stardew_command as C  # noqa: E402
from test_stardew_command_inbox import (  # noqa: E402
    TUNGGU_MOD,
    ModPalsu,
    baca_permintaan,
    tulis_sesi,
)

SESI_INSPECT = {
    "protocol": 1,
    "ev": "command_session",
    "session_id": "sesi-abc",
    "player_id": 100000000012,
    "player_name": "artihand",
    "location_id": "Farm",
    "screen_id": 0,
    "is_main_player": False,
    "is_split_screen": False,
    "inbox_enabled": True,
    "menu_step_enabled": False,
    "water_once_enabled": False,
    "world_press_enabled": False,
    "world_ready": True,
    "inflight_request_id": None,
    "input_busy": True,
}


@pytest.fixture
def kotak_inspect(tmp_path: Path) -> Path:
    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI_INSPECT), encoding="utf-8")
    return tmp_path


def envelope_inspect(**ubah) -> dict:
    isi = {
        "protocol": 1,
        "ev": "command_result",
        "op": "inspect_tiles",
        "session_id": "sesi-abc",
        "player_id": 100000000012,
        "location_id": "Farm",
        "accepted": True,
        "status": "completed",
        "reject_reason": None,
        "outcome": "Inspected",
        "success": True,
        "completed_unix_ms": 1,
        "captured_ticks": 40,
        "center_x": 57,
        "center_y": 18,
        "radius": 2,
        "facts": {
            "tf:Farm:57,18:HoeDirt": {
                "kind": "terrain",
                "terrain_type": "HoeDirt",
                "tile": {"x": 57, "y": 18},
                "passable": True,
                "crop": {
                    "qualified_item_id": None,
                    "phase": 1,
                    "dead": False,
                    "watered": False,
                    "ready_for_harvest": False,
                },
            }
        },
    }
    isi.update(ubah)
    return isi


def test_permintaan_inspect_membawa_jendela_dan_identitas(kotak_inspect: Path):
    with ModPalsu(kotak_inspect, envelope_inspect()) as mod:
        hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect)

    assert hasil.sukses and hasil.outcome == "Inspected"
    req = mod.permintaan
    assert req is not None
    assert set(req) == {
        "protocol",
        "op",
        "request_id",
        "session_id",
        "expected_player_id",
        "expected_player_name",
        "expected_location_id",
        "center_x",
        "center_y",
        "radius",
        "deadline_unix_ms",
    }
    assert req["op"] == "inspect_tiles"
    assert req["center_x"] == 57 and req["center_y"] == 18 and req["radius"] == 2
    assert req["session_id"] == SESI_INSPECT["session_id"]
    assert req["expected_player_id"] == SESI_INSPECT["player_id"]
    assert req["expected_location_id"] == "Farm"
    assert "direction" not in req
    assert "tile_x" not in req
    assert "walkable" not in req


def test_inspect_baca_watered_dari_fact_hasil(kotak_inspect: Path):
    with ModPalsu(kotak_inspect, envelope_inspect()):
        kering = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect)
    assert kering.sukses
    crop = kering.mentah["facts"]["tf:Farm:57,18:HoeDirt"]["crop"]
    assert crop["watered"] is False

    basah = envelope_inspect()
    basah["facts"]["tf:Farm:57,18:HoeDirt"]["crop"]["watered"] = True
    basah["captured_ticks"] = 41
    with ModPalsu(kotak_inspect, basah):
        ulang = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect)
    assert ulang.sukses
    assert ulang.mentah["facts"]["tf:Farm:57,18:HoeDirt"]["crop"]["watered"] is True
    assert ulang.mentah["captured_ticks"] == 41


def test_inspect_tidak_butuh_gerbang_motor_atau_input_busy(kotak_inspect: Path):
    sesi = C.baca_sesi(kotak_inspect)
    assert sesi.menu_step_enabled is False
    assert sesi.water_once_enabled is False
    assert sesi.world_press_enabled is False
    assert sesi.input_busy is True
    with ModPalsu(kotak_inspect, envelope_inspect()):
        hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect)
    assert hasil.sukses


def test_menu_step_tetap_menolak_input_busy(kotak_inspect: Path):
    isi = dict(SESI_INSPECT)
    isi["menu_step_enabled"] = True
    (kotak_inspect / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    hasil = C.minta_menu_step("down", folder=kotak_inspect, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert "input menu/dunia" in (hasil.alasan or "")
    assert not (kotak_inspect / C.NAMA_REQUEST).exists()


@pytest.mark.parametrize(
    "x,y,r",
    [
        (-1, 18, 2),
        (57, -1, 2),
        (C.TILE_MAX + 1, 18, 2),
        (57, 18, 5),
        (57, 18, -1),
        (57, 18, True),
        (10.5, 18, 2),
        ("57", 18, 2),
    ],
)
def test_jendela_tidak_sah_tidak_pernah_menulis_berkas(kotak_inspect: Path, x, y, r):
    with pytest.raises(C.KesalahanPerintah):
        C.minta_inspect_tiles(x, y, r, folder=kotak_inspect)
    assert not (kotak_inspect / C.NAMA_REQUEST).exists()


def test_location_kosong_tidak_pernah_menulis_berkas(kotak_inspect: Path):
    hasil = C.minta_inspect_tiles(
        57, 18, 2, expected_location_id="", folder=kotak_inspect, batas_detik=0.2
    )
    assert hasil.status == C.GAGAL_LOKAL
    assert not hasil.sukses
    assert not (kotak_inspect / C.NAMA_REQUEST).exists()


def test_dunia_belum_siap_sebelum_validasi_lokasi(kotak_inspect: Path):
    isi = dict(SESI_INSPECT)
    isi["world_ready"] = False
    isi["location_id"] = ""
    (kotak_inspect / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert "dunia belum siap" in (hasil.alasan or "")
    assert not (kotak_inspect / C.NAMA_REQUEST).exists()


@pytest.mark.parametrize(
    "ubah,potongan",
    [
        ({"inbox_enabled": False}, "MenuStepCommandInboxEnabled"),
        ({"world_ready": False}, "dunia belum siap"),
        ({"is_main_player": True}, "HOST"),
        ({"is_split_screen": True}, "split-screen"),
        ({"inflight_request_id": "lain"}, "masih ada perintah berjalan"),
        ({"session_id": ""}, "session_id"),
    ],
)
def test_sesi_inspect_belum_siap_menolak_tanpa_menulis(kotak_inspect: Path, ubah, potongan):
    isi = dict(SESI_INSPECT)
    isi.update(ubah)
    (kotak_inspect / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert not hasil.sukses
    assert potongan in (hasil.alasan or "")
    assert not (kotak_inspect / C.NAMA_REQUEST).exists()


def test_slot_terpakai_tidak_ditimpa_inspect(kotak_inspect: Path):
    (kotak_inspect / C.NAMA_REQUEST).write_text(
        '{"request_id":"punya-orang-lain"}', encoding="utf-8"
    )
    hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert baca_permintaan(kotak_inspect)["request_id"] == "punya-orang-lain"


@pytest.mark.parametrize(
    "ubah",
    [
        {"outcome": "Inspected", "success": False},
        {"outcome": "Stepped", "success": True},
        {"outcome": "ObservedWatered", "success": True},
        {"success": True, "status": "rejected", "reject_reason": "LocationMismatch"},
    ],
)
def test_hanya_inspected_yang_boleh_sukses(kotak_inspect: Path, ubah):
    with ModPalsu(kotak_inspect, envelope_inspect(**ubah)):
        hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect)
    assert not hasil.sukses
    assert hasil.pasti


def test_hasil_tanpa_walkable(kotak_inspect: Path):
    with ModPalsu(kotak_inspect, envelope_inspect()) as mod:
        hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect)
    assert hasil.sukses
    assert "walkable" not in json.dumps(hasil.mentah).lower()
    assert mod.permintaan is not None
    assert "walkable" not in hasil.mentah
    assert "route_safe" not in hasil.mentah
    assert "can_path" not in hasil.mentah


def test_identitas_hasil_tidak_cocok_bukan_sukses(kotak_inspect: Path):
    palsu = envelope_inspect(location_id="BusStop", center_x=1, radius=0)
    with ModPalsu(kotak_inspect, palsu):
        hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert "identitas hasil" in (hasil.alasan or "")


def test_tidak_diambil_vs_tidak_pasti_inspect(kotak_inspect: Path):
    hasil = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect, batas_detik=0.15)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.sukses and not hasil.pasti
    assert (kotak_inspect / C.NAMA_REQUEST).exists()
    blocked = C.minta_inspect_tiles(57,18,2,folder=kotak_inspect,batas_detik=0.01)
    assert blocked.status == C.GAGAL_LOKAL  # no automatic replay/slot deletion
    C.bersihkan_slot(kotak_inspect)  # explicit cleanup outside the pending operation

    with ModPalsu(kotak_inspect, envelope=None, jeda=TUNGGU_MOD):
        hilang = C.minta_inspect_tiles(57, 18, 2, folder=kotak_inspect, batas_detik=0.2)
    assert hilang.status == C.TIDAK_PASTI
    assert not hilang.sukses


def test_menu_step_field_set_tidak_berganti(kotak_inspect: Path):
    isi = dict(SESI_INSPECT)
    isi["menu_step_enabled"] = True
    isi["input_busy"] = False
    (kotak_inspect / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    from test_stardew_command_inbox import envelope as envelope_menu

    with ModPalsu(kotak_inspect, envelope_menu()) as mod:
        C.minta_menu_step("down", folder=kotak_inspect)
    assert set(mod.permintaan) == {
        "protocol",
        "op",
        "request_id",
        "session_id",
        "expected_player_id",
        "expected_player_name",
        "direction",
        "timeout_ticks",
        "delay_ticks",
        "deadline_unix_ms",
    }
