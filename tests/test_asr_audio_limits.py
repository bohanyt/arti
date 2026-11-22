"""Synthetic mic input only; the listener never opens a real device."""

from types import SimpleNamespace
from unittest.mock import Mock
import queue

import numpy as np
import pytest

import arti_bridge as bridge
from arti_audio_buffer import AudioChunkQueue, UtteranceBuffer


def test_utterance_stores_array_chunks_and_caps_samples():
    recording = UtteranceBuffer(16000, 1)
    for _ in range(20):
        recording.append(np.ones(1600, dtype=np.float32))
    assert recording.full
    assert all(isinstance(chunk, np.ndarray) for chunk in recording.chunks)
    assert len(recording.chunks) == 10
    assert recording.as_array().shape == (16000,)
    recording.clear()
    assert not recording.full
    assert recording.as_array().size == 0


def test_queue_drops_oldest_and_counts_without_blocking(capsys):
    audio = AudioChunkQueue(2)
    for value in range(100):
        audio.put_latest(np.array([value]))
    assert audio.qsize() == 2
    assert audio.dropped == 98
    audio.report_drops()
    audio.report_drops()
    assert capsys.readouterr().out.count("drop_oldest=98") == 1
    assert audio.get_nowait()[0] == 98
    assert audio.get_nowait()[0] == 99


@pytest.mark.parametrize("mode", ["ptt", "passive", "wake"])
def test_continuous_sound_finalized_at_cap_through_existing_routes(monkeypatch, mode):
    monkeypatch.setattr(bridge, "CONFIG", dict(bridge.CONFIG,
        asr_max_utterance_sec=1, asr_audio_queue_max_chunks=3,
        trigger_mode="wake_word" if mode == "wake" else "push_to_talk",
        asr_ptt_silence_tail_sec=4))
    for attr, value in {"hotkey_active": mode == "ptt", "tts_is_playing": False,
                        "_reflex_playing": False, "_telinga_dimatikan": False,
                        "_asr_restart_requested": False, "_asr_ptt_cooldown_until": 0,
                        "_brain_busy": False}.items():
        monkeypatch.setattr(bridge, attr, value)
    monkeypatch.delattr(bridge.voice_listener_worker, "_last_tts_end", raising=False)
    monkeypatch.setattr(bridge, "resolve_asr_input_device", lambda: (None, "fake mic"))
    clock = [100.0]
    monkeypatch.setattr(bridge, "time", SimpleNamespace(
        time=lambda: clock[0], perf_counter=lambda: clock[0],
        monotonic=lambda: clock[0], sleep=lambda _: None))  # mic callback watchdog clock
    stream = {"callback": None, "opens": 0, "gets": 0}

    class FakeStream:
        def __init__(self, **kwargs):
            stream["callback"] = kwargs["callback"]
        def __enter__(self):
            stream["opens"] += 1
            return self
        def __exit__(self, *args):
            pass

    class FedQueue(AudioChunkQueue):
        def get(self, block=True, timeout=None):
            if timeout is not None and self.empty():
                stream["gets"] += 1
                if stream["gets"] >= 80:
                    bridge._telinga_dimatikan = True
                    raise queue.Empty  # bound a regression instead of reopening forever
                clock[0] += 0.1
                level = 0 if stream["opens"] == 1 else 0.5
                stream["callback"](np.full((1600, 1), level, dtype=np.float32), 1600, None, None)
            return super().get(block, timeout)

    monkeypatch.setattr(bridge.sd, "InputStream", FakeStream)
    monkeypatch.setattr(bridge, "AudioChunkQueue", FedQueue)
    arrays = []

    def transcribe(data, *args, **kwargs):
        arrays.append(data)
        bridge._telinga_dimatikan = True
        return "halo"

    monkeypatch.setattr(bridge, "_transcribe_stardew_ptt", transcribe)
    monkeypatch.setattr(bridge, "transcribe_audio", transcribe)
    monkeypatch.setattr(bridge, "filter_whisper_hallucination", lambda text, **kwargs: text)
    monkeypatch.setattr(bridge, "is_asr_noise_transcript", lambda *args: False)
    monkeypatch.setattr(bridge, "is_asr_echo_of_arti", lambda *args: False)
    monkeypatch.setattr(bridge, "is_arti_wake_call", lambda *args: True)
    monkeypatch.setattr(bridge, "queue_voice_trigger", Mock())
    monkeypatch.setattr(bridge, "summarizer_queue", SimpleNamespace(put=Mock()))
    monkeypatch.setattr(bridge, "add_to_history", Mock())
    complete_mock = Mock()
    monkeypatch.setattr(bridge, "_complete_stardew_asr", complete_mock)
    bridge.voice_listener_worker()
    assert len(arrays) == 1
    assert arrays[0].shape == (16000,)
    assert np.all(arrays[0] == 0.5)
    if mode == "ptt":
        assert complete_mock.call_args.args[2] == 1.0
    elif mode == "wake":
        bridge.queue_voice_trigger.assert_called_once()
    else:
        bridge.add_to_history.assert_called_once_with("Streamer", "halo")
