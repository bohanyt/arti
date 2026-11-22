"""P13: one model choice arms a bounded sequence of existing verified leased primitives.

Native facts own identity, readiness and completion; the runtime owns ordering only.
"""

from arti_runtime import wait_ledger as W
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re
import time
from arti_stardew_pause import game_now, wait_resume

import arti_stardew_interact as interact
import arti_stardew_world_extras as extras

OPERATION = "animal_routine"
STEPS_MAX = 120  # below the trial's 128 generation steps; physical inputs keep their own ledger
SECONDS_MAX = 600
INPUTS_MAX = 160
HOUSE_INPUTS_MAX = 96
PLAIN_TOOLS = ("(T)Axe", "(T)Hoe", "(T)Pickaxe", "(T)WateringCan")


def actor_cell_open(grid, tile):
    """A19 (journal 3268-3295): chicken Rartono stood on ARTI's own Coop tile 9,4; native
    collision marks that cell '#', open_paths() refuses a non-open start, so no exit waypoint
    was published -> unreachable_house:Farm. ARTI already occupies its own cell: only that one
    collision cell is treated as open; warps, water, stones and every other cell are unchanged."""
    here = tuple(tile) if tile is not None else None
    if grid and here is not None and grid.get(here) == "#":
        grid = dict(grid)
        grid[here] = "."
    return grid


def house_waypoint(grid, start, destination, surveyed, *, window_best=False):
    """A fresh observed-open leg, including detours out of a local distance minimum.

    Remember window bounds, never old walkability. Only the current grid authorizes
    a leg. Survey unseen frontiers when no closer cell exists; a closed component
    stays unreachable. The original executor/input/deadline limits bound exploration.

    window_best (Part A a25): compare the closest cell only with the best seen in the SAME
    window. A taller earlier window (Farm 26,39 -> rows 16..55) set a best that no later
    y0=24 window could beat, so every leg surveyed frontiers away from the open corridor.
    """
    from arti_stardew_goal_routes import open_paths, open_path, WAYPOINT_PATH_MAX
    parents = open_paths(grid, start)
    if not parents:
        return None
    xs, ys = zip(*grid)
    window = (min(xs), min(ys), max(xs), max(ys))
    windows = surveyed.setdefault("windows", [])
    if window not in windows:
        if len(windows) >= STEPS_MAX:
            return None
        windows.append(window)
    distance = lambda p: abs(p[0] - destination[0]) + abs(p[1] - destination[1])
    path = open_path(parents, destination)
    if path is None:
        best = min(parents, key=lambda p: (distance(p), p))
        if window_best:
            bests = surveyed.setdefault("best_by_window", {})
            previous = bests.get(window, distance(start) + 1)
            bests[window] = min(previous, distance(best))
        else:
            previous = surveyed.get("best_distance", distance(start) + 1)
            surveyed["best_distance"] = min(previous, distance(best))
        if distance(best) < min(previous, distance(start)):
            path = open_path(parents, best)
        else:
            # An open cell next to an unobserved cell is a survey frontier, not
            # permission to enter that unknown cell (nor a warp/stone/bed cell).
            def unseen(p):
                return (p[0] >= 0 and p[1] >= 0 and not any(
                    x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for x0, y0, x1, y1 in windows))
            frontiers = [p for p in parents if any(unseen(n) for n in
                ((p[0]-1, p[1]), (p[0]+1, p[1]), (p[0], p[1]-1), (p[0], p[1]+1)))]
            if not frontiers:
                return None
            paths = [open_path(parents, p) for p in frontiers]
            path = min(paths, key=lambda p: (distance(p[-1]) + len(p)-1, distance(p[-1]), p[-1]))
    if len(path) <= 1:
        return None
    return path[min(WAYPOINT_PATH_MAX, len(path)-1)]


def routine_offer(value):
    return (isinstance(value, Mapping) and value.get("kind") == "extras_animals"
            and bool(value.get("houses")) and value.get("reason") in
            ("animal_work_remaining", "animal_product_inventory_unverified", "observed_all_animals_cared_for")
            and not value.get("executor"))


def act(operation, args=None, ref=None):
    return dict(kind="act", operation=operation, target_ref=ref, args=dict(args or {}), evidence_refs=[])


def matches_task(target, entry):
    """Native animal source ends in its exact ID; tile-only matching could pet a neighbour."""
    if not isinstance(target, Mapping) or target.get("kind") != entry["kind"]:
        return False
    if entry["kind"] in ("animal_pet", "animal_milk", "animal_shear"):
        animal_id = entry["ref"].removesuffix(":product").rsplit(":", 1)[-1]
        return str(target.get("source_fact_id", "")).rsplit(":", 1)[-1] == animal_id
    if entry["kind"] == "forage":
        return target.get("source_fact_id") == entry["ref"]
    return target.get("tile_x") == entry["x"] and target.get("tile_y") == entry["y"]


TARGET_VISITS_MAX = 2
BUDGET_REFUSALS = ("action_session_limit", "animal_input_limit")
TARGET_APPROACHES_MAX = 6


def defer_target(state, ref, location):
    """Skip this identity for this visit; one bounded later visit may retry it."""
    counts = state.setdefault("capture_deferred", {})
    counts[ref] = counts.get(ref, 0) + 1
    state.setdefault("visit_deferred", set()).add(ref)
    state.setdefault("capture_deferred_houses", set()).add(location)
    state.setdefault("capture_reapproach", set()).add(ref)
    for key in ("target", "pose", "pose_reads"):
        state.pop(key, None)


def rebaseline_moving_tasks(raw, state):
    """A changed pose invalidates local retry evidence, never identity or the product ledger."""
    poses = state.setdefault("task_poses", {})
    for entry in raw["targets"]:
        if entry["kind"] not in ("animal_pet", "animal_milk", "animal_shear"):
            continue
        ref = entry["ref"]
        pose = (entry["instance"], entry["x"], entry["y"], entry["ready"])
        previous = poses.get(ref)
        poses[ref] = pose
        if previous is None or previous == pose:
            continue
        for key in ("capture_deferred", "target_approaches", "target_reads", "attempted_stands"):
            state.get(key, {}).pop(ref, None)
        for key in ("visit_deferred", "capture_reapproach"):
            state.get(key, set()).discard(ref)
        state.get("exhausted_houses", set()).discard(raw["location"])
        # Other animals cannot restart the read-only wait for this task.
        # Keep the visit-wide read bound even when a resident wanders.
        if state.get("target") == ref:
            state.pop("pose", None)
            state.pop("pose_reads", None)
            # ARRIVED belongs to the previous pose. Re-approach this exact
            # identity from the fresh catalog before waiting for a capture.
            state.setdefault("capture_reapproach", set()).add(ref)


def plan_choice(snap, raw, state, nav):
    """Choose only from this fresh snapshot. No IO, tool use, effects or synthetic offers."""
    rebaseline_moving_tasks(raw, state)
    pin = snap.water_context or snap.movement_context
    location = pin.location
    # After the explicit refresh, defer only the vanished identity for this
    # visit. Existing retry/house bounds and native completion debts survive.
    unavailable = state.pop("unavailable_target", None)
    if unavailable:
        defer_target(state, unavailable, location)
        entry = next((e for e in raw["targets"] if e["ref"] == unavailable), None)
        if entry and entry["kind"] == "hay_place" and any(
                abs(a["tile"][0]-entry["x"]) + abs(a["tile"][1]-entry["y"]) == 1
                for a in raw.get("animals", ())):
            state.setdefault("trough_obstructions", set()).add(unavailable)
    for trough in raw.get("troughs", ()):
        if trough["filled"]:
            ref = f'xt:trough:{location}:{trough["tile"][0]},{trough["tile"][1]}'
            state.get("capture_deferred", {}).pop(ref, None)
            state.get("trough_obstructions", set()).discard(ref)
    routine = snap.routine_objective or {}
    if routine.get("status") == "achieved" and location == "Farm":
        return dict(kind="finish", scope="goal"), None
    here = next((h for h in raw["houses"] if h["location"] == location), None)
    clock = getattr(snap.motor, "time_of_day", None)
    sleeping = type(clock) is int and clock >= extras.ANIMAL_SLEEP_TIME
    # Current house before the next; Farm residents are worked before entering a house.
    order = {"animal_pet": 0, "animal_milk": 1, "animal_shear": 2, "forage": 3,
             "hay_place": 4, "hay_hopper": 5}
    tasks = [e for e in raw["targets"] if e["ready"] and e["kind"] in order
             and not (sleeping and e["kind"] == "animal_pet")
             and (e["kind"] not in ("hay_place", "hay_hopper") or here and here["empty"])]
    if raw["hay_inventory"] == 0:
        tasks = [e for e in tasks if e["kind"] != "hay_place"]
    else:
        tasks = [e for e in tasks if e["kind"] != "hay_hopper"]
    # An ARRIVED animal can wander out of the faced capture, or stand on a
    # product whose A offer takes precedence. Work other tasks before retrying
    # that exact identity; native remaining counts still require its completion.
    deferred = state.get("capture_deferred", {})
    deferred_houses = state.get("capture_deferred_houses", set())
    if state.get("visit_location") != location:
        state["visit_deferred"] = ({ref for ref, n in deferred.items() if n}
                                    if "visit_location" not in state else set())
        state["visit_location"] = location
        state["target_approaches"] = {}
        state["target_reads"] = {}
        state["blocked_task_reads"] = 0
    available = [e for e in tasks if e["ref"] not in state.get("visit_deferred", set())
                 and deferred.get(e["ref"], 0) < state.get("target_visits_max", TARGET_VISITS_MAX)]
    if tasks and not available and (here or location == "Farm") and not any(
            h["location"] != location and inside_pending(h) for h in raw["houses"]):
        # Other tasks have drained: retry once here before the final Farm return.
        available = [e for e in tasks if deferred.get(e["ref"], 0) < state.get("target_visits_max", TARGET_VISITS_MAX)]
        if available:
            state["blocked_task_reads"] = 0
        for e in available:
            state.get("visit_deferred", set()).discard(e["ref"])
            state.get("target_approaches", {}).pop(e["ref"], None)
            state.get("target_reads", {}).pop(e["ref"], None)
    defer_house = bool(tasks and not available and here)
    house_tasks = tasks
    tasks = available
    # Keep task classes/tool groups, but sweep nearby floor/trough targets in
    # walking order rather than repeatedly crossing the room in ref order.
    tile = snap.motor.tile
    tasks.sort(key=lambda e: (deferred.get(e["ref"], 0), order[e["kind"]],
        min((abs(x-tile[0])+abs(y-tile[1]) for x,y in extras.stand_tiles(e)), default=10000), e["ref"]))
    if tasks:
        # Preserve one identity while it wanders; never switch to the animal captured at the old tile.
        # Native remaining tasks can outnumber the bounded approach catalog.
        # Start with a published task rather than declaring the lexically first
        # (omitted) product unreachable. Once selected, keep its exact identity.
        offered_refs = {e["ref"] for e in snap.approach_targets}
        first = next((e for e in tasks if e["ref"] in offered_refs
                      or matches_task(snap.world_interaction_target, e)), tasks[0])
        # Finish the ready product of the animal just petted before walking
        # away. This preserves its exact native instance, including re-approaches,
        # and avoids crossing the room a second time for each resident.
        last_animal = state.get("last_animal")
        first = next((e for e in tasks if e["kind"] in ("animal_milk", "animal_shear")
                      and (location, e["instance"]) == last_animal
                      and (e["ref"] in offered_refs or matches_task(snap.world_interaction_target, e))), first)
        entry = next((e for e in tasks if e["ref"] == state.get("target")), first)
        state["target"] = entry["ref"]
        if entry["kind"].startswith("animal_"):
            state["last_animal"] = (location, entry["instance"])
        if entry.get("refusal"):
            return None, entry["refusal"]  # native capacity, including quality/count, before travel/selection
        qid = {"animal_milk": "(T)MilkPail", "animal_shear": "(T)Shears", "hay_place": "(O)178"}.get(entry["kind"])
        held = snap.held_item
        if not isinstance(held, Mapping):
            return None, "unknown_held_item"
        held_qid = held.get("qualified_item_id")
        needs_selection = held_qid != qid if qid else held_qid is not None and held_qid not in PLAIN_TOOLS
        if needs_selection:
            item = next((i for i in snap.selectable_items if i.get("qualified_item_id") == qid), None) if qid else next(
                (i for i in snap.selectable_items if i.get("qualified_item_id") in PLAIN_TOOLS), None)
            if item is None:
                return None, "missing_tool:" + (qid or "plain_tool")
            if not snap.select_item_available:
                return None, "selection_unavailable"
            return act("select_item", item), None
        target = snap.world_interaction_target
        if matches_task(target, entry):
            return act("world_interact", target), None
        refusal = routine.get("refusal") or {}
        if refusal.get("kind") == entry["kind"] and refusal.get("reason") in (
                "inventory_cannot_accept", "inventory_full", "stamina_low"):
            return None, refusal["reason"]
        offered = next((e for e in snap.approach_targets if e["ref"] == entry["ref"]), None)
        attempts = state.setdefault("target_approaches", {})
        # B20: a resident can occupy the last trough's only stand while an
        # approach is in flight. Its disappearance is not an exhausted trough.
        # Keep the two-read/no-progress fence, but let the animal move before
        # each fresh inspection. Never press without a new exact native offer.
        if (entry["kind"] == "hay_place" and offered is None and raw["hay_inventory"] > 0
                and any(abs(a["tile"][0] - entry["x"]) + abs(a["tile"][1] - entry["y"]) == 1
                        for a in raw.get("animals", ()))):
            reads = state.setdefault("trough_obstruction_reads", {})
            reads[entry["ref"]] = reads.get(entry["ref"], 0) + 1
            if reads[entry["ref"]] <= 2 and state.get("blocked_task_reads", 0) < 2:
                state["blocked_task_reads"] = state.get("blocked_task_reads", 0) + 1
                return dict(kind="inspect", source="state", question="Refresh animal-blocked trough", scope=None), None
            state.setdefault("trough_obstructions", set()).add(entry["ref"])
            defer_target(state, entry["ref"], location)
            return plan_choice(snap, raw, state, nav)
        if offered is None or attempts.get(entry["ref"], 0) >= TARGET_APPROACHES_MAX:
            reads = state.setdefault("target_reads", {})
            reads[entry["ref"]] = reads.get(entry["ref"], 0) + 1
            # Two refreshes across the visit, not two per omitted target. More
            # consecutive identical reads trip the ordinary no-progress fence
            # before we can defer this house and work the other one.
            if reads[entry["ref"]] <= 2 and state.get("blocked_task_reads", 0) < 2:
                state["blocked_task_reads"] = state.get("blocked_task_reads", 0) + 1
                return dict(kind="inspect", source="state", question="Refresh blocked animal task", scope=None), None
            defer_target(state, entry["ref"], location)
            return plan_choice(snap, raw, state, nav)
        attempts[entry["ref"]] = attempts.get(entry["ref"], 0) + 1
        stands = offered.get("reach_tiles") or extras.stand_tiles(entry) or []
        if stands:
            state["target_stand"] = min(stands, key=lambda p: abs(p[0]-tile[0])+abs(p[1]-tile[1]))

        reapproach = state.get("capture_reapproach", set())
        if entry["ref"] in reapproach:
            reapproach.remove(entry["ref"])
            state.pop("pose", None)
            state.pop("pose_reads", None)
            return act("approach_target", ref=entry["ref"]), None
        progress = snap.working_memory.approach_progress or {}
        if progress.get("target_ref") == entry["ref"] and progress.get("status") == "ARRIVED":
            # Fresh publication after facing may take a frame. Two read-only refreshes, no blind A.
            # MotorView intentionally has no facing field. ARRIVED already
            # verifies the faced approach; these are bounded read-only waits.
            pose = (entry["ref"], entry["x"], entry["y"], tuple(snap.motor.tile))
            state["pose_reads"] = state.get("pose_reads", 0) + 1 if pose == state.get("pose") else 1
            state["pose"] = pose
            if state["pose_reads"] > 2 or state.get("blocked_task_reads", 0) >= 2:
                defer_target(state, entry["ref"], location)
                return plan_choice(snap, raw, state, nav)
            state["blocked_task_reads"] = state.get("blocked_task_reads", 0) + 1
            return dict(kind="inspect", source="state", question="Refresh faced animal task", scope=None), None
        return act("approach_target", ref=entry["ref"]), None
    state.pop("target", None)
    if (here and inside_pending(here) - (here["inside_unpetted"] if sleeping else 0) > 0
            and not defer_house):
        return None, "hay_unavailable" if here["empty"] and raw["hay_inventory"] == 0 and here["hay_stock"] == 0 else "unknown_house_tasks"
    if defer_house:
        # First finish the other house, then revisit this one once. Report
        # remaining native work only after returning to Farm.
        refs = [e["ref"] for e in house_tasks]
        if refs and all(deferred.get(ref, 0) >= state.get("target_visits_max", TARGET_VISITS_MAX) for ref in refs):
            state.setdefault("exhausted_houses", set()).add(location)
    entries = [e for e in snap.approach_targets if e.get("kind") in ("warp", "door", "building_door", "animal_route_stand")]
    destination = state.get("destination")
    if location == destination:
        state.pop("destination", None)
        state["nav"] = {}
        destination = None
    if destination is None:
        unfinished = sorted((h for h in raw["houses"] if
                             inside_pending(h) - (h["inside_unpetted"] if sleeping else 0) > 0
                             and h["location"] not in state.get("exhausted_houses", set())), key=lambda h: (
            h["location"] in deferred_houses, "Coop" not in h["type"], h["location"]))
        if location == "Farm" and not unfinished and any(inside_pending(h) for h in raw["houses"]):
            if sleeping and any(h["inside_unpetted"] for h in raw["houses"]):
                return None, "time_rule:animal_sleeping"
            remaining = sorted(ref for ref, count in deferred.items() if count)
            state["deferred_remaining"] = remaining
            troughs = sorted(state.get("trough_obstructions", set()) & set(remaining))
            return None, ("trough_access_blocked_by_animal:" + ",".join(troughs)
                          if troughs else "blocked_targets_remaining")
        if location == "Farm" and not unfinished and house_tasks:
            return None, "blocked_targets_remaining"
        destination = unfinished[0]["location"] if location == "Farm" and unfinished else "Farm"
        state["destination"] = destination
    entries = [e for e in entries if e.get("target_location") == destination]
    for target, operation in ((snap.world_interaction_target, "world_interact"), (snap.move_through_target, "move_through")):
        if (target and target.get("kind") in ("door", "warp", "animal_house_route")
                and target.get("target_location_id") == destination
                and not (destination == "Farm" and target.get("kind") == "animal_house_route")):
            if target.get("kind") == "animal_house_route":
                # Human-door A needs a plain hand. Native exit move_through
                # uses only walking and accepts the current held item.
                held = snap.held_item
                if not held or held.get("qualified_item_id") not in PLAIN_TOOLS:
                    item = next((i for i in snap.selectable_items if i.get("qualified_item_id") in PLAIN_TOOLS), None)
                    if not item or not snap.select_item_available:
                        return None, "missing_tool:plain_tool"
                    return act("select_item", item), None
            return act(operation), None  # existing original-offer filler includes warp distance
    if entries:
        progress = snap.working_memory.approach_progress or {}
        entry = entries[0]
        held = snap.held_item
        if entry["kind"] in ("door", "building_door") and (not held or held.get("qualified_item_id") not in PLAIN_TOOLS):
            item = next((i for i in snap.selectable_items if i.get("qualified_item_id") in PLAIN_TOOLS), None)
            if item and snap.select_item_available:
                return act("select_item", item), None
            return None, "missing_tool:plain_tool"
        if progress.get("target_ref") == entry["ref"] and progress.get("status") == "ARRIVED":
            if entry["kind"] == "animal_route_stand":
                return dict(kind="inspect", source="state", question="Refresh next house waypoint", scope=None), None
            if entry["kind"] == "warp":
                # A direct stand can be reached sideways. Turn once before the
                # older crossing fallback so the native move_through offer can
                # be captured and verified under its destination/instance pins.
                source = re.search(r":(\d+),(\d+)->", entry["ref"])
                pose = (entry["ref"], tuple(snap.motor.tile))
                if source and state.get("exit_face") != pose:
                    dx,dy = int(source[1])-snap.motor.tile[0],int(source[2])-snap.motor.tile[1]
                    direction = {(0,-1):"up",(1,0):"right",(0,1):"down",(-1,0):"left"}.get(
                        ((dx>0)-(dx<0),(dy>0)-(dy<0)))
                    if direction and 1 <= abs(dx)+abs(dy) <= 2:
                        state["exit_face"] = pose
                        return act("world_key.move_cardinal", dict(direction=direction,ticks=1)), None
            from arti_stardew_skills import _exit_step
            choice = _exit_step(snap, nav, state.setdefault("nav", {}))
            if choice:
                return choice, None
            return None, "exit_not_captured"
        return act("approach_target", ref=entry["ref"]), None
    if destination == "Farm" and here:
        # A vanished local exit/waypoint is not proof of a permanent obstacle.
        # Spend the existing visit allowance, not a new executor/input/deadline.
        reads = state.setdefault("exit_refreshes", {})
        count = reads.get(location, 0)
        if count < state.get("target_visits_max", TARGET_VISITS_MAX):
            reads[location] = count + 1
            return dict(kind="inspect", source="state", scope=None,
                        question="Refresh refused house exit"), None
    return None, "unreachable_house:" + destination


TARGET_CHANGED_REFRESHES = 2


def unpublished_replan(evidence):
    """These approach refusals occur before publication; no world effect is owed.

    A23 (journal 3636-3640, SMAPI 21:43:22-27): cow Ci stepped 13,13 -> 14,13 between the Milk Pail
    selection and the milk press. The runtime's own pre-lease precheck refused the press
    (world_interaction_target_changed, request_id None, before None, cleanup confirmed); the mod armed no
    HOLD and input reservations stayed 516. Like the approach refusals, nothing was published."""
    return (evidence.lifecycle == "not_taken" and evidence.request_id is None
            and evidence.outcome is None and evidence.cleanup_confirmed and evidence.reason in {
                "approach_replan_required", "approach_publication_context_changed", "approach_arrived_and_facing",
                "approach_partial", "approach_target_unavailable", "world_interaction_target_changed",
                "approach_reach_changed"})


def native_binding_refusal(evidence):
    """PART_A34 (a34 journal L1140-1145): the mod refused the published milk press at ARM
    (CompleteRejected BindingMismatch) because Ci moved 11,5 -> 11,4. The runtime reconciles
    only the exact pre-arm receipt with a confirmed lease release (no press, no effect)."""
    return (evidence.lifecycle == "not_taken" and evidence.reason == "native_binding_refusal_reconciled"
            and bool(evidence.request_id) and evidence.outcome is None and evidence.cleanup_confirmed is True
            and evidence.world_effect_status == "zero_effect" and not getattr(evidence, "observed_changes", ()))


def released_move_no_effect(evidence):
    """RECONCILE_GENERAL (proof A6 journal L324-335, Coop): an approach step toward the moving chicken
    pressed left into the west wall until the native deadline released it (RELEASE_VERIFIED,
    displacement_px=0). The runtime reconciles that step from the fresh post-release pose; the routine
    then re-reads and re-approaches within the A23 refresh/defer bound, from a different stand."""
    return (evidence.lifecycle == "completed" and evidence.reason == "released_early_no_effect_reconciled"
            and bool(evidence.request_id) and evidence.outcome is None and evidence.cleanup_confirmed is True
            and evidence.world_effect_status in ("unchanged", "observed_change")
            and set(getattr(evidence, "observed_changes", ()) or ()) <= {"pixel", "facing"})


def animal_input_envelope(owner_text):
    """Reserve the existing 160-input ceiling for a multi-building/moving-target goal.

    This is a bounded allowance, not a guarantee of travel cost. The runner refuses a
    smaller default unless the operator explicitly selects the envelope; no approval is edited.
    """
    if not isinstance(owner_text, str):
        return None
    text = re.sub(r"^(?:arti[\s,:]+)?(?:main mandiri|main sendiri|tujuan baru)\s*[:,-]\s*", "", owner_text.strip(), flags=re.I)
    objective = extras.parse_extras_objective(text)
    if objective is not None and objective.kind == "animals":
        return 160
    # S3 is a registered Skills v1 goal. Its Indonesian plain text is owned by
    # the skill parser, while the animal executor's older grammar is English-only.
    from arti_stardew_skills import ANIMAL_GOAL
    if text.rstrip(".").casefold() == ANIMAL_GOAL.rstrip(".").casefold():
        return 160
    return None


def projection(raw, location, date):
    if (not isinstance(raw, Mapping) or raw.get("complete") is not True
            or raw.get("location") != location or raw.get("date") != list(date or ())
            or not isinstance(raw.get("location_name"), str) or not raw["location_name"]):
        return None
    houses = raw.get("houses")
    if not isinstance(houses, (list, tuple)) or not 1 <= len(houses) <= 16:
        return None
    seen = set()
    for h in houses:
        if (not isinstance(h, Mapping) or h.get("known") is not True
                or not isinstance(h.get("location"), str) or not h["location"] or h["location"] in seen
                or not isinstance(h.get("instance"), str) or not h["instance"]
                or h.get("type") not in ("Coop", "Big Coop", "Deluxe Coop", "Barn", "Big Barn", "Deluxe Barn")
                or not _tile(h.get("door")) or type(h.get("enterable")) is not bool
                or not isinstance(h.get("door_ref"), str) or not 0 < len(h["door_ref"]) <= 120
                or any(type(h.get(k)) is not int or h[k] < 0 for k in
                       ("unpetted", "ready", "inside_unpetted", "inside_ready", "floor", "empty", "filled", "blocked", "missing", "hay_stock"))
                or h["missing"] != 0 or h["floor"] > 64 or h["empty"] + h["filled"] + h["blocked"] > 64):
            return None
        roster = h.get("roster")
        if (not isinstance(roster, (list, tuple)) or len(roster) > 64
                or any(not isinstance(r, (list, tuple)) or len(r) != 2
                       or any(not isinstance(v, str) or not v for v in r) for r in roster)
                or len({r[0] for r in roster}) != len(roster)
                or max(h["unpetted"], h["ready"]) > len(roster)
                or h["inside_unpetted"] > h["unpetted"] or h["inside_ready"] > h["ready"]):
            return None
        seen.add(h["location"])
        products = h.get("products")
        if (not isinstance(products, (list, tuple)) or len(products) > 128
                or any(not isinstance(p, Mapping) or not isinstance(p.get("qid"), str) or not p["qid"].startswith("(O)")
                       or type(p.get("count")) is not int or not 1 <= p["count"] <= 256 for p in products)
                or len({p["qid"] for p in products}) != len(products)):
            return None
    if not any("Coop" in h["type"] for h in houses) or not any("Barn" in h["type"] for h in houses):
        return None
    for key, cap in (("animals", 64), ("troughs", 64), ("targets", 256)):
        if not isinstance(raw.get(key), (list, tuple)) or len(raw[key]) > cap or any(not isinstance(e, Mapping) for e in raw[key]):
            return None
    for a in raw["animals"]:
        if (any(not isinstance(a.get(k), str) for k in ("id", "name", "instance", "tool", "product"))
                or not a["id"] or not a["instance"] or type(a.get("wasPet")) is not bool or type(a.get("ready")) is not bool
                or not _tile(a.get("tile"))):
            return None
    for t in raw["troughs"]:
        if not _tile(t.get("tile")) or any(type(t.get(k)) is not bool for k in ("filled", "occupied")):
            return None
    for e in raw["targets"]:
        if (not isinstance(e.get("ref"), str) or not 0 < len(e["ref"]) <= 120
                or not isinstance(e.get("instance"), str) or type(e.get("ready")) is not bool
                or not _tile([e.get("x"), e.get("y")]) or extras.stand_tiles(e) is None
                or e.get("refusal") not in (None, "inventory_cannot_accept")):
            return None
    if type(raw.get("hay_inventory")) is not int or raw["hay_inventory"] < 0:
        return None
    # An old DLL may omit routes. A malformed/new-route dump is not permission
    # to travel or to spend an unbounded reservation.
    from arti_stardew_command import validate_world_interaction_args, KesalahanPerintah
    routes = raw.get("routes", ())
    if not isinstance(routes, (list, tuple)) or len(routes) > 16:
        return None
    destinations = set()
    for route in routes:
        if (not isinstance(route, Mapping) or set(route) != {"target", "input_limit"}
                or type(route["input_limit"]) is not int or not 1 <= route["input_limit"] <= HOUSE_INPUTS_MAX
                or not isinstance(route["target"], Mapping)):
            return None
        target = route["target"]
        try:
            validate_world_interaction_args(dict(target))
        except (ValueError, TypeError, KesalahanPerintah):
            return None
        destination = target.get("target_location_id")
        if target.get("kind") != "animal_house_route" or destination in destinations:
            return None
        destinations.add(destination)
        if location == "Farm":
            house = next((h for h in houses if h["location"] == destination), None)
            if (house is None or not house["enterable"] or target["source_fact_id"] != house["door_ref"]
                    or [target["tile_x"], target["tile_y"]] != list(house["door"])):
                return None
        elif destination != "Farm" or not any(h["location"] == location for h in houses):
            return None
    return deepcopy(dict(raw))


def _tile(value):
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(type(v) is int and 0 <= v <= 10000 for v in value)


def basis(raw):
    return tuple(sorted((h["location"], h["instance"], tuple(sorted(tuple(r) for r in h["roster"])))
                        for h in raw["houses"]))


def pending(h):
    return h["unpetted"] + h["ready"] + h["floor"] + h["blocked"] + h["empty"]


def inside_pending(h):
    return h["inside_unpetted"] + h["inside_ready"] + h["floor"] + h["blocked"] + h["empty"]


def verdict(summary, blocker=None):
    if blocker:
        return "partial", blocker
    if not summary or not summary.get("houses"):
        return "uncertain", "animal_state_unavailable"
    if (any(h["empty"] for h in summary["houses"]) and summary.get("hay_inventory") == 0
            and all(h["hay_stock"] == 0 for h in summary["houses"])):
        return "partial", "hay_unavailable_for_empty_troughs"
    if any(pending(h) for h in summary["houses"]):
        return "partial", "animal_work_remaining"
    if summary.get("inventory_status") != "verified":
        return "uncertain", summary.get("inventory_status") or "animal_product_inventory_unverified"
    return "achieved", "observed_all_animals_cared_for"


def summary(raw):
    # Counts for all houses, detailed state for the current house. Tokens/rosters stay in the runtime fence.
    return {"hay_inventory": raw["hay_inventory"], "houses": [{k: h[k] for k in ("location", "type", "unpetted", "ready", "inside_unpetted", "inside_ready", "floor", "empty", "filled", "blocked", "hay_stock")}
                       for h in raw["houses"]],
            "animals": [{k: a[k] for k in ("id", "instance", "name", "tile", "wasPet", "ready", "tool", "product")}
                        for a in raw.get("animals", ())],
            "troughs": deepcopy(raw.get("troughs", [])),
            "next": "Choose remaining tasks and tools; approach a published target then world_interact. Pet with a plain tool or empty hand; milk/shear with the required tool; floor pickup and hopper with plain hand; select Hay then A hay_place for each empty trough. Visit both houses. Finish only when all native remaining counts are zero."}


def moving_summary_identity(value):
    if not isinstance(value, Mapping) or value.get("kind") != "extras_animals":
        return value
    result = deepcopy(dict(value))
    for a in result.get("animals", ()):
        a.pop("tile", None)
    executor = result.get("executor")
    if (isinstance(executor, dict) and executor.get("status") == "running"
            and type(executor.get("steps")) is int and 0 <= executor["steps"] <= STEPS_MAX):
        # FIXB67: generating a primitive spends a step AFTER its snapshot was
        # built. That runtime-owned counter is telemetry, not a changed native
        # prerequisite. Keep status/actions/cleanup/exceptions and all native
        # facts pinned; the executor still enforces its original step budget.
        executor.pop("steps")
    return result


def wire_summary(value, location):
    """Compact current counts before sacrificing executable offers; canonical evidence stays intact."""
    result = deepcopy(dict(value))
    for key in ("houses", "animals", "troughs", "inventory_check"):
        rows = result.get(key)
        if rows:
            fields = list(rows[0])
            result[key + "_fields"] = fields
            # Every completion ledger row is protected, even beyond the preview cap.
            shown = rows if key in ("houses", "inventory_check") else rows[:8]
            result[key] = [[r.get(k) for k in fields] for r in shown]
            if len(shown) < len(rows):
                result[key + "_omitted"] = len(rows) - len(shown)
    result["next"] = "Choose animal_routine once for both houses; runtime handles approach_target/tools/actions."
    if value.get("executor"):
        result["next"] = "Executor owns steps; resolve its exception before a new routine."
    if value.get("status") == "achieved":
        result["next"] = "All native counts and product inventory gains verified; choose finish."
    return result


def wire_selections(value, items):
    """One offered plain tool plus the current house's needed tools/hay; no invented slots."""
    plain = next((t for t in items if t.get("qualified_item_id") in
                  ("(T)Axe", "(T)Hoe", "(T)Pickaxe", "(T)WateringCan")), None)
    tools = {"(T)" + a["tool"].replace(" ", "") for a in value.get("animals", ()) if a.get("ready")}
    if value.get("hay_inventory", 0) > 0 and any(not t["filled"] and not t["occupied"] for t in value.get("troughs", ())):
        tools.add("(O)178")
    result = ([plain] if plain else []) + [t for t in items if t.get("qualified_item_id") in tools]
    return deepcopy(result)


class AnimalRoutineRuntimeMixin:
    def _animal_skill_run(self):
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        if (self._skills_enabled and session is not None and session.status == "active"
                and run is not None and run.call.name == "rawat_hewan"
                and run.goal_id == getattr(self._active, "goal_id", None)):
            return run
        return None

    def _animal_inputs_spent(self):
        run = self._animal_skill_run()
        executor = getattr(self._active, "animal_executor", None) or {}
        start = run.inputs_start if run is not None else executor.get("inputs_start", 0)
        return self._physical_action_publications - start

    def _animal_input_limit(self):
        ceiling = INPUTS_MAX
        approval = self._animal_trial_approval
        if approval is not None and self._trial_admission is not None:
            approved = approval(self._active)
            if type(approved) is int and 0 < approved <= 280:
                ceiling = approved
        limit = self._action_session_limit
        if limit is None:
            return ceiling
        # A24 (journal 6291-6305): the session cap is absolute; the animal ledger counts from this
        # run's start. Compare like with like so the executor stops BEFORE the session cap refuses.
        run = self._animal_skill_run()
        executor = getattr(self._active, "animal_executor", None) or {}
        start = run.inputs_start if run is not None else executor.get("inputs_start", 0)
        # A25: a Part A row never spends into the day's sleep reserve (run-relative ceiling).
        reserve = (getattr(run, "state", None) or {}).get("campaign_input_ceiling")
        return max(0, min(ceiling, limit - start, reserve if type(reserve) is int else limit))

    def _animal_start_reason(self, request):
        goal = self._active
        pin = request.snapshot.water_context or request.snapshot.movement_context
        if (not self._animal_open(goal) or not routine_offer(request.snapshot.routine_objective)
                or not self._v2_pad_path(pin) or getattr(goal, "animal_executor", None)):
            return "animal_routine_not_offered"
        if (request.snapshot.water_context is None or request.snapshot.motor.is_player_free is not True
                or request.snapshot.motor.can_player_move is not True):
            return "animal_controls_unavailable"
        raw = self._animal_raw(pin)
        if raw is None or basis(raw) != getattr(goal, "animal_basis", None):
            return "animal_state_changed"
        run = self._animal_skill_run()
        resume = run.state.get("campaign_animal_resume") if run is not None else None
        if resume and basis(raw) != resume["basis"]:
            return "animal_instance_or_roster_changed"
        if pin.location not in ("Farm", *(h["location"] for h in raw["houses"])) and not extras.farm_interior(pin.location):
            return "animal_start_location_unreviewed"
        if goal.extras_objective.blocker or goal.extras_objective.stop:
            return "animal_routine_blocked"
        return None

    def _animal_start(self, decision, request):
        """No physical input at start. Re-read through the ordinary continuation pipeline."""
        from dataclasses import replace
        from arti_stardew_decision import InspectChoice
        goal = self._active
        seconds = SECONDS_MAX
        # The runner installs this deadline only after validating the original
        # approval. Generic runtime configuration cannot extend the animal budget.
        if (self._animal_trial_approval is not None and self._trial_admission is not None
                and self._animal_trial_approval(goal) is not None
                and self._trial_deadline_unix_ms is not None):
            seconds = min(900, max(0, (self._trial_deadline_unix_ms - time.time()*1000)/1000))
        run = self._animal_skill_run()
        if run is not None:
            seconds = min(seconds, max(0, 900 - (game_now(self) - run.started_at)))
        goal.animal_executor = dict(status="running", deadline=game_now(self) + seconds,
                                    steps=0, cleanup=True, actions=0, seen=set(), staged=set(),
                                    inputs_start=run.inputs_start if run is not None else 0,
                                    inputs_max=self._animal_input_limit())
        resume = run.state.get("campaign_animal_resume") if run is not None else None
        if resume:
            goal.animal_executor.update(deadline=min(goal.animal_executor["deadline"], resume["deadline"]),
                                        steps=resume["steps"], target_visits_max=1,
                                        inputs_max=min(goal.animal_executor["inputs_max"], resume["caps"][1]),
                                        revisit_refs=set(resume["refs"]),
                                        trough_obstructions={ref for ref in resume["refs"] if ref.startswith("xt:trough:")})
        self._journal({"ev": "animal_executor", "goal_id": goal.goal_id, "status": "started",
                       "inputs_max": goal.animal_executor["inputs_max"], "seconds_max": seconds, "steps_max": STEPS_MAX})
        return self._complete_non_action(replace(decision, choice=InspectChoice(
            "state", "Refresh native animal routine before execution", None)), request)

    def _animal_record_action(self, goal, evidence):
        """Worst-case aggregation is bound to the original goal, including superseded tails."""
        state = getattr(goal, "animal_executor", None)
        if not state or evidence is None:
            return
        no_effect_release = released_move_no_effect(evidence)
        if ((evidence.lifecycle == "completed" and evidence.cleanup_confirmed
                and evidence.outcome == "NOT_COMPLETED:NO_EFFECT" and state.get("target")
                and state.get("target_stand") is not None
                and not evidence.observed_changes)
                or (no_effect_release and state.get("target") and state.get("target_stand") is not None)):
            # A no-motion press toward this stand: offer the other published stands next.
            ref = state["target"]
            state.setdefault("attempted_stands", {}).setdefault(ref, set()).add(tuple(state["target_stand"]))
        key = evidence.request_id
        if key and key not in state["seen"]:
            state["seen"].add(key)
            state["actions"] += 1
        state["cleanup"] = state["cleanup"] and evidence.cleanup_confirmed
        if (evidence.lifecycle == "not_taken" and evidence.request_id is None
                and evidence.reason in BUDGET_REFUSALS):
            # A24 (journal 6297-6305): input 900/900 was reserved, the next publication was refused
            # at the write boundary. That is budget exhaustion, not an unreconciled native action.
            # The cleanup flag above stays honest; the routine stops either way.
            goal.extras_objective.stop = "animal_budget_exhausted"
            self._animal_exception(goal, "animal_budget_exhausted")
            return
        # A bounded approach replan published no input. Its generic placeholder
        # effect is "uncertain", but there is no physical action to reconcile.
        replan = unpublished_replan(evidence)
        if (not evidence.cleanup_confirmed or evidence.lifecycle in ("uncertain", "interrupted")
                or evidence.world_effect_status == "uncertain" and not replan):
            state["status"] = "blocked"
            state["exception"] = "animal_action_unreconciled"
            goal.extras_objective.stop = "animal_action_unreconciled"
            self._animal_exception(goal, "animal_action_unreconciled")
        elif ((replan and evidence.reason in {"world_interaction_target_changed", "approach_reach_changed"}
               or native_binding_refusal(evidence))
              and state["status"] == "running"):
            # A23: the animal moved before publication. Re-read, then re-approach the same identity;
            # after TARGET_CHANGED_REFRESHES refusals it is deferred like a vanished target.
            # PART_A34: the same move, caught one layer later by the mod's ARM binding check (published,
            # charged above, never pressed). It shares the same per-identity refresh/defer bound.
            counts = state.setdefault("target_changed", {})
            ref = state.get("target")
            counts[ref] = counts.get(ref, 0) + 1
            if counts[ref] > TARGET_CHANGED_REFRESHES:
                state["unavailable_target"] = ref
            state["refresh_unavailable_target"] = True
        elif no_effect_release and state["status"] == "running":
            # RECONCILE_GENERAL: the A23 bound, its own counter. Re-read, re-approach the same identity
            # from another stand; after TARGET_CHANGED_REFRESHES no-effect approaches defer it for the visit.
            counts = state.setdefault("no_effect_approaches", {})
            ref = state.get("target")
            counts[ref] = counts.get(ref, 0) + 1
            if counts[ref] > TARGET_CHANGED_REFRESHES:
                state["unavailable_target"] = ref
            state["refresh_unavailable_target"] = True
        elif replan and evidence.reason == "approach_target_unavailable" and state["status"] == "running":
            state["unavailable_target"] = state.get("target")
            state["refresh_unavailable_target"] = True
        elif evidence.lifecycle == "rejected" and evidence.outcome == "InteractionRejected" and (evidence.before or {}).get("house_route_destination"):
            self._animal_exception(goal, "house_route_refused:"+evidence.before["house_route_destination"])
        elif evidence.outcome == "InteractionNoEffect" and not (
                evidence.lifecycle == "completed" and (evidence.before or {}).get("house_route_destination")
                and evidence.world_effect_status in {"observed_change", "unchanged"}):
            state["status"] = "blocked"
            state["exception"] = "animal_primitive_no_effect"
            goal.extras_objective.stop = "animal_primitive_no_effect"
            self._animal_exception(goal, "animal_primitive_no_effect")

    def _animal_progress_facts(self, goal):
        """Only native task counts and the exact product ledger count as progress."""
        if not self._animal_open(goal):
            return None
        routine = goal.extras_objective.routine or {}
        return (routine.get("hay_inventory"), tuple(
            (h.get("location"), *(h.get(k) for k in
                ("unpetted", "ready", "floor", "empty", "filled", "blocked")))
            for h in routine.get("houses", ())), tuple(
            (c.get("qid"), c.get("current")) for c in routine.get("inventory_check", ())))

    def _animal_exception(self, goal, reason):
        state = goal.animal_executor
        state["status"], state["exception"] = "blocked", reason
        goal.extras_objective.blocker = reason
        question = (
            "The inventory cannot accept the next product. May I stop here so you can free space?"
            if reason in ("inventory_cannot_accept", "inventory_full") else
            "The required tool is missing or unavailable: " + reason.partition(":")[2] + ". Can you put it in the toolbar?"
            if reason.startswith("missing_tool:") else
            "The verified animal routine stopped at " + reason + ". Can you resolve this prerequisite before a new routine?"
        )
        state["question"] = question
        if goal.extras_objective.routine is None:
            goal.extras_objective.routine = {}
        goal.extras_objective.routine["executor"] = dict(status="blocked", exception=reason, question=question)
        self._journal({"ev": "animal_executor", "goal_id": goal.goal_id, "status": "blocked",
                       "exception": reason, "question": question, "cleanup": state["cleanup"]})

    def _animal_executor_invoke(self, request):
        """Every generated primitive still goes through normal admission/publication/effect/cleanup."""
        with self._lock:
            goal = self._active
            state = getattr(goal, "animal_executor", None)
            if (not self._animal_open(goal) or request.goal_id != goal.goal_id or not state
                    or state["status"] != "running" or goal.progress_marker == "terminal"):
                return None
            # An owner turn/STOP never borrows the running routine's authority.
            if request.reason == "owner_turn":
                return None
            if self._worker_stop.is_set() or self._action_cancel.is_set():
                self._animal_exception(goal, "cancelled")
                return None
            pin = request.snapshot.water_context or request.snapshot.movement_context
            raw = self._animal_raw(pin)
            reason = goal.extras_objective.stop or goal.extras_objective.blocker
            if reason is None and raw is not None and basis(raw) != goal.animal_basis:
                reason = "animal_instance_or_roster_changed"
            if reason is None and (game_now(self) >= state["deadline"] or state["steps"] >= STEPS_MAX):
                reason = "animal_budget_exhausted"
            if reason is None and not state["cleanup"]:
                reason = "animal_action_unreconciled"
            if reason:
                self._animal_exception(goal, reason)
                return None
            if raw is None:
                # A read can expire/change between snapshot construction and invocation.
                # No mutation or identity/ledger rebind until a fresh bound capture exists.
                state["state_refreshes"] = state.get("state_refreshes", 0) + 1
                if state["state_refreshes"] > 3:
                    self._animal_exception(goal, "animal_state_unavailable")
                    return None
                return self._rule_json(request, dict(kind="inspect", source="state", scope=None,
                    question="Refresh changed animal capture"), "animal_executor", "state_refresh")
            state["state_refreshes"] = 0
            if state.pop("refresh_unavailable_target", False):
                choice, reason = dict(kind="inspect", source="state", scope=None,
                                      question="Refresh unavailable animal target before reselection"), None
            else:
                choice, reason = plan_choice(request.snapshot, raw, state, goal.interact_objective)
            # A fresh identity-bound finish is read-only; the ordinary finish
            # verifier still checks native state, inventory, Farm return and cleanup.
            # At the cap no new physical action (including tool staging) may start.
            if (self._animal_inputs_spent() >= min(state.get("inputs_max", INPUTS_MAX), self._animal_input_limit())
                    and (choice is None or choice.get("kind") != "finish")):
                self._animal_exception(goal, "animal_budget_exhausted")
                return None
            if reason and reason.startswith("missing_tool:"):
                qid = reason.partition(":")[2]
                # Reuse the reviewed inventory-menu worker, once per actual item. Fresh inventory only.
                # A24 (journal 4440-4543, 5074): Day 2 staged Hay in the Coop, used it all, then the Barn's
                # hopper Hay landed in the backpack again (products had refilled the freed toolbar slot) and
                # the once-per-item rule raised missing_tool:(O)178. Once per item PER HOUSE visit location.
                key = (qid, pin.location)
                if qid.startswith("(") and key not in state["staged"] and (self._animal_inventory_count(pin, qid) or 0) > 0:
                    state["staged"].add(key)
                    goal.item_stage_want = qid
                    if (goal.item_stage or {}).get("stop"):
                        goal.item_stage = {}
                    self._stage_end_resume("item_to_toolbar", goal, force=True)
                    choice, reason = dict(kind="wait", reason="item_to_toolbar", ms=0), None
            if reason:
                self._animal_exception(goal, reason)
                return None  # only exceptions hand control back to the actual provider
            state["steps"] += 1
            self._journal({"ev": "animal_executor", "goal_id": goal.goal_id, "status": "step",
                           "n": state["steps"], "operation": choice.get("operation", choice["kind"]),
                           "target_ref": choice.get("target_ref"), "cleanup": state["cleanup"]})
        if choice.get("question") == "Refresh refused house exit":
            # Rebuild only local route hints from the next fresh capture.
            # Native exits, blocked-motion evidence and every budget survive.
            getattr(goal, "animal_waypoints", {}).pop("Farm", None)
            getattr(goal, "animal_surveys", {}).pop("Farm", None)
        if choice.get("question") in ("Refresh animal-blocked trough", "Refresh refused house exit"):
            W.call('poll', 108, self._action_cancel.wait, 1.0)
        return self._rule_json(request, choice, "animal_executor", choice.get("operation", choice["kind"]))

    def _animal_open(self, goal):
        return getattr(getattr(goal, "extras_objective", None), "kind", None) == "animals"

    def _animal_exception_refresh(self, request):
        """Hand back only after publishing the exception, never the running pre-step snapshot."""
        with self._lock:
            goal = self._active
            if (not self._animal_open(goal) or request.goal_id != goal.goal_id
                    or request.reason == "owner_turn" or goal.progress_marker == "terminal"
                    or self._worker_stop.is_set() or self._action_cancel.is_set()):
                return None
            state = getattr(goal, "animal_executor", None)
            old = (request.snapshot.routine_objective or {}).get("executor") or {}
            if state and state["status"] == "blocked" and old.get("status") == "running":
                return self._rule_json(request, dict(kind="inspect", source="state", scope=None,
                    question="Refresh the animal executor exception before asking the owner"),
                    "animal_executor", "exception_refresh")
        return None

    def _animal_raw(self, pin):
        facts = self._fishing_ui_facts(pin)
        raw = facts.get("animal_routine") if isinstance(facts, Mapping) else None
        motor = self._state.latest(pin.player_id) if pin else None
        if (not isinstance(facts, Mapping) or not isinstance(raw, Mapping)
                or motor is None or self._state.is_stale(pin.player_id)
                or raw.get("location_name") != motor.location
                or facts.get("runtime_instance_id") != pin.runtime_instance_id
                or facts.get("session_id") != pin.command_session_id
                or facts.get("player_id") != pin.player_id or facts.get("screen_id") != pin.screen_id
                or facts.get("location_id") != pin.location):
            return None
        identity = (pin.runtime_instance_id, pin.command_session_id, pin.observation_session_id, pin.player_id, pin.screen_id)
        original = getattr(self._active, "play_identity", None)
        if original is None or identity != original:
            return None  # original owner/request epoch, never rebound to replacement native sessions
        return projection(raw, pin.location, self._date_tuple(motor))

    def _animal_prepare(self, goal, pin):
        if not self._animal_open(goal):
            return
        objective = goal.extras_objective
        goal.animal_destinations = set()
        if pin is not None and pin.location != getattr(goal, "animal_route_location", None):
            goal.animal_route_location, goal.animal_waypoints = pin.location, {}
            goal.animal_surveys = {}
        raw = self._animal_raw(pin)
        objective.routine = None
        if raw is None:
            executor = getattr(goal, "animal_executor", None)
            if executor and executor.get("exception"):
                objective.routine = {"executor": {k: executor[k] for k in ("status", "exception", "question")}}
            goal.interact_objective = None
            return
        current_basis = basis(raw)
        run = self._animal_skill_run()
        resume = run.state.get("campaign_animal_resume") if run is not None else None
        if resume and current_basis != resume["basis"]:
            objective.blocker = "animal_instance_or_roster_changed"
        original = getattr(goal, "animal_basis", None)
        if original is None:
            goal.animal_basis = current_basis
        elif original != current_basis:
            objective.blocker = "animal_instance_or_roster_changed"
        required = getattr(goal, "animal_products_required", None)
        if required is None:
            required = {}
            for h in raw["houses"]:
                for p in h["products"]:
                    required[p["qid"]] = required.get(p["qid"], 0) + p["count"]
            goal.animal_products_required = required
            goal.animal_inventory_baseline = None
        if getattr(goal, "animal_inventory_baseline", None) is None:
            baseline = {qid: self._animal_inventory_count(pin, qid) for qid in required}
            if all(v is not None for v in baseline.values()):
                goal.animal_inventory_baseline = baseline
        if getattr(goal, "date_blocked", False):
            objective.blocker = "date_changed_fresh_criterion_required"
        objective.routine = summary(raw)
        executor = getattr(goal, "animal_executor", None)
        if executor:
            objective.routine["executor"] = {k: executor[k] for k in ("status", "steps", "actions", "cleanup", "exception", "question") if k in executor}
        if getattr(goal, "animal_inventory_baseline", None) is None:
            objective.routine["inventory_status"] = "animal_inventory_baseline_unavailable"
            return  # zero task admission until the initial product inventory baseline is available
        check = []
        for qid, count in goal.animal_products_required.items():
            current = self._animal_inventory_count(pin, qid)
            base = goal.animal_inventory_baseline[qid]
            check.append(dict(qid=qid, baseline=base, required_gain=count, current=current,
                              verified=current is not None and current >= base + count))
        objective.routine["inventory_check"] = check
        objective.routine["inventory_status"] = "verified" if all(c["verified"] for c in check) else "animal_product_inventory_unverified"
        motor = self._state.latest(pin.player_id)
        objective.blocker = objective.blocker or extras.time_refusal(objective, motor.time_of_day)
        facts = self._fishing_ui_facts(pin) or {}
        refusal = facts.get("extras_refusal")
        if isinstance(refusal, Mapping) and refusal.get("kind") in extras.OBJ_KINDS["animals"]:
            objective.routine["refusal"] = {k: str(refusal.get(k, ""))[:120] for k in ("kind", "reason", "ref")}
        self._journal_animal_summary(goal, objective.routine)
        if objective.blocker or objective.stop or verdict(objective.routine)[0] == "achieved" and pin.location == "Farm":
            goal.interact_objective = None
            return
        # Existing door/warp contract. The model selects between unfinished houses on Farm.
        allowed = {h["location"] for h in raw["houses"] if inside_pending(h)} if pin.location == "Farm" else {"Farm"}
        # Leaving an obstructed house is permitted; only the deterministic
        # planner orders deferrals, and native counts still fence completion.
        goal.animal_destinations = allowed
        nav = goal.interact_objective
        if allowed:
            if nav is None or nav.start_location != pin.location:
                goal.interact_objective = interact.InteractObjective("enter_location", route_leg=False,
                    start_location=pin.location, target=None)
        else:
            goal.interact_objective = None

    def _journal_animal_summary(self, goal, value):
        key = json.dumps(value, sort_keys=True)
        if key != getattr(goal, "animal_summary_key", None):
            goal.animal_summary_key = key
            self._journal({"ev": "animal_completion_summary", "goal_id": goal.goal_id, "native": value})

    def _animal_inventory_count(self, pin, qid):
        rec = self._obs.get(pin.player_id, "progress.inventory")
        import time
        if (rec is None or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
            return None
        return interact.inventory_count(rec, qid)

    def _animal_house_target(self, pin):
        from arti_stardew_command import validate_world_interaction_args, KesalahanPerintah
        goal = self._active
        if not self._animal_open(goal) or not extras.objective_open(goal.extras_objective):
            return None
        raw = self._animal_raw(pin)
        if raw is None or getattr(goal, "animal_inventory_baseline", None) is None:
            return None
        # A faced Farm resident owns the immediate A press before house travel.
        # Keep the native capability gate and exact task identity; the ordinary
        # world-target adapter still validates the captured actor pose/token.
        facts = self._fishing_ui_facts(pin) or {}
        faced = facts.get("world_interaction_target")
        if (pin.location == "Farm" and isinstance(faced, Mapping)
                and faced.get("kind") in extras.OBJ_KINDS["animals"]
                and facts.get("capabilities", {}).get("world_interact", {}).get("available") is True
                and self._animal_world_offered(goal, faced, pin)):
            return None
        allowed = getattr(goal, "animal_destinations", set())
        state = getattr(goal, "animal_executor", None) or {}
        wanted = state.get("destination")
        if wanted not in allowed or wanted == "Farm":
            unfinished = sorted((h for h in raw["houses"] if h["location"] in allowed),
                                key=lambda h: (h["location"] in state.get("capture_deferred_houses", set()),
                                               "Coop" not in h["type"], h["location"]))
            wanted = unfinished[0]["location"] if unfinished else None
        for route in raw.get("routes", ()):
            target = route.get("target") if isinstance(route, Mapping) else None
            if not isinstance(target, Mapping) or target.get("kind") != "animal_house_route":
                continue
            try:
                validate_world_interaction_args(dict(target))
            except (ValueError, TypeError, KesalahanPerintah):
                continue
            if (wanted is not None and target.get("target_location_id") == wanted and wanted in allowed
                    and type(route.get("input_limit")) is int and 1 <= route["input_limit"] <= HOUSE_INPUTS_MAX):
                return dict(target)
        return None

    def _animal_house_budget(self, pin, target):
        raw = self._animal_raw(pin)
        for route in (raw or {}).get("routes", ()):
            if route.get("target") == target and type(route.get("input_limit")) is int and 1 <= route["input_limit"] <= HOUSE_INPUTS_MAX:
                return route["input_limit"]
        raise ValueError("animal_house_route_changed")

    def _animal_house_arrival(self, goal, destination):
        pin = self._movement_context(self._binding_view()[1])
        raw = self._animal_raw(pin)
        return (raw is not None and pin.location == destination and basis(raw) == goal.animal_basis
                and (destination == "Farm" and raw["location_name"] == "Farm"
                     or any(h["location"] == destination for h in raw["houses"])))

    def _animal_entries(self, goal, pin, entries):
        if not self._animal_open(goal):
            return entries
        raw = self._animal_raw(pin)
        if raw is None or not extras.objective_open(goal.extras_objective) or getattr(goal, "animal_inventory_baseline", None) is None:
            return []
        allowed = getattr(goal, "animal_destinations", set())
        out = [e for e in entries if e.get("target_location") in allowed
               and e.get("kind") in ("building_door", "door", "warp")]
        if pin.location == "Farm":
            out = []  # Farm entry still uses the native whole-map house route
        else:
            from arti_stardew_goal_routes import open_paths, open_path, WAYPOINT_PATH_MAX
            motor = self._state.latest(pin.player_id)
            grid = self._nav_grid(pin)
            if grid:
                grid = dict(grid)
                for tile in extras.sleep_exit_tiles(grid, motor.tile):
                    grid[tile] = "."  # only the connected sleep row ARTI already occupies
                grid = actor_cell_open(grid, motor.tile)
            parents = open_paths(grid, motor.tile)
            # Local observed-open waypoints to distant doors; same global trial/approach budgets.
            for e in list(out):
                held = getattr(goal, "animal_waypoints", {}).get(e["target_location"])
                progress = goal.approach_progress or {}
                if held and progress.get("target_ref") == held["ref"] and progress.get("status") == "ARRIVED":
                    goal.animal_waypoints.pop(e["target_location"], None)
                    held = None
                if held:
                    out.remove(e)
                    if open_path(parents, held["tile"]) is not None:
                        out.append(held)
                    continue
                reaches = [e["tile"]] if e.get("reach") == "DIRECT_TARGET" else e.get("reach_tiles", [])
                paths = [p for r in reaches if (p := open_path(parents, r)) is not None]
                path = min(paths, key=lambda p: (len(p), p[-1])) if paths else None
                if path is not None and len(path)-1 <= WAYPOINT_PATH_MAX:
                    continue
                out.remove(e)
                surveys = getattr(goal, "animal_surveys", None)
                if surveys is None:
                    goal.animal_surveys = surveys = {}
                survey = surveys.setdefault(e["target_location"], {})
                destination = path[-1] if path else (reaches[0] if reaches else e["tile"])
                tile = house_waypoint(grid, motor.tile, destination, survey)
                if tile is not None:
                    held = dict(ref=f"animal_waypoint:{pin.location}:{tile[0]},{tile[1]}", kind="animal_route_stand",
                        tile=list(tile), reach="DIRECT_TARGET", target_location=e["target_location"],
                        revision=hashlib.sha256(str((pin.location, tile)).encode()).hexdigest()[:16])
                    goal.animal_waypoints[e["target_location"]] = held
                    out.append(held)
        state = getattr(goal, "animal_executor", None) or {}
        rebaseline_moving_tasks(raw, state)
        if state.get("stand_location") != pin.location:
            state["stand_location"] = pin.location
            for e in raw.get("targets", ()):
                if (state.get("capture_deferred", {}).get(e["ref"], 0) == 1
                        or e["ref"] in state.get("revisit_refs", set())):
                    state.get("attempted_stands", {}).pop(e["ref"], None)
                    goal.approach_blocked_refs.discard(e["ref"])
                    goal.approach_no_motion.pop(e["ref"], None)
        for e in raw.get("targets", ()):
            if (not isinstance(e, Mapping) or e.get("ready") is not True
                    or e.get("kind") not in (*extras.OBJ_KINDS["animals"], interact.KIND_FORAGE)):
                continue
            stands = extras.stand_tiles(e)
            state = getattr(goal, "animal_executor", None) or {}
            tried = state.get("attempted_stands", {}).get(e["ref"], set())
            stands = [p for p in (stands or []) if tuple(p) not in tried]
            if not stands:
                continue
            if tried and e["ref"] in goal.approach_blocked_refs:
                # Alternate freshly published stand, within the original caps.
                goal.approach_blocked_refs.discard(e["ref"])
                goal.approach_no_motion.pop(e["ref"], None)
            out.append(dict(ref=e["ref"], tile=[e["x"], e["y"]], kind=e["kind"], reach_tiles=stands,
                instance=e["instance"], revision=extras._revision(e["ref"], e["x"], e["y"], e["instance"], stands)))
        self._extras_retry_moving(goal, out)  # same moved-tile retry cap; never a new input/deadline allowance
        return out

    def _animal_flush_due(self, goal, ref):
        if not self._animal_open(goal):
            return None
        if not extras.objective_open(goal.extras_objective):
            return False
        raw = self._animal_raw(self._movement_context(self._binding_view()[1]))
        entry = next((e for e in (raw or {}).get("targets", ()) if e["ref"] == ref), None)
        if entry is not None:
            return entry["ready"] and entry["kind"] not in extras.X_KINDS
        return False if ref.startswith(("xt:animal:", "xt:trough:", "xt:obj:", "obj:")) else None

    def _animal_world_offered(self, goal, target, pin):
        if not self._animal_open(goal) or not isinstance(target, Mapping) or not extras.objective_open(goal.extras_objective):
            return False
        raw = self._animal_raw(pin)
        if raw is None or getattr(goal, "animal_inventory_baseline", None) is None:
            return False
        kind = target.get("kind")
        if kind in (interact.KIND_DOOR, interact.KIND_WARP, "animal_house_route"):
            if kind != "animal_house_route" and pin.location == "Farm":
                return False
            return target.get("target_location_id") in getattr(goal, "animal_destinations", set())
        if kind == interact.KIND_FORAGE:
            return any(e["kind"] == kind and e["ref"] == target.get("source_fact_id") for e in raw.get("targets", ()))
        return kind in extras.OBJ_KINDS["animals"] and any(
            e["ready"] and matches_task(target, e) for e in raw.get("targets", ()))

    def _animal_finish_verdict(self, goal):
        from arti_stardew_runtime import FinishEvidenceView
        if not self._animal_open(goal):
            return None
        pin = self._movement_context(self._binding_view()[1])
        self._animal_prepare(goal, pin)
        objective = goal.extras_objective
        last = getattr(goal, "last_action", None)
        blocked = objective.blocker or objective.stop
        executor = getattr(goal, "animal_executor", None)
        if executor and not executor["cleanup"]:
            blocked = "animal_action_unreconciled"
        if last is not None and (not last.cleanup_confirmed or last.world_effect_status == "uncertain" and not unpublished_replan(last)
                                 or last.lifecycle in ("uncertain", "interrupted")):
            blocked = "animal_action_unreconciled"
        status, reason = verdict(objective.routine, blocked)
        if status == "achieved" and pin.location != "Farm":
            status, reason = "partial", "animal_return_to_farm_pending"
        if executor and status == "achieved":
            executor["status"] = "complete"
            objective.routine["executor"]["status"] = "complete"
        return FinishEvidenceView(status, reason, dict(objective.routine or {}, origin="native_animal_state",
                                                       effects=list(objective.effects)))
