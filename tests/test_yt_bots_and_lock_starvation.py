"""Dua temuan live 11,5 jam (2026-08-01) yang belum tertutup:

1. BOT CHAT MENTRIGGER JAWABAN. Menit-menit terakhir stream, @Streamlabs
   memposting leaderboard jam nonton dan bridge mencetak "Panggilan dari
   @Streamlabs terdeteksi!" + mengantri jawaban — untung keburu shutdown.
   Arti tidak boleh pernah debat sama leaderboard.

2. LOCK STARVATION SAAT THREAD CURSOR MACET. Turn "emoji batu": send_collect
   yang menggantung tetap MENGGENGGAM _session_lock sampai iteratornya mati.
   Turn berikutnya menunggu di lock (prewarm/mark_dirty/send_turn pakai
   `with` yang blocking) -> vts_mikir=75589ms, total 125 detik bisu.
   Semua pintu masuk lock sekarang wajib acquire(timeout=...) dan menyerah
   cepat ke Groq, bukan ikut tersandera.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_cursor_agent as ca
import hermes_vtuber_bridge as b


# --- bot blacklist ------------------------------------------------------------


def test_known_service_bots_are_bots():
    for name in ("@Streamlabs", "Streamlabs", "streamlabs", "@Nightbot",
                 "@StreamElements", "@Moobot", "@Fossabot"):
        assert b.is_bot_viewer(name, {}) is True, name


def test_real_viewers_are_not_bots():
    for name in ("@bohanyt", "@penontonsetia241", "@tamubaru", "Dewi-radio108"):
        assert b.is_bot_viewer(name, {}) is False, name


def test_bot_list_overridable_via_config():
    cfg = {"yt_bot_viewers": ["BotKustom"]}
    assert b.is_bot_viewer("@BotKustom", cfg) is True
    assert b.is_bot_viewer("@Streamlabs", cfg) is False, "override mengganti default"


def test_bot_gate_wired_before_wake_call():
    """Gate harus di process_message SEBELUM is_arti_wake_call — bot boleh
    tercatat di history (konteks leaderboard), tapi tidak pernah jadi trigger."""
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    gate = src.index("if is_bot_viewer(viewer")
    wake = src.index("if is_arti_wake_call(chat_msg)")
    hist = src.index('add_to_history(f"Viewer {viewer} (YouTube)"')
    assert hist < gate < wake, (
        "urutan wajib: history dulu -> gate bot -> deteksi panggilan"
    )
    assert '"yt_bot_viewers"' in src, "CONFIG kehilangan yt_bot_viewers"


# --- lock starvation ----------------------------------------------------------


def _with_lock_held(fn, max_wait=3.0):
    """Jalankan fn saat _session_lock digenggam thread lain; return (hasil, detik)."""
    release = threading.Event()
    held = threading.Event()

    def _holder():
        with ca._session_lock:
            held.set()
            release.wait(timeout=max_wait + 5)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert held.wait(timeout=2), "holder gagal menggenggam lock"
    t0 = time.monotonic()
    try:
        out = fn()
    finally:
        release.set()
        t.join(timeout=5)
    return out, time.monotonic() - t0


def test_prewarm_gives_up_fast_when_lock_held():
    out, dt = _with_lock_held(lambda: ca.prewarm({"cursor_agent_enabled": True}))
    assert out is False
    assert dt < 2.0, f"prewarm menunggu {dt:.1f}s — harus menyerah cepat ke Groq"


def test_is_warm_false_fast_when_lock_held():
    out, dt = _with_lock_held(ca.is_warm)
    assert out is False
    assert dt < 2.0


def test_send_turn_busy_fast_when_lock_held():
    out, dt = _with_lock_held(lambda: ca.send_turn("s", "u", {}))
    assert out.reason in (ca.REASON_BUSY, ca.REASON_UNAVAILABLE)
    assert dt < 3.0, f"send_turn menunggu {dt:.1f}s — turn live tersandera"


def test_mark_dirty_global_returns_fast_and_applies_later():
    ca._session = ca.CursorSession.__new__(ca.CursorSession)
    ca._session.dirty = False
    ca._session.dirty_reason = ""
    try:
        _out, dt = _with_lock_held(lambda: ca.mark_dirty_global("outer_timeout"))
        assert dt < 2.0, f"mark_dirty_global menunggu {dt:.1f}s — ikut tersandera"
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not ca._session.dirty:
            time.sleep(0.05)
        assert ca._session.dirty, "tanda rusak harus tetap diterapkan setelah lock bebas"
    finally:
        ca._session = None
