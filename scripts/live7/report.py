#!/usr/bin/env python
"""Live7 WP7: automatic post-live report.

    report.py <cycle> [--archive-root R] [--ledger SKILLS_W1_LEDGER.md] [--receipt-dir PREP]
    report.py --run RUN_OR_ARCHIVE_DIR [--run ...] [--cycle C] [--out DIR]

Writes REPORT.md + report.json (into `<archive-root>/<cycle>/` for a cycle, else `--out`), and with --receipt-dir a copy
`RECEIPT_LIVE7_<cycle>.md`. Contents per session (PLAN §3.3): per-day M1-M8, per-skill attempts by outcome class and
success rate, the first error (first non-VERIFIED terminal), zero-input retry streaks, time lost (wall gaps classified,
converted to game minutes at the run's measured rate), top failure causes by reason prefix, cap use, and suggested
fixes looked up in the ledger. Every finding links its journal line.

Labels: all counts come from the journal/report (READ). Game times with `~` and game-minute losses are derived from
wall time. Suggestions are SUGGESTION/HYPOTHESIS only. The report never upgrades a label; unit/replay results are not
included.

Live7 (rehearsal 1 fix): the cycle supervisor's status (cycle_state/cycle_events, e.g. SAFETY_STOP) is shown first and
per session; M1-M8 use the strict PLAN §1 definitions (core/planned rows of the cycle-1 day table, native bed time,
pad_watch, the supervisor's night checkpoint/end-day, archive + cycle evidence + index). Saturday 2026-10-24: TV/mail
(`cek_pagi`) is ARTI's choice and no longer an M4 core row, so day 3's core rows are just `tidur` (live7_common.LIVE7_CORE). A value that cannot be read
is `?`, never Y. Cycle evidence comes from the archived run-evidence archive; if only the source run folder still has
it, it is used and labelled NOT archived. When the cycle folder holds run_manifest.json (live_cycle.py), a short "Run
identity" header (source SHA/dirty, DLL + config hashes, flags, provider, save, host, webhook boolean) comes first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live7_common as C  # noqa: E402

DEFAULT_LEDGER = C.REPO_ROOT.parent / "operator-staging" / "SKILLS_W1_LEDGER.md"
FIX_WORDS = re.compile(r"\bfix|fixed|merged|→|now\b", re.I)


def _loc(v: str | None) -> str:
    return C.short_location(v) or "?"


def _t(s: C.Session, line: int | None) -> str:
    r = s.clock.at_line(line) if line else None
    if not r or r.time_min is None:
        return "?"
    return ("" if str(r.time_src or "").startswith("anchor") else "~") + C.fmt_min(r.time_min)


def ledger_suggestions(ledger: Path | None, causes: list[str], per_cause: int = 2) -> dict[str, list[dict]]:
    if not ledger or not ledger.is_file():
        return {}
    lines = ledger.read_text(encoding="utf-8", errors="replace").splitlines()
    out: dict[str, list[dict]] = {}
    for cause in causes:
        token = cause.split(":")[-1] if cause.startswith(("partial:", "stalled:")) else cause.split(":")[0]
        token = re.sub(r":attempted$", "", token)
        if not token or token == "(none)":
            continue
        hits = []
        for n, text in enumerate(lines, 1):
            if token in text:
                i = text.index(token)
                excerpt = text[max(0, i - 160): i + 200].strip()
                hits.append({"ledger_line": n, "fix_words": bool(FIX_WORDS.search(text)), "excerpt": excerpt})
        hits.sort(key=lambda h: (not h["fix_words"], -h["ledger_line"]))
        if hits:
            out[cause] = [dict(h, label="SUGGESTION/HYPOTHESIS") for h in hits[:per_cause]]
    return out


# Mirror of arti_stardew_live7.day_terminal (the trial/supervisor live-day contract); a test keeps them equal.
LIVE7_DAY_VERIFIED, LIVE7_DAY_LOST = "LIVE7_DAY_VERIFIED", "LIVE7_DAY_LOST"


def live_day_terminal(report: dict | None) -> str | None:
    report = report or {}
    session = ((report.get("skills") or {}).get("session") or {}) if isinstance(report.get("skills"), dict) else {}
    status, reason = report.get("status"), report.get("reason")
    if reason == "live7_sleep_verified" and (status == LIVE7_DAY_VERIFIED or status == "HELD"
                                            and session.get("verdict") == "achieved"):
        return "verified"
    if status == LIVE7_DAY_LOST and reason in ("live7:recovery_loop_day_lost", "live7:passed_out",
                                             "live7:home_sleep_retries_exhausted"):
        return "lost"
    return None


RUN_MANIFEST = "run_manifest.json"  # scripts/m1_operator/run_manifest.py (schema live7_run_manifest.v1)


def _short(v, n: int = 8) -> str:
    return "?" if not isinstance(v, str) or not v else re.sub(r"[^A-Za-z0-9._:+-]", "", v)[:n] or "?"


def run_identity(cycle: C.CycleEvidence | None) -> dict | None:
    """Compact run identity from the cycle's run_manifest.json (READ), or None when the cycle has none."""
    if cycle is None:
        return None
    folder = Path(cycle.source) / "live7" / cycle.name if cycle.archived else Path(cycle.source)
    m = C.load_json(folder / RUN_MANIFEST)
    if not isinstance(m, dict) or m.get("schema") != "live7_run_manifest.v1":
        return None
    get = lambda *keys: _dig(m, keys)  # noqa: E731
    return {"file": (folder / RUN_MANIFEST).as_posix(), "phase": get("phase"), "written_utc": get("written_utc"),
            "hostname": get("machine", "hostname"), "cycle_id": get("session", "cycle_id"),
            "save_id": get("session", "save_id"), "save_name": get("session", "save_name"),
            "source_sha": get("source", "sha"), "source_dirty": get("source", "dirty"),
            "farmhand_dll": get("dlls", "farmhand", "sha256"), "host_dll": get("dlls", "host", "sha256"),
            "configs": {k: get("configs", k, "sha256") for k in ("farmhand_mod", "trial", "host_mod")},
            "flags": get("flags") or {}, "provider": get("provider") or {},
            "webhook_configured": get("watcher", "webhook_configured"), "consistency": get("consistency") or {},
            "refreshes": len(get("history") or []), "identity_changes": get("identity_changes")}


def _dig(value, keys):
    for k in keys:
        value = value.get(k) if isinstance(value, dict) else None
    return value


def _yn(v) -> str:
    return "?" if v is None else ("yes" if v is True else "no" if v is False else str(v))


def run_identity_lines(ri: dict) -> list[str]:
    """The short "Run identity" header (REPORT.md and stdout)."""
    flags, prov, cons = ri["flags"], ri["provider"], ri["consistency"]
    trial = flags.get("trial") or {}

    def mod(name: str) -> str:
        f = flags.get(name) or {}
        return ("unreadable" if f.get("error") else
                f"HostConsole {_yn(f.get('RehearsalHostConsoleEnabled'))}, CharCreate {_yn(f.get('CharacterCreationEnabled'))}")
    dirty = ri["source_dirty"]
    return [
        f"- Cycle `{_short(ri['cycle_id'], 40)}` on `{_short(ri['hostname'], 40)}`; save `{_short(ri['save_name'], 64)}` "
        f"(id `{_short(ri['save_id'], 32)}`); manifest `{_short(ri['phase'], 20)}` at {_short(ri['written_utc'], 20)}, "
        f"{ri['refreshes']} refresh(es), identity changes {ri['identity_changes'] if ri['identity_changes'] is not None else '?'}.",
        f"- Source `{_short(ri['source_sha'], 12)}` ({'DIRTY' if dirty else 'clean' if dirty is False else 'dirty ?'}); "
        f"DLL farmhand `{_short(ri['farmhand_dll'])}`, HOST `{_short(ri['host_dll'])}` "
        f"(HOST=farmhand {_yn(cons.get('host_dll_matches_farmhand'))}, farmhand=enrolled "
        f"{_yn(cons.get('farmhand_dll_matches_enrolled'))}, source=enrolled {_yn(cons.get('source_matches_enrolled'))}).",
        "- Configs (sha256): " + ", ".join(f"{k} `{_short(v)}`" for k, v in ri["configs"].items()) + ".",
        f"- Flags: live7_help_enabled {_yn(trial.get('live7_help_enabled'))}"
        + (f", live7_moments_enabled {_yn(trial.get('live7_moments_enabled'))}" if "live7_moments_enabled" in trial else "")
        + f"; farmhand mod {mod('farmhand_mod')}; HOST mod {mod('host_mod')}; provider "
        f"`{_short(prov.get('kind'), 40)}`/`{_short(prov.get('model'), 40)}` ({_short(prov.get('effort'), 20)}); "
        f"watcher webhook configured {_yn(ri['webhook_configured'])}.",
    ]


def cycle_summary(cycle: C.CycleEvidence | None, attempt: str | None) -> dict | None:
    if cycle is None:
        return None
    day = cycle.day(attempt) or {}
    evs = [{"ev": C.ev_of(e), "at": C.utc_iso(e.get("at")) if isinstance(e.get("at"), (int, float)) else None,
            **({"source": e["source"]} if e.get("source") else {}), **({"reason": e["reason"]} if e.get("reason") else {})}
           for e in cycle.events]
    return {"name": cycle.name, "status": cycle.status, "source": cycle.source, "archived": cycle.archived,
            "run_identity": run_identity(cycle),
            "note": cycle.note, "safety_stop": cycle.status == "safety_stop" or any(e["ev"] == "SAFETY_STOP" for e in evs),
            "completed_days": sum(d.get("status") == "ended" for d in cycle.state.get("days", [])),
            "events": evs, "day": {k: day.get(k) for k in ("index", "status", "outcome", "next_date", "checkpoint")},
            "pad_watch": {"rows": cycle.pad_rows, "violation_rows": len(cycle.pad_violations),
                          "pad_lines_seen": cycle.pad_lines_seen}}


def summarize(s: C.Session, archived: bool, cycle: C.CycleEvidence | None = None, index_db: Path | None = None) -> dict:
    report = s.report or {}
    limits = report.get("limits") or {}
    by_skill: dict[str, Counter] = {}
    for r in s.runs:
        by_skill.setdefault(r.skill, Counter())[r.cls] += 1
    skills = []
    for skill, cnt in sorted(by_skill.items()):
        total = sum(cnt.values())
        skills.append({"skill": skill, "attempts": total, "verified": cnt.get("VERIFIED", 0),
                       "success_rate": round(cnt.get("VERIFIED", 0) / total, 3) if total else None,
                       "by_class": {k: cnt[k] for k in C.CLASSES if cnt.get(k)}})
    first = next((r for r in sorted(s.runs, key=lambda r: r.end_line or r.cause_line or 10 ** 9) if r.cls != "VERIFIED"), None)
    first_error = None
    if first:
        line = first.end_line or first.cause_line or first.start_line
        row = next((x for x in s.rows if not x.verified and abs(x.line - line) <= 3), None)
        first_error = {"run_id": first.run_id, "skill": first.skill, "arg": first.arg, "task": first.task, "class": first.cls,
                       "reason": first.reason or first.verdict, "line": line, "row_line": row.line if row else None,
                       "cause_line": first.cause_line, "time": _t(s, line), "location": _loc(s.clock.at_line(line).location),
                       "inputs": first.inputs, "cite": s.cite(line)}
    streaks = []
    for st in C.zero_input_streaks(s.runs):
        a, b = s.clock.at_line(st["first_line"]), s.clock.at_line(st["last_line"])
        streaks.append(dict(st, wall_s=round((b.wall or 0) - (a.wall or 0), 1) if a and b else None,
                            time=_t(s, st["first_line"]), location=_loc(a.location if a else None),
                            starts_limit=limits.get("skills"), cite=s.cite(st["first_line"]) + f"-L{st['last_line']}"))
    gaps = [{"start_line": g.start_line, "end_line": g.end_line, "wall_s": g.wall_s, "game_min": g.game_min, "kind": g.kind,
             "detail": g.detail, "time": _t(s, g.start_line), "location": _loc(s.clock.at_line(g.start_line).location),
             "cite": s.cite(g.start_line) + f"-L{g.end_line}"} for g in s.gaps]
    lost = [g for g in gaps if g["kind"] != "host_wait"]
    host = [g for g in gaps if g["kind"] == "host_wait"]
    by_kind: dict[str, dict] = {}
    for g in gaps:
        k = by_kind.setdefault(g["kind"], {"count": 0, "wall_s": 0.0, "game_min": 0.0})
        k["count"] += 1
        k["wall_s"] = round(k["wall_s"] + g["wall_s"], 1)
        k["game_min"] = round(k["game_min"] + g["game_min"], 1)
    causes = Counter()
    cause_lines: dict[str, int] = {}
    for r in s.runs:
        if r.cls != "VERIFIED":
            p = C.reason_prefix(r.reason or r.verdict)
            causes[p] += 1
            cause_lines.setdefault(p, r.end_line or r.cause_line or r.start_line)
    rows = []
    for r in s.rows:
        # A row is emitted before its skill terminal; associate by its run interval,
        # never by repeated skill/task names (R10 travels changed args every row).
        run = next((x for x in s.runs if x.start_line <= r.line <= (x.end_line or len(s.lines))), None)
        rows.append({"line": r.line, "part": r.part, "task": r.task, "verified": r.verified, "reason": r.reason,
                     "inputs": run.inputs if run else None, "time": _t(s, r.line), "cite": s.cite(r.line)})
    skills_session = ((report.get("skills") or {}).get("session") or {})
    starts = skills_session.get("skills_run", len(s.runs))
    caps = {
        "inputs": [report.get("physical_inputs"), limits.get("input")],
        "skill_starts": [starts, limits.get("skills")],
        "provider_requests": [(report.get("reserved") or {}).get("provider"), limits.get("provider")],
        "wall_s": [round(report["elapsed_seconds"], 1) if report.get("elapsed_seconds") is not None else None,
                   limits.get("seconds")],
        "decisions": [len(report.get("steps") or []) or None, limits.get("steps")],
    }
    caps_hit = [k for k, (used, cap) in caps.items() if used is not None and cap and used >= cap]
    days = []
    live7 = C.is_live7(s)
    _, attempt = C.journal_tags(s.lines)
    for d in (C.live7_day_rows(s, archived, cycle, index_db) if live7 else C.day_rows(s, archived)):
        days.append({**{k: v for k, v in d.items() if k != "date"}, "date": C.date_label(d["date"]),
                     "date_key": C.date_key(d["date"]), "cite": s.cite(d["first_line"])})
    curfew = next((d for d in s.lines if C.ev_of(d) == "curfew"), None)
    return {
        "session": f"{s.cycle}/{s.day_trial}", "journal": s.cite_path, "lines": len(s.lines),
        "code_sha": (report.get("identity") or {}).get("source"), "dll_sha": (report.get("identity") or {}).get("dll"),
        "profile": report.get("profile"), "status": report.get("status"), "terminal": s.terminal,
        "terminal_cite": s.cite(s.terminal.get("line")), "dates": [C.date_label(d) for d in C.session_dates(s)],
        "rate_game_min_per_s": s.clock.rate, "rate_src": s.clock.rate_src,
        "skills": skills, "runs_total": len(s.runs), "runs_verified": sum(1 for r in s.runs if r.cls == "VERIFIED"),
        "rows": rows, "zero_input_rows": sum(r["inputs"] == 0 for r in rows),
        "first_error": first_error, "zero_input_streaks": streaks,
        "first_curfew": {"line": curfew["_line"], "time_of_day": curfew.get("time_of_day"), "cite": s.cite(curfew["_line"])}
        if curfew else None,
        "time_lost": {"threshold_s": C.DEFAULT_GAP_S, "gaps": gaps, "by_kind": by_kind,
                      "lost_wall_s": round(sum(g["wall_s"] for g in lost), 1),
                      "lost_game_min": round(sum(g["game_min"] for g in lost), 1),
                      "host_wait_wall_s": round(sum(g["wall_s"] for g in host), 1),
                      "host_wait_game_min": round(sum(g["game_min"] for g in host), 1)},
        "failure_causes": [{"cause": c, "count": n, "first_line": cause_lines[c], "cite": s.cite(cause_lines[c])}
                           for c, n in causes.most_common()],
        "caps": caps, "caps_hit": caps_hit, "days": days,
        "live7": live7, "live_day_terminal": live_day_terminal(report) if live7 else None,
        "cycle": cycle_summary(cycle, attempt or s.day_trial) if live7 else None,
    }


def crop_ledger(summaries: list[dict]) -> dict:
    """Cross-day crop view (live7-game-m2): planted, watered per day, natively wet (rain), growth readback.

    READ from each day's `crops` readback (live7_common.crop_day). Cells: `W` = a VERIFIED siram row watered it,
    `R` = seen wet before any ARTI watering (rain or native), `wet` = seen wet later without a row, `dry` = seen
    only dry and not watered, `ready` = seen ready to harvest (not scored), `harv` = a VERIFIED panen row
    targeted it, `dead` = seen dead, `?` = not observed that day. A ready, harvested or dead crop leaves the
    table after that day (`-`) and is never scored again, unless it is seen alive and unripe again (regrowth).
    A tracked crop not observed or watered on a later day makes that day's M2 N unless the day was rain (native
    weather, else all crops seen wet); with no crop observed and no native weather that day M2 is `?`.
    This adjusts the report only; the index keeps per-journal M2."""
    days = sorted((d for summ in summaries if summ.get("live7") for d in summ["days"]
                   if d.get("played") and isinstance(d.get("crops"), dict)), key=lambda d: d.get("date_key") or "")
    rows: dict[str, dict] = {}
    gone: set[str] = set()
    dates = []
    for d in days:
        c, date = d["crops"], d["date"]
        if date not in dates:
            dates.append(date)
        ready, harvested, dead = set(c.get("ready", ())), set(c.get("harvested", ())), set(c.get("dead", ()))
        for ref in c["seen"]:
            if ref not in ready and ref not in harvested:
                gone.discard(ref)  # alive and unripe again (regrowth): tracked again
        known = [ref for ref in rows if ref not in gone and ref not in c["seen"] and ref not in c["watered"]
                 and ref not in ready | harvested | dead]
        for ref, o in c["seen"].items():
            row = rows.setdefault(ref, {"tile": C.crop_tile(ref), "first_seen": date, "planted": None, "days": {}})
            if ref in c.get("planted", ()) or row["first_seen"] == date and o.get("phase") == 0 and c.get("tanam_verified"):
                row["planted"] = date
        for ref, row in rows.items():
            o = c["seen"].get(ref)
            if ref in gone and o is None and ref not in harvested | dead:
                continue  # left the table on an earlier day
            cell = ("harv" if ref in harvested else "ready" if ref in ready else "dead" if ref in dead and o is None
                    else "W" if ref in c["watered"] else "R" if ref in c["native_wet"] else "?" if o is None
                    else "wet" if o.get("wet_line") is not None else "dry")
            row["days"][date] = {"cell": cell, "phase": (o or {}).get("phase")}
        gone |= ready | harvested | dead
        if known and d.get("m2") is not False:
            notes = d.setdefault("notes", {})
            tiles = ", ".join(C.crop_tile(r) for r in known)
            if not c["seen"] and not c["rain"] and not c.get("weather"):
                d["m2"] = None
                notes["m2"] = f"{len(known)} crops from earlier days not observed and not watered ({tiles}); weather unknown"
            elif not c["rain"]:
                # live7-game-m4 D2: native `sun` was journalled all day; a known dry day is N, never "weather unknown".
                d["m2"] = False
                native = (f"; native weather {c['weather']}" if c.get("weather")
                          and f"native weather {c['weather']}" not in notes.get("m2", "") else "")
                notes["m2"] = (notes.get("m2", "") + f"; {len(known)} crops from earlier days neither watered nor seen "
                               f"({tiles}, cross-day ledger){native}").lstrip("; ")
        if d.get("m2") is not None or d.get("matang") is not None:
            d["matang"] = all(d.get(f"m{i}") is True for i in range(1, 9))
    return {"dates": dates, "crops": rows}


def _ledger_lines(ledger: dict) -> list[str]:
    if not ledger.get("crops"):
        return []
    dates = ledger["dates"]
    out = ["## Crop ledger (cross-day)", "",
           "READ from live7_baseline crop blocks and VERIFIED siram/panen rows. `W` ARTI watered (VERIFIED siram), "
           "`R` wet before any watering (rain/native, auto-watered), `wet` seen wet later, `dry` seen dry and not "
           "watered, `ready` ready to harvest, `harv` harvested (VERIFIED panen), `dead` dead, `?` not observed, "
           "`-` not tracked that day (before first sight, or after ready/harvest/death); `pN` = native crop phase "
           "(growth readback). M2 counts dry unripe live crops only.", "",
           "| Crop | Planted | " + " | ".join(dates) + " |", "|---|---|" + "---|" * len(dates)]
    for ref, row in sorted(ledger["crops"].items(), key=lambda kv: (kv[1]["first_seen"], kv[1]["tile"])):
        cells = []
        for date in dates:
            day = row["days"].get(date)
            cells.append("-" if day is None else day["cell"] + ("" if day["phase"] is None else f" p{day['phase']}"))
        out.append(f"| {row['tile']} | {row['planted'] or 'before ' + row['first_seen']} | " + " | ".join(cells) + " |")
    totals = []
    for date in dates:
        cells = [row["days"][date]["cell"] for row in ledger["crops"].values() if date in row["days"]]
        totals.append(f"{date}: W {cells.count('W')}, R {cells.count('R')}, dry {cells.count('dry')}, ? {cells.count('?')}"
                      + "".join(f", {k} {cells.count(k)}" for k in ("ready", "harv", "dead") if k in cells))
    return out + ["", "- " + "; ".join(totals), ""]


def _link(cite: str, out_dir: Path, s_root_journal: Path | None) -> str:
    """Markdown link to a journal line (relative to REPORT.md when possible)."""
    m = re.search(r":L(\d+)(?:-L(\d+))?$", cite)
    if not m or s_root_journal is None:
        return f"`{cite}`"
    try:
        common = os.path.commonpath([str(Path(s_root_journal).resolve()), str(Path(out_dir).resolve())])
        far = len(Path(out_dir).resolve().relative_to(common).parts) > 2
        rel = Path(s_root_journal).resolve().as_posix() if far else os.path.relpath(s_root_journal, out_dir).replace("\\", "/")
    except ValueError:
        rel = Path(s_root_journal).as_posix()
    label = "L" + m.group(1) + (f"-L{m.group(2)}" if m.group(2) else "")
    return f"[{label}](<{rel}#L{m.group(1)}>)"


def _mb(v, note: str | None = None) -> str:
    if v is True and str(note or "").startswith("n/a"):
        return "n/a"
    return "?" if v is None else ("Y" if v else "N")


def _cycle_lines(cy: dict) -> list[str]:
    status = str(cy["status"]).upper() if cy["status"] else "UNKNOWN"
    where = ("archived run evidence" if cy["archived"] else
             "source run folder, NOT archived" if cy["source"] else "missing")
    out = [f"- **Cycle `{cy['name']}`: {status}**" + (" (SAFETY_STOP: the supervisor stopped the cycle)"
                                                      if cy["safety_stop"] else "")
           + f"; completed days {cy['completed_days']}; read from {where} (`{cy['source']}`)."]
    if cy.get("note"):
        out.append(f"  - {cy['note']}")
    day = cy["day"]
    out.append(f"  - This day in the supervisor: status `{day.get('status')}`, outcome `{day.get('outcome')}`, "
               f"night checkpoint `{day.get('checkpoint')}`" + ("" if day.get("status") == "ended" else
                                                                "; **end-day not done**") + ".")
    out.append("  - Cycle events: " + ", ".join(
        e["ev"] + (f"({e['source']})" if e.get("source") else "") + (f" {e['at']}" if e.get("at") else "")
        for e in cy["events"]) + ".")
    pw = cy["pad_watch"]
    out.append(f"  - pad_watch: {pw['rows']} rows, {pw['violation_rows']} with violations, {pw['pad_lines_seen']} PAD "
               "lines seen" + (" (0 PAD lines: it may have read the HOST SMAPI log)" if pw["rows"] and not pw["pad_lines_seen"]
                               else "") + ".")
    return out


def render_md(cycle: str, summaries: list[dict], sessions: list[C.Session], out_dir: Path, suggestions: dict,
              ledger: Path | None, crops: dict | None = None) -> str:
    L = [f"# Live7 post-live report: {cycle}", "",
         "Labels: counts and classes are READ from the journal and the trial report. Times with `~` and lost game minutes "
         "are derived from wall time at the run's measured rate. Suggestions are SUGGESTION/HYPOTHESIS. This report never "
         "upgrades a label; unit/replay results are not included.", ""]
    cycles = {}
    for summ in summaries:
        if summ.get("cycle"):
            cycles.setdefault(summ["cycle"]["name"], summ["cycle"])
    identities = [c for c in cycles.values() if c.get("run_identity")]
    if identities:
        L += ["## Run identity (run_manifest.json, READ)", ""]
        for c in identities:
            L += run_identity_lines(c["run_identity"])
        L.append("")
    live_sessions = [summ for summ in summaries if summ.get("live7")]
    if live_sessions:
        L += ["## Cycle status (supervisor)", ""]
        stopped = [c for c in cycles.values() if c["safety_stop"]]
        if stopped:
            L += ["**SAFETY_STOP: " + ", ".join(f"cycle `{c['name']}`" for c in stopped)
                  + " ended in SAFETY_STOP. A day without the supervisor's night checkpoint and end-day is not matang.**", ""]
        for c in cycles.values():
            L += _cycle_lines(c)
        missing = [summ["session"] for summ in live_sessions if not summ.get("cycle")]
        if missing:
            L.append("- **Cycle evidence missing** (cycle state/events, pad_watch) for " + ", ".join(missing)
                     + ": M3/M6/M8 cannot be Y.")
        L.append("")
    L += _ledger_lines(crops or {})
    for summ, s in zip(summaries, sessions):
        link = lambda cite: _link(cite, out_dir, s.journal_path)  # noqa: E731
        t = summ["terminal"]
        L += [f"## {summ['session']}", "",
              f"- Journal `{summ['journal']}` ({summ['lines']} lines); code `{(summ['code_sha'] or '?')[:8]}`, "
              f"DLL `{(summ['dll_sha'] or '?')[:8]}`; dates {', '.join(summ['dates'])}.",
              f"- Verdict: **{summ['status']}** / `{t.get('reason')}` at {link(summ['terminal_cite'])}. "
              f"Skill runs {summ['runs_verified']}/{summ['runs_total']} VERIFIED.",
              *([f"- Live-day terminal (trial/supervisor contract): **{summ['live_day_terminal'] or 'NONE (refused)'}**; "
                 f"supervisor: " + (f"cycle `{summ['cycle']['name']}` **{str(summ['cycle']['status']).upper()}**, "
                                    f"day status `{summ['cycle']['day'].get('status')}`, checkpoint "
                                    f"`{summ['cycle']['day'].get('checkpoint')}`." if summ.get("cycle")
                                    else "**cycle evidence missing**.")] if summ.get("live7") else []),
              f"- Clock rate {summ['rate_game_min_per_s']} game min/s ({summ['rate_src']})."]
        fe = summ["first_error"]
        if fe:
            L.append(f"- **First error:** run {fe['run_id']} `{fe['skill']}` {_q(fe['arg'])} (task `{fe['task']}`) "
                     f"{fe['class']} `{fe['reason']}` at {fe['time']} in {fe['location']}, {fe['inputs']} inputs, "
                     f"{link(fe['cite'])}" + (f" (row {link(s.cite(fe['row_line']))})" if fe.get("row_line") else "")
                     + (f"; first uncertain marker {link(s.cite(fe['cause_line']))}" if fe.get("cause_line") else "") + ".")
        else:
            L.append("- First error: none (every skill run VERIFIED).")
        L.append(f"- Zero-input rows: {summ['zero_input_rows']} of {len(summ['rows'])}.")
        for st in summ["zero_input_streaks"]:
            L.append(f"- **Zero-input retries:** {st['count']} x `{st['skill']}` {_q(st['arg'])} failed `{st['reason']}` with "
                     f"0 inputs ({st['time']}, {st['location']}, {st['wall_s']} s wall), {st['count']} of "
                     f"{st['starts_limit']} skill starts, {link(st['cite'])}.")
        if summ.get("first_curfew"):
            fc = summ["first_curfew"]
            L.append(f"- First curfew: native time_of_day {fc['time_of_day']} at {link(fc['cite'])}.")
        tl = summ["time_lost"]
        L += ["", "### Time lost", "",
              f"Wall gaps >= {tl['threshold_s']:.0f} s between timestamped journal lines: lost **{tl['lost_wall_s']} s wall "
              f"~ {tl['lost_game_min']:.0f} game min** (excluding HOST waits {tl['host_wait_wall_s']} s ~ "
              f"{tl['host_wait_game_min']:.0f} game min).", "",
              "| Lines | Wall s | Game min | Kind | At | Detail |", "|---|---|---|---|---|---|"]
        for g in sorted(tl["gaps"], key=lambda g: -g["wall_s"]):
            L.append(f"| {link(g['cite'])} | {g['wall_s']} | ~{g['game_min']:.1f} | {g['kind']} | {g['time']} {g['location']} | {g['detail']} |")
        L += ["", "### Per skill", "", "| Skill | Attempts | VERIFIED | Success | Classes |", "|---|---|---|---|---|"]
        for sk in summ["skills"]:
            rate = f"{sk['success_rate'] * 100:.0f}%" if sk["success_rate"] is not None else "?"
            L.append(f"| {sk['skill']} | {sk['attempts']} | {sk['verified']} | {rate} | "
                     + ", ".join(f"{k} {v}" for k, v in sk["by_class"].items()) + " |")
        if summ["rows"]:
            L += ["", "### Campaign rows", "", "| Row | Verified | Reason | At | Line |", "|---|---|---|---|---|"]
            for r in summ["rows"]:
                L.append(f"| {r['task']} | {'yes' if r['verified'] else 'no'} | {r['reason'] or ''} | {r['time']} | {link(r['cite'])} |")
        L += ["", "### Top failure causes", ""]
        if summ["failure_causes"]:
            for c in summ["failure_causes"]:
                L.append(f"- `{c['cause']}` x{c['count']} (first {link(c['cite'])})")
        else:
            L.append("- none")
        L += ["", "### Cap use", "", "| Cap | Used | Limit |", "|---|---|---|"]
        for k, (used, cap) in summ["caps"].items():
            L.append(f"| {k} | {used if used is not None else '?'} | {cap if cap is not None else '?'}"
                     + (" (HIT)" if k in summ["caps_hit"] else "") + " |")
        if summ.get("live7"):
            L += ["", "### Days (M1-M8, strict PLAN §1)", "",
                  "`?` = not derivable from the evidence read here (never counted as met). Matang = all eight Y.", "",
                  "| Date | Wkday | Wake | Bed | M1 | M2 | M3 | M4 | M5 | M6 | M7 | M8 | Matang | Line |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for d in summ["days"]:
                notes = d.get("notes") or {}
                L.append(f"| {d['date']} | {d['weekday']} | {d['wake_time'] or '?'} | {d['bed_time'] or '-'} | "
                         + " | ".join(_mb(d[f'm{i}'], notes.get(f'm{i}')) for i in range(1, 9))
                         + f" | {'-' if d.get('matang') is None else ('Y' if d['matang'] else 'N')} | {link(d['cite'])} |")
            for d in summ["days"]:
                if d.get("notes"):
                    L += ["", f"{d['date']}:"] + [f"- M{k[1:]}: {v}" for k, v in sorted(d["notes"].items())]
        else:
            L += ["", "### Days (M1-M8)", "",
                  "M2/M3/M5 need WP4 tags, pad_watch audit and the Live7 planner; `?` = not derivable from this journal.", "",
                  "| Date | Wkday | Wake | Bed | M1 | M2 | M3 | M4 | M5 | M6 | M7 | M8 | Line |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for d in summ["days"]:
                L.append(f"| {d['date']} | {d['weekday']} | {d['wake_time'] or '?'} | {d['bed_time'] or '-'} | "
                         + " | ".join(_mb(d[f'm{i}']) for i in range(1, 9)) + f" | {link(d['cite'])} |")
        L.append("")
    if suggestions:
        L += ["## Suggested fixes (SUGGESTION/HYPOTHESIS, ledger lookup)", "",
              f"Matched in `{ledger}` by reason token; a match is a pointer, not a diagnosis of this run.", ""]
        for cause, hits in suggestions.items():
            for h in hits:
                L.append(f"- `{cause}` -> ledger L{h['ledger_line']}: {h['excerpt']}")
        L.append("")
    return "\n".join(L)


def _q(v) -> str:
    return "" if v is None else f"`{v}`"


def _cycle_for(s: C.Session, d: Path, cycles: dict) -> C.CycleEvidence | None:
    return C.session_cycle_evidence(s, d, cycles)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cycle", nargs="?")
    ap.add_argument("--archive-root")
    ap.add_argument("--run", action="append", default=[], help="a raw run folder or one archive session dir")
    ap.add_argument("--out", help="output dir (default: <archive-root>/<cycle>/ for a cycle)")
    ap.add_argument("--ledger", help=f"default {DEFAULT_LEDGER} when present")
    ap.add_argument("--receipt-dir", help="also write RECEIPT_LIVE7_<cycle>.md there (e.g. PREP)")
    ap.add_argument("--gap-s", type=float, default=C.DEFAULT_GAP_S)
    args = ap.parse_args(argv)
    root = Path(args.archive_root) if args.archive_root else C.default_archive_root()
    cycles: dict[str, C.CycleEvidence] = {}
    if args.run:
        dirs = []
        for r in map(Path, args.run):
            attempts = sorted(a for c in r.glob("live7/*/cycle_state.json") for a in c.parent.glob("day-*-attempt-*")
                              if C.find_journal(a)) if C.find_journal(r) is None else []
            dirs += [(a, args.cycle) for a in attempts] or [(r, args.cycle)]
        cycle = args.cycle or Path(args.run[0]).name
        out_dir = Path(args.out) if args.out else root / "reports" / cycle
    elif args.cycle:
        dirs = [(p, args.cycle) for p in C.discover_sessions(root, args.cycle)]
        cycles = C.cycle_evidence_for(C.discover_run_evidence(root, args.cycle))
        cycle = args.cycle
        out_dir = Path(args.out) if args.out else root / args.cycle
        if not dirs:
            print(f"no archived sessions for cycle {cycle!r} under {root}", file=sys.stderr)
            return 2
    else:
        ap.error("give a cycle or --run DIR")
    sessions, summaries = [], []
    for d, cyc in dirs:
        archived = (d / "manifest.json").is_file()
        s = C.load_session(d, cyc, None, cite_base=root if archived else None, gap_s=args.gap_s)
        sessions.append(s)
        summaries.append(summarize(s, archived, _cycle_for(s, d, cycles),
                                   root / "index.sqlite" if archived else None))
    ledger = Path(args.ledger) if args.ledger else (DEFAULT_LEDGER if DEFAULT_LEDGER.is_file() else None)
    causes = []
    for summ in summaries:
        for c in summ["failure_causes"]:
            if c["cause"] not in causes:
                causes.append(c["cause"])
    suggestions = ledger_suggestions(ledger, causes)
    crops = crop_ledger(summaries)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_md(cycle, summaries, sessions, out_dir, suggestions, ledger, crops)
    (out_dir / "REPORT.md").write_text(md, encoding="utf-8")
    doc = {"schema": "live7_report.v1", "tool": C.TOOL_VERSION, "cycle": cycle, "sessions": summaries, "crop_ledger": crops,
           "suggestions": suggestions, "ledger": str(ledger) if ledger else None}
    (out_dir / "report.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    if args.receipt_dir:
        shutil.copyfile(out_dir / "REPORT.md", Path(args.receipt_dir) / f"RECEIPT_LIVE7_{cycle}.md")
    seen = set()
    for summ in summaries:
        ri = (summ.get("cycle") or {}).get("run_identity")
        if ri and ri["file"] not in seen:
            seen.add(ri["file"])
            print("Run identity (" + ri["file"] + ")")
            for line in run_identity_lines(ri):
                print("  " + line[2:])
    print(f"REPORT {out_dir / 'REPORT.md'}")
    for summ in summaries:
        fe = summ["first_error"]
        if summ.get("live7"):
            cy = summ.get("cycle")
            print(f"  {summ['session']}: live-day terminal {summ['live_day_terminal']}; cycle "
                  + (f"{cy['name']} {str(cy['status']).upper()} (day {cy['day'].get('status')})" if cy else "evidence MISSING")
                  + "; M1-M8 " + " ".join("".join(_mb(day[f'm{i}'], (day.get('notes') or {}).get(f'm{i}'))
                                                  for i in range(1, 9)) for day in summ["days"] if day.get("played")))
        print(f"  {summ['session']}: {summ['status']}/{summ['terminal'].get('reason')}; "
              f"first error {fe['skill'] + ' ' + str(fe['reason']) + ' ' + fe['cite'] if fe else 'none'}; "
              f"zero-input streaks {[(st['skill'], st['arg'], st['count']) for st in summ['zero_input_streaks']]}; "
              f"lost {summ['time_lost']['lost_wall_s']} s ~ {summ['time_lost']['lost_game_min']:.0f} game min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
