"""The real Live7 skill-session boundary over a synthetic native read (offline only).

``SimRuntime`` subclasses the shipped ``SkillRuntimeMixin``: the real
``process_skill_boundary`` -> ``_live7_leg``/``DayPolicy.record`` ->
``_live7_advance`` -> ``_skill_model_choose`` -> ``_skill_boundary_request`` ->
``day_plan``/``_live7_enrich_offer`` -> production ``render_skill_messages`` trim ->
``live7.choice`` path runs unchanged. Only the native seams are doubles:

* the read (facts, wave-1 context, motor) comes from ``world.World``;
* ``_skill_start`` records the call instead of publishing input; the harness then
  applies ``World.execute`` and sets the run's terminal outcome;
* ``_begin_generation`` builds a ``GenerationRequest`` without the bridge;
* the provider is a ``ProductionDecisionProvider`` whose physical call is replaced
  by a stub policy. Its real ``_render_messages`` (8192-token reserve trim) decides
  which offers the stub, and ``_skill_rendered_calls``, can see.

No command IO, game, SMAPI, network or model is touched.
"""
from __future__ import annotations

from decimal import Decimal
import json
import threading
import time
from types import SimpleNamespace as NS

import arti_stardew_live7 as L
import arti_stardew_part_a as A
import arti_stardew_skills as S
import arti_stardew_skills_runtime as SR
from arti_stardew_interact_b_runtime import RoutineRuntimeMixin
from arti_stardew_runtime import (DecisionProviderPolicy, GenerationRequest, OwnerTurnResult,
                                  ProductionDecisionProvider, StardewGoalRuntime,
                                  _decision_provider_extra_payload, _encoded_provider_request,
                                  _skill_prompt_token_estimate)

from .world import IDENTITY

CLEAN = dict(menu=None, free=True, input_released=True, food_clear=True)
MODEL = "gpt-6-luna"  # rehearsal trial-config model id; only its length reaches the byte budget


class StubProvider(ProductionDecisionProvider):
    """Production render/trim + a deterministic policy instead of the physical text call."""

    def __init__(self, policy, runtime, *, model=MODEL):  # noqa: D107 - no transport state is created
        self.policy = DecisionProviderPolicy(profile="sim", provider_kind="sim", endpoint="sim://none",
                                             model=model, api_key="", free_profile=True,
                                             budget_usd=Decimal("0"), worst_case_cost_usd=Decimal("0"))
        self.choose = policy
        self.runtime = runtime
        self._lock = threading.Lock()
        self._request_indices = {}
        self._committed_cost_usd = Decimal("0")
        self._physical_requests = 0
        self.trial_owner_authorized = False

    def complete(self, request, *, start_fence=None):
        if start_fence is not None:
            start_fence(lambda: None)
        messages = self._render_messages(request)
        rendered = json.loads(messages[1]["content"])
        encoded = _encoded_provider_request(self.policy.model, messages,
            _decision_provider_extra_payload(request.snapshot.working_memory.play_mode == "local_activity", messages=messages))
        with self._lock:
            self._physical_requests += 1
            self._request_indices[request.generation_id] = self._physical_requests
        rt = self.runtime
        view = View(rendered, request.skill_offer, rt.world.clock, rt.world.location, rt.world.energy)
        decision = self.choose(view)
        rt.boundaries.append(dict(
            kind="model", request_index=self._physical_requests, day=rt.world.day, clock=rt.world.clock,
            location=rt.world.location, energy=rt.world.energy,
            planner=[(c.name, c.arg) for c in rt.last_calls],
            rendered=[(e["name"], list(e.get("args", ()))) for e in rendered["skills"]],
            choice=decision, render_bytes=len(messages[1]["content"]), seen=rendered,
            input_token_bound=_skill_prompt_token_estimate(encoded),
            next_step=(rendered.get("day") or {}).get("crops", {}).get("next_step")))
        return json.dumps(game_decision(request, decision), ensure_ascii=False)


class View:
    """What the stub model sees: exactly the rendered (possibly trimmed) offer."""

    def __init__(self, rendered, offer, clock, location, energy):
        self.rendered, self.offer = rendered, offer
        self.clock, self.location, self.energy = clock, location, energy
        self.skills = {e["name"]: list(e.get("args", ())) for e in rendered["skills"]}
        self.day = rendered.get("day") or {}

    def has(self, name, arg=None):
        return name in self.skills and (arg is None or arg in self.skills[name])

    def args(self, name):
        return self.skills.get(name, [])


def game_decision(request, decision):
    """GameDecision.v1 JSON in the provider's reply shape (see provider_archive responses)."""
    kind = decision[0]
    if kind == "act":
        args = {"name": decision[1]}
        if len(decision) > 2 and decision[2] is not None:
            args["arg"] = decision[2]
        choice = dict(kind="act", operation=S.SKILL_OPERATION, target_ref=None, args=args, evidence_refs=[])
    elif kind == "finish":
        choice = dict(kind="finish", scope=decision[1] if len(decision) > 1 else "goal")
    elif kind == "ask_owner":
        choice = dict(kind="ask_owner", basis="optional", wait_ms=None)
    else:  # "invalid": an unoffered pick that live7.choice must refuse
        choice = dict(kind="act", operation=S.SKILL_OPERATION, target_ref=None,
                      args={"name": "tidak_ada"}, evidence_refs=[])
    return dict(v=1, request_nonce=request.request_nonce, goal_id=request.goal_id,
                goal_revision=request.goal_revision, context_ref=request.context_ref,
                speech="", choice=choice, activity=None)


class SimRuntime(SR.SkillRuntimeMixin):
    """Real skill/Live7 mixins over a synthetic read; mirrors tests' ``Boundary`` double."""
    _curfew_home_path = staticmethod(RoutineRuntimeMixin._curfew_home_path)
    _revoke_play = StardewGoalRuntime._revoke_play
    _supersede_generation = StardewGoalRuntime._supersede_generation
    _request_current = StardewGoalRuntime._request_current

    def __init__(self, world, policy, *, config=None, prompt_padding=""):
        self.world = world
        self.prompt_padding = prompt_padding  # synthetic context pressure on the production render reserve
        self._lock = threading.RLock()
        self._worker_wakeup = threading.Condition(self._lock)
        self._worker_stop = threading.Event()
        self._action_cancel = threading.Event()
        self._skills_enabled = True
        self._status = "READY"
        self._config = dict(config or {})
        self._skill_session = SR.SkillSession("live7-sim", "part_live", L.GOAL, "ptt", None)
        self._skill_session.goal_id = "session"
        self._physical_action_publications = 0
        self._action_session_limit = None
        self._trial_deadline_unix_ms = None
        self._trial_admission = None
        self._action_busy = False
        self._inflight_generation = None
        self._results_pending = 0
        self._day_start_active = False
        self._owner_play_stopped = False
        self._pending_continuation = None
        self._pending_event_reason = None
        self._gather_mutation_unresolved = False
        self._skill_emits = []
        self._skill_day = dict(date=None, skills=0, model=0)
        self._skill_ended_goals = []
        self._skill_history = []
        self._current_generation = 0
        self._logical_request = None
        self._active = NS(goal_id="session", revision=1, play_identity=IDENTITY, last_action=None,
                          last_finish=None, approach_progress=None, approach_blocked_refs=set(),
                          mines_exit=None, routine_objective=None, play_mode=None)
        self._motor = world.motor()
        self._state = NS(latest=lambda pid: self._read_motor(), is_stale=lambda pid: False)
        self._obs = NS(get=lambda pid, domain: NS(received_monotonic=self._frame_time()))
        self._provider = StubProvider(policy, self)
        self.events = []
        self.boundaries = []   # model boundaries (offers + choice), see StubProvider.complete
        self.starts = []       # every started call, by model or plan
        self.last_calls = []
        self._wet_soil = {}   # tw.wet_soil_view memory, as on StardewGoalRuntime
        self.wet_soil = True

    # ------------------------------------------------------------------ synthetic native read
    def _frame_time(self):
        live = getattr(getattr(self._skill_session, "campaign", None), "live", None)
        return max(time.monotonic(), (getattr(live, "release_after", None) or 0) + .001)

    def _read_motor(self):
        self.world.motor(self._motor)
        self._motor.diterima_pada = self._frame_time()
        return self._motor

    def _binding_view(self):
        return NS(status="bound"), -1

    def _date_tuple(self, motor):
        return motor.year, motor.season, motor.day

    def _skill_current_facts(self):
        self._read_motor()
        return self.world.facts()

    def _skill_facts(self, snapshot):
        return self.world.facts()

    def _skill_wave1_context(self, snapshot):
        context = self.world.context()
        if self.world.stale_scene:
            # Same merge as production _skill_wave1_context: global scene, then the fresh local window
            # through tw.wet_soil_view (``wet_soil=False`` replays the pre-fix plain dict merge).
            from arti_stardew_tool_work import wet_soil_view
            scene, window = context["targets"], (context.get("local_window") or {}).get("targets")
            if self.wet_soil:
                motor = self._read_motor()
                context["targets"] = wet_soil_view(self._wet_soil, self.world.location,
                                                   (motor.year, motor.season, motor.day), scene, window)
            else:
                context["targets"] = {**scene, **(window or {})}
            self.world.view = context["targets"]
        return context

    def _ui_pad_view(self, pid):
        return None

    def _build_decision_snapshot(self, goal):
        # A Live7 session goal holds a local_activity play grant: its strict schema carries the activity object
        # (rehearsal 11 provider archive), which is ~1 KB of the 8192-token render reserve.
        return NS(motor=self._read_motor(), binding=NS(player_id=-1),
                  context_ref="ctx-%032x" % self._current_generation,
                  working_memory=NS(play_mode="local_activity", last_action=None), routine_objective=None)

    def _live7_calls(self, session, snapshot, facts):
        self.last_calls = super()._live7_calls(session, snapshot, facts)
        return self.last_calls

    # ------------------------------------------------------------------ generation / provider seam
    def _begin_generation(self, *, text, trigger_type, viewer_name, reason, replace_goal):
        self._supersede_generation()
        self._current_generation += 1
        generation_id = "sim-gen-%d" % self._current_generation
        self._inflight_generation = generation_id
        snapshot = self._build_decision_snapshot(self._active)
        request = GenerationRequest(
            generation_id=generation_id, request_nonce="nonce-%012x" % self._current_generation,
            goal_id=self._active.goal_id, goal_revision=self._active.revision, context_ref=snapshot.context_ref,
            text=text, trigger_type=trigger_type, viewer_name=viewer_name,
            reason=reason + self.prompt_padding, snapshot=snapshot)
        self._logical_request = request
        return request

    # ------------------------------------------------------------------ executor seam
    def _cancel_action(self):
        self._action_cancel.set()

    def _journal(self, row):
        self.events.append(dict(row, sim=dict(day=self.world.day, clock=self.world.clock,
                                              location=self.world.location)))

    def _skill_flush(self):
        with self._lock:
            self._skill_emits = []

    def _trial_result_pending(self):
        return False

    def _skill_chest_close_due(self, session, run):
        return None

    def _skill_run_outcome(self, session, run):
        return run.pending

    def skill_terminal_cleanup(self):
        if self.world.held:
            # A cursor-held item keeps the GameMenu open: input cannot be released to a free player.
            return dict(CLEAN, menu="GameMenu", free=False, location=self.world.location)
        return dict(CLEAN, location=self.world.location)

    def skill_post_row_state(self):
        return self.skill_terminal_cleanup()

    def _skill_door_wait(self, *args):
        return False

    def _skill_session_goal(self, session):
        self._status = "READY"
        self._active.play_identity = IDENTITY
        self._active.last_action = self._active.last_finish = None
        return OwnerTurnResult(admitted=True)

    def _skill_start(self, session, call, *, by, facts=None, resume=None):
        run = SR.SkillRun(call, "SIM", by, time.monotonic(), self._physical_action_publications,
                          goal_id="goal-%012x" % (len(session.runs) + 1), status="running")
        run.state.update(campaign_task=session.campaign_start, live7=True,
                         campaign_travel=call.name in ("pergi_ke", "jalan_ke"))
        session.runs.append(run)
        session.current = run
        session.campaign.starts += 1
        self._active.goal_id = run.goal_id
        self._active.last_finish = self._active.last_action = None
        self.starts.append(dict(day=self.world.day, clock=self.world.clock, location=self.world.location,
                                call=(call.name, call.arg), by=by))
        return run

    def execute_current(self):
        """Apply the started run in the synthetic world and leave its terminal outcome pending."""
        run = self._skill_session.current
        outcome, observed = self.world.execute(run.call)
        if run.call.name == "tanam" and observed.get("tile"):
            # The physical executor double retains the native tile selection just as S9._extras does.
            run.state["plant_target"] = dict(tile=tuple(observed["tile"]))
        self._physical_action_publications += 1
        run.pending = outcome
        self._active.last_finish = NS(observed=observed)
        self.starts[-1].update(outcome=outcome, end_clock=self.world.clock, end_location=self.world.location)
        return outcome
