r"""`mabar memory-report` (T-30 before a stream) says which processes are ARTI's and which are the owner's.

2026-11-19: the workspace-folder test was once written so that a "\a" became the BEL character, so no path ever
matched and every ARTI python/bridge process was labelled the owner's. mabar.py also carried a docstring with "\s".
Both raised SyntaxWarnings at import, which a dispatch job read as a failure.
"""
import json
import warnings
from pathlib import Path

import pytest

from scripts.livepath import mabar

ROOT = Path(__file__).resolve().parents[1]


def _rows(monkeypatch, rows):
    monkeypatch.setattr(mabar.soak, "ps", lambda script: json.dumps(rows))
    return {r["name"]: r for r in mabar._process_rows()}


def test_a_process_under_documents_arti_is_ours(monkeypatch):
    got = _rows(monkeypatch, [dict(name="python.exe", gb=1.5, title="",
                                   path=str(mabar.ARTI / "runtime-checkout" / "venv" / "Scripts" / "python.exe"),
                                   cmd='"%s" voice' % (mabar.ARTI / "package-checkout" / "scripts" / "livepath" / "mabar.py"))])
    assert got["python.exe"]["owner"] == "ours"


def test_a_process_outside_it_is_the_owners(monkeypatch):
    got = _rows(monkeypatch, [dict(name="obs64.exe", gb=1.0, title="OBS",
                                   path=r"C:\Program Files\obs-studio\bin\64bit\obs64.exe", cmd="obs64.exe"),
                              dict(name="python.exe", gb=0.2, title="",
                                   path=r"C:\Users\Laptop\Documents\other\venv\python.exe", cmd="python x.py")])
    assert got["obs64.exe"]["owner"] == "owner"
    assert got["python.exe"]["owner"] == "owner"


@pytest.mark.parametrize("title, owner", [("Stardew Valley [farmhand]", "ours"), ("Stardew Valley [main player]", "owner")])
def test_the_games_are_told_apart_by_their_window_title(monkeypatch, title, owner):
    got = _rows(monkeypatch, [dict(name="StardewModdingAPI.exe", gb=1.2, title=title, path="", cmd="")])
    assert got["StardewModdingAPI.exe"]["owner"] == owner


def test_mabar_compiles_without_a_syntax_warning():
    source = (ROOT / "scripts" / "livepath" / "mabar.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(source, "mabar.py", "exec")
