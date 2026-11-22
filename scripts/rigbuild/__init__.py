"""Build procedures of the rig, in the repository (ADR 245).

Until 2026-11-19 the scripts that build a test build or a live package, write its bookings and run the native chain
lived only in evidence folders on one laptop (`lab-rig\\evidence\\tb*`, `lab-rig\\evidence\\pkg*`) and were copied by hand
from run to run, changing names, paths and commit ids. This package is those scripts, one command each:

    python -m scripts.rigbuild test-build --source <commit> --out <folder> --python-gates <text> [--config <file>]
    python -m scripts.rigbuild package --source <commit> --number <N> --python-gates <text> --warning-file <file>
    python -m scripts.rigbuild booking --build <test build or package folder> --like <earlier booking> --out <new>
    python -m scripts.rigbuild savestates [--make]
    python -m scripts.rigbuild check

Machine paths come from one config file (`rigbuild.example.json` holds placeholder paths to replace) or
arguments, never from constants here. The behaviour is the hand procedure's: `test-build` is testbuild140
(`RECEIPT_TESTBUILD140_2026-11-19.md`), `package` is pkg112 (`RECEIPT_PKG112_2026-11-18.md`). Where the two
disagree, each command keeps its own receipt's rule; ADR 245 lists every difference.
"""
