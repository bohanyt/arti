"""Batch 9 contracts: synthetic audio/text and fake transports only."""

import ast
import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close
from websockets.protocol import State
import websockets.sync.client

import arti_bridge as bridge
import arti_donations as donations
import arti_openrouter as openrouter
import arti_reply_policy as rp
import arti_voice_dsp as dsp
import subtitle_server as subtitles


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(bridge, "CONFIG", dict(bridge.CONFIG))
    monkeypatch.setattr(bridge, "local_whisper_model", None)
    monkeypatch.setattr(bridge, "_local_whisper_gpu_failed", False, raising=False)
    monkeypatch.setattr(bridge, "_local_whisper_lock", threading.Lock(), raising=False)
    monkeypatch.setattr(bridge, "_idle_ws", None)
    monkeypatch.setattr(bridge, "_idle_ws_lock", None)
    monkeypatch.setattr(bridge, "_idle_reconnect_backoff", 0.0)


def run_saweria(monkeypatch, outcomes):
    listener = donations.SaweriaListener("synthetic", Mock())
    waits, clock, connects = [], [0.0], []
    monkeypatch.setattr(donations.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(listener._stop_event, "wait", lambda delay: waits.append(delay))
    pending = iter(outcomes)

    class Socket:
        def __init__(self, outcome):
            self.outcome = outcome

        def __enter__(self):
            if self.outcome == "stop":
                listener.stop()
            return self

        def __exit__(self, *args):
            pass

        def recv(self, **kwargs):
            duration, error = self.outcome
            clock[0] += duration
            raise error

    def connect(*args, **kwargs):
        connects.append(clock[0])
        outcome = next(pending)
        if isinstance(outcome, Exception):
            raise outcome
        return Socket(outcome)

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    listener.run()
    listener.on_event.assert_not_called()
    return waits, connects


def test_saweria_clean_close_reconnects_without_sleep(monkeypatch):
    clean = ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
    waits, connects = run_saweria(monkeypatch, [(120, clean), "stop"])
    assert waits == []
    assert connects == [0, 120]


def test_saweria_clean_close_right_after_connect_still_backs_off(monkeypatch):
    clean = ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
    waits, _ = run_saweria(monkeypatch, [(0.5, clean), "stop"])
    assert waits == [5]


def test_saweria_backoff_resets_on_every_connect(monkeypatch):
    waits, _ = run_saweria(monkeypatch, [
        OSError("connect"), OSError("connect"),
        (1, OSError("short")), (10, OSError("medium")), "stop",
    ])
    # Failed handshakes double; any successful connect resets to 5 s, so the
    # blind window after a connection is never longer than 5 s.
    assert waits == [5, 10, 5, 5]


def test_saweria_clean_close_after_errors_still_retries_instantly(monkeypatch):
    clean = ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
    waits, connects = run_saweria(monkeypatch, [
        OSError("connect"), (6, clean), (2, OSError("short")), "stop",
    ])
    assert waits == [5, 5]
    assert connects == [0, 0, 6, 8]  # clean close at 6 s: reconnect at once


@pytest.mark.parametrize("speech", ["synthetic input", "[Inisiatif] synthetic input"])
def test_reply_token_budget_ignores_reply_char_config(speech):
    # Batch 9 derived tokens from arti_reply_max_chars; reverted (empty replies
    # on reasoning models). The char cut is the only length control.
    small = {**bridge.CONFIG, "arti_reply_max_chars": 90}
    large = {**bridge.CONFIG, "arti_reply_max_chars": 580}
    assert (bridge.live_max_tokens_for_trigger(speech, small)
            == bridge.live_max_tokens_for_trigger(speech, large))
    assert bridge.get_arti_reply_limits(speech, small) == bridge.get_arti_reply_limits(speech, large)


@pytest.mark.parametrize("text,limit,expected", [
    ("First sentence here. Another long sentence follows.", 30, "First sentence here."),
    ("First longer clause, another long clause follows.", 25, "First longer clause…"),
    ("First whole token continues without punctuation", 19, "First whole token…"),
    ("Unbrokenwordtoolong", 7, "…"),
    ("Fits unchanged.", 30, "Fits unchanged."),
    # A boundary that keeps < 60% of the budget falls back to the word cut.
    ("First sentence. Another long sentence follows.", 30, "First sentence. Another long…"),
    # "." / "," between digits is not a boundary, even at the cut edge.
    ("Bibitnya murah kok, harganya Rp10.000 lho buat satu kantong", 35,
     "Bibitnya murah kok, harganya…"),
    ("Rating game ini 3,5 dari 5 bintang menurut aku", 19, "Rating game ini…"),
    # One leftover word would be a lone interjection: keep the hard cut.
    ("Hah, " + "x" * 40, 20, "Hah, " + "x" * 14 + "…"),
])
def test_reply_safety_cut_keeps_boundaries(text, limit, expected):
    # 2026-10-23: the clause/word cutter is now only the runaway brake (one
    # sentence > 2x the hard cap). Normal truncation is sentence-boundary only;
    # see tests/test_reply_length_no_midcut.py.
    result = bridge._runaway_clause_cut(text, limit)
    assert result == expected
    assert len(result) <= limit


@pytest.mark.parametrize("opener", ["Hah,", "Iya!", "Eh,"])
def test_reply_cut_never_reduces_to_opening_interjection(opener):
    text = (f"{opener} serius kamu nanya itu sekarang padahal dari tadi aku udah "
            "jelasin panjang lebar soal farming di Stardew yang bikin pusing banget "
            "sampai aku lupa makan siang tadi")
    assert len(text) > 135
    # Runaway brake only (see test above). "Iya!" + a long sentence below
    # 2x the cap stays whole through _truncate_reply_length (review follow-up
    # 2026-10-23: same rule as a long first sentence, no mid-cut).
    result = bridge._runaway_clause_cut(text, 135)
    if opener == "Iya!":
        assert bridge._truncate_reply_length(text, 5, 135) == text
    assert result.startswith(f"{opener} serius kamu nanya")
    assert result.endswith("…") and 0.6 * 135 <= len(result) <= 135
    assert text.startswith(result[:-1])


def test_openrouter_reply_budget_keeps_original_limit(monkeypatch):
    call = Mock(return_value="synthetic reply")
    monkeypatch.setattr(openrouter, "openrouter_chat", call)
    monkeypatch.setattr(openrouter, "openrouter_live_model_chain", lambda cfg: ["unit-model"])
    cfg = {"openrouter_api_key": "synthetic", "arti_reply_max_chars": 90}
    openrouter.openrouter_live_completion("system", "synthetic input", cfg)
    assert call.call_args.kwargs["max_tokens"] == 380


@pytest.mark.parametrize("text", ["", "   ", None])
def test_groq_successful_silence_never_loads_local_whisper(monkeypatch, text):
    monkeypatch.setattr(bridge, "_groq_asr_with_deadline", lambda *a: (200, "", {"text": text}))
    factory = Mock(side_effect=AssertionError("local Whisper must stay unloaded"))
    monkeypatch.setattr(bridge, "WhisperModel", factory)
    assert bridge.transcribe_audio(np.zeros(160), api_key="gsk_SYNTHETIC") == ""
    factory.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["load", "segments"])
def test_cuda_failure_is_remembered_even_if_cpu_first_load_fails(monkeypatch, failure_stage):
    devices = []
    cpu_attempts = []

    def segments(*args, **kwargs):
        yield SimpleNamespace(text="synthetic")
        raise RuntimeError("cublas unavailable")

    def factory(model, *, device, compute_type):
        devices.append(device)
        if device == "cuda":
            if failure_stage == "load":
                raise RuntimeError("cuda unavailable")
            return SimpleNamespace(transcribe=lambda *a, **kw: (segments(), None))
        cpu_attempts.append(True)
        if len(cpu_attempts) == 1:
            raise RuntimeError("temporary CPU load failure")
        return SimpleNamespace(transcribe=lambda *a, **kw: ([SimpleNamespace(text="synthetic")], None))

    monkeypatch.setattr(bridge, "WhisperModel", factory)
    assert bridge.transcribe_audio(np.zeros(160), use_groq=False) == ""
    assert bridge.transcribe_audio(np.zeros(160), use_groq=False) == "synthetic"
    assert devices == ["cuda", "cpu", "cpu"]


def test_concurrent_local_whisper_calls_load_once(monkeypatch):
    started, release = threading.Event(), threading.Event()
    model = SimpleNamespace(transcribe=lambda *a, **kw: ([SimpleNamespace(text="synthetic")], None))

    def load(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return model

    factory = Mock(side_effect=load)
    monkeypatch.setattr(bridge, "WhisperModel", factory)
    results = []
    workers = [threading.Thread(target=lambda: results.append(
        bridge.transcribe_audio(np.zeros(160), use_groq=False))) for _ in range(2)]
    try:
        workers[0].start()
        assert started.wait(1)
        workers[1].start()
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(3)
    assert results == ["synthetic", "synthetic"]
    assert not any(worker.is_alive() for worker in workers)
    assert factory.call_count == 1


class IdleSocket:
    def __init__(self, blocked=False):
        self.state, self.close_code = State.OPEN, None
        self.blocked = blocked
        self.sent = []
        self.closed = 0
        self.receiving = asyncio.Event()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        self.receiving.set()
        if self.blocked:
            await asyncio.Event().wait()
        return json.dumps({"requestID": self.sent[-1]["requestID"]})

    async def close(self):
        self.closed += 1
        self.state, self.close_code = State.CLOSED, 1000


@pytest.mark.parametrize("state,code,expected", [
    (State.OPEN, None, True), (State.CLOSING, None, False),
    (State.CLOSED, 1000, False), (State.OPEN, 1000, False),
])
def test_idle_ws_health_uses_modern_state(monkeypatch, state, code, expected):
    monkeypatch.setattr(bridge, "_idle_ws", SimpleNamespace(state=state, close_code=code))
    assert bridge._idle_ws_ok() is expected


@pytest.mark.parametrize("cancel", [False, True])
def test_idle_interrupted_recv_discards_socket_and_reconnects_once(monkeypatch, cancel):
    async def exercise():
        old, new = IdleSocket(blocked=True), IdleSocket()
        monkeypatch.setattr(bridge, "_idle_ws", old)
        connect = AsyncMock(return_value=new)
        monkeypatch.setattr(bridge, "_idle_connect_ws", connect)
        real_wait_for = asyncio.wait_for
        timeouts = []

        async def fast_wait_for(awaitable, timeout):
            timeouts.append(timeout)
            return await real_wait_for(awaitable, min(timeout, 0.05))

        monkeypatch.setattr(bridge.asyncio, "wait_for", fast_wait_for)
        task = asyncio.create_task(bridge._idle_send(old, {"requestID": "old"}))
        await old.receiving.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
            await task
        assert old.closed == 1 and bridge._idle_ws is None
        assert 3.0 in timeouts
        results = await asyncio.gather(bridge._idle_reconnect(old), bridge._idle_reconnect(old))
        assert results == [new, new]
        connect.assert_awaited_once()
        with pytest.raises(bridge.websockets.exceptions.ConnectionClosed):
            await bridge._idle_send(old, {"requestID": "stale"})
        response = await bridge._idle_send(new, {"requestID": "new"})
        assert response["requestID"] == "new"
        assert len(old.sent) == 1

    asyncio.run(exercise())


def test_idle_motion_reconnect_backs_off_when_model_guard_refuses(monkeypatch):
    # MODEL GUARD: connect returns None at once; the worker recreates the
    # motion track every 0.2 s. Each recreated track must wait longer.
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    connect = AsyncMock(return_value=None)
    monkeypatch.setattr(bridge, "_idle_connect_ws", connect)
    monkeypatch.setattr(bridge, "_motion_run_state", lambda: "idle")
    monkeypatch.setitem(bridge.CONFIG, "idle_motion_sambung", True)

    async def exercise():
        for _ in range(8):
            await bridge._motion_track({"IdleMotion1": "synthetic-id"})

    asyncio.run(exercise())
    assert connect.await_count == 8
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_idle_reconnect_backoff_resets_after_successful_send(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    async def exercise():
        socket = IdleSocket()
        connect = AsyncMock(side_effect=[None, None, socket, None])
        monkeypatch.setattr(bridge, "_idle_connect_ws", connect)
        monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
        assert await bridge._idle_reconnect(None) is None
        assert await bridge._idle_reconnect(None) is None
        assert await bridge._idle_reconnect(None) is socket
        assert delays == [1, 2]
        await bridge._idle_send(socket, {"requestID": "ok"})
        assert bridge._idle_reconnect_backoff == 0
        await socket.close()
        assert await bridge._idle_reconnect(socket) is None
        assert delays == [1, 2]  # first retry after a good send is immediate

    asyncio.run(exercise())


def test_idle_motion_retries_failed_hotkey_lookup(monkeypatch):
    socket = object()
    sent = []

    async def sleep(delay):
        pass

    async def reconnect(ws):
        bridge._idle_ws = socket
        return socket

    async def send(ws, payload):
        if ws is None:
            raise bridge.websockets.exceptions.ConnectionClosed(None, None)
        sent.append(payload["data"]["hotkeyID"])
        return {"messageType": "HotkeyTriggerResponse"}

    discover = AsyncMock(side_effect=[{}, {"IdleMotion2": "id-2"}])
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    monkeypatch.setattr(bridge, "_idle_reconnect", reconnect)
    monkeypatch.setattr(bridge, "_idle_send", send)
    monkeypatch.setattr(bridge, "_idle_ws_ok", lambda: bridge._idle_ws is not None)
    monkeypatch.setattr(bridge, "_discover_motion_hotkey_ids", discover)
    monkeypatch.setattr(bridge, "_motion_run_state", lambda: "idle" if len(sent) < 2 else None)
    monkeypatch.setitem(bridge.CONFIG, "idle_motion_sambung", True)
    asyncio.run(bridge._motion_track({"IdleMotion1": "id-1"}))
    # The failed lookup did not kill the track: it was retried and used.
    assert discover.await_count == 2
    assert sent == ["id-2", "id-2"]


def test_groq_error_removes_temporary_wav_already_fixed(monkeypatch, tmp_path):
    paths = []
    make_temp = bridge.tempfile.NamedTemporaryFile

    def temporary(**kwargs):
        result = make_temp(dir=tmp_path, **kwargs)
        paths.append(Path(result.name))
        return result

    monkeypatch.setattr(bridge.tempfile, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(bridge.sf, "write", lambda *a: None)
    monkeypatch.setattr(bridge.arti_http_util, "groq_session", lambda: SimpleNamespace(
        post=Mock(side_effect=OSError("synthetic transport error"))))
    with pytest.raises(OSError, match="synthetic transport error"):
        bridge._groq_asr_with_deadline(np.zeros(160), 16000, "synthetic", {}, {}, 2)
    assert len(paths) == 1 and not paths[0].exists()
    assert not bridge._groq_asr_request_lock.locked()


def test_dsp_clips_overshoot_before_pcm_write_already_fixed(monkeypatch, tmp_path):
    monkeypatch.setattr(dsp, "aktif", lambda cfg: True)
    monkeypatch.setattr(dsp, "_lebarkan_range", lambda *a: np.array([-1.2, 0.5, 1.2]))
    audio = dsp.poles_suara(np.zeros(3), 16000, {"supertonic_range_factor": 1.4})
    wav = tmp_path / "synthetic.wav"
    bridge.sf.write(wav, audio, 16000, subtype="PCM_16")
    pcm, _ = bridge.sf.read(wav)
    assert np.all(np.abs(audio) <= 1)
    assert pcm[0] < -0.99 and pcm[-1] > 0.99


@pytest.mark.parametrize("kind", ["subtitle", "status"])
def test_subtitle_broadcast_snapshots_clients(monkeypatch, kind):
    sent = []
    clients = set()

    class Client:
        async def send(self, message):
            sent.append(json.loads(message)["type"])
            clients.clear()

    clients.update([Client(), Client()])
    monkeypatch.setattr(subtitles, "connected_clients", clients)
    if kind == "subtitle":
        asyncio.run(subtitles.broadcast_subtitle([], "synthetic"))
    else:
        asyncio.run(subtitles.broadcast_status("synthetic"))
    assert sent == [kind, kind]


def fake_turn(monkeypatch, reply="synthetic reply"):
    monkeypatch.setattr(bridge, "_prepare_turn_start", AsyncMock())
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: True)
    monkeypatch.setitem(bridge.CONFIG, "minecraft_mute_screen_curious", True)
    monkeypatch.setitem(bridge.CONFIG, "expression_nod_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "expression_emotion_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "expression_emosi_linger_sec", 0)
    monkeypatch.setitem(bridge.CONFIG, "expression_emosi_fade_sec", 0)
    monkeypatch.setattr(bridge, "mood_block_now", lambda: "")
    monkeypatch.setattr(bridge, "build_today_block", lambda: "")
    monkeypatch.setattr(bridge, "viewer_block_for", lambda _: "")
    monkeypatch.setattr(bridge, "_should_route_to_cursor", lambda _: False)
    monkeypatch.setattr(bridge, "_try_stardew_owner_generation", lambda _: (reply, True))
    monkeypatch.setattr(bridge.arti_voice_pipeline, "prepare_turn_context", AsyncMock(
        return_value=SimpleNamespace(formatted_history="", llm_system="system",
                                     prompt_content="synthetic", target_instruction="", rag_query="")))
    monkeypatch.setattr(bridge, "clean_ai_reply", lambda text: text)
    monkeypatch.setattr(bridge, "post_process_response", lambda text, _: text)
    monkeypatch.setattr(bridge, "_maybe_execute_reply_tags", lambda text, *a: text)
    monkeypatch.setattr(bridge.arti_expression_runtime, "parse_reply_emotion", lambda text: (text, "sedih"))
    monkeypatch.setattr(bridge.arti_expression_runtime, "resolve_turn_emotion", lambda *a: "sedih")
    monkeypatch.setattr(bridge.arti_expression_runtime, "audit_mood_exp_on_disk", lambda *a: {})
    monkeypatch.setattr(bridge, "_sentences_or_empty", lambda text: [])
    monkeypatch.setattr(bridge, "_speak_reply_chunks", AsyncMock())
    monkeypatch.setattr(bridge, "_media_playback_until", 0)
    monkeypatch.setattr(bridge, "add_to_history", Mock())
    monkeypatch.setattr(bridge, "_cermin_ke_chat_game", Mock())
    monkeypatch.setattr(bridge, "_schedule_post_answer_cleanup", Mock())
    monkeypatch.setattr(bridge, "last_arti_reply_text", "")
    monkeypatch.setattr(bridge, "hotkey_active", True)
    monkeypatch.setattr(bridge, "tts_stop_flag", False)
    monkeypatch.setattr(bridge, "_pending_turn_id", "synthetic-turn")
    monkeypatch.setattr(bridge.voice_listener_worker, "_last_tts_end", 0, raising=False)
    vts = SimpleNamespace(trigger_expression_state=AsyncMock(), send_expression=AsyncMock())
    monkeypatch.setattr(bridge, "vts", vts)
    return vts


@pytest.mark.parametrize("failure", ["history", "speech_cancel", "speaking"])
def test_failed_turn_finally_resets_mood_and_ptt(monkeypatch, failure):
    # Reuse the reactor harness that excludes all live startup effects.
    from tests.test_bridge_silent_death import _fake_reactor, _reactor

    _fake_reactor(monkeypatch, 1)
    monkeypatch.setitem(bridge.CONFIG, "curious_enabled", False)
    vts = fake_turn(monkeypatch)
    monkeypatch.setattr(bridge, "_maybe_auto_off_ptt", lambda: None)
    if failure == "history":
        bridge.add_to_history.side_effect = RuntimeError("synthetic history error")
    elif failure == "speech_cancel":
        bridge._speak_reply_chunks.side_effect = asyncio.CancelledError()
    else:
        async def speaking(*args):
            await vts.send_expression("ArtiSedih.exp3.json", True)
            raise RuntimeError("synthetic speaking error")
        monkeypatch.setattr(bridge.arti_expression_runtime, "apply_speaking", speaking)
    bridge.voice_trigger_queue.put(bridge.VoiceTrigger("synthetic input", "mic"))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_reactor()())
    assert not bridge.hotkey_active and bridge._pending_turn_id is None
    assert not bridge._brain_busy
    calls = [(call.args[0], call.args[1]) for call in vts.send_expression.call_args_list]
    assert ("ArtiSedih.exp3.json", True) in calls
    assert ("ArtiSedih.exp3.json", False) in calls
    states = [call.args[0] for call in vts.trigger_expression_state.call_args_list]
    assert states[-1] == "default" and states.count("default") == 1


def test_failed_turn_keeps_ptt_pressed_during_the_turn(monkeypatch):
    from tests.test_bridge_silent_death import _fake_reactor, _reactor

    _fake_reactor(monkeypatch, 1)
    monkeypatch.setitem(bridge.CONFIG, "curious_enabled", False)
    fake_turn(monkeypatch)
    monkeypatch.setattr(bridge, "_maybe_auto_off_ptt", lambda: None)

    async def speaking(*args):
        # Streamer presses PTT ON mid-turn (as _ptt_attention_pause does) and
        # a newer capture takes the pending id; then the turn fails.
        bridge.tts_stop_flag = True
        bridge._pending_turn_id = "newer-capture"
        raise RuntimeError("synthetic speaking error")

    monkeypatch.setattr(bridge.arti_expression_runtime, "apply_speaking", speaking)
    bridge.voice_trigger_queue.put(bridge.VoiceTrigger("synthetic input", "mic"))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_reactor()())
    assert bridge.hotkey_active is True
    assert bridge._pending_turn_id == "newer-capture"
    assert not bridge._brain_busy


@pytest.mark.parametrize("provider", ["gemini", "gemini_live", "groq", "groq_reasoning_retry"])
def test_direct_reply_providers_budget_covers_hard_cap(monkeypatch, provider):
    fake_turn(monkeypatch)
    monkeypatch.setattr(bridge, "_try_stardew_owner_generation", lambda _: (None, False))
    monkeypatch.setattr(bridge, "clean_ai_reply", lambda text: "")
    monkeypatch.setitem(bridge.CONFIG, "api_provider", "groq" if provider.startswith("groq") else provider)
    monkeypatch.setitem(bridge.CONFIG, "arti_reply_max_chars", 90)
    monkeypatch.setitem(bridge.CONFIG, "groq_api_key", "synthetic")
    monkeypatch.setitem(bridge.CONFIG, "gemini_api_key", "synthetic")
    monkeypatch.setitem(bridge.CONFIG, "groq_stream_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "smart_groq_routing", False)
    monkeypatch.setitem(bridge.CONFIG, "groq_models", ["unit-model", "unit-gpt-oss"])
    generate = AsyncMock(side_effect=RuntimeError("synthetic"))
    generate._groq_idx = 0
    monkeypatch.setattr(bridge, "generate_live_api_response", generate)
    payloads = []

    async def post(*args, **kwargs):
        payloads.append(json.loads(json.dumps(kwargs["json"])))
        status = 429 if provider == "groq_reasoning_retry" and len(payloads) == 1 else 200
        return SimpleNamespace(status_code=status, text="synthetic", json=lambda: {
            "candidates": [{"content": {"parts": [{"text": "synthetic"}]}}],
            "choices": [{"message": {"content": "synthetic"}}],
        })

    monkeypatch.setattr(bridge.arti_http_util, "post_in_thread", post)
    asyncio.run(bridge._handle_voice_trigger(bridge.VoiceTrigger("synthetic input", "mic"), [], "system"))
    assert payloads
    # 2026-10-23 (no mid-thought cuts): the old flat 150/200/512 budgets sat
    # below the reply hard cap (~900 ch), so long replies were cut by tokens
    # mid-sentence. Budgets now cover the hard cap; reasoning models get
    # thinking headroom on top, never below the old floors. Still independent
    # of arti_reply_max_chars (set to 90 above).
    reply = rp.tokens_for_chars(bridge.CONFIG["arti_reply_hard_cap_chars"])
    thinking = reply + bridge.REPLY_THINKING_HEADROOM_TOKENS
    if provider.startswith("gemini"):
        # gemini-2.5-flash counts thinking tokens here; never below the old 200.
        assert payloads[0]["generationConfig"]["maxOutputTokens"] == max(200, thinking)
    else:
        assert payloads[0]["max_tokens"] == reply > 150
        if provider.endswith("retry"):
            # gpt-oss reasoning shares this budget
            assert payloads[1]["max_tokens"] == max(512, thinking)


def _direct_provider_turn(monkeypatch, provider):
    """Harness above, but records the raw reply handed to clean_ai_reply."""
    fake_turn(monkeypatch)
    seen = []
    monkeypatch.setattr(bridge, "_try_stardew_owner_generation", lambda _: (None, False))
    monkeypatch.setattr(bridge, "clean_ai_reply", lambda text: (seen.append(text), "")[1])
    monkeypatch.setitem(bridge.CONFIG, "api_provider", provider)
    monkeypatch.setitem(bridge.CONFIG, "groq_api_key", "synthetic")
    monkeypatch.setitem(bridge.CONFIG, "gemini_api_key", "synthetic")
    monkeypatch.setitem(bridge.CONFIG, "smart_groq_routing", False)
    monkeypatch.setitem(bridge.CONFIG, "groq_models", ["unit-model"])
    generate = AsyncMock(side_effect=RuntimeError("synthetic"))
    generate._groq_idx = 0
    monkeypatch.setattr(bridge, "generate_live_api_response", generate)
    return seen


@pytest.mark.parametrize("finish,expected", [
    ("MAX_TOKENS", "Ini jawaban utuh."),
    ("STOP", "Ini jawaban utuh. Dan ini kalimat yang kepo"),
    (None, "Ini jawaban utuh. Dan ini kalimat yang kepo"),
])
def test_gemini_http_finish_reason_drops_partial_sentence(monkeypatch, finish, expected):
    seen = _direct_provider_turn(monkeypatch, "gemini")
    candidate = {"content": {"parts": [{"text": "Ini jawaban utuh. Dan ini kalimat yang kepo"}]}}
    if finish:
        candidate["finishReason"] = finish

    async def post(*args, **kwargs):
        return SimpleNamespace(status_code=200, text="", json=lambda: {"candidates": [candidate]})

    monkeypatch.setattr(bridge.arti_http_util, "post_in_thread", post)
    asyncio.run(bridge._handle_voice_trigger(bridge.VoiceTrigger("synthetic input", "mic"), [], "system"))
    assert seen == [expected]


@pytest.mark.parametrize("finish,expected", [
    ("length", "Ini jawaban utuh."),
    ("stop", "Ini jawaban utuh. Dan ini kalimat yang kepo"),
    # No finish_reason and far below the budget: the heuristic stays quiet.
    (None, "Ini jawaban utuh. Dan ini kalimat yang kepo"),
])
def test_groq_stream_finish_reason_drops_partial_sentence(monkeypatch, finish, expected):
    seen = _direct_provider_turn(monkeypatch, "groq")
    monkeypatch.setitem(bridge.CONFIG, "groq_stream_enabled", True)
    chunks = [
        {"choices": [{"delta": {"content": "Ini jawaban utuh. "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "Dan ini kalimat yang kepo"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": finish}]},
    ]
    lines = [f"data: {json.dumps(c)}".encode() for c in chunks] + [b"data: [DONE]"]
    resp = SimpleNamespace(status_code=200, iter_lines=lambda decode_unicode=False: iter(lines))
    monkeypatch.setattr(bridge.arti_http_util, "groq_session",
                        lambda: SimpleNamespace(post=lambda *a, **kw: resp))

    async def no_post(*args, **kwargs):
        raise AssertionError("stream reply must not fall back to non-stream")

    monkeypatch.setattr(bridge.arti_http_util, "post_in_thread", no_post)
    asyncio.run(bridge._handle_voice_trigger(bridge.VoiceTrigger("synthetic input", "mic"), [], "system"))
    assert seen == [expected]


def test_stream_trim_prefers_finish_reason_over_heuristic():
    partial = "Ini jawaban utuh. Dan ini kalimat yang kepo"
    # gpt-oss: reasoning shares the budget, content stops far below the
    # heuristic threshold — only the reported finish_reason catches it.
    assert bridge._trim_stream_reply(partial, "length", 620, "unit") == "Ini jawaban utuh."
    assert bridge._trim_stream_reply(partial, "stop", 10, "unit") == partial
    # No finish_reason: heuristic fallback near the budget only.
    assert bridge._trim_stream_reply(partial, None, 620, "unit") == partial
    assert bridge._trim_stream_reply(partial, None, 10, "unit") == "Ini jawaban utuh."


def test_gemini_live_websocket_reply_budget(monkeypatch):
    sent = []
    responses = iter([{"setupComplete": {}}, {"serverContent": {"turnComplete": True}}])

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, raw):
            sent.append(json.loads(raw))

        async def recv(self):
            return json.dumps(next(responses))

    monkeypatch.setitem(bridge.CONFIG, "gemini_api_key", "synthetic")
    monkeypatch.setitem(bridge.CONFIG, "arti_reply_max_chars", 90)
    monkeypatch.setattr(bridge.websockets, "connect", lambda *a, **kw: Socket())
    assert asyncio.run(bridge.generate_live_api_response("synthetic", "system")) == ""
    assert sent[0]["setup"]["generation_config"]["max_output_tokens"] == 200


def test_stardew_runtime_has_only_startup_construction_paths():
    """Pin the absence of a mid-session recreation path, not just helper naming."""
    tree = ast.parse(Path(bridge.__file__).read_text(encoding="utf-8"))
    creators, callers = [], []
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_stardew_runtime"
                for target in node.targets
            ) and isinstance(node.value, ast.Call):
                creators.append((function.name, node.value.func.attr))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "_start_stardew_runtime_if_enabled":
                    callers.append(function.name)
    assert creators == [
        ("_start_stardew_runtime_if_enabled", "start_runtime"),
        ("_start_stardew_runtime_if_enabled", "construct_if_enabled"),
    ]
    assert callers == ["main_loop"]
    main = next(node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "main_loop")
    startup = next(index for index, node in enumerate(main.body)
                   if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and isinstance(node.value.func, ast.Name)
                   and node.value.func.id == "_start_stardew_runtime_if_enabled")
    loop = next(index for index, node in enumerate(main.body) if isinstance(node, ast.While))
    assert startup < loop


@pytest.mark.parametrize("trial_start", [False, True])
def test_stardew_startup_binds_help_and_shutdown_clears_runtime(monkeypatch, trial_start):
    sink = Mock()
    runtime = SimpleNamespace(set_help_sink=sink, stop=Mock())
    relay = SimpleNamespace(set_runtime=Mock(), submit=Mock(), config={"live7_help_enabled": True})
    trial = SimpleNamespace(start_runtime=Mock(return_value=runtime), stop=Mock())
    monkeypatch.setattr(bridge, "_stardew_runtime", None)
    monkeypatch.setattr(bridge, "_live7_help_relay", relay)
    monkeypatch.setattr(bridge, "_stardew_bridge_trial", trial if trial_start else None)
    monkeypatch.setitem(bridge.CONFIG, "stardew_runtime_enabled", True)
    import arti_stardew_runtime
    monkeypatch.setattr(arti_stardew_runtime, "construct_if_enabled", lambda cfg: runtime)
    bridge._start_stardew_runtime_if_enabled()
    sink.assert_called_once_with(relay.submit)
    relay.set_runtime.assert_called_once_with(runtime)
    bridge._stop_stardew_runtime()
    relay.set_runtime.assert_called_with(None)
    assert bridge._stardew_runtime is None
    (trial.stop if trial_start else runtime.stop).assert_called_once()
