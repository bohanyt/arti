"""Observed game-pause time, separate from transport and physical safety time.

Only fresh native state can suspend gameplay. Missing samples never grant an
unbounded exemption to a crashed/disconnected game. No OS clock is patched.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

import threading
import time
from contextlib import contextmanager


def game_now(runtime):
    clock = getattr(runtime, "_game_now", None)
    return clock() if clock else time.monotonic()


def wait_resume(runtime):
    wait = getattr(runtime, "_wait_host_resume", None)
    if wait:
        wait()


@contextmanager
def action_wait(runtime):
    """Scope receipt timing without replacing the authorized command adapter."""
    from arti_stardew_command import result_wait_clock
    token = result_wait_clock.set(runtime._action_wait_now)
    try:
        yield
    finally:
        result_wait_clock.reset(token)


class PauseClock:
    FRESH_S = 0.5

    def __init__(self):
        self._lock = threading.RLock()
        self._at = None
        self._paused = False
        self._total = 0.0

    def observe(self, paused, now):
        with self._lock:
            if self._at is not None and self._paused:
                self._total += min(max(0.0, now - self._at), self.FRESH_S)
            self._at, self._paused = now, paused is True

    def seconds(self, now):
        with self._lock:
            tail = (min(max(0.0, now - self._at), self.FRESH_S)
                    if self._at is not None and self._paused else 0.0)
            return self._total + tail


class StallClock:
    """ADR 128: seconds the machine stalled the game, from the native state feed alone. a54 d23: 86 s of the 607 s no-progress
    window ran at normal speed; for the rest the HOST drew 0.5 frames a second. Two measures, never added together: no state
    arrived, or states arrive and the host's clock does not move. A silent gap counts up to GAP_MAX_S, so a game that crashed
    does not hide behind this clock; a clock paused, on a festival map or past the last hour never counts."""
    FEED_S = 2.0  # the feed normally arrives several times a second (motor freshness is 0.5 s)
    CLOCK_S = 30.0  # ten game minutes pass in about 7 s
    GAP_MAX_S = 300.0

    def __init__(self):
        self._lock = threading.RLock()
        self._at = None
        self._clock = None
        self._clock_at = None
        self._total = 0.0

    def observe(self, now, clock, running):
        with self._lock:
            gap = None if self._at is None else max(0.0, now - self._at)
            if gap is not None and gap > self.FEED_S:
                self._total += min(gap, self.GAP_MAX_S)
            elif (gap is not None and running and clock == self._clock and self._clock_at is not None
                  and now - self._clock_at > self.CLOCK_S):
                self._total += gap
            if clock != self._clock or not running:
                self._clock, self._clock_at = clock, now
            self._at = now

    def seconds(self, now):
        with self._lock:
            quiet = 0.0 if self._at is None else max(0.0, now - self._at)
            return self._total + (min(quiet, self.GAP_MAX_S) if quiet > self.FEED_S else 0.0)


class HostPauseRuntimeMixin:
    def _pause_player(self):
        pick = getattr(self._state, "pilih_target", None)
        return pick() if pick else None

    def _paused_seconds(self):
        pause = getattr(self._state, "paused_seconds", None)
        return pause(self._pause_player()) if pause else 0.0

    def _stall_seconds(self):
        """ADR 128: StallClock's reading; only the progress watchdog spends it."""
        stall = getattr(self._state, "stall_seconds", None)
        return stall(self._pause_player()) if stall else 0.0

    def _game_now(self):
        """Gameplay elapsed time; freshness/leases/STOP use ordinary monotonic."""
        return time.monotonic() - self._paused_seconds()

    def _action_wait_now(self):
        if self._worker_stop.is_set() or self._action_cancel.is_set():
            return time.monotonic()  # cancellation immediately ends a suspended receipt wait
        return self._game_now()

    def _host_paused(self):
        check = getattr(self._state, "game_paused", None)
        return bool(check and check(self._pause_player()))

    def _pause_boundary(self):
        self._sync_pause_deadlines()
        # Poll the existing emergency stop even when every gameplay rule waits.
        if self._live7_active() and self.live7_stop_file_check():
            return True
        # A native pause cannot conceal a changed runtime/session/actor. Reuse
        # the existing pinned identity and authoritative ui.context product;
        # no menu/free-move inference is used to grant this exemption.
        with self._lock:
            goal = getattr(self, "_active", None)
            expected = getattr(goal, "play_identity", None)
            campaign = getattr(getattr(self, "_skill_session", None), "campaign", None)
            expected = expected or getattr(campaign, "epoch", None)
            if (expected and self._status in ("READY", "PAUSED_RECOVERY") and self._host_paused()):
                rec = self._obs.get(self._pause_player(), "ui.context")
                if rec is not None and rec.is_complete and rec.authoritative:
                    facts = rec.facts
                    actual = (facts.get("runtime_instance_id"), facts.get("session_id"), rec.session_id,
                              rec.player_id, rec.screen_id)
                    if tuple(expected) != actual:
                        self._cancel_action("native_identity_changed")
                        self._status = "FAIL_CLOSED"
                        self._journal(dict(ev="pause_identity_mismatch", reason="native_identity_changed"))
                        return False
        action = getattr(getattr(self, "_active", None), "last_action", None)
        if (getattr(action, "cleanup_confirmed", None) is False
                or getattr(action, "lifecycle", None) in ("uncertain", "interrupted")):
            return False
        return (self._status in ("READY", "PAUSED_RECOVERY") and self._host_paused()
                and not self._worker_stop.is_set()
                and not getattr(self, "_owner_play_stopped", False)
                and not getattr(self, "_gather_mutation_unresolved", False))

    def _sync_pause_deadlines(self):
        # Approach sensor timestamps use real monotonic throughout their proof
        # protocol. Extend that one absolute deadline instead of mixing clocks.
        with self._lock:
            goal = getattr(self, "_active", None)
            if goal is None:
                return
            total = self._paused_seconds()
            previous = getattr(goal, "approach_pause_seconds", total)
            if getattr(goal, "approach_deadline", 0) and total > previous:
                goal.approach_deadline += total - previous
                goal.approach_pause_replan = True
            if getattr(goal, "approach_pause_replan", False) and not self._action_busy:
                goal.approach_target = None  # replan from fresh pose; retain step/input caps
                goal.approach_pause_replan = False
            goal.approach_pause_seconds = total

    def _wait_host_resume(self):
        """Read-only; Condition releases the runtime lock for receiver/STOP.

        Physical release/containment never calls this wait. Stale pause facts
        fall through to the existing freshness and fail-closed checks.
        """
        with self._worker_wakeup:
            while self._pause_boundary():
                W.call('poll', 112, self._worker_wakeup.wait, timeout=0.1)

    def _note_host_pause(self):
        paused = self._host_paused()
        previous = getattr(self, "_host_pause_noted", False)
        now = time.monotonic()
        if paused != previous or paused and now - getattr(self, "_host_pause_fact_at", -1e9) >= 1:
            self._host_pause_fact_at = now
            self._host_pause_noted = paused
            snap = self._state.latest(self._pause_player())
            fresh_resume = (snap is not None and not self._state.is_stale(snap.player_id)
                            and snap.paused is False and snap.host_paused is False)
            self._journal(dict(ev="game_paused" if paused else "game_resumed" if fresh_resume
                               else "game_pause_unavailable",
                               at=time.time(),
                               reason="native_host_pause", fact="game dijeda oleh Bohan" if paused
                               else "game dilanjutkan oleh Bohan" if fresh_resume else "status jeda game belum diketahui"))
