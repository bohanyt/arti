"""Timecode RAG tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_vault_rag as rag


def test_parse_mmss_heading():
    assert rag.parse_mmss("## [12:34] Pomni panik") == 12 * 60 + 34
    assert rag.parse_mmss("3:05") == 185
    assert rag.parse_mmss("no time") is None


def test_mmss_from_seconds():
    assert rag.mmss_from_seconds(125) == "2:05"


def test_search_by_timecode_window(tmp_path):
    db = tmp_path / "rag.db"
    cfg = {**rag.DEFAULT_CONFIG, "vault_rag_db_path": str(db)}
    rag.init_db(cfg)
    conn = rag._connect(cfg)
    for mmss, title in [("12:00", "a"), ("12:30", "b"), ("14:00", "c")]:
        conn.execute(
            """
            INSERT INTO chunks (
                source_path, source_type, folder, chunk_index, heading,
                content, content_hash, mtime, char_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"vault/watch-parties/tadc-ep1/ep.md",
                "vault",
                "watch-parties/tadc-ep1",
                0,
                f"## [{mmss}] {title}",
                f"Scene {title}",
                f"hash-{mmss}",
                1.0,
                20,
                1.0,
            ),
        )
    conn.commit()
    conn.close()

    hits = rag.search_by_timecode(
        "tadc-ep1",
        "12:34",
        cfg,
        window_before_sec=45,
        window_after_sec=15,
        limit=3,
    )
    headings = [h["heading"] for h in hits]
    assert any("[12:30]" in h for h in headings)
    assert not any("[14:00]" in h for h in headings)


def test_should_skip_general_live_rag():
    assert not rag.should_skip_general_live_rag({"watch_party_enabled": False})
    assert rag.should_skip_general_live_rag(
        {
            "watch_party_enabled": True,
            "watch_party_event_id": "tadc-ep1",
        }
    )
