"""walk_to.py TX TY [--face up|down|left|right] [--plan-only] [--stop-before-warp]: operator pre-positioning for the
farmhand (not product code).

Reads the live pose (mod NDJSON `state` event on 127.0.0.1:PORT, same as capture_frames/poll_state), gathers
passability with inspect_tiles in radius-4 windows laid along the planned route, BFS-plans (unknown tiles optimistic,
blocked facts avoided), walks the path as same-direction holds (215 ms/tile, ui_op.py hold), re-reads the pose after
every segment and replans on deviation (<= 6 replans, <= 60 commanded tiles). A tile that gave zero progress is learned
as blocked (buildings / map tiles are not scene facts). Optional final facing tap (40 ms). The HOST window is
foregrounded at the end, also on failure.

Building footprints from the scene observation (`building_door.footprint`, WS3a) are always blocked (live D8: a stand
tile inside the cabin). --stop-before-warp also blocks every warp source tile the scene lists and refuses a leg that ends
on a warp or whose next tile in its direction is one (live B2W6: walk_to 10 24 in BusStop crossed the warp at x=9).

Exit codes: 0 arrived | 2 usage / bad pose / no scene for --stop-before-warp | 3 no route (or a warp guard refusal) |
4 bounds exceeded | 5 lost (location change, not free)
Binds the mod port and writes the inbox: NEVER run it while a trial is running (`--plan-only` included).
"""
from __future__ import annotations

import argparse, heapq, json, os, socket, subprocess, sys, time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402

MS_PER_TILE = 215
SINGLE_TILE_MS = 110      # live B3 (BusStop forage restage): `hold w 110` moved exactly 1 tile; a 215 ms 1-tile leg
                          # overshot by a tile and oscillated (24,14)<->(24,16). HYPOTHESIS (one sample); --tile-ms /
                          # --single-ms override, --tiles N caps every hold at N tiles with a pose readback in between.
# Hazards the scene does not publish (no fact): never a goal, stand, chest spot or park tile. location -> {tile: why}.
KNOWN_HAZARDS = {
    "BusStop": {(22, 8): "bus door warp to the Desert (warp:BusStop:22,8->Desert:18,27, live B3)"},
    "Farm": {(x, y): "1.6 pet bowl, a doorless Building on the standard farm (53-54,7-8): no scene fact; a park there "
                     "gave CURSOR_NOT_INERT:BUILDING (live B3W1a)" for x in (53, 54) for y in (7, 8)},
}
FACE_TAP_MS = 40
SETTLE_S = 0.6
INSPECT_RADIUS = 4
MAX_REPLANS = 6
MAX_TILES = 60
MAX_INSPECTS = 8
PLAN_MARGIN = 8           # BFS box = bbox(start, target) grown by this many tiles
MAP_SIZES = {"Farm": (80, 65)}

DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
KEYS = {"up": "w", "down": "s", "left": "a", "right": "d"}
FACING_CODE = {0: "up", 1: "right", 2: "down", 3: "left"}


class WalkError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg); self.code = code


def read_observation(domain, seconds=6.0):
    """Facts of the first available `observation` of `domain` (e.g. progress.inventory) on the mod port, or None.
    Binds the port like pose(): never during a trial."""
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", paths.PORT)); srv.listen(1); srv.settimeout(seconds)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close()
        return None
    conn.settimeout(0.5); buf = b""; t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            try:
                chunk = conn.recv(1 << 20)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                pl = e.get("payload") if e.get("ev") == "observation" else None
                if isinstance(pl, dict) and pl.get("domain") == domain and pl.get("available"):
                    return dict(((pl.get("body") or {}).get("facts")) or {})
    finally:
        conn.close(); srv.close()
    return None


class LearnedStore:
    """D37: tiles walk_to learned as blocked (map cliffs, buildings, anything with no scene/inspect fact) and goals whose
    live walk ended with exit 4, persisted per location in RUN/learned-blocked.json so walk_to, find_layout, pick_tree and
    sweep stop re-proposing them (live: stand (40,7) along the cliff row y=6, exit 4 twice). Written atomically."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and isinstance(d.get("locations"), dict) else {"version": 1, "locations": {}}
        except (OSError, ValueError):
            return {"version": 1, "locations": {}}

    def _save(self, d):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        for _ in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.2)

    def _loc(self, d, location):
        return d["locations"].setdefault(str(location), {"blocked": [], "failed_goals": []})

    def remove(self, location, tile):
        """Drop a tile from both lists of a location; True when something was removed."""
        d = self._load()
        loc = d["locations"].get(str(location))
        if not loc:
            return False
        nb = [t for t in loc.get("blocked") or [] if (int(t[0]), int(t[1])) != tuple(tile)]
        ng = [g for g in loc.get("failed_goals") or [] if (int(g[0]), int(g[1])) != tuple(tile)]
        if len(nb) == len(loc.get("blocked") or []) and len(ng) == len(loc.get("failed_goals") or []):
            return False
        loc["blocked"], loc["failed_goals"] = nb, ng
        self._save(d)
        return True

    def listing(self):
        return self._load()["locations"]

    def blocked(self, location):
        loc = self._load()["locations"].get(str(location)) or {}
        return {(int(t[0]), int(t[1])) for t in loc.get("blocked") or []}

    def failed(self, location):
        loc = self._load()["locations"].get(str(location)) or {}
        return {(int(g[0]), int(g[1])) for g in loc.get("failed_goals") or []}

    def add_blocked(self, location, tiles):
        d = self._load()
        loc = self._loc(d, location)
        have = {tuple(t) for t in loc["blocked"]}
        new = [list(t) for t in tiles if tuple(t) not in have]
        if new:
            loc["blocked"] += new
            self._save(d)

    def fail_goal(self, location, goal, why=""):
        d = self._load()
        loc = self._loc(d, location)
        if not any((g[0], g[1]) == tuple(goal) for g in loc["failed_goals"]):
            loc["failed_goals"].append([goal[0], goal[1], str(why)[:200], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())])
            self._save(d)


def default_store():
    return LearnedStore(paths.LEARNED_BLOCKED)


# ---------------------------------------------------------------- live adapter (never used by the offline tests)
class LiveGame:
    """Real I/O: pose from the mod socket, facts from inspect_tiles, input through ui_op.py."""

    def __init__(self):
        self.fh = paths.FH_HWND_FILE.read_text().strip()
        self.host = paths.HOST_HWND_FILE.read_text().strip()
        self._cmd = None

    def pose(self, seconds=5.0):
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", paths.PORT)); srv.listen(1); srv.settimeout(seconds)
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            srv.close(); raise WalkError(2, f"no mod connection on 127.0.0.1:{paths.PORT} within {seconds}s (is another listener bound?)")
        conn.settimeout(0.5); buf = b""; t0 = time.time()
        try:
            while time.time() - t0 < seconds:
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("ev") == "state":
                        return e["payload"]
        finally:
            conn.close(); srv.close()
        raise WalkError(2, "no `state` event received")

    def scene(self, seconds=6.0):
        """Facts of the latest `scene.current_location` observation (warps, building doors + footprints, the 3x3
        gather window) or None. Binds the mod port like pose(): never during a trial."""
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", paths.PORT)); srv.listen(1); srv.settimeout(seconds)
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            srv.close()
            return None
        conn.settimeout(0.5); buf = b""; t0 = time.time()
        found, token = None, None
        try:
            while time.time() - t0 < seconds and not (found is not None and token is not None):
                try:
                    chunk = conn.recv(1 << 20)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    pl = e.get("payload") if e.get("ev") == "observation" else None
                    if not isinstance(pl, dict) or not pl.get("available"):
                        continue
                    if pl.get("domain") == "scene.current_location":
                        found = dict(((pl.get("body") or {}).get("facts")) or {})
                    elif pl.get("domain") == "ui.context":
                        token = (((pl.get("body") or {}).get("facts")) or {}).get("location_id")
        finally:
            conn.close(); srv.close()
        if found is not None and token:
            # D14: indoors the pose says "Cabin" but the scene uses the unique token ("FarmHouse..."); ui.context
            # carries that token, like the runtime's D7 pin
            found["ui:context"] = {"kind": "ui_context", "location_id": token}
        return found

    def inspect(self, cx, cy, r):
        if self._cmd is None:
            sys.path.insert(0, str(paths.WORKTREE))
            import arti_stardew_command as command
            self._cmd = command
        res = self._cmd.minta_inspect_tiles(cx, cy, r, folder=str(paths.COMMAND_DIR))
        if not res.sukses:
            raise WalkError(2, f"inspect_tiles({cx},{cy},{r}) failed: status={res.status} outcome={res.outcome} alasan={getattr(res, 'alasan', None)}")
        return (res.mentah or {}).get("facts") or {}

    def _ui(self, *args):
        p = subprocess.run([sys.executable, os.path.join(HERE, "ui_op.py"), *map(str, args)], capture_output=True, text=True)
        if p.returncode != 0:
            raise WalkError(5, f"ui_op {' '.join(map(str, args))} failed: {p.stdout.strip()[-200:]} {p.stderr.strip()[-200:]}")
        return p.stdout

    def hold(self, key, ms):
        self._ui("hold", self.fh, key, int(ms))

    def key(self, name):
        self._ui("key", name, self.fh)

    def click(self, cx, cy):
        self._ui("click", self.fh, int(cx), int(cy))

    def move(self, cx, cy):
        self._ui("move", self.fh, int(cx), int(cy))

    def fg_host(self):
        out = self._ui("fg", self.host)          # ui_op exits 0 even when the foreground failed; check its log line
        if '"ok": true' not in out:
            raise WalkError(5, f"host foreground not confirmed: {out.strip()[-200:]}")

    def client_size(self):
        import ctypes, ctypes.wintypes as W
        r = W.RECT()
        ctypes.windll.user32.SetProcessDPIAware()
        if not ctypes.windll.user32.GetClientRect(int(self.fh), ctypes.byref(r)):
            return 1280, 720
        return r.right - r.left, r.bottom - r.top

    def sleep(self, s):
        time.sleep(s)


# ---------------------------------------------------------------- pure helpers
def pose_tile(p):
    return int(p["tile"]["x"]), int(p["tile"]["y"])


def scene_location(scene):
    for f in (scene or {}).values():
        if isinstance(f, dict) and f.get("kind") == "location":
            return f.get("location_id")
    return None


def footprint_tiles(scene):
    """Every tile covered by a building footprint (WS3a building_door facts)."""
    out = set()
    for f in (scene or {}).values():
        fp = f.get("footprint") if isinstance(f, dict) and f.get("kind") == "building_door" else None
        if isinstance(fp, dict):
            for dx in range(int(fp.get("width") or 0)):
                for dy in range(int(fp.get("height") or 0)):
                    out.add((int(fp["x"]) + dx, int(fp["y"]) + dy))
    return out


def scene_matches(scene, location):
    """True when the scene belongs to the pose's location: same id, the ui.context token (D14: "Cabin" vs
    "FarmHouse1a2b3c~..."), or the location fact's location_kind."""
    loc = scene_location(scene)
    if loc is None or loc == location:
        return True
    for f in (scene or {}).values():
        if isinstance(f, dict) and f.get("kind") == "ui_context" and f.get("location_id") == loc:
            return True
        if isinstance(f, dict) and f.get("kind") == "location" and f.get("location_kind") == location:
            return True
    return False


def door_tiles(scene):
    """Building door tile + the doorstep in the footprint's bottom row (the cabin doorstep (52,16) is inside the
    5x3 footprint 50,14 but walkable): allowed as the END of a walk, never routed through."""
    out = set()
    for f in (scene or {}).values():
        if isinstance(f, dict) and f.get("kind") == "building_door" and isinstance(f.get("tile"), dict):
            x, y = int(f["tile"]["x"]), int(f["tile"]["y"])
            out.add((x, y))
            fp = f.get("footprint")
            if isinstance(fp, dict):
                out.add((x, int(fp["y"]) + int(fp.get("height") or 1) - 1))
    return out


def hazard_tiles(location):
    return dict(KNOWN_HAZARDS.get(location) or {})


def hold_ms(n, tile_ms=None, single_ms=None):
    """Hold time for an n-tile leg: a single tile uses the calibrated SINGLE_TILE_MS, longer legs n * MS_PER_TILE."""
    tile_ms = MS_PER_TILE if tile_ms is None else tile_ms
    single_ms = SINGLE_TILE_MS if single_ms is None else single_ms
    return single_ms if n == 1 else n * tile_ms


def warp_tiles(scene):
    """Walk-onto warp source tiles a farmer triggers (npc_only warps excluded); may lie just outside the map."""
    out = set()
    for f in (scene or {}).values():
        if isinstance(f, dict) and f.get("kind") == "warp" and not f.get("npc_only") and isinstance(f.get("source_tile"), dict):
            out.add((int(f["source_tile"]["x"]), int(f["source_tile"]["y"])))
    return out


def warp_guard(segs, warps):
    """First leg (direction, n, end) that ends on a warp or points straight into one, else None."""
    for d, n, end in segs:
        nxt = (end[0] + DIRS[d][0], end[1] + DIRS[d][1])
        if end in warps or nxt in warps:
            return d, n, end, (end if end in warps else nxt)
    return None


def chebyshev(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def npc_tiles(facts):
    return {(int(f["tile"]["x"]), int(f["tile"]["y"])) for f in (facts or {}).values()
            if isinstance(f, dict) and f.get("kind") in ("npc", "monster") and isinstance(f.get("tile"), dict)}


# A stop counts as a bounding-box corner clip when the farmer is off its tile line perpendicular to the move. Aligned
# ranges measured over 80 live poses (pixel - tile*64): x mostly -8..8 (mode 0), y mostly -6..32 (modes 16 / 32).
CLIP_X = 16          # |x offset| beyond this on an up/down move = clip (HYPOTHESIS)
CLIP_Y = (-8, 40)    # y offset outside this on a left/right move = clip (HYPOTHESIS)


def transient_block(pose, d, nxt, npcs):
    """Why a zero-progress step must NOT be persisted as a map block (D40), or None: a character within one tile of the
    blocked tile (NPC traffic), or the farmer off the tile line perpendicular to the move (its bounding box clipped a
    diagonal neighbour's corner; pose pixel = Position, tile = ((px+32)//64, (py+16)//64))."""
    if any(chebyshev(nxt, n) <= 1 for n in npcs or ()):
        return "NPC traffic"
    tx, ty = pose_tile(pose)
    if d in ("up", "down"):
        off = pose["pixel"]["x"] - tx * 64
        clip = abs(off) > CLIP_X
    else:
        off = pose["pixel"]["y"] - ty * 64
        clip = not (CLIP_Y[0] <= off <= CLIP_Y[1])
    return f"diagonal clip ({off:+.0f}px off the tile line)" if clip else None


def facts_to_tiles(facts):
    """Return (blocked, passable) tile sets from inspect facts. Any blocking fact on a tile wins."""
    blocked, passable = set(), set()
    for f in (facts or {}).values():
        if not isinstance(f, dict) or not isinstance(f.get("tile"), dict):
            continue
        x, y = int(f["tile"]["x"]), int(f["tile"]["y"])
        if f.get("kind") == "clump":           # resource clumps (stump/boulder/log) are w x h and carry no `passable`
            for dx in range(int(f.get("width") or 2)):
                for dy in range(int(f.get("height") or 2)):
                    blocked.add((x + dx, y + dy))
        elif f.get("kind") == "npc":
            continue                           # moving; never a static obstacle
        elif f.get("passable") is False:
            blocked.add((x, y))
        elif f.get("passable") is True:
            passable.add((x, y))
    return blocked, passable - blocked


def bfs(start, goal, blocked, box):
    """Shortest 4-connected path start->goal (inclusive) avoiding `blocked`, inside box=(x0,y0,x1,y1). None if none.
    Uniform-cost search over (tile, heading) with cost (tiles, turns): among shortest routes, fewest segments."""
    if goal in blocked:
        return None
    if start == goal:
        return [start]
    x0, y0, x1, y1 = box
    inside = lambda t: x0 <= t[0] <= x1 and y0 <= t[1] <= y1
    TURN = 1.0 / 1024                     # a turn never outweighs one extra tile (paths are < 1024 tiles)
    best = {(start, None): 0.0}
    prev = {(start, None): None}
    heap = [(0.0, 0, start, None)]
    seq = 0
    while heap:
        cost, _, cur, head = heapq.heappop(heap)
        if cost > best.get((cur, head), float("inf")):
            continue
        if cur == goal:
            node, path = (cur, head), []
            while node is not None:
                path.append(node[0]); node = prev[node]
            return path[::-1]
        for d in DIRS.values():
            nxt = (cur[0] + d[0], cur[1] + d[1])
            if nxt in blocked or not inside(nxt):
                continue
            c = cost + 1 + (TURN if head is not None and d != head else 0)
            if c < best.get((nxt, d), float("inf")):
                best[(nxt, d)] = c; prev[(nxt, d)] = (cur, head)
                seq += 1; heapq.heappush(heap, (c, seq, nxt, d))
    return None


def segments(path):
    """[(direction, n_tiles, end_tile)] for consecutive same-direction steps."""
    out = []
    for a, b in zip(path, path[1:]):
        d = next(k for k, v in DIRS.items() if v == (b[0] - a[0], b[1] - a[1]))
        if out and out[-1][0] == d:
            out[-1] = (d, out[-1][1] + 1, b)
        else:
            out.append((d, 1, b))
    return out


def plan_box(start, goal, location):
    x0 = max(0, min(start[0], goal[0]) - PLAN_MARGIN); y0 = max(0, min(start[1], goal[1]) - PLAN_MARGIN)
    x1 = max(start[0], goal[0]) + PLAN_MARGIN; y1 = max(start[1], goal[1]) + PLAN_MARGIN
    if location in MAP_SIZES:
        w, h = MAP_SIZES[location]; x1 = min(x1, w - 1); y1 = min(y1, h - 1)
    return x0, y0, x1, y1


class World:
    """Accumulated knowledge: facts from inspect windows + tiles learned blocked by zero progress."""

    def __init__(self, game, log):
        self.game, self.log = game, log
        self.blocked, self.passable, self.learned = set(), set(), set()
        self.static = set()          # building footprints (+ warp tiles with --stop-before-warp) from the scene
        self.scene_facts = None      # scene snapshot facts: the fallback when inspect_tiles is refused (D21)
        self.npcs = set()            # character tiles seen (scene / inspect): blocks there are traffic, not map (D40)
        self.scene_only = False
        self.covered = set()
        self.inspects = 0

    def add_window(self, cx, cy):
        cx, cy = max(0, cx), max(0, cy)
        r = INSPECT_RADIUS
        facts = None
        if not self.scene_only:
            try:
                facts = self.game.inspect(cx, cy, r)
            except WalkError as e:
                # D21 (live B4): after ~256 legacy inspects in one farmhand session the mod refuses every inspect
                # (CommandSessionCapacityReached). Plan from the scene snapshot from here on (truncated, nearest facts).
                self.scene_only = True
                self.log(f"inspect refused ({e}); planning from the scene snapshot only from here on")
        if facts is None:
            facts = {k: f for k, f in (self.scene_facts or {}).items() if isinstance(f, dict)
                     and isinstance(f.get("tile"), dict) and abs(f["tile"]["x"] - cx) <= r and abs(f["tile"]["y"] - cy) <= r}
        self.inspects += 1
        b, p = facts_to_tiles(facts)
        self.npcs |= npc_tiles(facts)
        window = {(x, y) for x in range(cx - r, cx + r + 1) for y in range(cy - r, cy + r + 1)}
        self.passable = (self.passable - b) | p
        self.blocked = (self.blocked - (window - b)) | b    # fresh window replaces stale facts inside it
        self.covered |= window
        self.log(f"inspect ({cx},{cy}) r{r}: {len(facts)} facts, {len(b)} blocked in window")

    def all_blocked(self, start):
        return (self.blocked | self.learned | self.static) - {start}

    def plan(self, start, goal, location, max_len):
        """BFS, inspecting windows along the route until every path tile is covered (bounded by MAX_INSPECTS)."""
        box = plan_box(start, goal, location)
        while True:
            path = bfs(start, goal, self.all_blocked(start), box)
            if path is None:
                raise WalkError(3, f"no route {start}->{goal} inside box {box} (blocked facts {len(self.blocked)}, learned {sorted(self.learned)})")
            if len(path) - 1 > max_len:
                raise WalkError(4, f"route {start}->{goal} is {len(path) - 1} tiles > remaining budget {max_len}")
            uncovered = [i for i, t in enumerate(path) if t not in self.covered]
            if not uncovered or self.inspects >= MAX_INSPECTS:
                if uncovered:
                    self.log(f"inspect budget {MAX_INSPECTS} used; {len(uncovered)} route tiles stay unknown (optimistic)")
                return path
            i = uncovered[0]
            cx, cy = path[min(i + INSPECT_RADIUS, len(path) - 1)]   # window centre ahead, still covering path[i]
            self.add_window(cx, cy)


def check_pose(p, location):
    if p.get("location") != location:
        raise WalkError(5, f"location changed: {location} -> {p.get('location')}")
    if p.get("is_player_free") is False or p.get("active_menu"):
        raise WalkError(5, f"player not free (is_player_free={p.get('is_player_free')}, active_menu={p.get('active_menu')})")


def walk(game, goal, face=None, plan_only=False, log=print, max_replans=MAX_REPLANS, max_tiles=MAX_TILES,
         stop_before_warp=False, tiles_per_hold=None, tile_ms=None, single_ms=None, scene_only=False, store=None):
    """Walk to `goal` (see _walk). With a LearnedStore: tiles learned blocked are loaded and saved per location, and a
    live walk that ends with exit 4 records the goal as failed (D37)."""
    ctx = {}
    try:
        return _walk(game, goal, face, plan_only, log, max_replans, max_tiles, stop_before_warp, tiles_per_hold,
                     tile_ms, single_ms, scene_only, store, ctx)
    except WalkError as e:
        # D40: only a live walk that moved and failed (replan / tile budget) marks the goal: a planning budget refusal
        # before any input (e.g. a Farm goal requested from inside the Cabin) or NPC traffic on the route does not.
        npc_near = any(chebyshev(t, n) <= 1 for t in ctx.get("path") or () for n in ctx.get("npcs") or ())
        if (e.code == 4 and store is not None and not plan_only and ctx.get("location") and ctx.get("holds")
                and not npc_near):
            store.fail_goal(ctx["location"], goal, str(e))
            log(f"goal {goal} recorded as failed in {store.path.name} under {ctx['location']} (exit 4)")
        elif e.code == 4 and store is not None and not plan_only:
            log(f"goal {goal} not recorded ({'NPC traffic on the route' if npc_near else 'no input was sent'})")
        raise


def _walk(game, goal, face, plan_only, log, max_replans, max_tiles, stop_before_warp, tiles_per_hold, tile_ms, single_ms,
          scene_only, store, ctx):
    """Walk to `goal`. Returns a result dict; raises WalkError on failure. Does NOT foreground the host (caller does)."""
    p = game.pose()
    location = p.get("location")
    check_pose(p, location)
    start = pose_tile(p)
    log(f"pose {location} {start} px=({p['pixel']['x']:.1f},{p['pixel']['y']:.1f}) facing={FACING_CODE.get(p.get('facing'))}")
    world = World(game, log)
    ctx["location"] = location
    ctx["npcs"] = world.npcs
    if store is not None:
        known = store.blocked(location)
        world.learned |= known
        if known:
            log(f"{len(known)} tile(s) learned blocked earlier in {location} ({store.path.name})")
    scene = game.scene() if hasattr(game, "scene") else None
    if scene is not None and not scene_matches(scene, location):
        log(f"scene is for {scene_location(scene)}, not {location}: ignored")
        scene = None
    if stop_before_warp and scene is None:
        raise WalkError(2, "--stop-before-warp needs the scene observation (warp list); none received")
    world.static = footprint_tiles(scene)
    world.scene_facts = scene
    world.npcs |= npc_tiles(scene)
    world.scene_only = bool(scene_only)
    warps = warp_tiles(scene) if stop_before_warp else set()
    world.static |= warps
    if scene is not None:
        log(f"scene: {len(footprint_tiles(scene))} footprint tiles blocked" + (f", {len(warps)} warp tiles blocked" if stop_before_warp else ""))
    hz = hazard_tiles(location)
    if goal in hz:
        raise WalkError(3, f"goal {goal} is a known hazard: {hz[goal]}")
    world.static |= set(hz)
    if goal in world.static and goal in door_tiles(scene) and goal not in warps:
        world.static.discard(goal)                   # a doorstep / door tile may END a walk (B3W4a: (52,16))
        log(f"goal {goal} is a building door tile: allowed as the end of the walk")
    if goal in world.static:
        raise WalkError(3, f"goal {goal} is inside a building footprint" + (" or on a warp" if goal in warps else ""))
    budget = max_tiles
    replans = 0
    path = world.plan(start, goal, location, budget)
    ctx["path"] = path
    log(f"plan {len(path) - 1} tiles: " + " ".join(f"{d}x{n}" for d, n, _ in segments(path)))
    bad = warp_guard(segments(path), warps) if warps else None
    if bad:
        raise WalkError(3, f"stop-before-warp: leg {bad[0]}x{bad[1]} ends at {bad[2]} on/next to warp {bad[3]}")
    if plan_only:
        return dict(ok=True, plan_only=True, start=start, path=path, segments=segments(path), blocked=sorted(world.blocked))
    cur = start
    walked = []
    while cur != goal:
        d, n, end = segments(path)[0]
        if tiles_per_hold and n > tiles_per_hold:     # --tiles N: precise mode, at most N tiles per hold
            n = tiles_per_hold
            end = path[n]
        bad = warp_guard([(d, n, end)], warps) if warps else None
        if bad:
            raise WalkError(3, f"stop-before-warp: leg {d}x{n} ends at {end} on/next to warp {bad[3]}; not sent")
        if n > budget:
            raise WalkError(4, f"tile budget exhausted ({max_tiles} commanded); at {cur}, goal {goal}")
        before_px = (p["pixel"]["x"], p["pixel"]["y"])
        game.hold(KEYS[d], hold_ms(n, tile_ms, single_ms))
        ctx["holds"] = ctx.get("holds", 0) + 1
        budget -= n
        game.sleep(SETTLE_S)
        p = game.pose(); check_pose(p, location)
        new = pose_tile(p)
        dx, dy = DIRS[d]
        moved_px = (p["pixel"]["x"] - before_px[0]) * dx + (p["pixel"]["y"] - before_px[1]) * dy
        walked.append((d, n, cur, new))
        log(f"hold {d} {n} tile(s) {cur}->{new} (expected {end})")
        if new == end:
            cur = new; path = path[path.index(end):]
            continue
        # deviation: (almost) zero pixel progress along d means the next tile is blocked (map tile / building / debris)
        if new == cur and moved_px < 16:
            nxt = (cur[0] + dx, cur[1] + dy)
            world.learned.add(nxt)
            transient = transient_block(p, d, nxt, world.npcs)
            if store is not None and not transient:
                store.add_blocked(location, [nxt])
            log(f"no progress {d} from {cur} ({moved_px:.0f}px): learned blocked {nxt}"
                + (f" for this walk only ({transient}; not persisted)" if transient else ""))
        cur = new
        if cur == goal:
            break
        replans += 1
        if replans > max_replans:
            raise WalkError(4, f"replan limit {max_replans} exceeded; at {cur}, goal {goal}")
        path = world.plan(cur, goal, location, budget)
        ctx["path"] = path
        log(f"replan {replans}: {len(path) - 1} tiles: " + " ".join(f"{dd}x{nn}" for dd, nn, _ in segments(path)))
    if face:
        game.hold(KEYS[face], FACE_TAP_MS)
        game.sleep(SETTLE_S)
        p = game.pose(); check_pose(p, location)
        log(f"face tap {face}: now {pose_tile(p)} facing={FACING_CODE.get(p.get('facing'))}")
        if pose_tile(p) != goal:
            raise WalkError(5, f"face tap moved the player off the goal: {pose_tile(p)}")
    return dict(ok=True, start=start, end=pose_tile(p), facing=FACING_CODE.get(p.get("facing")), replans=replans,
                tiles_commanded=max_tiles - budget, walked=walked, pixel=p.get("pixel"))


def main(argv=None, game=None, store=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tx", type=int); ap.add_argument("ty", type=int)
    ap.add_argument("--face", choices=sorted(DIRS))
    ap.add_argument("--plan-only", action="store_true", help="read pose + inspect + print the plan; send no input")
    ap.add_argument("--scene-only", action="store_true",
                    help="plan from the scene snapshot only, no inspect_tiles (automatic when inspect is refused, D21)")
    ap.add_argument("--tiles", type=int, metavar="N", help="precise mode: at most N tiles per hold, pose readback after each")
    ap.add_argument("--tile-ms", type=int, help=f"ms per tile for multi-tile legs (default {MS_PER_TILE})")
    ap.add_argument("--single-ms", type=int, help=f"ms for a 1-tile leg (default {SINGLE_TILE_MS}, live-calibrated)")
    ap.add_argument("--stop-before-warp", action="store_true",
                    help="block scene warp tiles and refuse a leg ending on / pointing into a warp (outside the Farm)")
    a = ap.parse_args(argv)
    if game is None:
        game = LiveGame()
        store = store if store is not None else default_store()
    rc = 0
    try:
        if a.tiles is not None and a.tiles < 1:
            raise WalkError(2, "--tiles must be >= 1")
        res = walk(game, (a.tx, a.ty), a.face, a.plan_only, stop_before_warp=a.stop_before_warp, tiles_per_hold=a.tiles,
                   tile_ms=a.tile_ms, single_ms=a.single_ms, scene_only=a.scene_only, store=store)
        print("RESULT", json.dumps({k: v for k, v in res.items() if k not in ("walked",)}, default=str))
    except WalkError as e:
        print(f"FAIL ({e.code}): {e}", file=sys.stderr); rc = e.code
    finally:
        if not a.plan_only:
            try:
                game.fg_host()
            except Exception as e:      # keep the original failure code; report the fg problem loudly
                print(f"WARNING: host foreground failed: {e}", file=sys.stderr); rc = rc or 5
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
