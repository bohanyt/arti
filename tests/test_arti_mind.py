"""Mind slice 1: arti_mind stores, caps, render, intent line and lint (game-agnostic). Unit evidence only."""
import json
import os
import time

import arti_mind as M


def prefs_file(base, rows):
    base.mkdir(parents=True, exist_ok=True)
    (base / M.PREFS_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def pref(i, text, about=(), scope="any", source="soul", strength=3):
    return dict(v=1, id="p%d" % i, scope=scope, text=text, about=list(about), polarity="like", source=source,
                pinned=scope == "any", strength=strength)


def goal(gid, horizon, text, day, for_day=None, status="open"):
    return dict(v=1, id=gid, horizon=horizon, text=text, status=status, set_on_day=day,
                for_day=for_day if horizon == "today" else None)


def test_mind_dir_prefers_config_then_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTI_MIND_DIR", str(tmp_path / "env"))
    assert M.mind_dir({}) == tmp_path / "env"
    assert M.mind_dir({"arti_mind_dir": str(tmp_path / "cfg")}) == tmp_path / "cfg"
    monkeypatch.delenv("ARTI_MIND_DIR")
    assert M.mind_dir({"arti_state_dir": str(tmp_path / "state")}) == tmp_path / "state" / "mind"


def test_prefs_default_to_the_example_trait_a_and_file_rows_win(tmp_path):
    # The example traits are the code defaults.
    assert [p["id"] for p in M.load_prefs(tmp_path)] == ["example-trait-a", "example-trait-b", "example-trait-c", "example-trait-d", "example-trait-e", "example-trait-f"]
    prefs_file(tmp_path, [dict(M.OWNER_PREFS[0], text="aku versi pemilik yang diubah sendiri"),
                          pref(1, "aku suka merhatiin detail kecil"), {"id": "bad", "text": ""}, "junk"])
    rows = M.load_prefs(tmp_path)
    assert [p["id"] for p in rows] == ["example-trait-b", "example-trait-c", "example-trait-d", "example-trait-e", "example-trait-f", "example-trait-a", "p1"]
    assert next(r for r in rows if r["id"] == "example-trait-a")["text"] == "aku versi pemilik yang diubah sendiri"


def test_private_texts_are_soul_rows_only(tmp_path):
    prefs_file(tmp_path, [pref(1, "aku sifat dari soul"), pref(2, "aku dari pemilik", source="owner")])
    assert M.private_texts(tmp_path) == ["aku sifat dari soul"]
    assert M.private_texts(tmp_path / "missing") == []


def test_traits_rank_by_relevance_then_strength_and_cap_at_three():
    prefs = [pref(1, "aku satu sifat", strength=5), pref(2, "aku dua sifat", strength=4),
             pref(3, "aku tiga sifat", strength=3), pref(4, "aku suka tidur larut", about=["tidur"], strength=1),
             pref(5, "aku suka mancing di laut", about=["tangkap_ikan"], scope="stardew"),
             pref(6, "aku suka nebang pohon", about=["tebang"], scope="stardew")]
    traits, game = M.pick_prefs(prefs, game="stardew", tags=["tidur", "pergi_ke"])
    assert [t["id"] for t in traits] == ["p4", "p1", "p2"]
    assert game == []  # a game pref renders only when its about matches the boundary
    _, game = M.pick_prefs(prefs, game="stardew", tags=["tangkap_ikan"])
    assert [g["id"] for g in game] == ["p5"]


def test_goals_have_no_ttl_and_the_cap_is_a_logged_status():
    # FIX75C (ADR 133): a goal she wrote is alive until done, dropped or safety-capped; no horizon expires by age.
    rows = [goal("t5-1", "today", "aku mau nanem parsnip", 5, 5), goal("w2-1", "week", "aku mau ke pantai", 2),
            goal("w5-1", "week", "aku mau beli pancingan", 5), goal("t4-1", "today", "aku mau ke kota", 4, 4),
            goal("l3-1", "long", "aku mau benerin Community Center", 3)]
    live = M.open_goals(rows, 5)
    assert [g["id"] for g in live["today"]] == ["t5-1", "t4-1"]  # file order; a today goal whose night was missed is still hers
    assert [g["id"] for g in live["week"]] == ["w2-1", "w5-1"] and [g["id"] for g in live["long"]] == ["l3-1"]
    assert [g["id"] for g in M.open_goals(rows, 40)["week"]] == ["w2-1", "w5-1"]  # 38 game days old, still hers
    assert M.goal_age_days(rows[1], 40) == 38 and not hasattr(M, "expire_goals") and not hasattr(M, "WEEK_TTL_DAYS")
    many = [goal("t5-%d" % i, "today", "aku mau hal nomor %d" % i, 5, 5) for i in range(5)]
    assert len(M.open_goals(many, 5)["today"]) == 5  # nothing hidden by a slice
    out, capped = M.cap_overflow(many, 5)
    assert [g["id"] for g in capped] == ["t5-0", "t5-1"] and all(g["capped_on_day"] == 5 for g in capped)
    assert len(M.open_goals(out, 5)["today"]) == 3
    assert M.render_block([], out, game="stardew", day=5)["tujuan"]["dibatasi"][0]["status"].startswith("closed by the 3-per-horizon safety cap on ")


def test_block_is_capped_at_600_bytes_and_never_drops_today_goals():
    prefs = [pref(i, ("aku sifat panjang nomor %d " % i) + "x" * 60) for i in range(6)]
    goals = [goal("t5-%d" % i, "today", ("aku mau tujuan hari ini %d " % i) + "y" * 60, 5, 5) for i in range(3)]
    goals += [goal("w5-%d" % i, "week", ("aku mau tujuan minggu %d " % i) + "z" * 60, 5) for i in range(3)]
    block = M.render_block(prefs, goals, game="stardew", day=5, tags=(), yesterday="kemarin aku capek banget di tambang")
    assert M.block_bytes(block) <= M.BLOCK_MAX_BYTES
    assert [g["id"] for g in block["tujuan"]["today"]] == ["t5-0", "t5-1", "t5-2"]
    assert "kemarin" not in block  # the least recent line goes first
    small = M.render_block(prefs[:1], [], game="stardew", day=5, yesterday="kemarin aku mancing seharian")
    assert small["kemarin"] == "kemarin aku mancing seharian [kemarin = Y1 spring 3: passed]" and "note" not in small
    assert M.render_block([], [], game="stardew", day=5) is None


def test_clip_why_keeps_her_words_one_line():
    assert M.clip_why("  aku mau\nke kota  ") == ("aku mau ke kota", False)
    assert M.clip_why("") == (None, False) and M.clip_why(None) == (None, False)
    why, clipped = M.clip_why("aku " + "pengen banget lihat isi tambang itu " * 6)
    assert clipped and len(why) <= M.WHY_MAX_CHARS and why.startswith("aku pengen")


def test_now_json_roundtrip_stale_and_broken(tmp_path):
    row = M.write_now(tmp_path, game="g", why="aku mau lihat tambang", goal_ref="t5-1",
                      goal_today=["aku mau nanem"], game_time={"time": 1010})
    assert row["schema"] == M.NOW_SCHEMA
    line = M.intent_block(tmp_path)
    assert line.startswith("\n[NIATKU] ") and "aku mau lihat tambang" in line and "aku mau nanem" in line
    assert M.intent_block(tmp_path, now=time.time() + M.NOW_MAX_AGE_SEC + 5) == ""
    assert M.intent_block(tmp_path / "missing") == ""
    (tmp_path / M.NOW_FILE).write_text("{broken", encoding="utf-8")
    assert M.intent_block(tmp_path) == ""
    M.write_now(tmp_path, game="g", why=None, goal_ref=None, goal_today=[])
    assert M.intent_block(tmp_path) == ""  # no why and no goal: nothing is invented


def test_intent_line_is_game_agnostic():
    line = M.intent_line(dict(why="aku mau ke kota", goal_today=[]))
    assert "stardew" not in line.lower() and "farm" not in line.lower()


def test_steering_lint_words_and_phrases():
    assert M.steering_hits("Utamakan kebun; Prioritas") == ["utamakan", "prioritas"]
    assert M.steering_hits("kamu harus tidur") == ["harus"]
    assert M.steering_hits("Kegagalan berulang tidak mengharuskan pulang") == []
    assert M.steering_hits("you MUST choose") == ["must"]


def test_text_reason_rejects_junk():
    assert M.text_reason("aku mau ke kota", 100) == ""
    for bad in ("", "x" * 101, "lihat https://x.com", "halo @orang", "[log] aku", "{\"a\":1}"):
        assert M.text_reason(bad, 100)


def test_journey_rows_yesterday_and_diary(tmp_path):
    M.save_journey_row(tmp_path, "stardew", dict(id="Y1 spring 5", day=5, date="Y1 spring 5",
        reflected_at="2026-10-28T21:00:00+07:00", reflection=dict(today="hari ini aku nebang pohon", fun=[
            dict(text="nebang pohon gede", ref="r2")])))
    assert M.yesterday_line(tmp_path, "stardew", 6) == "hari ini aku nebang pohon"
    assert M.yesterday_line(tmp_path, "stardew", 9) is None
    beats = M.diary_beats(tmp_path, wall_date="2026-10-28")
    assert beats == ["[stardew Y1 spring 5] hari ini aku nebang pohon Seru: nebang pohon gede."]
    assert M.diary_beats(tmp_path, wall_date="2026-10-29") == []
    assert M.diary_beats(tmp_path / "nope", wall_date="2026-10-28") == []


def test_module_has_no_game_import():
    source = open(M.__file__, encoding="utf-8").read()
    assert "arti_stardew" not in source and "import arti_bridge" not in source
    assert os.path.basename(M.__file__) == "arti_mind.py"


def test_owner_default_prefs_are_valid_rows():
    assert all(M.valid_pref(r) for r in M.OWNER_PREFS)



def test_open_goals_legacy_caller_without_horizons_keeps_main_contract():
    """Default callers see only today/week/long, file order and no age TTL, as on main.

    Loading a valid stored season row must not add a key, expose it to a legacy
    voice caller, or mutate any authored row. The opt-in extension is explicit.
    """
    from copy import deepcopy
    rows = [goal('w-old', 'week', 'aku ingin melihat pantai', 1),
            goal('t-old', 'today', 'aku ingin mencari batu', 1, 1),
            goal('s-now', 'season', 'aku ingin mencoba hal baru', 29),
            goal('l-old', 'long', 'aku ingin mengenal lembah', 1),
            goal('t-next', 'today', 'aku ingin membaca surat', 30, 30),
            goal('t-closed', 'today', 'aku selesai', 1, 1, status='done'),
            goal('l-future', 'long', 'aku ingin melihat besok', 31)]
    before = deepcopy(rows)
    expected = {'today': [rows[1], rows[4]], 'week': [rows[0]], 'long': [rows[3]]}
    assert M.HORIZONS == ('today', 'week', 'long')
    assert M.open_goals(rows, 30) == expected  # no horizons argument
    assert M.open_goals(rows, 30, horizons=M.SEASON_HORIZONS)['season'] == [rows[2]]
    assert M.render_block([], rows, game='stardew', day=30) == M.render_block(
        [], [r for r in rows if r['horizon'] != 'season'], game='stardew', day=30)
    assert rows == before
