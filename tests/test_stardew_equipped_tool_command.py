"""#208 strict equipped-tool publication; no game/provider/private config."""
import json

import pytest

from tests.test_stardew_leased_water_command import C, LEASE, SESSION, write_session


TOOL = dict(slot=0, qualified_item_id="(T)Axe", tile_x=3, tile_y=8, facing=1)


def call(folder, **changes):
    args = dict(TOOL, ticks=3)
    args.update(changes)
    return C.minta_use_tool_once(folder=folder, **LEASE, **args)


def test_tool_publication_is_typed_guarded_and_independent_of_movement(tmp_path, monkeypatch):
    write_session(tmp_path, world_press_enabled=False, use_tool_enabled=True)
    seen = []
    def wait(folder, request_id, timeout):
        payload = json.loads((folder / C.NAMA_REQUEST).read_text())
        seen.append(payload)
        return C.HasilPerintah(C.TIDAK_DIAMBIL, False, request_id)
    monkeypatch.setattr(C, "_tunggu_hasil", wait)
    call(tmp_path)
    assert len(seen) == 1
    assert seen[0]["primitive"] == "use_tool"
    assert seen[0]["family"] == "world_key"
    assert seen[0]["args"] == dict(TOOL, ticks=3)
    assert seen[0]["op"] == C.OP_INPUT_ONCE


@pytest.mark.parametrize("gate", [False, None, 1, "true"])
def test_missing_or_nonboolean_tool_gate_publishes_nothing(tmp_path, gate):
    write_session(tmp_path, use_tool_enabled=gate)
    assert call(tmp_path).status == C.GAGAL_LOKAL
    assert not (tmp_path / C.NAMA_REQUEST).exists()


@pytest.mark.parametrize("changes", [dict(slot=-1), dict(slot=True), dict(ticks=61),
    dict(ticks=0), dict(facing=4), dict(tile_x=-1), dict(qualified_item_id=""),
    dict(qualified_item_id=3)])
def test_invalid_tool_target_rejected_before_publication(tmp_path, changes):
    write_session(tmp_path, use_tool_enabled=True)
    with pytest.raises(C.KesalahanPerintah):
        call(tmp_path, **changes)
    assert not (tmp_path / C.NAMA_REQUEST).exists()


def test_tool_publication_supersession_and_journal_pin_coverage(tmp_path, monkeypatch):
    write_session(tmp_path, use_tool_enabled=True)
    def deny(publish):
        raise RuntimeError("superseded")
    with pytest.raises(RuntimeError, match="superseded"):
        C.minta_use_tool_once(ticks=3, **TOOL, **LEASE, folder=tmp_path, guarded_publish=deny)
    assert not (tmp_path / C.NAMA_REQUEST).exists()
    payloads = []
    def write(folder, payload):
        payloads.append(payload)
    monkeypatch.setattr(C, "_tulis_permintaan", write)
    monkeypatch.setattr(C, "_tunggu_hasil", lambda f, r, t: C.HasilPerintah(C.TIDAK_DIAMBIL, False, r))
    for changes in [{}, dict(slot=1), dict(qualified_item_id="(T)Pickaxe"),
                    dict(tile_x=4), dict(tile_y=9), dict(facing=2), dict(ticks=2)]:
        call(tmp_path, **changes)
    for payload in payloads:
        payload["deadline_unix_ms"] = 1000
    assert len({C._journal_payload_digest(p) for p in payloads}) == len(payloads)


def test_native_digest_vector():
    payload = dict(protocol=1, op="input_once", request_id="tool-1", runtime_instance_id="runtime",
        session_id="session", owner_id="owner", generation=1, control_seq=1, context_generation=7,
        expected_player_id=42, expected_player_name="arti", expected_screen_id=0, expected_location_id="farm",
        family="world_key", primitive="use_tool", args=dict(TOOL, ticks=3), deadline_unix_ms=1000)
    assert C._journal_payload_digest(payload) == "2a35a6c17d62056dfee2e7e4356ce142a1eebd9141bc4a019176a0e6690166e9"
