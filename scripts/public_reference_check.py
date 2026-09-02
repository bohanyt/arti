#!/usr/bin/env python3
"""Validate public documentation/source references against the tracked tree.

This catches a recurring publication failure mode: a curated file survives the
export but still tells users to open an internal plan, private handoff, or helper
script/test that was intentionally not published.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".js", ".json", ".yml", ".yaml", ".txt"}

PRIVATE_REF_PREFIXES = (
    ".cur" + "sor/",
    "tas" + "ks/",
    "docs/han" + "doff/",
    "docs/res" + "earch/",
    "docs/pla" + "ns/",
)

LOCAL_ONLY_PREFIXES = (
    "vault/", "session_logs/", "transcripts/", "data/", "tmp/", "dump/",
)
LOCAL_ONLY_EXACT = {
    ".env", "config_local.json", "ARTI_SOUL.md", "ARTI_VIEWERS.md",
    "ARTI_MOOD_STATE.json", "vts_token.txt", "live_session.json",
}

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Bare repo paths often appear in code comments without Markdown/backticks.
BARE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:docs|scripts|tests|mc-bot)/[A-Za-z0-9_./-]+\.(?:md|py|js|json|html))"
)
PATHLIKE_RE = re.compile(
    r"^(?:\.?\.?/)?(?:docs|scripts|tests|mc-bot)/[^\s]+$"
    r"|^(?:\.?\.?/)?(?:arti_[A-Za-z0-9_.-]+\.py|hermes_vtuber_bridge\.py|"
    r"bridge_health\.py|subtitle_server\.py|subtitle\.html)$"
)


def tracked_files() -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def normalize_candidate(raw: str) -> str | None:
    s = raw.strip().strip("'\".,:;)")
    if not s or s.startswith(("http://", "https://", "mailto:", "#")):
        return None
    s = s.split("#", 1)[0].split("?", 1)[0].strip()
    if not s or any(ch in s for ch in "*{}<>|"):
        return None
    if " " in s or "=" in s:
        return None
    return s.replace("\\", "/")


def resolve_relative(source: str, candidate: str) -> str:
    if candidate.startswith("/"):
        candidate = candidate.lstrip("/")
    # Bare paths that begin with a top-level public directory are repo-root refs.
    if candidate.startswith(("docs/", "scripts/", "tests/", "mc-bot/")):
        return candidate
    source_dir = PurePosixPath(source).parent
    parts: list[str] = []
    for part in (source_dir / candidate).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def is_intentionally_local(path: str) -> bool:
    return path in LOCAL_ONLY_EXACT or any(path.startswith(p) for p in LOCAL_ONLY_PREFIXES)


def main() -> int:
    tracked = set(tracked_files())
    failures: set[str] = set()

    for source in sorted(tracked):
        path = ROOT / source
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
            ".gitattributes", ".gitignore"
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for prefix in PRIVATE_REF_PREFIXES:
            if prefix in text:
                failures.add(f"private/internal reference in {source}: {prefix}")

        candidates: list[tuple[str, str]] = []
        if path.suffix.lower() == ".md":
            candidates.extend(("markdown", m.group(1)) for m in MARKDOWN_LINK_RE.finditer(text))
        candidates.extend(("backtick", m.group(1)) for m in BACKTICK_RE.finditer(text))
        candidates.extend(("bare", m.group(1)) for m in BARE_PATH_RE.finditer(text))

        seen: set[tuple[str, str]] = set()
        for kind, raw in candidates:
            key = (kind, raw)
            if key in seen:
                continue
            seen.add(key)
            cand = normalize_candidate(raw)
            if not cand:
                continue
            if kind == "backtick" and not PATHLIKE_RE.match(cand):
                continue
            resolved = resolve_relative(source, cand)
            if is_intentionally_local(resolved) or is_intentionally_local(cand):
                continue
            if resolved not in tracked:
                prefix = resolved.rstrip("/") + "/"
                if any(p.startswith(prefix) for p in tracked):
                    continue
                failures.add(f"missing public reference in {source}: {raw} -> {resolved}")

    if failures:
        print("PUBLIC REFERENCE CHECK: BLOCKED")
        for failure in sorted(failures):
            print(f"- {failure}")
        return 1

    print("PUBLIC REFERENCE CHECK: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
