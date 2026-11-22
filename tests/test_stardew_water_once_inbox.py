"""P5-R3D: satu pemanggilan Python -> satu water_once -> satu putusan.

Memakai kotak masuk berkas yang sama dengan menu_step. Yang dijaga:

1. permintaan sah membawa tile_x/tile_y dan identitas sesi;
2. argumen di luar batas tidak pernah menulis berkas;
3. gerbang sesi/deadline/satu-slot/busy menolak sebelum tulis;
4. sukses hanya ObservedWatered; status native lain disalin apa adanya;
5. tidak_diambil vs tidak_pasti, tanpa retry otomatis;
6. menu_step tidak bergeser.
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

SESI_WATER = {
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
    "water_once_enabled": True,
    "world_ready": True,
    "inflight_request_id": None,
    "input_busy": False,
}


@pytest.fixture
def kotak_water(tmp_path: Path) -> Path:
    tulis_sesi(tmp_path, **{k: v for k, v in SESI_WATER.items() if k != "protocol"})
    # tulis_sesi starts from SESI_SIAP then overlay. Force water flags.
    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI_WATER), encoding="utf-8")
    return tmp_path


def envelope_water(**ubah) -> dict:
    isi = {
        "protocol": 1,
        "ev": "command_result",
        "op": "water_once",
        "session_id": "sesi-abc",
        "accepted": True,
        "status": "completed",
        "reject_reason": None,
        "arm_status": "Queued",
        "run_id": 3,
        "outcome": "ObservedWatered",
        "success": True,
        "completed_unix_ms": 1,
    }
    isi.update(ubah)
    return isi


def test_permintaan_water_membawa_identitas_ubin_deadline(kotak_water: Path):
    with ModPalsu(kotak_water, envelope_water()) as mod:
        hasil = C.minta_water_once(53, 17, folder=kotak_water)

    assert hasil.sukses and hasil.outcome == "ObservedWatered"
    req = mod.permintaan
    assert req is not None
    assert set(req) == {
        "protocol",
        "op",
        "request_id",
        "session_id",
        "expected_player_id",
        "expected_player_name",
        "tile_x",
        "tile_y",
        "deadline_unix_ms",
    }
    assert req["op"] == "water_once"
    assert req["tile_x"] == 53 and req["tile_y"] == 17
    assert req["session_id"] == SESI_WATER["session_id"]
    assert req["expected_player_id"] == SESI_WATER["player_id"]
    assert req["deadline_unix_ms"] > C._sekarang_ms() - 1000
    assert "direction" not in req


def test_setiap_panggilan_water_memakai_request_id_baru(kotak_water: Path):
    ids = []
    for _ in range(2):
        with ModPalsu(kotak_water, envelope_water()) as mod:
            C.minta_water_once(10, 8, folder=kotak_water)
        ids.append(mod.permintaan["request_id"])
    assert ids[0] != ids[1]


@pytest.mark.parametrize(
    "x,y",
    [
        (-1, 8),
        (10, -1),
        (C.TILE_MAX + 1, 8),
        (10, C.TILE_MAX + 1),
        (True, 8),
        (10, False),
        (10.5, 8),
        ("10", 8),
    ],
)
def test_ubin_tidak_sah_tidak_pernah_menulis_berkas(kotak_water: Path, x, y):
    with pytest.raises(C.KesalahanPerintah):
        C.minta_water_once(x, y, folder=kotak_water)
    assert not (kotak_water / C.NAMA_REQUEST).exists()


@pytest.mark.parametrize(
    "ubah,potongan",
    [
        ({"inbox_enabled": False}, "MenuStepCommandInboxEnabled"),
        ({"water_once_enabled": False}, "WaterOnceEnabled"),
        ({"world_ready": False}, "dunia belum siap"),
        ({"is_main_player": True}, "HOST"),
        ({"is_split_screen": True}, "split-screen"),
        ({"inflight_request_id": "lain"}, "masih ada perintah berjalan"),
        ({"input_busy": True}, "input menu/dunia"),
        ({"session_id": ""}, "session_id"),
    ],
)
def test_sesi_water_belum_siap_menolak_tanpa_menulis(kotak_water: Path, ubah, potongan):
    isi = dict(SESI_WATER)
    isi.update(ubah)
    (kotak_water / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    hasil = C.minta_water_once(10, 8, folder=kotak_water, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert not hasil.sukses
    assert potongan in (hasil.alasan or "")
    assert not (kotak_water / C.NAMA_REQUEST).exists()


def test_water_tidak_butuh_menu_step_enabled(kotak_water: Path):
    assert C.baca_sesi(kotak_water).menu_step_enabled is False
    with ModPalsu(kotak_water, envelope_water()):
        hasil = C.minta_water_once(10, 8, folder=kotak_water)
    assert hasil.sukses


def test_slot_terpakai_tidak_ditimpa_water(kotak_water: Path):
    (kotak_water / C.NAMA_REQUEST).write_text(
        '{"request_id":"punya-orang-lain"}', encoding="utf-8"
    )
    hasil = C.minta_water_once(10, 8, folder=kotak_water, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert baca_permintaan(kotak_water)["request_id"] == "punya-orang-lain"


@pytest.mark.parametrize(
    "ubah",
    [
        {"outcome": "AlreadyWatered", "success": False},
        {"outcome": "UnknownTimedOut", "success": False},
        {"outcome": "UnknownBeginError", "success": False},
        {"outcome": "UnknownReleaseError", "success": False},
        {"outcome": "ContextChanged", "success": False},
        {"outcome": "Queued", "success": False},
        {"outcome": "AlreadyWatered", "success": True},
        {"outcome": "ObservedWatered", "success": False},
        {"outcome": "Stepped", "success": True},
    ],
)
def test_hanya_observed_watered_yang_boleh_sukses(kotak_water: Path, ubah):
    with ModPalsu(kotak_water, envelope_water(**ubah)):
        hasil = C.minta_water_once(10, 8, folder=kotak_water)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert hasil.pasti
    if "outcome" in ubah:
        assert hasil.outcome == ubah["outcome"]


def test_observed_watered_penuh_adalah_sukses(kotak_water: Path):
    with ModPalsu(kotak_water, envelope_water()):
        hasil = C.minta_water_once(10, 8, folder=kotak_water)
    assert hasil.status == C.SELESAI and hasil.sukses
    assert hasil.outcome == "ObservedWatered"


def test_water_ditolak_membawa_status_native(kotak_water: Path):
    with ModPalsu(
        kotak_water,
        envelope_water(
            accepted=False,
            status="rejected",
            reject_reason="ArmRejected",
            arm_status="AlreadyWatered",
            outcome=None,
            success=False,
        ),
    ):
        hasil = C.minta_water_once(10, 8, folder=kotak_water)
    assert hasil.status == C.DITOLAK and not hasil.sukses
    assert "AlreadyWatered" in (hasil.alasan or "")


def test_water_timeout_mempertahankan_permintaan_dan_tidak_pasti(kotak_water: Path):
    hasil = C.minta_water_once(10, 8, folder=kotak_water, batas_detik=0.3)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.pasti and not hasil.sukses
    assert json.loads((kotak_water / C.NAMA_REQUEST).read_text())["request_id"] == hasil.request_id
    assert "JANGAN ulangi otomatis" in hasil.alasan


def test_water_sudah_diambil_tanpa_hasil_adalah_tidak_pasti(kotak_water: Path):
    with ModPalsu(kotak_water, envelope=None):
        hasil = C.minta_water_once(10, 8, folder=kotak_water, batas_detik=TUNGGU_MOD)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.pasti and not hasil.sukses
    assert "JANGAN ulangi otomatis" in (hasil.alasan or "")


def test_water_tidak_pasti_tidak_menulis_permintaan_kedua(kotak_water: Path):
    with ModPalsu(kotak_water, envelope=None) as mod:
        C.minta_water_once(10, 8, folder=kotak_water, batas_detik=TUNGGU_MOD)
    assert mod.permintaan is not None
    assert not (kotak_water / C.NAMA_REQUEST).exists()
    assert not (kotak_water / (C.NAMA_REQUEST + ".tmp")).exists()


def test_menu_step_tetap_jalan_tanpa_water_once_enabled(tmp_path: Path):
    from test_stardew_command_inbox import envelope

    tulis_sesi(tmp_path)
    sesi = C.baca_sesi(tmp_path)
    assert sesi.water_once_enabled is False
    with ModPalsu(tmp_path, envelope()):
        hasil = C.minta_menu_step("down", folder=tmp_path)
    assert hasil.sukses and hasil.outcome == "Stepped"


def test_menu_step_tidak_menganggap_observed_watered_sebagai_stepped(tmp_path: Path):
    from test_stardew_command_inbox import envelope

    tulis_sesi(tmp_path)
    with ModPalsu(tmp_path, envelope(outcome="ObservedWatered", success=True, op="menu_step")):
        hasil = C.minta_menu_step("down", folder=tmp_path)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert hasil.outcome == "ObservedWatered"
