"""pick_tree.py CHEST_X CHEST_Y: print `TX TY FACE` for the nearest reachable stand tile next to a mature tree.

Read-only sweep: inspect_tiles windows (radius 4, every 8 tiles, +-12 around the given point, usually the chest), keeps
wild trees with growth_stage >= 5 that are not stumps, takes their free cardinal neighbours sorted by Manhattan distance
to the point, drops stand tiles inside a building footprint (scene `building_door.footprint`, WS3a; live D8: stand (50,13)
was inside the cabin) or on a walk_to.KNOWN_HAZARDS tile (pet bowl, bus door), and returns the first one that `walk_to.walk(..., plan_only=True)` can route to (footprints blocked
there too; it sends no input). Never start a trial after a failed walk.
Exit 0 = printed a candidate | 2 = a trial runner is running (or the process list is unknown) / no pose | 3 = none.
Writes the inbox and binds the mod port: NEVER run it while a trial is running (it refuses when it can see one).
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402,F401
import walk_to as W  # noqa: E402

FACE = {"left": (1, 0), "right": (-1, 0), "up": (0, 1), "down": (0, -1)}     # stand = tree + offset, face toward it


def candidates(facts, cx, cy, footprint=()):
    """[(distance, stand, face)] for mature trees, stand tiles passable and outside building footprints."""
    by_tile = {}
    for f in facts.values():
        t = f.get("tile") if isinstance(f, dict) else None
        if t:
            by_tile[(t["x"], t["y"])] = f
    blocked, _ = W.facts_to_tiles(facts)
    fp = set(footprint)
    free = lambda t: t in by_tile and by_tile[t].get("passable") is True and t not in blocked and t not in fp
    out = []
    for t, f in by_tile.items():
        w = f.get("wild_tree")
        if w and not w.get("stump") and (w.get("growth_stage") or 0) >= 5:
            for face, (dx, dy) in FACE.items():
                s = (t[0] + dx, t[1] + dy)
                if free(s):
                    out.append((abs(s[0] - cx) + abs(s[1] - cy), s, face))
    return sorted(out)


def pick(game, cx, cy, top=8, log=lambda m: None, scene_only=False, store=None):
    scene = game.scene() if hasattr(game, "scene") else None
    facts = {k: f for k, f in (scene or {}).items() if isinstance(f, dict) and isinstance(f.get("tile"), dict)}
    refused = scene_only
    for wx in range(cx - 12, cx + 13, 8):
        for wy in range(cy - 12, cy + 13, 8):
            if refused:
                break
            try:
                facts.update(game.inspect(max(0, wx), max(0, wy), 4) or {})
            except W.WalkError as e:                  # D21: refused for the rest of the session -> scene only
                refused = True
                log(f"inspect refused ({e}); scene snapshot only")
    location = (game.pose() or {}).get("location")
    fp = W.footprint_tiles(scene) | set(W.hazard_tiles(location))      # + known hazards (pet bowl, bus door)
    if store is not None:                                              # D37: learned blocked + failed stands
        fp |= store.blocked(location) | store.failed(location)
    for _, s, face in candidates(facts, cx, cy, fp)[:top]:
        try:
            W.walk(game, s, face, plan_only=True, log=log, scene_only=refused, store=store)
        except W.WalkError as e:
            log(f"stand {s} {face}: no plan ({e})")
            continue
        return s, face
    return None


def main(argv=None, game=None, trial_check=None, store=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cx", type=int, metavar="CHEST_X"); ap.add_argument("cy", type=int, metavar="CHEST_Y")
    ap.add_argument("--scene-only", action="store_true", help="no inspect_tiles (automatic when refused, D21)")
    a = ap.parse_args(argv)
    if game is None:
        import win_facts as wf
        running = (trial_check or wf.trial_running)()
        if running is not False:
            print("REFUSED: " + ("a trial runner is running" if running else "cannot list python processes"), file=sys.stderr)
            return 2
        game = W.LiveGame()
        store = store if store is not None else W.default_store()
    try:
        res = pick(game, a.cx, a.cy, log=lambda m: print(m, file=sys.stderr), scene_only=a.scene_only, store=store)
    except W.WalkError as e:
        print(f"FAIL ({e.code}): {e}", file=sys.stderr)
        return 2
    if not res:
        return 3
    (sx, sy), face = res
    print(sx, sy, face)
    return 0


if __name__ == "__main__":
    sys.exit(main())
