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
            self.rows[ref] = dict(location=location, tile=parsed[1], items=items, names=names, date=list(row["date"]),
                                  time=row.get("time") if type(row.get("time")) is int else None,
                                  order=row.get("order") if type(row.get("order")) is int else 0)
        self._order = max((r["order"] for r in self.rows.values()), default=0)

    def dump(self):
        return dict(chests={ref: dict(row, tile=list(row["tile"]), items=dict(row["items"]), names=dict(row["names"]))
                            for ref, row in self.rows.items()})

    def observe(self, ref, slots, *, date, clock=None, names=None, location=None):
        """One native read of an open chest. True when what she knows changed (contents, or the day of the sighting).

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
        for qid in items:
            name = (known.get(qid) or (names or {}).get(qid) or ((before or {}).get("names") or {}).get(qid))
            if isinstance(name, str) and name:
                known[qid] = name[:40]
        self._order += 1
        self.rows[ref] = dict(location=location or parsed[0], tile=parsed[1], items=items, names=known, date=list(date),
                              time=clock if type(clock) is int else None, order=self._order)
        while len(self.rows) > MAX_CHESTS:
            del self.rows[min(self.rows, key=lambda k: self.rows[k]["order"])]
        return before is None or before["items"] != items or list(before["date"]) != list(date)

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
