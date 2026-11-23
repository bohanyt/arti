"""Passive-mic hygiene for a live stream (solo stream 2026-11-15, CT decision on W1).

CONFIG["passive_mic_hygiene"] (default False) leaves today's path unchanged. mabar turns it on.
When on:
- a whole transcript that is a known Whisper bleed (the old vocabulary prompt, outro phrases) is noise;
- during a mediashare hold a passive mic line goes to history only, unless it names her.
Vocabulary-list filtering applies only to passive mic transcripts.
Which lines are addressed to her is a priority question and lives elsewhere.
"""
from __future__ import annotations

import re

# Whole-transcript prompt-bleed / Whisper outro (punctuation stripped).
# Single tokens (game, nama, amen, book) match only the entire transcript. "Tidur." is a real thing
# the owner says to her in this game, so it is never treated as bleed.
_HALLUCINATION_EXACT = frozenset({
    "bahasa indonesia",
    "nama indonesia",
    "nama arti",
    "sampai jumpa di video selanjutnya",
    "amen",
    "book",
    "game",
    "nama",
    "bahasa indonesia nama arti bohan game stardew valley",
})

# Multi-word bleed may be the whole line or padded with the rest of the prompt.
_HALLUCINATION_PREFIXES = (
    "bahasa indonesia",
    "nama indonesia",
)
_HALLUCINATION_PHRASES = (
    "sampai jumpa di video selanjutnya",
)
# Vocab list "Nama: Arti, …" / "Nama, Arti, …" — not "kamu kasih nama Arti".
_HALLUCINATION_NAME_LIST = re.compile(r"\bnama\s*[:,]\s*arti\b", re.I)


def normalize_transcript(text: str) -> str:
    return re.sub(r"[^\w\s]", "", str(text).lower()).strip()


def has_question_mark(text: str) -> bool:
    return "?" in text or "？" in text


def is_prompt_echo(text: str, prompt: str) -> bool:
    """A passive vocabulary echo includes both prompted names and has no question mark."""
    if has_question_mark(text):
        return False
    words = set(re.findall(r"\w+", text.casefold()))
    terms = [re.findall(r"\w+", term.casefold()) for term in prompt.split(",")]
    prompt_words = {word for term in terms for word in term}
    names = {term[0] for term in terms if len(term) == 1}
    return len(names) >= 2 and names <= words <= prompt_words


def is_hallucination_transcript(text: str, *, prompt: str = "") -> bool:
    """True when the whole transcript is a listed Whisper bleed, not a real sentence."""
    raw = str(text or "").strip()
    if not raw or has_question_mark(raw):
        return False
    if prompt and is_prompt_echo(raw, prompt):
        return True
    t = normalize_transcript(raw)
    if not t:
        return False
    if t in _HALLUCINATION_EXACT:
        return True
    words = t.split()
    if any(t == p or t.startswith(p + " ") for p in _HALLUCINATION_PREFIXES) and len(words) <= 6:
        return True
    if any(p in t for p in _HALLUCINATION_PHRASES):
        return True
    if _HALLUCINATION_NAME_LIST.search(raw) and len(words) <= 8:
        return True
    # Whisper listing "Nama Arti Bohan" / "[recorded remark omitted]" with no comma.
    if re.match(r"nama a+r+t+[iy]+(?: bohan)?(?: game)?(?: stardew)?(?: valley)?$", t) and len(words) <= 8:
        return True
    return False


def passive_history_only_reason(*, named: bool, now: float, media_until: float) -> str | None:
    """None = the passive line goes on as today; "mediashare" = history only while a clip plays."""
    if named:
        return None
    try:
        until = float(media_until or 0.0)
        return "mediashare" if until > 0 and float(now) < until else None
    except (TypeError, ValueError):
        return None
