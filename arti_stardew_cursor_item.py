"""Generic cursor settle choices and conservation receipts. No menu/feature rules."""
import copy
import json
from collections.abc import Mapping


def parse_cursor_settle(raw, component_ids):
    if raw is None:
        return None
    if (not isinstance(raw, Mapping) or raw.get("status") not in ("Empty", "NeedsChoice", "BlockedInventory", "Uncertain")
            or type(raw.get("complete")) is not bool or not isinstance(raw.get("targets"), (list, tuple))
            or len(raw["targets"]) > 512):
        return None
    cursor = raw.get("cursor")
    if (raw["status"] == "Empty" and cursor is not None
            or raw["status"] in ("NeedsChoice", "BlockedInventory") and cursor is None):
        return None
    if cursor is not None:
        if (not isinstance(cursor, Mapping) or not isinstance(cursor.get("item"), str) or not cursor["item"]
                or type(cursor.get("stack")) is not int or cursor["stack"] < 1
                or type(cursor.get("quality")) is not int or cursor["quality"] not in (0,1,2,4)
                or type(cursor.get("is_tool")) is not bool or type(cursor.get("quest_item")) is not bool):
            return None
    seen = set()
    for target in raw["targets"]:
        if (not isinstance(target, Mapping) or type(target.get("id")) is not int or target["id"] not in component_ids
                or target["id"] in seen or target.get("kind") not in ("inventory", "container", "swap", "drop", "trash")
                or type(target.get("room")) is not int or target["room"] < 1
                or not isinstance(target.get("token"), str) or not 0 < len(target["token"]) <= 128):
            return None
        if target["kind"] == "swap" and not (isinstance(target.get("swap"), Mapping)
                                             and isinstance(target["swap"].get("item"), str) and target["swap"]["item"]):
            return None  # PM-1 fix14b: a swap always names the bag item that comes back on the cursor
        seen.add(target["id"])
    return copy.deepcopy(dict(raw))


def disposal_refusal(cursor):
    """Only the owner's tools/quest-item denylist. No price, rarity or preference gate."""
    if not isinstance(cursor, Mapping):
        return "cursor_unreadable"
    if cursor.get("is_tool") is True or str(cursor.get("item", "")).startswith(("(T)", "(W)")):
        return "tool"
    if cursor.get("quest_item") is True:
        return "quest_item"
    if type(cursor.get("is_tool")) is not bool or type(cursor.get("quest_item")) is not bool:
        return "cursor_unreadable"
    return None


def held_disposal_refusal(cursor, kind, target=None):
    """PM-1 fix14b (ADR 33): native's own refusal for this held-item target first. A drop keeps the item in the world:
    only the game's rule (trashable; quest items never). A trash destroys it: the owner's denylist on top."""
    if isinstance(target, Mapping) and target.get("refused"):
        return str(target["refused"])
    if not isinstance(cursor, Mapping):
        return "cursor_unreadable"
    if cursor.get("quest_item") is True:
        return "quest_item"
    if cursor.get("trashable") is False:
        return "game_keeps_item"
    if kind == "drop" and cursor.get("trashable") is True:
        return None
    return disposal_refusal(cursor)


def settle_cursor_item(view, destination_id):
    block = view.cursor_settle
    if not view.generic or not view.available or not block or block.get("complete") is not True:
        raise ValueError("cursor_readback_unavailable")
    if not block.get("cursor"):
        return None  # already empty: observed no-input result
    target = next((t for t in block["targets"] if t["id"] == destination_id and t["kind"] != "trash"), None)
    if target is None:
        raise ValueError("cursor_destination_unavailable")
    if target["kind"] == "drop" and (why := held_disposal_refusal(block["cursor"], "drop", target)):
        raise ValueError("item_policy:" + why)
    return "ui_press", view.press_args("A", choice=destination_id, settle=True)


def close_step(view, destination_id=None):
    """One state-machine step. The caller executes it, re-inspects, then calls again.

    No destructive default, no chosen first/last slot. A held cursor asks for a
    destination; after a verified empty cursor, the ordinary close is offered.
    """
    if view.held_item is None and not (view.cursor_settle or {}).get("cursor"):
        return dict(status="Closing", input=("ui_press", view.press_args("B")))
    block = view.cursor_settle or {}
    if block.get("complete") is not True:
        return dict(status="Uncertain", refusal="cursor_readback_unavailable", input=None, choices=[])
    choices = [t for t in block.get("targets", ()) if t["kind"] != "trash"
               and not (t["kind"] == "drop" and held_disposal_refusal(block.get("cursor"), "drop", t))]
    if destination_id is None:
        return dict(status="NeedsChoice" if choices else "BlockedInventory", choices=copy.deepcopy(choices), input=None)
    return dict(status="Settling", input=settle_cursor_item(view, destination_id), close_after="cursor_empty_and_conserved")


def readback(raw):
    """Native settle report. The leased executor authenticates its envelope."""
    if not isinstance(raw, str) or len(raw) > 131072:
        return None
    try:
        cell = json.loads(raw).get("cursor_settle")
        if (not isinstance(cell, Mapping) or cell.get("Status") not in ("Settled", "Partial", "Swapped", "NoEffect", "Uncertain")
                or type(cell.get("Conserved")) is not bool or type(cell.get("Moved")) is not int):
            return None
        if cell["Status"] in ("Settled", "Partial", "Swapped") and (not cell["Conserved"] or cell["Moved"] <= 0):
            return None
        return copy.deepcopy(dict(cell))
    except (ValueError, TypeError, AttributeError):
        return None
