"""Progress readback from verified skill receipts (ADR 36)."""


def harvested_items(days):
    total = 0
    for day in days or ():
        for row in day.get("skills", ()):
            if row.get("skill") != "panen" or row.get("verified") is not True:
                continue
            observed = (row.get("evidence") or {}).get("observed") or {}
            effects = [e for e in observed.get("effects", ()) if e.get("kind") == "harvest_crop"]
            total += sum(max(0, e["delta"]) if type(e.get("delta")) is int else 1 for e in effects) if effects else 1
    return total


def harvest_names(row):
    """`` Parsnip x2`` for a verified panen row (ADR 99); empty when the receipt names no crop."""
    from arti_stardew_seeds import crop_name
    observed = (row.get("evidence") or {}).get("observed") or {}
    if row.get("skill") != "panen" or not isinstance(observed, dict):
        return ""
    return "".join(" %s x%d" % (crop_name(e["item"]), e["delta"] if type(e.get("delta")) is int else 1)
                   for e in observed.get("effects", ()) if e.get("kind") == "harvest_crop" and e.get("item"))


DAY_KINDS = 4


def day_harvest(rows):
    """``hari ini panen: Parsnip 3`` from one day's verified panen effects (ADR 110; a48 T111), or None. Receipts name one
    harvest at a time; this is their sum by crop, with no new read. Named crops only, four at most, then a count of the rest."""
    from arti_stardew_seeds import crop_name
    totals = {}
    for row in rows or ():
        if row.get("skill") != "panen" or row.get("verified") is not True:
            continue
        for effect in ((row.get("evidence") or {}).get("observed") or {}).get("effects", ()):
            if effect.get("kind") == "harvest_crop" and effect.get("item"):
                from arti_runtime.fact_names import item_name
                names = (row.get("evidence") or {}).get("names")
                name = item_name(effect["item"], names, effect.get("name")) if names is not None else crop_name(effect["item"])
                totals[name] = totals.get(name, 0) + (max(0, effect["delta"]) if type(effect.get("delta")) is int else 1)
    if not totals:
        return None
    shown = ", ".join("%s %d" % item for item in list(totals.items())[:DAY_KINDS])
    return "hari ini panen: " + shown + (" +%d jenis lain" % (len(totals) - DAY_KINDS) if len(totals) > DAY_KINDS else "")
