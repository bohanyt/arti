"""Native measurements beside authored targets, without changing intent (ADR 53)."""
import re


def _name(value):
    return re.escape(str(value)).replace(r"\ ", r"\s+")


def measurements(snapshot=None, *, date, history=(), wallet=None, inventory=None, quests=(), places=()):
    """Project existing scoped receipts. Missing measurements stay unknown, never zero."""
    snapshot = snapshot or {}
    out = []
    def add(kind, name, value, aliases=(), samples=(), **extra):
        out.append(dict(kind=kind, name=name, value=value, aliases=list(aliases), date=date,
                        samples=list(samples), **extra))
    for skill in ("farming", "fishing", "foraging", "mining", "combat", "luck"):
        samples = []
        seen_levels = set()
        for row in history:
            for phase in ("start", "latest"):
                value = ((row.get(phase) or {}).get("skills") or {}).get(skill)
                if type(value) is int and value not in seen_levels:
                    samples.append([value, row["day_label"]])
                    seen_levels.add(value)
        add("skill", skill, (snapshot.get("skills") or {}).get(skill), samples=samples)
    add("money", "wallet", wallet)
    add("money", "earned", snapshot.get("money_earned_self"))
    add("harvest", "harvested", (snapshot.get("crops") or {}).get("harvested"))
    if inventory is not None:
        # Native labels/IDs only: no crop-specific synonyms or guessed item names.
        counts, labels, slots = {}, {}, {}
        for row in inventory:
            qid, n = row.get("qualified_item_id"), row.get("stack")
            if isinstance(qid, str) and type(n) is int:
                counts[qid] = counts.get(qid, 0) + n
                labels[qid] = list(dict.fromkeys(row[k] for k in ("display_name", "name", "internal_name") if row.get(k)))
                if type(row.get("slot")) is int:
                    slots.setdefault(qid, []).append(row["slot"])
        for qid, n in counts.items():
            add("item", qid, n, labels[qid], **({"slots": slots[qid][:3]} if qid in slots else {}))
    for q in quests:
        # fix60A (ADR 87): the native target (the quest giver) is an alias too, so "bantu Jodi" binds Jodi's Request,
        # and the speech line carries the requirement and status beside her words. No quest/NPC-specific rule.
        facts = q.get("facts") if isinstance(q.get("facts"), dict) else {}
        aliases = [n for n in (q.get("title"), facts.get("target")) if isinstance(n, str) and n.strip()]
        from arti_runtime.quest_facts import speech_line
        add("quest", str(q.get("item_id") or q.get("id")), q.get("status"), aliases,
            **({"line": speech_line(dict(q, facts=facts))} if facts or q.get("title") else {}))
    for place in places:
        if place.get("location"):
            add("place", place["location"], place.get("status", "visited"), place.get("aliases", ()),
                samples=[[1, place.get("date")]])
    return out


def crop_facts(texts, own, seen, date, *, seen_date=None, where="Farm", season=None):
    """ADR 99 (a46: "[recorded remark omitted]" rode days after that crop left the farm; the only status beside it
    was the place): what is known of a crop her words name. ``own`` = her crop ledger's rows, ``seen`` = crop item ids read
    in the scene of ``where`` on ``seen_date``, or None when it was not read today. A plan line is never rewritten or closed."""
    from arti_stardew_seeds import CROP_DATA, crop_seasons
    out = []
    for qid, row in CROP_DATA.items():
        pattern = r"(?<!\w)" + _name(row[0]) + r"(?!\w)"
        if not any(isinstance(t, str) and re.search(pattern, t, re.I) for t in texts):
            continue
        mine = sum(r.get("item") == qid and r.get("state", "growing") in ("growing", "ready") for r in own)
        dead = sum(r.get("item") == qid and r.get("state") == "dead" for r in own)  # ADR 113: her words beside dead rows
        parts = [("%d hidup, %d mati di catatan tanamanmu" % (mine, dead)) if dead
                 else "%d di catatan tanamanmu" % mine if mine else "tidak ada di catatan tanamanmu"]
        seasons = crop_seasons(qid)
        if mine and season and seasons and str(season).casefold() not in seasons:  # d25: the ledger had not seen the turn yet
            parts.append("%s bukan musim %s" % (str(season).casefold(), row[0]))
        if seen is None:
            parts.append("%s belum terlihat hari ini" % where)
        else:
            n = sum(item == qid for item in seen)
            parts.append("%s %d di %s (%s)" % ("terlihat", n, where, seen_date) if n
                         else "tidak terlihat di %s (%s)" % (where, seen_date))
        out.append(dict(kind="crop", name=qid, value=mine, aliases=[row[0]], date=date, line="%s: %s" % (row[0], ", ".join(parts))))
    return out


def _target(text, fact):
    names = [fact["name"], *fact.get("aliases", ())]
    name = "(?<!\\w)(?:" + "|".join(_name(n) for n in names) + ")(?!\\w)"
    kind = fact["kind"]
    if kind == "skill":
        patterns = [rf"{name}\s*(?:level|lvl|lv\.?|tingkat)?\s*(\d+)\b",
                    rf"(?:level|lvl|lv\.?|tingkat)\s*(\d+)\s*{name}\b"]
    elif kind == "money":
        earned = bool(re.search(r"\b(?:earn\w*|income|penghasilan|hasilkan|menghasilkan|dapatkan|pendapatan)\b", text, re.I))
        if earned != (fact["name"] == "earned"):
            return None
        patterns = [r"\b(\d[\d,._]*)\s*(?:g\b|gold\b|emas\b)",
                    r"\b(?:uang|money|gold|emas)\s*(\d[\d,._]*)\b"]
    elif kind == "harvest":
        patterns = [r"\b(?:harvest\w*|panen)\s*(\d+)\b", r"\b(\d+)\s*(?:kali\s*)?(?:harvest\w*|panen)\b"]
    elif kind == "item":
        patterns = [rf"(?<!\w){name}\s*(?:x|sebanyak|count)?\s*(\d+)\b", rf"\b(\d+)\s*(?:buah\s+)?{name}(?!\w)"]
    elif kind == "quest":
        # Numeric IDs must be explicitly quest IDs; never match an unrelated level/count.
        return True if (re.search(rf"\b(?:quest|misi)\s*(?:id\s*|#)?{_name(fact['name'])}(?!\w)", text, re.I)
                        or any(re.search(rf"(?<!\w){_name(n)}(?!\w)", text, re.I) for n in fact.get("aliases", ()))) else None
    elif kind == "place":
        return True if re.search(rf"(?<!\w){name}(?!\w)", text, re.I) else None
    elif kind == 'sleep':
        # Only the authored condition supported by this receipt; no invented goal.
        return True if (re.search(r'\btidur\b', text, re.I) and re.search(r'\bkasur\b', text, re.I)
                        and re.search(r'pingsan|jam\s*2\b|02[.:]00', text, re.I)) else None
    else:
        return None
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(re.sub(r"[,._]", "", match[1]))
    return None


def selected(texts, facts):
    texts = [t for t in texts if isinstance(t, str)]
    chosen = [f for f in facts if any(_target(t, f) is not None for t in texts)]
    for text in texts:
        for kind, pattern in (("quest", r"\b(?:quest|misi)\s*(?:id\s*|#)?(\d+)\b"),
                              ("item", r"(\([A-Za-z]+\)[A-Za-z0-9_]+)")):
            for name in re.findall(pattern, text, re.I):
                if not any(f["kind"] == kind and f["name"] == name for f in chosen):
                    chosen.append(dict(kind=kind, name=name, value=None, aliases=[], date="unknown"))
    return chosen


def status(text, facts):
    """Factual annotations only; reaching a threshold never completes its goal."""
    out = []
    for fact in selected([text], facts):
        target = _target(text, fact)
        if target is None:
            continue
        value, kind, name = fact.get("value"), fact["kind"], fact["name"]
        label = (name + " level") if kind == "skill" else (kind + " " + name)
        if kind == "item":
            from arti_runtime.fact_names import item_name
            label = "item " + item_name(name, name=next(iter(fact.get("aliases") or ()), None))
        if value is None:
            line = f"{label}: unknown"
        elif kind == "quest" and isinstance(fact.get("line"), str):
            line = f"{fact['line']} (observed {fact['date']})"  # fix60A: requirement + native status, not a status word
        elif kind == 'sleep':
            line = f"Semalam tidur di kasur terverifikasi (bukti {fact['date']})"
        elif kind in ("quest", "place"):
            line = f"{label}: {value} (observed {fact['date']})"
        else:
            reached = value >= target
            since = next((date for n, date in fact.get("samples", ()) if n >= target), None)
            line = f"{label} {value}; target {target}: " + ("reached" if reached else "not reached")
            line += f" (observed reached {since})" if reached and since else f" (observed {fact['date']})"
        if line not in out:
            out.append(line)
    for fact in facts:
        if (line := owned(text, fact)) and line not in out and _target(text, fact) is None:
            out.append(line)
        if (line := ground(text, fact)) and line not in out:
            out.append(line)
    return out


def met_status(text, facts):
    """Observed completion beside her still-authored goal, without rewriting her file."""
    matched = [(f, _target(text, f)) for f in selected([text], facts)]
    if not matched:
        return None
    for f, target in matched:
        value, kind = f.get('value'), f['kind']
        if kind == 'sleep':
            reached = value in ('slept_before_0200', 'slept_verified')
        elif kind == 'quest':
            reached = value in ('complete', 'completed', 'done')
        elif kind == 'place':
            reached = value == 'visited'
        else:
            reached = type(value) in (int, float) and type(target) is int and value >= target
        if not reached:
            return None
    return 'terpenuhi (bukti native %s)' % ', '.join(dict.fromkeys(str(f['date']) for f, _ in matched))


def sleep_measurement(night, date):
    """The carried native saved-night receipt, including legacy receipts without a bed clock.

    The legacy time_of_day is a wallet timestamp, NEVER the time she got into bed.
    A lost/collapsed night, a different morning, or a known post-02:00 bed cannot meet this goal.
    """
    from arti_stardew_live7 import mind_label, _clock
    if tuple(night.get('next_date') or ()) != tuple(date) or night.get('kind') != 'slept':
        return None
    observed = night.get('observed') or {}
    at = observed.get('bed_time')
    if type(at) is int and at >= 2600:
        return None
    from arti_runtime.sleep_truth import bed_evidence
    timed = bed_evidence(observed) and at < 2600
    saved = night.get('outcome') == 'verified' and night.get('native_save_confirmed') is True
    if not (timed or saved):
        return None
    return dict(kind='sleep', name='own_bed', value='slept_before_0200' if timed else 'slept_verified', aliases=[],
                date=mind_label(date) + (' (semalam %s)' % _clock(at) if timed else
                                         ' (semalam tidur terverifikasi; waktu kasur tidak tersimpan)'))


def ground(text, fact):
    """fix59A (ADR 86): a named item she left on the ground (where, since when, still there) beside her words."""
    if fact["kind"] not in ("ground", "crop", "letter") or not fact.get("aliases") or not isinstance(fact.get("line"), str):
        return None
    if not re.search(r"(?<!\w)(?:" + "|".join(_name(n) for n in fact["aliases"]) + r")(?!\w)", text, re.I):
        return None
    return fact["line"]


def owned(text, fact):
    """fix55B (a40 T35, D5:J26): a named item already in her bag, without a count to measure. The native location
    rides beside her words ("[recorded remark omitted]" while it sits in slot 7); a plan line is never rewritten or closed."""
    if fact["kind"] != "item" or type(fact.get("value")) is not int or fact["value"] < 1 or not fact.get("aliases"):
        return None
    if not re.search("(?<!\w)(?:" + "|".join(_name(n) for n in fact["aliases"]) + ")(?!\w)", text, re.I):
        return None
    slots = fact.get("slots") or ()
    where = "slot " + ",".join(str(s) for s in slots) if slots else "tas"
    return f"{fact['aliases'][0]}: sudah di tas ({where}) x{fact['value']} (observed {fact['date']})"


def annotate(text, facts):
    lines = status(text, facts)
    return text + (" [native: " + "; ".join(lines) + "]" if lines else "")


def intent_views(row, views, *, now):
    fact = ((row or {}).get("native_facts") or {}).get("goal_targets") or {}
    stamp = fact.get("observed_ts")
    facts = fact.get("value", ()) if type(stamp) in (int, float) and 0 <= now - stamp <= 45 else ()
    facts = [f for f in facts if 0 <= now - f.get("observed_ts", stamp) <= 45]
    return dict(views, why=annotate(views["why"], facts), goals=[annotate(g, facts) for g in views["goals"]],
                **({"goals_earlier_days": [annotate(g, facts) for g in views["goals_earlier_days"]]}
                   if views.get("goals_earlier_days") else {}))
