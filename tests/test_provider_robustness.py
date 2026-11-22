"""Focused provider timeout and telemetry regressions; all provider calls are fake."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import arti_api_telemetry as telemetry
import arti_scouter_client
import arti_vision_client
import arti_bridge as bridge


def test_llm_turn_deadline_cancels_task():
    async def _run():
        cancelled = asyncio.Event()

        async def _slow_reply():
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(_slow_reply())
        assert await bridge._await_llm_deadline(task, 0.01) is False
        assert cancelled.is_set()

    asyncio.run(_run())
    assert bridge.CONFIG["turn_llm_deadline_sec"] == 25.0


def test_groq_asr_wall_deadline_returns_control(monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    calls = []

    class _SlowSession:
        def post(self, *args, **kwargs):
            calls.append(1)
            release.wait(2)
            finished.set()
            return SimpleNamespace(status_code=200, text="{}", json=lambda: {"text": "late"})

    monkeypatch.setattr(bridge.arti_http_util, "groq_session", lambda: _SlowSession())
    monkeypatch.setattr(bridge.sf, "write", lambda *args, **kwargs: None)
    try:
        with pytest.raises(TimeoutError, match="wall-clock deadline"):
            bridge._groq_asr_with_deadline(
                [0.0], 16000, "https://fake.invalid/asr", {}, {"model": "fake"}, 0.01
            )
        with pytest.raises(TimeoutError, match="previous Groq ASR request"):
            bridge._groq_asr_with_deadline(
                [0.0], 16000, "https://fake.invalid/asr", {}, {"model": "fake"}, 0.01
            )
        assert calls == [1]
    finally:
        release.set()
    assert finished.wait(1)
    assert bridge._groq_asr_request_lock.acquire(timeout=1)
    bridge._groq_asr_request_lock.release()
    assert bridge.CONFIG["groq_asr_deadline_sec"] == 10.0


def test_groq_asr_deadline_uses_local_fallback(monkeypatch):
    class _LocalWhisper:
        def transcribe(self, *args, **kwargs):
            return [SimpleNamespace(text="lokal berjalan")], None

    monkeypatch.setitem(bridge.CONFIG, "groq_api_key", "gsk_TEST_FAKE_NO_SECRET")
    monkeypatch.setitem(bridge.CONFIG, "groq_asr_deadline_sec", 0.01)
    monkeypatch.setattr(bridge.transcribe_audio, "_widx", 0, raising=False)
    monkeypatch.setattr(bridge, "_groq_asr_with_deadline", lambda *a, **k: (_ for _ in ()).throw(
        TimeoutError("deadline")
    ))
    monkeypatch.setattr(bridge, "local_whisper_model", _LocalWhisper())

    assert bridge.transcribe_audio([0.0] * 160, 16000, use_groq=True) == "lokal berjalan"


@pytest.mark.parametrize("client", ["vision", "scouter"])
def test_cursor_failure_telemetry_records_error_reason(client, monkeypatch):
    import arti_cursor_agent as cursor

    records = []
    monkeypatch.setattr(telemetry, "record_call", lambda **kwargs: records.append(kwargs))
    monkeypatch.setattr(cursor, "send_task", lambda *args, **kwargs: SimpleNamespace(
        ok=False, reason="timeout", latency_ms=321, model="composer-2.5", text=""
    ))
    monkeypatch.setattr(cursor, "resolve_role_model", lambda *args: ("composer-2.5", "high"))

    with pytest.raises(RuntimeError, match="timeout"):
        if client == "vision":
            arti_vision_client._call_cursor("prompt", "aA==", {})
        else:
            arti_scouter_client._call_cursor("prompt", {})

    assert len(records) == 1
    assert records[0]["ok"] is False
    assert records[0]["latency_ms"] == 321
    assert records[0]["extra"] == {"error_class": "timeout"}

