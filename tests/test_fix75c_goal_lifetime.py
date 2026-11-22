"""FIX75C (ADR 133): a goal she wrote lasts until native evidence closes it or she drops it; her fun / not_fun are memory.

Evidence: reflection rows of a recorded run (one goal carried nearly a week,
some dropped by her, some done).
Every night below runs `reflect` from the files alone, alternating two base folders (a copy between nights), so each
night is a restart. Unit/replay evidence only; no game session.
"""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import arti_mind as M
import arti_mind_reflect as R

GAME = "stardew"
VERIFIED = [dict(ref="r1", text="pergi_ke farm: observed_location_change_objective"),
            dict(ref="r3", text="tebang: wood_collected")]
PROGRESS = [dict(ref="p:cc", text="Community Center: 0/31 bundles"), dict(ref="p:skills", text="Farming level 1")]


def reply(**over):
    body = dict(today="hari ini aku nebang pohon terus jalan-jalan", fun=[], not_fun=[],
                goals=dict(keep=[], done=[], drop=[], new=[], progress=[], calendar_links=[]), tomorrow="besok aku lanjut")
    goals = dict(body["goals"], **over.pop("goals", {}))
    body.update(over)
    body["goals"] = goals
    return json.dumps(body)


class Nights:
    """Runs one reflection per game day from disk; every night moves the files to the other folder (a restart)."""

    def __init__(self, tmp_path):
        self.bases = [tmp_path / "mind-a", tmp_path / "mind-b"]
        self.base = self.bases[0]
        self.rows = []

    def restart(self):
        other = self.bases[1] if self.base == self.bases[0] else self.bases[0]
        if other.exists():
            shutil.rmtree(other)
        if self.base.exists():
            shutil.copytree(self.base, other)
        self.base = other

    def night(self, day, *, verified=VERIFIED, facts=PROGRESS, fun=(), not_fun=(), whys=("aku mau nebang dulu",), **goals):
        self.restart()
        raw = reply(fun=list(fun), not_fun=list(not_fun), goals=goals)
        row = R.reflect(lambda m, s: raw, self.base, game=GAME, day=day, day_label=M.plan_dates.label(GAME, day),
                        verified=list(verified), whys=list(whys), progress=list(facts))
        assert row["ev"] == "mind_reflection", row
        self.rows.append(row)
        return row

    def goals(self):
        return {g["id"]: g for g in M.load_goals(self.base, GAME)}

    def live(self, day):
        return M.open_goals(M.load_goals(self.base, GAME), day)

    def block(self, day):
        return M.render_block(M.load_prefs(self.base), M.load_goals(self.base, GAME), game=GAME, day=day)

    def payload(self, day):
        """What the next bedtime shows her, read from a copy of the files (the capture never writes her state)."""
        peek = self.base.parent / "peek"
        if peek.exists():
            shutil.rmtree(peek)
        shutil.copytree(self.base, peek)
        seen = {}
        def capture(messages, schema):
            seen.update(json.loads(messages[1]["content"]))
            raise RuntimeError("capture_only")
        R.reflect(capture, peek, game=GAME, day=day, day_label=M.plan_dates.label(GAME, day), verified=[], whys=[])
        return seen


# --------------------------------------------------------------------------- 1. lifetime

def test_a54_t20_1_survives_the_eighth_night_and_thirty_more(tmp_path):
    n = Nights(tmp_path)
    n.night(19, new=[dict(horizon="today", text="aku mau nyiram kebun lalu ke kota", done_when="")])
    assert n.rows[-1]["new_goals"] == ["t20-1"]
    for day in range(20, 51):  # a54 stopped at the 7th carry (day 26); the old code expired it on the 8th
        n.night(day, keep=["t20-1"] if day % 3 else [])
    goal = n.goals()["t20-1"]
    assert goal["status"] == "open" and goal["carried_days"] == 31 and goal["for_day"] == 51
    today = n.block(51)["tujuan"]["today"]
    assert today[0]["id"] == "t20-1" and today[0]["umur_hari"] == 31 and today[0]["maju_terakhir"] == "belum ada bukti"
    assert n.rows[-1]["alive"] == {"today": 1, "week": 0, "long": 0} and n.rows[-1]["oldest_age_days"] == 31
    assert n.rows[-1]["carried"][0]["umur_hari"] == 31


def test_week_and_long_goals_have_no_ttl_and_long_is_a_horizon(tmp_path):
    n = Nights(tmp_path)
    row = n.night(4, new=[dict(horizon="week", text="aku mau beli pancingan", done_when="pancingan di tas"),
                          dict(horizon="long", text="aku mau benerin Community Center", done_when="semua ruangan selesai")])
    assert row["new_goals"] == ["w5-1", "l5-2"] and row["dropped"] == []
    for day in range(5, 41):
        n.night(day)
    live = n.live(41)
    assert [g["id"] for g in live["week"]] == ["w5-1"] and [g["id"] for g in live["long"]] == ["l5-2"]  # 36 days, no TTL
    block = n.block(41)
    assert block["tujuan"]["long"][0]["umur_hari"] == 36 and block["tujuan"]["week"][0]["umur_hari"] == 36
    assert M.block_bytes(block) <= M.BLOCK_MAX_BYTES and M.steering_hits(json.dumps(block)) == []


def test_last_progress_she_cited_rides_as_a_dated_fact(tmp_path):
    n = Nights(tmp_path)
    n.night(4, new=[dict(horizon="long", text="aku mau benerin Community Center", done_when="")])
    n.night(5)
    n.night(9, progress=[dict(id="l5-1", ref="p:cc")])
    n.night(10)
    view = n.block(11)["tujuan"]["long"][0]
    assert view["umur_hari"] == 6 and view["maju_terakhir"] == "Y1 spring 9: Community Center: 0/31 bundles"
    payload = n.payload(11)
    assert payload["goals_open"][0]["maju_terakhir"].startswith("Y1 spring 9: ") and payload["slots"] == {"today": "0/3", "week": "0/3", "long": "1/3"}


def test_a_missed_night_still_carries_a_today_goal(tmp_path):
    rows = [dict(v=1, id="t5-1", horizon="today", text="aku mau ke kota", status="open", set_on_day=5, for_day=5)]
    assert M.goal_alive(rows[0], 8)  # three nights without any reflection (crash, no runner): still hers
    out, _ = R.apply(rows, dict(goals=dict(keep=[], done=[], drop=[], new=[], progress=[])), day=8)
    assert out[0]["for_day"] == 9 and out[0]["carried_days"] == 1 and out[0]["status"] == "open"
    stale = R.carry_stale(rows, day=8)
    assert stale[0]["for_day"] == 9 and stale[0]["stale"] is True


def test_she_drops_it_and_the_release_is_a_fact_shown_once(tmp_path):
    n = Nights(tmp_path)
    n.night(12, new=[dict(horizon="today", text="aku mau cek surat di rumah", done_when="")])
    n.night(13, keep=["t13-1"])
    row = n.night(14, drop=["t13-1"])  # a54 d14: t13-1 dropped by her
    assert row["dropped_by_her"] == 1 and n.goals()["t13-1"]["status"] == "dropped"
    assert n.block(15)["tujuan"]["dilepas"][0]["status"] == "dropped on Y1 spring 14"
    assert "dilepas" not in n.block(17).get("tujuan", {}) and n.live(17)["today"] == []


# --------------------------------------------------------------------------- 2. her writing

def test_bedtime_prompt_stays_neutral_and_adds_no_bundle_or_forage_words():
    low = R.SYSTEM.lower()
    assert M.steering_hits(R.SYSTEM) == []
    for word in ("bundle", "forage", "hijauan", "peti", "chest", "kuota", "quota", "target", "wajib"):
        assert word not in low, word
    assert "long = sampai kejadiannya terjadi" in R.SYSTEM and "Tujuan itu pilihanmu sendiri" in R.SYSTEM
    assert "horizon" in R.SCHEMA["properties"]["goals"]["properties"]["new"]["items"]["properties"]
    assert R.SCHEMA["properties"]["goals"]["properties"]["new"]["items"]["properties"]["horizon"]["enum"] == ["today", "week", "long"]
    assert "pref_updates" not in json.dumps(R.SCHEMA)


def test_no_goal_is_ever_written_by_code(tmp_path):
    n = Nights(tmp_path)
    for day in range(5, 20):
        n.night(day, facts=[])  # fifteen nights with letters/quests/progress facts and an empty `new`
    assert n.goals() == {} and all(r["new_goals"] == [] for r in n.rows)


# --------------------------------------------------------------------------- 3. evidence

def test_native_evidence_closes_a_goal_only_through_a_cited_read(tmp_path):
    n = Nights(tmp_path)
    n.night(10, new=[dict(horizon="week", text="aku mau naik level farming", done_when="Farming level 1")])
    row = n.night(11, done=[dict(id="w11-1", ref="r99")])  # a ref no verified row or progress fact has
    assert n.goals()["w11-1"]["status"] == "open" and row["closed_with_evidence"] == []
    assert any(d["kind"] == "done" and d["reason"] == "unverified_ref" for d in row["dropped"])
    row = n.night(15, done=[dict(id="w11-1", ref="p:skills")])  # a54 d15: w11-1 done with p:skills
    goal = n.goals()["w11-1"]
    assert goal["status"] == "done" and goal["done_ref"] == "p:skills" and goal["done_evidence"] == "Farming level 1"
    assert row["closed_with_evidence"] == ["w11-1"] and n.live(16)["week"] == []


def test_a_done_claim_whose_read_has_no_text_is_not_counted_as_closed_with_evidence(tmp_path):
    n = Nights(tmp_path)
    n.night(10, new=[dict(horizon="today", text="aku mau ke pantai", done_when="")])
    row = n.night(11, verified=[dict(ref="r7", text="")], done=[dict(id="t11-1", ref="r7")])
    assert n.goals()["t11-1"]["status"] == "done" and row["closed_with_evidence"] == []


def test_a_goal_without_a_checkable_condition_stays_hers_to_close(tmp_path):
    n = Nights(tmp_path)
    n.night(10, new=[dict(horizon="long", text="aku mau ngerti game ini pelan-pelan", done_when="")])
    for day in range(11, 31):
        n.night(day)  # twenty nights of native facts; nothing in code matches or closes it
    goal = n.goals()["l11-1"]
    assert goal["status"] == "open" and "done_ref" not in goal and "native_status" not in n.block(31)["tujuan"]["long"][0]
    assert all(r["closed_with_evidence"] == [] for r in n.rows)


# --------------------------------------------------------------------------- 1b. slots: she empties them herself

def test_a_full_slot_refuses_the_new_goal_shows_it_to_her_and_accepts_it_after_her_drop(tmp_path):
    n = Nights(tmp_path)
    n.night(5, new=[dict(horizon="week", text="aku mau tujuan minggu %d" % i, done_when="") for i in range(3)])
    row = n.night(6, keep=["w6-1", "w6-2", "w6-3"], new=[dict(horizon="week", text="aku mau yang keempat", done_when="")])
    assert [d for d in row["dropped"] if d["kind"] == "new"] == [dict(kind="new", reason="cap", horizon="week", text="aku mau yang keempat")]
    assert {g["id"] for g in n.live(7)["week"]} == {"w6-1", "w6-2", "w6-3"}  # none dropped for her
    payload = n.payload(7)
    assert payload["slots"]["week"] == "3/3"
    assert payload["not_kept_last_night"] == [dict(horizon="week", text="aku mau yang keempat", reason="slot_full")]
    row = n.night(7, drop=["w6-1"], new=[dict(horizon="week", text="aku mau yang keempat", done_when="")])
    assert row["new_goals"] == ["w8-1"] and row["dropped"] == [] and n.goals()["w6-1"]["status"] == "dropped"
    assert "not_kept_last_night" not in n.payload(8)


def test_a_legacy_surplus_is_capped_logged_and_visible_never_silent(tmp_path):
    base = tmp_path / "mind"
    M.save_goals(base, GAME, [dict(v=1, id="w%d-1" % d, horizon="week", text="aku mau hal %d" % d, status="open", set_on_day=d)
                              for d in range(1, 6)])
    assert len(M.open_goals(M.load_goals(base, GAME), 9)["week"]) == 5  # nothing hidden by a slice
    row = R.reflect(lambda m, s: reply(), base, game=GAME, day=9, day_label="Y1 spring 9", verified=VERIFIED, whys=[])
    assert row["capped"] == ["w1-1", "w2-1"] and row["alive"]["week"] == 3
    assert [d for d in row["dropped"] if d["kind"] == "goal"] == [dict(kind="goal", reason="safety_cap", id="w1-1", horizon="week"),
                                                                 dict(kind="goal", reason="safety_cap", id="w2-1", horizon="week")]
    goals = M.load_goals(base, GAME)
    block = M.render_block([], goals, game=GAME, day=10)
    assert [v["id"] for v in block["tujuan"]["dibatasi"]] == ["w1-1", "w2-1"]
    assert block["tujuan"]["dibatasi"][0]["status"] == "closed by the 3-per-horizon safety cap on Y1 spring 10"
    assert "dibatasi" not in M.render_block([], goals, game=GAME, day=13)["tujuan"]
    assert {g["id"]: g["status"] for g in goals if g["id"] in ("w1-1", "w2-1")} == {"w1-1": "capped", "w2-1": "capped"}


def test_goal_history_is_bounded_by_count_and_bytes_and_open_rows_never_go(tmp_path):
    closed = [dict(v=1, id="t%d-1" % d, horizon="today", text="aku mau hal %d" % d, status="done", set_on_day=d, for_day=d,
                   done_evidence="x" * 2000) for d in range(1, 131)]
    open_rows = [dict(v=1, id="l200-%d" % i, horizon="long", text="aku mau tetap %d" % i, status="open", set_on_day=200) for i in range(3)]
    rows = closed[:10] + open_rows + closed[10:]
    kept, trimmed = M.bound_goals(rows)
    assert trimmed >= 10 and all(r in kept for r in open_rows)
    assert sum(len(json.dumps(r, ensure_ascii=False).encode()) + 1 for r in kept) <= M.GOALS_FILE_MAX_BYTES
    assert [r["id"] for r in kept if r["status"] == "done"] == [r["id"] for r in closed[-(len(kept) - 3):]]  # oldest closed first
    base = tmp_path / "mind"
    assert M.save_goals(base, GAME, rows) == trimmed
    assert len(M.open_goals(M.load_goals(base, GAME), 201)["long"]) == 3


# --------------------------------------------------------------------------- 4. enjoyment

def test_fun_and_not_fun_become_dated_memory_and_come_back_to_her(tmp_path):
    n = Nights(tmp_path)
    n.night(5, fun=[dict(text="nebang pohon", ref="r3")], not_fun=[dict(text="jalan jauh ke farm", ref="r1")])
    likes = M.load_likes(n.base, GAME)
    assert [(r["day"], r["date"], r["polarity"], r["text"], r["ref"], r["evidence"]) for r in likes] == [
        (5, "Y1 spring 5", "like", "nebang pohon", "r3", "tebang: wood_collected"),
        (5, "Y1 spring 5", "dislike", "jalan jauh ke farm", "r1", "pergi_ke farm: observed_location_change_objective")]
    assert n.rows[-1]["likes"] == dict(saved=2, trimmed=0)
    n.night(6, fun=[dict(text="mancing di sungai", ref="r3")])
    payload = n.payload(7)
    assert payload["yang_kamu_suka"] == dict(suka=["Y1 spring 5: nebang pohon", "Y1 spring 6: mancing di sungai"],
                                             nggak_suka=["Y1 spring 5: jalan jauh ke farm"])
    assert M.steering_hits(json.dumps(payload["yang_kamu_suka"])) == []
    # a memory, not a rule: the decision block and the pref ranking never read it; soul/live prefs files are untouched
    block = n.block(7)
    assert "yang_kamu_suka" not in json.dumps(block) and "suka" not in block
    assert not (n.base / M.PREFS_FILE).exists() and not list(Path(tmp_path).rglob("ARTI_SOUL*"))


def test_likes_keep_their_shape_and_their_bound(tmp_path):
    base = tmp_path / "mind"
    assert M.save_likes(base, GAME, day=5, day_label="Y1 spring 5", fun=[dict(text="[log] x", ref="r1"), "junk", dict(text="ok", ref="")],
                        not_fun=[]) == dict(saved=0, trimmed=0)
    assert M.load_likes(base, GAME) == []
    for day in range(1, 81):
        M.save_likes(base, GAME, day=day, day_label="d%d" % day, fun=[dict(text="seru " + "y" * 100, ref="r3")],
                     not_fun=[dict(text="nggak " + "z" * 100, ref="r1")], evidence={"r3": "e" * 140, "r1": "f" * 140})
    rows = M.load_likes(base, GAME)
    assert len(rows) <= M.LIKES_MAX and sum(len(json.dumps(r, ensure_ascii=False).encode()) + 1 for r in rows) <= M.LIKES_MAX_BYTES
    assert rows[-1]["day"] == 80 and rows[0]["day"] > 1  # oldest trimmed first, newest kept
    lines = M.likes_lines(base, GAME, before_day=81)
    assert len(lines["suka"]) == 3 == len(lines["nggak_suka"]) and all(len(x) <= M.LIKE_LINE_CHARS for x in lines["suka"])
    assert lines["suka"][-1].startswith("d80: ")
    # re-reflecting the same night replaces that day's rows instead of doubling them
    M.save_likes(base, GAME, day=80, day_label="d80", fun=[dict(text="lain", ref="r3")], not_fun=[])
    assert [r["text"] for r in M.load_likes(base, GAME) if r["day"] == 80] == ["lain"]


def test_pref_updates_is_not_applied_and_unverified_likes_are_dropped(tmp_path):
    raw = reply(fun=[dict(text="ketemu naga", ref="r99")], pref_updates=[dict(new=True, text="aku suka naga")])
    result, dropped = R.parse(raw, verified_refs=["r1", "r3"], open_goal_ids=[])
    assert result["fun"] == [] and sorted((d["kind"], d["reason"]) for d in dropped) == [
        ("fun", "unverified_ref"), ("pref_updates", "not_in_schema_use_fun_not_fun")]
    base = tmp_path / "mind"
    R.reflect(lambda m, s: raw, base, game=GAME, day=5, day_label="Y1 spring 5", verified=VERIFIED, whys=[])
    assert M.load_likes(base, GAME) == [] and not (base / M.PREFS_FILE).exists()


# --------------------------------------------------------------------------- budgets

def _full_slots(day):
    rows = []
    for h, prefix in (("today", "t"), ("week", "w"), ("long", "l")):
        for i in range(3):
            set_on = day - (1 + 9 * i)
            rows.append(dict(v=1, id="%s%d-%d" % (prefix, set_on, i + 1), horizon=h, status="open", set_on_day=set_on,
                             for_day=day if h == "today" else None, carried_days=day - set_on if h == "today" else None,
                             text=("aku mau %s nomor %d " % (h, i)) + "x" * 40,
                             evidence=[[day - 2, "p:cc", "Community Center: 0/31 bundles"]] if i == 1 else []))
    return rows


def test_decision_block_with_every_slot_full_stays_in_its_600_bytes_and_keeps_every_today_goal():
    rows = _full_slots(30)
    block = M.render_block(M.load_prefs(Path("nonexistent")), rows, game=GAME, day=30, yesterday="kemarin aku nebang seharian")
    assert M.block_bytes(block) <= M.BLOCK_MAX_BYTES
    assert [v["id"] for v in block["tujuan"]["today"]] == [r["id"] for r in rows if r["horizon"] == "today"]
    shown = {h: len(block["tujuan"].get(h, ())) for h in M.HORIZONS}
    assert shown["today"] == 3 and shown["week"] + shown["long"] >= 2  # compact first; the rest is reported, not asserted
    assert "kemarin" not in block  # the least recent line goes first, before any goal of hers
    full = M.render_block([], rows, game=GAME, day=30, max_bytes=4000)
    assert all(len(full["tujuan"][h]) == 3 for h in M.HORIZONS) and full["tujuan"]["week"][1]["maju_terakhir"].startswith("Y1 spring 28: ")
    assert all("umur_hari" in v for h in M.HORIZONS for v in full["tujuan"][h])  # age facts ride whenever the page has room


def test_bedtime_input_stays_under_its_cap_with_full_slots_likes_and_a_refused_goal():
    verified = [dict(ref="r%d" % i, text="x" * 140) for i in range(40)]
    whys = ["aku mau " + "y" * 100] * 12
    progress = [dict(ref="p%d" % i, text="z" * 140) for i in range(20)]
    goals = _full_slots(30)
    likes = dict(suka=["Y1 spring %d: seru " % d + "s" * 50 for d in range(3)], nggak_suka=["Y1 spring 9: " + "n" * 55])
    payload = R.build_input(day_label="Y1 spring 30", verified=verified, whys=whys, goals_open=goals, prefs=[dict(text="aku suka a")],
                            progress=progress, game=GAME, day=30, likes=likes,
                            not_kept=[dict(horizon="long", text="aku mau yang keempat " + "k" * 60)])
    assert R._size(payload) <= R.INPUT_MAX_BYTES
    assert len(payload["goals_open"]) == 9 and all("umur_hari" in g and "maju_terakhir" in g for g in payload["goals_open"])
    assert payload["slots"] == {"today": "3/3", "week": "3/3", "long": "3/3"} and len(payload["yang_kamu_suka"]["suka"]) == 3
    assert payload["not_kept_last_night"][0]["reason"] == "slot_full"
    # her goals, slots, likes and the refused goal are never trimmed; the oldest of the largest fact list goes first
    assert len(payload["verified"]) + len(payload["whys"]) + len(payload["progress"]) < 24 + 8 + 20


# --------------------------------------------------------------------------- 5. report

def test_failure_night_carries_every_horizon_and_invents_nothing(tmp_path):
    n = Nights(tmp_path)
    n.night(5, new=[dict(horizon="today", text="aku mau ke kota", done_when=""), dict(horizon="week", text="aku mau ke pantai", done_when=""),
                    dict(horizon="long", text="aku mau tamatin game", done_when="")])
    def boom(messages, schema):
        raise RuntimeError("provider_hourly_limit")
    row = R.reflect(boom, n.base, game=GAME, day=6, day_label="Y1 spring 6", verified=VERIFIED, whys=[])
    assert row["ev"] == "mind_reflection_failed" and row["carried"] == ["t6-1", "w6-2", "l6-3"]
    live = n.live(7)
    assert [(g["id"], g.get("stale")) for g in live["today"]] == [("t6-1", True)]
    assert [g["id"] for g in live["week"]] == ["w6-2"] and [g["id"] for g in live["long"]] == ["l6-3"]
    assert M.load_likes(n.base, GAME) == [] and len(M.load_journey(n.base, GAME)) == 1


def test_reflection_row_carries_the_receipt_facts_and_the_evaluator_prints_the_line(tmp_path):
    n = Nights(tmp_path)
    n.night(5, new=[dict(horizon="long", text="aku mau benerin Community Center", done_when=""),
                    dict(horizon="week", text="aku mau naik level farming", done_when="")])
    n.night(6, keep=["l6-1"], done=[dict(id="w6-2", ref="p:skills")], fun=[dict(text="nebang", ref="r3")])
    row = n.night(7, drop=[], keep=["l6-1"])
    assert {k: row[k] for k in ("alive", "oldest_age_days", "dropped_by_her", "closed_with_evidence", "capped", "history_trimmed")} == dict(
        alive={"today": 0, "week": 0, "long": 1}, oldest_age_days=2, dropped_by_her=0, closed_with_evidence=[], capped=[], history_trimmed=0)
    assert n.rows[1]["closed_with_evidence"] == ["w6-2"] and n.rows[1]["likes"]["saved"] == 1
    from scripts.free_play import evaluate
    slept = dict(ev="skill_session_finished", reason="live7_sleep_verified")
    journals = [(Path(tmp_path, "day-%04d" % i, "journal", "g1_journal.jsonl"), list(enumerate([dict(ev="skill_model_decision", choice="x"), r, slept])))
                for i, r in enumerate(n.rows, 1)]
    result = evaluate.mind(NS(track=lambda _: None), journals)
    lt = result["goal_lifetime"]
    assert lt["goals_alive"] == 1 and lt["oldest_goal_age_days"] == 2 and lt["closed_with_evidence"] == 1 and lt["likes_saved"] == 1
    line = evaluate.goal_lifetime_line(lt)
    assert line.startswith("Goals (FIX75C): alive 1 at the last bedtime (today 0, week 0, long 1); oldest 2 days; dropped by her 0; closed with evidence 1;")
    assert evaluate.goal_lifetime_line({}).startswith("Goals (FIX75C): alive unknown") and evaluate.EVALUATOR_VERSION == "1.7.16"
