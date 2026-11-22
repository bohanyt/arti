"""ADR 196: a native way down that the game lists is a choice, finished by the hands."""
from collections.abc import Mapping

from arti_stardew_world_extras import extras_targets, stand_tiles

DOWN_KINDS = ("ladder_down", "shaft")
APPROACH_KINDS = frozenset({"shop_counter", "shipping_bin", "ladder_up", "ladder_down", "shaft"})


def current_floor(location):
    text = str(location or "")
    if not text.startswith("UndergroundMine"):
        return None
    suffix = text[len("UndergroundMine"):]
    return int(suffix) if suffix.isdigit() else None


def note_depth(policy, location):
    floor = current_floor(location)
    if floor is None or policy is None:
        return floor
    prev = getattr(policy, "deepest_mine_floor", None)
    policy.deepest_mine_floor = floor if type(prev) is not int else max(prev, floor)
    return floor


def depth_line(location, catalog_deepest, observed):
    here = current_floor(location)
    deepest = max((n for n in (catalog_deepest, observed, here) if type(n) is int), default=None)
    if deepest is None:
        return None
    if here is None:
        return "lantai terdalam %d" % deepest + (" (belum pernah turun)" if deepest == 0 else "")
    return "lantai %d, terdalam %d" % (here, deepest)


def _ready(entry, require_ready):
    return not require_ready or entry.get("ready") is True or entry.get("kind") in DOWN_KINDS


def _on_floor(ref, location):
    loc = str(location or "")
    return isinstance(ref, str) and (not loc or ref.startswith("xt:mine:" + loc + ":")
                                     or ref.startswith("xt:ladder_down:" + loc + ":")
                                     or ref.startswith("xt:shaft:" + loc + ":"))


def downs(context, location, *, require_ready=True):
    """Native down ladders and shafts the game lists on this floor, extras then ordinary ACTION."""
    seen = {}
    extras = list(context.get("extras") or ())
    ui = context.get("mines_ui") or {}
    if isinstance(ui, Mapping) and ui.get("extras_targets"):
        extras = extras + list(ui.get("extras_targets") or ())
    for e in extras_targets(dict(extras_targets=extras)):
        if e.get("kind") in DOWN_KINDS and _ready(e, require_ready) and _on_floor(e.get("ref"), location):
            seen[e["ref"]] = e
    from scripts.live7.interactions import ordinary_targets
    for row in ordinary_targets(context):
        if row.get("kind") not in DOWN_KINDS or row.get("status") not in ("openable", "available"):
            continue
        tile = row.get("tile") or ()
        if len(tile) != 2:
            continue
        seen.setdefault(row["ref"], dict(ref=row["ref"], kind=row["kind"], x=tile[0], y=tile[1],
                                         ready=True, stands=row.get("stands")))
    return list(seen.values())


def listed_downs(context, location=None):
    """Every native down the game lists on this floor, including ones this visit already refused."""
    return downs(context, location, require_ready=False)


def approach_kind(kind):
    return kind if kind in APPROACH_KINDS else "object"


def unreachable(snap, context, observed, here):
    """Named fact when a listed way down cannot be pressed. Never a silent not-reachable."""
    from collections.abc import Mapping as Map
    ref = observed.get("ref") if isinstance(observed, Map) else None
    tile = tuple(observed.get("tile") or ()) if isinstance(observed, Map) else ()
    if len(tile) != 2:
        tile = (observed.get("x"), observed.get("y")) if isinstance(observed, Map) else ()
    if isinstance(observed, Map) and len(tile) == 2 and ("x" not in observed or "y" not in observed):
        observed = dict(observed, x=tile[0], y=tile[1])
    grid = (context or {}).get("nav_grid") or {}
    combat = (context or {}).get("combat") or {}
    stones, monsters = [], []
    stands = None
    if isinstance(observed, Map) and type(observed.get("x")) is int and type(observed.get("y")) is int:
        stands = stand_tiles(observed)
    spots = [tuple(s) for s in stands] if stands else (
        [(tile[0] + dx, tile[1] + dy) for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))] if len(tile) == 2 else [])
    for cell in spots:
        if grid.get(cell) == "s":
            stones.append(list(cell))
    for m in combat.get("monsters") or ():
        if isinstance(m, Map) and type(m.get("distance")) in (int, float) and m["distance"] <= 2:
            monsters.append(dict(type=m.get("type"), tile=m.get("tile"), distance=m["distance"]))
    extras = listed_downs(context or {}, (getattr(getattr(snap, "motor", None), "location", None)))
    still = any(e.get("ref") == ref for e in extras)
    reason = "ladder_down_blocked" if stones or monsters else (
        "ladder_down_not_in_catalog" if still else "ladder_down_not_listed")
    return dict(reason=reason, ref=ref, tile=list(tile) if len(tile) == 2 else None,
                here=list(here) if here else None, stones=stones[:6], monsters=monsters[:4],
                pecah_batu="ditawarkan jika game menawarkannya" if stones else None)
