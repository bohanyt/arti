"""Passive-mic hygiene (solo stream 2026-11-15, CT decision on W1): bleed noise, mediashare history, vocabulary."""
import queue
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import arti_bridge as bridge
from arti_runtime import mic_hygiene as gate
import arti_voice_pipeline
import arti_voice_queue
from arti_runtime.streamer_voice import addressed

SPEECH = "aduh panennya banyak banget hari ini"
NAMED = "Arti, baca chatnya dong"
GAME_SENTENCE = "kamu mau main game bareng?"
HALLUCINATIONS = (
    "Bahasa Indonesia.",
    "Nama Indonesia.",
    "Nama Arti.",
    "Nama, Arti, Bohan. Game: Stardew Valley.",
    "Nama Aarti Bohan.",
    "Sampai jumpa di video selanjutnya.",
    "Amen.",
    "Book",
    "Game.",
    "Nama.",
)


@pytest.fixture
def passive(monkeypatch):
    monkeypatch.setattr(bridge, "CONFIG", dict(bridge.CONFIG))
    for key, value in {
        "trigger_mode": "push_to_talk",
        "asr_pasif_reply_enabled": True,
        "asr_pasif_reply_min_words": 3,
        "asr_pasif_reply_min_sec": 1.0,
        "asr_pasif_reply_cooldown_sec": 20.0,
        "voice_queue_enabled": False,
        "live7_help_enabled": False,
        "proactive_viewer_wait_sec": 60.0,
        "passive_mic_hygiene": False,
    }.items():
        monkeypatch.setitem(bridge.CONFIG, key, value)
    monkeypatch.setattr(bridge, "hotkey_active", False)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge, "tts_stop_flag", False)
    monkeypatch.setattr(bridge, "_brain_busy", False)
    monkeypatch.setattr(bridge, "_last_passive_voice_turn_ts", 0.0)
    monkeypatch.setattr(bridge, "_passive_junk_gate", bridge.streamer_voice.PassiveGate())
    monkeypatch.setattr(bridge, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(bridge, "voice_trigger_buffer", arti_voice_queue.VoiceTriggerQueue())
    monkeypatch.setattr(bridge, "_stardew_runtime", None)
    monkeypatch.setattr(bridge, "_stardew_bridge_trial", None, raising=False)
    monkeypatch.setattr(bridge, "_live7_help_relay", None)
    monkeypatch.setattr(bridge, "_yt_viewers_seen", {})
    monkeypatch.setattr(bridge, "_last_yt_chat_trigger_ts", 0.0)
    monkeypatch.setattr(bridge, "_pending_turn_id", None)
    monkeypatch.setattr(bridge, "_host_mode", False)
    monkeypatch.setattr(bridge, "_last_streamer_speech_ts", 0.0)
    monkeypatch.setattr(bridge, "_last_arti_reply_ts", 0.0)
    monkeypatch.setattr(bridge, "_media_playback_until", 0.0)
    monkeypatch.setattr(bridge, "_note_screen_privacy", lambda text: None)
    monkeypatch.setattr(bridge, "_note_streamer_text_for_afk", lambda text: None)
    monkeypatch.setattr(bridge, "last_arti_reply_text", "")
    monkeypatch.setattr(bridge.voice_listener_worker, "_last_tts_end", 0.0, raising=False)
    monkeypatch.setattr(bridge.session_transcript, "log_trigger", lambda *a, **k: "t-test")
    history = Mock()
    monkeypatch.setattr(bridge, "add_to_history", history)
    return SimpleNamespace(history=history)


def _queued():
    return list(bridge.voice_trigger_queue.queue)


def _gate_on(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "passive_mic_hygiene", True)


def test_default_switch_off():
    assert bridge.CONFIG.get("passive_mic_hygiene", True) is False


def test_switch_off_is_today(passive):
    assert bridge._handle_passive_ptt_transcript(SPEECH, 2.0) is True
    assert [t.trigger_type for t in _queued()] == ["mic_pasif"]
    passive.history.assert_called_once_with("Streamer", SPEECH)
    assert bridge.is_asr_noise_transcript("Bahasa Indonesia.") is False
    assert bridge.is_asr_noise_transcript("Game.") is False


def test_unnamed_ordinary_speech_still_reaches_her(passive, monkeypatch, capsys):
    """Not a gate: an unaddressed line goes on exactly as today."""
    _gate_on(monkeypatch)
    assert bridge._handle_passive_ptt_transcript(SPEECH, 2.0) is True
    assert [t.trigger_type for t in _queued()] == ["mic_pasif"]
    assert "History only" not in capsys.readouterr().out


@pytest.mark.parametrize("text", HALLUCINATIONS)
def test_hallucination_phrases_dropped(passive, monkeypatch, text):
    _gate_on(monkeypatch)
    assert gate.is_hallucination_transcript(text)
    assert bridge.is_asr_noise_transcript(text) is True
    assert bridge._handle_passive_ptt_transcript(text, 2.0) is False
    passive.history.assert_not_called()
    assert _queued() == []


@pytest.mark.parametrize("text", ["kamu kasih nama Arti dong", "Arti, ayo tidur", "game ini seru banget", "Tidur."])
def test_real_sentences_are_not_hallucinations(text):
    assert not gate.is_hallucination_transcript(text)


def test_real_sentence_with_game_is_kept(passive, monkeypatch):
    _gate_on(monkeypatch)
    assert bridge.is_asr_noise_transcript(GAME_SENTENCE) is False
    assert bridge._handle_passive_ptt_transcript(GAME_SENTENCE, 2.0) is True
    assert [t.trigger_type for t in _queued()] == ["mic_pasif"]


def test_mediashare_unnamed_passive_is_history(passive, monkeypatch, capsys):
    _gate_on(monkeypatch)
    monkeypatch.setattr(bridge, "_media_playback_until", time.time() + 60.0)
    assert bridge._handle_passive_ptt_transcript(SPEECH, 2.0) is False
    passive.history.assert_called_once_with("Streamer", SPEECH)
    assert _queued() == []
    assert "[ASR Pasif] History only (mediashare)." in capsys.readouterr().out


def test_mediashare_named_passive_still_queues(passive, monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setattr(bridge, "_media_playback_until", time.time() + 60.0)
    assert addressed(NAMED, bridge.CONFIG)
    assert bridge._handle_passive_ptt_transcript(NAMED, 2.0) is True
    assert [t.trigger_type for t in _queued()] == ["mic_pasif"]


def test_hold_over_is_today(passive, monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setattr(bridge, "_media_playback_until", time.time() - 1.0)
    assert bridge._handle_passive_ptt_transcript(SPEECH, 2.0) is True


@pytest.mark.parametrize("kind", ["mic", "ptt", "wake_word"])
def test_owner_direct_never_touched(passive, monkeypatch, kind):
    _gate_on(monkeypatch)
    monkeypatch.setattr(bridge, "_media_playback_until", time.time() + 60.0)
    assert bridge.queue_voice_trigger(SPEECH, trigger_type=kind) is True
    assert _queued()[0].trigger_type == kind


def test_whisper_vocabulary_dropped_bleed_words():
    vocab = arti_voice_pipeline.MIC_ASR_VOCABULARY
    assert vocab == "Arti, Bohan, Stardew Valley."
    for bleed in ("Bahasa Indonesia", "Nama", "Game"):
        assert bleed not in vocab


@pytest.mark.parametrize("text", ["Arti, Bohan, Stardew Valley.", "ARTI! Bohan…", "Bohan, Arti.",
                                 "Terima kasih."])
def test_prompt_echo_and_literal_phantom_do_not_count_as_owner_lines(passive, monkeypatch, text):
    _gate_on(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "mic_priority_mark", True)
    revision = bridge.speech_control.owner_revision
    assert bridge._handle_passive_ptt_transcript(text, 2.0) is False
    passive.history.assert_not_called()
    assert bridge.speech_control.owner_revision == revision
    assert not _queued()


@pytest.mark.parametrize("text", ["Arti!", "Stardew Valley.", "Arti, kenapa kamu main Stardew Valley?"])
def test_prompt_echo_keeps_a_real_call_title_or_question(text):
    assert not gate.is_prompt_echo(text, arti_voice_pipeline.MIC_ASR_VOCABULARY)


def test_short_addressed_game_question_passes_passive_ptt_and_typed(passive, monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "mic_priority_mark", True)
    question = "Arti, Stardew Valley?"
    assert bridge._handle_passive_ptt_transcript(question, 2.0)
    bridge.voice_trigger_queue.get_nowait()
    assert bridge._complete_stardew_asr(question, None, 2.0, {})
    bridge.voice_trigger_queue.get_nowait()
    assert bridge.queue_voice_trigger(question, "mic")
    assert _queued()[0].text == question


def test_deliberate_capture_does_not_use_the_vocabulary_subset(passive, monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setattr(bridge, "summarizer_queue", queue.Queue())
    echo = "Arti, Bohan, Stardew Valley."
    assert not bridge._complete_stardew_asr(echo, None, 2.0, {})
    assert not bridge.queue_voice_trigger(echo, "wake_word")
    assert not bridge.queue_voice_trigger(echo, "mic")
    assert bridge._complete_stardew_asr("Bohan, Arti.", None, 2.0, {})
    assert not bridge._complete_stardew_asr("Terima kasih.", None, 2.0, {})


@pytest.mark.parametrize("question", ["Nama Arti?", "Nama Arti？"])
def test_passive_literal_words_with_question_mark_are_kept(passive, monkeypatch, question):
    _gate_on(monkeypatch)
    assert bridge._handle_passive_ptt_transcript(question, 2.0)
    assert _queued()[0].text == question


def test_mabar_turns_hygiene_on(tmp_path):
    from scripts.livepath import mabar
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=False, mic=1, live={})
    assert cfg["passive_mic_hygiene"] is True
    assert cfg.get("mic_priority_mark") is True
