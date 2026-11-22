"""One-time farm tags for the mabar session notes already in a vault (ADR 238). DRY RUN by default: writes nothing.

    python -B -m scripts.livepath.vault_farm_backfill --vault <vault folder> --runs <runs folder> [--runs ...]
        [--saves <Stardew Saves folder>] [--inbox <farmhand mod inbox>] [--apply --backup <dated backup folder>]

It reads ``<vault>/sessions/*.md`` (``index.md`` and ``*_beats.md`` are left out: beats keep their own label) and
finds the mabar run each note came from:

- the bridge's note is named after the run folder (mabar sets ``stream_session_id`` to the run name);
- mabar's own note is ``<YYYY-MM-DD>-mabar.md``, the WIB date the run started (``mabar_memory.session_id``). A
  run started that date is not enough, even when it is the only one: the run must also hold a byte-identical
  ``vault-export`` copy of the note, or its ``mabar.jsonl`` must say it created the note. Several runs with that
  evidence place the note only if they are the same farm.

The farm comes from that run the way mabar finds it: the continued save's farm (``continued.json``), else the farm her
verified self facts name (``self_facts.json``); the scope hashes are ``mabar.run_scope_hashes(run, current=False)``
(the farmhand occurrence rings of the run's save). When ``continued.json`` names no save and two saves carry that
farm name, the note is unplaced (``mabar.run_save`` would take the first).

Output, one tab-separated line per note, then one summary line:

    placed    <note>  farm=<name>  scope_hash=<h>[,<h>...]  evidence=<how the run, the farm and the scope were found>
    unplaced  <note>  why=<why no farm can be named>
    tagged    <note>  farm=<name>  scope_hash=<h>[,...]     (already carries a tag; left as it is)

``--apply`` needs ``--backup DIR``: DIR's name carries a date (YYYY-MM-DD or YYYYMMDD), DIR is not inside the vault,
and DIR holds a byte-identical copy (same file name, same sha256) of every note it would change. Otherwise it refuses
and writes nothing. Then each placed note gets only its tag line, under its title; nothing else in it changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime import vault_farm  # noqa: E402

DATED = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8})(?!\d)")
MABAR_NOTE = re.compile(r"\d{4}-\d{2}-\d{2}-mabar")
REFUSED = 3


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_runs(roots):
    """Run folders under each root (a root that is itself a run counts as one). Missing roots are reported."""
    runs, missing = [], []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            missing.append(str(root))
        elif (root / "MODE.json").is_file():
            runs.append(root)
        else:
            runs.extend(sorted(p for p in root.iterdir() if p.is_dir()))
    return runs, missing


def run_session(run):
    """``mabar_memory.session_id(run)``, but None (never today's date) without a readable MODE.json start."""
    import datetime as dt
    from scripts.livepath import mabar_memory
    stamp = (_json(Path(run) / "MODE.json") or {}).get("utc")
    try:
        start = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None
    return start.astimezone(mabar_memory.WIB).strftime("%Y-%m-%d") + "-mabar"


def logged_created(run, name):
    """The run's mabar.jsonl says it created a vault note with this file name (auto_exported / exported_to_live)."""
    try:
        lines = (Path(run) / "mabar.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("event") in ("auto_exported", "exported_to_live")
                and row.get("note") == "created" and re.split(r"[\\/]", str(row.get("note_target") or ""))[-1] == name):
            return True
    return False


def same_name_saves(farm, saves):
    """The save folders whose farm name is ``farm`` (the folders ``mabar.run_save`` chooses from)."""
    from scripts.livepath import mabar
    found = []
    for folder in sorted((Path(saves) if saves else mabar.APPDATA / "Saves").glob("*_*")):
        try:
            if vault_farm.clean_name(mabar.saved_farm(folder)["farm"]) == farm:
                found.append(folder.name)
        except (RuntimeError, OSError, ValueError):
            continue
    return found


def run_farm(run, saves, inbox_dir):
    """(name, scopes, evidence) for a run, or (None, None, why)."""
    from scripts.livepath import mabar
    run = Path(run)
    continued = _json(run / "continued.json")
    name = vault_farm.clean_name((continued or {}).get("farm"))
    if name:
        how = "farm from continued.json"
    else:
        name, how = vault_farm.self_facts_farm(run / "self_facts.json"), "farm from self_facts.json"
    if not name:
        return None, None, "run %s: no farm name (no farm in continued.json or self_facts.json)" % run.name
    if inbox_dir is None:
        return None, None, "run %s: farmhand mod inbox unknown (pass --inbox)" % run.name
    if not (continued or {}).get("save"):
        # mabar.run_save takes the first save with this farm name; with two (a test copy of a farm) that may be the
        # wrong save's rings, so the note stays unplaced.
        same = same_name_saves(name, saves)
        if len(same) > 1:
            return None, None, "run %s: farm %s, %d saves carry that farm name (%s) and continued.json names none" % (
                run.name, name, len(same), ", ".join(same))
    try:
        save = mabar.run_save(run, saves)
        scopes = sorted(mabar.run_scope_hashes(run, current=False, saves=saves, inbox_dir=inbox_dir))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return None, None, "run %s: scope not readable (%s)" % (run.name, type(exc).__name__)
    if not save:
        return None, None, "run %s: farm %s, but no save folder found for it" % (run.name, name)
    if not scopes:
        return None, None, "run %s: farm %s, save %s, but no farmhand occurrence ring for it" % (run.name, name, save)
    return name, scopes, "%s, save %s, %d farmhand ring(s)" % (how, save, len(scopes))


def plan_note(note, runs, saves, inbox_dir):
    """One row: dict(state=placed|unplaced|tagged, note, ...)."""
    note = Path(note)
    try:
        data = note.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return dict(state="unplaced", note=note, why="not readable as UTF-8 (%s)" % type(exc).__name__)
    tag = vault_farm.parse_tag(text)
    if tag is not None:
        return dict(state="tagged", note=note, farm=tag["farm"], scopes=list(tag["scope_hashes"]))
    if "\n" not in text:
        return dict(state="unplaced", note=note, why="one-line note")
    sid = note.stem[:-len("_reflection")] if note.stem.endswith("_reflection") else note.stem
    named = [r for r in runs if r.name == sid]
    if named:
        candidates, how = named, "note named after run folder"
    elif MABAR_NOTE.fullmatch(sid):
        candidates, how = [r for r in runs if run_session(r) == sid], "run started on the note's WIB date (MODE.json)"
    else:
        candidates, how = [], ""
    if not candidates:
        return dict(state="unplaced", note=note, why="no mabar run with session id %s" % sid)
    same = [r for r in candidates if (r / "vault-export" / note.name).is_file()
            and _sha((r / "vault-export" / note.name).read_bytes()) == _sha(data)]
    if named:
        if same:
            how += ", its vault-export copy is byte-identical"
    else:
        # A dated note belongs to the first run that day that exported it (export_to_live never overwrites). A run
        # started that day is not enough: the run that made the note may be offloaded or not passed in --runs.
        made = [r for r in candidates if logged_created(r, note.name)]
        if same:
            candidates, how = same, how + ", its vault-export copy is byte-identical"
        elif made:
            candidates, how = made, how + ", its mabar.jsonl logged creating this note"
        else:
            return dict(state="unplaced", note=note, why=(
                "%d run(s) started on the note's WIB date (%s), but none has a byte-identical vault-export copy "
                "or logged creating it" % (len(candidates), ", ".join(r.name for r in candidates))))
    farms = [(r,) + run_farm(r, saves, inbox_dir) for r in candidates]
    bad = [f for f in farms if f[1] is None]
    if bad:
        return dict(state="unplaced", note=note, why="; ".join(f[3] for f in bad))
    if len({(f[1], tuple(f[2])) for f in farms}) != 1:
        return dict(state="unplaced", note=note, why="%d runs with session id %s are different farms: %s" % (
            len(farms), sid, ", ".join("%s=%s" % (f[0].name, f[1]) for f in farms)))
    _, name, scopes, farm_how = farms[0]
    runs_named = ", ".join(r.name for r in candidates)
    evidence = ("run %s (%s); %s" % (runs_named, how, farm_how) if len(candidates) == 1 else
                "runs %s (%s; all the same farm); %s" % (runs_named, how, farm_how))
    return dict(state="placed", note=note, farm=name, scopes=scopes, evidence=evidence, sha256=_sha(data),
                line=vault_farm.tag_line(name, scopes))


def plan(vault, runs, saves, inbox_dir):
    sessions = Path(vault) / "sessions"
    notes = sorted(p for p in sessions.glob("*.md") if p.is_file() and p.name.lower() != "index.md"
                   and not p.name.endswith("_beats.md"))
    return [plan_note(n, runs, saves, inbox_dir) for n in notes]


def render(row):
    if row["state"] == "unplaced":
        return "unplaced\t%s\twhy=%s" % (row["note"], row["why"])
    line = "%s\t%s\tfarm=%s\tscope_hash=%s" % (row["state"], row["note"], row["farm"], ",".join(row["scopes"]))
    return line + ("\tevidence=%s" % row["evidence"] if row["state"] == "placed" else "")


def backup_refusal(backup, vault, placed):
    """Why --apply must not write, or None."""
    if backup is None:
        return "--apply needs --backup DIR (a dated folder holding a copy of every note it would change)"
    backup, vault = Path(backup), Path(vault)
    if not backup.is_dir():
        return "backup folder not found: %s" % backup
    if not DATED.search(backup.name):
        return "backup folder name carries no date (YYYY-MM-DD or YYYYMMDD): %s" % backup.name
    b, v = backup.resolve(), vault.resolve()
    if b == v or v in b.parents:
        return "backup folder is inside the vault (its copies would be indexed as her memories): %s" % backup
    copies = {}
    for f in b.rglob("*"):
        if f.is_file():
            copies.setdefault(f.name, []).append(f)
    missing = [str(row["note"]) for row in placed
               if not any(_sha(c.read_bytes()) == row["sha256"] for c in copies.get(row["note"].name, ()))]
    if missing:
        return "no byte-identical copy in the backup for: %s" % ", ".join(missing)
    return None


def apply(placed):
    """Add each note's tag line. Every note is re-read and its new bytes checked before the first write: unchanged
    since the plan, and the new note is the old one plus exactly the tag line. Returns the notes written."""
    staged = []
    for row in placed:
        data = row["note"].read_bytes()
        if _sha(data) != row["sha256"]:
            raise RuntimeError("note changed since it was checked: %s" % row["note"])
        new = vault_farm.add_tag(data.decode("utf-8"), row["line"]).encode("utf-8")
        nl = b"\r\n" if data.split(b"\n", 1)[0].endswith(b"\r") else b"\n"
        if new == data or new.replace(row["line"].encode("utf-8") + nl, b"", 1) != data:
            raise RuntimeError("tag insertion would change more than one line: %s" % row["note"])
        staged.append((row, new))
    written = []
    for row, new in staged:
        tmp = row["note"].with_name("." + row["note"].name + ".farmtag.tmp")
        tmp.write_bytes(new)
        os.replace(tmp, row["note"])
        if _sha(row["note"].read_bytes()) != _sha(new):
            raise RuntimeError("tagged note did not verify: %s" % row["note"])
        written.append(row["note"])
    return written


def default_inbox():
    try:
        from scripts.livepath import mabar
        return mabar.inbox()
    except Exception:  # noqa: BLE001 - no farmhand mod on this machine: the operator passes --inbox
        return None


def main(argv=None, out=print):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vault", type=Path, required=True, help="the vault folder that holds sessions/")
    p.add_argument("--runs", type=Path, action="append", default=[], required=True,
                   help="a folder of mabar run folders (repeat for each); a missing one is reported and skipped")
    p.add_argument("--saves", type=Path, default=None, help="Stardew Saves folder (default: this machine's)")
    p.add_argument("--inbox", type=Path, default=None, help="farmhand mod inbox (default: from the mod config)")
    p.add_argument("--apply", action="store_true", help="write the tag lines (needs --backup)")
    p.add_argument("--backup", type=Path, default=None, help="dated folder with a copy of every note it changes")
    a = p.parse_args(argv)
    if not (a.vault / "sessions").is_dir():
        out("refused\tno sessions folder under --vault: %s" % a.vault)
        return REFUSED
    if a.saves is None:
        from scripts.livepath import mabar
        a.saves = mabar.APPDATA / "Saves"
    runs, missing = find_runs(a.runs)
    for root in missing:
        out("skipped\truns folder not found: %s" % root)
    rows = plan(a.vault, runs, a.saves, a.inbox if a.inbox is not None else default_inbox())
    for row in rows:
        out(render(row))
    placed = [r for r in rows if r["state"] == "placed"]
    counts = "%d placed, %d unplaced, %d already tagged" % (
        len(placed), sum(r["state"] == "unplaced" for r in rows), sum(r["state"] == "tagged" for r in rows))
    if not a.apply:
        out("dry run: %s; nothing written" % counts)
        return 0
    why = backup_refusal(a.backup, a.vault, placed)
    if why:
        out("refused\t%s; nothing written" % why)
        return REFUSED
    try:
        written = apply(placed)
    except RuntimeError as exc:
        out("refused\t%s" % exc)
        return REFUSED
    for note in written:
        out("applied\t%s" % note)
    out("applied: %d tagged (%s); backup %s" % (len(written), counts, a.backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
