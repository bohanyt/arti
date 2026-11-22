"""V6.4: the owner's lines stay available to her window for 15 minutes.

On a busy stream his plan can leave the bridge's 50-line history deque within minutes, so no window over the
deque can show it when he comes back to it.
Synthetic lines only.
"""
import collections
import time

import pytest

import arti_bridge as b
from arti_runtime import voice_window as vw

NOW = time.mktime((2026, 1, 1, 20, 0, 0, 0, 0, -1))
PLAN = "Rencana malam ini: panen dulu, terus ke pantai cari kerang ya."


def stamp(ago, label, text):
    return "[%s] [%s] %s" % (time.strftime("%H:%M:%S", time.localtime(NOW - ago)), label, text)


@pytest.fixture
def busy_night(monkeypatch):
    """The plan, then 60 synthetic lines, mostly hers and chat (a few of them his): the 50-line deque has
    dropped the plan, and he said fewer than OWNER_KEEP lines since, so the store can bring it back."""
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", True)
    monkeypatch.setattr(b, "stream_history", collections.deque(maxlen=50))
    monkeypatch.setattr(vw, "_OWNER_LINES", collections.deque(maxlen=256))
    plan = stamp(482, "Streamer", PLAN)
    with b.history_lock:
        b.stream_history.append(plan)
        vw.note_owner(plan, now=NOW - 482)
        for i in range(60):
            ago = 470 - i * 7
            label = "Streamer" if i % 12 == 11 else ("Arti (VTuber)", "Viewer Sintetis Satu (YouTube)")[i % 2]
            text = "baris %d" % i
            entry = stamp(ago, label, text)
            b.stream_history.append(entry)
            if label == "Streamer":
                vw.note_owner(entry, now=NOW - ago)
    monkeypatch.setattr(vw.time, "time", lambda: NOW)
    return plan


def test_the_23_04_plan_is_gone_from_the_deque_but_in_her_window(busy_night):
    assert busy_night not in b.stream_history
    out = b.get_categorized_history()
    assert PLAN in out
    talk = out.split(vw.HEADER, 1)[1].strip().splitlines()
    assert talk[0].endswith(PLAN)                       # oldest first, in order
    assert sum("[Streamer]" in t for t in talk) <= vw.OWNER_KEEP + vw.WINDOW_LINES
    assert len(vw.HEADER) + sum(1 + len(t) for t in talk) <= vw.MAX_CHARS


def test_only_the_owners_last_8_lines_ride_beyond_the_window(busy_night):
    talk = b.get_categorized_history().split(vw.HEADER, 1)[1].strip().splitlines()
    old = [t for t in talk if "[Streamer]" in t and t not in list(b.stream_history)[-24:]]
    assert len(old) <= vw.OWNER_KEEP


def test_lines_older_than_15_minutes_leave_the_store():
    vw.clear_owner_lines()
    vw.note_owner("[22:50:00] [Streamer] lama", now=NOW - 16 * 60)
    vw.note_owner("[23:00:00] [Streamer] baru", now=NOW - 12 * 60)
    assert vw.owner_lines(now=NOW) == ["[23:00:00] [Streamer] baru"]
    vw.clear_owner_lines()


def test_a_privacy_boundary_clears_the_store_too(busy_night):
    with b.history_lock:
        b._clear_screen_privacy_context()
    assert vw.owner_lines(now=NOW) == []
    assert PLAN not in b.get_categorized_history()


def test_flag_off_feeds_nothing(monkeypatch):
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", False)
    monkeypatch.setattr(vw, "_OWNER_LINES", collections.deque(maxlen=256))
    monkeypatch.setattr(b, "stream_history", collections.deque(maxlen=50))
    monkeypatch.setattr(b, "_note_screen_privacy", lambda text: None)
    b.add_to_history("Streamer", "Coba ke sungai dulu.")
    assert list(vw._OWNER_LINES) == []


def test_more_than_8_owner_lines_since_keep_the_cap(monkeypatch):
    """The cap stays: with 9+ owner lines after it, the old plan is not shown (the prompt does not grow)."""
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", True)
    monkeypatch.setattr(vw.time, "time", lambda: NOW)
    plan = stamp(482, "Streamer", PLAN)
    owner = [plan] + [stamp(470 - i * 40, "Streamer", "baris %d" % i) for i in range(10)]
    out = vw.render([], now=NOW, owner_extra=owner)
    assert PLAN not in out and out.count("[Streamer]") == vw.OWNER_KEEP
