"""Watch party context wiring tests (module-level, no bridge import)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_screen_context as sc
import arti_vault_rag as rag


def test_watch_party_timecode_hits_sample_format():
    hits = [
        {
            "score": 1.0,
            "semantic": 0.0,
            "keyword": 1.0,
            "source_path": "vault/watch-parties/tadc-ep1-sample.md",
            "source_type": "vault",
            "folder": "watch-parties/tadc-ep1",
            "heading": "## [12:34] Pomni",
            "content": "Scene at tent door.",
        }
    ]
    block = rag.format_hits_for_prompt(hits, 500)
    assert "[12:34]" in block
    assert "tent door" in block


def test_watch_state_tracks_playback():
    sc.watch_state.event_id = "tadc-ep1"
    snap = sc.ScreenSnapshot(wall_ts=1.0, scene="test", playback_mmss="12:34")
    sc.update_watch_state_from_snapshot(snap, event_id="tadc-ep1")
    assert sc.watch_state.playback_mmss == "12:34"
