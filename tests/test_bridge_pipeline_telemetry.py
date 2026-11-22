"""Real bridge timing/telemetry hooks exercised with fake providers and audio."""

import asyncio
import json
import time
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest

import arti_bridge as bridge
import pipeline_timer


def _events(tmp_path):
    return [json.loads(line) for path in (tmp_path / "telemetry").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


# An asyncio sleep shorter than the Windows event-loop clock tick (15.6 ms) returns at once,
# so a 15 ms fake synth measured 0 ms in a full run (pkg101B). 50 ms is above the tick.
SYNTH_SEC = 0.05


@pytest.fixture
def audio(monkeypatch, tmp_path):
    engine = bridge.TTSEngine.__new__(bridge.TTSEngine)
    engine.device_id = None
    monkeypatch.setattr(bridge, "CONFIG", {**bridge.CONFIG, "tts_engine": "supertone",
                        "telemetry_enabled": True, "telemetry_log_each_call": True,
                        "telemetry_dir": str(tmp_path / "telemetry"),
                        "arti_speech_censor_enabled": False})
    monkeypatch.setattr(bridge.subtitle_runtime, "enabled", False)
    monkeypatch.setattr(bridge.sf, "read", lambda path: ([0] * 480, 48000))
    monkeypatch.setattr(bridge, "resample_audio", lambda data, source, target: (data, target))
    monkeypatch.setattr(bridge.sd, "play", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_sd_gerbang_play_retry_device", lambda *args: nullcontext())
    monkeypatch.setattr(bridge, "_wait_for_playback", lambda duration: time.sleep(0.015))
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    pipeline_timer.consume_tts_stages()
    yield engine
    pipeline_timer.consume_tts_stages()


def test_supertone_records_existing_synth_and_play_stages(audio, monkeypatch, tmp_path):
    async def acquire(text):
        await asyncio.sleep(SYNTH_SEC)
        return "unit-no-file.wav", []

    monkeypatch.setattr(audio, "_acquire_supertone", acquire)
    asyncio.run(audio.speak("unit private text"))
    events = [row for row in _events(tmp_path) if row["subsystem"] == "tts"]
    assert len(events) == 1
    row = events[0]
    stages = pipeline_timer.consume_tts_stages()
    assert row["provider"] == row["extra"]["engine"] == "supertone"
    assert row["ok"] is True
    assert row["extra"]["synth_ms"] == stages["tts_synth_ms"] >= 5
    assert row["extra"]["play_ms"] == stages["tts_play_ms"] >= 5
    assert row["latency_ms"] == stages["tts_synth_ms"] + stages["tts_play_ms"]
    assert "unit private text" not in json.dumps(events)
    assert bridge.tts_is_playing is False


@pytest.mark.parametrize("fail_playback", [False, True])
def test_supertone_fallback_records_both_attempts_and_edge_playback(
    audio, monkeypatch, tmp_path, fail_playback
):
    async def acquire(text):
        raise TimeoutError("unit private text")

    class Communicate:
        def __init__(self, *args):
            pass

        async def stream(self):
            await asyncio.sleep(SYNTH_SEC)
            yield {"type": "audio", "data": b"fake"}

    monkeypatch.setattr(audio, "_acquire_supertone", acquire)
    monkeypatch.setattr(bridge.edge_tts, "Communicate", Communicate)
    if fail_playback:
        def fail(*args, **kwargs):
            raise RuntimeError("unit private text")
        monkeypatch.setattr(bridge.sd, "play", fail)
    asyncio.run(audio.speak("unit private text"))
    events = [row for row in _events(tmp_path) if row["subsystem"] == "tts"]
    assert [row["provider"] for row in events] == ["supertone", "edge_tts"]
    assert events[0]["ok"] is False
    assert events[0]["extra"]["error_class"] == "TimeoutError"
    edge = events[1]
    assert edge["ok"] is (not fail_playback)
    assert edge["extra"]["synth_ms"] >= 5
    if fail_playback:
        assert edge["extra"]["error_class"] == "RuntimeError"
    else:
        assert edge["extra"]["play_ms"] >= 5
    assert "unit private text" not in json.dumps(events)
    assert bridge.tts_is_playing is False


@pytest.mark.parametrize("source", ["groq:unit-model", "codex", "cursor",
                                    "groq:openrouter/vendor/unit-model"])
def test_main_llm_records_measured_latency_and_used_provider(monkeypatch, tmp_path, source):
    from arti_voice_pipeline import TurnContext

    monkeypatch.setattr(bridge, "CONFIG", {**bridge.CONFIG,
                        "expression_nod_enabled": False, "codex_model": "unit-codex",
                        "cursor_model": "unit-cursor", "telemetry_enabled": True,
                        "telemetry_log_each_call": True,
                        "telemetry_dir": str(tmp_path / "telemetry")})
    monkeypatch.setattr(bridge, "_prepare_turn_start", AsyncMock())
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: True)
    monkeypatch.setattr(bridge, "mood_block_now", lambda: "")
    monkeypatch.setattr(bridge, "viewer_block_for", lambda name: "")
    monkeypatch.setattr(bridge, "_should_route_to_cursor", lambda trigger: True)
    monkeypatch.setattr(bridge, "_try_stardew_owner_generation", lambda trigger: (None, False))
    monkeypatch.setattr(bridge.arti_voice_pipeline, "prepare_turn_context", AsyncMock(
        return_value=TurnContext("unit private text", [], "system", llm_system="system",
                                 prompt_content="unit private text")))
    monkeypatch.setattr(bridge.arti_expression_runtime, "apply_turn_end", AsyncMock())
    monkeypatch.setattr(bridge, "clean_ai_reply", lambda reply: "")

    async def reply(*args, **kwargs):
        await asyncio.sleep(SYNTH_SEC)
        return "unit private reply", [], source

    monkeypatch.setattr(bridge, "_cursor_reply_with_fallback", reply)
    asyncio.run(bridge._handle_voice_trigger(bridge.VoiceTrigger("unit private text", "mic"), [], "system"))
    rows = [row for row in _events(tmp_path) if row["subsystem"] == "llm"]
    assert len(rows) == 1
    assert rows[0]["latency_ms"] >= 5
    assert rows[0]["ok"] is True
    provider = "openrouter" if "openrouter/" in source else source.partition(":")[0]
    assert rows[0]["provider"] == provider
    expected_model = {"groq": "unit-model", "openrouter": "vendor/unit-model"}.get(provider, f"unit-{provider}")
    assert rows[0]["model"] == expected_model
    assert "unit private" not in json.dumps(rows)
