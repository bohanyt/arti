"""Current native collection acceptance (ADR 137), never an empty-slot heuristic."""
from collections.abc import Mapping


def acceptance(context):
    """ADR 139: compact decision dependencies, including yes/no/unknown and every refusal reason.

    Item + tile identifies the target on this page's current map. Native refs stay
    in the canonical context; repeating them here spends more bytes than the facts.
    No row/character budget may remove a predicate an action depends on.
    """
    from arti_runtime.fact_names import item_name
    rows = []
    for row in context.get("collection_fit", ()):
        if isinstance(row, Mapping):
            rows.append(["pickup", item_name(row.get("item"), name=row.get("name")), row.get("tile"),
                         row.get("accepts"), row.get("reason") or ("" if type(row.get("accepts")) is bool else "unreadable")])
    if context.get("collection_fit_status"):
        rows.append(["pickup", None, None, None, context["collection_fit_status"]])
    for row in context.get("extras", ()):
        if isinstance(row, Mapping) and row.get("kind") == "harvest_crop":
            accepts = row.get("inventory_accepts")
            rows.append(["harvest", row.get("label") or row.get("ref"), [row.get("x"), row.get("y")], accepts,
                         row.get("inventory_accepts_reason") or ("inventory_cannot_accept" if accepts is False
                                                                 else "" if accepts is True else "unreadable")])
    refusal = (context.get("mines_ui") or {}).get("extras_refusal")
    if isinstance(refusal, Mapping):
        rows.append([refusal.get("kind"), refusal.get("ref"), [refusal.get("x"), refusal.get("y")],
                     False, refusal.get("reason")])
    if rows:
        return dict(cols=["action", "item", "tile", "accepts", "reason"], rows=rows)
    return None


def harvest_refused(entry, full):
    """One harvest row, judged on its own native fact (a row without a ref is still judged, never by a shared key)."""
    return (isinstance(entry, Mapping) and entry.get("kind") == "harvest_crop"
            and (entry.get("inventory_accepts") is False or full and entry.get("inventory_accepts") is not True))


def harvest_refusals(context, full):
    """Refs of refused harvest rows. pkg77: only real refs; a missing ref was None in this set, and filtering by it
    hid every other ref-less row of the context with it (the shipping bin, so `kirim`, on a full bag)."""
    return {e["ref"] for e in context.get("extras", ()) if harvest_refused(e, full) and isinstance(e.get("ref"), str)}


def pickup_accepted(context):
    """Refs the native fit list says the bag takes now (ADR 137)."""
    return {r["ref"] for r in context.get("collection_fit", ()) if isinstance(r, Mapping)
            and isinstance(r.get("ref"), str) and r.get("accepts") is True}


def approach_refused(context, entry, full):
    """pkg77 (ADR 137): an approach-catalog forage row on a full bag without its own native acceptance is unknown,
    so refused, the same rule pickup_refusals applies to a scene target."""
    return (full and isinstance(entry, Mapping) and entry.get("kind") == "forage"
            and entry.get("ref") not in pickup_accepted(context))


def pickup_refusals(context, full):
    """Missing native acceptance on an older adapter keeps its full-bag refusal, explicitly."""
    rows = {r["ref"]: r for r in context.get("collection_fit", ()) if isinstance(r, Mapping) and isinstance(r.get("ref"), str)}
    from arti_stardew_interact import is_forage_fact
    return {ref for ref, fact in context.get("targets", {}).items() if full and is_forage_fact(fact)
            and rows.get(ref, {}).get("accepts") is not True} | {
                ref for ref, row in rows.items() if row.get("accepts") is not True}


def describe(context):
    """Retain the item and its native refusal even when it has no collecting action."""
    from arti_runtime.fact_names import item_name
    rows = []
    harvest = [dict(item=e.get("label"), name=e.get("label"), tile=[e.get("x"), e.get("y")],
                    accepts=False, reason=e.get("inventory_accepts_reason") or "inventory_cannot_accept") for e in context.get("extras", ())
               if e.get("inventory_accepts") is False]
    for row in (*context.get("collection_fit", ()), *harvest):
        if not isinstance(row, Mapping) or row.get("accepts") is not False:
            continue
        label = item_name(row.get("item"), name=row.get("name"))
        rows.append("%s %s: %s" % (label, row.get("tile"),
                    "tidak muat di tas" if row.get("reason") == "inventory_cannot_accept" else row.get("reason")))
    shown = []
    for row in rows:
        if len("; ".join([*shown, row])) > 320:
            break
        shown.append(row)
    if len(shown) < len(rows):
        shown.append("+%d objek lain (collection_details)" % (len(rows) - len(shown)))
    return "; ".join(shown) or None
