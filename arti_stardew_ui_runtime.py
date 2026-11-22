"""Primitives v2 WS-G1 runtime side: ui_inspect / ui_nav / ui_press offering, leased execution, readback, journal,
and the G1 menu-objective finish rule ("[recorded remark omitted]", "[recorded remark omitted]", "[recorded remark omitted]").

Kept out of ``arti_stardew_runtime.py`` (several workstreams edit it): ``StardewGoalRuntime`` inherits
:class:`UiPadRuntimeMixin` and calls a handful of hooks. Every physical step reuses WS2's leased path
(``_run_leased_once``: ACQUIRE -> one guarded input_once -> RELEASE, session limit + trial input budget, strictly newer
ui.context readback). Request-local handles expand to the exact offered ``ui_pad`` pins; no target is invented.

Offering (PLAN 1.1 rule 8, 5.3): ``ui_nav`` / ``ui_press`` are offered only when a menu is up, the mod says the
block is ready, and NO routine owns that menu: never during the day-start rule, never for LevelUp / Shipping (runtime
owned), dialogue / question / letter only where WS2's own model offer would stand (the Sleep question only under
the owner's sleep objective), a chest only when no store chain / container offer owns it, never a latched receiver.
The model sees labels, values and neighbour ids, never coordinates (``bounds`` stay runtime-only).

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G1 live witness.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

import time
from arti_stardew_pause import wait_resume
from collections.abc import Mapping
from dataclasses import asdict, replace

import arti_stardew_command as command
import arti_stardew_ui_pad as ui_pad
from arti_stardew_reading_dwell import DWELL_CONTEXTS
from arti_stardew_ui_key_runtime import _DWELL_UI_PAD_ORIGINS, LeasedRun
from arti_stardew_event_control import EventActionsRuntimeMixin

UI_PAD_OPS = ("ui_inspect", "ui_nav", "ui_press", "pad_button")
_PAD_BUTTON_MENUS = {"Start": "GameMenu", "Y": "GameMenu", "Select": "QuestLog"}
_UI_PAD_IDENTITY_FIELDS = ("runtime_instance_id", "command_session_id", "observation_session_id",
                           "context_generation", "player_id", "screen_id", "location")


class UiPadRuntimeMixin(EventActionsRuntimeMixin):
    """Mixed into StardewGoalRuntime; uses its lock, stores, leased helpers and journal."""

    def _init_ui_pad_runtime(self) -> None:
        self._ui_pad_uncertain: dict[str, str] = {}
        self._ui_pad_last_inspect: str | None = None

    # ------------------------------------------------------------------ reads
    def _ui_pad_view(self, player_id: int | None) -> ui_pad.UiPadView | None:
        rec, age = self._ui_key_record(player_id)
        if rec is None:
            return None
        view = ui_pad.baca_ui_pad(rec.facts, age_s=age, authoritative=rec.authoritative, complete=rec.is_complete)
        if view is not None and (getattr(self, "_config", None) or {}).get("live7_fact_line_enabled") is True:
            from dataclasses import replace
            view = replace(view, named_item_args=True)
        return view

    def _ui_pad_slot_free(self, ctx, primitive: str) -> bool:
        enabled = "pad_button_enabled" if primitive == command.PRIMITIVE_PAD_BUTTON else "ui_pad_enabled"
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            self._ui_key_session(folder, ctx, enabled_key=enabled, primitive=primitive, acquired=False)
            return True
        except (OSError, ValueError, TypeError, command.KesalahanPerintah):
            return False

    # ------------------------------------------------------------------ offering (rule 8)
    def _ui_pad_owner_reason(self, goal, view: ui_pad.UiPadView, *, ui_key_offer: dict | None,
                             store_menu: bool) -> str | None:
        """Why a runtime routine owns this menu (no model offer), or None."""
        if self._day_start_active:
            return "day_start_rule"
        # Primitives v2 WS-G3: naming / number / ready_check are runtime-owned (D3 / D4); the event context only
        # for the owner's event-question objective (arti_stardew_text_event.model_offer_allowed).
        session = getattr(self, "_skill_session", None)
        run = getattr(session, "current", None)
        purchase_skill = (goal is not None and run is not None and run.goal_id == goal.goal_id
                          and session.status == "active" and run.status == "running"
                          and run.call.name == "beli_hewan" and bool(run.call.arg) and not run.state.get("wave1_driver"))
        if view.menu_type == "PurchaseAnimalsMenu":
            return None if purchase_skill else "animal_purchase_requires_owner_request"
        g3_owner = self._text_event_owner_reason(goal, view)
        if g3_owner is not None:
            return g3_owner
        if view.context in ui_pad.RUNTIME_OWNED_CONTEXTS:
            return "runtime_owned:" + view.context
        if view.context in ui_pad.DIALOGUE_CONTEXTS or view.context == "letter":
            # Exactly where WS2 offers its own model ui_key (talk / eat / bed rules, Sleep only under the
            # owner's sleep objective, latches): never wider than v1.
            if ui_key_offer is None or ui_key_offer.get("receiver_token") != view.receiver_token:
                return "ui_key_rules:" + view.context
        if view.context == "item_grab":
            # Review major 1: a chest is v1's (container_transfer / container_exit need an UNSNAPPED ItemGrabMenu,
            # and a snap is never cleared): no ui_nav / ui_press offer at all, store chain or not.
            return "store_chain" if store_menu else "item_grab_v1_container_ops"
        if view.receiver_token in self._ui_pad_uncertain or view.receiver_token in self._ui_key_uncertain:
            return "uncertain_latched"
        return None

    def _ui_pad_offer(self, goal, binding, player_id: int | None, *, ui_key_offer: dict | None,
                      store_menu: bool) -> dict | None:
        """Snapshot entry (ui_menu view + catalog) or None. Also notes a G1 objective's menu seen open."""
        if binding.status != "bound" or player_id is None:
            return None
        view = self._ui_pad_view(player_id)
        if view is not None and view.hud:
            return None  # HUD remains explicitly inspectable/clickable; it is not a modal skill interruption.
        ctx = self._ui_key_context(player_id)
        if view is None or ctx is None or ctx.context_generation != view.context_generation:
            return None
        objective = getattr(goal, "menu_objective", None) if goal is not None else None
        if ui_pad.note_view(objective, view):
            self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "menu_objective": asdict(objective),
                           "origin": "ui_pad_view"})
        self._extras_note_menu_view(goal, view)  # Primitives v2 WS-G6 (bundle / museum / elevator menu seen open)
        if not view.available:
            return None
        owner = self._ui_pad_owner_reason(goal, view, ui_key_offer=ui_key_offer, store_menu=store_menu)
        offer = ui_pad.offer_payload(view)
        if owner is None and hasattr(self, "_economy_filter_offer"):
            # WS-G2: buy / sell / craft presses only for the owner's economy objective and both item policies.
            offer = self._economy_filter_offer(goal, view, offer)
        if owner is not None or not (offer["nav"] or offer["press"] or offer["tabs"]):
            return None
        if not self._ui_pad_slot_free(ctx, command.PRIMITIVE_UI_PRESS):
            return None
        # Runtime-only original-offer fence. Tokens can recur after a native
        # restart; they cannot authorize rebinding an outstanding model request.
        offer["identity"] = {key: getattr(ctx, key) for key in _UI_PAD_IDENTITY_FIELDS}
        if view.target_token != self._ui_pad_last_inspect:
            self._ui_pad_last_inspect = view.target_token
            self._journal({"ev": "ui_inspect", "origin": "offer", "receiver_token": view.receiver_token,
                           "ready_check": view.ready_check,
                           "menu_type": view.menu_type, "context": view.context, "page_type": view.page_type,
                           "snapped_id": view.snapped_id, "components": len(view.components),
                           "target_token": view.target_token, "page_index": view.page_index,
                           "goal_component_ids": (offer.get("view") or {}).get("goal_component_ids", []),
                           "recipe_ids": [r["id"] for r in (view.economy or {}).get("recipes", ())],
                           "recipes": list((view.economy or {}).get("recipes", ())),
                           **({"identity_probe": view.identity_probe,
                               "ui_state": (view.ui_safety or {}).get("state")} if view.identity_probe else {})})
        return offer

    def _ui_pad_flag_configured(self) -> bool:
        """The mod's UiPadMenusEnabled flag: session.json carries ui_pad_enabled only when it is on (review minor 3)."""
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            from arti_stardew_runtime import _SESSION_READ_RETRY_SECONDS
            return "ui_pad_enabled" in self._commands.baca_sesi(folder, retry_detik=_SESSION_READ_RETRY_SECONDS).mentah
        except Exception:  # noqa: BLE001 - unreadable session = flag unknown = no menu objective
            return False

    def _pad_button_offer(self, goal, binding, player_id: int | None) -> dict | None:
        """WS-G0 pad_button for a G1 menu objective (Start / Select), a G2 craft objective (Y: GameMenu crafting tab,
        D-B2-1) or the G6 bundle objective; RB for pegang_slot and, fix74a, when a tool is only in another bag row (verified
        by the bag fingerprint, _run_leased_once). Only with no menu open."""
        objective = getattr(goal, "menu_objective", None) if goal is not None else None
        button = ui_pad.OPEN_BUTTON.get(objective.kind) if objective is not None and not objective.opened else None
        if button is None and objective is None and hasattr(self, "_economy_pad_button"):
            button = self._economy_pad_button(goal)  # D-B2-1: a craft objective opens the crafting tab (Y)
        if button is None and objective is None:
            button = self._extras_pad_button(goal)  # Primitives v2 WS-G6: bundle -> GameMenu (Junimo note icon)
        run = getattr(getattr(self, "_skill_session", None), "current", None)
        if run is not None and run.call.name in ("pegang_slot", "inventori", "cek_jurnal"):
            button = {"pegang_slot": "RB", "inventori": "Start", "cek_jurnal": "Select"}[run.call.name]
        festival_leave = False
        if button is None and run is not None and run.call.name == "pergi_ke" and player_id is not None:
            # fix63A (ADR 94): her own trip out of a festival asks the game's leave question; never offered otherwise.
            from arti_stardew_event_control import festival_leave_offered
            leave_rec, _ = self._ui_key_record(player_id)
            festival_leave = leave_rec is not None and festival_leave_offered(leave_rec.facts)
            button = "Select" if festival_leave else None
        other_row_tools = ()
        if button is None and run is not None and player_id is not None:
            # fix74a: a hand step needs a tool that only another row holds (witness d2 J100 on: exit_not_offered 34,
            # plain_hand_not_offered 6). The step decides; the offer only says the swap reaches it.
            other_row_tools = self._tools_in_other_row(player_id)
            button = "RB" if other_row_tools else None
        if button is None or binding.status != "bound" or player_id is None:
            return None
        rec, _ = self._ui_key_record(player_id)
        ctx = self._ui_key_context(player_id)
        if rec is None or ctx is None or isinstance(rec.facts.get("interactive"), Mapping):
            return None
        # S27 opens the bundle UI only after travel. During a walk ACQUIRE makes
        # _ui_pad_slot_free false: an incidental Start offer would disappear and
        # invalidate that very walk at the publication fence (attempts27a/b).
        run = getattr(getattr(self, "_skill_session", None), "current", None)
        if (goal is not None and run is not None and run.call.name == "donasi" and run.goal_id == goal.goal_id
                and ctx.location != "CommunityCenter"):
            return None
        pad = rec.facts.get("pad")
        cell = pad.get("buttons", {}).get(button) if isinstance(pad, Mapping) and pad.get("enabled") is True else None
        if not isinstance(cell, Mapping) or cell.get("family") != command.PRIMITIVE_PAD_BUTTON:
            return None
        if run is not None and run.call.name == "cek_jurnal" and not str(cell.get("detail", "")).startswith("QuestLog"):
            return None  # Select may now mean event skip or festival leave; it is no longer a journal offer.
        # fix74a: the row swap opens no menu and is revalidated when it is sent. Her own ACQUIRE makes the slot not free,
        # so an offer tied to it vanished under the lease and the fence discarded her next act (witness d2 J110-J112).
        if button != "RB" and not self._ui_pad_slot_free(ctx, command.PRIMITIVE_PAD_BUTTON):
            return None
        if festival_leave:
            return {"button": button, "opens": "festival leave question", "festival_leave": True}
        if other_row_tools:
            return {"button": button, "opens": "toolbar row", "other_row_tools": other_row_tools}
        return {"button": button, "opens": _PAD_BUTTON_MENUS.get(button, "toolbar row")}

    def _tools_in_other_row(self, player_id: int) -> list:
        """The tools the bag holds outside the toolbar row (a backpack upgrade makes a second row), sorted."""
        inventory = self._obs.get(player_id, "progress.inventory")
        facts = inventory.facts if inventory is not None and isinstance(inventory.facts, Mapping) else {}
        return sorted({f["qualified_item_id"] for k, f in facts.items() if str(k).startswith("slot:")
                       and isinstance(f, Mapping) and f.get("occupied") is True and type(f.get("slot")) is int
                       and f["slot"] >= 12 and str(f.get("qualified_item_id")).startswith("(T)")})

    @staticmethod
    def _pad_button_catalog(offer: dict) -> dict:
        return {"target_ref": None, "args": {"button": offer["button"]},
                "example": {"kind": "act", "operation": "pad_button", "target_ref": None,
                            "args": {"button": offer["button"]}, "evidence_refs": []},
                "result_scope": f"opens the {offer['opens']} (one direct call, no pad press)"}

    # ------------------------------------------------------------------ model decision path
    def _ui_pad_model_reason(self, request, operation: str, args) -> str | None:
        snap = request.snapshot
        if not isinstance(args, dict):
            return "ui_pad_args_invalid"
        if operation == "pad_button":
            offer = getattr(snap, "pad_button_offer", None)
            if offer is None:
                return "pad_button_not_offered"
            return None if args == {"button": offer["button"]} else "pad_button_not_offered"
        offer = getattr(snap, "ui_pad_target", None)
        if offer is None:
            return "ui_pad_not_offered"
        if self._day_start_active:
            return "day_start_active"
        if operation == "ui_inspect":
            return None if args == {} else "ui_pad_args_invalid"
        try:
            args = ui_pad.resolve_model_args(offer, operation, args)
        except ValueError as exc:
            return str(exc)
        pins = offer["pins"]
        if any(args.get(k) != pins[k] for k in command.UI_PAD_BASE_ARGS):
            return "ui_pad_target_not_offered"
        if pins["receiver_token"] in self._ui_pad_uncertain:
            return "ui_pad_uncertain_latched"
        try:
            if operation == "ui_nav":
                if "destination_id" in args:
                    if set(args) != set(command.UI_PAD_BASE_ARGS) | {"destination_id"}:
                        return "ui_pad_args_invalid"
                    return None if ui_pad.published_route(offer, args["destination_id"]) else "ui_nav_route_not_offered"
                command.validate_ui_nav_args(args)
                navigation = offer.get("view", {}).get("navigation", {})
                if navigation.get(args["dir"], {}).get("refused"):
                    return "ui_nav_denylisted_target"
                return None if offer["nav"] else "ui_nav_not_offered"
            command.validate_ui_press_args(args)
        except command.KesalahanPerintah:
            return "ui_pad_args_invalid"
        if args["snapped_id"] != offer["snapped_id"]:
            return "ui_pad_target_not_offered"
        from types import SimpleNamespace
        g3_reason = self._text_event_model_args_reason(
            SimpleNamespace(context=pins["context"], g3=(offer.get("view") or {}).get("g3")), args)
        if g3_reason:
            return g3_reason
        if args["btn"] == "tab":
            return None if args["tab"] in offer["tabs"] else "ui_press_tab_not_offered"
        if args["btn"] not in offer["press"]:
            return "ui_press_button_not_offered"
        if pins["menu_type"] == "PurchaseAnimalsMenu":
            from arti_stardew_animal_purchase import payload_reason
            run = getattr(getattr(self, "_skill_session", None), "current", None)
            reason = payload_reason(run, offer, args)
            if reason:
                return reason
        if "choice" in args and pins["context"] == "question":
            key = self._ui_key_view(self._binding_view()[1])
            if key is None or key.question_key not in ("TV", "Blacksmith", "Marnie") or key.receiver_token != pins["receiver_token"]:
                return "ui_press_choice_not_offered"
            options = (offer.get("view") or {}).get("options") or ()
            allowed = ("Upgrade",) if key.question_key == "Blacksmith" else ("Purchase",) if key.question_key == "Marnie" else ("Weather", "Fortune")
            if not any(str(o.get("index")) == args["choice"] and o.get("response_key") in allowed for o in options):
                return "ui_press_choice_not_offered"
        if hasattr(self, "_economy_press_reason"):
            return self._economy_press_reason(offer, args)  # WS-G2: exactly the offered qty / stack payload
        return None

    def _ui_pad_request_context(self, request):
        offer = request.snapshot.ui_pad_target
        identity = offer.get("identity") if offer else None
        binding, pid = self._binding_view()
        ctx = self._ui_key_context(pid)
        if (not isinstance(identity, Mapping) or binding.status != "bound" or ctx is None
                or pid != identity.get("player_id")
                or any(getattr(ctx, key) != identity.get(key) for key in _UI_PAD_IDENTITY_FIELDS)):
            raise ValueError("ui_pad_request_identity_changed")
        return ctx

    def _ui_pad_precheck_decision(self, request, operation: str, args) -> None:
        """Continuation precheck (nothing written): the offered pins are still the current published ones."""
        reason = self._ui_pad_model_reason(request, operation, args)
        if reason:
            raise ValueError(reason)
        if operation in ("ui_inspect", "pad_button"):
            return
        self._ui_pad_request_context(request)
        args = ui_pad.resolve_model_args(request.snapshot.ui_pad_target, operation, args)
        _, pid = self._binding_view()
        view = self._ui_pad_view(pid)
        if (view is None or not view.available or view.receiver_token != args["receiver_token"]
                or view.target_token != args["target_token"]):
            raise ValueError("ui_pad_target_changed")

    @W.span('arming', 121, runtime=True)
    def _run_ui_pad(self, primitive: str, args: dict, *, origin: str,
                    revalidate_extra=None) -> tuple[LeasedRun, ui_pad.UiPadView | None]:
        """One ui_nav / ui_press / pad_button through the leased path (journal ev = the primitive)."""
        wait_resume(self)
        with self._lock:
            animal_goal = getattr(self, "_active", None)  # late stage result belongs to this goal
        binding, pid = self._binding_view()
        if primitive == command.PRIMITIVE_UI_PRESS and origin in _DWELL_UI_PAD_ORIGINS:
            current = self._ui_pad_view(pid)
            if current is not None and current.context in DWELL_CONTEXTS and current.receiver_token == args.get("receiver_token"):
                self._reading_dwell_wait(pid, kind=current.context, origin=origin)
        ctx = self._ui_key_context(pid)
        before = self._ui_pad_view(pid) if primitive != command.PRIMITIVE_PAD_BUTTON else None
        ui_session = getattr(self, "_skill_session", None)
        ui_run = getattr(ui_session, "current", None)
        from arti_stardew_ui_effects import observe, publish
        from arti_stardew_generic_ui import latch_close_due
        observe(self, ui_session, before)
        self._journal({"ev": primitive, "phase": "request", "origin": origin, "args": dict(args),
                       "context": before.context if before else None,
                       "snapped_label": (before.snapped or {}).get("label") if before else None})
        receiver = args.get("receiver_token")
        started_unix = time.time()  # W1b B5: before any press could be sent
        if ctx is None:
            run = LeasedRun("not_taken", "ui_context_unavailable")
        elif receiver is not None and receiver in self._ui_pad_uncertain and not (
                args.get("btn") in ("B", "Start") and latch_close_due(self, receiver)):
            run = LeasedRun("not_taken", "ui_pad_uncertain_latched")
        else:
            def revalidate():
                if primitive == command.PRIMITIVE_PAD_BUTTON:
                    rec, _ = self._ui_key_record(pid)
                    if rec is None or isinstance(rec.facts.get("interactive"), Mapping):
                        raise ValueError("pad_button_menu_already_open")
                    return
                view = self._ui_pad_view(pid)
                if (view is None or not view.available or view.receiver_token != args["receiver_token"]
                        or view.target_token != args["target_token"] or view.context_generation != ctx.context_generation
                        or (primitive == command.PRIMITIVE_UI_NAV and not view.nav_allowed)
                        or (primitive == command.PRIMITIVE_UI_PRESS and args["btn"] != "tab"
                            and not view.press_allowed(args["btn"]))
                        or (primitive == command.PRIMITIVE_UI_PRESS and args["btn"] == "tab"
                            and args.get("tab") not in view.tabs)):
                    raise ValueError("ui_pad_target_changed")
                if revalidate_extra is not None:
                    revalidate_extra()
            try:
                run = self._run_leased_once(primitive, args, ctx, origin=primitive.replace("_", "") + "-" + origin,
                                            revalidate=revalidate)
            except Exception as exc:  # noqa: BLE001 — never let a ui step escape as an exception
                run = LeasedRun("not_taken", type(exc).__name__)
        self._journal({"ev": primitive, "phase": "result", "origin": origin, "request_id": run.request_id,
                       "status": run.lifecycle, "outcome": run.outcome, "effect": run.effect,
                       "result_receiver_token": run.result_receiver_token, "result_menu_type": run.result_menu_type,
                       "verified": run.verified, "cleanup": run.cleanup, "reason": run.reason, "readback": run.readback,
                       # W1b B5 (ADR 250): the press's wall window, for the bench's focus bracket
                       "started_unix": started_unix, "ended_unix": time.time()})
        from scripts.live7.native_facts import cursor_completion, ground_receipts
        destination = cursor_completion(before, args, run)
        if destination is not None:
            self._journal(destination)
        for ground in ground_receipts(before, args, run) if primitive == command.PRIMITIVE_UI_PRESS else ():
            self._journal(ground)  # fix59A (ADR 86): trash / reward-menu spill, world-effects memory
        if run.uncertain and receiver is not None:
            with self._lock:
                self._ui_pad_uncertain[receiver] = run.reason
                self._journal({"ev": "ui_pad_uncertain", "receiver_token": receiver, "reason": run.reason})
                if run.hard_uncertain:
                    from arti_stardew_runtime import PAUSED_RECOVERY
                    self._status = PAUSED_RECOVERY
                    self._revoke_play()
                    self._pending_event_reason = None
        with self._lock:
            if getattr(animal_goal, "animal_executor", None):
                from arti_stardew_runtime import ActionEvidenceView
                self._animal_record_action(animal_goal, ActionEvidenceView(run.lifecycle, run.reason, run.request_id,
                    run.outcome, world_effect_status="uncertain" if run.uncertain else "zero_effect",
                    cleanup_confirmed=run.cleanup))
            current = getattr(getattr(self, "_skill_session", None), "current", None)
            if (primitive == command.PRIMITIVE_PAD_BUTTON
                    and (args.get("button"), getattr(getattr(current, "call", None), "name", None))
                    in (("Start", "inventori"), ("Select", "cek_jurnal"), ("Y", "buat"))  # ADR 178: visible journal
                    and current.goal_id == getattr(animal_goal, "goal_id", None) and run.verified and run.cleanup):
                current.state["generic_menu_opened"] = True
            publish(self, ui_session, before, run, (primitive, args), goal=animal_goal, current=ui_run)
        return run, before

    @W.span('arming', 122, runtime=True)
    def _run_ui_pad_route(self, request, goal, args: dict, *, origin: str):
        """One model decision, only its published route. Every press has its own
        lease, budget admission, guarded write, newer readback and clean release.
        Never replan an edge or retry an input after a failure/divergence.
        """
        offer = request.snapshot.ui_pad_target
        route = ui_pad.published_route(offer, args.get("destination_id"))
        _, pid = self._binding_view()
        initial = self._ui_pad_view(pid)
        trace, attempts, last = [], [], None

        def abort(reason):
            self._journal({"ev": "ui_nav_route", "status": "aborted", "reason": reason, "steps": trace})
            if last is None:
                return LeasedRun("not_taken", reason), initial, trace
            # Every attempted lease counts, even if its budget gate refused the
            # input write. Never hide a later cleanup fault behind earlier success.
            cleanup = all(run.cleanup for run in attempts)
            uncertain = not cleanup or any(run.uncertain for run in attempts)
            return replace(last, lifecycle="uncertain" if uncertain else last.lifecycle,
                           applied=last.applied if uncertain else False, cleanup=cleanup,
                           submitted=any(run.submitted for run in attempts),
                           verified=False, reason=reason), initial, trace

        try:
            self._ui_pad_request_context(request)
        except ValueError as exc:
            return abort(str(exc))
        if (not route or initial is None or initial.pins() != offer["pins"]
                or initial.snapped_id != offer["snapped_id"]):
            return abort("ui_pad_target_changed")
        initial_nodes = {c["id"]: c for c in initial.components}
        route_ids = {s[k] for s in route for k in ("from", "to")}

        def route_view(expected_snap):
            if (not self._request_current(request) or self._active is not goal
                    or self._action_cancel.is_set() or self._worker_stop.is_set()):
                raise ValueError("action_cancelled")
            ctx = self._ui_pad_request_context(request)
            view = self._ui_pad_view(ctx.player_id)
            rows = {c["id"]: c for c in view.components} if view else {}
            if (view is None or not view.available or not view.nav_allowed
                    or view.receiver_token != initial.receiver_token or view.menu_type != initial.menu_type
                    or view.context != initial.context or view.page_type != initial.page_type
                    or view.tab != initial.tab or view.page_index != initial.page_index
                    or view.held_item != initial.held_item or view.economy != initial.economy
                    or view.bundle != initial.bundle or view.g3 != initial.g3
                    or view.snapped_id != expected_snap
                    or any(rows.get(cid) != initial_nodes.get(cid) for cid in route_ids)
                    or any(cid in view.denylisted_ids for cid in route_ids)):
                raise ValueError("ui_nav_route_diverged")
            return view

        for step in route:
            def guard():
                view = route_view(step["from"])
                row = next(c for c in view.components if c["id"] == step["from"])
                if row["nbr"][ui_pad.DIRECTIONS.index(step["dir"])] != step["to"]:
                    raise ValueError("ui_nav_route_diverged")
                return view
            try:
                with self._lock:
                    native_args = guard().nav_args(step["dir"])
            except ValueError as exc:
                return abort(str(exc))
            run, _ = self._run_ui_pad("ui_nav", native_args, origin=origin + "-route", revalidate_extra=guard)
            attempts.append(run)
            last = run
            with self._lock:
                landing = self._ui_pad_view(pid)
                trace.append(dict(step, to=landing.snapped_id if landing else None, expected_to=step["to"],
                                  request_id=run.request_id, lifecycle=run.lifecycle,
                                  submitted=run.submitted, verified=False, cleanup=run.cleanup, reason=run.reason))
            if not run.verified or not run.cleanup:
                return abort(run.reason)
            try:
                with self._lock:
                    confirmed = route_view(step["to"])
                    trace[-1].update(to=confirmed.snapped_id, verified=True)
            except ValueError as exc:
                if str(exc) == "ui_nav_route_diverged":
                    self._ui_pad_uncertain[initial.receiver_token] = str(exc)
                trace[-1]["reason"] = str(exc)
                return abort(str(exc))
        self._journal({"ev": "ui_nav_route", "status": "completed", "destination_id": args["destination_id"],
                       "steps": trace})
        return last, initial, trace

    def _execute_ui_pad_decision(self, decision, request, goal, *, origin: str = "model"):
        from arti_stardew_runtime import ActionEvidenceView, OwnerTurnResult
        operation = decision.choice.operation
        args = dict(decision.choice.args)
        with self._lock:
            reason = self._ui_pad_model_reason(request, operation, args)
            if not reason and operation in ("ui_nav", "ui_press"):
                try:
                    self._ui_pad_request_context(request)
                except ValueError as exc:
                    reason = str(exc)
        route_trace = None
        if not reason and operation in ("ui_nav", "ui_press"):
            args = ui_pad.resolve_model_args(request.snapshot.ui_pad_target, operation, args)
        if operation == "ui_inspect" and not reason:
            # Zero input, zero writes: re-read the mod's published menu (fields only, never hover).
            _, pid = self._binding_view()
            view = self._ui_pad_view(pid)
            self._journal({"ev": "ui_inspect", "origin": "model", "receiver_token": view.receiver_token if view else None,
                           "menu_type": view.menu_type if view else None, "context": view.context if view else None,
                           "snapped_id": view.snapped_id if view else None})
            evidence = ActionEvidenceView("completed" if view else "not_taken",
                                          "ui_inspect" if view else "ui_pad_view_unavailable",
                                          after={"ui_menu": view.model_view(
                                              self._economy_focus(goal) if hasattr(self, "_economy_focus") else None)}
                                          if view else None,
                                          world_effect_status="observed_ui_inspect" if view else "zero_effect",
                                          cleanup_confirmed=True)
            applied = view is not None
            run = None
        else:
            if reason:
                run, before = LeasedRun("not_taken", reason), None
            elif operation == "ui_nav" and "destination_id" in args:
                run, before, route_trace = self._run_ui_pad_route(request, goal, args, origin=origin)
            else:
                run, before = self._run_ui_pad(operation, args, origin=origin,
                    revalidate_extra=(lambda: self._ui_pad_request_context(request))
                    if operation in ("ui_nav", "ui_press") else None)
            evidence = ActionEvidenceView(
                run.lifecycle, run.reason, run.request_id, run.outcome,
                before={operation: {k: v for k, v in args.items() if k != "target_token"}},
                after=({operation: dict({"effect": run.effect, "result_receiver_token": run.result_receiver_token,
                                         "result_menu_type": run.result_menu_type},
                                        **({"economy": run.economy} if run.economy else {}))} if run.request_id else None),
                world_effect_status=(("observed_" + operation) if run.verified else "unchanged"
                                     if run.prearm_rejected or run.lifecycle == "completed" and not run.applied
                                     else "zero_effect" if not run.submitted else "uncertain"),
                cleanup_confirmed=run.cleanup)
            if route_trace is not None:
                evidence = replace(evidence, after=dict(evidence.after or {}, navigation_route=route_trace,
                    destination_reached=run.verified), world_effect_status=("observed_ui_nav_partial"
                    if not run.verified and not run.uncertain and any(s["verified"] for s in route_trace)
                    else evidence.world_effect_status))
            applied = run.verified
            self._text_event_note_model_run(goal, operation, args, before, run)  # WS-G3 (event answer by the model)
            if hasattr(self, "_economy_note_press"):
                self._economy_note_press(goal, operation, run, before)  # WS-G2 economy_rule progress (ledger)
            if run.verified:
                self._extras_note_ui_press(goal, operation, args, run, before)  # Primitives v2 WS-G6
            objective = getattr(goal, "menu_objective", None) if goal is not None else None
            if objective is not None and run.verified:
                _, pid = self._binding_view()
                changed = (ui_pad.note_close(objective, menu_type_before=before.menu_type if before else None,
                                             effect=run.effect, verified=run.verified)
                           if operation == "ui_press" else
                           ui_pad.note_view(objective, self._ui_pad_view(pid)) if operation == "pad_button" else False)
                if changed:
                    self._journal({"ev": "objective_progress", "goal_id": goal.goal_id,
                                   "menu_objective": asdict(objective), "origin": operation})
        with self._lock:
            superseded = (not self._request_current(request) or self._active is not goal or self._action_cancel.is_set())
            self._animal_record_action(goal, evidence)
            if not superseded and goal is not None:
                _, pid = self._binding_view()
                ui_session = dict(request.observation_sessions).get("ui.context")
                previous = asdict(goal.last_action) if goal.last_action is not None else None
                if (ui_pad.inventory_pickup_from_action(previous) or {}).get("observation_session_id") != ui_session:
                    previous = None
                pickup = ui_pad.inventory_pickup_evidence(
                    before if run is not None else None, self._ui_pad_view(pid), goal.text,
                    operation, args, asdict(evidence), previous)
                if pickup is not None and ui_session:
                    pickup["observation_session_id"] = ui_session
                    evidence = replace(evidence, after=dict(evidence.after or {}, inventory_pickup=pickup))
                goal.last_action = evidence
            if self._inflight_generation == request.generation_id:
                self._inflight_generation = None
            self._action_busy = False
            self._journal({"ev": "action_result", "goal_id": request.goal_id, "action": asdict(evidence)})
            if not superseded:
                self._schedule_continuation(decision, request)
        if not superseded and applied and (run is None or run.cleanup):
            self._resume_after_pause(reason="action_settled")
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=not superseded and applied,
                               discarded=superseded, reason="superseded" if superseded else
                               (run.reason if run is not None else "ui_inspect"), action_evidence=evidence)

    # ------------------------------------------------------------------ objective + finish
    def _pin_menu_objective(self, goal, objective, request) -> None:
        goal.menu_objective = objective
        self._journal({"ev": "objective_progress", "goal_id": goal.goal_id, "pinned": asdict(objective),
                       "origin": "owner_menu_objective"})

    def _ui_pad_finish_verdict(self, goal):
        """FinishEvidenceView for a G1 menu objective (verified open + verified close only), or None."""
        objective = getattr(goal, "menu_objective", None) if goal is not None else None
        if objective is None:
            return None
        from arti_stardew_runtime import FinishEvidenceView
        status, reason = ui_pad.objective_verdict(objective)
        if status == "achieved":
            objective.finish_recorded = True
        return FinishEvidenceView(status, reason, dict(ui_pad.objective_summary(objective) or {}, origin="menu_rule"))
