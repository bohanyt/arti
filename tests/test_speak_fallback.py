"""Example-based unit tests for `TTSEngine.speak()` fallback + edge_tts tag passthrough.

Task 5.6 of the Supertone 3 TTS upgrade. These are EXAMPLE-based (not
property-based) tests that pin down the concrete dual-engine routing and
fallback behavior of `TTSEngine.speak()` (see the "TTSEngine.speak() —
Dual-Engine Low-Level Design" section of design.md and the Error Code /
Fallback table).

Covered behaviors:

  * Each fallback failure mode reaches edge_tts with the SAME text that was
    passed to speak(), exactly once (Req 2.1). The Supertone acquisition path
    (`_acquire_supertone`) is injected to raise every entry from the fallback
    table: FileNotFoundError (interpreter/spawn resolution),
    asyncio.TimeoutError (synthesis timeout), SupertoneError with
    MODEL_LOAD_FAILED / EOF / DESYNC, an ok:false response surfaced as a
    SupertoneError(BAD_REQUEST), and a generic Exception.

  * An unrecognized `CONFIG["tts_engine"]` value (e.g. "bogus", absent, empty,
    wrong case) coerces to the edge_tts path with the text, never touching the
    Supertone path, and prints a warning that identifies the rejected value
    (Req 1.4, 1.5).

  * The edge_tts path receives expression tags (`<laugh>`/`<breath>`/`<sigh>`)
    as literal text, verbatim (Req 12.5).

  * The Supertone success path does NOT fall back to edge_tts (Req 1.2 /
    2.1 — fallback is triggered by failure only).

Test approach mirrors test_speak_totality_properties.py: no real subprocess,
audio device, or network is touched. A bare TTSEngine is built with
``TTSEngine.__new__`` (skipping the device-probe __init__), ``device_id`` is set
to None, and ``supertone`` is an inert stub. Collaborator methods
(`_acquire_supertone`, `_play_wav`, `_speak_edge_tts`) are replaced per scenario.
CONFIG is mutated with ``monkeypatch.setitem`` / ``delitem`` so it is restored
automatically, and the module mic-gate global ``tts_is_playing`` is forced False
before each run.
"""

from __future__ import annotations

import asyncio

import pytest

# Importing the bridge runs module-level setup (a stdout/stderr tee logger +
# session log) but performs no blocking work and touches no audio device at
# import time — the device probe lives in TTSEngine.__init__, which these tests
# bypass via TTSEngine.__new__.
import hermes_vtuber_bridge as bridge
from hermes_vtuber_bridge import TTSEngine, SupertoneError


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _InertSupertone:
    """Stand-in for the SupertoneProcess handle (never reached in these tests)."""

    async def ensure_alive(self) -> None:  # pragma: no cover - never invoked
        return None

    async def request(self, req, timeout):  # pragma: no cover - never invoked
        return {"ok": True, "id": req.get("id"), "wav_path": "/tmp/x.wav"}

    async def shutdown(self) -> None:  # pragma: no cover - never invoked
        return None


class _EdgeRecorder:
    """Records every text handed to `_speak_edge_tts` and how many times."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, text: str) -> None:
        self.calls.append(text)

    @property
    def count(self) -> int:
        return len(self.calls)


def _make_engine() -> TTSEngine:
    """Build a TTSEngine WITHOUT running its real __init__ device probe."""
    eng = TTSEngine.__new__(TTSEngine)
    eng.device_id = None
    eng.supertone = _InertSupertone()
    return eng


# The fallback table: label -> factory producing the exception that
# `_acquire_supertone` raises. Each MUST cause speak() to fall back to edge_tts.
_FALLBACK_FAILURES = {
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


@pytest.fixture(autouse=True)
def _reset_mic_gate():
    """Force the module mic-gate False before and after each test."""
    bridge.tts_is_playing = False
    yield
    bridge.tts_is_playing = False


# ---------------------------------------------------------------------------
# 1) Every fallback failure mode reaches edge_tts with IDENTICAL text, once.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("failure_label", list(_FALLBACK_FAILURES.keys()))
def test_supertone_failure_falls_back_to_edge_with_identical_text(
    failure_label, monkeypatch
):
    """Each Supertone fallback-table failure routes the SAME text to edge_tts
    exactly once (Req 2.1).
    """
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "supertone")
    monkeypatch.setitem(bridge.CONFIG, "tts_fallback_to_edge", True)

    text = "Halo guys! <laugh> harganya Rp 10.000 <sigh>"
    exc_factory = _FALLBACK_FAILURES[failure_label]

    eng = _make_engine()
    edge = _EdgeRecorder()

    async def _acquire_fails(text_arg):
        raise exc_factory()

    async def _play_wav(wav_path, text_arg, word_timings, owns_temp):
        # Should never run on a failure path, but keep it inert just in case.
        return None  # pragma: no cover

    eng._acquire_supertone = _acquire_fails
    eng._play_wav = _play_wav
    eng._speak_edge_tts = edge

    asyncio.run(eng.speak(text))

    assert edge.count == 1, (
        f"edge_tts expected exactly once for failure {failure_label!r}, "
        f"got {edge.count}"
    )
    assert edge.calls[0] == text, (
        f"edge_tts received different text for failure {failure_label!r}: "
        f"{edge.calls[0]!r} != {text!r}"
    )
    assert bridge.tts_is_playing is False


# ---------------------------------------------------------------------------
# 2) Unrecognized / absent / empty tts_engine coerces to edge_tts + warns.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "engine_value",
    ["bogus", "", "EDGE_TTS", "Supertone", "  supertone  "],
)
def test_unrecognized_engine_coerces_to_edge_tts_with_warning(
    engine_value, monkeypatch, capsys
):
    """A tts_engine value other than the exact strings "supertone"/"edge_tts"
    routes to edge_tts with the text, never touches the Supertone path, and logs
    a warning identifying the rejected value (Req 1.4, 1.5).
    """
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", engine_value)
    monkeypatch.setitem(bridge.CONFIG, "tts_fallback_to_edge", True)

    text = "selamat datang di stream"
    eng = _make_engine()
    edge = _EdgeRecorder()

    acquire_calls = {"n": 0}

    async def _acquire_should_not_run(text_arg):
        acquire_calls["n"] += 1  # pragma: no cover
        raise AssertionError("Supertone path must not run for unrecognized engine")

    eng._acquire_supertone = _acquire_should_not_run
    eng._speak_edge_tts = edge

    asyncio.run(eng.speak(text))

    assert acquire_calls["n"] == 0, "Supertone acquisition must not be invoked"
    assert edge.count == 1
    assert edge.calls[0] == text

    # The warning must identify the rejected value (repr appears in the message).
    out = capsys.readouterr().out
    assert repr(engine_value) in out, (
        f"warning should identify rejected value {engine_value!r}; got:\n{out}"
    )


def test_absent_engine_key_coerces_to_edge_tts(monkeypatch):
    """An absent `CONFIG["tts_engine"]` key defaults to the edge_tts path with
    the text and never touches the Supertone path (Req 1.4).
    """
    monkeypatch.delitem(bridge.CONFIG, "tts_engine", raising=False)

    text = "tidak ada engine yang dipilih"
    eng = _make_engine()
    edge = _EdgeRecorder()

    async def _acquire_should_not_run(text_arg):
        raise AssertionError("Supertone path must not run when key absent")  # pragma: no cover

    eng._acquire_supertone = _acquire_should_not_run
    eng._speak_edge_tts = edge

    asyncio.run(eng.speak(text))

    assert edge.count == 1
    assert edge.calls[0] == text


# ---------------------------------------------------------------------------
# 3) edge_tts receives expression tags as literal text.
# ---------------------------------------------------------------------------
def test_edge_tts_path_receives_tags_verbatim(monkeypatch):
    """When the edge_tts path is selected, expression tags reach
    `_speak_edge_tts` as literal text, verbatim (Req 12.5).
    """
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "edge_tts")

    text = "Hai <laugh> aku senang <breath> hari ini <sigh> selesai"
    eng = _make_engine()
    edge = _EdgeRecorder()

    async def _acquire_should_not_run(text_arg):
        raise AssertionError("Supertone path must not run on edge_tts engine")  # pragma: no cover

    eng._acquire_supertone = _acquire_should_not_run
    eng._speak_edge_tts = edge

    asyncio.run(eng.speak(text))

    assert edge.count == 1
    handed = edge.calls[0]
    assert handed == text, f"edge_tts text altered: {handed!r} != {text!r}"
    for tag in ("<laugh>", "<breath>", "<sigh>"):
        assert tag in handed, f"tag {tag!r} missing from edge_tts text: {handed!r}"


# ---------------------------------------------------------------------------
# 4) Supertone success path does NOT fall back to edge_tts.
# ---------------------------------------------------------------------------
def test_supertone_success_does_not_fall_back(monkeypatch):
    """On a successful Supertone acquisition + playback, `_speak_edge_tts` is
    never called (fallback is failure-driven only — Req 1.2 / 2.1).
    """
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "supertone")

    text = "sintesis supertone berhasil <laugh>"
    eng = _make_engine()
    edge = _EdgeRecorder()

    play_calls = {"args": None}

    async def _acquire_ok(text_arg):
        return ("/tmp/x.wav", [])

    async def _play_wav(wav_path, text_arg, word_timings, owns_temp):
        play_calls["args"] = (wav_path, text_arg, word_timings, owns_temp)
        return None

    eng._acquire_supertone = _acquire_ok
    eng._play_wav = _play_wav
    eng._speak_edge_tts = edge

    asyncio.run(eng.speak(text))

    assert edge.count == 0, "edge_tts must not be called on the Supertone success path"
    assert play_calls["args"] is not None, "_play_wav should run on success"
    wav_path, text_arg, word_timings, owns_temp = play_calls["args"]
    assert wav_path == "/tmp/x.wav"
    assert text_arg == text
    assert word_timings == []
    assert owns_temp is True
    assert bridge.tts_is_playing is False
