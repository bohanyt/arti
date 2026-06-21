"""Tests for Groq streaming sentence splitter."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_groq_stream as gs


def test_split_indonesian_sentences():
    text = "Halo Kak! Aku Arti. Senang ketemu ya?"
    parts = gs.split_indonesian_sentences(text)
    assert len(parts) == 3
    assert parts[0] == "Halo Kak!"


def test_extract_delta_text():
    chunk = {"choices": [{"delta": {"content": "hai"}}]}
    assert gs.extract_delta_text(chunk) == "hai"


def test_collect_streaming_reply_sentences():
    lines = [
        b'data: {"choices":[{"delta":{"content":"Halo! "}}]}\n',
        b'data: {"choices":[{"delta":{"content":"Apa kabar?"}}]}\n',
        b"data: [DONE]\n",
    ]

    def _iter():
        yield from lines

    full, sentences = gs.collect_streaming_reply(_iter())
    assert "Halo" in full
    assert len(sentences) >= 1
