"""The live profile is code (CT 2026-11-18).

A livestream's play and bridge settings once lived in an overlay outside the repo, and a rehearsal on plain mabar
then ran with stream_game_notes off, so fix134A part A, ADR 233 and the said rows were dead.
These tests pin what a livestream session runs with, and that status/preflight show it.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import session_transcript
from scripts.livepath import mabar

EXPECTED_PLAY = dict(
    stardew_decision_provider_kind="openai_api", stardew_decision_model="gpt-6-luna",
    stardew_decision_effort="medium", stardew_decision_service_tier="priority",
    stream_game_notes=True, live7_fact_line_enabled=True, stardew_page_budget_scale=2,
    stardew_game_text_delivery_enabled=True, stardew_live7_place_time_page=True, stardew_live7_goal_ladder=True,
    stardew_live7_ship_on_the_way=True, stardew_offer_cache_enabled=False, live7_own_play_enabled=True,
    voice_game_memory_enabled=True, stardew_live7_raw_hands=True,
)
EXPECTED_BRIDGE = dict(owner_chat_as_owner=True, stream_game_notes=True, live_voice_fixes_v1=True, live_play_rules_enabled=True,
                       mic_priority_mark=True, voice_game_memory_enabled=True,
                       # V13 (ADR 200): one long voice thread, the lean Codex harness, timeout from measurement
                       codex_voice_lean_harness=True, voice_append_context=True, codex_context_window="auto",
                       voice_context_timeout_sec=20.0, voice_context_rollover_keeps_lines=True,
                       # V14 (ADR 238): her recall says which farm a session note is from
                       vault_farm_labels=True)
EXPECTED_MOD = dict(CharacterCreationEnabled=True, UiPadMenusEnabled=True, GenericWorldInputEnabled=True,
                    FestivalReflexEnabled=True, ReflexFishingEnabled=True)


def test_the_profile_is_pinned():
    assert mabar.LIVE_PLAY == EXPECTED_PLAY
    assert mabar.LIVE_BRIDGE == EXPECTED_BRIDGE
    assert mabar.LIVE_MOD_FLAGS == EXPECTED_MOD


def test_play_config_carries_the_profile_over_any_trial(tmp_path):
    trial = dict(stream_game_notes=False, stardew_decision_effort="low", other="kept")
    out = mabar.play_trial_config(trial, tmp_path, cap=2.0)
    for key, value in EXPECTED_PLAY.items():
        assert out[key] == value, key
    assert out["other"] == "kept" and out["stardew_openai_session_cap_usd"] == 2.0
    assert out["stardew_openai_session_state_file"] == str(Path(tmp_path) / "decision-api-session.json")


def test_bridge_config_carries_the_profile(tmp_path):
    cfg = mabar.mabar_bridge_config(tmp_path / "run", vts=False, mic=1, live={})
    for key, value in EXPECTED_BRIDGE.items():
        assert cfg[key] == value, key
    assert "vault_write_root" not in cfg and "rehearsal_scripted_ptt" not in cfg


def test_rehearsal_scratch_runs_the_same_profile_and_writes_its_note_to_the_scratch_vault(monkeypatch, tmp_path):
    for name in ("MIND", "VAULT_ROOT", "VAULT_INDEX", "REHEARSAL_SCRIPTED_PTT"):
        monkeypatch.setattr(mabar, name, getattr(mabar, name))
    real = tmp_path / "real"
    monkeypatch.setattr(mabar, "MIND", real / "arti-workspace" / "mind")
    monkeypatch.setattr(mabar, "STREAM", real / "stream-folder")
    monkeypatch.setattr(mabar, "VAULT_INDEX", real / "arti-workspace" / "vault-index" / "vault_rag.db")
    root = mabar.apply_rehearsal_scratch(SimpleNamespace(rehearsal_scratch=tmp_path / "scratch"))
    cfg = mabar.mabar_bridge_config(tmp_path / "run", vts=False, mic=1, live={})
    for key, value in EXPECTED_BRIDGE.items():
        assert cfg[key] == value, key
    assert cfg["vault_write_root"] == str(root / "vault") == cfg["vault_rag_root"]


def test_status_and_preflight_print_the_profile_in_force(monkeypatch, tmp_path, capsys):
    import scripts.livepath.mabar_supervise as sup
    monkeypatch.setattr(sup, "gather_status", lambda run, **kw: "STATUS")
    monkeypatch.setattr("scripts.livepath.mabar_solo.status_extras", lambda run: "")
    mabar.status(SimpleNamespace(run=tmp_path))
    printed = capsys.readouterr()
    assert printed.out.strip() == "STATUS"  # stdout stays the one operator line other tools parse
    assert "profile %s play:" % mabar.LIVE_PROFILE_NAME in printed.err and "stream_game_notes=True" in printed.err
    assert "profile %s bridge:" % mabar.LIVE_PROFILE_NAME in printed.err
    assert "voice_game_memory_enabled=True" in printed.err
    from scripts.livepath import bringup
    monkeypatch.setattr(bringup, "run_preflight", lambda a, **kw: 0)
    assert mabar.preflight(SimpleNamespace(vts_port=8002, run=tmp_path)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("OK profile %s play:" % mabar.LIVE_PROFILE_NAME)
    # The live run sheet follows the profile: audio check, the profile lines, rollover.
    assert out[3:6] == ["DO " + step for step in mabar.LIVE_RUN_SHEET]
    sheet = " ".join(out[3:6])
    assert "audio check before the games open" in sheet and "run note" in sheet
    assert "profile lines" in sheet and "rollover" in sheet


def test_session_note_goes_to_vault_write_root_when_set(tmp_path, monkeypatch):
    assert session_transcript._vault_dir({}, "sessions") == session_transcript._ROOT / "vault" / "sessions"
    scratch = tmp_path / "scratch-vault"
    assert session_transcript._vault_dir({"vault_write_root": str(scratch)}, "sessions", "x.md") == \
        scratch / "sessions" / "x.md"
    monkeypatch.setattr(session_transcript, "_session_id", "rehearse-x")
    path = session_transcript.write_vault_session_md({"vault_write_root": str(scratch)}, "ringkasan", {}, [])
    assert path == scratch / "sessions" / "rehearse-x.md" and path.is_file()
    session_transcript.update_vault_index("rehearse-x", {"vault_write_root": str(scratch)})
    assert (scratch / "sessions" / "index.md").is_file()
    assert not (session_transcript._ROOT / "vault" / "sessions" / "rehearse-x.md").exists()


def test_the_game_lane_file_carries_her_game_memory_key(tmp_path, monkeypatch):
    """2026-11-18: the key reached only the bridge config, so the game lane never wrote her experience digest. It must
    arrive in the trial-config.json the game lane reads (play_once), and survive the voice step's own update."""
    import json
    from arti_runtime import experience_digest
    run, prep = tmp_path / "run", tmp_path / "prep"
    run.mkdir()
    prep.mkdir()
    (run / "trial-config.json").write_text(json.dumps({"stream_game_notes": False}), encoding="utf-8")
    monkeypatch.setattr(mabar, "PREP", prep)
    calls = []
    monkeypatch.setattr(mabar, "tool", lambda run, script, args, name: calls.append(script))
    monkeypatch.setattr(mabar, "log", lambda *a, **kw: None)
    mabar.play_once(SimpleNamespace(run=run, decision_cap_usd=2.0), cycle=run / "runtime" / "live7" / "c1")
    for path in (run / "trial-config.json", prep / "trial-config.json"):
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written[experience_digest.SETTING] is True, path
        assert experience_digest.enabled(written)
    assert calls[:1] == ["witness_run.py"]  # the file is written before the game lane starts
    trial = json.loads((run / "trial-config.json").read_text(encoding="utf-8"))
    voice = mabar.mabar_bridge_config(tmp_path / "v", vts=False, mic=1, live={})
    assert mabar.play_voice_trial_config(trial, voice)[experience_digest.SETTING] is True
    assert "voice_game_memory_enabled=True" in mabar.live_profile_lines()[0]  # the printed play line shows it


def test_the_bridge_config_names_the_runs_farm(monkeypatch, tmp_path):
    # ADR 238: a continued farm is named from its save; a new farm from her verified self facts once they exist.
    monkeypatch.setattr(mabar, "MIND", tmp_path / "mind")  # synthetic mind: no farm facts file
    run = tmp_path / "run"
    run.mkdir()
    continued = dict(save="Ladang_111", farm="Ladang", season="spring", day=3, year=1, evening=2, recap="")
    cfg = mabar.mabar_bridge_config(run, vts=False, mic=1, live={}, continued=continued)
    assert cfg["session_farm_name"] == "Ladang"
    assert "session_farm_name" not in mabar.mabar_bridge_config(run, vts=False, mic=1, live={})
    (run / "self_facts.json").write_text('{"lines": ["Namamu Arti.", "Farm ini bernama Uji Farm."]}', encoding="utf-8")
    cfg = mabar.mabar_bridge_config(run, vts=False, mic=1, live={})
    assert cfg["session_farm_name"] == "Uji" and cfg["live7_self_facts_file"] == str(run / "self_facts.json")
    monkeypatch.setitem(mabar.LIVE_BRIDGE, "vault_farm_labels", False)  # the setting off: the config gains nothing
    assert "session_farm_name" not in mabar.mabar_bridge_config(run, vts=False, mic=1, live={}, continued=continued)


def test_remember_tags_the_runs_note_with_its_farm(monkeypatch, tmp_path):
    from arti_runtime import vault_farm
    from scripts.livepath import mabar_memory
    run = tmp_path / "run"
    run.mkdir()
    (run / "continued.json").write_text('{"save": "Ladang_111", "farm": "Ladang"}', encoding="utf-8")
    scopes = {"a" * 64, "b" * 64}
    monkeypatch.setattr(mabar, "run_scope_hashes", lambda r, current=True: set(scopes))
    monkeypatch.setattr(mabar, "MIND", tmp_path / "mind")
    seen = {}
    monkeypatch.setattr(mabar_memory, "build", lambda r, mind, **kw: seen.update(kw) or {})
    mabar.remember(SimpleNamespace(run=run))
    assert seen["farm_tag"] == vault_farm.tag_line("Ladang", scopes) and seen["scope_hashes"] == scopes
    monkeypatch.setitem(mabar.LIVE_BRIDGE, "vault_farm_labels", False)
    mabar.remember(SimpleNamespace(run=run))
    assert seen["farm_tag"] is None
