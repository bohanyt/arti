"""Build a read-only ``legacy_scopes.json`` sidecar from run journals only.

Usage:
    python tools/mind_scope_map.py --runs <folder> --out <file>

Reads ONLY run cycle state (``cycle_state.json``) and day-attempt journals
(``g1_journal.jsonl``) under ``--runs``. Never opens any mind file, vault, save,
secret, bridge log, or session log. Never writes into a mind folder; the
operator places the generated sidecar into ``<mind>/stardew/legacy_scopes.json``
only after owner confirmation.

Any goal id or journey/likes day that appears under two different scopes is left
out of the map and counted as ambiguous. No goal/reflection text is written.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime import ipc, mind_scope, plan_dates  # noqa: E402

GAME = "stardew"
SKIP_DIRS = frozenset({
    "mind",
    "vault",
    "vault-export",
    "bridge",
    "session_logs",
    "checkout-state",
    "saves",
    "secrets",
    "checkpoints",
    "provider_archive",
    ".git",
    "__pycache__",
})
MIND_FILES = frozenset({
    "goals.jsonl",
    "journey.jsonl",
    "likes.jsonl",
    "discoveries.jsonl",
    "ledger.jsonl",
    "progress.jsonl",
    "prefs.jsonl",
    "now.json",
    "experience_digest.json",
})


def _looks_like_mind_target(out_path: Path) -> str | None:
    """Refuse writing into any mind directory (never mutates a mind store by itself)."""
    resolved = out_path.resolve()
    env_mind = os.environ.get("ARTI_MIND_DIR", "").strip()
    if env_mind:
        try:
            mind_root = Path(env_mind).resolve()
            if resolved == mind_root or mind_root in resolved.parents:
                return f"refusing to write inside ARTI_MIND_DIR ({mind_root})"
        except OSError:
            pass
    for parent in resolved.parents:
        if parent.name.lower() == "mind":
            return f"refusing to write inside mind directory ({parent})"
        try:
            if parent.is_dir() and any((parent / f).exists() for f in MIND_FILES):
                return f"refusing to write beside existing mind files ({parent})"
        except OSError:
            continue
    return None


def _iter_cycle_dirs(roots: Iterable[Path]) -> list[Path]:
    """Find cycle folders containing ``cycle_state.json`` without entering forbidden dirs."""
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
            if "cycle_state.json" in filenames:
                key = os.path.normcase(os.path.abspath(dirpath))
                if key not in seen:
                    seen.add(key)
                    found.append(Path(dirpath))
    found.sort(key=lambda p: str(p))
    return found


def _iter_cycle_journals(cycle_dir: Path) -> list[Path]:
    """Find ``g1_journal.jsonl`` files inside one cycle folder."""
    found: list[Path] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(cycle_dir):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
        if "g1_journal.jsonl" in filenames:
            path = Path(dirpath) / "g1_journal.jsonl"
            key = os.path.normcase(os.path.abspath(str(path)))
            if key not in seen:
                seen.add(key)
                found.append(path)
    found.sort(key=lambda p: str(p))
    return found


def _read_json_dict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _remembered_day_from_planning(row: dict) -> int | None:
    remembered = row.get("remembered_day")
    if type(remembered) is int and remembered > 0:
        return remembered
    mode = row.get("mode")
    day = row.get("day") if type(row.get("day")) is int and row["day"] > 0 else None
    target_day = row.get("target_day") if type(row.get("target_day")) is int and row["target_day"] > 0 else None
    if mode == "night":
        if target_day is not None and target_day > 1:
            return target_day - 1
        return day
    if target_day is not None:
        return target_day
    return day


def build_map(runs_roots: Iterable[Path]) -> dict:
    """Scan run folders and build the sidecar dictionary (IDs/keys -> scope_hash; zero text)."""
    goal_candidates: dict[str, set[str]] = defaultdict(set)
    journey_candidates: dict[str, set[str]] = defaultdict(set)
    likes_candidates: dict[str, set[str]] = defaultdict(set)

    cycles_scanned = 0
    cycles_without_scope = 0
    journals_scanned = 0

    for cycle_dir in _iter_cycle_dirs(runs_roots):
        cycles_scanned += 1
        state = _read_json_dict(cycle_dir / "cycle_state.json")
        sh = mind_scope.cycle_scope_from_state(state)
        if not sh:
            cycles_without_scope += 1
            continue
        for journal_path in _iter_cycle_journals(cycle_dir):
            journals_scanned += 1
            try:
                text = journal_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                ev = row.get("ev")
                if ev == "goal_planning":
                    for gid in row.get("new") or ():
                        if isinstance(gid, str) and gid.strip():
                            goal_candidates[gid].add(sh)
                    jday = _remembered_day_from_planning(row)
                    if jday is not None:
                        journey_candidates[plan_dates.label(GAME, jday)].add(sh)
                        likes_candidates[str(jday)].add(sh)
                elif ev == "mind_reflection" and row.get("game", GAME) == GAME:
                    for gid in row.get("new_goals") or ():
                        if isinstance(gid, str) and gid.strip():
                            goal_candidates[gid].add(sh)
                    day = row.get("day")
                    if type(day) is int and day > 0:
                        journey_candidates[plan_dates.label(GAME, day)].add(sh)
                        likes_candidates[str(day)].add(sh)
                elif ev == "discovery" and row.get("game", GAME) == GAME:
                    day = row.get("day")
                    if type(day) is int and day > 0:
                        row_sh = row.get("scope_hash") if mind_scope.is_scope_hash(row.get("scope_hash")) else sh
                        journey_candidates[plan_dates.label(GAME, day)].add(row_sh)

    def resolve(candidates: dict[str, set[str]]) -> tuple[dict[str, str], int]:
        mapped: dict[str, str] = {}
        ambiguous = 0
        for key in sorted(candidates):
            scopes = candidates[key]
            if len(scopes) == 1:
                mapped[key] = next(iter(scopes))
            elif len(scopes) > 1:
                ambiguous += 1
        return mapped, ambiguous

    goals, goals_ambiguous = resolve(goal_candidates)
    journey, journey_ambiguous = resolve(journey_candidates)
    likes, likes_ambiguous = resolve(likes_candidates)

    return {
        "schema": mind_scope.LEGACY_SCOPES_SCHEMA,
        "goals": goals,
        "journey": journey,
        "likes": likes,
        "counts": {
            "cycles_scanned": cycles_scanned,
            "cycles_without_scope": cycles_without_scope,
            "journals_scanned": journals_scanned,
            "goals_mapped": len(goals),
            "goals_ambiguous": goals_ambiguous,
            "journey_mapped": len(journey),
            "journey_ambiguous": journey_ambiguous,
            "likes_mapped": len(likes),
            "likes_ambiguous": likes_ambiguous,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        action="append",
        required=True,
        type=Path,
        help="Run folder or runs root folder containing cycle_state.json and journals (repeatable).",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Destination file for the legacy_scopes.json sidecar (must not be inside a mind folder).",
    )
    args = parser.parse_args(argv)

    missing = [str(p) for p in args.runs if not p.is_dir()]
    if missing:
        print(f"error: runs folder not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    refusal = _looks_like_mind_target(args.out)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 2

    payload = build_map(args.runs)
    ipc.atomic_write(
        args.out,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        fsync=True,
    )
    c = payload["counts"]
    print(
        "mind_scope_map: cycles=%d (no_scope=%d) journals=%d "
        "goals=%d (ambiguous=%d) journey=%d (ambiguous=%d) likes=%d (ambiguous=%d) -> %s"
        % (
            c["cycles_scanned"],
            c["cycles_without_scope"],
            c["journals_scanned"],
            c["goals_mapped"],
            c["goals_ambiguous"],
            c["journey_mapped"],
            c["journey_ambiguous"],
            c["likes_mapped"],
            c["likes_ambiguous"],
            args.out,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
