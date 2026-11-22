"""Review M4 E1 / ADR 256: every automatic close of a session game asks the game's own mod to exit when safe and waits
for its answer. No window message is ever sent; a game whose mod does not answer stays up and the caller reports it.
The mod double here speaks the real file contract (exit_request.json / exit_answer.json in the mod's folder)."""

import json

import pytest

from scripts.livepath import mabar_supervise as S

IDENT = dict(run_id="run12345", runtime_id="hs-A", save_id="100000001", player_id=-123456)


class Mod:
    """A mod double: on each poll it reads exit_request.json and writes the scripted answer for that request."""

    def __init__(self, folder, script, pid=202, controller="ctl-1"):
        self.folder, self.script, self.pid, self.alive, self.seen = folder, list(script), pid, True, []
        if controller:  # what this supervisor published in the HOST's folder (review M4d item 4: checked before a request)
            (folder / "controller.json").write_text(json.dumps(dict(v=1, run_id="run12345", session=controller)), encoding="utf-8")

    def poll(self, _seconds):
        req = json.loads((self.folder / S.EXIT_REQUEST_FILE).read_text(encoding="utf-8"))
        self.seen.append(req)
        step = self.script.pop(0) if self.script else None
        if step is None:
            return
        state, why = step if isinstance(step, tuple) else (step, None)
        answer = dict(v=1, run_id=req["run_id"], runtime_id=req["runtime_id"],
                      request_id=req["request_id"] if state != "foreign" else "someone-else",
                      state="exiting" if state == "foreign" else state, why=why)
        (self.folder / S.EXIT_ANSWER_FILE).write_text(json.dumps(answer), encoding="utf-8")
        if state == "exiting":
            self.alive = False  # the game quits itself at its safe tick


def ask(tmp_path, mod, *, clock=None, stop=lambda: False, horizon=20):
    t = clock or iter(range(1000, 2000))
    return S.ask_game_exit(tmp_path, "helper_host", mod.pid, session="ctl-1", alive_fn=lambda pid: mod.alive,
                           wait_exit_fn=lambda pid, s: not mod.alive, now_fn=lambda: float(next(t)), sleep_fn=mod.poll,
                           target_fn=lambda run, name, kind: (mod.folder, IDENT), stop_fn=stop, horizon_s=horizon)


def test_a_save_starting_after_the_barrier_is_waited_out_then_the_game_exits_itself(tmp_path):
    mod = Mod(tmp_path, [("waiting", "saving"), ("waiting", "menu:SaveGameMenu"), "exiting"])
    result = ask(tmp_path, mod)
    assert result["state"] == "exited" and not mod.alive
    req = mod.seen[0]
    assert req["v"] == 1 and req["session"] == "ctl-1" and req["save_id"] == "100000001" and req["player_id"] == -123456
    assert req["runtime_id"] == "hs-A" and type(req["requester_pid"]) is int and req["requester_image"].lower().startswith("python")
    assert req["target_pid"] == mod.pid  # review M4d item 4: the request names the process it is for
    assert req["deadline_utc"] > req["issued_utc"]
    assert not (tmp_path / S.EXIT_REQUEST_FILE).exists()  # withdrawn: it can never exit a game later


@pytest.mark.parametrize("answer", ["refused:not_owned", "refused:identity", "expired", "aborted"])
def test_a_refusing_mod_keeps_its_game_up_and_says_why(tmp_path, answer):
    mod = Mod(tmp_path, [(answer, "x")])
    with pytest.raises(S.GameStaysUp, match=answer.split(":")[0]):
        ask(tmp_path, mod)
    assert mod.alive and not (tmp_path / S.EXIT_REQUEST_FILE).exists()


def test_no_answer_by_the_deadline_keeps_the_game_up_never_a_forced_close(tmp_path):
    mod = Mod(tmp_path, [])
    with pytest.raises(S.GameStaysUp, match="no answer from its mod"):
        ask(tmp_path, mod, horizon=5)
    assert mod.alive and not (tmp_path / S.EXIT_REQUEST_FILE).exists()


def test_a_foreign_answer_is_not_ours(tmp_path):
    mod = Mod(tmp_path, ["foreign", "foreign"])
    with pytest.raises(S.GameStaysUp, match="no answer from its mod"):
        ask(tmp_path, mod, horizon=4)
    assert mod.alive


def test_a_stop_outranks_the_close(tmp_path):
    mod = Mod(tmp_path, [("waiting", "saving")] * 10)
    stops = iter([False, False, True])
    with pytest.raises(S.GameStaysUp, match="a stop outranks"):
        ask(tmp_path, mod, stop=lambda: next(stops))
    assert mod.alive and not (tmp_path / S.EXIT_REQUEST_FILE).exists()


def test_exiting_but_the_pid_stays_up_is_reported(tmp_path):
    mod = Mod(tmp_path, ["exiting"])
    with pytest.raises(S.GameStaysUp, match="still up"):
        S.ask_game_exit(tmp_path, "helper_host", 202, session="ctl-1", alive_fn=lambda pid: True,
                        wait_exit_fn=lambda pid, s: False, now_fn=iter(range(1000, 2000)).__next__, sleep_fn=mod.poll,
                        target_fn=lambda run, name, kind: (tmp_path, IDENT), stop_fn=lambda: False, horizon_s=20)


def test_unknown_identity_cannot_be_asked(tmp_path):
    with pytest.raises(S.GameStaysUp, match="identity unknown"):
        S.game_exit_target(tmp_path, "helper_host", "solo")
    with pytest.raises(S.GameStaysUp, match="identity unknown"):
        S.game_exit_target(tmp_path, "farmhand", "owner", inbox_fn=lambda: tmp_path)


def test_the_helper_identity_comes_from_its_config_and_barrier(tmp_path):
    out = tmp_path / "host"
    out.mkdir()
    cfg = tmp_path / S.HELPER_CONFIG
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps(dict(RunId="run12345", OutputDirectory=str(out), SaveId=100000001, HostId=-123456)), encoding="utf-8")
    (out / "barrier.json").write_text(json.dumps(dict(run_id="run12345", runtime_id="hs-A")), encoding="utf-8")
    folder, ident = S.game_exit_target(tmp_path, "helper_host", "solo")
    assert folder == out and ident == IDENT
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "session.json").write_text(json.dumps(dict(runtime_instance_id="rt-F", player_id=777)), encoding="utf-8")
    folder, ident = S.game_exit_target(tmp_path, "farmhand", "solo", inbox_fn=lambda: inbox)
    assert folder == inbox and ident == dict(run_id="run12345", runtime_id="rt-F", save_id="100000001", player_id=777)


def test_no_window_message_is_left_in_the_close_path():
    import inspect
    src = inspect.getsource(S.close_session_games) + inspect.getsource(S.ask_game_exit)
    assert "ui_op.py" not in src and "WM_CLOSE" not in src.replace("never sent a window message", "")


# ---------------------------------------------------------------- review M4b: every stop counts, the controller id

def test_any_stop_of_the_run_counts_the_cycles_own_included(tmp_path):
    run = tmp_path / "run"
    cycle = run / "runtime/live7/c2"
    cycle.mkdir(parents=True)
    (cycle / "cycle_state.json").write_text(json.dumps(dict(status="active")), encoding="utf-8")
    assert not S.run_stopped(run)
    for stop in (cycle / "LIVE7_STOP", run / "runtime" / "LIVE7_STOP", run / "LIVE7_STOP", run / "SUPERVISE_STOP"):
        stop.write_text("stop", encoding="utf-8")
        assert S.run_stopped(run), stop
        stop.unlink()
    for reason in ("effect_ladder_recover", "menu_held_recover"):  # review M4c rule 7: a drain record is not a stop
        (cycle / "cycle_state.json").write_text(json.dumps(dict(status="not_recovered", stop_reason=reason)), encoding="utf-8")
        assert not S.run_stopped(run), reason
    (bridge := run / "bridge-stop-request").write_text("stop", encoding="utf-8")
    assert S.run_stopped(run)  # an ordinary mabar stop writes this marker
    bridge.unlink()


def test_a_cycle_local_stop_withdraws_the_request_before_the_host_can_quit(tmp_path):
    run = tmp_path / "run"
    (run / "runtime/live7/c1").mkdir(parents=True)
    mod = Mod(tmp_path, [("waiting", "saving")] * 10)
    seen = []

    def poll(s):
        mod.poll(s)
        if len(mod.seen) == 2:
            (run / "runtime/live7/c1/LIVE7_STOP").write_text("stop", encoding="utf-8")  # the cycle's own stop

    t = iter(range(1000, 2000))
    with pytest.raises(S.GameStaysUp, match="a stop outranks"):
        S.ask_game_exit(run, "helper_host", 202, session="ctl-1", alive_fn=lambda pid: True, wait_exit_fn=lambda p, s: False,
                        now_fn=lambda: float(next(t)), sleep_fn=poll, target_fn=lambda r, n, k: (tmp_path, IDENT),
                        horizon_s=50)
    assert not (tmp_path / S.EXIT_REQUEST_FILE).exists()  # withdrawn: the HOST's last re-read finds none, so no quit


def test_a_stop_during_the_pid_wait_stops_what_comes_next(tmp_path):
    mod = Mod(tmp_path, ["exiting"])
    stops = iter([False, False, False, True])
    with pytest.raises(S.GameStaysUp, match="a stop outranks"):
        ask(tmp_path, mod, stop=lambda: next(stops))


def test_the_controller_session_is_written_once_per_supervisor_and_a_new_one_replaces_it(tmp_path):
    import types
    out = tmp_path / "host"
    out.mkdir()
    cfg = tmp_path / S.HELPER_CONFIG
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps(dict(RunId="run12345", OutputDirectory=str(out))), encoding="utf-8")
    (tmp_path / "SUPERVISE_STOP").write_text("stop", encoding="utf-8")
    ids = []
    for _ in range(2):
        S.run_supervise(types.SimpleNamespace(run=str(tmp_path)), sleep_fn=lambda s: None, clock_fn=lambda: 1010.0,
                        bridge_fn=lambda: [], games_fn=lambda: "1", rejoin_fn=lambda: None, host_relaunch_fn=lambda: None)
        row = json.loads((out / "controller.json").read_text(encoding="utf-8"))
        assert row["run_id"] == "run12345" and row["session"]
        ids.append(row["session"])
    assert ids[0] != ids[1]  # a handoff: requests naming the old id are refused by the HOST
    assert not (out / "controller_lease.json").exists()  # no fence, no beat


def test_rung_six_from_a_real_drain_record_is_not_vetoed_by_its_own_record(tmp_path):
    """Review M4c rule 7: Live7 records a drain as not_recovered with stop_reason effect_ladder_recover and no stop file;
    rung six is its recovery. Through the real waiter (its default stop check), a responding mod exits."""
    run = tmp_path / "run"
    cycle = run / "runtime/live7/c1"
    cycle.mkdir(parents=True)
    (cycle / "cycle_state.json").write_text(json.dumps(dict(status="not_recovered", stop_reason="effect_ladder_recover")),
                                            encoding="utf-8")
    mod = Mod(tmp_path, [("waiting", "holding_safe"), "exiting"])
    t = iter(range(1000, 2000))
    result = S.ask_game_exit(run, "helper_host", mod.pid, session="ctl-1", alive_fn=lambda pid: mod.alive,
                             wait_exit_fn=lambda pid, s: not mod.alive, now_fn=lambda: float(next(t)), sleep_fn=mod.poll,
                             target_fn=lambda r, n, k: (tmp_path, IDENT), horizon_s=20)
    assert result["state"] == "exited" and not mod.alive


# ---------------------------------------------------------------- review M4d: exit request binding, main's close refusals

@pytest.mark.parametrize("controller", [None, "ctl-OLD"])
def test_no_request_reaches_a_host_whose_folder_lacks_this_supervisors_controller_json(tmp_path, controller):
    """Review M4d item 4: after a HOST relaunch (a new folder) the supervisor sends nothing until its controller.json is
    there and names this run and session."""
    mod = Mod(tmp_path, ["exiting"], controller=controller)
    with pytest.raises(S.GameStaysUp, match="controller.json is not published"):
        ask(tmp_path, mod)
    assert mod.alive and mod.seen == [] and not (tmp_path / S.EXIT_REQUEST_FILE).exists()


def test_no_recorded_launch_pid_no_request(tmp_path):
    mod = Mod(tmp_path, ["exiting"])
    mod.pid = None
    with pytest.raises(S.GameStaysUp, match="no recorded launch pid"):
        ask(tmp_path, mod)
    assert not (tmp_path / S.EXIT_REQUEST_FILE).exists()


def _records(run, fh_hwnd="101", host_hwnd="102"):
    (run / "runtime/batch-host").mkdir(parents=True, exist_ok=True)
    (run / "runtime/farmhand-launch.json").write_text(json.dumps(dict(pid=201)), encoding="utf-8")
    (run / "runtime/batch-host/host-launch.json").write_text(json.dumps(dict(pid=202)), encoding="utf-8")
    for name, hwnd in (("fh-hwnd.txt", fh_hwnd), ("host-hwnd.txt", host_hwnd)):
        if hwnd is not None:
            (run / "runtime" / name).write_text(hwnd, encoding="utf-8")


WINDOWS = {101: dict(exists=True, pid=201), 102: dict(exists=True, pid=202)}


def _close(run, windows, asked):
    return S.close_session_games(run, "solo", alive_fn=lambda pid: True, wait_exit_fn=lambda pid, t: True,
                                 keep_fn=lambda run, label: None, window_fn=lambda h: windows.get(h),
                                 ask_fn=lambda run, name, pid, **k: asked.append((name, pid)))


def test_both_recorded_games_with_their_own_windows_are_asked_by_pid(tmp_path):
    _records(tmp_path)
    asked = []
    assert _close(tmp_path, WINDOWS, asked) == ["farmhand", "helper_host"]
    assert asked == [("farmhand", 201), ("helper_host", 202)]


@pytest.mark.parametrize("case, windows, records, match", [
    ("window not recorded", WINDOWS, dict(host_hwnd=None), "window is not recorded"),
    ("window gone", {101: dict(exists=True, pid=201), 102: dict(exists=False, pid=None)}, {}, "exists=False"),
    ("window of another process", {101: dict(exists=True, pid=201), 102: dict(exists=True, pid=999)}, {}, "pid=999"),
    ("no window facts", {101: dict(exists=True, pid=201)}, {}, "exists=False"),
])
def test_every_refusal_of_mains_close_is_kept_before_any_request(tmp_path, case, windows, records, match):
    """Review M4d item 2: origin/main refused a close whose window was not recorded, did not exist or belonged to another
    pid (ui_op close --expect-pid). The exit path keeps each refusal, and asks nothing."""
    _records(tmp_path, **records)
    asked = []
    with pytest.raises(S.GameStaysUp, match=match):
        _close(tmp_path, windows, asked)
    assert ("helper_host", 202) not in asked, case


def test_the_farmhand_and_host_records_naming_one_pid_are_refused(tmp_path):
    _records(tmp_path)
    (tmp_path / "runtime/batch-host/host-launch.json").write_text(json.dumps(dict(pid=201)), encoding="utf-8")
    with pytest.raises(RuntimeError, match="name the same pid"):
        _close(tmp_path, WINDOWS, [])
