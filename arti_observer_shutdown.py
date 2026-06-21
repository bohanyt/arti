"""Run Observer + Kurator shutdown pipeline (blocking)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import arti_curator as curator
import arti_observer_pipeline as pipeline
import arti_observer_progress as progress
import arti_observer_rag as obs_rag
import session_transcript as st


def run_observer_shutdown(
    config: dict,
    *,
    on_progress: Callable[[str, int, int, str], None] | None = None,
) -> list[Any]:
    """Observe → curate → write beats → embed observer DB. Blocking."""
    if not config.get("observer_enabled", True):
        return []

    session_id = st.get_session_id(config) or ""
    tx_path = st.get_transcript_path(config)
    if not session_id or not tx_path or not Path(tx_path).is_file():
        print("[Observer] Skip — no transcript")
        return []

    cb = on_progress or progress.make_progress_callback("Observer")
    print(f"[Observer] Starting post-stream pipeline for {session_id}...")

    beats = pipeline.run_observe(session_id, tx_path, config, on_progress=cb)
    cb("curate", 0, 1, "verifying")
    result = curator.curate_beats(beats, config)
    beats = result.beats
    cb("curate", 1, 1, f"approved={result.approved_count}")

    jsonl_path, md_path = pipeline.beats_paths(session_id, config)
    pipeline.write_beats_jsonl(beats, jsonl_path)
    pipeline.write_beats_md(beats, md_path, session_id)
    print(f"[Observer] Wrote {jsonl_path.name} ({len(beats)} beats)")

    cb("embed_observer", 0, 1, "indexing")
    obs_rag.reindex_beats_session(session_id, config)
    cb("embed_observer", 1, 1, "done")

    obs_rag.promote_approved_to_vault(session_id, config)
    curator.append_timeline_to_session_md(session_id, beats)

    print(
        f"[Observer] Done — approved={result.approved_count} "
        f"rejected={result.rejected_count} learnings={result.learnings_written}"
    )
    return beats
