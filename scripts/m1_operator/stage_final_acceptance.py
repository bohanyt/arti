"""Print the final acceptance save-staging recipe; --execute runs existing guarded tools.

No game launch, approval, trial, policy toggle or post-approval staging is done here.
Farmhand chat scripts are separate and must run before approval, after joining.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m1_paths as paths


BASELINES = {"a": ("host1_100000003", "w1-parta-day0b"),
             "b": ("host1_100000003", "w1-thu-farm-corridor"),
             # PLAN 2a.4: the same Part B campaign on the Riverland proof save (Mon Spring 8 Y1;
             # the preface earns Tue Spring 9: Pierre open, Mines open, no festival). No edit.
             "b-proof": ("proof1_100000002", "proof1-riverland-base"),
             "c": ("proof1_100000002", "proof1-riverland-base")}


def recipe(part):
    save, snapshot = BASELINES[part]
    if paths.SAVE_NAME != save:
        raise ValueError(f"Part {part.upper()} requires ARTI_M1_SAVE_NAME={save}")
    stage = str(Path(__file__).with_name("stage_save.py"))
    commands = [[sys.executable, "-B", stage, "backup"],
                [sys.executable, "-B", stage, "restore", snapshot]]
    if part == "c":
        # Read only the declared baseline, never a running game's state.
        source = paths.PREP / "savestates" / snapshot / "save" / save
        root = ET.parse(source).getroot()
        host = root.find("player")
        farmhands = [f for f in root.findall("farmhands/Farmer") if f.findtext("name") == "artihand"]
        if host is None or len(farmhands) != 1:
            raise ValueError("baseline requires one HOST and one artihand farmhand")
        ids = [f.findtext("UniqueMultiplayerID") for f in (host, farmhands[0])]
        if any(not value or not value.lstrip("-").isdigit() for value in ids) or ids[0] == ids[1]:
            raise ValueError("baseline farmer identities unavailable or ambiguous")
        # Wednesday -> ordinary preface sleep -> Thursday: both shops open,
        # Spring Foraging available, no festival. These are setup facts only.
        commands.append([sys.executable, "-B", stage, "edit", "--set", "date=spring:10", "--set", "year=1"])
        for farmer, flag in zip(ids, ("ccDoorUnlock", "canReadJunimoText")):
            commands.append([sys.executable, "-B", stage, "edit", "--set", f"mail_received={farmer}:{flag}"])
    return commands


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", required=True, choices=tuple(BASELINES))
    parser.add_argument("--execute", action="store_true", help="games closed; back up and restore the declared baseline")
    args = parser.parse_args(argv)
    try:
        commands = recipe(args.part)
    except (ValueError, OSError, ET.ParseError) as exc:
        parser.error(str(exc))
    print(json.dumps(dict(staging_only=True, execute=args.execute, commands=commands), indent=2), flush=True)
    if args.execute:
        for command in commands:
            subprocess.run(command, check=True)  # existing stage_save guards/refusals; never retry or bypass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
