"""Optional arti.help/1 integration. The Bridge owns the shared bus/transport.

Requests are queued under the runtime lock and dispatched by _skill_flush.
File replies are polled by process_skill_boundary, also outside that lock.
Neither transport nor an advisory grants permission or changes native offers.
``live7_help_enabled`` defaults off. ``stardew_live7_help_dir`` is the
operator's run folder; the design's ``live7_help_dir`` spelling is an alias.
Requires a runtime restart after adding the optional bus module/config.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

try:
    import arti_help_bus
except ImportError:  # Stardew and Bridge can land independently.
    arti_help_bus = None


class Live7HelpMixin:
    def _live7_init_help(self, help_sink):
        self._help_sink = help_sink
        self._help_channel = None
        self._help_pending = []
        self._help_requests = {}
        self._help_poll_after = 0.
        self._said_to_you = []
        self._said_seen = {}
        self._said_lines = {}
        folder = self._config.get("stardew_live7_help_dir") or self._config.get("live7_help_dir")
        if (self._config.get("live7_help_enabled") is True and folder and help_sink is None
                and arti_help_bus is not None):
            try:
                self._help_channel = arti_help_bus.FileHelpChannel(folder)
                self._help_sink = self._help_channel.write_request
            except Exception as exc:  # Optional relay must not stop the game worker.
                self._journal(dict(ev="help_transport_error", error=type(exc).__name__))

    def set_help_sink(self, fn):
        """Install an in-process HelpRequest callback; invoked outside _lock."""
        if fn is not None and not callable(fn):
            raise TypeError("help sink must be callable or None")
        with self._lock:
            self._help_sink = fn
            self._help_channel = None

    @staticmethod
    def _live7_help_speech():
        from arti_stardew_runtime import OwnerTurnResult

        return OwnerTurnResult(admitted=True, skip_reply_tags=True,
            reason="minta_tolong", speech="Duh, aku buntu nih. Bantuin dong, lewat suara atau chat. Penonton juga boleh!")

    def _live7_queue_help(self, session, reason, *, request_id=None):
        """Caller holds _lock. No transport or callback IO here."""
        if self._config.get("live7_help_enabled") is not True or arti_help_bus is None:
            return
        now = time.time()
        self._help_requests = {k: v for k, v in self._help_requests.items()
                               if v["request"].expires_at > now and v["request"].session_id == session.session_id}
        # Expiry and a hard bound keep long-running sessions small.
        while len(self._help_requests) >= 32:
            self._help_requests.pop(next(iter(self._help_requests)))
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        p, c = session.campaign.live, session.campaign
        run = session.current or next(reversed(session.runs), None)
        rows = c.days[-1].get("skills", []) if c.days else []
        row_code = rows[-1].get("outcome_class", "none") if rows else "none"
        detail = (f"boundaries={p.idle_boundaries}; recoveries={p.recoveries}; "
                  f"clock={getattr(motor, 'time_of_day', None)}; last={row_code}")[:160]
        # A model ask_owner speech is not a machine reason code.
        code = "model_asked_for_help" if reason == session.question else str(reason)
        code = re.sub(r"[^a-zA-Z0-9_:.-]", "_", code)[:96]
        year, season, day = c.date
        try:
            request = arti_help_bus.HelpRequest(v=1, request_id=request_id or uuid.uuid4().hex,
                session_id=session.session_id, reason=code,
                game_date=f"Y{year} {str(season).capitalize()} {day}",
                game_time=getattr(motor, "time_of_day", None), location=getattr(motor, "location", None),
                skill=run.call.name if run is not None else None, detail=detail,
                created_at=now, expires_at=now + 600.)
        except Exception as exc:
            self._journal(dict(ev="help_transport_error", error=type(exc).__name__))
            return
        self._help_requests[request.request_id] = dict(request=request, policy=p, seen=set())
        if request_id is None:
            self._help_pending.append(request)
        return request

    def _live7_flush_help(self, pending, sink):
        if sink is None:
            return
        for request in pending:
            with self._lock:
                session = self._skill_session
                current = (self._config.get("live7_help_enabled") is True and session is not None
                           and session.status == "active" and session.session_id == request.session_id
                           and request.expires_at > time.time())
            if current:
                try:
                    sink(request)
                except Exception as exc:
                    # Never journal the exception message: a relay may include hint text.
                    self._journal(dict(ev="help_transport_error", error=type(exc).__name__))

    def accept_help_hint(self, request_id, hints):
        """Thread-safe advisory ingress; return the number of new accepted hints.

        Repeated cumulative replies are deduped, including hints already evicted
        from the five-entry display. A new advisory is one external change to
        Live7's frozen-clock pause; it never clears a safety or admission hold.
        """
        with self._lock:
            if (self._config.get("live7_help_enabled") is not True or arti_help_bus is None
                    or not isinstance(request_id, str)):
                return 0
            record = getattr(self, "_help_requests", {}).get(request_id)
            session = self._skill_session
            if (record is None or session is None or session.status != "active" or session.mode != "part_live"
                    or session.session_id != record["request"].session_id or session.campaign is None
                    or session.campaign.live is not record["policy"] or record["request"].expires_at <= time.time()):
                return 0
            p = session.campaign.live
            accepted = []
            for h in hints or ():
                if type(h) is not arti_help_bus.HelpHint:
                    continue
                source, at = h.source, float(h.at)
                text = arti_help_bus.sanitize_hint_text(h.text)
                if not text:
                    continue
                fingerprint = hashlib.sha256(json.dumps([source, text, at]).encode("utf-8")).digest()
                if fingerprint in record["seen"]:
                    continue
                record["seen"].add(fingerprint)
                # Keep only the contract fields, never viewer metadata on a caller's object.
                accepted.append(arti_help_bus.HelpHint(source=source, text=text, at=at))
                self._journal(dict(ev="help_hint", source=source, length=len(text)))
            if not accepted:
                return 0
            # Reversing arrivals breaks timestamp ties in favour of the newest arrival.
            p.help_hints = sorted(list(reversed(accepted)) + p.help_hints, key=lambda h: h.at, reverse=True)[:5]
            p.idle_boundaries = 0
            p.finish_streak = 0  # one new advisory permits another bounded choice attempt
            p.empty_fallbacks = 0
            self._worker_wakeup.notify_all()
            return len(accepted)

    def _live7_help_advisories(self, session):
        """Caller holds _lock; JSON-quote hint text inside explicit data blocks."""
        if self._config.get("live7_help_enabled") is not True:
            return ""
        hints = session.campaign.live.help_hints
        if not hints:
            return ""
        return "\n".join(f"[BEGIN {label}]\n" + json.dumps(
            [h.text for h in hints if h.source == source], ensure_ascii=False) + f"\n[END {label}]"
            for source, label in (("owner", "owner advisory"), ("viewer", "viewer untrusted")))

    def _live7_poll_help(self):
        """Boundary tick: reserve the poll under _lock, perform file IO outside it."""
        with self._lock:
            channel = getattr(self, "_help_channel", None)
            if (self._config.get("live7_help_enabled") is not True or channel is None
                    or time.monotonic() < self._help_poll_after):
                return
            self._help_poll_after = time.monotonic() + 1.
            now = time.time()
            self._help_requests = {k: v for k, v in self._help_requests.items()
                                   if v["request"].expires_at > now}
            request_ids = tuple(self._help_requests)
        # ADR 178: optional additive channel method. No open help request is required.
        read_said = getattr(channel, "read_said", None)
        if callable(read_said):
            try:
                self.accept_said_to_you(read_said())
            except Exception as exc:
                self._journal(dict(ev="help_transport_error", error=type(exc).__name__))
        for request_id in request_ids:
            try:
                reply = channel.read_reply(request_id)
                if reply is not None and reply.status == "hinted":
                    self.accept_help_hint(reply.request_id, reply.hints)
            except Exception as exc:
                self._journal(dict(ev="help_transport_error", error=type(exc).__name__))

    def accept_said_to_you(self, rows):
        """Existing help channel data, never permission, priority or a goal (ADR 178).

        Memory only (15 minutes, two answered pages); never written to her record. What is said to her is
        persisted through one path only: ADR 216's inbox -> stream_notes.ingest -> record_line (ADR 228).
        """
        if self._config.get("live7_help_enabled") is not True or not isinstance(rows, (list, tuple)):
            return 0
        with self._lock:
            scope = getattr(self, "_live7_discovery_scope", None)
            if not scope:
                return 0
            now = time.time()
            seen = {k: at for k, at in getattr(self, "_said_seen", {}).items() if 0 <= now - at <= 900}
            pending = [r for r in getattr(self, "_said_to_you", ()) if r["scope"] == scope and 0 <= now - r["at"] <= 900]
            added = 0
            from arti_stardew_screen_text import game_text
            for row in rows[-3:]:
                if not isinstance(row, dict) or row.get("kind") != "said_to_you":
                    continue
                at = row.get("at")
                if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= now - at <= 900:
                    continue
                text = game_text(row.get("text"), 140)
                who, platform = game_text(row.get("who"), 40), game_text(row.get("platform"), 24)
                if not text or not who or not platform:
                    continue
                ref = hashlib.sha256(json.dumps([scope, who, platform, text, at]).encode()).hexdigest()
                if ref in seen:
                    continue
                seen[ref] = at
                pending.append(dict(ref=ref, kind="said_to_you", who=who, platform=platform, text=text,
                                    at=at, scope=scope, answers=0))
                added += 1
            self._said_seen = dict(list(seen.items())[-128:])
            self._said_to_you = sorted(pending, key=lambda r: r["at"])[-3:]
            return added

    def _live7_said_lines(self, scope):
        if (getattr(self, "_config", None) or {}).get("live7_help_enabled") is not True:
            self._said_to_you, self._said_lines = [], {}
            return []
        now = time.time()
        rows = [r for r in getattr(self, "_said_to_you", ()) if r["scope"] == scope
                and 0 <= now - r["at"] <= 900 and r["answers"] < 2]
        self._said_to_you = rows[-3:]
        self._said_lines = {r["ref"]: 'Dikatakan ke kamu soal game: %s/%s (%d menit lalu): %s' % (
            r["who"], r["platform"], int((now - r["at"]) // 60), json.dumps(r["text"], ensure_ascii=False)) for r in rows[-3:]}
        return list(self._said_lines.values())

    def _live7_said_answered(self, shown):
        """A render/retry/read is not an answer. Retire only lines actually on the answered model page."""
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for v in value.values():
                    yield from strings(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    yield from strings(v)
        visible = set(strings(shown))
        for r in getattr(self, "_said_to_you", ()):
            line = getattr(self, "_said_lines", {}).get(r["ref"])
            # The ordinary page bound can keep a prefix plus a detail-read link.
            # That is still an exposure; it must not recur on every answered page.
            exposed = line and any(v == line or len(v) >= 40 and line.startswith(v) for v in visible)
            if exposed:
                r["answers"] += 1
        self._said_to_you = [r for r in getattr(self, "_said_to_you", ()) if r["answers"] < 2]
