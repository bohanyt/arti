"""Content-free co-op speech metrics. Read only the named run's public session evidence.

python -m scripts.livepath.measure_streamer --run RUN
python -m scripts.livepath.measure_streamer --log bridge.log --joined ISO_TIME --stopped ISO_TIME
Historical name waits use exact transcript matching to owner_reactions, not FIFO guesses.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from statistics import median

from arti_runtime.streamer_voice import PassiveGate, addressed, normalized

WIB = timezone(timedelta(hours=7))
STAMP = re.compile(r"^(\d\d:\d\d:\d\d\.\d+)")


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(WIB)


def rows(path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                continue


def measure(logs, joined, stopped, reactions=(), config=None):
    config = config or {}
    voiced, starts, ingress, queued, name_waits = [], [], [], {}, []
    ticks, attention_voice = [], []
    first = None
    for path in sorted(logs):
        day = joined.date()
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("[Session started "):
                stamp = datetime.strptime(line[17:36], "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB)
                day = stamp.date()
                first = stamp if first is None else min(first, stamp)
            match = STAMP.match(line)
            if not match:
                continue
            at = datetime.combine(day, datetime.strptime(match[1], "%H:%M:%S.%f").time(), WIB)
            if at > stopped:
                continue
            # Passive transcripts are recorded before the queue; keep text in memory only.
            if '[ASR Info] Pasif: "' in line:
                text = line.split('[ASR Info] Pasif: "', 1)[1].rsplit('"', 1)[0]
                ingress.append((at, normalized(text)))
            if "[Livepath] " not in line:
                continue
            row = json.loads(line.split("[Livepath] ", 1)[1])
            if joined <= at <= stopped:
                if row.get("event") == "attention_tick":
                    ticks.append(row)
                elif row.get("event") == "attention_voiced":
                    attention_voice.append(row)
            if row.get("event") == "queued" and row.get("name_addressed"):
                queued[row["turn_id"]] = at
            if row.get("event") == "voiced":
                voiced.append((at, row.get("kind")))
                if row.get("turn_id") in queued:
                    name_waits.append((at - queued[row["turn_id"]]).total_seconds())
            if row.get("event") == "start" and row.get("kind") == "mic_pasif":
                starts.append(at)
    before = [r for r in voiced if r[0] < joined]
    playing = [r for r in voiced if joined <= r[0] <= stopped]
    kinds = {}
    for _, kind in playing:
        kinds[kind] = kinds.get(kind, 0) + 1
    # Each historical reaction row corresponds to a processed passive turn.
    # A name wait is measured from its matching ASR ingress to speech completion.
    waits, replay = [], []
    gate = PassiveGate()
    admission = {(at, text): gate.reject(text, config) is None for at, text in sorted(ingress)}
    matched = 0
    for row in reactions:
        if row.get("kind") != "mic_pasif" or not row.get("utc"):
            continue
        started = utc(row["utc"])
        if started > stopped:
            continue
        text = row.get("owner_text") or ""
        # Historical reaction UTC is floored to seconds; log ingress has milliseconds.
        heard = [at for at, norm in ingress if norm == normalized(text) and at < started + timedelta(seconds=1)]
        if heard:
            matched += 1
            replay.append(admission[(max(heard), normalized(text))])
        if addressed(text, config) and row.get("arti_after_utc"):
            if heard:
                waits.append((utc(row["arti_after_utc"]) - max(heard)).total_seconds())
    waits = name_waits or waits
    allowed = sum(replay)
    duration = (stopped - joined).total_seconds()
    latencies = sorted(r["latency_ms"] for r in ticks if type(r.get("latency_ms")) in (int, float))
    deferred = {turn for r in attention_voice if r.get("kind") == "focus" for turn in r.get("deferred_turn_ids", ()) if turn}
    answered = {r.get("turn_id") for r in attention_voice if r.get("deferred")}
    return dict(
        before=dict(lines=len(before), per_10_min=round(len(before) * 600 / (joined - first).total_seconds(), 1)) if first else None,
        playing=dict(lines=len(playing), per_10_min=round(len(playing) * 600 / (stopped - joined).total_seconds(), 1),
                     kinds=kinds, own_play_share=round((kinds.get("own_play", 0) + kinds.get("moment", 0) + kinds.get("read_in_game", 0)) / len(playing), 4) if playing else 0,
                     new_own_play_lines=kinds.get("own_play", 0)),
        attention=dict(spoken_kinds_per_10_min={kind: round(count*600/duration, 2) for kind, count in kinds.items()},
                       deferral_announcements=sum(r.get("kind") == "focus" for r in attention_voice),
                       deferred_people_turns=len(deferred), deferred_answered=len(deferred & answered),
                       deferred_not_yet_answered=len(deferred - answered),
                       cut_ins=sum(r.get("kind") == "cut_in" for r in attention_voice),
                       selector_ticks=len(ticks), fallback_ticks=sum(bool(r.get("fallback")) for r in ticks),
                       selector_latency_ms=dict(median=round(median(latencies), 3) if latencies else None,
                         p90=latencies[max(0, math.ceil(.9*len(latencies))-1)] if latencies else None)),
        passive_model_turns=len(starts),
        passive_text_replay=dict(before=len(replay), after=allowed, rejected=len(replay) - allowed,
                                 transcript_lines=len(ingress), matched_turns=matched,
                                 coverage_matches_model_turns=len(replay) == len(starts)),
        name_reply_completion_wait=dict(samples=len(waits), median_seconds=round(median(waits), 3) if waits else None),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--joined")
    parser.add_argument("--stopped")
    args = parser.parse_args()
    if args.run:
        events = list(rows(args.run / "mabar.jsonl"))
        joined = utc(next(r["utc"] for r in events if r.get("event") == "joined"))
        stopped = utc(next(r["utc"] for r in events if r.get("event") == "stop_requested"))
        logs = sorted((args.run / "session_logs").glob("*_bridge.log"))
        reactions = rows(args.run / "owner_reactions.jsonl")
    else:
        parser.error("use --run, or --log with ISO --joined and --stopped") if not (args.log and args.joined and args.stopped) else None
        joined, stopped = utc(args.joined), utc(args.stopped)
        logs, reactions = [args.log], ()
    print(json.dumps(measure(logs, joined, stopped, reactions), sort_keys=True))


if __name__ == "__main__":
    main()
