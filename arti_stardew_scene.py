"""Helper minimal OBS-1 untuk fact scene.current_location.

Tidak merancang ulang RichObservationStore. Fact bersifat opaq di store;
modul ini hanya menandai kind/ID semantik untuk tes dan inspeksi.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import time


def gather_window(record, *, session_id, screen_id, location_id):
    """Validate the scoped fact in the existing record, never a second store.

    A truncated *global* map may contain a complete bounded local window.
    A stale/lost delta or malformed local window can never prove absence.
    """
    if (record is None or not record.available or not record.authoritative
            or record.stale or record.requires_resync or record.session_id != session_id
            or record.screen_id != screen_id or not 0 <= time.monotonic() - record.received_monotonic <= 2):
        return None
    f = record.facts.get("gather:nearby")
    if not isinstance(f, Mapping) or f.get("kind") != "gather_window" or f.get("available") is not True:
        return None
    if (f.get("location_id") != location_id or type(f.get("captured_ticks")) is not int
            or f["captured_ticks"] < 0 or f.get("radius") != 1
            or type(f.get("using_tool")) is not bool or type(f.get("drops_complete")) is not bool):
        return None
    center, targets, drops = f.get("center"), f.get("targets"), f.get("drops")
    if (not isinstance(center, Mapping) or any(type(center.get(k)) is not int for k in ("x", "y"))
            or not isinstance(targets, Mapping) or len(targets) > 18
            or not isinstance(drops, (tuple, list)) or len(drops) > 16):
        return None
    for ref, target in targets.items():
        if not isinstance(ref, str) or len(ref) > 256 or not isinstance(target, Mapping): return None
        tile = target.get("tile")
        if (target.get("kind") not in {"object", "terrain"} or not isinstance(tile, Mapping)
                or any(type(tile.get(k)) is not int or abs(tile[k] - center[k]) > 1 for k in ("x", "y"))):
            return None
    for drop in drops:
        if not isinstance(drop, Mapping): return None
        pixel = drop.get("pixel")
        if (not isinstance(drop.get("id"), str) or len(drop["id"]) > 256
                or not isinstance(drop.get("qualified_item_id"), str) or len(drop["qualified_item_id"]) > 128
                or type(drop.get("quality")) is not int or not isinstance(pixel, Mapping)
                or any(type(pixel.get(k)) not in (int, float) or not math.isfinite(pixel[k]) for k in ("x", "y"))):
            return None
    use = f.get("last_use")
    if use is not None and (not isinstance(use, Mapping) or not isinstance(use.get("request_id"), str)
            or len(use["request_id"]) > 128
            or any(type(use.get(k)) is not bool for k in ("cycle_observed", "input_completed", "cleanup_confirmed"))):
        return None
    if not _tool_keys_valid(f, center):
        return None
    return f


def _tool_keys_valid(f, center) -> bool:
    """WS1 optional keys; absent is fine (older mod), malformed fails the whole window closed.

    tool_tile: the exact hit tile, within Chebyshev 2 of the center (facing left/right inside the
    outer 8 px band hits two tiles away; the runtime reports that as not adjacent).
    tiles: at most nine distinct 3x3 entries with bool tillable / water_refill / villager.
    refill_sources: at most four distinct native refill sightings within 28 tiles;
    these guide approach, never authorize a tool input outside the local window.
    """
    if "refill_sources" in f:
        sources = f["refill_sources"]
        if not isinstance(sources, (list, tuple)) or len(sources) > 4:
            return False
        seen = set()
        for entry in sources:
            if (not isinstance(entry, Mapping)
                    or any(type(entry.get(k)) is not int or entry[k] < 0 or abs(entry[k] - center[k]) > 28
                           for k in ("x", "y"))
                    or (entry["x"], entry["y"]) in seen):
                return False
            seen.add((entry["x"], entry["y"]))
    if "tool_tile" in f:
        hit = f["tool_tile"]
        if (not isinstance(hit, Mapping)
                or any(type(hit.get(k)) is not int or abs(hit[k] - center[k]) > 2 for k in ("x", "y"))):
            return False
    if "tiles" in f:
        tiles = f["tiles"]
        if not isinstance(tiles, (list, tuple)) or len(tiles) > 9:
            return False
        seen = set()
        for entry in tiles:
            if (not isinstance(entry, Mapping)
                    or any(type(entry.get(k)) is not int or abs(entry[k] - center[k]) > 1 for k in ("x", "y"))
                    or any(type(entry.get(k)) is not bool for k in ("tillable", "water_refill", "villager"))
                    or (entry["x"], entry["y"]) in seen):
                return False
            seen.add((entry["x"], entry["y"]))
    return True


def inventory_amount(record, *, session_id, screen_id, qid, quality):
    if (record is None or not record.is_complete or not record.authoritative
            or not record.available or record.stale or record.requires_resync
            or record.session_id != session_id or record.screen_id != screen_id
            or not 0 <= time.monotonic() - record.received_monotonic <= 2): return None
    meta = record.facts.get("meta:inventory", {})
    count = meta.get("slot_count")
    if type(count) is not int or not 1 <= count <= 48: return None
    total, empty = 0, False
    for i in range(count):
        slot = record.facts.get(f"slot:{i}", {})
        if slot.get("slot") != i or type(slot.get("occupied")) is not bool: return None
        if slot["occupied"] is False:
            empty = True
            continue
        if (not isinstance(slot.get("qualified_item_id"), str) or type(slot.get("stack")) is not int
                or not 1 <= slot["stack"] <= 999 or type(slot.get("quality")) is not int): return None
        if slot["qualified_item_id"] == qid and slot["quality"] == quality:
            total += slot["stack"]
            empty |= slot["stack"] < 999  # only supported ordinary stackable resources
    return total, not empty

SCENE_PRODUCT = "scene"
SCENE_DOMAIN = "scene.current_location"

FACT_LOCATION = "location"
FACT_WARP = "warp"
FACT_DOOR = "door"
FACT_OBJECT = "object"
FACT_TERRAIN = "terrain"
FACT_NPC = "npc"
FACT_MONSTER = "monster"
FACT_CLUMP = "clump"


def fact_kind(fact: object) -> str | None:
    if not isinstance(fact, dict):
        return None
    kind = fact.get("kind")
    return kind if isinstance(kind, str) else None


def facts_of_kind(facts: Mapping[str, object], kind: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for kunci, isi in facts.items():
        if fact_kind(isi) == kind and isinstance(isi, dict):
            out[kunci] = isi
    return out


def is_ephemeral_monster_id(fact_id: str) -> bool:
    return fact_id.startswith("mon:") and ":eph-" in fact_id
