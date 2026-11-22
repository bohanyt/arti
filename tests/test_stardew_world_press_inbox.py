"""P5-R3D: satu pemanggilan Python -> satu world_press -> satu putusan.

Memakai kotak masuk berkas yang sama dengan menu_step / water_once. Yang
dijaga:

1. permintaan sah membawa direction/ticks dan identitas sesi;
2. argumen di luar batas tidak pernah menulis berkas;
3. gerbang sesi/deadline/satu-slot/busy menolak sebelum tulis;
4. sukses hanya PressCompleted; itu bukan klaim farmer pindah ubin;
5. tidak_diambil vs tidak_pasti, tanpa retry otomatis;
6. menu_step dan water_once tidak bergeser.
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
from test_stardew_water_once_inbox import envelope_water  # noqa: E402

SESI_PRESS = {
    "protocol": 1,
    "ev": "command_session",
    "session_id": "sesi-abc",
    "player_id": 100000000012,
    "player_name": "artihand",
    "screen_id": 0,
    "is_main_player": False,
    "is_split_screen": False,
    "inbox_enabled": True,
    "menu_step_enabled": False,
    "water_once_enabled": False,
    "world_press_enabled": True,
    "world_ready": True,
    "inflight_request_id": None,
    "input_busy": False,
}


@pytest.fixture
def kotak_press(tmp_path: Path) -> Path:
    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI_PRESS), encoding="utf-8")
    return tmp_path


def envelope_press(**ubah) -> dict:
    isi = {
        "protocol": 1,
        "ev": "command_result",
        "op": "world_press",
        "session_id": "sesi-abc",
        "accepted": True,
        "status": "completed",
        "reject_reason": None,
        "arm_status": "Started",
        "run_id": 4,
        "outcome": "PressCompleted",
        "success": True,
        "direction": "left",
        "press_count": 12,
        "moved": None,
        "completed_unix_ms": 1,
    }
    isi.update(ubah)
    return isi


def test_permintaan_press_membawa_identitas_arah_ticks_deadline(kotak_press: Path):
    with ModPalsu(kotak_press, envelope_press()) as mod:
        hasil = C.minta_world_press("LEFT", 12, folder=kotak_press)

    assert hasil.sukses and hasil.outcome == "PressCompleted"
    req = mod.permintaan
    assert req is not None
    assert set(req) == {
        "protocol",
        "op",
        "request_id",
        "session_id",
        "expected_player_id",
        "expected_player_name",
        "direction",
        "ticks",
        "deadline_unix_ms",
    }
    assert req["op"] == "world_press"
    assert req["direction"] == "left"
    assert req["ticks"] == 12
    assert req["session_id"] == SESI_PRESS["session_id"]
    assert req["expected_player_id"] == SESI_PRESS["player_id"]
    assert req["deadline_unix_ms"] > C._sekarang_ms() - 1000
    assert "tile_x" not in req


@pytest.mark.parametrize("arah", ["left", "right", "up", "down"])
def test_empat_arah_sah(kotak_press: Path, arah: str):
    with ModPalsu(kotak_press, envelope_press(direction=arah)):
        hasil = C.minta_world_press(arah, 1, folder=kotak_press)
    assert hasil.sukses


def test_setiap_panggilan_press_memakai_request_id_baru(kotak_press: Path):
    ids = []
    for _ in range(2):
        with ModPalsu(kotak_press, envelope_press()) as mod:
            C.minta_world_press("left", 12, folder=kotak_press)
        ids.append(mod.permintaan["request_id"])
    assert ids[0] != ids[1]


@pytest.mark.parametrize(
    "arah,ticks",
    [
        ("forward", 12),
        ("a", 12),
        ("left", 0),
        ("left", 61),
        ("left", -1),
        ("left", True),
        ("left", 12.5),
        ("left", "12"),
    ],
)
def test_arah_atau_ticks_tidak_sah_tidak_pernah_menulis_berkas(kotak_press: Path, arah, ticks):
    with pytest.raises(C.KesalahanPerintah):
        C.minta_world_press(arah, ticks, folder=kotak_press)
    assert not (kotak_press / C.NAMA_REQUEST).exists()


@pytest.mark.parametrize(
    "ubah,potongan",
    [
        ({"inbox_enabled": False}, "MenuStepCommandInboxEnabled"),
        ({"world_press_enabled": False}, "WorldPressEnabled"),
        ({"world_ready": False}, "dunia belum siap"),
        ({"is_main_player": True}, "HOST"),
        ({"is_split_screen": True}, "split-screen"),
        ({"inflight_request_id": "lain"}, "masih ada perintah berjalan"),
        ({"input_busy": True}, "input menu/dunia"),
        ({"session_id": ""}, "session_id"),
    ],
)
def test_sesi_press_belum_siap_menolak_tanpa_menulis(kotak_press: Path, ubah, potongan):
    isi = dict(SESI_PRESS)
    isi.update(ubah)
    (kotak_press / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    hasil = C.minta_world_press("left", 12, folder=kotak_press, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert not hasil.sukses
    assert potongan in (hasil.alasan or "")
    assert not (kotak_press / C.NAMA_REQUEST).exists()


def test_press_tidak_butuh_menu_atau_water_enabled(kotak_press: Path):
    sesi = C.baca_sesi(kotak_press)
    assert sesi.menu_step_enabled is False
    assert sesi.water_once_enabled is False
    assert sesi.world_press_enabled is True
    with ModPalsu(kotak_press, envelope_press()):
        hasil = C.minta_world_press("left", 12, folder=kotak_press)
    assert hasil.sukses


def test_slot_terpakai_tidak_ditimpa_press(kotak_press: Path):
    (kotak_press / C.NAMA_REQUEST).write_text(
        '{"request_id":"punya-orang-lain"}', encoding="utf-8"
    )
    hasil = C.minta_world_press("left", 12, folder=kotak_press, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert baca_permintaan(kotak_press)["request_id"] == "punya-orang-lain"


@pytest.mark.parametrize(
    "ubah",
    [
        {"outcome": "Started", "success": False},
        {"outcome": "RejectedAlreadyActive", "success": False},
        {"outcome": "Stepped", "success": True},
        {"outcome": "ObservedWatered", "success": True},
        {"outcome": "PressCompleted", "success": False},
        {"outcome": "Moved", "success": True},
    ],
)
def test_hanya_press_completed_yang_boleh_sukses(kotak_press: Path, ubah):
    with ModPalsu(kotak_press, envelope_press(**ubah)):
        hasil = C.minta_world_press("left", 12, folder=kotak_press)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert hasil.pasti
    if "outcome" in ubah:
        assert hasil.outcome == ubah["outcome"]


def test_press_completed_penuh_adalah_sukses_tanpa_klaim_pindah(kotak_press: Path):
    with ModPalsu(kotak_press, envelope_press()) as mod:
        hasil = C.minta_world_press("left", 12, folder=kotak_press)
    assert hasil.status == C.SELESAI and hasil.sukses
    assert hasil.outcome == "PressCompleted"
    assert (hasil.mentah or {}).get("moved") is None
    assert (hasil.mentah or {}).get("press_count") == 12
    assert mod.permintaan["ticks"] == 12


def test_press_ditolak_membawa_status_native(kotak_press: Path):
    with ModPalsu(
        kotak_press,
        envelope_press(
            accepted=False,
            status="rejected",
            reject_reason="ArmRejected",
            arm_status="RejectedHost",
            outcome=None,
            success=False,
        ),
    ):
        hasil = C.minta_world_press("left", 12, folder=kotak_press)
    assert hasil.status == C.DITOLAK and not hasil.sukses
    assert "RejectedHost" in (hasil.alasan or "")


def test_press_timeout_mempertahankan_permintaan_dan_tidak_pasti(kotak_press: Path):
    hasil = C.minta_world_press("left", 12, folder=kotak_press, batas_detik=0.3)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.pasti and not hasil.sukses
    assert json.loads((kotak_press / C.NAMA_REQUEST).read_text())["request_id"] == hasil.request_id
    assert "JANGAN ulangi otomatis" in hasil.alasan


def test_press_sudah_diambil_tanpa_hasil_adalah_tidak_pasti(kotak_press: Path):
    with ModPalsu(kotak_press, envelope=None):
        hasil = C.minta_world_press("left", 12, folder=kotak_press, batas_detik=TUNGGU_MOD)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.pasti and not hasil.sukses
    assert "JANGAN ulangi otomatis" in (hasil.alasan or "")


def test_press_tidak_pasti_tidak_menulis_permintaan_kedua(kotak_press: Path):
    with ModPalsu(kotak_press, envelope=None) as mod:
        C.minta_world_press("left", 12, folder=kotak_press, batas_detik=TUNGGU_MOD)
    assert mod.permintaan is not None
    assert not (kotak_press / C.NAMA_REQUEST).exists()
    assert not (kotak_press / (C.NAMA_REQUEST + ".tmp")).exists()


def test_menu_step_tetap_jalan_tanpa_world_press_enabled(tmp_path: Path):
    from test_stardew_command_inbox import envelope

    tulis_sesi(tmp_path)
    sesi = C.baca_sesi(tmp_path)
    assert sesi.world_press_enabled is False
    with ModPalsu(tmp_path, envelope()):
        hasil = C.minta_menu_step("down", folder=tmp_path)
    assert hasil.sukses and hasil.outcome == "Stepped"


def test_water_once_tetap_jalan_tanpa_world_press_enabled(tmp_path: Path):
    from test_stardew_water_once_inbox import SESI_WATER

    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI_WATER), encoding="utf-8")
    sesi = C.baca_sesi(tmp_path)
    assert sesi.world_press_enabled is False
    with ModPalsu(tmp_path, envelope_water()):
        hasil = C.minta_water_once(10, 8, folder=tmp_path)
    assert hasil.sukses and hasil.outcome == "ObservedWatered"


def test_menu_step_tidak_menganggap_press_completed_sebagai_stepped(tmp_path: Path):
    from test_stardew_command_inbox import envelope

    tulis_sesi(tmp_path)
    with ModPalsu(tmp_path, envelope(outcome="PressCompleted", success=True, op="menu_step")):
        hasil = C.minta_menu_step("down", folder=tmp_path)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert hasil.outcome == "PressCompleted"


SESI_INPUT = {
    **SESI_PRESS,
    "location_id": "farm",
    "runtime_instance_id": "runtime-aaa",
    "owner_id": "owner-1",
    "generation": 1,
    "control_seq": 1,
}


def envelope_input_once(**ubah) -> dict:
    isi = envelope_press(op="input_once")
    isi.update(ubah)
    return isi


@pytest.fixture
def kotak_input(tmp_path: Path) -> Path:
    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI_INPUT), encoding="utf-8")
    return tmp_path


def test_input_once_membawa_lease_dan_args_ketat(kotak_input: Path):
    with ModPalsu(kotak_input, envelope_input_once()) as mod:
        hasil = C.minta_input_once(
            "LEFT",
            12,
            owner_id="owner-1",
            generation=1,
            control_seq=1,
            context_generation=1,
            folder=kotak_input,
        )
    assert hasil.sukses and hasil.outcome == "PressCompleted"
    assert (hasil.mentah or {}).get("moved") is None
    req = mod.permintaan
    assert req is not None
    assert req["op"] == "input_once"
    assert req["family"] == "world_key"
    assert req["primitive"] == "move_cardinal"
    assert req["args"] == {"direction": "left", "ticks": 12}
    assert req["owner_id"] == "owner-1"
    assert req["generation"] == 1
    assert req["control_seq"] == 1
    assert req["context_generation"] == 1
    assert req["runtime_instance_id"] == "runtime-aaa"
    assert req["expected_location_id"] == "farm"
    assert req["expected_screen_id"] == 0
    assert "keycode" not in req
    assert "method" not in req
    assert "path" not in req
    assert "SButton" not in req
    assert "direction" not in req
    assert "ticks" not in req


def test_input_once_argumen_tidak_sah_tidak_menulis(kotak_input: Path):
    with pytest.raises(C.KesalahanPerintah):
        C.minta_input_once(
            "left",
            0,
            owner_id="owner-1",
            generation=1,
            control_seq=1,
            context_generation=1,
            folder=kotak_input,
        )
    with pytest.raises(C.KesalahanPerintah):
        C.minta_input_once(
            "left",
            12,
            owner_id="",
            generation=1,
            control_seq=1,
            context_generation=1,
            folder=kotak_input,
        )
    assert not (kotak_input / C.NAMA_REQUEST).exists()


def test_input_once_interrupted_bukan_sukses(kotak_input: Path):
    with ModPalsu(
        kotak_input,
        envelope_input_once(
            accepted=True,
            status="interrupted",
            success=False,
            outcome=None,
            reject_reason="StopLatched",
        ),
    ):
        hasil = C.minta_input_once(
            "left",
            8,
            owner_id="owner-1",
            generation=1,
            control_seq=1,
            context_generation=1,
            folder=kotak_input,
        )
    assert hasil.status == C.TERPUTUS and not hasil.sukses
    assert hasil.outcome != "PressCompleted"


def test_input_once_tidak_pasti_tidak_menulis_kedua(kotak_input: Path):
    with ModPalsu(kotak_input, envelope=None) as mod:
        C.minta_input_once(
            "left",
            12,
            owner_id="owner-1",
            generation=1,
            control_seq=1,
            context_generation=1,
            folder=kotak_input,
            batas_detik=TUNGGU_MOD,
        )
    assert mod.permintaan is not None
    assert not (kotak_input / C.NAMA_REQUEST).exists()
