"""Read-only segment inventory for explicit evidence copies; never discovers a live journal.

Semantic recovery is provided by CommandSettledJournal.ReadSnapshotForReplay. The gameplay
evaluator's g1_journal.jsonl is a different journal and retains its existing evidence references.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.free_play import storage

NAME = re.compile(r"[0-9]{19}-[0-9a-f]{32}\Z")


def digest(path):
    return storage.digest_file(path)


def head(path):
    try:
        envelope = json.loads(storage.read_bytes(path).decode("utf-8-sig"))
        payload = base64.b64decode(envelope["payloadBase64"], validate=True)
        if envelope["schemaVersion"] != 1 or hashlib.sha256(payload).hexdigest() != envelope["sha256"]:
            return None
        value = json.loads(payload)
        names = [value["active"], *value["retired"]]
        if value["generation"] < 1 or value["checkpointBytes"] < 1 or len(set(names)) != len(names):
            return None
        return value if all(NAME.fullmatch(n) for n in names) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def inventory(logical, *, verify_closed=True, resolve=lambda p: p):
    logical = Path(logical)
    heads = [head(resolve(Path(str(logical) + suffix))) for suffix in (".head", ".head.bak")]
    valid = [h for h in heads if h is not None]
    if not valid and any(storage.exists(resolve(Path(str(logical) + s))) for s in (".head", ".head.bak")):
        raise ValueError("invalid command journal heads")
    selected = max(valid, key=lambda h: h["generation"]) if valid else None
    if len(valid) == 2 and valid[0]["generation"] == valid[1]["generation"] and valid[0] != valid[1]:
        raise ValueError("conflicting command journal heads")
    root = Path(str(logical) + ".segments")
    active = root / selected["active"] / "journal.jsonl" if selected else logical
    copies = [resolve(p) for p in (active, Path(str(active) + ".bak")) if storage.exists(resolve(p))]
    if not copies:
        raise ValueError("missing active command journal")
    closed = []
    for name in selected["retired"] if selected else []:
        primary = root / name / "journal.jsonl"
        description = json.loads(storage.read_bytes(resolve(Path(str(primary) + ".closed.json"))).decode("utf-8-sig"))
        for role, suffix in (("primary", ""), ("mirror", ".bak")):
            file = resolve(Path(str(primary) + suffix))
            expected = description[role]
            if verify_closed and (storage.file_size(file) != expected["bytes"] or digest(file) != expected["sha256"]):
                raise ValueError("closed command journal hash mismatch")
        closed.append(dict(segment=name, **description))
    return dict(segmented=selected is not None, generation=selected["generation"] if selected else 0,
                heads_agree=heads[0] is not None and heads[0] == heads[1],
                active=[dict(path=str(p), bytes=storage.file_size(p)) for p in copies], closed=closed)


def inventory_run(run, *, phase="preserved", verify_closed=True):
    """Read the harness file-set mounts directly, without copying journal history."""
    run = Path(run)
    if phase not in {"baseline", "preserved"}:
        raise ValueError("unknown journal phase")
    name = "command-settled-journal-v1.json"
    def mount(path):
        rel = path.relative_to(run)
        first, *tail = rel.parts
        key = "command_segments" if first == name + ".segments" else "command_journal"
        root = run / "baseline" / key if phase == "baseline" else run / ("preserved-" + key)
        return root.joinpath(*tail) if key == "command_segments" else root / first
    return inventory(run / name, verify_closed=verify_closed, resolve=mount)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, nargs="?", help="logical path in an explicitly copied evidence directory")
    parser.add_argument("--run", type=Path, help="explicit retained harness run with mounted file sets")
    parser.add_argument("--phase", choices=("baseline", "preserved"), default="preserved")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.run) == bool(args.journal):
        parser.error("provide exactly one journal or --run")
    result = inventory_run(args.run, phase=args.phase) if args.run else inventory(args.journal)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
