"""WS1 (primitives v1) tool-work hooks for StardewGoalRuntime (mixin).

All decisions live in the pure module ``arti_stardew_tool_work``; this mixin only reads the
existing observation/state stores through the runtime's own helpers and keeps the per-goal
accounting. No new store, transport, inbox, lease or input path: every physical step is still
the unchanged leased executor (lease, guarded publication, trial reservation, readback). Runs
under the runtime lock wherever the gather counterparts do. Evidence ceiling: UNIT_TESTED.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict, replace

import arti_stardew_command as command
import arti_stardew_tool_work as tw
from arti_stardew_decision import ActChoice

TOOL_WORK_MIN_ENERGY = 20  # same floor as the runtime's _BATCH_MIN_ENERGY (mod floor: 4)
# Mod refusal codes that mean "this tool may not hit this target now" (count as a whiff). Any other
# rejection (busy, lease, binding, cursor, ...) is not_taken: no effect, no whiff, no budget.
_TOOL_REFUSAL_CODES = ("VILLAGER_AT_TOOL_TILE", "PLACED_OBJECT_AT_TOOL_TILE", "CROP_AT_TOOL_TILE",
                       "TOOL_TILE_NOT_ADJACENT", "PICKAXE_TARGET_NOT_STONE", "HOE_TILE_NOT_TILLABLE",
                       "CAN_TARGET_INVALID", "SCYTHE_TARGET_INVALID", "SCYTHE_AREA_UNSAFE", "STAMINA_LOW",
                       "TOOL_TILE_FACTS_UNREADABLE", "TOOL_NOT_WHITELISTED", "INVALID_HOLD_TICKS")


class ToolWorkRuntimeMixin:
    """Methods named ``_tool_work_*`` / ``_equipped_water`` only (merge-friendly with WS2/WS3)."""

    # ------------------------------------------------------------------ ingress / pins

    def _tool_work_begin(self, goal, request, kind: str, count: int = 1) -> None:
        """Owner ingress only (anchored grammar already matched). FIXB17: ``count`` effects (`pecah N batu`)."""
        goal.tool_work_count = max(1, min(int(count), tw.TOOL_WORK_COUNT_MAX))
        pin = request.snapshot.water_context
        tool_qid, steps = tw.KINDS[kind]
        progress = tw.ToolWorkProgress(goal.goal_id, pin.observation_session_id if pin else "", kind, tool_qid,
                                       remaining_steps=steps,
                                       crop_required=kind == "water_tile" and getattr(self, "_live7_active", lambda: False)())
        if pin is None:
            progress = replace(progress, status="UNAVAILABLE", reason="tool_work_context_unavailable")
            goal.play_blocker = "tool_work_context_unavailable"
        goal.tool_work = progress
        goal.tool_work_pin = pin
        self._journal({"ev": "tool_work_pinned", "goal_id": goal.goal_id, "kind": kind, "tool": tool_qid,
                       "pin": pin is not None, **({"count": goal.tool_work_count} if goal.tool_work_count > 1 else {})})

    def _tool_work_next(self, goal, effect) -> bool:
        """FIXB17 (`pecah N batu`): caller holds the lock; one more verified effect of a counted goal: the effect is
        kept, a fresh objective (same kind, new target) is pinned on the current context. False on the last one."""
        if len(goal.tool_work_done) + 1 >= goal.tool_work_count:
            return False
        progress = goal.tool_work
        goal.tool_work_done.append({"target_ref": effect.get("target_ref"), "effect": effect.get("reason"),
                                    "request_id": effect.get("request_id")})
        pin = self._water_context(goal.tool_work_pin.player_id) if goal.tool_work_pin is not None else None
        tool_qid, steps = tw.KINDS[progress.kind]
        goal.tool_work = tw.ToolWorkProgress(goal.goal_id, pin.observation_session_id if pin else "", progress.kind,
                                             tool_qid, remaining_steps=steps)
        if pin is None:
            goal.tool_work = replace(goal.tool_work, status="UNAVAILABLE", reason="tool_work_context_unavailable")
        goal.tool_work_pin = pin or goal.tool_work_pin
        goal.tool_work_tool = None
        goal.tool_work_steps = goal.tool_work_settled = 0
        goal.tool_work_action = None
        self._journal({"ev": "tool_work_next", "goal_id": goal.goal_id, "kind": progress.kind,
                       "done": len(goal.tool_work_done), "count": goal.tool_work_count})
        return True

    def _tool_work_pin_current(self, goal):
        """The pinned native/observation context, re-pinned only across a ui_revision change between
        settled steps (selecting the tool bumps it); any other difference is no context."""
        pin = goal.tool_work_pin
        if pin is None:
            return None
        current = self._water_context(pin.player_id)
        if current == pin:
            return pin
        if (current is not None and replace(current, ui_revision=pin.ui_revision) == pin
                and goal.tool_work_steps == goal.tool_work_settled):
            goal.tool_work_pin = current
            return current
        return None

    def _wet_soil_facts(self, pin, scene, window_targets):
        """Scene facts merged with the fresh local window; HoeDirt the window showed wet today stays wet
        (tw.wet_soil_view, live7-game-m2). Every scene+window merge on the Farm goes through here."""
        memory = getattr(self, "_wet_soil", None)
        if memory is None:
            memory = self._wet_soil = {}
        date = None
        try:
            motor = self._state.latest(pin.player_id) if pin is not None else None
            date = self._date_tuple(motor) if motor is not None else None
        except Exception:  # noqa: BLE001 - an unreadable date only disables the overlay
            date = None
        return tw.wet_soil_view(memory, getattr(pin, "location", None), date, scene, window_targets)

    def _tool_work_window(self, goal):
        from arti_stardew_scene import gather_window
        pin = self._tool_work_pin_current(goal)
        if pin is None:
            return None
        return gather_window(self._obs.get(pin.player_id, "scene.current_location"),
                             session_id=pin.observation_session_id, screen_id=pin.screen_id, location_id=pin.location)

    def _equipped_water(self, pin) -> dict | None:
        """Active-slot watering-can level from progress.inventory (fresh, bound, authoritative)."""
        if pin is None:
            return None
        rec = self._obs.get(pin.player_id, "progress.inventory")
        if (rec is None or not rec.is_complete or not rec.authoritative
                or bool(getattr(rec, "stale", False)) or bool(getattr(rec, "requires_resync", False))
                or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
            return None
        slot = rec.facts.get("meta:inventory", {}).get("active_slot")
        if type(slot) is not int:
            return None
        item = rec.facts.get(f"slot:{slot}", {})
        left, maximum = item.get("tool_water_left"), item.get("tool_water_max")
        if (item.get("is_active") is not True or type(left) is not int or type(maximum) is not int
                or not 0 <= left <= maximum <= 100 or maximum < 1):
            return None
        return {"water_left": left, "water_max": maximum}

    # ------------------------------------------------------------------ refresh / settlement

    def _refresh_tool_work(self, goal) -> None:
        progress = getattr(goal, "tool_work", None)
        if progress is None:
            return
        steps = goal.tool_work_steps
        if goal.date_blocked:
            if progress.status in tw.ACTIVE:
                goal.tool_work = replace(progress, status="UNAVAILABLE", reason="date_changed_fresh_criterion_required")
            return
        if goal.tool_work_uncertain:
            if progress.status != "UNCERTAIN" or progress.reason != "original_request_unresolved":
                goal.tool_work = replace(progress, status="UNCERTAIN", reason="original_request_unresolved")
            return
        if progress.status in tw.TERMINAL:
            goal.tool_work = tw.budget(progress, steps)
            return
        pin = self._tool_work_pin_current(goal)
        window = self._tool_work_window(goal)
        water = self._equipped_water(pin)
        if window is not None:
            tick = window["captured_ticks"]
            if tick < goal.tool_work_seen_tick:
                goal.tool_work_uncertain = True
                goal.tool_work = replace(progress, status="UNCERTAIN", reason="capture_clock_regressed")
                self._journal({"ev": "tool_work_capture_regressed", "goal_id": goal.goal_id})
                return
            goal.tool_work_seen_tick = tick
            self._wet_soil_facts(pin, None, window["targets"])  # remember a just-watered tile (native readback)
            use = window.get("last_use") or {}
            action = goal.tool_work_action
            if (steps > goal.tool_work_settled and goal.tool_work_request and action is not None
                    and tick > goal.tool_work_step_tick and use.get("request_id") == goal.tool_work_request
                    and use.get("cycle_observed") is True and use.get("input_completed") is True
                    and use.get("cleanup_confirmed") is True and window["using_tool"] is False
                    and action.lifecycle == "completed" and action.cleanup_confirmed
                    and not tw.settle_pending(progress, water, tick, goal.tool_work_step_tick)):
                goal.tool_work_settled = steps
                before = progress
                progress = tw.settle_step(progress, window, water, request_id=goal.tool_work_request,
                                          captured_ticks=tick)
                self._journal({"ev": "tool_work_step_settled", "goal_id": goal.goal_id,
                               "request_id": goal.tool_work_request, "last_step": progress.last_step,
                               "status": progress.status, "reason": progress.reason})
                if progress.effect and not before.effect:
                    self._journal({"ev": "tool_work_effect", "goal_id": goal.goal_id, "kind": progress.kind,
                                   "reason": progress.effect["reason"], "captured_ticks": tick})
                if progress.last_step == "refilled":
                    self._journal({"ev": "tool_work_refilled", "goal_id": goal.goal_id,
                                   "water_left": (water or {}).get("water_left")})
                if progress.target_ref is None:
                    goal.tool_work_tool = None
            if steps == goal.tool_work_settled:
                phased = tw.refresh_phase(progress, water)
                if phased.status != progress.status:
                    goal.tool_work_tool = None
                    if phased.status == "NEEDS_REFILL":
                        self._journal({"ev": "tool_work_refill_needed", "goal_id": goal.goal_id})
                progress = phased
        goal.tool_work = tw.budget(progress, steps)

    # ------------------------------------------------------------------ gates

    def _tool_work_hard_gate(self, decision, goal) -> str | None:
        """Any mode: a hoe / can / scythe use_tool needs the matching owner tool-work objective."""
        choice = decision.choice
        if not isinstance(choice, ActChoice) or choice.operation != "world_key.use_tool":
            return None
        args = choice.args if isinstance(choice.args, Mapping) else {}
        qid = args.get("qualified_item_id")
        progress = getattr(goal, "tool_work", None) if goal is not None else None
        if qid in tw.TOOL_WORK_ONLY_QIDS and (progress is None or progress.tool_qid != qid):
            return "tool_requires_tool_work_objective"
        return None

    def _tool_work_action_reason(self, request, args) -> str | None:
        """Mirror of _gather_action_reason; pins the target and baseline on the first pass."""
        try:
            command.validate_use_tool_args(args)
        except (ValueError, TypeError, command.KesalahanPerintah):
            return "invalid_tool_work_args"
        goal = self._active
        self._refresh_tool_work(goal)
        progress = goal.tool_work
        if progress.status not in tw.ACTIVE:
            return "tool_work_" + progress.status.lower()
        if not tw.tool_matches(progress, args.get("qualified_item_id")):
            return "tool_work_wrong_tool"
        if args["ticks"] > tw.MAX_TICKS:
            return "tool_work_ticks_exceed_max"
        window = self._tool_work_window(goal)
        if window is None or window["using_tool"]:
            return "tool_work_use_cycle_pending"
        if goal.tool_work_steps != goal.tool_work_settled:
            return "tool_work_use_cycle_pending"
        if window["captured_ticks"] <= goal.tool_work_step_tick:
            return "tool_work_new_capture_required"
        tool = {k: v for k, v in args.items() if k != "ticks"}
        if goal.tool_work_tool is not None and tool != goal.tool_work_tool:
            return "tool_work_tool_or_reach_changed"
        tile_reason = tw.tool_tile_reason(args, window)
        if tile_reason:
            return "tool_work_" + tile_reason
        pin = goal.tool_work_pin
        water = self._equipped_water(pin)
        eligible = tw.eligible_target(progress, args, window, water, pin.location)
        if eligible is None:
            return "tool_work_target_unavailable"
        ref, fact = eligible
        repair_guard = getattr(self, "_debris_route_tool_reason", None)
        repair_reason = repair_guard(ref, args, window) if repair_guard is not None else None
        if repair_reason:
            return repair_reason
        session = getattr(self, "_skill_session", None)
        run = session.current if session is not None and session.status == "active" else None
        if (run is not None and (run.goal_id == goal.goal_id or run.status == "starting"
                                and run.goal_id is None and goal.text == run.objective_text)
                and run.state.get("wave1_driver") == "place_till"
                and ref != run.state.get("wave1_pin", {}).get("ref")):
            return "tool_work_target_replaced"  # exact named S18 tile, including snapshot-time pinning
        if progress.target_ref is not None and ref != progress.target_ref:
            return "tool_work_target_replaced"
        if tw.needs_energy(progress):
            motor = self._state.latest(request.snapshot.binding.player_id)
            energy = getattr(motor, "energy", None) if motor is not None else None
            if not isinstance(energy, (int, float)) or isinstance(energy, bool) or energy < TOOL_WORK_MIN_ENERGY:
                return "tool_work_energy_low"
        if progress.target_ref is None:
            goal.tool_work = tw.pin_target(progress, ref, fact, water)
            self._journal({"ev": "tool_work_target_pinned", "goal_id": goal.goal_id, "ref": ref,
                           "tile": list(goal.tool_work.target_tile or ()), "baseline": goal.tool_work.baseline})
        goal.tool_work_tool = tool
        return None

    _TILL_DIRS = ((0, (0, -1)), (1, (1, 0)), (2, (0, 1)), (3, (-1, 0)))

    def _till_face_invoke(self, request):
        """FIXB16 D-B14-5 (live V2B14HOE-stg2: `cangkul satu petak` with the Hoe active facing tall grass (53,22): the gate
        rightly hid use_tool (`pin_current false`), till has no approach target, the model inspected until HELD): a till
        objective whose faced tile is not tillable turns (1-tick D-pad) to a cardinal neighbour the tool-work window
        reports tillable (no fact on it); each side once. None left: the objective ends UNAVAILABLE
        `no_tillable_tile_adjacent` (journaled) instead of an inspect loop."""
        import json as _json
        with self._lock:
            goal = self._active
            progress = getattr(goal, "tool_work", None) if goal is not None else None
            snap = request.snapshot
            if (progress is None or progress.kind != "till_tile" or progress.status not in tw.ACTIVE
                    or request.goal_id != goal.goal_id or goal.play_mode != "local_activity"
                    or request.reason == "owner_turn"):
                return None
            equipped = getattr(snap, "equipped_tool", None) or {}
            if not equipped or not tw.tool_matches(progress, equipped.get("qualified_item_id")):
                return None
            pending = getattr(goal, "till_face_pending", None)
            if pending is not None:
                # FIXB16 review: the pose after the turn (faced tile + whether the tool-work target pinned) is journaled
                goal.till_face_pending = None
                fx, fy, ff = equipped.get("tile_x"), equipped.get("tile_y"), equipped.get("facing")
                faced = ([fx + self._TILL_DIRS[ff][1][0], fy + self._TILL_DIRS[ff][1][1]]
                         if type(fx) is int and type(fy) is int and type(ff) is int and 0 <= ff <= 3 else None)
                self._journal({"ev": "till_face", "goal_id": goal.goal_id, "status": "turned", "direction": pending,
                               "facing": ff, "tile": faced,
                               "target_pinned": getattr(snap, "tool_work_target", None) is not None})
            if getattr(snap, "tool_work_target", None) is not None:
                return None
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            if not self._v2_pad_path(pin):
                return None  # FIXB16 review: the turn is a v2 pad-path stage (the v1 keyboard path is unchanged)
            window = self._tool_work_window(goal)
            if window is None:
                return None
            x, y, facing = equipped.get("tile_x"), equipped.get("tile_y"), equipped.get("facing")
            if type(x) is not int or type(y) is not int:
                return None
            for direction, (dx, dy) in self._TILL_DIRS:
                if direction == facing or direction in goal.till_faces:
                    continue
                entry = tw._tile_entry(window, x + dx, y + dy)
                if (tw._facts_on(window, x + dx, y + dy) or entry is None or entry.get("tillable") is not True
                        or entry.get("villager") is not False):
                    continue
                goal.till_faces.append(direction)
                name = ("up", "right", "down", "left")[direction]
                goal.till_face_pending = name
                self._journal({"ev": "till_face", "goal_id": goal.goal_id, "direction": name,
                               "tile": [x + dx, y + dy], "tried": len(goal.till_faces)})
                choice = {"kind": "act", "operation": "world_key.move_cardinal", "target_ref": None,
                          "args": {"direction": name, "ticks": 1}, "evidence_refs": []}
                activity = {"activity": "objective step (till_face)", "purpose": "the owner's objective", "target": None,
                            "next_step": "world_key.move_cardinal"}
                return _json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                    "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                    "speech": "", "choice": choice, "activity": activity}, ensure_ascii=False)
            goal.tool_work = replace(progress, status="UNAVAILABLE", reason="no_tillable_tile_adjacent")
            self._journal({"ev": "till_face", "goal_id": goal.goal_id, "status": "no_tillable_tile_adjacent",
                           "tile": [x, y], "tried": len(goal.till_faces)})
            return None

    TOOL_WORK_RULE_STEPS_MAX = 40
    TOOL_WORK_RULE_TARGETS_MAX = 2
    TOOL_WORK_RULE_SELECTS_MAX = 2
    TOOL_WORK_RULE_EMPTY_MAX = 3

    def _tool_work_scene_age(self, pin) -> float | None:
        """FIXB18 review: seconds since the bound scene record arrived (None: no record for this session)."""
        try:
            rec = self._obs.get(pin.player_id, "scene.current_location") if pin is not None else None
            if rec is None or getattr(rec, "session_id", pin.observation_session_id) != pin.observation_session_id:
                return None
            received = getattr(rec, "received_monotonic", None)
            return None if received is None else max(0.0, time.monotonic() - received)
        except Exception:  # noqa: BLE001 - a diagnostic read never raises
            return None
    _FACE = {(0, -1): 0, (1, 0): 1, (0, 1): 2, (-1, 0): 3}

    def _tool_work_offered_entries(self, pin, snap) -> list:
        """FIXB18: the snapshot's approach entries whose scene fact is this objective's own kind (tw.approach_rank 1),
        nearest first."""
        goal = self._active
        progress = getattr(goal, "tool_work", None)
        rec = self._obs.get(pin.player_id, "scene.current_location") if pin is not None else None
        facts = rec.facts if rec is not None and isinstance(getattr(rec, "facts", None), Mapping) else {}
        if getattr(self, "_live7_active", lambda: False)():
            # The bounded local window remains authoritative on a clipped Farm.
            # A global scene omission must not discard its offered dry crop.
            window = self._tool_work_window(goal)
            if window is not None:
                facts = self._wet_soil_facts(pin, facts, window["targets"])
        motor = getattr(snap, "motor", None)
        here = getattr(motor, "tile", None)
        out = []
        repair_run = getattr(self, "_debris_route_run", lambda: None)()
        for entry in getattr(snap, "approach_targets", ()) or ():
            if not isinstance(entry, Mapping) or tw.approach_rank(progress, facts.get(entry.get("ref"))) != 1:
                continue
            if repair_run is not None and entry.get("ref") != repair_run.state["route_clear"]["target"]["ref"]:
                continue
            tile = entry.get("tile")
            if not (isinstance(tile, (list, tuple)) and len(tile) == 2 and all(type(v) is int for v in tile)):
                continue
            d = abs(tile[0] - here[0]) + abs(tile[1] - here[1]) if here is not None else 0
            out.append((d, str(entry["ref"]), entry))
        return [e for _, _, e in sorted(out, key=lambda t: (t[0], t[1]))]

    def _tool_work_rule_invoke(self, request):
        """FIXB18 D-B15a-1 (live V2B15aACC2a / V2B15aT6-a / V2B14cTILL-a: the tool equipped, the objective's targets
        offered as approach entries, `use_tool_gate offered false target_pinned false` unless ARTI already faced one, and
        no rule approached them: the model inspected until HELD). On the v2 pad path the tool-work objective is driven
        end to end, no model turn: select the tool -> the gate offers the faced target: use_tool -> else approach the
        nearest offered target of the kind (stone / weeds or grass / dry dirt) -> face it -> one settle read -> the
        target failed (blocked for the goal), the next one; <= TOOL_WORK_RULE_TARGETS_MAX targets per effect, then
        UNAVAILABLE `no_reachable_tool_work_target`. Till turns at its own stage (till_face); refill is not driven."""
        with self._lock:
            goal = self._active
            progress = getattr(goal, "tool_work", None) if goal is not None else None
            if (progress is None or progress.status not in tw.ACTIVE or request.goal_id != goal.goal_id
                    or goal.progress_marker == "terminal" or goal.play_mode != "local_activity"
                    or request.reason == "owner_turn" or progress.kind == "refill_can" or tw.refill_phase(progress)):
                return None
            snap = request.snapshot
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            if not self._v2_pad_path(pin) or self.curfew_window_now():
                return None
            rule = goal.tool_work_rule
            if rule is None or rule.get("effect") != len(goal.tool_work_done):
                rule = goal.tool_work_rule = {"steps": (rule or {}).get("steps", 0), "effect": len(goal.tool_work_done),
                                              "tried": [], "target": None, "settles": 0, "faces": 0,
                                              "selects": (rule or {}).get("selects", 0)}
            rule["steps"] += 1
            if rule["steps"] > self.TOOL_WORK_RULE_STEPS_MAX:
                if rule["steps"] == self.TOOL_WORK_RULE_STEPS_MAX + 1:
                    self._journal({"ev": "tool_work_rule", "goal_id": goal.goal_id, "status": "steps_max"})
                return None
            equipped = getattr(snap, "equipped_tool", None) or {}
            if rule.get("face_pending") is not None:
                # FIXB18 review: the pose after the face nudge (faced tile + whether the gate pinned the target)
                fx, fy, ff = equipped.get("tile_x"), equipped.get("tile_y"), equipped.get("facing")
                d = {0: (0, -1), 1: (1, 0), 2: (0, 1), 3: (-1, 0)}.get(ff) if type(ff) is int else None
                self._journal({"ev": "tool_work_rule", "goal_id": goal.goal_id, "status": "faced",
                               "direction": rule.pop("face_pending"), "facing": ff,
                               "tile": [fx + d[0], fy + d[1]] if d and type(fx) is int and type(fy) is int else None,
                               "target_pinned": getattr(snap, "tool_work_target", None) is not None})

            def act(operation, args, label, target_ref=None):
                self._journal({"ev": "tool_work_rule", "goal_id": goal.goal_id, "status": label, "kind": progress.kind,
                               "operation": operation, "target": target_ref or rule.get("target"), "n": rule["steps"]})
                return self._rule_json(request, {"kind": "act", "operation": operation, "target_ref": target_ref,
                                                 "args": args, "evidence_refs": []}, "tool_work_rule", label)
            if not tw.tool_matches(progress, equipped.get("qualified_item_id")):
                item = next((i for i in getattr(snap, "selectable_items", ()) or () if isinstance(i, Mapping)
                             and tw.tool_matches(progress, i.get("qualified_item_id"))), None)
                if (item is None or not getattr(snap, "select_item_available", False)
                        or rule["selects"] >= self.TOOL_WORK_RULE_SELECTS_MAX):
                    return None
                rule["selects"] += 1
                return act("select_item", dict(item), "select_tool")
            if getattr(snap, "tool_work_target", None) is not None:
                if not getattr(snap, "use_tool_available", False):
                    return None
                args = {k: equipped[k] for k in ("slot", "qualified_item_id", "tile_x", "tile_y", "facing")}
                args["ticks"] = 1
                rule["settles"] = 0
                return act("world_key.use_tool", args, "swing")
            if progress.kind == "till_tile":
                return None  # till_face turned or ended it (a tillable tile is not an approach fact)
            offered = [e for e in self._tool_work_offered_entries(pin, snap) if e["ref"] not in rule["tried"]
                       or e["ref"] == rule.get("target")]
            target = next((e for e in offered if e["ref"] == rule.get("target")), None)
            approach = goal.approach_progress or {}
            if target is not None and approach.get("status") == "ARRIVED" and approach.get("target_ref") == target["ref"]:
                # at the target's stand: face it, one settle read, else the target fails
                x, y = equipped.get("tile_x"), equipped.get("tile_y")
                need = self._FACE.get((target["tile"][0] - x, target["tile"][1] - y)) if type(x) is int and type(y) is int else None
                if need is not None and equipped.get("facing") != need and rule["faces"] < 2:
                    rule["faces"] += 1
                    rule["face_pending"] = ("up", "right", "down", "left")[need]
                    return act("world_key.move_cardinal", {"direction": ("up", "right", "down", "left")[need], "ticks": 1},
                               "face")
                if rule["settles"] < 1:
                    rule["settles"] += 1
                    from arti_stardew_world_extras_runtime import skills_inspect
                    return self._rule_json(request, dict(skills_inspect(), question="tool work: is the faced target pinned now?"),
                                           "tool_work_rule", "settle")
                goal.approach_blocked_refs.add(target["ref"])
                why = None
                try:
                    window = self._tool_work_window(goal)
                    args = {k: equipped.get(k) for k in ("slot", "qualified_item_id", "tile_x", "tile_y", "facing")}
                    why = tw.eligibility_miss(progress, args, window) if window is not None else "window_unavailable"
                except Exception:  # noqa: BLE001 - a diagnostic never raises
                    why = None
                rule["last_miss"] = why
                self._journal({"ev": "tool_work_rule", "goal_id": goal.goal_id, "status": "target_failed",
                               "target": target["ref"], "tile": target["tile"], "why": why})
                rule["target"] = None
                target = None
                offered = [e for e in offered if e["ref"] not in goal.approach_blocked_refs]
            if target is None:
                age = self._tool_work_scene_age(pin)
                if not offered and len(rule["tried"]) < self.TOOL_WORK_RULE_TARGETS_MAX:
                    # FIXB18 review MAJOR: an empty / stale catalog is re-read before the goal ends: end only after a
                    # fresh (< 2 s) scene showed nothing on a second look, or after EMPTY_MAX empty decisions.
                    rule["empty"] = rule.get("empty", 0) + 1
                    fresh = age is not None and age < 2.0
                    if not ((fresh and rule["empty"] >= 2) or rule["empty"] >= self.TOOL_WORK_RULE_EMPTY_MAX):
                        self._journal({"ev": "tool_work_rule", "goal_id": goal.goal_id, "status": "no_target_refresh",
                                       "empty": rule["empty"], "scene_age_s": None if age is None else round(age, 2)})
                        from arti_stardew_world_extras_runtime import skills_inspect
                        return self._rule_json(request, dict(skills_inspect(), question="tool work: refresh the targets"),
                                               "tool_work_rule", "refresh")
                if len(rule["tried"]) >= self.TOOL_WORK_RULE_TARGETS_MAX or not offered:
                    reason = "no_reachable_tool_work_target" if rule["tried"] else "no_tool_work_target_offered"
                    if rule["tried"] and str(rule.get("last_miss") or "").startswith("scythe_area_unsafe"):
                        reason = "no_safe_tool_work_target"  # FIXB20 D-B17-2: the sweep would hit a sapling / crop
                    goal.tool_work = replace(progress, status="UNAVAILABLE", reason=reason)
                    self._journal({"ev": "tool_work_rule", "goal_id": goal.goal_id, "status": reason,
                                   "tried": list(rule["tried"]), "empty": rule.get("empty", 0),
                                   "scene_age_s": None if age is None else round(age, 2)})
                    return None
                rule["empty"] = 0
                target = offered[0]
                rule.update(target=target["ref"], settles=0, faces=0)
                rule["tried"].append(target["ref"])
            return act("approach_target", {}, "approach", target_ref=target["ref"])

    # ------------------------------------------------------------------ FIXB31 D-SOAK3-1: the gather chain's tool select
    GATHER_TOOL_SELECTS_MAX = 2
    GATHER_TOOLS = {"(O)388": "(T)Axe", "(O)390": "(T)Pickaxe"}  # the base tool (journal / backpack fallback name)

    def _gather_tool_rule_invoke(self, request):
        """FIXB31 D-SOAK3-1 (live V2SOAK1A3 t3 / t4: after the `tools` trial the Pickaxe stayed in hand; the wood / chest
        goals' gate offers the tree only to the Axe (`use_tool_gate equipped_axe false offered false`), the model never
        took the offered select and inspected / asked the owner until HELD with 0-2 inputs). On the v2 pad path a gather /
        episode / one-tree / store goal whose resource still needs work (WORK_REMAINING) selects its owned tool first, no
        model turn: the Axe for wood, the Pickaxe for stone (toolbar: select_item; backpack only: the locked
        item_to_toolbar stage once, its end resumes here). Bounded (GATHER_TOOL_SELECTS_MAX selects, one stage per goal);
        the tool in hand, an owner turn, the exit leg, the store phase, flag off: None (unchanged)."""
        with self._lock:
            goal = self._active
            progress = getattr(goal, "gather_progress", None) if goal is not None else None
            if (progress is None or progress.status != "WORK_REMAINING" or request.goal_id != goal.goal_id
                    or goal.progress_marker == "terminal" or goal.play_mode != "local_activity"
                    or request.reason == "owner_turn" or getattr(goal, "tool_work", None) is not None
                    or self._exit_leg_open(goal)):
                return None
            required = self.GATHER_TOOLS.get(progress.qualified_item_id)
            from arti_stardew_runtime import GATHER_TOOL_QIDS
            accepted = GATHER_TOOL_QIDS.get(progress.qualified_item_id, ())  # review: Copper / Steel / Gold / Iridium too
            snap = request.snapshot
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            if required is None or not self._v2_pad_path(pin) or self.curfew_window_now():
                return None
            equipped = getattr(snap, "equipped_tool", None) or {}
            if equipped.get("qualified_item_id") in accepted:
                return None
            rule = goal.gather_tool_rule
            if rule is None:
                rule = goal.gather_tool_rule = {"selects": 0, "staged": None}
            held = equipped.get("qualified_item_id")
            item = next((dict(i) for i in getattr(snap, "selectable_items", ()) or () if isinstance(i, Mapping)
                         and i.get("qualified_item_id") in accepted), None)
            if item is not None:
                if not getattr(snap, "select_item_available", False) or rule["selects"] >= self.GATHER_TOOL_SELECTS_MAX:
                    return None
                rule["selects"] += 1
                self._journal({"ev": "gather_tool_rule", "goal_id": goal.goal_id, "status": "select_tool",
                               "qid": item.get("qualified_item_id"), "slot": item.get("slot"), "held": held,
                               "n": rule["selects"]})
                return self._rule_json(request, {"kind": "act", "operation": "select_item", "target_ref": None,
                                                 "args": item, "evidence_refs": []}, "gather_tool_rule", "select_tool")
            if rule["staged"] is not None:
                return None  # the stage ran once: the model's turn (its catalog is unchanged)
            _, pid = self._binding_view()
            inventory = self._obs.get(pid, "progress.inventory") if pid is not None else None
            inv = inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping) else {}
            from arti_stardew_combat import TOOLBAR_SLOTS
            owned = next((f for _, f in sorted(inv.items()) if isinstance(f, Mapping) and f.get("occupied") is True
                          and f.get("qualified_item_id") in accepted and type(f.get("slot")) is int), None)
            if owned is None or owned["slot"] < TOOLBAR_SLOTS:
                return None  # not owned, or in the toolbar but not offered yet: unchanged (the model / a fresh read)
            rule["staged"] = owned["slot"]
            required = owned["qualified_item_id"]
            goal.item_stage_want = required
            if (goal.item_stage or {}).get("stop"):
                goal.item_stage = {}  # an earlier stage's stop never blocks this move (as clear_path)
            self._stage_end_resume("item_to_toolbar", goal, force=True)  # its end resumes this rule (select)
            self._journal({"ev": "gather_tool_rule", "goal_id": goal.goal_id, "status": "tool_to_toolbar",
                           "qid": required, "slot": owned["slot"], "held": held})
            return self._rule_json(request, {"kind": "wait", "reason": "item_to_toolbar", "ms": 0},
                                   "gather_tool_rule", "tool_to_toolbar")

    def _gather_tool_repin(self, goal) -> None:
        """FIXB31: the item_to_toolbar stage opened and closed the GameMenu, which moves ui.context's ui_revision; the
        gather pin (exact MovementContext equality) would never match again. Once this rule staged its tool, a pin that
        differs only in ui_revision is re-pinned (settled steps only, nothing uncertain). Caller: the snapshot build,
        before the refreshes."""
        rule = getattr(goal, "gather_tool_rule", None)
        pin = getattr(goal, "gather_pin", None)
        if (not rule or rule.get("staged") is None or pin is None or goal.gather_steps != goal.gather_settled
                or goal.gather_uncertain):
            return
        current = self._water_context(pin.player_id)
        if current is not None and current != pin and replace(current, ui_revision=pin.ui_revision) == pin:
            goal.gather_pin = current
            self._journal({"ev": "gather_tool_rule", "goal_id": goal.goal_id, "status": "repinned",
                           "ui_revision": [pin.ui_revision, current.ui_revision]})

    def _tool_work_target(self, goal, equipped_tool) -> str | None:
        """Snapshot: the ref the gate would accept on the exact faced tile, or None (never on the wire)."""
        progress = getattr(goal, "tool_work", None) if goal is not None else None
        if (progress is None or equipped_tool is None or progress.status not in tw.ACTIVE
                or not tw.tool_matches(progress, equipped_tool.get("qualified_item_id"))):
            return None
        try:
            window = self._tool_work_window(goal)
            if window is None:
                return None
            eligible = tw.eligible_target(progress, equipped_tool, window,
                                          self._equipped_water(goal.tool_work_pin), goal.tool_work_pin.location)
        except Exception:  # noqa: BLE001 — a read failure advertises nothing
            return None
        if eligible is None or (progress.target_ref is not None and eligible[0] != progress.target_ref):
            return None
        repair_guard = getattr(self, "_debris_route_tool_reason", None)
        if repair_guard is not None and repair_guard(eligible[0], equipped_tool, window):
            return None
        return eligible[0]

    @staticmethod
    def _tool_work_offer_reason(snap) -> str | None:
        """Why use_tool is hidden for tool work (None = offer). Only when use_tool is otherwise available."""
        if not snap.use_tool_available or snap.equipped_tool is None:
            return None
        progress = snap.working_memory.tool_work
        reason = tw.offer_reason(progress, snap.equipped_tool.get("qualified_item_id"), snap.tool_work_target)
        if reason is None and progress is not None and tw.needs_energy(progress):
            energy = snap.motor.energy
            if not isinstance(energy, (int, float)) or isinstance(energy, bool) or energy < TOOL_WORK_MIN_ENERGY:
                reason = "tool_work_energy_low"
        return reason

    @staticmethod
    def _tool_work_act_offered(goal, request) -> bool:
        """D3 rule layer: use_tool is offered now on the faced tool-work target (pinned, if pinned)."""
        progress = getattr(goal, "tool_work", None)
        snap = request.snapshot
        if progress is None or snap.tool_work_target is None:
            return False
        if progress.target_ref is not None and snap.tool_work_target != progress.target_ref:
            return False
        return ToolWorkRuntimeMixin._tool_work_offer_reason(snap) is None

    # ------------------------------------------------------------------ publication / accounting

    def _tool_work_before_write(self, goal, request, args) -> None:
        """Inside the guarded write(): re-check, then account the step before publication."""
        reason = self._tool_work_action_reason(request, args)
        if reason:
            raise ValueError(reason)
        window = self._tool_work_window(goal)
        if window is None:  # the record aged out between the gate and here: nothing is written
            raise ValueError("tool_work_use_cycle_pending")
        goal.tool_work_step_tick = window["captured_ticks"]
        goal.tool_work_steps += 1
        goal.tool_work_request = None
        goal.tool_work_action = None

    def _tool_work_record_result(self, goal, evidence, superseded: bool) -> None:
        """Under the lock, after a submitted step. Never replays; doubt latches the global stop."""
        goal.tool_work_request = evidence.request_id
        if evidence.request_id and evidence.request_id not in goal.tool_work_requests:
            goal.tool_work_requests += (evidence.request_id,)
        goal.tool_work_action = evidence
        rejected_clean = (evidence.lifecycle == "rejected" and evidence.cleanup_confirmed and not superseded
                          and evidence.world_effect_status != "uncertain")
        if (rejected_clean and evidence.reason != "exhausted"
                and not any(code in str(evidence.reason or "") for code in _TOOL_REFUSAL_CODES)):
            # Review fix (WS1 minor 4): a non-tool rejection (busy, lease, binding, cursor) is not_taken:
            # nothing pressed, no whiff and no budget; the next decision may simply retry.
            goal.tool_work_steps -= 1
            goal.tool_work_settled = goal.tool_work_steps
            goal.tool_work = replace(goal.tool_work, last_step="not_taken",
                                     reason="mod_rejected:" + str(evidence.reason or "rejected")[:80])
            self._journal({"ev": "tool_work_step_settled", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                           "last_step": "not_taken", "status": goal.tool_work.status, "reason": goal.tool_work.reason})
        elif rejected_clean:
            # The mod's tool guard refused before any Press: zero effect, no pause; counts as a whiff.
            goal.tool_work_settled = goal.tool_work_steps
            progress = goal.tool_work
            whiffs = progress.whiffs + 1
            refused = "mod_refused:" + str(evidence.reason or "rejected")[:80]
            goal.tool_work = replace(progress, whiffs=whiffs, last_step="refused", settled_steps=progress.settled_steps + 1,
                                     **(dict(status="UNAVAILABLE", reason=refused) if (whiffs >= tw.MAX_WHIFFS
                                        or evidence.reason == "exhausted" or str(evidence.reason or "").startswith("ArmRejected (PRECONDITION_UNAVAILABLE("))
                                        else dict(reason=refused)))
            self._journal({"ev": "tool_work_step_settled", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                           "last_step": "refused", "status": goal.tool_work.status, "reason": refused})
        elif (evidence.lifecycle != "completed" or not evidence.cleanup_confirmed
                or evidence.world_effect_status == "uncertain" or superseded):
            goal.tool_work_uncertain = True
            self._gather_mutation_unresolved = True
        self._refresh_tool_work(goal)

    # ------------------------------------------------------------------ batching / finish

    def _tool_work_batch_stop(self, request, goal) -> str | None:
        """Under the lock: tool-specific stop rules; only break_rock continues on observed damage."""
        self._refresh_tool_work(goal)
        progress = goal.tool_work
        if progress.status != "WORK_REMAINING":
            return "tool_work_" + progress.status.lower()
        if goal.tool_work_steps >= tw.KINDS[progress.kind][1]:
            return "tool_work_step_budget"
        if goal.tool_work_steps != goal.tool_work_settled:
            return None  # the gate waits for this step's settled capture
        if progress.last_step in {"whiff", "refused", "not_taken"}:
            return "tool_work_whiff"
        if progress.last_step == "refilled" or progress.target_ref is None:
            return "target_gone"
        if tw.needs_energy(progress):
            motor = self._state.latest(request.snapshot.binding.player_id)
            energy = getattr(motor, "energy", None) if motor is not None else None
            if not isinstance(energy, (int, float)) or isinstance(energy, bool) or energy < TOOL_WORK_MIN_ENERGY:
                return "energy_low"
        return None

    def _tool_work_finish(self, goal) -> tuple[str, str]:
        """(verdict status, reason): achieved only on an observed effect of a settled step."""
        self._refresh_tool_work(goal)
        progress = goal.tool_work
        if self._gather_mutation_unresolved or goal.tool_work_uncertain or progress.status == "UNCERTAIN":
            return "uncertain", progress.reason
        if progress.status == "UNAVAILABLE":  # review fix (WS1 minor 4): settled, honest, not achieved
            return "partial", progress.reason
        if progress.status == "EFFECT_OBSERVED" and progress.effect:
            if len(goal.tool_work_done) + 1 < goal.tool_work_count:
                return "partial", "tool_work_count_not_reached"  # FIXB17 review: N effects asked, fewer verified
            return "achieved", "observed_tool_effect"
        return "partial", progress.reason

    @staticmethod
    def _tool_work_counted(goal) -> dict:
        """FIXB17 review: a counted goal's verdict carries the count and every verified effect (also when partial)."""
        if getattr(goal, "tool_work_count", 1) <= 1:
            return {}
        effects = list(goal.tool_work_done)
        progress = goal.tool_work
        effect = getattr(progress, "effect", None) or {}
        if getattr(progress, "status", None) == "EFFECT_OBSERVED" and effect:
            effects.append({"target_ref": effect.get("target_ref"), "effect": effect.get("reason"),
                            "request_id": effect.get("request_id")})
        return {"count": goal.tool_work_count, "effects": effects}

    # ------------------------------------------------------------------ deterministic finish (D35)

    def process_tool_work_finish(self) -> int:
        """Worker hook (live acceptance ACC2a, D35): the rock was removed by one background swing and the
        model never chose finish. Once the pinned target's effect is observed on a settled step, the
        runtime records the verified finish itself, like WS3b's routine_sleep_rule. The model finish path
        is unchanged; this skips a goal that is already terminal."""
        try:
            result = self._tool_work_rule_finish()
            if getattr(self, "_tool_work_next_due", False):
                self._tool_work_next_due = False
                self._resume_after_pause(reason="tool_work_next")
                return 1
            if result is not None and self._on_generation_result is not None:
                from types import SimpleNamespace
                self._on_generation_result(SimpleNamespace(reason="tool_work_finish", generation_id=None), result)
            return 1 if result is not None else 0
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            self._journal({"ev": "tool_work_finish_error", "error": type(exc).__name__})
            return 0

    def tool_work_finish_due(self) -> bool:
        """Caller holds the runtime lock (the trial runner's idle check): an observed effect still waits for
        the worker's rule finish, so the runner must not declare the goal held meanwhile."""
        goal = self._active
        progress = getattr(goal, "tool_work", None) if goal is not None else None
        return (progress is not None and goal.progress_marker != "terminal"
                and progress.status == "EFFECT_OBSERVED" and bool(progress.effect)
                and not goal.tool_work_uncertain and not self._gather_mutation_unresolved)

    def _tool_work_rule_finish(self):
        """Terminal achieved verdict for an observed tool-work effect, or None (not yet / not applicable).

        Waits while a decision, action or delivered result is in flight, so a model finish already on its
        way is never raced; never fires on doubt (unresolved input, unsettled step, effect off the pin)."""
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        with self._lock:
            goal = self._active
            progress = getattr(goal, "tool_work", None) if goal is not None else None
            if progress is None or goal.progress_marker == "terminal":
                return None
            if self._action_busy or self._inflight_generation is not None or getattr(self, "_results_pending", 0):
                return None
            if (self._gather_mutation_unresolved or goal.tool_work_uncertain
                    or goal.tool_work_steps != goal.tool_work_settled):
                return None
            self._refresh_tool_work(goal)
            progress = goal.tool_work
            effect = progress.effect or {}
            if (progress.status != "EFFECT_OBSERVED" or not effect or effect.get("target_ref") is None
                    or effect.get("target_ref") != progress.target_ref):
                return None
            if (not self._gather_mutation_unresolved and not goal.tool_work_uncertain
                    and self._tool_work_next(goal, effect)):
                self._pending_continuation = None
                self._tool_work_next_due = True  # FIXB17: one fresh inspect + continuation drives the next one
                return None
            status, reason = self._tool_work_finish(goal)
            if (status, reason) != ("achieved", "observed_tool_effect"):
                return None
            counted = self._tool_work_counted(goal)
            verdict = FinishEvidenceView("achieved", "observed_tool_effect", {
                "origin": "tool_work_rule", "kind": progress.kind, "effect": effect.get("reason"),
                "target_ref": effect.get("target_ref"), "request_id": effect.get("request_id"),
                "captured_ticks": effect.get("captured_ticks"), **counted})
            goal.last_finish = verdict
            goal.progress_marker = "terminal"
            goal.revision += 1
            self._pending_continuation = None
            self._pending_event_reason = None
            self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                           "verdict": asdict(verdict), "origin": "tool_work_rule"})
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                               speech=StatusSpeech("Efek alatnya sudah terverifikasi dari pengamatan."), finish_evidence=verdict)

    # ------------------------------------------------------------------ provider / journal projections

    @staticmethod
    def _tool_work_memory_wire(memory_payload: dict, snap) -> None:
        progress = snap.working_memory.tool_work
        if progress is None:
            memory_payload.pop("tool_work", None)
        else:
            memory_payload["tool_work"] = tw.wire(progress, snap.equipped_water)

    def _tool_work_inspect(self, facts: dict, window: Mapping | None) -> None:
        goal = self._active
        progress = getattr(goal, "tool_work", None) if goal is not None else None
        if isinstance(facts.get("gather_window"), dict) and isinstance(window, Mapping):
            hit = window.get("tool_tile")
            if isinstance(hit, Mapping):
                facts["gather_window"]["tool_tile"] = dict(hit)
            tiles = window.get("tiles")
            if isinstance(tiles, (list, tuple)):
                facts["gather_window"]["tillable_tiles"] = [[t["x"], t["y"]] for t in tiles if t.get("tillable") is True]
                facts["gather_window"]["water_refill_tiles"] = [[t["x"], t["y"]] for t in tiles
                                                               if t.get("water_refill") is True]
        if progress is not None:
            self._refresh_tool_work(goal)
            facts["tool_work"] = tw.wire(goal.tool_work, self._equipped_water(goal.tool_work_pin))

    def _tool_work_gate_fields(self, goal, window, equipped_tool) -> dict:
        """Journal-only use_tool_gate diagnostics (R10); booleans and small ints."""
        progress = getattr(goal, "tool_work", None)
        hit = window.get("tool_tile") if isinstance(window, Mapping) else None
        faced = tw.faced_tile(equipped_tool) if equipped_tool is not None else None
        water = self._equipped_water(goal.tool_work_pin) if progress is not None else None
        return {"equipped_qid": equipped_tool.get("qualified_item_id") if equipped_tool else None,
                "tool_tile": [hit["x"], hit["y"]] if isinstance(hit, Mapping) else None,
                "tool_tile_adjacent": (faced is not None and isinstance(hit, Mapping)
                                       and (hit.get("x"), hit.get("y")) == faced),
                "tool_work_kind": progress.kind if progress else None,
                "tool_work_status": progress.status if progress else None,
                "water_left": water["water_left"] if water else None}

    @staticmethod
    def _tool_work_progress_facts(goal) -> tuple | None:
        progress = getattr(goal, "tool_work", None)
        if progress is None:
            return None
        return (progress.kind, progress.status, progress.target_ref, progress.settled_steps, progress.last_step)
