"""Wake-word detection — single source of truth for Arti call detection."""

from __future__ import annotations

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
        "artis",
        "art",
        "arbi",
        "ardi",
        "arty",
        "atty",
        "ati",
        "atih",
        "rt",
        "ert",
        "erti",
        "erte",
        "aarti",
        "rty",
        "at",
        "boyarti",
    }
)

_ART_START_FALSE: frozenset[str] = frozenset(
    {"serta", "kertas", "harta", "warta", "pertama", "pergi", "perlu", "pernah"}
)


def _tokenize(text: str) -> list[str]:
    return [w.strip(".,!?\"'()[]") for w in text.lower().split() if w.strip(".,!?\"'()[]")]


def is_arti_wake_call(text: str) -> bool:
    """Return True when text is a genuine call to Arti (not e.g. ``berarti``)."""
    if not (text or "").strip():
        return False
    for word in _tokenize(text):
        if word in WAKE_FALSE_POSITIVES:
            continue
        if word in WAKE_KEYWORDS:
            return True
        if any(sub in word for sub in ("arti", "erti", "rt", "arte")) and len(word) <= 8:
            return True
        if (word.startswith("art") or word.startswith("ert") or word.startswith("rt")) and len(
            word
        ) <= 6:
            if word in _ART_START_FALSE:
                continue
            return True
    return False
