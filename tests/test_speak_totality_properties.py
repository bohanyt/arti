"""Property-based test for `TTSEngine.speak()` totality.

Uses Hypothesis to validate the totality contract of the dual-engine speak
entrypoint (`TTSEngine.speak()`) defined in the Supertone 3 TTS upgrade design
(see the "TTSEngine.speak() — Dual-Engine Low-Level Design" section, its formal
spec, and the Error Code / Fallback table).

Property 6: speak() totality
    Validates: Requirements 2.9, 14.3
    speak() never raises and always resets the mic gate, for any text, either
    engine selection, and each injected failure from the fallback table:

        ∀ text, ∀ engine ∈ {supertone, edge_tts, unrecognized},
        ∀ failure ∈ fallback-table.
            speak(text) terminates without raising
            ∧ tts_is_playing == False afterward

    Fallback table failures exercised on the Supertone acquisition path
    (`_acquire_supertone`), each of which `speak()` must catch and fall back
    from at most once (Req 2.2-2.6):

        FileNotFoundError            -> interpreter missing / spawn resolution
        OSError                      -> spawn error
        asyncio.TimeoutError         -> ready-banner / synthesis timeout
        SupertoneError(MODEL_LOAD_FAILED)
        SupertoneError(EOF)          -> subprocess closed stdout
        SupertoneError(DESYNC)       -> response id mismatch
        SupertoneError(BAD_REQUEST)  -> ok:false response
        generic Exception            -> any other Supertone fault

    On top of those, the edge_tts path itself is also injected to fail
    (Req 2.8/2.9): even when both engines fail, speak() must return without
    raising and leave the mic gate False.

    Engine selection is driven directly through ``CONFIG["tts_engine"]`` rather
    than the ``monkeypatch`` fixture, because Hypothesis disallows
    function-scoped fixtures under ``@given`` (they are not reset per generated
    input). CONFIG is saved and restored per run via try/finally.

Test approach
    No real subprocess, audio device, or network is touched. A bare TTSEngine
    is built with ``TTSEngine.__new__`` (skipping the real ``__init__`` device
    probe), ``device_id`` is set to ``None``, and ``supertone`` is a tiny inert
    stub. The collaborator methods are monkeypatched per scenario:

      * ``_acquire_supertone`` -> raises an injected failure, OR returns a
        fake ``(wav_path, [])`` success.
      * ``_play_wav``          -> async no-op (avoids real audio).
      * ``_speak_edge_tts``    -> async no-op OR raises (edge_tts-fails case).

    The module global ``tts_is_playing`` is forced False before each run and
    asserted False afterward. CONFIG is mutated via ``monkeypatch.setitem`` and
    restored automatically.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings, strategies as st

# Importing the bridge module executes module-level setup (a stdout/stderr tee
# logger + session log) but performs no blocking work and does not touch audio
# devices at import time — those happen only inside TTSEngine.__init__, which
# this test deliberately bypasses via TTSEngine.__new__. Mirrors the import
# pattern in test_supertone_process_properties.py / _lifecycle.py.
import hermes_vtuber_bridge as bridge
from hermes_vtuber_bridge import TTSEngine, SupertoneError


# ---------------------------------------------------------------------------
# Inert stub for TTSEngine.supertone
# ---------------------------------------------------------------------------
class _InertSupertone:
    """Stand-in for the SupertoneProcess handle.

    speak() never calls into this directly in these scenarios (we monkeypatch
    ``_acquire_supertone`` wholesale), but assigning a real-ish attribute keeps
    the engine object honest and avoids any accidental AttributeError.
    """

    async def ensure_alive(self) -> None:  # pragma: no cover - never invoked
        return None

    async def request(self, req, timeout):  # pragma: no cover - never invoked
        return {"ok": True, "id": req.get("id"), "wav_path": "/tmp/x.wav"}

    async def shutdown(self) -> None:  # pragma: no cover - never invoked
        return None


def _make_engine() -> TTSEngine:
    """Build a TTSEngine WITHOUT running its real __init__ device probe."""
    eng = TTSEngine.__new__(TTSEngine)
    eng.device_id = None
    eng.supertone = _InertSupertone()
    return eng


# ---------------------------------------------------------------------------
# Injected Supertone failures (the fallback table) + the success path
# ---------------------------------------------------------------------------
# Each entry is a label -> factory producing the exception instance to raise
# from _acquire_supertone. A separate "ok" sentinel marks the success path.
_SUPERTONE_FAILURES: dict[str, "callable"] = {
    "interpreter_missing": lambda: FileNotFoundError("venv312 interpreter not found"),
    "spawn_oserror": lambda: OSError("cannot spawn subprocess"),
    "timeout": lambda: asyncio.TimeoutError(),
    "model_load_failed": lambda: SupertoneError(
        {"code": "MODEL_LOAD_FAILED", "message": "boom"}
    ),
    "eof": lambda: SupertoneError({"code": "EOF", "message": "subprocess closed stdout"}),
    "desync": lambda: SupertoneError({"code": "DESYNC", "message": "id mismatch"}),
    "ok_false": lambda: SupertoneError({"code": "BAD_REQUEST", "message": "empty text"}),
    "generic": lambda: Exception("unexpected fault"),
}

# The full set of supertone-path scenarios: every fallback-table failure plus
# the success-ish path where acquisition returns a fake WAV + empty timings.
_SUPERTONE_SCENARIOS = list(_SUPERTONE_FAILURES.keys()) + ["success"]

# Engine selection values: the two recognized engines plus unrecognized values
# that Req 1.4 coerces to edge_tts (absent is modeled by removing the key).
_ENGINE_VALUES = ["supertone", "edge_tts", "", "EDGE_TTS", "Supertone", "bogus", None]

# Arbitrary utterance text, including non-ASCII + expression tags, constrained
# to UTF-8-encodable characters (excludes lone surrogates).
_text_strategy = st.text(alphabet=st.characters(codec="utf-8"), max_size=80)


async def _run_speak(
    *, engine_value, supertone_scenario: str, edge_fails: bool, text: str
) -> None:
    """Drive a single speak() call under the given scenario and assert totality.

    Forces ``tts_is_playing`` False, wires the collaborators per scenario, runs
    ``speak(text)`` (which MUST NOT raise), then asserts the mic gate is False.
    """
    # Reset the module mic-gate global before each run (Req 14.3 precondition).
    bridge.tts_is_playing = False

    # Apply engine selection directly to CONFIG, saving the prior value so it can
    # be restored after the run. ``None`` models an absent key (Req 1.4): we
    # delete it so speak()'s CONFIG.get(..., "edge_tts") default applies.
    _sentinel = object()
    _prev = bridge.CONFIG.get("tts_engine", _sentinel)
    if engine_value is None:
        bridge.CONFIG.pop("tts_engine", None)
    else:
        bridge.CONFIG["tts_engine"] = engine_value

    eng = _make_engine()

    # --- _play_wav: async no-op so the supertone success path performs no
    #     real audio I/O but still completes speak() without raising.
    async def _fake_play_wav(wav_path, text_arg, word_timings, owns_temp):
        return None

    eng._play_wav = _fake_play_wav

    # --- _acquire_supertone: inject a fallback-table failure, or succeed.
    if supertone_scenario == "success":
        async def _fake_acquire(text_arg):
            return ("/tmp/supertone_tmp/temp_test.wav", [])
    else:
        exc_factory = _SUPERTONE_FAILURES[supertone_scenario]

        async def _fake_acquire(text_arg):
            raise exc_factory()

    eng._acquire_supertone = _fake_acquire

    # --- _speak_edge_tts: async no-op, or raise to model edge_tts ALSO failing
    #     (Req 2.8) — speak() must still swallow it and return.
    if edge_fails:
        async def _fake_edge(text_arg):
            raise RuntimeError("edge_tts network failure")
    else:
        async def _fake_edge(text_arg):
            return None

    eng._speak_edge_tts = _fake_edge

    try:
        # speak() MUST complete without raising for ANY input/engine/failure
        # (Req 2.9).
        await eng.speak(text)

        # The mic gate MUST be left False on return, for every path (Req 14.3).
        assert bridge.tts_is_playing is False, (
            f"tts_is_playing left True after speak() "
            f"(engine={engine_value!r}, supertone={supertone_scenario!r}, "
            f"edge_fails={edge_fails!r})"
        )
    finally:
        # Restore the prior CONFIG["tts_engine"] so runs stay isolated.
        if _prev is _sentinel:
            bridge.CONFIG.pop("tts_engine", None)
        else:
            bridge.CONFIG["tts_engine"] = _prev


@settings(max_examples=300, deadline=None)
@given(
    text=_text_strategy,
    engine_value=st.sampled_from(_ENGINE_VALUES),
    supertone_scenario=st.sampled_from(_SUPERTONE_SCENARIOS),
    edge_fails=st.booleans(),
)
def test_speak_totality(text, engine_value, supertone_scenario, edge_fails):
    """speak() returns without raising and resets the mic gate, for any text,
    engine selection, and injected fallback-table failure (incl. edge_tts also
    failing).

    **Validates: Requirements 2.9, 14.3**
    """
    asyncio.run(
        _run_speak(
            engine_value=engine_value,
            supertone_scenario=supertone_scenario,
            edge_fails=edge_fails,
            text=text,
        )
    )
