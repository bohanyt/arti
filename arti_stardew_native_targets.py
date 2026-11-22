"""Target selection over admitted native observations; no IO or action/effect authority."""
from collections.abc import Mapping
from collections import deque


def xy(value):
    if isinstance(value, Mapping):
        value = (value.get("x"), value.get("y"))
    if isinstance(value, (tuple, list)) and len(value) == 2 and all(type(v) is int for v in value):
        return tuple(value)
    return None


def tile(entry):
    if isinstance(entry, Mapping):
        return xy(entry.get("tile")) or xy(entry)
    return None


def adjacent(tile):
    x, y = tile
    return ((x, y-1), (x-1, y), (x+1, y), (x, y+1))


def work_tiles(entry):
    """Native stands narrow the cardinal defaults; an empty offer stays empty."""
    target = tile(entry)
    if target is None:
        return ()
    if isinstance(entry.get("stands"), (tuple, list)):
        return tuple(s for raw in entry["stands"] if (s := xy(raw)) in adjacent(target))
    return adjacent(target)


def _distances(grid, starts):
    distances = {s: 0 for s in starts if grid.get(s) == "."}
    todo = deque(distances)
    while todo:
        here = todo.popleft()
        for s in adjacent(here):
            if s not in distances and grid.get(s) == ".":
                distances[s] = distances[here] + 1
                todo.append(s)
    return distances


class WorkRanker:
    """One bounded BFS per pose and remaining task, shared across all candidates.

    Reachable stands win on exact cardinal path length. Off-window work ranks
    behind reachable work, ahead of known blocked work, using only a distance
    lower bound. This estimate grants no route/stand/action authority.
    """
    def __init__(self, grid, here, *, remaining=(), player_id=None, stands=work_tiles):
        self.here, self.player_id, self.stands = xy(here), player_id, stands
        self.grid = grid if grid and len(grid) <= 4096 else {}
        self.costs = _distances(self.grid, (self.here,)) if self.here else {}
        self.remaining = []
        for group in remaining:
            sites = {s for e in group for s in work_tiles(e)}
            if sites:
                self.remaining.append((sites, _distances(self.grid, sites)))

    def rank(self, entry):
        sites = tuple(self.stands(entry))
        owner = entry.get("owner_id", entry.get("owner_player_id"))
        own = self.player_id is not None and owner == self.player_id
        distance = lambda a, b: abs(a[0]-b[0])+abs(a[1]-b[1])
        onward = lambda s: tuple(costs[s] if s in costs else min(distance(s, t) for t in goals)
                                 for goals, costs in self.remaining)
        reachable = [s for s in sites if s in self.costs]
        if reachable:
            cost, tail = min((self.costs[s], onward(s)) for s in reachable)
            score = (0, cost, tail, not own)
        elif self.costs:
            unseen = [s for s in sites if s not in self.grid]
            score = (1 if unseen else 2,
                     min((distance(self.here, s) for s in sites), default=float("inf")), (), not own)
        else:
            # Older/native snapshots without a grid retain discovery ranking;
            # the travel planner must still obtain current path evidence.
            score = (3, not own, distance(self.here, tile(entry)), ())
        return (*score, str(entry.get("ref", "")), tile(entry))


def nearest(entries, here, *, player_id=None, grid=None, remaining=(), stands=work_tiles):
    """Cheapest current-grid work stand, then remaining work and native identity."""
    here = xy(here)
    candidates = [e for e in entries if tile(e) is not None]
    if here is None or not candidates:
        return None
    ranker = WorkRanker(grid, here, remaining=remaining, player_id=player_id, stands=stands)
    return min(candidates, key=ranker.rank)


def machines(context):
    return [e for e in context.get("extras", ()) if isinstance(e, Mapping)
            and e.get("kind") in ("machine_collect", "machine_insert") and e.get("ready") is not False]


def gift_villager(context, here):
    """A currently offered, gift-eligible villager; never a scheduled or remembered NPC location."""
    import re
    if xy(here) is None:
        return None
    candidates = [e for e in context.get("extras", ()) if isinstance(e, Mapping)
                  and e.get("kind") == "npc_gift" and e.get("ready") is not False
                  and e.get("gift_refusal") == ""
                  and re.fullmatch(r"[a-z][a-z'-]{0,19}", str(e.get("label", "")).casefold())]
    # FINAL B7: S16 talks first. The talk routine never offers an excluded name or a villager already talked to
    # today (arti_stardew_interact_b.npc_entry_skips), so such an offer cannot be this row's villager.
    from arti_stardew_interact_b import EXCLUDED_NPCS
    scene = context.get("targets") or {}
    candidates = [e for e in candidates if str(e.get("label", "")).casefold() not in EXCLUDED_NPCS
                  and not (isinstance(e.get("npc_fact_id"), str) and isinstance(scene.get(e["npc_fact_id"]), Mapping)
                           and scene[e["npc_fact_id"]].get("talked_today") is True)]
    grid = context.get("nav_grid") or {}
    ranker = WorkRanker(grid, here)
    candidates = [e for e in candidates if tile(e) is not None and work_tiles(e)
                  and ranker.rank(e)[0] != 2]
    return min(candidates, key=ranker.rank) if candidates else None


def debris(context):
    return [dict(f, ref=ref) for ref, f in context.get("targets", {}).items()
            if isinstance(f, Mapping) and f.get("kind") == "object"
            and f.get("name") in ("Weeds", "Stone", "Twig", "Twigs") and tile(f) is not None]


def work_stands(context, kind):
    """Only native water-edge/counter stands, never a map landmark seed."""
    if kind == "fish":
        fishing = context.get("fishing")
        edges = fishing.get("water_edges") if isinstance(fishing, Mapping) else None
        if not isinstance(edges, (list, tuple)):
            return ()
        return tuple(dict.fromkeys(s for e in edges if isinstance(e, Mapping)
            and (s := xy((e.get("stand_x"), e.get("stand_y")))) is not None
            and (f := xy((e.get("face_x"), e.get("face_y")))) is not None
            and abs(s[0]-f[0])+abs(s[1]-f[1]) == 1))
    from arti_stardew_world_extras import stand_tiles
    return tuple(dict.fromkeys(tuple(s) for e in context.get("extras", ())
        if e.get("kind") == "shop_counter" and e.get("ready") is not False
        for s in (stand_tiles(e) or ())))


FISHING_EXPLORE_LEGS = 4
FISHING_STAND_GAP = "part_b_target_unavailable:fishing_stand"


def fishing_search_target(grid, here, state):
    """An observed-open water approach or bounded survey waypoint, never a cast offer.

    BFS distance chooses the nearest reachable cardinal shore in this window. Without
    visible water, at most four eight-cell legs survey previously unseen window edges.
    Remembered windows rank exploration only; every returned tile needs a fresh path.
    Callers retain their movement, identity, cleanup and total-budget guards.
    """
    from arti_stardew_goal_routes import open_paths, open_path, WAYPOINT_PATH_MAX
    here = xy(here)
    if here is None:
        return None
    parents = open_paths(grid, here)
    if not parents:
        return None
    neighbors = lambda p: ((p[0], p[1]-1), (p[0]-1, p[1]), (p[0]+1, p[1]), (p[0], p[1]+1))
    shores = [p for p in parents if any(grid.get(n) == "w" for n in neighbors(p))]
    if shores:
        stand = min(shores, key=lambda p: (len(open_path(parents, p)), p))
        # At a shore without a native offer, fail honestly instead of starting fishing.
        return stand if stand != here else None
    if "w" in grid.values():
        return None  # visible water has no reachable shore; never route over unknown/closed cells
    pending = state.get("pending")
    if pending is not None and pending != here and pending in parents:
        return pending  # snapshot re-publication cannot consume another exploration leg
    state.pop("pending", None)
    if state.get("legs", 0) >= FISHING_EXPLORE_LEGS:
        return None
    xs, ys = zip(*grid)
    window = (min(xs), min(ys), max(xs), max(ys))
    windows = state.setdefault("windows", [])
    if window not in windows:
        windows.append(window)
    def unseen(p):
        return p[0] >= 0 and p[1] >= 0 and not any(
            x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for x0, y0, x1, y1 in windows)
    visited = state.setdefault("visited", [])
    frontiers = [p for p in parents if p != here and p not in visited
                 and any(unseen(n) for n in neighbors(p))]
    if not frontiers:
        return None
    path = min((open_path(parents, p) for p in frontiers), key=lambda p: (len(p), p[-1]))
    target = path[min(WAYPOINT_PATH_MAX, len(path)-1)]
    state["pending"] = target
    state["legs"] = state.get("legs", 0) + 1
    visited.append(target)
    return target
