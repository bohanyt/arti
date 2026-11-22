"""Primitives v2 WS-G0 slice 2: the runtime side of the virtual-gamepad (pad) path.

Kept out of ``arti_stardew_runtime.py`` (several workstreams edit it): ``StardewGoalRuntime`` inherits
:class:`PadRuntimeMixin` and the decision renderer calls :func:`annotate_catalog`.

What it does (and does not):

* The translation table lives in the MOD (``Protocol/PadContextTable.cs``), never here, in the model or
  in the prompt rules. The mod publishes the current row in ``ui.context`` ``pad.buttons`` (per button
  ``{family, detail}`` or ``{refused}``). This module only READS it.
* Presentation only: when the mod says gamepad mode is owned (``pad.enabled`` and ``pad.mode_owned``),
  every offered v1 action is labelled with its controller button (stick / A / X / select_slot, LT/RT
  slots on ``select_item``) and the prompt gets a compact ``pad_buttons`` legend (button -> family, or
  "-" for a refused cell). The model still answers with the existing operation names: the decision
  contract, the validators and every journal digest are unchanged.
* With the flag off (no ``pad`` fact) nothing changes: no label, no legend, no snapshot key.
* The v1 cursor-inert park check was never a runtime gate (only the operator's ``park_cursor.py`` read
  ``cursor_inert``); on the pad path the mod enforces the cursor-hidden gate itself (``cursor_hidden`` in
  session.json / ui.context is informational here).

Evidence ceiling: UNIT_TESTED.
"""
from __future__ import annotations

import time
from collections.abc import Mapping

# Offered catalog action -> the controller button that executes it on the pad path (mod world row).
ACTION_BUTTONS = {
    "world_key.move_cardinal": "stick",
    "approach_target": "stick",
    "move_through": "stick",
    "world_interact": "A",
    "world_key.use_tool": "X",
    "select_item": "select_slot",
    "reflex_fish": "X",  # WS-G4: X with a rod = the fishing reflex (mod PadContextTable family reflex_fish)
    "reflex_combat": "X",  # WS-G5: X with a melee weapon = the combat reflex (PadContextTable family reflex_combat)
    "reflex_charge": "X",  # WS-G5: X held with an upgraded hoe / can (one charged release)
}
# Families that are a v1 action (the legend names them); pad_button cells stay informational here.
_V1_FAMILIES = {"move_cardinal", "world_interact", "use_tool", "select_item", "reflex_fish", "reflex_combat"}
_FRESH_S = 2.0
GUIDANCE = (" Pad: each offered action names its controller button (pad_buttons: button -> family, '-' = no"
            " meaning here); answer with the action name.")


def pad_context_from_facts(facts: Mapping | None) -> dict | None:
    """The ``ui.context`` pad fact when the mod owns gamepad mode, else None (flag off / not taken yet)."""
    pad = facts.get("pad") if isinstance(facts, Mapping) else None
    if not isinstance(pad, Mapping) or pad.get("enabled") is not True or pad.get("mode_owned") is not True:
        return None
    buttons = pad.get("buttons")
    if not isinstance(buttons, Mapping):
        return None
    cells = {}
    for name, cell in buttons.items():
        if not isinstance(name, str) or not isinstance(cell, Mapping):
            return None
        if "refused" in cell:
            cells[name] = {"refused": str(cell["refused"])}
        elif isinstance(cell.get("family"), str):
            cells[name] = {k: str(v) for k, v in cell.items() if k in ("family", "detail")}
        else:
            return None
    return {"buttons": cells, "context": str(pad.get("context") or ""),
            "cursor_hidden": pad.get("cursor_hidden") is True, "auto_run": pad.get("auto_run") is True}


def _trigger_slot(cell: Mapping | None) -> int | None:
    if not isinstance(cell, Mapping) or cell.get("family") != "select_item":
        return None
    detail = str(cell.get("detail") or "")
    if not detail.startswith("slot="):
        return None
    try:
        return int(detail[len("slot="):])
    except ValueError:
        return None


def annotate_catalog(supported_actions: dict, pad_context: dict | None) -> dict | None:
    """Label offered actions with their pad button (in place); return the legend, or None (no pad)."""
    if pad_context is None:
        return None
    buttons = pad_context["buttons"]
    for action, button in ACTION_BUTTONS.items():
        entry = supported_actions.get(action)
        if isinstance(entry, dict):
            entry["button"] = button
    selection = supported_actions.get("select_item")
    if isinstance(selection, dict):
        triggers = {name: slot for name in ("LT", "RT") if (slot := _trigger_slot(buttons.get(name))) is not None}
        if triggers:
            selection["triggers"] = triggers
    return {name: (cell["family"] if "family" in cell and cell["family"] in _V1_FAMILIES else "-")
            for name, cell in sorted(buttons.items())}


class PadRuntimeMixin:
    """Runtime hook: read the pad fact from the fresh ui.context of the pinned player."""

    def _pad_context(self, pin):
        if pin is None:
            return None
        rec = self._obs.get(pin.player_id, "ui.context")
        if (rec is None or not rec.is_complete or not rec.authoritative
                or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or not 0 <= time.monotonic() - rec.received_monotonic <= _FRESH_S):
            return None
        return pad_context_from_facts(rec.facts)

    def _v2_pad_path(self, pin=None) -> bool:
        """FIXB9 (D-B7-2 root cause, live V2B7ACC11b): pad mode is owned from the first pad admission per inactive session
        until focus regain / STOP / title / night save; there every continuation was discarded before any pad press was
        admitted (pad_watch owned=False / Auto), a chicken-and-egg loop: every v2 gate keyed on the mode-owned pad fact
        stayed off (no churn normalization, no combat_rule_direct, no combat activity). The v2 runtime
        gate is the mod's pad path (ui.context pad.enabled, any freshness / completeness); mode ownership only decides the
        button legend (``_pad_context``)."""
        try:
            if pin is not None and self._pad_context(pin) is not None:
                return True
        except Exception:  # noqa: BLE001 - a partial pin: fall back to the pad path fact
            pass
        try:
            pid = pin.player_id if pin is not None else self._binding_view()[1]
            rec = self._obs.get(pid, "ui.context") if pid is not None else None
            facts = rec.facts if rec is not None and isinstance(rec.facts, Mapping) else {}
            pad = facts.get("pad")
            return isinstance(pad, Mapping) and pad.get("enabled") is True
        except Exception:  # noqa: BLE001 - a gate probe never raises
            return False
