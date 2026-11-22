"""Tee proses mandiri (19 Agu) — dubbing & discord tidak boleh bisu di disk."""

from __future__ import annotations

import sys
from pathlib import Path

import arti_debug_log as adl

ROOT = Path(__file__).resolve().parent.parent


def test_pytest_default_noop():
    """Suite tidak boleh menabur berkas log (pelajaran 293-byte 12 Agu)."""
    assert adl.pasang("tes-noop") is None


def test_force_menulis_stdout_dan_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(adl, "_DIR", tmp_path)
    asli_out, asli_err = sys.stdout, sys.stderr
    try:
        path = adl.pasang("tesproc", force=True)
        print("baris stdout uji")
        print("baris stderr uji", file=sys.stderr)
    finally:
        sys.stdout, sys.stderr = asli_out, asli_err
    isi = Path(path).read_text(encoding="utf-8")
    assert "baris stdout uji" in isi
    assert "baris stderr uji" in isi
    assert "[PID" in isi


def test_rotasi_per_nama(monkeypatch, tmp_path):
    monkeypatch.setattr(adl, "_DIR", tmp_path)
    import time as _t

    for i in range(4):
        p = tmp_path / f"2026-08-1{i}_000000_tesproc.log"
        p.write_text("x", encoding="utf-8")
        _t.sleep(0.01)
    adl._rotasi("tesproc", keep_n=2)
    assert len(list(tmp_path.glob("*_tesproc.log"))) == 2


def test_dubbing_dan_discord_memasang_tee():
    for f in ("arti_dubbing.py", "arti_discord.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert "arti_debug_log.pasang(" in src, f"{f} bisu di disk"
