"""Rig-only test instrument: scripted said-lines for a free-play session (ADR 229 part 5).

ADR 228 part B (what is said to her stays in the record and reaches her page)
cannot be checked in free play: nobody speaks on the rig. When a free-play
session's config carries ``rig_scripted_said_lines`` (absent by default) naming
a JSON file, each listed line is delivered once, at its listed moment, through
the same functions a real bridge line takes: ``stream_notes.bridge_line``
(owner, ingress ``ptt``) or ``stream_notes.bridge_viewer`` (viewer, ingress
``chat``; its synthetic label is the display name she remembers, ADR 230), which write the inbox row with
the session id, privacy epoch, ``game_active``, ``recent_moments`` and wall time
they always derive. Live7's own ``live7_ingest`` -> ``ingest`` -> gate ->
``record_line`` then decide, as at a live. This module adds nothing to her page.

Only ``scripts/stardew_m1_trial.run_trial`` starts it, for a Live7 grant. The
bridge and the co-op path never import it. ``stream_game_notes`` must already
be on; the instrument never switches it on.

File shape (``arti.rig_said_lines/1``)::

    {"schema": "arti.rig_said_lines/1", "lines": [
      {"text": "Coba panen dulu sebelum tidur ya.", "speaker": "owner", "at": {"day": 1, "clock": "19:00"}},
      {"text": "Kirim kayunya juga.", "speaker": "viewer", "viewer": "synthetic-1",
       "at": {"after_first_page_min": 30}}]}

``day`` counts game days from the session's first observed day (day 1).
Journal rows: ``rig_said_line`` per delivered line (text, speaker, moment,
when, inbox result) and ``rig_said_line_refused`` per line the gate refused,
with its reason.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

KEY = "rig_scripted_said_lines"
SCHEMA = "arti.rig_said_lines/1"
SPEAKERS = ("owner", "viewer")
MAX_LINES = 64
KEPT = ("kept", "repeat")


def load(path):
    """The listed lines, validated; a malformed file is refused whole (ValueError), never guessed."""
    return parse(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def parse(doc):
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("lines"), list):
        raise ValueError("rig_said_lines_schema")
    lines = []
    for i, line in enumerate(doc["lines"][:MAX_LINES]):
        at = line.get("at") if isinstance(line, dict) else None
        if (not isinstance(line, dict) or not isinstance(line.get("text"), str) or not line["text"].strip()
                or line.get("speaker") not in SPEAKERS or not isinstance(at, dict)
                or line["speaker"] == "viewer" and not (isinstance(line.get("viewer"), str) and line["viewer"].strip())):
            raise ValueError("rig_said_lines_line:%d" % i)
        if "after_first_page_min" in at:
            if not isinstance(at["after_first_page_min"], (int, float)) or at["after_first_page_min"] < 0:
                raise ValueError("rig_said_lines_moment:%d" % i)
            moment = dict(after_first_page_min=float(at["after_first_page_min"]))
        else:
            clock = re.fullmatch(r"(\d{1,2}):(\d{2})", str(at.get("clock") or ""))
            if type(at.get("day")) is not int or at["day"] < 1 or not clock or int(clock[2]) > 59:
                raise ValueError("rig_said_lines_moment:%d" % i)
            moment = dict(day=at["day"], clock=int(clock[1]) * 100 + int(clock[2]))
        identity = hashlib.sha256(json.dumps([i, line["text"], line["speaker"], at], ensure_ascii=False,
                                             sort_keys=True).encode()).hexdigest()[:16]
        lines.append(dict(index=i, id=identity, text=line["text"], speaker=line["speaker"],
                          viewer=line.get("viewer"), at=dict(at), moment=moment))
    return lines


class RigIngress:
    """The few bridge attributes ``bridge_line``/``bridge_viewer`` read, for a rig session without a bridge.

    No screen privacy exists on the rig: epoch 0, never restricted. The game
    is active exactly when the Live7 runtime says so. No viewer roster.
    """
    def __init__(self, runtime, config):
        self.CONFIG = config
        self._stardew_runtime = runtime
        self._screen_privacy_ingress_lock = threading.Lock()
        self._yt_viewers_seen = set()
        self.screen_privacy = _Privacy()

    def _stardew_live(self):
        return True

    def _live7_game_session_stopped(self):
        return False


class _Privacy:
    epoch = 0
    restricted = False

    def current(self, epoch):
        return epoch == self.epoch


def _journals(runtime):
    """This cycle's day journals in order (the same set live7_ingest reads)."""
    current = Path(runtime._journal_path())
    paths = sorted(current.parent.parent.parent.glob("day-*/journal/g1_journal.jsonl")) if current.parent.name == "journal" else []
    if current not in paths:
        paths.append(current)
    return [p for p in paths if p.exists()]


class Instrument:
    """Delivers each listed line once; one poll per ``poll_s``; never raises into the trial."""

    def __init__(self, runtime, config, lines, *, clock=time.time, poll_s=1.0):
        self.runtime, self.config, self.lines = runtime, config, lines
        self.clock, self.poll_s = clock, poll_s
        self.ingress = RigIngress(runtime, config)
        self.outcomes = runtime._rig_said_outcomes = []  # filled by stream_notes.ingest, read here only
        self.delivered, self.reported = {}, set()
        self.first_day = self.first_page = self.latest = None
        self._offsets = {}
        self._inactive = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="rig-said-lines", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self):
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a test instrument never ends a trial
                self.runtime._journal(dict(ev="rig_said_line_error", error=type(exc).__name__))

    def _read(self):
        from arti_runtime.stream_notes import game_day
        for path in _journals(self.runtime):
            offset = self._offsets.get(path, 0)
            with path.open("rb") as handle:
                handle.seek(offset)
                while raw := handle.readline():
                    if not raw.endswith(b"\n"):
                        break
                    self._offsets[path] = handle.tell()
                    try:
                        row = json.loads(raw.decode("utf-8-sig", errors="replace"))
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    ev = row.get("ev")
                    if ev == "native_vitals" and isinstance(row.get("at"), (int, float)):
                        game = row.get("game") or {}
                        try:
                            day = game_day(game.get("date"))
                        except ValueError:
                            continue
                        if self.first_day is None:
                            self.first_day = day
                        self.latest = dict(at=row["at"], day=day, clock=game.get("time"), place=game.get("location"))
                    elif ev == "trial_decision" and self.first_page is None and isinstance(row.get("at"), (int, float)):
                        self.first_page = row["at"]
                    elif ev == "rig_said_line" and row.get("id"):
                        self.delivered.setdefault(row["id"], dict(row))
                    elif ev == "rig_said_line_refused" and row.get("id"):
                        self.reported.add(row["id"])

    def due(self, line, now):
        moment = line["moment"]
        if "after_first_page_min" in moment:
            return self.first_page is not None and now - self.first_page >= moment["after_first_page_min"] * 60
        offset = self.latest["day"] - self.first_day + 1
        clock = self.latest["clock"] if type(self.latest["clock"]) is int else -1
        return offset > moment["day"] or offset == moment["day"] and clock >= moment["clock"]

    def tick(self):
        """One poll: deliver what is due after a native observation exists; report gate refusals."""
        from arti_runtime import stream_notes
        if self.config.get("stream_game_notes") is not True:
            if not self._inactive:
                self._inactive = True
                self.runtime._journal(dict(ev="rig_said_lines_inactive", reason="stream_game_notes_off",
                                           lines=len(self.lines)))
            return
        self._read()
        if self.latest is None:
            return  # ingest drops a row without a preceding native observation: wait for one
        for line in self.lines:
            if line["id"] in self.delivered or not self.due(line, self.clock()):
                continue
            before = time.time()
            if line["speaker"] == "owner":
                inbox = stream_notes.bridge_line(self.ingress, "owner", "ptt", line["text"])
            else:
                inbox = stream_notes.bridge_viewer(self.ingress, line["viewer"], line["text"])
            row = dict(ev="rig_said_line", id=line["id"], line=line["index"], text=line["text"],
                       speaker=line["speaker"], moment=line["at"], inbox=bool(inbox), wall_after=before,
                       game=dict(day_offset=self.latest["day"] - self.first_day + 1, day=self.latest["day"],
                                 clock=self.latest["clock"], place=self.latest["place"]),
                       **({"viewer": stream_notes.viewer_hash(line["viewer"]),
                           "viewer_name": stream_notes.display_name(line["viewer"])} if line["speaker"] == "viewer" else {}))
            self.delivered[line["id"]] = row
            self.runtime._journal(row)
        self._report(stream_notes)

    def _report(self, stream_notes):
        for outcome in list(self.outcomes):
            for line in self.lines:
                sent = self.delivered.get(line["id"])
                if (sent is None or line["id"] in self.reported or not sent.get("inbox")
                        or outcome.get("speaker") != (line["speaker"] if line["speaker"] == "owner" else "viewer")
                        or outcome.get("text_sha256") != hashlib.sha256(line["text"].encode()).hexdigest()
                        or float(outcome.get("wall_ts") or 0) < float(sent.get("wall_after") or 0)):
                    continue
                self.reported.add(line["id"])
                if outcome.get("reason") not in KEPT:
                    self.runtime._journal(dict(ev="rig_said_line_refused", id=line["id"], line=line["index"],
                                               text=line["text"], speaker=line["speaker"], reason=outcome.get("reason")))


def maybe_start(runtime, config, *, start=True, **kwargs):
    """The run_trial hook. No key: nothing at all (no file, no thread, no row)."""
    path = (config or {}).get(KEY)
    if not path:
        return None
    try:
        lines = load(path)
    except (OSError, ValueError) as exc:
        runtime._journal(dict(ev="rig_said_lines_refused", reason=str(exc)[:80] if isinstance(exc, ValueError)
                              else type(exc).__name__))
        return None
    instrument = Instrument(runtime, config, lines, **kwargs)
    return instrument.start() if start else instrument
