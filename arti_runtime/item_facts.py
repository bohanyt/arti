"""What is in her bag and what the game says it is for: a human's item tooltip as facts (ADR 88).

Facts only. Every word comes from the game's own item text or its native eligibility reads. Nothing here filters an
offer, ranks a choice or names an item in code; she reads the facts and chooses.
"""
from collections.abc import Mapping

DESCRIBED_PER_PAGE = 3  # the page shows a few tooltips at a time; the whole bag stays on the open menu
DESCRIPTION_CHARS = 120
USES_CHARS = 240        # a minimal page carries this line (the MINIMAL_PAGE_FACTS bound)
ITEM_KINDS = ("item_seen", "item_obtained")
GROUND_ROW = ": materialized ground item"  # the ledger's name-only row for ground debris, not a description


def native_descriptions(rows):
    """Qualified id -> the game's ``Name: description`` from native item discovery rows (first row wins)."""
    known = {}
    for row in rows or ():
        if not isinstance(row, Mapping) or row.get("kind") not in ITEM_KINDS:
            continue
        subject, text = row.get("subject"), row.get("text")
        if isinstance(subject, str) and isinstance(text, str) and text.strip() and not text.endswith(GROUND_ROW):
            known.setdefault(subject, text.strip())
    return known


def _short(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def next_descriptions(bag, known, told, seen_today):
    """Up to DESCRIBED_PER_PAGE tooltips she has not been shown today, items newly in her bag first.

    ``bag`` is qualified ids in bag order. ``told`` and ``seen_today`` are that day's sets and are updated here. Tools
    and weapons are left out: they have their own ``day.tools`` line and the open menu still describes them."""
    goods = [q for q in dict.fromkeys(bag) if not str(q).startswith(("(T)", "(W)"))]
    fresh = [q for q in goods if q not in seen_today] if seen_today else []
    seen_today.update(goods)
    waiting = [q for q in fresh + goods if q in known and q not in told]
    shown = list(dict.fromkeys(waiting))[:DESCRIBED_PER_PAGE]
    told.update(shown)
    from arti_runtime.fact_names import text
    return [_short(text(known[q]), DESCRIPTION_CHARS) for q in shown]


def _joined(names, room):
    """Names that fit ``room`` characters, then a count of the rest (every name stays in the progress read)."""
    kept, left = [], len(names)
    for name in names:
        if kept and len(", ".join(kept + [name])) > room:
            break
        kept.append(name)
        left -= 1
    return ", ".join(kept) + (" +%d lagi" % left if left else "")


def _museum_names(museum, held):
    rows = (museum or {}).get("bag_matches") if isinstance(museum, Mapping) else None
    if not isinstance(rows, list):
        return []
    return list(dict.fromkeys(r[2] for r in rows if isinstance(r, (list, tuple)) and len(r) >= 3
                              and r[1] in held and isinstance(r[2], str)))


def _bundle_parts(cc, held):
    """``Wood: cocok bundle Construction``, per item, rooms already open first."""
    rows = cc.get("bag_matches") if isinstance(cc, Mapping) else None
    if not isinstance(rows, list) or cc.get("joja") or cc.get("cc_complete"):
        return []
    fits = {}
    for row in sorted((r for r in rows if isinstance(r, Mapping)), key=lambda r: r.get("room_open") is not True):
        if row.get("item") in held and row.get("name") and row.get("bundle_name"):
            shut = " (ruangnya belum terbuka)" if row.get("room_open") is False else ""
            bundles = fits.setdefault(row["name"], [])
            if row["bundle_name"] + shut not in bundles:
                bundles.append(row["bundle_name"] + shut)
    return ["%s: cocok bundle %s" % (name, ", ".join(bundles[:2])) for name, bundles in fits.items()]


def readable_progress(progress, memory):
    """ADR 113 (a50: 18 of 144 pages holding a fitting Parsnip had no line, each from a frame with no `progress` read: its hash
    moves with the bag and its chunks arrive over later frames). Keeps this runner's last read of the native matches; ``bag_uses``
    still names only what she holds now. A read frame always replaces it. Nothing read yet stays unknown."""
    if isinstance(progress, Mapping) and ("cc" in progress or "museum" in progress):
        memory.clear()
        memory.update({k: progress[k] for k in ("cc", "museum") if k in progress})
        return progress
    return dict(memory) if memory else progress


def bag_uses(progress, held):
    """One line naming her bag items the game reads as museum-eligible or bundle-matching, or None.

    ``progress`` is the native progress read and ``held`` her current bag ids, so an item already gone is not named."""
    if not isinstance(progress, Mapping):
        return None
    cc = progress.get("cc") if isinstance(progress.get("cc"), Mapping) else {}
    museum = _museum_names(progress.get("museum"), held)
    bundles = _bundle_parts(cc, held)
    if not museum and not bundles:
        return None
    parts = []
    if museum:
        parts.append("%s: bisa disumbang ke museum (belum ada di museum; lewat Gunther)" % _joined(museum, 80))
    notes = []
    if bundles:
        if cc.get("unlocked") is False:
            notes.append("pintu Community Center masih terkunci")
        if cc.get("can_read_junimo_text") is False:
            notes.append("tulisan Junimo belum terbaca")
    room = USES_CHARS - len("; ".join(parts + notes)) - 30  # 30: room for the count of what did not fit
    used = 0
    for part in bundles:
        if len(part) + 2 > room:
            break
        parts.append(part)
        room -= len(part) + 2
        used += 1
    if used < len(bundles):
        parts.append("+%d barang cocok bundle lain" % (len(bundles) - used))
    return _short("; ".join(parts + notes), USES_CHARS)
