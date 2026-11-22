"""Primitives v2 WS-G6 runtime side: extras objectives, offers, approach entries, the press + bounded follow-through,
readback and the deterministic finish (``extras_rule``).

Kept out of ``arti_stardew_runtime.py`` (several workstreams edit it): ``StardewGoalRuntime`` inherits
:class:`WorldExtrasRuntimeMixin` and calls a handful of hooks. Every physical step reuses an existing path: the press is
the unchanged single-step leased executor (``_execute_leased_action(..., world_interact=True, hold=True)``, pad A / X
underneath), the question answer is WS2's leased ``ui_key`` (``_run_ui_key``), the elevator floor is WS-G1's leased
``ui_nav`` / ``ui_press`` (``_run_ui_pad``). The runtime never invents a target: press args are the mod's observed
target, menu args are copied from the mod's published blocks, and the only answers it gives itself are the owner
objective's own (shaft ``Jump``, bus ``Yes``, the named minecart destination, the named elevator floor).

Flag: the objective grammar runs only when session.json carries ``world_extras_enabled`` (mod WorldExtrasEnabled on).

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G6 live witnesses.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

import time
from arti_stardew_pause import game_now, wait_resume
from collections.abc import Mapping
from dataclasses import asdict, replace

import arti_stardew_command as command
import arti_stardew_world_extras as extras


def skills_inspect() -> dict:
    import arti_stardew_skills as _skills
    return dict(_skills.INSPECT)

_ARRIVAL_WAIT_S = 12.0
_RECEIVER_READBACK_S = 2.5


from arti_stardew_animal_routine import AnimalRoutineRuntimeMixin



def prefer_own_soil(entries, own):
    """Offered place entries with today's own hoe tiles first (``own`` newest first); others keep their order."""
    if not own:
        return entries
    rank = {tuple(t): i for i, t in enumerate(own)}
    return sorted(entries, key=lambda e: rank.get(tuple(e["tile"]), len(rank)))

class WorldExtrasRuntimeMixin(AnimalRoutineRuntimeMixin):
    """Mixed into StardewGoalRuntime; uses its lock, stores, leased executors and journal."""

    def _init_world_extras_runtime(self) -> None:
        self._extras_last_entries_key = None

    # ------------------------------------------------------------------ flag + objective
    def _world_extras_flag_configured(self) -> bool:
        """The mod's WorldExtrasEnabled: session.json carries world_extras_enabled only when it is on."""
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            from arti_stardew_runtime import _SESSION_READ_RETRY_SECONDS
            return "world_extras_enabled" in self._commands.baca_sesi(folder, retry_detik=_SESSION_READ_RETRY_SECONDS).mentah
        except Exception:  # noqa: BLE001 - unreadable session = flag unknown = no extras objective
            return False

    def _pin_extras_objective(self, goal, objective, request) -> None:
        pin = request.snapshot.water_context or request.snapshot.movement_context
        objective.start_location = pin.location if pin else None
        blocker = extras.time_refusal(objective, getattr(request.snapshot.motor, "time_of_day", None))
        goal.extras_objective = objective
        if blocker:
            objective.blocker = blocker
            goal.play_blocker = blocker
        self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "pinned": asdict(objective),
                       "origin": "owner_extras_objective"})

    def _extras_time_now(self):
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        return getattr(motor, "time_of_day", None)

    # ------------------------------------------------------------------ offers
    def _extras_world_offered(self, goal, target) -> bool:
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        if self._animal_open(goal):
            return self._animal_world_offered(goal, target, self._water_context(self._binding_view()[1]))
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        if (goal is not None and run is not None and session.status == "active" and run.status == "running"
                and run.goal_id == goal.goal_id and not run.state.get("wave1_driver")):
            kind = (target or {}).get("kind")
            if (run.call.name == "crab_pot" and run.call.arg in (None, "pasang", "ambil")
                    and kind in ("place_item", "machine_insert", "machine_collect")):
                # S24 gap7c: the native offer precedes the leg's ExtrasObjective. The
                # composition pins its water/pot tile; the existing leg revalidates before X/A.
                return True
            if run.call.name == "masak" and run.call.arg and kind == "kitchen":
                return True  # W1 gap B3 S30: only the owner-named recipe attempt presses its own kitchen
            if run.call.name == "beli_hewan" and run.call.arg and kind == "animal_shop":
                return True
            if run.call.name == "surat_quest" and run.call.arg and kind in ("mailbox", "quest_delivery"):
                return True  # W1 gap B4 S28: the actor's mailbox and the mod's quest hand-in only (never npc_gift)
            if run.call.name == "upgrade_alat" and run.call.arg and kind == "blacksmith":
                return True
            if run.call.name == "donasi" and run.call.arg and " ke bundle " in run.call.arg and kind == "community_note":
                return True
            if run.call.name == "aksi" and run.call.arg and run.call.tile is not None:
                # fix54F (a39 D22:J652): ordinary ACTION on the named target she chose presses the mod's own faced
                # capture of it (same ref/kind/tile, still published). The mod re-checks it and its receiver.
                from arti_stardew_generic_world import ordinary
                from scripts.live7.interactions import faced_named_target
                pid = self._binding_view()[1]
                pin = self._water_context(pid) if pid is not None else None
                if faced_named_target(run.call, target, ordinary(self, pin)):
                    return True
        if not extras.target_offered(target, objective):
            return False
        if objective.kind == "gift":
            _, pid = self._binding_view()
            pin = self._water_context(pid) if pid is not None else None
            held = self._held_item_fact(goal, pin) or {}
            if (pin is None or (objective.start_location is not None and pin.location != objective.start_location)
                    or str(held.get("name") or "").casefold() != objective.item):
                return False
        return extras.time_refusal(objective, self._extras_time_now()) is None

    def _extras_approach_entries(self, pin, motor_tile) -> list:
        goal = self._active
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        if objective is None or pin is None or self._animal_open(goal):
            return []
        rec = self._obs.get(pin.player_id, "ui.context")
        # FIXB54 W5: a Feed Hopper was advertised ready with no root hay. Do not spend
        # an approach on a positively observed zero withdrawal. Older/malformed facts
        # remain unknown; a stocked alternative or native faced offer still wins.
        if (objective.kind == "hay" and extras.objective_open(objective) and objective.done < objective.count
                and getattr(rec, "authoritative", False) is True):
            hoppers = [e for e in extras.extras_targets(rec.facts) if e["kind"] == "hay_hopper"]
            faced = (rec.facts or {}).get("world_interaction_target")
            if (hoppers and not extras.target_offered(faced, objective)
                    and all(e.get("ready") is False and type(e.get("withdrawal_count")) is int
                            and e["withdrawal_count"] == 0 for e in hoppers)):
                objective.blocker = "hay_unavailable"
                self._journal({"ev": "extras_blocked", "goal_id": goal.goal_id, "kind": "hay",
                               "blocker": "hay_unavailable", "detail": "no_native_withdrawal",
                               "refs": [e["ref"] for e in hoppers][:4]})
        if (objective.kind == "gift" and rec is not None and rec.authoritative
                and objective.done < objective.count):
            # PART_B3 (journal 1443-1454): the native target re-published `already_gifted_today`
            # right AFTER GiftGiven was recorded (done 1/1); that refusal is the gift's own
            # consequence, not a hold. Only an open (not yet given) gift is held by it.
            named = next((e for e in extras.extras_targets(rec.facts)
                          if e["kind"] == "npc_gift" and str(e.get("label", "")).casefold() == objective.name), None)
            if named and named.get("gift_refusal"):
                objective.blocker, objective.stop = "gift_held", str(named["gift_refusal"])[:60]
        unreachable: list = []
        self._extras_stand_check(goal, objective, rec, tuple(motor_tile))
        entries = extras.approach_entries(rec.facts if rec is not None else None, tuple(motor_tile), objective,
                                          unreachable=unreachable, invalid_stands=goal.extras_invalid_stands)
        if objective.kind == "place" and entries:
            # Smoke 2026-10-23 D3 J335/J359/J486 ("[recorded remark omitted]"): cangkul tilled 52,17,
            # then tanam walked 18 steps to old bare soil at 50,12. Live7 plants today's own fresh soil first,
            # newest first, whenever the native offer lists it; every other offered plot keeps the mod's order.
            entries = prefer_own_soil(entries, getattr(self, "_live7_own_soil", lambda: [])())
        for u in unreachable:
            key = (u["ref"], tuple(u["tile"]))
            if key not in goal.extras_unreachable:
                goal.extras_unreachable.add(key)
                self._journal({"ev": "extras_target_unreachable", "goal_id": goal.goal_id, "ref": u["ref"],
                               "tile": u["tile"], "reason": "no_walkable_stand", "misses": u.get("misses") or []})
        refusal = (rec.facts or {}).get("extras_refusal") if rec is not None and isinstance(rec.facts, Mapping) else None
        if (isinstance(refusal, Mapping) and isinstance(refusal.get("reason"), str) and objective.blocker is None
                and extras.objective_open(objective)):
            # FIXB16 D-B14-4 (live V2B14G6harvest-a: `profession_menu_policy:level_5`, the rule alternated two crops): the
            # first policy refusal of the faced target ends the objective (the owner decides the profession, not ARTI).
            # Review MAJOR: only a refusal of a target of this objective's kind (a pet goal passing a ripe crop goes on).
            reason = refusal["reason"][:60]
            if (refusal.get("kind") in extras.OBJ_KINDS.get(objective.kind, ())
                    and (objective.kind != "gift" or extras.target_offered(
                        {"kind": refusal.get("kind"), "source_fact_id": refusal.get("ref")}, objective))):
                policy = reason.split(":", 1)[0]
                objective.blocker = objective.kind + ("_refused_profession_policy" if policy == "profession_menu_policy"
                                                      else "_refused_policy")
                if objective.kind == "gift":
                    objective.stop = reason
                self._journal({"ev": "extras_blocked", "goal_id": goal.goal_id, "kind": objective.kind,
                               "blocker": objective.blocker, "detail": reason,
                               "ref": str(refusal.get("ref") or "")[:120] or None})
            elif (refusal.get("kind"), refusal.get("ref")) != getattr(goal, "extras_refusal_ignored", None):
                goal.extras_refusal_ignored = (refusal.get("kind"), refusal.get("ref"))
                self._journal({"ev": "extras_refusal_ignored", "goal_id": goal.goal_id, "objective": objective.kind,
                               "kind": refusal.get("kind"), "detail": reason, "ref": str(refusal.get("ref") or "")[:120] or None})
        wait = getattr(goal, "extras_stand_wait", None)
        if (not entries and unreachable and objective.kind in self._MOVING_OBJECTIVES and objective.blocker is None
                and (not wait or wait.get("ended") == "target_reachable") and not self.curfew_window_now()):
            # FIXB16 review: a target that moved back into a nook is waited for again, bounded per goal.
            n = (wait or {}).get("n", 0) + 1
            if n > self.STAND_WAITS_MAX:
                objective.blocker = "target_no_stand"
                self._journal({"ev": "extras_blocked", "goal_id": goal.goal_id, "kind": objective.kind,
                               "blocker": "target_no_stand", "refs": [u["ref"] for u in unreachable][:4],
                               "waits": self.STAND_WAITS_MAX})
            else:
                goal.extras_stand_wait = {"since": game_now(self), "refs": [u["ref"] for u in unreachable][:4], "n": n}
                self._stage_lock_last = "extras_stand_wait"  # FIXB16 D-B14-3: released by process_stage_lock (continuation)
                self._journal({"ev": "extras_stand_wait", "goal_id": goal.goal_id, "status": "started",
                               "refs": goal.extras_stand_wait["refs"], "max_s": self.STAND_WAIT_S, "n": n})
                self._journal({"ev": "stage_lock", "goal_id": goal.goal_id, "stage": "extras_stand_wait", "status": "locked"})
        if not entries and objective.kind == "mount" and objective.blocker is None:
            unnamed = next((b for b in extras.extras_blockers(rec.facts if rec is not None else None)
                            if b["kind"] == "horse_mount" and b["blocker"] == "horse_unnamed"), None)
            if unnamed is not None:
                # FIXB15 D-B12-5: the only horse has no name; only its stable owner can name it (vanilla naming menu):
                # a staging requirement, reported as such instead of "[recorded remark omitted]".
                objective.blocker = "horse_unnamed"
                self._journal({"ev": "extras_blocked", "goal_id": goal.goal_id, "kind": objective.kind,
                               "blocker": "horse_unnamed", "stable_owner": unnamed["stable_owner"],
                               "needs": "the stable owner names the horse once"})
        self._extras_retry_moving(goal, entries)
        key = tuple(e["ref"] for e in entries)
        if key != self._extras_last_entries_key:
            self._extras_last_entries_key = key
            self._journal({"ev": "extras_approach_offered", "kind": objective.kind, "refs": list(key)[:8]})
        return entries

    EXTRAS_MOVING_RETRIES = 3
    STAND_INVALID_READS = 2

    def _extras_stand_check(self, goal, objective, rec, motor_tile) -> None:
        """FIXB19 D-B16-1 (live V2B16PET-a/b: arrived at the published stand (54,9) facing Dudley (54,8); the mod's capture
        said `building_at_grab_tile` (the farmhouse's tile-property area), no world target, the pet dropped from the
        offer -> inspect loop). ARTI on a published stand of an objective target, the approach ARRIVED there, and no
        offered world target of the objective's kinds on STAND_INVALID_READS distinct ui.context revisions: that stand
        is invalid for that target tile (``extras_stand_invalid``); with no stand left the target is unreachable -> the
        bounded stand wait (moving targets) -> `extras_blocked target_no_stand`, never an inspect loop."""
        progress = getattr(goal, "approach_progress", None) or {}
        facts = rec.facts if rec is not None and isinstance(getattr(rec, "facts", None), Mapping) else None
        if progress.get("status") != "ARRIVED" or not isinstance(facts, Mapping) or not extras.objective_open(objective):
            goal.extras_stand_reads = None
            return
        kinds = extras.OBJ_KINDS.get(objective.kind, ())
        target = facts.get("world_interaction_target")
        if isinstance(target, Mapping) and target.get("kind") in kinds:
            goal.extras_stand_reads = None
            return
        entry = next((e for e in extras.extras_targets(facts) if e["ref"] == progress.get("target_ref")
                      and e["kind"] in kinds and [motor_tile[0], motor_tile[1]] in (extras.stand_tiles(e) or [])), None)
        if entry is None:
            goal.extras_stand_reads = None
            return
        key = (entry["ref"], entry["x"], entry["y"], motor_tile[0], motor_tile[1])
        if key in goal.extras_invalid_stands:
            return
        # Review MAJOR: only a settled, FACED pose counts (ARRIVED does not imply facing; the mod captures at the grab
        # tile), only a ui.context read published after that pose began, and only reads from separate decisions
        # (_approach_targets runs several times per decision; the revision churns every tick).
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        need = {(0, -1): 0, (1, 0): 1, (0, 1): 2, (-1, 0): 3}.get((entry["x"] - motor_tile[0], entry["y"] - motor_tile[1]))
        if need is None or getattr(motor, "facing", None) != need:
            goal.extras_stand_reads = None
            return
        from arti_stardew_runtime import _grab_tile
        grab = _grab_tile(getattr(motor, "pixel", None), need)
        if (grab is not None and grab == tuple(motor_tile)
                and getattr(goal, "chest_flush_key", None) != (progress.get("target_ref"), tuple(motor_tile))):
            # GAP8B J2 L3162-L3171: on the up-ladder stand, facing it, a short (slowed) flush left the native grab
            # tile on ARTI's own tile; the mod captured no target there and the only ladder stand was invalidated
            # (no_walkable_stand). A miss before the owed flush has landed is a pose fact, not a stand fact.
            goal.extras_stand_reads = None
            return
        now = time.monotonic()  # compared with native observation receipt timestamps below
        reads = goal.extras_stand_reads if (goal.extras_stand_reads or {}).get("key") == key else {
            "key": key, "pose_since": now, "gens": []}
        goal.extras_stand_reads = reads
        received = getattr(rec, "received_monotonic", None)
        generation = getattr(self, "_inflight_generation", None)
        if type(received) not in (int, float) or received <= reads["pose_since"] or generation is None:
            return
        if generation not in reads["gens"]:
            reads["gens"].append(generation)
        if len(reads["gens"]) >= self.STAND_INVALID_READS:
            goal.extras_invalid_stands.add(key)
            goal.extras_stand_reads = None
            self._journal({"ev": "extras_stand_invalid", "goal_id": goal.goal_id, "ref": entry["ref"],
                           "tile": [entry["x"], entry["y"]], "stand": [motor_tile[0], motor_tile[1]],
                           "miss": str(facts.get("extras_refusal") or "")[:60] or None})

    # ------------------------------------------------------------------ FIXB15 D-B12-6: the exit leg to an off-scene target
    OFFSCENE_LEG_STEPS_MAX = 40
    OFFSCENE_LEGS_MAX = 2
    OFFSCENE_LEG_SPEECH = "Sebentar, aku keluar dulu ke tempat tujuannya."

    def _offscene_leg_invoke(self, request):
        """_stage_invoke hook: an open farm-outdoor extras objective given while ARTI is inside the farmhouse / a cabin
        pins a navigation leg (an ``enter_location`` objective, like the curfew's home leg) and answers every decision
        of the goal with the deterministic nav step (exit approach -> door -> location change) until ARTI is in the
        destination; then the leg is unpinned and the extras rule continues there. Bounded (steps, legs per goal); a
        failing step ends the leg (the provider decides again)."""
        import arti_stardew_interact as interact
        import arti_stardew_skills as skills
        import json
        with self._lock:
            goal = self._active
            if goal is None or request.goal_id != goal.goal_id or goal.progress_marker == "terminal":
                return None
            snap = request.snapshot
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            location = getattr(pin, "location", None)
            leg = getattr(goal, "offscene_leg", None)
            objective = getattr(goal, "extras_objective", None)
            arrival = getattr(goal, "offscene_arrival", None)
            if leg is None and arrival is not None and extras.objective_open(objective):
                kinds = extras.OBJ_KINDS.get(objective.kind, ())
                if location != arrival["location"] or any(
                        isinstance(t, Mapping) and t.get("kind") in kinds for t in getattr(snap, "approach_targets", ()) or ()):
                    goal.offscene_arrival = None  # a target is here (or ARTI moved on): the extras rule works on
                else:
                    # FIXB15 review: count only a REFRESHED ui.context (a new extras_targets scan), never the same one twice
                    rec = self._obs.get(pin.player_id, "ui.context") if pin is not None else None
                    rev = getattr(rec, "domain_revision", None)
                    if rev is None or rev != arrival.get("rev"):
                        arrival["checks"] += 1
                        arrival["rev"] = rev
                    if arrival["checks"] >= self.OFFSCENE_ARRIVAL_CHECKS:
                        objective.blocker = "target_not_in_view"
                        goal.offscene_arrival = None
                        self._journal({"ev": "extras_blocked", "goal_id": goal.goal_id, "kind": objective.kind,
                                       "blocker": "target_not_in_view", "location": location,
                                       "needs": "the target in view (e.g. the pet outdoors, not raining)"})
                        return self._rule_json(request, {"kind": "wait", "reason": "extras_blocked", "ms": 0},
                                               "extras_rule", "blocked")
            if leg is None:
                destination = extras.offscene_destination(objective, location)
                resource = destination is None and self._offscene_resource_due(goal, location)
                if resource:
                    destination = "Farm"  # FIXB28 D-SOAK-10: a wood / chest / tools goal from the farmhouse / a cabin
                if (destination is None or goal.play_mode != "local_activity" or goal.interact_objective is not None
                        or goal.offscene_legs >= self.OFFSCENE_LEGS_MAX or not self._v2_pad_path(pin)
                        or self.curfew_window_now()
                        or (not resource and any(isinstance(t, Mapping)
                                                 and t.get("kind") in extras.OBJ_KINDS.get(objective.kind, ())
                                                 for t in getattr(snap, "approach_targets", ()) or ()))):
                    return None
                nav = interact.InteractObjective("enter_location", alias=destination)
                blocker = interact.resolve_objective(nav, current_location=location, home_location=None)
                if blocker is not None:
                    return None
                goal.interact_objective = nav
                goal.location_basis = location
                goal.offscene_legs += 1
                leg = goal.offscene_leg = {"destination": nav.target, "from": location, "steps": 0, "state": {},
                                           **({"resource": True} if resource else {})}
                self._journal({"ev": "offscene_leg", "goal_id": goal.goal_id, "status": "started",
                               "kind": "resource" if resource else objective.kind, "from": location,
                               "destination": nav.target})
                # this request's snapshot predates the pin (no exit offered yet): the nav step below is a paced
                # inspect whose refreshed snapshot offers the exits
            still_open = (self._offscene_resource_due(goal, location, any_location=True) if leg.get("resource")
                          else extras.objective_open(objective))
            if location == leg["destination"] or not still_open:
                arrived = location == leg["destination"]
                self._offscene_leg_end(goal, "arrived" if arrived else "objective_closed")
                if arrived and not leg.get("resource"):
                    goal.offscene_arrival = {"location": location, "checks": 0}
                return None
            if request.reason == "owner_turn":
                return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                   "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                   "speech": self.OFFSCENE_LEG_SPEECH, "choice": {"kind": "speak"}, "activity": None},
                                  ensure_ascii=False)
            leg["steps"] += 1
            state = leg["state"]
            state["nav_objective"] = goal.interact_objective
            # FIXB29: the shared nav step, incl. the door-step fallback at an ARRIVED exit (once per exit ref + tile: the
            # leg's own state) when no move_through / door press is presented
            step = skills.plan_step(skills.SkillCall("pergi_ke", "farm"), snap, state=state)
            if (step.kind != "decision" or leg["steps"] > self.OFFSCENE_LEG_STEPS_MAX
                    or step.choice.get("kind") == "finish"):
                self._offscene_leg_end(goal, "steps_max" if leg["steps"] > self.OFFSCENE_LEG_STEPS_MAX
                                       else (step.reason if step.kind != "decision" else "nav_finish"))
                return None
            choice, pace = step.choice, step.pace
            self._journal({"ev": "offscene_leg", "goal_id": goal.goal_id, "status": "step", "n": leg["steps"],
                           "kind": choice.get("kind"), "operation": choice.get("operation")})
        if pace:
            W.call('poll', 113, time.sleep, min(pace, 2.0))
        return self._rule_json(request, choice, "offscene_leg", str(choice.get("operation")))

    def _offscene_resource_due(self, goal, location, *, any_location: bool = False) -> bool:
        """FIXB28 D-SOAK-10 (V2SOAK1 t4: the `chest` goal (gather 1 wood + store) started in the Cabin after the night ran
        0 inputs; the episode / gather goals have no leg out, unlike the extras ones): an open wood / chest / tools goal
        (gather, episode plan or tool work; no extras objective) whose inventory step is not satisfied yet, while ARTI is
        inside the farmhouse / a cabin, takes the same bounded exit leg to the Farm first."""
        if getattr(goal, "extras_objective", None) is not None:
            return False
        resource = (bool(getattr(goal, "gather_required", False)) or getattr(goal, "gather_progress", None) is not None
                    or getattr(goal, "episode_plan", None) is not None or getattr(goal, "tool_work", None) is not None
                    or self._economy_ship_goal(goal))  # FIXB33: ship t17 started in the Cabin, without an exit leg
        satisfied = getattr(getattr(goal, "gather_progress", None), "status", None) == "INVENTORY_SATISFIED"
        return resource and not satisfied and (any_location or extras.farm_interior(location))

    @staticmethod
    def _exit_leg_open(goal) -> bool:
        """FIXB29 D-B26-1 (V2SOAK1 attempt 2 t1-t3: the resource leg reached the warp stand (3,10) of the Cabin exit (3,12)
        but the runtime presented no move_through there (`gathering` hid every exit during a gather / episode / tool-work
        objective; an episode goal saw no exit approach at all), so the nav step inspected until `exit_not_offered`): while
        a locked exit leg owns the goal's navigation (its own enter_location pin), the exits it needs (exit approach,
        door press, walk-onto warp) are offered even with a resource objective open. Only the leg: a resource goal without
        a leg still offers no exit (unchanged)."""
        leg = getattr(goal, "offscene_leg", None) if goal is not None else None
        nav = getattr(goal, "interact_objective", None) if goal is not None else None
        return bool(leg) and nav is not None and nav.kind == "enter_location" and nav.target == leg.get("destination")

    @classmethod
    def _exit_leg_keeps(cls, goal, target_location) -> bool:
        """FIXB29 review: a resource leg (wood / chest / tools to the Farm) is offered only the exits to its destination
        (never the farmhouse cellar stairs / another room); every other goal (and the extras legs) is unchanged."""
        leg = getattr(goal, "offscene_leg", None) if goal is not None else None
        if not leg or not leg.get("resource") or not cls._exit_leg_open(goal):
            return True
        return target_location == leg.get("destination")

    def _offscene_arrival_pins(self, goal) -> None:
        """FIXB29 D-B26-1: a resource leg's arrival keeps the goal's objective. The gather / tool-work pins were taken in
        the farmhouse / cabin (no work done there: the leg runs before any step); the leg's own location change would
        otherwise void them (`objective_location_changed`, a pin that never matches again). Caller: the snapshot build,
        before the refreshes; settled steps only; once per leg."""
        leg = getattr(goal, "offscene_leg", None)
        if not leg or not leg.get("resource") or leg.get("repinned"):
            return
        _, pid = self._binding_view()
        current = self._water_context(pid) if pid is not None else None
        if current is None or current.location != leg.get("destination"):
            return
        moved = []
        pin = getattr(goal, "gather_pin", None)
        if (pin is not None and pin.location != current.location and goal.gather_steps == goal.gather_settled
                and not goal.gather_uncertain):
            goal.gather_pin = current
            goal.gather_target = goal.gather_tool = goal.gather_request = None
            moved.append("gather")
        pin = getattr(goal, "tool_work_pin", None)
        if (getattr(goal, "tool_work", None) is not None and pin is not None and pin.location != current.location
                and goal.tool_work_steps == goal.tool_work_settled):
            goal.tool_work_pin = current
            moved.append("tool_work")
        leg["repinned"] = current.location
        self._journal({"ev": "offscene_leg", "goal_id": goal.goal_id, "status": "repinned",
                       "destination": current.location, "pins": moved})

    def _offscene_arrival_unblock(self, goal, current, cleared: list) -> None:
        """FIXB29: the location change of a resource leg that re-pinned its objective there is not a foreign pin."""
        leg = getattr(goal, "offscene_leg", None)
        if (leg and leg.get("repinned") == current and "gather_objective_blocked" in cleared
                and goal.play_blocker == "objective_location_changed"):
            goal.play_blocker = None
            cleared[cleared.index("gather_objective_blocked")] = "gather_pin_moved"

    def _offscene_leg_end(self, goal, status: str) -> None:
        leg = goal.offscene_leg or {}
        if goal.interact_objective is not None and goal.interact_objective.kind == "enter_location" \
                and goal.interact_objective.target == leg.get("destination"):
            goal.interact_objective = None  # the leg only: never the goal's own objective
        goal.offscene_leg = None
        self._journal({"ev": "offscene_leg", "goal_id": goal.goal_id, "status": status,
                       "destination": leg.get("destination"), "steps": leg.get("steps", 0)})

    def _extras_retry_moving(self, goal, entries) -> None:
        """D-B7b-1: a pet / animal / horse whose approach was blocked is offered again once it has MOVED (new tile),
        at most EXTRAS_MOVING_RETRIES times per goal (it may now stand where a side is reachable)."""
        for e in entries:
            ref = e["ref"]
            if not ref.startswith(("xt:gift:", "xt:pet:", "xt:animal:", "xt:horse:")) or ref not in goal.approach_blocked_refs:
                continue
            seen = goal.extras_retry.setdefault(ref, {"tile": list(e["tile"]), "n": 0})
            if seen["tile"] != list(e["tile"]) and seen["n"] < self.EXTRAS_MOVING_RETRIES:
                seen["tile"], seen["n"] = list(e["tile"]), seen["n"] + 1
                goal.approach_blocked_refs.discard(ref)
                goal.auto_acts.pop("extras:approach:" + ref, None)
                goal.auto_act_exhausted.discard("extras:approach:" + ref)
                self._journal({"ev": "extras_target_retry", "goal_id": goal.goal_id, "ref": ref, "tile": e["tile"],
                               "retry": seen["n"]})

    def _extras_seed_slots(self, pin) -> tuple:
        """D-B3-5 / D-B4-8: seed slots (category -74) from the authoritative progress.inventory record, ranked for the
        objective (named seed, else crop seeds before tree seeds / saplings)."""
        if pin is None:
            return ()
        rec = self._obs.get(pin.player_id, "progress.inventory")
        if rec is None or not rec.authoritative:
            return ()
        seeds = []
        for key, fact in sorted((rec.facts or {}).items()):
            if (str(key).startswith("slot:") and isinstance(fact, Mapping) and fact.get("occupied") is True
                    and fact.get("category") == extras.SEED_CATEGORY and type(fact.get("slot")) is int):
                seeds.append({"slot": fact["slot"], "qid": fact.get("qualified_item_id"),
                              "name": fact.get("display_name") or fact.get("name")})
        seeds.sort(key=lambda s: s["slot"])
        goal = self._active
        motor = self._state.latest(pin.player_id)
        # Farm outdoors uses calendar seasons. Other locations may implement SeedsIgnoreSeasonsHere;
        # without an observed override fact, preserve their native admission.
        season = (getattr(motor, "season", None) if pin.location == "Farm"
                  and not self._state.is_stale(pin.player_id) else None)
        return extras.rank_seed_slots(getattr(goal, "extras_objective", None) if goal is not None else None,
                                      seeds, season=season)

    def _extras_pad_button(self, goal) -> str | None:
        """The bundle objective opens the GameMenu (Start) to reach the Junimo note icon (G1 pad_button)."""
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        if objective is not None and objective.kind == "bundle" and not objective.opened:
            return "Start"
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        # S31 attaches bait through the same GameMenu inventory page (owner 2026-10-18 real attempt).
        if (goal is not None and session is not None and session.status == "active" and run is not None
                and run.status == "running" and run.goal_id == goal.goal_id and run.call.name == "siapkan_pancing"
                and not run.state.get("wave1_driver")):
            return "Start"
        return None

    # ------------------------------------------------------------------ single step (inside _execute_leased_action)
    def _extras_step_readback(self, request, args, result, evidence):
        """world_effect_status for one extras press from the mod verdict (+ the receiver identity for questions)."""
        kind = args.get("kind")
        envelope = result.mentah if result is not None and result.mentah else {}
        effect = None
        if evidence.lifecycle == "completed" and evidence.world_effect_status == "unverified":
            effect = extras.verified_effect(kind, envelope)
            if effect is not None and kind in (*extras.RECEIVER_KINDS, "npc_gift", "quest_delivery") and not self._extras_receiver_up(request, effect):
                effect = None
        if effect is not None:
            evidence = replace(evidence, after=dict(effect), world_effect_status=extras.FINISH)
        elif evidence.lifecycle == "completed" and evidence.outcome == "InteractionNoEffect":
            evidence = replace(evidence, world_effect_status="no_effect_observed")
        elif evidence.lifecycle == "completed":
            evidence = replace(evidence, world_effect_status="uncertain")
        self._journal({"ev": "extras_effect", "kind": kind, "outcome": evidence.outcome,
                       "world_effect_status": evidence.world_effect_status,
                       "detail": envelope.get("effect_detail") if isinstance(envelope, Mapping) else None})
        return evidence, envelope.get("cleanup_confirmed") is True

    def _extras_receiver_up(self, request, effect) -> bool:
        """The question / elevator menu is the result receiver in a fresh ui.context (bounded wait)."""
        pin = request.snapshot.water_context
        if pin is None:
            return False
        menu = extras.RECEIVER_MENUS.get(effect["kind"], "DialogueBox")
        deadline = game_now(self) + _RECEIVER_READBACK_S
        while game_now(self) < deadline and not self._worker_stop.is_set():
            rec = self._obs.get(pin.player_id, "ui.context")
            facts = rec.facts if rec is not None else {}
            interactive = facts.get("interactive") if isinstance(facts.get("interactive"), Mapping) else {}
            if (rec is not None and rec.authoritative and interactive.get("receiver_token") == effect["receiver_token"]
                    and interactive.get("menu_type") == menu):
                return True
            W.call('poll', 114, time.sleep, 0.05)
        return False

    # ------------------------------------------------------------------ chain
    def _execute_extras_chain(self, decision, request, goal):
        """One admitted extras press, the verified progress, then the bounded deterministic follow-through."""
        kind = decision.choice.args.get("kind")
        pin = request.snapshot.water_context or request.snapshot.movement_context
        before_location = pin.location if pin else None
        result = self._execute_leased_action(decision, request, goal, world_interact=True, hold=True)
        evidence = result.action_evidence
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        steps: list[str] = []
        stop = "no_effect"
        verified = (not result.discarded and evidence is not None and evidence.lifecycle == "completed"
                    and evidence.cleanup_confirmed
                    and evidence.world_effect_status in (extras.FINISH, "observed_location_change"))
        if kind == "npc_gift" and objective is not None:
            stop = "gift_effect_unverified"
            if verified:
                import arti_stardew_interact_b as ib
                try:
                    stop, steps = self._routine_follow_through(goal, ib.KIND_NPC, request, evidence)
                except Exception as exc:
                    stop = "exception:" + type(exc).__name__
            if not verified or stop != "dialogue_closed":
                objective.blocker = "gift_held"
                objective.stop = stop
                verified = False
        if verified and objective is not None:
            effect = dict(evidence.after or {})
            if kind == "ladder_down":
                effect = {"kind": kind, "location": (evidence.after or {}).get("location_id")}
            else:
                effect.setdefault("kind", kind)
            with self._lock:
                progress = extras.record_effect(objective, effect)
                if progress is not None:
                    self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "extras": progress})
            stop = "effect_recorded"
            if kind in ("shaft", "bus", "minecart", "mine_elevator", "ladder_up"):
                try:
                    stop, steps = self._extras_follow_through(goal, objective, kind, before_location)
                except Exception as exc:  # noqa: BLE001 — any doubt ends the chain; every step already settled
                    stop = "exception:" + type(exc).__name__
                if stop not in ("arrived", "effect_recorded"):
                    with self._lock:
                        objective.stop = objective.stop or stop
        self._journal({"ev": "extras_chain", "generation_id": request.generation_id, "kind": kind, "steps": steps,
                       "stop": stop})
        self._settle_held_action(decision, request, result, leased=True)
        if verified:
            with self._lock:
                pending = self._pending_continuation is not None
            if not pending:
                self._resume_after_pause(reason="extras_chain_settled")
        return result

    def _extras_follow_through(self, goal, objective, kind, before_location) -> tuple[str, list[str]]:
        _, pid = self._binding_view()
        deadline = game_now(self) + extras.FOLLOW_TOTAL_S
        waiting_since = game_now(self)
        steps: list[str] = []
        while True:
            if self._worker_stop.is_set() or self._action_cancel.is_set():
                return "cancelled", steps
            if game_now(self) >= deadline:
                return "follow_deadline", steps
            if kind == "mine_elevator":
                step = extras.plan_elevator_step(objective, self._ui_pad_view(pid))
            else:
                step = extras.plan_question_answer(objective, self._ui_key_view(pid))
            if step.kind == "wait":
                if game_now(self) - waiting_since >= extras.FOLLOW_STEP_WAIT_S:
                    return "step_wait:" + step.reason, steps
                W.call('poll', 116, time.sleep, 0.1)
                continue
            if step.kind == "stop":
                return step.reason, steps
            if step.kind == "done":
                break
            if len(steps) >= extras.FOLLOW_MAX_UI_STEPS:
                return "ui_step_budget", steps
            waiting_since = game_now(self)
            steps.append(step.reason)
            if kind == "mine_elevator":
                primitive = command.PRIMITIVE_UI_PRESS if step.reason == "press_floor" else command.PRIMITIVE_UI_NAV
                run, before = self._run_ui_pad(primitive, dict(step.args), origin="extras_elevator")
                if run.uncertain or not run.verified:
                    return "ui_pad_" + ("uncertain" if run.uncertain else run.lifecycle), steps
                if primitive == command.PRIMITIVE_UI_PRESS:
                    with self._lock:
                        progress = extras.note_ui_press(objective, context="mine_elevator", snapped_kind="floor", held=False,
                                                        btn="A", effect=run.effect, verified=run.verified)
                        if progress is None:
                            return "floor_press_no_close", steps
                        self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "extras": progress})
            else:
                run = self._run_ui_key(dict(step.args), origin="extras_" + kind)
                if run.uncertain or not run.verified:
                    return "ui_key_" + ("uncertain" if run.uncertain else run.lifecycle), steps
                with self._lock:
                    objective.answered = True
                    self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "extras": {
                        "kind": objective.kind, "answered": step.reason}})
        # Arrival: the farmhand warp round trip (bounded); success only from a changed observed location.
        arrival_deadline = game_now(self) + _ARRIVAL_WAIT_S
        while game_now(self) < arrival_deadline and not self._worker_stop.is_set():
            motor = self._state.latest(pid) if pid is not None else None
            location = getattr(motor, "location", None)
            if location and location != before_location:
                with self._lock:
                    progress = extras.record_arrival(objective, before_location, location)
                    if progress is not None:
                        self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "extras": progress})
                        return "arrived", steps
            W.call('poll', 115, time.sleep, 0.1)
        return "arrival_not_observed", steps

    # ------------------------------------------------------------------ G1 menu hooks (bundle / museum / elevator)
    def _extras_note_ui_press(self, goal, operation: str, args: dict, run, before) -> None:
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        if objective is None or operation != "ui_press" or before is None:
            return
        snapped = before.snapped or {}
        with self._lock:
            progress = extras.note_ui_press(objective, context=before.context, snapped_kind=snapped.get("kind"),
                                            held=before.held_item is not None, btn=args.get("btn"), effect=run.effect,
                                            verified=run.verified)
            if progress is not None:
                if objective.kind == "bundle" and progress.get("deposit"):
                    goal.bundle_receiver = before.receiver_token
                self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "extras": progress,
                               "origin": "ui_press"})

    def _extras_note_menu_view(self, goal, view) -> None:
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        if objective is not None and view is not None and extras.note_menu_view(objective, view.context):
            self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "extras": {
                "kind": objective.kind, "opened": True}, "origin": "ui_pad_view"})

    # ------------------------------------------------------------------ finish
    def extras_finish_due(self) -> bool:
        """Caller holds the lock (runner idle check): a verified extras objective awaits the worker's extras_rule finish."""
        try:
            goal = self._active
            objective = getattr(goal, "extras_objective", None) if goal is not None else None
            if self._animal_open(goal):
                return False  # the model chooses finish; fresh native state is the criterion
            exit_stopped = objective is not None and objective.kind == "exit_mines" and objective.stop is not None
            return (objective is not None and goal.progress_marker != "terminal" and not objective.finish_recorded
                    and (extras.objective_verdict(objective)[0] == "achieved" or self.extras_terminal_blocker(objective.blocker)
                         or exit_stopped))
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    # FIXB15 D-B13-4 (live V2B13G6horse-a / pet-b/c: `extras_blocked horse_unnamed` / no pet on the Farm after the exit leg,
    # then model inspect loops -> HELD): these blockers end the goal at once (verdict partial with the blocker, journaled).
    EXTRAS_TERMINAL_BLOCKERS = frozenset({"horse_unnamed", "target_not_in_view", "target_no_stand", "pet_rule_budget", "gift_rule_budget", "gift_held"})
    # FIXB24 review: the exit leg's own ends (blocker -> partial with that reason)
    MINES_EXIT_BLOCKERS = ("mines_exit_failed", "hp_floor", "no_plain_tool")
    EXTRAS_BLOCKER_SPEECH = {
        "horse_unnamed": "Kudanya belum punya nama; pemilik kandang harus menamainya dulu.",
        "target_not_in_view": "Aku sudah keluar, tapi targetnya tidak terlihat di sini.",
        "target_no_stand": "Targetnya ada di tempat yang tidak bisa kudekati.",
        "pet_rule_budget": "Batas pendekatan sudah habis; elusnya belum terverifikasi.",
    }
    STAND_WAIT_S = 30.0
    STAND_WAITS_MAX = 3  # FIXB16 review: waits per goal (a new one only after the last ended target_reachable)
    _MOVING_OBJECTIVES = ("gift", "pet", "mount", "animal_pet")

    MINES_EXIT_STEPS_MAX = 40
    MINES_EXIT_CONTROL_WAITS = 8  # GAP8E: re-reads while can_player_move is false (outside the step budget)

    def mines_exit_pin(self, goal, location: str, combat_summary: dict) -> None:
        """Caller holds the lock (combat finish in a MineShaft): the combat goal's exit leg (PLAN §5.3 `exited_mines`):
        approach the level's up-ladder, press A, answer ExitMine `Leave`, observe the location change."""
        goal.mines_exit = {"from": location, "combat": combat_summary, "steps": 0}
        from arti_runtime.mines_exit import policy
        p = policy(self)
        if p is not None:
            # New goals keep failed route edges; they never renew a native offer.
            memo = p.mines_exit_recovery.setdefault(location, {})
            goal.mines_exit["route"] = memo.setdefault("route", {})
            goal.mines_exit["alternate"] = bool(memo.get("alternate"))
        session = getattr(self, "_skill_session", None)
        run = session.current if session is not None and session.status == "active" else None
        if (run is not None and run.goal_id == goal.goal_id and (
                run.call.name in ("lawan_mine", "tambang_bijih")
                or run.call.name == "pergi_ke" and run.call.arg == "mine")):
            goal.mines_exit["steps"] = run.state.pop("mines_exit_steps_used", 0)
            run.state["mines_exit_attempted"] = True
        goal.extras_objective = extras.ExtrasObjective("exit_mines", start_location=location)
        self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "started", "from": location,
                       "combat": combat_summary})

    def _mines_exit_invoke(self, request):
        """_stage_invoke hook: while the exit leg is open every decision of the goal is its next deterministic step (the
        extras rule's own choice on this snapshot: world_interact on the faced up-ladder, else approach it); bounded."""
        with self._lock:
            goal = self._active
            leg = getattr(goal, "mines_exit", None) if goal is not None else None
            objective = getattr(goal, "extras_objective", None) if goal is not None else None
            if (leg is None or objective is None or objective.kind != "exit_mines" or request.goal_id != goal.goal_id
                    or goal.progress_marker == "terminal" or not extras.objective_open(objective) or objective.arrived):
                return None
            # A clean unpublished refusal owes no input. A possibly-applied command must settle before
            # any new approach or alternate; never retry it just because the pose is unchanged.
            from arti_stardew_part_a import unpublished_placeholder
            from arti_stardew_skills import _g
            last = getattr(goal, "last_action", None)
            if last is not None and (not unpublished_placeholder(last) and (
                    _g(last, "cleanup_confirmed") is not True
                    or _g(last, "lifecycle") not in ("completed", "rejected", "not_taken")
                    or _g(last, "world_effect_status") in ("uncertain", "possible_mutation", "unverified"))):
                return self._rule_json(request, dict(kind="wait", reason="mines_exit_input_unsettled", ms=0),
                                       "mines_exit", "wait_receipt")
            if request.reason == "owner_turn":
                import json as _json
                return _json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                    "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                    "speech": "Sebentar, aku keluar dari tambang dulu.", "choice": {"kind": "speak"},
                                    "activity": None}, ensure_ascii=False)
            snap_pin = (getattr(request.snapshot, "water_context", None)
                        or getattr(request.snapshot, "movement_context", None))
            import arti_stardew_combat as _combat
            low = _combat.idle_hp_floor(((self._combat_ui_facts(snap_pin) or {}).get("combat")) or None)
            session = getattr(self, "_skill_session", None)
            run = session.current if session is not None and session.status == "active" else None
            motor_view = _g(request.snapshot, "motor")
            from arti_runtime.mines_exit import remember, policy
            def blocked(reason):
                objective.blocker = reason
                remember(self, self._skill_wave1_context(request.snapshot), _g(motor_view, "location"), reason)
                if leg.get("floor_recorded") != reason:
                    # ADR 179: every exit failure leaves one floor record for the next analysis.
                    leg["floor_recorded"] = reason
                    from arti_runtime.mines_body import floor_record
                    floor_record(self, self._skill_wave1_context(request.snapshot), _g(motor_view, "location"),
                                 "exit:" + str(reason), here=_g(motor_view, "tile"))
            if low is not None and low["hp"] <= 0:
                # ADR 169: positive low HP cannot veto every exit. Food, route
                # and escape combat below replace the old work-floor refusal.
                objective.blocker = "hp_floor"
                self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "hp_floor", "hp": low["hp"],
                               "max_hp": low["max_hp"], "floor_pct": low["floor_pct"]})
                return None
            from arti_runtime.mines_exit import blocked as exit_blocked
            p = policy(self)
            native_up = (_g(request.snapshot, "world_interaction_target") or {}).get("kind") == "ladder_up"
            current_context = self._skill_wave1_context(request.snapshot)
            # ADR 192: food availability does not cancel an exit already owed.
            # Her ordinary food choices remain available at the next decision.
            from arti_runtime.mines_exit import chosen, REPLAYED
            if p is not None and not native_up and not chosen(run):
                # ADR 239: a remembered failure bounds the program's own recovery, never her chosen way out.
                refusal = exit_blocked(p, self._skill_wave1_context(request.snapshot), _g(motor_view, "location"))
                if refusal:
                    if run is not None:
                        run.state[REPLAYED] = refusal
                    # The memo already holds this failure and its facts; a replay re-dates nothing.
                    clock = p.mines_exit_recovery.get(_g(motor_view, "location"), {}).get("clock")
                    blocked(refusal)
                    p.mines_exit_recovery.setdefault(_g(motor_view, "location"), {})["clock"] = clock
                    return None
            leg["steps"] += 1
            if leg["steps"] > self.MINES_EXIT_STEPS_MAX:
                blocked("mines_exit_failed:steps_max")
                self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "steps_max"})
                return None
            if leg.get("escape_combat") and native_up:
                goal.combat = None  # a native up press is safer than another swing
                leg.pop("escape_combat", None)
            if leg.get("escape_combat"):
                from arti_runtime.mines_survival import escape_invoke
                result = escape_invoke(self, goal, request, leg)
                if result is not None or objective.blocker:
                    return result
            # s15w3: the first-Geode DialogueBox hid all ladder offers. Use the
            # same offered close as terminal cleanup, within this exit's steps;
            # the next fresh decision resumes the SAME native exit objective.
            from arti_stardew_skills import _menu_close, _g
            if (run is not None and run.call.name in ("lawan_mine", "tambang_bijih") and run.goal_id == goal.goal_id
                    and run.state.get("mines_unowned_menu")):
                objective.blocker = "mines_unowned_menu"
                return None  # an exit attempt cannot adopt a refused work-phase menu
            key = _g(request.snapshot, "ui_key_target") or {}
            pad = _g(request.snapshot, "ui_pad_target") or {}
            if (key.get("kind") == "dialogue"
                    or (pad.get("pins") or {}).get("context") in ("dialogue", "inventory")):
                close = _menu_close(request.snapshot)
                if close is not None:
                    return self._rule_json(request, close, "mines_exit", "close_discovery_dialogue")
            if (_g(motor_view, "can_player_move") is False and leg.get("control_waits", 0) < self.MINES_EXIT_CONTROL_WAITS
                    and goal.mines_exit is leg):
                # GAP8E J2 L3565-L3566: the up-ladder world_interact was refused not_taken
                # motor_unavailable:can_player_move=false (being hit / knocked back). Re-read until control returns,
                # then the same offered ladder press follows; these reads do not spend the exit's step budget.
                leg["control_waits"] = leg.get("control_waits", 0) + 1
                leg["steps"] -= 1
                return self._rule_json(request, dict(skills_inspect(), question="mines exit: wait for control"),
                                       "mines_exit", "wait_control")
            picked = extras.auto_choice(objective, request.snapshot, approach_progress=goal.approach_progress)
            progress = goal.approach_progress or {}
            ui = self._combat_ui_facts(snap_pin) or {}
            ups = [e for e in extras.extras_targets(ui) if e.get("kind") == "ladder_up" and e.get("ready") is True
                   and e["ref"].startswith("xt:mine:" + str(_g(motor_view, "location")) + ":")]
            if picked and picked[0] == "approach_target" and not any(e["ref"] == picked[1] for e in ups):
                picked = None  # an old catalogue entry cannot resurrect yesterday's ladder
            if not ups and not objective.opened and not (picked and picked[0] == "world_interact"):
                leg["missing_reads"] = leg.get("missing_reads", 0) + 1
                if leg["missing_reads"] >= 2:
                    blocked("mines_exit_not_observed")
                    return None
                return self._rule_json(request, dict(skills_inspect(), question="mines exit: re-observe native ladder"),
                                       "mines_exit", "refresh_ladder")
            leg.pop("missing_reads", None)
            # ADR 165: moving to an exit uses fresh threat geometry on every
            # short step, rather than an opaque approach through a monster.
            grid = self._nav_grid(snap_pin) if snap_pin is not None else None
            combat = ui.get("combat") or {}
            if (run is not None and run.state.get("live7") and not native_up and not objective.opened
                    and (not grid or "monsters" not in combat)):
                blocked("mines_exit_threat_geometry_unknown")
                return None
            if grid and "monsters" in combat and not objective.opened and not native_up:
                # ADR 177: with a monster near, the walk to the up-ladder stand is ONE mod leg at game tick
                # (nav-grid path, back-to-back holds, defend only when cornered), not one round trip per 8 ticks.
                from arti_runtime.mines_body import disengage_step
                body = disengage_step(self, goal, request, leg, ui, run=run)
                if body is not None or objective.blocker:
                    return body
            if grid and "monsters" in combat and not objective.opened:
                leg["alternate"] = True
                hp = min((v for v in (_g(motor_view, "health"), combat.get("hp")) if type(v) is int), default=None)
                if not native_up and hp is not None and leg.get("walk_hp") is not None and hp < leg["walk_hp"]:
                    # Reassess the current corridor below; HP loss alone must
                    # not suppress the replacement escape fight (ADR 169).
                    from arti_runtime.mines_survival import assessment, escape_invoke
                    if assessment(self._skill_wave1_context(request.snapshot), _g(motor_view, "location")) == "fight":
                        return escape_invoke(self, goal, request, leg)
                    # ADR 174: falling HP makes leaving more urgent, never impossible. The guarded route
                    # step below (flee-in-contact included) keeps her moving; the existing step/attempt
                    # bounds and named physical blockers end it.
                    leg["hp_falling_steps"] = leg.get("hp_falling_steps", 0) + 1
                    self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "hp_falling_continue",
                                   "hp_before": leg["walk_hp"], "hp": hp, "n": leg["hp_falling_steps"]})
                leg["walk_hp"] = hp
            # ARRIVED for an old ladder is not an arrival at today's observed stand.
            at_ladder = (progress.get("status") == "ARRIVED" and any(
                progress.get("target_ref") == e["ref"] and list(_g(motor_view, "tile") or ()) in
                (extras.stand_tiles(e) or ()) for e in ups))
            alternate = leg.get("alternate") or any(goal.extras_replans.get(e["ref"], 0) >= 1 for e in ups)
            if (not alternate and picked is None and not at_ladder and any(
                    list(_g(motor_view, "tile") or ()) in (extras.stand_tiles(e) or ()) for e in ups)):
                # FIX76A (a55 d5 J2563 ARRIVED on 18,4 under the combat goal; J2572 the curfew ended that run; J2584, J2630, J2677,
                # J2733, J2768 five new exit legs on the same tile, each with no arrival of its own, "[recorded remark omitted]" until
                # `stalled`, then 5 h 40 with no choice to 26:00 on floor 4). Standing on a native stand of the ladder is the arrival.
                alternate = True
            if alternate and not objective.opened and not (picked and picked[0] == "world_interact"):
                # B6 J2786/2793/2800: the same unpublished approach failed three times. Route from fresh
                # native grid/stands, including when the ordinary catalog has rotated that ref away.
                leg["alternate"] = True
                p = policy(self)
                if p is not None:
                    p.mines_exit_recovery.setdefault(_g(motor_view, "location"), {})["alternate"] = True
                here = _g(motor_view, "tile")
                at_ladder = any(list(here or ()) in (extras.stand_tiles(e) or ()) for e in ups)
                if (not ui or snap_pin is None or _g(snap_pin, "location") != _g(motor_view, "location")):
                    return self._rule_json(request, dict(skills_inspect(), question="mines exit: refresh floor"),
                                           "mines_exit", "refresh_floor")
                from arti_stardew_skills_mines import exit_route_step
                route = exit_route_step(request.snapshot, grid, ups, leg.setdefault("route", {}), combat=combat)
                if route is not None:
                    if route.kind == "fail":
                        from arti_runtime.mines_survival import assessment, escape_invoke
                        if (route.reason == "mines_exit_monster_corridor"
                                or route.reason == "mines_exit_failed" and assessment(current_context, _g(motor_view, "location")) == "fight"):
                            return escape_invoke(self, goal, request, leg)
                        self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "alternate_exhausted"})
                        # ADR 179: a cornered body acts (break through, else defend) before any page.
                        from arti_runtime.mines_body import cornered
                        raw = cornered(self, run, goal, request, leg, _g(motor_view, "location"), route.reason)
                        if raw is not None:
                            return raw
                        blocked(route.reason)
                        return None
                    self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "alternate_route",
                                   "operation": route.choice.get("operation"), "n": leg["steps"]})
                    return self._rule_json(request, route.choice, "mines_exit", "alternate_route")
                # At a freshly observed stand: the faced native press still wins. Never reuse a blocked approach.
                if picked is not None and picked[0] == "approach_target":
                    picked = None
            if picked is None and at_ladder:
                # FIXB24 D-B21-3 (live V2B21ACC11c: on the up-ladder (12,6) facing up, the A press was refused
                # `held_item_not_plain_tool` with the sword held -> inspects -> HELD): the ladder's A needs an empty hand
                # or a plain tool (as the shop counter, D-B7-3b). Only on the ladder (arrived; never while walking): select
                # an owned plain tool, else an empty toolbar slot, else the leg ends partial `no_plain_tool`. The weapon is
                # not re-selected (leaving). With a plain tool / empty hand the mod offers the ladder: world_interact.
                from arti_stardew_auto_act import empty_slot_choice, plain_tool_choice, plain_hand
                if not plain_hand(request.snapshot):
                    plain = plain_tool_choice(request.snapshot) or empty_slot_choice(request.snapshot)
                    if plain is None:
                        blocked("no_plain_tool")
                        self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "no_plain_tool",
                                       "held": str(((getattr(request.snapshot, "held_item", None) or {})
                                                    .get("qualified_item_id")))[:40]})
                        return None
                    picked = plain
                else:
                    # ADR 146: ARRIVED is a tile receipt, not GetGrabTile on the ladder. a60 selected a Hoe
                    # then inspected while monsters killed her. Re-enter the *offered* approach executor's
                    # bounded face/flush path, including alternate-route arrivals. No synthetic A permission.
                    from arti_stardew_runtime import _grab_tile
                    here = list(_g(motor_view, "tile") or ())
                    grab = _grab_tile(_g(motor_view, "pixel"), _g(motor_view, "facing"))
                    entry = next((e for e in ups if here in (extras.stand_tiles(e) or ())
                                  and grab is not None and grab != (e["x"], e["y"])
                                  and any(t.get("ref") == e["ref"] for t in _g(request.snapshot, "approach_targets", ()) or ())), None)
                    if entry is not None:
                        picked = ("approach_target", entry["ref"], {}, "finish_up_ladder_pose")
                if picked is None and leg.get("alternate"):
                    from arti_runtime.mines_exit import pose_step
                    entry = next((e for e in ups if list(_g(motor_view, "tile") or ()) in
                                  (extras.stand_tiles(e) or ())), None)
                    if entry is not None:
                        turn = pose_step(request.snapshot, entry, leg)
                        if turn.kind == "fail":
                            blocked(turn.reason)
                            return None
                        return self._rule_json(request, turn.choice, "mines_exit", "finish_native_up_ladder_pose")
            if picked is None:
                return self._rule_json(request, dict(skills_inspect(), question="mines exit: observe the up-ladder"),
                                       "mines_exit", "inspect")
            operation, target_ref, args, label = picked
            self._journal({"ev": "mines_exit", "goal_id": goal.goal_id, "status": "step", "operation": operation,
                           "target": label, "n": leg["steps"]})
            choice = {"kind": "act", "operation": operation, "target_ref": target_ref, "args": dict(args or {}),
                      "evidence_refs": []}
        return self._rule_json(request, choice, "mines_exit", str(label))

    EXTRAS_PRESS_MAX = 6
    EXTRAS_REPLANS_MAX = 2

    def _gift_selection_snapshot(self, snap, pin):
        from types import SimpleNamespace
        rec = self._obs.get(pin.player_id, "progress.inventory") if pin is not None else None
        facts = (rec.facts if rec is not None and rec.authoritative and rec.is_complete
                 and rec.session_id == pin.observation_session_id and rec.screen_id == pin.screen_id else {})
        by_slot = {f.get("slot"): f for f in (facts or {}).values() if isinstance(f, Mapping)}
        objective = self._active.extras_objective
        slots = tuple(s for s in getattr(snap, "selectable_items", ())
                      if by_slot.get(s.get("slot"), {}).get("qualified_item_id") == s.get("qualified_item_id")
                      and str(by_slot.get(s.get("slot"), {}).get("display_name") or
                             by_slot.get(s.get("slot"), {}).get("name") or "").casefold() == objective.item)
        held = getattr(snap, "held_item", None) or {}
        held_fact = by_slot.get(held.get("slot"), {})
        held_name = ((held_fact.get("display_name") or held_fact.get("name"))
                     if held_fact.get("qualified_item_id") == held.get("qualified_item_id") else None)
        return SimpleNamespace(world_interaction_target=snap.world_interaction_target,
            approach_targets=snap.approach_targets, held_item=dict(held, name=held_name),
            selectable_items=tuple(dict(s, name=objective.item) for s in slots), select_item_available=bool(slots))

    PET_DIRECT_MAX = 64  # decisions (including refreshes); existing moving approach/input/time caps still apply

    def _pet_rule_invoke(self, request):
        """FIXB41: own pet continuations before the provider can replace the route with a raw 60-tick move.

        The existing approach executor re-acquires the current tile each step and publishes short, leased chunks.
        The existing extras press verifies the native lastPetDay change. Arrival and refreshes record no effect.
        Exhaustion ends partial instead of handing an unfinished pet route back to model movement.
        """
        from arti_stardew_runtime import _CONTINUATION_REASONS, READY
        with self._lock:
            goal, snap = self._active, request.snapshot
            objective = getattr(goal, "extras_objective", None) if goal is not None else None
            pin = getattr(snap, "movement_context", None) or getattr(snap, "water_context", None)
            if (goal is None or request.goal_id != goal.goal_id or objective is None or objective.kind not in ("pet", "gift")
                    or not extras.objective_open(objective) or objective.done >= objective.count
                    or objective.blocker is not None or goal.play_mode != "local_activity"
                    or goal.progress_marker == "terminal" or self._status != READY or goal.question_basis
                    or goal.no_progress_blocked or goal.no_progress_until is not None
                    or not self._v2_pad_path(pin) or self.curfew_window_now()
                    or not (request.reason in _CONTINUATION_REASONS or request.trigger_type == "game")):
                return None
            if objective.kind == "gift":
                if objective.start_location is not None and pin.location != objective.start_location:
                    objective.blocker, objective.stop = "gift_held", "gift_location_changed"
                    return self._rule_json(request, {"kind": "wait", "reason": "extras_blocked", "ms": 0},
                                           extras.RULE_EXTRAS, objective.stop)
                snap = self._gift_selection_snapshot(snap, pin)
            picked = extras.auto_choice(objective, snap, approach_progress=goal.approach_progress)
            exhausted = goal.pet_direct >= self.PET_DIRECT_MAX
            if picked is not None and picked[0] == "approach_target":
                exhausted |= (goal.approach_steps >= self._goal_approach_steps_max(goal, picked[1], pin)
                              or (goal.approach_deadline != 0 and time.monotonic() >= goal.approach_deadline))
            if picked is not None and picked[0] == "world_interact":
                exhausted |= goal.extras_presses >= (1 if objective.kind == "gift" else self.EXTRAS_PRESS_MAX)
            if exhausted:
                objective.blocker = "gift_rule_budget" if objective.kind == "gift" else "pet_rule_budget"
                self._journal({"ev": "extras_blocked", "goal_id": goal.goal_id, "kind": objective.kind,
                               "blocker": objective.blocker, "decisions": goal.pet_direct,
                               "approach_steps": goal.approach_steps, "presses": goal.extras_presses})
                return self._rule_json(request, {"kind": "wait", "reason": "extras_blocked", "ms": 0},
                                       extras.RULE_EXTRAS, objective.blocker)
            goal.pet_direct += 1
            if picked is not None and picked[0] == "world_interact":
                return self._extras_press_invoke(request)
            if picked is None:
                # Refresh a temporarily absent target or an arrived pose without a native grab-tile offer.
                # Existing stand validation / bounded moving-target waits run on the refreshed facts.
                choice = skills_inspect()
            else:
                operation, target_ref, args, _ = picked
                choice = {"kind": "act", "operation": operation, "target_ref": target_ref,
                          "args": dict(args), "evidence_refs": []}
            self._journal({"ev": "pet_rule_direct", "goal_id": goal.goal_id,
                           "generation_id": request.generation_id, "choice": choice["kind"],
                           "operation": choice.get("operation"), "count": goal.pet_direct, "max": self.PET_DIRECT_MAX})
            return self._rule_json(request, choice, extras.RULE_EXTRAS, "pet")

    def _extras_press_invoke(self, request):
        """FIXB17 D-B14c-1 (live V2B14cSHAKE-a: routed to the stand (64,27) facing the tree; the pipelined continuation
        was dropped at L2487 (motor facts moved after it was decided), the re-inspect went to the model, which swung the
        Axe at the tree (seed consumed, no TreeShaken)): on the v2 pad path, once the faced world target is one the open
        extras objective offers, every decision of the goal is that press (world_interact), with no model turn between
        the arrival and the press. Bounded (EXTRAS_PRESS_MAX per goal); the same admission gates run on it."""
        with self._lock:
            goal = self._active
            objective = getattr(goal, "extras_objective", None) if goal is not None else None
            if (goal is None or request.goal_id != goal.goal_id or goal.progress_marker == "terminal"
                    or goal.play_mode != "local_activity" or request.reason == "owner_turn"
                    or not extras.objective_open(objective) or objective.blocker is not None or self._animal_open(goal)
                    or objective.done >= objective.count or getattr(goal, "mines_exit", None) is not None):
                return None
            if self.curfew_window_now():
                return None  # FIXB17 review: from 24:00 the curfew stage owns the goal (no new extras press)
            snap = request.snapshot
            target = getattr(snap, "world_interaction_target", None)
            if not isinstance(target, Mapping) or not self._extras_world_offered(goal, target):
                return None
            if not self._v2_pad_path(getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)):
                return None
            goal.extras_presses += 1
            if goal.extras_presses > self.EXTRAS_PRESS_MAX:
                if goal.extras_presses == self.EXTRAS_PRESS_MAX + 1:
                    self._journal({"ev": "extras_press", "goal_id": goal.goal_id, "status": "press_max",
                                   "max": self.EXTRAS_PRESS_MAX})
                return None
            kind = str(target.get("kind"))
            self._journal({"ev": "extras_press", "goal_id": goal.goal_id, "status": "press", "kind": kind,
                           "n": goal.extras_presses, "reason": request.reason})
            choice = {"kind": "act", "operation": "world_interact", "target_ref": None, "args": {}, "evidence_refs": []}
        return self._rule_json(request, choice, "extras_press", kind)

    def _extras_target_protected(self, goal, request, args) -> bool:
        """FIXB17 D-B14c-1 (b): while an extras objective is open, a tool swing at one of its own targets (the faced
        world target, an offered approach target or a published extras target of the objective's kinds) is refused."""
        import arti_stardew_tool_work as tw
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        if not extras.objective_open(objective):
            return False
        faced = tw.faced_tile(args) if isinstance(args, Mapping) else None
        if faced is None:
            return False
        snap = request.snapshot
        ui = None
        try:
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            rec = self._obs.get(pin.player_id, "ui.context") if pin is not None else None
            ui = rec.facts if rec is not None else None
        except Exception:  # noqa: BLE001 - the snapshot targets still protect
            ui = None
        return faced in extras.objective_target_tiles(objective, getattr(snap, "approach_targets", ()),
                                                      getattr(snap, "world_interaction_target", None), ui)

    def _extras_replan_leg(self, goal, ref, request) -> bool:
        """Handle a clean unpublished replan within the per-target retry/rotation cap.

        FIXB42 keeps a bin controller's armed detour; other extras/gather rebuild their
        controller on the next decision. Neither path renews the parent deadline or steps.
        """
        if not self._approach_replan_eligible(goal, request.snapshot.movement_context):
            return False
        n = goal.extras_replans.get(ref, 0) + 1
        goal.extras_replans[ref] = n
        if getattr(goal, "mines_exit", None) is not None and str(ref).startswith("xt:mine:"):
            # ADR 160: the next attempt must differ even if this row ends before
            # invoking the alternate controller. The native detour is not reset.
            goal.mines_exit["alternate"] = True
            from arti_runtime.mines_exit import policy
            p = policy(self)
            if p is not None:
                p.mines_exit_recovery.setdefault(goal.mines_exit["from"], {})["alternate"] = True
        rotated = n > self.EXTRAS_REPLANS_MAX
        # FIXB42: a bin controller has just armed its ONE detour. Rebuilding it here loses
        # that state and returns the same unpublished replan at the same pose until rotation.
        # Keep it only for this still-open bin/ref; next_step rechecks identity, revision,
        # fresh routing and the original deadline. Other extras/gather retries are unchanged.
        retain = (not rotated and (self._economy_ship_goal(goal) and str(ref).startswith("xt:bin:")
                                  or getattr(goal, "mines_exit", None) is not None and str(ref).startswith("xt:mine:"))
                  and goal.approach_controller is not None
                  and getattr(goal.approach_target, "target_ref", None) == ref)
        if not retain:
            goal.approach_target = None
        if rotated:
            goal.approach_blocked_refs.add(ref)
        self._journal({"ev": "extras_replan", "goal_id": goal.goal_id, "target_ref": str(ref)[:120], "n": n,
                       "status": "rotated" if rotated else "replan", "max": self.EXTRAS_REPLANS_MAX,
                       "controller_retained": retain})
        return True

    def _approach_replan_eligible(self, goal, pin=None, *, gather_only=False) -> bool:
        """One v2 admission predicate shared by the gate, trial runner and retry handler."""
        progress = getattr(goal, "approach_progress", None) or {}
        ship_ref = progress.get("target_ref")
        ship = (self._economy_ship_goal(goal) and str(ship_ref or "").startswith("xt:bin:")
                and progress.get("status") == "REPLAN_REQUIRED"
                and ship_ref not in goal.approach_blocked_refs)
        return bool(self._v2_pad_path(pin) and (self._gather_replan_open(goal) or ship or (
            not gather_only and extras.objective_open(getattr(goal, "extras_objective", None)))))

    @staticmethod
    def _gather_replan_open(goal) -> bool:
        """FIXB33 t13: a tree was offered, but its unpublished route replan ended the gather trial.

        Reuse the extras per-target retry/rotation bound; never reset the parent approach input/time cap.
        """
        progress = getattr(goal, "gather_progress", None)
        return (progress is not None and progress.status == "WORK_REMAINING"
                and not getattr(goal, "gather_uncertain", False))

    @classmethod
    def extras_terminal_blocker(cls, blocker) -> bool:
        return blocker in cls.EXTRAS_TERMINAL_BLOCKERS or blocker in cls.MINES_EXIT_BLOCKERS \
            or blocker in ("mines_escape_native_unavailable", "mines_exit_food_available") \
            or str(blocker or "").startswith("mines_escape_fight_stopped:") \
            or str(blocker or "").startswith("mines_exit_failed:") \
            or str(blocker or "").endswith("_refused_profession_policy") \
            or str(blocker or "").endswith("_refused_policy")

    def _extras_stand_wait_owns(self, goal) -> bool:
        """FIXB16 D-B14-3 (live V2B14G6pet-a/b: Dudley walked into a nook with no walkable stand; inspect loop -> HELD): a
        moving target with no stand is waited for (it moves; re-offered on every refresh) as a locked stage, bounded
        STAND_WAIT_S; then `extras_blocked target_no_stand` ends the goal. Released the moment a stand is published."""
        wait = getattr(goal, "extras_stand_wait", None)
        objective = getattr(goal, "extras_objective", None)
        if not wait or wait.get("ended"):
            return False
        if not extras.objective_open(objective):
            wait["ended"] = "objective_closed"
            return False
        if self.curfew_window_now():
            # FIXB16 review: the curfew stage owns the night (the wait never holds ARTI past 24:00)
            wait["ended"] = "curfew"
            self._journal({"ev": "extras_stand_wait", "goal_id": goal.goal_id, "status": "curfew",
                           "waited_s": round(game_now(self) - wait["since"], 1)})
            return False
        _, pid = self._binding_view()
        pin = self._water_context(pid)
        motor = self._state.latest(pid) if pid is not None else None
        rec = self._obs.get(pid, "ui.context") if pid is not None else None
        if pin is not None and motor is not None and getattr(motor, "tile", None) is not None and extras.approach_entries(
                rec.facts if rec is not None else None, tuple(motor.tile), objective,
                invalid_stands=goal.extras_invalid_stands):
            wait["ended"] = "target_reachable"
            self._journal({"ev": "extras_stand_wait", "goal_id": goal.goal_id, "status": "target_reachable",
                           "waited_s": round(game_now(self) - wait["since"], 1)})
            return False
        if game_now(self) - wait["since"] > self.STAND_WAIT_S:
            wait["ended"] = "timeout"
            objective.blocker = "target_no_stand"
            self._journal({"ev": "extras_blocked", "goal_id": goal.goal_id, "kind": objective.kind,
                           "blocker": "target_no_stand", "refs": wait.get("refs"), "waited_s": self.STAND_WAIT_S})
            return False
        return True

    OFFSCENE_ARRIVAL_CHECKS = 2

    def process_extras_finish(self) -> int:
        """Worker hook (FIXB11 D-B8-3, live V2B8G6shake-a: `extras_effect TreeShaken`, objective 1/1, then the continuation
        was discarded `effect_motor` and the runner HELD): once the objective's verified effects reach its count (shake /
        pet / mount / harvest / place / insert / ladder / travel arrival), the runtime records the goal finish itself
        (origin extras_rule), like economy_rule / fishing_rule. Waits while anything is in flight."""
        from types import SimpleNamespace
        from arti_stardew_runtime import OwnerTurnResult, StatusSpeech, FinishEvidenceView
        try:
            with self._lock:
                goal = self._active
                if (not self.extras_finish_due() or self._action_busy or self._inflight_generation is not None
                        or getattr(self, "_results_pending", 0) or self._trial_result_pending()):
                    return 0
                needs_close = goal.extras_objective.kind == "bundle" and goal.extras_objective.done >= goal.extras_objective.count
            close = self._bundle_close(goal) if needs_close else None
            with self._lock:
                if self._active is not goal or goal.progress_marker == "terminal" or goal.extras_objective.finish_recorded:
                    return 0
                verdict = self._extras_finish_verdict(goal)
                if needs_close and verdict is not None:
                    verdict = FinishEvidenceView(verdict.status if close == "closed" else "partial",
                        verdict.reason if close == "closed" else "bundle_menu_left_open:" + str(close),
                        dict(verdict.observed or {}, close=close))
                blocked = (verdict is not None and verdict.status != "achieved"
                           and (self.extras_terminal_blocker(goal.extras_objective.blocker)
                                or (goal.extras_objective.kind == "exit_mines" and goal.extras_objective.stop is not None)))
                if verdict is None or (verdict.status != "achieved" and not blocked and not needs_close):
                    return 0
                if blocked:
                    goal.extras_objective.finish_recorded = True
                goal.last_finish = verdict
                goal.progress_marker = "terminal"
                goal.revision += 1
                self._pending_continuation = None
                self._pending_event_reason = None
                self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                               "verdict": asdict(verdict), "origin": "extras_rule"})
            speech = ("Selesai; efeknya terverifikasi dari mod." if verdict.status == "achieved"
                      else self.EXTRAS_BLOCKER_SPEECH.get(verdict.reason, "Belum bisa: " + str(verdict.reason)))
            if verdict.reason == "exited_mines":
                speech = "Monsternya sudah dikalahkan dan aku sudah keluar dari tambang; terverifikasi."
            if str(verdict.reason).endswith("_refused_profession_policy"):
                speech = "Aku berhenti: aksinya akan membuka pilihan profesi, dan itu keputusan pemilik."
            result = OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                                     speech=StatusSpeech(speech), finish_evidence=verdict)
            if self._on_generation_result is not None:
                self._on_generation_result(SimpleNamespace(reason="extras_rule_finish", generation_id=None), result)
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "extras_rule_error", "error": type(exc).__name__})
            return 0

    def _bundle_close(self, goal) -> str:
        """One back then one close on the donation receiver, using fresh leased pins; never retry an uncertain press."""
        _, pid = self._binding_view()
        receiver = getattr(goal, "bundle_receiver", None)
        if not receiver:
            return "receiver_unavailable"
        for _ in range(2):
            view = self._ui_pad_view(pid)
            if view is None or view.receiver_token != receiver or view.context != "junimo_note":
                return "receiver_changed"
            if not view.available or not view.press_allowed("B") or view.held_item is not None:
                return "close_not_offered"
            with self._lock:
                if self._active is not goal or self._action_cancel.is_set():
                    return "superseded"
                was_busy, self._action_busy = self._action_busy, True
            try:
                run, _ = self._run_ui_pad("ui_press", view.press_args("B"), origin="extras_rule")
            finally:
                with self._lock:
                    self._action_busy = was_busy
            self._journal({"ev": "bundle_close_rule", "goal_id": goal.goal_id, "effect": run.effect,
                           "verified": run.verified, "cleanup": run.cleanup, "reason": run.reason})
            if not run.verified or not run.cleanup:
                return "close_unverified"
            if run.effect == "closed":
                return "closed"
            if run.effect != "changed":
                return "unexpected_effect"
        return "menu_left_open"

    def _extras_finish_verdict(self, goal):
        """FinishEvidenceView for a WS-G6 objective (extras_rule: verified effects only), or None."""
        objective = getattr(goal, "extras_objective", None) if goal is not None else None
        if objective is None:
            return None
        from arti_stardew_runtime import FinishEvidenceView
        if self._animal_open(goal):
            return self._animal_finish_verdict(goal)
        status, reason = extras.objective_verdict(objective)
        exit_leg = getattr(goal, "mines_exit", None)
        if objective.kind == "exit_mines" and exit_leg is not None:
            # FIXB17 D-B5-0 (acceptance step 11): the kill (observed_monster_killed) + exited_mines(from->to)
            observed = {"origin": "combat_rule+mines_exit", "combat": exit_leg.get("combat"),
                        "exited_mines": "%s->%s" % (exit_leg.get("from"), objective.arrived) if objective.arrived else None}
            if status == "achieved":
                objective.finish_recorded = True
                return FinishEvidenceView("achieved", "exited_mines", observed)
            if objective.blocker in ("hp_floor", "no_plain_tool"):
                return FinishEvidenceView("partial", objective.blocker, observed)
            return FinishEvidenceView("partial", "mines_exit_not_observed:" + str(reason), observed)
        if status == "achieved":
            objective.finish_recorded = True
        return FinishEvidenceView(status, reason, dict(extras.objective_summary(objective) or {}, origin="extras_rule",
                                                       effects=list(objective.effects)))
