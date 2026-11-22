"""Read-only campaign ledger check; never connects to the game or issues input."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_days(path, *, part="a"):
    path = Path(path)
    if not path.is_file():
        raise ValueError("journal_file_required")
    days = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a writer may still be appending the last record
        if row.get("ev") == "part_" + part + "_row":
            day = days.setdefault(row["day"], dict(session_id=row["session_id"], owner_goal_id=row["owner_goal_id"], rows={}))
            day["rows"][row["task"]] = dict(verified=row["verified"], evidence=row["evidence"],
                                          **({"reason":row["reason"]} if "reason" in row else {}))
    return days


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--day", type=int, choices=(1, 2, 3), required=True)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--part-c", action="store_true", help="read the progress-day Part C ledger and exact native gaps")
    group.add_argument("--part-b", action="store_true", help="read the one-day Part B native row ledger")
    a = p.parse_args(argv)
    if (a.part_b or a.part_c) and a.day != 1:
        p.error("Parts B/C have one day")
    day = read_days(a.journal, part="c" if a.part_c else "b" if a.part_b else "a").get(a.day)
    print(json.dumps(dict(day=a.day, ledger=day, verdict="READ_ONLY_NATIVE_ROWS; operator acceptance checks still required"),
                     ensure_ascii=False))
    if a.part_b or a.part_c:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        if a.part_c:
            # Owner 2026-10-20: the approval-day S29 row (journaled day 1, preface) is a mandatory Part C row.
            from arti_stardew_part_c import ROWS as TASKS
        else:
            from arti_stardew_part_b import TASKS
        rows = (day or {}).get("rows", {})
        unmet = {k: rows.get(k, {}).get("reason", "unattempted") for k in TASKS
                 if rows.get(k, {}).get("verified") is not True}
        print(json.dumps(dict(verdict="PARTIAL" if unmet else "RECORDED_ROWS_VERIFIED", unmet=unmet), ensure_ascii=False))
        return 2 if unmet else 0
    return 0 if day and all(day["rows"].get(k, {}).get("verified") is True
                           for k in ("crops", "animals", "storage", "sleep")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
