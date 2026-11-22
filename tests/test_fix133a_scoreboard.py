"""ADR 231 part F: the scoreboard writes for evaluations whose session metrics hold a count by class.

Synthetic copies of the shapes in the archived evaluations of attempts 85 (S1, S2), 86 and 87 (evaluator 1.7.16,
session metrics 1.7.12): every metric a float or None, ``version`` a string, and ``mines.fight_outcomes_by_class`` a
dict of counts ({"VERIFIED": 2}, {}, {"VERIFIED": 1}, {"NOT_READY": 3, "VERIFIED": 5, "CLEAN_FAIL": 1}).
"""
import pytest

from scripts.free_play import scoreboard as B
from scripts.free_play.session_metrics import METRICS

SHAPES = {"synthetic-a85-s1": {"VERIFIED": 2}, "synthetic-a85-s2": {}, "synthetic-a86-s1": {"VERIFIED": 1},
          "synthetic-a87-s1": {"NOT_READY": 3, "VERIFIED": 5, "CLEAN_FAIL": 1}}


def evaluation(run, fights):
    metrics = {"version": "1.7.12"}
    for group, keys in METRICS.items():
        metrics[group] = {k: dict(value=0.25 if i % 2 else None, definition="synthetic") for i, k in enumerate(keys)}
    metrics["mines"]["fight_outcomes_by_class"] = dict(value=fights, counts=fights, definition="synthetic")
    return dict(schema=B.SCHEMA, evaluator_version="1.7.16", run=run, session_metrics=metrics,
                goal_gate=dict(status="fail", measured_criteria=dict(enabled=False)))


def test_the_metric_cell_formats_every_shape():
    assert B.metric_cell(0.123456) == "0.1235" and B.metric_cell(3) == "3.0000"
    assert B.metric_cell({"NOT_READY": 3, "VERIFIED": 5, "CLEAN_FAIL": 1}) == "CLEAN_FAIL=1; NOT_READY=3; VERIFIED=5"
    assert B.metric_cell({}) == "none" and B.metric_cell("text") == "text" and B.metric_cell(True) == "True"


def test_the_session_metrics_table_writes_the_three_archived_shapes():
    evaluations = [evaluation(run, fights) for run, fights in SHAPES.items()]
    lines = B.session_metrics_table(evaluations)
    rows = {line.split(" | ")[0].lstrip("| "): line for line in lines if line.startswith("| synthetic-")}
    assert set(rows) == set(SHAPES)
    assert "CLEAN_FAIL=1; NOT_READY=3; VERIFIED=5" in rows["synthetic-a87-s1"]
    assert "| none |" in rows["synthetic-a85-s2"] or " none " in rows["synthetic-a85-s2"]
    assert "UNKNOWN" in rows["synthetic-a86-s1"] and "0.2500" in rows["synthetic-a86-s1"]


def test_the_scoreboard_file_is_written(tmp_path):
    evaluations = [evaluation(run, fights) for run, fights in SHAPES.items()]
    out = tmp_path / "SCOREBOARD_AUTO.md"
    try:
        B.write_scoreboard(evaluations, out)
    except (KeyError, AttributeError) as exc:  # sections a minimal synthetic evaluation does not carry
        pytest.fail("scoreboard needs a section the evaluator always writes: %r" % exc)
    text = out.read_text(encoding="utf-8")
    assert "## Session metrics beside the goal gate" in text and "VERIFIED=5" in text
