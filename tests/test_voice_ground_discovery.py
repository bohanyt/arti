"""V1 (mabar voice test 2026-11-17): on the mabar path her voice knows the game only from her own record.

Owner ruling: she learns Stardew by playing; handing her game facts (or reading them from model knowledge) is
cheating. The live play rules carry a discovery section on every mabar voice turn; with the flag off the prompt
is unchanged. All lines here are synthetic.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_mind  # noqa: E402
from arti_live7_status_facts import StatusFactsRelay  # noqa: E402
from arti_runtime import experience_digest, live_rules, time_grounding  # noqa: E402

ON = {"live_play_rules_enabled": True}
BASE = "Base sintetis.\n\n[MEMORI TERBARU]\nObrolan sintetis kemarin.  \n"


def section(text, header):
    """The lines of one rules section: from its header to the next header or the end."""
    start = text.index(header)
    rest = text[start + len(header):]
    nxt = rest.find("\n[")
    return rest if nxt < 0 else rest[:nxt]


def discovery():
    return section(live_rules.apply(BASE, ON), live_rules.DISCOVERY_HEADER)


def test_flag_off_prompt_is_byte_identical():
    for cfg in ({}, {"live_play_rules_enabled": False}, None):
        assert live_rules.apply(BASE, cfg).encode() == BASE.encode()


def test_flag_on_carries_the_discovery_section_once_on_every_assembly():
    out = live_rules.apply(BASE, ON)
    assert out.startswith(BASE.rstrip())
    assert out.count(live_rules.DISCOVERY_HEADER) == 1
    assert live_rules.apply(out, ON) == out  # the startup and the test-mode assembly both call apply once


def test_game_questions_are_answered_only_from_her_record():
    rule = discovery().lower()
    # the kinds of game question the owner named
    for topic in ("cara main", "warga", "tempat", "barang", "harga", "festival", "toko", "acara"):
        assert topic in rule, topic
    assert "cuma dari catatanmu" in rule
    # unknown -> she has not found out yet, in her own words; no guessing, no outside knowledge
    assert "belum nemu" in rule and "kata-katamu sendiri" in rule
    for banned in ("jangan nebak dari nama", "pengetahuan umum", "wiki"):
        assert banned in rule, banned


def test_record_blocks_named_by_the_rule_are_the_ones_the_voice_turn_carries():
    """The rule points at real blocks: a renamed block would leave the rule pointing at nothing."""
    rule = discovery()
    now = time.time()
    row = {"v": 1, "game": "stardew", "why": "Aku mau ke pantai.", "decision_ts": now - 5, "observed_ts": now - 2,
           "game_time": {"date": [1, "spring", 10], "time": 740}, "native_facts": {"location": "Farm"}}
    produced = (
        StatusFactsRelay({"live7_session_facts": ["Sesi sintetis."]}, None, clock=lambda: now).status_block()
        + arti_mind.intent_line(row, now=now)
        + time_grounding.current_line({})
        + experience_digest.TAG
    )
    for tag in ("[STARDEW STATUS]", "[NIATKU]", "[KINI]", "[NATIVE_FACTS]", "[INGATAN GAME]"):
        assert tag in rule, tag
        assert tag in produced, tag


def test_being_told_is_legitimate_and_she_says_who_told_her():
    rule = discovery().lower()
    assert "ngasih tahu" in rule and "boleh kamu ulang" in rule and "kata siapa" in rule


def test_rule_does_not_over_refuse():
    rule = discovery().lower()
    for kept in ("di luar game", "perasaanmu", "pendapatmu", "sudah kamu temukan", "dijawab biasa"):
        assert kept in rule, kept


def test_she_names_unnamed_things_herself():
    rule = discovery().lower()
    assert "belum tahu namanya" in rule and "sebut pakai kata-katamu sendiri" in rule


def test_discovery_section_survives_the_bridge_trimmer():
    """trim_system_prompt_for_llm drops optional blocks up to the next header; the rules keep their own header."""
    import arti_bridge as bridge

    prompt = live_rules.apply(BASE, ON)
    trimmed = bridge.trim_system_prompt_for_llm(prompt, {"llm_system_prompt_max_chars": len(prompt) - 10})
    assert "[MEMORI TERBARU]" not in trimmed
    assert section(trimmed, live_rules.DISCOVERY_HEADER).rstrip() == discovery().rstrip()
