"""Persona facts from native observations and verified receipts (ADRs 39, 40)."""
from collections.abc import Mapping
import time
from copy import deepcopy

STATE_FACT_KEYS = ("own_state", "menu_container", "current_menu", "peer_sleep", "shop_availability", "ready_check")


def ready_check_readback(runtime, pose, wall):
    """Use the same native UI wire as the chooser; never use a sleep objective as a check identity."""
    from arti_stardew_ui_pad import baca_ui_pad
    binding, pid = runtime._binding_view()
    obs = getattr(runtime, "_obs", None)
    rec = obs.get(pid, "ui.context") if obs is not None else None
    received = getattr(rec, "received_monotonic", None)
    if (rec is None or not getattr(rec, "available", False) or getattr(rec, "stale", True)
            or getattr(rec, "requires_resync", True) or not getattr(rec, "authoritative", False)
            or rec.player_id != pid or rec.screen_id != getattr(binding, "screen_id", rec.screen_id)
            or rec.screen_id != getattr(pose, "screen_id", rec.screen_id)
            or type(received) not in (int, float) or not 0 <= time.monotonic() - received <= 3
            or rec.facts.get("location_id") != getattr(pose, "location", None)):
        return None
    view = baca_ui_pad(rec.facts, age_s=time.monotonic() - received)
    if view is None or view.menu_type != "ReadyCheckDialog" or not view.ready_check:
        return None
    from scripts.live7.ready_checks import kind
    value = dict(view.ready_check, kind=kind(view.ready_check["check"]))
    return dict(value=value, observed_ts=wall-(time.monotonic()-received), source="native_ui_context")


def shop_readback(runtime, pose, wall):
    """Reuse fix19's native counter facts, with their own observation age/identity."""
    binding, pid = runtime._binding_view()
    obs = getattr(runtime, "_obs", None)
    rec = obs.get(pid, "ui.context") if obs is not None else None
    received = getattr(rec, "received_monotonic", None)
    if (rec is None or not getattr(rec, "available", False) or getattr(rec, "stale", True)
            or getattr(rec, "requires_resync", True) or not getattr(rec, "authoritative", False)
            or rec.player_id != pid or rec.screen_id != getattr(binding, "screen_id", rec.screen_id)
            or rec.screen_id != getattr(pose, "screen_id", rec.screen_id)
            or type(received) not in (int, float) or not 0 <= time.monotonic() - received <= 45
            or rec.facts.get("location_id") != getattr(pose, "location", None)):
        return None
    counters = [dict(ref=r.get("ref"), shop=r.get("label"), ready=r.get("ready"), refused=r.get("refused"),
                     availability={k: r["availability"].get(k) for k in (
                         "open_now", "shopkeeper_present", "opens_at", "closes_at", "closed_today", "hours_basis")})
                for r in rec.facts.get("extras_targets", ())
                if isinstance(r, Mapping) and r.get("kind") == "shop_counter" and isinstance(r.get("availability"), Mapping)]
    return dict(value=counters, observed_ts=wall - (time.monotonic() - received), source="native_ui_context",
                received_monotonic=received, observation_session=getattr(rec, "session_id", None))


def bound_pose(runtime, fallback=None):
    binding_view = getattr(runtime, "_binding_view", None)
    if not callable(binding_view):
        return fallback
    binding, pid = binding_view()
    if binding.status != "bound" or pid is None or runtime._state.is_stale(pid):
        return None
    latest = runtime._state.latest(pid)
    if latest is not None and (fallback is None or getattr(latest, "diterima_pada", -1) > getattr(fallback, "diterima_pada", -1)):
        return latest
    return fallback


def state_readback(runtime, *, wall=None):
    """Each self fact is stamped at native reception, never at a decision's authorship."""
    pose = bound_pose(runtime)
    received = getattr(pose, "diterima_pada", None)
    if pose is None or type(received) not in (int, float):
        return dict.fromkeys(STATE_FACT_KEYS)
    wall = time.time() if wall is None else wall
    stamp = wall - max(0, time.monotonic() - received)
    raw = getattr(pose, "own_state", None) or {}
    own = {}
    for key in ("inventory", "energy", "health", "money"):
        value = raw.get(key)
        if key in ("energy", "health"):
            value = value if isinstance(value, Mapping) else dict(current=getattr(pose, key, None), max=None)
            valid = all(v is None or type(v) in (int, float) for v in value.values())
        elif key == "inventory":
            valid = (isinstance(value, Mapping) and all(type(value.get(k)) is int for k in ("used", "free", "capacity"))
                     and 0 <= value["used"] <= value["capacity"] and value["free"] == value["capacity"] - value["used"])
        else:
            valid = type(value) is int
        own[key] = dict(value=deepcopy(value) if valid else None, observed_ts=stamp, source="native_state_readback")
    container = getattr(pose, "menu_container", None)
    if not isinstance(container, Mapping) or container.get("kind") not in ("shipping_bin", "chest", "other"):
        container = dict(kind="unknown") if getattr(pose, "active_menu", None) == "ItemGrabMenu" else None
    sleep = getattr(pose, "peer_sleep", None)
    sleep = player_identity(sleep, getattr(runtime, '_config', None) or {})
    return dict(own_state=own, menu_container=dict(container, observed_ts=stamp, source="native_menu_context") if container else None,
                current_menu=dict(value=getattr(pose, "active_menu", None), observed_ts=stamp, source="native_state_readback"),
                peer_sleep=dict(value=sleep, observed_ts=stamp, source="native_state_readback") if sleep else None,
                shop_availability=shop_readback(runtime, pose, wall), ready_check=ready_check_readback(runtime, pose, wall))


def player_identity(sleep, config):
    """Only native farmer names; chat identities are never mapped to farmers by name."""
    if not isinstance(sleep, Mapping):
        return None
    result = {k: sleep.get(k) for k in ('role', 'peer_bed', 'peer_ready', 'self_bed')}
    if isinstance(sleep.get('players'), list):
        result['players'] = []
        for player in sleep['players'][:8]:
            if not isinstance(player, Mapping):
                continue
            row = {k: player.get(k) for k in ('name', 'role', 'self', 'in_bed', 'ready_time')}
            if 'place' in player:
                place = player.get('place')
                row['place'] = place.strip()[:64] if isinstance(place, str) and place.strip() else None
            row['controller'] = (config.get('stardew_host_controller', 'unknown')
                                 if row['role'] == 'host farmer' else 'this agent' if row['self'] else 'unknown')
            result['players'].append(row)
        result['audience'] = 'Chat audience is outside the game; a chat speaker is not a native farmer. Matching names do not establish identity.'
        result['ready_basis'] = 'native timeWentToBed; 0 means no recorded ready time, not proof of bed absence; old-night times may persist after morning'
    return result


def cursor_completion(view, args, run):
    """The native conservation verdict proves the selected destination, including ground drops."""
    from arti_stardew_cursor_item import readback as cursor_readback
    if (view is None or args.get("settle") is not True or run.lifecycle != "completed"
            or not run.verified or not run.cleanup or run.uncertain or not run.request_id):
        return None
    receipt = cursor_readback(run.readback)
    block = view.cursor_settle or {}
    # fix59A (a43 d0001 J1492): press_args sends every G3 payload as a string ("0"), native target ids are ints, so
    # the drop of the Bamboo Pole never produced this receipt. Compare as text.
    target = next((t for t in block.get("targets", ()) if str(t["id"]) == str(args.get("choice"))), None)
    cursor = block.get("cursor") or {}
    if (not receipt or receipt["Status"] not in ("Settled", "Partial") or not target or not cursor
            or target["kind"] not in ("drop", "inventory", "container")):
        return None
    destination = {"drop": "ground", "inventory": "inventory"}.get(target["kind"])
    if target["kind"] == "container":
        destination = (getattr(view, "menu_container", None) or {}).get("kind")
        if destination not in ("shipping_bin", "chest", "other"):
            return None  # generic routing alone never establishes chest identity
    record = dict(ev="native_cursor_destination", effect="dropped" if destination == "ground" else
                  "shipped" if destination == "shipping_bin" else "stored",
                  destination=destination, item=cursor["item"],
                  quantity=receipt["Moved"], request_id=run.request_id, source="native_cursor_conservation",
                  observed={k: receipt[k] for k in ("Status", "Conserved", "Moved", "Deltas")})
    if destination == "ground":
        record.update(ground_facts(view, run, cursor["item"], is_tool=cursor.get("is_tool") is True))
    return record


def _generic_values(run):
    """The before/after GenericValues the native press readback carries (location, bag, menu items), or {}."""
    import json
    try:
        raw = json.loads(run.readback) if isinstance(run.readback, str) and len(run.readback) <= 1 << 20 else {}
    except ValueError:
        return {}, {}
    if not isinstance(raw, Mapping):
        return {}, {}
    before, after = raw.get("before"), raw.get("after")
    return (before if isinstance(before, Mapping) else {}), (after if isinstance(after, Mapping) else {})


def _bag_count(values, qid):
    rows = values.get("inventory")
    if not isinstance(rows, list):
        return None
    return sum(r["stack"] for r in rows if isinstance(r, Mapping) and r.get("item") == qid and type(r.get("stack")) is int)


def ground_facts(view, run, qid, *, is_tool):
    """fix59A (ADR 86): where an item left her bag onto the ground, from the same native readback."""
    from arti_runtime.ground_items import label
    from arti_runtime.fact_names import item_name
    before, after = _generic_values(run)
    cursor = (getattr(view, "cursor_settle", None) or {}).get("cursor") or {}
    name = cursor.get("name") if cursor.get("item") == qid else None
    return dict(location=before.get("location") if isinstance(before.get("location"), str) else None,
                name=item_name(qid, name=name) if name else label(qid, getattr(view, "components", ())), is_tool=bool(is_tool),
                bag_after=_bag_count(after, qid))


def ground_receipts(view, args, run):
    """fix59A (ADR 86): a verified trash press on the held item, or a reward/gift menu that closed with items the bag
    never got (the game drops them when the bag is full). Native readback only; the settle drop is cursor_completion."""
    if view is None or run.lifecycle != "completed" or not run.verified or run.uncertain or not run.request_id:
        return []
    before, after = _generic_values(run)
    if not before or not after or before.get("location") != after.get("location"):
        return []
    out = []
    held = before.get("ui_held_item") or before.get("cursor_item")
    block = getattr(view, "cursor_settle", None) or {}
    target = next((t for t in block.get("targets", ()) if str(t.get("id")) == str(args.get("choice"))), None)
    if (target is not None and target.get("kind") == "trash" and isinstance(held, str) and held
            and not (after.get("ui_held_item") or after.get("cursor_item"))
            and _bag_count(after, held) == _bag_count(before, held)):
        stack = before.get("ui_held_stack") or before.get("cursor_stack")
        out.append(dict(ev="native_ground_item", effect="trashed", item=held,
                        quantity=stack if type(stack) is int and stack > 0 else 1, request_id=run.request_id,
                        source="native_trash_readback", **ground_facts(view, run, held, is_tool=held.startswith(("(T)", "(W)")))))
    menu = before.get("menu_container") if isinstance(before.get("menu_container"), Mapping) else {}
    if (menu.get("menu_type") == "ItemGrabMenu" and menu.get("kind") == "other" and not after.get("menu_container")
            and isinstance(before.get("menu_items"), list)):
        left = {}
        for row in before["menu_items"]:
            path = row.get("path") if isinstance(row, Mapping) else None
            if (isinstance(path, str) and path.startswith("menu.ItemsToGrabMenu.") and isinstance(row.get("item"), str)
                    and type(row.get("stack")) is int and row["stack"] > 0):
                left[row["item"]] = left.get(row["item"], 0) + row["stack"]
        for qid, n in left.items():
            gained = (_bag_count(after, qid) or 0) - (_bag_count(before, qid) or 0)
            if gained < n:
                out.append(dict(ev="native_ground_item", effect="spilled", item=qid, quantity=n - max(0, gained),
                                request_id=run.request_id, source="native_menu_close_readback",
                                **ground_facts(view, run, qid, is_tool=qid.startswith(("(T)", "(W)")))))
    return out


def pose_readback(runtime, session, fallback=None, *, wall=None):
    """Latest bound native pose, stamped at reception rather than publication."""
    from arti_stardew_live7 import mind_day, mind_label
    wall = time.time() if wall is None else wall
    pose = bound_pose(runtime, fallback)
    date = tuple(getattr(pose, k, None) for k in ("year", "season", "day"))
    clock = dict(date=None, time=None, place=None)
    if pose is not None and type(getattr(pose, "time_of_day", None)) is int:
        clock = dict(date=mind_label(date if mind_day(date) is not None else session.campaign.date),
                     time=pose.time_of_day, place=getattr(pose, "location", None))
    received = getattr(pose, "diterima_pada", None)
    age = max(0, time.monotonic() - received) if type(received) in (int, float) else 0
    stamp = wall - age
    if type(received) in (int, float):
        # Wall-minus-age is a projection of two clocks and may jitter backwards; reception order owns pose freshness.
        last = vars(runtime).get("_live7_pose_stamp")
        if last is None or received >= last[0]:
            stamp = stamp if last is None else max(stamp, last[1])
            runtime._live7_pose_stamp = (received, stamp)
    return clock, stamp, pose is not None


def completed_receipt(record, stamp):
    """Compact native outcomes only; never derive a destination from a skill/why."""
    if (record.get("ev") == "menu_recovery_outcome" and record.get("source") == "native_post_recovery_readback"
            and record.get("request_id") and record.get("status") == "interrupted"):
        return dict(status="native_verified", effect="interrupted",
                    observed_ts=stamp, **{k: record.get(k) for k in ("text", "skill", "item", "bag_left", "request_id", "source")})
    if record.get("ev") == "item_move_outcome" and record.get("source") == "native_paired_item_move":
        return dict(status="native_verified", effect="stored" if record["destination"] == "chest" else "withdrawn",
                    observed_ts=record["at"], quantity=record["moved"],
                    **{k: record[k] for k in ("item", "name", "quality", "destination", "bag_left", "chest_count",
                                             "chest", "text", "request_id", "source")})
    if record.get("ev") == "item_move_outcome" and record.get("source") == "native_menu_item_diff":
        return dict(status="native_verified", observed_ts=record["at"], quantity=record["moved"],
                    **{k: record[k] for k in ("effect", "item", "name", "quality", "destination", "origin",
                        "bag_left", "bag_delta", "cursor_after", "text", "request_id", "source")})
    if record.get("ev") == "native_fishing_catch" and record.get("request_id") and record.get("item"):
        return dict(status="native_verified", effect="caught", observed_ts=stamp, source="native_fishing_catch",
                    **{k: record[k] for k in ("item", "name", "quantity", "destination", "request_id") if k in record})
    if record.get("ev") == "part_live_row":
        evidence = record.get("evidence") or {}
        if record.get("verified") is not True and evidence.get("landed_catch"):
            return completed_receipt(dict(evidence["landed_catch"], ev="native_fishing_catch"), stamp)
        observed = (record.get("evidence") or {}).get("observed") or {}
        if record.get("verified") is True and observed.get("effect") == "caught":
            receipt = completed_receipt(dict(ev="finish_verified", verdict=dict(status="achieved",
                reason=record.get("reason"), observed=observed)), stamp)
            if receipt is not None and type(observed.get("kind_count_session")) is int:
                receipt["kind_count_session"] = observed["kind_count_session"]
            return receipt
        if ((record.get("verified") is True or record.get("outcome_class") == "PARTIAL")
                and (observed.get("actual_stored") or observed.get("actual_shipped"))):
            return dict(status="native_verified", effect="stored" if observed.get("actual_stored") else "shipped",
                        destination="chest" if observed.get("actual_stored") else "shipping_bin",
                        observed_ts=stamp, observed=deepcopy(observed), source="native_chosen_transfers",
                        choice="selected by this agent", target=(record.get("evidence") or {}).get("objective"))
        if record.get("verified") is not True and evidence.get("inventory_gains"):
            return dict(status="native_verified", effect="bag_gain_observed", observed_ts=stamp,
                        gains=deepcopy(evidence["inventory_gains"]), source="native_inventory_delta",
                        cause="unknown", action_status=record.get("outcome_class"))
    if (record.get("ev") == "native_ground_item" and record.get("source") == "native_fishing_catch_menu"
            and record.get("caught") is True and record.get("request_id")):
        return dict(status="native_verified", effect="caught", destination="ground", observed_ts=stamp,
                    **{k: record[k] for k in ("item", "name", "quantity", "request_id", "source") if k in record})
    if (record.get("ev") == "purchase_lifecycle" and record.get("state") == "completed"
            and record.get("source") == "generic_ui_native_readback"
            and record.get("reason") == "observed_purchase_currency_and_inventory_delta"
            and record.get("request_ids") and not record.get("held")
            and type(record.get("inventory_delta")) is int and record["inventory_delta"] > 0
            and type(record.get("currency_delta")) is int and record["currency_delta"] <= 0
            and (record.get("currency") != 0 or record.get("money_delta") == record["currency_delta"])):
        return dict(status="native_verified", effect="purchased", destination="inventory", observed_ts=stamp,
                    quantity=record["inventory_delta"], **{k: record[k] for k in (
                        "item", "money_delta", "inventory_delta", "currency", "currency_delta", "request_ids", "source")})
    if record.get("ev") == "native_cursor_destination" and record.get("source") == "native_cursor_conservation":
        return dict(status="native_verified", observed_ts=stamp,
                    **{k: record[k] for k in ("effect", "destination", "item", "quantity", "request_id", "source", "observed")})
    if (record.get("ev") == "native_transaction" and record.get("state") == "completed"
            and record.get("source") == "generic_ui_native_readback" and record.get("request_id")):
        kind = record.get("kind")
        quantity = record.get("quantity")
        if (kind not in ("ship", "sell") or type(quantity) is not int or quantity <= 0
                or record.get("inventory_delta") != -quantity
                or kind == "ship" and record.get("shipping_delta") != quantity
                or kind == "sell" and not (type(record.get("money_delta")) is int and record["money_delta"] > 0)):
            return None
        return dict(status="native_verified", effect="shipped" if kind == "ship" else "sold",
                    destination="shipping_bin" if kind == "ship" else "shop",
                    observed_ts=stamp, **{k: record[k] for k in (
                        "item", "quantity", "inventory_delta", "money_delta", "shipping_delta", "request_id", "source") if k in record})
    if record.get("ev") != "finish_verified" or not isinstance(record.get("verdict"), Mapping):
        return None
    verdict = record["verdict"]
    observed = verdict.get("observed") or {}
    if (verdict.get("status") == "achieved" and verdict.get("reason") in (
            "observed_fish_caught", "observed_fish_caught_dropped_at_feet")
            and observed.get("effect") == "caught" and observed.get("destination") in ("ground", "inventory")
            and observed.get("request_id")):
        receipt = dict(status="native_verified", observed_ts=stamp, source="native_fishing_catch",
                       **{k: observed[k] for k in ("effect", "destination", "item", "request_id")})
        fish = observed.get("fish") if isinstance(observed.get("fish"), Mapping) else {}
        if isinstance(fish.get("name"), str):  # ADR 113 (a50 T85): speech had only `(O)170`
            from arti_live7_status_facts import catch_kind
            receipt["name"] = fish["name"]
            if kind := catch_kind(fish):
                receipt["kind"] = kind
        return receipt
    if (isinstance(observed, Mapping) and verdict.get("status") == "achieved"
            and verdict.get("reason") == "observed_paired_transfer_objective"
            and type(observed.get("chest_gain")) is int and observed["chest_gain"] > 0
            and observed.get("bag_decrease") == observed["chest_gain"]):
        return dict(status="native_verified", effect="stored", destination="chest", observed_ts=stamp,
                    observed=dict(observed), source="native_paired_transfer")
    return None


def readback(session, facts=None, *, fresh=True, pending=None):
    rows = [r for d in getattr(session.campaign, "days", ()) for r in d.get("skills", ())]
    last = rows[-1] if rows else None
    active = getattr(session, "current", None)
    action = dict(status="pending", skill=pending) if pending else None
    if action is None and active is not None and getattr(active, "status", None) not in ("finished", "failed"):
        action = dict(status="pending", skill=active.call.name)
    if action is None and last:
        observed = (last.get("evidence") or {}).get("observed") or {}
        # Lifecycle success alone is not an effect. Keep the native observation verbatim.
        proven = last.get("verified") is True and bool(observed.get("effect") or observed.get("effects")
            or observed.get("changes") or observed.get("events") or observed.get("chest_gain"))
        action = dict(skill=last.get("skill"), status="native_verified" if proven else "uncertain",
                      observed={k: observed[k] for k in ("kind", "effect", "effects", "changes", "request_id",
                          "chest_gain", "bag_decrease", "transferred", "objective", "events", "actual_shipped",
                          "planned_item_shipped", "item", "done", "fish", "destination") if k in observed} if proven else None, reason=last.get("reason"))
    result = dict(action=action or dict(status="no_native_completion_observed"))
    result["own_crops"] = deepcopy(getattr(session, "_persona_own_crops", None))
    completed = getattr(session, "_persona_completed_action", None)
    if completed is not None:
        result["completed_action"] = dict(completed)
    can = getattr(facts, "can_water", None) if fresh else None
    result["watering_can"] = dict(water_left=can[0], water_max=can[1]) if can is not None else "unknown"
    inventory = getattr(facts, "inventory", None) if fresh and getattr(facts, "inventory_current", False) else None
    result["possession"] = dict(inventory) if isinstance(inventory, Mapping) else "unknown"
    return result
