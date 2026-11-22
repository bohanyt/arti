"""Offline tests for scripts/m1_operator/find_layout.py with a fake game (pose + inspect facts; no socket, no inbox,
no input). Run: ./venv/Scripts/python.exe -m pytest tests/test_m1_operator_find_layout.py -q
"""
import io, json, os, sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "m1_operator"))
import find_layout as F  # noqa: E402


def tree(x, y, stage=5, stump=False, tapped=False):
    return f"tf:Farm:{x},{y}:Tree", {"kind": "terrain", "tile": {"x": x, "y": y}, "passable": False,
                                     "wild_tree": {"growth_stage": stage, "stump": stump, "tapped": tapped}}


def obj(x, y, oid="(O)450"):
    return f"obj:Farm:{x},{y}:{oid}", {"kind": "object", "tile": {"x": x, "y": y}, "passable": False}


class FakeGame:
    def __init__(self, me=(40, 15), facts=(), location="Farm"):
        self.me, self.location = me, location
        self.facts = dict(facts)
        self.inputs = []

    def pose(self):
        return {"location": self.location, "tile": {"x": self.me[0], "y": self.me[1]}, "pixel": {"x": self.me[0] * 64.0, "y": self.me[1] * 64.0},
                "facing": 2, "is_player_free": True, "active_menu": None, "time_of_day": 900, "energy": 250}

    def inspect(self, cx, cy, r):
        return {k: f for k, f in self.facts.items()
                if abs(f["tile"]["x"] - cx) <= r and abs(f["tile"]["y"] - cy) <= r}

    def hold(self, *a):                       # find_layout must never move ARTI
        self.inputs.append(a)
        raise AssertionError("input sent")


def test_filters_immature_stumps_and_tapped_trees():
    facts = dict([tree(45, 15, stage=4), tree(46, 15, stump=True), tree(47, 15, tapped=True), tree(48, 15)])
    c = F.candidates(facts, (40, 15))
    assert {tuple(x["tree"]) for x in c} == {(48, 15)}


def test_open_tree_with_close_chest_ranks_first():
    walls = [obj(30 + dx, 10 + dy) for dx, dy in ((0, 1), (1, 0), (-1, 0))]       # tree (30,10) boxed on 3 sides
    facts = dict([tree(30, 10), *walls, tree(44, 15), obj(44, 18, "(BC)130")])
    c = F.candidates(facts, (40, 15))
    best = c[0]
    assert best["tree"] == [44, 15] and best["open_sides"] >= 3
    assert best["chest"] == [44, 18] and 2 <= best["chest_dist"] <= 5
    assert {"left": (1, 0), "right": (-1, 0), "up": (0, 1), "down": (0, -1)}[best["face"]] == \
        (best["stand"][0] - best["tree"][0], best["stand"][1] - best["tree"][1])


def test_chest_spots_avoid_any_fact():
    facts = dict([tree(44, 15), obj(46, 15), obj(44, 17)])
    for c in F.candidates(facts, (40, 15)):
        for s in c["chest_spots"]:
            assert tuple(s) not in {(46, 15), (44, 17), (44, 15)}
            assert 2 <= F.manhattan(s, c["stand"]) <= 4


def test_find_plans_without_input_and_main_prints_layout():
    g = FakeGame(facts=dict([tree(44, 15), obj(44, 18, "(BC)130")]))
    res = F.find(g, log=lambda m: None)
    assert res["ok"] and res["best"]["plan"]["tiles"] >= 1 and not g.inputs
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = F.main(["--plan-only"], game=g)
    line = [l for l in out.getvalue().splitlines() if l.startswith("LAYOUT ")][0]
    assert rc == 0 and json.loads(line[7:])["best"]["tree"] == [44, 15]


def test_no_tree_is_exit_3():
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        assert F.main([], game=FakeGame()) == 3


def test_refuses_while_a_trial_runs_without_touching_the_game():
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        assert F.main([], trial_check=lambda: True) == 2
    assert "trial runner" in err.getvalue()


def test_unknown_process_list_refuses_too():
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        assert F.main([], trial_check=lambda: None) == 2
    assert "cannot list python processes" in err.getvalue()


def test_indoors_is_its_own_exit_code_and_does_not_sweep():
    g = FakeGame(facts=dict([tree(44, 15)]), location="Cabin")
    g.inspect = lambda *a: (_ for _ in ()).throw(AssertionError("inspect indoors"))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert F.main([], game=g) == 4
    assert "walk out first" in err.getvalue() and '"reason": "indoors"' in out.getvalue()
