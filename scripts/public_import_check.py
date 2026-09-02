#!/usr/bin/env python3
"""Fail CI when public Python source imports a local module that was not shipped.

`compileall` checks syntax but does not resolve imports. Curated public exports can
therefore compile cleanly while still referring to a private/excluded sibling
module. This checker catches repository-local import shapes without trying to
validate third-party packages or optional lazy provider SDKs.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOCAL_PREFIXES = (
    "arti_",
    "bridge_",
    "session_",
    "pipeline_",
    "subtitle_",
    "text_preprocessor",
    "supertone_engine",
)


def tracked_python() -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z", "*.py"], cwd=ROOT)
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def looks_local(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in LOCAL_PREFIXES)


def main() -> int:
    tracked = tracked_python()
    root_modules = {Path(p).stem for p in tracked if "/" not in p}
    failures: set[str] = set()

    for rel in tracked:
        path = ROOT / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            failures.add(f"cannot parse {rel}: {exc}")
            continue

        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.append(node.module.split(".", 1)[0])

            for root in roots:
                if looks_local(root) and root not in root_modules:
                    failures.add(f"missing local import in {rel}: {root}")

    if failures:
        print("PUBLIC LOCAL IMPORT CHECK: BLOCKED")
        for failure in sorted(failures):
            print(f"- {failure}")
        return 1

    print("PUBLIC LOCAL IMPORT CHECK: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
