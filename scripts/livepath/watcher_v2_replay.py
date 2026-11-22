"""Five-clip acceptance replay. Real calls are opt-in by omitting --dry-run.

Example: python -B scripts/livepath/watcher_v2_replay.py --dry-run --clips
'[["aaaaaaaaaaa",5,0],["bbbbbbbbbbb",5,1],["ccccccccccc",5,2],
  ["ddddddddddd",5,3],["eeeeeeeeeee",5,4]]' --cap-usd 0.05

Only status, counts and timing are written. Never run this against the network
as an implementation check; the operator owns real acceptance.
--measure-all records both sources to their own deadlines without cancelling
the loser; winner still identifies the first complete source.
Clip JSONL includes spend_usd; stdout ends with a run spend summary. The USD
ledger is retained in system temp separately from disposable clip media.
"""
from __future__ import annotations

import argparse
import contextlib
import heapq
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime.watcher_v2 import WatcherV2Session
from arti_video_watcher import VideoJob


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeScheduler:
    """Deterministic arrival/stage ordering, with no sockets or wall-clock sleeps."""
    def __init__(self, clock):
        self.clock, self.pending, self.sequence = clock, [], 0

    def call_at(self, when, target):
        self.sequence += 1
        heapq.heappush(self.pending, (when, self.sequence, target))

    def launch(self, target):
        self.call_at(self.clock() + 0.1, target)

    def until(self, when):
        while self.pending and self.pending[0][0] <= when:
            at, _, target = heapq.heappop(self.pending)
            self.clock.now = at
            target()
        self.clock.now = when


def _fakes():
    return dict(meta=lambda r, c: dict(title="Synthetic clip", duration=r.job.clip_seconds),
                captions=lambda r, c: "Synthetic caption",
                audio=lambda r, c: "Synthetic speech", frames=lambda r, c: [b"fake"] * 10,
                vision=lambda r, c: "Synthetic visible action", gemini=lambda r, c: "Synthetic digest")


def _report(row):
    row = dict(row)
    row.pop("event", None)
    elapsed = row.pop("stage_elapsed_seconds", None)
    row["stage_seconds"] = {name.removesuffix("_ms"): (round(value / 1000, 6)
                            if isinstance(value, (int, float)) else value)
                            for name, value in list(row.items()) if name.endswith("_ms")}
    for name in list(row):
        if name.endswith("_ms"):
            del row[name]
    if elapsed is not None:
        row["stage_seconds"].update(elapsed)
    row["spend_usd"] = row["vision_usd"]
    return row


def _spend_snapshot(session):
    """Read the existing cap ledger while it is still available, never infer spend from winners."""
    path = Path(session.vision_config["voice_api_session_state_file"])
    if not path.exists():
        return dict(spend_usd=0.0, reserved_usd=0.0, uncertain_usd=0.0)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        values = {key: float(state[key]) for key in ("spent_usd", "reserved_usd", "uncertain_usd")}
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            raise ValueError("invalid_spend")
        return dict(spend_usd=values["spent_usd"], reserved_usd=values["reserved_usd"], uncertain_usd=values["uncertain_usd"])
    except (OSError, ValueError, KeyError, TypeError):
        return dict(spend_usd=None, reserved_usd=None, uncertain_usd=None)


def replay(clips, *, dry_run=False, cap_usd=0.05, measure_all=False, summary=None):
    clock = FakeClock() if dry_run else time.monotonic
    wall = clock if dry_run else time.time
    scheduler = FakeScheduler(clock) if dry_run else None
    rows, active, finished = [], [], []
    with tempfile.TemporaryDirectory(prefix="arti-watcher-replay-") as folder:
        # A cancelled transport can settle after media cleanup. Its small USD
        # ledger therefore lives independently of disposable media directories.
        accounting = Path(tempfile.mkdtemp(prefix="arti-watcher-replay-accounting-")) / "vision-usd.json"
        config = dict(video_watcher_v2=True, video_gemini_enabled=True,
                      video_vision_cap_usd=cap_usd, video_media_dir=folder,
                      video_vision_session_state_file=str(accounting))
        if not dry_run:
            from dotenv import dotenv_values
            from arti_runtime.openai_decisions import DEFAULT_ENV_FILE
            # Read in-process only; key values never enter output or command lines.
            env = dotenv_values(DEFAULT_ENV_FILE)
            config["gemini_api_key"] = env.get("ARTI_VIDEO_GEMINI_API_KEY") or env.get("GEMINI_API_KEY")
            from arti_bridge import CONFIG, transcribe_audio
            CONFIG["groq_api_key"] = env.get("GROQ_API_KEY") or CONFIG.get("groq_api_key")
            asr = transcribe_audio
        else:
            asr = None
        def emit(row):
            if row.get("event") == "watcher_v2_clip":
                rows.append(_report(row))
        options = dict(clock=clock, wall_clock=wall, emit=emit, asr=asr, measure_all=measure_all)
        if dry_run:
            options.update(fetchers=_fakes(), launch=scheduler.launch, cleanup=lambda target: target())
        session = WatcherV2Session(config, **options)
        began = clock()
        pending = iter(sorted(clips, key=lambda item: item[2]))
        next_clip = next(pending, None)
        try:
            while next_clip is not None or active:
                elapsed = clock() - began
                while next_clip is not None and next_clip[2] <= elapsed + 1e-8:
                    video_id, played, _ = next_clip
                    job = VideoJob(video_id, "saweria", "Replay donor", "Rp1", clip_end=played,
                                   enqueued_at=wall(), playback_end=wall() + played)
                    active.append(session.start(job))
                    next_clip = next(pending, None)
                for race in list(active):
                    race.tick()
                    if race.built is None and clock() >= race.end and (
                            race.complete.is_set() or clock() >= race.end + race.wait):
                        race.build()
                    if race.logged:
                        race.close()
                        finished.append(race)
                        active.remove(race)
                if dry_run:
                    scheduler.until(clock() + 0.025)
                else:
                    time.sleep(0.025)
        finally:
            for race in active:
                race.close()
        # Refresh any costs that settled after a loser's evidence was discarded.
        rows[:] = [_report(race.row()) for race in finished]
        if summary is not None:
            summary.update(event="watcher_v2_replay_summary", clips=len(rows),
                           vision_pending=sum(race.vision_pending for race in finished),
                           **_spend_snapshot(session))
    return rows


def parse_clips(value):
    try:
        rows = json.loads(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("clips must be valid JSON") from None
    if not isinstance(rows, list) or not rows:
        raise argparse.ArgumentTypeError("--clips requires a nonempty JSON list")
    for item in rows:
        if (not isinstance(item, list) or len(item) != 3 or
                not isinstance(item[0], str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", item[0]) or
                any(type(n) not in (int, float) or not math.isfinite(n) for n in item[1:]) or
                item[1] <= 0 or item[2] < 0):
            raise argparse.ArgumentTypeError("each clip must be [video_id, positive played_seconds, nonnegative arrival_offset]")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--clips", type=parse_clips)
    inputs.add_argument("--clips-file", type=Path, help="UTF-8 JSON list; avoids shell quoting")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--measure-all", action="store_true", help="Measure both sources without cancelling the loser")
    parser.add_argument("--cap-usd", default=0.05, type=float)
    parser.add_argument("--out", type=Path, help="Optional JSONL timing report")
    args = parser.parse_args(argv)
    if args.clips_file:
        try:
            args.clips = parse_clips(args.clips_file.read_text(encoding="utf-8-sig"))
        except (OSError, argparse.ArgumentTypeError) as exc:
            parser.error("invalid --clips-file: " + type(exc).__name__)
    if not math.isfinite(args.cap_usd) or args.cap_usd < 0:
        parser.error("--cap-usd must be finite and nonnegative")
    # Existing ASR/Gemini diagnostics are suppressed; no provider text escapes.
    summary = {}
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        rows = replay(args.clips, dry_run=args.dry_run, cap_usd=args.cap_usd, measure_all=args.measure_all, summary=summary)
    lines = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(lines, encoding="utf-8")
    print(lines + json.dumps(summary, sort_keys=True) + "\n", end="")
    return 0 if len(rows) == len(args.clips) else 1


if __name__ == "__main__":
    raise SystemExit(main())
