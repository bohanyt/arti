"""Rehearsal-only scripted PTT and scratch stores (2026-11-18).

The pkg111 rehearsal showed: typed `mabar say` lines entered as trigger "mic", so the said-row path (ADR 233), the
voice play mark and the marked-line window (V6.5) could not be proven without a person; and the vault recall of her
real farm answered over the scratch game memory. With `mabar --rehearsal-scratch DIR` her mind, vault recall and
index live in DIR and a scripted owner line enters exactly like a PTT transcript. It needs the config flag, a
rehearsal parent, the env var, and no live chat, so it cannot switch on in a live session by accident.
"""
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

import arti_bridge as b
from arti_runtime import stream_notes
from scripts.livepath import mabar


@pytest.fixture
def typed(monkeypatch):
    seen = SimpleNamespace(queued=[], notes=[])
    monkeypatch.setattr(b, "queue_voice_trigger", lambda text, trigger_type="mic", **kw: seen.queued.append(
        (text, trigger_type)) or True)
    monkeypatch.setattr(b, "add_to_history", lambda *a, **k: None)
    monkeypatch.setattr(b, "_note_screen_privacy", lambda text: None)
    monkeypatch.setattr(stream_notes, "bridge_line", lambda bridge, speaker, ingress, text, **kw: seen.notes.append(
        (speaker, ingress, text, kw.get("owner_marked"))))
    for key, value in dict(rehearsal_scripted_ptt=True, rehearsal_parent_pid=1234, youtube_chat_enabled=False,
                           ssn_chat_enabled=False, stream_game_notes=True).items():
        monkeypatch.setitem(b.CONFIG, key, value)
    monkeypatch.setenv("ARTI_REHEARSAL_SCRIPTED_PTT", "1")

    def feed(*lines):
        monkeypatch.setattr(b.sys, "stdin", io.StringIO("".join(line + "\n" for line in lines)))
        b.text_input_worker()
        return seen
    return feed


def test_a_scripted_owner_line_enters_as_ptt_with_its_said_row(typed):
    seen = typed("Ambil parsnip yang dekat rumahku ya.")
    assert seen.queued == [("Ambil parsnip yang dekat rumahku ya.", "ptt")]
    assert seen.notes == [("owner", "ptt", "Ambil parsnip yang dekat rumahku ya.", True)]


@pytest.mark.parametrize("off", [
    dict(config=dict(rehearsal_scripted_ptt=False)),
    dict(config=dict(rehearsal_parent_pid=None)),
    dict(config=dict(youtube_chat_enabled=True)),   # a live audience is attached
    dict(config=dict(ssn_chat_enabled=True)),
    dict(env=None),
])
def test_any_missing_condition_keeps_todays_mic_line(typed, monkeypatch, off):
    for key, value in off.get("config", {}).items():
        monkeypatch.setitem(b.CONFIG, key, value)
    if "env" in off:
        monkeypatch.delenv("ARTI_REHEARSAL_SCRIPTED_PTT", raising=False)
    seen = typed("halo arti")
    assert seen.queued == [("halo arti", "mic")] and seen.notes == []


@pytest.fixture
def scratch_globals(monkeypatch, tmp_path):
    for name in ("MIND", "VAULT_ROOT", "VAULT_INDEX", "REHEARSAL_SCRIPTED_PTT"):
        monkeypatch.setattr(mabar, name, getattr(mabar, name))
    real = tmp_path / "real"
    monkeypatch.setattr(mabar, "MIND", real / "arti-workspace" / "mind")
    monkeypatch.setattr(mabar, "STREAM", real / "stream-folder")
    monkeypatch.setattr(mabar, "VAULT_INDEX", real / "arti-workspace" / "vault-index" / "vault_rag.db")
    return tmp_path


def test_rehearsal_scratch_moves_mind_vault_and_index(scratch_globals):
    root = scratch_globals / "scratch"
    mabar.apply_rehearsal_scratch(SimpleNamespace(rehearsal_scratch=root))
    assert mabar.MIND == root.resolve() / "mind" and mabar.VAULT_ROOT == root.resolve() / "vault"
    assert mabar.VAULT_INDEX == root.resolve() / "vault-index" / "vault_rag.db" and mabar.REHEARSAL_SCRIPTED_PTT
    vault = mabar.vault_config({})
    assert vault["vault_rag_root"] == str(root.resolve() / "vault")
    cfg = mabar.mabar_bridge_config(scratch_globals / "run", vts=False, mic=1, live={})
    assert cfg["rehearsal_scripted_ptt"] is True and cfg["voice_game_memory_enabled"] is True
    assert cfg["youtube_chat_enabled"] is False and cfg["ssn_chat_enabled"] is False
    assert cfg["arti_mind_dir"] == str(root.resolve() / "mind")
    assert cfg["vault_rag_root"] == str(root.resolve() / "vault")


@pytest.mark.parametrize("live", [dict(youtube_video_id="abc"), dict(video_id="abc"), dict(ssn_from_app=True),
                                  dict(ssn_session_file="x")])
def test_rehearsal_scratch_refuses_live_chat(scratch_globals, live):
    with pytest.raises(RuntimeError, match="no audience"):
        mabar.apply_rehearsal_scratch(SimpleNamespace(rehearsal_scratch=scratch_globals / "s", **live))
    assert mabar.REHEARSAL_SCRIPTED_PTT is False


@pytest.mark.parametrize("inside", ["real/arti-workspace/mind/x", "real/stream-folder", "real"])
def test_rehearsal_scratch_refuses_her_real_stores(scratch_globals, inside):
    with pytest.raises(RuntimeError, match="outside her real stores"):
        mabar.apply_rehearsal_scratch(SimpleNamespace(rehearsal_scratch=scratch_globals / inside))


def test_without_scratch_the_bridge_config_has_no_rehearsal_keys(scratch_globals):
    cfg = mabar.mabar_bridge_config(scratch_globals / "run", vts=False, mic=1, live={})
    assert "rehearsal_scripted_ptt" not in cfg
    assert mabar.vault_config({})["vault_rag_root"] == str(mabar.STREAM)


def test_cli_parses_the_flag():
    seen = {}
    original = mabar.STEPS["status"]
    mabar.STEPS["status"] = lambda a: seen.update(scratch=a.rehearsal_scratch) or 0
    saved = (mabar.MIND, mabar.VAULT_ROOT, mabar.VAULT_INDEX, mabar.REHEARSAL_SCRIPTED_PTT)
    try:
        with pytest.raises(RuntimeError, match="no audience"):
            mabar.main(["status", "--run", "x", "--rehearsal-scratch", "y", "--youtube-video-id", "v"])
    finally:
        mabar.STEPS["status"] = original
        mabar.MIND, mabar.VAULT_ROOT, mabar.VAULT_INDEX, mabar.REHEARSAL_SCRIPTED_PTT = saved


# --------------------------------------------------------------- pass-3 follow-up: the scripted double toggle (bungkam)

@pytest.fixture
def cut(typed, monkeypatch):
    """Her turn in flight and her voice playing; records what the double-toggle path touches."""
    calls = []

    class Task:
        def done(self):
            return False

        def cancel(self):
            calls.append("api_task.cancel")

    monkeypatch.setattr(b, "current_api_task", Task())
    monkeypatch.setattr(b, "tts_is_playing", True)
    monkeypatch.setattr(b, "tts_stop_flag", False)
    monkeypatch.setattr(b, "sd", SimpleNamespace(stop=lambda: calls.append("sd.stop")))
    monkeypatch.setattr(b, "clear_trigger_queue", lambda **kw: calls.append("clear_trigger_queue"))
    monkeypatch.setattr(b, "_ptt_attention_pause", lambda: calls.append("attention_pause"))
    monkeypatch.setattr(b, "_cancel_lamp_fallback", lambda: calls.append("lamp"))
    monkeypatch.setattr(b, "start_idle_animation", lambda: calls.append("idle"))
    monkeypatch.setattr(b, "request_asr_stream_restart", lambda reason="": calls.append("asr_restart"))
    monkeypatch.setattr(b.arti_executors, "note_user_cancel", lambda: calls.append("user_cancel"))
    return SimpleNamespace(feed=typed, calls=calls)


def test_a_scripted_bungkam_stops_her_turn_like_the_owners_double_toggle(cut):
    seen = cut.feed("bungkam")
    assert seen.queued == [] and seen.notes == []  # not an owner line she answers
    assert b.tts_stop_flag is True
    assert cut.calls == ["user_cancel", "clear_trigger_queue", "attention_pause",  # toggle ON
                         "user_cancel", "lamp", "idle", "api_task.cancel", "sd.stop", "asr_restart",  # OFF, bungkam
                         "clear_trigger_queue"]


@pytest.mark.parametrize("off", [
    dict(config=dict(rehearsal_scripted_ptt=False)),
    dict(config=dict(rehearsal_parent_pid=None)),
    dict(config=dict(youtube_chat_enabled=True)),
    dict(config=dict(ssn_chat_enabled=True)),
    dict(env=None),
])
def test_without_every_rehearsal_condition_bungkam_is_todays_typed_line(cut, monkeypatch, off):
    for key, value in off.get("config", {}).items():
        monkeypatch.setitem(b.CONFIG, key, value)
    if "env" in off:
        monkeypatch.delenv("ARTI_REHEARSAL_SCRIPTED_PTT", raising=False)
    seen = cut.feed("bungkam")
    assert seen.queued == [("bungkam", "mic")] and cut.calls == [] and b.tts_stop_flag is False


def test_mabar_say_bungkam_writes_only_into_a_rehearsal_run(tmp_path):
    import json
    live, rehearsal = tmp_path / "live", tmp_path / "rehearsal"
    for run, scripted in ((live, None), (rehearsal, True)):
        run.mkdir()
        cfg = {"stream_game_notes": True} if scripted is None else {"rehearsal_scripted_ptt": True}
        (run / "rehearsal-config.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(SystemExit, match="rehearsal"):
        mabar.say(SimpleNamespace(run=live, text="", bungkam=True))
    assert not (live / "owner-say.txt").exists()
    mabar.say(SimpleNamespace(run=rehearsal, text="", bungkam=True))
    mabar.say(SimpleNamespace(run=rehearsal, text="halo", bungkam=False))
    assert (rehearsal / "owner-say.txt").read_text(encoding="utf-8") == "bungkam\nhalo\n"
