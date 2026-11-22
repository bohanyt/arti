"""Alerts yield after the current sentence (2026-11-12 option A)."""
from __future__ import annotations

import asyncio
import queue as _queue
from pathlib import Path
from types import SimpleNamespace

import pytest

import arti_bridge as b

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


class FakeTimer:
    def __init__(self, interval, fn, *a, **k):
        self.interval = interval
        self.fn = fn
        self.daemon = False
        self.started = False
        self.cancelled = False
        timers.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


timers: list[FakeTimer] = []


@pytest.fixture
def alerts(monkeypatch):
    timers.clear()
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", False)
    monkeypatch.setitem(b.CONFIG, "alerts_yield_max_sec", 8.0)
    monkeypatch.setitem(b.CONFIG, "tts_jeda_antar_kalimat_sec", 0)
    monkeypatch.setitem(b.CONFIG, "donation_alert_base_sec", 0)
    monkeypatch.setitem(b.CONFIG, "voice_queue_enabled", False)
    monkeypatch.setattr(b.threading, "Timer", FakeTimer)
    monkeypatch.setattr(b.session_transcript, "log_trigger", lambda *a, **k: "tid")
    monkeypatch.setattr(b, "request_asr_stream_restart", lambda *a, **k: None)
    monkeypatch.setattr(b.screen_privacy, "current", lambda _: True)
    monkeypatch.setattr(b, "tts_stop_flag", False)
    monkeypatch.setattr(b, "tts_is_playing", False)
    b._yield_after_sentence = False
    b._yield_after_sentence_reason = None
    b._pending_alert_yield_fact = None
    b._yield_bound_timer = None
    b._reply_chunks_inflight = False
    b.voice_trigger_buffer.clear()
    while True:
        try:
            b.voice_trigger_queue.get_nowait()
        except _queue.Empty:
            break
    yield
    b._clear_alert_yield()
    b._pending_alert_yield_fact = None
    b._reply_chunks_inflight = False


def _speak_engine(monkeypatch, on_chunk=None):
    spoken = []

    async def speak(chunk):
        spoken.append(chunk)
        if on_chunk:
            on_chunk(chunk, spoken)
        engine._last_playback_completed = True

    engine = SimpleNamespace(speak=speak, _last_playback_completed=True)
    monkeypatch.setattr(b, "tts", engine, raising=False)
    return spoken


def test_config_defaults_off():
    assert '"alerts_yield_after_sentence": False,' in SRC
    assert '"alerts_yield_max_sec": 8.0,' in SRC
    from scripts.livepath import mabar
    assert "alerts_yield_after_sentence" in mabar.LIVE_OVERLAY_KEYS
    assert "alerts_yield_max_sec" in mabar.LIVE_OVERLAY_KEYS


def test_yield_lands_only_between_sentences(alerts, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)
    stopped = []
    monkeypatch.setattr(b.sd, "stop", lambda: stopped.append(1))

    def on_chunk(_chunk, spoken):
        if len(spoken) == 1:
            b.hold_media_playback(10)

    spoken = _speak_engine(monkeypatch, on_chunk)
    result = asyncio.run(b._speak_reply_chunks(["Satu.", "Dua.", "Tiga."], 1))
    assert spoken == ["Satu."]
    assert result.spoken_n == 1 and result.yielded
    assert stopped == []
    assert not b.tts_stop_flag


def test_yield_during_jeda_skips_next_chunk(alerts, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)
    monkeypatch.setitem(b.CONFIG, "tts_jeda_antar_kalimat_sec", 2.0)
    spoken = _speak_engine(monkeypatch)

    async def sleep(_seconds):
        b.queue_voice_trigger("[DONASI]", trigger_type="donation", viewer_name="A")

    monkeypatch.setattr(b.asyncio, "sleep", sleep)
    result = asyncio.run(b._speak_reply_chunks(["Satu.", "Dua."], 1))
    assert spoken == ["Satu."]
    assert result.spoken_n == 1 and result.yielded
    assert result.spoken_text == "Satu."


def test_hold_soft_does_not_call_sd_stop(alerts, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)
    monkeypatch.setattr(b, "tts_is_playing", True)
    monkeypatch.setattr(b, "_reply_chunks_inflight", True)
    stopped = []
    monkeypatch.setattr(b.sd, "stop", lambda: stopped.append(1))
    until_before = b._media_playback_until
    b.hold_media_playback(15.0)
    assert stopped == []
    assert not b.tts_stop_flag
    assert b._yield_after_sentence
    assert b._yield_after_sentence_reason == "mediashare"
    assert b._media_playback_until >= until_before
    assert timers and timers[0].interval == 8.0 and timers[0].started


def test_hold_switch_off_still_hard_stops(alerts, monkeypatch):
    monkeypatch.setattr(b, "tts_is_playing", True)
    stopped = []
    monkeypatch.setattr(b.sd, "stop", lambda: stopped.append(1))
    b.hold_media_playback(10.0)
    assert stopped == [1]
    assert b.tts_stop_flag
    assert not b._yield_after_sentence
    assert timers == []


def test_unsaid_text_never_reaches_history(alerts, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)
    logged = []
    history = []
    monkeypatch.setattr(b, "stream_history", history)
    monkeypatch.setattr(
        b.session_transcript, "log_arti_reply",
        lambda message, cfg, **k: logged.append(message),
    )
    monkeypatch.setattr(b.session_transcript, "append_from_history", lambda *a, **k: None)

    def on_chunk(_chunk, spoken):
        if len(spoken) == 1:
            b._request_alert_yield("donasi")

    _speak_engine(monkeypatch, on_chunk)
    full = "Kalimat satu. Kalimat dua jangan tersimpan."
    chunks = ["Kalimat satu.", "Kalimat dua jangan tersimpan."]
    result = asyncio.run(b._speak_reply_chunks(chunks, 1))
    text = b._apply_alert_yield_to_reply(full, result)
    b.add_to_history("Arti (VTuber)", text, arti_meta={"turn_id": "t"})
    assert text == "Kalimat satu."
    assert "jangan tersimpan" not in text
    assert logged == ["Kalimat satu."]
    assert history and "Kalimat satu." in history[-1]
    assert "jangan tersimpan" not in history[-1]
    i_speak = SRC.index("await _speak_reply_chunks(tts_sentence_chunks")
    i_apply = SRC.index("ai_reply = _apply_alert_yield_to_reply(ai_reply, voiced)")
    i_hist = SRC.index('add_to_history("Arti (VTuber)", ai_reply')
    assert i_speak < i_apply < i_hist


def test_yield_fact_appears_once_on_next_turn(alerts, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)

    def on_chunk(_chunk, spoken):
        if len(spoken) == 1:
            b._request_alert_yield("donasi")

    _speak_engine(monkeypatch, on_chunk)
    asyncio.run(b._speak_reply_chunks(["Satu.", "Dua."], 1))
    first = b._alert_yield_fact_block()
    assert first == (
        "\n[FAKTA] Sisa jawabanmu tadi belum terucap karena ada donasi masuk.\n"
    )
    assert b._alert_yield_fact_block() == ""
    assert "_alert_yield_fact_block()" in SRC.split("turn_system_prompt = (")[1][:500]

    def on_media(_chunk, spoken):
        if len(spoken) == 1:
            b._request_alert_yield("mediashare")

    _speak_engine(monkeypatch, on_media)
    asyncio.run(b._speak_reply_chunks(["Satu.", "Dua."], 1))
    media = b._alert_yield_fact_block()
    assert media == (
        "\n[FAKTA] Sisa jawabanmu tadi belum terucap karena ada mediashare masuk.\n"
    )
    assert b._alert_yield_fact_block() == ""


def test_yt_chat_game_and_owner_never_set_yield_flag(alerts, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)
    b._reply_chunks_inflight = True
    monkeypatch.setattr(b, "_brain_busy", False)
    monkeypatch.setattr(b, "add_to_history", lambda *a, **k: None)
    for kind in ("yt_chat", "game", "own_play", "read_in_game", "status", "moment", "mic"):
        b._yield_after_sentence = False
        b._yield_after_sentence_reason = None
        text = "halo arti"
        if kind == "read_in_game":
            text = type("GameRead", (str,), {
                "created_at": __import__("time").time(),
                "_live7_moment": False,
                "_own_play": False,
            })("baca")
        b.queue_voice_trigger(text, trigger_type=kind, viewer_name=None if kind == "mic" else "v")
        assert not b._yield_after_sentence, kind
    b._yield_after_sentence = False
    assert b.queue_voice_trigger("[DONASI]", trigger_type="donation", viewer_name="A")
    assert b._yield_after_sentence and b._yield_after_sentence_reason == "donasi"
    b._clear_alert_yield()
    b._reply_chunks_inflight = True
    b.schedule_donation_trigger("[DONASI]", "A", "hai")
    assert b._yield_after_sentence_reason == "donasi"


def test_yield_bound_hard_stop_and_log(alerts, monkeypatch, capsys):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)
    monkeypatch.setattr(b, "tts_is_playing", True)
    monkeypatch.setattr(b, "_reply_chunks_inflight", True)
    stopped = []
    monkeypatch.setattr(b.sd, "stop", lambda: stopped.append(1))
    b.hold_media_playback(10.0)
    assert stopped == []
    assert not b.tts_stop_flag
    assert timers and timers[0].interval == 8.0
    timers[0].fn()
    assert stopped == [1]
    assert b.tts_stop_flag
    out = capsys.readouterr().out
    assert "[Alerts] yield bound hit (8.0s) — hard stop" in out


def test_switch_off_speak_still_hard_cuts_on_media(alerts, monkeypatch):
    stopped = []
    monkeypatch.setattr(b.sd, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(b, "tts_is_playing", True)

    def on_chunk(_chunk, spoken):
        if len(spoken) == 1:
            b.hold_media_playback(10)

    spoken = _speak_engine(monkeypatch, on_chunk)
    result = asyncio.run(b._speak_reply_chunks(["Satu.", "Dua."], 1))
    assert spoken == ["Satu."]
    assert stopped == [1]
    assert b.tts_stop_flag
    assert not getattr(result, "yielded", False)
    full = "Satu. Dua."
    assert b._apply_alert_yield_to_reply(full, result) == full


def test_owner_stop_and_prompt_wiring_unchanged(alerts, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "alerts_yield_after_sentence", True)
    monkeypatch.setattr(b, "tts_is_playing", True)
    stopped = []
    monkeypatch.setattr(b.sd, "stop", lambda: stopped.append(1))
    try:
        b._note_owner_speech("diam", "mic", None)
        assert b.tts_stop_flag and stopped == [1]
    finally:
        b._note_owner_speech("boleh ngomong lagi", "mic", None)
    ptt = SRC[SRC.index("def init_global_hotkey"): SRC.index("def init_global_hotkey") + 8000]
    assert "tts_stop_flag = True" in ptt
    assert "DOUBLE TOGGLE" in ptt


def test_mabar_alert_yield_flag_sets_only_the_run_config(tmp_path):
    from scripts.livepath import mabar
    on = mabar.mabar_bridge_config(tmp_path / "a", vts=False, mic=1, live={}, alert_yield=True)
    off = mabar.mabar_bridge_config(tmp_path / "b", vts=False, mic=1, live={})
    assert on["alerts_yield_after_sentence"] is True
    assert not off.get("alerts_yield_after_sentence")
