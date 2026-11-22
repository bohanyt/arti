"""Cancelled voice calls cannot discard a replacement Codex session (UNIT_TESTED only)."""

import threading
from types import SimpleNamespace

import pytest

import arti_codex_agent as codex


class _Client:
    def __init__(self):
        self.closes = 0

    def close(self):
        self.closes += 1


@pytest.fixture
def pending_call(monkeypatch):
    started, release = threading.Event(), threading.Event()
    workers, waits, health, prompts = [], [], [], []
    state = SimpleNamespace(outcome="timeout", during_wait=lambda: None)
    old_client, new_client = _Client(), _Client()

    class PendingThread:
        def run(self, _prompt, **_kwargs):
            started.set()
            assert release.wait(5), "test must release the orphaned SDK call"
            if state.outcome == "error":
                raise RuntimeError("old call failed after cancellation")
            return SimpleNamespace(final_response="" if state.outcome == "empty" else "old reply")

    class HealthyThread:
        def run(self, prompt, **_kwargs):
            prompts.append(prompt)
            return SimpleNamespace(final_response="healthy reply")

    class Worker(threading.Thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            workers.append(self)

        def join(self, timeout=None):
            if self is workers[0]:
                assert started.wait(2), "old SDK call must start before cancellation"
                waits.append(timeout)
                state.during_wait()
                if state.outcome != "timeout":
                    release.set()
                    return super().join(2)
                # Advance the old join's deadline only after the replacement reply.
                return super().join(0)
            return super().join(timeout)

    old_thread, new_thread = PendingThread(), HealthyThread()
    for name, value in {
        "_client": old_client, "_thread": old_thread, "_turns": 0,
        "_generation": 1, "_gagal_beruntun": 0, "_warming": False,
        "_logical_hold": 0, "_deferred_recycle": False, "_last_config": None,
        "_privacy_epoch": codex.screen_privacy.epoch,
    }.items():
        monkeypatch.setattr(codex, name, value)
    monkeypatch.setattr(codex, "threading", SimpleNamespace(Thread=Worker))
    monkeypatch.setattr(codex, "_buka_thread", lambda _cfg: (new_client, new_thread))
    monkeypatch.setattr(codex, "_health_catat", lambda err, **_kw: health.append(err))
    config = {"codex_agent_enabled": True, "codex_timeout_sec": 8.0,
              "codex_cold_timeout_factor": 1.5}
    state.config, state.waits, state.health, state.prompts = config, waits, health, prompts
    state.old_client, state.new_client, state.new_thread = old_client, new_client, new_thread

    def warm_replacement():
        # Run the real prewarm lifecycle with a fake SDK factory, never a server.
        with monkeypatch.context() as warming:
            warming.delenv("PYTEST_CURRENT_TEST", raising=False)
            assert codex.prewarm(config) is False
            workers[-1].join(2)
        assert codex.is_warm() and not codex._warming

    state.warm_replacement = warm_replacement
    yield state
    release.set()
    for worker in workers:
        threading.Thread.join(worker, 2)
        assert not worker.is_alive(), "test must leave no SDK worker running"


@pytest.mark.parametrize("outcome", ["timeout", "error", "empty", "success"])
def test_cancel_new_reply_then_old_failure_preserves_reusable_thread(pending_call, outcome):
    call = pending_call
    call.outcome = outcome
    replacement_generation = []

    def cancel_then_reply():
        codex.abort_turn("cancelled")
        assert call.old_client.closes == 1
        call.warm_replacement()
        assert codex.send_turn("system", "new turn", call.config) == "healthy reply"
        replacement_generation.append(codex._generation)

    call.during_wait = cancel_then_reply
    # 20:02:46 cancel -> 20:02:50 reply -> 20:02:57 old 12 s deadline.
    assert codex.send_turn("system", "old turn", call.config) is None
    assert call.waits == [12.0], "ADR 113 cold timeout remains unchanged"
    assert call.new_client.closes == 0
    assert codex._thread is call.new_thread
    assert codex._generation == replacement_generation[0]
    assert codex._turns == 1
    assert "timeout" not in call.health
    assert codex.send_turn("system", "next turn", call.config) == "healthy reply"
    assert call.prompts == ["system\n\nnew turn", "system\n\nnext turn"]
    assert codex._thread is call.new_thread and codex._turns == 2


@pytest.mark.parametrize("turns,timeout", [(0, 12.0), (3, 8.0)])
def test_current_timeout_still_closes_the_thread(pending_call, monkeypatch, turns, timeout):
    call = pending_call
    monkeypatch.setattr(codex, "_turns", turns)
    generation = codex._generation
    assert codex.send_turn("system", "current turn", call.config) is None
    assert call.waits == [timeout]
    assert call.old_client.closes == 1
    assert codex._thread is None and codex._client is None and codex._turns == 0
    assert codex._generation == generation + 1
    assert call.health == ["timeout"]


def test_cancellation_between_timeout_detection_and_close_is_also_safe(pending_call, monkeypatch):
    call = pending_call

    def cancel_during_failure_recording(err, **_kwargs):
        if err == "timeout":
            codex.abort_turn("cancelled")
            call.warm_replacement()
            assert codex.send_turn("system", "new turn", call.config) == "healthy reply"

    monkeypatch.setattr(codex, "_health_catat", cancel_during_failure_recording)
    assert codex.send_turn("system", "old turn", call.config) is None
    assert call.old_client.closes == 1 and call.new_client.closes == 0
    assert codex._thread is call.new_thread and codex._turns == 1
    assert codex.send_turn("system", "next turn", call.config) == "healthy reply"
    assert call.prompts == ["system\n\nnew turn", "system\n\nnext turn"]
