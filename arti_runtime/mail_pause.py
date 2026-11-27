"""The flagged mailbox beside her door, as she walks out past it. Facts only; no order, no ranking.

Letters can pile up in her mailbox for days while its flag stands on most pages, because the mailbox is
offered only on a page in the Cabin, where the box is not, before the trip her plan names; that trip walks
out of the door and past the box without a page.

The same shape as the shipping box on the way home: a model-chosen trip from her home that has just
crossed into the Farm while the native mailbox flag stands pauses once that day through the route pause. The
Farm page is an ordinary page; the mailbox is on it only when the page's own rules find it (the native press beside
her). Continuing the trip stays offered, first. Setting ``stardew_live7_mail_on_the_way``, default off.
"""
from __future__ import annotations

from collections.abc import Mapping
import time

SETTING = "stardew_live7_mail_on_the_way"
FARM = "Farm"  # the game's own id for the map that holds her home door
HOMES = ("Cabin", "FarmHouse")
FRESH_S = 2.0


def enabled(config):
    return (config or {}).get(SETTING, False) is True


def flagged(owner, refs=()):
    """True only when a fresh native frame of her own says that letters lie in her mailbox (the flag the game draws) and
    lists that box among what she can walk up to and press here (``refs``: retired today). Out of another player's
    door her own box is far away: no pause there, and the day's one pause is not used up."""
    from arti_stardew_world_extras import extras_targets
    rec = owner._obs.get(owner._binding_view()[1], "ui.context")
    received = getattr(rec, "received_monotonic", None)
    if (rec is None or getattr(rec, "available", True) is False or getattr(rec, "stale", False)
            or getattr(rec, "authoritative", True) is not True or getattr(rec, "requires_resync", False)
            or type(received) not in (int, float) or not 0 <= time.monotonic() - received <= FRESH_S):
        return False
    seen = getattr(rec, "facts", None) or {}
    count = seen.get("mailbox_count")
    return (type(count) is int and count > 0 and seen.get("mode") == "gameplay"
            and any(t["kind"] == "mailbox" and t["ref"] not in refs for t in extras_targets(seen)))


def due(owner, session, call, facts, previous, *, reserve):
    """The pause record for a trip that just left her home with the mailbox flagged, else None."""
    import arti_stardew_live7 as L
    import arti_stardew_skills as S
    if not enabled(getattr(owner, "_config", None)) or reserve:
        return None
    c = session.campaign
    p, key = c.live, (tuple(c.date), FARM + ":mail")
    if (facts.location != FARM or not str(previous or "").startswith(HOMES) or key in p.route_pauses
            or getattr(call, "name", None) != "pergi_ke" or L.destination_map(call.arg) in (FARM, None)
            or call.arg == "home" or owner._live7_route_pause_fence(p, facts) is not None):
        return None
    mail = S.SkillCall("cek_pagi", "mail")
    # "In the time left", and not a press that has failed here today: the choice must be one she could make.
    if (L.admission(mail, facts, p) is not None or p.blocked(mail, FARM)
            or L.admission(call, facts, p) is not None or p.blocked(call, FARM)):
        return None
    if not flagged(owner, p.refs):
        return None
    return dict(date=list(c.date), map=FARM, once=key[1], from_map=previous, destination=call.arg, reserve=False,
                villagers=[], villager_refs=[], clock=facts.time_of_day, mail=True)


def pause_line(pause: Mapping, place, offered: bool) -> str:
    """The page fact for this pause: where she was going and what she sees. The press is described by its own choice."""
    tail = ("pergi_ke %s ditawarkan: memilihnya melanjutkan perjalanan yang sama" % pause.get("destination") if offered
            else "pergi_ke %s tidak ditawarkan di halaman ini" % pause.get("destination"))
    # The page says the trip is kept, not what to choose.
    return "perjalanan ke %s berhenti di %s, kotak suratmu di dekat pintu bertanda surat; perjalanan ke %s tetap bisa " \
           "dilanjutkan setelah ini; %s." % (pause.get("destination"), place or str(pause.get("map") or ""),
                                             pause.get("destination"), tail)
