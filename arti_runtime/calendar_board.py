"""ADR 230 N1: she reads the open calendar board one day cell at a time.

While the game's calendar board (Billboard) is open on her screen, each day cell
is a separate choice on her page, labelled only by the day number the board
shows and by whether the cell shows a mark (never what the mark is). Choosing
one records, as a game text she read, exactly the string the game itself
prepared for that cell (native ``calendar_days``: Billboard.calendarDayData
[day].HoverText, the board's tooltip text, in the game's language), dated,
source ``calendar board``. One choice reads one day; nothing is read without
the board open; nothing comes from data files or festival tables; an empty
cell reads as empty (no text, no receipt). The cells' strings are runtime-only:
they never reach her page unless she reads that cell.
"""
from collections.abc import Mapping
import hashlib
import re

SOURCE = "calendar board"
PREFIX = "baca:"
TEXT_MAX = 512
MARK = "ada tanda"


def parse(raw):
    """Native ``calendar_days`` [[day, marked 0/1, text], ...] -> ((day, marked, text), ...); malformed -> ()."""
    if not isinstance(raw, (list, tuple)) or not 0 < len(raw) <= 28:
        return ()
    out, days = [], set()
    for cell in raw:
        if (not isinstance(cell, (list, tuple)) or len(cell) != 3 or type(cell[0]) is not int or not 1 <= cell[0] <= 28
                or cell[1] not in (0, 1) or type(cell[1]) is not int or not isinstance(cell[2], str)
                or len(cell[2]) > TEXT_MAX or cell[0] in days or (cell[1] == 0 and cell[2])):
            return ()  # a cell without a mark has nothing prepared for it; anything else is a wire defect
        days.add(cell[0])
        out.append((cell[0], cell[1] == 1, cell[2]))
    return tuple(sorted(out))


def project(view, actions):
    """(actions + one read choice per cell, card) for an open calendar board; otherwise unchanged and {}."""
    cells = getattr(view, "calendar_days", ()) or ()
    if getattr(view, "menu_type", None) != "Billboard" or not cells:
        return actions, {}
    choices = {}
    for day, marked, _ in cells:
        choices[PREFIX + str(day)] = "hari %d%s" % (day, ", " + MARK if marked else "")
    return {**actions, **{k: None for k in choices}}, dict(choices=choices, source=SOURCE)


def is_pick(arg):
    return isinstance(arg, str) and re.fullmatch(r"baca:([1-9]|1\d|2[0-8])", arg) is not None


def read(base, scope, view, arg, *, year, season, day, clock, place, mind_scoped=False):
    """Record the one cell she chose as a game text she read; returns the page line, or None when not offered.

    ``year``/``season``: the board's own month (the game shows the current season); ``day``: the game day
    she read it on (absolute). The receipt is ``calendar_day_read`` with id ``year:season:cell``, its text
    owner holds the cell's exact string, source ``calendar board``. An empty cell writes nothing.
    """
    if not is_pick(arg):
        return None
    cell = next((c for c in getattr(view, "calendar_days", ()) or () if c[0] == int(arg[len(PREFIX):])), None)
    if cell is None:
        return None
    number, _, text = cell
    if not text.strip():
        return "hari %d: kosong (papan kalender)" % number
    import arti_mind as M
    from arti_runtime.plan_dates import label
    from arti_runtime.seen_receipts import SeenReceipts
    entry = "%s:%s:%d" % (year, season, number)
    event_id = "calendar_board:%s:%s" % (entry, hashlib.sha256(text.encode()).hexdigest()[:16])
    text_ref = hashlib.sha256((scope + "\0" + event_id).encode()).hexdigest()
    date = label("stardew", day)
    with M._JOURNEY_LOCK:
        M.record_discoveries(base, "stardew", scope=scope, discoveries=[dict(
            id=event_id, kind="text_read", subject="calendar", text=text, source=SOURCE, date=date,
            event_place=place)], day=day, day_label=date, scoped=mind_scoped)
        SeenReceipts(base, scope).note("calendar_day_read", entry, game_day=day, clock=clock, place=place,
                                       source=SOURCE, text_ref=text_ref)
    return "hari %d: %s (papan kalender, dibaca %s)" % (number, " ".join(text.split()), date)


def read_card(results, receiver):
    """This board's reads so far, newest last, for her page (her own record; at most the last 6)."""
    rows = [r for r in results if isinstance(r, Mapping) and r.get("receiver") == receiver]
    return [r["line"] for r in rows[-6:]]
