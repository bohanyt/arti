#!/usr/bin/env python
"""Live7 WP6: read-only queries over the Stardew archive index (`index.py build`).

    sv.py [--db PATH] [--session CYCLE/DAYTRIAL] [--json] <command> ...
      sessions                         sessions in the index
      day "spring 5"                   timeline of one game day (runs, rows, location changes, errors)
      at "spring 5 14:00"              what was happening at that game time (+ nearest evidence)
      skill siram [--class uncertain]  runs of one skill
      loc Mine                         visits to a location (time spans) and runs there
      error exit_not_offered           errors whose code/text contains the text
      text "Willy"                     full-text search (FTS5) over journal, SMAPI lines and speech
      run <id>                         one run: legs, native receipts, screenshots (id = run number, or session#run)
      diff <A> <B>                     per-skill outcome classes, A vs B (a cycle or cycle/day-trial)

Every answer line cites `<journal path>:L<line>` (path relative to the archive root for archived sessions).
Game times marked `~` are derived (interpolated/extrapolated), not native readbacks.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live7_common as C  # noqa: E402


KEY_EVENTS = ("ev IN ('location_changed','skill_started','skill_finished','skill_failed','skill_leg','skill_leg_started',"
              "'skill_chosen','skill_offer','skill_model_decision','skill_model_malformed','curfew','no_progress_hold',"
              "'finish_verified','interaction_effect','extras_effect','tool_work_effect','bed_waiting_for_host','bed_wait_ended',"
              "'day_start_detected','skill_budget_exhausted','skill_session_finished','move_reconciled','skill_step_boundary')"
              " OR ev LIKE 'part_%' OR ev LIKE 'live_%' OR (ev='approach_feedback' AND reason NOT LIKE 'fresh%')"
              " OR (ev='continuation_not_scheduled' AND reason LIKE '%PAUSED%')")


class SV:
    def __init__(self, db: Path, session: str | None = None):
        if not db.is_file():
            raise SystemExit(f"no index at {db}; run index.py build first")
        self.con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.fts5 = self.meta().get("fts5") == "1"
        self.sids = self._session_filter(session)

    def meta(self) -> dict:
        return {r["key"]: r["value"] for r in self.con.execute("SELECT key, value FROM meta")}

    def _session_filter(self, session: str | None) -> list[int] | None:
        if not session:
            return None
        if "/" in session:
            cycle, dt = session.split("/", 1)
            rows = self.con.execute("SELECT id FROM sessions WHERE cycle=? AND day_trial=?", (cycle, dt)).fetchall()
        else:
            rows = self.con.execute("SELECT id FROM sessions WHERE cycle=? OR day_trial=?", (session, session)).fetchall()
        if not rows:
            raise SystemExit(f"no session matches {session!r}")
        return [r["id"] for r in rows]

    def _where(self, alias: str = "") -> tuple[str, list]:
        if self.sids is None:
            return "1=1", []
        col = f"{alias}session_id" if alias else "session_id"
        return f"{col} IN ({','.join('?' * len(self.sids))})", list(self.sids)

    def journal(self, sid: int) -> str:
        return self.con.execute("SELECT journal FROM sessions WHERE id=?", (sid,)).fetchone()["journal"]

    def cite(self, sid: int, line) -> str:
        return f"{self.journal(sid)}:L{line}" if line else self.journal(sid)

    def label(self, sid: int) -> str:
        r = self.con.execute("SELECT cycle, day_trial FROM sessions WHERE id=?", (sid,)).fetchone()
        return f"{r['cycle']}/{r['day_trial']}"

    def _time(self, sid: int, line: int | None, t: str | None) -> str:
        if not line or not t:
            return t or "?"
        r = self.con.execute("SELECT time_src FROM events WHERE session_id=? AND line=?", (sid, line)).fetchone()
        return t if r and str(r["time_src"] or "").startswith("anchor") else "~" + t

    # ------------------------------------------------------------------------------------------- commands

    def sessions(self) -> list[dict]:
        w, p = self._where()
        out = []
        for r in self.con.execute(f"SELECT * FROM sessions WHERE {w.replace('session_id', 'id')} ORDER BY cycle, day_trial", p):
            dates = [x["date"] for x in self.con.execute("SELECT date FROM days WHERE session_id=? ORDER BY date_key", (r["id"],))]
            out.append({"session": f"{r['cycle']}/{r['day_trial']}", "status": r["status"], "reason": r["reason"],
                        "code": (r["code_sha"] or "")[:8], "dll": (r["dll_sha"] or "")[:8], "dates": dates,
                        "inputs": f"{r['inputs']}/{r['input_cap']}", "starts": f"{r['starts']}/{r['start_cap']}",
                        "cite": self.cite(r["id"], r["terminal_line"])})
        return out

    def day(self, text: str) -> list[dict]:
        q = C.parse_day_query(text)
        if not q:
            raise SystemExit(f"cannot parse day {text!r} (try 'spring 5' or 'Y6 fall 5')")
        season, dnum, year = q
        w, p = self._where()
        days = self.con.execute(f"SELECT * FROM days WHERE {w} ORDER BY session_id", p).fetchall()
        out = []
        for d in days:
            date = C.norm_date(d["date"])
            if not date or date[1] != season or date[2] != dnum or (year is not None and date[0] != year):
                continue
            sid = d["session_id"]
            m = " ".join(f"M{i}={'?' if d[f'm{i}'] is None else ('Y' if d[f'm{i}'] else 'N')}" for i in range(1, 9))
            out.append({"kind": "day", "session": self.label(sid), "date": d["date"], "weekday": d["weekday"],
                        "text": f"lines L{d['first_line']}-L{d['last_line']} wake {d['wake_time']} bed {d['bed_time']} {m}",
                        "cite": self.cite(sid, d["first_line"])})
            lo, hi = d["first_line"], d["last_line"]
            items = []
            for r in self.con.execute("SELECT * FROM skill_runs WHERE session_id=? AND start_line BETWEEN ? AND ?",
                                      (sid, lo, hi)):
                items.append((r["start_line"], {
                    "kind": "run", "time": self._time(sid, r["start_line"], r["start_time"]),
                    "text": f"run {r['run_id']} {r['skill']} {_arg(r['arg'])} [{r['task'] or '-'}] by {r['by'] or '?'} "
                            f"{r['start_loc']} -> {r['end_loc']} {self._time(sid, r['end_line'], r['end_time'])} "
                            f"{r['class']} {r['reason'] or r['verdict'] or ''} inputs {r['inputs']}",
                    "cite": self.cite(sid, r["start_line"]) + (f"-L{r['end_line']}" if r["end_line"] else "")}))
            for r in self.con.execute("SELECT * FROM rows WHERE session_id=? AND line BETWEEN ? AND ?", (sid, lo, hi)):
                items.append((r["line"], {"kind": "row", "time": self._time(sid, r["line"], r["time"]),
                                          "text": f"{r['part']} row {r['task']} verified={bool(r['verified'])} {r['reason'] or ''}",
                                          "cite": self.cite(sid, r["line"])}))
            for r in self.con.execute("SELECT line, time, location, json FROM events WHERE session_id=? AND ev='location_changed'"
                                      " AND line BETWEEN ? AND ?", (sid, lo, hi)):
                ev = json.loads(r["json"])
                items.append((r["line"], {"kind": "move", "time": self._time(sid, r["line"], r["time"]),
                                          "text": f"location {C.short_location(ev.get('from'))} -> {C.short_location(ev.get('to'))}",
                                          "cite": self.cite(sid, r["line"])}))
            for r in self.con.execute("SELECT * FROM gaps WHERE session_id=? AND start_line BETWEEN ? AND ?", (sid, lo, hi)):
                items.append((r["start_line"], {"kind": "gap", "time": "",
                                                "text": f"{r['kind']} {r['wall_s']} s wall ~{r['game_min']:.0f} game min ({r['detail']})",
                                                "cite": self.cite(sid, r["start_line"]) + f"-L{r['end_line']}"}))
            out += [it for _, it in sorted(items, key=lambda x: x[0])]
        return out

    def at(self, text: str, window_min: int = 20) -> list[dict]:
        q = C.parse_at_query(text)
        if not q:
            raise SystemExit(f"cannot parse {text!r} (try 'spring 5 14:00')")
        season, dnum, year, minutes = q
        w, p = self._where()
        out = []
        for d in self.con.execute(f"SELECT * FROM days WHERE {w}", p).fetchall():
            date = C.norm_date(d["date"])
            if not date or date[1] != season or date[2] != dnum or (year is not None and date[0] != year):
                continue
            sid = d["session_id"]
            near = self.con.execute(
                "SELECT line, time, time_src, location, ev, reason FROM events WHERE session_id=? AND date=? AND time_min IS NOT NULL"
                " ORDER BY abs(time_min - ?), line LIMIT 1", (sid, d["date"], minutes)).fetchone()
            if not near:
                continue
            line = near["line"]
            run = self.con.execute("SELECT * FROM skill_runs WHERE session_id=? AND start_line<=? AND (end_line>=? OR end_line IS NULL)"
                                   " ORDER BY start_line DESC LIMIT 1", (sid, line, line)).fetchone()
            if run:
                ctx = (f"; inside run {run['run_id']} {run['skill']} {_arg(run['arg'])} [{run['task'] or '-'}] "
                       f"{run['class']} {run['reason'] or run['verdict'] or ''}")
            else:
                prev = self.con.execute("SELECT * FROM skill_runs WHERE session_id=? AND end_line<? ORDER BY end_line DESC LIMIT 1",
                                        (sid, line)).fetchone()
                nxt = self.con.execute("SELECT * FROM skill_runs WHERE session_id=? AND start_line>? ORDER BY start_line LIMIT 1",
                                       (sid, line)).fetchone()
                ctx = "; between runs" + (
                    f"; previous run {prev['run_id']} {prev['skill']} {_arg(prev['arg'])} [{prev['task'] or '-'}] {prev['class']} "
                    f"{prev['reason'] or prev['verdict'] or ''} ended L{prev['end_line']}" if prev else "") + (
                    f"; next run {nxt['run_id']} {nxt['skill']} {_arg(nxt['arg'])} starts L{nxt['start_line']}" if nxt else "")
            out.append({"kind": "at", "session": self.label(sid), "time": self._time(sid, line, near["time"]),
                        "text": f"{d['date']} nearest line L{line} at {C.short_location(near['location'])}{ctx}",
                        "cite": self.cite(sid, line)})
            for r in self.con.execute(
                    "SELECT line, time, location, ev, reason FROM events WHERE session_id=? AND date=? AND time_min BETWEEN ? AND ?"
                    f" AND ({KEY_EVENTS}) ORDER BY line LIMIT 30", (sid, d["date"], minutes - window_min, minutes + window_min)):
                out.append({"kind": "event", "time": self._time(sid, r["line"], r["time"]),
                            "text": f"{r['ev']} {r['reason'] or ''} @ {C.short_location(r['location'])}",
                            "cite": self.cite(sid, r["line"])})
            lw = self.con.execute("SELECT wall FROM events WHERE session_id=? AND line=?", (sid, line)).fetchone()["wall"]
            for a in self.con.execute("SELECT path, line, time FROM artifacts WHERE session_id=? AND kind='screenshot' AND line IS NOT NULL"
                                      " AND abs(wall - ?) <= 90 ORDER BY abs(wall - ?) LIMIT 2", (sid, lw or 0, lw or 0)):
                out.append({"kind": "screenshot", "time": a["time"] or "?", "text": a["path"], "cite": self.cite(sid, a["line"])})
        return out

    def skill(self, name: str, cls: str | None = None) -> list[dict]:
        w, p = self._where()
        sql = f"SELECT * FROM skill_runs WHERE {w} AND (skill=? OR start_skill=?)"
        args = p + [name, name]
        if cls:
            sql += " AND upper(class)=?"
            args.append(cls.upper())
        out = []
        for r in self.con.execute(sql + " ORDER BY session_id, start_line", args):
            sid = r["session_id"]
            out.append({"kind": "run", "session": self.label(sid), "time": f"{r['date']} {self._time(sid, r['start_line'], r['start_time'])}",
                        "text": f"run {r['run_id']} {r['skill']} {_arg(r['arg'])} [{r['task'] or '-'}] {r['class']} "
                                f"{r['reason'] or r['verdict'] or ''} inputs {r['inputs']} @ {r['start_loc']}",
                        "cite": self.cite(sid, r["start_line"]) + (f"-L{r['end_line']}" if r["end_line"] else "")})
        if not out:
            counts = self.con.execute(f"SELECT class, count(*) n FROM skill_runs WHERE {w} AND (skill=? OR start_skill=?) GROUP BY class",
                                      p + [name, name]).fetchall()
            out.append({"kind": "none", "text": f"no {name} runs" + (f" with class {cls.upper()}" if cls else "")
                        + (f"; all classes: {dict((c['class'], c['n']) for c in counts)}" if counts else ""), "cite": ""})
        return out

    def loc(self, name: str) -> list[dict]:
        w, p = self._where()
        out = []
        sids = [r["id"] for r in self.con.execute(f"SELECT id FROM sessions WHERE {w.replace('session_id', 'id')}", p)]
        pat = name.lower()
        for sid in sids:
            spans, cur = [], None
            for r in self.con.execute("SELECT line, date, time, time_src, location FROM events WHERE session_id=? ORDER BY line", (sid,)):
                here = r["location"] is not None and pat in C.short_location(r["location"]).lower()
                if here and cur is None:
                    cur = [r["line"], r["line"], r["date"], r["time"], r["time"], r["location"]]
                elif here:
                    cur[1], cur[4] = r["line"], r["time"]
                elif cur is not None:
                    spans.append(cur)
                    cur = None
            if cur is not None:
                spans.append(cur)
            for a, b, date, t0, t1, locname in spans:
                out.append({"kind": "visit", "session": self.label(sid), "time": f"{date} {self._time(sid, a, t0)}-{self._time(sid, b, t1)}",
                            "text": f"at {C.short_location(locname)} L{a}-L{b}", "cite": self.cite(sid, a) + f"-L{b}"})
            for r in self.con.execute("SELECT * FROM skill_runs WHERE session_id=? AND (lower(start_loc) LIKE ? OR lower(end_loc) LIKE ?)",
                                      (sid, f"%{pat}%", f"%{pat}%")):
                out.append({"kind": "run", "session": self.label(sid), "time": self._time(sid, r["start_line"], r["start_time"]),
                            "text": f"run {r['run_id']} {r['skill']} {_arg(r['arg'])} {r['start_loc']} -> {r['end_loc']} {r['class']} "
                                    f"{r['reason'] or r['verdict'] or ''}", "cite": self.cite(sid, r["start_line"])})
        if not out:
            out.append({"kind": "none", "text": f"no journal line at a location matching {name!r}", "cite": ""})
        return out

    def error(self, text: str) -> list[dict]:
        w, p = self._where()
        out = []
        for r in self.con.execute(f"SELECT * FROM errors WHERE {w} AND (lower(code) LIKE ? OR lower(text) LIKE ?) ORDER BY session_id, source, line",
                                  p + [f"%{text.lower()}%"] * 2):
            sid = r["session_id"]
            cite = self.cite(sid, r["line"]) if r["source"] == "journal" else f"{self.label(sid)}/{r['path']}:L{r['line']}"
            out.append({"kind": r["source"], "session": self.label(sid),
                        "time": f"{r['date'] or ''} {self._time(sid, r['line'], r['time']) if r['source'] == 'journal' else ''}".strip(),
                        "text": f"{r['code']}: {r['text'][:220]} @ {C.short_location(r['location']) or '?'}", "cite": cite})
        if not out:
            out.append({"kind": "none", "text": f"no error matching {text!r}", "cite": ""})
        return out

    def text(self, query: str, limit: int = 40) -> list[dict]:
        w, p = self._where()
        if self.fts5:
            fq = " ".join('"' + t.replace('"', '""') + '"' for t in query.split())
            rows = self.con.execute(f"SELECT source, session_id, ref, snippet(fts, 3, '[', ']', '...', 12) snip FROM fts "
                                    f"WHERE fts MATCH ? AND {w} ORDER BY rank LIMIT ?", [fq] + p + [limit]).fetchall()
        else:
            rows = self.con.execute(f"SELECT source, session_id, ref, substr(body, 1, 200) snip FROM fts WHERE body LIKE ? AND {w} LIMIT ?",
                                    [f"%{query}%"] + p + [limit]).fetchall()
        out = []
        for r in rows:
            sid = r["session_id"]
            ref = r["ref"]
            if r["source"] == "journal":
                line = int(ref[1:])
                ev = self.con.execute("SELECT date, time FROM events WHERE session_id=? AND line=?", (sid, line)).fetchone()
                cite, t = self.cite(sid, line), f"{ev['date'] or ''} {self._time(sid, line, ev['time'])}" if ev else ""
            elif r["source"] == "speech":
                d = self.con.execute("SELECT line FROM decisions WHERE session_id=? AND request_index=?",
                                     (sid, int(ref.split()[-1]))).fetchone()
                cite, t = (self.cite(sid, d["line"]) if d and d["line"] else self.label(sid)) + f" ({ref})", ""
            else:
                cite, t = f"{self.label(sid)}/{ref}", ""
            out.append({"kind": r["source"], "session": self.label(sid), "time": t, "text": r["snip"], "cite": cite})
        if not out:
            out.append({"kind": "none", "text": f"no match for {query!r} in journal, SMAPI lines or speech", "cite": ""})
        return out

    def run(self, ident: str) -> list[dict]:
        w, p = self._where()
        if "#" in ident:
            sess, rid = ident.rsplit("#", 1)
            self.sids = self._session_filter(sess)
            w, p = self._where()
        else:
            rid = ident
        rows = self.con.execute(f"SELECT * FROM skill_runs WHERE {w} AND run_id=?", p + [int(rid)]).fetchall()
        if len(rows) > 1:
            return [{"kind": "ambiguous", "session": self.label(r["session_id"]), "text": f"run {rid}: use --session or session#{rid}",
                     "cite": self.cite(r["session_id"], r["start_line"])} for r in rows]
        if not rows:
            return [{"kind": "none", "text": f"no run {ident}", "cite": ""}]
        r = rows[0]
        sid = r["session_id"]
        out = [{"kind": "run", "session": self.label(sid), "time": f"{r['date']} {self._time(sid, r['start_line'], r['start_time'])}"
                f" - {self._time(sid, r['end_line'], r['end_time'])}",
                "text": f"run {r['run_id']} {r['skill']} {_arg(r['arg'])} [{r['task'] or '-'}] by {r['by']} ({r['chooser_src']})"
                        f" request {r['request_index']} {r['class']} {r['reason'] or r['verdict'] or ''} inputs {r['inputs']}"
                        f" decisions {r['decisions']} {r['start_loc']} -> {r['end_loc']}",
                "cite": self.cite(sid, r["start_line"]) + (f"-L{r['end_line']}" if r["end_line"] else "")}]
        if r["cause_line"]:
            out.append({"kind": "cause", "text": "first uncertain marker", "cite": self.cite(sid, r["cause_line"])})
        for leg in json.loads(r["legs"] or "[]"):
            out.append({"kind": "leg", "text": f"{leg.get('skill')} {_arg(leg.get('arg'))} {leg.get('reason') or ''}".strip(),
                        "cite": self.cite(sid, leg.get("line"))})
        hi = r["end_line"] or 10 ** 9
        for e in self.con.execute(
                "SELECT line, ev, reason FROM events WHERE session_id=? AND line BETWEEN ? AND ? AND (ev IN ('finish_verified',"
                "'location_changed','objective_progress','interaction_effect','extras_effect','tool_work_effect','ui_press',"
                "'skill_finished','skill_failed','curfew','no_progress_hold') OR ev LIKE 'part_%_row' OR ev LIKE 'observed%')"
                " ORDER BY line LIMIT 60", (sid, r["start_line"], hi + 3)):
            out.append({"kind": "receipt", "text": f"{e['ev']} {e['reason'] or ''}".strip(), "cite": self.cite(sid, e["line"])})
        if r["wall_start"] is not None:
            for a in self.con.execute("SELECT path, wall_src, line FROM artifacts WHERE session_id=? AND kind='screenshot'"
                                      " AND wall BETWEEN ? AND ?", (sid, r["wall_start"] - 1, (r["wall_end"] or r["wall_start"]) + 30)):
                out.append({"kind": "screenshot", "text": a["path"], "cite": self.cite(sid, a["line"])})
        return out

    def diff(self, a: str, b: str) -> list[dict]:
        def counts(sel):
            ids = self._session_filter(sel)
            res: dict[str, dict] = {}
            for r in self.con.execute(f"SELECT skill, class, count(*) n FROM skill_runs WHERE session_id IN ({','.join('?' * len(ids))})"
                                      " GROUP BY skill, class", ids):
                res.setdefault(r["skill"], {})[r["class"]] = r["n"]
            return res, ids
        ca, ia = counts(a)
        cb, ib = counts(b)
        out = []
        for skill in sorted(set(ca) | set(cb)):
            fa, fb = _fmt_counts(ca.get(skill, {})), _fmt_counts(cb.get(skill, {}))
            if fa != fb:
                out.append({"kind": "skill", "text": f"{skill}: {a} [{fa or '-'}]  vs  {b} [{fb or '-'}]", "cite": ""})
        for label, ids in ((a, ia), (b, ib)):
            for sid in ids:
                s = self.con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                out.append({"kind": "session", "session": label, "text": f"{s['day_trial']}: {s['status']} {s['reason']} inputs "
                            f"{s['inputs']}/{s['input_cap']} starts {s['starts']}/{s['start_cap']} code {(s['code_sha'] or '')[:8]}",
                            "cite": self.cite(sid, s["terminal_line"])})
        return out


def _arg(v) -> str:
    if v is None:
        return ""
    try:
        x = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        x = v
    return "" if x is None else f"'{x}'"


def _fmt_counts(d: dict) -> str:
    return " ".join(f"{k}:{d[k]}" for k in C.CLASSES if d.get(k))


def render(items: list[dict]) -> str:
    out = []
    for it in items:
        head = " ".join(x for x in (f"[{it['session']}]" if it.get("session") else "", it.get("time") or "") if x)
        out.append(f"{it['kind']:<10} {head + ' ' if head else ''}{it['text']}" + (f"  <{it['cite']}>" if it.get("cite") else ""))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="default <archive-root>/index.sqlite")
    ap.add_argument("--archive-root")
    ap.add_argument("--session", help="cycle or cycle/day-trial")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sessions")
    sub.add_parser("day").add_argument("date")
    sub.add_parser("at").add_argument("when")
    sk = sub.add_parser("skill")
    sk.add_argument("name")
    sk.add_argument("--class", dest="cls", choices=[c.lower() for c in C.CLASSES] + list(C.CLASSES))
    sub.add_parser("loc").add_argument("name")
    sub.add_parser("error").add_argument("text")
    sub.add_parser("text").add_argument("query")
    sub.add_parser("run").add_argument("id")
    df = sub.add_parser("diff")
    df.add_argument("a")
    df.add_argument("b")
    args = ap.parse_args(argv)
    root = Path(args.archive_root) if args.archive_root else C.default_archive_root()
    sv = SV(Path(args.db) if args.db else root / "index.sqlite", args.session)
    items = {
        "sessions": lambda: sv.sessions(), "day": lambda: sv.day(args.date), "at": lambda: sv.at(args.when),
        "skill": lambda: sv.skill(args.name, args.cls), "loc": lambda: sv.loc(args.name), "error": lambda: sv.error(args.text),
        "text": lambda: sv.text(args.query), "run": lambda: sv.run(args.id), "diff": lambda: sv.diff(args.a, args.b),
    }[args.cmd]()
    if args.cmd == "sessions" and not args.json:
        for s in items:
            print(f"{s['session']}: {s['status']} {s['reason']} code {s['code']} dll {s['dll']} dates {s['dates']} "
                  f"inputs {s['inputs']} starts {s['starts']}  <{s['cite']}>")
    else:
        print(json.dumps(items, indent=1, ensure_ascii=False) if args.json else render(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
