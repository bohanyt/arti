"""Tests for observer segment pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import arti_observer_pipeline as pipe


def test_ts_to_seconds():
    assert pipe.ts_to_seconds("00:05:30") == 330
    assert pipe.ts_to_seconds("bad") is None


def test_segment_by_minutes():
    rows = [
        {"ts": "00:01:00", "kind": "streamer", "text": "halo"},
        {"ts": "00:11:00", "kind": "arti", "text": "hai"},
    ]
    segs = pipe.segment_by_minutes(rows, minutes=10)
    assert len(segs) == 2
    assert segs[0].index == 0
    assert segs[1].index == 1


def test_write_beats_jsonl_md(tmp_path, monkeypatch):
    beats = [
        pipe.BeatDraft(
            session_id="2026-06-05-default",
            segment_index=0,
            t_start="00:00:00",
            t_end="00:10:00",
            event_count=2,
            summary="tes segmen",
            curator_status="approved",
        )
    ]
    jl = tmp_path / "beats.jsonl"
    md = tmp_path / "beats.md"
    pipe.write_beats_jsonl(beats, jl)
    pipe.write_beats_md(beats, md, "2026-06-05-default")
    assert jl.is_file()
    row = json.loads(jl.read_text(encoding="utf-8").strip())
    assert row["summary"] == "tes segmen"
    assert "tes segmen" in md.read_text(encoding="utf-8")


def test_summarize_session_from_beats(tmp_path, monkeypatch):
    import session_transcript as st

    jl = tmp_path / "beats.jsonl"
    jl.write_text(
        json.dumps(
            {
                "curator_status": "approved",
                "t_start": "00:00:00",
                "summary": "Beat satu",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "_ROOT", tmp_path.parent)
    # beats path uses _ROOT / vault / sessions - place file there
    sess_dir = tmp_path / "vault" / "sessions"
    sess_dir.mkdir(parents=True)
    beats_in_vault = sess_dir / "2026-06-05-default_beats.jsonl"
    beats_in_vault.write_text(jl.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(st, "_session_id", "2026-06-05-default")
    monkeypatch.setattr(st, "_ROOT", tmp_path)
    out = st.summarize_session_from_beats(beats_in_vault, {})
    assert "Beat satu" in out

    tx = tmp_path / "t.jsonl"
    tx.write_text(
        json.dumps({"ts": "00:01:00", "kind": "streamer", "text": "hello"}) + "\n",
        encoding="utf-8",
    )

    def fake_summarize(block, config):
        return {"summary": "mock", "topics": ["test"], "facts": [], "worth_embed": True, "noise_level": "low", "provider": "mock"}

    monkeypatch.setattr("arti_observer_client.summarize_segment", fake_summarize)
    beats = pipe.run_observe("sess", tx, {"observer_segment_minutes": 10})
    assert len(beats) == 1
    assert beats[0].summary == "mock"
