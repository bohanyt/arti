"""Primitives v2 WS-G3 runtime side: the owner's naming / event-question / ready-check objectives.

Kept out of ``arti_stardew_runtime.py`` (several workstreams edit it): ``StardewGoalRuntime`` inherits
:class:`TextEventRuntimeMixin` and calls a handful of hooks. Every physical step is WS-G1's leased ``ui_press``
(``_run_ui_pad``: ACQUIRE -> one guarded input_once -> RELEASE, session limit + trial input budget, strictly newer
ui.context readback), with args copied from the mod's published ``ui_pad`` block plus the G3 payload
(``text`` = the owner's name, ``choice`` = the owner's answer). The runtime never invents a payload.

Offering (PLAN 5.3 rule 8): naming / number / ready_check contexts are runtime-owned (D3: the model never types a
name; D4: the ready check is wait-only for the model); the event context is offered to the model only for the
owner's event-question objective without an owner choice. Finishing (``text_rule``): the runtime records the
verified finish itself once the objective's verified effect is in (entered name read back == the owner's name;
the event answer read back; the ready check observed then cleared), like WS1's tool_work_rule.

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G3 live witnesses.
"""
from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import arti_stardew_command as command
import arti_stardew_text_event as te


class TextEventRuntimeMixin:
    """Mixed into StardewGoalRuntime; uses its lock, stores, the G1 leased ui_press and the journal."""

    def _init_text_event_runtime(self) -> None:
        self._text_event_last_wait: str | None = None
        self._event_skip_state: dict = {"n": 0, "last_try": None, "last_why": None}

    # ------------------------------------------------------------------ FIXB31 opt-in event skip
    EVENT_SKIP_MAX = 6            # skips per runtime (a trial), bounded
    EVENT_SKIP_RETRY_S = 10.0     # a not-verified skip is not retried sooner

    def _event_skip_enabled(self) -> bool:
        return bool(self._config.get("stardew_event_skip_enabled", False))

    def process_event_skip(self) -> int:
        """Worker hook (FIXB31, owner decision 2026-10-12, runtime flag ``stardew_event_skip_enabled`` default OFF + the
        mod's EventSkipEnabled): during a plain cutscene with no question pending, ONE leased pad_button Select (the
        vanilla gamepad Back skip, a direct call in the mod). Off: returns 0 before any read. Never raises."""
        if not self._event_skip_enabled():
            return 0
        try:
            return self._event_skip_tick()
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "event_skip_error", "error": type(exc).__name__})
            return 0

    def _event_skip_tick(self) -> int:
        import time
        from arti_stardew_runtime import READY
        state = self._event_skip_state
        with self._lock:
            if (self._status != READY or self._action_busy or self._inflight_generation is not None
                    or self._day_start_active or self._worker_stop.is_set() or state["n"] >= self.EVENT_SKIP_MAX):
                return 0
            if state["last_try"] is not None and time.monotonic() - state["last_try"] < self.EVENT_SKIP_RETRY_S:
                return 0
            binding, pid = self._binding_view()
            if binding.status != "bound" or pid is None:
                return 0
            rec, _ = self._ui_key_record(pid)
            facts = rec.facts if rec is not None else None
            step, why = te.event_skip_step(facts, self._text_event_of(self._active), enabled=True)
            if step != "skip":
                if why != state["last_why"] and why not in ("no_event",):
                    state["last_why"] = why
                    self._journal({"ev": "event_skip", "status": "not_offered", "reason": why})
                return 0
            ctx = self._ui_key_context(pid)
            if ctx is None:
                return 0
            state["n"] += 1
            state["last_try"] = time.monotonic()
            state["last_why"] = None
            self._action_busy = True

        def revalidate():
            rec2, _ = self._ui_key_record(pid)
            if te.event_skip_step(rec2.facts if rec2 is not None else None, self._text_event_of(self._active),
                                  enabled=True)[0] != "skip":
                raise ValueError("event_skip_target_changed")
        try:
            self._journal({"ev": "event_skip", "status": "request", "n": state["n"]})
            try:
                run = self._run_leased_once(command.PRIMITIVE_PAD_BUTTON, dict(te.EVENT_SKIP_ARGS), ctx,
                                            origin="padbutton-event_skip", revalidate=revalidate,
                                            expect_outcome="EventSkipped")
            except Exception as exc:  # noqa: BLE001 - never let the step escape
                from arti_stardew_ui_key_runtime import LeasedRun
                run = LeasedRun("not_taken", type(exc).__name__)
        finally:
            with self._lock:
                self._action_busy = False
        self._journal({"ev": "event_skip", "status": run.lifecycle, "outcome": run.outcome, "verified": run.verified,
                       "applied": run.applied, "cleanup": run.cleanup, "reason": run.reason, "request_id": run.request_id})
        return 1

    # ------------------------------------------------------------------ flags / objective
    def _text_event_flag_configured(self) -> bool:
        """The mod's TextEventMenusEnabled: session.json carries text_event_menus_enabled only when it is on."""
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            from arti_stardew_runtime import _SESSION_READ_RETRY_SECONDS
            return "text_event_menus_enabled" in self._commands.baca_sesi(folder, retry_detik=_SESSION_READ_RETRY_SECONDS).mentah
        except Exception:  # noqa: BLE001 - unreadable session = flag unknown = no G3 objective
            return False

    def _pin_text_event_objective(self, goal, objective, request) -> None:
        goal.text_event_objective = objective
        self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "pinned": asdict(objective),
                       "origin": "owner_text_event_objective"})

    @staticmethod
    def _text_event_of(goal):
        return getattr(goal, "text_event_objective", None) if goal is not None else None

    # ------------------------------------------------------------------ offering (rule 8)
    def _text_event_owner_reason(self, goal, view) -> str | None:
        """Why the runtime owns this G3 menu (no model offer), or None (also None for non-G3 contexts)."""
        if view is None or view.context not in te.G3_CONTEXTS:
            return None
        return None if te.model_offer_allowed(self._text_event_of(goal), view) else "text_event_runtime_owned:" + view.context

    def _text_event_model_args_reason(self, view, args) -> str | None:
        return te.model_args_reason(view, args)

    def _text_event_holds_continuation(self) -> bool:
        """The ready-check objective is a wait (D4/D13: the owner on the HOST ends it): no model turn meanwhile."""
        objective = self._text_event_of(self._active)
        return (objective is not None and objective.kind == te.OBJ_READY and not objective.ready_cleared
                and objective.blocked is None and not self._owner_play_stopped and not self._worker_stop.is_set())

    # ------------------------------------------------------------------ worker hook
    def process_text_event(self) -> int:
        """Worker hook: one deterministic G3 step (or the verified rule finish). Never raises."""
        try:
            return self._text_event_tick()
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "text_event_error", "error": type(exc).__name__})
            return 0

    def _text_event_tick(self) -> int:
        with self._lock:
            goal = self._active
            objective = self._text_event_of(goal)
            if objective is None or goal.progress_marker == "terminal" or objective.finish_recorded:
                return 0
            if self._action_busy or self._inflight_generation is not None or self._day_start_active:
                return 0
            binding, pid = self._binding_view()
            if binding.status != "bound" or pid is None:
                return 0
            rec, _ = self._ui_key_record(pid)
            facts = rec.facts if rec is not None else None
            view = self._ui_pad_view(pid)
            if te.note_observation(objective, view, facts):
                self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "text_event": asdict(objective),
                               "origin": "text_event_observed"})
            step = te.plan_step(objective, view, facts)
            if step.kind == "act":
                self._action_busy = True  # no model action may start while the routine owns this menu step
            elif step.kind == "stop" and objective.blocked is None:
                objective.blocked = step.reason
                self._journal({"ev": "text_event_blocked", "goal_id": goal.goal_id, "reason": step.reason,
                               "objective": te.objective_summary(objective)})
            elif step.kind in ("wait", "model") and step.reason != self._text_event_last_wait:
                self._text_event_last_wait = step.reason
                self._journal({"ev": "text_event_wait", "goal_id": goal.goal_id, "reason": step.reason,
                               "detail": te.ready_check_wait_reason(facts)})
        if step.kind == "act":
            context = view.context if view is not None else None
            try:
                run, before = self._run_ui_pad(command.PRIMITIVE_UI_PRESS, dict(step.args), origin="text_event")
            finally:
                with self._lock:
                    self._action_busy = False
            with self._lock:
                changed = te.note_result(objective, context=context, args=step.args, verified=run.verified,
                                         effect=run.effect, readback=run.readback, uncertain=run.uncertain)
                self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "text_event": asdict(objective),
                               "origin": "text_event_" + step.reason, "changed": changed, "readback": run.readback})
            return 1
        result = self._text_event_rule_finish(goal)
        if result is not None and self._on_generation_result is not None:
            self._on_generation_result(SimpleNamespace(reason="text_event_finish", generation_id=None), result)
        return 1 if result is not None else 0

    def _text_event_note_model_run(self, goal, operation: str, args: dict, before, run) -> None:
        """A model ui_press on the event question (offered only under the objective) counts when VERIFIED."""
        objective = self._text_event_of(goal)
        if objective is None or operation != command.PRIMITIVE_UI_PRESS or run is None or before is None:
            return
        if before.context not in te.G3_CONTEXTS:
            return
        changed = te.note_result(objective, context=before.context, args=args, verified=run.verified, effect=run.effect,
                                 readback=run.readback, uncertain=run.uncertain)
        if changed:
            self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "text_event": asdict(objective),
                           "origin": "model_ui_press"})

    # ------------------------------------------------------------------ finish (text_rule)
    def _text_event_rule_finish(self, goal):
        """Terminal achieved verdict once the objective's verified effect is in, or None. Never races a model turn."""
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        with self._lock:
            objective = self._text_event_of(goal)
            if (objective is None or self._active is not goal or goal.progress_marker == "terminal"
                    or objective.finish_recorded):
                return None
            if self._action_busy or self._inflight_generation is not None or getattr(self, "_results_pending", 0):
                return None
            status, reason = te.objective_verdict(objective)
            if status != "achieved":
                return None
            verdict = FinishEvidenceView("achieved", reason, dict(te.objective_summary(objective) or {},
                                                                  origin="text_rule", readback=objective.readback))
            objective.finish_recorded = True
            goal.last_finish = verdict
            goal.progress_marker = "terminal"
            goal.revision += 1
            self._pending_continuation = None
            self._pending_event_reason = None
            self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                           "verdict": asdict(verdict), "origin": "text_rule"})
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                               speech=StatusSpeech("Selesai; terverifikasi dari readback mod."), finish_evidence=verdict)

    def _text_event_finish_verdict(self, goal):
        """FinishEvidenceView for a G3 objective on the model's finish path (verified progress only), or None."""
        objective = self._text_event_of(goal)
        if objective is None:
            return None
        from arti_stardew_runtime import FinishEvidenceView
        status, reason = te.objective_verdict(objective)
        if status == "achieved":
            objective.finish_recorded = True
        return FinishEvidenceView(status, reason, dict(te.objective_summary(objective) or {}, origin="text_rule",
                                                       readback=objective.readback))
