"""Offline tests for scripts/m1_operator/walk_to.py and place_item.py with a fake game (no socket, no inspect inbox,
no ui_op, no windows). Moved from the live prep folder's tools/test_walk_to.py unchanged except for the import path.

Run: ./venv/Scripts/python.exe -m pytest tests/test_m1_operator_walk_to.py -q
"""
import io, os, sys, unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "m1_operator"))
import walk_to as W  # noqa: E402
import place_item as P  # noqa: E402

FACE = {"w": 0, "d": 1, "s": 2, "a": 3}
DELTA = {"w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0)}


class FakeGame:
    """Tile world. `walls` are reported by inspect; `hidden` block movement but never appear as facts (buildings).
    `slips` = per-hold tile shortfall (deviation injection); `frozen` = nothing ever moves."""

    def __init__(self, start, walls=(), hidden=(), slips=(), frozen=False, location="Farm", hotbar=None):
        self.x, self.y = start
        self.subx = 0.0
        self.walls, self.hidden = set(walls), set(hidden)
        self.slips = list(slips)
        self.frozen = frozen
        self.location = location
        self.facing = 2
        self.objects = {}
        self.inputs, self.inspects = [], []
        self.hotbar = hotbar or [{"slot": 0, "name": "Axe", "stack": 1, "is_tool": True},
                                 {"slot": 9, "name": "Chest", "stack": 1, "is_tool": False}]

    # pose: pixel chosen so that ((px+32)//64, (py+16)//64) == tile
    def pose(self):
        return {"location": self.location, "tile": {"x": self.x, "y": self.y},
                "pixel": {"x": self.x * 64 + self.subx, "y": self.y * 64.0}, "facing": self.facing,
                "is_player_free": True, "active_menu": None, "inventory": {"hotbar": self.hotbar}}

    def inspect(self, cx, cy, r):
        self.inspects.append((cx, cy, r))
        facts = {}
        for x in range(cx - r, cx + r + 1):
            for y in range(cy - r, cy + r + 1):
                if (x, y) in self.walls:
                    facts[f"obj:Farm:{x},{y}:(O)450"] = {"kind": "object", "tile": {"x": x, "y": y}, "passable": False}
                if (x, y) in self.objects:
                    facts[f"obj:Farm:{x},{y}:{self.objects[(x, y)]}"] = {"kind": "object", "tile": {"x": x, "y": y}, "passable": False}
        return facts

    def blocked(self, t):
        return t in self.walls or t in self.hidden or t in self.objects

    def hold(self, key, ms):
        self.inputs.append(("hold", key, ms))
        self.facing = FACE[key]
        if self.frozen or ms < 100:
            return
        n = round(ms / W.MS_PER_TILE)
        if self.slips:
            n = max(0, n - self.slips.pop(0))
        dx, dy = DELTA[key]
        for _ in range(n):
            nxt = (self.x + dx, self.y + dy)
            if self.blocked(nxt):
                break
            self.x, self.y = nxt

    def key(self, name):
        self.inputs.append(("key", name))
        if name.isdigit():
            self.active = P.hotbar_index(name)

    def _place(self, cx, cy):
        t = P.client_to_tile(self.pose(), cx, cy, self.client_size())
        if max(abs(t[0] - self.x), abs(t[1] - self.y)) <= 1 and not self.blocked(t) and t != (self.x, self.y):
            item = next(s for s in self.hotbar if s["slot"] == self.active)
            self.objects[t] = "(BC)130"; item["stack"] -= 1

    def click(self, cx, cy):
        self.inputs.append(("click", cx, cy)); self._place(cx, cy)

    def move(self, cx, cy):
        self.inputs.append(("move", cx, cy)); self.cursor = (cx, cy)

    def fg_host(self):
        self.inputs.append(("fg_host",))

    def client_size(self):
        return 1280, 720

    def sleep(self, s):
        pass


def quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **k)


ROW = {(x, 5) for x in range(0, 30) if x != 12}   # blocked row y=5 with one gap at x=12


class TestBfs(unittest.TestCase):
    def test_routes_around_blocked_row(self):
        path = W.bfs((5, 2), (5, 8), ROW, (0, 0, 29, 15))
        self.assertIsNotNone(path)
        self.assertIn((12, 5), path)                      # must go through the gap
        self.assertFalse(set(path) & ROW)
        self.assertEqual(len(path) - 1, 7 + 6 + 7)        # right 7, down 6, left 7 (shortest)

    def test_unknown_tiles_are_optimistic(self):
        self.assertEqual(len(W.bfs((0, 0), (4, 0), set(), (0, 0, 10, 10))) - 1, 4)

    def test_blocked_goal_and_box(self):
        self.assertIsNone(W.bfs((0, 0), (3, 3), {(3, 3)}, (0, 0, 9, 9)))
        self.assertIsNone(W.bfs((5, 2), (5, 8), ROW | {(12, 5)}, (0, 0, 29, 15)))

    def test_segments_compress_same_direction(self):
        segs = W.segments(W.bfs((5, 2), (5, 8), ROW, (0, 0, 29, 15)))
        self.assertEqual([(d, n) for d, n, _ in segs], [("right", 7), ("down", 6), ("left", 7)])

    def test_clump_facts_block_2x2(self):
        b, _ = W.facts_to_tiles({"clump:Farm:3,3:600": {"kind": "clump", "tile": {"x": 3, "y": 3}, "width": 2, "height": 2}})
        self.assertEqual(b, {(3, 3), (4, 3), (3, 4), (4, 4)})


class TestWalk(unittest.TestCase):
    def test_walks_around_blocked_row_end_to_end(self):
        g = FakeGame((5, 2), walls=ROW)
        rc = quiet(W.main, ["5", "8", "--face", "up"], game=g)
        self.assertEqual(rc, 0)
        self.assertEqual((g.x, g.y), (5, 8))
        holds = [i for i in g.inputs if i[0] == "hold"]
        self.assertEqual(holds, [("hold", "d", 7 * 215), ("hold", "s", 6 * 215), ("hold", "a", 7 * 215), ("hold", "w", 40)])
        self.assertEqual(g.facing, 0)
        self.assertEqual(g.inputs[-1], ("fg_host",))
        self.assertTrue(all(abs(cx - 5) <= 12 and r == 4 for cx, cy, r in g.inspects))

    def test_plan_only_sends_no_input(self):
        g = FakeGame((5, 2), walls=ROW)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = W.main(["5", "8", "--plan-only"], game=g)
        self.assertEqual(rc, 0)
        self.assertEqual(g.inputs, [])
        self.assertIn("plan 20 tiles: rightx7 downx6 leftx7", out.getvalue())

    def test_replans_after_deviation(self):
        g = FakeGame((0, 0), slips=[2])                   # first hold comes up 2 tiles short
        res = quiet(W.walk, g, (6, 3))
        self.assertEqual((g.x, g.y), (6, 3))
        self.assertEqual(res["replans"], 1)
        self.assertEqual(res["tiles_commanded"], 9 + 2)   # 9-tile route + the 2 tiles lost to the slip

    def test_learns_hidden_blocker_and_replans(self):
        hidden = {(x, 3) for x in range(1, 4)}            # a 3-wide 'building': no facts, discovered by zero progress
        g = FakeGame((2, 1), hidden=hidden)
        res = quiet(W.walk, g, (2, 5))
        self.assertEqual((g.x, g.y), (2, 5))
        self.assertGreaterEqual(res["replans"], 1)
        self.assertTrue(any(h[1] in "ad" for h in g.inputs if h[0] == "hold"))  # detoured sideways around it

    def test_wide_hidden_wall_hits_replan_bound(self):
        g = FakeGame((2, 1), hidden={(x, 3) for x in range(0, 12)})   # needs > 6 one-tile probes
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = W.main(["2", "5"], game=g)
        self.assertEqual(rc, 4)
        self.assertIn("replan limit 6 exceeded", out.getvalue())
        self.assertEqual(out.getvalue().count("replan "), W.MAX_REPLANS + 1)   # 6 'replan N:' lines + the failure
        self.assertLessEqual(sum(i[2] for i in g.inputs if i[0] == "hold") / W.MS_PER_TILE, W.MAX_TILES)
        self.assertEqual(g.inputs[-1], ("fg_host",))

    def test_replan_limit_bounded(self):
        g = FakeGame((0, 0), frozen=True)
        rc = quiet(W.main, ["5", "0"], game=g)
        self.assertNotEqual(rc, 0)
        holds = [i for i in g.inputs if i[0] == "hold"]
        self.assertLessEqual(len(holds), W.MAX_REPLANS + 1)
        self.assertEqual(g.inputs[-1], ("fg_host",))      # host foregrounded even on failure

    def test_tile_budget_bounded(self):
        g = FakeGame((0, 0))
        rc = quiet(W.main, ["40", "30"], game=g)          # 70 tiles > 60
        self.assertEqual(rc, 4)
        self.assertEqual([i for i in g.inputs if i[0] == "hold"], [])
        self.assertEqual(g.inputs, [("fg_host",)])

    def test_repeated_slips_exhaust_replans(self):
        g = FakeGame((0, 0), slips=[1] * 20)              # every hold falls one tile short
        with self.assertRaises(W.WalkError) as cm:
            quiet(W.walk, g, (0, 12))
        self.assertIn(cm.exception.code, (3, 4))          # replan limit, or the never-yielding tile learned blocked
        self.assertLessEqual(len(g.inputs), W.MAX_REPLANS + 1)

    def test_no_route(self):
        g = FakeGame((5, 2), walls=ROW | {(12, 5)})
        self.assertEqual(quiet(W.main, ["5", "8"], game=g), 3)

    def test_location_change_aborts(self):
        g = FakeGame((0, 0))
        orig = g.hold
        def hold(key, ms):
            orig(key, ms); g.location = "BusStop"
        g.hold = hold
        with self.assertRaises(W.WalkError) as cm:
            quiet(W.walk, g, (3, 0))
        self.assertEqual(cm.exception.code, 5)


class TestPlace(unittest.TestCase):
    def test_mapping_matches_live_click_0258(self):
        # 2026-10-07 02:58:35: click client (690,285), pose pixel (3534.31,1738.71) facing right -> chest at (56,26)
        p = {"location": "Farm", "pixel": {"x": 3534.313720703125, "y": 1738.7061767578125}, "facing": 1}
        self.assertEqual(P.client_to_tile(p, 690, 285), (56, 26))
        cx, cy, _ = P.tile_to_client(p, 56, 26)
        self.assertEqual(P.client_to_tile(p, cx, cy), (56, 26))
        self.assertEqual(P.grab_tile(p), (56, 27))

    def test_mapping_clamps_at_map_edge(self):
        p = {"location": "Farm", "pixel": {"x": 64.0, "y": 64.0}, "facing": 2}
        _, _, vp = P.tile_to_client(p, 1, 1)
        self.assertEqual(vp, (0, 0))

    def test_place_reports_actual_tile(self):
        g = FakeGame((55, 27))
        rc = quiet(P.main, ["0", "56", "26"], game=g)
        self.assertEqual(rc, 0)
        self.assertIn((56, 26), g.objects)
        kinds = [i[0] if i[0] != "key" else ("key", i[1]) for i in g.inputs]
        self.assertEqual(kinds, [("key", "0"), "click", ("key", "1"), "fg_host"])

    def test_place_refuses_far_target_without_walk(self):
        g = FakeGame((50, 20))
        self.assertEqual(quiet(P.main, ["0", "56", "26"], game=g), 2)
        self.assertEqual(g.objects, {})

    def test_place_with_walk(self):
        g = FakeGame((50, 20))
        self.assertEqual(quiet(P.main, ["0", "56", "26", "--walk"], game=g), 0)
        self.assertIn((56, 26), g.objects)

    def test_place_refuses_occupied_target(self):
        g = FakeGame((55, 27), walls={(56, 26)})
        self.assertEqual(quiet(P.main, ["0", "56", "26"], game=g), 2)

    def test_place_detects_miss(self):
        g = FakeGame((55, 27))
        orig = g.click
        g.click = lambda cx, cy: orig(cx - 64, cy)          # game puts it one tile west of the target
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = P.main(["0", "56", "26"], game=g)
        self.assertEqual(rc, 3)
        self.assertEqual(set(g.objects), {(55, 26)})
        self.assertIn("placed at [(55, 26)], NOT at target (56, 26)", out.getvalue())
        self.assertEqual(g.inputs[-2:], [("key", "1"), ("fg_host",)])

    def test_place_nothing_placed(self):
        g = FakeGame((55, 27))
        g.click = lambda cx, cy: g.inputs.append(("click", cx, cy))
        self.assertEqual(quiet(P.main, ["0", "56", "26"], game=g), 4)
        self.assertEqual(g.inputs[-2:], [("key", "1"), ("fg_host",)])

    def test_dry_run_sends_no_input(self):
        g = FakeGame((55, 27))
        self.assertEqual(quiet(P.main, ["0", "56", "26", "--dry-run"], game=g), 0)
        self.assertEqual(g.inputs, [])


if __name__ == "__main__":
    unittest.main()
