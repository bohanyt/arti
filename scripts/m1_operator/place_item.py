"""place_item.py SLOT TX TY [--walk] [--method click|c] [--dry-run]: operator placement of a hotbar item (e.g. a chest).

SLOT is the hotbar key (1-9, 0 = 10th slot). The player must stand within one tile (8-neighbourhood) of TX,TY, or pass
--walk to walk to the nearest free cardinal neighbour first (walk_to logic) and face the target.

Cursor mapping (camera model, matches the 2026-10-07 02:58:35 click: client (690,285) at pixel (3534.3,1738.7) -> (56,26)):
    standing = (px + 32, py + 16)                       # centre of the farmer bbox (px+8, py, 48, 32)
    viewport = standing - client/2 (clamped to the map), client = (tile*64 + 32) - viewport   (zoom 1)
The cursor is put on the tile CENTRE so a +-31 px model error still hits the tile. Stardew 1.6 may move the placement to
a nearby valid tile when the cursor tile is invalid (grass/debris blocks a chest), so the target is pre-inspected and
the result is ALWAYS re-inspected; the actual tile is printed.

Exit codes: 0 placed on target | 2 usage/precondition | 3 placed elsewhere | 4 nothing placed | 5 lost/input failure
Binds the mod port and writes the inbox (walk_to.LiveGame): NEVER run it while a trial is running (`--dry-run` included).
"""
from __future__ import annotations

import argparse, json, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import walk_to as W  # noqa: E402

PLACE_SETTLE_S = 0.8


def grab_tile(p):
    """Stardew Farmer.GetGrabTile from the pose pixel + facing (bbox (px+8, py, 48, 32))."""
    bx, by, bw, bh = p["pixel"]["x"] + 8, p["pixel"]["y"], 48, 32
    f = W.FACING_CODE.get(p.get("facing"))
    if f == "right":
        return int((bx + bw + 5) // 64), int((by + bh / 2) // 64)
    if f == "left":
        return int((bx - 5) // 64), int((by + bh / 2) // 64)
    if f == "down":
        return int((bx + bw / 2) // 64), int((by + bh + 5) // 64)
    return int((bx + bw / 2) // 64), int((by - 5) // 64)


def tile_to_client(p, tx, ty, client=(1280, 720), zoom=1.0, offset=(0, 0)):
    """Client pixel of the centre of tile (tx,ty) under the camera model; returns (cx, cy, viewport)."""
    vw, vh = client[0] / zoom, client[1] / zoom
    vx = p["pixel"]["x"] + 32 - vw / 2
    vy = p["pixel"]["y"] + 16 - vh / 2
    size = W.MAP_SIZES.get(p.get("location"))
    if size:
        mw, mh = size[0] * 64, size[1] * 64
        if mw > vw: vx = min(max(0.0, vx), mw - vw)
        if mh > vh: vy = min(max(0.0, vy), mh - vh)
    vx, vy = int(vx), int(vy)            # Game1.viewport is an int rectangle
    cx = round((tx * 64 + 32 - vx) * zoom + offset[0])
    cy = round((ty * 64 + 32 - vy) * zoom + offset[1])
    return cx, cy, (vx, vy)


def client_to_tile(p, cx, cy, client=(1280, 720), zoom=1.0):
    _, _, (vx, vy) = tile_to_client(p, 0, 0, client, zoom)
    return math.floor((cx / zoom + vx) / 64), math.floor((cy / zoom + vy) / 64)


def tile_facts(facts):
    """{(x,y): [fact_key, ...]} for facts with a tile (npcs excluded)."""
    out = {}
    for k, f in (facts or {}).items():
        if isinstance(f, dict) and isinstance(f.get("tile"), dict) and f.get("kind") != "npc":
            out.setdefault((int(f["tile"]["x"]), int(f["tile"]["y"])), []).append(k)
    return out


def hotbar_index(slot_key):
    return (int(slot_key) - 1) % 10


def place(game, slot_key, target, walk=False, method="click", force=False, dry_run=False, zoom=1.0, offset=(0, 0), log=print):
    tx, ty = target
    p = game.pose()
    loc = p.get("location"); W.check_pose(p, loc)
    me = W.pose_tile(p)
    idx = hotbar_index(slot_key)
    item = next((s for s in (p.get("inventory") or {}).get("hotbar") or [] if s.get("slot") == idx), None)
    if item is None:
        raise W.WalkError(2, f"hotbar slot key {slot_key} (index {idx}) is empty")
    if item.get("is_tool"):
        raise W.WalkError(2, f"slot key {slot_key} holds a tool ({item.get('name')}), not a placeable item")
    log(f"item slot {slot_key}: {item.get('name')} x{item.get('stack')}; player {me} target {target}")

    pre = game.inspect(tx, ty, 1)
    pre_by_tile = tile_facts(pre)
    if pre_by_tile.get(target) and not force:
        raise W.WalkError(2, f"target {target} is occupied: {pre_by_tile[target]} (grass/debris block placement; clear it or --force)")

    if max(abs(me[0] - tx), abs(me[1] - ty)) > 1 or me == target:
        if not walk:
            raise W.WalkError(2, f"player {me} is not within one tile of {target}; use walk_to.py first or pass --walk")
        blocked, _ = W.facts_to_tiles(pre)
        stands = [((tx - dx, ty - dy), d) for d, (dx, dy) in W.DIRS.items() if (tx - dx, ty - dy) not in blocked]
        if not stands:
            raise W.WalkError(3, f"no free cardinal neighbour around {target}")
        stands.sort(key=lambda s: abs(s[0][0] - me[0]) + abs(s[0][1] - me[1]))
        stand, face = stands[0]
        log(f"walking to stand tile {stand} facing {face}" + (" (dry-run: plan only)" if dry_run else ""))
        W.walk(game, stand, face=face, plan_only=dry_run, log=log)
        if dry_run:
            log("dry-run: cursor mapping below is for the CURRENT pose, not the stand tile")
        p = game.pose(); W.check_pose(p, loc); me = W.pose_tile(p)

    client = game.client_size()
    cx, cy, vp = tile_to_client(p, tx, ty, client, zoom, offset)
    legacy = (math.floor((cx + p["pixel"]["x"] - 608) / 64), math.floor((cy + p["pixel"]["y"] - 264) / 64))
    log(f"client {client} viewport {vp} -> cursor ({cx},{cy}) for {target}; grab tile {grab_tile(p)}; legacy formula would read {legacy}")
    if not (0 <= cx < client[0] and 0 <= cy < client[1]):
        raise W.WalkError(2, f"cursor ({cx},{cy}) outside the client {client}")
    if dry_run:
        return dict(ok=True, dry_run=True, cursor=(cx, cy), viewport=vp, grab=grab_tile(p))

    try:
        game.key(str(slot_key))
        if method == "click":
            game.click(cx, cy)
        else:                                   # keyboard path: `c` places at the mouse tile (receipt, 48,17 placement)
            game.move(cx, cy); game.key("c")
        game.sleep(PLACE_SETTLE_S)
        post = game.inspect(tx, ty, 1)
    finally:
        game.key("1")
    new = {k: f for k, f in post.items() if k not in pre and isinstance(f, dict) and f.get("kind") == "object"}
    placed = sorted({(int(f["tile"]["x"]), int(f["tile"]["y"])) for f in new.values() if isinstance(f.get("tile"), dict)})
    for k in sorted(new):
        log(f"new fact {k}")
    p2 = game.pose()
    item2 = next((s for s in (p2.get("inventory") or {}).get("hotbar") or [] if s.get("slot") == idx), None)
    log(f"stack {item.get('stack')} -> {item2.get('stack') if item2 else 0}")
    res = dict(target=target, placed=placed, cursor=(cx, cy), viewport=vp, new_facts=sorted(new))
    if not placed:
        raise W.WalkError(4, f"nothing new in the 3x3 around {target} (stack {item.get('stack')} -> {item2.get('stack') if item2 else 0}); {json.dumps(res, default=str)}")
    if target not in placed:
        raise W.WalkError(3, f"placed at {placed}, NOT at target {target}; {json.dumps(res, default=str)}")
    return dict(ok=True, **res)


def main(argv=None, game=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slot", choices=[str(i) for i in range(10)])
    ap.add_argument("tx", type=int); ap.add_argument("ty", type=int)
    ap.add_argument("--walk", action="store_true", help="walk to the nearest free cardinal neighbour of the target first")
    ap.add_argument("--method", choices=["click", "c"], default="click", help="left click (default) or cursor move + `c` key")
    ap.add_argument("--force", action="store_true", help="click even when the target tile already has a fact")
    ap.add_argument("--dry-run", action="store_true", help="pose + inspect + print cursor mapping; send no input")
    ap.add_argument("--zoom", type=float, default=1.0)
    ap.add_argument("--dx", type=int, default=0, help="client-pixel correction added to the cursor x")
    ap.add_argument("--dy", type=int, default=0, help="client-pixel correction added to the cursor y")
    a = ap.parse_args(argv)
    game = game or W.LiveGame()
    rc = 0
    try:
        res = place(game, a.slot, (a.tx, a.ty), a.walk, a.method, a.force, a.dry_run, a.zoom, (a.dx, a.dy))
        print("RESULT", json.dumps(res, default=str))
        if res.get("placed"):
            print("PLACED_AT", *res["placed"][0])
    except W.WalkError as e:
        print(f"FAIL ({e.code}): {e}", file=sys.stderr); rc = e.code
    finally:
        if not a.dry_run:
            try:
                game.fg_host()
            except Exception as e:
                print(f"WARNING: host foreground failed: {e}", file=sys.stderr); rc = rc or 5
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
