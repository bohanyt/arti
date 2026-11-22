"""Lossless native shop projection and receipt-backed, self-authored purchase intent.

No price, fit, vendor, itinerary or affordability decisions are made here.
"""
from collections.abc import Mapping
from copy import deepcopy
from types import SimpleNamespace


def _plain(value):
    """Native ui.context blocks are read-only Mapping/tuple; validation still wants JSON lists."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


FIELDS = ["index", "item", "name", "price", "stock", "stack", "capacity", "affordable", "control", "buy_quantities", "refused"]
SELL_FIELDS = ["control", "slot", "item", "name", "stack", "quality", "unit_price", "accepted"]


def parse_shop(raw):
    if raw is None:
        return None
    raw = _plain(raw)
    if not isinstance(raw, dict) or raw.get("stock_fields") != FIELDS or not isinstance(raw.get("stock"), list):
        raise ValueError("shop_catalog")
    for key in ("money", "currency", "balance", "bag_capacity", "bag_occupied"):
        if type(raw.get(key)) is not int:
            raise ValueError("shop_" + key)
    if not isinstance(raw.get("id"), str) or type(raw.get("ready")) is not bool:
        raise ValueError("shop_identity")
    indices, controls = set(), set()
    for row in raw["stock"]:
        if (not isinstance(row, list) or len(row) != len(FIELDS)
                or any(type(row[i]) is not int for i in (0, 3, 4, 5, 6))
                or not all(isinstance(row[i], str) for i in (1, 2))
                or type(row[7]) is not bool or row[8] is not None and type(row[8]) is not int
                or not isinstance(row[9], list) or any(type(n) is not int or n not in (1, 5, 25) for n in row[9])
                or row[10] is not None and not isinstance(row[10], str)
                or row[0] in indices or row[8] is not None and row[8] in controls):
            raise ValueError("shop_stock")
        indices.add(row[0])
        if row[8] is not None:
            controls.add(row[8])
    if "sell" in raw:
        if raw.get("sell_fields") != SELL_FIELDS or not isinstance(raw["sell"],list):
            raise ValueError("shop_sell_catalog")
        seen = set()
        for r in raw["sell"]:
            if (not isinstance(r,list) or len(r)!=8 or any(type(r[i]) is not int or r[i]<0 for i in (0,1,4,5,6))
                    or not all(isinstance(r[i],str) for i in (2,3)) or type(r[7]) is not bool
                    or r[0] in seen or r[5] not in (0,1,2,4)):
                raise ValueError("shop_sell_catalog")
            seen.add(r[0])
    return deepcopy(raw)


def purchase_holder(session, goal, view=None):
    """Existing run scratch, or one session/goal-bound UI intent when a shop was already open.
    This records her choice only; it creates no skill, world goal, input or permission.
    """
    if session.current is not None:
        return session.current
    scope = (getattr(session, "session_id", None), getattr(goal, "goal_id", None), getattr(goal, "revision", None))
    previous = getattr(session, "_ui_purchase", None)
    if previous is None or previous.scope != scope:
        previous = SimpleNamespace(scope=scope, call=SimpleNamespace(name="ui_action", arg=None, n=1), state={})
        session._ui_purchase = previous
    state = previous.state.get("purchase") or {}
    if (view is not None and state.get("state") in ("selected", "settling")
            and state.get("receiver") != view.receiver_token):
        state["state"] = "refused"
        state["result"] = dict(state="refused", reason="receiver_changed_without_owned_receipt", item=state.get("item"))
    return previous


def selected(current):
    call = getattr(current, "call", None)
    return current is not None and (getattr(call, "name", None) == "beli" or "purchase" in current.state)


def transaction_context(current, view):
    if not selected(current):
        return None
    state = current.state.get("purchase") or {}
    intent = (dict(item=current.call.arg, quantity=current.call.n, source="self_chosen_activity")
              if current.call.name == "beli" and state.get("source", "activity") == "activity"
              else dict(item=state.get("item"), quantity=state.get("units"),
                  source="self_chosen_shop_control"))
    control = ("buy: <nama barang> ×<jumlah>" if getattr(view, "named_item_args", False)
               else "buy:{control}:{quantity}")
    return dict(intent=intent,
        state=state.get("state", "selected"), chosen_item=state.get("item"),
        quantity=state.get("quantity", current.call.n), result=deepcopy(state.get("result")),
        fact=f"A selected purchase remains pending across inspect/re-render. {control} is a native stock click. "
             "Held is not owned. Completion requires currency and bag deltas. refuse:* cancels a pending request; "
             "a completed purchase remains owned when the shop is closed. "
             "closing by choice does not mean the store is closed.")


def refusal_actions(current, view, actions):
    if not selected(current) or (current.state.get("purchase") or {}).get("state") in ("completed", "refused"):
        return actions
    from arti_stardew_generic_ui import exit_keys
    exits = exit_keys(view)
    close_ids = {c["id"] for c in view.components if c.get("kind") == "close"}
    return {("refuse:purchase:" + key if key in exits or key.startswith("press:")
             and any(key.split(":")[2:3] == [str(cid)] for cid in close_ids) else key): value
            for key, value in actions.items()}


def resolve_transaction_action(current, view, arg):
    from arti_stardew_generic_ui import controls, resolve_action, action_key
    base = controls(view)
    actions = refusal_actions(current, view, base)
    key = action_key(view, arg)
    if key in actions:
        return actions[key]
    if key in base:
        raise ValueError("purchase_requires_explicit_refusal")
    return resolve_action(view, arg)


def _count(ledger, item):
    rows = ledger.get("inventory")
    if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get("stack")) is not int for r in rows):
        return None
    return sum(r["stack"] for r in rows if r.get("item") == item)


def _balance(ledger, currency):
    shop = ledger.get("shop") or {}
    return ledger.get("money") if currency == 0 else shop.get("balance") if shop.get("currency") == currency else None


def stock_control(view, chosen):
    if not isinstance(chosen, tuple) or chosen[0] != "ui_press" or chosen[1].get("btn") not in ("A", "X"):
        return None
    cid = chosen[1].get("choice", chosen[1].get("snapped_id"))
    return next((r for r in (view.shop or {}).get("stock", ()) if r[8] is not None and str(r[8]) == str(cid)), None)


def stock_offer(view, chosen):
    """Terms of the native row under a recycled control, excluding available balances."""
    row = stock_control(view, chosen)
    return stock_terms(view.shop, row)


def stock_terms(shop, row):
    if row is None or not row[1] or type(row[3]) is not int or row[3] < 0:
        return None  # unread: a queued buy of it is refused, never pressed on a guess
    trades = shop.get("trades")
    if not isinstance(trades, (list, tuple)) or any(
            not isinstance(t, (list, tuple)) or len(t) != 4 or type(t[0]) is not int
            or not isinstance(t[1], str) or type(t[2]) is not int for t in trades):
        return None
    trades = [tuple(t[1:3]) for t in trades if t[0] == row[0]]
    return (shop.get("id"), shop.get("currency"), row[0], row[1], row[3], row[5], tuple(trades))


def observed_stock_choice(view, chosen, proof, arg):
    """Existing native stock clicks have the same receipt path as quantity choices.
    Read the actual inventory+cursor increase; never infer native modifier clamping or affordability.
    """
    if str(arg).startswith(("buy:", "refuse:")):
        return arg
    row = stock_control(view, chosen)
    if row is None or row[5] < 1:
        return arg
    def total(ledger):
        count = _count(ledger, row[1])
        if count is None:
            return None
        return count + (ledger.get("ui_held_stack", 0) if ledger.get("ui_held_item") == row[1] else 0)
    before, after = total(proof["before"]), total(proof["after"])
    if type(before) is int and type(after) is int and after > before and (after-before) % row[5] == 0:
        return "buy:%s:%s" % (row[8], (after-before)//row[5])
    return arg


def observe_purchase(current, view, chosen, proof, arg, why=None, *, recovery=False, her_choice=False):
    """Only authenticated owned-input proofs enter here. A cursor buy and a later bag settlement
    form one transaction; no credit is given for a money-only or cursor-only change. ADR 257: a `buy:N:Q` she chose
    herself is a transaction of exactly that row and quantity; the activity's own repeated presses keep its n.
    """
    arg = observed_stock_choice(view, chosen, proof, arg)
    if not selected(current) and not str(arg).startswith("buy:"):
        return None
    state = current.state.get("purchase")
    continuing = state is not None and state.get("state") in ("completed", "refused")
    if continuing and proof["request_id"] in state.get("requests", ()):
        return None
    if str(arg).startswith("buy:") and continuing:
        state = None  # a new explicit stock choice is a new transaction, not the old activity's quantity
    elif state is not None and state.get("state") == "completed":
        return None  # an exit cannot undo a native purchase or publish its receipt twice
    if str(arg).startswith("refuse:purchase:"):
        # ADR 43: the menu deadline's controller close is a recovery, never attributed to her choice or "why".
        result = dict(state="refused", reason="purchase_abandoned_by_recovery" if recovery else "purchase_refused_by_choice",
                      why="controller_menu_deadline" if recovery else why or "explicit_cancel_purchase",
                      request_id=proof["request_id"], menu_effect=proof["effect"])
        current.state["purchase"] = dict(state or {}, state="refused", result=result)
        return result
    if str(arg).startswith("buy:") and state is None:
        _, control, quantity = arg.split(":")
        row = next((r for r in (view.shop or {}).get("stock", ()) if r[8] == int(control)), None)
        if row is None:
            return None
        activity = current.call.name == "beli" and not continuing and not her_choice
        units = max(int(quantity), current.call.n or 1) if activity else int(quantity)
        state = dict(state="selected", source="activity" if activity else "shop_control", item=row[1], quantity=units * row[5], price=row[3],
            units=units, currency=view.shop["currency"], before=deepcopy(proof["before"]),
            receiver=proof["before_receiver"], requests=[])
        current.state["purchase"] = state
    if state is None or state["state"] in ("completed", "refused"):
        return None
    if state["receiver"] != proof["before_receiver"] or proof["request_id"] in state["requests"]:
        return None
    state["requests"].append(proof["request_id"])
    state["receiver"] = proof["after_receiver"]
    before, after = state["before"], proof["after"]
    ib, ia = _count(before, state["item"]), _count(after, state["item"])
    mb, ma = _balance(before, state["currency"]), _balance(after, state["currency"])
    if any(type(n) is not int for n in (ib, ia, mb, ma)):
        return None
    state["state"] = "settling" if after.get("ui_held_item") == state["item"] else "selected"
    result = dict(state=state["state"], reason="purchase_pending_inventory_readback", item=state["item"],
        source="generic_ui_native_readback",
        requested_quantity=state["quantity"], inventory_delta=ia-ib, money_delta=after.get("money", 0)-before.get("money", 0),
        currency=state["currency"], currency_delta=ma-mb, held=after.get("ui_held_item"),
        request_ids=list(state["requests"]))
    if (ia-ib == state["quantity"] and mb-ma == state["price"] * state["units"]
            and not after.get("ui_held_item") and not after.get("cursor_item")):
        state["state"] = result["state"] = "completed"
        result["reason"] = "observed_purchase_currency_and_inventory_delta"
        result.update(kind="buy", quantity=ia-ib, request_id=proof["request_id"])
    state["result"] = result
    return result
