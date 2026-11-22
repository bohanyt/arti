"""ADR 140: bounded model-authored offered calls, using the ordinary executors.

No action is synthesized here. A queued call is a proposal until the next native
offer and the existing executor admit it. Native ingress latches interruptions.
"""
from dataclasses import replace
from copy import deepcopy


def drop(runtime, session, reason):
    slot = getattr(session, "_decision_plan", None)
    if slot:
        slot["cancelled"] = True
        session._decision_plan = None
        runtime._journal(dict(ev="decision_plan_dropped", session_id=session.session_id,
                              reason=reason, remaining=len(slot["tail"])))


def page(view):
    container = view.menu_container or {}
    return (view.menu_type, view.context, view.text, tuple(str(o) for o in view.options),
            container.get("kind"), container.get("source_fact_id"))


def proposal_valid(session, call):
    proposal = getattr(session, "_queued_plan_pick", None)
    return not proposal or proposal[0] != call or not proposal[1].get("cancelled")


def started(session, call):
    proposal = getattr(session, "_queued_plan_pick", None)
    if proposal and proposal[0] == call:
        session._queued_plan_pick = None
        if not proposal[1]["tail"]:
            session._decision_plan = None


def ui_target(runtime, session, call, view):
    from arti_stardew_generic_ui import controls
    from scripts.live7.transactions import resolve_transaction_action, purchase_holder
    actions = controls(view)
    actions.update(runtime._live7_transfer_actions(view))
    chosen = actions.get(call.arg) if str(call.arg).startswith("transfer:") else resolve_transaction_action(
        purchase_holder(session, runtime._active), view, call.arg)
    if not isinstance(chosen, tuple):
        raise ValueError("plan_ui_not_pressable")
    op, args = chosen
    # Re-pin cosmetic publication tokens only. Item, quality, slot, mode and quantity
    # must still denote the same native target. One-at-a-time transfers may shrink.
    semantic = {k: v for k, v in args.items() if k not in (
        "receiver_token", "target_token", "readback_token", "context_generation")}
    if args.get("quantity") == 1 or args.get("mode") == "ship_one":
        semantic.pop("stack", None)
        semantic.pop("owner_ceiling", None)
    cid = args.get("choice", args.get("snapped_id"))
    # Stock terms bind the purchase below. Widget labels can also include the
    # remaining stock, which the native shop checks for the requested press.
    component = None if purchase_control(view, call.arg, chosen) is not None else next(
        (c for c in view.components if str(c.get("id")) == str(cid)), None)
    return deepcopy((op, semantic, component))


def purchase_control(view, arg, chosen=None):
    if str(arg).startswith("buy:"):
        return arg.split(":")[1]
    if chosen is None:
        from arti_stardew_generic_ui import action_key, controls
        chosen = controls(view).get(action_key(view, arg))
    from scripts.live7.transactions import stock_control
    row = stock_control(view, chosen)
    if row is not None:
        return str(row[8])
    if isinstance(chosen, tuple) and chosen[0] == "ui_press" and chosen[1].get("btn") in ("A", "X"):
        cid = str(chosen[1].get("choice", chosen[1].get("snapped_id")))
        if any(c.get("kind") == "stock" and str(c.get("id")) == cid for c in view.components):
            return cid
    return None


def buy_offer(view, arg, chosen=None):
    from arti_stardew_generic_ui import action_key, controls
    from scripts.live7.transactions import stock_offer
    return stock_offer(view, chosen if chosen is not None else controls(view).get(action_key(view, arg)))


def page_buy(view, arg, shown):
    """Bind a purchase to its own visible card and the native view it came from."""
    cid = purchase_control(view, arg)
    if cid is None:
        return None
    from arti_runtime.shop_quantity import free_quantity, stock_row
    from scripts.live7.transactions import stock_terms
    composed = free_quantity(view, arg) is not None
    terms = stock_terms(view.shop, stock_row(view, cid)) if composed else buy_offer(view, arg)
    if terms is None:
        return "shop_offer_unread"
    if shown is None:
        return terms
    from arti_stardew_generic_ui import action_key
    key = action_key(view, arg)
    cards = [shown.get(key)] if key.startswith("buy:") and not composed else [
        card for k, card in shown.items() if k == f"stock:{cid}" or k.startswith(f"buy:{cid}:")]
    _, currency, _, _, price, stack, trades = terms
    if any(isinstance(card, dict) and card.get("row") and card.get("price") == price
           and card.get("currency") == currency and card.get("stack", 1) == stack
           and card.get("barter", []) == [list(trade) for trade in trades] for card in cards):
        return terms
    return "shop_offer_unread"


def verify_buy(view, call, expected):
    if expected is None:
        return
    if expected == "shop_offer_unread":
        raise ValueError(expected)
    try:
        current = buy_offer(view, call.arg) if view is not None else None
    except ValueError:
        current = None
    if current != expected:
        raise ValueError("shop_offer_changed")


def verify_queued_buy(session, call, view):
    proposal = getattr(session, "_queued_plan_pick", None)
    if proposal and proposal[0] == call:
        verify_buy(view, call, proposal[2]["buy"])


def basis(runtime, session, facts, motor):
    from arti_stardew_live7 import home_due
    return (tuple(session.campaign.epoch), runtime._date_tuple(motor),
            facts.time_of_day // 100, home_due(facts, session.campaign.live))


def admit(runtime, session, decision, first, rendered, arg_map, shown=None):
    """All-or-nothing admission against this decision's offered catalogue."""
    from arti_stardew_decision import ActChoice
    from arti_stardew_live7 import choice
    from arti_stardew_skills import SkillCall
    proposed = getattr(decision, "plan", ())
    if not proposed:
        return True
    if session.mode != "part_live" or not isinstance(first, SkillCall):
        return False
    entries = []
    ui = getattr(runtime, "_live7_ui_offer", None)
    view = runtime._live7_unknown_ui_view(session) if ui else None
    expanded = [dict(s, arg=press, repeat=1, presses=None) for s in proposed
                for press in (s.get("presses") or [s["arg"]] * s.get("repeat", 1))]
    for step in expanded:
        args = {k: v for k, v in step.items() if k not in ("repeat", "presses") and (k != "n" or v is not None)}
        selected = (SkillCall("ui_action", args.get("arg")) if ui and args.get("name") == "ui_action"
                    and set(args) == {"name", "arg"} else choice(
                        replace(decision, choice=ActChoice("skill", None, args, ())), rendered, arg_map,
                        goods_names=getattr(session.campaign.live, "store_names", None)))
        if not isinstance(selected, SkillCall) or selected.batch or selected.name == "read_boundary":
            return False
        target = None
        if selected.name == "ui_action":
            if view is None:
                return False
            try:
                target = ui_target(runtime, session, selected, view)
            except (ValueError, KeyError, TypeError):
                return False
        elif step.get("repeat", 1) != 1:
            return False
        # ADR 257: a buy is bound to the stock offer of the page she read, not to whatever the shop shows at her reply.
        seen = getattr(runtime, "_live7_ui_seen_view", None) or view
        buy = None
        if target is not None:
            try:
                buy = page_buy(seen, selected.arg, shown)
                if buy is None and purchase_control(view, selected.arg) is not None:
                    buy = "shop_offer_unread"
            except ValueError:
                buy = "shop_offer_unread"
        entries.extend(dict(call=selected, target=target, page=page(view) if view else None, buy=buy)
                       for _ in range(step.get("repeat", 1)))
    if not entries or entries[0]["call"] != first:
        return False
    facts, motor, _, held = runtime._live7_read(session)
    if held:
        return False
    old = getattr(session, "_decision_plan", None)
    # A menu subplan can run while the enclosing activity is unfinished. Its
    # presses precede the remaining world steps; never replace the owner's plan.
    tail = entries[1:] + (old["tail"] if old and ui else [])
    if tail:
        session._decision_plan = dict(tail=tail, basis=old["basis"] if old and ui else basis(runtime, session, facts, motor),
            health=old["health"] if old and ui else getattr(motor, "health", None),
            receiver=view.receiver_token if view else None, ui_ready=False,
            world_before=old["world_before"] if old and ui else len(session.runs),
            pending_world=old["pending_world"] if old and ui else first if not ui else None)
        runtime._journal(dict(ev="decision_plan_admitted", session_id=session.session_id,
            steps=[dict(s) for s in proposed],
            remaining=len(tail)))
    return True


def observe(runtime, session, motor):
    """Latch new native events even if they close before the next skill boundary."""
    slot = getattr(session, "_decision_plan", None)
    if not slot:
        return
    reason = None
    clock = getattr(motor, "time_of_day", None)
    if runtime._date_tuple(motor) != slot["basis"][1] or type(clock) is not int or clock // 100 != slot["basis"][2]:
        reason = "time_boundary"
    health = getattr(motor, "health", None)
    if type(health) in (int, float) and type(slot["health"]) in (int, float) and health < slot["health"]:
        reason = "native_damage"
    menu = getattr(motor, "active_menu", None)
    ui_rec = runtime._obs.get(runtime._binding_view()[1], "ui.context")
    from arti_stardew_live7 import threat
    if threat((getattr(ui_rec, "facts", {}) or {}).get("combat"), health,
              damaged_at=session.campaign.live.damaged_at, now=clock):
        reason = "under_attack"
    if menu:
        run = session.current
        last = getattr(runtime._active, "last_action", None)
        trace = run.state.get("native_ui_episode") if run is not None else None
        known_menu = ((trace or {}).get("opened") or {}).get("menu")
        after = getattr(last, "after", None) or {}
        known_menu = known_menu or after.get("menu_type") or after.get("active_menu")
        receivers = {after.get("receiver_token"), (run.state.get("goods_receiver") if run else None)}
        if trace:
            receivers.add(trace["opened"]["receiver"])
            receivers.update(i.get("after_receiver") for i in trace.get("inputs", ()) if i.get("linked"))
        receiver = ((getattr(ui_rec, "facts", {}) or {}).get("interactive") or {}).get("receiver_token")
        native_open = (getattr(last, "lifecycle", None) == "completed"
                       and getattr(last, "cleanup_confirmed", False) is True
                       and getattr(last, "world_effect_status", None) == "observed_container_open"
                       and menu == "ItemGrabMenu" and receiver is not None)
        if native_open and slot.get("opened_request") != getattr(last, "request_id", None):
            # Same attribution as the existing native container-open consumer.
            # Bind it once; a second receiver cannot reuse the old open receipt.
            slot["opened_request"] = getattr(last, "request_id", None)
            slot["owned_receiver"] = receiver
        owned = (run is not None and run.goal_id == getattr(runtime._active, "goal_id", None)
                 and receiver is not None and receiver in receivers
                 and (known_menu == menu or menu == "ItemGrabMenu" and run.state.get("goods_receiver")))
        owned = owned or receiver is not None and receiver == slot.get("owned_receiver")
        same_menu_plan = slot["receiver"] is not None and receiver == slot["receiver"]
        if (("Dialogue" in menu or "Question" in menu) and not same_menu_plan
                or not owned and not same_menu_plan and not slot.get("ui_inflight")):
            if getattr(runtime, "_action_busy", False) and "Dialogue" not in menu and "Question" not in menu:
                slot["pending_menu"] = True  # native effect attribution is not available until this action settles
            else:
                reason = "dialogue_or_foreign_menu"
        elif owned or same_menu_plan:
            slot.pop("pending_menu", None)
    elif slot.get("pending_menu"):
        reason = "unattributed_transient_menu"
    if reason:
        drop(runtime, session, reason)


def take(runtime, session, trigger):
    """Return one queued proposal, or None to ask the model. Never send input."""
    from arti_stardew_live7 import threat
    from arti_stardew_skills import precondition_failures
    slot = getattr(session, "_decision_plan", None)
    if not slot:
        return None
    if not slot["tail"]:
        drop(runtime, session, "preceding_step_not_completed")
        return None
    if session is not runtime._skill_session or session.status != "active" or not runtime._live7_active():
        drop(runtime, session, "session_changed")
        return None
    facts, motor, context, held = runtime._live7_read(session)
    if held:
        drop(runtime, session, "readback_unavailable")
        return None
    observe(runtime, session, motor)
    if not getattr(session, "_decision_plan", None):
        return None
    if basis(runtime, session, facts, motor) != slot["basis"] or threat(
            context.get("combat"), facts.health, damaged_at=session.campaign.live.damaged_at, now=facts.time_of_day):
        drop(runtime, session, "time_identity_or_attack")
        return None
    last = session.runs[-1] if session.runs else None
    if last is not None and last.status == "failed":
        drop(runtime, session, "step_failed")
        return None
    entry = slot["tail"][0]
    call = entry["call"]
    ui = getattr(runtime, "_live7_ui_offer", None)
    if ui and call.name != "ui_action":
        return None  # the running activity's own menu still needs a choice
    if (call.name == "ui_action" and not slot["ui_ready"] or call.name != "ui_action" and (
            len(session.runs) <= slot["world_before"] or last is None or last.status != "finished"
            or last.call != slot["pending_world"])):
        drop(runtime, session, "preceding_step_not_completed")
        return None
    try:
        if call.name == "ui_action":
            view = runtime._live7_unknown_ui_view(session) if ui else None
            verify_buy(view, call, entry["buy"])
            if (view is None or page(view) != entry["page"] or view.receiver_token != slot["receiver"]
                    or ui_target(runtime, session, call, view) != entry["target"]):
                raise ValueError("menu_page_or_target_changed")
        else:
            if facts.menu_open:
                raise ValueError("unexpected_menu")
            fresh = runtime._live7_calls(session, runtime._build_decision_snapshot(runtime._active), facts)
            if call not in fresh or precondition_failures(call, facts):
                raise ValueError("native_precondition_or_bag_changed")
    except (ValueError, KeyError, TypeError) as exc:
        drop(runtime, session, str(exc))
        if str(exc) in ("shop_offer_changed", "shop_offer_unread"):
            runtime._live7_menu_changed(session, str(exc))
        return None
    slot["tail"].pop(0)
    if call.name == "ui_action":
        slot["ui_ready"] = False
    else:
        slot["world_before"], slot["pending_world"] = len(session.runs), call
    session.model_pick = None  # a continuation is not a new provider reservation
    session._queued_plan_pick = (call, slot, entry)
    runtime._journal(dict(ev="decision_plan_step", session_id=session.session_id, trigger=trigger,
                          choice=[call.name, call.arg], remaining=len(slot["tail"])))
    return call


def ui_result(runtime, session, run, fresh):
    slot = getattr(session, "_decision_plan", None)
    if not slot:
        return
    slot.pop("ui_inflight", None)
    if not run.verified or not run.cleanup or run.uncertain:
        drop(runtime, session, "menu_step_failed")
    else:
        slot["ui_ready"] = True
    if getattr(session, "_decision_plan", None) and fresh is not None:
        # Only this authenticated action may advance the receiver; a foreign
        # receiver seen by take() is never silently adopted.
        expected = getattr(run, "result_receiver_token", None)
        if fresh.receiver_token not in (slot["receiver"], expected):
            drop(runtime, session, "foreign_receiver")
        else:
            slot["receiver"] = fresh.receiver_token
            slot["owned_receiver"] = fresh.receiver_token
    session._queued_plan_pick = None
    if not slot["tail"]:
        session._decision_plan = None
