"""Golden ASR text behavior captured from the bridge before the split."""

import ast
from pathlib import Path

import pytest

from arti_runtime import asr_text


_BRIDGE_NAMES = (
    "_ASR_LITERAL_PHANTOMS",
    "_ASR_BARE_THANKS",
    "_ASR_ALWAYS_NOISE",
    "_ASR_NOISE_SUBSTRINGS",
    "_normalize_asr_text",
    "filter_whisper_hallucination",
)


def test_bridge_reexports_the_same_objects():
    import arti_bridge

    for name in _BRIDGE_NAMES:
        assert getattr(arti_bridge, name) is getattr(asr_text, name), name


def test_asr_text_does_not_import_the_bridge():
    source = Path(asr_text.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert "arti_bridge" not in (node.module or "").split(".")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert all("arti_bridge" not in alias.name.split(".") for alias in node.names)


def test_noise_patterns_keep_their_literal_matches():
    assert "Terima kasih." in asr_text._ASR_LITERAL_PHANTOMS
    assert "terima kasih." not in asr_text._ASR_LITERAL_PHANTOMS
    assert "terima kasih" in asr_text._ASR_BARE_THANKS
    assert "selamat menikmati" in asr_text._ASR_ALWAYS_NOISE
    assert asr_text._ASR_NOISE_SUBSTRINGS == (
        "terima kasih telah menonton",
        "terima kasih sudah menonton",
        "like and subscribe",
        "thank you for watching",
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  ArTi, MÉRDEKA?!_  ", "arti mérdeka_"),
        ("\tHalo...   DUNIA!\nBARU  ", "halo   dunia\nbaru"),
    ],
)
def test_normalize_asr_text(text, expected):
    assert asr_text._normalize_asr_text(text) == expected


@pytest.mark.parametrize(
    ("text", "is_passive_monitoring", "expected"),
    [
        (None, True, ""),
        ("  \t ", True, ""),
        (" THANK YOU FOR WATCHING! ", False, ""),
        (" Terima kasih guys! ", True, ""),
        (" Terima kasih guys! ", False, "Terima kasih guys!"),
        (" OH?! ", False, ""),
        ("ya ya ya ya ya lanjut bentar", True, ""),
        ("ya ya ya ya lanjut bentar sekarang", True, "ya ya ya ya lanjut bentar sekarang"),
        ("ya ya ya ya ya ya", True, "ya ya ya ya ya ya"),
        ("  Arti, aku di sini!  ", True, "Arti, aku di sini!"),
    ],
)
def test_filter_whisper_hallucination(text, is_passive_monitoring, expected):
    assert asr_text.filter_whisper_hallucination(
        text, is_passive_monitoring=is_passive_monitoring,
    ) == expected
