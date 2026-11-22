"""Classify git porcelain for the Live7 identity check (solo stream 2026-11-15).

A tracked source/DLL change is a hard stop. An untracked file that is not Python/C#
source — or that sits under a known runtime-output path — is a WARN, and play continues.
The identity dict shape (source, tree, dll, dirty) is unchanged.
"""
from __future__ import annotations

from pathlib import Path

from scripts.livepath.bringup import git_porcelain, parse_porcelain

SOURCE_SUFFIXES = {".py", ".pyi", ".cs", ".csx", ".dll"}
RUNTIME_OUTPUT_PREFIXES = (
    ".tmp/", "__pycache__/", ".pytest_cache/", ".hypothesis/",
    "session_logs/", "transcripts/", "vault/", "data/", "dump/",
    "runtime/", ".cursor/", "obj/", "bin/", "stardew-mod/",
)
RUNTIME_OUTPUT_PARTS = ("/__pycache__/", "/bin/", "/obj/")


def _norm(path):
    p = str(path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def is_runtime_output(path):
    p = _norm(path)
    if not p:
        return False
    if p.endswith(".pyc") or p.endswith(".pyo"):
        return True
    if any(p.startswith(prefix) for prefix in RUNTIME_OUTPUT_PREFIXES):
        return True
    return any(part in ("/" + p) or p.endswith(part.strip("/")) for part in RUNTIME_OUTPUT_PARTS)


def is_source_or_dll(path):
    return Path(_norm(path)).suffix.lower() in SOURCE_SUFFIXES


def classify_porcelain_drift(text):
    """Return (stop_paths, warn_paths) from `git status --porcelain` text.

    Untracked (`??`): WARN unless it is Python/C# source (or a DLL) *and* not under
    a known runtime-output path. Any other porcelain (tracked M/A/D/R/…) is a stop.
    """
    stop, warn = [], []
    for line in (text or "").splitlines():
        if len(line) < 2:
            continue
        xy = line[:2]
        path = line[3:] if len(line) >= 4 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path:
            continue
        untracked = xy.strip() == "??" or xy == "??"
        if untracked:
            if is_runtime_output(path) or not is_source_or_dll(path):
                warn.append(path)
            else:
                stop.append(path)
        else:
            stop.append(path)
    return stop, warn


def drift_verdict(actual, enrolled, porcelain_text):
    """Decide ok / warn / stop. Does not change the identity dict contract.

    Hash drift (source/tree/dll) or a stop-class porcelain path is a hard stop.
    Warn-only untracked dirt, with hashes matching, is a WARN (keep playing).
    `dirty=True` with nothing classifiable (empty porcelain) fails closed: stop.
    """
    actual = actual or {}
    enrolled = enrolled or {}
    hash_diff = [k for k in ("source", "tree", "dll") if actual.get(k) != enrolled.get(k)]
    stop_paths, warn_paths = classify_porcelain_drift(porcelain_text)
    if hash_diff or stop_paths:
        return dict(action="stop", paths=stop_paths, warn_paths=warn_paths, hash_diff=hash_diff)
    if warn_paths:
        return dict(action="warn", paths=warn_paths, warn_paths=warn_paths, hash_diff=[])
    if actual.get("dirty"):
        return dict(action="stop", paths=[], warn_paths=[], hash_diff=hash_diff)
    return dict(action="ok", paths=[], warn_paths=[], hash_diff=[])


def porcelain_of(root):
    try:
        return git_porcelain(root)
    except Exception:
        return ""


# parse_porcelain is the shared path extractor; re-export for tests.
paths_of = parse_porcelain
