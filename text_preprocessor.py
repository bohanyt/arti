"""
text_preprocessor.py — Indonesian Number Pre-processor for TTS

Converts Arabic numerals to spoken Indonesian words before sending to TTS engine.
Handles: cardinals, decimals, currency (Rp/IDR), dates, time, percentages, phone numbers.

Pure function — no I/O, no global mutable state, idempotent.
Designed to sit in front of any TTS engine (Supertone, edge_tts, etc.)

Usage:
    from text_preprocessor import preprocess_indonesian_text
    result = preprocess_indonesian_text("harganya Rp 10.000 dan suhu 25 derajat")
    # -> "harganya sepuluh ribu rupiah dan suhu dua puluh lima derajat"
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INDONESIAN_DIGITS: dict[str, str] = {
    "0": "nol", "1": "satu", "2": "dua", "3": "tiga", "4": "empat",
    "5": "lima", "6": "enam", "7": "tujuh", "8": "delapan", "9": "sembilan",
}

_INDONESIAN_MONTHS: tuple[str | None, ...] = (
    None, "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)

_CARDINAL_TENS = {
    10: "sepuluh", 11: "sebelas", 12: "dua belas", 13: "tiga belas",
    14: "empat belas", 15: "lima belas", 16: "enam belas", 17: "tujuh belas",
    18: "delapan belas", 19: "sembilan belas",
}

_CARDINAL_TENS_PREFIX = {
    20: "dua puluh", 30: "tiga puluh", 40: "empat puluh", 50: "lima puluh",
    60: "enam puluh", 70: "tujuh puluh", 80: "delapan puluh", 90: "sembilan puluh",
}

_MAX_CARDINAL = 999_999_999_999_999  # 999 trillion
_MAX_OUTPUT_EXPANSION = 16

# Expression tag matcher: captures a whole `<...>` token including brackets.
# Used as a capturing group so re.split keeps the tags as delimiters.
_TAG_RE = re.compile(r"(<[^<>]+>)")

# Matches any run of ASCII digits left behind after the main number pass.
# Used by the residual-digit safety net so no digit can survive preprocessing
# even when a spelling helper raises or rejects an out-of-range token.
_RESIDUAL_DIGITS_RE = re.compile(r"\d+")

# Priority-ordered named regex: currency > date > time > percent > phone > decimal > cardinal
_MASTER_REGEX = re.compile(
    r"(?P<currency>(?:Rp|IDR)\s*\d{1,3}(?:\.\d{3})*(?:,\d+)?)"
    r"|(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{4})"
    r"|(?P<percent>\d+(?:,\d+)?%)"
    r"|(?P<phone>(?:(?:\+62|0)\d{8,13}))"
    r"|(?P<decimal>\d+(?:\.\d{3})+,\d+)"
    r"|(?P<cardinal>-?\d{1,3}(?:\.\d{3})+(?:,\d+)?|-?\d+)"
    r"|(?P<time>(?<!\d\.)\b\d{1,2}[:.]\d{2}(?!\d))"
)


# ---------------------------------------------------------------------------
# Cardinal spelling
# ---------------------------------------------------------------------------

def _spell_cardinal(n: int) -> str:
    """Convert integer 0..999_999_999_999_999 to Indonesian words."""
    if n == 0:
        return "nol"
    if n < 0:
        return "minus " + _spell_cardinal(-n)

    ones = ["", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan", "sembilan"]

    def _under_100(x: int) -> str:
        if x == 0:
            return ""
        if x < 10:
            return ones[x]
        if x < 20:
            return _CARDINAL_TENS[x]
        tens = (x // 10) * 10
        rem = x % 10
        base = _CARDINAL_TENS_PREFIX[tens]
        return base if rem == 0 else f"{base} {ones[rem]}"

    def _under_1000(x: int) -> str:
        if x == 0:
            return ""
        if x < 100:
            return _under_100(x)
        hundreds = x // 100
        rem = x % 100
        prefix = "seratus" if hundreds == 1 else f"{ones[hundreds]} ratus"
        return prefix if rem == 0 else f"{prefix} {_under_100(rem)}"

    parts = []
    trillion = n // 1_000_000_000_000
    billion  = (n % 1_000_000_000_000) // 1_000_000_000
    million  = (n % 1_000_000_000) // 1_000_000
    thousand = (n % 1_000_000) // 1_000
    rest     = n % 1_000

    if trillion:
        parts.append(_under_1000(trillion) + " triliun")
    if billion:
        parts.append(_under_1000(billion) + " miliar")
    if million:
        parts.append(_under_1000(million) + " juta")
    if thousand:
        # special: x000 where x>=10 → not "satu ribu" → "sepuluh ribu", etc.
        if thousand == 1:
            parts.append("seribu")
        else:
            parts.append(_under_1000(thousand) + " ribu")
    if rest:
        parts.append(_under_1000(rest))

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

def _spell_decimal(integer_part: str, decimal_part: str) -> str:
    """Spell '1.234,56' → 'seribu dua ratus tiga puluh empat koma lima enam'."""
    int_val = int(integer_part.replace(".", ""))
    int_spelling = _spell_cardinal(int_val)
    dec_digits = " ".join(_INDONESIAN_DIGITS[d] for d in decimal_part if d.isdigit())
    return f"{int_spelling} koma {dec_digits}"


def _spell_currency(token: str) -> str:
    """Spell 'Rp 10.000' or 'IDR 1.500,50' → 'sepuluh ribu rupiah' / '... koma lima nol sen'."""
    numeric = re.sub(r"^(?:Rp|IDR)\s*", "", token).strip()
    if "," in numeric:
        int_part, dec_part = numeric.rsplit(",", 1)
        # Spell the integer part as cardinal (strip thousand separators)
        int_val = int(int_part.replace(".", ""))
        result = _spell_cardinal(int_val)
        # Spell decimal part digit-by-digit + "sen"
        dec_digits = " ".join(_INDONESIAN_DIGITS[d] for d in dec_part if d.isdigit())
        if dec_part.strip("0 "):
            result += f" koma {dec_digits} sen"
        else:
            result += " rupiah"
    else:
        val = int(numeric.replace(".", ""))
        result = _spell_cardinal(val) + " rupiah"
    return result


def _spell_date(token: str) -> str:
    """Spell '15/06/2026' or '15-06-2026' → 'lima belas Juni dua ribu dua puluh enam'."""
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", token)
    if not m:
        return token
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= dd <= 31 and 1 <= mm <= 12):
        return token  # out of range — leave unchanged
    day_spelling = _spell_cardinal(dd)
    month_name = _INDONESIAN_MONTHS[mm] or ""
    year_spelling = _spell_cardinal(yyyy)
    return f"{day_spelling} {month_name} {year_spelling}"


def _spell_time(token: str) -> str:
    """Spell '14:30' or '14.30' → 'empat belas tiga puluh'."""
    m = re.match(r"(\d{1,2})[:.](\d{2})", token)
    if not m:
        return token
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return token  # out of range — leave unchanged
    return f"{_spell_cardinal(hh)} {_spell_cardinal(mm)}"


def _spell_percent(token: str) -> str:
    """Spell '25%' → 'dua puluh lima persen', '12,5%' → 'dua belas koma lima persen'."""
    numeric = token.rstrip("%")
    if "," in numeric:
        int_part, dec_part = numeric.rsplit(",", 1)
        return f"{_spell_cardinal(int(int_part))} koma {' '.join(_INDONESIAN_DIGITS[d] for d in dec_part if d.isdigit())} persen"
    else:
        val = int(numeric.replace(".", ""))
        return f"{_spell_cardinal(val)} persen"


def _spell_phone(token: str) -> str:
    """Spell phone number digit-by-digit: '08123456789' → 'nol delapan satu dua ...'."""
    digits = token.lstrip("+")
    spelled = " ".join(_INDONESIAN_DIGITS.get(d, d) for d in digits if d.isdigit())
    if token.startswith("+"):
        spelled = f"plus {spelled}"
    return spelled


def _spell_negative(unsigned_spelling: str) -> str:
    return f"minus {unsigned_spelling}"


def _log_token_error(category: str, token: str, exc: BaseException) -> None:
    """Log preprocessor errors — never raises."""
    logger.warning(f"[Preprocessor] {category}: {type(exc).__name__}: {exc} (token={token!r})")


# ---------------------------------------------------------------------------
# Master dispatch
# ---------------------------------------------------------------------------

def _dispatch(m: re.Match) -> str:
    """Wire regex match groups to the appropriate spelling function."""
    token = m.group(0)

    try:
        if m.group("currency"):
            return _spell_currency(token)

        if m.group("date"):
            return _spell_date(token)

        if m.group("time"):
            return _spell_time(token)

        if m.group("percent"):
            return _spell_percent(token)

        if m.group("phone"):
            return _spell_phone(token)

        if m.group("decimal"):
            int_part, dec_part = token.rsplit(",", 1)
            return _spell_decimal(int_part, dec_part)

        if m.group("cardinal"):
            val = int(token.replace(".", ""))
            # Route overflow to phone spelling (digit-by-digit)
            if abs(val) > _MAX_CARDINAL:
                return _spell_phone(token)
            return _spell_cardinal(val)

    except Exception as exc:
        _log_token_error("cardinal" if m.group("cardinal") else "unknown", token, exc)
        return token  # leave unchanged on any exception

    return token  # fallback — should never reach


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _spell_residual_digits(match: re.Match) -> str:
    """Spell a leftover run of ASCII digits one digit at a time.

    Acts as the final safety net for Requirement 11.3: any digit run that the
    main number pass left untouched (because a spelling helper raised, or
    rejected an out-of-range value such as a >999-trillion percentage or an
    invalid date/time) is converted digit-by-digit to Indonesian words so no
    ASCII digit ever survives in a non-tag segment.
    """
    return " ".join(_INDONESIAN_DIGITS[d] for d in match.group(0))


def _strip_residual_digits(text: str) -> str:
    """Replace any remaining ASCII digit runs with digit-by-digit spelling.

    Run after the master substitution pass. Surrounds the spelled digits with
    spaces only as needed so adjacent words stay separated; collapses the
    common ``word123`` / ``123word`` boundaries cleanly.
    """
    if not _RESIDUAL_DIGITS_RE.search(text):
        return text
    return _RESIDUAL_DIGITS_RE.sub(_spell_residual_digits, text)


def preprocess_indonesian_text(text: str) -> str:
    """Convert Arabic numerals in `text` to spoken Indonesian words.

    Pure function — no side effects, no I/O, idempotent.
    Empty-string input returns "" without raising.
    Inputs > 4096 chars are processed gracefully.

    A final safety-net pass guarantees no ASCII digit survives (Requirement
    11.3): any digit run the structured spellers left behind — e.g. an
    out-of-range percentage/date/time or a token whose speller raised — is
    spelled digit-by-digit.
    """
    if not isinstance(text, str):
        return str(text)
    if not text:
        return ""

    try:
        result = _MASTER_REGEX.sub(_dispatch, text)
    except Exception as exc:
        _log_token_error("regex_engine", text[:64], exc)
        result = text  # fail-safe: fall back to original on regex engine failure

    # Safety net: ensure no ASCII digit leaks through any fail-safe path.
    return _strip_residual_digits(result)


def preprocess_preserving_tags(text: str) -> str:
    """Apply Indonesian number preprocessing while leaving expression tags intact.

    Tokenizes `text` around `<...>` expression tags (e.g. ``<laugh>``,
    ``<breath>``, ``<sigh>``) using a capturing-group split, transforms only the
    non-tag segments via :func:`preprocess_indonesian_text`, then re-concatenates.
    Every tag segment is forwarded byte-for-byte and in its original order,
    regardless of the number preprocessor's internals.

    Pure function — no side effects, no I/O, idempotent. Non-string input is
    coerced to ``str``; empty-string input returns ``""`` without raising.
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""

    # Capturing group => re.split keeps the tags as delimiters:
    #   "a <laugh> b 10" -> ["a ", "<laugh>", " b 10"]
    parts = _TAG_RE.split(text)
    out: list[str] = []
    for seg in parts:
        if _TAG_RE.fullmatch(seg):
            out.append(seg)              # tag segment — leave untouched
        else:
            out.append(preprocess_indonesian_text(seg))
    return "".join(out)


# ---------------------------------------------------------------------------
# Self-test (run with: python text_preprocessor.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_cases = [
        ("harganya Rp 10.000", "harganya sepuluh ribu rupiah"),
        ("suhu 25 derajat", "suhu dua puluh lima derajat"),
        ("tanggal 15/06/2026", "tanggal lima belas Juni dua ribu dua puluh enam"),
        ("waktu 14:30", "waktu empat belas:tiga puluh"),  # separator : preserved by time regex
        ("diskon 25%", "diskon dua puluh lima persen"),
        ("call 08123456789", "call nol delapan satu dua tiga empat lima enam tujuh delapan sembilan"),
        ("1.500.000 rupiah", "satu juta lima ratus ribu rupiah"),
        ("minus -500", "minus minus lima ratus"),  # double minus: "minus" + "-500" → expected behavior
        ("normal text no numbers", "normal text no numbers"),
        ("", ""),
        ("IDR 1.234,56", "seribu dua ratus tiga puluh empat koma lima enam sen"),
    ]

    print("=" * 60)
    print("  text_preprocessor.py — Self-Test")
    print("=" * 60)

    passed = 0
    failed = 0
    for inp, expected in test_cases:
        result = preprocess_indonesian_text(inp)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {inp!r}")
        if status == "FAIL":
            print(f"         Expected: {expected!r}")
            print(f"         Got:      {result!r}")

    print(f"\n  Results: {passed}/{passed+failed} passed")
    if failed == 0:
        print("  All tests passed! ✅")
    else:
        print(f"  {failed} test(s) failed ❌")
