"""TEST-ONLY staging: stage_event.py question|control|festival-start [--timeout 1..30].

Writes MOD/operator-event-staging/request.json, outside the bridge inbox. Requires
OperatorEventStagingEnabled=true at deploy. Turn OFF after W6.
festival-start uses a separate HOST-only directory; --mod-dir selects that bridge copy.
No port, chat, foreground or supervisor stdin. Exit: 0 started, 2
refused/invalid, 3 timeout (never automatically retry an uncertain request).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m1_paths as paths


def stage(verb, *, mod=paths.MOD, timeout=15, trial_check=None, context_path=None):
    if verb not in ("question", "control", "festival-start") or not 1 <= timeout <= 30:
        print("REFUSED: verb must be question|control|festival-start; timeout must be 1..30", file=sys.stderr)
        return 2
    if trial_check is None:
        from win_facts import trial_running
        trial_check = trial_running
    if trial_check() is not False:
        print("REFUSED: trial running or process list unknown", file=sys.stderr)
        return 2
    if verb == "control" and context_path is not None:
        from w13_preflight import check_file
        try:
            check_file(context_path)
        except (OSError, ValueError, TypeError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    try:
        cfg = json.loads((mod / "config.json").read_text(encoding="utf-8-sig"))
        if cfg.get("OperatorEventStagingEnabled") is not True:
            raise ValueError("staging gate OFF; CT must enable OperatorEventStagingEnabled at deploy/restart")
        directory = mod / ("operator-festival-staging" if verb == "festival-start" else "operator-event-staging")
        directory.mkdir(exist_ok=True)
        request_id = uuid.uuid4().hex
        pending = directory / "request.json"
        temp = directory / (request_id + ".tmp")
        request = dict(id=request_id, verb=verb, expires_at_ms=int(time.time() * 1000) + int(timeout * 1000))
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(request, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Atomic no-overwrite publication on Windows AND POSIX; concurrent operators cannot replace a request.
            os.link(temp, pending)
        finally:
            temp.unlink()
    except (OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    receipt = directory / (request_id + ".receipt.json")
    deadline = time.monotonic() + timeout
    print(json.dumps(dict(request_id=request_id, request=str(pending), receipt=str(receipt))), flush=True)
    while time.monotonic() < deadline:
        if receipt.exists():
            result = json.loads(receipt.read_text(encoding="utf-8"))
            print(json.dumps(result))
            return 0 if result.get("id") == request_id and result.get("success") is True else 2
        time.sleep(0.1)
    print(json.dumps(dict(timeout=timeout, request_id=request_id, consumed=not pending.exists(),
                         receipt=str(receipt), note="inspect receipt/SMAPI; no automatic retry; request expires")))
    return 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=("question", "control", "festival-start"))
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--mod-dir", type=Path, default=paths.MOD,
                        help="explicit operator mod directory (festival-start requires the HOST bridge copy)")
    parser.add_argument("--context", type=Path, help="fresh native inspect_context receipt; required for Haley control")
    args = parser.parse_args(argv)
    if args.verb == "control" and args.context is None:
        print("REFUSED: control requires --context from a fresh plain Beach inspection", file=sys.stderr)
        return 2
    return stage(args.verb, timeout=args.timeout, mod=args.mod_dir, context_path=args.context)


if __name__ == "__main__":
    raise SystemExit(main())
