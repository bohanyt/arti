"""W1 gap B5: bounded Mines floor survey (S15) and the S23 ore objective.

Pure planning over the runtime's fresh, identity-fenced native facts (``_skill_wave1_context``). Nothing here
publishes input, creates an offer, spawns a ladder/loot or credits a kill. Targets come only from native scene
facts, the native nav grid ('s' = breakable stone) and native approach/tool offers; no coordinate literal lives
here. Existing drivers own every effect: tool_work break_rock (removal), approach_target (drop movement), the
ladder/extras rule (descent) and the mines exit controller (alive exit).
"""
from collections.abc import Mapping
import re
import time

import arti_stardew_skills as S

# ---------------------------------------------------------------------------- shared: reachability + survey

SURVEY_MOVES_MAX = 24          # observed-open walking inputs per floor visit before exhaustion may be declared
SURVEY_DESTINATIONS_MAX = 6    # distinct native stone cells surveyed per floor visit
SURVEY_STUCK_MAX = 3           # our own moves with an unchanged fresh tile before a destination is dropped


# W1 gap S15b (journal-attempts15 L548, report steps 54/58): one max_s=4 chase batch had 2 swings / 13 steps
# and the targeted GreenSlime fell 24 -> 18 HP, yet "no-effect swings" abandoned combat for the floor visit;
# the slimes then stayed within 0.3-1.8 tiles and took ARTI 100 -> 59. Only a stationary-scale no-kill swing
# total (one full 4 s batch at the 24-tick cadence = 11 swings; s15w2 batches were 72/76) gives up the floor.
NO_EFFECT_SWINGS_MIN = 11


# W1 gap S15c (gap4b S15 report steps 155-191): on UndergroundMine4 every HP loss (100 -> 94 -> 84 -> 80 -> 70
# -> 50) came with a GreenSlime at 0.2-1.1 tiles; with the nearest monster at >= 5.1 tiles HP never fell. The
# UM3 slimes kept their tiles for the whole floor and the UM4 slimes "closed" 5.1 -> 1.1 tiles only while ARTI
# itself walked 4 tiles toward them. A monster in contact is the threat; one several tiles away cannot reach a
# meal before the in-meal abort (monster arriving in this radius or any HP loss) ends it.
CONTACT_RADIUS_TILES = 1.5     # the reflex sword reach (CombatReflex.Reach 1.35) plus half a tile of drift
EAT_SAFE_RADIUS_TILES = CONTACT_RADIUS_TILES  # native eat-site guards still own the actual offered press
# GAP7B L3437-3448: two zero-swing batches lost 25 HP before exit at 58;
# the exit approach/selection took another ~30 s, ending at the knockout boundary.
SURVIVAL_HP_PCT = 80
# After the floor's combat gave up (s15_no_effect_swings), mining resumed beside both slimes and lost 50 HP in
# ~25 report steps while the two reflex batches themselves held HP at 100 (journal L1570/L1585 hp_min 100).
# A monster in contact re-arms the bounded combat objective at most this many times per floor visit.
DEFEND_REARMS_MAX = 2


def _live_within(combat, radius):
    for m in (combat or {}).get("monsters") or () if isinstance(combat, Mapping) else ():
        if (isinstance(m, Mapping) and isinstance(m.get("distance"), (int, float)) and not isinstance(m["distance"], bool)
                and m["distance"] <= radius and not (type(m.get("hp")) is int and m["hp"] <= 0)):
            return True
    return False


def eat_unsafe(combat):
    """W1 gap S15b: never eat beside a monster. Journal L850-L971: slimes on the offered eat-site cells (the mod's
    EatSiteMiss character rule) kept removing the eat target for 12 stages while HP fell 59 -> 11.
    GAP7B: only contact blocks the meal; native sites and in-meal HP-loss abort still apply."""
    return "unsafe:monster_in_radius" if _live_within(combat, EAT_SAFE_RADIUS_TILES) else None


def survival_low(combat):
    """S15's early meal/exit reserve, also used by the native reflex batch."""
    from arti_stardew_combat import idle_hp_floor
    return idle_hp_floor(combat, SURVIVAL_HP_PCT) is not None


# The pre-GAP7B survival floor. Between it and SURVIVAL_HP_PCT a native down-ladder is still an escape.
DESCEND_HP_PCT = 60


def descend_first(combat, level, ladder):
    """GAP8B J2 L3036-L3078: on UndergroundMine4 (kill recorded on UM3) ARTI was walking to the offered down-ladder
    when a GreenSlime in contact took HP to 75; the 80% reserve refused the meal and turned to the up-ladder, so
    floor 5 was abandoned one ladder away. A native descent lands beside the next floor's up-ladder (a fresh exit),
    away from this floor's monsters. In the 60-80% band, with a monster in contact (the meal is unsafe), an offered
    or already-pinned down-ladder below floor 5 is taken instead of the walk back; below 60% the exit is unchanged."""
    from arti_stardew_combat import idle_hp_floor
    return bool(ladder and 0 < level < 5 and survival_low(combat)
                and idle_hp_floor(combat, DESCEND_HP_PCT) is None and eat_unsafe(combat) is not None)


# GAP8E J2 (UndergroundMine3): a GreenSlime reached contact while ARTI mined (report steps 344/347, 2.0 -> 0.3 tiles,
# HP 89 -> 85). The S15 reflex batch ran with hp_floor_pct 80: after one hit it turned to its greedy retreat
# (no BFS), which got stuck (L3354 Aborted(stuck) retreat_stuck:hp_floor, 456 ticks, 2 swings, HP 85 -> 66) with the
# slime at 13/24 HP still in contact. The meal was then refused in contact (L3359) and the walk to the up-ladder
# (L3375-L3546, ~25 s, 10.56 px per 10-tick press = 1/5 speed: Slimed) lost the remaining 61 HP; L3569 hp 0.
# Measured contact damage ~2.5 HP/s (85->66 in 7.6 s; 61->0 in ~25 s); a native slime kill cost ~11 HP (UM2 L1099,
# report steps 159-166, 100 -> 89). Walking away from a slime in contact is the expensive option; fighting it is
# the cheap one while the reflex visibly hurts it. The fight floor is the reflex's own default floor (the same
# admission term Python's monster_entries / weapon_stage_unsafe already use).
FIGHT_FLOOR_PCT = 40
CONTACT_DEFENDS_MAX = 4     # contact defences started per floor visit


def _monster_hp(combat):
    return {str(m["id"]): m.get("hp") for m in (combat or {}).get("monsters") or ()
            if isinstance(m, Mapping) and m.get("id") is not None}


def defend_progress(base, combat):
    """A settled reflex batch hurt or removed (killed: the floor's monster list is complete) a monster seen at the
    defence's start. GAP4B (s15c) UM4: 14 swings left both slimes at 30 HP -- no progress, so no more defence."""
    now = _monster_hp(combat)
    return any(i not in now or (type(h) is int and type(now[i]) is int and now[i] < h) for i, h in base.items())


def contact_defend(state, combat, location):
    """'start' / 'hold' (fight the monster in contact) or None (the meal / exit rules decide).

    Only between FIGHT_FLOOR_PCT and the survival reserve callers apply this (below the reserve, contact). One
    defence per contact episode, CONTACT_DEFENDS_MAX per floor visit; after every settled S15 reflex batch
    (``mines_reflex_batches``) the defence must show progress, else it is ineffective for the floor visit."""
    from arti_stardew_combat import idle_hp_floor
    # ADR 146: once escape owns the run, contact cannot turn it back into a fight.
    if state.get("wave1_survival_exit") or state.get("wave1_combat_timed_out"):
        return None
    if not _live_within(combat, CONTACT_RADIUS_TILES) or idle_hp_floor(combat, FIGHT_FLOOR_PCT) is not None:
        return None
    batches = state.get("mines_reflex_batches", 0)
    d = state.get("mines_defend")
    if not isinstance(d, dict) or d.get("location") != location:
        d = state["mines_defend"] = dict(location=location, count=0, base=None, batches=batches, active=False,
                                         ineffective=False)
    if d["ineffective"]:
        return None
    if d["base"] is not None and batches > d["batches"]:
        if not defend_progress(d["base"], combat):
            d.update(ineffective=True, active=False)
            return None
        d.update(base=_monster_hp(combat), batches=batches)
    if d["active"]:
        return "hold"
    last = state.get("mines_last_batch") or {}
    if (last.get("batches") == batches and last.get("swings") == 0 and not last.get("killed")
            and last.get("location") == location):
        # GAP7B J2 L3437/L3444: the batch that just settled in this contact swung zero times (RockCrab chases,
        # 88 -> 74 -> 63): no evidence the fight works -> the meal / exit rules decide, as before.
        return None
    if d["count"] >= CONTACT_DEFENDS_MAX:
        return None
    d.update(count=d["count"] + 1, active=True, base=_monster_hp(combat), batches=batches)
    return "start"


def combat_due(state, combat, level):
    """Which combat leg (if any) the Mines skill owes now: "chase", "defend", "rearm", "exhausted" or None.

    Before the floor-visit kill requirement is met, any monster inside the reflex radius is chased (unchanged).
    W1 gap S15c: once a kill is recorded and the floor is below 5, a monster is fought only in contact; the
    skill otherwise keeps mining for the ladder. After the floor's combat gave up, a monster in contact re-arms
    it (bounded by DEFEND_REARMS_MAX per floor visit) instead of mining beside it. #316: contact that persists
    after the last re-arm is "exhausted": the caller leaves (offered ladder, else exit), never a rock or survey."""
    from arti_stardew_combat import command as combat_command, REFLEX_RADIUS_MARGIN
    contact = _live_within(combat, CONTACT_RADIUS_TILES)
    if state.get("wave1_combat_timed_out"):
        if not contact:
            return None
        return "rearm" if state.get("wave1_defend_rearms", 0) < DEFEND_REARMS_MAX else "exhausted"
    if state.get("wave1_kills", 0) > 0 and level < 5:
        return "defend" if contact else None
    # The pre-S15c chase predicate (distance only, no HP filter) is kept byte-for-byte.
    return "chase" if any(isinstance(m, Mapping) and isinstance(m.get("distance"), (int, float)) and
                          m["distance"] <= combat_command.REFLEX_COMBAT_RADIUS_TILES_MAX - REFLEX_RADIUS_MARGIN
                          for m in (combat or {}).get("monsters") or ()) else None


def _cardinal(tile):
    x, y = tile
    return ((x, y + 1), (x - 1, y), (x, y - 1), (x + 1, y))


def ref_tile(ref):
    """The native tile inside an object ref ``obj:<location>:<x>,<y>:<qid>`` (None otherwise)."""
    m = re.fullmatch(r"obj:[^:]+:(\d+),(\d+):.*", str(ref or ""))
    return (int(m[1]), int(m[2])) if m else None


def ref_tile_at(ref, location):
    """An object ref's tile only on the current floor; removal history spans floors."""
    return ref_tile(ref) if str(ref or "").startswith(f"obj:{location}:") else None


def stand_path(parents, grid, tile):
    """Shortest observed-open path from the actor to a cardinal stand of ``tile`` (None when unobserved/closed)."""
    from arti_stardew_goal_routes import open_path
    paths = [p for stand in _cardinal(tile) if grid.get(stand) == "." and (p := open_path(parents, stand)) is not None]
    return min(paths, key=lambda p: (len(p), p[-1])) if paths else None


def rank_reachable(rows, here, grid):
    """Order (ref, tile) candidates: an observed-open stand path first (shortest), then Manhattan distance.

    Without a native grid this is the previous nearest-first order. Never removes a candidate.
    """
    from arti_stardew_goal_routes import open_paths
    parents = open_paths(grid, here) if grid and here is not None else {}

    def key(row):
        ref, tile = row
        manhattan = abs(here[0] - tile[0]) + abs(here[1] - tile[1])
        path = stand_path(parents, grid, tile) if parents else None
        return (0, len(path), manhattan, ref) if path is not None else (1, manhattan, 0, ref)
    return sorted(rows, key=key)


# FINAL B5 time budget. A rock's native ladder roll does not depend on WHICH stone breaks (MineShaft.checkStoneForItems:
# 0.02 + 1/stonesLeft + luck, per break), but floors 2-4 also get one ladder at a random clear tile when the level is
# generated (MineShaft.populateLevel: level > 1, level % 5 != 0, 95%, at a random tile that must be clear). Before the
# wider-ladder repair the mod offered down-ladders only within Chebyshev 12, and nearest-first rock
# work keeps ARTI beside the arrival: B5 UndergroundMine2 spent 16 rocks inside x 4-13 / y 5-9 and found none; B3's
# UndergroundMine2 (its own random contents) showed a ladder at 12,21 only after ARTI had walked to 10,13. Among offered stones within EXPLORE_DETOUR_CELLS of the nearest observed-open stand path, prefer
# the one whose stand brings the most never-scanned native floor cells into that radius. Same rock count per ladder,
# a moving scan window; ties and no-new-cell floors keep the plain nearest order.
SCAN_RADIUS_TILES = 12          # ordinary object/ore scan; ladders now use 40, frontier search still helps ore
EXPLORE_DETOUR_CELLS = 3        # extra observed-open walking a frontier stone may cost over the nearest one
SEEN_TILES_MAX = 256            # ARTI tiles remembered per floor visit for the scanned-area estimate
EXPLORE_MIN_CELLS = 2 * SCAN_RADIUS_TILES + 1  # a detour must bring at least one full new scan row/column of floor


def seen_mark(state, location, here):
    """Remember ARTI's tile for this floor visit (the centre of every native scan made here)."""
    v = state.get("mines_seen")
    if not isinstance(v, dict) or v.get("location") != location:
        v = state["mines_seen"] = dict(location=location, tiles=[])
    if here is not None and list(here) not in v["tiles"] and len(v["tiles"]) < SEEN_TILES_MAX:
        v["tiles"].append(list(here))
    return v


def _scanned(grid, tiles):
    r, out = SCAN_RADIUS_TILES, set()
    for x, y in tiles:
        out.update(c for c in ((a, b) for a in range(x - r, x + r + 1) for b in range(y - r, y + r + 1)) if c in grid)
    return out


def rank_frontier(rows, here, grid, state, location):
    """``rank_reachable`` with a bounded frontier preference (see SCAN_RADIUS_TILES). Never removes a candidate."""
    from arti_stardew_goal_routes import open_paths
    ranked = rank_reachable(rows, here, grid)
    if not grid or here is None or not ranked:
        return ranked
    parents = open_paths(grid, here)
    paths = {ref: p for ref, tile in ranked if (p := stand_path(parents, grid, tile)) is not None}
    if not paths:
        return ranked
    nearest = min(len(p) for p in paths.values())
    scanned = _scanned(grid, [tuple(t) for t in seen_mark(state, location, here)["tiles"]])
    floor = {c for c, ch in grid.items() if ch in (".", "s")} - scanned
    if not floor:
        return ranked
    r = SCAN_RADIUS_TILES

    def fresh(ref):
        sx, sy = paths[ref][-1]
        return sum(1 for x, y in floor if abs(x - sx) <= r and abs(y - sy) <= r)
    near = [(ref, tile) for ref, tile in ranked if ref in paths and len(paths[ref]) <= nearest + EXPLORE_DETOUR_CELLS]
    gain = {ref: fresh(ref) for ref, _ in near}
    best = max(gain.values(), default=0)
    if best < EXPLORE_MIN_CELLS:
        return ranked
    pick = next((ref, tile) for ref, tile in near if gain[ref] == best)
    return [pick] + [row for row in ranked if row != pick]


def explore_floor(level, skill):
    """S15 explores only where a generation ladder can exist (floors 2-4); S23 everywhere (ore nodes come into view)."""
    return skill == "tambang_bijih" or (1 < level < 5)


def survey_visit(state, location):
    s = state.get("mines_survey")
    if not isinstance(s, dict) or s.get("location") != location:
        s = state["mines_survey"] = dict(location=location, moves=0, destinations=0, done=[], dest=None,
                                         frm=None, stuck=0, arrived=None)
    return s


def survey_step(snap, state, context, location, candidates):
    """One bounded observed-open move toward the nearest native candidate cell, or None (survey exhausted).

    ``candidates``: native tiles that are not offered now (scene stone facts / nav-grid 's' cells / ore nodes).
    A destination is dropped for this floor visit when its stand is reached and a fresh read still offers
    nothing there, or after SURVEY_STUCK_MAX of our own moves left the fresh tile unchanged.
    """
    from arti_stardew_goal_routes import open_paths
    here = S._wave1_xy(S._g(S._g(snap, "motor"), "tile"))
    grid = context.get("nav_grid") or {}
    if here is None or not grid or not context.get("ui_complete"):
        return None
    s = survey_visit(state, location)
    if s["frm"] is not None:
        s["stuck"] = s["stuck"] + 1 if s["frm"] == list(here) else 0
        s["frm"] = None
        if s["stuck"] >= SURVEY_STUCK_MAX and s["dest"] is not None:
            s["done"].append(s["dest"])
            s.update(dest=None, stuck=0)
    if s["moves"] >= SURVEY_MOVES_MAX:
        return None
    parents = open_paths(grid, here)
    if not parents:
        return None
    done = {tuple(t) for t in s["done"]}
    cells = sorted({tuple(t) for t in candidates} - done)
    if s["dest"] is not None and tuple(s["dest"]) not in cells:
        s.update(dest=None, stuck=0)
    if s["dest"] is not None and abs(here[0] - s["dest"][0]) + abs(here[1] - s["dest"][1]) == 1:
        # Arrived beside the destination. One fresh read may still publish its native offer.
        if s["arrived"] != [list(here), list(s["dest"])]:
            s["arrived"] = [list(here), list(s["dest"])]
            return S.Step("decision", dict(S.INSPECT, question="skill mines: read the surveyed stone"))
        s["done"].append(s["dest"])
        s.update(dest=None, stuck=0)
        return survey_step(snap, state, context, location, candidates)
    if s["dest"] is None:
        if s["destinations"] >= SURVEY_DESTINATIONS_MAX:
            return None
        reachable = [(len(p), cell) for cell in cells
                     if abs(here[0] - cell[0]) + abs(here[1] - cell[1]) > 1
                     and (p := stand_path(parents, grid, cell)) is not None]
        if not reachable:
            return None
        s["dest"] = list(min(reachable)[1])
        s["destinations"] += 1
        s["arrived"] = None
    path = stand_path(parents, grid, tuple(s["dest"]))
    if path is None or len(path) < 2:
        s["done"].append(s["dest"])
        s.update(dest=None, stuck=0)
        return survey_step(snap, state, context, location, candidates)
    delta = (path[1][0] - here[0], path[1][1] - here[1])
    run = 1  # at most three observed-open cells in one straight hold; the next fresh read re-plans
    while run < 3 and run + 1 < len(path) and (path[run + 1][0] - path[run][0], path[run + 1][1] - path[run][1]) == delta:
        run += 1
    ticks = 8 if len(path) == 2 else S.WALK_TICKS_PER_STEP * run
    # B6 J2745-2775: fixed holds crossed the turn in both directions (2,7 <-> 4,7).
    # Reuse the pixel-sized clear walk; three returns to a pose abandon this destination.
    walk = s.setdefault("walk", {})
    visits = walk.setdefault("visits", {})
    key = (tuple(s["dest"]), here)
    pixel = S._g(S._g(snap, "motor"), "pixel")
    poses = walk.setdefault("poses", {})
    if pixel is not None and key in poses and poses[key] != tuple(pixel):
        visits[key] = 0  # slow movement within a tile is progress, not an identical failure
    if visits.get(key, 0) >= SURVEY_STUCK_MAX:
        s["done"].append(s["dest"])
        s.update(dest=None, stuck=0)
        return survey_step(snap, state, context, location, candidates)
    aimed = S._clear_walk_ticks(walk, S._g(snap, "motor"), here, path[run], delta, ticks)
    if aimed is not None:
        ticks = aimed
    visits[key] = visits.get(key, 0) + 1
    if pixel is not None:
        poses[key] = tuple(pixel)
    walk["clear_walk"] = (dict(pixel=list(pixel), direction=S._DIRS[delta], ticks=ticks)
                          if aimed is not None else None)
    s["moves"] += 1
    s["frm"] = list(here)
    return S._decision(S.act("world_key.move_cardinal", {"direction": S._DIRS[delta], "ticks": ticks}))


def survey_summary(state):
    s = state.get("mines_survey") or {}
    return dict(survey_moves=s.get("moves", 0), survey_destinations=s.get("destinations", 0),
                survey_dropped=len(s.get("done", ())))


def stone_cells(context, state, location, offered_tiles):
    """Survey candidates for S15: native scene stones and nav-grid 's' cells, minus this visit's exclusions."""
    grid = context.get("nav_grid") or {}
    excluded = _closed_stones(state, location)
    cells = {cell for cell, c in grid.items() if c == "s"}
    for ref, f in context.get("targets", {}).items():
        if (isinstance(f, Mapping) and f.get("kind") == "object" and f.get("name") == "Stone"
                and (tile := S._wave1_xy(f.get("tile"))) is not None and grid.get(tile) != "."):
            cells.add(tile)
    return cells - excluded - set(offered_tiles)


def listed_downs(context, location=None):
    """Every native down the game lists on this floor, including ones this visit already refused."""
    from arti_runtime.mines_down import listed_downs as native
    seen, loc = {}, str(location or "")
    for e in native(context, location):
        seen[e.get("ref") or id(e)] = e
    for e in context.get("extras") or ():
        if not isinstance(e, Mapping) or e.get("kind") not in ("ladder_down", "shaft"):
            continue
        ref = str(e.get("ref", e.get("source_fact_id") or ""))
        if loc.startswith("UndergroundMine") and ref and not (
                ref.startswith("xt:mine:" + loc + ":") or ref.startswith("xt:ladder_down:" + loc + ":")
                or ref.startswith("xt:shaft:" + loc + ":")):
            continue
        seen.setdefault(ref or id(e), e)
    return list(seen.values())


def ladder_entries(context, state):
    """Offered native down-ladders not refused during this floor visit."""
    refused = set(state.get("wave1_ladder_refused", ()))
    return [e for e in context.get("extras", ()) if isinstance(e, Mapping) and e.get("kind") in ("ladder_down", "shaft")
            and str(e.get("ref", e.get("source_fact_id"))) not in refused]


# B5: 7.2 rocks per ladder, 5.1 wall s/rock; ~0.75 wall s per walked cell.
# A path estimate chooses work only. The ordinary native approach/press still owns every descent.
LADDER_SEARCH_SECONDS = 7.2 * 5.1
WALK_CELL_SECONDS = .75


def ladder_choice(snap, context, state, here):
    """Cheapest observed stand path versus another expected rock search; unknown paths grant no shortcut.

    A far/blocked ladder loses to a reachable offered stone when its walk exceeds that stone's
    walk plus the measured expected search. With no reachable stone, retain native approach's
    bounded partial-window discovery rather than infer that the ladder is unreachable.
    """
    from arti_stardew_goal_routes import open_paths
    from arti_stardew_native_targets import work_tiles
    entries = ladder_entries(context, state)
    if not entries:
        return None
    grid = context.get("nav_grid") or {}
    parents = open_paths(grid, here) if here is not None and grid and len(grid) <= 4096 else {}
    walks = {}
    for cell, parent in parents.items():
        walks[cell] = 0 if parent is None else walks[parent] + 1
    def cost(entry):
        stands = work_tiles(entry)
        exact = min((walks[s] for s in stands if s in walks), default=float("inf"))
        if exact != float("inf"):
            return exact * WALK_CELL_SECONDS
        # A 40-tile offer can outlive the smaller current grid. Price only an improving
        # observed-open prefix plus an explicitly heuristic unseen tail. The existing approach
        # must reobserve that tail; known blocked/disconnected stands never get this estimate.
        distance = lambda a, b: abs(a[0]-b[0]) + abs(a[1]-b[1])
        partial = min((walks[p] + distance(p, s) for s in stands if s not in grid
                       for p in walks if distance(p, s) < distance(here, s)), default=float("inf"))
        return partial * WALK_CELL_SECONDS
    ranked = sorted(entries, key=lambda e: (cost(e), str(e.get("ref", ""))))
    offered = {e.get("ref") for e in S._g(snap, "approach_targets", ()) or () if isinstance(e, Mapping)}
    offered.add(S._g(snap, "tool_work_target"))
    stones = [dict(f, ref=ref) for ref, f in context.get("targets", {}).items()
              if isinstance(f, Mapping) and f.get("kind") == "object" and f.get("name") == "Stone"
              and ref in offered and ref not in state.get("wave1_removed", ())
              and ref not in state.get("wave1_unreachable", ()) and grid.get(S._wave1_xy(f.get("tile"))) != "."]
    rock_walk = min((cost(e) for e in stones), default=float("inf"))
    return ranked[0] if rock_walk == float("inf") or cost(ranked[0]) <= rock_walk + LADDER_SEARCH_SECONDS else None


def descend(state, entry=None):
    pin = dict(ref=entry.get("ref", entry.get("source_fact_id")), tile=_ladder_tile(entry)) if entry else {}
    return S.wave1_repin(state, "turun tangga", "ladder", "observed_extras_effect", **pin)


def ladder_refused(state, extras):
    """A ladder leg whose native interaction was never offered: exclude only this floor visit's ladders."""
    pin = state.get("wave1_pin", {}).get("ref")
    refs = [str(e.get("ref", e.get("source_fact_id"))) for e in extras
            if isinstance(e, Mapping) and e.get("kind") == "ladder_down"
            and (not pin or e.get("ref", e.get("source_fact_id")) == pin)]
    known = state.setdefault("wave1_ladder_refused", [])
    fresh = [r for r in refs if r not in known]
    if not fresh:
        return False
    known.extend(fresh)
    # FINAL B3: the floor's verified breaks at refusal time; a later dig may reopen it (ladder_reopen).
    state["wave1_ladder_refused_breaks"] = state.get("wave1_count", 0)
    return True


# FINAL B3 (J L4148-L4195): UndergroundMine2's native ladder appeared at 12,21 while ARTI stood at 12,14 behind a row
# of offered stones; the approach found "no improving candidate" (LOCAL_EVIDENCE_LIMIT), the ladder was refused for
# the floor visit and S15 never reached it. Breakable stones ('s') are the only cells S15/S23 may open, so a refused
# ladder is dug toward: the first stone on the cheapest observed path (stones cost about one rock's inputs in cells).
DIG_STONE_CELLS = 6           # S15_FIX2: ~6.5 inputs per rock vs ~1 input per walked cell
LADDER_REOPENS_MAX = 2        # re-offers of one refused ladder per floor visit, only after a verified dig opened a path


def _ladder_tile(e):
    return S._wave1_xy((e.get("x"), e.get("y"))) if isinstance(e, Mapping) else None


def _refused_ladders(context, state):
    refused = set(state.get("wave1_ladder_refused", ()))
    return [(str(e.get("ref", e.get("source_fact_id"))), t) for e in context.get("extras", ())
            if isinstance(e, Mapping) and e.get("kind") == "ladder_down"
            and str(e.get("ref", e.get("source_fact_id"))) in refused and (t := _ladder_tile(e)) is not None]


def dig_stone(grid, here, tile, closed=()):
    """The first breakable-stone cell on the cheapest observed path to a cardinal stand of ``tile``.

    Open cells cost 1, native 's' cells DIG_STONE_CELLS; every other / unobserved cell is closed. None when no
    such path exists or the cheapest one is already stone-free (an open path: nothing to dig)."""
    import heapq
    here = tuple(here)
    if not grid or len(grid) > 4096 or here not in grid:
        return None
    goals = {s for s in _cardinal(tile) if grid.get(s) in (".", "s") and s not in closed}
    cost, parents, todo = {here: 0}, {here: None}, [(0, here)]
    end = None
    while todo:
        c, p = heapq.heappop(todo)
        if c != cost[p]:
            continue
        if p in goals:
            end = p
            break
        for n in ((p[0], p[1] - 1), (p[0] - 1, p[1]), (p[0] + 1, p[1]), (p[0], p[1] + 1)):
            cell = grid.get(n)
            step = 1 if cell == "." else DIG_STONE_CELLS if cell == "s" and n not in closed else None
            if step is not None and c + step < cost.get(n, float("inf")):
                cost[n], parents[n] = c + step, p
                heapq.heappush(todo, (c + step, n))
    path = []
    while end is not None:
        path.append(end)
        end = parents[end]
    return next((cell for cell in reversed(path) if grid.get(cell) == "s"), None)


def _closed_stones(state, location):
    return {t for ref in list(state.get("wave1_removed", ())) + list(state.get("wave1_unreachable", ()))
            if (t := ref_tile_at(ref, location)) is not None}


def ladder_dig(context, state, here, location):
    """The stone cell to break toward a refused native ladder this floor visit (None: nothing to dig)."""
    grid = context.get("nav_grid") or {}
    if here is None or not grid:
        return None
    closed = _closed_stones(state, location)
    cells = [d for _, t in _refused_ladders(context, state) if (d := dig_stone(grid, here, t, closed)) is not None]
    return min(cells, key=lambda d: (abs(d[0] - here[0]) + abs(d[1] - here[1]), d)) if cells else None


def ladder_reopen(context, state, here):
    """Re-offer a refused ladder once a verified break since its refusal opened an observed path to its stand.

    Bounded by LADDER_REOPENS_MAX per ladder and floor visit; no ladder, offer or descent is manufactured."""
    from arti_stardew_goal_routes import open_paths
    grid = context.get("nav_grid") or {}
    if (here is None or not grid or not state.get("wave1_ladder_refused")
            or state.get("wave1_count", 0) <= state.get("wave1_ladder_refused_breaks", 0)):
        return False
    parents = open_paths(grid, here)
    reopens = state.setdefault("wave1_ladder_reopens", {})
    opened = False
    for ref, tile in _refused_ladders(context, state):
        if reopens.get(ref, 0) < LADDER_REOPENS_MAX and stand_path(parents, grid, tile) is not None:
            reopens[ref] = reopens.get(ref, 0) + 1
            state["wave1_ladder_refused"].remove(ref)
            opened = True
    return opened


def dig_choice(snap, state, context, location, stones, here):
    """(ref, tile) of an offered stone on the dig path to a refused ladder, a survey step toward it, or None."""
    dig = ladder_dig(context, state, here, location)
    if dig is None:
        return None
    for ref, tile in stones:
        if tile == dig:
            return ref, tile
    return survey_step(snap, state, context, location, [dig])


def ladder_rock_pinned(state):
    """S23's current rock leg is a ladder-search stone, never the ore node."""
    return bool(state.get("ore_ladder_rock")) and state.get("wave1_pin", {}).get("ref") == state["ore_ladder_rock"]


def exit_route_step(snap, grid, entries, state, *, combat=None):
    """B6 alternate: walk an observed-open path around an obstacle to a native up-ladder stand.

    Caller fences settled input and identity. No stand/press/arrival is invented; no rock/barrel is treated
    as open. One settled move with no pixel progress excludes that edge on the next read.
    The parent's unchanged forty-step exit bound pays for every proposal and re-read.
    """
    from arti_stardew_goal_routes import open_path
    from collections import deque
    from arti_stardew_world_extras import stand_tiles
    motor = S._g(snap, "motor")
    here = S._wave1_xy(S._g(motor, "tile"))
    if (here is None or not grid or S._g(motor, "status") != "current"
            or S._g(motor, "is_player_free") is not True or S._g(motor, "can_player_move") is not True):
        return S.Step("decision", dict(S.INSPECT, question="mines exit: read the alternate route"))
    stands = {tuple(s) for e in entries for s in (stand_tiles(e) or ())}
    if here in stands:
        return None  # native faced capture / plain-hand staging must supply the press
    from math import hypot
    monsters = [m for m in (combat or {}).get("monsters", ())
                if not (type(m.get("hp")) is int and m["hp"] <= 0)]
    threats = [S._wave1_xy(m.get("tile")) for m in monsters]
    if any(t is None for t in threats):
        return S.Step("fail", reason="mines_exit_threat_geometry_unknown")
    separation = lambda cell: min((hypot(cell[0]-t[0], cell[1]-t[1]) for t in threats), default=float("inf"))
    safe_edge = lambda a, b: (separation(b) > CONTACT_RADIUS_TILES
                             if separation(a) > CONTACT_RADIUS_TILES else separation(b) >= separation(a))
    visits, excluded = state.setdefault("visits", {}), state.setdefault("excluded", [])
    edges = state.setdefault("excluded_edges", [])
    poses, pixel = state.setdefault("poses", {}), S._g(motor, "pixel")
    for cell, count in visits.items():
        if pixel is not None and cell[0] == here and cell in poses and poses[cell] != tuple(pixel):
            visits[cell] = 0
            continue
        if count >= 1 and cell[0] == here and list(cell[1]) not in excluded:
            excluded.append(list(cell[1]))
        if count >= 1 and cell[0] == here and [list(cell[0]), list(cell[1])] not in edges:
            edges.append([list(cell[0]), list(cell[1])])
    # A refused edge does not close the destination cell from another side.
    # In particular, the sole native stand can still be reached around a corner.
    parents, todo = {here: None}, deque([here])
    threat_blocked = False
    while todo:
        cell = todo.popleft()
        for dx, dy in S._DIRS:
            nxt = cell[0] + dx, cell[1] + dy
            if grid.get(nxt) == "." and not safe_edge(cell, nxt):
                threat_blocked = True
            if (nxt not in parents and grid.get(nxt) == "."
                    and safe_edge(cell, nxt)
                    and [list(cell), list(nxt)] not in edges):
                parents[nxt] = cell
                todo.append(nxt)
    paths = [p for stand in stands if (p := open_path(parents, stand)) is not None and len(p) > 1]
    if not paths and separation(here) <= CONTACT_RADIUS_TILES:
        # ADR 174: in contact with no guarded path up, break contact first: one observed-open step that
        # increases separation, nearest-to-a-stand first. Standing still is the one move known to cost HP.
        flee = [(separation(n), -min((abs(n[0]-s[0]) + abs(n[1]-s[1]) for s in stands), default=0), n)
                for dx, dy in S._DIRS for n in [(here[0] + dx, here[1] + dy)]
                if grid.get(n) == "." and separation(n) > separation(here) and [list(here), list(n)] not in edges]
        if flee:
            nxt = max(flee)[2]
            paths = [[here, nxt]]
            state["contact_flee"] = state.get("contact_flee", 0) + 1
    if not paths:
        # ADR 169: a rejected edge beside a remote monster is not evidence
        # that it cuts the exit. Preserve debris repair when even the
        # unguarded open-cell graph cannot reach a native stand.
        if threat_blocked:
            from arti_stardew_goal_routes import open_paths
            unguarded = open_paths(grid, here)
            threat_blocked = any(stand in unguarded for stand in stands)
        return S.Step("fail", reason="mines_exit_monster_corridor" if threat_blocked else "mines_exit_failed")
    path = min(paths, key=lambda p: (len(p), p[-1]))
    nxt = path[1]
    delta = (nxt[0] - here[0], nxt[1] - here[1])
    # Small, pixel-sized holds cannot cross two cells at an off-centre turn (B6 survey oscillation).
    ticks = S._clear_walk_ticks(state, motor, here, nxt, delta, 8)
    if ticks is None:
        return S.Step("decision", dict(S.INSPECT, question="mines exit: read the current pixel pose"))
    visits[(here, nxt)] = visits.get((here, nxt), 0) + 1
    poses[(here, nxt)] = tuple(pixel)
    state["clear_walk"] = dict(pixel=list(S._g(motor, "pixel")), direction=S._DIRS[delta], ticks=ticks)
    return S._decision(S.act("world_key.move_cardinal", dict(direction=S._DIRS[delta], ticks=ticks)))


# ---------------------------------------------------------------------------- S23 tambang_bijih

# Native ore-node object ids -> the ore item their breakStone path drops (decomp GameLocation.breakStone:
# "751"/"849" -> (O)378, "290"/"850" -> (O)380, "764" -> (O)384, "765" -> (O)386). Item identities, not tiles.
ORE_NODES = {"(O)751": "(O)378", "(O)849": "(O)378", "(O)290": "(O)380", "(O)850": "(O)380",
             "(O)764": "(O)384", "(O)765": "(O)386"}
ORE_DESCENTS_MAX = 3      # native ladders S23 may take while no ore node is observed
ORE_PICKUP_LEGS = 6       # approach legs onto observed ore drops
ORE_PICKUP_WAIT_S = 25.0  # drops settle / magnet into the bag (> the runtime's own 20 s pickup bound)
ORE_STACK_MAX = 999
WINDOW_DROPS_MAX = 16     # GatherFeedback.MaxDrops: a full list may be truncated (arti_stardew_scene.gather_window)


def ore_node(ref, fact):
    """(node qid, ore qid) when a native scene fact is an ore node, else None. Ambiguity is no identity."""
    if not isinstance(fact, Mapping) or fact.get("kind") != "object" or fact.get("name") != "Stone":
        return None
    suffix = str(ref).rsplit(":", 1)[-1] if str(ref).startswith("obj:") else None
    qid = fact.get("qualified_item_id")
    if qid is not None and suffix is not None and suffix.startswith("(") and qid != suffix:
        return None
    qid = qid if qid is not None else suffix
    return (qid, ORE_NODES[qid]) if qid in ORE_NODES else None


# FINAL B7 (J2078-J2512): S23 saw no ore node on floors 1-3. The scene publishes only the 40 nearest objects
# (SceneObservationCaps.Objects), which on a dense mine floor is a few tiles around ARTI; the third descent spent
# ORE_DESCENTS_MAX and UndergroundMine3 failed `ore_node_not_observed` on arrival (14:20) without one step of
# search, with the 14:50 search clock still open. Two read-only inputs now widen what S23 can look for:
IDENTIFIED_TILES_MAX = 512    # scene-identified object tiles remembered per floor visit (not ore when seen as stone)
ORE_OFFERS_MAX = 6            # mirror of WorldExtras.MineOreOffersMax (ui.context mine_ore_nodes, <= 40 tiles)


def ore_offer_tiles(context, location):
    """The mod's read-only ``mine_ore_nodes`` (native ore objects within the mine-ladder radius, nearest first):
    survey destinations only. The ordinary scene fact / approach / tool_work offer still owns the break."""
    ui = context.get("mines_ui") or {}
    raw = ui.get("mine_ore_nodes") if isinstance(ui, Mapping) and ui.get("location_id") in (None, location) else None
    out = []
    for e in raw if isinstance(raw, (list, tuple)) else ():
        tile = S._wave1_xy((e.get("x"), e.get("y"))) if isinstance(e, Mapping) else None
        if tile is not None and e.get("qualified_item_id") in ORE_NODES and tile not in out:
            out.append(tile)
    return out[:ORE_OFFERS_MAX]


def unidentified_stone_cells(context, state, location, offered_tiles):
    """Nav-grid stone cells ('s') never published as a scene object during this floor visit: the only cells that
    can still hide an ore node. Walking toward the nearest one brings it into the nearest-40 scene window."""
    grid = context.get("nav_grid") or {}
    v = state.get("ore_identified")
    if not isinstance(v, dict) or v.get("location") != location:
        v = state["ore_identified"] = dict(location=location, tiles=[])
    known = {tuple(t) for t in v["tiles"]}
    for f in context.get("targets", {}).values():
        tile = S._wave1_xy(f.get("tile")) if isinstance(f, Mapping) and f.get("kind") == "object" else None
        if tile is not None and tile not in known and len(v["tiles"]) < IDENTIFIED_TILES_MAX:
            known.add(tile)
            v["tiles"].append(list(tile))
    return {cell for cell, c in grid.items() if c == "s"} - known - _closed_stones(state, location) - set(offered_tiles)


def ore_count(context, qid):
    return sum(f.get("stack") if type(f.get("stack")) is int else 1
               for f in context.get("inventory", ()) if f.get("qualified_item_id") == qid)


def _bag_room(context, qid):
    if any(f.get("qualified_item_id") == qid and type(f.get("stack")) is int and f["stack"] < ORE_STACK_MAX
           for f in context.get("inventory", ())):
        return True
    free = context.get("bag_free_slots")
    return None if type(free) is not int else free > 0


def _mines_menu(snap):
    key, pad = S._g(snap, "ui_key_target") or {}, S._g(snap, "ui_pad_target") or {}
    if key.get("kind") in ("question", "npc_question") or (pad.get("pins") or {}).get("context") in (
            "question", "npc_question", "event", "profession"):
        return S.Step("fail", reason="unknown_menu_choice")
    close = S._menu_close(snap)
    return S._decision(close) if close else S.Step("decision", S.INSPECT)


def ore_plan(call, snap, state, context):
    """S23: native ore node -> pickaxe tool_work removal -> ordinary movement onto observed drops ->
    fresh complete ore bag gain -> verified alive exit (the exit/alive check is the runtime's)."""
    motor = S._g(snap, "motor")
    location, t = S._g(motor, "location"), S._g(motor, "time_of_day")
    o = state.setdefault("ore", dict(phase="find"))
    floor = re.fullmatch(r"UndergroundMine(\d+)", str(location or ""))
    if not floor and location != "Mine":
        return S.Step("fail", reason="unsupported:route_to_mines")
    if type(t) is not int:
        return S.Step("fail", reason="mines_time_unknown")
    if state.get("live7"):
        # ADR 149: free play has no entry window; only the game's 02:00 pass-out bounds an entry from the lobby.
        from arti_runtime.mines_rules import CURFEW_REASON, entry_open
        if not floor and not entry_open(t, context.get("live7_travel_policy")):
            return S.Step("fail", reason=CURFEW_REASON)
    elif (not state.get("wave1_entered") or not floor) and not 900 <= t <= 1800:
        return S.Step("fail", reason="mines_time_window:enter_0900_1800")
    state["wave1_entered"] = True
    if floor:
        state["wave1_floor"] = max(state.get("wave1_floor", 0), int(floor[1]))
    if state.get("wave1_visit_location") != location:
        state.update(wave1_visit_location=location, wave1_unreachable=[], wave1_ladder_refused=[],
                     wave1_ladder_reopens={})
    vitals = context.get("combat") or {}
    if (type(vitals.get("hp")) is not int or type(vitals.get("max_hp")) is not int
            or vitals["hp"] <= 0 or vitals["max_hp"] <= 0):
        return S.Step("fail", reason="mines_vitals_unknown")
    from arti_stardew_combat import idle_hp_floor
    if idle_hp_floor(vitals) is not None and floor:
        # S23 owns no combat: a low HP floor is a survival exit, never more mining.
        state.update(wave1_hold=state.get("wave1_hold", "partial:mines_low_hp_exit"), wave1_survival_exit=True)
        return S.wave1_repin(state, "skill " + call.name, "exit", "exited_mines")
    if S.menu_up(snap):
        return _mines_menu(snap)
    if floor and (S._g(motor, "is_player_free") is False or S._g(motor, "can_player_move") is False):
        return S._wait(state, "mines_controls", "mines_controls_unavailable",
                       "skill mines: observe control release or discovery dialogue", 3)
    if o["phase"] == "pickup":
        return _ore_pickup(snap, state, context, location)
    if o["phase"] == "exit":
        return S.wave1_repin(state, "skill " + call.name, "exit", "exited_mines")
    here = S._wave1_xy(S._g(motor, "tile"))
    grid = context.get("nav_grid") or {}
    if floor:
        seen_mark(state, location, here)  # FINAL B5: the scan centres of this floor visit
    nodes = []
    for ref, f in context.get("targets", {}).items():
        ident = ore_node(ref, f)
        tile = S._wave1_xy(f.get("tile")) if isinstance(f, Mapping) else None
        if (ident is None or tile is None or grid.get(tile) == "."
                or ref in state.get("wave1_removed", ()) or ref in state.get("wave1_unreachable", ())):
            continue
        nodes.append((ref, tile, ident))
    offered = {e.get("ref") for e in S._g(snap, "approach_targets", ()) or () if isinstance(e, Mapping)}
    offered.add(S._g(snap, "tool_work_target"))
    ready = [(ref, tile) for ref, tile, _ in nodes if ref in offered]
    if floor and ready and here is not None:
        ref, tile = rank_reachable(ready, here, grid)[0]
        node_qid, ore = next(ident for r, _, ident in nodes if r == ref)
        if not context.get("inventory_complete") or not context.get("ui_complete"):
            return S._wait(state, "ore_readback", "composition_readback_unavailable",
                           "skill ore: read the bag before the node", S.WAIT_S)
        room = _bag_room(context, ore)
        if room is None:
            return S._wait(state, "ore_readback", "composition_readback_unavailable",
                           "skill ore: read the bag capacity", S.WAIT_S)
        if not room:
            return S.Step("fail", reason="ore_bag_full")
        window = context.get("local_window")
        if window is None:
            return S._wait(state, "ore_window", "ore_drop_window_unavailable",
                           "skill ore: read the nearby drops", S.WAIT_S)
        drops = window.get("drops", ())
        if any(d.get("qualified_item_id") == ore for d in drops) or (
                window.get("drops_complete") is not True and len(drops) >= WINDOW_DROPS_MAX):
            # A bag gain could not be attributed to this node; never credit older/unknown debris.
            return S.Step("fail", reason="ore_debris_preexisting_or_unknown")
        # FINAL B4 (J L2396-L2398, report steps 188/189): UndergroundMine2 offered the copper node (O)751 at 11+
        # tiles, but the window round ARTI read drops [] / drops_complete false: a RockCrab in contact (0.8 tiles,
        # HP 100 -> 95) left its damage-number debris. The mod marks the window incomplete for exactly two causes
        # (SceneSnapshotCapture): a chunk with NO item identity (numbers, stone chunks: never a bag item) or more
        # than WINDOW_DROPS_MAX item drops (truncated, so an ore drop could be unlisted). Only the second can hide
        # ore; it still fails above, as does any listed ore drop. The bag-gain verifier is unchanged.
        o["window_unlisted_chunks"] = window.get("drops_complete") is not True
        o.update(phase="remove", node=ref, node_qid=node_qid, ore=ore, floor=location, tile=list(tile),
                 bag_before=ore_count(context, ore), identity=context.get("identity"))
        state.pop("ore_ladder_rock", None)
        return S.wave1_repin(state, "pecah 1 batu", "rock", "observed_tool_effect", ref=ref, tile=tile)
    node_tiles = {node_tile for _, node_tile, _ in nodes}
    remote = ([cell for cell in ore_offer_tiles(context, location) if cell not in node_tiles and grid.get(cell) != "."]
              if floor else [])
    if floor and (nodes or remote):
        step = survey_step(snap, state, context, location,
                           [tile for ref, tile, _ in nodes if ref not in offered] + remote)
        if step is not None:
            return step
    searching = state.get("wave1_descents", 0) < ORE_DESCENTS_MAX and t < state.get("campaign_ore_search_due", 2400)
    if floor and searching:
        ladder_reopen(context, state, here)
    ladder = ladder_choice(snap, context, state, here) if floor else None
    if (location == "Mine" or ladder is not None) and searching:
        return descend(state, ladder)
    if floor and searching and here is not None:
        # FINAL B3 (J L2113-L2128): floor 1 offered only plain stones and no ladder; S23 failed at once and
        # climbed out. A mine floor's down-ladder appears only from broken stones, so S23 breaks stones for
        # it (dig toward a refused ladder first) inside its own caps and descent bound, like S15's floor work.
        # These breaks are never the ore node: no baseline, pickup or ore credit (ore_completed).
        stones = [(ref, tile) for ref, tile in (
                  (ref, S._wave1_xy(f.get("tile"))) for ref, f in context.get("targets", {}).items()
                  if isinstance(f, Mapping) and f.get("kind") == "object" and f.get("name") == "Stone"
                  and ore_node(ref, f) is None and ref in offered
                  and ref not in state.get("wave1_removed", ()) and ref not in state.get("wave1_unreachable", ()))
                  if tile is not None and grid.get(tile) != "."]
        dig = dig_choice(snap, state, context, location, stones, here)
        if dig is not None and not isinstance(dig, tuple):
            return dig
        # FINAL B5: ladder stones prefer (bounded) never-scanned floor, which also brings ore nodes into view.
        choice = dig if isinstance(dig, tuple) else rank_frontier(stones, here, grid, state, location)[0] if stones else None
        if choice is not None:
            state["ore_ladder_rock"] = choice[0]
            return S.wave1_repin(state, "pecah 1 batu", "rock", "observed_tool_effect", ref=choice[0], tile=choice[1])
        offered_tiles = {S._wave1_xy(e.get("tile")) for e in S._g(snap, "approach_targets", ()) or ()
                         if isinstance(e, Mapping) and str(e.get("ref", "")).startswith("obj:")}
        step = survey_step(snap, state, context, location, stone_cells(context, state, location, offered_tiles))
        if step is not None:
            return step
    if floor and here is not None and not searching and t < state.get("campaign_ore_search_due", 2400):
        # FINAL B7: the descents are spent but the search clock is open: survey THIS floor (the same per-visit
        # SURVEY_MOVES_MAX / SURVEY_DESTINATIONS_MAX bounds) toward the nearest stone cells the scene never
        # identified, before the floor may be declared ore-free. Seen nodes are handled above on the next read.
        offered_tiles = {S._wave1_xy(e.get("tile")) for e in S._g(snap, "approach_targets", ()) or ()
                         if isinstance(e, Mapping) and str(e.get("ref", "")).startswith("obj:")}
        step = survey_step(snap, state, context, location,
                           unidentified_stone_cells(context, state, location, offered_tiles))
        if step is not None:
            return step
    state["ore_exhaustion"] = dict(reason="ore_node_not_observed", observed_nodes=len(nodes),
                                   descents=state.get("wave1_descents", 0),
                                   search_due=not t < state.get("campaign_ore_search_due", 2400),
                                   ladder_rocks=state.get("ore_ladder_rocks", 0), **survey_summary(state))
    return S.Step("fail", reason="ore_node_not_observed")


def _ore_pickup(snap, state, context, location):
    o = state["ore"]
    if location != o.get("floor") or context.get("identity") != o.get("identity"):
        return S.Step("fail", reason="ore_identity_changed")
    if not context.get("inventory_complete") or not context.get("ui_complete"):
        return S._wait(state, "ore_readback", "composition_readback_unavailable",
                       "skill ore: read the bag", S.WAIT_S)
    now = (state.get("clock") or (lambda: state.get("_game_now", time.monotonic())))()
    o.setdefault("pickup_at", now)
    bag = ore_count(context, o["ore"])
    gained = bag - o["bag_before"]
    if gained < 0:
        return S.Step("fail", reason="ore_bag_decreased")
    window = context.get("local_window")
    drops = [d for d in (window or {}).get("drops", ()) if d.get("qualified_item_id") == o["ore"]]
    arrived = S._arrived_ref(snap)
    offers = [e for e in S._g(snap, "approach_targets", ()) or () if isinstance(e, Mapping)
              and str(e.get("ref", "")).startswith("drop:") and e.get("qualified_item_id") == o["ore"]]
    settled = window is not None and window.get("drops_complete") is True and not drops and not offers
    expired = now - o["pickup_at"] > ORE_PICKUP_WAIT_S
    if gained > 0 and (settled or expired):
        o.update(phase="exit", evidence=dict(node=o["node"], node_qid=o["node_qid"], ore=o["ore"], floor=o["floor"],
                 bag_before=o["bag_before"], bag_after=bag, gained=gained, removal_request=o.get("removal_request"),
                 drops_settled=settled))
        return S.wave1_repin(state, "skill tambang_bijih", "exit", "exited_mines")
    if expired:
        return S.Step("fail", reason="ore_pickup_not_observed")
    target = next((e for e in offers if e.get("ref") != arrived), None)
    if target is not None and o.setdefault("pickup_legs", 0) < ORE_PICKUP_LEGS:
        o["pickup_legs"] += 1
        return S._decision(S.act("approach_target", {}, str(target["ref"])))
    return S.Step("decision", dict(S.INSPECT, question="skill ore: observe the drop entering the bag"), pace=.5)


def ore_completed(state, driver, observed):
    """wave1_completed hook for S23 legs (identity already checked by the caller)."""
    o = state.setdefault("ore", dict(phase="find"))
    if driver == "rock" and ladder_rock_pinned(state):
        # FINAL B3: a verified ladder-search break. Never the node: no pickup phase, baseline or ore credit.
        state.setdefault("wave1_removed", []).append(state.pop("ore_ladder_rock"))
        state["ore_ladder_rocks"] = state.get("ore_ladder_rocks", 0) + 1
        state["mines_refresh_due"] = True
    elif driver == "rock":
        if (o.get("phase") != "remove" or o.get("node") != state.get("wave1_pin", {}).get("ref")
                or ORE_NODES.get(o.get("node_qid")) != o.get("ore")
                or not str(o.get("node", "")).startswith("obj:" + str(o.get("floor")) + ":")
                or not (observed or {}).get("request_id")):
            return "fail", "ore_removal_unbound"
        o.update(phase="pickup", removal_request=(observed or {}).get("request_id"))
        state.setdefault("wave1_removed", []).append(state["wave1_pin"]["ref"])
        state["mines_refresh_due"] = True
    return None
