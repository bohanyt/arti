#!/usr/bin/env python3
"""Fail CI when the tracked public tree contains excluded/private artifacts.

The scan intentionally operates on ``git ls-files`` so ignored local files do not
create false alarms. Content checks are conservative and target credential forms
and known operational identifiers that should never exist in the public tree.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_PREFIXES = (
    ".cursor/",
    "docs/handoff/",
    "docs/research/",
    "docs/plans/",
    "tasks/",
    "vault/",
    "session_logs/",
    "transcripts/",
    "dump/",
    "vts-backup/",
    "data/telemetry/",
    "data/stardew/",
    "data/finetune/",
    "finetune/curation/input/",
    "finetune/curation/reviewed/",
)
EXCLUDED_EXACT = {
    ".env",
    "CLAUDE.md",
    "GITHUB_PUSH.md",
    "docs/CURRENT.md",
    "config_local.json",
    "ARTI_SOUL.md",
    "ARTI_VIEWERS.md",
    "ARTI_MOOD_STATE.json",
    "vts_token.txt",
    "live_session.json",
    "PROGRESS.md",
}
EXCLUDED_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".dmp",
)

# Construct high-signal secret markers in pieces so this scanner does not match
# its own source merely by containing the literal token prefix.
SECRET_PATTERNS = [
    re.compile("gh" + r"p_[A-Za-z0-9]{30,}"),
    re.compile("github_pat" + r"_[A-Za-z0-9_]{20,}"),
    re.compile("sk" + r"-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|bearer|password)\s*[:=]\s*['\"][A-Za-z0-9_./+\-=]{16,}['\"]"),
]
PRIVATE_PATHS = [
    # Accept spaces in Windows user names; stop only at the next backslash/newline.
    re.compile(r"(?i)[A-Z]:\\Users\\[^\\\r\n]+\\"),
    re.compile(r"/Users/[^/\r\n]+/"),
    re.compile(r"/home/[^/\r\n]+/"),
]

# Known real operational values from the private frozen baseline. They are built
# in pieces so the scanner itself does not contain the complete forbidden value.
KNOWN_PRIVATE_LITERALS = (
    "HuWZx" + "-APkAM",          # historical live/video id
    "MSI" + " Thin 15",          # one-machine identifier
    "@bohan" + "yt",             # operator handle
    "bohan" + "yto",             # historical fixture/operator identifier
    "@Bohan" + "YT",             # case variant
)

TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".json", ".jsonl", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".env", ".example", ".bat", ".ps1", ".cs", ".java",
}


def tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


def is_text_candidate(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile", ".gitignore", ".gitattributes"}


def main() -> int:
    failures: list[str] = []
    for rel in tracked_files():
        norm = rel.replace("\\", "/")
        lower = norm.lower()
        if norm in EXCLUDED_EXACT or any(norm.startswith(p) for p in EXCLUDED_PREFIXES):
            failures.append(f"excluded tracked path: {norm}")
            continue
        if lower.endswith(EXCLUDED_SUFFIXES):
            failures.append(f"runtime/database artifact: {norm}")
            continue
        if not is_text_candidate(ROOT / rel):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                failures.append(f"secret-like content: {norm}")
                break
        for pat in PRIVATE_PATHS:
            if pat.search(text):
                failures.append(f"private absolute path: {norm}")
                break
        for value in KNOWN_PRIVATE_LITERALS:
            if value in text:
                failures.append(f"known private operational value: {norm}")
                break

    if failures:
        print("PUBLIC PRIVACY SCAN: BLOCKED")
        for failure in sorted(set(failures)):
            print(f"- {failure}")
        return 1

    print("PUBLIC PRIVACY SCAN: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
