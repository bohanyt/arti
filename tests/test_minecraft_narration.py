"""Tests mode komentator saat main game (spek Bohan 2026-08-04 "like a streamer").

Inti spek: kalau Arti LAGI MAIN, dia ngoceh terus soal yang terjadi / yang
lagi dia lakuin / yang mau dia lakuin. Aturan "sepi total = diam" hanya
berlaku saat dia TIDAK main.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as ac  # noqa: E402
import arti_voice_pipeline as avp  # noqa: E402

CFG = {
    "initiative_enabled": True,
    "initiative_quiet_sec": 30.0,
    "initiative_streamer_gap_sec": 5.0,
    "initiative_dormant_after_idle_sec": 300.0,
    "initiative_backoff_base_sec": 0,
    "minecraft_narration_gap_sec": 20.0,
}
NOTE = "HP 12/20, malam, mode follow, dekat bohanyto"


def _gate_kwargs(now: float, **over):
    kw = dict(
        now=now,
        last_arti_ts=now - 25,
        last_streamer_ts=now - 60,
        tts_playing=False,
        brain_busy=False,
        ptt_active=False,
        last_human_ts=now - 3600,  # ruangan sepi 1 jam
    )
    kw.update(over)
    return kw


def _reset():
    ac._init_last_fire_ts = 0.0
    ac._init_streak = 0
    ac._used_init_materials.clear()
    ac._mc_angle_idx = 0


# --- dormansi -------------------------------------------------------------

def test_dormant_ignored_while_in_game():
    now = 100000.0
    assert ac.is_dormant(CFG, now=now, last_human_ts=now - 3600) is True
    assert ac.is_dormant(CFG, now=now, last_human_ts=now - 3600, mode="duet_game") is False


def test_initiative_silent_when_idle_but_talks_while_playing():
    _reset()
    now = 100000.0
    # Tidak main + ruangan sepi total -> diam (perilaku lama dipertahankan)
    assert ac.should_fire_initiative(CFG, **_gate_kwargs(now)) is False
    # Lagi main -> tetap ngomong walau tidak ada manusia sama sekali
    _reset()
    assert ac.should_fire_initiative(CFG, mode="duet_game", **_gate_kwargs(now)) is True


def test_in_game_uses_tighter_gap():
    _reset()
    now = 100000.0
    # 25 dtk sejak Arti bicara: < initiative_quiet_sec (30) tapi > gap game (20)
    assert ac.should_fire_initiative(
        CFG, mode="duet_game", **_gate_kwargs(now, last_human_ts=now)
    ) is True
    _reset()
    # 15 dtk: di bawah gap game -> masih tahan (bukan spam)
    assert ac.should_fire_initiative(
        CFG, mode="duet_game", **_gate_kwargs(now, last_arti_ts=now - 15, last_human_ts=now)
    ) is False


def test_in_game_still_respects_streamer_and_busy_guards():
    _reset()
    now = 100000.0
    # Bohan lagi ngomong 2 dtk lalu -> jangan motong, walau lagi main
    assert ac.should_fire_initiative(
        CFG, mode="duet_game", **_gate_kwargs(now, last_streamer_ts=now - 2)
    ) is False
    _reset()
    assert ac.should_fire_initiative(
        CFG, mode="duet_game", **_gate_kwargs(now, tts_playing=True)
    ) is False


def test_in_game_skips_backoff_escalation():
    _reset()
    cfg = dict(CFG, initiative_backoff_base_sec=180.0)
    now = 100000.0
    ac.mark_initiative_fired(now - 25)  # streak 1: backoff 180 dtk kalau berlaku
    assert ac.should_fire_initiative(
        cfg, mode="duet_game", **_gate_kwargs(now, last_human_ts=now)
    ) is True


# --- isi prompt komentar --------------------------------------------------

def test_narration_prompt_frames_as_playing_not_silence():
    _reset()
    p = ac.build_minecraft_narration_prompt(NOTE)
    assert "[Komentar main game]" in p
    assert NOTE in p
    # Framing lama ("stream lagi hening, kamu isi kekosongan") TIDAK dipakai;
    # kata "hening" hanya boleh muncul sebagai LARANGAN mengomentari sepi.
    assert "Stream lagi hening" not in p
    assert "JANGAN bilang stream-nya sepi/hening" in p
    assert "[MC: ...]" in p
    # Anti-"gapunya pendirian": dilarang minta arahan
    assert "inisiatif sendiri" in p


def test_narration_angles_rotate_through_all():
    _reset()
    seen = [
        ac.build_minecraft_narration_prompt(NOTE)
        for _ in range(len(ac._MC_NARRATION_ANGLES))
    ]
    for angle in ac._MC_NARRATION_ANGLES:
        assert any(angle in p for p in seen), f"sudut tak pernah kepakai: {angle[:40]}"
    # tidak mengulang sudut yang sama beruntun
    assert len({p for p in seen}) == len(ac._MC_NARRATION_ANGLES)


def test_initiative_prompt_switches_to_narration_when_in_game():
    _reset()
    p = ac.build_initiative_prompt(
        CFG,
        memory_bullets=["- [2026-08-01] Bohan suka kopi"],
        present_viewers=["@someone"],
        scouter_summary="obrolan soal cuaca",
        screen_hook="ada grafik aneh di layar",
        minecraft_note=NOTE,
        rng=lambda: 0.99,
    )
    # Bahan non-game SENGAJA tidak ikut saat main
    assert "[Komentar main game]" in p
    assert "kopi" not in p and "cuaca" not in p and "grafik" not in p


def test_new_viewer_greeting_still_wins_once_while_playing():
    _reset()
    join = "Barusan ada yang masuk nonton (penonton naik ke 5)."
    p = ac.build_initiative_prompt(
        CFG, minecraft_note=NOTE, viewer_join_note=join, rng=lambda: 0.5
    )
    assert join in p and "[Komentar main game]" in p
    # event yang sama tidak disapa dua kali
    p2 = ac.build_initiative_prompt(
        CFG, minecraft_note=NOTE, viewer_join_note=join, rng=lambda: 0.5
    )
    assert join not in p2


def test_no_minecraft_note_keeps_old_initiative_behaviour():
    _reset()
    p = ac.build_initiative_prompt(
        CFG, memory_bullets=["- [2026-08-01] Bohan suka kopi"], rng=lambda: 0.5
    )
    assert "[Inisiatif — buka topik sendiri]" in p
    assert "[Komentar main game]" not in p


# --- pipeline: instruksi turn ---------------------------------------------

def _prepare_curious(speech: str):
    import asyncio

    return asyncio.run(
        avp.prepare_curious_turn_context(
            speech,
            [],
            "SYS",
            {"curious_skip_rag": True},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "",
        )
    )


def test_curious_fast_path_uses_game_instruction():
    ctx = _prepare_curious("[Komentar main game]\nKamu lagi MAIN Minecraft...")
    assert "MAIN GAME" in ctx.target_instruction
    assert "layar OBS" in ctx.target_instruction  # justru dilarang
    assert "detail spesifik di layar" not in ctx.target_instruction


def test_curious_fast_path_screen_instruction_unchanged_otherwise():
    ctx = _prepare_curious("[Inisiatif — buka topik sendiri]\nStream lagi hening")
    assert "detail spesifik di layar" in ctx.target_instruction
