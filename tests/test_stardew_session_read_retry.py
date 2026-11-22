"""session.json is replaced atomically by the mod; a read that lands inside the rename must not
turn a running lease into an uncertain action (live 2026-10-06 attempt 10a)."""
import json
import threading
import time

import pytest

import arti_stardew_command as command
import arti_stardew_runtime as runtime_module
from tests.test_stardew_runtime_move import rig  # noqa: F401  (fixture)


def _session(folder, **extra):
    raw = dict(protocol=1, session_id="command-1", runtime_instance_id="runtime-1", player_id=7,
               player_name="Arti", screen_id=0, location_id="Farm", is_main_player=False,
               is_split_screen=False, inbox_enabled=True, world_press_enabled=True, world_ready=True,
               input_busy=False, inflight_request_id=None, release_requested=False, release_acked=True)
    raw.update(extra)
    (folder / command.NAMA_SESSION).write_text(json.dumps(raw), encoding="utf-8")


def test_read_without_retry_fails_at_once(tmp_path):
    started = time.monotonic()
    with pytest.raises(command.KesalahanPerintah):
        command.baca_sesi(tmp_path)
    assert time.monotonic() - started < 0.2


def test_read_retries_until_the_file_reappears(tmp_path):
    def reappear():
        time.sleep(0.08)
        _session(tmp_path)
    threading.Thread(target=reappear, daemon=True).start()
    session = command.baca_sesi(tmp_path, retry_detik=0.5)
    assert session.session_id == "command-1"


def test_read_retry_is_bounded(tmp_path):
    started = time.monotonic()
    with pytest.raises(command.KesalahanPerintah):
        command.baca_sesi(tmp_path, retry_detik=0.1)
    assert 0.09 <= time.monotonic() - started < 1.0


def test_lease_poll_survives_a_momentary_missing_session(rig, monkeypatch):  # noqa: F811
    rt, _motor, _obs, raw, _calls, publish, folder = rig
    pin = runtime_module._WaterLeasePin(folder=folder, session=command.baca_sesi(folder),
                                        owner="owner-1", generation=1)
    target = folder / command.NAMA_SESSION
    content = target.read_text(encoding="utf-8")
    target.unlink()

    def restore():
        time.sleep(0.05)
        target.write_text(content, encoding="utf-8")
    threading.Thread(target=restore, daemon=True).start()
    assert rt._water_bound_session(pin).session_id == "command-1"


def test_lease_poll_still_fails_closed_on_binding_change(rig):  # noqa: F811
    rt, _motor, _obs, raw, _calls, publish, folder = rig
    pin = runtime_module._WaterLeasePin(folder=folder, session=command.baca_sesi(folder),
                                        owner="owner-1", generation=1)
    raw.update(location_id="Town")
    publish()
    with pytest.raises(ValueError, match="lease_binding_changed"):
        rt._water_bound_session(pin)


def test_wait_lease_survives_a_momentary_missing_session(rig):  # noqa: F811
    """Live 2026-10-07 (M1 attempt 10s): the lease wait read session.json inside the mod's atomic
    rename; the approach step was not_taken and the trial held."""
    rt, _motor, _obs, raw, _calls, publish, folder = rig
    session = command.baca_sesi(folder)
    target = folder / command.NAMA_SESSION
    content = target.read_text(encoding="utf-8")
    target.unlink()

    def restore():
        time.sleep(0.05)
        target.write_text(content, encoding="utf-8")
    threading.Thread(target=restore, daemon=True).start()
    current = rt._wait_lease(folder, session, "owner-1", 1, release=True)
    assert current.session_id == "command-1"
