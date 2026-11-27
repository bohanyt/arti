"""ADR 241: one rule in the offer path for every skill: a choice withheld from her next page, with its reason in the
game's terms, while nothing she did has changed the game since (the effect clock's world state, ADR 241 A).

Sources: the ladder's rung 2 (the choice she last repeated without an effect) and invariant C1: a choice whose last
run was refused with zero inputs is not offered again unchanged, for every skill (attempt 90: the way up refused 45
times with zero inputs, offered again each time). "Unchanged" is the effect clock's world state and her place: the
choice returns as soon as anything she does changes the game, or she is somewhere else. The rule never empties a
page: when every choice would be withheld, the page keeps them and still says why. What else she can do comes first.
A failed walk to a sighting uses the same world and place test, with the final pose and navigation grid.
That refusal belongs to the object ref, even after inputs, and leaves other targets available.
"""
from __future__ import annotations

from arti_runtime.effect_clock import EffectClock
from arti_stardew_animal_routine import actor_cell_open

KEY = "ditahan"  # her page's fact line naming what is withheld and why
TOLD = "pemulihan_terakhir"  # her page's fact line: what the ladder did, once


def _clock(runtime, session):
    slot = runtime.__dict__.get("_live7_effect_slot")
    return slot[1] if slot is not None and slot[0] == session.session_id and isinstance(slot[1], EffectClock) else None


def _ladder(runtime, session):
    slot = runtime.__dict__.get("_live7_ladder_slot")
    return slot[1] if slot is not None and slot[0] == session.session_id else None


def refusal_words(reason):
    """C1: why, in her words; the game's own refusal code stays beside them."""
    return "tadi ditolak sebelum satu tombol pun ditekan (%s), dan sejak itu belum ada yang berubah di game" % (
        str(reason or "ditolak")[:80])


def _unchanged(state, clock, place):
    if "refused_world" not in state:
        state["refused_world"], state["refused_place"] = clock.serial, place
    return state["refused_world"] == clock.serial and state["refused_place"] == place


def failed_route_ref(run, reason, place):
    """Only an exhausted walk to an identified sighting supplies route evidence."""
    if run.call.name == "forage":
        look = run.state.get("live7_forage_look") or {}
        pinned = look.get("target") if look.get("location") == place else None
        reasons = ("route_no_clearable_way", "no_open_look_path", "forage_not_offered:no_open_look_path")
    elif run.call.name == "jalan_ke":
        pinned = run.state.get("route_seen_walk")
        if pinned and pinned.get("location") != place:
            pinned = None
        reasons = ("route_no_clearable_way", "no_open_look_path", "seen_walk_no_open_path")
    else:
        return None
    return pinned.get("ref") if pinned and reason in reasons else None


def bind_route(run, ref, clock, place, here, grid):
    basis = dict(ref=ref, here=here, grid=dict(actor_cell_open(grid, here) or {}))
    _unchanged(basis, clock, place)
    run.state["route_refusal"] = basis


def route_refs(runtime, session, place, here, grid):
    """Live route refusals by object ref, regardless of which skill walked to it."""
    clock = _clock(runtime, session)
    if clock is None or place is None or here is None:
        return set()
    grid = actor_cell_open(grid, here)
    refs = set()
    for run in getattr(session, "runs", ()) or ():
        basis = getattr(run, "state", {}).get("route_refusal")
        if basis is None or run.status != "failed":
            continue
        if (_unchanged(basis, clock, place) and basis["here"] == here
                and (not grid or basis["grid"] == grid)):
            refs.add(basis["ref"])
        else:
            # A new world releases the refusal even if she later returns.
            run.state["route_refusal"] = None
    return refs


def refused(runtime, session, facts=None):
    """C1: [(name, arg, reason)] whose latest run was refused with zero inputs, while nothing she did has changed the
    game since and she is at the same place. Each refusal is bound, at the first page after it, to the clock's world
    state and her place (no input was sent between the refusal and that page)."""
    clock = _clock(runtime, session)
    if clock is None:
        return []
    place = getattr(facts, "location", None)
    latest = {}
    for run in getattr(session, "runs", ()) or ():
        if getattr(run, "status", None) not in ("finished", "failed", "unknown"):
            continue
        latest[(run.call.name, run.call.arg)] = run
    out = []
    for key, run in latest.items():
        if run.status != "failed" or run.inputs != 0:
            continue
        state = run.state
        if "route_refusal" in state:
            continue  # the target is withheld, including when other forage remains
        if state.get("halt_interrupted"):
            if runtime.__dict__.get("_live7_ladder_halted") == session.session_id:
                out.append((key[0], key[1], refusal_words(run.reason)))
            continue  # a session HALT refusal has no world or target basis
        if _unchanged(state, clock, place):
            out.append((key[0], key[1], refusal_words(run.reason)))
    return out


def withheld(runtime, session, facts=None):
    """[(name, arg, reason)] withheld from this page now."""
    out = refused(runtime, session, facts)
    ladder = _ladder(runtime, session)
    if ladder is not None and ladder.withheld and tuple(ladder.withheld[:2]) not in {r[:2] for r in out}:
        out.append(tuple(ladder.withheld[:3]))
    return out


def apply(runtime, session, calls, facts=None):
    """(calls her page offers, the page line or None). Withheld choices leave the page; the rest keep their order.
    ADR 243: her raw hands are the floor of the page; they are never withheld (a refusal or "nothing changed" is a
    fact on her page, `tangan_hasil`, not a reason to take her hands away)."""
    rows = withheld(runtime, session, facts)
    from arti_runtime import raw_hands
    if rows and raw_hands.enabled(getattr(runtime, "_config", None)):
        from types import SimpleNamespace
        rows = [r for r in rows if not raw_hands.is_raw(SimpleNamespace(name=r[0], arg=r[1]))]
    if not rows:
        return calls, None
    keys = {(name, arg) for name, arg, _ in rows}
    kept = [c for c in calls if (c.name, c.arg) not in keys]
    shown = [r for r in rows if any((c.name, c.arg) == r[:2] for c in calls)]
    if not shown:
        return calls, None
    line = "; ".join("%s%s: %s" % (name, " " + str(arg) if arg not in (None, "") else "", reason)
                     for name, arg, reason in shown)
    if not kept:
        return calls, "Semua pilihan di sini baru saja dicoba tanpa perubahan (" + line + "); tetap kutawarkan."
    return kept, "Kutahan dulu sampai ada yang berubah: " + line + ". Pilihan lain di halaman ini tetap bisa."


def told(runtime, session):
    """The ladder's line for her next page, once (then it lives only in her ONE record)."""
    ladder = _ladder(runtime, session)
    if ladder is not None and ladder.told:
        text, ladder.told = ladder.told, None
        return text
    resume = (getattr(runtime, "_config", None) or {}).get("stardew_live7_resume") or {}
    text = resume.get("ladder_told") if isinstance(resume, dict) else None
    if isinstance(text, str) and text.strip() and not runtime.__dict__.get("_live7_ladder_resume_told"):
        runtime._live7_ladder_resume_told = True  # a fresh cycle after rung 5: said once on its first page
        return text
    return None
