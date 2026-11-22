#!/usr/bin/env python3
"""Print one simulated Live7 Spring 1 day (offline; no game, SMAPI, network or model).

    venv/Scripts/python.exe scripts/live7/sim_day.py                      # baseline stub model
    venv/Scripts/python.exe scripts/live7/sim_day.py --replay r11 --until 8   # R11 replies, then baseline

Output: per-start timeline, then per-model-boundary planner offers vs what the render left visible.
UNIT_TESTED tooling only; see docs/stardew/live7/SIM.md.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "live7_sim" / "rehearsals.json"


def load():
    sys.path.insert(0, str(ROOT))
    package = Path(__file__).with_name("sim")
    spec = importlib.util.spec_from_file_location("live7_sim", package / "__init__.py",
                                                  submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["live7_sim"] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay", help="rehearsal key in the fixture (r1, r3, r4, r11)")
    parser.add_argument("--until", type=int, help="replay only provider requests below this index")
    args = parser.parse_args(argv)
    sim = load()
    policy = sim.gardener()
    if args.replay:
        steps = json.loads(FIXTURE.read_text(encoding="utf-8"))["rehearsals"][args.replay]["steps"]
        policy = sim.Replay([s for s in steps if args.until is None or s["request"] < args.until], policy)
    result = sim.run_day(sim.World.spring1(), policy)
    print("\n".join(result.timeline() + ["-- offers --"] + result.offers_table()))
    print("status=%s verdict=%s reason=%s bed=%s stalled=%s" % (
        result.status, result.verdict, result.reason, result.bed_time, result.stalled))
    return 0 if result.status != "active" else 1


if __name__ == "__main__":
    sys.exit(main())
