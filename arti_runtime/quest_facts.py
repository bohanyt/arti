"""Native quest requirements and literal objective time windows, without goal policy (ADR 69)."""
from arti_runtime.native_rows import native_rows
import re

from arti_runtime.plan_dates import label, ordinal


FIELDS = ("description", "objective", "type", "progress", "progress_kind", "progress_note", "reward_g", "reward", "days_left", "target",
          "target_location", "search_location", "item", "item_name", "number", "daily", "cancelable", "deadline_day",
          "observed_day", "observed_time")


def capture(row, catalog):
    facts = {k: row[k] for k in FIELDS if row.get(k) not in (None, "", [])}
    for key, cap in (("description", 180), ("objective", 140), ("reward", 60)):
        if key in facts:
            facts[key] = str(facts[key])[:cap]
    # Basic quests have no structured target/time. Bind literal native objective words only;
    # no quest IDs, inferred expiry dates, opening hours, or action instructions.
    text_source = "objective" if str(row.get("objective") or "").strip(" .") else "description"
    text = str(row.get(text_source) or "")
    if not facts.get("target_location"):
        matches = []
        for place in native_rows((catalog.get("world_graph") or {}).get("places", ())):
            for name in (place.get("id"), place.get("label")):
                if isinstance(place.get("id"), str) and isinstance(name, str) and name and (m := re.search(
                        r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)):
                    matches.append((m.start(), place.get("id")))
        if matches:
            facts["target_location"] = min(matches)[1]
            facts["target_location_source"] = text_source
    m = re.search(r"\bbefore\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.I)
    if m and 1 <= int(m[1]) <= 12 and int(m[2] or 0) < 60:
        facts["time_before"] = (int(m[1]) % 12 + (12 if m[3].lower() == "pm" else 0)) * 100 + int(m[2] or 0)
    if type(facts.get("observed_day")) is not int:
        parts = str(catalog.get("date", "")).split(":")
        if len(parts) == 3:
            facts["observed_day"] = ordinal("stardew", f"Y{parts[0]} {parts[1]} {parts[2]}")
    return facts


def detail(facts, shown=""):
    """Structured detail row: only what the paired detail line ``shown`` does not already carry in full.
    Native defaults (untimed days_left=0, reward_g=0, false flags) say nothing; observed_day only dates a
    deadline. The raw clock sample is left out so clock ticks alone cannot invalidate an offered read handle.
    ``cancelable`` is the journal's own cancel button, seen natively when the log is open (ADR 74).
    A field cut by a line or token bound stays structured, so no requirement, counter or deadline is lost."""
    covered = {k for keys, text, whole in _parts(facts) + _tail(facts) if whole and text in shown for k in keys}
    return {k: v for k, v in facts.items() if k not in ("observed_time", "cancelable", "read_time") and k not in covered
            and not (type(v) in (int, bool) and not v)
            and not (k == "observed_day" and type(facts.get("deadline_day")) is not int)}


def _parts(facts):
    """(fields, text, whole) per rendered piece; requirements first so prose cannot crowd them out.
    ``whole`` is False when a 30-character token bound cut a value."""
    out = []
    due, today = facts.get("deadline_day"), facts.get("observed_day")
    if type(due) is int and due > 0:
        relation = "unknown" if type(today) is not int else "passed" if due < today else "today" if due == today else "future"
        out.append((("deadline_day", "observed_day"), f"due {label('stardew', due)}: {relation}", True))
    before, now = facts.get("time_before"), facts.get("observed_time")
    if type(before) is int:
        relation = "time unknown" if type(now) is not int else "today passed" if now >= before else "today future"
        out.append((("time_before",), f"window before {before // 100:02}:{before % 100:02} ({relation})", True))
    progress = facts.get("progress")
    kind = facts.get("progress_kind") or ""
    if isinstance(progress, list) and len(progress) == 2 and progress[1] is not None:
        have, need = progress
        same = ("number",) if facts.get("number") == need else ()  # native Number is the counter's source
        out.append((("progress", "progress_kind") + same, f"{need} remaining" if kind == "remaining" else
                    f"{kind + ' ' if kind else ''}{'?' if have is None else have}/{need}", True))
    from arti_runtime.fact_names import item_name
    item = item_name(facts.get("item"), name=facts.get("item_name")) if facts.get("item_name") or facts.get("item") else None
    number = facts.get("number")
    if item:
        counted = type(number) is int and number > 0 and not progress
        out.append((("item_name" if facts.get("item_name") else "item",) + (("number",) if counted else ()),
                    str(item)[:30] + (f" x{number}" if counted else ""), len(str(item)) <= 30))
    target, location = facts.get("target"), facts.get("target_location")
    if target or location:
        values = [(k, str(v)) for k, v in (("target", target), ("target_location", location)) if v]
        out.append((tuple(k for k, _ in values), "@".join(v[:30] for _, v in values),
                    all(len(v) <= 30 for _, v in values)))
    if facts.get("search_location"):
        value = str(facts["search_location"])
        out.append((("search_location",), "search area " + value[:30], len(value) <= 30))
    return out


def requirements(facts):
    """Facts first so a long objective cannot crowd out a counter or deadline."""
    return [text for _, text, _ in _parts(facts)]


def _tail(facts):
    out = []
    if facts.get("progress_note"):
        out.append((("progress_note",), " (" + str(facts["progress_note"]) + ")", True))
    if type(facts.get("reward_g")) is int and facts["reward_g"] > 0:
        out.append((("reward_g",), " hadiah %dg" % facts["reward_g"], True))
    if facts.get("reward"):
        out.append((("reward",), " hadiah " + str(facts["reward"]), True))
    if type(facts.get("days_left")) is int and facts["days_left"] > 0 and not facts.get("deadline_day"):
        out.append((("days_left",), ", sisa %d hari" % facts["days_left"], True))
    return out


def indicator_line(catalog):
    value = ((catalog or {}).get("journal_indicator") or {}).get("new_activity")
    return ("ada tanda seru di jurnal" if value is True else "tidak ada tanda seru di jurnal" if value is False
            else "tanda seru di jurnal: belum diketahui")


def line(record, limit):
    facts = record.get("facts") or {}
    title = str(record.get("title") or record.get("item_id") or "quest")
    core = "; ".join(requirements(facts))
    # Budget is unchanged: 80 chars in decisions, 120 in detail/reflection.
    # Fields a line cannot carry in full remain in the progress detail rows (detail()).
    tail = "".join(t for _, t, _ in _tail(facts))
    since = facts.get("since") or record.get("first_label")
    if isinstance(since, str) and since and record.get("status") != "completed":
        tail += ", sejak " + since  # fix33: first-seen date (ADR 45)
    if core:
        title = title[:max(8, limit - len(core) - 2)]
    result = title + (": " + core if core else "")
    text = str(record.get("text") or "").strip()
    if text.strip(" ."):
        # ADR 74: objective prose absorbs the bound, so reward and first-seen date are not cut off by it.
        # Only requirements that alone fill the line may still crowd the tail out (full date on detail lines).
        room = limit - len(result) - len(tail) - 2
        if len(text) <= room:
            result += ("; " if core else ": ") + text
        elif room >= 8:
            result += ("; " if core else ": ") + text[:room - 1].rstrip() + "…"
    return (result + tail)[:limit]


# --------------------------------------------------------------------------- speech side (fix60A, ADR 87)
# a44 T114: after shipping a Parsnip she said she had already helped Jodi, while the native log still held
# "[recorded remark omitted]" active, needing a Cauliflower. The persona only ever saw a quest as a title and a status word,
# and only when her own words named the title. These lines give the speech side the same native requirement
# and status the decision side has (ADR 69), compactly, requirements first, status last, no quest/item IDs.
SPEECH_LINE_MAX = 80
SPEECH_LINES_MAX = 4
QUEST_CHANGES_MAX = 4
STATUS_ID = {"active": "belum selesai", "completed": "selesai (tercatat di log)",
             "not_in_quest_log": "hilang dari log (selesai/batal?)"}
_DUE_ID = {"passed": "lewat", "today": "hari ini", "future": "belum lewat", "unknown": "?"}
CHANGE_STATES = ("completed", "not_in_quest_log")  # a removal is a status change, never completion (ADR 69)


def _speech_pieces(facts):
    """Native requirement pieces in ADR 69 order: deadline, window, counter, item, target, search area."""
    out = []
    due, today = facts.get("deadline_day"), facts.get("observed_day")
    if type(due) is int and due > 0:
        relation = "unknown" if type(today) is not int else "passed" if due < today else "today" if due == today else "future"
        out.append(f"tenggat {label('stardew', due)} ({_DUE_ID[relation]})")
    before = facts.get("time_before")
    if type(before) is int:
        out.append(f"sebelum {before // 100:02}:{before % 100:02}")
    progress, kind = facts.get("progress"), facts.get("progress_kind") or ""
    counted = False
    if isinstance(progress, list) and len(progress) == 2 and progress[1] is not None:
        have, need = progress
        out.append(f"sisa {need}" if kind == "remaining" else f"{'?' if have is None else have}/{need}{' ' + kind if kind else ''}")
        counted = True
    from arti_runtime.fact_names import item_name
    item = item_name(facts.get("item"), name=facts.get("item_name")) if facts.get("item_name") or facts.get("item") else None
    number = facts.get("number")
    target, location = facts.get("target"), facts.get("target_location")
    if item:
        out.append("butuh " + str(item)[:30] + (f" x{number}" if type(number) is int and number > 0 and not counted else "")
                   + (" ke " + str(target)[:30] if target else ""))
    elif target:
        out.append("ke " + str(target)[:30])
    if location and not target:
        out.append("di " + str(location)[:30])
    if facts.get("search_location"):
        out.append("area " + str(facts["search_location"])[:30])
    return out


def speech_line(record, limit=SPEECH_LINE_MAX):
    """``Jodi's Request: butuh Cauliflower x1 ke Jodi, belum selesai``. The status always survives the bound;
    the title gives way first, then the requirement text (cut with ``…``). Objective prose never rides here."""
    facts = record.get("facts") or {}
    title = str(record.get("title") or record.get("item_id") or record.get("id") or "quest").strip()
    status = STATUS_ID.get(record.get("status"), str(record.get("status") or "unknown"))
    core = ", ".join(_speech_pieces(facts))
    tail = ", " + status
    room = limit - len(tail)
    if core:
        title = title[:max(8, room - len(core) - 2)]
        core_room = room - len(title) - 2
        if len(core) > core_room:
            core = core[:max(0, core_room - 1)].rstrip(" ,") + "…"
        return (title + ": " + core + tail)[:limit]
    return (title[:max(8, room)] + tail)[:limit]


def speech_status(records, *, limit=SPEECH_LINE_MAX, max_lines=SPEECH_LINES_MAX):
    """Compact status lines for the persona: active quests first (native order), then completed ones."""
    ordered = [r for r in records if r.get("status") == "active"] + [r for r in records if r.get("status") == "completed"]
    lines = [speech_line(r, limit) for r in ordered[:max_lines]]
    if len(ordered) > max_lines:
        lines.append("+%d quest lagi" % (len(ordered) - max_lines))
    return lines


def status_fact(records, *, observed_ts, date, changes=()):
    """The ``quest_status`` native fact for now.json: a dated memory of the newest native quest log read
    (never a fresh pose read), plus the most recent native status changes with their reception stamps."""
    records = list(records)
    return dict(value=speech_status(records),
                count=dict(active=sum(r.get("status") == "active" for r in records),
                           completed=sum(r.get("status") == "completed" for r in records)),
                changes=[dict(c) for c in list(changes)[-QUEST_CHANGES_MAX:]],
                observed_ts=observed_ts, date=date, source="native_quest_log", relation="memory")


def quest_effect(completed_action, quest_status):
    """What the newest native quest log says about a completed action. An action that no native status change
    followed did not complete a quest, whatever its item or her intent said. Unknown stays unknown."""
    if not isinstance(completed_action, dict) or not isinstance(quest_status, dict):
        return None
    acted, read = completed_action.get("observed_ts"), quest_status.get("observed_ts")
    if type(acted) not in (int, float) or type(read) not in (int, float):
        return None
    if read < acted:
        return "log quest belum dibaca ulang setelah aksi ini; status di quest_status adalah bacaan sebelumnya"
    done = [c.get("title") or c.get("item_id") or "?" for c in quest_status.get("changes") or ()
            if isinstance(c, dict) and c.get("status") in CHANGE_STATES
            and type(c.get("observed_ts")) in (int, float) and c["observed_ts"] >= acted]
    if done:
        return "quest yang tercatat berubah status setelah aksi ini: " + ", ".join(done[:QUEST_CHANGES_MAX])
    return ("tidak ada quest yang tercatat selesai setelah aksi ini (log quest dibaca ulang); "
            "quest aktif di quest_status tetap belum selesai, aksi ini bukan bantuan ke NPC mana pun")
