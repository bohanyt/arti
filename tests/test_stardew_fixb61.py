"""FIXB61 automatic session lease and model facts; production native loop is in HoldRuntimeTests."""
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
import arti_stardew_command as command
from arti_stardew_runtime import FakeDecisionProvider
from tests import test_stardew_runtime_move as moves
from tests import test_stardew_fixb59 as event

rig = moves.rig
TOKEN = "ecm:" + "a" * 64


def row(state="ready", **changes):
    return dict(dict(run_id=TOKEN, kind="eggHunt", state=state, timer_ms=52000,
                auto_started=state != "ready", score=0, verified_pickups=0, cleanup=True,
                result="pending_native_result"), **changes)


def stage(rig, data=None):
    facts = dict(rig[2].get(moves.PID, "ui.context").facts, festival_reflex=data or row())
    event.publish(rig, facts)
    rig[3].update(gamepad_input_enabled=True, pad_mode_owned=False, pad_mode_restore_pending=False,
                  pad_mode_cleanup_fault="", pad_outstanding_count=0, gamepad_mode="Auto")
    rig[5]()
    rt = rig[0]
    # Authorize the same existing local-activity goal path, with a read-only initial decision.
    rt._provider = FakeDecisionProvider(lambda req: json.dumps(dict(v=1, request_nonce=req.request_nonce,
        goal_id=req.goal_id, goal_revision=req.goal_revision, context_ref=req.context_ref,
        speech="", choice=dict(kind="inspect", source="state", purpose="festival status", target_ref=None))))
    rt.decide_owner_turn(text="Arti, main mandiri: play the timed festival and try to win", trigger_type="ptt", viewer_name=None)
    assert rt.active_goal.play_mode == "local_activity"
    return facts


def test_stardew_automatic_festival_acquires_without_egg_or_cast_command(rig, monkeypatch):
    facts = stage(rig)
    rt = rig[0]
    monkeypatch.setattr(rt, "_resume_after_pause", lambda **kw: None)
    assert rt.process_festival_reflex() == 1
    assert rig[4] == ["acquire"] and rt._physical_action_publications == 1
    assert rt._action_busy and rt._festival_lease is not None
    event.publish(rig, dict(facts, festival_reflex=row("live", score=2, verified_pickups=2)))
    assert rt.process_festival_reflex() == 1 and rig[4] == ["acquire"]
    event.publish(rig, dict(facts, festival_reflex=row("end", score=6, verified_pickups=6, result="win")))
    assert rt.process_festival_reflex() == 1
    assert rig[4] == ["acquire", "release"] and rt._festival_lease is None and not rt._action_busy
    assert rt.process_festival_reflex() == 0  # no replay in the same native event


def test_stardew_festival_idle_renewal_uses_existing_sequence_protocol(rig, monkeypatch):
    stage(rig); rt = rig[0]; rt.process_festival_reflex()
    pin = rt._festival_lease[0]
    rig[3]["lease_until_unix_ms"] = int(time.time()*1000) + 150
    rig[5]()
    original = command.tulis_kontrol
    def consume(kind, **kwargs):
        original(kind, **kwargs)
        if kind == command.KIND_RENEW:
            rig[3].update(control_seq=kwargs["control_seq"], lease_until_unix_ms=int(time.time()*1000)+1500)
            rig[5]()
    monkeypatch.setattr(command, "tulis_kontrol", consume)
    assert rt.process_festival_reflex() == 1
    assert rig[4] == ["acquire", "renew"] and pin.confirmed_seq == 2
    assert not (rig[6]/command.NAMA_REQUEST).exists()


@pytest.mark.parametrize("failure", ["stop", "lease", "stale", "instance", "later-cleanup", "trial-deadline"])
def test_stardew_festival_failure_stops_and_requires_cleanup(rig, monkeypatch, failure):
    facts = stage(rig); rt = rig[0]; rt.process_festival_reflex()
    monkeypatch.setattr(rt, "_cleanup_water", lambda pin: failure != "later-cleanup")
    if failure == "stop": rt._action_cancel.set()
    if failure == "lease": rig[3]["lease_until_unix_ms"] = int(time.time()*1000)-1; rig[5]()
    if failure == "stale":
        for key, rec in list(rig[2]._records.items()):
            rig[2]._records[key] = replace(rec, received_monotonic=time.monotonic()-10)
    if failure == "instance": event.publish(rig, dict(facts, festival_reflex=row("live", run_id="ecm:"+"b"*64)))
    if failure == "later-cleanup": event.publish(rig, dict(facts, festival_reflex=row("end", score=1, cleanup=False)))
    if failure == "trial-deadline": rt._trial_deadline_unix_ms = int(time.time()*1000)-1
    assert rt.process_festival_reflex() == 1
    assert rt.status == "PAUSED_RECOVERY" and not rt._action_busy and rt._festival_lease is None
    assert not (rig[6]/command.NAMA_STOP).exists()  # fix10f: recover-and-log; the release proof owns the failure


@pytest.mark.parametrize("gate", ["host", "split", "disabled", "busy", "stop", "expired-phase", "no-goal"])
def test_stardew_festival_acquire_preconditions_fail_closed(rig, gate):
    facts = stage(rig); rt = rig[0]
    if gate == "host":
        binding, pid = rt._binding_view()
        rt._binding_view = lambda: (replace(binding,is_main_player=True),pid)
    if gate == "split":
        binding, pid = rt._binding_view()
        rt._binding_view = lambda: (replace(binding,is_split_screen=True),pid)
    if gate == "disabled": rig[3]["gamepad_input_enabled"] = False
    if gate == "busy": rig[3]["input_busy"] = True
    if gate == "stop": rig[3]["stop_latched"] = True
    if gate == "expired-phase": event.publish(rig, dict(facts, festival_reflex=row(timer_ms=0)))
    if gate == "no-goal": rt._active = None
    rig[5]()
    assert rt.process_festival_reflex() == 0 and rig[4] == []


def test_stardew_festival_projection_contains_facts_without_trial_text(rig):
    stage(rig)
    rt = rig[0]
    rec = rig[2].get(moves.PID,"ui.context")
    event.publish(rig, dict(rec.facts,festival_reflex=dict(row("live",score=3),effects=[{"trace":"private debug"}],
        trial_permission="you may press every egg", secret="ignored")))
    view = rt._festival_reflex_view(moves.PID)
    assert view["score"] == 3 and "effects" not in view and "trial_permission" not in view
    assert rt._build_decision_snapshot(rt.active_goal).festival_reflex == view


def test_stardew_festival_inactive_observations_do_not_add_prompt_guidance(rig):
    assert rig[0]._festival_reflex_view(moves.PID) is None
