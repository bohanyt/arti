"""Read-only journal census plus an explicitly optimistic plan grouping estimate.

This does not execute plans or change evidence. Different menus opened without
an intervening close/non-menu boundary may be one inferred visit. Superseded and
invalid calls remain overhead; native preconditions/plan uptake are not replayed.
"""
import argparse
import json
from pathlib import Path


def census(run):
    days = sorted(Path(run).glob("runtime/live7/*/day-*/journal/g1_journal.jsonl"))
    valid = reservations = menu_valid = menu_reservations = 0
    visits = []
    for path in days:
        current, trigger = None, None
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not any('"ev":"' + ev + '"' in line or '"ev": "' + ev + '"' in line for ev in (
                        "skill_offer", "trial_reservation", "skill_model_decision", "live7_ui_choice")):
                    continue
                row = json.loads(line)
                if row["ev"] == "skill_offer":
                    trigger = row.get("trigger")
                    if trigger != "live7_unknown_ui":
                        current = None
                if row["ev"] == "trial_reservation" and row.get("kind") == "provider":
                    reservations += 1
                    if trigger == "live7_unknown_ui":
                        if current is None:
                            current = dict(calls=0, decisions=0)
                            visits.append(current)
                        current["calls"] += 1
                        menu_reservations += 1
                if row["ev"] == "skill_model_decision" and row.get("choice") != "invalid":
                    valid += 1
                    choice = row.get("choice")
                    if isinstance(choice, list) and choice and choice[0] == "ui_action":
                        menu_valid += 1
                        if current is None:
                            current = dict(calls=0, decisions=0)
                            visits.append(current)
                        current["decisions"] += 1
                if row["ev"] == "live7_ui_choice" and (row.get("result_v1") or {}).get("changed") == "closed":
                    current = None
    groups = sum((v["decisions"] + 2) // 3 for v in visits)
    saved = menu_valid - groups
    return dict(kind="journal_census_and_optimistic_estimate", days=len(days), valid_decisions=valid,
        journaled_provider_reservations=reservations, menu_valid_decisions=menu_valid,
        menu_provider_reservations=menu_reservations, inferred_menu_visits=len(visits),
        three_choice_groups=groups, estimated_calls_saved_max=saved,
        valid_decisions_after_estimate=valid-saved,
        decisions_per_day_before=valid/len(days) if days else None,
        decisions_per_day_after_estimate=(valid-saved)/len(days) if days else None,
        menu_calls_per_visit_before=menu_reservations/len(visits) if visits else None,
        menu_calls_per_visit_after_estimate=(menu_reservations-saved)/len(visits) if visits else None,
        assumptions=["same historical action trace", "up to three choices grouped per visit",
                     "provider retry/supersede overhead unchanged", "all grouped choices jointly offered and survive checks",
                     "no gain credited for world plans or four-press menu steps"],
        live_tempo_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    print(json.dumps(census(args.run), indent=2))


if __name__ == "__main__":
    main()
