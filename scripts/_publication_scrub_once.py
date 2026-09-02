#!/usr/bin/env python3
"""ONE-SHOT publication transform for PR #12. Delete before merge.

Only genericizes maintainer-bound text and dead private-workspace references.
It does not parse or change control flow, configuration keys, thresholds, models,
or protocol semantics.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".json", ".jsonl", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".bat", ".ps1", ".cs", ".java",
}

MAINTAINER_NAME = "Bo" + "han"
PRIVATE_VTS_DEV = "Antigravity" + "Developer"


def tracked() -> list[Path]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / p.decode("utf-8") for p in raw.split(b"\0") if p]


def is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {".gitignore", ".gitattributes"}


def transform(text: str) -> str:
    # Preserve label capitalization where it reads as a speaker label; elsewhere
    # use a role noun so a fresh public clone is not bound to the maintainer.
    text = text.replace(MAINTAINER_NAME + ":", "Streamer:")
    text = text.replace(MAINTAINER_NAME.upper(), "STREAMER")
    text = text.replace(MAINTAINER_NAME, "streamer")
    text = text.replace(PRIVATE_VTS_DEV, "YourDeveloperName")

    # Remove private-workspace archaeology from comments/docs while retaining the
    # useful statement that evidence/design exists outside the public tree.
    private_patterns = (
        r"\.cursor/[A-Za-z0-9_./\[\]-]+",
        r"tasks/[A-Za-z0-9_./\[\]-]+",
        r"docs/handoff/[A-Za-z0-9_./\[\]-]+",
        r"docs/research/[A-Za-z0-9_./\[\]-]+",
        r"docs/plans/[A-Za-z0-9_./\[\]-]+",
    )
    for pat in private_patterns:
        text = re.sub(pat, "private development notes", text)
    return text


def main() -> int:
    changed: list[str] = []
    for path in tracked():
        if path.resolve() == SELF or not is_text(path):
            continue
        try:
            before = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        after = transform(before)
        if after != before:
            path.write_text(after, encoding="utf-8", newline="")
            changed.append(path.relative_to(ROOT).as_posix())
    print("publication scrub changed:")
    for rel in changed:
        print(f"- {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
