"""ADR 257: one shop intent composed from leased, receipt-backed native presses."""
from collections import Counter
from decimal import Decimal
import time

from arti_runtime.item_choice import NATIVE_BUY
from scripts.live7.transactions import _balance, _count, purchase_holder, stock_terms

MAX_PRESSES = 36  # Includes any native bag settlements needed by this decision.
HINT = "buy:<id>:<jumlah> (jumlah bulat positif)."


def stock_row(view, control):
    return next((row for row in (view.shop or {}).get("stock") or ()
                 if row[8] is not None and str(row[8]) == control), None)


def free_quantity(view, arg):
    """A different quantity on this row. Exact native keys keep their own path."""
    if view is None or not isinstance(arg, str) or not NATIVE_BUY.fullmatch(arg):
        return None
    from arti_stardew_generic_ui import controls
    _, control, digits = arg.split(":")
    if stock_row(view, control) is None or arg in controls(view):
        return None
    amount = Decimal(digits)
    return (control, amount) if amount > 0 else None


def _held(ledger, item):
    if ledger.get("cursor_item") or ledger.get("ui_held_item") not in (None, "", item):
        return None
    return ledger.get("ui_held_stack") if ledger.get("ui_held_item") == item else 0


def _purchase_delta(terms, proof, native_qty):
    """Recover units and bagged items from payment and inventory, including the menu cursor."""
    _, currency, _, item, price, stack, trades = terms
    before, after = proof["before"], proof["after"]
    costs = Counter()
    for material, count in trades:
        costs[material] += count
    keys = costs.keys() | {item}
    prior = {key: _count(before, key) for key in keys}
    now = {key: _count(after, key) for key in keys}
    held_before, held_after = _held(before, item), _held(after, item)
    b, a = _balance(before, currency), _balance(after, currency)
    if any(type(n) is not int for n in (*prior.values(), *now.values(), held_before, held_after, b, a,
                                       before.get("money"), after.get("money"))):
        return None
    change = {key: now[key] - prior[key] for key in keys}
    bagged = change[item]
    if not native_qty:
        if bagged > 0 and held_before - held_after == bagged and b == a and all(
                change[key] == 0 for key in keys - {item}):
            return 0, bagged, held_after, 0
        return None
    total = bagged + held_after - held_before
    actual = [n for n in range(1, native_qty + 1)
              if total == n * (stack - costs[item]) and b - a == n * price
              and all(change[key] == -n * costs[key] for key in keys - {item})]
    if len(actual) == 1:
        units, = actual
        return units, bagged + units * costs[item], held_after, a - b
    return None


def execute(runtime, session, arg, initial, intent, goal, revision, decision_until):
    from arti_stardew_generic_ui import controls, stable_identity, ui_readback
    from scripts.live7.menu_deadline import enabled

    visit_bound = enabled(runtime._config)
    control, amount = intent
    row = stock_row(initial, control)
    expected = stock_terms(initial.shop, row)
    if expected is None:
        runtime._live7_menu_changed(session, "shop_offer_unread")
        return True

    with runtime._lock:
        if runtime._action_busy or runtime._inflight_generation is not None:
            return True
        active_run = session.current
        runtime._action_busy = True
    bought = bagged = held = currency_delta = money_delta = 0
    requests = []
    unsafe = None
    reason = "shop_press_safety_bound"

    def fresh():
        if (runtime._active is not goal or goal.revision != revision
                or session is not runtime._skill_session or session.current is not active_run or not runtime._live7_active()
                or runtime.live7_stop_file_check()):
            raise ValueError("shop_purchase_interrupted")
        view = runtime._live7_unknown_ui_view(session, publishing=True)
        if view is None:
            raise ValueError("shop_view_unavailable")
        if view.receiver_token != initial.receiver_token:
            raise ValueError("menu_receiver_changed")
        if stock_terms(view.shop, stock_row(view, control)) != expected:
            raise ValueError("shop_offer_changed")
        if not requests and (time.monotonic() >= decision_until or stable_identity(view) != stable_identity(initial)):
            raise ValueError("unknown_ui_changed")
        return view

    try:
        fresh()
        if not visit_bound:
            runtime._live7_menu_admitted(session, initial)
        for _ in range(MAX_PRESSES):
            view = fresh()
            actions = controls(view)
            if held:
                cursor = view.cursor_settle or {}
                if (cursor.get("cursor") or {}).get("item") != expected[3]:
                    reason = "shop_purchase_cursor_changed"
                    break
                targets = [(t["room"], f"settle:{t['id']}") for t in cursor.get("targets", ())
                           if t["kind"] == "inventory" and f"settle:{t['id']}" in actions]
                if not targets:
                    reason = "inventory_capacity_insufficient"
                    break
                _, key = max(targets)
                qty = 0
            else:
                native = [(chosen[1]["qty"], key) for key, chosen in actions.items()
                          if key.startswith(f"buy:{control}:") and bought + chosen[1]["qty"] <= amount]
                if not native:
                    reason = stock_row(view, control)[10] or "shop_quantity_not_published"
                    break
                qty, key = max(native)
            chosen = actions[key]

            def guard():
                current = fresh()
                if stable_identity(current) != stable_identity(view) or controls(current).get(key) != chosen:
                    raise ValueError("unknown_ui_changed")

            run, _ = runtime._run_ui_pad(*chosen, origin="live7_shop_quantity", revalidate_extra=guard)
            if run.uncertain or not run.cleanup:
                reason = str(run.reason or "shop_purchase_unconfirmed")
                unsafe = dict(pick=arg[:64], reason=reason[:64], menu=view.menu_type, receiver=view.receiver_token,
                              request_id=run.request_id, close=False)
                break
            if not run.verified:
                reason = str(run.reason or "native_purchase_refused")
                break
            after = runtime._live7_unknown_ui_view(session, publishing=True)
            proof = ui_readback(view, run, after)
            delta = _purchase_delta(expected, proof, qty) if proof and proof["request_id"] not in requests else None
            if delta is None:
                reason = "shop_purchase_readback_unconfirmed"
                break
            units, moved, held, payment = delta
            bought += units
            bagged += moved
            currency_delta += payment
            money_delta += proof["after"]["money"] - proof["before"]["money"]
            requests.append(proof["request_id"])
            if visit_bound:
                runtime._live7_menu_press_verified(session, view, key)
            runtime._live7_ui_progress_seq = getattr(runtime, "_live7_ui_progress_seq", 0) + 1
            runtime._journal(dict(ev="generic_ui_readback", **proof))
            if qty and units < qty:
                reason = "native_purchase_partial"
                break
            if bought == amount and not held:
                reason = "completed"
                break
    except ValueError as exc:
        reason = str(exc)
    finally:
        with runtime._lock:
            runtime._action_busy = False
            if unsafe is not None:
                runtime._live7_ui_unsafe = unsafe
            if runtime._active is goal and goal.revision == revision and session is runtime._skill_session:
                requested = int(amount) if amount.adjusted() < 64 else format(amount, ".12E")
                completed = reason == "completed"
                verdict = "observed_purchase_currency_and_inventory_delta" if completed else reason
                requested_items = requested * expected[5] if type(requested) is int else requested
                result = dict(requested=requested, received=bagged, reason=reason, item=expected[3],
                              items_per_unit=expected[5], currency=expected[1], currency_delta=currency_delta)
                if held:
                    result["in_hand"] = held
                session._shop_purchase_result = result
                current = session.current
                if current is active_run:
                    owner = purchase_holder(session, goal)
                    owner.state["purchase"] = dict(state="completed" if completed else "refused",
                        source="shop_control", item=expected[3], units=requested, quantity=requested_items, result=result)
                if current is not None and current is active_run and current.goal_id == goal.goal_id:
                    current.pending = ("done" if completed else "fail", verdict)
                    objective = getattr(goal, "economy_objective", None)
                    if bagged and getattr(objective, "kind", None) == "buy":
                        objective.item_id = expected[3]
                        objective.done += bagged
                        objective.events.append(dict(kind="buy", item=expected[3], quantity=bagged,
                            request_ids=list(requests), money_delta=money_delta))
                receipt = dict(result, reason=verdict, source="generic_ui_native_readback", request_ids=requests,
                    state="completed" if completed else "partial" if bagged else "refused", held=held,
                    requested_quantity=requested_items, inventory_delta=bagged, money_delta=money_delta)
                runtime._journal(dict(ev="purchase_lifecycle", **receipt))
                if bagged:
                    runtime._journal(dict(ev="native_transaction", kind="buy", quantity=bagged,
                        request_id=requests[-1], **receipt))
    return "unsafe" if unsafe is not None else True
