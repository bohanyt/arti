"""Primitives v2 WS-G4 fishing hooks for StardewGoalRuntime (mixin).

All decisions live in the pure module ``arti_stardew_fishing``; this mixin reads the existing observation/session
stores through the runtime's own helpers and keeps the objective accounting. The physical step is the unchanged
leased executor (``_execute_leased_action(..., reflex_fish=True)``: lease, guarded publication, renewal while the
mod runs the reflex, cleanup supervisor). Methods named ``_fishing_*`` / ``*fishing_finish*`` only (merge-friendly).
Evidence ceiling: UNIT_TESTED.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict

import arti_stardew_fishing as fishing


class FishingRuntimeMixin:
    """Owner objective 'catch one fish' -> one offered reflex_fish action -> fishing_rule finish."""

    # ------------------------------------------------------------------ objective / observation

    def _fishing_begin(self, goal, request) -> None:
        """Owner ingress only (anchored grammar already matched)."""
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        catch_any = bool(session and session.status == "active" and run
                         and session.mode != "part_b"
                         and run.call.name == "tangkap_ikan" and run.objective_text == goal.text)
        goal.fishing = fishing.FishingObjective(goal.goal_id, catch_any_item=catch_any)
        self._journal({"ev": "reflex_fish_objective_pinned", "goal_id": goal.goal_id,
                       "max_calls": fishing.MAX_REFLEX_CALLS})

    def _fishing_ui_facts(self, pin) -> Mapping | None:
        """Fresh (<= 2 s), complete, bound ui.context facts of the pinned player, or None."""
        if pin is None:
            return None
        rec = self._obs.get(pin.player_id, "ui.context")
        if (rec is None or not rec.is_complete or not rec.authoritative
                or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
            return None
        return rec.facts

    def _fishing_target(self, goal, binding, pin, *, session_check: bool = True) -> dict | None:
        """The offered reflex target (snapshot field), or None. Session readiness (reflex_fish_enabled, idle slot) is
        checked for the offer; the publication precheck runs under ARTI's own lease and compares the facts only."""
        objective = getattr(goal, "fishing", None) if goal is not None else None
        if not fishing.objective_open(objective):
            return None
        if self._goal_route_owns(goal):
            return None  # FIXB38 the lake walk must hand back before the fishing reflex may publish
        target = fishing.reflex_target(self._fishing_ui_facts(pin), objective)
        if target is None or (session_check and not self._water_snapshot_available(binding, pin, reflex_fish=True)):
            return None
        return target

    def _fishing_extra_entries(self, pin, motor_tile) -> list[dict]:
        """water_edge approach entries while the objective is open (journaled when they change)."""
        goal = self._active
        objective = getattr(goal, "fishing", None) if goal is not None else None
        entries = fishing.water_edge_entries(self._fishing_ui_facts(pin), motor_tile, objective)
        refs = tuple(e["ref"] for e in entries)
        if refs and refs != getattr(self, "_last_water_edge_refs", ()):
            self._last_water_edge_refs = refs
            self._journal({"ev": "reflex_fish_water_edge_offered", "goal_id": goal.goal_id, "refs": list(refs)})
        return entries

    def _fishing_rod_facts(self, goal, pin) -> dict | None:
        """D-B2-4/5: rod held / rod slot for the open objective's snapshot summary (None otherwise)."""
        objective = getattr(goal, "fishing", None) if goal is not None else None
        if not fishing.objective_open(objective):
            return None
        return fishing.rod_facts(self._fishing_ui_facts(pin))

    @staticmethod
    def _fishing_offer_payload(target: Mapping) -> dict:
        return {
            "target_ref": None,
            "observed_target": dict(target),
            "args": ("{} (runtime fills defaults) or a subset of {max_s 1-90, cast_power 0.30-0.95, treasure bool,"
                     " hook_retries 0-2}"),
            "result_scope": fishing.RESULT_SCOPE,
        }

    # ------------------------------------------------------------------ settlement

    def _fishing_record_result(self, goal, evidence, envelope: Mapping | None, superseded: bool) -> None:
        """Under the lock, after a submitted reflex call. Never replays; doubt latches the global stop."""
        objective = getattr(goal, "fishing", None)
        if objective is None:
            return
        before = objective.status
        fresh = not evidence.request_id or evidence.request_id not in objective.request_ids
        goal.fishing = fishing.settle(objective, request_id=evidence.request_id, lifecycle=evidence.lifecycle,
                                      cleanup=evidence.cleanup_confirmed and not superseded, envelope=envelope)
        self._journal({"ev": "reflex_fish_result", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                       "lifecycle": evidence.lifecycle, "objective_status": goal.fishing.status,
                       "objective_reason": goal.fishing.reason, "reflex_calls": goal.fishing.calls,
                       **fishing.journal_fields(envelope)})
        if fresh:
            for record in fishing.ground_receipts(envelope, evidence):
                self._journal(record)
            if (catch := fishing.landed_catch(envelope, evidence)) is not None:
                self._journal(catch)
        if goal.fishing.status == "CAUGHT" and before != "CAUGHT":
            self._journal({"ev": "reflex_fish_caught", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                           "fish": goal.fishing.fish})
        if goal.fishing.status == "UNCERTAIN":
            self._gather_mutation_unresolved = True

    # ------------------------------------------------------------------ finish (model path + D35-style rule)

    def _fishing_finish_verdict(self, goal):
        """FinishEvidenceView for a fishing objective (verified progress only), or None for other goals."""
        objective = getattr(goal, "fishing", None) if goal is not None else None
        verdict = fishing.finish_verdict(objective)
        if verdict is None:
            return None
        from arti_stardew_runtime import FinishEvidenceView
        return FinishEvidenceView(verdict[0], verdict[1], {**fishing.catch_observed(objective), "objective": fishing.OBJ_CATCH_FISH, "fish": objective.fish,
                                                           "request_id": objective.caught_request_id,
                                                           "reflex_calls": objective.calls})

    def fishing_finish_due(self) -> bool:
        """Caller holds the runtime lock (the trial runner's idle check): a verified catch awaits the worker's rule
        finish, so the runner must not declare the goal held meanwhile."""
        goal = self._active
        objective = getattr(goal, "fishing", None) if goal is not None else None
        return (objective is not None and goal.progress_marker != "terminal"
                and objective.status in ("CAUGHT", "BLOCKED")
                and not self._gather_mutation_unresolved)

    def process_fishing_finish(self) -> int:
        """Worker hook: once the reflex reported a verified FishCaught (cleanup confirmed, settled), the runtime records
        the goal finish itself (fishing_rule -> observed_fish_caught), like WS1's tool_work_rule. The model finish
        path is unchanged; this skips a goal that is already terminal."""
        try:
            result = self._fishing_rule_finish()
            if result is not None and self._on_generation_result is not None:
                from types import SimpleNamespace
                self._on_generation_result(SimpleNamespace(reason="reflex_fish_finish", generation_id=None), result)
            return 1 if result is not None else 0
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            self._journal({"ev": "reflex_fish_finish_error", "error": type(exc).__name__})
            return 0

    def _fishing_rule_finish(self):
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        with self._lock:
            goal = self._active
            objective = getattr(goal, "fishing", None) if goal is not None else None
            if objective is None or goal.progress_marker == "terminal":
                return None
            if self._action_busy or self._inflight_generation is not None or getattr(self, "_results_pending", 0):
                return None
            if objective.status == "BLOCKED":
                # FIXB20 D-B17-3: the mod refuses the cast on the edge for a reason walking cannot cure: partial
                verdict = FinishEvidenceView("partial", objective.reason, {
                    "origin": fishing.RULE_ORIGIN, "objective": fishing.OBJ_CATCH_FISH, "reflex_calls": objective.calls,
                    "edge": (getattr(goal, "fishing_edge", None) or {}).get("ref")})
                goal.last_finish = verdict
                goal.progress_marker = "terminal"
                goal.revision += 1
                self._pending_continuation = None
                self._pending_event_reason = None
                self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                               "verdict": asdict(verdict), "origin": fishing.RULE_ORIGIN})
                from arti_stardew_runtime import OwnerTurnResult as _R
                return _R(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                          speech=StatusSpeech("Memancing berhenti: " + objective.reason + "."),
                          finish_evidence=verdict)
            if objective.status != "CAUGHT":
                return None
            if self._gather_mutation_unresolved:
                return None
            action = goal.last_action
            if action is None or action.request_id != objective.caught_request_id or not action.cleanup_confirmed:
                return None
            verdict = FinishEvidenceView("achieved", objective.reason, {
                **fishing.catch_observed(objective),
                "origin": fishing.RULE_ORIGIN, "objective": fishing.OBJ_CATCH_FISH, "fish": objective.fish,
                "request_id": objective.caught_request_id, "reflex_calls": objective.calls})
            goal.last_finish = verdict
            goal.progress_marker = "terminal"
            goal.revision += 1
            self._pending_continuation = None
            self._pending_event_reason = None
            self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                           "verdict": asdict(verdict), "origin": fishing.RULE_ORIGIN})
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                               speech=StatusSpeech(fishing.catch_speech(objective)), finish_evidence=verdict)
