"""Tests for arti_nod constants."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_nod


def test_nod_expression_filenames():
    assert arti_nod.NOD_ATAS == "ArtiNganggukAtas.exp3.json"
    assert arti_nod.NOD_BAWAH == "ArtiNganggukBawah.exp3.json"


def test_nod_y_range_reads_expression_files(tmp_path, monkeypatch):
    import json

    for name, value in ((arti_nod.NOD_ATAS, 4.5), (arti_nod.NOD_BAWAH, -3.25)):
        (tmp_path / name).write_text(json.dumps({"Parameters": [{"Id": "ParamAngleY", "Value": value}]}),
                                     encoding="utf-8")
    monkeypatch.setattr(arti_nod, "NOD_MODEL_DIR", str(tmp_path))
    assert arti_nod._nod_y_range() == (4.5, -3.25)


def test_nod_y_range_falls_back_without_expression_files(tmp_path, monkeypatch):
    monkeypatch.setattr(arti_nod, "NOD_MODEL_DIR", str(tmp_path / "missing"))
    assert arti_nod._nod_y_range() == (arti_nod.NOD_Y_UP, arti_nod.NOD_Y_DOWN)
