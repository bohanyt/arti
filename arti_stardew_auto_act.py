"""Offered-act-ignored -> act: the runtime takes an OFFERED step itself when the model stalls on inspects.

Live v2 batch 1c (receipt RECEIPT_V2_BATCH1C_LIVE_2026-10-08.md) failed three acceptance shapes on model whim, not on
missing capability: the chest was offered after the wood was verified and the model inspected 8 times (A5 step 1); the
house exit was offered and the model inspected until the no-progress block (A5 step 4, D-B1b-2); the GameMenu / QuestLog
was verifiably open and the only remaining step (close it) never came (D-B1c-1).

Three rules, one budget, one journal event. Every auto act is a GameDecision built from the CURRENT request snapshot's
offered target (never a coordinate or target the catalog did not offer) and goes through the same admission
(``runtime_choice_admitted``, ``_play_action_reason``), the same continuation / precheck gates and the same leased
publication path as a model act. Nothing is skipped; only the model's choice is replaced.

* ``store_start_rule`` (step 1): the D3 rule refused a repeated inspect while a store act is offered
  (``store_act_offered_inspect_redundant``) -> the runtime takes that store act: container_transfer of the offered stack,
  else world_interact of the faced chest (-> store chain -> WS2 ``store_rule`` finish), else approach_target of the offered
  chest. Fires on the first refusal (N=1).
* ``nav_rule`` (step 4): a navigation objective (enter_location / round_trip) and the model inspects twice in a row ->
  move_through the offered warp, else world_interact the offered door, else approach_target the offered exit (not one
  already ARRIVED at); with the objective verdict already ``achieved`` -> finish (``_interact_finish_verdict``).
* ``menu_rule`` (G1, worker hook, D-B1c-1): an "[recorded remark omitted]" objective's menu is observed open -> one
  ``ui_press B`` (else Start) on the offered menu, then the deterministic finish (verified open + verified close).

* ``fishing_rule`` (live batch 2 D-B2-5, V2B2ACC10a/b): a "catch one fish" objective and the model inspects twice in
  a row -> reflex_fish when offered, else select_item of the rod (its own slot only) when it is not held, else
  approach_target of an offered water_edge entry. Keys per stage (rod / edge / cast<n>).
* ``economy_open_rule`` (live batch 2 D-B2-1, V2B2G2craft-a / take-a): an economy objective and the model inspects twice
  in a row -> the offered opener / transfer: pad_button Y for a craft objective (no menu open), economy_transfer of the
  first offered bin / chest target for ship / take; for take with no chest open (D-B2-7) world_interact of the faced
  chest, else approach_target of an offered chest; for sell / buy with no menu open world_interact of the faced shop
  counter (mod kind shop_counter), else approach_target of an offered counter. Keys per operation.

Budget: ``stardew_auto_act_budget`` (default 2, 0 = off) auto acts per objective key - store: per episode; nav: per
leg (the objective's verified transition count; a round trip is two "go to X" legs); menu: per objective - and at most
``AUTO_ACT_GOAL_MAX`` per goal. Journal: ``auto_act`` {rule, key, count, budget, operation, target, replaces},
``auto_act_refused`` {rule, reason}, ``auto_act_budget_exhausted`` {rule, key} (once per key).

Evidence ceiling: UNIT_TESTED (simulated native + live-journal replays). HYPOTHESIS until the v2 live witness.
"""
from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from types import SimpleNamespace

import arti_stardew_combat as combat
import arti_stardew_economy as economy
import arti_stardew_fishing as fishing
import arti_stardew_interact as interact
import arti_stardew_interact_b as interact_b
import arti_stardew_ui_pad as ui_pad
import arti_stardew_world_extras as extras
from arti_stardew_decision import (ActChoice, ActivityIntention, FinishChoice, GameDecision, InspectChoice,
                                   runtime_choice_admitted)

RULE_STORE = "store_start_rule"
RULE_NAV = "nav_rule"
RULE_MENU = "menu_rule"
RULE_FISH = fishing.RULE_FISH
RULE_ECON = "economy_open_rule"
RULE_COMBAT = combat.RULE_COMBAT_AUTO
RULE_EXTRAS = extras.RULE_EXTRAS
RULE_APPROACH = "approach_continue_rule"  # FIXB22 D-B19-1
RULE_GATHER_PICKUP = "gather_pickup_rule"  # FIXB42 A9 t4: matching drops offered, repeated inspect
# D-B7-3b: the counter's A admits an empty hand or a plain tool (mod GHeldPlain: no rod / weapon / object in hand).
_PLAIN_TOOL = re.compile(r"^\(T\)\w*(?:Axe|Pickaxe|Hoe|WateringCan)$")
# fix54L (review L1, ADR 68): the native extras guard GHeldPlain (Protocol/WorldExtras.cs Failing; the held class is
# ModEntry.Extras.cs ExtrasHeldClass -> PadHeld.Classify) admits an empty hand or ANY Tool except a rod (PadHeld.Rod),
# a slingshot, a melee weapon (the scythe too) and the MilkPail / Shears / Pan / Wand types. Weapons and slingshots
# are (W) items; this lists the (T) tool types the guard refuses. _PLAIN_TOOL above stays the safe set to SELECT.
_NATIVE_NOT_PLAIN_TOOL = re.compile(r"^\(T\)\w*(?:Rod|Pole|MilkPail|Shears|Pan|ReturnScepter|Wand|Slingshot)$")
# FIXB7 curfew guard: the operations that start a new stage of an extras / economy / combat objective.
_CURFEW_NEW_STAGE_OPS = ("approach_target", "world_interact", "pad_button", "select_item", combat.OPERATION_COMBAT)
AUTO_ACT_DEFAULT_BUDGET = 2
AUTO_ACT_BUDGET_MAX = 4
AUTO_ACT_GOAL_MAX = 6
APPROACH_FREE_LEGS_MAX = 8  # FIXB4 review: free legs per approach target key (a moving monster is not chased forever)
# FIXB23 D-B20-1 (live V2B20ACC11a: a retreating GreenSlime): a MOVING target (monster / pet / farm animal / horse) gets
# 16 free legs (still bounded: the runtime's moving-target step cap and the per-goal caps).
APPROACH_FREE_LEGS_MOVING_MAX = 16
_MOVING_LEG_PREFIXES = ("monster:", "xt:pet:", "xt:animal:", "xt:horse:")


def approach_free_legs_max(target_ref) -> int:
    return (APPROACH_FREE_LEGS_MOVING_MAX if str(target_ref or "").startswith(_MOVING_LEG_PREFIXES)
            else APPROACH_FREE_LEGS_MAX)
STORE_CHEST_SUFFIXES = ("(BC)130", "(BC)232")
EXIT_KINDS = frozenset({"door", "building_door", "warp", "home_route_stand"})
MENU_CLOSE_BUTTONS = ("B", "Start")


# ---------------------------------------------------------------------------- pure selection (replay-testable)

def store_auto_choice(snap, *, current_ref: str | None = None) -> tuple[str, str | None, dict, str] | None:
    """(operation, target_ref, args, target label) for the offered store act, or None. D-B4-7: the chest the goal is
    already approaching is kept while it is still offered (never switch chests mid-approach)."""
    transfers = tuple(getattr(snap, "transfer_targets", ()) or ())
    if transfers and isinstance(transfers[0], Mapping):
        first = dict(transfers[0])
        return "container_transfer", None, first, str(first.get("source_fact_id") or first.get("slot") or "transfer")
    target = getattr(snap, "world_interaction_target", None)
    if isinstance(target, Mapping) and interact.interaction_kind(target) in (None, interact.KIND_CHEST):
        return "world_interact", None, {}, str(target.get("source_fact_id") or "chest")
    chests = [str(t.get("ref", "")) for t in getattr(snap, "approach_targets", ()) or ()
              if isinstance(t, Mapping) and str(t.get("ref", "")).endswith(STORE_CHEST_SUFFIXES)]
    if current_ref in chests:
        return "approach_target", current_ref, {}, current_ref
    if chests:
        return "approach_target", chests[0], {}, chests[0]
    return None


def nav_auto_choice(objective, snap, *, current_location: str | None,
                    approach_progress: Mapping | None) -> tuple[str, str | None, dict, str] | None:
    """(operation, target_ref, args, target label) for the offered navigation step, or None.

    ``operation == "finish"`` when the objective's own verdict is already achieved (verified transitions or exact native home start)."""
    if objective is None or not getattr(objective, "navigation", False) or objective.blocker:
        return None
    if interact.objective_verdict(objective, current_location)[0] == "achieved":
        return "finish", None, {}, "finish"
    approaches = getattr(snap, "approach_targets", ()) or ()
    # PART_B6 (journal 796-823): standing on Mountain's return warp must not
    # override the offered Mine-side waypoint. Keep native offers unchanged;
    # only prefer exits/stands for the exact enter_location destination.
    toward = tuple(t for t in approaches if isinstance(t, Mapping) and t.get("kind") in EXIT_KINDS
                   and objective.kind == "enter_location" and objective.target
                   and t.get("target_location") == objective.target)
    through = getattr(snap, "move_through_target", None)
    if (isinstance(through, Mapping) and through.get("kind") == interact.KIND_WARP
            and (not toward or through.get("target_location_id") == objective.target)
            and (objective.alias != interact.HOME
                 or interact.exit_offered(objective, through.get("target_location_id"), current_location))):
        return "move_through", None, {}, str(through.get("source_fact_id") or "warp")
    target = getattr(snap, "world_interaction_target", None)
    if (isinstance(target, Mapping) and target.get("kind") == interact.KIND_DOOR
            and (not toward or target.get("target_location_id") == objective.target)
            and (objective.alias != interact.HOME
                 or interact.exit_offered(objective, target.get("target_location_id"), current_location))):
        return "world_interact", None, {}, str(target.get("source_fact_id") or "door")
    progress = approach_progress or {}
    for t in toward or approaches:
        if not isinstance(t, Mapping) or t.get("kind") not in EXIT_KINDS:
            continue
        if progress.get("status") == "ARRIVED" and progress.get("target_ref") == t.get("ref"):
            if t.get("kind") in DOOR_KINDS and not door_hand(snap):
                # FIXB30 D-B27-1 (live V2B27ACC4a: back at the porch (52,16) facing the farmhouse door with the Parsnip
                # still in hand after the eat goal, SMAPI `world_interact unavailable: held_item_not_plain_tool`, no door
                # offered, the model inspected until HELD): a door's A needs a plain tool in hand (the mod's door /
                # forage invariant); select an owned one first, then the door is offered.
                plain = door_tool_choice(snap)
                if plain is not None:
                    return plain
            continue  # already standing at its reach tile: re-approaching is a no-op
        return "approach_target", str(t["ref"]), {}, str(t["ref"])
    return None


DOOR_KINDS = frozenset({"door", "building_door"})


def door_hand(snap) -> bool:
    """FIXB30: the item in hand admits a door press: the snapshot names a held item only when it does not (the runtime
    publishes ``held_item`` for a navigation objective only for a non-plain-tool / empty hand); unknown = admitted."""
    held = getattr(snap, "held_item", None)
    if not isinstance(held, Mapping):
        return True
    qid = held.get("qualified_item_id")
    return isinstance(qid, str) and bool(_PLAIN_TOOL.fullmatch(qid))


def door_tool_choice(snap) -> tuple[str, None, dict, str] | None:
    """FIXB30: an owned plain tool to select for a door press (an empty hand does not admit one), or None."""
    plain = next((dict(i) for i in getattr(snap, "selectable_items", ()) or ()
                  if isinstance(i, Mapping) and _PLAIN_TOOL.fullmatch(str(i.get("qualified_item_id") or ""))),
                 None) if getattr(snap, "select_item_available", False) else None
    if plain is None:
        return row_shift_choice(snap, _PLAIN_TOOL.fullmatch)
    return "select_item", None, plain, "plain_tool:" + str(plain.get("slot"))


def row_shift_choice(snap, wanted) -> tuple[str, None, dict, str] | None:
    """fix74a: the offered row swap when the wanted tool is only in another row of the bag, or None. select_item reaches
    the toolbar row alone; on the next snapshot the tool is selectable. A row with nothing to select still swaps: an empty
    selectable list is what an all-forage row looks like (witness d4 J248), and an empty list closes select_item."""
    offer = getattr(snap, "pad_button_offer", None)
    if not isinstance(offer, Mapping) or not any(wanted(q) for q in offer.get("other_row_tools") or ()):
        return None
    return "pad_button", None, {"button": offer["button"]}, "row_shift"


def plain_tool_choice(snap) -> tuple[str, None, dict, str] | None:
    """D-B7-3b / FIXB23 D-B21-3: an A press that needs an empty hand or a plain tool (shop counter, the Mines up-ladder)
    while something else is held (rod, weapon, object): select an owned plain tool first, or None."""
    held = getattr(snap, "held_item", None) or {}
    qid = held.get("qualified_item_id") if isinstance(held, Mapping) else None
    if isinstance(qid, str) and not _PLAIN_TOOL.fullmatch(qid):
        plain = next((dict(i) for i in getattr(snap, "selectable_items", ()) or ()
                      if isinstance(i, Mapping) and _PLAIN_TOOL.fullmatch(str(i.get("qualified_item_id") or ""))),
                     None) if getattr(snap, "select_item_available", False) else None
        if plain is not None:
            return "select_item", None, plain, "plain_tool:" + str(plain.get("slot"))
        return row_shift_choice(snap, _PLAIN_TOOL.fullmatch)
    return None


def native_plain_qid(qid) -> bool:
    """fix54L: the native GHeldPlain verdict for a held qualified item id (empty hand = plain)."""
    return (not isinstance(qid, str) or not qid
            or qid.startswith("(T)") and not _NATIVE_NOT_PLAIN_TOOL.fullmatch(qid))


def plain_hand(snap) -> bool:
    """FIXB24 review: the item in hand admits an A that needs an empty hand or a plain tool (native GHeldPlain)."""
    held = getattr(snap, "held_item", None) or {}
    qid = held.get("qualified_item_id") if isinstance(held, Mapping) else None
    return native_plain_qid(qid)


def empty_slot_choice(snap) -> tuple[str, None, dict, str] | None:
    """FIXB24 review: an offered empty toolbar slot (no item) to select when no plain tool is owned, or None."""
    if not getattr(snap, "select_item_available", False):
        return None
    empty = next((dict(i) for i in getattr(snap, "selectable_items", ()) or ()
                  if isinstance(i, Mapping) and type(i.get("slot")) is int and not i.get("qualified_item_id")), None)
    return None if empty is None else ("select_item", None, empty, "empty_slot:" + str(empty.get("slot")))


def economy_auto_choice(objective, snap, take_chest: str | None = None) -> tuple[str, str | None, dict, str] | None:
    """(operation, target_ref, args, label) for the economy objective's offered opener / transfer, or None (D-B2-1)."""
    if objective is None or objective.uncertain or objective.finish_recorded:
        return None
    offer = getattr(snap, "pad_button_offer", None)
    if (objective.kind == economy.KIND_CRAFT and objective.done == 0 and isinstance(offer, Mapping)
            and offer.get("button") == "Y"):
        return "pad_button", None, {"button": "Y"}, "Y:" + str(offer.get("opens"))
    transfer = getattr(snap, "economy_offer", None)
    targets = transfer.get("targets") if isinstance(transfer, Mapping) else None
    if objective.kind in (economy.KIND_SHIP, economy.KIND_TAKE) and targets and isinstance(targets[0], Mapping):
        first = dict(targets[0])
        return economy.ECONOMY_OP, None, first, str(first.get("mode")) + ":" + str(first.get("slot"))
    if (objective.kind in (economy.KIND_SELL, economy.KIND_BUY) and getattr(snap, "ui_pad_target", None) is None
            and not objective.done):
        # D-B7-3b (live V2B7G2sell-b: SMAPI `world_interact unavailable: held_item_not_plain_tool`, the rod active): the
        # counter's A needs an empty hand or a plain tool; select an owned plain tool first.
        plain = plain_tool_choice(snap)
        if plain is not None:
            return plain
        # Fix D-B2-1: no menu open -> open the shop at the faced counter, else approach an offered counter.
        target = getattr(snap, "world_interaction_target", None)
        if isinstance(target, Mapping) and target.get("kind") == economy.KIND_SHOP_COUNTER:
            return "world_interact", None, {}, str(target.get("source_fact_id") or "shop_counter")
        for t in getattr(snap, "approach_targets", ()) or ():
            if isinstance(t, Mapping) and t.get("kind") == economy.KIND_SHOP_COUNTER:
                return "approach_target", str(t["ref"]), {}, str(t["ref"])
    if (objective.kind == economy.KIND_SHIP and getattr(snap, "ui_pad_target", None) is None
            and getattr(snap, "container_context", None) is None
            and (objective.remaining is None and objective.done == 0 or (objective.remaining or 0) > 0)):
        # FIXB7 D-B5b-3 (live V2B5bG2ship-a: no bin opener): open the faced shipping bin, else approach the offered bin.
        target = getattr(snap, "world_interaction_target", None)
        if isinstance(target, Mapping) and target.get("kind") == economy.KIND_SHIPPING_BIN:
            return "world_interact", None, {}, str(target.get("source_fact_id") or "shipping_bin")
        for t in getattr(snap, "approach_targets", ()) or ():
            if isinstance(t, Mapping) and t.get("kind") == economy.KIND_SHIPPING_BIN:
                return "approach_target", str(t["ref"]), {}, str(t["ref"])
    if objective.kind == economy.KIND_TAKE and objective.done == 0 and getattr(snap, "container_context", None) is None:
        # D-B2-7: the take-out needs ARTI's OWN pinned chest menu (the mod refuses container_transfer in a chest it did
        # not open by its background world_interact): open the faced chest, else approach an offered chest.
        target = getattr(snap, "world_interaction_target", None)
        if isinstance(target, Mapping) and interact.interaction_kind(target) == interact.KIND_CHEST:
            return "world_interact", None, {}, str(target.get("source_fact_id") or "chest")
        refs = [str(t.get("ref", "")) for t in getattr(snap, "approach_targets", ()) or () if isinstance(t, Mapping)]
        if take_chest is not None and take_chest in refs:
            return "approach_target", take_chest, {}, take_chest  # FIXB12 D-B9-2: the chest known to hold the item
        for ref in refs:
            if ref.endswith(STORE_CHEST_SUFFIXES):
                return "approach_target", ref, {}, ref
    return None


def menu_close_args(objective, offer: Mapping | None) -> dict | None:
    """ui_press args closing the objective's own menu from the offered ui_pad block, or None."""
    if objective is None or offer is None or objective.kind not in ui_pad.OPEN_BUTTON:
        return None
    pins = offer.get("pins") or {}
    if pins.get("menu_type") not in ui_pad.TARGET_MENUS[objective.kind]:
        return None
    press = offer.get("press") or {}
    btn = next((b for b in MENU_CLOSE_BUTTONS if b in press), None)
    if btn is None:
        return None
    return dict(pins, btn=btn, snapped_id=offer["snapped_id"])


def _without_approach_refs(snap, refs: set):
    """A view of the request snapshot without the given approach targets (presentation for the rule pick only)."""
    from dataclasses import is_dataclass, replace
    kept = tuple(t for t in getattr(snap, "approach_targets", ()) or ()
                 if not (isinstance(t, Mapping) and t.get("ref") in refs))
    if is_dataclass(snap):
        return replace(snap, approach_targets=kept)
    return SimpleNamespace(**dict(vars(snap), approach_targets=kept))


# ---------------------------------------------------------------------------- runtime mixin

class AutoActRuntimeMixin:
    """Mixed into StardewGoalRuntime; uses its lock, snapshot builder, admission gates and journal."""

    def _auto_act_budget(self) -> int:
        raw = self._config.get("stardew_auto_act_budget", AUTO_ACT_DEFAULT_BUDGET)
        if type(raw) is not int:
            return AUTO_ACT_DEFAULT_BUDGET
        return max(0, min(AUTO_ACT_BUDGET_MAX, raw))

    @staticmethod
    def _auto_act_key(goal, rule: str, stage: str | None = None) -> str:
        if rule == RULE_STORE and stage:
            return f"store:episode{goal.episode_index}:{stage}"  # D-B4-7: per chest target
        if rule in (RULE_FISH, RULE_ECON, RULE_COMBAT, RULE_EXTRAS, RULE_APPROACH):
            return {RULE_FISH: "fish:", RULE_ECON: "econ:", RULE_COMBAT: "combat:", RULE_EXTRAS: "extras:",
                    RULE_APPROACH: "cont:"}[rule] + str(stage)
        if rule == RULE_NAV:
            objective = goal.interact_objective
            return f"nav:{objective.kind}:leg{objective.transitions}"
        if rule == RULE_MENU:
            return "menu:" + goal.menu_objective.kind
        return f"store:episode{goal.episode_index}"

    def _auto_act_left(self, goal, rule: str, stage: str | None = None) -> bool:
        key = self._auto_act_key(goal, rule, stage)
        budget = self._auto_act_budget()
        acts = goal.auto_acts
        if acts.get(key, 0) < budget and sum(acts.values()) < AUTO_ACT_GOAL_MAX:
            return True
        if budget and key not in goal.auto_act_exhausted:
            goal.auto_act_exhausted.add(key)
            self._journal({"ev": "auto_act_budget_exhausted", "goal_id": goal.goal_id, "rule": rule, "key": key,
                           "count": acts.get(key, 0), "budget": budget, "goal_total": sum(acts.values())})
        return False

    def _auto_act_take(self, goal, rule: str, request, operation: str, target: str, replaces: str,
                       stage: str | None = None, charge: bool = True) -> None:
        key = self._auto_act_key(goal, rule, stage)
        if charge:
            goal.auto_acts[key] = goal.auto_acts.get(key, 0) + 1
        self._journal({"ev": "auto_act", "goal_id": goal.goal_id, "generation_id": request.generation_id,
                       "rule": rule, "key": key, "count": goal.auto_acts.get(key, 0), "budget": self._auto_act_budget(),
                       "operation": operation, "target": target, "replaces": replaces,
                       **({} if charge else {"leg": True})})

    @staticmethod
    def _approach_leg(goal, key: str, tile) -> bool:
        """D-B4-7: the same approach target as the last auto act of this key AND the farmer moved since: a further leg of
        one approach (not charged). A blocked approach (no movement) is charged; its target fails at the budget."""
        prev = goal.auto_act_legs.get(key)
        if prev is None or tile is None:
            return False
        prev_tile, prev_steps = (prev if len(prev) == 2 and isinstance(prev[0], tuple) else (tuple(prev), None))
        # D-B5b-4 (live V2B5bG6shake-a: two acts for 68,23 back to back with no approach step in between charged the
        # whole budget while the approach was still closing in): no approach step since the last act = not a failed leg.
        ran = prev_steps is None or goal.approach_steps != prev_steps
        return tuple(prev_tile) != tuple(tile) or not ran

    # ------------------------------------------------------------------ in-generation substitution (store / nav)

    def _auto_act_substitute(self, decision, request, play_reason: str | None):
        """Under the runtime lock, right after the model decision's play gate. Returns the substituted decision
        (already admitted by the same gates) or None (the model decision proceeds exactly as before)."""
        goal = self._active
        if (goal is not None and getattr(decision.choice, "kind", None) in ("ask_owner", "finish")
                and goal.play_mode == "local_activity" and goal.progress_marker != "terminal" and not goal.question_basis
                and not self._day_start_active):
            return self._auto_act_defer_ask_owner(decision, request, goal)
        if (goal is not None and getattr(decision.choice, "kind", None) == "wait"
                and getattr(request, "reason", None) != "owner_turn"  # review: "tunggu" from the owner is a real wait
                and getattr(decision.choice, "reason", None) not in self._RULE_WAIT_REASONS
                and (getattr(goal, "interact_objective", None) is not None
                     or self._approach_continue_pick(goal, request.snapshot) is not None)  # FIXB22 D-B19-1
                and goal.play_mode == "local_activity" and goal.progress_marker != "terminal" and not goal.question_basis
                and not self._day_start_active):
            # FIXB21 D-B18-3 (live V2B18ACC4a: after the first 6-step batch of the round trip the model chose `wait`; nothing
            # continued -> HELD): a model wait while the nav objective still offers its leg is deferred like ask_owner.
            return self._auto_act_defer_ask_owner(decision, request, goal)
        if (goal is None or not isinstance(decision.choice, InspectChoice) or goal.play_mode != "local_activity"
                or goal.progress_marker == "terminal" or goal.question_basis or self._day_start_active):
            return None
        return self._auto_act_run(decision, request, goal, play_reason)

    ASK_OWNER_DEFERRALS_MAX = 2
    # waits the runtime's own rules publish (never deferred: they are the rule's step)
    _RULE_WAIT_REASONS = frozenset({"stage_lock", "combat_wait_for_aggro", "clear_path_pickaxe", "clear_path_route",
                                    "clear_path_select_refresh", "routine_eat_rule", "extras_blocked", "skill_boundary",
                                    "skill_refused"})

    def _auto_act_defer_ask_owner(self, decision, request, goal):
        """D-B5-4 (live V2B5ACC10a: the model's FIRST decision was ask_owner "I can't reach the water" with the rod, the
        water edges and fishing_rule all offered -> HELD, 0 inputs): an ask_owner / finish while an objective rule still
        has an offered step is deferred (at most twice per goal, journaled ask_owner_deferred): the rule's step is taken
        (budget-counted, same gates) or, when the play gate first needs a current inspect, an inspect is taken instead.
        Nothing offered -> the model's ask_owner / finish stands."""
        if goal.ask_owner_deferrals >= self.ASK_OWNER_DEFERRALS_MAX:
            return None
        blocked = self._ask_owner_must_stand(decision, request)
        if blocked is not None:
            self._journal({"ev": "ask_owner_not_deferred", "goal_id": goal.goal_id, "reason": blocked})
            return None
        # FIXB22 review MAJOR: a model finish / ask_owner is never overridden by the approach continuation (only an
        # inspect or a deferred wait continues an approach)
        rule, picked = self._auto_act_pick(goal, request.snapshot, None, set(), first_ok=True,
                                           allow_continue=getattr(decision.choice, "kind", None) == "wait")
        if picked is None:
            return None
        goal.ask_owner_deferrals += 1
        refused: list = []
        # A first decision carries no activity yet (play_activity_required): the rule names its own bounded step.
        activity = (ActivityIntention("objective step (" + rule + ")", "the owner's objective", None, picked[0])
                    if goal.activity_intent is None else None)
        auto = self._auto_act_commit(goal, decision, request, request.snapshot, rule, picked, refused_out=refused,
                                     replaces=decision.choice.kind, activity=activity)
        if auto is None and refused and refused[0] == "play_inspect_required":
            auto = GameDecision(decision.v, decision.request_nonce, decision.goal_id, decision.goal_revision,
                                decision.context_ref, "", InspectChoice("state", "objective step offered: observe", None))
        self._journal({"ev": "ask_owner_deferred", "goal_id": goal.goal_id, "generation_id": request.generation_id,
                       "model_choice": decision.choice.kind, "rule": rule, "operation": picked[0],
                       "taken": None if auto is None else auto.choice.kind, "refused": refused[0] if refused else None,
                       "deferrals": goal.ask_owner_deferrals})
        return auto

    _OWNER_WORDS = re.compile(r"\b(?:owner|host|bohan|tuan|pemilik)\b", re.IGNORECASE)

    def _ask_owner_must_stand(self, decision, request) -> str | None:
        """FIXB6 review: never defer an ask_owner / finish while a ReadyCheck or the bed / sleep question is up (the owner
        or HOST acts there), or when the model's words name the owner / HOST."""
        pid = request.snapshot.binding.player_id
        rec = self._obs.get(pid, "ui.context") if pid is not None else None
        facts = rec.facts if rec is not None and isinstance(rec.facts, Mapping) else {}
        if isinstance(facts.get("ready_check"), Mapping):
            return "ready_check"
        for value in facts.values():
            if isinstance(value, Mapping) and value.get("question_key") == "Sleep":
                return "sleep_question"
        text = " ".join(str(x) for x in (decision.speech, getattr(decision.choice, "question", None)) if x)
        if self._OWNER_WORDS.search(text):
            return "names_owner"
        return None

    def _auto_act_run(self, decision, request, goal, play_reason):
        snap = request.snapshot
        failed_refs: set = set()
        for _attempt in range(3):
            rule, picked = self._auto_act_pick(goal, snap, play_reason, failed_refs)
            if picked is None or picked[0] != "approach_target":
                break
            stage = self._auto_act_stage(goal, rule, picked)
            key = self._auto_act_key(goal, rule, stage)
            tile = getattr(getattr(snap, "motor", None), "tile", None)
            leg = self._approach_leg(goal, key, tile)
            if leg and goal.auto_act_free_legs.get(key, 0) >= approach_free_legs_max(picked[1]):
                self._journal({"ev": "approach_leg_budget", "goal_id": goal.goal_id, "rule": rule, "key": key,
                               "legs": goal.auto_act_free_legs.get(key, 0), "max": approach_free_legs_max(picked[1])})
            elif leg or self._auto_act_left(goal, rule, stage):
                break
            # D-B4-3 / D-B4-7: this target's approach budget is spent without reaching it: it fails for the goal and
            # the next offered target of the same rule is tried (bounded: three picks, the per-goal cap still holds).
            failed_refs.add(picked[1])
            goal.approach_blocked_refs.add(picked[1])
            self._journal({"ev": "auto_act_target_failed", "goal_id": goal.goal_id, "rule": rule, "key": key,
                           "target": picked[1]})
            picked = None
        if picked is None:
            return None
        # Combat and the scoped additional-wood pickup rule bind their own bounded objective
        # step when no activity is declared yet (as the D-B5-4 deferral does). Admission still
        # checks that activity; other inspect rules require the model's declared activity.
        activity = (ActivityIntention("objective step (" + rule + ")", "the owner's objective", None, picked[0])
                    if goal.activity_intent is None and rule in (RULE_COMBAT, RULE_GATHER_PICKUP)
                    and self._v2_pad_path() else None)
        return self._auto_act_commit(goal, decision, request, snap, rule, picked, activity=activity)

    def _auto_act_pick(self, goal, snap, play_reason, failed_refs: set, first_ok: bool = False,
                       allow_continue: bool = True):
        """(rule, picked) for the current inspect; ``failed_refs`` are approach targets no longer to be proposed.
        ``first_ok`` (D-B5-4 deferral) lets every rule pick without the second-inspect condition."""
        if (getattr(goal, "animal_executor", None) or {}).get("status") == "running":
            return None, None  # B21: executor inspections must not become an exit walk.
        if self._goal_route_owns(goal):
            return None, None  # FIXB38 travel owns its bounded steps; inspect rules cannot finish a subleg
        if failed_refs:
            snap = _without_approach_refs(snap, failed_refs)
        rule = picked = None
        if getattr(goal, "last_choice_kind", None) != "inspect" and not first_ok:
            # D-B4-6 (live V2B4ACC11a): in the Mines the first inspect's refresh died on scene churn (capture_changed)
            # before a second inspect could trigger the rule; a combat step the snapshot already offers (weapon
            # select / reflex / monster approach) is taken on the FIRST inspect. Every other rule keeps N=2.
            if (play_reason is None and combat.combat_open(getattr(goal, "combat", None))
                    and goal.gather_progress is None and goal.tool_work is None):
                rule, picked = RULE_COMBAT, combat.combat_auto_choice(goal.combat, snap,
                                                                      approach_progress=goal.approach_progress)
            return rule, picked
        elif play_reason == "store_act_offered_inspect_redundant":
            current = goal.approach_target.target_ref if getattr(goal, "approach_target", None) is not None else None
            rule, picked = RULE_STORE, store_auto_choice(snap, current_ref=current)
        elif play_reason is None and self._additional_store_pickup_open(goal, snap):
            progress = goal.gather_progress
            current = getattr(getattr(goal, "approach_target", None), "target_ref", None)
            drops = [t for t in getattr(snap, "approach_targets", ()) or ()
                     if isinstance(t, Mapping) and str(t.get("ref", "")).startswith("drop:")
                     and t.get("reach") == "DIRECT_TARGET"
                     and t.get("qualified_item_id") == progress.qualified_item_id
                     and t.get("quality") == progress.quality
                     and t.get("ref") not in goal.approach_blocked_refs]
            if drops:
                ref = next((t["ref"] for t in drops if t["ref"] == current), drops[0]["ref"])
                rule, picked = RULE_GATHER_PICKUP, ("approach_target", ref, {}, ref)
        elif play_reason is None and goal.interact_objective is not None and goal.gather_progress is None \
                and goal.tool_work is None:
            pin = snap.water_context or snap.movement_context
            rule, picked = RULE_NAV, nav_auto_choice(goal.interact_objective, snap,
                                                     current_location=pin.location if pin is not None else None,
                                                     approach_progress=goal.approach_progress)
        elif (play_reason is None and fishing.objective_open(getattr(goal, "fishing", None))
              and goal.gather_progress is None and goal.tool_work is None):
            # D-B2-5 (live V2B2ACC10a/b): rod / water edge / reflex offered, the model inspected for the rod 10 times.
            rule, picked = RULE_FISH, self._fishing_auto_pick(goal, snap)
        elif (play_reason is None and combat.combat_open(getattr(goal, "combat", None))
              and goal.gather_progress is None and goal.tool_work is None):
            # D-B3-6 (live V2B3ACC11a): reflex_combat offered (sword held, slime near), the model asked for the weapon 10x.
            rule, picked = RULE_COMBAT, combat.combat_auto_choice(goal.combat, snap,
                                                                  approach_progress=goal.approach_progress)
        elif (play_reason is None and extras.objective_open(getattr(goal, "extras_objective", None))
              and not self._animal_open(goal)
              and goal.gather_progress is None and goal.tool_work is None):
            # D-B3-5 (live V2B3G6shake-a / plant-a): approach entries offered, the model swung the axe / inspected.
            pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
            rule, picked = RULE_EXTRAS, extras.auto_choice(goal.extras_objective, snap,
                                                           approach_progress=goal.approach_progress,
                                                           seed_slots=self._extras_seed_slots(pin))
        elif (play_reason is None and getattr(goal, "economy_objective", None) is not None
              and goal.gather_progress is None and goal.tool_work is None):
            # D-B2-1 (live V2B2G2craft-a): the craft opener (pad_button Y) / a bin or chest transfer offered, 10 inspects.
            rule, picked = RULE_ECON, economy_auto_choice(goal.economy_objective, snap,
                                                          take_chest=self._take_chest_choice(goal, snap))
        if picked is None and play_reason is None and allow_continue:
            cont = self._approach_continue_pick(goal, snap)
            if cont is not None:
                rule, picked = RULE_APPROACH, cont
        return rule, picked

    def _additional_store_pickup_open(self, goal, snap) -> bool:
        """Only the additional-wood chest goal's current pickup phase, with ordinary auto-act caps."""
        progress = getattr(goal, "gather_progress", None)
        plan = getattr(goal, "episode_plan", None)
        spec = plan[goal.episode_index] if plan and goal.episode_index < len(plan) else None
        pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
        return bool(goal.gather_store_required and progress is not None
                    and progress.status == "WORK_REMAINING" and progress.qualified_item_id == "(O)388"
                    and not goal.gather_uncertain and spec is not None and spec.mode == "additional"
                    and self._v2_pad_path(pin))

    def _approach_continue_pick(self, goal, snap):
        """FIXB22 D-B19-1 (live V2B19ACC1a: the tree stayed offered, two 6-step batches around the cabin, then the model
        inspected until HELD): an approach still under way (PROPOSED / PROGRESS / FACING_REQUIRED) whose target is still
        offered is continued on the model's repeated inspect (or deferred wait), like the other rules' legs (free legs
        while the farmer moves, the same per-target budget otherwise). v2 pad path only."""
        if goal is None or goal.progress_marker == "terminal" or getattr(goal, "last_finish", None) is not None:
            return None  # review: only while the goal's objective is still open
        target = getattr(goal, "approach_target", None)
        progress = getattr(goal, "approach_progress", None) or {}
        ref = getattr(target, "target_ref", None)
        if (ref is None or progress.get("target_ref") != ref
                or progress.get("status") not in ("PROPOSED", "PROGRESS", "FACING_REQUIRED") or not self._v2_pad_path()):
            return None
        if not any(isinstance(e, Mapping) and e.get("ref") == ref for e in getattr(snap, "approach_targets", ()) or ()):
            return None
        return "approach_target", str(ref), {}, str(ref)

    def _fishing_auto_pick(self, goal, snap):
        """FIXB20 D-B17-3: the fishing rule with its pinned water edge; a durable refusal on the arrived edge (stamina,
        full bag, flags) ends the objective BLOCKED `reflex_fish_unavailable:<reason>` instead of walking the shore."""
        edge = goal.fishing_edge if isinstance(getattr(goal, "fishing_edge", None), dict) else {}
        progress = goal.approach_progress or {}
        summary = getattr(snap, "fishing_objective", None) or {}
        refusal = summary.get("reflex_refusal")
        arrived = (progress.get("status") == "ARRIVED"
                   and str(progress.get("target_ref") or "").startswith(fishing.KIND_WATER_EDGE + ":"))
        if (arrived and summary.get("rod_held") is True and refusal in fishing.DURABLE_REFUSALS
                and getattr(snap, "reflex_fish_target", None) is None):
            goal.fishing = replace(goal.fishing, status="BLOCKED", reason="reflex_fish_unavailable:" + refusal)
            self._journal({"ev": "reflex_fish_blocked", "goal_id": goal.goal_id, "reason": refusal,
                           "edge": progress.get("target_ref")})
            return None
        allow_hop = True
        if arrived and refusal in fishing.POSITIONAL_REFUSALS:
            # Review: a `not_facing_water` read before the pose settled never costs a hop: one face step first, and a
            # hop only on a ui.context read published after the arrival with ARTI facing the edge's water.
            ref = progress.get("target_ref")
            now = time.monotonic()
            if edge.get("arrived_ref") != ref:
                edge = dict(edge, arrived_ref=ref, arrived_at=now, faced=0)
                goal.fishing_edge = edge
            _, pid = self._binding_view()
            motor = self._state.latest(pid) if pid is not None else None
            need = fishing.edge_facing(ref)
            if need is not None and getattr(motor, "facing", None) != need:
                if edge.get("faced", 0) < 1:
                    goal.fishing_edge = edge = dict(edge, faced=edge.get("faced", 0) + 1)
                    self._journal({"ev": "reflex_fish_edge", "goal_id": goal.goal_id, "status": "face", "edge": ref})
                    return ("world_key.move_cardinal", None,
                            {"direction": ("up", "right", "down", "left")[need], "ticks": 1}, "face_water")
                allow_hop = False
            else:
                pin = getattr(snap, "water_context", None) or getattr(snap, "movement_context", None)
                rec = self._obs.get(pin.player_id, "ui.context") if pin is not None else None
                received = getattr(rec, "received_monotonic", None)
                allow_hop = type(received) in (int, float) and received > edge.get("arrived_at", now)
        picked = fishing.auto_choice(goal.fishing, snap, approach_progress=goal.approach_progress, edge=edge,
                                     allow_hop=allow_hop)
        if picked is not None and picked[0] == "approach_target":
            if str(picked[3]).startswith(("hop:", "repin:")):
                edge = dict(edge, ref=picked[1], hops=edge.get("hops", 0) + 1,
                            tried=sorted(set(edge.get("tried") or ()) | {progress.get("target_ref")}))
                self._journal({"ev": "reflex_fish_edge", "goal_id": goal.goal_id,
                               "status": str(picked[3]).split(":", 1)[0], "edge": picked[1],
                               "refusal": refusal, "hops": edge["hops"]})
                picked = (picked[0], picked[1], picked[2], picked[1])
            elif edge.get("ref") is None:
                edge = dict(edge, ref=picked[1])
                self._journal({"ev": "reflex_fish_edge", "goal_id": goal.goal_id, "status": "pinned", "edge": picked[1]})
            goal.fishing_edge = edge
        return picked

    @staticmethod
    def _auto_act_stage(goal, rule, picked) -> str | None:
        operation, target_ref = picked[0], picked[1]
        if operation == "approach_target" and rule in (RULE_STORE, RULE_ECON, RULE_EXTRAS, RULE_COMBAT, RULE_APPROACH):
            return "approach:" + str(target_ref)  # D-B4-3 / D-B4-7: the approach budget is per target
        stage = None
        if rule == RULE_FISH:
            stage = ("cast%d" % goal.fishing.calls if operation == fishing.OPERATION
                     else "rod" if operation == "select_item" else "face" if operation == "world_key.move_cardinal"
                     else "edge")
        elif rule == RULE_ECON:
            stage = operation
        elif rule == RULE_EXTRAS:
            stage = {"world_interact": "interact", "select_item": "select"}.get(operation, "approach")
        elif rule == RULE_COMBAT:
            stage = "cast%d" % goal.combat.calls if operation == combat.OPERATION_COMBAT else "weapon"
        return stage

    def _auto_act_commit(self, goal, decision, request, snap, rule, picked, refused_out: list | None = None,
                         replaces: str = "inspect", activity=None):
        operation, target_ref, args, label = picked
        if (rule in (RULE_ECON, RULE_EXTRAS, RULE_COMBAT, RULE_APPROACH, RULE_GATHER_PICKUP)
                and operation in _CURFEW_NEW_STAGE_OPS
                and goal.play_mode == "local_activity" and self._v2_pad_path()
                and interact_b.curfew_phase(getattr(getattr(snap, "motor", None), "time_of_day", None)) is not None):
            # FIXB7 curfew guard: from 24:00 the extras / economy / combat rules start no new stage (approach, opener,
            # cast); an open menu's own steps (economy_transfer, close) still finish.
            self._journal({"ev": "auto_act_refused", "goal_id": goal.goal_id, "generation_id": request.generation_id,
                           "rule": rule, "operation": operation, "reason": "curfew_window"})
            if refused_out is not None:
                refused_out.append("curfew_window")
            return None
        stage = self._auto_act_stage(goal, rule, picked)
        key = self._auto_act_key(goal, rule, stage)
        tile = getattr(getattr(snap, "motor", None), "tile", None)
        leg = (operation == "approach_target" and self._approach_leg(goal, key, tile)
               and goal.auto_act_free_legs.get(key, 0) < approach_free_legs_max(target_ref))
        if not leg and not self._auto_act_left(goal, rule, stage):
            return None
        choice = FinishChoice() if operation == "finish" else ActChoice(operation, target_ref, args, ())
        auto = GameDecision(decision.v, decision.request_nonce, decision.goal_id, decision.goal_revision,
                            decision.context_ref, "", choice, activity)
        from arti_stardew_runtime import _fill_observed_chest_args
        auto = _fill_observed_chest_args(auto, snap)
        reason = None if runtime_choice_admitted(auto) else "choice_unavailable"
        reason = reason or self._play_action_reason(auto, request)
        if reason:
            self._journal({"ev": "auto_act_refused", "goal_id": goal.goal_id, "generation_id": request.generation_id,
                           "rule": rule, "operation": operation, "reason": reason})
            if refused_out is not None:
                refused_out.append(reason)
            return None
        self._auto_act_take(goal, rule, request, operation, label, replaces=replaces, stage=stage, charge=not leg)
        if operation == "approach_target" and tile is not None:
            goal.auto_act_legs[key] = (tuple(tile), goal.approach_steps)
        if leg:
            goal.auto_act_free_legs[key] = goal.auto_act_free_legs.get(key, 0) + 1
        return auto

    # ------------------------------------------------------------------ menu_rule (worker hook, D-B1c-1)

    def _menu_rule_objective(self, goal):
        """The goal's G1 "[recorded remark omitted]" objective when the rule may act on it (caller holds the lock)."""
        from arti_stardew_runtime import READY
        objective = getattr(goal, "menu_objective", None) if goal is not None else None
        if (objective is None or objective.kind not in ui_pad.OPEN_BUTTON or not objective.opened
                or objective.finish_recorded or goal.play_mode != "local_activity"
                or goal.progress_marker == "terminal" or goal.question_basis or self._status != READY
                or self._worker_stop.is_set() or self._day_start_active
                or not self._config.get("stardew_menu_rule_enabled", True)):
            return None
        return objective

    def _menu_rule_press_ready(self, goal, objective) -> bool:
        if objective.closed or not self._auto_act_budget():
            return False
        _, pid = self._binding_view()
        view = self._ui_pad_view(pid)
        return (view is not None and view.available and view.menu_type in ui_pad.TARGET_MENUS[objective.kind]
                and any(view.press_allowed(b) for b in MENU_CLOSE_BUTTONS)
                and view.receiver_token not in self._ui_pad_uncertain)

    def menu_rule_due(self) -> bool:
        """Caller holds the lock (trial runner idle check): the worker's menu_rule acts or finishes next."""
        try:
            goal = self._active
            objective = self._menu_rule_objective(goal)
            if objective is None:
                return False
            if objective.closed:
                return ui_pad.objective_verdict(objective)[0] == "achieved"
            budget = self._auto_act_budget()
            key = self._auto_act_key(goal, RULE_MENU)
            return (goal.auto_acts.get(key, 0) < budget and sum(goal.auto_acts.values()) < AUTO_ACT_GOAL_MAX
                    and self._menu_rule_press_ready(goal, objective))
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def auto_act_due(self) -> bool:
        """Trial runner idle check (caller holds the lock): a runtime auto act / rule finish is about to run."""
        return (self.menu_rule_due() or getattr(self, "economy_shop_rule_due", lambda: False)()
                or getattr(self, "combat_weapon_stage_due", lambda: False)() or self._stage_lock_owner() is not None
                or getattr(self, "combat_rearm_due", lambda: False)()
                # FIXB19 D-B16-2 (live V2B16ACC11c/d: `combat_wait_for_aggro timeout next=clear_path` fired inside this idle
                # check; the owner was gone, nothing pending -> HELD before process_stage_lock resumed the clear_path leg):
                # a stage that just ended still awaits its resume (one fresh inspect + continuation), bounded.
                or self._stage_resume_pending())

    STAGE_RESUME_GRACE_S = 10.0

    def _stage_resume_pending(self) -> bool:
        """Caller holds the lock. FIXB19 review: an ended stage (no owner, `_stage_lock_last` set) counts as work until
        process_stage_lock resumes it, at most STAGE_RESUME_GRACE_S after the end was first seen; then
        `stage_resume_overdue` is journaled once and the idle check may hold the trial."""
        last = getattr(self, "_stage_lock_last", None)
        if last is None or self._stage_lock_owner() is not None:
            self._stage_resume_since = None
            return False
        now = time.monotonic()
        since = getattr(self, "_stage_resume_since", None)
        if since is None or since[0] != last:
            self._stage_resume_since = since = (last, now, False)
        if now - since[1] <= self.STAGE_RESUME_GRACE_S:
            return True
        if not since[2]:
            self._stage_resume_since = (since[0], since[1], True)
            goal = self._active
            self._journal({"ev": "stage_resume_overdue", "goal_id": goal.goal_id if goal else None, "stage": last,
                           "waited_s": round(now - since[1], 1)})
        return False

    # ------------------------------------------------------------------ D-B6-1 stage lock (worker stages own the goal)

    def _stage_lock_owner(self) -> str | None:
        """Caller holds the lock. D-B6-1 (live V2B6ACC11a: a model ui_press closed the GameMenu between weapon-stage steps,
        a refused model press ended the trial with the sword on the cursor): the worker stage that owns the goal's next
        presses, or None. While it owns them no provider decision is made (like a skill's step program)."""
        goal = self._active
        if (goal is None or getattr(goal, "progress_marker", None) == "terminal"
                or getattr(goal, "play_mode", None) != "local_activity"):
            return None
        try:
            if self._combat_weapon_stage_owns(goal):
                return "combat_weapon_stage"
            if self._item_stage_owns(goal):
                return "item_to_toolbar"  # D-B7b-2
            if self._combat_wait_owns(goal):
                return "combat_wait_for_aggro"  # FIXB11 D-B8-1
            if self._extras_stand_wait_owns(goal):
                return "extras_stand_wait"  # FIXB16 D-B14-3
            if self._economy_shop_stage_owns(goal):
                return "economy_shop_rule"
        except Exception:  # noqa: BLE001 - a lock probe never raises
            return None
        return None

    def _stage_lock_holds(self) -> bool:
        """Caller holds the lock: True while a stage owns the goal (journals ``stage_lock`` once per stage)."""
        owner = self._stage_lock_owner()
        last = getattr(self, "_stage_lock_last", None)
        if owner is not None and owner != last:
            self._journal({"ev": "stage_lock", "goal_id": self._active.goal_id, "stage": owner, "status": "locked"})
        self._stage_lock_last = owner if owner is not None else last
        return owner is not None

    def process_stage_lock(self) -> int:
        """Worker hook: when the owning stage ends, journal the release and resume with ONE fresh read-only inspect and
        continuation (the ticket held during the stage is stale by then)."""
        with self._lock:
            last = getattr(self, "_stage_lock_last", None)
            if last is None or self._stage_lock_owner() is not None or self._action_busy \
                    or self._inflight_generation is not None:
                return 0
            self._stage_lock_last = None
            self._stage_resume_since = None
            goal = self._active
            self._journal({"ev": "stage_lock", "goal_id": goal.goal_id if goal else None, "stage": last,
                           "status": "released"})
            self._pending_continuation = None
        self._resume_after_pause(reason="stage_end:" + last)
        return 1

    STAGE_LOCK_SPEECH = {
        "combat_weapon_stage": "Sebentar, aku sedang memindahkan senjata ke toolbar dulu.",
        "economy_shop_rule": "Sebentar, aku sedang menyelesaikan transaksi di toko dulu.",
        "item_to_toolbar": "Sebentar, aku sedang memindahkan barang ke toolbar dulu.",
        "combat_wait_for_aggro": "Sebentar, aku menunggu monsternya mendekat dulu.",
        "extras_stand_wait": "Sebentar, aku menunggu dia pindah ke tempat yang bisa kudekati.",
        "clear_path": "Sebentar, aku pecahkan batu yang menghalangi jalan dulu.",
        None: "Sebentar, aku sedang menyelesaikan langkah ini dulu.",
    }

    def _stage_invoke(self, request):
        """_invoke_provider hook (after the skills hook): a deterministic decision JSON or None (provider as before).
        Order: a locked worker stage answers ``wait`` (D-B6-1); the curfew home / bed stage steps (D-B6-3); the combat
        rule on the request's own fresh snapshot (D-B6-2)."""
        import json
        with self._lock:
            goal = self._active
            if goal is None or request.goal_id != goal.goal_id:
                return None
            if request.reason == "owner_turn" and self._stage_lock_holds():
                # FIXB8 review: the owner is never silenced by a stage: a short spoken reply (a model act now would land
                # mid-stage; any act is refused anyway by _play_action_reason: stage_lock_active). Stop / pause words
                # are handled before any decision, as always.
                self._journal({"ev": "stage_lock", "goal_id": goal.goal_id, "stage": self._stage_lock_last,
                               "status": "owner_reply"})
                return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                   "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                   "speech": self.STAGE_LOCK_SPEECH.get(self._stage_lock_last, self.STAGE_LOCK_SPEECH[None]),
                                   "choice": {"kind": "speak"}, "activity": None}, ensure_ascii=False)
            if self._stage_lock_holds():
                self._journal({"ev": "stage_lock", "goal_id": goal.goal_id, "stage": self._stage_lock_last,
                               "status": "provider_refused", "reason": request.reason})
                return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                                   "goal_revision": request.goal_revision, "context_ref": request.context_ref,
                                   "speech": "", "choice": {"kind": "wait", "reason": "stage_lock", "ms": 0},
                                   "activity": None})
        raw = self._curfew_stage_invoke(request)
        if raw is not None:
            return raw
        raw = self._animal_executor_invoke(request)
        if raw is not None:
            return raw
        raw = self._animal_exception_refresh(request)
        if raw is not None:
            return raw
        raw = self._goal_route_invoke(request)  # FIXB38: deterministic Farm itinerary, then the original domain rule
        if raw is not None:
            return raw
        raw = self._offscene_leg_invoke(request)  # FIXB15 D-B12-6: leave the house toward an off-scene target first
        if raw is not None:
            return raw
        raw = self._till_face_invoke(request)  # FIXB16 D-B14-5: turn to a tillable side
        if raw is not None:
            return raw
        raw = self._tool_work_rule_invoke(request)  # FIXB18 D-B15a-1: select -> approach -> face -> swing
        if raw is not None:
            return raw
        raw = self._gather_tool_rule_invoke(request)  # FIXB31 D-SOAK3-1: the gather chain selects its own tool
        if raw is not None:
            return raw
        raw = self._economy_ship_rule_invoke(request)  # FIXB36: fresh bin route/open/transfer, no model delay
        if raw is not None:
            return raw
        raw = self._routine_eat_stage_invoke(request)  # FIXB20 D-B17-1: select food -> eat site / face -> eat rule
        if raw is not None:
            return raw
        raw = self._routine_bed_stage_invoke(request)  # FIXB21: on the bed's stand: face the sleep row -> move_through
        if raw is not None:
            return raw
        raw = self._mines_exit_invoke(request)  # FIXB17 D-B5-0: the combat goal's Mines exit leg
        if raw is not None:
            return raw
        raw = self._pet_rule_invoke(request)  # FIXB41: fresh short pet approach / press before any model move
        if raw is not None:
            return raw
        raw = self._extras_press_invoke(request)  # FIXB17 D-B14c-1: arrived + faced: the extras press, no model turn
        if raw is not None:
            return raw
        raw = self._clear_path_invoke(request)  # FIXB13 D-B10-1: the rock-breaking leg of a combat goal
        if raw is not None:
            return raw
        return self._combat_rule_invoke(request)

    @staticmethod
    def _rule_json(request, choice: dict, rule: str, label: str) -> str:
        import json
        activity = ({"activity": ("objective step (" + rule + ")")[:80], "purpose": "the owner's objective",
                     "target": choice.get("target_ref"), "next_step": str(choice.get("operation"))[:160]}
                    if choice.get("kind") == "act" else None)
        return json.dumps({"v": 1, "request_nonce": request.request_nonce, "goal_id": request.goal_id,
                           "goal_revision": request.goal_revision, "context_ref": request.context_ref, "speech": "",
                           "choice": choice, "activity": activity}, ensure_ascii=False)

    COMBAT_DIRECT_MAX = 24
    RESUME_UNSENT_MAX = 3  # ADR 177: re-proposals of a batch whose publication sent nothing

    def _combat_rule_invoke(self, request):
        """D-B6-2 (live V2B6ACC11b: 20 model reflex choices discarded at L2513 while slimes took HP 100 -> 36): in v2 pad
        mode, a continuation of an open combat objective takes the combat_rule step (reflex_combat when offered, else
        the weapon select / monster approach) from the request's own fresh snapshot, without a provider round trip.
        Bounded (COMBAT_DIRECT_MAX per goal, the objective's own call cap)."""
        from arti_stardew_runtime import _CONTINUATION_REASONS
        with self._lock:
            goal = self._active
            snap = request.snapshot
            if (goal is None or not combat.combat_open(getattr(goal, "combat", None)) or goal.play_mode != "local_activity"
                    or not self._v2_pad_path() or self.curfew_window_now() and not goal.combat.escape
                    or not (request.reason in _CONTINUATION_REASONS or request.trigger_type == "game")
                    or goal.combat_direct >= self.COMBAT_DIRECT_MAX or goal.gather_progress is not None):
                if goal is not None and combat.combat_open(getattr(goal, "combat", None)):
                    self._combat_mines_handoff(goal, "continuation_unavailable")
                return None
            session = getattr(self, "_skill_session", None)
            run = getattr(session, "current", None)
            batch = run.state.get("mines_last_batch", {}) if run is not None else {}
            resuming = (run is not None and run.call.name == "lawan_mine" and run.goal_id == goal.goal_id
                        and batch.get("goal_id") == goal.goal_id and not batch.get("killed"))
            if resuming:
                from arti_runtime.mines_body import body_guard_running, note_body_busy
                if body_guard_running(self):
                    # ADR 183 (a74 B d2 J1172-J1177, A d2 J2665, A d4 J1286): no batch is offered while her guard
                    # holds the body; the fight waits for the guard's end instead of ending batch_not_offered.
                    note_body_busy(self, "lawan_mine", "combat_resume")
                    return self._skill_wait_json(request)
                checkpoint = (goal.goal_id, batch.get("batches"))
                if run.state.get("mines_batch_resume_attempt") == checkpoint:
                    # ADR 177 (a70 J2171-J2181): a proposed batch discarded at publication (not_taken, no request
                    # id) sent nothing; a fresh read may propose it again, bounded by RESUME_UNSENT_MAX.
                    objective = goal.combat
                    unsent = run.state.get("mines_batch_resume_sig") == (
                        len(objective.request_ids), objective.calls, objective.last_verdict)
                    retries = run.state.get("mines_batch_resume_unsent", 0)
                    if not unsent or retries >= self.RESUME_UNSENT_MAX:
                        self._combat_mines_handoff(goal, "previous_step_sent_no_batch")
                        return self._skill_wait_json(request)
                    run.state["mines_batch_resume_unsent"] = retries + 1
                    self._journal({"ev": "mines_combat_resume_retry", "goal_id": goal.goal_id,
                                   "reason": "proposal_not_settled", "n": retries + 1,
                                   "cap": self.RESUME_UNSENT_MAX})
                else:
                    run.state.pop("mines_batch_resume_unsent", None)
                run.state["mines_batch_resume_attempt"] = checkpoint
                run.state["mines_batch_resume_sig"] = (len(goal.combat.request_ids), goal.combat.calls,
                                                       goal.combat.last_verdict)
            if (not resuming and getattr(snap, "reflex_combat_target", None) is None and self._route_needs_rock(goal, snap)
                    and self._clear_path_start(goal, learned_only=True)):
                # FIXB14 D-B12-3: the approach walked into a breakable Stone (nav grid 's' at the learned tile): break it
                # now, before any other route or the aggro wait.
                raw = self._clear_path_invoke(request)
                if raw is not None:
                    return raw
            snap = self._combat_routable_snapshot(goal, snap)  # FIXB11 D-B8-1 (a): only monsters with a known route
            picked = combat.combat_auto_choice(goal.combat, snap, approach_progress=goal.approach_progress)
            if resuming and (picked is None or picked[0] != combat.OPERATION_COMBAT):
                self._combat_mines_handoff(goal, "batch_not_offered")
                return self._skill_wait_json(request)
            if picked is None:
                if self._clear_path_start(goal, learned_only=True) or (
                        goal.clear_path_rocks > 0 and self._clear_path_route_start(goal)):
                    # FIXB13 D-B10-1 (live V2B10ACC11a, Mines 3: every route pressed into rocks): the approach stopped
                    # at a rock -> break it before any aggro wait. FIXB21 D-B18-1: after a cleared route rock, the next
                    # stone of the re-planned route follows at once (no second 60 s wait).
                    raw = self._clear_path_invoke(request)
                    if raw is not None:
                        return raw
                if self._combat_wait_start(goal, request):
                    return self._rule_json(request, {"kind": "wait", "reason": "combat_wait_for_aggro", "ms": 0},
                                           RULE_COMBAT, "wait")
                return None
            operation, target_ref, args, label = picked
            session = getattr(self, "_skill_session", None)
            run = getattr(session, "current", None)
            if (operation == combat.OPERATION_COMBAT and getattr(session, "status", None) == "active"
                    and run is not None and run.goal_id == goal.goal_id and run.call.name == "lawan_mine"):
                # S15: settle a short native batch before retrying an ineffective
                # chase. GAP8E J2 L3354: with the 80% survival reserve as the
                # reflex floor, one hit at 85 HP started the greedy native retreat
                # (no BFS), which stuck with a 13/24-HP slime in contact (85 -> 66).
                # The batch now fights to the reflex's own floor and never
                # retreats; the skill's guard owns meals / exits between batches.
                from arti_stardew_skills_mines import FIGHT_FLOOR_PCT
                args = dict(args, max_s=4, hp_floor_pct=FIGHT_FLOOR_PCT, retreat=False)
            goal.combat_direct += 1
            self._journal({"ev": "combat_rule_direct", "goal_id": goal.goal_id, "generation_id": request.generation_id,
                           "operation": operation, "target": label, "count": goal.combat_direct,
                           **({"target_kind": (args or {}).get("target_kind")} if operation == combat.OPERATION_COMBAT else {})})
            choice = {"kind": "act", "operation": operation, "target_ref": target_ref, "args": dict(args or {}),
                      "evidence_refs": []}
            return self._rule_json(request, choice, RULE_COMBAT, label)

    def _menu_rule_finish(self, goal):
        """Under the lock: the deterministic menu-objective finish (verified open + verified close), or None."""
        from arti_stardew_runtime import OwnerTurnResult, StatusSpeech
        verdict = self._ui_pad_finish_verdict(goal)
        if verdict is None or verdict.status != "achieved":
            return None
        goal.last_finish = verdict
        goal.progress_marker = "terminal"
        goal.revision += 1
        self._pending_continuation = None
        self._pending_event_reason = None
        self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                       "verdict": asdict(verdict), "origin": RULE_MENU})
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                               speech=StatusSpeech("Menu sudah dibuka lalu ditutup; terverifikasi."), finish_evidence=verdict)

    def process_menu_rule(self) -> int:
        """Worker hook (live D-B1c-1): once the objective's menu is observed open, the runtime closes it with the
        offered ui_press B (same leased path as a model ui_press) and records the verified finish; a menu the model
        closed itself is finished the same way. Waits while anything is in flight; never races a model decision."""
        decision = request = goal = None
        try:
            with self._lock:
                goal = self._active
                objective = self._menu_rule_objective(goal)
                if (objective is None or self._action_busy or self._inflight_generation is not None
                        or self._results_pending or self._trial_result_pending()):
                    return 0
                if objective.closed:
                    finish = self._menu_rule_finish(goal)
                    if finish is None:
                        return 0
                else:
                    finish = None
                    if not self._menu_rule_press_ready(goal, objective) or not self._auto_act_left(goal, RULE_MENU):
                        return 0
                    request = self._begin_generation(text=goal.text, trigger_type="continuation", viewer_name=None,
                                                     reason=RULE_MENU, replace_goal=False)
                    args = menu_close_args(objective, request.snapshot.ui_pad_target)
                    if args is None:
                        # The live view allowed a close but the snapshot offers none (slot busy, owner rule): counted
                        # against the same budget so the runner's idle check cannot spin on it.
                        self._inflight_generation = None
                        key = self._auto_act_key(goal, RULE_MENU)
                        goal.auto_acts[key] = goal.auto_acts.get(key, 0) + 1
                        self._journal({"ev": "auto_act_refused", "goal_id": goal.goal_id, "rule": RULE_MENU,
                                       "operation": "ui_press", "reason": "menu_close_not_offered",
                                       "count": goal.auto_acts[key]})
                        return 0
                    decision = GameDecision(1, request.request_nonce, request.goal_id, request.goal_revision,
                                            request.context_ref, "", ActChoice("ui_press", None, args, ()))
                    self._action_busy = True
                    self._action_cancel.clear()
                    self._auto_act_take(goal, RULE_MENU, request, "ui_press",
                                        f"{args['receiver_token']}:{args['btn']}", replaces="no_model_close")
            if decision is None:
                self._emit_rule_result(RULE_MENU + "_finish", None, finish)
                return 1
            press = self._execute_ui_pad_decision(decision, request, goal, origin=RULE_MENU)
            with self._lock:
                finish = (self._menu_rule_finish(goal)
                          if self._active is goal and goal.progress_marker != "terminal" else None)
            self._emit_rule_result(RULE_MENU, request.generation_id, press)
            if finish is not None:
                self._emit_rule_result(RULE_MENU + "_finish", None, finish)
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "menu_rule_error", "error": type(exc).__name__})
            if decision is not None:
                with self._lock:
                    self._action_busy = False
                    if request is not None and self._inflight_generation == request.generation_id:
                        self._inflight_generation = None
            return 0

    def _emit_rule_result(self, reason: str, generation_id, result) -> None:
        if self._on_generation_result is not None and result is not None:
            self._on_generation_result(SimpleNamespace(reason=reason, generation_id=generation_id), result)
