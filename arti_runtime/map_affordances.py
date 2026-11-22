"""Current-map affordances, bounded with the same scene geometry for each class."""
from collections.abc import Mapping
import re

from arti_runtime.native_rows import native_rows

LIMIT = 8
WORDS = {"press": "ditekan", "pick_up": "dipungut", "break": "dipecah dengan alat",
         "talk": "diajak bicara", "walk_through": "dilewati"}


def observed(context):
    """Read canonical scene claims. The old UI wire remains readable on an older mod."""
    scene = context.get("scene_map_affordances")
    has_scene = isinstance(scene, Mapping) and scene.get("location_id") == context.get("location_id")
    if has_scene:
        rows = native_rows(scene.get("cells"))
    else:
        rows = [(*r[:3], ("press",), r[3] if len(r) == 4 else None)
                for r in native_rows((context.get("mines_ui") or {}).get("map_actions"))
                if isinstance(r, (list, tuple)) and len(r) in (3, 4)]
    out = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 5:
            continue
        ref, x, y, classes, group = row
        if (not isinstance(ref, str) or re.fullmatch(r"map-action:[0-9a-f]{20}", ref) is None
                or not all(type(n) is int and 0 <= n <= 4096 for n in (x, y))):
            continue
        allowed = tuple(dict.fromkeys(c for c in native_rows(classes) if isinstance(c, str) and c in WORDS))
        if allowed:
            out[ref] = dict(tile=(x, y), classes=allowed, group=group if isinstance(group, str) else None, cell=True)
    if has_scene and scene.get("ordinary_world") is not False and context.get("describe_affordances"):
        from arti_stardew_native_targets import xy
        targets = context.get("scene_affordance_targets", context.get("targets")) or {}
        for ref, fact in targets.items():
            if not isinstance(fact, Mapping) or fact.get("kind") != "object" or not isinstance(ref, str):
                continue
            tile = xy(fact.get("tile"))
            classes = tuple(c for c, key in (("pick_up", "grabbable"), ("break", "tool_breakable"))
                            if fact.get(key) is True)
            if tile is not None and classes:
                out[ref] = dict(tile=tile, classes=classes, group=None, cell=False)
    omitted = scene.get("omitted") if has_scene else None
    return out, omitted if type(omitted) is int and omitted > 0 else 0


def _distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _relative(tile, anchor, origin):
    x, y = tile[0] - anchor[0], tile[1] - anchor[1]
    offsets = []
    if x:
        offsets.append(f"{abs(x)} langkah ke {'timur' if x > 0 else 'barat'}")
    if y:
        offsets.append(f"{abs(y)} langkah ke {'selatan' if y > 0 else 'utara'}")
    return f"{' dan '.join(offsets)} dari {origin}" if offsets else f"di {origin}"


def _doors(witnesses, location):
    from arti_runtime.fact_line import DOOR_KINDS, DOOR_KIND_PREFIXES

    graph = witnesses.catalog.get("world_graph") or {}
    if not isinstance(graph, Mapping) or graph.get("current") != location:
        return []
    known = witnesses.names()
    names = {p["id"]: witnesses.display_name(p, known[p["id"]])
             for p in native_rows(graph.get("places")) if isinstance(p, Mapping)
             and isinstance(p.get("id"), str) and p["id"] in known and p.get("label")}
    doors = set()
    for edge in native_rows(graph.get("connections")):
        if isinstance(edge, Mapping):
            source, dest, kind, player, ref = (edge.get(k) for k in ("from", "to", "kind", "player", "id"))
        elif isinstance(edge, (list, tuple)) and len(edge) >= 7:
            source, dest, kind, _, _, player, ref = edge[:7]
        else:
            continue
        if (source != location or not isinstance(dest, str) or dest not in names or player is False
                or not isinstance(kind, str) or not (kind in DOOR_KINDS or kind.startswith(DOOR_KIND_PREFIXES))):
            continue
        if isinstance(ref, str) and (match := re.search(r":(\d+),(\d+):[^:]+$", ref)):
            doors.add(((int(match[1]), int(match[2])), names[dest]))
    return sorted(doors)


def choices(calls, context, here, witnesses):
    """Return ordered controls and descriptions, without granting new tool or movement authority."""
    claims, already_omitted = observed(context)
    from arti_runtime.fact_line import visible_targets
    visible = visible_targets(context)
    offered = {c.arg for c in calls if c.name == "aksi" and c.arg in claims and c.tile is not None}
    candidates = {ref: row["tile"] for ref, row in claims.items() if ref in offered or ref in visible}
    if not candidates or here is None:
        return {}, [], offered, already_omitted
    groups = {ref: (row["group"], row["classes"]) for ref, row in claims.items() if row["group"]}
    positions = {(groups[ref], *tile): ref for ref, tile in candidates.items() if ref in groups}
    location = context.get("location_id")
    doors = _doors(witnesses, location)
    receipts = {r["id"]: r for r in witnesses.matching("map_action_read") if r.get("place") == location}
    seen, ranked = set(), []
    for ref, tile in candidates.items():
        if ref in seen:
            continue
        members, frontier = [], [ref]
        seen.add(ref)
        for member in frontier:
            members.append(member)
            x, y = candidates[member]
            for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                neighbor = positions.get((groups.get(member), x + dx, y + dy))
                if neighbor is not None and neighbor not in seen:
                    seen.add(neighbor)
                    frontier.append(neighbor)
        target = min(members, key=lambda m: (_distance(candidates[m], here), candidates[m]))
        tile = candidates[target]
        adjacent = min(doors, key=lambda d: min(_distance(d[0], candidates[m]) for m in members), default=None)
        by_door = adjacent is not None and any(_distance(adjacent[0], candidates[m]) <= 2 for m in members)
        label = _relative(tile, adjacent[0], f"pintu {adjacent[1]}") if by_door else _relative(tile, here, "tempatmu")
        if by_door:
            for other, name in doors:
                anchor, nearest = adjacent
                if (name != nearest and _distance(other, tile) <= 4
                        and _distance(anchor, tile) + _distance(tile, other) == _distance(anchor, other)):
                    label = f"di antara pintu {nearest} dan pintu {name} ({label})"
                    break
        receipt = next((receipts[m] for m in members if m in receipts), None)
        if receipt:
            from arti_runtime.native_notices import source_line
            label = f"{witnesses.text(receipt)[:120]} ({source_line(receipt)})"
        classes = claims[target]["classes"]
        label = "Bisa " + ", ".join(WORDS[c] for c in classes) + " " + label
        ranked.append((not by_door, _distance(tile, here), tile, target, label))
    ranked.sort()
    selected = ranked[:LIMIT]
    return ({ref: label for _, _, _, ref, label in selected if ref in offered},
            [label for _, _, _, ref, label in selected if ref not in offered],
            offered, already_omitted + max(0, len(ranked) - LIMIT))
