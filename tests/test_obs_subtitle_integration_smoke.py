"""Integration smoke tests for the OBS Subtitle Integration feature.

Spec: ``.kiro/specs/obs-subtitle-integration``

Covers two tasks from the implementation plan:

- Task 8.1 (Property 13): 50 ms latency ceiling for end-of-stream → ``sd.play``,
  plus a round-trip envelope check across a real in-process WebSocket server.
  Validates Requirements 2.5, 4.2, 4.5.

- Task 8.5: ``python subtitle_server.py`` standalone still binds 9999.
  Validates Requirement 3.4.

Both halves run as plain ``asyncio.run(...)`` coroutines because the project
does not depend on ``pytest-asyncio`` (matching the style of
``tests/test_bridge_startup_bugfix.py``). Real network only happens on the
loopback interface.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Path / import setup
# ---------------------------------------------------------------------------

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import websockets  # noqa: E402

import hermes_vtuber_bridge  # noqa: E402
import subtitle_server  # noqa: E402


PORT_9999 = 9999
ROUND_TRIP_TIMEOUT_SEC = 5.0
STANDALONE_BIND_DEADLINE_SEC = 5.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _port_is_already_bound(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True when *something* is already listening on host:port.

    Used to skip Task 8.5 cleanly if another instance of subtitle_server.py
    (or any other process) is already holding port 9999.
    """

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


async def _wait_for_client_registered(timeout: float = 2.0) -> None:
    """Spin until ``subtitle_server.connected_clients`` has at least one entry.

    The websockets handshake completes on the client side a few microseconds
    before ``handler`` adds the websocket to ``connected_clients`` on the
    server side, so we poll briefly to remove that race from the test.
    """

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if len(subtitle_server.connected_clients) >= 1:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        "subtitle_server.connected_clients stayed empty after handshake"
    )


# ---------------------------------------------------------------------------
# Task 8.1A — Round-trip envelope across the in-process server
# ---------------------------------------------------------------------------

# Feature: obs-subtitle-integration, Property 13 (round-trip half):
# broadcast_subtitle reaches a real WebSocket client with the exact envelope
# documented in design.md ("Data Models" §`subtitle` JSON envelope).
# Validates Requirements 2.5, 4.2, 4.5.
def test_round_trip_subtitle_envelope_to_websocket_client():
    """Start the bridge's subtitle server on port=0, connect a real client,
    and assert the received JSON message matches the documented envelope."""

    async def scenario():
        # Replicate the relevant call from start_subtitle_server() inline so
        # we can read the OS-assigned port off the bound server object. The
        # production helper does not return the server, hence the inline copy.
        server = await websockets.serve(
            subtitle_server.handler, "127.0.0.1", 0
        )
        try:
            bound_port = server.sockets[0].getsockname()[1]
            assert isinstance(bound_port, int)
            assert bound_port > 0

            # Snapshot connected_clients so this test does not interact with
            # any leftover state from other tests.
            previous_clients = set(subtitle_server.connected_clients)
            subtitle_server.connected_clients.clear()

            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{bound_port}"
                ) as client:
                    await _wait_for_client_registered(timeout=2.0)

                    word_timings = [
                        {"word": "hi", "start": 0.0, "duration": 0.5}
                    ]
                    await subtitle_server.broadcast_subtitle(
                        word_timings, "hi there"
                    )

                    raw = await asyncio.wait_for(
                        client.recv(), timeout=ROUND_TRIP_TIMEOUT_SEC
                    )
            finally:
                # Always restore the snapshot so neighboring tests stay clean.
                subtitle_server.connected_clients.clear()
                subtitle_server.connected_clients.update(previous_clients)

            data = json.loads(raw)
            assert data["type"] == "subtitle"
            assert data["text"] == "hi there"
            assert data["words"] == [
                {"word": "hi", "start": 0.0, "duration": 0.5}
            ]
            # The server stamps the envelope with a numeric event-loop time;
            # we don't pin the value, only the type contract from design.md.
            assert isinstance(data["timestamp"], (int, float))
        finally:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                pass  # Test must not hang on a sluggish shutdown.

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Task 8.1B — 50 ms latency ceiling for end-of-stream → sd.play
# ---------------------------------------------------------------------------


class _FakeCommunicate:
    """Drop-in for ``edge_tts.Communicate`` that records the last-byte time.

    Yields a small audio payload then a single WordBoundary so the bridge's
    subtitle path is exercised end-to-end. ``t_last_byte`` is captured AFTER
    the final audio chunk leaves the generator, which is what Property 13
    measures against (architectural absence of synchronous network waits
    between end-of-stream and ``sd.play``).
    """

    # Class-level so the test body can read it back without instance plumbing.
    t_last_byte: float | None = None

    def __init__(self, text, voice):
        # Mirror the real Communicate signature so any future signature drift
        # in TTSEngine.speak fails this test loudly. We don't use the args.
        self._text = text
        self._voice = voice

    async def stream(self):
        # Two tiny audio chunks so audio_bytes_written > 0 (Req 1.9 path
        # not triggered) and one valid WordBoundary so the subtitle broadcast
        # branch in speak() actually runs in the failure-mode variant.
        yield {"type": "audio", "data": b"\x00\x01\x02\x03"}
        yield {"type": "WordBoundary", "offset": 0, "duration": 5_000_000,
               "text": "hi"}
        yield {"type": "audio", "data": b"\x04\x05\x06\x07"}
        # Stamp t_last_byte after the final audio chunk has been yielded.
        type(self).t_last_byte = time.perf_counter()


def _install_speak_stubs(monkeypatch_targets: dict) -> dict:
    """Install patches on hermes_vtuber_bridge that neutralize real I/O.

    Returns a dict of recorded values the test reads after speak() finishes
    (notably ``t_play_start`` from the sd.play recorder).
    """

    recorded: dict = {"t_play_start": None, "play_calls": 0}

    # Sentinels for sf.read / resample_audio so they don't touch disk and
    # don't spend measurable time on the audio path. Their outputs are only
    # ever forwarded into the patched sd.play recorder, so the values can be
    # arbitrary.
    fake_audio_array = b"audio-bytes-sentinel"
    fake_sample_rate = 48000

    def _fake_sf_read(path):
        # Mirror soundfile.read's (data, samplerate) tuple shape.
        return (fake_audio_array, fake_sample_rate)

    def _fake_resample(data, orig_sr, target_sr):
        # No-op resample so failure-mode timing isolates the subtitle path.
        return (data, target_sr)

    def _fake_sd_play(data, samplerate, device=None):
        recorded["t_play_start"] = time.perf_counter()
        recorded["play_calls"] += 1

    def _fake_sd_wait():
        return None

    async def _fake_async_sleep(_seconds):
        # The post-playback await asyncio.sleep(0.3) inside speak()'s
        # finally block is irrelevant to the measurement and would just slow
        # the test down. Replace with a near-zero await.
        return None

    monkeypatch_targets["edge_tts_Communicate"] = (
        hermes_vtuber_bridge.edge_tts, "Communicate",
        getattr(hermes_vtuber_bridge.edge_tts, "Communicate"),
        _FakeCommunicate,
    )
    monkeypatch_targets["sf_read"] = (
        hermes_vtuber_bridge.sf, "read",
        getattr(hermes_vtuber_bridge.sf, "read"),
        _fake_sf_read,
    )
    monkeypatch_targets["resample_audio"] = (
        hermes_vtuber_bridge, "resample_audio",
        getattr(hermes_vtuber_bridge, "resample_audio"),
        _fake_resample,
    )
    monkeypatch_targets["sd_play"] = (
        hermes_vtuber_bridge.sd, "play",
        getattr(hermes_vtuber_bridge.sd, "play"),
        _fake_sd_play,
    )
    monkeypatch_targets["sd_wait"] = (
        hermes_vtuber_bridge.sd, "wait",
        getattr(hermes_vtuber_bridge.sd, "wait"),
        _fake_sd_wait,
    )
    # ``asyncio`` is bound at the module level in hermes_vtuber_bridge; patch
    # the .sleep attribute on the imported module reference so speak()'s
    # ``await asyncio.sleep(0.3)`` resolves to the no-op above.
    monkeypatch_targets["asyncio_sleep"] = (
        hermes_vtuber_bridge.asyncio, "sleep",
        getattr(hermes_vtuber_bridge.asyncio, "sleep"),
        _fake_async_sleep,
    )

    for _, (target, attr, _orig, replacement) in monkeypatch_targets.items():
        setattr(target, attr, replacement)

    return recorded


def _restore_speak_stubs(monkeypatch_targets: dict) -> None:
    for _, (target, attr, original, _replacement) in monkeypatch_targets.items():
        setattr(target, attr, original)


def _measure_speak_latency(*, enabled: bool, server_started: bool) -> float:
    """Run TTSEngine.speak() against the fakes and return the latency in ms.

    Latency is ``t_play_start - t_last_byte``. Both timestamps come from
    ``time.perf_counter()`` taken on opposite sides of the bridge's "after
    end-of-stream, before sd.play" decision window — exactly the surface
    Property 13 protects.
    """

    # Reset class-level last-byte stamp before each variant.
    _FakeCommunicate.t_last_byte = None

    monkeypatch_targets: dict = {}
    recorded = _install_speak_stubs(monkeypatch_targets)

    # Snapshot subtitle_runtime so this measurement does not leak into other
    # tests in the suite.
    saved_enabled = hermes_vtuber_bridge.subtitle_runtime.enabled
    saved_status_enabled = (
        hermes_vtuber_bridge.subtitle_runtime.status_enabled
    )
    saved_server_started = (
        hermes_vtuber_bridge.subtitle_runtime.server_started
    )
    saved_clients = set(subtitle_server.connected_clients)

    try:
        # Configure the failure-mode / baseline branch.
        hermes_vtuber_bridge.subtitle_runtime.enabled = enabled
        # Status broadcasts are not the surface under test here; turn them
        # off so the timing window only spans the subtitle broadcast itself.
        hermes_vtuber_bridge.subtitle_runtime.status_enabled = False
        hermes_vtuber_bridge.subtitle_runtime.server_started = server_started

        # Zero connected clients so broadcast_subtitle returns immediately.
        subtitle_server.connected_clients.clear()

        # Build a TTSEngine without going through __init__ so we don't query
        # real audio devices on this machine.
        engine = hermes_vtuber_bridge.TTSEngine.__new__(
            hermes_vtuber_bridge.TTSEngine
        )
        engine.device_id = None

        asyncio.run(engine.speak("hi there"))

        assert recorded["play_calls"] == 1, (
            "sd.play must be invoked exactly once per speak()"
        )
        assert _FakeCommunicate.t_last_byte is not None
        assert recorded["t_play_start"] is not None

        # Convert to milliseconds for human-readable assertions later.
        return (recorded["t_play_start"] - _FakeCommunicate.t_last_byte) * 1000.0
    finally:
        _restore_speak_stubs(monkeypatch_targets)
        hermes_vtuber_bridge.subtitle_runtime.enabled = saved_enabled
        hermes_vtuber_bridge.subtitle_runtime.status_enabled = (
            saved_status_enabled
        )
        hermes_vtuber_bridge.subtitle_runtime.server_started = (
            saved_server_started
        )
        subtitle_server.connected_clients.clear()
        subtitle_server.connected_clients.update(saved_clients)


# Feature: obs-subtitle-integration, Property 13: 50 ms latency ceiling for
# end-of-stream → sd.play.
# Validates Requirements 2.5, 4.2, 4.5.
def test_subtitle_path_does_not_add_more_than_50ms_to_sd_play():
    """The subtitle path must not insert a synchronous wait that delays
    playback by more than 50 ms, even when zero clients are connected."""

    # Baseline: subtitles fully disabled → no broadcast call, no gating
    # branches taken. Captures the unavoidable cost of sf.read + resample
    # (here both are no-ops, so this number is essentially the bridge's own
    # bookkeeping overhead).
    baseline_ms = _measure_speak_latency(enabled=False, server_started=False)

    # Failure mode: subtitles enabled, server "started" but zero clients.
    # ``subtitle_server.broadcast_subtitle`` short-circuits on the empty
    # ``connected_clients`` set, so the only added cost should be the
    # is-empty check plus one extra ``await``.
    failure_mode_ms = _measure_speak_latency(
        enabled=True, server_started=True
    )

    delta_ms = failure_mode_ms - baseline_ms

    # Property 13 caps the architectural delta at 50 ms.
    assert delta_ms <= 50.0, (
        "Subtitle broadcast path added "
        f"{delta_ms:.3f} ms over the no-subtitle baseline "
        f"(failure_mode={failure_mode_ms:.3f} ms, "
        f"baseline={baseline_ms:.3f} ms); Requirement 4.5 caps this at 50 ms."
    )

    # Sanity ceiling on the absolute wall-clock — the requirement is about
    # absence of synchronous network waits, not microbenchmark precision, so
    # we tolerate up to 200 ms before considering the test environment
    # itself broken.
    assert failure_mode_ms <= 200.0, (
        "Failure-mode end-of-stream → sd.play wall clock was "
        f"{failure_mode_ms:.3f} ms, which exceeds the 200 ms sanity ceiling. "
        "Either Communicate.stream() became blocking or the test machine "
        "stalled."
    )
    assert baseline_ms <= 200.0, (
        "Baseline end-of-stream → sd.play wall clock was "
        f"{baseline_ms:.3f} ms, exceeding the 200 ms sanity ceiling."
    )


# ---------------------------------------------------------------------------
# Task 8.5 — Standalone subtitle_server.py still binds 9999 (Req 3.4)
# ---------------------------------------------------------------------------

# Feature: obs-subtitle-integration, Task 8.5: ``python subtitle_server.py``
# standalone still binds 9999.
# Validates Requirement 3.4.
def test_standalone_subtitle_server_binds_default_port_9999():
    """Spawning ``subtitle_server.py`` as a subprocess must end with a
    process that is listening on TCP port 9999, exactly as the unmodified
    ``__main__`` block of the file promises."""

    if _port_is_already_bound("127.0.0.1", PORT_9999):
        pytest.skip(
            "port 9999 already in use; cannot run standalone subtitle_server "
            "smoke test (close any existing subtitle_server.py / bridge "
            "process and retry)"
        )

    proc = subprocess.Popen(
        [sys.executable, "subtitle_server.py"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        deadline = time.perf_counter() + STANDALONE_BIND_DEADLINE_SEC
        bound = False
        last_err: BaseException | None = None
        while time.perf_counter() < deadline:
            # Bail out early if the process died before binding.
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=2.0)
                pytest.fail(
                    "subtitle_server.py exited before binding port 9999. "
                    f"returncode={proc.returncode}\n"
                    f"stdout={stdout!r}\nstderr={stderr!r}"
                )
            try:
                with socket.create_connection(
                    ("127.0.0.1", PORT_9999), timeout=0.5
                ):
                    bound = True
                    break
            except (OSError, socket.timeout) as exc:
                last_err = exc
                time.sleep(0.1)

        assert bound, (
            "subtitle_server.py did not bind 127.0.0.1:9999 within "
            f"{STANDALONE_BIND_DEADLINE_SEC:.1f}s "
            f"(last connect error: {last_err!r})"
        )
    finally:
        # ``terminate`` is the documented graceful stop for the subprocess;
        # the standalone __main__ wraps asyncio.run in a KeyboardInterrupt
        # handler, so SIGTERM/Windows CTRL_BREAK_EVENT handling is fine for
        # this smoke check. We bound the wait so a hung process can't stall
        # the suite.
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)

        # We do not pin returncode to a specific value: terminate-on-Windows
        # produces a non-zero exit code that depends on the Python build,
        # and on POSIX it is -SIGTERM. We only care that the process did
        # eventually exit, which proc.wait above already guarantees.
        assert proc.returncode is not None
