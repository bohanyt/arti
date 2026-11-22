"""Lossy Live7 moment inbox, polled only by the help relay thread.

Disabled moments leave files untouched. Low-priority status facts yield to
moments and preserve their files while occupying the voice queue.
No writer or device imports.
"""

from __future__ import annotations

from arti_runtime import ipc, chat_media
from collections import Counter
import json
import logging
import math
from pathlib import Path
import re

from arti_help_bus import MAX_JSON_BYTES, sanitize_hint_text
from arti_runtime.event_framing import MomentText, DiscoveryText, OwnPlayText, discovery_event
from arti_runtime.streamer_voice import natural_intent

_LOG = logging.getLogger(__name__)
_PREFIX = "[Stardew]"
_KINDS = frozenset({"planted", "watered_all", "harvested", "rain_day", "shop_closed",
                    "went_to_town", "bought_seeds", "bed", "stuck", "other"})
_FILENAME = re.compile(r"ev-(\d+)-(\d+)\.json\Z")
# Owner 2026-10-24 ("[recorded remark omitted]"): bed is never
# dropped for the gap or a busy queue. It is held and queued once the voice
# queue is empty; it still never lands ahead of PTT, viewers or help.
_HELD_KINDS = frozenset({"bed"})
_HOLD_MAX_SEC = 300.0
_TAGS = re.compile(r"\[[^\]]*\]|<[^>]*>")


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _decode(payload):
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("size")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("json") from None
    if not isinstance(data, dict):
        raise ValueError("shape")
    generic = data.get("schema") == "arti.game_event/1"
    if data.get("schema") != "arti.stardew_moment/1" and not generic:
        raise ValueError("schema")
    kind = data.get("kind")
    if not isinstance(kind, str) or (kind != "discovery" if generic else kind not in _KINDS):
        raise ValueError("kind")
    situation = data.get("situation")
    prefix = "[Game]" if generic else _PREFIX
    if generic and isinstance(situation, str):
        situation = prefix + " " + situation
    if not isinstance(situation, str) or not situation.startswith(prefix):
        raise ValueError("prefix")
    # Preserve exactly one trusted source tag, never payload-supplied tags.
    body = _TAGS.sub(" ", situation[len(prefix):]).replace("[", " ").replace("]", " ")
    situation = sanitize_hint_text(f"{prefix} {body}")
    if situation == prefix:
        raise ValueError("situation")
    ts, ttl = data.get("ts"), data.get("ttl_s", 120)
    if not _number(ts) or ts < 0:
        raise ValueError("ts")
    if not _number(ttl) or not 10 <= ttl <= 600:
        raise ValueError("ttl_s")
    text = MomentText(situation)
    if generic:
        # Retain the same facts as the rig; the live turn grounds them at delivery.
        event = discovery_event(data, payload)
        text = DiscoveryText(situation)
        text.events, text.shown_ids = [event], [event["id"]]
    if kind in _HELD_KINDS:
        text._live7_hold = True  # bridge stale-cull reads this
    return ts, ttl, kind, text


class MomentsRelay:
    def __init__(self, config, *, started_at, viewer_waiting=None, game_trigger_waiting=None):
        self.config = config
        self._started_at = started_at
        self._viewer_waiting = viewer_waiting or (lambda: False)
        self._game_trigger_waiting = game_trigger_waiting or (lambda: False)
        self._last_voiced_at = None
        self._held = None  # (key, kind, text, hold_until) — pending bed
        self._last_summary_at = started_at
        self._pending_dropped = 0
        self.by_reason = Counter()
        self._pending_by_reason = Counter()
        self._diagnostics_dir = None
        self._diagnostics_written = -1
        self._logged_reasons = set()
        self.voiced = 0
        self.dropped = 0

    def _invalid(self, reason):
        if reason not in self._logged_reasons:
            self._logged_reasons.add(reason)
            _LOG.warning("[Moments] dropped reason=%s", reason)

    def _consume(self, path):
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            try:
                ipc.replace(path, path.with_suffix(".json.done"))
            except OSError:
                self._invalid("consume")
                return False
        return True

    def can_deliver(self, now, *, help_open=False):
        """Shared admission gate for optional moments and important status facts."""
        gap = self._gap()
        reactive = self.config.get("live7_reactive_play_enabled") and self.config.get("live7_own_play_enabled")
        return (not help_open and not chat_media.watch_held(self.config, now)
                and (self._last_voiced_at is None or now - self._last_voiced_at >= gap)
                and (self.config.get("attention_desk_enabled") or
                     ((reactive or not self._viewer_waiting()) and not self._game_trigger_waiting())))

    def _gap(self):
        if self.config.get("live7_reactive_play_enabled"):
            from arti_runtime.reactive_play import number
            return number(self.config, "live7_play_line_gap_sec", 45)
        return max(0.0, float(self.config.get("live7_moments_min_gap_sec", 90)))

    def _gate_reason(self, now, help_open):
        if help_open:
            return "help_open"
        if chat_media.watch_held(self.config, now):
            return "media_hold"
        if self._last_voiced_at is not None and now - self._last_voiced_at < self._gap():
            return "gap"
        if not self.config.get("attention_desk_enabled"):
            if self._viewer_waiting() and not (self.config.get("live7_reactive_play_enabled")
                                              and self.config.get("live7_own_play_enabled")):
                return "chat_waiting"
            if self._game_trigger_waiting():
                return "busy"
        return None

    def _note_dropped(self, count, reason="tick_newest"):
        if count <= 0:
            return
        self.dropped += count
        self._pending_dropped += count
        self.by_reason[reason] += count
        self._pending_by_reason[reason] += count

    def note_voiced(self, at):
        self._last_voiced_at = at

    def _deliver_held(self, enqueue, *, now, help_open, status_pending):
        """Held bed: no gap check, but it queues behind help, viewers and PTT."""
        if self._held is None:
            return False
        if now > self._held[3]:
            self._held = None
            self._note_dropped(1, "stale_ttl")
            return False
        if (help_open or status_pending or self._viewer_waiting()
                or self._game_trigger_waiting()):
            return False
        accepted = False
        try:
            accepted = bool(enqueue(self._held[2], trigger_type="game"))
        except Exception:
            pass  # fail closed this tick; retried until hold_until
        if not accepted:
            # Count the refused queue attempt, without changing the bed retry policy.
            self._note_dropped(1, "queue_refused_game")
            return False
        self._last_voiced_at = now
        self.voiced += 1
        print(f"[Moments] voiced kind={self._held[1]}", flush=True)
        self._held = None
        return accepted

    def _summary(self, now):
        if self._diagnostics_dir is not None and self._diagnostics_written != self.dropped:
            try:
                if ipc.write_json(Path(self._diagnostics_dir) / "moments-drops.json",
                                  dict(ts=now, dropped=self.dropped, by_reason=dict(self.by_reason)), critical=False):
                    self._diagnostics_written = self.dropped
            except OSError:
                self._invalid("diagnostics_write")
        if now - self._last_summary_at >= 60:
            if self._pending_dropped:
                print(f"[Moments] dropped={self._pending_dropped} by_reason={dict(self._pending_by_reason)}", flush=True)
                self._pending_dropped = 0
                self._pending_by_reason.clear()
            self._last_summary_at = now

    def tick(self, run_dir, enqueue, *, now, help_open=False, status_pending=False, defer_if_busy=False,
             session_over=False):
        if not self.config.get("live7_moments_enabled", False):
            return
        if self._diagnostics_dir != run_dir:
            self._diagnostics_written = -1
        self._diagnostics_dir = run_dir
        if session_over:
            # Consume leftover files so they cannot fire after a later voice gap (K16).
            if self._held is not None:
                self._note_dropped(1, "status_pending")
            self._held = None
            help_open = True
            status_pending = True
        if defer_if_busy and not self.can_deliver(now, help_open=help_open):
            self._deliver_held(enqueue, now=now, help_open=help_open, status_pending=status_pending)
            self._summary(now)
            return
        newest, newest_held, accepted = None, None, False
        merged_count = 0
        try:
            paths = list(Path(run_dir).glob("ev-*.json")) if run_dir is not None else ()
        except OSError:
            self._invalid("list")
            paths = ()
        for path in paths:
            try:
                if now - path.stat().st_mtime < 0.2:
                    continue
            except OSError:
                continue
            moment = None
            reason = None
            try:
                with ipc.open_shared(path, "rb") as handle:
                    payload = handle.read(MAX_JSON_BYTES + 1)
                # Test mode has a durable generic consumer. Never race it through
                # this legacy, deliberately lossy newest-moment path. Live unchanged.
                if (self.config.get("bridge_test_mode")
                        and json.loads(payload).get("schema") in ("arti.game_event/1", "arti.stardew_moment/1")):
                    continue
                name = _FILENAME.fullmatch(path.name)
                if name is None:
                    raise ValueError("filename")
                ts, ttl, kind, text = _decode(payload)
                if self.config.get("live7_own_play_enabled", False):
                    data = json.loads(payload)
                    facts = data.get("facts") or {}
                    intent = facts.get("kind") == "own_intent"
                    reactive = bool(self.config.get("live7_reactive_play_enabled"))
                    direct = natural_intent(facts.get("text")) if intent and not reactive else None
                    if intent and not direct and not reactive:
                        raise ValueError("mechanical_intent")
                    if data.get("importance", "high") != "high":
                        raise ValueError("not_notable")
                    play_facts = facts if reactive and facts.get("reactive_play") else None
                    if reactive and play_facts is None:
                        play_facts = dict(kind=facts.get("kind", kind), outcome=[str(text)])
                    text = OwnPlayText(text, created_at=ts, direct_speech=direct, facts=play_facts)
                    from arti_runtime.attention_desk import native_salience
                    text.game_salience = native_salience(facts if data.get("schema") == "arti.game_event/1" else {"kind": kind})
                    text.game_event_kind = facts.get("kind") if data.get("schema") == "arti.game_event/1" else kind
                    text.mark_voiced = self.note_voiced
                    # Own thoughts are never held across a busy lane or a stale outcome.
                    ttl = min(ttl, 20.0)
                    kind = "own_play"
                if ts >= self._started_at and now <= ts + ttl:
                    moment = ((ts, int(name[1]), int(name[2])), kind, text)
                else:
                    reason = "stale_ttl"
            except ValueError as exc:
                # Only fixed validation reason codes, never payload/exception text.
                self._invalid(str(exc))
                reason = str(exc)
            except OSError:
                self._invalid("read")
                reason = "read"
            consumed = self._consume(path)
            if reason is not None:
                self._note_dropped(1, reason)
            if consumed and moment is not None:
                if moment[1] in _HELD_KINDS and not self.config.get("live7_reactive_play_enabled"):
                    if newest_held is not None:
                        self._note_dropped(1, "bed_superseded")
                    if newest_held is None or moment[0] > newest_held[0]:
                        newest_held = moment
                else:
                    if newest is not None:
                        if self.config.get("live7_reactive_play_enabled"):
                            merged_count += 1
                        else:
                            self._note_dropped(1, "tick_newest")
                    if newest is None or moment[0] > newest[0]:
                        newest = moment
            elif not consumed and moment is not None:
                self._note_dropped(1, "consume")
        if newest is not None and merged_count:
            old = newest[2]
            extra = f"dan {merged_count} hal lain"
            if isinstance(old, OwnPlayText):
                facts = dict(old.play_facts, outcome=list(old.play_facts.get("outcome", ())) + [extra])
                text = OwnPlayText(old, created_at=old.created_at, facts=facts)
                for attr in ("game_salience", "game_event_kind", "mark_voiced"):
                    setattr(text, attr, getattr(old, attr))
            else:
                text = MomentText(str(old) + "; " + extra)
            newest = (newest[0], newest[1], text)
        if newest_held is not None and not session_over and self.config.get("live7_moments_enabled", False):
            if self._held is not None:
                self._note_dropped(1, "bed_superseded")  # superseded: bed is voiced once
            self._held = (newest_held[0], newest_held[1], newest_held[2], now + _HOLD_MAX_SEC)
        elif newest_held is not None:
            self._note_dropped(1, "status_pending")
        self._deliver_held(enqueue, now=now, help_open=help_open, status_pending=status_pending)
        if newest is not None and self.config.get("live7_moments_enabled", False) and not status_pending:
            reason = None
            try:
                if self.can_deliver(now, help_open=help_open):
                    accepted = bool(enqueue(newest[2], trigger_type="own_play" if newest[1] == "own_play" else "game"))
                    if not accepted:
                        reason = "queue_refused_" + ("own_play" if newest[1] == "own_play" else "game")
                else:
                    reason = self._gate_reason(now, help_open)
            except Exception:
                # Enqueue and advisory predicates fail closed; consumed = no retry.
                reason = "busy"
            if accepted:
                if not self.config.get("attention_desk_enabled"):
                    self._last_voiced_at = now  # Desk pacing is committed on actual voice only.
                self.voiced += 1
                print(f"[Moments] voiced kind={newest[1]}", flush=True)
            else:
                self._note_dropped(1 + merged_count, reason or "busy")
        elif newest is not None:
            self._note_dropped(1 + merged_count, "status_pending")
        self._summary(now)
