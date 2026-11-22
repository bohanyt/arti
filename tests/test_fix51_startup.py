"""Offline startup handshake; stops before any gameplay, process, window or game port."""
from datetime import datetime, timezone
import json

import pytest

from scripts.free_play import soak


def status(folder, **changes):
    row = dict(utc=datetime.now(timezone.utc).isoformat(), run_id="offline-startup",
               state="held", host_paused=True, time=600, day=4)
    soak.write(folder / "startup.json", dict(row, **changes))


def test_offline_dry_startup_slow_join_then_native_release(tmp_path):
    status(tmp_path)
    assert soak.startup_pause(tmp_path, "offline-startup")["time"] == 600
    # Model a slow client join; elapsed wall time must not require a later morning rule.
    now = [155.5]
    def tick(seconds):
        now[0] += seconds
        assert (tmp_path / "STARTUP_RELEASE").read_text() == "offline-startup"
        status(tmp_path, state="released", host_paused=False)
    row = soak.startup_pause(tmp_path, "offline-startup", release=True,
                             clock=lambda: now[0], sleep=tick)
    assert row["time"] == 600 and not row["host_paused"]
    assert not (tmp_path / "ARMED").exists()
    assert not (tmp_path / "window.json").exists()


@pytest.mark.parametrize("changes,cause", [
    ({"run_id": "other-run"}, "identity_mismatch"),
    ({"utc": "2026-01-01T00:00:00Z"}, "stale_telemetry"),
    ({"time": 930}, "morning_window_expired"),
])
def test_startup_refuses_bad_witness_before_release(tmp_path, changes, cause):
    status(tmp_path, **changes)
    with pytest.raises(RuntimeError, match=cause):
        soak.startup_pause(tmp_path, "offline-startup", release=True)
    assert not (tmp_path / "STARTUP_RELEASE").exists()


def test_startup_names_helper_failure_and_missing_ack(tmp_path):
    (tmp_path / "HOST_STOP").write_text("startup_pause_timeout")
    with pytest.raises(RuntimeError, match="startup_host_stopped:startup_pause_timeout"):
        soak.startup_pause(tmp_path, "offline-startup")
    (tmp_path / "HOST_STOP").unlink()
    now = [0]
    def tick(s): now[0] += s
    with pytest.raises(RuntimeError, match="startup_pause_held_timeout"):
        soak.startup_pause(tmp_path, "offline-startup", timeout_s=1, clock=lambda: now[0], sleep=tick)


def test_startup_order_keeps_bridge_wait_paused_and_rechecks_before_arm():
    import inspect
    body = inspect.getsource(soak._run_soak)
    assert body.index('startup_pause(host_out, run.name)') < body.index('["launch-farmhand"')
    assert body.index('realpath.wait_ready()') < body.index('startup_pause(host_out, run.name, release=True)')
    assert body.index('"pre-arm-ready.log"') < body.index('ipc.publish(host_out / "ARMED"')
