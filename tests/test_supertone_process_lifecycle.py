"""Unit tests for `SupertoneProcess` lifecycle failure modes.

Covers the spawn / readiness / request / shutdown failure paths of the
bridge-side subprocess lifecycle manager (`SupertoneProcess`) described in the
Supertone 3 TTS upgrade design (sections "Subprocess Lifecycle Manager —
SupertoneProcess" and "Restart / Liveness Policy").

Validates Requirements:
    4.5  ready banner not received within READY_TIMEOUT_S -> treat as failed
    4.6  first stdout line not a valid ok:true ready banner -> treat as failed
    9.7  spawn failure marks not-ready and reports a spawn error
    9.8  not-ready subprocess respawns at most once per `speak` call

Test approach
    No real subprocess is ever spawned (we never launch `venv312` /
    `supertone_engine.py`). Instead we monkeypatch the two module-level seams the
    manager uses to reach the OS — `_resolve_venv312_python()` and
    `subprocess.Popen` — and install an in-process ``FakeProc`` that mimics the
    tiny slice of ``subprocess.Popen`` the manager touches (``poll``, ``wait``,
    ``kill``, ``stdin.write/flush/close``, ``stdout.readline``).

    Each ``SupertoneProcess`` is constructed *inside* a running event loop (via
    ``asyncio.run``) so its ``asyncio.Lock`` binds to that loop, mirroring the
    pattern in ``test_supertone_process_properties.py``.
"""

from __future__ import annotations

import asyncio
import collections
import json
import subprocess
import time

import pytest

import hermes_vtuber_bridge as bridge
from hermes_vtuber_bridge import SupertoneProcess, SupertoneError, PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# In-process fake subprocess handle
# ---------------------------------------------------------------------------
class FakeProc:
    """Minimal stand-in for the ``subprocess.Popen`` handle.

    Only the surface the manager actually uses is implemented:

    * ``poll()``           -> ``poll_result`` (``None`` means "alive").
    * ``wait(timeout)``    -> returns ``wait_return`` or raises ``wait_raises``.
    * ``kill()``           -> records the force-kill and marks the proc exited.
    * ``stdin.write/flush/close`` and ``stdout.readline``.

    Behaviour is driven by:
    * ``pending``        — a deque of lines ``stdout.readline()`` pops in order;
                           an exhausted deque yields ``""`` (EOF).
    * ``on_write``       — optional callback invoked with each written line
                           (used to echo a response back into ``pending``).
    * ``readline_hook``  — optional callable that fully overrides ``readline``
                           (used to simulate a blocking/never-returning read).
    """

    def __init__(self, *, poll_result=None):
        self.poll_result = poll_result
        self.pending: "collections.deque[str]" = collections.deque()
        self.on_write = None
        self.readline_hook = None
        self.killed = False
        self.wait_calls: list = []
        self.wait_return = 0
        self.wait_raises: "BaseException | None" = None
        self.stdin = self._Stdin(self)
        self.stdout = self._Stdout(self)
        self.stderr = None

    def poll(self):
        return self.poll_result

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_raises is not None:
            raise self.wait_raises
        return self.wait_return

    def kill(self):
        self.killed = True
        self.poll_result = 1  # process has now exited

    class _Stdin:
        def __init__(self, parent: "FakeProc"):
            self._parent = parent
            self.writes: list = []
            self.closed = False

        def write(self, line: str) -> None:
            self.writes.append(line)
            if self._parent.on_write is not None:
                self._parent.on_write(line)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class _Stdout:
        def __init__(self, parent: "FakeProc"):
            self._parent = parent

        def readline(self) -> str:
            if self._parent.readline_hook is not None:
                return self._parent.readline_hook()
            if self._parent.pending:
                return self._parent.pending.popleft()
            return ""  # EOF


def _popen_returning(fake: FakeProc):
    """Build a ``subprocess.Popen`` replacement that returns ``fake``."""

    def _popen(*args, **kwargs):
        return fake

    return _popen


# ===========================================================================
# Spawn / readiness handshake failures (ensure_alive / _spawn_locked)
# ===========================================================================
def test_missing_interpreter_raises_and_stays_not_ready(monkeypatch):
    """Missing venv312 interpreter -> ensure_alive raises, never marks ready.

    **Validates: Requirements 9.7**
    """

    def _raise():
        raise FileNotFoundError("venv312 interpreter not found")

    monkeypatch.setattr(bridge, "_resolve_venv312_python", _raise)

    async def _run():
        p = SupertoneProcess()
        with pytest.raises(FileNotFoundError):
            await p.ensure_alive()
        # Spawn aborted before Popen, so no handle and not ready (Req 9.7).
        assert p.proc is None
        assert p._ready is False

    asyncio.run(_run())


def test_spawn_oserror_propagates_and_stays_not_ready(monkeypatch):
    """``subprocess.Popen`` raising OSError propagates out of ensure_alive.

    **Validates: Requirements 9.7**
    """
    monkeypatch.setattr(bridge, "_resolve_venv312_python", lambda: "python")

    def _popen_oserror(*args, **kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(bridge.subprocess, "Popen", _popen_oserror)

    async def _run():
        p = SupertoneProcess()
        with pytest.raises(OSError):
            await p.ensure_alive()
        assert p.proc is None
        assert p._ready is False

    asyncio.run(_run())


def test_ready_banner_timeout_raises_and_stays_not_ready(monkeypatch):
    """A ready banner that never arrives within READY_TIMEOUT_S triggers timeout.

    **Validates: Requirements 4.5**
    """
    monkeypatch.setattr(bridge, "_resolve_venv312_python", lambda: "python")
    # Shrink the ready-banner ceiling so the test is fast.
    monkeypatch.setattr(bridge, "READY_TIMEOUT_S", 0.05)

    fake = FakeProc(poll_result=None)

    def _slow_readline():
        # Sleeps well past the patched READY_TIMEOUT_S so asyncio.wait_for fires
        # before this ever returns a banner.
        time.sleep(0.25)
        return json.dumps(
            {"v": PROTOCOL_VERSION, "type": "ready", "ok": True}
        ) + "\n"

    fake.readline_hook = _slow_readline
    monkeypatch.setattr(bridge.subprocess, "Popen", _popen_returning(fake))

    async def _run():
        p = SupertoneProcess()
        with pytest.raises(asyncio.TimeoutError):
            await p.ensure_alive()
        # Handle was created, but the banner never validated -> not ready (Req 4.5).
        assert p.proc is fake
        assert p._ready is False

    asyncio.run(_run())


def test_ok_false_ready_banner_raises_supertone_error(monkeypatch):
    """A ready banner with ok:false (MODEL_LOAD_FAILED) is treated as failure.

    **Validates: Requirements 4.6**
    """
    monkeypatch.setattr(bridge, "_resolve_venv312_python", lambda: "python")

    fake = FakeProc(poll_result=None)
    fake.pending.append(
        json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "type": "ready",
                "ok": False,
                "error": {"code": "MODEL_LOAD_FAILED", "message": "boom"},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(bridge.subprocess, "Popen", _popen_returning(fake))

    async def _run():
        p = SupertoneProcess()
        with pytest.raises(SupertoneError) as excinfo:
            await p.ensure_alive()
        assert excinfo.value.code == "MODEL_LOAD_FAILED"
        assert p._ready is False

    asyncio.run(_run())


# ===========================================================================
# request() failure modes (timeout / EOF / desync mark not-ready)
# ===========================================================================
def test_request_timeout_marks_not_ready(monkeypatch):
    """A response that never arrives within the per-request timeout raises and
    marks the subprocess not ready.

    **Validates: Requirements 9.8**
    """

    async def _run():
        p = SupertoneProcess()
        p._ready = True
        fake = FakeProc(poll_result=None)

        def _slow_readline():
            time.sleep(0.25)
            return json.dumps({"v": PROTOCOL_VERSION, "id": 1, "ok": True}) + "\n"

        fake.readline_hook = _slow_readline
        p.proc = fake

        with pytest.raises(asyncio.TimeoutError):
            await p.request({"type": "synthesize", "text": "hi"}, timeout=0.05)

        # The request was written, but the stalled read marked it not-ready so the
        # next speak() respawns a fresh engine (Req 9.8).
        assert p._ready is False
        assert len(fake.stdin.writes) == 1

    asyncio.run(_run())


def test_request_eof_raises_and_marks_not_ready(monkeypatch):
    """EOF (empty readline) mid-stream surfaces as a SupertoneError(EOF).

    **Validates: Requirements 9.8**
    """

    async def _run():
        p = SupertoneProcess()
        p._ready = True
        # No pending responses + no readline_hook -> readline() returns "" (EOF).
        p.proc = FakeProc(poll_result=None)

        with pytest.raises(SupertoneError) as excinfo:
            await p.request({"type": "synthesize", "text": "hi"}, timeout=1.0)
        assert excinfo.value.code == "EOF"
        assert p._ready is False

    asyncio.run(_run())


def test_request_desync_raises_and_marks_not_ready(monkeypatch):
    """A response whose id does not match the request id raises DESYNC.

    **Validates: Requirements 9.8**
    """

    async def _run():
        p = SupertoneProcess()
        p._ready = True
        fake = FakeProc(poll_result=None)

        def _echo_wrong_id(line: str) -> None:
            req = json.loads(line)
            # Deliberately wrong id to provoke desynchronization detection.
            resp = {
                "v": PROTOCOL_VERSION,
                "id": req["id"] + 1000,
                "ok": True,
                "wav_path": "/tmp/x.wav",
                "duration": 1.0,
            }
            fake.pending.append(json.dumps(resp) + "\n")

        fake.on_write = _echo_wrong_id
        p.proc = fake

        with pytest.raises(SupertoneError) as excinfo:
            await p.request({"type": "synthesize", "text": "hi"}, timeout=1.0)
        assert excinfo.value.code == "DESYNC"
        assert p._ready is False

    asyncio.run(_run())


# ===========================================================================
# Single-respawn cap (at most one spawn per ensure_alive call)
# ===========================================================================
def test_ensure_alive_spawns_at_most_once_per_call(monkeypatch):
    """ensure_alive triggers at most one spawn per call and skips spawning when
    the subprocess is already live and ready.

    **Validates: Requirements 9.8**
    """

    async def _run():
        p = SupertoneProcess()
        calls = {"n": 0}

        async def _fake_spawn():
            # Simulate a successful spawn + readiness handshake.
            calls["n"] += 1
            p.proc = FakeProc(poll_result=None)
            p._ready = True

        p._spawn_locked = _fake_spawn

        # 1) Fresh manager (no proc, not ready) -> exactly one spawn.
        await p.ensure_alive()
        assert calls["n"] == 1

        # 2) Already live + ready -> no additional spawn.
        await p.ensure_alive()
        assert calls["n"] == 1

        # 3) Marked not-ready after a failure -> exactly one more spawn.
        p._ready = False
        await p.ensure_alive()
        assert calls["n"] == 2

    asyncio.run(_run())


# ===========================================================================
# shutdown() — graceful exit and force-kill on wait timeout
# ===========================================================================
def test_shutdown_graceful_closes_stdin_and_clears_state(monkeypatch):
    """shutdown writes the shutdown line, closes stdin, waits, clears state.

    **Validates: Requirements 9.8** (post-shutdown a later ensure_alive respawns)
    """

    async def _run():
        p = SupertoneProcess()
        fake = FakeProc(poll_result=None)
        p.proc = fake
        p._ready = True

        await p.shutdown()

        # A shutdown control message was written as a single NDJSON line.
        assert len(fake.stdin.writes) == 1
        line = fake.stdin.writes[0]
        assert line.endswith("\n")
        assert json.loads(line) == {"v": PROTOCOL_VERSION, "type": "shutdown"}
        # stdin closed as EOF backup, proc was waited on (not force-killed).
        assert fake.stdin.closed is True
        assert fake.wait_calls == [5]
        assert fake.killed is False
        # State cleared so a later ensure_alive() spawns fresh.
        assert p.proc is None
        assert p._ready is False

    asyncio.run(_run())


def test_shutdown_force_kills_on_wait_timeout(monkeypatch):
    """If the subprocess does not exit within 5s, shutdown force-kills it.

    **Validates: Requirements 9.8**
    """

    async def _run():
        p = SupertoneProcess()
        fake = FakeProc(poll_result=None)
        fake.wait_raises = subprocess.TimeoutExpired(cmd="supertone_engine.py", timeout=5)
        p.proc = fake
        p._ready = True

        await p.shutdown()

        # wait() timed out -> force-kill, no orphan left behind.
        assert fake.wait_calls == [5]
        assert fake.killed is True
        assert p.proc is None
        assert p._ready is False

    asyncio.run(_run())
