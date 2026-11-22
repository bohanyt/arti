"""SCORE1 (ADR 248): the session scorecard on synthetic journals in the real row shapes (2026-11-18 mabar runs and
the 2026-11-19 bench journal that carries ADR 241's clock and ladder rows)."""
from __future__ import annotations

import json
import os

from scripts.session_scorecard import scorecard as SC
from scripts.session_scorecard.__main__ import main

T0 = 1795012875.0  # 2026-11-18 21:41:15 WIB, the first row of the healthy 21:38 run


def _journal(run, rows, *, cycle="c1", day="day-0001-attempt-000", tail=""):
    path = run / "runtime" / "live7" / cycle / day / "journal" / "g1_journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(dict(row, domain="stardew", cycle=cycle, day_trial=day)) + "\n")
        f.write(tail)
    return path


def vitals(at, location="Farm", menu=None, date=(1, "spring", 14)):
    return {"ev": "native_vitals", "at": at, "hp": 100, "game": {"date": list(date), "time": 900, "location": location},
            "active_menu": menu}


def done(skill="siram", arg="", location="Farm"):
    return {"ev": "skill_finished", "skill": skill, "arg": arg, "verdict": "observed", "decisions": 1, "inputs": 1,
            "game": {"date": [1, "spring", 14], "time": 900, "location": location}}


def fail(skill="aksi", reason="interact_target_not_reachable_or_not_offered"):
    return {"ev": "skill_failed", "skill": skill, "arg": "", "reason": reason, "decisions": 0, "inputs": 0}


def session_end(reason=SC.SLEPT):
    return {"ev": "skill_session_finished", "status": "finished", "verdict": "achieved", "reason": reason}


def clock(at, age, since, kind="tick", reset_by="start", **extra):
    return dict(ev="live7_effect_clock", at=at, kind=kind, age_s=age, ladder_age_s=age, reset_by=reset_by,
                since_at=since, resets=0, **extra)


def ladder(at, rung, name, result, kind="rung", **extra):
    return dict(ev="live7_effect_ladder", at=at, age_s=at - T0, ladder_age_s=at - T0, rung=rung, name=name,
                result=result, kind=kind, **extra)


def test_a_torn_last_line_is_skipped_and_counted_and_the_row_counts_once_it_is_complete(tmp_path):
    run = tmp_path / "mabar-test"
    rows = [vitals(T0), done(), vitals(T0 + 10), done("pergi_ke", "town")]
    path = _journal(run, rows, tail='{"ev":"skill_failed","skill":"aksi","rea')
    card = SC.score(run, now=T0 + 20)
    assert card["read"]["torn_last_line"] == 1 and card["read"]["bad_lines"] == 0
    assert card["skills"]["finished"] == 2 and card["skills"]["failed"] == 0
    assert card["trips"] == {"finished": 1, "failed": 0}
    with open(path, "a", encoding="utf-8", newline="\n") as f:  # the writer finishes its line and goes on
        f.write('son":"tool_target_not_offered"}\n' + json.dumps(vitals(T0 + 30)) + "\n")
    card = SC.score(run, now=T0 + 40)
    assert card["read"]["torn_last_line"] == 0
    assert card["skills"]["failed"] == 1
    assert [tuple(x) for x in card["skills"]["top_failures"]] == [("tool_target_not_offered", 1)]
    assert card["last_row_age_s"] == 10.0


def test_stuck_events_come_from_the_effect_clock_rows_with_their_ladder_rungs(tmp_path):
    run = tmp_path / "mabar-clock"
    rows = [{"ev": "trial_started", "at": T0}, vitals(T0),
            clock(T0, 0.0, T0), clock(T0 + 30, 30.0, T0),
            ladder(T0 + 60, 1, "menu_close", "not_applicable", why="no_menu"), clock(T0 + 61, 61.0, T0, rung=1),
            ladder(T0 + 120, 2, "step_away", "not_applicable", why="nothing_offered"),
            ladder(T0 + 150, 3, "leave_place", "acting", call=["pergi_ke", "backwoods"]),
            clock(T0 + 183, 183.0, T0, rung=3),
            ladder(T0 + 211, 3, "leave_place", "no_change", kind="read_back"),
            clock(T0 + 214, 0.0, T0 + 214, kind="reset", reset_by="map"), vitals(T0 + 214, "Backwoods"),
            clock(T0 + 244, 30.0, T0 + 214), session_end()]
    _journal(run, rows)
    card = SC.score(run, now=T0 + 250)
    stuck = card["stuck"]
    assert stuck["source"] == "effect_clock" and stuck["count"] == 1
    event = stuck["events"][0]
    assert event["start"] == T0 and event["seconds"] == 214.0 and event["ended_by"] == "map"
    acted = [(r["rung"], r["result"]) for r in event["rungs"] if r["result"] != "not_applicable"]
    assert acted == [(3, "acting"), (3, "no_change")]
    assert stuck["rungs_taken"] == {"3": 1}
    assert card["nights"]["slept"] == 1
    assert "r3 no_change" in SC.table(card)


def test_an_open_clock_stretch_at_the_end_of_a_journal_still_being_written_is_reported(tmp_path):
    run = tmp_path / "mabar-open"
    _journal(run, [vitals(T0), clock(T0, 0.0, T0), clock(T0 + 30, 30.0, T0), clock(T0 + 90, 90.0, T0)])
    event = SC.score(run, now=T0 + 95)["stuck"]["events"][0]
    assert event["seconds"] == 90.0 and event["ended_by"] == "<journal end>"
    _journal(run, [session_end(SC.PASSED_OUT), clock(T0 + 120, 120.0, T0)])  # a late tick after her session ended
    stuck = SC.score(run, now=T0 + 125)["stuck"]
    assert stuck["count"] == 1 and stuck["events"][0]["ended_by"] == "session_end:live7:passed_out"


def test_without_clock_rows_the_adr241_replay_finds_the_stretch(tmp_path):
    run = tmp_path / "mabar-old"
    _journal(run, [vitals(T0), vitals(T0 + 5), vitals(T0 + 130, "Town"), done("pergi_ke", "town", "Town"),
                   vitals(T0 + 135, "Town")])
    stuck = SC.score(run, now=T0 + 140)["stuck"]
    assert stuck["source"] == "replay" and stuck["count"] == 1
    assert stuck["events"][0]["seconds"] == 130.0 and stuck["events"][0]["ended_by"] == "map"
    assert stuck["replay"]["count"] == 1


def _dead_hands_run(run):
    rows = []
    for i in range(20):
        rows += [vitals(T0 + i * 10), done()]
    for j in range(25):
        rows += [vitals(T0 + 200 + j * 10), fail(reason="backend_undeliverable:ArmRejected")]
    rows.append(vitals(T0 + 500))
    _journal(run, rows)


def test_hands_share_of_the_last_20_runs_and_dead_minutes(tmp_path):
    run = tmp_path / "mabar-dead"
    _dead_hands_run(run)
    hands = SC.score(run, now=T0 + 500)["hands"]
    assert hands["share_now"] == 0.0
    # the share drops under 30 % at the 15th failure (T0 + 340); from there to the last row (T0 + 500) is dead
    assert hands["dead_minutes"] == round(160 / 60, 1)
    # observed from the 10th run (T0 + 90) to the last row: 410 s, of which 160 dead (review R3b: unobserved is not live)
    assert hands["alive_minutes"] == round(250 / 60, 1) and hands["coverage"] == 0.82
    assert hands["unobserved_minutes"] == 1.5
    assert hands["series"][19]["share"] == 1.0 and len(hands["series"]) == 45
    assert hands["every_15_min"] == [0.0]


def test_nights_trips_and_attempts_without_an_end(tmp_path):
    run = tmp_path / "mabar-nights"
    _journal(run, [vitals(T0), done("pergi_ke", "town"), fail("pergi_ke", "exit_not_offered"), session_end()],
             day="day-0001-attempt-000")
    _journal(run, [vitals(T0 + 900, date=(1, "spring", 15)), session_end(SC.PASSED_OUT)], day="day-0002-attempt-000")
    _journal(run, [vitals(T0 + 1800, date=(1, "spring", 16)), done()], day="day-0003-attempt-000")
    card = SC.score(run, now=T0 + 1801)
    assert card["nights"] == {"slept": 1, "passed_out": 1, "other": {}, "attempts_without_end": 1}
    assert card["trips"] == {"finished": 1, "failed": 1}
    assert card["game_days"]["count"] == 3 and card["game_days"]["last"] == [1, "spring", 16]
    assert [a["day_trial"] for a in card["attempts"]] == ["day-0001-attempt-000", "day-0002-attempt-000",
                                                          "day-0003-attempt-000"]


def test_what_she_achieved(tmp_path):
    run = tmp_path / "mabar-achieved"
    rows = [vitals(T0), {"ev": "live7_wallet", "money": 440, "date": [1, "spring", 14], "time_of_day": 650},
            {"ev": "bag_sitting_result", "at": T0 + 10, "verb": "kirim", "request_ids": ["r1"],
             "items": [{"item": "(O)24", "name": "Parsnip", "requested": 2, "moved": 2, "state": "done"}]},
            {"ev": "item_move_outcome", "at": T0 + 9, "request_id": "r1", "name": "Parsnip", "moved": 2,
             "effect": "shipped", "destination": "shipping_bin"},
            {"ev": "item_move_outcome", "at": T0 + 20, "request_id": "r2", "name": "Daffodil", "moved": 1,
             "effect": "shipped", "destination": "shipping_bin"},
            {"ev": "bag_sitting_result", "at": T0 + 30, "verb": "jual", "request_ids": ["r3"],
             "items": [{"name": "Stone", "moved": 3}]},
            {"ev": "bag_sitting_result", "at": T0 + 31, "verb": "simpan", "items": [{"name": "Wood", "moved": 9}]},
            vitals(T0 + 40, "Town"),
            {"ev": "objective_progress", "kind": "talk_npc", "opened": True, "speaker": "Haley"},
            done("ngobrol_hadiah", "harvey ke town", "Town"), done("ngobrol_hadiah", "warga", "Town"),
            {"ev": "screen_text", "at": T0 + 50, "family": "dialogue", "speaker": "Jas", "text": "..."},
            {"ev": "screen_text", "at": T0 + 51, "family": "dialogue", "speaker": "", "text": "..."},
            vitals(T0 + 60, "Temp", date=(1, "spring", 13)),
            {"ev": "live7_native_event", "facts": {"date": [1, "spring", 13], "event_up": True,
                                                   "event_id": "festival_spring13"}},
            {"ev": "live7_wallet", "money": 540, "date": [1, "spring", 13], "time_of_day": 900}]
    _journal(run, rows)
    ach = SC.score(run, now=T0 + 61)["achieved"]
    assert (ach["gold_first"], ach["gold_last"], ach["gold_delta"]) == (440, 540, 100)
    assert ach["shipped"]["total"] == 3 and dict(map(tuple, ach["shipped"]["items"])) == {"Parsnip": 2, "Daffodil": 1}
    assert ach["sold"]["total"] == 3
    assert ach["talked_to"] == ["Haley", "Harvey"]
    assert [tuple(x) for x in ach["dialogue_from"]] == [("Jas", 1)]
    assert [f["event"] for f in ach["festivals"]] == ["festival_spring13"]
    assert ach["places"] == ["Farm", "Temp", "Town"]


def test_menus_held_over_60_seconds(tmp_path):
    run = tmp_path / "mabar-menus"
    _journal(run, [vitals(T0), vitals(T0 + 10, menu="QuestLog"), vitals(T0 + 50, menu="QuestLog"),
                   vitals(T0 + 100), vitals(T0 + 110, menu="DialogueBox"), vitals(T0 + 130),
                   vitals(T0 + 140, menu="ShippingMenu"), vitals(T0 + 220, menu="ShippingMenu")])
    held = SC.score(run, now=T0 + 221)["menus_held"]
    assert [(m["menu"], m["seconds"], m["ended_by"]) for m in held["events"]] == [
        ("QuestLog", 90.0, "closed"), ("ShippingMenu", 80.0, "<journal end>")]


def test_hand_rescues_from_rescues_tsv_or_none_recorded(tmp_path):
    run = tmp_path / "mabar-rescue"
    _journal(run, [vitals(T0), done(), vitals(T0 + 1800)])
    assert SC.score(run, now=T0)["rescues"] is None
    assert "none recorded" in SC.table(SC.score(run, now=T0))
    (run / "rescues.tsv").write_text("R1\t%s\tQuestLog (Journal)\tesc on farmhand\n" % SC.wib(T0 + 600), encoding="utf-8")
    card = SC.score(run, now=T0)
    assert [(r["id"], r["at"]) for r in card["rescues"]] == [("R1", T0 + 600)]
    assert card["longest_unaided_minutes"] == 20.0


def test_verdicts_against_a_comparison_run(tmp_path):
    good, bad = tmp_path / "mabar-good", tmp_path / "mabar-bad"
    rows = []
    for i in range(100):
        rows += [vitals(T0 + i * 10, "Farm" if i % 2 else "Town"), done()]
    _journal(good, rows)
    _dead_hands_run(bad)
    q1, q2, q3 = SC.verdicts(SC.score(good, now=T0), SC.score(bad, now=T0))
    assert q1.startswith("Q1 hands without a hand: BETTER")
    assert q2.startswith("Q2 stuck: BETTER")
    assert q3.startswith("Q3 achieved: MORE")
    q1, q2, _ = SC.verdicts(SC.score(bad, now=T0), SC.score(good, now=T0))
    assert q1.startswith("Q1 hands without a hand: WORSE") and q2.startswith("Q2 stuck: WORSE")


DAY_S = 360  # one game day = one attempt of 6 active minutes; the night gap between attempts is not active time


def _steady(run, minutes, *, gold=50, ship=1, finish_every=1, fail_days=(), stuck_day=None, unended=(),
            extra_person_day=None, rescues=None, finish_per_row=1, runs="done"):
    """A run of `minutes` active minutes at fixed rates per game day: 37 rows a day (one every 10 s), a finished
    skill per row, the place alternating Farm/Town, gold +`gold` and `ship` units shipped a day, Haley on day 1, a
    festival on day 2, and on day 11 a new person and a new place (outside a 30-minute window)."""
    for d in range(minutes * 60 // DAY_S):
        base, rows = T0 + d * 1000, []
        for k in range(DAY_S // 10 + 1):
            place = "Farm" if (k % 2 or (d == stuck_day and 10 <= k <= 22)) else "Town"
            rows.append(vitals(base + k * 10, place, date=(1, "spring", 1 + d)))
            if k == 0:
                rows.append({"ev": "live7_wallet", "money": 100 + gold * d, "date": [1, "spring", 1 + d]})
            if runs == "done" and k % finish_every == 0:
                rows += [done(location=place)] * finish_per_row
            elif runs == "refused":  # only refusals before a start (`_skill_fail_unstarted`): no run of her hands
                rows.append(dict(fail("pergi_ke", "precondition:fresh_inventory_unavailable"), started=False))
            if d in fail_days:
                rows += [fail(reason="backend_undeliverable:ArmRejected")] * 4
            if k == 5 and d == 0:
                rows.append({"ev": "objective_progress", "kind": "talk_npc", "opened": True, "speaker": "Haley"})
            if k == 5 and d == 1:
                rows.append({"ev": "live7_native_event", "facts": {"date": [1, "spring", 2], "event_id": "festival_x"}})
            if k == 7 and d in (10, extra_person_day):
                rows.append({"ev": "objective_progress", "kind": "talk_npc", "opened": True,
                             "speaker": "Lewis" if d == 10 else "Robin"})
                if d == 10:
                    rows.append(vitals(base + k * 10 + 1, "Mountain", date=(1, "spring", 1 + d)))
            if k == 18:
                rows.append({"ev": "bag_sitting_result", "at": base + k * 10 + 1, "verb": "kirim",
                             "items": [{"name": "Parsnip", "moved": ship}]})
        rows.append({"ev": "live7_wallet", "money": 100 + gold * (d + 1), "date": [1, "spring", 1 + d]})
        if d not in unended:
            rows.append(session_end())
        _journal(run, rows, day="day-%04d-attempt-000" % (d + 1))
    if rescues is not None:
        (run / "rescues.tsv").write_text("".join("R%d\t%s\tShippingMenu\tclick OK\n" % (i + 1, SC.wib(T0 + 60 + i))
                                                 for i in range(rescues)), encoding="utf-8")
    return SC.score(run, now=T0)


def _words(lines):
    return tuple(line.split(": ", 1)[1].split(" - ", 1)[0] for line in lines)


def test_run_length_never_decides_a_verdict_at_equal_rates(tmp_path):
    short, long_ = _steady(tmp_path / "short", 30), _steady(tmp_path / "long", 108)
    assert (short["active_minutes"], long_["active_minutes"]) == (30.0, 108.0)
    assert long_["achieved"]["places"] == ["Farm", "Mountain", "Town"] and short["achieved"]["places"] == ["Farm", "Town"]
    assert _words(SC.verdicts(short, long_)) == ("SAME", "SAME", "SAME")
    assert _words(SC.verdicts(long_, short)) == ("SAME", "SAME", "SAME")
    q1, _, q3 = SC.verdicts(short, long_)
    assert "same: live hands 100%/100%; unknown: rescues/h ?/? (a run without rescues.tsv)" in q1
    assert ("same: gold/h +500/+500, shipped or sold/h 10.0/10.0, nights ended/h 10.0/10.0, people 1/1, festivals 1/1, "
            "places 2/2, skills finished/h 370/370 (this/long; people, festivals and places within the first 30 active "
            "min of both)") in q3
    assert "totals: gold +250/+900, shipped or sold 5/18, nights ended 5/18, skills finished 185/666" in q3


def test_each_rate_varied_alone_moves_only_its_own_verdict(tmp_path):
    long_ = _steady(tmp_path / "long", 108)
    cases = [
        (dict(fail_days=(2, 3, 4)), ("WORSE", "SAME", "SAME")),  # live hands share; finished skills per hour unchanged
        (dict(stuck_day=2), ("SAME", "WORSE", "SAME")),  # seconds stuck per hour
        (dict(finish_every=2), ("SAME", "SAME", "LESS")),  # skills finished per hour
        (dict(gold=100), ("SAME", "SAME", "MORE")),  # gold per hour
        (dict(ship=3), ("SAME", "SAME", "MORE")),  # shipped units per hour
        (dict(unended=(1, 2, 3)), ("SAME", "SAME", "LESS")),  # days ended per hour
        (dict(extra_person_day=3), ("SAME", "SAME", "MORE")),  # people within the matched window
    ]
    for n, (kwargs, want) in enumerate(cases):
        short = _steady(tmp_path / ("short%d" % n), 30, **kwargs)
        assert _words(SC.verdicts(short, long_)) == want, kwargs


def test_rescues_compare_as_a_rate_and_missing_records_are_unknown(tmp_path):
    long_ = _steady(tmp_path / "long", 108, rescues=0)
    clean = _steady(tmp_path / "clean", 30, rescues=0)
    rescued = _steady(tmp_path / "rescued", 30, rescues=1)
    unrecorded = _steady(tmp_path / "unrecorded", 30)
    assert _words(SC.verdicts(clean, long_))[0] == "SAME"
    q1 = SC.verdicts(rescued, long_)[0]
    assert q1.startswith("Q1 hands without a hand: WORSE - worse: rescues/h 2.0/0.0; same: live hands 100%/100%")
    assert "rescues 1 (longest unaided" in q1
    assert _words(SC.verdicts(long_, rescued))[0] == "BETTER"
    q1 = SC.verdicts(unrecorded, long_)[0]
    assert q1.startswith("Q1 hands without a hand: SAME - same: live hands 100%/100%; unknown: rescues/h ?/0.0 "
                         "(a run without rescues.tsv)")
    assert "rescues none recorded / 0 (longest unaided" in q1


def test_zero_exposure_is_unknown_not_a_number(tmp_path):
    long_ = _steady(tmp_path / "long", 108)
    empty = tmp_path / "blank"
    _journal(empty, [vitals(T0), done()])  # one timestamp: no active second
    blank = SC.score(empty, now=T0)
    assert blank["active_s"] == 0.0
    assert _words(SC.verdicts(blank, long_)) == ("UNKNOWN", "UNKNOWN", "UNKNOWN")
    assert _words(SC.verdicts(long_, blank)) == ("UNKNOWN", "UNKNOWN", "UNKNOWN")
    assert "per hour  gold ?, shipped or sold ?, nights ended ?, skills finished ?" in SC.table(blank)


def test_q3_is_no_vote_tonights_shape_is_mixed_with_gold_and_shipped_named_as_fallen(tmp_path):
    # one private run against another: more skills an hour and more people, far less gold and shipping
    long_ = _steady(tmp_path / "long", 108)
    tonight = _steady(tmp_path / "tonight", 30, gold=7, ship=0, finish_per_row=2, extra_person_day=3)
    q3 = SC.verdicts(tonight, long_)[2]
    assert q3.startswith("Q3 achieved: MIXED - rose: people 2/1, skills finished/h 740/370; "
                         "fell: gold/h +70/+500, shipped or sold/h 0.0/10.0; same: nights ended/h 10.0/10.0")
    q3 = SC.verdicts(long_, tonight)[2]
    assert q3.startswith("Q3 achieved: MIXED - rose: gold/h +500/+70, shipped or sold/h 10.0/0.0; "
                         "fell: people 1/2, skills finished/h 370/740; same: nights ended/h 10.0/10.0")


def test_q3_one_direction_reads_more_or_less_and_within_margins_same(tmp_path):
    long_ = _steady(tmp_path / "long", 108)
    up = _steady(tmp_path / "up", 30, gold=100, ship=3, extra_person_day=3, finish_per_row=2)
    down = _steady(tmp_path / "down", 30, gold=10, ship=0, finish_every=2)
    near = _steady(tmp_path / "near", 30, gold=52)  # 520 against 500 gold an hour: inside the 10 % margin
    assert _words(SC.verdicts(up, long_))[2] == "MORE" and _words(SC.verdicts(long_, up))[2] == "LESS"
    assert _words(SC.verdicts(down, long_))[2] == "LESS" and _words(SC.verdicts(long_, down))[2] == "MORE"
    assert _words(SC.verdicts(near, long_))[2] == "SAME" and _words(SC.verdicts(long_, near))[2] == "SAME"
    q3 = SC.verdicts(down, long_)[2]
    assert q3.startswith("Q3 achieved: LESS - fell: gold/h +100/+500, shipped or sold/h 0.0/10.0, "
                         "skills finished/h 190/370; same:")


def test_q1_without_observed_hands_is_unknown_and_its_two_measures_never_vote(tmp_path):
    long_ = _steady(tmp_path / "long", 108, rescues=2)
    for runs in ("none", "refused"):  # an active span with no run ending, or only refusals before a start
        quiet = _steady(tmp_path / runs, 30, runs=runs, rescues=0)
        assert quiet["hands"]["coverage"] == 0.0
        q1 = SC.verdicts(quiet, long_)[0]
        # her hands unobserved: UNKNOWN even though fewer rescues were recorded (a rescue rate cannot vouch for hands)
        assert q1.startswith("Q1 hands without a hand: UNKNOWN - better: rescues/h 0.0/1.1; unknown: live hands ?/100%")
        assert "hands observed 0% of 0.5 h" in q1
        assert _words(SC.verdicts(_steady(tmp_path / (runs + "-norec"), 30, runs=runs), long_))[0] == "UNKNOWN"
    worse_hands = _steady(tmp_path / "dead", 30, fail_days=(2, 3, 4), rescues=0)
    q1 = SC.verdicts(worse_hands, long_)[0]
    assert q1.startswith("Q1 hands without a hand: MIXED - better: rescues/h 0.0/1.1; worse: live hands ")
    assert SC.verdicts(long_, worse_hands)[0].startswith("Q1 hands without a hand: MIXED - better: live hands ")


def test_a_refusal_before_a_start_never_cancels_an_open_run(tmp_path):
    run = tmp_path / "mabar-unstarted"
    _journal(run, [vitals(T0),
                   {"ev": "skill_started", "skill": "mancing", "arg": "", "admitted": True},
                   vitals(T0 + 10),
                   {"ev": "skill_failed", "skill": "pergi_ke", "arg": "town",  # `_skill_fail_unstarted`'s row
                    "reason": "precondition:fresh_inventory_unavailable", "started": False},
                   vitals(T0 + 20)])
    card = SC.score(run, now=T0 + 20)
    s = card["skills"]
    assert (s["started"], s["finished"], s["failed"], s["open"], s["refused_before_start"]) == (1, 0, 0, 1, 1)
    assert [tuple(x) for x in s["top_failures"]] == [("precondition:fresh_inventory_unavailable", 1)]
    assert card["hands"]["series"] == [] and card["trips"] == {"finished": 0, "failed": 0}
    assert "1 refused before a start" in SC.table(card)


def test_a_decision_provider_fallback_gets_one_line(tmp_path):
    run = tmp_path / "mabar-fallback"
    _journal(run, [vitals(T0), done()])
    assert "provider  " not in SC.table(SC.score(run, now=T0))
    _journal(run, [{"ev": "decision_provider_fallback", "provider": "openai_api", "fallback_provider": "codex_subscription",
                    "reason": "timeout", "running_usd": 0.2, "at": T0 + 30}])
    card = SC.score(run, now=T0 + 30)
    assert [(f["provider"], f["to"], f["reason"], f["at"]) for f in card["provider_fallbacks"]] == [
        ("openai_api", "codex_subscription", "timeout", T0 + 30)]
    assert "provider  %s openai_api -> codex_subscription (timeout), c1 day-0001" % SC.wib(T0 + 30) in SC.table(card)


def _listing(folder):
    return sorted((str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns) for p in folder.rglob("*"))


def test_the_command_is_read_only_on_the_run_and_refuses_json_inside_it(tmp_path, capsys):
    run = tmp_path / "mabar-cli"
    _dead_hands_run(run)
    (run / "rescues.tsv").write_text("R1\t21:45:00\tShippingMenu\tclick OK\n", encoding="utf-8")
    before = _listing(run)
    out = tmp_path / "cards" / "now.json"
    assert main([str(run), "--compare", str(run), "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "SCORECARD mabar-cli" in printed and "Q1 hands without a hand: SAME" in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["card"]["skills"]["failed"] == 25 and payload["verdicts"][0]["against"] == "mabar-cli"
    assert main([str(run), "--json", str(run / "card.json")]) == 2
    assert main([str(tmp_path / "missing")]) == 2
    assert _listing(run) == before


def test_journals_below_a_cycle_folder_and_natural_cycle_order(tmp_path):
    run = tmp_path / "run"
    _journal(run, [vitals(T0 + 100), done()], cycle="c10")
    _journal(run, [vitals(T0), fail()], cycle="c2")
    names = [p.parents[2].name for p in SC.journals(run)]
    assert names == ["c2", "c10"]
    assert [p.parents[1].name for p in SC.journals(run / "runtime" / "live7" / "c2")] == ["day-0001-attempt-000"]
    assert os.path.basename(str(SC.journals(run)[0])) == "g1_journal.jsonl"
