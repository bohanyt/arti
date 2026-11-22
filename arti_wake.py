"""Wake-word detection — single source of truth for Arti call detection."""

from __future__ import annotations

import re

WAKE_FALSE_POSITIVES: frozenset[str] = frozenset(
    {
        "seperti",
        "berarti",
        "artinya",
        "mengartikan",
        "kategori",
        "parti",
        "partai",
        "kartu",
        "harta",
        "serta",
        "kertas",
        "warta",
        "pertama",
        "pergi",
        "perlu",
        "pernah",
    }
)

WAKE_KEYWORDS: frozenset[str] = frozenset(
    {
        "arti",
        "artik",
        "art",
        "arbi",
        "ardi",
        "arty",
        "atty",
        "ati",
        "atih",
        "ert",
        "erti",
        "erte",
        "aarti",
        "rty",
        "boyarti",
    }
)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def is_arti_wake_call(text: str) -> bool:
    """Return True when text is a genuine call to Arti (not e.g. ``berarti``)."""
    if not (text or "").strip():
        return False
    words = _tokenize(text)
    if any(pair == ("ar", "ti") for pair in zip(words, words[1:])):
        return True
    for word in words:
        if word in WAKE_FALSE_POSITIVES:
            continue
        if word in WAKE_KEYWORDS:
            return True
        # Only a whole elongated name is a call, never a substring in e.g.
        # ngerti / Jakarta / party. Keep the explicit ASR aliases above.
        if re.fullmatch(r"a+r+t+[iy]+", word):
            return True
    return False
