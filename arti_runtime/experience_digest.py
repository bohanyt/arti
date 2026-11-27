"""One experience record, read by the voice too (ADR 228 part A).

The game lane projects what is already in her first-seen ledger
(``discoveries.jsonl`` receipts and their text owners), her own day summaries
(``journey.jsonl``), her verified outcomes and her open wants into one small,
dated, attributed file: ``<mind>/stardew/experience_digest.json``. Nothing here
is a new store: the file is rewritten whole (temp file + replace) by its one
writer, the Live7 runtime, after each answered decision page and at the end of
a day. Selection is by place, time and recency only: no model, no retrieval,
no embedding.

The bridge reads it like ``now.json``: one ``stat`` per turn and a cached parse
keyed by mtime and size. A missing, malformed, oversized, restricted or stale
file (written before the bridge's current privacy scope began) gives no block.

People are only those with a scene, dialogue or text receipt. A name that the
ledger holds only as identity from save data (``villager_met`` from friendship
data, backfilled flags, ``text_available=false``) is never written as known; it
is counted once ("nama tercatat tanpa adegan").
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import threading
import time

SCHEMA = "arti.experience_digest/1"
SETTING = "voice_game_memory_enabled"
FILE = "experience_digest.json"
GAME = "stardew"
BLOCK_MAX_BYTES = 1500
TAG = "[INGATAN GAME]"
PEOPLE_MAX = 12
PLACES_MAX = 10
SAID_MAX = 6
WANTS_MAX = 4
ACTIONS_MAX = 6
LINE_CHARS = 110
SPEECH = ("owner_said", "arti_said", "viewer_said")
# Subjects that name a reader surface or a narrator, never a person she saw speak.
NOT_PEOPLE = frozenset("""dialogue narasi narration narrator unknown owner arti journal letter calendar cc cc_note
viewer penonton donasi system menu sign door""".split())
_SPEECH_ROLES = {"owner_said": "owner", "arti_said": "aku"}
_COMPLETE_TEXT = {}
_BUILT = {}


def enabled(config):
    return (config or {}).get(SETTING, False) is True


def path(base):
    import arti_mind
    return arti_mind.game_dir(Path(base), GAME) / FILE


def _clip(text, limit=LINE_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _label(day):
    from arti_runtime.plan_dates import label
    return label(GAME, day) if type(day) is int and day > 0 else None


def place_names(ids, catalog=None):
    """ADR 231 part G: {native place id: the name her page shows for it}, never the native id itself.

    The page's own labels (``place_names.source_place``: her home as rumahmu/kabinmu, the native world graph's label,
    then the shared readable names), without the native id the page keeps in brackets: the voice reads words. A place
    with no name is not named (it is counted, never read aloud as an id)."""
    import re
    from arti_runtime.place_names import source_place
    out = {}
    for place in ids:
        if not isinstance(place, str) or not place:
            continue
        name = source_place(place, catalog)
        name = re.sub(r"\s*\(%s\)\Z" % re.escape(place), "", name) if isinstance(name, str) else None
        if name and name != place and not name.startswith("tempat (nama belum diketahui)"):
            out[place] = name
    return out


def _places_part(places, labels):
    """The places she has been, by their page names; numbered floors of one place folded as her page words them
    (``tambang lantai 1-4``, from ``place_names``' own floor pattern). Unnamed places are counted."""
    from arti_runtime.place_names import _MINE
    rows, floors, unnamed = [], [], 0
    for p in places:
        if p.get("event"):
            rows.append("%s (%s %02d:%02d)" % (p["label"], _label(p["first_day"]), *divmod(p["clock"], 100)))
            continue
        mine = _MINE.match(str(p.get("place") or ""))
        if mine:
            floors.append((int(mine.group(1)), p["first_day"]))
            continue
        name = labels.get(p.get("place"))
        if name:
            rows.append("%s (sejak %s)" % (name, _label(p["first_day"])))
        else:
            unnamed += 1
    if floors:
        numbers = sorted({f for f, _ in floors})
        if len(numbers) == 1:
            span = "%d" % numbers[0]
        elif numbers == list(range(numbers[0], numbers[-1] + 1)):
            span = "%d-%d" % (numbers[0], numbers[-1])
        else:
            span = ", ".join(str(n) for n in numbers)
        rows.append("tambang lantai %s (sejak %s)" % (span, _label(min(d for _, d in floors))))
    if unnamed:
        rows.append("%d tempat lain yang belum kukenal namanya" % unnamed)
    return rows


def _said_at(row):
    """ADR 231 part B3: " (di game Y1 winter 8 07:10)": the game day and clock the line was said at."""
    day = _label(row.get("day"))
    if not day:
        return ""
    clock = row.get("clock")
    return " (di game %s%s)" % (day, " %02d:%02d" % divmod(clock, 100) if type(clock) is int and 0 <= clock <= 2600 else "")


def _person(subject):
    name = " ".join(str(subject or "").split())
    if not name or len(name) > 40 or not name[0].isalpha() or name.casefold() in NOT_PEOPLE:
        return None
    return name


def _speaker(receipt, owner, read_text):
    """The native speaker of a dialogue/scene receipt, only from its own text owner."""
    if receipt["kind"] not in ("dialogue_read", "event_watched"):
        return None
    ref = receipt.get("text_ref")
    if isinstance(ref, str) and ref.startswith("game_text:"):
        shown = read_text(ref)
        return _person((shown or {}).get("speaker")) if shown and shown.get("complete") is True else None
    if owner is None:
        return None
    return _person(owner.get("subject"))


def _text(receipt, owners, read_text):
    ref = receipt.get("text_ref")
    if not isinstance(ref, str):
        return None
    if ref.startswith("game_text:"):
        shown = read_text(ref)
        return shown["text"] if shown and shown.get("complete") is True and shown.get("text") else None
    owner = owners.get(ref)
    return owner.get("text") if owner else None


def _attribution(receipt, owner):
    kind = receipt["kind"]
    if kind in _SPEECH_ROLES:
        return _SPEECH_ROLES[kind]
    from arti_runtime.stream_notes import said_label  # ADR 230: "kata <name>"; a hash-only row is "seorang penonton"
    return said_label(dict(owner) if isinstance(owner, Mapping) else {})


def build(base, scope, *, day, place=None, actions=None, now=None, catalog=None, mind_scoped=False):
    """The projection, from the existing ledger only. ``actions``: {day: [verified outcome text, ...]}.

    ``catalog`` (ADR 231 part G): the page's native world read, for the names her page shows for places."""
    import arti_mind as M
    import arti_state
    from arti_runtime.seen_receipts import SeenReceipts
    store = SeenReceipts(base, scope)
    rows = arti_state.read_jsonl(store.path) if store.path.exists() else []
    scope_hash = store.scope_hash
    owners = {r["id"]: r for r in rows if isinstance(r, Mapping) and r.get("scope_hash") == scope_hash
              and r.get("kind") != "seen_receipt" and isinstance(r.get("text"), str) and r.get("text").strip()
              and isinstance(r.get("id"), str)}
    from arti_runtime.game_text_delivery import read_text as game_text

    def read_text(ref):
        key = (str(base), scope, ref)
        if key in _COMPLETE_TEXT:
            return _COMPLETE_TEXT[key]  # a complete, hash-checked delivery never changes
        try:
            shown = game_text(base, scope, ref)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if shown and shown.get("complete") is True:
            _COMPLETE_TEXT[key] = shown
        return shown

    people, unnamed, places, said, shown = {}, set(), {}, [], {}
    for receipt in store.receipts(rows=rows):
        kind, when = receipt["kind"], (receipt.get("game_day") or {}).get("absolute")
        if type(when) is not int or when > day:
            continue
        owner = owners.get(receipt.get("text_ref")) if isinstance(receipt.get("text_ref"), str) else None
        if kind == "villager_met":
            from arti_runtime.experience_facts import person_shown
            if person_shown(receipt):  # ADR 230 N2: the game showed her that person (dialogue, scene, social page)
                text = (owner or {}).get("text") if owner else None
                name = _person(str(text).partition(":")[0]) if isinstance(text, str) else None
                if name:
                    shown.setdefault(name, dict(name=name, first_day=when, place=receipt.get("place"),
                                                source=receipt.get("source"), line=None))
                    continue
            unnamed.add(receipt["id"])  # friendship data is identity, not a scene (counted, never "known")
            continue
        if kind == "place_entered":
            # Save flags prove identity, not the day she was there (fact_line writes them as native_save_flags:*).
            if (not receipt.get("identity_only") and not receipt.get("backfilled")
                    and not str(receipt.get("source") or "").startswith("native_save_flags")):
                places.setdefault(receipt["id"], dict(place=receipt["id"], first_day=when))
            continue
        if receipt.get("identity_only"):
            continue
        if kind in SPEECH:
            text = owner.get("text") if owner else None
            stream = (owner or {}).get("stream") or {}
            if text and stream.get("date") and "\ufffd" not in text:  # ADR 231 part G: whole or not at all
                said.append(dict(by=_attribution(receipt, owner), date=stream["date"], day=when,
                                 # ADR 231 part B3: the game clock it was said at (a row without one: date only)
                                 **({"clock": receipt["clock"]} if type(receipt.get("clock")) is int else {}),
                                 place=receipt.get("place"), text=text, wib=stream.get("wib"),
                                 # ADR 230: viewer names the line names, for a training export's replacement only
                                 **({"mentions": list(owner["mentions"])} if isinstance((owner or {}).get("mentions"), list)
                                    and owner["mentions"] else {})))
            continue
        if kind not in ("dialogue_read", "event_watched"):
            continue
        name = _speaker(receipt, owner, read_text)
        text = _text(receipt, owners, read_text) if name else None
        if name and text and name not in people:
            people[name] = dict(name=name, first_day=when, place=receipt.get("place"),
                                source=receipt.get("source"), line=text)
    for name, row in shown.items():  # a person shown without a captured line still counts as met (ADR 230 N2)
        people.setdefault(name, row)
    unnamed_count = len({u for u in unnamed if u not in people})
    here = str(place or "")
    # Place x time: what touches here first, then the newest; never a ranking of meaning.
    said.sort(key=lambda r: (r["day"], r.get("wib") or r["date"]), reverse=True)
    said.sort(key=lambda r: not (here and r.get("place") == here))
    said = [{k: v for k, v in r.items() if k != "wib"} for r in said[:SAID_MAX]]
    journey = {r.get("day"): r for r in M.load_journey(base, GAME, scope=scope, scoped=mind_scoped) if isinstance(r, Mapping)}
    days = {}
    for name, d in (("kemarin", day - 1), ("hari_ini", day)):
        summary = ((journey.get(d) or {}).get("reflection") or {}).get("today")
        done = list((actions or {}).get(d) or ())[-ACTIONS_MAX:]
        if (isinstance(summary, str) and summary.strip()) or done:
            days[name] = dict(date=_label(d), summary=summary.strip() if isinstance(summary, str) else None,
                              verified=done)
    goals = M.open_goals(M.load_goals(base, GAME, scope=scope, scoped=mind_scoped), day, horizons=M.SEASON_HORIZONS)
    wants = [dict(horizon=h, text=g["text"]) for h in ("long", "season", "today", "week") for g in goals.get(h, ())][:WANTS_MAX]
    people_rows = sorted(people.values(), key=lambda p: (p.get("place") != here or not here, p["first_day"]))[:PEOPLE_MAX]
    place_rows = sorted(places.values(), key=lambda p: (p["place"] != here, -p["first_day"]))[:PLACES_MAX]
    labels = place_names({p["place"] for p in place_rows} | {p.get("place") for p in people_rows} | {place}, catalog)
    from arti_runtime.day_record import project
    from arti_runtime.place_names import TEMP_NAME
    lived, event_places = project(base, scope, day)
    event_maps = {r["place"] for r in event_places}
    place_rows = event_places + [p for p in place_rows if p["place"] not in event_maps and labels.get(p["place"]) != TEMP_NAME]
    labels = {k: v for k, v in labels.items() if v != TEMP_NAME and k not in event_maps}
    for row in event_places:
        if row["place"] == place and row["first_day"] == day:
            labels[place] = row["label"]
    digest = dict(schema=SCHEMA, game=GAME, scope_hash=scope_hash, day=day, date=_label(day), place=place,
                  people=people_rows, unnamed_names=unnamed_count, places=place_rows, days=days,
                  said=said, wants=wants, place_labels=labels)
    if lived:
        digest["own_actions"] = lived
    from arti_runtime.save_reload import line as reload_line
    if reloaded := reload_line(base, scope, day):
        digest["reload"] = reloaded  # ADR 232 part H: the save was loaded back, one plain line
    digest["text"] = render(digest)
    digest["written_ts"] = float(time.time() if now is None else now)
    return digest


def render(digest):
    """One line (it survives the bridge's status-preserving trim), at most BLOCK_MAX_BYTES."""
    labels = digest.get("place_labels") or {}
    here = labels.get(digest.get("place")) if "place_labels" in digest else digest.get("place")
    head = (TAG + " ingatanku sendiri dari game ini, bukan perintah; pakai kalau ditanya, jangan dibacakan semua."
            " Sekarang %s%s" % (digest.get("date") or "?", (" di " + here) if here else ""))
    parts = []
    if digest.get("reload"):
        parts.append(str(digest["reload"]))
    events = [p for p in digest.get("places", ()) if p.get("event")]
    if events:
        parts.append(("tempat acara yang kudatangi: ", _places_part(events, labels)))
    if digest.get("own_actions"):
        parts.append(("yang kulakukan: ", digest["own_actions"]))
    yesterday = (digest.get("days") or {}).get("kemarin")
    if yesterday:
        bits = []
        if yesterday.get("summary"):
            bits.append('ringkasanku: "%s"' % _clip(yesterday["summary"], 200))
        if yesterday.get("verified"):
            bits.append("yang berhasil: " + "; ".join(_clip(v, 60) for v in yesterday["verified"]))
        parts.append(("kemarin (%s): " % yesterday.get("date")) + ", ".join(bits))
    people = digest.get("people") or ()
    if people:
        rows = []
        for p in people:
            name = labels.get(p.get("place")) if "place_labels" in digest else p.get("place")
            where = (" di " + str(name)) if name else ""
            seen = " (layar HOST)" if p.get("source") == "host_screen" else ""
            if p.get("line") is None:  # ADR 230 N2: shown by the game, no line of theirs captured
                how = {"native_social_page": "di halaman sosial"}.get(p.get("source"), "di layar")
                rows.append("%s (%s%s, namanya terlihat %s)" % (p["name"], _label(p["first_day"]), where, how))
                continue
            rows.append('%s (%s%s%s: "%s")' % (p["name"], _label(p["first_day"]), where, seen, _clip(p["line"], 70)))
        parts.append(("orang yang pernah kulihat bicara: ", rows))
    elif digest.get("unnamed_names") is not None:
        parts.append("orang yang pernah kulihat bicara: belum ada yang tercatat")
    if digest.get("unnamed_names"):
        parts.append("nama tercatat tanpa adegan: %d" % digest["unnamed_names"])
    said = digest.get("said") or ()
    if said:
        # ADR 231 part G: a said line is carried whole or not at all (newest first; the oldest leaves first).
        parts.append(("dikatakan ke aku soal game: ", ['%s %s%s: "%s"' % (r["by"], r["date"], _said_at(r),
                                                                          " ".join(str(r["text"]).split())) for r in said]))
    today = (digest.get("days") or {}).get("hari_ini")
    if today:
        bits = []
        if today.get("summary"):
            bits.append('ringkasanku: "%s"' % _clip(today["summary"], 160))
        if today.get("verified"):
            bits.append("yang berhasil: " + "; ".join(_clip(v, 60) for v in today["verified"]))
        parts.append("hari ini: " + ", ".join(bits))
    wants = digest.get("wants") or ()
    if wants:
        parts.append(("keinginanku yang masih terbuka: ", ["%s: %s" % (w["horizon"], _clip(w["text"], 80))
                                                           for w in wants]))
    places = [p for p in digest.get("places", ()) if not p.get("event")]
    if places:
        rows = (_places_part(places, labels) if "place_labels" in digest
                else ["%s (sejak %s)" % (p["place"], _label(p["first_day"])) for p in places])
        if rows:
            parts.append(("tempat yang pernah kudatangi: ", rows))
    # Whole parts in priority order; a part that does not fit is shortened, then left off. ADR 231 part G: a part of
    # items (said lines, people, wants, places) loses whole items from its end, never a piece of one; no replacement
    # character reaches the voice.
    line = head
    for part in parts:
        if isinstance(part, tuple):
            prefix, items = part[0], [i for i in part[1] if "\ufffd" not in i]
            while items and len((line + " | " + prefix + "; ".join(items)).encode("utf-8")) > BLOCK_MAX_BYTES:
                items = items[:-1]
            if items:
                line = line + " | " + prefix + "; ".join(items)
            continue
        part = part.replace("\ufffd", "")
        candidate = line + " | " + part
        while len(candidate.encode("utf-8")) > BLOCK_MAX_BYTES and "; " in part:
            part = part.rsplit("; ", 1)[0]
            candidate = line + " | " + part
        if len(candidate.encode("utf-8")) <= BLOCK_MAX_BYTES:
            line = candidate
    return line.replace("\ufffd", "")


def write(base, digest):
    """One writer, whole-file replace; a failure never blocks a decision."""
    from arti_runtime import ipc
    return ipc.write_json(path(base), digest, critical=False)


def live7_scope(runtime, session, scope):
    """A different save scope replaces the file at once, so no voice turn reads another save's memory."""
    config = getattr(runtime, "_config", None) or {}
    if not enabled(config) or not scope or getattr(runtime, "_experience_digest_scope", None) == scope:
        return None
    runtime._experience_digest_scope = scope
    base = runtime._live7_mind_base()
    if base is None:
        return None
    try:
        from arti_runtime import ipc
        old = ipc.read_json(path(base))
        if isinstance(old, Mapping) and old.get("scope_hash") == hashlib.sha256(scope.encode()).hexdigest():
            return None
    except (OSError, ValueError):
        pass
    return live7_write(runtime, session)


def live7_write(runtime, session, *, place=None):
    """The game lane's single call site. Off by default; never raises."""
    config = getattr(runtime, "_config", None) or {}
    if not enabled(config):
        return None
    try:
        base = runtime._live7_mind_base()
        scope = getattr(runtime, "_live7_discovery_scope", None)
        from arti_stardew_live7 import mind_day
        campaign = getattr(session, "campaign", None)
        day = mind_day(getattr(campaign, "date", None))
        if base is None or not scope or day is None:
            return None
        from arti_runtime.progress_counts import harvest_names
        actions = {}
        for row in getattr(campaign, "days", ()) or ():
            d = mind_day(row.get("date")) if isinstance(row, Mapping) else None
            if d is None:
                continue
            for r in row.get("skills", ()) or ():
                if isinstance(r, Mapping) and r.get("verified") is True and r.get("skill"):
                    arg = r.get("arg")
                    shown = "" if arg is None or str(arg).startswith("jelajah-") else " " + str(arg)
                    actions.setdefault(d, []).append("%s%s%s" % (r["skill"], shown, harvest_names(r)))
        if place is None:
            history = getattr(getattr(campaign, "live", None), "history", None) or ()
            place = next((h.get("location") for h in reversed(history) if isinstance(h, Mapping) and h.get("location")), None)
        import arti_mind
        from arti_runtime import mind_scope
        mind_scoped = mind_scope.enabled(config)
        folder = arti_mind.game_dir(Path(base), GAME)
        stamps = []
        stamp_names = ("discoveries.jsonl", arti_mind.JOURNEY_FILE, arti_mind.GOALS_FILE, arti_mind.LEDGER_FILE)
        if mind_scoped:
            stamp_names = stamp_names + (mind_scope.LEGACY_SCOPES_FILE,)
        for name in stamp_names:
            try:
                st = os.stat(folder / name)
                stamps.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append(None)
        # ADR 231 part G: the page's own world read names the places (her home, the native labels she saw).
        catalog = getattr(runtime, "_live7_fact_line_catalog", None) or getattr(runtime, "_live7_raw_knowledge", None)
        catalog = catalog if isinstance(catalog, Mapping) else None
        names = ([(r.get("id"), r.get("label")) for r in ((catalog or {}).get("world_graph") or {}).get("places") or ()
                  if isinstance(r, Mapping)], ((catalog or {}).get("rest") or {}).get("home"))
        key = json.dumps([scope, day, place, stamps, actions.get(day), actions.get(day - 1), names, mind_scoped], default=str)
        cached = _BUILT.get(str(base))
        if cached is not None and cached[0] == key:
            digest = dict(cached[1], written_ts=time.time())  # unchanged record; fresh for the voice's privacy scope
        else:
            digest = build(base, scope, day=day, place=place, actions=actions, catalog=catalog, mind_scoped=mind_scoped)
            _BUILT[str(base)] = (key, digest)
        if mind_scoped:
            with mind_scope.file_lock(path(base)):
                wrote = write(base, digest)
        else:
            wrote = write(base, digest)
        if wrote and digest.get("own_actions"):
            current = tuple(digest["own_actions"])
            if current != getattr(runtime, "_experience_digest_action_rows", ()):
                runtime._experience_digest_action_rows = current
                runtime._journal(dict(ev="experience_digest_actions", day=day, count=len(current),
                    event_places=sum(bool(r.get("event")) for r in digest["places"])))
        return digest
    except Exception as exc:  # noqa: BLE001 - memory projection never blocks the game lane
        journal = getattr(runtime, "_journal", None)
        if callable(journal):
            journal(dict(ev="experience_digest_error", error=type(exc).__name__))
        return None


class Reader:
    """The bridge's turn-path read: one stat, a parse only when mtime/size change."""

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._text = ""
        self._written = 0.0
        self._scope_hash = None

    def block(self, file, *, not_before=0.0, restricted=False, scope_hash=None, require_scope=False):
        if restricted:
            return ""
        try:
            st = os.stat(file)
        except OSError:
            return ""
        key = (str(file), st.st_mtime_ns, st.st_size)
        with self._lock:
            if key != self._key:
                self._key, self._text, self._written, self._scope_hash = key, "", 0.0, None
                try:
                    if st.st_size > 64 * 1024:
                        raise ValueError("oversized")
                    from arti_runtime import ipc
                    row = json.loads(ipc.read_text(file))
                    text, written = row.get("text"), row.get("written_ts")
                    if (row.get("schema") == SCHEMA and isinstance(text, str) and text.startswith(TAG)
                            and "\n" not in text and "\ufffd" not in text and len(text.encode("utf-8")) <= BLOCK_MAX_BYTES
                            and type(written) in (int, float)):
                        self._text, self._written = text, float(written)
                        self._scope_hash = row.get("scope_hash") if isinstance(row.get("scope_hash"), str) else None
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            text, written, row_scope = self._text, self._written, self._scope_hash
        if not text or written < float(not_before or 0.0):
            return ""
        if require_scope:
            from arti_runtime import mind_scope
            if not mind_scope.is_scope_hash(scope_hash) or row_scope != scope_hash:
                return ""
        return "\n" + text + "\n"


_READERS = {}
_READERS_LOCK = threading.Lock()


# The bridge's status-preserving trim keeps these single fact lines; this adds only the digest tag.
RESERVED_PATTERN = r"(?m)^\[(?:STARDEW(?: STATUS)?|NIATKU|KINI|NATIVE_FACTS|PLAN_DATES|INGATAN GAME)\][^\r\n]*"


def bridge_block(bridge):
    """The bridge hook (called only with the setting on): the current privacy scope bounds freshness."""
    try:
        import arti_mind
        from arti_runtime import mind_scope
        privacy = bridge.screen_privacy
        cfg = getattr(bridge, "CONFIG", None) or {}
        if mind_scope.enabled(cfg):
            lived, playing = mind_scope.bridge_memory_scope(bridge)
            if not lived:
                return ""
            block = voice_block(arti_mind.mind_dir(cfg), not_before=privacy.changed_at,
                                restricted=privacy.restricted, scope_hash=lived, require_scope=True)
            # The header's "Sekarang <date> di <place>" is true only while she plays; afterwards it is where she last was.
            return block if playing else block.replace(" Sekarang ", " Terakhir main ", 1)
        return voice_block(arti_mind.mind_dir(cfg), not_before=privacy.changed_at,
                           restricted=privacy.restricted)
    except Exception:  # noqa: BLE001 - a persona turn never fails on the memory file
        return ""


def voice_block(base, *, not_before=0.0, restricted=False, scope_hash=None, require_scope=False):
    """``"\\n[INGATAN GAME] ...\\n"`` or '' (missing/stale/broken/restricted -> nothing). Never raises."""
    try:
        file = path(base)
        with _READERS_LOCK:
            reader = _READERS.setdefault(str(file), Reader())
        return reader.block(file, not_before=not_before, restricted=restricted, scope_hash=scope_hash,
                            require_scope=require_scope)
    except Exception:  # noqa: BLE001 - a persona turn never fails on the memory file
        return ""
