"""Harvest offer readback, including blocked work. Facts only; no inventory mutation (ADR 67)."""
from collections.abc import Mapping


def home_bag_fact(facts, context, calls):
    """ADR 186: current slots and kinds at home, at every fill level; no packing policy."""
    if not facts.inventory_current or not context.get("inventory_complete") or type(facts.free_slots) is not int:
        return None
    from arti_stardew_economy import hard_dispose_refusal
    rows = [r for r in context.get("inventory", ()) if type(r.get("slot")) is int and r.get("stack", 0) > 0]
    if not rows and any(n for n in facts.inventory.values()):
        return None
    used = len({r["slot"] for r in rows if type(r.get("slot")) is int})
    if rows and used != len(rows):
        return None  # unknown slot basis is not 0/0
    kinds = {"alat": 0, "quest": 0, "benih": 0, "barang lain": 0}
    for r in rows:
        refused = hard_dispose_refusal(r)
        kind = "alat" if refused == "tool" else "quest" if refused == "quest_item" else "benih" if r.get("category") == -74 else "barang lain"
        kinds[kind] += 1
    sinks = describe(facts, context, calls)["make_space"]
    labels = {"ship": "kotak pengiriman", "sell": "toko", "store_in_chest": "peti", "drop_or_trash": "tas: buang/jatuhkan"}
    ways = [labels[k] + " lewat " + v["via"] for k, v in sinks.items() if v["available"]]
    if any(c.name == "makan" for c in calls):
        ways.append("makan")
    return "%s (%d/%d); kosong %d slot; isi per slot: %s. Menu ditawarkan: %s." % (
        "tas penuh" if facts.free_slots == 0 else "tas", used, used + facts.free_slots, facts.free_slots,
        ", ".join("%s %d" % pair for pair in kinds.items()), "; ".join(ways) or "belum ada")


def describe(facts, context, calls, reasons=()):
    ready = []
    for ref, crop in context.get("ready_crops", {}).items():
        ready.append(dict(ref=ref, location=crop.get("location"), tile=crop.get("tile"),
                          item=crop.get("item"), basis="own_last_seen_ready"))
    scene = [ref for ref, target in context.get("targets", {}).items()
             if isinstance(crop := target.get("crop"), Mapping)
             and crop.get("ready_for_harvest") is True and crop.get("dead") is not True]
    offered = any(c.name == "panen" for c in calls)
    names = {c.name for c in calls}
    rows = [r for r in context.get("inventory", ()) if r.get("stack", 0) > 0]
    # ADR 99 (a46 T155: bag_used 13 of 14 beside the native 11 of 12): count bag slots; other rows are not bag room.
    occupied = len({r["slot"] for r in rows if type(r.get("slot")) is int}) or len(rows)
    free = facts.free_slots if facts.inventory_current and context.get("inventory_complete") else None
    capacity = occupied + free if type(free) is int else None
    from scripts.live7.interactions import ordinary_targets
    opened = {r["kind"]: r for r in ordinary_targets(context)
              if any(c.name == "aksi" and c.arg == r["ref"] for c in calls)}
    def sink(skill, kind=None):
        if skill in names:
            return dict(available=True, via=skill)
        row = opened.get(kind)
        return dict(available=bool(row), via="aksi" if row else skill,
                    **({"arg": row["ref"], "scope": "open native menu; choose item/quantity there"} if row else {}))
    return dict(offered=offered, own_ripe=ready[:6], own_ripe_count=len(ready), scene_ripe=len(scene), free_slots=free,
                bag_used=occupied if capacity is not None else None, bag_capacity=capacity,
                offer_needs_free_slots=0,  # ADR 137: native acceptance, not empty-slot count
                exclusion_causes=[] if offered else list(dict.fromkeys(reasons)),
                make_space={"ship": sink("kirim", "shipping_bin"), "sell": sink("jual", "shop_counter"),
                            "store_in_chest": sink("simpan_ke_chest", "chest"), "drop_or_trash": sink("inventori")},
                note="Ripe crops remain ripe when the bag is full. Make-space choices use their observed interfaces; nothing is moved automatically.")


LANDMARK_KINDS = ("building_door", "door", "warp")
RIPE_GROUPS = 4  # groups (crop x landmark) on the line, the largest first; the rest are counted


def ripe_where(context, item_names, place_label):
    """ADR 232 part D: what is ripe on the map she stands on and where, by the building or way out it is nearest to.

    ``context``: the boundary's native context (scene ``targets``: crops with ``ready_for_harvest``, doors and warps
    with their ``tile`` and ``target_location``). ``item_names``: qualified id -> native name she has read.
    ``place_label(location)``: the name her page uses for a place, or None. Whose crop it is is not read: a ripe crop
    she can see is a ripe crop. No tile numbers; None when nothing ripe is in the scene."""
    targets = (context or {}).get("targets") or {}
    ripe, marks = [], {}
    for ref, t in targets.items():
        if not isinstance(t, Mapping):
            continue
        tile = t.get("tile") if isinstance(t.get("tile"), Mapping) else None
        if tile is None or type(tile.get("x")) is not int or type(tile.get("y")) is not int:
            continue
        crop = t.get("crop")
        if isinstance(crop, Mapping) and crop.get("ready_for_harvest") is True and crop.get("dead") is not True:
            ripe.append(((tile["x"], tile["y"]), crop.get("qualified_item_id")))
        elif (t.get("kind") in LANDMARK_KINDS and t.get("npc_only") is not True
              and isinstance(t.get("target_location"), str) and t["target_location"]):
            label = place_label(t["target_location"])
            if label:
                marks.setdefault(label, []).append((tile["x"], tile["y"]))
    if not ripe:
        return None
    groups = {}
    for (x, y), qid in ripe:
        near = min(((abs(x - mx) + abs(y - my), label) for label, tiles in marks.items() for mx, my in tiles),
                   default=None)
        name = (item_names or {}).get(qid) or "tanaman (nama belum diketahui)"
        key = (name, near[1] if near else None)
        groups[key] = groups.get(key, 0) + 1
    order = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1] or ""))
    parts = ["%d %s%s" % (n, name, " dekat " + label if label else "") for (name, label), n in order[:RIPE_GROUPS]]
    rest = sum(n for _, n in order[RIPE_GROUPS:])
    return "di peta ini %d siap panen: %s%s" % (len(ripe), ", ".join(parts), "; %d lainnya" % rest if rest else "")
