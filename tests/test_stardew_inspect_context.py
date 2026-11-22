"""G2 bounded slice: inspect_context read-only via existing command inbox."""
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
    ModPalsu,
)

SESI_CONTEXT = {
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
def kotak_context(tmp_path: Path) -> Path:
    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI_CONTEXT), encoding="utf-8")
    return tmp_path


def envelope_context(**ubah) -> dict:
    isi = {
        "protocol": 1,
        "ev": "command_result",
        "op": "inspect_context",
        "session_id": "sesi-abc",
        "player_id": 100000000012,
        "screen_id": 0,
        "location_id": "Farm",
        "accepted": True,
        "status": "completed",
        "reject_reason": None,
        "outcome": "ContextInspected",
        "success": True,
        "completed_unix_ms": 1,
        "captured_ticks": 40,
        "runtime_instance_id": "runtime-test-001",
        "context_generation": 1,
        "ui_revision": 1,
        "viewport_transform_revision": 1,
        "context": {
            "runtime_instance_id": "runtime-test-001",
            "context_generation": 1,
            "session_id": "sesi-abc",
            "player_id": 100000000012,
            "screen_id": 0,
            "ui_revision": 1,
            "viewport_transform_revision": 1,
            "location_id": "Farm",
            "mode": "gameplay",
            "is_player_free": True,
            "can_player_move": True,
            "capabilities": {
                "inspect_context": {"available": True},
                "inspect_tiles": {"available": True},
                "world_key": {"available": False, "reason": "backend_disabled"},
                "ui_pointer": {"available": False, "reason": "not_implemented"},
            },
        },
    }
    isi.update(ubah)
    return isi


def test_permintaan_inspect_context_membawa_identitas(kotak_context: Path):
    with ModPalsu(kotak_context, envelope_context()) as mod:
        hasil = C.minta_inspect_context(folder=kotak_context)

    assert hasil.sukses and hasil.outcome == "ContextInspected"
    req = mod.permintaan
    assert req is not None
    assert req["op"] == "inspect_context"
    assert req["expected_location_id"] == "Farm"
    assert req["expected_screen_id"] == 0
    assert req["session_id"] == SESI_CONTEXT["session_id"]
    assert "center_x" not in req


def test_inspect_context_tidak_butuh_gerbang_motor(kotak_context: Path):
    sesi = C.baca_sesi(kotak_context)
    assert sesi.input_busy is True
    assert sesi.menu_step_enabled is False
    with ModPalsu(kotak_context, envelope_context()):
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert hasil.sukses


def test_menu_step_tetap_menolak_input_busy(kotak_context: Path):
    isi = dict(SESI_CONTEXT)
    isi["menu_step_enabled"] = True
    (kotak_context / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    hasil = C.minta_menu_step("down", folder=kotak_context, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert "input menu/dunia" in (hasil.alasan or "")


def test_mode_menu_vs_gameplay(kotak_context: Path):
    with ModPalsu(kotak_context, envelope_context()):
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert hasil.sukses
    assert hasil.mentah["context"]["mode"] == "gameplay"
    assert hasil.mentah["context"]["session_id"] == "sesi-abc"
    assert hasil.mentah["context"]["player_id"] == 100000000012
    assert "interactive" not in hasil.mentah["context"]

    menu = envelope_context()
    menu["context"] = dict(menu["context"])
    menu["context"]["mode"] = "menu"
    menu["context"]["interactive"] = {
        "receiver_token": "rcv:mi1",
        "page_token": "pg:mi1:DialogueBox",
        "menu_type": "DialogueBox",
        "visible_text": "Halo.",
        "speaker": "Robin",
        "focus_target_token": "tgt:mi1:dlg:42",
    }
    with ModPalsu(kotak_context, menu):
        hasil_menu = C.minta_inspect_context(folder=kotak_context)
    assert hasil_menu.sukses
    assert hasil_menu.mentah["context"]["mode"] == "menu"
    assert hasil_menu.mentah["context"]["interactive"]["visible_text"] == "Halo."


def test_identitas_hasil_tidak_cocok_bukan_sukses(kotak_context: Path):
    palsu = envelope_context(screen_id=9)
    palsu["context"] = dict(palsu["context"])
    palsu["context"]["screen_id"] = 9
    with ModPalsu(kotak_context, palsu):
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert "identitas hasil" in (hasil.alasan or "")


@pytest.mark.parametrize(
    "mutasi",
    [
        "envelope_session",
        "envelope_player",
        "context_session",
        "context_player",
        "context_runtime",
        "context_generation",
        "context_location",
        "context_screen",
        "context_ui_revision",
        "context_viewport",
    ],
)
def test_pin_campuran_bukan_sukses(kotak_context: Path, mutasi: str):
    palsu = envelope_context()
    palsu["context"] = dict(palsu["context"])
    if mutasi == "envelope_session":
        palsu["session_id"] = "sesi-lain"
    elif mutasi == "envelope_player":
        palsu["player_id"] = 1
    elif mutasi == "context_session":
        palsu["context"]["session_id"] = "sesi-lain"
    elif mutasi == "context_player":
        palsu["context"]["player_id"] = 1
    elif mutasi == "context_runtime":
        palsu["context"]["runtime_instance_id"] = "runtime-lain"
    elif mutasi == "context_generation":
        palsu["context"]["context_generation"] = 99
    elif mutasi == "context_location":
        palsu["context"]["location_id"] = "BusStop"
    elif mutasi == "context_screen":
        palsu["context"]["screen_id"] = 9
    elif mutasi == "context_ui_revision":
        palsu["context"]["ui_revision"] = 8
    elif mutasi == "context_viewport":
        palsu["context"]["viewport_transform_revision"] = 8
    with ModPalsu(kotak_context, palsu):
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert "identitas hasil" in (hasil.alasan or "")


@pytest.mark.parametrize(
    "ubah,potongan",
    [
        ({"inbox_enabled": False}, "MenuStepCommandInboxEnabled"),
        ({"is_main_player": True}, "HOST"),
        ({"is_split_screen": True}, "split-screen"),
    ],
)
def test_sesi_context_gates(kotak_context: Path, ubah, potongan):
    isi = dict(SESI_CONTEXT)
    isi.update(ubah)
    (kotak_context / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")
    hasil = C.minta_inspect_context(folder=kotak_context, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert potongan in (hasil.alasan or "")


def test_hanya_context_inspected_yang_boleh_sukses(kotak_context: Path):
    with ModPalsu(kotak_context, envelope_context(outcome="Inspected")):
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert not hasil.sukses


def test_tidak_klaim_hud_chat(kotak_context: Path):
    with ModPalsu(kotak_context, envelope_context()) as mod:
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert hasil.sukses
    blob = json.dumps(hasil.mentah).lower()
    assert "hud_messages" not in blob
    assert "chat_box" not in blob
    assert mod.permintaan is not None


def test_text_input_token_grounded_diteruskan(kotak_context: Path):
    menu = envelope_context()
    menu["context"] = dict(menu["context"])
    menu["context"]["mode"] = "menu"
    menu["context"]["interactive"] = {
        "receiver_token": "rcv:mi1",
        "page_token": "pg:mi1:DialogueBox",
        "menu_type": "DialogueBox",
        "text_input_token": "txt:ti1",
    }
    with ModPalsu(kotak_context, menu):
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert hasil.sukses
    assert hasil.mentah["context"]["interactive"]["text_input_token"] == "txt:ti1"
    assert hasil.mentah["context"]["interactive"]["receiver_token"] == "rcv:mi1"


def test_tanpa_text_input_token_klien_tidak_mengarang(kotak_context: Path):
    menu = envelope_context()
    menu["context"] = dict(menu["context"])
    menu["context"]["mode"] = "menu"
    menu["context"]["interactive"] = {
        "receiver_token": "rcv:mi1",
        "page_token": "pg:mi1:DialogueBox",
        "menu_type": "DialogueBox",
    }
    with ModPalsu(kotak_context, menu):
        hasil = C.minta_inspect_context(folder=kotak_context)
    assert hasil.sukses
    interactive = hasil.mentah["context"]["interactive"]
    assert "text_input_token" not in interactive
    blob = json.dumps(hasil.mentah)
    assert "txt:mi1" not in blob
