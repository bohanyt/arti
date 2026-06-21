"""Tests for arti_screen_context."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_screen_context as sc


def test_parse_vision_response_json():
    raw = json.dumps(
        {
            "scene": "Pomni berdiri di depan tenda.",
            "playback_mmss": "12:34",
            "ocr_text": "PAUSE",
        }
    )
    snap = sc.parse_vision_response(raw)
    assert snap.scene.startswith("Pomni")
    assert snap.playback_mmss == "12:34"
    assert snap.ocr_text == "PAUSE"


def test_parse_playback_null_string():
    raw = json.dumps({"scene": "Desktop.", "playback_mmss": "null", "ocr_text": ""})
    snap = sc.parse_vision_response(raw)
    assert snap.playback_mmss is None


def test_build_vision_prompt_contract():
    p = sc.build_vision_prompt()
    assert "MAKS 200" in p
    assert "null JSON" in p


def test_screen_ring_max_size():
    ring = sc.ScreenRing(max_size=2)
    for i in range(4):
        ring.push(sc.ScreenSnapshot(wall_ts=float(i), scene=f"s{i}"))
    assert len(ring.snapshot()) == 2
    assert ring.latest().scene == "s3"


def test_build_vision_prompt_mentions_timecode():
    p = sc.build_vision_prompt()
    assert "playback_mmss" in p
    assert "play" in p.lower()


def test_format_screen_context_empty():
    ring = sc.ScreenRing()
    assert sc.format_screen_context(ring) == ""
