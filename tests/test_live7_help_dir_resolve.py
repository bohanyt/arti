"""Live7 help dir follows the active cycle's run folder (both dir layouts)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import arti_help_bus as bus
import arti_stardew_driver_guard as guard
from arti_help_relay import HelpRelay, Live7HelpChannelResolver


def make_request(number=1, **changes):
    values = dict(request_id=f"{number:032x}", session_id="fake-session",
                  reason="no_progress_frozen_clock", game_date="Y1 Spring 3",
                  game_time=910, location="Farm", skill="walk", detail="clock frozen at 0910",
                  created_at=10000.0, expires_at=20000.0)
    values.update(changes)
    return bus.HelpRequest(**values)


def make_cycle(run: Path, cycle="c1", *, status=None, lock=False, stop_file="default",
               age=0.0, raw=None) -> Path:
    """<run>/live7/<cycle>/ like live_cycle.py writes it."""
    folder = run / "live7" / cycle
    folder.mkdir(parents=True, exist_ok=True)
    if status is not None or raw is not None:
        state = {"schema": "live7_cycle.v1", "status": status, "cycle": cycle}
        if stop_file == "default":
            state["live7_stop_file"] = str((run / "LIVE7_STOP").resolve())
        elif stop_file is not None:
            state["live7_stop_file"] = str(stop_file)
        path = folder / "cycle_state.json"
        path.write_text(raw if raw is not None else json.dumps(state), encoding="utf-8")
        if age:
            old = time.time() - age
            os.utime(path, (old, old))
    if lock:
        (folder / "supervisor.lock").write_text("x", encoding="utf-8")
    return folder


def same(a, b) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def run_dirs(config):
    return [c.run_dir for c in guard.active_live7_cycles(config)]


def test_runs_root_with_locked_cycle_resolves_run_folder(tmp_path):
    root = tmp_path / "run"
    run = root / "live7-smoke-20261023"
    make_cycle(run, status="ready", lock=True)
    dirs = run_dirs({"live7_help_dir": str(root)})
    assert len(dirs) == 1 and same(dirs[0], run)
    lines = []
    channel = Live7HelpChannelResolver({"live7_help_dir": str(root)}, str(root), announce=lines.append)()
    assert same(channel.help_dir, run)
    assert lines == [f"[Help] file channel → {dirs[0]} (cycle c1)"]


@pytest.mark.parametrize("key", ["live7_help_dir", "stardew_live7_help_dir"])
def test_configured_dir_is_the_run_folder(tmp_path, key):
    run = tmp_path / "live7-smoke-20261023"
    make_cycle(run, status="running")
    cfg = {key: str(run)}
    dirs = run_dirs(cfg)
    assert len(dirs) == 1 and same(dirs[0], run)
    # The startup two-driver guard sees the cycle in this layout too.
    assert same(guard.active_live7_run_dir(cfg), run)


def test_command_dir_parent_is_a_candidate(tmp_path):
    run = tmp_path / "live7-smoke"
    make_cycle(run, status="host_wait")
    assert same(guard.active_live7_run_dir({"stardew_command_dir": str(run / "commands")}), run)


def test_both_layouts_dedupe_to_one_cycle(tmp_path):
    root = tmp_path / "run"
    run = root / "live7-smoke"
    make_cycle(run, lock=True)
    cfg = {"live7_help_dir": str(root), "stardew_live7_help_dir": str(run),
           "stardew_command_dir": str(run / "commands")}
    cycles = guard.active_live7_cycles(cfg)
    assert len(cycles) == 1 and same(cycles[0].run_dir, run)


@pytest.mark.parametrize("status,age", [("ready", 0), ("stopped", 0), ("safety_stop", 0),
                                        ("active", 13 * 3600)])
@pytest.mark.parametrize("as_root", [False, True])
def test_resting_or_stale_cycle_is_not_active(tmp_path, status, age, as_root):
    root = tmp_path / "run"
    run = root / "live7-rehearsal-3"
    make_cycle(run, status=status, age=age)
    cfg = {"live7_help_dir": str(root if as_root else run)}
    assert guard.active_live7_cycles(cfg) == []
    assert guard.active_live7_run_dir(cfg) is None


def test_no_active_cycle_uses_static_fallback(tmp_path):
    root = tmp_path / "run"
    make_cycle(root / "live7-rehearsal-3", status="ready")
    lines = []
    channel = Live7HelpChannelResolver({"live7_help_dir": str(root)}, str(root), announce=lines.append)()
    assert channel.help_dir == root
    assert lines == [f"[Help] file channel → {root} (static fallback, no active Live7 cycle)"]
    assert Live7HelpChannelResolver({}, "", announce=lines.append)() is None
    assert lines[-1] == "[Help] file channel → off (no active Live7 cycle, no static dir)"


def test_cycle_started_after_bridge_is_picked_up_on_re_resolve(tmp_path):
    root = tmp_path / "run"
    run = root / "live7-smoke"
    run.mkdir(parents=True)
    clock, voice, lines = [10000.0], [], []
    cfg = {"live7_help_dir": str(root), "live7_help_speak_enabled": True}
    resolver = Live7HelpChannelResolver(cfg, str(root), announce=lines.append)
    relay = HelpRelay(cfg, lambda *_a, **_kw: voice.append(True) or True, file_channel=resolver(),
                      channel_resolver=resolver, resolve_interval_sec=5, clock=lambda: clock[0])
    relay.tick(10000)
    assert relay._channel.help_dir == root
    make_cycle(run, status="starting", lock=True)
    bus.FileHelpChannel(run).write_request(make_request())
    relay.tick(10001)  # cached: still the static dir, request not seen yet
    assert relay._channel.help_dir == root and voice == []
    relay.tick(10006)
    assert same(relay._channel.help_dir, run) and voice == [True]
    relay.tick(10012)
    assert len(lines) == 2 and lines[1].endswith("(cycle c1)")


def test_reply_goes_to_the_request_folder_after_dir_switch(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    current = [bus.FileHelpChannel(a)]
    clock = [10000.0]
    relay = HelpRelay({}, lambda *_a, **_kw: True, file_channel=current[0],
                      channel_resolver=lambda: current[0], resolve_interval_sec=5,
                      clock=lambda: clock[0])
    first, second = make_request(1), make_request(2, session_id="other-session")
    bus.FileHelpChannel(a).write_request(first)
    relay.tick(10000)  # admitted from A, collection window open
    current[0] = bus.FileHelpChannel(b)
    bus.FileHelpChannel(b).write_request(second)
    clock[0] = 10010
    relay.tick(10010)  # switches to B; second is suppressed (window open)
    assert relay._channel.help_dir == b
    assert bus.FileHelpChannel(b).read_reply(second.request_id).status == "suppressed"
    relay.note_owner_text("utara")
    reply = bus.FileHelpChannel(a).read_reply(first.request_id)
    assert reply.status == "hinted" and reply.hints[0].text == "utara"
    assert not (b / f"rep-{first.request_id}.json").exists()
    assert not (a / f"rep-{second.request_id}.json").exists()
    assert relay._request_channels == {}


def test_resolver_failure_keeps_current_channel(tmp_path):
    def broken():
        raise OSError("disk")

    channel = bus.FileHelpChannel(tmp_path)
    relay = HelpRelay({}, lambda *_a, **_kw: True, file_channel=channel,
                      channel_resolver=broken, clock=lambda: 10000)
    relay.tick(10000)
    assert relay._channel is channel


def test_stop_file_elsewhere_is_honoured(tmp_path):
    root = tmp_path / "run"
    elsewhere = tmp_path / "other-run"
    elsewhere.mkdir()
    make_cycle(root / "live7-smoke", status="active", stop_file=elsewhere / "LIVE7_STOP")
    dirs = run_dirs({"live7_help_dir": str(root)})
    assert len(dirs) == 1 and same(dirs[0], elsewhere)


def test_stop_file_with_missing_parent_falls_back_to_cycle_parent(tmp_path):
    run = tmp_path / "run" / "live7-smoke"
    make_cycle(run, status="active", stop_file=tmp_path / "gone" / "LIVE7_STOP")
    dirs = run_dirs({"live7_help_dir": str(run)})
    assert len(dirs) == 1 and same(dirs[0], run)


def test_garbage_json_is_not_active_and_never_raises(tmp_path):
    run = tmp_path / "live7-smoke"
    make_cycle(run, raw="{not json")
    make_cycle(run, "c2", raw="[1, 2]")
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    cfg = {"live7_help_dir": str(run), "stardew_live7_help_dir": str(tmp_path / "a-file"),
           "stardew_command_dir": str(tmp_path / "missing" / "commands")}
    assert guard.active_live7_cycles(cfg) == []
    assert guard.active_live7_run_dir(cfg) is None
    assert guard.active_live7_cycles(None) == [] and guard.active_live7_run_dir(None) is None
    # A lock still counts; the run dir then comes from the folder layout.
    make_cycle(run, "c3", raw="{not json", lock=True)
    dirs = run_dirs(cfg)
    assert len(dirs) == 1 and same(dirs[0], run)


def test_several_active_cycles_pick_newest_and_log_once(tmp_path):
    root = tmp_path / "run"
    old_run, new_run = root / "live7-old", root / "live7-new"
    make_cycle(old_run, status="active", age=600)
    make_cycle(new_run, status="active", age=10)
    lines = []
    resolver = Live7HelpChannelResolver({"live7_help_dir": str(root)}, str(root), announce=lines.append)
    assert same(resolver().help_dir, new_run)
    assert same(resolver().help_dir, new_run)
    assert len(lines) == 2
    assert lines[0].startswith("[Help] 2 Live7 cycles active; using newest")
    assert lines[1].endswith("(cycle c1)")
    assert same(guard.active_live7_run_dir({"live7_help_dir": str(root)}), new_run)
