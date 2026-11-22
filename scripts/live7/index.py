#!/usr/bin/env python
"""Live7 WP6: build the Stardew archive index (stdlib sqlite3 + FTS5 when available).

    index.py build [--archive-root R] [--db PATH] [--run DIR --cycle C ...] [--gap-s 5]
    index.py check-fts5

Sources per session (an archive `<root>/<cycle>/<day-trial>/` with manifest.json, or a raw run folder via --run):
journal (+ derived clock), trial report, provider archive, SMAPI logs, pad_watch, ui-op-log and the screenshot list.
The database is rebuilt from scratch into `<db>.tmp` and atomically replaced. Read it with `sv.py`.
Every row keeps the session's journal path and the journal line, so every answer can cite `path:L<line>`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.free_play import storage
import live7_common as C  # noqa: E402

SCHEMA_VERSION = 2
SMAPI_LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2}) (TRACE|DEBUG|INFO|WARN|ERROR|ALERT)\s+([^\]]+)\] ?(.*)$")
SMAPI_KEEP = {"INFO", "WARN", "ERROR", "ALERT"}

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sessions(id INTEGER PRIMARY KEY, cycle TEXT, day_trial TEXT, root TEXT, journal TEXT, archived INTEGER,
  code_sha TEXT, dirty TEXT, dll_sha TEXT, profile TEXT, save_name TEXT, wall_start TEXT, wall_end TEXT,
  status TEXT, reason TEXT, terminal_line INTEGER, inputs INTEGER, input_cap INTEGER, starts INTEGER, start_cap INTEGER,
  provider INTEGER, provider_cap INTEGER, elapsed_s REAL, seconds_cap REAL, rate REAL, rate_src TEXT, lines INTEGER,
  UNIQUE(root));
CREATE TABLE days(session_id INTEGER, date TEXT, date_key TEXT, weekday TEXT, weather TEXT, first_line INTEGER,
  last_line INTEGER, wake_time TEXT, bed_time TEXT, sleep_line INTEGER, m1 INTEGER, m2 INTEGER, m3 INTEGER, m4 INTEGER,
  m5 INTEGER, m6 INTEGER, m7 INTEGER, m8 INTEGER);
CREATE TABLE skill_runs(session_id INTEGER, run_id INTEGER, skill TEXT, arg TEXT, start_skill TEXT, task TEXT, by TEXT,
  chooser_src TEXT, request_index INTEGER, start_line INTEGER, end_line INTEGER, date TEXT, date_key TEXT,
  start_time TEXT, end_time TEXT, start_min REAL, end_min REAL, start_loc TEXT, end_loc TEXT, inputs INTEGER,
  decisions INTEGER, class TEXT, verdict TEXT, reason TEXT, cause_line INTEGER, legs TEXT, wall_start REAL, wall_end REAL);
CREATE TABLE rows(session_id INTEGER, line INTEGER, part TEXT, task TEXT, day TEXT, verified INTEGER, reason TEXT,
  date TEXT, time TEXT, location TEXT);
CREATE TABLE decisions(session_id INTEGER, request_index INTEGER, line INTEGER, generation_id TEXT, lane TEXT,
  prompt_sha256 TEXT, prompt_bytes INTEGER, offered TEXT, choice_kind TEXT, chosen_skill TEXT, chosen_arg TEXT,
  speech TEXT, tokens TEXT, malformed TEXT, request_path TEXT, response_path TEXT);
CREATE TABLE events(session_id INTEGER, line INTEGER, wall REAL, date TEXT, date_key TEXT, time TEXT, time_min REAL,
  time_src TEXT, location TEXT, ev TEXT, reason TEXT, json TEXT);
CREATE TABLE errors(session_id INTEGER, source TEXT, code TEXT, text TEXT, line INTEGER, path TEXT, date TEXT,
  time TEXT, location TEXT);
CREATE TABLE gaps(session_id INTEGER, start_line INTEGER, end_line INTEGER, wall_s REAL, game_min REAL, kind TEXT,
  detail TEXT);
CREATE TABLE artifacts(session_id INTEGER, path TEXT, kind TEXT, sha256 TEXT, bytes INTEGER, wall REAL, wall_src TEXT,
  line INTEGER, date TEXT, time TEXT, location TEXT);
CREATE TABLE smapi(session_id INTEGER, path TEXT, line INTEGER, clock TEXT, level TEXT, source TEXT, text TEXT);
CREATE INDEX ev_idx ON events(session_id, line);
CREATE INDEX ev_date ON events(date_key, time_min);
CREATE INDEX run_skill ON skill_runs(skill, class);
"""

FTS_DDL = "CREATE VIRTUAL TABLE fts USING fts5(source, session_id UNINDEXED, ref UNINDEXED, body, tokenize='unicode61')"
FTS_FALLBACK_DDL = "CREATE TABLE fts(source TEXT, session_id INTEGER, ref TEXT, body TEXT)"


def fts5_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        con.close()
        return True
    except sqlite3.OperationalError:
        return False


def _j(v) -> str | None:
    return None if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))


def _error_rows(s: C.Session) -> list[tuple]:
    """(source, code, text, line) for journal errors."""
    out = []
    for d in s.lines:
        ev, n = C.ev_of(d), d["_line"]
        if ev == "skill_failed":
            out.append(("journal", d.get("reason"), f"{d.get('skill')} {d.get('arg')!s}: {d.get('reason')}", n))
        elif re.fullmatch(r"(part_[a-z0-9]+|live)_row", ev) and not d.get("verified"):
            out.append(("journal", d.get("reason"), f"row {d.get('task')}: {d.get('reason')}", n))
        elif ev == "skill_model_malformed":
            out.append(("journal", "model_malformed:" + str(d.get("code")), f"malformed model reply {d.get('code')}", n))
        elif ev == "approach_feedback" and str(d.get("status", "")).upper() in ("UNCERTAIN", "BLOCKED", "FAILED"):
            out.append(("journal", str(d.get("command_reason") or d.get("blocker")),
                        f"approach {d.get('status')} {d.get('blocker')} {d.get('command_reason')} {d.get('target_ref')}", n))
        elif ev == "action_result" and (d.get("action") or {}).get("lifecycle") == "interrupted":
            out.append(("journal", str(d["action"].get("reason")), f"action interrupted: {d['action'].get('reason')}", n))
        elif ev == "continuation_not_scheduled" and "PAUSED" in str(d.get("continuation_deny_site", "")):
            out.append(("journal", "runtime_paused_recovery", f"continuation denied {d.get('continuation_deny_site')}", n))
        elif ev in ("curfew", "skill_budget_exhausted", "no_progress_hold"):
            out.append(("journal", ev, C.compact(d, 300), n))
        elif ev == "skill_step_boundary" and d.get("outcome") == "fail":
            out.append(("journal", d.get("reason"), f"{d.get('skill')} step boundary fail: {d.get('reason')}", n))
        elif re.search(r"error|fault|exception", ev, re.I):
            out.append(("journal", ev, C.compact(d, 300), n))
    return out


def _smapi_files(root: Path) -> list[Path]:
    return [p for p in C.iter_files(root) if C.file_kind(p.relative_to(root).as_posix()) == "smapi_log"]


def _shot_walls(root: Path) -> dict[str, float]:
    """Screenshot wall times from ui-op-log `shot` records (path -> epoch)."""
    out = {}
    for p in C.iter_files(root):
        if p.name != "ui-op-log.jsonl":
            continue
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("op") == "shot" and d.get("out"):
                w = C.parse_iso_utc(d.get("utc"))
                if w is not None:
                    out[Path(str(d["out"]).replace("\\", "/")).name + "|" + Path(str(d["out"]).replace("\\", "/")).parent.name] = w
    return out


def load_provider(path):
    from scripts.free_play import storage
    return json.loads(storage.read_bytes(path).decode("utf-8-sig")) if path and storage.exists(path) else None


def _decisions(s: C.Session) -> list[dict]:
    from scripts.free_play import storage
    pa = s.root / "journal" / "provider_archive"
    req_line = {}
    for d in s.lines:
        if C.ev_of(d) == "trial_reservation" and d.get("kind") == "provider" and isinstance(d.get("index"), int):
            req_line.setdefault(d["index"], d["_line"])
    # journal decision lines after each provider reservation
    after: dict[int, dict] = {}
    cur = None
    for d in s.lines:
        ev = C.ev_of(d)
        if ev == "trial_reservation" and d.get("kind") == "provider":
            cur = d.get("index")
        elif cur is not None and ev in ("skill_offer",):
            pass
        elif cur is not None and ev == "skill_model_malformed":
            after.setdefault(cur, {})["malformed"] = d.get("code")
        elif cur is not None and ev == "skill_model_decision":
            after.setdefault(cur, {})["choice"] = d.get("choice")
            after[cur]["trigger"] = d.get("trigger")
    offers = {}
    last_offer = None
    for d in s.lines:
        ev = C.ev_of(d)
        if ev == "skill_offer":
            last_offer = d.get("skills")
        elif ev == "trial_reservation" and d.get("kind") == "provider":
            offers[d.get("index")] = last_offer
    out = []
    indexes = set(req_line)
    files = {}
    if pa.is_dir():
        for p in storage.files(pa, "*.json"):
            m = re.match(r"(\d+)-(request|response)\.json$", p.name)
            if m:
                files.setdefault(int(m.group(1)), {})[m.group(2)] = p
        indexes |= set(files)
    for idx in sorted(indexes):
        rec = {"request_index": idx, "line": req_line.get(idx), "offered": offers.get(idx)}
        req = load_provider(files.get(idx, {}).get("request"))
        resp = load_provider(files.get(idx, {}).get("response"))
        if req:
            content = req.get("content") or {}
            prompt = str(content.get("prompt") or "")
            rec.update(generation_id=req.get("generation_id"), lane=req.get("lane"),
                       prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(), prompt_bytes=len(prompt.encode("utf-8")))
        if resp:
            content = resp.get("content") or {}
            raw = content.get("raw_response")
            parsed = None
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
            elif isinstance(raw, dict):
                parsed = raw
            if isinstance(parsed, dict):
                choice = parsed.get("choice") or {}
                args = choice.get("args") or {}
                rec.update(choice_kind=choice.get("kind"), chosen_skill=args.get("name"), chosen_arg=args.get("arg"),
                           speech=parsed.get("speech") or None)
            usage = content.get("usage") or resp.get("usage")
            if usage:
                rec["tokens"] = usage
        j = after.get(idx) or {}
        if j.get("malformed"):
            rec["malformed"] = j["malformed"]
        if not rec.get("chosen_skill") and isinstance(j.get("choice"), list) and j["choice"]:
            rec["chosen_skill"] = j["choice"][0]
            rec["chosen_arg"] = j["choice"][1] if len(j["choice"]) > 1 else None
        for k in ("request", "response"):
            p = files.get(idx, {}).get(k)
            rec[k + "_path"] = p.relative_to(s.root).as_posix() if p else None
        out.append(rec)
    return out


def index_session(con: sqlite3.Connection, s: C.Session, archived: bool, fts: bool) -> int:
    report = s.report or {}
    manifest = s.manifest or {}
    ident = report.get("identity") or {}
    limits = report.get("limits") or {}
    skills = ((report.get("skills") or {}).get("session") or {})
    walls = [r.wall for r in s.clock.rows if r.wall is not None]
    save = None
    for d in s.lines[:50]:
        if d.get("save_name"):
            save = d["save_name"]
    cur = con.execute(
        "INSERT INTO sessions(cycle, day_trial, root, journal, archived, code_sha, dirty, dll_sha, profile, save_name,"
        " wall_start, wall_end, status, reason, terminal_line, inputs, input_cap, starts, start_cap, provider,"
        " provider_cap, elapsed_s, seconds_cap, rate, rate_src, lines) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s.cycle, s.day_trial, str(s.root.resolve()), s.cite_path, int(archived),
         ident.get("source") or (manifest.get("code") or {}).get("sha"), _j(ident.get("dirty")),
         ident.get("dll") or (manifest.get("dll") or {}).get("sha256"), _j(report.get("profile")), save,
         C.utc_iso(min(walls)) if walls else None, C.utc_iso(max(walls)) if walls else None,
         report.get("status"), s.terminal.get("reason"), s.terminal.get("line"), report.get("physical_inputs"),
         limits.get("input"), skills.get("skills_run", len(s.runs)), limits.get("skills"),
         (report.get("reserved") or {}).get("provider"), limits.get("provider"), report.get("elapsed_seconds"),
         limits.get("seconds"), s.clock.rate, s.clock.rate_src, len(s.lines)))
    sid = cur.lastrowid

    def at(line):
        r = s.clock.at_line(line) if line else None
        return r

    def fts_add(source, ref, body):
        if body:
            con.execute("INSERT INTO fts(source, session_id, ref, body) VALUES (?,?,?,?)", (source, sid, ref, body))

    for d, r in zip(s.lines, s.clock.rows):
        ev = C.ev_of(d)
        reason = C.reason_of(d)
        js = C.compact(d)
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, r.line, r.wall, C.date_label(r.date) if r.date else None, C.date_key(r.date),
                     C.fmt_min(r.time_min) if r.time_min is not None else None, r.time_min, r.time_src, r.location,
                     ev, reason, js))
        if ev not in ("trial_reservation", "trial_decision", "approach_proposal"):
            fts_add("journal", f"L{r.line}", f"{ev} {reason or ''} {js}")
    for run in s.runs:
        a, b = at(run.start_line), at(run.end_line)
        con.execute("INSERT INTO skill_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, run.run_id, run.skill, _j(run.arg), run.start_skill, run.task, run.by, run.chooser_src,
                     run.request_index, run.start_line, run.end_line, C.date_label(a.date) if a and a.date else None,
                     C.date_key(a.date) if a else None, C.fmt_min(a.time_min) if a and a.time_min is not None else None,
                     C.fmt_min(b.time_min) if b and b.time_min is not None else None, a.time_min if a else None,
                     b.time_min if b else None, a.location if a else None, b.location if b else None, run.inputs,
                     run.decisions, run.cls, run.verdict, run.reason, run.cause_line, _j(run.legs),
                     a.wall if a else None, b.wall if b else None))
    for row in s.rows:
        r = at(row.line)
        con.execute("INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, row.line, row.part, row.task, _j(row.day), int(row.verified), row.reason,
                     C.date_label(r.date) if r and r.date else None,
                     C.fmt_min(r.time_min) if r and r.time_min is not None else None, r.location if r else None))
    for g in s.gaps:
        con.execute("INSERT INTO gaps VALUES (?,?,?,?,?,?,?)", (sid, g.start_line, g.end_line, g.wall_s, g.game_min,
                                                                g.kind, g.detail))
    for source, code, text, line in _error_rows(s):
        r = at(line)
        con.execute("INSERT INTO errors VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, source, code, text, line, s.cite_path, C.date_label(r.date) if r and r.date else None,
                     C.fmt_min(r.time_min) if r and r.time_min is not None else None, r.location if r else None))
    for dec in _decisions(s):
        con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, dec["request_index"], dec.get("line"), dec.get("generation_id"), dec.get("lane"),
                     dec.get("prompt_sha256"), dec.get("prompt_bytes"), _j(dec.get("offered")), dec.get("choice_kind"),
                     dec.get("chosen_skill"), _j(dec.get("chosen_arg")), dec.get("speech"), _j(dec.get("tokens")),
                     dec.get("malformed"), dec.get("request_path"), dec.get("response_path")))
        if dec.get("speech"):
            fts_add("speech", f"request {dec['request_index']}", dec["speech"])
    if C.is_live7(s):
        # Same strict PLAN §1 M1-M8 as report.py (rehearsal 1: the lenient table marked M4 met).
        label = (s.manifest or {}).get("cycle")
        cycles = C.cycle_evidence_for(C.discover_run_evidence(s.root.parent.parent, label)) if archived and label else {}
        rows = C.live7_day_rows(s, archived, C.session_cycle_evidence(s, s.root, cycles), indexed=True)
    else:
        rows = C.day_rows(s, archived)
    for dr in rows:
        con.execute("INSERT INTO days VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, C.date_label(dr["date"]), C.date_key(dr["date"]), dr["weekday"], dr["weather"],
                     dr["first_line"], dr["last_line"], dr["wake_time"], dr["bed_time"], dr["sleep_line"],
                     *[None if dr[f"m{i}"] is None else int(bool(dr[f"m{i}"])) for i in range(1, 9)]))
    # SMAPI logs: keep INFO/WARN/ERROR/ALERT lines (TRACE/DEBUG are bulk).
    for p in _smapi_files(s.root):
        rel = p.relative_to(s.root).as_posix()
        with open(p, encoding="utf-8", errors="replace") as fh:
            for n, raw in enumerate(fh, 1):
                m = SMAPI_LINE.match(raw.rstrip("\n"))
                if not m or m.group(2) not in SMAPI_KEEP:
                    continue
                clock, level, src, text = m.groups()
                con.execute("INSERT INTO smapi VALUES (?,?,?,?,?,?,?)", (sid, rel, n, clock, level, src.strip(), text))
                fts_add("smapi", f"{rel}:L{n}", f"{level} {src.strip()} {text}")
                if level in ("ERROR", "ALERT"):
                    con.execute("INSERT INTO errors VALUES (?,?,?,?,?,?,?,?,?)",
                                (sid, "smapi", f"{level} {src.strip()}", text[:500], n, rel, None, None, None))
    # pad_watch violations
    for p in C.iter_files(s.root):
        rel = p.relative_to(s.root).as_posix()
        if C.file_kind(rel) != "pad_watch":
            continue
        with open(p, encoding="utf-8", errors="replace") as fh:
            for n, raw in enumerate(fh, 1):
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("violations"):
                    con.execute("INSERT INTO errors VALUES (?,?,?,?,?,?,?,?,?)",
                                (sid, "pad_watch", "violation", _j(d["violations"])[:500], n, rel, None, None, None))
    # artifacts (from the manifest when archived; else the folder)
    files = manifest.get("files") or [{"path": p.relative_to(s.root).as_posix(), "bytes": storage.file_stat(p).st_size,
                                       "sha256": None, "mtime": storage.file_stat(p).st_mtime,
                                       "kind": C.file_kind(p.relative_to(s.root).as_posix())}
                                      for p in C.iter_files(s.root)]
    shots = _shot_walls(s.root)
    for f in files:
        wall, wsrc, line, r = None, None, None, None
        if f.get("kind") == "screenshot":
            pp = Path(f["path"])
            key = pp.name + "|" + pp.parent.name
            if key in shots:
                wall, wsrc = shots[key], "ui_op_log"
            elif f.get("mtime"):
                wall, wsrc = float(f["mtime"]), "mtime"
            if wall is not None and walls and min(walls) - 5 <= wall <= max(walls) + 5:
                line = s.clock.line_for_wall(wall)
                r = at(line)
        con.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, f["path"], f.get("kind"), f.get("sha256"), f.get("bytes"), wall, wsrc, line,
                     C.date_label(r.date) if r and r.date else None,
                     C.fmt_min(r.time_min) if r and r.time_min is not None else None, r.location if r else None))
    return sid


def build(db: Path, sessions: list[tuple[Path, str | None, str | None, bool]], archive_root: Path | None,
          gap_s: float = C.DEFAULT_GAP_S) -> dict:
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(db.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(DDL)
    has_fts = fts5_available()
    con.execute(FTS_DDL if has_fts else FTS_FALLBACK_DDL)
    summary = {"sessions": [], "fts5": has_fts}
    seen = set()
    for root, cycle, day_trial, archived in sessions:
        run_key = os.path.normcase(str(root.resolve()))
        if run_key in seen:
            continue
        seen.add(run_key)
        cite_base = archive_root if archived and archive_root else None
        s = C.load_session(root, cycle, day_trial, cite_base=cite_base, gap_s=gap_s)
        index_session(con, s, archived, has_fts)
        summary["sessions"].append({"cycle": s.cycle, "day_trial": s.day_trial, "lines": len(s.lines),
                                    "runs": len(s.runs), "journal": s.cite_path})
    meta = {"schema_version": SCHEMA_VERSION, "tool": C.TOOL_VERSION, "fts5": int(has_fts),
            "archive_root": str(archive_root) if archive_root else ""}
    con.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    con.commit()
    con.close()
    os.replace(tmp, db)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--archive-root", help=f"default ${C.ARCHIVE_ENV} or <repo parent>/arti-archive")
    b.add_argument("--db", help="default <archive-root>/index.sqlite")
    b.add_argument("--run", action="append", default=[], help="also index a raw run folder (repeatable)")
    b.add_argument("--cycle", action="append", default=[], help="cycle label for each --run (same order)")
    b.add_argument("--no-archive", action="store_true", help="index only --run folders")
    b.add_argument("--gap-s", type=float, default=C.DEFAULT_GAP_S)
    sub.add_parser("check-fts5")
    args = ap.parse_args(argv)
    if args.cmd == "check-fts5":
        ok = fts5_available()
        print(f"sqlite {sqlite3.sqlite_version}: FTS5 {'available' if ok else 'NOT available (LIKE fallback)'}")
        return 0
    root = Path(args.archive_root) if args.archive_root else C.default_archive_root()
    db = Path(args.db) if args.db else root / "index.sqlite"
    sessions: list[tuple[Path, str | None, str | None, bool]] = []
    if not args.no_archive:
        sessions += [(p, None, None, True) for p in C.discover_sessions(root)]
    for i, run in enumerate(args.run):
        cycle = args.cycle[i] if i < len(args.cycle) else None
        sessions.append((Path(run), cycle, None, False))
    if not sessions:
        print(f"nothing to index under {root} (use --run DIR for raw run folders)", file=sys.stderr)
        return 2
    summary = build(db, sessions, root, args.gap_s)
    print(f"INDEX {db} fts5={summary['fts5']}")
    for s in summary["sessions"]:
        print(f"  {s['cycle']}/{s['day_trial']}: {s['lines']} journal lines, {s['runs']} skill runs ({s['journal']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
