"""Opt-in Live7 help admission and advisory hints; no device/network imports.

``spoken`` records acceptance by the injected voice queue, not proof of audio.
Requests receive one terminal reply at owner hint, window close or expiry.
Runtime/queue/file callbacks run outside the state lock. Failed reply delivery
stays in an outbox for the next tick, without speaking the request again.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import logging
import math
from pathlib import Path
import re
import threading
import time

from arti_help_bus import MAX_HINTS, MAX_SAID, FileHelpChannel, HelpHint, HelpReply, HelpRequest, sanitize_hint_text
from arti_live7_moments import MomentsRelay
from arti_live7_status_facts import StatusFactsRelay
import arti_speech_censor

_LOG = logging.getLogger(__name__)
_COMMAND = re.compile(r"^\s*!(?:bantu|tolong)(?=\s|$)", re.IGNORECASE)
_TAGS = re.compile(r"\[[^\]]*\]|<[^>]*>")
HELP_TRIGGER_PREFIX = "[Stardew] Aku buntu:"


@dataclass
class _Window:
    request: HelpRequest
    closes_at: float
    spoken: bool = False
    ready: bool = False
    closing: bool = False
    hints: list[HelpHint] = field(default_factory=list)
    viewers: set[str] = field(default_factory=set)


class HelpRelay:
    def __init__(
        self, config, enqueue, *, runtime=None, file_channel: FileHelpChannel | None = None,
        reply_sink=None, clock=time.time, thread_factory=threading.Thread,
        channel_resolver=None, resolve_interval_sec: float = 10.0,
        viewer_waiting=None, game_trigger_waiting=None, status_voice_ready=None,
    ):
        self.config = config
        from arti_runtime.coop_session import CoopSession
        self._session_facts = CoopSession(config)
        self._enqueue = enqueue
        self._runtime = runtime
        self._channel = file_channel
        # Optional callable -> FileHelpChannel | None, re-run every interval
        # (e.g. follow the active Live7 cycle's run dir).
        self._channel_resolver = channel_resolver
        self._resolve_interval = max(0.0, float(resolve_interval_sec))
        self._resolved_at = None
        # request_id -> channel it was read from; its reply goes back there.
        self._request_channels = {}
        self._reply_sink = reply_sink
        self._clock = clock
        self._thread_factory = thread_factory
        self._lock = threading.RLock()
        self._delivery_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._stopped = False
        self._window = None
        self._said = []
        self._recent = deque()
        self._last_admitted_at = None
        self._dedupe = set()
        self._decisions = {}
        self._from_file = set()
        self._outbox = deque()
        self._last_priority_at = None
        self._notes_cycle = None
        self._game_stopped = False
        self._moments = MomentsRelay(config, started_at=clock(), viewer_waiting=viewer_waiting,
                                     game_trigger_waiting=game_trigger_waiting)
        self._status_facts = StatusFactsRelay(config, self._moments, clock=clock,
                                              voice_ready=status_voice_ready,
                                              extra_session_facts=lambda: self._session_facts.notes,
                                              on_host_left=self._session_facts.clear_for_host_left)
        # Mabar: what the HOST watches while she is not in it (arti_runtime/host_story.py). Off unless configured.
        # Never offer the solo relay in a run a person hosts, before she joined or after she left.
        self._host_story = None
        self._host_story_ready = status_voice_ready or (lambda: True)
        if config.get("host_story_relay_file"):
            from arti_runtime.human_host import is_owner_hosted_run
            relay_path = Path(config["host_story_relay_file"])
            if not is_owner_hosted_run(relay_path.parent):
                import arti_mind
                from arti_runtime.host_story import HostStoryRelay
                self._host_story = HostStoryRelay(config["host_story_relay_file"], mind_base=arti_mind.mind_dir(config),
                                                  clock=clock, stopped=self._host_story_stopped)
                self._host_story.owner_name = str(config.get("streamer_name") or "Bohan")

    def status_block(self):
        return self._status_facts.status_block()

    def reply_facts(self, *, include_host_story=True):
        """Refresh story at reply preparation, independently of moment delivery."""
        if not include_host_story:
            return self._session_facts.block()
        try:
            story = self._host_story.reply_context() if self._host_story is not None else ""
        except (OSError, ValueError, TypeError, KeyError) as exc:
            _LOG.warning("Host story context read failed (%s)", type(exc).__name__)
            story = ""  # keep the human reply; source remains available for the next read
        block = self._session_facts.block() + story
        return story.with_text(block) if getattr(story, "_host_story_guarded", False) else block

    def note_session_owner(self, text):
        self._session_facts.note_owner(text)

    def remember_session_fact(self, fact):
        """Keep a model-chosen [FAKTA_SESI] fact for the rest of this process."""
        self._session_facts.remember(fact)

    def game_session_stopped(self):
        """Memory-only: the worker tick sets this. Voice path must not stat STOP files."""
        return self._game_stopped

    def _compute_session_over(self):
        """Worker-only: may stat LIVE7_STOP / WATCH_STOP on the current cycle."""
        from arti_live7_status_facts import cycle_stop_present
        resolver = self._channel_resolver
        cycle = getattr(resolver, "cycle_dir", None) if resolver is not None else None
        if cycle_stop_present(cycle):
            return True
        # cycle_dir is retained after the last Live7 cycle drops out of active
        # discovery. Static fallback with cycle_dir still None is not a stop.
        return bool(cycle is not None and resolver is not None
                    and not getattr(resolver, "has_active_cycle", True))

    def _host_story_stopped(self):
        from arti_stardew_stop import read_stop

        # WATCH_STOP ends the farmhand's watcher even on normal exit. A
        # host_left sentinel also drains only that leg; native HOST presence
        # decides whether the owner is still playing.
        from arti_runtime.human_host import is_owner_hosted_run
        run = self._host_story.path.parent
        if self._stopped or is_owner_hosted_run(run) or (run / "bridge-stop-request").exists():
            return True
        paths = [run / "runtime" / "LIVE7_STOP"]
        cycle = getattr(self._channel_resolver, "cycle_dir", None)
        if cycle is not None:
            cycle = Path(cycle)
            paths.extend((cycle / "OPERATOR_STOP", cycle / "LIVE7_STOP"))
        for path in paths:
            try:
                raw, stop = read_stop(path)
            except (OSError, ValueError):
                return True
            if raw is not None and not (path.name == "LIVE7_STOP" and isinstance(stop, dict)
                                        and stop.get("writer") == "supervisor.host_left"):
                return True
        return False

    def event_dir(self):
        """Resolved generic event inbox, shared with the text-only discovery consumer."""
        return getattr(self._channel, "help_dir", None)

    def help_open(self) -> bool:
        """A help window is open. Lock-free read: safe on the bridge main loop."""
        return self._window is not None

    def set_runtime(self, runtime) -> None:
        """Point in-process hint delivery at the current Stardew runtime (or None)."""
        with self._lock:
            self._runtime = runtime

    def _reply(self, request, status, spoken=False, hints=()):
        return HelpReply(request_id=request.request_id, status=status, spoken=spoken, hints=tuple(hints))

    def _finish_locked(self, now):
        window = self._window
        if window is None:
            return
        window.closing = True
        if not window.ready:
            return
        status = "hinted" if window.hints else (
            "expired" if now >= window.request.expires_at else "delivered"
        )
        reply = self._reply(window.request, status, window.spoken, window.hints)
        self._decisions[reply.request_id] = reply
        self._outbox.append(reply)
        self._window = None

    def _advance_locked(self, now):
        if self._window is not None and now >= self._window.closes_at:
            self._finish_locked(now)

    def _flush(self):
        # One sender owns the outbox, but holds no state lock while calling
        # the runtime (whose own callback can submit a new request).
        if not self._delivery_lock.acquire(blocking=False):
            return
        try:
            while True:
                with self._lock:
                    if not self._outbox:
                        return
                    reply = self._outbox[0]
                    from_file = reply.request_id in self._from_file
                    channel = self._request_channels.get(reply.request_id, self._channel)
                try:
                    accept = getattr(self._runtime, "accept_help_hint", None)
                    if self._reply_sink is not None:
                        self._reply_sink(reply)
                    elif channel is not None and (from_file or not callable(accept)):
                        channel.write_reply(reply)
                    elif callable(accept):
                        accept(reply.request_id, reply.hints)
                except Exception as exc:
                    # Never log request/hint/viewer text or exception messages.
                    _LOG.warning("Help reply delivery failed (%s)", type(exc).__name__)
                    return
                with self._lock:
                    self._outbox.popleft()
                    self._request_channels.pop(reply.request_id, None)
        finally:
            self._delivery_lock.release()

    def submit(self, request: HelpRequest) -> HelpReply:
        """Return the admission decision; duplicate IDs never speak twice."""
        return self._submit(request, self._clock())

    def _submit(self, request, now, *, from_file=False, channel=None):
        if type(request) is not HelpRequest:
            raise ValueError("expected HelpRequest")
        with self._lock:
            if from_file:
                self._from_file.add(request.request_id)
                if channel is not None and request.request_id not in self._decisions:
                    self._request_channels[request.request_id] = channel
            self._advance_locked(now)
            previous = self._decisions.get(request.request_id)
            if previous is not None:
                return previous
            while self._recent and self._recent[0] <= now - 3600:
                self._recent.popleft()
            key = (request.session_id, request.reason, request.game_date)
            gap = max(0.0, float(self.config.get("live7_help_min_gap_sec", 900)))
            maximum = max(0, int(self.config.get("live7_help_max_per_hour", 3)))
            if now >= request.expires_at:
                decision = self._reply(request, "expired")
            elif (
                self._stopped or key in self._dedupe or self._window is not None
                or len(self._recent) >= maximum
                or (self._last_admitted_at is not None and now - self._last_admitted_at < gap)
            ):
                decision = self._reply(request, "suppressed")
            else:
                self._dedupe.add(key)
                self._recent.append(now)
                self._last_admitted_at = now
                duration = max(0.0, float(self.config.get("live7_help_window_sec", 180)))
                window = _Window(request, min(now + duration, request.expires_at))
                self._window = window
                decision = self._reply(request, "delivered")
            self._decisions[request.request_id] = decision
            if decision.status != "delivered":
                self._outbox.append(decision)
        if decision.status == "delivered":
            spoken = False
            if self.config.get("live7_help_speak_enabled", True):
                reason = sanitize_hint_text(request.reason.replace("_", " "))
                location = sanitize_hint_text(request.location or "lokasi belum diketahui")
                detail = sanitize_hint_text(request.detail)
                text = (
                    f"{HELP_TRIGGER_PREFIX} {reason} di {location}, {detail}. "
                    "Minta tolong singkat ke Bohan/penonton pakai kata-katamu sendiri, santai kayak "
                    "streamer yang lagi buntu (jangan bacain kode atau angka teknis di atas); "
                    "penonton boleh ketik !bantu <saran>."
                )
                try:
                    spoken = bool(self._enqueue(text, trigger_type="game"))
                except Exception as exc:
                    _LOG.warning("Help voice enqueue failed (%s)", type(exc).__name__)
            with self._lock:
                window.spoken = spoken
                window.ready = True
                # Start collection after enqueue delivery; never extend TTL.
                window.closes_at = min(self._clock() + duration, request.expires_at)
                decision = replace(decision, spoken=spoken)
                self._decisions[request.request_id] = decision
                if window.closing or self._clock() >= window.closes_at:
                    self._finish_locked(self._clock())
        self._flush()
        return decision

    def note_priority_turn(self):
        """Owner/donation ingress invalidates older queued help speech."""
        with self._lock:
            self._last_priority_at = self._clock()

    def speech_is_current(self, queued_at):
        with self._lock:
            return not self._stopped and (
                self._last_priority_at is None or queued_at > self._last_priority_at
            )

    def _hint_text(self, text, *, viewer=False):
        text = sanitize_hint_text(_TAGS.sub(" ", text))
        if viewer:
            text = arti_speech_censor.censor_from_config(text, self.config)
        return sanitize_hint_text(_TAGS.sub(" ", text))

    def _said_owner_name(self):
        name = str(self.config.get("streamer_name") or "").strip()
        if not name:
            name = str(getattr(self._host_story, "owner_name", "") or "").strip()
        return (name or "owner")[:40]

    def note_said(self, who, platform, text, *, viewer=False, now=None):
        """Last three spoken lines for the game page; independent of a help window."""
        try:
            text = self._hint_text("" if text is None else str(text), viewer=viewer)[:140]
        except (TypeError, ValueError):
            return
        if not text:
            return
        who = " ".join(str(who if who is not None else "").split())[:40]
        platform = " ".join(str(platform if platform is not None else "").split())[:24]
        if not who or not platform:
            return
        try:
            at = float(self._clock() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(at) or at < 0:
            return
        row = dict(kind="said_to_you", who=who, platform=platform, text=text, at=at)
        with self._lock:
            rows = list(self._said)
            replaced = False
            for index in range(len(rows) - 1, -1, -1):
                if rows[index]["who"] == who and rows[index]["platform"] == platform:
                    if at - rows[index]["at"] < 20.0:
                        rows[index] = row
                        replaced = True
                    break
            if not replaced:
                rows.append(row)
            self._said = rows[-MAX_SAID:]
            snapshot = list(self._said)
            channel = self._channel
        writer = getattr(channel, "write_said", None)
        if not callable(writer):
            return
        try:
            writer(snapshot)
        except Exception as exc:
            _LOG.warning("Help said write failed (%s)", type(exc).__name__)

    def note_owner_text(self, text):
        self.note_said(self._said_owner_name(), "mic", text)
        self.note_session_owner(text)
        now = self._clock()
        with self._lock:
            self._last_priority_at = now
            self._advance_locked(now)
            window = self._window
            if window is not None and not window.closing:
                hint = self._hint_text(text)
                if hint:
                    window.hints.append(HelpHint("owner", hint, now))
                    self._finish_locked(now)
        self._flush()

    def note_viewer_command(self, viewer, text, is_bot=False) -> bool:
        match = _COMMAND.match(text)
        if match is None:
            return False
        now = self._clock()
        with self._lock:
            self._advance_locked(now)
            window = self._window
            consumed = window is not None and not window.closing
            if consumed and not is_bot:
                identity = viewer.strip().lstrip("@").casefold()
                maximum = min(MAX_HINTS - 1, max(0, int(self.config.get("live7_help_viewer_hints", 5))))
                if identity and identity not in window.viewers and len(window.viewers) < maximum:
                    hint = self._hint_text(text[match.end():], viewer=True)
                    if hint:
                        window.viewers.add(identity)
                        window.hints.append(HelpHint("viewer", hint, now))
        self._flush()
        return consumed

    def _maybe_resolve_channel(self, now):
        """Re-run the optional channel resolver at most once per interval."""
        if self._channel_resolver is None:
            return
        with self._lock:
            last = self._resolved_at
            if last is not None and last <= now < last + self._resolve_interval:
                return
            self._resolved_at = now
        try:
            channel = self._channel_resolver()
        except Exception as exc:
            _LOG.warning("Help channel resolve failed (%s)", type(exc).__name__)
            return
        with self._lock:
            current = self._channel
            old = getattr(current, "help_dir", current)
            new = getattr(channel, "help_dir", channel)
            if old != new:
                self._channel = channel

    def tick(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            self._advance_locked(now)
            stopped = self._stopped
        if not stopped:
            self._maybe_resolve_channel(now)
        channel = self._channel
        if channel is not None and not stopped and self.config.get("live7_help_enabled", True):
            try:
                requests = channel.pending_requests()
            except (OSError, ValueError) as exc:
                _LOG.warning("Help request poll failed (%s)", type(exc).__name__)
            else:
                for request in requests:
                    self._submit(request, now, from_file=True, channel=channel)
        if not stopped:
            with self._lock:
                help_open = self._window is not None
            cycle_dir = getattr(self._channel_resolver, "cycle_dir", None)
            # Owner corrections last for the whole bridge process: a rejoin or a new
            # Live7 cycle mid-stream is still the same session for the owner.
            if cycle_dir is not None:
                self._notes_cycle = cycle_dir
            session_over = self._compute_session_over()
            self._game_stopped = session_over
            status_pending = False
            try:
                self._status_facts.config = self.config
                status_pending = self._status_facts.tick(
                    cycle_dir, self._enqueue,
                    now=now, help_open=help_open, defer_low_priority=True,
                    session_over=session_over)
            except Exception as exc:
                _LOG.warning("Status facts tick failed (%s)", type(exc).__name__)
            try:  # moments must never block help replies (REVIEW_MOMENTS F2)
                self._moments.config = self.config
                self._moments.tick(getattr(channel, "help_dir", None), self._enqueue,
                                   now=now, help_open=help_open, status_pending=status_pending,
                                   defer_if_busy=not status_pending and self._status_facts.low_priority_pending(),
                                   session_over=session_over)
                if not session_over and not status_pending:
                    self._status_facts.deliver(self._enqueue, now=now, help_open=help_open)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("Moments tick failed (%s)", type(exc).__name__)
            if self._host_story is not None:
                try:
                    self._host_story.tick(self._enqueue, ready=lambda: not help_open and self._host_story_ready())
                except Exception as exc:  # noqa: BLE001 - never blocks help replies
                    _LOG.warning("Host story tick failed (%s)", type(exc).__name__)
        self._flush()

    def _poll(self):
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                _LOG.warning("Help relay tick failed (%s)", type(exc).__name__)
            self._stop_event.wait(1.0)

    def start(self):
        # Tick also closes in-process windows, even without a file channel.
        with self._lock:
            if self._thread is not None or self._stopped:
                return
            self._thread = self._thread_factory(target=self._poll, name="live7-help", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stopped = True
            self._stop_event.set()
            thread = self._thread
            self._finish_locked(self._clock())
        self._status_facts.stop()
        if self._host_story is not None:
            self._host_story.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._flush()


class Live7HelpChannelResolver:
    """``channel_resolver`` for HelpRelay: the newest active Live7 cycle's run dir.

    Falls back to the static configured dir (or None) when no cycle is active.
    Announces each change of the resolved dir once. Never raises.
    """

    def __init__(self, config, static_dir="", *, clock=time.time, active_cycles=None, announce=None):
        self._config = config
        self._static = str(static_dir or "").strip()
        self._clock = clock
        self._active_cycles = active_cycles
        self._announce = announce or (lambda line: print(line, flush=True))
        self._last = None
        self._last_multi = None
        self.has_active_cycle = False
        # Retain the last selected cycle after it stops being "active", so the
        # worker can drain its terminal event and watch its STOP resolution.
        self.cycle_dir = None

    def _say(self, line):
        try:
            self._announce(line)
        except Exception:  # noqa: BLE001 — logging must not block resolution
            pass

    def __call__(self):
        try:
            active = self._active_cycles
            if active is None:
                import arti_stardew_driver_guard
                active = arti_stardew_driver_guard.active_live7_cycles
            cycles = list(active(self._config, now=self._clock()) or ())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Live7 cycle lookup failed (%s)", type(exc).__name__)
            cycles = []
        self.has_active_cycle = bool(cycles)
        if cycles:
            chosen = max(cycles, key=lambda c: c.mtime)
            self.cycle_dir = chosen.cycle_dir
            multi = tuple(sorted(str(c.cycle_dir) for c in cycles)) if len(cycles) > 1 else None
            if multi is not None and multi != self._last_multi:
                self._say(f"[Help] {len(cycles)} Live7 cycles active; using newest "
                          f"{chosen.cycle_dir} (cycle_state.json mtime)")
            self._last_multi = multi
            target, label = str(chosen.run_dir), f"cycle {chosen.cycle_dir.name}"
        elif self._static:
            target, label = self._static, "static fallback, no active Live7 cycle"
        else:
            target, label = None, "no active Live7 cycle, no static dir"
        if (target, label) != self._last:
            self._last = (target, label)
            self._say(f"[Help] file channel → {target or 'off'} ({label})")
        return FileHelpChannel(target) if target else None
