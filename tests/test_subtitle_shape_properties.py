"""Property-based test for engine-selection subtitle word-timing shape.

Spec: supertone-tts-upgrade
Task: 5.5 — Property 7 (Engine selection => subtitle word-timing shape)
Validates: Requirements 13.1, 13.2

Property 7
    Validates: Requirements 13.1, 13.2
    The subtitle word-timing list broadcast for an utterance is determined by
    the active TTS engine:

    * Req 13.1 — WHEN the Supertone path produces audio, the TTS_Engine
      broadcasts the full utterance text together with a word-timing list
      containing ZERO entries (``words == []``). Supertone exposes no
      WordBoundary metadata.
    * Req 13.2 — WHEN the edge_tts path produces audio, the TTS_Engine
      broadcasts a word-timing list DERIVED from WordBoundary events, where each
      entry carries the word's text, its start offset in seconds, and its
      duration in seconds (non-empty when the edge stream yields WordBoundary
      events).

Method under test
    ``hermes_vtuber_bridge.TTSEngine.speak()`` and the shared playback tail
    ``_play_wav()`` (Supertone path) plus ``_speak_edge_tts()`` (edge_tts path).

Test approach (capture what gets broadcast)
    The subtitle broadcast helper the playback tail calls is
    ``_subtitle_broadcast(word_timings, full_text)`` (imported into the bridge
    module from ``subtitle_server.broadcast_subtitle``). We monkeypatch it to
    record the ``word_timings`` argument instead of doing network I/O, and force
    ``subtitle_runtime.enabled``/``server_started``/``status_enabled`` True so
    the broadcast path is taken. ``sf.read``/``resample_audio``/``sd.play``/
    ``sd.wait`` are replaced with no-ops so real device/audio work is skipped.

    * Supertone path — install a fake ``SupertoneProcess`` on the engine whose
      ``ensure_alive()`` is a no-op and whose ``request()`` returns a successful
      response carrying a real temp ``wav_path``. The real ``_acquire_supertone``
      then returns ``(wav_path, [])`` and the real ``_play_wav`` broadcasts the
      empty word-timing list. Assert the recorded ``word_timings == []``.
    * edge_tts path — monkeypatch ``edge_tts.Communicate`` so ``.stream()``
      yields a mix of audio chunks and WordBoundary chunks. The real
      ``_speak_edge_tts`` parses the boundaries via ``_parse_word_boundary`` and
      broadcasts the derived timings. Assert the recorded ``word_timings`` equals
      the WordBoundary-derived list (non-empty; each entry has keys
      ``word``/``start``/``duration``).

    CONFIG and subtitle_runtime mutations go through ``monkeypatch`` so they are
    restored automatically; ``tts_is_playing`` is reset to False before each
    driven utterance.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_vtuber_bridge as bridge  # noqa: E402 (path-mutation prerequisite)


# ---------------------------------------------------------------------------
# Recorder + harness
# ---------------------------------------------------------------------------


class _Recorder:
    """Captures the word_timings/full_text passed to the subtitle broadcast."""

    def __init__(self) -> None:
        self.broadcast_calls: list[tuple[list, str]] = []
        self.status_calls: list[tuple] = []
        self.play_calls: int = 0
        # Chunk script replayed by the fake edge_tts Communicate.stream().
        self.edge_chunks: list = []
        # wav_path the fake Supertone subprocess returns.
        self.supertone_wav_path: str | None = None

    def reset(self) -> None:
        self.broadcast_calls.clear()
        self.status_calls.clear()
        self.play_calls = 0
        self.edge_chunks = []
        self.supertone_wav_path = None


class _FakeSupertoneProcess:
    """In-process stand-in for SupertoneProcess used by the Supertone path.

    Only the slice ``_acquire_supertone`` touches is implemented:
    ``ensure_alive()`` (no-op) and ``request()`` (returns a successful response
    echoing the assigned id and carrying a real temp wav_path).
    """

    def __init__(self, wav_path: str):
        self._wav_path = wav_path

    async def ensure_alive(self) -> None:
        return None

    async def request(self, req: dict, timeout: float) -> dict:
        return {
            "v": bridge.PROTOCOL_VERSION,
            "id": req.get("id", 1),
            "ok": True,
            "wav_path": self._wav_path,
            "duration": 1.0,
            "engine": "supertonic-3",
        }


def _make_engine() -> "bridge.TTSEngine":
    """Construct a TTSEngine without running __init__ (which scans audio
    devices via find_virtual_cable). device_id=None matches a host without a
    virtual cable; sd.play is patched so the value is never used for real I/O.
    """
    tts = bridge.TTSEngine.__new__(bridge.TTSEngine)
    tts.device_id = None
    return tts


@pytest.fixture
def harness(monkeypatch):
    record = _Recorder()

    async def fake_broadcast(word_timings, full_text):
        # Copy the list so later mutation by the bridge cannot rewrite history.
        record.broadcast_calls.append((list(word_timings), full_text))

    async def fake_status(status, message=""):
        record.status_calls.append((status, message))

    monkeypatch.setattr(bridge, "_subtitle_broadcast", fake_broadcast)
    monkeypatch.setattr(bridge, "_subtitle_broadcast_status", fake_status)

    def fake_sf_read(path):
        # Decode is irrelevant to the word-timing shape; return a stable 48kHz
        # buffer so resample_audio is a no-op and sd.play has something to take.
        return (np.zeros(1024, dtype=np.float32), 48000)

    monkeypatch.setattr(bridge.sf, "read", fake_sf_read)

    def fake_resample(data, orig_sr, target_sr=44100):
        return data, target_sr

    monkeypatch.setattr(bridge, "resample_audio", fake_resample)

    def fake_play(data, samplerate, device=None):
        record.play_calls += 1

    monkeypatch.setattr(bridge.sd, "play", fake_play)
    monkeypatch.setattr(bridge.sd, "wait", lambda: None)

    # The shared playback tail awaits asyncio.sleep(0.3) before resetting the
    # mic gate. That real delay would otherwise dominate the property run
    # (one tail per example); replace it with a no-op so coverage stays high
    # without the wall-clock cost. The mic-gate ordering is unaffected — the
    # reset still happens after this await, just instantly.
    async def _instant_sleep(_delay):
        return None

    monkeypatch.setattr(bridge.asyncio, "sleep", _instant_sleep)

    # Force the broadcast path to be taken (Req 13.1/13.2 gating). These are
    # restored automatically by monkeypatch after each test.
    monkeypatch.setattr(bridge.subtitle_runtime, "enabled", True)
    monkeypatch.setattr(bridge.subtitle_runtime, "server_started", True)
    monkeypatch.setattr(bridge.subtitle_runtime, "status_enabled", True)

    yield record


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_NONEMPTY_TEXT = st.text(min_size=1, max_size=60)


def _wb_chunk(offset: int, duration: int, text: str) -> dict:
    return {"type": "WordBoundary", "offset": offset,
            "duration": duration, "text": text}


# A single audio chunk (empty payload allowed: the bridge guards `if data:`).
_AUDIO_ITEM = st.builds(lambda d: ("audio", d), st.binary(min_size=0, max_size=32))

# A single WordBoundary descriptor with non-negative, non-bool ticks so
# _parse_word_boundary keeps every one (no skips). Text may be empty — the
# parser passes it through byte-for-byte.
_WB_ITEM = st.builds(
    lambda o, d, t: ("wb", o, d, t),
    st.integers(min_value=0, max_value=10**11),
    st.integers(min_value=0, max_value=10**10),
    st.text(min_size=0, max_size=15),
)


@st.composite
def _mixed_chunks_with_boundary(draw):
    """A mix of audio + WordBoundary items guaranteed to contain at least one
    valid WordBoundary (so edge_tts broadcasts a non-empty derived list).
    """
    items = draw(st.lists(st.one_of(_AUDIO_ITEM, _WB_ITEM),
                          min_size=0, max_size=18))
    guaranteed = draw(_WB_ITEM)
    pos = draw(st.integers(min_value=0, max_value=len(items)))
    items.insert(pos, guaranteed)
    return items


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


def _run_supertone(record: _Recorder, engine, text: str) -> None:
    record.reset()
    bridge.tts_is_playing = False

    # Real temp WAV so _play_wav's owns_temp unlink branch runs against a real
    # path. sf.read is patched, so the file contents are never parsed.
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="supertone_prop_")
    os.close(fd)
    with open(wav_path, "wb") as fh:
        fh.write(b"\x00\x00")
    record.supertone_wav_path = wav_path

    engine.supertone = _FakeSupertoneProcess(wav_path)
    try:
        asyncio.run(engine.speak(text))
    finally:
        if os.path.exists(wav_path):
            try:
                os.unlink(wav_path)
            except OSError:
                pass


def _run_edge_tts(record: _Recorder, engine, text: str, items: list) -> None:
    record.reset()
    bridge.tts_is_playing = False

    chunks: list = []
    expected: list[dict] = []
    for it in items:
        if it[0] == "audio":
            chunks.append({"type": "audio", "data": it[1]})
        else:
            _, offset, duration, wtext = it
            chunks.append(_wb_chunk(offset, duration, wtext))
            expected.append({
                "word": wtext,
                "start": float(offset) / bridge.HNS_PER_SECOND,
                "duration": float(duration) / bridge.HNS_PER_SECOND,
            })
    record.edge_chunks = chunks

    class _FakeCommunicate:
        def __init__(self, text_arg, voice_arg):
            self._text = text_arg

        async def stream(self):
            for chunk in chunks:
                yield chunk

    # edge_tts.Communicate is referenced as bridge.edge_tts.Communicate.
    import unittest.mock as _mock
    with _mock.patch.object(bridge.edge_tts, "Communicate", _FakeCommunicate):
        asyncio.run(engine.speak(text))

    return expected


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


# supertone-tts-upgrade, Property 7 (part A): supertone => empty word timings
# Validates: Requirements 13.1
@given(text=_NONEMPTY_TEXT)
@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_7_supertone_broadcasts_empty_word_timings(harness, monkeypatch,
                                                            text):
    """When CONFIG["tts_engine"] == "supertone", the subtitle broadcast for the
    utterance carries an EMPTY word-timing list.

    **Validates: Requirements 13.1**
    """
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "supertone")
    engine = _make_engine()

    _run_supertone(harness, engine, text)

    # speak() must reach the shared playback tail and broadcast exactly once.
    assert len(harness.broadcast_calls) == 1, (
        "Supertone path must broadcast the utterance exactly once before playback"
    )
    word_timings, full_text = harness.broadcast_calls[0]
    assert word_timings == [], (
        "Supertone path must broadcast a word-timing list with ZERO entries "
        f"(Req 13.1); got {word_timings!r}"
    )
    assert full_text == text, "full text must be broadcast byte-for-byte"
    assert harness.play_calls == 1, "audio must be played via the Supertone path"
    assert bridge.tts_is_playing is False, "mic gate must reset to False on return"


# supertone-tts-upgrade, Property 7 (part B): edge_tts => WordBoundary-derived
# Validates: Requirements 13.2
@given(items=_mixed_chunks_with_boundary(), text=_NONEMPTY_TEXT)
@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_7_edge_tts_broadcasts_wordboundary_derived(harness, monkeypatch,
                                                             items, text):
    """When CONFIG["tts_engine"] == "edge_tts", the subtitle broadcast carries
    WordBoundary-derived word timings (non-empty when the edge stream yields
    WordBoundary events); each entry has word/start/duration.

    **Validates: Requirements 13.2**
    """
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "edge_tts")
    engine = _make_engine()

    expected = _run_edge_tts(harness, engine, text, items)

    # At least one valid WordBoundary was generated, so the edge_tts broadcast
    # gate (enabled + server_started + non-empty word_timings) fires once.
    assert len(harness.broadcast_calls) == 1, (
        "edge_tts must broadcast exactly once when WordBoundary events exist"
    )
    word_timings, full_text = harness.broadcast_calls[0]

    assert full_text == text, "full text must be broadcast byte-for-byte"
    assert len(word_timings) >= 1, (
        "edge_tts word-timing list must be non-empty when boundaries are present"
    )
    # The derived list must match the WordBoundary events in arrival order and
    # carry exactly the documented keys/shape (Req 13.2).
    assert word_timings == expected, (
        "edge_tts word timings must be derived from WordBoundary events "
        f"(offset/duration in seconds); expected {expected!r} got {word_timings!r}"
    )
    for entry in word_timings:
        assert set(entry.keys()) == {"word", "start", "duration"}
        assert isinstance(entry["word"], str)
        assert isinstance(entry["start"], float)
        assert isinstance(entry["duration"], float)
        assert entry["start"] >= 0.0
        assert entry["duration"] >= 0.0

    assert harness.play_calls == 1, "audio must be played via the edge_tts path"
    assert bridge.tts_is_playing is False, "mic gate must reset to False on return"
