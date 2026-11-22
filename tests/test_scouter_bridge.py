"""Tests for bridge scouter wiring (vision auto-window, context)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_bridge as bridge


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


# --- brain batch B (2026-10-21): slim scouter ---------------------------------


def test_scouter_defaults_slim():
    """Defaults in code (config_local may still override per machine)."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert '"scouter_interval_sec": 300,' in src
    assert '"scouter_min_gap_sec": 120,' in src
    assert '"scouter_every_n_triggers": 0,' in src
    # REVIEW_BRAIN F8: facts back on so the learnings file keeps growing.
    assert '"scouter_facts_enabled": True,' in src
    assert '"observer_rag_live_enabled": True,' in src
    # the frozen startup summary path is gone (it was always empty)
    assert "summarizer_context = get_summarizer_context()" not in src


def test_apply_scouter_result_skips_facts_but_keeps_mood(monkeypatch):
    saved: list[str] = []
    moods: list[str] = []
    monkeypatch.setattr(bridge, "save_long_term_memory", saved.append)
    monkeypatch.setattr(bridge, "set_mood", moods.append)
    monkeypatch.setitem(bridge.CONFIG, "scouter_facts_enabled", False)
    data = {"summary": "Ringkasan sintetis.", "emotion": "senang", "topic": "x",
            "important_facts": ["Streamer punya rencana konten mingguan yang tetap"],
            "screen_relevant": False}
    bridge.apply_scouter_result(data)
    assert saved == []
    assert moods == ["happy"]
    monkeypatch.setitem(bridge.CONFIG, "scouter_facts_enabled", True)
    bridge.apply_scouter_result(data)
    assert saved == ["Stream fact: Streamer punya rencana konten mingguan yang tetap"]


def test_trigger_queue_does_not_run_scouter_when_every_n_zero(monkeypatch):
    import queue as _queue

    runs: list[str] = []
    q = _queue.Queue()
    q.put("wake")
    monkeypatch.setattr(bridge, "scouter_queue", q)
    monkeypatch.setitem(bridge.CONFIG, "scouter_every_n_triggers", 0)
    monkeypatch.setitem(bridge.CONFIG, "scouter_enabled", True)
    monkeypatch.setattr(bridge, "_run_scouter_pass", runs.append)

    def timer_due():
        bridge.scouter_running = False
        return False

    monkeypatch.setattr(bridge, "_scouter_timer_due", timer_due)
    bridge.scouter_worker()
    assert runs == []
    assert q.empty()
