"""Mechanical checks for the maintainability rules in AGENTS.md.

Read-only. Prints each violation and exits 1 if there is any, 0 otherwise.
Daily housekeeping runs it and lists new violations in its HK report.

    python scripts/check_repo_rules.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DOC = "docs/CURRENT.md"
CURRENT_MAX_LINES = 40
ROOT_PY_ALLOWLIST = "scripts/repo_rules_root_py_allowlist.txt"
PRIVATE_NAMES = frozenset(
    {".env", "config_local.json", "ARTI_SOUL.md", "ARTI_VIEWERS.md", "ARTI_MOOD_STATE.json"}
)
_LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def is_live_doc(path: str) -> bool:
    """Live docs: root *.md, docs/README.md and docs/CURRENT.md.

    The docs/current/ mirror is excluded: it is a verbatim copy of the controller
    runbooks, so its relative links point into the runbooks folder, not the repo.
    """
    p = PurePosixPath(path)
    if p.suffix != ".md":
        return False
    return len(p.parts) == 1 or path in ("docs/README.md", CURRENT_DOC)


def current_doc_too_long(text: str) -> str | None:
    lines = len(text.splitlines())
    if lines > CURRENT_MAX_LINES:
        return f"{CURRENT_DOC}: {lines} lines (max {CURRENT_MAX_LINES})"
    return None


def broken_links(doc: str, text: str, tracked: set[str]) -> list[str]:
    """Relative markdown links in ``doc`` whose target is neither a tracked file nor a tracked directory."""
    out = []
    base = PurePosixPath(doc).parent
    for lineno, line in enumerate(text.splitlines(), 1):
        for target in _LINK.findall(line):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                continue
            rel = target.split("#", 1)[0]
            if not rel:
                continue
            parts: list[str] = []
            for part in (base / rel).parts:
                if part == "..":
                    if parts:
                        parts.pop()
                elif part != ".":
                    parts.append(part)
            resolved = "/".join(parts).rstrip("/")
            if resolved in tracked or any(t.startswith(resolved + "/") for t in tracked):
                continue
            out.append(f"{doc}:{lineno}: broken link -> {target}")
    return out


def new_root_py(tracked: set[str], allowlist: set[str]) -> list[str]:
    return sorted(p for p in tracked if "/" not in p and p.endswith(".py") and p not in allowlist)


def tracked_private(tracked: set[str]) -> list[str]:
    return sorted(p for p in tracked if PurePosixPath(p).name in PRIVATE_NAMES)


def _tracked() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8")
    return {p for p in out.split("\0") if p}


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


def collect_violations() -> list[str]:
    tracked = _tracked()
    violations: list[str] = []
    if CURRENT_DOC in tracked:
        too_long = current_doc_too_long(_read(CURRENT_DOC))
        if too_long:
            violations.append(too_long)
    else:
        print(f"skip: {CURRENT_DOC} is not in this checkout", file=sys.stderr)
    for doc in sorted(p for p in tracked if is_live_doc(p)):
        violations.extend(broken_links(doc, _read(doc), tracked))
    if (ROOT / ROOT_PY_ALLOWLIST).is_file():
        allowlist = {ln.strip() for ln in _read(ROOT_PY_ALLOWLIST).splitlines() if ln.strip()}
        violations.extend(f"{p}: new top-level .py (put it in a package)" for p in new_root_py(tracked, allowlist))
    else:
        print(f"skip: {ROOT_PY_ALLOWLIST} is not in this checkout; the top-level .py check did not run", file=sys.stderr)
    violations.extend(f"{p}: private file is tracked" for p in tracked_private(tracked))
    return violations


def main() -> int:
    violations = collect_violations()
    for v in violations:
        print(v)
    print(f"check_repo_rules: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
