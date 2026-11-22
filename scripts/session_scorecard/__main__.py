"""SCORE1 (ADR 248) command: python -m scripts.session_scorecard <run> [--compare <run> ...] [--json <file or folder>]

Read-only on every run folder: the JSON goes to --json (a file, or a folder for a dated name) or, by default, to
`<temp>/arti-scorecard/`; a JSON path inside a scored run folder is refused. Exit 0 with a table, 2 on bad input.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from scripts.session_scorecard.scorecard import dump, score, table, verdicts


def _inside(path, folder):
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m scripts.session_scorecard",
                                 description="Session scorecard (ADR 248): read-only, seconds, safe on a run being written.")
    ap.add_argument("run", help="session run folder (e.g. lab-rig\\mabar-runs\\mabar-20261119-1900)")
    ap.add_argument("--compare", action="append", default=[], metavar="RUN", help="a run to judge against (repeatable)")
    ap.add_argument("--json", dest="json_path", help="JSON file, or a folder for <run>-<time>.json")
    args = ap.parse_args(argv)
    runs = [Path(args.run)] + [Path(c) for c in args.compare]
    for run in runs:
        if not run.is_dir():
            print("scorecard: not a folder: %s" % run, file=sys.stderr)
            return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.json_path) if args.json_path else Path(tempfile.gettempdir()) / "arti-scorecard"
    if out.is_dir() or not out.suffix:
        out = out / ("%s-%s.json" % (runs[0].name, stamp))
    if any(_inside(out, run) for run in runs):
        print("scorecard: refused: the JSON would be written inside a run folder (%s); it is read-only" % out, file=sys.stderr)
        return 2
    card = score(runs[0])
    others = [score(run) for run in runs[1:]]
    print(table(card))
    lines = []
    for other in others:
        lines.append(dict(against=other["name"], lines=verdicts(card, other)))
        print("--- against %s" % other["name"])
        print("\n".join(lines[-1]["lines"]))
    path = dump(out, dict(card=card, compare=others, verdicts=lines))
    print("json      %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
