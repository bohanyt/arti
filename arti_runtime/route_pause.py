"""ADR 155: a route pauses where people are, and a carried goal says how old it is. Facts only; no orders.

a63 (`ANALYSIS_A63_TEMPO_SOCIAL_2026-11-10.md` sections 1 and 3): a trip was one decision and the evening home route was
the clock reserve's own plan, so she made no decision outside the Cabin and the Mines and walked past Linus, Robin and Maru on
the Mountain without a page. A goal with horizon *today* was carried nine days with no line saying so.

What counts as "something new" on a map is generic and uses the page's own talk rule: a talkable villager on the map she has
not talked to today (``social_facts.villagers_here`` / ``talk_args``, the rule that opens ``ngobrol_hadiah``). Other "new"
things were not included (ADR 155, "What is new"): an open shop needs the destination door hours that are published only when
she stands at the door's map with the full catalogue, and a sighting needs the forage classifier on a full context read. Neither
is a cheap generic read at a mid-route map change.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

ROUTE_PAUSE_REASON = "route_paused_new_map"   # old journals (ADR 155); still read
ROUTE_PAUSE_PEOPLE = "route_paused_people_here"  # live ending (ADR 195): same pause, not a failed walk
ROUTE_PAUSE_MAIL = "route_paused_mailbox"  # the flagged mailbox beside her door, its own reason
PAUSE_NAMES = 4          # villager names on the pause fact; a cut says how many more
CARRIED_SHOWN = 3        # today goals on the age line (the today cap is 3)
CARRIED_TEXT = 60        # chars of her goal text on the age line


def pause_reason(reason) -> bool:
    """Live people-page or mailbox pause, or the old journal reason for the same pause."""
    return reason in (ROUTE_PAUSE_REASON, ROUTE_PAUSE_PEOPLE, ROUTE_PAUSE_MAIL)


def scene_on_map(scene: Mapping | None, location) -> dict:
    """The scene facts when they describe ``location`` (its own ``location`` fact), else {}: a read from the previous map
    or a scene that has not caught up with the warp never names who is here."""
    if not isinstance(scene, Mapping) or not isinstance(location, str) or not location:
        return {}
    if not any(isinstance(f, Mapping) and f.get("kind") == "location" and f.get("location_id") == location
               for f in scene.values()):
        return {}
    return dict(scene)


def new_villagers(scene: Mapping | None) -> list[dict]:
    """Talkable villagers on this map she has not talked to today, each ``{ref, name}``: the set ``talk_args`` offers."""
    from arti_runtime.social_facts import talk_args, villagers_here
    offered = set(talk_args(scene))
    return [dict(ref=r["ref"], name=n) for r in villagers_here(scene) if not r["talked_today"]
            for n in [next((x for x in (r["name"], r["internal"]) if x in offered), None)] if n]


def pause_line(pause: Mapping, place: str | None, offered: bool) -> str:
    """The page fact for a paused route: where she was going, who is here, whether continuing is offered."""
    if pause.get("mail"):
        from arti_runtime.mail_pause import pause_line as mail_line  # the flagged mailbox beside her door
        return mail_line(pause, place, offered)
    if pause.get("box"):
        from arti_runtime.ship_pause import pause_line as box_line  # ADR 228 part D: the box on the way home
        return box_line(pause, place, offered)
    names = [str(n) for n in pause.get("villagers") or ()]
    more = ", +%d lagi" % (len(names) - PAUSE_NAMES) if len(names) > PAUSE_NAMES else ""
    where = place or str(pause.get("map") or "")
    head = ("perjalanan ke %s (pulang) berhenti di %s" if pause.get("reserve") else "perjalanan ke %s berhenti di %s") % (
        pause.get("destination"), where)
    tail = ("pergi_ke %s ditawarkan: memilihnya melanjutkan perjalanan yang sama" % pause.get("destination") if offered
            else "pergi_ke %s tidak ditawarkan di halaman ini" % pause.get("destination"))
    return "%s, karena di peta ini ada warga yang belum disapa hari ini: %s%s; %s." % (
        head, ", ".join(names[:PAUSE_NAMES]), more, tail)


def carried_goal_line(goals: Iterable[Mapping], *, day, game: str) -> str | None:
    """Her carried *today* goals with their age from ``goals.jsonl`` (``carried_days``) and the last day she cited
    progress (its ``evidence`` rows), or None. Plain facts: no advice, no replacement offered."""
    import arti_mind as M
    from arti_runtime import plan_dates
    if type(day) is not int:
        return None
    parts = []
    for g in goals:
        if (not M.valid_goal(g) or g.get("horizon") != "today" or not M.goal_alive(g, day)
                or type(g.get("carried_days")) is not int or g["carried_days"] < 1):
            continue
        rows = g.get("evidence") or []
        last = rows[-1] if rows and isinstance(rows[-1], (list, tuple)) and len(rows[-1]) >= 1 else None
        when = (plan_dates.label(game, last[0]) if last is not None and type(last[0]) is int
                else str(last[0]) if last is not None else None)
        text = " ".join(str(g["text"]).split())
        text = text if len(text) <= CARRIED_TEXT else text[:CARRIED_TEXT - 3] + "..."
        parts.append('%s "%s": dibawa %d hari, belum tercapai; %s' % (
            g["id"], text, g["carried_days"], "terakhir maju " + when if when else "belum ada bukti maju"))
    return " | ".join(parts[:CARRIED_SHOWN]) or None
