"""Unit tests for supertone_engine.py model load, voice cache, and ready banner.

These tests run in the Python 3.11 environment where ``supertonic`` is NOT
installed. They rely on supertone_engine importing ``supertonic`` lazily
(inside ``load_model()``), so the module imports cleanly here. A fake
``supertonic`` module is injected into ``sys.modules`` to exercise
``load_model()``, ``get_voice_style()``, and ``main()``'s readiness banner
without the real ~260MB model.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

import supertone_engine as se


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeTTS:
    """Stand-in for ``supertonic.TTS`` that records voice-style requests."""

    instances = 0

    def __init__(self, auto_download=False):
        type(self).instances += 1
        self.auto_download = auto_download
        self.voice_style_calls = []

    def get_voice_style(self, voice_name):
        self.voice_style_calls.append(voice_name)
        return f"style::{voice_name}"


class FailingTTS:
    """Stand-in whose construction raises, to exercise MODEL_LOAD_FAILED."""

    def __init__(self, auto_download=False):
        raise RuntimeError("model download exploded")


@pytest.fixture(autouse=True)
def reset_engine_state(monkeypatch):
    """Reset module globals and remove any fake supertonic between tests."""
    se._tts = None
    se._voice_cache = {}
    FakeTTS.instances = 0
    monkeypatch.delitem(sys.modules, "supertonic", raising=False)
    yield
    se._tts = None
    se._voice_cache = {}
    monkeypatch.delitem(sys.modules, "supertonic", raising=False)


def _install_fake_supertonic(monkeypatch, tts_cls):
    """Inject a fake ``supertonic`` module exposing ``TTS = tts_cls``."""
    import types

    fake = types.ModuleType("supertonic")
    fake.TTS = tts_cls
    monkeypatch.setitem(sys.modules, "supertonic", fake)
    return fake


# --------------------------------------------------------------------------- #
# load_model()
# --------------------------------------------------------------------------- #


def test_load_model_loads_tts_once_with_auto_download(monkeypatch):
    _install_fake_supertonic(monkeypatch, FakeTTS)

    se.load_model()

    assert isinstance(se._tts, FakeTTS)
    assert se._tts.auto_download is True
    assert FakeTTS.instances == 1


def test_load_model_warms_default_voice_before_returning(monkeypatch):
    _install_fake_supertonic(monkeypatch, FakeTTS)

    se.load_model()

    # The default voice must be cached as a side effect of load_model().
    assert se.DEFAULT_VOICE in se._voice_cache
    assert se._tts.voice_style_calls == [se.DEFAULT_VOICE]


def test_load_model_propagates_failure(monkeypatch):
    _install_fake_supertonic(monkeypatch, FailingTTS)

    with pytest.raises(RuntimeError, match="model download exploded"):
        se.load_model()
    assert se._tts is None


# --------------------------------------------------------------------------- #
# get_voice_style()
# --------------------------------------------------------------------------- #


def test_get_voice_style_caches_on_first_request(monkeypatch):
    _install_fake_supertonic(monkeypatch, FakeTTS)
    se.load_model()
    se._tts.voice_style_calls.clear()

    style = se.get_voice_style("M2")

    assert style == "style::M2"
    assert se._voice_cache["M2"] == "style::M2"
    assert se._tts.voice_style_calls == ["M2"]


def test_get_voice_style_reuses_cache_without_recreating(monkeypatch):
    _install_fake_supertonic(monkeypatch, FakeTTS)
    se.load_model()
    se._tts.voice_style_calls.clear()

    first = se.get_voice_style("M2")
    second = se.get_voice_style("M2")

    assert first is second
    # Created exactly once: only one underlying call despite two requests.
    assert se._tts.voice_style_calls == ["M2"]


# --------------------------------------------------------------------------- #
# main() readiness banner
# --------------------------------------------------------------------------- #


def _capture_stdout(monkeypatch, tmp_path=None):
    buf = io.StringIO()
    monkeypatch.setattr(se.sys, "stdout", buf)
    # main() hands control to serve_loop(), which reads stdin until EOF. Provide
    # an already-empty stream so the loop exits immediately after the banner.
    monkeypatch.setattr(se.sys, "stdin", io.StringIO(""))
    # Redirect TEMP_DIR so serve_loop()'s cleanup_all_temp() never touches the
    # real system temp dir during these banner-focused tests.
    if tmp_path is not None:
        monkeypatch.setattr(se, "TEMP_DIR", tmp_path)
    return buf


def test_main_emits_success_ready_banner_and_returns_zero(monkeypatch, tmp_path):
    _install_fake_supertonic(monkeypatch, FakeTTS)
    buf = _capture_stdout(monkeypatch, tmp_path)

    rc = se.main()

    assert rc == 0
    line = buf.getvalue().strip()
    # Exactly one protocol line, newline-terminated, no embedded newline.
    assert buf.getvalue().endswith("\n")
    assert "\n" not in line
    banner = json.loads(line)
    assert banner == {
        "v": se.PROTOCOL_VERSION,
        "type": "ready",
        "ok": True,
        "voice_cached": [se.DEFAULT_VOICE],
        "engine": "supertonic-3",
    }


def test_main_caches_default_voice_before_emitting_banner(monkeypatch, tmp_path):
    """Requirement 3.5: default voice cached BEFORE the ready banner."""
    _install_fake_supertonic(monkeypatch, FakeTTS)
    buf = _capture_stdout(monkeypatch, tmp_path)

    se.main()

    banner = json.loads(buf.getvalue().strip())
    # The banner's voice_cached reflects that the default voice was cached
    # prior to emission.
    assert banner["voice_cached"] == [se.DEFAULT_VOICE]


def test_main_emits_failure_banner_and_returns_nonzero(monkeypatch):
    _install_fake_supertonic(monkeypatch, FailingTTS)
    buf = _capture_stdout(monkeypatch)

    rc = se.main()

    assert rc == 1
    banner = json.loads(buf.getvalue().strip())
    assert banner["v"] == se.PROTOCOL_VERSION
    assert banner["type"] == "ready"
    assert banner["ok"] is False
    assert banner["error"]["code"] == "MODEL_LOAD_FAILED"
    assert "model download exploded" in banner["error"]["message"]


# --------------------------------------------------------------------------- #
# Task 3.6: request validation, clamping, and error codes
#
# These tests exercise serve_loop() + handle_synthesize() end to end by feeding
# NDJSON request lines through a fake stdin and capturing the NDJSON responses
# written to stdout. A fake ``_tts`` is injected so synthesis runs without the
# real ~260MB model, and TEMP_DIR is redirected to a pytest tmp_path so no real
# disk writes occur (the fake save_audio is a no-op anyway).
#
# Covers requirements 8.3 (blank/missing/oversized text -> BAD_REQUEST),
# 8.4/8.5 (total_steps/speed clamping), 8.8 (UNKNOWN_TYPE), 8.9 (ping/pong),
# and 8.10 (per-request error isolation: a bad request never stops the loop),
# plus version framing (6.5) and malformed-line handling (5.5).
# --------------------------------------------------------------------------- #


class FakeSynthTTS:
    """Fake ``_tts`` exposing the synthesize/save_audio/get_voice_style surface.

    Records the keyword arguments actually passed to ``synthesize`` so tests can
    assert that clamping happened before the call, and returns a fixed
    ``(wav_stub, [duration])`` pair shaped like the real Supertone API.
    """

    def __init__(self, duration: float = 1.5):
        self.duration = duration
        self.synth_calls: list[dict] = []  # kwargs actually passed to synthesize
        self.saved: list[tuple] = []  # (wav, path) passed to save_audio

    def get_voice_style(self, voice_name):
        return f"style::{voice_name}"

    def synthesize(self, text, voice_style=None, lang=None, total_steps=None, speed=None):
        self.synth_calls.append(
            {
                "text": text,
                "voice_style": voice_style,
                "lang": lang,
                "total_steps": total_steps,
                "speed": speed,
            }
        )
        return ("wav_stub", [self.duration])

    def save_audio(self, wav, path):
        self.saved.append((wav, path))


@pytest.fixture
def fake_tts(monkeypatch, tmp_path):
    """Install a fake ``_tts`` and redirect TEMP_DIR so no real disk writes occur."""
    tts = FakeSynthTTS()
    monkeypatch.setattr(se, "_tts", tts)
    monkeypatch.setattr(se, "TEMP_DIR", tmp_path)
    return tts


def _run_serve_loop(monkeypatch, lines):
    """Feed NDJSON ``lines`` to serve_loop via fake stdin; return (rc, responses).

    Each entry in ``lines`` is written as its own physical line. The captured
    stdout is split back into a list of parsed JSON response objects (blank
    lines ignored).
    """
    payload = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)
    monkeypatch.setattr(se.sys, "stdin", io.StringIO(payload))
    out = io.StringIO()
    monkeypatch.setattr(se.sys, "stdout", out)
    rc = se.serve_loop()
    responses = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    return rc, responses


def _synth_req(**overrides):
    """Build a JSON-serialized synthesize request line with sensible defaults."""
    req = {"v": se.PROTOCOL_VERSION, "id": 1, "type": "synthesize", "text": "halo dunia"}
    req.update(overrides)
    return json.dumps(req)


# --- text validation (req 8.3) --------------------------------------------- #


@pytest.mark.parametrize("bad_text", ["", "   ", "\t\n  "])
def test_synthesize_blank_text_is_bad_request(monkeypatch, fake_tts, bad_text):
    rc, responses = _run_serve_loop(monkeypatch, [_synth_req(text=bad_text)])

    assert rc == 0
    assert len(responses) == 1
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "BAD_REQUEST"
    assert responses[0]["id"] == 1
    # No synthesis attempted on a rejected request.
    assert fake_tts.synth_calls == []


def test_synthesize_missing_text_is_bad_request(monkeypatch, fake_tts):
    req = json.dumps({"v": se.PROTOCOL_VERSION, "id": 3, "type": "synthesize"})

    rc, responses = _run_serve_loop(monkeypatch, [req])

    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "BAD_REQUEST"
    assert responses[0]["id"] == 3
    assert fake_tts.synth_calls == []


def test_synthesize_oversized_text_is_bad_request(monkeypatch, fake_tts):
    # 5001 chars is one past the 5000-char limit (req 8.3).
    rc, responses = _run_serve_loop(monkeypatch, [_synth_req(text="a" * 5001)])

    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "BAD_REQUEST"
    assert fake_tts.synth_calls == []


def test_synthesize_text_at_5000_chars_succeeds(monkeypatch, fake_tts):
    # Exactly 5000 chars is the inclusive upper bound and must synthesize (req 8.1).
    rc, responses = _run_serve_loop(monkeypatch, [_synth_req(text="a" * 5000)])

    assert responses[0]["ok"] is True
    assert len(fake_tts.synth_calls) == 1


# --- clamping (reqs 8.4, 8.5) ---------------------------------------------- #


@pytest.mark.parametrize(
    "requested, expected",
    [(2, 5), (4, 5), (5, 5), (8, 8), (12, 12), (13, 12), (99, 12)],
)
def test_total_steps_clamped_to_5_12(monkeypatch, fake_tts, requested, expected):
    _run_serve_loop(monkeypatch, [_synth_req(total_steps=requested)])

    # Assert the value actually passed to synthesize, not just the response.
    assert fake_tts.synth_calls[0]["total_steps"] == expected


@pytest.mark.parametrize(
    "requested, expected",
    [(0.1, 0.7), (0.7, 0.7), (1.0, 1.0), (2.0, 2.0), (2.5, 2.0), (5.0, 2.0)],
)
def test_speed_clamped_to_0_7_2_0(monkeypatch, fake_tts, requested, expected):
    _run_serve_loop(monkeypatch, [_synth_req(speed=requested)])

    assert fake_tts.synth_calls[0]["speed"] == expected


# --- control messages & framing (reqs 8.8, 8.9, 6.5, 5.5) ------------------ #


def test_unknown_type_is_unknown_type_error(monkeypatch, fake_tts):
    req = json.dumps({"v": se.PROTOCOL_VERSION, "id": 5, "type": "frobnicate"})

    rc, responses = _run_serve_loop(monkeypatch, [req])

    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "UNKNOWN_TYPE"
    assert responses[0]["id"] == 5


def test_ping_returns_pong_echoing_id(monkeypatch, fake_tts):
    req = json.dumps({"v": se.PROTOCOL_VERSION, "id": 7, "type": "ping"})

    rc, responses = _run_serve_loop(monkeypatch, [req])

    assert responses[0] == {
        "v": se.PROTOCOL_VERSION,
        "id": 7,
        "ok": True,
        "type": "pong",
    }


def test_version_mismatch_is_unsupported_version(monkeypatch, fake_tts):
    req = json.dumps(
        {"v": se.PROTOCOL_VERSION + 1, "id": 8, "type": "synthesize", "text": "hi"}
    )

    rc, responses = _run_serve_loop(monkeypatch, [req])

    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "UNSUPPORTED_VERSION"
    assert responses[0]["id"] == 8
    # A version mismatch is rejected before any synthesis.
    assert fake_tts.synth_calls == []


def test_missing_version_is_unsupported_version(monkeypatch, fake_tts):
    req = json.dumps({"id": 9, "type": "ping"})

    rc, responses = _run_serve_loop(monkeypatch, [req])

    assert responses[0]["error"]["code"] == "UNSUPPORTED_VERSION"
    assert responses[0]["id"] == 9


def test_malformed_json_is_bad_request_with_null_id(monkeypatch, fake_tts):
    rc, responses = _run_serve_loop(monkeypatch, ["{not valid json"])

    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "BAD_REQUEST"
    assert responses[0]["id"] is None


def test_non_object_json_is_bad_request_with_null_id(monkeypatch, fake_tts):
    # A bare JSON array cannot carry protocol fields -> BAD_REQUEST, id None.
    rc, responses = _run_serve_loop(monkeypatch, ["[1, 2, 3]"])

    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "BAD_REQUEST"
    assert responses[0]["id"] is None


def test_whitespace_only_lines_are_skipped(monkeypatch, fake_tts):
    lines = ["   ", "", _synth_req(id=4, text="halo")]

    rc, responses = _run_serve_loop(monkeypatch, lines)

    # Blank lines produce no response; only the synthesize request is answered.
    assert len(responses) == 1
    assert responses[0]["id"] == 4
    assert responses[0]["ok"] is True


# --- success shape & per-request error isolation (reqs 8.1, 8.10) ---------- #


def test_synthesize_success_response_shape(monkeypatch, fake_tts):
    rc, responses = _run_serve_loop(
        monkeypatch, [_synth_req(id=11, text="halo dunia", voice="M3")]
    )

    resp = responses[0]
    assert resp["ok"] is True
    assert resp["id"] == 11
    assert resp["duration"] == fake_tts.duration
    assert resp["duration"] > 0
    assert resp["engine"] == "supertonic-3"
    assert resp["wav_path"].endswith(".wav")
    assert "temp_11_" in resp["wav_path"]
    # The requested voice style was resolved and handed to synthesize.
    assert fake_tts.synth_calls[0]["voice_style"] == "style::M3"


def test_serve_loop_isolates_errors_and_keeps_processing(monkeypatch, fake_tts):
    """A bad request must not stop serve_loop from handling the next one (8.10)."""
    lines = [
        "{not json",  # malformed -> BAD_REQUEST (id None)
        _synth_req(id=1, text=""),  # blank text -> BAD_REQUEST (id 1)
        json.dumps({"v": se.PROTOCOL_VERSION, "id": 2, "type": "bogus"}),  # UNKNOWN_TYPE
        json.dumps({"v": se.PROTOCOL_VERSION, "id": 3, "type": "ping"}),  # pong
        _synth_req(id=4, text="masih jalan"),  # valid -> ok
    ]

    rc, responses = _run_serve_loop(monkeypatch, lines)

    assert rc == 0
    # Every line produced exactly one response, in order.
    assert len(responses) == 5
    assert responses[0]["error"]["code"] == "BAD_REQUEST" and responses[0]["id"] is None
    assert responses[1]["error"]["code"] == "BAD_REQUEST" and responses[1]["id"] == 1
    assert responses[2]["error"]["code"] == "UNKNOWN_TYPE" and responses[2]["id"] == 2
    assert responses[3]["type"] == "pong" and responses[3]["id"] == 3
    # The final valid request still synthesizes despite all the earlier errors.
    assert responses[4]["ok"] is True
    assert responses[4]["id"] == 4
    assert len(fake_tts.synth_calls) == 1


def test_shutdown_breaks_loop_without_response(monkeypatch, fake_tts):
    lines = [
        json.dumps({"v": se.PROTOCOL_VERSION, "id": 1, "type": "ping"}),
        json.dumps({"v": se.PROTOCOL_VERSION, "type": "shutdown"}),
        _synth_req(id=2, text="never reached"),  # after shutdown -> ignored
    ]

    rc, responses = _run_serve_loop(monkeypatch, lines)

    assert rc == 0
    # Only the ping before shutdown is answered; the loop stops at shutdown.
    assert len(responses) == 1
    assert responses[0]["type"] == "pong"
    assert fake_tts.synth_calls == []
