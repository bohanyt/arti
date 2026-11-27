"""What a placed chest held when she last looked, across pages, days and restarts (ADR 86 amendment, FIX72D).

a53 d6 J2296: she put 16 Parsnip Seeds in the chest at (74,27) and no page after it said seeds existed. The only memory
was the day's `state/chest_contents.json`: a new folder each day and "today only", so `chest_memory_loaded` read 0 on all
6 of its rows (d5 to d12), each at that day's first open. A row is written only from a native open chest (the menu's own slots). It is a dated
sighting, never a fresh read: the chest menu stays the proof, and the HOST can move things overnight.

Facts only. Nothing here offers, ranks or filters a choice; she decides whether to go back for it.
"""
from collections.abc import Mapping
import re

MAX_CHESTS = 8
MAX_SLOTS = 72
LINE_CHARS = 240       # one rendered line (fix58B fact-line bound)
ROW_CHARS = 140        # a bedtime row (arti_mind_reflect.progress_facts cuts there)
USES_CHARS = 200
PUBLISHED_MAX = 4      # native StoreChestsMax: four rows may be a cut list, so absence from them proves nothing
PUBLISHED_RADIUS = 24  # native StoreChestRadius, the same max-of-axes distance
_REF = re.compile(r"obj:(?P<location>.+):(?P<x>\d+),(?P<y>\d+):\(BC\)(?:130|232)")
DATE_TEMPLATE = "%s %s Y%s"


def parse_ref(ref):
    """(location, [x, y]) of a placed chest's scene fact id, or None."""
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    return (match["location"], [int(match["x"]), int(match["y"])]) if match else None


def slot_counts(slots):
    """(taken, total) from a complete, valid published open-chest slot list, or None."""
    if not isinstance(slots, (list, tuple)) or not 0 < len(slots) <= MAX_SLOTS:
        return None
    seen = set()
    taken = 0
    for slot in slots:
        if not isinstance(slot, Mapping) or "item" not in slot:
            return None
        idx = slot.get("slot")
        if type(idx) is not int or not 0 <= idx < len(slots) or idx in seen:
            return None
        seen.add(idx)
        item = slot["item"]
        if item is None:
            continue
        if (not isinstance(item, Mapping)
                or not isinstance(item.get("qualified_item_id"), str) or not item["qualified_item_id"]
                or type(item.get("stack")) is not int or not 0 < item["stack"] <= 999999
                or type(item.get("quality")) is not int or item["quality"] not in (0, 1, 2, 4)):
            return None
        taken += 1
    if len(seen) != len(slots):
        return None
    return taken, len(slots)


def is_full_for_item(slots, qid, quality=None):
    """(taken, total) when every published slot is taken and none holds this item; else None."""
    counts = slot_counts(slots)
    if counts is None or counts[0] < counts[1] or not isinstance(qid, str) or not qid:
        return None
    match_quality = type(quality) is int
    for slot in slots:
        item = slot["item"]
        if (item is not None and item["qualified_item_id"] == qid
                and (not match_quality or item["quality"] == quality)):
            return None
    return counts


def store_chest_ref(context, *, blocked=()):
    """The placed chest ``simpan_ke_chest`` targets here, matching ``_store_chest_pin_entries``."""
    if not isinstance(context, Mapping):
        return None
    from arti_stardew_economy import STORE_CHEST_SUFFIXES, store_chests
    blocked = set(blocked or ())
    seen = sorted(
        (f["tile"]["x"], f["tile"]["y"], ref)
        for ref, f in (context.get("targets") or {}).items()
        if isinstance(f, Mapping) and f.get("kind") == "object"
        and str(ref).endswith(STORE_CHEST_SUFFIXES) and ref not in blocked
        and isinstance(f.get("tile"), Mapping)
        and all(type(f["tile"].get(k)) is int for k in ("x", "y"))
    )
    here = context.get("actor_tile")
    if isinstance(here, (list, tuple)) and len(here) == 2 and all(type(v) is int for v in here) and seen:
        return min(seen, key=lambda s: (max(abs(s[0] - here[0]), abs(s[1] - here[1])), s[2]))[2]
    chests = [c for c in (context.get("chests") or store_chests(context.get("mines_ui")))
              if isinstance(c, Mapping) and isinstance(c.get("ref"), str) and c["ref"] not in blocked]
    if chests:
        return chests[0]["ref"]
    return seen[0][2] if len(seen) == 1 else None


def _valid_full(raw):
    if (not isinstance(raw, Mapping)
            or type(raw.get("taken")) is not int or type(raw.get("total")) is not int
            or not 0 < raw["total"] <= MAX_SLOTS or raw["taken"] != raw["total"]
            or not isinstance(raw.get("date"), (list, tuple)) or len(raw["date"]) != 3):
        return None
    return dict(taken=raw["taken"], total=raw["total"], date=list(raw["date"]),
                time=raw.get("time") if type(raw.get("time")) is int else None)


def _seen(row):
    date, clock = row["date"], row.get("time")
    try:
        day = DATE_TEMPLATE % (str(date[1]).capitalize(), int(date[2]), int(date[0]))
    except (TypeError, ValueError, IndexError):
        return "?"
    return day + (" %02d:%02d" % divmod(clock, 100) if type(clock) is int else "")


class ChestMemory:
    """{scene fact id: dict(location, tile, items {qid: count}, names {qid: name}, date, time, order)}."""

    def __init__(self, raw=None):
        self.rows = {}
        chests = raw.get("chests") if isinstance(raw, Mapping) else None
        for ref, row in chests.items() if isinstance(chests, Mapping) else ():
            parsed = parse_ref(ref)
            if (parsed is None or not isinstance(row, Mapping) or not isinstance(row.get("items"), Mapping)
                    or not isinstance(row.get("date"), (list, tuple)) or len(row["date"]) != 3):
                continue
            items = {str(q): c for q, c in row["items"].items() if type(c) is int and c > 0}
            names = {str(q): str(n)[:40] for q, n in (row.get("names") or {}).items() if isinstance(n, str)}
            location = row["location"] if isinstance(row.get("location"), str) and row["location"] else parsed[0]
            loaded = dict(location=location, tile=parsed[1], items=items, names=names, date=list(row["date"]),
                          time=row.get("time") if type(row.get("time")) is int else None,
                          order=row.get("order") if type(row.get("order")) is int else 0)
            if (full := _valid_full(row.get("full"))) is not None:
                loaded["full"] = full
            self.rows[ref] = loaded
        self._order = max((r["order"] for r in self.rows.values()), default=0)

    def dump(self):
        out = {}
        for ref, row in self.rows.items():
            dumped = dict(row, tile=list(row["tile"]), items=dict(row["items"]), names=dict(row["names"]))
            if (full := _valid_full(row.get("full"))) is not None:
                dumped["full"] = full
            else:
                dumped.pop("full", None)
            out[ref] = dumped
        return dict(chests=out)

    def observe(self, ref, slots, *, date, clock=None, names=None, location=None):
        """One native read of an open chest. True when what she knows changed (contents, fullness, or sighting day).

        ``location`` is where she stands: a scene fact id carries a shortened name for a cabin or a shed."""
        parsed = parse_ref(ref)
        if parsed is None or not isinstance(date, (list, tuple)) or len(date) != 3:
            return False
        items, known = {}, {}
        for slot in slots or ():
            item = slot.get("item") if isinstance(slot, Mapping) else None
            qid = item.get("qualified_item_id") if isinstance(item, Mapping) else None
            if isinstance(qid, str) and qid and type(item.get("stack")) is int and item["stack"] > 0:
                items[qid] = items.get(qid, 0) + item["stack"]
                if isinstance(item.get("name"), str) and item["name"]:
                    known[qid] = item["name"][:40]
        before = self.rows.get(ref)
        before_full = _valid_full((before or {}).get("full"))
        counts = slot_counts(slots)
        has_free = any(isinstance(slot, Mapping) and "item" in slot and slot["item"] is None for slot in (slots or ()))
        kept_full = None
        if (before_full is not None and not has_free and counts is not None
                and counts == (before_full["total"], before_full["total"])):
            kept_full = (dict(before_full) if list(before["date"]) == list(date)
                         else dict(taken=counts[0], total=counts[1], date=list(date),
                                   time=clock if type(clock) is int else None))
        for qid in items:
            name = (known.get(qid) or (names or {}).get(qid) or ((before or {}).get("names") or {}).get(qid))
            if isinstance(name, str) and name:
                known[qid] = name[:40]
        self._order += 1
        row = dict(location=location or parsed[0], tile=parsed[1], items=items, names=known, date=list(date),
                   time=clock if type(clock) is int else None, order=self._order)
        if kept_full is not None:
            row["full"] = kept_full
        self.rows[ref] = row
        while len(self.rows) > MAX_CHESTS:
            del self.rows[min(self.rows, key=lambda k: self.rows[k]["order"])]
        return (before is None or before["items"] != items or list(before["date"]) != list(date)
                or before_full != kept_full)

    def note_full(self, ref, taken, total, *, date, clock=None, location=None, slots=None, names=None):
        """Record a native open-chest full observation (taken == total) on this chest's memory row."""
        parsed = parse_ref(ref)
        if (parsed is None or type(taken) is not int or type(total) is not int
                or not 0 < total <= MAX_SLOTS or taken != total
                or not isinstance(date, (list, tuple)) or len(date) != 3):
            return False
        if slots is not None:
            if slot_counts(slots) != (taken, total):
                return False
            self.observe(ref, slots, date=date, clock=clock, names=names, location=location)
        row = self.rows.get(ref)
        full = dict(taken=taken, total=total, date=list(date), time=clock if type(clock) is int else None)
        if row is not None and _valid_full(row.get("full")) == full:
            return False
        self._order += 1
        if row is None:
            row = dict(location=location or parsed[0], tile=parsed[1], items={}, names={}, date=list(date),
                       time=full["time"], order=self._order)
            self.rows[ref] = row
        else:
            if location:
                row["location"] = location
            row.update(date=list(date), time=full["time"], order=self._order)
        row["full"] = full
        while len(self.rows) > MAX_CHESTS:
            del self.rows[min(self.rows, key=lambda k: self.rows[k]["order"])]
        return True

    def full_note(self, ref, *, location=None):
        """Dated page line when the newest observation of ``ref`` showed every slot taken; else None."""
        row = self.rows.get(ref)
        if row is None or (location is not None and row.get("location") != location):
            return None
        full = _valid_full(row.get("full"))
        if full is None:
            return None
        return "peti ini penuh waktu kamu buka, %d/%d (%s)" % (full["taken"], full["total"], _seen(full))

    def here(self, location):
        return [dict(ref=ref, tile=list(row["tile"])) for ref, row in self.rows.items() if row["location"] == location]

    def outcome(self, outcome):
        """ADR 140: the exact native per-item outcome also feeds the next page's memory."""
        ref, qid, count = outcome["chest"], outcome["item"], outcome["chest_count"]
        parsed = parse_ref(ref)
        if parsed is None or type(count) is not int or count < 0:
            return False
        if ref not in self.rows:
            # A per-item outcome alone does not establish a complete chest inventory.
            return False
        row = self.rows[ref]
        if count:
            row["items"][qid] = count
            row["names"][qid] = outcome["name"]
        else:
            row["items"].pop(qid, None)
            if outcome.get("destination") == "inventory":
                row.pop("full", None)
        self._order += 1
        row.update(date=list(outcome["date"]), time=outcome["clock"], order=self._order)
        return True

    def note_published(self, location, here, published):
        """A chest native no longer lists where it must be listed (it was picked up or destroyed) leaves the memory.

        ``published`` is the native ``store_chests`` list, or None when this read has none. A list cut at its limit, or
        a chest beyond native's radius of her, proves nothing. True when a row left."""
        if not isinstance(published, (list, tuple)) or len(published) >= PUBLISHED_MAX or here is None:
            return False
        listed = {row.get("ref") for row in published if isinstance(row, Mapping)}
        gone = [ref for ref, row in self.rows.items() if row["location"] == location and ref not in listed
                and max(abs(row["tile"][0] - here[0]), abs(row["tile"][1] - here[1])) <= PUBLISHED_RADIUS]
        for ref in gone:
            del self.rows[ref]
        return bool(gone)

    def _named(self, row, names):
        """[(name, count)], largest pile first; items no source names stay one explicit unknown count per kind."""
        from arti_runtime.fact_names import item_name
        merged = dict(names or {}, **row["names"])
        return sorted(((item_name(q, merged), c, q) for q, c in row["items"].items()), key=lambda t: (-t[1], t[0], t[2]))

    def stored(self, names=None):
        """{item name: count} across every chest she has seen, for a line that ties a stored stack to something else."""
        out = {}
        from arti_runtime.fact_names import UNKNOWN_ITEM
        for row in self.rows.values():
            for name, count, _ in self._named(row, names):
                if name != UNKNOWN_ITEM:
                    out[name] = out.get(name, 0) + count
        return out

    def holding(self, qids, names=None, catalog=None):
        """FIX76A: where these items lie, as she last read each chest: name, count, place, date. Newest read first."""
        from arti_runtime.fact_names import item_name
        from arti_runtime.place_names import source_place
        out = []
        for row in sorted(self.rows.values(), key=lambda r: -r.get("order", 0)):
            merged = dict(names or {}, **row["names"])
            items = ["%s x%d" % (item_name(q, merged), c) for q, c in sorted(row["items"].items()) if q in qids and c > 0]
            if items:
                out.append("%s (%s %d,%d; dilihat %s)" % (", ".join(items), source_place(row["location"], catalog),
                                                         row["tile"][0], row["tile"][1], _seen(row)))
        return out

    def counts(self):
        """{qid: count} across every chest she has seen."""
        out = {}
        for row in self.rows.values():
            for qid, count in row["items"].items():
                out[qid] = out.get(qid, 0) + count
        return out

    def _chest_text(self, row, names, limit, catalog=None):
        from arti_runtime.place_names import source_place
        head = "%s %d,%d: " % (source_place(row["location"], catalog), row["tile"][0], row["tile"][1])
        tail = "; dilihat " + _seen(row)
        if not row["items"]:
            return head + "kosong" + tail
        room = limit - len(head) - len(tail) - len(", +99 jenis")
        kept, shown = [], 0
        for name, count, _ in self._named(row, names):
            text = "%s x%d" % (name, count)
            if shown + len(text) + 2 > room:
                break
            kept.append(text)
            shown += len(text) + 2
        rest = len(row["items"]) - len(kept)
        return head + ", ".join(kept + (["+%d jenis" % rest] if rest else [])) + tail

    def line(self, names=None, catalog=None):
        """Where her chests stand and what they held when she last looked, newest sighting first; None with no sighting."""
        rows = sorted(self.rows.values(), key=lambda r: -r["order"])
        if not rows:
            return None
        share = (LINE_CHARS - 2 * (len(rows) - 1)) // len(rows) if len(rows) <= 2 else (LINE_CHARS - 4) // 3
        parts = [self._chest_text(row, names, share, catalog) for row in rows[:3]]
        return "; ".join(parts) + (" +%d peti lagi" % (len(rows) - 3) if len(rows) > 3 else "")

    def reflection(self, names=None, limit=2, catalog=None):
        """The same facts as bedtime rows, one chest each."""
        rows = sorted(self.rows.values(), key=lambda r: -r["order"])
        return [dict(ref="p:peti%d" % i, text="Di peti " + self._chest_text(row, names, ROW_CHARS - len("Di peti "), catalog))
                for i, row in enumerate(rows[:limit])]
