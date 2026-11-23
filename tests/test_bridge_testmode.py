"""Real-path TEXT-ONLY bridge test mode (owner 2026-10-28). Offline: the provider is a fake."""
import ast
import asyncio
import inspect
import json
from pathlib import Path
import time

import pytest

import arti_bridge as B
import arti_bridge_testmode as T
import arti_mind as M
from arti_live7_status_facts import StatusText

ROOT = Path(__file__).resolve().parents[1]


def _config_literals():
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    config = next(node.value for node in ast.parse(src).body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in node.targets))
    return {k.value: v for k, v in zip(config.keys, config.values) if isinstance(k, ast.Constant)}


def test_test_mode_defaults_off_in_source():
    literals = _config_literals()
    assert literals["bridge_test_mode"].value is False
    assert literals["bridge_test_dir"].value == ""


def test_test_mode_entry_runs_before_the_live_main_block():
    tree = ast.parse((ROOT / "arti_bridge.py").read_text(encoding="utf-8"))
    mains = [n for n in tree.body if isinstance(n, ast.If) and "__name__" in ast.unparse(n.test)]
    assert len(mains) == 1            # one __main__ block (guard tests walk the first one)
    first = mains[0].body[0]
    assert isinstance(first, ast.If) and "bridge_test_mode" in ast.unparse(first.test)
    assert "arti_bridge_testmode.main" in ast.unparse(first) and "sys.exit" in ast.unparse(first)
    assert "startup_wizard" not in ast.unparse(first) and "startup_wizard" in ast.unparse(mains[0])
    wizard = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef) and node.name == "startup_wizard")
    delegation = wizard.body[-1]
    assert isinstance(delegation, ast.Return) and isinstance(delegation.value, ast.Call)
    assert ast.unparse(delegation.value.func) == "_startup_wizard.startup_wizard"


def test_env_test_config_replaces_config_local(tmp_path, monkeypatch):
    cfg_path = tmp_path / "test-config.json"
    cfg_path.write_text(json.dumps({"bridge_test_dir": str(tmp_path), "codex_model": "x-model"}), encoding="utf-8")
    monkeypatch.setattr(B, "CONFIG", dict(B.CONFIG))
    monkeypatch.setenv("ARTI_BRIDGE_TEST_CONFIG", str(cfg_path))
    real_isfile = B.os.path.isfile

    def isfile(path):
        assert not str(path).endswith("config_local.json"), "test mode must never read config_local.json"
        return real_isfile(path)

    monkeypatch.setattr(B.os.path, "isfile", isfile)
    B._load_local_config()
    assert B.CONFIG["bridge_test_mode"] is True
    assert B.CONFIG["codex_model"] == "x-model"
    assert B.CONFIG["bridge_test_config_path"] == str(cfg_path)


def test_unreadable_test_config_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "CONFIG", dict(B.CONFIG, bridge_test_dir=str(tmp_path)))
    monkeypatch.setenv("ARTI_BRIDGE_TEST_CONFIG", str(tmp_path / "missing.json"))
    B._load_local_config()
    assert B.CONFIG["bridge_test_mode"] is True and B.CONFIG["bridge_test_config_error"]
    assert T.main(B) == T.EXIT_REFUSED
    rows = [json.loads(line) for line in (tmp_path / T.TRANSCRIPT_NAME).read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["ev"] == "test_refused" and rows[-1]["reason"].startswith("test_config_unreadable")


def test_text_only_forces_side_effects_off_and_bounds_provider():
    cfg = {"youtube_chat_enabled": True, "subtitle_enabled": True, "vision_enabled": True,
           "vault_rag_enabled": True, "agy_agent_enabled": True, "groq_api_key": "k"}
    changed = T.apply_text_only(cfg)
    assert {"youtube_chat_enabled", "subtitle_enabled", "vision_enabled", "vault_rag_enabled",
            "agy_agent_enabled", "groq_api_key"} <= set(changed)
    assert all(cfg[k] is False for k in ("youtube_chat_enabled", "subtitle_enabled", "health_check_on_startup",
                                         "text_input_enabled", "live7_help_enabled", "stardew_runtime_enabled"))
    assert cfg["groq_api_key"] == "" and cfg["openrouter_live_fallback_enabled"] is False
    assert cfg["cursor_last_resort_incharacter"] is False


def test_refuses_when_turns_do_not_route_to_the_luna_chain(tmp_path):
    cfg = dict(B.CONFIG, bridge_test_dir=str(tmp_path), codex_agent_enabled=False, codex_primary_voice=False)
    T.apply_text_only(cfg)
    assert T.refusal_reason(B, cfg).startswith("provider_route_not_text_testable")
    cfg.update(codex_agent_enabled=True, codex_primary_voice=True, cursor_trigger_types=["mic", "game"])
    assert T.refusal_reason(B, cfg) is None
    assert T.refusal_reason(B, dict(cfg, bridge_test_dir="relative")) == "bridge_test_dir_missing_or_relative"


def test_turn_blocks_and_source_lines_match_the_live_handler():
    handler = inspect.getsource(B._handle_voice_trigger)
    expected = "dynamic_system_prompt\n" + "".join(
        f"        + {fn}({'trigger.viewer_name' if fn == 'viewer_block_for' else ''})\n"
        + ("        + _alert_yield_fact_block()\n" if fn == "_live7_status_block_now" else "")
        for _, fn in T.TURN_BLOCKS)
    assert expected in handler, "test mode must assemble the same per-turn blocks, in the same order"
    for line in T.SOURCE_LINES.values():
        assert line in handler
    assert T.SOURCE_TAIL.strip().split(". ")[0] in " ".join(handler.split())
    main_src = inspect.getsource(B.main_loop)
    assert "assemble_startup_prompt(" in main_src
    assert "assemble_startup_prompt(" in inspect.getsource(T.TestModeBridge.build_persona)
    # One shared tail (emotion guidance, then live play rules) for the live startup and test mode.
    assert "persona_prompt_tail(" in main_src
    assert "persona_prompt_tail(" in inspect.getsource(T.TestModeBridge.build_persona)
    assert "emotion_prompt_for_system" in inspect.getsource(B.persona_prompt_tail)


def test_probe_scheduler_fires_once_per_slot_per_game_day():
    clock = [0.0]
    s = T.ProbeScheduler(["p1", "p2"], [900, 1300], min_gap_sec=10, clock=lambda: clock[0])
    row = lambda date, t: {"fresh": True, "game_time": {"date": date, "time": t}}
    assert s.due(row("Spring 1", 800)) is None
    text, info = s.due(row("Spring 1", 910))
    assert text == "p1" and info["slot"] == 900
    clock[0] = 5
    assert s.due(row("Spring 1", 1310)) is None          # min gap
    clock[0] = 20
    text, info = s.due(row("Spring 1", 1310))
    assert text == "p2" and info["slot"] == 1300
    clock[0] = 40
    assert s.due(row("Spring 1", 1500)) is None          # both slots used today
    text, info = s.due(row("Spring 2", 1400))              # a new day, late: one probe, earlier slot skipped
    assert info == {"slot": 1300, "date": "Spring 2", "game_clock": 1400, "skipped_slots": [900], "now_stale": False}
    clock[0] = 80
    assert s.due({"fresh": False, "game_time": {"date": "Spring 3", "time": 1000}}) is None


def test_probe_scheduler_start_probe_for_a_dry_run():
    clock = [0.0]
    s = T.ProbeScheduler(["lagi mau ngapain?"], [900], on_start=True, start_delay_sec=2, clock=lambda: clock[0])
    assert s.due({}) is None
    clock[0] = 2.5
    assert s.due({}) == ("lagi mau ngapain?", {"slot": "start"})
    clock[0] = 100
    assert s.due({}) is None


class _Forbidden:
    def __init__(self, *a, **k):
        raise AssertionError("text-only test mode touched a voice/VTS/mic path")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Bridge CONFIG in test mode with every voice/VTS/mic/chat entry point booby-trapped."""
    test_dir = tmp_path / "bridge"
    mind = tmp_path / "mind"
    cfg = dict(B.CONFIG, bridge_test_mode=True, bridge_test_dir=str(test_dir), arti_mind_dir=str(mind),
               arti_state_dir=str(tmp_path / "state"), arti_state_enabled=False, live7_help_dir=str(tmp_path / "run"),
               codex_agent_enabled=True, codex_primary_voice=True, cursor_trigger_types=["mic", "game"],
               live7_status_facts_enabled=True, live7_moments_enabled=False,
               bridge_test_probe_on_start=True, bridge_test_probe_start_delay_sec=0.1,
               bridge_test_duration_sec=4, bridge_test_max_turns=3, transcript_dir=str(tmp_path / "tr"),
               telemetry_dir=str(tmp_path / "tel"))
    monkeypatch.setattr(B, "CONFIG", cfg)
    monkeypatch.setattr(B, "_SCRIPT_DIR", str(tmp_path))  # never load a real soul
    # Test mode mutates bridge globals for its (single-use) process; restore them for the rest of the suite.
    for name in T.SideEffectGuard.NAMES + ("main_event_loop", "_brain_busy", "_last_arti_reply_ts", "_live7_help_relay"):
        monkeypatch.setattr(B, name, getattr(B, name))
    monkeypatch.setitem(B._discord_stories, "block", B._discord_stories["block"])
    for name in ("VTSController", "TTSEngine"):
        monkeypatch.setattr(B, name, _Forbidden)
    for name in ("voice_listener_worker", "youtube_chat_worker", "init_global_hotkey", "init_vision_hotkey",
                 "start_text_input_worker", "start_desktop_audio_worker", "start_screen_watcher_worker",
                 "start_scouter", "start_idle_animation", "start_subtitle_server", "_speak_reply_chunks",
                 "refresh_vision_for_turn", "transcribe_audio"):
        monkeypatch.setattr(B, name, _Forbidden)
    monkeypatch.setattr(B.sd, "InputStream", _Forbidden)
    monkeypatch.setattr(B.sd, "play", _Forbidden)
    monkeypatch.setattr(B.sd, "OutputStream", _Forbidden)
    calls = []

    async def fake_reply(llm_system, prompt_content, user_speech, config=None, trigger_type=""):
        calls.append(dict(system=llm_system, prompt=prompt_content, speech=user_speech, trigger_type=trigger_type))
        return ("Aku lagi mau ke tambang, penasaran sama batunya. [MEMORY_SAVE: Bohan nanya niat] [MC: join]",
                [], "codex")

    monkeypatch.setattr(B, "_cursor_reply_with_fallback", fake_reply)
    monkeypatch.setattr(B, "_shutdown_codex_clients", lambda *a, **k: None)
    monkeypatch.setattr(B.arti_codex_agent, "prewarm", lambda cfg: True)
    monkeypatch.setattr(B.arti_codex_agent, "is_warm", lambda: True)
    with B.history_lock:
        saved = list(B.stream_history)
    yield dict(cfg=cfg, dir=test_dir, mind=mind, calls=calls)
    with B.history_lock:
        B.stream_history.clear()
        B.stream_history.extend(saved)


def _rows(test_dir):
    return [json.loads(line) for line in (test_dir / T.TRANSCRIPT_NAME).read_text(encoding="utf-8").splitlines()]


def test_dry_run_answers_a_probe_with_niatku_in_text_only(isolated):
    M.write_now(isolated["mind"], game="stardew", why="aku mau lihat tambang", goal_ref=None,
                goal_today=["cari batu"], game_time={"date": "Spring 2", "time": 1010, "place": "Mine"})
    assert T.main(B) == T.EXIT_OK
    rows = _rows(isolated["dir"])
    evs = [r["ev"] for r in rows]
    assert evs[0] == "test_start" and evs[-1] == "test_end" and "turn" in evs
    start = rows[0]
    assert start["persona"]["system_prompt_chars"] > 1000 and len(start["persona"]["system_prompt_sha256"]) == 64
    assert "[IDENTITAS]" not in json.dumps(start)     # the private base prompt is never written
    turn = next(r for r in rows if r["ev"] == "turn")
    assert turn["source"] == "owner_probe" and turn["trigger_type"] == "mic"
    assert turn["now"]["why"] == "aku mau lihat tambang" and turn["now"]["fresh"] is True
    assert "aku mau lihat tambang" in turn["context"]["niatku"]
    assert turn["reply"] and "[MEMORY_SAVE" not in turn["reply"] and "[MC:" not in turn["reply"]
    assert turn["memory_saves_not_written"] == ["Bohan nanya niat"]
    assert {"mc": "join"} in turn["tags_not_executed"]
    assert turn["provider_source"] == "codex"
    call = isolated["calls"][0]
    assert "[NIATKU]" in call["system"] and "[SUMBER GILIRAN INI] Streamer (Bohan)" in call["system"]
    assert len(isolated["calls"]) == 1
    assert rows[-1]["reason"] == "duration" and rows[-1]["llm_turns"] == 1
    assert (isolated["dir"] / T.READY_NAME).is_file()


def test_fix14a_each_probe_passes_current_intent_to_provider_after_trimming(isolated):
    mode = T.TestModeBridge(B, isolated["cfg"], T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME))
    asyncio.run(mode.build_persona())
    isolated["cfg"]["llm_system_prompt_max_chars"] = 80
    for slot, question in zip((900, 1300, 1800), T.DEFAULT_PROBES):
        why = "my current decision at " + str(slot)
        M.write_now(isolated["mind"], game="stardew", why=why, goal_ref=None, goal_today=[],
                    game_time=dict(date="Spring 5", time=slot, place="Cabin"))
        mode.inject_probe(question, {"slot": slot})
        asyncio.run(mode.handle(mode.queue.get_nowait()))
        assert why in isolated["calls"][-1]["system"]
        assert "[NIATKU]" in isolated["calls"][-1]["system"]
    assert len(isolated["calls"]) == 3


def test_stop_file_ends_cleanly_and_nothing_queued_is_dropped_silently(isolated):
    isolated["cfg"].update(bridge_test_probe_on_start=False, bridge_test_duration_sec=30)
    isolated["dir"].mkdir(parents=True)
    (isolated["dir"] / T.STOP_NAME).write_text("x")
    assert T.main(B) == T.EXIT_OK
    rows = _rows(isolated["dir"])
    assert rows[-1]["ev"] == "test_end" and rows[-1]["reason"] == "stop_file" and rows[-1]["turns"] == 0
    mode = T.TestModeBridge(B, isolated["cfg"], T.Transcript(isolated["dir"] / "x.jsonl"))
    mode.inject_probe("lagi mau ngapain?", {"slot": 900})
    assert mode.drain_unanswered() == 1
    assert _rows_file(isolated["dir"] / "x.jsonl")[0]["ev"] == "turn_unanswered_at_end"


def _rows_file(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("owner", ["Bohan", "SyntheticOwner"])
def test_text_owner_source_uses_live_configured_identity(isolated, owner):
    isolated["cfg"]["streamer_name"] = owner
    T.apply_text_only(isolated["cfg"])
    mode = T.TestModeBridge(B, isolated["cfg"], T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME))
    asyncio.run(mode.build_persona())
    mode.inject_probe("lagi mau ngapain?", {"slot": "start"})
    asyncio.run(mode.handle(mode.queue.get_nowait()))
    assert f"[SUMBER GILIRAN INI] Streamer ({owner}) berbicara lewat mic/chat pemilik." in isolated["calls"][0]["system"]


class _Owner:
    def __init__(self, current):
        self.current, self.voiced = current, []

    def is_current(self, text):
        return self.current

    def mark_voiced(self, text):
        self.voiced.append(str(text))


def _status(text, owner):
    s = StatusText(text)
    s._owner = owner
    return s


def test_game_status_turns_are_answered_acknowledged_or_logged_as_dropped(isolated):
    T.apply_text_only(isolated["cfg"])
    transcript = T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME)
    mode = T.TestModeBridge(B, isolated["cfg"], transcript)
    asyncio.run(mode.build_persona())
    live, stale = _Owner(True), _Owner(False)
    mode.enqueue(_status("[Stardew status] Arti baru masuk tambang.", live), trigger_type="game")
    mode.enqueue(_status("[Stardew status] basi", stale), trigger_type="game")
    asyncio.run(mode.handle(mode.queue.get_nowait()))
    asyncio.run(mode.handle(mode.queue.get_nowait()))
    rows = _rows(isolated["dir"])
    turn = next(r for r in rows if r["ev"] == "turn")
    assert turn["source"] == "game" and turn["trigger_type"] == "game" and turn["reply"]
    assert live.voiced == ["[Stardew status] Arti baru masuk tambang."]
    assert any(r["ev"] == "game_trigger_dropped" for r in rows) and stale.voiced == []
    assert "[SUMBER GILIRAN INI] Sumber giliran ini adalah kejadian game" in isolated["calls"][0]["system"]


def test_turn_budget_logs_skips_instead_of_calling_the_provider(isolated):
    isolated["cfg"]["bridge_test_max_turns"] = 0
    mode = T.TestModeBridge(B, isolated["cfg"], T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME))
    mode.inject_probe("lagi mau ngapain?", {"slot": "start"})
    asyncio.run(mode.handle(mode.queue.get_nowait()))
    assert isolated["calls"] == []
    assert _rows(isolated["dir"])[-1]["ev"] == "turn_skipped"


def test_side_effect_guard_records_instead_of_writing(isolated):
    transcript = T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME)
    guard = T.SideEffectGuard(B, transcript)
    B.save_long_term_memory("fakta")
    B._cermin_ke_chat_game("halo")
    assert guard.hits == ["save_long_term_memory", "_cermin_ke_chat_game"]
    assert [r["name"] for r in _rows(isolated["dir"])] == guard.hits


def test_read_now_row_reports_freshness_without_raising(tmp_path):
    assert T.read_now_row(tmp_path, 900) == {"present": False}
    M.write_now(tmp_path, game="stardew", why="w", goal_ref=None, goal_today=[], decision_ts=time.time() - 1000)
    row = T.read_now_row(tmp_path, 900)
    assert row["present"] and row["fresh"] is False and row["why"] == "w"
    (tmp_path / "now.json").write_text("{broken", encoding="utf-8")
    assert T.read_now_row(tmp_path, 900) == {"present": False}


def test_parent_alive_for_self_and_missing_pid():
    import os
    assert T.parent_alive(None) is True
    assert T.parent_alive(os.getpid()) is True
    assert T.parent_alive(0x7FFFFFF0) is False


def test_voice_provider_is_warmed_before_ready_or_the_test_refuses(isolated, monkeypatch):
    warm = {"calls": 0}
    monkeypatch.setattr(B.arti_codex_agent, "prewarm", lambda cfg: warm.__setitem__("calls", warm["calls"] + 1))
    monkeypatch.setattr(B.arti_codex_agent, "is_warm", lambda: False)
    clock = [0.0]
    sleep = lambda s: clock.__setitem__(0, clock[0] + s)
    assert T.warm_voice_provider(B, isolated["cfg"], timeout_sec=3, clock=lambda: clock[0], sleep=sleep) is False
    assert warm["calls"] == 1
    assert T.warm_voice_provider(B, dict(isolated["cfg"], codex_primary_voice=False), timeout_sec=3) is True
    isolated["cfg"]["bridge_test_warm_timeout_sec"] = 0.5
    assert T.main(B) == T.EXIT_REFUSED
    rows = _rows(isolated["dir"])
    assert rows[-1]["ev"] == "test_refused" and rows[-1]["reason"] == "voice_provider_not_warm"
    assert not (isolated["dir"] / T.READY_NAME).exists() and isolated["calls"] == []


def _probe_turn():
    return T.Turn("abis ini mau ngapain?", "mic", "owner_probe", time.time(), {"slot": 1800})


@pytest.mark.parametrize("fail_all", [False, True])
def test_a13_codex_timeout_probe_is_retried_then_answered_or_explicitly_unavailable(isolated, monkeypatch, fail_all):
    """PM-1 fix10d (a13 bridge.log 00:05:27): Codex 8 s timeout -> blanked Groq -> empty probe reply."""
    calls, warms = [], []

    async def flaky(llm_system, prompt_content, user_speech, config=None, trigger_type=""):
        calls.append(user_speech)
        if fail_all or len(calls) == 1:
            return None, [], "gagal"
        return "Aku mau pulang dulu, Bohan.", [], "codex"

    monkeypatch.setattr(B, "_cursor_reply_with_fallback", flaky)
    monkeypatch.setattr(B.arti_codex_agent, "prewarm", lambda cfg: warms.append(1))
    isolated["dir"].mkdir(parents=True, exist_ok=True)
    tester = T.TestModeBridge(B, isolated["cfg"], T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME))
    tester.dynamic_system_prompt, tester.memories = "x", []
    result = asyncio.run(tester.reply_for(_probe_turn()))
    assert len(calls) == 2 and warms == [1]
    assert result["reply_attempts"] == ["gagal", "gagal" if fail_all else "codex"]
    if fail_all:
        assert result["reply"] == T.UNAVAILABLE_NOTICE and result["provider_source"] == "unavailable:gagal"
    else:
        assert result["reply"] and result["provider_source"] == "codex"
