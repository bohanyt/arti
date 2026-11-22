"""ADR 140/147: native per-item outcomes shared by pages and chest memory."""
from collections import Counter
import re
import time


def counts(view, side, quality=None):
    out = Counter()
    for slot in view.get(side, ()):
        item = slot.get("item") or {}
        if quality is None or item.get("quality") == quality:
            if item.get("qualified_item_id") and type(item.get("stack")) is int:
                out[item["qualified_item_id"]] += item["stack"]
    return out


def publish(runtime, args, request_id, before, after, *, before_counts=None):
    """Only call after authenticated completion and fresh native readback.

    Positive paired deltas are actual moved quantities (including a partial
    stack); unknown or non-conserved effects get no completion claim.
    """
    if not request_id or not after or not before and before_counts is None:
        return None
    mode = args.get("mode", "put")
    if mode not in ("put", "quick", "take_out"):
        return None
    qid, quality = args["qualified_item_id"], args["quality"]
    if (after.get("source_fact_id") != args.get("source_fact_id")
            or after.get("container_token") != args.get("container_token") or after.get("cursor") is not None):
        return None
    bag, chest = counts(after, "actor_slots", quality)[qid], counts(after, "container_slots", quality)[qid]
    old_bag, old_chest = (before_counts if before_counts is not None else
                         (counts(before, "actor_slots", quality)[qid], counts(before, "container_slots", quality)[qid]))
    direction = -1 if mode == "take_out" else 1
    moved = (old_bag - bag) * direction
    if moved <= 0 or (chest - old_chest) * direction != moved:
        return None
    session = getattr(runtime, "_skill_session", None)
    if session is None or getattr(session, "mode", None) != "part_live":
        return None
    seen = getattr(session, "_item_move_requests", None)
    if seen is None:
        seen = session._item_move_requests = []
    receipt_key = (request_id, qid, quality)
    if receipt_key in seen:
        return None
    seen.append(receipt_key)
    del seen[:-64]
    live = session.campaign.live
    from arti_runtime.fact_names import item_name
    name = item_name(qid, live.item_names)
    destination = "inventory" if direction == -1 else "chest"
    row = dict(ev="item_move_outcome", at=time.time(), request_id=request_id,
               source="native_paired_item_move", item=qid, name=name, quality=quality,
               moved=moved, destination=destination, chest=args["source_fact_id"],
               bag_left=counts(after, "actor_slots")[qid], bag_quality_left=bag,
               chest_count=counts(after, "container_slots")[qid],
               date=list(session.campaign.date), clock=getattr(runtime._state.latest(runtime._binding_view()[1]), "time_of_day", None))
    from arti_runtime.chest_memory import parse_ref
    parsed = parse_ref(row["chest"])
    where = "peti %s %d,%d" % (parsed[0], *parsed[1]) if parsed else "peti yang dibuka"
    row["text"] = "%s: pindah %d ke %s; tersisa %d di tas" % (
        name, moved, "tas dari " + where if destination == "inventory" else where, row["bag_left"])
    runtime._journal(row)
    return row


def remember(session, row):
    last = getattr(session, "_last_item_move_rows", [])
    if not last or last[0]["request_id"] != row["request_id"]:
        last = session._last_item_move_rows = []
    last.append(dict(row))
    latest = getattr(session, "_persona_item_moves", None)
    if latest is None:
        latest = session._persona_item_moves = {}
    key = row["item"], row["quality"]
    latest.pop(key, None)
    latest[key] = dict(row)
    while len(latest) > 12:
        del latest[next(iter(latest))]
    if "chest_count" in row:
        session.campaign.live.chest_memory.outcome(row)


def _bag(ledger):
    """A partial/malformed bag cannot establish absence. Slots are native, zero based."""
    rows, capacity = ledger.get("inventory"), ledger.get("capacity")
    if (not isinstance(rows, list) or type(capacity) is not int or capacity < 1
            or len(rows) < capacity or any(not isinstance(r, dict) for r in rows)):
        return None
    slots = {}
    for r in rows:
        slot, qid, n, quality = (r.get(k) for k in ("slot", "item", "stack", "quality"))
        if (type(slot) is not int or slot < 0 or slot in slots or not isinstance(qid, str)
                or type(n) is not int or n < 0 or type(quality) is not int or bool(qid) != (n > 0)):
            return None
        slots[slot] = r
    if not set(range(capacity)) <= slots.keys():
        return None
    return slots


def _held(ledger):
    block = ledger.get("cursor_settle") or {}
    if block.get("complete") is not True or "cursor" not in block:
        return None
    cursor = block["cursor"]
    if cursor is None:
        return {}
    if (not isinstance(cursor, dict) or not cursor.get("item") or type(cursor.get("stack")) is not int
            or cursor["stack"] < 1 or type(cursor.get("quality")) is not int):
        return None
    return cursor


def _quantity(rows, key):
    return sum(r["stack"] for r in rows if (r.get("item"), r.get("quality")) == key)


def _slots(rows, key):
    return {s: r["stack"] for s, r in rows.items() if (r["item"], r["quality"]) == key and r["stack"]}


def _slot_text(slots):
    return ", ".join("slot %d x%d" % (s + 1, n) for s, n in sorted(slots.items()))


def _outside(ledger, key):
    """Native non-bag slot counts; never count the menu's duplicate bag widgets."""
    rows = [r for r in ledger.get("ui_slots", ())
            if not re.search(r"(?:^|\.)inventory\.inventory\[\d+\]$", r.get("field_path", ""))]
    return _quantity(rows, key)


def publish_menu(runtime, args, result, view, fresh):
    """After owned native readback, diff every bag item/slot and the held item.

    There is no menu-type dispatch. Bag membership is independent of cursor
    ownership; a disappearing cursor is never called a drop without native
    conservation. The same diff covers swaps, purchases, recipes and mod items.
    """
    session = getattr(runtime, "_skill_session", None)
    if (session is None or getattr(session, "mode", None) != "part_live" or view is None or fresh is None):
        return []
    from arti_stardew_generic_ui import ui_readback
    proof = ui_readback(view, result, fresh)
    if proof is None:
        return []
    before, after = proof["before"], proof["after"]
    old, new = _bag(before), _bag(after)
    held_before, held_after = _held(before), _held(after)
    if old is None or new is None or held_before is None or held_after is None:
        return []
    import json
    receipt = json.loads(result.readback).get("cursor_settle") or {}
    target = next((t for t in before["cursor_settle"].get("targets", ())
                   if str(t.get("id")) == str(args.get("choice", args.get("snapped_id")))), {})
    keys = {(r["item"], r["quality"]) for r in [*old.values(), *new.values(), held_before, held_after] if r.get("item")}
    names = dict(session.campaign.live.item_names)
    names.update({r["item"]: r["name"] for r in [*old.values(), *new.values(), held_before, held_after]
                  if r.get("item") and r.get("name")})
    from arti_runtime.fact_names import item_name
    rows = []
    for key in sorted(keys):
        qid, quality = key
        old_slots, new_slots = _slots(old, key), _slots(new, key)
        b, a = sum(old_slots.values()), sum(new_slots.values())
        hb, ha = _quantity([held_before], key), _quantity([held_after], key)
        if old_slots == new_slots and hb == ha:
            continue
        seen = session.__dict__.setdefault("_item_move_requests", [])
        receipt_key = (result.request_id, qid, quality)
        if receipt_key in seen:
            continue
        delta = a - b
        moved = max(abs(delta), abs(ha - hb), sum(max(0, n - new_slots.get(s, 0)) for s, n in old_slots.items()))
        destination, origin, effect = "unknown", "unknown", "bag_changed"
        container = (before.get("menu_container") or {}).get("kind")
        outside = _outside(after, key) - _outside(before, key)
        shipping = _quantity(after.get("shipping_items", ()), key) - _quantity(before.get("shipping_items", ()), key)
        if delta > 0:
            destination, effect = "inventory", "received"
            if hb - ha == delta:
                origin = "cursor"
            elif shipping == -delta:
                origin = "shipping_bin"
            elif outside == -delta:
                origin = container or "menu"
        elif delta < 0:
            if ha - hb == -delta:
                destination, effect = "cursor", "held"
            elif shipping == -delta:
                destination, effect = "shipping_bin", "shipped"
            elif outside == -delta:
                destination, effect = container or "menu", "stored"
        elif a and old_slots != new_slots:
            destination, effect = "inventory", "rearranged"
        # Disposal can happen on a later press with an unchanged bag. It must
        # name the actual held item and native conserved quantity, not the why.
        if (hb > ha and receipt.get("Conserved") is True and receipt.get("Status") in ("Settled", "Partial")
                and receipt.get("Moved") == hb - ha and target.get("kind") in ("drop", "trash")):
            destination = "ground" if target["kind"] == "drop" else "trash"
            effect = "dropped" if destination == "ground" else "trashed"
        elif ha > hb and not delta:
            destination, effect = "cursor", "held"
        if before.get("shop") and type(before.get("money")) is int and type(after.get("money")) is int:
            money_delta = after["money"] - before["money"]
            if delta < 0 and hb == ha == 0 and money_delta > 0 and destination == "unknown":
                destination, effect = "shop", "sold"
            elif delta > 0 and hb == ha == 0 and money_delta < 0 and origin == "unknown":
                origin, effect = "shop", "purchased"
        places = dict(cursor="tangan", ground="tanah", trash="tempat sampah", chest="peti",
                      shipping_bin="bin pengiriman", shop="toko", menu="menu")
        if delta > 0:
            text = "masuk %d ke tas (%s)" % (delta, _slot_text(new_slots))
            if origin in places:
                text += " dari " + places[origin]
        elif delta < 0:
            text = "keluar %d dari tas (%s); %s" % (-delta, _slot_text(old_slots),
                "ke " + places[destination] if destination in places else "tujuan tidak terbaca")
        elif old_slots != new_slots:
            text = "tetap di tas: %s -> %s" % (_slot_text(old_slots), _slot_text(new_slots))
        else:
            text = "di tangan %d -> %d" % (hb, ha)
        if destination in ("ground", "trash") and not delta:
            text += "; pindah %d ke %s" % (hb - ha, places[destination])
        if ha:
            text += "; di tangan %d" % ha
        name = item_name(qid, names)
        # Qualified IDs/quality can be shared by distinct native display names.
        # Attribute this bag delta to the actual departing/arriving names.
        native_names = []
        if delta:
            from collections import Counter
            old_names=Counter();new_names=Counter()
            for ledger,totals in ((old,old_names),(new,new_names)):
                for value in ledger.values():
                    if ((value['item'],value['quality'])==key and isinstance(value.get('name'),str) and value['name']
                            and value['name']!=value['item']):
                        totals[value['name']]+=value['stack']
            changed_names=(old_names-new_names) if delta<0 else (new_names-old_names)
            if sum(changed_names.values())==abs(delta):
                native_names=[dict(name=n,moved=v) for n,v in sorted(changed_names.items())]
                name=' / '.join(r['name'] for r in native_names)
                if len(native_names)>1:
                    text+='; '+', '.join('%s x%s'%(r['name'],r['moved']) for r in native_names)
        bag_left = sum(r["stack"] for r in new.values() if r["item"] == qid)
        if quality:
            text = "kualitas %d; %s" % (quality, text)
        row = dict(ev="item_move_outcome", at=time.time(), request_id=result.request_id,
            source="native_menu_item_diff", item=qid, name=name, quality=quality, moved=moved, native_names=native_names,
            effect=effect, destination=destination, origin=origin, bag_before=b, bag_left=bag_left,
            bag_delta=delta, bag_quality_left=a, cursor_before=hb, cursor_after=ha,
            slots_before=old_slots, slots_after=new_slots, date=list(session.campaign.date),
            clock=getattr(runtime._state.latest(runtime._binding_view()[1]), "time_of_day", None),
            text="%s: %s; tersisa %d di tas" % (name, text, bag_left))
        seen.append(receipt_key)
        del seen[:-64]
        runtime._journal(row)
        rows.append(row)
    return rows


def publish_changes(runtime, request_id, before, after):
    """Native slot/cursor presses use the same per-item outcome, once settled."""
    session = getattr(runtime, "_skill_session", None)
    if session is None or getattr(session, "mode", None) != "part_live" or after is None:
        return
    pending = getattr(session, "_chest_cursor_origin", None)
    if pending and pending.get("source_fact_id") != after.get("source_fact_id"):
        session._chest_cursor_origin = None
        pending = None
    if after.get("cursor") is not None:
        # Nothing has reached a final bag/chest destination yet. Preserve the
        # original native baseline through a sequence of partial cursor settles.
        session._chest_cursor_origin = pending or before
        return
    session._chest_cursor_origin = None
    before = pending or before
    if (before.get("source_fact_id"), before.get("container_token")) != (after.get("source_fact_id"), after.get("container_token")):
        return
    keys = {(s["item"]["qualified_item_id"], s["item"]["quality"])
            for side in ("actor_slots", "container_slots") for s in before.get(side, ()) if s.get("item")}
    for qid, quality in sorted(keys):
        old_chest, new_chest = counts(before, "container_slots", quality)[qid], counts(after, "container_slots", quality)[qid]
        if old_chest == new_chest:
            continue
        publish(runtime, dict(qualified_item_id=qid, quality=quality,
            source_fact_id=after["source_fact_id"], container_token=after["container_token"],
            mode="take_out" if new_chest < old_chest else "put"), request_id, before, after)


def lines(session):
    """Keep the whole last press plus recent item positions; all explicitly dated."""
    rows = list(getattr(session, "_persona_item_moves", {}).values())
    last = getattr(session, "_last_item_move_rows", [])
    recent = [r for r in reversed(rows) if not last or r["request_id"] != last[0]["request_id"]]
    shown = list(reversed(last)) + recent[:max(0, 4 - len(last))] if last else list(reversed(rows[-4:]))
    result = [r["text"] + " (terakhir dibaca %s %s)" % (r["date"], r["clock"]) for r in shown]
    sitting = getattr(session, '_bag_sitting_result', None)
    if sitting:
        result.insert(0, 'Permintaan tas terakhir (%s): %s; menu %s; %s (dibaca %s %s)' % (sitting['verb'],
            '; '.join('%s kualitas %s: pindah %s/%s; belum %s' % (
                r.get('name') or r['item'], r['quality'], r['moved'], r['requested'], '?' if r['remaining'] is None else r['remaining']) for r in sitting['items']),
            'tertutup' if sitting['closed'] else 'belum terverifikasi tertutup', sitting['reason'], sitting['date'], sitting['clock']))
    return result
