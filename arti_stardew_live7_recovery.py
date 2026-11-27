"""Live7 recovery accounting and progress deadline (owner policy 2026-10-26).

Recovery never certifies an effect, replays an input, or grants menu ownership.
The existing identity, mutation, lease and native menu executors remain authority.
"""
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import random
import threading
import time
from types import SimpleNamespace

from arti_stardew_pause import game_now

NO_PROGRESS_SECONDS = 600
NO_PROGRESS_RECOVER_SECONDS = 60
NATIVE_PAGE_FRESH_SECONDS = 20
NATIVE_PAGE_DEFER_MAX_SECONDS = 90  # ADR 95 (FIX64 #4): fresh pages defer the soft recovery at most this long per run
STALL_HOLD_MAX_S = 1200  # ADR 128: one no-progress window sets aside at most ADR 123's 20 minutes of machine stall
STALL_ENDED_QUIET_S = 30  # this long without new stall seconds ends one journaled stall
RECOVERY_REPEAT_LIMIT = 3
CHOICE_RETRY_DELAYS = (2.0, 5.0, 15.0)
# fix57 (ADR 83): seconds between next-skill asks after consecutive transient provider failures, capped at the last.
# A provider failure is never her choice, so it never spends her unusable-choice cap; the 600 s deadline still runs.
PROVIDER_RETRY_DELAYS = (2.0, 5.0, 15.0, 30.0, 60.0)
PROVIDER_RETRY_JITTER = 0.2  # +-20 %, so asks never lock step with a periodic provider stall
PROVIDER_LANE_POLL = 1.0  # a lane still owned by a logically timed-out call: look again, never ask into it
PROVIDER_ALERT_AFTER = 3  # consecutive failures that reached the provider: one named health FAULT per streak
PROVIDER_WEDGED_SECONDS = 90.0  # a lane owned this long past its logical timeout: recycle that app-server once
# Exception codes that a later ask cannot fix (a quota, a login, admission). They keep the pre-fix57 path.
PROVIDER_PERMANENT = frozenset({"provider_autonomous_inference_off", "provider_hourly_limit", "provider_session_limit",
    "provider_budget_exhausted", "provider_closed", "codex_chatgpt_subscription_required", "codex_luna_effort_unavailable",
    "STARDew_CODEX_DEDICATED_LOGIN_REQUIRED", "codex_disabled_under_pytest"})


def native_page_reading(state, now=None):
    """ADR 95 (FIX64 #4): a fresh native page defers the soft recovery, but never past NATIVE_PAGE_DEFER_MAX_SECONDS
    from the first refresh of the run, so a stuck page whose text keeps changing cannot hide from recovery."""
    now = time.monotonic() if now is None else now
    page_at, first_at = state.get("native_ui_page_at"), state.get("native_ui_page_first_at", state.get("native_ui_page_at"))
    return (type(page_at) is float and now - page_at < NATIVE_PAGE_FRESH_SECONDS
            and not (type(first_at) is float and now - first_at >= NATIVE_PAGE_DEFER_MAX_SECONDS))


@dataclass
class ChoiceRetryBudget:
    """Read-only chooser attempts for one pinned question; no input/release authority."""
    attempts: int = 0
    next_at: float = 0.0

    def ready(self, now):
        return self.attempts < len(CHOICE_RETRY_DELAYS) and now >= self.next_at

    def exhausted(self, now):
        """Every re-offer was spent and its backoff elapsed: only a recovery close/decline remains."""
        return self.attempts >= len(CHOICE_RETRY_DELAYS) and now >= self.next_at

    def start(self):
        self.attempts += 1

    def failed(self, now):
        self.next_at = now + CHOICE_RETRY_DELAYS[min(self.attempts, len(CHOICE_RETRY_DELAYS))-1]

    def restart(self, now):
        """ADR 128: the re-offers are spent and the question is still hers; ask again after the longest backoff."""
        self.attempts = 0
        self.next_at = now + CHOICE_RETRY_DELAYS[-1]

    def refund(self, now):
        """fix56B (ADR 82): the ask was refused before it reached her (provider lane busy); it spends no re-offer."""
        self.attempts = max(0, self.attempts - 1)
        self.next_at = now + CHOICE_RETRY_DELAYS[0]


@dataclass
class ProviderBackoff:
    """fix57: the next-skill ask's provider health. Read-only: it only delays the next ask, never picks for her."""
    failures: int = 0
    next_at: float = 0.0
    codes: list = field(default_factory=list)
    alerted: bool = False
    recycled: bool = False
    busy_since: float | None = None

    def ready(self, now):
        return now >= self.next_at

    def failed(self, now, code, rng=None):
        self.failures += 1
        self.codes = (self.codes + [code])[-5:]
        base = PROVIDER_RETRY_DELAYS[min(self.failures, len(PROVIDER_RETRY_DELAYS)) - 1]
        self.next_at = now + base * (1 + PROVIDER_RETRY_JITTER * (2 * (rng or random.random)() - 1))
        return self.next_at - now

    def refused_busy(self, now):
        """Refused unsent (the lane is still owned): not a failure, spends nothing."""
        self.next_at = max(self.next_at, now + PROVIDER_LANE_POLL)


def provider_transient(error):
    """``(type, code)`` from the chooser's provider_error: True when a later ask can succeed (timeout, busy lane,
    transport). Render/budget errors (ValueError) and the PROVIDER_PERMANENT codes are not."""
    kind, code = error if isinstance(error, tuple) and len(error) == 2 else (None, None)
    return kind in ("TimeoutError", "RuntimeError", "OSError", "ConnectionError") and code not in PROVIDER_PERMANENT


def semantic_receipt(row) -> bool:
    """The harness's semantic-progress receipt (scripts/free_play/soak.py JournalRecorder), which the evaluator's
    longest_no_progress_gap_s is measured between: a VERIFIED live row whose cleanup left input released, the
    player free and no menu, or an observed world change with confirmed cleanup."""
    if not isinstance(row, dict):
        return False
    action = row.get("action") if isinstance(row.get("action"), dict) else {}
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    cleanup = evidence.get("cleanup") if isinstance(evidence.get("cleanup"), dict) else {}
    return ((row.get("ev") == "part_live_row" and row.get("outcome_class") == "VERIFIED"
             and cleanup.get("input_released") is True and cleanup.get("free") is True and cleanup.get("menu") is None)
            or (row.get("ev") == "action_result" and action.get("world_effect_status") == "observed_change"
                and action.get("cleanup_confirmed") is True))


@dataclass
class ProgressWatchdog:
    since: float
    signature: str | None = None
    completed: int = 0
    repeats: Counter = field(default_factory=Counter)
    recovery_attempted: bool = False
    receipts: int = 0
    physical: dict = field(default_factory=dict)
    date: tuple | None = None
    held: float = 0.0
    stall_seen: float | None = None

    def hold_stall(self, stalled):
        """ADR 128 (a54 d23 J675-J677): seconds the machine stalled the game do not count as seconds she made no progress.
        The window moves forward by the new stall seconds, up to STALL_HOLD_MAX_S. True no progress still reaches the
        bound: no state went quiet, the clock moved, and nothing here is credited as progress."""
        seen, self.stall_seen = self.stall_seen, stalled
        if seen is None:
            return 0.0
        delta = min(max(0.0, stalled - seen), STALL_HOLD_MAX_S - self.held)
        self.since += delta
        self.held += delta
        return delta

    def observe(self, now, signature=None, completed=0, receipts=None, date=None):
        # ADR 72 (a39: 7 no_progress_reobserve right at morning start): a native day change (the wake) is a new
        # baseline. The night, bedtime reflection and runner startup are not a stall of the new day.
        if date is not None:
            if self.date is not None and date != self.date:
                self.since = now
                self.held = 0.0
                self.repeats.clear()
                self.recovery_attempted = False
            self.date = date
        # A fresh initial observation establishes a baseline, not progress.
        changed = (signature is not None and self.signature is not None and signature != self.signature)
        # A semantic receipt journaled since the last boundary is progress (the evaluator's gap basis).
        if receipts is not None:
            changed = changed or receipts > self.receipts
            self.receipts = max(self.receipts, receipts)
        # Owner rule (10 min of no progress): only a changed world is progress. A run terminal is not; a12's
        # curfew terminal (J1063) reset the watch and the stop came at 18.9 min (J1309). `completed` is kept
        # for the readback only.
        if changed:
            self.since = now
            self.held = 0.0
            self.repeats.clear()
            self.recovery_attempted = False
        if signature is not None:
            self.signature = signature
        self.completed = max(self.completed, completed)
        return now - self.since >= NO_PROGRESS_SECONDS


class Live7RecoveryMixin:
    def _live7_provider_lane_busy(self):
        """fix56B: a logically timed-out provider call still runs physically; any new ask is refused unsent."""
        return getattr(getattr(self, "_provider", None), "physical_inflight", False) is True

    def _live7_lane_wait_note(self, session, receiver, trigger):
        noted = self.__dict__.setdefault("_live7_lane_wait_noted", set())
        if (session.session_id, receiver) not in noted:
            noted.add((session.session_id, receiver))
            self._journal(dict(ev="live7_question_wait_provider_lane", receiver=receiver, trigger=trigger))

    def _live7_provider_backoff(self, session):
        slot = self.__dict__.get("_live7_provider_slot")
        if slot is None or slot[0] != session.session_id:
            slot = self._live7_provider_slot = (session.session_id, ProviderBackoff())
        return slot[1]

    def _live7_provider_wait(self, session):
        """fix57 (a42 D2:J129-J153): True while a next-skill ask must not be sent. After a provider failure the ask
        waits its backoff; while a logically timed-out call still owns the lane, an ask would be refused unsent.
        Waiting is not her choice and not an activity. The 600 s progress deadline still runs."""
        b = self._live7_provider_backoff(session)
        now = game_now(self)
        if self._live7_provider_lane_busy():
            b.busy_since = now if b.busy_since is None else b.busy_since
            if now - b.busy_since >= PROVIDER_WEDGED_SECONDS:
                self._live7_provider_recycle(session, b, "lane_wedged", force=True)
            return True
        b.busy_since = None
        return not b.ready(now)

    def _live7_provider_result(self, session, selected):
        """Account one next-skill ask. True when it was a transient provider failure: then it is no choice of hers,
        never an implicit activity, and the next ask waits a jittered backoff. Anything she answered resets it."""
        b = self._live7_provider_backoff(session)
        if selected != "provider_error":
            if not (selected in ("invalid", "finish", "ask_owner") or hasattr(selected, "name")):
                return False  # superseded / none / trial limit: the provider's health is not known from it
            if b.failures:
                self._journal(dict(ev="live7_provider_recovered", failures=b.failures, codes=list(b.codes),
                                   recycled=b.recycled))
            self._live7_provider_slot = (session.session_id, ProviderBackoff())
            return False
        error = getattr(self, "_skill_last_provider_error", None)
        if not provider_transient(error):
            return False  # unchanged pre-fix57 path: a render/admission error is no provider hiccup
        now, code = game_now(self), error[1]
        if code == "provider_physical_inflight" or self._live7_provider_lane_busy():
            b.refused_busy(now)
            self._journal(dict(ev="live7_provider_retry", code=code, refused_unsent=True, count=b.failures,
                               chooser="none", activity=None))
            return True
        delay = b.failed(now, code)
        self._journal(dict(ev="live7_provider_retry", code=code, count=b.failures, retry_in_s=round(delay, 2),
                           chooser="none", activity=None))
        if b.failures >= PROVIDER_ALERT_AFTER and not b.alerted:
            # Owner rule: a brain failure is a health alert, never faked as activity or as her being stuck.
            b.alerted = True
            self._journal(dict(ev="FAULT", session_id=session.session_id, type="decision_provider_unhealthy",
                               failures=b.failures, codes=list(b.codes), alert=True, fallback="none_configured"))
            self._live7_provider_recycle(session, b, "repeated_failures", force=False)
        return True

    def _live7_provider_recycle(self, session, b, why, *, force):
        """Once per failure streak: restart the decision lane's app-server (owner rule 2026-10-23). The Stardew
        decision policy configures no second provider, so recycling is the whole fail-over; nothing new is called."""
        if b.recycled or not force and self._live7_provider_lane_busy():
            return  # a still-owned lane is recycled only by the wedged bound, never mid-call before it
        b.recycled = True
        provider = getattr(self, "_provider", None)
        recycle = getattr(provider, "recycle_decision_lanes", None)
        owners = getattr(provider, "lane_owners", None) if force else None
        # The wedged lane is whichever one still owns a call (a background ask also waits for the foreground lane).
        want = tuple(k for k, v in owners.items() if v is not None) if isinstance(owners, dict) else ()
        lanes = recycle(want or ("background",), force=force) if callable(recycle) else None
        self._journal(dict(ev="FAULT", session_id=session.session_id, type="decision_provider_recycled", why=why,
                           failures=b.failures, codes=list(b.codes), lanes=lanes, alert=True))

    def _live7_choice_retry_budget(self, session, pin):
        budgets = self.__dict__.setdefault("_live7_choice_retries", {})
        key = (session.session_id, pin)
        return budgets.setdefault(key, ChoiceRetryBudget())

    def _live7_watchdog(self, session):
        """Every worker boundary, including busy actions, menus and read-only waits."""
        if session.campaign is None:
            return False
        now = game_now(self)  # owner/host pause does not spend the gameplay deadline
        slot = self.__dict__.get("_live7_progress_watch")
        if slot is None or slot[0] != session.session_id:
            # The harness carries ARMED / last semantic receipt across day
            # runners. Starting a worker without a receipt is not progress.
            # fix54H: a runner that opens a native day after a night begins at
            # the wake, never inside last evening's gap (night + reflection).
            stamp = self._config.get("stardew_live7_progress_at")
            after_night = (self._config.get("stardew_live7_resume") or {}).get("after_night") is True
            gap = max(0, time.time() - stamp) if not after_night and type(stamp) in (int, float) else 0
            slot = (session.session_id, ProgressWatchdog(now - gap, receipts=self._live7_semantic_receipts()))
            self._live7_progress_watch = slot
        watch = slot[1]
        facts, motor, context, held = self._live7_read(session)
        signature = None
        if not held and facts is not None and context is not None:
            # Progress is the evaluator's semantic notion (ADR 32): a verified inventory, money, location or tile
            # change, or a verified world effect. Clock ticks, vitals, receipt IDs, provider activity, a pixel
            # nudge and any menu re-render (open/close, paging, offer shrinkage, learned no-effect variants,
            # recovery rows) are not; a19 menu re-renders kept the watch below 600 s for a 647 s gap.
            watch.physical = self._live7_effect_evidence(facts, motor, context, watch.physical)
            signature = json.dumps(watch.physical, sort_keys=True, default=str)
        reading = watch.physical if signature is not None else None
        completed = sum(r.status in ("finished", "failed") for r in session.runs)
        menu_slot = self.__dict__.get("_live7_menu_deadline_slot")
        if menu_slot is not None and (menu_slot[0] != session.session_id
                                      or self.skill_post_row_state().get("menu") is None):
            self._live7_menu_deadline_slot = None  # ADR 43: a closed menu ends its in-menu decision deadline
        if self.skill_post_row_state().get("menu") is None:
            self._live7_menu_gone(session)
        native = (getattr(motor, "year", None), getattr(motor, "season", None), getattr(motor, "day", None))
        bed_wait = not held and self._live7_sleep_waiting(session)
        human_ready_wait = (not held and hasattr(self, "_live7_human_ready_waiting")
                            and self._live7_human_ready_waiting(session))
        held_before = watch.held
        if human_ready_wait:
            # Waiting for a human host at a native ready check is not a stall fault.
            stall = getattr(self, "_stall_seconds", None)
            if callable(stall):
                watch.stall_seen = stall()
        else:
            self._live7_stall_hold(session, watch, now)
        stalled = max(0.0, watch.held - held_before)
        stopped = watch.observe(now, signature, completed, receipts=self._live7_semantic_receipts(),
                                date=native if not held and None not in native else None)
        if bed_wait or human_ready_wait:
            # A fresh native bed wait or owner-hosted ready check holds the idle clock,
            # without manufacturing an effect receipt or crediting a completed night.
            watch.since, watch.recovery_attempted, stopped = now, False, False
        from arti_runtime import hands_halt
        raw = hands_halt.session_raw(self)
        halt = hands_halt.of(raw)
        was_halted = self.__dict__.get("_live7_ladder_halted") == session.session_id
        halt_held = halt is not None or (was_halted and not (
            raw is not None and raw.get("stop_latched") is False and "halt" in raw and raw["halt"] is None))
        # fix144A C10 (ADR 249; review R1c S1): while a HALT holds her hands the ladder is SUSPENDED, not spent: no rung is
        # judged, its age does not run, and once the mod's session.json shows the HALT explicitly cleared the climb starts
        # where a fresh stall would. The effect clock excludes the hold and rebaselines the first clear reading.
        if halt_held and not was_halted:
            self._live7_ladder_halted = session.session_id
            ladder = self._live7_ladder_state(session)
            if ladder.acting and self.__dict__.get("_live7_carry_after_close") is ladder.acting.get("call"):
                self.__dict__.pop("_live7_carry_after_close", None)
            clock_now = self._live7_effect_clock(session, now)
            ladder.restart(clock_now.resets)
            self._live7_ladder_row(session, ladder, clock_now, now, dict(
                kind="suspended", why=hands_halt.why(halt), clears=hands_halt.CLEARS,
                halt=dict(seq=halt.get("seq"), notes=halt.get("notes"))))
        elif was_halted and not halt_held:
            self.__dict__.pop("_live7_ladder_halted", None)
            watch.since, watch.recovery_attempted, stopped = now, False, False
            watch.repeats.clear()
            if session.current is not None:
                session.current.state.pop("effect_watchdog", None)
            clock_now = self._live7_effect_clock(session, now)
            self._live7_ladder_row(session, self._live7_ladder_state(session), clock_now, now,
                                   dict(kind="resumed", why="halt_cleared"))
        if halt is not None:
            self._live7_halt_said(session, halt)
        # ADR 241 A: the effect clock, observe-only; the same reading, never a second read of the game.
        effect = self._live7_effect_watch(session, now, reading, None if held else self.skill_post_row_state().get("menu"),
                                          native if not held and None not in native else None,
                                          stalled=stalled, bed_wait=bed_wait, known=not held,
                                          halt_held=halt_held, rearmed=was_halted and not halt_held,
                                          human_ready_wait=human_ready_wait)
        # ADR 241 B: the ladder of generic actions on that clock (suspended while a HALT holds). A fault in it never
        # aborts the boundary; the rest of the watchdog (the 60 s re-observe, the 600 s window) runs as before.
        try:
            if not halt_held:
                self._live7_ladder_step(session, now, effect, facts, motor, held, bed_wait or human_ready_wait, watch, context=context)
        except Exception as exc:  # noqa: BLE001 - named once per kind, the watchdog goes on
            seen = self.__dict__.setdefault("_live7_ladder_errors", set())
            if type(exc).__name__ not in seen:
                seen.add(type(exc).__name__)
                self._journal(dict(ev="FAULT", type="effect_ladder_error", error=type(exc).__name__,
                                   detail=str(exc)[:160], outcome_class="UNCERTAIN", disposition="RECOVER"))
        beat = getattr(self, "_live7_mind_heartbeat", None)
        if callable(beat):
            try:
                beat(session, now - watch.since, facts, motor)
            except OSError as exc:
                from arti_runtime.ipc import LOG
                LOG.error("FAULT mind_heartbeat error=%s", type(exc).__name__)
        if halt_held:
            return False
        # fix63B (ADR 95, a45 D2:J223): a second TV channel of a morning, no tile changed since the first; recovery closed
        # the forecast page and failed the run. Fresh native pages are not a stall; the 600 s bound is untouched.
        reading = native_page_reading(getattr(session.current, "state", {}))
        # Under a HALT the 60 s re-observe waits too: its menu close is a hand call (the window restarts at the clear).
        if (now - watch.since >= NO_PROGRESS_RECOVER_SECONDS and not watch.recovery_attempted and not reading
                and not self._live7_own_menu_fresh(session, now) and not self._live7_own_menu_closed(session)
                and not self._live7_menu_deadline_can_act(session, now)):
            # Never race an executor/provider or change identity to escape a
            # wait. Retry this boundary once it is idle, without resetting age.
            if not self._action_busy and self._inflight_generation is None:
                watch.recovery_attempted = True
                self._live7_progress_recover(session)
        if stopped and self._live7_menu_held_recover(session, watch, now):
            return False
        if not stopped:
            return False
        # ADR 241 B: no progress is never a safety stop. By 600 s the ladder has closed, stepped, left, gone home and
        # asked the cycle for a fresh one (rung 5 at 240 s); this row names the window and it starts over.
        # `at`: the harness can read this row minutes late (a54: 5 min, its loop was blocked by the same stall).
        ladder = self._live7_ladder_state(session)
        self._journal(dict(ev="live7_no_progress", at=time.time(), elapsed_s=round(now-watch.since, 3),
                           bound_s=NO_PROGRESS_SECONDS, stall_held_s=round(watch.held, 3),
                           repeats=dict(watch.repeats), rungs=ladder.summary(), outcome_class="UNCERTAIN",
                           disposition="RECOVER", recoverable=True))
        watch.since, watch.recovery_attempted = now, False
        watch.repeats.clear()
        return False

    def _live7_effect_clock(self, session, now=None):
        """ADR 241 A: this session's effect clock (one per session; a fresh runner starts its own baseline)."""
        from arti_runtime.effect_clock import EffectClock
        slot = self.__dict__.get("_live7_effect_slot")
        if slot is None or slot[0] != session.session_id:
            slot = self._live7_effect_slot = (session.session_id, EffectClock(since=game_now(self) if now is None else now))
        return slot[1]

    def _live7_effect_program(self):
        """True while a change in the game is the program's own (a ladder rung's trip is running)."""
        session = getattr(self, "_skill_session", None)
        slot = self.__dict__.get("_live7_ladder_slot")
        if session is None or slot is None or slot[0] != getattr(session, "session_id", None):
            return False
        acting = slot[1].acting
        run = getattr(session, "current", None)
        return bool(acting and acting.get("call") is not None and run is not None and run.call == acting["call"]
                    and run.started_at >= acting["mono"])

    def _live7_ladder_state(self, session):
        from arti_runtime.recovery_ladder import Ladder
        slot = self.__dict__.get("_live7_ladder_slot")
        if slot is None or slot[0] != session.session_id:
            slot = self._live7_ladder_slot = (session.session_id, Ladder())
        return slot[1]

    def _live7_ladder_row(self, session, ladder, clock, now, row):
        self._journal(dict(ev="live7_effect_ladder", at=time.time(), age_s=round(clock.age(now), 3),
                           ladder_age_s=round(clock.ladder_age(now), 3), **row))

    def _live7_ladder_tell(self, session, ladder, rung, clock, now, facts, motor, *, did=None, result=None,
                           withheld=None, witness=None):
        """One dated row in her ONE record (ADR 236 F) and the same line once on her next page."""
        from arti_runtime import recovery_ladder as LAD
        c = session.campaign
        clock_now = getattr(motor, "time_of_day", None)
        place = getattr(facts, "location", None) or getattr(motor, "location", None)
        text = LAD.her_line(rung, date=tuple(c.date), clock=clock_now, seconds=clock.age(now), place=place, did=did,
                            result=result, withheld=withheld)
        ladder.told = text
        if type(clock_now) is int:
            from arti_runtime import day_record
            day_record.live(self, tuple(c.date), clock_now, "ladder_rung_%d" % rung, text.split(" ", 4)[-1],
                            place=place, witness=witness or "ladder:%d:%s" % (rung, round(clock.since, 3)))
        return text

    def _live7_ladder_step(self, session, now, clock, facts, motor, held, bed_wait, watch, context=None):
        """ADR 241 B: the ladder, one judgement per boundary. A rung acts only past its bound on the effect clock,
        never races an executor or a provider call, and her own effect ends the climb."""
        from arti_runtime import recovery_ladder as LAD
        ladder = self._live7_ladder_state(session)
        if clock.resets != ladder.resets_seen:
            if ladder.taken:
                self._live7_ladder_row(session, ladder, clock, now, dict(kind="climb_over", reset_by=clock.reset_by,
                                                                         rungs=ladder.summary()))
            if ladder.acting and self.__dict__.get("_live7_carry_after_close") is ladder.acting.get("call"):
                self.__dict__.pop("_live7_carry_after_close", None)  # her own choice won over the rung
            ladder.restart(clock.resets)
            return
        self._live7_ladder_settle(session, ladder, clock, now, facts, motor)
        if held or bed_wait or clock.waiting(now):
            return
        rung = ladder.next_rung(clock.ladder_age(now), now)
        if rung is None or self._action_busy or self._inflight_generation is not None:
            return
        menu = self.skill_post_row_state().get("menu")
        if menu is None and self._live7_minigame_up():
            menu = LAD.MINIGAME  # ADR 243: a minigame holds her like a menu; rung 1 takes the game's own way out
        calls = None
        while rung is not None:
            if rung == 1:
                return self._live7_ladder_menu(session, ladder, clock, now, menu, facts, motor, watch)
            if rung == 5:
                row = ladder.judge(5, now, result="fresh_cycle_due", acted=True)
                self._live7_ladder_tell(session, ladder, 5, clock, now, facts, motor)
                self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung", rungs=ladder.summary(),
                                                                         told=ladder.told))
                return
            if menu is not None:
                row = ladder.judge(rung, now, result="not_applicable", why="menu_up", menu=menu)
                return self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))
            if calls is None:
                snapshot = self._build_decision_snapshot(self._active)
                calls = list(self._live7_calls(session, snapshot, facts)) if facts is not None else []
            place = getattr(facts, "location", None)
            here = getattr(motor, "tile", None)
            exits = LAD.scene_exits((context or {}).get("targets") if isinstance(context, dict) else None)
            grid = (context or {}).get("nav_grid") if isinstance(context, dict) else None
            withheld = None
            stalling = self._live7_ladder_stalling(session)
            # fix144A (ADR 249; review R4 SF8): a rung chooses from what C1 would leave on her page: a choice refused
            # with zero inputs while nothing changed is not the rung's either (her raw hands are never withheld).
            from arti_runtime import offer_withhold, raw_hands
            refused = {r[:2] for r in offer_withhold.refused(self, session, facts)}
            offered = [c for c in calls if (c.name, c.arg) not in refused or raw_hands.is_raw(c)]
            viable = [c for c in offered if (c.name, c.arg) not in stalling]

            def choose(pool):
                if rung == 2:
                    return LAD.step_target(pool, here, withheld=withheld)
                if rung == 3:
                    trips = getattr(session.campaign.live, "travel_trips", None)
                    return LAD.way_out(pool, place, trips=trips if isinstance(trips, dict) else None,
                                       withheld=ladder.withheld, exits=exits, here=here, grid=grid)
                return LAD.home_trip(pool, place, exits=exits, here=here, grid=grid)
            if rung == 2:
                withheld = self._live7_ladder_repeated(session)
                ladder.withheld = withheld
            # fix144A C9 (ADR 249): never re-issue the very call (name and argument) that is running or was the last to
            # stall; the rung takes its next viable choice (review R1 SF2) and is skipped only when nothing else is.
            call = choose(viable)
            stalled = choose(offered) if call is None and len(viable) < len(offered) else None
            if call is None and stalled is not None:
                row = ladder.judge(rung, now, result="not_applicable", why="same_call_is_stalling",
                                   call=[stalled.name, stalled.arg])
                self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))
                rung = ladder.pass_on(clock.ladder_age(now))  # a fresh cycle still waits for its own bound
                continue
            break
        if rung is None:
            return
        if call is None and withheld is None:
            row = ladder.judge(rung, now, result="not_applicable", why="nothing_offered")
            return self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))
        if call is not None:
            self._live7_ladder_carry(session, call)
            ladder.acting = dict(rung=rung, call=call, mono=time.monotonic(), at=now,
                                 evidence=dict(clock.evidence or {}), withheld=withheld)
            row = ladder.judge(rung, now, result="acting", call=[call.name, call.arg], acted=True)
        else:
            row = ladder.judge(rung, now, result="withheld_only", acted=True)
            self._live7_ladder_tell(session, ladder, rung, clock, now, facts, motor, withheld=withheld)
        if withheld:
            row["withheld"] = list(withheld)
        self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))

    def _live7_ladder_carry(self, session, call):
        """Rungs 2-4 start an offered call through the same path a menu's world choice uses (`_live7_advance`'s
        carry): the skill's own native admission, identity fence and verifier. A stuck run ends first, released."""
        run = session.current
        if run is not None:
            self._live7_release_for_recovery()
            run.state["live7_recovery_requested"] = True
            run.state["ladder_interrupted"] = True
        self._live7_carry_after_close = call

    def _live7_ladder_stalling(self, session):
        """fix144A C9 (ADR 249): the (skill, argument) of the run that is running now and of the last run that ended
        without finishing (bench 19: rung 3 chose `pergi_ke home` while `pergi_ke home` was the run that stalled).
        Identity is the name and the argument only, never a call's other fields (a tile, a label; review R1 SF2)."""
        out = set()
        run = getattr(session, "current", None)
        if run is not None and run.status == "running":
            out.add((run.call.name, run.call.arg))
        last = next((r for r in reversed(session.runs) if r is not run and r.status != "skipped"), None)
        if last is not None and last.status not in ("running", "finished"):
            out.add((last.call.name, last.call.arg))
        return out

    def _live7_ladder_repeated(self, session):
        """The choice she last repeated without an effect: two of her last three runs, or her last run that failed
        with no input. (name, arg, reason in the game's terms) or None."""
        runs = [r for r in session.runs if r.status in ("finished", "failed") and not r.state.get("halt_interrupted")][-3:]
        if not runs:
            return None
        last = runs[-1]
        same = [r for r in runs if (r.call.name, r.call.arg) == (last.call.name, last.call.arg)]
        if len(same) >= 2:
            return (last.call.name, last.call.arg, "dicoba %d kali tanpa perubahan di game" % len(same))
        if last.status == "failed" and last.inputs == 0:
            return (last.call.name, last.call.arg, "tadi tidak bisa dijalankan sama sekali (%s)" % (last.reason or "ditolak"))
        return None

    def _live7_ladder_settle(self, session, ladder, clock, now, facts, motor):
        """Read back a rung that acted: what the game shows changed while it ran (map, tile, items, money)."""
        from arti_runtime import recovery_ladder as LAD
        acting = ladder.acting
        if acting is None:
            return
        menu_rung = acting.get("rung") == 1
        if menu_rung:
            gone = (not self._live7_minigame_up() if acting.get("minigame")
                    else self.skill_post_row_state().get("menu") != acting.get("menu"))
            if not gone and now - acting["at"] < LAD.STEP_S:
                return
            word = "menu_closed" if gone else None
        else:
            started = [r for r in session.runs if r.call == acting["call"] and r.started_at >= acting["mono"]]
            ended = started and started[-1].status in ("finished", "failed", "unknown")
            if not ended and (now - acting["at"] < 2 * LAD.STEP_S or started and now - acting["at"] < LAD.RUN_MAX_S):
                return  # still running (bounded), or not started yet (the boundary has not reached the carry)
            if not ended and started and session.current is started[-1]:
                started[-1].state["live7_recovery_requested"] = True  # the rung's own trip is ended, released, read back
            word = LAD.read_back(acting.get("evidence"), clock.evidence)
        ladder.acting = None
        ladder.acted_at = now
        result = "changed:" + word if word else "no_change"
        row = next((r for r in reversed(ladder.taken) if r["rung"] == acting["rung"]), None)
        if row is not None:
            row.update(result=result, read_back=word or "nothing changed")
        call = acting.get("call")
        said = None if word else "Belum ada yang berubah."
        if acting["rung"] != 1:
            self._live7_ladder_tell(session, ladder, acting["rung"], clock, now, facts, motor,
                                    did=(call.arg if call.name == "pergi_ke" else "%s" % call.arg) if call else None,
                                    result=said, withheld=acting.get("withheld"))
        self._live7_ladder_row(session, ladder, clock, now, dict(kind="read_back", rung=acting["rung"],
                                                                 name=LAD.NAMES[acting["rung"]], result=result,
                                                                 told=ladder.told))

    def _live7_menu_deadline_can_act(self, session, now):
        from scripts.live7 import menu_deadline as D
        slot = self.__dict__.get("_live7_menu_deadline_slot")
        if D.enabled(getattr(self, "_config", None)) and slot is not None and slot[0] == session.session_id:
            from arti_stardew_generic_ui import MENU_DECISION_BOUND_S
            deadline = slot[1]
            view = self._ui_pad_view(self._binding_view()[1])
            return (deadline.receiver is not None and deadline.receiver == getattr(view, "receiver_token", None)
                    and deadline.menu == self.skill_post_row_state().get("menu")
                    and deadline.can_act(now, MENU_DECISION_BOUND_S))
        return False

    def _live7_ladder_menu(self, session, ladder, clock, now, menu, facts, motor, watch):
        """Rung 1, one generic rule for every menu kind: the game's own close; a question's leaving answer; a passive
        event is watched (the clock honours it); a minigame or a menu with no way out the game offers is said."""
        from arti_runtime import recovery_ladder as LAD
        from scripts.live7 import menu_deadline as D
        if menu is None:
            row = ladder.judge(1, now, result="not_applicable", why="no_menu")
            return self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))
        view = self._ui_pad_view(self._binding_view()[1])
        wait = getattr(view, "wait_reason", None)
        kind = LAD.menu_kind(view, wait, minigame=True) if self._live7_minigame_up() else LAD.menu_kind(view, wait)
        slot = self.__dict__.get("_live7_menu_deadline_slot")
        bounded = D.enabled(getattr(self, "_config", None))
        expired = False
        if kind == "closable" and slot is not None and slot[0] == session.session_id and slot[1].menu == menu:
            deadline = slot[1]
            if not bounded:
                row = ladder.judge(1, now, result="stood_aside", why="menu_deadline_owns", menu=menu)
                return self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))
            if deadline.receiver == view.receiver_token:
                from arti_stardew_generic_ui import MENU_DECISION_BOUND_S
                expired = not deadline.can_act(now, MENU_DECISION_BOUND_S)
                if not expired:
                    if not deadline.rung_deferred:
                        deadline.rung_deferred = True
                        self._live7_ladder_row(session, ladder, clock, now, dict(kind="rung_deferred", rung=1,
                            name=LAD.NAMES[1], result="stood_aside", why="menu_deadline_owns", menu=menu,
                            slot_age_s=deadline.elapsed(now), receiver=deadline.receiver))
                    return
        if kind == "question":
            from arti_stardew_generic_ui import controls
            if LAD.leaving_answer(view, controls(view)) is None:
                kind = "none"
            else:
                self._live7_ladder_leave = True
        if kind in ("closable", "question"):
            if not watch.recovery_attempted:
                if (native_page_reading(getattr(session.current, "state", {}) or {})
                        or (not expired and self._live7_own_menu_fresh(session, now)) or self._live7_own_menu_closed(session)):
                    self.__dict__.pop("_live7_ladder_leave", None)
                    return  # her fresh page or her own menu: rung 1 waits exactly as the 60 s re-observe (ADR 95, 105, FIX73A)
                watch.recovery_attempted = True
                self._live7_progress_recover(session)  # release, the game's own close (or the leaving answer), re-read
            self.__dict__.pop("_live7_ladder_leave", None)
            ladder.acting = dict(rung=1, call=None, menu=menu, mono=time.monotonic(), at=now,
                                 **({"minigame": True} if self._live7_minigame_up() else {}))  # ADR 243
            row = ladder.judge(1, now, result="acting", menu=menu, kind_of_menu=kind, acted=True)
            if kind == "question":  # a closed menu is already one row in her record (ADR 237 B, `menu_closed`)
                self._live7_ladder_tell(session, ladder, 1, clock, now, facts, motor)
            return self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))
        why = LAD.MINIGAME if kind == LAD.MINIGAME else "game_offers_no_way_out"
        row = ladder.judge(1, now, result="no_way_out", why=why, menu=menu)
        self._live7_ladder_row(session, ladder, clock, now, dict(row, kind="rung"))

    def _live7_effect_evidence(self, facts, motor, context, prior):
        base = self._live7_baseline(facts, motor, context)
        evidence = {k: base[k] for k in ("inventory", "money", "location", "tile", "crops")}
        evidence["location"] = getattr(motor, "location_id", None) or base.get("location_id") or evidence["location"]
        physical = ("kind", "terrain_type", "qualified_item_id", "stack", "health", "crop",
                    "growth_stage", "ready", "produce", "watered", "tile_x", "tile_y", "passable")
        evidence["world"] = {ref: {k: f[k] for k in physical if k in f}
            for ref, f in context.get("targets", {}).items() if isinstance(f, dict)}
        # A field this read could not supply is unknown, not a change.
        return {key: value if value is not None else prior.get(key) for key, value in evidence.items()}

    def _live7_effect_watch(self, session, now, evidence, menu, date, *, stalled=0.0, bed_wait=False, known=True,
                            halt_held=False, rearmed=False, human_ready_wait=False):
        """ADR 241 A: one boundary's reading of the game into the effect clock; one row on a reset, else one every
        ROW_INTERVAL_S. Observe-only: it sends nothing and decides nothing."""
        clock = self._live7_effect_clock(session, now)
        if halt_held or rearmed:
            clock.seen(now, hold_why="halt_latched")
            clock.restart_ladder(now)
            if halt_held:
                if clock.row_due(now):
                    self._journal(clock.row(now, kind="tick", **self._live7_effect_extra(session)))
                return clock
            # Changes up to the first clear read have no attribution to her hands.
            clock.evidence = clock.date = None
            clock.foreign_destination = None
            clock.menu_seen = clock.menu_counted = False
            self.__dict__.get("_live7_quiet_since", {}).pop(session.session_id, None)
        if not rearmed:
            clock.hold(stalled)
        if human_ready_wait:
            # Waiting for a human host at a native ready check she joined does not spend WAIT_HONOUR_MAX_S
            # or climb the ladder; if facts later become stale/unknown, the ordinary bounds start from here.
            clock.seen(now, hold_why="human_ready_wait")
            clock.restart_ladder(now)
        else:
            clock.seen(now, hold_why="bed_wait" if bed_wait else None)
        marks = self.__dict__.get("_live7_effect_program_marks", 0)
        program = marks != clock.program_seen or self._live7_effect_program()
        clock.program_seen = marks
        feedback = getattr(session, "choice_feedback", None)
        submitted = feedback.get("submitted") if isinstance(feedback, dict) else None
        if not human_ready_wait and isinstance(submitted, dict) and submitted.get("kind") == "wait" and feedback is not clock.wait_seen:
            clock.wait_seen = feedback
            clock.honour_wait(now, "her_wait")  # her own wait at a skill boundary, honoured a bounded time
        event = None
        reader = getattr(self, "_live7_native_event", None)
        if known and callable(reader):
            try:
                event = reader()
            except Exception:  # noqa: BLE001 - a missing event read is no event
                event = None
        passive = (isinstance(event, dict) and event.get("event_up") is not False
                   and event.get("event_player_control") is not True)
        if passive and not human_ready_wait:
            if not clock.waiting(now):
                clock.honour_wait(now, "watch_event")  # a passive native event: she watches; bounded per stretch
        # fix144A C13 (ADR 249; review R4 SF5, stream 11-19 c1 d7 J1195/J1619): while a passive event owns her input,
        # and at the first reading after it, a map, tile, item or menu change is the event's (its warps, its lines),
        # never hers: it moves her world (serial) without resetting the clock, as a rung's own change does.
        owned = passive or self.__dict__.get("_live7_event_owned_prev", False)
        self._live7_event_owned_prev = passive
        cause = clock.observe(now, evidence=evidence, menu=menu, menu_known=known, date=date, program=program or owned)
        if cause is not None:
            self._journal(clock.row(now, kind="reset", **self._live7_effect_extra(session)))
        elif clock.row_due(now):
            self._journal(clock.row(now, kind="tick", **self._live7_effect_extra(session)))
        return clock

    def _live7_effect_extra(self, session):
        slot = self.__dict__.get("_live7_ladder_slot")
        if slot is None or slot[0] != session.session_id or not slot[1].done:
            return {}
        return dict(rung=max(slot[1].done))

    def _live7_effect_receipt(self, record):
        """ADR 241 A: a journal row that shows her action changed the game resets the clock (the same `effect_of`
        the archive replay and the soak bench read)."""
        if record.get("ev") == "live7_ui_choice" and "controller" in record:
            self._live7_effect_program_marks = self.__dict__.get("_live7_effect_program_marks", 0) + 1
            return
        from arti_runtime.effect_clock import effect_of, foreign_arrival
        cause = effect_of(record)
        destination = foreign_arrival(record)
        if cause is None and destination is None:
            return
        session = getattr(self, "_skill_session", None)
        slot = self.__dict__.get("_live7_effect_slot")
        if session is None or slot is None or slot[0] != session.session_id:
            return
        from arti_runtime import hands_halt
        if self.__dict__.get("_live7_ladder_halted") == session.session_id or hands_halt.read(self) is not None:
            return
        now = game_now(self)
        clock = slot[1]
        if destination is not None:
            clock.foreign_destination = destination
            return
        if clock.note(now, cause, program=self._live7_effect_program()) is not None:
            self._journal(clock.row(now, kind="reset", **self._live7_effect_extra(session)))

    def _live7_menu_gone(self, session):
        """ADR 237 B: a menu that is gone holds nobody. ADR 237 C: when it held her, one row says so, so the cycle's
        bounded recovery never acts on a menu she answered or that went away by itself."""
        self._live7_receiver_visit = None
        gone = getattr(session, "_menu_held", None)
        session._menu_held = None
        if gone:
            self._journal(dict(ev="menu_held_cleared", at=time.time(), source="native_menu_gone",
                               menu=gone.get("menu"), receiver=gone.get("receiver")))

    def _live7_menu_held_recover(self, session, watch, now):
        """fix137A (ADR 237 B): a menu that holds her is a recoverable state, never a safety stop (owner rules 2026-10-26,
        2026-11-13: recover, not stop; a knockout is an event). mabar 2026-11-18 c1 d2 J4882: her QuestLog held 600 s and
        `live7_no_progress` stopped the game while one Escape closed it. The menu deadline closes a closable menu at
        EXIT_S; a menu the game will not close (refused, or a question that must be answered) stays hers to answer, its
        page says so, and this window reports `menu_held` with the menu's name and starts over. Nothing is pressed."""
        menu = self.skill_post_row_state().get("menu")
        slot = self.__dict__.get("_live7_menu_deadline_slot")
        if menu is None or slot is None or slot[0] != session.session_id or slot[1].menu != menu:
            return False
        held = getattr(session, "_menu_held", None) or {}
        self._journal(dict(ev="live7_menu_held", at=time.time(), state="menu_held", menu=menu, recoverable=True,
                           elapsed_s=round(now - watch.since, 3), bound_s=NO_PROGRESS_SECONDS,
                           deadline_cycle=slot[1].cycle, game_refused=held.get("menu") == menu,
                           shown=held.get("shown"), page=held.get("page"), repeats=dict(watch.repeats),
                           outcome_class="UNCERTAIN", disposition="RECOVER"))
        self._live7_recovery_fault(session, "menu_held", refs=dict(menu=menu, shown=held.get("shown"),
                                                                    page=held.get("page"), cycle=slot[1].cycle))
        watch.since, watch.recovery_attempted = now, False
        watch.repeats.clear()
        return True

    def _live7_stall_hold(self, session, watch, now):
        """ADR 128: uncertainty re-observes and continues, logged as a fault. One FAULT when a stall starts, one row when it ends."""
        stall = getattr(self, "_stall_seconds", None)
        delta = watch.hold_stall(stall()) if callable(stall) else 0.0
        slot = self.__dict__.get("_live7_stall_slot")
        if delta > 0:
            if slot is None:
                # ADR 130: prior_held_s lets the evaluator size an episode still open at the stop (stall_held_s - prior).
                self._journal(dict(ev="FAULT", session_id=session.session_id, type="live7_host_stall", cause="machine_stall_hold",
                                   at=time.time(), held_s=round(watch.held, 3), prior_held_s=round(watch.held - delta, 3),
                                   bound_s=STALL_HOLD_MAX_S,
                                   outcome_class="UNCERTAIN", verified=False, disposition="RECOVER", replay=False))
            self._live7_stall_slot = (now, slot[1] if slot else watch.held - delta)
        elif slot is not None and now - slot[0] >= STALL_ENDED_QUIET_S:
            self._live7_stall_slot = None
            self._journal(dict(ev="live7_host_stall_ended", at=time.time(), session_id=session.session_id,
                               held_s=round(watch.held - slot[1], 3), window_held_s=round(watch.held, 3)))

    def _live7_own_menu_fresh(self, session, now):
        """ADR 105 (a47 d12 J1199, d21 J361, d25 J393): the 60 s above counts time without a world effect, and it ran out
        while she thought, so the bag her own `inventori` had just opened was shut one row later. A menu her running skill
        opened gets the same time as any menu decision (ADR 43's exit bound), restarted by each decision she makes in it."""
        from scripts.live7.menu_deadline import EXIT_S
        action, run = getattr(self._active, "last_action", None), session.current
        if (run is None or run.goal_id != getattr(self._active, "goal_id", None)
                or getattr(action, "outcome", None) not in ("MenuOpened", "QuestionOpened")
                or getattr(action, "lifecycle", None) != "completed" or self.skill_post_row_state().get("menu") is None):
            self._live7_own_menu = None
            return False
        key = (session.session_id, getattr(action, "request_id", None))
        slot = self.__dict__.get("_live7_own_menu")
        if slot is None or slot[0] != key:
            slot = self._live7_own_menu = [key, now]
        return now - slot[1] < EXIT_S

    def _live7_own_menu_closed(self, session):
        """FIX73A (a54 d19 J670: the note opened, J675 `MenuOpened`; she read it for 2.5 minutes and closed it herself, J845; then
        J849 `no_progress_reobserve` turned that into `skill_failed` "MenuOpened", J851, and a `tanpa_efek` line). The same run
        ended well on d16 J784 and d19 J651, which just had fresher tiles. Her own closed menu is that run's finish, so its next
        step reports it; the recovery waits one row, never the 600 s stop, and the world still has to change to count as progress."""
        state = getattr(session.current, "state", None)
        return (isinstance(state, dict) and state.get("generic_menu_receiver") is not None
                and self.skill_post_row_state().get("menu") is None)

    def _live7_note_journal(self, record):
        """Count progress receipts and publish native persona facts at effect boundaries."""
        if semantic_receipt(record):
            self._live7_semantic_receipt_count = self._live7_semantic_receipts() + 1
        if record.get("ev") != "live7_effect_clock":
            try:
                self._live7_effect_receipt(record)
            except Exception as exc:  # noqa: BLE001 - the observe-only clock never aborts a journal write
                from arti_runtime.ipc import LOG
                LOG.error("FAULT effect_clock error=%s", type(exc).__name__)
        session = getattr(self, "_skill_session", None)
        if session is None or getattr(session, "mode", None) != "part_live" or session.campaign is None:
            return
        from scripts.live7.native_facts import completed_receipt
        if record.get("ev") == "item_move_outcome":
            from arti_runtime.transfer_outcomes import remember
            remember(session, record)
            if "chest_count" in record:
                self._journal(dict(ev="live7_chest_memory", at=record["at"], game=dict(date=record["date"]),
                                   memory=session.campaign.live.chest_memory.dump()))
        completed = completed_receipt(record, time.time())
        if (record.get("ev") == "finish_verified" and completed is not None
                and completed.get("effect") == "stored" and getattr(session, "_persona_item_moves", None)):
            completed = None  # episode close cannot supersede a newer item move (ADR 140)
        if completed is not None:
            moves = getattr(session, "_last_item_move_rows", [])
            if moves and (completed.get("request_id") == moves[0]["request_id"]
                          or moves[0]["request_id"] in (completed.get("request_ids") or ())):
                from arti_runtime.transfer_outcomes import lines
                completed["item_move_outcomes"] = lines(session)
            session._persona_completed_action = completed
        if (record.get("ev") == "native_cursor_destination" and record.get("effect") == "dropped"
                or record.get("ev") == "native_ground_item"):
            self._live7_ground_record(session, record)
        if record.get("ev") == "native_fishing_catch":
            self._live7_catch_record(session, record)
        crops_changed = record.get("ev") == "live7_crop_memory" and isinstance(record.get("crops"), dict)
        scene_changed = (record.get("ev") == "part_live_row"
                         and isinstance((record.get("evidence") or {}).get("scene_state"), dict))
        if crops_changed:
            from scripts.live7.live7_common import own_crop_facts
            value = own_crop_facts(record["crops"], session.campaign.date)
            session._persona_own_crops = dict(value=value or dict(total=0, ready=0, growing=0, dead=0, lines=[]),
                observed_ts=record.get("at", time.time()), source="native_crop_ledger",
                date=list(session.campaign.date))
        if completed is not None or crops_changed or scene_changed or (record.get("ev") == "interaction_effect"
                and record.get("world_effect_status") == "observed_location_change"):
            # Receipt-time publication also runs while the worker is inside a skill.
            # Keep the existing watchdog's blocked state and the model's intent.
            slot = getattr(self, "_live7_progress_watch", None)
            stalled = max(0, game_now(self) - slot[1].since) if slot else 0
            try:
                self._live7_mind_heartbeat(session, stalled, None, None)
            except Exception as exc:  # evidence must never abort an action
                from arti_runtime.ipc import LOG
                LOG.error("FAULT mind_native_receipt error=%s", type(exc).__name__)

    def _live7_catch_record(self, session, record):
        """ADR 115: one landed catch -> today's count on her page, under the place she cast from."""
        from arti_runtime import loop_facts
        from scripts.live7.native_facts import bound_pose
        live = getattr(session.campaign, "live", None)
        if live is None:
            return
        current = getattr(session, "current", None)
        if current is not None and current.call.name == "tangkap_ikan":
            current.state["native_landed_catch"] = dict(record)
        location = record.get("location") or getattr(bound_pose(self), "location", None) or "tempat tak terbaca"
        if loop_facts.note_catch(live.catches, request_id=record.get("request_id"), location=location, item=record.get("item"),
                                 name=record.get("name"), category=record.get("category")):
            loop_facts.note_total(live.catch_totals, location, record.get("category"))
            loop_facts.note_kind(live.catch_kinds, record.get("item"))  # FIX73D: this kind, across all days
            self._journal(dict(ev="live7_catch_totals", totals=live.catch_totals, kinds=live.catch_kinds))  # carried into the next day's process

    def _live7_chest_seen(self, facts):
        """FIX72D: one native read of an open chest -> her memory of it, journaled for the next day's process."""
        from scripts.live7.native_facts import bound_pose
        session = getattr(self, "_skill_session", None)
        c = getattr(session, "campaign", None)
        live = getattr(c, "live", None)
        if getattr(c, "part", None) != "live" or live is None or not hasattr(live, "chest_memory"):
            return
        pose = bound_pose(self)
        if live.chest_memory.observe(facts.get("source_fact_id"), facts.get("container_slots"), date=c.date,
                                     clock=getattr(pose, "time_of_day", None), names=live.item_names,
                                     location=getattr(pose, "location", None)):
            self._journal(dict(ev="live7_chest_memory", at=time.time(), game=dict(date=list(c.date)), memory=live.chest_memory.dump()))

    def _live7_chest_full(self, fact):
        """Record a verified full-chest observation on that chest's memory row and journal it."""
        session = getattr(self, "_skill_session", None)
        c = getattr(session, "campaign", None)
        live = getattr(c, "live", None)
        if getattr(c, "part", None) != "live" or live is None or not hasattr(live, "chest_memory"):
            return
        if live.chest_memory.note_full(fact["ref"], fact["taken"], fact["total"], date=fact["date"],
                                       clock=fact.get("clock"), location=fact.get("location"),
                                       slots=fact.get("slots"), names=live.item_names):
            self._journal(dict(ev="live7_chest_memory", at=time.time(), game=dict(date=list(c.date)), memory=live.chest_memory.dump()))

    def _live7_known_chests(self, location):
        """Chests she remembers on this map; a place to walk to, never proof the chest or its contents are still there."""
        live = getattr(getattr(getattr(self, "_skill_session", None), "campaign", None), "live", None)
        memory = getattr(live, "chest_memory", None)
        return memory.here(location) if memory is not None else ()

    def _live7_ground_record(self, session, record):
        """fix59A (ADR 86): one native ground receipt -> world-effects memory, with where she stood and when."""
        from scripts.live7.native_facts import bound_pose
        memory = getattr(getattr(session.campaign, "live", None), "ground_items", None)
        if memory is None:
            return
        pose = bound_pose(self)
        location = record.get("location") or getattr(pose, "location", None)
        same = pose is not None and getattr(pose, "location", None) == location
        row = memory.record(item=record.get("item"), count=record.get("quantity"), location=location,
                            via=record.get("via") or {"dropped": "dropped", "spilled": "spilled", "trashed": "trashed"}.get(record.get("effect")),
                            date=record.get("date") or session.campaign.date,
                            clock=record.get("time", getattr(pose, "time_of_day", None)),
                            tile=record.get("tile") or (getattr(pose, "tile", None) if same else None),
                            # FIX71C (a52 d2 J1358: her page read "[recorded remark omitted]"): the bag's own native name, seen before it left
                            name=getattr(session.campaign.live, "item_names", {}).get(record.get("item")) or record.get("name"),
                            is_tool=record.get("is_tool") is True, bag=record.get("bag_after"), receipt_at=time.time(),
                            quality=record.get("quality"))
        if row is not None:
            self._journal(dict(ev="live7_ground_items", at=time.time(), game=dict(date=list(session.campaign.date)),
                               request_id=record.get("request_id"), items=memory.dump()))

    def _live7_pose_observed(self):
        """Native state ingress: publish self-state/menu edges even during a provider call."""
        session = getattr(self, "_skill_session", None)
        if session is None or getattr(session, "mode", None) != "part_live" or session.campaign is None:
            return
        binding, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None and binding.status == "bound" else None
        if motor is None or self._state.is_stale(pid):
            return
        from arti_runtime.decision_plan import observe
        observe(self, session, motor)
        from scripts.live7.native_facts import state_readback
        from arti_runtime.time_grounding import fact_values
        key = (session.session_id, motor.location, motor.year, motor.season, motor.day, motor.time_of_day,
               json.dumps(fact_values(state_readback(self)), sort_keys=True))
        from arti_stardew_live7 import MIND_HEARTBEAT_SEC
        beat = getattr(self, "_live7_mind_beat", None)
        if (key != getattr(self, "_live7_persona_pose", None)
                or beat is None or time.time() - beat[1] >= MIND_HEARTBEAT_SEC):
            slot = getattr(self, "_live7_progress_watch", None)
            stalled = max(0, game_now(self) - slot[1].since) if slot else 0
            self._live7_mind_heartbeat(session, stalled, None, motor)
            self._live7_persona_pose = key

    def _live7_semantic_receipts(self):
        return self.__dict__.get("_live7_semantic_receipt_count", 0)

    def _live7_minigame_up(self):
        """ADR 243 (raw hands on): native says a minigame holds her input now (ui_motor `control_taken`)."""
        from arti_runtime import raw_hands
        if not raw_hands.enabled(getattr(self, "_config", None)):
            return False
        reader = getattr(self, "_live7_motor_facts", None)
        native = reader() if callable(reader) else None
        return isinstance(native, dict) and native.get("control_taken") == "minigame"

    def _live7_progress_recover(self, session):
        """Release, admissible close, fresh read; no effect credit or action replay."""
        self._live7_recovery_fault(session, "no_progress_reobserve")
        released = self._live7_release_for_recovery()
        _, _, _, held = self._live7_read(session)
        state = self.skill_post_row_state()
        if released and not held and (state.get("menu") is not None or self._live7_minigame_up()):
            self._live7_generic_ui(session, recovery_close=True)  # ADR 243: a minigame's own way out too
        self._live7_read(session)  # the close's result never substitutes for observation
        p = session.campaign.live
        p.wait_since = game_now(self)
        p.release_after = None
        if session.current is not None:
            session.current.state["live7_recovery_requested"] = True

    def _live7_leg_end_reason(self, session, status):
        """fix54I (a39: 7 `runtime_not_ready:READY` rows, each after a `no_progress_reobserve` on the same run): a leg
        that ends with no reason while the runtime is READY is not a readiness state. Name the preceding recovery
        cause when this run recorded one; otherwise say plainly that no reason was given. Still a NOT_READY class."""
        if status != "READY":
            return "runtime_not_ready:" + str(status)
        last = self.__dict__.get("_live7_last_recovery_cause")
        if last and last[:2] == (session.session_id, len(session.runs)):
            return "recovered_after:" + last[2]
        return "leg_ended_no_reason"

    def _live7_recovery_fault(self, session, cause, *, observed=None, refs=None):
        """One named FAULT per recovery attempt; polls of that attempt do not multiply it."""
        p = session.campaign.live
        run = session.current
        action = getattr(self._active, "last_action", None)
        refs = dict(refs or {})
        refs.update(goal_id=getattr(run, "goal_id", None),
                    request_id=getattr(action, "request_id", None))
        recent = getattr(self, "_live7_ui_recent", ())
        if recent:
            refs.setdefault("ui_request_id", recent[-1].get("request_id"))
        key = (session.session_id, len(session.runs), cause, str(refs))
        seen = self.__dict__.setdefault("_live7_recovery_faults", set())
        if key in seen:
            return False
        seen.add(key)
        # fix54I: the leg this recovery ends may carry no reason of its own; remember the real cause for its label.
        self.__dict__["_live7_last_recovery_cause"] = (session.session_id, len(session.runs), str(cause))
        state = dict(observed if observed is not None else self.skill_post_row_state())
        facts, motor, context, held = self._live7_read(session)
        state["readback_hold"] = held
        view = self._ui_pad_view(self._binding_view()[1]) if not held else None
        state["held_item"] = getattr(view, "held_item", "UNKNOWN")
        if not held:
            rec = self._obs.get(self._binding_view()[1], "ui.context")
            raw_facts = getattr(rec, "facts", {})
            if isinstance(raw_facts, dict):
                state["toolbar"] = raw_facts.get("toolbar")
        # UiPadInput omits held_item for an empty cursor. A validated current
        # view parses that omission as None; it is not an unreadable hand.
        state["held_item_known"] = view is not None
        if view is None:
            state["held_item"] = "UNKNOWN"
        if not held and facts is not None:
            state["inventory"] = dict(facts.inventory)
            state["location"] = facts.location
        fault = dict(type="live7_recovery", cause=str(cause), refs=refs,
                     skill=run.call.name if run else None, observed=state,
                     outcome_class="UNCERTAIN", verified=False, disposition="RECOVER", replay=False)
        self._journal(dict(ev="FAULT", session_id=session.session_id, **fault))
        # The current decision already carries the complete inventory/toolbar.
        # Retain compact recovery facts so UI paging cannot be crowded out by old bags.
        note = dict(fault, observed={k: state[k] for k in
            ("menu", "location", "free", "input_released", "food_clear", "held_item", "held_item_known", "readback_hold")
            if k in state})
        p.recovery_history.append(note)
        del p.recovery_history[:-8]
        slot = self.__dict__.get("_live7_progress_watch")
        if slot and slot[0] == session.session_id:
            watch = slot[1]
            watch.repeats[str(cause)] += 1
            if watch.repeats[str(cause)] == RECOVERY_REPEAT_LIMIT:
                self._journal(dict(ev="live7_recovery_loop", cause=str(cause),
                    count=RECOVERY_REPEAT_LIMIT, action="wait_for_verified_progress_or_600s_deadline"))
        return True

    def _live7_uncertain_ui(self, session, cause):
        failed = self.__dict__.pop("_live7_ui_unsafe", None)
        named = cause
        refs = None
        if isinstance(failed, dict):
            # ADR 257 (live 11-19: 10 of 11 on one chest): name the menu input that stayed unconfirmed, e.g.
            # `generic_ui_readback_unconfirmed:press:B:UiPressUncertain`, not the press that opened the menu. The FAULT
            # keeps its class as its cause and carries the input in its refs.
            named = "%s:%s:%s" % (cause, failed.get("pick"), failed.get("reason"))
            refs = dict(failed_input=failed.get("pick"), input_result=failed.get("reason"), menu=failed.get("menu"),
                        receiver=failed.get("receiver"))
        if not (self._live7_recovery_fault(session, cause, refs=refs) if refs else self._live7_recovery_fault(session, cause)):
            return 0
        cause = named
        p = session.campaign.live
        p.wait_since = p.wait_since or game_now(self)
        run = session.current
        trace = (run.state.get("native_ui_episode") or {}) if run is not None else {}
        opened = trace.get("chest_open")
        if (opened and failed and failed.get("close") is True and failed.get("menu") == "ItemGrabMenu"
                and failed.get("request_id") and failed["request_id"] != opened["request_id"]
                and failed.get("receiver") and failed["receiver"] == trace.get("chest_receiver")):
            # A later close of this chest keeps its own FAULT and receiver latch.
            run.pending = ("done", "observed_generic_readback")
            run.state["live7_recovery_requested"] = True
            run.state["menu_input_unconfirmed"] = cause
        elif run is not None:
            session.current.pending = ("fail", cause)
            session.current.state["live7_recovery_requested"] = True
            session.current.state["ui_uncertain_cause"] = cause  # a later native-verified finish still wins
        # No replay and no clearing of per-receiver uncertainty/mutation latches.
        # A different/fresh safe receiver may still be offered to her chooser.
        return 0

    def _live7_release_for_recovery(self):
        """Only release our existing lease, never acquire one or send gameplay input."""
        pin = getattr(self, "_water_action_pin", None)
        if pin is not None:
            if not self._cleanup_water(pin):
                return False
            self._water_action_pin = None
        pin = getattr(self, "_action_pin", None)
        if pin is not None:
            folder, session, owner, generation = pin
            try:
                self._commands.tulis_kontrol("release", folder=folder, sesi=session,
                    owner_id=owner, generation=generation, control_seq=2,
                    expected_screen_id=session.screen_id)
                self._wait_lease(folder, session, owner, generation, release=True,
                    request_id=getattr(getattr(self._active, "last_action", None), "request_id", None))
            except (OSError, ValueError, self._commands.KesalahanPerintah):
                return False
            self._action_pin = None
        return self.skill_post_row_state().get("input_released") is True

    def _live7_unheld_wait_end(self, session):
        """fix144A C7 and C10 (ADR 249): a recovery wait that waits for a proof that cannot come ends its run.

        The recovery boundary (`_live7_recover`) holds a run read-only until native proves her input released
        (`input_release_state`). A latched HALT makes that proof impossible (it needs `halt is None`): bench 19 held a
        failed `pergi_ke home` for 170 s, no `skill_failed`, no page. Here, before that wait, when native evidence shows
        nothing of hers held (`input_held` false and the mod's session.json quiet: not busy, nothing in flight, no
        lease owner, no outstanding pad press), no executor or provider call runs, a fresh read holds nothing back, and
        the night, a day rollover or a passive event does not own the moment:
        - URGENT (a latched HALT, or a session-level precheck refusal, C8): the run ends at once, even when an earlier
          request's idle receipt made `input_released` true and a menu is up (review R1b SF1);
        - otherwise only a run whose own outcome is already pending ends, after UNPROVEN_END_S of CONTINUOUS quiet:
          the stretch starts at a fully read, quiet boundary and any unread, busy, held, menu or night poll restarts it
          (review R1b SF4); her last effect on the effect clock also restarts it.
        A run whose selected food must still be put away keeps its cleanup owner (R1b SF4). The run ends as
        `skill_failed` with a cause that names what it waited for. With a possible-mutation fence set C7 steps aside
        before it closes anything (review R1c B1/B2): main's recovery tail keeps the run and settles the fence on that
        run's own release proof; the hold is reported once. Her next page says it
        once and carries her raw hands; a HALT is said once per native episode. With a menu up the menu stays the
        recovery's. Returns 1 when it ended the wait, else None. Nothing is pressed, released, cleared or re-armed."""
        from arti_stardew_runtime import READY, PAUSED_RECOVERY
        from arti_runtime import hands_halt
        c = getattr(session, "campaign", None)
        p = getattr(c, "live", None)
        quiet = self.__dict__.setdefault("_live7_quiet_since", {})
        key = getattr(session, "session_id", None)
        with self._lock:
            if (p is None or p.wait_since is None or session.status != "active" or session.advancing
                    or self._action_busy or self._inflight_generation is not None or getattr(p, "pending_sleep", None)
                    or self._status not in (READY, PAUSED_RECOVERY) or self._worker_stop.is_set()
                    or getattr(self, "_owner_play_stopped", False) or getattr(self, "_day_start_active", False)
                    or getattr(self, "_water_action_pin", None) is not None or getattr(self, "_action_pin", None) is not None):
                quiet.pop(key, None)
                return None
            session.advancing += 1
        try:
            state = self.skill_post_row_state()
            raw = hands_halt.session_raw(self)
            run = session.current
            refusal = run.state.get("session_refusal") if run is not None else None
            halt = hands_halt.of(raw)
            urgent = halt is not None or bool(refusal)
            if ((state.get("input_released") is True and not urgent) or state.get("input_held") is not False
                    or not hands_halt.quiet(raw) or state.get("body_guard_active") or state.get("food_clear") is False
                    or (run is not None and run.state.get("recovery_food_required"))):
                quiet.pop(key, None)  # released: the recovery's own tail; held, unknown or food to clear: its wait
                return None
            ui_free = state.get("menu") is None and state.get("free") is True
            if not ui_free and (not urgent or run is None):  # no run left to end: the menu is the recovery's
                quiet.pop(key, None)
                return None
            if self._live7_night_or_event_owns(session):
                quiet.pop(key, None)
                return None
            facts, motor, context, held = self._live7_read(session)
            if held or facts is None:
                quiet.pop(key, None)  # an unread or held reading is no quiet: the stretch starts again after it
                return None
            now = game_now(self)
            clock = self._live7_effect_clock(session, now)
            since = now
            if halt is not None:
                quiet.pop(key, None)
                waited = hands_halt.why(halt)
            elif refusal:
                quiet.pop(key, None)
                waited = str(refusal)
            else:
                menu = getattr(motor, "active_menu", None)
                if (menu is not None or getattr(motor, "is_player_free", None) is not True
                        or self.__dict__.get("_live7_ladder_halted") == key):
                    quiet.pop(key, None)
                    return None
                since, last = quiet.get(key, (now, now))
                if now - last > hands_halt.QUIET_POLL_GAP_S:
                    since = now
                quiet[key] = (since, now)
                if (run is None or run.pending is None
                        or min(now - p.wait_since, now - since) < hands_halt.UNPROVEN_END_S):
                    return None
                # This read can be newer than the watchdog's sample from the same worker boundary.
                evidence = self._live7_effect_evidence(facts, motor, context, clock.evidence or {})
                self._live7_effect_watch(session, now, evidence, menu, self._date_tuple(motor))
                if clock.age(now) < hands_halt.UNPROVEN_END_S:
                    return None
                waited = "input_release_proof"
            if getattr(self, "_gather_mutation_unresolved", False):
                # Review R1c B1/B2: a possible mutation is never settled by C7 and its run is never closed out from under
                # it. Main's own recovery tail keeps the run and settles the fence only on that run's release proof and
                # fresh read, by its own transitions (arti_stardew_live7.py:7357-7469). The hold stays; it is said once.
                held_keys = self.__dict__.setdefault("_live7_wait_held", set())
                if (key, id(run)) not in held_keys:
                    held_keys.add((key, id(run)))
                    self._journal(dict(ev="live7_wait_held", at=time.time(), reason="possible_mutation_owned_by_recovery",
                                       waited_for=waited, skill=run.call.name if run is not None else None,
                                       arg=run.call.arg if run is not None else None, halt=halt))
                if halt is not None:
                    self._live7_halt_said(session, halt)
                return None
            why = waited
            if run is not None:
                base = run.pending[1] if run.pending else "no_change"
                why = base if waited in base else base + ":waited_for:" + waited
                sent = self._physical_action_publications - run.inputs_start
                cleanup = dict(state)
                if halt is not None or hands_halt.session_level(refusal) == "stop_latched":
                    cleanup["halt_interrupted"] = True
                if halt is not None or sent <= 0:
                    # Nothing of this run can be held: it sent no input, or a latched HALT refuses and releases every
                    # input natively, and native shows none held (no mutation fence is set here, see above).
                    cleanup.update(input_released=True, release_basis="halt_latched" if halt is not None else "no_input")
                self._live7_leg(session, run, ("fail", why), cleanup)
                from arti_stardew_live7 import SAFETY_STOP
                if run.state.get("live7_outcome") == SAFETY_STOP:
                    with self._lock:
                        self._skill_end_locked(session, "held", SAFETY_STOP, "live7:" + why)
                    return 1
                with self._lock:
                    if session.current is run and session.status == "active":
                        self._skill_close_run_locked(session, run, ("fail", why))
            row = dict(ev="live7_wait_ended", at=time.time(), cause=why, waited_for=waited,
                       waited_s=round(now - p.wait_since, 3), quiet_s=round(now - since, 3),
                       effect_age_s=round(clock.age(now), 3), skill=run.call.name if run is not None else None,
                       arg=run.call.arg if run is not None else None, halt=halt, replay=False)
            if halt is not None:
                self._live7_halt_said(session, halt)
            if run is not None:
                line = hands_halt.ended_line(run.call.name, run.call.arg, clock.age(now), waited, halt)
                ladder = self._live7_ladder_state(session)
                ladder.told = ladder.told + " " + line if ladder.told else line  # her next page, once
            if not ui_free:
                # SF1: the refused run has its one terminal outcome; the open menu stays the recovery's (generic UI).
                quiet.pop(key, None)
                self._journal(dict(row, ui_owned_by="recovery", menu=state.get("menu")))
                return None
            if halt is None:
                self._live7_reconcile(session, facts, motor, context)
            quiet.pop(key, None)
            p.wait_since = None
            p.release_after = None
            self._journal(row)
            result = self._skill_session_goal(session)  # a fresh page, as the recovery's own tail
            if not result.admitted:
                self._live7_help(session, "automatic_recovery_refused:" + str(result.reason))
            return 1
        finally:
            with self._lock:
                session.advancing -= 1

    def _live7_night_or_event_owns(self, session):
        """SF4: what the recovery's own tail honours before it ends a run (arti_stardew_live7.py:7357-7369): a bed
        ticket or bed evidence of today, a native morning pending, a day-start pass, a day rollover, a passive event.
        Pure reads; nothing is credited or cleared."""
        c = session.campaign
        if c.live.pending_sleep or getattr(self, "_day_start_active", False):
            return True
        try:
            if self._live7_morning_pending():
                return True
        except Exception:  # noqa: BLE001 - an unread morning is not a free moment
            return True
        from arti_runtime.sleep_truth import bed_evidence, last_bed_observed
        observed = last_bed_observed(c)
        if bed_evidence(observed) and tuple(observed.get("start_date") or ()) == tuple(c.date):
            return True
        motor = self._state.latest(self._binding_view()[1])
        date = self._date_tuple(motor) if motor is not None else None
        if date is not None and None not in date and tuple(date) != tuple(c.date):
            return True
        try:
            event = self._live7_native_event()
        except Exception:  # noqa: BLE001 - a missing event read is no event
            event = None
        return isinstance(event, dict) and event.get("event_up") is not False and event.get("event_player_control") is not True

    def _live7_halt_alert_path(self):
        """Where the alerted HALT episodes are kept for this run: beside the cycles (`<run>/runtime/live7/`), so a
        replacement runner or the cycle after a rejoin reads the same file (review R1b SF3). None without a cycle."""
        from pathlib import Path
        cycle = (getattr(self, "_config", None) or {}).get("stardew_live7_cycle_id")
        if not isinstance(cycle, str) or not cycle:
            return None
        folder = Path(cycle).parent
        return folder / "hands_halt_alerts.json" if folder.is_dir() else None

    def _live7_halt_journal(self, path, entry, save, rows):
        """Recover the append receipt if the previous writer died before saving it."""
        from arti_runtime import ipc

        receipts = entry["receipts"]
        current = getattr(self, "_journal_path", None)
        root = path.parent.resolve() if path is not None else None
        journal = current().resolve() if root is not None and callable(current) else None
        if journal is not None and not journal.is_relative_to(root):
            journal = None

        def read_receipts():
            if root is None or "journal" not in entry:
                return
            name, offset = entry["journal"]
            source = (root / name).resolve()
            if (not source.is_relative_to(root) or source.suffix != ".jsonl"
                    or {"bridge", "session_logs"} & {part.casefold() for part in source.relative_to(root).parts}):
                return
            try:
                with ipc.open_shared(source, "rb") as stream:
                    stream.seek(offset)
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and row.get("alert_id") == rows[0]["alert_id"]:
                            event = row.get("ev")
                            if event in ("live7_hands_halted", "minta_tolong") and event not in receipts:
                                receipts.append(event)
            except FileNotFoundError:
                pass

        if all(row["ev"] in receipts for row in rows):
            return
        read_receipts()
        pending = [row for row in rows if row["ev"] not in receipts]
        if pending and journal is not None:
            entry["journal"] = [str(journal.relative_to(root)), journal.stat().st_size if journal.exists() else 0]
            save()  # record where to look before appending
        for row in pending:
            self._journal(row)
            if journal is None:
                receipts.append(row["ev"])
        read_receipts()
        save()

    def _live7_halt_said(self, session, halt):
        """C10: one incident per native HALT episode; speech and help keep separate destination receipts."""
        from arti_runtime import hands_halt, ipc
        key = hands_halt.episode_key(halt)
        facts = [(halt or {}).get("seq"), (halt or {}).get("cause"), list((halt or {}).get("notes") or ())]
        path = self._live7_halt_alert_path()
        spoken = getattr(self, "_on_generation_result", None)
        sink = getattr(self, "_help_sink", None) if self._config.get("live7_help_enabled") is True else None
        cache = self.__dict__.setdefault("_live7_halts_said", {})
        retry = self.__dict__.setdefault("_live7_halt_retry_at", {})
        if cache.get(key) == (facts, spoken, sink) or time.monotonic() < retry.get(key, 0):
            return False
        local = self.__dict__.setdefault("_live7_halt_alerts", {})
        try:
            lock = self.__dict__.setdefault("_live7_halt_lock", threading.Lock())
            with hands_halt.episode_lock(path) if path is not None else lock:
                said = local
                if path is not None:
                    try:
                        said = ipc.read_json(path)
                    except FileNotFoundError:
                        said = {}
                    if not isinstance(said, dict):
                        raise ValueError("invalid HALT episode store")
                entry = said.get(key)
                if isinstance(entry, list):  # old ledgers proved only the two journal rows
                    entry = dict(facts=entry, receipts=["live7_hands_halted", "minta_tolong"])
                first = entry is None
                if first:
                    entry = dict(facts=facts, receipts=[])
                if (not isinstance(entry, dict) or not isinstance(entry.get("receipts"), list)
                        or not isinstance(entry.get("facts"), list) or len(entry["facts"]) != 3):
                    raise ValueError("invalid HALT episode entry")
                prior = entry["facts"]
                if (type(prior[0]) is int and type(facts[0]) is int and prior[0] > facts[0]):
                    cache[key] = (facts, spoken, sink)
                    return False
                said[key] = entry

                def save():
                    if path is not None:
                        ipc.write_json(path, said, fsync=True)

                if prior != facts:
                    self._journal(dict(ev="live7_hands_halt_updated", at=time.time(), alert=False, halt=halt,
                                       why=hands_halt.why(halt)))
                    entry["facts"] = facts
                    save()
                alert_id = hashlib.sha256(key.encode()).hexdigest()[:32]
                why = hands_halt.why(halt)
                reason = why + ":" + key
                self._live7_halt_journal(path, entry, save, (
                    dict(ev="live7_hands_halted", at=time.time(), alert=True, alert_id=alert_id, halt=halt,
                         why=why, clears=hands_halt.CLEARS, action="voice_on_hands_held_wait_for_operator_rearm",
                         outcome_class="UNCERTAIN", disposition="RECOVER"),
                    dict(ev="minta_tolong", session_id=session.session_id, reason=reason, alert_id=alert_id,
                         channels=["owner_voice_chat", "viewers"], verified=False),
                ))
                receipts = entry["receipts"]
                destinations = {channel: fn for channel, fn in (("speech", spoken), ("help", sink)) if fn is not None}
                for channel, destination in destinations.items():
                    if channel in receipts:
                        continue
                    try:
                        accepted = self._live7_halt_delivery(session, channel, destination, alert_id, reason)
                    except Exception as exc:  # destination failure must not block recovery
                        ipc.LOG.error("FAULT halt_alert_delivery channel=%s error=%s", channel, type(exc).__name__)
                        accepted = False
                    if accepted:
                        receipts.append(channel)
                        save()
                if all(token in receipts for token in ("live7_hands_halted", "minta_tolong", *destinations)):
                    cache[key] = (facts, spoken, sink)
                else:
                    retry[key] = time.monotonic() + 5
                return first
        except (OSError, ValueError) as exc:
            ipc.LOG.error("FAULT halt_episode_store error=%s", type(exc).__name__)
            retry[key] = time.monotonic() + 5
            return False

    def _live7_halt_delivery(self, session, channel, destination, alert_id, reason):
        if channel == "speech":
            request = SimpleNamespace(reason="minta_tolong", generation_id=None, alert_id=alert_id)
            return destination(request, self._live7_help_speech()) is not False
        file_channel = self._help_channel
        if file_channel is not None:
            if file_channel.read_reply(alert_id) is not None:
                return True
            pending = file_channel.read_request(alert_id)
            if pending is not None and pending.expires_at > time.time():
                return False  # keep the existing unread request available to the relay
        with self._lock:
            request = self._live7_queue_help(session, reason, request_id=alert_id)
        if request is None:
            return False
        receipt = destination(request)
        if file_channel is not None:
            return file_channel.read_reply(alert_id) is not None
        return receipt is not False and getattr(receipt, "request_id", alert_id) == alert_id
