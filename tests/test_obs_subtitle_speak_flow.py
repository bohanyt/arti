"""OBS Subtitle integration — TTSEngine.speak property tests.

Spec: obs-subtitle-integration
Sub-tasks covered: 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11

The function under test is `hermes_vtuber_bridge.TTSEngine.speak`. Every test
swaps `edge_tts.Communicate` with an in-memory fake whose `stream()` yields a
hypothesis-generated chunk list, swaps `sd.play` / `sd.wait` / `sf.read` /
`resample_audio` with no-op recorders, spies on the two subtitle broadcast
helpers, and drives a single utterance via `asyncio.run(tts.speak(text))`.

The TTSEngine instance is built via `__new__` to skip `__init__`'s
`find_virtual_cable()` call, which would otherwise scan real audio devices.
`tts.device_id = None` is set explicitly so the recorder can compare the
device argument against a baseline run.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_vtuber_bridge as bridge  # noqa: E402  (path-mutation prerequisite)


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


class _StreamRaise:
    """Marker class used inside a chunk list to signal the fake stream loop
    should raise an exception at that point. Cannot collide with an edge_tts
    chunk because chunks are dicts and this is a class instance.
    """

    def __init__(self, exc: Exception):
        self.exc = exc


class SpeakRecord:
    """In-memory recorder shared between the harness patches and each test."""

    def __init__(self) -> None:
        # Event log captures the relative ordering of broadcast / status / play
        # so tests can assert that broadcast_subtitle is awaited BEFORE sd.play.
        self.events: list[tuple] = []
        self.broadcast_calls: list[tuple] = []
        self.status_calls: list[tuple] = []
        self.play_calls: list[tuple] = []
        self.wait_calls: int = 0
        self.unlink_calls: list[str] = []
        self.unlink_paths_existed: list[bool] = []

        self.captured_audio: bytes | None = None
        self.communicate_text: str | None = None
        self.communicate_voice: str | None = None

        # tts_is_playing observed AT the moment the sd.play recorder fires —
        # the bridge contract (Req 5.4) requires this to be True.
        self.tts_is_playing_during_play: bool | None = None

        # The chunk script the FakeCommunicate.stream() will replay.
        # Items are either edge_tts-style dicts or _StreamRaise markers.
        self.chunks: list = []

        # Optional injected exceptions (for failure-mode tests).
        self.broadcast_raises: type[BaseException] | None = None
        self.status_raises: type[BaseException] | None = None
        self.unlink_raises: type[BaseException] | None = None

    def reset(self) -> None:
        self.events.clear()
        self.broadcast_calls.clear()
        self.status_calls.clear()
        self.play_calls.clear()
        self.wait_calls = 0
        self.unlink_calls.clear()
        self.unlink_paths_existed.clear()
        self.captured_audio = None
        self.communicate_text = None
        self.communicate_voice = None
        self.tts_is_playing_during_play = None
        self.chunks = []
        self.broadcast_raises = None
        self.status_raises = None
        self.unlink_raises = None


def _make_engine() -> bridge.TTSEngine:
    """Construct a TTSEngine without running __init__ (which scans audio
    devices via find_virtual_cable). device_id=None matches the baseline
    behavior on systems without a virtual cable.
    """
    tts = bridge.TTSEngine.__new__(bridge.TTSEngine)
    tts.device_id = None
    return tts


@pytest.fixture
def harness(monkeypatch):
    """Apply every patch needed to drive `speak()` in isolation.

    The recorder is yielded so tests can configure chunk scripts / failure
    modes per example and read back the captured events. The fixture is
    function-scoped; hypothesis re-uses it across examples (the
    HealthCheck.function_scoped_fixture suppression on each test makes that
    explicit).
    """
    record = SpeakRecord()

    real_unlink = os.unlink  # captured BEFORE we replace bridge.os.unlink

    class FakeCommunicate:
        def __init__(self, text, voice):
            record.communicate_text = text
            record.communicate_voice = voice

        async def stream(self):
            for item in record.chunks:
                if isinstance(item, _StreamRaise):
                    raise item.exc
                yield item

    monkeypatch.setattr(bridge.edge_tts, "Communicate", FakeCommunicate)

    async def fake_broadcast(word_timings, full_text):
        record.events.append(("broadcast", list(word_timings), full_text))
        record.broadcast_calls.append((list(word_timings), full_text))
        if record.broadcast_raises is not None:
            raise record.broadcast_raises("simulated broadcast failure")

    async def fake_status(status, message=""):
        record.events.append(("status", status, message))
        record.status_calls.append((status, message))
        if record.status_raises is not None:
            raise record.status_raises("simulated status failure")

    monkeypatch.setattr(bridge, "_subtitle_broadcast", fake_broadcast)
    monkeypatch.setattr(bridge, "_subtitle_broadcast_status", fake_status)

    def fake_play(data, samplerate, device=None):
        record.events.append(("play", data, samplerate, device))
        record.play_calls.append((data, samplerate, device))
        # Read the live module attribute, NOT the value at fixture setup time.
        record.tts_is_playing_during_play = bridge.tts_is_playing

    def fake_wait():
        record.events.append(("wait",))
        record.wait_calls += 1

    monkeypatch.setattr(bridge.sd, "play", fake_play)
    monkeypatch.setattr(bridge.sd, "wait", fake_wait)

    def fake_sf_read(path):
        # Sniff the actual bytes the bridge wrote so Property 4 can verify
        # byte-for-byte preservation across the stream loop.
        try:
            with open(path, "rb") as f:
                record.captured_audio = f.read()
        except OSError:
            record.captured_audio = b""
        # Return a stable, deterministic decoded buffer. The 24000 Hz source
        # rate forces resample_audio to be exercised; the recorder ignores
        # rate and returns its input unchanged for byte-equal comparisons.
        return (np.zeros(48000, dtype=np.float32), 24000)

    monkeypatch.setattr(bridge.sf, "read", fake_sf_read)

    def fake_resample(data, orig_sr, target_sr=44100):
        record.events.append(("resample", target_sr))
        return data, target_sr

    monkeypatch.setattr(bridge, "resample_audio", fake_resample)

    def fake_unlink(path):
        record.unlink_calls.append(path)
        record.unlink_paths_existed.append(os.path.exists(path))
        if record.unlink_raises is not None:
            raise record.unlink_raises("simulated unlink failure")
        return real_unlink(path)

    monkeypatch.setattr(bridge.os, "unlink", fake_unlink)

    yield record

    # Best-effort cleanup of any tmp file the harness left behind because
    # fake_unlink raised.
    # (The bridge uses NamedTemporaryFile(delete=False); when our unlink
    #  recorder raises, the file persists. Sweep the system tempdir for
    #  orphans created during this fixture's lifetime.)
    import glob
    import tempfile

    for leftover in glob.glob(os.path.join(tempfile.gettempdir(), "tmp*.mp3")):
        try:
            real_unlink(leftover)
        except OSError:
            pass


def _reset_runtime(*, enabled=True, status_enabled=True, server_started=True):
    """Reset the bridge's subtitle runtime + tts_is_playing flag."""
    bridge.subtitle_runtime.enabled = enabled
    bridge.subtitle_runtime.status_enabled = status_enabled
    bridge.subtitle_runtime.server_started = server_started
    bridge.tts_is_playing = False


def _run_speak(record: SpeakRecord, text: str, chunks, *,
               enabled=True, status_enabled=True, server_started=True,
               broadcast_raises=None, status_raises=None, unlink_raises=None):
    """One-shot helper: reset state, prime chunk script, drive speak()."""
    record.reset()
    record.chunks = list(chunks)
    record.broadcast_raises = broadcast_raises
    record.status_raises = status_raises
    record.unlink_raises = unlink_raises
    _reset_runtime(enabled=enabled, status_enabled=status_enabled,
                   server_started=server_started)
    asyncio.run(_make_engine().speak(text))


# ---------------------------------------------------------------------------
# Hypothesis strategies for stream chunks
# ---------------------------------------------------------------------------

# Audio chunk payloads. Empty bytes are allowed: the bridge guards `if data:`
# before writing, so an empty payload contributes no bytes to the file.
_AUDIO_CHUNK = st.builds(
    lambda data: {"type": "audio", "data": data},
    st.binary(min_size=0, max_size=64),
)

# Unknown chunk type — the bridge contract says these are ignored without
# breaking the loop.
_UNKNOWN_CHUNK = st.builds(
    lambda t: {"type": t, "data": b"ignored"},
    st.sampled_from(["SentenceBoundary", "Viseme", "Bookmark", "unknown"]),
)


def _wb(offset: int, duration: int, text: str) -> dict:
    return {"type": "WordBoundary", "offset": offset,
            "duration": duration, "text": text}


# Valid WordBoundary chunks (non-negative ticks, non-empty text). Used by
# tests that need at least one well-formed boundary to land in the Word
# Timings List.
_VALID_WORDBOUNDARY = st.builds(
    _wb,
    st.integers(min_value=0, max_value=10**11),
    st.integers(min_value=0, max_value=10**10),
    st.text(min_size=0, max_size=20),
)


def _ascending_wordboundaries(min_size=1, max_size=8):
    """Generator that yields a list of WordBoundary chunks whose offsets are
    monotonically non-decreasing (matches the edge_tts contract for left-to-
    right speech). Property 3 asserts this invariant survives the stream loop.
    """

    return st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10**8),  # gap to previous offset
            st.integers(min_value=0, max_value=10**8),  # duration
            st.text(min_size=0, max_size=20),
        ),
        min_size=min_size, max_size=max_size,
    ).map(_inflate_ascending)


def _inflate_ascending(triples):
    out = []
    cur = 0
    for gap, duration, text in triples:
        cur += gap
        out.append(_wb(cur, duration, text))
    return out


# Malformed WordBoundary variants used by Property 7's failure-mode coverage.
# Each variant is something `_parse_word_boundary` is contractually obliged to
# skip without raising.
def _malformed_wordboundary_strategy():
    drop_offset = st.fixed_dictionaries({
        "type": st.just("WordBoundary"),
        "duration": st.integers(min_value=0, max_value=10**6),
        "text": st.text(max_size=10),
    })
    drop_duration = st.fixed_dictionaries({
        "type": st.just("WordBoundary"),
        "offset": st.integers(min_value=0, max_value=10**6),
        "text": st.text(max_size=10),
    })
    drop_text = st.fixed_dictionaries({
        "type": st.just("WordBoundary"),
        "offset": st.integers(min_value=0, max_value=10**6),
        "duration": st.integers(min_value=0, max_value=10**6),
    })
    negative = st.builds(
        _wb,
        st.integers(min_value=-10**6, max_value=-1),
        st.integers(min_value=-10**6, max_value=-1),
        st.text(max_size=10),
    )
    non_numeric = st.builds(
        lambda t: {"type": "WordBoundary", "offset": "nope",
                   "duration": "nope", "text": t},
        st.text(max_size=10),
    )
    return st.one_of(drop_offset, drop_duration, drop_text, negative, non_numeric)


_MALFORMED_WORDBOUNDARY = _malformed_wordboundary_strategy()


# A mixed chunk list with arbitrary interleaving — used by Properties 4, 5, 6.
_MIXED_CHUNKS = st.lists(
    st.one_of(_AUDIO_CHUNK, _VALID_WORDBOUNDARY, _UNKNOWN_CHUNK),
    min_size=1, max_size=20,
)


# A chunk list guaranteed to contain ZERO well-formed WordBoundary entries.
# (Malformed boundaries are still permitted — the bridge will skip them and
#  end up with an empty Word Timings List, which is the Property 6 trigger.)
_NO_WORDBOUNDARY_CHUNKS = st.lists(
    st.one_of(_AUDIO_CHUNK, _UNKNOWN_CHUNK),
    min_size=1, max_size=15,
)


_NONEMPTY_TEXT = st.text(min_size=1, max_size=80)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


# Feature: obs-subtitle-integration, Property 4: Audio bytes preserved across the stream loop
# Validates: Requirements 1.2, 1.3
@given(chunks=_MIXED_CHUNKS, text=_NONEMPTY_TEXT)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_4_audio_bytes_preserved(harness, chunks, text):
    expected = b"".join(c["data"] for c in chunks
                        if isinstance(c, dict) and c.get("type") == "audio")
    _run_speak(harness, text, chunks)
    assert harness.captured_audio == expected, (
        "bytes written to tmp MP3 must equal concatenation of audio chunk "
        "payloads in arrival order"
    )


# Feature: obs-subtitle-integration, Property 3: Word Timings List shape and ordering
# Validates: Requirements 1.6
@given(boundaries=_ascending_wordboundaries(min_size=1, max_size=8),
       audio_padding=st.lists(_AUDIO_CHUNK, min_size=0, max_size=4),
       text=_NONEMPTY_TEXT)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_3_word_timings_shape_and_ordering(harness, boundaries,
                                                    audio_padding, text):
    # Interleave audio chunks among the boundaries while preserving the
    # boundary order.
    chunks = list(audio_padding) + list(boundaries)
    # Move audio in front; boundaries stay in ascending order.
    _run_speak(harness, text, chunks)

    assert len(harness.broadcast_calls) == 1, (
        "exactly one broadcast must fire for a non-empty Word Timings List"
    )
    word_timings, _ = harness.broadcast_calls[0]
    assert len(word_timings) == len(boundaries)
    prev_start = -1.0
    for entry in word_timings:
        assert set(entry.keys()) == {"word", "start", "duration"}
        assert isinstance(entry["word"], str)
        assert isinstance(entry["start"], float)
        assert isinstance(entry["duration"], float)
        assert entry["start"] >= 0.0
        assert entry["duration"] >= 0.0
        assert entry["start"] >= prev_start, (
            "start values must be monotonically non-decreasing"
        )
        prev_start = entry["start"]


# Feature: obs-subtitle-integration, Property 5: One subtitle broadcast per non-empty utterance
# Validates: Requirements 2.1, 2.2, 2.4, 2.8
@given(boundaries=_ascending_wordboundaries(min_size=1, max_size=6),
       audio=st.lists(_AUDIO_CHUNK, min_size=0, max_size=5),
       unknown=st.lists(_UNKNOWN_CHUNK, min_size=0, max_size=3),
       text=_NONEMPTY_TEXT)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_5_one_broadcast_before_play(harness, boundaries, audio,
                                              unknown, text):
    # Boundaries kept in their generated order; audio + unknown chunks
    # interleave around them at the head.
    chunks = list(audio) + list(unknown) + list(boundaries)
    _run_speak(harness, text, chunks)

    assert len(harness.broadcast_calls) == 1, (
        "broadcast_subtitle must be invoked exactly once per non-empty utterance"
    )
    _, full_text = harness.broadcast_calls[0]
    assert full_text == text, "full_text must equal speak()'s text byte-for-byte"

    # Locate broadcast and play in the event log; the broadcast event MUST
    # come before any play event.
    event_kinds = [evt[0] for evt in harness.events]
    assert "broadcast" in event_kinds and "play" in event_kinds
    first_broadcast = event_kinds.index("broadcast")
    first_play = event_kinds.index("play")
    assert first_broadcast < first_play, (
        "broadcast_subtitle must complete BEFORE sd.play"
    )


# Feature: obs-subtitle-integration, Property 6: Empty Word Timings List skips broadcast_subtitle
# Validates: Requirements 2.5
@given(chunks=_NO_WORDBOUNDARY_CHUNKS, text=_NONEMPTY_TEXT)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_6_empty_word_timings_skips_broadcast(harness, chunks, text):
    _run_speak(harness, text, chunks)

    assert harness.broadcast_calls == [], (
        "broadcast_subtitle must be skipped when Word Timings List is empty"
    )
    assert len(harness.play_calls) == 1, (
        "sd.play must still fire even when broadcast is skipped"
    )


# Feature: obs-subtitle-integration, Property 7: Subtitle failures never block or alter audio
# Validates: Requirements 4.1, 4.3, 4.4, 4.8, 5.4, 5.5
_FAILURE_MODES = ["server_started_false", "broadcast_raises",
                  "broadcast_status_raises", "malformed_boundaries"]


@given(boundaries=_ascending_wordboundaries(min_size=1, max_size=4),
       malformed=st.lists(_MALFORMED_WORDBOUNDARY, min_size=1, max_size=3),
       audio=st.lists(_AUDIO_CHUNK, min_size=1, max_size=4),
       failure=st.sampled_from(_FAILURE_MODES),
       text=_NONEMPTY_TEXT)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_7_subtitle_failures_dont_alter_audio(harness, boundaries,
                                                       malformed, audio,
                                                       failure, text):
    base_chunks = list(audio) + list(boundaries)

    # Baseline: subtitles disabled. Same chunk script, no broadcasts.
    _run_speak(harness, text, base_chunks, enabled=False)
    assert harness.tts_is_playing_during_play is True, (
        "tts_is_playing must be True at the moment sd.play fires"
    )
    assert bridge.tts_is_playing is False, (
        "tts_is_playing must reset to False after speak() returns"
    )
    assert len(harness.play_calls) == 1
    baseline_data, baseline_sr, baseline_dev = harness.play_calls[0]
    baseline_bytes = baseline_data.tobytes()

    # Failure mode run.
    kwargs = {"enabled": True, "server_started": True}
    chunks = list(base_chunks)
    if failure == "server_started_false":
        kwargs["server_started"] = False
    elif failure == "broadcast_raises":
        kwargs["broadcast_raises"] = RuntimeError
    elif failure == "broadcast_status_raises":
        kwargs["status_raises"] = RuntimeError
    elif failure == "malformed_boundaries":
        chunks = list(audio) + list(malformed) + list(boundaries)

    _run_speak(harness, text, chunks, **kwargs)

    assert len(harness.play_calls) == 1, (
        "sd.play must still fire exactly once under any subtitle failure mode"
    )
    fail_data, fail_sr, fail_dev = harness.play_calls[0]
    assert fail_data.tobytes() == baseline_bytes, (
        "audio bytes passed to sd.play must equal the no-subtitle baseline"
    )
    assert fail_sr == baseline_sr
    assert fail_dev == baseline_dev
    assert harness.tts_is_playing_during_play is True
    assert bridge.tts_is_playing is False, (
        "tts_is_playing must always end at False (False -> True -> False)"
    )


# Feature: obs-subtitle-integration, Property 8: tts_is_playing assignment ordering
# Validates: Requirements 5.4
def test_property_8_tts_is_playing_assignment_ordering():
    """Static AST check: in TTSEngine.speak the `tts_is_playing = True`
    statement is immediately followed by `sd.play(...)` with no `await`
    expression between them in the same statement-block ordering.
    """
    src = (REPO_ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    speak_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TTSEngine":
            for item in node.body:
                if (isinstance(item, ast.AsyncFunctionDef)
                        and item.name == "speak"):
                    speak_node = item
                    break
    assert speak_node is not None, "TTSEngine.speak not found"

    # Walk every statement-list (function body, try/except branches, etc.)
    # looking for `tts_is_playing = True` and verify the next statement is
    # an `sd.play(...)` Call.
    found_pairing = False

    def _check_block(stmts):
        nonlocal found_pairing
        for i, stmt in enumerate(stmts):
            if (isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "tts_is_playing"
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is True):
                assert i + 1 < len(stmts), (
                    "tts_is_playing = True must be followed by another statement"
                )
                next_stmt = stmts[i + 1]
                assert isinstance(next_stmt, ast.Expr), (
                    "statement after tts_is_playing = True must be an "
                    "expression call (sd.play), not %r" % type(next_stmt)
                )
                call = next_stmt.value
                assert isinstance(call, ast.Call), (
                    "statement after tts_is_playing = True must be a Call"
                )
                func = call.func
                assert isinstance(func, ast.Attribute), (
                    "statement after tts_is_playing = True must be sd.play(...)"
                )
                assert (isinstance(func.value, ast.Name)
                        and func.value.id == "sd"
                        and func.attr == "play"), (
                    "expected sd.play(...) right after tts_is_playing = True, "
                    "got %s.%s" % (
                        getattr(func.value, "id", "?"), func.attr,
                    )
                )
                # Awaitables anywhere in the assignment statement or the
                # immediately following sd.play call would violate Req 5.4.
                for sub in ast.walk(stmt):
                    assert not isinstance(sub, ast.Await), (
                        "no await may appear in the tts_is_playing assignment"
                    )
                for sub in ast.walk(next_stmt):
                    assert not isinstance(sub, ast.Await), (
                        "no await may appear in the sd.play call statement"
                    )
                found_pairing = True

    # Walk every nested statement list inside speak.
    for node in ast.walk(speak_node):
        for attr in ("body", "orelse", "finalbody", "handlers"):
            value = getattr(node, attr, None)
            if isinstance(value, list):
                # ExceptHandler items have their own body; skip non-stmt lists.
                if value and isinstance(value[0], ast.stmt):
                    _check_block(value)

    assert found_pairing, (
        "tts_is_playing = True followed by sd.play(...) pairing not found in "
        "TTSEngine.speak"
    )


# Feature: obs-subtitle-integration, Property 9: Best-effort temp file deletion
# Validates: Requirements 5.7
@given(execution_path=st.sampled_from(["success", "mid_utterance_failure",
                                       "zero_byte_failure"]),
       unlink_behavior=st.sampled_from(["ok", "permission_error", "os_error"]),
       text=_NONEMPTY_TEXT)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_9_best_effort_unlink(harness, execution_path,
                                       unlink_behavior, text):
    if execution_path == "success":
        chunks = [
            {"type": "audio", "data": b"\x00\x01\x02\x03"},
            _wb(0, 1_000_000, "halo"),
            {"type": "audio", "data": b"\x04\x05\x06"},
        ]
    elif execution_path == "mid_utterance_failure":
        chunks = [
            {"type": "audio", "data": b"\x00\x01\x02\x03"},
            _StreamRaise(RuntimeError("mid-utterance boom")),
        ]
    else:  # zero_byte_failure
        chunks = [_StreamRaise(RuntimeError("zero-byte boom"))]

    raises = None
    if unlink_behavior == "permission_error":
        raises = PermissionError
    elif unlink_behavior == "os_error":
        raises = OSError

    # speak() must NEVER propagate the unlink exception.
    _run_speak(harness, text, chunks, unlink_raises=raises)

    assert harness.unlink_calls, (
        "an unlink attempt must be made when tmp file existed at return time"
    )
    # Every recorded unlink attempt observed the file actually existing on
    # disk at the moment it was called (Req 5.7 says "if and only if the path
    # exists on disk"). The path may be deleted by a successful first call
    # before a follow-up gets recorded; we only require the FIRST attempt to
    # have seen an existing file.
    assert harness.unlink_paths_existed[0] is True, (
        "first unlink call must observe the tmp file existing on disk"
    )


# Feature: obs-subtitle-integration, Property 12: subtitle_enabled = False fully suppresses subtitle work
# Validates: Requirements 3.2
@given(boundaries=_ascending_wordboundaries(min_size=1, max_size=4),
       audio=st.lists(_AUDIO_CHUNK, min_size=1, max_size=4),
       text=_NONEMPTY_TEXT)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_12_subtitle_enabled_false_suppresses_all(harness, boundaries,
                                                           audio, text):
    chunks = list(audio) + list(boundaries)

    # Baseline: subtitles enabled and server up.
    _run_speak(harness, text, chunks, enabled=True, status_enabled=True,
               server_started=True)
    assert len(harness.play_calls) == 1
    enabled_data, enabled_sr, enabled_dev = harness.play_calls[0]
    enabled_bytes = enabled_data.tobytes()
    # Sanity: the baseline run DID broadcast (so the suppression test below
    # is not vacuously trivial).
    assert len(harness.broadcast_calls) == 1
    assert len(harness.status_calls) == 2  # speaking + idle

    # subtitle_enabled = False run.
    _run_speak(harness, text, chunks, enabled=False, status_enabled=True,
               server_started=True)
    assert harness.broadcast_calls == [], (
        "broadcast_subtitle must not be called when subtitle_enabled is False"
    )
    assert harness.status_calls == [], (
        "broadcast_status must not be called when subtitle_enabled is False"
    )
    assert len(harness.play_calls) == 1, "audio must still play"
    disabled_data, disabled_sr, disabled_dev = harness.play_calls[0]
    assert disabled_data.tobytes() == enabled_bytes, (
        "audio bytes passed to sd.play must be byte-identical between "
        "enabled and disabled runs"
    )
    assert disabled_sr == enabled_sr
    assert disabled_dev == enabled_dev
