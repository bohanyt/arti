"""Package-build guard (V9b, 2026-11-18): refuse to build a package from a checkout that holds private state.

pkg110 stopped NOT READY on its privacy guard because the 11-17 livestream test had run the bridge from the package
checkout and left her viewer file, mood, vault session note and VTS token there. A package tool calls this first:

    python -m scripts.livepath.package_guard <checkout>

Exit 0 = no private state (one OK line). Exit 1 = REFUSED, naming the paths (never their content); `mabar restore`
on the run that wrote them moves them out.
"""
from __future__ import annotations

from pathlib import Path
import sys

from scripts.livepath import bringup


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[2]
    found = bringup.private_state_in(root)
    if found:
        print("REFUSED package build: private state in %s: %s (run `mabar restore` for the run that wrote it, or move "
              "it out by hand, verified)" % (root, ", ".join(found)))
        return 1
    print("OK package build: no private state in %s" % root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
