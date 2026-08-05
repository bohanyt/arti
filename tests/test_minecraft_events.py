"""Tests kebijakan reaksi suara + format konteks (clock palsu, tanpa Node)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_minecraft import (  # noqa: E402
    ReactionLimiter,
    format_context,
    map_event_to_reaction,
    status_note,
)

CFG = {"minecraft_reaction_cooldown_sec": 60.0}


def test_death_always_reacts_with_dedupe():
    lim = ReactionLimiter()
    r1 = map_event_to_reaction({"ev": "death", "killer": "creeper"}, lim, 100.0, CFG)
    assert r1 and r1.startswith("[MINECRAFT]") and "creeper" in r1
    # duplikat dalam 10 dtk (server kadang dobel event) -> diam
    assert map_event_to_reaction({"ev": "death"}, lim, 105.0, CFG) is None
    # kematian berikutnya (nyawa baru) -> bicara lagi
    assert map_event_to_reaction({"ev": "death"}, lim, 200.0, CFG) is not None


def test_combat_bucket_shared_cooldown():
    lim = ReactionLimiter()
    hurt = {"ev": "hurt", "source": "zombie", "health": 15}
    near = {"ev": "hostile_near", "kind": "creeper", "distance": 4}
    assert map_event_to_reaction(hurt, lim, 100.0, CFG) is not None
    # hostile_near ikut bucket combat yang sama -> kena cooldown
    assert map_event_to_reaction(near, lim, 130.0, CFG) is None
    assert map_event_to_reaction(near, lim, 161.0, CFG) is not None


def test_hurt_unknown_source_is_context_only():
    lim = ReactionLimiter()
    ev = {"ev": "hurt", "source": "unknown", "health": 19}
    assert map_event_to_reaction(ev, lim, 100.0, CFG) is None


def test_low_health_once_per_life_reset_on_respawn():
    lim = ReactionLimiter()
    ev = {"ev": "low_health", "health": 4}
    assert map_event_to_reaction(ev, lim, 100.0, CFG) is not None
    assert map_event_to_reaction(ev, lim, 120.0, CFG) is None  # sekali per nyawa
    map_event_to_reaction({"ev": "respawn"}, lim, 130.0, CFG)
    assert map_event_to_reaction(ev, lim, 140.0, CFG) is not None


def test_low_health_zero_defers_to_death():
    lim = ReactionLimiter()
    assert map_event_to_reaction({"ev": "low_health", "health": 0}, lim, 1.0, CFG) is None


def test_stuck_rate_limited_other_failures_silent():
    lim = ReactionLimiter()
    stuck = {"ev": "task_failed", "task": "follow", "reason": "stuck_timeout"}
    assert map_event_to_reaction(stuck, lim, 100.0, CFG) is not None
    assert map_event_to_reaction(stuck, lim, 150.0, CFG) is None  # < 120 dtk
    assert map_event_to_reaction(stuck, lim, 221.0, CFG) is not None
    other = {"ev": "task_failed", "task": "come", "reason": "streamer_not_visible"}
    assert map_event_to_reaction(other, lim, 300.0, CFG) is None


def test_kicked_and_deadman_once_per_session():
    lim = ReactionLimiter()
    assert map_event_to_reaction({"ev": "kicked", "reason": "x"}, lim, 1.0, CFG)
    assert map_event_to_reaction({"ev": "kicked"}, lim, 2.0, CFG) is None
    assert map_event_to_reaction({"ev": "deadman"}, lim, 3.0, CFG)
    assert map_event_to_reaction({"ev": "deadman"}, lim, 4.0, CFG) is None


def test_quiet_events_never_react():
    lim = ReactionLimiter()
    for ev in (
        {"ev": "spawned", "pos": {}},
        {"ev": "status", "health": 20},
        {"ev": "chat", "from": "bohanyto", "text": "halo"},
        {"ev": "task_done", "task": "come"},
    ):
        assert map_event_to_reaction(ev, lim, 100.0, CFG) is None


STATUS = {
    "ev": "status", "health": 18, "food": 12, "task": "follow",
    "dim": "overworld", "is_night": True, "pos": {"x": 10, "y": 64, "z": -3},
    "nearby_players": [{"name": "bohanyto", "distance": 2}],
    "nearby_hostiles": [{"kind": "creeper", "distance": 9}],
}


def test_format_context_status_and_fresh_events():
    events = [
        (100.0, {"ev": "death", "killer": ""}),
        (150.0, {"ev": "status"}),  # status TIDAK diulang di daftar kejadian
        (190.0, {"ev": "chat", "from": "bohanyto", "text": "sini arti"}),
    ]
    out = format_context(STATUS, events, ttl_sec=120.0, now=200.0)
    assert "HP 18/20" in out and "malam" in out
    assert "bohanyto (2 blok)" in out and "creeper (9 blok)" in out
    assert "kamu MATI" in out and "sini arti" in out
    assert out.count("dtk lalu") == 2


def test_format_context_ttl_drops_stale():
    events = [(10.0, {"ev": "death"})]
    out = format_context(None, events, ttl_sec=120.0, now=500.0)
    assert out == ""


def test_status_note_summary():
    note = status_note(STATUS)
    assert "HP 18/20" in note and "malam" in note and "bohanyto" in note
    assert status_note(None) == ""
