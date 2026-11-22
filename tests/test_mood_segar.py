"""Mood di prompt harus SEGAR tiap giliran, bukan beku sejak startup.

Kalau `mood_block` beku sejak startup, scouter boleh memperbarui mood berkali-kali
tapi prompt tetap membawa mood awal, dan balasan memakai formula yang sama.

Akarnya: `mood_block` dirakit sekali lalu ikut dipanggang ke
`dynamic_system_prompt`. Mood boleh berubah sesering apa pun — Arti tetap
membaca mood saat bridge dinyalakan.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_bridge as bridge


def test_mood_block_ikut_berubah(monkeypatch):
    terlihat = []
    for mood in ("confused", "excited", "lazy"):
        monkeypatch.setattr(bridge, "get_current_mood", lambda m=mood: m)
        terlihat.append(bridge.mood_block_now())
    assert len(set(terlihat)) == 3, f"blok mood tidak ikut berubah: {terlihat}"
    assert "excited" in terlihat[1]
    assert "lazy" in terlihat[2]


def test_mood_block_memuat_mood_saat_ini(monkeypatch):
    monkeypatch.setattr(bridge, "get_current_mood", lambda: "happy")
    assert "[MOOD SAAT INI: happy]" in bridge.mood_block_now()


def test_riwayat_mood_dipangkas(monkeypatch, tmp_path):
    """Berkas sempat 174 KB / 2.649 entri — dan kini dibaca tiap giliran."""
    monkeypatch.setattr(bridge, "_SCRIPT_DIR", str(tmp_path))
    p = tmp_path / "ARTI_MOOD_STATE.json"
    p.write_text(json.dumps({"current_mood": "happy", "mood_history": []}), encoding="utf-8")

    for i in range(bridge.MOOD_HISTORY_MAX + 60):
        bridge.set_mood("lazy" if i % 2 else "excited")

    state = json.loads(p.read_text(encoding="utf-8"))
    n = len(state["mood_history"])
    assert n <= bridge.MOOD_HISTORY_MAX, f"riwayat tidak dipangkas: {n} entri"
    assert state["current_mood"] in ("lazy", "excited")
