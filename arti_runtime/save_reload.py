"""ADR 232 part H: a save that was loaded back to an earlier game day, as one dated row of her existing ledger.

Owner, 2026-11-18: the night stays in her memory, the game does not count as advanced, and she must understand that
the game was loaded back. When a session starts on a save whose game day is earlier than the latest day of her
record for that farm (the present timeline: her day snapshots in ``progress.jsonl``, written with their wall time),
the runtime writes one ``save_reload`` row (kind of ``ledger.jsonl``, the existing store): from which game day back
to which, and when in real time. World records written before it and dated on or after the day loaded back to
belong to the days before the reload; her experience rows stay and read as what she remembers from before it.
Nothing here tells her what to do about it.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import time

GAME = "stardew"
KIND = "save_reload"
WIB = timezone(timedelta(hours=7))


def reloads(base, scope):
    """Her reload rows for this farm, oldest first (by the real time they were written)."""
    if base is None or not scope:
        return []
    import arti_mind as M
    from arti_runtime import jsonl_snapshot  # one parse per ledger revision (ADR 231 part C): read on every page
    try:
        ledger = jsonl_snapshot.rows(M.game_dir(base, GAME) / M.LEDGER_FILE)
    except (OSError, ValueError):
        return []
    scope_hash = M._scope_hash(scope)
    rows = [r for r in ledger if r.get("scope_hash") == scope_hash and r.get("kind") == KIND
            and isinstance(r.get("facts"), Mapping) and type(r["facts"].get("to_day")) is int
            and type(r["facts"].get("from_day")) is int and type(r["facts"].get("at")) in (int, float)]
    return sorted(rows, key=lambda r: r["facts"]["at"])


def latest(base, scope):
    rows = reloads(base, scope)
    return rows[-1]["facts"] if rows else None


def before_reload(facts, *, day, at):
    """True when a world record of game day ``day`` written at wall time ``at`` belongs to the days before ``facts``'
    reload: written before it and dated on or after the day it went back to. Unknown write time: dated after it."""
    if not isinstance(facts, Mapping) or type(day) is not int:
        return False
    if type(at) in (int, float):
        return at < facts["at"] and day >= facts["to_day"]
    return day > facts["to_day"]


def present_day(base, scope):
    """The latest game day of her record on the present timeline, or None when the record holds no day."""
    import arti_mind as M
    last = latest(base, scope)
    days = [r["day"] for r in M.progress_days(base, GAME, scope=scope, include_before_reload=True)
            if last is None or (type(r.get("at")) in (int, float) and r["at"] >= last["at"])]
    if last is not None:
        days.append(last["to_day"])
    return max(days) if days else None


def note(base, scope, day, *, wall=None):
    """At a session's start: write the reload row when the save's game ``day`` is earlier than her record's present
    day. Returns the new row's facts, or None (no record, the same day or later, or no farm scope)."""
    if base is None or not scope or type(day) is not int:
        return None
    present = present_day(base, scope)
    if present is None or day >= present:
        return None
    import arti_mind as M
    from arti_runtime.plan_dates import label
    at = float(time.time() if wall is None else wall)
    stamp = datetime.fromtimestamp(at, timezone.utc)
    facts = dict(from_day=present, from_label=label(GAME, present), to_day=day, to_label=label(GAME, day), at=at,
                 utc=stamp.isoformat().replace("+00:00", "Z"), wib=stamp.astimezone(WIB).strftime("%Y-%m-%d %H:%M"))
    M.record_items(base, GAME, scope=scope, kind=KIND, day=day, day_label=facts["to_label"],
                   items=[dict(id="reload:%d:%d:%d" % (present, day, int(at)), status="observed", facts=facts)])
    return facts


def line(base, scope, today):
    """One plain line while the game day is not past the day the save went back from; None otherwise."""
    last = latest(base, scope)
    if last is None or type(today) is not int or today > last["from_day"]:
        return None
    from arti_runtime.plan_dates import label
    first = last["to_day"] + 1
    gone = last["from_label"] if first >= last["from_day"] else "%s sampai %s" % (label(GAME, first), last["from_label"])
    return ("save dimuat ulang %s WIB: hari game kembali dari %s ke %s; yang kuingat dari %s terjadi sebelum save "
            "dimuat ulang" % (last["wib"], last["from_label"], last["to_label"], gone))
