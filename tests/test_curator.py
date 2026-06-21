"""Tests for arti_curator."""

from __future__ import annotations

import arti_curator as curator
import arti_observer_pipeline as pipe


def test_curate_rejects_noise():
    beat = pipe.BeatDraft(
        session_id="s",
        segment_index=0,
        t_start="00:00:00",
        t_end="00:10:00",
        event_count=1,
        summary="",
        noise_level="high",
    )
    result = curator.curate_beats([beat], {})
    assert result.rejected_count == 1
    assert beat.curator_status == "rejected"


def test_curate_rejects_misleading_fact():
    beat = pipe.BeatDraft(
        session_id="s",
        segment_index=0,
        t_start="00:00:00",
        t_end="00:10:00",
        event_count=1,
        summary="ok",
        facts=[{"text": "Arti baru mulai live stream hari ini", "confidence": 0.9}],
        worth_embed=True,
    )
    result = curator.curate_beats([beat], {"arti_debut_label": "27 Mei 2026"})
    assert beat.curator_status == "rejected"
    assert result.rejected_count == 1
