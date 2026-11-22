"""What she has seen on a map and can walk back to: a dated row per name, per map (FIX77C, ADR 136).

a56: Robin's axe, `obj:Forest:110,81:(O)788`, read on d7 J1094 and d11 J1190, was 54 tiles away at her closest in 25 Forest visits. No
choice named it. The scene holds 40 objects nearest one tile, so the same axe drops out of the read (d7 J1451, d11 J1707), and
`forage_memory`, whose rows are today's and whose read counts as the whole map, forgot it each time. That tile is not always hers: d7 J1126
and J1451 report one pose and share no object, so what a read leaves out says nothing about a far tile.

A row comes only from a native scene fact: an object that is spawned and grabbable, a machine, a chest, an artifact spot. There is no list of
items and no quest in here. A row is a dated sighting, never a fresh read: it survives the day. Only her own eyes end it: she stands beside
the tile, the scene does not hold it, and the native nav grid, read around her each tick, says the tile is open. A newer sighting of the same
name replaces it.

Nothing here decides for her. `offers` bounds the walks a page lists; the rest are a line one read away.
"""
from collections.abc import Mapping

MAX_PLACES = 16
MAX_NAMES = 12          # per map; past it the oldest sighting goes (a56 replay: 8 at most, Beach)
MAX_OFFERS = 3
NEAREST_OFFERS = 2      # the nearest two, then the oldest of the rest: a far thing is not hidden by newer, nearer ones for good
REACH = 12              # within FORAGE_REACH the map's own choices already act on it (forage, aksi, gali_artefak)
ADJACENT = 2            # she stands beside it, with the nav grid's own read of the tile
READ_CHARS = 400
GONE_KEEP = 2
READ_HEAD = "Benda yang pernah terlihat: "
NAME_TEXT = "%s, terlihat %s"
GONE_TEXT = "%s yang terlihat %s di %d,%d: tidak ada lagi di sana"
WALK_KIND = "seen_object"
_CHESTS = ("(BC)130", "(BC)232")
_SEASONS = ("spring", "summer", "fall", "winter")


def _date(raw):
    return list(raw) if isinstance(raw, (list, tuple)) and len(raw) == 3 and type(raw[0]) is int and type(raw[2]) is int else None


def _dated(date, today):
    return "hari ini" if date == list(today) else "%s %d" % (str(date[1]).capitalize(), date[2])


def _age(date):
    season = str(date[1]).casefold()
    return date[0], _SEASONS.index(season) if season in _SEASONS else 0, date[2]


def _far(tile, here):
    return max(abs(tile[0] - here[0]), abs(tile[1] - here[1]))


def worth_a_walk(fact):
    """A native read names it as something a player walks back to, not the map's own litter or weeds."""
    from arti_stardew_interact import is_forage_fact
    from arti_stardew_tool_work import is_artifact_spot
    if not isinstance(fact, Mapping):
        return False
    if is_artifact_spot(fact):
        return True
    if fact.get("kind") != "object":
        return False
    ready = fact.get("minutes_until_ready")
    if is_forage_fact(fact):
        return not (type(ready) is int and ready > 0)  # a stone with hits left is not something to pick up
    return fact.get("machine") is True or fact.get("qualified_item_id") in _CHESTS


def stands(tile):
    x, y = tile
    return [[a, b] for a, b in ((x, y + 1), (x - 1, y), (x, y - 1), (x + 1, y)) if a >= 0 and b >= 0]


class SeenObjects:
    """places {location: {name: dict(ref, tile, date)}}: one row per name, the newest sighting, oldest row first."""

    def __init__(self, raw=None):
        self.places, self.gone = {}, []
        self._picked, self._picked_on = set(), None  # the scene can keep a consumed object after the pickup; a new day clears it
        places = raw.get("places") if isinstance(raw, Mapping) else None
        for location, rows in places.items() if isinstance(places, Mapping) else ():
            for name, row in rows.items() if isinstance(rows, Mapping) else ():
                if (isinstance(row, Mapping) and isinstance(row.get("ref"), str) and _date(row.get("date"))
                        and isinstance(row.get("tile"), (list, tuple)) and len(row["tile"]) == 2):
                    self.places.setdefault(str(location), {})[str(name)] = dict(
                        ref=row["ref"], tile=[int(row["tile"][0]), int(row["tile"][1])], date=_date(row["date"]))

    def dump(self):
        return dict(places={k: {n: dict(ref=r["ref"], tile=list(r["tile"]), date=list(r["date"])) for n, r in v.items()}
                            for k, v in self.places.items()})

    def observe(self, location, targets, *, here, date, grid=None):
        """One native scene read from ``here``. True when what she knows changed, so a journal row is for a change, not for each frame.
        ``grid``: the current nav grid {(x, y): cell}. Without one a read can add a sighting and never end one."""
        from arti_stardew_interact import _fact_tile
        date = _date(date)
        if date is None or not isinstance(location, str) or not location or here is None:
            return False
        if self._picked_on != date:
            self._picked, self._picked_on = set(), date
        here, seen = tuple(here), {}
        listed = {ref for ref in (targets or {}) if isinstance(ref, str)}
        for ref, fact in (targets or {}).items():
            if not isinstance(ref, str) or ref in self._picked or not worth_a_walk(fact):
                continue
            name, tile = fact.get("display_name") or fact.get("name"), _fact_tile(fact)
            if not isinstance(name, str) or not 0 < len(name) <= 40 or tile is None:
                continue
            row = seen.get(name)
            if row is None or _far(tile, here) < _far(row["tile"], here):
                seen[name] = dict(ref=ref, tile=list(tile), date=date)
        rows = self.places.get(location, {})
        changed = False
        for name, row in list(rows.items()):
            if (row["ref"] not in listed and _far(row["tile"], here) <= ADJACENT and isinstance(grid, Mapping)
                    and grid.get(tuple(row["tile"])) == "."):  # what she walked up to is not there: said once, on the page
                self.gone = (self.gone + [dict(name=name, tile=list(row["tile"]), date=list(row["date"]))])[-GONE_KEEP:]
                del rows[name]
                changed = True
        for name, row in seen.items():
            if rows.get(name) != row:
                rows.pop(name, None)  # newest last, so the trim below drops the oldest sighting
                rows[name] = row
                changed = True
        while len(rows) > MAX_NAMES:
            del rows[min(rows, key=lambda n: _age(rows[n]["date"]))]
        if rows:
            self.places[location] = self.places.pop(location, rows)
        else:
            self.places.pop(location, None)
        while len(self.places) > MAX_PLACES:
            del self.places[next(iter(self.places))]
        return changed

    def forget(self, ref):
        """A verified pickup: the scene may keep the object, so a later read of today does not bring it back."""
        self._picked.add(ref)
        for location, rows in list(self.places.items()):
            for name in [n for n, row in rows.items() if row["ref"] == ref]:
                del rows[name]
            if not rows:
                del self.places[location]

    def at(self, location, tile):
        """The row whose object stands on ``tile`` of this map, or None. Only such a row is a walk to a sighting."""
        if tile is None:
            return None
        return next((r for r in self.places.get(location, {}).values() if tuple(r["tile"]) == tuple(tile)), None)

    def offers(self, location, here, *, refused=()):
        """At most MAX_OFFERS rows beyond REACH: the nearest, then the oldest of the rest. Never a row she has not seen."""
        if here is None:
            return []
        rows = [dict(row, name=name) for name, row in self.places.get(location, {}).items()
                if _far(row["tile"], here) > REACH and row["ref"] not in refused]
        rows.sort(key=lambda r: (_far(r["tile"], here), r["ref"]))
        picked = rows[:NEAREST_OFFERS]
        rest = sorted(rows[NEAREST_OFFERS:], key=lambda r: (_age(r["date"]), r["ref"]))
        return picked + rest[:MAX_OFFERS - len(picked)]

    def names(self, rows, here, today):
        """{arg: what she saw there and when}, for the page beside the walk's own arg."""
        out = {}
        for row in rows:
            text = NAME_TEXT % (row["name"], _dated(row["date"], today))
            if here is not None:
                text += ", %d petak" % (abs(row["tile"][0] - here[0]) + abs(row["tile"][1] - here[1]))
            out["%d,%d" % tuple(row["tile"])] = text
        return out

    def read_line(self, location, today, *, place=None, here=None, offered=()):
        """What the page does not already say, this map first, newest map next: one read away, never on the page. None with nothing.
        Left out: on this map, what lies within REACH (the map's own facts and choices) and what a walk already names."""
        place = place or (lambda where: where)
        order = ([location] if location in self.places else []) + [w for w in reversed(list(self.places)) if w != location]

        def told(where, row):
            return where == location and (row["ref"] in offered or (here is not None and _far(row["tile"], here) <= REACH))
        parts = []
        for where in order:
            rows = ["%s %d,%d %s" % (name, row["tile"][0], row["tile"][1], _dated(row["date"], today))
                    for name, row in self.places[where].items() if not told(where, row)]
            if rows:
                parts.append("%s: %s" % (place(where), ", ".join(rows)))
        if not parts:
            return None
        kept, room = [], READ_CHARS - len(READ_HEAD) - len(", +99 tempat lagi")
        for part in parts:
            if len(part) + 2 > room:
                break
            kept.append(part)
            room -= len(part) + 2
        rest = len(parts) - len(kept)
        return READ_HEAD + "; ".join(kept + (["+%d tempat lagi" % rest] if rest else []))

    def take_gone(self, today):
        """What she walked up to and did not find, once."""
        gone, self.gone = self.gone, []
        return "; ".join(GONE_TEXT % (g["name"], _dated(g["date"], today), g["tile"][0], g["tile"][1]) for g in gone) or None
