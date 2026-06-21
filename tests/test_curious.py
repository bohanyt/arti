"""Tests for arti_curious proactive trigger."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as curious
import arti_screen_context as sc


def test_should_fire_requires_fresh_vision():
    curious.reset_session()
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="Game FPS di layar."))
    cfg = {
        "curious_enabled": True,
        "curious_interval_sec": 0,
        "curious_cooldown_sec": 0,
        "curious_requires_fresh_screen": True,
        "vision_stale_sec": 30,
        "vision_enabled": True,
        "vision_runtime_on": True,
    }
    assert curious.should_fire(
        cfg, brain_busy=False, tts_playing=False, ptt_active=False, yt_cooling=False
    )


def test_should_fire_skip_when_busy():
    curious.reset_session()
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="Desktop."))
    cfg = {"curious_enabled": True, "curious_interval_sec": 0, "curious_cooldown_sec": 0}
    assert not curious.should_fire(
        cfg, brain_busy=True, tts_playing=False, ptt_active=False
    )


def test_build_prompt_contains_scene():
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="YouTube music."))
    text = curious.build_prompt({})
    assert "YouTube music" in text
    assert "[Curious" in text
