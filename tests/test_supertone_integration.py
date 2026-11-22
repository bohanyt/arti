"""Gated integration test for the real ``supertone_engine.py`` subprocess.

This test spawns the *actual* Supertone synthesis subprocess under the
Python 3.12 ``venv312`` interpreter (the one that has the ~260-400MB
``supertonic`` model installed) and exercises the full NDJSON round-trip:

    spawn -> readiness handshake -> ping/pong -> synthesize -> shutdown

It is **skipped by default** because it loads the real model and requires the
``venv312`` environment. It only runs when BOTH of these hold:

* the opt-in env var ``RUN_SUPERTONE_INTEGRATION=1`` is set, AND
* the venv312 Python 3.12 interpreter can be resolved (either via the
  ``SUPERTONE_VENV312_PYTHON`` env var or
  ``arti_bridge._resolve_venv312_python()``).

If either condition is missing the test ``pytest.skip(...)``s cleanly, so it
never fails in CI or on a dev box where venv312/supertonic is absent.

It drives the subprocess directly with ``subprocess.Popen`` (argv list, no
shell), mirroring ``SupertoneProcess._spawn_locked`` (``text=True``,
``encoding="utf-8"``, ``bufsize=1``), and always terminates the process in a
fixture cleanup even if assertions fail.

**Sample-rate contract.** The engine does NOT pick a sample rate: it hands the
model's buffer straight to ``_tts.save_audio()`` (``supertone_engine.py`` step
5) without ever naming a rate, so the WAV comes out at whatever the supertonic
model emits natively. The bridge owns normalisation instead --
``_play_wav()`` resamples "only when the source sample rate differs"
(``arti_bridge.py``, ``resample_audio(raw_data, raw_sr, 48000)``).
So this test asserts only what the subprocess actually promises -- a readable
WAV with a positive sample rate, audible (non-silent) samples, and a positive
duration -- and leaves the resampling contract to the bridge's own tests. It
must NOT assert a specific native rate: real runtime output on 2026-09-02 was
mono 44100 Hz, and an earlier ``samplerate == 48000`` assertion here encoded a
requirement that no design doc, no engine code path, and no bridge code path
ever made.

_Requirements: 8.1, 8.2, 8.9_
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

# --------------------------------------------------------------------------- #
# Opt-in / interpreter resolution
# --------------------------------------------------------------------------- #

#: Set this to "1" to actually run the heavy round-trip against the real model.
INTEGRATION_OPT_IN_ENV = "RUN_SUPERTONE_INTEGRATION"

#: Optional explicit path to the Python 3.12 (venv312) interpreter. Takes
#: precedence over the bridge's resolver when set.
VENV312_PYTHON_ENV = "SUPERTONE_VENV312_PYTHON"

#: Protocol version hardcoded by both bridge and subprocess (see design).
PROTOCOL_VERSION = 1

#: Queue sentinel meaning "the subprocess closed stdout". Distinct from a real
#: line so an EOF is never mistaken for a blank line.
_EOF = object()

#: Repo root. ``supertone_engine.py`` and ``text_preprocessor.py`` live HERE,
#: one level ABOVE tests/ -- this file used to join against its own directory
#: and therefore spawned a path that has never existed, so the round-trip could
#: not pass even against a perfectly healthy runtime.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The exact script the bridge spawns. ``SupertoneProcess._spawn_locked()``
#: builds this as ``os.path.join(dirname(abspath(bridge __file__)),
#: "supertone_engine.py")`` -- i.e. next to the bridge, at the repo root.
ENGINE_PATH = os.path.join(REPO_ROOT, "supertone_engine.py")

#: Per-operation read ceilings (seconds). Generous to allow first-run model
#: download (readiness) and CPU synthesis.
READY_TIMEOUT_S = 180.0
SYNTH_TIMEOUT_S = 120.0
SHUTDOWN_TIMEOUT_S = 15.0


def _resolve_venv312_interpreter() -> "str | None":
    """Resolve the venv312 Python 3.12 interpreter path, or ``None``.

    Prefers the explicit ``SUPERTONE_VENV312_PYTHON`` override; otherwise defers
    to ``arti_bridge._resolve_venv312_python()``. Any failure (missing
    interpreter, import error, etc.) returns ``None`` so the caller can skip.
    """
    override = os.environ.get(VENV312_PYTHON_ENV)
    if override:
        return override if os.path.isfile(override) else None
    try:
        from arti_bridge import _resolve_venv312_python

        return _resolve_venv312_python()
    except Exception:
        # FileNotFoundError (no venv312), ImportError (bridge deps absent),
        # or anything else => treat as "not available" and let the test skip.
        return None


# Opt-in gate evaluated at collection time. Cheap (env var only) so we never
# import the heavy bridge module just to decide whether to skip in CI.
#
# Deliberately NOT a module-level ``pytestmark``: the static engine-path guard
# below has to run in every ordinary pytest run (it loads no model, needs no
# venv312, spawns nothing). Only the real round-trip is gated.
requires_real_engine = pytest.mark.skipif(
    os.environ.get(INTEGRATION_OPT_IN_ENV) != "1",
    reason=(
        "Supertone real-subprocess integration test is opt-in: loads the "
        "~260-400MB model and needs venv312. Set "
        f"{INTEGRATION_OPT_IN_ENV}=1 to run."
    ),
)


# --------------------------------------------------------------------------- #
# NDJSON client over the spawned subprocess
# --------------------------------------------------------------------------- #


class _EngineClient:
    """Thin NDJSON helper around the spawned ``supertone_engine.py`` process."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._next_id = 0
        # Lines are read on a daemon thread and handed over through a Queue,
        # because Queue.get() honours a timeout on Windows and readline() does
        # not. Daemon so a wedged reader can never keep pytest from exiting.
        self._lines: "queue.Queue" = queue.Queue()
        self._reader = threading.Thread(
            target=self._pump, name="supertone-engine-reader", daemon=True
        )
        self._reader.start()

    def _pump(self) -> None:
        """Drain stdout line by line into :attr:`_lines` until EOF.

        This is the ONLY place that blocks on ``readline()``. It ends when the
        subprocess closes stdout -- normally on shutdown, or because we killed
        it -- and always posts :data:`_EOF` so a waiting :meth:`recv` is
        released instead of sitting on the queue forever.
        """
        try:
            while True:
                line = self.proc.stdout.readline()
                if line == "":  # EOF -> subprocess closed stdout / died
                    break
                self._lines.put(line)
        except Exception:
            # A killed process can surface as ValueError/OSError on the pipe;
            # that is still just "no more lines", so fall through to the EOF
            # sentinel rather than dying inside a daemon thread.
            pass
        finally:
            self._lines.put(_EOF)

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def send(self, obj: dict) -> None:
        """Write one compact JSON object as a single NDJSON line and flush."""
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float) -> dict:
        """Read exactly one response line and parse it as JSON, within ``timeout``.

        The old version did NOT actually bound anything: it called
        ``proc.stdout.readline()`` on this thread, which blocks with no timeout
        of its own, and only consulted the deadline after a *blank* line came
        back. A subprocess that stayed alive and simply said nothing -- a model
        wedged mid-load, a hung ONNX session -- never produced that blank line,
        so ``recv`` waited forever and the whole pytest run hung with no
        diagnostic. The liveness check had the same hole: it ran BEFORE the
        blocking call, so a process that went silent after the check was never
        re-examined.

        Now the blocking read lives on the pump thread and this method waits on
        ``queue.Queue.get(timeout=...)``, which is honoured on Windows (no
        ``select`` on pipes, no ``signal.alarm``, no extra dependency). The
        wall-clock budget spans the whole call, so blank lines cannot extend it.

        On timeout the subprocess is killed before we raise -- see
        :meth:`_timeout` for why that is required and not merely tidy.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._timeout(timeout)
            try:
                item = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise self._timeout(timeout) from None
            if item is _EOF:
                rc = self.proc.poll()
                raise AssertionError(
                    "subprocess closed stdout (EOF) before responding"
                    + (f" (exit code={rc})" if rc is not None else "")
                )
            line = item.strip()
            if not line:
                continue
            return json.loads(line)

    def _timeout(self, timeout: float) -> AssertionError:
        """Kill the subprocess, then BUILD (not raise) the timeout error.

        Killing is what makes the timeout safe rather than cosmetic: it closes
        the stdout pipe, which turns the pump thread's pending ``readline()``
        into an EOF return and lets that thread finish. Without it a timed-out
        test would leave both an orphan engine holding the model and a reader
        thread blocked on a process nobody is talking to any more.
        """
        self._kill_proc()
        self._reader.join(timeout=5)
        return AssertionError(
            f"timed out after {timeout:.1f}s waiting for a response line "
            "(subprocess was alive but silent; it has been killed)"
        )

    def _kill_proc(self) -> None:
        """Terminate the subprocess, escalating to kill; never raises."""
        if self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
            return
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass

    def close(self) -> None:
        """Graceful stdin-EOF close, then guarantee no orphan and no live reader.

        Closing stdin is the engine's documented shutdown path; if it has not
        exited within 5s we terminate/kill it. Joining the pump thread proves
        the stdout pipe is really gone rather than assuming it.
        """
        try:
            if (
                self.proc.poll() is None
                and self.proc.stdin
                and not self.proc.stdin.closed
            ):
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self._kill_proc()
        self._reader.join(timeout=5)


@pytest.fixture()
def engine_client():
    """Spawn the real subprocess (skipping if venv312 is absent) and clean up.

    The process is always terminated in teardown — including when a test body
    raises — so no orphan engine is left running.
    """
    interpreter = _resolve_venv312_interpreter()
    if not interpreter:
        pytest.skip(
            "venv312 Python 3.12 interpreter not resolvable (set "
            f"{VENV312_PYTHON_ENV} or create venv312); skipping real-subprocess test."
        )

    # argv list, NEVER shell=True -> no shell injection; mirrors _spawn_locked.
    proc = subprocess.Popen(
        [interpreter, ENGINE_PATH],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,  # line-buffered
        cwd=REPO_ROOT,
    )
    client = _EngineClient(proc)
    try:
        yield client
    finally:
        # Graceful close, then guarantee no orphan process and no blocked
        # reader thread remains -- even when the test body raised.
        client.close()


# --------------------------------------------------------------------------- #
# Cheap harness guards (always run -- no model, no venv312, no supertonic)
# --------------------------------------------------------------------------- #


def test_engine_script_lives_beside_the_bridge_at_repo_root():
    """CI guard against re-breaking :data:`ENGINE_PATH`.

    The round-trip test below only runs when someone opts in with a venv312 on
    the box, so a wrong engine path can sit undetected for weeks -- which is
    exactly what happened: the path pointed inside ``tests/`` and the spawned
    interpreter died with ``[Errno 2] No such file or directory`` before it
    could emit a single protocol line.

    This costs one ``os.path.isfile`` per assertion: it imports no bridge, loads
    no ~260-400MB model, and spawns nothing, so it is safe in cloud CI and while
    a live stream is running.
    """
    assert os.path.isfile(ENGINE_PATH), (
        f"engine script not found at {ENGINE_PATH!r}; the round-trip test would "
        "spawn a path that does not exist and fail for the wrong reason"
    )

    # The bridge derives its own engine path from its own directory, so the two
    # files must be siblings. Compared as paths, not by importing the bridge
    # (importing it drags in numpy/sounddevice and is not cloud-safe).
    bridge_path = os.path.join(REPO_ROOT, "arti_bridge.py")
    assert os.path.isfile(bridge_path), (
        "arti_bridge.py is not at the repo root any more; "
        f"ENGINE_PATH ({ENGINE_PATH!r}) no longer mirrors _spawn_locked()"
    )
    assert os.path.dirname(ENGINE_PATH) == os.path.dirname(bridge_path)

    # ``supertone_engine`` does ``from text_preprocessor import ...`` at module
    # top level with no sys.path fiddling, so it resolves via sys.path[0] = the
    # script's own directory. If the preprocessor is not a sibling the engine
    # dies on import, before the ready banner.
    assert os.path.isfile(os.path.join(REPO_ROOT, "text_preprocessor.py"))

    # Guard the other repair, too: nobody should "fix" a broken path by copying
    # the engine into tests/, which would leave a stale duplicate to drift.
    stray = os.path.join(REPO_ROOT, "tests", "supertone_engine.py")
    assert not os.path.exists(stray), (
        f"unexpected duplicate engine at {stray!r}; the bridge only ever spawns "
        f"{ENGINE_PATH!r}"
    )


def test_recv_cannot_block_forever_on_a_live_but_silent_subprocess():
    """The timeout on :meth:`_EngineClient.recv` must be real, not decorative.

    Stands in for the failure the harness could not survive: an engine that is
    still RUNNING but never writes a line. It is driven with a bare
    ``python -c "time.sleep(...)"`` under the interpreter already running
    pytest -- no venv312, no supertonic, no ~260-400MB model -- so it is safe
    in cloud CI and safe to run while a stream is live. Cost is one interpreter
    start plus the 1s timeout being asserted.

    The child sleeps far longer than the timeout on purpose: if ``recv`` ever
    regresses to a blocking ``readline()`` this test stops being a 1s failure
    and becomes a 60s hang, which the elapsed-time assertion reports.

    Also pins the two cleanup guarantees, because a timeout that leaves the
    engine running is barely better than the hang: the process must be dead and
    the reader thread must be finished by the time the error surfaces.
    """
    proc = subprocess.Popen(
        # Alive, holds stdout open, says nothing. No project imports at all.
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    client = _EngineClient(proc)
    try:
        started = time.monotonic()
        with pytest.raises(AssertionError, match="timed out"):
            client.recv(timeout=1.0)
        elapsed = time.monotonic() - started

        assert elapsed < 15.0, (
            f"recv() took {elapsed:.1f}s for a 1s timeout -- it is blocking on "
            "readline() again instead of honouring the budget"
        )
        assert elapsed >= 0.9, (
            f"recv() gave up after {elapsed:.2f}s without waiting the 1s it was "
            "asked for"
        )
        assert proc.poll() is not None, (
            "timeout path left the subprocess alive -- an orphan engine would "
            "keep holding the model"
        )
        assert not client._reader.is_alive(), (
            "timeout path left the reader thread blocked on readline()"
        )
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# The gated round-trip test
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@requires_real_engine
def test_real_subprocess_round_trip(engine_client: _EngineClient):
    """Full handshake + ping + synthesize + shutdown against the real engine.

    _Requirements: 8.1, 8.2, 8.9_
    """
    import numpy as np  # imported lazily; only needed when the test runs.
    import soundfile as sf

    # 1) Readiness handshake: the FIRST stdout line must be the ready banner
    #    with type=="ready" and ok==true (Req 4.1).
    banner = engine_client.recv(timeout=READY_TIMEOUT_S)
    assert banner.get("type") == "ready", f"unexpected first line: {banner!r}"
    assert banner.get("ok") is True, f"engine reported not-ready: {banner!r}"

    # 2) ping -> pong echoing the request id (Req 8.9).
    ping_id = engine_client.next_id()
    engine_client.send({"v": PROTOCOL_VERSION, "id": ping_id, "type": "ping"})
    pong = engine_client.recv(timeout=SYNTH_TIMEOUT_S)
    assert pong.get("type") == "pong", f"expected pong, got: {pong!r}"
    assert pong.get("ok") is True
    assert pong.get("id") == ping_id, "pong did not echo the request id"

    # 3) synthesize -> ok, a readable WAV at wav_path, duration > 0
    #    (Reqs 8.1, 8.2).
    synth_id = engine_client.next_id()
    engine_client.send(
        {
            "v": PROTOCOL_VERSION,
            "id": synth_id,
            "type": "synthesize",
            "text": "Halo, ini tes.",
            "voice": "F1",
            "speed": 1.0,
            "lang": "id",
            "total_steps": 8,
            "preprocess_numbers": True,
        }
    )
    resp = engine_client.recv(timeout=SYNTH_TIMEOUT_S)
    assert resp.get("id") == synth_id, "synthesize response did not echo the id"
    assert resp.get("ok") is True, f"synthesize failed: {resp!r}"

    wav_path = resp.get("wav_path")
    assert isinstance(wav_path, str) and wav_path, "missing wav_path"
    assert os.path.isfile(wav_path), f"wav_path does not exist on disk: {wav_path}"

    # Readable WAV with real audio in it (Req 8.2). NOT a specific rate: the
    # engine never names one (it just calls _tts.save_audio), so the native rate
    # belongs to the supertonic model -- observed 44100 Hz in real runtime on
    # 2026-09-02 -- and normalising it to 48 kHz is the bridge's job
    # (_play_wav -> resample_audio(..., 48000)), covered by the bridge tests.
    data, samplerate = sf.read(wav_path)
    assert isinstance(samplerate, int) and samplerate > 0, (
        f"expected a positive sample rate, got {samplerate!r}"
    )
    assert len(data) > 0, "synthesized WAV contains no samples"
    assert np.any(np.abs(data) > 0.0), (
        "synthesized WAV is digital silence (all-zero samples)"
    )

    duration = resp.get("duration")
    assert isinstance(duration, (int, float)) and duration > 0, (
        f"expected positive duration, got: {duration!r}"
    )

    # 4) shutdown -> the engine exits its serve loop and the process terminates
    #    (Req 10.1). Close stdin as an EOF backup, then assert the process exits.
    engine_client.send({"v": PROTOCOL_VERSION, "type": "shutdown"})
    proc = engine_client.proc
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
    assert proc.returncode is not None, "subprocess did not exit after shutdown"
