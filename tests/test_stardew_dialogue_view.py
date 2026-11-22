"""Issue #174 — cloud/no-game tests for the pure presented-dialogue view."""

from __future__ import annotations

import ast
import copy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from arti_stardew_dialogue_view import derive_presented_dialogue

P2 = -100000000000000001
CMD_SESSION = "cmdinbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUNTIME = "runtime-g3-001"


def _context(**overrides: object) -> dict:
    facts = {
        "runtime_instance_id": RUNTIME,
        "context_generation": 7,
        "session_id": CMD_SESSION,
        "player_id": P2,
        "screen_id": 0,
        "ui_revision": 11,
        "viewport_transform_revision": 3,
        "location_id": "Town",
        "mode": "menu",
        "is_player_free": False,
        "can_player_move": False,
        "capabilities": {
            "inspect_tiles": {"available": True},
            "inspect_context": {"available": True},
            "ui_navigate": {"available": True},
        },
        "interactive": {
            "receiver_token": "rcv:mi7",
            "page_token": "pg:mi7:DialogueBox",
            "menu_type": "DialogueBox",
            "page_type": "DialogueBox",
            "snap_id": 42,
            "focus_key": "dlg:42",
            "focus_target_token": "tgt:mi7:dlg:42",
            "visible_text": "Mau ikut ke festival?",
            "speaker": "Abigail",
        },
    }
    facts.update(overrides)
    return facts


def _occ(
    seq: int = 7,
    *,
    text: str = "Mau ikut ke festival?",
    status: str = "active",
    instance_token: str = "mi7",
    reveal_status: str = "full",
    complete: bool = True,
    replayed: bool = False,
    options: list[dict] | None = None,
) -> dict:
    return {
        "occurrence_seq": seq,
        "family": "dialogue",
        "instance_token": instance_token,
        "page_type": "DialogueBox",
        "source": "DialogueBox.getCurrentString",
        "text": text,
        "text_revision": 3,
        "reveal_status": reveal_status,
        "status": status,
        "complete": complete,
        "replayed": replayed,
        "actionable": False,
        "first_capture_ticks": 100,
        "last_capture_ticks": 104,
        "origin_occurrence_seq": seq,
        "speaker": "Abigail",
        "options": options if options is not None else [],
    }


def _text_facts(*, occ: dict | None = None, active_seqs: list[int] | None = None) -> dict:
    occurrence = occ if occ is not None else _occ()
    seq = occurrence["occurrence_seq"]
    return {
        "binding": {
            "runtime_instance_id": RUNTIME,
            "session_id": CMD_SESSION,
            "player_id": P2,
            "screen_id": 0,
            "location_id": "Town",
            "context_generation": 7,
            "ui_revision": 11,
            "menu_instance_token": "mi7",
            "capture_ticks": 104,
        },
        "meta": {
            "cursor_occurrence_seq": seq,
            "floor_lost_before_seq": 1,
            "dropped_count": 0,
            "replayed_history": False,
            "active_seqs": active_seqs if active_seqs is not None else [seq],
        },
        f"occ:{seq}": occurrence,
    }


def _choices() -> list[dict]:
    return [
        {"index": 0, "label": "Ya", "selected": True},
        {"index": 1, "label": "Nanti saja", "selected": False},
    ]


def test_current_fully_presented_dialogue_is_readable():
    view = derive_presented_dialogue(_text_facts(), _context())

    assert view.available
    assert view.readable
    assert view.current
    assert not view.actionable
    assert view.text == "Mau ikut ke festival?"
    assert view.speaker == "Abigail"
    assert view.source == "DialogueBox.getCurrentString"
    assert view.occurrence_seq == 7
    assert view.instance_token == "mi7"
    assert view.context_matches
    assert view.provenance == "DERIVED"


def test_complete_displayed_options_are_actionable_only_with_current_selectable_context():
    facts = _text_facts(occ=_occ(options=_choices()))
    view = derive_presented_dialogue(facts, _context())

    assert view.current
    assert view.capture_complete
    assert view.options_complete
    assert [item.label for item in view.options] == ["Ya", "Nanti saja"]
    assert view.actionable

    context = _context()
    context["capabilities"] = {
        **context["capabilities"],
        "ui_navigate": {"available": False, "reason": "backend_disabled"},
    }
    blocked = derive_presented_dialogue(facts, context)
    assert blocked.current
    assert not blocked.actionable
    assert "context_not_selectable" in blocked.blockers


def test_incomplete_or_truncated_choice_set_stays_readable_but_not_actionable():
    view = derive_presented_dialogue(
        _text_facts(occ=_occ(options=_choices(), complete=False)),
        _context(),
    )
    assert view.available and view.readable and view.current
    assert not view.capture_complete
    assert not view.options_complete
    assert not view.actionable
    assert "capture_incomplete" in view.blockers


def test_expired_and_replayed_history_never_becomes_actionable():
    occ = _occ(status="expired", replayed=True, options=_choices())
    facts = _text_facts(occ=occ, active_seqs=[])
    facts["meta"]["replayed_history"] = True
    view = derive_presented_dialogue(facts, _context(), occurrence_seq=7)
    assert view.available and view.readable
    assert not view.current
    assert not view.actionable
    assert view.status == "expired"
    assert view.replayed
    assert "history_or_expired" in view.blockers
    assert "replayed_history" in view.blockers


def test_identical_strings_preserve_distinct_occurrence_and_instance_identity():
    same = "Sama persis."
    facts = _text_facts(occ=_occ(seq=8, text=same, options=_choices()), active_seqs=[8])
    facts["meta"]["cursor_occurrence_seq"] = 8
    facts["occ:7"] = _occ(seq=7, text=same, status="expired", instance_token="mi6", options=_choices())
    old = derive_presented_dialogue(facts, _context(), occurrence_seq=7)
    new = derive_presented_dialogue(facts, _context(), occurrence_seq=8)
    assert old.text == new.text == same
    assert old.occurrence_seq == 7
    assert new.occurrence_seq == 8
    assert old.instance_token == "mi6"
    assert new.instance_token == "mi7"
    assert old != new
    assert not old.actionable
    assert new.actionable


def test_typewriter_prefix_is_current_visible_text_but_not_a_complete_page():
    view = derive_presented_dialogue(_text_facts(occ=_occ(text="Mau ikut ke fest", reveal_status="prefix", options=_choices())), _context())
    assert view.readable
    assert view.current
    assert view.text == "Mau ikut ke fest"
    assert not view.reveal_complete
    assert not view.actionable
    assert "reveal_incomplete" in view.blockers


@pytest.mark.parametrize(("pin", "bad_value"), [("runtime_instance_id", "runtime-other"), ("session_id", "cmdinbox-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), ("player_id", P2 - 1), ("screen_id", 1), ("context_generation", 8), ("ui_revision", 12)])
def test_context_identity_pin_mismatch_fails_closed(pin: str, bad_value: object):
    context = _context(**{pin: bad_value})
    view = derive_presented_dialogue(_text_facts(occ=_occ(options=_choices())), context)
    assert view.readable
    assert not view.current
    assert not view.context_matches
    assert not view.actionable
    assert f"context_mismatch:{pin}" in view.blockers


def test_instance_mismatch_fails_closed():
    view = derive_presented_dialogue(_text_facts(occ=_occ(instance_token="mi-other", options=_choices())), _context())
    assert view.readable
    assert not view.current
    assert not view.context_matches
    assert not view.actionable
    assert "context_mismatch:occurrence_instance_token" in view.blockers


def test_unknown_additive_fields_and_producer_actionable_cannot_elevate_privilege():
    occ = _occ(options=_choices())
    occ["actionable"] = True
    occ["future_can_act"] = True
    facts = _text_facts(occ=occ)
    facts["future_root_capability"] = {"available": True}
    context = _context()
    context["capabilities"] = {**context["capabilities"], "ui_navigate": {"available": False, "reason": "not_implemented"}, "future_select_anything": {"available": True}}
    view = derive_presented_dialogue(facts, context)
    assert view.current
    assert not view.actionable
    assert "context_not_selectable" in view.blockers


def test_empty_or_unavailable_facts_are_explicitly_unavailable():
    view = derive_presented_dialogue({}, {})
    assert not view.available
    assert not view.readable
    assert not view.current
    assert not view.actionable
    assert view.text is None
    assert view.options == ()
    assert view.blockers == ("ui_text_unavailable",)
    no_dialogue = derive_presented_dialogue({"binding": {}, "meta": {}, "menu:1": {"family": "menu_label"}}, _context())
    assert not no_dialogue.available
    assert no_dialogue.blockers == ("dialogue_unavailable",)


def test_malformed_options_are_not_promoted_to_a_complete_choice_set():
    bad = _choices()
    bad[1] = {"index": 0, "label": "duplicate", "selected": False}
    view = derive_presented_dialogue(_text_facts(occ=_occ(options=bad)), _context())
    assert view.readable and view.current
    assert not view.options_complete
    assert not view.actionable
    assert "options_invalid" in view.blockers


def test_occurrence_identity_mismatch_is_not_current_or_actionable():
    facts = _text_facts(occ=_occ(seq=8, options=_choices()), active_seqs=[8])
    facts["occ:7"] = facts.pop("occ:8")
    view = derive_presented_dialogue(facts, _context())
    assert view.readable
    assert not view.current
    assert not view.actionable
    assert "occurrence_identity_invalid" in view.blockers


def test_origin_occurrence_mismatch_is_not_current_or_actionable():
    occ = _occ(options=_choices())
    occ["origin_occurrence_seq"] = 6
    view = derive_presented_dialogue(_text_facts(occ=occ), _context())
    assert view.readable
    assert not view.current
    assert not view.actionable
    assert "origin_occurrence_mismatch" in view.blockers


def test_missing_replayed_field_is_not_current_or_actionable():
    occ = _occ(options=_choices())
    occ.pop("replayed")
    view = derive_presented_dialogue(_text_facts(occ=occ), _context())
    assert view.readable
    assert not view.current
    assert not view.actionable
    assert "replayed_invalid" in view.blockers


def test_invalid_text_revision_is_not_current_or_actionable():
    occ = _occ(options=_choices())
    occ["text_revision"] = 0
    view = derive_presented_dialogue(_text_facts(occ=occ), _context())
    assert view.readable
    assert not view.current
    assert not view.actionable
    assert "text_revision_invalid" in view.blockers


def test_result_is_immutable_inputs_are_not_mutated_and_module_has_no_side_effect_imports():
    facts = _text_facts(occ=_occ(options=_choices()))
    context = _context()
    before_facts = copy.deepcopy(facts)
    before_context = copy.deepcopy(context)
    view = derive_presented_dialogue(facts, context)
    assert facts == before_facts
    assert context == before_context
    assert isinstance(view.options, tuple)
    with pytest.raises(FrozenInstanceError):
        view.actionable = False  # type: ignore[misc]
    module_path = Path(__file__).parents[1] / "arti_stardew_dialogue_view.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert not imported_roots.intersection({"os", "pathlib", "socket", "subprocess", "threading", "requests", "urllib"})
