#!/usr/bin/env python3
"""Re-curate old observer beats with the fixed curator (brain batch B).

Until 2026-10-21 the curator rejected every beat with noise_level "high", even
when the observer said worth_embed: true (BRAIN_AUDIT.md §2.2: 234 of 820
beats). Rejected beats are still on disk in vault/sessions/<sid>_beats.jsonl.
This tool re-runs arti_curator.decide over the REJECTED beats only and
promotes the newly approved ones the same way the normal pipeline does:

  * <sid>_beats.jsonl  (status + curator_reason)
  * <sid>_beats.md     (approved timeline; vault RAG picks it up at the next
                        bridge start / shutdown reindex, hash-skip incremental)
  * <sid>.md           ("## Timeline beats" section, replaced, never appended)
  * learnings          (arti_curator.promote_learnings, same gate as live;
                        noisy beats never feed the learnings FIFO)
  * observer RAG DB    (arti_observer_rag.reindex_beats_session)

Safety:
  * dry run by default; --apply writes.
  * idempotent: already approved beats are never touched, a second run finds
    nothing new and changes nothing.
  * every file it changes is copied first to
    data/recurate_backups/<timestamp>/<same relative path> (outside vault/, so
    backups are never indexed).
  * sessions with a *_beats.partial marker (catch-up mid-run) are skipped.
  * output is counts, segment indexes and reasons only, never summary text.

Run with the bridge STOPPED:

    ./venv/Scripts/python.exe scripts/recurate_beats.py            # dry run
    ./venv/Scripts/python.exe scripts/recurate_beats.py --apply
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import arti_curator as curator  # noqa: E402
import arti_observer_catchup as catchup  # noqa: E402
import arti_observer_pipeline as pipeline  # noqa: E402
import arti_observer_rag as obs_rag  # noqa: E402
import arti_vault_rag as vault_rag  # noqa: E402

_ROOT_MODULES = (curator, catchup, pipeline, obs_rag, vault_rag)


def _bind_root(root: Path) -> dict:
    """Point every module that resolves vault paths at `root`; returns old values."""
    old = {m: m._ROOT for m in _ROOT_MODULES}
    for m in _ROOT_MODULES:
        m._ROOT = root
    return old


def _restore_root(old: dict) -> None:
    for m, value in old.items():
        m._ROOT = value


def _session_ids(root: Path, only: list[str] | None) -> list[str]:
    sessions = root / "vault" / "sessions"
    if not sessions.is_dir():
        return []
    sids = sorted(p.name[: -len("_beats.jsonl")] for p in sessions.glob("*_beats.jsonl"))
    if only:
        wanted = set(only)
        sids = [s for s in sids if s in wanted]
    return sids


def _plan_session(sid: str, config: dict) -> tuple[list[Any], list[Any]]:
    """(all beats, newly approved copies). Pure: no writes."""
    jsonl_path, _ = pipeline.beats_paths(sid, config)
    beats = catchup._load_existing_beats(jsonl_path)
    promoted: list[Any] = []
    for beat in beats:
        if beat.curator_status != "rejected":
            continue
        trial = dataclasses.replace(beat)
        status, reason = curator.decide(trial, config)
        if status == "approved":
            trial.curator_status = status
            trial.curator_reason = reason
            promoted.append(trial)
    return beats, promoted


def _would_promote_learnings(beat: Any) -> bool:
    return bool(beat.worth_learning and beat.facts
                and (beat.noise_level or "low") != "high")


class _Backup:
    def __init__(self, root: Path, backup_dir: Path):
        self.root = root
        self.dir = backup_dir
        self.saved: list[Path] = []
        self._seen: set[Path] = set()

    def save(self, path: Path) -> None:
        path = Path(path)
        if path in self._seen or not path.is_file():
            return
        self._seen.add(path)
        try:
            rel = path.resolve().relative_to(self.root.resolve())
        except ValueError:
            rel = Path(path.name)
        dest = self.dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        self.saved.append(dest)


def recurate(
    root: Path | str,
    config: dict,
    *,
    apply: bool = False,
    sessions: list[str] | None = None,
    learnings: bool = True,
    reindex: bool = True,
    backup_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Re-curate rejected beats under `root`. Returns a counts-only report."""
    root = Path(root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = _Backup(root, Path(backup_dir) if backup_dir else root / "data" / "recurate_backups" / stamp)
    report: dict[str, Any] = {
        "apply": apply, "sessions_scanned": 0, "sessions_changed": 0,
        "sessions_skipped_partial": 0, "beats_rejected_before": 0,
        "beats_promoted": 0, "learning_candidates": 0, "learnings_written": 0,
        "reindex_failed": 0, "still_rejected_reasons": {}, "per_session": {},
        "backups": [], "backup_dir": "",
    }
    still = collections.Counter()
    old_roots = _bind_root(root)
    try:
        for sid in _session_ids(root, sessions):
            jsonl_path, md_path = pipeline.beats_paths(sid, config)
            if catchup._partial_marker(jsonl_path).is_file():
                report["sessions_skipped_partial"] += 1
                continue
            report["sessions_scanned"] += 1
            beats, promoted = _plan_session(sid, config)
            rejected = [b for b in beats if b.curator_status == "rejected"]
            report["beats_rejected_before"] += len(rejected)
            promoted_idx = {b.segment_index for b in promoted}
            for b in rejected:
                if b.segment_index not in promoted_idx:
                    still[curator.decide(b, config)[1]] += 1
            if not promoted:
                continue
            n_learn = sum(1 for b in promoted if _would_promote_learnings(b))
            report["beats_promoted"] += len(promoted)
            report["learning_candidates"] += n_learn
            report["per_session"][sid] = {
                "promoted_segments": sorted(promoted_idx),
                "reasons": dict(collections.Counter(b.curator_reason for b in promoted)),
            }
            if not apply:
                continue

            session_md = root / "vault" / "sessions" / f"{sid}.md"
            for p in (jsonl_path, md_path, session_md):
                backup.save(p)
            if learnings and n_learn:
                backup.save(curator.learnings_path(config))
            if reindex:
                backup.save(obs_rag.vault_rag._db_path(obs_rag._observer_cfg(config)))

            by_index = {b.segment_index: b for b in beats}
            for b in promoted:
                by_index[b.segment_index] = b
                if learnings:
                    report["learnings_written"] += curator.promote_learnings(b, config)
            merged = [by_index[k] for k in sorted(by_index)]
            pipeline.write_beats_jsonl(merged, jsonl_path)
            pipeline.write_beats_md(merged, md_path, sid)
            curator.append_timeline_to_session_md(sid, merged)
            if reindex:
                try:
                    if obs_rag.reindex_beats_session(sid, config) is None:
                        report["reindex_failed"] += 1
                except Exception as e:  # noqa: BLE001 — beats are safe on disk
                    report["reindex_failed"] += 1
                    print(f"[Recurate] {sid}: observer reindex failed ({type(e).__name__})")
            report["sessions_changed"] += 1
    finally:
        _restore_root(old_roots)
    report["still_rejected_reasons"] = dict(still)
    report["backups"] = [str(p) for p in backup.saved]
    report["backup_dir"] = str(backup.dir) if backup.saved else ""
    return report


def _print_report(r: dict[str, Any]) -> None:
    mode = "APPLY" if r["apply"] else "DRY RUN (nothing written; --apply to write)"
    print(f"[Recurate] {mode}")
    print(f"[Recurate] sessions scanned={r['sessions_scanned']} "
          f"skipped_partial={r['sessions_skipped_partial']} changed={r['sessions_changed']}")
    print(f"[Recurate] rejected before={r['beats_rejected_before']} "
          f"newly approved={r['beats_promoted']} "
          f"learning candidates={r['learning_candidates']} "
          f"learnings written={r['learnings_written']}")
    for sid, info in sorted(r["per_session"].items()):
        print(f"  {sid}: +{len(info['promoted_segments'])} {info['reasons']}")
    print(f"[Recurate] still rejected by reason: {r['still_rejected_reasons']}")
    if r["reindex_failed"]:
        print(f"[Recurate] observer reindex failed for {r['reindex_failed']} session(s); "
              "the vault index catches up at the next bridge start")
    if r["backups"]:
        print(f"[Recurate] {len(r['backups'])} backup(s) under {r['backup_dir']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--root", type=Path, default=_REPO, help="repo root holding vault/ (default: this repo)")
    ap.add_argument("--session", action="append", default=None, help="limit to a session id (repeatable)")
    ap.add_argument("--no-learnings", action="store_true", help="do not write learnings")
    ap.add_argument("--no-reindex", action="store_true", help="do not reindex the observer RAG DB")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    import arti_bridge as bridge  # loads .env + config_local, same as rangkum_susulan

    report = recurate(args.root, dict(bridge.CONFIG), apply=args.apply,
                      sessions=args.session, learnings=not args.no_learnings,
                      reindex=not args.no_reindex)
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
