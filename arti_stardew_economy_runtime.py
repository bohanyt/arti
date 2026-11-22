"""Primitives v2 WS-G2 runtime side: economy objectives (sell / buy / craft / ship / take), the economy filter on the
G1 ui_press offer, the ``economy_transfer`` operation (container_transfer modes ship_one / ship_stack / take_out),
the item policy gate (journal ``item_policy_refused``) and the deterministic finish rule (``economy_rule``).

Kept out of ``arti_stardew_runtime.py`` (several workstreams edit it): ``StardewGoalRuntime`` inherits
:class:`EconomyRuntimeMixin` and calls a handful of hooks. Every physical step reuses WS2's leased path
(``_run_leased_once``: ACQUIRE -> one guarded input_once -> RELEASE, strictly newer ui.context readback). The runtime
never invents a target: ui_press pins come from the mod's ``ui_pad`` block, transfer args from its ``shipping_bin`` /
``container`` blocks. Economy presses (buy / sell / craft) and transfers are offered ONLY for an active owner economy
objective naming that item, only when the menu is up and no routine owns it (G1 rule 8), and only when both the mod's
policy and the runtime's ``stardew_item_policy`` table allow it.

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G2 live witnesses.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

from collections.abc import Mapping
from dataclasses import asdict

import time
from arti_stardew_pause import game_now, wait_resume

import arti_stardew_command as command
import arti_stardew_economy as economy
from arti_stardew_ui_key_runtime import LeasedRun

ECONOMY_OPS = (economy.ECONOMY_OP,)
# economy_rule close outcomes that leave the menu open with an item on the cursor: never a clean finish.
_CLOSE_LEFT_OPEN = frozenset({"held_item"})
_POLICY_REASONS = ("never_list", "not_allowlisted", "mod_policy:", "over_buy_max_total", "not_enough_money",
                   "tool_upgrade_not_allowed", "craft_denied", "trash_never")


def _economy_context(view) -> bool:
    return view is not None and (view.context in ("shop", "crafting")
                                 or (view.context == "game_menu" and view.page_type == "CraftingPage"))


class EconomyRuntimeMixin:
    """Mixed into StardewGoalRuntime; uses its lock, stores, leased helpers and journal."""

    def _init_economy_runtime(self) -> None:
        self._item_policy = economy.ItemPolicy.from_config(self._config)
        self._economy_seen: set = set()

    # ------------------------------------------------------------------ flag / objective
    def _economy_flag_configured(self) -> bool:
        """The mod's EconomyMenusEnabled flag: session.json carries economy_enabled only when it is on."""
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            from arti_stardew_runtime import _SESSION_READ_RETRY_SECONDS
            return "economy_enabled" in self._commands.baca_sesi(folder, retry_detik=_SESSION_READ_RETRY_SECONDS).mentah
        except Exception:  # noqa: BLE001 - unreadable session = flag unknown = no economy objective
            return False

    def _pin_economy_objective(self, goal, objective, request) -> None:
        goal.economy_objective = objective
        self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "pinned": asdict(objective),
                       "origin": "owner_economy_objective"})

    @staticmethod
    def _economy_objective(goal):
        return getattr(goal, "economy_objective", None) if goal is not None else None

    def _economy_journal_once(self, key: tuple, record: dict) -> None:
        if key in self._economy_seen:
            return
        if len(self._economy_seen) > 512:
            self._economy_seen.clear()
        self._economy_seen.add(key)
        self._journal(record)

    # ------------------------------------------------------------------ ui_press filter (shop / crafting)
    # ------------------------------------------------------------------ shop counter opener (fix D-B2-1)
    @staticmethod
    def _economy_counter_goal(goal) -> bool:
        """A sell / buy objective still open: the shop counter (mod kind shop_counter) is its world opener."""
        objective = getattr(goal, "economy_objective", None) if goal is not None else None
        return (objective is not None and objective.kind in (economy.KIND_SELL, economy.KIND_BUY) and not objective.uncertain
                and not objective.finish_recorded and (objective.remaining is None and objective.done == 0
                                                       or (objective.remaining or 0) > 0))

    @staticmethod
    def _economy_ship_goal(goal) -> bool:
        """FIXB7 D-B5b-3: a ship objective still open: the shipping bin (mod kind shipping_bin) is its world opener."""
        objective = getattr(goal, "economy_objective", None) if goal is not None else None
        return (objective is not None and objective.kind == economy.KIND_SHIP and not objective.uncertain
                and not objective.finish_recorded and (objective.remaining is None and objective.done == 0
                                                       or (objective.remaining or 0) > 0))

    def _economy_world_offered(self, goal, target) -> bool:
        """world_interact of the shop counter for an open sell / buy objective (the mod refuses a closed counter), or of
        the shipping bin for an open ship objective (FIXB7 D-B5b-3)."""
        if not isinstance(target, Mapping):
            return False
        if target.get("kind") == economy.KIND_SHIPPING_BIN:
            return self._economy_ship_goal(goal)
        return target.get("kind") == economy.KIND_SHOP_COUNTER and self._economy_counter_goal(goal)

    def _economy_approach_entries(self, pin, motor_tile) -> list:
        """Approach entries for the shop counters the mod published (extras_targets kind shop_counter), or the shipping
        bins (shipping_bins) for a ship objective (FIXB7 D-B5b-3)."""
        goal = self._active
        if pin is None:
            return []
        rec = self._obs.get(pin.player_id, "ui.context")
        facts = rec.facts if rec is not None else None
        if self._economy_ship_goal(goal):
            return economy.bin_entries(facts)
        if not self._economy_counter_goal(goal):
            return []
        return economy.counter_entries(facts, tuple(motor_tile))

    SHIP_DIRECT_MAX = 16

    def _economy_ship_rule_invoke(self, request):
        """FIXB36: drive an open v2 ship continuation from its offered facts before calling the provider.

        Reuse the economy picker and ordinary execution gates. Six-input approach batches, the 40-proposal /
        300-second parent cap, trial reservations and no-progress/replan accounting still bound the route.
        This also prevents a model's unrelated 60-tick move from replacing the bin approach after a replan.
        """
        from arti_stardew_auto_act import economy_auto_choice
        from arti_stardew_runtime import _CONTINUATION_REASONS, READY
        with self._lock:
            goal, snap = self._active, request.snapshot
            pin = getattr(snap, "movement_context", None) or getattr(snap, "water_context", None)
            if (not self._economy_ship_goal(goal) or request.goal_id != goal.goal_id
                    or goal.play_mode != "local_activity" or goal.progress_marker == "terminal"
                    or self._status != READY or goal.question_basis or goal.no_progress_blocked
                    or goal.no_progress_until is not None or not self._v2_pad_path(pin)
                    or self.curfew_window_now() or goal.gather_progress is not None or goal.tool_work is not None
                    or not (request.reason in _CONTINUATION_REASONS or request.trigger_type == "game")
                    or goal.ship_direct >= self.SHIP_DIRECT_MAX):
                return None
            picked = economy_auto_choice(goal.economy_objective, snap)
            if picked is None:
                return None
            operation, target_ref, args, label = picked
            goal.ship_direct += 1
            self._journal({"ev": "ship_rule_direct", "goal_id": goal.goal_id,
                           "generation_id": request.generation_id, "operation": operation,
                           "target": label, "count": goal.ship_direct, "max": self.SHIP_DIRECT_MAX})
            return self._rule_json(request, {"kind": "act", "operation": operation, "target_ref": target_ref,
                                            "args": dict(args or {}), "evidence_refs": []}, "ship_rule", label)

    def _economy_pad_button(self, goal) -> str | None:
        """D-B2-1 (live V2B2G2craft-a): a craft objective with no menu open offers the world pad Y (the mod's
        PadContextTable: Y = GameMenu on the crafting tab); nothing else of the economy opens by a pad button."""
        objective = self._economy_objective(goal)
        if (objective is None or objective.kind != economy.KIND_CRAFT or objective.done > 0 or objective.uncertain
                or objective.finish_recorded):
            return None
        return "Y"

    def _economy_focus(self, goal):
        """The active objective's item for the compact economy menu view (D-B2-2), or None."""
        return economy.focus_of(self._economy_objective(goal))

    def _economy_filter_offer(self, goal, view, offer: dict | None) -> dict | None:
        """Withhold A / X on economy rows unless the active objective names that item and both policies allow it."""
        if offer is None or not _economy_context(view):
            return offer
        objective = self._economy_objective(goal)
        if not economy.batch_receiver_current(objective, view.receiver_token):
            return None
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        item_policy = self._item_policy
        if (getattr(session, "mode", None) == "part_live" and getattr(session, "status", None) == "active"
                and run is not None and run.status == "running" and goal is not None
                and run.goal_id == goal.goal_id and run.call.name == "beli"):
            from arti_stardew_live7 import seed_buy_policy
            item_policy = seed_buy_policy(item_policy, (view.economy or {}).get("money"))
        if (session is not None and session.status == "active" and run is not None
                and run.status == "running" and goal is not None and run.goal_id == goal.goal_id
                and run.call.name == "masak" and not run.state.get("wave1_driver")):
            # W1 gap B3 S30: X cooks ONE of the pinned owner-named recipe on the pinned standalone cooking page; A only
            # stashes that recipe's output from the cursor into an inventory slot. Never another row, never A-qty.
            from arti_stardew_skills_attempts import cook_press_allowed
            allowed = cook_press_allowed(run.state.get("attempt", {}).get("cook") or {}, view, self._item_policy)
            return dict(offer, press={k: v for k, v in offer["press"].items() if k not in ("A", "X") or k in allowed})
        if (session is not None and session.status == "active" and run is not None
                and run.status == "running" and goal is not None and run.goal_id == goal.goal_id
                and run.call.name == "upgrade_alat" and not run.state.get("wave1_driver")):
            pinned = run.state.get("attempt", {}).get("upgrade_row") or {}
            row = next((r for r in (view.economy or {}).get("rows", ()) if r.get("id") == view.snapped_id), {})
            allowed = ((view.economy or {}).get("shop_id") == "ClintUpgrade" and self._item_policy.allow_tool_upgrade
                and (view.economy or {}).get("upgrade_allowed") is True and row.get("item") == pinned.get("item")
                and pinned.get("receiver") == view.receiver_token and row.get("price") == pinned.get("price")
                and row.get("required_tool") == pinned.get("required_tool") and row.get("trade_item") == pinned.get("trade_item")
                and row.get("trade_count") == pinned.get("trade_count") and row.get("trade_have", 0) >= row.get("trade_count", 1))
            return dict(offer, press={k: v for k, v in offer["press"].items()
                if k not in ("A", "X") or k == "A" and allowed})
        if view.economy is not None and economy.bind_item(objective, economy.economy_candidates(view.economy)):
            self._journal({"ev": "economy_item_bound", "goal_id": goal.goal_id, "item_id": objective.item_id,
                           "item_name": objective.item_name, "origin": "ui_pad_view"})
        elif (getattr(self, "_skill_session", None) is not None
              and self._skill_session.status == "active" and self._skill_session.current is not None
              and self._skill_session.current.goal_id == goal.goal_id
              and self._skill_session.current.call.name == "beli"
              and self._skill_session.current.call.arg == "benih"
              and economy.bind_seed_item(objective, view.economy, item_policy,
                                         allow_quantity=getattr(session, "mode", None) == "part_live")):
            self._journal({"ev": "economy_item_bound", "goal_id": goal.goal_id, "item_id": objective.item_id,
                           "item_name": objective.item_name, "origin": "skill_seed_stock"})
        elif view.economy is not None and economy.bind_category_item(objective, view.economy,
                                                                     self._inventory_slot_categories()):
            # FIXB24 D-B21-4: "jual satu ikan" -> any fish in the bag
            self._journal({"ev": "economy_item_bound", "goal_id": goal.goal_id, "item_id": objective.item_id,
                           "item_name": objective.item_name, "origin": "fish_category"})
        press = dict(offer["press"])
        payloads: dict = {}
        for btn in ("A", "X"):
            if btn not in press:
                continue
            if view.economy is None:
                reason = "economy_block_unavailable"
            else:
                payload = economy.press_payload(objective, view.economy, view.snapped_id) if btn == "A" else {}
                reason = economy.press_refusal(objective, item_policy, view.economy, view.snapped_id, btn, payload,
                                               held_item=view.held_item)
                if reason is None and payload:
                    payloads[btn] = payload
            if reason is None:
                continue
            press.pop(btn)
            role, entry = economy.snapped_role(view.economy, view.snapped_id)
            ev = "item_policy_refused" if reason.startswith(_POLICY_REASONS) else "economy_press_withheld"
            self._economy_journal_once((view.target_token, btn, reason), {
                "ev": ev, "goal_id": getattr(goal, "goal_id", None), "receiver_token": view.receiver_token,
                "context": view.context, "btn": btn, "role": role, "item": (entry or {}).get("item"), "reason": reason})
        # D-B2-2: the model view keeps the objective's row (now bound) + the snap neighbourhood, bounded.
        offer = dict(offer, press=press, view=view.model_view(economy.focus_of(objective)))
        if payloads:
            offer["payload"] = payloads
        summary = economy.objective_summary(objective)
        if summary is not None:
            offer["economy_goal"] = summary
        return offer

    def _inventory_slot_categories(self) -> dict:
        """FIXB24 D-B21-4: {slot: item category} from the mod's progress.inventory slot facts (read-only)."""
        try:
            _, pid = self._binding_view()
            rec = self._obs.get(pid, "progress.inventory") if pid is not None else None
            facts = rec.facts if rec is not None and isinstance(rec.facts, Mapping) else {}
            return {f["slot"]: f["category"] for f in facts.values()
                    if isinstance(f, Mapping) and type(f.get("slot")) is int and type(f.get("category")) is int}
        except Exception:  # noqa: BLE001 - a read never raises
            return {}

    def _economy_press_reason(self, offer: Mapping, args: Mapping) -> str | None:
        """A model ui_press must carry exactly the offered payload for its button (none when none was offered)."""
        extras = {k: args[k] for k in command.UI_PRESS_ECONOMY_ARGS if k in args}
        wanted = (offer.get("payload") or {}).get(args.get("btn"), {})
        return None if extras == wanted else "economy_payload_not_offered"

    def _economy_note_press(self, goal, operation: str, run: LeasedRun, before) -> None:
        objective = self._economy_objective(goal)
        if objective is None or operation != "ui_press" or not _economy_context(before):
            return
        label = economy.note_press(objective, run.economy, verified=run.verified, uncertain=run.uncertain)
        if label is None and run.uncertain and run.submitted:
            objective.uncertain = "ui_press_uncertain:" + (run.reason or "")[:60]
            label = "uncertain"
        if label is not None:
            self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "economy_objective": asdict(objective),
                           "origin": "ui_press", "label": label, "economy": run.economy})

    # ------------------------------------------------------------------ economy_transfer (bin / chest)
    def _economy_transfer_targets(self, goal, player_id: int | None) -> list[dict]:
        objective = self._economy_objective(goal)
        if objective is None or objective.kind not in (economy.KIND_SHIP, economy.KIND_TAKE) or player_id is None:
            return []
        rec, _ = self._ui_key_record(player_id)
        if rec is None or self._day_start_active:
            return []
        bound_before = objective.item_id
        targets = (economy.bin_targets(objective, self._item_policy, rec.facts) if objective.kind == economy.KIND_SHIP
                   else economy.chest_take_targets(objective, rec.facts))
        if bound_before is None and objective.item_id is not None:
            self._journal({"ev": "economy_item_bound", "goal_id": goal.goal_id, "item_id": objective.item_id,
                           "item_name": objective.item_name, "origin": objective.kind})
        blocker = getattr(objective, "take_blocker", None)
        if objective.kind == economy.KIND_TAKE and not targets and isinstance(blocker, dict):
            self._economy_journal_once(("take_bag_full", blocker.get("room"), blocker.get("stack")), {
                "ev": "economy_take_bag_full", "goal_id": goal.goal_id, "item": objective.item_id, **blocker})
        valid = []
        for args in targets:
            try:
                command.validate_container_transfer_args(args)
            except command.KesalahanPerintah:
                continue
            valid.append(args)
        if objective.kind == economy.KIND_SHIP and objective.item_id is not None and not valid:
            block = rec.facts.get("shipping_bin") if isinstance(rec.facts.get("shipping_bin"), Mapping) else None
            for slot in (block or {}).get("actor_slots") or ():
                item = slot.get("item") if isinstance(slot, Mapping) else None
                if isinstance(item, Mapping) and economy._qualify(item.get("qualified_item_id")) == objective.item_id:
                    reason = self._item_policy.refuse_dispose(economy.KIND_SHIP, objective.item_id, None,
                                                              mod_policy=slot.get("ship_policy"), mod_priced=True)
                    if reason:
                        self._economy_journal_once(("ship", block.get("readback_token"), reason), {
                            "ev": "item_policy_refused", "goal_id": goal.goal_id, "action": "ship",
                            "item": objective.item_id, "reason": reason})
        return valid

    def _economy_offer(self, goal, binding, player_id: int | None) -> dict | None:
        if binding.status != "bound" or player_id is None:
            return None
        objective = self._economy_objective(goal)
        if objective is None or objective.kind not in (economy.KIND_SHIP, economy.KIND_TAKE):
            return None
        if objective.kind == economy.KIND_TAKE and (getattr(goal, "gather_store_required", False)
                                                     or getattr(goal, "episode_plan", None)):
            return None  # a store chain owns the chest (PLAN 5.3 rule 8)
        targets = self._economy_transfer_targets(goal, player_id)
        if not targets:
            return None
        ctx = self._ui_key_context(player_id)
        if ctx is None or not self._economy_slot_free(ctx):
            return None
        return {"targets": targets, "economy_goal": economy.objective_summary(objective)}

    def _economy_slot_free(self, ctx) -> bool:
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            self._ui_key_session(folder, ctx, enabled_key="container_transfer_enabled", primitive="container_transfer",
                                 acquired=False)
            return True
        except (OSError, ValueError, TypeError, command.KesalahanPerintah):
            return False

    @staticmethod
    def _economy_catalog(offer: dict) -> dict:
        return {"target_ref": None, "observed_targets": offer["targets"],
                "args": "copy one observed target exactly (mode ship_one / ship_stack / take_out)",
                "result_scope": "one item move; paired slot / bin or chest readback"}

    def _economy_model_reason(self, request, args) -> str | None:
        offer = getattr(request.snapshot, "economy_offer", None)
        if offer is None:
            return "economy_transfer_not_offered"
        if not isinstance(args, dict) or (args not in offer["targets"] and not any(
                all(t.get(k) == args.get(k) for k in self._REBIND_KEYS) for t in offer["targets"])):
            return "economy_transfer_target_not_offered"
        if self._day_start_active:
            return "day_start_active"
        return None

    def _economy_precheck_decision(self, request, args) -> None:
        reason = self._economy_model_reason(request, args)
        if reason:
            raise ValueError(reason)
        _, pid = self._binding_view()
        if self._economy_rebind(args, pid) not in self._economy_transfer_targets(self._active, pid):
            raise ValueError("economy_transfer_target_changed")

    _REBIND_KEYS = ("mode", "slot", "qualified_item_id", "quality", "source_fact_id", "stack", "owner_ceiling")

    def _economy_rebind(self, args: dict, pid) -> dict:
        """D-B7b-3: the same move (mode, slot, item, quality, chest, stack) on the FRESH container view: its receiver /
        container / readback / target tokens are re-bound right before the transfer (a refreshed view is not a
        different target). Returns the fresh args, or the given ones when no identical move is offered now."""
        fresh = self._economy_transfer_targets(self._active, pid)
        if args in fresh:
            return args
        same = [t for t in fresh if all(t.get(k) == args.get(k) for k in self._REBIND_KEYS)]
        if len(same) == 1:
            self._journal({"ev": "economy_transfer_rebound", "mode": args.get("mode"), "slot": args.get("slot")})
            return dict(same[0])
        return args

    def _run_economy_transfer(self, args: dict, *, origin: str) -> LeasedRun:
        _, pid = self._binding_view()
        args = self._economy_rebind(args, pid)
        ctx = self._ui_key_context(pid)
        self._journal({"ev": economy.ECONOMY_OP, "phase": "request", "origin": origin, "args": dict(args)})
        if ctx is None:
            run = LeasedRun("not_taken", "ui_context_unavailable")
        else:
            def revalidate():
                if args not in self._economy_transfer_targets(self._active, pid):
                    raise ValueError("economy_transfer_target_changed")
            try:
                run = self._run_leased_once("container_transfer", args, ctx, origin="econ-" + origin, revalidate=revalidate)
            except Exception as exc:  # noqa: BLE001 — never let a step escape as an exception
                run = LeasedRun("not_taken", type(exc).__name__)
        self._journal({"ev": economy.ECONOMY_OP, "phase": "result", "origin": origin, "request_id": run.request_id,
                       "status": run.lifecycle, "outcome": run.outcome, "verified": run.verified, "cleanup": run.cleanup,
                       "reason": run.reason})
        if run.hard_uncertain:
            from arti_stardew_runtime import PAUSED_RECOVERY
            with self._lock:
                self._status = PAUSED_RECOVERY
                self._revoke_play()
                self._pending_event_reason = None
        return run

    def _execute_economy_decision(self, decision, request, goal):
        from arti_stardew_runtime import ActionEvidenceView, OwnerTurnResult
        args = dict(decision.choice.args)
        with self._lock:
            reason = self._economy_model_reason(request, args)
        run = LeasedRun("not_taken", reason) if reason else self._run_economy_transfer(args, origin="model")
        objective = self._economy_objective(goal)
        label = economy.note_transfer(objective, args, verified=run.verified, outcome=run.outcome,
                                      uncertain=run.uncertain or not run.cleanup, request_id=run.request_id,
                                      her_menu_pick=False)  # the goal's own transfer: only her menu picks wait for her close
        if label is not None:
            self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "economy_objective": asdict(objective),
                           "origin": economy.ECONOMY_OP, "label": label})
        evidence = ActionEvidenceView(
            run.lifecycle, run.reason, run.request_id, run.outcome,
            before={economy.ECONOMY_OP: {k: v for k, v in args.items() if k not in ("target_token", "readback_token")}},
            after=({economy.ECONOMY_OP: {"outcome": run.outcome}} if run.request_id else None),
            world_effect_status=("observed_paired_transfer" if run.verified else "unchanged"
                                 if run.lifecycle == "completed" and not run.applied
                                 else "zero_effect" if not run.submitted else "uncertain"),
            cleanup_confirmed=run.cleanup)
        with self._lock:
            superseded = (not self._request_current(request) or self._active is not goal or self._action_cancel.is_set())
            if not superseded and goal is not None:
                goal.last_action = evidence
            if self._inflight_generation == request.generation_id:
                self._inflight_generation = None
            self._action_busy = False
            self._journal({"ev": "action_result", "goal_id": request.goal_id, "action": asdict(evidence)})
            if not superseded:
                self._schedule_continuation(decision, request)
        if not superseded and run.verified and run.cleanup:
            self._resume_after_pause(reason="action_settled")
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=not superseded and run.verified,
                               discarded=superseded, reason="superseded" if superseded else run.reason,
                               action_evidence=evidence)

    # ------------------------------------------------------------------ economy_rule shop stage (D-B5-1)
    def _economy_shop_ready(self):
        """Under the lock: (goal, objective) when the deterministic shop stage may act now, else None."""
        from arti_stardew_runtime import READY
        goal = self._active
        objective = self._economy_objective(goal)
        if (objective is None or objective.kind not in (economy.KIND_SELL, economy.KIND_BUY)
                or goal.progress_marker == "terminal" or goal.play_mode != "local_activity" or self._status != READY
                or self._worker_stop.is_set() or self._day_start_active or self._action_busy
                or self._inflight_generation is not None or getattr(self, "_results_pending", 0)
                or self._trial_result_pending() or objective.shop_rule_stop is not None
                or objective.shop_rule_steps >= economy.SHOP_RULE_STEPS_MAX):
            return None
        return goal, objective

    def _economy_shop_stage_owns(self, goal) -> bool:
        """D-B6-1: the deterministic shop stage owns the goal while the ShopMenu is open for an open sell / buy objective
        (until its stop hands the menu back, or the economy_rule finish closes it)."""
        objective = self._economy_objective(goal)
        if (objective is None or objective.kind not in (economy.KIND_SELL, economy.KIND_BUY) or objective.uncertain
                or objective.finish_recorded or objective.shop_rule_stop is not None
                or objective.shop_rule_steps >= economy.SHOP_RULE_STEPS_MAX):
            return False
        _, pid = self._binding_view()
        view = self._ui_pad_view(pid)
        return view is not None and view.context == "shop"

    def economy_shop_rule_due(self) -> bool:
        """Caller holds the lock (runner idle check): the shop stage acts next (ShopMenu open, objective open)."""
        try:
            ready = self._economy_shop_ready()
            if ready is None:
                return False
            _, pid = self._binding_view()
            view = self._ui_pad_view(pid)
            return view is not None and view.context == "shop" and view.available
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def process_economy_shop_rule(self) -> int:
        """Worker hook (D-B5-1): with the ShopMenu open and a sell / buy objective, step deterministically toward the
        objective's own row (toggle stock <-> inventory, nav by the published neighbour ids) and press the offered
        X (sell one) / A (buy, payload as offered) once there. Every step is the same leased ui_nav / ui_press a model
        step uses; the economy_rule finish then closes the shop. Bounded (SHOP_RULE_STEPS_MAX); a stop hands the menu
        back to the model (fallback) and is journaled once."""
        with self._lock:
            ready = self._economy_shop_ready()
            if ready is None:
                return 0
            goal, objective = ready
            binding, pid = self._binding_view()
            view = self._ui_pad_view(pid)
            if view is None or view.context != "shop":
                return 0
            offer = self._ui_pad_offer(goal, binding, pid, ui_key_offer=None, store_menu=False)
            kind, operation, args, reason = economy.shop_step(objective, view, offer)
            stopped = kind == "stop"
            if kind != "act":
                if stopped:
                    objective.shop_rule_stop = reason
                    self._journal({"ev": "economy_shop_rule", "goal_id": goal.goal_id, "status": "stop", "reason": reason,
                                   "steps": objective.shop_rule_steps})
                else:
                    return 0
            else:
                objective.shop_rule_steps += 1
                self._action_busy = True
        if stopped:
            return self._economy_shop_stop_finish(goal, objective)
        try:
            run, before = self._run_ui_pad(operation, args, origin="economy_rule")
            if operation == "ui_press":
                self._economy_note_press(goal, operation, run, before)
        finally:
            with self._lock:
                self._action_busy = False
        self._journal({"ev": "economy_shop_rule", "goal_id": goal.goal_id, "operation": operation, "reason": reason,
                       "verified": run.verified, "effect": run.effect, "step": objective.shop_rule_steps})
        if objective.shop_rule_steps >= economy.SHOP_RULE_STEPS_MAX and economy.objective_verdict(objective)[0] != "achieved":
            self._journal({"ev": "economy_shop_rule", "goal_id": goal.goal_id, "status": "stop",
                           "reason": "economy_shop_rule_budget", "steps": objective.shop_rule_steps})
            with self._lock:
                objective.shop_rule_stop = objective.shop_rule_stop or "economy_shop_rule_budget"
            self._economy_shop_stop_finish(goal, objective)
        return 1

    def _economy_shop_stop_finish(self, goal, objective) -> int:
        """FIXB24 D-B21-4 (live V2B21SELL-a / -b: `economy_shop_rule stop goal_item_not_published` / `press_withheld:X`,
        the ShopMenu left open both times and closed by the operator): a stopped shop stage closes the ShopMenu (the same
        economy close as the verified finish: one B) and ends the goal partial `shop_rule_stop:<reason>`; the menu is never
        handed back to the model open."""
        from types import SimpleNamespace
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        try:
            with self._lock:
                if (self._active is not goal or goal.progress_marker == "terminal" or objective.finish_recorded
                        or economy.objective_verdict(objective)[0] == "achieved"):
                    return 0
            close = self._economy_close(goal, objective)
            with self._lock:
                if self._active is not goal or goal.progress_marker == "terminal" or objective.finish_recorded:
                    return 1
                objective.finish_recorded = True
                verdict = FinishEvidenceView("partial", "shop_rule_stop:" + str(objective.shop_rule_stop)[:60],
                                             dict(economy.objective_summary(objective) or {}, origin="economy_rule",
                                                  close=close))
                goal.last_finish = verdict
                goal.progress_marker = "terminal"
                goal.revision += 1
                self._pending_continuation = None
                self._pending_event_reason = None
                self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                               "verdict": asdict(verdict), "origin": "economy_rule"})
            result = OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=False, reason="finish",
                                     speech=StatusSpeech("Barangnya tidak bisa kuproses di toko ini, jadi menunya kututup."),
                                     finish_evidence=verdict)
            if self._on_generation_result is not None:
                self._on_generation_result(SimpleNamespace(reason="economy_rule_finish", generation_id=None), result)
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "economy_rule_error", "error": type(exc).__name__})
            return 0

    # ------------------------------------------------------------------ economy_rule worker finish (D-B3-2)
    def economy_finish_due(self) -> bool:
        """Caller holds the lock (runner idle check): a verified economy objective awaits the worker's economy_rule."""
        try:
            goal = self._active
            objective = self._economy_objective(goal)
            if (objective is not None and objective.kind == economy.KIND_SHIP
                    and any(e.get("selected_by_model") is True and e.get("her_menu_pick") is not False for e in objective.events)
                    and getattr(self._state.latest(self._binding_view()[1]), "active_menu", None) is not None):
                return False  # ARTI may choose another stack or close; never auto-close between her choices
            if objective is not None and getattr(objective, "close_deferred", None) and self._economy_cursor_held():
                return False  # PM-1 fix14b: her cursor item is still hers to place; the finish waits for the readback
            return (objective is not None and goal.progress_marker != "terminal" and not objective.finish_recorded
                    and not self._gather_mutation_unresolved and economy.objective_verdict(objective)[0] == "achieved")
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def process_economy_finish(self) -> int:
        """Worker hook (live D-B3-2, V2B3G2take-a: take_out verified 484 wood, then 10 inspects and the chest left open):
        once the objective's ledger / transfer events are verified achieved, the runtime closes the menu it was done in
        (chest: the ordinary container_exit of THAT chest; shop / crafting: one ui_press B) and records the goal finish
        (economy_rule) itself, like store_rule / fishing_rule. The close is best effort and journaled; the finish rests
        on the verified events only. Waits while anything is in flight; never races a model decision."""
        from types import SimpleNamespace
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        try:
            with self._lock:
                goal = self._active
                if (not self.economy_finish_due() or self._action_busy or self._inflight_generation is not None
                        or getattr(self, "_results_pending", 0) or self._trial_result_pending()):
                    return 0
                objective = self._economy_objective(goal)
            close = self._economy_close(goal, objective)
            with self._lock:
                if self._active is not goal or goal.progress_marker == "terminal" or objective.finish_recorded:
                    return 0
                base = self._economy_finish_verdict(goal)
                if base is None or base.status != "achieved":
                    return 0
                if close in _CLOSE_LEFT_OPEN:
                    # FIXB3 review MAJOR: the crafted item is still on the cursor and B is refused, so this is not a
                    # clean finish yet. PM-1 fix14b (a19 d2:J62-111): recording it as a terminal uncertain verdict here
                    # outlived her own settle (J96) and close (J108) and denied a verified +2 Chest craft. The finish
                    # now waits: placing the item is her choice; once the cursor reads back empty the verified events
                    # are credited from that readback (economy_finish_due re-arms; nothing is auto-placed).
                    objective.finish_recorded = False
                    if getattr(objective, "close_deferred", None) != close:
                        self._journal({"ev": "economy_finish_deferred", "goal_id": goal.goal_id,
                                       "kind": objective.kind, "close": close})
                    objective.close_deferred = close
                    return 0
                observed = dict(base.observed or {}, close=close)
                if getattr(objective, "close_deferred", None):
                    observed["cursor_settled_before_close"] = objective.close_deferred
                verdict = FinishEvidenceView("achieved", base.reason, observed)
                goal.last_finish = verdict
                goal.progress_marker = "terminal"
                goal.revision += 1
                self._pending_continuation = None
                self._pending_event_reason = None
                self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                               "verdict": asdict(verdict), "origin": "economy_rule"})
            clean = verdict.status == "achieved"
            result = OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=clean, reason="finish",
                                     speech=StatusSpeech("Transaksinya sudah terverifikasi dari catatan mod." if clean
                                     else "Transaksinya terjadi, tapi menunya masih terbuka dengan barang di kursor."),
                                     finish_evidence=verdict)
            if self._on_generation_result is not None:
                self._on_generation_result(SimpleNamespace(reason="economy_rule_finish", generation_id=None), result)
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "economy_rule_error", "error": type(exc).__name__})
            return 0

    def _economy_cursor_held(self) -> bool:
        """PM-1 fix14b: the native cursor readback of the current menu; unreadable counts as still held."""
        try:
            view = self._ui_pad_view(self._binding_view()[1])
        except Exception:  # noqa: BLE001 - an idle check never raises
            return True
        if view is None:
            motor = self._state.latest(self._binding_view()[1])
            return getattr(motor, "active_menu", None) is not None  # menu open but no current read: wait
        return view.held_item is not None or bool((view.cursor_settle or {}).get("cursor"))

    TAKE_CLOSE_ATTEMPTS_MAX = 2  # FIXB26 review: close faults before the bag_full finish records close=error

    def economy_take_blocked_due(self) -> bool:
        """Caller holds the lock (runner idle check): FIXB26 D-B23-3 a take objective blocked `bag_full` (nothing moved yet)
        awaits the worker's close + partial finish."""
        try:
            goal = self._active
            objective = self._economy_objective(goal)
            blocker = getattr(objective, "take_blocker", None) if objective is not None else None
            return (objective is not None and objective.kind == economy.KIND_TAKE and goal.progress_marker != "terminal"
                    and not objective.finish_recorded and objective.done == 0 and not objective.uncertain
                    and isinstance(blocker, dict) and blocker.get("reason") == "bag_full"
                    and not self._gather_mutation_unresolved)
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def process_economy_take_blocked(self) -> int:
        """Worker hook, FIXB26 D-B23-3 (live V2B23TAKE-b: `economy_take_bag_full room 0 stack 765`, then 12 model inspects
        and the chest left open): the take cannot move the whole stack (the mod moves whole stacks only), so the runtime
        closes the chest it opened (the ordinary container_exit of THAT chest, like the verified take's close) and ends the
        goal partial `bag_full` (the blocker journaled). Waits while anything is in flight; never races a model decision."""
        from types import SimpleNamespace
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech
        try:
            with self._lock:
                goal = self._active
                if (not self.economy_take_blocked_due() or self._action_busy or self._inflight_generation is not None
                        or getattr(self, "_results_pending", 0) or self._trial_result_pending()):
                    return 0
                objective = self._economy_objective(goal)
                blocker = dict(objective.take_blocker)
                _, pid = self._binding_view()
            try:
                ctx = self._water_context(pid, container=True)
                view = self._container_view(ctx) if ctx is not None else None
                if view is None:
                    status = "no_menu"
                else:
                    goal.store_container = (view.get("container_token"), view.get("source_fact_id"))
                    record = self._store_close(goal, origin="economy_rule", failsafe=True)
                    status = (record or {}).get("status") or "not_offered"
            except Exception as exc:  # noqa: BLE001
                # FIXB26 review: a faulting close never loops: retried once, then the goal ends partial close=error
                goal.take_close_errors = getattr(goal, "take_close_errors", 0) + 1
                self._journal({"ev": "economy_close_rule", "goal_id": goal.goal_id, "kind": objective.kind,
                               "status": "error", "error": type(exc).__name__, "attempt": goal.take_close_errors,
                               "reason": "bag_full"})
                if goal.take_close_errors < self.TAKE_CLOSE_ATTEMPTS_MAX:
                    return 0
                status = "error"
            self._journal({"ev": "economy_close_rule", "goal_id": goal.goal_id, "kind": objective.kind, "status": status,
                           "reason": "bag_full"})
            with self._lock:
                if self._active is not goal or goal.progress_marker == "terminal" or objective.finish_recorded:
                    return 0
                objective.finish_recorded = True
                verdict = FinishEvidenceView("partial", "bag_full", dict(economy.objective_summary(objective) or {},
                                                                         origin="economy_rule", blocked=blocker, close=status))
                goal.last_finish = verdict
                goal.progress_marker = "terminal"
                goal.revision += 1
                self._pending_continuation = None
                self._pending_event_reason = None
                self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                               "verdict": asdict(verdict), "origin": "economy_rule"})
            result = OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=False, reason="finish",
                                     speech=StatusSpeech("Tasku penuh, jadi tumpukannya tidak bisa kuambil; peti sudah kututup."),
                                     finish_evidence=verdict)
            if self._on_generation_result is not None:
                self._on_generation_result(SimpleNamespace(reason="economy_rule_finish", generation_id=None), result)
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "economy_rule_error", "error": type(exc).__name__})
            return 0

    def _economy_close(self, goal, objective) -> str:
        """Close the menu the verified effect happened in (once per objective). Returns a short journaled status."""
        status = "no_menu"
        _, pid = self._binding_view()
        last = next((e for e in reversed(objective.events) if e.get("mode") == "take_out"), None)
        if objective.kind == economy.KIND_TAKE and last is not None:
            ctx = self._water_context(pid, container=True)
            view = self._container_view(ctx) if ctx is not None else None
            if view is None:
                status = "no_menu"
            elif (view.get("container_token"), view.get("source_fact_id")) != (last.get("container_token"),
                                                                              last.get("source_fact_id")):
                status = "other_container"
            else:
                # The v1 ordinary chest close of THIS chest (its fail-safe pin is the take's container).
                goal.store_container = (last.get("container_token"), last.get("source_fact_id"))
                record = self._store_close(goal, origin="economy_rule", failsafe=True)
                status = (record or {}).get("status") or "not_offered"
        else:
            view = self._ui_pad_view(pid)
            if view is not None and view.context == "shop":
                view = self._economy_close_ready_view(pid, view)
            # FIXB7 D-B5b-3: the shipping bin (ItemGrabMenu "item_grab") of a ship objective closes with the mod's B.
            ship_bin = objective.kind == economy.KIND_SHIP and view is not None and view.context == "item_grab"
            generic_receiver = (view is not None and view.generic
                                and view.receiver_token == objective.receiver_token)
            if view is not None and not view.hud and (view.context in ("shop", "crafting", "game_menu") or ship_bin or generic_receiver):
                btn = next((b for b in ("B", "Start") if view.press_allowed(b)), None)
                if not view.available:
                    status = "menu_not_ready"
                elif view.held_item is not None:
                    status = "held_item"  # B is refused with an item on the cursor; the model places it first
                elif btn is None:
                    status = "close_not_offered"
                else:
                    with self._lock:
                        was_busy, self._action_busy = self._action_busy, True
                    try:
                        run, _ = self._run_ui_pad("ui_press", view.press_args(btn), origin="economy_rule")
                    finally:
                        with self._lock:
                            self._action_busy = was_busy
                    status = "closed" if run.verified and run.effect == "closed" else (run.reason or "not_closed")[:40]
        self._journal({"ev": "economy_close_rule", "goal_id": goal.goal_id, "kind": objective.kind, "status": status})
        return status

    ECONOMY_CLOSE_WAIT_S = 3.0

    def _economy_close_ready_view(self, pid, view):
        """D-B6-5 (live V2B6bG2sell-a: `economy_close_rule close_not_offered` right after `sold`, the ShopMenu left open;
        the buy closed fine): ShopMenu.readyToClose is heldItem == null AND no running animations, and a sale starts the
        coin animation, so B is refused for a moment after a sell. Re-read the menu (bounded) until B is offered."""
        deadline = game_now(self) + self.ECONOMY_CLOSE_WAIT_S
        while (view is not None and view.context == "shop" and not view.press_allowed("B")
               and game_now(self) < deadline and not self._worker_stop.is_set()):
            W.call('poll', 109, time.sleep, 0.1)
            view = self._ui_pad_view(pid)
        return view

    # ------------------------------------------------------------------ finish (economy_rule)
    def _economy_finish_verdict(self, goal):
        objective = self._economy_objective(goal)
        if objective is None:
            return None
        from arti_stardew_runtime import FinishEvidenceView
        status, reason = economy.objective_verdict(objective)
        if status == "achieved":
            objective.finish_recorded = True
        observed = dict(economy.objective_summary(objective) or {}, origin="economy_rule",
                        events=list(objective.events)[-8:])
        return FinishEvidenceView(status, reason, observed)
