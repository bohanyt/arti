"""Where she stands, what can be pressed there, and that she has been there (ADR 197)."""
from arti_runtime.native_rows import native_rows
from collections.abc import Mapping

UNKNOWN = "tempat (nama belum diketahui)"
EMPTY = "di sini tidak ada yang bisa dikerjakan selain keluar"

# Skills that act on this map. Menus, reads and leaving are not "something to do here".
HERE = frozenset({
    "aksi", "ngobrol_hadiah", "forage", "jual", "beli", "kirim", "simpan_ke_chest",
    "cangkul", "tanam", "siram", "panen", "tangkap_ikan", "gali_artefak", "elus",
    "rawat_hewan", "tebang", "pecah_batu", "pungut_drop", "masak", "ikuti_festival",
    "jalan_ke", "isi_gembor", "bersihkan", "tambang_bijih", "lawan", "lawan_mine",
})


def here_name(location, catalog=None, calendar=None, today=None, names=None):
    """Readable name of the current map from native labels; unknown stays an honest unknown."""
    from arti_runtime.place_names import current_place_label, source_place
    if location == "Temp":
        if fest := current_place_label(location, calendar, today):
            return fest
    found = []
    if isinstance(names, Mapping) and isinstance(location, str) and names.get(location):
        found.append(names[location])
    if catalog is not None and location:
        found.append(source_place(location, catalog))
    if labeled := current_place_label(location, calendar, today):
        found.append(labeled)
    return next((n for n in found if n and n != UNKNOWN), UNKNOWN)


def _bits(calls, context):
    from scripts.live7.interactions import ordinary_targets
    from arti_runtime.social_facts import villagers_here
    out = []
    seen = set()
    for row in ordinary_targets(context or {}):
        kind, ref = row.get("kind"), row.get("ref")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        label = row.get("label") or kind
        if row.get("status") in ("openable", "available"):
            out.append(str(label))
        elif kind:
            out.append("%s (%s)" % (label, row.get("status") or "tidak bisa"))
    for who in villagers_here((context or {}).get("targets")):
        name = who.get("name")
        if name and name not in seen:
            seen.add(name)
            out.append("warga " + name)
    for call in calls or ():
        name, arg = getattr(call, "name", None), getattr(call, "arg", None)
        if name not in HERE:
            continue
        key = (name, arg)
        if key in seen:
            continue
        seen.add(key)
        if name == "aksi" and arg in seen:
            continue
        if name == "ngobrol_hadiah" and arg:
            text = "ngobrol " + str(arg)
            if text not in out:
                out.append(text)
        elif name not in ("aksi", "ngobrol_hadiah"):
            out.append(name if arg is None else "%s %s" % (name, arg))
    return out[:8]


def actionable_line(calls, context=None):
    bits = _bits(calls, context)
    return ("; ".join(bits)[:96]) if bits else EMPTY


def here_line(location, calls, *, catalog=None, calendar=None, today=None, names=None, context=None):
    return here_name(location, catalog, calendar, today, names)


def goal_places(catalog, date):
    """Places this actor has entered, for goal native_status. Unvisited maps are omitted, never 'unknown'."""
    from arti_runtime.experience_facts import entered
    known = entered(catalog or {})
    labels = {p["id"]: p.get("label") for p in native_rows(((catalog or {}).get("world_graph") or {}).get("places", ()))
              if isinstance(p, Mapping) and p.get("id")}
    rows = []
    for loc in known:
        if not isinstance(loc, str) or not loc:
            continue
        label = labels.get(loc)
        aliases = [a for a in (label, loc) if isinstance(a, str) and a and a != "area belum dijelajahi"]
        rows.append(dict(location=loc, date=date, status="visited", aliases=list(dict.fromkeys(aliases))))
    return rows


def _visit_row(row):
    if not isinstance(row, Mapping):
        return None
    kind = row.get("kind") or row.get("Kind")
    subject = row.get("subject") or row.get("Subject")
    if kind != "location_entered" or not isinstance(subject, str) or not subject:
        return None
    ident = row.get("id") or row.get("Id")
    if not isinstance(ident, str) or not ident:
        ident = "location_entered:" + subject
    return dict(id=ident, kind="location_entered", subject=subject,
                text=row.get("text") or row.get("Text") or subject,
                source=row.get("source") or row.get("Source") or "native_ledger",
                date=row.get("date") or row.get("Date"),
                event_place=row.get("event_place") or row.get("EventPlace") or subject,
                place_name=row.get("place_name"))


def visit_events(catalog):
    """Native location_entered rows (list, discovery:* keys, and current map) to persist."""
    from arti_runtime.experience_facts import events, experiences
    out, seen = [], set()
    rows = list(events(catalog or {})) + list(experiences(catalog or {}))
    for key, value in (catalog or {}).items():
        if str(key).startswith("discovery:") and isinstance(value, Mapping):
            rows.append(value)
    for row in rows:
        got = _visit_row(row)
        if got is None or got["subject"] in seen:
            continue
        seen.add(got["subject"])
        out.append(got)
    current = ((catalog or {}).get("world_graph") or {}).get("current")
    if isinstance(current, str) and current and current not in seen:
        out.append(dict(id="location_entered:" + current, kind="location_entered", subject=current,
                        text=current, source="native_current", event_place=current,
                        date=(catalog or {}).get("date")))
    return out


def arrival_line(mind, location, date, actionable, names=None, *, visits=None, clock=None, display_name=None):
    """One page at each verified map arrival; no goal text matching (ADR 207)."""
    if not location:
        return None
    label = (names or {}).get(location) or display_name or location
    visits = visits if visits is not None else {}
    if visits.get("current") == (location, date):
        return None
    prior = visits.get(location)
    visits["current"] = (location, date)
    visits[location] = date
    when = "lagi hari ini" if prior == date else "pertama hari ini" + ("; terakhir " + prior if prior else "")
    at = " %02d:%02d" % (clock//100, clock%100) if type(clock) is int else ""
    return "%s%s: tiba %s (%s). %s" % (date, at, label, when, actionable or EMPTY)


def visit_days(base, scope):
    """Latest visits across process/day restarts, in the existing scoped item ledger."""
    if base is None or not scope:
        return {}
    import arti_mind
    try:
        return {r["item_id"]: r["facts"]["date"] for r in arti_mind.ledger_rows(base,"stardew",scope=scope,kind="place_visit")
                if isinstance(r.get("facts"), Mapping) and isinstance(r["facts"].get("date"),str)}
    except (OSError, ValueError):
        return {}


def remember_visit(base, scope, location, date, clock):
    if base is None or not scope:
        return
    import arti_mind
    with arti_mind._LEDGER_LOCK:
        _write_visits(base, scope, location, date, clock, visit_records(base, scope).get(location, []))


# ---------------------------------------------------------------- ADR 232 part C: her own dated record of a place
VISITS_KEEP = 6      # visits kept per place in its place_visit row
VISITS_SHOWN = 3     # visits a trip label carries (newest)
HERE_CHARS = 72      # what her page said she could do there, as it said it
CHOSE_CHARS = 40     # what she chose on that arrival page


def visit_key(line):
    """The arrival line without the appended "what can be done here" tail (the fact line trims it, fix123A)."""
    return line.split("). ", 1)[0] + ")." if isinstance(line, str) and "). " in line else line


def visit_records(base, scope, *, strict=False):
    """{location: [{date, time, here, chose}, ...]} from the place_visit rows (oldest first); {} when none."""
    if base is None or not scope:
        return {}
    import arti_mind
    try:
        rows = arti_mind.ledger_rows(base, "stardew", scope=scope, kind="place_visit")
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    out = {}
    for r in rows:
        facts = r.get("facts") if isinstance(r.get("facts"), Mapping) else {}
        visits = [dict(v) for v in native_rows(facts.get("visits")) if isinstance(v, Mapping) and isinstance(v.get("date"), str)]
        if isinstance(r.get("item_id"), str) and visits:
            out[r["item_id"]] = visits
    return out


def record_visit(base, scope, location, date, clock, *, here=None, chose=None):
    """remember_visit plus this visit in the same row: what her arrival page said she could do there and what she
    chose on it. The same row, the same writer; ``date``/``time`` keep their meaning (the latest visit)."""
    if base is None or not scope or not location:
        return None
    import arti_mind
    import time
    visit = dict(date=date, time=clock if type(clock) is int else None,
                 here=str(here)[:HERE_CHARS] if here else None, chose=str(chose)[:CHOSE_CHARS] if chose else None,
                 at=time.time())  # when it was recorded, to tell a visit before a reload
    with arti_mind._LEDGER_LOCK:
        old = visit_records(base, scope).get(location, [])
        visits = (old + [visit])[-VISITS_KEEP:]
        _write_visits(base, scope, location, date, clock, visits)
    return visits


def _write_visits(base, scope, location, date, clock, visits):
    import arti_mind
    from arti_runtime.plan_dates import ordinal
    facts = dict(date=date, time=clock, **({"visits": visits} if visits else {}))
    arti_mind.record_items(base, "stardew", scope=scope, kind="place_visit",
        items=[dict(id=location, status="observed", facts=facts)],
        day=ordinal("stardew", date), day_label=date)


def close_visit(base, scope, location, at, result):
    """Only the open visit identified by this writer may receive this departure."""
    import arti_mind
    from arti_runtime.plan_dates import ordinal
    from arti_runtime.save_reload import before_reload, latest
    with arti_mind._LEDGER_LOCK:
        visits = visit_records(base, scope, strict=True).get(location, [])
        visit = next((row for row in reversed(visits) if row.get("at") == at), None)
        if visit is None:
            return None
        if before_reload(latest(base, scope), day=ordinal("stardew", visit["date"]), at=at):
            return None
        if "result" in visit:
            return visits if visit["result"] == result else None
        visit["result"] = result
        latest_visit = visits[-1]
        _write_visits(base, scope, location, latest_visit["date"], latest_visit.get("time"), visits)
        return visits


def consume_visit(runtime, request, choice):
    """The arrival page she answered is her visit (fact line on: the page's line is the trimmed one). Records it
    with what that page offered there and what she chose, and clears the pending visit. False: not this page."""
    from arti_runtime.place_visit_result import matches_arrival, open_visit

    line = (((getattr(request, "skill_offer", None) or {}).get("day") or {}).get("kunjungan"))
    with runtime._lock:
        pending = getattr(runtime, "_live7_visit_pending", None)
        if (not pending or not pending.get("scope") or pending["scope"] != getattr(runtime, "_live7_visit_scope", None)
                or not isinstance(line, str) or visit_key(line) != visit_key(pending.get("line"))):
            return False
        if (sample := pending.get("sample")) is not None and not matches_arrival(request, sample):
            return False
        runtime._live7_visits = pending["visits"]
        runtime._live7_visit_pending = None
    full = pending.get("line") or ""
    here = full.split("). ", 1)[1] if "). " in full else None
    name, arg = getattr(choice, "name", None), getattr(choice, "arg", None)
    chose = None if not name else name if arg is None else "%s %s" % (name, arg)
    try:
        visits = record_visit(runtime._live7_mind_base(), pending["scope"], pending["location"], pending["date"],
                              pending["clock"], here=here, chose=chose)
    except (OSError, ValueError) as exc:
        runtime._journal(dict(ev="visit_memory_error", error=type(exc).__name__))
        return True
    with runtime._lock:
        if visits is None or pending["scope"] != getattr(runtime, "_live7_visit_scope", None):
            return True
        records = getattr(runtime, "_live7_visit_records", None)
        if isinstance(records, dict) and pending["scope"] == getattr(runtime, "_live7_visit_records_scope", None):
            records[pending["location"]] = visits
        if sample is not None and sample["place"] == pending["location"]:
            open_visit(runtime, pending, visits[-1]["at"])
    return True


WORDS_CHARS = 120   # the game's words shown at a visit, cut at a word with a visible mark


def with_game_words(base, scope, records):
    """Each visit with the game's own words shown to her at that place during it (the newest complete capture in the
    game-text ledger, ADR 219: that place, that day, from her arrival clock until her next visit there)."""
    if base is None or not scope or not records:
        return records
    from arti_runtime.game_text_delivery import _rows, _text
    from arti_runtime.plan_dates import ordinal
    captures = {}
    for row in _rows(base, scope):
        f = row["facts"]
        if (isinstance(f.get("place"), str) and type(f.get("day")) is int and type(f.get("clock")) is int
                and f.get("capture_complete") is True and len(f.get("chunks") or {}) == f.get("parts")):
            text = " ".join(_text(f).split())
            if text:
                captures.setdefault((f["place"], f["day"]), []).append((f["clock"], text))
    out = {}
    for place, visits in records.items():
        rows = []
        for i, v in enumerate(visits):
            v = dict(v)
            day = ordinal("stardew", v["date"])
            later = next((w for w in visits[i + 1:] if w.get("date") == v["date"]), None)
            start, end = v.get("time"), (later or {}).get("time")
            seen = [t for c, t in sorted(captures.get((place, day), ()))
                    if (type(start) is not int or c >= start) and (type(end) is not int or c < end)]
            if seen:
                text = seen[-1]
                v["game"] = text if len(text) <= WORDS_CHARS else text[:WORDS_CHARS].rsplit(" ", 1)[0] + "…"
            rows.append(v)
        out[place] = rows
    return out


def mark_reload(records, reload):
    """ADR 232 part H: visits recorded before a reload and dated on or after the day it went back to are her memory of
    the days before it; they keep their place in her record and say so."""
    if not isinstance(records, Mapping) or not records or not reload:
        return records
    from arti_runtime.plan_dates import ordinal
    from arti_runtime.save_reload import before_reload
    reloads = (reload,) if isinstance(reload, Mapping) else native_rows(reload)
    reloads = [event for event in reloads if isinstance(event, Mapping) and type(event.get("to_day")) is int
               and type(event.get("at")) in (int, float)]
    return {place: [dict(v, before_reload=True) if any(
        before_reload(event, day=ordinal("stardew", v.get("date")), at=v.get("at")) for event in reloads) else v
                    for v in native_rows(visits) if isinstance(v, Mapping)]
            for place, visits in records.items()}


def mark_clock_rewind(records, today, clock, observation_session, marks):
    """Keep earlier same-day results visibly historical after a bound clock rollback."""
    marked = {}
    out = {}
    if not isinstance(records, Mapping):
        return records
    for place, visits in records.items():
        rows = []
        for visit in native_rows(visits):
            if not isinstance(visit, Mapping):
                continue
            result = visit.get("result")
            if not isinstance(result, Mapping):
                rows.append(visit)
                continue
            at = visit.get("at")
            key = (place, at) if type(at) in (int, float) else None
            marker = marks.get(key)
            end = result.get("time")
            if result and type(end) is not int:
                end = visit.get("time")
            if (not marker and visit.get("date") == today and type(clock) is int and type(end) is int
                    and end > clock):
                prior_session = result.get("observation_session")
                marker = ("before_reload" if prior_session and prior_session != observation_session
                          else "before_clock_rewind")
            if marker:
                if key is not None:
                    marked[key] = marker
                visit = dict(visit, **{marker: True})
            rows.append(visit)
        out[place] = rows
    marks.clear()
    marks.update(marked)
    return out


def visit_label(records, location, today):
    """Her own dated record on a trip label: when she arrived there, what her page there said, what she chose.
    ``today``: the game date label her page uses ("Y1 spring 10"). A fact, never advice; None without a record."""
    stored = records.get(location) if isinstance(records, Mapping) else None
    visits = [v for v in native_rows(stored) if isinstance(v, Mapping) and isinstance(v.get("date"), str)]
    if not visits:
        return None
    shown = visits[-VISITS_SHOWN:]
    parts = []
    for v in shown:
        timeline = (" sebelum save dimuat ulang" if v.get("before_reload") else
                    " sebelum jam game mundur" if v.get("before_clock_rewind") else "")
        when = ("hari ini" if v["date"] == today and not timeline else v["date"]) + (
            " %02d:%02d" % divmod(v["time"], 100) if type(v.get("time")) is int else "") + timeline
        bits = [pattern % v[key] for key, pattern in (("here", "halamanmu di sana: %s"),
                ("game", '"%s" (pesan layar game)'), ("chose", "kamu pilih %s"))
                if isinstance(v.get(key), str) and v[key]]
        parts.append(when + (" (" + "; ".join(bits) + ")" if bits else ""))
    more = len(visits) - len(shown)
    return "kunjunganmu ke sana: " + ", ".join(parts) + (" (+%d sebelumnya)" % more if more > 0 else "")


RESULT_FIELD = "hasil_kunjungan"


def result_label(records, location):
    """One dated result from her most recent completed stay, including replayed history."""
    stored = records.get(location) if isinstance(records, Mapping) else None
    visit = next((v for v in reversed(native_rows(stored))
                  if isinstance(v, Mapping) and isinstance(v.get("date"), str)
                  and isinstance(v.get("result"), Mapping) and v["result"].get("end")), None)
    if visit is None:
        return None
    result = visit["result"]
    when = visit["date"] + (" %02d:%02d" % divmod(visit["time"], 100) if type(visit.get("time")) is int else "")
    if visit.get("before_reload"):
        when += " sebelum save dimuat ulang"
    elif visit.get("before_clock_rewind"):
        when += " sebelum jam game mundur"
    bits = []
    for key, lead in (("seen", "tercatat "), ("available", "ditawarkan "), ("effect", ""),
                      ("stalled", ""), ("object", "tercatat "), ("pose", ""), ("menu", "")):
        row = result.get(key)
        if isinstance(row, Mapping) and isinstance(row.get("label"), str):
            bits.append(lead + row["label"])
        if len(bits) == 3:
            break
    left = result.get("left_name") or result.get("left")
    bits.extend(("cakupan belum diketahui", "keluar ke " + left[:28] if isinstance(left, str) and left
                 else "keluar, tujuan belum terbaca"))
    return when + " (" + "; ".join(bits) + ")"
