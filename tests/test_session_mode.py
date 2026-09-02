"""Tests modul mode sesi murni: 4 mode, kebijakan, pemilik, jaring AFK, tag."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_session_mode as sm  # noqa: E402

CFG = {
    "initiative_quiet_sec": 30.0,
    "minecraft_narration_gap_sec": 20.0,
    "host_narration_gap_sec": 25.0,
    "owner_yt_handles": ["@streamer_test"],
}


# --- matriks mode ---------------------------------------------------------

def test_four_modes_from_two_switches():
    assert sm.resolve_mode(False, False) == sm.DUET
    assert sm.resolve_mode(False, True) == sm.DUET_GAME
    assert sm.resolve_mode(True, False) == sm.HOST_CHAT
    assert sm.resolve_mode(True, True) == sm.HOST_GAME
    assert set(sm.MODES) == {sm.DUET, sm.DUET_GAME, sm.HOST_CHAT, sm.HOST_GAME}


def test_mode_predicates():
    assert sm.is_host(sm.HOST_CHAT) and sm.is_host(sm.HOST_GAME)
    assert not sm.is_host(sm.DUET) and not sm.is_host(sm.DUET_GAME)
    assert sm.is_game(sm.DUET_GAME) and sm.is_game(sm.HOST_GAME)
    assert not sm.is_game(sm.DUET) and not sm.is_game(sm.HOST_CHAT)


def test_dormancy_only_applies_to_duet():
    """Inti spek streamer: "sepi = diam" cuma saat dia hadir & tidak ada acara."""
    assert sm.mode_policy(sm.DUET, CFG)["dormancy_applies"] is True
    for mode in (sm.DUET_GAME, sm.HOST_CHAT, sm.HOST_GAME):
        assert sm.mode_policy(mode, CFG)["dormancy_applies"] is False, mode


def test_proactive_gap_per_mode():
    assert sm.mode_policy(sm.DUET, CFG)["proactive_gap_sec"] == 30.0
    assert sm.mode_policy(sm.DUET_GAME, CFG)["proactive_gap_sec"] == 20.0
    assert sm.mode_policy(sm.HOST_CHAT, CFG)["proactive_gap_sec"] == 25.0
    assert sm.mode_policy(sm.HOST_GAME, CFG)["proactive_gap_sec"] == 20.0


def test_screen_curious_muted_only_in_game():
    assert sm.mode_policy(sm.DUET, CFG)["screen_curious_allowed"] is True
    assert sm.mode_policy(sm.HOST_CHAT, CFG)["screen_curious_allowed"] is True
    assert sm.mode_policy(sm.DUET_GAME, CFG)["screen_curious_allowed"] is False
    assert sm.mode_policy(sm.HOST_GAME, CFG)["screen_curious_allowed"] is False


def test_every_mode_has_own_scene_key():
    keys = {sm.mode_policy(m, CFG)["obs_scene_key"] for m in sm.MODES}
    assert keys == {
        "obs_scene_duet", "obs_scene_duet_game",
        "obs_scene_host_chat", "obs_scene_host_game",
    }


# --- siapa yang boleh nyuruh ---------------------------------------------

def test_handle_normalization_tolerates_at_and_case():
    # Chat YT asli TANPA '@', config PAKAI '@' — tanpa normalisasi tak pernah cocok.
    assert sm.normalize_handle("@streamer_test") == "streamertest"
    assert sm.normalize_handle(" streamertest ") == "streamertest"
    assert sm.normalize_handle("") == ""


def test_owner_chat_accepted_in_any_form():
    for form in ("streamertest", "@streamer_test", "StreamerTest", " @streamer_test "):
        assert sm.is_owner_turn("yt_chat", form, CFG) is True, form


def test_other_viewer_rejected():
    assert sm.is_owner_turn("yt_chat", "penonton_iseng", CFG) is False
    assert sm.is_owner_turn("yt_chat", None, CFG) is False
    assert sm.is_owner_turn("donation", "sultan", CFG) is False


def test_streamer_voice_and_console_are_owner():
    # "mic" = ketikan console; suara asli selalu ptt/wake_word.
    for ttype in ("ptt", "wake_word", "mic"):
        assert sm.is_owner_turn(ttype, None, CFG) is True, ttype


def test_arti_own_proactive_turns_may_change_session():
    # Kalau tidak, dia tak akan pernah bisa menyatakan misinya selesai.
    assert sm.is_owner_turn("curious", None, CFG) is True
    assert sm.is_owner_turn("game", None, CFG) is True


def test_owner_falls_back_to_yt_default_viewer():
    cfg = {"yt_default_viewer": "@streamer_test"}
    assert sm.is_owner_turn("yt_chat", "streamertest", cfg) is True
    assert sm.is_owner_turn("yt_chat", "orang_lain", cfg) is False


def test_no_owner_configured_rejects_all_chat():
    assert sm.is_owner_turn("yt_chat", "siapa_saja", {}) is False


# --- jaring AFK -----------------------------------------------------------

def test_afk_intent_positive():
    for t in (
        "eh arti aku afk ya, kamu main minecraft sana",
        "aku pergi dulu bentar",
        "kamu pegang stream-nya ya",
        "arti gantiin aku dulu",
        "ku tinggal dulu ya",
    ):
        assert sm.detect_afk_intent(t) is True, t


def test_afk_intent_negative():
    for t in (
        "nanti aku afk ya kalau udah malem",   # rencana, bukan pamit
        "kalau aku afk kamu yang pegang ya",   # pengandaian
        "jangan afk dulu dong",
        "halo semuanya apa kabar",
        "",
    ):
        assert sm.detect_afk_intent(t) is False, t


# --- tag [MODE: ...] ------------------------------------------------------

def test_mode_tag_parsed_and_stripped():
    clean, cmd = sm.parse_mode_tags("Oke aku pegang ya! [MODE: host]")
    assert cmd == "host"
    assert clean == "Oke aku pegang ya!"


def test_mode_tag_duet():
    _, cmd = sm.parse_mode_tags("Eh StreamerTest balik! [MODE: duet]")
    assert cmd == "duet"


def test_invalid_mode_tag_still_stripped():
    clean, cmd = sm.parse_mode_tags("Hmm [MODE: jadi presiden] gimana ya")
    assert cmd is None
    assert "[MODE" not in clean and "presiden" not in clean


def test_only_first_mode_tag_wins_all_stripped():
    clean, cmd = sm.parse_mode_tags("a [MODE: host] b [MODE: duet] c")
    assert cmd == "host"
    assert "[MODE" not in clean


def test_mode_tag_case_and_spacing_tolerant():
    _, cmd = sm.parse_mode_tags("siap [ mode : HOST ]")
    assert cmd == "host"


def test_no_tag_passthrough():
    clean, cmd = sm.parse_mode_tags("Halo semuanya")
    assert clean == "Halo semuanya" and cmd is None
