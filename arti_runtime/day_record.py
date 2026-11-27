"""Dated native outcomes in the existing scoped experience ledger."""
from collections.abc import Mapping

KIND = "own_action"
MAX_ROWS = 10


def record(base, scope, date, clock, kind, text, *, place=None, witness=None, event_label=None):
    import arti_mind
    from arti_runtime.native_story import day_of
    from arti_runtime.plan_dates import label

    day = day_of(":".join(map(str, date))) if isinstance(date, (list, tuple)) and len(date) == 3 else None
    if (not scope or day is None or type(clock) is not int or not 0 <= clock <= 2600 or clock % 100 >= 60
            or not isinstance(text, str) or not text.strip() or "\ufffd" in text):
        return []
    identity = f"{day}:{clock}:{kind}:{witness or text}"
    facts = dict(clock=clock, place=place, action=kind)
    if kind == "event_entered" and isinstance(event_label, str) and event_label and "\ufffd" not in event_label:
        facts["event_label"] = " ".join(event_label.split())[:240]
    return arti_mind.record_items(base, "stardew", scope=scope, kind=KIND, day=day,
        day_label=label("stardew", day), items=[dict(id=identity, status="observed", text=text,
            facts=facts)])


def live(runtime, date, clock, kind, text, *, place=None, witness=None, scope=None, event_label=None):
    if (getattr(runtime, "_config", None) or {}).get("voice_game_memory_enabled") is not True:
        return
    base = runtime._live7_mind_base()
    scope = scope or getattr(runtime, "_live7_discovery_scope", None)
    if base is None or not scope:
        return
    try:
        changed = record(base, scope, date, clock, kind, text, place=place, witness=witness, event_label=event_label)
        if changed:
            facts = changed[0]["facts"]
            runtime._journal(dict(ev="own_action_recorded", action=kind, date=list(date), clock=clock, place=place,
                **({"event_label": facts["event_label"]} if "event_label" in facts else {})))
            from arti_runtime.experience_digest import live7_write
            if (session := getattr(runtime, "_skill_session", None)) is not None:
                live7_write(runtime, session)
    except (OSError, ValueError, TypeError) as exc:
        runtime._journal(dict(ev="own_action_record_error", error=type(exc).__name__))


def place_word(place, catalog=None):
    from arti_runtime.experience_digest import place_names
    return place_names([place], catalog).get(place) or "tempat itu"


def event_words(catalog, event_id, day, clock, opening=None):
    """Name an arrival with the same visible evidence as the event page."""
    from arti_runtime.fact_line import Witnesses, event_display
    from arti_runtime.native_notices import today_notices, words

    witnesses = Witnesses(catalog)
    display = event_display(witnesses, event_id, day, through=clock)
    if display and display["name"]:
        return display["name"], f'Aku masuk ke {display["name"]} saat game memulai acaranya.'
    observed = (display or {}).get("text") or (opening or {}).get("text")
    if not observed and type(clock) is int:
        notice = next((row for row in today_notices(witnesses, day)
                       if type(row.get("clock")) is int and row["clock"] <= clock), None)
        observed = witnesses.text(notice) if notice else None
    basis = f' sesudah game menampilkan "{words(observed)[:160]}"' if observed else " saat game memulai acara"
    return "Game membawaku ke tempat lain" + basis, "Aku tiba di tempat lain" + basis + "."


def knockout_text(row, catalog=None):
    before, after = row["before"], row["after"]
    return f"Aku pingsan di {place_word(before.get('location'), catalog)} lalu sadar di {place_word(after.get('location'), catalog)}."


def bag_text(result, names=None):
    verb = {"simpan": "Kusimpan", "ambil": "Kuambil dari peti", "kirim": "Kukirim", "jual": "Kujual"}.get(result.get("verb"))
    parts = []
    if verb:
        for row in result.get("items", ()) or ():
            if not isinstance(row, Mapping) or row.get("state") not in ("done", "partial"):
                continue
            item, count = row.get("item"), row.get("moved")
            name = row.get("name") or (names or {}).get(item)
            if type(count) is int and count > 0 and isinstance(name, str) and name and name != item:
                parts.append(f"{count} {name}")
    return f"{verb} {', '.join(parts[:5])}." if parts else None


def skill(runtime, row, date, clock, place, *, catalog=None, witness=None):
    if row.get("verified") is not True:
        return
    name = row.get("skill")
    evidence = row.get("evidence") or {}
    observed = evidence.get("observed") or {}
    if name == "pergi_ke" and place:
        label = place_word(place, catalog)
        if evidence.get("left_native_event"):
            name, text = "event_left", f"Aku keluar dari acara dan sampai di {label}."
        else:
            text = f"Aku sampai di {label}."
    elif name == "ngobrol_hadiah" and isinstance(observed.get("speaker"), str) and observed["speaker"]:
        text = f'Aku berbicara dengan {observed["speaker"]}.'
    elif name == "tidur":
        clock = observed.get("bed_time")
        text = "Aku tidur di tempat tidurku dan bangun esok pagi."
    elif name == "aksi":
        from arti_stardew_generic_world import unreadable_press
        if not unreadable_press((evidence.get("objective") or {}).get("item"), evidence.get("native_ui_episode")):
            return
        name, text = "menu_opened", "Aku menekan sesuatu. Menu dengan simbol yang tidak bisa kubaca terbuka."
        witness = evidence["native_ui_episode"]["opened"].get("request_id")
    elif name in ("simpan_ke_chest", "kirim", "jual", "ambil_dari_chest"):
        result = observed.get("bag_sitting")
        if not isinstance(result, Mapping):
            verb = {"simpan_ke_chest": "simpan", "kirim": "kirim", "jual": "jual", "ambil_dari_chest": "ambil"}[name]
            moves = observed.get("actual_stored") or observed.get("actual_shipped") or ()
            result = dict(verb=verb, items=[dict(r, moved=r.get("quantity"), state="done")
                for r in moves if isinstance(r, Mapping)])
        text = bag_text(result, evidence.get("names"))
    else:
        return
    if text:
        live(runtime, date, clock, name, text, place=place, witness=witness)


def project(base, scope, day):
    import arti_mind

    rows = arti_mind.ledger_rows(base, "stardew", scope=scope, kind=KIND)
    dated = [r for r in rows if type(r.get("first_day")) is int and day - 7 <= r["first_day"] <= day
             and isinstance(r.get("text"), str) and type((r.get("facts") or {}).get("clock")) is int]
    dated.sort(key=lambda r: (r["first_day"], r["facts"]["clock"]))
    # An encountered event remains available for the week even on a busy day.
    event_day = next((r["first_day"] for r in reversed(dated) if r["facts"].get("action") == "event_entered"), None)
    event_rows = [r for r in dated if r["first_day"] == event_day
                  and r["facts"].get("action") in ("event_entered", "event_left", "walk_refused")][-5:]
    rows = dated[-(MAX_ROWS - len(event_rows)):]
    latest = sorted({r["id"]: r for r in event_rows + rows}.values(),
                    key=lambda r: (r["first_day"], r["facts"]["clock"]))
    visits = {}
    for row in dated:
        facts = row["facts"]
        if label := facts.get("event_label"):
            day_number = row["first_day"]
            visits.setdefault((day_number, facts.get("place"), label), dict(place=facts.get("place"),
                first_day=day_number, clock=facts["clock"], label=label, event=True))
    lines = [f"{r['first_label']} %02d:%02d {r['text']}" % divmod(r['facts']['clock'], 100) for r in latest]
    return lines, list(visits.values())[-3:]
