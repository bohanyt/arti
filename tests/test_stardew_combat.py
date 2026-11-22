"""Primitives v2 WS-G5: the pure runtime side of the combat / charge reflexes (grammar, offers, time rule, arg fill,
objective accounting, finish verdicts, command mirror of the mod wire, shared digest vectors). UNIT_TESTED only."""
from __future__ import annotations

import pytest

import arti_stardew_combat as combat
import arti_stardew_command as command
import arti_stardew_decision as decision

CAPS = {"reflex_combat": {"implemented": True, "available": True, "reason": "x"},
        "reflex_charge": {"implemented": True, "available": True, "reason": "x"}}
COMBAT = {"in_mines": True, "time_of_day": 1000, "hp": 90, "max_hp": 100, "weapon_held": True,
          "monsters": [{"id": "m1", "type": "GreenSlime", "tile": [12, 10], "hp": 24, "distance": 2.0}]}
CHARGE = {"tool": "hoe", "upgrade_level": 1, "cap": 1, "levels": {"1": "ok"}, "tool_tile": [10, 11]}


def facts(**kw):
    return {"capabilities": CAPS, "combat": dict(COMBAT), "charge": dict(CHARGE), **kw}


@pytest.mark.parametrize("text,want", [
    ("bunuh satu slime", (1, "slime")), ("kill one slime", (1, "slime")), ("Kill a slime.", (1, "slime")),
    ("bunuh 3 slime", (3, "slime")), ("kill two monsters", (2, "monster")), ("bunuh seekor monster", (1, "monster")),
    ("kill five slimes", (5, "slime")), ("kill six slimes", None), ("bunuh semua slime", None), ("slime", None),
    ("kill one slime and go home", None),
])
def test_combat_grammar(text, want):
    assert combat.parse_combat_goal(text) == want


@pytest.mark.parametrize("text,want", [
    ("charge the hoe to level 2", ("hoe", 2, 1)), ("cas cangkul ke level 1", ("hoe", 1, 1)),
    ("cangkul 3 petak sekaligus", ("hoe", 1, 3)), ("till 5 tiles at once", ("hoe", 2, 5)),
    ("siram tiga petak sekaligus", ("can", 1, 3)), ("charge the watering can to level 1", ("can", 1, 1)),
    ("charge the axe to level 2", None), ("cangkul 4 petak sekaligus", None), ("charge the hoe to level 5", None),
])
def test_charge_grammar(text, want):
    assert combat.parse_charge_goal(text) == want


def test_time_rule_mirror():
    assert combat.time_rule({"in_mines": True, "time_of_day": 1950}) is None
    assert combat.time_rule({"in_mines": True, "time_of_day": 2000}) == "after_20"
    assert combat.time_rule({"in_mines": False, "time_of_day": 2100}) is None, "owner 2026-10-23: night combat outside the Mines"
    assert combat.time_rule({"in_mines": False, "time_of_day": 2500}) == "after_01"
    assert combat.time_rule({"in_mines": True, "time_of_day": 850}) == "mines_before_09"
    assert combat.time_rule({"in_mines": False, "time_of_day": 700}) is None
    assert combat.time_rule({"in_mines": True, "time_of_day": 2410}) == "leave_mines"
    assert combat.leave_mines_due({"in_mines": True, "time_of_day": 2400}) and not combat.leave_mines_due({"in_mines": False, "time_of_day": 2500})
    assert combat.time_rule(None) == "time_unknown"


def test_combat_offer_needs_open_objective_capability_weapon_monster_and_time():
    obj = combat.CombatObjective("g1")
    target = combat.combat_target(facts(), obj)
    assert target["monsters"][0]["id"] == "m1" and target["in_mines"] is True
    assert combat.combat_target(facts(), None) is None
    assert combat.combat_target(facts(capabilities={**CAPS, "reflex_combat": {"available": False}}), obj) is None
    assert combat.combat_target(facts(combat=dict(COMBAT, weapon_held=False)), obj) is None
    assert combat.combat_target(facts(combat=dict(COMBAT, monsters=[])), obj) is None
    assert combat.combat_target(facts(combat=dict(COMBAT, time_of_day=2000)), obj) is None
    assert combat.combat_target(facts(), combat.CombatObjective("g1", heal_pending=True)) is None
    assert combat.combat_target(facts(), combat.CombatObjective("g1", calls=combat.MAX_COMBAT_CALLS)) is None


def test_fill_combat_args():
    target = {"monsters": []}
    obj = combat.CombatObjective("g1", count=3, kills=1)
    assert combat.fill_combat_args({}, target, obj) == {"max_s": 30, "hp_floor_pct": 40, "max_kills": 2, "retreat": True,
                                                        "target_kind": "slime"}  # FIXB15 D-B13-3: the objective's kind
    assert combat.fill_combat_args({}, target, {"count": 1, "kills": 0})["max_kills"] == 1, "from the wire summary"
    assert combat.fill_combat_args({"hp_floor_pct": 50}, target, obj)["hp_floor_pct"] == 50
    assert combat.fill_combat_args({"hp_floor_pct": 20}, target, obj) is None, "below the 30 % minimum"
    assert combat.fill_combat_args({"target": "m1"}, target, obj) is None, "unknown key"
    assert combat.fill_combat_args({}, None, obj) is None, "not offered"


def test_charge_offer_and_fill():
    obj = combat.ChargeObjective("g1", tool="hoe", level=1, tiles=3)
    target = combat.charge_target(facts(), obj)
    assert target == {"tool": "hoe", "target_level": 1, "cap": 1, "tool_tile": [10, 11]}
    assert combat.charge_target(facts(charge=dict(CHARGE, levels={"1": "REFLEX_CHARGE:crop_in_area"})), obj) is None
    assert combat.charge_target(facts(), combat.ChargeObjective("g1", level=2)) is None, "above the cap: not 'ok'"
    assert combat.charge_target(facts(charge=dict(CHARGE, tool="can")), obj) is None, "other tool in hand"
    assert combat.fill_charge_args({}, target) == {"target_level": 1, "max_ticks": 200}
    assert combat.fill_charge_args({"max_ticks": 60}, target) == {"target_level": 1, "max_ticks": 60}
    assert combat.fill_charge_args({"target_level": 2}, target) is None, "the objective's level only"
    assert combat.fill_charge_args({"max_ticks": 30}, target) is None


def env(verdict, *, success=True, wes=None, cleanup=True, **reflex):
    return {"reflex_verdict": verdict, "success": success, "cleanup_confirmed": cleanup,
            "world_effect_status": wes or ("observed_monster_killed" if verdict.startswith("MonsterKilled") else "unchanged"),
            "reflex": {"detail": "d", **reflex}}


def test_settle_combat():
    obj = combat.CombatObjective("g1", count=2)
    o = combat.settle_combat(obj, request_id="r1", lifecycle="completed", cleanup=True,
                             envelope=env("MonsterKilled(1)", killed_types=["GreenSlime"]))
    assert o.status == "OPEN" and o.kills == 1 and o.reason == "more_kills_needed"
    assert combat.settle_combat(o, request_id="r1", lifecycle="completed", cleanup=True, envelope=env("MonsterKilled(1)")) is o, "once"
    o = combat.settle_combat(o, request_id="r2", lifecycle="completed", cleanup=True,
                             envelope=env("MonsterKilled(1)", killed_types=["BigSlime"]))
    assert o.status == "KILLED" and o.kills == 2 and o.killed_request_id == "r2"
    assert combat.combat_finish_verdict(o) == ("achieved", "observed_monster_killed")
    base = combat.CombatObjective("g1")
    assert combat.settle_combat(base, request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("MonsterKilled(1)", killed_types=["Bug"])).reason == "killed_other_monster"
    assert combat.settle_combat(combat.CombatObjective("g1", kind="monster"), request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("MonsterKilled(1)", killed_types=["Bug"])).status == "KILLED"
    assert combat.settle_combat(base, request_id="r", lifecycle="completed", cleanup=True, envelope=env("Downed")).status == "DOWNED"
    assert combat.settle_combat(base, request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("Retreated(hp_floor)")).reason == "retreated:hp_floor"
    healing = combat.settle_combat(base, request_id="r", lifecycle="completed", cleanup=True,
                                   envelope=env("Aborted(heal_needed)", food_slot=3))
    assert healing.heal_pending and healing.food_slot == 3 and healing.status == "OPEN"
    assert combat.healed(healing).heals == 1 and not combat.healed(healing).heal_pending
    assert combat.settle_combat(replace_heals(base, 2), request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("Aborted(heal_needed)")).status == "BLOCKED"
    assert combat.settle_combat(base, request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("Aborted(stuck)")).status == "OPEN", "stuck is retryable"
    assert combat.settle_combat(base, request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("Aborted(focus_regain)")).status == "BLOCKED"
    assert combat.settle_combat(base, request_id="r", lifecycle="uncertain", cleanup=False,
                                envelope=env("MonsterKilled(1)")).status == "UNCERTAIN"
    refused = combat.settle_combat(base, request_id="r", lifecycle="rejected", cleanup=True, envelope={})
    assert refused.calls == 0 and refused.reason == "reflex_refused"


def replace_heals(obj, heals):
    from dataclasses import replace
    return replace(obj, heals=heals)


def test_settle_charge():
    obj = combat.ChargeObjective("g1", level=1, tiles=3)
    ok = combat.settle_charge(obj, request_id="r", lifecycle="completed", cleanup=True,
                              envelope=env("ChargeReached(1)", wes="observed_charge_area", tiles_changed=3))
    assert ok.status == "CHARGED" and combat.charge_finish_verdict(ok) == ("achieved", "observed_charge_area")
    few = combat.settle_charge(obj, request_id="r", lifecycle="completed", cleanup=True,
                               envelope=env("ChargeReached(1)", wes="observed_charge_area", tiles_changed=2))
    assert few.status == "OPEN" and few.reason == "too_few_tiles_changed"
    assert combat.settle_charge(obj, request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("ChargeAborted(above_cap)")).status == "BLOCKED"
    assert combat.settle_charge(obj, request_id="r", lifecycle="completed", cleanup=True,
                                envelope=env("ChargeAborted(timeout)")).status == "OPEN"


def test_verdict_parse_and_wire():
    assert combat.parse_verdict("MonsterKilled(2)", command.REFLEX_VERDICTS_COMBAT) == ("MonsterKilled", "2")
    assert combat.parse_verdict("MonsterKilled", command.REFLEX_VERDICTS_COMBAT) is None
    assert combat.parse_verdict("Aborted(heal_needed)", command.REFLEX_VERDICTS_COMBAT) == ("Aborted", "heal_needed")
    assert combat.parse_verdict("FishCaught", command.REFLEX_VERDICTS_COMBAT) is None
    assert combat.parse_verdict("ChargeReached(x)", command.REFLEX_VERDICTS_CHARGE) is None
    w = combat.combat_wire(combat.CombatObjective("g1", count=2, kills=1, last_verdict="MonsterKilled(1)"))
    assert w == {"kind": "kill_monsters", "target": "slime", "count": 2, "kills": 1, "status": "OPEN", "reflex_calls": 0,
                 "calls_left": combat.MAX_COMBAT_CALLS, "last_verdict": "MonsterKilled(1)"}
    j = combat.journal_fields(env("MonsterKilled(1)", killed_types=["GreenSlime"], kills=1, hp_min=70, per_tick=[1, 2]))
    assert j["kills"] == 1 and j["killed_types"] == ["GreenSlime"] and "per_tick" not in j
    j = combat.journal_fields(env("MonsterKilled(1)", kill_witness={"hp_zero_seen": 0, "stats_monsters_killed_delta": 1,
                                                                    "vanished_unwitnessed": 0}))
    assert j["kill_witness"]["stats_monsters_killed_delta"] == 1, "both kill witnesses reach the journal"
    assert combat.parse_verdict("Aborted(location_refused)", command.REFLEX_VERDICTS_COMBAT) == ("Aborted", "location_refused")
    volcano = combat.settle_combat(combat.CombatObjective("g1"), request_id="r", lifecycle="completed", cleanup=True,
                                   envelope=env("Aborted(location_refused)"))
    assert volcano.status == "BLOCKED" and volcano.reason == "reflex_aborted:location_refused"


def test_command_mirror_args_readiness_and_digest_vectors():
    command.validate_reflex_combat_args({"max_s": 30, "hp_floor_pct": 40, "max_kills": 1, "retreat": True})
    command.validate_reflex_charge_args({"target_level": 1, "max_ticks": 120})
    for bad in ({"max_s": 30, "hp_floor_pct": 40, "max_kills": 1}, {"max_s": 61, "hp_floor_pct": 40, "max_kills": 1, "retreat": True},
                {"max_s": 30, "hp_floor_pct": 40, "max_kills": 6, "retreat": True}, {"max_s": 30, "hp_floor_pct": 40, "max_kills": 1, "retreat": 1}):
        with pytest.raises(command.KesalahanPerintah):
            command.validate_reflex_combat_args(bad)
    for bad in ({"target_level": 5, "max_ticks": 120}, {"target_level": 1, "max_ticks": 44}, {"target_level": 1}):
        with pytest.raises(command.KesalahanPerintah):
            command.validate_reflex_charge_args(bad)
    base = {"protocol": 1, "op": "input_once", "request_id": "tool-1", "runtime_instance_id": "runtime",
            "session_id": "session", "owner_id": "owner", "generation": 1, "control_seq": 1, "context_generation": 7,
            "expected_player_id": 42, "expected_player_name": "arti", "expected_screen_id": 0,
            "expected_location_id": "farm", "family": "world_key", "deadline_unix_ms": 1000}
    combat_payload = dict(base, primitive="reflex_combat", args={"max_s": 30, "hp_floor_pct": 40, "max_kills": 1, "retreat": True})
    charge_payload = dict(base, primitive="reflex_charge", args={"target_level": 1, "max_ticks": 120})
    # ReflexCombatTests.CombatDigestVector / ReflexChargeTests.ChargeDigestVector (C#)
    assert command._journal_payload_digest(combat_payload) == "2e94a5ca8ee75d2793090a60513cbbfb01320544c5faf3d82c2e429b3dc769cc"
    assert command._journal_payload_digest(charge_payload) == "c0481bebbf6abcf9c4cc724c07b488031ffc25c40fe8a8848663e17ea58a7850"
    assert command._journal_payload_digest(dict(combat_payload, args=dict(combat_payload["args"], retreat=False))) \
        != command._journal_payload_digest(combat_payload)
    sesi = command.SesiPerintah("s", 1, "Arti", "Farm", 0, False, False, True, False, False, True, True, None, False,
                                mentah={"reflex_combat_enabled": False})
    assert "ReflexCombatEnabled" in (sesi.alasan_belum_siap(command.OP_INPUT_ONCE, primitive="reflex_combat") or "")
    assert command.OUTCOME_SUKSES_PRIMITIVE["reflex_combat"] == "MonsterKilled"


def test_decision_admission_accepts_only_filled_contract_args():
    def admitted(op, args):
        d = decision.GameDecision(1, "n", "g", 1, "c", "", decision.ActChoice(op, None, args, ()))
        return decision.runtime_choice_admitted(d)
    assert admitted("reflex_combat", {"max_s": 30, "hp_floor_pct": 40, "max_kills": 1, "retreat": True})
    assert not admitted("reflex_combat", {"max_s": 30})
    assert admitted("reflex_charge", {"target_level": 1, "max_ticks": 120})
    assert not admitted("reflex_charge", {"target_level": 1, "max_ticks": 120, "x": 1})
