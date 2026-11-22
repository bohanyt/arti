"""Primitives v2 WS-G1: pure helpers for the generic menu layer (ui_inspect / ui_nav / ui_press).

No IO, no provider, no command inbox. The mod is authoritative for everything here: it publishes the active
menu in ``ui.context`` ``ui_pad`` (context, binding pins, snapped component, labelled components, what each
button does or why it is refused) and refuses every "-" cell itself. This module only

* parses that block into :class:`UiPadView` (fail closed on any doubt),
* builds the model-facing view: labels / values / neighbour ids, NEVER coordinates (``bounds`` stay in
  :attr:`UiPadView.bounds`, runtime-only, PLAN 1.1 rule 9),
* derives the exact ui_nav / ui_press args from the published pins (the runtime never invents a target),
* parses the owner's G1 menu objectives ("[recorded remark omitted]", "[recorded remark omitted]",
  "[recorded remark omitted]") and computes their finish verdicts from verified effects only.

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G1 live witness.
"""
from __future__ import annotations

import re
import copy
from collections.abc import Mapping
from dataclasses import dataclass, field

import arti_stardew_command as command
import arti_stardew_economy as economy_mod
from arti_stardew_animal_purchase import facts as animal_purchase_facts
from arti_runtime.journal_read import parse as parse_journal
from arti_runtime.calendar_board import parse as parse_calendar

UI_PAD_MAX_AGE_S = 2.0
MAX_COMPONENTS = 512
MAX_LABEL = 80
MAX_TEXT = 4096

# Contexts whose menu a runtime routine owns (PLAN 5.3 rule 8): never offered to the model.
RUNTIME_OWNED_CONTEXTS = frozenset({"level_up", "level_up_profession", "shipping",
                                    # Live7: the character menu belongs to the buat_karakter runner
                                    # (scripts/m1_operator/buat_karakter.py), never to the play-time model.
                                    "char_custom"})
# DialogueBox contexts (the talk / eat / bed routines own them while their objective is active).
DIALOGUE_CONTEXTS = frozenset({"dialogue", "question", "npc_question"})

MODEL_GUIDANCE = (" Menu: is_main_player=false is ARTI farmhand, not a permission refusal; never control HOST."
                  " Owner authorization/native gates still apply. Copy example.args: request-local handle (expires); no pins."
                  " A pick, ui_nav, A drop, B close. nbr=up,right,down,left; published IDs.")
INVENTORY_GUIDANCE = (" ui_menu slots/held_item are current; held_item:null means empty hand."
                      " snapped:null: ui_nav initializes default snap, then follow published nbr IDs."
                      " After verified whole-stack drop, B close; don't restart the move.")
INVENTORY_MOVE_GUIDANCE = (" Match the requested item name to source label/QID before A pick, not any snapped item."
                           " inventory_move is mechanics, not completion proof; follow phase/route."
                           " Holding the verified picked stack: navigate destination; A at source puts it back."
                           " phase=conflict: don't navigate/drop an unverified stack; restore it only to its known original slot, then B when empty."
                           " phase=close: B now, no extra inspect or repick.")


def event_question_guidance(menu: Mapping, *, nav_available: bool = True) -> str:
    dialogue = ((menu.get("g3") or {}).get("event") or {}).get("dialogue") or {}
    if menu.get("context") != "event" or dialogue.get("answerable") is not True:
        return ""
    guidance = " Event objective delegates the in-game answer choice to you. Choose an in-game response yourself."
    if not nav_available:
        return guidance + " Use only the offered A/choice payload; native navigation refusal stays refused."
    return (guidance + " Use ui_nav before A in this trial, even if already snapped (move to your chosen response)."
            " snapped:null: ui_nav initializes the default response; read its fresh snap/pins in the next snapshot."
            " event_selection.navigation_verified=true: A answers the chosen snapped response; omit choice."
            " No extra inspect is needed when the next snapshot has fresh pins. Native refused choices stay refused.")


def event_selection_hint(offer: Mapping, last_action: Mapping | None) -> dict | None:
    """Keep existing verified trial-nav provenance when receipt samples compact.

    Staging has no trial last_action. A foreign receiver, failed/uncertain input
    or unconfirmed cleanup never supplies this hint. This adds no input gate.
    """
    menu = offer.get("view") or {}
    if not offer.get("nav") or not event_question_guidance(menu):
        return None
    action = last_action or {}
    before = (action.get("before") or {}).get("ui_nav") or {}
    after = (action.get("after") or {}).get("ui_nav") or {}
    pins = offer.get("pins") or {}
    verified = (action.get("lifecycle") == "completed" and action.get("cleanup_confirmed") is True
                and action.get("world_effect_status") == "observed_ui_nav" and after.get("effect") == "snapped"
                and before.get("context") == "event" and before.get("receiver_token") == pins.get("receiver_token")
                and after.get("result_receiver_token") == pins.get("receiver_token")
                and after.get("result_menu_type") == pins.get("menu_type") and menu.get("snapped") is not None)
    return {"navigation_verified": verified}


@dataclass(frozen=True)
class UiPadView:
    """One parsed ``ui_pad`` block. ``bounds`` is runtime-only (never rendered into a prompt)."""
    receiver_token: str
    menu_type: str
    context: str
    target_token: str
    ready: bool
    available: bool
    wait_reason: str | None
    snapped_id: int                       # -1 = nothing snapped
    snapped: dict | None
    components: tuple[dict, ...]          # {id, kind, label, nbr, value?} - no bounds
    components_total: int
    buttons: dict                         # button -> {"does": str} | {"refused": str}; "tab": {"tabs": [...]}
    page_type: str | None
    tab: int | None
    tab_name: str | None
    held_item: dict | None
    text: str | None
    options: tuple[dict, ...]
    context_generation: int
    bounds: tuple[tuple[int, int, int, int, int], ...] = field(default=(), repr=False)
    g3: dict | None = None                # WS-G3 sub-block (text_entry / number / ready_check / event), sanitized
    # Primitives v2 WS-G2: shop / crafting facts (stock rows, sell list with policy, recipes) and the player's gold.
    economy: dict | None = None
    money: int | None = None
    bundle: dict | None = None
    page_index: int = 0
    denylisted_ids: tuple[int, ...] = ()
    purchase_animals: dict | None = None
    question_key: str | None = None
    generic: bool = False
    state_digest: str | None = None
    component_state: tuple[str, ...] = ()
    hud: bool = False
    cursor_settle: dict | None = None
    ready_check: dict | None = None
    ambient_state: tuple[str, ...] = ()
    ui_safety: dict | None = None
    menu_items: tuple[dict, ...] = ()
    end_of_night_status: tuple[dict, ...] = ()
    passout_readiness: dict | None = None
    close_only: bool = False  # a bounded stale uncertain latch (PM-1 fix4 #1) or a wire-budget block (ADR 44)
    # PM-1 fix8 (CONTROLLER_CONTRACT_v1): native's published affordances, the dry run of its own arm admission
    # (list / none / pending; container entries carry their encoded wire args). None = a publisher without fix8.
    affordances: dict | None = None
    # PM-1 fix10b: native's readback-only identity probe (anchor hash, input_free, learned count); journal only.
    identity_probe: dict | None = None
    # PM-1 fix13b: a generic CraftingPage's own recipe facts (CraftingFacts.Page): visible recipe ids, makes, have/need.
    crafting: dict | None = None
    shop: dict | None = None
    menu_container: dict | None = None
    # FIX78B (ADR 138): a wire-budget close-only stub that also kept the menu's native primitive entries (container
    # transfers). Only that stub sets it; a latch's close-only view never offers them.
    kept_primitives: bool = False
    item_names: dict | None = None  # that stub's {item id: the shed slot row's label}, the game's own display name
    journal: dict | None = None  # ADR 184: native entry flags and bound generic control IDs
    # ADR 230 N1: an open calendar board's cells ((day, marked, the cell's own display string), ...). Runtime-only:
    # never in model_view; one cell's string reaches her page only when she reads that cell (calendar_board).
    calendar_days: tuple = field(default=(), repr=False)
    named_item_args: bool = field(default=False, repr=False)

    def decision_view(self) -> dict:
        """Semantic comparison: native-learned ambient values are visible but do not fence input."""
        view = self.model_view()
        view.pop("ambient_state", None)
        return view

    def press_allowed(self, btn: str) -> bool:
        if self.affordances is not None:
            return btn in published_buttons(self.affordances)
        cell = self.buttons.get(btn)
        return isinstance(cell, Mapping) and isinstance(cell.get("does"), str)

    @property
    def nav_allowed(self) -> bool:
        return self.press_allowed("stick")

    @property
    def tabs(self) -> tuple[str, ...]:
        cell = self.buttons.get("tab")
        tabs = cell.get("tabs") if isinstance(cell, Mapping) else None
        return tuple(t for t in tabs if t in command.UI_PAD_TAB_NAMES) if isinstance(tabs, (list, tuple)) else ()

    def pins(self) -> dict:
        return {"receiver_token": self.receiver_token, "menu_type": self.menu_type, "context": self.context,
                "target_token": self.target_token}

    def nav_args(self, direction: str) -> dict:
        args = dict(self.pins(), dir=direction)
        command.validate_ui_nav_args(args)
        return args

    def press_args(self, btn: str, tab: str | None = None, **reserved: str) -> dict:
        """WS-G3: ``reserved`` = the payload a G3 context consumes (text / value / choice / festival / slot / item),
        always sent as strings (the mod echoes them as strings)."""
        args = dict(self.pins(), btn=btn, snapped_id=self.snapped_id)
        if tab is not None:
            args["tab"] = tab
        # WS-G2 qty / half / stack keep their JSON type; every G3 payload travels as a string.
        args.update({k: (v if k in ("qty", "half", "stack", "shift", "settle") else str(v)) for k, v in reserved.items()})
        command.validate_ui_press_args(args)
        return args

    def press_variants(self, btn: str, **reserved):
        """Enumerate only variants admitted by the current context's command schema."""
        for shift in (False, True) if reserved else (False,):
            try:
                args = self.press_args(btn, **reserved, **({"shift": True} if shift else {}))
            except (ValueError, command.KesalahanPerintah):
                continue  # a refused variant cannot hide other controls
            yield shift, args

    def model_view(self, focus: Mapping | None = None) -> dict:
        """What the model sees: labels, values, neighbour ids; never coordinates.

        Economy menus (shop / crafting) are compacted (D-B2-2, live batch 2: 13.9 KB shop block > the provider input
        bound): the snapped component, its neighbours and the objective's item (``focus``), a bounded window of the
        rest, and counts for what is omitted. Non-economy menus are unchanged."""
        components = list(self.components)
        goal_ids: list = []
        if self.economy is not None:
            components, goal_ids = economy_mod.compact_components(components, self.snapped, focus)
            if self.economy.get("kind") == "crafting":
                # An existing output stack in the bag is not a recipe to X.
                recipe_ids = {r["id"] for r in self.economy.get("recipes", ())
                              if economy_mod._is_focus(r, focus)}
                goal_ids = [cid for cid in goal_ids if cid in recipe_ids]
        view = {"menu_type": self.menu_type, "context": self.context, "components": components,
                "components_total": self.components_total, "snapped": self.snapped}
        if self.generic:
            view["generic"] = True
            view["hud"] = self.hud
            view["state_digest"] = self.state_digest
            view["component_state"] = list(self.component_state)
            view["ambient_state"] = list(self.ambient_state)
            view["ui_safety"] = copy.deepcopy(self.ui_safety)
            view["menu_items"] = copy.deepcopy(list(self.menu_items))
            view["end_of_night_status"] = copy.deepcopy(list(self.end_of_night_status))
            view["passout_readiness"] = copy.deepcopy(self.passout_readiness)
            if self.cursor_settle is not None:
                view["cursor_settle"] = copy.deepcopy(self.cursor_settle)
        if self.shop is not None:
            view["shop"] = copy.deepcopy(self.shop)
        if self.menu_container is not None:
            view["menu_container"] = copy.deepcopy(self.menu_container)
        if self.journal is not None:
            view["journal"] = copy.deepcopy(self.journal)
        if self.ready_check is not None:
            view["ready_check"] = copy.deepcopy(self.ready_check)
        if goal_ids:
            view["goal_component_ids"] = goal_ids
        for key, value in (("page_type", self.page_type), ("tab_name", self.tab_name), ("held_item", self.held_item),
                           ("text", self.text), ("wait_reason", self.wait_reason)):
            if value is not None:
                view[key] = value
        if self.page_type == "InventoryPage":
            # Native ReadUiPadFacts reads CursorSlotItem even when it is null.
            # W5 G4-b completed the drop but omitted null looked like unknown
            # under the provider's omissions rule, inviting repeated inspects.
            view["held_item"] = self.held_item
        if self.options:
            view["options"] = list(self.options)
        if self.purchase_animals is not None:
            view["purchase_animals"] = dict(self.purchase_animals, buildings=[
                {k: v for k, v in b.items() if k not in ("x", "y", "width", "height")}
                for b in self.purchase_animals["buildings"]])
        if self.g3:
            view["g3"] = self.g3
        if self.economy is not None:
            view["economy"] = economy_mod.compact_economy(self.economy, self.snapped_id, focus)
            if self.economy.get("kind") == "crafting":
                view["page_index"] = self.page_index
                matches = [r for r in self.economy.get("recipes", ()) if economy_mod._is_focus(r, focus)]
                view["recipe_match_status"] = ("known" if any(r["known"] for r in matches)
                                               else "unknown" if matches else "absent") if focus else "unfocused"
                view["page_controls"] = [{"id": c["id"], "action": "previous" if c["id"] == 88 else "next"}
                                         for c in self.components if c["id"] in (88, 89)]
                view["crafting_hint"] = "Recipe absent on this page: navigate to a page control, A change page, inspect, navigate to recipe, X craft one."
        if self.bundle is not None:
            view["bundle"] = self.bundle
            # Keep navigation and donation targets under the unchanged provider bound.
            ids = [self.snapped_id] + self.bundle["accepting_slots"] + self.bundle["empty_slots"]
            ids += [n for c in self.components if c["id"] == self.snapped_id for n in c["nbr"]]
            ids += [n for i in self.bundle["ingredients"] for n in i["matching_slots"]]
            ids = list(dict.fromkeys(ids))
            by_id = {c["id"]: c for c in self.components}
            priority = [by_id[n] for n in ids if n in by_id]
            view["components"] = (priority + [c for c in self.components if c["id"] not in ids])[:12]
            view["donation_hint"] = "A pick matching inventory slot, navigate by nbr to accepting slot, A donate, B back, B close."
        if self.bundle is not None or (self.economy or {}).get("kind") == "crafting":
            view["held_item"] = self.held_item
            view["denylisted_ids"] = list(self.denylisted_ids)
            targets = (self.bundle["accepting_slots"] if self.held_item else
                       [n for i in self.bundle["ingredients"] for n in i["matching_slots"]]) if self.bundle else goal_ids
            if not self.bundle and not goal_ids:
                # Use only native page controls. No guessed recipes or tab IDs.
                controls = {c["id"] for c in self.components} - set(self.denylisted_ids)
                # Scan forward before returning to an earlier page. Preferring
                # previous on every nonzero page made B19 bounce 0 -> 1 -> 0.
                page_target = 89 if 89 in controls else 88 if self.page_index > 0 and 88 in controls else None
                if page_target is not None:
                    view["page_target_id"] = page_target
                    targets = [page_target]
            route = menu_route(self.components, self.snapped_id, targets, self.denylisted_ids)
            if route:
                view["navigation_route"] = route
                keep = {self.snapped_id} | {s["from"] for s in route} | {s["to"] for s in route}
                # Both render stages preserve this published path, including recipe nodes.
                view["route_component_ids"] = sorted(keep)
                by_id = {c["id"]: c for c in self.components}
                view["components"] = [by_id[n] for n in sorted(keep) if n in by_id] + [
                    c for c in view["components"] if c["id"] not in keep]
                view["components"] = view["components"][:max(12, len(keep))]
            snap = next((c for c in self.components if c["id"] == self.snapped_id), {})
            view["navigation"] = {direction: {"to": n, **({"refused": "denylisted_target"}
                if n in self.denylisted_ids else {})} for direction, n in zip(DIRECTIONS, snap.get("nbr", ())) }
            if self.bundle:
                view["donation_hint"] += " With held_item, follow navigation_route to an accepting_slots ID; up into bag rows does not donate."
            else:
                view["crafting_hint"] = ("Goal recipe present: follow navigation_route, re-read pins, X on its recipe ID crafts one."
                    if goal_ids else "Recipe absent on this page: A on previous/next page control once, re-read page_index and recipes.")
        return view


def _inventory_move_match(owner_text: str):
    return re.search(r"\bmove the entire (.+?) stack from slot (\d{1,2}) to empty slot (\d{1,2})\b",
                     owner_text or "", re.IGNORECASE)


def _inventory_pickup_matches(pickup: Mapping | None, menu: Mapping, owner_text: str,
                              receiver_token: str | None) -> bool:
    match = _inventory_move_match(owner_text)
    if match is None or not isinstance(pickup, Mapping) or not receiver_token:
        return False
    name, src, dst = match.groups()
    value, held = pickup.get("value") or {}, menu.get("held_item") or {}
    return (pickup.get("receiver_token") == receiver_token and bool(pickup.get("request_id"))
            and str(pickup.get("requested_item", "")).casefold() == name.casefold()
            and (pickup.get("source"), pickup.get("destination")) == (int(src), int(dst))
            and isinstance(value.get("item"), str) and type(value.get("stack")) is int and value["stack"] > 0
            and type(value.get("quality")) is int
            and held.get("item") == value["item"] and held.get("stack") == value["stack"]
            and ("quality" not in held or held["quality"] == value["quality"]))


def inventory_pickup_from_action(action: Mapping | None) -> dict | None:
    action = action or {}
    if (action.get("lifecycle") != "completed" or action.get("cleanup_confirmed") is not True
            or action.get("world_effect_status") not in {"observed_ui_press", "observed_ui_nav", "observed_ui_inspect"}):
        return None
    return (action.get("after") or {}).get("inventory_pickup")


def inventory_move_hint(menu: Mapping, owner_text: str, pickup: Mapping | None = None,
                        *, receiver_token: str | None = None) -> dict | None:
    """Current slot mechanics for an explicit goal, never an action grant/effect oracle.

    No inferred QID, geometry or invented edge. A held stack
    needs the verified pickup receipt; matching names alone proves no
    conservation. Missing/conflicting slots request inspection, never a swap.
    """
    match = _inventory_move_match(owner_text)
    if menu.get("page_type") != "InventoryPage" or match is None:
        return None
    name, src, dst = match.groups()
    src, dst = int(src), int(dst)
    hint = dict(requested_item=name, source=src, destination=dst, phase="conflict")
    rows = {c["id"]: c for c in menu.get("components", ()) if c.get("kind") == "slot"}
    if src == dst or src not in rows or dst not in rows or "held_item" not in menu:
        return hint
    source, destination, held = rows[src], rows[dst], menu["held_item"]
    named = lambda row: row.get("label", "").casefold() == name.casefold() and bool(row.get("value"))
    if held is None and not source.get("value") and named(destination):
        hint["phase"] = "close"  # observed arrangement only; no achieved verdict
        return hint
    if held is None and named(source) and not destination.get("value"):
        target, phase = src, "pick"
    elif held is not None and not source.get("value") and not destination.get("value"):
        if not _inventory_pickup_matches(pickup, menu, owner_text, receiver_token):
            return hint
        target, phase = dst, "drop"  # only proceed with the verified requested pickup
    else:
        return hint
    hint["target"] = target
    start = (menu.get("snapped") or {}).get("id")
    if start is None:
        hint["phase"] = "initialize_snap"
    elif start == target:
        hint["phase"] = phase
    else:
        hint["phase"] = "navigate_source" if held is None else "navigate_destination"
        # Read-only shortest path using only the published neighbour graph.
        # Components are bounded by MAX_COMPONENTS; unknown edges stay unknown.
        graph = {c["id"]: c for c in menu.get("components", ())}
        todo, seen = [(start, [])], {start}
        for cid, route in todo:
            if cid == target:
                hint["route"] = route
                break
            for direction, neighbour in zip(("up", "right", "down", "left"), graph.get(cid, {}).get("nbr", ())):
                if neighbour in graph and neighbour not in seen:
                    seen.add(neighbour)
                    todo.append((neighbour, route + [direction]))
        if "route" not in hint:
            hint["phase"] = "route_unknown"
    return hint


def inventory_pickup_evidence(before: UiPadView | None, after: UiPadView | None, owner_text: str,
                              operation: str, args: Mapping, action: Mapping,
                              previous: Mapping | None = None) -> dict | None:
    """Receipt-local whole-stack provenance, carried only through verified nav/inspect.

    Native held_item publishes QID/count, not quality. Quality comes from the
    verified source pickup and survives only non-mutating same-receiver steps.
    A swap/drop, missing view, failed cleanup or changed held stack retires it.
    This is guidance evidence, never a completion oracle or an input grant.
    """
    if (after is None or after.page_type != "InventoryPage" or action.get("lifecycle") != "completed"
            or action.get("cleanup_confirmed") is not True
            or action.get("world_effect_status") != "observed_" + operation):
        return None
    menu = after.model_view()
    if operation == "ui_press":
        if (before is None or before.receiver_token != after.receiver_token or args.get("btn") != "A"
                or any(k in args for k in ("half", "stack", "qty"))
                or args.get("snapped_id") != before.snapped_id
                or ((action.get("after") or {}).get(operation) or {}).get("effect") != "changed"):
            return None
        hint = inventory_move_hint(before.model_view(), owner_text)
        if hint is None or hint["phase"] != "pick":
            return None
        source = next(c for c in before.components if c["id"] == hint["source"])
        pickup = {k: hint[k] for k in ("requested_item", "source", "destination")}
        pickup.update(receiver_token=after.receiver_token, request_id=action.get("request_id"),
                      value=dict(source["value"]))
    elif operation in ("ui_nav", "ui_inspect"):
        if operation == "ui_nav" and (before is None or before.receiver_token != after.receiver_token
                or ((action.get("after") or {}).get(operation) or {}).get("effect") != "snapped"):
            return None
        pickup = inventory_pickup_from_action(previous)
    else:
        return None
    if not _inventory_pickup_matches(pickup, menu, owner_text, after.receiver_token):
        return None
    rows = {c["id"]: c for c in after.components if c.get("kind") == "slot"}
    if any(n not in rows or rows[n].get("value") for n in (pickup["source"], pickup["destination"])):
        return None
    return dict(pickup, value=dict(pickup["value"]))


DIRECTIONS = ("up", "right", "down", "left")


def menu_route(components, start: int, targets, denied=()) -> list[dict]:
    """Shortest path over published concrete edges; no inferred sentinel edges."""
    rows = {c["id"]: c for c in components if c["id"] not in denied}
    if start not in rows or start in targets:
        return []
    pending, seen = [(start, [])], {start}
    for cid, path in pending:
        if cid in targets:
            return path
        for direction, neighbour in zip(DIRECTIONS, rows[cid]["nbr"]):
            if neighbour in rows and neighbour not in seen:
                seen.add(neighbour)
                pending.append((neighbour, path + [{"from": cid, "dir": direction, "to": neighbour}]))
    return []


def inventory_move_keep_ids(menu: Mapping, owner_text: str) -> set[int]:
    """Protect requested inventory slots and a path through published neighbours.

    Presentation only: this recognizes explicit whole-stack slot instructions;
    it does not authorize, plan inputs, bind an objective or invent menu edges.
    """
    if menu.get("page_type") != "InventoryPage":
        return set()
    rows = {c["id"]: c for c in menu.get("components", ()) if isinstance(c, Mapping) and type(c.get("id")) is int}
    start = (menu.get("snapped") or {}).get("id", 0)
    keep = {start} & rows.keys()
    keep.update(n for n in rows.get(start, {}).get("nbr", ()) if n in rows)
    match = _inventory_move_match(owner_text)
    if match is None:
        return keep
    source, destination = map(int, match.groups()[1:])
    keep.update(cid for cid in (source, destination) if rows.get(cid, {}).get("kind") == "slot")
    target = destination if menu.get("held_item") is not None else source
    if start not in rows or target not in keep:
        return keep
    paths = [(start, [start])]
    seen = {start}
    for cid, path in paths:
        if cid == target:
            keep.update(path)
            break
        for neighbour in rows[cid].get("nbr", ()):
            if neighbour in rows and neighbour not in seen:
                seen.add(neighbour)
                paths.append((neighbour, path + [neighbour]))
    return keep


def _bounded(value: object, limit: int = 128) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _int(value: object) -> bool:
    return type(value) is int


def _value(raw: object) -> dict | None:
    if raw is None:
        return None
    if (not isinstance(raw, Mapping) or not _bounded(raw.get("item")) or not _int(raw.get("stack"))
            or not _int(raw.get("quality"))):
        raise ValueError("value")
    return {"item": raw["item"], "stack": raw["stack"], "quality": raw["quality"]}


_G3_STR, _G3_INT, _G3_BOOL = (str,), (int,), (bool,)
_G3_SHAPES = {
    "text_entry": {"title": _G3_STR, "text": _G3_STR, "min_length": _G3_INT, "max_length": _G3_INT, "keyboard_open": _G3_BOOL},
    "number": {"message": _G3_STR, "min": _G3_INT, "max": _G3_INT, "value": _G3_INT, "price": _G3_INT, "money": _G3_INT},
    "ready_check": {"kind": _G3_STR, "check": _G3_STR, "ready_count": _G3_INT, "needed": _G3_INT, "cancelable": _G3_BOOL,
                    "initiated_by_arti": _G3_BOOL, "policy": _G3_STR, "wait_reason": _G3_STR, "auto_ready": _G3_BOOL},
    "event": {"id": _G3_STR, "festival": _G3_BOOL, "skippable": _G3_BOOL, "skip": _G3_STR, "player_control": _G3_BOOL,
              "festival_id": _G3_STR, "command": _G3_STR, "command_index": _G3_INT, "previous_answer": _G3_INT,
              "festival_menu": _G3_STR},
}
_G3_DIALOGUE = {"kind": _G3_STR, "text": _G3_STR, "ready": _G3_BOOL, "wait_reason": _G3_STR, "question_key": _G3_STR,
                "question_class": _G3_STR, "answerable": _G3_BOOL, "selected_response": _G3_INT}


def _typed(value: object, kinds: tuple) -> bool:
    if kinds is _G3_INT:
        return type(value) is int
    if kinds is _G3_BOOL:
        return type(value) is bool
    return isinstance(value, str) and len(value) <= MAX_TEXT


def _g3_section(raw: object, shape: dict) -> dict:
    if not isinstance(raw, Mapping) or any(k not in shape or not _typed(v, shape[k]) for k, v in raw.items()):
        raise ValueError("g3")
    return dict(raw)


def baca_g3(raw: object) -> dict | None:
    """WS-G3 ``g3`` sub-block / top-level ``ui_event`` / ``ready_check`` shapes (fail closed: ValueError)."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("g3")
    out: dict = {}
    for name, section in raw.items():
        if name not in _G3_SHAPES:
            raise ValueError("g3")
        sub = dict(section) if isinstance(section, Mapping) else None
        dialogue = sub.pop("dialogue", None) if sub is not None else None
        out[name] = _g3_section(sub, _G3_SHAPES[name])
        if dialogue is not None:
            if name != "event" or not isinstance(dialogue, Mapping):
                raise ValueError("g3")
            d = dict(dialogue)
            choices = d.pop("choices", None)
            festival_owner = d.pop("festival_owner", None)
            out[name]["dialogue"] = _g3_section(d, _G3_DIALOGUE)
            if festival_owner is not None:
                shape = {"lifecycle_id": _G3_STR, "command_index": _G3_INT,
                         "command_hash": _G3_STR, "dialogue_id": _G3_STR}
                if not isinstance(festival_owner, Mapping) or set(festival_owner) != set(shape):
                    raise ValueError("g3")
                out[name]["dialogue"]["festival_owner"] = _g3_section(festival_owner, shape)
            if choices is not None:
                if not isinstance(choices, (list, tuple)) or len(choices) > 8:
                    raise ValueError("g3")
                parsed = []
                for i, c in enumerate(choices):
                    if not isinstance(c, Mapping) or c.get("index") != i or not isinstance(c.get("label"), str):
                        raise ValueError("g3")
                    parsed.append({"index": i, "label": c["label"][:MAX_LABEL]})
                out[name]["dialogue"]["choices"] = parsed
    return out


def baca_ui_pad(facts: object, *, age_s: float | None, authoritative: bool = True,
                complete: bool = True) -> UiPadView | None:
    """ui.context facts -> UiPadView, or None on any doubt (stale, partial, receiver mismatch, bad shapes)."""
    if (not isinstance(facts, Mapping) or not authoritative or not complete or age_s is None
            or not 0 <= age_s <= UI_PAD_MAX_AGE_S):
        return None
    block, interactive, caps = facts.get("ui_pad"), facts.get("interactive"), facts.get("capabilities")
    if not isinstance(block, Mapping) or not isinstance(caps, Mapping):
        return None
    hud = block.get("generic") is True and block.get("hud") is True and block.get("menu_type") == "HUD"
    if hud:
        if interactive is not None:
            return None  # a nonmodal HUD cannot replace a real modal receiver
        interactive = {"receiver_token": block.get("receiver_token"), "menu_type": "HUD"}
    elif not isinstance(interactive, Mapping):
        return None
    capability = caps.get("ui_pad")
    generation = facts.get("context_generation")
    receiver, menu_type, context = block.get("receiver_token"), block.get("menu_type"), block.get("context")
    token, ready = block.get("target_token"), block.get("ready")
    if (not _bounded(receiver) or not receiver.startswith("rcv:") or receiver != interactive.get("receiver_token")
            or not _bounded(menu_type) or menu_type != interactive.get("menu_type")
            or context not in command.UI_PAD_CONTEXTS or not command.ui_pad_token_shape(token)
            or type(ready) is not bool or not isinstance(capability, Mapping)
            or type(capability.get("available")) is not bool or not _int(generation) or generation < 1):
        return None
    wait = block.get("wait_reason")
    if wait is not None and not _bounded(wait, 128):
        return None
    try:
        components: list[dict] = []
        bounds: list[tuple[int, int, int, int, int]] = []
        raw_components = block.get("components")
        compact = block.get("generic") is True and block.get("components_compact") is True
        if not isinstance(raw_components, (list, tuple)) or len(raw_components) > (MAX_COMPONENTS if block.get("generic") is True else 48):
            return None
        for raw in raw_components:
            if (not isinstance(raw, Mapping) or not _int(raw.get("id")) or not _bounded(raw.get("kind"), 32)
                    or not isinstance(raw.get("label"), str) or len(raw["label"]) > MAX_LABEL):
                return None
            nbr, box = raw.get("nbr"), raw.get("bounds")
            if compact:
                # FIX78B (ADR 138): a generic block over its wire budget drops geometry first; an absent row means
                # no neighbour (-1) and no bounds, never a guessed one.
                nbr = [-1, -1, -1, -1] if nbr is None else nbr
            if (not isinstance(nbr, (list, tuple)) or len(nbr) != 4 or not all(_int(n) for n in nbr)
                    or not (compact and box is None)
                    and (not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_int(n) for n in box))):
                return None
            item = {"id": raw["id"], "kind": raw["kind"], "label": raw["label"] or "(unlabelled)", "nbr": list(nbr)}
            value = _value(raw.get("value"))
            if value is not None:
                item["value"] = value
            if block.get("generic") is True:
                if box is not None:
                    item["bounds"] = list(box)
                if "visible" in raw:
                    if type(raw["visible"]) is not bool:
                        return None
                    item["visible"] = raw["visible"]
                if "accepts_item" in raw:
                    if type(raw["accepts_item"]) is not bool:
                        return None
                    item["accepts_item"] = raw["accepts_item"]
                if "protected_item" in raw:
                    if type(raw["protected_item"]) is not bool:
                        return None
                    item["protected_item"] = raw["protected_item"]
                for detail, limit in (("hover_text", 512), ("field_path", 256), ("text_value", 128)):
                    if detail in raw:
                        if not isinstance(raw[detail], str) or len(raw[detail]) > limit:
                            return None
                        item[detail] = raw[detail]
            components.append(item)
            if box is not None:
                bounds.append((raw["id"], *box))
        total = block.get("components_total")
        if not _int(total) or total < len(components):
            return None
        snapped_raw = block.get("snapped")
        snapped, snapped_id = None, -1
        if isinstance(snapped_raw, Mapping) and snapped_raw:
            if not _int(snapped_raw.get("id")) or not isinstance(snapped_raw.get("label"), str):
                return None
            snapped_id = snapped_raw["id"]
            snapped = {"id": snapped_id, "kind": snapped_raw.get("kind"), "label": snapped_raw["label"][:MAX_LABEL]}
            value = _value(snapped_raw.get("value"))
            if value is not None:
                snapped["value"] = value
        elif snapped_raw is not None and not isinstance(snapped_raw, Mapping):
            return None
        buttons_raw = block.get("buttons")
        if not isinstance(buttons_raw, Mapping):
            return None
        buttons: dict = {}
        for name, cell in buttons_raw.items():
            if not isinstance(name, str) or not isinstance(cell, Mapping):
                return None
            if name == "tab":
                tabs = cell.get("tabs")
                if not isinstance(tabs, (list, tuple)) or not all(t in command.UI_PAD_TAB_NAMES for t in tabs):
                    return None
                buttons[name] = {"tabs": list(tabs)}
            elif isinstance(cell.get("does"), str) and len(cell["does"]) <= 128:
                buttons[name] = {"does": cell["does"]}
            elif _bounded(cell.get("refused"), 128):
                buttons[name] = {"refused": cell["refused"]}
            else:
                return None
        held = block.get("held_item")
        if held is not None and (not isinstance(held, Mapping) or not _bounded(held.get("item")) or not _int(held.get("stack"))):
            return None
        options = []
        for i, opt in enumerate(block.get("options") or ()):
            if not isinstance(opt, Mapping) or opt.get("index") != i or not isinstance(opt.get("label"), str):
                return None
            options.append({"index": i, "label": opt["label"][:MAX_LABEL]})
            if isinstance(opt.get("response_key"), str):
                options[-1]["response_key"] = opt["response_key"][:MAX_LABEL]
        text = block.get("text")
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
            return None
        tab, tab_name, page = block.get("tab"), block.get("tab_name"), block.get("page_type")
        if (tab is not None and not _int(tab)) or (tab_name is not None and tab_name not in command.UI_PAD_TAB_NAMES) or (
                page is not None and not _bounded(page, 64)):
            return None
        g3 = baca_g3(block.get("g3"))
        economy = _economy(block.get("economy"))
        from scripts.live7.transactions import parse_shop
        shop = parse_shop(block.get("shop"))
        purchase = animal_purchase_facts(block.get("purchase_animals"))
        bundle = _bundle(block.get("bundle"))
        if bundle is not None and context != "junimo_note":
            return None
        page_index = block.get("page_index", 0)
        if not _int(page_index) or page_index < 0:
            return None
        denied = block.get("denylisted_ids", [])
        if not isinstance(denied, (list, tuple)) or len(denied) > MAX_COMPONENTS or any(not _int(n) for n in denied):
            return None
        money = block.get("money")
        if money is not None and not _int(money):
            return None
    except ValueError:
        return None
    component_state = block.get("component_state", [])
    if (not isinstance(component_state, (list, tuple)) or len(component_state) > 128
            or any(not isinstance(value, str) or len(value) > 256 for value in component_state)):
        return None
    ambient = block.get("ambient_state", [])
    safety = block.get("ui_safety")
    menu_items = block.get("menu_items", [])
    night_status = block.get("end_of_night_status", [])
    passout = block.get("passout_readiness")
    if (not isinstance(ambient, (list, tuple)) or len(ambient) > 128
            or any(not isinstance(s, str) or len(s) > 256 for s in ambient)
            or safety is not None and not isinstance(safety, dict)
            or not isinstance(menu_items, list) or not all(isinstance(i, dict) for i in menu_items)
            or not isinstance(night_status, list) or not all(isinstance(i, dict) for i in night_status)
            or passout is not None and (not isinstance(passout, dict)
                or set(passout) != {"menu_clear", "event_clear", "can_move", "tool_clear", "location_request_clear"}
                or any(type(v) is not bool for v in passout.values()))):
        return None
    question_key = block.get("question_key")
    if question_key is not None and (context != "question" or not _bounded(question_key, MAX_LABEL)):
        return None
    from arti_stardew_cursor_item import parse_cursor_settle
    cursor_settle = parse_cursor_settle(block.get("cursor_settle"), {c["id"] for c in components})
    if block.get("cursor_settle") is not None and cursor_settle is None:
        return None
    readiness = block.get("ready_check")
    if readiness is not None:
        if (not isinstance(readiness, Mapping) or "local_ready" not in readiness or not _bounded(readiness.get("check"), 128)
                or (readiness.get("local_ready") is not None and type(readiness.get("local_ready")) is not bool)
                or readiness.get("local_state") not in ("Ready", "NotReady", "Locked", "unknown")
                or type(readiness.get("ready_count")) is not int or type(readiness.get("needed")) is not int
                or type(readiness.get("all_ready")) is not bool or type(readiness.get("cancelable")) is not bool
                or readiness.get("waiting_for") not in ("other_required_players", "required_players_or_network_confirmation", "none")
                or readiness.get("cancel_control") not in ("cancelButton; closing cancels local readiness", "none")
                or readiness.get("waiting_player_ids_known") is not False):
            return None
        readiness = {k: readiness[k] for k in ("check", "local_ready", "local_state", "ready_count", "needed",
            "all_ready", "cancelable", "waiting_for", "cancel_control", "waiting_player_ids_known")}
        from scripts.live7.ready_checks import enrich
        readiness = enrich(readiness, block["ready_check"])
        if readiness is None:
            return None
    affordances = _affordances(block.get("affordances"), facts, receiver)
    # PM-1 fix8: with a native publication, "available" is its own verdict (some control admitted now), not a
    # receiver-level readiness re-read: the 3b final TV page was not "ready" while its arm admitted Start.
    available = bool(affordances["list"]) if affordances is not None else ready and capability["available"] is True
    return UiPadView(receiver, menu_type, context, token, ready, available, wait,
                     snapped_id, snapped, tuple(components), total, buttons, page, tab, tab_name,
                     {"item": held["item"], "stack": held["stack"]} if held else None, text, tuple(options), generation,
                     tuple(bounds), g3, economy, money, bundle, page_index, tuple(denied), purchase, question_key,
                     block.get("generic") is True, block.get("state_digest") if isinstance(block.get("state_digest"), str) else None,
                     tuple(component_state), hud, cursor_settle, readiness, tuple(ambient), safety,
                     tuple(menu_items), tuple(night_status), passout, affordances=affordances,
                     identity_probe=_identity_probe(block.get("identity_probe")),
                     # ADR 44 (fix32): a block the mod reduced to fit the wire offers only leaving the menu.
                     close_only=block.get("budget_omitted") is True,
                     kept_primitives=block.get("budget_omitted") is True and block.get("primitives_kept") is True,
                     item_names=_item_names(block.get("item_names")),
                     crafting=_crafting_page(block.get("crafting"), {c["id"] for c in components}), shop=shop,
                     journal=parse_journal(block.get("journal")),
                     calendar_days=parse_calendar(block.get("calendar_days")) if menu_type == "Billboard" else (),
                     menu_container=block.get("menu_container") if isinstance(block.get("menu_container"), dict)
                         and block["menu_container"].get("kind") in ("shipping_bin", "chest", "other") else None)


def _item_names(raw):
    """FIX78B (ADR 138): a primitives stub's names, bounded strings only; anything else reads as no names."""
    if not isinstance(raw, Mapping) or len(raw) > 96:
        return None
    names = {k: v for k, v in raw.items() if _bounded(k, 128) and _bounded(v, MAX_LABEL)}
    return names or None


def _crafting_page(raw, ids):
    """PM-1 fix13b: the native recipe rows [id, name, makes, n, state, needs, recipe] of visible components only."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("recipes"), list):
        return None
    rows = [list(r) for r in raw["recipes"] if isinstance(r, (list, tuple)) and len(r) >= 7 and r[0] in ids
            and all(isinstance(v, str) for v in (r[1], r[2], r[4], r[5], r[6])) and type(r[3]) is int]
    return dict(cooking=raw.get("cooking") is True, page=raw.get("page") if type(raw.get("page")) is int else 0,
                pages=raw.get("pages") if type(raw.get("pages")) is int else 1, recipes=rows)


def _identity_probe(probe):
    if (not isinstance(probe, Mapping) or set(probe) != {"anchor", "input_free", "input_inflight", "learned"}
            or not isinstance(probe["anchor"], str) or len(probe["anchor"]) > 16
            or type(probe["input_free"]) is not bool or type(probe["input_inflight"]) is not bool
            or type(probe["learned"]) is not int):
        return None
    return dict(probe)


AFFORDANCE_VERBS = ("choose", "scroll", "type", "transfer")


def published_buttons(affordances: Mapping) -> set:
    """Every pad button some published entry admits (plain, component variant, answer, settle, type)."""
    buttons = set()
    for entry in affordances.get("list", ()):
        control = entry.get("control")
        if isinstance(control, str) and control not in ("close", "stick"):
            buttons.add(control)
        elif isinstance(control, int):
            buttons.add(entry.get("button", "A"))
        buttons |= {"A" if b == "settle" else b[-1] for b in entry.get("buttons", ())}
        if entry.get("verb") == "type":
            buttons.add("Y")
        if control == "stick":
            buttons.add("stick")
    return buttons


def _affordances(raw: object, facts: Mapping, receiver: str) -> dict | None:
    """PM-1 fix8: native's affordances, rendered as published. Python only ENCODES a container entry's wire args from
    the same published container block; an entry it cannot encode is dropped as a wire defect, never re-judged."""
    if not isinstance(raw, Mapping) or raw.get("v") != 1 or not isinstance(raw.get("list"), list) or len(raw["list"]) > 512:
        return None
    box = facts.get("container") if isinstance(facts.get("container"), Mapping) else {}
    out, dropped = [], []
    for entry in raw["list"]:
        if not isinstance(entry, Mapping) or entry.get("verb") not in AFFORDANCE_VERBS:
            return None
        entry = copy.deepcopy(dict(entry))
        if entry.get("primitive") == "container_exit" or entry["verb"] == "transfer":
            args = {k: box.get(k) for k in ("receiver_token", "container_token", "source_fact_id", "readback_token")}
            try:
                if args["receiver_token"] != receiver:
                    raise ValueError("container_receiver")
                if entry["verb"] == "transfer":
                    side = box["container_slots" if entry.get("from") == "container" else "actor_slots"]
                    row = next(r for r in side if r.get("slot") == entry.get("slot"))
                    item = row["item"]
                    args.update(target_token=row["target_token"], slot=row["slot"], qualified_item_id=item["qualified_item_id"],
                                stack=item["stack"], quality=item["quality"], owner_ceiling=item["stack"])
                    if entry.get("mode") == "put_one":
                        args["quantity"] = 1
                    elif entry.get("mode") in ("quick", "take_out"):
                        args["mode"] = entry["mode"]
                    command.validate_container_transfer_args(args)
                else:
                    args["target_token"] = box["close_target"]["target_token"]
                    command.validate_container_exit_args(args)
            except (KeyError, StopIteration, TypeError, ValueError, command.KesalahanPerintah) as exc:
                dropped.append(dict(entry=entry, reason=type(exc).__name__))
                continue
            entry["wire"] = args
        out.append(entry)
    result = {k: copy.deepcopy(raw[k]) for k in ("none", "pending") if k in raw}
    result["list"] = out
    if dropped:
        result["wire_defects"] = dropped
    return result


def _bundle(raw: object) -> dict | None:
    if raw is None:
        return None
    if (not isinstance(raw, Mapping) or not _int(raw.get("index")) or not _bounded(raw.get("name"), MAX_LABEL)
            or type(raw.get("deposits_allowed")) is not bool):
        raise ValueError("bundle")
    out = {k: raw[k] for k in ("index", "name", "deposits_allowed")}
    def ids(value):
        if not isinstance(value, (list, tuple)) or len(value) > 48 or not all(_int(v) and v >= 0 for v in value):
            raise ValueError("bundle_slots")
        return list(value)
    for key in ("empty_slots", "accepting_slots"):
        out[key] = ids(raw.get(key))
    if not set(out["accepting_slots"]) <= set(out["empty_slots"]):
        raise ValueError("bundle_accepting_slots")
    ingredients = raw.get("ingredients")
    if not isinstance(ingredients, (list, tuple)) or len(ingredients) > 16:
        raise ValueError("bundle_ingredients")
    out["ingredients"] = []
    for i, entry in enumerate(ingredients):
        if (not isinstance(entry, Mapping) or type(entry.get("index")) is not int or entry["index"] != i
                or not _bounded(entry.get("item")) or not _int(entry.get("need")) or entry["need"] < 1
                or not _int(entry.get("quality")) or entry["quality"] < 0 or type(entry.get("completed")) is not bool):
            raise ValueError("bundle_ingredient")
        out["ingredients"].append({k: entry[k] for k in ("index", "item", "need", "quality", "completed")}
                                 | {"matching_slots": ids(entry.get("matching_slots"))})
        persistent = entry.get("persistent_completed")
        if persistent is not None and type(persistent) is not bool:
            raise ValueError("bundle_persistent_completed")
        out["ingredients"][-1]["persistent_completed"] = persistent
        if "name" in entry:
            if not isinstance(entry["name"], str) or len(entry["name"]) > MAX_LABEL:
                raise ValueError("bundle_ingredient_name")
            out["ingredients"][-1]["name"] = entry["name"]
    return out


_ECONOMY_ROW = {"index": int, "id": int, "item": str, "name": str, "price": int, "stock": int, "fits": bool,
                "trade_item": str, "tool": bool, "capacity": int, "trade_count": int, "trade_have": int, "required_tool": str}
_ECONOMY_SELL = {"id": int, "slot": int, "item": str, "name": str, "stack": int, "quality": int, "unit_price": int,
                 "policy": str}
_ECONOMY_RECIPE = {"id": int, "name": str, "recipe": str, "item": str, "produces": int, "craftable": bool, "known": bool,
                   "max_crafts": int}


def _economy_typed(entry: object, schema: dict) -> dict:
    if not isinstance(entry, Mapping):
        raise ValueError("economy_entry")
    out = {}
    for key, kind in schema.items():
        if key not in entry:
            continue
        value = entry[key]
        if (kind is int and type(value) is not int) or (kind is bool and type(value) is not bool) or (
                kind is str and (not isinstance(value, str) or len(value) > 128)):
            raise ValueError("economy_" + key)
        out[key] = value
    return out


def _economy(raw: object) -> dict | None:
    """WS-G2 ``ui_pad.economy`` -> a typed copy (labels / prices / counts only), or None; ValueError on a bad shape."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or raw.get("kind") not in ("shop", "crafting") or not _int(raw.get("money")):
        raise ValueError("economy")
    out: dict = {"kind": raw["kind"], "money": raw["money"]}
    if raw["kind"] == "shop":
        rows, sell = raw.get("rows"), raw.get("sell")
        if not isinstance(rows, (list, tuple)) or not isinstance(sell, (list, tuple)) or len(rows) > 60 or len(sell) > 48:
            raise ValueError("economy_lists")
        out["rows"] = [_economy_typed(r, _ECONOMY_ROW) for r in rows]
        out["sell"] = [_economy_typed(r, _ECONOMY_SELL) for r in sell]
        for key, kind in (("shop_id", str), ("currency", int), ("ready", bool), ("tool_upgrading", str), ("rows_total", int), ("upgrade_allowed", bool)):
            if key in raw:
                if (kind is str and not isinstance(raw[key], str)) or (kind is int and not _int(raw[key])) or (
                        kind is bool and type(raw[key]) is not bool):
                    raise ValueError("economy_" + key)
                out[key] = raw[key]
    else:
        recipes = raw.get("recipes")
        if not isinstance(recipes, (list, tuple)) or len(recipes) > 60 or type(raw.get("cooking")) is not bool:
            raise ValueError("economy_recipes")
        out["cooking"] = raw["cooking"]
        out["recipes"] = []
        for r in recipes:
            entry = _economy_typed(r, _ECONOMY_RECIPE)
            ingredients = r.get("ingredients") if isinstance(r, Mapping) else None
            if not isinstance(ingredients, (list, tuple)) or len(ingredients) > 8:
                raise ValueError("economy_ingredients")
            entry["ingredients"] = [_economy_typed(i, {"item": str, "need": int, "have": int}) for i in ingredients]
            out["recipes"].append(entry)
    if raw.get("truncated") is True:
        out["truncated"] = True
    return out


def offer_payload(view: UiPadView, focus: Mapping | None = None) -> dict:
    """Snapshot entry for an offerable menu (runtime-filtered); labels only."""
    press = {name: cell["does"] for name, cell in sorted(view.buttons.items())
             if name not in ("stick", "tab") and isinstance(cell, Mapping) and "does" in cell and view.press_allowed(name)}
    return {"pins": view.pins(), "snapped_id": view.snapped_id, "view": view.model_view(focus),
            "nav": view.nav_allowed, "press": press, "tabs": list(view.tabs)}


def catalog_entries(offer: dict) -> dict:
    """supported_actions entries for ui_inspect / ui_nav / ui_press (syntax, not permission)."""
    entries = {"ui_inspect": {"target_ref": None, "args": "{}", "result_scope": "read menu; no input"}}
    if offer["nav"]:
        entries["ui_nav"] = {"target_ref": None, "observed_target": {"handle": "a1"},
                             "args": "dir: up|right|down|left; no destination_id"}
        navigation = offer["view"].get("navigation")
        if navigation:
            entries["ui_nav"]["directions"] = {d: v["to"] for d, v in navigation.items() if "refused" not in v}
            entries["ui_nav"]["args"] = "dir from directions; fresh handle each step"
        route = (offer.get("view") or {}).get("navigation_route") or []
        if route and published_route(offer, route[-1].get("to")):
            entries["ui_nav"]["destinations"] = [route[-1]["to"]]
            entries["ui_nav"]["args"] = "destination_id from destinations executes navigation_route; or dir for one step"
    if offer["press"] or offer["tabs"]:
        entries["ui_press"] = {"target_ref": None, "observed_target": {"handle": "a2"},
                               "buttons": offer["press"], "args": "btn from buttons"
                               + ("; btn tab + tab from tabs" if offer["tabs"] else "")
                               + (('; event question: ui_nav then use next snapshot fresh pins (no extra inspect); btn A answers the snapped response; omit choice'
                                   if offer["nav"] else '; event question: btn A with an offered choice string index')
                                   + '; optional choice is a string index from ui_menu.g3.event.dialogue.choices'
                                  if event_question_guidance(offer.get("view") or {}) else "")}
        if offer["tabs"]:
            entries["ui_press"]["tabs"] = offer["tabs"]
        if offer.get("payload"):
            # WS-G2: the exact payload a button needs on this row (e.g. {"A": {"qty": 3}}); none = send none.
            entries["ui_press"]["payload"] = offer["payload"]
            entries["ui_press"]["args"] += "; add the payload listed for btn"
        if offer.get("economy_goal"):
            entries["ui_press"]["economy_goal"] = offer["economy_goal"]
    # One exact choice per offered operation. Keep examples flat (the provider's
    # generic args schema is scalar); alternate buttons/payloads remain above.
    for operation, entry in entries.items():
        args = {}
        if operation == "ui_nav":
            args = {"handle": "a1"}
            if entry.get("destinations"):
                args["destination_id"] = entry["destinations"][0]
            else:
                args["dir"] = next(iter(entry.get("directions") or {}), "down")
        elif operation == "ui_press":
            buttons = offer["press"]
            btn = ("X" if "X" in buttons and (offer.get("economy_goal") or {}).get("objective") == "economy_craft"
                   else next(iter(buttons), "tab"))
            args = {"handle": "a2", "btn": btn}
            args.update((offer.get("payload") or {}).get(btn) or {})
            if btn == "tab":
                args["tab"] = offer["tabs"][0]
            dialogue = (((offer.get("view") or {}).get("g3") or {}).get("event") or {}).get("dialogue") or {}
            if btn == "A" and dialogue.get("answerable") is True and not offer["nav"]:
                choices = dialogue.get("choices") or []
                if choices:
                    args["choice"] = str(choices[0]["index"])
        entry["example"] = {"kind": "act", "operation": operation, "target_ref": None,
                            "args": args, "evidence_refs": []}
    return entries


def normalize_handle_args(operation: str, args: Mapping, target_ref=None) -> dict:
    """Syntax aliases only. Never choose/rebind a handle or fill native pins.

    Multiple selectors (even equal), mixed native pins, null/non-string selectors
    and nested extra keys are ambiguous and refused. Other operations keep their
    existing target contracts (e.g. NPC gift refs are not UI handles).
    """
    if operation not in ("ui_nav", "ui_press"):
        raise ValueError("ui_pad_handle_shape")
    selectors = [key for key in ("handle", "observed_target", "target") if key in args]
    if target_ref is not None:
        selectors.append("target_ref")
    if not selectors:
        return dict(args)
    if len(selectors) != 1:
        raise ValueError("ui_pad_handle_shape")
    if set(args) & (set(command.UI_PAD_BASE_ARGS) | {"snapped_id"}):
        raise ValueError("ui_pad_handle_not_offered")
    key = selectors[0]
    handle = target_ref if key == "target_ref" else args[key]
    if key == "observed_target" and isinstance(handle, Mapping):
        if set(handle) != {"handle"}:
            raise ValueError("ui_pad_handle_shape")
        handle = handle["handle"]
    if not isinstance(handle, str) or not handle:
        raise ValueError("ui_pad_handle_shape")
    return {**{k: v for k, v in args.items() if k not in ("handle", "observed_target", "target")}, "handle": handle}


def resolve_model_args(offer: Mapping, operation: str, args: dict) -> dict:
    """Expand a request-local handle from THIS immutable offer, never from a newer observation.

    The GameDecision nonce/context/revision fence rejects responses from other requests.
    Legacy full-pin callers still pass the same exact native gates. Mixed handle/pin
    payloads are refused so a model cannot override any server-bound pin or snap.
    """
    args = normalize_handle_args(operation, args)
    if "handle" not in args:
        return dict(args)
    expected = {"ui_nav": "a1", "ui_press": "a2"}.get(operation)
    if args["handle"] != expected or expected is None or set(args) & (set(command.UI_PAD_BASE_ARGS) | {"snapped_id"}):
        raise ValueError("ui_pad_handle_not_offered")
    if operation == "ui_nav" and not offer.get("nav"):
        raise ValueError("ui_nav_not_offered")
    if operation == "ui_press" and not (offer.get("press") or offer.get("tabs")):
        raise ValueError("ui_press_not_offered")
    resolved = dict(offer["pins"])
    if operation == "ui_press":
        resolved["snapped_id"] = offer["snapped_id"]
    resolved.update({k: v for k, v in args.items() if k != "handle"})
    return resolved


def published_route(offer: Mapping, destination) -> list[dict]:
    """Accept only the exact bounded route published in this offer, over concrete safe edges."""
    if type(destination) is not int or not offer.get("nav"):
        return []
    menu = offer.get("view") or {}
    route = menu.get("navigation_route") or []
    if not route or len(route) > MAX_COMPONENTS or route[-1].get("to") != destination:
        return []
    rows = {c["id"]: c for c in menu.get("components", ())}
    denied = menu.get("denylisted_ids") or []
    start, seen = offer["snapped_id"], {offer["snapped_id"]}
    for step in route:
        direction, target = step.get("dir"), step.get("to")
        if (step.get("from") != start or direction not in DIRECTIONS or start not in rows
                or target not in rows or start in denied or target in denied or target in seen
                or rows[start]["nbr"][DIRECTIONS.index(direction)] != target):
            return []
        seen.add(target)
        start = target
    return [dict(s) for s in route]


# ---------------------------------------------------------------------------- G1 menu objectives

OBJ_JOURNAL = "journal"
OBJ_MAIL = "mail"
OBJ_MENU = "menu"
TARGET_MENUS = {OBJ_JOURNAL: ("QuestLog",), OBJ_MAIL: ("LetterViewerMenu",), OBJ_MENU: ("GameMenu",)}
# The world pad button that opens the objective's menu (WS-G0 pad_button); the mailbox is not a G1 primitive.
OPEN_BUTTON = {OBJ_JOURNAL: "Select", OBJ_MENU: "Start"}
FINISH_MENU_CLOSED = "observed_menu_opened_and_closed"

_CLOSE = r"(?: (?:and|then|lalu|terus|dan) (?:close|tutup)(?: (?:it|lagi|kembali|again))?)?"


@dataclass
class MenuObjective:
    """One owner-authored G1 menu objective (authenticated owner text only, never model/scene text)."""
    kind: str
    opened: bool = False
    open_receiver: str | None = None
    closed: bool = False
    close_effect: str | None = None
    finish_recorded: bool = False


def parse_menu_objective(text: str) -> MenuObjective | None:
    t = re.sub(r"\s+", " ", (text or "").strip().rstrip(".!")).casefold()
    if re.fullmatch(r"(?:open|check|buka|cek|lihat) (?:the |my )?(?:journal|quest log|questlog|jurnal)" + _CLOSE, t):
        return MenuObjective(OBJ_JOURNAL)
    if re.fullmatch(r"(?:read|open|baca|buka) (?:the |my |a )?(?:mail|letter|surat)(?:nya)?" + _CLOSE, t):
        return MenuObjective(OBJ_MAIL)
    if re.fullmatch(r"(?:open|buka) (?:the |my )?(?:menu|game menu|inventory|inventori|tas)" + _CLOSE, t):
        return MenuObjective(OBJ_MENU)
    return None


def note_view(objective: MenuObjective | None, view: UiPadView | None) -> bool:
    """The objective's menu is observed open (fresh mod view). Returns True when progress changed."""
    if objective is None or view is None or objective.closed or objective.opened:
        return False
    if view.menu_type in TARGET_MENUS[objective.kind]:
        objective.opened = True
        objective.open_receiver = view.receiver_token
        return True
    return False


def note_close(objective: MenuObjective | None, *, menu_type_before: str | None, effect: str | None,
               verified: bool) -> bool:
    """A VERIFIED ui_press / ui_key close of the objective's menu (effect closed). True when progress changed."""
    if (objective is None or not objective.opened or objective.closed or not verified or effect != "closed"
            or menu_type_before not in TARGET_MENUS[objective.kind]):
        return False
    objective.closed = True
    objective.close_effect = effect
    return True


def objective_verdict(objective: MenuObjective) -> tuple[str, str]:
    if objective.opened and objective.closed:
        return "achieved", FINISH_MENU_CLOSED
    if not objective.opened:
        return "partial", "menu_not_observed_open"
    return "partial", "menu_not_closed_by_verified_press"


def objective_summary(objective: MenuObjective | None) -> dict | None:
    if objective is None:
        return None
    return {"objective": "menu_" + objective.kind, "target_menu": TARGET_MENUS[objective.kind][0],
            "opened": objective.opened, "closed": objective.closed}
