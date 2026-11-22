"""Offline tests for the batch-2 operator follow-ups: building footprints from the scene (pick_tree D8, find_layout,
walk_to), walk_to --stop-before-warp (B2W6: walk_to 10 24 crossed the BusStop warp at x=9), and sweep.py
(Stone / Weeds / Forage / Water). Fake game only: no socket, no inbox, no input. Run:
  ./venv/Scripts/python.exe -m pytest tests/test_m1_operator_scene_guards.py -q
"""
import io, json, os, sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "m1_operator"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_layout as F  # noqa: E402
import pick_tree as P  # noqa: E402
import sweep as S  # noqa: E402
import walk_to as W  # noqa: E402
from test_m1_operator_walk_to import FakeGame as WalkGame  # noqa: E402

CABIN = {"bdoor:Farm:52,15:FarmHouse1": {"kind": "building_door", "tile": {"x": 52, "y": 15},
                                         "footprint": {"x": 50, "y": 13, "width": 5, "height": 3},
                                         "target_location": "FarmHouse1", "enterable": True}}
LOC = {"loc:Farm": {"kind": "location", "location_id": "Farm", "map_width": 80, "map_height": 65}}


def terrain(x, y, **kw):
    return f"tf:Farm:{x},{y}:Grass", dict({"kind": "terrain", "tile": {"x": x, "y": y}, "passable": True}, **kw)


def tree(x, y):
    return f"tf:Farm:{x},{y}:Tree", {"kind": "terrain", "tile": {"x": x, "y": y}, "passable": False,
                                     "wild_tree": {"growth_stage": 5, "stump": False, "tapped": False}}


def obj(x, y, name, **kw):
    return f"obj:Farm:{x},{y}:{name}", dict({"kind": "object", "name": name, "tile": {"x": x, "y": y}, "passable": False}, **kw)


class SceneGame(WalkGame):
    """walk_to's fake game + facts for inspect + a scene (footprints are hidden walls, like real buildings)."""

    def __init__(self, start, facts=(), scene=None, **kw):
        sc = dict(LOC, **(scene or {}))
        hidden = W.footprint_tiles(sc)
        super().__init__(start, hidden=hidden, **kw)
        self.extra = dict(facts)
        self._scene = sc

    def inspect(self, cx, cy, r):
        out = super().inspect(cx, cy, r)
        out.update({k: f for k, f in self.extra.items()
                    if abs(f["tile"]["x"] - cx) <= r and abs(f["tile"]["y"] - cy) <= r})
        return out

    def scene(self):
        return self._scene


# ---------------------------------------------------------------- D8: footprints
def grass_around(x, y, r=3):
    return dict(terrain(i, j) for i in range(x - r, x + r + 1) for j in range(y - r, y + r + 1))


def test_pick_tree_never_proposes_a_stand_inside_a_building():
    facts = grass_around(50, 12)
    facts.update([tree(50, 12)])
    g = SceneGame((46, 12), facts, CABIN)
    assert (50, 13) in W.footprint_tiles(CABIN)
    c = P.candidates(facts, 45, 12, W.footprint_tiles(CABIN))
    assert all(tuple(s) != (50, 13) for _, s, _ in c)
    res = P.pick(g, 45, 12)
    assert res and res[0] != (50, 13) and res[0] not in W.footprint_tiles(CABIN)
    assert not g.inputs


def test_pick_tree_exit_3_when_every_stand_is_inside_and_refuses_unknown_process_list():
    facts = {k: v for k, v in grass_around(51, 14, 1).items()}
    facts.update([tree(51, 14)])                                   # tree surrounded by the footprint
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        assert P.main(["45", "12"], game=SceneGame((46, 12), facts, CABIN)) == 3
        assert P.main(["45", "12"], trial_check=lambda: None) == 2


def test_find_layout_excludes_footprint_stands_and_chest_spots():
    facts = grass_around(50, 12)
    facts.update([tree(50, 12)])
    for c in F.candidates(facts, (46, 12), W.footprint_tiles(CABIN)):
        assert tuple(c["stand"]) not in W.footprint_tiles(CABIN)
        assert all(tuple(s) not in W.footprint_tiles(CABIN) for s in c["chest_spots"])


def test_walk_plan_routes_around_a_footprint():
    g = SceneGame((48, 14), scene=CABIN)
    res = W.walk(g, (56, 14), plan_only=True, log=lambda m: None)
    assert not set(map(tuple, res["path"])) & W.footprint_tiles(CABIN)
    with pytest.raises(W.WalkError) as e:
        W.walk(g, (52, 14), plan_only=True, log=lambda m: None)
    assert e.value.code == 3 and "footprint" in str(e.value)


# ---------------------------------------------------------------- --stop-before-warp
BUSSTOP_WARPS = {f"warp:BusStop:9,{y}->Farm:79,{y}": {"kind": "warp", "source_tile": {"x": 9, "y": y}, "target_location": "Farm"}
                 for y in (22, 23, 24, 25)}


def busstop(start):
    g = SceneGame(start, scene=BUSSTOP_WARPS, location="BusStop")
    g._scene = dict(BUSSTOP_WARPS, **{"loc:BusStop": {"kind": "location", "location_id": "BusStop"}})
    return g


def test_stop_before_warp_refuses_a_leg_pointing_into_a_warp():
    g = busstop((14, 24))
    with pytest.raises(W.WalkError) as e:
        W.walk(g, (10, 24), "left", log=lambda m: None, stop_before_warp=True)
    assert e.value.code == 3 and "warp (9, 24)" in str(e.value) and not g.inputs      # refused before any hold


def test_stop_before_warp_blocks_crossing_and_needs_the_scene():
    g = busstop((10, 21))
    res = W.walk(g, (10, 27), plan_only=True, log=lambda m: None, stop_before_warp=True)
    assert all(t[0] != 9 for t in res["path"])
    with pytest.raises(W.WalkError) as e:
        W.walk(g, (9, 23), plan_only=True, log=lambda m: None, stop_before_warp=True)
    assert e.value.code == 3
    plain = WalkGame((14, 24), location="BusStop")                  # no scene method
    with pytest.raises(W.WalkError) as e:
        W.walk(plain, (12, 24), plan_only=True, log=lambda m: None, stop_before_warp=True)
    assert e.value.code == 2


def test_without_the_flag_warps_are_not_blocked():
    g = busstop((14, 24))
    res = W.walk(g, (10, 24), plan_only=True, log=lambda m: None)
    assert res["ok"]


# ---------------------------------------------------------------- sweep.py
def test_sweep_stone_weeds_and_forage():
    facts = grass_around(20, 20, 4)
    facts.update([obj(22, 20, "Stone"), obj(18, 20, "Weeds"), obj(20, 23, "Daffodil", spawned=True),
                  obj(24, 22, "Chest", qualified_item_id="(BC)130")])
    st = S.sweep("Stone", facts, LOC, (20, 20))
    assert [r["tile"] for r in st] == [[22, 20]] and st[0]["stands"]
    fo = S.sweep("Forage", facts, LOC, (20, 20))
    assert [r["name"] for r in fo] == ["Daffodil"]
    sc = dict(LOC, **{"o": {"kind": "object", "name": "Leek", "tile": {"x": 30, "y": 20}, "spawned": True}})
    assert {r["name"] for r in S.sweep("Forage", facts, sc, (20, 20))} == {"Daffodil", "Leek"}


def test_sweep_water_uses_the_gather_window_and_skips_footprints():
    scene = dict(LOC, **{"gather:nearby": {"kind": "gather_window", "tiles": [
        {"x": 30, "y": 10, "water_refill": True, "tillable": False, "villager": False},
        {"x": 31, "y": 10, "water_refill": False, "tillable": True, "villager": False}]}})
    w = S.sweep("Water", {}, scene, (31, 11))
    assert [r["tile"] for r in w] == [[30, 10]] and [30, 11, "up"] in w[0]["stands"]
    facts = grass_around(51, 16, 1)
    facts.update([obj(51, 17, "Stone")])
    st = S.sweep("Stone", facts, dict(CABIN, **LOC), (51, 18))
    assert all(tuple(s[:2]) not in W.footprint_tiles(CABIN) for s in st[0]["stands"])


def test_sweep_main_refuses_unknown_and_prints_json():
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        assert S.main(["Stone"], trial_check=lambda: None) == 2
    facts = grass_around(20, 20, 4)
    facts.update([obj(22, 20, "Stone")])
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        assert S.main(["Stone", "8"], game=SceneGame((20, 20), facts)) == 0
        assert S.main(["Water"], game=SceneGame((20, 20), facts)) == 3
    line = [l for l in out.getvalue().splitlines() if l.startswith("{")][0]
    assert json.loads(line)["tile"] == [22, 20]
