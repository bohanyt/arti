"""Tests mode "Arti pegang siaran" di bridge + inisiatif + tag pemilik.

Spek Bohan 2026-08-04: dia bisa pamit lewat SUARA atau CHAT YT, satu kalimat
bisa memicu beberapa hal ("aku afk ya, main minecraft sana, bikin rumah kecil"),
dan cuma dia yang boleh mengubah mode/misi.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as ac  # noqa: E402
import arti_minecraft as am  # noqa: E402
import arti_obs  # noqa: E402
import arti_session_mode as sm  # noqa: E402
import arti_voice_pipeline as avp  # noqa: E402
import hermes_vtuber_bridge as bridge  # noqa: E402

GATE_CFG = {
    "initiative_enabled": True,
    "initiative_quiet_sec": 30.0,
    "initiative_streamer_gap_sec": 5.0,
    "initiative_dormant_after_idle_sec": 300.0,
    "initiative_backoff_base_sec": 0,
    "host_narration_gap_sec": 25.0,
    "minecraft_narration_gap_sec": 20.0,
}


def _gate_kwargs(now: float, **over):
    kw = dict(
        now=now, last_arti_ts=now - 28, last_streamer_ts=now - 600,
        tts_playing=False, brain_busy=False, ptt_active=False,
        last_human_ts=now - 3600,  # sepi total 1 jam
    )
    kw.update(over)
    return kw


def _reset():
    ac._init_last_fire_ts = 0.0
    ac._init_streak = 0
    ac._used_init_materials.clear()
    ac._host_angle_idx = 0


# --- inisiatif: kotak yang dulu kosong ------------------------------------

def test_host_chat_keeps_talking_when_room_is_dead():
    """Inti fitur: Bohan pergi + chat sepi = Arti TETAP siaran (dulu diam)."""
    _reset()
    now = 100000.0
    assert ac.should_fire_initiative(GATE_CFG, **_gate_kwargs(now)) is False
    _reset()
    assert ac.should_fire_initiative(
        GATE_CFG, mode=sm.HOST_CHAT, **_gate_kwargs(now)
    ) is True


def test_host_chat_gap_is_between_duet_and_game():
    _reset()
    now = 100000.0
    # 28 dtk: lebih dari gap host (25), kurang dari duet (30)
    assert ac.should_fire_initiative(
        GATE_CFG, mode=sm.HOST_CHAT, **_gate_kwargs(now, last_human_ts=now)
    ) is True
    _reset()
    assert ac.should_fire_initiative(
        GATE_CFG, mode=sm.DUET, **_gate_kwargs(now, last_human_ts=now)
    ) is False


def test_host_mode_still_yields_to_streamer_and_tts():
    _reset()
    now = 100000.0
    assert ac.should_fire_initiative(
        GATE_CFG, mode=sm.HOST_CHAT, **_gate_kwargs(now, last_streamer_ts=now - 2)
    ) is False
    _reset()
    assert ac.should_fire_initiative(
        GATE_CFG, mode=sm.HOST_CHAT, **_gate_kwargs(now, tts_playing=True)
    ) is False


# --- prompt siaran solo ---------------------------------------------------

def test_host_prompt_frames_as_host_not_filler():
    _reset()
    p = ac.build_host_prompt("bahan apa pun", angle_idx=0)
    assert "[Arti pegang siaran]" in p
    assert "kamu yang pegang mic" in p
    assert "JANGAN mengeluh sepi" in p
    assert "seolah dia menjawab" in p


def test_host_angles_rotate_through_all():
    _reset()
    seen = [ac.build_host_prompt("bahan", ) for _ in range(len(ac._HOST_ANGLES))]
    for angle in ac._HOST_ANGLES:
        assert any(angle in p for p in seen), angle[:40]
    assert len(set(seen)) == len(ac._HOST_ANGLES)


def test_initiative_switches_to_host_wrapper():
    _reset()
    p = ac.build_initiative_prompt(
        GATE_CFG, mode=sm.HOST_CHAT,
        memory_bullets=["- [2026-08-01] Bohan suka kopi"], rng=lambda: 0.5,
    )
    assert "[Arti pegang siaran]" in p
    assert "[Inisiatif — buka topik sendiri]" not in p


def test_host_only_materials_ignored_in_other_modes():
    _reset()
    p = ac.build_initiative_prompt(
        GATE_CFG, mode=sm.DUET, vault_topic="rahasia vault",
        heard_note="lagu apa gitu", web_topic="berita besar", rng=lambda: 0.5,
    )
    assert "rahasia vault" not in p and "berita besar" not in p


def test_host_materials_used_and_not_repeated():
    _reset()
    kw = dict(mode=sm.HOST_CHAT, vault_topic="cerita soal kucing tetangga")
    p1 = ac.build_initiative_prompt(GATE_CFG, rng=lambda: 0.5, **kw)
    assert "kucing tetangga" in p1
    # bahan yang sama tidak diangkat dua kali dalam satu sesi
    p2 = ac.build_initiative_prompt(GATE_CFG, rng=lambda: 0.5, **kw)
    assert "kucing tetangga" not in p2


def test_game_wins_over_host_when_both(monkeypatch):
    """host_game: yang dikomentari dunia game, bukan bahan obrolan."""
    _reset()
    p = ac.build_initiative_prompt(
        GATE_CFG, mode=sm.HOST_GAME, minecraft_note="HP 20/20, siang",
        vault_topic="cerita lama", rng=lambda: 0.5,
    )
    assert "[Komentar main game]" in p and "cerita lama" not in p


# --- pipeline -------------------------------------------------------------

def _prepare_curious(speech: str):
    import asyncio

    return asyncio.run(
        avp.prepare_curious_turn_context(
            speech, [], "SYS", {"curious_skip_rag": True},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "",
        )
    )


def test_fast_path_host_instruction():
    ctx = _prepare_curious("[Arti pegang siaran]\nBohan lagi AFK...")
    assert "PEGANG SIARAN" in ctx.target_instruction
    assert "detail spesifik di layar" not in ctx.target_instruction


# --- bridge: state, gate pemilik, console ---------------------------------

def _reset_bridge(monkeypatch):
    monkeypatch.setattr(bridge, "_host_mode", False)
    monkeypatch.setattr(bridge, "_afk_armed_ts", 0.0)
    monkeypatch.setattr(bridge, "add_to_history", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_apply_session_mode_change", lambda reason: None)
    queued: list[tuple] = []
    monkeypatch.setattr(
        bridge, "queue_voice_trigger",
        lambda text, trigger_type="mic", viewer_name=None, **kw: queued.append(
            (text, trigger_type)
        ),
    )
    return queued


def test_set_host_mode_toggles_and_announces(monkeypatch):
    queued = _reset_bridge(monkeypatch)
    bridge._set_host_mode(True, "console")
    assert bridge._host_mode is True
    assert queued and queued[0][0].startswith("[Arti pegang siaran]")
    bridge._set_host_mode(False, "console")
    assert bridge._host_mode is False
    assert queued[-1][0].startswith("[Bohan balik]")


def test_set_host_mode_can_skip_announcement(monkeypatch):
    queued = _reset_bridge(monkeypatch)
    bridge._set_host_mode(True, "tag_llm", announce=False)
    assert bridge._host_mode is True and queued == []


def test_session_mode_reflects_both_switches(monkeypatch):
    _reset_bridge(monkeypatch)
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: False)
    assert bridge._session_mode() == sm.DUET
    monkeypatch.setattr(bridge, "_host_mode", True)
    assert bridge._session_mode() == sm.HOST_CHAT
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: True)
    assert bridge._session_mode() == sm.HOST_GAME


def test_host_context_block_switches_with_mode(monkeypatch):
    _reset_bridge(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "host_mode_enabled", True)
    out = bridge._append_host_context("SYS")
    assert "[SIARAN: Bohan lagi nemenin kamu.]" in out and "[MODE: host]" in out
    monkeypatch.setattr(bridge, "_host_mode", True)
    out = bridge._append_host_context("SYS")
    assert "[KAMU PEGANG SIARAN" in out and "[MODE: duet]" in out


def test_console_host_commands(monkeypatch, capsys):
    _reset_bridge(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "host_mode_enabled", True)
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: False)
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO("host on\nhost status\nhost off\n"))
    bridge.text_input_worker()
    out = capsys.readouterr().out
    assert "[Host] ON (console)" in out and "[Host] OFF (console)" in out


def test_afk_net_arms_only_on_real_goodbye(monkeypatch):
    _reset_bridge(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "host_mode_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "host_auto_after_afk_sec", 120.0)
    bridge._note_streamer_text_for_afk("nanti aku afk ya")
    assert bridge._afk_armed_ts == 0.0
    bridge._note_streamer_text_for_afk("oke arti aku afk dulu ya")
    assert bridge._afk_armed_ts > 0.0


def test_afk_net_disabled_when_gap_zero(monkeypatch):
    _reset_bridge(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "host_auto_after_afk_sec", 0)
    bridge._note_streamer_text_for_afk("aku afk dulu ya")
    assert bridge._afk_armed_ts == 0.0


# --- kalimat Bohan end-to-end (parser) ------------------------------------

def test_bohan_sentence_produces_three_commands():
    """Contoh persis dari Bohan — satu kalimat, tiga perintah."""
    reply = (
        "Siap Bohan, aku pegang ya! [MODE: host] [MC: join] "
        "[MC: goal bikin rumah kecil yang aman dari mob]"
    )
    clean, mode_cmd = sm.parse_mode_tags(reply)
    clean, cmds = am.parse_mc_tags(clean, {"minecraft_mine_allowlist": []})
    assert mode_cmd == "host"
    assert [c["cmd"] for c in cmds] == ["join", "goal"]
    assert cmds[1]["text"] == "bikin rumah kecil yang aman dari mob"
    # TTS bersih dari semua tag
    assert "[MC" not in clean and "[MODE" not in clean
    assert clean == "Siap Bohan, aku pegang ya!"


def test_lifecycle_and_goal_are_owner_only_actions_are_not():
    assert am.is_owner_only({"cmd": "join"}) is True
    assert am.is_owner_only({"cmd": "leave"}) is True
    assert am.is_owner_only({"cmd": "goal", "text": "x"}) is True
    assert am.is_owner_only({"cmd": "goal_done"}) is True
    for verb in ("follow", "roam", "come", "stop", "say", "status"):
        assert am.is_owner_only({"cmd": verb}) is False, verb


def test_one_command_per_category_no_conflicting_moves():
    _, cmds = am.parse_mc_tags(
        "[MC: follow] [MC: roam] [MC: join] [MC: leave]", {}
    )
    assert [c["cmd"] for c in cmds] == ["join", "follow"]  # urutan eksekusi


def test_goal_tag_rejects_injection_and_clamps():
    _, cmds = am.parse_mc_tags("[MC: goal /op arti_berarti]", {})
    assert cmds == []
    _, cmds = am.parse_mc_tags("[MC: goal " + "x" * 300 + "]", {})
    assert len(cmds[0]["text"]) <= am.GOAL_MAX_CHARS


# --- OBS 4 scene ----------------------------------------------------------

def test_obs_scene_per_mode():
    cfg = {
        "obs_scene_duet": "Duet", "obs_scene_duet_game": "MainBareng",
        "obs_scene_host_chat": "ArtiSolo", "obs_scene_host_game": "ArtiMain",
    }
    assert arti_obs.scene_for_mode(cfg, sm.DUET) == "Duet"
    assert arti_obs.scene_for_mode(cfg, sm.DUET_GAME) == "MainBareng"
    assert arti_obs.scene_for_mode(cfg, sm.HOST_CHAT) == "ArtiSolo"
    assert arti_obs.scene_for_mode(cfg, sm.HOST_GAME) == "ArtiMain"
    assert arti_obs.scene_for_mode({}, sm.DUET) == ""


# --- wiring bridge -> curious (pola test_initiative.py) -------------------

def test_bridge_wires_mode_to_initiative_gate():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "mode=_mode_now," in src, "gate inisiatif tidak menerima mode"
    assert "_mode_policy[\"screen_curious_allowed\"]" in src
    assert "_append_host_context(llm_system)" in src
    assert "_execute_reply_tags(" in src, "eksekusi tag tidak lewat gate"


# --- gate pemilik, diuji FUNGSIONAL (bukan cuma cek teks kode) ------------

def _capture_tag_effects(monkeypatch):
    """Jalankan _execute_reply_tags dengan efek samping direkam."""
    calls: list = []
    monkeypatch.setattr(bridge, "add_to_history", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_apply_session_mode_change", lambda reason: None)
    monkeypatch.setattr(bridge, "queue_voice_trigger", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_host_mode", False)
    monkeypatch.setattr(bridge, "_execute_mc_tag", calls.append)
    monkeypatch.setitem(bridge.CONFIG, "host_mode_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "owner_yt_handles", ["bohanyt"])
    return calls


REPLY = (
    "Oke! [MODE: host] [MC: join] [MC: goal cari desa] [MC: roam]"
)


def test_owner_voice_turn_executes_everything(monkeypatch):
    calls = _capture_tag_effects(monkeypatch)
    clean = bridge._execute_reply_tags(REPLY, "ptt", None)
    assert bridge._host_mode is True
    assert [c["cmd"] for c in calls] == ["join", "goal", "roam"]
    assert clean == "Oke!"


def test_owner_chat_turn_executes_everything(monkeypatch):
    calls = _capture_tag_effects(monkeypatch)
    bridge._execute_reply_tags(REPLY, "yt_chat", "bohanyt")
    assert bridge._host_mode is True
    assert [c["cmd"] for c in calls] == ["join", "goal", "roam"]


def test_other_viewer_cannot_change_mode_or_mission(monkeypatch):
    calls = _capture_tag_effects(monkeypatch)
    clean = bridge._execute_reply_tags(REPLY, "yt_chat", "penonton_iseng")
    # mode & misi & keluar-masuk dunia DITOLAK...
    assert bridge._host_mode is False
    assert [c["cmd"] for c in calls] == ["roam"]  # ...aksi kecil tetap boleh
    # tag tetap tidak pernah terucap
    assert "[MC" not in clean and "[MODE" not in clean


def test_artis_own_proactive_turn_may_finish_mission(monkeypatch):
    calls = _capture_tag_effects(monkeypatch)
    bridge._execute_reply_tags("Nemu! [MC: goal_done]", "game", None)
    assert [c["cmd"] for c in calls] == ["goal_done"]


def test_bridge_materials_carry_mode_and_host_sources(monkeypatch):
    monkeypatch.setattr(bridge, "_host_mode", True)
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: False)
    monkeypatch.setattr(bridge, "_host_vault_topic", lambda: "topik vault")
    monkeypatch.setattr(bridge, "_host_heard_note", lambda: "denger lagu")
    monkeypatch.setattr(bridge, "_host_web_topic_cache", "berita anu")
    mats = bridge._initiative_materials()
    assert mats["mode"] == sm.HOST_CHAT
    assert mats["vault_topic"] == "topik vault"
    assert mats["heard_note"] == "denger lagu"
    assert mats["web_topic"] == "berita anu"
