"""Integration test: Supertone fallback to edge_tts on a bad interpreter.

End-to-end-ish exercise of the dual-engine fallback path from the Supertone 3
TTS upgrade design (sections "TTSEngine.speak() — Dual-Engine Low-Level Design"
and "Subprocess Lifecycle Manager — SupertoneProcess", plus the Error
Code / Fallback table, row 1: missing interpreter -> fallback edge_tts).

Validates Requirements:
    2.2  Supertone_Process fails to resolve the Python 3.12 interpreter
         -> TTS_Engine uses the edge_tts path for the current utterance.
    2.9  speak() completes without raising for any input / engine selection.

What makes this an *integration* test (vs. the Property 6 totality test in
``test_speak_totality_properties.py``):
    The Property 6 test stubs ``_acquire_supertone`` wholesale to inject
    failures. Here we keep the REAL ``_acquire_supertone`` and a REAL
    ``SupertoneProcess`` instance, so ``speak()`` -> ``_acquire_supertone()`` ->
    ``self.supertone.ensure_alive()`` -> ``_spawn_locked()`` all run for real and
    the failure originates from the genuine interpreter-resolution / spawn seam.
    Only that single seam is faked:

      * Test 1 monkeypatches ``_resolve_venv312_python`` to raise
        ``FileNotFoundError`` (interpreter missing).
      * Test 2 monkeypatches ``_resolve_venv312_python`` to return a bogus path
        and lets the REAL ``subprocess.Popen`` fail to launch it.

    In both cases the only collaborator stubbed on the engine is
    ``_speak_edge_tts`` (an async recorder), and ``_play_wav`` is never reached
    because acquisition fails. No real audio device, network, or subprocess is
    touched.

Test approach
    A bare ``TTSEngine`` is built with ``TTSEngine.__new__`` (skipping the real
    ``__init__`` device probe), ``device_id`` is set to ``None``, and a real
    ``SupertoneProcess()`` is attached. The engine + manager are constructed
    INSIDE the running event loop (``asyncio.run``) so the manager's
    ``asyncio.Lock`` binds to that loop, mirroring
    ``test_supertone_process_lifecycle.py``.

    The module global ``tts_is_playing`` is forced False before the run and
    asserted False afterward. ``CONFIG["tts_engine"]`` is set to "supertone" via
    ``monkeypatch.setitem`` and the interpreter seam via ``monkeypatch.setattr``;
    both restore automatically after the test.
"""

from __future__ import annotations

import asyncio
import os

import pytest

# Importing the bridge module runs module-level setup (a stdout/stderr tee
# logger + session log) but performs no blocking work and touches no audio
# devices at import time — those happen only inside TTSEngine.__init__, which
# this test deliberately bypasses via TTSEngine.__new__. Mirrors the import
# pattern in test_speak_totality_properties.py / test_supertone_process_*.py.
import hermes_vtuber_bridge as bridge
from hermes_vtuber_bridge import TTSEngine, SupertoneProcess


def _make_engine_with_real_manager() -> TTSEngine:
    """Build a TTSEngine WITHOUT its real __init__ device probe, attaching a
    REAL SupertoneProcess so ensure_alive()/_spawn_locked() run for real.

    MUST be called inside a running event loop so the manager's asyncio.Lock
    binds to that loop.
    """
    eng = TTSEngine.__new__(TTSEngine)
    eng.device_id = None
    eng.supertone = SupertoneProcess()
    return eng


async def _drive_speak_and_capture(text: str) -> dict:
    """Run a single ``speak(text)`` with the REAL Supertone acquisition path and
    a recorded edge_tts fallback.

    Returns a dict describing what happened: the captured edge_tts calls and the
    final manager state, so the caller can assert on them.
    """
    # Req 14.3 precondition: start from a known mic-gate state.
    bridge.tts_is_playing = False

    eng = _make_engine_with_real_manager()

    # Record every edge_tts invocation (text + that it was awaited). We replace
    # the bound method on the instance so the REAL _acquire_supertone still runs
    # and only the fallback sink is stubbed. _play_wav stays untouched because
    # acquisition fails before it would ever be reached.
    edge_calls: list[str] = []

    async def _record_edge(text_arg: str) -> None:
        edge_calls.append(text_arg)

    eng._speak_edge_tts = _record_edge

    # speak() MUST NOT raise for any input/engine/failure (Req 2.9). If the
    # fallback contract is broken this await would raise and fail the test.
    await eng.speak(text)

    return {
        "edge_calls": edge_calls,
        # After a failed spawn, no handle should remain and the manager stays
        # not-ready (confirms the REAL spawn path was attempted, not skipped).
        "proc": eng.supertone.proc,
        "ready": eng.supertone._ready,
    }


def test_fallback_to_edge_tts_when_interpreter_resolution_raises(monkeypatch):
    """Missing venv312 interpreter (FileNotFoundError from
    ``_resolve_venv312_python``) -> speak() falls back to edge_tts exactly once
    with the original text, never raises, and leaves the mic gate False.

    **Validates: Requirements 2.2, 2.9**
    """
    text = "Halo guys! <laugh> harganya Rp 10.000 <sigh>"

    # Engine selection: route to Supertone first so the fallback path is taken.
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "supertone")
    monkeypatch.setitem(bridge.CONFIG, "tts_fallback_to_edge", True)

    # Point the manager at a non-existent interpreter by making resolution fail
    # exactly as it would when venv312 is absent (Fallback table, row 1 / Req 2.2).
    def _raise_missing() -> str:
        raise FileNotFoundError("venv312 Python interpreter not found (test)")

    monkeypatch.setattr(bridge, "_resolve_venv312_python", _raise_missing)

    result = asyncio.run(_drive_speak_and_capture(text))

    # edge_tts was invoked exactly once with the ORIGINAL, unmodified text
    # (Req 2.2: same utterance synthesized through edge_tts).
    assert result["edge_calls"] == [text]
    # The real spawn path was attempted and aborted before Popen, so no handle
    # remains and the manager is not ready.
    assert result["proc"] is None
    assert result["ready"] is False
    # Mic gate left False on return, for the fallback path (Req 14.3 / totality).
    assert bridge.tts_is_playing is False


def test_fallback_to_edge_tts_when_popen_target_missing(monkeypatch):
    """Bogus interpreter path -> the REAL ``subprocess.Popen`` fails to launch it
    -> speak() falls back to edge_tts exactly once with the original text, never
    raises, and leaves the mic gate False.

    Exercises the genuine spawn seam (Popen actually attempts to start the
    process and fails because the executable does not exist) rather than
    short-circuiting at interpreter resolution.

    **Validates: Requirements 2.2, 2.9**
    """
    text = "Test fallback via real Popen failure."

    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "supertone")
    monkeypatch.setitem(bridge.CONFIG, "tts_fallback_to_edge", True)

    # Resolve to a path that definitely does not exist so the real Popen launch
    # raises (FileNotFoundError / OSError) — i.e. _spawn_locked's Popen target
    # does not exist (Req 2.2).
    bogus = os.path.join(
        os.path.dirname(os.path.abspath(bridge.__file__)),
        "venv312_does_not_exist",
        "python_missing.exe",
    )

    def _return_bogus() -> str:
        return bogus

    monkeypatch.setattr(bridge, "_resolve_venv312_python", _return_bogus)

    result = asyncio.run(_drive_speak_and_capture(text))

    assert result["edge_calls"] == [text]
    # Popen raised before returning a handle, so proc stays None and not ready.
    assert result["proc"] is None
    assert result["ready"] is False
    assert bridge.tts_is_playing is False
