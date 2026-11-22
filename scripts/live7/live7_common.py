"""Shared parsing and crop continuity for Live7 runtime and post-live tooling.

Python stdlib only. Nothing here mutates a game or a save. CropMemory consumes verified journal
rows and native readback; the post-live helpers read finished runs or packed archives.

Journal facts used (old journals, before WP4):
- `at` (wall epoch seconds) on `trial_started` / `trial_decision` / `trial_reservation` lines;
- locations from `location_changed.to`, `action_result.action.{before,after}.location`,
  `home_route_survey.location` and `*.post_row_state.location`;
- dates from `part_*_started.date`, `day_start_*`, `bed_wait_ended`, `date_bound_facts_invalidated.after`;
- game time from `curfew.time_of_day`, the 06:00 day start, and the trial report's step motor facts
  (`time_of_day` + `revisi`, mapped onto journal lines through `action_result` revisions).
WP4 fields are used when present and win over derived values: `game` {date, time, location}, `domain`,
`cycle`, `day_trial`, `chooser`, `request_index`.

Everything not read from a native fact is labelled `derived` (interpolated by wall time or line order).
"""

from __future__ import annotations

import bisect
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TOOL_VERSION = "live7-wp567.1"
ARCHIVE_ENV = "ARTI_STARDEW_ARCHIVE"
REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_JOURNAL = "journal/g1_journal.jsonl"
DEFAULT_GAME_MIN_PER_S = 1.37  # measured 1.37-1.47 game min per real second (PLAN §2.3)
DEFAULT_GAP_S = 5.0
SEASONS = ("spring", "summer", "fall", "winter")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CLASSES = ("VERIFIED", "CLEAN_FAIL", "UNCERTAIN", "NOT_READY", "PRECONDITION", "SAFETY_STOP")


def default_archive_root() -> Path:
    env = os.environ.get(ARCHIVE_ENV)
    return Path(env) if env else REPO_ROOT.parent / "arti-archive"


# --------------------------------------------------------------------------------------------- time/date


def tod_to_min(value: Any) -> int | None:
    """Stardew `time_of_day` (1420, "14:20", 2500) -> minutes after 00:00 of the game day."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return (v // 100) * 60 + v % 100
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(value))
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    if str(value).strip().isdigit():
        return tod_to_min(int(str(value).strip()))
    return None


def fmt_min(minutes: float | None) -> str:
    if minutes is None:
        return "?"
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def norm_date(value: Any) -> tuple[int | None, str, int] | None:
    """[6,"fall",5] / {"year":6,"season":"fall","day":5} / "fall 5" / "Y6 fall 5" -> (year, season, day)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        y, s, d = value
        if str(s).lower() in SEASONS:
            try:
                return (int(y) if y is not None else None, str(s).lower(), int(d))
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(value, dict):
        s = str(value.get("season", "")).lower()
        if s in SEASONS and value.get("day") is not None:
            y = value.get("year")
            return (int(y) if y is not None else None, s, int(value["day"]))
        return None
    if isinstance(value, str):
        q = parse_day_query(value)
        if q:
            return (q[2], q[0], q[1])
    return None


def date_label(date: tuple | None) -> str:
    if not date:
        return "?"
    y, s, d = date
    return f"Y{y} {s} {d}" if y is not None else f"{s} {d}"


def date_key(date: tuple | None) -> str | None:
    """Stable sortable key: 'Y0006-1-05' (season index)."""
    if not date:
        return None
    y, s, d = date
    return f"Y{(y or 0):04d}-{SEASONS.index(s)}-{d:02d}"


def weekday(date: tuple | None) -> str | None:
    return WEEKDAYS[(date[2] - 1) % 7] if date else None


def parse_day_query(text: str) -> tuple[str, int, int | None] | None:
    """"spring 5" / "Y6 fall 5" / "fall 5 y6" -> (season, day, year|None)."""
    t = text.strip().lower()
    year = None
    m = re.search(r"\by(?:ear)?\s*(\d+)\b", t)
    if m:
        year = int(m.group(1))
        t = (t[: m.start()] + t[m.end():]).strip()
    m = re.search(r"\b(spring|summer|fall|winter)\s+(\d{1,2})\b", t)
    if not m:
        return None
    return (m.group(1), int(m.group(2)), year)


def parse_at_query(text: str) -> tuple[str, int, int | None, int] | None:
    """"spring 5 14:00" -> (season, day, year|None, minutes)."""
    m = re.search(r"(\d{1,2}[:.]\d{2})\s*$", text.strip())
    if not m:
        return None
    day = parse_day_query(text[: m.start()])
    minutes = tod_to_min(m.group(1))
    if not day or minutes is None:
        return None
    return (*day, minutes)


def utc_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------- files


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    from scripts.free_play import storage
    return storage.digest_file(path)


def iter_files(root: Path, exclude: Iterable[str] = ()) -> list[Path]:
    from scripts.free_play import storage
    pats = list(exclude)
    return [p for p in storage.walk_files(root)
            if not any(fnmatch.fnmatch(p.relative_to(root).as_posix(), pat) for pat in pats)]


def file_kind(rel: str) -> str:
    r = rel.lower()
    name = r.rsplit("/", 1)[-1]
    if r.startswith("journal/provider_archive/"):
        return "provider_archive"
    if r.startswith("journal/") and r.endswith(".jsonl"):
        return "journal"
    if r.endswith(".png") or r.endswith(".jpg"):
        return "screenshot"
    if "smapi" in name and r.endswith(".txt"):
        return "smapi_log"
    if re.fullmatch(r"report-\d{8}t\d{6}z\.json", name) or name == "report.json":
        return "report"
    if ("pad-watch" in name or "pad_watch" in name) and r.endswith(".jsonl"):
        return "pad_watch"
    if name in ("cycle_state.json", "cycle_events.jsonl"):
        return name.split(".")[0]
    if name == "ui-op-log.jsonl":
        return "ui_op_log"
    if name.startswith("approval") and r.endswith(".json"):
        return "approval"
    if name in ("staging-log.jsonl", "host_actions.jsonl", "speech.jsonl", "clock_index.jsonl"):
        return name.split(".")[0]
    if r.endswith(".dll"):
        return "dll"
    if "stdout" in name or "stderr" in name:
        return "stdout"
    if name.endswith("-launch.json") or name.endswith("-exit.json") or name.startswith("launch-"):
        return "launch"
    return "other"


def find_journal(run_dir: Path) -> Path | None:
    p = run_dir / CANONICAL_JOURNAL
    if p.is_file():
        return p
    cands = sorted((run_dir / "journal").glob("*.jsonl")) if (run_dir / "journal").is_dir() else []
    return cands[0] if cands else None


def find_report(run_dir: Path) -> Path | None:
    from scripts.free_play import storage
    for name in ("report.json",):
        path = run_dir / name
        if storage.exists(path):
            return path
    cands = sorted(p for p in run_dir.glob("report-*.json") if re.fullmatch(r"report-\d{8}T\d{6}Z\.json", p.name))
    return cands[-1] if cands else None


def load_json(path: Path | None) -> Any:
    from scripts.free_play import storage
    if not path or not storage.exists(path):
        return None
    try:
        return json.loads(storage.read_bytes(path).decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return None


# --------------------------------------------------------------------------------------------- journal


def read_journal(path: Path) -> list[dict]:
    """Each record gets `_line` (1-based). Unparseable lines become ev `_unparsed`."""
    out: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.rstrip("\n")
            if not raw.strip():
                out.append({"ev": "_blank", "_line": n})
                continue
            try:
                d = json.loads(raw)
                if not isinstance(d, dict):
                    d = {"ev": "_unparsed", "value": d}
            except ValueError:
                d = {"ev": "_unparsed", "_raw": raw[:500]}
            d["_line"] = n
            out.append(d)
    return out


def ev_of(d: dict) -> str:
    return str(d.get("ev") or d.get("event") or d.get("kind") or "?")


def reason_of(d: dict) -> str | None:
    for key in ("reason", "verdict", "code", "status"):
        v = d.get(key)
        if isinstance(v, str) and v:
            return v
    act = d.get("action")
    if isinstance(act, dict) and isinstance(act.get("reason"), str):
        return act["reason"]
    if d.get("continuation_deny_site"):
        return str(d["continuation_deny_site"])
    return None


def compact(d: dict, limit: int = 1500) -> str:
    c = {k: v for k, v in d.items() if not k.startswith("_")}
    s = json.dumps(c, ensure_ascii=False, separators=(",", ":"), default=str)
    return s if len(s) <= limit else s[: limit - 3] + "..."


_DATE_EVENTS = {"day_start_detected", "day_start_cleared", "bed_wait_ended", "chest_memory_loaded"}


def _line_date(d: dict) -> tuple | None:
    game = d.get("game")
    if isinstance(game, dict) and norm_date(game.get("date")):
        return norm_date(game.get("date"))
    ev = ev_of(d)
    if ev in _DATE_EVENTS or re.fullmatch(r"part_[a-z0-9]+_started", ev) or ev == "live_started":
        return norm_date(d.get("date"))
    if ev == "date_bound_facts_invalidated":
        return norm_date(d.get("after"))
    return None


def _line_location(d: dict) -> str | None:
    game = d.get("game")
    if isinstance(game, dict) and game.get("location"):
        return str(game["location"])
    ev = ev_of(d)
    if ev == "location_changed" and d.get("to"):
        return str(d["to"])
    if ev == "action_result" and isinstance(d.get("action"), dict):
        act = d["action"]
        for side in ("after", "before"):
            s = act.get(side)
            if isinstance(s, dict) and s.get("location"):
                return str(s["location"])
    if ev == "home_route_survey" and d.get("location"):
        return str(d["location"])
    prs = d.get("post_row_state")
    if isinstance(prs, dict) and prs.get("location"):
        return str(prs["location"])
    return None


def _line_revisi(d: dict) -> list[int]:
    act = d.get("action")
    out = []
    if isinstance(act, dict):
        for side in ("before", "after"):
            s = act.get(side)
            if isinstance(s, dict) and isinstance(s.get("revisi"), int):
                out.append(s["revisi"])
    return out


def short_location(loc: str | None) -> str | None:
    """`FarmHouse7a8b9c~0d1e...` -> `FarmHouse`; keeps ordinary names."""
    if not loc:
        return loc
    m = re.match(r"([A-Za-z_]+?)[0-9a-f]{6}~[0-9a-f]+$", loc)
    return m.group(1) if m else loc


@dataclass
class ClockRow:
    line: int
    wall: float | None
    wall_src: str | None
    date: tuple | None
    time_min: float | None
    time_src: str | None
    location: str | None


@dataclass
class Clock:
    rows: list[ClockRow]
    rate: float  # game minutes per wall second
    rate_src: str
    anchors: list[dict] = field(default_factory=list)

    def at_line(self, line: int) -> ClockRow | None:
        return self.rows[line - 1] if 1 <= line <= len(self.rows) else None

    def line_for_wall(self, wall: float) -> int | None:
        walls = [(r.wall, r.line) for r in self.rows if r.wall is not None]
        if not walls:
            return None
        ws = [w for w, _ in walls]
        i = bisect.bisect_left(ws, wall)
        cands = [walls[j] for j in (i - 1, i) if 0 <= j < len(walls)]
        return min(cands, key=lambda c: abs(c[0] - wall))[1]


def _report_anchors(report: dict | None) -> list[dict]:
    out = []
    for i, step in enumerate((report or {}).get("steps") or []):
        ev = (step or {}).get("inspect_evidence") or {}
        motor = ((ev.get("facts") or {}).get("motor")) or {}
        tod = tod_to_min(motor.get("time_of_day"))
        rev = motor.get("revisi")
        if tod is None or not isinstance(rev, int):
            continue
        date = None
        if motor.get("season") and motor.get("day") is not None:
            date = norm_date([motor.get("year"), motor.get("season"), motor.get("day")])
        out.append({"step": i, "revisi": rev, "time": tod, "date": date, "location": motor.get("location")})
    return out


def build_clock(lines: list[dict], report: dict | None = None, default_rate: float = DEFAULT_GAME_MIN_PER_S) -> Clock:
    n = len(lines)
    # 1. wall time: exact on `at` lines, interpolated by line order elsewhere.
    known = [(i, float(d["at"])) for i, d in enumerate(lines)
             if isinstance(d.get("at"), (int, float)) and not isinstance(d.get("at"), bool)]
    walls: list[float | None] = [None] * n
    wsrc: list[str | None] = [None] * n
    for i, w in known:
        walls[i], wsrc[i] = w, "at"
    for (i0, w0), (i1, w1) in zip(known, known[1:]):
        for j in range(i0 + 1, i1):
            walls[j] = w0 + (w1 - w0) * (j - i0) / (i1 - i0)
            wsrc[j] = "interp"
    if known:
        for j in range(0, known[0][0]):
            walls[j], wsrc[j] = known[0][1], "carry"
        for j in range(known[-1][0] + 1, n):
            walls[j], wsrc[j] = known[-1][1], "carry"

    # 2. date + location carried forward.
    dates: list[tuple | None] = [None] * n
    locs: list[str | None] = [None] * n
    cur_date, cur_loc = None, None
    for i, d in enumerate(lines):
        nd = _line_date(d)
        if nd:
            cur_date = nd
        nl = _line_location(d)
        if nl:
            cur_loc = nl
        dates[i], locs[i] = cur_date, cur_loc
    first_loc = next((x for x in locs if x), None)
    for i in range(n):  # lines before the first location fact: the first known location
        if locs[i] is None:
            locs[i] = first_loc
        else:
            break
    first_date = next((x for x in dates if x), None)
    for i in range(n):  # lines before the first date fact belong to the first date
        if dates[i] is None:
            dates[i] = first_date
        else:
            break

    # 3. game-time anchors.
    anchors: list[dict] = []
    for i, d in enumerate(lines):
        game = d.get("game")
        if isinstance(game, dict) and tod_to_min(game.get("time")) is not None:
            anchors.append({"idx": i, "time": tod_to_min(game.get("time")), "src": "game"})
        elif ev_of(d) == "curfew" and tod_to_min(d.get("time_of_day")) is not None:
            anchors.append({"idx": i, "time": tod_to_min(d.get("time_of_day")), "src": "curfew"})
        elif ev_of(d) in ("day_start_cleared",) or (
                ev_of(d) == "day_start_detected" and not any(ev_of(x) == "day_start_cleared" for x in lines[i + 1:i + 12])) or (
                ev_of(d) == "bed_wait_ended" and d.get("reason") == "day_changed"
                and not any(ev_of(x) in ("day_start_cleared", "day_start_detected") for x in lines[i + 1:i + 12])):
            anchors.append({"idx": i, "time": 6 * 60, "src": "day_start"})
    rev_lines: list[tuple[int, int]] = []  # (revisi, idx) of action_result lines (monotone in practice)
    for i, d in enumerate(lines):
        for r in _line_revisi(d):
            rev_lines.append((r, i))
    rev_lines.sort()
    rev_keys = [r for r, _ in rev_lines]
    for a in _report_anchors(report):
        if not rev_lines:
            break
        k = bisect.bisect_left(rev_keys, a["revisi"])
        hi = rev_lines[k][1] if k < len(rev_lines) else n - 1
        lo = rev_lines[k - 1][1] if k > 0 else 0
        idx = hi
        if a["date"] and dates[idx] != a["date"]:
            same = [j for j in range(min(lo, hi), max(lo, hi) + 1) if dates[j] == a["date"]]
            if not same:
                continue
            idx = same[0] if dates[hi] != a["date"] and k < len(rev_lines) and hi > lo else same[-1]
        anchors.append({"idx": idx, "time": a["time"], "src": "report_step", "step": a["step"]})
    anchors.sort(key=lambda a: (a["idx"], 0 if a["src"] == "game" else 1))
    # One anchor per line; native journal facts beat report-step mapping.
    prio = {"game": 0, "curfew": 1, "day_start": 2, "report_step": 3}
    by_idx: dict[int, dict] = {}
    for a in anchors:
        if a["idx"] not in by_idx or prio[a["src"]] < prio[by_idx[a["idx"]]["src"]]:
            by_idx[a["idx"]] = a
    anchors = [by_idx[k] for k in sorted(by_idx)]
    # Drop anchors that run backwards inside one date (mapping noise).
    clean: list[dict] = []
    for a in anchors:
        prev = next((c for c in reversed(clean) if dates[c["idx"]] == dates[a["idx"]]), None)
        if prev and a["time"] < prev["time"] and a["src"] == "report_step":
            continue
        clean.append(a)
    anchors = clean

    # 4. rate (game min per wall s): least squares over one date's native anchors (not the 06:00 day start, whose
    #    wall time sits inside the HOST bed wait), widest wall span wins; else the measured default.
    rate, rate_src = default_rate, "default"
    by_date: dict[Any, list[dict]] = {}
    for a in anchors:
        by_date.setdefault(dates[a["idx"]], []).append(a)
    best = None
    for group in by_date.values():
        pts = [(walls[a["idx"]], a["time"]) for a in group if a["src"] != "day_start" and walls[a["idx"]] is not None]
        if len(pts) < 2:
            continue
        span = max(p[0] for p in pts) - min(p[0] for p in pts)
        if span < 60:
            continue
        mw = sum(p[0] for p in pts) / len(pts)
        mt = sum(p[1] for p in pts) / len(pts)
        den = sum((p[0] - mw) ** 2 for p in pts)
        if den <= 0:
            continue
        slope = sum((p[0] - mw) * (p[1] - mt) for p in pts) / den
        if 0.5 <= slope <= 3.0 and (best is None or span > best[1]):
            best = (slope, span)
    if best:
        rate, rate_src = round(best[0], 3), "derived"

    # 5. per-line game time: anchors exact, wall-interpolated between anchors of the same date,
    #    extrapolated at `rate` before the first / after the last anchor of a date.
    times: list[float | None] = [None] * n
    tsrc: list[str | None] = [None] * n
    for date, group in by_date.items():
        idxs = [i for i in range(n) if dates[i] == date]
        if not idxs:
            continue
        for i in idxs:
            prev = next((a for a in reversed(group) if a["idx"] <= i), None)
            nxt = next((a for a in group if a["idx"] >= i), None)
            wi = walls[i]
            if prev is not None and prev["idx"] == i:
                times[i], tsrc[i] = prev["time"], "anchor:" + prev["src"]
            elif prev is not None and nxt is not None:
                w0, w1 = walls[prev["idx"]], walls[nxt["idx"]]
                if wi is not None and w0 is not None and w1 is not None and w1 > w0:
                    f = (wi - w0) / (w1 - w0)
                else:
                    f = (i - prev["idx"]) / max(1, nxt["idx"] - prev["idx"])
                times[i], tsrc[i] = prev["time"] + (nxt["time"] - prev["time"]) * f, "interp"
            elif prev is not None and wi is not None and walls[prev["idx"]] is not None:
                times[i], tsrc[i] = prev["time"] + (wi - walls[prev["idx"]]) * rate, "extrap"
            elif nxt is not None and wi is not None and walls[nxt["idx"]] is not None:
                times[i], tsrc[i] = max(0.0, nxt["time"] - (walls[nxt["idx"]] - wi) * rate), "extrap"
            if times[i] is not None:  # a game day runs 06:00..26:00
                times[i] = min(max(times[i], 6 * 60), 26 * 60)
    rows = [ClockRow(i + 1, walls[i], wsrc[i], dates[i], times[i], tsrc[i], locs[i]) for i in range(n)]
    return Clock(rows, rate, rate_src, [dict(a, line=a["idx"] + 1) for a in anchors])


def clock_index_records(clock: Clock, lines: list[dict]) -> list[dict]:
    out = []
    for r, d in zip(clock.rows, lines):
        out.append({"line": r.line, "ev": ev_of(d), "wall": round(r.wall, 3) if r.wall is not None else None,
                    "wall_src": r.wall_src, "date": list(r.date) if r.date else None,
                    "time": fmt_min(r.time_min) if r.time_min is not None else None, "time_src": r.time_src,
                    "location": r.location})
    return out


# --------------------------------------------------------------------------------------------- classes


_RE_SAFETY = re.compile(r"(^|[:_ ])(stop_requested|stopped_by_owner|input_leak|identity_mismatch|"
                        r"terminal_cleanup_unconfirmed|permission_denied|approval_denied|pad_watch_violation|safety_stop|JournalCapacityReached|JournalPersistenceFault)",
                        re.I)
_RE_NOT_READY = re.compile(r"paused_recovery|runtime_not_ready|not_ready|^recovered_after:|^leg_ended_no_reason", re.I)
_RE_UNCERTAIN = re.compile(r"uncertain|possible_mutation|released_before_latch|unreconciled", re.I)
_RE_PRECONDITION = re.compile(r"precondition_unavailable|skipped_time|_time_window|missing_tool|no_food_candidate|"
                              r"shop_closed|_door_locked|locked_hours", re.I)


def classify(reason: str | None, verified: bool) -> str:
    """Map a terminal onto the PLAN §2.1 outcome classes. Never upgrades: only `verified` gives VERIFIED."""
    if verified:
        return "VERIFIED"
    r = reason or ""
    if _RE_SAFETY.search(r):
        return "SAFETY_STOP"
    if _RE_UNCERTAIN.search(r):
        return "UNCERTAIN"
    if _RE_NOT_READY.search(r):
        return "NOT_READY"
    if _RE_PRECONDITION.search(r):
        return "PRECONDITION"
    return "CLEAN_FAIL"


def reason_prefix(reason: str | None) -> str:
    if not reason:
        return "(none)"
    r = reason
    if r.startswith("part_a_unmet") or r.startswith("partial:") or r.startswith("stalled:"):
        return ":".join(r.split(":")[:2])
    return r.split(":")[0]


# --------------------------------------------------------------------------------------------- session model


@dataclass
class SkillRun:
    run_id: int
    skill: str
    arg: Any
    start_skill: str
    start_arg: Any
    by: str | None
    chooser_src: str
    start_line: int
    end_line: int | None
    terminal_ev: str | None
    verdict: str | None
    reason: str | None
    inputs: int | None
    decisions: int | None
    legs: list[dict]
    cls: str
    cause_line: int | None = None
    request_index: int | None = None
    task: str | None = None
    trigger: str | None = None


_UNSET = object()


@dataclass
class Row:
    line: int
    part: str
    task: str
    day: Any
    verified: bool
    reason: str | None


@dataclass
class Gap:
    start_line: int
    end_line: int
    wall_s: float
    game_min: float
    kind: str
    detail: str


@dataclass
class Session:
    cycle: str
    day_trial: str
    root: Path
    journal_path: Path | None
    cite_path: str
    lines: list[dict]
    report: dict | None
    manifest: dict | None
    clock: Clock
    runs: list[SkillRun]
    rows: list[Row]
    gaps: list[Gap]
    terminal: dict

    def cite(self, line: int | None) -> str:
        return f"{self.cite_path}:L{line}" if line else self.cite_path


def _first_uncertain_line(lines: list[dict], start: int, end: int) -> int | None:
    for d in lines[start - 1:end]:
        ev = ev_of(d)
        if ev == "approach_feedback" and str(d.get("status", "")).upper() == "UNCERTAIN":
            return d["_line"]
        if ev == "action_result" and _RE_UNCERTAIN.search(str((d.get("action") or {}).get("reason", ""))):
            return d["_line"]
    return None


def extract_runs(lines: list[dict], terminal_reason: str | None = None) -> list[SkillRun]:
    runs: list[SkillRun] = []
    cur: dict | None = None
    last_choice: dict | None = None
    pending_request: int | None = None
    last_trigger: str | None = None

    def close(end_line, terminal_ev, verdict, reason, inputs, decisions, skill=None, verified=False, arg=_UNSET):
        nonlocal cur
        if cur is None:
            return
        if inputs is None:  # unterminated run: count its input reservations (derived)
            inputs = sum(1 for x in lines[cur["start_line"] - 1:(end_line or len(lines))]
                         if ev_of(x) == "trial_reservation" and x.get("kind") == "input")
        cls = classify(reason or verdict, verified)
        cause = None
        if not verified:
            cause = _first_uncertain_line(lines, cur["start_line"], end_line or len(lines))
            if cause and cls in ("CLEAN_FAIL", "NOT_READY"):
                cls = "UNCERTAIN"
        runs.append(SkillRun(len(runs) + 1, skill or cur["skill"], arg if arg is not _UNSET else cur["arg"], cur["skill"],
                             cur["arg"], cur["by"], cur["chooser_src"],
                             cur["start_line"], end_line, terminal_ev, verdict, reason, inputs, decisions, cur["legs"],
                             cls, cause, cur.get("request_index"), None, cur.get("trigger")))
        cur = None

    for d in lines:
        ev, n = ev_of(d), d["_line"]
        if ev == "trial_reservation" and d.get("kind") == "provider" and isinstance(d.get("index"), int):
            pending_request = d["index"]
        elif ev == "skill_offer":
            last_trigger = d.get("trigger")
        elif ev == "skill_chosen":
            last_choice = d
        elif ev == "skill_started":
            if cur is not None:  # a start without a terminal: close it honestly
                close(n - 1, None, None, "no_terminal_before_next_start", None, None)
            choice = last_choice if last_choice and last_choice.get("skill") == d.get("skill") else None
            by, src = None, "none"
            if choice:
                if choice.get("chooser"):
                    by, src = str(choice["chooser"]), "chooser"
                elif choice.get("by"):
                    by, src = str(choice["by"]), "by_label"
            req = choice.get("request_index") if choice and isinstance(choice.get("request_index"), int) else None
            if req is None and by == "model":
                req = pending_request
            cur = {"skill": d.get("skill"), "arg": d.get("arg"), "start_line": n, "by": by, "chooser_src": src,
                   "legs": [], "request_index": req, "trigger": last_trigger}
            last_choice, last_trigger = None, None
        elif ev == "skill_leg_started" and cur is not None:
            cur["legs"].append({"skill": d.get("skill"), "arg": d.get("arg"), "line": n})
        elif ev == "skill_leg" and cur is not None:
            cur["legs"].append({"skill": d.get("skill"), "reason": d.get("reason"), "line": n, "boundary": True})
        elif ev in ("skill_finished", "skill_failed"):
            if cur is None:
                cur = {"skill": d.get("skill"), "arg": d.get("arg"), "start_line": n, "by": None, "chooser_src": "none",
                       "legs": [], "request_index": None}
            ok = ev == "skill_finished" and not re.search(r"fail|partial|unverified", str(d.get("verdict", "")), re.I)
            close(n, ev, d.get("verdict"), d.get("reason"), d.get("inputs"), d.get("decisions"), d.get("skill"), ok,
                  d.get("arg") if "arg" in d else _UNSET)
    if cur is not None:
        close(None, None, None, terminal_reason or "session_ended_without_terminal", None, None)
    return runs


def extract_rows(lines: list[dict]) -> list[Row]:
    out = []
    for d in lines:
        ev = ev_of(d)
        m = re.fullmatch(r"(part_[a-z0-9]+|live)_row", ev)
        if m:
            out.append(Row(d["_line"], m.group(1), str(d.get("task")), d.get("day"), bool(d.get("verified")), d.get("reason")))
            continue
        m = re.fullmatch(r"(part_[a-z0-9]+|live)_preface", ev)
        if m:
            ok = bool(d.get("sleep_verified"))
            out.append(Row(d["_line"], m.group(1), "preface_sleep", 0, ok, None if ok else d.get("reason") or "unverified"))
    return out


def attach_tasks(runs: list[SkillRun], rows: list[Row]) -> None:
    """A campaign row (`part_*_row`) is written right before its run's terminal line."""
    for r in runs:
        hi = (r.end_line or 10 ** 9) + 3
        for row in rows:
            if r.start_line <= row.line <= hi and (r.end_line is None or row.line >= r.end_line - 3):
                r.task = row.task
                break
        if r.task is None and r.trigger and ":" in r.trigger:  # campaign offer trigger, e.g. part_a:buy
            r.task = r.trigger.split(":", 1)[1]


def extract_gaps(lines: list[dict], clock: Clock, threshold_s: float = DEFAULT_GAP_S) -> list[Gap]:
    stamped = [(d["_line"], float(d["at"])) for d in lines
               if isinstance(d.get("at"), (int, float)) and not isinstance(d.get("at"), bool)]
    out = []
    for (l0, w0), (l1, w1) in zip(stamped, stamped[1:]):
        g = w1 - w0
        if g < threshold_s:
            continue
        inside = lines[l0:l1]  # lines l0+1 .. l1
        evs = [ev_of(d) for d in inside]
        if any(e in ("bed_waiting_for_host", "day_start_waiting", "host_wait", "bed_wait_ended") for e in evs):
            kind, detail = "host_wait", "bed/day-start wait for HOST"
        elif "no_progress_hold" in evs:
            kind, detail = "stall", "no_progress_hold"
        elif any(e == "continuation_not_scheduled" and "PAUSED" in str(d.get("continuation_deny_site", ""))
                 for e, d in zip(evs, inside)):
            kind, detail = "recovery", "PAUSED_RECOVERY"
        elif any(e == "move_reconciled" and "released_early" in str(d.get("reason", "")) for e, d in zip(evs, inside)):
            kind, detail = "released_early", "move released early (deadline) then reconciled"
        elif any(e == "trial_reservation" and d.get("kind") == "provider" for e, d in zip(evs, inside)):
            kind, detail = "provider", "model call"
        else:
            last = lines[l0 - 1]
            if ev_of(last) == "trial_reservation" and last.get("kind") == "input":
                kind, detail = "idle_after_input", "input done, next decision late (continuation wait/throttle)"
            else:
                kind, detail = "idle", "no journal activity"
        out.append(Gap(l0, l1, round(g, 1), round(g * clock.rate, 1), kind, detail))
    return out


def session_terminal(lines: list[dict], report: dict | None) -> dict:
    out = {"status": (report or {}).get("status"), "reason": (report or {}).get("reason"), "line": None, "ev": None}
    for d in reversed(lines):
        ev = ev_of(d)
        if ev == "skill_session_finished":
            out.update(line=d["_line"], ev=ev, session_status=d.get("status"), session_reason=d.get("reason"))
            if not out["reason"]:
                out["reason"] = d.get("reason")
            break
    if out["line"] is None:
        for d in reversed(lines):
            if ev_of(d) == "continuation_not_scheduled" and d.get("continuation_deny_site"):
                out.update(line=d["_line"], ev="continuation_not_scheduled", session_reason=d.get("continuation_deny_site"))
                break
    for d in lines:
        if ev_of(d) == "skill_budget_exhausted":
            out.setdefault("budget_line", d["_line"])
    return out


def load_session(root: Path, cycle: str | None = None, day_trial: str | None = None, cite_base: Path | None = None,
                 gap_s: float = DEFAULT_GAP_S) -> Session:
    root = Path(root)
    manifest = load_json(root / "manifest.json")
    # Archive labels identify the run. Journals from different rehearsals both say c1/day-0001.
    if manifest:
        cycle = cycle or manifest.get("cycle")
        day_trial = day_trial or manifest.get("day_trial")
    journal = find_journal(root)
    report = load_json(find_report(root))
    lines = read_journal(journal) if journal else []
    for d in lines:  # WP4 tags win when no label was given
        if cycle is None and d.get("cycle"):
            cycle = str(d["cycle"])
        if day_trial is None and d.get("day_trial"):
            day_trial = str(d["day_trial"])
        if cycle and day_trial:
            break
    if manifest:
        cycle = cycle or manifest.get("cycle")
        day_trial = day_trial or manifest.get("day_trial")
    cycle = cycle or root.parent.name
    day_trial = day_trial or root.name
    clock = build_clock(lines, report)
    terminal = session_terminal(lines, report)
    if journal is not None:
        try:
            cite_path = journal.relative_to(cite_base).as_posix() if cite_base else journal.as_posix()
        except ValueError:
            cite_path = journal.as_posix()
    else:
        cite_path = root.as_posix()
    runs, rows = extract_runs(lines, terminal.get("reason")), extract_rows(lines)
    attach_tasks(runs, rows)
    return Session(cycle, day_trial, root, journal, cite_path, lines, report, manifest, clock, runs, rows,
                   extract_gaps(lines, clock, gap_s), terminal)


def zero_input_streaks(runs: list[SkillRun], min_len: int = 2) -> list[dict]:
    """Consecutive completed failed runs with 0 inputs, across different tasks."""
    out, cur = [], []

    def flush():
        if len(cur) >= min_len:
            same = all((r.skill, r.arg) == (cur[0].skill, cur[0].arg) for r in cur)
            out.append({"skill": cur[0].skill if same else "mixed", "arg": cur[0].arg if same else None,
                        "reason": cur[0].reason if all(r.reason == cur[0].reason for r in cur) else "mixed", "count": len(cur),
                        "first_line": cur[0].start_line, "last_line": cur[-1].end_line,
                        "run_ids": [r.run_id for r in cur]})

    for r in runs:
        zero = r.end_line is not None and r.cls != "VERIFIED" and r.inputs == 0
        if zero:
            cur.append(r)
            continue
        flush()
        cur = []
    flush()
    return out


def session_dates(s: Session) -> list[tuple]:
    seen = []
    for r in s.clock.rows:
        if r.date and r.date not in seen:
            seen.append(r.date)
    return seen


RUN_EVIDENCE_KIND = "live7_run_evidence"


def discover_sessions(archive_root: Path, cycle: str | None = None) -> list[Path]:
    """Archived day-trial sessions (a Live7 run-evidence archive has no journal and is not a session)."""
    root = Path(archive_root)
    if not root.is_dir():
        return []
    out = []
    for m in sorted(root.glob("*/*/manifest.json")):
        if cycle and m.parent.parent.name != cycle:
            continue
        if (load_json(m) or {}).get("kind") == RUN_EVIDENCE_KIND:
            continue
        out.append(m.parent)
    return out


def discover_run_evidence(archive_root: Path, cycle: str | None = None) -> list[Path]:
    root = Path(archive_root)
    if not root.is_dir():
        return []
    return [m.parent for m in sorted(root.glob("*/*/manifest.json"))
            if (not cycle or m.parent.parent.name == cycle) and (load_json(m) or {}).get("kind") == RUN_EVIDENCE_KIND]


# --------------------------------------------------------------------------------------------- Live7 cycle evidence

# PLAN §1 cycle-1 day table (Y1 Spring 1-7): core rows (must) and planned rows (attempt, honest skip allowed),
# as skill names. Rain exempts S2 siram (native weather fact needed to apply it).
# Saturday owner decision: TV/mail are choices, never a required daily score row.
LIVE7_CORE = {1: ("bersihkan", "tanam", "siram", "tidur"), 2: ("siram", "tidur"),
              3: ("tidur",), 4: ("siram", "tidur"), 5: ("panen", "siram", "tidur"),
              6: ("siram", "tidur"), 7: ("siram", "tidur")}
# "skill:arg" pins the PLAN row's destination (S7 Farm->Town, ->Beach, ->Mine); the home leg of S1 never counts.
LIVE7_PLANNED = {1: ("tebang", "pergi_ke:town", "ngobrol_hadiah", "forage", "kirim"),
                 2: ("pergi_ke:beach", "tangkap_ikan", "beli", "tanam"),
                 3: ("tangkap_ikan", "tebang", "pecah_batu", "bersihkan", "buat", "simpan_ke_chest"),
                 4: ("beli", "tanam", "kirim", "ngobrol_hadiah", "forage"),
                 5: ("kirim", "pergi_ke:mine", "lawan_mine", "tambang_bijih"),
                 6: ("beli", "tanam", "buat", "pasang", "tangkap_ikan", "lawan_mine", "surat_quest"),
                 7: ("lawan_mine", "tambang_bijih", "tangkap_ikan", "kirim", "jual", "simpan_ke_chest", "upgrade_alat")}
LIVE7_RECOVERY_MAX = 2


@dataclass
class CycleEvidence:
    name: str                 # cycle folder name (journal `cycle` tag), e.g. "c1"
    source: str               # where it was read from
    archived: bool            # True only when read from an archived run-evidence archive
    state: dict
    events: list[dict]
    pad_rows: int | None      # None = pad_watch.jsonl missing
    pad_violations: list[dict]
    pad_lines_seen: int
    note: str | None = None

    @property
    def status(self) -> str | None:
        return self.state.get("status")

    def day(self, attempt: str | None) -> dict | None:
        for d in self.state.get("days", []):
            if any(a.get("id") == attempt for a in d.get("attempts", [])):
                return d
        return None


def load_cycle_evidence(folder: Path, *, archived: bool, source: str, note: str | None = None) -> CycleEvidence | None:
    folder = Path(folder)
    state = load_json(folder / "cycle_state.json")
    if not isinstance(state, dict):
        return None
    events = []
    if (folder / "cycle_events.jsonl").is_file():
        events = [d for d in read_journal(folder / "cycle_events.jsonl") if ev_of(d) not in ("_blank", "_unparsed")]
    pad = folder / "pad_watch.jsonl"
    rows, violations, seen = None, [], 0
    if pad.is_file():
        data = [d for d in read_journal(pad) if ev_of(d) != "_blank"]
        rows = len(data)
        violations = [d for d in data if d.get("violations")]
        seen = sum(len(d.get("pad_lines") or ()) for d in data)
    return CycleEvidence(folder.name, source, archived, state, events, rows, violations, seen, note)


def cycle_evidence_for(roots: Iterable[Path]) -> dict[str, CycleEvidence]:
    """Cycle folders under archived run-evidence archives (`live7/<c>/cycle_state.json`)."""
    out: dict[str, CycleEvidence] = {}
    for root in roots:
        for st in sorted(Path(root).glob("live7/*/cycle_state.json")):
            ev = load_cycle_evidence(st.parent, archived=True, source=Path(root).as_posix())
            if ev is not None:
                out.setdefault(ev.name, ev)
    return out


def journal_tags(lines: list[dict]) -> tuple[str | None, str | None]:
    """The WP4 `cycle` / `day_trial` tags (the cycle folder and attempt id of a Live7 trial)."""
    cyc = next((str(d["cycle"]) for d in lines if d.get("cycle")), None)
    dt = next((str(d["day_trial"]) for d in lines if d.get("day_trial")), None)
    return cyc, dt


def is_live7(s: "Session") -> bool:
    return any(ev_of(d) in ("part_live_started", "part_live_row") for d in s.lines)


def _live_rows(s: "Session", date: tuple) -> list[dict]:
    """part_live_row records of the runs that STARTED on ``date`` (a bed row is written after the wake)."""
    out, started = [], {}
    for d in s.lines:
        ev = ev_of(d)
        if ev == "skill_started":
            row = s.clock.at_line(d["_line"])
            started[str(d.get("skill"))] = row.date if row else None
        elif ev == "part_live_row":
            row = s.clock.at_line(d["_line"])
            if started.get(str(d.get("skill")), row.date if row else None) == date:
                out.append(d)
    return out


def _indexed(index_db: Path | None, s: "Session") -> bool | None:
    if index_db is None or not Path(index_db).is_file():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{Path(index_db).as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT 1 FROM sessions WHERE journal=? OR (cycle=? AND day_trial=?)",
                              (s.cite_path, s.cycle, s.day_trial)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row is not None


def session_cycle_evidence(s: "Session", d: Path, cycles: dict) -> CycleEvidence | None:
    """Archived run evidence first; else the raw cycle folder (a raw attempt's parent, or an archived attempt's
    still-existing source folder), labelled NOT archived."""
    if not is_live7(s):
        return None
    tag, _ = journal_tags(s.lines)
    name = tag or (s.manifest or {}).get("live7_cycle_dir")
    if name in cycles:
        return cycles[name]
    for folder in (Path(d).parent, Path((s.manifest or {}).get("source") or d).parent):
        if (folder / "cycle_state.json").is_file():
            return load_cycle_evidence(folder, archived=False, source=folder.as_posix(),
                                       note="cycle evidence read from the source run folder; it is NOT in the archive")
    return None


def crop_tile(ref: str) -> str:
    """`tf:Farm:75,18:HoeDirt` -> `Farm 75,18` (display only)."""
    parts = str(ref).split(":")
    return f"{parts[1]} {parts[2]}" if len(parts) >= 3 else str(ref)


# Native goal_route.weather tokens (GoalRouteRule.WeatherToken) under which vanilla waters every outdoor crop.
RAIN_WEATHER = frozenset({"rain", "storm", "green_rain"})


def crop_state(crop: Any) -> str:
    """Native crop lifecycle, shared by the planner memory and report ledger."""
    if not isinstance(crop, dict):
        return "gone"
    if crop.get("dead") is True:
        return "dead"
    if crop.get("ready_for_harvest") is True:
        return "ready"
    return "growing" if crop.get("phase") is not None or crop.get("qualified_item_id") else "gone"


def verified_crop_ref(row: dict, skill: str) -> str | None:
    if (row.get("skill") == skill and row.get("verified") is True
            and row.get("outcome_class", "VERIFIED") == "VERIFIED"):
        return ((row.get("evidence") or {}).get("observed") or {}).get("target_ref")
    return None


CROP_MEMORY_MAX = 48  # own plantings remembered across days; oldest planting leaves first (ADR 45)
CROP_STATES = ("growing", "ready", "dead")
DEAD_CROP_DAYS = 3  # native death day plus three days, persisted through resume (ADR 53)
UNCERTAIN_PLANTING_MAX = 8  # open uncertain planting attempts awaiting native corroboration (ADR 76)
RECONCILED_FROM_UNCERTAIN = "reconciled_from_uncertain"


def date_ordinal(date) -> int | None:
    date = norm_date(date)
    if not date or date[0] is None:
        return None
    return date[0] * 112 + SEASONS.index(date[1]) * 28 + date[2]


class CropMemory:
    """Own VERIFIED planting only; a persistent fact ledger of her own unfinished world effects (ADR 45).

    JSON-shaped state lives in the cycle's existing hints/resume. Wet dates follow crop_day's
    monotone-within-day rule. Only a fresh local window can disprove a remembered crop's existence
    (gone/dead); a crop that reads ``ready`` stays remembered, dated, until her VERIFIED panen, a local
    absence read. Native deaths remain dated facts for a bounded time, including season changes.
    Tiles are travel hints, never executable native crop facts.
    """
    def __init__(self, crops=None):
        self.crops = {}
        self._planted_refs = set()  # scoped to this process; masks stale scene copies after retirement
        # ADR 76: her UNCERTAIN planting attempts, awaiting native corroboration in this process/day.
        # Never persisted: a restart or a new day forgets them, so no crop is ever claimed without
        # the paired evidence (new crop on the exact tile + one seed of that kind gone).
        self.uncertain = []
        for record in (crops or {}).values():
            self._enroll(record)

    def _enroll(self, record):
        if not isinstance(record, dict):
            return
        location, tile = record.get("location"), record.get("tile")
        planted = norm_date(record.get("planted_date"))
        if (not isinstance(location, str) or not location or not isinstance(tile, (list, tuple))
                or len(tile) != 2 or any(type(v) is not int or v < 0 for v in tile)
                or not planted or planted[0] is None or not 1 <= planted[2] <= 28):
            return
        ref = f"tf:{location}:{tile[0]},{tile[1]}:HoeDirt"
        self._planted_refs.add(ref)
        state = record.get("state") if record.get("state") in CROP_STATES else "growing"
        ready = norm_date(record.get("ready_date")) if state == "ready" else None
        dead = norm_date(record.get("dead_date")) if state == "dead" else None
        self.crops[ref] = dict(location=location, tile=list(tile), planted_date=list(planted),
                              watered_date=list(wet) if (wet := norm_date(record.get("watered_date"))) else None,
                              watered_time=record.get("watered_time"),
                              last_seen_date=list(norm_date(record.get("last_seen_date")) or planted),
                              walk_date=record.get("walk_date"), walk_misses=record.get("walk_misses", 0),
                              walk_best=record.get("walk_best"),
                              item=record.get("item") if isinstance(record.get("item"), str) else None,
                              phase=record.get("phase") if type(record.get("phase")) is int else None,
                              state=state, ready_date=list(ready) if ready else None,
                              dead_date=list(dead) if dead else None,
                              dead_reason=record.get("dead_reason") if dead else None)
        if record.get("provenance") == RECONCILED_FROM_UNCERTAIN:
            self.crops[ref]["provenance"] = RECONCILED_FROM_UNCERTAIN

    @staticmethod
    def _plausible(record, crop, date):
        """A stale scene copy of the crop she replaced cannot outrank her VERIFIED planting: a scene read
        counts only when the planting is older than today, and a scene ``ready`` only once the crop is at
        least 60% through its vanilla growth days (fertiliser shortens, never below that). Local reads are
        always trusted."""
        from arti_stardew_seeds import crop_grow_days
        if crop_state(crop) == "dead":
            return True  # Native death is possible even on planting day; ripe records count too.
        age = (date_ordinal(date) or 0) - (date_ordinal(record["planted_date"]) or 0)
        if age < 1:
            return False
        if crop_state(crop) != "ready":
            return True
        days = crop_grow_days(crop.get("qualified_item_id") or record.get("item"))
        return days is None or age * 10 >= days * 6

    def _native(self, record, crop, date):
        """Positive native crop facts (identity, phase, lifecycle) from any fresh read; never absence or wetness."""
        if not isinstance(crop, dict):
            return
        if record["state"] == "dead":
            return  # Old scene copies cannot revive a crop. VERIFIED replanting enrolls a new record.
        if isinstance(crop.get("qualified_item_id"), str) and crop["qualified_item_id"]:
            record["item"] = crop["qualified_item_id"]
        if type(crop.get("phase")) is int:
            record["phase"] = crop["phase"]
        state = crop_state(crop)
        if state == "ready":
            if record["state"] != "ready":
                record.update(state="ready", ready_date=list(date))
        elif state == "dead":
            if record["state"] != "dead":
                # The native dead flag proves death; a boundary since the last live read
                # supplies its seasonal context. Never infer death from the date alone.
                prior = norm_date(record.get("last_seen_date"))
                reason = "season_change" if prior and prior[:2] != date[:2] and date[2] == 1 else "native_dead"
                record.update(state="dead", ready_date=None, watered_date=None, watered_time=None,
                              dead_date=list(date), dead_reason=reason)
        elif state == "growing" and record["state"] == "ready":
            record.update(state="growing", ready_date=None)  # regrowing crop (beans, berries) after a harvest

    def record(self, row, date=None, now=None):
        if row.get("skill") == "tanam" and row.get("outcome_class") == "UNCERTAIN":
            self.note_uncertain_planting(row, date)
            return
        if row.get("verified") is not True or row.get("outcome_class", "VERIFIED") != "VERIFIED":
            return
        date = norm_date((row.get("game") or {}).get("date") or date)
        now = (row.get("game") or {}).get("time", now)
        if row.get("skill") == "tanam":
            crop = (row.get("evidence") or {}).get("crop")
            if isinstance(crop, dict) and norm_date(crop.get("planted_date")) == date:
                self._enroll(dict(crop, state="growing", ready_date=None, dead_date=None))
                # A VERIFIED planting on that tile settles any older uncertain attempt there.
                planted = f"tf:{crop.get('location')}:{(crop.get('tile') or [None, None])[0]},{(crop.get('tile') or [None, None])[1]}:HoeDirt"
                self.uncertain = [a for a in self.uncertain if a["ref"] != planted]
                self.expire(date)
        ref = verified_crop_ref(row, "siram")
        if ref in self.crops and date:
            self.crops[ref]["watered_date"] = list(date)
            self.crops[ref]["watered_time"] = now
            self.crops[ref]["last_seen_date"] = list(date)
        self.crops.pop(verified_crop_ref(row, "panen"), None)

    def note_uncertain_planting(self, row, date=None):
        """ADR 76: an UNCERTAIN ``tanam`` row opens one attempt: the exact target tile, the seed kind and its
        count before the press. Generic: tile and seed come from the row's own evidence, never from a list."""
        evidence = row.get("evidence") or {}
        attempt = evidence.get("planting_attempt") or {}
        location, tile, seed = attempt.get("location"), attempt.get("tile"), attempt.get("seed")
        count = attempt.get("seed_count_before")
        date = norm_date((row.get("game") or {}).get("date") or attempt.get("date") or date)
        if (not isinstance(location, str) or not location or not isinstance(tile, (list, tuple)) or len(tile) != 2
                or any(type(v) is not int or v < 0 for v in tile) or not isinstance(seed, str) or not seed
                or type(count) is not int or count < 1 or not date or date[0] is None):
            return None
        ref = f"tf:{location}:{tile[0]},{tile[1]}:HoeDirt"
        if ref in self.crops:
            return None  # her VERIFIED crop already stands there; nothing to reconcile
        before = (evidence.get("reconcile") or {}).get("before") or {}
        if isinstance((before.get("crops") or {}).get(ref), dict):
            return None  # a crop was already on that tile before her press: it is not hers to claim
        self.uncertain = [a for a in self.uncertain if a["ref"] != ref]  # the newest attempt per tile
        self.uncertain.append(dict(ref=ref, location=location, tile=list(tile), seed=seed, seed_count_before=count,
                                   date=list(date), time=(row.get("game") or {}).get("time", attempt.get("time"))))
        del self.uncertain[:-UNCERTAIN_PLANTING_MAX]
        return self.uncertain[-1]

    @staticmethod
    def _seed_counts(inventory):
        """``{qid: stack}`` from either the live7 baseline dict or a wave1 inventory fact list; None when unknown."""
        if isinstance(inventory, dict):
            return {q: n for q, n in inventory.items() if isinstance(q, str) and type(n) is int}
        if isinstance(inventory, (list, tuple)):
            counts = {}
            for f in inventory:
                if isinstance(f, dict) and isinstance(f.get("qualified_item_id"), str) and type(f.get("stack")) is int:
                    counts[f["qualified_item_id"]] = counts.get(f["qualified_item_id"], 0) + f["stack"]
            return counts
        return None

    def reconcile_uncertain(self, date, crops, inventory, *, now=None):
        """ADR 76: corroborate open attempts from one fresh native read. ``crops`` = ``{ref: crop}`` of the
        crops that read now (any map), ``inventory`` = the bag that read with it. An attempt becomes her
        VERIFIED planting (original date, ``reconciled_from_uncertain``) only when BOTH hold on that read:
        a crop now stands on the exact target tile, and exactly one seed of that kind is gone. Returns the
        refs enrolled. Anything else leaves the crop unowned: a crop with no seed loss, a seed loss with no
        crop, another planter's crop, or an attempt older than today (forgotten, never guessed)."""
        date = norm_date(date)
        if not date or date[0] is None:
            return set()
        counts = self._seed_counts(inventory)
        enrolled = set()
        keep = []
        for attempt in self.uncertain:
            if norm_date(attempt["date"]) != date or attempt["ref"] in self.crops:
                continue  # a new day or her own VERIFIED planting on that tile ends the attempt
            crop = (crops or {}).get(attempt["ref"])
            if not isinstance(crop, dict) or crop_state(crop) == "gone" or counts is None:
                keep.append(attempt)
                continue
            if counts.get(attempt["seed"], 0) != attempt["seed_count_before"] - 1:
                keep.append(attempt)  # no (or a different) seed loss: not corroborated on this read
                continue
            record = dict(location=attempt["location"], tile=list(attempt["tile"]), planted_date=list(attempt["date"]),
                          watered_date=list(date) if crop.get("watered") is True else None,
                          watered_time=now if crop.get("watered") is True else None,
                          item=crop.get("qualified_item_id") if isinstance(crop.get("qualified_item_id"), str) else None,
                          phase=crop.get("phase") if type(crop.get("phase")) is int else None,
                          state="growing", ready_date=None, dead_date=None, provenance=RECONCILED_FROM_UNCERTAIN)
            self._enroll(record)
            self._native(self.crops[attempt["ref"]], crop, date)
            enrolled.add(attempt["ref"])
        self.uncertain = keep
        if enrolled:
            self.expire(date)
        return enrolled

    def observe(self, date, location, targets, window=None, *, now=None, inventory=None):
        date = norm_date(date)
        if not date:
            return set()
        if inventory is not None and self.uncertain:
            self.reconcile_uncertain(date, {ref: f.get("crop") for ref, f in (targets or {}).items()
                                            if isinstance(f, dict) and isinstance(f.get("crop"), dict)},
                                     inventory, now=now)
        window = window or {}
        center = window.get("center") or {}
        fresh = window.get("targets") or {}
        def covered(tile):
            if isinstance(tile, dict):
                tile = (tile.get("x"), tile.get("y"))
            return (isinstance(tile, (tuple, list)) and len(tile) == 2 and all(type(v) is int for v in tile)
                    and type(center.get("x")) is int and type(center.get("y")) is int
                    and abs(tile[0]-center["x"]) <= 1 and abs(tile[1]-center["y"]) <= 1)
        # Keep local absence authoritative on every read, even after a crop left the ledger.
        absent = {ref for ref, f in targets.items() if ref in self._planted_refs and f.get("terrain_type") == "HoeDirt"
                  and covered(f.get("tile")) and ref not in fresh}
        for ref, record in list(self.crops.items()):
            if (norm_date(record["watered_date"]) == date and type(now) is int
                    and (type(record["watered_time"]) is not int or now < record["watered_time"])):
                record.update(watered_date=None, watered_time=None)
            if record["location"] != location:
                continue
            local = covered(record["tile"])
            fact = fresh.get(ref) if local else targets.get(ref)
            crop = (fact or {}).get("crop")
            if local and crop_state(crop) == "gone":
                if record.get("state") != "dead":
                    self.crops.pop(ref)  # a dated native death remains a fact even after removal
                if ref not in fresh:
                    absent.add(ref)
            elif local:
                self._native(record, crop, date)
                record["last_seen_date"] = list(date)
                if isinstance(crop, dict) and crop.get("watered") is False:
                    record.update(watered_date=None, watered_time=None)
                elif isinstance(crop, dict) and crop.get("watered") is True:
                    record.update(watered_date=list(date), watered_time=now)
            elif fact is not None and crop_state(crop) in ("growing", "ready", "dead") and self._plausible(record, crop, date):
                # A wider scene read is a positive native fact about her crop (identity, phase, ready);
                # it never proves absence or today's wetness (HOST rollback / stale scene copies).
                self._native(record, crop, date)
                record["last_seen_date"] = list(date)
        self.expire(date)
        return absent

    def expire(self, date):
        """Retire dated native deaths after their retention window; keep the newest plantings.
        A season boundary is not a native death read (some crops survive it)."""
        date = norm_date(date)
        if not date or date[0] is None:
            return
        for ref, record in list(self.crops.items()):
            died = date_ordinal(record.get("dead_date"))
            if record.get("state") == "dead" and died is not None and date_ordinal(date) - died > DEAD_CROP_DAYS:
                self.crops.pop(ref)
        while len(self.crops) > CROP_MEMORY_MAX:
            oldest = min(self.crops, key=lambda r: (date_key(norm_date(self.crops[r]["planted_date"])), r))
            self.crops.pop(oldest)

    def skipped(self, date):
        return {ref for ref, r in self.crops.items()
                if norm_date(r["walk_date"]) == norm_date(date) and r["walk_misses"] >= 2}

    def walk_result(self, ref, date, *, verified, remaining, start_remaining, local_read=False):
        """Two failed/stalled approaches per crop/day; forward route legs are approach progress only."""
        record = self.crops.get(ref)
        if record is None or local_read:
            return False
        same_day = norm_date(record["walk_date"]) == norm_date(date)
        best = record["walk_best"] if same_day and record["walk_best"] is not None else start_remaining
        advanced = verified and type(remaining) is int and type(best) is int and remaining < best
        misses = (record["walk_misses"] if same_day else 0) + (not advanced)
        lengths = [v for v in (best, remaining) if type(v) is int]
        record.update(walk_date=list(date), walk_misses=misses, walk_best=min(lengths) if lengths else None)
        return misses >= 2

    def pending(self, date, weather):
        date = norm_date(date)
        self.expire(date)
        if not date or weather in RAIN_WEATHER:
            return {}
        skipped = self.skipped(date)
        return {ref: r for ref, r in self.crops.items() if ref not in skipped and r.get("state", "growing") == "growing"
                if norm_date(r["watered_date"]) != date and date_key(norm_date(r["planted_date"])) <= date_key(date)}

    def ready(self, date):
        """Her own crops last read ``ready`` and not yet harvested by her (travel hints; harvest is her choice)."""
        date = norm_date(date)
        self.expire(date)
        if not date:
            return {}
        skipped = self.skipped(date)
        return {ref: r for ref, r in self.crops.items() if ref not in skipped and r.get("state") == "ready"}

    def facts(self, date, *, limit=6):
        """Bounded decision facts about her own plantings (ADR 45): counts by native state and up to ``limit``
        dated lines, ready first. Facts only: harvesting, watering or revisiting stays her choice."""
        return own_crop_facts(self.crops, date, limit=limit)


def _crop_line(ref, record, date, *, with_tile=True):
    from arti_stardew_seeds import crop_grow_days, crop_name
    today = date_ordinal(date)
    planted = norm_date(record.get("planted_date"))
    name = crop_name(record["item"]) if record.get("item") else "tanaman"
    where = (crop_tile(ref) + " ") if with_tile else ""
    state = record.get("state", "growing")
    if state == "ready":
        since = date_label(norm_date(record.get("ready_date")))
        return "%s%s: SIAP PANEN sejak %s, belum kupanen (ditanam %s)" % (where, name, since, date_label(planted))
    if state == "dead":
        reason = "; season change" if record.get("dead_reason") == "season_change" else "; native dead"
        return "%s%s: mati (terlihat %s%s; ditanam %s)" % (where, name,
                date_label(norm_date(record.get("dead_date"))), reason, date_label(planted))
    seen = norm_date(record.get("last_seen_date"))
    age = today - date_ordinal(planted) if today is not None and date_ordinal(planted) is not None else None
    line = "%s%s: tumbuh" % (where, name) + (" fase %d" % record["phase"] if type(record.get("phase")) is int else "")
    line += ", ditanam %s" % date_label(planted) + (" (%d hari lalu)" % age if age is not None else "")
    wet = norm_date(record.get("watered_date")) == norm_date(date)
    line += ", tanahnya basah hari ini (disiram atau hujan)" if wet else ", belum tentu disiram hari ini"  # a48 T89: the wet flag is not her watering
    if seen == planted and seen != norm_date(date):
        line += ", belum kulihat lagi"
    elif seen and seen != norm_date(date):
        line += ", terakhir dilihat %s" % date_label(seen)
    days = crop_grow_days(record["item"]) if record.get("item") else None
    if days is not None and age is not None:
        left = days - age
        line += (", perkiraan siap ~%d hari lagi kalau disiram tiap hari" % left if left > 0
                 else ", lewat masa tumbuh, belum terbaca siap (mungkin kurang disiram)")
    return line


def own_crop_facts(crops, date, *, limit=6):
    """``{total, ready, growing, dead, lines, note}`` or None when she has no remembered planting."""
    if not crops:
        return None
    rows = sorted(crops.items(), key=lambda kv: ({"ready": 0, "growing": 1, "dead": 2}.get(kv[1].get("state"), 1),
                                                 date_key(norm_date(kv[1].get("planted_date"))) or "", kv[0]))
    counts = {s: sum(1 for _, r in rows if r.get("state", "growing") == s) for s in CROP_STATES}
    lines = [_crop_line(ref, r, date)[:180] for ref, r in rows[:limit]]
    out = dict(total=len(rows), ready=counts["ready"], growing=counts["growing"], dead=counts["dead"], lines=lines)
    if len(rows) > limit:
        out["more"] = len(rows) - limit
    out["note"] = ("Fakta tanaman yang kamu tanam sendiri (terverifikasi), diingat lintas hari. Tanggal absolut. "
                   "Panen/siram/menengok itu pilihanmu; yang belum dilihat lagi bisa sudah berubah.")
    return out


def own_crop_reflection_facts(crops, date, *, limit=6):
    """Citable ``{ref, text}`` bedtime facts about her own plantings, ready first (fix13c progress shape)."""
    facts = own_crop_facts(crops, date, limit=limit)
    if not facts:
        return []
    rows = sorted(crops.items(), key=lambda kv: ({"ready": 0, "growing": 1, "dead": 2}.get(kv[1].get("state"), 1),
                                                 date_key(norm_date(kv[1].get("planted_date"))) or "", kv[0]))
    out = [dict(ref="c:" + crop_tile(ref).replace(" ", ":"), text=("tanamanku di " + _crop_line(ref, r, date))[:140])
           for ref, r in rows[:limit]]
    if facts.get("more"):
        out.append(dict(ref="c:more", text="tanamanku yang lain: %d lagi (%d siap panen total, %d tumbuh, %d mati)"
                        % (facts["more"], facts["ready"], facts["growing"], facts["dead"])))
    return out


def day_weather(s: "Session", first: int, last: int) -> str | None:
    """Today's native weather (goal_route.weather, journalled in live7_baseline `before.weather` since DLL
    0ff66c60). Island locations have their own weather and are skipped; None = not journalled (older run)."""
    seen = []
    for d in s.lines[max(0, first - 1):last]:
        before = d.get("before") if ev_of(d) == "live7_baseline" else None
        w = before.get("weather") if isinstance(before, dict) else None
        if isinstance(w, str) and w and not str(before.get("location") or "").startswith("Island"):
            seen.append(w)
    if not seen:
        return None
    # The valley's weather is fixed for the day; a mixed read prefers the rain token (vanilla watered the soil).
    return next((w for w in seen if w in RAIN_WEATHER), seen[-1])


def crop_day(s: "Session", first: int, last: int, live: list[dict]) -> dict:
    """One day's crop readback (READ): every live crop in the live7_baseline `before.crops` blocks and the
    VERIFIED siram rows' `observed.target_ref`. Watered state is monotone within a day (vanilla soil only dries
    overnight), so any `watered true` observation is true; a `false` can be a stale global-scene copy
    (live7-game-m2), so only VERIFIED siram rows or a later wet observation count as watered.

    Not scored (never needs water, as the planner's crop_needs_water): a crop seen `ready_for_harvest true`,
    a crop a VERIFIED panen row targeted, and a dead crop. They are listed in `ready`/`harvested`/`dead`
    and the cross-day ledger drops them after this day.

    A crop seen wet before ARTI's first VERIFIED siram on it is natively wet (rain, or watered by someone else):
    auto-watered, never needed. `rain`: the native weather (live7_baseline `before.weather`) is rain/storm/
    green_rain; only when no weather was journalled, inferred = scored crops were seen and all natively wet."""
    seen: dict[str, dict] = {}
    dead: set[str] = set()
    for d in s.lines[max(0, first - 1):last]:
        if ev_of(d) != "live7_baseline":
            continue
        for ref, c in (((d.get("before") or {}).get("crops")) or {}).items():
            if not isinstance(c, dict):
                continue
            if crop_state(c) == "dead":
                dead.add(str(ref))
                continue
            o = seen.setdefault(str(ref), {"first_line": d["_line"], "phase": None, "wet_line": None,
                                           "item": c.get("qualified_item_id"), "ready": False})
            if type(c.get("phase")) is int and (o["phase"] is None or c["phase"] > o["phase"]):
                o["phase"] = c["phase"]
            if c.get("watered") is True and o["wet_line"] is None:
                o["wet_line"] = d["_line"]
            if crop_state(c) == "ready":
                o["ready"] = True
    watered: dict[str, int] = {}
    harvested: set[str] = set()
    failed = 0
    planted = CropMemory()
    for r in live:
        if str(r.get("skill")) == "tanam":
            planted.record(r, _line_date(r))
            for ref, record in planted.crops.items():
                wet = norm_date(record["watered_date"]) == norm_date(record["planted_date"])
                seen.setdefault(ref, {"first_line": r.get("_line"), "phase": 0,
                                      "wet_line": r.get("_line") if wet else None, "item": None, "ready": False})
        ref = verified_crop_ref(r, str(r.get("skill")))
        if str(r.get("skill")) == "panen":
            if r.get("verified") and ref:
                harvested.add(str(ref))
            continue
        if str(r.get("skill")) != "siram":
            continue
        if r.get("verified") and ref:
            watered.setdefault(str(ref), r.get("_line") or 0)
            seen.setdefault(str(ref), {"first_line": r.get("_line"), "phase": None, "wet_line": r.get("_line"),
                                       "item": None, "ready": False})
        elif not r.get("verified"):
            failed += 1
    ready = sorted(ref for ref, o in seen.items() if o["ready"])
    unscored = set(ready) | harvested | dead
    scored = [ref for ref in seen if ref not in unscored]
    native = sorted(ref for ref in scored
                    if seen[ref]["wet_line"] is not None and seen[ref]["wet_line"] < watered.get(ref, 10 ** 12))
    weather = day_weather(s, first, last)
    if weather is not None:
        rain, rain_source = weather in RAIN_WEATHER, "native"
    else:
        rain, rain_source = bool(scored) and len(native) == len(scored), "inferred"
    needed = [] if rain else sorted(set(scored) - set(native))
    missed = [ref for ref in needed if ref not in watered and seen[ref]["wet_line"] is None]
    return {"seen": seen, "watered": sorted(watered), "native_wet": native, "rain": rain, "needed": needed,
            "missed": missed, "siram_failed": failed, "weather": weather, "rain_source": rain_source,
            "ready": ready, "harvested": sorted(harvested), "dead": sorted(dead),
            "planted": sorted(planted.crops),
            "tanam_verified": sum(1 for r in live if str(r.get("skill")) == "tanam" and r.get("verified"))}


def live7_day_rows(s: "Session", archived: bool, cycle: CycleEvidence | None = None,
                   index_db: Path | None = None, indexed: bool | None = None) -> list[dict]:
    """Strict PLAN §1 M1-M8 for a Live7 trial. Each value is True/False/None (None = not derivable);
    `notes` explains every value. A day is matang only when all eight are True."""
    rows = day_rows(s, archived)
    _, attempt = journal_tags(s.lines)
    attempt = attempt or s.day_trial
    cday = cycle.day(attempt) if cycle else None
    indexed = _indexed(index_db, s) if indexed is None else indexed
    for rec in rows:
        notes: dict[str, str] = {}
        if not rec.get("played"):
            rec["notes"] = notes
            rec["matang"] = None
            continue
        first, last = rec["first_line"], rec["last_line"]
        live = _live_rows(s, rec["date"])
        last = max(last, max((r["_line"] for r in live), default=last))
        by_skill: dict[str, list[dict]] = {}
        for r in live:
            by_skill.setdefault(str(r.get("skill")), []).append(r)
        # M1: native bed time from the tidur row (observed.bed_time), day change verified, bed <= 24:00.
        sleep = next((r for r in by_skill.get("tidur", ()) if r.get("verified")
                      and r.get("reason") == "observed_day_change"), None)
        bed = ((sleep or {}).get("evidence") or {}).get("observed", {}).get("bed_time") if sleep else None
        if isinstance(bed, int):
            rec["bed_time"] = fmt_min(tod_to_min(bed))
        rec["m1"] = bool(sleep) and (bed is None or bed <= 2400) and not any(
            ev_of(d) in ("passed_out", "pass_out") for d in s.lines[first - 1:last])
        notes["m1"] = (f"tidur VERIFIED observed_day_change, bed {rec['bed_time']}" if rec["m1"]
                       else "no verified tidur with observed_day_change before 24:00")
        date = rec["date"]
        day_no = date[2] if date and date[0] == 1 and date[1] == "spring" and date[2] in LIVE7_CORE else None
        planted = any(r.get("verified") for r in by_skill.get("tanam", ()))
        siram = by_skill.get("siram", [])
        crops = crop_day(s, first, last, live)
        rec["crops"] = crops
        rec["weather"] = crops["weather"]
        if crops["seen"] or crops["rain"]:
            # live7-game-m2: score watering against the crops that were dry today, not against siram rows.
            # A tile already wet (rain, or watered earlier) is auto-watered; a failed siram on it is not a miss.
            rec["m2"] = not crops["missed"]
            if crops["rain"] and crops["rain_source"] == "native":
                notes["m2"] = (f"native weather {crops['weather']}: every crop auto-watered "
                               f"({len(crops['seen'])} crops seen)")
            elif crops["rain"]:
                notes["m2"] = (f"rain/native-wet day: all {len(crops['seen'])} crops seen were wet before any siram "
                               "(auto-watered; no native weather journalled)")
            else:
                dry = crops["needed"]
                notes["m2"] = (f"dry crops watered {len(set(dry) - set(crops['missed']))}/{len(dry)}"
                               + (f"; native weather {crops['weather']}" if crops["weather"] else "")
                               + (f"; {len(crops['native_wet'])} already wet natively (auto-watered)"
                                  if crops["native_wet"] else "")
                               + (f"; missed {', '.join(crop_tile(r) for r in crops['missed'])}" if crops["missed"] else ""))
            unscored = sorted(set(crops["ready"]) | set(crops["harvested"]))
            if unscored:
                notes["m2"] += (f"; {len(unscored)} ready/harvested crops not scored "
                                f"({', '.join(crop_tile(r) for r in unscored)})")
            if crops["siram_failed"]:
                notes["m2"] += f"; {crops['siram_failed']} siram rows without a dry target (not scored)"
        elif siram:
            rec["m2"] = all(r.get("verified") for r in siram)
            notes["m2"] = f"siram rows {sum(bool(r.get('verified')) for r in siram)}/{len(siram)} VERIFIED"
        elif not planted and not any(ev_of(d) == "part_live_row" and d.get("skill") == "tanam" and d.get("verified")
                                     for d in s.lines[:first - 1]):
            rec["m2"] = True
            notes["m2"] = "n/a: no enrolled crop tiles (no tanam VERIFIED in this journal)"
        else:
            rec["m2"] = False
            notes["m2"] = "crops planted but no siram row and no native rain fact"
        # M3: zero operator input + pad_watch exit 0 + ui-op-log audit clean.
        if cycle is None or cycle.pad_rows is None:
            rec["m3"] = None
            notes["m3"] = "pad_watch evidence missing"
        elif cycle.pad_violations:
            rec["m3"] = False
            notes["m3"] = f"pad_watch violations in {len(cycle.pad_violations)} rows"
        else:
            rec["m3"] = None
            notes["m3"] = ("pad_watch clean (%d rows, %d PAD lines seen%s); ui-op-log audit not run here" % (
                cycle.pad_rows, cycle.pad_lines_seen,
                "; 0 PAD lines = it may have read the HOST SMAPI log" if not cycle.pad_lines_seen else ""))
        # M4: every PLAN §1 core row VERIFIED (cycle-1 table, Y1 Spring 1-7 only).
        if day_no is None:
            rec["m4"] = None
            notes["m4"] = "no PLAN core table for this date"
        else:
            missing = [k for k in LIVE7_CORE[day_no] if not any(r.get("verified") for r in by_skill.get(k, ()))]
            rec["m4"] = not missing
            notes["m4"] = ("all core rows VERIFIED" if not missing else
                           "core rows not VERIFIED: " + ", ".join(
                               f"{k} ({'not attempted' if k not in by_skill else 'failed'})" for k in missing))
        # M5: planned rows attempted -> >=60% VERIFIED (every row has an honest class by construction).
        if day_no is None:
            rec["m5"] = None
            notes["m5"] = "no PLAN planned table for this date"
        else:
            tried = [r for k in LIVE7_PLANNED[day_no] for r in by_skill.get(k.split(":")[0], ())
                     if r.get("task") != "sleep" and (":" not in k or str(r.get("arg")) == k.split(":", 1)[1])]
            ok = sum(bool(r.get("verified")) for r in tried)
            rec["m5"] = (ok / len(tried) >= 0.6) if tried else None
            notes["m5"] = (f"{ok}/{len(tried)} attempted planned rows VERIFIED" if tried
                           else "no planned row attempted")
        # M6: no session-ending hold except a safety stop; <=2 automatic recoveries; the cycle went on.
        recoveries = sum(1 for d in s.lines[first - 1:last] if ev_of(d) == "live7_recovery")
        problems = []
        if recoveries > LIVE7_RECOVERY_MAX:
            problems.append(f"{recoveries} automatic recoveries")
        if any(r.get("outcome_class") == "SAFETY_STOP" for r in live):
            problems.append("a SAFETY_STOP row")
        if cycle is None:
            rec["m6"] = None
            notes["m6"] = "cycle evidence missing; " + ("; ".join(problems) or "journal clean")
        else:
            if not cday or cday.get("status") != "ended":
                problems.append("cycle %s; day %s (no night checkpoint/end-day)" % (
                    cycle.status, (cday or {}).get("status", "unknown")))
            rec["m6"] = not problems
            notes["m6"] = "; ".join(problems) or f"day ended in cycle {cycle.name}; {recoveries} recoveries"
        if rec.get("m7") is not False:
            rec["m7"] = True
            notes["m7"] = "Live7: no numeric caps; none raised or hit"
        else:
            notes["m7"] = "a cap was hit"
        # M8: archived + cycle evidence archived + indexed.
        if not archived:
            rec["m8"] = False
            notes["m8"] = "session not archived"
        elif cycle is None or not cycle.archived:
            rec["m8"] = False
            notes["m8"] = "cycle-level evidence (cycle state/events, pad_watch, logs, screenshots) not archived"
        elif indexed is None:
            rec["m8"] = None
            notes["m8"] = "archived; index not found/checked"
        else:
            rec["m8"] = bool(indexed)
            notes["m8"] = "archived, cycle evidence archived, " + ("indexed" if indexed else "NOT indexed")
        rec["notes"] = notes
        rec["matang"] = all(rec[f"m{i}"] is True for i in range(1, 9))
    return rows


def day_rows(s: "Session", archived: bool) -> list[dict]:
    """Per game date: wake/bed and the M1-M8 booleans that old journals can support (None = not derivable)."""
    out = []
    dates = session_dates(s)
    report = s.report or {}
    limits, inputs = report.get("limits") or {}, report.get("physical_inputs")
    for date in dates:
        idx = [r for r in s.clock.rows if r.date == date]
        first, last = idx[0], idx[-1]
        sleep = None
        for run in s.runs:
            if run.skill == "tidur" and run.cls == "VERIFIED" and run.end_line:
                start = s.clock.at_line(run.start_line)
                if start and start.date == date:
                    sleep = run
                    break
        bed_time = fmt_min(s.clock.at_line(sleep.start_line).time_min) if sleep else None
        rec = {"date": date, "weekday": weekday(date), "weather": None, "first_line": first.line, "last_line": last.line,
               "wake_time": fmt_min(first.time_min) if first.time_min is not None else None,
               "bed_time": bed_time, "sleep_line": sleep.end_line if sleep else None}
        bed_min = s.clock.at_line(sleep.start_line).time_min if sleep else None
        played = any(first.line <= r.start_line <= last.line for r in s.runs)
        if not played:  # e.g. the morning reached after the last sleep: nothing was lived on it
            rec.update({f"m{i}": None for i in range(1, 9)}, played=False)
            out.append(rec)
            continue
        rec["played"] = True
        rec["m1"] = bool(sleep) and bed_min is not None and bed_min <= 24 * 60
        rec["m2"] = None  # needs native soil receipts / weather fact (WP4/S29)
        rec["m3"] = None  # needs pad_watch check + ui-op audit verdict
        day_rows_ = [r for r in s.rows if first.line <= r.line <= last.line and r.task != "preface_sleep"]
        rec["m4"] = (all(r.verified for r in day_rows_) if day_rows_ else None)
        rec["m5"] = None  # needs the planner's core/planned split (Live7 planner)
        nr = [r for r in s.runs if first.line <= r.start_line <= last.line and r.cls in ("NOT_READY", "SAFETY_STOP")]
        rec["m6"] = not nr and not (s.terminal.get("status") == "HELD" and last.line == len(s.lines))
        within = None
        if limits and inputs is not None:
            within = inputs <= (limits.get("input") or 10 ** 9)
            if "budget" in str(s.terminal.get("reason") or "") and first.line <= (s.terminal.get("line") or 0) <= last.line:
                within = False  # a cap was hit on this day
        rec["m7"] = within
        rec["m8"] = archived
        out.append(rec)
    return out
