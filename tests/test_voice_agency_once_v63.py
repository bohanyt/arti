"""Livestream test 2026-11-17 (V6.3): the agency text rode both the [STARDEW] role line and the [STARDEW STATUS]
line of every voice turn (about 330 characters twice). Under ``live_voice_fixes_v1`` (mabar) one copy stays: the
status line's, which sits next to the facts it explains; with no status line the role line keeps it.
"""
import asyncio
from types import SimpleNamespace

import pytest

import arti_bridge as b
from arti_live7_status_facts import LIVE7_AGENCY_FACT

STATUS = "\n[STARDEW STATUS] " + LIVE7_AGENCY_FACT + " Sekarang aku di kebun (Farm).\n"
ROLE_BEFORE = (
    "\n\n[STARDEW] Kamu (Arti) lagi main Stardew Valley pakai karaktermu sendiri; "
    + LIVE7_AGENCY_FACT + " Jangan ngomong seolah "
    "Bohan atau penonton yang mainin karaktermu. Tidak perlu sering-sering menyebut 'kebun kita'."
)


@pytest.fixture
def stardew(monkeypatch):
    status = {"text": STATUS}
    monkeypatch.setattr(b, "_stardew_live", lambda: True)
    monkeypatch.setattr(b, "_live7_help_relay", SimpleNamespace(status_block=lambda: status["text"]))
    monkeypatch.setitem(b.CONFIG, "live7_status_facts_enabled", True)
    return status


def test_flag_off_role_line_is_unchanged(stardew, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", False)
    assert b.stardew_role_block_now() == ROLE_BEFORE
    assert (b.stardew_role_block_now() + b._live7_status_block_now()).count(LIVE7_AGENCY_FACT) == 2


def test_agency_text_rides_once_when_the_status_line_carries_it(stardew, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", True)
    role = b.stardew_role_block_now()
    assert (role + b._live7_status_block_now()).count(LIVE7_AGENCY_FACT) == 1
    assert role.startswith("\n\n[STARDEW] Kamu (Arti) lagi main Stardew Valley pakai karaktermu sendiri.")
    assert "Jangan ngomong seolah Bohan atau penonton yang mainin karaktermu." in role
    assert len(ROLE_BEFORE) - len(role) >= len(LIVE7_AGENCY_FACT)


def test_without_a_status_line_the_role_line_keeps_the_agency_text(stardew, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", True)
    stardew["text"] = ""
    assert b.stardew_role_block_now() == ROLE_BEFORE


async def _nothing(*_a, **_kw):
    pass


def test_one_voice_turn_system_prompt_carries_the_agency_text_once(stardew, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", True)
    for name in ("mood_block_now", "build_today_block", "discord_stories_block_now", "mind_intent_block_now",
                 "_alert_yield_fact_block"):
        monkeypatch.setattr(b, name, lambda: "")
    monkeypatch.setattr(b, "viewer_block_for", lambda _: "")
    monkeypatch.setattr(b, "_prepare_turn_start", _nothing)
    captured = []

    class Captured(Exception):
        pass

    async def capture(_speech, _memories, system, *_a, **_kw):
        captured.append(system)
        raise Captured

    monkeypatch.setattr(b.arti_voice_pipeline, "prepare_turn_context", capture)
    with pytest.raises(Captured):
        asyncio.run(b._handle_voice_trigger(b.VoiceTrigger("lagi ngapain?", "mic_pasif"), [], "dasar"))
    assert captured[0].count(LIVE7_AGENCY_FACT) == 1
    assert "[STARDEW] Kamu (Arti)" in captured[0] and "[STARDEW STATUS] " in captured[0]
