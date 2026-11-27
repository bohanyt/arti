"""Skills tahap 1 runtime side (PLAN v2 §7): skill sessions, the in-skill deterministic driver, skill boundaries.

``StardewGoalRuntime`` inherits :class:`SkillRuntimeMixin` (appended last, after CombatRuntimeMixin) and calls four hooks:

* ``_skill_owner_turn`` (top of ``decide_owner_turn``): an authenticated owner grant whose text maps to a skill plan
  (``arti_stardew_skills.parse_skill_goal``) starts a session; unknown text starts a free session where the model
  chooses skills; text only a non-skill legacy grammar parses passes through unchanged.
* ``_skill_invoke`` (top of ``_invoke_provider``): every decision of a running skill's goal comes from the skill's
  deterministic step program (``plan_step``) instead of the provider. Same admission fence (``_start_provider``),
  same codec, play gate, prechecks and leased publication as a model decision: only the chooser changes.
* ``process_skill_boundary`` (worker loop): a finished / failed / stalled skill ends; the next planned skill starts
  (a fresh goal through the unchanged owner ingress with the skill's canonical objective text), or the model is
  asked ONCE for the next skill (skills-only catalog, no buttons), or the session ends.
* ``skill_boundary_due`` / ``skill_report`` (trial runner).

Each skill = one goal; its objective, rules and auto acts are the existing ones. The model is never called inside a
skill. Budgets: per skill (decisions / inputs / seconds from the registry), per session and per in-game day
(skills, model decisions). Flag ``stardew_skills_enabled`` (default False): every hook returns immediately when off.

Evidence ceiling: UNIT_TESTED (simulated native). HYPOTHESIS until a skills live witness.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

from arti_runtime import page_budget as _page_budget

import json
import re
from copy import copy, deepcopy
import threading
import time
from arti_stardew_pause import game_now, wait_resume
import traceback
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from arti_stardew_command import lease_state
from types import SimpleNamespace

import arti_stardew_skills as skills
from arti_stardew_part_a import PartACampaignMixin, recognizes as part_a_goal, machine_input
from arti_stardew_part_b import recognizes as part_b_goal
from arti_stardew_part_c import recognizes as part_c_goal
from arti_stardew_live7 import Live7CampaignMixin, recognizes as live7_goal
from arti_stardew_interact_b import eat_consumption_observed
from arti_stardew_interact import native_held_tool
from arti_stardew_debris_route_runtime import DebrisRouteRuntimeMixin
from arti_runtime.action_outcomes import unreadable as unreadable_outcome, UNKNOWN_TEXT

_RESOURCE_GOAL = (r"(?:kumpulkan|gather) ([1-9][0-9]{0,2}) (kayu|wood|batu|stone)"
                  r"( kemudian simpan| lalu simpan| and store)?[.!]?")
_VIEW_RADIUS = 12
_REPORT_KEEP = 4
_STALL_REFRESHES = 3  # bounded read-only refreshes before an idle skill counts as stalled
TILL_TURNS_MAX = 3  # face attempts (edge nudge included) from one stand toward one tillable tile
_EDGE_MARGIN_PX = 10  # a 1-tick press moves ~5 px; closer than this to the faced edge it crosses into the tile
_EDGE_PX_PER_TICK = 5.0


def _edge_nudge(pixel, here, tile):
    """A short press AWAY from ``tile`` when ARTI's bounding-box centre is within _EDGE_MARGIN_PX of the edge she
    must face, so the following 1-tick turn changes facing without walking onto ``tile``; None when not needed.

    Canonical motor pixel = Farmer.Position; TilePoint uses the 48x32 bounding-box centre (+32,+16). Map-independent:
    only the fresh pose and the adjacent candidate are used."""
    if (not isinstance(pixel, (list, tuple)) or len(pixel) != 2 or here is None or tile is None
            or not all(isinstance(v, (int, float)) for v in pixel)):
        return None
    dx, dy = tile[0] - here[0], tile[1] - here[1]
    if abs(dx) + abs(dy) != 1:
        return None
    cx, cy = pixel[0] + 32, pixel[1] + 16
    if (int(cx // 64), int(cy // 64)) != tuple(here):
        return None  # inconsistent geometry: keep the plain turn
    margin = {(1, 0): (here[0] + 1) * 64 - cx, (-1, 0): cx - here[0] * 64,
              (0, 1): (here[1] + 1) * 64 - cy, (0, -1): cy - here[1] * 64}[(dx, dy)]
    if margin >= _EDGE_MARGIN_PX:
        return None
    away = {(1, 0): "left", (-1, 0): "right", (0, 1): "up", (0, -1): "down"}[(dx, dy)]
    ticks = max(1, min(6, int((24 - margin) / _EDGE_PX_PER_TICK + 0.999)))
    return skills.act("world_key.move_cardinal", {"direction": away, "ticks": ticks})


def _moved_progress(action, seen):
    """A25 D1 wood (journal 2759-2905): the axe select, then the bouncing drops' churning refs, spent all
    three stall refreshes of one tebang run while ARTI was still walking to the wood (48,9 -> 46,9).
    A new completed, cleaned-up input that moved ARTI is progress; the skill's own caps still bound it."""
    before, after = skills._g(action, "before") or {}, skills._g(action, "after") or {}
    tile, moved_to = before.get("tile"), after.get("tile")
    return (action is not None and skills._g(action, "lifecycle") == "completed"
            and skills._g(action, "cleanup_confirmed") is True and bool(skills._g(action, "request_id"))
            and skills._g(action, "request_id") != seen and tile is not None and moved_to is not None
            and before.get("location") == after.get("location") and list(tile) != list(moved_to))
_START_STUCK_S = 30.0  # a run still "starting" this long (its ingress never returned) fails
_ENDED_GOALS_KEEP = 64
# C13 (ADR 249): a composition's final exit verdict gives no travel credit to its work legs.
PLACE_CRITERIA = ("location", "goal_tile", "approach_arrived")


def reaches_place(spec) -> bool:
    """True when the skill's own finish criterion is reaching a place (C13); an unknown spec keeps the old credit."""
    reasons = getattr(spec, "finish_reasons", None)
    return reasons is None or any(word in str(r) for r in reasons for word in PLACE_CRITERIA)
TV_LEAVE_CLOSE_S = 2.0  # S29: an owned TV question's verified Leave closes natively; read-only wait for that close


@dataclass(frozen=True)
class SkillPin:
    """Internal owner-ingress override for a skill start (never built from model or scene text). ``session_id`` is
    re-checked inside decide_owner_turn's locked section: a stop / new grant in the gap refuses the start."""
    session_id: str
    episode_plan: tuple | None = None
    store_quality: int = 0
    goods_slot: int | None = None
    goods_quality: int | None = None


@dataclass
class SkillRun:
    call: skills.SkillCall
    objective_text: str
    by: str                           # owner | plan | model
    started_at: float
    inputs_start: int
    goal_id: str | None = None
    status: str = "starting"          # starting | running | finished | failed | unknown | skipped
    verdict: str | None = None
    reason: str | None = None
    decisions: int = 0
    foreign_model_calls: int = 0      # provider calls for another goal while this skill ran (must stay 0)
    pending: tuple | None = None      # (done|fail, reason) from the step program
    idle_since: float | None = None
    ended_at: float | None = None
    stage_start: int | None = None    # FIXB12 D-B9-4: the stage-press counter when the skill first decided
    stage_inputs: int = 0             # FIXB12 D-B9-4: locked-stage presses during this skill (not its own inputs)
    inputs: int = 0
    state: dict = field(default_factory=dict)
    retry_of: int | None = None
    wall_started: float = field(default_factory=time.time)  # W1b B5 (ADR 250): wall clock, for the bench's focus bracket

    def report(self) -> dict:
        out = {"skill": self.call.name, "arg": self.call.arg, "by": self.by, "status": self.status,
               "decisions": self.decisions, "inputs": self.inputs, "model_calls_inside": self.foreign_model_calls}
        if self.verdict:
            out["verdict"] = self.verdict
        if self.reason:
            out["reason"] = self.reason
        if self.ended_at is not None:
            out["seconds"] = round(self.ended_at - self.started_at, 2)
        if "result_observation" in self.state:
            out["result_observation"] = self.state["result_observation"]
        if "post_row_state" in self.state:
            out["post_row_state"] = dict(self.state["post_row_state"])
        if "plan_input" in self.state:
            out["plan_input"] = dict(self.state["plan_input"])
        for key in ("native_ui_episode", "terminal_cleanup_attempts"):
            if key in self.state:
                out[key] = self.state[key]
        if "campaign_task" in self.state:
            out["task"] = self.state["campaign_task"]
            out["legs"] = self.state.get("campaign_evidence", [])
        if "alive_exit" in self.state:
            out["alive_exit"] = self.state["alive_exit"]
        if "mines_exhaustion" in self.state:
            out["mines_exhaustion"] = dict(self.state["mines_exhaustion"])
        if "mines_eat_refused" in self.state:
            out["mines_eat_refused"] = self.state["mines_eat_refused"]
        if isinstance(self.state.get("ore"), dict):
            # W1 gap B5 (S23): native node/ore identities, bag before/after and the removal request only.
            ore = self.state["ore"]
            out["ore"] = {k: ore[k] for k in ("phase", "node", "node_qid", "ore", "floor", "bag_before", "evidence")
                          if k in ore}
        if "ore_exhaustion" in self.state:
            out["ore_exhaustion"] = dict(self.state["ore_exhaustion"])
        if "attempt" in self.state:
            out["attempt"] = {k: v for k, v in self.state["attempt"].items() if k not in ("survey", "walk_from")}
        if "donation_evidence" in self.state:
            out["donation_evidence"] = dict(self.state["donation_evidence"])
        if "animal_pet_status" in self.state:
            out["animal_pet_status"] = self.state["animal_pet_status"]
            out["animal_pet_observed"] = dict(self.state.get("animal_pet_observed", {}))
        if "route_clear_evidence" in self.state:
            out["route_clear_evidence"] = list(self.state["route_clear_evidence"])
        return out


@dataclass
class SkillSession:
    session_id: str
    mode: str                         # plan | free
    goal_text: str
    trigger_type: str
    viewer_name: str | None
    plan: list = field(default_factory=list)
    composite: str | None = None
    index: int = 0
    runs: list = field(default_factory=list)
    current: SkillRun | None = None
    goal_id: str | None = None        # free mode: the session goal awaiting the first model choice
    status: str = "active"            # active | finished | failed | held | stopped
    verdict: str | None = None
    reason: str | None = None
    model_decisions: int = 0
    started_at: float = 0.0
    retried: set = field(default_factory=set)
    advancing: int = 0                # >0 while a thread starts the next skill / asks the model (boundary waits)
    binding_goal: bool = False        # the next owner-ingress generation is the session goal (no objective)
    not_ready_since: float | None = None
    question: str | None = None       # the model's ask_owner speech (surfaced once, then the session holds)
    blocked_refs: set = field(default_factory=set)  # D-B7-4: approach targets blocked in this session's skill goals
    blocked_basis: tuple | None = None  # FIXB9 review: (date, location) the blocked refs were learned in
    blocked_runs: int = 0  # FIXB9 review: skills closed since the blocked refs were last reset
    farm_entry_attempted: dict = field(default_factory=dict)
    farm_work_started: dict = field(default_factory=dict)
    campaign: object | None = None
    campaign_offer: object | None = None
    campaign_start: str | None = None
    model_pick: tuple | None = None   # Live7 WP4: (SkillCall, request_index) of the last model choice, until started
    festival_leave_pick: tuple | None = None  # (chosen trip, festival id) from the decision's own page
    choice_feedback: dict | None = None  # last rejected boundary choice; facts for a fresh offer, never permission
    boundary_page: tuple | None = None
    boundary_budget: int = 16128
    boundary_reads: int = 0  # fix56B: read-only pages taken inside the current decision
    notes_read: set = field(default_factory=set)  # digests of the note rows she has read this session
    notes_unread: list | None = None  # the unread rows behind the page being asked
    page_budget: _page_budget.PageBudget = _page_budget.DEFAULT

    def report(self) -> dict:
        return {"session_id": self.session_id, "mode": self.mode, "composite": self.composite,
                "status": self.status, "verdict": self.verdict, "reason": self.reason,
                "model_decisions": self.model_decisions,
                "skills_run": sum(r.status in ("finished", "failed", "unknown") for r in self.runs),
                "skills_finished": sum(r.status == "finished" for r in self.runs),
                "runs": [r.report() for r in self.runs],
                **({"campaign": self.campaign.report()} if self.campaign is not None else {})}


# D-B7-4: a skill failure that leaves nothing to act on ends the session without a model remedy / ask_owner.
_TERMINAL_FAILURES = frozenset({"no_tree", "input_envelope_exhausted"})

class SkillRuntimeMixin(DebrisRouteRuntimeMixin, Live7CampaignMixin, PartACampaignMixin):
    """Mixed into StardewGoalRuntime; uses its lock, snapshot builder, owner ingress, admission fence and journal."""

    def _init_skill_runtime(self) -> None:
        self._skills_enabled = self._config.get("stardew_skills_enabled") is True
        self._skill_session: SkillSession | None = None
        self._skill_history: list = []
        self._skill_day = {"date": None, "skills": 0, "model": 0}
        self._skill_emits: list = []
        self._skill_delivery_pending = 0
        self._skill_ended_goals: list = []  # goals of ended sessions: never answered by the provider again

    def _skill_pin_current(self, pin) -> bool:
        """decide_owner_turn (under the lock): the skill start / session goal still belongs to the live session."""
        session = self._skill_session  # (PAUSED_RECOVERY at the first owner turn is reset by decide_owner_turn)
        return (self._skills_enabled and session is not None and session.status == "active"
                and session.session_id == pin.session_id
                and (session.binding_goal or (session.current is not None and session.current.status == "starting")))

    @staticmethod
    def _skill_own_goals(session) -> set:
        return {r.goal_id for r in session.runs if r.goal_id} | ({session.goal_id} - {None})

    def _skill_emit(self, reason: str, result) -> None:
        """Queue a trial-runner result (caller holds the lock); delivered by _skill_flush outside the lock."""
        self._skill_emits.append((reason, result))

    def _skill_flush(self) -> None:
        with self._lock:
            items, self._skill_emits = self._skill_emits, []
            # A terminal session is no longer a due boundary. The trial observer
            # must still wait until these outside-lock callbacks reach its queue.
            self._skill_delivery_pending = getattr(self, "_skill_delivery_pending", 0) + len(items)
            help_items, self._help_pending = getattr(self, "_help_pending", []), []
            help_sink = getattr(self, "_help_sink", None)
        try:
            for reason, result in items:
                self._emit_rule_result(reason, None, result)
        finally:
            with self._lock:
                self._skill_delivery_pending -= len(items)
        self._live7_flush_help(help_items, help_sink)
        self._live7_flush_moments()

    # ------------------------------------------------------------------ config / budgets
    def _skill_cfg(self, key: str, default: int, lo: int, hi: int) -> int:
        raw = self._config.get(key, default)
        return max(lo, min(hi, raw)) if type(raw) is int else default

    def _skill_unlocked(self, goal_text=None) -> frozenset:
        raw = self._config.get("stardew_skills_unlocked") or ()
        configured = (frozenset(x for x in raw if isinstance(x, str) and x in skills.REGISTRY)
                      if isinstance(raw, (list, tuple)) else frozenset())
        session = self._skill_session
        text = goal_text if goal_text is not None else session.goal_text if session is not None else ""
        if live7_goal(text):
            from arti_stardew_live7 import NAMES
            return configured | NAMES
        return configured | skills.wave1_goal_unlocks(text)

    def _skill_day_roll(self) -> dict:
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        date = self._date_tuple(motor) if motor is not None else None
        if date is not None and date != self._skill_day["date"]:
            self._skill_day = {"date": date, "skills": 0, "model": 0}
        return self._skill_day

    def _skill_budget_left(self, session: SkillSession) -> dict:
        day = self._skill_day_roll()
        if session.mode == "part_live":
            return dict(model_left=float("inf"), skills_left=float("inf"))
        return {"model_left": max(0, min(
                    self._skill_cfg("stardew_skill_session_max_model", 6, 0, 24) - session.model_decisions,
                    self._skill_cfg("stardew_skill_day_max_model", 24, 0, 240) - day["model"])),
                "skills_left": max(0, min(
                    self._skill_cfg("stardew_skill_session_max_skills", 8, 1, 24)
                    - (session.campaign.starts if session.campaign is not None else
                       sum(r.status != "skipped" for r in session.runs)),
                    self._skill_cfg("stardew_skill_day_max_skills", 24, 1, 240) - day["skills"]))}

    # ------------------------------------------------------------------ facts
    def _skill_facts(self, snapshot) -> skills.SkillFacts:
        motor = snapshot.motor
        pid = snapshot.binding.player_id
        inventory, toolbar, equipped, can, free = {}, [], None, None, None
        rec = self._obs.get(pid, "progress.inventory") if pid is not None else None
        pin = (getattr(snapshot, "water_context", None) or getattr(snapshot, "movement_context", None)
               or self._ui_key_context(pid))
        if (rec is None or pin is None or not rec.authoritative or not rec.is_complete
                or not rec.available or rec.stale or rec.requires_resync
                or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
            rec = None
        facts = rec.facts if rec is not None and isinstance(rec.facts, Mapping) else {}
        active = (facts.get("meta:inventory") or {}).get("active_slot")
        slots = [f for k, f in facts.items() if str(k).startswith("slot:") and isinstance(f, Mapping)]
        if slots:
            free = sum(f.get("occupied") is not True for f in slots)
        for f in slots:
            qid = f.get("qualified_item_id")
            if f.get("occupied") is True and isinstance(qid, str):
                inventory[qid] = inventory.get(qid, 0) + (f.get("stack") if type(f.get("stack")) is int else 1)
                if f.get("slot") == active:
                    equipped = qid
                if qid == "(T)WateringCan" and type(f.get("tool_water_left")) is int and type(f.get("tool_water_max")) is int:
                    can = (f["tool_water_left"], f["tool_water_max"])
        ui = self._obs.get(pid, "ui.context") if pid is not None else None
        ui_facts = ui.facts if ui is not None and isinstance(ui.facts, Mapping) else {}
        for target in ((ui_facts.get("toolbar") or {}).get("targets") or ()):
            if isinstance(target, Mapping) and isinstance(target.get("qualified_item_id"), str):
                toolbar.append(target["qualified_item_id"])
        if equipped and equipped not in toolbar:
            toolbar.append(equipped)
        trees = stones = chests = dry = 0
        drops: dict = {}
        scene = self._obs.get(pid, "scene.current_location") if pid is not None else None
        scene_facts = scene.facts if scene is not None and isinstance(scene.facts, Mapping) else {}
        here = motor.tile
        seen = set()

        def near(fact) -> bool:
            tile = fact.get("tile")
            if not isinstance(tile, Mapping) or here is None:
                return here is None
            try:
                return max(abs(int(tile["x"]) - here[0]), abs(int(tile["y"]) - here[1])) <= _VIEW_RADIUS
            except (KeyError, TypeError, ValueError):
                return False

        def count(ref, fact):
            nonlocal trees, stones, chests, dry
            if ref in seen or not isinstance(fact, Mapping) or not near(fact):
                return
            seen.add(ref)
            kind = fact.get("kind")
            if kind == "terrain" and fact.get("terrain_type") == "Tree":
                wild = fact.get("wild_tree") if isinstance(fact.get("wild_tree"), Mapping) else {}
                if (wild.get("stump") is not True and wild.get("tapped") is False
                        and type(wild.get("growth_stage")) is int and wild["growth_stage"] >= 5):
                    trees += 1
            elif kind == "terrain" and fact.get("terrain_type") == "HoeDirt":
                crop = fact.get("crop")
                if isinstance(crop, Mapping) and crop.get("watered") is False:
                    dry += 1
            elif kind == "object" and fact.get("name") == "Stone":
                stones += 1
            elif kind == "object" and str(fact.get("qualified_item_id") or "").endswith(skills.CHEST_SUFFIXES):
                chests += 1

        for ref, fact in scene_facts.items():
            if isinstance(fact, Mapping) and fact.get("kind") == "gather_window":
                for wref, wfact in (fact.get("targets") or {}).items():
                    count(wref, wfact)
                for drop in fact.get("drops") or ():
                    if isinstance(drop, Mapping) and isinstance(drop.get("qualified_item_id"), str):
                        drops[drop["qualified_item_id"]] = drops.get(drop["qualified_item_id"], 0) + 1
            else:
                count(ref, fact)
        if self._v2_pad_path():
            # D-B6-6 (live V2B6bSKLstore-a: `precondition:chest_in_view` with the chest 11 tiles away, though the
            # runtime pins / offers it from the mod's `store_chests`): v2 pad mode counts the published store chests.
            import arti_stardew_economy as _economy
            blocked = set(getattr(self._active, "approach_blocked_refs", None) or ()) if self._active else set()
            for chest in _economy.store_chests(ui_facts):
                if chest["ref"] not in seen and chest["ref"] not in blocked:  # FIXB8 review: a blocked chest is no chest
                    seen.add(chest["ref"])
                    chests += 1
        # D-B6-6 (live V2B6bSKL2a: pungut_drop skipped `drop_in_view` after the tree): the drops the approach catalog
        # offers (pickup reach) count too, not only the gather window's.
        offered: dict = {}
        for t in getattr(snapshot, "approach_targets", ()) or ():
            if (isinstance(t, Mapping) and t.get("reach") == "DIRECT_TARGET"
                    and isinstance(t.get("qualified_item_id"), str)):
                offered[t["qualified_item_id"]] = offered.get(t["qualified_item_id"], 0) + 1
        for qid, n in offered.items():
            drops[qid] = max(drops.get(qid, 0), n)
        flags = set()
        try:
            if self._economy_flag_configured():
                flags.add("economy")
            if self._world_extras_flag_configured():
                flags.add("extras")
        except Exception:  # noqa: BLE001 - an unreadable session = no optional families
            pass
        pad = snapshot.ui_pad_target if isinstance(snapshot.ui_pad_target, Mapping) else {}
        import arti_stardew_economy as economy
        import arti_stardew_interact_b as ib
        cheap_food = {c["qualified_item_id"] for key in ("eat_candidates", "eat_backpack_candidates")
                      for c in (ui_facts.get(key) or ()) if isinstance(c, Mapping)
                      and isinstance(c.get("qualified_item_id"), str)}
        campaign = getattr(self._skill_session, "campaign", None)
        if campaign is not None:
            # Baseline eggs/milk may accompany earned products into the chest.
            # Keep enrolled non-animal food (e.g. parsnips) fenced out of storage.
            cheap_food &= set(campaign.reserve)-economy.STORE_ANIMAL_PRODUCTS
        policy = getattr(self, "_item_policy", None) or economy.ItemPolicy.from_config(self._config)
        # Native crop categories, not the tiny sell/ship allowlist: B27 Bok Choy was absent there.
        crops = {f.get("qualified_item_id") for f in slots if f.get("category") in (-75, -79, -80)}
        from arti_stardew_live7 import goods_facts
        goods = goods_facts(dict(inventory=slots, mines_ui=ui_facts))
        store_items = policy.store_candidates(inventory, protected=cheap_food, native_crops=crops,
                                             sentimental={f.get("qualified_item_id") for f in goods
                                                 if economy.sentimental_facts(f) and economy.hard_dispose_refusal(f) is None},
                                             refused={f.get("qualified_item_id") for f in goods
                                                 if economy.hard_dispose_refusal(f) is not None},
                                             live7=self._live7_active())
        stacks = {}
        for f in slots:
            qid, quality, count = f.get("qualified_item_id"), f.get("quality"), f.get("stack")
            if (f.get("occupied") is True and qid in store_items and type(quality) is int
                    and quality in (0, 1, 2, 4) and type(count) is int and count > 0):
                stacks[qid, quality] = stacks.get((qid, quality), 0) + count
        from arti_stardew_scene import inventory_amount
        inventory_current = bool(rec is not None and pin is not None and inventory_amount(rec,
            session_id=pin.observation_session_id, screen_id=pin.screen_id, qid="(O)0", quality=0) is not None)
        if not inventory_current:
            stacks.clear()  # a truncated/malformed bag is unknown, never an owned-product list
        # The adapter supplies counter readiness; no vendor-name or schedule gate here.
        from arti_stardew_generic_world import ordinary
        native_context = getattr(self, "_water_context", None)
        native_pin = pin or (native_context(pid) if pid is not None and callable(native_context) else None)
        native_shop = any(t.get("kind") == economy.KIND_SHOP_COUNTER and t.get("status") == "available"
                          for t in ordinary(self, native_pin))
        if native_shop and isinstance(ui_facts.get("goal_route"), Mapping):
            native_shop = self._goal_route_facts(native_pin) is not None  # identity/date/pose scope only, never shop hours
        from scripts.live7.capabilities import owned_rods
        rods = owned_rods(dict(inventory_complete=inventory_current,
            inventory=tuple(rec.facts.values()) if inventory_current else (), fishing=ui_facts.get("fishing") or {}))
        return skills.SkillFacts(
            location=motor.location, time_of_day=motor.time_of_day, energy=motor.energy,
            health=getattr(self._state.latest(pid), "health", None),
            player_free=bool(motor.is_player_free) and not skills.menu_up(snapshot), menu_open=skills.menu_up(snapshot),
            menu_closable=(skills._menu_close(snapshot) is not None
                           or getattr(snapshot, "container_context", None) is not None),
            equipped=equipped, inventory=inventory, toolbar=tuple(toolbar),
            edible=bool(ib.choose_eat_candidate(ui_facts) or ib.choose_backpack_eat_candidate(ui_facts)),
            can_water=can, trees=trees, stones=stones, chests=chests,
            dry_soil=dry, drops=drops, free_slots=free, flags=frozenset(flags),
            shop_open=(pad.get("pins") or {}).get("context") == "shop" or native_shop
                      or bool((ui_facts.get("ui_pad") or {}).get("shop")),
            pet_available=self._skill_reachable_pet(ui, ui_facts, motor), store_items=store_items,
            store_stacks=tuple((qid, q, n) for (qid, q), n in stacks.items()), inventory_current=inventory_current, rods=rods)

    def _skill_reachable_pet(self, ui, facts, motor):
        """Use the pet planner's current native entries and observed-open path; never search off-scene."""
        import arti_stardew_world_extras as extras
        from arti_stardew_goal_routes import open_paths, open_path
        if (ui is None or not ui.authoritative or not ui.is_complete
                or not 0 <= time.monotonic() - ui.received_monotonic <= 2 or motor.tile is None):
            return False
        entries = extras.approach_entries(facts, motor.tile, extras.ExtrasObjective("pet"))
        from arti_stardew_animal_routine import actor_cell_open
        parents = open_paths(actor_cell_open(extras.nav_grid(facts, motor.location), motor.tile), motor.tile)
        return any(open_path(parents, stand) is not None for e in entries for stand in e.get("reach_tiles", ()))

    def _skill_current_facts(self) -> skills.SkillFacts | None:
        with self._lock:
            goal = self._active
            if goal is not None:
                return self._skill_facts(self._build_decision_snapshot(goal))
            # No goal yet (first owner turn): the bound motor alone; no menu offers are built without a goal.
            binding, pid = self._binding_view()
            if binding.status != "bound" or pid is None:
                return None
            return self._skill_facts(SimpleNamespace(motor=self._motor_view(pid), binding=binding, exit_target=None,
                                                     ui_key_target=None, ui_pad_target=None))

    # ------------------------------------------------------------------ owner ingress hook
    def _skill_legacy_recognizes(self, text: str) -> bool:
        """A non-skill objective the EXISTING grammars own (talk, menus, naming, round trip, gather N, ...)."""
        import re
        import arti_stardew_economy as economy
        import arti_stardew_fishing as fishing
        import arti_stardew_interact as interact
        import arti_stardew_interact_b as interact_b
        import arti_stardew_text_event as text_event
        import arti_stardew_tool_work as tool_work
        import arti_stardew_ui_pad as ui_pad
        import arti_stardew_world_extras as extras
        from arti_stardew_episode import parse_episode_plan, parse_one_tree_store_goal
        if (parse_one_tree_store_goal(text) or re.fullmatch(_RESOURCE_GOAL, text.strip(), re.IGNORECASE)
                or tool_work.parse_tool_work_goal(text) or fishing.parse_fishing_goal(text)
                or interact.parse_interact_objective(text) is not None
                or interact_b.parse_routine_objective(text) is not None
                or ui_pad.parse_menu_objective(text) is not None
                or text_event.parse_text_event_objective(text) is not None
                or economy.parse_economy_objective(text) is not None
                or extras.parse_extras_objective(text) is not None):
            return True
        try:
            return parse_episode_plan(text) is not None
        except ValueError:
            return True

    def _skill_owner_turn(self, *, text, grant, trigger_type, viewer_name, skill_pin):
        """decide_owner_turn hook. None = the legacy path continues unchanged (flag off, internal start, passthrough)."""
        if not self._skills_enabled or skill_pin is not None:
            return None
        # This hook is called only after authenticated owner admission. Bare,
        # anchored Wave 1 goals carry the same intent as the explicit prefix.
        # Unknown conversation and legacy objectives keep their existing path.
        if grant is None and (skills.wave1_owner_goal(text) or live7_goal(text)):
            import re
            grant = re.fullmatch(r"(main mandiri):\s*([\s\S]+)", "main mandiri: " + text.strip())
        from arti_stardew_runtime import OwnerTurnResult
        with self._lock:
            active = self._skill_session is not None and self._skill_session.status == "active"
            from arti_stardew_runtime import READY
            if active and not grant and self._status != READY and self._skill_session.mode != "part_live":
                # Not READY (PAUSED_RECOVERY): the session ends and the owner's text takes the normal reset path.
                self._skill_end_locked(self._skill_session, "held", "partial", "runtime_not_ready:" + str(self._status),
                                       emit=False)
                active = False
            if active and not grant:
                # Owner chat during a skill session is conversation, never a decision inside the running skill:
                # not admitted here, so the bridge answers on its ordinary conversation path; the skill continues.
                self._journal({"ev": "skill_owner_conversation", "session_id": self._skill_session.session_id})
                return OwnerTurnResult(admitted=False, reason="skill_session_conversation")
            if active:
                self._skill_end_locked(self._skill_session, "stopped", None, "owner_new_goal", emit=False)
        if not grant or grant[1].casefold() not in ("main mandiri", "main sendiri"):
            return None  # "tujuan baru" stays the legacy direct goal
        goal_text = grant[2].strip()
        campaign = any(recognizes(t) for recognizes in (part_a_goal, part_b_goal, part_c_goal, live7_goal) for t in (text, goal_text))
        plan = skills.parse_skill_goal(goal_text)
        if plan is None and not campaign and self._skill_legacy_recognizes(goal_text):
            self._journal({"ev": "skill_mode_passthrough", "reason": "legacy_objective"})
            return None
        unlocked = self._skill_unlocked(goal_text)
        if plan is not None:
            locked = [(c.name, skills.skill_locked(c.name, unlocked)) for c in plan.calls
                      if not c.optional and skills.skill_locked(c.name, unlocked)]
            if locked:
                if self._skill_legacy_recognizes(goal_text):
                    # A locked skill's own legacy objective: the existing objective runs unchanged (as flag off).
                    self._journal({"ev": "skill_mode_passthrough", "reason": "skill_locked",
                                   "skills": [list(x) for x in locked]})
                    return None
                self._journal({"ev": "skill_refused", "reason": "skill_locked", "skills": locked})
                return OwnerTurnResult(admitted=False, reason=f"skill_locked:{locked[0][0]}:{locked[0][1]}")
        page_budget = _page_budget.PageBudget.from_config(self._config)
        session = SkillSession("skills-" + uuid.uuid4().hex[:10], "plan" if plan else "free", goal_text, trigger_type,
                               viewer_name, plan=list(plan.calls) if plan else [],
                               composite=plan.composite if plan else None, started_at=game_now(self),
                               boundary_budget=16128 * page_budget.scale, page_budget=page_budget)
        if campaign:
            session.mode = ("part_live" if live7_goal(text) or live7_goal(goal_text) else
                            "part_c" if part_c_goal(text) or part_c_goal(goal_text) else
                            "part_b" if part_b_goal(text) or part_b_goal(goal_text) else "part_a")
        with self._lock:
            self._skill_session = session
            session.advancing += 1
            self._journal({"ev": "skill_session_started", "session_id": session.session_id, "mode": session.mode,
                           "composite": session.composite,
                           "plan": [[c.name, c.arg, c.optional] for c in session.plan]})
        try:
            if session.mode in ("free", "part_a", "part_b", "part_c", "part_live"):
                # The session goal (no objective); the worker's boundary asks the model for the first skill.
                result = self._skill_session_goal(session)
                if not result.admitted:
                    with self._lock:
                        self._skill_end_locked(session, "failed", None, "session_goal_refused:" + str(result.reason),
                                               emit=False)
                if session.mode in ("part_a", "part_b", "part_c", "part_live") and result.admitted:
                    return self._part_a_begin(session)
                return result
            return self._skill_advance(session, initial=True)
        finally:
            with self._lock:
                session.advancing -= 1
                self._worker_wakeup.notify_all()
            self._skill_flush()

    def _skill_session_goal(self, session: SkillSession):
        """The session's own goal (no objective, the owner's text) through the unchanged owner ingress; its first
        generation is answered with a wait by the driver (never the provider). Used before a model boundary when
        no session goal / skill goal is active (free start, a required skill refused at the owner turn)."""
        with self._lock:
            session.binding_goal = True
            session.goal_id = None
        try:
            return self.decide_owner_turn(text="main mandiri: " + session.goal_text, trigger_type=session.trigger_type,
                                          viewer_name=session.viewer_name, replace_goal=True,
                                          skill_pin=SkillPin(session.session_id))
        finally:
            with self._lock:
                if session.binding_goal and self._active is not None:
                    session.goal_id = self._active.goal_id  # admitted without a generation reaching the driver
                session.binding_goal = False

    # ------------------------------------------------------------------ starting skills
    def _skill_advance(self, session: SkillSession, *, initial: bool = False):
        """Start the next runnable planned skill (optional steps whose preconditions fail are skipped), or end."""
        from arti_stardew_runtime import OwnerTurnResult
        unlocked = self._skill_unlocked()
        while True:
            with self._lock:
                if session is not self._skill_session or session.status != "active":
                    return OwnerTurnResult(admitted=False, reason="skill_session_inactive")
                if session.index >= len(session.plan):
                    result = self._skill_finish_plan_locked(session)
                    return result
                call = session.plan[session.index]
                session.index += 1
                runs = sum(r.status != "skipped" for r in session.runs)
            if session.composite and runs >= skills.COMPOSITE_MAX_RUN:
                self._skill_skip(session, call, "composite_max_run")
                continue
            facts = self._skill_current_facts()
            if call.name == "siram" and call.optional and self._active is not None:
                # B19: the composite has up to four calls, not four mandatory
                # waters. Never approach unrelated grass after the last dry crop.
                if self._skill_no_dry_crops(self._build_decision_snapshot(self._active)):
                    self._skill_skip(session, call, "observed_no_dry_crops")
                    continue
            if (facts is not None and skills.requires_farm_entry(call)
                    and str(facts.location or "").startswith(("Cabin", "FarmHouse", "Coop", "Barn", "Shed"))
                    and (call.name, call.arg) not in session.farm_entry_attempted):
                # Both legs spend the original session/trial envelope.
                with self._lock:
                    if session is not self._skill_session or session.status != "active":
                        return OwnerTurnResult(admitted=False, reason="skill_session_inactive")
                    # B21: travel spends the approved TOTAL envelope. Work keeps
                    # its own step counters, starting only after Farm is observed.
                    caps = skills.skill_costs(skills.SkillCall("pergi_ke", "farm"))
                    if self._action_session_limit is not None:
                        caps = (caps[0], max(0, self._action_session_limit - self._physical_action_publications), caps[2])
                    if self._trial_deadline_unix_ms is not None:
                        caps = (*caps[:2], min(caps[2], max(0, (self._trial_deadline_unix_ms-time.time()*1000)/1000)))
                    entry = (game_now(self), self._physical_action_publications, caps)
                    work = session.plan[session.index - 1:]
                    for child in work if session.composite in ("tanam", "siram") else [call]:
                        session.farm_entry_attempted[(child.name, child.arg)] = entry
                    session.plan[session.index - 1:session.index] = [skills.SkillCall("pergi_ke", "farm"), call]
                    session.index -= 1
                continue
            failures = [] if facts is None else skills.precondition_failures(call, facts)
            if call.name == "simpan_ke_chest" and call.arg == "hasil" and facts is not None:
                if not facts.inventory_current:
                    self._skill_fail_unstarted(session, call, "precondition:fresh_inventory_unavailable")
                    return self._skill_after_failure(session, initial=initial)
                expanded = [skills.SkillCall("simpan_ke_chest", qid, n, quality=q)
                            for qid, q, n in facts.store_stacks if facts.inventory.get(qid, 0) >= n > 0]
                with self._lock:
                    if session is self._skill_session and session.status == "active":
                        entry_budget = session.farm_entry_attempted.get((call.name, call.arg))
                        if entry_budget is not None:
                            for child in expanded:
                                session.farm_entry_attempted[(child.name, child.arg)] = entry_budget
                        session.plan[session.index - 1:session.index] = expanded
                        session.index -= 1
                if not expanded:
                    self._skill_skip(session, call, "no_policy_eligible_products")
                continue
            if failures and any(f.endswith("_in_view") for f in failures):
                facts, failures = self._skill_recheck_view(session, call, facts, failures)
            gate = skills.skill_locked(call.name, unlocked)
            if gate:
                failures.append("locked:" + gate)
            worth = skills.COMPOSITE_WORTH.get(call.name)
            if call.optional and facts is not None and worth is not None and not worth(facts, call):
                failures.append("not_needed")
            if facts is None:
                failures.append("facts_unavailable")
            if failures and call.optional:
                self._skill_skip(session, call, ",".join(failures))
                continue
            if failures:
                self._skill_fail_unstarted(session, call, "precondition:" + ",".join(failures))
                return self._skill_after_failure(session, initial=initial)
            return self._skill_start(session, call, by="owner" if initial and len(session.plan) == 1 else "plan",
                                     facts=facts)

    SKILL_BLOCKED_RUNS_MAX = 6

    def _skill_blocked_refs_refresh(self, session) -> None:
        """Caller holds the lock. FIXB9 review: the session's blocked targets are dropped on a new day, another location,
        or after SKILL_BLOCKED_RUNS_MAX skills (a blocked tree may be reachable again later)."""
        if session.mode == "part_live" and session.campaign is not None:
            session.blocked_refs |= session.campaign.live.refs
            return  # Live7 uncertainty/loop quarantine lasts through this native day.
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        basis = (self._date_tuple(motor), getattr(motor, "location", None))
        if session.blocked_basis is None:
            session.blocked_basis = basis
            return
        why = ("day" if basis[0] != session.blocked_basis[0] else "location" if basis[1] != session.blocked_basis[1]
               else "runs" if session.blocked_runs >= self.SKILL_BLOCKED_RUNS_MAX else None)
        if why is not None:
            if session.blocked_refs:
                self._journal({"ev": "skill_blocked_refs_reset", "session_id": session.session_id, "reason": why,
                               "count": len(session.blocked_refs)})
            session.blocked_refs, session.blocked_basis, session.blocked_runs = set(), basis, 0

    SKILL_VIEW_SETTLE_S = 1.5

    def _skill_recheck_view(self, session, call, facts, failures):
        """D-B7-1 (live V2B7SKLstore-a: `precondition:chest_in_view` false at session start for the 11-tile chest, true on
        the retry): an *_in_view precondition is re-read on fresh snapshots (bounded) before it is declared false."""
        deadline = game_now(self) + self.SKILL_VIEW_SETTLE_S
        while game_now(self) < deadline and not self._worker_stop.is_set():
            W.call('poll', 20, time.sleep, 0.25)
            fresh = self._skill_current_facts()
            if fresh is None:
                continue
            facts, failures = fresh, skills.precondition_failures(call, fresh)
            if not any(f.endswith("_in_view") for f in failures):
                break
        self._journal({"ev": "skill_precondition_recheck", "session_id": session.session_id, "skill": call.name,
                       "failures": list(failures)})
        return facts, failures

    def _skill_skip(self, session, call, reason) -> None:
        with self._lock:
            run = SkillRun(call, "", "plan", game_now(self), self._physical_action_publications,
                           status="skipped", reason=reason, ended_at=game_now(self))
            session.runs.append(run)
            self._journal({"ev": "skill_skipped", "session_id": session.session_id, "skill": call.name,
                           "arg": call.arg, "reason": reason})

    def _skill_fail_unstarted(self, session, call, reason) -> None:
        with self._lock:
            run = SkillRun(call, "", "plan", game_now(self), self._physical_action_publications,
                           status="failed", reason=reason, ended_at=game_now(self))
            session.runs.append(run)
            self._journal({"ev": "skill_failed", "session_id": session.session_id, "skill": call.name,
                           "arg": call.arg, "reason": reason, "started": False})

    def _skill_morning_progress(self, goal):
        """Validated native text counts as progress; readiness keeps its existing wall bound."""
        session = getattr(self, "_skill_session", None)
        run = session.current if session is not None and session.status == "active" else None
        if (not self._skills_enabled or run is None or run.status != "running"
                or run.goal_id != goal.goal_id or run.call.name != "cek_pagi"):
            return None, False
        state = run.state
        result = state.get("plan_input") or {}
        facts = (result.get("weather"), result.get("fortune"), tuple(result.get("mail", ())),
                 state.get("morning_channel"), tuple(state.get("morning_pages", ())),
                 state.get("morning_letters_read", 0))
        since = state.get("wait:morning_ready")
        clock = state.get("clock") or (lambda: game_now(self))
        waiting = (since is not None and 0 <= clock() - since <= skills.MORNING_READY_WAIT_S
                   and state.get("morning_pending") in ("channel", "reading", "pages", "mail", "mail_close"))
        # Part C2 S29: the bounded read-only wait for a fresh offer of the already-open view (skills_ui.plan).
        offer_since = state.get("wait:menu_offer")
        waiting = waiting or (offer_since is not None and 0 <= clock() - offer_since <= skills.WAIT_S)
        return facts, waiting

    def _skill_animal_purchase_waiting(self, goal):
        """Let the authenticated species press finish its existing bounded read-only refresh.

        Gap8n's inspect captured ready HOME facts, then the unchanged-pose stall
        counter stopped continuation before S26 could consume the offered Coop.
        No new allowance, progress claim, species retry or budget reset.
        """
        session = getattr(self, "_skill_session", None)
        run = session.current if session is not None and session.status == "active" else None
        if (not self._skills_enabled or run is None or run.status != "running"
                or run.goal_id != goal.goal_id or run.call.name != "beli_hewan"):
            return False
        state = run.state
        attempt = state.get("attempt") or {}
        since = state.get("wait:animal_home_transition_pending")
        if (attempt.get("animal_phase") != "selected" or not attempt.get("animal_species_request")
                or attempt.get("animal_identity") != goal.play_identity or since is None):
            return False
        from arti_stardew_animal_purchase import READBACK_WAIT_S
        clock = state.get("clock") or (lambda: game_now(self))
        return 0 <= clock() - since <= READBACK_WAIT_S

    def _skill_mines_stone_reach(self, goal, location, ref, fact):
        """S15 alone may explore native scene stones within the existing routing radius.

        Called inside the fresh/session-fenced approach catalog, before its eight
        entries are capped. Removed/refused stones cannot hide another offer.
        """
        session = getattr(self, "_skill_session", None)
        run = session.current if session is not None and session.status == "active" else None
        if (run is None or run.call.name not in skills.MINES_SKILLS or goal is None or run.goal_id != goal.goal_id
                or not str(location).startswith("UndergroundMine")
                or fact.get("kind") != "object" or fact.get("name") != "Stone"):
            return None
        if run.call.name == "tambang_bijih":
            # W1 gap B5: S23 explores only native ore nodes; ordinary stones keep the default reach.
            from arti_stardew_skills_mines import ore_node
            if ore_node(ref, fact) is None:
                return None
        if (ref in run.state.get("wave1_removed", ()) or
                run.state.get("wave1_visit_location") == location and ref in run.state.get("wave1_unreachable", ())):
            return 0
        from arti_stardew_local_approach import ROUTING_RADIUS
        return ROUTING_RADIUS

    def _skill_mines_drop_rank(self, goal, pin, entry):
        """#319: S23's pickup phase ranks the removed node's own ore drop first, before the eight-entry cut.

        Same run/goal/identity/floor and the recorded ore qid only; the cap, reach and every other entry are
        unchanged. Called inside the fresh/session-fenced approach catalog."""
        session = getattr(self, "_skill_session", None)
        run = session.current if session is not None and session.status == "active" else None
        if (run is None or run.call.name != "tambang_bijih" or goal is None or run.goal_id != goal.goal_id
                or pin is None or not str(entry.get("ref", "")).startswith("drop:")):
            return None
        o = run.state.get("ore") or {}
        identity = tuple(getattr(pin, k, None) for k in ("runtime_instance_id", "command_session_id",
                                                        "observation_session_id", "player_id", "screen_id", "location"))
        if (o.get("phase") != "pickup" or o.get("floor") != pin.location or o.get("identity") != identity
                or not o.get("ore") or entry.get("qualified_item_id") != o["ore"]):
            return None
        return 0

    def _skill_mines_exit_reserve(self, run):
        """Spend no new allowance: keep 48 decisions, 40 inputs, 180s for native exit."""
        decisions, inputs, seconds = self._part_a_caps(run)
        left = dict(decisions=decisions-run.decisions,
                    inputs=inputs-(self._physical_action_publications-run.inputs_start),
                    seconds=seconds-(game_now(self)-run.started_at))
        if self._action_session_limit is not None:
            left["inputs"] = min(left["inputs"], self._action_session_limit-self._physical_action_publications)
        if self._trial_deadline_unix_ms is not None:
            left["seconds"] = min(left["seconds"], self._trial_deadline_unix_ms/1000-time.time())
        budget = getattr(self, "_trial_budget_remaining", None)
        if budget is not None:
            for key, value in budget().items():
                if key in left:
                    left[key] = min(left[key], value)
        return any(left[k] <= v for k, v in dict(decisions=48, inputs=40, seconds=180).items())

    @staticmethod
    def _skill_mines_receipt_pending(run, goal):
        """#318: this run's goal holds an achieved terminal receipt the boundary has not consumed yet."""
        last = getattr(goal, "last_finish", None)
        return (goal is not None and getattr(goal, "progress_marker", None) == "terminal"
                and last is not None and getattr(last, "status", None) == "achieved"
                and getattr(goal, "goal_id", None) == getattr(run, "goal_id", None))

    def _skill_mines_idle_damage(self, run, goal, motor):
        """ADR 171: two native inspections losing HP without a physical action hand back immediately."""
        if (run.call.name not in skills.MINES_SKILLS or goal is None or goal.goal_id != run.goal_id
                or not str(getattr(motor, "location", "")).startswith("UndergroundMine")):
            return None
        hp = getattr(motor, "health", None)
        if type(hp) is int and hp <= 0 or run.state.get("mines_death"):
            return None  # the native death/KO outcome still wins
        if run.state.get("mines_idle_handoff"):
            return "fail", run.state["mines_idle_handoff"]
        pid = self._binding_view()[1]
        action = getattr(goal, "last_action", None)
        if (type(hp) is not int or hp <= 0 or self._state.is_stale(pid)
                or getattr(self, "_action_busy", False) or getattr(self, "_results_pending", 0)
                or self._trial_result_pending()
                or self._gather_mutation_unresolved or goal.gather_uncertain
                or getattr(goal, "tool_work_uncertain", False)
                or action is not None and (skills._g(action, "cleanup_confirmed") is not True
                    or skills._g(action, "lifecycle") not in ("completed", "rejected", "not_taken")
                    or skills._g(action, "world_effect_status") in ("uncertain", "possible_mutation", "unverified"))):
            run.state.pop("mines_idle_hp", None)
            return None
        # A newly settled batch may have cost HP. Compare only observations
        # with the same released action/publication baseline and native epoch.
        basis = (goal.goal_id, motor.location, self._date_tuple(motor),
                 self._physical_action_publications, skills._g(action, "request_id"))
        previous = run.state.get("mines_idle_hp")
        run.state["mines_idle_hp"] = (basis, hp)
        if previous is None or previous[0] != basis or hp >= previous[1]:
            return None
        run.state["mines_idle_handoff"] = "under_attack"
        run.state["mines_idle_damage_fact"] = dict(hp_before=previous[1], hp=hp, location=motor.location,
                                                   action_in_flight=False)
        self._journal(dict(ev="skill_mines_idle_damage", skill=run.call.name, goal_id=goal.goal_id,
                           **run.state["mines_idle_damage_fact"], outcome="under_attack"))
        return "fail", "under_attack"

    def _skill_mines_safety(self, run, goal, context=None):
        """Read native HP even with no continuation. Re-pin existing S5/exit; never credit recovery."""
        if run.call.name not in skills.MINES_SKILLS or goal is None or goal.goal_id != run.goal_id:
            return None
        if run.state.get("mines_death"):
            return "fail", run.state["mines_death"]
        pid = self._binding_view()[1]
        motor = self._state.latest(pid)
        pin = self._water_context(pid) or self._ui_key_context(pid)
        if (motor is None or self._state.is_stale(pid) or pin is None
                or goal.play_identity != tuple(getattr(pin, k, None) for k in (
                    "runtime_instance_id", "command_session_id", "observation_session_id", "player_id", "screen_id"))
                or motor.location != pin.location):
            return None
        native = self._combat_ui_facts(pin) or {}
        vitals = (context or {}).get("combat") or native.get("combat") or {}
        hp = getattr(motor, "health", None)
        observed_hp = [v for v in (hp, vitals.get("hp")) if type(v) is int]
        hp = min(observed_hp) if observed_hp else None
        inside = str(motor.location).startswith("UndergroundMine")
        if inside:
            run.state["mines_seen_inside"] = True
        # The live PlayerKilled wake-up is positive HP (10) in the lobby, with
        # DialogueBox. It cannot be mistaken for a voluntary, verified exit.
        knockout = (motor.location == "Mine" and hp == 10
                    and getattr(motor, "active_menu", None) == "DialogueBox"
                    and (run.state.get("mines_seen_inside") or run.state.get("wave1_floor", 0) > 0))
        if (type(hp) is int and hp <= 0) or knockout:
            reason = "death:mine_knockout" if knockout else "death:native_hp_zero"
            run.state.update(mines_death=reason, alive_exit="death")
            self._journal({"ev": "skill_mines_death", "reason": reason, "hp": hp, "location": motor.location})
            return "fail", reason
        max_hp = vitals.get("max_hp")
        from arti_stardew_skills_mines import SURVIVAL_HP_PCT
        reserve = SURVIVAL_HP_PCT if run.call.name == 'lawan_mine' else 60
        low = (type(hp) is int and hp > 0 and (hp < reserve
               or type(max_hp) is int and max_hp > 0 and hp * 100 < reserve * max_hp))
        if run.call.name != "lawan_mine" and run.state.get("live7"):
            # ADR 149: in free play mining rocks at 60 HP is hers; 60% applies under attack, the 40% reflex floor always.
            from arti_runtime.mines_rules import ore_hp_low
            low = ore_hp_low(run.state, vitals, hp, max_hp, getattr(motor, "time_of_day", None))
        if not inside or not low:
            return None
        # ADR 169: do not interrupt a damaging fight with the very exit whose
        # corridor that monster blocks. Safe food still has first refusal.
        from arti_runtime.mines_survival import meal
        last_batch = run.state.get("mines_last_batch") or {}
        ongoing = getattr(goal, "combat", None)
        if (ongoing is not None and ongoing.status == "OPEN"
                and run.state.get("wave1_driver") in ("combat", "combat_floor")
                and last_batch.get("goal_id") == goal.goal_id and last_batch.get("location") == motor.location
                and type(last_batch.get("damage_dealt")) is int and last_batch["damage_dealt"] > 0
                and not meal(dict(native, combat=vitals))
                and not self._worker_stop.is_set() and not self._action_cancel.is_set()
                and not self._owner_voice_busy and not self._gather_mutation_unresolved and not goal.gather_uncertain):
            from dataclasses import replace
            goal.combat = replace(ongoing, escape=True)
            return None
        # ADR 146: the exit's route/pose/Leave/readback owns this run until it is safe.
        # Do not spend the remaining HP re-pinning defenses or a meal from the ladder stand.
        if run.state.get("wave1_survival_exit") and run.state.get("wave1_driver") == "exit":
            return None
        if (run.call.name == "lawan_mine" and (run.state.get("wave1_combat_timed_out")
                or run.state.get("wave1_driver") in ("combat_floor", "combat")
                and (getattr(goal, "combat_wait", None) or {}).get("ended") == "timeout")
                and not self._worker_stop.is_set() and not self._action_cancel.is_set() and not self._owner_voice_busy
                and not self._gather_mutation_unresolved and not goal.gather_uncertain
                and (getattr(goal, "last_action", None) is None
                     or skills._g(goal.last_action, "cleanup_confirmed") is True
                     and skills._g(goal.last_action, "lifecycle") in ("completed", "rejected", "not_taken"))):
            # ADR 146 amendment: a stopped fight below reserve retains fix82A's immediate survival handoff.
            from arti_runtime.mines_disengage import exit_step
            run.state["wave1_combat_timed_out"] = True
            exit_step(run.state, "hp_reserve")
            run.pending = None
            self._pending_continuation = None
            return "next_leg", "native_low_hp_exit"
        if run.call.name == "lawan_mine" and run.state.get("wave1_driver") == "ladder":
            import re
            from arti_stardew_skills_mines import descend_first
            floor = re.fullmatch(r"UndergroundMine(\d+)", str(motor.location))
            vitals_hp = dict(vitals, hp=hp, max_hp=max_hp if type(max_hp) is int and max_hp > 0 else 100)
            if floor and descend_first(vitals_hp, int(floor[1]), True):
                # GAP8B J2 L3077-L3078: the pinned down-ladder leg is the nearer fresh exit; keep it.
                if run.state.get("mines_descend_first_hp") != hp:
                    run.state["mines_descend_first_hp"] = hp
                    self._journal({"ev": "skill_mines_descend_first", "hp": hp, "location": motor.location})
                return None
        action = getattr(goal, "last_action", None)
        if (self._worker_stop.is_set() or self._action_cancel.is_set() or self._owner_voice_busy
                or self._gather_mutation_unresolved or goal.gather_uncertain
                or action is not None and (skills._g(action, "cleanup_confirmed") is not True
                    or skills._g(action, "lifecycle") not in ("completed", "rejected", "not_taken"))):
            return None
        state, now = run.state, game_now(self)
        from arti_stardew_skills_mines import eat_unsafe
        unsafe = eat_unsafe(vitals)
        if run.call.name == "lawan_mine" and not (state.get("wave1_driver") == "eat"
                                                  and getattr(motor, "active_menu", None)):
            from arti_stardew_skills_mines import contact_defend
            fighting = state.get("wave1_driver") in ("combat_floor", "combat")
            d = state.get("mines_defend")
            if isinstance(d, dict) and not fighting:
                d["active"] = False
            vitals_hp = dict(vitals, hp=hp, max_hp=max_hp if type(max_hp) is int and max_hp > 0 else 100)
            verdict = contact_defend(state, vitals_hp, motor.location)
            if verdict is not None and fighting:
                # GAP8E J2 L3354-L3360: the fight in contact continues below the reserve (reflex floor 40, no
                # greedy retreat) while each settled batch still hurts a monster; never the walk out in contact.
                if verdict == "start":
                    self._journal({"ev": "skill_mines_contact_defend", "status": "held", "hp": hp,
                                   "location": motor.location, "defends": state["mines_defend"]["count"]})
                return None
            if verdict == "start":
                self._journal({"ev": "skill_mines_contact_defend", "status": "started", "hp": hp,
                               "location": motor.location, "from": state.get("wave1_driver"),
                               "defends": state["mines_defend"]["count"]})
                skills.wave1_repin(state, "bunuh satu monster", "combat_floor", "observed_monster_killed")
                state.pop("mines_refresh_due", None)
                run.pending = None
                self._pending_continuation = None
                return "next_leg", "native_contact_defend"
        if state.get("wave1_driver") == "eat":
            # W1 gap S15b (journal-attempts15 L841-L978, report steps 82->96): 12 eat-site stages while two
            # slimes stood on the offered sites; every new stage input reset the idle timer and HP fell 59 -> 11
            # before the exit started. A monster in radius or any HP loss since the meal began ends the meal.
            eat_hp = state.setdefault("mines_eat_hp", hp)
            abort = None if getattr(motor, "active_menu", None) else (
                unsafe or ("unsafe:hp_falling_during_meal" if hp < eat_hp else None))
            if abort is not None:
                state["mines_eat_refused"] = abort
                self._journal({"ev": "skill_mines_eat_aborted", "reason": abort, "hp": hp, "hp_at_meal": eat_hp})
            else:
                # Includes a dropped food-stage continuation; don't sit exposed for
                # the ordinary 30-second idle grace if consumption never verifies.
                request_id = skills._g(action, "request_id")
                if request_id and request_id != state.get("mines_heal_progress_request"):
                    state.update(mines_heal_progress_request=request_id, mines_heal_started=now)
                if now - state.setdefault("mines_heal_started", now) < 8:
                    return None
        else:
            state.pop("mines_eat_hp", None)
            food_known_absent = ("eat_candidates" in native and "eat_backpack_candidates" in native
                                 and not native["eat_candidates"] and not native["eat_backpack_candidates"])
            # W1 gap B5 (S15E attempt 1, journal 573-631): the meal ended no_food_candidate (backpack staging
            # stopped unsafe:monster_in_radius), the terminal exit was pinned, and this guard re-pinned the SAME
            # meal (native_low_hp_eat) instead of walking out; ARTI died after the run. A meal that ended without
            # a verified consumption is not retried on this run: low HP now keeps/starts the survival exit.
            if (state.get("wave1_heals", 0) < 2 and not food_known_absent and not state.get("mines_eat_refused")
                    and unsafe is not None):
                state["mines_eat_refused"] = unsafe
                self._journal({"ev": "skill_mines_eat_refused", "reason": unsafe, "hp": hp})
            if state.get("wave1_heals", 0) < 2 and not food_known_absent and not state.get("mines_eat_refused"):
                state["wave1_heals"] = state.get("wave1_heals", 0) + 1
                state["mines_heal_started"] = now
                state["mines_eat_hp"] = hp
                skills.wave1_repin(state, "makan", "eat", "observed_eat_effect")
                run.pending = None
                self._pending_continuation = None
                return "next_leg", "native_low_hp_eat"
        state.update(wave1_exit_due=True, wave1_survival_exit=True,
                     wave1_hold=state.get("wave1_hold", "partial:mines_low_hp_exit"))
        if state.get("wave1_driver") == "exit":
            return None
        skills.wave1_repin(state, "skill " + run.call.name, "exit", "exited_mines")
        state.pop("mines_refresh_due", None)
        run.pending = None
        self._pending_continuation = None
        return "next_leg", "native_low_hp_exit"

    def _skill_crab_composing(self, goal):
        """S24 needs native offers/hand facts before it can create its next verified extras leg."""
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        return (goal is not None and run is not None and session.status == "active"
                and run.status == "running" and run.goal_id == goal.goal_id
                and run.call.name == "crab_pot" and run.call.arg in (None, "pasang", "ambil")
                and not run.state.get("wave1_driver"))

    def _skill_quest_approach_entries(self, pin, motor_tile):
        """S28's bound native NPC uses the existing moving-target approach, without a gift grant.

        FINAL B7 (J3302/J3303): S16 picked Linus from the mod's npc_gift offers (published within 24 tiles), but the
        talk routine only offers an NPC within ib.NPC_REACH (12), so Linus was `too_far` and nothing was offered. The
        talk leg now walks the same positional npc_gift stands toward its bound villager (no gift grant / press);
        within the talk reach the routine's own NPC entry takes over."""
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        goal = self._active
        if (pin is None or goal is None or run is None or session.status != "active" or run.status != "running"
                or run.goal_id != goal.goal_id):
            return []
        social = (run.call.name == "ngobrol_hadiah" and run.state.get("wave1_driver") == "talk"
                  and not run.state.get("wave1_talk_done") and isinstance(run.state.get("wave1_social_npc"), str))
        if not social and (run.call.name != "surat_quest" or not run.call.arg or run.state.get("wave1_driver")):
            return []
        rec = self._obs.get(pin.player_id, "ui.context")
        if (rec is None or not rec.authoritative or not rec.is_complete or not rec.available or rec.stale
                or rec.requires_resync or not 0 <= time.monotonic() - rec.received_monotonic <= 2
                or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or rec.facts.get("location_id") != pin.location):
            return []
        if social:
            import arti_stardew_interact_b as ib
            import arti_stardew_world_extras as extras
            routine = getattr(goal, "routine_objective", None)
            if routine is None or routine.kind != ib.OBJ_TALK or routine.opened or not ib.objective_open(routine):
                return []
            name = run.state["wave1_social_npc"]
            here = tuple(motor_tile)
            # Only beyond the talk reach: inside it the routine's own NPC entry (and its press) applies unchanged.
            return [e for e in extras.approach_entries(rec.facts, here, extras.ExtrasObjective("gift", name=name))
                    if max(abs(e["tile"][0] - here[0]), abs(e["tile"][1] - here[1])) > ib.NPC_REACH]
        from arti_stardew_skills_quest import npc_approach_entries
        return npc_approach_entries(rec.facts, tuple(motor_tile), run.state.get("attempt", {}).get("quest", {}))

    def _skill_wave1_context(self, snapshot):
        """A transient view of the SAME admitted observation domains; never a second world store."""
        import arti_stardew_world_extras as extras
        import arti_stardew_interact_b as ib
        import arti_stardew_economy as economy
        from arti_stardew_animal_routine import actor_cell_open
        # Optional route repair cannot manufacture an observation binding. Older
        # planner-only snapshots still belong to their existing driver.
        if skills._g(skills._g(snapshot, "binding"), "player_id") is None:
            return {}
        pin = snapshot.water_context or snapshot.movement_context
        if pin is None and hasattr(self, "_ui_key_context"):
            pin = self._ui_key_context(snapshot.binding.player_id)
        if pin is None:
            return {}
        def facts(domain, complete=True):
            rec = self._obs.get(snapshot.binding.player_id, domain)
            if (rec is None or not rec.authoritative or (complete and not rec.is_complete)
                    or not rec.available or rec.stale or rec.requires_resync
                    or not 0 <= time.monotonic() - rec.received_monotonic <= 2
                    or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id):
                return None
            return rec.facts
        ui, scene, bag = facts("ui.context"), facts("scene.current_location", complete=False), facts("progress.inventory")
        from arti_stardew_world import current_catalog
        world_knowledge = current_catalog(self, pin)
        route = (ui or {}).get("goal_route")
        from arti_stardew_scene import gather_window
        window = gather_window(self._obs.get(snapshot.binding.player_id, "scene.current_location"),
                               session_id=pin.observation_session_id, screen_id=pin.screen_id, location_id=pin.location)
        # Global truncation cannot erase the independently complete local tool window.
        targets = {ref: fact for ref, fact in (scene or {}).items()
                   if isinstance(fact, Mapping) and fact.get("kind") not in ("gather_window", "map_affordances")}
        wet_soil = getattr(self, "_wet_soil_facts", None)
        if wet_soil is not None:
            # live7-game-m2: in-place watering emits no scene delta; keep today's window-observed wet soil wet.
            targets = wet_soil(pin, targets, window["targets"] if window is not None else None)
        elif window is not None:
            targets.update(window["targets"])
        tillable_tiles = [(t["x"], t["y"]) for t in (window or {}).get("tiles", ())
                          if t.get("tillable") is True and t.get("villager") is False]
        return dict(extras=extras.extras_targets(ui), combat=(ui or {}).get("combat"),
                    collection_fit=(ui or {}).get("collection_fit") or (),
                    collection_fit_status=(ui or {}).get("collection_fit_status"),
                    chests=economy.store_chests(ui), fishing=(ui or {}).get("fishing") or {},
                    mines_ui=ui, world_knowledge=world_knowledge,
                    scene_map_affordances=(scene or {}).get("affordance:map"),
                    scene_affordance_targets=scene,
                    ui_complete=ui is not None, inventory_complete=bag is not None,
                    mailbox_count=(ui or {}).get("mailbox_count"),
                    money=(ui or {}).get("money"), tool_upgrading=(ui or {}).get("tool_upgrading"),
                    date=route.get("date") if isinstance(route, Mapping) else None,
                    identity=tuple(getattr(pin, k, None) for k in ("runtime_instance_id", "command_session_id",
                        "observation_session_id", "player_id", "screen_id", "location")),
                    facing=getattr(self._state.latest(snapshot.binding.player_id), "facing", None),
                    inventory=tuple(f for f in (bag or {}).values() if isinstance(f, Mapping) and f.get("occupied") is True),
                    # W1 gap B5: S23 refuses a full bag before breaking an ore node (None = unknown capacity).
                    bag_free_slots=(sum(isinstance(f, Mapping) and f.get("occupied") is False for f in bag.values())
                                    if bag is not None and any(isinstance(f, Mapping) and "occupied" in f
                                                               for f in bag.values()) else None),
                    machine_food=ib.eat_candidates(ui) + ib.eat_candidates({"eat_candidates": (ui or {}).get("eat_backpack_candidates")}),
                    targets=targets, scene_complete=facts("scene.current_location") is not None,
                    greeted_today=getattr(self, "_live7_greeted_names", frozenset)(),  # ADR 161: her verified talks today
                    actor_tile=skills._wave1_xy(skills._g(skills._g(snapshot, "motor"), "tile")),
                    actor_pixel=tuple(skills._g(skills._g(snapshot, "motor"), "pixel") or ()),
                    actor_facing=skills._g(skills._g(snapshot, "motor"), "facing"),
                    clock=skills._g(skills._g(snapshot, "motor"), "time_of_day"),  # ADR 239: failures bind to it
                    local_window=window, location_id=pin.location,
                    tillable_tiles=tillable_tiles,
                    # A23: a character on ARTI's own cell marks it '#'; only that one cell is opened.
                    nav_grid=actor_cell_open(extras.nav_grid(ui or {}, pin.location),
                                             skills._g(skills._g(snapshot, "motor"), "tile")))

    def _skill_item_ready(self, run, goal, snapshot, context, qids, reason):
        """Reuse native inventory staging and selection; only current held readback unlocks use."""
        if getattr(self, "_stage_lock_last", None) == "item_to_toolbar" and self._stage_lock_holds():
            return skills.Step("decision", dict(kind="wait", reason="stage_lock", ms=0))
        held = (skills._g(snapshot, "equipped_tool") if reason == "tool" else
                skills._g(snapshot, "held_item")) or {}
        if (reason == "tool" and not held and context.get("ui_complete") and context.get("inventory_complete")
                and not skills.menu_up(snapshot)):
            # ADR 40: selection is independent of use_tool admission (e.g. a
            # background rod uses reflex_fish). Join the current native toolbar
            # to the active bag slot; a selection proposal/receipt alone cannot
            # unlock preparation. The context reader enforces freshness/identity.
            toolbar = (context.get("mines_ui") or {}).get("toolbar") or {}
            active = toolbar.get("active_slot")
            held = next((item for item in context.get("inventory", ())
                if type(active) is int and type(item.get("slot")) is int and item["slot"] == active
                and item.get("occupied") is True and item.get("is_active") is True
                and item.get("type_token") == "tool" and item.get("qualified_item_id") in qids
                and any(type(t.get("slot")) is int and t["slot"] == active
                        and t.get("qualified_item_id") == item["qualified_item_id"]
                        for t in toolbar.get("targets", ()) if isinstance(t, Mapping))), {})
        if held.get("qualified_item_id") in qids:
            # A verified selection ends this consecutive retry sequence; a
            # later tool switch inside the same run gets its own allowance.
            starts = run.state.get("item_select_publication_start", {})
            for qid in qids:
                starts.pop(qid, None)
            return None
        select = skills._select(snapshot, qids)
        if select is not None:
            qid = select["args"]["qualified_item_id"]
            starts = run.state.setdefault("item_select_publication_start", {})
            publications = self._physical_action_publications
            # A proposal may be discarded by the inspect/publication fence.
            # Only published inputs spend this retry allowance; held readback
            # above still decides success, and all run/input/time caps remain.
            start = starts.setdefault(qid, publications)
            if publications - start >= 2:
                return skills.Step("fail", reason=reason + "_selection_unverified")
            return skills.Step("decision", select)
        from arti_stardew_combat import TOOLBAR_SLOTS
        owned = next((f for f in context.get("inventory", ()) if context.get("inventory_complete")
                      and f.get("qualified_item_id") in qids and type(f.get("slot")) is int
                      and f["slot"] >= TOOLBAR_SLOTS), None)
        staged = run.state.setdefault("item_staged_qids", [])
        if owned is not None and owned["qualified_item_id"] not in staged:
            qid = owned["qualified_item_id"]
            staged.append(qid)
            goal.item_stage_want = qid
            self._stage_end_resume("item_to_toolbar", goal, force=True)
            return skills.Step("decision", dict(kind="wait", reason="item_to_toolbar", ms=0))
        return skills.Step("fail", reason=reason + "_not_selectable")

    def _skill_plant_step(self, run, goal, snapshot):
        if self._skill_finish_ready(goal, SimpleNamespace(snapshot=snapshot)):
            return skills.Step("decision", skills.FINISH)
        context = self._skill_wave1_context(snapshot)
        pin = skills._g(snapshot, "water_context") or skills._g(snapshot, "movement_context")
        slots = self._extras_seed_slots(pin)
        seeds = [f for f in context.get("inventory", ()) if context.get("inventory_complete")
                 and f.get("slot") in slots and f.get("category") == -74]
        qids = tuple(dict.fromkeys(f.get("qualified_item_id") for slot in slots for f in seeds
                                  if f.get("slot") == slot and isinstance(f.get("qualified_item_id"), str)))
        if not qids:
            return skills.Step("fail", reason="seed_not_selectable")
        run.state["plant_seed_qids"] = qids
        ready = self._skill_item_ready(run, goal, snapshot, context, qids, "seed")
        if ready is not None:
            return ready
        step = skills.plan_step(run.call, snapshot, state=run.state)
        if (step.choice and step.choice.get("operation") == "world_interact"
                or run.state.get("live7") and run.state.get("plant_target", {}).get("tile") is not None):
            # R9: extras_rule may replace an inspect/turn with the native X.
            # Capture BEFORE that rule too, rather than only on a skill X step.
            qid = snapshot.held_item["qualified_item_id"]
            run.state.setdefault("plant_before", dict(qid=qid, tile=run.state["plant_target"]["tile"],
                count=sum(f.get("stack", 0) for f in context["inventory"] if f.get("qualified_item_id") == qid)))
        return step

    def _skill_plant_verified(self, run, goal):
        """Native X already pairs crop creation on its pinned tile with seed loss.

        Also require fresh scene/inventory readback before crediting the skill;
        a command receipt or a selected seed alone is insufficient.
        """
        before = run.state.get("plant_before") or {}
        if not before:
            return False
        context = self._skill_wave1_context(self._build_decision_snapshot(goal))
        native = skills._g(goal.last_finish, "observed", {}) or {}
        paired = any(e.get("kind") == "place_item" and e.get("item") == before["qid"] and e.get("delta") == -1
                     for e in native.get("effects", ()))
        count = sum(f.get("stack", 0) for f in context.get("inventory", ())
                    if f.get("qualified_item_id") == before["qid"])
        crop = any(skills._wave1_xy(f.get("tile")) == skills._wave1_xy(before["tile"]) and f.get("terrain_type") == "HoeDirt"
                   and isinstance(f.get("crop"), Mapping) for f in context.get("targets", {}).values())
        # A fresh positive crop fact needs no globally complete Farm dump (the
        # bounded local tool window is authoritative even on a clipped map).
        return paired and context.get("inventory_complete") and crop and count == before["count"] - 1

    ROUTE_READS_POLL_S = 0.05
    _ROUTE_CATALOG_SETTLING = frozenset({"no_frame", "frame_unusable", "frame_old", "other_location", "graph_unreadable"})

    def _skill_route_reads_settling(self, pin) -> bool:
        """fix90A / ADR 159: True while the native reads a trip routes on do not describe ``pin``'s map yet.

        After a warp the world catalog still names the old map, is older than its 2 s freshness, or is the incomplete
        frame the mod writes when its capture throws during the fade (one capture per 60 ticks); the nav grid can still
        be the old map's for up to 250 ms. Read-only local cache; a wait condition, never a permission."""
        if pin is None or getattr(pin, "player_id", None) is None or not getattr(pin, "location", None):
            return False
        from arti_stardew_world import places, read_catalog
        try:
            catalog, why = read_catalog(self, pin)
            if catalog is None and why in self._ROUTE_CATALOG_SETTLING:
                return True
            if catalog is not None and not places({"world_knowledge": catalog}):
                return True  # the capture-failure frame: an empty graph for the new map
            rec = self._obs.get(pin.player_id, "ui.context")
            raw = (getattr(rec, "facts", None) or {}).get("nav_grid")
        except Exception:  # noqa: BLE001 - an unreadable cache is no reason to wait
            return False
        return isinstance(raw, Mapping) and raw.get("location") not in (None, pin.location)

    @W.span('route_read', 8, runtime=True)
    def _skill_await_route_reads(self, pin, pace) -> dict:
        """fix90A / ADR 159: wait until the new map's reads settle, the actor leaves ``pin``'s map, or an interrupt,
        bounded by the exit wait's own window (``skills.WAIT_S``). Replaces the fixed pace of that wait only; the
        inspect that follows reads the settled facts. Not settled at the end: the old pace is kept as the floor."""
        started = time.monotonic()
        settled = False
        while time.monotonic() - started < skills.WAIT_S:
            if (self._worker_stop.is_set() or self._action_cancel.is_set() or getattr(self, "_owner_voice_busy", False)):
                break
            motor = self._state.latest(pin.player_id)
            if getattr(motor, "location", None) != pin.location:
                break
            if not self._skill_route_reads_settling(pin):
                settled = True
                break
            W.call('route_read', 21, self._action_cancel.wait, self.ROUTE_READS_POLL_S)
        waited = time.monotonic() - started
        if not settled and waited < pace:
            W.call('route_read', 22, time.sleep, pace - waited)
        row = {"ev": "route_reads_wait", "location": pin.location, "settled": settled,
               "waited_ms": int(waited * 1000), "at": time.time()}
        self._journal(row)
        return row

    def _skill_route_leg_choice(self, goal, snapshot):
        """fix90A / ADR 159: the running trip's own next leg after an observed waypoint arrival, or None.

        Called by the approach batch under the runtime lock at a route-leg boundary (a ``home_waypoint`` ARRIVED, on a
        freshly rebuilt snapshot). It runs the checks this boundary runs (clock reserve and curfew, caps, the verifier,
        exhaustion, under_attack, the route pause, the effect watchdog) and the skill's own navigation choice
        (``nav_auto_choice``, what ``_nav`` returns). Only a further ``approach_target`` on the same map is returned;
        anything else (an exit to cross, a finish, an inspect, a repair, a cap, an event) ends the batch and the
        ordinary boundary decides as before. An interrupt found here becomes the run's pending outcome, as the boundary
        would set it."""
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        if (not self._skills_enabled or session is None or session.status != "active" or run is None
                or run.call.name != "pergi_ke" or run.status != "running" or run.pending is not None
                or goal is None or run.goal_id != goal.goal_id or run.state.get("route_clear")
                or any(k in run.state for k in ("farm_entry_caps", "campaign_task", "route_caps",
                                                "campaign_mine_recovery", "campaign_travel"))
                or getattr(goal, "food_reselect_due", False) or self._stage_lock_holds()
                or self._skill_rule_due() or self._pending_event_reason is not None
                or self._pending_continuation is not None):
            return None
        hop = getattr(goal, "home_route_state", None) or {}
        if hop.get("repair_reaches") or hop.get("repair_plan"):
            return None  # a route repair (clearing) step may be due: the boundary's debris route step owns it
        motor = skills._g(snapshot, "motor")
        mem = skills._g(snapshot, "working_memory")
        pin = skills._g(snapshot, "water_context") or skills._g(snapshot, "movement_context")
        location = skills._g(pin, "location")
        clock = skills._g(motor, "time_of_day")
        if (motor is None or not location or skills._g(motor, "location") != location or type(clock) is not int
                or skills._g(motor, "active_menu") or skills._g(mem, "last_inspect_status") != "current"):
            return None
        import arti_stardew_interact_b as ib
        if ib.curfew_phase(clock) == "hard":
            return None
        if session.mode == "part_live" and session.campaign is not None:
            from arti_runtime.travel_time import reserve
            if session.campaign.task != "sleep" and reserve(location, clock, session.campaign.live)["active"]:
                return None  # the boundary latches the clock reserve
        decision_cap, input_cap, seconds_cap = self._part_a_caps(run)
        if run.call == skills.SkillCall("pergi_ke", "farm") and "input_budget" in hop:
            input_cap = min(input_cap, hop["inputs_start"] - run.inputs_start + hop["input_budget"])
        inputs = self._physical_action_publications - run.inputs_start - run.stage_inputs
        if run.decisions >= decision_cap or inputs >= input_cap or game_now(self) - run.started_at > seconds_cap:
            return None
        if self._skill_finish_ready(goal, SimpleNamespace(snapshot=snapshot)):
            return None
        pending = (self._live7_exhaustion(run, goal, motor) or self._live7_attack(run, goal, motor)
                   or self._live7_route_pause(run, goal, motor) or self._skill_effect_watchdog(run, goal, motor))
        if pending is not None:
            run.pending = pending
            self._journal({"ev": "skill_step_boundary", "session_id": session.session_id, "skill": run.call.name,
                           "outcome": pending[0], "reason": pending[1], "site": "route_leg_chain"})
            self._worker_wakeup.notify_all()
            return None
        from arti_stardew_auto_act import nav_auto_choice
        picked = nav_auto_choice(goal.interact_objective, snapshot, current_location=location,
                                 approach_progress=skills._g(mem, "approach_progress"))
        if picked is None or picked[0] != "approach_target" or not picked[1] or picked[2]:
            return None
        return picked[1]

    def _skill_route_leg_commit(self, goal, target_ref) -> None:
        """fix90A / ADR 159: a chained leg is one skill step (same caps, same journal row, flagged ``chained``)."""
        session = self._skill_session
        run = session.current
        run.decisions += 1
        for key in [k for k in run.state if k.startswith("wait:")]:
            del run.state[key]  # progress: a later wait starts a fresh window (as plan_step does)
        self._journal({"ev": "skill_step", "session_id": session.session_id, "skill": run.call.name,
                       "n": run.decisions, "kind": "act", "operation": "approach_target", "target": target_ref,
                       "chained": True, "at": time.time()})

    def _skill_mail_readback(self, snapshot):
        """Read the queue from the same closed native context as the current motor."""
        pid = snapshot.binding.player_id
        rec, _ = self._ui_key_record(pid)
        pin = snapshot.water_context or snapshot.movement_context
        motor = self._capture_motor_basis(snapshot)
        if (pin is None or motor is None or snapshot.motor.status != "current"
                or snapshot.event_movement is not None
                or motor.active_menu is not None or motor.can_player_move is not True
                or rec is None or rec.session_id != pin.observation_session_id
                or rec.facts.get("mode") != "gameplay" or rec.player_id != pin.player_id
                or rec.screen_id != pin.screen_id
                or any(rec.facts.get(k) != v for k, v in dict(runtime_instance_id=pin.runtime_instance_id,
                    session_id=pin.command_session_id, location_id=pin.location,
                    context_generation=pin.context_generation, ui_revision=motor.ui_revision).items())):
            return {}
        return dict(mailbox_count=rec.facts.get("mailbox_count"), ui_domain_revision=rec.domain_revision,
                    context_generation=pin.context_generation, identity=(pin.runtime_instance_id, pin.command_session_id,
                        pin.observation_session_id, pin.player_id, pin.screen_id, pin.location))

    def _skill_effect_watchdog(self, run, goal, motor):
        """R8: rules can turn forever despite Live7's unlimited cost envelope.

        Selection, facing, sub-tile drift and revisits are preparation, not effects.
        Novel cells on trips and verified progress on the active work step renew the
        window. This guard never grants completion or clears an uncertainty fence.
        """
        session = getattr(self, "_skill_session", None)
        halted_session_id = self.__dict__.get("_live7_ladder_halted")
        if halted_session_id is not None and halted_session_id == getattr(session, "session_id", None):
            return None
        inputs = self._physical_action_publications
        clock = skills._g(motor, "time_of_day")
        minute = clock // 100 * 60 + clock % 100 if type(clock) is int else None
        mark = run.state.setdefault("effect_watchdog", dict(inputs=inputs, minute=minute, request=None, tiles=[]))
        travels = reaches_place(skills.REGISTRY.get(run.call.name))
        date = tuple(skills._g(motor, k) for k in ("year", "season", "day"))
        if all(v is not None for v in date):
            if mark.get("date") is not None and date != mark["date"]:
                mark["minute"] = minute  # new day's clock starts a new no-effect window, never a negative delta
            mark["date"] = date
        routine = skills._g(goal, "routine_objective")
        bed_waiting = (run.call.name == "tidur" and skills._g(routine, "kind") == "sleep"
                       and skills._g(routine, "answered") is True
                       and skills._g(routine, "waiting_since") is not None
                       and skills._g(motor, "active_menu") == "ReadyCheckDialog"
                       and skills._g(routine, "start_date") is not None
                       and date == tuple(routine.start_date))
        if bed_waiting and hasattr(self, "_ui_pad_view") and hasattr(self, "_binding_view"):
            view = self._ui_pad_view(self._binding_view()[1])
            readiness = getattr(view, "ready_check", None)
            if readiness is not None:
                bed_waiting = readiness.get("check") == "sleep" and readiness.get("local_ready") is True
        human_ready_waiting = (session is not None and hasattr(self, "_live7_human_ready_waiting")
                               and self._live7_human_ready_waiting(session))
        if bed_waiting or human_ready_waiting:
            # Current native menu + verified readiness own this decision, not the possibly
            # blank menu cached at follow-done. Suspend only the game-minute guard.
            # Inputs remain bounded; rollover/cancellation/stale facts immediately rearm time.
            mark["minute"] = minute
        if (getattr(self, "_stage_lock_last", None) and self._stage_lock_holds()
                and self._stage_lock_last in ("item_to_toolbar", "combat_weapon_stage")):
            # Locked inventory stages have their own finite step/deadline + abort
            # bounds. Let them put the cursor item back and close before ending.
            mark.update(inputs=inputs, minute=minute)
            return None
        if mark["minute"] is None:
            mark["minute"] = minute
        action = skills._g(goal, "last_action")
        request = skills._g(action, "request_id")
        effect = skills._g(action, "world_effect_status")
        fresh = (bool(request) and request != mark["request"] and skills._g(action, "lifecycle") == "completed"
                 and skills._g(action, "cleanup_confirmed") is True)
        if action is not None and (skills._g(action, "cleanup_confirmed") is False
                or skills._g(action, "lifecycle") in ("uncertain", "interrupted")
                or request and effect == "uncertain"):
            return None  # existing reconciliation/safety owns this action; never relabel it CLEAN_FAIL
        verified = fresh and effect in {
            "observed_extras_effect", "observed_tool_effect", "observed_item_pickup",
            "observed_location_change", "observed_paired_transfer", "observed_monster_killed",
            "observed_eat_effect", "observed_purchase", "observed_craft", "observed_event_effect", "observed_fish_caught"}
        handoff = run.state.get("mail_handoff")
        if (handoff is not None and handoff["request_id"] != mark.get("mail_request")
                and skills._g(motor, "active_menu") is None):
            from arti_stardew_skills_ui import mail_close_current
            snapshot = self._build_decision_snapshot(goal)
            context = self._skill_mail_readback(snapshot)
            if (mail_close_current(run.state, context, snapshot.motor)
                    and type(context.get("mailbox_count")) is int
                    and context["mailbox_count"] == run.state["mail_unread_before"] - 1):
                mark["mail_request"] = handoff["request_id"]
                verified = True
        if verified and effect == "observed_location_change":
            verified = travels or run.state.get("wave1_driver") == "exit"
        if run.call.name in skills.MINES_SKILLS:
            from arti_runtime.mines_fight import facts
            mined = facts(run)
            work = tuple(mined[k] for k in ("floors_gained", "rocks", "kills", "items"))
            verified |= any(after > before for before, after in zip(mark.get("mine_work", (0, 0, 0, 0)), work))
            mark["mine_work"] = work
        progress = skills._g(goal, "approach_progress")
        approaching = (fresh and skills._g(goal, "approach_effect_request_id") == request
                       and isinstance(progress, Mapping) and progress.get("status") == "PROGRESS"
                       and progress.get("target_ref") == skills._g(skills._g(goal, "approach_target"), "target_ref")
                       and _moved_progress(action, mark["request"]))
        arrived = (isinstance(progress, Mapping) and progress.get("status") == "ARRIVED" and progress.get("target_ref")
                   and progress["target_ref"] not in mark.setdefault("arrived", []))
        if arrived:
            mark["arrived"].append(progress["target_ref"])  # C13: preparation that reached its step's target
        # FIX76A (a55 d3 J1329-J1347, d17 J280-J361): 8 verified TransferObserved in her own open chest, then her close, and this guard
        # read 150 game minutes with no world receipt: the clock runs in a menu, and a chest move writes no goal.last_action.
        # A menu her own aksi opened answered that aksi (generic_world's own verdict); the time she spent in it is not "no effect".
        moved = len(run.state.get("chosen_transfers", ()))
        if moved != mark.get("moved", 0):
            mark["moved"] = moved
            verified = True
        if run.state.get("generic_menu_opened"):
            mark.update(inputs=inputs, minute=minute)
            return None
        tile = skills._wave1_xy(skills._g(motor, "tile"))
        cell = (skills._g(motor, "location"), tile) if tile is not None else None
        # Seed the start cell without granting a reset. Later *new* cells bound
        # long legitimate approaches while two-tile ping-pong still expires.
        novel = cell is not None and cell not in mark["tiles"]
        had_tiles = bool(mark["tiles"])
        if novel:
            mark["tiles"].append(cell)
        if fresh:
            mark["request"] = request
        ui_progress = False
        if fresh and effect in {"observed_ui_press", "observed_ui_nav", "observed_pad_button"}:
            view = self._ui_pad_view(self._binding_view()[1])
            if view is not None:
                # Long stock/recipe scans are real preparation. Count only a
                # newly observed menu state, excluding churning receiver/tokens;
                # bouncing between the same snaps must still expire.
                signature = json.dumps({k: skills._g(view, k) for k in (
                    "menu_type", "context", "page_type", "page_index", "snapped_id", "text", "held_item")},
                    sort_keys=True)
                rows = tuple((f.get("index"), f.get("item")) for f in (skills._g(view, "economy") or {}).get("rows", ()))
                ui_cell = (signature, rows)
                seen = mark.setdefault("menus", [])
                if ui_cell not in seen:
                    seen.append(ui_cell)
                    ui_progress = True
        if not bed_waiting and (verified or fresh and novel and had_tiles and (travels or approaching)
                                or arrived or ui_progress):
            mark.update(inputs=inputs, minute=minute)
        if (inputs - mark["inputs"] >= skills.NO_EFFECT_INPUTS
                or minute is not None and mark["minute"] is not None
                and minute - mark["minute"] >= skills.NO_EFFECT_MINUTES):
            from arti_runtime.mines_exit import intent, remember, policy, exit_ran
            # ADR 239: a work leg's no-effect window is not a failed exit (attempt 90 J3198 -> 40 refusals).
            if intent(run, skills._g(motor, "location")) and exit_ran(run) and policy(self) is not None:
                context = self._skill_wave1_context(self._build_decision_snapshot(goal))
                remember(self, context, skills._g(motor, "location"), "no_effect_progress")
            spec = skills.REGISTRY.get(run.call.name)
            if isinstance(run.state, dict) and spec is not None:
                run.state["fail_detail"] = "waited_for:" + str(spec.objective)  # C13: the window's end is named
            return "fail", "no_effect_progress"
        return None

    def _skill_harvest_step(self, run, goal, snapshot):
        ready = self._skill_finish_ready(goal, SimpleNamespace(snapshot=snapshot))
        if (ready or skills._g(snapshot.working_memory, "progress_marker") == "terminal"
                or skills._g(snapshot.working_memory, "last_inspect_status") != "current"
                or skills._g(snapshot.working_memory, "last_action") is not None and (
                    skills._g(snapshot.working_memory.last_action, "cleanup_confirmed") is False
                    or skills._g(snapshot.working_memory.last_action, "lifecycle") in ("uncertain", "interrupted"))):
            return skills.plan_step(run.call, snapshot,
                finish_ready=ready, state=run.state)
        context = self._skill_wave1_context(snapshot)
        offered = context.get("extras", ())
        if (context.get("ui_complete") and (snapshot.world_interaction_target or {}).get("kind") != "harvest_crop"
                and not any(e.get("kind") == "harvest_crop" for e in offered)
                and any((f.get("crop") or {}).get("ready_for_harvest") is True
                        for f in context.get("targets", {}).values() if isinstance(f, Mapping))):
            return skills.Step("fail", reason="harvest_not_offered:ready_crop_requires_native_grab_offer")
        return skills._extras(run.call, snapshot, run.state, offered)

    def _skill_till_step(self, run, goal, snapshot):
        """S9 has no remote empty-tile offer: use only the fresh adjacent window."""
        import arti_stardew_tool_work as tw
        progress = goal.tool_work
        if progress is None or progress.status not in tw.ACTIVE:
            return skills.plan_step(run.call, snapshot, state=run.state)
        window = self._tool_work_window(goal)
        if window is None or "tiles" not in window:
            return skills.Step("decision", skills.INSPECT)
        here = skills._wave1_xy(getattr(snapshot.motor, "tile", None))
        if here != skills._wave1_xy(window.get("center")):
            return skills.Step("decision", skills.INSPECT)
        candidates = []
        for facing, (dx, dy) in enumerate(((0, -1), (1, 0), (0, 1), (-1, 0))):
            tile = (here[0] + dx, here[1] + dy)
            entry = tw._tile_entry(window, *tile)
            if (entry is not None and entry.get("tillable") is True and entry.get("villager") is False
                    and not tw._facts_on(window, *tile)):
                candidates.append(tile)
        turns = run.state.setdefault("till_turns", {})
        # Smoke 2026-10-23 D2 J2582-J2811: 33 one-tick turns alternated 45,9 <-> 46,9. ARTI stood 3 px from the
        # tile edge, so each 1-tick turn toward the tillable neighbour walked her onto it, and from there the
        # tillable tile was behind her. A (stand, tile) turn that already failed twice is not a candidate.
        offered = skills._g(snapshot, "tool_work_target")
        tried = [tile for tile in candidates if turns.get("%d,%d>%d,%d" % (*here, *tile), 0) >= TILL_TURNS_MAX
                 and offered != tw.tile_ref(window["location_id"], *tile)]  # a faced, offered tile is never capped
        if tried and len(tried) == len(candidates):
            return skills.Step("fail", reason="till_face_unstable")
        candidates = [tile for tile in candidates if tile not in tried]
        if not candidates:
            context = self._skill_wave1_context(snapshot)
            if any(e.get("kind") == "place_item" for e in context.get("extras", ())):
                return skills.Step("fail", reason="till_not_needed")
            return skills.Step("fail", reason="no_tillable_tile")
        view = SimpleNamespace(**{k: skills._g(snapshot, k) for k in (
            "working_memory", "motor", "equipped_tool", "selectable_items", "select_item_available",
            "use_tool_available", "tool_work_target")}, approach_targets=())
        if view.tool_work_target not in {tw.tile_ref(window["location_id"], *tile) for tile in candidates}:
            view.tool_work_target = None
        if skills._equipped(snapshot) == "(T)Hoe" and view.tool_work_target is None:
            tile = candidates[0]
            key = "%d,%d>%d,%d" % (*here, *tile)
            turns[key] = turns.get(key, 0) + 1
            nudge = _edge_nudge(getattr(snapshot.motor, "pixel", None), here, tile)
            if nudge is not None:
                return skills.Step("decision", nudge)
            return skills.Step("decision", skills._face(snapshot, tile))
        return skills.plan_step(run.call, view, state=run.state)

    def _skill_no_dry_crops(self, snapshot):
        """Negative evidence needs a fresh complete scene in the bound location.

        A missing/stale catalogue is not an empty area. Unknown crop water state
        is not watered. The scope is the same nearby area as skill admission.
        """
        context = self._skill_wave1_context(snapshot)
        last = skills._g(skills._g(snapshot, "working_memory"), "last_action")
        if (self._gather_mutation_unresolved or self._action_cancel.is_set()
                or (last is not None and (skills._g(last, "cleanup_confirmed") is False
                    or skills._g(last, "lifecycle") in ("uncertain", "interrupted")))):
            return False
        pin = snapshot.water_context or snapshot.movement_context
        here = getattr(snapshot.motor, "tile", None)
        targets = context.get("targets", {})
        if pin is None or here is None:
            return False
        for fact in targets.values():
            if not isinstance(fact, Mapping) or fact.get("terrain_type") != "HoeDirt":
                continue
            tile = skills._wave1_xy(fact.get("tile"))
            crop = fact.get("crop")
            if (tile is None or isinstance(crop, Mapping) and crop.get("watered") is not True
                    and max(abs(tile[0]-here[0]), abs(tile[1]-here[1])) <= _VIEW_RADIUS):
                return False
        if not context.get("scene_complete"):
            # A clipped map was B23's false blocker. Read this bounded area with
            # the same native inspect_tiles and identity fences as S17.
            if context.get("local_window") is None:
                return False
            try:
                folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
                session = self._commands.baca_sesi(folder)
                if not lease_state(session.mentah).matches(pin.command_session_id, pin.runtime_instance_id, pin.player_id, pin.screen_id):
                    return False
                targets = {}
                # inspect_tiles is capped at radius 4. Nine bounded reads cover
                # the original +/-12 admission area without enlarging that cap.
                for dx in (-8, 0, 8):
                    for dy in (-8, 0, 8):
                        cx, cy = here[0]+dx, here[1]+dy
                        result = self._commands.minta_inspect_tiles(cx, cy, 4, expected_location_id=pin.location,
                                                                   folder=folder, sesi=session)
                        raw = result.mentah or {}
                        current = self._water_context(pin.player_id)
                        motor = self._state.latest(pin.player_id)
                        if (not result.sukses or result.outcome != "Inspected" or current is None
                                or motor is None or skills._wave1_xy(motor.tile) != skills._wave1_xy(here)
                                or self._action_cancel.is_set() or self._gather_mutation_unresolved
                                or any(getattr(current, k) != getattr(pin, k) for k in
                                    ("runtime_instance_id", "command_session_id", "observation_session_id", "player_id", "screen_id", "location"))
                                or raw.get("session_id") != pin.command_session_id or raw.get("player_id") != pin.player_id
                                or raw.get("location_id") != pin.location or raw.get("center_x") != cx
                                or raw.get("center_y") != cy or raw.get("radius") != 4
                                or not isinstance(raw.get("facts"), Mapping)):
                            return False
                        targets.update(raw["facts"])
            except Exception:
                return False
        elif not any(f.get("kind") == "location" and f.get("location_id") == pin.location
                     for f in targets.values() if isinstance(f, Mapping)):
            return False
        for fact in targets.values():
            if not isinstance(fact, Mapping) or fact.get("terrain_type") != "HoeDirt":
                continue
            tile = skills._wave1_xy(fact.get("tile"))
            if tile is None:
                return False
            if max(abs(tile[0] - here[0]), abs(tile[1] - here[1])) > _VIEW_RADIUS:
                continue
            crop = fact.get("crop")
            if isinstance(crop, Mapping) and crop.get("watered") is not True:
                return False
        return True

    def _skill_clear_context(self, run, request, context):
        """Read the bounded rectangle, not the possibly truncated whole map.

        Positive target facts are insufficient to certify rectangle coverage.
        Reuse inspect_tiles; its square may cover extra tiles, but only the
        owner's rectangle is ever passed to the target chooser.
        """
        import re
        try:
            bounds = tuple(int(n) for n in re.split("[:,]", run.call.arg or ""))
        except ValueError:
            return context
        if (len(bounds) != 4 or not 0 <= bounds[2]-bounds[0] <= 3
                or not 0 <= bounds[3]-bounds[1] <= 3 or min(bounds) < 0):
            return context
        if context.get("scene_complete"):
            return context
        pin = request.snapshot.water_context or request.snapshot.movement_context
        if pin is None or not self._request_current(request):
            return context
        from arti_stardew_scene import gather_window
        rec = self._obs.get(pin.player_id, "scene.current_location")
        window = gather_window(rec, session_id=pin.observation_session_id,
                               screen_id=pin.screen_id, location_id=pin.location)
        if window is not None:
            x, y = window["center"]["x"], window["center"]["y"]
            if x-1 <= bounds[0] <= bounds[2] <= x+1 and y-1 <= bounds[1] <= bounds[3] <= y+1:
                return dict(context, area_bounds=bounds, targets=window["targets"])
        # The nav grid is a rolling 40x40 movement window, not map coverage.
        # A bounded read may inspect the owner's rectangle outside that window;
        # it grants no route, stand or tool offer. Those still need native admission.
        if not self._nav_grid(pin):
            return context  # retain the fresh native navigation-context fence
        cx, cy = (bounds[0]+bounds[2])//2, (bounds[1]+bounds[3])//2
        radius = max(bounds[2]-cx, bounds[3]-cy)
        facts = SkillRuntimeMixin._skill_inspect_tiles(self, request, pin, cx, cy, radius)  # unit doubles: no mixin
        return context if facts is None else dict(context, area_bounds=bounds, targets=facts)

    @W.span('settle_readback', 9, runtime=True)
    def _skill_inspect_tiles(self, request, pin, cx, cy, radius):
        """One bounded native inspect_tiles read (identity-fenced), shared by S17's rectangle and the GAP8E route
        repair. The read's facts, or None. It grants no route, stand or tool offer."""
        folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
        session = self._commands.baca_sesi(folder)
        if not lease_state(session.mentah).matches(pin.command_session_id, pin.runtime_instance_id, pin.player_id, pin.screen_id):
            return None
        result = self._commands.minta_inspect_tiles(cx, cy, radius, expected_location_id=pin.location,
                                                   folder=folder, sesi=session)
        raw = result.mentah or {}
        current = self._water_context(pin.player_id)
        if (not result.sukses or result.outcome != "Inspected" or not self._request_current(request)
                or current is None or any(getattr(current, k) != getattr(pin, k) for k in
                    ("runtime_instance_id", "command_session_id", "observation_session_id", "player_id", "screen_id", "location"))
                or raw.get("session_id") != pin.command_session_id or raw.get("player_id") != pin.player_id
                or raw.get("location_id") != pin.location or raw.get("center_x") != cx
                or raw.get("center_y") != cy or raw.get("radius") != radius
                or not isinstance(raw.get("facts"), Mapping)):
            return None
        return raw["facts"]

    def _skill_mine_recovery_step(self, run, goal, request):
        """B6: a campaign's S7 floor->Mine hop uses the native up-ladder/Leave controller.

        This is travel within the same row/run/caps, never an S15/S23 attempt or an invented floor warp.
        """
        named_escape = False
        if run.call.name == "aksi" and run.call.arg and not run.state.get("generic_pressed"):
            from arti_runtime.mines_exit import ups
            context = self._skill_wave1_context(request.snapshot)
            if any(e["ref"] == run.call.arg for e in ups(context, request.snapshot.motor.location)):
                from arti_runtime.mines_survival import escape_offered
                named_escape = escape_offered(context, request.snapshot.motor.location)
                from arti_runtime.mines_body import _offered
                native_press = (skills._g(request.snapshot, "world_interaction_target") or {}).get("kind") == "ladder_up"
                if not named_escape and not native_press and _offered(self._combat_ui_facts(
                        self._water_context(request.snapshot.binding.player_id)) or {}):
                    # ADR 179: walking to the stand is the exit controller's mod leg, never the generic tap-walk.
                    named_escape = run.state["mines_up_via_exit"] = True
                if not named_escape:
                    from arti_stardew_generic_world import step
                    return step(run.call, request.snapshot, run.state, context=context)
        home_exit = run.call.name == "pergi_ke" and run.call.arg != "mine" and run.state.get("live7")
        if (not named_escape and not home_exit and (run.call != skills.SkillCall("pergi_ke", "mine") or not run.state.get("campaign_travel"))
                or not str(request.snapshot.motor.location).startswith("UndergroundMine")):
            return None
        if getattr(goal, "mines_exit", None) is None:
            run.state["campaign_mine_recovery"] = request.snapshot.motor.location
            goal.interact_objective = None
            self.mines_exit_pin(goal, request.snapshot.motor.location, dict(reason="campaign_mine_recovery"))
        raw = self._mines_exit_invoke(request)
        if raw is not None:
            return raw
        blocker = getattr(goal.extras_objective, "blocker", None)
        return skills.Step("fail", reason="mine_recovery_failed:" + blocker) if blocker else skills.Step(
            "decision", dict(skills.INSPECT, question="mine recovery: observe native exit"))

    def _skill_attempt_ui_view(self, player_id):
        """Project the UI pad onto the attempt planner's modal view."""
        view = self._ui_pad_view(player_id)
        return None if view is not None and getattr(view, "hud", False) else view

    def _skill_wave1_step(self, run, goal, request):
        """Compose existing planners/rules; admission/cleanup remain authoritative."""
        snap, state = request.snapshot, run.state
        if run.call.name in skills.MINES_SKILLS and SkillRuntimeMixin._skill_mines_receipt_pending(run, goal):
            # #318: never re-pin (safety or planner) over an unconsumed achieved receipt; the boundary consumes it
            # once, then runs the same safety for the next input.
            return skills.Step("decision", dict(kind="wait", reason="skill_receipt_pending", ms=0))
        safety = self._skill_mines_safety(run, goal) if run.call.name in skills.MINES_SKILLS else None
        if safety is not None:
            return skills.Step(safety[0], reason=safety[1])
        last = skills._g(skills._g(snap, "working_memory"), "last_action")
        if last is not None and (skills._g(last, "cleanup_confirmed") is False
                                 or skills._g(last, "lifecycle") in ("uncertain", "interrupted")):
            return skills.Step("fail", reason="action_unreconciled")
        if skills._g(skills._g(snap, "working_memory"), "last_inspect_status") != "current":
            return skills.Step("decision", skills.INSPECT)
        context = self._skill_wave1_context(snap)
        if run.state.get("live7"):
            context = dict(context, live7_travel_policy=getattr(getattr(self._skill_session, "campaign", None), "live", None))
        if run.state.get("live7") and run.call.name in ("forage", "panen"):
            from arti_runtime.collection_facts import pickup_refusals, harvest_refusals
            refused = pickup_refusals if run.call.name == "forage" else harvest_refusals
            excluded = self._skill_session.campaign.live.refs | refused(context, context.get("bag_free_slots") == 0)
            if run.call.name == "forage":
                from arti_runtime.offer_withhold import route_refs
                motor = snap.motor
                excluded |= route_refs(self, self._skill_session, motor.location,
                                       skills._wave1_xy(motor.tile), context.get("nav_grid"))
            context = dict(context, targets={ref: f for ref, f in context.get("targets", {}).items()
                                            if ref not in excluded},
                           extras=[e for e in context.get("extras", ()) if e.get("ref") not in excluded])
            changes = dict(approach_targets=tuple(e for e in skills._g(snap, "approach_targets", ()) or ()
                                                  if e.get("ref") not in excluded))
            target = skills._g(snap, "world_interaction_target") or {}
            if target.get("source_fact_id", target.get("ref")) in excluded:
                changes["world_interaction_target"] = None
            # Live smoke 2026-10-24: DecisionContextSnapshot is frozen; a copy() plus setattr raised
            # FrozenInstanceError and stalled every Live7 forage (stalled:no_continuation).
            if is_dataclass(snap):
                snap = replace(snap, **changes)
            else:
                snap = copy(snap)
                vars(snap).update(changes)
        if (run.call.name == "lawan" and state.get("wave1_driver") == "combat_floor" and not skills.menu_up(snap)
                and not getattr(self, "_stage_lock_holds", lambda: False)()):
            # Live7 self-defence: low HP / swing cap / a combat leg that ended without a kill end the run between
            # inputs (never over an open menu or a locked inventory stage); else the existing combat rule continues.
            stop = skills.lawan_guard(state, context.get("combat") or {},
                                      objective_status=getattr(getattr(goal, "combat", None), "status", None),
                                      location=skills._g(skills._g(snap, "motor"), "location"))
            if stop is not None:
                return stop
        if run.state.get("live7") and run.call.name == "forage" and not state.get("wave1_driver"):
            look = self._live7_forage_step(run, snap, context)
            if look is not None:
                return look
        safety = self._skill_mines_safety(run, goal, context) if run.call.name in skills.MINES_SKILLS else None
        if safety is not None:
            return skills.Step(safety[0], reason=safety[1])
        if run.call.name in skills.ATTEMPT_NAMES:
            # Owner 2026-10-18: a real attempt (travel, existing verified buttons, native readback).
            import arti_stardew_skills_attempts as attempts
            context["publications"] = self._physical_action_publications
            if run.call.name == "masak" and not state.get("wave1_driver") and context.get("ui_complete"):
                context["ui_view"] = self._skill_attempt_ui_view(snap.binding.player_id)  # the cooking page the attempt reads
            if state.get("wave1_driver") == "attempt_travel":
                return attempts.leg_step(state, skills.plan_step(skills.SkillCall("pergi_ke", "farm"), snap, state=state))
            if not state.get("wave1_driver"):
                if run.call.name in ("donasi", "siapkan_pancing", "upgrade_alat", "beli_hewan", "surat_quest") and context.get("ui_complete") and context.get("inventory_complete"):
                    import arti_stardew_economy as economy
                    policy = economy.ItemPolicy.from_config(self._config)
                    context["upgrade_policy"] = policy.allow_tool_upgrade
                    context["disposal_refusals"] = {f.get("qualified_item_id"): policy.refuse_gift(
                        "sell", f.get("qualified_item_id"), None) for f in context.get("inventory", ())}
                    context["ui_view"] = self._skill_attempt_ui_view(snap.binding.player_id)
                return attempts.plan(run.call, snap, state, context)
        if run.call.name == "cek_pagi" and state.get("wave1_driver") == "morning_farm":
            return skills.plan_step(skills.SkillCall("pergi_ke", "farm"), snap, state=state)
        if run.call.name == "cek_pagi":
            from arti_stardew_skills_ui import plan
            import arti_stardew_economy as economy
            policy = economy.ItemPolicy.from_config(self._config)
            context["disposal_refusals"] = {f.get("qualified_item_id"): policy.refuse_gift("sell", f.get("qualified_item_id"), None)
                for f in context.get("inventory", ())}
            context["ui_view"] = self._ui_pad_view(snap.binding.player_id)
            if state.get("mail_handoff_required"):
                context.update(self._skill_mail_readback(snap))
            # The same receiver's native letter facts (has_quest / has_items): S29 closes only an inert letter.
            key_view = getattr(self, "_ui_key_view", None)
            context["ui_key_view"] = key_view(snap.binding.player_id) if key_view is not None else None
            return plan(run.call, snap, state, context)
        if state.pop("placement_refresh_due", False):
            return skills.Step("decision", skills.INSPECT)
        if run.call.name == "bersihkan" and not state.get("wave1_driver"):
            context = self._skill_clear_context(run, request, context)
        if run.call.name == "pasang" and not state.get("wave1_driver"):
            here = skills._wave1_xy(snap.motor.tile)
            tile = run.call.tile
            if here is not None and tile is not None and abs(here[0]-tile[0])+abs(here[1]-tile[1]) == 1:
                window = context.get("local_window")
                if window is None or skills._wave1_xy(window.get("center")) != here:
                    return skills.Step("decision", skills.INSPECT)
        if run.call.name in skills.MINES_SKILLS:
            # Exit has priority over refresh/reselection, including an active
            # rock/combat leg. PLAN: entry ends at 18:00, exit deadline is 01:00.
            location, t = snap.motor.location, snap.motor.time_of_day
            inside = str(location).startswith("UndergroundMine")
            if type(t) is not int and state.get("wave1_driver") != "exit":
                return skills.Step("fail", reason="mines_time_unknown")
            from arti_runtime import mines_rules
            free = bool(state.get("live7")) and "campaign_mines_exit_due" not in state
            # ADR 167 timing belongs to free play; task planners need no Live7 session.
            timing = getattr(getattr(self._skill_session, "campaign", None), "live", None) if free else None
            if location == "Mine" and state.get("wave1_driver") != "exit" and (
                    not mines_rules.entry_open(t, timing) if free else not 900 <= t <= 1800):
                # ADR 149: free play has no entry window, only the game's 02:00 pass-out; Wave-1 keeps its task window.
                return skills.Step("fail", reason=mines_rules.CURFEW_REASON if free else "mines_time_window:enter_0900_1800")
            curfew = (inside and state.get("wave1_driver") not in ("exit", "eat")
                      and (mines_rules.curfew_due(location, t, timing) if free
                           else t >= min(state.get("campaign_mines_exit_due", 2200), 2200)))
            energy_exit = free and mines_rules.energy_due(getattr(snap.motor, "energy", None))
            if (inside and state.get("wave1_driver") not in ("exit", "eat")
                    and (curfew or energy_exit or self._skill_mines_exit_reserve(run))):
                state["wave1_exit_due"] = True
                if free and run.call.name == "lawan_mine":
                    # ADR 149: no floor-five / kill label in free play; the hold says only why she was walked out.
                    state["wave1_hold"] = (mines_rules.CURFEW_HOLD if curfew else mines_rules.ENERGY_HOLD
                                           if energy_exit else mines_rules.BUDGET_HOLD)
                elif (((state.get("ore") or {}).get("evidence") is None) if run.call.name == "tambang_bijih"
                        else (int(location.removeprefix("UndergroundMine")) < 5
                              or state.get("wave1_kills", 0) + getattr(goal.combat, "kills", 0) < 1)):
                    state["wave1_hold"] = "partial:mines_time_or_budget_exit"
                state["wave1_exit_reason"] = "time_or_budget_reserve"
                from arti_stardew_combat import idle_hp_floor
                from arti_stardew_skills_mines import eat_unsafe
                if (idle_hp_floor(context.get("combat")) is not None and state.get("wave1_heals", 0) < 2
                        and not state.get("mines_eat_refused") and eat_unsafe(context.get("combat")) is None):
                    # The exit controller cannot defend/heal. Keep the existing
                    # bounded S5 consumption verifier before walking out.
                    state["wave1_heals"] = state.get("wave1_heals", 0) + 1
                    return skills.wave1_repin(state, "makan", "eat", "observed_eat_effect")
                skills.wave1_repin(state, "skill " + run.call.name, "exit", "exited_mines")
                state.pop("mines_refresh_due", None)
                if goal.mines_exit is None:
                    self.mines_exit_pin(goal, location, {"reason": "time_or_budget_reserve",
                                                        "kills": state.get("wave1_kills", 0)})
            if skills.wave1_stone_reach_failed(state, last, getattr(goal, "approach_progress", None), snap.motor.location,
                                               name=run.call.name):
                return skills.Step("next_leg", reason="stone_unreachable_for_floor_visit")
            if skills.wave1_stone_loop(state, last, getattr(goal, "approach_progress", None), snap.motor.location,
                                       name=run.call.name):
                self._skill_mines_loop_journal(run, last, snap.motor.location)
                return skills.Step("next_leg", reason="stone_unreachable_for_floor_visit")
            if inside and state.get("wave1_driver") not in ("exit", "eat") and run.call.name == "lawan_mine":
                if not skills.menu_up(snap):
                    state["mines_work_identity"] = context.get("identity")
                progress = getattr(goal, "tool_work", None)
                if (state.get("wave1_driver") == "rock" and skills._g(progress, "kind") == "break_rock"
                        and skills._g(progress, "target_ref") == state.get("wave1_pin", {}).get("ref")
                        and skills._g(progress, "settled_steps", 0) > 0
                        and skills._g(last, "lifecycle") == "completed"
                        and skills._g(last, "cleanup_confirmed") is True
                        and skills._g(last, "outcome") == "InputCompleted"
                        and (skills._g(last, "before") or {}).get("location") == location):
                    skills.mines_discovery_arm(state, progress.target_ref, skills._g(last, "request_id"))
                discovery = skills.mines_discovery_step(snap, state, context)
                if discovery is not None:
                    return discovery
            if state.pop("mines_refresh_due", False):
                return skills.Step("decision", skills.INSPECT)
            import re
            level = re.fullmatch(r"UndergroundMine(\d+)", str(snap.motor.location or ""))
            if level:
                state["wave1_floor"] = max(state.get("wave1_floor", 0), int(level[1]))
            t = snap.motor.time_of_day
            if type(t) is not int and state.get("wave1_driver") != "exit":
                return skills.Step("fail", reason="mines_time_unknown")
            if level and type(t) is int and t >= state.get("campaign_mines_exit_due", 2400) and goal.mines_exit is None:
                state["wave1_exit_due"] = True
            driver = state.get("wave1_driver")
            fight_timeout = (state.get("wave1_combat_timed_out") or driver in ("combat_floor", "combat")
                             and (getattr(goal, "combat_wait", None) or {}).get("ended") == "timeout")
            if driver not in ("exit", "eat") and state.get("wave1_survival_exit"):
                # ADR 146: survival ownership remains sticky through native Leave/readback.
                state.update(wave1_exit_due=True, wave1_survival_exit=True)
                state.setdefault("wave1_hold", "partial:mines_combat_timeout" if fight_timeout else "partial:mines_low_hp_exit")
                return skills.wave1_repin(state, "skill lawan_mine", "exit", "exited_mines")
            if driver not in ("exit", "eat") and fight_timeout:
                # A healthy stopped fight ends on this floor. Unsafe retreat still owes the verified exit.
                state["wave1_combat_timed_out"] = True
                from arti_runtime.mines_disengage import step
                return step(snap, state, context)
            defending = (state.get("wave1_driver") == "combat_floor"
                         and (state.get("mines_defend") or {}).get("active") is True)
            if state.get("wave1_exit_due") and state.get("wave1_driver") != "eat" and not defending:
                if not level:
                    return skills.Step("fail", reason="partial:mines_time_exit")
                if goal.mines_exit is None:
                    self.mines_exit_pin(goal, snap.motor.location, {"reason": "skill_time_window", "kills": 0})
                state["wave1_driver"], state["wave1_expected"] = "exit", "exited_mines"
        if run.call.name == "mesin" and state.get("campaign_task") == "machine":
            c = self._skill_session.campaign
            # Two food slots per remaining day is the maximum S5 consumption path.
            state["campaign_machine_food_keep"] = 2 * (4-len(c.days))
            qid = state.get("campaign_machine_input")
            if qid and not self._skill_finish_ready(goal, request):
                if (food := machine_input(context, state["campaign_machine_food_keep"])) is None or food["qualified_item_id"] != qid:
                    return skills.Step("fail", reason="machine_input_reserve_required")
                if not any(f.get("qualified_item_id") == qid for f in snap.selectable_items):
                    if state.get("campaign_machine_staged"):
                        return skills.Step("fail", reason="machine_input_not_offered")
                    state["campaign_machine_staged"] = True
                    goal.item_stage_want = qid
                    self._stage_end_resume("item_to_toolbar", goal, force=True)
                    return skills.Step("decision", dict(kind="wait", reason="item_to_toolbar", ms=0))
        driver = state.get("wave1_driver")
        from arti_stardew_skills_mines import ladder_choice, ladder_rock_pinned
        if ((run.call.name == "lawan_mine" or ladder_rock_pinned(state)) and driver == "rock"
                and not self._skill_finish_ready(goal, request)
                and ladder_choice(snap, context, state, skills._wave1_xy(snap.motor.tile)) is not None
                and not skills.menu_up(snap)):
            # A fresh native ladder supersedes walking/mining the old rock.
            # Re-pinning clears its tool stage; no break is credited.
            # FINAL B3 (J L4195-L6060): only a ladder NOT refused for this floor visit supersedes. The
            # refused 12,21 ladder kept re-planning the same rock: 153 zero-input legs, 45 wall s.
            return skills.wave1_plan(run.call, snap, state, context)
        if run.call.name == "lawan_mine" and driver in ("rock", "ladder") and not self._skill_finish_ready(goal, request):
            from arti_stardew_combat import idle_hp_floor
            import re
            from arti_stardew_skills_mines import combat_due
            floor = re.fullmatch(r"UndergroundMine(\d+)", str(snap.motor.location or ""))
            # W1 gap S15c: the same combat predicate as the planner (contact defence / bounded re-arm after the
            # floor's give-up). A ladder leg after the recorded kill keeps walking: the ladder is the escape.
            due = combat_due(state, context.get("combat") or {}, int(floor[1]) if floor else 0)
            if driver == "ladder" and (state.get("wave1_kills", 0) > 0 or due == "exhausted"):
                due = None  # #316: an exhausted contact keeps walking the pinned ladder (the escape)
            if idle_hp_floor(context.get("combat")) is not None or due is not None:
                return skills.wave1_plan(run.call, snap, state, context)
        if driver == "gift_close":
            if not skills.menu_up(snap) and state.get("wave1_gift_verified"):
                if run.call.name == "surat_quest":
                    from arti_stardew_skills_attempts import gift_is_not_delivery
                    return gift_is_not_delivery(state)
                return skills.Step("done", reason="observed_extras_effect")
            key = snap.ui_key_target or {}
            if key.get("kind") in ("question", "npc_question"):
                return skills.Step("fail", reason="unknown_menu_choice")
            close = skills._menu_close(snap)
            return skills.Step("decision", close) if close else skills.Step("fail", reason="dialogue_not_closed")
        # Gift names are owner input. The existing disposal policy additionally excludes
        # protected IDs; the native gift rule still checks quest/special/equipment flags.
        if driver == "gift":
            import arti_stardew_economy as economy
            item_name = run.call.arg.rsplit(" ke ", 1)[0].casefold()  # ADR 149: Live7 args carry the bag's own case
            slots = [f for f in context.get("inventory", ()) if str(f.get("name") or f.get("display_name") or "").casefold() == item_name]
            if len(slots) != 1:
                return skills.Step("fail", reason="gift_item_unavailable_or_ambiguous")
            qid = slots[0].get("qualified_item_id")
            policy = economy.ItemPolicy.from_config(self._config)
            if run.state.get("live7"):
                # ADR 149: free play judges a gift like a sale (owner B: tools and quest items only); the capture decides.
                from arti_runtime.social_facts import gift_refusal
                if gift_refusal(slots[0]) is not None:
                    return skills.Step("fail", reason="gift_policy_refused")
            elif policy.refuse_gift("sell", qid, None) is not None:
                return skills.Step("fail", reason="gift_policy_refused")
            ready = self._skill_item_ready(run, goal, snap, context, {qid}, "gift_item")
            if ready is not None:
                return ready
        if driver == "exit" and goal.mines_exit is None:
            self.mines_exit_pin(goal, snap.motor.location, {"reason": state.get("wave1_hold", "skill_floor_five"), "kills": state.get("wave1_kills", 0)})
        if driver in ("combat", "combat_floor") and run.call.name == "lawan_mine":
            from arti_stardew_combat import is_weapon
            held = (snap.held_item or {}).get("qualified_item_id")
            if is_weapon(held):
                run.state["wave1_weapon_in_hand"] = True
            else:
                qids = {f.get("qualified_item_id") for f in context.get("inventory", ())
                        if is_weapon(f.get("qualified_item_id"))}
                if qids:
                    ready = self._skill_item_ready(run, goal, snap, context, qids, "combat_weapon")
                    if ready is not None:
                        return ready
                    run.state["wave1_weapon_in_hand"] = True
        if driver in ("combat", "combat_floor", "eat", "gift", "exit"):
            # Own the already verified weapon/item staging, HP-floor/S5 handoff,
            # six-call reflex, moving-NPC gift controller and forty-step exit.
            # An interrupted rock leg still has tool_work. The generic stage
            # dispatcher runs that before mines_exit, so dispatch exit directly.
            raw = self._mines_exit_invoke(request) if driver == "exit" else self._stage_invoke(request)
            if raw is not None:
                return raw
            if self._skill_finish_ready(goal, request):
                return skills.Step("decision", skills.FINISH)
            if driver == "exit" and getattr(goal.extras_objective, "blocker", None):
                return skills.Step("fail", reason="mine_recovery_failed:" + goal.extras_objective.blocker)
            return skills._wait(state, "wave1_rule", "existing_rule_unavailable:" + driver,
                                "Amati hasil langkah yang sedang berjalan", skills.WAIT_S)
        if driver == "place" and not self._skill_finish_ready(goal, request):
            # S18 stages exactly the requested family; a seed/sprinkler cannot substitute for a chest.
            chest = run.call.name == "pasang" and run.call.arg in ("peti", "chest")
            allowed = ("(BC)130",) if chest else ("(O)599", "(O)621", "(O)645")
            qids = {f.get("qualified_item_id") for f in context.get("inventory", ())
                    if f.get("qualified_item_id") in allowed}
            if len(qids) != 1:
                return skills.Step("fail", reason=("chest" if chest else "sprinkler") + "_unavailable_or_ambiguous")
            if (snap.held_item or {}).get("qualified_item_id") not in qids:
                return self._skill_item_ready(run, goal, snap, context, qids, "placement_item")
        from arti_runtime.festival_facts import completed_step as festival_done
        if (done := festival_done(run.call, snap, state)) is not None:
            return done
        # Unexpected menus use the established inspect/close path; unknown questions hold.
        if skills.menu_up(snap) and driver not in ("talk",):
            key = snap.ui_key_target or {}
            if key.get("kind") in ("question", "npc_question"):
                return skills.Step("fail", reason="unknown_menu_choice")
            close = skills._menu_close(snap)
            if close is None and driver == "forage" and self._skill_finish_ready(goal, request):
                # ADR 136: a found quest item's dialogue opens in the press frame; its close can reach the page a read later. The pickup is verified.
                return skills._wait(state, "found_dialogue", "menu_not_closable", "Amati dialog hasil pungut", skills.WAIT_S)
            return skills.Step("decision", close) if close else skills.Step("fail", reason="menu_not_closable")
        if self._skill_finish_ready(goal, request):
            return skills.Step("decision", skills.FINISH)
        step = skills.wave1_step(run.call, snap, state, context)
        def clean():  # only mines runs read these runtime fences (other skills' doubles may lack them)
            return (context.get("ui_complete") and not skills.menu_up(snap) and snap.motor.is_player_free is True
                    and not self._gather_mutation_unresolved and not goal.gather_uncertain
                    and (last is None or skills._g(last, "cleanup_confirmed") is True
                         and skills._g(last, "lifecycle") in ("completed", "rejected", "not_taken")))
        if (run.call.name in skills.MINES_SKILLS and state.get("wave1_driver") == "rock"
                and step.kind == "fail" and step.reason == "tool_target_not_offered"
                and context.get("local_window") is not None and clean()):
            # B34: a cached approach arrival is not a native tool target. Exclude
            # only this unoffered pin for this floor visit; never credit a break.
            ref = state.get("wave1_pin", {}).get("ref")
            if ref and ref not in state.setdefault("wave1_unreachable", []):
                state["wave1_unreachable"].append(ref)
                state.update(wave1_driver=None, wave1_text="skill " + run.call.name, mines_refresh_due=True)
                return skills.Step("next_leg", reason="stone_not_offered_for_floor_visit")
        if (run.call.name in skills.MINES_SKILLS and state.get("wave1_driver") == "ladder"
                and step.kind == "fail" and step.reason == "target_not_offered:ladder"
                and str(snap.motor.location).startswith("UndergroundMine") and clean()):
            # W1 gap B5: a ladder whose native A was never offered is refused for this floor visit only;
            # mining/survey resumes from fresh offers. No descent is credited and no ladder is created.
            from arti_stardew_skills_mines import ladder_refused
            if ladder_refused(state, context.get("extras", ())):
                state.update(wave1_driver=None, wave1_text="skill " + run.call.name, mines_refresh_due=True)
                return skills.Step("next_leg", reason="ladder_not_offered_for_floor_visit")
        return step

    @W.span('arming', 10, runtime=True, lifecycle='start')
    def _skill_start(self, session: SkillSession, call: skills.SkillCall, *, by: str, facts=None, resume=None):
        """One skill = one fresh goal through the unchanged owner ingress with the skill's canonical objective text."""
        from arti_stardew_runtime import OwnerTurnResult
        if session.mode == "part_live" and session.campaign is not None:
            if self.live7_stop_file_check():
                return OwnerTurnResult(admitted=False, reason="live7_stop")
            fresh, motor, goods_context, held = self._live7_read(session)
            if held:
                session.campaign.live.wait_since = session.campaign.live.wait_since or game_now(self)
                return OwnerTurnResult(admitted=True, reason="live7_waiting_for_readback")
            ended = self._live7_guard_day(session, motor, allow_answered_sleep=resume is not None)
            if ended is not None:
                return ended
            facts = fresh
            if call.slot is not None and call.name in skills.GOODS_SKILLS:
                from arti_stardew_live7 import goods_facts
                from arti_stardew_skills_goods import stack_current
                if not stack_current(call, goods_facts(goods_context)):
                    return OwnerTurnResult(admitted=False, reason="goods_stack_changed")
        from arti_runtime.decision_plan import proposal_valid
        if not proposal_valid(session, call):
            return OwnerTurnResult(admitted=False, reason="decision_plan_interrupted")
        spec = skills.REGISTRY[call.name]
        if resume is not None and session.campaign is not None and facts is None:
            facts = self._skill_current_facts()  # storage quantities survive travel/resume
        text = skills.objective_text(call, facts)
        if spec.family == skills.F_WAVE1 and resume is not None:
            text = resume.state.get("wave1_text", text)
        clearing = resume.state.get("route_clear") if resume is not None else None
        if clearing:
            text = clearing["text"]
        pet_phase = (call.name == "rawat_hewan" and resume is not None
                     and resume.state.get("animal_care_verified") and resume.state.get("animal_pet_after"))
        if pet_phase:
            text = "elus peliharaan"
        with self._lock:
            if session is not self._skill_session or session.status != "active":
                return OwnerTurnResult(admitted=False, reason="skill_session_inactive")
            if not proposal_valid(session, call):
                return OwnerTurnResult(admitted=False, reason="decision_plan_interrupted")
            budget = self._skill_budget_left(session)
            if resume is None and budget["skills_left"] <= 0:
                self._journal({"ev": "skill_budget_exhausted", "session_id": session.session_id, "scope": "skills"})
                return self._skill_end_locked(session, "held", "partial", "skill_budget_exhausted")
            run = resume or SkillRun(call, text, by, game_now(self), self._physical_action_publications)
            if resume is None:
                from arti_runtime.bag_sitting import start_state
                run.state.update(start_state(session, call))
            if session.mode == "part_live":
                run.state["live7"] = True
            if resume is None and session.campaign is not None:
                run.state["campaign_task"] = session.campaign_start
                if (session.campaign.part == "b" and session.campaign.days
                        and session.campaign_start == "sleep"):
                    # B7: 137 travel + 120 home inputs hit home's standalone cap at the
                    # offered cabin door. The existing 280 reserve covers the WHOLE row,
                    # not a new allowance at each hop. Preface/other parts stay unchanged.
                    from arti_stardew_part_b import SLEEP_RESERVE
                    run.state["campaign_sleep_input_cap"] = SLEEP_RESERVE
                if "work_caps" in session.campaign.travel:
                    run.state["campaign_work_caps"] = session.campaign.travel["work_caps"]
                if "input_ceiling" in session.campaign.travel:
                    run.state["campaign_input_ceiling"] = session.campaign.travel["input_ceiling"]
                if "mines_exit_due" in session.campaign.travel:
                    run.state["campaign_mines_exit_due"] = session.campaign.travel["mines_exit_due"]
                if "ore_search_due" in session.campaign.travel:
                    run.state["campaign_ore_search_due"] = session.campaign.travel["ore_search_due"]
                if session.campaign_start == "animals" and session.campaign.animal_deferred:
                    run.state["campaign_animal_resume"] = dict(session.campaign.animal_deferred)
                run.state["campaign_inventory_start"] = dict(facts.inventory) if facts is not None else {}
                session.campaign.starts += 1
                run.state["campaign_travel"] = bool(session.campaign.travel.get("active"))
                if run.state["campaign_travel"]:
                    from arti_stardew_part_a import CAMPAIGN_INPUT
                    remaining = max(0, (CAMPAIGN_INPUT.get(session.campaign.part, float("inf")) if self._action_session_limit is None
                                        else self._action_session_limit)-self._physical_action_publications)
                    seconds = max(0, (self._trial_deadline_unix_ms-time.time()*1000)/1000) if self._trial_deadline_unix_ms is not None else 5400
                    run.state["campaign_travel_caps"] = ((float("inf"),) * 3 if session.mode == "part_live"
                                                         else (4096, remaining, seconds))
            if resume is None:
                session.runs.append(run)
                self._skill_day["skills"] += 1
            else:
                run.objective_text, run.goal_id, run.status, run.idle_since = text, None, "starting", None
                if spec.family == skills.F_WAVE1 or clearing or run.state.pop("route_resuming", False):
                    run.pending = None
            run.state["animal_pet_phase"] = pet_phase
            session.current = run
            from arti_runtime.decision_plan import started
            started(session, call)
            entry_budget = None
            if (call == skills.SkillCall("pergi_ke", "farm")
                    and session.index < len(session.plan)):
                following = session.plan[session.index]
                entry_budget = session.farm_entry_attempted.get((following.name, following.arg))
            if entry_budget is None:
                entry = session.farm_entry_attempted.get((call.name, call.arg))
                if entry is not None:
                    if entry not in session.farm_work_started:
                        caps = skills.skill_costs(call)
                        if session.composite in ("tanam", "siram"):
                            work = [c for c in session.plan if c.name != "pergi_ke"
                                    and session.farm_entry_attempted.get((c.name, c.arg)) == entry]
                            caps = (sum(skills.skill_costs(c)[0] for c in work),
                                    skills.plan_input_envelope(session.goal_text),
                                    min(600, sum(skills.skill_costs(c)[2] for c in work)))
                        session.farm_work_started[entry] = (game_now(self), self._physical_action_publications, caps)
                    entry_budget = session.farm_work_started[entry]
            if entry_budget is not None:
                run.started_at, run.inputs_start, run.state["farm_entry_caps"] = entry_budget
                run.decisions = max([run.decisions] + [r.decisions for r in session.runs
                    if r is not run and r.started_at == run.started_at
                    and r.state.get("farm_entry_caps") == run.state["farm_entry_caps"]])
            # Live7 WP4: chooser is exact. "model" only for the fresh start of a model choice (by="model", or a
            # model remedy started through the plan); a resumed leg/hop is "plan" even when its run says by=model.
            pick = getattr(session, "model_pick", None) if resume is None else None
            model = resume is None and (by == "model" or pick is not None and pick[0] == call)
            if model and (leave := getattr(session, "festival_leave_pick", None)) and leave[0] == call:
                from arti_stardew_event_control import festival_free_roam, festival_seated
                movement = self._event_movement_offer(self._binding_view()[1])
                # Chosen from a festival seat, the trip stands her up first, then asks the same leave.
                seat_offer = getattr(self, "_event_actions_offer", None)
                actions = None if festival_free_roam(movement) or seat_offer is None else seat_offer(self._binding_view()[1])
                seated = festival_seated(actions) and actions.get("event_id") == leave[1]
                if festival_free_roam(movement) and movement.get("festival_id") == leave[1] or seated:
                    run.state["festival_leave_chosen"] = leave[1]
                if seated:
                    run.state["festival_stand_chosen"] = leave[1]  # only her own seated pick stands her up
            if model:
                session.model_pick = None
                session.festival_leave_pick = None
            self._journal({"ev": "skill_chosen", "session_id": session.session_id, "skill": call.name, "arg": call.arg,
                           "by": by, "chooser": "model" if model else "plan",
                           "request_index": pick[1] if model and pick is not None else None})
        # W1 gap S15c (journal-attempts15route L699-L745): a growth-stage 3-4 route tree is a tool_work
        # cut_small_tree leg only. Pinning S6's one_tree criterion as well made the guarded write run the
        # mature-tree gather gate after tool_work had counted the step: the swing was refused not_taken
        # (gather_permitted_adjacent_target_unavailable) and tool_work_use_cycle_pending held forever.
        from arti_stardew_debris_route_runtime import small_tree_target
        pin = SkillPin(session.session_id,
                       # A clear leg's pin comes from its clear target only: a Live7 tebang clearing a stone off
                       # its approach must not inherit the mature-tree gather gate (review sat-path r1 P1).
                       episode_plan=skills.one_tree_no_store_plan() if (
                           clearing["target"]["kind"] == "tree" and not small_tree_target(clearing["target"])
                           if clearing else spec.family == skills.F_GATHER) else None,
                       store_quality=call.quality, goods_slot=call.slot,
                       goods_quality=call.quality if call.slot is not None else None)
        tile = call.tile if spec.family == skills.F_WALK and not clearing else None
        result = self.decide_owner_turn(text="main mandiri: " + text, trigger_type=session.trigger_type,
                                        viewer_name=session.viewer_name, replace_goal=True,
                                        completion_tile=tile, skill_pin=pin)
        with self._lock:
            if run.status == "starting":
                if not result.admitted:
                    run.pending = ("fail", "start_refused:" + str(result.reason))
                    run.status = "running"
                elif run.goal_id is None and self._active is not None:
                    run.goal_id = self._active.goal_id
                    run.status = "running"
            self._journal({"ev": "skill_leg_started" if resume is not None and session.campaign is not None else "skill_started",
                           "session_id": session.session_id, "skill": call.name,
                           "arg": call.arg, "goal_id": run.goal_id, "objective": spec.objective,
                           "at": time.time(), "admitted": result.admitted, "first_reason": result.reason})
            self._worker_wakeup.notify_all()
        return result

    # ------------------------------------------------------------------ in-skill driver (provider hook)
    def _skill_finish_ready(self, goal, request) -> bool:
        """The existing objective's own verifier reports achieved now (families finished by a model finish)."""
        try:
            for verdict in (self._routine_finish_verdict(goal), self._economy_finish_verdict(goal),
                            self._extras_finish_verdict(goal), self._interact_finish_verdict(goal, request)):
                if verdict is not None and verdict.status == "achieved":
                    return True
        except Exception:  # noqa: BLE001 - a verdict read never raises into the driver
            return False
        return False

    def _skill_arrival_before_reserve(self, run, goal, request):
        """A fresh, achieved navigation verdict predates the reserve's bookkeeping.

        a52 d10 J2353/J2362 and d23 J1853/J1861 crossed successfully and were
        then labelled curfew failures. Reuse the objective's own verifier; an
        auxiliary clear, unreconciled action or other task cannot finish here.
        """
        if run.call.name != 'pergi_ke' or run.state.get('route_clear'):
            return None
        verdict = self._interact_finish_verdict(goal, request)
        if verdict is not None and verdict.status == 'achieved':
            self._journal(dict(ev='travel_arrival_at_reserve', destination=run.call.arg, reason=verdict.reason))
            return 'done', verdict.reason
        return None

    def _skill_invoke(self, request):
        """_invoke_provider hook: the running skill's deterministic decision JSON, or None (provider as before)."""
        wait_resume(self)
        if not self._skills_enabled:
            return None
        with self._lock:
            session = self._skill_session
            if session is None or session.status != "active":
                if request.goal_id in self._skill_ended_goals:
                    self._journal({"ev": "skill_ended_goal_refused", "reason": request.reason})
                    return self._skill_wait_json(request)
                return None
            run = session.current
            if run is not None:
                run.state["_game_now"] = game_now(self)
            if run is not None and run.goal_id == request.goal_id and "goods_remaining" in run.state:
                self._skill_goods_menu_guard(run, request.snapshot.motor)
            own_goals = {r.goal_id for r in session.runs if r.goal_id} | ({session.goal_id} - {None})
            if run is None and session.binding_goal:
                session.goal_id = request.goal_id  # the session goal: wait for the boundary's model choice
                session.binding_goal = False
            elif (run is not None and run.status == "starting" and run.goal_id is None and self._active is not None
                  and request.goal_id == self._active.goal_id and self._active.text == run.objective_text):
                run.goal_id = request.goal_id
                run.status = "running"
            if run is not None and session.blocked_refs:
                self._skill_blocked_refs_refresh(session)
            if (run is not None and run.call.name == "pegang" and run.goal_id == request.goal_id
                    and self._active is not None and self._active.goal_id == run.goal_id):
                self._active.item_stage_want = skills.tool_qid(run.call.arg)  # D-B7b-2: the stage may move it
            if (run is not None and run.goal_id == request.goal_id and self._active is not None
                    and self._active.goal_id == run.goal_id and session.blocked_refs):
                # D-B7-4 (live V2B7SKL5b: the second tebang re-picked tree 42,10, blocked by the first): a skill's new goal
                # starts with the session's blocked targets.
                self._active.approach_blocked_refs |= session.blocked_refs
            if run is None or run.goal_id != request.goal_id:
                if request.goal_id not in own_goals | ({session.goal_id} - {None}):
                    # A goal outside the session while it is active: refused (a wait), never the provider.
                    if run is not None and run.status == "running":
                        run.foreign_model_calls += 1
                    self._journal({"ev": "skill_foreign_request", "session_id": session.session_id,
                                   "skill": run.call.name if run is not None else None, "reason": request.reason})
                    return self._skill_wait_json(request)
                run = None  # the session's own goal outside a running skill: never the provider (wait below)
        self._start_provider(request, lambda: None)  # same admission fence as an injected provider
        with self._lock:
            goal = self._active
            if run is not None and run.pending is None:
                run.pending = self._skill_mines_idle_damage(run, goal, self._state.latest(self._binding_view()[1]))
            if run is not None and self._stage_lock_holds() and not run.state.get("mines_idle_handoff"):
                return self._stage_invoke(request)  # locked inventory worker owns its native menu until cleanup
            choice = None
            pace = 0.0
            if run is not None and run.pending is None and goal is not None and goal.goal_id == run.goal_id:
                if run.call.name == "rawat_hewan":
                    basis = (goal.play_identity, self._skill_day_roll()["date"])
                    if run.state.setdefault("animal_owner_basis", basis) != basis:
                        run.pending = ("fail", "animal_skill_epoch_changed")
                spec = skills.REGISTRY[run.call.name]
                if run.call.name == "aksi":
                    from arti_runtime.mines_exit import ups
                    if (any(t.get("kind") == "ladder_up" and t.get("ref") == run.call.arg
                            for t in skills._g(request.snapshot, "ordinary_interactions", ()) or ())
                            or str(request.snapshot.motor.location or "").startswith("UndergroundMine")
                            and any(t["ref"] == run.call.arg for t in ups(
                                self._skill_wave1_context(request.snapshot), request.snapshot.motor.location))):
                        run.state["mines_up_action"] = True
                if session.mode == "part_live" and session.campaign is not None:
                    if run.call.name == "pergi_ke":
                        location = request.snapshot.motor.location
                        path = run.state.setdefault("travel_observed_route", [])
                        if location and (not path or path[-1] != location):
                            path.append(location)
                    if run.call.name in ("jual", "kirim"):
                        self._live7_remember_quest_refusals(self._skill_wave1_context(request.snapshot))
                    ended = self._live7_guard_day(session, request.snapshot.motor, allow_answered_sleep=True)
                    if ended is not None:
                        return self._skill_wait_json(request)
                from arti_runtime.mines_exit import reserve_exempt as mines_escape
                if (session.mode == "part_live" and session.campaign is not None and session.campaign.task != "sleep"
                        and not mines_escape(run, request.snapshot.motor.location)):
                    from arti_runtime.travel_time import reserve
                    clock_now = request.snapshot.motor.time_of_day
                    if type(clock_now) is not int or reserve(request.snapshot.motor.location, clock_now, session.campaign.live)["active"]:
                        session.campaign.live.sleep_only = True
                        from arti_stardew_interact_b_runtime import home_facility
                        from arti_stardew_interact_b import curfew_phase
                        if (type(clock_now) is not int or curfew_phase(clock_now) == "hard"
                                or home_facility(self, session, run) is None):
                            run.pending = self._skill_arrival_before_reserve(run, goal, request) or ("fail", "curfew")
                decision_cap, input_cap, seconds_cap = self._part_a_caps(run)
                if run.state.get("live7") and run.call.name == "forage":
                    # R6: visible forage beyond the local twelve-tile admission needs a
                    # native walk first; the original sixteen-input S21 cap cannot cover it.
                    decision_cap, input_cap, seconds_cap = 256, 160, 300
                if run.call.name == "makan":
                    input_cap = max(0, input_cap - 1)  # reserve one select_slot for terminal food cleanup
                    seconds_cap = max(0, seconds_cap - 5)  # leave time for its bounded native readback
                if run.call.name == "langkah" and (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True:
                    input_cap = max(input_cap, 4)
                    decision_cap = max(decision_cap, 12)
                if run.call == skills.SkillCall("pergi_ke", "farm"):
                    hop = goal.home_route_state
                    if "input_budget" in hop:
                        input_cap = min(input_cap, hop["inputs_start"] - run.inputs_start + hop["input_budget"])
                if "farm_entry_caps" in run.state:
                    caps = run.state["farm_entry_caps"]
                    decision_cap, input_cap, seconds_cap = caps
                if run.call.name == "pasang":
                    here = skills._wave1_xy(request.snapshot.motor.tile)
                    tile = run.call.tile
                    walking = (not run.state.get("wave1_driver") and here is not None and tile is not None
                               and abs(here[0]-tile[0])+abs(here[1]-tile[1]) != 1)
                    if walking and "placement_work_start" not in run.state:
                        decision_cap, input_cap, seconds_cap = skills.skill_costs(skills.SkillCall("pergi_ke", "farm"))
                        input_cap = min(input_cap, max(0, (self._action_session_limit or input_cap)-run.inputs_start))
                    else:
                        offset = run.state.setdefault("placement_work_start", (
                            run.decisions, self._physical_action_publications-run.inputs_start,
                            game_now(self)-run.started_at))
                        decision_cap += offset[0]
                        input_cap += offset[1]
                        seconds_cap += offset[2]
                    run.state["placement_seconds_cap"] = seconds_cap
                if run.stage_start is None:
                    run.stage_start = getattr(self, "_stage_publications", 0)
                # FIXB12 D-B9-4 (live V2B9SKLpegang-a: the item stage moved the pickaxe under its lock, then pegang failed
                # skill_budget_inputs 2): presses of a locked worker stage count as stage_inputs, not the skill's.
                run.stage_inputs = getattr(self, "_stage_publications", 0) - run.stage_start
                inputs = self._physical_action_publications - run.inputs_start - (
                    0 if run.call.name in ("rawat_hewan", "ikuti_festival") or spec.family == skills.F_WAVE1
                    or "farm_entry_caps" in run.state or "campaign_task" in run.state
                    or "route_caps" in run.state else run.stage_inputs)
                if getattr(goal, "food_reselect_due", False) and run.pending is None:
                    tools = [t for t in request.snapshot.selectable_items
                             if str(t.get("qualified_item_id", "")).startswith("(T)")]
                    retries = run.state.get("food_reselect_attempts", 0)
                    if (tools and request.snapshot.select_item_available and retries < 2
                            and inputs < input_cap and run.decisions < decision_cap
                            and game_now(self)-run.started_at < seconds_cap):
                        run.state["food_reselect_attempts"] = retries + 1
                        run.decisions += 1
                        return self._rule_json(request, {"kind": "act", "operation": "select_item", "target_ref": None,
                            "args": dict(tools[0]), "evidence_refs": []}, "skill_food_reselect", "select_tool")
                    run.pending = ("fail", "food_reselect_unavailable")
                aggregate_spent = (session.composite == "siram" and session.runs
                    and self._physical_action_publications - session.runs[0].inputs_start
                        >= skills.plan_input_envelope(skills.SKILLS_V1_GOALS["siram"]))
                if run.call.name == "siram" and not run.state.get("live7") and self._skill_no_dry_crops(request.snapshot):
                    run.pending = ("done", "observed_no_dry_crops")
                # D-B3-3 (live L2: tebang failed skill_budget_inputs 31 > 30 AFTER the tree was down and the wood picked
                # up): an objective its own verifier already reports achieved finishes before the input / decision caps
                # are checked (the finish adds no input). The seconds cap still applies.
                finishing = (run.pending == ("done", "observed_no_dry_crops") or self._skill_finish_ready(goal, request)
                             or getattr(goal.gather_progress, "status", None) == "INVENTORY_SATISFIED"
                             or (run.call.name == "ikuti_festival" and run.state.get("festival_phase") == "seat_wait"
                                 and skills._event_receipt(request.snapshot, run.state.get("seat_source"), "FestivalSeated")
                                 and not skills.menu_up(request.snapshot)))
                if not finishing and run.pending is None:
                    run.pending = (self._live7_exhaustion(run, goal, skills._g(request.snapshot, "motor"))
                                   or self._live7_attack(run, goal, skills._g(request.snapshot, "motor"))
                                   # ADR 195: a map change re-observes and continues; this returns None unless a real stop
                                   or self._live7_route_pause(run, goal, skills._g(request.snapshot, "motor"))
                                   or self._skill_effect_watchdog(run, goal, skills._g(request.snapshot, "motor")))
                if not finishing and (aggregate_spent or (self._action_session_limit is not None
                        and self._physical_action_publications >= self._action_session_limit)):
                    # FIXB11 D-B8-4: the trial's approved inputs are spent: a terminal, named failure, never a stall.
                    run.pending = ("fail", "input_envelope_exhausted")
                elif run.decisions >= decision_cap and not finishing:
                    run.pending = ("fail", "skill_budget_decisions")
                elif inputs >= input_cap and not finishing:
                    run.pending = ("fail", "skill_budget_inputs")
                elif game_now(self) - run.started_at > seconds_cap:
                    run.pending = ("fail", "skill_budget_seconds")
                else:
                    repair = (self._debris_route_step(run, goal, request, (decision_cap, input_cap, seconds_cap))
                              if run.pending is None else None)
                    if run.pending is None and repair is None and spec.family == skills.F_TOOL and not finishing:
                        progress = skills._g(request.snapshot.working_memory, "tool_work")
                        from arti_stardew_tool_work import KIND_TOOL_QIDS
                        want = skills._g(progress, "tool_qid")
                        wanted = KIND_TOOL_QIDS.get(skills._g(progress, "kind"), (want,) if want else ())
                        if wanted and skills._equipped(request.snapshot) not in wanted:
                            repair = self._skill_item_ready(run, goal, request.snapshot,
                                self._skill_wave1_context(request.snapshot), wanted, "tool")
                    if (run.pending is None and repair is None and run.call.name == "rawat_hewan" and not run.state.get("animal_pet_phase")
                            and getattr(goal, "animal_executor", None)):
                        executor = goal.animal_executor
                        if executor["status"] == "blocked":
                            run.pending = ("fail", executor.get("exception", "animal_routine_blocked"))
                        elif finishing:
                            # The finish verifier may mark the executor complete.
                            # Do not send it back through the running-only stage hook.
                            pass
                        else:
                            # Reuse the stage hook's item staging, curfew, animal planner and readbacks.
                            raw = self._stage_invoke(request)
                            if raw is not None:
                                return raw
                            run.pending = ("fail", executor.get("exception", "animal_executor_unavailable"))
                    if run.pending is None and run.call.name == "makan":
                        # Reuse the bounded eat stage: select food, then the native
                        # prompt in place, or its explicit safe-site hint if needed.
                        raw = self._routine_eat_stage_invoke(request)
                        if raw is not None:
                            run.decisions += 1
                            return raw
                    run.state["nav_objective"] = goal.interact_objective
                    snapshot = request.snapshot
                    if run.call.name in ("siram", "pecah_batu") and run.pending is None:
                        # The generic tool fallback used to approach every scene
                        # feature when its targets vanished (B19: Grass; M4: a
                        # blocked Stone). Only this tool's own targets may move us.
                        pin = snapshot.water_context or snapshot.movement_context
                        view = SimpleNamespace(**{key: skills._g(snapshot, key) for key in (
                            "working_memory", "motor", "equipped_tool", "selectable_items",
                            "select_item_available", "use_tool_available", "tool_work_target")})
                        view.approach_targets = tuple(self._tool_work_offered_entries(pin, snapshot))
                        if run.call.name == "siram":
                            self._live7_water_view(view, snapshot)
                            view = self._part_a_crop_view(view)
                        snapshot = view
                    from arti_runtime.mines_body import body_guard_running, note_body_busy
                    if run.pending is None and repair is None and body_guard_running(self):
                        # ADR 183 (a74 B d3 J2397: cek_jurnal tried to open its menu under a running guard):
                        # nothing steps on top of the guard; the step waits and re-observes after its end.
                        note_body_busy(self, run.call.name, "skill_step")
                        return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                           "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                           "speech": "", "choice": {"kind": "wait", "reason": "body_guard_running",
                                                                    "ms": 250}, "activity": None})
                    found = (self._skill_found_step(run, request)
                             if run.pending is None and repair is None else None)
                    recovery = (self._skill_mine_recovery_step(run, goal, request)
                                if run.pending is None and repair is None and found is None else None)
                    step = (skills.Step(run.pending[0], reason=run.pending[1]) if run.pending else
                            repair if repair is not None else
                            found if found is not None else
                            recovery if recovery is not None else
                            self._part_a_walk_step(run, snapshot) if (run.state.get("campaign_travel") or run.state.get("local_walk")) and run.call.name == "jalan_ke" else
                            self._skill_wave1_step(run, goal, request) if spec.family == skills.F_WAVE1 else
                            self._skill_harvest_step(run, goal, snapshot) if run.call.name == "panen" else
                            self._skill_till_step(run, goal, snapshot) if run.call.name == "cangkul" else
                            self._skill_plant_step(run, goal, snapshot) if run.call.name == "tanam" else
                            __import__("arti_stardew_generic_world").step(
                                run.call, snapshot, run.state, context=self._skill_wave1_context(snapshot), one_tile=True
                            ) if run.call.name == "langkah" and (getattr(self, "_config", None) or {}).get("stardew_live7_local_walks") is True else
                            self._live7_fish_step(run, snapshot) or self._live7_refill_step(run, snapshot) or skills.plan_step(
                                run.call, snapshot, finish_ready=self._skill_finish_ready(goal, request), state=run.state))
                    if spec.family == skills.F_GENERIC:
                        last = skills._g(skills._g(snapshot, "working_memory"), "last_action")
                        if skills._g(last, "world_effect_status") in ("observed_world_delta", "unchanged"):
                            run.state["generic_readback"] = skills._g(last, "after")
                    if isinstance(step, str):
                        run.decisions += 1
                        return step
                    if step.kind == "decision":
                        choice, pace = step.choice, step.pace
                        run.decisions += 1
                        self._journal({"ev": "skill_step", "session_id": session.session_id, "skill": run.call.name,
                                       "n": run.decisions, "kind": choice["kind"],
                                       "operation": choice.get("operation"), "target": choice.get("target_ref")})
                    else:
                        run.pending = (step.kind, step.reason)
                if run.pending is not None:
                    detail = run.state.pop("fail_detail", None) if isinstance(run.state, dict) else None
                    self._journal({"ev": "skill_step_boundary", "session_id": session.session_id,
                                   "skill": run.call.name, "outcome": run.pending[0], "reason": run.pending[1],
                                   **({"detail": detail} if detail else {})})
                    self._worker_wakeup.notify_all()
            if choice is None:
                choice = {"kind": "wait", "reason": "skill_boundary", "ms": 0}
        if pace:
            pin = request.snapshot.water_context or request.snapshot.movement_context
            if (run is not None and isinstance(choice, Mapping) and choice.get("question") == skills.EXIT_WAIT_QUESTION
                    and self._skill_route_reads_settling(pin)):
                self._skill_await_route_reads(pin, pace)  # fix90A / ADR 159: wait on the reads, not a fixed pace
            else:
                W.call('paced_inspect', 24, time.sleep, pace)  # a waiting inspect is paced like a fast model (never a tight decision loop)
        activity = skills.activity_for(run.call, choice) if run is not None else None
        return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                           "goal_revision": request.goal_revision, "context_ref": request.context_ref, "speech": "",
                           "choice": choice, "activity": activity}, ensure_ascii=False)

    @staticmethod
    def _skill_wait_json(request) -> str:
        return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                           "goal_revision": request.goal_revision, "context_ref": request.context_ref, "speech": "",
                           "choice": {"kind": "wait", "reason": "skill_refused", "ms": 0}, "activity": None})

    # ------------------------------------------------------------------ boundary (worker hook)
    def skill_boundary_due(self) -> bool:
        """Trial runner idle check (caller holds the lock): an active session in a READY runtime is never an idle
        trial; its own boundary hook ends a stalled skill and the session reports its outcome. A runtime that is not
        READY is not covered (the runner may HOLD; the session watchdog ends the session)."""
        from arti_stardew_runtime import READY
        session = getattr(self, "_skill_session", None)
        return bool(self._skills_enabled and session is not None and session.status == "active"
                    and (self._status == READY or session.mode == "part_live" and self._status == "PAUSED_RECOVERY")
                    and not self._worker_stop.is_set())

    def _skill_session_refusal(self, code) -> bool:
        """fix144A C8 (ADR 249): a precheck refused with a session-level cause (a latched HALT, a faulted record, a
        session that is not hers to drive) ends the running skill at once with that cause named. Before, it was
        refreshed as a malformed decision and the same step was tried again (bench 19: 7 refusals in 40 s, then the
        run stayed "running"). Nothing was published. A Live7 run ends through the recovery boundary, whose C7 rule
        closes it without waiting for a release proof that a HALT makes impossible. Caller holds the runtime lock."""
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        if (session is None or session.status != "active" or run is None or run.status != "running"
                or run.pending is not None):
            return False
        run.pending = ("fail", str(code))
        run.state["session_refusal"] = str(code)
        self._journal({"ev": "skill_step_boundary", "session_id": session.session_id, "skill": run.call.name,
                       "outcome": "fail", "reason": str(code), "site": "session_precondition"})
        live = getattr(getattr(session, "campaign", None), "live", None)
        if session.mode == "part_live" and live is not None:
            run.state["live7_recovery_requested"] = True
            live.wait_since = live.wait_since or game_now(self)
        self._worker_wakeup.notify_all()
        return True

    def _skill_rule_due(self) -> bool:
        try:
            return bool(self._store_rule_pending() or self.tool_work_finish_due() or self.fishing_finish_due()
                        or self.auto_act_due() or self._routine_holds_continuation()
                        or self._text_event_holds_continuation())
        except Exception:  # noqa: BLE001
            return False

    def _skill_native_finish_wins(self, run):
        """PM-1 a9 #5 (d11:J1938-1957): an earlier press stayed uncertain (its own FAULT), then native
        TransferObserved + finish_verified settled this goal. Never downgrade native done."""
        cause, goal = run.state.get("ui_uncertain_cause"), self._active
        finish = getattr(goal, "last_finish", None) if goal is not None and goal.goal_id == run.goal_id else None
        # ADR 112 (a50 T120/T229): a verified goods finish can arrive during
        # menu handling/cleanup, after a harmless opener or close named failure.
        # Cleanup, STOP and unresolved-action failures retain their own authority.
        expected = {"simpan_ke_chest": "observed_paired_transfer_objective", "kirim": "observed_ship",
                    "jual": "observed_sale", "beli": "observed_purchase"}.get(getattr(getattr(run, "call", None), "name", None))
        stale = {"MenuOpened", "ContainerOpened", "store_closed_by_choice", "shipping_closed_by_choice",
                 "shop_closed_by_choice", "purchase_refused_by_choice", "goods_menu_changed"}
        if (expected and getattr(finish, "status", None) == "achieved" and finish.reason == expected
                and run.pending is not None and run.pending[0] == "fail" and run.pending[1] in stale):
            run.pending = ("done", finish.reason)
            return True
        if cause and run.pending == ("fail", cause) and getattr(finish, "status", None) == "achieved" and finish.reason:
            run.pending = ("done", finish.reason)
            return True
        return False

    def _skill_run_outcome(self, session: SkillSession, run: SkillRun):
        """Keep optional pet results separate from the already verified livestock verdict."""
        handoff = self._skill_mines_idle_damage(run, self._active, self._state.latest(self._binding_view()[1]))
        if handoff is not None:
            return handoff  # immediate page; never repin the stalled skill into another automatic leg
        SkillRuntimeMixin._skill_native_finish_wins(self, run)
        if "goods_remaining" in run.state:
            self._skill_goods_menu_guard(run, self._state.latest(self._binding_view()[1]))
        if run.pending == ("fail", "goods_menu_changed"):
            return run.pending
        mines =session is not None and session.status == "active" and run.call.name in skills.MINES_SKILLS
        consumed = (mines and not run.state.get("mines_death")
                    and SkillRuntimeMixin._skill_mines_receipt_pending(run, self._active))
        if consumed:
            # #318: the eat/exit re-pin below replaces the leg; an achieved terminal receipt (the S15 kill credit,
            # the S23 node removal) is consumed once FIRST, then safety chooses the next input. A death still wins.
            outcome = SkillRuntimeMixin._skill_required_run_outcome(self, session, run)
        safety = self._skill_mines_safety(run, self._active) if mines else None
        if safety is not None:
            return safety
        if not consumed:
            outcome = SkillRuntimeMixin._skill_required_run_outcome(self, session, run)
        if (mines and consumed and outcome is not None and outcome[0] == "fail"
                and run.state.get("wave1_combat_timed_out") and not run.state.get("wave1_survival_exit")
                and run.state.get("wave1_driver") in ("combat_floor", "combat")
                and outcome[1] != "partial:mines_combat_disengaged"):
            # Consume the stopped combat leg before its terminal failure can enter the generic exit fallback.
            # A fresh leg drops its old terminal receipt/objective but retains the timeout and retreat budget.
            skills.wave1_repin(run.state, "skill lawan_mine", "disengage", "mines_disengaged")
            run.pending = None
            return "next_leg", "mines_combat_disengage"
        if (outcome is not None and outcome[0] == "fail" and run.call.name in skills.MINES_SKILLS
                and run.state.get("wave1_driver") == "eat"):
            # W1 gap B5 (S15E attempt 1): a refused meal (no_food_candidate after the backpack staging stopped
            # on unsafe:monster_in_radius) hands off to leaving, never to the same meal again on this run.
            run.state["mines_eat_refused"] = str(outcome[1])
        if (outcome is not None and outcome[0] == "fail" and run.call.name in skills.MINES_SKILLS
                and not run.state.get("wave1_terminal_exit_attempted")
                and not (outcome[1] == "partial:mines_combat_disengaged"
                         and (run.state.get("mines_disengage") or {}).get("verified") is True)):
            goal = self._active
            motor = self._state.latest(self._binding_view()[1])
            if (goal is not None and goal.goal_id == run.goal_id and motor is not None
                    and str(motor.location).startswith("UndergroundMine")
                    and getattr(goal, "mines_exit", None) is None
                    and not (goal.last_finish is not None and goal.last_finish.reason == "exited_mines")):
                # One common boundary covers wait exhaustion, failed legs and
                # idle terminals too. Reuse the SAME run/caps, with a fresh exit
                # goal so a failed combat/tool objective cannot preempt leaving.
                run.state.update(wave1_terminal_exit_attempted=True, wave1_hold=outcome[1],
                                 wave1_exit_due=True, alive_exit="unverified")
                run.pending = None
                skills.wave1_repin(run.state, "skill " + run.call.name, "exit", "exited_mines")
                run.state.pop("mines_refresh_due", None)
                return "next_leg", "terminal_native_exit:" + outcome[1]
        if outcome is not None and outcome[0] == "fail" and run.call.name in skills.ATTEMPT_NAMES:
            from arti_stardew_skills_attempts import finalize
            outcome = finalize(run.call, run.state, outcome, self._physical_action_publications)
        if (outcome is None or outcome[0] != "fail" or run.call.name != "rawat_hewan"
                or not run.state.get("animal_pet_phase") or not run.state.get("animal_care_verified")):
            return outcome
        goal = self._active
        action = goal.last_action if goal is not None else None
        if (self._gather_mutation_unresolved or goal is not None and goal.gather_uncertain
                or self._action_cancel.is_set() or self._worker_stop.is_set()
                or action is not None and (action.cleanup_confirmed is not True
                    or action.lifecycle not in ("completed", "rejected", "not_taken")
                    or action.world_effect_status == "uncertain")):
            return outcome  # Possible mutation/unclean input retains the existing fence.
        reason = (run.pending[1] if run.pending and run.pending[0] == "fail"
                  and run.pending[1].startswith("start_refused:") else outcome[1])
        run.state["animal_pet_status"] = "skipped_optional:" + reason
        last = goal.last_finish if goal is not None and goal.goal_id == run.goal_id else None
        run.state["animal_pet_observed"] = dict(last.observed) if last is not None else {}
        run.pending = None
        self._journal({"ev": "skill_pet_skipped", "session_id": session.session_id,
                       "skill": run.call.name, "status": run.state["animal_pet_status"],
                       "observed": run.state["animal_pet_observed"]})
        return "done", "observed_all_animals_cared_for"

    def _skill_required_run_outcome(self, session: SkillSession, run: SkillRun):
        """Under the lock: (done|fail, reason) when the running skill ended, else None."""
        if getattr(self, "_pause_boundary", lambda: False)():
            return None
        goal = self._active
        now = game_now(self)
        if run.status == "starting":
            # Only reachable when no thread is advancing (its ingress raised / never bound a goal).
            return ("fail", "start_stuck") if now - run.started_at > _START_STUCK_S else None
        if goal is None or goal.goal_id != run.goal_id:
            return ("fail", "goal_replaced")
        last = goal.last_finish
        if goal.progress_marker == 'terminal' and last is not None and (last.observed or {}).get('bag_sitting'):
            # ADR 222: a sitting verifies the complete named request and close,
            # not the old single-stack paired-transfer objective. Preserve its
            # partial/zero/uncertain verdict before generic economy wrapping.
            from arti_runtime.bag_sitting import finish_verdict
            status, reason = finish_verdict(last.observed['bag_sitting'])
            if (last.status, last.reason) == (status, reason):
                return ('done' if status == 'achieved' else 'fail'), reason
            return 'fail', 'bag_sitting_finish_unverified'
        if (goal.progress_marker == 'terminal' and last is not None and last.status == 'partial'
                and last.reason in ('observed_paired_transfer_partial', 'observed_ship_partial')
                and run.pending == ('fail', last.reason)):
            return run.pending  # keep the native partial reason through the generic economy boundary
        if (run.call.name == "simpan_ke_chest" and last is not None and last.status == "partial"
                and last.reason == "observed_paired_transfer_partial"
                and self._paired_store_observed(goal) is not None):
            return "fail", last.reason  # clean shortfall; observed amount stays in the row, never claimed complete
        if goal.progress_marker == "terminal" and last is not None and last.status == "achieved":
            if run.state.get("route_clear"):
                # A clear leg's receipt is judged by the clear verifier first (review sat-path r1 P1): a Live7
                # tanam/siram approach repair must resume its parent, never be read as the parent's own effect.
                return self._debris_route_completed(run, goal, last)
            if run.call.name == "tanam":
                if self._skill_plant_verified(run, goal):
                    return "done", last.reason
                since = run.state.setdefault("plant_readback_since", now)
                return ("fail", "plant_effect_unverified") if now - since >= skills.WAIT_S else None
            if run.state.get("campaign_mine_recovery") and run.call.name == "pergi_ke":
                pid = self._binding_view()[1]
                motor = self._state.latest(pid)
                if (last.reason != "exited_mines" or (last.observed or {}).get("exited_mines") !=
                        run.state["campaign_mine_recovery"] + "->Mine"
                        or motor is None or motor.location != "Mine" or self._state.is_stale(pid)
                        or type(getattr(motor, "health", None)) not in (int, float) or motor.health <= 0):
                    return "fail", "mine_recovery_arrival_unverified"
                if run.call.arg != "mine":
                    run.state.pop("campaign_mine_recovery")
                    run.state["route_resuming"] = True
                    run.pending = None
                    return "next_leg", "exited_mines_resume_destination"
                return "done", "exited_mines"
            receipt = (goal.goal_id, last.reason, (last.observed or {}).get("request_id"))
            if run.call.name in skills.MINES_SKILLS and run.state.get("mines_consumed_receipt") == receipt:
                # B6: a second boundary before goal replacement must not turn the old rock receipt into
                # a whole-skill success (driver is now None/eat/exit), or add its credit twice.
                return "next_leg", "mines_receipt_consumed"
            if session is not None and session.campaign is not None and session.campaign.part in ("b", "c"):
                # Preserve each native composition leg before re-pinning clears
                # last_finish (especially talk -> gift -> close).
                run.state.setdefault("campaign_native_legs", {})[goal.goal_id] = dict(
                    driver=run.state.get("wave1_driver"), reason=last.reason, observed=dict(last.observed),
                    npc=getattr(getattr(goal, "extras_objective", None), "name", None))
            if skills.REGISTRY[run.call.name].family == skills.F_WAVE1:
                if run.call.name == "tambang_bijih" and last.reason == "exited_mines":
                    # W1 gap B5: S23 credits the ore objective only after a native, fresh, alive exit.
                    motor = self._state.latest(self._binding_view()[1])
                    cf = self._combat_ui_facts(self._water_context(self._binding_view()[1])) or {}
                    hp = (cf.get("combat") or {}).get("hp")
                    if hp is None:
                        hp = skills._g(motor, "health")
                    alive_exit = (motor is not None and bool(motor.location)
                                  and not self._state.is_stale(self._binding_view()[1])
                                  and not str(motor.location).startswith("UndergroundMine")
                                  and type(motor.time_of_day) is int and motor.time_of_day <= 2500
                                  and type(hp) in (int, float) and hp > 0)
                    run.state["alive_exit"] = "verified" if alive_exit else "unverified"
                    if run.state.get("wave1_hold"):
                        hold = run.state["wave1_hold"]
                        return "fail", hold if alive_exit else hold + ":alive_exit_unverified"
                    if not alive_exit:
                        return "fail", "partial:ore_alive_exit_unverified"
                if run.call.name == "lawan_mine" and last.reason == "exited_mines":
                    motor = self._state.latest(self._binding_view()[1])
                    cf = self._combat_ui_facts(self._water_context(self._binding_view()[1])) or {}
                    hp = (cf.get("combat") or {}).get("hp")
                    # The optional combat projection can expire at the finish
                    # boundary; the bound motor still supplies native health.
                    if hp is None:
                        hp = skills._g(motor, "health")
                    if run.state.get("wave1_hold"):
                        alive_exit = (motor is not None and bool(motor.location)
                                      and not self._state.is_stale(self._binding_view()[1])
                                      and not str(motor.location).startswith("UndergroundMine")
                                      and type(motor.time_of_day) is int and motor.time_of_day <= 2500
                                      and type(hp) in (int, float) and hp > 0)
                        run.state["alive_exit"] = "verified" if alive_exit else "unverified"
                        hold = run.state["wave1_hold"]
                        from arti_runtime.mines_fight import close as fight_close
                        return fight_close(run, "fail", hold if alive_exit else hold + ":alive_exit_unverified")
                    # ADR 149: in free play a verified alive exit finishes the trip on any floor, with or without a kill.
                    if (not run.state.get("live7") and (run.state.get("wave1_floor", 0) < 5
                                                        or run.state.get("wave1_kills", 0) + getattr(goal.combat, "kills", 0) < 1)
                            or motor is None or str(motor.location).startswith("UndergroundMine")
                            or type(motor.time_of_day) is not int or motor.time_of_day > 2500
                            or type(hp) not in (int, float) or hp <= 0):
                        return "fail", "partial:mines_floor_kill_alive_exit_unverified"
                outcome = skills.wave1_completed(run.call, run.state, last.reason, last.observed)
                if run.call.name in skills.MINES_SKILLS and outcome[0] == "next_leg":
                    run.state["mines_consumed_receipt"] = receipt
                return outcome
            if run.call.name == "ikuti_festival":
                return run.pending if run.pending == ("done", "observed_festival_seated") else (
                    "fail", "festival_sequence_unverified")
            if run.call.name == "rawat_hewan":
                expected = "observed_extras_effect" if run.state.get("animal_pet_phase") else "observed_all_animals_cared_for"
                if last.reason != expected:
                    return ("fail", "finish_reason_mismatch:" + str(last.reason))
                if run.state.get("animal_pet_phase"):
                    if not run.state.get("animal_care_verified"):
                        return ("fail", "animal_care_unverified")
                    run.state["animal_pet_done"] = True
                    run.state["animal_pet_status"] = "verified"
                    run.state["animal_pet_observed"] = dict(last.observed)
                    return ("done", "observed_all_animals_cared_for")
                run.state["animal_care_verified"] = True
                run.state["animal_care_observed"] = dict(last.observed)
                if not run.state.get("animal_pet_done"):
                    facts = self._skill_current_facts()
                    if facts is not None and facts.location == "Farm" and facts.pet_available:
                        run.state["animal_pet_after"] = True
                        return ("next_leg", "animal_pet_offered_on_return")
                    run.state["animal_pet_status"] = "skipped_optional:pet_not_offered"
            if run.call.name == "makan":
                if (last.reason == "already_full" and last.observed.get("consumed") is False
                        and last.observed.get("origin") == "routine_eat_rule"
                        and last.observed.get("energy_health_full") is True):
                    return ("not_applicable", "already_full")
                if last.reason != "observed_eat_effect":
                    return ("fail", "finish_reason_mismatch:" + str(last.reason))
                if not eat_consumption_observed(last.observed):
                    return ("fail", "eat_consumption_not_observed")
            if run.call.name == "simpan_ke_chest" and last.reason != "observed_paired_transfer_objective":
                return ("fail", "finish_reason_mismatch:" + str(last.reason))
            return ("done", last.reason)
        if (run.call.name == "rawat_hewan" and run.state.get("animal_pet_phase")
                and last is not None and last.status in ("partial", "uncertain")):
            return "fail", last.reason
        if last is not None and last.status in ("partial", "uncertain") and (
                goal.routine_objective is not None and goal.routine_objective.blocker
                or goal.economy_objective is not None or run.call.name == "tangkap_ikan" and goal.fishing is not None):
            return "fail", "finish_" + last.status + ":" + last.reason
        if run.pending is not None:
            return run.pending
        if run.state.get("campaign_mine_recovery") and getattr(goal.extras_objective, "blocker", None):
            return "fail", "mine_recovery_failed:" + goal.extras_objective.blocker
        if run.call.name == "rawat_hewan":
            executor = getattr(goal, "animal_executor", None) or {}
            if executor.get("status") == "blocked":
                return "fail", executor.get("exception", "animal_routine_blocked")
        spec = skills.REGISTRY[run.call.name]
        if now - run.started_at > run.state.get("placement_seconds_cap", run.state.get("farm_entry_caps", self._part_a_caps(run))[2]):
            return ("fail", "skill_budget_seconds")  # applies even while a rule is due (rules keep their own bounds)
        action = goal.last_action
        if (session.mode == "part_a" and run.state.get("campaign_travel")
                and run.call.name == "jalan_ke"):
            from arti_stardew_part_a import unpublished_budget_refusal
            if unpublished_budget_refusal(action):
                return "fail", "approach_budget_exhausted"  # row gap, never another stalled refresh
        motor = self._state.latest(self._binding_view()[1])
        exhausted = (self._live7_exhaustion(run, goal, motor) or self._live7_attack(run, goal, motor)
                     or self._live7_route_pause(run, goal, motor))  # ADR 155/195: people page, or None to continue
        if exhausted is not None:
            return exhausted
        if (run.state.get("store_menu_handoff") and motor is not None and motor.active_menu is None
                and not self._state.is_stale(motor.player_id)):
            return "fail", "goods_menu_changed"
        watchdog = self._skill_effect_watchdog(run, goal, motor)
        if watchdog is not None:
            return watchdog
        routine = getattr(goal, "routine_objective", None)
        from arti_stardew_part_a import next_date
        if (run.call.name == "tidur" and skills._g(routine, "kind") == "sleep"
                and skills._g(routine, "answered") is True
                and skills._g(routine, "waiting_since") is not None
                and not skills._g(routine, "finish_recorded", False)
                and routine.start_date is not None and self._date_tuple(motor) == next_date(routine.start_date)):
            # The chooser can observe rollover during its last retry. Preserve
            # the answered bed until the sleep verifier runs; the input/time and
            # no-effect safety bounds above still apply. No action or credit.
            return None
        # PART_A30: a fresh observed arrival at a not-yet-credited target refills the idle allowance (once per ref).
        arrived = (skills.animal_arrival_progress(action, getattr(goal, "approach_progress", None),
                                                  run.state.get("stall_progress_request"),
                                                  run.state.get("stall_arrived_refs"))
                   if run.call.name == "rawat_hewan" else None)
        if run.call.name in skills.MINES_SKILLS and skills.wave1_stone_reach_failed(
                run.state, action, getattr(goal, "approach_progress", None), (skills._g(action, "before") or {}).get("location"),
                name=run.call.name):
            return "next_leg", "stone_unreachable_for_floor_visit"
        if run.call.name in skills.MINES_SKILLS and skills.wave1_stone_loop(
                run.state, action, getattr(goal, "approach_progress", None), (skills._g(action, "before") or {}).get("location"),
                name=run.call.name):
            self._skill_mines_loop_journal(run, action, (skills._g(action, "before") or {}).get("location"))
            return "next_leg", "stone_unreachable_for_floor_visit"
        if (run.call.name == "rawat_hewan" and action is not None
                and action.lifecycle == "completed" and action.cleanup_confirmed and action.request_id
                and action.world_effect_status in {"observed_extras_effect", "observed_item_pickup",
                                                   "observed_location_change"}
                and action.request_id != run.state.get("stall_progress_request")
                or run.call.name == "rawat_hewan" and skills.animal_selection_progress(
                    action, run.state.get("stall_progress_request"))
                or arrived
            or run.call.name == "tebang" and _moved_progress(action, run.state.get("stall_progress_request"))):
            if arrived:
                run.state.setdefault("stall_arrived_refs", []).append(arrived)
            # The allowance bounds consecutive idle recovery, not the number of
            # products in a house. A reread of the same receipt grants nothing.
            # A19: a fresh observed tool switch (Milk Pail for the next cow) is executor
            # progress too; the executor's own step/input/deadline caps still bound it.
            run.state["stall_progress_request"] = action.request_id
            run.state["stall_refreshes"] = 0
        ui_progress = getattr(self, "_live7_ui_progress_seq", 0)
        if ui_progress != run.state.get("stall_ui_progress", 0):
            # ADR 98 (a46 d14 J658-J725): each letter or TV page she handled herself is progress the action receipt
            # above never sees; the third letter of a visit ended stalled:no_continuation with the mail read.
            run.state["stall_ui_progress"] = ui_progress
            run.state["stall_refreshes"] = 0
        if (self._skill_rule_due() or self._pending_continuation is not None or self._pending_event_reason is not None
                or (goal.no_progress_until is not None and not goal.no_progress_blocked)
                or goal.question_until is not None):
            run.idle_since = None
            return None
        if run.idle_since is None:
            run.idle_since = now
            return None
        grace = self._skill_cfg("stardew_skill_stall_grace_ms", 1500, 0, 60000) / 1000
        if run.call.name == "lawan_mine":
            grace = min(grace, .5)
        if now - run.idle_since < grace:
            return None
        if goal.progress_marker in ("partial", "uncertain") and last is not None:
            return ("fail", f"finish_{goal.progress_marker}:{last.reason}")
        # A stale ticket can be discarded while the unchanged rate fence is
        # closed. Wait for its window; refreshing now cannot schedule anything.
        if self._continuation_rate_limited_only():
            run.idle_since = None
            return None
        if (run.state.get("stall_refreshes", 0) < _STALL_REFRESHES and not goal.no_progress_blocked
                and not goal.question_basis and goal.play_blocker is None):
            # A dropped continuation (scene churn between a ticket and its publication, a torn read) is not a
            # failed skill: take the runtime's own bounded read-only refresh (fresh inspect + one continuation).
            run.idle_since = None
            refreshed = self._resume_after_pause(reason="skill_stall_refresh")
            # ADR 148: a missing/stale read did not resume a decision. Charge
            # normal refresh attempts, with a separate finite wall wait
            # for unavailable reads (a62 consumed all three without a decision).
            read_unavailable = (not refreshed and (
                goal.last_inspect_status in ("stale", "mismatch", "inspect_unavailable", "unavailable")
                or str(getattr(self, "_cv_last_deny", "")).startswith("inspect_refresh:")))
            if not read_unavailable:
                run.state["stall_refreshes"] = run.state.get("stall_refreshes", 0) + 1
                run.state.pop("stall_read_since", None)
            else:
                since = run.state.setdefault("stall_read_since", now)
                if now - since >= 30:
                    return "fail", "stalled:fresh_read_unavailable"
            self._journal({"ev": "skill_stall_refresh", "session_id": session.session_id, "skill": run.call.name,
                           "n": run.state.get("stall_refreshes", 0), "scheduled": bool(refreshed),
                           "continuation_budget": self._continuation_budget_diagnostic()})
            if refreshed:
                return None
            if run.state.get("stall_refreshes", 0) < _STALL_REFRESHES:
                return None  # a torn refresh is bounded by consecutive attempts and the wall cap
        if (run.call.name == "lawan_mine" and not goal.question_basis
                and (goal.play_blocker is None or (
                    goal.play_blocker == "repeated_input_without_physical_change"
                    and run.state.get("wave1_driver") in ("ladder", "exit")))):
            # Fresh reads exhausted without progress: terminate through the existing
            # native up-ladder/Leave/alive verifier, never leave a stalled floor loop.
            if run.state.get("wave1_driver") == "exit":
                loc = skills._g(motor, "location")
                ctx = self._skill_wave1_context(self._build_decision_snapshot(goal))
                if str(loc or "").startswith("UndergroundMine"):
                    from arti_stardew_skills_mines import listed_downs, descend
                    downs = listed_downs(ctx, loc)
                    if downs:
                        descend(run.state, downs[0])
                        run.idle_since = None
                        run.state["stall_refreshes"] = 0
                        return "next_leg", "floor_exhausted_descend"
                run.idle_since = None
                run.state["stall_refreshes"] = 0
                run.state.setdefault("wave1_hold", "floor_exhausted")
                return None
            if (self._worker_stop.is_set() or self._action_cancel.is_set() or self._owner_voice_busy
                    or self._gather_mutation_unresolved or goal.gather_uncertain
                    or action is not None and (skills._g(action, "cleanup_confirmed") is not True
                        or skills._g(action, "lifecycle") not in ("completed", "rejected", "not_taken"))):
                return "fail", "floor_exhausted:alive_exit_unverified"
            excluded = self._skill_mines_stall_exclude(session, run, action)
            if excluded is not None:
                return excluded
            run.state["wave1_hold"] = "floor_exhausted"
            skills.wave1_repin(run.state, "skill lawan_mine", "exit", "exited_mines")
            run.idle_since = None
            return "next_leg", "floor_exhausted_alive_exit"
        reason = goal.play_blocker or ("no_progress" if goal.no_progress_blocked else
                                       "question" if goal.question_basis else "no_continuation")
        from arti_runtime.mines_exit import intent, remember, policy, exit_ran
        if intent(run, skills._g(motor, "location")) and exit_ran(run) and policy(self) is not None:
            remember(self, self._skill_wave1_context(self._build_decision_snapshot(goal)),
                     skills._g(motor, "location"), "stalled:" + str(reason))
        return ("fail", "stalled:" + str(reason))

    def _skill_mines_loop_journal(self, run, action, location):
        """FINAL B5: diagnostic row for a stone excluded by ``skills.wave1_stone_loop`` (floor visit)."""
        session = getattr(self, "_skill_session", None)
        self._journal({"ev": "mines_approach_loop_exclusion", "session_id": getattr(session, "session_id", None),
                       "skill": run.call.name, "ref": run.state["wave1_unreachable"][-1], "location": location,
                       "tile": (skills._g(action, "before") or {}).get("tile"), "reason": skills._g(action, "reason"),
                       "n": run.state.get("wave1_loop_exclusions", 0)})

    def _skill_mines_stall_exclude(self, session, run, action):
        """GAP8H J2 L1792-L1885: one unreachable pinned stone is not an exhausted floor.

        After the bounded stall refreshes on a rock leg whose last approach was a released not_taken
        (approach_partial / approach_replan_required), exclude only that pin for this floor visit and
        re-plan from fresh offers (another stone, an offered ladder, or the bounded survey). Bounded by
        MINES_STALL_EXCLUSIONS per floor visit; past that the existing floor_exhausted exit applies.
        """
        state = run.state
        ref = (state.get("wave1_pin") or {}).get("ref")
        location = (skills._g(action, "before") or {}).get("location") if action is not None else None
        if (state.get("wave1_driver") != "rock" or not str(location or "").startswith("UndergroundMine")
                or state.get("wave1_visit_location") != location
                or not str(ref).startswith("obj:" + str(location) + ":")
                or ref in state.setdefault("wave1_unreachable", [])
                or skills._g(action, "lifecycle") != "not_taken"
                or skills._g(action, "cleanup_confirmed") is not True
                or state.get("wave1_stall_exclusions", 0) >= skills.MINES_STALL_EXCLUSIONS):
            return None
        state["wave1_unreachable"].append(ref)
        state["wave1_stall_exclusions"] = state.get("wave1_stall_exclusions", 0) + 1
        state["stall_refreshes"] = 0
        state.update(wave1_driver=None, wave1_text="skill " + run.call.name, mines_refresh_due=True)
        run.idle_since = None
        self._journal({"ev": "mines_stall_exclusion", "session_id": session.session_id, "skill": run.call.name,
                       "ref": ref, "location": location, "reason": skills._g(action, "reason"),
                       "n": state["wave1_stall_exclusions"]})
        return "next_leg", "stone_unreachable_for_floor_visit"

    def _skill_chest_close_due(self, session, run):
        """Under the lock. D-B4-5: tutup_menu with ARTI's own chest open and no close offered to the step program (the
        v1 exit is offered only inside a store episode) -> the goal whose chest the runtime closes now (once per run)."""
        if (run is None or run.call.name != "tutup_menu" or run.status != "running" or run.state.get("chest_close_tried")
                or self._active is None or self._active.goal_id != run.goal_id):
            return None
        _, pid = self._binding_view()
        ctx = self._water_context(pid, container=True) if pid is not None else None
        view = self._container_view(ctx) if ctx is not None else None
        if view is None:
            return None
        run.state["chest_close_tried"] = True
        goal = self._active
        goal.store_container = (view.get("container_token"), view.get("source_fact_id"))
        return goal

    def _skill_arrived_native(self, run, outcome, state):
        """ADR 105: `plan_step` saw the native location equal the trip's target, and it still does at this terminal."""
        from arti_stardew_runtime import READY
        seen = run.state.get("arrived_native")
        return bool(seen and outcome[0] == "done" and self._status == READY
                    and state.get("location") == seen["location"] and state.get("input_released") is True
                    and state.get("food_clear") is not False)

    @W.span('settle_readback', 11, runtime=True)
    def skill_post_row_state(self):
        """Fresh native state, with unknowns retained for restore-vs-continue decisions."""
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        current = motor is not None and not self._state.is_stale(pid)
        state = dict(location=getattr(motor, "location", None) if current else None,
                     menu=getattr(motor, "active_menu", None) if current else "UNKNOWN",
                     free=getattr(motor, "is_player_free", None) if current else None,
                     input_released=False)
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            session = self._commands.baca_sesi(folder, retry_detik=0.25)
            raw = session.mentah
            from arti_stardew_command import input_release_state, lease_state, _baca_json, NAMA_RESULT
            action = skills._g(self._active, "last_action")
            request_id = skills._g(action, "request_id") if action is not None else getattr(self, "_last_input_request_id", None)
            state["request_id"] = request_id
            released = input_release_state(raw, request_id=request_id,
                result=_baca_json(folder / NAMA_RESULT) if request_id else None)
            identity = getattr(self._active, "play_identity", None)
            lease = lease_state(raw)
            matches = lease.matches(identity[1] if identity else session.session_id,
                identity[0] if identity else lease.binding[1], pid, session.screen_id)
            from arti_runtime.mines_body import body_guard_running
            guard = (body_guard_running(self) and raw.get("inflight_request_id") is None
                     and lease.owner is None)  # ADR 183: the input is held by her guard, not by a runtime request
            state["input_held"] = matches and released is False and not guard
            if guard:
                state["body_guard_active"] = True
            state["input_released"] = bool(matches and released is True
                and raw.get("inflight_request_id") is None
                and lease.owner is None and not self._action_busy
                and self._water_action_pin is None and self._action_pin is None)
            if not state["input_released"]:
                state["input_release_readback"] = dict(input_busy=raw.get("input_busy"),
                    owner_present=lease.owner is not None,
                    player_matches=session.player_id == pid, action_busy=self._action_busy,
                    action_pinned=self._water_action_pin is not None or self._action_pin is not None,
                    release_proven=released is True)
        except Exception as exc:  # unknown release is never reported as confirmed
            from arti_stardew_diagnostics import exception_record
            state["input_release_readback"] = exception_record(exc, phase="session_read")
        return state

    @W.span('settle_readback', 12, runtime=True)
    def skill_terminal_cleanup(self):
        """Bounded ordinary close path; STOP, identity, input caps and lease release stay authoritative."""
        from arti_stardew_runtime import READY
        live7 = getattr(self, "_live7_active", lambda: False)()
        if live7 and (self._live7_motor_facts() or {}).get("control_taken"):
            return self.skill_post_row_state()
        with self._lock:
            if (self._status != READY or self._worker_stop.is_set() or self._action_cancel.is_set()
                    or self._action_busy or self._inflight_generation is not None):
                return self.skill_post_row_state()
            run = self._skill_session.current if self._skill_session else None
            if run and run.call.name == "lawan_mine" and run.state.get("mines_unowned_menu"):
                return self.skill_post_row_state()  # recovery must not grant cleanup ownership
            self._action_busy = True
            goal = self._active
            generation = self._current_generation
            self._pending_continuation = None
            run = self._skill_session.current if self._skill_session else None
            last = getattr(goal, "last_action", None)
            finished = getattr(goal, "last_finish", None)
            settle = bool(run and (run.pending and run.pending[0] == "done"
                or finished and getattr(finished, "status", None) == "achieved"
                or last and getattr(last, "world_effect_status", None) not in (None, "unchanged", "observed_change")))
            identity = self._ui_key_context(self._binding_view()[1])
        try:
            deadline = game_now(self) + (6 if settle else 0)
            quiet_until = game_now(self) + (2 if settle else 0)
            presses = 0
            press_limit = 8 if run and run.call.name == "cek_pagi" else 4
            closed_container = False
            cleanup_owned_receiver = None  # scoped to this identity-fenced cleanup, never persisted across runs
            answered_tv, answered_until = None, 0.0
            for _ in range(124):  # at most six seconds; four ordinary or eight owned TV close calls
                wait_resume(self)
                with self._lock:
                    if (self._status != READY or self._active is not goal or self._current_generation != generation
                            or self._worker_stop.is_set() or self._action_cancel.is_set()):
                        break
                _, pid = self._binding_view()
                current = self._ui_key_context(pid)
                if identity is not None and (current is None or any(getattr(current, k) != getattr(identity, k)
                        for k in ("runtime_instance_id", "command_session_id", "observation_session_id", "player_id", "screen_id", "location"))):
                    break
                view = self._ui_pad_view(pid)
                # Universal terminal close follows only this run's verified receiver. Unknown UI is
                # never an ownership grant; B must describe a close, not a guessed question answer.
                from arti_stardew_menu_cleanup import close_button, owned_menu
                if (live7 and getattr(self._state.latest(pid), "active_menu", None) is not None
                        and not (view is not None and (view.receiver_token == cleanup_owned_receiver
                            or owned_menu(run, getattr(goal, "goal_id", None), getattr(goal, "last_action", None), view)))):
                    break  # foreign arrival in ANY cleanup phase belongs to the chooser
                own_button = close_button(view)
                if (own_button is not None and presses < press_limit
                        and (view.receiver_token == cleanup_owned_receiver
                            or owned_menu(run, getattr(goal, "goal_id", None), getattr(goal, "last_action", None), view))
                        and view.receiver_token not in self._ui_pad_uncertain
                        and view.receiver_token not in self._ui_key_uncertain):
                    result, _ = self._run_ui_pad("ui_press", view.press_args(own_button), origin="skill_terminal_cleanup")
                    presses += 1
                    if not result.cleanup or not result.verified or getattr(result, "uncertain", False):
                        break
                    cleanup_owned_receiver = getattr(result, "result_receiver_token", None)
                    self._journal({"ev": "skill_owned_menu_close", "menu_type": view.menu_type,
                                   "receiver_token": view.receiver_token, "effect": getattr(result, "effect", None)})
                    if getattr(result, "effect", None) == "closed":
                        break
                    continue
                if view is not None and view.context == "other":
                    break  # no verified receiver ownership: inspect/choose at the general UI boundary
                ctx = self._water_context(pid, container=True) if goal is not None else None
                container = self._container_view(ctx) if ctx is not None else None
                if container is not None:
                    if closed_container:
                        break
                    closed_container = True
                    goal.store_container = (container.get("container_token"), container.get("source_fact_id"))
                    self._store_close(goal, origin="skill_terminal_cleanup", failsafe=True)
                    if settle:
                        quiet_until = min(deadline, game_now(self) + 2)
                        W.call('poll', 27, self._action_cancel.wait, .05)
                        continue
                    break
                view = self._ui_pad_view(pid)
                key = self._ui_key_view(pid)
                if answered_tv is not None and (getattr(view, "receiver_token", None) == answered_tv
                                                or getattr(key, "receiver_token", None) == answered_tv
                                                or (view is None and key is None and getattr(
                                                    self._state.latest(pid), "active_menu", None) == "DialogueBox")):
                    # Part C2 S29 (journal L152-L156): the verified Leave answer keeps the same DialogueBox receiver
                    # through its native close animation (VERDICT effect=answered result_receiver=rcv:mi4); the next
                    # read still showed that question, so the loop stopped and the row held as unconfirmed while the
                    # box closed moments later (L160 menu=None). Wait read-only, bounded; never answer it twice.
                    if game_now(self) < answered_until:
                        W.call('poll', 28, self._action_cancel.wait, .05)
                        continue
                    break
                # S29 may leave its own exact native TV receiver, even after an early offer failure.
                from arti_stardew_skills_ui import tv_cleanup_args, blacksmith_cleanup_args
                tv_identity = tuple(getattr(current, k, None) for k in (
                    "runtime_instance_id", "command_session_id", "observation_session_id", "player_id", "screen_id", "location")) if current else None
                tv_leave = tv_cleanup_args(run, getattr(goal, "last_action", None), view, key, tv_identity)
                clint_leave = blacksmith_cleanup_args(run, getattr(goal, "last_action", None), view, key, tv_identity)
                if clint_leave is not None and presses < press_limit and view.receiver_token not in self._ui_pad_uncertain:
                    result = self._run_ui_key(clint_leave, origin="skill_terminal_cleanup")
                    presses += 1
                    if not result.cleanup or not result.verified:
                        break
                    continue
                if tv_leave is not None and presses < press_limit and view.receiver_token not in self._ui_pad_uncertain:
                    result = self._run_ui_key(tv_leave, origin="skill_terminal_cleanup")
                    presses += 1
                    if not result.cleanup or not result.verified:
                        break
                    if view.context == "dialogue":
                        run.state["morning_tv_receiver"] = getattr(result, "result_receiver_token", None)
                    else:  # the owned question's verified Leave: only its own close may follow
                        answered_tv, answered_until = view.receiver_token, game_now(self) + TV_LEAVE_CLOSE_S
                    continue
                # All other questions/choices remain untouched.
                if (view and view.context in ("question", "npc_question", "profession", "event")
                        or key and key.kind in ("question", "npc_question", "level_up_profession")):
                    break
                # Vanilla LetterViewerMenu.cleanupBeforeExit takes attachments on close (a full bag opens an
                # ItemGrabMenu): a mail letter is closed here only when its native facts prove none. A presented quest
                # is filed by the close, not held (her fact; Live7 2026-10-24 SAFETY_STOP).
                from arti_stardew_skills_ui import letter_close_hold
                letter_receiver = (view.receiver_token if view is not None and view.context == "letter"
                                   else key.receiver_token if view is None and key is not None and key.kind == "letter"
                                   else None)
                if letter_receiver is not None and letter_close_hold(key, letter_receiver):
                    break
                if presses >= press_limit and (view is not None or key is not None):
                    break
                if view is not None:
                    if getattr(view, "receiver_token", None) in self._ui_pad_uncertain:
                        break
                    if view.context == "shop":
                        view = self._economy_close_ready_view(pid, view)
                    # S29 must still prove ownership through tv_cleanup_args above.
                    plain_dialogue = (view and view.context == "dialogue" and (not run or (
                        run.call.name != "cek_pagi" and not run.state.get("morning_tv_receiver"))))
                    buttons = ("A", "B", "Start") if plain_dialogue else ("B", "Start")
                    button = next((b for b in buttons if view and view.press_allowed(b)), None)
                    if not view or not view.available or view.held_item is not None or button is None:
                        if settle and game_now(self) < deadline and view is not None and view.held_item is None:
                            W.call('poll', 30, self._action_cancel.wait, .05)
                            continue
                        break
                    result, _ = self._run_ui_pad("ui_press", view.press_args(button), origin="skill_terminal_cleanup")
                    presses += 1
                    if not result.cleanup or not result.verified:
                        break
                elif key is not None and key.available:
                    button = next((b for b in ("close", "cancel", "advance") if key.token_for(b)), None)
                    if button is None:
                        break
                    result = self._run_ui_key(key.args_for(button), origin="skill_terminal_cleanup")
                    presses += 1
                    if not result.cleanup or not result.verified:
                        break
                else:
                    if not settle or game_now(self) >= min(quiet_until, deadline):
                        break
                    W.call('poll', 29, self._action_cancel.wait, .05)
                    continue
                quiet_until = min(deadline, game_now(self) + 2)
        except Exception as exc:  # cleanup failure holds the row; never hides the original outcome
            from arti_stardew_diagnostics import exception_record
            self._journal(dict(ev="skill_terminal_cleanup_error", **exception_record(exc, phase="terminal_cleanup")))
        finally:
            with self._lock:
                self._action_busy = False
        # PROOF_A2: ARTI's own late item hold-up / "You found" box (journal 960-971) is settled here, bounded.
        self.skill_found_settle()
        state = self.skill_post_row_state()
        # A UI still on screen is not evidence that the selected object is unsafe. Its own
        # controller resolves it before any toolbar cleanup can be attempted.
        food_clear = self._skill_food_cleanup() if state.get("menu") is None else None
        state = self.skill_post_row_state()
        if food_clear is not None:
            state["food_clear"] = food_clear
        self._journal({"ev": "skill_terminal_cleanup", "post_row_state": state})
        return state

    def _skill_food_cleanup(self):
        """A7: one native select_slot after S5 (including skipped/failed food), inside all original caps.

        Campaign boundaries also release object slots left by another row. No tool use or new grant.
        A failed/unverified selection holds the boundary; it cannot erase an uncertain original action.
        """
        from arti_stardew_runtime import READY, PAUSED_RECOVERY
        with self._lock:
            session = self._skill_session
            run = session.current if session else None
            goal = self._active
            if not run or (run.call.name != "makan" and session.campaign is None):
                return None
            _, pid = self._binding_view()
            ctx = self._ui_key_context(pid)
            rec = self._obs.get(pid, "ui.context")
            if (ctx is None or rec is None or not rec.is_complete or not rec.authoritative
                    or getattr(rec, "stale", False) or getattr(rec, "requires_resync", False)
                    or rec.session_id != ctx.observation_session_id or rec.screen_id != ctx.screen_id
                    or not 0 <= time.monotonic()-rec.received_monotonic <= 2):
                return False
            # fix10e (a14 d9:J1231): proving the held slot is already clear is a read-only readback, so it runs before
            # the READY/busy gate. Under PAUSED_RECOVERY the gate made a held Hoe "unconfirmed" forever (11 game hours).
            toolbar = rec.facts.get("toolbar", {})
            targets = toolbar.get("targets", ())
            active = toolbar.get("active_slot")
            held = next((t for t in targets if t.get("slot") == active), None)
            if held is not None and str(held.get("qualified_item_id", "")).startswith(("(T)", "(W)")):
                return True
            # Empty slots have no published item; inventory must positively establish emptiness.
            inv = self._obs.get(pid, "progress.inventory")
            inv_fresh = (inv is not None and inv.is_complete and inv.authoritative
                    and not getattr(inv, "stale", False) and not getattr(inv, "requires_resync", False)
                    and 0 <= time.monotonic()-inv.received_monotonic <= 2
                    and inv.session_id == ctx.observation_session_id and inv.screen_id == ctx.screen_id)
            if (held is None and inv_fresh
                    and inv.facts.get("meta:inventory", {}).get("active_slot") == active
                    and f"slot:{active}" not in inv.facts):
                return True
            if held is None and inv_fresh and "toolbar" not in rec.facts and native_held_tool(inv.facts):
                # fix55A (ADR 77, a40 D9:J562-J569): GameLocation.startEvent sets displayHUD=false, so the mod
                # publishes no toolbar receiver in a festival/cutscene and the slot cannot be read or
                # changed there. The native bag still names the held slot (Farmer.CurrentToolIndex);
                # a Tool/weapon in it is the same safe hand the toolbar branch accepts above.
                return True
            if (self._status != READY or self._worker_stop.is_set() or self._action_cancel.is_set()
                    or self._action_busy or self._inflight_generation is not None
                    or goal is None or goal.goal_id != run.goal_id or self._gather_mutation_unresolved):
                return False  # the selection below publishes input; only READY may publish it
            tool = next((dict(t) for t in targets
                         if str(t.get("qualified_item_id", "")).startswith("(T)")), None)
            if tool is None or rec.facts.get("capabilities", {}).get("select_item", {}).get("available") is not True:
                return False
            _, cap, seconds = self._part_a_caps(run)
            if (self._physical_action_publications-run.inputs_start >= cap
                    or game_now(self)-run.started_at >= seconds):
                return False
            generation = self._current_generation
            date = self._date_tuple(self._state.latest(pid))
            self._action_busy = True
        try:
            def revalidate():
                current = self._obs.get(pid, "ui.context")
                if (self._status != READY or self._active is not goal or self._skill_session is not session
                        or session.current is not run or self._current_generation != generation
                        or self._date_tuple(self._state.latest(pid)) != date
                        or self._ui_key_context(pid) != ctx or current is None
                        or not current.authoritative or not current.is_complete
                        or getattr(current, "stale", False) or getattr(current, "requires_resync", False)
                        or current.session_id != ctx.observation_session_id or current.screen_id != ctx.screen_id
                        or not 0 <= time.monotonic()-current.received_monotonic <= 2
                        or tool not in current.facts.get("toolbar", {}).get("targets", ())
                        or self._physical_action_publications-run.inputs_start >= cap
                        or game_now(self)-run.started_at >= seconds):
                    raise ValueError("food_cleanup_context_changed")
            result = self._run_leased_once("select_item", tool, ctx, origin="skill-food-cleanup", revalidate=revalidate)
            ok = result.verified and result.cleanup and result.lifecycle == "completed"
            # A correlated pre-input refusal is a deferred cleanup, not an
            # unsafe selected object. Retry only cleanup after her UI choice.
            no_input = (getattr(result, "prearm_rejected", False)
                        or result.lifecycle == "not_taken" and not result.submitted)
            deferred = (not ok and no_input and result.cleanup and not result.hard_uncertain
                        and getattr(self._state.latest(pid), "active_menu", None) is not None)
            attempt = dict(phase="food", status="deferred_menu" if deferred else "verified" if ok else "failed",
                           verified=bool(ok), request_id=result.request_id, reason=result.reason,
                           cleanup=result.cleanup, prearm_rejected=getattr(result, "prearm_rejected", False))
            run.state.setdefault("terminal_cleanup_attempts", []).append(attempt)
            self._journal({"ev": "skill_food_cleanup", "skill": run.call.name, "verified": ok,
                           "request_id": result.request_id, "reason": result.reason, "cleanup": result.cleanup,
                           "status": attempt["status"], "prearm_rejected": attempt["prearm_rejected"]})
            if result.hard_uncertain:
                with self._lock:
                    self._status = PAUSED_RECOVERY
                    self._revoke_play()
            return None if deferred else bool(ok)
        finally:
            with self._lock:
                self._action_busy = False

    def _skill_found_step(self, run, request):
        """In-skill PROOF_A2 hook: the generic FIX4 recovery (skills.found_dialogue_step) or None."""
        snap, state = request.snapshot, run.state
        motor = skills._g(snap, "motor")
        if run.call.name in skills.FOUND_SKIP or not (
                state.get("found_dialogue") or state.get("found_holdup")
                or skills.found_holdup(motor) or skills.menu_up(snap)):
            return None
        now = (state.get("clock") or (lambda: game_now(self)))()
        context = self._skill_wave1_context(snap)
        return skills.found_dialogue_step(run.call.name, snap, state, context, now,
                                          getattr(self, "_last_publication_game_time",
                                                  getattr(self, "_last_publication_monotonic", None)))

    def skill_found_settle(self):
        """Boundary PROOF_A2 settle; None when nothing owned was pending, else the journaled summary.

        Owned = ARTI's own physical publication within FOUND_SOURCE_S, unchanged native identity, an active
        skill session. Read-only while ARTI's farmer is held (free, no menu, cannot move; <= FOUND_HOLDUP_S).
        Only a plain "You found" DialogueBox is pressed: the offered A after native typing, <= 3 per receiver,
        <= 8 per session, 15 s / 24 fresh reads per box; each press needs verified cleanup and a native
        ``closed`` effect. Any other menu/question/choice is left untouched and reported.
        """
        from arti_stardew_runtime import READY
        with self._lock:
            session = getattr(self, "_skill_session", None)
            source_at = getattr(self, "_last_publication_game_time", getattr(self, "_last_publication_monotonic", None))
            _, pid = self._binding_view()
            motor = self._state.latest(pid) if pid is not None else None
            if (session is None or session.status != "active" or self._status != READY
                    or self._worker_stop.is_set() or self._action_cancel.is_set()
                    or self._action_busy or self._inflight_generation is not None
                    or source_at is None or motor is None or self._state.is_stale(pid)
                    or not 0 <= game_now(self) - source_at <= skills.FOUND_SOURCE_S
                    or not (getattr(motor, "active_menu", None) == "DialogueBox" or skills.found_holdup(motor))):
                return None
            self._action_busy = True
            identity = self._ui_key_context(pid)
            goal, generation = self._active, self._current_generation
        started = game_now(self)
        deadline = started + skills.FOUND_HOLDUP_S + 2 * skills.FOUND_BOX_S
        boxes, outcome, revision = {}, "interrupted", None
        held = skills.found_holdup(motor)
        holdup_since, windows = None, 0
        try:
            while True:
                wait_resume(self)
                with self._lock:
                    if (self._status != READY or self._active is not goal or self._current_generation != generation
                            or self._worker_stop.is_set() or self._action_cancel.is_set()):
                        break
                current = self._ui_key_context(pid)
                if identity is not None and (current is None or any(getattr(current, k) != getattr(identity, k)
                        for k in ("runtime_instance_id", "command_session_id", "observation_session_id",
                                  "player_id", "screen_id", "location"))):
                    outcome = "identity_changed"
                    break
                now = game_now(self)
                if now > deadline:
                    outcome = "deadline"
                    break
                motor = self._state.latest(pid)
                if motor is None or self._state.is_stale(pid):
                    outcome = "motor_stale"
                    break
                menu = getattr(motor, "active_menu", None)
                if menu is None:
                    if skills.found_holdup(motor):
                        if holdup_since is None:
                            holdup_since, windows = now, windows + 1
                        if now - holdup_since <= skills.FOUND_HOLDUP_S and windows <= skills.FOUND_HOLDUP_WINDOWS:
                            held = True
                            W.call('poll', 31, self._action_cancel.wait, .05)
                            continue
                        outcome = "holdup_timeout"
                        break
                    pressed = [b for b in boxes.values() if b["presses"]]
                    if getattr(motor, "is_player_free", None) is True:
                        outcome = "settled" if all(b["closed"] for b in pressed) else "close_unverified"
                    else:
                        outcome = "not_free"
                    break
                if getattr(self, "_live7_active", lambda: False)():
                    from arti_stardew_menu_cleanup import owned_menu
                    run = session.current
                    if not owned_menu(run, getattr(goal, "goal_id", None), getattr(goal, "last_action", None),
                                      self._ui_pad_view(pid)):
                        outcome = "unowned_menu"
                        break
                holdup_since = None
                rec = self._obs.get(pid, "ui.context")
                fresh = (rec is not None and rec.authoritative and not rec.stale and not rec.requires_resync
                         and 0 <= time.monotonic() - rec.received_monotonic <= 2)
                receiver = skills.found_dialogue_receiver(rec.facts, menu) if fresh else None
                if receiver is None:
                    if fresh:
                        outcome = "unowned_menu"  # never adopted: left for the existing fences
                        break
                    W.call('poll', 25, self._action_cancel.wait, .05)
                    continue
                box = boxes.get(receiver)
                if box is None:
                    if not 0 <= now - source_at <= skills.FOUND_SOURCE_S:
                        outcome = "unowned_menu"
                        break
                    box = boxes[receiver] = dict(at=now, reads=0, presses=0, closed=False)
                if (rec.domain_revision, rec.received_monotonic) != revision:
                    revision = (rec.domain_revision, rec.received_monotonic)
                    box["reads"] += 1
                if now - box["at"] > skills.FOUND_BOX_S or box["reads"] > skills.FOUND_BOX_READS:
                    outcome = "wait_cap"
                    break
                view = self._ui_pad_view(pid)
                if (view is None or not view.available or view.receiver_token != receiver
                        or view.context != "dialogue" or view.held_item is not None
                        or receiver in self._ui_pad_uncertain or not view.press_allowed("A")):
                    W.call('poll', 26, self._action_cancel.wait, .05)  # typing: the native refusal is observed, never bypassed
                    continue
                total = getattr(self, "_found_session_presses", {}).get(session.session_id, 0)
                if box["presses"] >= skills.FOUND_BOX_PRESSES or total >= skills.FOUND_RUN_PRESSES:
                    outcome = "press_cap"
                    break
                box["presses"] += 1
                self._found_session_presses = {session.session_id: total + 1}
                result, _ = self._run_ui_pad("ui_press", view.press_args("A"), origin="skill_found_dialogue")
                if not result.cleanup or not result.verified:
                    outcome = "press_unverified"
                    break
                box["closed"] = getattr(result, "effect", None) == "closed"
        except Exception as exc:  # noqa: BLE001 - a settle failure only leaves the old (held) state
            outcome = "error:" + type(exc).__name__
        finally:
            with self._lock:
                self._action_busy = False
        summary = dict(outcome=outcome, held=held, waited_s=round(game_now(self) - started, 2),
                       boxes=[dict(receiver=r, **b) for r, b in boxes.items()])
        self._journal({"ev": "skill_found_settle", "session_id": session.session_id, **summary})
        return summary

    @W.span('validation', 13, runtime=True)
    def process_skill_boundary(self) -> int:
        """Worker hook: end the running skill when it finished / failed / stalled, then the next skill, ONE model
        choice, or the session end. Waits while anything is in flight; never races a decision or an action."""
        if getattr(self, "_pause_boundary", lambda: False)():
            return 0
        if not self._skills_enabled:
            return 0
        from arti_stardew_runtime import READY, OwnerTurnResult
        session = None
        next_leg = None
        terminal_run = None
        try:
            self._live7_poll_help()
            if self._live7_active() and self.live7_stop_file_check():
                return 1  # PLAN 2.3 RUN/LIVE7_STOP: a STOP at any boundary, even mid-skill
            if self._live7_active() and self._live7_watchdog(self._skill_session):
                return 1
            if self._live7_active():
                from arti_runtime.knockout import poll
                if self._skill_session.campaign is not None and poll(self, self._skill_session):
                    return 0  # ADR 191: fade/wake/dialog owns this boundary; never a combat/model turn
            if self._live7_active():
                handoff = self._live7_control_handoff(self._skill_session)
                if handoff in ("waiting", "stopped"):
                    return 1 if handoff == "stopped" else 0
                if handoff == "returned":
                    return self._live7_recover(self._skill_session, reason="control_returned:reobserve")
            if (self._live7_active() and self._status == READY and not self._action_busy
                    and self._inflight_generation is None and self._skill_session.current is None
                    and self._live7_unknown_ui_view(self._skill_session) is not None):
                return self._live7_recover(self._skill_session)
            if self._live7_active() and (self._status != READY or
                    self._skill_session.campaign is not None
                    and self._skill_session.campaign.live.wait_since is not None
                    and (self._skill_session.current is None
                         or self._skill_session.current.state.get("recovery_food_required")
                         or self._skill_session.current.state.get("live7_recovery_requested"))):
                session = self._skill_session
                ended = self._live7_unheld_wait_end(session)  # fix144A C7/C10 (ADR 249): a wait with no proof to come
                return ended if ended is not None else self._live7_recover(session)
            generic_session = self._skill_session
            generic_run = generic_session.current if generic_session else None
            if generic_run is not None and generic_run.state.get("terminal_ui_outcome") is not None:
                terminal = generic_run.state["terminal_ui_outcome"]
                state = self.skill_post_row_state()
                if (self._live7_terminal_ui_safe(generic_session, terminal, state)
                        and game_now(self) - generic_run.state["terminal_ui_since"] < 180):
                    handled = self._live7_generic_ui(generic_session)
                    if handled == "unsafe":
                        return self._live7_uncertain_ui(generic_session, "generic_ui_readback_unconfirmed")
                    return 0
                # Closed, changed identity/release, or timed out: re-run the
                # original terminal's cleanup. Never replay the action.
                generic_run.state.pop("terminal_ui_outcome")
                if state.get("menu") is not None and game_now(self) - generic_run.state["terminal_ui_since"] >= 180:
                    generic_run.pending = ("fail", "terminal_cleanup_unconfirmed:ui_choice_timeout:" + terminal[1])
            if (self._live7_active() and self._status == READY and not self._action_busy
                    and self._inflight_generation is None and generic_run is not None and generic_run.pending is None
                    # ADR 98 (a46 d11 J404-J407, d22 J410-J413): a proved complete store is the store rule's finish and
                    # close (worker hook order); a results-pending tick must not hand that chest back as a new choice.
                    and not self._goods_store_rule_due()):
                generic_view = self._live7_unknown_ui_view(generic_session)
                from arti_stardew_generic_ui import unexpected_festival_leave
                if (generic_view is not None and not self._live7_routine_owns_ready_check(generic_session, generic_view)
                        and (generic_view.generic or generic_run.state.get("mail_attachment_wait")
                             or unexpected_festival_leave(generic_view, generic_run))):
                    handled = self._live7_generic_ui(generic_session)
                    if handled == "unsafe":
                        return self._live7_uncertain_ui(generic_session, "generic_ui_readback_unconfirmed")
                    return 0
            with self._lock:
                session = self._skill_session
                if session is None or session.status != "active":
                    return 0
                if self._status != READY or self._worker_stop.is_set():
                    # Session watchdog: a runtime that stays not READY (PAUSED_RECOVERY / FAIL_CLOSED) ends it.
                    now = game_now(self)
                    session.not_ready_since = session.not_ready_since or now
                    bound = self._skill_cfg("stardew_skill_not_ready_ms", 30000, 0, 600000) / 1000
                    if now - session.not_ready_since >= bound:
                        self._skill_end_locked(session, "held", "partial", "runtime_not_ready:" + str(self._status))
                    return 0
                session.not_ready_since = None
                if (session.advancing or self._action_busy or self._inflight_generation is not None
                        or self._results_pending or self._trial_result_pending() or self._day_start_active):
                    return 0
                run = session.current
                chest_goal = self._skill_chest_close_due(session, run)
                if chest_goal is None:
                    if run is not None:
                        wait = run.state.get("campaign_mines_wait")
                        door = run.state.get("skill_door_wait")
                        # A parked Part B Mines leg / S28 door wait has not started its next leg; a refused wait
                        # closes the run as a named failure (never re-reads the consumed arrival terminal).
                        outcome = (("next_leg", "part_b_mines_wait") if wait is True else ("fail", wait)
                                   if wait is not None else ("next_leg", "skill_door_wait") if isinstance(door, dict)
                                   else ("fail", door) if door is not None else self._skill_run_outcome(session, run))
                        if outcome is None:
                            return 0
                        if outcome[0] == "next_leg":
                            next_leg = run
                        else:
                            terminal_run = run
                    elif session.mode == "free" and session.goal_id is None:
                        return 0  # the free session goal is not created yet (or its creation was refused)
                    # FIXB7 curfew guard: a skill ended by the curfew advances to the pinned pergi_ke / tidur plan.
                    # PART_B1 (journal 341-344): the Part B shop wait returns with no current run; the
                    # town row's clean, recorded failure (campaign_deferred) must not end the day here.
                    failed = (bool(session.runs) and session.runs[-1].status == "failed"
                              and session.runs[-1].reason != "curfew"
                              and not session.runs[-1].state.get("campaign_deferred"))
                    session.advancing += 1
            if chest_goal is not None:
                # D-B4-5: tutup_menu closes ARTI's open chest with the v1 ordinary container_exit (fail-safe pin = this
                # chest); the step program then observes the menu closed (observed_menu_closed) or fails as before.
                record = self._store_close(chest_goal, origin="skill_tutup_menu", failsafe=True)
                self._journal({"ev": "skill_chest_close", "session_id": session.session_id,
                               "status": (record or {}).get("status") or "not_offered"})
                return 1
            if terminal_run is not None:
                if self._live7_active() and self._live7_control_handoff(session) is not None:
                    session.advancing -= 1
                    return 0  # preserve the run until a fresh released boundary can re-observe
                before_cleanup = self._state.latest(self._binding_view()[1])
                had_menu = getattr(before_cleanup, "active_menu", None) is not None
                state = self.skill_post_row_state() if self._live7_active() else None
                # ADR 105: she arrived and an event holds her. The event owns its menus; there is nothing to clean up or wait for.
                arrived = state is not None and self._skill_arrived_native(terminal_run, outcome, state)
                if (state is not None and not arrived and self._live7_terminal_ui_safe(session, outcome, state)
                        and (outcome[1] == "observed_fish_caught_menu_pending_choice"
                             or game_now(self) - terminal_run.state.setdefault("terminal_ui_since", game_now(self)) < 180)):
                    terminal_run.state["terminal_ui_outcome"] = outcome
                    self._journal(dict(ev="skill_terminal_ui_pending", skill=terminal_run.call.name,
                        outcome=outcome, cleanup=state, input_released=True, replay=False))
                    session.advancing -= 1
                    return 0
                # An interrupted action never owns an unexpected menu. Pause before the old automatic
                # cleanup can click it; the brain sees the native UI at the next released boundary.
                from arti_stardew_generic_ui import unplanned_ui_reason
                interrupted_ui = (session.mode == "part_live" and had_menu and unplanned_ui_reason(
                    outcome[1], getattr(getattr(self._active, "last_action", None), "reason", "")))
                state = self.skill_post_row_state() if interrupted_ui or arrived else self.skill_terminal_cleanup()
                if self._skill_native_finish_wins(terminal_run):
                    # ADR 105 (a47 d5 J1416-J1417): the sleep verdict arrived during this cleanup, after the outcome was read.
                    outcome = terminal_run.pending
                # A menu can arrive during cleanup itself. Preserve the original
                # outcome here too, before any row can be emitted without cleanup.
                if (not arrived and self._live7_active() and self._live7_terminal_ui_safe(session, outcome, state)
                        and game_now(self) - terminal_run.state.setdefault("terminal_ui_since", game_now(self)) < 180):
                    terminal_run.state["terminal_ui_outcome"] = outcome
                    self._journal(dict(ev="skill_terminal_ui_pending", skill=terminal_run.call.name,
                        outcome=outcome, cleanup=state, input_released=True, replay=False))
                    session.advancing -= 1
                    return 0
                if self._live7_active() and self._live7_control_handoff(session) is not None:
                    session.advancing -= 1
                    return 0
                with self._lock:
                    if (session is not self._skill_session or session.status != "active"
                            or session.current is not terminal_run):
                        session.advancing -= 1
                        return 0
                    terminal_run.state["post_row_state"] = state
                    if state.get("body_guard_active") and not terminal_run.state.get("mines_death"):
                        # ADR 183 (a74 B d3 J2404-J2410): the input still held is her guard's; wait for its end.
                        from arti_runtime.mines_body import note_body_busy
                        note_body_busy(self, terminal_run.call.name, "terminal_cleanup")
                        if session.campaign is not None:
                            session.campaign.live.wait_since = game_now(self)
                        session.advancing -= 1
                        return 0
                    if terminal_run.state.get("mines_death"):
                        session.advancing -= 1
                        if session.mode != "part_live":
                            terminal_run.state.pop("mines_death", None)
                            self._skill_close_run_locked(session, terminal_run, ("fail", "knocked_out"))
                        return 0  # live owner observes native recovery on the next boundary
                    if session.mode == "part_live" and state.get("food_clear") is False:
                        # Keep the cleanup owner until selected-item safety is
                        # settled; closing the run would lose this mutation fence.
                        terminal_run.state["recovery_food_required"] = True
                        terminal_run.pending = ("fail", "terminal_cleanup_unconfirmed:" + outcome[1])
                        session.campaign.live.wait_since = game_now(self)
                        self._live7_recovery_fault(session, terminal_run.pending[1], observed=state)
                        session.advancing -= 1
                        return 0
                    if (terminal_run.call.name == "lawan_mine"
                            and terminal_run.state.get("wave1_hold")
                            and outcome == ("fail", terminal_run.state["wave1_hold"] + ":alive_exit_unverified")):
                        # Cleanup refreshes native state after the exit terminal.
                        # Do not freeze a pre-refresh label over a verified exit.
                        outcome = self._skill_run_outcome(session, terminal_run) or outcome
                    # A fresh released UI is a pause even if a skill did not predict its type.
                    # Preserve every unsafe action/identity/STOP and unresolved mutation fence.
                    from arti_stardew_live7 import safety_reason, CLEAN_LIFECYCLES
                    last_ui_action = getattr(self._active, "last_action", None)
                    ordinary_ui = (session.mode == "part_live" and state.get("menu") is not None
                        and state.get("input_released") is True and state.get("food_clear") is not False
                        and not self._gather_mutation_unresolved and not safety_reason(outcome[1])
                        and not safety_reason(skills._g(last_ui_action, "reason", ""))
                        and (last_ui_action is None or (skills._g(last_ui_action, "cleanup_confirmed") is True
                            and skills._g(last_ui_action, "lifecycle") in CLEAN_LIFECYCLES
                            and skills._g(last_ui_action, "world_effect_status") not in
                                ("uncertain", "possible_mutation", "unreconciled")))
                        and (self._live7_unknown_ui_view(session) is not None or self._live7_released_ui_wait(session)))
                    if arrived:
                        session.campaign.live.wait_since = game_now(self)  # the event settles before her next choice
                    elif ((interrupted_ui or ordinary_ui) and state.get("input_released") is True
                            and state.get("food_clear") is not False):
                        outcome = ("fail", "runtime_not_ready:unplanned_ui")
                    elif (self._status != READY or state["menu"] is not None or state["free"] is not True
                            or not state["input_released"] or state.get("food_clear") is False):
                        knockout = getattr(self, "_live7_knockout_cleanup", None)
                        if session.campaign is not None and knockout is not None and knockout(session, terminal_run, state):
                            outcome = ("fail", "knocked_out")  # M12: the clinic wake-up event, recovered by live7
                        else:
                            outcome = ("fail", "terminal_cleanup_unconfirmed:" + outcome[1])
                    leg = getattr(self._active, "mines_exit", None) or {}
                    if (terminal_run.call.name == "lawan_mine" and had_menu
                            and terminal_run.state.get("wave1_driver") == "exit"
                            and not terminal_run.state.get("mines_dialogue_exit_retried")
                            and self._active is not None and self._active.goal_id == terminal_run.goal_id
                            and str(state["location"]).startswith("UndergroundMine")
                            and self._status == READY and state["menu"] is None and state["free"] is True
                            and state["input_released"] and leg and leg["steps"] < self.MINES_EXIT_STEPS_MAX
                            and not outcome[1].startswith("terminal_cleanup_unconfirmed:")):
                        # Cleanup is recovery, not an alive exit. One fresh goal
                        # removes the old no-progress hold, retaining run caps and
                        # the native exit's already consumed step allowance.
                        terminal_run.state.update(mines_dialogue_exit_retried=True,
                                                  mines_exit_steps_used=leg["steps"])
                        terminal_run.pending = None
                        skills.wave1_repin(terminal_run.state, "skill lawan_mine", "exit", "exited_mines")
                        outcome = ("next_leg", "dialogue_closed_resume_native_exit")
                        next_leg, terminal_run = terminal_run, None
                    elif session.campaign is not None and self._part_a_leg(session, terminal_run, outcome, state):
                        next_leg = terminal_run
                        terminal_run = None
                    else:
                        if (session.campaign is not None and terminal_run.pending and terminal_run.pending[0] == "fail"
                                and not outcome[1].startswith("terminal_cleanup_unconfirmed:")):
                            outcome = terminal_run.pending
                        self._skill_close_run_locked(session, terminal_run, outcome)
                    if terminal_run is None:
                        failed = False
                    else:
                        failed = (terminal_run.status == "failed" and terminal_run.reason != "curfew"
                                  and not terminal_run.state.get("campaign_deferred"))
                    if terminal_run is not None and terminal_run.reason and terminal_run.reason.startswith("terminal_cleanup_unconfirmed:"):
                        if session.mode == "part_live":
                            session.campaign.live.wait_since = game_now(self)
                            self._live7_recovery_fault(session, terminal_run.reason, observed=state)
                        else:
                            self._skill_end_locked(session, "held", "partial", terminal_run.reason)
                            session.advancing -= 1
                            return 1
            self._skill_flush()  # the closed skill's result reaches the runner before the next skill's first step
            try:
                if next_leg is not None and next_leg.state.get("campaign_mines_wait") is True:
                    # One read-only lobby sample OUTSIDE the lock: STOP, receiver events and
                    # owner ingress are never blocked by the wait (shop-wait style re-poll).
                    from arti_stardew_part_b import MINES_WAITING, mine_wait
                    facts = self._skill_current_facts()
                    held = ("part_b_mines_wait:context_changed" if facts is None or session.campaign is None
                            else mine_wait(self, session.campaign.task, facts))
                    if held == MINES_WAITING:
                        return 1
                    with self._lock:
                        if session is not self._skill_session or session.current is not next_leg:
                            return 1
                        if held:
                            if session.campaign is not None:
                                session.campaign.mines_wait = None
                            next_leg.state["campaign_mines_wait"] = held  # next boundary closes the row
                            return 1
                        next_leg.state.pop("campaign_mines_wait")
                        # As before the fix, the idle wait is not charged to the dive's own leg/work caps.
                        base = (next_leg.decisions, self._physical_action_publications-next_leg.inputs_start,
                                game_now(self)-next_leg.started_at)
                        next_leg.state["campaign_leg_base"] = base
                        if "campaign_work_base" in next_leg.state:
                            next_leg.state["campaign_work_base"] = base
                if (next_leg is not None and "campaign_mines_wait" not in next_leg.state
                        and self._skill_door_wait(session, next_leg, outcome)):
                    return 1  # parked (or refused: the next boundary closes the run); no start, no input
                if next_leg is not None:
                    self._journal({"ev": "skill_leg", "session_id": session.session_id,
                                   "skill": next_leg.call.name, "reason": outcome[1]})
                    self._skill_start(session, next_leg.call, by=next_leg.by, resume=next_leg)
                elif session.mode == "part_live":
                    from arti_stardew_live7 import SAFETY_STOP
                    if terminal_run is not None and terminal_run.state.get("live7_outcome") == SAFETY_STOP:
                        self._skill_end_locked(session, "held", SAFETY_STOP, terminal_run.reason or "live7:safety_stop")
                    elif terminal_run is not None and self._skill_goods_continue(session, terminal_run):
                        pass  # bounded continuation of the same choice, without another model call
                    else:
                        self._part_a_advance(session)
                elif session.mode in ("part_a", "part_b", "part_c"):
                    if failed:
                        reason = session.runs[-1].reason
                        if session.mode == "part_a" and self._part_a_inputs_exhausted():
                            # A24: input 900/900 then a refused publication is the PLAN cost cap, never
                            # an unreconciled action. Name the exhausted limit honestly.
                            reason = "part_a_budget:inputs"
                        self._skill_end_locked(session, "held", "partial", reason if reason in (
                            "part_a_preface_sleep_failed", "part_a_budget:inputs") else "part_a_skill_failed:" + reason)
                    else:
                        self._part_a_advance(session)
                elif session.mode == "free":
                    self._skill_model_boundary(session, trigger="start" if not session.runs else
                                               "skill_failed" if failed else "skill_finished")
                elif failed:
                    self._skill_after_failure(session)
                else:
                    self._skill_advance(session)
            finally:
                with self._lock:
                    session.advancing -= 1
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            # Proof B2: the type alone left no triage trail; name the raising frame too (no locals).
            frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
            from arti_stardew_diagnostics import exception_record
            self._journal(dict(ev="skill_boundary_error", **exception_record(exc, phase="skill_boundary"),
                at=f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" if frame else None))
            if session is not None:
                with self._lock:
                    if session.status == "active":
                        self._skill_end_locked(session, "failed", "uncertain", "skill_boundary_error")
            return 0
        finally:
            self._skill_flush()

    def _skill_goods_continue(self, session, run):
        from arti_stardew_live7 import VERIFIED, goods_facts, home_due
        from arti_stardew_skills_goods import stack_current, MAX_STACKS
        remaining = run.state.pop("goods_remaining", [])
        if not remaining:
            return False
        completed = run.state.get("goods_completed", 0) + 1
        reason = None
        if (session is not self._skill_session or session.status != "active"
                or self._worker_stop.is_set() or self._owner_play_stopped or self.live7_stop_file_check()):
            return True
        if run.status != "finished" or run.state.get("live7_outcome") != VERIFIED:
            reason = "stack_effect_unverified"
        elif completed >= MAX_STACKS:
            reason = "stack_limit"
        else:
            facts, motor, context, held = self._live7_read(session)
            basis = (tuple((context or {}).get("identity") or ()), self._date_tuple(motor), motor.location) if motor else None
            if held or basis != run.state.get("goods_basis"):
                reason = "context_changed"
            elif facts.menu_open or not facts.player_free or home_due(facts, session.campaign.live):
                reason = "menu_or_time_changed"
            elif not stack_current(remaining[0], goods_facts(context), implicit=run.state.get("goods_implicit", False),
                                   gifts=session.campaign.live.gift_from):
                reason = "stack_changed"
            else:
                call = remaining[0]
                failures = skills.precondition_failures(call, facts)
                if failures:
                    reason = "precondition:" + ",".join(failures)
                else:
                    self._skill_start(session, call, by=run.by, facts=facts)
                    following = session.current
                    if following is not None and following is not run and following.call == call:
                        following.state.update(goods_remaining=remaining[1:], goods_completed=completed,
                                               goods_implicit=run.state.get("goods_implicit", False),
                                               goods_basis=basis,
                                               live7_baseline=self._live7_baseline(facts, motor, context))
                    self._journal(dict(ev="live7_goods_batch_step", completed=completed, remaining=len(remaining)))
                    return True
        self._journal(dict(ev="live7_goods_batch_stopped", reason=reason, completed=completed - (reason == "stack_effect_unverified")))
        return False

    def _skill_goods_menu_guard(self, run, motor):
        """Opening the chosen container is expected; replacing it mid-effect is not."""
        if "goods_remaining" not in run.state or motor is None or run.pending is not None:
            return  # a verdict already named (e.g. store_closed_by_choice) is never relabelled
        goal = self._active
        if goal is not None and getattr(goal, "progress_marker", None) == "terminal":
            return  # normal verified finish/cleanup may close the menu
        if self._goods_effect_verified(goal):
            # ADR 98 (a46 d2 J1654-J1670, d22 J409-J431): the native ledger already proved this stack's effect. Her close
            # or the plan's own close after it is not a menu replaced mid-effect; the next stack re-reads the menu anyway.
            return
        menu = getattr(motor, "active_menu", None)
        previous = run.state.get("goods_menu")
        if previous is not None and menu != previous:
            run.state["goods_remaining"] = []
            run.pending = ("fail", "goods_menu_changed")
        elif menu is not None:
            run.state["goods_menu"] = menu
            _, pid = self._binding_view()
            # Generic UI owns the presented surface. A key-only classifier can describe
            # a different receiver; it must never override this menu's native pad pin.
            view = self._ui_pad_view(pid) or self._ui_key_view(pid)
            token = (getattr(view, "receiver_token", None)
                     if getattr(view, "menu_type", menu) == menu else None)
            if token is not None and run.state.setdefault("goods_receiver", token) != token:
                run.state["goods_remaining"] = []
                run.pending = ("fail", "goods_menu_changed")

    def _goods_store_rule_due(self):
        due = getattr(self, "_store_rule_pending", None)
        return bool(due is not None and due())

    def _goods_effect_verified(self, goal):
        """The native receipts of this goal already meet its goods objective (sale / shipment ledger, paired chest store)."""
        from arti_stardew_economy import objective_verdict
        objective = getattr(goal, "economy_objective", None)
        if objective is not None and objective_verdict(objective)[0] == "achieved":
            return True
        verdict = getattr(self, "_store_rule_verdict", None)
        return verdict is not None and verdict(goal) is not None

    def _skill_door_read(self, run, destination):
        """ONE read-only native sample for the S28 door wait: the bound motor plus the newest scene record's door
        facts of THIS location (same observation session/screen). Brief lock only; no input; None when unbound."""
        import arti_stardew_skills_quest as quest
        with self._lock:
            goal = self._active
            binding, pid = self._binding_view()
            if (goal is None or goal.goal_id != run.goal_id or goal.progress_marker != "terminal"
                    or binding.status != "bound" or pid is None or self._state.is_stale(pid)):
                return None
            motor = self._state.latest(pid)
            pin = self._water_context(pid)
            rec = self._obs.get(pid, "scene.current_location")
        if motor is None or pin is None or pin.location != motor.location:
            return None
        targets = {}
        if (rec is not None and rec.authoritative and rec.available and not rec.stale and not rec.requires_resync
                and rec.session_id == pin.observation_session_id and rec.screen_id == pin.screen_id):
            prefix = "door:%s:" % motor.location  # SceneIdentity.DoorFactId: this location's doors only
            targets = {ref: f for ref, f in rec.facts.items() if str(ref).startswith(prefix)}
        return dict(location=motor.location, date=self._date_tuple(motor), time=motor.time_of_day,
                    free=motor.is_player_free, menu=motor.active_menu,
                    doors=sum(isinstance(f, Mapping) and f.get("kind") == "door"
                              and f.get("target_location") == destination for f in targets.values()),
                    locked=quest.locked_doors(targets, destination))

    def _skill_door_refusal(self, run, destination, why, read):
        from arti_stardew_skills_attempts import end, note
        note(run.state, "door_wait_refused", destination=destination, why=why, time=(read or {}).get("time"))
        return end(run.state, {"publications": self._physical_action_publications},
                   destination.casefold() + "_door_locked").reason

    @W.span('settle_readback', 14, runtime=True)
    def _skill_door_wait(self, session, run, outcome) -> bool:
        """FINAL Part C S28 (J L624-L642): a travel arrival in front of the delivery destination's natively locked
        doors parks the run (no start, no brain call, no input) and each worker boundary takes ONE read-only sample
        outside the runtime lock (Part B shop/Mines wait pattern), so STOP and receiver events are never delayed.
        True = parked or refused (the next boundary closes the run); False = start the leg now."""
        import arti_stardew_skills_quest as quest
        wait = run.state.get("skill_door_wait")
        if not isinstance(wait, dict):
            destination = quest.door_wait_destination(run.call, run.state)
            if outcome is None or outcome[1] != "verified:attempt_travel" or destination is None:
                return False
            read = self._skill_door_read(run, destination)
            if (read is None or not read["locked"] or type(read["time"]) is not int
                    or read["time"] >= quest.DOOR_WAIT_OPEN or read["free"] is not True or read["menu"] is not None):
                return False  # open / unknown / past the opening: the plan step's own fresh read decides
            # Never renewed: the wait spends the run's own seconds cap (and the approved trial seconds).
            left = self._part_a_caps(run)[2] - (game_now(self) - run.started_at)
            deadline_ms = getattr(self, "_trial_deadline_unix_ms", None)
            if type(deadline_ms) in (int, float):
                left = min(left, deadline_ms / 1000 - time.time())
            max_seconds = min(quest.DOOR_WAIT_SECONDS, left - quest.DOOR_WAIT_MARGIN_S)
            with self._lock:
                if session is not self._skill_session or session.current is not run:
                    return True
                if max_seconds <= 0:
                    run.state["skill_door_wait"] = self._skill_door_refusal(run, destination, "time_budget", read)
                else:
                    run.state["skill_door_wait"] = dict(destination=destination, location=read["location"],
                        date=read["date"], time=read["time"], started=game_now(self), max_seconds=max_seconds)
                self._journal({"ev": "skill_door_wait", "session_id": session.session_id, "skill": run.call.name,
                               "status": "waiting" if max_seconds > 0 else "refused", "destination": destination,
                               "location": read["location"], "time": read["time"], "doors": list(read["locked"]),
                               "max_seconds": round(max(0.0, max_seconds), 1)})
            return True
        if self._action_cancel.is_set() or self._worker_stop.is_set():
            return True  # STOP owns the session; a parked wait never starts a leg
        read = self._skill_door_read(run, wait["destination"])
        held = quest.door_wait_sample(wait, read, game_now(self) - wait["started"])
        if held == quest.DOOR_WAITING:
            return True
        with self._lock:
            if session is not self._skill_session or session.current is not run:
                return True
            if self._action_cancel.is_set() or self._worker_stop.is_set():
                # A STOP that landed during the lock-free sample (lab flake of the STOP test at 2221fa0a, 2/8):
                # the sample may already show the door open, but STOP owns the session; never start the leg.
                return True
            self._journal({"ev": "skill_door_wait", "session_id": session.session_id, "skill": run.call.name,
                           "status": "done" if held is None else "refused", "reason": held,
                           "destination": wait["destination"], "time": (read or {}).get("time"),
                           "waited_s": round(game_now(self) - wait["started"], 2)})
            if held is None:
                run.state.pop("skill_door_wait")
                return False
            run.state["skill_door_wait"] = self._skill_door_refusal(run, wait["destination"], held, read)
            return True

    def _skill_failure_observation(self, snapshot=None):
        """Reuse the current snapshot/context; unavailable evidence remains unknown in the result facts."""
        try:
            pid = self._binding_view()[1]
            if pid is None or self._state.is_stale(pid):
                return None
            snapshot = snapshot or self._build_decision_snapshot(self._active)
            return skills.failure_observation(self._skill_facts(snapshot), snapshot.motor,
                                              self._skill_wave1_context(snapshot))
        except (AttributeError, TypeError, ValueError):
            return None

    @W.span('validation', 15, runtime=True, lifecycle='terminal')
    def _skill_close_run_locked(self, session, run, outcome) -> None:
        from arti_stardew_runtime import OwnerTurnResult, StatusSpeech
        if run.state.get("mines_death"):
            outcome = ("fail", run.state["mines_death"])
        kind, reason = outcome
        if run.call.name == "lawan_mine":
            from arti_runtime.mines_fight import close as fight_close
            kind, reason = fight_close(run, kind, reason)
        from arti_runtime.route_pause import pause_reason
        paused = pause_reason(reason) or run.state.get("live7_outcome") == "INTERRUPTED"
        run.ended_at = game_now(self)
        run.stage_inputs = getattr(self, "_stage_publications", 0) - (run.stage_start or 0) if run.stage_start is not None else 0
        run.inputs = self._physical_action_publications - run.inputs_start - (
            0 if run.call.name in ("rawat_hewan", "ikuti_festival") or skills.REGISTRY[run.call.name].family == skills.F_WAVE1
            or "campaign_task" in run.state else run.stage_inputs)
        session.current = None
        if not paused and (kind != "done" or run.state.get("outcome") == "unknown" or unreadable_outcome(reason)):
            from arti_runtime.decision_plan import drop
            drop(self, session, "step_not_finished:" + str(reason))
        goal = self._active
        if goal is not None and goal.goal_id == run.goal_id:
            self._pending_continuation = None  # the skill's goal ends with it (no orphan continuation)
            self._skill_blocked_refs_refresh(session)
            # D-B7-4: a work target blocked in this goal is carried to the next skill goal. fix10b #4 (a11 d2:J1756):
            # a native exit / route stand retired by this attempt's cap is not; a fresh trip may use it again.
            # ADR 195: a people-page pause is not blocked-call memory.
            if not paused:
                from arti_stardew_live7 import navigation_exit
                session.blocked_refs |= {r for r in goal.approach_blocked_refs or () if not navigation_exit(r)}
                session.blocked_runs += 1
        if kind == "not_applicable":
            run.status, run.reason = "skipped", reason
            self._journal({"ev": "skill_skipped", "session_id": session.session_id, "skill": run.call.name,
                           "arg": run.call.arg, "reason": reason, "decisions": run.decisions, "inputs": run.inputs})
        elif run.state.get("outcome") == "unknown" or unreadable_outcome(reason):
            # ADR 157: the closed run is unknown, so no second failure reaches her page/memory.
            run.status, run.reason = "unknown", reason
            self._journal(dict(ev="skill_unknown", session_id=session.session_id, skill=run.call.name,
                arg=run.call.arg, reason=reason, outcome="unknown", outcome_class="UNCERTAIN",
                outcome_id=run.state.get("outcome_id"),
                decisions=run.decisions, inputs=run.inputs))
        elif kind == "done":
            run.status, run.verdict = "finished", reason
            origin = ("skill_rule" if reason in ("observed_equipped_item", "observed_menu_closed",
                                                 "observed_approach_arrived") else "objective_rule")
            row = {"ev": "skill_finished", "session_id": session.session_id, "skill": run.call.name,
                   "arg": run.call.arg, "verdict": reason, "origin": origin, "decisions": run.decisions,
                   "inputs": run.inputs, "model_calls_inside": run.foreign_model_calls,
                   "started_unix": run.wall_started, "ended_unix": time.time()}  # W1b B5: wall window of her inputs
            if run.state.get("mines_progress"):
                row["mines_progress"] = run.state["mines_progress"]
            if letters := run.state.get("recovered_mail_letters"):
                row["letters_read"] = letters
            self._journal(row)
        elif reason == "knocked_out":
            run.status, run.reason = "interrupted", reason
            self._journal(dict(ev="skill_interrupted", skill=run.call.name, reason=reason))
        elif kind == "fail":
            from arti_runtime.route_pause import pause_reason
            if run.state.get("live7_outcome") == "INTERRUPTED" or pause_reason(reason):
                # ADR 195: people-page pause is interrupted, not skill_failed (not a recent_failure).
                run.status, run.reason = "interrupted", reason
                self._journal(dict(ev="skill_interrupted", session_id=session.session_id, skill=run.call.name,
                                   arg=run.call.arg, reason=reason, outcome_class="INTERRUPTED", verified=False,
                                   decisions=run.decisions, inputs=run.inputs))
            elif run.state.get("live7_outcome") == "PARTIAL":
                # ADR 116: preserve the native partial in the final skill event too;
                # a second bare skill_failed would reach speech as a fresh denial.
                run.status, run.reason = "partial", reason
                self._journal(dict(ev="skill_partial", session_id=session.session_id, skill=run.call.name,
                    arg=run.call.arg, reason=reason, outcome_class="PARTIAL", verified=False,
                    evidence=dict(observed=run.state.get("result_observation") or goal.last_finish.observed)))
            else:
                run.status, run.reason = "failed", reason
                run.state["result_observation"] = self._skill_failure_observation()
                row = dict(ev="skill_failed", session_id=session.session_id, skill=run.call.name,
                           arg=run.call.arg, reason=reason, decisions=run.decisions, inputs=run.inputs)
                if observed_reason := run.state.get("recovery_observed_reason"):
                    row["observed_reason"] = observed_reason
                self._journal(row)
        else:
            run.status, run.reason = "failed", reason
            run.state["result_observation"] = self._skill_failure_observation()
            self._journal({"ev": "skill_failed", "session_id": session.session_id, "skill": run.call.name,
                           "arg": run.call.arg, "reason": reason, "decisions": run.decisions, "inputs": run.inputs})
        self._skill_emit("skill_" + run.status, OwnerTurnResult(
            admitted=True, applied=True, skip_reply_tags=True, reason="skill_" + run.status,
            speech=StatusSpeech(f"Skill {run.call.name}: {run.verdict or run.reason}")))

    def _skill_after_failure(self, session: SkillSession, *, initial: bool = False):
        """A required planned skill failed: ONE model boundary may insert a remedy skill (then the failed skill is
        retried once) or finish; an optional composite step just moves on."""
        from arti_stardew_runtime import OwnerTurnResult
        with self._lock:
            last = session.runs[-1] if session.runs else None
            optional = last is not None and last.call.optional
            if last is not None and last.call.name == "cangkul" and last.reason == "no_tillable_tile":
                return self._skill_end_locked(session, "held", "partial", "no_tillable_tile")
            if (last is not None and not optional and last.goal_id is not None
                    and last.call.name in ("rawat_hewan", "makan")):
                # Neither a model retry nor ask_owner can reconcile a physical hold.
                return self._skill_end_locked(session, "held", "partial", "skill_failed:" + last.reason)
            if last is not None and not optional and last.call.name in ("beli", "jual"):
                facts = self._skill_current_facts()
                if facts is not None and facts.location == "SeedShop":
                    # Already at Pierre: a failed counter/purchase is not a
                    # reason to insert a model trip back out to Town.
                    return self._skill_end_locked(session, "held", "partial", "skill_failed:" + last.reason)
            if last is not None and skills.REGISTRY[last.call.name].family == skills.F_WAVE1:
                return self._skill_end_locked(session, "held", "partial", last.reason or "composition_incomplete")
            if (last is not None and last.call.name == "siram" and last.reason == "needs_refill"
                    and (last.call.name, last.call.arg) not in session.retried and session.status == "active"):
                session.retried.add((last.call.name, last.call.arg))
                session.plan[session.index:session.index] = [skills.SkillCall("isi_gembor"),
                                                              replace(last.call, optional=False)]
                self._journal({"ev": "skill_refill_composed", "session_id": session.session_id,
                               "failed": "siram", "refill": "isi_gembor", "retry": True})
                return self._skill_advance(session, initial=initial)
            if not optional and last is not None and last.reason in _TERMINAL_FAILURES and session.status == "active":
                # D-B7-4 (live V2B7SKL5b: tebang -> no tree -> model tebang again -> same tree -> ask_owner): nothing left
                # to act on is a terminal failure; no model remedy, no ask_owner.
                return self._skill_end_locked(session, "held", "partial", "skill_failed:" + last.reason)
        if optional:
            return self._skill_advance(session)
        choice = self._skill_rule_remedy(session, last)
        if choice is None:
            choice = self._skill_model_boundary(session, trigger="skill_failed", start=False)
        if isinstance(choice, skills.SkillCall):
            with self._lock:
                insert = [choice]
                key = (last.call.name, last.call.arg)
                if key not in session.retried and choice.name != last.call.name:
                    session.retried.add(key)
                    insert.append(replace(last.call, optional=False))
                session.plan[session.index:session.index] = insert
            return self._skill_advance(session, initial=initial)
        with self._lock:
            if session.status == "active" and choice == "ask_owner":
                return self._skill_end_ask_owner_locked(session)
            if session.status == "active":
                return self._skill_end_locked(session, "held", "partial",
                                              "skill_failed:" + (last.reason if last else "unknown"))
        return OwnerTurnResult(admitted=True, applied=True, skip_reply_tags=True, reason="skill_session_ended")

    def _skill_rule_remedy(self, session, last):
        """FIXB13 D-B10-2: the deterministic remedy for a failed precondition (skills.rule_remedy), once per failed skill;
        None = the model decides as before (ambiguous / no producer / producer not runnable / already retried)."""
        reason = str(getattr(last, "reason", "") or "")
        if last is None or not reason.startswith("precondition:") or (last.call.name, last.call.arg) in session.retried:
            return None
        facts = self._skill_current_facts()
        if facts is None:
            return None
        remedy = skills.rule_remedy(last.call, reason.split(":", 1)[1].split(","), facts)
        if remedy is None:
            return None
        self._journal({"ev": "remedy_rule", "session_id": session.session_id, "failed": last.call.name,
                       "precondition": reason.split(":", 1)[1], "remedy": remedy.name, "arg": remedy.arg})
        return remedy

    def _skill_end_ask_owner_locked(self, session: SkillSession):
        """The model asked the owner: its question goes out once as speech (conversation path), then the session holds."""
        from arti_stardew_runtime import OwnerTurnResult
        question = (session.question or "").strip()
        self._journal({"ev": "skill_ask_owner", "session_id": session.session_id, "question": question[:200]})
        if question:
            self._skill_emit("skill_ask_owner", OwnerTurnResult(admitted=True, applied=True, skip_reply_tags=True,
                                                                reason="skill_ask_owner", speech=question))
        return self._skill_end_locked(session, "held", "partial", "skill_choice:ask_owner")

    # ------------------------------------------------------------------ the model at a boundary
    def _skill_model_boundary(self, session: SkillSession, *, trigger: str, start: bool = True):
        """ONE model choice among the admissible skills (bounded retries for a malformed answer). Free mode starts
        the chosen skill / ends the session; plan mode returns the choice to the failure handler."""
        choice = self._skill_model_choose(session, trigger)
        if not start:
            return choice
        if isinstance(choice, skills.SkillCall):
            facts = self._skill_current_facts()
            failures = ["facts_unavailable"] if facts is None else skills.precondition_failures(choice, facts)
            if failures:
                # Preconditions are re-run at start: a stale / wrong choice fails unstarted (the next boundary asks).
                self._skill_fail_unstarted(session, choice, "precondition:" + ",".join(failures))
                return None
            return self._skill_start(session, choice, by="model", facts=facts)
        with self._lock:
            if session.status != "active":
                return None
            if choice == "ask_owner":
                return self._skill_end_ask_owner_locked(session)
            if choice == "finish":
                achieved = any(r.status == "finished" for r in session.runs)
                # No owner criterion to verify in free mode: the model's finish is recorded, never OBJECTIVE_VERIFIED.
                return self._skill_end_locked(session, "finished", "partial",
                                              "skill_session_model_finish" if achieved else "model_finish_no_skill")
            return self._skill_end_locked(session, "held", "partial", "skill_choice:" + str(choice))

    def _skill_model_choose(self, session: SkillSession, trigger: str):
        """PM-1 fix12a (a17 d7:J632-638): a halved render budget is one boundary's retry, never the session's. a17
        kept 4032 B for the rest of the day, smaller than any core render (system + schema alone ~3.8 KB)."""
        from arti_runtime.decision_plan import take
        queued = take(self, session, trigger)
        if queued is not None:
            return queued
        budget = session.boundary_budget
        try:
            return self._skill_model_choose_once(session, trigger)
        finally:
            session.boundary_budget, session.boundary_reads = budget, 0

    def _skill_render_fault(self, session, trigger, request, messages=None, exc=None):
        """PM-1 fix12a invariant: a render that left an optional block out, or could not render at all, is a named
        FAULT. Neither ends the decision loop: the next poll re-offers the boundary."""
        if (exc is not None or messages and skills.render_omitted(messages)) and getattr(self, "_journal_dir", None):
            from scripts.live7.ui_render_archive import archive_ui_input
            slot = self.__dict__.get("_ui_render_archive_slot", 0)
            self._ui_render_archive_slot = slot + 1
            archive_ui_input(self._journal_dir, request, slot, "render_failed" if exc else "render_bounded")
        if exc is not None:
            self._journal(dict(ev="FAULT", session_id=session.session_id, type="render_failed", trigger=trigger,
                               code=str(exc)[:60], generation=request.generation_id, budget=session.boundary_budget))
            return
        omitted = skills.render_omitted(messages) if messages else None
        if messages and request.skill_offer.get("action_facts"):
            rendered = json.loads(messages[1]["content"])
            if rendered.get("action_facts") != request.skill_offer["action_facts"]:
                self._journal(dict(ev="FAULT", session_id=session.session_id, type="render_action_fact_missing",
                                   trigger=trigger, generation=request.generation_id))
        if omitted:
            self._journal(dict(ev="FAULT", session_id=session.session_id, type="render_block_omitted",
                               blocks=omitted, trigger=trigger, generation=request.generation_id))

    @W.span('validation', 16, runtime=True)
    def _skill_model_choose_once(self, session: SkillSession, trigger: str):
        """SkillCall | "finish" | "ask_owner" | "budget" | "none" | "invalid" | "superseded" | "provider_error".

        ONE logical model decision; a malformed / invalid answer is retried (stardew_skill_boundary_attempts), and
        a request superseded before its answer (another runtime refresh raced it) is rebuilt at most twice."""
        from arti_stardew_runtime import ProductionDecisionProvider, _Superseded
        from arti_stardew_decision import DecisionCodecError, decode_game_decision
        self._skill_last_provider_error = None  # fix57: (type, code) of this boundary's provider_error, for live7
        with self._lock:
            session.model_pick = None  # Live7 WP4: a pick links only to this boundary's own request
            session.festival_leave_pick = None
            own = (self._active is not None
                   and self._active.goal_id in {r.goal_id for r in session.runs} | {session.goal_id})
        if not own and session is self._skill_session and session.status == "active":
            if not self._skill_session_goal(session).admitted:
                return "none"
        attempts = self._skill_cfg("stardew_skill_boundary_attempts", 2, 1, 3)
        campaign = session.mode in ("part_a", "part_b", "part_c") and session.campaign_offer is not None
        if campaign:
            # A20: a malformed Part A answer already runs the scheduled task, so a retry only spends the
            # <=24 brain-call cap (a20: four invalid boundaries cost eight calls). One call per boundary.
            # PART_B1 (journal 16/21/147/152): Parts B/C share the rule; B1 spent 4 of 24 calls on 2 boundaries.
            attempts = 1
        attempt = supersedes = rebuilds = 0
        start_budget, page_reset = session.boundary_budget, False
        choice = "invalid"
        while attempt < attempts:
            built = self._skill_boundary_request(session, trigger)
            if isinstance(built, str):
                return built
            request, calls, arg_map = built
            try:
                if isinstance(self._provider, ProductionDecisionProvider) and getattr(self._provider, "policy", None):
                    # final fit before trial/call admission
                    self._skill_render_fault(session, trigger, request, self._provider._render_messages(request))
            except ValueError as exc:
                with self._lock:
                    # a17 d7:J638: this return kept the generation in flight; every loop gate then waited 600 s.
                    if self._inflight_generation == request.generation_id:
                        self._inflight_generation = None
                if str(exc) != "skill_render_budget_exceeded" or rebuilds >= 2:
                    self._skill_render_fault(session, trigger, request, exc=exc)
                    if (str(exc) == "skill_render_budget_exceeded" and session.boundary_page is not None
                            and not page_reset):
                        # fix42: a read-only page selection is never a reason to lose the decision itself.
                        self._journal(dict(ev="skill_boundary_page_reset", session_id=session.session_id,
                                           handle=str(session.boundary_page[0])[:40], reason="render_failed"))
                        session.boundary_page, session.boundary_budget = None, start_budget
                        page_reset, rebuilds = True, 0
                        continue
                    return "provider_error"
                if "unknown_ui" in request.skill_offer:
                    # Capture the exact game input before a reduced-budget rebuild.
                    self._skill_render_fault(session, trigger, request, exc=exc)
                rebuilds += 1
                session.boundary_budget //= 2
                self._journal(dict(ev="skill_budget_rebuild", budget=session.boundary_budget, n=rebuilds))
                continue
            ui_lease_view = None
            try:
                ui_offer = getattr(self, "_live7_ui_offer", None)
                if ui_offer is not None:
                    from arti_stardew_runtime import _STARDEW_DECISION_LOGICAL_TIMEOUT_SEC
                    timeout = getattr(self._provider, "_logical_timeout_sec", _STARDEW_DECISION_LOGICAL_TIMEOUT_SEC)
                    timeout_ms = int(timeout * 1000) + 1000
                    if not 0 < timeout_ms <= 120000:
                        return "provider_error"
                    ui_lease_view = self._live7_unknown_ui_view(session)
                    if (ui_lease_view is None and getattr(self, "_live7_ui_view_miss", None) == "motor_stale"
                            and self._config.get("live7_fact_line_enabled") is True):
                        # The projected page may outlast its first motor sample. A
                        # newer native frame has to pass the same receiver fence.
                        pid = request.snapshot.binding.player_id
                        motor = self._state.latest(pid)
                        if motor is not None and self._state.wait_next(pid, motor.revisi, 0.5) is not None:
                            ui_lease_view = self._live7_unknown_ui_view(session)
                    lease_started = time.monotonic()
                    if ui_lease_view is None or not self._live7_ui_decision_lease(ui_lease_view, timeout_ms):
                        # ADR 43: this return was silent (a27: 150 superseded decisions, site not logged).
                        self._journal({"ev": "skill_model_superseded", "session_id": session.session_id,
                                       "n": supersedes + 1, "site": "decision_lease",
                                       "fence": ("view_unavailable:" + str(getattr(self, "_live7_ui_view_miss", None))
                                                 if ui_lease_view is None else "native_lease_refused")})
                        ui_lease_view = None
                        return "superseded"
                    ui_offer["decision_until"] = lease_started + timeout_ms / 1000
                if self._trial_admission is not None:
                    try:
                        self._trial_admission("decision")
                    except ValueError as exc:
                        return "trial_limit:" + str(exc)[:40]
                with self._lock:
                    session.model_decisions += 1
                    self._skill_day["model"] += 1
                decision = None
                self._skill_fence_site = self._live7_ui_fence_last = None
                try:
                    if isinstance(self._provider, ProductionDecisionProvider):
                        raw = self._provider.complete(request, start_fence=lambda go: self._skill_fence(request, go))
                    else:
                        with self._lock:
                            self._skill_fence_site = "start_fence"
                            if not self._skill_request_current(request):
                                raise _Superseded()
                        raw = self._provider.complete(request)
                    if (getattr(self, "_live7_gift_offer", None) is not None
                            or getattr(self, "_live7_ui_offer", None) is not None):
                        with self._lock:
                            self._skill_fence_site = "after_reply"
                            if not self._skill_request_current(request):
                                raise _Superseded()
                    decision = decode_game_decision(raw)
                    if getattr(decision, "normalized", 0):
                        self._journal(dict(ev="choice_normalized", envelope=decision.normalized))
                    if decision.dropped:
                        # ADR 155: her choice is kept; the stray field is named, never dropped silently.
                        self._journal(dict(ev="FAULT", session_id=session.session_id, type="decision_field_dropped",
                                           dropped=list(decision.dropped), trigger=trigger, attempt=attempt + 1,
                                           generation=request.generation_id, disposition="CONTINUE"))
                    rendered = self._skill_rendered_calls(request, calls)
                    from arti_runtime.item_choice import native_reply
                    decision = native_reply(decision, getattr(request, "item_choice_aliases", {}))
                    navigation = ()
                    page_aliases = {}
                    if str(getattr(decision.choice, "args", {}).get("name", "")).strip().casefold() == "read_boundary":
                        messages = self._provider._render_messages(request)
                        shown = json.loads(messages[1]["content"])
                        navigation = next((e.get("args", ()) for e in shown["skills"] if e["name"] == "read_boundary"), ())
                        from arti_runtime.page_fact_selection import notes_answered
                        from arti_runtime.choice_contract import navigation_aliases
                        page_aliases = navigation_aliases(shown)
                    from arti_runtime.choice_contract import normalize, echo_normalizable
                    goods_names = getattr(getattr(session.campaign, "live", None), "store_names", None)
                    decision, normalizations = normalize(decision, rendered, navigation, goods_names=goods_names,
                                                          page_aliases=page_aliases)
                    echo_ok = (decision.request_nonce, decision.goal_id, decision.goal_revision,
                               decision.context_ref) == (request.request_nonce, request.goal_id,
                                                         request.goal_revision, request.context_ref)
                    if not echo_ok and session.mode == "part_live":
                        from arti_stardew_live7 import choice as live_choice
                        proposed = (getattr(decision.choice, "args", {}).get("arg") in navigation if navigation
                                    else live_choice(decision, rendered, goods_names=goods_names) is not None)
                        if echo_normalizable(decision, request, current=self._skill_request_current(request), offered=proposed):
                            decision = replace(decision, request_nonce=request.request_nonce)
                            normalizations["echo"] = 1
                            echo_ok = True
                    if normalizations:
                        self._journal(dict(ev="choice_normalized", **normalizations))
                    rejection = None
                    if not echo_ok:
                        choice = None
                        rejection = dict(code="echo_mismatch", valid=dict(request_nonce=request.request_nonce,
                            goal_id=request.goal_id, goal_revision=request.goal_revision, context_ref=request.context_ref), fields=[k for k in (
                            "request_nonce", "goal_id", "goal_revision", "context_ref")
                            if getattr(decision, k) != getattr(request, k)])
                    elif (decision.choice.kind == "act" and decision.choice.operation == "skill"
                            and decision.choice.target_ref is None and decision.choice.args.get("name") == "read_boundary"):
                        arg = decision.choice.args.get("arg")
                        if arg in navigation and set(decision.choice.args) == {"name", "arg"}:
                            notes_answered(session, messages)
                            handle, index = arg.rsplit(":", 1)
                            session.boundary_page = (handle, int(index))
                            session.boundary_reads += 1
                            self._journal(dict(ev="skill_boundary_page", handle=handle, page=int(index)))
                            if session.mode == "part_live" and self._live7_reserve_due_after_read(session):
                                session.boundary_page = None
                                return "superseded"  # ADR 108: the next tick's home_due sees the clock
                            continue  # read-only, no skill attempt or motor input
                        choice = None
                        rejection = dict(code="page_not_offered", field="args.arg", offered_args=list(navigation),
                                         offered=sorted({c.name for c in rendered} | {"read_boundary"})[:16])
                    elif (getattr(self, "_live7_ui_offer", None) is not None
                            and str(getattr(decision.choice, "args", {}).get("arg", "")).startswith(("simpan:", "ambil:", "kirim:", "jual:"))):
                        from arti_runtime.bag_sitting import valid
                        args = decision.choice.args
                        shown = [c.arg for c in rendered if c.name == 'ui_action']
                        if (decision.choice.kind == 'act' and decision.choice.operation == 'skill'
                                and decision.choice.target_ref is None and set(args) == {'name', 'arg'}
                                and args['name'] == 'ui_action' and valid(args['arg'], self._live7_unknown_ui_view(session), shown)):
                            choice = skills.SkillCall('ui_action', args['arg'])
                        else:
                            choice = None
                    elif (getattr(self, "_live7_ui_offer", None) is not None
                            and re.match(r"\s*(?:press\s*:|type:|settle:|buy:)",
                                         str(getattr(decision.choice, "args", {}).get("arg", "")), re.I)):
                        from scripts.live7.transactions import resolve_transaction_action, purchase_holder
                        from arti_runtime.shop_quantity import free_quantity
                        args = decision.choice.args
                        if (decision.choice.kind == "act" and decision.choice.operation == "skill"
                                and decision.choice.target_ref is None and set(args) == {"name", "arg"}
                                and args["name"] == "ui_action"):
                            try:
                                view = self._live7_unknown_ui_view(session)
                                if free_quantity(view, args["arg"]) is None:
                                    resolve_transaction_action(purchase_holder(session, self._active), view, args["arg"])
                                from arti_stardew_generic_ui import action_key
                                canonical = action_key(view, args["arg"])
                                choice = skills.SkillCall("ui_action", args["arg"])
                                if canonical != args["arg"]:
                                    self._journal(dict(ev="choice_normalized", ui_arg=1))
                            except (ValueError, AttributeError) as exc:
                                choice = None
                                # PM-1 a9 #3: name the valid options (copied args) when no ID is pressable.
                                offered = [k for k in self._live7_ui_offer.get("actions", ()) if k != "wait"]
                                rejection = dict(code=str(exc) if isinstance(exc, ValueError)
                                                 else "generic_ui_view_unavailable",
                                    received_arg=str(args.get("arg", ""))[:160],
                                    expected=("press:<numeric id>:<listed variant>; labels are exact and unique"
                                              if any(k.startswith("press:") and k.count(":") >= 2 for k in offered)
                                              else ("copy one offered arg: " + ", ".join(offered))[:160]))
                                from arti_stardew_generic_ui import action_feedback
                                rejection.update(action_feedback(self._live7_unknown_ui_view(session), args.get("arg")))
                        else:
                            choice = None
                    elif campaign:
                        choice = skills.campaign_choice(decision, rendered[0]) if rendered else None
                    elif session.mode == "part_live":
                        from arti_stardew_live7 import choice as live_choice
                        choice = live_choice(decision, rendered, arg_map,
                            goods_names=getattr(getattr(session.campaign, "live", None), "store_names", None))
                    else:
                        choice = skills.skill_choice(decision, rendered, arg_map)
                    shown_buys = None
                    seen = getattr(self, "_live7_ui_seen_view", None)
                    if (isinstance(choice, skills.SkillCall) and choice.name == "ui_action" and seen is not None
                            and (seen.shop is not None or str(choice.arg).startswith("buy:")
                                 or any(c.get("kind") == "stock" for c in seen.components))
                            and isinstance(self._provider, ProductionDecisionProvider)
                            and getattr(self._provider, "policy", None)):
                        from arti_runtime.decision_plan import page_buy
                        page = json.loads(self._provider._render_messages(request)[1]["content"])
                        cards = (page.get("unknown_ui") or {}).get("buy_actions") or {}
                        aliases = getattr(request, "item_choice_aliases", {}).get("ui_action", {})
                        shown_buys = {aliases.get(arg, arg): card for arg, card in cards.items()}
                        if page_buy(seen, choice.arg, shown_buys) == "shop_offer_unread":
                            choice = None
                            rejection = dict(code="shop_offer_unread", field="args.arg",
                                             valid="Baca halaman dengan harga dan syarat tukar untuk kontrol itu.")
                    if choice is not None and getattr(decision, "plan", ()):
                        from arti_runtime.decision_plan import admit
                        if not admit(self, session, decision, choice, rendered, arg_map, shown_buys):
                            choice = None
                            from arti_runtime.choice_contract import plan_feedback
                            rejection = plan_feedback(decision, rendered)
                    choice = "invalid" if choice is None else choice
                    if choice == "invalid":
                        from arti_stardew_live7 import choice_feedback
                        from arti_runtime.experience_facts import explore_arg
                        travel_args = {a for e in request.skill_offer.get("skills", ()) if e.get("name") == "pergi_ke"
                                       for a in e.get("args", ())}
                        feedback_calls = [replace(c, arg=explore_arg(c.arg)) if c.name == "pergi_ke"
                                          and explore_arg(c.arg) in travel_args else c for c in rendered]
                        session.choice_feedback = dict(
                            rejection or choice_feedback(decision, feedback_calls), input_taken=False)
                        self._journal(dict(ev="skill_choice_rejected", session_id=session.session_id,
                                           trigger=trigger, **session.choice_feedback))
                    else:
                        session.choice_feedback = None
                    if choice == "ask_owner":
                        session.question = (decision.speech or "")[:400]
                except DecisionCodecError as exc:
                    choice = "invalid"
                    from arti_runtime.choice_contract import malformed_feedback
                    session.choice_feedback = malformed_feedback(exc.code)
                    self._journal({"ev": "skill_model_malformed", "session_id": session.session_id, "code": exc.code})
                    self._journal(dict(ev="skill_choice_rejected", session_id=session.session_id,
                                       trigger=trigger, **session.choice_feedback))
                except _Superseded:
                    supersedes += 1
                    # ADR 43: which fence (start / after reply) and, inside a menu, which predicate refused it.
                    site = self.__dict__.pop("_skill_fence_site", None) or "provider"
                    fence = (self.__dict__.pop("_live7_ui_fence_last", None)
                             if getattr(self, "_live7_ui_offer", None) is not None else None)
                    if fence and (fence in ("view_identity_changed", "receiver_changed", "snapped_changed",
                                           "prior_world_delta_changed", "native_motor_changed", "decision_lease_expired")
                                  or fence.startswith("view_unavailable:")):
                        self._live7_menu_changed(session, fence)
                    changes = self.__dict__.pop("_live7_ui_offer_changes", None)
                    self._journal({"ev": "skill_model_superseded", "session_id": session.session_id, "n": supersedes,
                                   "site": site, "fence": fence,
                                   **({"offer_fact_changes": changes} if fence == "offer_facts_changed" else {})})
                    if supersedes > 2:
                        return "superseded"
                    self._skill_wait_quiet(2.0)
                    continue
                except Exception as exc:  # noqa: BLE001 - provider failure ends the boundary, never the worker
                    if (isinstance(exc, ValueError) and "unknown_ui" in request.skill_offer
                            and str(exc) in ("provider_request_too_large", "provider_input_bound_exceeded",
                                             "provider_input_reserve_exceeded", "skill_render_budget_exceeded")):
                        self._skill_render_fault(session, trigger, request, exc=exc)
                    if (isinstance(exc, ValueError) and str(exc) in ("provider_request_too_large",
                            "provider_input_bound_exceeded", "provider_input_reserve_exceeded", "skill_render_budget_exceeded")
                            and rebuilds < 2):
                        rebuilds += 1
                        session.boundary_budget //= 2
                        self._journal(dict(ev="skill_budget_rebuild", budget=session.boundary_budget, n=rebuilds))
                        continue  # a fresh request, strictly smaller final wire budget
                    from arti_stardew_diagnostics import exception_record
                    self._skill_last_provider_error = (type(exc).__name__, str(exc)[:60])
                    self._journal(dict(ev="skill_model_error", session_id=session.session_id,
                        **exception_record(exc, phase="model_choice"),
                        code=str(exc)[:60] if isinstance(exc, ValueError) else None))
                    if session.mode == "part_live":
                        self._live7_recovery_fault(session, "model_choice_error",
                            refs=dict(generation=request.generation_id, trigger=trigger))
                    return "provider_error"
            finally:
                if ui_lease_view is not None:
                    self._live7_ui_decision_lease(ui_lease_view, 0)
                with self._lock:
                    if self._inflight_generation == request.generation_id:
                        self._inflight_generation = None
            attempt += 1
            if decision is not None and self._config.get("stardew_game_text_delivery_enabled") is True:
                from arti_runtime.game_text_delivery import accept_answer
                accept_answer(self, request)
            if decision is not None and self._config.get("live7_fact_line_enabled") is True:
                from arti_runtime.recipe_reads import accept_answer as accept_recipe_reads
                accept_recipe_reads(self, request)
            if (session.mode == 'part_live' and decision is not None
                    and (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True):
                # ADR 221: noticing survives even a choice mismatch; it never
                # changes the choice validator or authorizes an action.
                try:
                    from arti_runtime.goal_ladder_runtime import received, decision_note
                    received(self, session, (request.skill_offer or {}).get('mind') or {}, request=request)
                    decision_note(self, session, decision, request)
                except Exception as exc:  # observation failure cannot invalidate her choice
                    self._journal(dict(ev='goal_note_error', error=type(exc).__name__))
            # Mind slice 1: her why (speech) + goal_ref are journaled and overwrite mind/now.json (Live7 glue).
            mind = (self._live7_mind_decision(session, decision, choice, request)
                    if session.mode == "part_live" and decision is not None and choice != "invalid" else {})
            if (session.mode == "part_live" and decision is not None and choice != "invalid"
                    and getattr(self, "_said_to_you", ())):
                from arti_stardew_live7_help import Live7HelpMixin
                render = getattr(self._provider, "_render_messages", None)
                try:
                    shown = json.loads(render(request)[1]["content"]) if callable(render) else request.skill_offer
                    Live7HelpMixin._live7_said_answered(self, shown)
                except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                    self._journal(dict(ev="said_page_unreadable"))  # observation cannot invalidate her choice
            self._journal({"ev": "skill_model_decision", "session_id": session.session_id, "trigger": trigger,
                           "attempt": attempt,
                           "choice": [choice.name, choice.arg] if isinstance(choice, skills.SkillCall) else choice,
                           **({"plan": list(decision.plan)} if decision is not None and decision.plan else {}),
                           **mind})
            if session.mode == "part_live" and choice == "invalid":
                if decision is not None:
                    self._skill_remember_rejected_page(session, request, calls)
                self._live7_recovery_fault(session, "invalid_model_choice",
                    refs=dict(generation=request.generation_id, trigger=trigger, attempt=attempt))
            if choice != "invalid":
                self.__dict__.pop("_skill_rejected_page", None)
                break
        if choice != "invalid" and "unknown_ui" not in request.skill_offer:
            # fix42 (a33 d7:J640-J669): a read selection outlived her decision, so every later boundary reopened the
            # same read page. A read serves the decision it was taken for; the next boundary starts at its page 0.
            # A menu keeps its page across presses (ADR 42).
            render = getattr(self._provider, "_render_messages", None)
            if session.boundary_page is not None and decision is not None and callable(render):
                from arti_runtime.page_fact_selection import notes_answered
                notes_answered(session, render(request))
            session.boundary_page = None
        if isinstance(choice, skills.SkillCall):
            # Live7 WP4: the exact physical request (archive file number) whose reply produced this choice.
            lookup = getattr(self._provider, "request_index_for", None)
            index = lookup(request.generation_id) if callable(lookup) else None
            with self._lock:
                session.model_pick = (choice, index if type(index) is int else None)
                from arti_stardew_event_control import festival_free_roam, festival_seated
                movement = getattr(request.snapshot, "event_movement", None)
                actions = getattr(request.snapshot, "event_actions", None)
                festival = (movement.get("festival_id") if festival_free_roam(movement)
                            else actions.get("event_id") if festival_seated(actions) else None)
                if choice.name == "pergi_ke" and festival:
                    session.festival_leave_pick = (choice, festival)
        return choice

    @staticmethod
    def _skill_retry_scene(snapshot):
        """Only an identified, current scene can reuse a page; missing revisions fail closed."""
        scene = next((v for v in getattr(snapshot, "observations", ())
                      if v.domain == "scene.current_location"), None)
        if (scene is None or scene.status != "available" or not scene.available
                or scene.stale or scene.requires_resync):
            return None
        motor = snapshot.motor
        return (snapshot.binding, scene.domain_revision, motor.location, motor.active_menu,
                getattr(motor, "year", None), getattr(motor, "season", None), getattr(motor, "day", None))

    def _skill_remember_rejected_page(self, session, request, calls):
        scene = self._skill_retry_scene(request.snapshot)
        if scene is None:
            return
        render = getattr(self._provider, "_render_messages", None)
        messages = render(request) if callable(render) else skills.render_skill_messages(request)
        frozen = replace(request, skill_offer=deepcopy(request.skill_offer))
        self._skill_rejected_page = (session, scene, tuple(calls), frozen, deepcopy(messages),
                                     session.boundary_page, session.boundary_budget)

    def _skill_reuse_rejected_page(self, session, request, calls, trigger):
        cached = self.__dict__.pop("_skill_rejected_page", None)
        if cached is None or not session.choice_feedback:
            return None
        owner, scene, offered, previous, messages, page, budget = cached
        from arti_runtime import hands_halt
        if ((previous.skill_offer.get("facts") or {}).get(hands_halt.KEY)
                != hands_halt.page_line(hands_halt.read(self))):
            return None  # fix144A (R1 SF3): whether a HALT holds her hands changed; a fresh page says the current truth
        if (owner is not session or self._skill_retry_scene(request.snapshot) != scene
                or tuple(calls) != offered or session.boundary_page != page or session.boundary_budget != budget
                or request.read_identity != previous.read_identity
                or request.observation_sessions != previous.observation_sessions):
            return None
        generic = getattr(self, "_live7_ui_offer", None)
        if "unknown_ui" in previous.skill_offer:
            # Reuse only when the full presentation still agrees. A matching native pin
            # alone cannot certify that cached screen text or world facts are current.
            from arti_stardew_generic_ui import menu_offer_pin
            if (generic is None or generic["facts"] != previous.skill_offer["unknown_ui"]
                    or menu_offer_pin(generic) != previous.ui_offer_pin):
                return None
            request = replace(request, ui_offer_pin=previous.ui_offer_pin)
        elif generic is not None:
            return None
        feedback = dict(session.choice_feedback,
            rule="Previous choice was not run. Use the current echo and offered skills/args; no choice is selected for you.")
        shown = json.loads(messages[1]["content"])
        if "echo" not in shown:
            return None
        shown["echo"] = dict(request_nonce=request.request_nonce, goal_id=request.goal_id,
                             goal_revision=request.goal_revision, context_ref=request.context_ref)
        shown["decision_feedback"] = feedback
        saved = getattr(previous, 'live7_day_plan', None)
        if saved is not None and self._config.get('stardew_live7_plan_on_page') is not True:
            return None
        plan_shown = saved is not None and 'rencana_hari_ini' in (shown.get('mind') or {})
        messages[1]["content"] = json.dumps(shown, ensure_ascii=False, separators=(",", ":"), sort_keys=not plan_shown)
        offer = dict(previous.skill_offer, decision_feedback=feedback)
        retry = replace(request, skill_offer=offer, skill_retry_messages=messages,
                        item_choice_aliases=previous.item_choice_aliases, ui_inspect_layout=previous.ui_inspect_layout)
        if saved is not None:
            retry = replace(retry, live7_day_plan=saved)
        # Keep every admission ceiling. If the note no longer fits, use the ordinary fresh fit path.
        from arti_stardew_runtime import (_encoded_provider_request, _decision_provider_extra_payload,
            _provider_input_token_upper_bound, _skill_prompt_token_estimate, _STARDEW_DECISION_REQUEST_MAX_BYTES,
            _STARDEW_DECISION_INPUT_TOKEN_HARD_BOUND, _STARDEW_DECISION_INPUT_TOKEN_RESERVE,
            _STARDEW_PROTECTED_INPUT_TOKEN_BOUND, _protected_decision_context, _protected_reserve_exceeded)
        model = getattr(getattr(self._provider, "policy", None), "model", "")
        encoded = _encoded_provider_request(model, messages, _decision_provider_extra_payload(True, messages=messages))
        scale = _page_budget.for_request(retry).scale
        trial_owner = getattr(self._provider, "trial_owner_authorized", False)
        protected = _protected_decision_context(request.snapshot, trial_owner_authorized=trial_owner)
        ui_bound = (_STARDEW_PROTECTED_INPUT_TOKEN_BOUND if generic is not None and protected
                    else _STARDEW_DECISION_INPUT_TOKEN_HARD_BOUND) * scale
        if (len(encoded) > min(budget, _STARDEW_DECISION_REQUEST_MAX_BYTES * scale)
                or skills.encoded_render_bytes(messages) > budget
                or _provider_input_token_upper_bound(encoded) > ui_bound - (512 * scale if generic is not None else 0)
                or _protected_reserve_exceeded(retry, encoded, trial_owner)
                or _skill_prompt_token_estimate(encoded) > _STARDEW_DECISION_INPUT_TOKEN_RESERVE * scale
                or (_page_budget.for_request(retry) != _page_budget.DEFAULT and (
                    not _page_budget.fits_page(messages, budget=_page_budget.for_request(retry))
                    or skills.encoded_render_bytes(messages) > _page_budget.for_request(retry).guard))):
            return None
        if generic is not None and (layout := generic.get("inspect_layout")) is not None:
            layout[:] = previous.ui_inspect_layout or ()
        self._logical_request = retry
        self._journal(dict(ev="skill_boundary_page_reused", session_id=session.session_id, trigger=trigger))
        self._journal(dict(ev="skill_offer", session_id=session.session_id, trigger=trigger,
            skills=[c.name for c in calls], offered=[str(c.arg)[:64] for c in calls],
            talk_targets={n["ref"]: dict(n, kind="npc") for n in
                          (offer.get("social_availability") or {}).get("observed_npcs", ())}))
        self._journal(dict(self._skill_render_size(retry), ev="skill_boundary_render_bytes",
            session_id=session.session_id, trigger=trigger, render_rungs=0, render_reused=True))
        return retry, calls, {n: [c.arg for c in calls if c.name == n and c.arg is not None] for n in {c.name for c in calls}}

    @W.span('offer_build', 17, runtime=True)
    def _skill_boundary_request(self, session: SkillSession, trigger: str):
        """A fresh skills-only boundary request on the session's current goal, or a terminal string."""
        if session is self._skill_session and session.mode == "part_live" and session.campaign is not None:
            self._live7_flush_visit(session.campaign)
        with self._lock:
            if session is not self._skill_session or session.status != "active" or self._active is None:
                return "none"
            budget = self._skill_budget_left(session)
            if budget["model_left"] <= 0 or budget["skills_left"] <= 0:
                self._journal({"ev": "skill_budget_exhausted", "session_id": session.session_id,
                               "scope": "model" if budget["model_left"] <= 0 else "skills", "trigger": trigger})
                return "budget"
            request = self._begin_generation(text=session.goal_text, trigger_type="continuation", viewer_name=None,
                                             reason=skills.SKILL_BOUNDARY_REASON, replace_goal=False)
            page_budget = getattr(session, "page_budget", None) or _page_budget.DEFAULT
            # Default legacy boundaries can return before constructing a full
            # GenerationRequest (including lightweight adapter requests).
            if page_budget != _page_budget.DEFAULT:
                request = replace(request, decision_page_budget=page_budget)
            facts = self._skill_facts(request.snapshot)
            failed = {r.call.name for r in session.runs if r.status == "failed"
                      and sum(x.call.name == r.call.name and x.status == "failed" for x in session.runs) >= 2}
            calls = skills.admissible_skills(facts, unlocked=self._skill_unlocked(), for_model=True, exclude=failed,
                                             limit=skills.MAX_OFFERED)
            withheld_line = None
            if session.mode == "part_live":
                calls = self._live7_calls(session, request.snapshot, facts)
                # ADR 241: one rule in the offer path for every skill (rung 2's repeated choice, C1's zero-input refusal).
                from arti_runtime import offer_withhold
                calls, withheld_line = offer_withhold.apply(self, session, calls, facts)
                reused = self._skill_reuse_rejected_page(session, request, calls, trigger)
                if reused is not None:
                    return reused
            if session.mode in ("part_a", "part_b", "part_c") and session.campaign_offer is not None:
                # Explicit owner campaign authorizes only this scheduled skill.
                # It does not unlock owner-only skills in ordinary free mode.
                calls = [session.campaign_offer]
            if not calls:
                self._inflight_generation = None
                self._journal({"ev": "skill_no_admissible", "session_id": session.session_id, "trigger": trigger})
                if session.mode == "part_live" and session.boundary_page is not None:
                    # ADR 128 (a54 d13 J1280, d19 J2469, d23 J635): the world moved under a read, here a gift question
                    # opened, so the decision she was reading for is over. Not her unusable choice, and its page must not
                    # follow her to the next decision (d13 J1285, d19 J2474: a gift question on a progress page of 77).
                    session.boundary_page = None
                    return "superseded"
                return "none"
            from arti_runtime.mines_offer import up_label
            outcomes = [dict(skill=r.call.name, status=r.status,
                            result=UNKNOWN_TEXT if r.status == "unknown" else r.verdict or r.reason,
                            **({"label": label} if (label := up_label(r.call.name, r.call.arg,
                                (r.state.get("live7_baseline") or {}).get("location"))) else {}),
                            **({"plan_input": r.report()["plan_input"]} if "plan_input" in r.state else {}))
                        for r in session.runs if r.status != "skipped"]
            from arti_runtime.page_fact_selection import enabled as small_page
            if small_page(self._config):
                self._live7_page_deferred = {}
            generic = getattr(self, "_live7_ui_offer", None)
            if generic is not None:
                from arti_stardew_generic_ui import menu_offer_pin
                request = replace(request, ui_offer_pin=menu_offer_pin(generic),
                                  ui_inspect_layout=generic.get("inspect_layout"))
                listed = [dict(name="ui_action", does="Choose an observed UI control or wait",
                               args=list(generic["actions"]))]
                for call in generic.get("world_calls") or ():
                    row = dict(name=call.name)
                    if call.arg is not None:
                        row["args"] = [call.arg]
                    listed.append(row)
                offer = dict(owner_goal=session.goal_text[:400], trigger=trigger, live7=True,
                             skills=listed, unknown_ui=generic["facts"], outcomes=[], facts={},
                             budget=dict(unlimited=True))
            else:
                offer = skills.skill_offer(goal_text=session.goal_text, calls=calls, facts=facts, outcomes=outcomes,
                                           budget=budget, trigger=trigger)
            if session.mode == "part_live" and generic is None:
                offer["live7"] = True  # internal request marker; never accepted from a model reply
                advisories = self._live7_help_advisories(session)
                if advisories:
                    offer["help_advisories"] = advisories
                offer["budget"] = dict(unlimited=True, cost_alarm="notify_only")
                self._live7_enrich_offer(session, request.snapshot, facts, calls, offer)  # clock/day + travel
                if purchase := getattr(session, "_shop_purchase_result", None):
                    offer["facts"]["purchase_result"] = purchase
                from arti_runtime import offer_withhold
                if withheld_line:
                    offer.setdefault("facts", {})[offer_withhold.KEY] = withheld_line
                if told := offer_withhold.told(self, session):
                    offer.setdefault("facts", {})[offer_withhold.TOLD] = told  # ADR 241 B: what the ladder did, once
                from arti_runtime import raw_hands
                if raw_hands.enabled(self._config):
                    # ADR 243: one plain line for her raw hands, and what the game answered to her last raw press.
                    if hands := raw_hands.world_line(calls, getattr(self, "_live7_raw_use_why", None)):
                        offer.setdefault("facts", {})[raw_hands.KEY] = hands
                    if answered := raw_hands.result_line(session):
                        offer.setdefault("facts", {})[raw_hands.RESULT] = answered
            elif (session.mode == "part_live" and generic is not None
                  and (getattr(self, "_config", None) or {}).get("stardew_live7_goal_ladder", False) is True):
                self._live7_mind_offer(session, facts, calls, offer)
            if session.mode == "part_live":
                # fix144A C10 (ADR 249): her hands held, why, who clears, on every page of hers, a menu's too (R1 SF3).
                from arti_runtime import hands_halt
                if held := hands_halt.page_line(hands_halt.read(self)):
                    offer.setdefault("facts", {})[hands_halt.KEY] = held
            if session.mode in ("part_a", "part_b", "part_c") and session.campaign_offer is not None:
                # PART_B1 (provider 001-004): the generic location words (rumah/hutan/kota/bus stop) never
                # matched the scheduled pergi_ke home/town, so a copied arg was always invalid. List only
                # the scheduled arg; campaign_choice accepts it or a bare name.
                listed = skills.campaign_offer_args(session.campaign_offer)
                for entry in offer["skills"]:
                    entry.pop("args", None)
                    if listed:
                        entry["args"] = listed
            failures = skills.recent_failure_facts(session.runs,
                self._skill_failure_observation(request.snapshot), now=game_now(self))
            if failures:
                offer["facts"]["recent_failures"] = failures
            if session.choice_feedback:
                offer["decision_feedback"] = dict(session.choice_feedback,
                    rule="Previous choice was not run. Use the current echo and offered skills/args; no choice is selected for you.")
            gift = offer.get("gift")
            if isinstance(gift, dict) and session.boundary_reads >= skills.QUESTION_READ_LIMIT:
                # fix56B (a41 D2:J1240-J1468, ADR 82): the game holds an open question and the HOST stands waiting;
                # 32 read-only pages kept it open 4 min. After the bound this decision's page is the question with
                # its answers only (no read navigation). Her choice is unchanged: terima or tolak, never chosen for her.
                session.boundary_page = None
                offer["gift"] = dict(gift, bacaan=skills.QUESTION_READS_CLOSED % session.boundary_reads)
                offer["question_reads_closed"] = True
                self._journal(dict(ev="skill_boundary_reads_closed", session_id=session.session_id, trigger=trigger,
                                   reads=session.boundary_reads))
            elif session.boundary_reads >= skills.DECISION_READ_LIMIT:
                # fix62 (ADR 89): now that pages are reachable, one decision takes a bounded number; every choice rides
                # this page and the reads return with her next decision. Nothing is chosen for her.
                session.boundary_page = None
                offer["halaman_baca"] = skills.DECISION_READS_CLOSED % session.boundary_reads
                offer["question_reads_closed"] = True
                self._journal(dict(ev="skill_boundary_reads_closed", session_id=session.session_id, trigger=trigger,
                                   reads=session.boundary_reads))
            if session.mode == "part_live" and self._config.get("stardew_game_text_delivery_enabled") is True:
                from arti_runtime.game_text_delivery import attach
                attach(self, offer)
            if session.mode == "part_live":
                if (saved := offer.pop('_live7_day_plan', None)) is not None:
                    request = replace(request, live7_day_plan=saved)
                offer = self._live7_fact_line_offer(session, request.snapshot, offer)
                from arti_runtime.item_choice import present as present_item_choices, purchase_aliases
                aliases = present_item_choices(offer)
                if (generic is not None and self._config.get("live7_fact_line_enabled") is True
                        and any(key.startswith("buy:") for key in generic["actions"])):
                    aliases["ui_action"] = purchase_aliases(generic["facts"], generic["actions"])
                request = replace(request, item_choice_aliases=aliases)
                from arti_runtime.page_fact_selection import enabled as small_page, prepare
                if small_page(self._config):
                    context, _, _ = self._live7_context(request.snapshot)
                    catalog = self._live7_experience_catalog(context.get('world_knowledge'))
                    offer = prepare(offer, config=self._config, budget=session.page_budget,
                        deferred=(generic.get('page_deferred', {}) if generic is not None
                                  else getattr(self, '_live7_page_deferred', {})), catalog=catalog,
                        notes_read=getattr(session, "notes_read", ()),
                        # prepare owns/detaches only the fact policy's data dependencies.
                        policy=dict(catalog=catalog, config=self._config,
                                    context=context, policy=session.campaign.live))
                    session.notes_unread = getattr(offer, "notes", None)
            request = replace(request, skill_offer=offer)
            offer["boundary_page"] = session.boundary_page
            offer["render_budget"] = session.boundary_budget
            self._logical_request = request
            # PM-1 fix9: the exact offered set she saw (compact ids), so a receipt can tell whether e.g. close was offered.
            self._journal({"ev": "skill_offer", "session_id": session.session_id, "trigger": trigger,
                           "skills": [c.name for c in calls], "offered": [str(c.arg)[:64] for c in calls],
                           "talk_targets": {n["ref"]: dict(n, kind="npc") for n in
                               (offer.get("social_availability") or {}).get("observed_npcs", ())}})
            self._journal(dict(self._skill_render_size(request), ev="skill_boundary_render_bytes",
                               session_id=session.session_id, trigger=trigger))
            arg_map = ({n: [c.arg for c in calls if c.name == n and c.arg is not None] for n in {c.name for c in calls}}
                       if session.mode == "part_live" else
                       {} if session.mode in ("part_a", "part_b", "part_c") else skills.offered_args(calls, facts))
            return request, calls, arg_map

    @W.span('render', 18, runtime=True)
    def _skill_render_size(self, request) -> dict:
        """Actual render estimate and separate byte-conservative admission ceiling. Names/numbers only."""
        started = time.perf_counter()
        saved = getattr(request, 'live7_day_plan', None)
        shown_plan = None
        try:
            import arti_stardew_runtime as rt_mod
            if isinstance(self._provider, rt_mod.ProductionDecisionProvider):
                self._provider._skill_trace_enabled = self._config.get("telemetry_enabled", True)
            messages = (self._provider._render_messages(request) if getattr(self._provider, "policy", None)
                        else skills.render_skill_messages(request))
            model = getattr(getattr(self._provider, "policy", None), "model", None) or rt_mod.STARDEW_DECISION_MODEL
            encoded = rt_mod._encoded_provider_request(model, messages, rt_mod._decision_provider_extra_payload(
                request.snapshot.working_memory.play_mode == "local_activity", messages=messages))
            omitted = skills.render_omitted(messages)
            # fix62 (ADR 89): a44's report read reads per decision as pages per decision; say the page count itself.
            rendered = json.loads(messages[1]["content"])
            if saved is not None:
                shown_plan = (rendered.get('mind') or {}).get('rencana_hari_ini')
            presentation = rendered.get("presentation") or {}
            pages = presentation.get("pages", 1)
            source = request.skill_offer or {}
            if source.get('live7'):
                session = getattr(self, "_skill_session", None)
                policy = getattr(getattr(session, "campaign", None), "live", None)
                if records := getattr(policy, "place_visits", None):
                    from arti_runtime.place_visit_result import note_rendered
                    note_rendered(self, rendered, records)
                from arti_runtime.page_fact_selection import PageOffer
                metrics = getattr(self._provider, '_skill_render_metrics', {})
                reads = sum(len(e.get('args') or ()) for e in rendered.get('skills', ())
                            if e.get('name') in ('read_boundary', 'read_text'))
                reads += sum(str(a).startswith('inspect:') for e in rendered.get('skills', ())
                             if e.get('name')=='ui_action' for a in e.get('args') or ())
                # ADR 220/223: numeric evidence only; no denied values or refs.
                self._journal(dict(ev='page_fact_counts',
                    withheld=dict(getattr(self, '_live7_fact_counts', {})) if source.get('fact_line') else {},
                    left_off=metrics.get('selection_left_off', {}) if isinstance(source, PageOffer) else {},
                    read_handles=reads))
            blocks = [(k, len(json.dumps(v, ensure_ascii=False).encode())) for k,v in source.items()]
            for key in ('world','facts','day'):
                if isinstance(source.get(key), dict):
                    blocks.extend((key+'.'+k, len(json.dumps(v, ensure_ascii=False).encode()))
                                  for k,v in source[key].items())
            return {"bytes": len(encoded), "page_bytes": len(messages[1]["content"].encode("utf-8")), "token_bound": rt_mod._skill_prompt_token_estimate(encoded), "pages": pages,
                    **getattr(self._provider, "_skill_render_metrics", {}),
                    "render_ms": round((time.perf_counter() - started) * 1000, 3),
                    "render_rungs": skills.RENDER_RUNGS[0],
                    # FIX76A: `more` lived only in the provider archive, so a receipt could not see whether the bound ever ran.
                    **({"pages_more": len(presentation["more"].get("paths", ()))} if presentation.get("more") else {}),
                    "source_largest": dict(sorted(blocks, key=lambda row:-row[1])[:5]),
                    # ADR 139: what reached her, not merely what existed before rendering.
                    **({"action_facts": rendered["action_facts"]} if rendered.get("action_facts") else {}),
                    **({"omitted": omitted} if omitted else {}),
                    **_page_budget.for_request(request).telemetry(),
                    "render_budget_bytes": source.get("render_budget", 16128 * _page_budget.for_request(request).scale),
                    "reserve": rt_mod._STARDEW_DECISION_INPUT_TOKEN_RESERVE * _page_budget.for_request(request).scale,
                    "hard_token_bound": rt_mod._provider_input_token_upper_bound(encoded),
                    "hard_limit": rt_mod._STARDEW_DECISION_INPUT_TOKEN_HARD_BOUND * _page_budget.for_request(request).scale}
        except Exception as exc:  # noqa: BLE001 - a diagnostic never fails the boundary
            return {"error": type(exc).__name__, **_page_budget.for_request(request).telemetry()}
        finally:
            if saved is not None:
                import arti_mind
                self._journal(dict(ev='plan_line', target_day=saved.target_day,
                    status='rendered' if shown_plan else saved.status,
                    bytes=arti_mind.block_bytes(shown_plan) if shown_plan else 0))

    @W.span('settle_readback', 19, runtime=True)
    def _skill_wait_quiet(self, timeout: float) -> None:
        deadline = game_now(self) + timeout
        while game_now(self) < deadline and not self._worker_stop.is_set():
            with self._lock:
                if not (self._action_busy or self._inflight_generation is not None or self._results_pending):
                    return
            W.call('poll', 23, time.sleep, 0.05)

    def _skill_rendered_calls(self, request, calls):
        """The offered calls the model actually saw: a production render may trim the skills list to the input reserve
        (deterministic, so re-rendering gives the same list); a trimmed-off skill is never accepted."""
        from arti_stardew_runtime import ProductionDecisionProvider
        if not isinstance(self._provider, ProductionDecisionProvider):
            return calls
        messages = self._provider._render_messages(request)
        names = skills.rendered_skill_names(messages)
        if request.skill_offer.get("live7"):
            shown = json.loads(messages[1]["content"])
            args = {}
            for e in shown["skills"]:  # PM-1 fix5 #4: a page lists one entry per arg; union same-name entries
                aliases = getattr(request, "item_choice_aliases", {}).get(e["name"], {})
                args.setdefault(e["name"], []).extend(aliases.get(a, a) for a in e.get("args", ()))
            # Lossless boundary pages restrict presentation, never permission: every offered call stays choosable.
            from arti_runtime.experience_facts import explore_arg
            rendered = list(calls) if "presentation" in shown else [
                c for c in calls if c.name in args and (c.arg is None or c.arg in args[c.name]
                    or c.name == "pergi_ke" and explore_arg(c.arg) in args[c.name])]
            if "unknown_ui" in request.skill_offer:
                # Parameterized component actions resolve against the full fresh view, including
                # from a detail/other-kind page. Pages restrict presentation, never permission.
                rendered = list(calls)
                rendered.extend(skills.SkillCall("ui_action", arg) for arg in args.get("ui_action", ())
                                if arg.startswith("inspect:"))
            return rendered
        return [c for c in calls if c.name in names]

    def _skill_request_current(self, request) -> bool:
        # M15: a released mid-walk gift is answered while PAUSED_RECOVERY.
        # Keep the ordinary READY fence; only this pinned recovery decision may pass it.
        if getattr(self, "_live7_ui_offer", None) is not None:
            return self._live7_ui_request_current(request)
        return self._request_current(request) or self._live7_gift_request_current(request)

    def _skill_fence(self, request, start) -> None:
        from arti_stardew_runtime import _Superseded
        with self._lock:
            self._skill_fence_site = "start_fence"  # ADR 43: attributed in skill_model_superseded
            if not self._skill_request_current(request):
                raise _Superseded()
            start()
            self._skill_fence_site = "after_reply"

    # ------------------------------------------------------------------ session end
    def _skill_finish_plan_locked(self, session: SkillSession):
        """Verified iff every skill that ran ended finished (a retried skill counts by its last run) and at least
        one ran; a skipped composite step (its preconditions did not hold) is not a failure."""
        last = {}
        for r in session.runs:
            if r.status in ("finished", "failed", "unknown"):
                last[(r.call.name, r.call.arg)] = r.status
        if not last and session.runs and all(r.status == "skipped" and r.reason == "observed_no_dry_crops" for r in session.runs):
            return self._skill_end_locked(session, "not_applicable", "not_applicable", "observed_no_dry_crops")
        if not last and any(r.status == "skipped" and r.reason == "already_full" for r in session.runs):
            return self._skill_end_locked(session, "not_applicable", "not_applicable", "already_full")
        achieved = bool(last) and all(v == "finished" for v in last.values())
        return self._skill_end_locked(session, "finished" if achieved else "held",
                                      "achieved" if achieved else "partial",
                                      "skill_sequence_verified" if achieved else "skill_sequence_incomplete")

    def _skill_end_locked(self, session: SkillSession, status: str, verdict: str | None, reason: str, *,
                          emit: bool = True):
        # Recovery/day guards can run outside the worker's lock. Publish the
        # terminal state AND its pending result atomically to the trial observer.
        with self._lock:
            return self._skill_end_atomic(session, status, verdict, reason, emit=emit)

    def _skill_end_atomic(self, session: SkillSession, status: str, verdict: str | None, reason: str, *,
                          emit: bool = True):
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        if session.status != "active" and str(session.reason or "").startswith("stop:"):
            # fix144A C12 (ADR 249; bench 19 J705-J706): a session a STOP already ended is never ended again. A recovery
            # poll that read the ended session's identity as unavailable wrote a second SAFETY_STOP, and the cycle refused
            # on it (`journal_SAFETY_STOP`) before it could acknowledge the operator's STOP.
            return OwnerTurnResult(admitted=True, applied=False, skip_reply_tags=True, reason="skill_session_already_stopped")
        from arti_runtime.decision_plan import drop
        drop(self, session, "session_ended:" + reason)
        session.status, session.verdict, session.reason = status, verdict, reason
        if session.mode == "part_live" and session.campaign is not None and session.campaign.live_limits is not None:
            self._action_session_limit, self._trial_deadline_unix_ms = session.campaign.live_limits
        session.current = None
        own = self._skill_own_goals(session)
        self._skill_ended_goals = (self._skill_ended_goals + sorted(own))[-_ENDED_GOALS_KEEP:]
        if self._active is not None and self._active.goal_id in own:
            # The session's last goal ends with it, exactly like "[recorded remark omitted]": no live play grant, continuation,
            # event or voice resume may reach the provider with a button catalog after the session.
            self._supersede_generation()
            self._current_generation += 1
            self._inflight_generation = None
            if self._action_busy:
                self._cancel_action("skill_end")
            self._revoke_play()
            self._pending_continuation = None
            self._pending_event_reason = None
            self._active = None
        report = session.report()
        self._skill_history = (self._skill_history + [report])[-_REPORT_KEEP:]
        self._journal({"ev": "skill_session_finished", **report})
        plans = [r["plan_input"] for r in report["runs"] if "plan_input" in r]
        evidence = FinishEvidenceView(verdict or "partial", reason, {
            "mode": session.mode, "composite": session.composite, "skills_finished": report["skills_finished"],
            "skills_run": report["skills_run"], "model_decisions": session.model_decisions,
            "runs": [[r["skill"], r["status"], r.get("verdict") or r.get("reason")] for r in report["runs"]],
            **({"plan_input": plans[-1]} if plans else {})})
        result = OwnerTurnResult(admitted=True, applied=True, skip_reply_tags=True, reason="skill_sequence_finish",
                                 speech=StatusSpeech("Rangkaian skill selesai; tiap skill terverifikasi." if verdict == "achieved"
                                                     else "Rangkaian skill berhenti: " + reason),
                                 finish_evidence=evidence)
        if emit:
            self._skill_emit("skill_sequence_finish", result)
        return result

    def _skill_stop(self, reason: str) -> None:
        """Owner stop ('[recorded remark omitted]'): the session ends with the goal (caller holds the lock)."""
        session = getattr(self, "_skill_session", None)
        if session is not None and session.status == "active":
            self._skill_end_locked(session, "stopped", None, reason, emit=False)

    def skill_report(self) -> dict | None:
        """Per-skill outcomes for the trial report (no owner text, no ids beyond the session label)."""
        with self._lock:
            session = self._skill_session
            if session is None and not self._skill_history:
                return None
            current = session.report() if session is not None else self._skill_history[-1]
            return {"session": current, "day": {"skills": self._skill_day["skills"], "model": self._skill_day["model"]},
                    "model_decisions": current["model_decisions"]}
