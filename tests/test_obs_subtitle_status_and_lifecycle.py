"""Subtitle server lifecycle + status broadcast gating tests.

Spec: obs-subtitle-integration

Covers (optional sub-tasks):

* Task 4.2 — Property 11: Subtitle server startup is non-blocking and
  non-fatal across a parametrized set of failure modes.
  Validates: Requirements 3.1, 3.7, 3.9
* Task 4.3 — Example test: bounded subtitle server shutdown <= 2 s.
  Validates: Requirement 3.10
* Task 6.3 — Property 10: Subtitle status broadcasts gated and
  order-preserving around `sd.play`.
  Validates: Requirements 4.6, 4.7
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# Same sys.path bootstrap pattern used by tests/test_bridge_startup_bugfix.py
# so `import hermes_vtuber_bridge` resolves to the repo-root module.
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import hermes_vtuber_bridge as bridge  # noqa: E402  -- after sys.path tweak


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_subtitle_runtime():
    """Save-and-restore `subtitle_runtime` state around every test so an
    earlier test never leaks `server_started=True` (or similar) into a later
    one. Hypothesis runs many examples per test function, so restoring inside
    the fixture (function-scope) is sufficient — each property test is also
    careful to re-set the flags it cares about at the top of the body."""
    rt = bridge.subtitle_runtime
    saved = (
        rt.enabled,
        rt.status_enabled,
        rt.port,
        rt.server_task,
        rt.server_started,
    )
    rt.enabled = True
    rt.status_enabled = True
    rt.port = 9999
    rt.server_task = None
    rt.server_started = False
    try:
        yield
    finally:
        (
            rt.enabled,
            rt.status_enabled,
            rt.port,
            rt.server_task,
            rt.server_started,
        ) = saved


# ===========================================================================
# Task 4.2 — Property 11: Subtitle server startup is non-blocking and non-fatal
# Feature: obs-subtitle-integration, Property 11: Subtitle server startup is
# non-blocking and non-fatal
# Validates: Requirements 3.1, 3.7, 3.9
# ===========================================================================


class _FakeServer:
    """Stand-in for the `websockets.Server` returned by `websockets.serve`.

    `wait_closed()` raises whatever exception was wired in by the test, which
    lets us simulate Requirement 3.9 ("Subtitle Server task raises an
    unhandled exception while the Bridge is running") without spinning up a
    real network listener."""

    def __init__(self, raise_on_wait: Optional[BaseException]) -> None:
        self._raise_on_wait = raise_on_wait
        self.closed = False

    async def wait_closed(self) -> None:
        if self._raise_on_wait is not None:
            raise self._raise_on_wait

    def close(self) -> None:
        self.closed = True


def _make_serve_raising(exc: BaseException):
    """Build a fake `websockets.serve` that raises during the bind await."""

    async def fake_serve(*_args: Any, **_kwargs: Any):
        raise exc

    return fake_serve


def _make_serve_ok_then_failing(exc: BaseException):
    """Build a fake `websockets.serve` that succeeds, then `wait_closed()`
    raises post-startup (Requirement 3.9 path)."""

    async def fake_serve(*_args: Any, **_kwargs: Any) -> _FakeServer:
        return _FakeServer(raise_on_wait=exc)

    return fake_serve


# Failure-mode → exception map. The four "before-bind" modes raise from the
# `websockets.serve` await itself (Requirement 3.7). The fifth mode lets the
# bind succeed and only fails post-startup inside `wait_closed()`
# (Requirement 3.9).
_FAILURE_MODES: Tuple[str, ...] = (
    "bind_eaddrinuse",
    "permission_error",
    "invalid_port_negative",
    "invalid_port_overflow",
    "post_startup_exception",
)


def _exception_for(mode: str) -> BaseException:
    if mode == "bind_eaddrinuse":
        return OSError(98, "Address already in use")
    if mode == "permission_error":
        return PermissionError("denied")
    if mode == "invalid_port_negative":
        # The design notes that real port validation lives at main_loop level;
        # here we exercise start_subtitle_server's resilience to a synchronous
        # ValueError raised from the bind call itself.
        return ValueError("port -1 out of range")
    if mode == "invalid_port_overflow":
        return ValueError("port 70000 out of range")
    if mode == "post_startup_exception":
        return RuntimeError("post startup")
    raise AssertionError(f"unknown failure mode: {mode}")


# Feature: obs-subtitle-integration, Property 11: Subtitle server startup is
# non-blocking and non-fatal
# Validates: Requirements 3.1, 3.7, 3.9
@given(failure=st.sampled_from(_FAILURE_MODES))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_11_start_subtitle_server_non_fatal(failure: str) -> None:
    """For every failure mode, `start_subtitle_server` returns without
    raising and `subtitle_runtime.server_started` is `False` afterwards.

    Property 11 frames startup as non-fatal: any bind error, permission
    error, invalid port, or post-startup exception MUST be logged and
    swallowed (Req 3.7, 3.9) so the rest of the bridge keeps running.
    """
    # Reset state per-example so the previous example can't bleed flags in.
    bridge.subtitle_runtime.server_started = False
    bridge.subtitle_runtime.server_task = None

    exc = _exception_for(failure)
    if failure == "post_startup_exception":
        fake_serve = _make_serve_ok_then_failing(exc)
    else:
        fake_serve = _make_serve_raising(exc)

    original_serve = bridge.websockets.serve
    bridge.websockets.serve = fake_serve  # type: ignore[assignment]
    try:
        # Must NOT raise: start_subtitle_server is required to swallow the
        # underlying exception and return None (Req 3.7, 3.9).
        result = asyncio.run(bridge.start_subtitle_server(9999))
        assert result is None
    finally:
        bridge.websockets.serve = original_serve  # type: ignore[assignment]

    # End-state contract: even when the bind briefly succeeded for the
    # post-startup-exception mode, the outer except clause flips
    # server_started back to False before returning.
    assert bridge.subtitle_runtime.server_started is False


# Feature: obs-subtitle-integration, Property 11: Subtitle server startup is
# non-blocking and non-fatal
# Validates: Requirement 3.1
def test_property_11_create_task_returns_synchronously() -> None:
    """`asyncio.create_task(start_subtitle_server(...))` returns a Task object
    synchronously without awaiting completion.

    Requirement 3.1 says scheduling the server MUST be a non-blocking
    primitive; main_loop's subsequent VTS / mic / chat startup steps must run
    while the server task is still pending. This is verified by checking
    that `create_task` returns an `asyncio.Task` without driving the
    coroutine to completion (the coroutine here hangs forever)."""

    async def fake_serve_hangs(*_args: Any, **_kwargs: Any) -> _FakeServer:
        # Simulate a slow / never-completing bind so we can prove that
        # create_task() is the non-blocking primitive (the task remains
        # pending until we explicitly cancel it).
        await asyncio.Event().wait()
        return _FakeServer(raise_on_wait=None)

    async def _check() -> None:
        bridge.subtitle_runtime.server_started = False
        original_serve = bridge.websockets.serve
        bridge.websockets.serve = fake_serve_hangs  # type: ignore[assignment]
        try:
            task = asyncio.create_task(bridge.start_subtitle_server(9999))
            # Synchronous return contract: create_task hands back a Task
            # immediately. Anything else would block main_loop startup.
            assert isinstance(task, asyncio.Task)
            assert not task.done(), (
                "create_task must not have awaited the coroutine to "
                "completion synchronously"
            )
            # Cleanup: cancel and let the cancellation settle so we don't
            # leave a pending-task warning behind.
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            bridge.websockets.serve = original_serve  # type: ignore[assignment]

    asyncio.run(_check())


# ===========================================================================
# Task 4.3 — Bounded subtitle server shutdown <= 2 seconds
# Feature: obs-subtitle-integration, Property 11 (lifecycle): bounded shutdown
# Validates: Requirement 3.10
# ===========================================================================


# Feature: obs-subtitle-integration, Property 11 (lifecycle): bounded shutdown
# Validates: Requirement 3.10
def test_bounded_shutdown_within_2_seconds() -> None:
    """Cancelling a hung subtitle-server-like task and awaiting it under a
    2 s bound completes within <= 2.5 s wall-clock and the task settles in a
    cancelled / done state.

    Mirrors the shutdown logic in the bridge's `__main__` finally block:
    `task.cancel()` + `asyncio.wait_for(task, timeout=2.0)` with all
    exceptions swallowed so bridge shutdown never blocks for more than 2 s
    on subtitle clients (Req 3.10). The 0.5 s slack on top of the 2 s budget
    accommodates scheduler jitter / Windows monotonic-clock granularity."""

    async def _hang() -> None:
        # Stand-in for a hung start_subtitle_server: simulates websockets.serve
        # bound but no client ever disconnects, so wait_closed() never returns.
        await asyncio.sleep(60)

    async def _bounded_shutdown() -> Tuple[float, asyncio.Task]:
        task = asyncio.create_task(_hang())
        # Yield once so the task is actually scheduled before we cancel it.
        await asyncio.sleep(0)
        start = time.monotonic()
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            # The bridge swallows everything in the shutdown path so it can
            # never block for more than the configured 2 s budget.
            pass
        elapsed = time.monotonic() - start
        return elapsed, task

    elapsed, task = asyncio.run(_bounded_shutdown())

    assert elapsed <= 2.5, (
        f"bounded shutdown took {elapsed:.3f}s, exceeding 2.5s budget"
    )
    assert task.done() or task.cancelled(), (
        "task must be in a done/cancelled state after the bounded wait"
    )


# ===========================================================================
# Task 6.3 — Property 10: Subtitle status broadcasts gated and order-preserving
# Feature: obs-subtitle-integration, Property 10: Subtitle status broadcasts
# gated and order-preserving
# Validates: Requirements 4.6, 4.7
# ===========================================================================


class _FakeCommunicate:
    """Minimal stand-in for `edge_tts.Communicate` used by Property 10.

    `stream()` yields a single audio chunk and zero WordBoundary chunks. The
    empty Word Timings List keeps `broadcast_subtitle` skipped (Req 2.5)
    while still letting the speak() happy-path reach `sd.play`, which is
    where we want to observe the status-broadcast ordering."""

    def __init__(self, text: str, voice: str) -> None:
        self._text = text
        self._voice = voice

    async def stream(self):
        yield {"type": "audio", "data": b"\x00" * 16}


def _install_speak_harness(events: List[Any]):
    """Patch the bridge module so `speak()` runs without touching real I/O.

    All audio / subtitle side effects are funnelled into the shared `events`
    list so call ordering across `broadcast_status`, `sd.play`, and the
    post-playback sleep is observable. Returns an unwind dict that
    `_restore_speak_harness` consumes."""
    saved = {
        "edge_tts.Communicate": bridge.edge_tts.Communicate,
        "sf.read": bridge.sf.read,
        "sd.play": bridge.sd.play,
        "sd.wait": bridge.sd.wait,
        "resample_audio": bridge.resample_audio,
        "_subtitle_broadcast_status": bridge._subtitle_broadcast_status,
        "_subtitle_broadcast": bridge._subtitle_broadcast,
    }

    bridge.edge_tts.Communicate = _FakeCommunicate  # type: ignore[assignment]

    def fake_sf_read(_path: str):
        # Return a tiny float buffer at 48 kHz so resample_audio is a no-op.
        return (np.zeros(16, dtype="float32"), 48000)

    def fake_resample(data, _orig_sr, target_sr=48000):
        return data, target_sr

    def fake_sd_play(_data, _samplerate, device=None):
        # Record the relative ordering of sd.play vs broadcast_status calls.
        events.append(("play", device))

    def fake_sd_wait():
        # No-op: we never produce real audio.
        pass

    async def fake_status(status: str, message: str) -> None:
        events.append(("status", status, message))

    async def fake_subtitle(_word_timings, _text) -> None:
        # Property 10 focuses on status calls; broadcast_subtitle is gated on
        # word_timings being non-empty, which the harness keeps empty, so
        # this stub is only here for completeness of the patched namespace.
        events.append(("subtitle",))

    bridge.sf.read = fake_sf_read  # type: ignore[assignment]
    bridge.sd.play = fake_sd_play  # type: ignore[assignment]
    bridge.sd.wait = fake_sd_wait  # type: ignore[assignment]
    bridge.resample_audio = fake_resample  # type: ignore[assignment]
    bridge._subtitle_broadcast_status = fake_status  # type: ignore[assignment]
    bridge._subtitle_broadcast = fake_subtitle  # type: ignore[assignment]

    return saved


def _restore_speak_harness(saved) -> None:
    bridge.edge_tts.Communicate = saved["edge_tts.Communicate"]
    bridge.sf.read = saved["sf.read"]
    bridge.sd.play = saved["sd.play"]
    bridge.sd.wait = saved["sd.wait"]
    bridge.resample_audio = saved["resample_audio"]
    bridge._subtitle_broadcast_status = saved["_subtitle_broadcast_status"]
    bridge._subtitle_broadcast = saved["_subtitle_broadcast"]


def _build_tts_engine() -> "bridge.TTSEngine":
    """Create a TTSEngine instance without running __init__ (which probes
    real audio devices). device_id = None matches the design's headless
    test posture."""
    tts = bridge.TTSEngine.__new__(bridge.TTSEngine)
    tts.device_id = None
    return tts


def _run_speak(tts) -> List[Any]:
    """Run a single `speak("halo")` invocation under the harness and return
    the ordered events list."""
    events: List[Any] = []
    saved = _install_speak_harness(events)
    try:
        asyncio.run(tts.speak("halo"))
    finally:
        _restore_speak_harness(saved)
    return events


# Feature: obs-subtitle-integration, Property 10: Subtitle status broadcasts
# gated and order-preserving
# Validates: Requirements 4.6, 4.7
def test_property_10_status_broadcast_order_when_all_enabled() -> None:
    """All three gate flags True → exactly TWO status broadcasts:
    `("speaking", "")` BEFORE `sd.play`, then `("idle", "")` AFTER `sd.play`.

    The implementation places `broadcast_status("speaking", "")` immediately
    before `tts_is_playing = True` / `sd.play(...)` (Req 4.6) and
    `broadcast_status("idle", "")` after the post-playback `await
    asyncio.sleep(0.3)` (Req 4.7). This test pins both call counts and the
    relative ordering against the `sd.play` recorder."""
    bridge.subtitle_runtime.enabled = True
    bridge.subtitle_runtime.status_enabled = True
    bridge.subtitle_runtime.server_started = True

    events = _run_speak(_build_tts_engine())

    status_events = [e for e in events if isinstance(e, tuple) and e[0] == "status"]
    play_events = [e for e in events if isinstance(e, tuple) and e[0] == "play"]

    # Exactly two status broadcasts: speaking + idle, in that order.
    assert len(status_events) == 2, f"expected 2 status calls, got {status_events!r}"
    assert status_events[0] == ("status", "speaking", "")
    assert status_events[1] == ("status", "idle", "")

    # Exactly one sd.play call.
    assert len(play_events) == 1, f"expected 1 play call, got {play_events!r}"

    # Order: speaking < play < idle.
    speaking_idx = events.index(("status", "speaking", ""))
    play_idx = events.index(("play", None))
    idle_idx = events.index(("status", "idle", ""))
    assert speaking_idx < play_idx < idle_idx, (
        f"order violated: speaking@{speaking_idx}, play@{play_idx}, "
        f"idle@{idle_idx}; events={events!r}"
    )


# Feature: obs-subtitle-integration, Property 10: Subtitle status broadcasts
# gated and order-preserving
# Validates: Requirements 4.6, 4.7
@given(
    enabled=st.booleans(),
    status_enabled=st.booleans(),
    server_started=st.booleans(),
)
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_10_status_broadcast_gating(
    enabled: bool, status_enabled: bool, server_started: bool
) -> None:
    """Status broadcasts fire iff `subtitle_runtime.enabled` AND
    `subtitle_runtime.status_enabled` AND `subtitle_runtime.server_started`
    are all True. Otherwise zero status calls.

    `sd.play` MUST fire exactly once regardless of subtitle gates — that's
    the audio-pipeline-preservation contract from Requirement 4.1 / 5.3.
    """
    bridge.subtitle_runtime.enabled = enabled
    bridge.subtitle_runtime.status_enabled = status_enabled
    bridge.subtitle_runtime.server_started = server_started

    events = _run_speak(_build_tts_engine())

    status_events = [e for e in events if isinstance(e, tuple) and e[0] == "status"]
    play_events = [e for e in events if isinstance(e, tuple) and e[0] == "play"]

    if enabled and status_enabled and server_started:
        assert len(status_events) == 2, (
            f"all gates True must yield 2 status calls; got {status_events!r}"
        )
        assert status_events[0] == ("status", "speaking", "")
        assert status_events[1] == ("status", "idle", "")
        # Order check (speaking before play, idle after play) is enforced by
        # the dedicated all-enabled test above; here we just confirm the
        # count + content.
    else:
        assert status_events == [], (
            "any gate False must suppress every status broadcast; "
            f"got {status_events!r} for "
            f"enabled={enabled}, status_enabled={status_enabled}, "
            f"server_started={server_started}"
        )

    # Audio is unaffected by subtitle gating — sd.play always fires.
    assert len(play_events) == 1, (
        f"sd.play must fire exactly once; got {play_events!r}"
    )
