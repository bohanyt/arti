"""How many controls on her real menu pages read as unknown (FIX77A, ADR 135). Report-only, evaluator-independent.

Reads a run's archived provider requests (raw or packed, through the storage layer) and counts the non-slot rows of each menu
page she decided from: `before` is the page as it was; `after` is what `arti_runtime.fact_names.control_label` makes of the same
rows. A group's `source` is the field a row list lives in. A row with no `source` has a field path with no index, so it lives
in a named field by construction (`menu.geodeSpot`); those count as named after, and the page does not say which field.

    python tools/control_names.py <run> [<run> ...] [--json out.json]
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime.fact_names import UNNAMED_CONTROL, control_label  # noqa: E402
from scripts.free_play.decision_visibility import archived_requests, prompt_body  # noqa: E402


class _Evidence:
    def __init__(self):
        self.warnings = []

    def ref(self, path):
        return str(path)

    def track(self, path):
        pass


def _rows(ui):
    """(kind, source, label) of every control on one overview page; slots carry the bag's own names."""
    for group in ui.get("groups", ()) if isinstance(ui.get("groups"), list) else ():
        fields = group.get("fields") or ()
        if "label" not in fields:
            continue
        at = fields.index("label")
        for row in group.get("rows", ()):
            if isinstance(row, list) and len(row) > at and isinstance(row[at], str):
                yield group.get("kind"), group.get("source"), row[at]


def page_counts(ui):
    """{controls, unknown_before, unknown_after, unknown_fields} for one page. A detail page (she read `inspect:N`) carries component
    rows with their real `field_path` instead of groups."""
    out = Counter()
    fields = Counter()

    def count(label, path, source):
        out["controls"] += 1
        if label.startswith(UNNAMED_CONTROL):
            out["unknown_before"] += 1
            if control_label(label, path) == label:
                out["unknown_after"] += 1
                fields[source] += 1
    for _, source, label in _rows(ui):
        # No source: a named field, the page does not carry which. A list source names its rows, unless it is the menu's own list.
        count(label, source + "[0]" if isinstance(source, str) else "menu.namedField", source)
    for row in ui.get("details", ()) if isinstance(ui.get("details"), list) else ():
        if (isinstance(row, list) and len(row) == 2 and isinstance(row[1], dict) and row[1].get("kind") not in (None, "slot")
                and isinstance(row[1].get("label"), str)):
            count(row[1]["label"], row[1].get("field_path"), row[1].get("field_path"))
    return out, fields


def measure(run):
    run = Path(run)
    totals, menus, still = Counter(), {}, Counter()
    for journal in sorted(run.glob("runtime/live7/*/day-*/journal")):
        for record in archived_requests(_Evidence(), journal).values():
            body, _ = prompt_body(record)
            ui = (body or {}).get("unknown_ui")
            if not isinstance(ui, dict):
                continue
            counts, fields = page_counts(ui)
            counts["pages"] = 1
            totals.update(counts)
            still.update(fields)
            menus.setdefault(str(ui.get("menu_type")), Counter()).update(counts)
    def share(row, key):
        return round(row[key] / row["controls"], 4) if row["controls"] else None
    return dict(run=run.name, report_only=True, **dict(totals), share_before=share(totals, "unknown_before"),
                share_after=share(totals, "unknown_after"), still_unknown_fields={str(k): v for k, v in still.most_common()},
                menus={m: dict(dict(c), share_before=share(c, "unknown_before"), share_after=share(c, "unknown_after"))
                       for m, c in sorted(menus.items(), key=lambda kv: -kv[1]["unknown_before"])})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--json")
    a = parser.parse_args(argv)
    report = [measure(run) for run in a.runs]
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps([{k: v for k, v in r.items() if k != "menus"} for r in report], indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
