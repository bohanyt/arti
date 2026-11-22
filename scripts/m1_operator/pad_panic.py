"""pad_panic.py: the operator's pad kill switch (PLAN §1.5 e). Allowed WHILE a trial runs: it is the kill switch.

  pad_panic.py [--timeout 10] [--dry-run]

Writes the inbox control file COMMAND_DIR/<m1_paths.PAD_PANIC_FILE> (atomic; protocol, op "pad_panic", request_id,
runtime_instance_id + session_id from session.json, utc), then waits up to --timeout seconds for BOTH:
  - a new SMAPI line with `PAD_PANIC` (every pad SButton suppressed, press set cleared, neutral oldPadState, reflexes
    stopped, gamepadMode restored via §1.5 a), and
  - session.json pad_mode_owned == false (key present).
The mod side (op name / file name) is WS-G0's; the names live in m1_paths (PAD_PANIC_FILE, PAD_KEYS): HYPOTHESIS until
G0 lands the op. Idempotent: a second panic is harmless.
No port, no input, no window is touched.
Exit: 0 panic confirmed | 2 no session (nothing written) | 3 no PAD_PANIC line in time | 4 PAD_PANIC seen but
pad_mode_owned is still true / not published.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402
import win_facts as wf  # noqa: E402

OWNED = paths.PAD_KEYS["owned"]


def panic_request(session, now):
    return {"protocol": 1, "op": "pad_panic", "request_id": uuid.uuid4().hex,
            "runtime_instance_id": session.get("runtime_instance_id"), "session_id": session.get("session_id"),
            "utc": wf.utc(now), "source": "operator pad_panic.py"}


def verdict(saw_panic, session):
    """Exit code for what was observed."""
    if not saw_panic:
        return 3
    return 0 if (session or {}).get(OWNED) is False else 4


class LiveIO:
    def __init__(self):
        self.tail = wf.FileTail(paths.SMAPI_LOG)

    def now(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def start_tail(self):
        self.tail.start()

    def new_smapi(self):
        return self.tail.read_new()

    def session(self):
        for _ in range(5):
            try:
                return paths.read_session()
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                time.sleep(0.05)
        return None

    def write_atomic(self, name, data):
        tmp = paths.COMMAND_DIR / (name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, paths.COMMAND_DIR / name)


def run(a, io=None):
    io = io or LiveIO()
    sess = io.session()
    if not sess or not sess.get("session_id"):
        print("REFUSED: no session.json with a session_id (farmhand not joined / inbox off): nothing written")
        return 2
    req = panic_request(sess, io.now())
    if a.dry_run:
        print(json.dumps(dict(dry_run=True, file=str(paths.COMMAND_DIR / paths.PAD_PANIC_FILE), request=req)))
        return 0
    io.start_tail()
    io.write_atomic(paths.PAD_PANIC_FILE, req)
    t0, saw, lines, last = io.now(), False, [], sess
    while io.now() - t0 < a.timeout:
        for line in io.new_smapi().splitlines():
            if "PAD_PANIC" in line:
                saw = True
                lines.append(line.strip()[:300])
        last = io.session() or last
        if saw and last.get(OWNED) is False:
            break
        io.sleep(0.2)
    code = verdict(saw, last)
    print(json.dumps(dict(exit=code, request_id=req["request_id"], seconds=round(io.now() - t0, 2), pad_panic_lines=lines[:5],
                          pad_mode_owned=last.get(OWNED), facts={k: last.get(v) for k, v in paths.PAD_KEYS.items()})))
    return code


def main(argv=None, io=None):
    ap = argparse.ArgumentParser(prog="pad_panic.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--dry-run", action="store_true", help="print the request, write nothing")
    a = ap.parse_args(argv)
    if not 0 < a.timeout <= 120:
        print("--timeout must be 0..120 s", file=sys.stderr)
        return 2
    return run(a, io)


if __name__ == "__main__":
    sys.exit(main())
