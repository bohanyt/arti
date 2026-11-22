"""Bounded scene/bag facts beside a one-action receipt (ADR 104). No native IO or policy."""
from collections import Counter
from collections.abc import Mapping
import time

KINDS = 4


def snapshot_fact(snapshot, *, date, stamp, receipt_item=None, names=None, event_active=None):
    """Aggregate the existing baseline, including crops outside her own planting ledger.

    The merged scene can lag outside the local window. Never label it a complete farm census.
    Unknown watering is not dry, ready crops do not need watering, and bare soil is not a crop.
    """
    from arti_stardew_seeds import CROP_DATA, CROP_SEEDS
    crops = [c for c in (snapshot.get("crops") or {}).values() if isinstance(c, Mapping)
             and (c.get("phase") is not None or c.get("qualified_item_id") is not None)]
    live = [c for c in crops if c.get("dead") is False]
    growing = [c for c in live if c.get("ready_for_harvest") is False]
    counts = dict(total=len(crops), growing=len(growing), ready=sum(c.get("ready_for_harvest") is True for c in live),
                  dead=sum(c.get("dead") is True for c in crops), watered=sum(c.get("watered") is True for c in live),
                  dry=sum(c.get("watered") is False for c in growing))
    counts["water_unknown"] = sum(c.get("watered") not in (True, False) for c in growing)
    kinds = Counter(c.get("qualified_item_id") or "unknown" for c in crops)
    def crop_name(qid):
        return str((names or {}).get(qid) or CROP_DATA.get(qid, (qid,))[0])[:36]
    counts["kinds"] = [{"item": q, "name": crop_name(q), "count": n} for q, n in sorted(kinds.items())[:KINDS]]
    counts["other_kinds"] = max(0, len(kinds) - KINDS)
    inventory = snapshot.get("inventory")
    seeds = None
    if isinstance(inventory, Mapping):
        held = [(q, n) for q, n in sorted(inventory.items()) if q in CROP_SEEDS and type(n) is int and n > 0]
        seeds = dict(total=sum(n for _, n in held), kinds=[dict(item=q, name=CROP_SEEDS[q][0][:36], count=n)
                      for q, n in held[:KINDS]], other_kinds=max(0, len(held) - KINDS), scope="known crop seeds in bag")
    value = dict(scope="observed scene; unseen tiles unknown", crops=counts, seeds=seeds)
    if receipt_item and isinstance(inventory, Mapping):
        value["receipt_item_in_bag"] = dict(item=receipt_item, count=inventory.get(receipt_item, 0))
    if type(event_active) is bool:
        value["event_active"] = event_active  # native event/cutscene flag, never inferred from a calendar day
    return dict(value=value, place=snapshot.get("location"), date=date, time=snapshot.get("time_of_day"),
                observed_ts=stamp, source="native_scene_and_bag")


def publish(runtime, session, facts, motor, context):
    """Only already-read, same-identity, complete frames; return nothing on a held/foreign read."""
    from arti_stardew_live7 import mind_label
    if (facts is None or not facts.inventory_current or not context.get("ui_complete")
            or not context.get("inventory_complete") or runtime._date_tuple(motor) != session.campaign.date
            or tuple((context.get("identity") or ())[:5]) != session.campaign.epoch):
        return None
    received = getattr(motor, "diterima_pada", None)
    if type(received) not in (int, float) or not 0 <= time.monotonic() - received <= 45:
        return None
    snapshot = dict(inventory=dict(facts.inventory), location=facts.location, time_of_day=motor.time_of_day,
                    crops={ref: f["crop"] for ref, f in context.get("targets", {}).items()
                           if isinstance(f.get("crop"), Mapping)})
    ui = context.get("mines_ui") or {}
    names = {r.get("qualified_item_id"): r.get("name") for r in context.get("inventory", ())}
    completed = getattr(session, "_persona_completed_action", None) or {}
    from arti_stardew_skills import event_up
    fact = snapshot_fact(snapshot, date=mind_label(session.campaign.date),
                         stamp=time.time() - (time.monotonic() - received), names=names,
                         receipt_item=completed.get("item"), event_active=event_up(ui))
    session._persona_scene_state = fact
    # ADR 172: describe already captured native state, never the chosen skill/why.
    from arti_runtime.attention_desk import scene_facts
    previous = getattr(runtime, "_attention_pose", None)
    tile = getattr(motor, "tile", None)
    pose = (motor.location, tuple(tile)) if isinstance(tile, (list, tuple)) and len(tile) == 2 else None
    fact["value"]["attention"] = scene_facts(event_active=event_up(ui), menu=motor.active_menu,
        combat=context.get("combat") or {}, walking=(previous[1] != pose[1]
            if previous is not None and pose is not None and previous[0] == pose[0] else None))
    runtime._attention_pose = pose
    return fact


def receipt_line(fact):
    """Numbers with their native scope, short enough for the existing 180-character moment."""
    v = (fact or {}).get("value") or {}
    c, s = v.get("crops"), v.get("seeds")
    if not c:
        return ""
    return (f"scene: {c['watered']}/{c['total']} watered, {c['dry']} dry growing, {c['ready']} ready"
            + (f"; bag: {s['total']} crop seeds" if s is not None else ""))
