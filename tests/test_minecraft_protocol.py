"""Tests encode_command / decode_event — protokol NDJSON ke bot Node."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_minecraft import decode_event, encode_command  # noqa: E402


def test_encode_simple_verbs():
    for verb in ("follow", "come", "stop", "status", "quit"):
        assert json.loads(encode_command({"cmd": verb})) == {"cmd": verb}


def test_encode_say_carries_text():
    line = encode_command({"cmd": "say", "text": "halo bohan"})
    assert json.loads(line) == {"cmd": "say", "text": "halo bohan"}
    assert "\n" not in line


def test_encode_rejects_unknown_verb():
    with pytest.raises(ValueError):
        encode_command({"cmd": "teleport"})
    with pytest.raises(ValueError):
        encode_command({"cmd": ""})
    with pytest.raises(ValueError):
        encode_command("follow")  # type: ignore[arg-type]


def test_encode_say_rejects_slash_and_empty():
    with pytest.raises(ValueError):
        encode_command({"cmd": "say", "text": "/stop"})
    with pytest.raises(ValueError):
        encode_command({"cmd": "say", "text": "   "})


def test_encode_mine_validates_block_and_clamps():
    payload = json.loads(encode_command({"cmd": "mine", "block": "stone", "count": 999}))
    assert payload == {"cmd": "mine", "block": "stone", "count": 32}
    with pytest.raises(ValueError):
        encode_command({"cmd": "mine", "block": "Stone;drop table"})


def test_decode_valid_event():
    ev = decode_event('{"ev": "spawned", "ts": 1.0, "health": 20}\n')
    assert ev is not None and ev["ev"] == "spawned"


def test_decode_garbage_returns_none():
    assert decode_event("") is None
    assert decode_event("   \n") is None
    assert decode_event("bukan json") is None
    assert decode_event('{"belum": "tutup"') is None
    assert decode_event('["list", "bukan", "dict"]') is None
    assert decode_event('{"tanpa_ev": 1}') is None
    assert decode_event('{"ev": 42}') is None  # ev wajib string
