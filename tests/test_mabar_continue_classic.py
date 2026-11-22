"""mabar --continue (a carried-on co-op farm, facts from the save) and --classic (the 11-10 voice lane)."""
import hashlib
import json
import pytest
from pathlib import Path

from scripts.livepath import mabar, mabar_memory

@pytest.fixture(autouse=True)
def isolated_farm_fact_data(tmp_path, monkeypatch):
    monkeypatch.setattr(mabar, "MIND", tmp_path / "mind")


INFO = """<?xml version="1.0"?><Farmer><name>Host</name><farmName>kebun</farmName><dayOfMonthForSaveGame>7
</dayOfMonthForSaveGame><seasonForSaveGame>1</seasonForSaveGame><yearForSaveGame>2</yearForSaveGame></Farmer>"""


def save(tmp_path, name="kebun_1"):
    d = tmp_path / "Saves" / name
    d.mkdir(parents=True)
    (d / "SaveGameInfo").write_text(INFO, encoding="utf-8")
    return d


def test_saved_farm_reads_the_save_and_never_writes_it(tmp_path):
    d = save(tmp_path)
    before = hashlib.sha256((d / "SaveGameInfo").read_bytes()).hexdigest()
    got = mabar.saved_farm(d)
    assert got == dict(save="kebun_1", farm="kebun", host="Host", season="summer", day=7, year=2)
    assert hashlib.sha256((d / "SaveGameInfo").read_bytes()).hexdigest() == before
    assert sorted(p.name for p in d.iterdir()) == ["SaveGameInfo"]


def test_evening_number_counts_earlier_runs_on_this_farm(tmp_path):
    runs = tmp_path / "runs"
    for name, farm in (("a", "kebun"), ("b", "lain"), ("c", "kebun")):
        (runs / name).mkdir(parents=True)
        (runs / name / "self_facts.json").write_text(json.dumps(dict(lines=["Farm ini bernama %s Farm." % farm])))
    (runs / "tonight").mkdir()
    assert mabar.evening_number("kebun", runs, exclude=runs / "tonight") == 3
    assert mabar.evening_number("baru", runs) == 1


def test_continue_facts_replace_the_new_farm_facts(tmp_path):
    continued = dict(save="kebun_1", farm="kebun", host="Host", season="summer", day=7, year=2, evening=3)
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1, live=dict(streamer_name="Bohan"),
                                    continued=continued)
    facts = " ".join(cfg["live7_session_facts"])
    assert "lanjutan farm kebun Farm milik Bohan" in facts and "ke-3" in facts and "summer hari 7 tahun 2" in facts
    assert "farm baru" not in facts and "pertama kalinya" not in facts
    fresh = " ".join(mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1)["live7_session_facts"])
    assert "farm baru" in fresh  # without --continue nothing changes


def test_classic_turns_the_desk_lane_off_and_default_keeps_it_on(tmp_path):
    off = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1)
    assert not off["attention_desk_enabled"] and not off["live7_own_play_enabled"]
    assert not off["voice_queue_preserve_humans"]
    classic = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1, classic=True)
    assert classic["attention_desk_enabled"] == off["attention_desk_enabled"]
    assert classic["live7_own_play_enabled"] == off["live7_own_play_enabled"]
    assert classic["voice_queue_preserve_humans"] == off["voice_queue_preserve_humans"]
    assert classic["voice_queue_priority_overrides"] == off["voice_queue_priority_overrides"]  # game-first stays
    on = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1, desk=True,
                                   live=dict(cursor_trigger_types=["yt_chat", "mic", "ptt"]))
    assert on["attention_desk_enabled"] and on["live7_own_play_enabled"] and on["voice_queue_preserve_humans"]
    assert {"cut_in", "focus", "scene_ended", "yt_chat", "mic"} <= set(on["cursor_trigger_types"])
    from scripts.livepath import bridge as lp_bridge
    path = tmp_path / "c.json"
    path.write_text(json.dumps(off), encoding="utf-8")
    lp_bridge.load_config(path)


def test_cli_flags_parse():
    import argparse
    seen = {}

    def fake(a):
        seen.update(vars(a))
    original = mabar.STEPS["status"]
    mabar.STEPS["status"] = fake
    try:
        mabar.main(["status", "--run", "x-run-tmp", "--continue", "kebun_1", "--classic", "--desk"])
    finally:
        mabar.STEPS["status"] = original
        Path("x-run-tmp").rmdir()
    assert seen["continue_save"] == "kebun_1" and seen["classic"] is True and seen["desk"] is True


def test_memory_note_says_continued(tmp_path):
    run, mind = tmp_path / "run", tmp_path / "mind"
    run.mkdir()
    (run / "MODE.json").write_text(json.dumps(dict(mode="mabar", utc="2026-11-11T12:30:00Z")))
    (run / "continued.json").write_text(json.dumps(dict(farm="kebun", evening=2)))
    mabar_memory.build(run, mind)
    note = (run / "vault-export/2026-11-11-mabar.md").read_text(encoding="utf-8")
    assert "lanjutan farm kebun Farm (malam main bareng ke-2)" in note and "farm baru" not in note
    rows = [json.loads(l) for l in (run / "diary-export.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["text"].startswith("Aku lanjut main Stardew Valley bareng Bohan di kebun Farm")


def test_recap_uses_only_this_farms_rows(tmp_path):
    import hashlib as h
    inbox, mind = tmp_path / "inbox", tmp_path / "mind"
    inbox.mkdir()
    (mind / "stardew").mkdir(parents=True)
    (inbox / "occurrences.8271.77.json").write_text("{}")
    mine, other = h.sha256(b"8271:77").hexdigest(), h.sha256(b"999:1").hexdigest()
    rows = [dict(kind="npc_met", text="Robin: met", scope_hash=mine),
            dict(kind="item_obtained", text="Parsnip Seeds: Plant", scope_hash=mine),
            dict(kind="location_entered", text="x", place_name="Pelican Town", scope_hash=mine),
            dict(kind="location_entered", text="x", place_name="stagedfarm Farm", scope_hash=other)]
    (mind / "stardew/discoveries.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    line = mabar.previous_recap("kebun_8271", mind=mind, inbox_dir=inbox)
    assert "kenal Robin" in line and "Parsnip Seeds" in line and "Pelican Town" in line
    assert "stagedfarm" not in line and len(line) <= 200
    assert mabar.previous_recap("lain_1", mind=mind, inbox_dir=inbox) == ""


def test_memory_down_line_is_never_the_one_cut(tmp_path):
    continued = dict(farm="kebun", season="summer", day=7, year=2, evening=3, recap="Yang tercatat: kenal Robin.")
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1, continued=continued, vault_why="LM Studio mati")
    facts = cfg["live7_session_facts"]
    assert any("LM Studio mati" in f for f in facts[:4]) and facts[-1].startswith("Yang tercatat")


def test_memory_note_keeps_only_this_farms_discoveries(tmp_path):
    run, mind = tmp_path / "run", tmp_path / "mind"
    run.mkdir()
    (mind / "stardew").mkdir(parents=True)
    (run / "MODE.json").write_text(json.dumps(dict(mode="mabar", utc="2026-11-11T12:30:00Z")))
    rows = [dict(kind="location_entered", text="x", place_name="Pelican Town", scope_hash="mine", day_label="Y1 spring 3"),
            dict(kind="location_entered", text="x", place_name="stagedfarm Farm", scope_hash="other", day_label="Y1 spring 3")]
    (mind / "stardew/discoveries.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    mabar_memory.build(run, mind, scope_hashes={"mine"})
    note = (run / "vault-export/2026-11-11-mabar.md").read_text(encoding="utf-8")
    assert "Pelican Town" in note and "stagedfarm" not in note
    rows = [json.loads(l) for l in (run / "discoveries.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == [dict(kind="location_entered", text="x", place_name="Pelican Town", day_label="Y1 spring 3",
                         scope_hash="mine")]  # run-local, farm-scoped feed for ARTI-DiaryAuto


def _ended_run(root, name, utc, events=()):
    run = root / name
    (run / "vault-export").mkdir(parents=True)
    (run / "MODE.json").write_text(json.dumps(dict(mode="mabar", utc=utc)))
    (run / "mabar.jsonl").write_text("".join(json.dumps(dict(event=e)) + "\n" for e in events), encoding="utf-8")
    return run


def test_export_note_only_writes_no_diary_rows(tmp_path):
    run = _ended_run(tmp_path, "mabar-a", "2026-11-11T12:00:00Z")
    (run / "vault-export/2026-11-11-mabar.md").write_text("note", encoding="utf-8")
    (run / "diary-export.jsonl").write_text(json.dumps(dict(id="x", status="draft")) + "\n", encoding="utf-8")
    stream = tmp_path / "stream"
    (stream / "vault/sessions").mkdir(parents=True)
    got = mabar_memory.export_to_live(run, stream, diary=False)
    assert got["note"] == "created" and not (stream / "data/diary/arti_diary.jsonl").exists()


def test_catch_up_builds_missing_notes_only_for_new_unexported_runs(tmp_path, monkeypatch):
    runs, stream = tmp_path / "runs", tmp_path / "stream"
    (stream / "vault/sessions").mkdir(parents=True)
    crashed = _ended_run(runs, "mabar-crashed", "2026-11-11T12:00:00Z", ["voice_start"])
    _ended_run(runs, "mabar-done", "2026-11-11T13:00:00Z", ["auto_exported"])
    _ended_run(runs, "mabar-old", "2026-11-10T12:00:00Z", ["voice_start"])
    _ended_run(runs, "smokeA-x", "2026-11-11T12:00:00Z", ["voice_start"])
    tonight = _ended_run(runs, "mabar-tonight", "2026-11-11T14:00:00Z")
    built = []

    def fake_remember(a, current=True):
        assert current is False  # an old run never uses tonight's occurrence ring
        built.append(Path(a.run).name)
        (Path(a.run) / "vault-export" / (Path(a.run).name + ".md")).write_text("note", encoding="utf-8")

    monkeypatch.setattr(mabar, "remember", fake_remember)
    assert mabar.catch_up_notes(tonight, runs, stream) == ["mabar-crashed"] and built == ["mabar-crashed"]
    assert (stream / "vault/sessions/mabar-crashed.md").is_file()
    assert mabar.catch_up_notes(tonight, runs, stream) == []  # logged auto_exported: never twice


def test_farm_data_matches_save_folder_is_date_ordered_and_survives_caps(tmp_path, monkeypatch, capsys):
    from arti_live7_status_facts import StatusFactsRelay
    from arti_runtime.coop_session import CoopSession
    monkeypatch.setattr(mabar, "MIND", tmp_path / "mind")
    folder = mabar.MIND / "stardew"
    folder.mkdir(parents=True)
    rows = [dict(farm="synthetic_123", scope_hash="test", date=f"2026-11-{i:02d}",
                 source="synthetic", text=f"Synthetic farm fact {i}.") for i in range(7, 0, -1)]
    rows += [dict(farm="different_456", scope_hash="test", date="2026-11-01", source="synthetic", text="Other synthetic fact.")]
    (folder / "farm_facts.json").write_text(json.dumps(dict(schema="arti.farm_facts/1", facts=rows)), encoding="utf-8")
    continued = dict(save="synthetic_123", farm="display name differs", season="summer", day=7,
                     year=2, evening=3, recap="Synthetic recap.")
    facts = mabar.session_facts("Tester", continued)
    expected = [f"Synthetic farm fact {i}." for i in range(1, 8)]
    assert facts[3:-1] == expected and facts[-1] == continued["recap"]
    assert "Other synthetic fact." not in facts
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=False, live={}, continued=continued, vault_why="test")
    assert cfg["live7_session_facts_limit"] == 11
    relay = StatusFactsRelay(cfg, None, clock=lambda: 0.0)
    snapshot = relay.status_block()
    assert all(text in snapshot for text in expected)
    assert all(text in CoopSession(cfg).facts for text in expected)
    output = capsys.readouterr().out
    assert all(text not in output for text in expected)


def test_farm_data_missing_malformed_or_no_match_is_optional(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mabar, "MIND", tmp_path)
    continued = dict(save="synthetic_123")
    assert mabar.farm_facts(continued) == []
    assert capsys.readouterr().out.count("[FarmFacts]") == 1
    (tmp_path / "stardew").mkdir()
    path = tmp_path / "stardew/farm_facts.json"
    for payload in ("bad json", "[]", '{"schema":"arti.farm_facts/1","facts":[{}]}',
                    '{"schema":"arti.farm_facts/1","facts":[]}'):
        path.write_text(payload, encoding="utf-8")
        assert mabar.farm_facts(continued) == []
        assert capsys.readouterr().out.count("[FarmFacts]") == 1
