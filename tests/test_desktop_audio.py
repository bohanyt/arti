"""Tests for arti_desktop_audio ring buffer and guards."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_desktop_audio as da


def test_dialogue_ring_maxlen():
    ring = da.DialogueRing(max_lines=3)
    for i in range(5):
        ring.append(f"line {i}")
    snap = ring.snapshot()
    assert len(snap) == 3
    assert snap[0].text == "line 2"


def test_should_reject_while_tts_playing():
    assert not da.should_accept_desktop_transcript(
        "hello",
        tts_is_playing=True,
        last_tts_end=None,
        is_echo_of_arti=lambda _: False,
    )


def test_should_reject_echo():
    assert not da.should_accept_desktop_transcript(
        "sama persis",
        tts_is_playing=False,
        last_tts_end=None,
        is_echo_of_arti=lambda t: t == "sama persis",
    )


def test_ingest_appends_when_ok():
    ring = da.DialogueRing(max_lines=5)
    ok = da.ingest_desktop_transcript(
        "dialog video",
        tts_is_playing=False,
        last_tts_end=None,
        is_echo_of_arti=lambda _: False,
        ring=ring,
    )
    assert ok
    assert ring.snapshot()[0].text == "dialog video"
