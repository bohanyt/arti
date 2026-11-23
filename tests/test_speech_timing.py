"""Golden behavior and bridge compatibility for the speech-timing helpers."""

import ast
from pathlib import Path

import numpy as np
import pytest

from arti_runtime import speech_timing


_BRIDGE_NAMES = (
    "resample_audio",
    "HNS_PER_SECOND",
    "_parse_word_boundary",
    "_PUNCTUATION_PHRASE",
    "_PUNCTUATION_CLAUSE",
    "MIN_PHRASE_CHARS",
    "MAX_PHRASE_CHARS",
    "_split_into_phrases",
    "_estimate_phrase_timings",
)

# Expected results were captured from the nine statements before the split.
_TEXT = (
    "The lanterns along the river are glowing softly, and the market stalls are still open tonight. "
    "Oh! We should go now."
)


def test_bridge_reexports_the_same_objects():
    import arti_bridge

    for name in _BRIDGE_NAMES:
        assert getattr(arti_bridge, name) is getattr(speech_timing, name), name


def test_speech_timing_does_not_import_the_bridge():
    source = Path(speech_timing.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert "arti_bridge" not in (node.module or "").split(".")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert all("arti_bridge" not in alias.name.split(".") for alias in node.names)


def test_split_long_clause_and_merge_short_fragment():
    assert speech_timing._split_into_phrases(_TEXT) == [
        "The lanterns along the river are glowing softly,",
        "and the market stalls are still open tonight. Oh!",
        "We should go now.",
    ]


def test_estimate_phrase_timings():
    assert speech_timing._estimate_phrase_timings(_TEXT, 3.2) == [
        {"word": "The lanterns along the river are glowing softly,", "start": 0.0, "duration": 1.347},
        {"word": "and the market stalls are still open tonight. Oh!", "start": 1.347, "duration": 1.375},
        {"word": "We should go now.", "start": 2.723, "duration": 0.477},
    ]


@pytest.mark.parametrize(
    ("data", "shape", "expected"),
    [
        (
            [0.0, 1.0, -0.5, 0.25],
            (8,),
            [0.0, 0.8571428571428572, -0.0714285714285714, 0.25],
        ),
        (
            [[0.0, 1.0], [1.0, 0.0], [-0.5, 0.5], [0.25, -1.0]],
            (8, 2),
            [
                [0.0, 1.0],
                [0.8571428571428572, 0.1428571428571428],
                [-0.0714285714285714, 0.35714285714285715],
                [0.25, -1.0],
            ],
        ),
    ],
)
def test_resample_audio(data, shape, expected):
    samples, rate = speech_timing.resample_audio(np.array(data, dtype=np.float32), 22050, 48000)
    assert rate == 48000
    assert samples.shape == shape
    assert np.allclose(samples[[0, 2, 4, 7]], expected)


def test_resample_audio_at_matching_rate_returns_original_buffer():
    data = np.array([0.0, 1.0, -0.5, 0.25], dtype=np.float32)
    samples, rate = speech_timing.resample_audio(data, 48000, 48000)
    assert rate == 48000
    assert samples is data


def test_parse_word_boundary():
    chunk = {"offset": 12_500_000, "duration": 2_500_000, "text": " Hello "}
    assert speech_timing._parse_word_boundary(chunk) == {
        "word": " Hello ",
        "start": 1.25,
        "duration": 0.25,
    }


def test_skip_malformed_word_boundary(capsys):
    chunk = {"offset": 12_500_000, "text": " Hello "}
    assert speech_timing._parse_word_boundary(chunk) is None
    assert capsys.readouterr().out == "[SubTitle] Skipping WordBoundary missing field: 'duration'\n"
