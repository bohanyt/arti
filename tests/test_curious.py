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


def test_build_prompt_contains_scene_and_question_guidance():
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="YouTube music."))
    text = curious.build_prompt(
        {
            "scouter_last_result": {
                "curious_hook": "Tombol pause di tengah video IShowSpeed",
                "curious_question": "Kamu sengaja pause di detik itu?",
            }
        }
    )
    assert "YouTube music" in text
    assert "[Curious" in text
    assert "pertanyaan" in text.lower()
    assert "IShowSpeed" in text or "pause" in text.lower()


def test_should_fire_rejects_generic_hook():
    curious.reset_session()
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="YouTube paused."))
    cfg = {
        "curious_enabled": True,
        "curious_interval_sec": 0,
        "curious_cooldown_sec": 0,
        "vision_enabled": True,
        "vision_runtime_on": True,
        "vision_stale_sec": 60,
        "scouter_last_result": {
            "curious_worthy": True,
            "curious_hook": "layar menampilkan sesuatu",
        },
    }
    assert not curious.should_fire(
        cfg, brain_busy=False, tts_playing=False, ptt_active=False
    )


def test_hook_dedup_after_mark_fired():
    curious.reset_session()
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="Code editor open."))
    hook = "Fungsi refresh_vision dipanggil dua kali berturut"
    cfg = {
        "curious_enabled": True,
        "curious_interval_sec": 0,
        "curious_cooldown_sec": 0,
        "vision_enabled": True,
        "vision_runtime_on": True,
        "vision_stale_sec": 60,
        "scouter_last_result": {"curious_worthy": True, "curious_hook": hook},
    }
    assert curious.should_fire(
        cfg, brain_busy=False, tts_playing=False, ptt_active=False
    )
    curious.mark_fired(cfg)
    curious._last_curious_ts = 0.0
    curious._last_interval_check_ts = 0.0
    cfg2 = {**cfg, "curious_cooldown_sec": 0}
    cfg2["scouter_last_result"] = {"curious_worthy": True, "curious_hook": hook}
    assert not curious.should_fire(
        cfg2, brain_busy=False, tts_playing=False, ptt_active=False
    )


def test_should_fire_rejects_bridge_log_hook():
    """Live 2026-08-02 sore: seed 'cursor screen relevant False' -> Arti narasi
    dapurnya sendiri. Hook yang mengutip log bridge = backstage, buang."""
    curious.reset_session()
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="Terminal aktif."))
    cfg = {
        "curious_enabled": True,
        "curious_interval_sec": 0,
        "curious_cooldown_sec": 0,
        "vision_enabled": True,
        "vision_runtime_on": True,
        "vision_stale_sec": 60,
        "scouter_last_result": {
            "curious_worthy": True,
            "curious_hook": (
                "Kenapa di terminal muncul cursor screen relevant False "
                "padahal Scouter jalan terus?"
            ),
        },
    }
    assert not curious.should_fire(
        cfg, brain_busy=False, tts_playing=False, ptt_active=False
    )

# --- layar kosong bukan topik (audit 2026-08-03) ---------------------------------


def test_prepare_for_fire_skips_when_scene_and_hook_boring(monkeypatch):
    """Background hitam + hook scouter juga boring = turn dibatalkan; hook
    berisi topik nyata = tetap jalan walau layar gelap."""
    monkeypatch.setattr(curious, "_vision_effective", lambda c: True)
    sc.screen_ring.push(sc.ScreenSnapshot(
        wall_ts=time.time(),
        scene="Layar hanya menampilkan latar belakang hitam dengan garis",
    ))
    cfg = {
        "curious_requires_fresh_screen": False,
        "scouter_last_result": {"curious_hook": "layar gelap tanpa aktivitas"},
    }
    assert curious.prepare_for_fire(cfg) is False

    cfg["scouter_last_result"] = {"curious_hook": "Bohan lagi sketsa Cakrawala"}
    assert curious.prepare_for_fire(cfg) is True
