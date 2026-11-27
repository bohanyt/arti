"""FIXB59: native reviewed event offers, original request identity, leased A and one-egg budgets."""
from dataclasses import replace
import json
import time

import pytest
import arti_stardew_command as command
from arti_stardew_event_control import action_offer, movement_offer, OUTCOMES
from tests import test_stardew_fixb53 as event
from tests import test_stardew_runtime_move as moves

rig = moves.rig


def native_target(kind="event_target"):
    return dict(kind=kind, target_token="a" * 64, source_fact_id="event:" + kind + ":ei1:53,8",
                tile_x=53, tile_y=8, actor_tile_x=52, actor_tile_y=8, facing=1)


def effect_pair(kind):
    kind = kind[:-6] if kind in command.EVENT_ROUTE_KINDS else kind
    egg = "egg" in kind
    before = dict(kind="festival_egg" if kind == "festival_egg_route" else kind, target_id="ei1", scope_valid=True,
        sequence="eggHunt" if egg else "haleyBeach" if kind == "event_target" else "jellies", control=True,
        command=3, target_tile=[53, 8], timer_positive=egg, score=2, present=True, expected_menu=False,
        menu_type=None, sitting=False)
    after = dict(before)
    if kind == "event_target": after.update(sequence="haleyBeach2", target_tile=[35, 11])
    elif kind in ("festival_npc", "event_interact"): after.update(expected_menu=True, menu_type="DialogueBox")
    elif kind == "festival_shop": after.update(expected_menu=True, menu_type="ShopMenu")
    elif kind == "festival_seat": after.update(sitting=True)
    elif kind == "festival_stand": before["sitting"] = True  # seated, then standing
    else: after.update(score=3, present=False)
    return before, after


def publish(rig, facts, revision=None):
    obs = rig[2]
    old = obs.get(moves.PID, "ui.context")
    assert obs.apply(dict(ev="observation", seq=old.seq + 1, player_id=moves.PID, screen_id=0, payload=dict(
        session_id=old.session_id, product="ui", domain="ui.context", kind="snapshot",
        domain_revision=revision or old.domain_revision + 1, provenance="LOCAL_AUTHORITATIVE", authoritative=True,
        available=True, error=None, truncation=dict(truncated=False, observed_count=len(facts), sent_count=len(facts)),
        body=dict(facts=facts)))).accepted


def stage(rig, kind="event_target"):
    event.stage(rig)
    rig[3]["world_interaction_enabled"] = True
    rig[5]()
    old = rig[2].get(moves.PID, "ui.context")
    movement = dict(old.facts["event_movement"], is_festival=kind not in ("event_target", "event_target_route"))
    if kind not in ("event_target", "event_target_route"):
        egg = kind in ("festival_egg", "festival_egg_route")
        movement.update(sequence="eggHunt" if egg else "jellies", festival_id="festival_spring13" if egg else "festival_summer28",
                        main_event_running=egg, timer_positive=egg)
    facts = dict(old.facts, event_movement=movement, event_actions=dict(event_token=event.TOKEN,
        sequence=movement["sequence"], target_tile=[53, 8], targets=[native_target(kind)], route_inputs={native_target(kind)["source_fact_id"]: 12}))
    publish(rig, facts)
    return facts


@pytest.mark.parametrize("kind", sorted(OUTCOMES))
def test_stardew_event_native_offer_exact_kind(kind):
    move = dict(available=True, event_token=event.TOKEN, sequence="haleyBeach", max_ticks=4, command_index=3, is_festival=False)
    if kind not in ("event_target", "event_target_route"):
        egg = "egg" in kind
        move.update(sequence="eggHunt" if egg else "jellies", is_festival=True,
                    festival_id="festival_spring13" if egg else "festival_summer28", main_event_running=egg, timer_positive=egg)
    facts = dict(event_movement=move, event_actions=dict(event_token=event.TOKEN, sequence=move["sequence"], target_tile=[native_target(kind)["tile_x"], native_target(kind)["tile_y"]],
        targets=[native_target(kind)], route_inputs={native_target(kind)["source_fact_id"]: 12}))
    assert action_offer(facts)["targets"]["e1"] == native_target(kind)
    facts["event_actions"]["targets"][0]["kind"] = "festival_unknown"
    assert action_offer(facts) is None


@pytest.mark.parametrize("sequence", ["haleyBeach", "haleyBeach2"])
def test_stardew_two_reviewed_sequences(sequence):
    assert movement_offer(dict(event_movement=dict(available=True, event_token=event.TOKEN,
        sequence=sequence, max_ticks=4, command_index=3)))


@pytest.mark.parametrize("field,value", [("timer_positive", False), ("main_event_running", False),
    ("festival_id", "festival_winter8"), ("festival_id", []), ("sequence", "iceFishing"), ("sequence", []),
    ("is_festival", False), ("event_token", "ecm:bad")])
def test_stardew_timed_admission_fails_closed(field, value):
    block = dict(available=True, event_token=event.TOKEN, sequence="eggHunt", max_ticks=4,
                 command_index=3, is_festival=True, festival_id="festival_spring13", main_event_running=True, timer_positive=True)
    block[field] = value
    assert movement_offer(dict(event_movement=block)) is None


def choice(req):
    assert req.snapshot.event_actions
    return moves.choice(req, operation="event_action", args={"handle": "e1", "btn": "A"})


@pytest.mark.parametrize("kind", sorted(OUTCOMES))
def test_stardew_real_leased_event_action_and_native_receipt(rig, monkeypatch, kind):
    facts = stage(rig, kind)
    def invoke(**kw):
        native = {k: kw[k] for k in native_target(kind)}
        rig[4].append("event_A")
        kw["guarded_publish"](lambda: None)
        before, after = effect_pair(kind)
        publish(rig, dict(facts, event_action_receipt=dict(request_id="event1", target_token=native["target_token"],
            source_fact_id=native["source_fact_id"], outcome=OUTCOMES[kind], verified=True, before=before, after=after,
            trace=[dict(kind="A", verified=True, cleanup=True)])))
        return command.HasilPerintah(command.SELESAI, True, "event1", OUTCOMES[kind], mentah=dict(
            primitive="world_interact", input_args=native, cleanup_confirmed=True, request_id="event1",
            accepted=True, session_id="command-1", runtime_instance_id="runtime-1", player_id=moves.PID,
            screen_id=0, context_generation=4, location_id="Farm", success=True, outcome=OUTCOMES[kind],
            input_settled=True, press_count=1))
    monkeypatch.setattr(command, "minta_world_interaction", invoke)
    result = moves.run(rig, choice)
    assert result.action_evidence is not None, result
    assert result.applied and result.action_evidence.world_effect_status == "observed_event_effect", result
    assert result.action_evidence.cleanup_confirmed
    assert rig[4] == ["acquire", "event_A", "release"]
    assert rig[0]._physical_action_publications == (12 if kind in command.APPROACH_ROUTE_KINDS else 40 if kind == "festival_egg_route" else 1)


@pytest.mark.parametrize("fault", ["token", "instance", "stale", "partial", "moving", "timer"])
def test_stardew_original_event_offer_drift_sends_no_input(rig, monkeypatch, fault):
    stage(rig, "festival_egg_route")
    monkeypatch.setattr(command, "minta_world_interaction", lambda **_: pytest.fail("stale input"))
    def changed(req):
        answer = choice(req)
        for key, rec in rig[2]._records.items():
            facts = dict(rec.facts)
            if fault == "token": facts["event_movement"] = dict(facts["event_movement"], event_token="ecm:" + "b" * 64)
            if fault == "instance": facts["runtime_instance_id"] = "replacement"
            if fault == "timer": facts["event_movement"] = dict(facts["event_movement"], timer_positive=False)
            if fault == "moving":
                facts["event_actions"] = dict(facts["event_actions"], targets=[dict(native_target("festival_egg_route"), tile_x=54)])
            rig[2]._records[key] = replace(rec, facts=facts, **({"received_monotonic": 0} if fault == "stale" else
                                                             {"is_complete": False} if fault == "partial" else {}))
        return answer
    result = moves.run(rig, changed)
    assert "event_A" not in rig[4] and not result.applied


def test_stardew_route_reserves_budget_before_wire(rig, monkeypatch):
    stage(rig, "festival_egg_route")
    rig[0]._action_session_limit = 39
    sent = []
    def invoke(**kw):
        kw["guarded_publish"](lambda: sent.append(True))
        pytest.fail("budget must refuse before wire")
    monkeypatch.setattr(command, "minta_world_interaction", invoke)
    result = moves.run(rig, choice)
    assert not sent and not result.applied


def test_stardew_provider_publishes_short_A_offer_without_native_pins(rig):
    from arti_stardew_runtime import ProductionDecisionProvider, stardew_decision_provider_admission
    from tests.test_stardew_decision_provider import BASE
    stage(rig, "festival_egg_route")
    with rig[0]._lock:
        req = rig[0]._begin_generation(text="collect one egg", trigger_type="ptt", viewer_name=None,
            reason="owner_turn", replace_goal=True)
    policy, _ = stardew_decision_provider_admission(BASE)
    provider = ProductionDecisionProvider(policy, session=object(), text_chat_fn=lambda *a, **k: ("", 0))
    payload = json.loads(provider._render_messages(req)[1]["content"])
    offered = payload["supported_actions"]["event_action"]
    assert offered["example"]["args"] == {"handle": "e1", "btn": "A"}
    assert offered["targets"][0]["reserved_inputs"] == 40
    assert "identity" not in json.dumps(offered) and "target_token" not in json.dumps(offered)


@pytest.mark.parametrize("kind", sorted(kind for kind in OUTCOMES if not kind.startswith("event_interact")))
@pytest.mark.parametrize("fault", ["target_id", "scope_valid", "command", "sequence"])
def test_stardew_effect_refuses_identity_phase_and_moving_target(kind, fault):
    from arti_stardew_event_control import effect_valid
    before, after = effect_pair(kind)
    assert effect_valid(kind, before, after, "ei1")
    after[fault] = {"target_id": "replacement", "scope_valid": False, "command": 9, "sequence": "unknown"}[fault]
    assert not effect_valid(kind, before, after, "ei1")


@pytest.mark.parametrize("kind", ["event_interact", "event_interact_route"])
def test_generic_event_action_requires_a_same_target_native_answer(kind):
    from arti_stardew_event_control import effect_valid
    before, after = effect_pair(kind)
    assert effect_valid(kind, before, after, "ei1")
    assert not effect_valid(kind, before, dict(after, target_id="replacement"), "ei1")
    assert not effect_valid(kind, before, dict(after, scope_valid=False), "ei1")
    assert not effect_valid(kind, before, dict(before, sitting=True), "ei1")
    assert effect_valid(kind, before, dict(before, command=4), "ei1")


@pytest.mark.parametrize("field,value", [("present", True), ("score", 2), ("score", 4), ("score", True),
    ("timer_positive", False), ("menu_type", "DialogueBox")])
def test_stardew_egg_requires_exact_prop_and_point_with_live_timer(field, value):
    from arti_stardew_event_control import effect_valid
    before, after = effect_pair("festival_egg_route")
    after[field] = value
    assert not effect_valid("festival_egg_route", before, after, "ei1")
