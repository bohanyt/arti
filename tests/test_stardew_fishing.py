"""Primitives v2 WS-G4: the pure runtime side of the fishing reflex (grammar, offer, args, accounting, finish).

Evidence ceiling: UNIT_TESTED (the reflex itself runs in the mod; see the C# ReflexFishingTests / HoldRuntimeTests).
"""
from __future__ import annotations

import pytest

import arti_stardew_command as command
import arti_stardew_fishing as fishing
import arti_stardew_pad_runtime as pad_runtime

UI = {
    "capabilities": {"reflex_fish": {"implemented": True, "available": True, "reason": "x"}},
    "fishing": {"rod_held": True, "faces_water": True, "landing_fishable": True, "landing": [13, 10], "fishing_level": 0,
                "water_edges": [dict(stand_x=11, stand_y=10, facing=1, face_x=12, face_y=10, land_x=14, land_y=10),
                                dict(stand_x=11, stand_y=9, facing=1, face_x=12, face_y=9, land_x=14, land_y=9)]},
}


def env(verdict="FishCaught", success=True, effect="observed_fish_caught", cleanup=True, **reflex):
    return {"reflex_verdict": verdict, "success": success, "world_effect_status": effect, "cleanup_confirmed": cleanup,
            "outcome": verdict.split("(")[0], "status": "completed",
            "reflex": {"detail": "item_added", "phase_ticks": {"Cast": 0, "Bar": 120}, "presses": 3,
                       "bar": {"updates": 400, "x_writes": 180},
                       "fish": {"item": "(O)142", "quality": 0, "size": 23, "category": -4},
                       **reflex}}


@pytest.mark.parametrize("text", ["tangkap satu ikan", "catch one fish", "Catch a fish!", "mancing satu ikan", "tangkap 1 ikan."])
def test_owner_grammar_accepts_one_fish(text):
    assert fishing.parse_fishing_goal(text)


@pytest.mark.parametrize("text", ["tangkap dua ikan", "catch 3 fish", "catch one fish and sell it", "ikan", "", None, 5])
def test_owner_grammar_refuses_anything_else(text):
    assert not fishing.parse_fishing_goal(text)


def test_args_fill_defaults_validate_and_refuse_foreign_keys():
    target = fishing.reflex_target(UI, fishing.FishingObjective("g"))
    assert target == {"landing": [13, 10], "fishing_level": 0}
    assert fishing.fill_args({}, target) == {"max_s": 60, "cast_power": 0.85, "treasure": False, "hook_retries": 1}
    assert fishing.fill_args({"treasure": True, "max_s": 30}, target)["treasure"] is True
    assert fishing.fill_args({"max_casts": 2}, target) is None
    assert fishing.fill_args({"cast_power": 1.0}, target) is None
    assert fishing.fill_args({}, None) is None, "only an offered target is filled"
    for bad in ({"max_s": 91, "cast_power": 0.5, "treasure": False, "hook_retries": 1},
                {"max_s": 60, "cast_power": True, "treasure": False, "hook_retries": 1},
                {"max_s": 60, "cast_power": 0.5, "treasure": 1, "hook_retries": 1},
                {"max_s": 60, "cast_power": 0.5, "treasure": False, "hook_retries": 3},
                {"max_s": 60, "cast_power": 0.5, "treasure": False}):
        with pytest.raises(command.KesalahanPerintah):
            command.validate_reflex_fish_args(bad)


def test_offer_needs_every_mod_fact_and_an_open_objective():
    open_ = fishing.FishingObjective("g")
    assert fishing.reflex_target(UI, open_) is not None
    for path, value in ((("capabilities", "reflex_fish", "available"), False), (("fishing", "rod_held"), False),
                        (("fishing", "faces_water"), False), (("fishing", "landing_fishable"), False),
                        (("fishing", "landing"), [1])):
        ui = {"capabilities": {"reflex_fish": dict(UI["capabilities"]["reflex_fish"])}, "fishing": dict(UI["fishing"])}
        node = ui
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        assert fishing.reflex_target(ui, open_) is None, path
    assert fishing.reflex_target(UI, fishing.FishingObjective("g", status="CAUGHT")) is None
    assert fishing.reflex_target(UI, fishing.FishingObjective("g", calls=fishing.MAX_REFLEX_CALLS)) is None
    assert fishing.reflex_target({"fishing": UI["fishing"]}, open_) is None, "no capability (flag off) -> no offer"


def test_water_edge_entries_are_stand_and_face_tiles():
    entries = fishing.water_edge_entries(UI, (8, 10), fishing.FishingObjective("g"))
    assert [e["ref"] for e in entries] == ["water_edge:12,10@11,10", "water_edge:12,9@11,9"]
    assert entries[0]["tile"] == [12, 10] and entries[0]["reach_tiles"] == [[11, 10]] and entries[0]["kind"] == "water_edge"
    assert fishing.rank_override(entries[0], fishing.FishingObjective("g")) == 0
    assert fishing.water_edge_entries(UI, (30, 30), fishing.FishingObjective("g")) == [], "beyond the reach"
    assert fishing.water_edge_entries(UI, (8, 10), fishing.FishingObjective("g", status="CAUGHT")) == []
    bad = {"fishing": {"water_edges": [dict(stand_x=11, stand_y=10, facing=1, face_x=13, face_y=10)]}}
    assert fishing.water_edge_entries(bad, (8, 10), fishing.FishingObjective("g")) == [], "face must be adjacent"


def test_settle_caught_escaped_nobite_aborted_uncertain_and_budget():
    o = fishing.FishingObjective("g")
    caught = fishing.settle(o, request_id="r1", lifecycle="completed", cleanup=True, envelope=env())
    assert caught.status == "CAUGHT" and caught.fish == {"item": "(O)142", "quality": 0, "size": 23, "category": -4}  # PART_B4: native category kept
    assert caught.caught_request_id == "r1" and fishing.finish_verdict(caught) == ("achieved", "observed_fish_caught")
    assert fishing.settle(caught, request_id="r2", lifecycle="completed", cleanup=True, envelope=env("NoBite", False, "unchanged")) is caught
    esc = fishing.settle(o, request_id="r1", lifecycle="completed", cleanup=True, envelope=env("FishEscaped", False, "unchanged"))
    assert esc.status == "OPEN" and esc.calls == 1 and esc.last_verdict == "FishEscaped"
    assert fishing.settle(esc, request_id="r1", lifecycle="completed", cleanup=True, envelope=env()) is esc, "never replayed"
    full = fishing.settle(o, request_id="r1", lifecycle="completed", cleanup=True,
                          envelope=env("Aborted(inventory_full)", False, "unchanged"))
    assert full.status == "BLOCKED" and full.reason == "reflex_aborted:inventory_full"
    missed = fishing.settle(o, request_id="r1", lifecycle="completed", cleanup=True,
                            envelope=env("Aborted(hook_missed)", False, "unchanged"))
    assert missed.status == "OPEN", "hook_missed is retryable"
    unsure = fishing.settle(o, request_id="r1", lifecycle="uncertain", cleanup=False, envelope=env(cleanup=False))
    assert unsure.status == "UNCERTAIN" and fishing.finish_verdict(unsure)[0] == "uncertain"
    fake = fishing.settle(o, request_id="r1", lifecycle="completed", cleanup=True, envelope=env("FishCaught", True, "unverified"))
    assert fake.status == "OPEN", "a catch without the mod's verified effect is not a catch"
    refused = fishing.settle(o, request_id="r1", lifecycle="rejected", cleanup=True, envelope={"reject_reason": "ArmRejected"})
    assert refused.calls == 0 and refused.status == "OPEN"
    x = o
    for i in range(fishing.MAX_REFLEX_CALLS):
        x = fishing.settle(x, request_id=f"n{i}", lifecycle="completed", cleanup=True, envelope=env("NoBite", False, "unchanged"))
    assert x.status == "BLOCKED" and x.reason == "reflex_call_budget_exhausted"
    assert fishing.finish_verdict(None) is None and fishing.finish_verdict(o) == ("partial", "fish_not_caught_yet")


@pytest.mark.parametrize("fish", [
    {"item": "(O)168", "quality": 0, "size": 0, "category": -20},   # Trash (FR:640-644: pulled without the bar)
    {"item": "(O)152", "quality": 0, "size": 0, "category": 0},     # Seaweed-like non-fish
    {"item": "(O)142", "quality": 0, "size": 23},                   # no category published: never counted as a fish
    {"item": "(O)142", "quality": 0, "size": 23, "category": "-4"},  # wrong type
])
def test_junk_is_not_the_fish_and_another_cast_is_allowed(fish):
    """Review minor 4: CAUGHT only for category -4; anything else is labelled junk, the goal stays open."""
    o = fishing.FishingObjective("g")
    junk = fishing.settle(o, request_id="r1", lifecycle="completed", cleanup=True, envelope=env(fish=fish))
    assert junk.status == "OPEN" and junk.reason == "caught_not_fish" and junk.last_verdict == fishing.LABEL_JUNK
    assert junk.calls == 1 and junk.caught_request_id is None and junk.fish is None
    assert fishing.finish_verdict(junk) == ("partial", "caught_not_fish")
    target = fishing.reflex_target(UI, junk)
    assert target is not None, "another cast is offered within the budget"
    caught = fishing.settle(junk, request_id="r2", lifecycle="completed", cleanup=True, envelope=env())
    assert caught.status == "CAUGHT" and caught.calls == 2 and caught.caught_request_id == "r2"
    x = o
    for i in range(fishing.MAX_REFLEX_CALLS):
        x = fishing.settle(x, request_id=f"j{i}", lifecycle="completed", cleanup=True, envelope=env(fish=fish))
    assert x.status == "BLOCKED" and x.reason == "reflex_call_budget_exhausted" and x.last_verdict == "junk"
    assert fishing.reflex_target(UI, x) is None


def test_wire_and_journal_projection_are_bounded():
    o = fishing.settle(fishing.FishingObjective("g"), request_id="r1", lifecycle="completed", cleanup=True, envelope=env())
    assert fishing.wire(o) == {"kind": "catch_fish", "status": "CAUGHT", "reflex_calls": 1, "calls_left": 3,
                               "last_verdict": "FishCaught", "fish": {"item": "(O)142", "quality": 0, "size": 23, "category": -4},
                               "reason": "observed_fish_caught"}
    j = fishing.journal_fields(env())
    assert j["verdict"] == "FishCaught" and j["phase_ticks"] == {"Cast": 0, "Bar": 120} and j["bar"]["x_writes"] == 180
    assert "per_tick" not in j


def test_digest_vector_is_shared_with_the_mod():
    payload = {"protocol": 1, "op": "input_once", "request_id": "tool-1", "runtime_instance_id": "runtime",
               "session_id": "session", "owner_id": "owner", "generation": 1, "control_seq": 1, "context_generation": 7,
               "expected_player_id": 42, "expected_player_name": "arti", "expected_screen_id": 0,
               "expected_location_id": "farm", "family": "world_key", "primitive": "reflex_fish",
               "args": {"max_s": 60, "cast_power": 0.85, "treasure": False, "hook_retries": 1}, "deadline_unix_ms": 1000}
    # ReflexFishingTests.ReflexDigestVector (C#)
    assert command._journal_payload_digest(payload) == "36a03328185e7a9ef864f593f87e914ae2d8f162270d9f4b3d4946461706eba8"
    other = dict(payload, args=dict(payload["args"], cast_power=0.6))
    assert command._journal_payload_digest(other) != command._journal_payload_digest(payload)


def test_session_readiness_names_the_reflex_flag():
    sesi = command.SesiPerintah("s", 1, "Arti", "Farm", 0, False, False, True, False, False, True, True, None, False,
                                mentah={"reflex_fish_enabled": False})
    assert "ReflexFishingEnabled" in (sesi.alasan_belum_siap(command.OP_INPUT_ONCE, primitive="reflex_fish") or "")
    ready = command.SesiPerintah("s", 1, "Arti", "Farm", 0, False, False, True, False, False, True, True, None, False,
                                 mentah={"reflex_fish_enabled": True})
    assert ready.alasan_belum_siap(command.OP_INPUT_ONCE, primitive="reflex_fish") is None


def test_pad_label_marks_reflex_fish_as_x():
    actions = {"reflex_fish": {"target_ref": None}}
    legend = pad_runtime.annotate_catalog(actions, {"buttons": {"X": {"family": "reflex_fish", "detail": "catch_one_fish"}}})
    assert actions["reflex_fish"]["button"] == "X" and legend == {"X": "reflex_fish"}
