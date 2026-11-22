"""Live play rules (solo stream 2026-11-15, VOICE_CHAT_ANALYSIS section 8)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_runtime import live_rules  # noqa: E402
from scripts.livepath import mabar  # noqa: E402


def test_off_by_default_and_unchanged():
    assert live_rules.apply("BASE", {}) == "BASE"


def test_on_appends_once():
    out = live_rules.apply("BASE", {"live_play_rules_enabled": True})
    assert out.startswith("BASE") and "[SAAT MAIN: CARA NGOBROL]" in out
    assert live_rules.apply(out, {"live_play_rules_enabled": True}) == out


def test_rules_cover_safety_persona_mic_and_vocabulary():
    r = live_rules.LIVE_PLAY_RULES
    for needle in ("- Jangan kasih nasihat medis atau kesehatan.", "berhenti darurat", "menyakiti diri", "emergency stop", "Saya"):
        assert needle in r


def test_mabar_turns_on_rules_without_overriding_speech_defaults(tmp_path):
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=False, mic=1, live={})
    assert cfg["live_play_rules_enabled"] is True
    for key in ("arti_reply_yt_adaptive", "arti_reply_max_sentences_yt", "arti_reply_max_sentences",
                "arti_reply_rant_chance", "arti_reply_max_chars_yt_cap"):
        assert key not in cfg
    assert "1-2 kalimat" not in live_rules.LIVE_PLAY_RULES
    assert "Kalau satu penonton nanya terus" not in live_rules.LIVE_PLAY_RULES
    assert "- Salam, pamit, kasih hati, atau jawaban atas pertanyaanmu: selalu dibalas, singkat saja." in live_rules.LIVE_PLAY_RULES


def test_no_name_ban_and_plain_honesty_about_a_stuck_game():
    """Owner 2026-11-12: "Bos" is a fact for her, not a ban; a stuck game may be named in plain words."""
    r = live_rules.LIVE_PLAY_RULES
    assert 'Panggil Bohan' not in r and "bos" not in r.lower()
    assert "bilang apa adanya" in r


def test_ambiguous_stop_gets_one_short_question():
    """V6.2: an ambiguous stop request gets one short question instead of a guess."""
    from arti_runtime import live_rules
    out = live_rules.apply("SYSTEM", {"live_play_rules_enabled": True})
    assert "Perintah berhenti atau selesai yang bisa berarti dua hal" in out
    assert "tanya sekali singkat" in out and "jangan langsung pamit" in out
    assert live_rules.apply("SYSTEM", {"live_play_rules_enabled": False}) == "SYSTEM"
