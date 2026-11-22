"""Offline tests for host_console.py: rehearsal-only background HOST actions via the HOST supervisor's console pipe.
Windows, the clock and the supervisor are faked; no game, no input, no real console."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "m1_operator"))
import host_console as H  # noqa: E402

HOST, HOST_PID, FH, FH_PID = 2002, 777, 1001, 888
ON = {H.FLAG_ENV: "1"}
T0 = 1_792_000_000.0
HOST_TITLE = "SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods [main player]"
FH_TITLE = "SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods [farmhand]"


class Ops:
    """Fake windows/clock. `supervisor` = rename *.pending -> *.sent on each sleep, as host_supervisor.py does."""

    def __init__(self, recovery, windows=None, supervisor=True):
        self.recovery, self.supervisor, self.now = recovery, supervisor, T0
        self.windows = windows if windows is not None else {
            HOST: dict(exists=True, pid=HOST_PID, title=HOST_TITLE), FH: dict(exists=True, pid=FH_PID, title=FH_TITLE)}

    def window(self, h):
        return self.windows.get(h, dict(exists=False))

    def foreground(self):
        return 66574                                   # rehearsal 12: an elevated window holds the foreground

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s
        if self.supervisor:
            for f in sorted((self.recovery / "host-console").glob("*.pending")):
                text = f.read_text(encoding="utf-8-sig")
                with (self.recovery / "evidence" / "HOST_CONSOLE_LOG.jsonl").open("a", encoding="utf-8") as log:
                    log.write(json.dumps(dict(utc="2026-10-22T03:00:00+00:00", file=f.name, pid=HOST_PID,
                                              command=text)) + "\n")
                f.rename(f.with_suffix(".sent"))


@pytest.fixture
def run(tmp_path):
    run_dir, rec = tmp_path / "run", tmp_path / "run" / "batch-host"
    (rec / "host-console").mkdir(parents=True)
    (rec / "evidence").mkdir()
    (rec / "host_supervisor.py").write_text("# copy")
    (rec / "host-launch.json").write_text(json.dumps(dict(pid=HOST_PID, supervisor_pid=1)))
    (run_dir / "host-hwnd.txt").write_text(str(HOST))
    (run_dir / "fh-hwnd.txt").write_text(str(FH))
    (run_dir / "farmhand-launch.json").write_text(json.dumps(dict(pid=FH_PID)))
    return run_dir, rec


def rows(run_dir):
    p = run_dir / "ui-op-log.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def files(rec):
    return sorted(p.name for p in (rec / "host-console").iterdir())


def test_sleep_is_delivered_in_the_background_and_logged_as_a_host_driver_row(run):
    run_dir, rec = run
    row, rc = H.send("sleep", rec, run_dir, Ops(rec), ON)
    assert rc == 0 and row["delivered"] is True
    sent = files(rec)
    assert len(sent) == 1 and sent[0].endswith("-sleep.sent")
    assert (rec / "host-console" / sent[0]).read_text(encoding="utf-8") == "debug sleep"
    [r] = rows(run_dir)
    assert r["op"] == "host_console" and r["action"] == "sleep" and r["command"] == "debug sleep"
    assert (r["source"], r["target"], r["host_hwnd"], r["hwnd"], r["pid"]) == ("host_driver", "host", HOST, HOST, HOST_PID)
    assert r["background"] is True and r["fg_hwnd"] == 66574


def test_flag_off_by_default_refuses_and_writes_nothing(run):
    run_dir, rec = run
    for env in ({}, {H.FLAG_ENV: "0"}, {H.FLAG_ENV: "true"}):
        row, rc = H.send("sleep", rec, run_dir, Ops(rec), env)
        assert rc == 2 and H.FLAG_ENV in row["refused"]
    assert files(rec) == [] and len(rows(run_dir)) == 3 and all(r["ok"] is False for r in rows(run_dir))


@pytest.mark.parametrize("mutate, why", [
    (lambda run_dir, rec, w: (run_dir / "host-hwnd.txt").write_text(str(FH)), "farmhand"),
    (lambda run_dir, rec, w: w[HOST].update(title=FH_TITLE), "farmhand"),
    (lambda run_dir, rec, w: (run_dir / "farmhand-launch.json").write_text(json.dumps(dict(pid=HOST_PID))), "farmhand"),
    (lambda run_dir, rec, w: w[HOST].update(pid=999), "pid"),
    (lambda run_dir, rec, w: w[HOST].update(title="SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods"), "[main player]"),
    (lambda run_dir, rec, w: w[HOST].update(exists=False), "does not exist"),
    (lambda run_dir, rec, w: (rec / "host-exit.json").write_text("{}"), "exited"),
    (lambda run_dir, rec, w: (rec / "host-launch.json").unlink(), "host-launch.json"),
    (lambda run_dir, rec, w: (run_dir / "host-hwnd.txt").unlink(), "host-hwnd.txt"),
])
def test_never_the_farmhand_or_an_unproven_host(run, mutate, why):
    run_dir, rec = run
    ops = Ops(rec)
    mutate(run_dir, rec, ops.windows)
    row, rc = H.send("sleep", rec, run_dir, ops, ON)
    assert rc == 2 and why in row["refused"]
    assert files(rec) == []


def test_one_command_in_flight(run):
    run_dir, rec = run
    (rec / "host-console" / "old-sleep.pending").write_text("debug sleep")
    row, rc = H.send("sleep", rec, run_dir, Ops(rec), ON)
    assert rc == 2 and "unsent" in row["refused"] and files(rec) == ["old-sleep.pending"]


def test_sleep_cooldown_reads_the_supervisor_log_for_this_host_pid(run):
    run_dir, rec = run
    log = rec / "evidence" / "HOST_CONSOLE_LOG.jsonl"
    recent = "2026-10-22T03:00:00+00:00"
    ops = Ops(rec)
    ops.now = H._epoch(recent) + 60
    log.write_text(json.dumps(dict(utc=recent, file="x.pending", pid=HOST_PID, command="﻿debug sleep")) + "\n")
    row, rc = H.send("sleep", rec, run_dir, ops, ON)
    assert rc == 2 and "cooldown" in row["refused"]
    log.write_text(json.dumps(dict(utc=recent, file="x.pending", pid=4242, command="debug sleep")) + "\n")  # old HOST
    assert H.send("sleep", rec, run_dir, ops, ON)[1] == 0
    ops.now += H.SLEEP_COOLDOWN_S + 1
    assert H.send("sleep", rec, run_dir, ops, ON)[1] == 0


def test_undelivered_file_is_reported_and_never_withdrawn(run):
    run_dir, rec = run
    row, rc = H.send("sleep", rec, run_dir, Ops(rec, supervisor=False), ON)
    assert rc == 3 and row["delivered"] is False and "never withdrawn" in row["note"]
    [f] = files(rec)
    assert f.endswith("-sleep.pending")
    assert H.send("sleep", rec, run_dir, Ops(rec, supervisor=False), ON)[1] == 2   # in flight: refuse more


def test_allowlist_only_no_free_text(run, capsys):
    run_dir, rec = run
    assert H.ACTIONS == {"sleep": "debug sleep", "ok": "arti_host ok", "load": "arti_host load {save}"}
    for bad in (["warp"], ["debug sleep"], ["sleep", "now"], ["ok", "now"], ["ok", "proof1_100000002"],
                ["load", "a_1", "b_2"], ["arti_host"], ["choose", "0"]):
        with pytest.raises(SystemExit) as e:
            H.main(bad + ["--run-dir", str(run_dir)], Ops(rec), ON)
        assert e.value.code == 2
    assert files(rec) == []


def test_cli_sleep_and_read_only_status(run, capsys):
    run_dir, rec = run
    assert H.main(["status", "--run-dir", str(run_dir)], Ops(rec), {}) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["flag"] is False and st["host"]["hwnd"] == HOST and st["unsent"] == []
    assert files(rec) == [] and rows(run_dir) == []
    assert H.main(["sleep", "--run-dir", str(run_dir)], Ops(rec), ON) == 0
    assert json.loads(capsys.readouterr().out)["delivered"] is True


def test_soak_audit_accepts_the_row_as_a_tagged_host_driver_row(run):
    soak = pytest.importorskip("soak")
    run_dir, rec = run
    H.send("sleep", rec, run_dir, Ops(rec), ON)
    [r] = rows(run_dir)
    assert soak.host_driver_row(r, HOST) is True
    assert r["op"] not in soak.PAD_FORBIDDEN_OPS                     # no focus, cursor or key: not a pad leak


# ---- Phase 2: `ok` and `load` (mod command arti_host, RehearsalHostConsoleEnabled on the HOST only) ----

TITLE = "SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods"         # title screen: no role marker yet
SAVE = "proof1_100000002"


@pytest.fixture
def saves(tmp_path):
    d = tmp_path / "Saves"
    (d / SAVE).mkdir(parents=True)
    (d / SAVE / SAVE).write_text("<SaveGame/>", encoding="utf-8")
    return d


def at_title(rec):
    ops = Ops(rec)
    ops.windows[HOST]["title"] = TITLE
    return ops


def test_ok_writes_exactly_the_mod_line_and_logs_a_host_driver_row(run):
    run_dir, rec = run
    row, rc = H.send("ok", rec, run_dir, Ops(rec), ON)
    assert rc == 0 and row["delivered"] is True and row["command"] == "arti_host ok"
    [f] = files(rec)
    assert f.endswith("-ok.sent") and (rec / "host-console" / f).read_text(encoding="utf-8") == "arti_host ok"
    [r] = rows(run_dir)
    assert (r["op"], r["action"], r["source"], r["target"], r["host_hwnd"]) == ("host_console", "ok", "host_driver", "host", HOST)
    assert r["background"] is True and r["fg_hwnd"] == 66574 and r["save"] is None


def test_ok_needs_a_loaded_world_and_has_a_short_cooldown(run):
    run_dir, rec = run
    row, rc = H.send("ok", rec, run_dir, at_title(rec), ON)
    assert rc == 2 and "[main player]" in row["refused"] and files(rec) == []
    ops = Ops(rec)
    log = rec / "evidence" / "HOST_CONSOLE_LOG.jsonl"
    log.write_text(json.dumps(dict(utc="2026-10-22T03:00:00+00:00", file="x", pid=HOST_PID, command="arti_host ok")) + "\n")
    ops.now = H._epoch("2026-10-22T03:00:00+00:00") + 1
    row, rc = H.send("ok", rec, run_dir, ops, ON)
    assert rc == 2 and "cooldown" in row["refused"]
    ops.now += H.COOLDOWN_S["ok"]
    assert H.send("ok", rec, run_dir, ops, ON)[1] == 0
    # `debug sleep` and `arti_host ok` cool down independently
    assert H.send("sleep", rec, run_dir, ops, ON)[1] == 0


def test_load_at_the_title_screen_writes_the_validated_line(run, saves):
    run_dir, rec = run
    row, rc = H.send("load", rec, run_dir, at_title(rec), ON, save=SAVE, saves_dir=saves)
    assert rc == 0 and row["delivered"] is True and row["command"] == f"arti_host load {SAVE}" and row["save"] == SAVE
    [f] = files(rec)
    assert f.endswith("-load.sent") and (rec / "host-console" / f).read_text(encoding="utf-8") == f"arti_host load {SAVE}"
    [r] = rows(run_dir)
    assert (r["source"], r["target"], r["host_hwnd"], r["pid"]) == ("host_driver", "host", HOST, HOST_PID)


@pytest.mark.parametrize("save", [None, "", "proof1", "../x_1", r"..\x_1", "a b_1", "a_1\n", "a_1;quit", "a_1 ok",
                                  "a_1\ndebug warp", "x" * 41 + "_1", "\u00e9t\u00e9_1"])
def test_load_refuses_any_save_name_outside_the_pattern(run, saves, save):
    run_dir, rec = run
    row, rc = H.send("load", rec, run_dir, at_title(rec), ON, save=save, saves_dir=saves)
    assert rc == 2 and "invalid save folder name" in row["refused"] and row["command"] is None
    assert files(rec) == []


def test_load_refusals_world_loaded_missing_save_flag_farmhand_cooldown(run, saves):
    run_dir, rec = run
    row, rc = H.send("load", rec, run_dir, Ops(rec), ON, save=SAVE, saves_dir=saves)             # world already loaded
    assert rc == 2 and "title-screen only" in row["refused"]
    row, rc = H.send("load", rec, run_dir, at_title(rec), ON, save="other_123", saves_dir=saves)  # no such save
    assert rc == 2 and "not found" in row["refused"]
    (saves / "empty_1").mkdir()
    row, rc = H.send("load", rec, run_dir, at_title(rec), ON, save="empty_1", saves_dir=saves)    # folder, no save file
    assert rc == 2 and "not found" in row["refused"]
    for env in ({}, {H.FLAG_ENV: "0"}):
        row, rc = H.send("load", rec, run_dir, at_title(rec), env, save=SAVE, saves_dir=saves)
        assert rc == 2 and H.FLAG_ENV in row["refused"]
    ops = at_title(rec)
    ops.windows[HOST]["title"] = TITLE + " [farmhand]"
    assert "farmhand" in H.send("load", rec, run_dir, ops, ON, save=SAVE, saves_dir=saves)[0]["refused"]
    ops = at_title(rec)
    (run_dir / "host-hwnd.txt").write_text(str(FH))
    ops.windows[FH]["title"] = TITLE
    assert "farmhand" in H.send("load", rec, run_dir, ops, ON, save=SAVE, saves_dir=saves)[0]["refused"]
    (run_dir / "host-hwnd.txt").write_text(str(HOST))
    assert files(rec) == []
    ops = at_title(rec)
    assert H.send("load", rec, run_dir, ops, ON, save=SAVE, saves_dir=saves)[1] == 0
    ops.now = H._epoch("2026-10-22T03:00:00+00:00") + 30                       # the fake supervisor's log stamp
    row, rc = H.send("load", rec, run_dir, ops, ON, save=SAVE, saves_dir=saves)
    assert rc == 2 and "cooldown" in row["refused"]
    assert all(r["op"] == "host_console" and r["source"] == "host_driver" for r in rows(run_dir))


def test_cli_load_ok_and_status_at_the_title(run, saves, capsys):
    run_dir, rec = run
    ops = at_title(rec)
    assert H.main(["status", "--run-dir", str(run_dir)], ops, {}) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["host"]["hwnd"] == HOST and st["host_at_title"] is True and st["actions"] == ["load", "ok", "sleep"]
    assert H.main(["load", SAVE, "--saves-dir", str(saves), "--run-dir", str(run_dir)], ops, ON) == 0
    assert json.loads(capsys.readouterr().out)["command"] == f"arti_host load {SAVE}"
    assert H.main(["load", "--saves-dir", str(saves), "--run-dir", str(run_dir)], ops, ON) == 2   # name missing
    assert "invalid save folder name" in json.loads(capsys.readouterr().out)["refused"]
    ops.windows[HOST]["title"] = HOST_TITLE
    ops.now += 10
    assert H.main(["ok", "--run-dir", str(run_dir)], ops, ON) == 0


def test_default_saves_dir_is_appdata():
    assert H.default_saves_dir({"APPDATA": "C:/U/AppData/Roaming"}) == Path("C:/U/AppData/Roaming/StardewValley/Saves")
