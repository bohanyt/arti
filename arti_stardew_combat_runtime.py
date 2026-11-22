"""Primitives v2 WS-G5 combat + tool-charge hooks for StardewGoalRuntime (mixin).

All decisions live in the pure module ``arti_stardew_combat``; this mixin reads the existing observation/session stores
through the runtime's own helpers and keeps the objective accounting. The physical step is the unchanged leased executor
(``_execute_leased_action(..., reflex_g5=<operation>)``: lease, guarded publication, renewal while the mod runs the reflex,
cleanup supervisor). The heal hand-off (``Aborted(heal_needed)``) pins the existing WS3b eat objective on the same goal so
the runtime's own ``routine_eat_rule`` chain eats, then unpins it and the combat objective may re-arm. Methods named
``_combat_*`` / ``_charge_*`` / ``*combat_*`` only (merge-friendly). Evidence ceiling: UNIT_TESTED.
"""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import asdict, replace

import arti_stardew_combat as combat
import arti_stardew_interact_b as ib


class CombatRuntimeMixin:
    """Owner objectives 'kill one slime' / 'charge the hoe to level 2' -> one offered reflex -> combat_rule / charge_rule."""

    # ------------------------------------------------------------------ objective / observation

    def _combat_mines_handoff(self, goal, reason):
        """ADR 171: an open settled batch cannot become an aggro/inspection wait."""
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        batch = run.state.get("mines_last_batch", {}) if run is not None else {}
        if (getattr(session, "status", None) != "active" or run is None
                or run.call.name != "lawan_mine" or run.goal_id != goal.goal_id
                or batch.get("goal_id") != goal.goal_id or batch.get("killed")):
            return False
        run.state["mines_idle_handoff"] = "mines_combat_resume_unavailable:" + reason
        run.pending = ("fail", run.state["mines_idle_handoff"])
        self._journal(dict(ev="mines_combat_handoff", goal_id=goal.goal_id, reason=reason,
                           batch=batch.get("batches"), outcome="page"))
        return True

    def _combat_begin(self, goal, request, parsed) -> None:
        count, kind = parsed
        goal.combat = combat.CombatObjective(goal.goal_id, kind=kind, count=count)
        self._journal({"ev": "reflex_combat_objective_pinned", "goal_id": goal.goal_id, "target": kind, "count": count,
                       "max_calls": combat.MAX_COMBAT_CALLS})

    def _charge_begin(self, goal, request, parsed) -> None:
        tool, level, tiles = parsed
        goal.charge = combat.ChargeObjective(goal.goal_id, tool=tool, level=level, tiles=tiles)
        self._journal({"ev": "reflex_charge_objective_pinned", "goal_id": goal.goal_id, "tool": tool, "level": level,
                       "tiles": tiles, "max_calls": combat.MAX_CHARGE_CALLS})

    def _combat_ui_facts(self, pin) -> Mapping | None:
        """Fresh (<= 2 s), complete, bound ui.context facts of the pinned player, or None."""
        if pin is None:
            return None
        rec = self._obs.get(pin.player_id, "ui.context")
        if (rec is None or not rec.is_complete or not rec.authoritative
                or rec.session_id != pin.observation_session_id or rec.screen_id != pin.screen_id
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
            return None
        return rec.facts

    def _combat_extra_entries(self, pin, motor_tile) -> list[dict]:
        """D-B4-6: monster approach entries (outside the reflex radius) while the combat objective is open."""
        goal = self._active
        objective = getattr(goal, "combat", None) if goal is not None else None
        entries = combat.monster_entries(self._combat_ui_facts(pin), motor_tile, objective)
        entries = entries + self._clear_path_stand_entries(goal)  # FIXB21 D-B18-1
        refs = tuple(e["ref"] for e in entries)
        if refs and refs != getattr(self, "_last_monster_refs", ()):
            self._last_monster_refs = refs
            self._journal({"ev": "combat_monster_approach_offered", "goal_id": goal.goal_id, "refs": list(refs)})
        return entries

    def _combat_weapon_facts(self, goal, pin) -> dict | None:
        """D-B3-6: weapon held / weapon slot for the open combat objective's snapshot summary (None otherwise)."""
        objective = getattr(goal, "combat", None) if goal is not None else None
        if not combat.combat_open(objective):
            return None
        inventory = self._obs.get(pin.player_id, "progress.inventory") if pin is not None else None
        return combat.weapon_facts(self._combat_ui_facts(pin), inventory.facts if inventory is not None else None)

    # ------------------------------------------------------------------ D-B5-2 / D-B7b-2: item_to_toolbar stage
    # One locked worker stage moves a needed item from the backpack into the toolbar through the GameMenu inventory page
    # (Start, nav, A pick, nav, A place / swap back, B), every step the same leased pad_button / ui_nav / ui_press as a
    # model step. Purposes: the combat weapon (D-B5-2) and, generalised for D-B7b-2 (live V2B7bPLANT-a: the seeds sat in
    # backpack slot 16 and were never selectable), the extras seed, the fishing rod and a skills `pegang` tool. Same lock,
    # caps, 60 s ownership cap and place-back rules for every purpose (FIXB6 / FIXB8).
    WEAPON_STAGE_LOCK_S = 60.0
    ITEM_STAGE_PURPOSES = ("weapon", "item")

    def _stage_end_resume(self, stage_name: str, goal=None, *, force: bool = False) -> None:
        """Caller holds the lock. FIXB28 D-B25-1 (live V2B25EAT-a: the `food_to_toolbar` item stage ended `done
        menu_closed` and the eat goal was never resumed -> HELD continuation_not_ready): a worker stage that owns the goal
        records itself as the last stage lock, so process_stage_lock releases it at the stage's end and resumes the owning
        rule's next step (one fresh inspect + continuation). Shared by the weapon / item / clear_path / eat stages; a
        stage already recorded is kept (``force``: a stage that starts now replaces it, as the clear_path / eat
        hand-offs did)."""
        if force or getattr(self, "_stage_lock_last", None) is None:
            self._stage_lock_last = stage_name
            goal = goal if goal is not None else self._active
            self._journal({"ev": "stage_lock", "goal_id": getattr(goal, "goal_id", None), "stage": stage_name,
                           "status": "locked", "origin": "stage_owner"})

    def _item_stage_dict(self, goal, purpose: str) -> dict:
        return goal.combat_weapon_stage if purpose == "weapon" else goal.item_stage

    def _item_stage_ev(self, purpose: str) -> str:
        return "combat_weapon_stage" if purpose == "weapon" else "item_stage"

    def _combat_weapon_stage_ready(self):
        """Under the lock: (goal, weapon facts) when the weapon stage may step NOW (idle terms + the shared terms)."""
        return self._item_stage_ready("weapon")

    def _item_stage_ready(self, purpose: str):
        from arti_stardew_runtime import READY
        goal = self._active
        if (goal is None or self._status != READY or self._worker_stop.is_set() or self._day_start_active
                or self._action_busy or self._inflight_generation is not None or getattr(self, "_results_pending", 0)
                or self._trial_result_pending()):
            return None
        target = self._item_stage_target(goal, purpose)
        return None if target is None else (goal, target)

    def _combat_weapon_stage_owns(self, goal) -> bool:
        """D-B6-1: the weapon stage owns the goal. Same stage terms as the worker (FIXB8 review MAJOR: they disagreed and
        every request was answered `wait stage_lock` forever); the idle terms only delay a step, the 60 s cap ends it."""
        return self._item_stage_target(goal, "weapon") is not None

    def _item_stage_owns(self, goal) -> bool:
        return self._item_stage_target(goal, "item") is not None

    def _weapon_stage_target(self, goal):
        return self._item_stage_target(goal, "weapon")

    def _item_stage_need(self, goal, purpose: str) -> dict | None:
        """The item the goal's open objective needs in the toolbar while it sits only in the backpack: {"weapon_qid": qid,
        "need": what} or None. Never a guess: unreadable facts open nothing."""
        _, pid = self._binding_view()
        rec = self._obs.get(pid, "ui.context") if pid is not None else None
        ui_facts = rec.facts if rec is not None else None
        inventory = self._obs.get(pid, "progress.inventory") if pid is not None else None
        inv = inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping) else {}
        if purpose == "weapon":
            if not combat.combat_open(getattr(goal, "combat", None)):
                return None
            weapon = combat.weapon_facts(ui_facts, inv or None)
            if (weapon is None or "weapon_qid" not in weapon or weapon.get("weapon_held")
                    or weapon.get("weapon_in_toolbar")):
                return None  # select_item reaches a toolbar weapon (combat_rule)
            return dict(weapon, need="weapon")

        def qid_at(slot):
            fact = inv.get(f"slot:{slot}") if type(slot) is int else None
            qid = fact.get("qualified_item_id") if isinstance(fact, Mapping) and fact.get("occupied") is True else None
            return qid if isinstance(qid, str) else None
        extras_objective = getattr(goal, "extras_objective", None)
        if (extras_objective is not None and extras_objective.kind == "place" and extras_objective.target == "seed"
                and extras_objective.done < extras_objective.count and extras_objective.blocker is None):
            pin = self._water_context(pid)
            slots = self._extras_seed_slots(pin)
            if slots and all(s >= combat.TOOLBAR_SLOTS for s in slots) and qid_at(slots[0]):
                return {"weapon_qid": qid_at(slots[0]), "need": "seed", "slot": slots[0]}
        fishing = getattr(goal, "fishing", None)
        if fishing is not None and getattr(fishing, "status", None) == "OPEN":
            import arti_stardew_fishing as _fishing
            rod = _fishing.rod_facts(ui_facts) or {}
            slot = rod.get("rod_slot")
            if rod.get("rod_held") is False and type(slot) is int and slot >= combat.TOOLBAR_SLOTS and qid_at(slot):
                return {"weapon_qid": qid_at(slot), "need": "rod", "slot": slot}
        want = getattr(goal, "item_stage_want", None)  # skills `pegang` (set by the skills runtime)
        if isinstance(want, str) and want:
            toolbar = {t.get("qualified_item_id") for t in ((ui_facts or {}).get("toolbar") or {}).get("targets") or ()
                       if isinstance(t, Mapping)} if isinstance((ui_facts or {}).get("toolbar"), Mapping) else set()
            at = next((int(str(k)[5:]) for k, f in sorted(inv.items()) if str(k).startswith("slot:")
                       and isinstance(f, Mapping) and f.get("qualified_item_id") == want and type(f.get("slot")) is int
                       and f["slot"] >= combat.TOOLBAR_SLOTS), None)
            if want not in toolbar and at is not None:
                return {"weapon_qid": want, "need": "tool", "slot": at}
        return None

    def _item_stage_target(self, goal, purpose: str):
        """Under the lock: the item facts the stage works on, or None (the stage does not own the goal). Once the menu
        was opened (or an abort runs) the step function always runs, whatever the item facts say now (the item placed
        into the active slot reads `held`, facts may be unreadable): it places a cursor item back and closes the menu.
        Ownership is capped at WEAPON_STAGE_LOCK_S: then abort (place back + close), then stop."""
        if (goal is None or getattr(goal, "progress_marker", None) == "terminal"
                or getattr(goal, "play_mode", None) != "local_activity"):
            return None
        if purpose == "weapon" and not combat.combat_open(getattr(goal, "combat", None)):
            return None
        stage = self._item_stage_dict(goal, purpose)
        if (stage.get("steps", 0) >= combat.WEAPON_STAGE_STEPS_MAX and not stage.get("abort") and not stage.get("stop")
                and (stage.get("opened") or stage.get("start_pressed"))):
            # FIXB10: the step cap reached mid-move never leaves an item on the cursor / the menu open: one bounded abort
            # window (place back + close) follows.
            stage["abort"] = "steps_cap"
        cap = combat.WEAPON_STAGE_STEPS_MAX + (combat.WEAPON_ABORT_STEPS if stage.get("abort") else 0)
        if stage.get("stop") or stage.get("steps", 0) >= cap:
            return None
        if stage.get("opened") or stage.get("start_pressed") or stage.get("abort"):
            target = {"weapon_qid": stage.get("weapon_qid") or "", "need": stage.get("need")}
        else:
            if self.curfew_window_now():
                return None  # FIXB7 curfew guard: no new item stage from 24:00
            other = getattr(goal, "item_stage" if purpose == "weapon" else "combat_weapon_stage", None) or {}
            if not other.get("stop") and (other.get("opened") or other.get("start_pressed") or other.get("abort")):
                return None  # one menu stage at a time (FIXB10 review: symmetric)
            target = self._item_stage_need(goal, purpose)
            if target is None:
                return None
        return None if self._item_stage_expired(goal, stage, purpose) else target

    def _weapon_stage_expired(self, goal, stage) -> bool:
        return self._item_stage_expired(goal, stage, "weapon")

    def _item_stage_expired(self, goal, stage, purpose: str) -> bool:
        now = time.monotonic()
        since = stage.setdefault("lock_since", now)
        if now - since <= self.WEAPON_STAGE_LOCK_S:
            return False
        ev = self._item_stage_ev(purpose)
        if (stage.get("opened") or stage.get("start_pressed")) and not stage.get("abort"):
            stage["abort"], stage["lock_since"] = "lock_timeout", now  # one more bounded window: place back + close
            self._journal({"ev": ev, "goal_id": goal.goal_id, "status": "lock_timeout_abort", "steps": stage.get("steps", 0)})
            return False
        stage["stop"] = "lock_timeout"
        self._journal({"ev": ev, "goal_id": goal.goal_id, "status": "stop", "reason": "lock_timeout",
                       "steps": stage.get("steps", 0)})
        return True

    def combat_weapon_stage_due(self) -> bool:
        try:
            return self._combat_weapon_stage_ready() is not None
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def item_stage_due(self) -> bool:
        try:
            return self._item_stage_ready("item") is not None
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def process_combat_weapon_stage(self) -> int:
        """Worker hook (D-B5-2, live V2B5ACC11a: the sword in backpack slot 13, no select possible)."""
        return self._process_item_stage("weapon")

    def process_item_stage(self) -> int:
        """Worker hook (D-B7b-2): the seed / rod / pegang tool from the backpack into the toolbar."""
        return self._process_item_stage("item")

    def _process_item_stage(self, purpose: str) -> int:
        ev = self._item_stage_ev(purpose)
        with self._lock:
            ready = self._item_stage_ready(purpose)
            if ready is None:
                return 0
            goal, target = ready
            stage = self._item_stage_dict(goal, purpose)
            stage.setdefault("weapon_qid", target.get("weapon_qid"))
            stage.setdefault("need", target.get("need"))
            _, pid = self._binding_view()
            if "edible_slots" not in stage:
                # FIXB19 review: a full toolbar never gives up its food (the heal hand-off eats from the toolbar)
                rec0 = self._obs.get(pid, "ui.context") if pid is not None else None
                cands = (rec0.facts or {}).get("eat_candidates") if rec0 is not None and isinstance(rec0.facts, Mapping) else None
                stage["edible_slots"] = sorted({c["slot"] for c in cands or () if isinstance(c, Mapping)
                                                and type(c.get("slot")) is int and 0 <= c["slot"] < combat.TOOLBAR_SLOTS})
            view = self._ui_pad_view(pid)
            row_shift = None
            if view is not None and getattr(view, "hud", False):
                # fix74a (witness d1 J1133, d4 J199: 15 `foreign_menu:HUD`, every stage stopped at step 0): the HUD is no
                # menu. Outside the toolbar the game's own row swap reaches the item with no menu at all (the same press
                # as pegang_slot, witness d2 J100-J103).
                if stage.get("opened") or stage.get("start_pressed") or stage.get("abort"):
                    view = None
                else:
                    row_shift = target.get("slot", target.get("weapon_slot"))
            unsafe = None
            if view is None and not stage.get("opened"):
                rec = self._obs.get(pid, "ui.context") if pid is not None else None
                facts = rec.facts if rec is not None else None
                if purpose == "weapon" or (isinstance(facts, Mapping) and isinstance(facts.get("combat"), Mapping)):
                    # never open the inventory next to a monster / at the HP floor (the item stages: only where the
                    # mod publishes combat facts, i.e. a monster location)
                    unsafe = combat.weapon_stage_unsafe(facts, goal.combat if purpose == "weapon" else None)
            if unsafe is not None:
                kind, operation, args, reason = "stop", None, None, "unsafe:" + unsafe
            elif view is not None and getattr(view, "hud", False):
                kind, operation, args, reason = (("act", "pad_button", {"button": "RB"}, "row_shift")
                                                 if type(row_shift) is int and row_shift >= combat.TOOLBAR_SLOTS
                                                 else ("stop", None, None, "item_row_unknown"))
            else:
                kind, operation, args, reason = combat.weapon_to_toolbar_step(view, stage.get("weapon_qid") or "", stage)
            if stage.get("abort") and not stage.get("abort_journaled"):
                stage["abort_journaled"] = True
                self._journal({"ev": ev, "goal_id": goal.goal_id, "status": "abort_place_back",
                               "reason": stage["abort"], "steps": stage.get("steps", 0)})
            if kind != "act":
                if kind in ("stop", "done"):
                    stage["stop"] = reason
                    # FIXB28 D-B25-1: the stage's end resumes the owning rule (eat / plant / fishing / combat / pegang)
                    self._stage_end_resume("combat_weapon_stage" if purpose == "weapon" else "item_to_toolbar", goal)
                    self._journal({"ev": ev, "goal_id": goal.goal_id, "status": kind, "reason": reason,
                                   "steps": stage.get("steps", 0), **({"need": stage.get("need")} if purpose != "weapon" else {}),
                                   **({"staging_requirement": "%s_not_in_toolbar" % (stage.get("need") or "item")}
                                      if kind == "stop" else {})})
                return 0
            stage["steps"] = stage.get("steps", 0) + 1
            self._action_busy = True
        try:
            run, _ = self._run_ui_pad(operation, args, origin=ev)
        finally:
            with self._lock:
                self._action_busy = False
        self._journal({"ev": ev, "goal_id": goal.goal_id, "operation": operation, "reason": reason,
                       "verified": run.verified, "effect": run.effect, "step": stage["steps"],
                       **({"need": stage.get("need")} if purpose != "weapon" else {})})
        if run.submitted:
            with self._lock:
                # FIXB12 D-B9-4: stage presses are counted apart (a skill's input budget never pays for them)
                self._stage_publications = getattr(self, "_stage_publications", 0) + 1
        if reason == "row_shift" and not run.verified:
            with self._lock:
                stage["stop"] = "row_shift_unverified:" + str(run.reason)[:40]  # one named end, never 48 refused presses
        elif reason == "row_shift":
            with self._lock:
                stage.pop("lock_since", None)  # no menu is owned between presses; a later need gets its own window
                if row_shift - combat.TOOLBAR_SLOTS < combat.TOOLBAR_SLOTS and purpose != "weapon":
                    # It is in the toolbar now. A later swap for her hand must not read as this item wanted back.
                    goal.item_stage_want = None
        if reason == "open_inventory" and run.submitted:
            with self._lock:
                stage["start_pressed"] = True  # FIXB8 review: a published Start may have opened the menu: still owned
        if not run.verified and run.submitted:
            with self._lock:
                why = "step_unverified:" + str(run.reason)[:40]
                if stage.get("opened") and not stage.get("abort"):
                    # D-B6-1: a refused / unverified step never ends the stage with the item on the cursor: the
                    # bounded place-back + close (FIXB6 fix 1) runs first, then the stop is reported.
                    stage["abort"] = why
                else:
                    stage["stop"] = why
        return 1

    # ------------------------------------------------------------------ FIXB11 D-B8-1: no route -> let it come
    COMBAT_WAIT_S = 60.0
    COMBAT_WAITS_MAX = 3
    MONSTER_ROUTE_NODES = 4000

    def _monster_routable(self, pin, motor, tile, goal, *, stones_open: bool | None = None) -> bool:
        """A route over the tiles not known blocked (scene facts + this goal's learned tiles; unknown tiles optimistic)
        from ARTI's tile to a cardinal neighbour of the monster, bounded. False only when the known obstacles box it.

        Bounds (FIXB11 review): every learned tile costs one settled NO_EFFECT press, so a farmer boxed in on all four
        sides by unpublished rocks is called unroutable after at most four blocked presses (one per side; the controller
        tries a detour first); a corridor wall around the monster needs as many presses as open boundary tiles. The
        search window is the monster's Chebyshev distance + 8 tiles around ARTI, at most MONSTER_ROUTE_NODES tiles (the
        bound is never taken as a wall)."""
        from collections import deque
        if motor is None or getattr(motor, "tile", None) is None or tile is None:
            return True
        routing = self._approach_routing(pin, motor, stones_open=stones_open)
        blocked = set(routing.blocked_tiles) if routing is not None else set()
        blocked |= self._learned_blocked_tiles(goal, pin.location)
        start = tuple(motor.tile)
        goals = {(tile[0] + dx, tile[1] + dy) for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))} - blocked
        if not goals:
            return False
        seen, queue = {start}, deque([start])
        span = max(abs(tile[0] - start[0]), abs(tile[1] - start[1])) + 8
        while queue and len(seen) < self.MONSTER_ROUTE_NODES:
            x, y = queue.popleft()
            if (x, y) in goals:
                return True
            for nx, ny in ((x, y + 1), (x, y - 1), (x + 1, y), (x - 1, y)):
                if ((nx, ny) in seen or (nx, ny) in blocked or nx < 0 or ny < 0
                        or max(abs(nx - start[0]), abs(ny - start[1])) > span):
                    continue
                seen.add((nx, ny))
                queue.append((nx, ny))
        return len(seen) >= self.MONSTER_ROUTE_NODES  # the bound is no proof of a wall

    def _combat_routable_snapshot(self, goal, snap):
        """FIXB11 D-B8-1 (live V2B8ACC11a, Mines 3: ARTI (9,31), slime (21,20); every approach pressed into rocks and
        learned (10,31) / (9,30) blocked): monster approach entries without a known route are not offered."""
        entries = [t for t in getattr(snap, "approach_targets", ()) or ()
                   if isinstance(t, Mapping) and t.get("kind") == combat.KIND_MONSTER_ENTRY]
        if not entries or getattr(snap, "reflex_combat_target", None) is not None:
            return snap
        pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
        if pin is None:
            _, pid = self._binding_view()
            pin = self._water_context(pid)
        if pin is None:
            return snap
        motor = self._state.latest(pin.player_id)
        drop = [t for t in entries if not self._monster_routable(pin, motor, t.get("tile"), goal)]
        if not drop:
            return snap
        self._journal({"ev": "combat_monster_unroutable", "goal_id": goal.goal_id,
                       "refs": [t.get("ref") for t in drop][:4]})
        kept = tuple(t for t in snap.approach_targets if t not in drop)
        try:
            from dataclasses import replace as _replace
            return _replace(snap, approach_targets=kept)
        except TypeError:
            from types import SimpleNamespace
            return SimpleNamespace(**{**vars(snap), "approach_targets": kept})

    def _combat_wait_start(self, goal, request) -> bool:
        """FIXB11 D-B8-1 (b): matching monsters are published but none is offered (no route, blocked): hold position and
        let them come (slimes aggro toward the farmer); the wait is a locked stage, bounded COMBAT_WAIT_S."""
        objective = getattr(goal, "combat", None)
        if (not combat.combat_open(objective) or objective.heal_pending or goal.combat_waits >= self.COMBAT_WAITS_MAX
                or (goal.combat_wait and not goal.combat_wait.get("ended"))):
            return False
        _, pid = self._binding_view()
        facts = self._combat_ui_facts(self._water_context(pid))
        monsters = [m for m in ((facts or {}).get("combat") or {}).get("monsters") or ()
                    if isinstance(m, Mapping) and combat.kill_matches(objective.kind, m.get("type"))]
        if not monsters or not combat.weapon_ready(facts):
            return False
        goal.combat_waits += 1
        goal.combat_wait = {"since": time.monotonic(), "n": goal.combat_waits}
        # FIXB15 D-B13-2 (live V2B13ACC11a: the wait ended `monster_in_radius` after 5.2 s, no continuation -> HELD): the
        # lock is recorded now (the wait decision itself is the last request), so process_stage_lock sees the release
        # and resumes with one fresh inspect + continuation.
        self._stage_lock_last = "combat_wait_for_aggro"
        self._journal({"ev": "stage_lock", "goal_id": goal.goal_id, "stage": "combat_wait_for_aggro", "status": "locked"})
        self._journal({"ev": "combat_wait_for_aggro", "goal_id": goal.goal_id, "generation_id": request.generation_id,
                       "status": "started", "monsters": len(monsters), "n": goal.combat_waits,
                       "nearest": min((m.get("distance") for m in monsters if isinstance(m.get("distance"), (int, float))),
                                      default=None)})
        return True

    def _combat_wait_owns(self, goal) -> bool:
        """Caller holds the lock: the aggro wait owns the goal until a monster is inside the reflex radius (released:
        the fresh continuation arms reflex_combat) or COMBAT_WAIT_S passed (verdict NoTarget(unreachable))."""
        wait = getattr(goal, "combat_wait", None)
        objective = getattr(goal, "combat", None)
        if not wait or wait.get("ended"):
            return False
        if not combat.combat_open(objective):
            wait["ended"] = "objective_closed"
            return False
        _, pid = self._binding_view()
        facts = self._combat_ui_facts(self._water_context(pid))
        cf = (facts or {}).get("combat") if isinstance(facts, Mapping) else None
        near = [m for m in (cf or {}).get("monsters") or () if isinstance(m, Mapping)
                and combat.kill_matches(objective.kind, m.get("type")) and isinstance(m.get("distance"), (int, float))
                # FIXB23 D-B21-2 (live V2B21ACC11b: released `monster_in_radius` at 8.0 tiles x3, the reflex (offered only
                # inside 7.5) never armed -> HELD): the wait releases at the reflex's own radius.
                and m["distance"] <= combat.command.REFLEX_COMBAT_RADIUS_TILES_MAX - combat.REFLEX_RADIUS_MARGIN]
        low = combat.idle_hp_floor(cf)
        rule = combat.time_rule(cf) if isinstance(cf, Mapping) and type(cf.get("time_of_day")) is int else None
        # FIXB11 review MAJOR: the wait never keeps ARTI standing still while it is hit or the day closes: it ends when
        # the reflex is offered, when a matching monster is inside the radius at all (weapon / HP admission aside: the
        # fresh decision sees why), when HP drops below the floor (heal hand-off) or when the time rule closes combat.
        ended = ("monster_in_radius" if combat.combat_target(facts, objective) is not None or near
                 else "hp_floor" if low is not None else ("time_rule:" + rule) if rule is not None else None)
        if ended is not None:
            wait["ended"] = ended
            if ended == "hp_floor":
                goal.combat = combat.heal_needed(objective)
            self._journal({"ev": "combat_wait_for_aggro", "goal_id": goal.goal_id, "status": ended.split(":")[0],
                           "detail": ended, "waited_s": round(time.monotonic() - wait["since"], 1),
                           **({"hp": low["hp"]} if low else {})})
            return False
        if time.monotonic() - wait["since"] > self.COMBAT_WAIT_S:
            wait["ended"] = "timeout"
            if self._clear_path_start(goal) or self._clear_path_route_start(goal):
                # FIXB13 D-B10-1 (c): break the rock on the blocked route (FIXB21: the first stone of the stone route)
                self._journal({"ev": "combat_wait_for_aggro", "goal_id": goal.goal_id, "status": "timeout",
                               "next": "clear_path", "n": wait.get("n")})
                return False
            goal.combat = replace(objective, status="BLOCKED", reason="no_target_unreachable",
                                  last_verdict="NoTarget(unreachable)")
            self._journal({"ev": "combat_wait_for_aggro", "goal_id": goal.goal_id, "status": "timeout",
                           "verdict": "NoTarget(unreachable)", "n": wait.get("n")})
            return False
        return True

    # ------------------------------------------------------------------ FIXB13 D-B10-1: clear_path (break a rock)
    COMBAT_REARMS_MAX = 3
    # ADR 153: continued timed-out batches per fight (combat.MAX_COMBAT_CALLS = 6 calls per objective bounds it too).
    MINES_FIGHT_CONTINUE_MAX = 5

    def combat_rearm_due(self) -> bool:
        goal = self._active
        return bool(goal is not None and goal.progress_marker != "terminal"
                    and (getattr(goal, "combat_rearm_due", False) or self._combat_unreachable_reopen_due(goal)))

    def _combat_unreachable_reopen_due(self, goal) -> bool:
        """FIXB14 D-B12-3: the objective ended NoTarget(unreachable) after the aggro wait, and a matching monster is now
        inside the reflex radius (it came after all): reopen once more (bounded with the re-arms)."""
        objective = getattr(goal, "combat", None)
        if (objective is None or objective.status != "BLOCKED" or objective.reason != "no_target_unreachable"
                or getattr(goal, "combat_unreachable_rearms", 0) >= self.COMBAT_REARMS_MAX):
            return False
        try:
            _, pid = self._binding_view()
            cf = ((self._combat_ui_facts(self._water_context(pid)) or {}).get("combat")) or {}
        except Exception:  # noqa: BLE001
            return False
        return any(isinstance(m, Mapping) and combat.kill_matches(objective.kind, m.get("type"))
                   and isinstance(m.get("distance"), (int, float))
                   and m["distance"] <= combat.command.REFLEX_COMBAT_RADIUS_TILES_MAX - combat.REFLEX_RADIUS_MARGIN
                   for m in cf.get("monsters") or ())  # FIXB23 D-B21-2: the reflex's own radius

    def process_combat_rearm(self) -> int:
        """Worker hook (FIXB13): one fresh read-only inspect + continuation after a refused reflex (the combat rule then
        approaches / waits / re-arms from the new facts)."""
        with self._lock:
            goal = self._active
            if (not self.combat_rearm_due() or self._action_busy or self._inflight_generation is not None
                    or getattr(self, "_results_pending", 0) or self._trial_result_pending()):
                return 0
            if self._combat_unreachable_reopen_due(goal):
                goal.combat_unreachable_rearms += 1
                self._journal({"ev": "combat_rearm", "goal_id": goal.goal_id, "n": goal.combat_unreachable_rearms,
                               "last_verdict": goal.combat.last_verdict, "reason": "monster_in_radius_after_unreachable"})
                goal.combat = replace(goal.combat, status="OPEN", reason="monster_not_killed_yet")
                goal.combat_wait = None
            else:
                goal.combat_rearm_due = False
                goal.combat_rearms += 1
                self._journal({"ev": "combat_rearm", "goal_id": goal.goal_id, "n": goal.combat_rearms,
                               "last_verdict": goal.combat.last_verdict, "reason": goal.combat.reason})
        self._resume_after_pause(reason="combat_rearm")
        return 1

    CLEAR_PATH_SWINGS_MAX = 6
    CLEAR_PATH_ROCKS_MAX = 4
    PICKAXES = ("(T)Pickaxe", "(T)CopperPickaxe", "(T)SteelPickaxe", "(T)GoldPickaxe", "(T)IridiumPickaxe")

    def _scene_facts_now(self):
        _, pid = self._binding_view()
        rec = self._obs.get(pid, "scene.current_location") if pid is not None else None
        return (rec.facts if rec is not None and isinstance(rec.facts, Mapping) else None) or {}

    def _route_needs_rock(self, goal, snap) -> bool:
        """FIXB14: no matching monster entry is routable while breakable rocks count as walls (a rock leg is needed)."""
        pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
        if pin is None:
            pin = self._water_context(self._binding_view()[1])
        if pin is None:
            return False
        motor = self._state.latest(pin.player_id)
        entries = [t for t in getattr(snap, "approach_targets", ()) or ()
                   if isinstance(t, Mapping) and t.get("kind") == combat.KIND_MONSTER_ENTRY]
        return not any(self._monster_routable(pin, motor, t.get("tile"), goal, stones_open=False) for t in entries)

    def _clear_path_possible(self, goal) -> bool:
        """FIXB14: an open combat goal that may still start a rock leg (pickaxe owned, rocks left): stones are walkable
        for its routing (the approach walks into the first one and the leg breaks it)."""
        objective = getattr(goal, "combat", None) if goal is not None else None
        if (not combat.combat_open(objective) or objective.heal_pending
                or getattr(goal, "clear_path_rocks", 0) >= self.CLEAR_PATH_ROCKS_MAX):
            return False
        if (getattr(goal, "clear_path", None) or {}).get("stand"):
            return False  # FIXB21 review: a route leg's stand approach never walks into another stone
        try:
            _, pid = self._binding_view()
            inventory = self._obs.get(pid, "progress.inventory") if pid is not None else None
        except Exception:  # noqa: BLE001
            return False
        inv = inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping) else {}
        return any(isinstance(f, Mapping) and f.get("qualified_item_id") in self.PICKAXES for f in inv.values())

    def _rock_here(self, pin, tile) -> str | None:
        """FIXB14 D-B12-3: a breakable rock on ``tile``: the nav grid's 's' cell (authoritative when published), else a
        scene Stone fact. Returns a ref for the journal, or None."""
        grid = self._nav_grid(pin) if pin is not None else None
        if grid and tuple(tile) in grid:
            return "nav:%d,%d" % tuple(tile) if grid[tuple(tile)] == "s" else None
        return self._rock_at(self._scene_facts_now(), tile)

    @staticmethod
    def _rock_at(facts, tile) -> str | None:
        """The scene fact id of a breakable rock (object named Stone) on ``tile``, or None."""
        for ref, fact in facts.items():
            t = fact.get("tile") if isinstance(fact, Mapping) else None
            if (isinstance(t, Mapping) and (t.get("x"), t.get("y")) == tuple(tile) and fact.get("kind") == "object"
                    and str(fact.get("name") or "").casefold() == "stone"):
                return str(ref)
        return None

    def _clear_path_start(self, goal, *, learned_only: bool = False, rock: tuple | None = None) -> bool:
        """Caller holds the lock. A rock (scene object Stone) on one of ARTI's four sides, preferring the side toward the
        nearest matching monster; ``learned_only``: only a side a no-motion press learned blocked (the approach really
        stopped there: started before any aggro wait). Needs a pickaxe owned. Bounded per goal."""
        objective = getattr(goal, "combat", None)
        if (not combat.combat_open(objective) or objective.heal_pending or goal.clear_path is not None
                or goal.clear_path_rocks >= self.CLEAR_PATH_ROCKS_MAX
                or self._clear_path_abort_reason(goal, forced=rock is not None) is not None):
            return False
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        if motor is None or getattr(motor, "tile", None) is None:
            return False
        pin = self._water_context(pid)
        facts = self._scene_facts_now()
        inventory = self._obs.get(pid, "progress.inventory") if pid is not None else None
        inv = inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping) else {}
        if not any(isinstance(f, Mapping) and f.get("qualified_item_id") in self.PICKAXES for f in inv.values()):
            return False
        cf = (self._combat_ui_facts(pin) or {}).get("combat") or {}
        monsters = [m for m in cf.get("monsters") or () if isinstance(m, Mapping) and isinstance(m.get("tile"), (list, tuple))
                    and combat.kill_matches(objective.kind, m.get("type"))]
        x, y = motor.tile
        toward = min(monsters, key=lambda m: m.get("distance") or 99)["tile"] if monsters else None
        sides = sorted(((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)),
                       key=lambda s: (abs(toward[0] - s[0]) + abs(toward[1] - s[1])) if toward else 0)
        learned = self._learned_blocked_tiles(goal, pin.location) if pin is not None else set()
        if rock is not None:
            # FIXB15 D-B13-3: the mod's reflex route reported this rock (adjacent: its next step); the grid decides when it
            # is gone, a tile the grid does not cover stays a rock until the swings run out.
            rock = (int(rock[0]), int(rock[1]))
            grid = self._nav_grid(pin) if pin is not None else None
            if (rock not in sides or rock in goal.clear_path_skip
                    or (grid and rock in grid and grid[rock] != "s")):
                return False
            goal.clear_path = {"rock": list(rock), "ref": "reflex:%d,%d" % rock, "swings": 0, "selected": False,
                               "forced": True}
            goal.clear_path_rocks += 1
            self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "started", "rock": list(rock),
                           "n": goal.clear_path_rocks, "origin": "reflex_route_blocked"})
            return True
        for side in sides:
            if (learned_only and side not in learned) or side in goal.clear_path_skip:
                continue
            ref = self._rock_here(pin, side)
            if ref is not None:
                goal.clear_path = {"rock": list(side), "ref": ref, "swings": 0, "selected": False}
                goal.clear_path_rocks += 1
                self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "started", "rock": list(side),
                               "n": goal.clear_path_rocks})
                return True
        return False

    ROUTE_STONE_COST = 6
    ROUTE_NODES_MAX = 2000

    def _clear_path_route_start(self, goal) -> bool:
        """FIXB21 D-B18-1 (live V2B18ACC11a: a Stone band (3..6,28) / (4,29) / (2,30) cut the ladder pocket off from the
        slimes; no rock was ADJACENT to the ladder tile, so no clear_path ever started -> `NoTarget(unreachable)`). Caller
        holds the lock: plan over the nav grid from ARTI to a cardinal neighbour of the nearest matching monster with
        breakable stones passable at a cost (other blocking cells closed), take the FIRST stone on that path as the rock
        and the open tile before it as the stand; the leg approaches the stand, breaks the rock, and the combat rule
        re-plans. Bounded by CLEAR_PATH_ROCKS_MAX per goal (as the adjacent legs)."""
        import heapq
        objective = getattr(goal, "combat", None)
        if (not combat.combat_open(objective) or objective.heal_pending or goal.clear_path is not None
                or goal.clear_path_rocks >= self.CLEAR_PATH_ROCKS_MAX
                or self._clear_path_abort_reason(goal, forced=True) is not None):
            return False
        _, pid = self._binding_view()
        motor = self._state.latest(pid) if pid is not None else None
        pin = self._water_context(pid)
        grid = self._nav_grid(pin) if pin is not None else None
        if motor is None or getattr(motor, "tile", None) is None or not grid:
            return False
        inventory = self._obs.get(pid, "progress.inventory") if pid is not None else None
        inv = inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping) else {}
        if not any(isinstance(f, Mapping) and f.get("qualified_item_id") in self.PICKAXES for f in inv.values()):
            return False
        cf = (self._combat_ui_facts(pin) or {}).get("combat") or {}
        monsters = sorted((m for m in cf.get("monsters") or () if isinstance(m, Mapping)
                           and isinstance(m.get("tile"), (list, tuple)) and len(m["tile"]) == 2
                           and combat.kill_matches(objective.kind, m.get("type"))),
                          key=lambda m: m.get("distance") or 99)
        if not monsters:
            return False
        tx, ty = monsters[0]["tile"]
        # review: only an open cell beside the monster is a goal (never a wall / water tile the path would end on)
        goals = {n for n in ((tx, ty + 1), (tx, ty - 1), (tx + 1, ty), (tx - 1, ty)) if grid.get(n) == "."}
        if not goals:
            return False
        start = tuple(motor.tile)
        dist, prev, heap, seen = {start: 0}, {}, [(0, start)], 0
        found = None
        while heap and seen < self.ROUTE_NODES_MAX:
            d, cur = heapq.heappop(heap)
            seen += 1
            if d > dist.get(cur, 1 << 30):
                continue
            if cur in goals:
                found = cur
                break
            x, y = cur
            for nxt in ((x, y + 1), (x, y - 1), (x + 1, y), (x - 1, y)):
                cell = grid.get(nxt)
                if nxt in goal.clear_path_skip and cell == "s":
                    continue
                cost = 1 if cell == "." else self.ROUTE_STONE_COST if cell == "s" else None
                if cost is None:
                    continue
                if d + cost < dist.get(nxt, 1 << 30):
                    dist[nxt] = d + cost
                    prev[nxt] = cur
                    heapq.heappush(heap, (d + cost, nxt))
        if found is None:
            return False
        path, cur = [], found
        while cur != start:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        before = start
        for tile in path:
            if grid.get(tile) == "s":
                goal.clear_path = {"rock": list(tile), "ref": "route:%d,%d" % tile, "swings": 0, "selected": False,
                                   "forced": True, "stand": list(before), "approaches": 0}
                goal.clear_path_rocks += 1
                self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "started", "rock": list(tile),
                               "stand": list(before), "n": goal.clear_path_rocks, "origin": "route",
                               "monster": [tx, ty], "path_len": len(path)})
                return True
            before = tile
        return False  # a path without a stone: the approach routes it, nothing to break

    def _clear_path_stand_entries(self, goal) -> list[dict]:
        """FIXB21 D-B18-1: the route leg's stand as an approach entry (reach = the stand, facing the rock)."""
        stage = getattr(goal, "clear_path", None) if goal is not None else None
        if not stage or not stage.get("stand"):
            return []
        rx, ry = stage["rock"]
        sx, sy = stage["stand"]
        ref = "clear_path:%d,%d@%d,%d" % (rx, ry, sx, sy)
        return [{"ref": ref, "tile": [rx, ry], "kind": "clear_path_stand", "reach_tiles": [[sx, sy]],
                 "revision": ref}]

    def _clear_path_rock_gone(self, stage) -> bool:
        pin = self._water_context(self._binding_view()[1])
        if stage.get("forced"):
            grid = self._nav_grid(pin) if pin is not None else None
            tile = tuple(stage["rock"])
            if not grid or tile not in grid:
                return False  # unknown: the swing bound decides
            return grid[tile] != "s"
        return self._rock_here(pin, stage["rock"]) is None

    def _clear_path_end(self, goal, status: str, **extra) -> None:
        stage = goal.clear_path or {}
        self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": status, "rock": stage.get("rock"),
                       "swings": stage.get("swings", 0), **extra})
        if status == "aborted" and extra.get("reason") != "monster_in_radius" and stage.get("rock"):
            goal.clear_path_skip.add(tuple(stage["rock"]))  # a monster in reach is a pause, anything else gives up
        if status == "cleared":
            rock = tuple(stage["rock"])
            pin = self._water_context(self._binding_view()[1])
            if pin is not None:
                (goal.learned_blocked.get(pin.location) or {}).pop(rock, None)
            goal.approach_blocked_refs = {r for r in goal.approach_blocked_refs if not str(r).startswith("monster:")}
            goal.approach_no_motion = {}
            goal.combat_wait = None
            session = getattr(self, "_skill_session", None)
            run = getattr(session, "current", None)
            if (run is not None and run.goal_id == goal.goal_id and run.call.name == "lawan_mine"
                    and run.state.get("wave1_driver") == "combat_floor"):
                # A natively verified route rock removal is progress. Earlier
                # refreshes while clearing it must not force an immediate exit.
                run.state["stall_refreshes"] = 0
                run.idle_since = None
        goal.clear_path = None
        goal.item_stage_want = None

    CLEAR_PATH_STEPS_MAX = 30  # every decision of the stage (stand approach legs, selects, faces, swings, re-select)
    CLEAR_PATH_APPROACHES_MAX = 8
    CLEAR_PATH_SELECTS_MAX = 2

    FORCED_NEAR_TILES = 1.6  # FIXB15: a route_blocked leg pauses only for a monster within striking distance

    def _clear_path_abort_reason(self, goal, snap=None, *, forced: bool = False) -> str | None:
        """Caller holds the lock: why the rock leg must stop now (released to the combat rule / reflex / heal), or None.
        ``forced`` (the mod's reflex reported this rock as the only route, FIXB15 D-B13-3): the monster in the reflex
        radius is the one behind the rock, so only one within striking distance pauses the leg."""
        objective = goal.combat
        if not combat.combat_open(objective):
            return "objective_closed"
        _, pid = self._binding_view()
        cf = (self._combat_ui_facts(self._water_context(pid)) or {}).get("combat")
        rule = combat.time_rule(cf) if isinstance(cf, Mapping) and type(cf.get("time_of_day")) is int else None
        near = [m for m in (cf or {}).get("monsters") or () if isinstance(m, Mapping)
                and combat.kill_matches(objective.kind, m.get("type")) and isinstance(m.get("distance"), (int, float))
                and m["distance"] <= (self.FORCED_NEAR_TILES if forced else combat.command.REFLEX_COMBAT_RADIUS_TILES_MAX)]
        offered = getattr(snap, "reflex_combat_target", None) is not None and not forced
        return ("monster_in_radius" if offered or near
                else "hp_floor" if combat.idle_hp_floor(cf) is not None
                else ("time_rule:" + rule) if rule is not None
                else "curfew_window" if self.curfew_window_now() else None)

    def _clear_path_abort(self, goal, stage, request, reason, *, held, weapon, can_select):
        """FIXB13 review: an abort never releases with the pickaxe in hand. First call with the pickaxe held and the
        weapon selectable: one weapon select_item is the leg's last step (journal ``release_weapon``); the next decision
        (or no pickaxe in hand) finishes the abort: hp_floor -> heal hand-off, journal ``aborted``, None (combat rule)."""
        if (not stage.get("abort") and held in self.PICKAXES and weapon is not None and can_select
                and combat.combat_open(goal.combat)):
            stage["abort"] = reason
            self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "release_weapon", "reason": reason,
                           "rock": stage.get("rock"), "slot": weapon.get("slot")})
            choice = {"kind": "act", "operation": "select_item", "target_ref": None, "args": dict(weapon),
                      "evidence_refs": []}
            return self._rule_json(request, choice, "clear_path", "weapon")
        if reason == "hp_floor":
            goal.combat = combat.heal_needed(goal.combat)
        self._clear_path_end(goal, "aborted", reason=reason)
        return None

    def _clear_path_invoke(self, request):
        """_stage_invoke hook: while a clear_path stage is open every decision of the goal is its next step from the
        request's own snapshot (no model): select the pickaxe (the locked item_to_toolbar stage first when it sits in the
        backpack), face the rock (1-tick press), use_tool until the rock fact is gone (<= CLEAR_PATH_SWINGS_MAX),
        re-select the weapon, then resume the approach. Aborts (released to the combat rule): a matching monster inside
        the reflex radius, HP below the floor (heal hand-off), the time rule, the 24:00 window, any step bound."""
        with self._lock:
            goal = self._active
            stage = getattr(goal, "clear_path", None) if goal is not None else None
            if stage is None or request.goal_id != goal.goal_id:
                return None
            if request.reason == "owner_turn":
                # locked like the worker stages: the owner gets a short spoken reply, never a model act mid-swing
                self._journal({"ev": "stage_lock", "goal_id": goal.goal_id, "stage": "clear_path", "status": "owner_reply"})
                return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                   "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                   "speech": self.STAGE_LOCK_SPEECH["clear_path"], "choice": {"kind": "speak"},
                                   "activity": None}, ensure_ascii=False)
            snap = request.snapshot
            weapon_slot = (getattr(snap, "combat_objective", None) or {}).get("weapon_slot")
            equipped = getattr(snap, "equipped_tool", None) or {}
            held = equipped.get("qualified_item_id")
            selectable = {i.get("qualified_item_id"): i for i in getattr(snap, "selectable_items", ()) or ()
                          if isinstance(i, Mapping)}
            can_select = bool(getattr(snap, "select_item_available", False))
            weapon = next((i for i in selectable.values() if i.get("slot") == weapon_slot), None)

            def abort_leg(reason):
                return self._clear_path_abort(goal, stage, request, reason, held=held, weapon=weapon,
                                              can_select=can_select)
            abort = self._clear_path_abort_reason(goal, snap, forced=bool(stage.get("forced")))
            if stage.get("abort"):
                # the weapon re-select of an abort was the last step: finish the abort (hp_floor: heal hand-off now)
                return abort_leg(stage["abort"] if abort != "hp_floor" else "hp_floor")
            stage["steps"] = stage.get("steps", 0) + 1
            if abort is None and stage["steps"] > self.CLEAR_PATH_STEPS_MAX:
                abort = "steps_cap"
            if abort is not None:
                return abort_leg(abort)
            if self._clear_path_rock_gone(stage):
                if (held in self.PICKAXES and weapon is not None and can_select
                        and stage.get("reselects", 0) < self.CLEAR_PATH_SELECTS_MAX):
                    stage["reselects"] = stage.get("reselects", 0) + 1
                    self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "reselect_weapon",
                                   "rock": stage["rock"], "slot": weapon_slot})
                    choice = {"kind": "act", "operation": "select_item", "target_ref": None, "args": dict(weapon),
                              "evidence_refs": []}
                    return self._rule_json(request, choice, "clear_path", "weapon")
                self._clear_path_end(goal, "cleared")
                return None
            if held not in self.PICKAXES:
                pick = next((selectable[q] for q in self.PICKAXES if q in selectable), None)
                if pick is not None:
                    if not can_select or stage.get("selects", 0) >= self.CLEAR_PATH_SELECTS_MAX:
                        return abort_leg("pickaxe_not_selected")
                    stage["selects"] = stage.get("selects", 0) + 1
                    self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "select_pickaxe",
                                   "rock": stage["rock"], "slot": pick.get("slot")})
                    choice = {"kind": "act", "operation": "select_item", "target_ref": None, "args": dict(pick),
                              "evidence_refs": []}
                    return self._rule_json(request, choice, "clear_path", "pickaxe")
                _, pid = self._binding_view()
                inventory = self._obs.get(pid, "progress.inventory") if pid is not None else None
                inv = inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping) else {}
                owned_fact = next((f for f in inv.values() if isinstance(f, Mapping)
                                   and f.get("qualified_item_id") in self.PICKAXES), None)
                owned = owned_fact.get("qualified_item_id") if owned_fact is not None else None
                if (owned_fact is not None and type(owned_fact.get("slot")) is int
                        and owned_fact["slot"] < combat.TOOLBAR_SLOTS):
                    # FIXB21 D-B18-2: already in the toolbar (the item stage has nothing to move): the snapshot of the
                    # decision that started the leg predates the leg's select offer; one fresh read, then select it.
                    if stage.get("select_refreshed"):
                        return abort_leg("pickaxe_not_selectable")
                    stage["select_refreshed"] = True
                    self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "select_refresh",
                                   "rock": stage["rock"], "slot": owned_fact["slot"]})
                    return self._rule_json(request, {"kind": "wait", "reason": "clear_path_select_refresh", "ms": 0},
                                           "clear_path", "wait")
                if owned is None or stage.get("staged"):
                    return abort_leg("no_pickaxe" if owned is None else "pickaxe_not_staged")
                stage["staged"] = True
                goal.item_stage_want = owned  # the locked item_to_toolbar stage moves it into the toolbar first
                if (goal.item_stage or {}).get("stop"):
                    goal.item_stage = {}  # FIXB19 D-B16-2: an earlier stage's stop never blocks this move
                self._stage_end_resume("item_to_toolbar", goal, force=True)  # FIXB19 D-B16-2: its end resumes clear_path (continuation)
                self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "stage_pickaxe", "qid": owned})
                return self._rule_json(request, {"kind": "wait", "reason": "clear_path_pickaxe", "ms": 0},
                                       "clear_path", "wait")
            rx, ry = stage["rock"]
            facing = {(0, -1): 0, (1, 0): 1, (0, 1): 2, (-1, 0): 3}.get((rx - equipped.get("tile_x", -99),
                                                                          ry - equipped.get("tile_y", -99)))
            if facing is None and stage.get("stand"):
                # FIXB21 D-B18-1: a route leg walks to the rock's stand first (approach entry `clear_path:...`)
                entry = next((e for e in getattr(snap, "approach_targets", ()) or () if isinstance(e, Mapping)
                              and e.get("kind") == "clear_path_stand"), None)
                stage["approaches"] = stage.get("approaches", 0) + 1
                if stage["approaches"] > self.CLEAR_PATH_APPROACHES_MAX:
                    return abort_leg("stand_unreachable")
                if entry is None:
                    return self._rule_json(request, {"kind": "wait", "reason": "clear_path_route", "ms": 0},
                                           "clear_path", "wait")
                self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "approach_stand",
                               "rock": stage["rock"], "stand": stage["stand"], "n": stage["approaches"]})
                choice = {"kind": "act", "operation": "approach_target", "target_ref": entry["ref"], "args": {},
                          "evidence_refs": []}
                return self._rule_json(request, choice, "clear_path", "approach_stand")
            if facing is None:
                return abort_leg("rock_not_adjacent")
            if equipped.get("facing") != facing:
                if stage.get("faces", 0) >= 2:
                    return abort_leg("facing_unverified")
                stage["faces"] = stage.get("faces", 0) + 1
                choice = {"kind": "act", "operation": "world_key.move_cardinal", "target_ref": None,
                          "args": {"direction": ("up", "right", "down", "left")[facing], "ticks": 1}, "evidence_refs": []}
                return self._rule_json(request, choice, "clear_path", "face_rock")
            if stage["swings"] >= self.CLEAR_PATH_SWINGS_MAX or not getattr(snap, "use_tool_available", False):
                return abort_leg("rock_unbroken" if stage["swings"] else "use_tool_unavailable")
            stage["swings"] += 1
            args = {k: equipped[k] for k in ("slot", "qualified_item_id", "tile_x", "tile_y", "facing")}
            args["ticks"] = 14
            self._journal({"ev": "clear_path", "goal_id": goal.goal_id, "status": "swing", "rock": stage["rock"],
                           "swing": stage["swings"]})
            choice = {"kind": "act", "operation": "world_key.use_tool", "target_ref": None, "args": args,
                      "evidence_refs": []}
            return self._rule_json(request, choice, "clear_path", "swing")

    def _combat_target(self, goal, binding, pin, *, session_check: bool = True) -> dict | None:
        objective = getattr(goal, "combat", None) if goal is not None else None
        if not combat.combat_open(objective):
            return None
        inventory = self._obs.get(pin.player_id, "progress.inventory") if pin is not None else None
        native = self._combat_ui_facts(pin)
        target = combat.combat_target(native, objective,
                                      inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping)
                                      else None)
        if target is None or (session_check and not self._water_snapshot_available(binding, pin, reflex_g5=combat.OPERATION_COMBAT)):
            return None
        if not session_check:
            # This is the existing leased publication recheck, before any swing.
            native = native or {}
            weapon = combat.weapon_facts(native, inventory.facts if inventory is not None else None) or {}
            readback = dict(batch=objective.calls + 1, held=weapon.get("weapon_qid"),
                            slot=weapon.get("weapon_slot"), verified=weapon.get("weapon_held") is True,
                            native_weapon=(native.get("combat") or {}).get("weapon"))
            goal.combat = replace(objective, weapon_readback=readback)
            self._journal(dict(ev="reflex_combat_weapon_readback", goal_id=goal.goal_id, **readback))
        return target

    def _charge_target(self, goal, binding, pin, *, session_check: bool = True) -> dict | None:
        objective = getattr(goal, "charge", None) if goal is not None else None
        if not combat.charge_open(objective):
            return None
        target = combat.charge_target(self._combat_ui_facts(pin), objective)
        if target is None or (session_check and not self._water_snapshot_available(binding, pin, reflex_g5=combat.OPERATION_CHARGE)):
            return None
        return target

    @staticmethod
    def _combat_offer_payload(target: Mapping) -> dict:
        return {
            "target_ref": None,
            "observed_target": dict(target),
            "args": ("{} (runtime fills defaults) or a subset of {max_s 1-60, hp_floor_pct 30-90, max_kills 1-5,"
                     " retreat bool}"),
            "result_scope": combat.RESULT_SCOPE_COMBAT,
        }

    @staticmethod
    def _charge_offer_payload(target: Mapping) -> dict:
        return {
            "target_ref": None,
            "observed_target": dict(target),
            "args": "{} (runtime fills the objective's level) or {max_ticks 45-200}",
            "result_scope": combat.RESULT_SCOPE_CHARGE,
        }

    # ------------------------------------------------------------------ settlement

    def _reflex_g5_record_result(self, goal, operation: str, evidence, envelope: Mapping | None, superseded: bool) -> None:
        """Under the lock, after a submitted reflex call. Never replays; doubt latches the global stop."""
        cleanup = evidence.cleanup_confirmed and not superseded
        fields = combat.journal_fields(envelope)
        if operation == combat.OPERATION_COMBAT and getattr(goal, "combat", None) is not None:
            before = goal.combat.status
            new_result = before == "OPEN" and (not evidence.request_id or evidence.request_id not in goal.combat.request_ids)
            goal.combat = combat.settle_combat(goal.combat, request_id=evidence.request_id, lifecycle=evidence.lifecycle,
                                               cleanup=cleanup, envelope=envelope)
            if goal.combat.disengage and new_result:
                # ADR 179: her page states the leg's real native numbers, never None -> None.
                goal.combat = replace(goal.combat, leg={k: fields.get(k) for k in (
                    "hp_start", "hp_end", "hp_min", "defends", "disengage_route", "kills", "ticks", "steps")})
            readback = goal.combat.weapon_readback
            if readback is not None and readback.get("batch") == goal.combat.calls:
                fields["held_weapon"] = dict(readback)
            session = getattr(self, "_skill_session", None)
            run = getattr(session, "current", None)
            if (getattr(session, "status", None) == "active" and run is not None
                    and run.goal_id == goal.goal_id and run.call.name == "lawan"):
                # Live7 self-defence: the run's swing cap (skills.LAWAN_MAX_SWINGS) counts every settled swing.
                swings = fields.get("swings") if type(fields.get("swings")) is int else 0
                run.state["lawan_swings"] = run.state.get("lawan_swings", 0) + swings
                run.state["lawan_calls"] = run.state.get("lawan_calls", 0) + 1
                run.state["lawan_last_verdict"] = fields.get("verdict")
            if (getattr(session, "status", None) == "active" and run is not None
                    and run.goal_id == goal.goal_id and run.call.name == "lawan_mine"
                    and new_result and cleanup and evidence.lifecycle == "completed"
                    and not getattr(goal.combat, "disengage", False)):  # ADR 183: a body leg is no fight batch
                # GAP8E: every settled S15 batch is a progress checkpoint for a contact defence.
                run.state["mines_reflex_batches"] = run.state.get("mines_reflex_batches", 0) + 1
                pin = self._water_context(self._binding_view()[1])
                run.state["mines_last_batch"] = dict(
                    goal_id=goal.goal_id,
                    batches=run.state["mines_reflex_batches"], location=getattr(pin, "location", None),
                    swings=fields.get("swings") if type(fields.get("swings")) is int else None,
                    killed=str(fields.get("verdict") or "").startswith("MonsterKilled"),
                    damage_dealt=fields.get("damage_dealt"), target=fields.get("target"),
                    held_weapon=fields.get("held_weapon"))
                from arti_runtime.mines_contact import keep_batch
                keep_batch(self, run.state["mines_last_batch"])  # ADR 174: damage 0 stays a fact on her next page
            if (getattr(goal.combat, "escape", False) and new_result and cleanup
                    and evidence.lifecycle == "completed" and fields.get("verdict") == "Aborted(timeout)"
                    and combat.batch_progress(fields) is None):
                goal.combat = replace(goal.combat, status="BLOCKED", reason="escape_no_observed_progress")
            if (getattr(session, "status", None) == "active" and run is not None
                    and run.goal_id == goal.goal_id and run.call.name == "lawan_mine"
                    and new_result and cleanup and evidence.lifecycle == "completed"
                    and not goal.combat.escape
                    and fields.get("verdict") == "Aborted(timeout)"
                    and fields.get("world_effect_status") == "unchanged"
                    and goal.combat.kills == 0):
                # ADR 153 (fix87B): one 4 s batch is a slice of the fight. While the native readback shows the fight
                # moving (target HP fell, or still closing in), the next bounded batch continues it; HP, curfew and the
                # skill's own guards still run between batches, and the reflex keeps its 40% floor inside one.
                progress = combat.batch_progress(fields)
                done = run.state.setdefault("mines_fight_continued", {})
                n = done.get(goal.goal_id, 0) if isinstance(done, dict) else 0
                still_open = combat.combat_open(goal.combat)
                if progress is not None and n < self.MINES_FIGHT_CONTINUE_MAX and still_open:
                    done[goal.goal_id] = n + 1
                    continued = dict(progress=progress, batch=n + 1, cap=self.MINES_FIGHT_CONTINUE_MAX)
                    self._journal({"ev": "mines_fight_continued", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                                   **continued, "damage_dealt": fields.get("damage_dealt"), "target": fields.get("target")})
                else:
                    # ADR 146: a stopped fight with no observed progress (or past the cap) is over for this floor.
                    # a60's retry/re-pin loop spent the escape reserve without another combat input.
                    # The fresh HP/threat/nav read decides floor retreat versus survival exit (ADR 146 amendment).
                    run.state.update(wave1_combat_timed_out=True)
                    defense = run.state.get("mines_defend")
                    if isinstance(defense, dict):
                        defense["active"] = False
                    goal.combat = replace(goal.combat, status="BLOCKED", reason="mines_combat_timeout")
                    self._journal({"ev": "mines_fight_stopped", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                                   "why": ("no_observed_progress" if progress is None else "fight_continue_cap"
                                           if still_open else "reflex_call_budget"),
                                   "continued": n, "damage_dealt": fields.get("damage_dealt"), "target": fields.get("target")})
            hidden = combat.unhittable_fact(fields) if evidence.lifecycle == "completed" and cleanup else None
            if hidden is not None and combat.combat_open(goal.combat):
                # ADR 153: alive but not hittable now (a burrowed Duggy): a native fact, never a retried fight.
                goal.combat = replace(goal.combat, status="BLOCKED", reason="monster_unhittable:" + hidden)
            self._journal({"ev": "reflex_combat_result", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                           "lifecycle": evidence.lifecycle, "objective_status": goal.combat.status,
                           "objective_reason": goal.combat.reason, "kills": goal.combat.kills,
                           "reflex_calls": goal.combat.calls, **fields})
            if goal.combat.status != before:
                ev = {"KILLED": "reflex_combat_killed", "DOWNED": "reflex_combat_downed",
                      "RETREATED": "reflex_combat_retreated"}.get(goal.combat.status)
                if ev:
                    self._journal({"ev": ev, "goal_id": goal.goal_id, "request_id": evidence.request_id,
                                   "kills": goal.combat.kills, "downed": fields.get("downed")})
            if goal.combat.heal_pending:
                self._journal({"ev": "reflex_combat_heal_needed", "goal_id": goal.goal_id, "food_slot": goal.combat.food_slot})
            rock = combat.route_blocked_rock(goal.combat.last_detail) if evidence.lifecycle == "completed" else None
            if rock is not None and combat.combat_open(goal.combat):
                # FIXB15 D-B13-3 (live V2B13ACC11b: 5x Aborted(stuck) into the Stone (9,22) until the call budget): the
                # reflex route needs this rock broken: the clear_path leg takes it before the next reflex.
                started = self._clear_path_start(goal, rock=rock)
                self._journal({"ev": "reflex_combat_route_blocked", "goal_id": goal.goal_id, "rock": list(rock),
                               "clear_path": started})
            if (evidence.lifecycle in ("rejected", "not_taken") and combat.combat_open(goal.combat)
                    and goal.combat_rearms < self.COMBAT_REARMS_MAX):
                # FIXB13 D-B10-1 (live V2B10ACC11a: after `NoTarget target_left_radius` the next reflex was refused and
                # the runner HELD): a refused reflex re-arms the combat rule on fresh facts (bounded per goal).
                goal.combat_rearm_due = True
            status = goal.combat.status
        elif operation == combat.OPERATION_CHARGE and getattr(goal, "charge", None) is not None:
            goal.charge = combat.settle_charge(goal.charge, request_id=evidence.request_id, lifecycle=evidence.lifecycle,
                                               cleanup=cleanup, envelope=envelope)
            self._journal({"ev": "reflex_charge_result", "goal_id": goal.goal_id, "request_id": evidence.request_id,
                           "lifecycle": evidence.lifecycle, "objective_status": goal.charge.status,
                           "objective_reason": goal.charge.reason, "reflex_calls": goal.charge.calls, **fields})
            status = goal.charge.status
        else:
            return
        if status == "UNCERTAIN":
            self._gather_mutation_unresolved = True

    # ------------------------------------------------------------------ heal hand-off + Mines time rule

    def process_combat_heal(self) -> int:
        """Worker hook: a combat objective waiting for a heal pins the WS3b eat objective (the runtime's own
        routine_eat_rule chain selects the food, presses A and answers Yes); once the eat effect is verified it is
        unpinned and the combat objective may re-arm. Also blocks the objective when the Mines must be left (24:00)."""
        try:
            with self._lock:
                goal = self._active
                objective = getattr(goal, "combat", None) if goal is not None else None
                if objective is None or goal.progress_marker == "terminal":
                    return 0
                if objective.escape or objective.disengage:
                    return 0  # ADR 169/177: the exit owns food/route/fight handoffs
                if combat.combat_open(objective) and not objective.heal_pending:
                    pin = self._water_context(self._binding_view()[1])
                    facts = self._combat_ui_facts(pin)
                    if facts is not None and combat.leave_mines_due(facts.get("combat")):
                        goal.combat = combat.time_blocked(objective)
                        self._journal({"ev": "reflex_combat_time_rule", "goal_id": goal.goal_id, "reason": "leave_mines",
                                       "time_of_day": (facts.get("combat") or {}).get("time_of_day")})
                        return 1
                    low = combat.idle_hp_floor(facts.get("combat") if facts is not None else None)
                    if (low is not None and goal.play_mode == "local_activity" and self._v2_pad_path(pin)
                            and not self._action_busy):
                        # D-B6-2 (live V2B6ACC11b: HP 100 -> 36 while ARTI stood still, no reflex running): below the
                        # reflex HP floor while idle, the heal hand-off runs (eat objective) before any re-arm.
                        goal.combat = combat.heal_needed(objective)
                        self._journal({"ev": "reflex_combat_idle_hp_floor", "goal_id": goal.goal_id, **low})
                        return 1
                    return 0
                if not objective.heal_pending:
                    return 0
                routine = getattr(goal, "routine_objective", None)
                if routine is None:
                    goal.routine_objective = ib.RoutineObjective(ib.OBJ_EAT)
                    self._journal({"ev": "reflex_combat_heal_pinned", "goal_id": goal.goal_id, "food_slot": objective.food_slot,
                                   "pinned": asdict(goal.routine_objective)})
                    return 1
                if routine.kind != ib.OBJ_EAT:
                    return 0
                if getattr(routine, "eat_effect", None):
                    goal.routine_objective = None
                    goal.combat = combat.healed(objective)
                    self._journal({"ev": "reflex_combat_healed", "goal_id": goal.goal_id, "heals": goal.combat.heals})
                    return 1
                if routine.blocker or routine.stop:
                    goal.routine_objective = None
                    goal.combat = combat.heal_failed(objective, str(routine.blocker or routine.stop))
                    self._journal({"ev": "reflex_combat_heal_failed", "goal_id": goal.goal_id, "reason": goal.combat.reason})
                    return 1
                return 0
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            self._journal({"ev": "reflex_combat_heal_error", "error": type(exc).__name__})
            return 0

    # ------------------------------------------------------------------ finish (model path + rule)

    def _combat_finish_verdict(self, goal):
        """FinishEvidenceView for a combat / charge objective (verified progress only), or None for other goals."""
        from arti_stardew_runtime import FinishEvidenceView
        objective = getattr(goal, "combat", None) if goal is not None else None
        verdict = combat.combat_finish_verdict(objective)
        if verdict is not None:
            return FinishEvidenceView(verdict[0], verdict[1], {"objective": combat.OBJ_KILL, "target": objective.kind,
                                                               "kills": objective.kills, "count": objective.count,
                                                               "request_id": objective.killed_request_id,
                                                               "reflex_calls": objective.calls})
        charge = getattr(goal, "charge", None) if goal is not None else None
        verdict = combat.charge_finish_verdict(charge)
        if verdict is not None:
            return FinishEvidenceView(verdict[0], verdict[1], {"objective": combat.OBJ_CHARGE, "tool": charge.tool,
                                                               "level": charge.level, "tiles_changed": charge.tiles_changed,
                                                               "request_id": charge.charged_request_id,
                                                               "reflex_calls": charge.calls})
        return None

    def _combat_rule_state(self, goal):
        objective = getattr(goal, "combat", None) if goal is not None else None
        if objective is not None and objective.escape and getattr(goal, "mines_exit", None):
            return None  # its kill resumes the exit; it is not an alive departure
        if objective is not None and objective.status == "KILLED":
            return combat.OPERATION_COMBAT, objective.killed_request_id
        charge = getattr(goal, "charge", None) if goal is not None else None
        if charge is not None and charge.status == "CHARGED":
            return combat.OPERATION_CHARGE, charge.charged_request_id
        return None

    def combat_finish_due(self) -> bool:
        """Caller holds the runtime lock (the trial runner's idle check): a verified kill / charge awaits the worker's
        rule finish, or a heal hand-off is in progress, so the runner must not declare the goal held meanwhile."""
        goal = self._active
        if goal is None or goal.progress_marker == "terminal" or self._gather_mutation_unresolved:
            return False
        objective = getattr(goal, "combat", None)
        if getattr(goal, "mines_exit", None) is not None:
            return False  # FIXB17 D-B5-0: the exit leg owns the goal (extras_finish ends it)
        return self._combat_rule_state(goal) is not None or (objective is not None and objective.heal_pending)

    def process_combat_finish(self) -> int:
        """Worker hook: a verified MonsterKilled (count reached) / ChargeReached (tiles changed) -> the runtime records the
        goal finish itself (combat_rule / charge_rule), like WS-G4's fishing_rule."""
        try:
            result, operation = self._combat_rule_finish()
            if getattr(self, "_mines_exit_resume_due", False):
                # FIXB17 D-B5-0: the exit leg was pinned instead of the finish: one fresh inspect + continuation drives it.
                self._mines_exit_resume_due = False
                self._resume_after_pause(reason="mines_exit")
                return 1
            if result is not None and self._on_generation_result is not None:
                from types import SimpleNamespace
                reason = "reflex_combat_finish" if operation == combat.OPERATION_COMBAT else "reflex_charge_finish"
                self._on_generation_result(SimpleNamespace(reason=reason, generation_id=None), result)
            return 1 if result is not None else 0
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            self._journal({"ev": "reflex_combat_finish_error", "error": type(exc).__name__})
            return 0

    def _combat_rule_finish(self):
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        with self._lock:
            goal = self._active
            state = self._combat_rule_state(goal)
            if state is None or goal.progress_marker == "terminal":
                return None, None
            if self._action_busy or self._inflight_generation is not None or getattr(self, "_results_pending", 0):
                return None, None
            if self._gather_mutation_unresolved:
                return None, None
            operation, request_id = state
            action = goal.last_action
            if action is None or action.request_id != request_id or not action.cleanup_confirmed:
                return None, None
            if getattr(goal, "mines_exit", None) is not None:
                return None, None  # FIXB17 D-B5-0: the exit leg's finish (extras_rule) ends this goal
            base = self._combat_finish_verdict(goal)
            pin = self._water_context(self._binding_view()[1])
            location = getattr(pin, "location", None) or ""
            session = getattr(self, "_skill_session", None)
            run = session.current if session is not None and session.status == "active" else None
            floor_combat = (run is not None and run.goal_id == goal.goal_id and run.call.name == "lawan_mine"
                            and run.state.get("wave1_driver") == "combat_floor")
            if (operation == combat.OPERATION_COMBAT and str(location).startswith("UndergroundMine")
                    and base.status == "achieved" and not floor_combat):
                # FIXB17 D-B5-0 (PLAN §5.3 step 11: `observed_monster_killed` + `exited_mines`): in the Mines the kill is
                # not the end: the exit leg runs first (AFK default: leave by the level's up-ladder, answer Leave).
                self.mines_exit_pin(goal, location, {"reason": base.reason, "kills": goal.combat.kills,
                                                     "killed_types": list(goal.combat.killed_types)})
                self._pending_continuation = None
                self._mines_exit_resume_due = True
                return None, None
            origin = combat.RULE_COMBAT if operation == combat.OPERATION_COMBAT else combat.RULE_CHARGE
            verdict = FinishEvidenceView("achieved", base.reason, {"origin": origin, **dict(base.observed or {})})
            goal.last_finish = verdict
            goal.progress_marker = "terminal"
            goal.revision += 1
            self._pending_continuation = None
            self._pending_event_reason = None
            self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                           "verdict": asdict(verdict), "origin": origin})
        speech = ("Monsternya sudah dikalahkan; terverifikasi dari hasil refleks mod." if operation == combat.OPERATION_COMBAT
                  else "Alatnya sudah di-charge dan petaknya berubah; terverifikasi dari hasil refleks mod.")
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish", speech=StatusSpeech(speech),
                               finish_evidence=verdict), operation
