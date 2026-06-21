"""Desktop audio loopback → dialogue ring (RAM). No auto-trigger to voice pipeline."""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_MAX_LINES = 50
DEFAULT_POST_TTS_COOLDOWN_SEC = 3.0


@dataclass(frozen=True)
class DialogueEntry:
    wall_ts: float
    text: str


class DialogueRing:
    """Fixed-size RAM ring of recent desktop-audio transcript lines."""

    def __init__(self, max_lines: int = DEFAULT_MAX_LINES):
        self._max = max(1, int(max_lines))
        self._entries: collections.deque[DialogueEntry] = collections.deque(maxlen=self._max)
        self._lock = threading.Lock()

    def append(self, text: str, *, wall_ts: float | None = None) -> None:
        line = (text or "").strip()
        if not line:
            return
        ts = wall_ts if wall_ts is not None else time.time()
        with self._lock:
            self._entries.append(DialogueEntry(wall_ts=ts, text=line))

    def snapshot(self) -> list[DialogueEntry]:
        with self._lock:
            return list(self._entries)

    def format_context(self, max_lines: int = 20, max_chars: int = 2000) -> str:
        lines = self.snapshot()[-max_lines:]
        if not lines:
            return ""
        parts: list[str] = []
        total = 0
        for entry in reversed(lines):
            chunk = entry.text
            if total + len(chunk) > max_chars:
                break
            parts.append(chunk)
            total += len(chunk)
        parts.reverse()
        return "\n".join(parts)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Module-level ring for bridge read access
dialogue_ring = DialogueRing()


def should_accept_desktop_transcript(
    text: str,
    *,
    tts_is_playing: bool,
    last_tts_end: float | None,
    is_echo_of_arti: Callable[[str], bool],
    post_tts_cooldown_sec: float = DEFAULT_POST_TTS_COOLDOWN_SEC,
    now: float | None = None,
) -> bool:
    """Guards: never accept while Arti TTS; skip echo; cooldown after TTS."""
    if not (text or "").strip():
        return False
    if tts_is_playing:
        return False
    if is_echo_of_arti(text):
        return False
    if last_tts_end is not None:
        t = now if now is not None else time.time()
        if t - last_tts_end < post_tts_cooldown_sec:
            return False
    return True


def ingest_desktop_transcript(
    text: str,
    *,
    tts_is_playing: bool,
    last_tts_end: float | None,
    is_echo_of_arti: Callable[[str], bool],
    ring: DialogueRing | None = None,
) -> bool:
    """Append to ring if guards pass. Never queues voice triggers."""
    if not should_accept_desktop_transcript(
        text,
        tts_is_playing=tts_is_playing,
        last_tts_end=last_tts_end,
        is_echo_of_arti=is_echo_of_arti,
    ):
        return False
    target = ring or dialogue_ring
    target.append(text)
    return True


def desktop_audio_worker(
    config: dict,
    *,
    get_tts_is_playing: Callable[[], bool],
    get_last_tts_end: Callable[[], float | None],
    is_echo_of_arti: Callable[[str], bool],
    transcribe_chunk: Callable[..., str | None] | None = None,
    sleep_sec: float = 0.5,
) -> None:
    """Background thread entry. Capture/transcribe when device configured; else idle."""
    device = (config.get("desktop_audio_device") or "").strip()
    if not config.get("desktop_audio_enabled"):
        return
    if not device:
        print("[Desktop Audio] desktop_audio_enabled but no desktop_audio_device — idle")
        while config.get("desktop_audio_enabled"):
            time.sleep(2.0)
        return

    chunk_sec = float(config.get("desktop_audio_chunk_sec", 3.0))
    print(f"[Desktop Audio] Worker started (device={device!r}, chunk={chunk_sec}s)")
    while config.get("desktop_audio_enabled"):
        if get_tts_is_playing():
            time.sleep(sleep_sec)
            continue
        if transcribe_chunk is None:
            time.sleep(1.0)
            continue
        try:
            text = transcribe_chunk()
            if text:
                ingest_desktop_transcript(
                    text,
                    tts_is_playing=get_tts_is_playing(),
                    last_tts_end=get_last_tts_end(),
                    is_echo_of_arti=is_echo_of_arti,
                )
        except Exception as e:
            print(f"[Desktop Audio] Error: {type(e).__name__}: {e}")
        time.sleep(sleep_sec)
