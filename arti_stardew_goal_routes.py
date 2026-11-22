"""FIXB38: bounded Farm travel for shop/fishing objectives, using observed exits and nav cells.

No command IO here. The existing leased approach, door, warp and domain verifiers own every effect.
Combat entry is deliberately deferred; it must not borrow a shop/fish completion criterion.
"""
from collections import deque
from collections.abc import Mapping
from dataclasses import replace
import hashlib
import time
from arti_stardew_pause import game_now, wait_resume
from types import SimpleNamespace

import arti_stardew_interact as interact
import arti_stardew_seeds as seeds

ITINERARIES = {"sell": ("Farm", "BusStop", "Town", "SeedShop"),
               "buy": ("Farm", "BusStop", "Town", "SeedShop"),
               "fish": ("Farm", "Backwoods", "Mountain"),
               # W1 gap B5 route witness (owner 2026-10-19): the B9 corridor plus the native Mountain->Mine warp.
               # Only next_hop() reads it; every hop still needs this location's observed native exit fact.
               "mine": ("Farm", "Backwoods", "Mountain", "Mine")}
# FINAL Part B only (owner 2026-10-20): vanilla map warps (GameLocation.updateWarps), observed in
# b20-s7-journal and part-b7-mine-capture. Guidance only: native exit offers own every hop. Kept out of
# ITINERARIES so Part A/C, skills_attempts._hop and interact route exactly as before; only an active
# Part B final-day campaign passes it to next_hop (arti_stardew_part_b.final_day_routes).
PART_B_FINAL_ITINERARIES = {"town_mountain": ("Town", "Mountain")}
ROUTE_STEPS_MAX = 160
ROUTE_SECONDS = 300
ROUTE_REPLANS_MAX = 2
WAYPOINT_PATH_MAX = 8


def next_hop(current, destination, extra=()):
    """Reuse the shipped itineraries in either direction; never create an exit.

    ``extra`` adds caller-scoped itineraries (location names only), e.g. the FINAL Part B
    Town<->Mountain edge. The caller must still find the hop in this location's native exit facts.
    """
    graph = {}
    for route in (*ITINERARIES.values(), *extra):
        for a, b in zip(route, route[1:]):
            graph.setdefault(a, set()).add(b)
            graph.setdefault(b, set()).add(a)
    todo, seen = deque([(current, None)]), {current}
    while todo:
        node, first = todo.popleft()
        if node == destination:
            return first
        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in seen:
                seen.add(neighbor)
                todo.append((neighbor, first or neighbor))
    return None


def open_paths(grid, start):
    """Bounded cardinal reachability using only the current grid's explicitly open cells."""
    start = tuple(start)
    if not grid or len(grid) > 4096 or grid.get(start) != ".":
        return {}
    todo, parents = deque([start]), {start: None}
    while todo:
        x, y = todo.popleft()
        for n in ((x, y - 1), (x - 1, y), (x + 1, y), (x, y + 1)):
            if n not in parents and grid.get(n) == ".":
                parents[n] = (x, y)
                todo.append(n)
    return parents


def open_path(parents, destination):
    destination = tuple(destination)
    if destination not in parents:
        return None
    path = [destination]
    while parents[path[-1]] is not None:
        path.append(parents[path[-1]])
    return list(reversed(path))


def waypoint(grid, start, destination):
    """At most eight observed-open cells toward a destination; never arrival/effect proof."""
    start, destination = tuple(start), tuple(destination)
    parents = open_paths(grid, start)
    if not parents:
        return None
    distance = lambda p: abs(p[0] - destination[0]) + abs(p[1] - destination[1])
    best = min(parents, key=lambda p: (distance(p), p))
    if distance(best) >= distance(start):
        return None
    path = open_path(parents, best)
    return path[min(WAYPOINT_PATH_MAX, len(path) - 1)]


def local_route_reaches(grid, start, destinations):
    """Observed exit stands, or the nearest current-window boundary toward unseen stands.

    Shared with the bounded debris repair. A boundary is a local planning/read target,
    never an exit, an open cell, or evidence that the destination was reached.

    ADR 108 (a48 d14-d16, 6 of 10 exit_not_offered): the nearest boundary was 42,55, water, and the open cells beside it are
    an island. No repair can reach that goal, so no read and no clear ran, and the survey walked until it ran out. A boundary
    counts only when her side of the water reaches it; a closed cell ('#', 's', 'b') may still be debris, so it does not cut.
    """
    goals = {tuple(t) for t in destinations}
    if not grid or not goals:
        return []
    observed = goals & set(grid)
    if observed:
        return sorted(observed)
    distance = lambda t: min(abs(t[0]-d[0]) + abs(t[1]-d[1]) for d in goals)
    start = tuple(start)
    side, todo = {start}, deque([start])
    while todo:
        x, y = todo.popleft()
        for n in ((x, y - 1), (x - 1, y), (x + 1, y), (x, y + 1)):
            if n not in side and grid.get(n) in (".", "#", "s", "b"):
                side.add(n)
                todo.append(n)
    boundary = [t for t in side if t in grid and any(n not in grid for n in
                ((t[0]-1, t[1]), (t[0]+1, t[1]), (t[0], t[1]-1), (t[0], t[1]+1)))]
    best = min((distance(t) for t in boundary), default=distance(start))
    return sorted(t for t in boundary if distance(t) == best) if best < distance(start) else []


ROUTE_MEMORY_CELLS_MAX = 20000  # cells remembered for one hop (a 40x40 window is 1600)
ROUTE_PLAN_CELLS_MAX = 40000  # native map cells a plan may search; a larger or unknown map keeps the observed box
ROUTE_SEEN_LOCATIONS_MAX = 32
ROUTE_CLOSED_CELLS = ("#", "s", "b")  # may be natural debris until a read says otherwise; water and warps never are
ROUTE_PLAN_REFUSALS_MAX = 12  # first-blockers one hop may rule out before the plan yields to the survey


def route_bounds(facts, location):
    """(x1, y1) of the native map for route planning, or None. ADR 125: the observed box is not the map."""
    width, height = interact._location_size(facts, location)
    return (width - 1, height - 1) if 0 < width * height <= ROUTE_PLAN_CELLS_MAX else None


def route_box(seen, goals, bounds):
    """Where an unobserved cell may be optimistic: the native map when known, else the observed box. Always holds the goals."""
    xs = [p[0] for p in seen] + [g[0] for g in goals]
    ys = [p[1] for p in seen] + [g[1] for g in goals]
    if bounds:
        return 0, 0, max(bounds[0], *(g[0] for g in goals)), max(bounds[1], *(g[1] for g in goals))
    return max(0, min(xs)), max(0, min(ys)), max(xs), max(ys)


def route_waypoint(grid, start, reaches, memory, bounds=None):
    """GAP8H (route J1 L852-L1101): the next leg of a hop, planned over every cell observed during this hop.

    The rolling 40x40 nav grid forgot the corridor it had just chosen: at Mountain 27,8 the window
    [8,0,47,39] showed the way south around the x36-40 block toward the Mine (best distance 8); at 23,12 the
    window moved back to [0,0,39,39], the best-distance memory (8) beat every cell left in that window, and the
    survey turned to frontiers west and south (L904-L1101, ~95 inputs). Here, cells observed earlier in this
    hop are guidance only (the current read overrides them; never-observed cells inside the observed box are
    optimistic). Only the current grid authorizes the returned leg: its cells are the plan's prefix that is
    explicitly open now, at most WAYPOINT_PATH_MAX cells. None when no such leg exists (the caller keeps the
    original survey). No exit, warp or walkability is manufactured.

    ADR 125 (a53 d12 J581): with only x>=40 observed, the box ended at x=40, so the open way west did not exist for the
    plan. ``bounds`` (the native map's last cell) replaces the box for the optimistic cells.
    """
    import heapq
    start = tuple(start)
    goals = {tuple(t) for t in reaches}
    if not grid or grid.get(start) != "." or not goals:
        return None
    memory["planned_length"] = None
    seen = memory.setdefault("seen", {})
    seen.update(grid)
    if len(seen) > ROUTE_MEMORY_CELLS_MAX:
        memory["seen"] = seen = dict(grid)
    x0, y0, x1, y1 = route_box(seen, goals, bounds)

    def passable(p):
        if p in seen:
            return seen[p] == "."
        return x0 <= p[0] <= x1 and y0 <= p[1] <= y1  # unobserved inside the observed box: optimistic

    h = lambda p: min(abs(p[0] - g[0]) + abs(p[1] - g[1]) for g in goals)
    cost, parents, todo = {start: 0}, {start: None}, [(h(start), 0, start)]
    end = None
    while todo:
        _, c, p = heapq.heappop(todo)
        if c != cost[p]:
            continue
        if p in goals:
            end = p
            break
        for n in ((p[0], p[1] - 1), (p[0] - 1, p[1]), (p[0] + 1, p[1]), (p[0], p[1] + 1)):
            if c + 1 < cost.get(n, float("inf")) and passable(n):
                cost[n], parents[n] = c + 1, p
                heapq.heappush(todo, (c + 1 + h(n), c + 1, n))
    if end is None:
        return None
    path = [end]
    while parents[path[-1]] is not None:
        path.append(parents[path[-1]])
    path.reverse()
    leg = 0
    while leg + 1 < len(path) and leg < WAYPOINT_PATH_MAX and grid.get(path[leg + 1]) == ".":
        leg += 1
    memory["planned_length"] = len(path) - 1
    return path[leg] if leg else None


def route_repair_plan(grid, start, reaches, memory, *, bounds=None):
    """ADR 125: the hop's next step when no open route exists over its observed cells.

    a53 d8 J183-J1110: no open cell connects the Farm to its south exit; 2 natural pieces do (31,55 / 31,58). The repair
    only ran with the exit's own stand inside the 40x40 window, which is why Backwoods (stand 41,1, d9 J2483) got its 2 clears
    and Forest (stand y=63) got none in 8 trips. Here the fewest closed cells, then fewest steps, over this hop's memory: an
    unobserved cell is optimistic, water/warps and ``memory['closed']`` (read, not removable) stay closed. The path is guidance.

    Returns {leg, blocker, reaches, pieces, length}: ``reaches`` set when the open way to the first closed cell is observed now,
    so the bounded read/clear can run; else ``leg``, the longest prefix of at most WAYPOINT_PATH_MAX current open cells.
    None when no such path or no current-grid step exists. Never a removal, an open cell or an arrival.
    """
    import heapq
    start = tuple(start)
    goals = {tuple(t) for t in reaches}
    if not grid or grid.get(start) != "." or not goals:
        return None
    seen = {**memory.get("seen", {}), **grid}
    closed = {tuple(t) for t in memory.get("closed", ())}
    x0, y0, x1, y1 = route_box(seen, goals, bounds)

    def pieces(p):
        cell = seen.get(p)
        if cell is None:
            return 0 if x0 <= p[0] <= x1 and y0 <= p[1] <= y1 else None
        if cell == ".":
            return 0
        return 1 if cell in ROUTE_CLOSED_CELLS and p not in closed else None

    costs, parents, todo = {start: (0, 0)}, {start: None}, [(0, 0, start)]
    end = None
    while todo:
        count, steps, p = heapq.heappop(todo)
        if costs[p] != (count, steps):
            continue
        if p in goals:
            end = p
            break
        for n in ((p[0], p[1] - 1), (p[0] - 1, p[1]), (p[0] + 1, p[1]), (p[0], p[1] + 1)):
            extra = pieces(n)
            if extra is None:
                continue
            score = (count + extra, steps + 1)
            if n not in costs or score < costs[n]:
                costs[n], parents[n] = score, p
                heapq.heappush(todo, (*score, n))
    if end is None:
        return None
    path = [end]
    while parents[path[-1]] is not None:
        path.append(parents[path[-1]])
    path.reverse()
    first = next((i for i, p in enumerate(path) if seen.get(p, ".") != "."), None)
    if first is None:
        return None  # an open/optimistic path: route_waypoint owns it
    plan = dict(blocker=list(path[first]), pieces=costs[end][0], length=len(path) - 1, leg=None, reaches=None)
    if path[first] in grid and all(grid.get(p) == "." for p in path[:first]):
        beyond = path[first]
        for p in path[first + 1:]:
            if p not in grid:
                break
            beyond = p
            if grid[p] == ".":
                break
        plan["reaches"] = [list(beyond)]
        return plan
    leg = 0
    while leg + 1 < first and leg < WAYPOINT_PATH_MAX and grid.get(path[leg + 1]) == ".":
        leg += 1
    if not leg:
        return None
    plan["leg"] = path[leg]
    return plan


def survey_leg(state, here, tile):
    """ADR 125 (a53 d9 J326-J692, d10, d11, d12 J581-J821): a survey picks its frontier from the current window, so two poses
    in two windows sent each other back, 32 inputs a trip until the watchdog. A repeated leg is exhausted, not a new survey."""
    if tile is None:
        return None
    legs = state.setdefault("survey_legs", set())
    if (tuple(here), tuple(tile)) in legs:
        return None
    legs.add((tuple(here), tuple(tile)))
    return tile


def route_seen_cells(runtime, location):
    """ADR 125: one location's observed cells, kept across this runtime's trips as guidance; a person remembers the farm.

    The current read overrides them and only the current grid authorizes a leg, as for one hop's memory (GAP8H)."""
    store = getattr(runtime, "_route_seen_by_location", None)
    if store is None:
        store = runtime._route_seen_by_location = {}
    if location not in store and len(store) >= ROUTE_SEEN_LOCATIONS_MAX:
        store.pop(next(iter(store)))
    return store.setdefault(location, {})


def _current_date(runtime, pin):
    try:
        return runtime._date_tuple(runtime._state.latest(pin.player_id))
    except Exception:  # noqa: BLE001 - no fresh date: no remembered crossing applies
        return None


def warp_dead_refs(runtime, pin) -> frozenset:
    """PART_B4: warp refs of ``pin``'s location whose move_through read NOT_COMPLETED:NO_EFFECT with the actor
    tile unchanged earlier on this same in-game day (any goal of this runtime). Never a new exit; only a ranking."""
    memory = getattr(runtime, "_warp_no_effect", None)
    if not memory or pin is None or memory.get("date") is None or memory.get("date") != _current_date(runtime, pin):
        return frozenset()
    return frozenset(ref for location, ref in memory.get("refs", ()) if location == getattr(pin, "location", None))


class GoalRoutesRuntimeMixin:
    def _home_navigation_entries(self, goal, pin, here, facts):
        """Existing observed exits, split into current-grid local legs."""
        nav = goal.interact_objective
        from arti_stardew_world import current_catalog, route
        catalog = current_catalog(self, pin)
        # ADR 148: recover an already native route through a knowledge.world gap.
        # Legacy itinerary legs keep their existing crossing/budget semantics.
        direct_nav = (replace(nav, world_route=(nav.target,), route_leg=True)
                      if nav is not None and nav.kind == "enter_location" and nav.target
                      and nav.world_route is not None and catalog is None else None)
        direct = (interact.navigation_entries(facts, tuple(here), direct_nav, pin.location)
                  if direct_nav is not None else [])
        if direct:
            nav.world_route = (nav.target,)
            native_nav = direct_nav
        elif nav is not None and nav.world_route is not None and catalog is None:
            goal.home_route_state["route_reaches"] = []
            return []  # a chunk/resync gap cannot revive a curated route or grant a stale crossing
        elif nav is not None and nav.kind == "enter_location" and catalog is not None:
            path = route({"world_knowledge": catalog}, nav.target)
            nav.world_route = tuple(path or ())
            nav.world_transition_limit = max(nav.world_transition_limit, nav.transitions + len(path or ()))
            # Native routing replaces the itinerary; the existing local solver/motor still owns every hop.
            native_nav = replace(nav, target=path[0], alias=path[0], route_leg=True,
                                 world_route=(path[0],)) if path else nav
        else:
            native_nav = None
        # Live7 R3 J2019-2043: HOME only accepted a direct Farm exit, so Mountain's
        # native Backwoods exit vanished. J2089-2219: Backwoods then skipped this
        # corridor planner and greedily approached the distant Farm warp. Use a
        # local route objective up to Farm; retain the original exact home token
        # and its independent transition verifier. Other acceptance lanes keep
        # their existing HOME behavior.
        live_home = (getattr(self, "_live7_active", lambda: False)() and nav is not None
                     and nav.alias == interact.HOME and pin.location != "Farm"
                     and pin.location != nav.target
                     and (next_hop(pin.location, "Farm") is not None or
                          any(isinstance(f, Mapping) and f.get("target_location") == "Farm"
                              and f.get("kind") in ("warp", "door", "building_door")
                              and f.get("npc_only") is not True for f in facts.values())))
        if live_home and native_nav is None:
            nav.itinerary_home = True  # native crossing admission uses the same hop as the walking catalog
            nav = replace(nav, alias="Farm", target="Farm")
        if native_nav is not None:
            nav = native_nav
        final_live_exit = (nav is not None and nav.transitions == interact.MAX_TRANSITIONS
            and getattr(self, "_live7_active", lambda: False)()
            and bool(interact.navigation_entries(facts, tuple(here), replace(nav, route_leg=True), pin.location)))
        routed = native_nav is not None or (nav is not None and nav.kind == "enter_location" and not nav.route_leg
                  and nav.alias != interact.HOME and not nav.blocker
                  and (nav.transitions < interact.MAX_TRANSITIONS or final_live_exit))
        # PART_B4: the current native grid and this day's observed no-effect crossings rank a warp whose
        # crossing cell is closed (Beach 40,0) after an open one to the same place (38,-1 / 39,-1).
        nav_grid = getattr(self, "_nav_grid", None)
        raw_grid = nav_grid(pin) if nav_grid is not None else None
        dead = warp_dead_refs(self, pin)
        if routed and native_nav is None:
            # Prefer an actual direct exit, then the next hop of an existing
            # itinerary. Only navigation_entries can turn native facts into exits.
            direct = interact.navigation_entries(facts, tuple(here), replace(nav, route_leg=True), pin.location,
                                                 grid=raw_grid, excluded=dead)
            from arti_stardew_part_b import final_day_routes
            hop = nav.target if direct else next_hop(pin.location, nav.target, final_day_routes(self))
            if hop is not None:
                nav = replace(nav, target=hop, route_leg=True)
            else:
                routed = False
        entries = interact.navigation_entries(facts, tuple(here), nav, pin.location, grid=raw_grid, excluded=dead)
        if routed:
            # GAP8F: exits of the itinerary's current hop are this objective's own targets (rank 0), never cut
            # from the eight-entry catalog by nearer actionable objects.
            entries = [dict(e, route_hop=True) for e in entries]
        # Native exit stands remain the intent even if the survey has no waypoint.
        goal.home_route_state["route_reaches"] = [tile for entry in entries
            for tile in (entry.get("reach_tiles") or [entry["tile"]])]
        if not routed and (nav is None or nav.alias != interact.HOME or pin.location != "Farm"):
            return entries
        return GoalRoutesRuntimeMixin._hop_entries(self, goal, pin, here, facts, raw_grid, entries, (pin.location, nav.target))

    def _hop_entries(self, goal, pin, here, facts, raw_grid, entries, basis):
        """One hop toward ``entries[0]``, split into current-grid legs: an exit of a trip, or forage she chose (FIX73F). ADR 125."""
        state = goal.home_route_state
        if state.get("basis") != basis:
            reaches_seen = state.get("route_reaches", [])
            state.clear()
            state["basis"] = basis
            state["route_reaches"] = reaches_seen
        if not entries:
            return []  # no native door fact: never manufacture a home offer
        target = entries[0]
        from arti_stardew_animal_routine import actor_cell_open
        # A23: a pet/animal/farmer on ARTI's own Farm cell marks it '#'; open only that cell.
        grid = actor_cell_open(raw_grid, here)
        parents = open_paths(grid, here)
        # ADR 210: a destination may have several native exits. The first
        # (geometrically nearest) one can be on an inaccessible side of a
        # river. Plan toward every observed exit to that same next map.
        exit_kinds = ("warp", "door", "building_door")
        exits = ([e for e in entries if e.get("kind") in exit_kinds
                  and e.get("target_location") == target.get("target_location")]
                 if target.get("kind") in exit_kinds and target.get("target_location") else [target])
        reaches = list(dict.fromkeys(tuple(t) for e in exits for t in (e.get("reach_tiles") or [e["tile"]])))
        paths = [p for tile in reaches if (p := open_path(parents, tile)) is not None]
        path = min(paths, key=lambda p: (len(p), p[-1])) if paths else None
        if path:
            target = next(e for e in exits if tuple(path[-1]) in
                          [tuple(t) for t in (e.get("reach_tiles") or [e["tile"]])])
        # B20: reserve a hop once from the observed-grid route length. When
        # the rolling grid omits the exit, use its native distance, never an
        # invented route. Re-observation cannot refill this allowance.
        if "input_budget" not in state:
            distance = len(path) - 1 if path else min(abs(x - here[0]) + abs(y - here[1]) for x, y in reaches)
            state["inputs_start"] = self._physical_action_publications
            state["input_budget"] = min(160, max(32, 2 * distance + 8))
        if not parents:
            # Preserve the existing nearby-door path; only the new distant leg
            # requires an observed-grid waypoint.
            return [e for e in entries if max(abs(e["tile"][0] - here[0]), abs(e["tile"][1] - here[1]))
                    <= interact.NAVIGATION_REACH]
        held = state.get("waypoint")
        progress = goal.approach_progress or {}
        if held and progress.get("target_ref") == held["ref"] and progress.get("status") == "ARRIVED":
            state.pop("waypoint", None)
            held = None
        if held:
            state.pop("repair_reaches", None)  # continue the survey, not a stale optional clear
            return [held] if open_path(parents, held["tile"]) is not None else []
        if path is not None and len(path) - 1 <= WAYPOINT_PATH_MAX:
            state.pop("repair_reaches", None)
            return [target]
        from arti_stardew_animal_routine import house_waypoint
        destination = path[-1] if path else (reaches[0] if reaches else target["tile"])
        # GAP8H: an itinerary hop follows its plan over this hop's observed cells; the original survey
        # (best distance, frontiers, one reset) remains the fallback when no current-grid leg exists.
        # live7-game-m4 D2 (journal L3679-L4036): the Farm home leg from the south of the Forest farm oscillated
        # 53,44 <-> 55,38 for 59 inputs (the 55,38 window saw the exit behind a detour, the 53,44 window did not and
        # its best-distance memory pulled it back), then home_route_unreachable. The home leg plans over the cells
        # observed during this leg too (same rule as an itinerary hop); the survey stays the fallback.
        memory = state.setdefault("route_memory", {})
        if "seen" not in memory:
            memory["seen"] = route_seen_cells(self, pin.location)
        tile = route_waypoint(grid, here, reaches, memory) if not path else None
        state["route_planned"] = tile is not None
        state.pop("repair_plan", None)
        # M5 D1 L267-L573: debris enclosed the eastward route while the BusStop stand was off-window.
        # The survey walked west; the repair's inspect gate required an observed stand and never ran.
        # Reuse its existing boundary projection so bounded native reads/clearing get a chance first.
        # The real native exit stays in route_reaches; no boundary can verify a crossing.
        # Smoke 2026-10-24 D2 (J1197-J1352): the Farm HOME leg (cabin door 25,33 off-window) was never routed, so
        # this repair stayed off; the survey shuttled 33,61 <-> 41,61 on a corridor walled by debris (32,61) and
        # failed exit_not_offered twice. Owner: every chosen trip (any destination, exits, home) may clear natural
        # debris with her tools under the same route_clear piece/tree/input caps; no per-destination rule.
        state["repair_reaches"] = (local_route_reaches(grid, here, reaches)
            if path is None and tile is None
            and getattr(self, "_live7_active", lambda: False)() else [])
        plan = None
        if path is None and tile is None and getattr(self, "_live7_active", lambda: False)():
            # ADR 125: walk first while the map may still hold an open way; cut only once observation rules that out.
            bounds = route_bounds(facts, pin.location)
            tile = route_waypoint(grid, here, reaches, memory, bounds) if bounds else None
            state["route_planned_map"] = tile is not None
            if tile is None and state.get("route_plan_refusals", 0) < ROUTE_PLAN_REFUSALS_MAX:
                plan = route_repair_plan(grid, here, reaches, memory, bounds=bounds)
                tile = plan["leg"] if plan is not None else None
        if tile is None and plan is None:
            tile = house_waypoint(grid, here, destination, state.setdefault("survey", {}))
            tile = tile if path else survey_leg(state, here, tile)
        if tile is None and plan is None and not state.get("survey_reset"):
            # PART_B3: the hop's own earlier leg set best_distance=1 for the
            # cell it was walking toward (a Farm cell whose reach tile is a wall), so on ARRIVAL at the hop
            # that same still-reachable cell was "[recorded remark omitted]" and no waypoint was offered
            # (exit_not_offered). A fresh retry from there offered a waypoint and
            # reached BusStop. A25 precedent: one fresh survey from the current pose; only the
            # current grid authorizes a leg, and the hop's input budget is unchanged.
            state["survey_reset"] = True
            state["survey"] = {}
            tile = house_waypoint(grid, here, destination, state["survey"], window_best=True)
            tile = tile if path else survey_leg(state, here, tile)
        if plan is not None and plan["reaches"] is not None:
            # No waypoint: the held-leg rule below would drop this flag before the repair step reads it (0 of 210).
            state["repair_plan"] = plan
            state["repair_reaches"] = plan["reaches"]
        journal = getattr(self, "_journal", None)
        if journal is not None:
            # B7 retained exit refs but no nav cells, making water/collision vs survey
            # exhaustion indistinguishable offline. Record only at a new local leg or
            # refusal, never on each input of a held waypoint; no model/scene expansion.
            cells = [[x, y, c] for (x, y), c in sorted((raw_grid or {}).items())]
            digest = hashlib.sha256(str((here, target["ref"], tile, cells)).encode()).hexdigest()
            if state.get("survey_diagnostic") != digest:
                state["survey_diagnostic"] = digest
                journal(dict(ev="home_route_survey", location=pin.location, actor_tile=list(here),
                    native_exit_ref=target["ref"], reach_tiles=reaches, grid_cells=cells,
                    exit_path_observed=path is not None, waypoint=list(tile) if tile is not None else None,
                    survey_reset=bool(state.get("survey_reset")),
                    best_distance=state.get("survey", {}).get("best_distance"),
                    survey_windows=list(state.get("survey", {}).get("windows", ())),
                    best_by_window=[[list(w), d] for w, d in sorted(
                        state.get("survey", {}).get("best_by_window", {}).items())],
                    input_budget=state.get("input_budget"), inputs_start=state.get("inputs_start"),
                    route_planned=state.get("route_planned"),
                    repair_reaches=state.get("repair_reaches", []),
                    planned_length=state.get("route_memory", {}).get("planned_length"),
                    route_planned_map=state.get("route_planned_map"),
                    repair_plan=None if plan is None else dict(blocker=plan["blocker"], pieces=plan["pieces"],
                                                                length=plan["length"], ready=plan["reaches"] is not None)))
        if tile is None:
            return []
        held = dict(ref=f"home_waypoint:{pin.location}:{tile[0]},{tile[1]}", kind="home_route_stand",
                    tile=list(tile), reach="DIRECT_TARGET", target_location=target.get("target_location"),
                    revision=hashlib.sha256(str((basis, tile)).encode()).hexdigest()[:16])
        state["waypoint"] = held
        return [held]

    def _warp_no_effect_record(self, args, evidence) -> None:
        """PART_B4 (journal 2547/2576/2604): a warp move_through that settled NOT_COMPLETED:NO_EFFECT, cleanup
        confirmed, with the actor's location and tile unchanged and equal to the move's own actor tile, is an
        observed no-effect crossing. Remembered for this in-game day only; it never grants or verifies anything."""
        if (not isinstance(args, Mapping) or args.get("kind") != interact.KIND_WARP
                or not isinstance(args.get("source_fact_id"), str) or evidence is None):
            return
        before, after = evidence.before or {}, evidence.after or {}
        if (evidence.lifecycle != "completed" or evidence.outcome != "NOT_COMPLETED:NO_EFFECT"
                or evidence.cleanup_confirmed is not True or not before or not after
                or before.get("location") is None or before.get("location") != after.get("location")
                or list(before.get("tile") or ()) != list(after.get("tile") or ())
                or list(before.get("tile") or ()) != [args.get("actor_tile_x"), args.get("actor_tile_y")]):
            return
        pin = SimpleNamespace(player_id=before.get("player_id"), location=before["location"])
        date = _current_date(self, pin)
        if date is None:
            return
        memory = getattr(self, "_warp_no_effect", None)
        if not memory or memory.get("date") != date:
            memory = self._warp_no_effect = {"date": date, "refs": set()}
        key = (before["location"], args["source_fact_id"])
        if key not in memory["refs"]:
            memory["refs"].add(key)
            self._journal({"ev": "warp_no_effect_recorded", "location": key[0], "ref": key[1],
                           "actor_tile": list(before["tile"])})

    def _warp_through_refused(self, goal, pin, through) -> bool:
        """PART_B4: withhold the mod's walk-onto warp offer only when its crossing is observed closed (current nav
        grid) or no-effect (today), AND another native warp to the same destination is not. The navigation catalog
        then offers that warp's stand. With no alternative the offer stands (old behaviour, old refusal)."""
        if not isinstance(through, Mapping) or through.get("kind") != interact.KIND_WARP or pin is None:
            return False
        try:
            actor = (int(through["actor_tile_x"]), int(through["actor_tile_y"]))
            source = (int(through["tile_x"]), int(through["tile_y"]))
        except (KeyError, TypeError, ValueError):
            return False
        ref = through.get("source_fact_id")
        grid = self._nav_grid(pin)
        dead = warp_dead_refs(self, pin)
        closed = interact.warp_crossing_closed(grid, actor, source)
        if ref not in dead and not closed:
            return False
        rec = self._obs.get(pin.player_id, "scene.current_location")
        alternatives = interact.open_warp_alternatives(getattr(rec, "facts", None), pin.location,
                                                       through.get("target_location_id"), grid, dead | {ref})
        if not alternatives:
            return False
        note = (ref, tuple(alternatives))
        if getattr(self, "_warp_withheld_note", None) != note:
            self._warp_withheld_note = note
            self._journal({"ev": "warp_through_withheld", "ref": ref, "actor_tile": list(actor),
                           "reason": "observed_no_effect_today" if ref in dead else "crossing_cell_closed",
                           "alternatives": alternatives[:4]})
        return True

    def _goal_route_facts(self, pin):
        facts = self._fishing_ui_facts(pin)  # existing <=2 s / complete / authoritative / session-screen guard
        raw = facts.get("goal_route") if isinstance(facts, Mapping) else None
        motor = self._state.latest(pin.player_id) if pin is not None else None
        if (not isinstance(raw, Mapping) or motor is None or self._state.is_stale(pin.player_id)
                or motor.location != pin.location
                or raw.get("location") != pin.location or list(motor.tile or ()) != raw.get("actor_tile")
                or raw.get("date") != list(self._date_tuple(motor) or ())
                or raw.get("time_of_day") != motor.time_of_day):
            return None
        return raw

    def _goal_route_kind(self, goal):
        if self._economy_counter_goal(goal):
            return goal.economy_objective.kind
        fishing = getattr(goal, "fishing", None)
        return "fish" if fishing is not None and fishing.status == "OPEN" else None

    def _goal_route_owns(self, goal):
        route = getattr(goal, "goal_route", None)
        curfew = getattr(goal, "curfew_stage", None) or {}
        return bool(route and route["status"] != "arrived" and self._goal_route_kind(goal) == route["kind"]
                    and curfew.get("status") not in ("home", "bed"))

    def _goal_route_action_reason(self, goal, pin):
        if not self._goal_route_owns(goal):
            return None
        route = goal.goal_route
        if route["status"] == "blocked":
            return route["blocker"]
        facts = self._goal_route_facts(pin)
        reason = self._goal_route_precondition(goal, route["kind"], facts)
        if reason:
            return reason
        if facts["date"] != route["date"]:
            return "route_date_changed"
        if game_now(self) >= route["deadline"]:
            return "route_budget_exhausted"
        if not self._nav_grid(pin):
            return "facts_unavailable:route_grid"
        return None

    def _goal_route_step_reason(self, goal, pin, motor, args):
        """Publication guard for the actual cardinal proposal, including a still-offered target."""
        if not self._goal_route_owns(goal):
            return None
        grid = self._nav_grid(pin)
        here = tuple(motor.tile or ())
        if not grid or grid.get(here) != ".":
            return "facts_unavailable:route_grid"
        dx, dy = {"up": (0,-1), "right": (1,0), "down": (0,1), "left": (-1,0)}[args["direction"]]
        next_tile = (here[0]+dx, here[1]+dy)
        if grid.get(next_tile) == ".":
            return None
        target = goal.approach_target
        # A one-tick facing input at a verified door stand points into the solid door;
        # it is not a walking proposal. Door/warp effects retain their native verifiers.
        if (args["ticks"] == 1 and target is not None and target.reach_kind != "DIRECT_TARGET"
                and here in target.reach_tiles and next_tile == target.target_tile
                and grid.get(next_tile) == "#"):
            return None
        return "route_step_not_observed_open"

    def _goal_route_precondition(self, goal, kind, facts):
        if facts is None:
            return "facts_unavailable:goal_route"
        season = facts["date"][1]
        if season not in seeds.SEASONS:
            return "facts_unavailable:route_season"
        if kind in ("sell", "buy"):
            if type(facts.get("shop_open")) is not bool:
                return "facts_unavailable:shop_hours"
            if not facts["shop_open"]:
                return "precondition:pierre_closed"
            if kind == "buy":
                obj = goal.economy_objective
                wanted = (obj.item_id or obj.item_name or obj.item_text).casefold()
                qid = next((q for q, (name, _) in seeds.CROP_SEEDS.items()
                            if wanted in (q.casefold(), name)), None)
                if qid is not None and seeds.seed_in_season(qid, season) is False:
                    return "precondition:buy_seed_out_of_season"
        else:
            rod = facts.get("rod_owned")
            if type(rod) is not bool:
                return "facts_unavailable:route_rod"
            if not rod:
                return "precondition:no_fishing_rod"
        return None

    def _goal_route_stop(self, goal, reason):
        route = goal.goal_route
        route["status"], route["blocker"] = "blocked", reason
        goal.interact_objective = None
        self._journal({"ev": "goal_route", "goal_id": goal.goal_id, "status": "blocked", "reason": reason})

    @staticmethod
    def _goal_route_replans(goal):
        route = goal.goal_route
        progress = goal.approach_progress or {}
        if progress.get("status") == "REPLAN_REQUIRED":
            key = (progress.get("target_ref"), progress.get("steps"))
            if key != route.get("last_replan"):
                route["last_replan"] = key
                route["replans"] += 1
        return route["replans"] > ROUTE_REPLANS_MAX

    def _goal_route_prepare(self, goal, pin):
        """Snapshot hook, after location invalidation and before catalog construction. Never performs input."""
        route = getattr(goal, "goal_route", None)
        kind = self._goal_route_kind(goal)
        if (kind is None or pin is None or goal.play_mode != "local_activity" or not self._v2_pad_path(pin)
                or self.curfew_window_now() or getattr(self, "_skill_session", None) is not None):
            return
        if route is not None and not self._goal_route_owns(goal):
            return
        if route is None:
            if pin.location != "Farm" or goal.interact_objective is not None:
                return  # preserve already-local shop/fish, including the live-proven Willy counter
            ui = self._fishing_ui_facts(pin) or {}
            fishing = ui.get("fishing") if isinstance(ui.get("fishing"), Mapping) else {}
            if kind == "fish" and fishing.get("water_edges"):
                return  # an existing local fishing edge still wins
            # Old DLLs keep FIXB34's honest diagnostics; a new route requires an explicit publication.
            if "goal_route" not in ui:
                return
            goal.goal_route = route = dict(kind=kind, path=ITINERARIES[kind], index=0, status="travel",
                                           steps=0, replans=0, deadline=game_now(self) + ROUTE_SECONDS,
                                           state={}, waypoint=None)
            self._journal({"ev": "goal_route", "goal_id": goal.goal_id, "status": "started", "kind": kind,
                           "from": pin.location, "destination": route["path"][-1]})
        if route["status"] in ("blocked", "arrived"):
            return
        facts = self._goal_route_facts(pin)
        reason = self._goal_route_precondition(goal, kind, facts)
        if reason:
            self._goal_route_stop(goal, reason)
            return
        if "date" not in route:
            route["date"] = facts["date"]
        if facts["date"] != route["date"]:
            self._goal_route_stop(goal, "route_date_changed")
            return
        action = goal.last_action
        if action is not None and (not action.cleanup_confirmed or action.lifecycle in ("uncertain", "interrupted")):
            self._goal_route_stop(goal, "route_action_unreconciled")
            return
        path, index = route["path"], route["index"]
        if pin.location != path[index]:
            nav = goal.interact_objective
            if (index + 1 >= len(path) or pin.location != path[index + 1] or nav is None
                    or interact.objective_verdict(nav, pin.location)[0] != "achieved"):
                self._goal_route_stop(goal, "route_transition_unverified")
                return
            route["index"] = index = index + 1
            route["state"], route["waypoint"] = {}, None
            goal.interact_objective = None
            self._journal({"ev": "goal_route", "goal_id": goal.goal_id, "status": "leg_arrived",
                           "location": pin.location, "transitions": nav.transitions})
        if game_now(self) >= route["deadline"] or route["steps"] >= ROUTE_STEPS_MAX:
            self._goal_route_stop(goal, "route_budget_exhausted")
            return
        if self._goal_route_replans(goal):
            self._goal_route_stop(goal, "route_replan_exhausted")
            return
        if index + 1 < len(path) and goal.interact_objective is None:
            nav = interact.InteractObjective("enter_location", alias=path[index + 1], route_leg=True)
            interact.resolve_objective(nav, current_location=pin.location, home_location=None)
            goal.interact_objective = nav
            goal.location_basis = pin.location

    def _goal_route_entries(self, goal, pin, entries):
        route = getattr(goal, "goal_route", None)
        if not self._goal_route_owns(goal):
            return entries
        if route["status"] == "blocked":
            return []
        facts = self._goal_route_facts(pin)
        if facts is None:
            return []
        motor = self._state.latest(pin.player_id)
        here = tuple(motor.tile)
        grid = self._nav_grid(pin)
        parents = open_paths(grid, here)
        if not parents:
            return []
        nav = goal.interact_objective
        if nav is not None:
            targets = [e for e in entries if e.get("target_location") == nav.target
                       and e.get("kind") in ("warp", "door", "building_door")]
            if not targets:
                return []
            target = targets[0]
        else:
            from arti_stardew_native_targets import work_stands, fishing_search_target, FISHING_STAND_GAP
            import arti_stardew_world_extras as extras
            ui = self._fishing_ui_facts(pin) or {}
            stands = work_stands(dict(extras=extras.extras_targets(ui), fishing=ui.get("fishing") or {}),
                                 "fish" if route["kind"] == "fish" else "counter")
            native_stand = bool(stands)
            if route["kind"] == "fish" and not stands:
                stand = fishing_search_target(grid, here, route["state"].setdefault("water_search", {}))
                if stand is None:
                    self._goal_route_stop(goal, FISHING_STAND_GAP)
                    return []
                stands = (stand,)
            paths_to_work = [p for t in stands if (p := open_path(parents, t)) is not None]
            if not paths_to_work:
                return []
            stand = min(paths_to_work, key=lambda p: (len(p), p[-1]))[-1]
            route["work_stand"] = stand if native_stand else None
            prefix = "route_landmark:" if native_stand else "route_water_search:"
            target = dict(ref=prefix + pin.location, kind="route_stand", tile=list(stand),
                          reach="DIRECT_TARGET", revision=hashlib.sha256(str(stand).encode()).hexdigest()[:16])
        # Doors occupy blocked cells: route to an explicitly published reach stand.
        reaches = ([target["tile"]] if target.get("reach") == "DIRECT_TARGET"
                   else target.get("reach_tiles", []))
        paths = [p for t in reaches if (p := open_path(parents, t)) is not None]
        destination = tuple(target["tile"])
        path = min(paths, key=lambda p: (len(p), p[-1])) if paths else None
        if path is not None:
            destination = path[-1]
        # Keep each observed waypoint until ARRIVED. A blocked waypoint never gets a new budget by target churn.
        held = route.get("waypoint")
        progress = goal.approach_progress or {}
        if held and progress.get("target_ref") == held["ref"] and progress.get("status") == "ARRIVED":
            route["waypoint"] = held = None
        if held:
            return [held] if open_path(parents, held["tile"]) is not None else []
        if abs(here[0] - destination[0]) + abs(here[1] - destination[1]) <= WAYPOINT_PATH_MAX:
            # Close targets may need a detour longer than eight cells; keep that walk bounded too.
            if path is None:
                return []
            if len(path) - 1 <= WAYPOINT_PATH_MAX:
                return [target]
        tile = path[min(WAYPOINT_PATH_MAX, len(path)-1)] if path else waypoint(grid, here, destination)
        if tile is None:
            return []
        entry = dict(ref=f"route_waypoint:{pin.location}:{tile[0]},{tile[1]}", kind="route_stand", tile=list(tile),
                     reach="DIRECT_TARGET", revision=hashlib.sha256(str((pin.location, tile)).encode()).hexdigest()[:16])
        route["waypoint"] = entry
        return [entry]

    def _goal_route_invoke(self, request):
        import arti_stardew_skills as skills
        with self._lock:
            goal = self._active
            route = getattr(goal, "goal_route", None) if goal is not None else None
            if not self._goal_route_owns(goal) or request.goal_id != goal.goal_id or self.curfew_window_now():
                return None
            if route["status"] == "blocked":
                return self._rule_json(request, {"kind": "wait", "reason": route["blocker"], "ms": 0}, "goal_route", "blocked")
            snap = request.snapshot
            progress = goal.approach_progress or {}
            if self._goal_route_replans(goal) or progress.get("status") in ("BLOCKED", "BUDGET_EXHAUSTED", "UNCERTAIN"):
                self._goal_route_stop(goal, "route_replan_exhausted")
                return self._goal_route_invoke(request)
            route["steps"] += 1
            pin = snap.water_context or snap.movement_context
            if route["steps"] > ROUTE_STEPS_MAX or game_now(self) >= route["deadline"]:
                self._goal_route_stop(goal, "route_budget_exhausted")
                return self._goal_route_invoke(request)
            if goal.interact_objective is None and pin is not None:
                ref = "route_landmark:" + pin.location
                if (tuple(snap.motor.tile or ()) == route.get("work_stand") and progress.get("status") == "ARRIVED"
                        and progress.get("target_ref") == ref):
                    route["status"] = "arrived"
                    self._journal({"ev": "goal_route", "goal_id": goal.goal_id, "status": "arrived", "location": pin.location})
                    return None  # original domain rule/ledger decides completion
            stand = next((t for t in snap.approach_targets or () if t.get("kind") == "route_stand"), None)
            if stand is not None:
                choice = skills.act("approach_target", {}, stand["ref"])
            else:
                state = route["state"]
                state["nav_objective"] = goal.interact_objective
                step = skills.plan_step(skills.SkillCall("pergi_ke", "farm"), snap, state=state)
                if step.kind != "decision" or step.choice.get("kind") == "finish":
                    self._goal_route_stop(goal, "route_target_not_offered")
                    return self._goal_route_invoke(request)
                choice = step.choice
            return self._rule_json(request, choice, "goal_route", str(choice.get("operation")))
