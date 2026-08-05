"""Property-based tests for text_preprocessor.preprocess_preserving_tags().

Uses Hypothesis to validate the universal correctness properties of the
tag-preserving Indonesian number preprocessor defined in the Supertone 3 TTS
upgrade design.

Property 1: Tag preservation
    Validates: Requirements 12.1, 12.2
    The number preprocessor must reproduce every expression tag in the output
    character-for-character, with the same total count and in the same
    left-to-right relative order as the input — never adding, dropping, or
    duplicating a tag.
"""

from __future__ import annotations

import re

from hypothesis import given, settings, strategies as st

from text_preprocessor import preprocess_preserving_tags

# Supported expression tags from the design (`<laugh>`, `<breath>`, `<sigh>`).
EXPRESSION_TAGS = ["<laugh>", "<breath>", "<sigh>"]

# Same matcher the preprocessor uses to identify a whole `<...>` tag token.
_EXTRACT_RE = re.compile(r"<[^<>]+>")


def extract_tags(s: str) -> list[str]:
    """Return the ordered sequence of `<...>` tag tokens found in ``s``."""
    return _EXTRACT_RE.findall(s)


# Arbitrary Unicode text fragments that carry NO angle brackets, so the only
# `<...>` tokens in a generated string are the expression tags we inject. This
# keeps the input space focused: the tags recovered by ``extract_tags`` are
# exactly the ones the generator placed. ``codec="utf-8"`` excludes lone
# surrogates that cannot round-trip through UTF-8.
_text_fragment = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="<>"),
    max_size=40,
)


@st.composite
def text_with_tags(draw):
    """Build arbitrary Unicode text with expression tags at random positions.

    Returns ``(text, injected_tags)`` where ``injected_tags`` is the exact
    left-to-right sequence of tags embedded in ``text``.
    """
    n_tags = draw(st.integers(min_value=0, max_value=6))
    tags = [draw(st.sampled_from(EXPRESSION_TAGS)) for _ in range(n_tags)]
    # n_tags + 1 bracket-free fragments interleave around the injected tags.
    fragments = [draw(_text_fragment) for _ in range(n_tags + 1)]

    pieces: list[str] = []
    for i, tag in enumerate(tags):
        pieces.append(fragments[i])
        pieces.append(tag)
    pieces.append(fragments[-1])
    return "".join(pieces), tags


@settings(max_examples=300)
@given(text_with_tags())
def test_tag_preservation(payload):
    """Tags survive preprocessing: same tags, same relative order.

    **Validates: Requirements 12.1, 12.2**
    """
    text, injected = payload

    result = preprocess_preserving_tags(text)

    # Core property: the tag sequence extracted from the output equals the tag
    # sequence extracted from the input — identical tokens, identical order,
    # identical count (no additions, drops, or duplications).
    assert extract_tags(result) == extract_tags(text)

    # Strengthening check: because the surrounding text carries no angle
    # brackets, the recovered tags are exactly the injected sequence. This
    # confirms the property is meaningful and not vacuously comparing two
    # empty lists when tags are present.
    assert extract_tags(text) == injected


# ---------------------------------------------------------------------------
# Property 2: No digit leakage
#     Validates: Requirements 11.3
#     After preprocessing, no non-tag segment of the output may contain an
#     ASCII digit (0-9). Number runs located outside expression tags must all
#     have been converted to Indonesian spoken words.
# ---------------------------------------------------------------------------

# Matches a run of ASCII digits — used to detect leakage in the output.
_DIGIT_RE = re.compile(r"[0-9]")


@st.composite
def text_with_digits_and_tags(draw):
    """Build arbitrary text rich in ASCII digits with expression tags injected.

    Each piece is one of: a run of ASCII digits, a separator/glue token that
    commonly surrounds numbers (so date/time/currency/percent shapes can form),
    an arbitrary bracket-free Unicode fragment, or an expression tag. Excluding
    angle brackets from the free text guarantees the only ``<...>`` tokens in
    the result are the injected expression tags (which carry no digits), so
    stripping tag tokens isolates exactly the non-tag segments.
    """
    piece = st.one_of(
        # Runs of ASCII digits of varied length (single digits up to long runs).
        st.text(alphabet="0123456789", min_size=1, max_size=8),
        # Glue tokens that let number-like shapes (dates, times, currency,
        # percentages, phone numbers, decimals) assemble around digit runs.
        st.sampled_from(
            ["/", "-", ":", ".", ",", "%", "+", " ", "Rp ", "IDR ", "x", "th"]
        ),
        # Arbitrary bracket-free Unicode text.
        st.text(
            alphabet=st.characters(codec="utf-8", exclude_characters="<>"),
            max_size=15,
        ),
        # Expression tags injected at random positions.
        st.sampled_from(EXPRESSION_TAGS),
    )
    n_pieces = draw(st.integers(min_value=0, max_value=12))
    return "".join(draw(piece) for _ in range(n_pieces))


@settings(max_examples=500)
@given(text_with_digits_and_tags())
def test_no_digit_leakage(text):
    """No ASCII digit survives in any non-tag segment of the output.

    **Validates: Requirements 11.3**
    """
    result = preprocess_preserving_tags(text)

    # Remove the `<...>` tag tokens so only non-tag segments remain. The
    # injected expression tags contain no digits, so any digit found after
    # stripping tags is a genuine leak from number preprocessing.
    non_tag = _EXTRACT_RE.sub("", result)

    leaked = _DIGIT_RE.search(non_tag)
    assert leaked is None, (
        f"ASCII digit leaked into non-tag output: input={text!r} "
        f"output={result!r} non_tag={non_tag!r}"
    )


# ---------------------------------------------------------------------------
# Property 3: Preprocessor idempotence
#     Validates: Requirements 11.5
#     Applying the preprocessor to already-preprocessed text must produce
#     output character-for-character identical to applying it exactly once:
#     f(f(text)) == f(text) where f = preprocess_preserving_tags. After one
#     pass there are no ASCII digits left in any non-tag segment (the residual
#     -digit safety net spells out anything the structured pass missed), so the
#     second pass has nothing to transform and reproduces its input verbatim.
# ---------------------------------------------------------------------------


@settings(max_examples=500)
@given(text_with_digits_and_tags())
def test_idempotence(text):
    """A second preprocessing pass changes nothing: f(f(x)) == f(x).

    **Validates: Requirements 11.5**
    """
    once = preprocess_preserving_tags(text)
    twice = preprocess_preserving_tags(once)

    assert twice == once, (
        f"preprocessing is not idempotent: input={text!r} "
        f"once={once!r} twice={twice!r}"
    )
