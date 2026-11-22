"""WS2 pure ui_key read model: fail-closed on any doubt. UNIT_TESTED."""
import copy

import pytest

import arti_stardew_command as C
from arti_stardew_ui import baca_ui_key

T1 = "uk:" + "1" * 32
T2 = "uk:" + "2" * 32


def facts(**block_changes):
    block = {"receiver_token": "rcv:mi7", "menu_type": "DialogueBox", "kind": "question", "ready": True,
             "wait_only": False, "keys": [{"key": "answer", "target_token": T1}, {"key": "cancel", "target_token": T2}],
             "options": [{"index": 0, "label": "Yes", "response_key": "Yes"},
                         {"index": 1, "label": "No", "response_key": "No"}]}
    block.update(block_changes)
    return {"context_generation": 5, "mode": "menu",
            "interactive": {"receiver_token": "rcv:mi7", "menu_type": "DialogueBox"},
            "capabilities": {"ui_key": {"implemented": True, "available": True, "reason": "ready"}},
            "day_transition": {"showing_end_of_night_stuff": False, "end_of_night_menus_pending": 0},
            "ui_key": block}


def test_valid_question_view_and_args():
    view = baca_ui_key(facts(), age_s=0.1)
    assert view is not None and view.kind == "question" and view.available and len(view.options) == 2
    assert view.args_for("answer", 1) == dict(receiver_token="rcv:mi7", menu_type="DialogueBox", kind="question",
                                              key="answer", target_token=T1, option_index=1)
    assert view.args_for("cancel")["target_token"] == T2
    with pytest.raises(C.KesalahanPerintah):
        view.args_for("answer", 2)          # option not published
    with pytest.raises(C.KesalahanPerintah):
        view.args_for("advance")            # key not published for this kind


@pytest.mark.parametrize("age,authoritative,complete", [(2.5, True, True), (None, True, True), (-1, True, True),
                                                        (0.1, False, True), (0.1, True, False)])
def test_stale_or_partial_context_fails_closed(age, authoritative, complete):
    assert baca_ui_key(facts(), age_s=age, authoritative=authoritative, complete=complete) is None


@pytest.mark.parametrize("mutate", [
    lambda f: f["interactive"].update(receiver_token="rcv:mi8"),     # receiver mismatch with interactive
    lambda f: f["interactive"].update(menu_type="LevelUpMenu"),      # menu mismatch
    lambda f: f["ui_key"].update(receiver_token="mi7"),               # raw serial
    lambda f: f["ui_key"]["keys"][0].update(target_token="uk:XYZ"),   # bad token shape
    lambda f: f["ui_key"]["keys"].pop(),                               # missing published key
    lambda f: f["ui_key"]["keys"][0].update(key="advance"),           # key outside kind
    lambda f: f["ui_key"].update(kind="mystery"),                     # unknown kind
    lambda f: f["ui_key"].update(menu_type="ShippingMenu"),
    lambda f: f["ui_key"].update(options=[{"index": i, "label": "x", "response_key": "x"} for i in range(9)]),
    lambda f: f["ui_key"]["options"][0].update(label="x" * 129),
    lambda f: f["ui_key"]["options"][1].update(index=5),
    lambda f: f["ui_key"].update(options=None),                       # ready question without options
    lambda f: f["ui_key"].update(ready="yes"),
    lambda f: f.update(context_generation=0),
    lambda f: f.pop("capabilities"),
    lambda f: f["day_transition"].update(end_of_night_menus_pending=-1),
])
def test_bad_shapes_fail_closed(mutate):
    value = copy.deepcopy(facts())
    mutate(value)
    assert baca_ui_key(value, age_s=0.1) is None


def test_wait_only_menus_are_never_actionable():
    value = facts(menu_type="SaveGameMenu", kind=None, ready=False, wait_only=True, not_ready_reason="wait_only",
                  keys=None, options=None)
    value["interactive"]["menu_type"] = "SaveGameMenu"
    view = baca_ui_key(value, age_s=0.1)
    assert view is not None and view.wait_only and view.kind is None and not view.available and view.keys == ()
    with pytest.raises(C.KesalahanPerintah):
        view.args_for("confirm")
    # A wait-only block that claims a kind or readiness is rejected.
    bad = copy.deepcopy(value)
    bad["ui_key"]["ready"] = True
    assert baca_ui_key(bad, age_s=0.1) is None


def test_capability_unavailable_keeps_view_but_not_available():
    value = facts()
    value["capabilities"]["ui_key"].update(available=False, reason="uncertain_latched")
    view = baca_ui_key(value, age_s=0.1)
    assert view is not None and not view.available


def test_profession_chooser_view():
    value = facts(menu_type="LevelUpMenu", kind="level_up_profession", keys=[{"key": "choose", "target_token": T1}],
                  options=None, professions=[{"index": 0, "profession_id": 13, "name": "Gatherer"},
                                             {"index": 1, "profession_id": 12, "name": "Forester"}], skill=2, level=5)
    value["interactive"]["menu_type"] = "LevelUpMenu"
    view = baca_ui_key(value, age_s=0.1)
    assert view is not None and view.skill == 2 and view.level == 5
    assert view.args_for("choose", 1, 12)["profession_id"] == 12
    with pytest.raises(C.KesalahanPerintah):
        view.args_for("choose", 0, 12)      # profession id must sit at that observed index
    bad = copy.deepcopy(value)
    bad["ui_key"]["professions"].pop()
    assert baca_ui_key(bad, age_s=0.1) is None
