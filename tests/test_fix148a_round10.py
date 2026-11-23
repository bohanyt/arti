"""A closed title screen does not block her next launch.

The failure: her game is stopped by its own watchdog; a replacement game latches a HALT (`native_unclean_exit`), never
joins, stays at the title screen and exits cleanly. Its last session.json is a title-screen publication (no world,
player 0, no name, `is_main_player` true because at the title screen every game is its own master), so the launch gate
holds `foreign_session` and no session can start her game.

Here a launch accepts such a session only when the mod's own exit record proves that very runtime exited; the latch
then goes through the unchanged launch rows (`launch_relatches`: her game starts with the lock still on, for a person
to clear). Every other act, and every other session shape, decides exactly as before. The fixtures are synthetic and
their values are invented; the mod's token consumption is a fake native.

Second hold: a run fresh from `mabar prepare` has no launch record and no cycle yet, so without this rule every first
launch of a new run held `latest_launch_session_unconfirmed` whenever her inbox still held an older game's
session.json. That row applies only to a run that has launched her game or has already played
(`_run_has_launched`); a fresh run's launch skips only that latest-launch publication check.

Both admissions hold ONLY in a solo run: `session_kind(run) == "solo"`, the program's helper hosts. In an owner-hosted
run her title-screen join can send input to the owner's foreground game, so there, and with no run folder, both rows
decide as without these admissions (cases f to j)."""
import argparse
import copy
import hashlib
import itertools
import json
import time

import pytest

from scripts.livepath import halt_authority as HA, mabar, mabar_supervise
from tools import arti_rearm

RUNTIME = "11111111111111111111111111111111"  # the replacement game that stayed at the title screen
PRIOR = "22222222222222222222222222222222"  # the game before it (the one the watchdog stopped)
NEXT = "33333333333333333333333333333333"  # her next game, after the launch
SEQ = 7
EPISODE = "native:%s:%d" % (RUNTIME, SEQ)
OLD_EPISODE = "native:%s:%d" % (PRIOR, SEQ - 1)
LATCHED_MS = 1_700_000_000_000
NATIVE_MS = 1_700_007_000_000
EXIT = dict(runtime_id=RUNTIME, ms=NATIVE_MS + 100)
ENTRIES = [dict(n=1, source="native", stop_id=EPISODE, stop_sha256="0" * 64, cause="native_unclean_exit",
                scope="all", writer="native", latched_ms=LATCHED_MS, file_present=False)]


def digest(entries):
    native = [{k: v for k, v in e.items() if k != "file_present"} for e in entries]
    return hashlib.sha256(json.dumps(native, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


DIGEST = digest(ENTRIES)
KEY = (EPISODE, SEQ, DIGEST)
HALT_FILE = {"v": "arti.halt/1", "state": "latched", "episode_id": EPISODE, "halt_seq": SEQ, "scope": "all",
             "since_ms": LATCHED_MS, "entries": ENTRIES, "entries_digest": DIGEST,
             "last_rearmed": dict(episode_id=OLD_EPISODE, halt_seq=SEQ - 1, ms=1_699_900_000_000,
                                  stop_ids=[OLD_EPISODE])}


def title_session(runtime=RUNTIME, *, latched=True, **extra):
    """The mod's title-screen publication: no world, player 0, no name, its own master, the HALT it restored."""
    halt = dict(episode_id=EPISODE, halt_seq=SEQ, entries_digest=DIGEST, scope="all", since_ms=LATCHED_MS,
                persisted=True, entries=copy.deepcopy(ENTRIES)) if latched else None
    session = {
        "mod_tick": 1000, "native_ms": NATIVE_MS, "acquired_ms": None, "input_idle": {"core": None},
        "halt": halt, "rearmed": dict(episode_id=OLD_EPISODE, halt_seq=SEQ - 1, ms=1_699_900_000_000,
                                      stop_ids=[OLD_EPISODE]),
        "halt_notes": ["halt_record_missing"] if latched else [], "fenced_generation": 0, "protocol": 1,
        "ev": "command_session", "session_id": "44444444444444444444444444444444", "player_id": 0,
        "player_name": "", "location_id": "", "screen_id": 0, "is_main_player": True, "world_ready": False,
        "inflight_request_id": None, "runtime_instance_id": runtime, "owner_id": None, "generation": None,
        "control_seq": None, "lease_until_unix_ms": None, "stop_latched": latched, "record_faulted": False,
        "release_requested": False, "release_acked": True,
    }
    session.update(extra)
    return session


def host_by(run, controller, helper):
    """What the HOST step leaves in the run: `mabar host --helper` writes stardew_host_controller=harness and installs
    the helper's config (solo); plain `mabar host` writes controller=owner and removes it (the owner hosts)."""
    path = run / "trial-config.json"
    trial = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    trial.pop("stardew_host_controller", None)
    if controller is not None:
        trial["stardew_host_controller"] = controller
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trial), encoding="utf-8")
    config = run / HA.HELPER_CONFIG
    if helper:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(dict(SaveId=100000001, FarmhandName="Arti")), encoding="utf-8")
    else:
        config.unlink(missing_ok=True)


def solo(run):
    host_by(run, "harness", True)


def owner_hosts(run):
    host_by(run, "owner", False)
    (run / "runtime").mkdir(parents=True, exist_ok=True)
    (run / "runtime/host-hwnd.txt").write_text("123456", encoding="utf-8")  # the owner's foreground HOST window


def put(inbox, session, *, halt=None, exit_record=None):
    for name, value in (("session.json", session), ("halt.json", halt), ("mod_exit.json", exit_record)):
        path = inbox / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


@pytest.fixture
def title(tmp_path, monkeypatch):
    """Her inbox with a closed title screen: a HALT latched and persisted, the matching exit. The run
    is the solo run whose own recovery launched that replacement game (its launch record binds the publication)."""
    inbox, run = tmp_path / "inbox", tmp_path / "run"
    inbox.mkdir()
    (run / "runtime").mkdir(parents=True)
    (run / "runtime/farmhand-launch.json").write_text(json.dumps(dict(
        pid=40004, launch_id="launch-replacement", issued_ms=NATIVE_MS - 6_000_000, previous_runtime=PRIOR)))
    solo(run)
    put(inbox, title_session(), halt=HALT_FILE, exit_record=EXIT)
    monkeypatch.setattr(mabar, "inbox", lambda: inbox)
    return argparse.Namespace(inbox=inbox, run=run)


def snapshot(inbox):
    return {p.name: p.read_bytes() for p in sorted(inbox.iterdir())}


def count_session_kind(monkeypatch):
    """Every read of who hosts the run, as `decide` makes it (through `_solo_run`)."""
    reads = []
    real = HA.session_kind

    def counted(run):
        reads.append(run)
        return real(run)
    monkeypatch.setattr(HA, "session_kind", counted)
    return reads


def no_title_predicate(monkeypatch):
    """Main's path: the closed-title-screen predicate never accepts (and records whether anyone asked it)."""
    asked = []
    monkeypatch.setattr(HA, "_closed_title_screen", lambda *a: asked.append(a) and False)
    return asked


# ---------------------------------------------------------------- 1. a closed title screen relaunches with the lock on

def test_closed_title_screen_with_a_halt_launches_with_the_latch_kept(title, monkeypatch):
    before = snapshot(title.inbox)
    for decision in (HA.check(title.inbox, title.run, act="launch"),
                     HA.check(title.inbox, title.run, act="launch", recovery=True, allow_watchdog=True),
                     HA.check(title.inbox, title.run, act="launch", recovery=True)):
        assert (decision.action, decision.reason) == ("nothing", "launch_relatches:native_unclean_exit")
        assert decision.halt["key"] == KEY and decision.watchdog is None and decision.expect is None
        assert decision.held == dict(episode_id=EPISODE, halt_seq=SEQ, entries_digest=DIGEST,
                                     session_id=title_session()["session_id"], runtime_instance_id=RUNTIME)
    decision = mabar.rearm_halt(title.run, None, "launch", allow_watchdog=True)  # `mabar rejoin` with the HOST up
    assert decision.reason == "launch_relatches:native_unclean_exit" and decision.held["halt_seq"] == SEQ
    assert snapshot(title.inbox) == before and not (title.inbox / "rearm.json").exists()  # no token, nothing moved
    assert HA.check(title.inbox, act="launch").reason == "foreign_session"  # no run folder: not solo, main's hold
    no_title_predicate(monkeypatch)
    assert HA.check(title.inbox, title.run, act="launch", recovery=True).reason == "foreign_session"  # main's hold


# ---------------------------------------------------------------- 2. her next game holds; the person path clears it

def native_consumes(inbox):
    """The mod's tick: a token that names the latched key clears it, with its receipt in session.json and halt.json."""
    def tick(_seconds=0.0):
        path = inbox / "rearm.json"
        session = json.loads((inbox / "session.json").read_text(encoding="utf-8"))
        if not path.exists() or session["halt"] is None:
            return
        raw = path.read_bytes()
        token = json.loads(raw)
        if tuple(token[k] for k in HA.KEY_FIELDS) != tuple(session["halt"][k] for k in HA.KEY_FIELDS):
            return
        session.update(stop_latched=False, halt=None, halt_notes=[],
                       rearmed=dict(episode_id=EPISODE, halt_seq=SEQ, ms=NATIVE_MS + 90_000, stop_ids=[EPISODE]))
        record = dict(HALT_FILE, state="rearmed",
                      rearmed=dict(halt_seq=SEQ, token_sha256=hashlib.sha256(raw).hexdigest(), ms=NATIVE_MS + 90_000))
        put(inbox, session, halt=record, exit_record=EXIT)
        path.unlink()
    return tick


def test_her_next_game_holds_the_latch_and_the_owners_rearm_clears_it(title, monkeypatch):
    launch = HA.check(title.inbox, title.run, act="launch", recovery=True)
    assert launch.reason == "launch_relatches:native_unclean_exit"
    put(title.inbox, title_session(NEXT, native_ms=NATIVE_MS + 60_000), halt=HALT_FILE, exit_record=EXIT)
    startup = dict(runtime_instance_id=NEXT, pid=40000, launch_id="launch-next")
    held = HA.decide(HA.read_evidence(title.inbox, startup=startup), act="join")
    assert (held.action, held.reason) == ("hold", "halt_latched:native_unclean_exit")
    assert held.held == dict(episode_id=EPISODE, halt_seq=SEQ, entries_digest=DIGEST,
                             session_id=title_session()["session_id"], runtime_instance_id=NEXT)
    with pytest.raises(HA.HandsHeld):
        mabar.report_hold(title.run, held)
    row = [json.loads(line) for line in (title.run / "mabar.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    assert row["event"] == "hands_held" and row["clears"] == "operator_rearm"
    assert row["command"][-4:] == ["--folder", str(title.inbox), "--actor", "owner"]
    assert row["command"][2].endswith("arti_rearm.py")

    lines = []
    outcome = arti_rearm.rearm(title.inbox, "owner",
                               lambda halt, notes: tuple(halt[k] for k in HA.KEY_FIELDS) == KEY,
                               out=lines.append, sleep=native_consumes(title.inbox))
    assert outcome == "rearmed" and lines[-1] == "re-armed rearm:%s:%d" % (EPISODE, SEQ)
    ev = HA.read_evidence(title.inbox, startup=startup)
    # The recovery worker keeps the launch's own binding (the title-screen runtime) and ends it on the restored receipt.
    assert HA.decide(ev, act="join", held=launch.held).reason == "operator_rearm_not_seen:" + EPISODE
    assert HA.restored_receipt(ev, launch.halt["key"]) and HA.decide(ev, act="join").reason == "clear"
    clear = HA.decide(ev, act="join", held=held.held)
    assert (clear.action, clear.reason) == ("nothing", "clear") and not (title.inbox / "rearm.json").exists()


# ---------------------------------------------------------------- 3. no proof that this very runtime exited: held

@pytest.mark.parametrize("exit_record", [
    None,  # no mod_exit.json: her title-screen game may still be running
    dict(EXIT, runtime_id=PRIOR),  # the exit of another runtime
    dict(EXIT, ms=NATIVE_MS - 1),  # an exit older than the last publication
    dict(EXIT, extra=1),  # not the mod's exact record
    "{not json",
], ids=["absent", "other_runtime", "older_than_publication", "extra_field", "unreadable"])
def test_a_title_screen_without_its_own_clean_exit_is_still_foreign(title, exit_record):
    put(title.inbox, title_session(), halt=HALT_FILE, exit_record=exit_record)
    for decision in (HA.check(title.inbox, act="launch"), HA.check(title.inbox, title.run, act="launch"),
                     HA.check(title.inbox, title.run, act="launch", recovery=True, allow_watchdog=True)):
        assert (decision.action, decision.reason) == ("hold", "foreign_session")
    assert not (title.inbox / "rearm.json").exists()


# ---------------------------------------------------------------- 4. an in-world master (the HOST's game) stays foreign

@pytest.mark.parametrize("shape", [
    dict(world_ready=True),  # the HOST's or the owner's own game in its world, even with player 0
    dict(world_ready=True, player_id=4242424242424242424, player_name="Host", location_id="Farm"),
    dict(player_id=4242424242424242425, player_name="Arti"),  # a farmer at the title screen is not a closed title
    dict(player_name="Arti"),
    dict(is_main_player=None),
], ids=["in_world_player0", "in_world_master", "title_with_player", "title_with_name", "untyped_master_flag"])
def test_any_other_master_session_holds_foreign_with_its_matching_exit(title, shape):
    put(title.inbox, title_session(**shape), halt=HALT_FILE, exit_record=EXIT)
    for decision in (HA.check(title.inbox, act="launch"), HA.check(title.inbox, title.run, act="launch"),
                     HA.check(title.inbox, title.run, act="launch", recovery=True, allow_watchdog=True)):
        assert (decision.action, decision.reason) == ("hold", "foreign_session")


# ---------------------------------------------------------------- 5. a closed title screen with no lock launches

def test_closed_title_screen_without_a_halt_launches_clear(title):
    put(title.inbox, title_session(latched=False), exit_record=EXIT)
    for decision in (HA.check(title.inbox, title.run, act="launch"),
                     HA.check(title.inbox, title.run, act="launch", recovery=True)):
        assert (decision.action, decision.reason, decision.halt) == ("nothing", "clear", None)
    assert HA.check(title.inbox, act="launch").reason == "foreign_session"  # no run folder: not solo, as main
    put(title.inbox, title_session(latched=False), halt=HALT_FILE, exit_record=EXIT)  # the game's own file disagrees
    assert HA.check(title.inbox, title.run, act="launch").reason == "persisted_halt_disagrees"


# ---------------------------------------------------------------- 6. every other act decides exactly as on main

@pytest.mark.parametrize("act", ["join", "start", "close", "observe"])
@pytest.mark.parametrize("latched", [True, False], ids=["latched", "clear"])
@pytest.mark.parametrize("startup", [None, dict(runtime_instance_id=RUNTIME)], ids=["no_startup", "startup"])
def test_other_acts_with_a_closed_title_screen_equal_main(title, monkeypatch, act, latched, startup):
    put(title.inbox, title_session(latched=latched), halt=HALT_FILE if latched else None, exit_record=EXIT)
    for run in (None, title.run):
        with monkeypatch.context() as patch:
            kinds = count_session_kind(patch)
            now = HA.decide(HA.read_evidence(title.inbox, run, startup=startup), act=act)
        with monkeypatch.context() as patch:
            asked = no_title_predicate(patch)
            main = HA.decide(HA.read_evidence(title.inbox, run, startup=startup), act=act)
        assert now == main and asked == [] and kinds == []  # who hosts is read for a launch only


# ================================================================ the first launch of a run fresh from `prepare`

FARMHAND = 7_314_159_265_358_979_323  # her farmer, in the HOST's world


def farmhand_session(runtime=RUNTIME, *, latched=False, **extra):
    """Her farmhand's in-world publication: a farmer in the HOST's world, not its master."""
    shape = dict(world_ready=True, is_main_player=False, player_id=FARMHAND, player_name="Arti",
                 location_id="FarmHouse")
    shape.update(extra)
    return title_session(runtime, latched=latched, **shape)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A run folder as `mabar prepare` leaves it (`witness_run.py prepare-run` never copies a launch or exit record):
    no `runtime/farmhand-launch.json`, no `runtime/live7` cycle; then `mabar host --helper` (as `mabar solo` runs it):
    the helper hosts. Her inbox still holds the closed title screen."""
    inbox, run = tmp_path / "inbox", tmp_path / "mabar-fresh"
    inbox.mkdir()
    for folder in ("runtime/batch-host/evidence", "runtime/batch-host/host-console", "template"):
        (run / folder).mkdir(parents=True)
    for name, value in (("backup.json", {}), ("trial-config.json", {}), ("self_facts.json", dict(name="Arti")),
                        ("MODE.json", dict(mode="mabar", gate_scored=False, owner_present=True))):
        (run / name).write_text(json.dumps(value), encoding="utf-8")
    solo(run)
    put(inbox, title_session(), halt=HALT_FILE, exit_record=EXIT)
    monkeypatch.setattr(mabar, "inbox", lambda: inbox)
    monkeypatch.setattr(mabar, "creation_facts", lambda p: ({}, (), {}))
    monkeypatch.setattr(mabar, "keep_smapi_logs", lambda run, label: None)
    monkeypatch.setattr(mabar, "release_farmhand_focus", lambda run: True)
    monkeypatch.setattr(mabar.subprocess, "run", lambda *a, **k: pytest.fail("no operator tool subprocess"))
    return argparse.Namespace(inbox=inbox, run=run)


def main_launch_rule(monkeypatch):
    """Main's row: any run folder (or any launch record) needed the latest launch's own publication."""
    asked = []

    def rule(ev):
        asked.append(ev)
        return ev.run is not None or ev.launch[0] is not None
    monkeypatch.setattr(HA, "_run_has_launched", rule)
    return asked


def rows(run, event):
    path = run / "mabar.jsonl"
    found = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []
    return [r for r in found if r["event"] == event]


def her_recovery(fresh, gates):
    """`mabar.recover_farmhand` as the real worker runs it (mabar._recover_farmhand -> restart_farmhand ->
    prep_trial supervise), with only the game process faked: the launcher persists its pending launch record and asks
    `child_recovery_gate` with the record read before it (as prep_trial.cmd_supervise does), her new game then
    restores the latch at its title screen, and the worker's join gate decides on it."""
    def recover(run, slot, *, held=None, expected=None, deadline=None, **_):
        assert slot == "name:Arti" and not HA.cycle_dirs(run)
        path = run / "runtime/farmhand-launch.json"
        predecessor = HA.read_launch_record(path)
        previous = json.loads((fresh.inbox / "session.json").read_text(encoding="utf-8"))["runtime_instance_id"]
        launch = dict(pid=None, launch_id="launch-fresh", exe="StardewModdingAPI.exe", stderr="farmhand-stderr-1.raw",
                      previous_runtime=previous, issued_ms=NATIVE_MS + 50_000, utc="2026-01-01T00:00:00Z")
        path.write_text(json.dumps(launch), encoding="utf-8")
        config = dict(run=str(run), inbox=str(fresh.inbox), act="launch", origin_cycle=None,
                      deadline=time.time() + 60, expected=expected, startup=None, held=held, stop_file=None,
                      cabin=False, host_hwnd=None, save_id=None, allow_watchdog=True)
        gate = HA.child_recovery_gate(config, saved_launch=("launch-fresh", predecessor))
        gates.append(gate)
        path.write_text(json.dumps(dict(launch, pid=40000)), encoding="utf-8")
        put(fresh.inbox, title_session(NEXT, native_ms=NATIVE_MS + 60_000), halt=HALT_FILE, exit_record=EXIT)
        startup = dict(runtime_instance_id=NEXT, pid=40000, launch_id="launch-fresh")
        joined = HA.decide(HA.read_evidence(fresh.inbox, run, recovery=True, expected=expected, startup=startup),
                           act="join", held=gate.held)
        if joined.action == "hold":
            raise HA.HandsHeld(joined)
        return {}
    return recover


def rejoin(fresh):
    ticks = itertools.count(0, 3)
    return mabar.rejoin_farmhand(argparse.Namespace(run=fresh.run, name=None), count_fn=lambda: "1",
                                 sleep_fn=lambda s: None, clock_fn=lambda: next(ticks), halt_fn=lambda: True)


# ---------------------------------------------------------------- a. a closed title screen, `mabar solo` -> rejoin_farmhand

def test_a_fresh_run_launches_a_closed_title_screen_with_the_latch_kept(fresh, monkeypatch):
    assert not (fresh.run / "runtime/farmhand-launch.json").exists() and HA.cycle_dirs(fresh.run) == []
    gates = []
    monkeypatch.setattr(mabar, "recover_farmhand", her_recovery(fresh, gates))
    with pytest.raises(HA.HandsHeld, match="halt_latched:native_unclean_exit"):
        rejoin(fresh)  # her new game is up with the lock on; the person path follows (test 2)
    [launch] = rows(fresh.run, "rearm_decision")
    assert (launch["act"], launch["action"], launch["reason"]) == ("launch", "nothing",
                                                                   "launch_relatches:native_unclean_exit")
    [gate] = gates  # the launcher's own read, with its pending record in place, agrees
    assert (gate.action, gate.reason) == ("nothing", "launch_relatches:native_unclean_exit")
    assert gate.halt["key"] == KEY and gate.halt["key"][1] == SEQ and gate.watchdog is None
    [held] = rows(fresh.run, "hands_held")
    assert held["reason"] == "halt_latched:native_unclean_exit" and held["held"]["halt_seq"] == SEQ
    assert held["command"][-4:] == ["--folder", str(fresh.inbox), "--actor", "owner"]
    assert not (fresh.inbox / "rearm.json").exists()


def test_a_fresh_run_held_on_mains_rule(fresh, monkeypatch):
    for decision in (HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=True),
                     HA.check(fresh.inbox, fresh.run, act="launch", recovery=True)):
        assert (decision.action, decision.reason, decision.halt["key"]) == (
            "nothing", "launch_relatches:native_unclean_exit", KEY)
    main_launch_rule(monkeypatch)
    monkeypatch.setattr(mabar, "recover_farmhand", lambda *a, **k: pytest.fail("main never launches"))
    with pytest.raises(HA.HandsHeld, match="latest_launch_session_unconfirmed"):
        rejoin(fresh)
    assert not (fresh.run / "runtime/farmhand-launch.json").exists() and not (fresh.inbox / "rearm.json").exists()


# ---------------------------------------------------------------- b. a clean farmhand exit, no halt: clear

@pytest.mark.parametrize("latched", [False, True], ids=["clear", "latched"])
def test_b_fresh_run_after_a_clean_farmhand_exit(fresh, monkeypatch, latched):
    put(fresh.inbox, farmhand_session(latched=latched), halt=HALT_FILE if latched else None, exit_record=EXIT)
    want = "launch_relatches:native_unclean_exit" if latched else "clear"
    for watchdog in (True, False):
        decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=watchdog)
        assert (decision.action, decision.reason) == ("nothing", want)
    assert mabar.rearm_halt(fresh.run, None, "launch", allow_watchdog=True).reason == want
    main_launch_rule(monkeypatch)
    assert HA.check(fresh.inbox, fresh.run, act="launch", recovery=True).reason == "latest_launch_session_unconfirmed"


# ---------------------------------------------------------------- c. a run that launched: the row still holds

@pytest.mark.parametrize("record", [
    dict(pid=777, launch_id="launch-later", issued_ms=NATIVE_MS + 10_000, previous_runtime=PRIOR),  # crashed unseen
    dict(pid=777, launch_id="launch-next", issued_ms=NATIVE_MS - 10_000, previous_runtime=RUNTIME),  # predecessor's
    dict(pid=777, launch_id="launch-other", issued_ms=NATIVE_MS - 10_000, previous_runtime=PRIOR,
         runtime_instance_id=NEXT),  # bound to another runtime
    dict(pid=None, launch_id="launch-pending", issued_ms=NATIVE_MS + 10_000, previous_runtime=RUNTIME),  # pending
    "{not json",
], ids=["issued_after_publication", "names_it_previous", "other_runtime", "pending", "unreadable"])
@pytest.mark.parametrize("shape", ["title", "farmhand"])
def test_c_a_launch_record_the_session_does_not_match_still_holds(fresh, record, shape):
    session = title_session() if shape == "title" else farmhand_session(latched=True)
    put(fresh.inbox, session, halt=HALT_FILE, exit_record=EXIT)
    path = fresh.run / "runtime/farmhand-launch.json"
    path.write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")
    for decision in (HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=True),
                     HA.check(fresh.inbox, fresh.run, act="launch")):
        assert (decision.action, decision.reason) == ("hold", "latest_launch_session_unconfirmed")


def test_c_a_not_started_record_is_no_launch(fresh):
    """prep_trial.cmd_supervise writes `not_started` when its own gate refused before any process started."""
    (fresh.run / "runtime/farmhand-launch.json").write_text(json.dumps(dict(
        pid=None, launch_id="launch-refused", issued_ms=NATIVE_MS + 10_000, previous_runtime=RUNTIME,
        not_started=True)), encoding="utf-8")
    decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=True)
    assert (decision.action, decision.reason) == ("nothing", "launch_relatches:native_unclean_exit")


# ---------------------------------------------------------------- d. a run that has played: the row still holds

@pytest.mark.parametrize("shape", ["title", "farmhand"])
def test_d_a_cycle_folder_without_a_launch_record_still_holds(fresh, shape):
    put(fresh.inbox, title_session() if shape == "title" else farmhand_session(),
        halt=HALT_FILE if shape == "title" else None, exit_record=EXIT)
    before = HA.read_evidence(fresh.inbox, fresh.run, recovery=True)  # read while the run had no cycle
    cycle = fresh.run / "runtime/live7/c1"
    cycle.mkdir(parents=True)
    (cycle / "cycle_state.json").write_text(json.dumps(dict(status="active")), encoding="utf-8")
    for decision in (HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=True),
                     HA.check(fresh.inbox, fresh.run, act="launch")):
        assert (decision.action, decision.reason) == ("hold", "latest_launch_session_unconfirmed")
    assert before.newest is None  # a cycle made after the read is still a run that has played
    assert HA.decide(before, act="launch").reason == "latest_launch_session_unconfirmed"


# ---------------------------------------------------------------- e. a fresh run without a clean exit still holds

@pytest.mark.parametrize("exit_record", [None, dict(EXIT, runtime_id=PRIOR), dict(EXIT, ms=NATIVE_MS - 1)],
                         ids=["absent", "other_runtime", "older_than_publication"])
def test_e_fresh_run_without_her_clean_exit_holds_as_before(fresh, exit_record):
    put(fresh.inbox, farmhand_session(), exit_record=exit_record)  # her game may still be running
    for watchdog in (True, False):
        decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=watchdog)
        assert (decision.action, decision.reason, decision.watchdog) == (
            "hold", "native_unclean_exit_before_launch", None)
    put(fresh.inbox, farmhand_session(latched=True), halt=HALT_FILE, exit_record=exit_record)
    decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=True)
    assert (decision.action, decision.reason) == ("hold", "lock_would_be_lost:native_unclean_exit")
    put(fresh.inbox, title_session(), halt=HALT_FILE, exit_record=exit_record)
    assert HA.check(fresh.inbox, fresh.run, act="launch", recovery=True).reason == "foreign_session"
    put(fresh.inbox, farmhand_session(), exit_record="{not json")
    assert HA.check(fresh.inbox, fresh.run, act="launch", recovery=True).reason == "exit_record_unreadable"
    assert not (fresh.inbox / "rearm.json").exists()


# ---------------------------------------------------------------- the fresh run decides as the no-run path; other acts

FRESH_SHAPES = {
    "title_latched_exit": lambda: (title_session(), HALT_FILE, EXIT),
    "title_clear_exit": lambda: (title_session(latched=False), None, EXIT),
    "title_latched_no_exit": lambda: (title_session(), HALT_FILE, None),
    "farmhand_clear_exit": lambda: (farmhand_session(), None, EXIT),
    "farmhand_latched_exit": lambda: (farmhand_session(latched=True), HALT_FILE, EXIT),
    "farmhand_no_exit": lambda: (farmhand_session(), None, None),
    "farmhand_latched_no_exit": lambda: (farmhand_session(latched=True), HALT_FILE, None),
    "session_only_latch": lambda: (farmhand_session(latched=True), None, EXIT),
    "disk_disagrees": lambda: (farmhand_session(), HALT_FILE, EXIT),
    "host_in_world": lambda: (farmhand_session(latched=False, is_main_player=True, player_id=FARMHAND + 1,
                                               player_name="Host", location_id="Farm"), None, EXIT),
}


TITLE_ADMITTED = {"title_latched_exit": "launch_relatches:native_unclean_exit", "title_clear_exit": "clear"}


@pytest.mark.parametrize("shape", sorted(FRESH_SHAPES))
def test_a_fresh_solo_runs_launch_equals_the_no_run_launch_but_a_closed_title_screen(fresh, shape):
    session, halt, exit_record = FRESH_SHAPES[shape]()
    put(fresh.inbox, session, halt=halt, exit_record=exit_record)
    no_run = HA.check(fresh.inbox, act="launch")
    for watchdog in (True, False):
        ran = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=watchdog)
        if shape in TITLE_ADMITTED:  # only a solo run admits a closed title screen; with no run folder it is main's hold
            assert (no_run.action, no_run.reason) == ("hold", "foreign_session")
            assert (ran.action, ran.reason) == ("nothing", TITLE_ADMITTED[shape])
        else:
            assert (ran.action, ran.reason, ran.halt, ran.held, ran.watchdog) == (
                no_run.action, no_run.reason, no_run.halt, no_run.held, no_run.watchdog)
    assert not (fresh.inbox / "rearm.json").exists()


@pytest.mark.parametrize("act", ["join", "start", "close", "observe"])
@pytest.mark.parametrize("shape", sorted(FRESH_SHAPES))
def test_other_acts_in_a_fresh_run_equal_main(fresh, monkeypatch, act, shape):
    session, halt, exit_record = FRESH_SHAPES[shape]()
    put(fresh.inbox, session, halt=halt, exit_record=exit_record)
    startup = dict(runtime_instance_id=RUNTIME)
    for run in (None, fresh.run):
        now = HA.decide(HA.read_evidence(fresh.inbox, run, recovery=True, startup=startup), act=act)
        with monkeypatch.context() as patch:
            asked = main_launch_rule(patch)
            main = HA.decide(HA.read_evidence(fresh.inbox, run, recovery=True, startup=startup), act=act)
        assert now == main and asked == []


# ================================================================ solo only: an owner-hosted run decides as main

def main_rows(monkeypatch):
    """Main's two rows inside the real table: no closed-title-screen admission, and any run folder (or launch record)
    needs the latest launch's own publication. Returns every call of either predicate."""
    asked = no_title_predicate(monkeypatch)
    asked.extend(main_launch_rule(monkeypatch))
    return asked


def as_main(inbox, run, monkeypatch, **kw):
    """Main's decision for this evidence: the same run made solo (so the table reaches both rows) with main's rows."""
    with monkeypatch.context() as patch:
        main_rows(patch)
        patch.setattr(HA, "_solo_run", lambda ev: True)
        return HA.check(inbox, run, act="launch", **kw)


def same(a, b):
    return (a.action, a.reason, a.halt, a.held, a.watchdog) == (b.action, b.reason, b.halt, b.held, b.watchdog)


# ---------------------------------------------------------------- f. an owner-hosted run holds as main

def test_f_owner_hosted_fresh_run_with_a_clean_title_exit_holds_foreign_session(fresh, monkeypatch):
    """The state: the owner hosts (controller owner, no helper, his foreground HOST window recorded), a fresh
    run, her saved name, a clear title-screen session.json with its own exit, no HALT, token or stop. Main holds
    `foreign_session`; so does this branch, and `mabar rejoin` never reaches the worker that would send input."""
    owner_hosts(fresh.run)
    put(fresh.inbox, title_session(latched=False), exit_record=EXIT)
    assert HA.session_kind(fresh.run) == "owner" and not (fresh.run / "runtime/farmhand-launch.json").exists()
    for watchdog in (True, False):
        with monkeypatch.context() as patch:
            asked = main_rows(patch)
            decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=watchdog)
        assert (decision.action, decision.reason) == ("hold", "foreign_session") and asked == []
        assert same(decision, as_main(fresh.inbox, fresh.run, monkeypatch, recovery=True, allow_watchdog=watchdog))
    monkeypatch.setattr(mabar, "recover_farmhand", lambda *a, **k: pytest.fail("no launch in an owner-hosted run"))
    with pytest.raises(HA.HandsHeld, match="foreign_session"):
        rejoin(fresh)
    [launch] = rows(fresh.run, "rearm_decision")
    assert (launch["act"], launch["action"], launch["reason"]) == ("launch", "hold", "foreign_session")
    assert not (fresh.run / "runtime/farmhand-launch.json").exists() and not (fresh.inbox / "rearm.json").exists()


# ---------------------------------------------------------------- g. the same state in a solo run is admitted

def test_g_solo_fresh_run_with_a_clean_title_exit_launches_clear(fresh, monkeypatch):
    put(fresh.inbox, title_session(latched=False), exit_record=EXIT)
    assert HA.session_kind(fresh.run) == "solo"
    for watchdog in (True, False):
        decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=watchdog)
        assert (decision.action, decision.reason, decision.halt) == ("nothing", "clear", None)
        assert as_main(fresh.inbox, fresh.run, monkeypatch, recovery=True,
                       allow_watchdog=watchdog).reason == "foreign_session"
    assert mabar.rearm_halt(fresh.run, None, "launch", allow_watchdog=True).reason == "clear"
    assert not (fresh.inbox / "rearm.json").exists()


# ---------------------------------------------------------------- h. half a solo record is not solo

NOT_SOLO = {
    "harness_without_helper": ("harness", False),
    "helper_without_harness": ("owner", True),
    "helper_without_controller": (None, True),
    "nothing_recorded": (None, False),
}


@pytest.mark.parametrize("case", sorted(NOT_SOLO) + ["trial_config_unreadable", "trial_config_not_an_object",
                                                    "trial_config_missing", "kind_read_fails"])
@pytest.mark.parametrize("shape", ["title_clear_exit", "title_latched_exit", "farmhand_clear_exit"])
def test_h_a_run_that_is_not_solo_decides_both_rows_as_main(fresh, monkeypatch, case, shape):
    if case in NOT_SOLO:
        host_by(fresh.run, *NOT_SOLO[case])
    elif case == "kind_read_fails":  # a trial config or helper folder that cannot be read: not solo
        def unreadable(run):
            raise PermissionError("trial-config.json")
        monkeypatch.setattr(HA, "session_kind", unreadable)
    else:
        path = fresh.run / "trial-config.json"
        if case == "trial_config_missing":
            path.unlink()
        else:
            path.write_text("{not json" if case == "trial_config_unreadable"
                            else json.dumps(["stardew_host_controller", "harness"]), encoding="utf-8")
        assert (fresh.run / HA.HELPER_CONFIG).is_file() and HA.session_kind(fresh.run) is None
    session, halt, exit_record = FRESH_SHAPES[shape]()
    put(fresh.inbox, session, halt=halt, exit_record=exit_record)
    want = "latest_launch_session_unconfirmed" if shape.startswith("farmhand") else "foreign_session"
    for watchdog in (True, False):
        with monkeypatch.context() as patch:
            asked = main_rows(patch)
            decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=watchdog)
        assert (decision.action, decision.reason) == ("hold", want) and asked == []
        assert same(decision, as_main(fresh.inbox, fresh.run, monkeypatch, recovery=True, allow_watchdog=watchdog))


# ---------------------------------------------------------------- i. a closed title screen in a solo run

def test_i_a_closed_title_screen_in_a_solo_run_relaunches_with_the_halt(fresh, monkeypatch):
    assert HA.session_kind(fresh.run) == "solo" and HA.cycle_dirs(fresh.run) == []
    for watchdog in (True, False):
        with monkeypatch.context() as patch:
            kinds = count_session_kind(patch)
            decision = HA.check(fresh.inbox, fresh.run, act="launch", recovery=True, allow_watchdog=watchdog)
        assert (decision.action, decision.reason) == ("nothing", "launch_relatches:native_unclean_exit")
        assert decision.halt["key"] == KEY and decision.held["halt_seq"] == SEQ and decision.watchdog is None
        assert kinds == [fresh.run]  # who hosts is read once per decision
    assert not (fresh.inbox / "rearm.json").exists()


# ---------------------------------------------------------------- j. every shape in an owner run, and with no run

@pytest.mark.parametrize("where", ["owner_run", "no_run"])
@pytest.mark.parametrize("shape", sorted(FRESH_SHAPES))
def test_j_a_launch_outside_a_solo_run_equals_main_in_every_shape(fresh, monkeypatch, where, shape):
    session, halt, exit_record = FRESH_SHAPES[shape]()
    put(fresh.inbox, session, halt=halt, exit_record=exit_record)
    if where == "owner_run":
        owner_hosts(fresh.run)
    run = fresh.run if where == "owner_run" else None
    for options in (dict(recovery=True, allow_watchdog=True), dict(recovery=True), {}):
        if run is None and options.get("recovery"):
            continue
        with monkeypatch.context() as patch:
            asked = main_rows(patch)
            decision = HA.check(fresh.inbox, run, act="launch", **options)
        assert asked == []  # neither admission is consulted outside a solo run
        assert same(decision, as_main(fresh.inbox, run, monkeypatch, **options))
    if shape == "title_latched_exit":  # the closed title screen
        assert (decision.action, decision.reason) == ("hold", "foreign_session")
    assert not (fresh.inbox / "rearm.json").exists()


def test_the_supervisor_reads_the_same_rule():
    """`session_kind` moved into halt_authority (mabar_supervise imports halt_authority, so the reverse import would be
    a cycle); the supervisor's rung 6 uses the very same function and helper path."""
    assert mabar_supervise.session_kind is HA.session_kind and mabar_supervise.HELPER_CONFIG == HA.HELPER_CONFIG
