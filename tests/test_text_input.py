"""Tests for text_input_worker (trigger via ketikan console)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_vtuber_bridge as bridge


def _run_worker_with_lines(monkeypatch, lines: str):
    """Jalankan text_input_worker dengan stdin palsu; rekam trigger & history."""
    triggers: list[tuple] = []
    history: list[tuple] = []

    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(lines))
    monkeypatch.setattr(
        bridge, "queue_voice_trigger",
        lambda text, trigger_type="mic", viewer_name=None, **kw: triggers.append(
            (text, trigger_type, viewer_name)
        ),
    )
    monkeypatch.setattr(
        bridge, "add_to_history",
        lambda source, message, arti_meta=None: history.append((source, message)),
    )

    # StringIO habis → readline() == "" → worker return (tidak infinite loop)
    bridge.text_input_worker()
    return triggers, history


def test_typed_message_queues_mic_trigger(monkeypatch):
    triggers, history = _run_worker_with_lines(monkeypatch, "eh arti kamu nyala?\n")
    assert triggers == [("eh arti kamu nyala?", "mic", None)]
    assert history == [("Streamer", "eh arti kamu nyala?")]


def test_yt_prefix_simulates_youtube_chat(monkeypatch):
    triggers, history = _run_worker_with_lines(monkeypatch, "yt @bohanyt: halo arti\n")
    assert len(triggers) == 1
    text, ttype, viewer = triggers[0]
    assert ttype == "yt_chat"
    assert viewer == "@bohanyt"
    assert "halo arti" in text and "(YouTube)" in text
    assert history == [("Viewer @bohanyt (YouTube)", "halo arti")]


def test_yt_without_name_uses_default_viewer(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "yt_default_viewer", "@bohanyt")
    triggers, history = _run_worker_with_lines(monkeypatch, "yt arti kamu nyala?\n")
    assert len(triggers) == 1
    text, ttype, viewer = triggers[0]
    assert ttype == "yt_chat"
    assert viewer == "@bohanyt"
    assert "arti kamu nyala?" in text
    assert history == [("Viewer @bohanyt (YouTube)", "arti kamu nyala?")]


def test_blank_lines_and_eof_ignored(monkeypatch):
    triggers, history = _run_worker_with_lines(monkeypatch, "\n   \n")
    assert triggers == []
    assert history == []
