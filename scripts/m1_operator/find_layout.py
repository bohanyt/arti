"""find_layout.py --plan-only: read-only test-layout finder for an M1 trial (OPERATOR_RUNBOOK.md section 5).

Reads ARTI's pose (mod port), sweeps inspect_tiles windows (radius 4, every 8 tiles, +-12 x +-8 around ARTI), and
lists mature wild trees (growth_stage >= 5, not a stump, not tapped) with their free stand tiles, open sides, existing
chests (`(BC)130`) and free chest spots (a tile with no fact at all, 2-4 tiles from the stand, >= 2 open sides). The best
candidates are checked with `walk_to.walk(..., plan_only=True)` (BFS plan, no input). Prints one `LAYOUT {json}` line:
tree, stand tile, face, chest (existing) or chest spots, the BFS plan and the runner-up candidates.

It never walks, clicks or places anything: --plan-only is the only mode (the flag is accepted for symmetry with
walk_to). Buildings and map tiles are not scene facts: look at a screenshot too.
The search widens 12 -> 24 -> 40 tiles (inspect windows are cached); when the mod refuses inspect_tiles (D21) it
continues from the scene snapshot only (truncated to the nearest facts), and --scene-only never inspects. When no stand
can be planned but a mature tree cluster lies within 40 tiles, it prints a DRY walk leg (<= 12 tiles toward the cluster,
plan-checked, never walked here; the soak walks it and reruns).
Exit: 0 layout found | 2 pose/inspect unavailable, or a trial runner is running | 3 no usable tree/stand (or no mature
      tree within 40 tiles) | 4 ARTI is indoors (Cabin, house, ...): walk out first
      (`DRY=1 NO_NUDGE=1 bash run_attempt.sh dry x x x`) | 5 a walk leg toward a tree cluster is suggested (not walked).
Binds the mod port and writes the inbox: NEVER run it while a trial is running (it refuses when it can see one).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402,F401  (folders for walk_to.LiveGame)
import walk_to as W  # noqa: E402

N4 = ((0, 1), (1, 0), (-1, 0), (0, -1))
FACE_TOWARD = {(1, 0): "left", (-1, 0): "right", (0, 1): "up", (0, -1): "down"}   # stand - tree -> face
STORE_RADIUS = 12
OUTDOOR = {"Farm", "Forest", "Town", "Mountain", "Beach", "BusStop", "Backwoods", "Railroad", "Desert", "Woods",
           "Summit", "IslandWest", "IslandNorth", "IslandSouth", "IslandEast"}   # the M1 layout needs the Farm


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class InspectRefused(Exception):
    pass


def gather(game, me, rx=12, ry=8, step=8, r=4, done=None):
    """inspect_tiles windows around me (skipping centres in `done`). Raises InspectRefused when the mod refuses
    (D21: CommandSessionCapacityReached after ~256 legacy inspects in one farmhand session)."""
    facts = {}
    done = set() if done is None else done
    for cx in range(me[0] - rx, me[0] + rx + 1, step):
        for cy in range(me[1] - ry, me[1] + ry + 1, step):
            c = (max(0, cx), max(0, cy))
            if c in done:
                continue
            try:
                facts.update(game.inspect(c[0], c[1], r) or {})
            except W.WalkError as e:
                raise InspectRefused(str(e))
            done.add(c)
    return facts


def _tile(f):
    t = f.get("tile") if isinstance(f, dict) else None
    return (int(t["x"]), int(t["y"])) if isinstance(t, dict) else None


def mature_tree(key, f):
    wt = (f.get("wild_tree") or {}) if isinstance(f, dict) else {}
    return (str(key).endswith(":Tree") and type(wt.get("growth_stage")) is int and wt["growth_stage"] >= 5
            and not wt.get("stump") and wt.get("tapped") is False)


def candidates(facts, me, footprint=()):
    """All (tree, stand) candidates with their layout facts, best first. Building footprint tiles (scene
    building_door.footprint) count as blocked: never a stand tile or a chest spot inside a building."""
    blocked, _ = W.facts_to_tiles(facts)
    blocked |= set(footprint)
    occupied = {t for f in facts.values() if (t := _tile(f)) and f.get("kind") != "npc"} | blocked
    chests = sorted({t for k, f in facts.items() if "(BC)130" in str(k) and (t := _tile(f))})
    out = []
    for k, f in sorted(facts.items()):
        if not mature_tree(k, f):
            continue
        tree = _tile(f)
        open_sides = sum((tree[0] + dx, tree[1] + dy) not in occupied for dx, dy in N4)
        for dx, dy in N4:
            stand = (tree[0] + dx, tree[1] + dy)
            if stand in blocked:
                continue
            near = sorted((manhattan(stand, c), c) for c in chests if manhattan(stand, c) <= STORE_RADIUS)
            spots = sorted((manhattan(stand, s), s) for s in
                           ((x, y) for x in range(tree[0] - 5, tree[0] + 6) for y in range(tree[1] - 5, tree[1] + 6))
                           if s not in occupied and s != tree and s != stand and 2 <= manhattan(stand, s) <= 4
                           and sum((s[0] + ex, s[1] + ey) not in occupied for ex, ey in N4) >= 2)[:3]
            stand_open = sum((stand[0] + ex, stand[1] + ey) not in occupied for ex, ey in N4)
            out.append(dict(tree=list(tree), key=k, stage=f["wild_tree"]["growth_stage"], stand=list(stand),
                            face=FACE_TOWARD[(dx, dy)], open_sides=open_sides, stand_open=stand_open,
                            dist=manhattan(stand, me), chest=list(near[0][1]) if near else None,
                            chest_dist=near[0][0] if near else None, chest_spots=[list(s) for _, s in spots]))
    out.sort(key=score)
    return out


def score(c):
    """Lower is better: open layouts first, then a close existing chest (2-5 tiles), a chest spot, short distance."""
    chest_ok = c["chest_dist"] is not None and 2 <= c["chest_dist"] <= 5
    return (c["open_sides"] < 3, not chest_ok, not (chest_ok or c["chest_spots"]), c["stand_open"] < 2, c["dist"])


RADII = (12, 24, 40)       # widening search; 40 tiles is the hard limit (D22: refuse clearly beyond)
LEG_TILES = 12             # a suggested (DRY) walk leg toward the nearest mature tree cluster is at most this long


def chebyshev(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def tree_clusters(facts, me, limit=RADII[-1], gap=4):
    """Mature trees within `limit` tiles, grouped (single link, Chebyshev <= gap); nearest cluster first."""
    trees = sorted({t for k, f in facts.items() if mature_tree(k, f) and (t := _tile(f)) and chebyshev(t, me) <= limit},
                   key=lambda t: manhattan(t, me))
    clusters = []
    for t in trees:
        for c in clusters:
            if any(chebyshev(t, u) <= gap for u in c):
                c.append(t)
                break
        else:
            clusters.append([t])
    clusters.sort(key=lambda c: min(manhattan(t, me) for t in c))
    return clusters


def walk_leg(game, me, cluster, blocked, log, store=None):
    """A plannable tile at most LEG_TILES toward the cluster's nearest tree (DRY: printed, never walked here)."""
    tx, ty = min(cluster, key=lambda t: manhattan(t, me))
    d = manhattan((tx, ty), me)
    f = min(1.0, LEG_TILES / d) if d else 0.0
    gx, gy = round(me[0] + (tx - me[0]) * f), round(me[1] + (ty - me[1]) * f)
    for r in range(0, 3):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                g = (gx + dx, gy + dy)
                if max(abs(dx), abs(dy)) != r or g in blocked or g == (tx, ty):
                    continue
                try:
                    res = W.walk(game, g, None, plan_only=True, log=lambda m: None, store=store)
                except W.WalkError as e:
                    log(f"  leg {g}: no plan ({e})")
                    continue
                return dict(leg=list(g), toward_tree=[tx, ty], plan_tiles=len(res["path"]) - 1)
    return None


def find(game, top=5, log=print, scene_only=False, radii=RADII, store=None):
    p = game.pose()
    me = W.pose_tile(p)
    base = dict(me=list(me), location=p.get("location"), time_of_day=p.get("time_of_day"), energy=p.get("energy"))
    if p.get("location") not in OUTDOOR:
        log(f"ARTI is indoors ({p.get('location')} {me}): walk out first; no inspect sweep done")
        return dict(base, ok=False, reason="indoors", best=None, others=[],
                    hint="DRY=1 NO_NUDGE=1 bash run_attempt.sh dry x x x  (Cabin key recipe), then rerun")
    scene = game.scene() if hasattr(game, "scene") else None
    if scene is not None and not W.scene_matches(scene, p.get("location")):
        scene = None
    fp = W.footprint_tiles(scene) | set(W.hazard_tiles(p.get("location")))
    if store is not None:                          # D37: learned blocked tiles + stands whose live walk ended exit 4
        fp |= store.blocked(p.get("location")) | store.failed(p.get("location"))
    facts = {k: f for k, f in (scene or {}).items() if isinstance(f, dict) and isinstance(f.get("tile"), dict)}
    source = "scene" if scene_only else "inspect+scene"
    done, cands, tried = set(), [], set()
    for radius in radii:
        if source != "scene":
            try:
                facts.update(gather(game, me, radius, radius, done=done))
            except InspectRefused as e:
                source = "scene"
                log(f"inspect refused ({e}); scene snapshot only (truncated, nearest facts)")
        cands = [c for c in candidates(facts, me, fp) if chebyshev(c["tree"], me) <= radius]
        log(f"ARTI {p.get('location')} {me} radius {radius} ({source}): {len(facts)} facts, {len(cands)} candidates")
        for c in [c for c in cands if (tuple(c["stand"]), c["face"]) not in tried][:top]:
            tried.add((tuple(c["stand"]), c["face"]))
            try:
                res = W.walk(game, tuple(c["stand"]), c["face"], plan_only=True, log=lambda m: None,
                             scene_only=source == "scene", store=store)
            except W.WalkError as e:
                log(f"  {c['tree']} stand {c['stand']}: no plan ({e})")
                continue
            best = dict(c, plan=dict(tiles=len(res["path"]) - 1, segments=[[d, n] for d, n, _ in res["segments"]]))
            return dict(base, ok=True, source=source, radius=radius, best=best, others=[x for x in cands[:top] if x is not c])
        if source == "scene":
            break                     # the scene does not grow with the radius
    clusters = tree_clusters(facts, me)
    if not clusters:
        log(f"no mature tree within {radii[-1]} tiles ({source}); stage elsewhere by hand")
        return dict(base, ok=False, source=source, reason=f"no_tree_within_{radii[-1]}", best=None, others=[])
    blocked, _ = W.facts_to_tiles(facts)
    leg = walk_leg(game, me, clusters[0], blocked | fp, log, store)
    return dict(base, ok=False, source=source, reason="walk_leg" if leg else "no_plan", best=None, others=cands[:top],
                walk_leg=leg, cluster=[list(t) for t in clusters[0]],
                hint="DRY leg: walk_to.py X Y, then rerun find_layout" if leg else "no plannable stand or leg")


def main(argv=None, game=None, trial_check=None, store=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan-only", action="store_true", help="the only mode (always on): print the layout, send no input")
    ap.add_argument("--top", type=int, default=5, help="candidates checked with a BFS plan (default 5)")
    ap.add_argument("--scene-only", action="store_true",
                    help="no inspect_tiles: the scene snapshot only (automatic when inspect is refused, D21)")
    ap.add_argument("--radius", type=int, default=RADII[-1], help=f"widest search radius, <= {RADII[-1]} (default)")
    a = ap.parse_args(argv)
    radii = tuple(r for r in RADII if r < a.radius) + (min(a.radius, RADII[-1]),)
    if game is None:
        import win_facts as wf
        running = (trial_check or wf.trial_running)()
        if running is not False:      # None = the process list could not be read: refuse too
            print("REFUSED: " + ("a trial runner is running (port 3017 / inbox belong to it)" if running
                                 else "cannot list python processes (PowerShell CIM failed)"), file=sys.stderr)
            return 2
        game = W.LiveGame()
        store = store if store is not None else W.default_store()
    try:
        res = find(game, a.top, log=lambda m: print(m, file=sys.stderr), scene_only=a.scene_only, radii=radii, store=store)
    except W.WalkError as e:
        print(f"FAIL ({e.code}): {e}", file=sys.stderr)
        return 2
    print("LAYOUT " + json.dumps(res, ensure_ascii=False))
    if res.get("reason") == "indoors":
        print(f"INDOORS: ARTI is in {res['location']!r}, walk out first ({res['hint']})", file=sys.stderr)
        return 4
    if res.get("reason") == "walk_leg":
        print(f"WALK LEG (not walked): walk_to.py {res['walk_leg']['leg'][0]} {res['walk_leg']['leg'][1]} toward the tree "
              f"cluster at {res['walk_leg']['toward_tree']}, then rerun", file=sys.stderr)
        return 5
    if res.get("reason", "").startswith("no_tree_within"):
        print(f"REFUSED: no mature tree within {radii[-1]} tiles of ARTI ({res.get('source')})", file=sys.stderr)
    return 0 if res["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
