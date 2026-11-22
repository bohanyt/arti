"""WS3b runtime side: npc talk / eat prompt / bed chains, readbacks, bed wait hold, finish verdicts.

Kept out of ``arti_stardew_runtime.py`` (several workstreams edit that file): ``StardewGoalRuntime``
inherits :class:`RoutineRuntimeMixin` and calls a handful of hooks. Every physical step reuses an
existing path: the press / walk is the unchanged single-step leased executor
(``_execute_leased_action(..., hold=True)``), every menu step is WS2's leased ``ui_key``
(``_run_ui_key``: ACQUIRE -> one guarded input_once -> RELEASE, session limit + trial input budget,
strictly newer ui.context readback). The runtime never invents a target: press args are the mod's
observed target, menu args are copied from the mod's published ``ui_key`` block, and the only answers
it gives itself are the owner objective's own (``Yes`` to ``Eat`` / ``Sleep``); ``npc_question`` is never
auto-answered.

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the CT live witness.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

import time
from arti_stardew_pause import game_now, wait_resume
from collections.abc import Mapping
from dataclasses import asdict, replace
from types import SimpleNamespace
from urllib.parse import unquote

import arti_stardew_interact as interact
import arti_stardew_interact_b as ib


def json_key(record: dict) -> str:
    import json
    return json.dumps({k: v for k, v in record.items() if k != "scene_age_s"}, sort_keys=True, default=str)

_BED_WAIT_JOURNAL_S = 30.0


def home_facility(runtime, session, run):
    """Name the exempt activity only on the authenticated last hop to her home."""
    if run is None:
        return None
    call = run.call
    shipment = call.name == "kirim" or call.name == "aksi" and (call.arg or "").startswith("xt:bin:")
    chest = (call.name == "simpan_ke_chest" and
             (getattr(runtime, "_config", None) or {}).get("stardew_live7_ship_on_the_way") is True)
    if not (shipment or chest):
        return None
    water, binding = getattr(runtime, "_water_context", None), getattr(runtime, "_binding_view", None)
    if not callable(water) or not callable(binding) or getattr(session, "campaign", None) is None:
        return None
    location = getattr(water(binding()[1]), "location", None)
    if not location:
        return None
    from arti_runtime.travel_time import home as home_route
    route = home_route(location, session.campaign.live).get("route")
    if isinstance(route, list) and 1 <= len(route) <= 2 and route[0] == location:
        return "shipment_at_home" if shipment else "chest_at_home"
    return None


def native_menu_open(runtime) -> bool:
    """ADR 231 part E: a game menu (a DialogueBox question, a chest) is up on her screen now (the motor's own read)."""
    state, binding = getattr(runtime, "_state", None), getattr(runtime, "_binding_view", None)
    if state is None or not callable(binding):
        return False
    pid = binding()[1]
    motor = state.latest(pid) if pid is not None else None
    return getattr(motor, "active_menu", None) is not None


class RoutineRuntimeMixin:
    """Mixed into StardewGoalRuntime; uses its lock, stores, leased executors and journal."""

    def _init_routine_runtime(self) -> None:
        self._routine_wait_journal_at = 0.0

    # ------------------------------------------------------------------ objective
    def _pin_routine_objective(self, goal, objective, request) -> None:
        pid = request.snapshot.binding.player_id
        objective.start_date = self._date_tuple(self._state.latest(pid)) if pid is not None else None
        goal.routine_objective = objective
        self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "pinned": asdict(objective)})

    def _routine_scene_facts(self, pin) -> dict:
        if pin is None:
            return {}
        rec = self._obs.get(pin.player_id, "scene.current_location")
        if (rec is None or not rec.available or not rec.authoritative or getattr(rec, "stale", False)
                or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
            return {}
        return dict(rec.facts or {})

    def _routine_world_offered(self, goal, target, *, gathering: bool, pin) -> bool:
        rec = self._obs.get(pin.player_id, "ui.context") if pin is not None else None
        offered = ib.interaction_offered(target, gathering=gathering, objective=getattr(goal, "routine_objective", None),
                                         facts=self._routine_scene_facts(pin), ui_facts=rec.facts if rec is not None else None)
        if offered and target.get("kind") == ib.KIND_NPC and pin is not None:
            # D32 (live B5W3a): never offer a talk whose grab tile (from the observed pixel) is ARTI's own
            # tile before the one D19 flush press was taken there: the flush puts the grab tile on him.
            from arti_stardew_runtime import _grab_tile
            motor = self._state.latest(pin.player_id)
            own = tuple(motor.tile) if motor is not None else None
            if (motor is not None and _grab_tile(motor.pixel, motor.facing) == own
                    and (getattr(goal, "chest_flush_key", None) or (None, None))[1] != own):
                return False
        return offered

    def _routine_talk_scene_pinned(self, ticket, old_scene, new_scene) -> bool:
        """D32: the ticket is a talk act and the current scene still has its villager on the target tile."""
        choice = getattr(ticket.decision, "choice", None)
        args = getattr(choice, "args", None)
        if (getattr(choice, "operation", None) != "world_interact" or ib.receiver_kind(args) != ib.KIND_NPC
                or (new_scene.session_id, new_scene.player_id, new_scene.screen_id)
                != (old_scene.session_id, old_scene.player_id, old_scene.screen_id)
                or new_scene.seq < old_scene.seq or new_scene.revision < old_scene.revision):
            return False
        rec = self._obs.get(new_scene.player_id, "scene.current_location")
        fact = (rec.facts or {}).get(args.get("source_fact_id")) if rec is not None else None
        tile = fact.get("tile") if isinstance(fact, dict) else None
        return isinstance(tile, dict) and (tile.get("x"), tile.get("y")) == (args.get("tile_x"), args.get("tile_y"))

    # ------------------------------------------------------------------ eat rule (D30)
    def process_routine_eat(self) -> int:
        """Worker hook (live B5 D30): once the eat target is offered for the runtime-chosen food, the
        runtime publishes the eat press itself (routine_eat_rule) inside the busy fence and runs the
        Yes follow-through, instead of waiting for a model act. Bounded to 2 attempts per objective."""
        from arti_stardew_decision import ActChoice, GameDecision
        from arti_stardew_runtime import READY
        try:
            finished = self._routine_eat_rule_finish()
            if finished is not None:
                if self._on_generation_result is not None:
                    self._on_generation_result(SimpleNamespace(reason="routine_eat_finish", generation_id=None), finished)
                return 1
            with self._lock:
                goal = self._active
                objective = getattr(goal, "routine_objective", None) if goal is not None else None
                if (objective is None or objective.kind != ib.OBJ_EAT or not ib.objective_open(objective)
                        or objective.opened or objective.eat_rule_attempts >= 2
                        or goal.play_mode != "local_activity" or goal.progress_marker == "terminal"
                        or self._status != READY or self._action_busy or self._inflight_generation is not None
                        or self._results_pending or self._day_start_active or self._worker_stop.is_set()
                        or self._trial_result_pending() or goal.question_basis or goal.no_progress_blocked):
                    return 0
                pin = self._water_context(self._binding_view()[1])
                rec = self._obs.get(pin.player_id, "ui.context") if pin is not None else None
                target = rec.facts.get("world_interaction_target") if rec is not None else None
                if not (isinstance(target, dict) and target.get("kind") == ib.KIND_EAT):
                    return 0
                request = self._begin_generation(text=goal.text, trigger_type="continuation", viewer_name=None,
                                                 reason="routine_eat_rule", replace_goal=False)
                offered = request.snapshot.world_interaction_target
                if not (isinstance(offered, dict) and offered.get("kind") == ib.KIND_EAT):
                    self._inflight_generation = None
                    return 0
                decision = GameDecision(1, request.request_nonce, request.goal_id, request.goal_revision,
                                        request.context_ref, "", ActChoice("world_interact", None, dict(offered), ()))
                objective.eat_rule_attempts += 1
                self._action_busy = True
                self._action_cancel.clear()
                goal.last_effect_motor = None
                self._journal({"ev": "routine_eat_rule", "goal_id": goal.goal_id, "generation_id": request.generation_id,
                               "attempt": objective.eat_rule_attempts, "target": offered.get("source_fact_id")})
            result = self._execute_routine_chain(decision, request, goal, kind=ib.KIND_EAT)
            if self._on_generation_result is not None:
                self._on_generation_result(SimpleNamespace(reason="routine_eat_rule", generation_id=request.generation_id),
                                           result)
            return 1
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            self._journal({"ev": "routine_eat_rule_error", "error": type(exc).__name__})
            with self._lock:
                self._action_busy = False
            return 0

    def _eat_full_fresh(self) -> dict | None:
        """FIXB18 review: the mod's eat_full only from a fresh (<= 2 s), same-session, authoritative ui.context."""
        _, pid = self._binding_view()
        pin = self._water_context(pid) if pid is not None else None
        rec = self._obs.get(pid, "ui.context") if pid is not None else None
        if (pin is None or rec is None or not getattr(rec, "authoritative", True)
                or getattr(rec, "session_id", None) != pin.observation_session_id
                or not 0 <= time.monotonic() - getattr(rec, "received_monotonic", -1e9) <= 2):
            return None
        return ib.vitals_full(rec.facts)

    BED_STAGE_STEPS_MAX = 8

    def _routine_bed_stage_invoke(self, request):
        """FIXB21 (live V2B18SLEEP-a: Cabin (8,8) at 01:20, the bed entry `bed:...:9,7@8,8` = ARTI's own tile, ARTI facing
        LEFT: `approach_arrived_and_facing` (a DIRECT stand needs no facing step), no move_through offered (it needs the
        farmer facing the sleep row), the next approach `approach_replan_required` -> HELD, 0 inputs). On the v2 pad path,
        an open sleep goal standing on a bed entry: move_through when the mod offers it, else one 1-tick turn toward the
        bed's sleep row (bounded)."""
        import json as _json
        with self._lock:
            goal = self._active
            objective = getattr(goal, "routine_objective", None) if goal is not None else None
            if (objective is None or objective.kind != ib.OBJ_SLEEP or not ib.objective_open(objective) or objective.opened
                    or request.goal_id != goal.goal_id or goal.progress_marker == "terminal"
                    or goal.play_mode != "local_activity" or request.reason == "owner_turn"):
                return None
            snap = request.snapshot
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            if not self._v2_pad_path(pin):
                return None
            home_leg = False
            stage0 = getattr(goal, "curfew_stage", None) or {}
            if pin is not None and stage0.get("status") not in ("home", "bed", "ended") and not stage0.get("sleep_leg"):
                home = self._scene_home_location(pin)
                # Live7: a currently offered native bed is usable even if the
                # actor's usual home is unreachable. Native ownership / sleep
                # admission and the ordinary next-morning verifier still own it.
                local_bed = (getattr(self, "_live7_active", lambda: False)() and
                    ((getattr(snap, "move_through_target", None) or {}).get("kind") == ib.KIND_BED
                     or any(e.get("kind") == ib.KIND_BED for e in getattr(snap, "approach_targets", ()) or ())))
                if home is not None and pin.location != home and not local_bed:
                    # FIXB27 D-B24-1 (live V2B24NIGHT-b): an owner / soak `tidur` away from ARTI's home: the locked home leg
                    # (the curfew stage's), never a model inspect loop for an off-scene bed. Once per goal.
                    nav = self._pin_sleep_home_leg(goal, pin, True, home, origin="sleep_goal")
                    goal.curfew_stage["sleep_leg"] = True
                    self._journal({"ev": "sleep_home_leg", "goal_id": goal.goal_id, "status": "pinned",
                                   "from": pin.location, "home_nav": nav})
                    home_leg = True
        if home_leg:
            raw = self._curfew_stage_invoke(request)
            if raw is None and getattr(objective, "blocker", None) == "home_unreachable":
                # FIXB27 review: no home leg from here: the worker's partial finish, never the model's inspect loop
                return self._rule_json(request, {"kind": "wait", "reason": "home_unreachable", "ms": 0}, "curfew",
                                       "home_unreachable")
            return raw
        with self._lock:
            _, pid = self._binding_view()
            motor = self._state.latest(pid) if pid is not None else None
            tile = tuple(getattr(motor, "tile", None) or ())
            entry = next((e for e in getattr(snap, "approach_targets", ()) or () if isinstance(e, dict)
                          and e.get("kind") == ib.KIND_BED and tuple(e.get("tile") or ()) == tile), None)
            if entry is None:
                return None
            stage = goal.bed_stage = goal.bed_stage or {"steps": 0, "faces": 0}
            stage["steps"] += 1
            if stage["steps"] > self.BED_STAGE_STEPS_MAX:
                return None
            through = getattr(snap, "move_through_target", None)
            if isinstance(through, dict) and through.get("kind") == ib.KIND_BED:
                choice, label = {"kind": "act", "operation": "move_through", "target_ref": None, "args": {},
                                 "evidence_refs": []}, "move_through"
            else:
                try:
                    bx, by = (int(v) for v in str(entry["ref"]).rsplit("@", 1)[0].rsplit(":", 1)[1].split(","))
                except (ValueError, IndexError):
                    return None
                # the sleep row is the bed tile's row + 1 (ib.bed_row_tiles); the stand is on that row, beside the bed
                need = 1 if tile[0] < bx else 3 if tile[1] == by + 1 else None
                if need is None or getattr(motor, "facing", None) == need or stage["faces"] >= 2:
                    return None
                stage["faces"] += 1
                choice, label = {"kind": "act", "operation": "world_key.move_cardinal", "target_ref": None,
                                 "args": {"direction": ("up", "right", "down", "left")[need], "ticks": 1},
                                 "evidence_refs": []}, "face_bed_row"
            self._journal({"ev": "routine_bed_stage", "goal_id": goal.goal_id, "status": label, "n": stage["steps"],
                           "stand": list(tile)})
            activity = {"activity": "objective step (routine_bed)", "purpose": "the owner's objective", "target": None,
                        "next_step": choice["operation"]}
            return _json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                "speech": "", "choice": choice, "activity": activity}, ensure_ascii=False)

    EAT_STAGE_STEPS_MAX = 12

    def _routine_eat_stage_invoke(self, request):
        """FIXB20 D-B17-1 (live V2B17EAT-c/d: `makan` with energy below max: no eat site / eat target was ever offered, the
        model selected the Parsnip and searched for "an empty floor tile" -> HELD). On the v2 pad path an open eat goal is
        driven without a model turn: the eat target offered -> wait (process_routine_eat presses it and answers Yes) ->
        else select the runtime's food -> approach the mod's eat site -> else turn (1-tick) through the facings until the
        mod captures the eat target; bounded, then `eat_site_not_found` (blocked, partial)."""
        import json as _json
        with self._lock:
            goal = self._active
            objective = getattr(goal, "routine_objective", None) if goal is not None else None
            if (objective is None or objective.kind != ib.OBJ_EAT or not ib.objective_open(objective) or objective.opened
                    or request.goal_id != goal.goal_id or goal.progress_marker == "terminal"
                    or goal.play_mode != "local_activity" or request.reason == "owner_turn"):
                return None
            snap = request.snapshot
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            if not self._v2_pad_path(pin):
                return None
            if self._eat_full_fresh() is not None and not objective.eat_force:
                return None  # the routine_eat_rule finish records `already_full`
            _, pid = self._binding_view()
            rec = self._obs.get(pid, "ui.context") if pid is not None else None
            # FIXB22 D-B19-3 (live V2B19EAT-a/b: `no_food_candidate fresh=true` with the Parsnip published as an eat
            # candidate): the observation store's facts are a read-only MappingProxyType, never a dict -> the stage read
            # an empty dict. Any Mapping now.
            facts = rec.facts if rec is not None and isinstance(getattr(rec, "facts", None), Mapping) else {}
            stage = goal.eat_stage = goal.eat_stage or {"steps": 0, "faces": []}
            stage["steps"] += 1
            if stage["steps"] > self.EAT_STAGE_STEPS_MAX:
                objective.blocker = "eat_site_not_found"
                return None

            def rule(choice, label):
                self._journal({"ev": "routine_eat_stage", "goal_id": goal.goal_id, "status": label, "n": stage["steps"]})
                activity = ({"activity": "objective step (routine_eat)", "purpose": "the owner's objective",
                             "target": choice.get("target_ref"), "next_step": str(choice.get("operation"))}
                            if choice.get("kind") == "act" else None)
                return _json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                    "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                    "speech": "", "choice": choice, "activity": activity}, ensure_ascii=False)
            target = getattr(snap, "world_interaction_target", None)
            if isinstance(target, Mapping) and target.get("kind") == ib.KIND_EAT:
                return rule({"kind": "wait", "reason": "routine_eat_rule", "ms": 0}, "eat_target_offered")
            food = ib.choose_eat_candidate(facts)
            published = facts.get("eat_candidates")
            if food is None and isinstance(published, (list, tuple)) and published:
                # FIXB22 decision: the mod publishes only cheap, non-artisan, non-quest food; a published candidate the
                # runtime still refuses is a POLICY refusal (price above MAX_EAT_PRICE, malformed), named as such
                objective.blocker = "no_eligible_food"
                self._journal({"ev": "routine_eat_stage", "goal_id": goal.goal_id, "status": "no_eligible_food",
                               "policy": "price<=%d" % ib.MAX_EAT_PRICE,
                               "published": [str((c or {}).get("qualified_item_id")) for c in published][:4]})
                return None
            import arti_stardew_combat as _combat
            # FIXB26 review: never open the inventory menu for food while a combat objective is open (the heal hand-off
            # eats from the toolbar; the weapon stage owns the menu)
            fighting = _combat.combat_open(getattr(goal, "combat", None))
            backpack = ib.choose_backpack_eat_candidate(facts) if food is None and not fighting else None
            if backpack is not None and not stage.get("backpack_food"):
                # FIXB26 (live batch 23: Parsnips only in the backpack -> `no_food_candidate`): the locked item_to_toolbar
                # stage (the same one as seeds / the rod / `pegang`) moves the cheapest backpack food into the toolbar
                # once; the next decision selects and eats it.
                stage["backpack_food"] = backpack["qualified_item_id"]
                goal.item_stage_want = backpack["qualified_item_id"]
                self._stage_end_resume("item_to_toolbar", goal, force=True)  # FIXB28 D-B25-1: its end resumes this eat stage
                self._journal({"ev": "routine_eat_stage", "goal_id": goal.goal_id, "status": "food_to_toolbar",
                               "slot": backpack["slot"], "qid": backpack["qualified_item_id"]})
                return rule({"kind": "wait", "reason": "item_to_toolbar", "ms": 0}, "food_to_toolbar")
            if food is None:
                # review: only a fresh (<= 2 s, same-session) record, or two reads, may say there is no food
                fresh = (rec is not None and getattr(rec, "authoritative", True) and pin is not None
                         and getattr(rec, "session_id", None) == pin.observation_session_id
                         and 0 <= time.monotonic() - getattr(rec, "received_monotonic", -1e9) <= 2)
                stage["no_food_reads"] = stage.get("no_food_reads", 0) + 1
                if fresh or stage["no_food_reads"] >= 2:
                    objective.blocker = "no_food_candidate"
                    self._journal({"ev": "routine_eat_stage", "goal_id": goal.goal_id, "status": "no_food_candidate",
                                   "fresh": fresh})
                return None
            toolbar = facts.get("toolbar") if isinstance(facts.get("toolbar"), Mapping) else {}
            if toolbar.get("active_slot") != food["slot"]:
                item = next((i for i in getattr(snap, "selectable_items", ()) or () if isinstance(i, dict)
                             and i.get("slot") == food["slot"]), None)
                if item is None or not getattr(snap, "select_item_available", False):
                    return None
                return rule({"kind": "act", "operation": "select_item", "target_ref": None, "args": dict(item),
                             "evidence_refs": []}, "select_food")
            progress = goal.approach_progress or {}
            site = next((e for e in getattr(snap, "approach_targets", ()) or () if isinstance(e, dict)
                         and e.get("kind") == "eat_site"), None)
            if site is not None and not (progress.get("status") == "ARRIVED" and progress.get("target_ref") == site["ref"]):
                return rule({"kind": "act", "operation": "approach_target", "target_ref": site["ref"], "args": {},
                             "evidence_refs": []}, "approach_eat_site")
            motor = self._state.latest(pid) if pid is not None else None
            facing = getattr(motor, "facing", None)
            tile = getattr(motor, "tile", None)
            grid = self._nav_grid(pin) if pin is not None else None
            if tile is not None and stage.get("faces_tile") != tuple(tile):
                if "faces_tile" in stage:
                    stage["faces"] = []
                stage["faces_tile"] = tuple(tile)
            destination = stage.get("relocate_to")
            if destination is not None and tuple(tile or ()) == destination:
                stage.pop("relocate_to")
                stage["faces"] = []
            elif destination is not None:
                from arti_stardew_goal_routes import open_paths, open_path
                path = open_path(open_paths(grid, tile), destination) if grid and tile else None
                if path and len(path) > 1:
                    dx, dy = path[1][0]-tile[0], path[1][1]-tile[1]
                    direction = {(0,-1): "up", (1,0): "right", (0,1): "down", (-1,0): "left"}[(dx,dy)]
                    return rule({"kind": "act", "operation": "world_key.move_cardinal", "target_ref": None,
                                 "args": {"direction": direction, "ticks": 8}, "evidence_refs": []},
                                "walk_for_eat_site")
                objective.blocker = "eat_site_not_found"
                return None
            if type(facing) is int and facing not in stage["faces"]:
                stage["faces"].append(facing)
            if grid and tile is not None:
                # review: never turn toward a warp / water / bed sleep-row cell (a 1-tick press could step onto it)
                for d, (dx, dy) in enumerate(((0, -1), (1, 0), (0, 1), (-1, 0))):
                    if d not in stage["faces"] and grid.get((tile[0] + dx, tile[1] + dy)) in ("x", "w", "z"):
                        stage["faces"].append(d)
            nxt = next((d for d in (0, 1, 2, 3) if d not in stage["faces"]), None)
            if nxt is None:
                rejected = stage.setdefault("rejected_stands", [])
                if tile is not None and tuple(tile) not in rejected:
                    rejected.append(tuple(tile))
                candidate = (ib.nearest_eat_stand(grid, tile, self._routine_scene_facts(pin), excluded=rejected)
                             if grid and tile is not None else None)
                if candidate is not None:
                    path, _ = candidate
                    stage["relocate_to"] = path[-1]
                    dx, dy = path[1][0]-tile[0], path[1][1]-tile[1]
                    direction = {(0,-1): "up", (1,0): "right", (0,1): "down", (-1,0): "left"}[(dx,dy)]
                    return rule({"kind": "act", "operation": "world_key.move_cardinal", "target_ref": None,
                                 "args": {"direction": direction, "ticks": 8}, "evidence_refs": []},
                                "walk_for_eat_site")
                objective.blocker = "eat_site_not_found"
                self._journal({"ev": "routine_eat_stage", "goal_id": goal.goal_id, "status": "eat_site_not_found",
                               "faces": list(stage["faces"])})
                return None
            stage["faces"].append(nxt)
            return rule({"kind": "act", "operation": "world_key.move_cardinal", "target_ref": None,
                         "args": {"direction": ("up", "right", "down", "left")[nxt], "ticks": 1}, "evidence_refs": []},
                        "turn_for_eat_site")

    def routine_eat_finish_due(self) -> bool:
        """Caller holds the lock (runner idle check): FIXB18 D-B15a-2: a verified eat effect (or an already-full start)
        awaits the worker's routine_eat_rule finish."""
        try:
            goal = self._active
            objective = getattr(goal, "routine_objective", None) if goal is not None else None
            if (objective is None or objective.kind != ib.OBJ_EAT or objective.finish_recorded
                    or goal.progress_marker == "terminal"):
                return False
            if objective.eat_effect or (objective.blocker in ("eat_site_not_found", "no_food_candidate", "no_eligible_food")
                                        and not objective.opened):
                return True
            if not ib.objective_open(objective) or objective.opened or objective.eat_force:
                return False
            return self._eat_full_fresh() is not None
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def _routine_eat_rule_finish(self):
        """FIXB18 D-B15a-2 (live V2B15aACC6b: `EatAnswered consumed true`, energy / health already full (gain 0), no
        finish -> HELD). The runtime records the eat goal's finish itself (origin routine_eat_rule), like the sleep rule:
        achieved `observed_eat_effect` on the verified effect (the item count down by one is enough; `no_gain` when
        nothing rose); before anything was eaten, with energy and health already full (mod `eat_vitals`) and the owner
        not asking to eat anyway, achieved `already_full` without consuming food. Waits while anything is in flight."""
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        with self._lock:
            goal = self._active
            objective = getattr(goal, "routine_objective", None) if goal is not None else None
            if (objective is None or objective.kind != ib.OBJ_EAT or objective.finish_recorded
                    or goal.progress_marker == "terminal" or goal.play_mode != "local_activity"):
                return None
            if self._action_busy or self._inflight_generation is not None or self._results_pending:
                return None
            if objective.blocker in ("eat_site_not_found", "no_food_candidate", "no_eligible_food") and not objective.opened:
                # FIXB20 D-B17-1: the eat stage ran out of facings / food: partial, never an inspect loop
                verdict = FinishEvidenceView("partial", objective.blocker, {"objective": ib.OBJ_EAT,
                                             "origin": "routine_eat_rule", "consumed": False})
                speech = ("Aku tidak menemukan tempat untuk makan di sini." if objective.blocker == "eat_site_not_found"
                          else "Makanan yang ada terlalu mahal untuk kumakan." if objective.blocker == "no_eligible_food"
                          else "Aku tidak punya makanan yang bisa kumakan.")
            elif objective.eat_effect and objective.eat_effect.get("consumed") is True:
                effect = dict(objective.eat_effect)
                no_gain = not (effect.get("energy_gain") or 0) > 0 and not (effect.get("health_gain") or 0) > 0
                verdict = FinishEvidenceView("achieved", ib.FINISH_EAT, {"objective": ib.OBJ_EAT,
                                             "origin": "routine_eat_rule", **effect, "no_gain": no_gain})
                speech = "Sudah kumakan." + (" Tenaga dan darahku memang sudah penuh." if no_gain else "")
            elif ib.objective_open(objective) and not objective.opened and not objective.eat_force:
                full = self._eat_full_fresh()
                if full is None:
                    return None
                verdict = FinishEvidenceView("achieved", "already_full", {"objective": ib.OBJ_EAT,
                                             "origin": "routine_eat_rule", "consumed": False, **full})
                speech = "Tenaga dan darahku sudah penuh, jadi aku tidak makan dulu."
            else:
                return None
            objective.finish_recorded = True
            goal.last_finish = verdict
            goal.progress_marker = "terminal"
            goal.revision += 1
            self._pending_continuation = None
            self._pending_event_reason = None
            self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                           "verdict": asdict(verdict), "origin": "routine_eat_rule"})
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish", speech=StatusSpeech(speech),
                               finish_evidence=verdict)

    def _routine_through_offered(self, goal, target, *, gathering: bool) -> bool:
        return ib.move_through_offered(target, gathering=gathering, objective=getattr(goal, "routine_objective", None))

    def _routine_selectable(self, items: tuple, ui_facts) -> tuple:
        goal = self._active
        if getattr(goal, "food_reselect_due", False):
            return tuple(t for t in items if str(t.get("qualified_item_id", "")).startswith("(T)"))
        return ib.filter_selectable(items, getattr(goal, "routine_objective", None), ui_facts)

    # ------------------------------------------------------------------ single step (inside _execute_leased_action)
    def _routine_before(self, request, args: dict, kind: str) -> dict:
        """Baseline captured before the press (raises ValueError before any write when unreadable)."""
        pid = request.snapshot.binding.player_id
        if kind != ib.KIND_EAT:
            return {}
        rec = self._obs.get(pid, "ui.context")
        parts = str(args.get("source_fact_id") or "").split(":")
        slot = int(parts[1]) if len(parts) >= 3 and parts[0] == "inv" and parts[1].isdigit() else None
        candidates = rec.facts.get("eat_candidates") if rec is not None else None
        qid = next((c.get("qualified_item_id") for c in (candidates or []) if isinstance(c, dict)
                    and c.get("slot") == slot), None)
        inventory = self._obs.get(pid, "progress.inventory")
        count = interact.inventory_count(inventory, qid) if isinstance(qid, str) else None
        motor = self._state.latest(pid)
        if count is None or motor is None:
            raise ValueError("eat_baseline_unavailable")
        return {"eat": {"qid": qid, "count": count, "energy": float(motor.energy), "health": int(motor.health),
                        "inventory_revision": inventory.domain_revision}}

    @W.span('settle_readback', 100, runtime=True)
    def _routine_open_readback(self, request, args: dict, envelope: dict, kind: str, before: dict) -> dict | None:
        """Strictly the pinned receiver: ui.context DialogueBox with the result receiver at generation pin+1,
        the NPC's speaker / the allowlisted question key, and the actor where the kind expects it."""
        pin = request.snapshot.water_context
        receiver, generation = envelope.get("result_receiver_token"), envelope.get("result_context_generation")
        if (pin is None or not isinstance(receiver, str) or not receiver.startswith("rcv:")
                or type(generation) is not int or generation != pin.context_generation + 1):
            self._journal({"ev": "move_readback_unavailable", "generation_id": request.generation_id,
                           "branch": "routine_receiver_envelope", "witness": False})
            return None
        speaker_expected = None
        if kind == ib.KIND_NPC:
            fact = self._routine_scene_facts(pin).get(args.get("source_fact_id"))
            name = fact.get("display_name") if isinstance(fact, dict) else None
            speaker_expected = name if isinstance(name, str) and name else None
        question = ib.QUESTION_FOR_KIND.get(kind)
        expected_tile = ((args["tile_x"], args["tile_y"]) if kind == ib.KIND_BED
                         else tuple(before.get("tile") or ()) or None)
        deadline = game_now(self) + ib.OPEN_READBACK_S
        branch = "pending"
        while game_now(self) < deadline and not self._worker_stop.is_set():
            rec = self._obs.get(pin.player_id, "ui.context")
            motor = self._state.latest(pin.player_id)
            facts = rec.facts if rec is not None else {}
            interactive = facts.get("interactive") if isinstance(facts.get("interactive"), dict) else {}
            block = facts.get("ui_key") if isinstance(facts.get("ui_key"), dict) else None
            speaker = interactive.get("speaker")
            for branch, failed in (
                    ("ui_context_missing", rec is None or not rec.is_complete or not rec.authoritative
                        or not 0 <= time.monotonic() - rec.received_monotonic <= 2),
                    ("ui_context_identity", rec is not None and (rec.session_id != pin.observation_session_id
                        or facts.get("runtime_instance_id") != pin.runtime_instance_id
                        or facts.get("session_id") != pin.command_session_id
                        or facts.get("player_id") != pin.player_id or facts.get("screen_id") != pin.screen_id
                        or facts.get("location_id") != pin.location)),
                    ("receiver_generation", facts.get("context_generation") != generation),
                    ("receiver_identity", interactive.get("receiver_token") != receiver
                        or interactive.get("menu_type") != "DialogueBox"),
                    # The scene display name is bounded (SceneWireLimits.DisplayNameMax); compare on that bound.
                    ("speaker", kind == ib.KIND_NPC and (not isinstance(speaker, str) or not speaker
                        or (speaker_expected is not None and speaker != speaker_expected
                            and not (len(speaker_expected) >= 20 and speaker.startswith(speaker_expected))))),
                    ("question_key", question is not None and (block is None or block.get("receiver_token") != receiver
                        or block.get("question_key") != question)),
                    ("actor_tile", motor is None or expected_tile is None or tuple(motor.tile) != tuple(expected_tile))):
                if failed:
                    break
            else:
                return {"receiver_token": receiver, "context_generation": generation, "menu_type": "DialogueBox",
                        "speaker": speaker if kind == ib.KIND_NPC else None, "question_key": question,
                        "tile": list(motor.tile), "revisi": motor.revisi}
            W.call('poll', 103, time.sleep, 0.05)
        self._journal({"ev": "move_readback_unavailable", "generation_id": request.generation_id,
                       "branch": "routine_" + branch, "witness": False})
        return None

    def _routine_step_readback(self, request, args, result, evidence, before, kind, action_context, folder):
        """world_effect_status for one npc / eat press or bed walk (called from _execute_leased_action)."""
        envelope = result.mentah if result is not None and result.mentah else {}
        if (evidence.lifecycle == "completed" and evidence.world_effect_status == "unverified"
                and evidence.outcome == ib.OUTCOME_FOR_KIND[kind] and result.sukses is True):
            try:
                after = self._routine_open_readback(request, args, envelope, kind, before)
            except Exception:  # noqa: BLE001 — a read failure is never success
                after = None
            evidence = replace(evidence, after=after,
                               world_effect_status=ib.EFFECT_FOR_KIND[kind] if after else "uncertain")
        elif evidence.lifecycle in {"completed", "rejected"}:
            # NoEffect / TARGET_GONE / a bed walk that crossed nothing: the ordinary move readback.
            try:
                after = self._move_readback(request, before, folder=folder, context=action_context)
            except Exception:  # noqa: BLE001
                after = None
            changes = tuple(k for k in ("tile", "pixel", "facing", "location")
                            if after is not None and after[k] != before[k])
            if kind == ib.KIND_BED:
                status = ("observed_change" if changes else "unchanged") if after else "uncertain"
            else:
                status = (interact.EFFECT_NONE if evidence.lifecycle == "completed" else "unchanged") \
                    if after is not None and not changes else "uncertain"
            evidence = replace(evidence, after=after, observed_changes=changes, world_effect_status=status)
        entry = {"ev": "interaction_effect", "kind": kind, "outcome": evidence.outcome,
                 "world_effect_status": evidence.world_effect_status}
        if kind == ib.KIND_NPC and evidence.after:
            entry["speaker"] = evidence.after.get("speaker")
        self._journal(entry)
        return evidence, envelope.get("cleanup_confirmed") is True

    # ------------------------------------------------------------------ chain
    def _execute_routine_chain(self, decision, request, goal, *, kind: str):
        """One admitted npc / eat press or bed walk, then its bounded deterministic menu follow-through."""
        through = kind == ib.KIND_BED
        result = self._execute_leased_action(decision, request, goal, world_interact=not through,
                                             move_through=through, hold=True)
        evidence = result.action_evidence
        steps: list[str] = []
        stop = "not_opened"
        opened = (not result.discarded and evidence is not None and evidence.lifecycle == "completed"
                  and evidence.cleanup_confirmed and evidence.world_effect_status in ib.OPEN_EFFECTS)
        native_npc = None
        native_speaker = None
        if not result.discarded and kind == ib.KIND_NPC and decision.choice.args.get("kind") == "generic_action":
            from arti_stardew_generic_world import opened_menu
            menu = opened_menu(evidence)
            view = self._ui_key_view(request.snapshot.binding.player_id)
            rec, _ = self._ui_key_record(request.snapshot.binding.player_id)
            interactive = (rec.facts.get("interactive") or {}) if rec is not None else {}
            native_speaker = interactive.get("speaker")
            target = request.snapshot.world_interaction_target or {}
            fact = self._routine_scene_facts(request.snapshot.water_context).get(target.get("source_fact_id")) or {}
            expected_speaker = fact.get("display_name") or ib.npc_name_from_fact_id(target.get("source_fact_id"))
            if (menu and menu["type"] == "DialogueBox" and view is not None
                    and view.receiver_token == menu["receiver"] and view.kind in ("dialogue", "npc_question")
                    and isinstance(native_speaker, str) and expected_speaker
                    and native_speaker.casefold() == expected_speaker.casefold()):
                opened = True
                native_npc = request.snapshot.world_interaction_target
        objective = getattr(goal, "routine_objective", None) if goal is not None else None
        if opened:
            if objective is not None:
                with self._lock:
                    objective.opened = True
                    objective.target_ref = (native_npc or decision.choice.args).get("source_fact_id")
                    objective.speaker = native_speaker if native_npc else (evidence.after or {}).get("speaker")
                    self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "kind": objective.kind,
                                   "opened": True, "effect": evidence.world_effect_status,
                                   "speaker": objective.speaker})
            try:
                stop, steps = self._routine_follow_through(goal, kind, request, evidence)
            except Exception as exc:  # noqa: BLE001 — any doubt ends the chain; every step already settled
                stop = "exception:" + type(exc).__name__
        if stop == ib.SLEEP_NOT_STARTED and objective is not None and objective.kind == ib.OBJ_SLEEP:
            self._routine_sleep_not_started(goal, objective, request)
        with self._lock:
            if objective is not None and stop not in ("dialogue_closed", "eat_effect_observed", "waiting_for_host",
                                                      "not_opened"):
                objective.stop = objective.stop or stop
            self._journal({"ev": "routine_chain", "generation_id": request.generation_id, "kind": kind,
                           "steps": steps, "stop": stop})
        self._settle_held_action(decision, request, result, leased=True)
        if opened:
            with self._lock:
                pending = self._pending_continuation is not None
            if not pending and not self._routine_holds_continuation():
                self._resume_after_pause(reason="routine_chain_settled")
        return result

    def _routine_sleep_not_started(self, goal, objective, request) -> None:
        """fix54H (a39 Sp13 D0009:J946-J955): the Yes closed the Sleep question but no native ReadyCheck opened.
        A named fault, never ``bed_waiting_for_host``. The answer is withdrawn so the bed stays her affordance (step
        off, touch it again, by her own choice); nothing is pressed for her and the safety reserve is unchanged."""
        pid = request.snapshot.binding.player_id
        motor = self._state.latest(pid) if pid is not None else None
        rec, _ = self._ui_key_record(pid)
        facts = rec.facts if rec is not None else {}
        readiness = facts.get("ready_check") if isinstance(facts.get("ready_check"), dict) else None
        with self._lock:
            objective.answered = False
            objective.opened = False
            objective.waiting_since = objective.wait_menu = None
            objective.stop = ib.SLEEP_NOT_STARTED
            self._journal({"ev": "FAULT", "type": "sleep_not_started", "goal_id": goal.goal_id,
                           "observed": {"menu": ib._menu_type(facts), "ready_check": readiness,
                                        "active_menu": getattr(motor, "active_menu", None),
                                        "is_player_free": getattr(motor, "is_player_free", None),
                                        "tile": list(getattr(motor, "tile", None) or ()) or None,
                                        "time_of_day": getattr(motor, "time_of_day", None)},
                           "disposition": "RECOVER", "outcome_class": "UNCERTAIN", "verified": False,
                           "note": "Yes closed the Sleep question without a ReadyCheck; the bed is offered again"})
            session = getattr(self, "_skill_session", None)
            current = getattr(session, "current", None)
            if (current is not None and getattr(current.call, "name", None) == "tidur"
                    and getattr(current, "goal_id", None) == goal.goal_id):
                current.pending = ("fail", ib.SLEEP_NOT_STARTED)

    def _routine_follow_through(self, goal, kind: str, request, evidence) -> tuple[str, list[str]]:
        objective = getattr(goal, "routine_objective", None)
        if objective is None and kind != ib.KIND_NPC:
            # Without the owner's eat / sleep objective the runtime never answers a question itself.
            return "no_objective_question_left_to_model", []
        state = objective if objective is not None else ib.RoutineObjective(ib.OBJ_TALK)
        pid = request.snapshot.binding.player_id
        max_steps = ib.TALK_MAX_UI_STEPS if kind == ib.KIND_NPC else ib.ANSWER_MAX_UI_STEPS
        deadline = game_now(self) + ib.FOLLOW_TOTAL_S
        waiting_since = game_now(self)
        steps: list[str] = []
        closed_receiver = None
        yes_revision = None
        while True:
            if self._worker_stop.is_set() or self._action_cancel.is_set():
                return "cancelled", steps
            now = game_now(self)
            if now >= deadline:
                return "follow_deadline", steps
            rec, _ = self._ui_key_record(pid)
            view = self._ui_key_view(pid)
            if rec is None:
                step = ib.FollowStep("wait", "ui_context_unavailable")
            else:
                try:
                    step = ib.plan_follow_step(kind, view, rec.facts, state, closed_receiver=closed_receiver)
                except Exception as exc:  # noqa: BLE001 — a published block that cannot be copied
                    step = ib.FollowStep("stop", "args_unavailable:" + type(exc).__name__)
            if step.kind == "wait" and kind == ib.KIND_BED and state.answered:
                # fix55C / ADR 78 (REVIEW2 #1): after her Yes any wait (no menu, ui.context gap) first reads the native night.
                started, free_same_day = self._routine_sleep_signal(pid, state, yes_revision)
                if started is not None:
                    step = ib.FollowStep("done", started)
                elif (step.reason == ib.SLEEP_NOT_STARTED and free_same_day
                      and now - waiting_since >= ib.FOLLOW_STEP_WAIT_S):
                    return ib.SLEEP_NOT_STARTED, steps  # 10 s: same date and she is free again (a39 Sp13)
                else:
                    W.call('poll', 107, time.sleep, 0.1)  # no proof either way yet: FOLLOW_TOTAL_S still bounds the chain
                    continue
            if step.kind == "wait":
                if now - waiting_since >= ib.FOLLOW_STEP_WAIT_S:
                    return "step_wait:" + step.reason, steps
                W.call('poll', 106, time.sleep, 0.1)
                continue
            if step.kind == "stop":
                return step.reason, steps
            if step.kind == "done":
                return self._routine_follow_done(goal, kind, step, request, evidence), steps
            if len(steps) >= max_steps:
                return "ui_step_budget", steps
            run = self._run_ui_key(dict(step.args), origin="routine_" + kind)
            steps.append(step.reason)
            with self._lock:
                state.ui_steps += 1
            if run.uncertain or not run.verified:
                return "ui_key_" + ("uncertain" if run.uncertain else run.lifecycle), steps
            # FINAL B4: after a verified close, a record still showing this receiver is its closing transition.
            closed_receiver = step.args["receiver_token"] if run.effect == "closed" else None
            if step.reason == "answer_yes":
                with self._lock:
                    state.answered = True
                    if state.kind == ib.OBJ_SLEEP:
                        _, sleep_pid = self._binding_view()
                        sleep_motor = self._state.latest(sleep_pid)
                        goal.home_route_state["bed_time"] = getattr(sleep_motor, "time_of_day", None)
                        yes_revision = getattr(sleep_motor, "revisi", None)
                    self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "kind": state.kind,
                                   "answered": "Yes", "question": ib.QUESTION_FOR_KIND.get(kind)})
            waiting_since = game_now(self)

    def _routine_sleep_signal(self, pid, objective, yes_revision) -> tuple[str | None, bool]:
        """fix55C / ADR 78 (REVIEW2 #1): (night-started reason or None, free on the same date) after her bed Yes.

        Since 870e62a the HOST readies the sleep check on its next tick, so the ReadyCheckDialog can close between
        0.1 s polls; the night is then read from the native date, the night-flow menu, or the night fade."""
        motor = self._state.latest(pid) if pid is not None else None
        if motor is None or self._state.is_stale(pid):
            return None, False
        date = self._date_tuple(motor)
        revisi = getattr(motor, "revisi", None)
        after_yes = type(revisi) is int and type(yes_revision) is int and revisi > yes_revision
        started = ib.sleep_signal(objective.start_date, date, getattr(motor, "active_menu", None),
                                  getattr(motor, "is_player_free", None), after_yes=after_yes)
        same_day = objective.start_date is None or date == objective.start_date
        return started, (started is None and getattr(motor, "is_player_free", None) is True and same_day
                         and getattr(motor, "active_menu", None) is None)

    def _routine_follow_done(self, goal, kind: str, step, request, evidence) -> str:
        objective = getattr(goal, "routine_objective", None)
        pid = request.snapshot.binding.player_id
        if kind == ib.KIND_NPC:
            talked = None
            fact = self._routine_scene_facts(request.snapshot.water_context).get(
                (evidence.before or {}).get("source_fact_id") or (objective.target_ref if objective else None))
            if isinstance(fact, dict):
                talked = fact.get("talked_today") is True
            with self._lock:
                if objective is not None:
                    objective.closed = True
                self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "kind": ib.OBJ_TALK,
                               "dialogue_closed": True, "talked_today_observed": talked})
            return "dialogue_closed"
        if kind == ib.KIND_EAT:
            baseline = (evidence.before or {}).get("eat")
            effect = self._routine_eat_readback(pid, baseline) if baseline else None
            with self._lock:
                if objective is not None and effect:
                    objective.eat_effect = effect
                self._journal({"ev": "interaction_effect", "kind": ib.KIND_EAT, "outcome": "EatAnswered",
                               "world_effect_status": ib.FINISH_EAT if effect else "uncertain", **(effect or {})})
            return "eat_effect_observed" if effect else "eat_effect_not_observed"
        rec, _ = self._ui_key_record(pid)
        menu = ((rec.facts.get("interactive") or {}).get("menu_type") if rec is not None
                and isinstance(rec.facts.get("interactive"), dict) else None)
        with self._lock:
            if objective is not None:
                objective.waiting_since = game_now(self)
                objective.wait_menu = menu
            self._journal({"ev": "bed_waiting_for_host", "goal_id": goal.goal_id, "menu_type": menu,
                           "started_by": step.reason,
                           "note": "farmhand asleep first: ReadyCheckDialog waits for every player; the HOST passes out at 02:00"})
        return "waiting_for_host"

    @W.span('settle_readback', 101, runtime=True)
    def _routine_eat_readback(self, pid, baseline: dict) -> dict | None:
        deadline = game_now(self) + ib.EAT_READBACK_S
        found, settle_until = None, None
        while game_now(self) < deadline and not self._worker_stop.is_set():
            inventory = self._obs.get(pid, "progress.inventory")
            motor = self._state.latest(pid)
            count = interact.inventory_count(inventory, baseline["qid"])
            if (count is not None and motor is not None
                    and inventory.domain_revision > baseline["inventory_revision"]):
                effect = ib.eat_effect(baseline, {"count": count, "energy": float(motor.energy),
                                                  "health": int(motor.health)})
                if effect:
                    if (effect.get("energy_gain") or 0) > 0 or (effect.get("health_gain") or 0) > 0:
                        return effect
                    # FIXB24 D-B21-1: consumed, vitals not applied yet (end of the eating animation): settle first
                    found = effect
                    if settle_until is None:
                        settle_until = min(deadline, game_now(self) + ib.EAT_VITALS_SETTLE_S)
            if settle_until is not None and game_now(self) >= settle_until:
                return found
            W.call('poll', 104, time.sleep, 0.1)
        return found

    def _routine_question_offerable(self, view) -> bool:
        """WS2 model offer: the Sleep question (it ends the day) only under the owner's sleep objective."""
        if view is None or getattr(view, "question_key", None) != "Sleep":
            return True
        goal = self._active
        objective = getattr(goal, "routine_objective", None) if goal is not None else None
        return ib.objective_open(objective) and objective.kind == ib.OBJ_SLEEP

    def _routine_extra_entries(self, pin, motor_tile, scene_facts, *, stale_age: float | None) -> list[dict]:
        """WS3b approach entries: talk NPCs / bed sides (scene), the eat site (ui.context). Journals why a
        talk objective has no NPC entry (D28) and every newly offered eat site (D25)."""
        goal = self._active
        objective = getattr(goal, "routine_objective", None) if goal is not None else None
        if not ib.objective_open(objective):
            return []
        # R S2:2404: every observed villager was outside the legacy 12-tile
        # talk radius. Discovery is not interaction permission: Live7 may walk
        # toward a current NPC through the existing bounded navigation gates.
        reach = None if getattr(self, "_live7_active", lambda: False)() else ib.NPC_REACH
        entries = ib.approach_entries(scene_facts or {}, motor_tile, objective, npc_reach=reach) if scene_facts is not None else []
        if objective.kind == ib.OBJ_TALK and not objective.opened and not any(e["kind"] == ib.KIND_NPC for e in entries):
            skips = ib.npc_entry_skips(scene_facts or {}, motor_tile, objective, npc_reach=reach)
            if scene_facts is None:
                skips = [{"npc": None, "reason": "scene_unavailable" if stale_age is None else "scene_stale"}]
            record = {"ev": "npc_entry_skipped", "goal_id": goal.goal_id, "npc": objective.npc_name,
                      "skipped": skips[:16], "scene_age_s": None if stale_age is None else round(stale_age, 1)}
            key = json_key(record)
            if key != getattr(self, "_last_npc_skip_key", None):
                self._last_npc_skip_key = key
                self._journal(record)
        if objective.kind == ib.OBJ_EAT and pin is not None:
            rec = self._obs.get(pin.player_id, "ui.context")
            ui_facts = rec.facts if rec is not None else None
            target = (ui_facts or {}).get("world_interaction_target")
            site = ib.eat_site_entry(ui_facts, objective,
                                     eat_target_offered=isinstance(target, dict) and target.get("kind") == ib.KIND_EAT)
            if site is not None:
                entries.append(site)
                if site["ref"] != objective.eat_site_ref:
                    objective.eat_site_ref = site["ref"]
                    self._journal({"ev": "eat_site_offered", "goal_id": goal.goal_id, "site": site["ref"]})
        return entries

    # ------------------------------------------------------------------ bed wait (continuation hold)
    def _routine_holds_continuation(self) -> bool:
        goal = self._active
        objective = getattr(goal, "routine_objective", None) if goal is not None else None
        if objective is None or objective.kind != ib.OBJ_SLEEP:
            return False
        return (not self._owner_play_stopped and not self._worker_stop.is_set()
                and ib.bed_wait_state(objective, date=None) == "waiting")

    # FIXB7 curfew guard: the goal fields of the objectives the curfew aborts (the WS3b sleep objective replaces them).
    _CURFEW_FIELDS = ("extras_objective", "economy_objective", "combat", "charge", "fishing", "tool_work", "menu_objective",
                      "episode_plan")

    def process_curfew(self) -> int:
        """Worker hook (FIXB7 curfew guard; live V2B5b: the shake trial ran past 01:00 and ARTI passed out at 02:00):
        at timeOfDay >= 2500 (01:00) every open non-sleep objective of the active goal is aborted ``curfew`` and the WS3b
        sleep objective is pinned, with the home navigation objective when ARTI is not at home (bed nav). A skills
        session ends its running skill (failed ``curfew``) and continues with pergi_ke pulang (when away) + tidur.
        Once per goal; journaled."""
        try:
            with self._lock:
                goal = self._active
                if (goal is None or goal.progress_marker == "terminal" or getattr(goal, "curfew_applied", False)
                        or goal.play_mode != "local_activity"):
                    return 0  # FIXB7 review: play (local_activity) goals only; direct goals are untouched
                _, pid = self._binding_view()
                motor = self._state.latest(pid) if pid is not None else None
                now = getattr(motor, "time_of_day", None)
                session = getattr(self, "_skill_session", None)
                live = session is not None and session.status == "active" and session.mode == "part_live"
                if self._action_busy or self._inflight_generation is not None:
                    return 0  # finish the current step before any curfew or rollover decision
                reserve_due = False
                if live and session.campaign is not None:
                    if pid is None or self._state.is_stale(pid):
                        return 0
                    if self._live7_guard_day(session, motor, allow_answered_sleep=True) is not None:
                        return 1
                    from arti_stardew_live7 import home_due
                    facts = self._skill_current_facts()
                    reserve_due = facts is not None and home_due(facts, session.campaign.live)
                if not reserve_due and (live or ib.curfew_phase(now) != "hard"):
                    return 0  # D-B6-3: never mid-action / mid-decision (the bump would drop that step's continuation)
                pin = self._water_context(pid)
                if not self._v2_pad_path(pin):
                    return 0  # v2 pad mode only (flag off = unchanged)
                goal.curfew_applied = True
                routine = goal.routine_objective
                session = getattr(self, "_skill_session", None)
                home = self._scene_home_location(pin)
                away = pin is not None and home is not None and pin.location != home
                if session is not None and session.status == "active":
                    return self._curfew_skills_locked(session, goal, now, away)
                if routine is not None and routine.kind == ib.OBJ_SLEEP and ib.objective_open(routine):
                    stage = getattr(goal, "curfew_stage", None) or {}
                    if stage.get("status") in ("home", "bed") or routine.opened:
                        self._journal({"ev": "curfew", "goal_id": goal.goal_id, "time_of_day": now, "aborted": [],
                                       "sleep": "already_pinned", "home_leg": stage.get("status") or "bed_question"})
                        return 0
                    # FIXB27 D-B24-1 (live V2B24NIGHT-b: `tidur` from the Farm at 20:50, the model only inspected for the
                    # off-scene bed, `curfew sleep already_pinned` returned here with no home leg -> pass-out at 02:00):
                    # the already pinned sleep objective gets the same locked home leg + bed stage as the curfew's own.
                    goal.revision += 1
                    self._pending_continuation = None
                    goal.question_basis = None
                    goal.no_progress_until, goal.no_progress_blocked, goal.no_progress_count = None, False, 0
                    nav = self._pin_sleep_home_leg(goal, pin, away, home, origin="curfew")
                    self._journal({"ev": "curfew", "goal_id": goal.goal_id, "time_of_day": now, "aborted": [],
                                   "sleep": "already_pinned", "home_nav": nav, "home_leg": goal.curfew_stage["status"]})
                else:
                    self._curfew_pin_sleep_locked(goal, pin, motor, now, away, home, routine)
            resumed = self._resume_after_pause(reason="curfew")
            self._journal({"ev": "curfew_stage", "goal_id": goal.goal_id, "status": "resumed" if resumed else "resume_refused"})
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "curfew_error", "error": type(exc).__name__})
            return 0

    def _curfew_pin_sleep_locked(self, goal, pin, motor, now, away, home, routine) -> None:
        """Caller holds the lock: the curfew's own path (no open sleep objective yet): abort the open objectives, pin the
        sleep objective, bump the revision first (D-B6-3), then the shared home leg (FIXB27)."""
        goal.curfew_replaced_objective = True
        aborted = []
        for name in self._CURFEW_FIELDS:
            if getattr(goal, name, None) is not None:
                aborted.append(name)
                setattr(goal, name, None)
        if goal.gather_progress is not None or goal.gather_required:
            aborted.append("gather")
            goal.gather_progress, goal.gather_required, goal.gather_store_required = None, False, False
        if routine is not None:
            aborted.append("routine:" + str(routine.kind))
        if goal.interact_objective is not None:
            aborted.append("interact:" + str(goal.interact_objective.kind))
            goal.interact_objective = None
        goal.play_blocker = None
        goal.approach_target = goal.approach_controller = None
        goal.routine_objective = ib.RoutineObjective(ib.OBJ_SLEEP, start_date=self._date_tuple(motor))
        # D-B6-3 (live V2B6G6shake-a: curfew pinned sleep + home_nav, then the pending step was dropped by the
        # revision bump (`L2468:goal_revision`) and no home leg ran): bump FIRST, pin after it, drop the old
        # ticket, then resume with a fresh inspect + continuation at the new revision; the curfew stage
        # (_curfew_stage_invoke) answers every decision deterministically (home legs, then the bed).
        goal.revision += 1
        self._pending_continuation = None
        goal.question_basis = None
        goal.no_progress_until, goal.no_progress_blocked, goal.no_progress_count = None, False, 0
        nav = self._pin_sleep_home_leg(goal, pin, away, home, origin="curfew")
        self._journal({"ev": "curfew", "goal_id": goal.goal_id, "time_of_day": now, "aborted": aborted,
                       "reason": "curfew", "sleep": "pinned", "home_nav": nav})

    def _pin_sleep_home_leg(self, goal, pin, away: bool, home, *, origin: str) -> bool:
        """Caller holds the lock. FIXB27 D-B24-1: the one shared home leg of a sleep objective (the curfew's and an owner /
        soak `tidur`): when ARTI is away from home the home navigation objective (enter_location HOME) is pinned and the
        locked curfew stage runs `home` (pergi_ke pulang legs), then `bed` (tidur: bed approach, move_through, Sleep Yes).
        True when the home nav was pinned (False: already home -> the bed stage only)."""
        nav = None
        if away:
            nav = interact.InteractObjective("enter_location", alias=interact.HOME)
            if interact.resolve_objective(nav, current_location=pin.location, home_location=home) is None:
                goal.interact_objective = nav
                goal.location_basis = pin.location
            else:
                nav = None
        goal.curfew_stage = {"status": "home" if nav is not None else "bed", "origin": origin}
        if away and nav is None:
            # FIXB27 review: away, but the home location does not resolve from here: no home leg exists
            goal.curfew_stage["status"] = "ended"
            self._sleep_home_failed_locked(goal, "home_unresolved")
        return nav is not None

    def _sleep_home_failed_locked(self, goal, why: str) -> None:
        """Caller holds the lock: the sleep objective's home leg failed: mark it for the worker's partial finish."""
        routine = getattr(goal, "routine_objective", None)
        if routine is not None and routine.kind == ib.OBJ_SLEEP and routine.blocker is None:
            routine.blocker = "home_unreachable"
            goal.sleep_home_failed = why
            self._journal({"ev": "sleep_home_leg", "goal_id": goal.goal_id, "status": "home_unreachable", "reason": why})

    def sleep_home_failed_due(self) -> bool:
        """Caller holds the lock (runner idle check): FIXB27 review: a failed home leg awaits the partial finish."""
        try:
            goal = self._active
            routine = getattr(goal, "routine_objective", None) if goal is not None else None
            return bool(routine is not None and routine.kind == ib.OBJ_SLEEP and routine.blocker == "home_unreachable"
                        and not routine.finish_recorded and goal.progress_marker != "terminal")
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def process_sleep_home_failed(self) -> int:
        """Worker hook (FIXB27 review): the sleep goal whose home leg failed ends partial `home_unreachable`."""
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        with self._lock:
            if (not self.sleep_home_failed_due() or self._action_busy or self._inflight_generation is not None
                    or getattr(self, "_results_pending", 0)):
                return 0
            goal = self._active
            routine = goal.routine_objective
            routine.finish_recorded = True
            verdict = FinishEvidenceView("partial", "home_unreachable", {"objective": ib.OBJ_SLEEP, "origin": "curfew_stage",
                                         "reason": getattr(goal, "sleep_home_failed", None)})
            goal.last_finish = verdict
            goal.progress_marker = "terminal"
            goal.revision += 1
            self._pending_continuation = None
            self._pending_event_reason = None
            self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                           "verdict": asdict(verdict), "origin": "curfew_stage"})
        result = OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=False, reason="finish",
                                 speech=StatusSpeech("Aku tidak menemukan jalan pulang ke kasurku."), finish_evidence=verdict)
        if self._on_generation_result is not None:
            self._on_generation_result(SimpleNamespace(reason="routine_sleep_finish", generation_id=None), result)
        return 1

    @staticmethod
    def _curfew_home_path(session, run) -> bool:
        """The run IS the curfew's own resolution: tidur, a home leg (pergi_ke home/pulang/...), or any leg of a
        campaign's sleep row (its travel toward the Farm, home, bed). FINAL Part C J L3629-L3810: the curfew aborted
        the sleep row's BusStop->Farm leg, then refused each of its own `pergi_ke pulang` legs (0 inputs) 18 times
        until all 24 starts were spent."""
        from arti_stardew_skills import LOCATION_WORDS
        campaign = getattr(session, "campaign", None)
        if campaign is not None and getattr(campaign, "task", None) == "sleep":
            return True
        word = str(getattr(run.call, "arg", None) or "").casefold() if run is not None else ""
        return run is not None and (run.call.name == "tidur" or run.call.name == "pergi_ke"
                                    and "pulang" in (word, LOCATION_WORDS.get(word)))

    def _curfew_skills_locked(self, session, goal, now, away) -> int:
        from arti_stardew_skills import SkillCall
        current = session.current
        if (session.mode == "part_live" and session.campaign is not None and ib.curfew_phase(now) != "hard"
                and (facility := home_facility(self, session, current))):
            # ADR 231 part A: the shipment finishes; the reserve still holds (her next page is the way home) and the
            # 01:00 curfew (the body guard) applies to this goal as before.
            session.campaign.live.sleep_only = True
            goal.curfew_applied = False
            if not getattr(goal, "curfew_shipment_noted", False):
                goal.curfew_shipment_noted = True
                self._journal(dict(ev="curfew", goal_id=goal.goal_id, time_of_day=now, reason="curfew",
                                   sleep=facility, skill=current.call.name, arg=current.call.arg))
            return 0
        if (session.mode == "part_live" and session.campaign is not None and ib.curfew_phase(now) != "hard"
                and current is not None and not self._curfew_home_path(session, current) and native_menu_open(self)):
            # ADR 231 part E (attempt 87 W13 d6 J3534-J3568: the reserve failed her chest run at 24:10 while the game's
            # question box was up; the cleanup could not confirm release and the row became UNCERTAIN twice): the menu
            # path answers or closes it first; the reserve holds and fails the run on its next tick. 01:00 does not wait.
            session.campaign.live.sleep_only = True
            goal.curfew_applied = False
            if not getattr(goal, "curfew_menu_noted", False):
                goal.curfew_menu_noted = True
                self._journal(dict(ev="curfew", goal_id=goal.goal_id, time_of_day=now, reason="curfew",
                                   sleep="menu_open_deferred", skill=current.call.name))
            return 0
        if session.mode == "part_live" and session.campaign is not None:
            session.campaign.live.sleep_only = True
            from arti_runtime.mines_exit import intent as mines_escape
            from arti_stardew_skills import MINES_SKILLS
            candidate = current is not None and (current.call == SkillCall("pergi_ke", "mine")
                        or current.call.name in MINES_SKILLS and current.state.get("wave1_driver") == "exit")
            escape = (candidate and mines_escape(
                current, getattr(self._build_decision_snapshot(goal).motor, "location", None)))
            if not self._curfew_home_path(session, current) and current is not None and not escape:
                if current.pending is None and current.call.name == 'pergi_ke' and not current.state.get('route_clear'):
                    request = SimpleNamespace(snapshot=self._build_decision_snapshot(goal))
                    current.pending = self._skill_arrival_before_reserve(current, goal, request)
                if current.call.name != 'pergi_ke' or current.pending is None or current.pending[0] not in ('done', 'fail'):
                    current.pending = ("fail", ib.CURFEW_REASON)
            self._journal(dict(ev="curfew", goal_id=goal.goal_id, time_of_day=now,
                               reason="curfew", sleep="live7_home_sleep"))
            return 1
        if self._curfew_home_path(session, current):
            self._journal({"ev": "curfew", "goal_id": goal.goal_id, "time_of_day": now, "aborted": [], "sleep": "skill_running",
                           **({"skill": current.call.name, "arg": current.call.arg} if current is not None else {})})
            return 0
        tail = ([SkillCall("pergi_ke", "pulang")] if away else []) + [SkillCall("tidur")]
        session.mode = "plan"
        session.plan[session.index:] = tail
        aborted = []
        if current is not None:
            aborted.append("skill:" + current.call.name)
            self._skill_close_run_locked(session, current, ("failed", ib.CURFEW_REASON))
        self._journal({"ev": "curfew", "goal_id": goal.goal_id, "time_of_day": now, "aborted": aborted,
                       "reason": "curfew", "sleep": "skill_plan", "plan": [c.name for c in tail]})
        return 1

    CURFEW_STAGE_MAX = 80

    def _curfew_stage_invoke(self, request):
        """_invoke_provider hook (D-B6-3): while the curfew stage is open, every decision of the goal is the next
        deterministic home leg (pergi_ke pulang = nav_rule) and then the bed (tidur = routine bed walk), from the
        request's own snapshot; no provider decision. A failing step ends the stage (the provider decides again)."""
        import arti_stardew_skills as skills
        pace = 0.0
        with self._lock:
            goal = self._active
            stage = getattr(goal, "curfew_stage", None) if goal is not None else None
            if (stage is None or stage.get("status") not in ("home", "bed") or request.goal_id != goal.goal_id
                    or goal.progress_marker == "terminal"):
                return None
            state = stage.setdefault("state", {})
            stage["steps"] = stage.get("steps", 0) + 1
            step = None
            if stage["status"] == "home":
                nav = goal.interact_objective
                pin = request.snapshot.water_context or request.snapshot.movement_context
                state["nav_objective"] = nav
                step = (skills.Step("done", reason="home") if nav is None or (pin is not None and pin.location == nav.target)
                        else skills.plan_step(skills.SkillCall("pergi_ke", "pulang"), request.snapshot, state=state))
                if step.kind == "decision" and step.choice.get("kind") == "finish" or step.kind == "done":
                    stage["status"] = "bed"  # home: the bed next (never the goal finish)
                    self._journal({"ev": "curfew_stage", "goal_id": goal.goal_id, "status": "home_reached"})
                    step = None
            if step is None:
                step = skills.plan_step(skills.SkillCall("tidur"), request.snapshot, state=state)
                if step.kind == "decision" and step.choice.get("kind") == "finish":
                    step = skills.Step("decision", dict(skills.INSPECT, question="curfew: observe the bed / sleep"))
            if step.kind != "decision" or stage["steps"] > self.CURFEW_STAGE_MAX:
                was = stage["status"]
                stage["status"] = "ended"
                why = step.reason if step.kind != "decision" else "steps_max"
                self._journal({"ev": "curfew_stage", "goal_id": goal.goal_id, "status": "ended", "reason": why})
                if was == "home":
                    # FIXB27 review: a home leg that cannot complete ends the sleep goal partial `home_unreachable` (worker
                    # finish), never a hand-back to the model's inspect loop for an off-scene bed
                    self._sleep_home_failed_locked(goal, why)
                    choice = {"kind": "wait", "reason": "home_unreachable", "ms": 0}
                    return self._rule_json(request, choice, "curfew", "home_unreachable")
                return None
            choice, pace = step.choice, step.pace
            self._journal({"ev": "curfew_stage", "goal_id": goal.goal_id, "status": stage["status"],
                           "kind": choice.get("kind"), "operation": choice.get("operation"), "n": stage["steps"]})
        if pace:
            W.call('bed', 105, time.sleep, pace)
        return self._rule_json(request, choice, "curfew", str(choice.get("operation")))

    def curfew_window_now(self) -> bool:
        """Caller holds the lock: the current timeOfDay is at or past 24:00 (no new extras / economy / combat stage)."""
        goal = self._active
        if goal is None or goal.play_mode != "local_activity":
            return False
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        return (ib.curfew_phase(getattr(motor, "time_of_day", None)) is not None
                and self._v2_pad_path(self._water_context(pid)))

    @W.span('bed', 102, runtime=True)
    def process_routine_wait(self) -> int:
        """Worker hook: end the bed wait at the day change (then the WS2 day-start rule owns the morning)."""
        try:
            with self._lock:
                goal = self._active
                objective = getattr(goal, "routine_objective", None) if goal is not None else None
                if objective is None or objective.kind != ib.OBJ_SLEEP:
                    return 0
                _, pid = self._binding_view()
                date = self._date_tuple(self._state.latest(pid)) if pid is not None else None
                now = game_now(self)
                state = ib.bed_wait_state(objective, date=date)
                if state is None:
                    if not objective.day_changed or objective.finish_recorded:
                        return 0  # nothing to wait for / finish already recorded
                elif state == "waiting":
                    if now - self._routine_wait_journal_at >= _BED_WAIT_JOURNAL_S:
                        self._routine_wait_journal_at = now
                        self._journal({"ev": "bed_waiting_for_host", "goal_id": goal.goal_id,
                                       "waited_s": round(now - objective.waiting_since, 1)})
                    return 0
                else:
                    objective.day_changed = True
                    self._journal({"ev": "bed_wait_ended", "goal_id": goal.goal_id, "reason": state,
                                   "date": list(date) if date else None,
                                   "waited_s": round(now - objective.waiting_since, 1)})
                    self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "kind": ib.OBJ_SLEEP,
                                   "day_changed": True})
            # The WS2 day-start rule owns the morning first (level-up / shipping / mail).
            self._day_start_tick()
            if self._day_start_active:
                return 1
            # D17 (live B3W8a): the day change IS the sleep objective's effect. The runtime records the
            # verified finish itself (no model finish, no motor-basis continuation, no date gate).
            result = self._routine_sleep_finish(goal, objective, pid)
            if result is not None and self._on_generation_result is not None:
                self._on_generation_result(SimpleNamespace(reason="routine_sleep_finish", generation_id=None), result)
            return 1
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            from arti_stardew_diagnostics import exception_record
            self._journal(dict(ev="routine_wait_error", **exception_record(exc, phase="routine_wait")))
            return 0

    def _routine_summary(self, goal, *, world_target=None) -> dict | None:
        """Bounded model-visible view of the WS3b objective (verified progress only; no identities).

        D18 (live B3W7a): the eat goal also names the runtime-chosen food and the next act, so the model
        knows which offered select_item target is food (qualified ids alone did not tell it)."""
        objective = getattr(goal, "routine_objective", None) if goal is not None else None
        if objective is None:
            return None
        status, reason = ib.objective_verdict(objective)
        summary = {"kind": objective.kind, "status": status, "reason": reason}
        if objective.npc_name:
            summary["npc"] = objective.npc_name
        if objective.kind == ib.OBJ_EAT and status != "achieved" and not objective.opened:
            _, pid = self._binding_view()
            rec = self._obs.get(pid, "ui.context") if pid is not None else None
            facts = rec.facts if rec is not None else {}
            food = ib.choose_eat_candidate(facts)
            toolbar = facts.get("toolbar") if isinstance(facts.get("toolbar"), Mapping) else {}
            eat_offered = (world_target or {}).get("kind") == ib.KIND_EAT
            site = ib.eat_site_entry(facts, objective, eat_target_offered=eat_offered)
            summary["active_slot"] = toolbar.get("active_slot")
            summary["next"] = ib.eat_next_step(food, toolbar.get("active_slot"), eat_offered, site is not None)
            if eat_offered and objective.eat_site_ref and not objective.eat_site_adjusted:
                objective.eat_site_adjusted = True
                self._journal({"ev": "eat_site_adjusted", "goal_id": goal.goal_id, "site": objective.eat_site_ref,
                               "eat_target": (world_target or {}).get("source_fact_id")})
            if food is not None:
                summary["food"] = {"slot": food["slot"], "qualified_item_id": food["qualified_item_id"]}
        if objective.kind == ib.OBJ_SLEEP and objective.waiting_since is not None and not objective.day_changed:
            summary["waiting_for_host"] = True
        return summary

    def _routine_sleep_finish(self, goal, objective, pid):
        """Terminal achieved verdict for an answered sleep once a fresh motor frame shows the new date.

        Waits (returns None, retried by the worker) while a decision or action is in flight, so a model
        finish already on its way is never raced; skips when that finish already ended the goal."""
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        with self._lock:
            if (self._active is not goal or goal.progress_marker == "terminal" or objective.finish_recorded
                    or not objective.answered or not objective.day_changed):
                objective.finish_recorded = objective.finish_recorded or goal.progress_marker == "terminal"
                return None
            if self._action_busy or self._inflight_generation is not None or self._results_pending:
                return None
            motor = self._state.latest(pid) if pid is not None else None
            date = self._date_tuple(motor)
            if motor is None or self._state.is_stale(pid) or date is None or date == objective.start_date:
                return None
            from arti_stardew_day_start import native_morning_pending
            ui = self._obs.get(pid, "ui.context")
            if native_morning_pending(getattr(ui, "facts", None), date):
                return None  # retain this answered sleep through farm events/summary/save sync
            verdict = FinishEvidenceView("achieved", ib.FINISH_DAY, {
                "objective": ib.OBJ_SLEEP, "origin": "routine_sleep_rule", "start_date": list(objective.start_date or ()),
                "date": list(date), "location": motor.location, "tile": list(motor.tile), "answered": True,
                "bed_time": goal.home_route_state.get("bed_time")})
            if goal.curfew_replaced_objective:
                verdict = replace(verdict, status="partial", reason="original_objective_aborted_curfew")
            objective.finish_recorded = True
            goal.last_finish = verdict
            goal.progress_marker = "terminal" if verdict.status == "achieved" else verdict.status
            goal.revision += 1
            self._pending_continuation = None
            self._pending_event_reason = None
            self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                           "verdict": asdict(verdict), "origin": "routine_sleep_rule"})
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                               speech=StatusSpeech("Sudah pagi; tidurnya terverifikasi dari tanggal baru."), finish_evidence=verdict)

    def _routine_post_sleep(self, goal) -> bool:
        """The goal's date change is its own verified sleep effect (the date gate must not block it)."""
        objective = getattr(goal, "routine_objective", None) if goal is not None else None
        if objective is None or objective.kind != ib.OBJ_SLEEP or not objective.answered:
            return False
        if objective.day_changed:
            return True
        _, pid = self._binding_view()
        date = self._date_tuple(self._state.latest(pid)) if pid is not None else None
        return ib.bed_wait_state(objective, date=date) == "day_changed"

    # ------------------------------------------------------------------ finish
    def _routine_finish_verdict(self, goal):
        """FinishEvidenceView for a WS3b objective (verified progress only), or None for other goals."""
        objective = getattr(goal, "routine_objective", None) if goal is not None else None
        if objective is None:
            return None
        from arti_stardew_runtime import FinishEvidenceView
        status, reason = ib.objective_verdict(objective)
        if objective.kind == ib.OBJ_SLEEP and status == "achieved" and goal.curfew_replaced_objective:
            status, reason = "partial", "original_objective_aborted_curfew"
        observed = {"objective": objective.kind, "opened": objective.opened, "closed": objective.closed,
                    "answered": objective.answered, "day_changed": objective.day_changed,
                    "eat_effect": objective.eat_effect, "speaker": objective.speaker,
                    "npc": unquote(objective.npc_name) if objective.npc_name else None}
        return FinishEvidenceView(status, reason, observed)
