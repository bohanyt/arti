"""HOST-witnessed native story for the persona (mabar): what the HOST watches while she is not in it.

The HOST mod appends `host_story.jsonl` (contract v1: an internal note "Relay contract"); this reader
only tails it. Each native line reaches her once as a game moment with its event id and speaker, as facts; the model
chooses whether and how to react. Lines that arrive while she is busy wait in order and are never dropped. Every row is
also written to the existing native_story ledger (ADR 63) under the save's `host` scope, so she remembers the episode
after she joins the same save. No event/NPC/intro names live here.
"""
from __future__ import annotations

import json
import threading
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


class HostStoryRelay:
    owner_name = "Bohan"  # the bridge sets the live streamer_name

    def __init__(self, path, *, mind_base=None, state_path=None, clock=None):
        self.path = Path(path)
        self.mind_base = Path(mind_base) if mind_base else None
        self.state_path = Path(state_path) if state_path else self.path.with_name(self.path.stem + ".bridge-state.json")
        self.offset, self.last_seq, self.pending, self.episodes = 0, 0, [], {}
        self._lock = threading.RLock()
        self.context_lines = []  # independent of the moment delivery cursor (ADR 168)
        self.last_runtime = None
        self.save_hint = None  # the save named by a later row binds an opening shown before the save existed
        self.voiced = self.rows = self.invalid = 0
        try:
            state = json.loads(ipc.read_text(self.state_path, encoding="utf-8"))
            self.offset, self.last_seq = int(state["offset"]), int(state["last_seq"])
            self.pending = [r for r in state.get("pending", ()) if isinstance(r, dict)]
            self.episodes = {k: v for k, v in (state.get("episodes") or {}).items() if isinstance(v, dict)}
            self.save_hint = state.get("save_hint")
            self.last_runtime = state.get("last_runtime")
            self.context_lines = state.get("context_lines", [])[-LINES_MAX:]
            if not self.context_lines:  # pre-ADR168 checkpoint, including already queued episodes
                self.context_lines = [dict(line, save_id=d.get("save_id"), source=d.get("event_type"),
                                           location=d.get("location"), date=d.get("date"))
                                      for d in self.episodes.values() for line in d.get("lines", ())][-LINES_MAX:]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save(self):
        try:
            ipc.atomic_write(self.state_path, json.dumps(dict(
                offset=self.offset, last_seq=self.last_seq, pending=self.pending,
                episodes=self.episodes, save_hint=self.save_hint, context_lines=self.context_lines,
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
            return 0
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        fresh = 0
        for raw in data[:end].split(b"\n"):
            row = parse(raw.decode("utf-8", errors="replace"))
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
            self._episode(row)
        self.offset += end + 1
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
                location=detail["location"], date=detail["date"]))
            self.context_lines = self.context_lines[-LINES_MAX:]
            line = dict(speaker=_text(row.get("speaker"), 64), text=safe_fact(row.get("text"), LINE_CHARS))
            if type(row.get("narration")) is bool:
                line["narration"] = row["narration"]
            if len(detail["lines"]) < LINES_MAX:
                detail["lines"].append(line)
            self.pending.append(dict(key=key, seq=row["seq"], **line))
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
        text = MomentText("[Stardew] Tampil di layar HOST sekarang (%s yang main HOST, kamu ikut nonton, " % self.owner_name +
                          "kamu tidak ada di adegan ini). Baris asli game: " + " | ".join(parts))
        text._live7_hold = True  # waits behind a long turn instead of the 28 s moment cull
        text._host_story = True
        text.batch_size = len(batch)
        return text

    def tick(self, enqueue, *, ready=lambda: True):
        with self._lock:
            return self._tick(enqueue, ready=ready)

    def _tick(self, enqueue, *, ready):
        self.poll()
        if not self.pending or not ready():
            return False
        text = self.text()
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
            self._save()
            print(f"[HostStory] voiced lines={len(done)} pending={len(self.pending)}", flush=True)
        return accepted

    def attention_situation(self):
        """Memory-only HOST episode state, separate from moment delivery (ADR 172)."""
        with self._lock:
            active = [d for d in self.episodes.values() if not d.get("complete")
                      and (d.get("event_type") == "Event" or d.get("source") in {"event", "farm_event", "minigame:GrandpaStory"})]
            return dict(cutscene=bool(active) if self.rows else None, witness="host", lines=[dict(speaker=r.get("speaker", "UNKNOWN"),
                        text=r.get("text", "")) for r in self.context_lines[-16:]])

    def reply_context(self):
        """Read stored native lines before the next model call, even while busy.

        Reading facts neither consumes nor enqueues a voice moment. Source,
        witness and past tense stay attached; these are never her current pose.
        """
        with self._lock:
            self._poll()
            lines = [line for line in self.context_lines
                     if not self.save_hint or line.get("save_id") == self.save_hint]
            if not lines:
                return ""
            parts = ['%s: "%s" (sumber: %s; %s; %s)' % (
                line["speaker"], line["text"], line.get("source", "baris asli game"),
                line.get("location", "UNKNOWN"), line.get("date", "UNKNOWN")) for line in lines]
            shown, size = [], 0
            for part in reversed(parts):
                if size + len(part) > CONTEXT_CHARS:
                    break
                shown.append(part)
                size += len(part) + 3
            quoted = " | ".join(reversed(shown))
            return ("\n[STARDEW STATUS] Cerita yang kuterima dari layar game %s lewat relay native; "
                    "aku ikut membaca, bukan berada di adegannya. %s\n" % (self.owner_name, quoted))
