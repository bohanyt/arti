"""Native receipts for the transfers she chose during an open container episode.

ADR 114. These are actual selected items, never fulfillment of another item's
store objective. The leased executor authenticates native request and release.
"""


def note_transfer(current, args, native):
    if current is None:
        return None
    receipts = current.state.setdefault("chosen_transfers", [])
    request = getattr(native, "request_id", None)
    mode = args.get("mode", "put")
    amount = args.get("quantity", args.get("stack"))
    if (not getattr(native, "verified", False) or getattr(native, "cleanup", False) is not True
            or getattr(native, "lifecycle", None) != "completed" or getattr(native, "uncertain", True)
            or getattr(native, "outcome", None) != "TransferObserved" or not request
            or mode not in ("put", "quick", "take_out") or type(amount) is not int or amount <= 0
            or not args.get("qualified_item_id") or args.get("quality") not in (0, 1, 2, 4)
            or not args.get("container_token") or not args.get("source_fact_id")
            or any(r["request_id"] == request for r in receipts)):
        return None
    receipt = dict(request_id=request, item=args["qualified_item_id"], quality=args["quality"], quantity=amount,
                   direction="out" if mode == "take_out" else "in", container=args["container_token"],
                   source=args["source_fact_id"])
    receipts.append(receipt)
    return receipt


def stored_by_choice(current):
    """Net positive stores by exact chest/item/quality; a take-out alone is never a store."""
    return _net_stored(current.state.get("chosen_transfers", ()))


def chest_moves(receipts):
    """FIX77B: her verified moves as evidence on the row, per chest and item, both directions. Stays beside `actual_stored`,
    which is only what net went in."""
    moves = {}
    for r in receipts or ():
        row = moves.setdefault((r["source"], r["item"]), dict(source=r["source"], item=r["item"], taken=0, stored=0))
        row["taken" if r["direction"] == "out" else "stored"] += r["quantity"]
    return list(moves.values())


def _net_stored(receipts):
    totals = {}
    for r in receipts:
        key = r["container"], r["source"], r["item"], r["quality"]
        row = totals.setdefault(key, dict(item=r["item"], quality=r["quality"], quantity=0, request_ids=[]))
        row["quantity"] += r["quantity"] * (-1 if r["direction"] == "out" else 1)
        row["request_ids"].extend(r.get("request_ids", [r["request_id"]]))
    return [r for r in totals.values() if r["quantity"] > 0]


def close_store(runtime, goal, current):
    """ADR 116: her close ends an episode by its native net deposits, not its menu token."""
    unresolved = runtime._gather_mutation_unresolved or getattr(goal, 'gather_uncertain', False)
    receipts = list(current.state.get('chosen_transfers', ()))
    progress = getattr(goal, 'transfer_progress', None) or {}
    # A later authenticated move of the same stack adjusts the historical pair.
    # An unexplained bag change still fails _store_receipt_standing unchanged.
    following = [r for r in receipts if
        (r['container'],r['source'],r['item'],r['quality']) ==
        tuple(progress.get(k) for k in ('container_token','source_fact_id','qualified_item_id','quality'))
        and r['request_id'] not in progress.get('request_ids', ())]
    if unresolved:
        paired = None  # An unresolved original transfer can never fulfill its objective.
        # A separate item's authenticated receipt still stands (ADR 121).
        # The unknown mutation might affect its own item, so exclude that item.
        uncertain_item = getattr(getattr(goal, 'gather_progress', None), 'qualified_item_id', current.call.arg)
        receipts = [r for r in receipts if r['item'] != uncertain_item]
    elif following:
        net = sum(r['quantity'] * (-1 if r['direction']=='out' else 1) for r in following)
        adjusted = dict(progress,transferred=progress['transferred']+net,
            actor_count=progress['actor_count']-net,container_count=progress['container_count']+net,
            request_ids=[*progress['request_ids'],*(r['request_id'] for r in following)])
        paired = runtime._store_transfer_receipt(goal, progress=adjusted)
    else:
        paired = runtime._store_transfer_receipt(goal)
    # Selected receipts must still belong to the goal's native session and final
    # bag. Check the latest native count of each item, including withdrawals.
    latest = {(r['item'],r['quality']):r for r in receipts}
    standing = {key for key,r in latest.items() if type(r.get('actor_count')) is int
                and runtime._store_receipt_standing(goal, dict(qualified_item_id=r['item'],
                    quality=r['quality'],actor_count=r['actor_count']))}
    receipts = [r for r in receipts if (r['item'],r['quality']) in standing]
    if paired:
        requests = paired['request_ids']
        receipts = [r for r in receipts if r['request_id'] not in requests]
        receipts.insert(0, dict(item=paired['item'],quality=paired['quality'],quantity=paired['transferred'],
            container=progress.get('container_token'),source=progress.get('source_fact_id'),
            direction='in',request_id=requests[0],request_ids=requests))
    actual = _net_stored(receipts)
    if not actual:
        return None
    requested = paired['requested'] if paired else current.call.n
    item = paired['item'] if paired else current.call.arg
    quality = paired['quality'] if paired else current.call.quality
    count = sum(r['quantity'] for r in actual if (r['item'],r['quality']) == (item,quality))
    # Gather/one-tree objectives require their own rule, not merely chosen deposits.
    complete = (not unresolved and not getattr(goal,'episode_plan',None)
                and type(requested) is int and requested > 0 and count >= requested)
    observed = dict(paired or {}, actual_stored=actual, original_objective_satisfied=complete,
                    source='native_container_transfer')
    if unresolved:
        observed['original_mutation_unresolved'] = True
    reason = 'observed_paired_transfer_objective' if complete else 'observed_paired_transfer_partial'
    return ('achieved' if complete else 'partial'), reason, observed


def close_shipping(objective):
    from arti_stardew_economy import shipping_summary, objective_verdict
    if objective is None or objective.kind != 'ship' or objective.uncertain:
        return None
    observed = shipping_summary(objective)
    if not observed['actual_shipped']:
        return None
    complete = objective_verdict(objective)[0] == 'achieved'
    observed['original_objective_satisfied'] = complete
    return ('achieved' if complete else 'partial'), ('observed_ship' if complete else 'observed_ship_partial'), observed


def finish_closed_store(runtime, session, goal, *, reason):
    """One closed episode, whether she closed it or its recovery did (ADR 143)."""
    current = session.current
    if (current is None or current.call.name != "simpan_ke_chest" or goal is None
            or current.goal_id != goal.goal_id or current.pending is not None or runtime._goods_effect_verified(goal)):
        return
    current.state["goods_remaining"] = []
    settled = close_store(runtime, goal, current)
    if settled:
        from arti_stardew_runtime import FinishEvidenceView
        status, finish_reason, observed = settled
        goal.last_finish = FinishEvidenceView(status, finish_reason, observed)
        goal.progress_marker = "terminal"
        goal.revision += 1
        current.state["result_observation"] = observed
        current.pending = ("done" if status == "achieved" else "fail", finish_reason)
        runtime._journal(dict(ev="finish_verified", goal_id=goal.goal_id, goal_revision=goal.revision,
            verdict=dict(status=status, reason=finish_reason, observed=observed),
            origin="chosen_store" if reason == "store_closed_by_choice" else "recovered_store"))
    else:
        current.pending = ("fail", reason)
