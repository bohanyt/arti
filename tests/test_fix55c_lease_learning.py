"""fix55C (REVIEW2 #4): a leased move learns from a slow ACQUIRE hand-off and its waits stay cancellable.

fix54I sampled the hand-off only after a successful ACQUIRE (arti_stardew_runtime.py observe_ack after
_wait_lease) with a fixed 1.0 s acquire deadline, so an ACQUIRE slower than 1 s failed `acquire_unconfirmed` and
the lease stayed at 1500 ms; the move's result wait (up to 4.5 s) had no cancel/STOP poll. UNIT_TESTED only."""
import json
import threading
import time
from types import SimpleNamespace as NS

import pytest

import arti_stardew_command as command
import arti_stardew_runtime as runtime
from arti_runtime.move_pacing import DEFAULT_LEASE_MS, MAX_LEASE_MS, MovePacing
from tests.test_stardew_runtime_move import rig  # noqa: F401  (pytest fixture import)


def test_acquire_wait_follows_the_requested_lease():
    assert MovePacing.acquire_s(DEFAULT_LEASE_MS) == 1.0  # the unloaded value is unchanged
    assert MovePacing.acquire_s(2900) == pytest.approx(2.4)
    assert MovePacing.acquire_s(MAX_LEASE_MS) == MAX_LEASE_MS / 1000 - 0.5
    assert MovePacing.acquire_s(60_000) == MAX_LEASE_MS / 1000 - 0.5  # bounded by the native maximum


def _acquire(outcome, *, waited_s, lease_ms=DEFAULT_LEASE_MS):
    pacing, seen = MovePacing(), {}

    def wait_lease(folder, session, owner, generation, *, acquire_s):
        seen["acquire_s"] = acquire_s
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    fake = NS(_wait_lease=wait_lease, _move_pacing=lambda: pacing)
    written_at = time.monotonic() - waited_s
    try:
        result = runtime.StardewGoalRuntime._acquire_move_lease(fake, "f", "s", "o", 1, lease_ms, written_at)
    except ValueError as exc:
        result = exc
    return pacing, seen, result


def test_a_slow_acquire_that_failed_is_learned_and_the_next_lease_grows():
    pacing, seen, result = _acquire(ValueError("acquire_unconfirmed"), waited_s=1.2)
    assert str(result) == "acquire_unconfirmed" and seen["acquire_s"] == 1.0
    assert pacing.lease_ms(60) > DEFAULT_LEASE_MS  # fix54I: stayed 1500 ms after every slow hand-off
    nxt = pacing.lease_ms(60)
    pacing2, seen2, _ = _acquire("current", waited_s=0.05, lease_ms=nxt)
    assert seen2["acquire_s"] == MovePacing.acquire_s(nxt) > 1.0  # the next ACQUIRE may take that long


def test_a_successful_acquire_is_still_sampled_and_other_refusals_are_not():
    pacing, _, result = _acquire("current", waited_s=0.4)
    assert result == "current" and [round(v, 1) for v in pacing._ack] == [0.4]
    for refusal in ("lease_busy", "lease_stopped_or_faulted", "lease_binding_changed"):
        pacing, _, result = _acquire(ValueError(refusal), waited_s=3.0)
        assert str(result) == refusal and not pacing._ack  # not a hand-off time


def test_wait_lease_acquire_deadline_is_the_given_window(rig):
    rt, _, _, raw, _, _, folder = rig
    session = command.baca_sesi(folder)
    started = time.monotonic()
    with pytest.raises(ValueError, match="acquire_unconfirmed"):
        rt._wait_lease(folder, session, "never-granted", 8, acquire_s=0.3)
    assert 0.3 <= time.monotonic() - started < 0.9


def test_result_wait_stops_soon_after_a_cancel_and_stays_uncertain(tmp_path):
    flag = threading.Event()
    timer = threading.Timer(0.1, flag.set)
    timer.start()
    started = time.monotonic()
    with command.result_wait_cancelled(flag.is_set):
        result = command._tunggu_hasil(tmp_path, "req-1", 4.5)
    elapsed = time.monotonic() - started
    assert elapsed < 0.1 + command.CANCEL_WAIT_GRACE_S + 0.4  # was the whole 4.5 s
    assert result.status == command.TIDAK_PASTI and not result.sukses
    assert result.alasan.startswith("cancelled_before_result")  # never "not taken", never replayed
    assert command.result_wait_cancel.get() is None  # scoped to the move's wait


def test_a_result_landing_in_the_cancel_grace_is_still_read(tmp_path):
    def land():
        (tmp_path / command.NAMA_RESULT).write_text(json.dumps(dict(request_id="req-2")), encoding="utf-8")
    threading.Timer(0.15, land).start()
    with command.result_wait_cancelled(lambda: True):
        result = command._tunggu_hasil(tmp_path, "req-2", 4.5)
    assert result.request_id == "req-2" and "cancelled_before_result" not in (result.alasan or "")


def test_result_wait_without_a_cancel_is_unchanged(tmp_path):
    started = time.monotonic()
    result = command._tunggu_hasil(tmp_path, "req-3", 0.3)
    assert time.monotonic() - started >= 0.3 and result.status == command.TIDAK_PASTI
    assert "cancelled_before_result" not in result.alasan
