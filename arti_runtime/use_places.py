"""Places where what she carries can be used. Native graph facts, not a written list."""
from arti_runtime.native_rows import native_rows
from arti_stardew_world import travel_options


PURPOSE = {
    "soil": ("tillable soil:", "Tanah bisa dicangkul/ditanami: benih dan cangkul dipakai di sini."),
    "crops": ("planted crops:", "Tanaman tertanam: gembor menyiram di sini."),
    "fish": ("fishing:", "Air dengan data pancing: joran dipakai di sini."),
    "bin": ("shipping bin:", "Kotak kirim: pilih barang; dibayar sesudah tidur."),
    "shop": ("shop counter ", "Konter: stok/harga, beli atau jual."),
}


def _tool(inventory, subtype):
    return any((r.get("tool_subtype") or "") == subtype for r in inventory or ())


def carried_uses(inventory):
    """Which use-kinds her bag and tools currently match, from native fields only."""
    rows = list(inventory or ())
    kinds = set()
    if any(r.get("category") == -74 and r.get("stack", 0) > 0 for r in rows) or _tool(rows, "Hoe"):
        kinds.add("soil")
    if _tool(rows, "WateringCan"):
        kinds.add("crops")
    if _tool(rows, "FishingRod"):
        kinds.add("fish")
    from arti_stardew_economy import hard_dispose_refusal
    sellable = any(hard_dispose_refusal(r) is None and r.get("qualified_item_id")
                   for r in rows if not r.get("tool_subtype") and not r.get("is_tool"))
    if sellable:
        kinds.add("bin")
        kinds.add("shop")
    return kinds


def matches(place, kinds):
    facts = tuple(place.get("facts") or ())
    out = []
    if "soil" in kinds and any(str(f).startswith("tillable soil:") for f in facts):
        out.append("soil")
    if "crops" in kinds and any("planted crops" in str(f) or (
            str(f).startswith("tillable soil:") and "watering can" in str(f)) for f in facts):
        out.append("crops")
    if "fish" in kinds and any(str(f).startswith("fishing:") for f in facts):
        out.append("fish")
    if "bin" in kinds and any(str(f).startswith("shipping bin:") for f in facts):
        out.append("bin")
    if "shop" in kinds and any(str(f).startswith("shop counter ") for f in facts):
        out.append("shop")
    return out


def purpose_text(kind):
    return PURPOSE.get(kind, ("", ""))[1]


def travel(context, facts, *, known=None, kinds=None, options=None):
    """Startable trips to places where what she carries can be used, with native purpose."""
    from arti_runtime.experience_facts import place_sources
    inventory = context.get("inventory") or ()
    kinds = carried_uses(inventory) if kinds is None else kinds
    if not kinds:
        return []
    known = place_sources(context.get("world_knowledge") or {}) if known is None else known
    here = facts.location
    graph = (context.get("world_knowledge") or {}).get("world_graph") or {}
    places = {p.get("id"): p for p in native_rows(graph.get("places", ())) if isinstance(p, dict) and p.get("id")}
    rows = []
    for option in (travel_options(context, here, now=facts.time_of_day) if options is None else options):
        loc = option["location"]
        if loc not in known:
            continue
        used = matches(places.get(loc) or {}, kinds)
        if not used:
            continue
        rows.append(dict(option, use_kinds=used))
    return rows
