"""tools/arti_stop: create an `arti.stop/1` stop file (R0-03 §2; R0-07 §7.5). WP P2.

Operator use writes `writer: arti_stop`. The same `write_stop` is the scripted stop writer / FakeCore `safety` writer
for the R1 native-half drills (R0-03 §8 "Phase split"). Create-if-absent via `os.link`: never overwrites, never deletes.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[1]))
from arti_runtime import ipc
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

# R0-03 §1 "Causes and scopes" (closed); scope decides the file.
CAUSE_SCOPE = {**dict.fromkeys(("H1", "H2", "H5", "owner", "operator", "legacy"), "core"),
               **dict.fromkeys(("H3", "H4", "identity", "malformed", "halt_record_corrupt", "native_containment",
                                "native_unclean_exit", "entries_overflow"), "all")}
SCOPE_FILE = {"core": "stop.json", "all": "stop_all.json"}
WRITERS = ("safety", "arti_stop", "launcher")


def write_stop(folder, cause, *, save_id, player_id, writer="arti_stop", stop_id=None, reason=None):
    """Return (created, path, record). `created` False = the target already existed (never success for an assertion)."""
    if cause not in CAUSE_SCOPE or writer not in WRITERS:
        raise ValueError(f"cause {cause!r} / writer {writer!r} not in the closed sets")
    folder = Path(folder)
    scope = CAUSE_SCOPE[cause]
    record = {"v": "arti.stop/1", "stop_id": stop_id or "stop-" + uuid.uuid4().hex, "cause": cause, "scope": scope,
              "save_id": save_id, "player_id": int(player_id), "created_ms": int(time.time() * 1000), "writer": writer}
    if reason:
        record["reason"] = reason[:64]
    target = folder / SCOPE_FILE[scope]
    try:
        ipc.write_json(target, record, create_only=True, fsync=True)
        return True, target, record
    except FileExistsError:
        return False, target, record


def write_torn(folder, name="stop.json", raw=b'{"cause":'):
    """Drill only (T03-11): torn bytes, bypassing os.link; still create-if-absent."""
    with (Path(folder) / name).open("xb") as stream:
        stream.write(raw)


def main(argv=None):
    p = argparse.ArgumentParser(description="Create a stop file (never overwrites an existing one).")
    p.add_argument("--folder", required=True, help="mod command folder (next to session.json)")
    p.add_argument("--cause", default="operator", choices=sorted(CAUSE_SCOPE))
    p.add_argument("--save-id", required=True)
    p.add_argument("--player-id", required=True, type=int)
    p.add_argument("--writer", default="arti_stop", choices=WRITERS, help="safety/launcher: FakeCore drills only")
    p.add_argument("--stop-id")
    p.add_argument("--reason")
    p.add_argument("--torn", action="store_true", help="drill T03-11: write torn bytes to stop.json")
    a = p.parse_args(argv)
    if a.torn:
        write_torn(a.folder)
        print("torn stop.json written")
        return 0
    created, path, record = write_stop(a.folder, a.cause, save_id=a.save_id, player_id=a.player_id, writer=a.writer,
                                       stop_id=a.stop_id, reason=a.reason)
    print(("created " if created else "exists, not overwritten: ") + str(path), json.dumps(record))
    return 0 if created else 1


if __name__ == "__main__":
    sys.exit(main())
