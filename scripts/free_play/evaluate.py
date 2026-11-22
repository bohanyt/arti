"""Versioned, offline free-play evaluation. Never imports or starts the game runner."""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
from scripts.free_play import storage
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.free_play._agreement import classify, native_index
from scripts.free_play.journey import journey
from scripts.free_play.goal_gate import goal_gate, native_progress, semantic_counter
from scripts.free_play.experience import experience_criteria, experience_metrics, summarize
from scripts.free_play.experience_review_template import agreement as judge_agreement, bound_items, judge_counter, load_ratings, load_review, owner_ratings
from scripts.free_play.decision_visibility import decision_visibility, visibility_markdown
from scripts.free_play.invented_rule import BAR_TEXT, counts_text
from scripts.free_play.native_safety import NativeSafety, SavedCounters, legacy_vitals

EVALUATOR_VERSION = "1.7.16"
# 1.7.16 (ADR 196): report Mines floors gained, ways down, fight outcome classes, minutes per floor (no gate).
# 1.7.15 (ADR 195 review): people-page pause is INTERRUPTED route_paused_people_here, not a failed walk; report pause follow-ups.
# 1.7.14 (ADR 195): report walks completed across maps and approach re-plans (no gate).
# 1.7.11 (ADR 190): a native answered bed/morning overrides a contradictory lost-day label.
# 1.7.10 (ADR 170): active time ends at native HP/KO hard-stop evidence; retain delayed-stop totals.
# 1.7.9 (ADR 156): talk-offer counts require the talk routine's accepted scene target evidence.
# 1.7.8 (ADR 130, fix82B): retain each unclosed row's own hold; infer further open-episode time only inside HOST silence.
# 1.7.7 (ADR 142): native actual_shipped receipts; report tempo/social/variety;
# their gate criteria remain disabled unless explicitly configured with thresholds.
# 1.7.6: FIX75C (ADR 133): prints the goal-lifetime line; goal gate unchanged.
# 1.7.5: FIX75A (ADR 130; reviews REVIEW_DRIFT_2026-11-07 Q1 and REVIEW_DRIFT_ASTRA_2026-11-07 item 1). Every machine stall in
#        the whole window leaves active_minutes: HOST heartbeat gaps of HOST_STALL_GAP_S or more and the runtime's own stall
#        rows, as one union with the terminal no-progress gap, so no second is subtracted twice; machine_stalled_minutes and
#        the longest stall are reported beside it as information (owner 2026-11-07: a stall is waited out; it never voids a
#        session and never joins goal_gate). The 1.7.4 host_resource relabel is windowed and corroborated: only HOST silence
#        inside the no-progress window that stopped her, beyond what the runtime had already set aside, counts; the runtime's
#        stall_held_s alone (feed silence) never relabels, so a software fault stays owner-hard-stop. Goal gate unchanged.
# 1.7.4: a no-progress stop with a machine-wide stall in its window is stop_class host_resource, detail host_stall (ADR 128):
#        at least half of the owner's 600 s with the HOST's heartbeat silent, or set aside by the runtime's stall hold. True
#        no progress stays owner-hard-stop. A stop row's own timestamp beats the time the harness read it. Goal gate unchanged.
# 1.7.3: a gap in native coverage is closed by a later read of the same monitor,
#        and a stop's own cycle clock outranks a late timeline stamp (ADR 126).
# 1.7.2: a null terminal snapshot cannot erase an earlier positive native skill
#        or individual earnings gain. Report its dated lower bound; zero still
#        requires a field-safe tail. Goal thresholds and rules are unchanged.
# 1.7.1: count recorded disk transient holds within the gameplay window (ADR 123).
#        Hard-floor detail flows through the existing host_resource stop contract; goal_gate is unchanged.
# 1.7.0: invented_facts is no longer an absolute zero (ADR 117, owner 2026-11-06): each contradicted claim has a severity
#        (major, minor; none counts as major) and MET needs the ADR 100 reviewer and coverage, 0 major and contradicted
#        claims in under 3% of reply turns; value is counts, in goal_gate and LX-1. Earlier evaluations are not regraded;
#        the scoreboard notes that a pre-1.7.0 row's criterion was zero.
# 1.6.2: a harness stop for a resource of the machine (disk_projection, low_disk) is stop_class host_resource (ADR 109),
#        never primitive and never budget; the disk monitor's own reasons at that stop are kept as stop_detail; a
#        stop-reasons receipt without a timestamp still outranks completed.json. Active minutes and goal gate unchanged.
# 1.6.1: corrections from attempt 47's review (ADR 103): a tool first seen at the end is not a tool upgrade (journey
#        points can only fall); the scoreboard loads every schema-1 evaluation and shows absent sections as absent,
#        regrading nothing; the LX-7 close-step detector takes the affixed/inflected close verbs, -nya nouns and the
#        craft/inventory/page screens; bag slot counts (live7_harvest_offer, baseline bag fields) are citable refs and
#        the merged baseline inventory is marked as never a slot count.
# 1.6.0: the invented-facts zero is earned (ADR 100): measured claim coverage (factual_coverage >= 0.9) and a named
#        non-operator reviewer are required for invented_facts = 0, else UNKNOWN; a counted contradiction stays NOT_MET;
#        part_live_row and live7_baseline rows are citable refs; LX-7 reads the enclosing plan behind a menu-close why;
#        earlier evaluations keep their recorded version and are listed with it on the scoreboard, never regraded.
# 1.5.1: 1.5.0 + 1.4.1 merged.
# 1.5.0: report-only decision_visibility: what she saw on the page she decided from vs what was offered (ADR 91).
# 1.4.1: experience_gate reads the blind judge review and the owner page export when present (ADR 92); still report-only.
# 1.4.0: report-only experience gate (LX-1..LX-14) beside goal_gate; transcript-only metrics (ADR 75).
# 1.3.9: versioned native heartbeats keep cached-read age explicit; stale is UNKNOWN (ADR 55).
SCHEMA = "arti.free_play.evaluation/1"
# 1.3.1: the owner's ten-minute no-progress rule, frozen here (runtime NO_PROGRESS_SECONDS may change separately).
# 1.3.2: a native craft readback (finish_verified economy_craft with craft events) credits craft completion apart
# from menu cleanup; every capability also reports effect_completed and cleanup_unconfirmed separately.
# 1.3.3: native generic-menu purchases/sales/shipments share effect credit with skill callers.
# 1.3.4: native progress counters, generic storage attribution, exact-reply semantic review, and three-valued goal gate.
# 1.3.5: persistent native ingredient, paired museum and linked letter-task receipts; coverage-bound zero evidence.
# 1.3.6: gameplay-only native coverage and exhaustive per-turn semantic review v2 (ADR 40).
# 1.3.7: bounded nonmutating endpoint intervals and native cross-press purchase credit (ADR 42).
# 1.3.8: field-safe native snapshot endpoints and presentation-tolerant purchase continuity (ADR 43).
NO_PROGRESS_STOP_S = 600
# Freeze the mind-slice1 lint/why definitions here so later runtime changes cannot
# silently change an existing evaluator version. Never read prefs or soul files.
WHY_MAX_CHARS = 120
BANNED_PHRASES = ("utamakan", "prioritas", "pilih satu", "progress_rule", "next_choice")
BANNED_WORD_RE = re.compile(r"(?<![\w])(harus|sebaiknya|should|must)(?![\w])", re.I)


def steering_hits(text):
    return any(p in text.casefold() for p in BANNED_PHRASES) or bool(BANNED_WORD_RE.search(text))
ACT = ("move", "world", "slot", "menu", "transfer", "question")
CAPABILITIES = {
    "fish": ("mancing", "pancing", "tangkap_ikan", "siapkan_pancing"),
    "buy": ("beli",), "sell": ("jual",), "craft": ("buat", "craft", "masak"),
    "chest": ("simpan_ke_chest", "pasang"), "ship": ("kirim",),
}
PRIMITIVES = ("open_menu", "inspect", "press", "scroll", "type", "close", "move",
              "use_tool", "interact", "hold", "select", "transfer", "info",
              "dialog_question", "sleep_save", "next_morning")
OP_PRIMITIVE = {"approach_target": "move", "move_through": "move",
                "world_key.move_cardinal": "move", "world_interact": "interact",
                "world_key.use_tool": "use_tool", "select_item": "select",
                "hold": "hold", "transfer": "transfer", "ui_scroll": "scroll", "ui_type": "type"}


def read(path, default=None):
    return json.loads(storage.read_bytes(path).decode("utf-8-sig")) if storage.exists(path) else default


def journal_paths(run):
    return sorted(Path(run).glob("runtime/live7/*/day-*/journal/g1_journal.jsonl"))


def decode_journal_row(line):
    row = json.loads(line)
    if not isinstance(row, dict):
        raise ValueError("row is not an object")
    return row


class JournalTail:
    """Keep evaluator row numbers while a run is still appending its journals."""
    def __init__(self, run):
        self.run = Path(run)
        self.positions = {}
        self.errors = []

    def rows(self, *, final=False):
        paths = journal_paths(self.run)
        paths += sorted(self.run.glob("runtime/live7/*/cycle_events.jsonl"))
        # W1 B5: the bench's foreground readback rows (bench_focus front / denied / lost) are evidence too.
        paths.extend(path for path in (self.run / "host/host.jsonl", self.run / "native-outcomes.jsonl", self.run / "focus.jsonl")
                     if path.is_file())
        for path in paths:
            offset, number = self.positions.get(path, (0, 0))
            if path.stat().st_size < offset:
                self.errors.append(f"{path.relative_to(self.run).as_posix()}: journal truncated")
                continue
            with ipc.open_shared(path, "rb") as stream:
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        if final:
                            self.errors.append(f"{path.relative_to(self.run).as_posix()}:{number + 1}: incomplete append")
                        break
                    number += 1
                    self.positions[path] = (stream.tell(), number)
                    ref = f"{path.relative_to(self.run).as_posix()}:{number}"
                    try:
                        row = decode_journal_row(line.decode("utf-8-sig"))
                    except ValueError:
                        self.errors.append(f"{ref}: malformed row")
                        continue
                    yield row, ref


class Evidence:
    def __init__(self, run):
        self.run = Path(run)
        self.warnings = []
        self.files = {}

    def track(self, path):
        if storage.exists(path):
            self.files[self.ref(path)] = storage.digest_file(path)

    def ref(self, path):
        return Path(os.path.relpath(path, self.run)).as_posix()

    def rows(self, path):
        if not path.is_file():
            return []
        self.track(path)
        result = []
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            for n, line in enumerate(stream, 1):
                try:
                    row = decode_journal_row(line)
                    result.append((n, row))
                except ValueError:
                    self.warnings.append(f"{path.relative_to(self.run).as_posix()}:{n}: malformed row")
        return result

    def json(self, relative, default=None):
        path = self.run / relative
        self.track(path)
        return read(path, default)


def stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def decision_windows(rows):
    current = None
    for n, row in rows:
        if row.get("ev") == "trial_decision":
            if current is not None:
                yield current
            current = {"ref": f"J{n}", "rows": []}
        elif current is not None:
            current["rows"].append((n, row))
    if current is not None:
        yield current


def aggregate(per, classes):
    counts = {o: sum(per.get(c, {}).get(o, 0) for c in classes) for o in "abcdef"}
    n = sum(counts.values())
    chosen = n - counts["a"]
    return dict(n=n, **counts, disagreement_pct=round(100 * sum(counts[o] for o in "bcd") / chosen, 1) if chosen else None,
                native_only_pct=round(100 * sum(counts[o] for o in "cd") / chosen, 1) if chosen else None)


def agreement(journals, nidx):
    per = defaultdict(Counter)
    for _, rows in journals:
        for window in decision_windows(rows):
            cls, outcome, _, _, _ = classify(window, nidx)
            per[cls][outcome] += 1
    return {"acts": aggregate(per, ACT), "overall": aggregate(per, (*ACT, "skill", "observe")),
            "per_class": {c: aggregate(per, (c,)) for c in (*ACT, "skill", "observe")},
            "definition": "recovery-step1 a>c>b>d>e>f; disagreement=(b+c+d)/(n-a); includes effect uncertainty, not only admission defects"}


def action_outcome(action, effect=False):
    lc, oc, ws = action.get("lifecycle"), str(action.get("outcome") or ""), action.get("world_effect_status")
    if lc in ("not_taken", "rejected"):
        return "failed"
    if lc in ("uncertain", "interrupted") or "NOT_COMPLETED" in oc:
        return "uncertain"
    if lc == "completed" and (str(ws).startswith("observed_") or effect):
        return "verified"
    return "uncertain"


def primitives(journals, cycles, nidx=None):
    table = {p: dict(attempts=0, verified=0, failed=0, uncertain=0) for p in PRIMITIVES}
    counts = Counter()
    examples = defaultdict(lambda: defaultdict(list))
    current_ref = None

    def add(p, outcome, ref=None):
        table[p]["attempts"] += 1
        table[p][outcome] += 1
        refs = examples[p][outcome]
        if len(refs) < 3 and (ref or current_ref):
            refs.append(ref or current_ref)

    for path, rows in journals:
        pending = None
        pending_ref = None
        window = []

        def finish():
            if pending is None:
                return
            primitive = OP_PRIMITIVE.get(pending.get("operation"))
            if primitive is None:
                return
            actions = [r.get("action") or {} for r in window if r.get("ev") == "action_result"]
            effect = any(r.get("ev") in ("tool_work_effect", "extras_effect", "interaction_effect") for r in window)
            # One act step can include multiple continuation and reconciliation results.
            # Its terminal readback is used once; mirrors are not independent attempts.
            if actions:
                outcome = action_outcome(actions[-1], effect)
            elif any(r.get("ev") in ("play_decision_refused", "continuation_precheck_failed") or
                     (r.get("ev") == "skill_step_boundary" and str(r.get("reason", "")).endswith("_not_offered")) for r in window):
                outcome = "failed"
            else:
                outcome = "uncertain"
            add(primitive, outcome, pending_ref)

        for n, row in rows:
            current_ref = f"{path.parent.parent.parent.name}/{path.parent.parent.name}:J{n}"
            ev = row.get("ev")
            if ev == "skill_step":
                finish()
                pending = row if row.get("kind") == "act" else None
                pending_ref = current_ref
                window = []
            else:
                window.append(row)
            if ev == "action_result":
                action = row.get("action") or {}
                if action.get("outcome") in ("MenuOpened", "QuestionOpened", "ContainerOpened", "DialogueOpened"):
                    add("open_menu", action_outcome(action))
            if ev == "ui_inspect":
                counts["inspect_rows"] += 1
                add("inspect", "verified")
            if ev in ("ui_press", "ui_key_result") and (ev != "ui_press" or row.get("phase") == "result"):
                counts[ev] += 1
                status = "verified" if row.get("verified") else "failed" if row.get("status") in ("not_taken", "rejected") or row.get("outcome") == "UiPressNoEffect" else "uncertain"
                add("press", status)
                if ev == "ui_key_result":
                    add("dialog_question", status)
                if row.get("effect") == "closed":
                    add("close", status)
            if ev == "container_exit" and row.get("phase") == "result":
                add("close", "verified" if row.get("verified") else "uncertain")
            # Failsafe close receipts are reported separately; not self-chosen PM-1 credit.
            if ev == "store_close_rule":
                counts["failsafe_close_rows"] += 1
                counts["failsafe_close_verified"] += bool(row.get("verified"))
                add("close", "verified" if row.get("verified") else "uncertain")
            if ev == "part_live_row" and row.get("skill") == "cek_pagi":
                add("info", "verified" if row.get("verified") else "failed" if row.get("outcome_class") == "CLEAN_FAIL" else "uncertain")
        finish()
        # Recovery-step1 transfer windows include both skill transfers and UI
        # transfers. Native RESULT rows and mirrored UI receipts are not added.
        for w in decision_windows(rows):
            cls, outcome, _, _, _ = classify(w, nidx or {})
            if cls == "transfer":
                add("transfer", "verified" if outcome == "e" else "failed" if outcome in "abc" else "uncertain", f"{path.parent.parent.name}:{w['ref']}")
        counts["inspect_receivers"] += len({r.get("receiver_token") for _, r in rows if r.get("ev") == "ui_inspect" and r.get("receiver_token")})
    days = [(cycle.get("cycle"), day) for cycle in cycles for day in cycle.get("days", [])]
    for cycle_name, day in days:
        current_ref = f"{cycle_name or 'cycle'}/cycle_state.json:day-{day.get('index')}"
        rows = [(n, r) for path, rs in journals if path.parent.parent.name.startswith(f"day-{day.get('index', 0):04}-")
                and (not cycle_name or path.parent.parent.parent.name == cycle_name) for n, r in rs]
        # Reaching a sleep question is not a saved night. Checkpoint is required.
        bed = any(r.get("ev") == "action_result" and (r.get("action") or {}).get("world_effect_status") == "observed_sleep_question" for _, r in rows)
        ended = any(r.get("ev") == "skill_session_finished" and r.get("reason") in ("live7_sleep_verified", "live7:passed_out") for _, r in rows)
        if bed or ended:
            add("sleep_save", "verified" if day.get("checkpoint") else "uncertain")
        if day.get("checkpoint"):
            add("next_morning", "verified")
    return {"table": table, "verified_primitives": sum(v["verified"] > (counts["failsafe_close_verified"] if p == "close" else 0) for p, v in table.items()),
            "examples": {p: dict(v) for p, v in examples.items()},
            "instrumentation": dict(counts),
            "definition": "act steps + terminal readback (continuations counted once); UI result rows; recovery-step1 transfer windows once; strict info skill rows; night checkpoints (including pass-out). Close includes separately labeled failsafes. Native effect rows are corroboration, never added to attempts."}


def terminal_journal(journals):
    """1.3.1: the last day/run (latest timestamped row, then path order). Its terminal event is the run's stop;
    a sleep or stop that ended an earlier day is not."""
    if not journals:
        return None, []
    def key(item):
        index, (_, rows) = item
        stamps = [stamp(r.get("at")) for _, r in rows if stamp(r.get("at")) is not None]
        return (max(stamps) if stamps else float("-inf"), index)
    return max(enumerate(journals), key=key)[1]


def native_bed_night(rows):
    """ADR 190: a native answered-bed/morning receipt overrides a mistaken outer terminal label."""
    from arti_stardew_part_a import next_date
    for _, r in rows:
        if r.get("ev") not in ("part_live_row", "part_live_row_rechecked") or r.get("skill") != "tidur":
            continue
        o = (r.get("evidence") or {}).get("observed") or {}
        if (o.get("origin") == "routine_sleep_rule" and o.get("answered") is True
                and type(o.get("bed_time")) is int and o.get("start_date") and o.get("date")
                and tuple(o["date"]) == next_date(tuple(o["start_date"]))):
            return True
    return False


def knockout_events(journals, counters):
    """Completed native wake receipts, plus older positive counter/zero-frame evidence.

    Counters and runtime rows corroborate one another; do not add them together.
    Missing historical cost/floor evidence stays unknown.
    """
    events = {r['id']: r for _, rows in journals for _, r in rows
              if r.get('ev') == 'knocked_out' and r.get('id')}
    rises, previous = 0, None
    for row, _ in counters:
        value = row.get('value')
        if type(value) is int:
            if previous is not None:
                rises += max(0, value-previous)
            previous = value
    zeros = 0
    for _, rows in journals:
        held = False
        for _, row in rows:
            hp = row.get('hp') if row.get('ev') == 'native_vitals' else None
            if type(hp) is int:
                if hp <= 0 and not held:
                    zeros += 1
                held = hp <= 0
    return dict(count=max(len(events), rises, zeros), events=list(events.values()),
                basis='native wake receipts, timesUnconscious increases or observed HP-zero episodes; overlapping evidence not summed')


def endurance(evidence, journals, cycles):
    bounds = evidence.json("window.json", {})
    timeline = [r for _, r in evidence.rows(evidence.run / "timeline.jsonl")]
    starts = [r.get("at") for _, rows in journals for _, r in rows if r.get("ev") == "trial_started" and stamp(r.get("at")) is not None]
    start = stamp(bounds.get("start_at"))
    if start is None:
        evidence.warnings.append("ARMED timestamp missing; using first trial timestamp")
        start = min(starts, default=None)
    by_ref = {(str(r.get("file", "")).replace("\\", "/"), r.get("line")): stamp(r.get("at")) for r in timeline}
    stop_events = []
    stop_class, cause, receipts = None, None, 0
    last_path, last_rows = terminal_journal(journals)
    for path, rows in ((last_path, last_rows),) if last_path is not None else ():
        for n, row in rows:
            if row.get("outcome_class") == "SAFETY_STOP" or row.get("ev") == "SAFETY_STOP":
                cause = row.get("reason") or row.get("cause") or row.get("ev")
                at = stamp(row.get("at")) or by_ref.get((path.relative_to(evidence.run).as_posix(), n))
                if at is not None:
                    stop_events.append((at, str(cause), classify_stop(str(cause))))
                stop_class = classify_stop(str(cause))
    for path in sorted(evidence.run.glob("stop-reasons*.json")):
        evidence.track(path)
        data = read(path, {})
        receipts += 1
        at = stamp(data.get("utc"))
        reason = ",".join(data.get("reasons", []))
        if at is not None:
            stop_events.append((at, reason, classify_stop(reason)))
        else:
            evidence.warnings.append(f"{path.name}: no utc; end uses window.json")
        if stop_class is None:
            cause = reason
            stop_class = classify_stop(cause)
    # ADR 192: name the initiating protection, not the downstream cooperative STOP file.
    for path in sorted(evidence.run.glob("runtime/live7/*/cycle_events.jsonl")):
        for _, row in evidence.rows(path):
            if row.get("ev") == "SAFETY_STOP" and row.get("reason", "").startswith("pad_watch"):
                at = stamp(row.get("at"))
                if at is not None:
                    protection = row.get("protection")
                    if not protection and row.get("line"):
                        watch = path.parent / "pad_watch.jsonl"
                        evidence.track(watch)
                        if watch.is_file():
                            with watch.open(encoding="utf-8-sig") as stream:
                                for n, line in enumerate(stream, 1):
                                    if n == row["line"]:
                                        protection = ",".join(v["kind"] for v in json.loads(line).get("violations", ()))
                                        break
                    reason = row["reason"] + ":" + str(protection or "pad_watch")
                    stop_events.append((at, reason, "protection"))
    completed = evidence.json("completed.json", {})
    # ADR 109: a run the harness told to stop did not complete its budget, whatever completed.json says afterwards.
    if stamp(completed.get("ended_utc")) is not None and not receipts:
        stop_events.append((stamp(completed["ended_utc"]), "duration_or_day_budget_complete", "budget"))
    if stop_events:
        end, cause, stop_class = min(stop_events, key=lambda event: event[0])
        if "live7_stop_file" in cause:
            initiating = [e for e in stop_events if e[2] == "protection" and abs(e[0] - end) <= 5]
            if initiating:
                at, cause, stop_class = min(initiating, key=lambda event: event[0])
                end = min(end, at)
    else:
        end = stamp(bounds.get("end_at"))
    observed_end = end
    observed_cause = cause
    safety = NativeSafety()
    counter_rows = SavedCounters(evidence.run).poll()
    for row, ref in counter_rows:
        if '/checkpoints/' not in ref:
            evidence.track(evidence.run / ref.split(':', 1)[0])
            safety.consume(row, ref)
    for path, rows in journals:
        legacy = legacy_vitals(evidence.run, path, rows)
        if legacy:
            evidence.track(path.parent.parent / 'report.json')
        for n, row in rows:
            ref = f'{path.relative_to(evidence.run).as_posix()}:{n}'
            at = stamp(row.get('at')) or by_ref.get((path.relative_to(evidence.run).as_posix(), n))
            safety.consume(row, ref, at)
            for vitals in legacy.get(n, []):
                safety.consume(vitals, ref, at)
    for row, ref in counter_rows:
        if '/checkpoints/' in ref:
            evidence.track(evidence.run / ref.split(':', 1)[0])
            safety.consume(row, ref)
    native_stop = safety.first_stop
    due = stamp(native_stop.get('at')) if native_stop else None
    # Missing wall evidence stays unknown; never interpolate a safety timestamp.
    if due is not None and start is not None and end is not None and start <= due < end:
        end, cause, stop_class = due, native_stop['reason'], 'owner-hard-stop'
    points = sorted({stamp(r.get("at")) for r in timeline if r.get("semantic_progress") and stamp(r.get("at")) is not None and start is not None and end is not None and start <= stamp(r["at"]) <= end})
    if stop_class is None:
        ends = [r.get("reason", "") for _, r in last_rows if r.get("ev") == "skill_session_finished"]
        operator = any(r.get("ev") == "live7_stop_file" for _, r in last_rows)
        cause = ends[-1] if ends else "live7_stop_file" if operator else "unknown"
        # A STOP file without a SAFETY_STOP row or harness stop reason is an operator stop. At or beyond the
        # owner's ten-minute rule (terminal semantic gap, from ARMED when no receipt) it is that hard stop.
        quiet = end - (points[-1] if points else start) if start is not None and end is not None else None
        if "live7_stop_file" in cause and quiet is not None and quiet >= NO_PROGRESS_STOP_S:
            cause += ";operator_no_progress_600s"
        stop_class = classify_stop(cause)
        evidence.warnings.append("No timestamped stop receipt; end uses window.json")
    disk = disk_monitor_summary(evidence, start, end)
    detail = disk["stop_detail"] if stop_class == "host_resource" else None
    # 1.7.5 (ADR 130): every machine stall across the whole window, from HOST telemetry and the runtime's stall rows.
    stalls = machine_stall(evidence, journals, last_rows, start, end)
    attribution = None
    if stop_class == "owner-hard-stop" and "no_progress" in str(cause) and end is not None:
        attribution = stop_window_stall(stalls, last_rows, end, start)
        if attribution["charged_stall_s"] >= HOST_STALL_MIN_S:
            stop_class, detail = "host_resource", "host_stall:%ds" % round(attribution["charged_stall_s"])
    seconds = end - start if start is not None and end is not None and end >= start else None
    gap = max((b - a for a, b in zip([start, *points], [*points, end])), default=None) if points else None
    # Existing semantic receipt metric is not a detector of every pause. Only subtract
    # the terminal no-progress interval when that watchdog actually stopped the run.
    stall = end - points[-1] if points and "no_progress" in str(cause) else 0 if points else None
    active_intervals = None
    if seconds is not None and stall is not None:
        # The terminal gap and the machine stalls are one union: a frozen minute inside the terminal gap leaves once.
        idle = [(points[-1], end)] if stall else []
        excluded = merge_intervals(idle + stalls["intervals"])
        active = seconds - interval_seconds(excluded)
        cursor, active_intervals = start, []
        for lo, hi in excluded:
            if lo > cursor:
                active_intervals.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < end:
            active_intervals.append((cursor, end))
    else:
        active = None
    observed_active = active
    if observed_end is not None and end is not None and observed_end > end:
        raw_points = sorted(stamp(r.get('at')) for r in timeline if r.get('semantic_progress')
                            and stamp(r.get('at')) is not None and start <= stamp(r['at']) <= observed_end)
        raw_stalls = machine_stall(evidence, journals, last_rows, start, observed_end)
        raw_idle = [(raw_points[-1], observed_end)] if raw_points and 'no_progress' in str(observed_cause) else []
        observed_active = (observed_end - start - interval_seconds(merge_intervals(raw_stalls['intervals'] + raw_idle))
                           if raw_points else None)
    days = [d for c in cycles for d in c.get("days", [])]
    sleeps = sum(r.get("ev") == "skill_session_finished" and r.get("reason") == "live7_sleep_verified" for _, rs in journals for _, r in rs)
    # A bed transition on an interrupted final save still counts as a bed, not a save.
    beds = sum(r.get("ev") == "action_result" and (r.get("action") or {}).get("world_effect_status") == "observed_sleep_question" for _, rs in journals for _, r in rs)
    passes = sum(any(r.get("ev") == "skill_session_finished" and r.get("reason") == "live7:passed_out"
                     for _, r in rs) and not native_bed_night(rs) for _, rs in journals)
    hiccups = {r.get("refs", {}).get("incident_id", f"{path}:{n}"): r
               for path, rs in journals for n, r in rs
               if r.get("ev") == "FAULT" and r.get("type") == "pad_hiccup_recovered"}
    return dict(pad_hiccup_recoveries=len(hiccups),
                pad_hiccup_seconds_lost=round(sum((r.get("observed") or {}).get("seconds_lost", 0) for r in hiccups.values()), 3),
                start_utc=utc(start), stop_utc=utc(end), minutes_played=round(seconds / 60, 3) if seconds is not None else None,
                active_minutes=round(active / 60, 3) if active is not None else None,
                observed_minutes_played=round((observed_end-start)/60, 3) if observed_end is not None and start is not None else None,
                observed_active_minutes=round(observed_active/60, 3) if observed_active is not None else None,
                native_hard_stop=native_stop, knockouts=knockout_events(journals, counter_rows),
                post_hard_stop_minutes=round((observed_end-end)/60, 3) if observed_end is not None and end is not None else None,
                active_intervals=active_intervals,
                active_basis="ARMED span through native knock-outs, minus the union of the terminal semantic-progress gap (only on a no_progress stop) and every machine stall in the window (HOST heartbeat gaps of %d s or more, the runtime's stall rows); actual STOP/process-loss still ends credit; proxy, not continuous activity detection" % HOST_STALL_GAP_S,
                machine_stalled_minutes=round(stalls["seconds"] / 60, 3) if stalls["known"] else None,
                longest_machine_stall_s=round(stalls["longest"], 3) if stalls["known"] else None,
                machine_stall_sources=stalls["sources"],
                machine_stall_basis="information, never a criterion (owner 2026-11-07: a stall is waited out); HOST heartbeat gaps >= %d s in host/host.jsonl outside a night save (else host-liveness.jsonl), and the runtime's live7_host_stall rows, merged so no second counts twice" % HOST_STALL_GAP_S,
                stop_window_stall=attribution,
                game_days=len(days), nights_bed=max(beds, sleeps, sum(native_bed_night(rs) for _, rs in journals)), nights_pass_out=passes,
                saved_nights=sum(bool(d.get("checkpoint")) for d in days), stop_cause=cause,
                stop_class=stop_class, stop_detail=detail,
                disk_transient_holds=disk["transient_holds"],
                stop_basis="runtime SAFETY_STOP or harness stop-reasons receipt; native HP/KO alone is an event, not a stop; host_resource names the machine's disk (ADR 109), its detail the disk monitor's first refusing sample at or before the stop",
                longest_no_progress_gap_s=round(gap, 3) if gap is not None else None,
                gap_basis="timeline semantic effect+cleanup receipt timestamps, including leading/trailing gap; legitimate waits included")


# ADR 109: soak.py stop reasons for the machine's own resources. Checked before every other class, so a disk that
# something else filled under her never reads as her primitives, and never as a completed budget.
HOST_RESOURCE_STOPS = ("disk_projection", "low_disk")


# ADR 128: the owner's no-progress rule is for true no progress. These seconds of the HOST's heartbeat silent inside its window
# (half of it) say the machine stopped the game; the HOST's normal beat is 1 s, and a gap under HOST_STALL_GAP_S is ordinary.
HOST_STALL_MIN_S = 300
HOST_STALL_GAP_S = 10
HOST_STALL_WINDOW_S = 600


def host_stall_seconds(evidence, end):
    """Seconds of HOST heartbeat gaps of at least HOST_STALL_GAP_S in the 600 s before ``end``, 0 without the helper's log.
    Streamed: one row per second."""
    path = evidence.run / "host" / "host.jsonl"
    if not path.is_file():
        return 0
    evidence.track(path)
    start, total, last = end - HOST_STALL_WINDOW_S, 0.0, None
    for at in _heartbeat_times(path):
        if last is not None and at - last >= HOST_STALL_GAP_S:
            total += max(0.0, min(at, end) - max(last, start))
        last = at
    if last is not None and end - last >= HOST_STALL_GAP_S:
        total += max(0.0, end - max(last, start))
    return total


# The HOST helper writes these rows while the game saves and starts the next day; a heartbeat gap around them is the
# save barrier (soak.host_day_boundary), never a machine stall.
HOST_DAY_BOUNDARY_EVENTS = frozenset({"native_saved", "native_day"})


def _heartbeat_rows(path):
    """(at, None) for each native_heartbeat; (None, ev) for every other row, in file order. Streamed."""
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("ev") == "native_heartbeat":
                at = stamp(row.get("at"))
                if at is not None:
                    yield at, None
            else:
                yield None, row.get("ev")


def _heartbeat_times(path):
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if '"native_heartbeat"' not in line:
                continue
            try:
                at = stamp(json.loads(line).get("at"))
            except (ValueError, AttributeError):
                continue
            if at is not None:
                yield at


def merge_intervals(intervals):
    """Sorted, non-overlapping (a, b) with a < b; touching or overlapping spans become one."""
    merged = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if a is not None and b is not None and b > a):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def interval_seconds(intervals):
    return sum(b - a for a, b in intervals)


def clip_intervals(intervals, lo, hi):
    """The parts of ``intervals`` inside [lo, hi]; None bounds are open."""
    out = []
    for a, b in intervals:
        a2, b2 = (a if lo is None else max(a, lo)), (b if hi is None else min(b, hi))
        if b2 > a2:
            out.append((a2, b2))
    return out


def host_stall_intervals(evidence, start, end):
    """(a, b) spans with no HOST heartbeat for HOST_STALL_GAP_S or more, clipped to [start, end]: host/host.jsonl
    native_heartbeat rows (one a second), else the harness's host-liveness.jsonl gap faults (fix56A). Open at the stop:
    the last heartbeat to the stop. Streamed: one row per second. ``None`` when neither log exists."""
    path = evidence.run / "host" / "host.jsonl"
    found = []
    if path.is_file():
        evidence.track(path)
        last, boundary = None, False
        for at, row_ev in _heartbeat_rows(path):
            if at is None:
                boundary = boundary or row_ev in HOST_DAY_BOUNDARY_EVENTS
                continue
            # a50-a53: every gap of 10-27 s brackets a night save (native_saved, native_day); that is the save barrier
            # the HOST waits in (soak's day_boundary), not a frozen machine. Against her it would still be wrong information.
            if last is not None and at - last >= HOST_STALL_GAP_S and not boundary:
                found.append((last, at))
            last, boundary = at, False
        if last is not None and end is not None and end - last >= HOST_STALL_GAP_S and not boundary:
            found.append((last, end))
        return clip_intervals(merge_intervals(found), start, end)
    liveness = evidence.run / "host-liveness.jsonl"
    if not liveness.is_file():
        return None
    for _, row in evidence.rows(liveness):
        a = stamp(row.get("heartbeat_at"))
        if a is None or row.get("cause", row.get("ev")) != "host_telemetry_gap":
            continue
        if row.get("ev") == "FAULT":
            b = stamp(row.get("resumed_at")) if row.get("disposition") == "RECOVER" else end
            if b is not None and b - a >= HOST_STALL_GAP_S:
                found.append((a, b))
    return clip_intervals(merge_intervals(found), start, end)


def runtime_stall_intervals(journals, last_rows, end, host_intervals=()):
    """The runtime's own stall episodes (ADR 128 rows): each FAULT live7_host_stall opens one at its ``at``; the next
    live7_host_stall_ended closes it with ``held_s`` seconds. Without that row, retain the FAULT's own held seconds
    (held_s minus prior_held_s), including episodes superseded by another FAULT. A recovered row ends there. For an
    unrecovered episode, the terminal stall_held_s delta or an unknown duration needs HOST silence to extend it;
    healthy heartbeats and missing telemetry never imply a stall. Spans remain [at, at + held], bounded by the stop."""
    rows = [r for _, rs in journals for _, r in rs]
    terminal = next((r.get("stall_held_s") for _, r in reversed(last_rows)
                     if r.get("ev") == "live7_no_progress" and type(r.get("stall_held_s")) in (int, float)), None)
    out, open_fault = [], None

    def close_unended(fault, limit, terminal_held=None):
        at = stamp(fault.get("at"))
        own = fault.get("held_s")
        prior = fault.get("prior_held_s", 0)
        prior = float(prior) if type(prior) in (int, float) else 0.0
        if type(own) in (int, float):
            out.extend(clip_intervals([(at, at + max(0.0, float(own) - prior))], at, limit))
        if fault.get("disposition") == "RECOVER":
            return
        if terminal_held is not None:
            # Preserve ADR 130's terminal delta (legacy rows used held_s as prior), but corroborate any extension.
            prior = fault.get("prior_held_s", own)
            prior = float(prior) if type(prior) in (int, float) else 0.0
            inferred_end = at + max(0.0, float(terminal_held) - prior)
            limit = min(limit, inferred_end) if limit is not None else inferred_end
        elif type(own) in (int, float):
            return
        else:
            # No duration receipt: only the gap containing this opening can corroborate the episode.
            # A later gap after a healthy heartbeat belongs to a different stall.
            out.extend(clip_intervals([(a, b) for a, b in host_intervals or [] if a <= at < b], at, limit))
            return
        if limit is not None:
            out.extend(clip_intervals(host_intervals or [], at, limit))

    for r in rows:
        if r.get("ev") == "FAULT" and r.get("type") == "live7_host_stall" and stamp(r.get("at")) is not None:
            if open_fault is not None:
                limit = stamp(r.get("at"))
                close_unended(open_fault, min(limit, end) if end is not None else limit)
            open_fault = r
        elif r.get("ev") == "live7_host_stall_ended" and open_fault is not None:
            held = r.get("held_s")
            if type(held) in (int, float) and held > 0:
                at = stamp(open_fault.get("at"))
                out.append((at, at + float(held)))
            open_fault = None
    if open_fault is not None:
        close_unended(open_fault, end, terminal)
    return merge_intervals(out)


def machine_stall(evidence, journals, last_rows, start, end):
    """ADR 130: every machine stall inside [start, end], one union of HOST telemetry gaps and runtime stall episodes.
    ``known`` is False only when the window itself is unknown."""
    if start is None or end is None:
        return dict(intervals=[], seconds=0.0, longest=0.0, known=False, sources=[], host=[], runtime=[])
    host = host_stall_intervals(evidence, start, end)
    runtime = clip_intervals(runtime_stall_intervals(journals, last_rows, end, host), start, end)
    merged = merge_intervals((host or []) + runtime)
    sources = (["host_heartbeat"] if host else []) + (["runtime_stall_rows"] if runtime else [])
    return dict(intervals=merged, seconds=interval_seconds(merged), longest=max((b - a for a, b in merged), default=0.0),
                known=True, sources=sources, host=host or [], runtime=runtime)


def stop_window_stall(stalls, last_rows, end, start=None):
    """ADR 130: the stall the owner's no-progress rule charged to her. The stopping window is the last
    NO_PROGRESS_STOP_S seconds plus whatever the runtime set aside (stall_held_s). Only HOST silence (telemetry) counts
    as machine stall, and the part the runtime's own episodes already set aside is removed; what remains is
    ``charged_stall_s``. Feed silence without HOST silence is a software fault and charges nothing here."""
    held = next((float(r.get("stall_held_s")) for _, r in reversed(last_rows)
                 if r.get("ev") == "live7_no_progress" and type(r.get("stall_held_s")) in (int, float)), 0.0)
    lo = end - (NO_PROGRESS_STOP_S + held)
    if start is not None:
        lo = max(lo, start)
    host = clip_intervals(stalls["host"], lo, end)
    host_s = interval_seconds(host)
    if stalls["runtime"]:
        set_aside = sum(min(b - a, interval_seconds(clip_intervals(host, a, b))) for a, b in clip_intervals(stalls["runtime"], lo, end))
    else:
        set_aside = min(held, host_s)
    return dict(window_start_utc=utc(lo), window_s=round(end - lo, 3), runtime_held_s=round(held, 3),
                host_stall_s=round(host_s, 3), set_aside_s=round(set_aside, 3),
                charged_stall_s=round(max(0.0, host_s - set_aside), 3), bound_s=HOST_STALL_MIN_S,
                basis="HOST heartbeat silence inside the no-progress window that stopped her, less what the runtime set aside; >= bound_s relabels the stop host_resource")


def classify_stop(cause):
    low = cause.casefold()
    if "pad_watch" in low or "monitor_violation" in low:
        return "protection"
    if any(s in low for s in HOST_RESOURCE_STOPS):
        return "host_resource"
    if any(s in low for s in ("host_stop_or_lost_telemetry", "harness", "save barrier", "bridge_not_ready")):
        return "harness"
    if any(s in low for s in ("no_progress", "hp_critical", "health_critical", "knockout", "mutation_risk", "stuck_input", "crash")):
        return "owner-hard-stop"
    if any(s in low for s in ("budget", "hours_complete", "duration_complete", "time_budget")):
        return "budget"
    return "primitive"


def disk_monitor_summary(evidence, start, end):
    """The disk monitor's reasons (including hard_floor; storage.Monitor) from its first
    refusing sample at or before the stop: the sample soak.py stopped on. The monitor keeps sampling for up to 45 s
    after the stop request, so later samples never count. Also count hold starts in the gameplay window (ADR 123).
    None detail / zero recorded holds without the log. Streamed: one row per second."""
    result = dict(stop_detail=None, transient_holds=0)
    refused = False
    path = evidence.run / "disk-projection.jsonl"
    if not path.is_file():
        return result
    evidence.track(path)
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            at = stamp(row.get("utc"))
            if at is not None and (end is None or at <= end):
                if (start is None or at >= start) and row.get("event") == "disk_transient_hold" and row.get("transition") == "started":
                    result["transient_holds"] += 1
                if row.get("allowed") is False and not refused:
                    refused = True
                    reasons = row.get("reasons")
                    result["stop_detail"] = (",".join(str(r) for r in reasons) or None) if isinstance(reasons, list) else None
    return result


def mind(evidence, journals):
    decisions, reflections, renders, morning = [], [], [], []
    pages = lint = checked = skipped = 0
    previous = None
    active_goals = set()
    previous_day_change = None
    for path, rows in journals:
        ds = [r for _, r in rows if r.get("ev") == "skill_model_decision" and r.get("choice") != "invalid"]
        decisions.extend(ds)
        first3 = [r.get("goal_ref") for r in ds[:3]]
        refl = [r for _, r in rows if r.get("ev") in ("mind_reflection", "mind_reflection_failed")]
        # fix54H: last night's reflection is journaled by this morning's runner (deferred) and belongs to it.
        late = [r for r in refl if r.get("deferred") is True and r.get("ev") == "mind_reflection"]
        if late:
            previous = late[-1]
            active_goals = set(previous.get("new_goals", [])) | set(previous.get("kept", []))
        if previous_day_change is not None:
            morning.append(dict(day=path.parent.parent.name, previous_day_change=previous_day_change,
                                previous_day_reflection=previous is not None,
                                new_goals=previous.get("new_goals", []) if previous else [],
                                kept_goals=previous.get("kept", []) if previous else [],
                                expected_active_goals=sorted(active_goals), first3_goal_refs=first3,
                                hit=bool(active_goals & set(x for x in first3 if x)) if active_goals else None))
        reflections.extend(refl)
        good = [r for r in refl if r.get("ev") == "mind_reflection" and r.get("deferred") is not True]
        # Never carry an old reflection across a pass-out day lacking a reflection.
        previous = good[-1] if good else None
        if previous is not None:
            active_goals = set(previous.get("new_goals", [])) | set(previous.get("kept", []))
        previous_day_change = next(("pass-out" if r.get("reason") == "live7:passed_out" else "bed"
                                    for _, r in rows if r.get("ev") == "skill_session_finished" and r.get("reason") in ("live7:passed_out", "live7_sleep_verified")), None)
        if native_bed_night(rows):
            previous_day_change = "bed"
        for _, row in rows:
            if row.get("ev") == "skill_boundary_render_bytes" and type(row.get("bytes")) is int:
                renders.append(row["bytes"])
            pages += row.get("ev") == "skill_boundary_page"
        for request in storage.files(path.parent / "provider_archive", "*-request.json"):
            evidence.track(request)
            prompt = (read(request, {}).get("content") or {}).get("prompt") or ""
            i = prompt.find("\nSchema: ")
            j = prompt.find("\n\n", i)
            try:
                if i < 0 or j < 0:
                    raise ValueError("schema marker missing")
                user = json.loads(prompt[j + 2:])
                user.pop("mind", None)
                user.pop("unknown_ui", None)
                user.pop("progress", None)  # 1.2: native game text (letters, quest objectives) and numbers
            except (ValueError, AttributeError):
                skipped += 1
                continue
            checked += 1
            lint += bool(steering_hits(prompt[:i]) or steering_hits(json.dumps(user, ensure_ascii=False)))
    why = sum(isinstance(r.get("why"), str) and 0 < len(r["why"]) <= WHY_MAX_CHARS for r in decisions)
    good = [r for r in reflections if r["ev"] == "mind_reflection"]
    # FIX75C (ADR 133): goal lifetime facts from the reflection rows; the last night says what is alive and how old.
    last = next((r for r in reversed(good) if isinstance(r.get("alive"), dict)), None)
    lifetime = dict(
        goals_alive=sum(int(v) for v in last["alive"].values() if type(v) is int) if last else None,
        goals_alive_by_horizon=dict(last["alive"]) if last else None,
        oldest_goal_age_days=last.get("oldest_age_days") if last else None,
        dropped_by_her=sum(len(r.get("dropped_goals") or ()) for r in good),
        closed_with_evidence=sum(len(r["closed_with_evidence"]) if isinstance(r.get("closed_with_evidence"), list)
                                 else sum(1 for d in r.get("done") or () if isinstance(d, dict) and str(d.get("evidence") or "").strip())
                                 for r in good),
        capped_for_her=sum(len(r.get("capped") or ()) for r in good),
        refused_over_cap=sum(1 for r in good for d in r.get("dropped") or () if d.get("kind") == "new" and d.get("reason") == "cap"),
        likes_saved=sum(int((r.get("likes") or {}).get("saved") or 0) for r in good),
        basis="last mind_reflection row with `alive` (FIX75C); older runs: None, done-with-evidence from `done` rows")
    per_day = []
    for path, rows in journals:
        ds = [r for _, r in rows if r.get("ev") == "skill_model_decision" and r.get("choice") != "invalid"]
        per_day.append(dict(day=path.parent.parent.name, decisions=len(ds), with_goal_ref=sum(bool(r.get("goal_ref")) for r in ds)))
    return dict(decisions=len(decisions), why_count=why, why_ratio=round(why / len(decisions), 3) if decisions else None,
                why_src_not_model=sum(r.get("why_src") != "model" for r in decisions), reflections=len(good),
                failed_reflections=len(reflections) - len(good), dropped_claims=sum(len(r.get("dropped", [])) for r in good),
                goal_carry_over=morning, goal_refs_by_day=per_day, kept_goal_count=sum(len(r.get("kept", [])) for r in good),
                steering_prompts_checked=checked, steering_prompts_unparsed=skipped, steering_hits=lint, goal_lifetime=lifetime,
                paging=pages, legacy_paging_baseline=5, render_max=max(renders, default=None),
                render_budget=max((r.get("render_budget_bytes", 16128 * r.get("page_budget_scale", 1))
                    for _, rows in journals for _, r in rows if r.get("ev") == "skill_boundary_render_bytes"), default=16128),
                page_budgets=[dict(render_guard_bytes=g, page_limit_bytes=l, page_budget_scale=s) for g,l,s in sorted({
                    (r.get("render_guard_bytes", 13000), r.get("page_limit_bytes", 12000), r.get("page_budget_scale", 1))
                    for _, rows in journals for _, r in rows if r.get("ev") == "skill_boundary_render_bytes"})])


def discoveries_surfaced(evidence, rows, journals=()):
    """1.1: count linked persona context/turns by adapter tier, never infer semantics.

    Grounded reactions require an explicit review bound to the exact reply hash
    and shown event IDs in bridge/discovery-reaction-ratings.json. A nonempty
    reply alone proves delivery, not grounding. Historical transcripts stay zero.
    """
    tiers = {tier: dict(context=0, reaction_turns=0, reacted_events=0, grounded_reactions=0)
             for tier in ("high", "low", "flood")}
    seen, linked = {}, {}
    for row in rows:
        if row.get("ev") == "discovery_context":
            event = row.get("event") or {}
            tier, identity = event.get("importance"), event.get("id")
            if tier in tiers and identity and identity not in seen:
                seen[identity] = event
                tiers[tier]["context"] += event.get("count", 1)
        elif row.get("ev") == "turn" and row.get("source") == "discovery":
            events = row.get("discoveries") or []
            shown = set(row.get("shown_discovery_ids") or [])
            # The facts must have been ingested and actually included, not just named.
            events = [e for e in events if e.get("id") in shown and seen.get(e.get("id")) == e]
            if not events or not row.get("reply") or str(row.get("provider_source", "")).startswith("unavailable:"):
                continue
            linked[row.get("n")] = (row, events)
            for tier in {e["importance"] for e in events}:
                tiers[tier]["reaction_turns"] += 1
                tiers[tier]["reacted_events"] += sum(e.get("count", 1) for e in events if e["importance"] == tier)
    ratings = evidence.json("bridge/discovery-reaction-ratings.json", [])
    rated, reviewed, rejected, violations = set(), set(), [], []
    for rating in ratings if isinstance(ratings, list) else []:
        pair = linked.get(rating.get("turn_n"))
        if not pair or rating.get("turn_n") in rated:
            continue
        row, events = pair
        valid = (type(rating.get("grounded")) is bool and rating.get("basis")
                and rating.get("reply_sha256") == hashlib.sha256(row["reply"].encode("utf-8")).hexdigest()
                and set(rating.get("event_ids") or []) == {e["id"] for e in events})
        if not valid:
            continue
        reviewed.add(rating["turn_n"])
        # Semantics remain an explicit exact-reply review. Recognize the old
        # review vocabulary as well as the new structured rubric field.
        basis = str(rating["basis"]).lower()
        past_as_current = ("past_place_as_current" in (rating.get("violations") or [])
            or ("delayed" in basis or "past" in basis) and "current" in basis
                and any(word in basis for word in ("location", "place", "travel")))
        if past_as_current:
            violations.append(dict(turn_n=rating["turn_n"], code="past_place_as_current",
                                   basis=rating["basis"]))
        if rating["grounded"] is False or past_as_current:
            rejected.append(rating["turn_n"])
            continue
        rated.add(rating["turn_n"])
        for tier in {e["importance"] for e in events}:
            tiers[tier]["grounded_reactions"] += 1
    discovered = {r.get("id") for _, rs in journals for _, r in rs if r.get("ev") == "discovery" and r.get("id")}
    return dict(by_tier=tiers, journal_discoveries=len(discovered),
                surfaced_journal_ids=len(discovered & seen.keys()),
                reaction_turns=len(linked), grounded_reactions=len(rated),
                unreviewed_reactions=len(linked)-len(reviewed),
                rejected_reactions=len(rejected), grounding_violations=violations,
                grounding_rubric="Reject describes a past place as current; event time/place must be distinguished from current time/place.",
                check="discoveries surfaced to persona (by tier)",
                basis="context is compact silent state plus durable event evidence; grounded requires exact-reply explicit review")


def real_path(evidence, receipt=None, journals=()):
    meta = evidence.json("realpath.json", {})
    rows = [r for _, r in evidence.rows(evidence.run / "bridge/bridge-transcript.jsonl")]
    starts = [r for r in rows if r.get("ev") == "test_start"]
    persona = (starts[-1].get("persona") or {}) if starts else {}
    turns = [r for r in rows if r.get("ev") == "turn"]
    probes = [r for r in turns if r.get("source") == "owner_probe"]
    scheduled, skipped, late = 0, set(), 0
    slots_seen = set()
    for row in probes:
        p = row.get("probe") or {}
        slot, clock = p.get("slot"), p.get("game_clock")
        if type(slot) is int and type(clock) is int:
            delta = (clock // 100 * 60 + clock % 100) - (slot // 100 * 60 + slot % 100)
            scheduled += 0 <= delta <= 10
            late += delta > 10
        slots_seen.add((p.get("date"), slot))
        for slot_skipped in p.get("skipped_slots", []):
            skipped.add((p.get("date"), str(slot_skipped)))
    logfile = evidence.run / "bridge/bridge.log"
    evidence.track(logfile)
    log = ipc.read_text(logfile, encoding="utf-8-sig", errors="replace") if logfile.is_file() else ""
    calls = len(re.findall(r"\[Codex\].*?\b\d+ms \(turn \d+\)", log))
    timeouts = len(re.findall(r"\[Codex\] timeout \d+s", log))
    ratings = dict(match=None, partial=None, invented=None, unrated=len(probes), basis="unknown: semantic judgments are not recorded in the transcript")
    if receipt is not None:
        ratings = receipt_ratings(Path(receipt), len(probes))
    real = meta.get("real_path") is True and bool(starts)
    mode = "test-mode" if starts and persona.get("soul_present") is False else "full soul" if starts and persona.get("soul_present") is True else "unknown"
    return dict(path="real" if real else "harness", requested=meta.get("requested", False), persona_mode=mode,
                test_mode=bool(starts), production_stream_verified=False, probe_turns=len(probes),
                probe_on_schedule=scheduled, probe_late=late, skipped_slots=len(skipped), unique_probe_slots=len(slots_seen),
                fresh_probes=sum(bool((r.get("now") or {}).get("fresh")) for r in probes),
                probe_schedule_basis="recorded slots, <=10 game minutes late; skipped slots deduplicated per date; unrecorded final slots unknown",
                ratings=ratings, llm_completed_calls=calls if logfile.exists() else None, llm_timeouts=timeouts if logfile.exists() else None,
                llm_call_scope="bridge Codex completions/timeouts from bridge.log; planner calls and unlogged fallback attempts are excluded",
                llm_turns=len(turns), answered_probes=sum(bool(r.get("reply")) for r in probes),
                discoveries=discoveries_surfaced(evidence, rows, journals))


def receipt_ratings(path, probes):
    """Explicit import of historical human annotations; never affects ranking."""
    text = ipc.read_text(path, encoding="utf-8-sig")
    match = re.search(r"Probe/why match:\s*(\d+) MATCH,\s*(\d+) PARTIAL,\s*(\d+) invented", text)
    if match:
        a, b, c = map(int, match.groups())
    elif re.search(r"Probe/why match:\s*10/10 answered probes MATCH, 0 invented", text):
        a, b, c = 10, 0, 0
    elif re.search(r"Probe/why match:\s*1 full, 1 partial", text):
        a, b, c = 1, 1, 1  # receipt explicitly calls the partial reply's movement invented
    else:
        raise ValueError("Receipt has no recognized probe rating annotation")
    if a + b > probes:
        raise ValueError("Receipt ratings exceed observed probes")
    return dict(match=a, partial=b, invented=c, unrated=probes - a - b,
                basis="EXPLICIT legacy receipt annotation, not an automatic semantic judgment",
                source=path.name, sha256=hashlib.sha256(ipc.read_bytes(path)).hexdigest())


def generic_transaction_credit(row):
    ev = row.get("ev")
    if ev not in ("purchase_lifecycle", "native_transaction") or row.get("state") != "completed":
        return None
    cap = "buy" if ev == "purchase_lifecycle" else row.get("kind")
    qty = row.get("requested_quantity", row.get("quantity"))
    inventory_delta, money_delta = row.get("inventory_delta"), row.get("money_delta")
    requests = row.get("request_ids") if ev == "purchase_lifecycle" else [row.get("request_id")]
    if (cap not in ("buy", "sell", "ship") or not isinstance(row.get("item"), str) or not row["item"]
            or type(qty) is not int or qty <= 0 or type(inventory_delta) is not int or type(money_delta) is not int
            or not isinstance(requests, list) or not requests or not all(isinstance(r, str) and r for r in requests)
            or ev == "native_transaction" and row.get("source") != "generic_ui_native_readback"):
        return None
    currency_delta = row.get("currency_delta", money_delta)
    valid = (cap == "buy" and inventory_delta == qty and type(currency_delta) is int and currency_delta <= 0
        or cap == "sell" and inventory_delta == -qty and money_delta > 0
        or cap == "ship" and inventory_delta == -qty and money_delta == 0 and row.get("shipping_delta") == qty)
    return (cap, row.get("goal_id") or "ui:" + requests[0]) if valid else None


def capability_ledger(journals):
    from scripts.free_play.purchase_credit import cross_press_purchases
    ledger = {k: dict(offered=0, chosen=0, completed=0, failed=0, voluntarily_closed=0, effect_completed=0,
                      cleanup_unconfirmed=0) for k in CAPABILITIES}
    economy_goals = defaultdict(set)
    purchase_evidence = []
    storage = {}
    for path, rows in journals:
        for purchase in cross_press_purchases(rows):
            rid = purchase['request_ids'][0]
            goals = {r['goal_id'] for _, r in rows if r.get('ev') == 'purchase_lifecycle'
                     and rid in (r.get('request_ids') or []) and isinstance(r.get('goal_id'), str) and r['goal_id']}
            if len(goals) > 1:
                continue  # conflicting goal ownership is not another completion
            economy_goals['buy'].add((str(path.parent.parent.parent), next(iter(goals), 'ui:' + rid)))
            purchase_evidence.extend(f'{path.relative_to(path.parents[5]).as_posix()}:J{n}' for n in purchase['refs'])
        for n, row in rows:
            ev = row.get("ev")
            if (ev == "container_transfer" and row.get("phase") == "result"
                    and row.get("status") == "completed" and row.get("verified") is True
                    and row.get("reason") == "TransferObserved" and row.get("request_id")):
                # Generic native chest storage is independent of simpan_ke_chest.
                # Shipping has its own native outcome and is not storage credit.
                key = (str(path.parent.parent.parent), row["request_id"])
                storage.setdefault(key, (row.get("cleanup") is True,
                    f"{path.relative_to(path.parents[5]).as_posix()}:J{n}"))
            if (credit := generic_transaction_credit(row)) is not None:
                cap, identity = credit
                economy_goals[cap].add((str(path.parent.parent.parent), identity))
            if ev == "finish_verified":
                verdict = row.get("verdict") or {}
                objective = (verdict.get("observed") or {}).get("objective")
                observed = verdict.get("observed") or {}
                cap = {"economy_ship": "ship", "economy_sell": "sell", "economy_buy": "buy",
                       "economy_craft": "craft"}.get(objective)
                # 1.3.2: a craft is credited only from its native readback events, never from the verdict alone.
                if cap == "craft" and not any(isinstance(x, dict) and x.get("kind") == "craft"
                                              for x in observed.get("events") or []):
                    cap = None
                if cap and verdict.get("status") == "achieved":
                    economy_goals[cap].add((str(path.parent.parent.parent), row.get("goal_id")))
            for cap, skills in CAPABILITIES.items():
                if ev=="native_capability_offer" and cap in (row.get("capabilities") or []):
                    ledger[cap]["offered"] += 1
                if ev=="native_capability_choice" and cap==row.get("capability"):
                    ledger[cap]["chosen"] += 1
                if ev == "skill_offer" and set(row.get("skills") or []) & set(skills):
                    ledger[cap]["offered"] += 1
                choice = row.get("choice")
                if ev == "skill_model_decision" and isinstance(choice, list) and choice and choice[0] in skills:
                    ledger[cap]["chosen"] += 1
                if ev == "part_live_row" and row.get("skill") in skills:
                    # Same verified skill+cleanup definition as the existing soak metrics.
                    cleanup = (row.get("evidence") or {}).get("cleanup") or {}
                    if row.get("verified") and cleanup.get("input_released") is True and cleanup.get("free") is True and cleanup.get("menu") is None and row.get("skill") != "pasang":
                        ledger[cap]["completed"] += 1
                    elif "closed_by_choice" in str(row.get("reason")):
                        ledger[cap]["voluntarily_closed"] += 1
                    else:
                        ledger[cap]["failed"] += 1
                        if "cleanup" in str(row.get("reason")) or "readback" in str(row.get("reason")):
                            ledger[cap]["cleanup_unconfirmed"] += 1
    for cap in ("ship", "sell", "buy", "craft"):
        # A skill summary may be downgraded after an independently verified economy
        # effect (a9, a22 craft). Use the goal-keyed effect receipt rather than erase the effect.
        ledger[cap]["effect_completed"] = len(economy_goals[cap])
        ledger[cap]["completed"] = max(ledger[cap]["completed"], len(economy_goals[cap]))
    ledger["chest"]["effect_completed"] = len(storage)
    ledger['buy']['evidence'] = list(dict.fromkeys(purchase_evidence))
    ledger["chest"]["completed"] = max(ledger["chest"]["completed"], sum(clean for clean, _ in storage.values()))
    ledger["chest"]["cleanup_unconfirmed"] += sum(not clean for clean, _ in storage.values())
    ledger["chest"]["evidence"] = [ref for _, ref in storage.values()]
    for row in ledger.values():
        row["status"] = ("completed" if row["completed"] else "unavailable" if row["offered"] == row["chosen"] == 0 else
                         "offered not chosen" if row["chosen"] == 0 else "completed" if row["completed"] else
                         "broken" if row["failed"] else "closed by choice" if row["voluntarily_closed"] else "unverified")
    return ledger


def experience_gate(evidence, semantic, persona=None):
    """1.4.0: live experience gate, report-only (ADR 75). A sibling of goal_gate that never feeds ranking,
    tagging or goal_gate. Automatic criteria come from the bridge transcript; judge and owner criteria stay
    UNKNOWN until those scorers exist. Thresholds are the design's starting values; the owner's three choices
    are still pending, so every status is provisional."""
    rows = evidence.rows(evidence.run / "bridge/bridge-transcript.jsonl")
    metrics = experience_metrics(rows)
    review, review_ref = load_review(evidence)
    judge = judge_counter(evidence, review, review_ref or "bridge/experience-review.json")
    owner = owner_ratings(evidence, load_ratings(evidence), persona)
    calibration = judge_agreement(bound_items(evidence, review) or {}, owner["by_turn"]) if judge["bound"] and owner["bound"] else \
        dict(compared=0, agree=0, rate=None, counted=False, threshold=">= 0.8 on >= 10 compared",
             basis="needs both a bound judge review and the owner page export for this run")
    criteria = experience_criteria(metrics, semantic, persona, judge, owner, calibration)
    owner_public = {k: v for k, v in owner.items() if k != "by_turn"}
    return dict(version=EVALUATOR_VERSION, report_only=True, provisional=True, **summarize(criteria),
                criteria=criteria, metrics=metrics, judge=judge, owner=owner_public, judge_calibration=calibration,
                owner_choices_pending=[1, 2, 3], owner_choice_defaults="1a (>=7 up, <=1 down), 3a (judge counts after >=80% agreement on >=10)",
                definition="LX-1..LX-14 from runbooks/design/LIVE_EXPERIENCE_GATE_DESIGN_2026-11-03.md; three-valued like "
                           "goal_gate (any NOT_MET fails, missing evidence is UNKNOWN); automatic_status covers scorer A only; "
                           "report-only: never a ranking, tagging or goal_gate input")


def evaluate(run, *, receipt=None, override=None, semantic_review=None, metrics_config=None):
    evidence = Evidence(run)
    if (evidence.run / "WITNESS.json").exists():
        raise ValueError("Witness evidence cannot count as free play; use witness report")
    if (evidence.run / "BENCH.json").exists():
        raise ValueError("Bench evidence cannot count as free play; use its verdicts.json")
    request = evidence.json("request.json", {})
    paths = journal_paths(evidence.run)
    if not paths:
        raise ValueError(f"No free-play journals in {evidence.run.name}")
    if receipt and evidence.run.name not in ipc.read_text(Path(receipt), encoding="utf-8-sig"):
        raise ValueError("Explicit receipt does not identify this run")
    journals = [(path, evidence.rows(path)) for path in paths]
    cycles = []
    for path in sorted(evidence.run.glob("runtime/live7/*/cycle_state.json")):
        evidence.track(path)
        cycles.append(read(path, {}))
    native = evidence.run / "farmhand-SMAPI.txt"
    evidence.track(native)
    nidx = native_index(native) if native.is_file() else {}
    ntext = ipc.read_text(native, encoding="utf-8-sig", errors="replace") if native.is_file() else ""
    real = real_path(evidence, receipt, journals)
    end = endurance(evidence, journals, cycles)
    pm = primitives(journals, cycles, nidx)
    mm = mind(evidence, journals)
    mm["day_changes"] = end["saved_nights"]
    mm["reflections_per_day_change"] = round(mm["reflections"] / end["saved_nights"], 3) if end["saved_nights"] else None
    faults = Counter(str(r.get("cause")) for _, rs in journals for _, r in rs if r.get("ev") == "FAULT")
    pm["fault_rows"] = sum(faults.values())
    pm["distinct_fault_codes"] = len(faults)
    pm["rescues"] = 0 if request.get("no_operator_rescue_policy") is True else None
    pm["rescue_basis"] = "declared harness no-rescue policy; not independent operator audit"
    pm["native_corroboration"] = {outcome: len(re.findall(r"outcome=" + outcome + r"\b", ntext)) for outcome in
                                    ("TransferObserved", "TransferUncertain", "SelectionObserved", "TransitionObserved")}
    blockers = [p for p, v in pm["table"].items() if v["verified"] <= (pm["instrumentation"].get("failsafe_close_verified", 0) if p == "close" else 0)]
    dlls = {"bridge": request.get("dll_sha256"), "helper": request.get("helper_sha256")}
    for path in sorted(evidence.run.glob("created-dependency-*.dll")):
        evidence.track(path)
        dlls[path.name.removeprefix("created-dependency-")] = evidence.files[path.relative_to(evidence.run).as_posix()]
    progress = native_progress(evidence, journals, ntext)
    semantic = semantic_counter(evidence, journals, semantic_review)
    real["invented_facts"] = semantic
    # Legacy intent annotations remain separately labeled; this field now names
    # the deterministic native-counter-evidence count, never a receipt total.
    real["ratings"]["invented"] = semantic["value"]
    real["ratings"]["invented_basis"] = semantic.get('definition', 'legacy distinct contradicted reply turns; see real_path.invented_facts coverage')
    journey_result = journey(evidence.run, journals)
    from scripts.free_play.session_metrics import session_metrics
    measured = session_metrics(evidence, journals, end)
    return dict(schema=SCHEMA, evaluator_version=EVALUATOR_VERSION, run=evidence.run.name,
                identity=dict(source_sha=request.get("code_sha"), dll_shas=dlls,
                              path=real["path"], persona_mode=real["persona_mode"], evaluator_version=EVALUATOR_VERSION),
                endurance=end, pm1=pm, agreement=agreement(journals, nidx), mind=mm, real_path=real,
                journey=journey_result,
                session_metrics=measured,
                native_progress=progress, goal_gate=goal_gate(end, progress, semantic,
                    metrics=measured, metrics_config=metrics_config),
                experience_gate=experience_gate(evidence, semantic, journey_result.get("persona")),
                decision_visibility=decision_visibility(evidence, journals),
                capability_ledger=capability_ledger(journals),
                verdict=dict(scope="retained free play; text-only real path where recorded; no production streaming claim",
                             pm1="incomplete" if blockers else "blocked" if end["stop_class"] == "primitive" or pm["rescues"] is None else "pass",
                             unverified_primitives=blockers, best_so_far=None, override=override,
                             automatic="PM-1 incomplete" if blockers else "PM-1 coverage reached; inspect stop and rescue evidence"),
                inputs={"legacy_receipt": Path(receipt).name if receipt else None, "override": override,
                        "semantic_review": semantic.get("evidence", [None])[0] if semantic.get("evidence") else None,
                        "metrics_config": metrics_config},
                evidence_files=evidence.files, warnings=evidence.warnings)


def rank(evaluations):
    """Ranking cannot be overridden, and never compares real against harness."""
    for current in evaluations:
        path, minutes = _ranked(current)
        candidates = [e for e in evaluations if _ranked(e)[0] == path and _ranked(e)[1] is not None]
        maximum = max((_ranked(e)[1] for e in candidates), default=None)
        verdict = current.setdefault("verdict", {})
        verdict["best_so_far"] = maximum is not None and minutes == maximum
        verdict["ranking_basis"] = "maximum ARMED-to-stop minutes in evaluated same-path cohort; ties retained; override ignored"
        verdict["comparison_runs"] = sorted(e["run"] for e in candidates)


def _ranked(e):
    """(path, minutes) of an evaluation of any version; an older file missing either section ranks as unknown (ADR 103)."""
    identity = e.get("identity") if isinstance(e.get("identity"), dict) else {}
    endurance = e.get("endurance") if isinstance(e.get("endurance"), dict) else {}
    return identity.get("path"), endurance.get("minutes_played")


def goal_lifetime_line(lt):
    """FIX75C receipt line: goals alive at the last bedtime, the oldest one's age, dropped by her, closed with evidence."""
    alive = lt.get("goals_alive")
    if alive is None:
        return "Goals (FIX75C): alive unknown (no reflection row carries `alive`; run predates ADR 133)."
    by = lt.get("goals_alive_by_horizon") or {}
    return (f"Goals (FIX75C): alive {alive} at the last bedtime ({', '.join(f'{h} {n}' for h, n in by.items())}); "
            f"oldest {lt.get('oldest_goal_age_days')} days; dropped by her {lt.get('dropped_by_her')}; "
            f"closed with evidence {lt.get('closed_with_evidence')}; safety-capped {lt.get('capped_for_her')}; "
            f"refused over cap {lt.get('refused_over_cap')}; likes saved {lt.get('likes_saved')}.")


def markdown(e):
    from scripts.free_play.session_metrics import metrics_markdown
    identity, end, pm, mm, real = (e[k] for k in ("identity", "endurance", "pm1", "mind", "real_path"))
    lines = [f"# Evaluation: {e['run']}", "", f"Evaluator **{EVALUATOR_VERSION}**. Minutes **{end['minutes_played']}**; primitives **{pm['verified_primitives']}/16**; distinct fault codes **{pm['distinct_fault_codes']}**.",
             "", f"Goal gate: **{e['goal_gate']['status']}**. Measured criteria enabled: {e['goal_gate'].get('measured_criteria', {}).get('enabled', False)}.",
             *metrics_markdown(e.get('session_metrics')),
             "", "## Identity", "", f"Source `{identity['source_sha']}`; path **{identity['path']}**; persona **{identity['persona_mode']}**.",
             f"DLL SHA256: bridge `{identity['dll_shas']['bridge']}`; helper `{identity['dll_shas']['helper']}`.",
             "", "## Endurance (NORTH_STAR)", "", f"ARMED {end['start_utc']} Ã¢â€ â€™ stop {end['stop_utc']}. Active minutes (proxy): {end['active_minutes']}; game days {end['game_days']}; bed/pass-out {end['nights_bed']}/{end['nights_pass_out']}; saved nights {end['saved_nights']}.",
             f"Stop **{end['stop_class']}**: `{end['stop_cause']}`" + (f" ({end['stop_detail']})" if end.get('stop_detail') else "") + f". Longest semantic gap {end['longest_no_progress_gap_s']} s.",
             f"Knock-outs: {end.get('knockouts', {}).get('count', 0)}. Native floor/cost receipts: {json.dumps(end.get('knockouts', {}).get('events', []), ensure_ascii=False)}. A knock-out alone does not end or void active play.",
             f"Recovered pad hiccups: {end.get('pad_hiccup_recoveries', 0)}; seconds lost: {end.get('pad_hiccup_seconds_lost', 0)}.",
             f"Disk transient holds recorded: {end['disk_transient_holds']}. Machine stalled minutes: {end.get('machine_stalled_minutes')} (longest {end.get('longest_machine_stall_s')} s; information, not a criterion; sources {end.get('machine_stall_sources')}).",
             end['active_basis'] + ". " + end['gap_basis'] + ".",
             *([f"Observed duration before the delayed stop: {end.get('observed_minutes_played')} minutes; observed active proxy {end.get('observed_active_minutes')}. Excluded after the native hard stop: {end['post_hard_stop_minutes']} minutes. Native evidence: {end.get('native_hard_stop', {}).get('refs', [])}."] if end.get('post_hard_stop_minutes') else []),
             "", "## PM-1 primitives", "", "| Primitive | Attempts | Verified | Failed | Uncertain |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {p} | {v['attempts']} | {v['verified']} | {v['failed']} | {v['uncertain']} |" for p, v in pm['table'].items()]
    lines += ["", pm["definition"], f"Native corroboration (separate): {json.dumps(pm['native_corroboration'])}. Rescues: {pm['rescues']} ({pm['rescue_basis']}).",
              "", "## Agreement", "", e['agreement']['definition'], "", "| Class | Windows | Disagreement % |", "|---|---:|---:|"]
    lines += [f"| {c} | {v['n']} | {v['disagreement_pct']} |" for c, v in {"acts": e['agreement']['acts'], "overall": e['agreement']['overall'], **e['agreement']['per_class']}.items()]
    lines += ["", "## Mind", "", f"Why {mm['why_count']}/{mm['decisions']}; non-model why {mm['why_src_not_model']}. Reflections {mm['reflections']}/{mm['day_changes']} saved day changes (bed or pass-out); failed {mm['failed_reflections']}. Kept goals {mm['kept_goal_count']}; carry-over hits {sum(x['hit'] is True for x in mm['goal_carry_over'])}/{sum(x['hit'] is not None for x in mm['goal_carry_over'])} known-goal mornings (new OR kept; pass-out mornings included).",
              f"Steering {mm['steering_hits']} hits / {mm['steering_prompts_checked']} parsed prompts ({mm['steering_prompts_unparsed']} unparsed). Paging {mm['paging']} (legacy baseline 5); render max {mm['render_max']} B / {mm['render_budget']} B. See JSON for each morning and goal refs by day.",
              *(["Page budgets used (encoded guard / UTF-8 page limit / scale): " + json.dumps(mm["page_budgets"])]
                if mm.get("page_budgets") else []),
              goal_lifetime_line(mm.get("goal_lifetime") or {}),
              "", "## Real path", "", f"Probes on schedule {real['probe_on_schedule']}/{real['probe_turns']}; skipped {real['skipped_slots']}; late {real['probe_late']}; fresh {real['fresh_probes']}. {real['probe_schedule_basis']}.",
              f"Ratings: {json.dumps(real['ratings'], ensure_ascii=False)}.", f"Bridge LLM completed calls {real['llm_completed_calls']}; timeouts {real['llm_timeouts']}; transcript turns {real['llm_turns']}. {real['llm_call_scope']}. Text only; production streaming unverified.",
              f"Discoveries surfaced to persona (by tier): {json.dumps(real['discoveries']['by_tier'])}. Journal discoveries {real['discoveries']['journal_discoveries']}; surfaced journal IDs {real['discoveries']['surfaced_journal_ids']}. Grounded reactions: {real['discoveries']['grounded_reactions']}; rejected: {real['discoveries']['rejected_reactions']}; unreviewed: {real['discoveries']['unreviewed_reactions']}; past-place-as-current: {len(real['discoveries']['grounding_violations'])}. {real['discoveries']['basis']}.",
              "", "## Capability ledger", "", "| Capability | Offered | Chosen | Completed | Effect | Cleanup unconfirmed | Status |", "|---|---:|---:|---:|---:|---:|---|"]
    lines += [f"| {c} | {v['offered']} | {v['chosen']} | {v['completed']} | {v.get('effect_completed', 0)} | {v.get('cleanup_unconfirmed', 0)} | {v['status']} |" for c, v in e['capability_ledger'].items()]
    lines += journey_markdown(e.get("journey"))
    lines += ["", "## Goal gate", "", f"**{e['goal_gate']['status']}**. {e['goal_gate']['definition']}",
              "", "| Criterion | Status | Value | Evidence / missing native signal |", "|---|---|---:|---|"]
    for name, c in e['goal_gate']['criteria'].items():
        detail = "; ".join(c['evidence']) or c.get('missing_signal') or 'unknown'
        value = counts_text(c['value']) if name == 'invented_facts' and c['value'] is not None else c['value']
        lines.append(f"| {name} | {c['status']} | {value} | {detail} |")
    for name, c in e['goal_gate'].get('measured_criteria', {}).get('criteria', {}).items():
        lines.append(f"| {name} | {c['status']} | {c['value']} | Configured thresholds: {c['thresholds']} |")
    for name, c in e['goal_gate']['criteria'].items():
        if c.get('endpoint_fallback'):
            ep = c['endpoint_snapshot']
            if ep.get('lower_bound'):
                lines += ['', f"{name}: observed positive lower bound at `{ep['ref']}` ({utc(ep['at'])}); "
                          "later null observations do not erase this gain; the terminal value remains unobserved (ADR 124)."]
            else:
                lines += ['', f"{name}: last valid native endpoint `{ep['ref']}`; checked through gameplay stop "
                          f"{utc(ep['tail_end_at'])}, with no intervening native effect capable of changing this field (ADR 43)."]
    lines += ["", "Native measurements (separate from stats and goal claims):", "",
              "| Counter | Value | Evidence / missing native signal |", "|---|---:|---|"]
    for name, c in e['native_progress'].items():
        if isinstance(c, dict):
            detail = "; ".join(c['evidence']) or c.get('missing_signal') or c.get('definition', '')
            lines.append(f"| {name} | {c['value']} | {detail} |")
    semantic = real['invented_facts']
    lines += ["", f"Invented-facts review {semantic['review_version']}: confirmed minimum {semantic['value']}; "
              f"fully reviewed turns {semantic['reviewed_turns']}/{semantic.get('bridge_turns', semantic['reply_turns'])}; "
              f"unreviewed {semantic['unreviewed_turns']}; complete={semantic['complete']}. "
              f"Unverifiable claims (separate, never invented): {semantic.get('unverifiable_claims', 'unknown')}. "
              f"MET needs {BAR_TEXT}; contradicted claims with no severity, counted as major: {semantic.get('severity_undeclared', 'n/a')}. "
              "See JSON for exact-reply hashes and native counter-evidence."]
    lines += coverage_markdown(semantic)
    lines += experience_markdown(e.get("experience_gate"))
    lines += visibility_markdown(e.get("decision_visibility"))
    lines += ["", "## Verdict", "", f"{e['verdict']['automatic']}. Best-so-far on **{identity['path']}**: **{e['verdict']['best_so_far']}** (endurance only).", e['verdict']['scope'] + ".",
              f"Explicit override: {e['verdict']['override']!r}. Explicit receipt input: {e['inputs']['legacy_receipt']!r}."]
    if e['warnings']:
        lines += ["", "Evidence warnings:", *[f"- {x}" for x in e['warnings']]]
    return "\n".join(lines) + "\n"


def coverage_markdown(semantic):
    """1.6.0 (ADR 100): measured coverage, the named reviewer and why MET is or is not admissible."""
    coverage, reviewer = semantic.get("coverage"), semantic.get("reviewer")
    if not isinstance(coverage, dict):
        return []
    share = coverage.get("factual_coverage")
    who = (f"{reviewer['role']} ({reviewer['identity']})" if isinstance(reviewer, dict) and reviewer.get("valid")
           else "missing (role operator|independent and identity required)")
    blockers = semantic.get("zero_blockers") or []
    return ["", f"Measured coverage: factual {('%.1f%%' % (share * 100)) if share is not None else 'n/a (no factual-looking text)'} "
            f"of threshold {coverage.get('threshold')} ({'met' if coverage.get('met') else 'not met'}); "
            f"claim characters {('%.1f%%' % (coverage['claim_char_share'] * 100)) if coverage.get('claim_char_share') is not None else 'unknown'} of reply text; "
            f"reply turns with no claim {coverage.get('no_claim_turns', 'unknown')}/{coverage.get('reply_turns', 'unknown')}, "
            f"of which {coverage.get('flagged_no_claim_count', 'unknown')} mention a number or a native name. "
            f"Reviewer: {who}. MET admissible: {semantic.get('zero_admissible')}"
            + (f" (blockers: {', '.join(blockers)})" if blockers else "") + "."]


def experience_markdown(g):
    """1.4.0: report-only experience gate (ADR 75); never a verdict or ranking input."""
    if not g:
        return []
    lines = ["", "## Experience gate (provisional, report-only)", "",
             f"Provisional **{g['status']}**; automatic criteria **{g['automatic_status']}** "
             f"({g['automatic_counts']['MET']} MET / {g['automatic_counts']['NOT_MET']} NOT_MET / {g['automatic_counts']['UNKNOWN']} UNKNOWN). "
             f"Owner choices pending: {g['owner_choices_pending']}. {g['definition']}.",
             "", "| ID | Criterion | Scorer / tier | Status | Value | Threshold | Evidence / missing signal |", "|---|---|---|---|---|---|---|"]
    for c in g['criteria'].values():
        detail = "; ".join(c['evidence']) or c.get('missing_signal') or 'unknown'
        lines.append(f"| {c['id']} | {c['criterion']} | {c['scorer']} / {c['tier']} | {c['status']} | "
                     f"{json.dumps(c['value'], ensure_ascii=False)} | {json.dumps(c['threshold'], ensure_ascii=False)} | {detail} |")
    m, p = g['metrics'], g['metrics']['population']
    lines += ["", f"Transcript population: {p['turns']} turns, {p['reply_turns']} replies ({p['spontaneous_replies']} spontaneous, "
              f"{p['probe_replies']} probe), {p['silent_turns']} silent; span {p['span_s']} s ({p['span_basis']}). "
              f"Reply latency p50/p95/max ms: {m['reply_latency']['p50_ms']}/{m['reply_latency']['p95_ms']}/{m['reply_latency']['max_ms']}. "
              f"Silence gaps over 15 min: {m['silence_gaps']['count_over_15min']}. Emotions: {json.dumps(m['mood_range']['counts'], ensure_ascii=False)}. "
              "Metric definitions are in each metric's basis in EVALUATION.json."]
    j, o, cal = g.get('judge') or {}, g.get('owner') or {}, g.get('judge_calibration') or {}
    lines += ["", f"Judge review: {'bound' if j.get('bound') else 'none'}; labelled {j.get('reviewed_turns', 0)}/{j.get('reply_turns', 0)} replies; "
              f"flagged {len(j.get('flagged') or [])}. Owner page: {o.get('rated_systematic', 0)}/{o.get('systematic_samples', 0)} systematic rated "
              f"(👍 {o.get('up', 0)} / ✏️ {o.get('edit', 0)} / 👎 {o.get('down', 0)}), judge-flagged extras rated {o.get('flagged_rated', 0)}. "
              f"Judge-vs-owner agreement {cal.get('rate')} on {cal.get('compared', 0)} compared; judge counts: {cal.get('counted')} ({cal.get('threshold')})."]
    return lines


def _v(field):
    return "unknown" if field is None or field.get("value") is None else f"{json.dumps(field['value'], ensure_ascii=False)} ({field['source']})"


def journey_markdown(j):
    """1.2.0: the game's own yardsticks, separate from endurance; never a pass/fail or a ranking input."""
    if not j:
        return []
    p, u, f, per = j["progress"], j["understanding"], j["finishing"], j["persona"]
    return ["", "## Journey (game's own yardsticks)", "",
            f"Journey points **{j['journey_points']}**{' (partial: ' + ', '.join(j['partial_missing']) + ')' if j['partial'] else ''}. {j['definition']}",
            "", "| Progress | Value (source) |", "|---|---|",
            *[f"| {k} | {_v(p[k])} |" for k in ("money_earned", "money_earned_individual", "crops_harvested", "crops_shipped",
                                               "new_places", "tool_upgrades", "skill_levels", "bundles_completed",
                                               "quests_completed", "friendship")],
            "", p["basis"] + ".",
            "", f"Understanding: goals done with evidence {u['goals_done_with_evidence']}/{u['goals_set']} set ({u['goals_done']} done claims); "
                f"decisions {u['decisions']}; loop proxy {u['loop_proxy']} ({u['loop_proxy_pct']}%); progress choices {u['progress_choices']} "
                f"({u['progress_choice_pct']}%); care {u['care_choices']}; activity kinds {u['activity_kinds']}; repeated failures "
                f"{u['repeated_failures']}/{u['failures']} ({u['repeat_failure_pct']}%). {u['definition']}",
            "", f"Distance to finishing ({f['source']}): CC bundles {f['bundles_done']}/{f['bundles_total']}, rooms {f['rooms_done']}/{f['rooms_total']}; "
                f"Grandpa {f['grandpa_points']}/{f['grandpa_max']} points, {f['candles']} candle(s) (start {f['grandpa_start']}); distance {f['distance']}. "
                f"Unknown criteria: {f['grandpa_unknown']}. {f['distance_basis']}.",
            "", f"Persona samples: {len(per['samples'])} of {per['candidates']} candidate turns; owner-rated {per['rated']} "
                f"(up {per['up']} / down {per['down']}). Owner rates in PERSONA_SAMPLES.md; viewer names scrubbed."]


def write_evaluation(run, result):
    run = Path(run)
    (run / "EVALUATION.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run / "EVALUATION.md").write_text(markdown(result), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--legacy-receipt", type=Path, help="explicit human probe annotations; one run only; recorded, never changes ranking")
    parser.add_argument("--override-verdict", help="explicit annotation printed alongside automatic verdict; never changes ranking")
    parser.add_argument("--semantic-review", type=Path, help="machine-readable exact-reply semantic review; one run only; hashed input, not a verdict override")
    parser.add_argument("--scoreboard", type=Path, default=ROOT / "runbooks/SCOREBOARD_AUTO.md")
    parser.add_argument("--metrics-config", type=Path, help="JSON {enabled: false, thresholds: {tempo.seconds_per_decision: {max: ...}}}; owner targets only")
    args = parser.parse_args(argv)
    if args.legacy_receipt and len(args.runs) != 1:
        parser.error("--legacy-receipt requires exactly one run")
    if args.semantic_review and len(args.runs) != 1:
        parser.error("--semantic-review requires exactly one run")
    metrics_config = read(args.metrics_config) if args.metrics_config else None
    results = [evaluate(run, receipt=args.legacy_receipt, override=args.override_verdict,
                        semantic_review=args.semantic_review, metrics_config=metrics_config) for run in args.runs]
    if args.metrics_config:
        for result in results:
            result['evidence_files'][str(args.metrics_config.resolve())] = storage.digest_file(args.metrics_config)
    peers = {}
    for run in args.runs:
        for path in run.parent.glob("*/EVALUATION.json"):
            old = read(path)
            if old.get("schema") == SCHEMA and old.get("evaluator_version") == EVALUATOR_VERSION:
                peers[old["run"]] = (path.parent, old)
    peers.update({result['run']: (run, result) for run, result in zip(args.runs, results)})
    rank([value for _, value in peers.values()])
    for run, result in peers.values():
        write_evaluation(run, result)
    from scripts.free_play.scoreboard import write_persona_samples, write_scoreboard
    write_scoreboard([r for _, r in peers.values()], args.scoreboard)
    write_persona_samples([r for _, r in peers.values()], Path(args.scoreboard).parent / "PERSONA_SAMPLES.md")
    for result in results:
        print(f"{result['run']}: {result['endurance']['minutes_played']:.1f} min; {result['identity']['path']}; best={result['verdict']['best_so_far']}")


if __name__ == "__main__":
    main()
