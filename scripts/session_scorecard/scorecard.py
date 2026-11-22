"""SCORE1 (ADR 248): one read-only pass over a session run folder's journals.

Read: `<run>/runtime/live7/<cycle>/day-*/journal/g1_journal.jsonl` (or the same below a cycle folder or a free-play
run), each cycle's `cycle_events.jsonl` and the run's `rescues.tsv`. Nothing else in the run folder is opened and
nothing is written there. A journal still being written is read up to the bytes on disk; a torn last line is
skipped and counted. Rows are streamed one at a time: memory stays at one journal file.

Stuck events come from ADR 241's effect clock rows (`live7_effect_clock`, `live7_effect_ladder`) where an attempt
has them, else from ADR 241's own archive replay (`arti_runtime.effect_clock.replay`: a verified skill, a map
change, an item, tool or money effect, a menu she opened or closed, the day). The replay runs on every attempt, so
two runs are always compared by the same rule.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arti_runtime.effect_clock import replay

WIB = timezone(timedelta(hours=7))
STUCK_S = 60.0  # ADR 241 rung 1; the healthy 2026-11-18 21:38 run's longest stretch without an effect was 62.9 s
MENU_HELD_S = 60.0
WINDOW = 20  # the hands signal: the share of her last 20 skill runs that finished
DEAD_SHARE = 0.3  # below this share, with at least DEAD_MIN_RUNS runs in the window, her hands count as dead
DEAD_MIN_RUNS = 10
OBSERVE_CAP_S = 300.0  # a stretch longer than this without any run ending observes her hands for this long only
MIN_COVERAGE = 0.5  # Q1 compares live hands only when the signal observed at least this share of the active time
BUCKET_S = 900.0  # the table's hands series: one value per 15 minutes of wall time
SLEPT, PASSED_OUT = "live7_sleep_verified", "live7:passed_out"  # arti_stardew_live7.py SLEEP_VERIFIED_REASON, PASSED_OUT_REASON
TRIP_SKILL, TALK_SKILL = "pergi_ke", "ngobrol_hadiah"
ROUTINE_CYCLE = frozenset({"cycle_started", "day_trial_started", "checkpoint", "night_saved", "host_wait",
                           "host_wait_ended", "day_ended"})
JOURNAL_GLOBS = ("runtime/live7/*/day-*/journal/g1_journal.jsonl", "live7/*/day-*/journal/g1_journal.jsonl",
                 "*/day-*/journal/g1_journal.jsonl", "day-*/journal/g1_journal.jsonl")
CANNOT_KNOW = (
    "dead hands that do not fail: a run of waits or speech with no skill result does not move the hands share "
    "(the stuck list and 'last finished skill' cover it)",
    "who she talked to without a talk row: only `objective_progress` talk_npc, a finished `ngobrol_hadiah` and "
    "dialogue boxes (`screen_text` speaker) name people",
    "whether a place is new to her life: her memory is not read; 'places' are the maps of this run",
    "gold from tonight's shipping: `live7_wallet` is written at boundaries (mostly mornings), the bin pays overnight, "
    "and no row prices a shipped item",
    "a festival without a native event row: festivals are `live7_native_event` ids starting 'festival' and the "
    "festival map (Temp)",
    "hand rescues not written in the run's rescues.tsv by the operator",
    "stuck events of older runs exactly as the live clock would count them: the replay has no machine-stall or bed "
    "hold and no honoured wait",
    "menus between two native_vitals rows (a menu shorter than the row interval, about 1 s)",
)


def _num(v):
    return type(v) in (int, float)


def wib(at, fmt="%H:%M:%S"):
    return datetime.fromtimestamp(at, WIB).strftime(fmt) if _num(at) else "?"


def _natural(path):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path.as_posix())]


def journals(run):
    run = Path(run)
    for pattern in JOURNAL_GLOBS:
        found = sorted(run.glob(pattern), key=_natural)
        if found:
            return found
    return []


def read_rows(path, stats):
    """The rows of one journal that may still be being written. A last line that is not JSON yet (the writer is
    in the middle of it) is skipped and counted, never an error; a broken line elsewhere is counted as bad."""
    with open(path, "rb") as f:
        data = f.read()
    stats["bytes"] += len(data)
    lines = data.split(b"\n")
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            stats["torn_last_line" if i == last else "bad_lines"] += 1
            continue
        if isinstance(row, dict):
            stats["rows"] += 1
            yield row


def _date(v):
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(x, (int, str)) for x in v):
        return tuple(v)
    return None


def _person(arg):
    """The person a finished talk names; `warga` ("a villager") is the skill's unnamed target, not a name."""
    name = str(arg or "").split(" ke ")[0].strip()
    if not name or name.lower() == "warga":
        return None
    return name[:1].upper() + name[1:]


class Attempt:
    """One day attempt's journal, read in one streaming pass."""

    def __init__(self, path):
        path = Path(path)
        self.path = path
        self.day_trial = path.parents[1].name
        self.cycle = path.parents[2].name if len(path.parents) > 2 else ""
        self.t = self.first = self.last = None
        self.outcomes = []  # (t, finished, skill, verdict or reason)
        self.started = 0
        self.failures, self.finished, self.trips = Counter(), Counter(), Counter()
        self.unstarted, self.fallbacks = Counter(), []
        self.ends = []
        self.dates = []
        self.places = {}
        self.money = []
        self.shipped, self.sold = Counter(), Counter()
        self.bag_requests, self.moves = set(), []
        self.talked, self.speakers = {}, Counter()
        self.festivals = {}
        self.menu = self.menu_since = None
        self.menus_held, self.menu_held_rows = [], 0
        self.clock = None
        self.clock_rows = 0
        self.clock_closed = set()
        self.clock_stuck, self.replay_stuck = [], []
        self.replay_effects = 0
        self.rungs = Counter()

    def feed(self, rows):
        for row in rows:
            self.note(row)
            yield row

    def note(self, row):
        at = row.get("at")
        if _num(at) and at > 1e9:
            self.t = at
            if self.first is None:
                self.first = at
            self.last = at if self.last is None else max(self.last, at)
        t = self.t
        game = row.get("game") if isinstance(row.get("game"), dict) else None
        if game:
            self._seen(_date(game.get("date")), game.get("location"), t)
        ev = row.get("ev")
        handler = getattr(self, "_ev_" + ev, None) if isinstance(ev, str) and ev.isidentifier() else None
        if handler is not None:
            handler(row, t)

    def _seen(self, date, place, t):
        if date is not None and (not self.dates or self.dates[-1] != date) and date not in self.dates:
            self.dates.append(date)
        if isinstance(place, str) and place and place not in self.places:
            self.places[place] = t

    # -- skills, nights, trips
    def _ev_skill_started(self, row, t):
        self.started += 1

    def _ev_skill_finished(self, row, t):
        skill = row.get("skill")
        self.outcomes.append((t, True, skill, row.get("verdict")))
        self.finished[skill] += 1
        if skill == TRIP_SKILL:
            self.trips["finished"] += 1
        if skill == TALK_SKILL and _person(row.get("arg")):
            self.talked.setdefault(_person(row.get("arg")), t)

    def _ev_skill_failed(self, row, t):
        skill = row.get("skill")
        self.failures[str(row.get("reason"))] += 1  # the failure breakdown keeps refusals before a start too
        if row.get("started") is False:
            # A refusal before any start (`_skill_fail_unstarted`, written instead of `skill_started`): no run of her
            # hands, so it is no outcome of the hands signal and never cancels a run that is really open.
            self.unstarted[str(row.get("reason"))] += 1
            return
        self.outcomes.append((t, False, skill, row.get("reason")))
        if skill == TRIP_SKILL:
            self.trips["failed"] += 1

    def _ev_decision_provider_fallback(self, row, t):
        self.fallbacks.append(dict(at=row["at"] if _num(row.get("at")) else t, provider=row.get("provider"),
                                   to=row.get("fallback_provider"), reason=row.get("reason"),
                                   cycle=self.cycle, day_trial=self.day_trial))

    def _ev_skill_session_finished(self, row, t):
        self.ends.append(str(row.get("reason")))
        if self.clock is not None:
            self._clock_close(t, "session_end:%s" % row.get("reason"))

    # -- what she achieved
    def _ev_live7_wallet(self, row, t):
        if _num(row.get("money")):
            self.money.append((t, row["money"]))
        self._seen(_date(row.get("date")), None, t)

    def _ev_bag_sitting_result(self, row, t):
        into = {"kirim": self.shipped, "jual": self.sold}.get(row.get("verb"))
        if into is None:
            return
        self.bag_requests.update(r for r in row.get("request_ids") or () if isinstance(r, str))
        for item in row.get("items") or ():
            if isinstance(item, dict) and _num(item.get("moved")) and item["moved"] > 0:
                into[str(item.get("name") or item.get("item"))] += int(item["moved"])

    def _ev_item_move_outcome(self, row, t):
        if row.get("effect") in ("shipped", "sold") and _num(row.get("moved")) and row["moved"] > 0:
            self.moves.append((row.get("request_id"), row["effect"], str(row.get("name") or row.get("item")),
                               int(row["moved"])))

    def _ev_objective_progress(self, row, t):
        if row.get("kind") == "talk_npc" and row.get("opened") is True and row.get("speaker"):
            self.talked.setdefault(str(row["speaker"]), t)

    def _ev_screen_text(self, row, t):
        if row.get("family") == "dialogue" and row.get("speaker"):
            self.speakers[str(row["speaker"])] += 1

    def _ev_live7_native_event(self, row, t):
        facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
        event = facts.get("event_id")
        if isinstance(event, str) and event.startswith("festival") and event not in self.festivals:
            self.festivals[event] = dict(at=t, date=list(_date(facts.get("date")) or ()))

    # -- menus
    def _ev_native_vitals(self, row, t):
        if "active_menu" not in row or t is None:
            return
        menu = row.get("active_menu")
        if menu != self.menu:
            self._menu_close(t, "closed" if menu is None else "next:%s" % menu)
            self.menu, self.menu_since = menu, t

    def _menu_close(self, t, ended_by):
        if self.menu is not None and self.menu_since is not None and t - self.menu_since >= MENU_HELD_S:
            self.menus_held.append(dict(menu=str(self.menu), start=self.menu_since, seconds=round(t - self.menu_since, 1),
                                        ended_by=ended_by, cycle=self.cycle, day_trial=self.day_trial))

    def _ev_live7_menu_held(self, row, t):
        self.menu_held_rows += 1

    # -- ADR 241 effect clock and ladder
    def _ev_live7_effect_clock(self, row, t):
        since, age = row.get("since_at"), row.get("age_s")
        if not _num(since) or not _num(age):
            return
        self.clock_rows += 1
        at = row.get("at") if _num(row.get("at")) else t
        if round(since, 2) in self.clock_closed:
            return  # a late tick of a stretch its session already ended: counted once
        cur = self.clock
        if cur is not None and abs(since - cur["since_at"]) > 0.01:
            fresh = row.get("reset_by") == "start"  # a new session's clock: the old one ended with its session
            self._clock_close(cur["last_at"] if fresh else since, "new_session" if fresh else str(row.get("reset_by")))
            cur = None
        if cur is None:
            cur = self.clock = dict(since_at=since, last_at=at, age=0.0, rungs=[], honoured=0.0)
        cur["last_at"], cur["age"] = at, max(cur["age"], age)
        if _num(row.get("honoured_s")):
            cur["honoured"] = max(cur["honoured"], row["honoured_s"])

    def _ev_live7_effect_ladder(self, row, t):
        kind, result = row.get("kind"), row.get("result")
        if kind == "rung" and result != "not_applicable":
            self.rungs[str(row.get("rung"))] += 1
        if self.clock is not None and kind in ("rung", "read_back"):
            self.clock["rungs"].append(dict(rung=row.get("rung"), name=row.get("name"), kind=kind, result=result))

    def _clock_close(self, end, ended_by):
        cur, self.clock = self.clock, None
        if cur is None or end is None:
            return
        self.clock_closed.add(round(cur["since_at"], 2))
        seconds = cur["age"] + max(0.0, end - cur["last_at"])
        if seconds >= STUCK_S:
            self.clock_stuck.append(dict(start=cur["since_at"], seconds=round(seconds, 1),
                                         wall_s=round(end - cur["since_at"], 1), ended_by=ended_by,
                                         rungs=cur["rungs"], honoured_s=round(cur["honoured"], 1),
                                         cycle=self.cycle, day_trial=self.day_trial, source="effect_clock"))

    def finish(self, stretches, open_end="<journal end>"):
        if self.last is not None:
            self._menu_close(self.last, open_end)
        if self.clock is not None:
            self._clock_close(self.last, open_end)
        self.replay_effects = sum(1 for s in stretches if s.get("ended_by") != "<end of journal>")
        for s in stretches:
            if s["seconds"] >= STUCK_S:
                self.replay_stuck.append(dict(start=s["start"], seconds=s["seconds"], wall_s=s["seconds"],
                                              ended_by=open_end if s["ended_by"] == "<end of journal>" else s["ended_by"],
                                              rungs=[], cycle=self.cycle, day_trial=self.day_trial, source="replay"))
        for request, effect, name, moved in self.moves:
            if request not in self.bag_requests:
                (self.shipped if effect == "shipped" else self.sold)[name] += moved
        return self


def score_attempt(path, stats, open_end="<journal end>"):
    attempt = Attempt(path)
    stretches, _ = replay(attempt.feed(read_rows(path, stats)))
    return attempt.finish(stretches, open_end)


def _hands(attempts):
    """The share of her last WINDOW skill runs that finished, after every run; the seconds the signal observed her
    hands (from a run to the next one in the same attempt, or to the attempt's last row, once the window holds
    DEAD_MIN_RUNS runs, at most OBSERVE_CAP_S per interval: a longer stretch without any run ending is unobserved,
    not live), and of those the seconds it sat below DEAD_SHARE."""
    seq = [(i, t if t is not None else a.first, ok) for i, a in enumerate(attempts) for t, ok, _, _ in a.outcomes]
    window, series, dead, observed = [], [], 0.0, 0.0
    for n, (i, t, ok) in enumerate(seq):
        window.append(1 if ok else 0)
        del window[:-WINDOW]
        share = sum(window) / len(window)
        series.append(dict(at=t, share=round(share, 3), n=len(window)))
        if t is None or len(window) < DEAD_MIN_RUNS:
            continue
        nxt = seq[n + 1] if n + 1 < len(seq) else None
        end = nxt[1] if nxt is not None and nxt[0] == i and nxt[1] is not None else attempts[i].last
        span = min(OBSERVE_CAP_S, max(0.0, (end or t) - t))
        observed += span
        if share < DEAD_SHARE:
            dead += span
    return series, dead, observed


def _buckets(series, start, end):
    if start is None or end is None:
        return []
    out = [None] * (int((end - start) // BUCKET_S) + 1)
    for point in series:
        if point["at"] is not None and start <= point["at"] <= end:
            out[int((point["at"] - start) // BUCKET_S)] = point["share"]
    return out


def _rescues(run, start, end):
    path = Path(run) / "rescues.tsv"
    if not path.is_file():
        return None, (end - start if _num(start) and _num(end) else None)  # none recorded: the whole span
    rows = []
    day = datetime.fromtimestamp(start, WIB).date() if _num(start) else None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        cols = line.split("\t")
        if not line.strip() or line.lstrip().startswith("#") or len(cols) < 2:
            continue
        at = None
        if day is not None and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", cols[1].strip()):
            parts = [int(p) for p in cols[1].strip().split(":")] + [0]
            stamp = datetime(day.year, day.month, day.day, parts[0], parts[1], parts[2], tzinfo=WIB).timestamp()
            at = stamp + 86400 if stamp < start - 3600 else stamp
        rows.append(dict(id=cols[0].strip(), time=cols[1].strip(), what=cols[2].strip() if len(cols) > 2 else "",
                         how=cols[3].strip() if len(cols) > 3 else "", at=at))
    marks = [start] + sorted(r["at"] for r in rows if _num(r["at"]) and start <= r["at"] <= end) + [end]
    longest = max((b - a for a, b in zip(marks, marks[1:])), default=0.0) if _num(start) and _num(end) else None
    return rows, longest


def _cycle_events(paths):
    seen, out = set(), Counter()
    for path in paths:
        cycle = Path(path).parents[2] / "cycle_events.jsonl"
        if cycle in seen or not cycle.is_file():
            continue
        seen.add(cycle)
        stats = Counter()
        for row in read_rows(cycle, stats):
            ev = row.get("ev")
            if isinstance(ev, str) and ev not in ROUTINE_CYCLE:
                out[ev] += 1
    return dict(out)


def _first_seen(attempts):
    """When each person, festival and place first appeared, in ACTIVE seconds since the run began (the attempts'
    own spans laid end to end, night gaps left out): the verdict counts discoveries over the same active window of
    both runs, because discovery saturates and a festival depends on the game date."""
    out = dict(people={}, festivals={}, places={})
    offset = 0.0
    for a in attempts:  # in time order
        base = a.first

        def active(t, base=base, offset=offset):
            return round(offset + max(0.0, (t if _num(t) else base or 0.0) - (base or 0.0)), 1)

        for key, seen in (("people", a.talked), ("places", a.places),
                          ("festivals", {k: v.get("at") for k, v in a.festivals.items()})):
            for name, t in seen.items():
                out[key][name] = min(out[key].get(name, float("inf")), active(t))
        if base is not None:
            offset += a.last - base
    return out


def score(run, now=None):
    """The scorecard of one run folder (a dict, JSON-ready)."""
    cpu0, wall0 = time.process_time(), time.perf_counter()
    run = Path(run)
    paths = journals(run)
    stats = Counter(bytes=0, rows=0, torn_last_line=0, bad_lines=0)
    attempts = [score_attempt(p, stats) for p in paths]
    attempts.sort(key=lambda a: (a.first is None, a.first or 0.0))
    timed = [a for a in attempts if a.first is not None]
    start = min((a.first for a in timed), default=None)
    end = max((a.last for a in timed), default=None)
    active_s = sum(a.last - a.first for a in timed)
    series, dead_s, observed_s = _hands(attempts)
    finished = sum(sum(a.finished.values()) for a in attempts)
    failed = sum(len(a.outcomes) for a in attempts) - finished  # runs that failed; refusals before a start are apart
    refused = sum((a.unstarted for a in attempts), Counter())
    started = sum(a.started for a in attempts)
    first_seen = _first_seen(attempts)
    failures = sum((a.failures for a in attempts), Counter())
    ends = Counter(e for a in attempts for e in a.ends)
    dates = []
    for a in attempts:
        dates += [d for d in a.dates if d not in dates]
    places = {}
    for a in attempts:
        for place, t in a.places.items():
            places.setdefault(place, t)
    money = [m for a in attempts for m in a.money]
    shipped = sum((a.shipped for a in attempts), Counter())
    sold = sum((a.sold for a in attempts), Counter())
    talked = {}
    for a in attempts:
        for name, t in a.talked.items():
            talked.setdefault(name, t)
    speakers = sum((a.speakers for a in attempts), Counter())
    festivals = {}
    for a in attempts:
        for event, info in a.festivals.items():
            festivals.setdefault(event, info)
    clocked = [a for a in attempts if a.clock_rows]
    stuck = sorted((s for a in attempts for s in (a.clock_stuck if a.clock_rows else a.replay_stuck)),
                   key=lambda s: s["start"])
    replayed = sorted((s for a in attempts for s in a.replay_stuck), key=lambda s: s["start"])
    menus = sorted((m for a in attempts for m in a.menus_held), key=lambda m: m["start"])
    rescues, unaided = _rescues(run, start, end) if start is not None else (None, None)
    last_finished = max((t for a in attempts for t, ok, _, _ in a.outcomes if ok and t is not None), default=None)
    now = time.time() if now is None else now
    card = dict(
        run=str(run), name=run.name, journals=len(paths), read=dict(stats),
        start=start, start_wib=wib(start), end=end, end_wib=wib(end),
        active_s=round(active_s, 1), active_minutes=round(active_s / 60, 1),
        span_minutes=round((end - start) / 60, 1) if start is not None else 0.0,
        last_row_age_s=round(now - end, 1) if end is not None else None,
        attempts=[dict(cycle=a.cycle, day_trial=a.day_trial, start_wib=wib(a.first), minutes=round(((a.last or 0) - (a.first or 0)) / 60, 1),
                       ends=a.ends, finished=sum(a.finished.values()), failed=len(a.outcomes) - sum(a.finished.values()),
                       clock_rows=a.clock_rows) for a in attempts],
        game_days=dict(count=len(dates), first=list(dates[0]) if dates else None, last=list(dates[-1]) if dates else None),
        skills=dict(finished=finished, failed=failed, started=started, open=max(0, started - finished - failed),
                    refused_before_start=sum(refused.values()), refused_reasons=refused.most_common(5),
                    share=round(finished / (finished + failed), 3) if finished + failed else None,
                    top_failures=failures.most_common(5),
                    finished_by_skill=sum((a.finished for a in attempts), Counter()).most_common()),
        hands=dict(window=WINDOW, dead_share=DEAD_SHARE, dead_minutes=round(dead_s / 60, 1),
                   observed_s=round(observed_s, 1), coverage=round(observed_s / active_s, 3) if active_s > 0 else None,
                   unobserved_minutes=round(max(0.0, active_s - observed_s) / 60, 1),
                   alive_s=round(observed_s - dead_s, 1), alive_minutes=round((observed_s - dead_s) / 60, 1),
                   share_now=series[-1]["share"] if series else None,
                   last_finished_wib=wib(last_finished),
                   minutes_since_last_finished=round((end - last_finished) / 60, 1) if last_finished and end else None,
                   every_15_min=_buckets(series, start, end), series=series),
        nights=dict(slept=ends.get(SLEPT, 0), passed_out=ends.get(PASSED_OUT, 0),
                    other={k: v for k, v in ends.items() if k not in (SLEPT, PASSED_OUT)},
                    attempts_without_end=sum(1 for a in attempts if not a.ends)),
        trips=dict(finished=sum(a.trips["finished"] for a in attempts), failed=sum(a.trips["failed"] for a in attempts)),
        rescues=rescues, longest_unaided_minutes=round(unaided / 60, 1) if _num(unaided) else None,
        cycle_events=_cycle_events(paths),
        stuck=dict(source="effect_clock" if clocked and len(clocked) == len(attempts) else ("mixed" if clocked else "replay"),
                   over_s=STUCK_S, count=len(stuck), longest_s=max((s["seconds"] for s in stuck), default=0.0),
                   total_s=round(sum(s["seconds"] for s in stuck), 1), events=stuck,
                   replay=dict(count=len(replayed), longest_s=max((s["seconds"] for s in replayed), default=0.0),
                               total_s=round(sum(s["seconds"] for s in replayed), 1),
                               effects=sum(a.replay_effects for a in attempts)),
                   rungs_taken=dict(sum((a.rungs for a in attempts), Counter()))),
        menus_held=dict(over_s=MENU_HELD_S, count=len(menus), events=menus,
                        menu_held_rows=sum(a.menu_held_rows for a in attempts)),
        achieved=dict(gold_first=money[0][1] if money else None, gold_last=money[-1][1] if money else None,
                      gold_delta=money[-1][1] - money[0][1] if money else None,
                      shipped=dict(total=sum(shipped.values()), items=shipped.most_common()),
                      sold=dict(total=sum(sold.values()), items=sold.most_common()),
                      talked_to=sorted(talked), dialogue_from=speakers.most_common(),
                      festivals=[dict(event=k, **v) for k, v in festivals.items()],
                      festival_map_days=sorted({tuple(d) for a in attempts if "Temp" in a.places for d in a.dates[:1]}),
                      places=sorted(places), first_seen_active_s=first_seen),
        provider_fallbacks=sorted((f for a in attempts for f in a.fallbacks), key=lambda f: f["at"] or 0.0),
        cannot_know=list(CANNOT_KNOW) + _missing(attempts, rescues, money),
    )
    card["cost"] = dict(cpu_s=round(time.process_time() - cpu0, 2), wall_s=round(time.perf_counter() - wall0, 2))
    return card


def _missing(attempts, rescues, money):
    out = []
    if rescues is None:
        out.append("no rescues.tsv in this run folder: hand rescues 'none recorded'")
    if not any(a.clock_rows for a in attempts):
        out.append("no live7_effect_clock rows: stuck events from the ADR 241 replay rule")
    if not money:
        out.append("no live7_wallet rows: gold unknown")
    if not attempts:
        out.append("no journal found under this folder")
    return out


# -- verdicts against a comparison run


def _word(a, b, *, more_is_better=True, rel=0.1, floor=0.0):
    if a is None or b is None:
        return "UNKNOWN"
    if abs(a - b) <= max(floor, rel * max(abs(a), abs(b))):
        return "SAME"
    return "BETTER" if (a > b) == more_is_better else "WORSE"


def _rate(n, seconds):
    """Per active hour; UNKNOWN (None) without exposure or without the number."""
    return None if n is None or not _num(seconds) or seconds <= 0 else n * 3600.0 / seconds


def _decide(measures, up, down, names):
    """No vote between measures (review R3b): `up` only when at least one measure is better beyond its margin and
    none is worse; `down` in the mirror case; SAME when none moved; MIXED when they go both ways. A measure that
    cannot be compared is listed as unknown and decides nothing; all unknown is UNKNOWN. `measures` are
    (word, text) in the order a viewer follows; the groups keep that order."""
    groups = {name: [] for name in names}  # better, worse, same, unknown (in the words of the question)
    for word, text in measures:
        groups[names[{"BETTER": 0, "WORSE": 1, "SAME": 2}.get(word, 3)]].append(text)
    better, worse, same = (groups[n] for n in names[:3])
    word = ("MIXED" if better and worse else up if better else down if worse else "SAME" if same else "UNKNOWN")
    return word, "; ".join("%s: %s" % (n, ", ".join(groups[n])) for n in names if groups[n])


def _active_s(card):
    return card.get("active_s", card["active_minutes"] * 60)


def _units(card):
    return card["achieved"]["shipped"]["total"] + card["achieved"]["sold"]["total"]


def _nights(card):
    return card["nights"]["slept"] + card["nights"]["passed_out"]


def _f(v, fmt="%.1f"):
    return "?" if v is None else fmt % v


def verdicts(a, b):
    """One line per question the session is judged by: run `a` against run `b`. Run length never decides: hands and
    rescues as shares and rates of active time, achievements as rates per active hour, discoveries over the same
    active window of both runs (the first N minutes, N the shorter run); the absolute totals stay on each line."""
    ea, eb = _active_s(a), _active_s(b)
    # Q1: the share of OBSERVED time with live hands (UNKNOWN when the signal observed under MIN_COVERAGE of the
    # active time: no skill runs ending is no evidence of live hands), then recorded rescues per active hour
    # (a run without rescues.tsv: UNKNOWN, never zero)
    def live_share(card, active):
        h = card["hands"]
        observed = h.get("observed_s", h["alive_s"])
        return h["alive_s"] / observed if active > 0 and observed > 0 and observed >= MIN_COVERAGE * active else None

    def pct(v):
        return "?" if v is None else "%.0f%%" % (v * 100)

    def coverage(card, active):
        return pct(card["hands"].get("observed_s", 0.0) / active if active > 0 else None)

    sa, sb = live_share(a, ea), live_share(b, eb)
    ra = _rate(len(a["rescues"]), ea) if a["rescues"] is not None else None
    rb = _rate(len(b["rescues"]), eb) if b["rescues"] is not None else None
    w_alive = _word(sa, sb, floor=0.05)
    w1, groups1 = _decide([(w_alive, "live hands %s/%s" % (pct(sa), pct(sb))),
                           (_word(ra, rb, more_is_better=False),
                            "rescues/h %s/%s" % (_f(ra), _f(rb)) + ("" if ra is not None and rb is not None
                                                                  else " (a run without rescues.tsv)"))],
                          "BETTER", "WORSE", ("better", "worse", "same", "unknown"))
    if w_alive == "UNKNOWN":
        w1 = "UNKNOWN"  # a rescue rate alone cannot say her hands were live

    def rescue_text(card):
        if card["rescues"] is None:
            return "none recorded"
        return "%d (longest unaided %s min)" % (len(card["rescues"]), card["longest_unaided_minutes"])

    q1 = ("Q1 hands without a hand: %s - %s (this/%s; hands observed %s of %.1f h / %s of %.1f h played; "
          "rescues %s / %s)" % (w1, groups1, b["name"], coverage(a, ea), ea / 3600, coverage(b, eb), eb / 3600,
                                rescue_text(a), rescue_text(b)))
    # Q2: seconds stuck per active hour, ADR 241's replay rule on both runs
    xa, xb = a["stuck"]["replay"], b["stuck"]["replay"]
    pa, pb = _rate(xa["total_s"], ea), _rate(xb["total_s"], eb)
    w2 = _word(pa, pb, more_is_better=False, floor=30.0)
    q2 = ("Q2 stuck: %s - %d stretches over %ds, longest %ds, %ss stuck per hour vs %d, longest %ds, %ss per hour "
          "in %s (ADR 241 replay rule on both)"
          % (w2, xa["count"], STUCK_S, xa["longest_s"], _f(pa, "%.0f"), xb["count"], xb["longest_s"], _f(pb, "%.0f"), b["name"]))
    if a["stuck"]["source"] != "replay":
        q2 += "; live clock: %d over %ds, longest %ds" % (a["stuck"]["count"], STUCK_S, a["stuck"]["longest_s"])
    # Q3: in the order a viewer follows (review R3b): gold gained and units shipped or sold, nights ended, then the
    # people, festivals and places first seen within the matched active window, and finished skills LAST (more
    # chores finished is no achievement by itself); rates per active hour; no vote between them (`_decide`)
    window = min(ea, eb)

    def seen(card, key):
        first = card["achieved"].get("first_seen_active_s", {}).get(key)
        return sum(1 for s in first.values() if s <= window) if window > 0 and first is not None else None

    totals = [("gold", a["achieved"]["gold_delta"], b["achieved"]["gold_delta"], "%+d"),
              ("shipped or sold", _units(a), _units(b), "%d"), ("nights ended", _nights(a), _nights(b), "%d"),
              ("skills finished", a["skills"]["finished"], b["skills"]["finished"], "%d")]
    gold, units, nights, skills = [(k, _rate(x, ea), _rate(y, eb), fmt) for k, x, y, fmt in totals]
    measures = [(_word(gold[1], gold[2]), "gold/h %s/%s" % (_signed(gold[1], "%+.0f"), _signed(gold[2], "%+.0f"))),
                (_word(units[1], units[2]), "shipped or sold/h %s/%s" % (_f(units[1]), _f(units[2]))),
                (_word(nights[1], nights[2]), "nights ended/h %s/%s" % (_f(nights[1]), _f(nights[2])))]
    for key in ("people", "festivals", "places"):
        x, y = seen(a, key), seen(b, key)
        measures.append((_word(x, y), "%s %s/%s" % (key, _f(x, "%d"), _f(y, "%d"))))
    measures.append((_word(skills[1], skills[2]), "skills finished/h %s/%s" % (_f(skills[1], "%.0f"), _f(skills[2], "%.0f"))))
    w3, groups3 = _decide(measures, "MORE", "LESS", ("rose", "fell", "same", "unknown"))
    q3 = ("Q3 achieved: %s - %s (this/%s; people, festivals and places within the first %.0f active min of both); "
          "totals: %s" % (w3, groups3, b["name"], window / 60,
                          ", ".join("%s %s/%s" % (k, _f(x, fmt), _f(y, fmt)) for k, x, y, fmt in totals)))
    new = sorted(set(a["achieved"]["places"]) - set(b["achieved"]["places"]))
    q3 += "; places not in %s: %s" % (b["name"], ", ".join(new) or "none")
    return [q1, q2, q3]


def _signed(v, fmt="%+d"):
    return "?" if v is None else (fmt % v)


# -- the table


def _date_text(d):
    return "Y%s %s %s" % tuple(d) if d else "?"


def table(card):
    s, h, n, st, ach = card["skills"], card["hands"], card["nights"], card["stuck"], card["achieved"]
    read = card["read"]
    lines = ["SCORECARD %s  (read-only: %d journals, %d rows, %.1f MB, torn last lines %d, bad lines %d; cpu %.2f s)"
             % (card["name"], card["journals"], read.get("rows", 0), read.get("bytes", 0) / 1e6,
                read.get("torn_last_line", 0), read.get("bad_lines", 0), card["cost"]["cpu_s"])]
    lines.append("time      %s-%s WIB, active %.0f min of %.0f min, last row %s s ago"
                 % (card["start_wib"], card["end_wib"], card["active_minutes"], card["span_minutes"], card["last_row_age_s"]))
    gd = card["game_days"]
    lines.append("days      %d game days (%s .. %s)" % (gd["count"], _date_text(gd["first"]), _date_text(gd["last"])))
    lines.append("skills    %d finished / %d failed (%s open), finished share %s; %d refused before a start"
                 % (s["finished"], s["failed"], s["open"], s["share"], s["refused_before_start"]))
    lines.append("  fails   " + (", ".join("%s %d" % (r, c) for r, c in s["top_failures"]) or "none"))
    for f in card["provider_fallbacks"]:
        lines.append("provider  %s %s -> %s (%s), %s %s" % (wib(f["at"]), f["provider"], f["to"], f["reason"],
                                                            f["cycle"], f["day_trial"][:8]))
    buckets = " ".join("--" if v is None else "%d" % round(v * 100) for v in h["every_15_min"])
    lines.append("hands     last-%d finished %% every 15 min from %s: %s" % (h["window"], card["start_wib"][:5], buckets or "-"))
    lines.append("          dead (share < %.0f%%) %.0f min, alive %.0f min, unobserved %.0f min (observed %s%% of active); "
                 "now %s, last finished skill %s (%s min before the last row)"
                 % (h["dead_share"] * 100, h["dead_minutes"], h["alive_minutes"], h["unobserved_minutes"],
                    _f(None if h["coverage"] is None else h["coverage"] * 100, "%.0f"), h["share_now"],
                    h["last_finished_wib"], h["minutes_since_last_finished"]))
    other = ", ".join("%s %d" % kv for kv in n["other"].items())
    lines.append("nights    %d slept / %d passed out%s" % (n["slept"], n["passed_out"], ("; other ends: " + other) if other else ""))
    lines.append("trips     %d cross-map trips finished / %d failed" % (card["trips"]["finished"], card["trips"]["failed"]))
    if card["rescues"] is None:
        lines.append("rescues   none recorded (no rescues.tsv)")
    else:
        lines.append("rescues   %d by hand: %s; longest unaided %s min"
                     % (len(card["rescues"]), "; ".join("%s %s %s" % (r["id"], r["time"], r["what"]) for r in card["rescues"]),
                        card["longest_unaided_minutes"]))
    if card["cycle_events"]:
        lines.append("cycle     " + ", ".join("%s %d" % kv for kv in sorted(card["cycle_events"].items())))
    rp = st["replay"]
    lines.append("stuck     %d over %ds (source %s), longest %ds, total %ds; replay rule %d, longest %ds; rungs %s"
                 % (st["count"], st["over_s"], st["source"], st["longest_s"], st["total_s"], rp["count"], rp["longest_s"],
                    st["rungs_taken"] or "none"))
    for e in sorted(st["events"], key=lambda e: -e["seconds"])[:8]:
        acted = [r for r in e["rungs"] if r["result"] != "not_applicable"]
        rungs = ", ".join("r%s %s" % (r["rung"], r["result"]) for r in acted) or "-"
        lines.append("  %s %5ds  %s %s, ended by %s; rungs %s"
                     % (wib(e["start"]), e["seconds"], e["cycle"], e["day_trial"][:8], e["ended_by"], rungs))
    mh = card["menus_held"]
    lines.append("menus     %d held over %ds%s; menu_held rows %d"
                 % (mh["count"], mh["over_s"], (": " + ", ".join("%s %ds at %s" % (m["menu"], m["seconds"], wib(m["start"]))
                                                                for m in mh["events"][:6])) if mh["events"] else "",
                    mh["menu_held_rows"]))
    gold = "?" if ach["gold_first"] is None else "%d -> %d (%s)" % (ach["gold_first"], ach["gold_last"], _signed(ach["gold_delta"]))
    ship = ach["shipped"]
    lines.append("achieved  gold %s; shipped %d%s; sold %d" % (
        gold, ship["total"], (" (" + ", ".join("%s %d" % kv for kv in ship["items"][:5]) + ")") if ship["items"] else "",
        ach["sold"]["total"]))
    lines.append("          talked to %s; dialogue from %s" % (", ".join(ach["talked_to"]) or "nobody",
                                                             ", ".join("%s %d" % kv for kv in ach["dialogue_from"][:6]) or "nobody"))
    fest = ", ".join("%s (%s)" % (f["event"], _date_text(f["date"]) if f["date"] else "?") for f in ach["festivals"])
    if not fest and ach["festival_map_days"]:
        fest = "festival map (Temp) on " + ", ".join(_date_text(d) for d in ach["festival_map_days"])
    lines.append("          festivals %s; places %d: %s" % (fest or "none", len(ach["places"]), ", ".join(ach["places"])))
    lines.append("          finished by skill: " + ", ".join("%s %d" % kv for kv in s["finished_by_skill"][:8]))
    active = card.get("active_s", 0.0)
    lines.append("per hour  gold %s, shipped or sold %s, nights ended %s, skills finished %s (of %.0f active min)"
                 % (_signed(_rate(ach["gold_delta"], active), "%+.0f"), _f(_rate(_units(card), active)),
                    _f(_rate(_nights(card), active)), _f(_rate(s["finished"], active), "%.0f"), active / 60))
    return "\n".join(lines)


def dump(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=list), encoding="utf-8")
    tmp.replace(path)
    return path
