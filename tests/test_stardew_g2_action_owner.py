"""G2-0 control/stop client helpers. Cloud-safe, tanpa game."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import arti_stardew_command as C  # noqa: E402

SESI = {
    "protocol": 1,
    "ev": "command_session",
    "session_id": "sesi-abc",
    "player_id": 100000000012,
    "player_name": "artihand",
    "screen_id": 0,
    "is_main_player": False,
    "is_split_screen": False,
    "inbox_enabled": True,
    "menu_step_enabled": True,
    "world_ready": True,
    "inflight_request_id": None,
    "input_busy": False,
    "runtime_instance_id": "runtime-aaa",
}


def test_tulis_kontrol_dan_stop_atomik(tmp_path: Path) -> None:
    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI), encoding="utf-8")
    sesi = C.baca_sesi(tmp_path)
    C.tulis_kontrol(
        C.KIND_ACQUIRE,
        owner_id="owner-1",
        generation=1,
        control_seq=1,
        folder=tmp_path,
        sesi=sesi,
    )
    control = json.loads((tmp_path / C.NAMA_CONTROL).read_text(encoding="utf-8"))
    assert control["kind"] == "acquire"
    assert control["runtime_instance_id"] == "runtime-aaa"
    assert control["generation"] == 1
    C.tulis_stop(generation=1, folder=tmp_path, sesi=sesi, owner_id="owner-1")
    stop = json.loads((tmp_path / C.NAMA_STOP).read_text(encoding="utf-8"))
    assert "kind" not in stop
    assert stop["generation"] == 1
    C.bersihkan_slot(tmp_path)
    assert (tmp_path / C.NAMA_STOP).exists()


def test_kind_control_salah_gagal_tertutup(tmp_path: Path) -> None:
    (tmp_path / C.NAMA_SESSION).write_text(json.dumps(SESI), encoding="utf-8")
    with pytest.raises(C.KesalahanPerintah):
        C.tulis_kontrol(
            "lease",
            owner_id="owner-1",
            generation=1,
            control_seq=1,
            folder=tmp_path,
        )


def test_legacy_request_tanpa_bukti_lease() -> None:
    """menu_step/world_press/water_once tidak membawa bukti owner/generation."""
    src = (ROOT / "arti_stardew_command.py").read_text(encoding="utf-8")
    for fn in ("minta_menu_step", "minta_water_once", "minta_world_press"):
        start = src.index(f"def {fn}(")
        nxt = src.find("\ndef ", start + 1)
        body = src[start:nxt]
        assert '"op"' in body
        assert "owner_id" not in body
        assert "generation" not in body
        assert "control_seq" not in body
    for fn in ("minta_inspect_tiles", "minta_inspect_context"):
        start = src.index(f"def {fn}(")
        nxt = src.find("\ndef ", start + 1)
        body = src[start:nxt]
        assert "owner_id" not in body

    start = src.index("def _minta_input_once(")
    nxt = src.find("\ndef ", start + 1)
    body = src[start:nxt]
    assert '"op": OP_INPUT_ONCE' in body or '"op":' in body
    assert "owner_id" in body
    assert "generation" in body
    assert "control_seq" in body
    assert "context_generation" in body
    assert "FAMILY_WORLD_KEY" in body
    assert "PRIMITIVE_MOVE_CARDINAL" in body
    assert "keycode" not in body
    assert "retry" not in body.lower() or "Tidak pernah retry" in body


def test_poller_bukan_transport_kedua() -> None:
    src = (
        ROOT
        / "stardew-mod"
        / "ArtiStardewBridge"
        / "Protocol"
        / "ActionOwnerRecordPoller.cs"
    ).read_text(encoding="utf-8")
    assert "DefaultIntervalMs = 50" in src
    assert "request.json" not in src
    assert "Game1" not in src


def test_invalid_stop_fail_closed_lewat_owner_yang_sama() -> None:
    owner = (
        ROOT
        / "stardew-mod"
        / "ArtiStardewBridge"
        / "Protocol"
        / "NativeActionOwner.cs"
    ).read_text(encoding="utf-8")
    entry = (
        ROOT / "stardew-mod" / "ArtiStardewBridge" / "ModEntry.cs"
    ).read_text(encoding="utf-8")
    apply_at = entry.index("void ApplyActionOwnerRecords(")
    apply_body = entry[apply_at : apply_at + 1800]
    assert "ConsumeRecordHandoff" in apply_body
    assert "File.Exists" not in apply_body
    assert "ReadAllText" not in apply_body
    consume_at = owner.index("ConsumeRecordHandoff(")
    consume_body = owner[consume_at : consume_at + 2200]
    assert "StopPresent" in consume_body
    assert "ApplyRecordFault" in consume_body
    assert consume_body.index("ApplyRecordFault") < consume_body.index("ApplyControl")
    assert "File.Exists" not in consume_body
    assert "ReadAllText" not in consume_body
