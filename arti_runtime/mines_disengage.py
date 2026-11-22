"""ADR 146: bounded floor retreat after an ineffective fight; no combat or exit credit."""
from collections import deque
from collections.abc import Mapping
from math import hypot

import arti_stardew_skills as S
from arti_stardew_skills_mines import SURVIVAL_HP_PCT

SAFE_DISTANCE = 3.0
MOVES_MAX = 24
PATH_MAX = 6


def exit_step(state, reason):
    state.update(wave1_exit_due=True, wave1_survival_exit=True,
                 wave1_hold="partial:mines_combat_timeout", mines_disengage_unsafe=reason)
    return S.wave1_repin(state, "skill lawan_mine", "exit", "exited_mines")


def step(snap, state, context):
    """Fresh native floor/nav facts propose ordinary short moves. A safe readback returns her choices.

    No retry of the stopped fight. HP loss, low reserve, blocked/stuck retreat or missing threat geometry
    hands ownership to the existing verified exit. Publication, identity and release fences remain upstream.
    """
    motor = S._g(snap, "motor")
    location = S._g(motor, "location")
    combat = context.get("combat") or {}
    if not context.get("ui_complete") or S.menu_up(snap):
        return S.Step("decision", S.INSPECT)
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if last is not None and (S._g(last, "cleanup_confirmed") is not True
                            or S._g(last, "lifecycle") not in ("completed", "rejected", "not_taken")):
        return S.Step("decision", S.INSPECT)
    here = S._wave1_xy(S._g(motor, "tile"))
    hp = [n for n in (S._g(motor, "health"), combat.get("hp")) if type(n) is int]
    hp = min(hp) if hp else None
    maximum = combat.get("max_hp")
    if (hp is None or type(maximum) is not int or maximum <= 0 or hp <= 0
            or hp < SURVIVAL_HP_PCT or hp * 100 < SURVIVAL_HP_PCT * maximum):
        return exit_step(state, "hp_reserve")
    retreat = state.setdefault("mines_disengage", dict(location=location, hp=hp, moves=0, stuck=0))
    if location != retreat["location"] or not str(location).startswith("UndergroundMine"):
        return S.Step("fail", reason="mines_disengage_floor_changed")
    if hp < retreat["hp"]:
        return exit_step(state, "hp_falling")
    if here is None or S._g(motor, "can_player_move") is False or S._g(motor, "is_player_free") is False:
        return exit_step(state, "controls_unavailable")
    monsters = [m for m in combat.get("monsters", ()) if isinstance(m, Mapping)
                and not (type(m.get("hp")) is int and m["hp"] <= 0)]
    if "monsters" not in combat:
        return S.Step("decision", S.INSPECT)
    if any(not isinstance(m.get("distance"), (int, float)) for m in monsters):
        return exit_step(state, "threat_distance_unknown")
    if not monsters or min(m["distance"] for m in monsters) >= SAFE_DISTANCE:
        # The result is deliberately partial: no kill, rock, floor or exit is claimed.
        retreat["verified"] = True
        return S.Step("fail", reason="partial:mines_combat_disengaged")
    tiles = [S._wave1_xy(m.get("tile")) for m in monsters]
    if any(t is None for t in tiles):
        return exit_step(state, "threat_tile_unknown")
    pose = (here, tuple(S._g(motor, "pixel") or ()))
    retreat["stuck"] = retreat["stuck"] + 1 if retreat.get("pose") == pose else 0
    if retreat["moves"] >= MOVES_MAX or retreat["stuck"] >= 3:
        return exit_step(state, "retreat_stuck")
    grid = context.get("nav_grid") or {}
    separation = lambda p: min(hypot(p[0]-t[0], p[1]-t[1]) for t in tiles)
    # Shortest observed-open route to separation; never walk closer to a monster to get there.
    queue, seen, path = deque([[here]]), {here}, None
    while queue:
        candidate = queue.popleft()
        cell = candidate[-1]
        if len(candidate) > 1 and separation(cell) >= SAFE_DISTANCE:
            path = candidate
            break
        if len(candidate) > PATH_MAX:
            continue
        for delta in S._DIRS:
            nxt = (cell[0]+delta[0], cell[1]+delta[1])
            if nxt not in seen and grid.get(nxt) == "." and separation(nxt) >= separation(cell):
                seen.add(nxt)
                queue.append(candidate + [nxt])
    if path is None:
        return exit_step(state, "no_safe_floor_path")
    delta = (path[1][0]-here[0], path[1][1]-here[1])
    retreat.update(moves=retreat["moves"]+1, pose=pose)
    return S._decision(S.act("world_key.move_cardinal", dict(direction=S._DIRS[delta], ticks=8)))
