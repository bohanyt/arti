"""Unit tests for text_preprocessor edge cases.

Concrete example-based tests for the tag-preserving Indonesian number
preprocessor used by the Supertone 3 TTS upgrade. These complement the
Hypothesis property tests in ``test_text_preprocessor_properties.py`` by
pinning down specific, human-readable examples and known edge cases.

Covers:
  * Mixed expression tags + numbers (tags verbatim, numbers expanded).
  * No-digit passthrough (non-tag text without digits reproduced unchanged).
  * Empty text -> "".
  * Adjacent tags preserved verbatim and in order.
  * A tag immediately adjacent to a number.
  * Previously-leaking inputs (out-of-range percentage / invalid date) now
    leave no ASCII digit in the non-tag output, thanks to the residual-digit
    safety net.

Validates: Requirements 11.4, 12.3
"""

from __future__ import annotations

import re

from text_preprocessor import preprocess_preserving_tags, preprocess_indonesian_text

# Same matcher the preprocessor uses to identify a whole `<...>` tag token.
_EXTRACT_RE = re.compile(r"<[^<>]+>")
_DIGIT_RE = re.compile(r"[0-9]")


def extract_tags(s: str) -> list[str]:
    """Return the ordered sequence of `<...>` tag tokens found in ``s``."""
    return _EXTRACT_RE.findall(s)


def non_tag_text(s: str) -> str:
    """Strip every `<...>` tag token, leaving only the non-tag segments."""
    return _EXTRACT_RE.sub("", s)


# ---------------------------------------------------------------------------
# Mixed expression tags + numbers
#   Tags forwarded byte-identical and in order; numbers outside tags expanded
#   to Indonesian words. (Requirements 11.4, 12.3)
# ---------------------------------------------------------------------------


def test_mixed_tags_and_numbers():
    text = "<laugh> harganya Rp 10.000 <sigh>"

    result = preprocess_preserving_tags(text)

    # Tags survive verbatim and in their original left-to-right order.
    assert extract_tags(result) == ["<laugh>", "<sigh>"]
    # The currency number is expanded to Indonesian words.
    assert result == "<laugh> harganya sepuluh ribu rupiah <sigh>"
    # No ASCII digit leaks into the non-tag output.
    assert _DIGIT_RE.search(non_tag_text(result)) is None


def test_mixed_tags_and_plain_cardinal():
    text = "ada <breath> 25 ekor"

    result = preprocess_preserving_tags(text)

    assert extract_tags(result) == ["<breath>"]
    assert result == "ada <breath> dua puluh lima ekor"
    assert _DIGIT_RE.search(non_tag_text(result)) is None


# ---------------------------------------------------------------------------
# No-digit passthrough
#   Non-tag segments without ASCII digits are reproduced character-for-char.
#   (Requirement 11.4)
# ---------------------------------------------------------------------------


def test_no_digit_passthrough_plain_text():
    text = "halo semuanya, apa kabar hari ini?"

    assert preprocess_preserving_tags(text) == text


def test_no_digit_passthrough_with_tags():
    text = "halo <laugh> apa kabar <sigh> sampai jumpa"

    result = preprocess_preserving_tags(text)

    # Tags preserved and non-tag text unchanged (it has no digits).
    assert result == text
    assert extract_tags(result) == ["<laugh>", "<sigh>"]


def test_no_digit_passthrough_unicode():
    # Non-ASCII, digit-free text must round-trip unchanged.
    text = "halo dunia — selamat datang ke café ☕"

    assert preprocess_preserving_tags(text) == text


# ---------------------------------------------------------------------------
# Empty text -> ""
# ---------------------------------------------------------------------------


def test_empty_text_returns_empty():
    assert preprocess_preserving_tags("") == ""


# ---------------------------------------------------------------------------
# Adjacent tags preserved verbatim and in order
#   (Requirements 12.1, 12.2 via the wrapper; 11.4)
# ---------------------------------------------------------------------------


def test_adjacent_tags_preserved():
    text = "<laugh><sigh>"

    result = preprocess_preserving_tags(text)

    assert result == "<laugh><sigh>"
    assert extract_tags(result) == ["<laugh>", "<sigh>"]


def test_three_adjacent_tags_order_preserved():
    text = "<sigh><laugh><breath>"

    result = preprocess_preserving_tags(text)

    assert result == "<sigh><laugh><breath>"
    assert extract_tags(result) == ["<sigh>", "<laugh>", "<breath>"]


# ---------------------------------------------------------------------------
# A tag immediately adjacent to a number
#   The number expands; the tag stays untouched and adjacent.
#   (Requirements 11.4, 12.3)
# ---------------------------------------------------------------------------


def test_tag_adjacent_to_number():
    text = "<laugh>10"

    result = preprocess_preserving_tags(text)

    assert result == "<laugh>sepuluh"
    assert extract_tags(result) == ["<laugh>"]
    assert _DIGIT_RE.search(non_tag_text(result)) is None


def test_number_adjacent_to_tag_both_sides():
    text = "3<sigh>4"

    result = preprocess_preserving_tags(text)

    assert result == "tiga<sigh>empat"
    assert extract_tags(result) == ["<sigh>"]
    assert _DIGIT_RE.search(non_tag_text(result)) is None


# ---------------------------------------------------------------------------
# Previously-leaking cases now leave no ASCII digit
#   The residual-digit safety net spells out any digit run the structured
#   spellers reject (out-of-range value) or fail on. (Requirement 11.3 net,
#   exercised here for 11.4's "no digit remains" guarantee.)
# ---------------------------------------------------------------------------


def test_out_of_range_percentage_leaves_no_digit():
    # A percentage far beyond the cardinal range previously slipped through the
    # structured speller and left digits in the output.
    text = "diskon <laugh> 1000000000000000% gila"

    result = preprocess_preserving_tags(text)

    # Tag preserved; absolutely no ASCII digit remains in the non-tag output.
    assert extract_tags(result) == ["<laugh>"]
    assert _DIGIT_RE.search(non_tag_text(result)) is None


def test_invalid_date_leaves_no_digit():
    # 99/99/9999 is not a valid calendar date, so the date speller rejects it;
    # the safety net must still strip the digits.
    text = "tanggal 99/99/9999 <sigh>"

    result = preprocess_preserving_tags(text)

    assert extract_tags(result) == ["<sigh>"]
    assert _DIGIT_RE.search(non_tag_text(result)) is None


def test_invalid_time_leaves_no_digit():
    # 99:99 is out of range for the time speller; digits must not survive.
    text = "jam 99:99"

    result = preprocess_preserving_tags(text)

    assert _DIGIT_RE.search(result) is None


def test_residual_digit_net_matches_inner_preprocessor():
    # The wrapper's transform of a tag-free segment matches calling the inner
    # preprocessor directly — the safety net is part of both paths.
    segment = "harga 1000000000000000% naik"

    assert preprocess_preserving_tags(segment) == preprocess_indonesian_text(segment)
    assert _DIGIT_RE.search(preprocess_preserving_tags(segment)) is None
