"""Each open quest's current native objective, plus a concrete game condition when the data holds one (ADR 197)."""
from arti_runtime.native_rows import native_rows
import re
from collections.abc import Mapping


def _text(record):
    facts = record.get("facts") if isinstance(record.get("facts"), Mapping) else {}
    return " ".join(str(record.get(k) or "") for k in ("title", "text")) + " " + " ".join(
        str(facts.get(k) or "") for k in ("objective", "description", "target_location", "search_location"))


def _place_lock(catalog, place_id):
    if not place_id:
        return None
    for p in native_rows(((catalog or {}).get("world_graph") or {}).get("places", ())):
        if isinstance(p, Mapping) and p.get("id") == place_id and p.get("locked"):
            return str(p["locked"])
    return None


def _progress_blocks(catalog):
    progress = catalog.get("progress") if isinstance(catalog.get("progress"), Mapping) else {}
    for key, block in progress.items():
        if isinstance(block, Mapping) and (block.get("unlock_location") or block.get("translation_location")
                                           or block.get("unlock_conditions") or block.get("translation_conditions")
                                           or "can_read_junimo_text" in block):
            yield key, block


def _tokens(value):
    if not isinstance(value, str) or not value:
        return ()
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    return {value.casefold(), spaced.casefold()}


def _about_here(record, block, here):
    facts = record.get("facts") if isinstance(record.get("facts"), Mapping) else {}
    target = facts.get("target_location") or facts.get("search_location")
    if here and target == here:
        return True
    text = _text(record).casefold()
    names = set()
    for loc in (here, target, facts.get("search_location"),
                block.get("unlock_location"), block.get("translation_location")):
        names.update(_tokens(loc))
    return any(n in text for n in names if len(n) >= 4)


def extras(record, catalog, here=None):
    """Concrete mail/event/place conditions from native progress and world locks, never a quest-id table."""
    bits = []
    facts = record.get("facts") if isinstance(record.get("facts"), Mapping) else {}
    if lock := _place_lock(catalog, facts.get("target_location") or facts.get("search_location")):
        bits.append("syarat tempat: " + lock)
    for _, block in _progress_blocks(catalog or {}):
        if not _about_here(record, block, here):
            continue
        if block.get("can_read_junimo_text") is False:
            bits.append("tulisan di sini belum bisa dibaca")
        note = block.get("junimo_note") if isinstance(block.get("junimo_note"), Mapping) else {}
        if note.get("text") is None and block.get("can_read_junimo_text") is False:
            bits.append("catatan belum terbaca")
        if block.get("translation_location"):
            bits.append("belajar membaca di " + str(block["translation_location"]))
        for key, label in (("translation_conditions", "syarat baca"), ("unlock_conditions", "syarat buka")):
            rows = [str(x) for x in (block.get(key) or ()) if x not in (None, "", [])]
            if rows:
                bits.append(label + ": " + "; ".join(rows[:4]))
    return bits[:6]


def line(record, catalog=None, here=None, limit=300, *, context=None, chests=None):
    facts = record.get("facts") if isinstance(record.get("facts"), Mapping) else {}
    title = str(record.get("title") or record.get("id") or "quest").strip()
    objective = str(facts.get("objective") or record.get("text") or facts.get("description") or "").strip(" .")
    core = (title + (": " + objective if objective else ""))[:120]
    extra = []
    from arti_stardew_world import location_arg
    text = _text(record)
    for p in native_rows(((catalog or {}).get("world_graph") or {}).get("places", ())):
        loc = p.get("id")
        if loc and (loc in (facts.get("target_location"), facts.get("search_location")) or any(
                re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.I)
                for n in (loc, p.get("label")) if isinstance(n, str) and len(n) > 3)):
            # The destination row already carries its native display name.
            extra.append("pergi_ke " + location_arg(loc))
    crafting = ((context or {}).get("mines_ui") or {}).get("crafting_known") or {}
    for recipe in list(crafting.get("craftable") or ()) + list(crafting.get("quest_recipes") or ()):
        if recipe.get("makes") != facts.get("item") and not any(
                re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.I)
                for n in (recipe.get("recipe"), recipe.get("name")) if n):
            continue
        needs = recipe.get("needs") or ()
        extra.append("buat %s; %s" % (recipe["recipe"], ", ".join("%s %s/%s" % tuple(n) for n in needs)))
        for chest in (chests or {}).values():
            held = ["%s x%d" % (chest.get("names", {}).get(q, q), count) for q, count in chest.get("items", {}).items()
                    if any(chest.get("names", {}).get(q) == n[0] for n in needs)]
            if held:
                from arti_runtime.chest_memory import _seen
                extra.append("pergi_ke %s: peti %s,%s %s (terlihat %s)" % (
                    location_arg(chest["location"]), *chest["tile"], ", ".join(held), _seen(chest)))
    item = facts.get("item")
    if item and (context or {}).get("inventory_complete") and any(
            r.get("qualified_item_id") == item for r in (context or {}).get("inventory", ())):
        extra.append("di tas: " + str(facts.get("item_name") or item))
    extra += extras(record, catalog or {}, here)
    text = (core + "; " + "; ".join(dict.fromkeys(extra))) if extra else core
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def lines(quests, catalog=None, here=None, limit=12, *, context=None, chests=None):
    """Indonesian page facts for each open native quest, current objective first."""
    out = []
    for q in quests or ():
        if not isinstance(q, Mapping) or q.get("status") not in (None, "active"):
            continue
        out.append(line(q, catalog, here, context=context, chests=chests))
        if len(out) >= limit:
            break
    more = sum(1 for q in (quests or ()) if isinstance(q, Mapping) and q.get("status") in (None, "active")) - len(out)
    if more > 0:
        out.append("+%d quest lagi" % more)
    return out
