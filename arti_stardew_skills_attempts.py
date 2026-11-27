"""Wave 1 real attempts (owner decision 2026-10-18, PLAN §2a): S19 S24 S25 S26 S27 S28 S30 S31.

Each skill travels to its native target, presses only existing verified buttons and ends on native
readback: the effect, or the precise named gap after the attempt (``partial:<gap>:attempted``). A gap
reached with zero inputs stays a plain hold, never "attempted".

Targets come only from native offers (extras/world/menu), scene facts or the nav grid. No coordinate
literal lives here (§2a.1). Travel reuses the existing pergi_ke legs and shipped itineraries; local
walking uses only observed-open nav cells. No IO, no new primitive, executor or success shortcut.
"""
from collections.abc import Mapping

import arti_stardew_skills as S

NAMES = frozenset({"pohon_buah", "crab_pot", "masak", "siapkan_pancing", "upgrade_alat", "donasi", "surat_quest",
                   "beli_hewan"})

# Default gap per skill when an attempt ends without the native effect. The ledger may name a more
# precise one (e.g. the faced door that the mod does not offer).
GAPS = {
    "pohon_buah": "fruit_bag_verifier",
    "crab_pot": "crab_pot_machine_kind",
    "masak": "kitchen_action_not_offered",
    "siapkan_pancing": "rod_attachment_readback",
    "upgrade_alat": "upgrade_geode_readback",
    "donasi": "community_center_bundle_opener_not_offered;inventory_bundle_view_only",
    "surat_quest": "quest_delivery_readback",
    "beli_hewan": "purchase_animals_placement",
}

# Run-level failures that must stay visible verbatim (safety, budgets, lifecycle), never relabelled a gap.
_KEEP = ("action_unreconciled", "input_envelope_exhausted", "skill_budget", "stalled:", "goal_replaced",
         "start_stuck", "runtime_", "curfew", "unknown_menu_choice", "composition_identity_changed",
         "attempt_cursor_not_empty")

TRAVEL_LEGS_MAX = 6
WALK_STEPS_MAX = 120
# S30 live 4a: arrivals at one tile for one walk target before the walk ends as attempt_walk_oscillating.
WALK_REVISITS_MAX = 3
# Farmer geometry (Stardew 1.6.15 Farmer.GetBoundingBox, 48x32 box at Position.X+8 / Position.Y+Sprite.getHeight()-32):
# the motor pixel is Farmer.Position and TilePoint is the box centre at these offsets (as arti_stardew_local_approach).
TILE_PX = 64
FARMER_CENTRE_DX = 32
FARMER_CENTRE_DY = 16
# Below this a move_cardinal press may only turn the farmer (a 1-tick press turns).
WALK_TICKS_MIN = 6
# A sideways drift beyond this is a collision slide, not a clean speed sample.
WALK_SLIDE_PX = 8
# Counter-stand grab flushes per stand: on row r, Position.Y is in [64r-16, 64r+48) and GetGrabTile(up) reaches r-1
# only below 64r+5, so at most 43 px; one 3-tick flush moved 15.8 px live (gap7a S26 L303) -> 3, plus one spare.
COUNTER_FLUSH_MAX = 4
FRUIT_PICKUP_WAIT_S = 8.0
FRUIT_CATEGORY = -79
BAIT_CATEGORY = -21
TACKLE_CATEGORY = -22
CRAB_POT = "(O)710"
RODS = frozenset({"(T)BambooPole", "(T)TrainingRod", "(T)FiberglassRod", "(T)IridiumRod", "(T)AdvancedIridiumRod"})
# Vanilla interiors and the outdoor location whose native exit enters them (location names, never tiles).
# Part C uses the same map to leave them; leaving Marnie's AnimalShop goes back to the Forest.
INTERIOR_PARENT = {"Blacksmith": "Town", "CommunityCenter": "Town", "ArchaeologyHouse": "Town", "AnimalShop": "Forest"}
# Outdoor locations adjacent to the Farm but absent from goal_routes.ITINERARIES. Bounded rule: the only hop into or
# out of them is the Farm, and that hop is the existing S7 pergi_ke leg (an observed native exit, never a created one).
FARM_NEIGHBOURS = frozenset({"Forest"})
# Existing interact grammar words for each travel destination (arti_stardew_interact._LOCATION_ALIASES).
TRAVEL_TEXT = {"Farm": "pergi ke farm", "BusStop": "pergi ke bus stop", "Town": "pergi ke kota",
               "Beach": "pergi ke pantai", "SeedShop": "pergi ke seed shop", "home": "pulang",
               "Blacksmith": "pergi ke blacksmith", "CommunityCenter": "pergi ke community center",
               "ArchaeologyHouse": "pergi ke museum", "Forest": "pergi ke forest",
               "AnimalShop": "pergi ke animal shop"}
# S26: the native menu at Marnie; explicit owner requests use the B2 purchase adapter.
PURCHASE_ANIMALS_MENU = "PurchaseAnimalsMenu"
ANIMAL_MENU_CLOSES_MAX = 2
# S25: ui_nav steps to scroll one ShopMenu stock row into view: ShopFactsTable.MaxRows (60) + the 4 visible rows.
UPGRADE_SCROLL_MAX = 64


# ---------------------------------------------------------------------------- ledger

def ledger(state, name, context=None):
    a = state.setdefault("attempt", dict(skill=name, legs=[], walk_steps=0, travels=0))
    if "inputs_start" not in a and isinstance((context or {}).get("publications"), int):
        a["inputs_start"] = context["publications"]
    return a


def note(state, leg, **detail):
    legs = state["attempt"]["legs"]
    if len(legs) < 32:
        legs.append(dict(leg=leg, **detail))


def _spent(state, publications):
    a = state.get("attempt") or {}
    start = a.get("inputs_start")
    return publications - start if isinstance(publications, int) and isinstance(start, int) else 0


def end(state, context, gap):
    """The named end of an attempt. Only real inputs make it ``attempted``; zero inputs stay a plain hold."""
    a = state["attempt"]
    a["gap"] = gap
    if _spent(state, (context or {}).get("publications")) > 0:
        return S.Step("fail", reason=f"partial:{gap}:attempted")
    return S.Step("fail", reason=gap)


def finalize(call, state, outcome, publications):
    """Run boundary: a clean leg failure after real inputs is the skill's named gap, original reason kept."""
    a = state.get("attempt")
    if (call.name in NAMES and outcome is not None and outcome[0] == "fail"
            and _spent(state, publications) <= 0):
        reason = str(outcome[1])
        if reason.startswith("partial:") and reason.endswith(":attempted"):
            return "fail", reason[len("partial:"):-len(":attempted")]
        return outcome
    if (call.name not in NAMES or a is None or outcome is None or outcome[0] != "fail"
            or str(outcome[1]).startswith("partial:") and str(outcome[1]).endswith(":attempted")
            or str(outcome[1]).startswith(_KEEP) or _spent(state, publications) <= 0):
        return outcome
    a.setdefault("terminal", outcome[1])
    # S26 live (2026-10-19): a travel leg failed (vanilla Forest event, then exit_not_offered) and the default
    # gap named a purchase step the attempt never reached. A failed travel leg names itself.
    gap = a.get("gap") or ("attempt_travel_failed" if state.get("wave1_driver") == "attempt_travel"
                           else GAPS[call.name])
    return "fail", "partial:%s:attempted" % gap


def completed(call, state, reason, observed):
    """wave1_completed for attempt legs: consume one existing verifier's terminal once."""
    driver = state.get("wave1_driver")
    a = state.setdefault("attempt", dict(skill=call.name, legs=[], walk_steps=0, travels=0))
    if driver == "attempt_travel":
        a["travels"] += 1
        note(state, "travel_arrived", reason=reason, to=state.get("attempt_travel_to"))
        state.pop("ui_identity", None)  # the composition location changes only through this verified leg
    elif driver == "shake_tree" and call.name == "pohon_buah":
        a["phase"] = "bag"
        note(state, "shake_verified", reason=reason, tile=state.get("wave1_pin", {}).get("tile"))
    elif driver == "gift" and call.name == "surat_quest":
        note(state, "gift_effect_verified", reason=reason)
        state.update(wave1_driver="gift_close", wave1_text="skill surat_quest", wave1_gift_verified=True)
        return "next_leg", "gift_effect_verified"
    elif driver in CRAB_LEGS and call.name == "crab_pot":
        # W1 gap B3: a crab-pot leg's own receipt never finishes the skill; the next plan reads the native pot + bag.
        a.setdefault("crab", {})["phase"] = CRAB_LEGS[driver]
        note(state, "crab_leg_verified", driver=driver, reason=reason, tile=state.get("wave1_pin", {}).get("tile"))
    else:
        return "fail", "attempt_leg_unknown:" + str(driver)
    for key in [k for k in state if k.startswith("wait:")]:
        del state[key]
    state.update(wave1_driver=None, wave1_text="skill " + call.name)
    return "next_leg", "verified:" + str(driver)


def gift_is_not_delivery(state):
    """S28: the native gift counter rose, so the item was received as a gift, not as a quest delivery."""
    note(state, "gift_counted_not_delivery", menu_closed=True)
    state["attempt"]["gap"] = GAPS["surat_quest"]
    return S.Step("fail", reason="partial:%s:attempted" % GAPS["surat_quest"])


def leg_step(state, step):
    """A failed travel leg after real travel is evidence, not a fresh hold."""
    if step.kind == "fail":
        note(state, "travel_failed", reason=step.reason, to=state.get("attempt_travel_to"))
    return step


# ---------------------------------------------------------------------------- shared legs

def _xy(value):
    return S._wave1_xy(value)


def _here(snap):
    return _xy(S._g(S._g(snap, "motor"), "tile"))


def _location(snap):
    return S._g(S._g(snap, "motor"), "location")


def _interior(location):
    return isinstance(location, str) and location.startswith(("FarmHouse", "Cabin"))


def _counts(inventory, pred):
    out = {}
    for f in inventory:
        qid = f.get("qualified_item_id")
        if isinstance(qid, str) and pred(f):
            out[qid] = out.get(qid, 0) + (f.get("stack") or 0)
    return out


def _hop(location, destination):
    """The first hop: shipped itineraries, else the bounded Farm-neighbour rule (FARM_NEIGHBOURS)."""
    from arti_stardew_goal_routes import next_hop
    hop = next_hop(location, destination)
    if hop is None and destination in FARM_NEIGHBOURS and location != destination:
        hop = destination if location == "Farm" else next_hop(location, "Farm")
    if hop is None and location in FARM_NEIGHBOURS and location != destination and (
            destination == "Farm" or next_hop("Farm", destination) is not None):
        hop = "Farm"
    return hop


def travel(snap, state, destination):
    """One existing pergi_ke leg toward ``destination`` (shipped itinerary hops, Farm neighbours, interior parents)."""
    a = state["attempt"]
    location = _location(snap)
    if destination == "home":
        hop = "home"
    elif _interior(location):
        hop = "Farm"
    else:
        hop = _hop(location, destination) or INTERIOR_PARENT.get(location)
        if hop is None and INTERIOR_PARENT.get(destination) is not None:
            hop = _hop(location, INTERIOR_PARENT[destination]) or (
                destination if location == INTERIOR_PARENT[destination] else None)
    if hop is None or hop not in TRAVEL_TEXT:
        return None
    if a["travels"] >= TRAVEL_LEGS_MAX:
        return S.Step("fail", reason="attempt_travel_cap")
    note(state, "travel", frm=location, to=hop)
    state["attempt_travel_to"] = hop
    expected = "observed_location_change_objective"
    return S.wave1_repin(state, TRAVEL_TEXT[hop], "attempt_travel", expected)


def _stands(target, grid):
    x, y = target
    return [t for t in ((x, y + 1), (x - 1, y), (x, y - 1), (x + 1, y)) if grid.get(t) == "."]


def walk_to(snap, state, context, target, stands=None):
    """Walk observed-open nav cells to a cardinal stand of the native ``target`` and face it.

    Returns a Step while travelling, or None once ARTI stands on a stand facing the target and one
    fresh read was taken after the turn. Never a target or offer is invented here."""
    from arti_stardew_goal_routes import open_paths, open_path
    from arti_stardew_animal_routine import house_waypoint
    a = state["attempt"]
    here, grid = _here(snap), context.get("nav_grid") or {}
    if here is None or not grid or not context.get("ui_complete"):
        return S._wait(state, "attempt_grid", "attempt_nav_grid_unavailable", "Amati grid navigasi", S.WAIT_S)
    stands = [tuple(s) for s in (stands if stands is not None else _stands(target, grid))]
    speed = _walk_speed(snap, a)  # consumes our previous press's probe on every fresh read
    if here in stands:
        key = [list(here), list(target)]
        if a.get("faced") != key:
            a["faced"] = key
            a.pop("faced_read", None)
            turn = S._face(snap, target)
            if turn is not None:
                a["walk_steps"] += 1
                return S._decision(turn)
        if not a.get("faced_read"):
            a["faced_read"] = True
            return S.Step("decision", dict(S.INSPECT, question="skill attempt: read the faced native target"))
        return None
    # Blocked = our previous published walk left the fresh native tile unchanged, four times in a row.
    if a.get("walk_from") is not None:
        a["walk_stuck"] = a.get("walk_stuck", 0) + 1 if a["walk_from"] == list(here) else 0
        a["walk_from"] = None
        if a["walk_stuck"] >= 4:
            note(state, "walk_blocked", tile=list(here), target=list(target))
            return end(state, context, "attempt_walk_blocked")
    if a["walk_steps"] >= WALK_STEPS_MAX:
        note(state, "walk_cap", tile=list(here), target=list(target))
        return end(state, context, "attempt_walk_cap")
    # S30 live 4a (journal-attempt30 L76-L322): 62 moves ping-ponged between two tiles for one target. Count arrivals
    # per tile for the current target; a tile reached again more than WALK_REVISITS_MAX times ends the walk cleanly.
    visits = a.get("walk_visits")
    if not isinstance(visits, dict) or visits.get("target") != list(target):
        visits = a["walk_visits"] = dict(target=list(target), tiles={}, last=None)
    if visits["last"] != list(here):
        visits["last"] = list(here)
        key = "%d,%d" % tuple(here)
        visits["tiles"][key] = visits["tiles"].get(key, 0) + 1
        if visits["tiles"][key] > WALK_REVISITS_MAX:
            note(state, "walk_oscillating", tile=list(here), target=list(target), visits=visits["tiles"][key],
                 tiles=len(visits["tiles"]))
            return end(state, context, "attempt_walk_oscillating")
    parents = open_paths(grid, here)
    paths = [p for s in stands if (p := open_path(parents, s)) is not None and len(p) > 1]
    if paths:
        path = min(paths, key=lambda p: (len(p), p[-1]))
    else:
        goal = stands[0] if stands else tuple(target)
        waypoint = house_waypoint(grid, here, goal, a.setdefault("survey", {}), window_best=True)
        path = open_path(parents, waypoint) if waypoint is not None else None
        if not path or len(path) < 2:
            note(state, "stand_not_reachable", tile=list(here), target=list(target))
            return end(state, context, "attempt_stand_not_reachable")
    delta = (path[1][0] - here[0], path[1][1] - here[1])
    run = 1  # at most three observed-open cells in one straight hold; the next fresh read re-plans
    while run < 3 and run + 1 < len(path) and (path[run + 1][0] - path[run][0], path[run + 1][1] - path[run][1]) == delta:
        run += 1
    run = min(run, max(1, len(path) - 3))  # slow down near the stand
    ticks = _walk_ticks(snap, here, delta, run, speed, len(path) == 2)
    a["walk_steps"] += 1
    a["walk_from"] = list(here)
    pixel = _pixel(snap)
    if pixel is not None:
        a["walk_probe"] = dict(pixel=list(pixel), delta=list(delta), ticks=ticks)
    return S._decision(S.act("world_key.move_cardinal", {"direction": S._DIRS[delta], "ticks": ticks}))


def _pixel(snap):
    pixel = S._g(S._g(snap, "motor"), "pixel")
    if (isinstance(pixel, (list, tuple)) and len(pixel) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pixel)):
        return float(pixel[0]), float(pixel[1])
    return None


def _walk_speed(snap, a):
    """Observed pixels per tick: the fastest clean sample of our own previous presses (a collision only slows one).
    Before any sample: the WALK_TICKS_PER_STEP hypothesis (one tile per that many ticks)."""
    probe, pixel = a.pop("walk_probe", None), _pixel(snap)
    if isinstance(probe, Mapping) and pixel is not None and probe.get("ticks", 0) > 1:
        dx, dy = probe["delta"]
        mx, my = pixel[0] - probe["pixel"][0], pixel[1] - probe["pixel"][1]
        along, across = mx * dx + my * dy, abs(mx * dy - my * dx)
        if along > 0 and across <= WALK_SLIDE_PX:
            a["walk_ppt"] = max(a.get("walk_ppt") or 0.0, along / probe["ticks"])
    return a.get("walk_ppt") or TILE_PX / S.WALK_TICKS_PER_STEP


def _walk_ticks(snap, here, delta, run, speed, last_cell):
    """Ticks that put the farmer's box centre on the middle of the cell ``run`` cells ahead.

    S30 live 4a: a fixed WALK_TICKS_PER_STEP press moved 84.5 px (1.32 tiles) indoors, so a one-cell leg before a turn
    skipped the corner cell in both directions. Sized from the observed pixel and measured speed; the
    fresh read after the press stays the only arrival oracle. Without a consistent pixel: the old fixed sizes."""
    import math
    fallback = 8 if last_cell else S.WALK_TICKS_PER_STEP * run  # a short last cell, never a 4-tick nudge
    pixel = _pixel(snap)
    if pixel is None:
        return fallback
    centre = (pixel[0] + FARMER_CENTRE_DX, pixel[1] + FARMER_CENTRE_DY)
    if tuple(math.floor(v / TILE_PX) for v in centre) != tuple(here):
        return fallback  # inconsistent geometry cannot size a press
    axis = 0 if delta[0] else 1
    sign = delta[axis]
    distance = sign * ((here[axis] + sign * run) * TILE_PX + TILE_PX / 2 - centre[axis])
    raw = distance / speed
    ticks = math.floor(raw) if run > 1 else math.ceil(raw)
    return max(WALK_TICKS_MIN, min(S.WALK_TICKS_PER_STEP * run, ticks))


def _faced_offer(snap, context, tile, kinds):
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") in kinds and S._wave1_target_tile(target) == tuple(tile):
        return target
    return next((e for e in context.get("extras", ()) if e.get("kind") in kinds
                 and S._wave1_target_tile(e) == tuple(tile)), None)


def _refusal(snap, context):
    ui = context.get("mines_ui") or {}
    refusal = ui.get("extras_refusal") if isinstance(ui, Mapping) else None
    target = S._g(snap, "world_interaction_target") or {}
    return dict(world_target=target.get("kind"), extras_refusal=dict(refusal) if isinstance(refusal, Mapping) else None)


def enter(snap, state, context, destination):
    """Reach an interior through its native exit fact in the current location (door/warp to ``destination``)."""
    here = _here(snap)
    exits = [(ref, f) for ref, f in context.get("targets", {}).items() if isinstance(f, Mapping)
             and f.get("kind") in ("door", "building_door", "warp") and f.get("target_location") == destination
             and _xy(f.get("tile") or f.get("source_tile")) is not None]
    if not exits or here is None:
        return None
    navigable = [e for e in exits if e[1]["kind"] == "warp" or e[1]["kind"] == "door" and (e[1].get("enterable") is True or "enterable" not in e[1] and "action" not in e[1])
                 or e[1]["kind"] == "building_door" and e[1].get("enterable") is True]
    if navigable:
        return travel(snap, state, destination)
    ref, fact = min(exits, key=lambda e: (sum(abs(a - b) for a, b in zip(here, _xy(e[1].get("tile")))), e[0]))
    door = _xy(fact["tile"])
    stand = (door[0], door[1] + 1)
    step = walk_to(snap, state, context, door, stands=[stand])
    if step is not None:
        return step
    target = S._g(snap, "world_interaction_target") or {}
    # The faced door target is the mod's live ResolveDoor (CaptureTileDoor; the press re-checks it in StillPresent).
    # Since FINAL Part C3 the mod re-resolves door facts once per native clock value (SceneDoorRefresh), so a stale
    # `enterable` lasts at most until the next state send after a clock tick; the live offer still wins, as before.
    if (target.get("kind") == "door" and target.get("target_location_id") == destination
            and S._wave1_target_tile(target) == door):
        return travel(snap, state, destination)  # the existing nav presses the offered door
    # FINAL Part C proof1 (J L599-L877): the door fact's `enterable` is computed only in a full scene snapshot
    # (SceneSnapshotCapture.CaptureDoors -> NativeB1Offers.ResolveDoor at Game1.timeOfDay); Town was entered before
    # Clint's 9:00 and no snapshot followed until an NPC refresh at L1081, so the Blacksmith door read false at ~11:45.
    # Only the mod's live faced offer decides. Facing up from the stand centre (Position.Y 5256.6) GetGrabTile is the
    # own tile (Character.GetGrabTile up = (box.Y - 5) / 64; SMAPI no_chest_at_grab_tile at the stand), so no door was
    # read at all. The same bounded up-flush as Clint's counter, then a fresh read; only a native door offer acts.
    # Kept after the C3 clock refresh: the stand-centre grab-tile geometry is independent of fact freshness, and it
    # costs at most COUNTER_FLUSH_MAX short presses on a door that is still really locked.
    from arti_stardew_runtime import _grab_tile
    if _grab_tile(_pixel(snap), 0) == here:
        step = _clint_flush(snap, state, context, door, owner="door")
        if step is not None:
            return step
    note(state, "door_not_offered", ref=ref, action=fact.get("action"), stand=list(stand), **_refusal(snap, context))
    return end(state, context, destination.casefold() + "_door_not_offered")


# ---------------------------------------------------------------------------- per-skill plans

def plan(call, snap, state, context) -> S.Step:
    """The next attempt step from fresh native facts (driver-less phase of the composition)."""
    if not context.get("ui_complete") or not context.get("inventory_complete"):
        return S._wait(state, "attempt_readback", "composition_readback_unavailable", "Amati fakta native", S.WAIT_S)
    ledger(state, call.name, context)
    return {"pohon_buah": _fruit, "crab_pot": _crab_pot, "masak": _cook, "siapkan_pancing": _rod,
            "upgrade_alat": _clint, "donasi": _donation, "surat_quest": _quest,
            "beli_hewan": _animal}[call.name](call, snap, state, context)


def _fruit(call, snap, state, context):
    a = state["attempt"]
    fruit = lambda f: f.get("category") == FRUIT_CATEGORY
    if a.get("phase") == "bag":
        now = _counts(context["inventory"], fruit)
        gained = {q: n - a["bag_before"].get(q, 0) for q, n in now.items() if n > a["bag_before"].get(q, 0)}
        if gained:
            a["fruit_gained"] = gained
            note(state, "fruit_in_bag", gained=gained)
            return S.Step("done", reason="observed_fruit_pickup")
        step = S._wait(state, "fruit_bag", "fruit_not_in_bag", "Amati buah masuk tas", FRUIT_PICKUP_WAIT_S)
        if step.kind == "fail":
            note(state, "fruit_not_in_bag", bag=now)
            return end(state, context, GAPS["pohon_buah"])
        return step
    here = _here(snap)
    trees = sorted((sum(abs(p - q) for p, q in zip(here, tile)), ref, tile)
                   for ref, f in context.get("targets", {}).items()
                   if isinstance(f, Mapping) and f.get("kind") == "terrain"
                   and (f.get("fruit_tree") or {}).get("has_fruit") is True
                   and (tile := _xy(f.get("tile"))) is not None) if here is not None else []
    if not trees:
        if _location(snap) != "Farm":
            step = travel(snap, state, "Farm")
            if step is not None:
                return step
        note(state, "fruit_tree_not_observed", location=_location(snap))
        return end(state, context, "fruit_tree_not_offered")
    _, ref, tile = trees[0]
    a.setdefault("tree", dict(ref=ref, tile=list(tile)))
    if a["tree"]["ref"] != ref:
        return end(state, context, "fruit_tree_identity_changed")
    offer = _faced_offer(snap, context, tile, ("shake_tree",))
    if offer is not None or any(e.get("kind") == "shake_tree" and S._wave1_target_tile(e) == tile
                                for e in S._g(snap, "approach_targets", ()) or ()):
        a["bag_before"] = _counts(context["inventory"], fruit)
        note(state, "shake_offered", tile=list(tile))
        return S.wave1_repin(state, "goyang pohon buah", "shake_tree", "observed_extras_effect", ref=None, tile=tile)
    step = walk_to(snap, state, context, tile)
    if step is not None:
        return step
    note(state, "shake_not_offered", tile=list(tile), **_refusal(snap, context))
    return end(state, context, "fruit_tree_shake_not_offered")


# ---------------------------------------------------------------------------- S24 crab_pot (W1 gap B3)

# The verified leg -> the fresh native readback phase that follows it (completed()); a leg receipt never finishes S24.
CRAB_LEGS = {"place_item": "placed", "machine_insert": "baited", "machine_collect": "collected"}
# Owner 2026-10-19 linked witness: `crab_pot pasang` (day 1 place + bait), ordinary sleep, `crab_pot ambil` (day 2).
# The plain goal (None) collects an own pot that already holds a catch, else places + baits and holds until the night.
CRAB_MODES = (None, "pasang", "ambil")
CRAB_READY_WAIT_S = 6.0  # a catch becomes collectable once the pot is drawn (CrabPot.draw -> tileIndexToShow 714)


def _crab_entries(context, key):
    """The mod's ui.context crab catalog (``crab_pots`` / ``crab_water``), validated; None when unreadable."""
    ui = context.get("mines_ui")
    raw = ui.get(key) if isinstance(ui, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return None
    out = []
    for e in raw:
        tile = _xy((e.get("x"), e.get("y"))) if isinstance(e, Mapping) else None
        if tile is not None:
            out.append(dict(e, tile=tile, stands=[_xy(s) for s in e.get("stands") or () if _xy(s) is not None]))
    return out


def _crab_own_complete(context, pots):
    """#322 native metadata: the mod lists own pots first and says whether every own pot is listed. True only when
    that claim is published and matches the listed own pots; a bridge without it keeps the capped-catalog guard."""
    ui = context.get("mines_ui")
    if not isinstance(ui, Mapping) or ui.get("crab_pots_own_complete") is not True:
        return False
    own_total = ui.get("crab_pots_own_total")
    return type(own_total) is int and own_total == sum(1 for p in pots if p.get("own") is True)


def _pinned_pot(pots, crab):
    """The pinned pot by its native identity (location + tile + owner token) at its pinned tile, or None."""
    pin = crab.get("pot") or {}
    hits = [p for p in pots if p["tile"] == tuple(pin.get("tile") or ()) and p.get("pot_id") == pin.get("pot_id")]
    return hits[0] if len(hits) == 1 else None


def _quality_count(inventory, qid, quality):
    return sum(f.get("stack") or 0 for f in inventory if f.get("qualified_item_id") == qid and f.get("quality", 0) == quality)


def _pot_fact(pot):
    return {k: pot.get(k) for k in ("pot_id", "own", "state", "bait", "catch", "catch_stack")} if pot else None


def _crab_pot(call, snap, state, context):
    """S24: place -> bait (day 1), the native night, collect (day 2). Each leg is an existing verified extras button
    pinned by tile; each phase ends on a fresh native pot + bag readback before the next one starts."""
    if call.arg not in CRAB_MODES:
        return S.Step("fail", reason="crab_pot_mode_unknown")
    crab = state["attempt"].setdefault("crab", {})
    pots, waters = _crab_entries(context, "crab_pots"), _crab_entries(context, "crab_water")
    if pots is None or waters is None:
        return S._wait(state, "crab_facts", "crab_pot_facts_unavailable", "Amati crab pot native", S.WAIT_S)
    bag = _counts(context["inventory"], lambda f: True)
    phase = crab.get("phase")
    # B3 publishes at most four pots. An omitted pin is unknown, never removal evidence or permission to place.
    # #322: a bridge that lists own pots first and reports every own pot listed lifts that doubt (S24 pins only
    # own pots); without that metadata a full catalog stays possibly truncated.
    if len(pots) >= 4 and not _crab_own_complete(context, pots):
        pin = crab.get("pot") or {}
        tile = pin.get("tile")
        if not pin and phase == "placed":
            tile = (crab.get("water") or {}).get("tile")
        if tile is not None and (not pin or pin.get("location") == _location(snap)) and not any(
                p["tile"] == tuple(tile) for p in pots):
            step = S._wait(state, "crab_catalog", "crab_pot_catalog_truncated", "Amati crab pot yang dipin", S.WAIT_S)
            return end(state, context, "crab_pot_catalog_truncated") if step.kind == "fail" else step
        if phase is None or phase == "collect" and not pin:
            return end(state, context, "crab_pot_catalog_full")
    if phase == "placed":
        return _crab_placed(snap, state, context, pots, bag)
    if phase == "bait":
        return _crab_bait(snap, state, context, pots, bag)
    if phase == "baited":
        return _crab_baited(call, state, context, pots, bag)
    if phase == "collect":
        return _crab_collect(snap, state, context, pots, bag)
    if phase == "collected":
        return _crab_collected(state, context, pots, bag)
    if phase is not None:
        return S.Step("fail", reason="crab_pot_leg_unverified:" + str(phase))
    own = [p for p in pots if p.get("own") is True]
    if call.arg == "ambil" or (call.arg is None and any(p.get("state") in ("catch", "ready") for p in own)):
        crab["phase"] = "collect"
        return _crab_collect(snap, state, context, pots, bag)
    if own:
        # One own pot per location keeps the day-2 identity unambiguous; an already baited pot waits for its night.
        note(state, "pot_already_placed", pots=[_pot_fact(p) for p in own][:4])
        return end(state, context, "crab_pot_awaiting_native_ready" if call.arg is None and any(
            p.get("state") == "baited" for p in own) else "crab_pot_already_placed")
    return _crab_place(snap, state, context, waters, bag)


def _crab_place(snap, state, context, waters, bag):
    crab = state["attempt"]["crab"]
    if bag.get(CRAB_POT, 0) < 1:
        return end(state, context, "crab_pot_not_owned")
    here, grid = _here(snap), context.get("nav_grid") or {}
    if "water" not in crab:
        if here is None or not grid:
            return S._wait(state, "attempt_grid", "attempt_nav_grid_unavailable", "Amati grid navigasi", S.WAIT_S)
        from arti_stardew_goal_routes import open_paths
        reach = open_paths(grid, here) or {}
        # Valid water is the mod's native CrabPot.IsValidCrabPotLocationTile list, never a nav-grid 'w' guess.
        choices = sorted((min(abs(s[0] - here[0]) + abs(s[1] - here[1]) for s in stands), w["tile"], stands)
                         for w in waters if (stands := [s for s in w["stands"] if not reach or s in reach]))
        if not choices:
            note(state, "water_not_offered", location=_location(snap), waters=len(waters))
            return end(state, context, "crab_pot_water_not_offered")
        _, tile, stands = choices[0]
        crab["water"] = dict(tile=list(tile), stands=[list(s) for s in stands])
    tile = tuple(crab["water"]["tile"])
    step = walk_to(snap, state, context, tile, stands=[tuple(s) for s in crab["water"]["stands"]])
    if step is not None:
        return step
    held = (S._g(snap, "held_item") or {}).get("qualified_item_id")
    if held != CRAB_POT and not crab.get("pot_selected"):
        choice = S._select(snap, {CRAB_POT})
        if choice is None:
            note(state, "pot_select_not_offered")
            return end(state, context, "crab_pot_select_not_offered")
        crab["pot_selected"] = True
        return S._decision(choice)
    if not crab.get("place_read"):
        crab["place_read"] = True
        return S.Step("decision", dict(S.INSPECT, question="skill crab_pot: read the native place offer at the water"))
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") == "place_item" and S._wave1_target_tile(target) == tile:
        # The mod offers this only with a held crab pot on native-valid crab-pot water.
        crab.update(phase="placing", bag_pot=bag.get(CRAB_POT, 0))
        note(state, "pot_place_offered", tile=list(tile))
        return S.wave1_repin(state, "pasang crab pot", "place_item", "observed_extras_effect", ref=None, tile=tile)
    note(state, "place_not_offered", held=held, water=list(tile), **_refusal(snap, context))
    return end(state, context, "crab_pot_place_not_offered")


def _crab_placed(snap, state, context, pots, bag):
    crab = state["attempt"]["crab"]
    tile = tuple(crab["water"]["tile"])
    hits = [p for p in pots if p["tile"] == tile]
    pot = hits[0] if len(hits) == 1 else None
    if (pot is None or pot.get("own") is not True or pot.get("state") != "empty" or pot.get("bait")
            or not pot.get("pot_id") or bag.get(CRAB_POT, 0) != crab["bag_pot"] - 1):
        note(state, "place_readback", pot=_pot_fact(pot), bag_pot=bag.get(CRAB_POT, 0))
        return end(state, context, "crab_pot_place_unverified")
    crab.update(phase="bait", bag_pot=bag.get(CRAB_POT, 0),
                pot=dict(pot_id=pot["pot_id"], tile=list(tile), location=_location(snap)))
    note(state, "pot_placed", **crab["pot"])
    return _crab_bait(snap, state, context, pots, bag)


def _crab_bait(snap, state, context, pots, bag):
    crab = state["attempt"]["crab"]
    pot = _pinned_pot(pots, crab)
    if pot is None or pot.get("state") != "empty":
        note(state, "bait_target", pot=_pot_fact(pot))
        return end(state, context, "crab_pot_identity_changed" if pot is None else "crab_pot_bait_unverified")
    tile = tuple(crab["pot"]["tile"])
    step = walk_to(snap, state, context, tile, stands=[tuple(s) for s in crab["water"]["stands"]])
    if step is not None:
        return step
    baits = {f.get("qualified_item_id") for f in context["inventory"]
             if f.get("category") == BAIT_CATEGORY and isinstance(f.get("qualified_item_id"), str)}
    if not baits:
        note(state, "bait_not_owned")
        return end(state, context, "crab_pot_bait_not_owned")
    if not crab.get("bait_selected"):
        choice = S._select(snap, baits)
        if choice is None:
            note(state, "bait_select_not_offered", baits=sorted(baits))
            return end(state, context, "crab_pot_bait_not_in_toolbar")
        crab["bait_selected"] = choice["args"].get("qualified_item_id")
        return S._decision(choice)
    if not crab.get("bait_read"):
        crab["bait_read"] = True
        return S.Step("decision", dict(S.INSPECT, question="skill crab_pot: read the native bait offer at the pot"))
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") == "machine_insert" and S._wave1_target_tile(target) == tile:
        # The mod offers this only for a bait-category item, a pot that needs bait and its native drop-in probe.
        crab.update(phase="baiting", bag_baits={q: bag.get(q, 0) for q in baits})
        note(state, "pot_bait_offered", bait=crab["bait_selected"], tile=list(tile))
        return S.wave1_repin(state, "isi mesin", "machine_insert", "observed_extras_effect", ref=None, tile=tile)
    note(state, "bait_not_offered", tile=list(tile), **_refusal(snap, context))
    return end(state, context, "crab_pot_bait_not_offered")


def _crab_baited(call, state, context, pots, bag):
    crab = state["attempt"]["crab"]
    pot = _pinned_pot(pots, crab)
    bait, before = (pot or {}).get("bait"), crab.get("bag_baits") or {}
    if (pot is None or pot.get("own") is not True or pot.get("state") != "baited" or bait not in before
            or bag.get(bait, 0) != before[bait] - 1 or bag.get(CRAB_POT, 0) != crab["bag_pot"]
            or any(bag.get(q, 0) != n for q, n in before.items() if q != bait)):
        note(state, "bait_readback", pot=_pot_fact(pot), bag={q: bag.get(q, 0) for q in before})
        return end(state, context, "crab_pot_bait_unverified")
    crab["day1"] = dict(crab["pot"], bait=bait)
    note(state, "pot_baited", **crab["day1"])
    if call.arg == "pasang":
        return S.Step("done", reason="observed_crab_pot_baited")
    # The catch exists only after the native DayUpdate of a real night: never awaited inside one run.
    return end(state, context, "crab_pot_awaiting_native_ready")


def _crab_collect(snap, state, context, pots, bag):
    crab = state["attempt"]["crab"]
    location = _location(snap)
    if "pot" not in crab:
        own = [p for p in pots if p.get("own") is True and p.get("state") != "empty"]
        if not own:
            if _interior(location):
                step = travel(snap, state, "Farm")
                if step is not None:
                    return step
            note(state, "pot_not_observed", location=location, pots=len(pots))
            return end(state, context, "crab_pot_not_observed")
        if len(own) != 1:
            note(state, "pot_ambiguous", pots=[_pot_fact(p) for p in own][:4])
            return end(state, context, "crab_pot_ambiguous")
        crab["pot"] = dict(pot_id=own[0]["pot_id"], tile=list(own[0]["tile"]), location=location, bait=own[0].get("bait"))
        note(state, "pot_pinned", pot_state=own[0].get("state"), **crab["pot"])
    pot = _pinned_pot(pots, crab)
    if pot is None or location != crab["pot"]["location"]:
        note(state, "pot_identity", pot=_pot_fact(pot), location=location)
        return end(state, context, "crab_pot_identity_changed")
    if pot.get("state") == "baited":
        return end(state, context, "crab_pot_awaiting_native_ready")
    if pot.get("state") not in ("catch", "ready") or not pot.get("catch"):
        note(state, "catch_readback", pot=_pot_fact(pot))
        return end(state, context, "crab_pot_catch_unverified")
    tile = tuple(crab["pot"]["tile"])
    step = walk_to(snap, state, context, tile, stands=pot["stands"])
    if step is not None:
        return step
    from arti_stardew_auto_act import plain_hand, plain_tool_choice, empty_slot_choice
    if not plain_hand(snap):  # the native A collects with an empty hand or a plain tool only
        choice = plain_tool_choice(snap) or empty_slot_choice(snap)
        if choice is None or crab.get("hand_selected"):
            return end(state, context, "crab_pot_plain_hand_not_offered")
        crab["hand_selected"] = choice[0] == "select_item"  # fix74a: a row swap is not the selection
        return S._decision(S.act(choice[0], choice[2]))
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") != "machine_collect":
        step = _crab_grab_flush(snap, state, tile)
        if step is not None:
            return step
    stack, quality = pot.get("catch_stack"), pot.get("catch_quality")
    if (pot.get("state") == "ready" and target.get("kind") == "machine_collect" and S._wave1_target_tile(target) == tile
            and type(stack) is int and stack > 0 and type(quality) is int):
        crab.update(phase="collecting", bag_pot=bag.get(CRAB_POT, 0),
                    catch=dict(item=pot["catch"], quality=quality, stack=stack, doubling=pot.get("doubling") is True),
                    bag_catch=_quality_count(context["inventory"], pot["catch"], quality))
        note(state, "pot_collect_offered", pot_id=pot["pot_id"], bait=pot.get("bait"), **crab["catch"])
        return S.wave1_repin(state, "ambil hasil mesin", "machine_collect", "observed_extras_effect", ref=None, tile=tile)
    step = S._wait(state, "crab_ready", "crab_pot_collect_not_offered", "Amati crab pot siap diambil", CRAB_READY_WAIT_S)
    if step.kind == "fail":
        note(state, "collect_not_offered", pot=_pot_fact(pot), **_refusal(snap, context))
        return end(state, context, "crab_pot_collect_not_offered")
    return step


def _crab_grab_flush(snap, state, tile):
    """gap8c P3 L213: arrival/facing is not grab-tile alignment. Four short ordinary moves at most, with fresh
    forward pixel progress on the same stand; only a subsequent native machine_collect offer authorizes A."""
    from arti_stardew_runtime import _grab_tile
    from arti_stardew_interact_b import EAT_SITE_FLUSH_TICKS
    a, here, pixel = state["attempt"], _here(snap), _pixel(snap)
    if here is None or pixel is None or a.get("faced") != [list(here), list(tile)] or not a.get("faced_read"):
        return None
    delta = (tile[0] - here[0], tile[1] - here[1])
    if delta not in S._DIRS:
        return None
    direction = S._DIRS[delta]
    facing = ("up", "right", "down", "left").index(direction)
    if _grab_tile(pixel, facing) != here:
        return None
    crab = a["crab"]
    flush = crab.setdefault("grab_flush", dict(origin=list(pixel), last=None, n=0))
    along = lambda p, q: sum((p[i] - q[i]) * delta[i] for i in range(2))
    if (flush["n"] >= 4 or along(pixel, flush["origin"]) >= TILE_PX
            or flush["last"] is not None and along(pixel, flush["last"]) <= 0):
        return None
    flush.update(last=list(pixel), n=flush["n"] + 1)
    note(state, "pot_grab_flush", tile=list(here), target=list(tile), pixel=list(pixel), n=flush["n"])
    return S._decision(S.act("world_key.move_cardinal", dict(direction=direction, ticks=EAT_SITE_FLUSH_TICKS)))


def _crab_collected(state, context, pots, bag):
    crab = state["attempt"]["crab"]
    pot, c = _pinned_pot(pots, crab), crab["catch"]
    gained = _quality_count(context["inventory"], c["item"], c["quality"]) - crab["bag_catch"]
    if (pot is None or pot.get("own") is not True or pot.get("state") != "empty" or pot.get("catch") or pot.get("bait")
            or bag.get(CRAB_POT, 0) != crab["bag_pot"]
            or not (gained == c["stack"] or c["doubling"] and gained == 2 * c["stack"])):
        note(state, "collect_readback", pot=_pot_fact(pot), gained=gained)
        return end(state, context, "crab_pot_collect_unverified")
    crab["day2"] = dict(crab["pot"], catch=c["item"], quality=c["quality"], gained=gained)
    note(state, "pot_collected", **crab["day2"])
    return S.Step("done", reason="observed_crab_pot_collected")


# ---------------------------------------------------------------------------- S30 masak (W1 gap B3)

COOK_OPEN_WAIT_S = 4.0  # ActivateKitchen's fridge mutex request is asynchronous (GameLocation.cs:8205-8218)


def _cook_bag_ok(row, inventory):
    """Bag-first consumeIngredients, reserving stacks across fixed/category ingredients.

    The current bridge publishes aggregate container availability, not a bounded
    container identity/debit ledger. Never press X if that ledger would be needed.
    """
    remaining = [dict(f) for f in sorted(inventory, key=lambda f: f.get("slot", -1), reverse=True)]
    for ing in row.get("ingredients") or ():
        key, need = str(ing.get("item", "")), ing.get("need")
        if not key or type(need) is not int or need < 1:
            return False
        cat = int(key) if key.startswith("-") and key[1:].isdigit() else None
        qid = key if key.startswith("(") else "(O)" + key
        for fact in remaining:
            if (fact.get("category") == cat if cat is not None else fact.get("qualified_item_id") == qid):
                stack = fact.get("stack")
                if type(stack) is int and stack > 0:
                    used = min(stack, need)
                    fact["stack"], need = stack - used, need - used
                    if need == 0:
                        break
        if need:
            return False
    return bool(row.get("ingredients"))


def cook_press_allowed(cook, view, policy):
    """The economy filter for a running masak attempt: X cooks ONE of the pinned recipe on the pinned standalone
    cooking page; A only stashes that recipe's output from the cursor into an inventory slot. Anything else: none."""
    row = cook.get("row") or {}
    econ = getattr(view, "economy", None) or {}
    if (not row or econ.get("kind") != "crafting" or econ.get("cooking") is not True
            or cook.get("receiver") != getattr(view, "receiver_token", None)):
        return set()
    held = getattr(view, "held_item", None) or {}
    snapped = getattr(view, "snapped_id", None)
    recipe = next((r for r in econ.get("recipes", ()) if r.get("id") == snapped), None)
    if not held:
        return {"X"} if (recipe is not None and recipe.get("id") == row["id"] and recipe.get("recipe") == row["recipe"]
                         and recipe.get("item") == row["item"] and recipe.get("craftable") is True
                         and recipe.get("known") is not False and policy.refuse_craft(recipe.get("recipe")) is None) else set()
    component = next((c for c in getattr(view, "components", ()) if c.get("id") == snapped), {})
    return {"A"} if (held.get("item") == row["item"] and recipe is None and component.get("kind") == "slot"
                     and (component.get("value") or {}).get("item") in (None, row["item"])) else set()


def _cook(call, snap, state, context):
    """S30: home -> the actor's own kitchen A -> the owner-named known recipe X (one) -> stash -> close -> bag readback."""
    from arti_stardew_skills_ui import _press
    if not call.arg:
        return S.Step("fail", reason="cook_recipe_not_requested")  # never a default recipe
    a = state["attempt"]
    cook = a.setdefault("cook", {})
    phase, view = cook.get("phase"), context.get("ui_view")
    if phase is None:
        return _kitchen(snap, state, context, view)
    if cook.get("identity") != context.get("identity"):
        return S.Step("fail", reason="cook_identity_changed")
    last = S._g(S._g(snap, "working_memory"), "last_action")
    before, after = S._g(last, "before") or {}, S._g(last, "after") or {}
    if phase == "closed":
        return _cook_verify(snap, state, context, last, before, after)
    if phase == "open":
        if view is None:
            if S.menu_up(snap):
                return S.Step("fail", reason="unexpected_kitchen_menu")
            step = S._wait(state, "cook_open", "kitchen_menu_not_opened", "Amati menu masak", COOK_OPEN_WAIT_S)
            if step.kind == "fail":
                note(state, "kitchen_menu_not_opened", **_refusal(snap, context))
                return end(state, context, "kitchen_menu_not_opened")
            return step
        if not view.available:
            return S._wait(state, "cook_ready", "cooking_menu_not_ready", "Tunggu menu masak siap", S.WAIT_S)
        econ = view.economy or {}
        if (S._g(last, "lifecycle") != "completed" or S._g(last, "cleanup_confirmed") is not True
                or S._g(last, "outcome") != "MenuOpened" or after.get("kind") != "kitchen"
                or S._g(last, "world_effect_status") != "observed_extras_effect"
                or after.get("receiver_token") != view.receiver_token
                or view.context != "crafting" or econ.get("cooking") is not True):
            return S.Step("fail", reason="cooking_menu_unverified")
        want = call.arg.casefold()
        rows = [r for r in econ.get("recipes", ()) if want in (str(r.get("name", "")).casefold(),
                                                              str(r.get("recipe", "")).casefold())]
        gap = ("cook_recipe_unavailable" if len(rows) != 1 else "cook_recipe_not_known" if rows[0].get("known") is not True
               else "cook_ingredients_missing" if rows[0].get("craftable") is not True
               else "cook_ingredients_not_in_bag" if not _cook_bag_ok(rows[0], context["inventory"]) else None)
        note(state, "cooking_menu", receiver=view.receiver_token, gap=gap,
             recipes=[str(r.get("name")) for r in econ.get("recipes", ())][:8])
        cook["receiver"] = view.receiver_token
        if gap is not None:
            cook.update(phase="close", gap=gap)  # close the opened page first; the named gap follows the closure
        else:
            row = rows[0]
            cook.update(phase="route", bag=_counts(context["inventory"], lambda f: True),
                        categories={f.get("qualified_item_id"): f.get("category") for f in context["inventory"]},
                        row=dict(id=row["id"], name=row.get("name"), recipe=row.get("recipe"), item=row.get("item"),
                                 produces=row.get("produces"), ingredients=[dict(i) for i in row.get("ingredients") or ()]))
        phase = cook["phase"]
    if view is None or view.receiver_token != cook.get("receiver") or view.context != "crafting":
        return S.Step("fail", reason="cooking_menu_changed")
    held = view.held_item or {}
    if phase == "cooking":
        row, press = cook["row"], before.get("ui_press") or {}
        receipt = (after.get("ui_press") or {}).get("economy") or {}
        if (not _press_receipt(last, cook["receiver"]) or press.get("btn") != "X" or press.get("snapped_id") != row["id"]
                or receipt.get("kind") != "craft" or receipt.get("item") != row["item"] or receipt.get("quantity") != 1
                or receipt.get("observed") is not True or receipt.get("effect") != "crafted"):
            note(state, "craft_receipt", receipt=dict(receipt) if isinstance(receipt, Mapping) else None)
            cook.update(phase="close", gap="cook_craft_unverified")
        else:
            cook.update(phase="stash" if held else "close", receipt=S._g(last, "request_id"))
            note(state, "crafted", item=row["item"], held=held.get("item"))
        phase = cook["phase"]
    if phase == "stashing":
        if not _press_receipt(last, cook["receiver"]) or held:
            return S.Step("fail", reason="attempt_cursor_not_empty")
        cook["phase"] = phase = "close"
    if phase == "route":
        if held:
            return S.Step("fail", reason="attempt_cursor_not_empty")
        # Re-read the bag after navigation too; no mutation if a fridge would now
        # be needed. Preserve the admitted baseline for the exact final ledger.
        bag_ok = _cook_bag_ok(cook["row"], context["inventory"])
        step = (_go(snap, view, [cook["row"]["id"]], "cook_navigation_not_offered", a) if bag_ok
                else S.Step("fail", reason="cook_ingredients_not_in_bag"))
        if step is not None and step.kind == "decision":
            return step
        if step is None and "X" in (S._g(snap, "ui_pad_target") or {}).get("press", {}):
            cook["phase"] = "cooking"
            return _press(snap, "X")
        cook.update(phase="close", gap=step.reason if step is not None else "cook_press_not_offered")
        phase = "close"
    if phase == "stash":
        if held.get("item") != cook["row"]["item"]:
            return S.Step("fail", reason="attempt_cursor_not_empty")
        targets = [c["id"] for c in view.components if c.get("kind") == "slot"
                   and (c.get("value") or {}).get("item") in (None, held.get("item"))]
        step = _go(snap, view, targets, "cook_stash_not_offered", a) if targets else S.Step("fail", reason="no_slot")
        if step is not None:
            return step if step.kind == "decision" else S.Step("fail", reason="attempt_cursor_not_empty")
        cook["phase"] = "stashing"
        step = _press(snap, "A")
        return step if step.kind == "decision" else S.Step("fail", reason="attempt_cursor_not_empty")
    if phase == "close":
        if held:
            return S.Step("fail", reason="attempt_cursor_not_empty")
        close = S._menu_close(snap)
        if close is None:
            return S.Step("fail", reason="cooking_menu_not_closable")
        cook["phase"] = "closed"
        return S._decision(close)
    return S.Step("fail", reason="cook_phase_unknown:" + str(phase))


def _kitchen(snap, state, context, view):
    """Home through the existing pulang leg, then only the mod's offered own-kitchen target is pressed."""
    a, cook = state["attempt"], state["attempt"]["cook"]
    location = _location(snap)
    if view is not None or S.menu_up(snap):
        return S.Step("fail", reason="unexpected_kitchen_menu")
    if not _interior(location):
        step = travel(snap, state, "home")
        return step if step is not None else end(state, context, "kitchen_home_not_reached")
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") == "kitchen":
        cook.update(phase="open", identity=context.get("identity"), tile=list(S._wave1_target_tile(target) or ()))
        note(state, "kitchen_pressed", tile=cook["tile"], location=location)
        return S._decision(S.act("world_interact", {}))
    here = _here(snap)
    kitchens = sorted(((sum(abs(p - q) for p, q in zip(here, tile)), str(e.get("ref")), tile, e)
                       for e in context.get("extras", ()) if e.get("kind") == "kitchen" and e.get("ready") is not False
                       and e.get("stands") and (tile := S._wave1_target_tile(e)) is not None),
                      key=lambda k: k[:3]) if here is not None else []
    if not kitchens:
        note(state, "kitchen_offer_read", location=location, **_refusal(snap, context),
             extras=sorted({str(e.get("kind")) for e in context.get("extras", ())}))
        return end(state, context, GAPS["masak"])
    from arti_stardew_auto_act import plain_hand, plain_tool_choice
    if not plain_hand(snap):  # the native Kitchen A with an object in hand would place / eat / gift instead
        choice = plain_tool_choice(snap)
        if choice is None or cook.get("hand_selected"):
            return end(state, context, "kitchen_plain_hand_not_offered")
        cook["hand_selected"] = choice[0] == "select_item"  # fix74a: a row swap is not the selection
        return S._decision(S.act(choice[0], choice[2]))
    _, _, tile, e = kitchens[0]
    step = walk_to(snap, state, context, tile, stands=e.get("stands"))
    if step is not None:
        return step
    # As at Clint's counter (S25 journal L645): facing up from a logical stand need not put GetGrabTile on the
    # kitchen. One bounded approach flush, then a fresh read; only the mod's next native offer authorizes A.
    from arti_stardew_runtime import _grab_tile
    from arti_stardew_interact_b import EAT_SITE_FLUSH_TICKS
    motor = S._g(snap, "motor")
    key = [list(here), list(tile)]
    # MotorView carries no facing (live 4a S25): walk_to's faced ledger proves the turn toward the kitchen.
    if (tile == (here[0], here[1] - 1) and a.get("faced") == key and a.get("faced_read")
            and _grab_tile(S._g(motor, "pixel"), 0) == here and cook.get("flush") != key):
        cook["flush"] = key
        a.pop("faced_read", None)
        a["walk_steps"] += 1
        note(state, "kitchen_grab_flush", tile=list(here), target=list(tile), **_refusal(snap, context))
        return S._decision(S.act("world_key.move_cardinal", {"direction": "up", "ticks": EAT_SITE_FLUSH_TICKS}))
    note(state, "kitchen_not_faced", tile=list(tile), **_refusal(snap, context))
    return end(state, context, GAPS["masak"])


def _cook_ingredient_ok(row, deltas, categories):
    """Bag-only exact ledger: the output gained ``produces``; each fixed ingredient lost exactly its need; a category
    ingredient (e.g. Fried Egg's -5 any egg) lost exactly its need across items of that category; nothing else moved."""
    produces = row.get("produces") if type(row.get("produces")) is int and row["produces"] > 0 else 1
    expected, cats = {row["item"]: produces}, {}
    for ing in row.get("ingredients") or ():
        key, need = str(ing.get("item", "")), ing.get("need")
        if type(need) is not int or need < 1 or not key:
            return False
        if key.startswith("-") and key[1:].isdigit():
            cats[int(key)] = cats.get(int(key), 0) + need
        else:
            qid = key if key.startswith("(") else "(O)" + key
            expected[qid] = expected.get(qid, 0) - need
    for cat, need in cats.items():
        moved = {q: d for q, d in deltas.items() if categories.get(q) == cat and q not in expected}
        if any(d > 0 for d in moved.values()) or sum(moved.values()) != -need:
            return False
        expected.update(moved)
    return all(deltas.get(q, 0) == n for q, n in expected.items()) and all(
        d == 0 for q, d in deltas.items() if q not in expected)


def _cook_verify(snap, state, context, last, before, after):
    cook = state["attempt"]["cook"]
    if S.menu_up(snap) or context.get("ui_view") is not None:
        return S.Step("fail", reason="cooking_menu_not_closed")
    closed = any((before.get(op) or {}).get("receiver_token") == cook["receiver"]
                 and (after.get(op) or {}).get("effect") == "closed" for op in ("ui_press", "ui_key"))
    if not (closed and S._g(last, "lifecycle") == "completed" and S._g(last, "cleanup_confirmed") is True):
        return S.Step("fail", reason="cooking_close_unverified")
    if cook.get("gap"):
        return end(state, context, cook["gap"])
    now = _counts(context["inventory"], lambda f: True)
    deltas = {q: now.get(q, 0) - cook["bag"].get(q, 0) for q in set(now) | set(cook["bag"])}
    deltas = {q: d for q, d in deltas.items() if d}
    categories = dict(cook["categories"], **{f.get("qualified_item_id"): f.get("category") for f in context["inventory"]})
    note(state, "cook_readback", deltas=deltas)
    if not _cook_ingredient_ok(cook["row"], deltas, categories):
        return end(state, context, "cook_bag_unverified")
    state["attempt"]["cook_evidence"] = dict(recipe=cook["row"]["recipe"], item=cook["row"]["item"], deltas=deltas,
                                            request_id=cook.get("receipt"), menu_closed=True)
    return S.Step("done", reason="observed_craft")


def _clint(call, snap, state, context):
    """Counter A -> pinned Upgrade answer -> owner row A -> native order/deltas -> close."""
    from arti_stardew_skills_ui import _press
    if not call.arg:
        return S.Step("fail", reason="upgrade_tool_not_requested")
    a = state["attempt"]
    location = _location(snap)
    if location != "Blacksmith":
        if a.get("clint_phase"):
            return S.Step("fail", reason="upgrade_location_changed")
        step = enter(snap, state, context, "Blacksmith")
        if step is not None:
            return step
        step = travel(snap, state, "Town") if location != "Town" else None
        return step if step is not None else end(state, context, "blacksmith_exit_not_observed")
    identity = context.get("identity")
    if identity is None or a.setdefault("clint_identity", identity) != identity:
        return S.Step("fail", reason="upgrade_identity_changed")
    view = context.get("ui_view")
    pad = S._g(snap, "ui_pad_target") or {}
    if view is not None and not view.available:
        return S._wait(state, "clint_ready", "upgrade_menu_not_ready", "Tunggu menu Clint siap", S.WAIT_S)
    state.pop("wait:clint_ready", None)  # a later native dialogue advance has its own bounded readiness wait
    if view is not None and ((pad.get("pins") or {}).get("receiver_token") != view.receiver_token
            or (pad.get("pins") or {}).get("target_token") != view.target_token):
        return S.Step("fail", reason="upgrade_menu_not_offered")
    last = S._g(S._g(snap, "working_memory"), "last_action")
    before, after = S._g(last, "before") or {}, S._g(last, "after") or {}
    phase = a.get("clint_phase")
    bag = _counts(context["inventory"], lambda f: True)
    if phase in ("purchase", "close", "closed"):
        row = a["upgrade_row"]
        expected = dict(a["upgrade_bag"])
        for qid, used in ((row["trade_item"], row["trade_count"]), (row["required_tool"], 1)):
            expected[qid] = expected.get(qid, 0) - used
        expected = {q: n for q, n in expected.items() if n}
        if (context.get("tool_upgrading") != row["item"] or bag != expected
                or context.get("money") != a["upgrade_money"] - row["price"]):
            return S.Step("fail", reason="upgrade_native_order_or_deltas_unverified")
        if phase == "purchase":
            receipt = (after.get("ui_press") or {}).get("economy") or {}
            press = before.get("ui_press") or {}
            if (not _press_receipt(last, row["receiver"]) or press.get("btn") != "A"
                    or press.get("snapped_id") != row["id"] or receipt.get("kind") != "upgrade"
                    or receipt.get("item") != row["item"] or receipt.get("quantity") != 1
                    or receipt.get("observed") is not True or receipt.get("effect") != "upgrade_started"
                    or receipt.get("tool_upgrading") != row["item"]
                    or receipt.get("money_before") != a["upgrade_money"]
                    or receipt.get("money_after") != context["money"]
                    or receipt.get("item_before") != 0 or receipt.get("item_after") != 0
                    or any((receipt.get("ingredients_before") or {}).get(q) != a["upgrade_bag"].get(q)
                        or (receipt.get("ingredients_after") or {}).get(q) != expected.get(q, 0)
                        for q in (row["trade_item"], row["required_tool"]))):
                return S.Step("fail", reason="upgrade_purchase_unverified")
            a["upgrade_receipt"] = S._g(last, "request_id")
            result = after.get("ui_press") or {}
            if view is None or result.get("result_receiver_token") != view.receiver_token:
                return S.Step("fail", reason="upgrade_confirmation_receiver_unverified")
            a["clint_phase"] = "close"
        if a["clint_phase"] == "closed":
            # Shared S25 closure (dialogue_close_receipt): gap8g L717 advanced -> follow only the receipt's resulting
            # DialogueBox; gap8j L716-L724 an advanced final page that then closed natively is the closed state.
            got = dialogue_close_receipt(snap, view, a, "upgrade")
            if got == "pending":
                return S._wait(state, "clint_close", "upgrade_close_unverified", "Amati dialog Clint", S.WAIT_S)
            if got == "closed":
                # menu_up also includes is_player_free=false. Actual menu evidence was checked separately so
                # a closed dialogue can settle that final motor bit without bypassing any close check.
                if S._g(S._g(snap, "motor"), "is_player_free") is not True:
                    return S._wait(state, "clint_return", "upgrade_player_not_free", "Tunggu Clint selesai", S.WAIT_S)
                a["upgrade_evidence"] = dict(item=row["item"], required_tool=row["required_tool"],
                    bars=row["trade_count"], money=row["price"], request_id=a["upgrade_receipt"], menu_closed=True)
                return S.Step("done", reason="observed_requested_tool_upgrade")
            if got != "advanced":  # never repeat the upgrade purchase
                return S.Step("fail", reason="upgrade_close_unverified")
        if view is None or view.context != "dialogue" or view.held_item or getattr(view, "options", ()):
            return S.Step("fail", reason="upgrade_confirmation_unavailable")
        a["clint_phase"] = "closed"  # at the cap it already was (every counted press set it)
        return dialogue_close_press(snap, state, a, view, "upgrade", "clint_close")
    if phase == "counter":
        if view is None:
            return S._wait(state, "clint_open", "blacksmith_question_not_opened", "Amati pertanyaan Clint", S.WAIT_S)
        if (not S._g(last, "request_id") or S._g(last, "lifecycle") != "completed" or S._g(last, "cleanup_confirmed") is not True
                or S._g(last, "outcome") != "QuestionOpened" or after.get("kind") != "blacksmith"
                or S._g(last, "world_effect_status") != "observed_extras_effect"
                or after.get("receiver_token") != view.receiver_token):
            return S.Step("fail", reason="blacksmith_question_unverified")
        a.update(clint_phase="question", clint_question=view.receiver_token)
    if a.get("clint_phase") == "question":
        key = S._g(snap, "ui_key_target") or {}
        choices = [o for o in view.options if o.get("response_key") == "Upgrade"] if view else []
        if (view is None or view.context != "question" or view.receiver_token != a["clint_question"]
                or (getattr(view, "question_key", None) or key.get("question_key")) != "Blacksmith"
                or len(choices) != 1 or "A" not in pad.get("press", {})):
            return S.Step("fail", reason="blacksmith_upgrade_response_not_offered")
        a.update(clint_phase="shop", clint_choice=str(choices[0]["index"]))
        return S._decision(S.act("ui_press", dict(pad["pins"], btn="A", snapped_id=pad.get("snapped_id"), choice=a["clint_choice"])))
    if a.get("clint_phase") == "shop":
        if "clint_shop" not in a:
            press, result = before.get("ui_press") or {}, after.get("ui_press") or {}
            if (not _press_receipt(last, a["clint_question"]) or press.get("choice") != a["clint_choice"]
                    or view is None or result.get("result_receiver_token") != view.receiver_token
                    or result.get("result_menu_type") != "ShopMenu"):
                return S.Step("fail", reason="blacksmith_upgrade_shop_unverified")
            a["clint_shop"] = view.receiver_token
        econ = view.economy or {} if view else {}
        if (view is None or view.context != "shop" or view.receiver_token != a["clint_shop"]
                or econ.get("shop_id") != "ClintUpgrade" or view.held_item):
            return S.Step("fail", reason="clint_upgrade_shop_changed")
        if context.get("upgrade_policy") is not True or econ.get("upgrade_allowed") is not True:
            return S.Step("fail", reason="upgrade_policy_refused")
        wanted = call.arg.casefold()
        owned = {f.get("qualified_item_id") for f in context["inventory"]
                 if str(f.get("name") or f.get("display_name") or "").casefold() == wanted}
        rows = [r for r in econ.get("rows", ()) if r.get("tool") is True and
                (str(r.get("name", "")).casefold() == wanted or r.get("required_tool") in owned)]
        if len(rows) != 1:
            return S.Step("fail", reason="upgrade_requested_row_unavailable_or_ambiguous")
        row = dict(rows[0], receiver=view.receiver_token)
        if (not row.get("required_tool") or bag.get(row["required_tool"]) != 1 or bag.get(row["item"], 0) != 0
                or type(row.get("price")) is not int or row["price"] <= 0
                or type(row.get("trade_count")) is not int or row["trade_count"] <= 0
                or bag.get(row.get("trade_item"), 0) < row["trade_count"]):
            return S.Step("fail", reason="upgrade_tool_or_bars_missing")
        if type(context.get("money")) is not int or context["money"] < row["price"]:
            return S.Step("fail", reason="upgrade_money_missing")
        a["upgrade_row"] = row
        if row.get("id") is None:
            # Live gap8a S25 (journal L669/L675): the requested row was forSale index 4 while the native window shows
            # four rows (ids only for the visible ones), so it had no component id. Scroll it in natively.
            step = _shop_scroll(snap, state, view, econ, row)
            if step is not None:
                return step
            return S.Step("fail", reason="upgrade_requested_row_not_visible")
        a.pop("upgrade_scroll_seen", None)
        a.pop("upgrade_scroll_request", None)
        step = _go(snap, view, [row["id"]], "upgrade_navigation_not_offered", a)
        if step is not None:
            return step
        if "A" not in pad.get("press", {}):
            return S._wait(state, "clint_row", "upgrade_purchase_not_offered", "Amati tawaran upgrade", S.WAIT_S)
        a.update(clint_phase="purchase", upgrade_bag=bag, upgrade_money=context["money"])
        return _press(snap, "A")
    if view is not None or S.menu_up(snap):
        return S.Step("fail", reason="unexpected_blacksmith_menu")
    target = S._g(snap, "world_interaction_target") or {}
    if target.get("kind") == "blacksmith":
        a["clint_phase"] = "counter"
        return S._decision(S.act("world_interact", {}))
    counters = [e for e in context.get("extras", ()) if e.get("kind") == "blacksmith" and e.get("ready") is not False]
    if counters:
        e = counters[0]
        step = walk_to(snap, state, context, (e["x"], e["y"]), stands=e.get("stands"))
        if step is not None:
            return step
        # S25 journal L645: facing up from a logical stand need not put GetGrabTile
        # on the counter. Reuse the runtime's one bounded approach flush, then read
        # again; only the mod's subsequent native offer can authorize A.
        from arti_stardew_runtime import _grab_tile
        motor = S._g(snap, "motor")
        # Facing up toward the counter directly above (walk_to's faced ledger); MotorView has no facing field.
        if _grab_tile(S._g(motor, "pixel"), 0) == _here(snap):
            step = _clint_flush(snap, state, context, (e["x"], e["y"]))
            if step is not None:
                return step
    # Live B1c (journal-attempt25 L672): the mod refused the faced counter as grab_tile_is_own_tile and the catalog
    # then marked it not ready, so the estimate-gated flush above never ran. The native refusal itself names the case.
    refusal = _refusal(snap, context).get("extras_refusal") or {}
    if refusal.get("kind") == "blacksmith" and refusal.get("reason") == "grab_tile_is_own_tile":
        step = _clint_flush(snap, state, context, (refusal.get("x"), refusal.get("y")))
        if step is not None:
            return step
    note(state, "clint_counter_not_offered", **_refusal(snap, context))
    return end(state, context, "clint_counter_not_offered")


def _shop_scroll(snap, state, view, econ, row):
    """Bring one published but off-window ShopMenu stock row into the native four-row window with ordinary ui_nav.

    Only offered native edges: a concrete published row neighbour, or the stock row's own CUSTOM_SNAP edge, which
    ShopMenu.customSnapBehavior turns into downArrowPressed/upArrowPressed while keeping the snapped row
    (ShopMenu.cs:458-459, :688-724). Each nav must change the snapped row or the window; a repeated state stops.
    Returns the nav decision, a fail, or None when nothing is offered (the caller names the row not visible)."""
    from arti_stardew_skills_ui import CUSTOM_SNAP
    from arti_stardew_ui_pad import DIRECTIONS, menu_route
    a = state["attempt"]
    offer = S._g(snap, "ui_pad_target") or {}
    rows = [r for r in econ.get("rows", ()) if type(r.get("index")) is int]
    window = sorted(r["index"] for r in rows if type(r.get("id")) is int)
    if "upgrade_scroll_seen" not in a:
        note(state, "upgrade_shop_window", index=row.get("index"), window=window, snapped=view.snapped_id,
             rows_total=econ.get("rows_total"))
    if not window or type(row.get("index")) is not int or not offer.get("nav"):
        return None
    if view.snapped_id is None or view.snapped_id == -1:
        # The shop opens with nothing snapped (journal L669 snapped_id -1): one offered nav snaps the native default.
        return _go(snap, view, (), "upgrade_row_scroll_not_offered", a)
    a.pop("snap_initialized", None)
    seen = a.setdefault("upgrade_scroll_seen", [])
    key = [view.snapped_id, window]
    settled = S._g(S._g(S._g(snap, "working_memory"), "last_action"), "request_id")
    if a.get("upgrade_scroll_request", object()) != settled:
        # A new action settled since the previous scroll decision (a discarded/re-planned decision is not one).
        if key in seen:
            return S.Step("fail", reason="upgrade_row_scroll_no_effect")
        if len(seen) >= UPGRADE_SCROLL_MAX:
            return S.Step("fail", reason="upgrade_row_scroll_cap")
        seen.append(key)
        last = S._g(S._g(snap, "working_memory"), "last_action")
        source = (S._g(last, "before") or {}).get("ui_nav") or {}
        result = (S._g(last, "after") or {}).get("ui_nav") or {}
        if (settled and S._g(last, "lifecycle") == "completed" and S._g(last, "cleanup_confirmed") is True
                and S._g(last, "world_effect_status") == "observed_ui_nav"
                and source.get("receiver_token") == view.receiver_token
                and source.get("context") == "shop" and result.get("effect") == "snapped"
                and result.get("result_receiver_token") == view.receiver_token):
            # gap8c L678/685/695/702: fresh native list navigation is progress, not idle recovery. Never refill
            # any input/decision/time/rate/64-nav cap, and a reread of this receipt cannot reset the allowance twice.
            state["stall_refreshes"] = 0
    a["upgrade_scroll_request"] = settled
    here = next((r for r in rows if r.get("id") == view.snapped_id), None)
    if here is None:
        # Snapped outside the stock window (e.g. the inventory): walk published edges to the nearest visible row.
        near = min((r for r in rows if type(r.get("id")) is int), key=lambda r: abs(r["index"] - row["index"]))
        route = menu_route(view.components, view.snapped_id, [near["id"]], view.denylisted_ids)
        if not route:
            return S.Step("fail", reason="upgrade_row_scroll_not_offered")
        return S._decision(S.act("ui_nav", dict(offer.get("pins") or {}, dir=route[0]["dir"])))
    direction = "down" if row["index"] > here["index"] else "up"
    comp = next((c for c in view.components if c.get("id") == view.snapped_id), None)
    edge = comp["nbr"][DIRECTIONS.index(direction)] if comp is not None and len(comp.get("nbr", ())) == 4 else None
    ids = {c.get("id") for c in view.components}
    if edge != CUSTOM_SNAP and edge not in ids:
        return S.Step("fail", reason="upgrade_row_scroll_not_offered")
    return S._decision(S.act("ui_nav", dict(offer.get("pins") or {}, dir=direction)))


def _clint_flush(snap, state, context, counter, *, owner="clint"):
    """One bounded up-flush when ARTI stands directly below the counter facing it but the native grab tile is its own
    tile. Only the mod's subsequent fresh native offer can authorize A."""
    from arti_stardew_interact_b import EAT_SITE_FLUSH_TICKS
    a = state["attempt"]
    here = _here(snap)
    key = [list(here), list(counter)] if here is not None else None
    # Live 4a: MotorView carries no facing, so `facing == 0` never held. walk_to's own ledger proves the turn: it
    # records faced=[here, target] and reads the faced target once (faced_read) after turning toward it.
    if (here is None or tuple(counter) != (here[0], here[1] - 1) or a.get("faced") != key
            or not a.get("faced_read")):
        return None
    # Live gap7a S26 (journal L296/L303): one 3-tick nudge moved Position.Y 1057.8 -> 1041.9 on Marnie's stand, but
    # the grab tile needs Y - 5 below the stand row's top edge, so it stayed ARTI's own tile and the counter was
    # never offered. Another nudge
    # is allowed only on the same stand, after a fresh faced read, when the previous one was observed to move ARTI up
    # (the counter's Buildings tile stops it at the edge), bounded to one tile of travel.
    pixel = _pixel(snap)
    y = pixel[1] if pixel is not None else None
    ys = a.get(owner + "_flush_y") or []
    if a.get(owner + "_flush") == key and (
            len(ys) >= COUNTER_FLUSH_MAX or y is None or ys[-1] is None or not y < ys[-1]):
        return None
    if a.get(owner + "_flush") != key:
        ys = []
    a[owner + "_flush"] = key
    a[owner + "_flush_y"] = ys + [y]
    a.pop("faced_read", None)
    a["walk_steps"] += 1
    note(state, owner + "_grab_flush", tile=list(here), target=list(counter), y=y, **_refusal(snap, context))
    return S._decision(S.act("world_key.move_cardinal", {"direction": "up", "ticks": EAT_SITE_FLUSH_TICKS}))


def _animal(call, snap, state, context):
    """S26: Farm -> Forest -> Marnie's AnimalShop through native exit facts, then only an offered counter."""
    a = state["attempt"]
    location = _location(snap)
    if location == "AnimalShop" or a.get("counter") is not None:
        return _animal_counter(snap, state, context, call)
    step = enter(snap, state, context, "AnimalShop") if location == "Forest" else None
    if step is not None:
        return step
    step = travel(snap, state, "Forest") if location != "Forest" else None
    if step is not None:
        return step
    note(state, "animalshop_exit_not_observed", location=location)
    return end(state, context, "animalshop_exit_not_observed")


def _menu_type(snap):
    key, pad = S._g(snap, "ui_key_target") or {}, S._g(snap, "ui_pad_target") or {}
    return (S._g(S._g(snap, "motor"), "active_menu") or key.get("menu_type")
            or (pad.get("pins") or {}).get("menu_type"))


def _animal_counter(snap, state, context, call=None):
    """Named requests use the native B2 phases. Legacy unnamed attempts only open/close; they never buy."""
    if call is not None and call.arg:
        from arti_stardew_animal_purchase import plan
        return plan(call, snap, state, context)
    a = state["attempt"]
    counter = a.get("counter")
    if counter is None:
        target = S._g(snap, "world_interaction_target") or {}
        if target.get("kind") == "shop_counter":
            if S.menu_up(snap):
                return S.Step("fail", reason="unexpected_animalshop_menu")
            a["counter"] = dict(label=target.get("label"), closes=0)
            note(state, "counter_pressed", label=target.get("label"))
            return S._decision(S.act("world_interact", {}))
        here = _here(snap)
        counters = sorted((sum(abs(p - q) for p, q in zip(here, tile)), str(e.get("label")), tile)
                          for e in context.get("extras", ()) if e.get("kind") == "shop_counter"
                          and (tile := S._wave1_target_tile(e)) is not None) if here is not None else []
        if not counters:
            note(state, "animalshop_inside", location=_location(snap), counters=[], **_refusal(snap, context))
            return end(state, context, "animalshop_counter_not_offered")
        _, label, tile = counters[0]
        step = walk_to(snap, state, context, tile)
        if step is not None:
            return step
        note(state, "counter_not_faced", label=label, tile=list(tile), **_refusal(snap, context))
        return end(state, context, "animalshop_counter_not_offered")
    if not S.menu_up(snap):
        if not counter.get("menu"):
            step = S._wait(state, "animal_menu", "animalshop_counter_menu_not_opened", "Amati menu Marnie", S.WAIT_S)
            if step.kind == "fail":
                note(state, "counter_menu_not_opened")
                return end(state, context, "animalshop_counter_menu_not_opened")
            return step
        note(state, "menu_closed", menu=counter["menu"])
        if counter["menu"] == PURCHASE_ANIMALS_MENU:
            return end(state, context, GAPS["beli_hewan"])  # the first unsupported step: placement, then the textbox
        return end(state, context, "animalshop_counter_question" if counter.get("question")
                   else "purchase_animals_menu_not_offered")
    key = S._g(snap, "ui_key_target") or {}
    menu = _menu_type(snap)
    if not counter.get("menu"):
        counter.update(menu=menu, question=key.get("kind") == "question")
        note(state, "counter_menu", menu=menu, key_kind=key.get("kind"))
    if key.get("kind") == "npc_question":
        return S.Step("fail", reason="unknown_menu_choice")  # never answer a question
    if counter["closes"] >= ANIMAL_MENU_CLOSES_MAX:
        note(state, "menu_not_closed", menu=menu)
        return end(state, context, "animalshop_menu_not_closed")
    close = S._menu_close(snap)  # existing B / cancel / close only: a question is cancelled, never answered
    if close is None:
        note(state, "menu_close_not_offered", menu=menu)
        return end(state, context, "animalshop_menu_close_not_offered")
    counter["closes"] += 1
    return S._decision(close)


def _donation(call, snap, state, context):
    from arti_stardew_skills_ui import plan as menu_plan
    a = state["attempt"]
    if not call.arg or " ke " not in call.arg:
        return S.Step("fail", reason="donation_item_not_requested")
    destination = "ArchaeologyHouse" if call.arg.rsplit(" ke ", 1)[1] == "museum" else "CommunityCenter"
    location = _location(snap)
    if location != destination and not a.get("menu_started"):
        if S.menu_up(snap):
            return S.Step("fail", reason="unexpected_donation_menu")
        step = enter(snap, state, context, destination)
        if step is None:
            step = travel(snap, state, "Town") if location != "Town" else None
        if step is not None:
            return step
        note(state, "donation_exit_not_observed", location=location)
        return end(state, context, destination.casefold() + "_exit_not_observed")
    a["menu_started"] = True
    step = menu_plan(call, snap, state, context)
    if step.kind == "fail":
        note(state, "donation_menu", reason=step.reason)
        if step.reason.startswith("unsupported:"):
            return end(state, context, step.reason[len("unsupported:"):])
    return step


def _quest(call, snap, state, context):
    """S28 requires a named item and villager before binding a native quest."""
    import sys
    from arti_stardew_skills_quest import inventory_item_matches, parse_request, plan as quest_plan

    request = parse_request(call.arg)
    if request is None:
        return S.Step("fail", reason="quest_item_and_npc_not_named")
    q = state["attempt"].get("quest") or {}
    if not q.get("bound") and not any(inventory_item_matches(request[0], f) for f in context["inventory"]):
        return end(state, context, "quest_item_missing")
    return quest_plan(request, snap, state, context, sys.modules[__name__])


# ---------------------------------------------------------------------------- S31 inventory attach

BAIT_SLOT = 0  # FishingRod.canThisBeAttached: native slot 0 takes bait, slots 1+ take tackle (FishingRod.cs:1053-1080)


def rod_attachments(fact):
    """S31: the native attachment facts of one inventory slot fact (mod S31 fields), or None when unreadable.

    Returns ``(tool_instance, entries)``; each entry is ``dict(slot, item, stack, category, uses)`` with
    ``item=None`` for an explicit empty slot. A capped or malformed list is unreadable, never guessed."""
    if not isinstance(fact, Mapping):
        return None
    token, count, raw = fact.get("tool_instance"), fact.get("attachment_slots"), fact.get("attachments")
    if (not isinstance(token, str) or not token or type(count) is not int or count < 1
            or not isinstance(raw, (list, tuple)) or len(raw) != count):
        return None
    entries = []
    for i, e in enumerate(raw):
        if not isinstance(e, Mapping) or e.get("slot") != i or type(e.get("occupied")) is not bool:
            return None
        if not e["occupied"]:
            entries.append(dict(slot=i, item=None, stack=0, category=None, uses=None))
            continue
        qid, stack = e.get("qualified_item_id"), e.get("stack")
        if not isinstance(qid, str) or not qid or type(stack) is not int or stack < 1:
            return None
        entries.append(dict(slot=i, item=qid, stack=stack, category=e.get("category"), uses=e.get("uses")))
    return token, tuple(entries)


def _slot_ids(view, qid):
    return [c["id"] for c in view.components if c.get("kind") == "slot" and (c.get("value") or {}).get("item") == qid]


def _press_receipt(last, receiver):
    """The last action is our verified, released ui_press (A or X) on this receiver."""
    press = (S._g(last, "before") or {}).get("ui_press") or {}
    return bool(S._g(last, "request_id") and S._g(last, "lifecycle") == "completed"
                and S._g(last, "cleanup_confirmed") is True and S._g(last, "world_effect_status") == "observed_ui_press"
                and press.get("receiver_token") == receiver and press.get("btn") in ("A", "X"))


# ---------------------------------------------------------------------------- shared NPC dialogue closure
# The S25 pattern (gap8g 285f4fa0 + gap8j 8e268e64; S25 VERIFIED LIVE in batch 8k), shared by S25 upgrade_alat,
# S26 beli_hewan and S28 surat_quest. State lives under ``<prefix>_close_presses / _receiver / _after_request``.

DIALOGUE_CLOSE_PRESSES_MAX = 8


def _menu_evidence(snap, view):
    return (view is not None or any(S._g(snap, k) for k in ("exit_target", "ui_key_target", "ui_pad_target"))
            or bool(S._g(S._g(snap, "motor"), "active_menu")))


def dialogue_close_receipt(snap, view, a, prefix):
    """Classify the newest receipt after our planned dialogue A on ``a[prefix + '_close_receiver']``.

    pending:   the newest request is still the one before that A (no new receipt yet);
    closed:    our verified A closed the DialogueBox, or (gap8j L716-L724) "advanced" its final page into the SAME
               receiver which then closed natively; either way no view and no menu evidence is left;
    advanced:  our verified A advanced / replaced it, and the current view IS the receipt's resulting DialogueBox;
    unsettled: our verified A advanced the same receiver, no view is readable yet but menu evidence remains;
    None:      anything else (unverified). The player-free wait belongs to the caller."""
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if S._g(last, "request_id") == a.get(prefix + "_close_after_request"):
        return "pending"
    source = (S._g(last, "before") or {}).get("ui_press") or {}
    result = (S._g(last, "after") or {}).get("ui_press") or {}
    if (not _press_receipt(last, a.get(prefix + "_close_receiver")) or source.get("btn") != "A"
            or source.get("context") != "dialogue"):
        return None
    if result.get("effect") == "closed":
        if (_menu_evidence(snap, view) or result.get("result_receiver_token") is not None
                or result.get("result_menu_type") is not None):
            return None
        return "closed"
    if result.get("effect") not in ("advanced", "replaced") or result.get("result_menu_type") != "DialogueBox":
        return None
    if view is None:
        if result.get("result_receiver_token") != a.get(prefix + "_close_receiver"):
            return None
        return "unsettled" if _menu_evidence(snap, view) else "closed"
    return "advanced" if result.get("result_receiver_token") == view.receiver_token else None


def dialogue_close_press(snap, state, a, view, prefix, wait_key):
    """One offered A on the verified current DialogueBox (the caller checked context/options), capped at 8."""
    from arti_stardew_skills_ui import _press
    pins = (S._g(snap, "ui_pad_target") or {}).get("pins") or {}
    if pins.get("receiver_token") != view.receiver_token or pins.get("target_token") != view.target_token:
        return S.Step("fail", reason=prefix + "_dialogue_not_offered")
    if a.get(prefix + "_close_presses", 0) >= DIALOGUE_CLOSE_PRESSES_MAX:
        return S.Step("fail", reason=prefix + "_dialogue_close_cap")
    state.pop("wait:" + wait_key, None)
    a[prefix + "_close_presses"] = a.get(prefix + "_close_presses", 0) + 1
    a.update({prefix + "_close_receiver": view.receiver_token,
              prefix + "_close_after_request": S._g(S._g(S._g(snap, "working_memory"), "last_action"), "request_id")})
    return _press(snap, "A")


def _go(snap, view, targets, reason, a):
    from arti_stardew_ui_pad import menu_route
    offer = S._g(snap, "ui_pad_target") or {}
    if view.snapped_id is None or view.snapped_id == -1:
        # Opening the menu can leave the native snap null (wire -1): no graph origin. One ordinary offered nav
        # initializes it, as the crafting bootstrap does (UiPadInput.NavRefusal admits it); re-read before routing.
        if a.get("snap_initialized") or not offer.get("nav"):
            return S.Step("fail", reason=reason)
        a["snap_initialized"] = True
        return S._decision(S.act("ui_nav", dict(offer.get("pins") or {}, dir="right")))
    a.pop("snap_initialized", None)
    if view.snapped_id in targets:
        return None
    route = menu_route(view.components, view.snapped_id, targets, view.denylisted_ids)
    if not offer.get("nav") or not route:
        return S.Step("fail", reason=reason)
    return S._decision(S.act("ui_nav", dict(offer.get("pins") or {}, dir=route[0]["dir"])))


def _rod_pick(call, state, context):
    """Choose one rod and one owned bait/tackle from fresh native facts. Returns a hold reason or None."""
    a, inventory = state["attempt"], context["inventory"]
    words = set((call.arg or "").casefold().split())
    tackle = bool(words & {"tackle", "tekel"})
    replace = bool(words & {"ganti", "replace"})  # an attachment is replaced only when the owner asked for it
    wanted = TACKLE_CATEGORY if tackle else BAIT_CATEGORY
    rods = sorted(((f.get("slot", 99), f.get("qualified_item_id"), f) for f in inventory
                   if f.get("qualified_item_id") in RODS), key=lambda r: r[0])
    baits = sorted((f.get("slot", 99), f.get("qualified_item_id")) for f in inventory
                   if f.get("category") == wanted and isinstance(f.get("qualified_item_id"), str))
    if not rods or not baits:
        return "rod_or_bait_not_owned"
    usable = []
    for slot, qid, fact in rods:
        parsed = rod_attachments(fact)
        if parsed is None:
            continue
        token, entries = parsed
        if tackle:
            empties = [e["slot"] for e in entries if e["slot"] != BAIT_SLOT and e["item"] is None]
            target = empties[0] if empties else (BAIT_SLOT + 1 if len(entries) > BAIT_SLOT + 1 else None)
        else:
            target = BAIT_SLOT
        if target is not None:
            usable.append((slot, qid, token, entries, target))
    if not usable:
        return GAPS["siapkan_pancing"]  # no rod with readable attachment facts / the requested slot: unverifiable
    slot, rod, token, entries, target = usable[0]
    if sum(1 for _, q, _f in rods if q == rod) != 1:
        return "rod_ambiguous"  # UI slot components match by qid; two same-qid rods cannot be told apart there
    bait, current = baits[0][1], entries[target]
    if current["item"] is not None and (not replace or current["item"] == bait):
        return "rod_attachment_occupied"
    a.update(rod=rod, rod_slot=slot, rod_token=token, bait=bait, phase="open", att_slot=target, replace=replace,
             att_before=[dict(e) for e in entries], old=current["item"], old_stack=current["stack"],
             bag_before=_counts(inventory, lambda f: f.get("qualified_item_id") in (rod, bait, current["item"])))
    note(state, "rod_bait_selected", rod=rod, rod_slot=slot, rod_token=token, bait=bait, bait_slot=baits[0][0],
         att_slot=target, old=current["item"])
    return None


def _rod_verify(state, context):
    """S31 PASS rule: the SAME rod instance's native attachment slot went empty -> bait (or, when asked, old -> bait)
    and the bag lost exactly what the slot gained, every other slot unchanged. Anything else is a named gap."""
    a, inventory = state["attempt"], context["inventory"]
    bag = _counts(inventory, lambda f: f.get("qualified_item_id") in (a["rod"], a["bait"], a["old"]))
    moved = a["bag_before"].get(a["bait"], 0) - bag.get(a["bait"], 0)
    same = [f for f in inventory if f.get("tool_instance") == a["rod_token"]]
    rods = [f for f in inventory if f.get("qualified_item_id") == a["rod"]]
    parsed = rod_attachments(same[0]) if len(same) == 1 else None
    after = parsed[1] if parsed is not None else None
    note(state, "closed_readback", bait_moved=moved, picked=a.get("picked", 0), same_rod=len(same) == 1,
         rods=len(rods), after=[[e["item"], e["stack"]] for e in after] if after else None)
    if len(same) != 1 or len(rods) != 1 or same[0].get("qualified_item_id") != a["rod"]:
        return "rod_identity_changed"  # not the rod instance observed before (an unobserved replacement)
    before, k = a["att_before"], a["att_slot"]
    if after is None or len(after) != len(before):
        return GAPS["siapkan_pancing"]
    landed = [e["slot"] for e in after if e["item"] == a["bait"] and before[e["slot"]]["item"] != a["bait"]]
    if landed and k not in landed:
        return "rod_attachment_wrong_slot"
    if any(after[i]["item"] != before[i]["item"] or after[i]["stack"] != before[i]["stack"]
           for i in range(len(after)) if i != k):
        return "rod_attachment_unreconciled"
    gained = after[k]["stack"] if after[k]["item"] == a["bait"] else 0
    if a["old"] is not None and after[k]["item"] == a["bait"]:
        if not a["replace"]:
            return "rod_attachment_replaced_previous"
        if bag.get(a["old"], 0) - a["bag_before"].get(a["old"], 0) != a["old_stack"]:
            return "rod_attach_bag_unreconciled"
    if moved == 0 and gained == 0:
        return "rod_attach_refused"
    if not (moved == gained == a.get("picked", 0) > 0) or bag.get(a["rod"], 0) != 1:
        return "rod_attach_bag_unreconciled"  # bag loss without the matching attachment is never a PASS
    return None


def _rod(call, snap, state, context):
    from arti_stardew_skills_ui import _press
    a = state["attempt"]
    view = context.get("ui_view")
    if "rod" not in a:
        reason = _rod_pick(call, state, context)
        if reason == "rod_or_bait_not_owned":
            return S.Step("fail", reason=reason)
        if reason is not None:
            return end(state, context, reason)
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if view is None:
        if S.menu_up(snap):
            return S.Step("fail", reason="unexpected_rod_menu")
        if a["phase"] == "open":
            offer = S._g(snap, "pad_button_offer") or {}
            if offer.get("button") != "Start":
                return end(state, context, "inventory_menu_not_offered")
            a["phase"] = "pick"
            note(state, "open_inventory")
            return S._decision(S.act("pad_button", {"button": "Start"}))
        if a["phase"] == "closed":
            gap = _rod_verify(state, context)
            if gap is None:
                note(state, "rod_attachment_verified", rod_token=a["rod_token"], att_slot=a["att_slot"], bait=a["bait"])
                return S.Step("done", reason="observed_rod_attachment")
            return end(state, context, gap)
        return S.Step("fail", reason="rod_menu_closed_unexpectedly")
    if view.context != "game_menu" or view.page_type not in (None, "InventoryPage"):
        return end(state, context, "inventory_page_not_offered")
    held = view.held_item or {}
    receipt = _press_receipt(last, view.receiver_token)
    if a.get("pending"):
        phase = a.pop("pending")
        if not receipt:
            return end(state, context, "rod_press_unverified")
        if phase == "pick":
            if held and held.get("item") != a["bait"]:
                return S.Step("fail", reason="attempt_cursor_not_empty")
            if not held:
                return end(state, context, "bait_pick_unverified")
            a["picked"] = held.get("stack") or 0
            a["phase"] = "attach"
            note(state, "bait_held", stack=a["picked"])
        elif phase == "attach":
            a["attach"] = "held_empty" if not held else "held_" + str(held.get("item"))
            note(state, "rod_pressed", held=held.get("item"), held_stack=held.get("stack"))
            a["phase"] = "close" if not held else "stash"
        elif phase == "stash":
            if held:
                return S.Step("fail", reason="attempt_cursor_not_empty")
            a["phase"] = "close"
    if a["phase"] in ("pick", "attach", "stash"):
        if a["phase"] == "pick":
            targets, button = _slot_ids(view, a["bait"]), "X" if view.press_allowed("X") else "A"
        elif a["phase"] == "attach":
            targets, button = _slot_ids(view, a["rod"]), "X"
        else:
            targets = [c["id"] for c in view.components if c.get("kind") == "slot"
                       and (not c.get("value") or (c.get("value") or {}).get("item") == held.get("item"))]
            button = "A"
        if not targets:
            return S.Step("fail", reason="attempt_cursor_not_empty") if held else end(state, context, "rod_slot_not_offered")
        step = _go(snap, view, targets, "inventory_navigation_not_offered", a)
        if step is not None:
            if step.kind == "decision":
                return step
            return S.Step("fail", reason="attempt_cursor_not_empty") if held else end(state, context, step.reason)
        a["pending"] = a["phase"]
        step = _press(snap, button)
        if step.kind == "fail" and held:
            return S.Step("fail", reason="attempt_cursor_not_empty")
        return step
    if held:
        return S.Step("fail", reason="attempt_cursor_not_empty")
    close = S._menu_close(snap)
    if close is None:
        return S.Step("fail", reason="rod_menu_not_closable")
    a["phase"] = "closed"
    return S._decision(close)
