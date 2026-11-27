import argparse
import json
from pathlib import Path

import pytest

from scripts.livepath import bridge as lp_bridge
from scripts.livepath import mabar


def write_cfg(tmp_path, cfg):
    p = tmp_path / "rehearsal-config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_mabar_config_is_accepted_with_mic_and_vts_on(tmp_path, monkeypatch):
    monkeypatch.setattr(mabar.os, "getpid", lambda: 4242)
    cfg = mabar.mabar_bridge_config(tmp_path / "mabar-run", vts=True, mic=1)
    loaded = lp_bridge.load_config(write_cfg(tmp_path, cfg))
    assert loaded["asr_enabled"] is True and loaded["vts_enabled"] is True and loaded["asr_input_device"] == 1
    assert loaded["attention_desk_enabled"] is False and loaded["live7_own_play_enabled"] is False
    assert loaded["youtube_chat_enabled"] is False and loaded["obs_scene_switch_enabled"] is False
    assert loaded["live7_activity_events_enabled"] is False
    assert loaded["host_story_relay_file"].endswith("host_story.jsonl")
    assert len(loaded["live7_session_facts"]) == len(mabar.SESSION_FACTS)
    assert "groq_api_key" not in loaded  # the key only ever lives in the bridge process env


@pytest.mark.parametrize("change,match", [
    (dict(livepath_mode=None, asr_enabled=True), "peripheral must be off"),      # a rehearsal never opens the mic
    (dict(livepath_mode="party"), "unknown livepath_mode"),
    (dict(vts_enabled="yes"), "explicit bool"),
    (dict(youtube_chat_enabled=True), "needs the live video id"),                 # chat only with the owner's video id
])
def test_mode_rules(tmp_path, change, match):
    cfg = mabar.mabar_bridge_config(tmp_path / "run", vts=True, mic=1)
    cfg.update(change)
    if cfg.get("livepath_mode") is None:
        cfg.pop("livepath_mode")
    with pytest.raises(ValueError, match=match):
        lp_bridge.load_config(write_cfg(tmp_path, cfg))


def test_groq_key_is_read_without_echo(tmp_path, monkeypatch, capsys):
    stream = tmp_path / "stream-folder"
    stream.mkdir()
    (stream / ".env").write_text('OTHER=1\nGROQ_API_KEY="sk-test-value"\n', encoding="utf-8")
    monkeypatch.setattr(mabar, "STREAM", stream)
    assert mabar.groq_key() == "sk-test-value"
    assert "sk-test-value" not in capsys.readouterr().out


def test_choice_moment_is_a_valid_held_free_moment(tmp_path):
    from arti_live7_moments import _decode
    (tmp_path / "runtime").mkdir()
    choice = dict(name="Arti", gender="female", favorite_thing="Kopi", skin=2, hair=5, shirt="(S)1001",
                  pants="(P)0", accessory=1, eye_color=dict(hue=60, saturation=50, value=70),
                  reasons=dict(name="namaku sendiri"), summary="rambut biru, kaos santai")
    mabar.choice_moment(tmp_path, choice)
    files = list((tmp_path / "runtime").glob("ev-*.json"))
    assert len(files) == 1 and not list((tmp_path / "runtime").glob(".ev-*"))
    ts, ttl, kind, text = _decode(files[0].read_bytes())
    assert kind == "other" and "Arti" in text and "rambut biru" in text and "OK paling akhir" in text
    assert len(text) <= 200  # the moment line limit; nothing important past it


def test_find_scope_matches_native_story_record_scope():
    session = dict(facts=dict(native=dict(occurrences=dict(save_id="452", player_id=777, tail=[]))))
    assert mabar.find_scope(session) == "452:777"
    assert mabar.find_scope({}) is None


def test_status_line_carries_session_facts_before_any_game_row():
    from arti_live7_status_facts import StatusFactsRelay
    relay = StatusFactsRelay(dict(live7_session_facts=list(mabar.SESSION_FACTS)), None, clock=lambda: 0.0)
    block = relay.status_block()
    assert "[STARDEW STATUS]" in block and "farm baru" in block and "lewat mic" in block
    assert StatusFactsRelay({}, None, clock=lambda: 0.0).status_block() == ""


def test_owner_reactions_record_owner_speech_with_before_and_after(tmp_path, monkeypatch):
    path = tmp_path / "owner_reactions.jsonl"
    monkeypatch.setattr(lp_bridge, "_reactions", dict(last_reply=None, last_reply_utc=None, pending={}))
    monkeypatch.setattr(lp_bridge, "_game_now", lambda: dict(date="Y1 spring 1", time=630))
    rec = lambda *a: lp_bridge.record_reaction(*a, path=str(path), clock=lambda: 0)
    rec("start", "t1", "moment", "[Stardew] x", None)
    rec("voiced", "t1", "moment", "[Stardew] x", "wah kebunnya luas")
    rec("start", "t2", "mic", "haha kamu lucu", None)
    rec("voiced", "t2", "mic", "haha kamu lucu", "makasih!")
    rec("start", "t3", "mic_pasif", "eh lihat itu", None)
    rec("end", "t3", "mic_pasif", "eh lihat itu", None)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert [r["owner_text"] for r in rows] == ["haha kamu lucu", "eh lihat itu"]
    assert rows[0]["arti_before"] == "wah kebunnya luas" and rows[0]["arti_after"] == "makasih!"
    assert rows[0]["game"] == dict(date="Y1 spring 1", time=630) and rows[0]["mode"] == "mabar"
    assert rows[1]["arti_after"] is None and rows[1]["arti_before"] == "makasih!"


def test_rehearsal_log_stays_content_free(capsys, monkeypatch):
    monkeypatch.delenv("ARTI_MABAR_REACTIONS", raising=False)
    trigger = type("T", (), dict(text="halo arti", trigger_type="mic"))()
    lp_bridge.turn_event("voiced", "t9", trigger, reply="rahasia balasan", source="codex")
    out = capsys.readouterr().out
    assert "rahasia balasan" not in out and "halo arti" not in out


def test_occurrence_scope_from_the_newest_ring_file(tmp_path):
    import os, time
    (tmp_path / "occurrences.111.222.json").write_text("{}")
    time.sleep(0.01)
    newer = tmp_path / "occurrences.100000007.10000000000000004.json"
    newer.write_text("{}")
    os.utime(newer, (time.time() + 5, time.time() + 5))
    assert mabar.occurrence_scope(tmp_path) == "100000007:10000000000000004"
    assert mabar.occurrence_scope(tmp_path / "missing") is None


@pytest.mark.parametrize("route,allowed", [
    (dict(cable_found=True, listen_on=True, name="Headset", iface="ExampleBuds", form_factor=5), True),
    (dict(cable_found=True, listen_on=True, name="Headphones", iface="X", form_factor=3), True),
    (dict(cable_found=True, listen_on=True, name="Speakers", iface="Realtek", form_factor=1), False),
    (dict(cable_found=True, listen_on=False, form_factor=1), True),
    (dict(error="TimeoutExpired"), False),
    (dict(cable_found=False), False),
])
def test_passive_only_when_her_voice_cannot_reach_the_mic(route, allowed):
    ok, why = mabar.passive_allowed(route)
    assert ok is allowed and why


def test_vts_port_reaches_the_bridge_config(tmp_path):
    assert mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1)["vts_api_port"] == 8002
    assert mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1, vts_port=8001)["vts_api_port"] == 8001


@pytest.fixture
def backup_paths(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    saves, backups, run = appdata / "Saves", tmp_path / "backups", tmp_path / "run"
    monkeypatch.setattr(mabar, "APPDATA", appdata)
    monkeypatch.setattr(mabar, "BACKUPS", backups)
    monkeypatch.setattr(mabar, "MOD", tmp_path / "mod")
    monkeypatch.setattr(mabar, "PREP", tmp_path / "prep")
    mabar.MOD.mkdir()
    (mabar.MOD / "ArtiStardewBridge.dll").write_bytes(b"mod")
    for name in ("Test_123", "Other_456"):
        (saves / name).mkdir(parents=True)
        (saves / name / "SaveGameInfo").write_bytes(name.encode())
    (appdata / "startup_preferences").write_bytes(b"prefs")
    return saves, run


@pytest.mark.parametrize("selection", [[], ["--continue", "Test_123"], ["--farm", "Test_123"]])
def test_backup_copies_only_the_selected_save(backup_paths, selection):
    saves, run = backup_paths
    assert mabar.main(["backup", "--run", str(run), *selection]) == 0
    backup = Path(mabar.soak.read(run / "backup.json")["path"])
    manifest = mabar.soak.read(backup / "MANIFEST.json")["sha256"]
    assert (backup / "startup_preferences").read_bytes() == b"prefs"
    assert manifest["startup_preferences"] == mabar.sha(backup / "startup_preferences")
    if selection:
        assert sorted(path.name for path in (backup / "Saves").iterdir()) == ["Test_123"]
        assert manifest["Saves/Test_123"] == mabar.tree_manifest(saves / "Test_123")
    else:
        assert not (backup / "Saves").exists()


def test_prepare_refuses_a_continued_save_that_wasnt_backed_up(backup_paths, monkeypatch):
    _, run = backup_paths
    assert mabar.main(["backup", "--run", str(run)]) == 0
    monkeypatch.setattr(mabar, "processes", lambda: set())
    monkeypatch.setattr(mabar, "free_gib", lambda: 20)
    with pytest.raises(RuntimeError, match="backup --continue"):
        mabar.main(["prepare", "--run", str(run), "--continue", "Test_123"])


@pytest.mark.parametrize("actual_save,warning", [("Test_123", False), ("Other_456", True), (None, False)])
def test_manual_play_logs_the_actual_unbacked_save(backup_paths, monkeypatch, actual_save, warning):
    _, run = backup_paths
    assert mabar.main(["backup", "--run", str(run), "--continue", "Test_123"]) == 0
    mabar.PREP.mkdir()
    mabar.soak.write(run / "trial-config.json", {"stardew_host_controller": "owner"})
    # A new-farm play can see an unbacked save. A prepared owner co-op now verifies its save before enrolment.
    cycle = run / "runtime/live7/c1"

    def fake_tool(run, script, args, name):
        if name == "start-cycle":
            cycle.mkdir(parents=True)
            mabar.soak.write(cycle / "cycle_state.json", {"save_name": actual_save})

    monkeypatch.setattr(mabar, "tool", fake_tool)
    monkeypatch.setattr(mabar, "release_farmhand_focus", lambda run: True)
    assert mabar.play_once(argparse.Namespace(run=run), cycle=cycle) == cycle
    rows = [json.loads(line) for line in (run / "mabar.jsonl").read_text(encoding="utf-8").splitlines()]
    gaps = [row["save"] for row in rows if row["event"] == "host_save_not_backed_up"]
    assert gaps == ([actual_save] if warning else [])


def test_helper_host_logs_an_unbacked_save_and_launches(backup_paths, monkeypatch):
    _, run = backup_paths
    assert mabar.main(["backup", "--run", str(run), "--continue", "Test_123"]) == 0
    monkeypatch.setattr(mabar, "processes", lambda: set())
    monkeypatch.setattr(mabar.rehearse, "read_booking", lambda spec: {})
    monkeypatch.setattr(mabar, "install_host_helper", lambda run, booking, save: {
        "save_folder": save, "farmhand_name": "Arti",
    })
    calls = []
    monkeypatch.setattr(mabar, "tool", lambda run, script, args, name: calls.append(name))
    assert mabar.main(["host", "--run", str(run), "--helper", "--continue", "Other_456"]) == 0
    rows = [json.loads(line) for line in (run / "mabar.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["save"] for row in rows if row["event"] == "host_save_not_backed_up"] == ["Other_456"]
    assert calls == ["launch-host"]


def test_restore_mirrors_the_backup_and_moves_additions_aside(tmp_path):
    import hashlib
    h = lambda b: hashlib.sha256(b).hexdigest()
    src, mod, prep, aside = tmp_path / "bk", tmp_path / "mod", tmp_path / "prep", tmp_path / "aside"
    (src / "farmhand-mod/data").mkdir(parents=True)
    (src / "farmhand-mod/A.dll").write_bytes(b"old")
    (src / "farmhand-mod/config.json").write_bytes(b"{}")
    (src / "startup_preferences").write_bytes(b"prefs")
    (src / "MANIFEST.json").write_text(json.dumps(dict(sha256={
        "farmhand-mod": {"A.dll": h(b"old"), "config.json": h(b"{}")}, "startup_preferences": h(b"prefs"),
        "prep-trial-config.json": None, "prep-approval.json": None})))
    (mod / "data").mkdir(parents=True); prep.mkdir()
    (mod / "A.dll").write_bytes(b"new"); (mod / "config.json").write_bytes(b"{}")
    (mod / "Dep.dll").write_bytes(b"dep"); (mod / "data/d.json").write_bytes(b"x")
    (prep / "trial-config.json").write_bytes(b"t")
    startup = tmp_path / "startup_preferences"; startup.write_bytes(b"changed")
    r = mabar.mirror_from_backup(src, mod, prep, startup, aside)
    assert (mod / "A.dll").read_bytes() == b"old" and not (mod / "Dep.dll").exists() and not (mod / "data/d.json").exists()
    assert (aside / "farmhand-mod/Dep.dll").read_bytes() == b"dep" and (aside / "prep-trial-config.json").exists()
    assert not (prep / "trial-config.json").exists() and startup.read_bytes() == b"prefs"
    assert r["mismatch"] == [] and r["extra"] == [] and r["startup_ok"]


def test_auto_rejoin_off_does_not_rejoin(tmp_path):
    a = argparse.Namespace(run=tmp_path, auto_rejoin=False, auto_rejoin_tries=12, auto_rejoin_minutes=30)
    called = []
    rc = mabar.auto_rejoin_after_host_left(
        a, joinable_fn=lambda: True, sleep_fn=lambda *_: called.append("sleep"),
        clock_fn=lambda: 0, rejoin_fn=lambda *_: called.append("rejoin"),
        play_fn=lambda *_args, **_kw: called.append("play"), print_fn=lambda *_: None)
    assert rc == 0 and called == []
    events = [json.loads(l)["event"] for l in (tmp_path / "mabar.jsonl").read_text().splitlines()] if (tmp_path / "mabar.jsonl").is_file() else []
    assert "auto_rejoin_attempt" not in events


def test_auto_rejoin_on_stops_at_try_bound(tmp_path):
    a = argparse.Namespace(run=tmp_path, auto_rejoin=True, auto_rejoin_tries=3, auto_rejoin_minutes=30)
    lines, sleeps, calls = [], [], []
    clock = {"t": 0.0}

    def tick():
        return clock["t"]

    def sleep(s):
        sleeps.append(s)
        clock["t"] += s

    rc = mabar.auto_rejoin_after_host_left(
        a, joinable_fn=lambda: False, sleep_fn=sleep, clock_fn=tick,
        rejoin_fn=lambda *_: calls.append("rejoin"),
        play_fn=lambda *_args, **_kw: calls.append("play"), print_fn=lines.append)
    assert rc == 0 and calls == [] and len(lines) == 3
    assert all(l.startswith("auto-rejoin ") for l in lines)
    events = [json.loads(l)["event"] for l in (tmp_path / "mabar.jsonl").read_text().splitlines()]
    assert events.count("auto_rejoin_attempt") == 3 and "auto_rejoin_gave_up" in events
    assert "rejoined" not in events


def test_auto_rejoin_on_rejoins_when_joinable_then_stops(tmp_path):
    a = argparse.Namespace(run=tmp_path, auto_rejoin=True, auto_rejoin_tries=5, auto_rejoin_minutes=30)
    lines, calls = [], []
    clock = {"t": 0.0}
    join = {"n": 0}

    def joinable():
        join["n"] += 1
        return join["n"] >= 2

    def play(_a, cycle=None):
        calls.append("play")
        Path(cycle).mkdir(parents=True, exist_ok=True)
        (Path(cycle) / "cycle_state.json").write_text(json.dumps(dict(status="ready")), encoding="utf-8")
        return cycle

    rc = mabar.auto_rejoin_after_host_left(
        a, joinable_fn=joinable, sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        clock_fn=lambda: clock["t"], rejoin_fn=lambda *_: calls.append("rejoin"),
        play_fn=play, print_fn=lines.append)
    assert rc == 0 and calls == ["rejoin", "play"]
    assert len(lines) == 2


def test_auto_rejoin_stops_at_minute_bound(tmp_path):
    a = argparse.Namespace(run=tmp_path, auto_rejoin=True, auto_rejoin_tries=50, auto_rejoin_minutes=1)
    clock = {"t": 0.0}
    lines = []

    def sleep(s):
        clock["t"] += max(s, 60)

    rc = mabar.auto_rejoin_after_host_left(
        a, joinable_fn=lambda: False, sleep_fn=sleep, clock_fn=lambda: clock["t"],
        rejoin_fn=lambda *_: None, play_fn=lambda *_a, **_k: None, print_fn=lines.append)
    assert rc == 0 and 1 <= len(lines) <= 3
    events = [json.loads(l)["event"] for l in (tmp_path / "mabar.jsonl").read_text().splitlines()]
    assert "auto_rejoin_gave_up" in events


def test_status_line_includes_leg_memory_and_voice_counts(tmp_path, monkeypatch):
    import argparse
    run = tmp_path / "mabar-x"
    cycle = run / "runtime/live7/c1"
    cycle.mkdir(parents=True)
    (cycle / "cycle_state.json").write_text(json.dumps(dict(
        status="host_left", started_at=1_000.0,
        days=[dict(index=2, date=dict(year=1, season="spring", day=9), status="active")])), encoding="utf-8")
    (run / "mabar.jsonl").write_text(
        json.dumps(dict(utc="2026-11-12T15:17:00Z", event="cycle_started")) + "\n"
        + json.dumps(dict(utc="2026-11-12T15:43:13Z", event="host_left")) + "\n"
        + json.dumps(dict(utc="2026-11-12T15:50:00Z", event="rejoined")) + "\n", encoding="utf-8")
    (run / "rehearsal-config.json").write_text(json.dumps(dict(
        voice_api_enabled=True, voice_api_session_cap_usd=0.5,
        voice_api_session_state_file=str(run / "voice-api-session.json"))), encoding="utf-8")
    (run / "voice-api-session.json").write_text(json.dumps(dict(
        spent_usd="0.12", reserved_usd="0", uncertain_usd="0", errors=0, fallback=False)), encoding="utf-8")
    tel = run / "telemetry"
    tel.mkdir()
    (tel / "mabar-x.jsonl").write_text(
        json.dumps(dict(source="openai_api")) + "\n" + json.dumps(dict(source="codex")) + "\n"
        + json.dumps(dict(source="cursor")) + "\n", encoding="utf-8")
    monkeypatch.setattr(mabar, "bridge_pids", lambda: [])
    monkeypatch.setattr(mabar, "vts_reachable", lambda: True)
    monkeypatch.setattr(mabar, "free_gib", lambda: 20.0)
    row = mabar.status_payload(argparse.Namespace(run=run), memory_fn=lambda: 41.2, now=1_000.0 + 26 * 60)
    assert row["cycle"] == "host_left"
    assert row["leg_minutes"] == 26
    assert row["game_day"] == dict(year=1, season="spring", day=9)
    assert row["host_left"] == 1 and row["rejoins"] == 1
    assert row["memory_commit_pct"] == 41.2
    assert row["voice_lanes"]["api"] == 1 and row["voice_lanes"]["fallback"] == 2
    assert row["voice_lanes"]["spent_usd"] == "0.12" and row["voice_lanes"]["cap_usd"] == 0.5
    assert "text" not in row and "reply" not in json.dumps(row["voice_lanes"])


def test_play_once_enrolls_human_host_owner(tmp_path, monkeypatch):
    seen = []

    def fake_tool(run, script, args, name):
        seen.append((script, list(map(str, args)), name))
        return tmp_path / name

    monkeypatch.setattr(mabar, "tool", fake_tool)
    monkeypatch.setattr(mabar.soak, "read", lambda p: {})
    monkeypatch.setattr(mabar.soak, "write", lambda *a, **k: None)
    monkeypatch.setattr(mabar, "play_trial_config", lambda *a, **k: {})
    monkeypatch.setattr(mabar, "PREP", tmp_path)
    monkeypatch.setattr(mabar, "log", lambda *a, **k: None)
    monkeypatch.setattr(mabar, "next_cycle", lambda run: tmp_path / "c1")
    (tmp_path / "trial-config.json").write_text("{}", encoding="utf-8")
    mabar.play_once(argparse.Namespace(run=tmp_path, decision_cap_usd=2.0))
    start = next(args for script, args, name in seen if name == "start-cycle")
    assert start == ["start-cycle", "--cycle-dir", str(tmp_path / "c1"), "--host-owner", "human"]


def test_status_omits_voice_lanes_when_api_voice_off(tmp_path, monkeypatch):
    import argparse
    run = tmp_path / "mabar-y"
    (run / "runtime/live7/c1").mkdir(parents=True)
    (run / "runtime/live7/c1" / "cycle_state.json").write_text(json.dumps(dict(status="ready")), encoding="utf-8")
    (run / "rehearsal-config.json").write_text(json.dumps(dict(voice_api_enabled=False)), encoding="utf-8")
    monkeypatch.setattr(mabar, "bridge_pids", lambda: [])
    monkeypatch.setattr(mabar, "vts_reachable", lambda: False)
    monkeypatch.setattr(mabar, "free_gib", lambda: 18.0)
    row = mabar.status_payload(argparse.Namespace(run=run), memory_fn=lambda: 10.0, now=0)
    assert "voice_lanes" not in row
    assert row["host_left"] == 0 and row["rejoins"] == 0
