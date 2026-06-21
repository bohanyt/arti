"""Screen capture context — semantic scene + OCR timecode in RAM (no vault embed)."""

from __future__ import annotations

import collections
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_RING_SIZE = 5


@dataclass
class ScreenSnapshot:
    wall_ts: float
    scene: str
    playback_mmss: str | None = None
    ocr_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_ts": self.wall_ts,
            "scene": self.scene,
            "playback_mmss": self.playback_mmss,
            "ocr_text": self.ocr_text,
        }


class ScreenRing:
    """RAM-only ring of recent screen snapshots."""

    def __init__(self, max_size: int = DEFAULT_RING_SIZE):
        self._max = max(1, int(max_size))
        self._items: collections.deque[ScreenSnapshot] = collections.deque(maxlen=self._max)
        self._lock = threading.Lock()

    def push(self, snapshot: ScreenSnapshot) -> None:
        with self._lock:
            self._items.append(snapshot)

    def snapshot(self) -> list[ScreenSnapshot]:
        with self._lock:
            return list(self._items)

    def latest(self) -> ScreenSnapshot | None:
        with self._lock:
            return self._items[-1] if self._items else None


screen_ring = ScreenRing()


@dataclass
class WatchState:
    event_id: str = ""
    playback_mmss: str | None = None
    scene_ring: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "playback_mmss": self.playback_mmss,
            "scene_ring": self.scene_ring,
            "updated_at": self.updated_at,
        }


watch_state = WatchState()


def build_vision_prompt() -> str:
    """Structured prompt for one-call scene + timecode OCR (Vision Prompt Contract)."""
    return (
        "Analisis screenshot layar. Balas HANYA satu objek JSON valid (tanpa markdown, tanpa ```).\n\n"
        "Keys wajib:\n"
        '  "scene": string — tepat 1 kalimat Bahasa Indonesia: jenis layar '
        "(game / YouTube / desktop / chat / lainnya) + elemen utama. Bukan paragraf. Bukan bahasa Inggris.\n"
        '  "playback_mmss": string "mm:ss" ATAU null JSON — HANYA jika bilah kontrol video '
        "YouTube/player terlihat jelas (tombol play + elapsed kiri bawah). "
        'Jika tidak ada player: null (bukan string "null", bukan "N/A").\n'
        '  "ocr_text": string — MAKS 200 karakter. Hanya: judul video, timecode on-screen, '
        "subtitle 1 baris, atau label UI penting. JANGAN salin full log terminal, "
        "JANGAN dump chat history, JANGAN ulang isi scene.\n\n"
        "Aturan:\n"
        "- Jangan tebak plot atau isi video dari teks kecil.\n"
        "- Jika layar penuh teks (terminal/log): scene sebut \"terminal\" atau \"log\", "
        "ocr_text ambil 1–2 baris teratas yang relevan saja.\n"
        "- Output total singkat; model tidak perlu reasoning panjang."
    )


_NULL_MMSS = frozenset({"", "null", "none", "n/a", "na"})


def _normalize_playback_mmss(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _NULL_MMSS:
        return None
    import re

    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        return s
    return None


def parse_vision_response(
    raw: str,
    *,
    scene_max_chars: int = 300,
    ocr_max_chars: int = 200,
) -> ScreenSnapshot:
    """Parse model JSON (best-effort) with contract normalization."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ScreenSnapshot(
            wall_ts=time.time(),
            scene=text[:200],
            playback_mmss=None,
            ocr_text="",
        )
    return ScreenSnapshot(
        wall_ts=time.time(),
        scene=str(data.get("scene", "")).strip()[:scene_max_chars],
        playback_mmss=_normalize_playback_mmss(data.get("playback_mmss")),
        ocr_text=str(data.get("ocr_text", "")).strip()[:ocr_max_chars],
    )


def format_screen_context(ring: ScreenRing | None = None, max_chars: int = 200) -> str:
    latest = (ring or screen_ring).latest()
    if not latest or not latest.scene:
        return ""
    return latest.scene[:max_chars]


def update_watch_state_from_snapshot(
    snap: ScreenSnapshot,
    *,
    event_id: str = "",
    ring: ScreenRing | None = None,
    state: WatchState | None = None,
) -> None:
    target_ring = ring or screen_ring
    target_state = state or watch_state
    target_ring.push(snap)
    target_state.playback_mmss = snap.playback_mmss
    if event_id:
        target_state.event_id = event_id
    target_state.scene_ring = [s.to_dict() for s in target_ring.snapshot()]
    target_state.updated_at = snap.wall_ts


def screen_watcher_worker(
    config: dict,
    *,
    capture_and_describe: Any | None = None,
    sleep_sec: float | None = None,
) -> None:
    """Background thread; calls vision provider when capture_fn wired."""
    enabled = config.get("vision_enabled", config.get("screen_context_enabled", False))
    if not enabled:
        return
    if not config.get("vision_background_poll", False):
        return
    if not config.get("vision_runtime_on", False):
        return
    interval = float(
        sleep_sec
        if sleep_sec is not None
        else config.get("vision_refresh_sec", config.get("screen_context_interval_sec", 10.0))
    )
    max_chars = int(config.get("vision_scene_max_chars", config.get("screen_context_max_chars", 200)))
    print(f"[Screen] Watcher started (interval={interval}s)")
    while (
        config.get("vision_enabled", config.get("screen_context_enabled", False))
        and config.get("vision_background_poll", False)
        and config.get("vision_runtime_on", False)
    ):
        if capture_and_describe is None:
            time.sleep(interval)
            continue
        try:
            snap, provider = capture_and_describe()
            if snap and snap.scene:
                if len(snap.scene) > max_chars:
                    snap.scene = snap.scene[:max_chars]
                update_watch_state_from_snapshot(
                    snap,
                    event_id=str(config.get("watch_party_event_id") or ""),
                )
                print(
                    f"[Screen] vision_provider={provider} scene={snap.scene[:60]!r} "
                    f"playback={snap.playback_mmss} ocr_len={len(snap.ocr_text)}"
                )
        except Exception as e:
            print(f"[Screen] Error: {type(e).__name__}: {e}")
        time.sleep(interval)
