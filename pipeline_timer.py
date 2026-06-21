"""Per-turn pipeline stage timing for Arti latency diagnostics."""

from __future__ import annotations

import time
from typing import Any

_STAGE_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("vts_mikir_ms", "start", "after_mikir"),
    ("rag_ms", "after_mikir", "after_rag"),
    ("llm_ms", "after_rag", "after_llm"),
    ("tts_ms", "after_llm", "after_tts"),
)

_tts_synth_ms: int | None = None
_tts_play_ms: int | None = None
_pending_asr_stages: dict[str, int] | None = None


def pop_asr_stages() -> dict[str, int]:
    """ASR timing captured before queue_voice_trigger (PTT / wake word)."""
    global _pending_asr_stages
    stages = _pending_asr_stages or {}
    _pending_asr_stages = None
    return dict(stages)


def set_pending_asr_stages(stages: dict[str, int] | None) -> None:
    global _pending_asr_stages
    _pending_asr_stages = dict(stages) if stages else None


def note_tts_synth_ms(ms: int) -> None:
    global _tts_synth_ms
    _tts_synth_ms = max(0, int(ms))


def note_tts_play_ms(ms: int) -> None:
    global _tts_play_ms
    _tts_play_ms = max(0, int(ms))


def consume_tts_stages() -> dict[str, int]:
    global _tts_synth_ms, _tts_play_ms
    out: dict[str, int] = {}
    if _tts_synth_ms is not None:
        out["tts_synth_ms"] = _tts_synth_ms
    if _tts_play_ms is not None:
        out["tts_play_ms"] = _tts_play_ms
    _tts_synth_ms = None
    _tts_play_ms = None
    return out


class PipelineTimer:
    """Mark named checkpoints; compute stage deltas in milliseconds."""

    __slots__ = ("_t0", "_marks", "_extra")

    def __init__(self, extra: dict[str, Any] | None = None):
        self._t0 = time.perf_counter()
        self._marks: dict[str, float] = {"start": self._t0}
        self._extra: dict[str, Any] = dict(extra) if extra else {}

    def mark(self, name: str) -> None:
        self._marks[name] = time.perf_counter()

    def _delta_ms(self, start: str, end: str) -> int | None:
        if start not in self._marks or end not in self._marks:
            return None
        return int((self._marks[end] - self._marks[start]) * 1000)

    def stages_ms(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, start, end in _STAGE_PAIRS:
            value = self._delta_ms(start, end)
            if value is not None:
                out[key] = value

        for key, value in self._extra.items():
            if isinstance(value, (int, float)):
                out[key] = int(value)

        tts = consume_tts_stages()
        out.update(tts)
        if "tts_synth_ms" in out or "tts_play_ms" in out:
            out["tts_ms"] = out.get("tts_synth_ms", 0) + out.get("tts_play_ms", 0)

        end_t = self._marks.get("after_tts", self._marks.get("end"))
        if end_t is not None:
            out["total_ms"] = int((end_t - self._t0) * 1000)
        return out


def format_latency_line(stages: dict[str, int]) -> str:
    """One terminal line: [Latency] asr=… rag=… llm=… tts=… total=…"""
    keys = (
        "vad_tail_ms",
        "asr_ms",
        "vts_mikir_ms",
        "rag_ms",
        "llm_ms",
        "tts_synth_ms",
        "tts_play_ms",
        "tts_ms",
        "total_ms",
    )
    parts = []
    for k in keys:
        if k in stages:
            label = k[:-3] if k.endswith("_ms") else k
            parts.append(f"{label}={stages[k]}ms")
    return "[Latency] " + " ".join(parts)
