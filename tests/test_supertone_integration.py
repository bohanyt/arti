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
  ``hermes_vtuber_bridge._resolve_venv312_python()``).

If either condition is missing the test ``pytest.skip(...)``s cleanly, so it
never fails in CI or on a dev box where venv312/supertonic is absent.

It drives the subprocess directly with ``subprocess.Popen`` (argv list, no
shell), mirroring ``SupertoneProcess._spawn_locked`` (``text=True``,
``encoding="utf-8"``, ``bufsize=1``), and always terminates the process in a
fixture cleanup even if assertions fail.

_Requirements: 8.1, 8.2, 8.9_
"""

from __future__ import annotations

import json
import os
import subprocess
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

#: Directory containing this test, supertone_engine.py, and text_preprocessor.py.
HERE = os.path.dirname(os.path.abspath(__file__))

#: Per-operation read ceilings (seconds). Generous to allow first-run model
#: download (readiness) and CPU synthesis.
READY_TIMEOUT_S = 180.0
SYNTH_TIMEOUT_S = 120.0
SHUTDOWN_TIMEOUT_S = 15.0


def _resolve_venv312_interpreter() -> "str | None":
    """Resolve the venv312 Python 3.12 interpreter path, or ``None``.

    Prefers the explicit ``SUPERTONE_VENV312_PYTHON`` override; otherwise defers
    to ``hermes_vtuber_bridge._resolve_venv312_python()``. Any failure (missing
    interpreter, import error, etc.) returns ``None`` so the caller can skip.
    """
    override = os.environ.get(VENV312_PYTHON_ENV)
    if override:
        return override if os.path.isfile(override) else None
    try:
        from hermes_vtuber_bridge import _resolve_venv312_python

        return _resolve_venv312_python()
    except Exception:
        # FileNotFoundError (no venv312), ImportError (bridge deps absent),
        # or anything else => treat as "not available" and let the test skip.
        return None


# Opt-in gate evaluated at collection time. Cheap (env var only) so we never
# import the heavy bridge module just to decide whether to skip in CI.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get(INTEGRATION_OPT_IN_ENV) != "1",
        reason=(
            "Supertone real-subprocess integration test is opt-in: loads the "
            "~260-400MB model and needs venv312. Set "
            f"{INTEGRATION_OPT_IN_ENV}=1 to run."
        ),
    ),
]


# --------------------------------------------------------------------------- #
# NDJSON client over the spawned subprocess
# --------------------------------------------------------------------------- #


class _EngineClient:
    """Thin NDJSON helper around the spawned ``supertone_engine.py`` process."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._next_id = 0

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def send(self, obj: dict) -> None:
        """Write one compact JSON object as a single NDJSON line and flush."""
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float) -> dict:
        """Read exactly one response line and parse it as JSON.

        ``subprocess`` text-mode ``readline`` is blocking, so we enforce the
        timeout with a coarse wall-clock budget plus liveness checks: if the
        process dies (EOF / non-None ``returncode``) we fail fast instead of
        hanging.
        """
        deadline = time.monotonic() + timeout
        # readline() itself blocks; we cannot easily interrupt it portably, so
        # we rely on the engine being responsive and guard against a dead proc.
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"subprocess exited (code={self.proc.returncode}) before "
                    "returning a response line"
                )
            line = self.proc.stdout.readline()
            if line == "":
                raise AssertionError("subprocess closed stdout (EOF) before responding")
            line = line.strip()
            if not line:
                if time.monotonic() > deadline:
                    raise AssertionError("timed out waiting for a response line")
                continue
            return json.loads(line)


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

    engine_path = os.path.join(HERE, "supertone_engine.py")
    # argv list, NEVER shell=True -> no shell injection; mirrors _spawn_locked.
    proc = subprocess.Popen(
        [interpreter, engine_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,  # line-buffered
        cwd=HERE,
    )
    client = _EngineClient(proc)
    try:
        yield client
    finally:
        # Best-effort graceful close, then guarantee no orphan remains.
        try:
            if proc.poll() is None and proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# The gated round-trip test
# --------------------------------------------------------------------------- #


def test_real_subprocess_round_trip(engine_client: _EngineClient):
    """Full handshake + ping + synthesize + shutdown against the real engine.

    _Requirements: 8.1, 8.2, 8.9_
    """
    import soundfile as sf  # imported lazily; only needed when the test runs.

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

    # 3) synthesize -> ok, a readable 48kHz WAV at wav_path, duration > 0
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

    # Readable WAV at 48kHz (Req 8.2).
    data, samplerate = sf.read(wav_path)
    assert samplerate == 48000, f"expected 48000 Hz, got {samplerate}"
    assert len(data) > 0, "synthesized WAV contains no samples"

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
