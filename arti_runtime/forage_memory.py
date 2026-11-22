"""Forage she has seen on the ground: on this map now, and where she saw it this season (FIX73E).

a54: over 23 days the morning saves held Daffodil, Dandelion, Leek and Wild Horseradish on the maps she crossed, and she walked
within 12 tiles of one 20 times, 10 of them within 4 (d19 J420, d20 J1066: 1 tile). 0 of 7 pages stood on such a map named it.
The sightings existed (`forage_here`, `forage_seen_on_way`) but rode in blocks the page diet moves to a read, and they lasted a day.

A row comes only from a native scene object (spawned, grabbable, not a machine, not a stone with hits left). A remembered row is a
dated sighting, never a fresh read; the game respawns forage each morning. No map or season table: what she has not seen is not
named. Facts only: nothing here offers, ranks or filters a choice, and a full bag is stated, not solved.

FIX75B (ADR 129): items keep their own order (distance here, newest sighting first elsewhere, the order seen today at bedtime); a
bundle fit never moves an item forward. A line's fits are one tail segment (``FIT_MARK``) that ``split_fits`` can lift off whole,
so a page that may name a bundle only once keeps the fit one read away. Bedtime rows carry no bundle name: the open bundles'
needs are rows of their own there.
"""
from collections.abc import Mapping

MAX_PLACES = 16
MAX_NAMES = 6
HERE_CHARS = 200       # one page line; PAGE_DAY rides it, the guard pays with detail blocks
SEEN_CHARS = 200
ROW_CHARS = 140        # a bedtime row (arti_mind_reflect.progress_facts cuts there)
HERE_HEAD = "Forage terlihat di %s: "
SEEN_HEAD = "Forage yang pernah terlihat musim ini: "
TODAY_HEAD = "Forage terlihat hari ini di %s: "
SEASON_HEAD = "Forage musim ini pernah terlihat di: "
FULL = "; tas penuh"
FIT_MARK = "; untuk bundle: "


def forage_rows(targets):
    """{scene fact id: dict(name, tile)} of what the scene shows as forage. The same family `forage` picks up, minus stones."""
    from arti_stardew_interact import is_forage_fact, _fact_tile
    rows = {}
    for ref, fact in (targets or {}).items():
        if not isinstance(ref, str) or not is_forage_fact(fact):
            continue
        name, tile = fact.get("display_name") or fact.get("name"), _fact_tile(fact)
        ready = fact.get("minutes_until_ready")
        if isinstance(name, str) and 0 < len(name) <= 40 and tile is not None and not (type(ready) is int and ready > 0):
            rows[ref] = dict(name=name, tile=list(tile))
    return rows


def _date(raw):
    return list(raw) if isinstance(raw, (list, tuple)) and len(raw) == 3 and type(raw[0]) is int and type(raw[2]) is int else None


def _dated(date):
    return "%s %d" % (str(date[1]).capitalize(), date[2])


def _cut(head, parts, tail, limit, more):
    """head + as many whole parts as fit + the count of the rest + tail. None with nothing to say. FIX75B: the count's room is
    reserved only when something is cut, so a line that just fits is not cut for the count's sake."""
    if not parts:
        return None

    def greedy(reserve):
        room, kept = limit - len(head) - len(tail) - reserve, []
        for part in parts:
            cost = len(part) + (2 if kept else 0)
            if cost > room:
                break
            kept.append(part)
            room -= cost
        return kept
    kept = greedy(0)
    if len(kept) < len(parts):
        kept = greedy(len(more % 99) + 2)
    rest = len(parts) - len(kept)
    return head + "; ".join(kept + ([more % rest] if rest else [])) + tail


def _fit_tail(names, fits, room):
    """One tail segment naming which of ``names`` (in their own order) an open bundle asks for, grouped by the bundle's own words:
    ``; untuk bundle: A, B (cocok X), C (Y minta x5)``. Empty with no fit. Bounded by ``room`` with an honest count; never
    reorders the line's items."""
    groups = {}
    for name in names:
        label = fits.get(name)
        if label:
            groups.setdefault(label.replace("cocok bundle ", "cocok ", 1).replace("bundle ", "", 1), []).append(name)
    if not groups:
        return ""
    parts = ["%s (%s)" % (", ".join(items), label) for label, items in groups.items()]
    text = ", ".join(parts)
    while len(parts) > 1 and len(FIT_MARK) + len(text) > room:
        parts.pop()
        text = ", ".join(parts) + ", +%d lagi" % (len(groups) - len(parts))
    return FIT_MARK + text[:max(0, room - len(FIT_MARK))]


def _tail_room(limit, head, parts, fixed):
    """How long a fit tail may be: whatever the items leave free, and at least a third of the line, so a long item list and
    its fit tail each take an honest cut instead of one hiding the other."""
    used = len(head) + sum(len(part) for part in parts) + 2 * max(0, len(parts) - 1) + len(fixed)
    return max(limit // 3, limit - used)


def split_fits(line):
    """(the line without its fit tail, the fit tail's text or None). A line without FIT_MARK comes back unchanged."""
    if not isinstance(line, str) or FIT_MARK not in line:
        return line, None
    head, rest = line.split(FIT_MARK, 1)
    full = rest.endswith(FULL)
    return head + (FULL if full else ""), rest[:-len(FULL)] if full else rest


class ForageMemory:
    """today {location: dict(date, rows {ref: dict(name, tile)})}; seasons {season: {location: {name: date}}}."""

    def __init__(self, raw=None):
        self.today, self.seasons = {}, {}
        self._picked, self._picked_on = set(), None  # the scene can keep a consumed object after the pickup (R7); a new day clears it
        raw = raw if isinstance(raw, Mapping) else {}
        today = raw.get("today") if isinstance(raw.get("today"), Mapping) else {}
        for location, place in today.items():
            rows = place.get("rows") if isinstance(place, Mapping) else None
            if isinstance(rows, Mapping) and _date(place.get("date")):
                self.today[str(location)] = dict(date=_date(place["date"]), rows={
                    r: dict(name=v["name"], tile=list(v["tile"])) for r, v in rows.items()
                    if isinstance(v, Mapping) and isinstance(v.get("name"), str) and isinstance(v.get("tile"), (list, tuple))})
        seasons = raw.get("seasons") if isinstance(raw.get("seasons"), Mapping) else {}
        for season, places in seasons.items():
            for location, names in places.items() if isinstance(places, Mapping) else ():
                for name, date in names.items() if isinstance(names, Mapping) else ():
                    if _date(date):
                        self.seasons.setdefault(str(season), {}).setdefault(str(location), {})[str(name)] = _date(date)

    def dump(self):
        return dict(today={k: dict(date=list(v["date"]), rows={r: dict(x) for r, x in v["rows"].items()}) for k, v in self.today.items()},
                    seasons={s: {k: {n: list(d) for n, d in v.items()} for k, v in p.items()} for s, p in self.seasons.items()})

    def _rows(self, location, date):
        place = self.today.get(location)
        return place["rows"] if place is not None and place["date"] == list(date) else {}

    def observe(self, location, targets, *, date, complete=True):
        """One native scene read of ``location``. A complete read replaces today's rows there (what she picked is gone); a partial
        window only adds. True when what she knows changed, so a journal row is written for a change and not for each frame."""
        date = _date(date)
        if date is None or not isinstance(location, str) or not location:
            return False
        if self._picked_on != date:
            self._picked, self._picked_on = set(), date
        before = self._rows(location, date)
        rows = {ref: row for ref, row in forage_rows(targets).items() if ref not in self._picked}
        rows = rows if complete else {**before, **rows}
        changed = set(rows) != set(before)
        if rows:
            self.today[location] = dict(date=date, rows=rows)
        else:
            self.today.pop(location, None)
        season = str(date[1])
        for name in dict.fromkeys(r["name"] for r in rows.values()):
            seen = self.seasons.setdefault(season, {}).setdefault(location, {})
            changed = changed or seen.get(name) != date
            seen.pop(name, None)  # newest last, so the trims below drop the oldest sighting
            seen[name] = date
            while len(seen) > MAX_NAMES:
                del seen[next(iter(seen))]
        if rows:
            places = self.seasons[season]
            places[location] = places.pop(location)
            while len(places) > MAX_PLACES:
                del places[next(iter(places))]
        return changed

    def forget(self, ref):
        """A verified pickup: the scene may keep the object, so a later read of today does not bring it back."""
        self._picked.add(ref)
        for place in self.today.values():
            place["rows"].pop(ref, None)

    def here_line(self, location, here, date, *, full=False, fits=None, place=None):
        """What the scene shows on this map: name, count, distance (nearest first), "tas penuh" when so, and as one tail segment
        which of them an open bundle asks for. FIX75B: the fit never moves an item forward."""
        fits = fits or {}
        grouped = {}
        for row in self._rows(location, date).values():
            far = None if here is None else abs(row["tile"][0] - here[0]) + abs(row["tile"][1] - here[1])
            count, near = grouped.get(row["name"], (0, None))
            grouped[row["name"]] = (count + 1, far if near is None or (far is not None and far < near) else near)
        names, texts = [], []
        for name, (count, near) in sorted(grouped.items(), key=lambda kv: (9999 if kv[1][1] is None else kv[1][1], kv[0])):
            text = name + (" x%d" % count if count > 1 else "")
            if near is not None:
                text += " %s%d petak" % ("terdekat " if count > 1 else "", near)
            names.append(name)
            texts.append(text)
        head = HERE_HEAD % (place or location)
        tail = _fit_tail(names, fits, _tail_room(HERE_CHARS, head, texts, FULL if full else "")) + (FULL if full else "")
        return _cut(head, texts, tail, HERE_CHARS, "+%d jenis lagi")

    def _elsewhere(self, location, date):
        """[(newest (year, day), place id, [names], [texts])] for every other map she saw forage on this season, newest first."""
        out = []
        for where, names in self.seasons.get(str(date[1]), {}).items():
            if where == location:
                continue
            found = list(self._rows(where, date).values())
            rows = []
            for name, seen in names.items():
                count = sum(r["name"] == name for r in found)
                if seen == list(date) and not count:
                    continue  # seen today and gone from the last read of that map
                rows.append((name, name + (" x%d" % count if count > 1 else "") + " " + ("hari ini" if count else _dated(seen))))
            if rows:
                newest = max((seen[0], seen[2]) for seen in names.values())
                out.append((newest, where, [name for name, _ in rows], [text for _, text in rows]))
        return sorted(out, key=lambda row: (-row[0][0], -row[0][1], row[1]))

    def seen_line(self, location, date, *, fits=None, place=None):
        """Other maps where she has seen forage this season, newest sighting first, each name with the day; today's rows carry
        their count. The fits, if any, are one tail segment."""
        fits, place = fits or {}, place or (lambda where: where)
        rows = self._elsewhere(location, date)
        parts = ["%s: %s" % (place(where), ", ".join(texts)) for _, where, _, texts in rows]
        names = list(dict.fromkeys(name for _, _, found, _ in rows for name in found))
        return _cut(SEEN_HEAD, parts, _fit_tail(names, fits, _tail_room(SEEN_CHARS, SEEN_HEAD, parts, "")), SEEN_CHARS, "+%d tempat lagi")

    def reflection(self, date, *, place=None, limit=2):
        """Today's sightings as bedtime rows, one map each, in the order she saw them. No bundle name: the open bundles' needs are
        rows of their own at bedtime (bundle_knowledge.reflection). Empty on a day with none."""
        place = place or (lambda where: where)
        maps = []
        for where, entry in self.today.items():
            if entry["date"] != list(date) or not entry["rows"]:
                continue
            names = {}
            for row in entry["rows"].values():
                names[row["name"]] = names.get(row["name"], 0) + 1
            maps.append((where, [name + (" x%d" % n if n > 1 else "") for name, n in names.items()]))
        return [dict(ref="p:forage%d" % i, text=_cut(TODAY_HEAD % place(where), texts, "", ROW_CHARS, "+%d lagi"))
                for i, (where, texts) in enumerate(maps[:limit])]

    def season_row(self, date, *, place=None):
        """FIX75B: the season's sightings on other days as one bedtime row (the page carries them only on a map change)."""
        place = place or (lambda where: where)
        parts = []
        for _, where, _, texts in self._elsewhere(None, date):
            if earlier := [text for text in texts if not text.endswith("hari ini")]:  # today's maps are rows of their own
                parts.append("%s: %s" % (place(where), ", ".join(earlier)))
        text = _cut(SEASON_HEAD, parts, "", ROW_CHARS, "+%d tempat lagi")
        return [dict(ref="p:forage-musim", text=text)] if text else []
