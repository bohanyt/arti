"""PTT and sentence interruption tests; no devices or runtime threads."""
import asyncio
import queue
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import arti_bridge as bridge


@pytest.fixture
def ptt(monkeypatch):
    calls = []
    clock = [100.0]
    monkeypatch.setattr(bridge, "hotkey_active", True)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge, "tts_stop_flag", False)
    monkeypatch.setattr(bridge, "_brain_busy", False)
    monkeypatch.setattr(bridge, "_stardew_ptt_epoch", None)
    monkeypatch.setattr(bridge, "_ptt_last_speech_at", 0.0, raising=False)
    monkeypatch.setitem(bridge.CONFIG, "ptt_auto_off_sec", 60.0)
    monkeypatch.setitem(bridge.CONFIG, "idle_motion_stop_hotkey", "")
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge, "_note_stardew_owner_ingress", lambda *a: 7)
    monkeypatch.setattr(bridge, "_note_stardew_bridge_trial_ptt_capture", lambda: None)
    monkeypatch.setattr(bridge, "_cancel_lamp_fallback", lambda: calls.append("cancel-lamp"))
    monkeypatch.setattr(bridge, "stop_idle_animation", lambda: calls.append("pause-idle"))
    monkeypatch.setattr(bridge, "start_idle_animation", lambda: calls.append("idle"))
    monkeypatch.setattr(bridge, "_discard_stardew_capture", lambda epoch: calls.append(("discard", epoch)))
    monkeypatch.setattr(bridge, "main_event_loop", None)
    return clock, calls


def test_silent_or_rejected_capture_auto_off_once(ptt, capsys):
    clock, calls = ptt
    bridge._ptt_attention_pause()
    clock[0] += 59
    bridge._maybe_auto_off_ptt()
    assert bridge.hotkey_active
    clock[0] += 1
    bridge._maybe_auto_off_ptt()
    bridge._maybe_auto_off_ptt()
    assert not bridge.hotkey_active
    assert calls.count("idle") == 1
    assert ("discard", 7) in calls
    assert capsys.readouterr().out.count("[PTT] auto-off") == 1


def test_captured_speech_resets_auto_off_clock(ptt):
    clock, _ = ptt
    bridge._ptt_attention_pause()
    clock[0] += 59
    bridge._note_ptt_speech()
    clock[0] += 2
    bridge._maybe_auto_off_ptt()
    assert bridge.hotkey_active
    clock[0] += 58
    bridge._maybe_auto_off_ptt()
    assert not bridge.hotkey_active


@pytest.mark.parametrize("backend", ["mouse", "keyboard"])
def test_ptt_toggle_preserves_paid_queue_items(ptt, monkeypatch, backend):
    callbacks = []
    fake = SimpleNamespace(on_button=lambda callback, **kw: callbacks.append(callback),
                           add_hotkey=lambda key, callback: callbacks.append(callback))
    monkeypatch.setitem(sys.modules, backend, fake)
    monkeypatch.setitem(bridge.CONFIG, "trigger_mode", "push_to_talk")
    monkeypatch.setitem(bridge.CONFIG, "hotkey_key", "mouse_x2" if backend == "mouse" else "ctrl+a")
    monkeypatch.setattr(bridge, "hotkey_registered", False)
    monkeypatch.setattr(bridge, "hotkey_active", False)
    monkeypatch.setattr(bridge, "current_api_task", None)
    monkeypatch.setattr(bridge, "_start_mic_watch_once", lambda *a: None)
    monkeypatch.setattr(bridge, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(bridge, "voice_trigger_buffer", bridge.arti_voice_queue.VoiceTriggerQueue())
    for kind in ("donation", "video", "yt_chat", "mic"):
        bridge.voice_trigger_queue.put(bridge.VoiceTrigger("synthetic", kind, None, 0.0))
        bridge.voice_trigger_buffer.enqueue(bridge.arti_voice_queue.QueuedVoiceTrigger("synthetic", kind))
    bridge.init_global_hotkey()
    callbacks[0]()
    assert bridge.hotkey_active and not bridge.tts_stop_flag
    assert [bridge.voice_trigger_queue.get_nowait().trigger_type for _ in range(2)] == ["donation", "video"]
    assert bridge.voice_trigger_queue.empty()
    assert [bridge.voice_trigger_buffer.dequeue().trigger_type for _ in range(2)] == ["donation", "video"]
    assert bridge.voice_trigger_buffer.dequeue() is None
    callbacks[0]()
    # Second press < 2 s = double toggle (bungkam): the only OFF that cuts.
    assert not bridge.hotkey_active and bridge.tts_stop_flag


@pytest.mark.parametrize("backend", ["mouse", "keyboard"])
def test_ptt_toggle_off_after_speech_does_not_cut_arti(ptt, monkeypatch, backend):
    """Owner 2026-10-24: ON + talk + OFF never stops audio or cancels the reply."""
    callbacks, stopped, wall = [], [], [1000.0]
    fake = SimpleNamespace(on_button=lambda callback, **kw: callbacks.append(callback),
                           add_hotkey=lambda key, callback: callbacks.append(callback))
    monkeypatch.setitem(sys.modules, backend, fake)
    monkeypatch.setitem(bridge.CONFIG, "trigger_mode", "push_to_talk")
    monkeypatch.setitem(bridge.CONFIG, "hotkey_key", "mouse_x2" if backend == "mouse" else "ctrl+a")
    monkeypatch.setattr(bridge, "hotkey_registered", False)
    monkeypatch.setattr(bridge, "hotkey_active", False)
    monkeypatch.setattr(bridge, "tts_is_playing", True)
    monkeypatch.setattr(bridge, "_brain_busy", True)
    monkeypatch.setattr(bridge.sd, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(bridge, "request_asr_stream_restart", lambda *a: None)
    monkeypatch.setattr(bridge, "_start_mic_watch_once", lambda *a: None)
    monkeypatch.setattr(bridge, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(bridge, "voice_trigger_buffer", bridge.arti_voice_queue.VoiceTriggerQueue())
    monkeypatch.setattr(bridge.time, "time", lambda: wall[0])
    api = SimpleNamespace(done=lambda: False, cancel=lambda: stopped.append("api-cancel"))
    monkeypatch.setattr(bridge, "current_api_task", api)
    bridge.init_global_hotkey()
    callbacks[0]()  # ON while Arti speaks
    assert bridge.hotkey_active and bridge._ptt_on_mid_turn
    assert stopped == [] and not bridge.tts_stop_flag
    wall[0] += 5.0
    callbacks[0]()  # OFF after talking: priority queue, no cut
    assert not bridge.hotkey_active
    assert stopped == [] and not bridge.tts_stop_flag
    wall[0] += 5.0
    callbacks[0]()
    wall[0] += 0.5
    callbacks[0]()  # double toggle = explicit bungkam still cuts
    assert bridge.tts_stop_flag and "api-cancel" in stopped and True in stopped


@pytest.mark.parametrize("interrupt_at", ["during-sentence", "during-gap"])
@pytest.mark.parametrize("interrupt", ["media", "stop"])
def test_interrupt_cancels_remaining_reply(ptt, monkeypatch, interrupt_at, interrupt):
    spoken = []
    monkeypatch.setattr(bridge, "_media_playback_until", 0.0)
    monkeypatch.setitem(bridge.CONFIG, "tts_jeda_antar_kalimat_sec", 2.0)

    def cancel():
        if interrupt == "ptt":
            bridge._ptt_attention_pause()
        elif interrupt == "media":
            bridge.hold_media_playback(10)
        else:
            bridge.tts_stop_flag = True

    async def speak(chunk):
        spoken.append(chunk)
        if interrupt_at == "during-sentence":
            cancel()

    async def sleep(seconds):
        assert seconds == 2.0
        cancel()

    monkeypatch.setattr(bridge, "tts", SimpleNamespace(speak=speak))
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    asyncio.run(bridge._speak_reply_chunks(["one", "two", "three"], bridge.screen_privacy.epoch))
    assert spoken == ["one"]


def test_ptt_on_keeps_current_audio(ptt, monkeypatch):
    """Owner 2026-10-24 (live): PTT ON never cuts audio Arti is playing."""
    stopped = []
    monkeypatch.setattr(bridge, "tts_is_playing", True)
    monkeypatch.setattr(bridge.sd, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(bridge, "request_asr_stream_restart", lambda *a: None)
    bridge._ptt_attention_pause()
    assert stopped == [] and not bridge.tts_stop_flag
    assert bridge._ptt_on_mid_turn


@pytest.mark.parametrize("interrupt_at", ["during-sentence", "during-gap"])
def test_ptt_on_during_reply_keeps_remaining_sentences(ptt, monkeypatch, interrupt_at):
    spoken = []
    monkeypatch.setattr(bridge, "_media_playback_until", 0.0)
    monkeypatch.setitem(bridge.CONFIG, "tts_jeda_antar_kalimat_sec", 2.0)

    async def speak(chunk):
        spoken.append(chunk)
        if interrupt_at == "during-sentence" and len(spoken) == 1:
            bridge._ptt_attention_pause()

    async def sleep(seconds):
        if interrupt_at == "during-gap" and len(spoken) == 1:
            bridge._ptt_attention_pause()

    monkeypatch.setattr(bridge, "tts", SimpleNamespace(speak=speak))
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    asyncio.run(bridge._speak_reply_chunks(["one", "two", "three"], bridge.screen_privacy.epoch))
    assert spoken == ["one", "two", "three"]


def test_uninterrupted_reply_keeps_all_sentences_and_gaps(ptt, monkeypatch):
    spoken, sleeps = [], []

    async def speak(chunk):
        spoken.append(chunk)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setitem(bridge.CONFIG, "tts_jeda_antar_kalimat_sec", 2.0)
    monkeypatch.setattr(bridge, "tts", SimpleNamespace(speak=speak))
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    asyncio.run(bridge._speak_reply_chunks(["one", "two", "three"], bridge.screen_privacy.epoch))
    assert spoken == ["one", "two", "three"] and sleeps == [2.0, 2.0]


@pytest.mark.parametrize("cause", ["interrupt", "privacy"])
def test_reply_cancelled_before_first_sentence_stays_silent(ptt, monkeypatch, cause):
    spoken = []

    async def speak(chunk):
        spoken.append(chunk)

    monkeypatch.setattr(bridge, "tts", SimpleNamespace(speak=speak))
    if cause == "interrupt":
        monkeypatch.setattr(bridge, "tts_stop_flag", True)
    else:
        monkeypatch.setattr(bridge, "screen_privacy", SimpleNamespace(current=lambda epoch: False, epoch=0))
    asyncio.run(bridge._speak_reply_chunks(["one", "two"], bridge.screen_privacy.epoch))
    assert spoken == []


def test_ptt_auto_off_can_be_disabled(ptt, monkeypatch):
    clock, calls = ptt
    bridge._ptt_attention_pause()
    monkeypatch.setitem(bridge.CONFIG, "ptt_auto_off_sec", 0)
    clock[0] += 600
    bridge._maybe_auto_off_ptt()
    assert bridge.hotkey_active and "idle" not in calls


def test_mid_turn_ptt_keeps_hotkey_and_next_turn_can_speak(ptt, monkeypatch):
    """Exercise the actual handler: PTT ON mid-turn no longer cuts the reply,
    the new PTT survives the turn's auto-OFF, and the next turn resets it."""
    spoken = []
    monkeypatch.setattr(bridge, "hotkey_active", False)
    monkeypatch.setattr(bridge, "tts_stop_flag", True)  # Previous turn was cancelled.
    monkeypatch.setattr(bridge, "_brain_busy", True)  # main loop holds the turn
    monkeypatch.setattr(bridge, "_prepare_turn_start", AsyncMock())
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: True)
    monkeypatch.setitem(bridge.CONFIG, "minecraft_mute_screen_curious", True)
    monkeypatch.setitem(bridge.CONFIG, "expression_nod_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "tts_jeda_antar_kalimat_sec", 2)
    monkeypatch.setattr(bridge, "mood_block_now", lambda: "")
    monkeypatch.setattr(bridge, "build_today_block", lambda: "")
    monkeypatch.setattr(bridge, "viewer_block_for", lambda _: "")
    monkeypatch.setattr(bridge, "_should_route_to_cursor", lambda _: False)
    monkeypatch.setattr(bridge, "_try_stardew_owner_generation", lambda _: ("Satu. Dua. Tiga.", True))
    monkeypatch.setattr(bridge.arti_voice_pipeline, "prepare_turn_context", AsyncMock(return_value=SimpleNamespace(
        formatted_history="", llm_system="", prompt_content="", target_instruction="", rag_query="",
    )))
    monkeypatch.setattr(bridge, "clean_ai_reply", lambda text: text)
    monkeypatch.setattr(bridge, "post_process_response", lambda text, _: text)
    monkeypatch.setattr(bridge, "_maybe_execute_reply_tags", lambda text, *a: text)
    monkeypatch.setattr(bridge, "_sentences_or_empty", lambda text: ["Satu.", "Dua.", "Tiga."])
    monkeypatch.setattr(bridge, "_media_playback_until", 0)
    monkeypatch.setattr(bridge, "vts", SimpleNamespace())
    monkeypatch.setattr(bridge.arti_expression_runtime, "apply_speaking", AsyncMock())
    ended = AsyncMock()
    monkeypatch.setattr(bridge.arti_expression_runtime, "apply_turn_end", ended)
    monkeypatch.setattr(bridge, "add_to_history", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_cermin_ke_chat_game", lambda *a: None)
    monkeypatch.setattr(bridge, "_schedule_post_answer_cleanup", lambda: None)
    monkeypatch.setattr(bridge, "current_api_task", None)
    monkeypatch.setattr(bridge, "last_arti_reply_text", "")
    monkeypatch.setattr(bridge, "_pending_turn_id", None)
    monkeypatch.setattr(bridge.voice_listener_worker, "_last_tts_end", 0, raising=False)

    async def speak(text):
        spoken.append(text)
        if len(spoken) == 1:
            bridge.hotkey_active = True
            bridge._ptt_attention_pause()

    async def sleep(_seconds):
        pass

    monkeypatch.setattr(bridge, "tts", SimpleNamespace(speak=speak))
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    trigger = bridge.VoiceTrigger("synthetic", "mic")
    asyncio.run(bridge._handle_voice_trigger(trigger, [], "synthetic"))
    assert spoken == ["Satu.", "Dua.", "Tiga."] and bridge.hotkey_active
    assert not bridge.tts_stop_flag
    ended.assert_awaited_once()
    asyncio.run(bridge._handle_voice_trigger(trigger, [], "synthetic"))
    assert spoken == ["Satu.", "Dua.", "Tiga."] * 2
    assert not bridge.hotkey_active and ended.await_count == 2
