"""Provider-evidence byte accounting (ADR fix115a); no evidence or admission authority."""
from contextlib import contextmanager
import fnmatch
import json
import os
from pathlib import Path
import sqlite3


def root_for(directory):
    journal = directory.parent
    if journal.name == "journal" and len(journal.parents) >= 5 and journal.parents[3].name == "runtime":
        return journal.parents[4], True
    return journal, False


def stamp(folder):
    try:
        s = folder.stat()
        return str((s.st_dev, s.st_ino, s.st_mtime_ns))
    except FileNotFoundError:
        return "missing"


def census(folder):
    """Read a packed index once, never once per logical file (ADR 193).

    Raw evidence is create-once. scandir supplies Windows file sizes from the
    directory enumeration; it does not open/stat each member through storage.
    """
    from scripts.free_play import storage
    try:
        with (folder / storage.INDEX).open(encoding="utf-8") as stream:
            doc = json.load(stream)
    except FileNotFoundError:
        doc = None
    if doc is not None:
        # Keep storage's index validation, without resolving each logical file.
        doc, _ = storage.index(folder)
        return sum(doc["files"][p.relative_to(folder).as_posix()]["bytes"]
                   for p in storage.files(folder, "*-*.json"))
    if not folder.exists():
        return 0
    total = 0
    for base, _, _ in os.walk(folder):
        with os.scandir(base) as entries:
            total += sum(e.stat().st_size for e in entries
                         if fnmatch.fnmatchcase(e.name, "*-*.json") and e.is_file())
    return total


@contextmanager
def accounting(directory):
    """Serialize cooperating processes; reconcile external creates and packing.

    The sidecar is disposable. A crash between evidence creation and commit
    leaves a changed directory stamp, so the next transaction recounts it.
    Directory probes scale with days; file stats on an unchanged write do not.
    """
    root, run = root_for(directory)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / ".provider-archive-budget.sqlite3", timeout=30)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY, stamp TEXT, bytes INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS total (id INTEGER PRIMARY KEY CHECK (id=1), bytes INTEGER)")
        db.execute("INSERT OR IGNORE INTO total SELECT 1, COALESCE(SUM(bytes), 0) FROM folders")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        folders = list(root.glob("runtime/live7/*/day-*/journal/provider_archive")) if run else [directory]
        if directory not in folders:
            folders.append(directory)
        rows = {p: (s, n) for p, s, n in db.execute("SELECT path, stamp, bytes FROM folders")}
        total = db.execute("SELECT bytes FROM total WHERE id=1").fetchone()[0]
        current = {str(p) for p in folders}
        for path in rows.keys() - current:
            total -= rows[path][1]
            db.execute("DELETE FROM folders WHERE path=?", (path,))
        own = 0
        for folder in folders:
            path, revision = str(folder), stamp(folder)
            old_stamp, size = rows.get(path, (None, 0))
            if revision != old_stamp:
                updated = census(folder)
                total += updated - size
                size = updated
                db.execute("INSERT OR REPLACE INTO folders VALUES (?, ?, ?)", (path, revision, size))
            if folder == directory:
                own = size

        def added(count):
            db.execute("INSERT OR REPLACE INTO folders VALUES (?, ?, ?)",
                       (str(directory), stamp(directory), own + count))
            db.execute("UPDATE total SET bytes=? WHERE id=1", (total + count,))

        db.execute("UPDATE total SET bytes=? WHERE id=1", (total,))
        yield own, total, added
        db.commit()
    finally:
        db.close()
