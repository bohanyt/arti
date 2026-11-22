"""A7/A8 unit contracts; synthetic facts only, no game or private state."""
from types import SimpleNamespace as NS
import json

import pytest

from arti_stardew_moments import MomentSource
from arti_runtime.event_framing import OwnPlayText
from arti_live7_moments import MomentsRelay
from tests.test_live7_moments import write_moment


CFG = {"live7_reactive_play_enabled": True}


def test_short_intent_waits_for_outcome(tmp_path):
    source = MomentSource(tmp_path, lambda row: None, config=CFG)
    source.intent("Aku mau panen.", skill="panen", arg="parsnip")
    assert source.pending == []
    source.reactive_outcome(dict(skill="panen", arg="parsnip", verified=True, outcome_class="VERIFIED",
                                 reason="observed_effect", evidence={}),
                            {"inventory": {}}, {"inventory": {"parsnip": 2}},
                            {}, NS(session_id="test", campaign=NS(date=(1, "spring", 2))), 900)
    assert len(source.pending) == 1
    assert source.pending[0][0]["facts"]["niat"] == "Aku mau panen."


def test_reactive_prompt_is_grounded_model_turn():
    text = OwnPlayText("[Game] panen", created_at=100,
                       facts={"niat": "Aku mau panen.", "outcome": ["Parsnip +2"]})
    assert text.direct_speech is None
    assert "Parsnip +2" in text
    assert "Ceritakan dengan satu kalimat pendek" in text


def session():
    return NS(session_id="test", campaign=NS(date=(1, "spring", 2)))


def row(skill="pergi_ke", arg="Town", **extra):
    return dict(skill=skill, arg=arg, verified=True, outcome_class="VERIFIED",
                reason="observed_effect", evidence={}, **extra)


@pytest.mark.parametrize("skill,eta,long", [("pergi_ke", 0, True), ("jalan_ke", 0, True),
    ("panen", 20, False), ("panen", 20.1, True), ("panen", None, False)])
def test_long_starts_and_short_waits(tmp_path, skill, eta, long):
    source = MomentSource(tmp_path, lambda r: None, config=CFG)
    source.intent("Aku mau ke kota.", skill=skill, arg="Town", eta=eta, goal_ref="visit", place="Farm")
    assert len(source.pending) == int(long)
    if long:
        facts = source.pending[0][0]["facts"]
        assert {k: facts[k] for k in ("skill", "arg", "long")} == dict(skill=skill, arg="Town", long=True)
        text = OwnPlayText("", created_at=1, facts=facts)
        assert text.direct_speech is None
        assert "Bilang dengan satu kalimat pendek" in text


def test_long_threshold_is_configurable(tmp_path):
    source = MomentSource(tmp_path, lambda r: None, config={**CFG, "live7_long_action_sec": 5})
    source.intent("panen", skill="panen", eta=6)
    assert len(source.pending) == 1


@pytest.mark.parametrize("context,after,extra,notable", [
    ({}, {"location": "Town"}, {}, True),
    ({}, {"location": "Farm"}, {}, False),
    ({"jalan_terhalang": "pagar"}, {"location": "Farm"}, {}, True),
    ({"route_home_blocked": {"note": "tertutup"}}, {"location": "Farm"}, {}, True),
    ({}, {"location": "Farm"}, {"outcome_class": "CLEAN_FAIL", "verified": False}, True),
    ({"targets": {"npc": {"kind": "npc", "villager": True, "internal_name": "Leah",
                            "display_name": "Leah", "tile": {"x": 1, "y": 2}}}}, {"location": "Farm"}, {}, True),
    ({"festival_hari": "Egg Festival"}, {"location": "Farm"}, {}, True),
    ({}, {"location": "Farm", "inventory": {"(O)388": 2}}, {}, True),
    ({"bisa_di_sini": ""}, {"location": "Farm"}, {}, True),
])
def test_long_outcome_only_when_notable(tmp_path, context, after, extra, notable):
    source = MomentSource(tmp_path, lambda r: None, config=CFG)
    source.intent("Aku mau ke kota.", skill="pergi_ke", arg="Town", place="Farm")
    source.pending.clear()
    receipt = row()
    receipt.update(extra)
    assert source.reactive_outcome(receipt, {"location": "Farm", "inventory": {}}, after, context, session(), 900) is notable
    assert len(source.pending) == int(notable)


@pytest.mark.parametrize("skill", ["inventori", "tutup_menu", "cek_jurnal", "cek_pagi", "pegang", "siapkan_pancing", "langkah"])
def test_menu_silent_unless_new(tmp_path, skill):
    source = MomentSource(tmp_path, lambda r: None, config=CFG)
    source.intent("lihat dulu", skill=skill, place="Farm")
    receipt = row(skill, None)
    state = {"location": "Farm", "inventory": {}}
    assert not source.reactive_outcome(receipt, state, state, {}, session(), 900)
    source.intent("lihat dulu", skill=skill, place="Farm")
    assert source.reactive_outcome(receipt, state, dict(state, inventory={"(O)388": 1}), {}, session(), 900)


def test_repetition_goal_normalized_intent_and_new_receipt(tmp_path, monkeypatch):
    import arti_stardew_moments as module
    clock = [100.0]
    monkeypatch.setattr(module, "time", NS(monotonic=lambda: clock[0], time=lambda: clock[0], time_ns=lambda: int(clock[0] * 1e9)))
    source = MomentSource(tmp_path, lambda r: None, config={**CFG, "live7_intent_repeat_sec": 600})
    a = dict(niat="Aku mau PANEN!", goal_ref="crop")
    assert source._intent_allowed(a, ["Parsnip +1"])
    assert not source._intent_allowed(dict(niat="aku mau panen", goal_ref="other"), ["Parsnip +1"])
    assert not source._intent_allowed(dict(niat="coba lagi", goal_ref="crop"), ["Parsnip +1"])
    assert source._intent_allowed(a, ["Parsnip +2"])
    assert not source._intent_allowed(a, ["Parsnip +2", "Parsnip +1"])
    assert not source._intent_allowed(a, ["  parsnip  +1  "])
    assert not source._intent_allowed(a, [])
    clock[0] += 601
    assert source._intent_allowed(a, [])


def test_sleep_uses_native_truth_not_fixed_bed_or_previous_night(tmp_path):
    from arti_runtime.sleep_truth import page_line
    source = MomentSource(tmp_path, lambda r: None, config=CFG)
    source.intent("Aku mau istirahat.", skill="tidur", page={"semalam": "old night"})
    receipt = row("tidur", None)
    receipt["evidence"] = {"observed": {"answered": True, "bed_time": 2200}}
    assert source.reactive_outcome(receipt, {}, {}, {}, session(), 600)
    facts = source.pending[0][0]["facts"]
    assert facts["outcome"] == [page_line("slept")]
    assert "Hari sudah berganti" not in str(facts) and "old night" not in str(facts)


def test_short_outcome_does_not_borrow_other_skill_intent(tmp_path):
    source = MomentSource(tmp_path, lambda r: None, config=CFG)
    source.intent("Aku mau panen.", skill="panen")
    source.reactive_outcome(row("siram", None), {}, {}, {}, session(), 900)
    assert not source.pending[0][0]["facts"].get("niat")


def relay(**over):
    return MomentsRelay(dict(live7_moments_enabled=True, **over), started_at=1000)


def test_same_tick_merges_newest_and_counts_other_facts(tmp_path):
    r = relay(live7_reactive_play_enabled=True, live7_own_play_enabled=True)
    for n in range(3):
        write_moment(tmp_path, 1001 + n, seq=n, situation=f"[Stardew] fact {n}")
    voiced = []
    r.tick(tmp_path, lambda text, **kw: voiced.append(text) or True, now=1005)
    assert len(voiced) == 1 and "fact 2" in voiced[0]
    assert voiced[0].play_facts["outcome"][-1] == "dan 2 hal lain"
    assert r.dropped == 0 and not r.by_reason


@pytest.mark.parametrize("own", [False, True])
def test_queue_refusal_is_counted_even_with_flags_off(tmp_path, own):
    r = relay(live7_own_play_enabled=own)
    write_moment(tmp_path, 1001)
    r.tick(tmp_path, lambda *a, **kw: False, now=1002)
    reason = "queue_refused_own_play" if own else "queue_refused_game"
    assert r.by_reason == {reason: 1}
    diagnostics = json.loads((tmp_path / "moments-drops.json").read_text())
    assert diagnostics["dropped"] == 1 and diagnostics["by_reason"] == {reason: 1}
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("gate,reason", [("help", "help_open"), ("status", "status_pending"),
                                        ("chat", "chat_waiting"), ("busy", "busy"), ("gap", "gap")])
def test_gate_drop_reasons(tmp_path, gate, reason):
    r = MomentsRelay({"live7_moments_enabled": True}, started_at=1000,
                     viewer_waiting=lambda: gate == "chat", game_trigger_waiting=lambda: gate == "busy")
    if gate == "gap":
        r.note_voiced(1001)
    write_moment(tmp_path, 1001)
    r.tick(tmp_path, lambda *a, **kw: True, now=1002, help_open=gate == "help", status_pending=gate == "status")
    assert r.by_reason == {reason: 1}


def test_45_second_config_gap_overrides_legacy_relay_gap(tmp_path):
    r = relay(live7_reactive_play_enabled=True, live7_own_play_enabled=True,
              live7_play_line_gap_sec=45, live7_moments_min_gap_sec=90)
    voiced = []
    for at in (1001, 1045, 1046):
        write_moment(tmp_path, at)
        r.tick(tmp_path, lambda text, **kw: voiced.append(text) or True, now=at)
    assert len(voiced) == 2 and r.by_reason == {"gap": 1}
    assert r._gap() == 45


@pytest.mark.parametrize("code,changes,raw", [
    ("json", {}, "{"), ("shape", {}, "[]"), ("schema", {"schema": "bad"}, None),
    ("kind", {"kind": "bad"}, None), ("prefix", {"situation": "bad"}, None),
    ("situation", {"situation": "[Stardew]"}, None), ("ts", {"ts": -1}, None),
    ("ttl_s", {"ttl_s": 1}, None), ("size", {}, "x" * 70000),
    ("facts", {"schema": "arti.game_event/1", "kind": "discovery", "facts": [1]}, None),
    ("count", {"schema": "arti.game_event/1", "kind": "discovery", "count": 0}, None),
    ("mechanical_intent", {"schema": "arti.game_event/1", "kind": "discovery", "facts": {"kind": "own_intent", "text": ""}}, None),
    ("not_notable", {"importance": "low"}, None),
    ("stale_ttl", {"ts": 1}, None),
], ids=lambda value: value[:40] if isinstance(value, str) else None)
def test_decode_drop_codes_counted(tmp_path, code, changes, raw):
    r = relay(live7_own_play_enabled=True)
    write_moment(tmp_path, 1001, raw=raw, **changes)
    r.tick(tmp_path, lambda *a, **kw: True, now=1002)
    assert r.by_reason == {code: 1}


def test_filename_tick_newest_and_bed_superseded_reasons(tmp_path):
    r = relay()
    bad = write_moment(tmp_path, 1001)
    bad.rename(tmp_path / "ev-bad.json")
    for seq in range(2, 4):
        write_moment(tmp_path, 1001, seq=seq)
    for seq in range(4, 6):
        write_moment(tmp_path, 1001, seq=seq, kind="bed")
    r.tick(tmp_path, lambda *a, **kw: True, now=1002)
    assert r.by_reason == {"filename": 1, "tick_newest": 1, "bed_superseded": 1, "gap": 1}


def test_drop_summary_counts_are_atomic_and_content_free(tmp_path, capsys):
    r = relay()
    write_moment(tmp_path, 1001, raw="private invalid payload")
    r.tick(tmp_path, lambda *a, **kw: False, now=1061)
    output = capsys.readouterr().out
    assert output == "[Moments] dropped=1 by_reason={'json': 1}\n"
    assert "private" not in (tmp_path / "moments-drops.json").read_text()


def test_held_bed_queue_refusal_count_does_not_change_retry(tmp_path):
    r = relay()
    write_moment(tmp_path, 1001, kind="bed")
    r.tick(tmp_path, lambda *a, **kw: False, now=1002)
    assert r._held is not None and r.by_reason == {"queue_refused_game": 1}
    r.tick(tmp_path, lambda *a, **kw: True, now=1003)
    assert r._held is None and r.voiced == 1


def test_same_tick_merge_rejection_counts_every_consumed_event(tmp_path):
    r = relay(live7_reactive_play_enabled=True, live7_own_play_enabled=True)
    for seq in range(3):
        write_moment(tmp_path, 1001, seq=seq)
    r.tick(tmp_path, lambda *a, **kw: False, now=1002)
    assert r.dropped == 3 and r.by_reason == {"queue_refused_own_play": 3}


def test_changed_item_total_is_a_new_outcome_for_repeated_intent(tmp_path):
    source = MomentSource(tmp_path, lambda r: None, config=CFG)
    for count in (1, 2):
        source.intent("mau panen", skill="panen", arg=None, goal_ref="crop")
        assert source.reactive_outcome(row("panen", None), {"inventory": {"(O)24": count - 1}},
                                       {"inventory": {"(O)24": count}}, {}, session(), 900)
    assert len(source.pending) == 2
