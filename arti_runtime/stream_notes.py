"""ADR 216: transient voice inbox, structural gate, and the existing ledger writer.

Inbox v1: wall_ts, utc, wib, session, speaker, ingress, text, game_time,
privacy_epoch, owner_marked, game_active, recent_moments. A control row holds
the current session/epoch (and restriction) across the two processes. It is
transport metadata, survives draining, and never becomes a discovery.

``stream``, ``truncated`` and her ``said_as``/``by_surface`` live on the text
owner, because SeenReceipts.note has no metadata parameter. The text_ref
names that exact envelope. The receipt API and source identifiers stay intact.

Main's ADR 219 game_text_delivery.capture/answered are native-UI delivery
writers: their text owner is ledger.jsonl game_text facts, with game_text:
pointers and answered-page offsets. Voice utterances are not native ui.text
records. ADR 216/fix121A instead require a discoveries.jsonl text_read envelope
and its scoped hash as text_ref. record_line keeps only that incompatible text
owner adapter and reuses main's SeenReceipts.note unchanged for the receipt.
"""
from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Literal, NotRequired, TypedDict


class InboxRow(TypedDict):
    """Versioned transient transport. ADR 230: a viewer's display name rides to her record (who told her);
    the bridge's roster of viewers is never serialized, only the names a line itself names (``mentions``)."""
    v: Literal[1]
    wall_ts: float
    utc: str
    wib: str
    session: str
    speaker: Literal["owner", "arti", "viewer", "donasi"]
    ingress: Literal["ptt", "mic_pasif", "spoken_reply", "chat", "donation"]
    text: str
    game_time: Any
    privacy_epoch: int
    game_active: bool
    recent_moments: list[str]
    owner_marked: NotRequired[bool]
    viewer: NotRequired[str]  # ADR 228: a short stable hash; it keys the rate limits
    viewer_name: NotRequired[str]  # ADR 230: the display name as the bridge has it
    mentions: NotRequired[list[str]]  # ADR 230: viewer display names the line itself names
    marked_by: NotRequired[str]  # ADR 232: "answered" = an unmarked passive-mic owner line she answered aloud;
    # ADR 233: "voice_marked" = the line her voice model marked as about her play (the play tag of her reply)

WIB = timezone(timedelta(hours=7))
NAME_CHARS = 60  # a display name as the bridge has it, whitespace collapsed; longer names are cut, never guessed
_LOCK = threading.RLock()
_MOMENTS = deque(maxlen=3)
_VIEWERS = set()
_SESSION = None
# ADR 228 part B: chat and donation lines are a source too (owner ruling 2026-11-17). Rate limits:
VIEWER_INBOX_GAP_S = 60.0      # bridge: one inbox row per viewer per minute
CHAT_INBOX_WINDOW_S = 600.0    # bridge: at most 30 chat/donation inbox rows per ten minutes, all viewers
CHAT_INBOX_MAX = 30
VIEWER_KEPT_GAP_S = 600.0      # record: one kept line per viewer per ten minutes
VIEWER_KEPT_SESSION_MAX = 6    # record: six kept lines per viewer per stream session
VIEWERS_KEPT_SESSION_MAX = 24  # record: 24 kept viewer + donation lines per stream session
_CHAT_SENT = {}
_CHAT_RECENT = deque()
_ANSWERING = None  # ADR 232 part A: (trigger, epoch) of the voice turn in progress, set when its trigger is delivered
VIEWER_SOURCES = {"viewer": "chat", "donasi": "donation"}


def viewer_hash(name):
    """Tells viewers apart without storing who they are: 8 hex of a salted hash of the display name."""
    return hashlib.sha256(("penonton\0" + str(name or "").strip().lstrip("@").casefold()).encode()).hexdigest()[:8]
_STOP = frozenset("arti owner aku kamu dia ini itu yang dan di ke dari dengan untuk ya sih nih tuh ada tidak bukan juga kok apa the a an is to of in on it i you we this that sekarang dulu lagi banget sudah belum".split())


def _path(base):
    return Path(base) / "stardew" / "stream_notes_inbox.jsonl"


@contextmanager
def _inbox_lock(base):
    """Serialize append/drain/purge across bridge and Live7, including Windows.

    The zero-byte sibling lock is coordination only, not another memory store.
    All three operations hold it through ledger commit, so privacy purge and
    ingest have a single ordering and a concurrent append cannot be lost.
    """
    path = _path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.with_suffix(".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _rows(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            row = json.loads(line)
            rows.append(row if isinstance(row, dict) else {"invalid": True})
        except ValueError:
            rows.append({"invalid": True})
    return rows


def _write(path, rows):
    import arti_state
    arti_state.write_jsonl_atomic(path, rows)


def append_inbox(mind_base, row: InboxRow):
    """Append an admitted ingress row; never persist viewer names/rosters."""
    if (not row.get("game_active") or row.get("restricted")
            or row.get("speaker") not in ("owner", "arti", *VIEWER_SOURCES)
            or contains_viewer(row.get("text", ""), row.get("viewer_names", ()))):
        return False
    clean = {k: v for k, v in row.items() if k not in ("viewer_names", "current_epoch")}
    with _inbox_lock(mind_base) as path:
        rows = _rows(path)
        boundary = next((r for r in rows if r.get("control") == "privacy"), None)
        epoch, session = row["privacy_epoch"], row["session"]
        if boundary and boundary.get("session") == session:
            if boundary["privacy_epoch"] != epoch or boundary.get("restricted"):
                return False
        else:
            rows = [dict(control="privacy", session=session, privacy_epoch=epoch, restricted=False)]
        _write(path, rows + [clean])
    return True


def prune_inbox_before_epoch(mind_base, epoch, *, session=None, restricted=False):
    """Revoke pending rows and publish the boundary even when inbox was empty."""
    if not _path(mind_base).exists():
        _MOMENTS.clear()
        _TOLD.clear()  # ADR 233: chat lines from before the boundary are never marked after it
        return
    with _inbox_lock(mind_base) as path:
        rows = _rows(path)
        session = session or next((r.get("session") for r in rows if r.get("session")), "")
        kept = [r for r in rows if r.get("control") != "privacy"
                and r.get("privacy_epoch") == epoch and r.get("session") == session and not restricted]
        _write(path, [dict(control="privacy", session=session, privacy_epoch=epoch, restricted=restricted)] + kept)
        _MOMENTS.clear()
        _TOLD.clear()


def verbatim(text, limit=300):
    """Never paraphrase or cut a sentence. No complete sentence => empty prefix."""
    if len(text) <= limit:
        return text, False
    ends = list(re.finditer(r'[.!?](?:["\u201d\u2019\')\]]*)(?=\s|$)', text[:limit + 1]))
    end = max((m.end() for m in ends if m.end() <= limit), default=0)
    return text[:end], True


def said_as(text):
    if text.rstrip().endswith("?") or re.search(r"\b(?:kenapa|apa ya|penasaran)\b", text, re.I):
        return "question"
    if re.search(r"\b(?:kayaknya|mungkin|pasti)\b", text, re.I):
        return "guess"
    if re.search(r"\b(?:pengin|mau|pingin)\b", text, re.I):
        return "want"
    return "opinion"


def strings(value):
    """Page vocabulary comes from string values only, never schema keys."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, (tuple, list)):
        for v in value:
            yield from strings(v)


def words(values):
    return {w for text in strings(values) for w in re.findall(r"\w+", text.casefold())
            if len(w) > 1 and not w.isdecimal() and w not in _STOP}


# Gate v2 (ADR 216, real-data revision 2026-11-16): "[recorded remark omitted]" means a GAME NAME from what
# either lane was shown, or one of her own game action words. Ordinary words on a page never count.
MOMENT_CHARS = 1200   # a moment keeps enough text for its names (a 435-char letter signs "Joja" near its end)
# Capitalized words that are not game names (sentence starts and filler in game/UI text, chat English).
_NOT_NAMES = frozenset("""
i im i'm ive you your yours we our they he she it its what who why how when where which whose there here this
that these those the a an and or but if so yes no not oh hey hi hello okay ok yeah wow well please thanks thank
sorry god game games stardew valley mr mrs ms dr day days today tomorrow yesterday morning evening night
spring summer fall winter monday tuesday wednesday thursday friday saturday sunday new my me us them his her
dear sincerely love from to of in on at by for with as is are was were be been am do does did have has had can
will would should could may might must just now then also very much more most some any all every each one two
three first last next good great nice bad
kamu aku dia ini itu hari tujuan pilihan lokasi waktu jam catatan fakta sekarang kemarin besok kalau jika yang
dan atau tapi ada tidak bukan sudah belum masih mau bisa harus boleh coba jangan tolong oke iya nah wah eh hmm
terus lalu baru lama nama main bareng semua banyak sedikit satu dua tiga kita kami mereka sini sana situ
arti bohan bos bang kak bahasa indonesia inggris chat stream streamer live guys video youtube saweria api wifi
internet donasi viewer penonton unknown""".split())


def _name_parts(token):
    """A compound game name also counts by its parts: "JojaMart" -> jojamart, joja, mart."""
    token = token.strip("'-")
    parts = re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+", token)
    return {token.casefold(), *(p.casefold() for p in parts if len(p) >= 3)}
# Her game action words (Indonesian), matched as stems inside a word ("dijual" -> "jual"). Closed list.
ACTION_STEMS = ("mancing", "pancing", "ikan", "siram", "nyiram", "tanam", "nanam", "benih", "panen", "jual",
                "kebun", "ladang", "cangkul", "kapak", "tambang", "museum", "peti", "hadiah", "bibit", "pupuk",
                "ternak")
_PREFIXES = ("", "di", "me", "mem", "men", "meng", "ber", "ter", "ke", "pe", "pem", "pen", "peng", "se")
ACTION_WORDS = frozenset(("tas", "chest", "uang", "kayu", "batu", "rumput", "kabin", "toko", "warga", "surat",
                          "bundel", "bundle", "alat", "tv"))
_SUFFIXES = ("nya", "ku", "mu", "lah", "kah", "pun")


def said_words(text):
    """Her/his words, also without an Indonesian clitic ("parsnipnya" -> parsnip, "tasku" -> tas)."""
    out = set()
    for t in re.findall(r"\w+", text.casefold()):
        out.add(t)
        for suffix in _SUFFIXES:
            if t.endswith(suffix) and len(t) - len(suffix) >= 3:
                out.add(t[:-len(suffix)])
    return out


def game_terms(texts):
    """Game names in game text: capitalized words that are not sentence starts, and short all-capitalized values
    (a field such as "Pelican Town" or "Bamboo Pole"). Casefolded; filler capitals are excluded."""
    out = set()
    for text in strings(texts):
        for token in re.findall(r"[^\W\d_][\w'-]*", text):
            if token[:1].isupper():
                out.update(_name_parts(token))
    return {t for t in out if len(t) >= 3 and t not in _NOT_NAMES}


def page_names(values):
    """From her decision page only name-shaped values count (a field such as "Pelican Town", "Bamboo Pole",
    "Penny"): the page also carries persona and program text, whose capitals are not game names."""
    out = set()
    for text in strings(values):
        tokens = re.findall(r"[^\W\d_][\w'-]*", text)
        if tokens and len(tokens) <= 3 and all(t[:1].isupper() for t in tokens):
            for t in tokens:
                out.update(_name_parts(t))
    return {t for t in out if len(t) >= 3 and t not in _NOT_NAMES}


def seen_terms(vocabulary):
    """vocabulary = {"game": game-origin texts, "page": her page's string values}, or a list of game texts."""
    if isinstance(vocabulary, dict):
        return game_terms(vocabulary.get("game", ())) | page_names(vocabulary.get("page", ()))
    return game_terms(vocabulary)


def moment_words(texts):
    """Content words of a moment shown in the last 180 s: a line reacting to it in its own words counts."""
    return {w for w in words(texts) if len(w) >= 5 and w not in _NOT_NAMES}


def action_word(text):
    toks = said_words(text)
    return any(t in ACTION_WORDS for t in toks) or any(
        t.startswith(prefix + stem) for t in toks for stem in ACTION_STEMS for prefix in _PREFIXES)


OWNER_NAMES = ("bohan",)  # coop_session.owner_name default; the owner chatting from his own account is the owner


def contains_viewer(text, names, owners=None):
    """A display name, its letters-only stem, or a spoken short form of it (a word of 5+ letters that starts the
    stem: "examplex" for "@examplexviewr530") counts. Conservative: a false hit only drops a line."""
    return bool(viewer_mentions(text, names, owners))


def _viewer_stem(name, owners=None):
    raw = str(name).strip().lstrip("@").casefold()
    stem = "".join(re.findall(r"[^\W\d_]+", raw))
    if not raw or any(stem.startswith(owner) for owner in (owners or OWNER_NAMES)):  # a blank name names nothing
        return raw, None  # the owner's own chat account (e.g. @bohanyt) is not a viewer
    return raw, stem


def viewer_mentions(text, names, owners=None):
    """The viewer display names ``text`` names, by contains_viewer's rule, in the order given (ADR 230)."""
    folded = str(text).casefold()
    toks = set(re.findall(r"[^\W\d_]+", folded))
    out = []
    for name in names:
        raw, stem = _viewer_stem(name, owners)
        if stem is None:
            continue
        if (raw in folded or (len(stem) >= 4 and stem in folded)
                or (len(stem) >= 6 and any(len(t) >= 5 and stem.startswith(t) for t in toks))):
            out.append(display_name(name))
    return list(dict.fromkeys(n for n in out if n))


def display_name(name):
    """A viewer's display name as the bridge has it, whitespace collapsed (ADR 230)."""
    return " ".join(str(name or "").split())[:NAME_CHARS]


def said_label(owner):
    """ADR 230: who told her, as her page and the voice digest show it.

    A viewer row with a name is "kata <name>" ("(donasi)" added for a donation
    message); a row written before ADR 230 (hash only) is "seorang penonton".
    Owner and her own rows are labelled by their callers, unchanged.
    """
    owner = owner if isinstance(owner, dict) else {}
    donation = " (donasi)" if owner.get("said_by") == "donasi" else ""
    name = display_name(owner.get("said_name"))
    return ("kata %s" % name if name else "seorang penonton") + donation


TRAINING_NAME = "viewer"


def _replace_viewer(text, name, owners=None):
    """Replace a viewer's name inside text with ``viewer``: the name, its letters-only stem, a spoken short form."""
    raw, stem = _viewer_stem(name, owners)
    if stem is None:
        return text
    shown = display_name(name).lstrip("@")
    if shown:
        text = re.sub(r"(?<![\w@])@?" + re.escape(shown) + r"(?!\w)", TRAINING_NAME, text, flags=re.I)

    def token(match):
        letters = "".join(re.findall(r"[^\W\d_]+", match.group(0).casefold()))
        hit = ((len(stem) >= 4 and stem in letters and letters != TRAINING_NAME)
               or (len(stem) >= 6 and len(letters) >= 5 and stem.startswith(letters)))
        return TRAINING_NAME if hit else match.group(0)
    return re.sub(r"@?\w+", token, text)


def anonymise_for_training(row, names=()):
    """ADR 230: the one replacement every training-data export calls on a said row or digest line.

    ``row``: a said text owner from discoveries.jsonl (``said_name``,
    ``mentions``, ``viewer``), a planner/page evidence row (``oleh``), a digest
    said row (``by``), or a plain string (a digest line). ``names``: other
    viewer names the exporter knows (for a line whose own fields do not carry
    them). Every viewer name, in its fields and inside the quoted text, becomes
    the plain word ``viewer``; the viewer hash is dropped. Owner and "aku"
    labels stay. Pure: the record itself is never changed.
    """
    if isinstance(row, str):
        text = row
        for name in sorted({display_name(n) for n in names if display_name(n)}, key=len, reverse=True):
            text = _replace_viewer(text, name)
        return text
    if not isinstance(row, dict):
        return row
    out = dict(row)
    known = [out.get("said_name"), *(out.get("mentions") or ()), *names]
    for key in ("oleh", "by"):
        label = out.get(key)
        if isinstance(label, str) and label.startswith("kata "):
            known.append(label[5:].removesuffix(" (donasi)"))
    known = sorted({display_name(n) for n in known if isinstance(n, str) and display_name(n)}, key=len, reverse=True)
    for key in ("oleh", "by"):
        label = out.get(key)
        if isinstance(label, str) and (label.startswith("kata ") or label.startswith(("penonton#", "donasi#"))):
            out[key] = said_label(dict(said_by="donasi" if label.endswith(" (donasi)") or label.startswith("donasi#")
                                       else "penonton", said_name=TRAINING_NAME))
    if "said_name" in out:
        out["said_name"] = TRAINING_NAME
    out.pop("mentions", None)
    out.pop("viewer", None)
    for key in ("text", "subject"):
        if isinstance(out.get(key), str):
            for name in known:
                out[key] = _replace_viewer(out[key], name)
    return out


def about_the_game(row, vocabulary):
    if row.get("invalid") or not isinstance(row.get("text"), str):
        return False, "unparsed"
    if not row.get("game_active"):
        return False, "no_game_session"
    if row.get("restricted"):
        return False, "restricted"
    if row.get("privacy_epoch") != row.get("current_epoch", row.get("privacy_epoch")):
        return False, "pre_boundary"
    speaker, ingress = row.get("speaker"), row.get("ingress")
    if not ((speaker == "owner" and (ingress == "ptt" or ingress == "mic_pasif" and row.get("owner_marked") is True))
            or (speaker == "arti" and ingress == "spoken_reply")
            or (VIEWER_SOURCES.get(speaker) == ingress and isinstance(row.get("viewer"), str))):
        return False, "speaker_ingress"
    # ADR 230: no live path passes viewer_names (append_inbox strips them); only the offline backfill of
    # recordings made before the owner's 2026-11-17 ruling does, and that tool is unchanged.
    if contains_viewer(row["text"], row.get("viewer_names", ())):
        return False, "viewer_name"
    moments = row.get("recent_moments", [])
    seen = seen_terms(vocabulary) | game_terms(moments) | moment_words(moments)
    if not (said_words(row["text"]) & seen or action_word(row["text"])):
        if row.get("marked_by") == VOICE_MARKED:
            return True, VOICE_MARKED  # ADR 233: her voice model judged it is about her play (OR, never instead)
        return False, "no_seen_match"
    return True, "kept"


VOICE_MARKED = "voice_marked"
TOLD_SAME_S = 600.0  # ADR 233: the same words from the same teller within ten minutes are one told line


def _folded(text):
    return " ".join(str(text or "").split()).casefold()


def told_already(base, scope, row):
    """ADR 233: a voice-marked line whose words her record already holds as a told line (same session, same teller,
    within ten minutes; the same words, or one containing the other, which covers a cut line and a joined turn)."""
    return _told_match(base, scope, row) is not None


def _told_match(base, scope, row):
    """The told line of ``told_already`` closest in time to ``row`` (its text-owner row), or None."""
    import arti_mind
    text = _folded(row.get("text"))
    if not text:
        return None
    viewer = row.get("speaker") in VIEWER_SOURCES
    subject = ("penonton" if row.get("speaker") == "viewer" else "donasi") if viewer else row.get("speaker")
    at = float(row.get("wall_ts") or 0.0)
    best = None
    for owner in _rows(arti_mind.game_dir(base, "stardew") / "discoveries.jsonl"):
        stream = owner.get("stream") if isinstance(owner.get("stream"), dict) else {}
        if (owner.get("scope_hash") != arti_mind._scope_hash(scope) or stream.get("session") != row.get("session")
                or owner.get("subject") != subject or viewer and owner.get("viewer") != row.get("viewer")):
            continue
        try:
            gap = abs(datetime.fromisoformat(stream["wib"]).timestamp() - at)
        except (KeyError, TypeError, ValueError):
            continue
        kept = _folded(owner.get("text"))
        if gap <= TOLD_SAME_S and kept and (kept == text or kept in text or text in kept):
            if best is None or gap < best[0]:
                best = (gap, owner)
    return best[1] if best else None


def _stamp_marked(base, told, marked_by):
    """Pass-3 follow-up (ADR 233, 6): her tag on a line her record already holds is kept on that told line, so the
    tag rate stays measurable when the heard copy passed the gate first. Never overwrites an existing mark."""
    import arti_mind as M
    if not told.get("id"):
        return
    path = M.game_dir(base, "stardew") / "discoveries.jsonl"
    with M._JOURNEY_LOCK:
        rows = _rows(path)
        hit = [r for r in rows if r.get("id") == told["id"] and r.get("stream") == told.get("stream")
               and not r.get("marked_by")]
        for r in hit:
            r["marked_by"] = marked_by
        if hit:
            _write(path, rows)


def game_day(date):
    from arti_runtime.plan_dates import ordinal
    if type(date) is int and date > 0:
        return date
    if isinstance(date, (list, tuple)) and len(date) == 3:
        date = "Y%s %s %s" % tuple(date)
    if isinstance(date, str) and re.fullmatch(r"\d+:(spring|summer|fall|winter):\d+", date):
        date = "Y%s %s %s" % tuple(date.split(":"))
    day = ordinal("stardew", date)
    if day is None:
        raise ValueError("unparsed game date")
    return day


def stamp(wall_ts):
    dt = datetime.fromtimestamp(wall_ts, timezone.utc)
    return dict(wall_ts=wall_ts, utc=dt.isoformat().replace("+00:00", "Z"),
                wib=dt.astimezone(WIB).isoformat())


def record_line(base, scope, row, observation, *, mind_scoped=False):
    """Mirror fix121A's text_ref/record_discoveries contract; no receipt API fork."""
    import arti_mind as M
    from arti_runtime.plan_dates import label
    from arti_runtime.seen_receipts import SeenReceipts
    text, truncated = verbatim(row["text"])
    if not text:
        return "no_sentence_end"
    identity = hashlib.sha256((row["session"] + "\0" + str(row["wall_ts"]) + "\0" + row["text"]).encode()).hexdigest()
    event_id = "stream_text:" + row["speaker"] + ":" + identity
    text_ref = hashlib.sha256((scope + "\0" + event_id).encode()).hexdigest()
    source = "stream:%s:%s" % (row["speaker"], row["ingress"])
    day = game_day(observation["day"])
    date = label("stardew", day)
    receipts = SeenReceipts(base, scope)
    viewer = row["speaker"] in VIEWER_SOURCES
    kind = "viewer_said" if viewer else row["speaker"] + "_said"
    subject = ("penonton" if row["speaker"] == "viewer" else "donasi") if viewer else row["speaker"]
    with M._JOURNEY_LOCK:
        repeat = receipts.seen(kind, identity) is not None
        if row["speaker"] == "arti":
            # ADR 232 part A (CT addendum): what she said herself is her speech, never a discovery or a game text she
            # read (mabar 2026-11-17: a sentence her voice explained from model knowledge sat in her record as
            # text_read). Its text owner is a "said_text" row of the same store, outside her day's journey.
            _speech_owner(base, scope, dict(id=event_id, kind=SAID_TEXT, subject=subject, text=text, source=source,
                                            date=date, event_place=observation.get("place")), day=day)
        else:
            M.record_discoveries(base, "stardew", scope=scope, discoveries=[dict(
                id=event_id, kind="text_read", subject=subject, text=text,
                source=source, date=date, event_place=observation.get("place"))], day=day, day_label=date,
                scoped=mind_scoped)
        # record_discoveries deliberately projects known fields. Enrich only this
        # text owner under its existing writer lock; the envelope ID never changes.
        path = M.game_dir(base, "stardew") / "discoveries.jsonl"
        rows = _rows(path)
        dirty = False
        for owner in rows:
            if owner.get("id") == text_ref and "stream" not in owner:
                owner.update(stream=dict(date=row["wib"][:10], wib=row["wib"], session=row["session"]), truncated=truncated)
                if row["speaker"] == "arti":
                    owner.update(said_as=said_as(row["text"]), by_surface=True)
                if viewer:
                    owner.update(said_by=subject, viewer=row["viewer"])
                    if display_name(row.get("viewer_name")):  # ADR 230: who told her stays in her memory
                        owner["said_name"] = display_name(row["viewer_name"])
                if mentions := [display_name(n) for n in row.get("mentions") or () if display_name(n)]:
                    owner["mentions"] = mentions  # ADR 230: viewers the line names, for the training export
                if row.get("marked_by"):  # ADR 233, 6: "answered" (ADR 232) or "voice_marked" stays on the record
                    owner["marked_by"] = row["marked_by"]
                dirty = True
        if dirty:
            _write(path, rows)
        receipts.note(kind, identity, game_day=day, clock=observation.get("clock"),
                      place=observation.get("place"), source=source, text_ref=text_ref)
    return "repeat" if repeat else "kept"


# ADR 232: the text owner of her own spoken line (its receipt is ``arti_said``). Every ADR 216 row, text owner or
# receipt, carries ``source`` "stream:<speaker>:<ingress>": readers of game knowledge skip them by that source, which
# also recognises her lines written before ADR 232 as ``text_read`` (arti_mind.recent_discoveries,
# experience_facts.with_memory, goal_ladder.remember, live7_ingest's vocabulary).
SAID_TEXT = "said_text"


def _speech_owner(base, scope, event, *, day):
    """The text owner row record_discoveries would write, in the same file, without the journey's discoveries list."""
    import arti_mind as M
    from arti_runtime import ipc
    path = M.game_dir(base, "stardew") / "discoveries.jsonl"
    identity = hashlib.sha256((scope + "\0" + event["id"]).encode()).hexdigest()
    if any(r.get("id") == identity for r in _rows(path)):
        return
    row = dict(id=identity, kind=event["kind"], subject=event["subject"], text=event["text"], source=event["source"],
               date=event["date"], observed_day=day, day=day, day_label=event["date"],
               scope_hash=hashlib.sha256(scope.encode()).hexdigest(), native_seq=0, event_time=event["date"],
               event_place=event.get("event_place"))
    path.parent.mkdir(parents=True, exist_ok=True)
    ipc.append_text(path, json.dumps(row, ensure_ascii=False) + "\n")


def ingest(base, scope, observe_at, vocabulary_at, *, day_range=None, outcomes=None, mind_scoped=False):
    """Drain atomically; callbacks use stream wall seconds, never now.json dates.

    A row without a preceding native observation is dropped, never stamped with
    a future observation. day_range is an offline hard refusal, preflighted for
    the whole batch before any ledger writes. ``outcomes`` (ADR 229, the rig's
    said-lines instrument only; default None) collects each row's gate reason as
    (session, wall_ts, speaker, text hash, reason); it changes no decision.
    """
    if not _path(base).exists():
        return {}
    with _inbox_lock(base) as path:
        rows = _rows(path)
        boundary = next((r for r in rows if r.get("control") == "privacy"), None)
        pending = []
        for row in rows:
            if row.get("control") == "privacy":
                continue
            row = dict(row)
            if boundary:
                row["current_epoch"] = boundary["privacy_epoch"]
                row["restricted"] = row.get("restricted", False) or boundary.get("restricted", False)
                if row.get("session") != boundary.get("session"):
                    row["current_epoch"] = None
            try:
                observation = observe_at(float(row["wall_ts"]))
            except (KeyError, TypeError, ValueError):
                observation = None
            if observation and day_range and not day_range[0] <= game_day(observation["day"]) <= day_range[1]:
                raise ValueError("game day outside --days")
            pending.append((row, observation))
        counts = Counter()
        kept = viewer_kept(base, scope) if any(r.get("speaker") in VIEWER_SOURCES for r, _ in pending) else None
        for row, observation in pending:
            if observation is None:
                counts["no_observation"] += 1
                if outcomes is not None:
                    outcomes.append(_outcome(row, "no_observation"))
                continue
            vocabulary = vocabulary_at(row["wall_ts"]) if callable(vocabulary_at) else vocabulary_at
            keep, reason = about_the_game(row, vocabulary)
            gate = reason
            told = _told_match(base, scope, row) if keep and row.get("marked_by") == VOICE_MARKED else None
            if told is not None:
                keep, reason = False, "repeat"  # ADR 233: the line her page already has is not shown twice
                _stamp_marked(base, told, VOICE_MARKED)  # ADR 233, 6: who marked it stays on the told line
            if keep and row.get("speaker") in VIEWER_SOURCES:
                keep, reason = viewer_allowed(kept, row)
            if keep:
                reason = record_line(base, scope, row, observation, mind_scoped=mind_scoped)
                if kept is not None and reason == "kept" and row.get("speaker") in VIEWER_SOURCES:
                    kept.append((row["session"], row["viewer"], float(row["wall_ts"])))
                if reason == "kept" and gate == VOICE_MARKED:
                    reason = VOICE_MARKED
            counts[reason] += 1
            if outcomes is not None:
                outcomes.append(_outcome(row, reason))
        _write(path, [boundary] if boundary else [])
    return dict(counts)


def _outcome(row, reason):
    text = row.get("text") if isinstance(row.get("text"), str) else ""
    return dict(session=row.get("session"), wall_ts=row.get("wall_ts"), speaker=row.get("speaker"),
                text_sha256=hashlib.sha256(text.encode()).hexdigest(), reason=reason)


def viewer_kept(base, scope):
    """(session, viewer, wall seconds) of every kept viewer/donation line in the record, for the limits."""
    import arti_mind
    out = []
    for row in _rows(arti_mind.game_dir(base, "stardew") / "discoveries.jsonl"):
        stream = row.get("stream") if isinstance(row.get("stream"), dict) else {}
        if (row.get("scope_hash") == arti_mind._scope_hash(scope) and row.get("said_by") in ("penonton", "donasi")
                and stream.get("session")):
            try:
                at = datetime.fromisoformat(stream["wib"]).timestamp()
            except (KeyError, TypeError, ValueError):
                at = 0.0
            out.append((stream["session"], row.get("viewer"), at))
    return out


def viewer_allowed(kept, row):
    """Durable limits: chat cannot flood her record (ADR 228)."""
    session, viewer, at = row["session"], row["viewer"], float(row["wall_ts"])
    same = [k for k in kept if k[0] == session]
    if len(same) >= VIEWERS_KEPT_SESSION_MAX:
        return False, "viewer_session_limit"
    mine = [k for k in same if k[1] == viewer]
    if len(mine) >= VIEWER_KEPT_SESSION_MAX:
        return False, "viewer_limit"
    if any(abs(at - k[2]) < VIEWER_KEPT_GAP_S for k in mine):
        return False, "viewer_gap"
    return True, "kept"


def remember_moment(text, at, *, viewer_names=()):
    if not contains_viewer(str(text), viewer_names):
        with _LOCK:
            _MOMENTS.append((at, str(text)[:MOMENT_CHARS]))


def bridge_boundary(bridge):
    import arti_mind
    import session_transcript
    prune_inbox_before_epoch(arti_mind.mind_dir(bridge.CONFIG), bridge.screen_privacy.epoch,
                            session=session_transcript.current_session_id(bridge.CONFIG),
                            restricted=bridge.screen_privacy.restricted)


def bridge_delivered(bridge, trigger, *, epoch):
    """Tap the delivered game trigger in its turn epoch; no second reader."""
    with bridge._screen_privacy_ingress_lock:
        if (bridge.screen_privacy.restricted or not _game_active(bridge)
                or epoch is not None and not bridge.screen_privacy.current(epoch)):
            return
        _bridge_session(bridge)
        global _ANSWERING
        _ANSWERING = (trigger, epoch)  # ADR 232 part A: her fully voiced reply below answers this trigger
        now = time.time()
        viewers = bridge._yt_viewers_seen
        if trigger.trigger_type in ("game", "moment", "read_in_game", "own_play"):
            remember_moment(trigger.text, now, viewer_names=viewers)


def bridge_reply_facts(bridge, facts, *, epoch):
    """Tap HostStoryRelay.reply_context's exact delivered native quotations.

    The existing reader already enforces save identity and its context budget.
    Capturing its return avoids admitting unseen lines left in context_lines.
    Nothing is read again, and the prompt is never edited. The required turn
    epoch prevents cancelled pre-boundary context from restoring the cache.
    """
    with bridge._screen_privacy_ingress_lock:
        if (bridge.screen_privacy.restricted or not _game_active(bridge)
                or epoch is not None and not bridge.screen_privacy.current(epoch)):
            return
        _bridge_session(bridge)
        for who, text in re.findall(r'([^|:\n]*): "(.*?)" \(sumber:', facts, re.S)[-3:]:
            # The first quotation follows reply_context's provenance sentence;
            # subsequent quotations follow a pipe. Keep the native speaker too.
            who = who.rsplit("adegannya. ", 1)[-1].strip()
            remember_moment(who + ": " + text, time.time(), viewer_names=bridge._yt_viewers_seen)


def _bridge_session(bridge):
    import session_transcript
    global _SESSION
    session = session_transcript.current_session_id(bridge.CONFIG)
    with _LOCK:
        if _SESSION != session:
            _SESSION = session
            _VIEWERS.clear()
            _MOMENTS.clear()
        _VIEWERS.update(bridge._yt_viewers_seen)
    return session


def _game_active(bridge):
    runtime = getattr(bridge, "_stardew_runtime", None)
    active = getattr(runtime, "_live7_active", None)
    return (bridge._stardew_live() and (runtime is None or callable(active) and active())
            and not bridge._live7_game_session_stopped())


def bridge_passive(bridge, text):
    """History-only passive turns still require the actual W3 owner mark."""
    if bridge.CONFIG.get("mic_priority_mark") and bridge._mic_pasif_owner_marked(text):
        return bridge_line(bridge, "owner", "mic_pasif", text, owner_marked=True)
    return False


def owner_trigger_line(bridge, trigger):
    """``(text, at)`` of an owner trigger line that may become her said row, else None. ONE rule for both owner paths
    (ADR 233 voice-marked, ADR 232 answered): not a viewer's line, not a bridge-framed line (V8 frames the owner
    talking to a viewer: "[recorded remark omitted]"), with its own wall time, said after the latest
    privacy change."""
    text = getattr(trigger, "text", None)
    if getattr(trigger, "viewer_name", None) or not isinstance(text, str) or not text.strip():
        return None
    at = getattr(trigger, "queued_at", None)
    at = float(at) if type(at) in (int, float) and at > 0 else None
    changed = float(getattr(bridge.screen_privacy, "changed_at", 0.0) or 0.0)
    if text.lstrip().startswith("[") or at is None or at <= changed:
        return None
    return text, at


def bridge_answered(bridge, trigger, *, epoch=None):
    """ADR 232 part A: a passive-mic owner line that the mark left unmarked and that she answered aloud.

    On the mabar route (``mic_priority_mark`` on) the passive mic carries the owner's voice. The mark is a priority
    cue, not a gate (``mic_priority``), so an unmarked owner request could be answered by her voice and never reach
    her game page. Her answer shows that the line was said to her; the row then passes the same gate as every owner
    line. ``bridge_line`` calls it for her own fully
    voiced line, before writing it, with the trigger her turn answered (the bridge's existing ``bridge_delivered``
    hook remembers it), so the bridge itself is unchanged.
    """
    try:
        if (getattr(trigger, "trigger_type", None) != "mic_pasif" or getattr(trigger, "owner_marked", True) is not False
                or getattr(trigger, "viewer_name", None) or not bridge.CONFIG.get("mic_priority_mark")):
            return False
        line = owner_trigger_line(bridge, trigger)  # the same refusals as the voice-marked path (ADR 233)
        if line is None:
            return False
        text, at = line
        return bridge_line(bridge, "owner", "mic_pasif", text, owner_marked=True, epoch=epoch, at=at, marked_by="answered")
    except Exception:  # noqa: BLE001 - her voiced reply never fails over her game record
        return False


_TOLD = deque(maxlen=64)  # ADR 233: (session, viewer name, donation, wall seconds, line) of chat lines as they came in


def bridge_voice_marked(bridge, trigger):
    """ADR 233: her voice model marked her reply to ``trigger`` with the play tag; write the line she was answering.

    The turn's privacy epoch is the one ``bridge_delivered`` remembered for this same trigger; any other trigger, or
    none, writes nothing (no epoch is guessed). Owner speech (PTT; the passive mic on the mabar route, where it is
    the owner's own microphone: ``mic_priority_mark``) is the trigger's text with its own wall time. A viewer's chat
    or donation line is the line as it came in (``_TOLD``), found verbatim inside the trigger's text, which the
    bridge frames with its sender; its wall time is the one it came in with. A bridge-framed owner line (V8: the
    owner talking to a viewer) and every other trigger kind are not written. The row then passes the same gate as
    every line, with ``voice_marked`` as one more way through. Never raises into the reply path.
    """
    try:
        answering = _ANSWERING
        if answering is None or answering[0] != trigger:
            return False
        epoch, kind, name = answering[1], getattr(trigger, "trigger_type", None), getattr(trigger, "viewer_name", None)
        text = getattr(trigger, "text", None)
        if not isinstance(text, str) or not text.strip():
            return False
        at = getattr(trigger, "queued_at", None)
        at = float(at) if type(at) in (int, float) and at > 0 else None
        # A line said before the latest privacy change belongs to the revoked side of the boundary.
        changed = float(getattr(bridge.screen_privacy, "changed_at", 0.0) or 0.0)
        if kind == "ptt" or kind == "mic_pasif" and bridge.CONFIG.get("mic_priority_mark"):
            line = owner_trigger_line(bridge, trigger)
            if line is None:
                return False
            return bridge_line(bridge, "owner", kind, line[0], owner_marked=True, epoch=epoch, at=line[1],
                               marked_by=VOICE_MARKED)
        if kind not in ("yt_chat", "donation") or not display_name(name):
            return False
        import session_transcript
        session, donation = session_transcript.current_session_id(bridge.CONFIG), kind == "donation"
        since = (at or time.time()) - TOLD_SAME_S
        with _LOCK:
            lines = [(told_at, line) for s, who, gift, told_at, line in _TOLD
                     if s == session and who == display_name(name) and gift == donation and told_at >= since
                     and told_at > changed and line.strip() and line in text]
        written = [_bridge_viewer(bridge, name, line, donation=donation, marked_by=VOICE_MARKED, at=told_at,
                                  epoch=epoch) for told_at, line in lines]
        return any(written)
    except Exception:  # noqa: BLE001 - her voiced reply never fails over her game record
        return False


def bridge_line(bridge, speaker, ingress, text, *, owner_marked=False, epoch=None, at=None, marked_by=None):
    """``at``: the line's own wall time when it is written later than it was said (ADR 232: an answered line)."""
    global _ANSWERING
    if speaker == "arti" and ingress == "spoken_reply" and _ANSWERING is not None:
        answered, _ANSWERING = _ANSWERING, None
        bridge_answered(bridge, answered[0], epoch=answered[1])
    import arti_mind
    with bridge._screen_privacy_ingress_lock:
        if (bridge.screen_privacy.restricted or not _game_active(bridge)
                or epoch is not None and not bridge.screen_privacy.current(epoch)):
            return False
        session = _bridge_session(bridge)
        from arti_runtime.coop_session import owner_name
        owners = ("".join(re.findall(r"[^\W\d_]+", owner_name(bridge.CONFIG).casefold())) or OWNER_NAMES[0],)
        # ADR 230: a line naming a viewer is kept (it is her memory); the names it names ride for the training export.
        mentions = viewer_mentions(str(text), sorted(_VIEWERS), owners)
        now = time.time() if at is None else min(float(at), time.time())
        base = arti_mind.mind_dir(bridge.CONFIG)
        from arti_runtime import mind_scope
        current = (mind_scope.bridge_read_now(bridge) if mind_scope.enabled(bridge.CONFIG)
                   else arti_mind.read_now(base)) or {}
        with _LOCK:
            recent = [t for seen, t in _MOMENTS if 0 <= now - seen <= 180 and not contains_viewer(t, _VIEWERS, owners)]
        return append_inbox(base, dict(v=1, **stamp(now), session=session, speaker=speaker,
            ingress=ingress, text=str(text), owner_marked=owner_marked, game_active=True,
            game_time=current.get("game_time"), privacy_epoch=bridge.screen_privacy.epoch,
            recent_moments=recent, **({"mentions": mentions} if mentions else {}),
            **({"marked_by": marked_by} if marked_by else {})))


def bridge_viewer(bridge, viewer, text, *, donation=False):
    """A chat or donation line, admitted like owner speech (ADR 228); its teller's name rides to her record (ADR 230).

    Never raises: a chat or donation reply never fails over her game record.
    """
    try:
        return _bridge_viewer(bridge, viewer, text, donation=donation)
    except Exception:  # noqa: BLE001 - chat/donation ingress must keep working
        return False


def _bridge_viewer(bridge, viewer, text, *, donation=False, marked_by=None, at=None, epoch=None):
    """``marked_by``/``at``/``epoch`` (ADR 233 only): a line she marked, written again with the wall time it came in
    with, in her turn's epoch. The durable record limits (``viewer_allowed``) still apply to it; the inbox rate limit
    already counted the line when it came in."""
    import arti_mind
    with bridge._screen_privacy_ingress_lock:
        if bridge.screen_privacy.restricted or not _game_active(bridge) or not str(text or "").strip():
            return False
        if epoch is not None and not bridge.screen_privacy.current(epoch):
            return False
        session = _bridge_session(bridge)
        from arti_runtime.coop_session import owner_name
        owners = ("".join(re.findall(r"[^\W\d_]+", owner_name(bridge.CONFIG).casefold())) or OWNER_NAMES[0],)
        # ADR 230: a line naming any viewer, the speaker included, is kept (it is her memory); the names it
        # names ride as mentions so a training export can replace them. The rate limits stay keyed by the hash.
        mentions = viewer_mentions(str(text), sorted(_VIEWERS | {str(viewer or "")}), owners)
        speaker = "donasi" if donation else "viewer"
        tag = viewer_hash(viewer)
        now = time.time() if at is None else min(float(at), time.time())
        with _LOCK:
            if marked_by is None and bridge.CONFIG.get("live_voice_fixes_v1"):
                _TOLD.append((session, display_name(viewer), donation, now, str(text)))  # ADR 233
            while _CHAT_RECENT and now - _CHAT_RECENT[0] > CHAT_INBOX_WINDOW_S:
                _CHAT_RECENT.popleft()
            if marked_by is None and (len(_CHAT_RECENT) >= CHAT_INBOX_MAX
                    or 0 <= now - _CHAT_SENT.get((session, speaker, tag), -1e18) < VIEWER_INBOX_GAP_S):
                return False
            if marked_by is None:
                _CHAT_SENT[(session, speaker, tag)] = now
                _CHAT_RECENT.append(now)
            recent = [t for at, t in _MOMENTS if 0 <= now - at <= 180 and not contains_viewer(t, _VIEWERS, owners)]
        base = arti_mind.mind_dir(bridge.CONFIG)
        from arti_runtime import mind_scope
        current = (mind_scope.bridge_read_now(bridge) if mind_scope.enabled(bridge.CONFIG)
                   else arti_mind.read_now(base)) or {}
        return append_inbox(base, dict(v=1, **stamp(now), session=session, speaker=speaker,
            ingress=VIEWER_SOURCES[speaker], text=str(text), viewer=tag, game_active=True,
            game_time=current.get("game_time"), privacy_epoch=bridge.screen_privacy.epoch,
            recent_moments=recent, **({"viewer_name": display_name(viewer)} if display_name(viewer) else {}),
            **({"mentions": mentions} if mentions else {}), **({"marked_by": marked_by} if marked_by else {})))


def _said_key(text):
    return " ".join(str(text or "").removesuffix("…").split())[:40].casefold()


def drop_help_duplicates(offer, help_rows, help_lines):
    """ADR 232 part A: a line her page already carries as a said row (``mind.dikatakan``, the one store) is not shown
    again by ADR 178's memory-only help channel (``game_facts`` "Dikatakan ke kamu soal game"). Same words: the first
    40 characters, whitespace collapsed, case folded (that channel keeps 140 characters, a said row may be shortened)."""
    said = {_said_key(r.get("text")) for r in ((offer.get("mind") or {}).get("dikatakan") or ())
            if isinstance(r, dict) and r.get("text")}
    facts = offer.get("game_facts")
    if not said or not isinstance(facts, list) or not isinstance(help_lines, dict):
        return 0
    same = {help_lines.get(r.get("ref")) for r in help_rows or () if isinstance(r, dict)
            and _said_key(r.get("text")) in said}
    same.discard(None)
    kept = [line for line in facts if line not in same]
    offer["game_facts"] = kept
    return len(facts) - len(kept)


def live7_ingest(runtime, scope, page):
    """Game-side adapter, called once with this decision's complete page."""
    base = runtime._live7_mind_base()
    if base is None or not scope or not runtime._live7_active() or not _path(base).exists():
        return {}
    import arti_mind
    # Reuse the game's native journal, not a voice-side prediction. native_vitals
    # at <= the utterance is authoritative, including before this decision tick.
    current = runtime._journal_path()
    paths = sorted(current.parent.parent.parent.glob("day-*/journal/g1_journal.jsonl")) if current.parent.name == "journal" else []
    if current not in paths:
        paths.append(current)
    # Incremental reads keep the decision path bounded by new journal rows.
    cache = getattr(runtime, "_stream_notes_observations", None)
    if cache is None or cache["scope"] != scope:
        cache = dict(scope=scope, offsets={}, observations=[])
        runtime._stream_notes_observations = cache
    for path in paths:
        if not path.exists():
            continue
        offset = cache["offsets"].get(path, 0)
        if path.stat().st_size < offset:
            offset = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            new = []
            while raw := handle.readline():
                if not raw.endswith(b"\n"):
                    break  # an in-flight journal append is retried next tick
                cache["offsets"][path] = handle.tell()
                try:
                    new.append(json.loads(raw.decode("utf-8-sig", errors="replace")))
                except ValueError:
                    continue
        for row in new:
            if not isinstance(row, dict):
                continue
            game = row.get("game") or {}
            if row.get("ev") == "native_vitals" and isinstance(row.get("at"), (int, float)):
                try:
                    cache["observations"].append((row["at"], dict(day=game_day(game.get("date")),
                        clock=game.get("time"), place=game.get("location"))))
                except ValueError:
                    pass
    observations = sorted(cache["observations"], key=lambda r: r[0])
    def observe_at(at):
        return next((obs for ts, obs in reversed(observations) if ts <= at), None)
    rows = _rows(arti_mind.game_dir(base, "stardew") / "discoveries.jsonl")
    # Game-origin text only: the stream notes this module writes (source "stream:...") never feed the gate.
    vocabulary = [value for row in rows if row.get("scope_hash") == arti_mind._scope_hash(scope)
                  and not str(row.get("source", "")).startswith("stream:")
                  for value in (row.get("subject", ""), row.get("text", ""))]
    # ADR 219's text_read receipts point to delivered chunks in ledger.jsonl,
    # rather than discoveries text owners. Reuse its exact-text accessor so
    # the gate knows the native words she actually received on current main.
    from arti_runtime.game_text_delivery import read_text
    for row in rows:
        ref = (row.get("receipt") or {}).get("text_ref", "")
        if row.get("scope_hash") == arti_mind._scope_hash(scope) and ref.startswith("game_text:"):
            native = read_text(base, scope, ref)
            if native:
                vocabulary.append(native["text"])
    from arti_runtime import mind_scope
    return ingest(base, scope, observe_at, dict(game=vocabulary, page=list(strings(page))),
                  outcomes=getattr(runtime, "_rig_said_outcomes", None),
                  mind_scoped=mind_scope.enabled(getattr(runtime, "_config", None)))  # set only by scripts/free_play/said_lines
