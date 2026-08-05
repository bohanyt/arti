"""Property-based test for the NDJSON protocol round-trip identity.

Validates the design's **Property 4: Protocol round-trip identity** for the
Supertone 3 TTS upgrade: serialize -> parse is the identity function for both
request and response objects.

The round-trip uses the SAME serialization the engine uses. ``supertone_engine.emit()``
writes ``json.dumps(obj) + "\\n"`` to stdout and the bridge reads one line and
parses it with ``json.loads``. This test mirrors that exact discipline:

    serialize(obj) == json.dumps(obj) + "\\n"      # one compact line + framing
    parse(line)    == json.loads(line)

and asserts ``parse(serialize(obj)) == obj`` with:

* an identical set of field names (none added, none omitted),
* equal values, and
* preserved JSON types -- ``int`` stays ``int``, ``float`` stays ``float``,
  ``bool`` stays ``bool``, ``str`` stays ``str`` (with ``bool`` kept distinct
  from ``int`` since ``True == 1`` in Python).

It also asserts the serialized form is a single physical line (no embedded
newline within the JSON content) and that appending the ``"\\n"`` framing yields
exactly one line, and that non-ASCII characters and expression tags survive the
round-trip verbatim and in their original order (Requirement 7.4).

Property 4: Protocol round-trip identity
    Validates: Requirements 7.1, 7.2, 7.4
"""

from __future__ import annotations

import json
import re

from hypothesis import given, settings, strategies as st

# --------------------------------------------------------------------------- #
# Serialization mirroring supertone_engine.emit()
# --------------------------------------------------------------------------- #


def serialize(obj: dict) -> str:
    """Serialize exactly as the engine's ``emit()`` does: compact JSON + "\\n".

    ``json.dumps`` with default arguments (no ``indent``) produces a single
    compact line, and ``emit()`` appends one trailing newline as the NDJSON
    frame delimiter.
    """
    return json.dumps(obj) + "\n"


def parse(line: str) -> dict:
    """Parse one NDJSON line back into an object, as the bridge does."""
    return json.loads(line)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Supported expression tags from the design (`<laugh>`, `<breath>`, `<sigh>`).
EXPRESSION_TAGS = ["<laugh>", "<breath>", "<sigh>"]

# Whole `<...>` tag-token matcher (same shape the preprocessor uses).
_TAG_RE = re.compile(r"<[^<>]+>")

# Voice style ids from the design: F1-F5 / M1-M5.
VOICES = [f"F{i}" for i in range(1, 6)] + [f"M{i}" for i in range(1, 6)]

# Protocol-side error codes from the design's error-code table.
ERROR_CODES = [
    "MODEL_LOAD_FAILED",
    "BAD_REQUEST",
    "UNSUPPORTED_VERSION",
    "SYNTH_FAILED",
    "SAVE_FAILED",
    "UNKNOWN_TYPE",
]


def extract_tags(s: str) -> list[str]:
    """Return the ordered sequence of `<...>` tag tokens found in ``s``."""
    return _TAG_RE.findall(s)


def non_ascii_chars(s: str) -> list[str]:
    """Return the ordered sequence of non-ASCII characters in ``s``."""
    return [c for c in s if ord(c) > 127]


def assert_json_identical(original, parsed, path: str = "$") -> None:
    """Assert ``original`` and ``parsed`` are structurally and type-identical.

    JSON types must be preserved exactly: ``bool`` is kept distinct from
    ``int`` (Python treats ``True == 1`` as equal, so a plain ``==`` is not
    enough), ``float`` stays ``float``, ``str`` stays ``str``. Field sets of
    objects must match exactly with no field added or omitted.
    """
    assert type(original) is type(parsed), (
        f"type changed at {path}: {type(original).__name__} -> "
        f"{type(parsed).__name__} (orig={original!r}, parsed={parsed!r})"
    )

    if isinstance(original, dict):
        assert set(original) == set(parsed), (
            f"field set changed at {path}: {sorted(original)} != {sorted(parsed)}"
        )
        for key in original:
            assert_json_identical(original[key], parsed[key], f"{path}.{key}")
    elif isinstance(original, list):
        assert len(original) == len(parsed), (
            f"list length changed at {path}: {len(original)} != {len(parsed)}"
        )
        for i, (a, b) in enumerate(zip(original, parsed)):
            assert_json_identical(a, b, f"{path}[{i}]")
    else:
        assert original == parsed, (
            f"value changed at {path}: {original!r} != {parsed!r}"
        )


def assert_single_line(obj: dict) -> str:
    """Assert the serialized object is one newline-terminated physical line.

    Returns the serialized frame so callers can parse it. The JSON content
    itself must contain no embedded newline; the only newline is the trailing
    NDJSON frame delimiter.
    """
    content = json.dumps(obj)  # exactly what emit() writes before the "\n"
    assert "\n" not in content, f"embedded newline in JSON content: {content!r}"

    framed = serialize(obj)
    assert framed == content + "\n"
    assert framed.endswith("\n")
    # Exactly one newline (the frame delimiter) -> exactly one line of content.
    assert framed.count("\n") == 1, f"expected one line, got {framed!r}"
    assert framed.split("\n") == [content, ""]
    return framed


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #

# Finite floats only: NaN/Infinity do not satisfy round-trip identity (NaN is
# never equal to itself) and are not part of the protocol's numeric payloads.
_finite_float = st.floats(allow_nan=False, allow_infinity=False)

# Bracket-free Unicode fragments so the only `<...>` tokens in a generated text
# are the expression tags we inject. ``codec="utf-8"`` excludes lone surrogates
# that cannot encode to UTF-8. Control characters (including "\n") are allowed
# so the single-line assertion exercises JSON escaping of literal newlines.
_free_text = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="<>"),
    max_size=40,
)


@st.composite
def protocol_text(draw) -> str:
    """Arbitrary UTF-8 text with expression tags injected at random positions.

    Mixes non-ASCII Unicode fragments with `<laugh>`/`<breath>`/`<sigh>` tags so
    the round-trip exercises both non-ASCII characters and expression tags
    (Requirement 7.4).
    """
    n_tags = draw(st.integers(min_value=0, max_value=5))
    tags = [draw(st.sampled_from(EXPRESSION_TAGS)) for _ in range(n_tags)]
    fragments = [draw(_free_text) for _ in range(n_tags + 1)]

    pieces: list[str] = []
    for i, tag in enumerate(tags):
        pieces.append(fragments[i])
        pieces.append(tag)
    pieces.append(fragments[-1])
    return "".join(pieces)


@st.composite
def synthesize_requests(draw) -> dict:
    """A ``synthesize`` request object as the bridge serializes it."""
    return {
        "v": 1,
        "id": draw(st.integers(min_value=1, max_value=2**53)),
        "type": "synthesize",
        "text": draw(protocol_text()),
        "voice": draw(st.sampled_from(VOICES)),
        "speed": draw(_finite_float),
        "lang": draw(st.text(alphabet=st.characters(codec="utf-8"), max_size=8)),
        "total_steps": draw(st.integers(min_value=0, max_value=50)),
        "preprocess_numbers": draw(st.booleans()),
    }


@st.composite
def ok_responses(draw) -> dict:
    """A successful synthesize response object as the engine serializes it."""
    return {
        "v": 1,
        "id": draw(st.integers(min_value=1, max_value=2**53)),
        "ok": True,
        "wav_path": draw(st.text(alphabet=st.characters(codec="utf-8"), max_size=80)),
        "duration": draw(_finite_float),
        "engine": "supertonic-3",
    }


@st.composite
def error_responses(draw) -> dict:
    """An error response object (`ok` False with a nested error) as serialized."""
    return {
        "v": 1,
        "id": draw(st.integers(min_value=1, max_value=2**53)),
        "ok": False,
        "error": {
            "code": draw(st.sampled_from(ERROR_CODES)),
            "message": draw(protocol_text()),
        },
    }


def protocol_objects():
    """Either a request or a (success/error) response object."""
    return st.one_of(synthesize_requests(), ok_responses(), error_responses())


# --------------------------------------------------------------------------- #
# Property 4: Protocol round-trip identity
# --------------------------------------------------------------------------- #


@settings(max_examples=400)
@given(protocol_objects())
def test_protocol_roundtrip_identity(obj):
    """parse(serialize(obj)) == obj, types preserved, single-line frame.

    **Validates: Requirements 7.1, 7.2, 7.4**
    """
    framed = assert_single_line(obj)
    reparsed = parse(framed)

    # Same field set, equal values, preserved JSON types (int/float/bool/str).
    assert_json_identical(obj, reparsed)

    # Plain structural equality must also hold (cheap cross-check).
    assert reparsed == obj


@settings(max_examples=400)
@given(synthesize_requests())
def test_text_nonascii_and_tags_roundtrip_verbatim(req):
    """Non-ASCII chars and expression tags survive verbatim and in order.

    **Validates: Requirements 7.4**
    """
    framed = assert_single_line(req)
    reparsed = parse(framed)

    original_text = req["text"]
    parsed_text = reparsed["text"]

    # Whole-string verbatim round-trip (the strongest statement).
    assert parsed_text == original_text
    assert type(parsed_text) is str

    # Expression tags reproduced verbatim and in their original relative order.
    assert extract_tags(parsed_text) == extract_tags(original_text)

    # Non-ASCII characters reproduced verbatim and in their original order.
    assert non_ascii_chars(parsed_text) == non_ascii_chars(original_text)


@settings(max_examples=200)
@given(error_responses())
def test_error_response_nested_object_roundtrip(resp):
    """Nested ``error`` object round-trips with identical fields and types.

    **Validates: Requirements 7.2**
    """
    framed = assert_single_line(resp)
    reparsed = parse(framed)

    assert_json_identical(resp, reparsed)
    assert set(reparsed["error"]) == {"code", "message"}
    assert reparsed["ok"] is False and type(reparsed["ok"]) is bool
