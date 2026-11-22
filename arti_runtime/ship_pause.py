"""ADR 228 part D: the shipping box as a reachable evening choice. Facts only; no order, no ranking.

Attempt 85, page by page (all three scenario runs): every evening she chose ``pergi_ke home`` from the Mine or
the Mountain. That one trip walked across the Farm, where the shipping box is, without a decision page, and her
next page was in the Cabin, where no box is. ``sleep_only`` was false on every evening page, so the bedtime
reserve did not remove ``kirim``; the box was simply never on a page she saw while she held goods.

The cause is fixed where it is: a model-chosen (or reserve) homeward trip that crosses into the Farm while her bag
holds storable goods, and while a ``kirim`` still fits the clock admission, pauses once that day through ADR 155's
route pause. The Farm page is then an ordinary page; ``kirim`` is on it only when the page's own rules find the box
(the mod's approach catalog or a published, reachable bin) and goods the box takes. Continuing home stays offered.
Setting ``stardew_live7_ship_on_the_way``, default off.
"""
from __future__ import annotations

from collections.abc import Mapping

SETTING = "stardew_live7_ship_on_the_way"
FARM = "Farm"  # the game's own id for the map that holds her home door
DOES = ("Taruh barang dari tasmu ke kotak pengiriman: satu nama, beberapa nama dipisah ';', atau 'semua'; "
        "paling banyak 8 tumpuk sekali pilih.")


def enabled(config):
    return (config or {}).get(SETTING, False) is True


def due(owner, session, call, facts, previous, *, reserve):
    """The pause record for a homeward trip that just entered the Farm with goods in the bag, else None.

    The caller (``_live7_route_pause_due``) already refused a repeat for this map today, the destination map,
    and the safety fences (low HP, low energy, a recent hit).
    """
    import arti_stardew_live7 as L
    import arti_stardew_skills as S
    if not enabled(getattr(owner, "_config", None)):
        return None
    if facts.location != FARM or previous == FARM or call != S.SkillCall("pergi_ke", "home"):
        return None
    stacks = [s for s in (getattr(facts, "store_stacks", None) or ()) if isinstance(s, (tuple, list)) and len(s) == 3]
    if not stacks:
        return None  # nothing in the bag a box takes (tools and food are not storable stacks)
    p = session.campaign.live
    # "In the time left": a kirim here must still pass the same clock admission as every other choice.
    if L.admission(S.SkillCall("kirim", str(stacks[0][0])), facts, p) is not None:
        return None
    return dict(date=list(session.campaign.date), map=FARM, from_map=previous, destination=call.arg or "home",
                reserve=reserve, villagers=[], villager_refs=[], clock=facts.time_of_day,
                box=True, goods_stacks=len(stacks))


def pause_line(pause: Mapping, place, offered: bool) -> str:
    """The page fact for this pause: where she was going and what she holds. The box is described by the page's own
    ``kirim`` entry when that choice is there; this line never tells her to ship or what shipping pays."""
    where = place or str(pause.get("map") or "")
    head = ("perjalanan ke %s (pulang) berhenti di %s" if pause.get("reserve") else "perjalanan ke %s berhenti di %s") % (
        pause.get("destination"), where)
    tail = ("pergi_ke %s ditawarkan: memilihnya melanjutkan perjalanan yang sama" % pause.get("destination") if offered
            else "pergi_ke %s tidak ditawarkan di halaman ini" % pause.get("destination"))
    return "%s, dengan %d tumpuk barang di tasmu; %s." % (head, int(pause.get("goods_stacks") or 0), tail)


def page_text(offer):
    """The ``kirim`` entry says what the batch really does; no payment words until she has seen the game say it."""
    for entry in offer.get("skills", ()) if isinstance(offer, Mapping) else ():
        if isinstance(entry, dict) and entry.get("name") == "kirim":
            entry["does"] = DOES
    return offer


def offer_origin(context, snapshot):
    """(grid, origin) as ``_day_plan`` reads them: the observed nav window (her own cell counted open) and the routing
    origin, an adjacent observed-open cell when she stands on a warp or bed row (the same filter as its debris offer)."""
    import arti_stardew_skills as S
    from arti_stardew_animal_routine import actor_cell_open
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    grid = actor_cell_open(context.get("nav_grid"), here)
    origin = here
    if grid and here is not None and grid.get(here) != ".":
        x, y = here
        origin = min((t for t in ((x, y-1), (x-1, y), (x+1, y), (x, y+1)) if grid.get(t) == "."), default=None)
    return grid, origin


def confirmed_bin_approachable(context, policy, grid, origin):
    """Use the established stand check without its provisional out-of-window offer."""
    from types import SimpleNamespace
    from arti_stardew_live7 import bin_approachable

    evidence = dict(context)
    evidence.pop(SETTING, None)
    return bin_approachable(evidence, SimpleNamespace(refs=policy.refs), grid, origin)


def box_calls(context, policy, snapshot, kinds, item_policy):
    """ADR 231 part A: [(item name, stack)] for every stack the box takes, when the box is here and can be reached.

    Attempt 87: the box pause stopped her trip home at the Farm's edge (40,0) five evenings; the 40x40 window there ends
    at x 63 and the bin's stand (71,15) lay outside it, so ``bin_approachable`` left ``kirim`` off the page the pause
    was for, while ``aksi`` on the same bin was offered and its approach reached the stand. With the setting (its
    flag on ``DayPolicy``), a stand outside the observed window is unknown, not blocked: the same approach ``aksi`` and
    ``kirim`` use walks to it, and a refused approach retires the bin for the day (``policy.refs``). A stand inside
    the window that is neither open nor clearable stays unoffered; no window, no published bin: nothing. Like the
    box's own ``aksi`` row and the chest (ADR 186), these are local choices: the caller adds them before the bedtime
    reserve and the low-HP returns."""
    import arti_stardew_live7 as L
    grid, origin = offer_origin(context, snapshot)
    if not L.bin_approachable(context, policy, grid, origin, kinds):
        return []
    ships = L.disposable(context, item_policy, "ship")
    return [(row.get("name"), min(999, row["stack"])) for row in L.goods_facts(context)
            if row.get("qualified_item_id") not in policy.uncertain_items and row in ships]


def batch_refused(selected, fresh):
    """ADR 232 part G: a goods choice whose stacks are no longer offered when it is about to run is refused to her with
    what the re-read page shows: the same feedback shape as any changed offer (``offer_changed``, ADR 210)."""
    name, arg = getattr(selected, "name", None), getattr(selected, "arg", None)
    still = sorted({c.arg for c in fresh or () if getattr(c, "name", None) == name and isinstance(c.arg, str)})
    return dict(code="offer_changed", input_taken=False, submitted=dict(name=name, arg=arg), field="current_offer",
                valid=("Barang pilihanmu tidak ada lagi di pilihan %s saat dibaca ulang; pilihan %s sekarang: %s."
                       % (name, name, ", ".join(still)) if still else
                       "Pilihan %s tidak ada lagi di halaman yang dibaca ulang; pilih dari halaman baru." % name))
