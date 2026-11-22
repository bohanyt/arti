"""FIX34-Q1: fail visibly when the obsolete composite pad replay hits a seat crossing.

327145f1e put warp-door presses on the proven seat path. The old standalone
house coordinator still arrives holding pad mode. Preserve the crossing gate
and all tests; only this exact native refusal is an expected failure. Other
exceptions remain failures, and a repaired replay becomes a strict XPASS.
"""
import json
from pathlib import Path

import pytest


class LegacyPadCrossingReplayBlocked(Exception):
    pass


NODES = frozenset(json.loads(Path(__file__).with_name("quarantine_pm1_replays.json").read_text())["FIX34-Q1"])


@pytest.fixture(autouse=True)
def quarantine_legacy_pad_crossing(request, monkeypatch):
    if request.node.nodeid not in NODES:
        return
    request.node.add_marker(pytest.mark.xfail(
        strict=True, raises=LegacyPadCrossingReplayBlocked,
        reason="FIX34-Q1: legacy composite pad route reaches a seat crossing in GAMEPAD_MODE (327145f1e)"))
    original = request.module.trial
    def trial(*args, **kwargs):
        result = original(*args, **kwargs)
        bridge = args[1]
        for row in bridge.native_runs:
            trace = row.get("trace", [])
            if trace and trace[-1].get("label") == "PRECONDITION_UNAVAILABLE(GAMEPAD_MODE)":
                assert row["result"]["success"] is not True
                raise LegacyPadCrossingReplayBlocked("Native crossing refused pad mode; no success invented")
        return result
    monkeypatch.setattr(request.module, "trial", trial)
