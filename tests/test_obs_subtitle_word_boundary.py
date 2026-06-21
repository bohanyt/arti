"""Property-based tests for ``_parse_word_boundary`` in hermes_vtuber_bridge.

Spec: ``.kiro/specs/obs-subtitle-integration``

Covers three properties from the OBS Subtitle Integration design:

- Property 1 (Task 2.2): WordBoundary tick-to-second conversion.
  Validates Requirements 1.4, 1.5, 5.10.

- Property 2 (Task 2.3): Malformed WordBoundary chunks are skipped without
  raising. Validates Requirement 1.7.

- Property 14 (Task 2.4): WordBoundary text non-ASCII passthrough.
  Validates Requirements 5.9, 5.10.

These tests exercise the helper directly against the byte-for-byte contract
defined in the spec — no normalization, escaping, case folding, or trimming
is permitted on the ``word`` field, and tick → second conversion is exactly
``value / 10_000_000`` (HNS Ticks → seconds).
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# Match the import-path pattern used by tests/test_bridge_startup_bugfix.py:
# pytest's conftest.py already prepends REPO_ROOT, but we re-add it here so
# the module is importable when this file is run directly as well.
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hermes_vtuber_bridge import HNS_PER_SECOND, _parse_word_boundary  # noqa: E402


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Non-negative integer ticks, bounded so float conversion stays exact.
_NON_NEG_INT_TICKS = st.integers(min_value=0, max_value=10**12)

# Non-negative float seconds-equivalents bounded well below float precision
# loss so the implementation's `float(x) / HNS_PER_SECOND` and the test's
# `x / HNS_PER_SECOND` agree bit-for-bit.
_NON_NEG_FLOAT_TICKS = st.floats(
    min_value=0.0,
    max_value=1e9,
    allow_nan=False,
    allow_infinity=False,
)

_NON_NEG_NUMERIC_TICKS = st.one_of(_NON_NEG_INT_TICKS, _NON_NEG_FLOAT_TICKS)


# ---------------------------------------------------------------------------
# Property 1 — WordBoundary tick-to-second conversion
# ---------------------------------------------------------------------------

# Feature: obs-subtitle-integration, Property 1: WordBoundary tick-to-second
# conversion. Validates Requirements 1.4, 1.5, 5.10.
@given(
    offset=_NON_NEG_NUMERIC_TICKS,
    duration=_NON_NEG_NUMERIC_TICKS,
    text=st.text(min_size=0, max_size=200),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_word_boundary_tick_to_second_conversion(offset, duration, text):
    """A valid WordBoundary chunk converts ticks → seconds exactly and passes
    the text through byte-for-byte."""

    chunk = {"offset": offset, "duration": duration, "text": text}

    result = _parse_word_boundary(chunk)

    assert result is not None, "valid chunk must not be skipped"

    # Tick → second conversion contract.
    assert result["start"] == offset / HNS_PER_SECOND
    assert result["duration"] == duration / HNS_PER_SECOND

    # Both values must be Python floats per Requirement 1.4.
    assert isinstance(result["start"], float)
    assert isinstance(result["duration"], float)

    # Byte-for-byte text passthrough per Requirements 1.5 / 5.10.
    assert result["word"] == text
    # `==` on str compares code-point sequences; reinforce the contract by
    # confirming the underlying UTF-8 bytes are identical too.
    assert result["word"].encode("utf-8") == text.encode("utf-8")


# ---------------------------------------------------------------------------
# Property 2 — Malformed WordBoundary chunks are skipped without raising
# ---------------------------------------------------------------------------

# A "non-numeric" value is anything except a real int or float. bool is a
# subclass of int in Python, but the implementation explicitly rejects bool
# because True/False are not meaningful tick counts — so True / False are
# included here as expected-None inputs to lock that behavior in.
_NON_NUMERIC_VALUES = st.one_of(
    st.text(min_size=0, max_size=20),
    st.none(),
    st.lists(st.integers(), min_size=0, max_size=3),
    st.dictionaries(st.text(min_size=0, max_size=4), st.integers(), max_size=3),
    st.booleans(),  # True / False — bool is a subclass of int but rejected
)

_NEGATIVE_NUMERIC = st.one_of(
    st.integers(min_value=-(10**12), max_value=-1),
    st.floats(min_value=-1e9, max_value=-1e-9, allow_nan=False, allow_infinity=False),
)


@st.composite
def malformed_word_boundary_chunks(draw):
    """Draw a chunk that is malformed in exactly one way per the contract.

    The mutation modes mirror Requirement 1.7's failure list:
      - missing offset / duration / text
      - non-numeric offset or duration (incl. bool)
      - negative offset or duration
    """

    mode = draw(
        st.sampled_from(
            [
                "missing_offset",
                "missing_duration",
                "missing_text",
                "non_numeric_offset",
                "non_numeric_duration",
                "negative_offset",
                "negative_duration",
            ]
        )
    )

    base_offset = draw(_NON_NEG_NUMERIC_TICKS)
    base_duration = draw(_NON_NEG_NUMERIC_TICKS)
    base_text = draw(st.text(min_size=0, max_size=40))

    chunk: dict = {"offset": base_offset, "duration": base_duration, "text": base_text}

    if mode == "missing_offset":
        chunk.pop("offset")
    elif mode == "missing_duration":
        chunk.pop("duration")
    elif mode == "missing_text":
        chunk.pop("text")
    elif mode == "non_numeric_offset":
        chunk["offset"] = draw(_NON_NUMERIC_VALUES)
    elif mode == "non_numeric_duration":
        chunk["duration"] = draw(_NON_NUMERIC_VALUES)
    elif mode == "negative_offset":
        chunk["offset"] = draw(_NEGATIVE_NUMERIC)
    elif mode == "negative_duration":
        chunk["duration"] = draw(_NEGATIVE_NUMERIC)

    return chunk


# Feature: obs-subtitle-integration, Property 2: Malformed WordBoundary chunks
# are skipped without raising. Validates Requirement 1.7.
@given(chunk=malformed_word_boundary_chunks())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_malformed_word_boundary_returns_none_without_raising(chunk, capsys):
    """Any malformed shape must yield None and no exception escapes."""

    # Capture the diagnostic stdout that ``_parse_word_boundary`` prints on
    # the skip path; we don't assert on its content, just keep the test
    # output clean. ``capsys`` does the work transparently.
    try:
        result = _parse_word_boundary(chunk)
    except Exception as e:  # pragma: no cover — would fail the property
        pytest.fail(
            f"_parse_word_boundary raised {type(e).__name__}: {e!r} on chunk {chunk!r}"
        )

    assert result is None

    # Drain captured output so it does not leak across hypothesis examples.
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Property 14 — WordBoundary text non-ASCII passthrough
# ---------------------------------------------------------------------------

# Feature: obs-subtitle-integration, Property 14: WordBoundary text non-ASCII
# passthrough. Validates Requirements 5.9, 5.10.
@given(
    text=st.text(
        alphabet=st.characters(min_codepoint=0x0080),
        min_size=1,
        max_size=80,
    )
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_word_boundary_non_ascii_text_passthrough(text):
    """Non-ASCII text must round-trip byte-for-byte through the parser."""

    result = _parse_word_boundary({"offset": 0, "duration": 0, "text": text})

    assert result is not None
    assert result["word"] == text
    # Reinforce: same code points and same UTF-8 byte sequence — no
    # normalization (NFC/NFD), no case folding, no escaping was applied.
    assert result["word"].encode("utf-8") == text.encode("utf-8")
