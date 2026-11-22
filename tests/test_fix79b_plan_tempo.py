import json

from scripts.free_play.plan_tempo import census


def test_estimate_keeps_failed_provider_overhead_and_breaks_at_close(tmp_path):
    path = tmp_path / "runtime/live7/c1/day-0001-attempt-000/journal/g1_journal.jsonl"
    path.parent.mkdir(parents=True)
    rows = [dict(ev="skill_offer", trigger="live7_unknown_ui")]
    for i in range(4):
        rows.extend([dict(ev="trial_reservation", kind="provider"),
                     dict(ev="skill_model_decision", choice=["ui_action", "transfer:%d:quick" % i])])
    rows.extend([dict(ev="trial_reservation", kind="provider"),
                 dict(ev="skill_model_decision", choice="invalid"),
                 dict(ev="live7_ui_choice", result_v1=dict(changed="closed")),
                 dict(ev="trial_reservation", kind="provider"),
                 dict(ev="skill_model_decision", choice=["ui_action", "close"])])
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows), encoding="utf-8")
    result = census(tmp_path)
    assert result["days"] == 1 and result["inferred_menu_visits"] == 2
    assert result["menu_provider_reservations"] == 6 and result["menu_valid_decisions"] == 5
    assert result["three_choice_groups"] == 3 and result["estimated_calls_saved_max"] == 2
    assert result["menu_calls_per_visit_before"] == 3 and result["menu_calls_per_visit_after_estimate"] == 2
    assert result["live_tempo_verified"] is False
