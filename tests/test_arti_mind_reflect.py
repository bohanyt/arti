"""Mind slice 1: bedtime reflection parse/citation check/apply/failure. Unit evidence only."""
import json

import pytest

import arti_mind as M
import arti_mind_reflect as R

VERIFIED = [dict(ref="r1", text="pergi_ke farm: observed_location_change_objective"),
            dict(ref="r3", text="tebang: wood_collected")]


def reply(**over):
    body = dict(today="hari ini aku nebang pohon terus jalan-jalan", fun=[dict(text="nebang pohon", ref="r3")],
                not_fun=[], goals=dict(keep=[], done=[], drop=[], new=[dict(horizon="today", text="aku mau nanem parsnip"),
                                                                   dict(horizon="week", text="aku mau ke pantai")]),
                tomorrow="besok aku mau nanem")
    body.update(over)
    return json.dumps(body)


def test_schema_is_strict_and_inlined():
    text = json.dumps(R.SCHEMA)
    assert "$ref" not in text and R.SCHEMA["additionalProperties"] is False


def test_unverified_claims_and_unknown_goals_are_dropped():
    raw = reply(fun=[dict(text="nebang pohon", ref="r3"), dict(text="ketemu naga", ref="r99")],
                goals=dict(keep=["ghost"], done=[dict(id="t5-1", ref="r42")], drop=[], new=[
                    dict(horizon="long", text="aku mau tamatin game"), dict(horizon="today", text="aku mau ke kota")]),
                pref_updates=[dict(new=True)])
    result, dropped = R.parse(raw, verified_refs=["r1", "r3"], open_goal_ids=["t5-1"])
    assert result["fun"] == [dict(text="nebang pohon", ref="r3")]
    assert result["goals"]["done"] == [] and result["goals"]["keep"] == []
    # FIX75C: `long` is a horizon of hers; `pref_updates` is not in the schema (her likes are fun / not_fun).
    assert result["goals"]["new"] == [dict(horizon="long", text="aku mau tamatin game"), dict(horizon="today", text="aku mau ke kota")]
    reasons = sorted((d["kind"], d["reason"]) for d in dropped)
    assert reasons == [("done", "unverified_ref"), ("fun", "unverified_ref"), ("keep", "unknown_goal"),
                       ("pref_updates", "not_in_schema_use_fun_not_fun")]


@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps(dict(today=5)), json.dumps(dict(today="x" * 300))])
def test_bad_replies_raise(raw):
    with pytest.raises(R.ReflectionError):
        R.parse(raw, verified_refs=[], open_goal_ids=[])


def test_a_bare_code_fence_is_tolerated():
    result, _ = R.parse("```json\n" + reply() + "\n```", verified_refs=["r3"], open_goal_ids=[])
    assert result["today"].startswith("hari ini")


def test_input_is_capped_at_3kb_and_keeps_goals():
    goals = [dict(id="t5-1", horizon="today", text="aku mau nanem parsnip")]
    payload = R.build_input(day_label="Y1 spring 5", verified=[dict(ref="r%d" % i, text="x" * 140) for i in range(40)],
                            whys=["aku mau " + "y" * 100] * 20, goals_open=goals, prefs=[dict(text="aku suka a")])
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= R.INPUT_MAX_BYTES
    assert payload["goals_open"] == goals


def test_reflect_writes_goals_journey_and_row(tmp_path):
    seen = {}
    def complete(messages, schema):
        seen["messages"], seen["schema"] = messages, schema
        return reply()
    row = R.reflect(complete, tmp_path, game="stardew", day=5, day_label="Y1 spring 5", verified=VERIFIED,
                    whys=["aku mau nebang pohon dulu"], places=["Farm"], effects={"tebang": 1})
    assert row["ev"] == "mind_reflection" and row["fun_refs"] == ["r3"] and row["new_goals"] == ["t6-1", "w6-2"]
    assert seen["schema"] is R.SCHEMA and seen["messages"][0]["content"] == R.SYSTEM
    live = M.open_goals(M.load_goals(tmp_path, "stardew"), 6)
    assert [g["text"] for g in live["today"]] == ["aku mau nanem parsnip"]
    assert [g["text"] for g in live["week"]] == ["aku mau ke pantai"]
    journey = M.load_journey(tmp_path, "stardew")
    assert journey[0]["reflection"]["today"].startswith("hari ini") and journey[0]["effects"] == {"tebang": 1}
    assert M.yesterday_line(tmp_path, "stardew", 6) == "hari ini aku nebang pohon terus jalan-jalan"


def test_next_reflection_marks_done_keeps_and_expires(tmp_path):
    R.reflect(lambda m, s: reply(), tmp_path, game="stardew", day=5, day_label="d5", verified=VERIFIED, whys=[])
    second = reply(goals=dict(keep=["w6-2"], done=[dict(id="t6-1", ref="r1")], drop=[], new=[]))
    row = R.reflect(lambda m, s: second, tmp_path, game="stardew", day=6, day_label="d6", verified=VERIFIED, whys=[])
    assert row["done"] == [dict(id="t6-1", ref="r1", evidence="pergi_ke farm: observed_location_change_objective")]
    rows = {g["id"]: g for g in M.load_goals(tmp_path, "stardew")}
    assert rows["t6-1"]["status"] == "done" and rows["w6-2"]["status"] == "open"


def test_new_goals_respect_caps(tmp_path):
    many = reply(goals=dict(keep=[], done=[], drop=[], new=[dict(horizon="today", text="aku mau hal %d" % i)
                                                            for i in range(4)]))
    row = R.reflect(lambda m, s: many, tmp_path, game="stardew", day=5, day_label="d5", verified=VERIFIED, whys=[])
    assert len(row["new_goals"]) == 3 and any(d["reason"] == "cap" for d in row["dropped"])


def test_failure_carries_goals_stale_and_invents_nothing(tmp_path):
    R.reflect(lambda m, s: reply(), tmp_path, game="stardew", day=5, day_label="d5", verified=VERIFIED, whys=[])
    def boom(messages, schema):
        raise RuntimeError("provider_hourly_limit")
    row = R.reflect(boom, tmp_path, game="stardew", day=6, day_label="d6", verified=VERIFIED, whys=[])
    assert row["ev"] == "mind_reflection_failed" and row["alert"] is True and row["code"] == "RuntimeError"
    assert row["carried"] == ["t6-1", "w6-2"]  # FIX75C: every horizon carries on a failed night
    live = M.open_goals(M.load_goals(tmp_path, "stardew"), 7)
    assert [(g["id"], g.get("stale")) for g in live["today"]] == [("t6-1", True)]
    assert len(M.load_journey(tmp_path, "stardew")) == 1  # no journey row invented for the failed day
    bad = R.reflect(lambda m, s: "nope", tmp_path, game="stardew", day=7, day_label="d7", verified=[], whys=[])
    assert bad["ev"] == "mind_reflection_failed" and bad["code"] == "not_json"
