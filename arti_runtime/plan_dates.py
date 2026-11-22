"""Date facts for authored plans and native calendar references. No goal policy."""
import re

SEASONS = ("spring", "summer", "fall", "winter")
RELATIVE_DATES = ((r"\b(?:besok|(?<!after )tomorrow)\b", 1), (r"\b(?:lusa|day after tomorrow)\b", 2),
                  (r"\b(?:hari ini|today)\b", 0), (r"\b(?:kemarin|yesterday)\b", -1),
                  (r"\b(?:minggu depan|next week)\b", 7), (r"\b(?:minggu lalu|last week)\b", -7))


def relative_dates(text, authored_day):
    if type(authored_day) is not int:
        return []
    return [(m.group(), authored_day + offset) for pattern, offset in RELATIVE_DATES
            for m in re.finditer(pattern, text, re.I) if authored_day + offset > 0]


def annotate(text, row, *, game, day):
    """Append resolved facts to her exact words; never rewrite stored intent (ADR 52)."""
    relative = relative_dates(text, row.get("authored_on_day"))
    parts = []
    covered = set()
    def resolved(ref):
        relation = "today unknown" if type(day) is not int else "passed" if ref < day else "today" if ref == day else "future"
        return label(game, ref) + ": " + relation
    for word, ref in relative:
        parts.append(word + " = " + resolved(ref))
        covered.add(ref)
    event_days = row.get("event_days") or []
    if event_days:
        # fix63B (a45 T113 "[recorded words omitted]" on the festival's own day): her word and the native date
        # disagree. Her words stay as written; the difference is stated without claiming which word is about the
        # event (the event name is not available here), the calendar being the native fact (ADR 95).
        if relative and not any(ref in event_days for _, ref in relative):
            parts.append("tanggal kata waktu tidak sama dengan tanggal native acara")
        parts.append("native event = " + "; ".join(resolved(ref) for ref in event_days))
        covered.update(event_days)
    other = [d for d in row.get("referenced_days", ()) if d not in covered]
    if other:
        dates = "; ".join(resolved(ref) for ref in other if type(ref) is int and ref > 0)
        parts.append(("stored plan dates (text binding unknown) = " if row.get("binding_unknown") else "") + dates)
    # A legacy row can have dates but no recoverable authorship. Keep its bound
    # native dates beside the words rather than resolving them against today.
    if not parts and any(re.search(pattern, text, re.I) for pattern, _ in RELATIVE_DATES):
        parts.append("date unknown; authorship unknown")
    return text + (" [" + "; ".join(dict.fromkeys(parts)) + "]" if parts else "")


UPCOMING_DAYS = 7  # AUDIT_UNDERSTANDING1 (ADR 93): a birthday or other dated event within a week is a dated fact too


def _event_days(e, game):
    return [d for date in e.get("dates", ()) if (d := ordinal(game, date)) is not None]


def calendar_context(calendar, *, game, day):
    """Current-season festival facts, including passed ones, plus every other native dated event (birthdays) due
    today or within UPCOMING_DAYS; never suggest an activity."""
    if game != "stardew" or type(day) is not int:
        return []
    rows = []
    for e in calendar:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not isinstance(e.get("name"), str):
            continue
        days = _event_days(e, game)
        if e.get("kind") == "festival":
            keep = any((d - 1) // 28 == (day - 1) // 28 for d in days)
        else:
            keep = any(0 <= d - day <= UPCOMING_DAYS for d in days)
        if keep:
            row = dict(id=e["id"], name=e["name"], date_fact=fact(dict(referenced_days=days), game=game, day=day))
            if e.get("kind") == "festival" and day in days:
                row["kind"] = "festival"  # ADR 110: only on its day, the fact that names the Temp map
            rows.append(row)
    return rows


def upcoming_line(calendar, *, game, day, within=UPCOMING_DAYS, limit=160):
    """One short line of native dated events today or within ``within`` days, nearest first, or None."""
    if game != "stardew" or type(day) is not int:
        return None
    # ADR 93: one entry per event (its first day in the window); the cut falls between items, never inside one.
    first = {}
    for e in calendar:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            for d in _event_days(e, game):
                if 0 <= d - day <= within:
                    first[e["name"]] = min(d, first.get(e["name"], d))
    found = sorted((d - day, name, label(game, d)) for name, d in first.items())
    if not found:
        return None
    parts = ["%s: hari ini" % name if left == 0 else "%s: %d hari lagi (%s)" % (name, left, when)
             for left, name, when in found]
    for kept in range(len(parts), 0, -1):
        text = "; ".join(parts[:kept]) + ("; +%d lagi" % (len(parts) - kept) if kept < len(parts) else "")
        if len(text) <= limit:
            return text
    return "+%d lagi" % len(parts)


def ordinal(game, label):
    if game != "stardew" or not isinstance(label, str):
        return None
    match = re.fullmatch(r"Y(\d+) (spring|summer|fall|winter) (\d+)", label, re.I)
    if match:
        year, season, day = match.groups()
        if int(year) >= 1 and 1 <= int(day) <= 28:
            return (int(year) - 1) * 112 + SEASONS.index(season.lower()) * 28 + int(day)
    return None


def label(game, day):
    if game == "stardew" and type(day) is int and day > 0:
        year, within = divmod(day - 1, 112)
        season, date = divmod(within, 28)
        return f"Y{year + 1} {SEASONS[season]} {date + 1}"
    return f"game day {day}"


def event_references(text, *, game, authored_day, calendar=(), named_only=False):
    """Bind named events to the native year, including dates already passed this year."""
    if game != "stardew" or type(authored_day) is not int:
        return []
    words = lambda s: " ".join(re.findall(r"\w+", s.casefold()))
    source = " " + words(text) + " "
    days = []
    for event in calendar:
        if not isinstance(event, dict):
            continue
        names = [event.get("name", "")] + list(event.get("aliases") or ())
        if any(isinstance(n, str) and words(n) and " " + words(n) + " " in source for n in names):
            for date in event.get("dates") or ():
                day = ordinal(game, date)
                if day is not None and (day - 1) // 112 == (authored_day - 1) // 112:
                    days.append(day)
    if not days and not named_only and re.search(r"\bfestival\w*\b", text, re.I):
        # FIX73D (a54 T80 "[recorded words omitted]" written on the Egg Festival's own day): a bare "festival" is the native
        # festival on, or one day either side of, the day her words point at (relative word, else the authored day),
        # so the calendar date stands beside her word. Two candidates or none: nothing is guessed.
        referenced = [d for _, d in relative_dates(text, authored_day)] or [authored_day]
        near = {ordinal(game, date) for event in calendar if isinstance(event, dict) and event.get("kind") == "festival"
                and not event.get("_receipt_only")
                for date in event.get("dates") or ()}
        near = {d for d in near if d is not None and any(abs(d - r) <= 1 for r in referenced)}
        if len(near) == 1:
            days.extend(near)
    return sorted(set(days))


def linked_days(links, target, *, game, calendar):
    refs = {l.get("ref") for l in links if isinstance(l, dict) and l.get("id") == target}
    return sorted({d for e in calendar if e.get("id") in refs for date in e.get("dates", ())
                   if (d := ordinal(game, date)) is not None})


def references(text, *, game, authored_day, calendar=()):
    """Explicit/relative dates plus dates of named events from the native calendar."""
    if type(authored_day) is not int:
        return []
    days = []
    if game == "stardew":
        for m in re.finditer(r"\b(?:Y(\d+)\s+)?(spring|summer|fall|winter)\s+(\d+)\b", text, re.I):
            year, season, date = m.groups()
            year = int(year) if year else (authored_day - 1) // 112 + 1
            day = ordinal(game, f"Y{year} {season.lower()} {date}")
            if day is not None:
                days.append(day)
    days.extend(d for _, d in relative_dates(text, authored_day))
    return sorted(set(days + event_references(text, game=game, authored_day=authored_day, calendar=calendar)))


def ground(row, *, game, authored_day, calendar=()):
    """Copy with immutable source/reference days. Existing references survive all carries."""
    row = dict(row)
    row.setdefault("authored_on_day", authored_day)
    text = " ".join((row.get("text", ""), row.get("done_when", "")))
    named = event_references(text, game=game, authored_day=row["authored_on_day"], calendar=calendar, named_only=True)
    if named:
        # Model calendar links are claims. A named native event corrects older
        # conflicting links; a relative word remains a separate date fact.
        row["event_days"] = named
        row["calendar_refs"] = [e["id"] for e in calendar if e.get("id") and any(
            ordinal(game, d) in named for d in e.get("dates", ()))]
    row.setdefault("referenced_days", references(text, game=game, authored_day=row["authored_on_day"]))
    # Legacy event-only goals have an explicitly empty reference list. Enrich it;
    # never re-resolve relative words against a later carried day.
    row["referenced_days"] = sorted(set(row["referenced_days"] + event_references(
        text, game=game, authored_day=row["authored_on_day"], calendar=calendar)))
    row["event_days"] = sorted(set(list(row.get("event_days") or []) + event_references(
        text, game=game, authored_day=row["authored_on_day"], calendar=calendar) + [
        d for e in calendar if e.get("id") in (row.get("calendar_refs") or [])
        for date in e.get("dates", ()) if (d := ordinal(game, date)) is not None
        and type(row["authored_on_day"]) is int and (d - 1) // 112 == (row["authored_on_day"] - 1) // 112]))
    return row


def intent_views(row, *, day):
    """Render persisted intent against the latest clock, without mutating it."""
    game = row.get("game")
    dated = list(row.get("goal_dates") or [])
    why = annotate(row.get("why") or "", dict(authored_on_day=row.get("intent_authored_on_day"),
        referenced_days=row.get("why_dates") or [], event_days=row.get("why_event_days") or []), game=game, day=day)
    goals, earlier = [], []
    for text in row.get("goal_today") or []:
        if not isinstance(text, str) or not text.strip():
            continue
        bound = next((g for g in dated if g.get("text") == text), None)
        if bound is None:
            # Old now.json omitted text/authorship. Its existing bound dates are
            # still evidence; a later decision day must not re-date the words.
            refs = sorted({d for g in dated for d in g.get("referenced_days", ())})
            bound = dict(referenced_days=refs, binding_unknown=bool(refs))
        view = annotate(text, bound, game=game, day=day)
        # FIX73D (a54 T117 "[recorded words omitted]" at 06:00 the next day): a goal written for an earlier day stays
        # her exact words, but is never presented as today's. now.json carries that day only from this change on.
        for_day = bound.get("for_day")
        if type(for_day) is int and type(day) is int and for_day < day:
            earlier.append(view + " [tujuan untuk %s, bukan hari ini %s]" % (label(game, for_day), label(game, day)))
        else:
            goals.append(view)
    return dict(why=why, goals=goals, **({"goals_earlier_days": earlier} if earlier else {}))


_RELATION = re.compile(r"(Y\d+ (?:spring|summer|fall|winter) \d+); today Y\d+ (?:spring|summer|fall|winter) \d+: (?:passed|today|future)")


def rebase(value, *, game, day):
    """Recompute every stored `<date>; today <date>: <relation>` against the delivered clock (fix63B, ADR 95).

    now.json holds these strings from the last decision, so a wake on the next day read "today" for yesterday's event
    (a45 T115, delivered Spring 14: "[recorded remark omitted]"). Dates are immutable; only their
    relation depends on when they are read. An unknown clock says "today unknown" rather than the frozen relation."""
    if isinstance(value, str):
        return _RELATION.sub(lambda m: fact(dict(referenced_days=[ordinal(game, m[1])]), game=game, day=day), value)
    if isinstance(value, list):
        return [rebase(v, game=game, day=day) for v in value]
    if isinstance(value, dict):
        return {k: rebase(v, game=game, day=day) for k, v in value.items()}
    return value


def fact(row, *, game, day):
    refs = row.get("referenced_days") or []
    if type(day) is not int:
        return "; ".join(f"{label(game, d)}; today unknown" for d in refs if type(d) is int and d > 0)
    return "; ".join(f"{label(game, d)}; today {label(game, day)}: "
                     + ("passed" if d < day else "today" if d == day else "future")
                     for d in refs if type(d) is int and d > 0)
