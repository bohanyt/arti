"""Desktop hearing cannot occupy the microphone ASR lane or start costly local work."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import ast
import os
import queue
import tempfile
import time
from pathlib import Path
from types import ModuleType
import pytest
import arti_voice_pipeline

# Execute the production ASR functions without importing Windows/audio startup.
# These tests exercise real threads, locks, deadlines and fallback routing.
bridge = ModuleType("asr_under_test")
bridge.__dict__.update(threading=threading, queue=queue, os=os, time=time,
    tempfile=tempfile, CONFIG={}, local_whisper_model=None,
    _groq_asr_request_lock=threading.Lock(),
    _desktop_groq_keys=lambda: [], _get_local_whisper_model=lambda: None,
    sf=SimpleNamespace(write=lambda *a: None),
    arti_voice_pipeline=arti_voice_pipeline,
    arti_http_util=SimpleNamespace(groq_session=lambda: None))
source = Path(__file__).resolve().parents[1] / "arti_bridge.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
functions = {"_groq_asr_with_deadline", "transcribe_audio", "_desktop_transcribe", "_desktop_groq_keys"}
locks = {"_groq_asr_request_lock", "_groq_asr_desktop_request_lock"}
nodes = [n for n in tree.body if
         (isinstance(n, ast.FunctionDef) and n.name in functions) or
         (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in locks
                                           for t in n.targets))]
assert len(nodes) == len(functions) + len(locks)
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), bridge.__dict__)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(bridge, "CONFIG", dict(bridge.CONFIG,
        groq_api_key="gsk_SYNTHETIC_MIC", groq_asr_deadline_sec=0.05,
        desktop_audio_local_fallback_enabled=False))
    monkeypatch.setattr(bridge, "_groq_asr_request_lock", threading.Lock())
    monkeypatch.setattr(bridge, "_groq_asr_desktop_request_lock", threading.Lock())
    monkeypatch.setattr(bridge, "_desktop_groq_keys", lambda: ["gsk_SYNTHETIC_DESKTOP"])
    monkeypatch.setattr(bridge, "_get_local_whisper_model",
        Mock(side_effect=AssertionError("unexpected local inference")))


def test_timed_out_desktop_request_cannot_block_mic_or_spawn_another(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    desktop_calls = []
    response = SimpleNamespace(status_code=200, text="", json=lambda: {"text": "cloud"})

    def post(url, **kwargs):
        if kwargs["headers"]["Authorization"] == "Bearer gsk_SYNTHETIC_DESKTOP":
            desktop_calls.append(True)
            entered.set()
            assert release.wait(3)
        return response

    # Keep the real deadline thread and temp-file lifecycle, without network/audio.
    monkeypatch.setattr(bridge.arti_http_util, "groq_session", lambda: SimpleNamespace(post=post))
    monkeypatch.setattr(bridge.sf, "write", lambda *a: None)
    # Completion is checked via the lane lock below after releasing the HTTP worker.
    audio = [0.0] * 160
    try:
        assert bridge._desktop_transcribe(audio) == ""
        assert entered.is_set()
        assert bridge._groq_asr_desktop_request_lock.locked()
        assert bridge.transcribe_audio(audio) == "cloud"
        assert not bridge._groq_asr_request_lock.locked()
        assert bridge._desktop_transcribe(audio) == ""
        assert len(desktop_calls) == 1
        bridge._get_local_whisper_model.assert_not_called()
    finally:
        release.set()
        for lock in (bridge._groq_asr_desktop_request_lock, bridge._groq_asr_request_lock):
            assert lock.acquire(timeout=2)
            lock.release()


@pytest.mark.parametrize("failure", [(429, "rate limit", {}), TimeoutError("slow")])
def test_desktop_cloud_failure_skips_local_but_mic_preserves_fallback(monkeypatch, failure):
    cloud = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    monkeypatch.setattr(bridge, "_groq_asr_with_deadline", cloud)
    assert bridge._desktop_transcribe([0.0] * 160) == ""
    bridge._get_local_whisper_model.assert_not_called()
    model = SimpleNamespace(transcribe=lambda *a, **kw: ([SimpleNamespace(text="mic local")], None))
    monkeypatch.setattr(bridge, "_get_local_whisper_model", Mock(return_value=model))
    assert bridge.transcribe_audio([0.0] * 160) == "mic local"
    bridge._get_local_whisper_model.assert_called_once()


def test_desktop_without_keys_skips_local_by_default_and_can_opt_in(monkeypatch):
    monkeypatch.setattr(bridge, "_desktop_groq_keys", lambda: [])
    assert bridge._desktop_transcribe([0.0] * 160) == ""
    bridge._get_local_whisper_model.assert_not_called()
    bridge.CONFIG["desktop_audio_local_fallback_enabled"] = True
    model = SimpleNamespace(transcribe=lambda *a, **kw: ([SimpleNamespace(text="desktop local")], None))
    monkeypatch.setattr(bridge, "_get_local_whisper_model", Mock(return_value=model))
    assert bridge._desktop_transcribe([0.0] * 160) == "desktop local"


def test_desktop_cloud_success_keeps_auto_language_and_key_rotation(monkeypatch):
    monkeypatch.setattr(bridge, "_desktop_groq_keys", lambda: ["gsk_A", "gsk_B"])
    monkeypatch.setattr(bridge._desktop_transcribe, "_kidx", 0, raising=False)
    cloud = Mock(return_value=(200, "", {"text": "desktop cloud"}))
    monkeypatch.setattr(bridge, "_groq_asr_with_deadline", cloud)
    for _ in range(2):
        assert bridge._desktop_transcribe([0.0] * 160) == "desktop cloud"
    assert [c.args[3]["Authorization"] for c in cloud.call_args_list] == ["Bearer gsk_A", "Bearer gsk_B"]
    assert all("language" not in c.args[4] for c in cloud.call_args_list)
    bridge._get_local_whisper_model.assert_not_called()


@pytest.mark.parametrize("background", [False, True])
def test_spawn_failure_releases_only_its_lane(monkeypatch, background):
    lane = bridge._groq_asr_desktop_request_lock if background else bridge._groq_asr_request_lock
    other = bridge._groq_asr_request_lock if background else bridge._groq_asr_desktop_request_lock
    other.acquire()
    worker = SimpleNamespace(start=Mock(side_effect=RuntimeError("cannot start")))
    monkeypatch.setattr(bridge.threading, "Thread", lambda **kw: worker)
    try:
        with pytest.raises(RuntimeError, match="cannot start"):
            bridge._groq_asr_with_deadline([0.0], 16000, "fake", {}, {}, 0.1, background)
        assert not lane.locked()
        assert other.locked()
    finally:
        other.release()


@pytest.mark.parametrize("background", [False, True])
def test_request_failure_releases_its_lane(monkeypatch, background):
    lane = bridge._groq_asr_desktop_request_lock if background else bridge._groq_asr_request_lock
    monkeypatch.setattr(bridge.arti_http_util, "groq_session", lambda: SimpleNamespace(
        post=Mock(side_effect=OSError("HTTP unavailable"))))
    with pytest.raises(OSError, match="HTTP unavailable"):
        bridge._groq_asr_with_deadline([0.0], 16000, "fake", {}, {}, 1, background)
    assert not lane.locked()
