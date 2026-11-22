"""Pagar keluaran kata kasar untuk suara dan permukaan publik Arti.

Transformasi ini sengaja murni dan lokal: tidak ada request model, I/O, atau
audio tambahan. Dengan begitu biayanya hanya satu substitusi regex sebelum TTS.
"""

from __future__ import annotations

from functools import lru_cache
import re
from typing import Iterable, Mapping


DEFAULT_BLOCKED_WORDS = (
    "tai",
    "bangsat",
    "bajingan",
    "brengsek",
    "goblok",
    "tolol",
    "keparat",
    "kontol",
    "memek",
    "ngentot",
    "jancuk",
    "fuck",
    "fucking",
    "shit",
    "bitch",
    "asshole",
    "motherfucker",
)

_INDONESIAN_SUFFIXES = ("nya", "mu", "ku", "lah")


def _clean_words(words: Iterable[object]) -> tuple[str, ...]:
    unique = {
        str(word).strip().casefold()
        for word in words
        if str(word).strip()
    }
    return tuple(sorted(unique, key=lambda word: (-len(word), word)))


@lru_cache(maxsize=32)
def _pattern(words: tuple[str, ...]) -> re.Pattern[str] | None:
    if not words:
        return None
    alternatives = []
    for word in words:
        # Spasi di frasa config boleh ditulis sekali, sementara keluaran model
        # kadang punya lebih dari satu whitespace.
        alternatives.append(re.escape(word).replace(r"\ ", r"\s+"))
    suffixes = "|".join(map(re.escape, _INDONESIAN_SUFFIXES))
    return re.compile(
        rf"(?<!\w)(?:{'|'.join(alternatives)})(?P<suffix>{suffixes})?(?!\w)",
        re.IGNORECASE,
    )


def censor_text(
    text: str,
    *,
    words: Iterable[object] | None = None,
    replacement: str = "sensor",
) -> str:
    """Ganti token terlarang tanpa mengenai substring seperti ``santai``."""
    if not text:
        return text
    selected = _clean_words(DEFAULT_BLOCKED_WORDS if words is None else words)
    pattern = _pattern(selected)
    if pattern is None:
        return text
    safe_replacement = str(replacement).strip() or "sensor"
    return pattern.sub(
        lambda match: safe_replacement + (match.group("suffix") or ""),
        text,
    )


def censor_from_config(text: str, config: Mapping[str, object] | None) -> str:
    """Terapkan sensor hanya jika kill switch lokal dinyalakan."""
    cfg = config or {}
    if not bool(cfg.get("speech_censor_enabled", False)):
        return text
    words = cfg.get("speech_censor_words")
    if not isinstance(words, (list, tuple, set)):
        words = DEFAULT_BLOCKED_WORDS
    return censor_text(
        text,
        words=words,
        replacement=str(cfg.get("speech_censor_replacement", "sensor")),
    )
