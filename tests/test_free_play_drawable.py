from types import SimpleNamespace as NS
import pytest
from scripts.free_play.drawable import DrawableWindows


def rig():
    rows = [dict(role="host", hwnd=11, pid=101), dict(role="farmhand", hwnd=12, pid=102)]
    state = {r["hwnd"]: dict(exists=True, pid=r["pid"], visible=False, iconic=True,
                              hung=False, rect=[0, 0, 640, 480]) for r in rows}
    changes = []
    def show(hwnd):
        changes.append(hwnd)
        state[hwnd].update(visible=True, iconic=False)
    api = NS(facts=lambda h: dict(state[h]), exe=lambda pid: "StardewModdingAPI.exe", show=show)
    return rows, state, changes, api


def test_both_owned_windows_restored_once_and_no_foreground_api_needed():
    rows, state, changes, api = rig()
    events = []
    guard = DrawableWindows(rows, api=api, record=events.append)
    guard.check()
    guard.check()
    assert changes == [11, 12] and len(events) == 2
    state[12]["iconic"] = True
    guard.check()
    assert changes == [11, 12, 12]


def test_foreign_reused_hwnd_prevents_all_changes():
    rows, state, changes, api = rig()
    state[12]["pid"] = 999
    with pytest.raises(RuntimeError, match="identity"):
        DrawableWindows(rows, api=api).check()
    assert changes == []


def test_failed_show_waits_then_stops_only_after_the_no_progress_bound():
    rows, state, changes, api = rig()
    api.show = lambda hwnd: None
    now, events = [0.0], []
    guard = DrawableWindows(rows, api=api, record=events.append, clock=lambda: now[0])
    guard.check()
    now[0] = 600.0
    guard.check()
    assert [e["ev"] for e in events] == ["drawable_wait", "drawable_wait"]
    assert events[1]["role"] == "farmhand" and events[1]["reasons"] == ["not_visible", "iconic"]
    now[0] = 601.0
    with pytest.raises(RuntimeError, match=r"not drawable: host \(not_visible,iconic\) for 601s"):
        guard.check()


def test_attempt5_live_window_briefly_not_drawable_is_waited_and_logged_not_stopped():
    # 22:47:26Z: the farmhand window still existed (WM_CLOSE ok); the failing fact was never logged.
    rows, state, changes, api = rig()
    now, events = [0.0], []
    guard = DrawableWindows(rows, api=api, record=events.append)
    guard.clock = lambda: now[0]
    guard.check()
    state[12]["hung"] = True
    now[0] = 30.0
    guard.check()
    assert events[-1] == dict(ev="drawable_wait", role="farmhand", hwnd=12, pid=102, reasons=["hung"], visible=True,
                              iconic=False, hung=True, rect=[0, 0, 640, 480], restored=False)
    state[12]["hung"] = False
    now[0] = 45.5
    guard.check()
    assert events[-1] == dict(ev="drawable_recovered", role="farmhand", hwnd=12, pid=102, waited_s=15.5)


def test_vanished_or_crashed_window_still_stops_at_once():
    rows, state, changes, api = rig()
    guard = DrawableWindows(rows, api=api)
    guard.check()
    state[12]["exists"] = False
    with pytest.raises(RuntimeError, match="identity"):
        guard.check()
