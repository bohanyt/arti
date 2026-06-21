"""Tests for timeline guard and memory fact filters."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_memory_quality as mq
import arti_timeline_guard as tg


def test_is_timeline_question():
    assert tg.is_timeline_question("arti mulai ada sejak kapan")
    assert tg.is_timeline_question("kapan debut arti")
    assert not tg.is_timeline_question("halo arti apa kabar")


def test_should_persist_rejects_session_start_noise():
    assert not tg.should_persist_memory_fact("Stream fact: Arti baru saja memulai live stream")
    assert not tg.should_persist_memory_fact("Live stream baru dimulai sekitar pukul 10:38")
    assert tg.should_persist_memory_fact("Stream fact: Viewer suka nasi goreng pedas")


def test_filter_scouter_facts():
    out = tg.filter_scouter_facts(
        [
            "Arti baru saja memulai live stream",
            "Viewer minta lagu Viva La Vida",
        ]
    )
    assert out == ["Viewer minta lagu Viva La Vida"]


def test_find_earliest_viewer_mention(tmp_path):
    sessions = tmp_path / "vault" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-05-27-default.md").write_text("Viewer ExampleViewer hello", encoding="utf-8")
    (sessions / "2026-06-05-default.md").write_text("ExampleViewer lagi chat", encoding="utf-8")
    assert tg.find_earliest_viewer_mention("ExampleViewer", tmp_path) == "2026-05-27"


def test_build_timeline_guard_contains_debut():
    block = tg.build_timeline_guard_block({"arti_debut_label": "27 Mei 2026"})
    assert "27 Mei 2026" in block
    assert "GUARD TIMELINE" in block


def test_memory_quality_skip_patterns():
    assert not mq.should_save_learning("Arti baru saja mulai streaming")
    assert mq.should_save_learning("Streamer suka kopi hitam")
