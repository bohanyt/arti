"""Native event movement is separate from ordinary world authority (offline doubles)."""
from dataclasses import replace
import json
import time

import pytest
import arti_stardew_command as command
from arti_stardew_event_control import movement_offer, displacement
from tests import test_stardew_runtime_move as moves

rig = moves.rig
TOKEN = "ecm:" + "a" * 64


def stage(rig):
    rt, motor, obs = rig[:3]
    event = moves.state(4)
    event["payload"].update(is_player_free=False, can_player_move=False)
    motor.catat(event)
    rec = obs.get(moves.PID, "ui.context")
    facts = dict(rec.facts, is_player_free=False, can_player_move=False,
        event_movement={"available": True, "event_token": TOKEN, "max_ticks": 4,
                        "sequence": "haleyBeach", "command_index": 3})
    assert obs.apply(dict(ev="observation", seq=5, player_id=moves.PID, screen_id=0, payload=dict(
        session_id=rec.session_id, product="ui", domain="ui.context", kind="snapshot",
        domain_revision=2, provenance="LOCAL_AUTHORITATIVE", authoritative=True,
        available=True, error=None, truncation=dict(truncated=False, observed_count=len(facts),
        sent_count=len(facts)), body=dict(facts=facts)))).accepted



def event_choice(req):
    assert req.snapshot.movement_context is not None
    assert req.snapshot.water_context is None  # the movement exception never unlocks ordinary world tools
    assert req.snapshot.motor.is_player_free is False
    return moves.choice(req, args={"direction": "right", "ticks": 4})


@pytest.mark.parametrize("dx,dy,observed", [
    (16, 0, True), (0, 5, False), (3.99, 5, False), (4, 8, True), (4, 9, False), (15.84, -1.25, True),
])
def test_event_move_uses_real_leased_move_with_native_token_and_independent_pixels(rig, monkeypatch, dx, dy, observed):
    stage(rig)
    def request(direction, ticks, **kw):
        assert kw["event_token"] == TOKEN and ticks == 4
        rig[4].append("input_once")
        kw["guarded_publish"](lambda: None)
        return command.HasilPerintah(command.SELESAI, True, "req-1", "PressCompleted", mentah={
            "session_id": "command-1", "op": "input_once", "request_id": "req-1", "accepted": True,
            "direction": direction, "press_count": ticks})
    def post(pid, revision, timeout):
        event = moves.state(revision + 1)
        event["payload"].update(is_player_free=False, can_player_move=False)
        event["payload"]["pixel"]["x"] += dx
        event["payload"]["pixel"]["y"] += dy
        rig[1].catat(event)
        return rig[1].latest(pid)
    monkeypatch.setattr(command, "minta_input_once", request)
    monkeypatch.setattr(rig[1], "wait_next", post)
    result = moves.run(rig, event_choice)
    assert result.action_evidence is not None, result
    assert result.action_evidence.lifecycle == "completed", result.action_evidence
    assert result.action_evidence.world_effect_status == ("observed_change" if observed else "uncertain")
    assert result.action_evidence.cleanup_confirmed
    assert rig[4] == ["acquire", "input_once", "release"]


@pytest.mark.parametrize("fault", ["token", "control", "stale", "partial"])
def test_event_offer_disappearing_during_model_turn_causes_zero_mutation(rig, fault):
    stage(rig)
    def changed(req):
        answer = event_choice(req)
        for key, rec in rig[2]._records.items():
            facts = dict(rec.facts)
            if fault == "token":
                facts["event_movement"] = dict(facts["event_movement"], event_token="ecm:" + "b" * 64)
            elif fault == "control":
                facts.pop("event_movement")
            rec = replace(rec, facts=facts, **({"received_monotonic": 0} if fault == "stale" else
                                             {"is_complete": False} if fault == "partial" else {}))
            rig[2]._records[key] = rec
        return answer
    result = moves.run(rig, changed)
    assert "input_once" not in rig[4]
    assert result.action_evidence is None or result.action_evidence.lifecycle != "completed"


# ADR 97: require 1 px forward per tick and retain the native corner slide (a45 J1226).
@pytest.mark.parametrize("dx,dy,ok", [(16, 0, True), (0, 0, False), (-1, 0, False), (1, 1, False), (1, 9, False), (65, 0, False)])
def test_event_motor_displacement_proof(dx, dy, ok):
    assert displacement("right", 4, {"pixel": [0, 0]}, {"pixel": [dx, dy]}) is ok


@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
@pytest.mark.parametrize("ticks,along,across,ok", [
    (4, 0, 5, False), (4, 0, 8, False), (4, 3.99, 0, False), (4, 4, 0, True),
    (4, 3.99, 8, False), (4, 4, 8, True), (4, 4, 9, False),
    (1, 0, 2, False), (1, .99, 0, False), (1, 1, 2, True), (1, 1, 2.01, False),
    (4, 56, 8, True), (4, 56.01, 8, False), (4, 40, 40, False),
])
def test_event_displacement_boundaries_on_every_axis(direction, ticks, along, across, ok):
    dx, dy = {"up": (across, -along), "down": (across, along),
              "left": (-along, across), "right": (along, across)}[direction]
    assert displacement(direction, ticks, {"pixel": [0, 0]}, {"pixel": [dx, dy]}) is ok


@pytest.mark.parametrize("ticks,dx,dy,max_ticks,ok", [
    (4, 16, 0, 4, True), (4, 4, 0, 4, True), (4, 3.99, 0, 4, False),
    (4, 16, 1, 4, False), (4, 0, 5, 4, False),
    (40, 160, 0, 40, True), (40, 160, 1, 40, False), (40, 160, 0, 4, False),
])
def test_reviewed_route_displacement_is_strict(ticks, dx, dy, max_ticks, ok):
    assert displacement("right", ticks, {"pixel": [0, 0]}, {"pixel": [dx, dy]},
                        max_ticks=max_ticks, slide_px_per_tick=0) is ok


@pytest.mark.parametrize("field,value", [("event_token", "ecm:bad"), ("max_ticks", 5), ("max_ticks", True),
                                        ("available", False), ("sequence", "eggHunt"), ("command_index", -1)])
def test_event_offer_needs_native_pin_and_exact_bounded_scope(field, value):
    offer = {"available": True, "event_token": TOKEN, "max_ticks": 4, "sequence": "haleyBeach", "command_index": 3}
    offer[field] = value
    assert movement_offer({"event_movement": offer}) is None


@pytest.mark.parametrize("token,ticks", [("ecm:bad", 4), (TOKEN, 5), (TOKEN, True)])
def test_command_rejects_malformed_event_scope_before_any_io(tmp_path, token, ticks):
    with pytest.raises(command.KesalahanPerintah, match="invalid_event_movement"):
        command.minta_input_once("right", ticks, event_token=token, folder=tmp_path,
            owner_id="owner", generation=1, control_seq=1, context_generation=1)
    assert not list(tmp_path.iterdir())


def test_provider_offers_stick_with_event_cap_but_no_ordinary_world_tools(rig):
    from arti_stardew_runtime import ProductionDecisionProvider, stardew_decision_provider_admission
    from tests.test_stardew_decision_provider import BASE
    stage(rig)
    with rig[0]._lock:
        request = rig[0]._begin_generation(text="move left during this event", trigger_type="ptt", viewer_name=None,
                                         reason="owner_turn", replace_goal=True)
    policy, _ = stardew_decision_provider_admission(BASE)
    provider = ProductionDecisionProvider(policy, session=object(), text_chat_fn=lambda *a, **k: ("", 0))
    payload = json.loads(provider._render_messages(request)[1]["content"])
    actions = payload["supported_actions"]
    assert actions["world_key.move_cardinal"]["args"]["ticks"]["maximum"] == 4
    assert not {"water_once", "world_key.use_tool", "world_interact", "approach_target"}.intersection(actions)


def test_real_command_sender_preserves_event_token_and_journal_identity(rig, monkeypatch):
    raw, folder = rig[3], rig[6]
    raw.update(owner_id="owner", generation=1, control_seq=1, lease_until_unix_ms=int(time.time() * 1000) + 1500)
    rig[5]()
    monkeypatch.setattr(command, "_tunggu_hasil", lambda *a: "sent")
    assert moves.REAL_INPUT_ONCE("left", 4, event_token=TOKEN, folder=folder, owner_id="owner", generation=1,
        control_seq=1, context_generation=4) == "sent"
    payload = json.loads((folder / command.NAMA_REQUEST).read_text(encoding="utf-8"))
    assert payload["args"] == {"direction": "left", "ticks": 4, "event_token": TOKEN}
    digest = command._journal_payload_digest(payload)
    payload["args"]["event_token"] = "ecm:" + "b" * 64
    assert command._journal_payload_digest(payload) != digest
    payload["args"].pop("event_token")
    assert command._journal_payload_digest(payload) != digest
