"""Tests for arti_wake — false positive berarti filter."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_wake import is_arti_wake_call


def test_berarti_not_wake():
    assert is_arti_wake_call("ya berarti belum bisa") is False
    assert is_arti_wake_call("berarti dia gak bodoh") is False
    assert is_arti_wake_call("berarti bang bohan ganteng") is False


def test_real_wake_calls():
    assert is_arti_wake_call("eh arti halo") is True
    assert is_arti_wake_call("halo arti!") is True
    assert is_arti_wake_call("Arti kamu di sana?") is True
