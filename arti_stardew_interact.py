"""WS3a (Primitives v1, 2026-10-07): door / walk-onto warp / forage helpers for the runtime.

Pure functions only: no IO, no provider, no command inbox. The runtime
(`arti_stardew_runtime.py`) calls these at small hook sites. The mod stays authoritative
for every target (`world_interaction_target`, `move_through_target`); these helpers only
decide presentation (what to offer), objective bookkeeping from authenticated owner text,
readback classification and which location-pinned goal state a location change voids.

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the CT live witness.

WS3b extension points (not implemented here): kind "npc" (talk objective, dialogue
readback, `ui_key` close), move_through kind "bed" (sleep objective, question readback),
"eat" — add them to OUTCOME/EFFECT tables, `parse_interact_objective` and
`objective_verdict` without reshaping the existing kinds.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

KIND_CHEST = "chest"
KIND_DOOR = "door"
KIND_FORAGE = "forage"
KIND_WARP = "warp"

EFFECT_LOCATION = "observed_location_change"
EFFECT_PICKUP = "observed_item_pickup"
EFFECT_NONE = "no_effect_observed"
INTERACT_EFFECTS = frozenset({EFFECT_LOCATION, EFFECT_PICKUP, EFFECT_NONE})

OUTCOME_NO_EFFECT = "InteractionNoEffect"
OUTCOME_TRANSITION = "TransitionObserved"

# A farmhand warp waits for host location data (mod WarpWaitMs 8 s); readback polls a bit longer.
WARP_READBACK_SECONDS = 10.0
# D16 (live B3W5b): the 2.5 s window missed the pickup (the farmer was still in the pickup animation,
# can_player_move=false). The readback waits through it, bounded.
PICKUP_READBACK_SECONDS = 4.0
FORAGE_REACH = 12
MOVE_THROUGH_DEFAULT_TICKS = 20
# Live B3W6c / B4W1a: a distance-2 warp move_through with ticks 20 travelled 50 / 74.3 px (2.5-3.7 px per
# tick; slower than a plain move) and stopped short of the warp. Worst case from the far side of the
# actor tile is 64*(d-1)+48 px to put the bounding box on the warp row; sized at 2.5 px/tick + 2. Extra
# ticks are harmless: the mod stops asking for H once the warp starts (same run, same containment).
MOVE_THROUGH_TICKS_MAX = 48
WARP_THROUGH_TICKS = {1: 22, 2: 47}
NAVIGATION_REACH = 24
MAX_TRANSITIONS = 3
MAX_FORAGE = 5

# FIXB48: these native animal-product identities need BOTH readbacks and exactly one gain.
# Admission/category/production flags remain native; this list never grants an interaction.
ANIMAL_PRODUCT_IDS = frozenset(f"(O){n}" for n in (176, 180, 174, 182, 442, 305, 928, 289, 107, 440, 444, 446))

RESULT_SCOPE = {
    KIND_CHEST: "matching receiver, not transfer/completion",
    KIND_DOOR: "location change to target_location_id",
    "animal_house_route": "native route and verified arrival at exact target_location_id",
    KIND_FORAGE: "item enters inventory",
}

# Owner aliases -> location tokens (SceneIdentity.Token of the vanilla names is the name itself).
_LOCATION_ALIASES = {
    "farm": "Farm", "ke farm": "Farm", "forest": "Forest", "hutan": "Forest",
    "the forest": "Forest", "cindersap forest": "Forest", "town": "Town", "kota": "Town",
    "the town": "Town", "bus stop": "BusStop", "busstop": "BusStop", "the bus stop": "BusStop",
    "backwoods": "Backwoods", "the backwoods": "Backwoods", "seed shop": "SeedShop",
    "the seed shop": "SeedShop", "toko benih": "SeedShop",
    "beach": "Beach", "pantai": "Beach", "mountain": "Mountain", "mine": "Mine",
    # W1 real attempts (owner 2026-10-18): Town interiors reached only through their native exit facts.
    "blacksmith": "Blacksmith", "community center": "CommunityCenter", "museum": "ArchaeologyHouse",
    # S26 real attempt: Marnie's shop, reached only through its native Forest exit fact.
    "animal shop": "AnimalShop",
}
HOME = "home"


@dataclass
class InteractObjective:
    """One owner-authored WS3a objective inside the existing goal (never a model grant)."""
    kind: str                      # enter_location | round_trip | forage_pickup
    alias: str | None = None       # "home", "Farm", ... (enter_location)
    target: str | None = None      # resolved location token (enter_location)
    count: int = 1                 # forage_pickup
    start_location: str | None = None
    visited: str | None = None     # round_trip: where leg one arrived
    transitions: int = 0
    pickups: int = 0
    blocker: str | None = None
    route_leg: bool = False        # FIXB38: one itinerary hop; only its observed destination exit
    itinerary_home: bool = False   # Live7 only: native return hops up to Farm, then the exact owned home
    world_route: tuple[str, ...] | None = None  # native-published route; None = legacy observation
    world_transition_limit: int = MAX_TRANSITIONS

    @property
    def navigation(self) -> bool:
        return self.kind in {"enter_location", "round_trip"}


def interaction_kind(args: object) -> str | None:
    kind = args.get("kind") if isinstance(args, Mapping) else None
    # WS3b kinds "npc" / "eat" are recognised here so they never fall back to the chest store path.
    # Primitives v2 WS-G6 extras kinds are recognised too (never the chest store path; their own chain runs them).
    from arti_stardew_command import WORLD_EXTRAS_KINDS, GENERIC_WORLD_KINDS
    return kind if kind in (KIND_CHEST, KIND_DOOR, KIND_FORAGE, "npc", "eat", "animal_house_route") + WORLD_EXTRAS_KINDS + GENERIC_WORLD_KINDS else None


def parse_interact_objective(text: str) -> InteractObjective | None:
    """Anchored owner grammar (authenticated ingress only; never model/scene text)."""
    t = re.sub(r"\s+", " ", text.strip().rstrip(".!")).casefold()
    if re.fullmatch(r"(?:go|walk) through (?:the |a |one )?(?:farmhouse |house |cabin |building )?(?:door|warp|exit)"
                    r" and (?:come )?back|keluar masuk rumah|lewat (?:satu )?pintu (?:lalu|dan|terus) (?:kembali|balik)", t):
        return InteractObjective("round_trip")
    if re.fullmatch(r"(?:go|walk) (?:into|inside) (?:the )?(?:house|cabin|farmhouse|home)|enter (?:the )?(?:house|cabin|home)"
                    r"|masuk (?:ke )?(?:dalam )?(?:rumah|kabin)|pulang(?: ke rumah)?", t):
        return InteractObjective("enter_location", alias=HOME)
    if re.fullmatch(r"go outside|keluar(?: dari)? (?:rumah|kabin)|go (?:out )?to the farm", t):
        return InteractObjective("enter_location", alias="Farm")
    exact = re.fullmatch(r"(?:go to|walk to|pergi ke|jalan ke) @([A-Za-z0-9_.:%~+\-]{1,160})", text.strip().rstrip("!"))
    if exact:
        return InteractObjective("enter_location", alias=exact[1])
    m = re.fullmatch(r"(?:go to|walk to|pergi ke|jalan ke) (.{2,24})", t)
    if m and m[1].strip() in _LOCATION_ALIASES:
        return InteractObjective("enter_location", alias=_LOCATION_ALIASES[m[1].strip()])
    m = re.fullmatch(r"(?:pick up|collect|pungut|ambil) (?:(one|a|an|satu|[1-5]) )?(?:forage|foraged)(?: items?| barang)?", t)
    if m:
        word = m[1] or "1"
        count = 1 if word in {"one", "a", "an", "satu"} else int(word)
        return InteractObjective("forage_pickup", count=count)
    return None


def resolve_objective(objective: InteractObjective, *, current_location: str | None,
                      home_location: str | None) -> str | None:
    """Pin the start location and the target token; returns a blocker or None."""
    if not current_location:
        return "objective_location_unavailable"
    objective.start_location = objective.start_location or current_location
    if objective.kind == "enter_location" and objective.target is None:
        target = home_location if objective.alias == HOME else objective.alias
        if not target:
            return "objective_home_location_unavailable"
        objective.target = target
        if target == current_location and objective.transitions == 0 and objective.alias != HOME:
            return "objective_already_satisfied_no_new_work"
    return None


def record_transition(objective: InteractObjective | None, before: str | None, after: str | None) -> dict | None:
    """A verified location change (door / warp). Returns the objective_progress journal body."""
    if objective is None or not objective.navigation or not before or not after or before == after:
        return None
    objective.transitions += 1
    if objective.kind == "round_trip" and objective.visited is None and before == objective.start_location:
        objective.visited = after
    if objective.transitions > objective.world_transition_limit and objective_verdict(objective, after)[0] != "achieved":
        objective.blocker = "objective_transition_budget_exhausted"
    return {"kind": objective.kind, "from": before, "to": after, "transitions": objective.transitions,
            "visited": objective.visited, "target": objective.target, "blocker": objective.blocker}


def record_pickup(objective: InteractObjective | None, qid: str, delta: int) -> dict | None:
    if objective is None or objective.kind != "forage_pickup" or delta < 1:
        return None
    objective.pickups += 1
    return {"kind": objective.kind, "qid": qid, "delta": delta, "pickups": objective.pickups, "count": objective.count}


def objective_verdict(objective: InteractObjective, current_location: str | None) -> tuple[str, str]:
    """Finish from verified transitions / pickups, or an exact native home start."""
    if objective.blocker:
        return "partial", objective.blocker
    if objective.kind == "forage_pickup":
        return (("achieved", "observed_item_pickup_objective") if objective.pickups >= objective.count
                else ("partial", "forage_pickups_not_satisfied"))
    if not current_location:
        return "uncertain", "objective_location_unavailable"
    if objective.kind == "enter_location":
        # A15: home is a destination, not a required door round trip. The
        # resolved native home token must match both the start and current pin.
        if (objective.alias == HOME and objective.transitions == 0
                and objective.target == objective.start_location == current_location):
            return "achieved", "observed_home_location_objective"
        return (("achieved", "observed_location_change_objective")
                if objective.transitions >= 1 and objective.target == current_location
                else ("partial", "target_location_not_reached"))
    if objective.kind == "round_trip":
        return (("achieved", "observed_round_trip_objective")
                if (objective.transitions >= 2 and objective.visited not in (None, objective.start_location)
                    and current_location == objective.start_location)
                else ("partial", "round_trip_not_completed"))
    return "partial", "objective_unknown"


def objective_summary(objective: InteractObjective | None, current_location: str | None) -> dict | None:
    """D13 (live B3W4a): bounded model-visible view of the WS3a objective, verified progress only.

    Same shape as the WS3b ``routine_objective`` summary ({kind, status, reason, ...}) so the
    existing "objective verified complete: choose finish" guidance applies. Location tokens are
    not echoed (the owner's alias is)."""
    if objective is None:
        return None
    status, reason = objective_verdict(objective, current_location)
    summary = {"kind": objective.kind, "status": status, "reason": reason}
    if objective.navigation:
        summary["legs_done"] = objective.transitions
        summary["legs_needed"] = 2 if objective.kind == "round_trip" else 1
        if objective.kind == "round_trip":
            summary["back_at_start"] = bool(current_location) and current_location == objective.start_location
        elif objective.alias:
            summary["target"] = objective.alias
    else:
        summary["pickups"] = objective.pickups
        summary["count"] = objective.count
    return summary


def exit_offered(objective: InteractObjective | None, target_location: str | None, current_location: str | None) -> bool:
    """Doors / warps are presented only under a navigation objective (never mid-M1 or in free play)."""
    if objective is None or not objective.navigation or objective.blocker or not target_location:
        return False
    if objective.kind == "enter_location":
        if objective.world_route is not None:
            return bool(objective.world_route) and target_location == objective.world_route[0]
        if objective.alias == HOME:
            if objective.itinerary_home and current_location != "Farm" and current_location != objective.target:
                from arti_stardew_goal_routes import next_hop
                hop = next_hop(current_location, "Farm")
                if hop is not None:
                    return (target_location == objective.target or
                            objective.transitions < MAX_TRANSITIONS and target_location == hop)
            # Home on the Farm is the owner's exact door. Else take the observed
            # Farm exit; entering a nearby animal house cannot advance this leg.
            return (target_location == objective.target or
                    (current_location != "Farm" and current_location != objective.target
                     and target_location == "Farm" and objective.transitions < MAX_TRANSITIONS))
        if objective.route_leg:
            return target_location == objective.target
        from arti_stardew_goal_routes import next_hop
        hop = next_hop(current_location, objective.target)
        if hop is not None:
            return (target_location == objective.target or
                    (objective.transitions < MAX_TRANSITIONS and target_location == hop))
        # Direct exit to the target first; any exit while it is not observed here (bounded multi-hop).
        return objective.target is None or target_location == objective.target or objective.transitions < MAX_TRANSITIONS
    # round_trip: leg one leaves the start, leg two only returns to it.
    if current_location == objective.start_location:
        return objective.visited is None
    return target_location == objective.start_location


def interaction_offered(target: Mapping | None, *, gathering: bool, objective: InteractObjective | None,
                        current_location: str | None, inventory_full: bool = False) -> bool:
    """Presentation filter for the mod-observed world_interaction_target (the mod stays authoritative)."""
    kind = interaction_kind(target)
    if kind is None:
        return False
    if kind == KIND_CHEST:
        return True  # M1 store path unchanged
    if kind == KIND_DOOR:
        return not gathering and exit_offered(objective, target.get("target_location_id"), current_location)
    if kind == KIND_FORAGE:
        return not gathering and not inventory_full and (objective is None or objective.kind == "forage_pickup")
    return False


def move_through_offered(target: Mapping | None, *, gathering: bool, objective: InteractObjective | None,
                         current_location: str | None) -> bool:
    return (isinstance(target, Mapping) and target.get("kind") == KIND_WARP and not gathering
            and exit_offered(objective, target.get("target_location_id"), current_location))


def invalidate_location_pins(goal) -> list[str]:
    """Drop every location-pinned field of the goal after an observed location change."""
    cleared = []
    for name, empty in (("approach_controller", None), ("approach_target", None), ("approach_progress", None),
                        ("chest_flush_key", None), ("store_container", None)):
        if getattr(goal, name, None) is not None:
            setattr(goal, name, empty)
            cleared.append(name)
    for name in ("approach_blocked_refs", "approach_no_motion", "approach_noop_arrivals"):
        value = getattr(goal, name, None)
        if value:
            value.clear()
            cleared.append(name)
    if getattr(goal, "approach_steps", 0) or getattr(goal, "approach_deadline", 0):
        # The previous approach ended with the transition; the next location starts its own budget.
        goal.approach_steps, goal.approach_deadline = 0, 0
        cleared.append("approach_budget")
    if getattr(goal, "gather_progress", None) is not None:
        # A gather objective pinned in another location is refused (its stale pin already reads
        # UNAVAILABLE through the existing context check; never silently re-pinned here).
        goal.play_blocker = "objective_location_changed"
        cleared.append("gather_objective_blocked")
    elif getattr(goal, "gather_pin", None) is not None:
        goal.gather_pin = None
        cleared.append("gather_pin")
    return cleared


def _fact_tile(fact: Mapping, key: str = "tile") -> tuple[int, int] | None:
    tile = fact.get(key)
    if not isinstance(tile, Mapping) or any(type(tile.get(k)) is not int for k in ("x", "y")):
        return None
    return tile["x"], tile["y"]


def _revision(fact: Mapping) -> str:
    return hashlib.sha256(json.dumps(dict(fact), sort_keys=True, default=str).encode()).hexdigest()[:16]


def warp_reach_tile(source: tuple[int, int], width: int, height: int,
                    warps: frozenset | set = frozenset(), grid: Mapping | None = None) -> tuple[int, int] | None:
    """The in-bounds tile TWO tiles short of the warp on its approach side (never a warp tile).

    Live B2W6a (D6): an approach step that ends on the tile next to an edge warp already crossed it
    (a 10-tick step overshoots), and an in-map warp column (BusStop x=9, y 22-25) made the old reach
    tile (9,24) itself a warp. The approach now stops two tiles short; move_through (one or two
    tiles straight ahead) takes the last step and observes the transition.

    fix54A (a39 D18:J1425-J1620): the nearest-edge guess for a single in-map warp picked the museum door's
    east side (ArchaeologyHouse 3,15 -> stand 5,15) although the native nav grid showed 5,15 and 4,15 as
    walls and 3,14 / 3,13 open; the survey walked 6,15 <-> 35,12 and failed exit_not_offered. With the
    current ``grid``, a guessed side whose stand or crossing cell is OBSERVED closed yields to another
    cardinal side whose stand and crossing cells are all observed open. Unobserved cells never decide;
    no grid (or no observed-open side) keeps the old stand.
    """
    x, y = source
    edge = x < 0 or (width and x >= width) or y < 0 or (height and y >= height)
    if x < 0:
        d = (-1, 0)
    elif width and x >= width:
        d = (1, 0)
    elif y < 0:
        d = (0, -1)
    elif height and y >= height:
        d = (0, 1)
    else:
        vertical = (x, y - 1) in warps or (x, y + 1) in warps
        horizontal = (x - 1, y) in warps or (x + 1, y) in warps
        if vertical and not horizontal:
            d = (-1, 0) if not width or x < width / 2 else (1, 0)
        elif horizontal and not vertical:
            d = (0, -1) if not height or y < height / 2 else (0, 1)
        else:
            # A single interior warp: entered toward its nearest map edge (cabin / farmhouse exits).
            edges = [((0, 1), (height - 1 - y) if height else 10**6), ((0, -1), y),
                     ((1, 0), (width - 1 - x) if width else 10**6), ((-1, 0), x)]
            d = min(edges, key=lambda e: e[1])[0]

    def ok(tile):
        tx, ty = tile
        return (tx >= 0 and ty >= 0 and (not width or tx < width) and (not height or ty < height)
                and tile not in warps)

    def stand(d):
        for k in (2, 1):
            reach = (x - k * d[0], y - k * d[1])
            between = [(x - j * d[0], y - j * d[1]) for j in range(1, k)]
            if ok(reach) and all(t not in warps for t in between):
                return reach, [reach, *between]
        return None, []

    guess, cells = stand(d)
    if edge or not grid or (guess is not None and not any(
            c in grid and grid[c] not in WARP_CROSSING_OPEN for c in cells)):
        return guess
    for side in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        if side == d:
            continue
        reach, cells = stand(side)
        if reach is not None and all(grid.get(c) is not None and grid[c] in WARP_CROSSING_OPEN for c in cells):
            return reach
    return guess


# PART_B4: nav_grid cells a walk can enter ('.' open, 'x' an in-bounds warp source). Every other observed
# class ('#', 'w', 's', 'b', 'z') is not walkable ground for a warp crossing.
WARP_CROSSING_OPEN = frozenset(".x")


def warp_crossing_closed(grid: Mapping | None, actor: tuple[int, int], source: tuple[int, int]) -> bool:
    """PART_B4 (journal 2535-2552): True only when the CURRENT native nav grid shows a not-walkable cell on the
    straight walk from ``actor`` (exclusive) onto the warp ``source`` (inclusive). Beach 40,1 -> warp 40,-1 crosses
    40,0, which is '#' in every observed Beach window (38,0 and 39,0 are '.'); the move_through there stopped at
    pixel y 66.8 and then read NOT_COMPLETED:NO_EFFECT. Unobserved cells (off-grid, out of bounds) are never
    closed: no grid means the old behaviour."""
    if not grid:
        return False
    (ax, ay), (sx, sy) = actor, source
    if ax != sx and ay != sy:
        return False
    dx, dy = (sx > ax) - (sx < ax), (sy > ay) - (sy < ay)
    x, y = ax, ay
    for _ in range(abs(sx - ax) + abs(sy - ay)):
        x, y = x + dx, y + dy
        cell = grid.get((x, y))
        if cell is not None and cell not in WARP_CROSSING_OPEN:
            return True
    return False


def _location_size(facts: Mapping, current_location: str | None) -> tuple[int, int]:
    location = next((f for f in facts.values() if isinstance(f, Mapping) and f.get("kind") == "location"
                     and f.get("location_id") == current_location), None)
    width = location.get("map_width") if isinstance(location, Mapping) and type(location.get("map_width")) is int else 0
    height = location.get("map_height") if isinstance(location, Mapping) and type(location.get("map_height")) is int else 0
    return width, height


def _walk_warp_sources(facts: Mapping) -> set[tuple[int, int]]:
    return {t for f in facts.values() if isinstance(f, Mapping) and f.get("kind") == "warp" and f.get("npc_only") is not True
            for t in [_fact_tile(f, "source_tile")] if t is not None}


def open_warp_alternatives(facts: Mapping | None, current_location: str | None, target_location: str | None,
                           grid: Mapping | None, excluded: set | frozenset = frozenset()) -> list[str]:
    """PART_B4: other native walk-onto warps to the same destination whose crossing from their own reach tile is
    not observed closed and that are not ``excluded`` (an observed no-effect crossing). Only native warp facts."""
    if not isinstance(facts, Mapping) or not target_location:
        return []
    width, height = _location_size(facts, current_location)
    warps = _walk_warp_sources(facts)
    out = []
    for ref, fact in facts.items():
        if (not isinstance(fact, Mapping) or not isinstance(ref, str) or ref in excluded or fact.get("kind") != "warp"
                or fact.get("npc_only") is True or fact.get("target_location") != target_location):
            continue
        source = _fact_tile(fact, "source_tile")
        reach = None if source is None else warp_reach_tile(source, width, height, warps, grid)
        if reach is not None and not warp_crossing_closed(grid, reach, source):
            out.append(ref)
    return sorted(out)


def navigation_entries(facts: Mapping, motor_tile: tuple[int, int], objective: InteractObjective | None,
                       current_location: str | None, *, grid: Mapping | None = None,
                       excluded: set | frozenset = frozenset()) -> list[dict]:
    """Approach catalog entries for exits under a navigation objective (nearest per destination).

    PART_B4: a warp whose straight crossing the current ``grid`` shows closed, or that is ``excluded`` (an observed
    no-effect crossing this day), ranks after every other warp to the same destination. It is never dropped: with no
    alternative the old nearest-warp offer stands."""
    if objective is None or not objective.navigation or objective.blocker:
        return []
    width, height = _location_size(facts, current_location)
    best: dict[str, tuple[tuple[int, int], dict]] = {}
    warps = _walk_warp_sources(facts)
    for ref, fact in facts.items():
        if not isinstance(fact, Mapping) or not isinstance(ref, str) or len(ref) > 120:
            continue
        kind = fact.get("kind")
        target = fact.get("target_location")
        if kind == "building_door" and fact.get("enterable") is True:
            tile = _fact_tile(fact)
            entry = None if tile is None else dict(ref=ref, tile=[tile[0], tile[1]], kind="building_door",
                target_location=target, reach_tiles=[[tile[0], tile[1] + 1]])
        elif kind == "door" and (fact.get("enterable") is True or "enterable" not in fact and ("action" not in fact or
                (objective.route_leg and fact.get("action") == "LockedDoorWarp" and target == "SeedShop"))):
            tile = _fact_tile(fact)
            entry = None if tile is None else dict(ref=ref, tile=[tile[0], tile[1]], kind="door",
                target_location=target, reach_tiles=[[tile[0], tile[1] + 1]])
        elif kind == "warp" and fact.get("npc_only") is not True:
            source = _fact_tile(fact, "source_tile")
            reach = None if source is None else warp_reach_tile(source, width, height, warps, grid)
            entry = None if reach is None else dict(ref=ref, tile=[reach[0], reach[1]], kind="warp",
                target_location=target, reach="DIRECT_TARGET")
        else:
            continue
        if entry is None or not exit_offered(objective, target, current_location):
            continue
        tile = entry["tile"]
        # A native route hop is split into bounded current-grid legs by goal_routes.
        # Its exit must survive from every farm pose, independent of map dimensions.
        reach = None if objective.route_leg else 128 if objective.alias == HOME else NAVIGATION_REACH
        if min(tile) < 0 or (reach is not None
                and max(abs(tile[0] - motor_tile[0]), abs(tile[1] - motor_tile[1])) > reach):
            continue
        entry["revision"] = _revision(fact)
        distance = abs(tile[0] - motor_tile[0]) + abs(tile[1] - motor_tile[1])
        # The crossing check starts AFTER the stand. A blocked stand cannot be
        # approached either: prefer another native exit to the same destination.
        # Off-window/unknown cells retain their old rank; this grants no walkability.
        stand_closed = bool(grid) and all(
            tuple(t) in grid and grid[tuple(t)] not in WARP_CROSSING_OPEN
            for t in entry.get("reach_tiles", [tile]))
        closed = stand_closed or kind == "warp" and (
            ref in excluded or warp_crossing_closed(grid, tuple(tile), source))
        rank = (int(closed), distance)
        key = str(target)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, entry)
    return [entry for _, entry in sorted(best.values(), key=lambda item: (item[0][1], item[1]["ref"]))]


def is_forage_fact(fact: object) -> bool:
    """A spawned, grabbable scene object (the family the mod's world_interact kind=forage picks up)."""
    return (isinstance(fact, Mapping) and fact.get("kind") == "object" and fact.get("spawned") is True
            and fact.get("grabbable") is True and fact.get("machine") is not True)


def forage_entries(facts: Mapping, motor_tile: tuple[int, int], objective: InteractObjective | None,
                   blocked: set | frozenset = frozenset(), keep_ref: str | None = None) -> list[dict]:
    """D15 (live B3W5a): approach entries for spawned forage under a forage objective.

    Like trees / chests: an ADJACENT_INTERACTION target whose reach tiles are the cardinal
    neighbours not known blocked (scene passable=false, warps); the controller routes to one and
    faces the forage, then the mod offers world_interact kind=forage. Up to FORAGE_REACH tiles.

    ``keep_ref``: the forage an approach is under way to stays offered beyond FORAGE_REACH while
    the fresh scene still reports it as spawned forage (FINAL B2 J L785-L787: the routed detour to
    BusStop 38,13 walked to 25,22, 13 tiles away, and the live offer read as TARGET_REMOVED). Its
    identity / revision stay the controller's pin check; nothing is remembered or inferred."""
    if objective is None or objective.kind != "forage_pickup" or objective.blocker:
        return []
    out = []
    for ref, fact in facts.items():
        if not isinstance(ref, str) or len(ref) > 120 or not is_forage_fact(fact):
            continue
        tile = _fact_tile(fact)
        if (tile is None or min(tile) < 0
                or (ref != keep_ref
                    and max(abs(tile[0] - motor_tile[0]), abs(tile[1] - motor_tile[1])) > FORAGE_REACH)):
            continue
        x, y = tile
        reach = [[a, b] for a, b in ((x, y + 1), (x - 1, y), (x, y - 1), (x + 1, y))
                 if a >= 0 and b >= 0 and (a, b) not in blocked]
        if not reach:
            continue
        entry = dict(ref=ref, tile=[x, y], kind=KIND_FORAGE, reach_tiles=reach, revision=_revision(fact))
        name = fact.get("display_name") or fact.get("name")
        if isinstance(name, str) and 0 < len(name) <= 40:
            entry["name"] = name
        out.append(entry)
    return sorted(out, key=lambda e: (abs(e["tile"][0] - motor_tile[0]) + abs(e["tile"][1] - motor_tile[1]), e["ref"]))


def blocked_tiles(facts: Mapping) -> set[tuple[int, int]]:
    """Tiles a scene fact reports not passable, plus in-bounds warp tiles (never a stand tile)."""
    out = set(warp_tiles(facts))
    for fact in facts.values():
        if isinstance(fact, Mapping) and fact.get("kind") in {"object", "terrain"} and fact.get("passable") is False:
            tile = _fact_tile(fact)
            if tile is not None:
                out.add(tile)
    return out


def rank_override(entry: Mapping, fact: object, objective: InteractObjective | None) -> int | None:
    """Objective's own targets first: exits for navigation, spawned forage for a forage objective."""
    if objective is None or objective.blocker:
        return None
    if objective.navigation and (entry.get("kind") == "home_route_stand" or entry.get("route_hop") is True):
        # GAP8F J L63/L64 (Riverland Farm 53,16): the survey held waypoint 45,16 toward the Backwoods hop of the
        # Mine itinerary; target_location (Backwoods) != objective.target (Mine) ranked it 1, tied with every
        # actionable stone/container (progress None -> rank 1), and eight nearer stones filled the eight-entry
        # catalog: the leg was never offered (3 inspects -> exit_not_offered). A route stand is only ever built
        # for this objective's current hop, so it is the objective's own target.
        return 0
    if objective.navigation and entry.get("kind") in {"building_door", "door", "warp"}:
        return 0 if entry.get("target_location") == objective.target else 1
    if (objective.kind == "forage_pickup" and isinstance(fact, Mapping) and fact.get("kind") == "object"
            and fact.get("spawned") is True):
        return 0
    return None


def move_location_change_expected(objective: InteractObjective | None, before: str | None, after: str | None) -> bool:
    """A location change observed after a plain move / approach step counts as expected only when the
    navigation objective offers that exit (D6); otherwise it is journaled unexpected."""
    return bool(after) and exit_offered(objective, after, before)


def warp_tiles(facts: Mapping) -> set[tuple[int, int]]:
    """In-bounds warp source tiles: approach routing treats them as blocked (no accidental warp)."""
    out = set()
    for fact in facts.values():
        if isinstance(fact, Mapping) and fact.get("kind") == "warp":
            tile = _fact_tile(fact, "source_tile")
            if tile is not None and tile[0] >= 0 and tile[1] >= 0:
                out.add(tile)
    return out


def inventory_count(record, qid: str) -> int | None:
    """Total stack of one qualified id over every slot and quality (forage quality is rolled)."""
    if record is None or not getattr(record, "is_complete", False) or not getattr(record, "authoritative", False):
        return None
    facts = record.facts
    meta = facts.get("meta:inventory", {})
    count = meta.get("slot_count") if isinstance(meta, Mapping) else None
    if type(count) is not int or not 1 <= count <= 48:
        return None
    total = 0
    for i in range(count):
        slot = facts.get(f"slot:{i}", {})
        if not isinstance(slot, Mapping) or slot.get("slot") != i or type(slot.get("occupied")) is not bool:
            return None
        if slot["occupied"] and slot.get("qualified_item_id") == qid:
            if type(slot.get("stack")) is not int:
                return None
            total += slot["stack"]
    return total


def forage_qid(source_fact_id: str) -> str | None:
    """obj:<loc>:<x>,<y>:<qid> -> qid (token form; vanilla ids are plain)."""
    parts = source_fact_id.split(":") if isinstance(source_fact_id, str) else []
    return ":".join(parts[3:]) if len(parts) >= 4 and parts[0] == "obj" else None


def warp_through_ticks(observed: Mapping) -> int | None:
    """Ticks for a warp move_through from its observed distance (1 or 2 tiles straight ahead)."""
    try:
        distance = max(abs(observed["tile_x"] - observed["actor_tile_x"]), abs(observed["tile_y"] - observed["actor_tile_y"]))
    except (KeyError, TypeError):
        return None
    return WARP_THROUGH_TICKS.get(distance)


def fill_move_through_args(args: Mapping, observed: Mapping | None) -> dict | None:
    """Observed move_through target + ticks. A warp is sized from its distance (never fewer than
    WARP_THROUGH_TICKS[d]); the model may only ask for more, up to MOVE_THROUGH_TICKS_MAX."""
    if observed is None:
        return None
    ticks = args.get("ticks", MOVE_THROUGH_DEFAULT_TICKS) if isinstance(args, Mapping) else MOVE_THROUGH_DEFAULT_TICKS
    if type(ticks) is not int or not 1 <= ticks <= MOVE_THROUGH_TICKS_MAX:
        ticks = MOVE_THROUGH_DEFAULT_TICKS
    if observed.get("kind") == KIND_WARP:
        ticks = max(ticks, warp_through_ticks(observed) or 0)
    if isinstance(args, Mapping) and any(k in observed and v != observed[k] for k, v in args.items()
                                         if k != "ticks" and not isinstance(observed.get(k), str)):
        return None  # a present non-string pose value that differs stays refused
    return {**observed, "ticks": ticks}


def native_held_tool(inventory_facts: Mapping | None) -> bool:
    """fix55A (ADR 77): the native bag's active slot holds a Tool or weapon.

    ``meta:inventory.active_slot`` is Farmer.CurrentToolIndex and ``slot:N`` is Items[N]. Only an
    occupied, active slot whose qualified id is a Tool ``(T)`` or weapon ``(W)`` counts; anything
    else (object, unknown, mismatched slot) is False and keeps the held-item fence."""
    if not isinstance(inventory_facts, Mapping):
        return False
    meta = inventory_facts.get("meta:inventory")
    active = meta.get("active_slot") if isinstance(meta, Mapping) else None
    if type(active) is not int or active < 0:
        return False
    slot = inventory_facts.get(f"slot:{active}")
    return (isinstance(slot, Mapping) and slot.get("slot") == active and slot.get("occupied") is True
            and slot.get("is_active") is not False
            and str(slot.get("qualified_item_id") or "").startswith(("(T)", "(W)")))


FESTIVAL_EXIT_KIND = "festival_exit"
FESTIVAL_EXIT_TICKS = 4  # the reviewed event-movement bound (EventControlMove.MaxTicks)
_FESTIVAL_EXIT_SEARCH_MAX = 2048


def festival_exit(event_movement: Mapping | None, grid: Mapping | None, here, width: int = 0,
                  height: int = 0) -> dict | None:
    """fix55A (ADR 77): the native way out of a festival as an ordinary fact.

    A festival map has no warps. Native Event.checkForCollision calls TryStartEndFestivalDialogue when
    the farmer walks out of bounds: in multiplayer that opens ReadyCheckDialog("festivalEnd") (single
    player: a leave ConfirmationDialog). Every player answers it; nobody is moved by this fact.

    Offered only under the pinned festival event movement before the main event, from the current
    native nav grid: the shortest observed-open path from ``here`` to an open map-edge cell and the
    outward direction there. Without a map-edge cell in the grid window, the open window-border cell
    nearest a map edge is the next waypoint (the window moves with ARTI). Unobserved cells never route.
    """
    if (not isinstance(event_movement, Mapping) or event_movement.get("is_festival") is not True
            or event_movement.get("main_event_running") is not False
            or event_movement.get("player_control") is not True or not grid or here is None):
        return None
    try:
        start = (int(here[0]), int(here[1]))
    except (TypeError, ValueError, IndexError):
        return None
    open_cell = lambda c: grid.get(c) == "." or c == start
    if not open_cell(start):
        return None

    def outward(cell):
        # fix63A: GameLocation.IsOutOfBounds wants her whole box past the edge (Right < 0, Bottom < 0), so her tile reads
        # -1 for the last steps. The same outward direction holds there.
        x, y = cell
        if x <= 0:
            return "left", 0
        if y <= 0:
            return "up", 0
        if width and x >= width - 1:
            return "right", 0
        if height and y >= height - 1:
            return "down", 0
        return None, None

    xs = [c[0] for c in grid]
    ys = [c[1] for c in grid]
    gx0, gx1, gy0, gy1 = min(xs), max(xs), min(ys), max(ys)

    def edge_gap(cell):
        x, y = cell
        gaps = [x, y] + ([width - 1 - x] if width else []) + ([height - 1 - y] if height else [])
        return min(gaps)

    parent = {start: None}
    order = [start]
    i = 0
    while i < len(order) and len(order) < _FESTIVAL_EXIT_SEARCH_MAX:
        x, y = order[i]
        i += 1
        for n in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
            if n not in parent and n[0] >= 0 and n[1] >= 0 and open_cell(n):
                parent[n] = (x, y)
                order.append(n)
    edges = [c for c in order if outward(c)[0] is not None]
    if edges:
        goal, final = edges[0], True  # BFS order: the nearest by path length
    else:
        border = [c for c in order if c != start and (c[0] in (gx0, gx1) or c[1] in (gy0, gy1))]
        if not border:
            return None
        goal, final = min(border, key=lambda c: (edge_gap(c), order.index(c))), False
    path = []
    at = goal
    while at is not None and at != start:
        path.append(at)
        at = parent[at]
    path.reverse()
    direction = outward(goal)[0] if final else None
    return dict(kind=FESTIVAL_EXIT_KIND, festival_id=str(event_movement.get("festival_id") or "")[:40],
                stand=[goal[0], goal[1]], at_map_edge=final, direction=direction,
                next=[path[0][0], path[0][1]] if path else None, path_tiles=len(path),
                native="walk off the map edge: the game asks every player to leave (festivalEnd ready check)")


def festival_exit_move(entry: Mapping | None, here) -> dict | None:
    """The next event-scope move_cardinal args toward the festival edge, or the outward step at it."""
    if not isinstance(entry, Mapping) or entry.get("kind") != FESTIVAL_EXIT_KIND or here is None:
        return None
    here = (int(here[0]), int(here[1]))
    nxt = entry.get("next")
    if nxt is None:
        if entry.get("at_map_edge") is True and tuple(entry.get("stand") or ()) == here and entry.get("direction"):
            return {"direction": entry["direction"], "ticks": FESTIVAL_EXIT_TICKS}
        return None
    dx, dy = int(nxt[0]) - here[0], int(nxt[1]) - here[1]
    direction = {(0, -1): "up", (1, 0): "right", (0, 1): "down", (-1, 0): "left"}.get((dx, dy))
    return None if direction is None else {"direction": direction, "ticks": FESTIVAL_EXIT_TICKS}
