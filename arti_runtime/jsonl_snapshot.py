"""ADR 231 part C: one parsed snapshot per JSONL file revision, extended by its appended lines only.

Attempt 87 (90 minutes) against attempt 86 (30 minutes): the page's world offer grew from a median of 770 ms to
1,949 ms, day by day (877 ms on the first game day, 4,626 ms on the seventh). Every page re-read and re-parsed her
whole first-seen record (``discoveries.jsonl``) and the game-text ledger many times over, and looked each delivered
text up with one more full ledger read per pointer.

``rows(path)`` returns exactly what ``arti_state.read_jsonl(path)`` returns (rows by id, the last line wins, the
position of the first appearance kept, unreadable lines skipped), from a snapshot keyed by the file's identity:
- the same identity, size and mtime: the snapshot itself, nothing is read;
- the same file (inode/device), grown, and the bytes just before the old end unchanged: only the appended complete
  lines are parsed (every writer that appends writes whole lines, ADR 226);
- anything else (replaced, truncated, rewritten, a different tail): a full parse.
A line without its newline yet is left for the next read, as ``read_jsonl`` skips an unparsable line. The rows are
shared, read-only snapshots: a caller that changes a row copies it first.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

TAIL = 64  # bytes before the old end that must still be there for an append-only extension
_CACHE: dict = {}
_LOCK = threading.Lock()


def _key(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _parse(data: bytes, by_id: dict, *, first: bool) -> None:
    text = data.decode("utf-8", errors="replace")
    if first and text.startswith("﻿"):
        text = text[1:]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
            by_id[row["id"]] = row


def rows(path) -> list[dict]:
    """``arti_state.read_jsonl(path)``'s rows, read once per file revision (see the module text)."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return []
    if not path.is_file():
        return []
    key = _key(path)
    with _LOCK:
        entry = _CACHE.get(key)
        same_file = entry is not None and (entry["ino"], entry["dev"]) == (stat.st_ino, stat.st_dev)
        if same_file and (stat.st_size, stat.st_mtime_ns) == (entry["size"], entry["mtime"]):
            return list(entry["rows"])
        try:
            with path.open("rb") as stream:
                if same_file and entry["consumed"] <= stat.st_size and entry["consumed"] >= len(entry["tail"]):
                    stream.seek(entry["consumed"] - len(entry["tail"]))
                    if stream.read(len(entry["tail"])) == entry["tail"]:
                        added = stream.read()
                        end = added.rfind(b"\n") + 1
                        by_id = dict(entry["by_id"])
                        _parse(added[:end], by_id, first=False)
                        consumed = entry["consumed"] + end
                        tail = (entry["tail"] + added[:end])[-TAIL:]
                        return _keep(key, stat, by_id, consumed, tail)
                stream.seek(0)
                data = stream.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        by_id = {}
        _parse(data[:end], by_id, first=True)
        if end < len(data):
            last = {}
            _parse(data[end:], last, first=end == 0)
            if last:
                # read_jsonl also reads a complete last line that has no newline yet: so does this read, and the
                # file is read whole again next time (no snapshot is kept for it)
                merged = dict(by_id)
                merged.update(last)
                _CACHE.pop(key, None)
                return list(merged.values())
        return _keep(key, stat, by_id, end, data[:end][-TAIL:])


def _keep(key, stat, by_id, consumed, tail):
    snapshot = tuple(by_id.values())
    _CACHE[key] = dict(ino=stat.st_ino, dev=stat.st_dev, size=stat.st_size, mtime=stat.st_mtime_ns,
                       consumed=consumed, tail=tail, by_id=by_id, rows=snapshot)
    return list(snapshot)


def clear() -> None:
    """Tests only: forget every snapshot."""
    with _LOCK:
        _CACHE.clear()
