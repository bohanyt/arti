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


def test_nod_y_range_reads_expression_files():
    import json
    from pathlib import Path

    base = Path(arti_nod.NOD_MODEL_DIR)
    if not base.exists():
        return
    atas = json.loads((base / arti_nod.NOD_ATAS).read_text(encoding="utf-8"))
    bawah = json.loads((base / arti_nod.NOD_BAWAH).read_text(encoding="utf-8"))
    ay = next(p["Value"] for p in atas["Parameters"] if p["Id"] == "ParamAngleY")
    by = next(p["Value"] for p in bawah["Parameters"] if p["Id"] == "ParamAngleY")
    up, down = arti_nod._nod_y_range()
    assert up == ay
    assert down == by
