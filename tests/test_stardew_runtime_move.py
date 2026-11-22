"""G1 -> existing G2 client composition; all native/file IO uses tmp_path/fakes."""
import json
import threading
import time
from dataclasses import replace

import pytest

import arti_stardew_command as command
from arti_stardew import StardewStateStore
from arti_stardew_observation import RichObservationStore
from arti_stardew_runtime import FakeDecisionProvider, StardewGoalRuntime

PID = -123
REAL_INPUT_ONCE = command.minta_input_once
SYNTHETIC_LOCATION_ID = "CabinInstance~synthetic-location-42"


def state(seq=2, *, tile=(3, 8), location="Farm", facing=2):
    return dict(ev="state", seq=seq, player_id=PID, screen_id=0, payload=dict(
        location=location, tile=dict(x=tile[0], y=tile[1]), pixel=dict(x=192., y=512.),
        facing=facing, time_of_day=610, date=dict(season="spring", day=1, year=1),
        energy=270., health=100, is_player_free=True, can_player_move=True,
        active_menu=None, ui_revision=1))


def choice(req, **changes):
    act = dict(kind="act", operation="world_key.move_cardinal", target_ref=None,
               args=dict(direction="right", ticks=10), evidence_refs=[])
    act.update(changes)
    return json.dumps(dict(v=1, request_nonce=req.request_nonce, goal_id=req.goal_id,
                          goal_revision=req.goal_revision, context_ref=req.context_ref,
                          speech="", choice=act))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    motor, obs = StardewStateStore(), RichObservationStore()
    motor.catat(dict(ev="hello", seq=1, player_id=PID, screen_id=0, player_name="Arti",
                     is_main_player=False, is_split_screen=False))
    motor.catat(state())
    facts = dict(runtime_instance_id="runtime-1", context_generation=4,
                 session_id="command-1", player_id=PID, screen_id=0,
                 ui_revision=1, viewport_transform_revision=1, location_id="Farm",
                 mode="gameplay", is_player_free=True, can_player_move=True,
                 capabilities={"world_key": {"available": True, "primitives": ["move_cardinal"]}})
    assert obs.apply(dict(ev="observation", seq=3, player_id=PID, screen_id=0, payload=dict(
        session_id="observation-epoch-7", product="ui", domain="ui.context", kind="snapshot",
        domain_revision=1, provenance="LOCAL_AUTHORITATIVE", authoritative=True,
        available=True, error=None, truncation=dict(truncated=False, observed_count=len(facts),
        sent_count=len(facts)), body=dict(facts=facts)))).accepted
    folder = tmp_path / "command"
    folder.mkdir()
    raw = dict(protocol=1, session_id="command-1", runtime_instance_id="runtime-1",
               player_id=PID, player_name="Arti", screen_id=0, location_id="Farm",
               is_main_player=False, is_split_screen=False, inbox_enabled=True,
               world_press_enabled=True, world_ready=True, input_busy=False,
               inflight_request_id=None, owner_id=None, generation=None, control_seq=None,
               lease_until_unix_ms=None, stop_latched=False, record_faulted=False,
               release_requested=False, release_acked=True)
    def publish():
        (folder / "session.json").write_text(json.dumps(raw), encoding="utf8")
    publish()
    calls = []
    real_control = command.tulis_kontrol
    def control(kind, **kw):
        calls.append(kind)
        real_control(kind, **kw)
        if kind == "acquire":
            raw.update(owner_id=kw["owner_id"], generation=kw["generation"],
                       control_seq=kw["control_seq"], lease_until_unix_ms=int(time.time()*1000)+1500)
            if raw.get("stop_latched") and raw.get("release_acked") and kw["generation"] > (raw.get("stop_generation") or 0):
                raw.update(stop_latched=False, stop_generation=0)  # NativeActionOwner.rearmAfterStop → ClearStopLatch
        elif kind == "release":
            raw.update(owner_id=None, generation=None, control_seq=None, lease_until_unix_ms=None)
        publish()
        (folder / "control.json").unlink()
    def request(*args, **kw):
        calls.append("input_once")
        return command.HasilPerintah(command.SELESAI, True, "req-1", "PressCompleted",
            mentah=dict(session_id="command-1", op="input_once", request_id="req-1",
                        accepted=True, direction=args[0], press_count=args[1]))
    monkeypatch.setattr(command, "tulis_kontrol", control)
    monkeypatch.setattr(command, "minta_input_once", request)
    for name in ("minta_world_press", "minta_water_once", "minta_menu_step", "minta_inspect_context"):
        monkeypatch.setattr(command, name, lambda *a, **k: pytest.fail("wrong command path"))
    rt = StardewGoalRuntime(config=dict(stardew_runtime_enabled=True,
        stardew_command_dir=str(folder)), state_store=motor, observation_store=obs,
        provider=FakeDecisionProvider(choice), journal_dir=tmp_path/"journal",
        start_listener=False, start_worker=False)
    rt.start()
    # This rig publishes its synthetic frames synchronously. Absence is immediate;
    # explicit asynchronous/native tests supply their own wait implementation.
    wait_next = motor.wait_next
    monkeypatch.setattr(motor, "wait_next", lambda pid, revision, timeout:
                        wait_next(pid, revision, 0))
    from tests.stardew_test_clock import virtual_readback_deadlines
    virtual_readback_deadlines(rt, monkeypatch)
    return rt, motor, obs, raw, calls, publish, folder


def run(rig, script=choice):
    rt = rig[0]
    rt._provider = FakeDecisionProvider(script)
    return rt.decide_owner_turn(text="move", trigger_type="ptt", viewer_name=None)


def test_subtile_pixel_displacement_is_preserved_in_movement_evidence(rig, monkeypatch):
    rt, motor = rig[:2]
    def post(pid, revision, timeout):
        event = state(revision + 1)
        event["payload"]["pixel"]["x"] = 208.0
        motor.catat(event)
        return motor.latest(pid)
    monkeypatch.setattr(motor, "wait_next", post)
    result = run(rig)
    evidence = result.action_evidence
    assert evidence.lifecycle == "completed" and evidence.cleanup_confirmed
    assert evidence.world_effect_status == "observed_change"
    assert evidence.observed_changes == ("pixel",)
    assert evidence.before["tile"] == evidence.after["tile"] == [3, 8]
    assert evidence.before["pixel"] == [192.0, 512.0]
    assert evidence.after["pixel"] == [208.0, 512.0]
    assert rt._build_decision_snapshot(rt.active_goal).motor.pixel == (208.0, 512.0)
    assert rig[4] == ["acquire", "input_once", "release"]


def test_subtile_displacement_does_not_satisfy_unreached_tile_goal(rig, monkeypatch):
    rt, motor = rig[:2]
    def post(pid, revision, timeout):
        event = state(revision + 1)
        event["payload"]["pixel"]["x"] = 208.0
        motor.catat(event)
        return motor.latest(pid)
    monkeypatch.setattr(motor, "wait_next", post)
    moved = rt.decide_owner_turn(text="reach declared tile", trigger_type="ptt", viewer_name=None,
                                completion_tile=(4, 8))
    assert moved.action_evidence.world_effect_status == "observed_change"
    finished = run(rig, finish_choice)
    assert finished.finish_evidence.status == "partial"
    assert finished.finish_evidence.reason == "goal_tile_not_reached"
    assert rt.active_goal.progress_marker != "terminal"
    assert rig[4] == ["acquire", "input_once", "release"]


@pytest.mark.parametrize("fault", ["old_sample", "session"])
def test_pixel_displacement_cannot_bypass_readback_freshness_or_session(rig, monkeypatch, fault):
    rt, motor, obs = rig[:3]
    def post(pid, revision, timeout):
        event = state(revision + 1)
        event["payload"]["pixel"]["x"] = 208.0
        motor.catat(event)
        if fault == "old_sample":
            return replace(motor.latest(pid), revisi=revision)
        for key, rec in obs._records.items():
            obs._records[key] = replace(rec, session_id="replacement-observation")
        return motor.latest(pid)
    monkeypatch.setattr(motor, "wait_next", post)
    result = run(rig)
    assert result.action_evidence.world_effect_status == "uncertain"
    assert result.action_evidence.after is None and rt.status == "PAUSED_RECOVERY"


def set_location_vocabularies(rig, motor_location, location_id):
    rig[1].catat(state(4, location=motor_location))
    for key, rec in rig[2]._records.items():
        rig[2]._records[key] = replace(rec, facts={**rec.facts, "location_id": location_id})
    rig[3]["location_id"] = location_id
    rig[5]()


def finish_choice(req):
    return json.dumps(dict(v=1, request_nonce=req.request_nonce, goal_id=req.goal_id,
        goal_revision=req.goal_revision, context_ref=req.context_ref,
        speech="SEMUA TANAMAN SUDAH DISIRAM [MC: join]", choice=dict(kind="finish")))


@pytest.mark.parametrize("target,observed,status,reason", [
    ((4, 8), (4, 8), "achieved", "observed_goal_tile"),
    ((4, 8), (3, 8), "partial", "goal_tile_not_reached"),
    (None, (4, 8), "partial", "goal_criterion_unavailable"),
])
def test_finish_uses_fresh_goal_oracle_not_model_speech(rig, monkeypatch, target, observed, status, reason):
    rt, motor = rig[:2]
    boundaries = []
    def post(pid, revision, timeout):
        boundaries.append(revision)
        motor.catat(state(10, tile=observed))
        return motor.latest(pid)
    monkeypatch.setattr(motor, "wait_next", post)
    rt._provider = FakeDecisionProvider(finish_choice)
    result = rt.decide_owner_turn(text="reach declared tile", trigger_type="ptt",
                                  viewer_name=None, completion_tile=target)
    assert result.finish_evidence.status == status
    assert result.finish_evidence.reason == reason
    assert result.finish_evidence.observed["revisi"] > boundaries[0]
    assert "TANAMAN" not in result.speech and "[MC:" not in result.speech
    assert rt.active_goal.last_finish_proposal.startswith("SEMUA")
    assert rt.active_goal.last_finish == result.finish_evidence
    assert rt.inbox_ops == 0 and rig[4] == []
    assert not (rig[6] / "request.json").exists()
    if status == "achieved":
        rt.note_event("another state")
        assert rt.process_pending_events() == 0
        assert len(rt._provider.calls) == 1


@pytest.mark.parametrize("fault", ["missing", "same_revision", "stale", "session", "runtime",
    "player", "screen", "context", "location", "raw_location", "unfocused", "unreconciled"])
def test_finish_rejects_missing_old_or_mismatched_readback(rig, monkeypatch, fault):
    from arti_stardew_runtime import ActionEvidenceView
    rt, motor, obs, session, calls, publish, folder = rig
    # Initialize through the public turn, without fabricating a lifecycle success.
    def speak(req):
        value = json.loads(finish_choice(req))
        value["choice"] = dict(kind="speak")
        return json.dumps(value)
    rt._provider = FakeDecisionProvider(speak)
    rt.decide_owner_turn(text="goal", trigger_type="ptt", viewer_name=None, completion_tile=(4, 8))
    if fault == "unreconciled":
        rt.active_goal.last_action = ActionEvidenceView("uncertain", "timeout")
    def post(pid, revision, timeout):
        if fault == "missing":
            return None
        if fault == "same_revision":
            return motor.latest(pid)
        motor.catat(state(10, tile=(4, 8), location="Town" if fault == "raw_location" else "Farm"))
        if fault == "stale":
            motor._state[pid] = replace(motor.latest(pid), diterima_pada=time.monotonic()-10)
        if fault in {"session", "runtime", "player", "screen", "location"}:
            field, value = {"session": ("session_id", "new-session"),
                "runtime": ("runtime_instance_id", "new-runtime"), "player": ("player_id", 999),
                "screen": ("screen_id", 2), "location": ("location_id", "Other")}[fault]
            session[field] = value
            publish()
        if fault in {"context", "unfocused"}:
            for key, rec in obs._records.items():
                updates = {"context_generation": 8} if fault == "context" else {"is_player_free": False}
                obs._records[key] = replace(rec, facts={**rec.facts, **updates})
        return motor.latest(pid)
    monkeypatch.setattr(motor, "wait_next", post)
    rt._provider = FakeDecisionProvider(finish_choice)
    result = rt.decide_owner_turn(text="finished?", trigger_type="ptt", viewer_name=None)
    assert result.finish_evidence.status == "uncertain"
    assert rt.active_goal.progress_marker != "terminal"
    assert rig[4] == [] and rt.inbox_ops == 0


def test_finish_wait_does_not_block_replacement_or_publish_late_success(rig, monkeypatch):
    rt, motor = rig[:2]
    entered, release = threading.Event(), threading.Event()
    def post(pid, revision, timeout):
        entered.set()
        assert release.wait(3)
        motor.catat(state(10, tile=(4, 8)))
        return motor.latest(pid)
    monkeypatch.setattr(motor, "wait_next", post)
    rt._provider = FakeDecisionProvider(finish_choice)
    results = []
    thread = threading.Thread(target=lambda: results.append(rt.decide_owner_turn(
        text="goal", trigger_type="ptt", viewer_name=None, completion_tile=(4, 8))))
    thread.start()
    try:
        assert entered.wait(2)
        def speak(req):
            payload = json.loads(finish_choice(req))
            payload["choice"] = dict(kind="speak")
            return json.dumps(payload)
        rt._provider = FakeDecisionProvider(speak)
        replacement = rt.decide_owner_turn(text="new goal", trigger_type="ptt",
                                           viewer_name=None, replace_goal=True)
        assert replacement.applied
        release.set()
        thread.join(3)
        assert not thread.is_alive() and results[0].discarded
        assert results[0].speech == "" and results[0].finish_evidence is None
        assert rt.active_goal.text == "new goal" and rt.active_goal.last_finish is None
        assert rt.inbox_ops == 0 and rig[4] == []
    finally:
        release.set()
        thread.join(3)


@pytest.mark.parametrize("target", [(True, 8), (1,), [1, 2], (10001, 0)])
def test_finish_condition_rejects_invalid_caller_target_before_inference(rig, target):
    result = rig[0].decide_owner_turn(text="goal", trigger_type="ptt", viewer_name=None,
                                      completion_tile=target)
    assert not result.admitted and result.reason == "invalid_completion_tile"
    assert rig[0]._provider.calls == [] and rig[4] == []


def test_finish_condition_cannot_be_overwritten_by_a_conversation(rig):
    rt = rig[0]
    def speak(req):
        payload = json.loads(finish_choice(req))
        payload["choice"] = dict(kind="speak")
        return json.dumps(payload)
    rt._provider = FakeDecisionProvider(speak)
    rt.decide_owner_turn(text="goal", trigger_type="ptt", viewer_name=None, completion_tile=(4, 8))
    result = rt.decide_owner_turn(text="change target", trigger_type="ptt", viewer_name=None,
                                  completion_tile=(9, 9))
    assert not result.admitted and result.reason == "completion_requires_new_goal"
    assert rt.active_goal.completion_tile == (4, 8)
    assert rt._provider.calls[0].snapshot.working_memory.completion_tile == (4, 8)
    assert len(rt._provider.calls) == 1 and rig[4] == []


@pytest.fixture
def distinct_location_rig(rig):
    set_location_vocabularies(rig, "Cabin", SYNTHETIC_LOCATION_ID)
    return rig


def test_distinct_location_vocabularies_submit_once_with_tokenized_pin(distinct_location_rig, monkeypatch):
    rig = distinct_location_rig
    original = command.minta_input_once
    submitted = []
    def request(*args, **kw):
        submitted.append(kw)
        return original(*args, **kw)
    monkeypatch.setattr(command, "minta_input_once", request)
    result = run(rig)
    assert result.reason == "PressCompleted"
    assert rig[4] == ["acquire", "input_once", "release"]
    assert rig[0].inbox_ops == len(submitted) == 1
    assert submitted[0]["expected_location_id"] == SYNTHETIC_LOCATION_ID
    assert result.action_evidence.before["location"] == "Cabin"
    assert result.action_evidence.cleanup_confirmed
    assert result.action_evidence.world_effect_status == "uncertain"
    assert result.action_evidence.after is None
    rig[0].note_event("tick")
    rig[0].process_pending_events()
    assert len(rig[0]._provider.calls) == 1
    assert rig[4] == ["acquire", "input_once", "release"]


@pytest.mark.parametrize("surface,field,value", [
    ("motor", "location", "Town"),
    ("motor", "screen_id", 2),
    ("motor", "ui_revision", 2),
    ("motor", "is_player_free", False),
    ("motor", "can_player_move", False),
    ("context", "location_id", "CabinInstance~other-location"),
    ("context", "context_generation", 5),
    ("context", "ui_revision", 2),
    ("context", "runtime_instance_id", "other-runtime"),
    ("context", "session_id", "other-session"),
    ("context", "player_id", 88),
    ("context", "screen_id", 2),
    ("session", "location_id", "CabinInstance~other-location"),
    ("session", "session_id", "other-session"),
    ("session", "runtime_instance_id", "other-runtime"),
    ("session", "player_id", 88),
    ("session", "screen_id", 2),
    ("session", "world_ready", False),
    ("stale", "motor", None),
    ("stale", "context", None),
])
def test_distinct_vocabularies_keep_changed_pins_zero_write(distinct_location_rig, surface, field, value):
    rig = distinct_location_rig
    def changed(req):
        assert req.snapshot.motor.location == "Cabin"
        assert req.snapshot.movement_context.location == SYNTHETIC_LOCATION_ID
        if surface == "motor":
            rig[1]._state[PID] = replace(rig[1].latest(PID), **{field: value})
        elif surface == "context":
            for key, rec in rig[2]._records.items():
                rig[2]._records[key] = replace(rec, facts={**rec.facts, field: value})
        elif surface == "session":
            rig[3][field] = value
            rig[5]()
        elif field == "motor":
            rig[1]._state[PID] = replace(rig[1].latest(PID), diterima_pada=time.monotonic()-10)
        else:
            rig[2].mark_stale(PID, "ui.context")
        return choice(req)
    run(rig, changed)
    assert len(rig[0]._provider.calls) == 1
    assert rig[4] == []
    assert rig[0].inbox_ops == 0
    assert not (rig[6]/"request.json").exists()
    assert not (rig[6]/"control.json").exists()


def test_move_acquire_one_request_release_and_no_invented_displacement(rig):
    result = run(rig)
    assert rig[4] == ["acquire", "input_once", "release"]
    assert result.action_evidence.lifecycle == "completed"
    assert result.action_evidence.world_effect_status == "uncertain"
    assert result.action_evidence.after is None
    assert rig[0].active_goal.last_action == result.action_evidence


@pytest.mark.parametrize("operation", ["world_press", "water_once", "use_tool", "world_key.other", "unknown"])
def test_other_operations_zero_writes(rig, operation):
    assert run(rig, lambda req: choice(req, operation=operation)).reason == "choice_unavailable"
    assert rig[4] == []


@pytest.mark.parametrize("args", [dict(direction="north", ticks=1), dict(direction="left", ticks=True),
    dict(direction="left", ticks=0), dict(direction="left", ticks=61), dict(direction="left", ticks=1.5),
    dict(direction="left"), dict(direction="left", ticks=1, keycode=65)])
def test_invalid_args_zero_writes(rig, args):
    run(rig, lambda req: choice(req, args=args))
    assert rig[4] == []


@pytest.mark.parametrize("field,value", [("is_main_player", True), ("is_split_screen", True),
    ("player_id", 88), ("screen_id", 2), ("location_id", "Town"),
    ("runtime_instance_id", "other-runtime"), ("session_id", "other-command"),
    ("world_ready", False), ("stop_latched", True), ("record_faulted", True),
    ("release_requested", True), ("world_press_enabled", False)])
def test_session_preconditions_zero_writes(rig, field, value):
    rig[3][field] = value
    rig[5]()
    run(rig)
    assert rig[4] == []


def test_stale_motor_zero_writes(rig):
    rig[1]._state[PID] = replace(rig[1].latest(PID), diterima_pada=time.monotonic()-10)
    run(rig)
    assert rig[4] == []


def test_stale_context_zero_writes(rig):
    rig[2].mark_stale(PID, "ui.context")
    run(rig)
    assert rig[4] == []


def test_context_changes_during_decision_zero_writes(rig):
    def changed(req):
        rec = rig[2].get(PID, "ui.context")
        # Immutable store record, replaced as a newly observed context.
        for key, old in rig[2]._records.items():
            if old is rec:
                rig[2]._records[key] = replace(rec, facts={**rec.facts, "context_generation": 5})
                break
        return choice(req)
    run(rig, changed)
    assert rig[4] == []


@pytest.mark.parametrize("tile,facing,location,expected", [((3,8),2,"Farm","unchanged"),
    ((4,8),1,"Farm","observed_change"), ((3,8),1,"Town","uncertain")])
def test_only_fresh_post_return_state_is_world_evidence(rig, monkeypatch, tile, facing, location, expected):
    original = rig[1].wait_next
    def readback(pid, after_revisi, timeout):
        rig[1].catat(state(10, tile=tile, facing=facing, location=location))
        rig[3]["location_id"] = location
        rig[5]()
        for key, rec in rig[2]._records.items():
            rig[2]._records[key] = replace(rec, facts={**rec.facts, "location_id": location})
        return original(pid, after_revisi, 0)
    monkeypatch.setattr(rig[1], "wait_next", readback)
    result = run(rig)
    assert result.action_evidence.world_effect_status == expected
    if expected == "uncertain":
        # A changed context/session token no longer matches the original pin.
        assert result.action_evidence.after is None
        return
    assert result.action_evidence.after["tile"] == list(tile)
    assert result.action_evidence.after["facing"] == facing
    assert result.action_evidence.after["location"] == location


@pytest.mark.parametrize("tile,raw_location,expected,changes", [
    ((4, 8), "Cabin", "observed_change", ("tile",)),
    ((3, 8), "Cabin", "unchanged", ()),
    ((3, 8), "CabinLabelChanged", "observed_change", ("location",)),
])
def test_distinct_vocabularies_fresh_readback(distinct_location_rig, monkeypatch,
                                            tile, raw_location, expected, changes):
    rig = distinct_location_rig
    original_request = command.minta_input_once
    original_wait = rig[1].wait_next
    during = []
    boundaries = []
    def request(*args, **kw):
        assert kw["expected_location_id"] == SYNTHETIC_LOCATION_ID
        rig[1].catat(state(5, location="Cabin"))
        during.append(rig[1].latest(PID).revisi)
        return original_request(*args, **kw)
    def readback(pid, after_revisi, timeout):
        # The command has returned and release completed before this callback.
        assert rig[4] == ["acquire", "input_once", "release"]
        assert after_revisi == during[0]
        assert rig[1].latest(pid).seq == 5
        boundaries.append(after_revisi)
        rig[1].catat(state(10, tile=tile, location=raw_location))
        assert rig[2].get(pid, "ui.context").facts["location_id"] == SYNTHETIC_LOCATION_ID
        assert rig[3]["location_id"] == SYNTHETIC_LOCATION_ID
        return original_wait(pid, after_revisi, 0)
    monkeypatch.setattr(command, "minta_input_once", request)
    monkeypatch.setattr(rig[1], "wait_next", readback)
    result = run(rig)
    ev = result.action_evidence
    assert result.applied and ev.lifecycle == "completed" and ev.outcome == "PressCompleted"
    assert ev.cleanup_confirmed
    assert len(boundaries) == 1
    assert ev.world_effect_status == expected
    assert ev.after["revisi"] > boundaries[0] > ev.before["revisi"]
    assert rig[1].latest(PID).seq == 10
    assert ev.after["revisi"] == rig[1].latest(PID).revisi
    assert ev.after["tile"] == list(tile)
    assert ev.after["location"] == raw_location
    assert ev.observed_changes == changes
    # Completion/readback itself must not enqueue a new decision. Explicitly
    # scheduling a new event would request another owner turn on a READY goal.
    assert rig[0].process_pending_events() == 0
    assert rig[4] == ["acquire", "input_once", "release"]
    assert rig[0].inbox_ops == len(rig[0]._provider.calls) == 1


@pytest.mark.parametrize("surface,field,value", [
    ("context", "location_id", "CabinInstance~changed"),
    ("session", "location_id", "CabinInstance~changed"),
    ("both", "location_id", "CabinInstance~changed"),
    ("context", "runtime_instance_id", "changed-runtime"),
    ("session", "runtime_instance_id", "changed-runtime"),
    ("context", "session_id", "changed-session"),
    ("session", "session_id", "changed-session"),
    ("context", "player_id", 88),
    ("session", "player_id", 88),
    ("post", "player_id", 88),
    ("context", "screen_id", 2),
    ("session", "screen_id", 2),
    ("post", "screen_id", 2),
    ("record", "session_id", "changed-observation-epoch"),
    ("record", "player_id", 88),
    ("record", "screen_id", 2),
    ("record", "authoritative", False),
    ("record", "truncated", True),
    ("context", "ui_revision", 2),
    ("post", "ui_revision", 2),
    ("post", "stale", None),
    ("post", "same_revision", None),
    ("post", "missing", None),
    ("record", "missing", None),
    ("binding", "player_name", "changed-name"),
    ("binding", "target", 88),
])
def test_distinct_vocabularies_invalid_post_readback_never_replays(
        distinct_location_rig, monkeypatch, surface, field, value):
    rig = distinct_location_rig
    def readback(pid, after_revisi, timeout):
        assert rig[4] == ["acquire", "input_once", "release"]
        if field == "missing" and surface == "post":
            return None
        if field == "same_revision":
            return rig[1].latest(pid)
        rig[1].catat(state(10, tile=(4, 8), location="Cabin"))
        post = rig[1].latest(pid)
        assert post.revisi > after_revisi
        if surface == "post":
            if field == "stale":
                post = replace(post, diterima_pada=time.monotonic()-10)
                rig[1]._state[pid] = post
            else:
                post = replace(post, **{field: value})
        if surface in {"context", "both", "record"}:
            for key, rec in list(rig[2]._records.items()):
                if surface != "record":
                    rig[2]._records[key] = replace(rec, facts={**rec.facts, field: value})
                elif field == "missing":
                    del rig[2]._records[key]
                else:
                    rig[2]._records[key] = replace(rec, **{field: value})
        if surface in {"session", "both"}:
            rig[3][field] = value
            rig[5]()
        if surface == "binding":
            if field == "target":
                binding, _ = rig[0]._binding_view()
                monkeypatch.setattr(rig[0], "_binding_view", lambda: (binding, value))
            else:
                rig[1]._identitas[pid] = replace(rig[1].identitas(pid), **{field: value})
        return post
    monkeypatch.setattr(rig[1], "wait_next", readback)
    result = run(rig)
    ev = result.action_evidence
    assert result.applied and ev.lifecycle == "completed" and ev.outcome == "PressCompleted"
    assert ev.cleanup_confirmed
    assert ev.after is None and ev.world_effect_status == "uncertain"
    assert ev.observed_changes == ()
    assert rig[0].status == "PAUSED_RECOVERY"
    rig[0].note_event("tick")
    rig[0].process_pending_events()
    assert rig[4] == ["acquire", "input_once", "release"]
    assert rig[0].inbox_ops == len(rig[0]._provider.calls) == 1


@pytest.mark.parametrize("status", [command.TIDAK_PASTI, command.TERPUTUS])
def test_uncertain_interrupted_no_automatic_replay(rig, monkeypatch, status):
    def request(*a, **k):
        rig[4].append("input_once")
        return command.HasilPerintah(status, False, "req-1")
    monkeypatch.setattr(command, "minta_input_once", request)
    run(rig)
    rig[0].note_event("tick")
    rig[0].process_pending_events()
    assert rig[4] == ["acquire", "input_once", "release"]


def test_stop_during_action_uses_existing_latch_and_releases(rig, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    def request(*a, **k):
        rig[4].append("input_once")
        started.set()
        assert finish.wait(2)
        return command.HasilPerintah(command.TERPUTUS, False, "req-1")
    monkeypatch.setattr(command, "minta_input_once", request)
    worker = threading.Thread(target=run, args=(rig,))
    worker.start()
    assert started.wait(2)
    rig[0].stop()
    assert not (rig[6]/"stop.json").exists()  # fix10f: shutdown is not an owner hard stop; the lease lapses natively
    finish.set()
    worker.join(3)
    assert not worker.is_alive()
    assert rig[4] == ["acquire", "input_once", "release"]


@pytest.mark.parametrize("field,value", [("stop_latched", True), ("record_faulted", True),
    ("lease_until_unix_ms", 1), ("control_seq", 7), ("release_requested", True)])
def test_revoked_acquire_never_submits(rig, monkeypatch, field, value):
    original = command.tulis_kontrol
    def revoke(kind, **kw):
        original(kind, **kw)
        if kind == "acquire":
            rig[3][field] = value
            rig[5]()
    monkeypatch.setattr(command, "tulis_kontrol", revoke)
    run(rig)
    assert rig[4] == ["acquire", "release"]
    assert rig[0].inbox_ops == 0


@pytest.mark.parametrize("field,value", [("screen_id", 2), ("runtime_instance_id", "wrong"),
    ("session_id", "wrong"), ("location_id", "wrong"), ("context_generation", 0)])
def test_invalid_context_fact_zero_writes(rig, field, value):
    for key, rec in rig[2]._records.items():
        rig[2]._records[key] = replace(rec, facts={**rec.facts, field: value})
    run(rig)
    assert rig[4] == []


def test_pending_stop_and_control_files_are_not_overwritten(rig):
    for filename in ("stop.json", "control.json"):
        path = rig[6]/filename
        path.write_text("{malformed-but-owned", encoding="utf8")
        run(rig)
        assert path.read_text(encoding="utf8") == "{malformed-but-owned"
        assert rig[4] == []
        path.unlink()


def test_post_snapshot_from_during_command_is_not_post_return_evidence(rig, monkeypatch):
    original = command.minta_input_once
    def request(*a, **k):
        rig[1].catat(state(5, tile=(4,8)))
        return original(*a, **k)
    monkeypatch.setattr(command, "minta_input_once", request)
    result = run(rig)
    assert result.action_evidence.lifecycle == "completed"
    assert result.action_evidence.after is None


@pytest.mark.parametrize("bad", ["stale", "player", "screen", "session", "runtime", "location"])
def test_invalid_post_snapshot_is_uncertain(rig, monkeypatch, bad):
    def readback(pid, after_revisi, timeout):
        rig[1].catat(state(10, tile=(4,8)))
        post = rig[1].latest(pid)
        if bad == "stale":
            post = replace(post, diterima_pada=time.monotonic()-10)
            rig[1]._state[pid] = post
        if bad == "player":
            post = replace(post, player_id=999)
        if bad == "screen":
            post = replace(post, screen_id=2)
        if bad in {"session", "runtime", "location"}:
            rig[3][{"session":"session_id", "runtime":"runtime_instance_id", "location":"location_id"}[bad]] = "wrong"
            rig[5]()
        return post
    monkeypatch.setattr(rig[1], "wait_next", readback)
    assert run(rig).action_evidence.after is None


def test_readback_failure_clears_busy_and_pauses(rig, monkeypatch):
    def broken(*a):
        raise OSError("readback unavailable")
    monkeypatch.setattr(rig[1], "wait_next", broken)
    assert run(rig).action_evidence.world_effect_status == "uncertain"
    assert not rig[0]._action_busy
    assert rig[0].status == "PAUSED_RECOVERY"


@pytest.mark.parametrize("ticks", [1, 60])
@pytest.mark.parametrize("motor_location,location_id", [("Farm", "Farm"),
    ("Cabin", SYNTHETIC_LOCATION_ID)])
def test_actual_existing_client_writes_one_typed_request(rig, monkeypatch, ticks, motor_location, location_id):
    set_location_vocabularies(rig, motor_location, location_id)
    monkeypatch.setattr(command, "minta_input_once", REAL_INPUT_ONCE)
    observed = []
    errors = []
    def native():
        try:
            deadline = time.monotonic()+3
            request = rig[6]/"request.json"
            while not request.exists() and time.monotonic()<deadline:
                time.sleep(.005)
            raw = json.loads(request.read_text(encoding="utf8"))
            observed.append(raw)
            rig[4].append("input_once")
            request.unlink()
            result = dict(protocol=1, ev="command_result", op="input_once",
                request_id=raw["request_id"], session_id=raw["session_id"], accepted=True,
                status="completed", success=True, outcome="PressCompleted",
                direction=raw["args"]["direction"], press_count=raw["args"]["ticks"])
            temp = rig[6]/"fake-result.tmp"
            temp.write_text(json.dumps(result), encoding="utf8")
            temp.replace(rig[6]/"result.json")
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=native)
    worker.start()
    result = run(rig, lambda req: choice(req, args=dict(direction="right", ticks=ticks)))
    worker.join(3)
    assert not errors and not worker.is_alive()
    assert len(observed) == 1
    raw = observed[0]
    assert raw["session_id"] == "command-1"  # Different from observation-epoch-7.
    assert raw["runtime_instance_id"] == "runtime-1" and raw["context_generation"] == 4
    assert raw["family"] == "world_key" and raw["primitive"] == "move_cardinal"
    assert raw["expected_player_id"] == PID and raw["expected_screen_id"] == 0
    assert raw["expected_location_id"] == location_id
    assert raw["owner_id"].startswith("g1-owner-") and raw["generation"] > 0
    assert raw["control_seq"] == 1 and raw["args"] == dict(direction="right", ticks=ticks)
    assert rig[4] == ["acquire", "input_once", "release"]
    assert result.action_evidence.lifecycle == "completed"


def test_events_do_not_start_another_generation_during_action(rig, monkeypatch):
    original = command.minta_input_once
    def request(*a, **kw):
        rig[0].note_event("state")
        assert rig[0].process_pending_events() == 0
        return original(*a, **kw)
    monkeypatch.setattr(command, "minta_input_once", request)
    run(rig)
    assert len(rig[0]._provider.calls) == 1


def test_restart_paused_without_replay(rig):
    run(rig)
    rig[4].clear()
    old = rig[0]
    restarted = StardewGoalRuntime(config=old._config, provider=FakeDecisionProvider(choice),
        state_store=rig[1], observation_store=rig[2], journal_dir=old._journal_dir,
        start_listener=False, start_worker=False)
    restarted.start()
    assert restarted.status == "PAUSED_RECOVERY"
    assert restarted.process_pending_events() == 0
    assert rig[4] == []


@pytest.mark.parametrize("change", ["host", "split", "screen", "location", "ui_revision", "busy"])
def test_motor_binding_guard_zero_writes(rig, change):
    if change == "location":
        def changed(req):
            rig[1].catat(state(5, location="Town"))
            return choice(req)
        run(rig, changed)
        assert rig[4] == []
        return
    if change in {"host", "split"}:
        ident = rig[1].identitas(PID)
        field = "is_main_player" if change == "host" else "is_split_screen"
        rig[1]._identitas[PID] = replace(ident, **{field: True})
    else:
        fields = {"screen": {"screen_id": 4},
                  "ui_revision": {"ui_revision": 2}, "busy": {"can_player_move": False}}
        rig[1]._state[PID] = replace(rig[1].latest(PID), **fields[change])
    run(rig)
    assert rig[4] == []


@pytest.mark.parametrize("field,value", [("session_id", "wrong"), ("op", "world_press"),
    ("press_count", 9), ("direction", "left"), ("accepted", False)])
def test_mismatched_completion_is_uncertain(rig, monkeypatch, field, value):
    original = command.minta_input_once
    def request(*a, **kw):
        result = original(*a, **kw)
        return replace(result, mentah={**result.mentah, field: value})
    monkeypatch.setattr(command, "minta_input_once", request)
    assert run(rig).action_evidence.lifecycle == "uncertain"


@pytest.mark.parametrize("status,expected", [(command.DITOLAK,"rejected"),
    (command.TIDAK_DIAMBIL,"not_taken"), (command.GAGAL_LOKAL,"not_taken")])
def test_lifecycle_is_preserved_separately(rig, monkeypatch, status, expected):
    def request(*a, **kw):
        rig[4].append("input_once")
        return command.HasilPerintah(status, False, "req-1")
    monkeypatch.setattr(command, "minta_input_once", request)
    result = run(rig)
    assert result.action_evidence.lifecycle == expected
    assert result.action_evidence.after is None
    assert rig[4] == ["acquire", "input_once", "release"]


def test_stopped_owner_cleanup_ack_does_not_clear_native_stop(rig, monkeypatch):
    original = command.tulis_kontrol
    def control(kind, **kw):
        original(kind, **kw)
        if kind == "release":
            rig[3].update(owner_id=kw["owner_id"], generation=kw["generation"],
                          control_seq=2, stop_latched=True, release_acked=True,
                          release_requested=False)
            rig[5]()
    monkeypatch.setattr(command, "tulis_kontrol", control)
    result = run(rig)
    assert result.action_evidence.cleanup_confirmed
    assert rig[3]["stop_latched"] is True


def test_stop_after_completion_does_not_restart_action(rig):
    run(rig)
    rig[0].stop()
    rig[4].clear()
    assert not run(rig).admitted
    assert rig[4] == []


def test_provider_snapshot_cannot_mutate_action_working_memory(rig):
    run(rig)
    goal = rig[0].active_goal
    snapshot = rig[0]._build_decision_snapshot(goal)
    snapshot.working_memory.last_action.before["tile"][0] = 999
    assert goal.last_action.before["tile"] == [3, 8]


def test_older_generation_stop_requires_operator_token_rearm(rig):
    """Generation changes cannot discharge persisted HALT or legacy stop levels."""
    rt, motor, obs, raw, calls, publish, folder = rig
    raw.update(stop_latched=True, stop_generation=1, release_acked=True, release_requested=False, owner_id=None)
    publish()
    result = run(rig)
    assert result.action_evidence is None or result.action_evidence.lifecycle != "completed"
    assert calls == []
    assert raw["stop_latched"] is True


@pytest.mark.parametrize("field,value", [("release_acked", False), ("release_requested", True),
                                         ("owner_id", "someone-else"), ("stop_generation", None)])
def test_stop_latch_that_is_not_stale_still_blocks(rig, field, value):
    rt, motor, obs, raw, calls, publish, folder = rig
    raw.update(stop_latched=True, stop_generation=1, release_acked=True, release_requested=False, owner_id=None)
    raw[field] = value
    publish()
    result = run(rig)
    assert result.action_evidence is None or result.action_evidence.lifecycle != "completed"
    assert "input_once" not in calls
