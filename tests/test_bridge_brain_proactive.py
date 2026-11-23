"""Brain batch A (owner 2026-10-21): proactive turns vs viewers, initiative
length, Stardew-aware session mode, curious off on Stardew, viewer jitter.

Pure modules with fake clocks; bridge wiring checked through monkeypatched
state and source anchors. No network, no game.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as ac  # noqa: E402
import arti_renungan as ar  # noqa: E402
import arti_reply_policy as rp  # noqa: E402
import arti_session_mode as sm  # noqa: E402
import arti_stardew_driver_guard as guard  # noqa: E402
import arti_yt_viewers as yv  # noqa: E402

BRIDGE_SRC = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
INIT_CFG = {"initiative_enabled": True, "initiative_backoff_base_sec": 0}


@pytest.fixture(autouse=True)
def _fresh():
    ac.reset_session()
    ar.reset_session()
    guard._reset_live_cache()
    yield
    ac.reset_session()
    ar.reset_session()
    guard._reset_live_cache()


# --- 1. viewer waiting -------------------------------------------------------


def _waiting(now=1000.0, queued=False, chat=0.0, trig=0.0, **cfg):
    return ac.viewer_chat_waiting(
        cfg, now=now, yt_queued=queued,
        last_viewer_chat_ts=chat, last_yt_trigger_ts=trig,
    )


def test_queued_viewer_chat_is_waiting():
    assert _waiting(queued=True) is True


def test_held_chat_newer_than_last_trigger_is_waiting():
    # chat landed 10 s ago, the last accepted yt trigger was before it
    assert _waiting(now=1000.0, chat=990.0, trig=950.0) is True


def test_triggered_chat_is_not_held():
    assert _waiting(now=1000.0, chat=990.0, trig=990.5) is False
    assert _waiting(now=1000.0, chat=0.0, trig=0.0) is False


def test_held_chat_stops_blocking_after_wait_window():
    assert _waiting(now=1000.0, chat=939.0, trig=0.0) is False  # 61 s > 60 default
    assert _waiting(now=1000.0, chat=939.0, trig=0.0,
                    proactive_viewer_wait_sec=120.0) is True


def _init_fire(now, **kw):
    return ac.should_fire_initiative(
        INIT_CFG, now=now, last_arti_ts=0.0, last_streamer_ts=0.0,
        tts_playing=False, brain_busy=False, ptt_active=False, **kw,
    )


def test_initiative_blocked_while_viewer_waiting():
    assert _init_fire(1000.0, viewer_waiting=True) is False
    assert _init_fire(1000.0, viewer_waiting=False) is True


def test_initiative_streamer_gap_default_unchanged():
    assert '"initiative_streamer_gap_sec": 5.0,' in BRIDGE_SRC


def _curious_cfg():
    return {"curious_enabled": True, "curious_interval_sec": 0,
            "curious_cooldown_sec": 0, "vision_enabled": True,
            "vision_runtime_on": True, "curious_requires_fresh_screen": False}


def _curious_ready():
    import time

    import arti_screen_context as sc

    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=time.time(), scene="Ladang di layar."))


def _curious(**kw):
    return ac.should_fire(_curious_cfg(), brain_busy=False, tts_playing=False,
                          ptt_active=False, **kw)


def test_curious_honours_viewer_wait_streamer_gap_and_stardew():
    _curious_ready()
    assert _curious(yt_queue_pending=True) is False
    assert _curious(streamer_recent=True) is False
    assert _curious(stardew_live=True) is False
    assert _curious() is True


def test_bridge_passes_new_gates():
    i = BRIDGE_SRC.index("if arti_curious.should_fire(")
    blok = BRIDGE_SRC[i:i + 1200]
    assert "yt_queue_pending=_viewer_chat_waiting()" in blok
    assert "streamer_recent=" in blok and "initiative_streamer_gap_sec" in blok
    assert "stardew_live=_stardew_live()" in blok
    j = BRIDGE_SRC.index("if arti_curious.should_fire_initiative(")
    assert "viewer_waiting=_viewer_chat_waiting()" in BRIDGE_SRC[j:j + 1200]


def test_bridge_viewer_waiting_reads_existing_state(monkeypatch):
    import time

    import arti_bridge as b

    monkeypatch.setitem(b.CONFIG, "voice_queue_enabled", False)
    monkeypatch.setattr(b, "_yt_viewers_seen", {"v1": time.time() - 5})
    monkeypatch.setattr(b, "_last_yt_chat_trigger_ts", time.time() - 30)
    assert b._viewer_chat_waiting() is True, "held chat (history only) waits"
    monkeypatch.setattr(b, "_last_yt_chat_trigger_ts", time.time())
    assert b._viewer_chat_waiting() is False, "chat already triggered"


def test_bridge_viewer_waiting_sees_queued_yt_chat(monkeypatch):
    import queue

    import arti_bridge as b

    q = queue.Queue()
    q.put(b.VoiceTrigger("[Pesan Live Chat]: x", "yt_chat", "v1"))
    monkeypatch.setitem(b.CONFIG, "voice_queue_enabled", False)
    monkeypatch.setattr(b, "voice_trigger_queue", q)
    monkeypatch.setattr(b, "_yt_viewers_seen", {})
    assert b._viewer_chat_waiting() is True


# --- 2. initiative length agrees with the reply cap --------------------------


def _init_prompt(cfg):
    return ac.build_initiative_prompt(cfg, rng=lambda: 0.0)


@pytest.mark.parametrize("cap", [None, 1, 2])
def test_initiative_prompt_length_matches_reply_cap(cap):
    cfg = {} if cap is None else {"arti_reply_inisiatif_max_sentences": cap}
    p = _init_prompt(cfg)
    m = re.search(r"Maksimal (\d+) kalimat berisi OPINI", p)
    assert m, p
    assert int(m.group(1)) == rp.resolve_reply_plan(p, cfg).sentences
    assert "Maksimal 3 kalimat berisi" not in p


# --- 3. session mode knows Stardew -------------------------------------------


def test_game_active_counts_stardew_and_minecraft():
    assert sm.game_active(minecraft=False, stardew=False) is False
    assert sm.game_active(minecraft=False, stardew=True) is True
    assert sm.game_active(minecraft=True, stardew=False) is True


def test_mode_labels_are_game_neutral():
    for mode in sm.MODES:
        assert "Minecraft" not in sm.mode_policy(mode, {})["label"], mode


def _live7(tmp_path, *, lock=False, status=None):
    d = tmp_path / "run1" / "live7" / "c1"
    d.mkdir(parents=True, exist_ok=True)
    if lock:
        (d / "supervisor.lock").write_text("x", encoding="utf-8")
    if status is not None:
        (d / "cycle_state.json").write_text('{"status": "%s"}' % status, encoding="utf-8")


def _live_now(cfg):
    """One synchronous refresh (what the background thread does), then the
    cached read the bridge main loop does."""
    guard._refresh_live_cache(cfg)
    return guard.stardew_live(cfg)


def test_stardew_live_same_definition_as_guard(tmp_path):
    cfg = {"live7_help_dir": str(tmp_path)}
    assert _live_now(cfg) is False
    assert guard.stardew_live(cfg, runtime_running=True) is True
    _live7(tmp_path, status="ready")  # resting rehearsal = not active
    assert _live_now(cfg) is False
    assert guard.active_live7_run_dir(cfg) is None
    _live7(tmp_path, status="running")
    assert _live_now(cfg) is True
    # Since the help-dir resolver (ba0dcd25) the guard names the run folder.
    assert guard.active_live7_run_dir(cfg) == str(tmp_path / "run1")


def test_stardew_live_lock_and_command_dir_parent(tmp_path):
    cmd = tmp_path / "commands"
    cmd.mkdir()
    _live7(tmp_path, lock=True)
    assert _live_now({"stardew_command_dir": str(cmd)}) is True


def _wait_for(pred, timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_stardew_live_reads_cache_and_refreshes_off_thread(tmp_path, monkeypatch):
    """REVIEW_BRAIN F1: the main-loop call never scans; a daemon thread does."""
    import threading
    import time

    scans = []
    real = guard.active_live7_cycles
    entered = threading.Event()
    release = threading.Event()
    expired = threading.Event()

    def slow_spy(config, now=None):
        scans.append(threading.current_thread())
        entered.set()
        if not release.wait(5):
            expired.set()
            raise AssertionError("foreground waited for the blocked scan")
        return real(config, now=now)

    monkeypatch.setattr(guard, "active_live7_cycles", slow_spy)
    _live7(tmp_path, lock=True)
    cfg = {"live7_help_dir": str(tmp_path), "stardew_live_cache_sec": 0.5}
    first = guard.stardew_live(cfg)
    assert first is False and not expired.is_set(), "caller returns before blocked scan"
    assert entered.wait(2)
    release.set()
    assert _wait_for(lambda: guard.stardew_live(cfg) is True)
    assert scans and all(t is not threading.main_thread() for t in scans)
    release.clear()
    for _ in range(100):  # 10 s of main-loop ticks, even while a refresh is blocked
        assert guard.stardew_live(cfg) is True
    assert not expired.is_set(), "cached reads never wait for a scan"
    assert all(t is not threading.main_thread() for t in scans)
    release.set()
    # the cycle ends -> the next background refresh flips the cached answer
    (tmp_path / "run1" / "live7" / "c1" / "supervisor.lock").unlink()
    assert _wait_for(lambda: guard.stardew_live(cfg) is False)


def test_live_refresher_never_raises_and_stops(monkeypatch, tmp_path):
    def boom(config, now=None):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(guard, "active_live7_cycles", boom)
    cfg = {"live7_help_dir": str(tmp_path)}
    assert guard.stardew_live(cfg) is False
    assert guard._refresh_live_cache(cfg) is False
    guard.stop_live_refresher()
    assert guard._refresher is None
    assert guard.stardew_live(cfg) is False  # after shutdown: cached, no restart
    assert guard._refresher is None


def test_stale_supervisor_lock_is_inactive(tmp_path):
    """REVIEW_BRAIN F2: a lock left by a hard-killed supervisor stops counting."""
    import os
    import time

    _live7(tmp_path, lock=True)
    lock = tmp_path / "run1" / "live7" / "c1" / "supervisor.lock"
    old = time.time() - guard._MAX_STATE_AGE_SEC - 60
    os.utime(lock, (old, old))
    cfg = {"live7_help_dir": str(tmp_path)}
    assert guard.active_live7_cycles(cfg) == []
    assert _live_now(cfg) is False


def test_fresh_supervisor_lock_is_active(tmp_path):
    import os
    import time

    _live7(tmp_path, lock=True)
    lock = tmp_path / "run1" / "live7" / "c1" / "supervisor.lock"
    recent = time.time() - 600
    os.utime(lock, (recent, recent))
    cfg = {"live7_help_dir": str(tmp_path)}
    cycles = guard.active_live7_cycles(cfg)
    assert len(cycles) == 1 and cycles[0].mtime == pytest.approx(recent)
    assert _live_now(cfg) is True


def test_old_lock_of_live_supervisor_stays_active(tmp_path, monkeypatch):
    """REVIEW_BRAIN_FIXES N1: live_cycle writes the lock once for a multi-day
    cycle, so a live supervisor PID wins over the lock's age."""
    import os
    import time

    _live7(tmp_path, lock=True)
    lock = tmp_path / "run1" / "live7" / "c1" / "supervisor.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")  # this pytest = a live python
    written = time.time() - 2  # written after this process started, like live_cycle
    os.utime(lock, (written, written))
    monkeypatch.setattr(guard, "_MAX_STATE_AGE_SEC", 1)  # age alone would say stale
    cfg = {"live7_help_dir": str(tmp_path)}
    if os.name == "nt":
        assert len(guard.active_live7_cycles(cfg)) == 1
    else:  # PID check unavailable: falls back to the age window
        assert guard.active_live7_cycles(cfg) == []


def test_lock_pid_reused_by_newer_python_is_inactive(tmp_path):
    """REVIEW_BRAIN_FINAL R2: a python started AFTER the lock was written is
    not the supervisor, even with the same PID."""
    import os
    import time

    _live7(tmp_path, lock=True)
    lock = tmp_path / "run1" / "live7" / "c1" / "supervisor.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    before_me = time.time() - 10 * 24 * 3600  # lock older than this process
    os.utime(lock, (before_me, before_me))
    if os.name == "nt":
        assert guard.active_live7_cycles({"live7_help_dir": str(tmp_path)}) == []


def test_fresh_lock_of_dead_pid_is_inactive(tmp_path, monkeypatch):
    _live7(tmp_path, lock=True)
    lock = tmp_path / "run1" / "live7" / "c1" / "supervisor.lock"
    lock.write_text("424242", encoding="utf-8")
    monkeypatch.setattr(guard, "_pid_is_live_python", lambda pid, mtime=None: False)
    assert guard.active_live7_cycles({"live7_help_dir": str(tmp_path)}) == []


def test_unknown_pid_answer_falls_back_to_lock_age(tmp_path, monkeypatch):
    """REVIEW_BRAIN_FINAL R1: access denied etc. -> None -> 12 h window."""
    _live7(tmp_path, lock=True)
    lock = tmp_path / "run1" / "live7" / "c1" / "supervisor.lock"
    lock.write_text("424242", encoding="utf-8")
    monkeypatch.setattr(guard, "_pid_is_live_python", lambda pid, mtime=None: None)
    assert len(guard.active_live7_cycles({"live7_help_dir": str(tmp_path)})) == 1


def test_bridge_logs_session_mode_changes_once():
    i = BRIDGE_SRC.index("_mode_now = _session_mode()")
    blok = BRIDGE_SRC[i:i + 300]
    assert "if _mode_now != _mode_logged:" in blok
    assert "(stardew_live={_stardew_live()})" in blok
    assert "_mode_logged = _mode_now" in blok


def test_bridge_session_mode_counts_stardew(monkeypatch):
    import arti_bridge as b

    monkeypatch.setattr(b, "_mc_runner_active", lambda: False)
    monkeypatch.setattr(b, "_stardew_live", lambda: True)
    monkeypatch.setattr(b, "_host_mode", False)
    assert b._session_mode() == sm.DUET_GAME
    monkeypatch.setattr(b, "_host_mode", True)
    assert b._session_mode() == sm.HOST_GAME
    monkeypatch.setattr(b, "_stardew_live", lambda: False)
    assert b._session_mode() == sm.HOST_CHAT


def test_bridge_stardew_live_uses_guard_and_runtime(monkeypatch):
    import arti_bridge as b

    seen = {}

    def fake(config, *, runtime_running=False, now=None):
        seen["rr"] = runtime_running
        return runtime_running

    monkeypatch.setattr(guard, "stardew_live", fake)
    monkeypatch.setattr(b, "_stardew_runtime", None)
    assert b._stardew_live() is False and seen["rr"] is False
    monkeypatch.setattr(b, "_stardew_runtime", object())
    assert b._stardew_live() is True and seen["rr"] is True


def test_host_game_without_minecraft_uses_host_wrapper():
    p = ac.build_initiative_prompt({}, mode=sm.HOST_GAME,
                                   vault_topic="cerita kebun", rng=lambda: 0.0)
    assert p.startswith("[Arti pegang siaran]")


def test_minecraft_host_game_still_narrates():
    p = ac.build_initiative_prompt({}, mode=sm.HOST_GAME,
                                   minecraft_note="HP 20/20, siang", rng=lambda: 0.5)
    assert "[Komentar main game]" in p


def test_renungan_treats_game_modes_like_chat_twins():
    cfg = {"renungan_enabled": True, "renungan_buka_tiap_n": 3}
    # host: opens every 2nd eligible slot (n - 1), same as host_chat
    hasil = [ar.giliran_renungan(cfg, mode="host_game", now=100.0 + i, force=True)
             for i in range(2)]
    assert hasil[0] is None and hasil[1] is not None


# --- review fix F3: Stardew keeps the pre-batch pacing ----------------------


def test_stardew_duet_game_keeps_quiet_gap_and_dormancy():
    cfg = {"initiative_quiet_sec": 30.0, "minecraft_narration_gap_sec": 20.0,
           "initiative_dormant_after_idle_sec": 300.0}
    pol = sm.mode_policy(sm.DUET_GAME, cfg, minecraft=False)
    assert pol["proactive_gap_sec"] == 30.0
    assert pol["dormancy_applies"] is True and pol["backoff_applies"] is True
    # dormant after 5 quiet minutes, like duet before the batch
    assert ac.is_dormant(cfg, now=1000.0, last_human_ts=600.0,
                         mode=sm.DUET_GAME, minecraft=False) is True
    assert ac.is_dormant(cfg, now=1000.0, last_human_ts=800.0,
                         mode=sm.DUET_GAME, minecraft=False) is False
    # host_game Stardew = host_chat pacing (25 s, no dormancy), as before
    host = sm.mode_policy(sm.HOST_GAME, cfg, minecraft=False)
    assert host["proactive_gap_sec"] == 25.0 and host["dormancy_applies"] is False


def test_minecraft_game_keeps_narration_gap_and_no_dormancy():
    cfg = {"initiative_quiet_sec": 30.0, "minecraft_narration_gap_sec": 20.0}
    for mode in (sm.DUET_GAME, sm.HOST_GAME):
        pol = sm.mode_policy(mode, cfg, minecraft=True)
        assert pol["proactive_gap_sec"] == 20.0 and pol["dormancy_applies"] is False
        assert pol["backoff_applies"] is False
    assert ac.is_dormant(cfg, now=5000.0, last_human_ts=600.0,
                         mode=sm.DUET_GAME, minecraft=True) is False


def test_stardew_pacing_config_switches():
    cfg = {"initiative_quiet_sec": 30.0, "stardew_initiative_gap_sec": 12.0,
           "stardew_dormancy_enabled": False}
    pol = sm.mode_policy(sm.DUET_GAME, cfg, minecraft=False)
    assert pol["proactive_gap_sec"] == 12.0 and pol["dormancy_applies"] is False
    assert sm.mode_policy(sm.DUET_GAME, {"stardew_initiative_gap_sec": None},
                          minecraft=False)["proactive_gap_sec"] == 30.0
    assert '"stardew_initiative_gap_sec": None,' in BRIDGE_SRC
    assert '"stardew_dormancy_enabled": True,' in BRIDGE_SRC


def test_initiative_gate_uses_stardew_gap():
    cfg = {**INIT_CFG, "initiative_quiet_sec": 30.0, "minecraft_narration_gap_sec": 20.0}

    def fire(now, minecraft):
        return ac.should_fire_initiative(
            cfg, now=now, last_arti_ts=1000.0, last_streamer_ts=0.0,
            tts_playing=False, brain_busy=False, ptt_active=False,
            last_human_ts=now - 10, mode=sm.DUET_GAME, minecraft=minecraft)

    assert fire(1022.0, minecraft=True) is True    # Minecraft: 20 s gap
    assert fire(1022.0, minecraft=False) is False  # Stardew: still 30 s
    assert fire(1031.0, minecraft=False) is True


def test_bridge_passes_minecraft_flag_to_pacing_gates():
    i = BRIDGE_SRC.index("_dormant_now = arti_curious.is_dormant(")
    assert "minecraft=_in_game_now," in BRIDGE_SRC[i:i + 300]
    j = BRIDGE_SRC.index("if arti_curious.should_fire_initiative(")
    assert "minecraft=_in_game_now," in BRIDGE_SRC[j:j + 1200]


# --- review fix F4: scouter summary is tracked initiative material -----------


def test_scouter_summary_is_not_repeated_turn_after_turn():
    cfg = {"renungan_enabled": False}
    s = "Bohan cerita soal panen stroberi"
    p1 = ac.build_initiative_prompt(cfg, scouter_summary=s, rng=lambda: 0.0)
    assert s in p1
    p2 = ac.build_initiative_prompt(cfg, scouter_summary=s, rng=lambda: 0.0)
    assert s not in p2, "same 5-min summary must not be reused next turn"
    p3 = ac.build_initiative_prompt(cfg, scouter_summary="Topik baru: ikan legenda",
                                    rng=lambda: 0.0)
    assert "ikan legenda" in p3, "a new summary is fresh material again"


def test_used_summary_does_not_block_memory_bullets_sharing_a_word():
    """REVIEW_BRAIN_FIXES N2: the summary is tracked apart from the topic ring,
    so a memory bullet sharing one word with it is still eligible."""
    cfg = {"renungan_enabled": False}
    ac.build_initiative_prompt(cfg, scouter_summary="Bohan cerita soal panen stroberi",
                               rng=lambda: 0.0)
    assert "Bohan cerita soal panen stroberi" not in ac._used_init_materials
    assert not ac._same_topic_as_used("- panen stroberi minggu lalu gagal total")


# --- 4. curious off while Stardew is live: see test_curious_honours_... -----


# --- 5. viewer-count jitter ---------------------------------------------------


def _run_filter(seq, **cfg):
    f = yv.RiseFilter(cfg)
    prev, out = None, []
    for t, c in seq:
        r = f.observe(prev, c, t)
        if r:
            out.append((t, r))
        prev = c
    return out


def test_jitter_back_to_recent_level_is_not_a_join():
    seq = [(0, 5), (30, 6), (60, 5), (90, 6), (120, 5), (150, 6), (180, 5)]
    assert _run_filter(seq) == [(30, (5, 6))], "only the first new max counts"


def test_rise_that_holds_60s_is_real():
    seq = [(0, 5), (30, 6), (60, 5), (90, 6), (120, 6), (150, 6)]
    assert _run_filter(seq) == [(30, (5, 6)), (150, (5, 6))]


def test_rise_above_recent_max_is_immediate():
    seq = [(0, 5), (30, 6), (60, 5), (90, 7)]
    assert _run_filter(seq) == [(30, (5, 6)), (90, (5, 7))]


def test_hold_zero_disables_filter():
    seq = [(0, 5), (30, 6), (60, 5), (90, 6)]
    assert _run_filter(seq, yt_viewer_rise_hold_sec=0) == [(30, (5, 6)), (90, (5, 6))]


def test_old_max_expires_from_window():
    seq = [(0, 5), (30, 6), (60, 5), (700, 6)]
    assert _run_filter(seq) == [(30, (5, 6)), (700, (5, 6))]


def test_worker_suppresses_phantom_join_with_fake_clock():
    cfg = {"youtube_chat_enabled": True, "yt_viewer_poll_sec": 30.0}
    samples = [5, 6, 5, 6, 5]
    clock = {"t": 0.0}
    ups, downs = [], []

    def fetch():
        if not samples:
            cfg["youtube_chat_enabled"] = False
            return None
        return samples.pop(0)

    def sleep(s):
        clock["t"] += s

    yv.viewer_count_worker(
        cfg, fetch_count=fetch,
        on_increase=lambda a, b: ups.append((a, b)),
        on_decrease=lambda a, b: downs.append((a, b)),
        sleep=sleep, clock=lambda: clock["t"],
    )
    assert ups == [(5, 6)]
    assert downs == [(6, 5), (6, 5)]
