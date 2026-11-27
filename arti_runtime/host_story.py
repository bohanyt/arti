"""HOST-witnessed native story for the persona (mabar): what the HOST watches while she is not in it.

The HOST mod appends `host_story.jsonl` and publishes its current relay window in `host_story.session.json`.
Only lines from the current HOST-only window can reach the voice or native story memory. Speech also expires
60 seconds after the HOST saw the line, even after the voice queue has accepted it.
"""
from __future__ import annotations

from contextvars import ContextVar
import json
import os
import threading
import time
from pathlib import Path

from arti_runtime import ipc
from arti_runtime.event_framing import MomentText, safe_fact

PHASES = frozenset({"started", "line", "change", "gap", "ended", "capped"})
BATCH_LINES = 8
LINE_CHARS = 240
READ_MAX = 1 << 20
LINES_MAX = 64
CHANGES_MAX = 32
CONTEXT_CHARS = 12000
MAX_LINE_AGE_MS = 60_000
PRESENCE_MAX_AGE_MS = 3_000
playback_story = ContextVar("host_story_playback", default=())


def playback_current():
    return all(story.is_current() for story in playback_story.get())


def host_scope(scope):
    """The HOST-witness scope for the save of an actor scope `save_id:player_id` (native_story.record_scope)."""
    save = str(scope).split(":", 1)[0] if scope else ""
    return save + ":host" if save else None


def _text(value, limit):
    return safe_fact(value, limit) or "UNKNOWN"


def parse(line):
    """One contract-v1 row, or None when it is not one (never raises)."""
    try:
        row = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if (not isinstance(row, dict) or row.get("v") != 1 or row.get("witness") != "host"
            or row.get("phase") not in PHASES or type(row.get("seq")) is not int
            or not isinstance(row.get("episode"), str) or "save_id" not in row):
        return None
    return row


class HostStoryText(MomentText):
    _host_story = True
    _host_story_guarded = True

    def is_current(self):
        return self.generation == self.relay.generation and self.relay.is_current(self.window, self.oldest_ms)

    def with_text(self, value):
        text = HostStoryText(value)
        text.relay, text.window, text.generation, text.oldest_ms = self.relay, self.window, self.generation, self.oldest_ms
        return text


class HostStoryRelay:
    owner_name = "Bohan"  # the bridge sets the live streamer_name

    def __init__(self, path, *, mind_base=None, state_path=None, clock=time.time, stopped=lambda: False):
        self.path = Path(path)
        self.mind_base = Path(mind_base) if mind_base else None
        self.state_path = Path(state_path) if state_path else self.path.with_name(self.path.stem + ".bridge-state.json")
        self.presence_path = self.path.with_name(self.path.stem + ".session.json")
        self._clock = clock
        self._started_ms = int(clock() * 1000)
        self._stopped = stopped
        self._closed = False
        self._window = None
        self.generation = 0
        self.offset, self.last_seq, self.pending, self.episodes = 0, 0, [], {}
        self._lock = threading.RLock()
        self.context_lines = []  # independent of the moment delivery cursor (ADR 168)
        self.last_runtime = None
        self.save_hint = None  # the save named by a later row binds an opening shown before the save existed
        self.voiced = self.rows = self.invalid = 0
        try:
            state = json.loads(ipc.read_text(self.state_path, encoding="utf-8"))
            self.offset, self.last_seq = int(state["offset"]), int(state["last_seq"])
            self.save_hint = state.get("save_hint")
            self.last_runtime = state.get("last_runtime")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            # Start at the append point, including rows the previous process
            # had not polled. A pre-restart partial line is discarded too.
            self.offset = self.path.stat().st_size
        except OSError:
            pass

    def _read_presence(self):
        try:
            status = ipc.read_json(self.presence_path)
        except (OSError, ValueError):
            return None
        now_ms = self._clock() * 1000
        if not isinstance(status, dict):
            return None
        runtime, window = status.get("runtime_id"), status.get("relay_window")
        epoch = status.get("window")
        stamp, pid = status.get("utc_ms"), status.get("pid")
        if not (isinstance(runtime, str) and runtime and type(epoch) is int and epoch > 0 and "relay_window" in status
                and (window is None or type(window) is int and window == epoch)
                and type(stamp) is int and 0 <= now_ms - stamp <= PRESENCE_MAX_AGE_MS
                and type(pid) is int and pid > 0):
            return None
        if os.name == "nt":
            from scripts.m1_operator.win_facts import pid_alive
            if not pid_alive(pid):
                return None
        return status

    @staticmethod
    def _open_window(presence):
        if presence is None or presence["relay_window"] is None:
            return None
        if os.name == "nt":
            from scripts.m1_operator.win_facts import event_signaled
            gate = presence.get("gate")
            if not (isinstance(gate, str) and gate.startswith("Local\\ArtiHostStory-") and event_signaled(gate)):
                return None
        return presence["runtime_id"], presence["relay_window"]

    def _owner_hosted(self):
        """Never offer the solo relay in a run a person hosts, before join or after leave."""
        from arti_runtime.human_host import is_owner_hosted_run
        return is_owner_hosted_run(self.path.parent)

    def _read_window(self):
        return None if self._closed or self._owner_hosted() or self._stopped() else self._open_window(self._read_presence())

    def _fresh(self, stamp, now_ms):
        return (type(stamp) is int and stamp >= self._started_ms
                and 0 <= now_ms - stamp <= MAX_LINE_AGE_MS)

    def is_current(self, window, oldest_ms):
        return (window is not None and self._read_window() == window
                and self._fresh(oldest_ms, self._clock() * 1000))

    def _discard(self):
        self.generation += 1
        self.pending.clear()
        self.episodes.clear()
        self.context_lines.clear()
        self.save_hint = None

    def stop(self):
        with self._lock:
            self._closed = True
            self._window = None
            self._discard()

    def _save(self):
        try:
            ipc.atomic_write(self.state_path, json.dumps(dict(
                offset=self.offset, last_seq=self.last_seq, save_hint=self.save_hint,
                last_runtime=self.last_runtime), ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass  # the source file stays; the next poll may repeat at most the unsaved rows

    def poll(self):
        with self._lock:
            return self._poll()

    def _poll(self):
        try:
            with ipc.open_shared(self.path, "rb") as stream:
                stream.seek(self.offset)
                data = stream.read(READ_MAX)
        except OSError:
            data = b""
        stopped = self._closed or self._owner_hosted() or self._stopped()
        presence = None if stopped else self._read_presence()
        window = self._open_window(presence)
        now_ms = self._clock() * 1000
        if window != self._window:
            self._discard()
            self._window = window
        expired = {r["key"] for r in self.pending if not self._fresh(r["utc_ms"], now_ms)}
        self.pending = [r for r in self.pending if self._fresh(r["utc_ms"], now_ms)]
        self.context_lines = [r for r in self.context_lines if self._fresh(r["utc_ms"], now_ms)]
        for key in expired:
            self._retire_if_done(key)
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        fresh = consumed = 0
        for raw in data[:end].split(b"\n"):
            row = parse(raw.decode("utf-8", errors="replace"))
            if row is not None and not stopped and self._awaiting_presence(row, presence, now_ms):
                # A background window write can lag the line. Leave this row
                # and the rest of the tail at the cursor for the next poll.
                break
            consumed += len(raw) + 1
            if row is None:
                self.invalid += raw.strip() != b""
                continue
            runtime = row.get("runtime_id")
            if runtime and runtime != self.last_runtime:
                self.last_seq, self.last_runtime = 0, runtime
            if row["seq"] <= self.last_seq:
                continue
            self.last_seq = row["seq"]
            self.rows += 1
            fresh += 1
            if (window is not None and (runtime, row.get("relay_window")) == window
                    and self._fresh(row.get("utc_ms"), now_ms)):
                self._episode(row)
        if not consumed:
            return 0
        self.offset += consumed
        if self.save_hint:
            for line in self.context_lines:
                if not line.get("save_id"):
                    line["save_id"] = self.save_hint
            for key, detail in list(self.episodes.items()):
                if detail.get("needs_binding"):
                    detail["save_id"], detail["needs_binding"] = self.save_hint, False
                    detail["save_bound_from_later_row"] = True
                    self._remember(key, detail)
                    self._retire_if_done(key)
        self._save()
        return fresh

    def _awaiting_presence(self, row, presence, now_ms):
        stamp, epoch = row.get("utc_ms"), row.get("relay_window")
        runtime = row.get("runtime_id")
        if (not isinstance(runtime, str) or not runtime or type(epoch) is not int or epoch <= 0
                or not self._fresh(stamp, now_ms)):
            return False
        if presence is None:
            return True
        if runtime == presence["runtime_id"]:
            return epoch > presence["window"]
        return stamp >= presence["utc_ms"]

    def _retire_if_done(self, key):
        detail = self.episodes.get(key, {})
        if detail.get("complete") and not detail.get("needs_binding") and not any(p["key"] == key for p in self.pending):
            self.episodes.pop(key, None)

    def _episode(self, row):
        save = str(row.get("save_id") or "").strip()
        if save in ("", "0", "UNKNOWN"):
            save = ""  # native save identity not assigned yet (e.g. the opening minigame)
        else:
            self.save_hint = save
        key = row["episode"]
        detail = self.episodes.setdefault(key, dict(
            save_id=save or None, episode=row["episode"], lines=[], changes=[], complete=False))
        if save and not detail.get("save_id"):
            detail["save_id"] = save
        detail.update(source=_text(row.get("source"), 96), event_id=_text(row.get("event_id"), 96), event_type=_text(row.get("event_type"), 80),
                      date=_text(row.get("date"), 32), location=_text(row.get("location"), 96),
                      last_seq=row["seq"], witness="host", bounded=True)
        phase = row["phase"]
        if phase == "line" and safe_fact(row.get("text"), LINE_CHARS):
            self.context_lines.append(dict(speaker=_text(row.get("speaker"), 64),
                text=safe_fact(row.get("text"), 2048), save_id=save or self.save_hint,
                source=_text(row.get("source") or row.get("event_type"), 96),
                location=detail["location"], date=detail["date"], utc_ms=row["utc_ms"]))
            self.context_lines = self.context_lines[-LINES_MAX:]
            line = dict(speaker=_text(row.get("speaker"), 64), text=safe_fact(row.get("text"), LINE_CHARS))
            if type(row.get("narration")) is bool:
                line["narration"] = row["narration"]
            if len(detail["lines"]) < LINES_MAX:
                detail["lines"].append(line)
            self.pending.append(dict(key=key, seq=row["seq"], utc_ms=row["utc_ms"], **line))
        elif phase == "change" and safe_fact(row.get("change"), LINE_CHARS) not in ("", "UNKNOWN"):
            if len(detail["changes"]) < CHANGES_MAX:
                detail["changes"].append(safe_fact(row.get("change"), LINE_CHARS))
        elif phase in ("ended", "capped"):
            detail["complete"] = True
        detail["unknown"] = detail.get("unknown", False) or phase == "gap"
        detail["needs_binding"] = not detail.get("save_id")
        self._remember(key, detail)
        self._retire_if_done(key)

    def _remember(self, key, detail):
        if self.mind_base is None or not detail.get("save_id"):
            return
        from arti_runtime.native_story import day_of
        import arti_mind
        day = day_of(detail["date"])
        facts = {k: v for k, v in detail.items() if k not in ("save_id", "episode", "needs_binding")}
        if day is None:
            # A native date before day 1 (the opening) is kept verbatim and remembered on the first day.
            facts.update(native_date=detail["date"], date="1:spring:1")
            day = day_of("1:spring:1")
        try:
            arti_mind.record_items(self.mind_base, "stardew", scope=detail["save_id"] + ":host", kind="native_story",
                                   items=[dict(id="host:" + detail["episode"],
                                               status="ended" if detail["complete"] else "witnessed", facts=facts)],
                                   day=day, day_label=facts["date"])
        except (OSError, ValueError, TypeError):
            pass  # memory failure never blocks the voice path

    def text(self):
        """The next batch as one held game moment, oldest first, or None."""
        if not self.pending:
            return None
        batch = self.pending[:BATCH_LINES]
        parts, last = [], None
        for row in batch:
            episode = self.episodes.get(row["key"], {})
            if row["key"] != last:
                parts.append("(%s %s, %s, %s)" % (episode.get("event_type", "UNKNOWN"), episode.get("event_id", "UNKNOWN"),
                                                 episode.get("location", "UNKNOWN"), episode.get("date", "UNKNOWN")))
                last = row["key"]
            who = "narasi" if row.get("narration") else row["speaker"]
            parts.append('%s: "%s"' % (who, row["text"]))
        text = self._guarded("[Stardew] Ditampilkan di layar HOST (%s yang main HOST, kamu ikut nonton, " % self.owner_name +
                            "kamu tidak ada di adegan ini). Baris asli game: " + " | ".join(parts),
                            min(row["utc_ms"] for row in batch))
        text._live7_hold = True  # the source timestamp, below, controls expiry
        text.batch_size = len(batch)
        return text

    def _guarded(self, value, oldest_ms):
        text = HostStoryText(value)
        text.relay, text.window, text.generation, text.oldest_ms = self, self._window, self.generation, oldest_ms
        return text

    def tick(self, enqueue, *, ready=lambda: True):
        with self._lock:
            return self._tick(enqueue, ready=ready)

    def _tick(self, enqueue, *, ready):
        self._poll()
        if not self.pending or not ready():
            return False
        text = self.text()
        if not text.is_current():
            return False
        try:
            accepted = bool(enqueue(text, trigger_type="game"))
        except Exception:  # noqa: BLE001 - fail closed this tick, retried next tick
            accepted = False
        if accepted:
            done = {(p["key"], p["seq"]) for p in self.pending[:text.batch_size]}
            self.pending = self.pending[text.batch_size:]
            self.voiced += len(done)
            for key in {k for k, _ in done}:
                self._retire_if_done(key)
            print(f"[HostStory] voiced lines={len(done)} pending={len(self.pending)}", flush=True)
        return accepted

    def attention_situation(self):
        """Current HOST episode state, separate from moment delivery."""
        with self._lock:
            if self._window is None or self._read_window() != self._window:
                return dict(cutscene=False)
            now_ms = self._clock() * 1000
            active = [d for d in self.episodes.values() if not d.get("complete")
                      and (d.get("event_type") == "Event" or d.get("source") in {"event", "farm_event", "minigame:GrandpaStory"})]
            lines = [r for r in self.context_lines[-16:] if self._fresh(r["utc_ms"], now_ms)]
            situation = dict(cutscene=bool(active) if self.rows else None, witness="host",
                             lines=[dict(speaker=r["speaker"], text=r["text"]) for r in lines])
            if active:
                situation["host_story"] = self._guarded("", min((r["utc_ms"] for r in lines), default=int(now_ms)))
            return situation

    def reply_context(self):
        """Read stored native lines before the next model call, even while busy.

        Reading facts neither consumes nor enqueues a voice moment. Source,
        witness and past tense stay attached; these are never her current pose.
        """
        with self._lock:
            if self._closed or self._owner_hosted() or self._stopped():
                self._discard()
                return ""
            self._poll()
            lines = [line for line in self.context_lines
                     if not self.save_hint or line.get("save_id") == self.save_hint]
            if not lines:
                return ""
            parts = [('%s: "%s" (sumber: %s; %s; %s)' % (
                line["speaker"], line["text"], line.get("source", "baris asli game"),
                line.get("location", "UNKNOWN"), line.get("date", "UNKNOWN")), line["utc_ms"]) for line in lines]
            shown, stamps, size = [], [], 0
            for part, stamp in reversed(parts):
                if size + len(part) > CONTEXT_CHARS:
                    break
                shown.append(part)
                stamps.append(stamp)
                size += len(part) + 3
            quoted = " | ".join(reversed(shown))
            return self._guarded("\n[STARDEW STATUS] Cerita yang kuterima dari layar game %s lewat relay native; "
                                 "aku ikut membaca, bukan berada di adegannya. %s\n" % (self.owner_name, quoted), min(stamps))
