"""Facts that end no-effect loops: repeats without a native change, owned rods, ways to make space (ADR 79).

Facts only. Nothing here filters an offer, ranks a choice or moves an item; she reads the facts and chooses.
"""
from collections import Counter
from collections.abc import Mapping

RODS = ("(T)BambooPole", "(T)TrainingRod", "(T)FiberglassRod", "(T)IridiumRod", "(T)AdvancedIridiumRod")
CHEST = "(BC)130"
MIN_REPEATS = 2      # one no-change row is ordinary; the second of the same choice is a fact worth stating
MAX_KEYS = 4         # bounded: only the newest repeated choices are kept
FACT_CHARS = 240


def native_effect(before, after, location, items):
    """Literal baseline diff: bag, wallet, place or own crops. None when the after-read is missing (unknown)."""
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    return bool(items or before.get("money") != after.get("money")
                or (before.get("location") is not None and location is not None and before.get("location") != location)
                or before.get("crops") != after.get("crops"))


def _is_press(name):
    return name == "aksi" or name.startswith("aksi ")


def note(store, skill, arg, *, effect, reason, clock, pose=None, target=None, keep_presses=False):
    """Count one settled row. Any native effect clears every count (the loop decayed); unknown counts nothing."""
    if effect is None:
        return
    if effect:
        if keep_presses:
            for key in [k for k in store if not _is_press(k)]:
                store.pop(key, None)
        else:
            store.clear()
        return
    name = skill + (" " + str(arg) if arg not in (None, "") else "")
    row = store.pop(name, None) or dict(count=0, since=clock)
    if pose is not None:
        if row.get("pose") != pose:
            row = dict(count=0, since=clock)
        row.update(pose=pose, target=target)
    row.update(count=row["count"] + 1, reason=str(reason or "")[:60])
    store[name] = row
    while len(store) > MAX_KEYS:
        store.pop(next(iter(store)))


def _clock(value):
    return "%02d:%02d" % divmod(value, 100) if type(value) is int else "?"


def movement_effect(movement):
    """A step's displacement is independent of nearby effects or a turn in place."""
    from arti_runtime.raw_hands import MOVED, OBSERVED_TRANSITIONS, STILL, TURNED

    if not movement:
        return None
    if movement["kind"] in (STILL, TURNED):
        return False
    if movement["kind"] == MOVED or movement.get("transition") in OBSERVED_TRANSITIONS:
        return True
    return None


def note_step(store, direction, movement, *, target, clock, keep_presses=False):
    effect = movement_effect(movement)
    if effect is None:
        return
    before = movement["before"]
    if not effect and not before.get("location_id"):
        return
    note(store, "langkah", direction, effect=effect, reason=movement["kind"], clock=clock,
         pose=(before.get("location_id"), tuple(before.get("pixel") or ())), target=target,
         keep_presses=keep_presses)


def _measured_approach(context, direction, target):
    from arti_runtime.raw_hands import DIRS, STEPS
    from arti_runtime.fact_names import target_name
    from arti_stardew_goal_routes import open_paths
    from arti_stardew_interact import warp_tiles
    from arti_stardew_skills import _wave1_xy

    here = _wave1_xy(context.get("actor_tile"))
    targets = context.get("targets") or {}
    row = targets.get(target)
    tile = _wave1_xy(row.get("tile")) if isinstance(row, Mapping) else None
    name = target_name(target, context)
    if not name or here is None or tile is None or direction not in DIRS:
        return None
    dx, dy = STEPS[DIRS.index(direction)]
    excluded = (warp_tiles(targets) | {tile, (here[0] + dx, here[1] + dy)}) - {here}
    paths = open_paths({t: c for t, c in (context.get("nav_grid") or {}).items() if t not in excluded}, here)
    stand = next(((tile[0] + x, tile[1] + y) for x, y in STEPS if (tile[0] + x, tile[1] + y) in paths), None)
    if stand is not None:
        return "jalur terbuka terukur ke petak %d,%d di sebelah %s" % (*stand, name[:60])
    return None


def repeat_fact(store, context=None, *, exclude_spots=None):
    """``inventori 9x sejak 06:40 tanpa perubahan native ...``, newest repeats first, or None."""
    context = context or {}
    pose = (context.get("location_id"), tuple(context.get("actor_pixel") or ()))
    facts = []
    movement_shown = False
    for name, row in reversed(list(store.items())):
        if row.get("count", 0) < MIN_REPEATS:
            continue
        if exclude_spots and _is_press(name) and row.get("spot") in exclude_spots:
            continue
        head = "%s %dx sejak %s" % (name, row["count"], _clock(row.get("since")))
        if "pose" in row:
            if movement_shown or row["pose"] != pose:
                continue
            approach = _measured_approach(context, name.removeprefix("langkah "), row.get("target"))
            facts.append(head + ", posisi tetap. " + (approach or "Belum ada pendekatan yang teramati dari sini") + ".")
            movement_shown = True
        else:
            reason = " (terakhir: %s)" % row["reason"] if row.get("reason") else ""
            facts.append(head + ", tas/uang/tempat/tanaman tidak berubah" + reason)
        if len(facts) == 2:
            break
    return "; ".join(facts)[:FACT_CHARS] or None


def bag_rows(inventory):
    return [r for r in inventory or () if isinstance(r, Mapping) and isinstance(r.get("qualified_item_id"), str)
            and type(r.get("stack")) is int and r["stack"] > 0]


def _where(row):
    from arti_runtime.fact_names import item_name
    label = item_name(row["qualified_item_id"], name=row.get("display_name") or row.get("name"))
    return "%s slot %s" % (label, row["slot"]) if type(row.get("slot")) is int else str(label)


FULL_BAG_CATCH = ("tas penuh: lempar memakai waktu/energi. Hasil butuh slot/tumpukan sejenis; jika tak muat: menu, belum masuk tas. "
                  "Pilih simpan/tukar/buang/jatuhkan. Jika dijatuhkan: jatuh di kakimu; masuk sendiri saat ada ruang dan kamu dekat.")


def fishing_fact(inventory, *, offered, free_slots, water_edge, water_seen=(), water_places=()):
    """Native fishing requirements while the offer is absent: rod owned (slot), water edge, the catch needs a slot."""
    rods = [r for r in bag_rows(inventory) if r["qualified_item_id"] in RODS]
    if not rods or offered:
        # ADR 105: offered on a full bag too; what a catch needs is the one fact the offer itself does not say.
        return FULL_BAG_CATCH if rods and free_slots == 0 else None
    why = [w for w, on in (("tidak ada tepi air di sini", not water_edge),) if on]
    parts = ["%s di tas" % _where(rods[0]),
             "mancing = pancing dipegang di tepi air; lempar tanpa slot kosong, ikan perlu 1 slot kosong",
             "tangkap_ikan belum ditawarkan: " + (", ".join(why) or "syarat native belum terbaca")]
    seen = ["%s %d,%d" % (loc, t[0], t[1]) for loc, t in list(dict(water_seen).items())[:2]
            if isinstance(t, (list, tuple)) and len(t) == 2]
    if seen or water_places:
        parts.append("air: " + ", ".join(seen + ["pergi_ke " + p for p in list(water_places)[:3]]))
    return "; ".join(parts) + "."


CATCH_CHARS = 240  # the MINIMAL_PAGE_FACTS bound
TOTALS_CHARS = 110  # its share for the count across days


def note_catch(log, *, request_id, location, item, name, category):
    """ADR 115: one landed catch today, from the native reflex result; a request counts once."""
    if any(row["request_id"] == request_id for row in log):
        return False
    from arti_runtime.fact_names import item_name
    log.append(dict(request_id=request_id, location=location, item=item, name=item_name(item, name=name), category=category))
    return True


def _catch_place(place, rows, names):
    fish = sum(row["category"] == -4 for row in rows)
    junk = sum(row["category"] == -20 for row in rows)
    other = len(rows) - fish - junk
    kinds = sorted(Counter(row["name"] for row in rows).items(), key=lambda kind: (-kind[1], kind[0]))
    listed = ["%s %d" % kind for kind in kinds[:names]]
    if names and len(kinds) > names:
        listed.append("+%d jenis lain" % (len(kinds) - names))
    return "%s %d tangkapan (ikan %d, sampah %d%s)%s" % (place, len(rows), fish, junk, ", lain %d" % other if other else "",
                                                         ": " + ", ".join(listed) if listed else "")


def note_total(totals, location, category):
    """FIX71C: one landed catch in the count kept across days, per place. ``totals`` is {place: {fish, junk, other}}."""
    row = totals.setdefault(location, dict(fish=0, junk=0, other=0))
    row["fish" if category == -4 else "junk" if category == -20 else "other"] += 1


def note_kind(kinds, item):
    """FIX73D: one landed catch in the per-kind count kept across days (``{item: n}``); the row then carries the
    run-wide ordinal beside today's, so "first today" never reads as the first ever. Returns the new count."""
    if not isinstance(item, str) or not item:
        return None
    kinds[item] = kinds.get(item, 0) + 1
    return kinds[item]


def load_kinds(raw):
    """A resume value is trusted only as far as its shape: {item: positive count}."""
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(k, str) and type(v) is int and v > 0}


def load_totals(raw):
    """A resume value is trusted only as far as its shape: {place: three non-negative counts}."""
    if not isinstance(raw, Mapping):
        return {}
    return {str(place): {k: row[k] for k in ("fish", "junk", "other")} for place, row in raw.items()
            if isinstance(row, Mapping) and all(type(row.get(k)) is int and row[k] >= 0 for k in ("fish", "junk", "other"))}


def _total_place(name, row):
    return "%s %d (ikan %d, sampah %d%s)" % (name, sum(row.values()), row["fish"], row["junk"],
                                             ", lain %d" % row["other"] if row["other"] else "")


def totals_line(totals):
    """FIX71C (a52: Farm 0 fish of 23 and Beach 8 of 10 never stood side by side; the count reset each morning).
    Every place she has fished, most catches first. A true count per place, no comparison, no advice."""
    rows = sorted(totals.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    for shown in range(len(rows), 0, -1):
        cut = "; +%d tempat lain" % (len(rows) - shown) if shown < len(rows) else ""
        text = "hari tercatat: " + "; ".join(_total_place(*kv) for kv in rows[:shown]) + cut + "."
        if len(text) <= TOTALS_CHARS:
            return text
    return text[:TOTALS_CHARS]


def catch_fact(log, totals=None):
    """ADR 115: what today's casts landed, per place and by native name. Counts only: she judges them herself.
    Over the bound, names shorten before a place goes, and whatever is cut is stated, so no count reads as complete.
    FIX71C: the count across days by place rides with it and goes first when the two must share the bound."""
    ever = totals_line(totals) if totals else None
    room = CATCH_CHARS - (len(ever) + 1 if ever else 0)
    places = {}
    for row in log:
        places.setdefault(row["location"], []).append(row)
    if not places:
        return ever
    rows = list(places.items())
    most = max(len({row["name"] for row in group}) for group in places.values())
    for cut in range(len(rows)):
        tail = "; +%d tempat lain (%d tangkapan)" % (cut, sum(len(g) for _, g in rows[:cut])) if cut else ""
        for names in range(most, -1, -1):
            text = "hari ini: " + "; ".join(_catch_place(p, g, names) for p, g in rows[cut:]) + tail + "."
            if len(text) <= room:
                return text + (" " + ever if ever else "")
    return (text[:room] + (" " + ever if ever else ""))[:CATCH_CHARS]


NEXT_BAG = {12: (24, 2000), 24: (36, 10000)}  # vanilla: slots in use on a full bag -> the next size and its price


def space_fact(inventory, *, location, offered):
    """Bag-full sinks she has: ship/sell/store are offers already; an unplaced chest she carries is stated here."""
    chests = sum(r["stack"] for r in bag_rows(inventory) if r["qualified_item_id"] == CHEST)
    sinks = [n for n in ("kirim", "jual", "simpan_ke_chest") if n in offered]
    parts = ["ditawarkan: " + ", ".join(sinks) if sinks else "kirim/jual/simpan_ke_chest belum ditawarkan di sini"]
    if chests:
        parts.append("peti di tas belum terpasang: %d (%s)" % (chests, "pasang peti ditawarkan" if "pasang peti" in offered
                                                                else "pasang peti ditawarkan di Farm" if location != "Farm"
                                                                else "pasang peti butuh petak kosong yang game tawarkan"))
    parts.append("inventori membuka tas; di menu itu press slot:A = barang ke tangan; jatuhkan dan buang bekerja pada barang di tangan")  # FIX75B: effects, no sequence
    parts.append("beli barang jenis baru butuh 1 slot kosong")  # ADR 99: a46 reached the counter full; `beli` listed only stacks she held
    if bigger := NEXT_BAG.get(len({r["slot"] for r in bag_rows(inventory) if type(r.get("slot")) is int})):
        parts.append("tas %d slot: %dg di toko Pierre" % bigger)  # ADR 113: a50's only word on this was one letter on d10
    return "tas penuh; " + "; ".join(parts) + "."
