"""fix90A / ADR 159: route-leg chaining inside one approach batch and the post-warp read wait.

A leg boundary (a ``home_waypoint`` ARRIVED) continues with the skill's own next leg on the freshly rebuilt snapshot
instead of a full decision round; anything else ends the batch as before. After a warp the exit wait waits on the
new map's native reads (bounded by the wait's own window) instead of a fixed pace.
"""
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import arti_stardew_auto_act as auto_act
import arti_stardew_skills as skills
from arti_stardew_decision import ActChoice, GameDecision
from arti_stardew_runtime import StardewGoalRuntime, _APPROACH_BATCH_MAX
from arti_stardew_skills_runtime import SkillRun, SkillRuntimeMixin

WP1, WP2, EXIT = "home_waypoint:Backwoods:14,31", "home_waypoint:Backwoods:14,23", "warp:Backwoods:50,15->Mountain:0,13"


def decision(ref=WP1):
    return GameDecision(1, "n", "g", 1, "ctx", "", ActChoice("approach_target", ref, {}, ()))


def snapshot(location="Backwoods", refs=(WP2,), clock=900, menu=None, inspect="current", progress=None):
    pin = SimpleNamespace(location=location, player_id=7, screen_id=0, observation_session_id="s")
    motor = SimpleNamespace(location=location, time_of_day=clock, active_menu=menu, status="current", tile=(14, 31))
    mem = SimpleNamespace(last_inspect_status=inspect,
                          approach_progress=progress or dict(target_ref=WP1, status="ARRIVED"))
    return SimpleNamespace(motor=motor, working_memory=mem, water_context=None, movement_context=pin,
                           binding=SimpleNamespace(status="bound"),
                           approach_targets=[dict(ref=r, kind="home_route_stand") for r in refs])


# ---------------------------------------------------------------- skill side: the boundary's checks + choice
class Skills(SkillRuntimeMixin):
    """The skill mixin with the runtime services it reads, all controllable."""

    def __init__(self, **over):
        self._skills_enabled = True
        self._pending_event_reason = None
        self._pending_continuation = None
        self._physical_action_publications = 10
        self._worker_wakeup = SimpleNamespace(notify_all=lambda: None)
        self.rows = []
        self.interrupt = over.pop("interrupt", {})
        self.caps = over.pop("caps", (100, 100, 10_000))
        self.finish_ready = over.pop("finish_ready", False)
        run = SkillRun(call=skills.SkillCall("pergi_ke", "mine"), objective_text="pergi ke mine", by="model",
                       started_at=time.monotonic(), inputs_start=0, goal_id="g", status="running")
        run.state.update(over.pop("state", {}))
        self._skill_session = SimpleNamespace(status="active", current=run, mode="part_live", session_id="ss",
                                              campaign=SimpleNamespace(task="free", live=None))
        self.goal = SimpleNamespace(goal_id="g", interact_objective=object(), home_route_state=over.pop("hop", {}),
                                    food_reselect_due=False)
        self.__dict__.update(over)

    @property
    def run(self):
        return self._skill_session.current

    def _journal(self, row): self.rows.append(row)
    def _stage_lock_holds(self): return False
    def _skill_rule_due(self): return False
    def _part_a_caps(self, run): return self.caps
    def _skill_finish_ready(self, goal, request): return self.finish_ready
    def _live7_exhaustion(self, run, goal, motor): return self.interrupt.get("exhaustion")
    def _live7_attack(self, run, goal, motor): return self.interrupt.get("attack")
    def _live7_route_pause(self, run, goal, motor): return self.interrupt.get("pause")
    def _skill_effect_watchdog(self, run, goal, motor): return self.interrupt.get("watchdog")


@pytest.fixture
def nav(monkeypatch):
    seen = []
    def choice(objective, snap, *, current_location, approach_progress):
        seen.append((snap, current_location, approach_progress))
        first = snap.approach_targets[0]["ref"] if snap.approach_targets else None
        return ("approach_target", first, {}, first) if first else None
    monkeypatch.setattr(auto_act, "nav_auto_choice", choice)
    return seen


def test_the_next_leg_is_the_skills_own_choice_on_the_fresh_snapshot(nav):
    rt, snap = Skills(), snapshot()
    assert rt._skill_route_leg_choice(rt.goal, snap) == WP2
    [(seen, location, progress)] = nav
    assert seen is snap and location == "Backwoods" and progress["status"] == "ARRIVED"
    rt._skill_route_leg_commit(rt.goal, WP2)
    [row] = rt.rows
    assert row["ev"] == "skill_step" and row["chained"] is True and row["target"] == WP2 and rt.run.decisions == 1


@pytest.mark.parametrize("which", ["attack", "pause", "exhaustion", "watchdog"])
def test_an_interrupt_at_the_leg_boundary_ends_the_chain_and_stays_the_runs_outcome(nav, which):
    rt = Skills(interrupt={which: ("fail", which)})
    assert rt._skill_route_leg_choice(rt.goal, snapshot()) is None
    assert rt.run.pending == ("fail", which)
    assert rt.rows[-1]["ev"] == "skill_step_boundary" and rt.rows[-1]["reason"] == which
    assert not nav  # no navigation choice past an interrupt


@pytest.mark.parametrize("case", ["event", "rule", "repair", "route_clear", "menu", "inspect_stale", "curfew",
                                  "reserve", "caps", "finish", "not_pergi_ke", "other_map_motor"])
def test_anything_the_boundary_owns_ends_the_chain_without_a_choice(nav, case):
    over, snap = {}, snapshot()
    if case == "event":
        over["_pending_event_reason"] = "npc_event"
    elif case == "rule":
        over["_skill_rule_due"] = lambda: True
    elif case == "repair":
        over["hop"] = dict(repair_reaches=[[1, 2]])
    elif case == "route_clear":
        over["state"] = dict(route_clear=True)
    elif case == "menu":
        snap = snapshot(menu="DialogueBox")
    elif case == "inspect_stale":
        snap = snapshot(inspect="stale")
    elif case == "curfew":
        snap = snapshot(clock=2510)
    elif case == "reserve":
        snap = snapshot(clock=2330)  # the boundary latches the clock reserve here
    elif case == "caps":
        over["caps"] = (100, 10, 10_000)  # 10 inputs spent
    elif case == "finish":
        over["finish_ready"] = True
    rt = Skills(**over)
    if case == "not_pergi_ke":
        rt.run.call = skills.SkillCall("tebang", None)
    if case == "other_map_motor":
        snap.motor.location = "Farm"
    assert rt._skill_route_leg_choice(rt.goal, snap) is None
    assert rt.run.pending is None and not nav


def test_an_exit_to_cross_is_left_to_the_boundary(monkeypatch):
    monkeypatch.setattr(auto_act, "nav_auto_choice", lambda *a, **k: ("move_through", None, {}, "warp"))
    rt = Skills()
    assert rt._skill_route_leg_choice(rt.goal, snapshot()) is None


# ---------------------------------------------------------------- runtime side: successor + batch wiring
class Batch:
    _batch_route_leg_successor = StardewGoalRuntime._batch_route_leg_successor
    _batch_next_request = StardewGoalRuntime._batch_next_request
    _execute_batched = StardewGoalRuntime._execute_batched

    def __init__(self, choice=WP2, live7=True, snap=None):
        self._lock = threading.RLock()
        self.rows, self.commits, self.moves, self.settled = [], [], [], []
        self.choice, self.live7, self.snap = choice, live7, snap or snapshot()
        self.chain_always = False

    def _live7_active(self): return self.live7
    def _game_now(self): return time.monotonic()
    def _journal(self, row): self.rows.append(row)
    def _build_decision_snapshot(self, goal): return self.snap
    def _skill_route_leg_choice(self, goal, snap): return self.choice
    def _skill_route_leg_commit(self, goal, ref): self.commits.append(ref)
    def _batch_common_stop(self, request, goal): return None
    def _batch_approach_stop(self, decision, goal, evidence): return "approach_arrived", False

    def _batch_step_request(self, decision, request, goal, *, tool, snapshot=None, new_target=False):
        assert new_target and snapshot is self.snap  # one fresh snapshot, no second rebuild
        ticket = SimpleNamespace(decision=decision)
        return SimpleNamespace(continuation=ticket, generation_id="gen"), None


def done(cleanup=True, lifecycle="completed"):
    evidence = SimpleNamespace(lifecycle=lifecycle, cleanup_confirmed=cleanup, outcome="PressCompleted",
                               world_effect_status="observed_change", request_id="r")
    return SimpleNamespace(discarded=False, applied=True, action_evidence=evidence)


def test_a_waypoint_arrival_yields_a_successor_for_the_next_leg():
    rt, goal = Batch(), SimpleNamespace()
    successor, snap = rt._batch_route_leg_successor(decision(), SimpleNamespace(snapshot=rt.snap), goal)
    assert successor.choice.target_ref == WP2 and successor.choice.operation == "approach_target"
    assert successor.request_nonce == "n" and snap is rt.snap


@pytest.mark.parametrize("case", ["not_live7", "not_waypoint", "same_ref", "not_offered", "map_changed", "no_choice"])
def test_no_successor_outside_a_same_map_route_leg(case):
    rt = Batch(live7=case != "not_live7", choice={"same_ref": WP1, "no_choice": None}.get(case, WP2),
               snap=snapshot(refs=() if case == "not_offered" else (WP2,)))
    request = SimpleNamespace(snapshot=snapshot(location="Farm" if case == "map_changed" else "Backwoods"))
    d = decision(EXIT if case == "not_waypoint" else WP1)
    assert rt._batch_route_leg_successor(d, request, SimpleNamespace()) is None


def test_the_batch_chains_only_after_a_completed_released_press():
    rt = Batch()
    following, stop, _ = rt._batch_next_request(decision(), SimpleNamespace(generation_id="gen", snapshot=snapshot()), SimpleNamespace(),
                                                done(), tool=False, facing_taken=False)
    assert stop is None and following.continuation.decision.choice.target_ref == WP2
    assert rt.commits == [WP2] and rt.rows[-1]["ev"] == "route_leg_chained" and rt.rows[-1]["to"] == WP2
    for result in (done(cleanup=False), done(lifecycle="uncertain")):
        rt = Batch()
        following, stop, _ = rt._batch_next_request(decision(), SimpleNamespace(generation_id="gen", snapshot=snapshot()),
                                                    SimpleNamespace(), result, tool=False, facing_taken=False)
        assert following is None and stop in ("cleanup_unconfirmed", "not_completed") and not rt.commits


def test_no_successor_keeps_the_old_arrived_stop():
    rt = Batch(choice=None)
    following, stop, _ = rt._batch_next_request(decision(), SimpleNamespace(generation_id="gen", snapshot=snapshot()), SimpleNamespace(),
                                                done(), tool=False, facing_taken=False)
    assert following is None and stop == "approach_arrived" and not rt.commits


class Executed(Batch):
    """_execute_batched over the real wiring: each press is recorded with the decision it ran under."""

    def __init__(self, legs):
        super().__init__()
        self.legs = list(legs)

    def _execute_move(self, decision, request, goal, hold):
        assert hold is True
        self.moves.append(decision.choice.target_ref)
        return done()

    def _batch_next_request(self, decision, request, goal, result, **kw):
        if not self.legs:
            return None, "approach_arrived", False
        nxt = self.legs.pop(0)
        return SimpleNamespace(continuation=SimpleNamespace(decision=decision if nxt is None else
                               replace(
                                   decision, choice=ActChoice("approach_target", nxt, {}, ())))), None, False

    def _settle_held_action(self, decision, request, result, *, leased):
        self.settled.append(decision.choice.target_ref)


def test_a_chained_batch_presses_and_settles_under_the_successor_decision():
    rt = Executed([None, WP2, None])  # leg 1 two presses, then leg 2 two presses
    rt._execute_batched(decision(), SimpleNamespace(generation_id="gen"), SimpleNamespace(gather_progress=None, last_effect_motor=None))
    assert rt.moves == [WP1, WP1, WP2, WP2] and rt.settled == [WP2]
    [batch] = [r for r in rt.rows if r["ev"] == "approach_batch"]
    assert batch["steps"] == 4 and batch["stop"] == "approach_arrived"


def test_chaining_keeps_the_batch_press_bound():
    rt = Executed([WP2, EXIT] * 10)
    rt._execute_batched(decision(), SimpleNamespace(generation_id="gen"), SimpleNamespace(gather_progress=None, last_effect_motor=None))
    assert len(rt.moves) == _APPROACH_BATCH_MAX
    assert [r for r in rt.rows if r["ev"] == "approach_batch"][0]["stop"] == "batch_max"


# ---------------------------------------------------------------- map change: wait on the new map's reads
class Record(SimpleNamespace):
    pass


def world(location, places=({"id": "Mountain", "reachable_now": True},), received=None):
    return Record(available=True, authoritative=True, is_complete=True, stale=False, requires_resync=False,
                  screen_id=0, session_id="s", received_monotonic=time.monotonic() if received is None else received,
                  facts={"world_graph": {"current": location, "places": list(places)}}, domain_revision=1)


class Reads(SkillRuntimeMixin):
    def __init__(self, records, motor_location="Backwoods"):
        self.records, self.rows = records, []
        self._worker_stop, self._action_cancel = threading.Event(), threading.Event()
        self._owner_voice_busy = False
        self._obs = SimpleNamespace(get=lambda pid, domain: self.records.get(domain))
        self._state = SimpleNamespace(latest=lambda pid: SimpleNamespace(location=self.motor_location))
        self.motor_location = motor_location

    def _journal(self, row): self.rows.append(row)


PIN = SimpleNamespace(location="Backwoods", player_id=7, screen_id=0, observation_session_id="s")


# Each case builds its records when the test runs: a frame stamped at collection time is older than the
# 2 s freshness window by the time a wide run reaches this test.
@pytest.mark.parametrize("records,settling", [
    (lambda: {"knowledge.world": world("Farm")}, True),              # the old map's graph
    (lambda: {}, True),                                              # no frame yet
    (lambda: {"knowledge.world": world("Backwoods", received=time.monotonic() - 5)}, True),  # older than 2 s
    (lambda: {"knowledge.world": world("Backwoods", places=())}, True),  # the capture-failure frame
    (lambda: {"knowledge.world": world("Backwoods"),
              "ui.context": Record(facts={"nav_grid": {"location": "Farm"}})}, True),  # the old map's nav grid
    (lambda: {"knowledge.world": world("Backwoods"),
              "ui.context": Record(facts={"nav_grid": {"location": "Backwoods"}})}, False),
    (lambda: {"knowledge.world": world("Backwoods")}, False),
])
def test_route_reads_settling_is_true_only_while_the_new_maps_reads_lag(records, settling):
    assert Reads(records())._skill_route_reads_settling(PIN) is settling


def test_the_exit_wait_proceeds_as_soon_as_the_new_map_is_readable():
    rt = Reads({"knowledge.world": world("Farm")})
    threading.Timer(0.2, lambda: rt.records.update({"knowledge.world": world("Backwoods")})).start()
    started = time.monotonic()
    row = rt._skill_await_route_reads(PIN, pace=0.3)
    assert row["settled"] is True and 0.15 <= time.monotonic() - started < 0.6  # not the 3 s window
    assert rt.rows == [row] and row["ev"] == "route_reads_wait"


def test_the_existing_window_is_the_ceiling():
    rt = Reads({"knowledge.world": world("Farm")})
    started = time.monotonic()
    row = rt._skill_await_route_reads(PIN, pace=0.3)
    assert row["settled"] is False and skills.WAIT_S <= time.monotonic() - started < skills.WAIT_S + 0.5


@pytest.mark.parametrize("stop", ["cancel", "worker_stop", "owner_voice", "left_map"])
def test_an_interrupt_or_another_map_ends_the_wait_at_once(stop):
    rt = Reads({"knowledge.world": world("Farm")})
    if stop == "cancel":
        rt._action_cancel.set()
    elif stop == "worker_stop":
        rt._worker_stop.set()
    elif stop == "owner_voice":
        rt._owner_voice_busy = True
    else:
        rt.motor_location = "Mountain"
    started = time.monotonic()
    row = rt._skill_await_route_reads(PIN, pace=0.3)
    assert row["settled"] is False and time.monotonic() - started < 0.6  # the old pace floor at most


def test_the_trip_exit_wait_names_its_question():
    state = {"clock": time.monotonic}
    step = skills._wait(state, "nav", "exit_not_offered", skills.EXIT_WAIT_QUESTION, skills.WAIT_S)
    assert step.choice["question"] == skills.EXIT_WAIT_QUESTION == "skill step: observe exits" and step.pace > 0
