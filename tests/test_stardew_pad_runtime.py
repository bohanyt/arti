"""Primitives v2 WS-G0 slice 2 (runtime side): pad_button command contract, the shared journal
vector with the mod, and the presentation-only pad labelling of the decision catalog.

Never game proof: evidence ceiling UNIT_TESTED.
"""
import copy
from pathlib import Path

import pytest

import arti_stardew_command as command
import arti_stardew_pad_runtime as pad
import arti_stardew_runtime as runtime

DIGEST_BASE = dict(op="input_once", runtime_instance_id="runtime", session_id="session", owner_id="owner",
                   generation=1, control_seq=1, context_generation=7, expected_player_id=42, expected_player_name="arti",
                   expected_screen_id=0, expected_location_id="farm", family="world_key", primitive="pad_button",
                   deadline_unix_ms=1000)

ROW = {
    "stick": {"family": "move_cardinal", "detail": "runs"}, "A": {"family": "world_interact", "detail": "grab_tile"},
    "X": {"family": "use_tool", "detail": "faced_tile"}, "B": {"refused": "NoMeaning:world_b"},
    "Y": {"family": "pad_button", "detail": "GameMenu:crafting"}, "LB": {"family": "pad_button", "detail": "shiftToolbar:left"},
    "RB": {"family": "pad_button", "detail": "shiftToolbar:right"}, "LT": {"family": "select_item", "detail": "slot=11"},
    "RT": {"family": "select_item", "detail": "slot=1"}, "Start": {"family": "pad_button", "detail": "GameMenu:inventory"},
    "Select": {"refused": "NoMeaning:select_journal_off"}, "select_slot": {"family": "select_item", "detail": "slot"},
}


def facts(enabled=True, owned=True, buttons=ROW):
    return {"pad": {"enabled": enabled, "mode_owned": owned, "restore_pending": False, "cursor_hidden": True,
                    "auto_run": True, "context": "World", "held": "tool", "buttons": copy.deepcopy(buttons)}}


# ---------------------------------------------------------------- command contract

def test_pad_button_args_exact_shape():
    for button in command.PAD_BUTTONS:
        command.validate_pad_button_args({"button": button})
    for broken, code in (({}, "invalid_pad_button_args"), ({"button": "LB", "ticks": 1}, "invalid_pad_button_args"),
                         ({"button": "B"}, "invalid_pad_button_button"), ({"button": "A"}, "invalid_pad_button_button"),
                         ({"button": "X"}, "invalid_pad_button_button")):
        with pytest.raises(command.KesalahanPerintah, match=code):
            command.validate_pad_button_args(broken)


def test_shared_native_digest_vectors_bind_the_button():
    """Same vectors as stardew-mod ProtocolTests PadInputTests (C# == Python); ticks canonicalize to 1."""
    lb = dict(DIGEST_BASE, args={"button": "LB"})
    y = dict(DIGEST_BASE, args={"button": "Y"})
    assert command._journal_payload_digest(lb) == "dd23d7188c65a17a9ffe5e83f7c603ec07fe5ef1079cc2f9aa94f847828f1180"
    assert command._journal_payload_digest(y) == "317d4810f0df3c5f87303b28a77e3b86a49420337c1652e35920f9f83b33ca13"


def test_existing_vectors_unchanged():
    """The v1 digests stay byte-identical (the pad_button branch adds no line to other primitives)."""
    chest = dict(DIGEST_BASE, primitive="world_interact", args=dict(
        target_token="chest-target", source_fact_id="obj:Farm:3:9:(BC)130", tile_x=3, tile_y=9,
        actor_tile_x=3, actor_tile_y=8, facing=2, kind="chest"))
    assert command._journal_payload_digest(chest) == "a02eda988caf39038795373a909647440119f517992ad800821c0d93ad9bf150"


@pytest.mark.parametrize("button,outcome,success", [
    ("LB", "ToolbarShifted", True), ("RB", "ToolbarShifted", True), ("Start", "MenuOpened", True),
    ("Y", "MenuOpened", True), ("Select", "MenuOpened", True), ("LB", "MenuOpened", False),
    ("Start", "ToolbarShifted", False), ("RB", "PadButtonNoEffect", False)])
def test_success_outcome_is_per_button(button, outcome, success):
    result = command._hasil_dari_envelope(dict(status="completed", success=True, outcome=outcome, op="input_once",
                                               primitive="pad_button", input_args={"button": button}), "r")
    assert result.sukses is success


def test_session_gate_for_pad_button():
    base = dict(session_id="s", player_id=1, player_name="a", location_id="farm", screen_id=0, is_main_player=False,
                is_split_screen=False, inbox_enabled=True, menu_step_enabled=False, water_once_enabled=False,
                world_press_enabled=True, world_ready=True, inflight_request_id=None, input_busy=False)
    off = command.SesiPerintah(**base, mentah={})
    on = command.SesiPerintah(**base, mentah={"pad_button_enabled": True})
    assert "PadButtonInputEnabled" in off.alasan_belum_siap(command.OP_INPUT_ONCE, primitive=command.PRIMITIVE_PAD_BUTTON)
    assert on.alasan_belum_siap(command.OP_INPUT_ONCE, primitive=command.PRIMITIVE_PAD_BUTTON) is None


def test_minta_pad_button_writes_world_key_payload(tmp_path, monkeypatch):
    captured = {}

    def fake_input_once(arah, ticks, **kwargs):
        captured.update(arah=arah, ticks=ticks, **kwargs)
        return "sent"

    monkeypatch.setattr(command, "_minta_input_once", fake_input_once)
    assert command.minta_pad_button(button="Y", owner_id="o", generation=1, control_seq=1, context_generation=1) == "sent"
    assert captured["_pad_button_target"] == {"button": "Y"} and captured["ticks"] == 1 and captured["arah"] == ""
    with pytest.raises(command.KesalahanPerintah):
        command.minta_pad_button(button="B", owner_id="o", generation=1, control_seq=1, context_generation=1)


# ---------------------------------------------------------------- presentation (catalog)

def test_pad_context_only_when_the_mod_owns_gamepad_mode():
    assert pad.pad_context_from_facts({}) is None  # flag off: no pad fact
    assert pad.pad_context_from_facts(facts(enabled=False)) is None
    assert pad.pad_context_from_facts(facts(owned=False)) is None  # not taken yet
    assert pad.pad_context_from_facts(facts(buttons={"A": {"oops": 1}})) is None  # malformed row: ignored
    ctx = pad.pad_context_from_facts(facts())
    assert ctx["buttons"]["B"] == {"refused": "NoMeaning:world_b"} and ctx["cursor_hidden"] is True


def test_catalog_labels_offered_actions_and_legend():
    actions = {"world_key.move_cardinal": {"args": {}}, "world_interact": {"observed_target": {}},
               "world_key.use_tool": {"args": {}}, "select_item": {"observed_targets": []}, "approach_target": {}}
    legend = pad.annotate_catalog(actions, pad.pad_context_from_facts(facts()))
    assert actions["world_key.move_cardinal"]["button"] == "stick" and actions["approach_target"]["button"] == "stick"
    assert actions["world_interact"]["button"] == "A" and actions["world_key.use_tool"]["button"] == "X"
    assert actions["select_item"]["button"] == "select_slot" and actions["select_item"]["triggers"] == {"LT": 11, "RT": 1}
    assert legend["A"] == "world_interact" and legend["B"] == "-" and legend["Y"] == "-" and legend["stick"] == "move_cardinal"
    assert set(legend) == set(ROW)


def test_flag_off_catalog_unchanged():
    actions = {"world_key.use_tool": {"args": {"ticks": {"maximum": 14}}}}
    before = copy.deepcopy(actions)
    assert pad.annotate_catalog(actions, None) is None and actions == before


def test_context_ref_unchanged_without_pad_context():
    """pad_context None is left out of the canonical form: flag-off context refs are byte-identical."""
    assert runtime._canonical({"a": 1, "pad_context": None}) == runtime._canonical({"a": 1})
    assert runtime._canonical({"a": 1, "pad_context": {"buttons": {}}}) != runtime._canonical({"a": 1})


def test_renderer_wiring_is_presentation_only():
    src = Path(runtime.__file__).read_text(encoding="utf-8")
    assert "pad_legend = _pad_runtime.annotate_catalog(supported_actions, snap.pad_context)" in src
    assert 'snapshot_payload.pop("pad_context", None)' in src
    assert 'user_payload["pad_buttons"] = pad_legend' in src
    assert "pad_context=self._pad_context(water_context or movement_context)," in src
    assert "class StardewGoalRuntime(HostPauseRuntimeMixin, UiKeyRuntimeMixin, ToolWorkRuntimeMixin, RoutineRuntimeMixin, PadRuntimeMixin," in src  # WS-G1/G4 append mixins
    assert ("EconomyRuntimeMixin, AutoActRuntimeMixin,\n                         FishingRuntimeMixin, WorldExtrasRuntimeMixin,"
            " CombatRuntimeMixin, SkillRuntimeMixin):") in src  # WS-G2 / auto-act / G4 / G6 / G5 / skills tahap 1 (last)


# ---------------------------------------------------------------- D-B1b-2 diagnostics

def test_approach_offer_journal_lists_targets_and_exit_candidates_on_change_only():
    """Live batch 1b D-B1b-2: the decision journal names the offered approach targets and the scene's exit
    candidates (with the navigation objective state) whenever they change; never on an unchanged offer."""
    from types import SimpleNamespace
    events = []
    stub = SimpleNamespace(_journal=events.append)
    facts = {"bdoor:Farm:3,9": {"kind": "building_door", "target_location": "Cabin", "enterable": True},
             "warp:Farm:0,5": {"kind": "warp", "target_location": "Town", "npc_only": False},
             "warp:Farm:9,9": {"kind": "warp", "target_location": "X", "npc_only": True},
             "obj:Farm:1,1:(O)16": {"kind": "object"}}
    offered = ({"ref": "obj:Farm:1,1:(O)16", "tile": [1, 1]},)
    objective = SimpleNamespace(navigation=True, blocker=None)
    runtime.StardewGoalRuntime._journal_approach_offer(stub, offered, facts, objective, 0)
    runtime.StardewGoalRuntime._journal_approach_offer(stub, offered, facts, objective, 0)
    assert len(events) == 1
    e = events[0]
    assert e["ev"] == "approach_targets_offered" and e["count"] == 1 and e["offered_exits"] == 0
    assert e["exit_candidates_total"] == 2 and {c["ref"] for c in e["exit_candidates"]} == {"bdoor:Farm:3,9", "warp:Farm:0,5"}
    assert e["navigation_objective"] is True and len(e["digest"]) == 16
    runtime.StardewGoalRuntime._journal_approach_offer(stub, (), facts, objective, 1)
    assert len(events) == 2 and events[1]["count"] == 0 and events[1]["blocked_refs"] == 1
