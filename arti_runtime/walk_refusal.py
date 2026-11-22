"""ADR 231 part B1: a dialogue the game opens during her walk is the walk's outcome, in the game's own words.

Attempt 87 (Y1 winter 8): told at 07:10 she chose ``pergi_ke forest``; at 08:20 on the Farm the game opened a
DialogueBox mid-walk, "[recorded remark omitted]" (captured as ``door_message``). The walk
ended ``UNCERTAIN(MENU_OPENED_MID_MOVE)``: that quarantined the trip on the Farm for the rest of the day and made it
``last_failed`` (``DayPolicy.record``), so page 003 (08:40, Farm) did not offer ``forest`` at all, and the words never
rode on that trip.

Rule (fact line on): when a ``pergi_ke`` walk ends that way, she is free on the map she was walking on, the walk's
input is released, and the game's dialogue text from this walk was captured (``game_text`` ledger, ADR 219: the kind
``door_message`` or ``dialogue_read``, today, at this place, at or after the walk's start), the walk's result is
``game_dialogue_mid_walk``: an INTERRUPTED row (no failure count, no quarantine, not ``last_failed``) and no recovery
FAULT. The trip stays a choice; for the rest of that game day its label carries the words, dated with the game clock
and place where she saw them, as text she was shown. No word list, no festival rule, no map name.
"""
from __future__ import annotations

from collections.abc import Mapping

REASON = "game_dialogue_mid_walk"
EVENT_REASON = "game_event_mid_walk"
KINDS = ("door_message", "dialogue_read")
MID_MOVE = "MENU_OPENED_MID_MOVE"


def captured(base, scope, *, date, place, since_clock, event_id=None):
    """The game's dialogue text from this walk: newest complete capture today at ``place`` since ``since_clock``."""
    if (base is None or not scope or (not place and event_id is None)
            or not isinstance(date, (list, tuple)) or len(date) != 3):
        return None
    from arti_runtime.game_text_delivery import _rows, _text
    from arti_runtime.native_story import day_of
    today = day_of("%s:%s:%s" % tuple(date))
    best = None
    for row in _rows(base, scope):
        f = row["facts"]
        event_text = event_id is not None and f.get("kind") == "event_watched" and f.get("id") == event_id
        if (not (event_text if event_id is not None else f.get("kind") in KINDS)
                or f.get("day") != today or (place is not None and f.get("place") != place)
                or type(f.get("clock")) is not int or (type(since_clock) is int and f["clock"] < since_clock)
                or f.get("capture_complete") is not True or len(f.get("chunks") or {}) != f.get("parts")):
            continue
        text = _text(f)
        if not text.strip():
            continue
        key = (f["clock"], f.get("capture_ticks", 0), f.get("native_seq", 0))
        if best is None or key > best[0]:
            best = (key, dict(text=text, clock=f["clock"], place=f.get("place"), day=today, kind=f["kind"],
                              text_ref="game_text:" + row["item_id"]))
    return best[1] if best else None


def due(call, action, cleanup, baseline):
    """The positive evidence a walk ended on a game dialogue and went nowhere (the text is looked up separately)."""
    from arti_stardew_skills import _g
    return (getattr(call, "name", None) == "pergi_ke" and MID_MOVE in str(_g(action, "reason") or "")
            and isinstance(cleanup, Mapping) and cleanup.get("menu") is None and cleanup.get("free") is True
            and cleanup.get("input_released") is True and cleanup.get("food_clear") is not False
            and isinstance(baseline, Mapping) and cleanup.get("location") == (_g(action, "before") or {}).get("location")
            and bool(cleanup.get("location")))


def refused(runtime, session, run, action, state):
    """At the walk's terminal (``_live7_recover``, fact line on): True when the game's dialogue ended this walk on the
    map she was on and its words were captured; the words then ride on that trip today (journaled once)."""
    baseline = run.state.get("live7_baseline") or {}
    if not due(run.call, action, state, baseline):
        return False
    c = session.campaign
    words = captured(runtime._live7_mind_base(), getattr(runtime, "_game_text_scope", None), date=list(c.date),
                     place=state.get("location"), since_clock=baseline.get("time_of_day"))
    if words is None:
        return False
    remember(c.live, run.call, words)
    runtime._journal(dict(ev="live7_walk_refused", skill=run.call.name, arg=run.call.arg, place=words["place"],
                          clock=words["clock"], kind=words["kind"], text_ref=words["text_ref"]))
    from arti_runtime import day_record
    day_record.live(runtime, c.date, words["clock"], "walk_refused",
                    f'Aku terhenti di jalan. Game bilang "{words["text"]}"',
                    place=words["place"], witness=words["text_ref"])
    return True


def event_arrival(runtime, session, run, action, state):
    """A released walk reached an active native event on a different map."""
    from arti_stardew_skills import _g
    baseline = run.state.get("live7_baseline") or {}
    before = (_g(action, "before") or {}).get("location") or baseline.get("location")
    here = state.get("location")
    if (run.call.name != "pergi_ke" or MID_MOVE not in str(_g(action, "reason") or "")
            or not before or not here or before == here or state.get("input_released") is not True
            or state.get("food_clear") is False):
        return False
    native_event = getattr(runtime, "_live7_native_event", None)
    event = native_event() if native_event is not None else None
    if not event or event.get("event_up") is not True or not event.get("event_id"):
        return False
    c = session.campaign
    # The opening may be rendered on the entry map before the event map is
    # installed. The exact active event identity scopes those native words.
    words = captured(runtime._live7_mind_base(), getattr(runtime, "_game_text_scope", None), date=list(c.date),
                     place=None, since_clock=baseline.get("time_of_day"), event_id=event["event_id"])
    runtime._journal(dict(ev="live7_walk_entered_event", skill=run.call.name, arg=run.call.arg, place=here,
                          event_id=event["event_id"], **({"game_words": words} if words else {})))
    if (getattr(runtime, "_config", None) or {}).get("voice_game_memory_enabled") is True:
        from arti_runtime import day_record
        motor = runtime._state.latest(runtime._binding_view()[1])
        clock = words["clock"] if words else getattr(motor, "time_of_day", None)
        catalog = getattr(runtime, "_live7_fact_line_catalog", None) or getattr(runtime, "_live7_raw_knowledge", None)
        from arti_runtime.native_story import day_of
        label, text = day_record.event_words(catalog, event["event_id"], day_of(":".join(map(str, c.date))), clock, words)
        day_record.live(runtime, c.date, clock, "event_entered", text, place=here,
                        witness=_g(action, "request_id"), event_label=label)
    return True


def category(outcome, current, fence):
    """``_live7_leg``'s class for that row: INTERRUPTED (no failure count, quarantine or ``last_failed``,
    ``DayPolicy.record``), unless a safety stop or an unresolved mutation already owns it."""
    import arti_stardew_live7 as L
    if outcome[1] in (REASON, EVENT_REASON) and current != L.SAFETY_STOP and not fence:
        return L.INTERRUPTED
    return current


def remember(policy, call, words):
    """Today's refusal words on that trip (``policy.walk_refusals``, keyed by absolute game day); other days dropped."""
    rows = {k: v for k, v in (getattr(policy, "walk_refusals", None) or {}).items() if k[0] == words["day"]}
    rows[(words["day"], str(call.arg))] = dict(words)
    policy.walk_refusals = rows


def label(policy, today, arg):
    """The words as her page shows them on that trip on game day ``today`` (absolute), or None."""
    from arti_runtime.native_notices import NOTICE_SOURCE, _clock, words as plain
    from arti_runtime.plan_dates import label as day_label
    row = (getattr(policy, "walk_refusals", None) or {}).get((today, str(arg)))
    if not row:
        return None
    return '"%s" (%s, %s %s, %s; perjalanan ini berhenti di sana)' % (
        plain(row["text"]), NOTICE_SOURCE, day_label("stardew", row["day"]), _clock(row["clock"]), row["place"])
