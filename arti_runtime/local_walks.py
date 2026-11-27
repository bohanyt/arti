"""Local walks on observed open floor, one-tile step, and sampling-time quest delta filter.

All behavior is behind ``stardew_live7_local_walks`` (default False). Pure geometric rules over the
current fresh native navigation grid: no place names, tile constants, object hints, or opinions.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping

SETTING = "stardew_live7_local_walks"
DIRECTIONS = (
    ("kiri", (-1, 0)),
    ("kanan", (1, 0)),
    ("atas", (0, -1)),
    ("bawah", (0, 1)),
)
STEP_DIRS = {
    "up": (0, -1),
    "right": (1, 0),
    "down": (0, 1),
    "left": (-1, 0),
}
DELTA_TO_STEP = {delta: name for name, delta in STEP_DIRS.items()}

MIN_STEPS = 3
MAX_STEPS = 14
MAX_EYES_BYTES = 320
MAX_VISIT_TILES = 512

LOCAL_WALK_MAX_INPUTS = 40
LOCAL_WALK_STEP_MAX_TICKS = 16
LOCAL_WALK_WARP_SAFE_MAX_TICKS = 10

ONE_TILE_DEFAULT_TICKS = 12
ONE_TILE_MAX_TICKS = 16
ONE_TILE_WARP_NO_PIXEL_TICKS = 4
ONE_TILE_MAX_SUBSTEPS = 4
ONE_TILE_EVENT_TICKS = 4
DEFAULT_PX_PER_TICK = 64.0 / ONE_TILE_DEFAULT_TICKS

QUEST_SAMPLING_TIME_KEYS = frozenset({
    "observed_time",
    "observed_day",
    "observed_at",
    "observed_tick",
    "observed_clock",
    "sampled_time",
    "sampled_at",
    "sampled_tick",
    "captured_tick",
    "captured_at",
    "read_time",
    "read_at",
})


def enabled(config) -> bool:
    return isinstance(config, Mapping) and config.get(SETTING, False) is True


def in_event(snapshot, context=None, *, native_event=None) -> bool:
    """True when an event, festival, or cutscene holds input."""
    import arti_stardew_skills as S

    if snapshot is None:
        return True
    if (S._g(snapshot, "event_movement") is not None
            or S._g(snapshot, "event_actions") is not None
            or S._g(snapshot, "festival_reflex") is not None
            or native_event is not None):
        return True
    motor = S._g(snapshot, "motor")
    if S._g(motor, "location") == "Temp":
        return True
    mines_ui = (context or {}).get("mines_ui")
    if isinstance(mines_ui, Mapping):
        if mines_ui.get("festival") or S.event_up(mines_ui):
            return True
    return False


def _warps_and_blocked(grid, context, here):
    import arti_stardew_skills as S

    warps = {tile for tile, cell in grid.items() if cell == "x"}
    blocked = set()
    targets = (context or {}).get("targets")
    if isinstance(targets, Mapping):
        for fact in targets.values():
            if not isinstance(fact, Mapping):
                continue
            kind = fact.get("kind")
            if kind == "warp" and fact.get("npc_only") is not True:
                wt = S._wave1_xy(fact.get("source_tile") or fact.get("tile"))
                if wt is not None:
                    warps.add(wt)
            elif kind in ("object", "terrain", "furniture") and fact.get("passable") is False:
                ft = S._wave1_xy(fact.get("tile"))
                if ft is not None and ft != here and ft not in warps:
                    blocked.add(ft)
    return warps, blocked


def fresh_grid(snapshot, facts, context):
    """Validate and return (raw_grid, walkable_grid, warps, here), or None when missing/stale."""
    import arti_stardew_skills as S
    from arti_stardew_animal_routine import actor_cell_open

    if snapshot is None or not isinstance(context, Mapping):
        return None
    if context.get("ui_complete") is False:
        return None
    motor = S._g(snapshot, "motor")
    here = S._wave1_xy(S._g(motor, "tile"))
    if here is None:
        return None
    actor_tile = context.get("actor_tile")
    if actor_tile is not None and S._wave1_xy(actor_tile) != here:
        return None
    loc_ok = {
        v for v in (
            getattr(facts, "location", None) if facts is not None else None,
            S._g(motor, "location"),
            context.get("location_id"),
        )
        if isinstance(v, str) and v
    }
    mines_ui = context.get("mines_ui")
    if isinstance(mines_ui, Mapping):
        raw_meta = mines_ui.get("nav_grid")
        if isinstance(raw_meta, Mapping):
            if raw_meta.get("stale") is True:
                return None
            meta_loc = raw_meta.get("location")
            if isinstance(meta_loc, str) and meta_loc and loc_ok and meta_loc not in loc_ok:
                return None
            meta_tile = raw_meta.get("actor_tile")
            if meta_tile is not None and S._wave1_xy(meta_tile) != here:
                return None
    grid = context.get("nav_grid")
    if not isinstance(grid, Mapping) or not grid or here not in grid:
        return None
    warps, blocked = _warps_and_blocked(grid, context, here)
    if warps or blocked:
        raw_grid = dict(grid)
        for wt in warps:
            if wt in raw_grid:
                raw_grid[wt] = "x"
        for bt in blocked:
            if raw_grid.get(bt) == ".":
                raw_grid[bt] = "#"
    else:
        raw_grid = grid
    here_cell = raw_grid.get(here)
    if here_cell == "#":
        walkable_grid = actor_cell_open(raw_grid, here)
    elif here_cell == "x":
        walkable_grid = dict(raw_grid)
        walkable_grid[here] = "."
    elif here_cell == ".":
        walkable_grid = raw_grid
    else:
        return None
    if walkable_grid.get(here) != ".":
        return None
    return raw_grid, walkable_grid, warps, here


def visible_bounds(context, facts, here):
    """Return validated (left, top, right, bottom) viewport tile bounds, or None."""
    if not isinstance(context, Mapping) or here is None:
        return None
    loc_ok = {
        v for v in (
            getattr(facts, "location", None) if facts is not None else None,
            context.get("location_id"),
        )
        if isinstance(v, str) and v
    }
    catalog = context.get("world_knowledge")
    raw = None
    if isinstance(catalog, Mapping) and catalog.get("visible_tiles") is not None:
        graph = catalog.get("world_graph")
        current = graph.get("current") if isinstance(graph, Mapping) else None
        if not (isinstance(current, str) and current and loc_ok and current not in loc_ok):
            raw = catalog.get("visible_tiles")
    if raw is None:
        mines_ui = context.get("mines_ui")
        if isinstance(mines_ui, Mapping):
            raw = mines_ui.get("visible_tiles")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    if not all(type(v) is int for v in raw):
        return None
    left, top, right, bottom = raw
    if left > right or top > bottom:
        return None
    if not (left <= here[0] <= right and top <= here[1] <= bottom):
        return None
    return (left, top, right, bottom)


def eligible(snapshot, facts, context, policy, calls, *, native_event=None) -> bool:
    """Whether open-floor local walks may be offered on this page."""
    import arti_stardew_live7 as L
    import arti_stardew_skills as S

    if snapshot is None or facts is None:
        return False
    if getattr(facts, "player_free", False) is not True or getattr(facts, "menu_open", False):
        return False
    if S.menu_up(snapshot):
        return False
    motor = S._g(snapshot, "motor")
    if (S._g(motor, "is_player_free") is not True
            or S._g(motor, "can_player_move") is False
            or bool(S._g(motor, "control_taken"))
            or S._wave1_xy(S._g(motor, "tile")) is None):
        return False
    if in_event(snapshot, context, native_event=native_event):
        return False
    if getattr(facts, "location", None) == "Temp":
        return False
    existing = calls or ()
    if any(getattr(c, "name", None) == "jalan_ke" for c in existing):
        return False
    if any(getattr(c, "name", None) in ("lawan", "lawan_mine") for c in existing):
        return False
    combat = (context or {}).get("combat") or {}
    if S.lawan_near(combat):
        return False
    damaged_at = getattr(policy, "damaged_at", None) if policy is not None else None
    if L.threat(combat, getattr(facts, "health", None), damaged_at=damaged_at,
                now=getattr(facts, "time_of_day", None)) is not None:
        return False
    hp = getattr(facts, "health", None)
    energy = getattr(facts, "energy", None)
    if (hp is not None and hp < 30) or (energy is not None and energy < L.VERY_LOW_ENERGY):
        return False
    if str(getattr(facts, "location", "") or "").startswith("UndergroundMine") and hp is not None and hp < L.LOW_HP:
        return False
    now = getattr(facts, "time_of_day", None)
    if type(now) is not int or now >= L.BED_OFFER_FROM or now >= L.PASS_OUT_CLOCK:
        return False
    if policy is not None:
        if (getattr(policy, "sleep_only", False)
                or getattr(policy, "bedtime_return", False)
                or getattr(policy, "knockout", None) is not None
                or getattr(policy, "pending_sleep", None) is not None
                or getattr(policy, "interrupted_travel", None) is not None):
            return False
        if L.home_due(facts, policy):
            return False
        if L.admission(S.SkillCall("jalan_ke", "0,0"), facts, policy) is not None:
            return False
    return True


def _visit_store(policy, location):
    if policy is None or not isinstance(location, str) or not location:
        return None
    store = getattr(policy, "_local_walk_visits", None)
    if not isinstance(store, dict) or store.get("location") != location:
        store = {"location": location, "counts": {}, "last": None}
        setattr(policy, "_local_walk_visits", store)
    return store


def note_visit(policy, location, tile) -> int:
    """Record standing on ``tile`` during this room visit; resets on location change."""
    if tile is None:
        return 0
    key = (int(tile[0]), int(tile[1]))
    store = _visit_store(policy, location)
    if store is None:
        return 0
    counts = store["counts"]
    if store["last"] != key:
        if key not in counts and len(counts) >= MAX_VISIT_TILES:
            oldest = next(iter(counts))
            counts.pop(oldest, None)
        counts[key] = counts.get(key, 0) + 1
        store["last"] = key
    return counts.get(key, 0)


def visit_count(policy, location, tile) -> int:
    if tile is None:
        return 0
    store = _visit_store(policy, location)
    if store is None:
        return 0
    return int(store["counts"].get((int(tile[0]), int(tile[1])), 0))


def floor_fact(raw_grid, warps, here, *, has_offers: bool = False) -> str | None:
    """Short factual line naming closed adjacent directions and adjacent native exits."""
    closed = []
    exits = []
    hx, hy = here
    for name, (dx, dy) in DIRECTIONS:
        nb = (hx + dx, hy + dy)
        if nb in warps or raw_grid.get(nb) == "x":
            exits.append(name)
        else:
            cell = raw_grid.get(nb)
            if cell is not None and cell != ".":
                closed.append(name)
    parts = []
    if closed:
        parts.append("tertutup: " + ",".join(closed))
    if exits:
        parts.append("keluar: " + ",".join(exits))
    if parts:
        return "; ".join(parts)
    if has_offers:
        return "tertutup: tidak ada"
    return None


def _tile_refused(tile, arg, facts, policy, here, targets) -> bool:
    import arti_stardew_skills as S

    call = S.SkillCall("jalan_ke", arg, tile=tile)
    if S.precondition_failures(call, facts):
        return True
    if policy is None:
        return False
    loc = getattr(facts, "location", None)
    refs = getattr(policy, "refs", None) or ()
    if refs:
        if (arg in refs
                or f"part_a_waypoint:{arg}" in refs
                or f"local_walk:{arg}" in refs
                or (loc and (f"{loc}:{arg}" in refs or f"local_walk:{loc}:{arg}" in refs))):
            return True
        if isinstance(targets, Mapping):
            for ref in refs:
                row = targets.get(ref)
                if isinstance(row, Mapping) and S._wave1_xy(row.get("tile")) == tile:
                    return True
    if getattr(policy, "last_failed", None) == ("jalan_ke", arg):
        return True
    blocked_fn = getattr(policy, "blocked", None)
    if callable(blocked_fn) and blocked_fn(call, loc, facts, here):
        return True
    return False


def _beside_warp_in_exit(tile, parent, delta, warps) -> bool:
    tx, ty = tile
    if tile in warps:
        return True
    if (tx + delta[0], ty + delta[1]) in warps:
        return True
    if parent is not None:
        step_dx, step_dy = tx - parent[0], ty - parent[1]
        if (tx + step_dx, ty + step_dy) in warps:
            return True
    return False


def _eyes_bytes(entry_lantai, fact_line) -> int:
    total = 0
    if entry_lantai:
        total += len(json.dumps({"lantai": entry_lantai}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) - 2
    if fact_line:
        total += len(json.dumps({"lantai": fact_line}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) - 2
    return total


def _cap_eyes(entry_lantai, fact_line):
    if _eyes_bytes(entry_lantai, fact_line) <= MAX_EYES_BYTES:
        return entry_lantai, fact_line
    trimmed = {
        arg: {k: v for k, v in row.items() if k != "kali"}
        for arg, row in entry_lantai.items()
    }
    if _eyes_bytes(trimmed, fact_line) <= MAX_EYES_BYTES:
        return trimmed, fact_line
    if fact_line and _eyes_bytes(trimmed, None) <= MAX_EYES_BYTES:
        return trimmed, None
    out = {}
    for arg, row in trimmed.items():
        candidate = dict(out, **{arg: row})
        if _eyes_bytes(candidate, None) <= MAX_EYES_BYTES:
            out = candidate
    return out, None


def plan(snapshot, facts, context, policy, calls, *, native_event=None):
    """Build up to four directional open-floor ``jalan_ke`` calls plus bounded eyes metadata."""
    import arti_stardew_skills as S
    from arti_stardew_goal_routes import open_path, open_paths

    if not eligible(snapshot, facts, context, policy, calls, native_event=native_event):
        return [], {}, None
    grid_data = fresh_grid(snapshot, facts, context)
    if grid_data is None:
        return [], {}, None
    raw_grid, walkable_grid, warps, here = grid_data
    loc = getattr(facts, "location", None) or S._g(S._g(snapshot, "motor"), "location")
    note_visit(policy, loc, here)
    parents = open_paths(walkable_grid, here)
    if not parents:
        return [], {}, floor_fact(raw_grid, warps, here, has_offers=False)

    bounds = visible_bounds(context, facts, here)
    targets = (context or {}).get("targets") or {}
    hx, hy = here

    beyond = {"kiri": False, "kanan": False, "atas": False, "bawah": False}
    left = top = right = bottom = 0
    if bounds is not None:
        left, top, right, bottom = bounds

    depths = {here: 0}
    candidates = []
    for node, parent in parents.items():
        if parent is None:
            continue
        pdepth = depths.get(parent)
        if pdepth is None:
            continue
        steps = pdepth + 1
        depths[node] = steps
        if node in warps or walkable_grid.get(node) != ".":
            continue
        nx, ny = node
        if bounds is not None:
            outside = False
            if nx < left:
                beyond["kiri"] = True
                outside = True
            elif nx > right:
                beyond["kanan"] = True
                outside = True
            if ny < top:
                beyond["atas"] = True
                outside = True
            elif ny > bottom:
                beyond["bawah"] = True
                outside = True
            if outside:
                continue
        if MIN_STEPS <= steps <= MAX_STEPS:
            candidates.append((node, steps, parent))

    offered_calls = []
    entry_lantai = {}
    seen_tiles = set()
    refused_cache = {}

    for dir_name, delta in DIRECTIONS:
        dx, dy = delta
        best = None
        best_rank = None
        for node, steps, parent in candidates:
            proj = (node[0] - hx) * dx + (node[1] - hy) * dy
            if proj <= 0:
                continue
            if _beside_warp_in_exit(node, parent, delta, warps):
                continue
            perp = abs((node[0] - hx) * dy - (node[1] - hy) * dx)
            rank = (-proj, perp, steps, node)
            if best_rank is not None and rank >= best_rank:
                continue
            arg = f"{node[0]},{node[1]}"
            refused = refused_cache.get(node)
            if refused is None:
                refused = _tile_refused(node, arg, facts, policy, here, targets)
                refused_cache[node] = refused
            if refused:
                continue
            best = (node, steps, arg)
            best_rank = rank
        if best is None:
            continue
        node, steps, arg = best
        if node in seen_tiles:
            continue
        path = open_path(parents, node)
        if not path or len(path) - 1 != steps:
            continue
        seen_tiles.add(node)
        offered_calls.append(S.SkillCall("jalan_ke", arg, tile=node))
        entry_lantai[arg] = {
            "arah": dir_name,
            "langkah": steps,
            "lanjut": bool(beyond.get(dir_name, False)),
            "kali": visit_count(policy, loc, node),
        }

    fact_line = floor_fact(raw_grid, warps, here, has_offers=bool(offered_calls))
    entry_lantai, fact_line = _cap_eyes(entry_lantai, fact_line)
    return offered_calls, entry_lantai, fact_line


def without_eyes(body):
    """Return a copy without local-walk eyes fields, or the original object if absent."""
    if not isinstance(body, Mapping):
        return body
    changed = False
    updates = {}
    facts = body.get("facts")
    if isinstance(facts, Mapping) and "lantai" in facts:
        updates["facts"] = {k: v for k, v in facts.items() if k != "lantai"}
        changed = True
    skills = body.get("skills")
    if isinstance(skills, list):
        new_skills = []
        skill_changed = False
        for entry in skills:
            if isinstance(entry, Mapping) and entry.get("name") == "jalan_ke" and "lantai" in entry:
                new_skills.append({k: v for k, v in entry.items() if k != "lantai"})
                skill_changed = True
            else:
                new_skills.append(entry)
        if skill_changed:
            updates["skills"] = new_skills
            changed = True
    if not changed:
        return body
    out = dict(body)
    out.update(updates)
    return out


def _pixel_dist_to_edge(pixel, here, delta):
    """Pixels from actor bounding-box centre to the boundary of ``here`` in direction ``delta``."""
    if (not isinstance(pixel, (tuple, list)) or len(pixel) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in pixel)):
        return None
    cx, cy = pixel[0] + 32.0, pixel[1] + 16.0
    if (math.floor(cx / 64.0), math.floor(cy / 64.0)) != tuple(here):
        return None
    dx, dy = delta
    if dx == 1:
        return (here[0] + 1) * 64.0 - cx
    if dx == -1:
        return cx - here[0] * 64.0
    if dy == 1:
        return (here[1] + 1) * 64.0 - cy
    if dy == -1:
        return cy - here[1] * 64.0
    return None


def _one_tile_ticks(pixel, here, delta, *, warp_after: bool, ppt: float | None = None) -> int:
    speed = ppt if isinstance(ppt, (int, float)) and 1.0 <= ppt <= 16.0 else DEFAULT_PX_PER_TICK
    dist_edge = _pixel_dist_to_edge(pixel, here, delta)
    if dist_edge is None:
        return ONE_TILE_WARP_NO_PIXEL_TICKS if warp_after else ONE_TILE_DEFAULT_TICKS
    if warp_after:
        # Enter next_tile safely while keeping well clear of the warp tile beyond it.
        target_px = dist_edge + 14.0
        safe_cap_px = dist_edge + 40.0
        raw_ticks = math.ceil(min(target_px, safe_cap_px) / max(speed, DEFAULT_PX_PER_TICK))
        return max(1, min(LOCAL_WALK_WARP_SAFE_MAX_TICKS, raw_ticks))
    target_px = dist_edge + 24.0
    raw_ticks = math.ceil(target_px / speed)
    return max(2, min(ONE_TILE_MAX_TICKS, raw_ticks))


def _walk_hit_detected(state, motor, context, policy) -> bool:
    import arti_stardew_live7 as L

    lw = state.get("local_walk")
    if not isinstance(lw, dict):
        return False
    combat = (context or {}).get("combat") or {}
    hp_vals = [v for v in (getattr(motor, "health", None), combat.get("hp")) if type(v) is int]
    hp = min(hp_vals) if hp_vals else None
    if hp is not None:
        start_hp = lw.get("start_hp")
        if type(start_hp) is not int:
            lw["start_hp"] = hp
        elif hp > start_hp:
            lw["start_hp"] = hp
        elif hp < start_hp:
            return True
    damaged_at = getattr(policy, "damaged_at", None) if policy is not None else None
    now = getattr(motor, "time_of_day", None)
    t = L.threat(combat, hp, damaged_at=damaged_at, now=now)
    if t is not None and (t.get("took_damage") or t.get("contact")):
        return True
    return False


def walk_step(call, snap, state, context=None, *, policy=None, stopped: bool = False,
              halted: bool = False, native_event=None):
    """Step a local walk one fresh ``open_path`` cell at a time with full stop and warp guards."""
    import arti_stardew_part_a as PA
    import arti_stardew_skills as S
    from arti_stardew_goal_routes import open_path, open_paths

    if stopped:
        return S.Step("fail", reason="local_walk_stopped")
    if halted:
        return S.Step("fail", reason="hands_halted")
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if last is not None and (S._g(last, "cleanup_confirmed") is False
                             or S._g(last, "lifecycle") in ("uncertain", "interrupted")):
        return S.Step("fail", reason="action_unreconciled")
    if S.menu_up(snap):
        return S.Step("fail", reason="local_walk_menu_open")
    if in_event(snap, context, native_event=native_event):
        return S.Step("fail", reason="local_walk_event_active")
    motor = S._g(snap, "motor")
    if _walk_hit_detected(state, motor, context, policy):
        return S.Step("fail", reason="under_attack")
    lw = state.get("local_walk") if isinstance(state, dict) else None
    loc = S._g(motor, "location")
    if isinstance(lw, Mapping) and lw.get("location") and loc != lw.get("location"):
        return S.Step("fail", reason="local_walk_map_changed")
    here = S._wave1_xy(S._g(motor, "tile"))
    if here is None:
        return S.Step("fail", reason="motor_unavailable")
    note_visit(policy, loc, here)
    target = call.tile if call.tile is not None else S._wave1_xy((lw or {}).get("tile"))
    if target is None:
        return S.Step("fail", reason="local_walk_target_missing")
    target = tuple(target)
    if here == target:
        return S._decision(S.FINISH)
    if S._g(S._g(snap, "working_memory"), "last_inspect_status") != "current":
        return S._decision(S.INSPECT)

    rid = S._g(last, "request_id")
    if rid and rid != state.get("walk_seen"):
        state["walk_seen"] = rid
        before = S._g(last, "before") or {}
        after = S._g(last, "after") or {}
        if (before.get("tile") is not None and before.get("tile") == after.get("tile")) or (
                S._g(last, "outcome") == "NOT_COMPLETED:NO_EFFECT"):
            state["walk_stuck"] = state.get("walk_stuck", 0) + 1
            if state["walk_stuck"] >= 2:
                return S.Step("fail", reason="walk_blocked")
        elif before.get("tile") != after.get("tile"):
            state["walk_stuck"] = 0

    if state.get("local_walk_inputs", 0) >= LOCAL_WALK_MAX_INPUTS:
        return S.Step("fail", reason="walk_blocked")

    grid_data = fresh_grid(snap, None, context)
    if grid_data is None:
        if state.get("local_walk_reinspects", 0) < PA.WALK_REINSPECTS:
            state["local_walk_reinspects"] = state.get("local_walk_reinspects", 0) + 1
            return S._decision(S.INSPECT)
        return S.Step("fail", reason="local_walk_no_open_path")
    raw_grid, walkable_grid, warps, here = grid_data
    if target in warps or raw_grid.get(target) == "x":
        return S.Step("fail", reason="local_walk_warp_blocked")

    parents = open_paths(walkable_grid, here)
    path = open_path(parents, target)
    if not path or len(path) < 2:
        if state.get("local_walk_reinspects", 0) < PA.WALK_REINSPECTS:
            state["local_walk_reinspects"] = state.get("local_walk_reinspects", 0) + 1
            return S._decision(S.INSPECT)
        return S.Step("fail", reason="local_walk_no_open_path")
    state["local_walk_reinspects"] = 0

    next_cell = tuple(path[1])
    delta = (next_cell[0] - here[0], next_cell[1] - here[1])
    if (next_cell in warps
            or raw_grid.get(next_cell) == "x"
            or walkable_grid.get(next_cell) != "."
            or delta not in DELTA_TO_STEP):
        return S.Step("fail", reason="local_walk_warp_blocked")

    direction = DELTA_TO_STEP[delta]
    after_next = (next_cell[0] + delta[0], next_cell[1] + delta[1])
    warp_after = after_next in warps or raw_grid.get(after_next) == "x"
    pixel = S._g(motor, "pixel")
    if warp_after:
        ticks = _one_tile_ticks(pixel, here, delta, warp_after=True, ppt=state.get("clear_walk_ppt"))
    else:
        ticks = S._clear_walk_ticks(state, motor, here, next_cell, delta, LOCAL_WALK_STEP_MAX_TICKS)
        if ticks is None:
            ticks = _one_tile_ticks(pixel, here, delta, warp_after=False, ppt=state.get("clear_walk_ppt"))
        ticks = max(1, min(LOCAL_WALK_STEP_MAX_TICKS, ticks))
    if isinstance(pixel, (tuple, list)) and len(pixel) == 2:
        state["clear_walk"] = {"pixel": list(pixel), "direction": direction, "ticks": ticks}
    state["local_walk_inputs"] = state.get("local_walk_inputs", 0) + 1
    return S._decision(S.act("world_key.move_cardinal", {"direction": direction, "ticks": ticks}))


def _context_warps(context):
    import arti_stardew_skills as S

    warps = set()
    if not isinstance(context, Mapping):
        return warps
    grid = context.get("nav_grid")
    if isinstance(grid, Mapping):
        for tile, cell in grid.items():
            if cell == "x":
                warps.add(tuple(tile))
    targets = context.get("targets")
    if isinstance(targets, Mapping):
        for fact in targets.values():
            if isinstance(fact, Mapping) and fact.get("kind") == "warp" and fact.get("npc_only") is not True:
                wt = S._wave1_xy(fact.get("source_tile") or fact.get("tile"))
                if wt is not None:
                    warps.add(wt)
    return warps


def one_tile_step(call, snap, state, *, context=None):
    """Point 4: outside events ``langkah`` asks for one tile; in events/festivals stays 4 ticks."""
    import arti_stardew_generic_world as GW
    import arti_stardew_skills as S
    from arti_runtime.raw_hands import MOVED, OBSERVED_TRANSITIONS, STILL, TURNED, step_readback

    last = S._g(S._g(snap, "working_memory"), "last_action")
    if call.arg not in STEP_DIRS:
        return S.Step("fail", reason="direction_invalid")

    if state.get("one_tile_event") or (not state.get("generic_pressed") and in_event(snap, context)):
        if state.get("generic_pressed"):
            if (S._g(last, "request_id") and S._g(last, "request_id") != state.get("generic_previous")
                    and (movement := step_readback(last)) is not None
                    and ("transition" not in movement or movement["transition"] in OBSERVED_TRANSITIONS)):
                state["step_movement"] = movement
                return S.Step("done", reason=movement["kind"])
            if (refused := GW.mod_refusal(last, state)) is not None:
                return refused
            if (S._g(last, "request_id") == state.get("generic_previous")
                    and state.get("generic_resends", 0) < GW.LANGKAH_RESENDS_MAX):
                state["generic_resends"] = state.get("generic_resends", 0) + 1
                return S._decision(S.act("world_key.move_cardinal", dict(direction=call.arg, ticks=ONE_TILE_EVENT_TICKS)))
            return S._wait(state, "move", "movement_unverified", "Amati posisi", S.WAIT_S)
        state.update(
            generic_pressed=True,
            generic_previous=S._g(last, "request_id"),
            one_tile_event=True,
        )
        return S._decision(S.act("world_key.move_cardinal", dict(direction=call.arg, ticks=ONE_TILE_EVENT_TICKS)))

    delta = STEP_DIRS[call.arg]
    motor = S._g(snap, "motor")
    here = S._wave1_xy(S._g(motor, "tile"))
    loc = S._g(motor, "location")
    pixel = S._g(motor, "pixel")
    warps = _context_warps(context)

    if state.get("generic_pressed"):
        if (S._g(last, "request_id") and S._g(last, "request_id") != state.get("generic_previous")
                and (movement := step_readback(last)) is not None
                and ("transition" not in movement or movement["transition"] in OBSERVED_TRANSITIONS)):
            if "one_tile_before" not in state:
                state["one_tile_before"] = movement.get("before")
            if "transition" in movement:
                state["step_movement"] = movement
                return S.Step("done", reason=movement["kind"])
            total_px = float(state.get("one_tile_px", 0.0)) + float(movement.get("px", 0.0))
            state["one_tile_px"] = total_px
            if movement["kind"] in (STILL, TURNED):
                if total_px > 0:
                    final = dict(kind=MOVED, px=total_px, before=state["one_tile_before"])
                else:
                    final = dict(movement, before=state["one_tile_before"])
                state["step_movement"] = final
                return S.Step("done", reason=final["kind"])
            # Movement observed: check whether fresh pose reached target tile.
            after_tile = S._wave1_xy((S._g(last, "after") or {}).get("tile"))
            start_tile = S._wave1_xy(state.get("one_tile_start"))
            target_tile = S._wave1_xy(state.get("one_tile_target"))
            now_tile = here if here is not None else after_tile
            reached = (
                target_tile is None
                or start_tile is None
                or now_tile == target_tile
                or after_tile == target_tile
                or (now_tile is not None and now_tile != start_tile)
            )
            substeps = state.get("one_tile_substeps", 1)
            if (reached
                    or substeps >= ONE_TILE_MAX_SUBSTEPS
                    or S.menu_up(snap)
                    or in_event(snap, context)
                    or (state.get("one_tile_loc") and loc != state.get("one_tile_loc"))):
                final = dict(kind=MOVED, px=total_px, before=state["one_tile_before"])
                state["step_movement"] = final
                return S.Step("done", reason=MOVED)
            # Moved partway on the same tile without hitting a wall: issue bounded follow-up.
            next_is_warp = target_tile in warps
            after_next = (target_tile[0] + delta[0], target_tile[1] + delta[1])
            warp_after = (not next_is_warp) and (after_next in warps)
            prev_ticks = state.get("one_tile_ticks")
            ppt = (float(movement.get("px", 0.0)) / prev_ticks) if prev_ticks and movement.get("px", 0.0) > 0 else None
            ticks = _one_tile_ticks(pixel, now_tile, delta, warp_after=warp_after, ppt=ppt)
            state.update(
                generic_previous=S._g(last, "request_id"),
                generic_resends=0,
                one_tile_ticks=ticks,
                one_tile_substeps=substeps + 1,
            )
            return S._decision(S.act("world_key.move_cardinal", dict(direction=call.arg, ticks=ticks)))
        if (refused := GW.mod_refusal(last, state)) is not None:
            return refused
        if (S._g(last, "request_id") == state.get("generic_previous")
                and state.get("generic_resends", 0) < GW.LANGKAH_RESENDS_MAX):
            state["generic_resends"] = state.get("generic_resends", 0) + 1
            ticks = state.get("one_tile_ticks", ONE_TILE_DEFAULT_TICKS)
            return S._decision(S.act("world_key.move_cardinal", dict(direction=call.arg, ticks=ticks)))
        return S._wait(state, "move", "movement_unverified", "Amati posisi", S.WAIT_S)

    next_tile = (here[0] + delta[0], here[1] + delta[1]) if here is not None else None
    next_is_warp = next_tile in warps if next_tile is not None else False
    after_next = (next_tile[0] + delta[0], next_tile[1] + delta[1]) if next_tile is not None else None
    warp_after = (not next_is_warp) and (after_next in warps if after_next is not None else False)
    ticks = _one_tile_ticks(pixel, here, delta, warp_after=warp_after) if here is not None else ONE_TILE_DEFAULT_TICKS
    state.update(
        generic_pressed=True,
        generic_previous=S._g(last, "request_id"),
        one_tile_event=False,
        one_tile_start=list(here) if here is not None else None,
        one_tile_target=list(next_tile) if next_tile is not None else None,
        one_tile_loc=loc,
        one_tile_ticks=ticks,
        one_tile_substeps=1,
        one_tile_px=0.0,
    )
    return S._decision(S.act("world_key.move_cardinal", dict(direction=call.arg, ticks=ticks)))


def _strip_sampling_time(value):
    if isinstance(value, Mapping):
        return {
            k: _strip_sampling_time(v)
            for k, v in value.items()
            if k not in QUEST_SAMPLING_TIME_KEYS
        }
    if isinstance(value, list):
        return [_strip_sampling_time(v) for v in value]
    return value


def quest_sampling_time_only(before, after) -> bool:
    """Point 5: True when two quest dicts differ only in sampling-time metadata."""
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    return _strip_sampling_time(before) == _strip_sampling_time(after)
