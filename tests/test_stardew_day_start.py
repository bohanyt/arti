"""WS2 deterministic day-start planner (pure): rule ordering, profession policy, bounds. UNIT_TESTED."""
import json
from pathlib import Path

import pytest

from arti_stardew_day_start import (DEFAULT_PROFESSION_POLICY, PROGRESSION_SURFACES, DayStartPolicy, DayStartState,
                                    note_result, plan_day_start_step)
from arti_stardew_ui import baca_ui_key

POLICY = DayStartPolicy(enabled=True)
# Test-only table: exercises the id-not-side resolution; the shipped policy is empty (WP P1, owner O4).
TABLE = DayStartPolicy(enabled=True, professions={
    "0:5": 1, "0:10:0": 2, "0:10:1": 4, "1:5": 6, "1:10:6": 8, "1:10:7": 10, "2:5": 12, "2:10:12": 14, "2:10:13": 16,
    "3:5": 18, "3:10:18": 20, "3:10:19": 23, "4:5": 24, "4:10:24": 26, "4:10:25": 29})


def tok(n):
    return "uk:" + f"{n:032x}"


def menu(menu_type, kind=None, *, receiver="rcv:mi1", ready=True, reason=None, showing=True, professions=None,
         skill=2, level=1, pending=0, options=None):
    keys = {"level_up": ["confirm"], "level_up_profession": ["choose"], "shipping_summary": ["confirm"],
            "dialogue": ["advance"], "letter": ["close"], "question": ["answer", "cancel"],
            "npc_question": ["answer"]}.get(kind, [])
    block = {"receiver_token": receiver, "menu_type": menu_type, "ready": ready and kind is not None,
             "wait_only": kind is None and menu_type in ("SaveGameMenu", "ReadyCheckDialog", "GameMenu")}
    if kind is not None:
        block["kind"] = kind
        block["keys"] = [{"key": k, "target_token": tok(i + 1)} for i, k in enumerate(keys)]
    if reason or kind is None:
        block["not_ready_reason"] = reason or "wait_only"
    if professions is not None:
        block["professions"] = [{"index": i, "profession_id": p, "name": ""} for i, p in enumerate(professions)]
    if menu_type == "LevelUpMenu":
        block.update(skill=skill, level=level)
    if options:
        block["options"] = [{"index": i, "label": o, "response_key": o} for i, o in enumerate(options)]
    facts = {"context_generation": 3, "mode": "menu", "is_player_free": False,
             "interactive": {"receiver_token": receiver, "menu_type": menu_type},
             "capabilities": {"ui_key": {"implemented": True, "available": block["ready"]}},
             "day_transition": {"showing_end_of_night_stuff": showing, "end_of_night_menus_pending": pending},
             "ui_key": block}
    return facts, baca_ui_key(facts, age_s=0.1)


def free(showing=False):
    return {"context_generation": 3, "mode": "gameplay", "is_player_free": True,
            "capabilities": {}, "day_transition": {"showing_end_of_night_stuff": showing, "end_of_night_menus_pending": 0}}, None


def step(state, pair, now=0.0, revision=None, policy=POLICY):
    facts, view = pair
    return plan_day_start_step(facts, view, policy, state, now=now, context_revision=revision, date=(1, "spring", 2))


def test_P1_whitelist_surfaces_match_progression_whitelist():
    rows = json.loads((Path(__file__).parents[1] / "contracts/progression_whitelist.json").read_text())["entries"]
    native = {r["menu_type"] for r in rows if r["entry"] in ("c", "d")}
    assert set(PROGRESSION_SURFACES) == native == {"ShippingMenu", "LevelUpMenu"}


@pytest.mark.parametrize("menu_type,kind", [("LevelUpMenu", "level_up"), ("ShippingMenu", "shipping_summary")])
def test_P1_level_up_and_shipping_left_to_progression_client(menu_type, kind):
    state = DayStartState()
    for t in range(3):
        s = step(state, menu(menu_type, kind), now=t, revision=t)
        assert (s.kind, s.reason, s.ui_key_args) == ("wait", "progression_client:" + kind, None) and state.active
    assert state.actions == 0
    assert step(state, menu(menu_type, kind), now=241).reason == "wall_bound"


def test_P1_shipped_policy_never_picks_a_profession():
    state = DayStartState()
    s = step(state, menu("LevelUpMenu", "level_up_profession", professions=[13, 12], level=5))
    assert s.kind == "blocked" and s.reason == "profession_policy_missing" and s.ui_key_args is None


def test_chooser_picks_by_profession_id_even_with_sides_swapped():
    state = DayStartState()
    s = step(state, menu("LevelUpMenu", "level_up_profession", professions=[13, 12], level=5), policy=TABLE)
    assert s.kind == "act" and s.ui_key_args["profession_id"] == 12 and s.ui_key_args["option_index"] == 1
    assert s.profession == {"skill": 2, "level": 5, "options": [13, 12], "chosen": 12}
    # Level 10 after Gatherer (13) offers 16/17 -> 16 in the test table
    s = step(DayStartState(), menu("LevelUpMenu", "level_up_profession", professions=[17, 16], level=10), policy=TABLE)
    assert s.ui_key_args["profession_id"] == 16 and s.ui_key_args["option_index"] == 1


@pytest.mark.parametrize("policy", [DayStartPolicy(enabled=True, professions={}),
                                    DayStartPolicy(enabled=True, professions={"2:5": 14})])
def test_missing_or_mismatched_policy_blocks_without_touching(policy):
    state = DayStartState()
    s = step(state, menu("LevelUpMenu", "level_up_profession", professions=[12, 13], level=5), policy=policy)
    assert s.kind == "blocked" and s.reason == "profession_policy_missing" and s.ui_key_args is None
    assert state.blocked == "profession_policy_missing"
    assert step(state, menu("LevelUpMenu", "level_up_profession", professions=[12, 13], level=5)).kind == "blocked"


def test_P1_default_profession_policy_is_neutralised():
    assert DEFAULT_PROFESSION_POLICY == {} and POLICY.professions == {}
    for skill in range(5):
        for options in ((skill * 6, skill * 6 + 1), (skill * 6 + 2, skill * 6 + 3), (skill * 6 + 4, skill * 6 + 5)):
            level = 5 if options[0] == skill * 6 else 10
            assert POLICY.profession_for(skill, level, options) is None
            assert TABLE.profession_for(skill, level, options) in options
    assert TABLE.profession_for(5, 5, (30, 31)) is None  # luck never offers a chooser


def test_P1_policy_config_rejects_a_profession_table():
    assert DayStartPolicy.from_config({"stardew_day_start_handler_enabled": True}).enabled
    assert not DayStartPolicy.from_config({}).enabled
    assert DayStartPolicy.from_config({"stardew_profession_policy": {}}).professions == {}
    for table in ({"2:5": 13}, ["2:5"]):
        with pytest.raises(ValueError, match="neutralised"):
            DayStartPolicy.from_config({"stardew_profession_policy": table})


@pytest.mark.parametrize("menu_type", ["SaveGameMenu", "ReadyCheckDialog"])
def test_save_and_ready_check_wait(menu_type):
    state = DayStartState()
    s = step(state, menu(menu_type))
    assert s.kind == "wait" and s.reason == "wait_only:" + menu_type and state.active


def test_dialogue_only_inside_the_day_window():
    assert step(DayStartState(), menu("DialogueBox", "dialogue", showing=False)).kind == "idle"
    state = DayStartState(date_changed_at=100.0)
    assert step(state, menu("DialogueBox", "dialogue", showing=False), now=130).kind == "act"
    late = DayStartState(date_changed_at=100.0)
    assert step(late, menu("DialogueBox", "dialogue", showing=False), now=200).kind == "idle"
    assert step(DayStartState(), menu("DialogueBox", "dialogue", showing=True)).kind == "act"


def test_letter_close_in_window():
    state = DayStartState(date_changed_at=0.0)
    s = step(state, menu("LetterViewerMenu", "letter", showing=False), now=5)
    assert s.kind == "act" and s.ui_key_args["key"] == "close"


@pytest.mark.parametrize("kind", ["question", "npc_question"])
def test_questions_are_never_auto_answered(kind):
    state = DayStartState()
    s = step(state, menu("DialogueBox", kind, options=["Yes", "No"]))
    assert s.kind == "done" and s.reason == "question_left_to_model" and s.ui_key_args is None


def test_bounds_actions_wall_no_effect_uncertain():
    state = DayStartState()
    for i in range(8):
        s = step(state, menu("DialogueBox", "dialogue", receiver=f"rcv:mi{i}"), now=i, revision=i)
        assert s.kind == "act"
        note_result(state, s, outcome="UiKeyApplied", uncertain=False, context_revision=i)
    s = step(state, menu("DialogueBox", "dialogue", receiver="rcv:mi99"), now=9, revision=99)
    assert s.kind == "blocked" and s.reason == "action_bound"

    state = DayStartState()
    assert step(state, menu("ShippingMenu", "shipping_summary", ready=False, reason="intro"), now=0).kind == "wait"
    assert step(state, menu("ShippingMenu", "shipping_summary", ready=False, reason="intro"), now=241).reason == "wall_bound"
    # WP P1 (ADR-19 item 4): the native OK ("saving") starts the bounded host-sync grace instead.
    state = DayStartState()
    assert step(state, menu("ShippingMenu", "shipping_summary", ready=False, reason="saving"), now=0).kind == "wait"
    assert state.host_sync_pending and step(state, menu("ShippingMenu", "shipping_summary", ready=False,
                                                          reason="saving"), now=241).kind == "wait"

    state = DayStartState()
    for i in range(2):
        s = step(state, menu("DialogueBox", "dialogue"), now=i, revision=i)
        note_result(state, s, outcome="UiKeyNoEffect", uncertain=False, context_revision=i)
    s = step(state, menu("DialogueBox", "dialogue"), now=3, revision=5)
    assert s.kind == "blocked" and s.reason == "ui_key_no_effect"

    state = DayStartState()
    s = step(state, menu("DialogueBox", "dialogue"))
    note_result(state, s, outcome=None, uncertain=True, context_revision=1)
    assert state.blocked == "ui_key_uncertain"
    assert step(state, menu("DialogueBox", "dialogue", receiver="rcv:mi2"), now=1, revision=2).kind == "blocked"


def test_stack_order_level_up_level_up_shipping_save_done():
    state = DayStartState()
    trace = []
    frames = [menu("LevelUpMenu", "level_up", receiver="rcv:mi1", pending=2),
              menu("LevelUpMenu", "level_up", receiver="rcv:mi2", skill=3, pending=1),
              menu("ShippingMenu", "shipping_summary", receiver="rcv:mi3"),
              menu("ShippingMenu", "shipping_summary", receiver="rcv:mi3", ready=False, reason="saving"),
              menu("SaveGameMenu", receiver="rcv:mi4"),
              free(showing=False)]
    for revision, frame in enumerate(frames):
        s = step(state, frame, now=revision, revision=revision)
        trace.append((s.kind, s.menu_type))
        if s.kind == "act":
            note_result(state, s, outcome="UiKeyApplied", uncertain=False, context_revision=revision)
    # WP P1: N1's ProgressionClient confirms LevelUp/Shipping; the legacy rule only waits the stack through.
    assert trace == [("wait", "LevelUpMenu"), ("wait", "LevelUpMenu"), ("wait", "ShippingMenu"),
                     ("wait", "ShippingMenu"), ("wait", "SaveGameMenu"), ("done", None)]
    assert state.finished == "cleared" and state.actions == 0
    assert state.menus == ["LevelUpMenu", "ShippingMenu", "SaveGameMenu"]


def test_no_menu_and_no_episode_is_idle_and_disabled_policy_never_acts():
    assert step(DayStartState(), free()).kind == "idle"
    disabled = DayStartPolicy(enabled=False)
    assert step(DayStartState(), menu("LevelUpMenu", "level_up"), policy=disabled).kind == "idle"


def test_player_not_free_after_last_menu_waits():
    state = DayStartState()
    step(state, menu("LevelUpMenu", "level_up"))
    facts = {"context_generation": 3, "mode": "gameplay", "is_player_free": False,
             "day_transition": {"showing_end_of_night_stuff": False, "end_of_night_menus_pending": 0}}
    assert plan_day_start_step(facts, None, POLICY, state, now=1).reason == "player_not_free"


@pytest.mark.parametrize("skill,l5,options,expected", [
    (0, 0, (2, 3), 2), (0, 1, (4, 5), 4),        # Farming: Rancher->Coopmaster, Tiller->Artisan
    (1, 6, (8, 9), 8), (1, 7, (10, 11), 10),     # Fishing: Fisher->Angler, Trapper->Mariner
    (2, 12, (14, 15), 14), (2, 13, (16, 17), 16),  # Foraging: Forester->Lumberjack, Gatherer->Botanist
    (3, 18, (20, 21), 20), (3, 19, (22, 23), 23),  # Mining: Miner->Blacksmith, Geologist->Gemologist
    (4, 24, (26, 27), 26), (4, 25, (28, 29), 29),  # Combat: Fighter->Brute, Scout->Desperado
])
def test_level_10_policy_for_every_skill_and_both_paths(skill, l5, options, expected):
    assert TABLE.profession_for(skill, 10, options) == expected
    assert TABLE.profession_for(skill, 10, tuple(reversed(options))) == expected  # by id, not side
    s = step(DayStartState(), menu("LevelUpMenu", "level_up_profession", professions=list(reversed(options)),
                                   skill=skill, level=10), policy=TABLE)
    assert s.kind == "act" and s.ui_key_args["profession_id"] == expected
    assert s.ui_key_args["option_index"] == list(reversed(options)).index(expected)


def test_finished_episode_never_rearms_on_the_same_screens():
    state = DayStartState()
    assert step(state, menu("DialogueBox", "question", options=["Yes", "No"]), now=0).kind == "done"
    started = state.started_at
    # Another menu without an intervening no-menu frame: still finished, no re-stamp, no act.
    for t in (1, 2, 500):
        s = step(state, menu("LevelUpMenu", "level_up", receiver="rcv:mi2"), now=t, revision=t)
        assert s.kind == "done" and s.ui_key_args is None and not state.active and state.started_at == started
    # A no-menu frame ends it; the next day-start menu starts a fresh, bounded episode.
    assert step(state, free(), now=600).kind == "idle" and state.finished is None
    s = step(state, menu("LevelUpMenu", "level_up", receiver="rcv:mi3"), now=601, revision=700)
    assert s.kind == "wait" and state.active and state.started_at == 601


def test_refusal_before_any_write_is_not_no_effect_and_does_not_wait():
    state = DayStartState()
    for i in range(3):
        s = step(state, menu("DialogueBox", "dialogue"), now=i, revision=5)
        assert s.kind == "act", s
        note_result(state, s, outcome=None, uncertain=False, context_revision=5, submitted=False)
    assert state.no_effect == {} and state.awaiting_newer_than is None and state.actions == 3
    # Still bounded by max_actions.
    for i in range(5):
        s = step(state, menu("DialogueBox", "dialogue"), now=10 + i, revision=5)
        note_result(state, s, outcome=None, uncertain=False, context_revision=5, submitted=False)
    assert step(state, menu("DialogueBox", "dialogue"), now=20, revision=5).reason == "action_bound"
