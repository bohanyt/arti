"""Dated native story evidence in the existing mind ledger (ADR 63). No story lookup/policy."""
from collections.abc import Mapping
import re

import arti_mind as M
import arti_state
from arti_runtime import jsonl_snapshot
from arti_runtime.plan_dates import RELATIVE_DATES, ordinal, event_references, fact, label
from arti_stardew_screen_text import game_text

TTL_DAYS = 7
EVENTS_MAX = 48
LINES_MAX = 64
CHANGES_MAX = 32
CONTEXT_MAX = 3
_LOCK = M._LEDGER_LOCK


def day_of(date):
    parts = date.split(":") if isinstance(date, str) else []
    try:
        return ordinal("stardew", f"Y{int(parts[0])} {parts[1]} {int(parts[2])}") if len(parts) == 3 else None
    except (ValueError, TypeError):
        return None


def record_scope(record):
    """The same accepted actor/save binding for both ingestion and memory presentation."""
    if (record is None or not record.authoritative or record.stale or record.requires_resync
            or not record.available):
        return None
    facts = record.facts
    native = facts.get("native") or {}
    ring = native.get("occurrences") if isinstance(native, Mapping) else None
    if (not isinstance(ring, Mapping) or ring.get("player_id") != record.player_id or not ring.get("save_id")
            or facts.get("player_id") != record.player_id or facts.get("screen_id") != record.screen_id):
        return None
    return f"{ring['save_id']}:{record.player_id}"


def remember(base, record, *, mind_scoped=False):
    """Accepted actor-bound ui.context only; consume native occurrence identities once, across restarts."""
    scope = record_scope(record)
    if scope is None:
        return []
    facts = record.facts
    native_date = (facts.get("day_transition") or {}).get("date")
    current_day = day_of(":".join(str(v) for v in native_date)) if isinstance(native_date, (list, tuple)) else None
    if current_day is None:
        return []
    ring = facts["native"]["occurrences"]
    changed = []
    kept = []
    with M._JOURNEY_LOCK, _LOCK:  # journey before ledger, the order game_text_delivery uses
        known = {r["item_id"]: r for r in M.ledger_rows(base, "stardew", scope=scope, kind="native_story")}
        for row in (ring.get("tail") or ())[-16:]:
            if not isinstance(row, Mapping):
                continue
            f = row.get("facts") if isinstance(row, Mapping) else None
            if row.get("kind") != "native_story" or not isinstance(f, Mapping):
                continue
            day = day_of(f.get("date"))
            if (day is None or not 0 <= current_day - day < TTL_DAYS
                    or not isinstance(row.get("runtime_id"), str) or type(row.get("occ_seq")) is not int):
                continue
            identity = row["runtime_id"] + ":" + game_text(row.get("subject"), 160)
            old = known.get(identity, {})
            detail = dict(old.get("facts") or {})
            # The ring is replayed on every observation; its sequence is monotonic across process restarts.
            if row["occ_seq"] <= detail.get("last_seq", 0):
                continue
            detail.update(event_id=game_text(f.get("event_id"), 96) or "UNKNOWN",
                          event_type=game_text(f.get("event_type"), 80) or "UNKNOWN",
                          date=f["date"], location=game_text(f.get("location"), 96) or "UNKNOWN",
                          last_seq=row["occ_seq"], bounded=True)
            lines = list(detail.get("lines") or [])
            line = dict(speaker=game_text(f.get("speaker"), 64) or "UNKNOWN", text=game_text(f.get("text"), 240))
            if type(f.get("narration")) is bool:
                line["narration"] = f["narration"]
            if f.get("phase") == "line" and line["text"] and line not in lines and len(lines) < LINES_MAX:
                lines.append(line)
            changes = list(detail.get("changes") or [])
            change = game_text(f.get("change"), 240)
            if change and change != "UNKNOWN" and change not in changes and len(changes) < CHANGES_MAX:
                changes.append(change)
            detail.update(lines=lines, changes=changes, unknown=detail.get("unknown", not old and f.get("phase") != "started")
                          or change == "UNKNOWN" or row.get("facts_truncated") is True,
                          complete=f.get("phase") == "ended" or detail.get("complete", False))
            item = dict(id=identity, status="ended" if detail["complete"] else "witnessed", facts=detail)
            # Existing store/schema; the full evidence is separate from the compact context string.
            M.record_items(base, "stardew", scope=scope, kind="native_story", items=[item], day=day, day_label=f["date"])
            known[identity] = dict(item_id=identity, facts=detail, first_day=day)
            kept.append(known[identity])
            changed.append(dict(ev="native_story_memory", scope=scope, day=day, date=f["date"],
                                event_id=detail["event_id"], location=detail["location"], phase=f.get("phase")))
        if changed:
            keep_witnessed(base, scope, kept, day=current_day, mind_scoped=mind_scoped)  # the one record keeps what she saw
            retire(base, scope, current_day, mind_scoped=mind_scoped)
    return changed


def retire(base, scope, day, *, keep_into=None, mind_scoped=False):
    """Retire only this actor's story rows; preserve every other ledger kind/scope.

    ADR 228 part C: before a row leaves, its witnessed lines are kept as receipts in the
    one record (``keep_into``: her actor scope; a HOST scope is never kept under itself).
    """
    path = M.game_dir(base, "stardew") / M.LEDGER_FILE
    rows = jsonl_snapshot.rows(path)  # read only here; a changed ledger is written whole below
    scoped = lambda r: r.get("kind") == "native_story" and r.get("scope_hash") == M._scope_hash(scope)
    # The native wake/date edge can precede Campaign's next-day handoff. A future
    # row is hidden until that handoff, never deleted as though it were stale.
    alive = [r for r in rows if scoped(r) and day - (
        day_of((r.get("facts") or {}).get("date")) or r.get("first_day", -TTL_DAYS)) < TTL_DAYS][-EVENTS_MAX:]
    keep = {r["id"] for r in alive}
    result = [r for r in rows if not scoped(r) or r.get("id") in keep]
    if result != rows:
        host = str(scope).endswith(":host")
        target = keep_into if host else (keep_into or scope)
        leaving = [dict(r, item_id=r.get("item_id") or r.get("id")) for r in rows if scoped(r) and r.get("id") not in keep]
        if target and keep_witnessed(base, target, leaving, host=host, day=day, mind_scoped=mind_scoped):
            # Keeping acknowledges through this same ledger file: drop the leaving rows from a fresh read.
            gone = {r.get("id") for r in leaving}
            result = [r for r in arti_state.read_jsonl(path) if r.get("id") not in gone]
        arti_state.write_jsonl_atomic(path, result)


KEPT_SOURCES = ("own_screen", "host_screen")  # ADR 228: receipts kept from story rows (long memory)
_KEPT = {}


def keep_witnessed(base, scope, rows, *, host=False, day=None, mind_scoped=False):
    """ADR 228 part C: what she saw of a story episode (speaker, words) becomes receipts in the one record.

    Spoken lines become ``dialogue_read``, narrator lines ``event_watched``, each with a text owner holding
    the native words and their true source (``host_screen`` for lines relayed from the HOST screen,
    ``own_screen`` for her own). A row with an event id and no words writes nothing: it stays identity-only.
    Idempotent; old lines are acknowledged so they never come back as a fresh cue.
    """
    import hashlib
    from arti_runtime.seen_receipts import SeenReceipts
    store = SeenReceipts(base, scope)
    source = "host_screen" if host else "own_screen"
    pending = []
    for row in rows:
        f = row.get("facts") if isinstance(row, Mapping) else None
        if not isinstance(f, Mapping) or not f.get("lines"):
            continue
        seen_day = day_of(f.get("date"))
        if seen_day is None:
            continue
        memo = (str(base), scope, str(row.get("item_id") or row.get("id")))
        lines = [line for line in f.get("lines") or () if isinstance(line, Mapping)
                 and isinstance(line.get("text"), str) and line["text"].strip() and line["text"] != "UNKNOWN"]
        if _KEPT.get(memo) == len(lines):
            continue
        place = f.get("location") if f.get("location") not in (None, "", "UNKNOWN") else None
        event = str(f.get("event_id") or "UNKNOWN")
        for line in lines:
            narration = line.get("narration") is True or str(line.get("speaker") or "UNKNOWN") == "UNKNOWN"
            speaker = "narasi" if narration else str(line["speaker"])
            digest = hashlib.sha256((speaker + "\0" + line["text"]).encode()).hexdigest()[:16]
            kind = "event_watched" if narration else "dialogue_read"
            identity = (event + ":" if narration else speaker + ":") + digest
            event_id = "story_line:%s:%s:%s" % (source, memo[2], digest)
            pending.append((memo, len(lines), seen_day, place, kind, identity, event_id, speaker, line["text"]))
    if not pending:
        return 0
    known = store.index()
    owners = [dict(id=p[6], kind="captured_text", subject=p[7], text=p[8], source=source, event_place=p[3],
                   date=label("stardew", p[2])) for p in pending]
    with M._JOURNEY_LOCK:
        for seen_day in sorted({p[2] for p in pending}):
            M.record_discoveries(base, "stardew", scope=scope, day=seen_day, day_label=label("stardew", seen_day),
                                 discoveries=[o for o, p in zip(owners, pending) if p[2] == seen_day],
                                 scoped=mind_scoped)
        fresh, old = 0, []
        for memo, count, seen_day, place, kind, identity, event_id, _, _ in pending:
            text_ref = hashlib.sha256((scope + "\0" + event_id).encode()).hexdigest()
            if store.repeat(known, kind, identity, game_day=seen_day, clock=None, place=place,
                            source=source, text_ref=text_ref) is None:
                receipt = store.note(kind, identity, game_day=seen_day, clock=None, place=place,
                                     source=source, text_ref=text_ref)
                known.setdefault((kind, identity), []).append(receipt)
                fresh += 1
                old.append("seen:" + hashlib.sha256((kind + "\0" + identity).encode()).hexdigest()[:24])
        for memo, count, *_ in pending:
            _KEPT[memo] = count
    if old and day is not None:
        # These lines already reached her through the story context; the ladder must not replay them as new.
        from arti_runtime.goal_ladder import acknowledge
        acknowledge(base, scope, old, day)
    return fresh


# ADR 230 N4: (mind base, scope) of saves whose pages follow the fact line; set only by fact_line.runtime_catalog.
# For them the calendar loaded with the save is not an observation: only entries she read on the board count.
BOARD_READ_ONLY = set()


def board_read_only(base, scope):
    return (str(base), scope) in BOARD_READ_ONLY


def calendar_memory(base, scope, day, calendar):
    """Native schedules share the existing scoped ledger and survive catalog gaps/restarts.

    ADR 230 N4: for a fact-line save only entries she read on the board are kept (status ``read``) and
    returned; rows written earlier from the loaded calendar (status ``observed``) stay, unread and unused.
    """
    read_only = board_read_only(base, scope)
    items = []
    for event in list(calendar)[:128]:
        if not isinstance(event, Mapping) or not event.get("id"):
            continue
        if read_only and event.get("_receipt_only") is not True:
            continue  # loaded save data, never something she saw
        dates = [d for d in event.get("dates", ()) if ordinal("stardew", d) is not None][:28]
        if dates:
            items.append(dict(id=game_text(event["id"], 96), status="read" if read_only else "observed", facts=dict(
                id=game_text(event["id"], 96), name=game_text(event.get("name"), 96), dates=dates,
                **{k: event[k] for k in ("kind", "location", "starts_at", "ends_at", "passive") if k in event},
                aliases=[game_text(a, 96) for a in event.get("aliases", ())[:8]])))
    if items:
        M.record_items(base, "stardew", scope=scope, kind="native_calendar", items=items,
                       day=day, day_label=label("stardew", day))
    return [r["facts"] for r in M.ledger_rows(base, "stardew", scope=scope, kind="native_calendar")
            if not read_only or r.get("status") == "read"]


def dated_story(base, scope, rows, calendar):
    """Bind quoted mail to native calendar dates, never to the later reading date.

    Unknown names stay unknown. Store absolute dates; recompute their relation
    at each presentation so a future notice cannot remain future after its date.
    """
    for row in rows:
        f = row["facts"]
        authored = day_of(f.get("date"))
        text = " ".join(line.get("text", "") for line in f.get("lines", ()))
        days = event_references(text, game="stardew", authored_day=authored, calendar=calendar)
        # Absolute dates are safe too. Relative words in a late-read letter have
        # unknown authorship and must never resolve against its reading date.
        for match in re.finditer(r"\b(?:Y(\d+)\s+)?(spring|summer|fall|winter)\s+(\d+)\b", text, re.I):
            year, season, date = match.groups()
            year = year or f["date"].split(":")[0]
            if (date_day := ordinal("stardew", f"Y{year} {season.lower()} {date}")) is not None:
                days.append(date_day)
        days = sorted(set(days + list(f.get("event_days") or [])))
        if days and days != f.get("event_days"):
            f["event_days"] = days
            M.record_items(base, "stardew", scope=scope, kind="native_story",
                           items=[dict(id=row["item_id"], status=row["status"], facts=f)],
                           day=authored, day_label=f["date"])
    return rows


def recent(base, scope, day, limit=CONTEXT_MAX, *, calendar=(), mind_scoped=False):
    if base is None or not scope or day is None:
        return []
    try:
        from arti_runtime.host_story import host_scope
        host = host_scope(scope)
        with M._JOURNEY_LOCK, _LOCK:  # journey before ledger, the order game_text_delivery uses
            retire(base, scope, day, mind_scoped=mind_scoped)
            calendar = calendar_memory(base, scope, day, calendar)
            rows = dated_story(base, scope, M.ledger_rows(base, "stardew", scope=scope, kind="native_story"), calendar)
            keep_witnessed(base, scope, rows, day=day, mind_scoped=mind_scoped)  # old-style rows too
            if host and host != scope:
                # Mabar: episodes the HOST watched for this same save, before or while she was elsewhere.
                keep_witnessed(base, scope, M.ledger_rows(base, "stardew", scope=host, kind="native_story"),
                               host=True, day=day, mind_scoped=mind_scoped)
                retire(base, host, day, keep_into=scope, mind_scoped=mind_scoped)
                hosted = M.ledger_rows(base, "stardew", scope=host, kind="native_story")
                rows = sorted(rows + hosted, key=lambda r: day_of((r.get("facts") or {}).get("date")) or 0)
        rows = [r for r in rows if (seen := day_of((r.get("facts") or {}).get("date"))) is not None
                and 0 <= day - seen < TTL_DAYS]
        return [dict(ref="story:" + r["id"][:16], text=summary(r["facts"], day=day)) for r in rows[-limit:]]
    except (OSError, ValueError, TypeError, KeyError):
        return []  # Observation/memory failure cannot remove an action or block native input.


_SIGNATURE = re.compile(r"(?:\^|^|\s)-\s*([A-Z][\w.' ]{1,30}?)\s*$")
_MAIL_ID = re.compile(r"\bmail:([\w./-]+):")


def letter_facts(base, scope, day, *, date):
    """FIX73D (a54 T115/T117 "[recorded words omitted]" hours after both were read): every letter she has
    opened, as one goal fact per letter (ADR 53 shape) so the words of a plan that names its sender carry the native
    read date and place. The sender is the letter's own signature; a letter without one stays unknown (no fact)."""
    if base is None or not scope or day is None:
        return []
    try:
        with _LOCK:
            rows = M.ledger_rows(base, "stardew", scope=scope, kind="native_story")
    except (OSError, ValueError, TypeError, KeyError):
        return []
    out = []
    for row in rows:
        f = row.get("facts") if isinstance(row, Mapping) else None
        if not isinstance(f, Mapping) or "letter" not in str(f.get("event_type")).lower():
            continue
        read_day = day_of(f.get("date"))
        if read_day is None or not 0 <= day - read_day < TTL_DAYS:
            continue
        text = " ".join(r.get("text", "") for r in f.get("lines") or () if isinstance(r, Mapping))
        found = _SIGNATURE.search(text.rstrip("^ "))
        if not found:
            continue
        sender = found.group(1).strip(" .")
        aliases = [sender] + ([sender.split()[-1]] if " " in sender else [])
        mail = next((m.group(1) for c in f.get("changes") or () if isinstance(c, str) and (m := _MAIL_ID.search(c))), None)
        year, season, read = str(f["date"]).split(":")
        place = f.get("location") if isinstance(f.get("location"), str) and f["location"] != "UNKNOWN" else None
        out.append(dict(kind="letter", name=mail or ("story:" + str(row.get("item_id"))[:16]), value="read", aliases=aliases,
                        date=date, line="surat dari %s: sudah kamu baca Y%s %s %s%s" % (
                            sender, year, season, read, " di " + place if place else "")))
    return out[-12:]


def summary(f, *, day=None):
    year, season, read_day = f["date"].split(":")
    head = f"Y{year} {season} {read_day}, {f['location']}; {f['event_type']} {f['event_id']}"
    if f.get("witness") == "host":
        head += "; watched on the HOST's screen with the owner (you were not in this scene)"
    if f.get("native_date"):
        head += "; native date shown then: %s (before day 1)" % f["native_date"]
    if f.get("event_days"):
        head += "; native event dates: " + fact(dict(referenced_days=f["event_days"]), game="stardew", day=day)
    # ADR 95: a quoted letter's relative words are on the writer's clock; live event dialogue is spoken now.
    if "letter" in str(f.get("event_type")).lower():
        if any(re.search(pattern, " ".join(r.get("text", "") for r in f.get("lines") or ()), re.I) for pattern, _ in RELATIVE_DATES):
            head += " (kata waktu di kutipan = saat ditulis, bukan saat dibaca)"
    # Native speech is quoted, not converted into a guessed narrative or a command.
    lines = f.get("lines") or []
    selected = context_lines(lines)
    shown = "; ".join(f"{r['speaker']}: {r['text']}" for r in selected)[:360]
    changes = "; ".join(f.get("changes", ())[-4:])[:320] or "UNKNOWN"
    return head + (f"; shown: {shown}" if shown else "; shown: UNKNOWN") + "; native changes around event: " + changes + "; other changes UNKNOWN" + (
        "; incomplete/UNKNOWN" if f.get("unknown") or not f.get("complete") else "")


def context_lines(lines):
    """Native narrator announcements plus the latest spoken line; no keyword/story-ID matching."""
    narrated = [r for r in lines if r.get("narration") is True][-2:]
    spoken = [r for r in lines if r.get("narration") is not True][-1:]
    return narrated + spoken


def reflection_facts(base, scope, day, *, calendar=(), mind_scoped=False):
    """Individual dated refs fit reflection's 140-character fact budget, including the native changes."""
    if base is None or not scope or day is None:
        return []
    try:
        with M._JOURNEY_LOCK, _LOCK:
            retire(base, scope, day, mind_scoped=mind_scoped)
            calendar = calendar_memory(base, scope, day, calendar)
            rows = [r for r in dated_story(base, scope, M.ledger_rows(base, "stardew", scope=scope, kind="native_story"), calendar)
                    if (seen := day_of((r.get("facts") or {}).get("date"))) is not None
                    and 0 <= day - seen < TTL_DAYS][-CONTEXT_MAX:]
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for row in rows:
        f = row["facts"]
        ref = "story:" + row["id"][:16]
        head = f"{f['date']} {f['location']} event {f['event_id']}"
        out.append(dict(ref=ref, text=head))
        for i, event_day in enumerate(f.get("event_days", ())):
            out.append(dict(ref=f"{ref}:date{i}", text="native event: " + fact(
                dict(referenced_days=[event_day]), game="stardew", day=day)))
        for i, change in enumerate(f.get("changes", ())[-4:]):
            out.append(dict(ref=f"{ref}:c{i}", text=f"{f['date']} native: {change}"[:140]))
        lines = f.get("lines") or []
        for i, line in enumerate(context_lines(lines)):
            out.append(dict(ref=f"{ref}:line{i}", text=f"{f['date']} {line['speaker']}: {line['text']}"[:140]))
        if not f.get("changes") or f.get("unknown"):
            out.append(dict(ref=ref + ":unknown", text=f"{f['date']} event {f['event_id']}: unread native changes UNKNOWN"))
    return out
