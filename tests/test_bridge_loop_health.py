"""Batch 8: blocked fakes only; no devices, providers or game startup."""

import asyncio
import io
import json
import threading
import time

import pytest

import arti_executors
import arti_http_util
import session_transcript as transcript
from arti_buffered_writer import BatchedWriter


@pytest.mark.parametrize("pool_name", ["background", "rag", "vision"])
def test_abandoned_background_work_cannot_starve_voice_http(monkeypatch, pool_name):
    release = threading.Event()
    started = threading.Event()
    background = arti_executors.WorkPool("test-background", 1)
    voice = arti_executors.WorkPool("test-llm", 1)
    monkeypatch.setitem(arti_executors._POOLS, pool_name, background)
    # Provider HTTP (post_in_thread) runs on the dedicated LLM pool.
    monkeypatch.setitem(arti_executors._POOLS, "llm", voice)
    calls = []

    def blocked():
        started.set()
        release.wait(5)

    class Session:
        def post(self, url, **kwargs):
            calls.append(threading.current_thread().name)
            return "response"

    async def exercise():
        task = asyncio.create_task(arti_executors.run(pool_name, blocked))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Cancellation did not release the still-running thread's slot.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(arti_executors.run(pool_name, lambda: None), 0.03)
        assert await asyncio.wait_for(arti_http_util.post_in_thread(Session(), "fake"), 0.5) == "response"

    try:
        asyncio.run(exercise())
        assert calls and calls[0].startswith("arti-test-llm")
    finally:
        release.set()
        background.shutdown()
        voice.shutdown()


def test_abandoned_llm_calls_cannot_starve_tts_voice_pool(monkeypatch):
    """A reply abandoned by the turn deadline keeps its LLM thread; TTS must not wait on it."""
    release = threading.Event()
    llm = arti_executors.WorkPool("test-llm2", 1)
    voice = arti_executors.WorkPool("test-voice2", 1)
    monkeypatch.setitem(arti_executors._POOLS, "llm", llm)
    monkeypatch.setitem(arti_executors._POOLS, "voice", voice)

    async def exercise():
        stuck = asyncio.create_task(arti_executors.run("llm", release.wait, 5))
        await asyncio.sleep(0.02)
        stuck.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stuck
        # LLM pool is still occupied by the abandoned thread...
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(arti_executors.run("llm", lambda: None), 0.03)
        # ...but the voice pool (TTS playback wait, Supertone I/O) is free.
        assert await asyncio.wait_for(arti_executors.run("voice", lambda: "played"), 0.5) == "played"

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        llm.shutdown()
        voice.shutdown()


@pytest.mark.parametrize("interval", [0.05, 60.0])
def test_transcript_fsync_is_batched_off_loop_and_final_close_drains(monkeypatch, tmp_path, interval):
    transcript.close_transcript_file()
    monkeypatch.setattr(transcript, "_ROOT", tmp_path)
    synced = threading.Event()
    sync_threads = []

    def fsync(fd):
        sync_threads.append(threading.get_ident())
        synced.set()

    monkeypatch.setattr("arti_buffered_writer.os.fsync", fsync)
    config = {"stream_session_id": "unit", "session_log_keep_n": 0,
              "transcript_flush_interval_sec": interval, "transcript_flush_lines": 32}
    try:
        transcript.init_session_artifacts(config)
        for number in range(3):
            transcript.append_transcript({"number": number}, config)
        assert synced.wait(1.5)  # capped at 1 s; no later write needed for the idle tail
        assert len(sync_threads) == 1
        assert all(ident != threading.get_ident() for ident in sync_threads)
        transcript.append_transcript({"final": True}, config)
        transcript.close_transcript_file()
        rows = [json.loads(line) for line in (tmp_path / "transcripts/unit.jsonl").read_text().splitlines()]
        assert rows[-1] == {"final": True}
        assert len(sync_threads) == 2
    finally:
        transcript.close_transcript_file()


def test_line_threshold_and_shutdown_flush_without_waiting_for_interval():
    class Sink(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushed = threading.Event()
            self.final = ""

        def flush(self):
            self.flushed.set()

        def close(self):
            self.final = self.getvalue()
            super().close()

    sink = Sink()
    writer = BatchedWriter(sink, interval=1.0, lines=3)
    try:
        writer.write("one\ntwo\nthree\n")
        assert sink.flushed.wait(0.5)
        writer.write("tail\n")
    finally:
        writer.close()
    assert sink.final == "one\ntwo\nthree\ntail\n"


def test_slow_disk_write_does_not_block_producer():
    entered, release = threading.Event(), threading.Event()

    class Sink(io.StringIO):
        def write(self, data):
            entered.set()
            release.wait(5)
            return super().write(data)

    writer = BatchedWriter(Sink(), lines=1)
    try:
        writer.write("first\n")
        assert entered.wait(0.5)
        started = time.monotonic()
        writer.write("second\n")
        assert time.monotonic() - started < 0.2
    finally:
        release.set()
        writer.close()


def test_vault_lock_wait_from_sync_history_callback_keeps_loop_live(monkeypatch, tmp_path):
    import arti_memory_quality
    import arti_bridge as bridge

    lock = tmp_path / "db.lock"
    lock.write_text("unit lock")
    entered, release = threading.Event(), threading.Event()
    saved = []
    monkeypatch.setattr(bridge, "LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "LOCK_FILE", str(lock))
    monkeypatch.setattr(arti_memory_quality, "append_learning", lambda *a, **kw: saved.append(True))

    def lock_sleep(_delay):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(bridge.time, "sleep", lock_sleep)

    async def exercise():
        task = bridge.save_long_term_memory("unit fact")
        assert isinstance(task, asyncio.Task)
        while not entered.is_set():
            await asyncio.sleep(0.001)
        # The reactor progressed while the real lock-file loop was waiting.
        lock.unlink()
        release.set()
        await asyncio.wait_for(task, 0.5)

    try:
        asyncio.run(exercise())
        assert saved == [True]
        assert not lock.exists()
    finally:
        release.set()


def test_cursor_prewarm_session_lock_does_not_block_loop(monkeypatch):
    import arti_cursor_agent
    import arti_bridge as bridge

    entered, release = threading.Event(), threading.Event()
    threads = []

    def prewarm(_config):
        threads.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return False

    monkeypatch.setattr(arti_cursor_agent, "prewarm", prewarm)
    monkeypatch.setattr(bridge.arti_agy_agent, "prewarm", lambda cfg: None)

    async def exercise():
        task = asyncio.create_task(bridge._cursor_reply_with_fallback(
            "system", "prompt", "unit", config={"cursor_timeout_sec": 1}))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        assert not task.done()
        assert threads[0] != threading.get_ident()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_tee_preserves_timestamps_without_flushing_each_print():
    import arti_bridge as bridge

    class Sink(io.StringIO):
        flushes = 0

        def flush(self):
            self.flushes += 1

    sink = Sink()
    tee = bridge._TeeOutput(io.StringIO(), sink)
    tee.write("one\ntwo\n")
    tee.write("three\n")
    assert sink.flushes == 0
    tee.flush()
    assert sink.flushes == 1
    assert len(sink.getvalue().splitlines()) == 3


def test_pool_preserves_context_and_releases_slot_after_error():
    from contextvars import ContextVar

    context = ContextVar("unit", default="missing")
    pool = arti_executors.WorkPool("test-context", 1)

    def fail():
        assert context.get() == "unit-context"
        raise ValueError("unit error")

    async def exercise():
        context.set("unit-context")
        with pytest.raises(ValueError):
            await pool.run(fail)
        assert await asyncio.wait_for(pool.run(context.get), 0.5) == "unit-context"

    try:
        asyncio.run(exercise())
    finally:
        pool.shutdown()


def test_clean_loop_exit_drains_memory_saves(monkeypatch):
    import arti_memory_quality
    import arti_bridge as bridge

    release = threading.Event()
    saved = []

    def append(*args, **kwargs):
        release.wait(5)
        saved.append(True)

    async def main():
        bridge.save_long_term_memory("unit fact")
        asyncio.get_running_loop().call_later(0.03, release.set)

    monkeypatch.setattr(bridge, "main_loop", main)
    monkeypatch.setattr(bridge, "wait_and_acquire_lock", lambda *args: True)
    monkeypatch.setattr(bridge, "release_vault_lock", lambda: None)
    monkeypatch.setattr(arti_memory_quality, "append_learning", append)
    try:
        asyncio.run(bridge._main_loop_with_memory_drain())
        assert saved == [True]
        assert not bridge._memory_save_tasks
    finally:
        release.set()
