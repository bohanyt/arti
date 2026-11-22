import threading
import time

import pytest

import arti_cursor_agent as ca
import arti_web_lookup as wl
from arti_brain_contract import FlagCancellation


def test_cursor_is_skipped_under_minimum_budget(monkeypatch):
    monkeypatch.setattr(ca, "send_task", lambda *a, **k: pytest.fail("insufficient-budget launch"))
    with pytest.raises(TimeoutError, match="insufficient budget"):
        wl._call_cursor("query", {"_web_lookup_deadline": time.monotonic() + 2})


def test_cursor_receives_cancel_and_remaining_deadline(monkeypatch):
    cancel = FlagCancellation()
    deadline = time.monotonic() + 5
    captured = []
    def send(*args, **kw):
        captured.append(kw)
        return ca.CursorResult(ok=True, text="answer", model="fake", latency_ms=1)
    monkeypatch.setattr(ca, "send_task", send)
    assert wl._call_cursor("query", {"_web_lookup_deadline": deadline, "_web_lookup_cancel": cancel}) == "answer"
    assert captured == [{"cancel": cancel, "deadline": deadline}]
    cancel.cancel()
    with pytest.raises(RuntimeError, match="cancelled"):
        wl._call_cursor("query", {"_web_lookup_deadline": deadline, "_web_lookup_cancel": cancel})
    assert len(captured) == 1


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_cursor_running_lookup_closes_on_cancel_or_deadline(monkeypatch, stop):
    cancel = FlagCancellation()
    release = threading.Event()
    entered = threading.Event()
    class Session:
        config = {}
        closed = False
        dirty = False
        timeout = None
        def send_collect(self, *a, **kw):
            self.timeout = kw["timeout_s"]
            entered.set()
            release.wait(2)
            return ca.CursorResult(ok=True, text="late answer")
        def mark_dirty(self, reason):
            self.dirty = True
        def close(self, detach=False):
            self.closed = True
    session = Session()
    lock = threading.Lock()
    monkeypatch.setattr(ca, "_role_sessions", {"lookup": session})
    monkeypatch.setattr(ca, "_role_lock", lambda role: lock)
    monkeypatch.setattr(ca, "_role_breaker_allows", lambda *a: True)
    monkeypatch.setattr(ca, "is_available", lambda *a: (True, ""))
    def stop_when_entered():
        assert entered.wait(1)
        cancel.cancel()
    timer = threading.Thread(target=stop_when_entered, daemon=True) if stop == "cancel" else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        result = ca.send_task("lookup", "", "q", {}, cancel=cancel,
                              deadline=started + (.15 if stop == "deadline" else 1))
        assert not result.ok
        assert time.monotonic() - started < .7
        assert session.closed and session.dirty and not lock.locked()
        assert session.timeout <= (.151 if stop == "deadline" else 1.001)
    finally:
        release.set()
        if timer:
            timer.join(1)
