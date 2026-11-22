"""Passive-mic priority mark (VOICE_CHAT_ANALYSIS change 2). Not a gate.

CONFIG["mic_priority_mark"] (default False) leaves today's path unchanged. mabar turns it on.
When on, a mic_pasif line is marked when it is vocative / second-person / a request opener /
a follow-up after her own question. Unmarked lines still reach the model, but they must not
outrank or age viewer chat. Owner-direct kinds (mic / ptt / wake_word) are never marked here.
"""
from __future__ import annotations

import re

from arti_wake import WAKE_FALSE_POSITIVES, WAKE_KEYWORDS

AFTER_QUESTION_SEC = 10.0

_SECOND_PERSON_PRONOUNS = re.compile(
    r"(?<!\w)(?:kamu|lu|lo|you)(?!\w)", re.I,
)
_SECOND_PERSON_ASK = re.compile(
    r"(?<!\w)(?:"
    r"apa|apaan|apasi|kenapa|mengapa|kok|gimana|bagaimana|"
    r"tolong|coba|jangan|baca|jawab|dengar|denger|dengerin|lihat|liat|"
    r"bales|balas|bilang|cek|diam|diem|tunggu|ingat|inget|kasih|kasi|"
    r"please|what|why|how|can|could|would|will|tell|answer|look|read|"
    r"listen|stop|wait|dont|"
    r"gak|nggak|ga|ngga|tidak|kan"
    r")(?!\w)",
    re.I,
)

_REQUEST_OPENERS = frozenset((
    "baca", "bacain", "jawab", "jawabin", "lihat", "liat", "coba", "cobain", "ayo", "yuk", "tolong",
    "bilang", "cek", "tunggu", "diam", "diem", "jangan", "pergi", "balik", "pulang", "tidur", "ambil",
    "jual", "beli", "kasih", "sapa", "bales", "balas", "lanjut", "lanjutin", "stop", "berhenti", "mancing",
    "main", "masuk", "keluar", "simpan", "taruh",
))
_QUESTION_OPENERS = frozenset((
    "apa", "apaan", "kenapa", "kok", "gimana", "bagaimana", "kapan", "siapa", "dimana", "mana", "berapa",
))

_INTERJECTIONS = frozenset((
    "eh", "ehh", "wait", "halo", "hallo", "hai", "hei", "hey", "oi", "woi", "yo", "oke", "ok", "okay",
    "nah", "ya", "yah", "salam", "heh", "aduh", "wah", "lho", "loh", "nih", "tuh", "sayang", "hmm",
    "em", "udah",
))
_PARTICLES = frozenset((
    "dong", "ya", "yah", "sih", "deh", "nih", "lah", "kok", "kan", "yuk", "ayo", "tuh", "hei",
))
_SENTENCE_SPLIT = re.compile(r"[.?!]+")
_NAME_ELONGATED = re.compile(r"a+r+t+[iy]+")


def normalize_transcript(text: str) -> str:
    return re.sub(r"[^\w\s]", "", str(text).lower()).strip()


def is_request_to_her(text: str) -> bool:
    words = normalize_transcript(text).split()
    return bool(words) and words[0] in _REQUEST_OPENERS


def is_question(text: str) -> bool:
    raw = str(text or "").strip()
    words = normalize_transcript(raw).split()
    return raw.endswith("?") or (bool(words) and words[0] in _QUESTION_OPENERS)


def is_second_person_to_her(text: str) -> bool:
    raw = str(text or "")
    if not _SECOND_PERSON_PRONOUNS.search(raw):
        return False
    if re.search(r"(?<!\w)kamu(?!\w)", raw, re.I):
        return True
    if "?" in raw:
        return True
    return _SECOND_PERSON_ASK.search(raw) is not None


def is_after_own_question(
    last_reply: str,
    last_reply_end_ts: float,
    now: float,
    window_sec: float = AFTER_QUESTION_SEC,
) -> bool:
    reply = str(last_reply or "").strip()
    if not reply.endswith("?"):
        return False
    try:
        end = float(last_reply_end_ts or 0.0)
    except (TypeError, ValueError):
        return False
    if end <= 0:
        return False
    dt = float(now) - end
    return 0 <= dt <= float(window_sec)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", str(text or "").lower())


def _name_spans(words: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in WAKE_FALSE_POSITIVES:
            i += 1
            continue
        if i + 1 < len(words) and word == "ar" and words[i + 1] == "ti":
            spans.append((i, i + 2))
            i += 2
            continue
        if word in WAKE_KEYWORDS or _NAME_ELONGATED.fullmatch(word):
            spans.append((i, i + 1))
        i += 1
    return spans


def _clauses(text: str) -> list[list[str]]:
    """Sentences, and the comma-delimited clauses inside them, as word lists (2026-11-17 V8)."""
    out = []
    for sentence in _SENTENCE_SPLIT.split(str(text or "")):
        for clause in [sentence, *_CLAUSE_SPLIT.split(sentence)]:
            words = _tokenize(clause)
            if words:
                out.append(words)
    return out


_CLAUSE_SPLIT = re.compile(r"[,;:]+")


def is_vocative(text: str) -> bool:
    """True when a name token is a call to her, not a third-person mention.

    A name standing alone between commas ("Eh, Arti, udah mulai") is a call as well (2026-11-17 V8)."""
    for words in _clauses(text):
        for start, end in _name_spans(words):
            before, after = words[:start], words[end:]
            clean_before = all(w in _INTERJECTIONS for w in before)
            clean_after = bool(after) and all(w in _PARTICLES for w in after)
            if clean_before or clean_after:
                return True
    return False


_VIEWER_TOKEN = re.compile(r"[^a-z]")


def addressed_viewer(text: str, viewers) -> str | None:
    """The chat viewer a passive-mic line is said to, when the owner calls one by (the start of) their name.

    A call is a clause that is only the name, with interjections before or particles after it ("Hai, Seka.",
    "Nanti dicatat ya, Seka."). Three letters at least; never when the line also calls her. Livestream test
    2026-11-17 (V8): such lines reached her as plain owner speech. This names who; it gates nothing."""
    if not viewers or is_vocative(text):
        return None
    handles = []
    for viewer in viewers:
        key = _VIEWER_TOKEN.sub("", str(viewer or "").lower())
        if key:
            handles.append((key, viewer))
    for words in _clauses(text):
        for i, word in enumerate(words):
            if len(word) < 3 or not word.isalpha():
                continue
            before, after = words[:i], words[i + 1:]
            if not (all(w in _INTERJECTIONS for w in before) and all(w in _PARTICLES for w in after)):
                continue
            if not before and not after and len(words) == 1 and word in _INTERJECTIONS:
                continue
            hits = [viewer for key, viewer in handles if key.startswith(word)]
            if len(hits) == 1:
                return hits[0]
    return None


def mark(text, *, last_reply, last_reply_end_ts, now) -> tuple[bool, str]:
    """Return (marked, reason). Unmarked reason is empty."""
    if is_vocative(text):
        return True, "vocative"
    if is_second_person_to_her(text):
        return True, "second_person"
    if is_after_own_question(last_reply, last_reply_end_ts, now):
        return True, "after_question"
    if is_request_to_her(text):
        return True, "request"
    try:
        spoke_recently = 0 <= float(now) - float(last_reply_end_ts or 0.0) <= AFTER_QUESTION_SEC
    except (TypeError, ValueError):
        spoke_recently = False
    if spoke_recently and float(last_reply_end_ts or 0.0) > 0 and is_question(text):
        return True, "question_after_her"
    return False, ""
