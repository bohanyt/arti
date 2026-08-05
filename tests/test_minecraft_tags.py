"""Tests parse_mc_tags — teks LLM tidak pernah mentah ke bot."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_minecraft import parse_mc_tags  # noqa: E402

CFG = {"minecraft_mine_allowlist": ["stone", "cobblestone", "oak_log"]}


def test_strip_and_extract_simple_verb():
    clean, cmds = parse_mc_tags("Oke aku ikutin kamu ya! [MC: follow]", CFG)
    assert clean == "Oke aku ikutin kamu ya!"
    assert cmds == [{"cmd": "follow"}]


def test_join_leave_are_bridge_verbs():
    _, cmds = parse_mc_tags("Gas! [MC: join]", CFG)
    assert cmds == [{"cmd": "join"}]
    _, cmds = parse_mc_tags("Aku cabut dulu. [MC: leave]", CFG)
    assert cmds == [{"cmd": "leave"}]


def test_only_first_valid_tag_executes_but_all_stripped():
    clean, cmds = parse_mc_tags(
        "Aku ke sana! [MC: come] eh atau diam aja [MC: stop]", CFG
    )
    assert cmds == [{"cmd": "come"}]
    assert "[MC" not in clean and "[mc" not in clean.lower()


def test_invalid_tags_stripped_even_when_game_off():
    # Tag ngaco / game off: TTS tetap bersih dari SEMUA bentuk [MC:...].
    clean, cmds = parse_mc_tags("Hmm [MC: teleport ke bulan] gimana ya", CFG)
    assert cmds == []
    assert "[MC" not in clean
    assert "teleport" not in clean


def test_say_clamped_and_control_chars_stripped():
    long_text = "a" * 200
    _, cmds = parse_mc_tags(f"[MC: say {long_text}]", CFG)
    assert len(cmds) == 1
    assert len(cmds[0]["text"]) <= 80
    _, cmds = parse_mc_tags("[MC: say halo\x07dunia]", CFG)
    assert cmds and "\x07" not in cmds[0]["text"]


def test_say_slash_command_injection_rejected():
    clean, cmds = parse_mc_tags("Hehe [MC: say /op arti_berarti]", CFG)
    assert cmds == []
    assert "/op" not in clean


def test_say_empty_rejected():
    _, cmds = parse_mc_tags("[MC: say ]", CFG)
    assert cmds == []


def test_mine_allowlist_and_count_clamp():
    _, cmds = parse_mc_tags("[MC: mine stone 500]", CFG)
    assert cmds == [{"cmd": "mine", "block": "stone", "count": 32}]
    _, cmds = parse_mc_tags("[MC: mine diamond_ore 3]", CFG)
    assert cmds == []  # di luar allowlist
    _, cmds = parse_mc_tags("[MC: mine St0ne!]", CFG)
    assert cmds == []  # nama blok tidak valid


def test_give_validates_player_and_item():
    _, cmds = parse_mc_tags("[MC: give bohanyto stone 4]", CFG)
    assert cmds == [
        {"cmd": "give", "player": "bohanyto", "item": "stone", "count": 4}
    ]
    _, cmds = parse_mc_tags("[MC: give boh@nyto stone 4]", CFG)
    assert cmds == []


def test_case_insensitive_and_spacing_tolerant():
    clean, cmds = parse_mc_tags("Siap! [mc:follow]", CFG)
    assert cmds == [{"cmd": "follow"}]
    assert clean == "Siap!"
    _, cmds = parse_mc_tags("[ MC : status ]", CFG)
    assert cmds == [{"cmd": "status"}]


def test_no_tags_passthrough():
    clean, cmds = parse_mc_tags("Halo semua, apa kabar?", CFG)
    assert clean == "Halo semua, apa kabar?"
    assert cmds == []


def test_whitespace_tidied_after_strip():
    clean, _ = parse_mc_tags("Aku jalan dulu  [MC: follow]  ya!", CFG)
    assert "  " not in clean
