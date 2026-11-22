"""Primitives v2 WS-G6 world extras: command contract, C#/Python kind parity, owner grammar, offers, approach entries,
verdict readback, follow-through planning (question answer, elevator floor) and the extras_rule finish.

Pure helpers + the runtime mixin over a small fake (no native folder, no provider). Evidence ceiling UNIT_TESTED.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import arti_stardew_command as command
import arti_stardew_interact as interact
import arti_stardew_world_extras as extras
from arti_stardew_decision import ActChoice, GameDecision, runtime_choice_admitted
from arti_stardew_ui import baca_ui_key  # noqa: F401  (import check: question keys)
from arti_stardew_ui_pad import UiPadView
from arti_stardew_world_extras_runtime import WorldExtrasRuntimeMixin

ROOT = Path(__file__).resolve().parents[1]
PET = dict(target_token="tok", source_fact_id="xt:pet:Farm:10,11:Dudley", tile_x=10, tile_y=11, actor_tile_x=10,
           actor_tile_y=10, facing=2, kind="pet")
LADDER = dict(PET, kind="ladder_down", source_fact_id="xt:ladder_down:UndergroundMine5:10,11",
              target_location_id="UndergroundMine6")


def decision(op, args):
    return GameDecision(1, "n", "g", 1, "c", "", ActChoice(op, None, dict(args), ()))


# ---------------------------------------------------------------- contract + parity

def test_kind_table_parity_with_the_mod():
    src = (ROOT / "stardew-mod/ArtiStardewBridge/Protocol/WorldExtras.cs").read_text(encoding="utf-8")
    block = re.search(r'public const string Pet = "pet".*?;', src, re.S).group(0)
    cs_kinds = set(re.findall(r'= "([a-z_]+)"', block))
    # 17 G6 kinds + fix-D-B2-1 shop_counter + FIXB7 D-B5b-3 shipping_bin + FIXB17 ladder_up
    assert cs_kinds == set(command.WORLD_EXTRAS_KINDS) and len(cs_kinds) == 29  # B1 Blacksmith + CC note, B2 Marnie, B3 kitchen, B4 quest
    outcomes = re.search(r"public static string OutcomeOf.*?};", src, re.S).group(0)
    for kind, outcome in command.WORLD_EXTRAS_OUTCOMES.items():
        assert outcome in src, outcome
    assert "ladder_down" in command.WORLD_INTERACT_LOCATION_KINDS and "Mounted" in outcomes
    import arti_stardew_decision
    assert arti_stardew_decision.WORLD_EXTRAS_KINDS == set(command.WORLD_EXTRAS_KINDS)
    ui = (ROOT / "stardew-mod/ArtiStardewBridge/Protocol/UiPadInput.cs").read_text(encoding="utf-8")
    for ctx in ("mine_elevator", "junimo_note", "museum"):
        assert f'"{ctx}"' in ui and ctx in command.UI_PAD_CONTEXTS


def test_world_interaction_args_for_extras_kinds():
    for kind in command.WORLD_EXTRAS_KINDS:
        args = dict(PET, kind=kind)
        if kind in command.WORLD_INTERACT_LOCATION_KINDS:
            args["target_location_id"] = "UndergroundMine6"
        command.validate_world_interaction_args(args)
        assert runtime_choice_admitted(decision("world_interact", args)), kind
        assert interact.interaction_kind(args) == kind  # never the chest store path
    with pytest.raises(command.KesalahanPerintah, match="invalid_world_interaction_args"):
        command.validate_world_interaction_args(dict(PET, kind="ladder_down"))
    with pytest.raises(command.KesalahanPerintah, match="invalid_world_interaction_args"):
        command.validate_world_interaction_args(dict(PET, target_location_id="Farm"))
    assert not runtime_choice_admitted(decision("world_interact", dict(PET, kind="gift")))


@pytest.mark.parametrize("kind,outcome,success", [
    ("pet", "Petted", True), ("pet", "InteractionNoEffect", False), ("animal_milk", "ProductCollected", True),
    ("place_item", "ItemPlaced", True), ("place_item", "Petted", False), ("shaft", "QuestionOpened", True),
    ("mine_elevator", "MenuOpened", True), ("ladder_down", "LocationChanged", True),
    ("ladder_down", "LocationChangedEventUp", True), ("horse_mount", "Mounted", True)])
def test_success_outcome_per_extras_kind(kind, outcome, success):
    args = dict(PET, kind=kind)
    if kind == "ladder_down":
        args["target_location_id"] = "UndergroundMine6"
    result = command._hasil_dari_envelope(dict(status="completed", success=True, outcome=outcome, op="input_once",
                                               primitive="world_interact", input_args=args), "r")
    assert result.sukses is success


def test_question_keys_published_by_the_mod_are_accepted():
    import arti_stardew_ui as ui
    src = Path(ui.__file__).read_text(encoding="utf-8")
    assert '"Shaft", "Bus", "pagedResponse"' in src


# ---------------------------------------------------------------- grammar + policy

@pytest.mark.parametrize("text,kind,extra", [
    ("elus kucing", "pet", None), ("pet the dog", "pet", None), ("perah sapi", "milk", None),
    ("milk the cow", "milk", None), ("cukur domba", "shear", None), ("elus sapi", "animal_pet", None),
    ("pet the chicken", "animal_pet", None), ("naik kuda", "mount", None), ("ride the horse", "mount", None),
    ("turun dari kuda", "dismount", None), ("goyang pohon", "shake", None), ("shake a tree", "shake", None),
    ("panen", "harvest", None), ("harvest a crop", "harvest", None), ("ambil hasil mesin", "collect", None),
    ("isi mesin", "insert", None), ("ambil jerami", "hay", None), ("turun tangga", "ladder", None),
    ("go down the ladder", "ladder", None), ("lompat ke lubang", "shaft", None),
    ("naik lift ke lantai 5", "elevator", 5), ("take the elevator to floor 40", "elevator", 40),
    ("naik bus ke gurun", "bus", "Desert"), ("naik minecart ke kota", "minecart", "Town"),
    ("tanam benih", "place", None), ("plant a seed", "place", None), ("pasang sprinkler", "place", None),
    ("donasi ke museum", "museum", None), ("isi bundle", "bundle", None), ("Fill the bundle.", "bundle", None)])
def test_owner_grammar(text, kind, extra):
    obj = extras.parse_extras_objective(text)
    assert obj is not None and obj.kind == kind
    if kind == "elevator":
        assert obj.floor == extra
    if kind in ("bus", "minecart"):
        assert obj.destination == extra


@pytest.mark.parametrize("text", ["naik lift", "naik lift ke lantai 7", "naik lift ke lantai 125", "naik minecart ke bulan",
                                  "give the cow a gift", "elus", "beri hadiah ke abigail", "isi semua bundle sekarang juga ya"])
def test_owner_grammar_refuses_anything_else(text):
    assert extras.parse_extras_objective(text) is None


def test_time_rule_mirrors_g5():
    ladder = extras.ExtrasObjective("ladder")
    assert extras.time_refusal(ladder, 1950) is None and extras.time_refusal(ladder, 2000) == "time_rule:mines_after_2000"
    assert extras.time_refusal(extras.ExtrasObjective("elevator", floor=0), 2200) is None
    assert extras.time_refusal(extras.ExtrasObjective("elevator", floor=5), 2200) == "time_rule:mines_after_2000"
    assert extras.time_refusal(extras.ExtrasObjective("pet"), 2500) == "time_rule:hard_stop"
    assert extras.time_refusal(extras.ExtrasObjective("animal_pet"), 1900) == "time_rule:animal_sleeping"
    assert extras.MINES_LATEST_DESCEND_TIME == 2000 and extras.HARD_STOP_TIME == 2500


# ---------------------------------------------------------------- offers + approach

def test_offer_only_the_objectives_own_kind():
    pet = extras.ExtrasObjective("pet")
    assert extras.target_offered(PET, pet)
    assert not extras.target_offered(dict(PET, kind="animal_pet"), pet)
    assert not extras.target_offered(PET, None) and not extras.target_offered(dict(PET, kind="chest"), pet)
    pet.done = 1
    assert not extras.target_offered(PET, pet)
    shaft = extras.ExtrasObjective("shaft", opened=True)
    assert not extras.target_offered(dict(PET, kind="shaft"), shaft)
    blocked = extras.ExtrasObjective("pet", blocker="time_rule:hard_stop")
    assert not extras.target_offered(PET, blocked)


def test_approach_entries_from_extras_targets():
    ui = {"extras_targets": [
        {"kind": "pet", "ref": "xt:pet:Farm:Dudley", "x": 14, "y": 10, "label": "Dudley", "ready": True},
        {"kind": "pet", "ref": "xt:pet:Farm:Other", "x": 14, "y": 12, "label": "Other", "ready": False},
        {"kind": "animal_pet", "ref": "xt:animal:Farm:1", "x": 12, "y": 10, "label": "Cow", "ready": True},
        {"kind": "pet", "ref": "x" * 101, "x": 1, "y": 1},
        {"kind": "pet", "ref": "far", "x": 40, "y": 40}]}
    entries = extras.approach_entries(ui, (10, 10), extras.ExtrasObjective("pet"))
    assert [e["ref"] for e in entries] == ["xt:pet:Farm:Dudley"] and entries[0]["tile"] == [14, 10]
    moved = extras.approach_entries({"extras_targets": [dict(ui["extras_targets"][0], x=15)]}, (10, 10),
                                    extras.ExtrasObjective("pet"))
    assert moved[0]["revision"] != entries[0]["revision"]
    assert extras.rank_override(entries[0], extras.ExtrasObjective("pet")) == 0
    assert extras.approach_entries(ui, (10, 10), extras.ExtrasObjective("bundle")) == []


# ---------------------------------------------------------------- readback

def env(kind, outcome, detail=None, **kw):
    args = dict(PET, kind=kind)
    return dict(status="completed", success=True, outcome=outcome, cleanup_confirmed=True, input_args=args,
                effect_detail=detail, **kw)


def test_verified_effect_per_kind():
    assert extras.verified_effect("pet", env("pet", "Petted", "pet||0|12")) == {"kind": "pet", "item": "", "delta": 0, "value": 12}
    assert extras.verified_effect("animal_milk", env("animal_milk", "ProductCollected", "animal_milk|(O)184|1|0"))["delta"] == 1
    assert extras.verified_effect("animal_milk", env("animal_milk", "ProductCollected", "animal_milk|(O)184|0|0")) is None
    assert extras.verified_effect("place_item", env("place_item", "ItemPlaced", "place_item|(O)472|-1|0"))["item"] == "(O)472"
    assert extras.verified_effect("pet", env("pet", "Petted", "pet||1|0")) is None
    assert extras.verified_effect("pet", env("pet", "Petted", None)) is None
    assert extras.verified_effect("pet", dict(env("pet", "Petted", "pet||0|0"), cleanup_confirmed=False)) is None
    assert extras.verified_effect("pet", env("pet", "InteractionNoEffect", "pet||0|0")) is None
    shaft = env("shaft", "QuestionOpened", "shaft", result_receiver_token="rcv:q1", result_context_generation=8)
    assert extras.verified_effect("shaft", shaft)["receiver_token"] == "rcv:q1"
    assert extras.verified_effect("shaft", dict(shaft, result_receiver_token=None)) is None
    assert extras.parse_effect_detail("a|b|c") is None and extras.parse_effect_detail("pet|x|1|y") is None


def test_record_and_verdict():
    pet = extras.ExtrasObjective("pet")
    assert extras.objective_verdict(pet) == ("partial", "effect_not_observed")
    assert extras.record_effect(pet, {"kind": "animal_pet"}) is None
    assert extras.record_effect(pet, {"kind": "pet", "delta": 0})["done"] == 1
    assert extras.objective_verdict(pet) == ("achieved", extras.FINISH)
    shaft = extras.ExtrasObjective("shaft")
    extras.record_effect(shaft, {"kind": "shaft", "receiver_token": "rcv:1"})
    assert shaft.opened and extras.objective_verdict(shaft) == ("partial", "question_not_answered")
    assert extras.record_arrival(shaft, "UndergroundMine5", "UndergroundMine9") is None  # not answered yet
    shaft.answered = True
    assert extras.record_arrival(shaft, "UndergroundMine5", "UndergroundMine9")["to"] == "UndergroundMine9"
    assert extras.objective_verdict(shaft) == ("achieved", extras.FINISH)
    ladder = extras.ExtrasObjective("ladder")
    extras.record_effect(ladder, {"kind": "ladder_down", "location": "UndergroundMine6"})
    assert extras.objective_verdict(ladder)[0] == "achieved"


def test_ui_press_notes_for_bundle_museum_elevator():
    bundle = extras.ExtrasObjective("bundle")
    assert extras.note_ui_press(bundle, context="junimo_note", snapped_kind="slot", held=False, btn="A",
                                effect="changed", verified=True) is None and bundle.opened
    assert extras.note_ui_press(bundle, context="junimo_note", snapped_kind="ingredient", held=True, btn="A",
                                effect="changed", verified=False) is None
    assert extras.note_ui_press(bundle, context="junimo_note", snapped_kind="ingredient", held=True, btn="A",
                                effect="changed", verified=True)["deposit"]
    assert extras.objective_verdict(bundle)[0] == "achieved"
    museum = extras.ExtrasObjective("museum")
    assert extras.note_ui_press(museum, context="museum", snapped_kind="slot", held=False, btn="A", effect="changed",
                                verified=True) is None
    assert extras.note_ui_press(museum, context="museum", snapped_kind="slot", held=True, btn="A", effect="changed",
                                verified=True)["donated"]
    elevator = extras.ExtrasObjective("elevator", floor=5)
    assert extras.note_ui_press(elevator, context="mine_elevator", snapped_kind="floor", held=False, btn="A",
                                effect="closed", verified=True)["pressed"]


# ---------------------------------------------------------------- follow-through planning

def key_view(question_key="Shaft", options=("Jump", "Do"), available=True):
    return SimpleNamespace(kind="question", question_key=question_key, available=available, not_ready_reason=None,
                           options=tuple({"index": i, "response_key": k} for i, k in enumerate(options)),
                           args_for=lambda key, option_index=None: {"key": key, "option_index": option_index})


def test_question_answers_only_the_objectives_own_response():
    shaft = extras.ExtrasObjective("shaft")
    step = extras.plan_question_answer(shaft, key_view())
    assert step.kind == "act" and step.args == {"key": "answer", "option_index": 0}
    assert extras.plan_question_answer(shaft, key_view("ExitMine")).reason == "unexpected_question:ExitMine"
    assert extras.plan_question_answer(shaft, key_view(available=False)).kind == "wait"
    bus = extras.ExtrasObjective("bus", destination="Desert")
    assert extras.plan_question_answer(bus, key_view("Bus", ("Yes", "No"))).args["option_index"] == 0
    cart = extras.ExtrasObjective("minecart", destination="Town")
    assert extras.plan_question_answer(cart, key_view("pagedResponse", ("Bus", "Mines", "Town"))).args["option_index"] == 2
    assert extras.plan_question_answer(cart, key_view("pagedResponse", ("Bus", "Mines"))).reason == "response_missing:Town"


def pad_view(snapped, components, buttons=None):
    return UiPadView("rcv:e", "MineElevatorMenu", "mine_elevator", "up:" + "b" * 32, True, True, None, snapped, None,
                     tuple(components), len(components), buttons or {"A": {"does": "x"}, "stick": {"does": "nav"}},
                     None, None, None, None, None, (), 9)


FLOORS = [{"id": 0, "kind": "floor", "label": "0", "nbr": [-1, 1, 3, -1]},
          {"id": 1, "kind": "floor", "label": "5", "nbr": [-1, 2, 4, 0]},
          {"id": 2, "kind": "floor", "label": "10", "nbr": [-1, -1, -1, 1]},
          {"id": 3, "kind": "floor", "label": "15", "nbr": [0, 4, -1, -1]},
          {"id": 4, "kind": "floor", "label": "20", "nbr": [1, -1, -1, 3]}]


def test_elevator_planning_routes_by_neighbour_ids():
    obj = extras.ExtrasObjective("elevator", floor=20)
    first = extras.plan_elevator_step(obj, pad_view(-1, FLOORS))
    assert first.kind == "act" and first.args["dir"] == "right"
    assert extras.plan_elevator_step(obj, pad_view(0, FLOORS)).args["dir"] in ("right", "down")
    press = extras.plan_elevator_step(obj, pad_view(4, FLOORS))
    assert press.reason == "press_floor" and press.args["btn"] == "A" and press.args["snapped_id"] == 4
    assert extras.plan_elevator_step(extras.ExtrasObjective("elevator", floor=60), pad_view(0, FLOORS)).kind == "stop"
    assert extras.nav_direction(FLOORS, 0, 2) == "right" and extras.nav_direction(FLOORS, 2, 3) in ("left", "down")


# ---------------------------------------------------------------- runtime mixin over a fake

class FakeObs:
    def __init__(self, facts):
        self.facts = facts

    def get(self, pid, domain):
        return SimpleNamespace(facts=self.facts, authoritative=True) if domain == "ui.context" else None


class Fake(WorldExtrasRuntimeMixin):
    def __init__(self, facts=None, time_of_day=900, location="UndergroundMine5"):
        self._init_world_extras_runtime()
        self.journal: list = []
        self._lock = threading.RLock()
        self._worker_stop = threading.Event()
        self._action_cancel = threading.Event()
        self._obs = FakeObs(facts or {})
        self.motor = SimpleNamespace(time_of_day=time_of_day, location=location)
        self._state = SimpleNamespace(latest=lambda pid: self.motor)
        self._active = None

    def _journal(self, e):
        self.journal.append(e)

    def _binding_view(self):
        return None, 42


def test_runtime_pin_offer_and_finish():
    rt = Fake(time_of_day=2010)
    goal = SimpleNamespace(goal_id="g", play_blocker=None, extras_objective=None)
    request = SimpleNamespace(snapshot=SimpleNamespace(water_context=SimpleNamespace(location="UndergroundMine5"),
                                                       movement_context=None, motor=SimpleNamespace(time_of_day=2010)))
    rt._pin_extras_objective(goal, extras.ExtrasObjective("ladder"), request)
    assert goal.play_blocker == "time_rule:mines_after_2000" and goal.extras_objective.blocker
    goal2 = SimpleNamespace(goal_id="g2", play_blocker=None, extras_objective=extras.ExtrasObjective("pet"))
    rt.motor.time_of_day = 900
    assert rt._extras_world_offered(goal2, PET) and not rt._extras_world_offered(goal2, dict(PET, kind="shake_tree"))
    rt.motor.time_of_day = 2500
    assert not rt._extras_world_offered(goal2, PET)
    verdict = rt._extras_finish_verdict(goal2)
    assert verdict.status == "partial" and verdict.observed["origin"] == "extras_rule"
    extras.record_effect(goal2.extras_objective, {"kind": "pet", "delta": 0})
    assert rt._extras_finish_verdict(goal2).status == "achieved" and goal2.extras_objective.finish_recorded
    assert rt._extras_finish_verdict(SimpleNamespace(extras_objective=None)) is None
    assert rt._extras_pad_button(SimpleNamespace(extras_objective=extras.ExtrasObjective("bundle"))) == "Start"
    assert rt._extras_pad_button(SimpleNamespace(extras_objective=extras.ExtrasObjective("pet"))) is None


def test_runtime_step_readback_and_receiver_identity():
    rt = Fake(facts={"interactive": {"receiver_token": "rcv:q1", "menu_type": "DialogueBox"}})
    request = SimpleNamespace(snapshot=SimpleNamespace(water_context=SimpleNamespace(player_id=42)))
    from arti_stardew_runtime import ActionEvidenceView
    ev = ActionEvidenceView("completed", "ok", "r", "QuestionOpened", world_effect_status="unverified", cleanup_confirmed=True)
    shaft = dict(PET, kind="shaft")
    result = SimpleNamespace(mentah=dict(env("shaft", "QuestionOpened", "shaft", result_receiver_token="rcv:q1"),
                                         input_args=shaft))
    out, cleanup = rt._extras_step_readback(request, shaft, result, ev)
    assert out.world_effect_status == extras.FINISH and cleanup and out.after["receiver_token"] == "rcv:q1"
    rt._obs.facts = {"interactive": {"receiver_token": "rcv:other", "menu_type": "DialogueBox"}}
    out2, _ = rt._extras_step_readback(request, shaft, result, ev)
    assert out2.world_effect_status == "uncertain"
    none = ActionEvidenceView("completed", "ok", "r", "InteractionNoEffect", world_effect_status="unverified")
    out3, _ = rt._extras_step_readback(request, PET, SimpleNamespace(mentah=env("pet", "InteractionNoEffect")), none)
    assert out3.world_effect_status == "no_effect_observed"


def test_runtime_follow_through_shaft_and_elevator():
    rt = Fake(location="UndergroundMine5")
    goal = SimpleNamespace(goal_id="g")
    shaft = extras.ExtrasObjective("shaft")
    views = [key_view()]
    rt._ui_key_view = lambda pid: views[0]

    def run_ui_key(args, origin):
        views[0] = SimpleNamespace(kind=None)
        rt.motor.location = "UndergroundMine11"
        return SimpleNamespace(uncertain=False, verified=True, lifecycle="completed")
    rt._run_ui_key = run_ui_key
    stop, steps = rt._extras_follow_through(goal, shaft, "shaft", "UndergroundMine5")
    assert stop == "arrived" and steps == ["answer:Jump"] and extras.objective_verdict(shaft)[0] == "achieved"

    rt2 = Fake(location="Mine")
    elevator = extras.ExtrasObjective("elevator", floor=20, opened=True)
    state = {"snapped": -1}
    rt2._ui_pad_view = lambda pid: pad_view(state["snapped"], FLOORS)
    path = {-1: 0, 0: 1, 1: 4, 3: 4}

    def run_ui_pad(primitive, args, origin):
        if primitive == command.PRIMITIVE_UI_NAV:
            state["snapped"] = path.get(state["snapped"], 4) if args["dir"] != "down" or state["snapped"] != 0 else 3
            return SimpleNamespace(uncertain=False, verified=True, lifecycle="completed", effect="snapped"), None
        rt2.motor.location = "UndergroundMine20"
        return SimpleNamespace(uncertain=False, verified=True, lifecycle="completed", effect="closed"), None
    rt2._run_ui_pad = run_ui_pad
    stop, steps = rt2._extras_follow_through(goal, elevator, "mine_elevator", "Mine")
    assert stop == "arrived" and steps[-1] == "press_floor" and elevator.pressed
    assert extras.objective_verdict(elevator) == ("achieved", extras.FINISH)


def test_runtime_note_ui_press_from_the_g1_path():
    rt = Fake()
    goal = SimpleNamespace(goal_id="g", extras_objective=extras.ExtrasObjective("museum"))
    before = SimpleNamespace(context="museum", snapped={"kind": "slot"}, held_item={"item": "(O)60", "stack": 1})
    rt._extras_note_ui_press(goal, "ui_press", {"btn": "A"}, SimpleNamespace(effect="changed", verified=True), before)
    assert goal.extras_objective.done == 1 and rt.journal[-1]["origin"] == "ui_press"
    rt._extras_note_ui_press(goal, "ui_nav", {"dir": "up"}, SimpleNamespace(effect="snapped", verified=True), before)
    assert goal.extras_objective.done == 1
