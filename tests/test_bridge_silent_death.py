"""Silent-death recovery with fake queues, audio streams and VTS only."""

import ast
import asyncio
import inspect
import io
import queue
import re
import threading
from types import SimpleNamespace

import pytest

import arti_bridge as H


def _reactor():
    """Execute the real main-loop tail, excluding all device/provider startup."""
    tree = ast.parse(inspect.getsource(H.main_loop))
    fn = tree.body[0]
    start = next(i for i, node in enumerate(fn.body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "_proactive_dormant_logged"
                         for t in node.targets))
    fn.body = ([n for n in fn.body[:start] if isinstance(n, ast.Global)]
               + ast.parse("memories = []\ndynamic_system_prompt = ''").body
               + fn.body[start:])
    namespace = {}
    exec(compile(ast.fix_missing_locations(tree), H.__file__, "exec"), H.__dict__, namespace)
    return namespace["main_loop"]


def _fake_reactor(monkeypatch, ticks):
    calls = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == 0.1:
            calls.append(delay)
            if len(calls) > ticks:
                raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(H.asyncio, "sleep", sleep)
    monkeypatch.setattr(H, "_mc_runner_active", lambda: False)
    monkeypatch.setattr(H, "_session_mode", lambda: "chat")
    monkeypatch.setattr(H.arti_session_mode, "mode_policy",
                        lambda *a: {"screen_curious_allowed": True})
    monkeypatch.setattr(H.arti_curious, "is_dormant", lambda *a, **kw: False)
    monkeypatch.setattr(H, "is_vision_active", lambda: True)
    monkeypatch.setattr(H, "_afk_armed_ts", 0)
    monkeypatch.setattr(H, "_media_playback_until", 0)
    monkeypatch.setattr(H, "_init_provider_fail_until", 0)
    monkeypatch.setattr(H, "_brain_busy", False)
    monkeypatch.setattr(H, "_brain_busy_since", 0)
    monkeypatch.setattr(H, "tts_is_playing", False)
    monkeypatch.setitem(H.CONFIG, "curious_enabled", True)
    monkeypatch.setitem(H.CONFIG, "initiative_enabled", False)
    monkeypatch.setitem(H.CONFIG, "voice_queue_enabled", False)
    monkeypatch.setattr(H, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(H, "_game_reaction_expired", lambda trigger: False)
    monkeypatch.setattr(H, "_schedule_post_answer_cleanup", lambda: None)
    monkeypatch.setattr(H, "_finish_stardew_owner_voice", lambda trigger: None)
    return calls


def test_curious_exception_does_not_end_reactor(monkeypatch, capsys):
    ticks = _fake_reactor(monkeypatch, 3)
    curious_calls = []
    handled = []

    def should_fire(*args, **kwargs):
        curious_calls.append(True)
        if len(curious_calls) <= 2:
            raise ValueError("curious fault")
        H.voice_trigger_queue.put(("halo", "mic", None))
        return False

    async def handle(*args):
        handled.append(True)

    monkeypatch.setattr(H.arti_curious, "should_fire", should_fire)
    monkeypatch.setattr(H, "_handle_voice_trigger", handle)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_reactor()())
    assert len(ticks) == 4 and handled == [True]
    out = capsys.readouterr().out
    assert out.count("Traceback (most recent call last)") == 1
    assert "count=2" in out


@pytest.mark.parametrize("vts_fails", [False, True])
def test_repeated_turn_failures_escalate_once_and_vts_cannot_kill_loop(
    monkeypatch, capsys, vts_fails
):
    _fake_reactor(monkeypatch, 7)
    monkeypatch.setitem(H.CONFIG, "curious_enabled", False)
    defaults = []

    async def default(state):
        defaults.append(state)
        if vts_fails:
            raise OSError("VTS offline")

    async def handle(*args):
        H.tts_is_playing = True
        H._tts_started_ts = 123
        raise ValueError("turn fault")

    monkeypatch.setattr(H, "vts", SimpleNamespace(trigger_expression_state=default))
    monkeypatch.setattr(H, "_handle_voice_trigger", handle)
    monkeypatch.setattr(H, "_tts_started_ts", 0)
    monkeypatch.setitem(H.CONFIG, "expression_emotion_enabled", False)
    for _ in range(7):
        H.voice_trigger_queue.put(("halo", "mic", None))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_reactor()())
    assert not H._brain_busy and H._brain_busy_since == 0
    assert not H.tts_is_playing and H._tts_started_ts == 0
    assert defaults == ["default"] * 7
    out = capsys.readouterr().out
    assert out.count("!!! BRIDGE TURN FAILURE") == 1
    assert out.count("Traceback (most recent call last)") == (2 if vts_fails else 1)


def test_success_breaks_consecutive_turn_failure_streak(monkeypatch, capsys):
    _fake_reactor(monkeypatch, 9)
    monkeypatch.setitem(H.CONFIG, "curious_enabled", False)
    turns = []

    async def handle(*args):
        turns.append(True)
        if len(turns) != 5:
            raise ValueError("turn fault")

    async def default(state):
        pass

    monkeypatch.setattr(H, "vts", SimpleNamespace(trigger_expression_state=default))
    monkeypatch.setattr(H, "_handle_voice_trigger", handle)
    for _ in range(9):
        H.voice_trigger_queue.put(("halo", "mic", None))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_reactor()())
    assert len(turns) == 9
    assert "!!! BRIDGE TURN FAILURE" not in capsys.readouterr().out


@pytest.mark.parametrize("path", ["wav", "edge", "reflex"])
def test_stalled_audio_finishes_and_releases_gate(monkeypatch, capsys, path):
    stopped = []
    never = threading.Event()
    clock = [0.0]

    def stop():
        assert H._sd_gerbang.locked()
        stopped.append(clock[0])

    monkeypatch.setattr(H, "sd", SimpleNamespace(
        play=lambda *a, **kw: None, wait=lambda: never.wait(), stop=stop,
        get_stream=lambda: SimpleNamespace(active=True, closed=False)))
    monkeypatch.setattr(H, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        time=H.time.time, perf_counter=H.time.perf_counter))
    monkeypatch.setattr(H.sf, "read", lambda _: (H.np.zeros(48), 48000))
    monkeypatch.setattr(H, "resample_audio", lambda data, sr, target: (data, sr))
    monkeypatch.setattr(H, "_audio_lock", threading.Lock())
    monkeypatch.setattr(H, "_sd_gerbang", threading.Lock())
    monkeypatch.setattr(H, "tts_is_playing", False)
    monkeypatch.setattr(H, "_reflex_playing", False)
    monkeypatch.setattr(H, "_brain_busy", False)
    monkeypatch.setattr(H, "vts", SimpleNamespace(trigger_expression_state=lambda _: asyncio.sleep(0)))
    monkeypatch.setattr(H, "main_event_loop", object())
    monkeypatch.setattr(H.asyncio, "run_coroutine_threadsafe", lambda coro, loop: coro.close())
    monkeypatch.setattr(H.subtitle_runtime, "enabled", False)
    monkeypatch.setattr(H, "_reflex_note", "")
    monkeypatch.setattr(H, "_reflex_note_ts", 0)
    engine = H.TTSEngine.__new__(H.TTSEngine)
    engine.device_id = 1
    monkeypatch.setattr(H, "tts", engine)

    class Communicate:
        def __init__(self, *args):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"fake"}

    monkeypatch.setattr(H.edge_tts, "Communicate", Communicate)
    if path == "wav":
        asyncio.run(asyncio.wait_for(engine._play_wav("fake", "halo", [], False), 1))
    elif path == "edge":
        asyncio.run(asyncio.wait_for(engine._speak_edge_tts("halo"), 1))
    else:
        H._reflex_worker("fake", "test", "Aduh!", "neutral")
    assert len(stopped) == 1 and 5.001 <= stopped[0] <= 5.051
    assert not H.tts_is_playing and not H._reflex_playing
    assert not H._audio_lock.locked() and not H._sd_gerbang.locked()
    assert "playback watchdog" in capsys.readouterr().out


@pytest.mark.parametrize("closed", [False, True])
def test_finished_or_interrupted_playback_closes_without_watchdog(monkeypatch, closed):
    closes = []

    def close():
        assert H._sd_gerbang.locked()
        closes.append(True)

    monkeypatch.setattr(H, "sd", SimpleNamespace(
        get_stream=lambda: SimpleNamespace(active=False, closed=closed, close=close)))
    H._wait_for_playback(1.0)
    assert closes == [True]


@pytest.mark.parametrize("failure_at", ["construct", "enter"])
def test_mic_open_retries_twice_then_works(monkeypatch, capsys, failure_at):
    attempts, delays, closed = [], [], []

    class Stream:
        def __enter__(self):
            if failure_at == "enter":
                attempts.append(True)
                if len(attempts) < 3:
                    raise OSError("mic unplugged")
            return self

        def __exit__(self, *args):
            closed.append(True)

    def factory(**kw):
        if failure_at == "construct":
            attempts.append(True)
            if len(attempts) < 3:
                raise OSError("mic unplugged")
        return Stream()

    monkeypatch.setattr(H.sd, "InputStream", factory)
    monkeypatch.setattr(H, "_telinga_dimatikan", False)
    monkeypatch.setattr(H, "resolve_asr_input_device", lambda: (None, "fake mic"))
    monkeypatch.setattr(H, "_asr_mic_id", None)
    monkeypatch.setattr(H, "_asr_mic_name", "fake mic")
    monkeypatch.setattr(H.time, "sleep", delays.append)
    with H._asr_input_stream({}) as stream:
        assert isinstance(stream, Stream)
    assert len(attempts) == 3 and closed == [True]
    assert delays == [0.5, 1.0]
    assert "ASR ERROR" in capsys.readouterr().out


@pytest.mark.parametrize("muted", [False, True])
def test_mic_callback_watchdog_reopens_stalled_stream(monkeypatch, capsys, muted):
    opens = []
    clock = [0.0]

    class Stream:
        def __init__(self, **kw):
            self.callback = kw["callback"]

        def __enter__(self):
            opens.append(clock[0])
            if len(opens) == 3:
                H._telinga_dimatikan = True
            return self

        def __exit__(self, *args):
            pass

    class EmptyAudio:
        # Stands in for the bounded AudioChunkQueue the listener now builds.
        def __init__(self, max_chunks=None):
            pass

        def empty(self):
            return True

        def put_latest(self, data):
            pass

        def report_drops(self):
            pass

        def get(self, timeout):
            clock[0] += 1
            if muted and clock[0] < 10:
                # Real callbacks still arrive while the speaking gate drops data.
                callbacks[-1](H.np.zeros((1, 1)), 1, None, None)
            raise queue.Empty

    callbacks = []

    def factory(**kw):
        callbacks.append(kw["callback"])
        return Stream(**kw)

    monkeypatch.setattr(H.sd, "InputStream", factory)
    monkeypatch.setattr(H, "AudioChunkQueue", EmptyAudio)
    monkeypatch.setattr(H, "resolve_asr_input_device", lambda: (None, "fake mic"))
    for name, value in [("_telinga_dimatikan", False), ("_asr_restart_requested", False),
                        ("tts_is_playing", muted), ("_reflex_playing", False),
                        ("_asr_mic_id", None), ("_asr_mic_name", "fake mic")]:
        monkeypatch.setattr(H, name, value)
    monkeypatch.setattr(H.time, "time", lambda: clock[0])
    monkeypatch.setattr(H.time, "monotonic", lambda: clock[0])

    def sleep(_):
        if muted and clock[0] >= 10:
            H.tts_is_playing = False

    # End muting after observing healthy callbacks, then let the stream stall.
    original_get = EmptyAudio.get

    def get(self, timeout):
        if clock[0] >= 10:
            H.tts_is_playing = False
        return original_get(self, timeout)

    monkeypatch.setattr(EmptyAudio, "get", get)
    monkeypatch.setattr(H.time, "sleep", sleep)
    H.voice_listener_worker()
    assert len(opens) == 3  # calibration, stalled stream, replacement
    if muted:
        assert opens[2] >= 14  # healthy callbacks during muting did not trigger it
    assert "ASR WATCHDOG" in capsys.readouterr().out


def test_tee_timestamps_split_and_multiline_writes_and_guards_flush():
    class BrokenFlush(io.StringIO):
        def flush(self):
            raise OSError("console closed")

    console, log = BrokenFlush(), BrokenFlush()
    tee = H._TeeOutput(console, log)
    tee.write("first")
    tee.write(" continued\nsecond\n\nthird")
    tee.write("\n")
    tee.flush()
    expected = ["first continued", "second", "", "third"]
    for output in (console.getvalue(), log.getvalue()):
        lines = output.splitlines()
        assert len(lines) == len(expected)
        for line, text in zip(lines, expected):
            assert re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{3} " + re.escape(text), line)
