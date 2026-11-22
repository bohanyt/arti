"""Bounded route repair over fresh native cells/facts; never movement authority.

Only the first natural blocker on a computed path is selected. Unknown cells,
placed objects, crops and infrastructure remain closed in every search.
"""
from collections.abc import Mapping
import heapq

from arti_stardew_native_targets import xy
from arti_stardew_tool_work import AXE_QIDS, PICKAXE_QIDS, SCYTHE_QIDS, scythe_area_tiles

MAX_PIECES = 4
# W1 gap S15b: a navigation run fells at most this many path-blocking wild trees (mature via S6 tebang, growth
# stage 3-4 via tool_work cut_small_tree). Fruit trees, tapped trees and saplings are never debris here.
MAX_TREES = 2
DELTAS = ((0, -1), (-1, 0), (1, 0), (0, 1))


def _checked_route(start, path):
    if not isinstance(path, (list, tuple)) or not 1 <= len(path) <= 4096:
        return None
    route = [xy(t) for t in path]
    if (None in route or len(set(route)) != len(route) or start not in route
            or any(abs(a[0]-b[0])+abs(a[1]-b[1]) != 1 for a, b in zip(route, route[1:]))):
        return None
    return route[route.index(start):]


def debris_kind(fact):
    if not isinstance(fact, Mapping):
        return None
    if (fact.get('kind') == 'object' and not str(fact.get('qualified_item_id', '')).startswith('(BC)')
            and not any(fact.get(k) for k in ('machine', 'big_craftable', 'spawned', 'crop'))):
        from arti_stardew_skills_mines import ORE_NODES
        if fact.get('qualified_item_id') in ORE_NODES:
            return None  # ADR 176: walking never selects an ore objective.
        return {'Stone': 'stone', 'Weeds': 'weeds', 'Twig': 'twig', 'Twigs': 'twig'}.get(fact.get('name'))
    if fact.get('kind') == 'terrain' and fact.get('terrain_type') == 'Tree':
        tree = fact.get('wild_tree') or {}
        if tree.get('tapped') is False and type(tree.get('growth_stage')) is int and tree['growth_stage'] >= 3:
            return 'tree'
    if fact.get('kind') == 'clump':
        return fact.get('clump_type') if fact.get('clump_type') in ('stump', 'boulder', 'hollow_log', 'meteorite') else None
    return None


def footprint(fact):
    tile = xy(fact.get('tile'))
    if tile is None:
        return ()
    w, h = fact.get('width', 1), fact.get('height', 1)
    if not all(type(n) is int and 1 <= n <= 16 for n in (w, h)):
        return (tile,)
    return tuple((tile[0]+dx, tile[1]+dy) for dx in range(w) for dy in range(h))


def unremovable_tiles(facts, tools):
    """Tiles of identified debris her tools cannot remove: closed for a read that looks for another way (ADR 108)."""
    return {tile for fact in facts.values() if (kind := debris_kind(fact)) is not None and not can_remove(kind, tools)
            for tile in footprint(fact)}


def can_remove(kind, tools):
    return bool(set(tools) & {'stone': PICKAXE_QIDS, 'weeds': SCYTHE_QIDS,
                             'twig': AXE_QIDS, 'tree': AXE_QIDS}.get(kind, frozenset()))


# fix10e (a14 spring 12): what an unremovable route blocker needs, as a fact for her choice (vanilla tool tiers).
BLOCKER_NEEDS = {'boulder': 'butuh beliung lebih kuat (baja)', 'meteorite': 'butuh beliung emas',
                 'stump': 'butuh kapak tembaga', 'hollow_log': 'butuh kapak baja', 'stone': 'butuh beliung',
                 'weeds': 'butuh sabit', 'twig': 'butuh kapak', 'tree': 'butuh kapak'}


def blocker_needs(blocker):
    """``route_blocked_unremovable:boulder`` -> its needed tool text, else None."""
    kind = str(blocker or '').rpartition(':')[2]
    return BLOCKER_NEEDS.get(kind) if str(blocker or '').startswith(('route_blocked_unremovable:',
                                                                      'route_blocked_unsupported:')) else None


def open_frontier(grid, start):
    """Open cells reachable from ``start`` that touch the rolling window's edge or an unread cell.

    fix10e (a14 d8:J2377-J2412): the open way home left the 40x40 window (y12-15) and came back to the cabin
    stand on its top row, so "no path inside the window" was not "no path". A blocker is proven only when this
    is empty: the open area she stands in is enclosed by observed cells. Negative tiles are off the map, not unread.
    """
    from arti_stardew_goal_routes import open_paths
    return sorted(t for t in open_paths(grid, start)
                  if any((n := (t[0]+dx, t[1]+dy)) not in grid and min(n) >= 0 for dx, dy in DELTAS))


def natural_sweep(tiles, facts, grid=None):
    """Only empty ground/natural debris; saplings retain the native S17 fence.

    The caller supplies observed coverage (a local tool window or native grid).
    A closed grid cell without an identified natural occupant is infrastructure
    or unknown, never evidence of harmless debris.
    """
    return not sweep_blockers(tiles, facts, grid)


def sweep_blockers(tiles, facts, grid=None):
    """The natural_sweep refusal, named (live7-game-m4: scythe_sweep_protected x9 with no journalled cause).

    Diagnostic only, same rule: each unsafe fact on a swept tile ([x, y, kind, name/terrain, qid, flags]) and each swept tile
    that is neither open in the grid nor covered by a natural fact ([x, y, "cell", grid char or None]).
    """
    tiles = set(tiles)
    natural, out = set(), []
    for fact in facts.values():
        cells = tiles.intersection(footprint(fact))
        if not cells or fact.get('kind') == 'location':
            continue
        kind = debris_kind(fact)
        safe = (kind in ('stone', 'weeds', 'twig')
                or kind == 'tree' and (fact.get('wild_tree') or {}).get('growth_stage', 0) >= 5
                or fact.get('kind') == 'terrain' and fact.get('terrain_type') == 'Grass'
                or fact.get('kind') == 'terrain' and fact.get('terrain_type') == 'HoeDirt' and not fact.get('crop')
                # ADR 125 (a53 d8 J644): weeds 31,55 beside a boulder at 29-30,54-55. A scythe does not damage these clumps.
                or kind in ('stump', 'boulder', 'hollow_log', 'meteorite'))
        if not safe:
            x, y = min(cells)
            out.append([x, y, str(fact.get('kind')), str(fact.get('name') or fact.get('terrain_type')
                                                          or fact.get('clump_type') or ''),
                        str(fact.get('qualified_item_id') or ''),
                        sorted(k for k in ('machine', 'big_craftable', 'spawned', 'crop') if fact.get(k))])
            continue
        natural.update(cells)
    if grid is not None and not out:
        out += [[t[0], t[1], 'cell', grid.get(t)] for t in sorted(tiles)
                if not (grid.get(t) == '.' or t in natural and t in grid)]
    return out


def blocking_path(grid, start, destinations, facts, tools, *, remaining=MAX_PIECES, tree_cap=None, blocked=(),
                  repair_cost=None, details=False, route_path=None):
    """Return {path, target} or a named blocker; None preserves ordinary routing.

    Minimize debris pieces before distance. A destination outside the rolling
    grid uses its nearest observed boundary cells as survey goals. This proves
    only a local escape, never connectivity to an unseen exit.

    ``tree_cap`` None keeps the legacy rule (only mature trees, uncapped inside
    the piece cap). An int (W1 gap S15b) also admits growth-stage 3-4 wild
    trees, prefers any tree-free repair within the piece cap, and names
    ``route_clear_tree_cap`` when every repair needs more trees than allowed.

    ``blocked`` (FINAL Part C4): refs whose clear approach stalled this location visit. They stay occupied and are
    never selected; a route that only they could open names ``route_clear_blocked:<kind>``.

    ``repair_cost`` optionally ranks walking steps plus weighted debris (the
    existing Mines walking-equivalent cost). ``details`` adds path counts;
    ``diagnostic_only`` results explain protected occupants and grant no target.
    """
    start = xy(start)
    destinations = tuple(t for t in (xy(t) for t in destinations) if t is not None)
    if not grid or len(grid) > 4096 or start is None or not destinations or grid.get(start) != '.':
        return None
    route_edges = None
    if route_path is not None:
        route = _checked_route(start, route_path)
        if route is None:
            return dict(blocker='route_clear_path_invalid')
        route_edges = set(zip(route, route[1:]))
    occupied, candidates = {}, {}
    for ref, fact in facts.items():
        if not isinstance(fact, Mapping):
            continue
        kind = debris_kind(fact)
        for tile in footprint(fact):
            # Grass can coexist with debris without being a hard obstruction.
            if fact.get('terrain_type') == 'Grass' or fact.get('kind') == 'location':
                continue
            occupied.setdefault(tile, []).append(ref)
            if kind is not None and grid.get(tile) not in (None, '.', 'w', 'x'):
                stage = (fact.get('wild_tree') or {}).get('growth_stage', 0) if kind == 'tree' else None
                candidates[tile] = dict(ref=ref, kind=kind, tile=xy(fact.get('tile')),
                    supported=kind != 'tree' or stage >= 5 or tree_cap is not None,
                    **({'growth_stage': stage} if kind == 'tree' and tree_cap is not None else {}))
    candidates = {t: c for t, c in candidates.items() if occupied.get(t) == [c['ref']]}
    held = {t: c for t, c in candidates.items() if c['ref'] in blocked}
    candidates = {t: c for t, c in candidates.items() if t not in held}
    from arti_stardew_goal_routes import local_route_reaches
    goals = set(local_route_reaches(grid, start, destinations))
    if not goals:
        return None

    def search(allowed):
        # GAP8E: with tree_cap set, equal-piece routes prefer fewer trees (gap7b J7: twig 42,8 + tree 41,7, never
        # trees 41,9 + 41,7); the legacy rule (tree_cap None) keeps (pieces, steps).
        todo = [(0, 0, 0, 0, start)]
        costs, parents = {start: (0, 0, 0)}, {start: None}
        def rank(score):
            return score if repair_cost is None else (score[2] + score[0] * (repair_cost-1), *score)
        while todo:
            _, pieces, trees, steps, tile = heapq.heappop(todo)
            if costs[tile] != (pieces, trees, steps):
                continue
            if tile in goals:
                path = [tile]
                while parents[path[-1]] is not None:
                    path.append(parents[path[-1]])
                return list(reversed(path))
            for dx, dy in DELTAS:
                nxt = tile[0]+dx, tile[1]+dy
                if route_edges is not None and (tile, nxt) not in route_edges:
                    continue
                if grid.get(nxt) != '.' and nxt not in allowed:
                    continue
                if nxt in allowed and allowed[nxt]['kind'] == 'weeds':
                    facing = {(0, -1): 0, (1, 0): 1, (0, 1): 2, (-1, 0): 3}[dx, dy]
                    if not natural_sweep(scythe_area_tiles(*tile, facing), facts, grid):
                        continue
                tree = int(tree_cap is not None and nxt in allowed and allowed[nxt]['kind'] == 'tree')
                score = pieces + int(nxt in allowed), trees + tree, steps+1
                if nxt not in costs or rank(score) < rank(costs[nxt]):
                    costs[nxt], parents[nxt] = score, tile
                    priority = score[0] if repair_cost is None else score[2] + score[0] * (repair_cost-1)
                    # Weighted Mines repairs compete with a long open detour; legacy trips still minimize pieces.
                    heapq.heappush(todo, (priority, *score, nxt))
        return None

    if repair_cost is None and search({}) is not None:
        return None
    removable = {t: c for t, c in candidates.items() if c['supported'] and can_remove(c['kind'], tools)}

    def pieces(found):
        return list(dict.fromkeys(removable[t]['ref'] for t in found if t in removable))

    def result(value, found, allowed):
        if details:
            refs = list(dict.fromkeys(allowed[t]['ref'] for t in found if t in allowed))
            value['obstacles'] = [dict(ref=ref, kind=debris_kind(facts[ref]),
                                       removable=can_remove(debris_kind(facts[ref]), tools)) for ref in refs]
        return value

    path = None
    if tree_cap is not None and repair_cost is None:
        # A real player fells a tree only when no tree-free repair fits the piece cap.
        path = search({t: c for t, c in removable.items() if c['kind'] != 'tree'})
        if path is not None and len(pieces(path)) > remaining:
            path = None
    path = path or search(removable)
    if path is None:
        diagnostic = search(candidates)
        if diagnostic:
            item = next(candidates[t] for t in diagnostic if t in candidates and t not in removable)
            prefix = 'route_blocked_unsupported:' if can_remove(item['kind'], tools) and not item['supported'] else 'route_blocked_unremovable:'
            return result(dict(blocker=prefix + item['kind']), diagnostic, candidates)
        diagnostic = search({**candidates, **held}) if held else None
        if diagnostic:
            return dict(blocker='route_clear_blocked:' + next(held[t] for t in diagnostic if t in held)['kind'])
        if details:
            # Diagnostic only: relaxing an identified protected occupant can
            # explain a refusal but can NEVER select a clear target or a walk.
            occupants = {t: dict(ref=refs[0], kind='protected') for t, refs in occupied.items()
                         if grid.get(t) in ('#', 's', 'b')}
            diagnostic = search(occupants)
            if diagnostic:
                refs = list(dict.fromkeys(ref for t in diagnostic if t in occupants for ref in occupied[t]))
                return dict(diagnostic_only=True, blocker='route_protected_occupant', obstacles=[
                    dict(ref=ref, kind='protected', label=str(facts[ref].get('terrain_type') or
                         facts[ref].get('name') or facts[ref].get('kind')), removable=False) for ref in refs])
        return None
    blockers = pieces(path)
    if not blockers:
        return None
    if (repair_cost is not None and (len(blockers) > remaining or tree_cap is not None and
            sum(removable[t]['kind'] == 'tree' for t in path if t in removable) > tree_cap)
            and search({}) is not None):
        return None  # a capped optional shortcut cannot retire a real open walk
    if len(blockers) > remaining:
        return result(dict(blocker='route_clear_piece_cap'), path, removable)
    if tree_cap is not None and sum(removable[t]['kind'] == 'tree' for t in path if t in removable) > tree_cap:
        return result(dict(blocker='route_clear_tree_cap'), path, removable)
    first = next(removable[t] for t in path if t in removable)
    first = dict(first, stand=path[path.index(first['tile'])-1])
    return result(dict(path=path, target=first), path, removable)


# GAP8E: one native inspect_tiles read (InspectTilesQuery.MaxRadius = 4, a 9x9 square).
INSPECT_RADIUS_MAX = 4


def _identified(facts):
    """{tile: [debris kind or None, ...]} for every non-grass object / terrain / clump footprint."""
    covered = {}
    for fact in facts.values():
        if (not isinstance(fact, Mapping) or fact.get('kind') not in ('object', 'terrain', 'clump')
                or fact.get('terrain_type') == 'Grass'):
            continue
        for tile in footprint(fact):
            covered.setdefault(tile, []).append(debris_kind(fact))
    return covered


def unidentified_path(grid, start, destinations, facts, known_closed=(), *, route_path=None):
    """GAP8E J1 L10/L1121/L1309: the Farm exit stand 41,1 is OBSERVED and open, but every observed route to it
    crosses closed cells whose identity is not in the fresh facts (the scene snapshot keeps the 40 nearest terrain
    features of the warp-in pose, SceneObservationCaps.Terrain; 2662 Farm terrain features, 1448 grass, so the
    stage-4 tree 41,7 is never captured; the gather window is radius 1). blocking_path therefore returns None and
    no repair is selected however long the survey walks.

    The cheapest path to an observed stand, counting each identified natural-debris cell or unidentified closed
    cell ('#', 's', 'b') as one piece; water, warps, sleep rows, identified non-debris (placed objects, saplings,
    fruit trees, crops) and ``known_closed`` (inspected, no debris) stay closed. Returns the path's unidentified
    cells in walking order ([] when every piece is identified), or None (no observed stand / no such path).
    Never movement or removal authority: it only names cells a bounded native read should identify."""
    start = xy(start)
    goals = {t for t in (xy(d) for d in destinations) if t is not None} & set(grid or {})
    if not grid or len(grid) > 4096 or start is None or not goals or grid.get(start) != '.':
        return None
    route_edges = None
    if route_path is not None:
        route = _checked_route(start, route_path)
        if route is None:
            return None
        route_edges = set(zip(route, route[1:]))
    covered, closed = _identified(facts), {tuple(t) for t in known_closed}

    def cost(tile):
        cell = grid.get(tile)
        if cell == '.':
            return 0
        if cell not in ('#', 's', 'b') or tile in closed:
            return None
        kinds = covered.get(tile)
        if kinds is None:
            return 1
        return 1 if len(kinds) == 1 and kinds[0] is not None else None

    todo = [(0, 0, start)]
    costs, parents = {start: (0, 0)}, {start: None}
    while todo:
        pieces, steps, tile = heapq.heappop(todo)
        if costs[tile] != (pieces, steps):
            continue
        if tile in goals:
            path = [tile]
            while parents[path[-1]] is not None:
                path.append(parents[path[-1]])
            return [t for t in reversed(path) if grid.get(t) != '.' and t not in covered]
        for dx, dy in DELTAS:
            nxt = tile[0]+dx, tile[1]+dy
            if route_edges is not None and (tile, nxt) not in route_edges:
                continue
            extra = cost(nxt)
            if extra is None:
                continue
            score = pieces + extra, steps + 1
            if nxt not in costs or score < costs[nxt]:
                costs[nxt], parents[nxt] = score, tile
                heapq.heappush(todo, (*score, nxt))
    return None


def inspect_square(cells):
    """(center, radius) of one native read: the first unidentified cell and the later ones within reach."""
    cx, cy = cells[0]
    near = [max(abs(x-cx), abs(y-cy)) for x, y in cells if max(abs(x-cx), abs(y-cy)) <= INSPECT_RADIUS_MAX]
    return (cx, cy), max([1] + near)


def inspected_closed(grid, center, radius, facts):
    """Closed cells of the read square that no natural-debris fact covers: observed, not repairable debris."""
    covered = _identified(facts)
    cx, cy = center
    return sorted(t for t, c in grid.items() if c in ('#', 's', 'b') and max(abs(t[0]-cx), abs(t[1]-cy)) <= radius
                  and not (len(covered.get(t, ())) == 1 and covered[t][0] is not None))
