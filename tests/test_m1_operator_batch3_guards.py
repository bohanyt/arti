"""Offline tests for the batch-3 operator follow-ups: D14 (indoor scene token vs the pose's "Cabin"), the cabin doorstep
as a walk end, the calibrated 1-tile hold + --tiles N precise mode, and KNOWN_HAZARDS (BusStop bus door (22,8), the 1.6
pet bowl (53-54,7-8)) in walk_to / pick_tree / find_layout / sweep / the soak park. Fake game only. Run:
  ./venv/Scripts/python.exe -m pytest tests/test_m1_operator_batch3_guards.py -q
"""
import os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "scripts", "m1_operator"))
sys.path.insert(0, HERE)
import find_layout as F  # noqa: E402
import pick_tree as P  # noqa: E402
import soak as SK  # noqa: E402
import sweep as S  # noqa: E402
import walk_to as W  # noqa: E402
from test_m1_operator_scene_guards import SceneGame, grass_around, obj, tree  # noqa: E402

TOKEN = "FarmHouse1a2b3c~0123456789abcdef"
CABIN_LIVE = {"bdoor:Farm:52,15:" + TOKEN: {"kind": "building_door", "tile": {"x": 52, "y": 15},
                                            "footprint": {"x": 50, "y": 14, "width": 5, "height": 3},
                                            "target_location": TOKEN, "enterable": True}}
LOG = dict(log=lambda m: None)


# ---------------------------------------------------------------- D14
def indoor_scene(with_token=True, kind=None):
    sc = {"loc:" + TOKEN: {"kind": "location", "location_id": TOKEN, "location_kind": kind or "FarmHouse"},
          "warp:" + TOKEN + ":3,12->Farm:52,16": {"kind": "warp", "source_tile": {"x": 3, "y": 12}, "target_location": "Farm"}}
    if with_token:
        sc["ui:context"] = {"kind": "ui_context", "location_id": TOKEN}
    return sc


def test_d14_indoor_scene_matches_through_the_ui_context_token():
    assert W.scene_matches(indoor_scene(), "Cabin")
    assert W.scene_matches(indoor_scene(False, kind="Cabin"), "Cabin")           # location_kind fallback
    assert not W.scene_matches(indoor_scene(False), "Cabin")
    g = SceneGame((8, 8), location="Cabin")
    g._scene = indoor_scene()
    res = W.walk(g, (5, 8), plan_only=True, stop_before_warp=True, **LOG)        # was exit 2 "scene is for FarmHouse..."
    assert res["ok"]
    with pytest.raises(W.WalkError) as e:
        W.walk(g, (3, 11), "down", stop_before_warp=True, **LOG)                  # leg into the exit warp (3,12)
    assert e.value.code == 3


# ---------------------------------------------------------------- doorstep
def test_cabin_doorstep_is_a_valid_walk_end_but_never_a_through_tile():
    assert (52, 16) in W.footprint_tiles(CABIN_LIVE) and (52, 16) in W.door_tiles(CABIN_LIVE)
    g = SceneGame((52, 19), scene=CABIN_LIVE)
    g.hidden -= W.door_tiles(CABIN_LIVE) - {(52, 15)}       # live: the doorstep row tile (52,16) is walkable
    res = W.walk(g, (52, 16), "up", **LOG)
    assert res["end"] == (52, 16)
    g = SceneGame((48, 17), scene=CABIN_LIVE)
    path = W.walk(g, (56, 17), plan_only=True, **LOG)["path"]
    assert (52, 16) not in path and not set(path) & W.footprint_tiles(CABIN_LIVE)
    with pytest.raises(W.WalkError):
        W.walk(SceneGame((52, 19), scene=CABIN_LIVE), (51, 16), plan_only=True, **LOG)


# ---------------------------------------------------------------- 1-tile calibration + precise mode
class FastGame(SceneGame):
    """Moves one tile per 107 ms held (the live BusStop behaviour: a 215 ms 1-tile hold moved 2 tiles)."""

    def hold(self, key, ms):
        self.inputs.append(("hold", key, ms))
        self.facing = {"w": 0, "d": 1, "s": 2, "a": 3}[key]
        if ms < 100:
            return
        dx, dy = {"w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0)}[key]
        for _ in range(max(1, round(ms / 107))):
            nxt = (self.x + dx, self.y + dy)
            if self.blocked(nxt):
                break
            self.x, self.y = nxt


def test_hold_ms_calibration():
    assert W.hold_ms(1) == W.SINGLE_TILE_MS == 110
    assert W.hold_ms(3) == 3 * W.MS_PER_TILE
    assert W.hold_ms(1, single_ms=90) == 90 and W.hold_ms(2, tile_ms=120) == 240


def test_single_tile_leg_no_longer_overshoots():
    g = FastGame((24, 16), location="BusStop")
    res = W.walk(g, (24, 15), **LOG)
    assert res["end"] == (24, 15) and res["replans"] == 0 and g.inputs[0][2] == 110


def test_precise_mode_reaches_the_goal_tile_by_tile():
    g = FastGame((20, 20))
    res = W.walk(g, (24, 20), tiles_per_hold=1, **LOG)
    assert res["end"] == (24, 20) and res["replans"] == 0
    assert [i[2] for i in g.inputs] == [110] * 4
    g = FastGame((20, 20))
    with pytest.raises(W.WalkError):                    # the default 215 ms/tile overshoots at this speed and loops
        W.walk(g, (24, 20), max_replans=2, **LOG)


# ---------------------------------------------------------------- KNOWN_HAZARDS
def test_hazards_are_never_a_walk_goal_and_are_routed_around():
    with pytest.raises(W.WalkError) as e:
        W.walk(SceneGame((22, 11), location="BusStop"), (22, 8), plan_only=True, **LOG)
    assert "bus door" in str(e.value)
    with pytest.raises(W.WalkError) as e:
        W.walk(SceneGame((53, 11)), (53, 8), plan_only=True, **LOG)
    assert "pet bowl" in str(e.value)
    path = W.walk(SceneGame((51, 8)), (56, 8), plan_only=True, **LOG)["path"]
    assert not set(path) & set(W.hazard_tiles("Farm"))


def test_hazards_never_become_stands_or_chest_spots():
    hz = set(W.hazard_tiles("Farm"))
    facts = grass_around(53, 6)
    facts.update([tree(53, 6)])
    assert any(tuple(s) == (53, 7) for _, s, _ in P.candidates(facts, 53, 9))     # without the hazard set it would
    g = SceneGame((50, 4), facts)
    res = P.pick(g, 53, 9)
    assert res is None or res[0] not in hz
    for c in F.candidates(facts, (50, 4), hz):
        assert tuple(c["stand"]) not in hz and all(tuple(s) not in hz for s in c["chest_spots"])
    facts.update([obj(53, 9, "Stone")])
    st = S.sweep("Stone", facts, {}, (53, 11), location="Farm")
    assert st and all(tuple(s[:2]) not in hz for s in st[0]["stands"])


def test_soak_park_mirrors_away_from_a_hazard():
    pose = {"location": "Farm", "tile": {"x": 49, "y": 8}, "pixel": {"x": 49 * 64.0, "y": 8 * 64.0}}
    assert SK.safe_park(pose, 4, 0) == (-4, 0)                      # (53,8) is the pet bowl (live B3W1a)
    assert SK.safe_park(pose, 0, 3) == (0, 3)
