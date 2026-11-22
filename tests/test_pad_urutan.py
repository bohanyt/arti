"""Parser helper join P2 — tanpa ViGEm, tanpa window sungguhan."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pad_urutan.py"
_SPEC = importlib.util.spec_from_file_location("pad_urutan_helper", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
pad_urutan = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pad_urutan)


def test_parse_langkah_default_jeda():
    assert pad_urutan.parse_langkah("START") == ("START", 1.0)
    assert pad_urutan.parse_langkah("a:2.0") == ("A", 2.0)
    assert pad_urutan.parse_langkah("dpad-down:0.8") == ("DPAD_DOWN", 0.8)


def test_parse_urutan_join_stardew():
    assert pad_urutan.parse_urutan(["START:2.0", "A:1.0"]) == [
        ("START", 2.0),
        ("A", 1.0),
    ]


def test_parse_urutan_tolak_nama_asing():
    with pytest.raises(ValueError, match="tidak dikenal"):
        pad_urutan.parse_urutan(["START:2.0", "KOTAK:1"])


def test_parse_urutan_tolak_quit_di_awal():
    with pytest.raises(ValueError, match="urutan awal"):
        pad_urutan.parse_urutan(["quit"])


def test_parse_langkah_buang_bom():
    assert pad_urutan.parse_langkah("\ufeffSTIK_BAWAH:1.5") == ("STIK_BAWAH", 1.5)


def test_stik_dan_dpad_dikenal():
    for nama in ("DPAD_DOWN", "DOWN", "STIK_BAWAH", "START", "A"):
        assert pad_urutan.langkah_dikenal(nama)
    assert not pad_urutan.langkah_dikenal("KOTAK")
