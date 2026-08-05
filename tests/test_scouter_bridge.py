"""Tests for bridge scouter wiring (vision auto-window, context)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_vtuber_bridge as bridge


@pytest.fixture(autouse=True)
def reset_vision_state():
    bridge.vision_runtime_on = False
    bridge.vision_auto_until = 0.0
    bridge._sync_vision_runtime_to_config()
    yield
    bridge.vision_runtime_on = False
    bridge.vision_auto_until = 0.0
    bridge._sync_vision_runtime_to_config()


def test_is_vision_active_manual():
    bridge.CONFIG["vision_enabled"] = True
    bridge.vision_runtime_on = True
    bridge._sync_vision_runtime_to_config()
    assert bridge.is_vision_active() is True


def test_is_vision_active_scouter_window():
    bridge.CONFIG["vision_enabled"] = True
    bridge.vision_runtime_on = False
    bridge.vision_auto_until = time.time() + 60
    bridge._sync_vision_runtime_to_config()
    assert bridge.is_vision_active() is True


def test_is_vision_active_off():
    bridge.CONFIG["vision_enabled"] = True
    bridge.vision_runtime_on = False
    bridge.vision_auto_until = 0.0
    bridge._sync_vision_runtime_to_config()
    assert bridge.is_vision_active() is False


def test_get_scouter_context_screen_hint():
    bridge.scouter_result = {
        "summary": "Chat ramai.",
        "emotion": "excited",
        "topic": "game",
        "screen_relevant": True,
        "screen_hint": "streamer bilang lihat boss",
    }
    bridge.summarizer_result = bridge.scouter_result
    ctx = bridge.get_scouter_context()
    assert "[LAYAR RELEVAN:" in ctx
    assert "lihat boss" in ctx


def test_apply_scouter_result_auto_vision():
    data = {
        "summary": "Layar penting.",
        "emotion": "neutral",
        "topic": "layar",
        "important_facts": [],
        "screen_relevant": True,
        "screen_hint": "chat minta lihat",
        "curious_worthy": True,
        "curious_hook": "komentar scene",
    }
    with patch.object(bridge, "set_mood"), patch.object(
        bridge.arti_vision_client, "refresh_if_stale", return_value=(None, "")
    ):
        bridge.apply_scouter_result(data)

    assert bridge.vision_auto_until > time.time()
    assert bridge.CONFIG.get("scouter_last_result") == data


def test_scouter_timer_due_new_history():
    import collections

    bridge._last_scouter_ts = 0.0
    bridge._last_scouter_history_snapshot = ["old line"]
    bridge.CONFIG["scouter_interval_sec"] = 90
    bridge.CONFIG["scouter_min_gap_sec"] = 30
    bridge.stream_history = collections.deque(["old line", "new chat"], maxlen=50)
    assert bridge._scouter_timer_due() is True


def test_scouter_timer_skip_unchanged_history():
    import collections

    bridge._last_scouter_ts = 0.0
    bridge._last_scouter_history_snapshot = ["same"]
    bridge.CONFIG["scouter_interval_sec"] = 90
    bridge.stream_history = collections.deque(["same"], maxlen=50)
    assert bridge._scouter_timer_due() is False
