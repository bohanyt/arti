"""Read-only Live7 journal webhook watcher; Python stdlib, no game/control imports.

python live_watcher.py --cycle-dir RUN/live7/c1 --idle-minutes 5
ARTI_LIVE7_WEBHOOK_URL or a private --config JSON {"webhook_url": "..."}.
The notification contains event codes and file/line references, never raw journal text.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import argparse
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


class ObservedPauseClock:
    """Gameplay time from fresh existing journal facts; state is JSON-resumable.

    Only no-progress / game wait timers use this. Safety scans keep real time.
    """
    def __init__(self, clock, state):
        self.clock, self.state = clock, state

    def _advance(self, at):
        sample = self.state.get("sample")
        if sample is None:
            return
        until = min(at, sample + 3)
        accounted = self.state.get("accounted", sample)
        if self.state.get("paused"):
            self.state["total"] = self.state.get("total", 0.) + max(0., until - accounted)
        self.state["accounted"] = max(accounted, until)

    def note(self, event):
        ev, at = event.get("ev"), event.get("at")
        if (ev not in {"game_paused", "game_resumed", "game_pause_unavailable"}
                or type(at) not in (float, int) or not math.isfinite(at) or not 0 <= self.clock() - at
                or at < self.state.get("sample", at)):
            return
        self._advance(at)
        self.state.update(sample=at, paused=ev == "game_paused",
                          accounted=max(at, self.state.get("accounted", at)))

    def paused(self):
        return bool(self.state.get("paused") and 0 <= self.clock() - self.state["sample"] <= 3)

    def now(self):
        now = self.clock()
        self._advance(now)
        return now - self.state.get("total", 0.)


def read_json(path, default=None):
    try:
        return json.loads(ipc.read_text(Path(path), encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, value, *, attempts=20, delay_s=0.05):
    ipc.write_json(path, value)


class Tail:
    """Close handles between polls (Windows journal preservation); buffer partial lines."""
    def __init__(self, positions=None):
        self.positions = positions if positions is not None else {}

    def read(self, path):
        path = Path(path)
        key = str(path)
        try:
            stat = path.stat()
            pos = self.positions.setdefault(key, {})
            identity = [stat.st_dev, stat.st_ino]
            if pos.get("identity") != identity or stat.st_size < pos.get("offset", 0):
                pos.clear()
                pos.update(identity=identity, offset=0, line=0, partial="")
            with ipc.open_shared(path, "rb") as f:
                f.seek(pos["offset"])
                data = f.read(256 * 1024)  # bounded memory on the lab host
                pos["offset"] = f.tell()
            data = bytes.fromhex(pos["partial"]) + data
            lines = data.split(b"\n")
            pos["partial"] = lines.pop().hex()
            # A corrupt giant line must not grow memory without limit.
            if len(pos["partial"]) > 2 * 1024 * 1024:
                pos["partial"] = ""
            result = []
            for line in lines:
                pos["line"] += 1
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    result.append((pos["line"], value))
            return result
        except (OSError, KeyError):
            return []


def safety(event):
    if (event.get("ev") == "FAULT" and event.get("disposition") == "RECOVER"
            and event.get("outcome_class") == "UNCERTAIN"):
        return False  # cause/refs may quote the original stop-shaped diagnostic
    return (event.get("ev") in {"live7_monitor_violation", "pause_identity_mismatch"} or
            any("safety_stop" in str(event.get(k, "")).lower()
                for k in ("ev", "reason", "status", "outcome_class", "result", "verdict")))


HOST_WAIT_REMINDER_S = 60.0


def update_host_wait(state, event, *, source, line):
    """One pending bed wait across the runtime journal and its supervisor mirror."""
    ev = event.get("ev")
    if safety(event):
        state.clear()
    elif (ev in {"bed_waiting_for_host", "host_wait"}
          or ev == "day_start_waiting" and event.get("host_wait") is True
          or "bed_waiting_for_host" in str(event.get("reason", ""))):
        goal = event.get("goal_id")
        if goal and state.get("goal_id") and goal != state["goal_id"]:
            state.clear()
        state.update(source=str(source), line=line)
        for key in ("phase", "message"):
            if event.get(key):
                state[key] = event[key]
        if goal:
            state["goal_id"] = goal
    elif (ev in {"day_start_cleared", "day_start_blocked", "bed_wait_ended", "host_wait_ended", "day_ended", "trial_crash", "crash",
                 "farmhand_crash", "host_crash", "session_dropped", "live7_stop_file",
                 "skill_session_finished", "day_trial_started", "cycle_stopped"}
          or ev in {"part_live_row", "skill_finished"} and event.get("skill") == "tidur"):
        state.clear()


def host_wait_alert(state, now):
    if state and ("last_alert" not in state or now - state["last_alert"] >= HOST_WAIT_REMINDER_S):
        state["last_alert"] = now
        return dict(event="host_wait", source=state["source"], line=state["line"], at=now,
                    **{k:state[k] for k in ("phase", "message") if k in state})
    return None


def journal_paths(cycle_dir):
    state = read_json(Path(cycle_dir) / "cycle_state.json", {})
    paths = [Path(cycle_dir) / "cycle_events.jsonl"]
    for day in state.get("days", []):
        paths.extend(Path(a["journal"]) / "g1_journal.jsonl" for a in day.get("attempts", []))
    return paths


class Watcher:
    def __init__(self, *, url=None, idle_minutes=5, loop_repeats=2, state=None, clock=time.time):
        self.url, self.idle_seconds, self.loop_repeats = url, idle_minutes * 60, loop_repeats
        self.clock = clock
        self.state = state if state is not None else {}
        self.game_clock = ObservedPauseClock(clock, self.state.setdefault("game_pause_clock", {}))
        self.state.setdefault("last_progress", self.game_clock.now())
        self.state.setdefault("loops", {})
        self.state.setdefault("host_wait", {})
        self.tail = Tail(self.state.setdefault("positions", {}))

    def scan(self, paths, *, active=True):
        alerts = []
        for path in paths:
            for line, event in self.tail.read(path):
                self.game_clock.note(event)
                update_host_wait(self.state["host_wait"], event, source=path, line=line)
                ev = str(event.get("ev", ""))
                code = None
                if ev in {"live7_stop_file", "live7_stop_file_created"}:
                    code = "stop_file"
                elif safety(event):
                    code = "SAFETY_STOP"
                elif ev == "live7_journal_capacity_warning":
                    code = "journal_capacity_warning"
                elif ev == "live7_cost_alarm":
                    code = "cost_alarm"
                elif ev == "mind_reflection_failed":
                    code = "mind_reflection_failed"  # mind slice 1: health alert, no goals were invented
                elif ev in {"crash", "farmhand_crash", "host_crash", "trial_crash"}:
                    code = "crash"
                elif ev in {"live7_loop", "loop_detector"}:
                    signature = json.dumps([event.get(k) for k in ("skill", "arg", "target", "reason")])
                    count = self.state["loops"].get(signature, 0) + 1
                    self.state["loops"][signature] = count
                    if count == self.loop_repeats:
                        code = "repeated_loop"
                if (ev in {"skill_finished", "part_live_row", "day_ended", "checkpoint", "location_changed"}
                        and (event.get("verified") is True or ev in {"day_ended", "checkpoint", "location_changed"})):
                    self.state.update(last_progress=self.game_clock.now(), idle_alerted=False)
                if code:
                    alerts.append(dict(event=code, source=str(path), line=line, at=self.clock()))
        if active and not self.game_clock.paused() and (alert := host_wait_alert(self.state["host_wait"], self.clock())):
            alerts.append(alert)
        if active and not self.game_clock.paused() and self.game_clock.now() - self.state["last_progress"] >= self.idle_seconds and not self.state.get("idle_alerted"):
            alerts.append(dict(event="no_progress", at=self.clock(), idle_minutes=self.idle_seconds / 60))
            self.state["idle_alerted"] = True
        return alerts

    def deliver(self, alert):
        if not self.url:
            return {**alert, "delivery": "log_only"}
        request = urllib.request.Request(self.url, data=json.dumps(alert).encode("utf-8"),
                                         headers={"Content-Type": "application/json",
                                                  # Cloudflare-fronted relays (T3) reject urllib's default UA (error 1010).
                                                  "User-Agent": "arti-live7-watcher/1.0"}, method="POST")
        # Never follow redirects: the configured endpoint is the only authorized recipient.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
                return {**alert, "delivery": "sent", "http_status": response.status}
        except (OSError, urllib.error.URLError, ValueError):
            return {**alert, "delivery": "failed"}  # do not leak URL credentials in exceptions


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--cycle-dir", type=Path)
    source.add_argument("--journal", type=Path)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--state", type=Path)
    ap.add_argument("--log", type=Path)
    ap.add_argument("--idle-minutes", type=float, default=5)
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--loop-repeats", type=int, default=2)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    if a.idle_minutes <= 0 or a.interval <= 0 or a.loop_repeats < 2:
        ap.error("positive idle/interval and --loop-repeats >=2 required")
    config = read_json(a.config, {}) if a.config else {}
    url = os.environ.get("ARTI_LIVE7_WEBHOOK_URL") or config.get("webhook_url")
    if url and urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        ap.error("webhook must be http(s)")
    state_path = a.state or ((a.cycle_dir or a.journal.parent) / "watcher_state.json")
    log = a.log or state_path.with_name("watcher_log.jsonl")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    w = Watcher(url=url, idle_minutes=a.idle_minutes, loop_repeats=a.loop_repeats,
                state=read_json(state_path, {}))
    try:
        while True:
            cycle = read_json(a.cycle_dir / "cycle_state.json", {}) if a.cycle_dir else {}
            active = cycle.get("status") in {"active", "host_wait", "rejoining"} if a.cycle_dir else True
            for alert in w.scan(journal_paths(a.cycle_dir) if a.cycle_dir else [a.journal], active=active):
                receipt = w.deliver(alert)
                ipc.append_text(log, json.dumps(receipt) + "\n")
                print(json.dumps(receipt), flush=True)
            write_json(state_path, w.state)
            if a.once:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
