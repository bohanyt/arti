"""Session and turn identifiers on content-free API telemetry rows."""

from __future__ import annotations

import asyncio
import json

import arti_api_telemetry as tel
import arti_bridge as bridge
from arti_executors import WorkPool


def _rows(directory):
    return [
        json.loads(line)
        for path in directory.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_session_start_sets_session_before_first_telemetry_flush(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bridge.session_transcript, "current_session_id", lambda config: "unit-session")
    monkeypatch.setattr(bridge.arti_voice_dsp, "aktif", lambda config: True)
    monkeypatch.setattr(bridge, "version", lambda package: "1.2.3")
    config = {
        "telemetry_dir": str(tmp_path),
        "telemetry_enabled": True,
        "telemetry_log_each_call": True,
        "youtube_chat_enabled": True,
        "youtube_video_id": "public-video-id",
        "tts_engine": "supertone",
        "supertonic_voice": "F1",
        "supertonic_speed": 1.1,
        "supertonic_total_steps": 10,
        "supertonic_pitch_semitone": 2.0,
        "supertonic_range_factor": 1.4,
        "tts_voice": "id-ID-GadisNeural",
    }

    bridge._record_session_start_telemetry(config)
    tel.flush(config)

    rows = _rows(tmp_path / "telemetry")
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "unit-session"
    assert row["subsystem"] == "session"
    assert row["provider"] == "bridge"
    assert row["model"] == "session_start"
    assert set(row) == {
        "ts", "session_id", "subsystem", "provider", "model", "weight_tier",
        "model_display", "latency_ms", "prompt_tokens", "completion_tokens",
        "total_tokens", "cost_usd", "cost_source", "reference_cost_usd",
        "reference_per_1m", "turn_id", "ok", "extra",
    }
    assert set(row["extra"]) == {
        "started_at", "youtube_video_id", "tts_engine", "supertonic_voice",
        "supertonic_speed", "supertonic_total_steps", "supertonic_pitch_semitone",
        "supertonic_range_factor", "voice_polish_active", "supertonic_pkg_version",
        "tts_voice",
    }
    assert row["turn_id"] is None
    assert row["extra"]["youtube_video_id"] == "public-video-id"
    assert row["extra"]["voice_polish_active"] is True
    assert row["extra"]["supertonic_pkg_version"] == "1.2.3"


def test_turn_id_is_scoped_to_voice_turn_and_null_outside(monkeypatch):
    observed = []

    async def impl(trigger, memories, prompt):
        observed.append(tel.current_turn_id())

    monkeypatch.setattr(bridge, "_handle_voice_trigger", impl)
    assert tel.current_turn_id() is None
    asyncio.run(bridge._handle_voice_trigger_traced(bridge.VoiceTrigger("", turn_id="unit-turn"), [], ""))
    assert observed == ["unit-turn"]
    assert tel.current_turn_id() is None


def test_turn_id_propagates_to_thread_dispatched_record_call(tmp_path):
    config = {
        "telemetry_dir": str(tmp_path),
        "telemetry_enabled": True,
        "telemetry_log_each_call": True,
    }
    token = tel.set_turn_id("unit-thread-turn")
    executor = WorkPool("telemetry-test", 1)
    try:
        asyncio.run(
            executor.run(
                lambda: tel.record_call(
                    subsystem="llm", provider="unit", model="unit", latency_ms=1,
                    config=config,
                )
            )
        )
    finally:
        executor.shutdown()
        tel.reset_turn_id(token)

    rows = _rows(tmp_path / "telemetry")
    assert len(rows) == 1
    assert rows[0]["turn_id"] == "unit-thread-turn"
