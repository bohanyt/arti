"""UNIT_TESTED, ADR 113: a turn queued behind a thread's last turn stays on Luna (a53: 5 of 24 renewals)."""
import threading
import time
from types import SimpleNamespace

import pytest

import arti_codex_agent as codex

CONFIG = {"codex_agent_enabled": True, "codex_timeout_sec": 2.0, "codex_renewal_wait_sec": 0.5}


class Thread:
    def __init__(self):
        self.prompts = []

    def run(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return SimpleNamespace(final_response="balasan luna")


@pytest.fixture
def renewing(monkeypatch):
    for name, value in {"_client": None, "_thread": None, "_turns": 0, "_generation": 1, "_warming": True,
                        "_gagal_beruntun": 0, "_logical_hold": 0, "_deferred_recycle": False, "_last_config": None,
                        "_privacy_epoch": codex.screen_privacy.epoch}.items():
        monkeypatch.setattr(codex, name, value)
    monkeypatch.setattr(codex, "_health_catat", lambda *_a, **_k: None)
    timers = []

    def after(seconds, action):
        timer = threading.Timer(seconds, action)
        timers.append(timer)
        timer.start()

    yield after
    for timer in timers:
        timer.cancel()
        timer.join(2)


def finish_warm(thread):
    with codex._lock:
        codex._client, codex._thread, codex._turns, codex._warming = SimpleNamespace(close=lambda: None), thread, 0, False


def test_queued_turn_waits_for_the_new_thread_and_is_answered_by_luna(renewing):
    thread = Thread()
    renewing(0.1, lambda: finish_warm(thread))
    assert codex.send_turn("system", "giliran yang antre", CONFIG) == "balasan luna"
    assert len(thread.prompts) == 1


def test_no_warm_up_running_means_no_wait(renewing, monkeypatch):
    monkeypatch.setattr(codex, "_warming", False)
    started = time.monotonic()
    assert codex.send_turn("system", "x", CONFIG) is None
    assert time.monotonic() - started < 0.3


def test_a_warm_up_that_never_finishes_falls_through_at_the_bound(renewing):
    started = time.monotonic()
    assert codex.send_turn("system", "x", CONFIG) is None
    assert 0.4 <= time.monotonic() - started < 2.0


def test_a_discard_during_the_wait_ends_the_turn_without_sending_it(renewing):
    thread = Thread()

    def abort_then_warm():
        with codex._lock:
            codex._generation += 1
        finish_warm(thread)

    renewing(0.1, abort_then_warm)
    assert codex.send_turn("system", "giliran basi", CONFIG) is None
    assert thread.prompts == []
