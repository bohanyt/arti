"""sweep.py KIND [R]: read-only target sweep around ARTI for staging a witness (operator helper, between trials only).

  KIND  Stone | Weeds   objects with that name (inspect_tiles facts), stand tiles, nearby chests/tilled soil/crops/saplings
        Forage          spawned objects (pickable forage, `spawned: true`, WS3a) from inspect + scene facts
        Water           watering-can refill tiles (`tiles[].water_refill`, WS1). The scene publishes these only for the
                        3x3 gather window around ARTI: walk next to the pond/river first, then sweep
  R     sweep radius in tiles (default 12; inspect windows of radius 4 every 8 tiles)

Stand tiles are the free cardinal neighbours of a target (not blocked, no fact on them, not inside a building footprint
from the scene `building_door.footprint`), each with the facing toward the target. Prints `ARTI ...` and one JSON line
per candidate (nearest first, at most 15). Sends no input.
Exit: 0 candidates printed | 2 a trial runner is running (or the process list is unknown) / no pose | 3 none found.
Binds the mod port and writes the inbox: NEVER run it while a trial is running (it refuses when it can see one).
Adopted from the batch-1/2 prep helpers (run/batch1/tools/sweep.py, run/batch2/tools/scan.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402,F401
import walk_to as W  # noqa: E402

KINDS = ("Stone", "Weeds", "Forage", "Water")
STAND = (((0, 1), "up"), ((0, -1), "down"), ((1, 0), "left"), ((-1, 0), "right"))   # stand offset, face toward target
RISKY = ("(BC)", "HoeDirt", "Crop", "Sapling")


def _tile(f):
    t = f.get("tile") if isinstance(f, dict) else None
    return (int(t["x"]), int(t["y"])) if isinstance(t, dict) else None


def gather_window_tiles(scene):
    for f in (scene or {}).values():
        if isinstance(f, dict) and isinstance(f.get("tiles"), list):
            return f["tiles"]
    return []


def sweep(kind, facts, scene, me, radius=12, top=15, location=None, exclude=()):
    """Pure: candidate dicts {kind, name, tile, dist, stands:[[x, y, face]], flags} nearest first."""
    allf = dict(scene or {})
    allf.update(facts or {})
    blocked, _ = W.facts_to_tiles(allf)
    fp = W.footprint_tiles(scene) | set(W.hazard_tiles(location)) | set(exclude)   # hazards, learned blocks (D37)
    by_tile = {}
    for k, f in allf.items():
        t = _tile(f)
        if t:
            by_tile.setdefault(t, []).append((k, f))

    def stands(t, water=False):
        out = []
        for (dx, dy), face in STAND:
            s = (t[0] + dx, t[1] + dy)
            if s in blocked or s in fp:
                continue
            here = by_tile.get(s, [])
            if water and s in water_set:
                continue
            if all((f.get("name") in ("Grass",) or f.get("passable") is True) and f.get("kind") != "object" for _, f in here):
                out.append([s[0], s[1], face])
        return out

    def flags(t):
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for k, f in by_tile.get((t[0] + dx, t[1] + dy), []):
                    if any(r in k for r in RISKY) or (kind == "Weeds" and "Tree" in k):
                        out.append(k)
        return out

    targets = []
    water_set = set()
    if kind == "Water":
        water_set = {(int(t["x"]), int(t["y"])) for t in gather_window_tiles(scene) if t.get("water_refill") is True}
        targets = [(t, "water") for t in sorted(water_set)]
    else:
        seen = set()
        for k, f in allf.items():
            t = _tile(f)
            if not t or f.get("kind") != "object" or t in seen:
                continue
            if (kind == "Forage" and f.get("spawned") is True) or (kind in ("Stone", "Weeds") and f.get("name") == kind):
                seen.add(t)
                targets.append((t, f.get("name") or f.get("qualified_item_id")))
    res = []
    for t, name in targets:
        d = abs(t[0] - me[0]) + abs(t[1] - me[1])
        if kind != "Water" and d > 2 * radius:
            continue
        res.append(dict(kind=kind, name=name, tile=list(t), dist=d, stands=stands(t, water=kind == "Water"), flags=flags(t)))
    res.sort(key=lambda r: (not r["stands"], r["dist"]))
    return res[:top]


def main(argv=None, game=None, trial_check=None, store=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("radius", nargs="?", type=int, default=12)
    a = ap.parse_args(argv)
    if game is None:
        import win_facts as wf
        running = (trial_check or wf.trial_running)()
        if running is not False:
            print("REFUSED: " + ("a trial runner is running (port 3017 / inbox belong to it)" if running
                                 else "cannot list python processes (PowerShell CIM failed)"), file=sys.stderr)
            return 2
        game = W.LiveGame()
        store = store if store is not None else W.default_store()
    try:
        p = game.pose()
    except W.WalkError as e:
        print(f"FAIL ({e.code}): {e}", file=sys.stderr)
        return 2
    me = W.pose_tile(p)
    print("ARTI", p.get("location"), me, p.get("time_of_day"))
    facts = {}
    if a.kind != "Water":
        r = a.radius
        for cx in range(me[0] - r, me[0] + r + 1, 8):
            for cy in range(me[1] - r, me[1] + r + 1, 8):
                try:
                    facts.update(game.inspect(max(0, cx), max(0, cy), 4) or {})
                except W.WalkError:
                    continue
    scene = game.scene() if hasattr(game, "scene") else None
    if scene is not None and W.scene_location(scene) not in (None, p.get("location")):
        scene = None
    excl = (store.blocked(p.get("location")) | store.failed(p.get("location"))) if store is not None else set()
    res = sweep(a.kind, facts, scene, me, a.radius, location=p.get("location"), exclude=excl)
    for r in res:
        print(json.dumps(r, ensure_ascii=False))
    if not res:
        print(f"none: no {a.kind} " + ("refill tile in ARTI's 3x3 gather window (walk next to water, rerun)"
                                        if a.kind == "Water" else f"within radius {a.radius}"), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
