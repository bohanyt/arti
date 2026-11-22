"""World-effects memory: items that left her bag onto the ground, and whether they are still there (ADR 86).

fix59A (a43 d0001 J1492): her bag was full when Willy gave her the Bamboo Pole; her own settle:0 dropped it on the
Beach and no later prompt said where it lay. A row is written only from a native receipt (the cursor conservation
verdict of a drop, a verified trash press, a reward menu that closed with items the bag never got). It is kept until
a complete native look at that spot shows the item gone, or her bag gains it back in that location.

Facts only. Nothing here offers, ranks or filters a choice; she decides whether to go back for it.
"""
from collections.abc import Mapping
import re

MAX_ROWS = 8          # bounded: see _trim
FACT_CHARS = 240      # one rendered line (fix58B fact-line bound)
NEAR = 2              # gather window scans +-4 tiles around her; a drop lands within about a tile of where she stood
GONE_KEEP_DAYS = 1    # a row seen gone (or trashed) is stated through the next game day, then forgotten
SEASONS = ("spring", "summer", "fall", "winter")
VIA = {"dropped": "kamu jatuhkan", "landed": "jatuh", "spilled": "tidak masuk tas saat menu hadiah ditutup", "trashed": "kamu buang ke tempat sampah"}
PRESENT, UNSEEN, GONE, PICKED = "ada", "belum_terlihat", "tidak_ada", "dipungut"


def ordinal(date):
    try:
        year, season, day = date
        return (int(year) - 1) * 112 + SEASONS.index(str(season).casefold()) * 28 + int(day)
    except (TypeError, ValueError):
        return None


def label(qid, components=()):
    """The native component label, or an explicitly unknown name."""
    from arti_runtime.fact_names import display_name, UNKNOWN_ITEM
    for c in components or ():
        value = c.get("value") if isinstance(c, Mapping) else None
        if isinstance(value, Mapping) and value.get("item") == qid and isinstance(c.get("label"), str) and c["label"].strip():
            return display_name(c["label"]) or UNKNOWN_ITEM
    return UNKNOWN_ITEM


def _xy(value):
    if isinstance(value, Mapping):
        value = (value.get("x"), value.get("y"))
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(type(v) is int for v in value):
        return [value[0], value[1]]
    return None


def _clock(value):
    return "%02d:%02d" % divmod(value, 100) if type(value) is int else "?"


def _when(date, clock):
    try:
        return "%s %s Y%s %s" % (str(date[1]).capitalize(), int(date[2]), int(date[0]), _clock(clock))
    except (TypeError, ValueError, IndexError):
        return "?"


class GroundItems:
    """Rows: item, name, count, location, tile (where she stood), date, time, via, status, is_tool, bag, seen, checked."""

    def __init__(self, raw=None):
        self.rows = []
        for row in raw if isinstance(raw, list) else ():
            if (isinstance(row, Mapping) and isinstance(row.get("item"), str) and row["item"]
                    and type(row.get("count")) is int and row["count"] > 0 and isinstance(row.get("location"), str)
                    and row.get("via") in VIA and row.get("status") in (PRESENT, UNSEEN, GONE)
                    and ordinal(row.get("date")) is not None):
                self.rows.append(dict(row, tile=_xy(row.get("tile")), date=list(row["date"])))
        self._trim()

    def _trim(self):
        """FIX71C (a52 d2 J1359 -> d6 J956): eight single catches pushed 16 seeds out. A pile outlives a single
        item; a row already gone goes first, a tool she lost last, the newest receipt never. Ties drop the oldest."""
        while len(self.rows) > MAX_ROWS:
            self.rows.remove(min(self.rows[:-1], key=lambda r: (r["status"] != GONE, r.get("is_tool") is True, r["count"])))

    def dump(self):
        return [dict(r) for r in self.rows]

    def record(self, *, item, count, location, via, date, clock, tile=None, name=None, is_tool=False, bag=None, receipt_at=None, quality=None):
        """One native receipt. A trash is gone at once (the game destroyed it); a spill waits for a sighting."""
        if via not in VIA or not isinstance(item, str) or type(count) is not int or count < 1 or not location:
            return None
        if ordinal(date) is None:
            return None
        status = GONE if via == "trashed" else PRESENT if via in ("dropped", "landed") else UNSEEN
        row = dict(item=item, name=name or label(item), count=count, location=str(location), tile=_xy(tile),
                   date=list(date), time=clock if type(clock) is int else None, via=via, status=status,
                   is_tool=bool(is_tool), bag=bag if type(bag) is int else None)
        if status == GONE:
            row["checked"] = dict(date=list(date), time=row["time"])
        if type(receipt_at) in (int, float):
            row["receipt_at"] = receipt_at
        if type(quality) is int:
            row["quality"] = quality
        self.rows.append(row)
        self._trim()
        return row

    def observe(self, *, location, here, date, clock, window=None, inventory=None, scan_at=None):
        """Native readbacks at her current place: the bag (pickup) and a complete local drop scan (seen / gone).

        ``window`` is a validated gather window (``drops_complete``, ``center``, ``drops`` with pixels); a partial
        scan proves nothing. Returns True when a row changed."""
        changed = False
        today = ordinal(date)
        keep = []
        for row in self.rows:
            if row["status"] == GONE:
                checked = ordinal((row.get("checked") or {}).get("date"))
                if today is not None and checked is not None and today - checked > GONE_KEEP_DAYS:
                    changed = True
                    continue
                keep.append(row)
                continue
            if row["location"] != location:
                keep.append(row)
                continue
            have = inventory.get(row["item"]) if isinstance(inventory, Mapping) else None
            # ADR 107: a fresh-enough cached scene may still predate this drop.
            # It cannot prove an item just dropped is absent (or still present).
            current = window if row.get("receipt_at") is None or (
                type(scan_at) in (int, float) and scan_at >= row["receipt_at"]) else None
            seen = self._sighting(row, current)
            scanned = self._scanned(row, current, here)
            if seen is not None:
                row.update(status=PRESENT, seen=dict(date=list(date), time=clock, tile=seen))
                row["tile"] = row["tile"] or seen
                changed = True
            elif (type(have) is int and type(row.get("bag")) is int and have > row["bag"]
                    and (row.get("receipt_at") is None or scanned)):
                changed = True  # a new catch of the same item alone cannot erase a drop
                continue
            elif scanned:
                row.update(status=GONE, checked=dict(date=list(date), time=clock))
                changed = True
            keep.append(row)
        self.rows = keep
        return changed

    @staticmethod
    def _drops(window):
        if not isinstance(window, Mapping):
            return None
        return [d for d in window.get("drops") or () if isinstance(d, Mapping)]

    def _sighting(self, row, window):
        drops = self._drops(window)
        if not drops:
            return None
        near = row.get("tile")
        for d in drops:
            pixel = d.get("pixel") or {}
            if (d.get("qualified_item_id") != row["item"]
                    or type(row.get("quality")) is int and d.get("quality") != row["quality"]
                    or not all(type(pixel.get(k)) in (int, float) for k in "xy")):
                continue
            tile = [int(pixel["x"] // 64), int(pixel["y"] // 64)]
            if near is None or max(abs(tile[0] - near[0]), abs(tile[1] - near[1])) <= NEAR + 2:
                return tile
        return None

    @staticmethod
    def _scanned(row, window, here):
        """Gone only from a complete scan centred within NEAR tiles of where it fell, with the item absent."""
        if not isinstance(window, Mapping) or window.get("drops_complete") is not True or row.get("tile") is None:
            return False
        center = _xy(window.get("center")) or _xy(here)
        return center is not None and max(abs(center[0] - row["tile"][0]), abs(center[1] - row["tile"][1])) <= NEAR

    def here(self, location):
        """Rows still on the ground at ``location`` with a known tile: an ordinary walk target, never a steer."""
        return [r for r in self.rows if r["location"] == location and r["status"] != GONE and r.get("tile")]

    def line(self, row):
        from arti_runtime.place_names import source_place
        place = source_place(row["location"])
        where = "%s dekat tile %d,%d" % (place, *row["tile"]) if row.get("tile") else place
        head = "%s x%d: %s di %s sejak %s" % (row["name"], row["count"], VIA[row["via"]], where,
                                               _when(row["date"], row.get("time")))
        if row["status"] == GONE:
            tail = "sudah tidak ada" + ("" if row["via"] == "trashed" else
                                        " (dicek %s)" % _when(row["checked"]["date"], row["checked"].get("time")))
        elif row["status"] == UNSEEN:
            tail = "belum terlihat di tanah"
        elif row.get("seen"):
            seen = row["seen"]
            tail = "masih ada (terlihat %s tile %d,%d)" % (_when(seen["date"], seen.get("time")), *seen["tile"])
        else:
            tail = "masih ada (belum dicek lagi)"
        return (head + "; " + tail)[:FACT_CHARS]

    def relevant(self, row, *, inventory=None, texts=(), location=None):
        """fix58B priority: decision-relevant when a goal/plan names it, or it is a tool she no longer carries."""
        if row["status"] == GONE and row["via"] == "trashed":
            return False
        if row["location"] == location:
            return True  # ADR 107: a drop here matters even when no goal names it.
        names = [n for n in (row["name"], label(row["item"])) if n]
        if any(re.search(r"(?<!\w)%s(?!\w)" % re.escape(n).replace(r"\ ", r"\s+"), t, re.I)
               for t in texts if isinstance(t, str) for n in names):
            return True
        have = inventory.get(row["item"], 0) if isinstance(inventory, Mapping) else None
        return bool(row.get("is_tool")) and have == 0

    def facts(self, *, inventory=None, texts=(), location=None):
        """(relevant lines, other lines), the biggest pile first, then newest: the page shows only the first few."""
        hot, cold = [], []
        for row in sorted(reversed(self.rows), key=lambda r: -r["count"]):
            (hot if self.relevant(row, inventory=inventory, texts=texts, location=location) else cold).append(self.line(row))
        return hot, cold

    def goal_facts(self, date_label):
        """Goal-annotation facts (``arti_runtime.goal_facts``): one per row, matched by the item's name."""
        return [dict(kind="ground", name=r["item"], value=r["status"], aliases=[r["name"]], date=date_label,
                     samples=[], line=self.line(r)) for r in self.rows]
