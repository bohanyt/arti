"""python -m scripts.rigbuild <command> ... (ADR 245). `--help` on each command lists its arguments."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from scripts.rigbuild import config as config_mod
from scripts.rigbuild.machine import Refused

CONFIG_ENV = "ARTI_RIGBUILD_CONFIG"


def _config_args(p):
    p.add_argument("--config", type=Path, default=os.environ.get(CONFIG_ENV),
                   help=f"JSON config (default: ${CONFIG_ENV}); example scripts/rigbuild/rigbuild.example.json")
    for key in config_mod.REQUIRED:
        p.add_argument("--" + key.replace("_", "-"), dest=key, help=config_mod.REQUIRED[key])


def _load(a):
    return config_mod.load(a.config, {k: getattr(a, k, None) for k in config_mod.REQUIRED})


def parser():
    top = argparse.ArgumentParser(prog="python -m scripts.rigbuild", description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)

    tb = sub.add_parser("test-build", help="native chain + staged test build + booking (testbuild140)")
    tb.add_argument("--source", required=True, help="the commit to build (full or short id)")
    tb.add_argument("--out", required=True, type=Path, help="new test-build folder, e.g. <rig>\\tb-fix142a")
    tb.add_argument("--python-gates", required=True, help="the coordinator's Python gate logs on this commit")
    tb.add_argument("--like", type=Path, help="earlier test-build booking to copy rig fields from (config booking_like)")
    tb.add_argument("--booking", type=Path, help="new booking (default <rig>\\free-play-<out name>-booking.json)")
    tb.add_argument("--evidence", type=Path, help="new evidence folder (default <rig>\\evidence\\<out name>)")
    tb.add_argument("--receipt", type=Path, help="receipt path (default <receipts>\\RECEIPT_TESTBUILD_<out>_<date>.md)")
    tb.add_argument("--previous", help="previous native build source, for the changed-files list and provenance")

    pk = sub.add_parser("package", help="the live package as pkg112 (native chain, golden, pages, staging, 2 bookings)")
    pk.add_argument("--source", required=True)
    pk.add_argument("--number", required=True, type=int, help="N: pm1-fix<N>-package, free-play<N>/mabar<N> bookings")
    pk.add_argument("--python-gates", required=True, help="the coordinator's Python gate logs on this commit")
    pk.add_argument("--warning-file", required=True, type=Path, help="the operator warning, verbatim, from the brief")
    pk.add_argument("--like-free-play", type=Path, help="default <rig>\\free-play<N-1>-booking.json")
    pk.add_argument("--like-mabar", type=Path, help="default <rig>\\mabar<N-1>-booking.json")
    pk.add_argument("--evidence", type=Path, help="default <rig>\\evidence\\pkg<N>")
    pk.add_argument("--receipt", type=Path, help="default <receipts>\\RECEIPT_PKG<N>_<date>.md")
    pk.add_argument("--golden-count", type=int, default=12, help="golden cases that must pass (pkg112: 12)")

    for p in (tb, pk):
        p.add_argument("--detach", action="store_true", help="check the source out detached in the checkout first")
        p.add_argument("--fetch", metavar="REMOTE", help="git fetch REMOTE in the checkout first")
        p.add_argument("--slot-minutes", type=int, default=90,
                       help="how long to wait for a held pytest slot before refusing (0 = refuse at once)")
        _config_args(p)

    bk = sub.add_parser("booking", help="write a booking from a test-build or package folder")
    bk.add_argument("--build", required=True, type=Path)
    bk.add_argument("--like", required=True, type=Path, help="an earlier booking (its rig fields are kept)")
    bk.add_argument("--out", required=True, type=Path)
    bk.add_argument("--code-sha", help="also set codeSha (a soak spec: the rig checkout's HEAD)")
    bk.add_argument("--output-root", type=Path, help="also set outputRoot (a soak spec: <rig>/rig/free-play-runs)")

    sv = sub.add_parser("savestates", help="verify, make or name the bench's savestates")
    sv.add_argument("--make", action="store_true", help="create the states whose source run is on this machine")
    _config_args(sv)

    ck = sub.add_parser("check", help="print the config, tool versions and one admission row; starts nothing")
    _config_args(ck)
    return top


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        if a.command == "booking":
            from scripts.rigbuild import stage
            row = stage.compose_booking(a.build, a.like, code_sha=a.code_sha, output_root=a.output_root)
            print(f"OK booking {a.out} sha256 {stage.write_booking(a.out, row, a.like)}")
            return 0
        cfg = _load(a)
        if a.command == "test-build":
            from scripts.rigbuild import testbuild
            return testbuild.run(cfg, source=a.source, out=a.out, python_gates=a.python_gates, like=a.like,
                                 booking=a.booking, evidence=a.evidence, receipt=a.receipt, detach=a.detach,
                                 fetch=a.fetch, previous=a.previous, slot_minutes=a.slot_minutes)
        if a.command == "package":
            from scripts.rigbuild import package
            return package.run(cfg, source=a.source, number=a.number, python_gates=a.python_gates,
                               warning_file=a.warning_file, like_free_play=a.like_free_play, like_mabar=a.like_mabar,
                               evidence=a.evidence, receipt=a.receipt, detach=a.detach, fetch=a.fetch,
                               golden_count=a.golden_count, slot_minutes=a.slot_minutes)
        if a.command == "savestates":
            from scripts.rigbuild import savestates
            if a.make:
                return int(bool(savestates.make(cfg)))
            for _, _, line in savestates.plan(cfg):
                print(line)
            return 0
        from scripts.rigbuild import machine
        import subprocess
        for line in config_mod.describe(cfg):
            print(line)
        for exe in (cfg.python312, cfg.python311):
            if exe.exists():
                print(f"{exe}: " + subprocess.run([str(exe), "--version"], capture_output=True, text=True).stdout.strip())
        print("admission row: ", machine.snapshot(cfg))
        print(f"pytest slot {cfg.slot}: " + ("HELD" if cfg.slot.exists() else "free"))
        return 0
    except (Refused, config_mod.ConfigError) as exc:
        print("REFUSED " + str(exc))
        return 2
    except Exception as exc:  # the step's own record (BLOCKED.md, receipt) is already written
        print("NOT DONE " + type(exc).__name__ + ": " + str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
