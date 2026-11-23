"""Speech timing helpers for audio playback and subtitles."""

import re

import numpy as np


def resample_audio(data, orig_sr, target_sr=44100):
    if orig_sr == target_sr:
        return data, target_sr
    duration = len(data) / orig_sr
    target_length = int(duration * target_sr)
    orig_xs = np.linspace(0, duration, len(data))
    target_xs = np.linspace(0, duration, target_length)
    if len(data.shape) > 1:
        resampled_channels = []
        for i in range(data.shape[1]):
            resampled_channels.append(np.interp(target_xs, orig_xs, data[:, i]))
        return np.column_stack(resampled_channels), target_sr
    else:
        return np.interp(target_xs, orig_xs, data), target_sr

# ------------------------------------------------------------------
# OBS Subtitle helpers (WordBoundary tick parsing)
# ------------------------------------------------------------------
# edge_tts emits offset / duration in HNS Ticks (100-nanosecond units).
# The Word Timings List contract consumed by subtitle.html expects seconds
# as Python floats, so every tick value is divided by HNS_PER_SECOND.
HNS_PER_SECOND = 10_000_000


def _parse_word_boundary(chunk: dict) -> dict | None:
    """Convert a raw edge_tts WordBoundary chunk into a Word Timings entry.

    Returns None (and logs a [SubTitle] diagnostic) on any malformed field —
    missing offset/duration/text, non-numeric or negative offset/duration —
    so the caller's stream loop can skip the chunk and keep iterating.
    Never raises on malformed input.

    `text` is passed through byte-for-byte: no .strip(), no .lower(), no
    unicodedata.normalize. (Requirements 1.4, 1.5, 1.7, 5.10.)
    """
    # Required field presence check.
    try:
        offset = chunk["offset"]
        duration = chunk["duration"]
        text = chunk["text"]
    except (KeyError, TypeError) as e:
        print(f"[SubTitle] Skipping WordBoundary missing field: {e}")
        return None

    # Numeric type check. bool is a subclass of int in Python, so it would
    # pass isinstance(x, int); we explicitly reject it because True/False are
    # not meaningful tick counts.
    def _is_numeric(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if not _is_numeric(offset) or not _is_numeric(duration):
        print(f"[SubTitle] Skipping WordBoundary with non-numeric ticks: {chunk!r}")
        return None

    # Negative-tick guard. (NaN comparisons are always False, so a NaN slips
    # past this check; the upstream generator-test bound (allow_nan=False)
    # keeps NaN out of the input space, matching the design contract.)
    if offset < 0 or duration < 0:
        print(f"[SubTitle] Skipping WordBoundary with negative ticks: {chunk!r}")
        return None

    return {
        "word": text,                                  # byte-for-byte passthrough
        "start": float(offset) / HNS_PER_SECOND,
        "duration": float(duration) / HNS_PER_SECOND,
    }


# ==========================================
# PHRASE TIMING ESTIMATOR (Option C)
# ==========================================
# Supertone TTS doesn't provide word-level timestamps.
# We estimate phrase boundaries proportional to character count.
# Format matches edge_tts word_timings: [{"word": str, "start": float, "duration": float}]

_PUNCTUATION_PHRASE = r"[.!?]"        # hard breaks — always split
_PUNCTUATION_CLAUSE = r"[,;:\u2014\u2013]"  # soft breaks — split if result >= MIN_PHRASE_CHARS
MIN_PHRASE_CHARS = 8                   # merge very short fragments into previous phrase
MAX_PHRASE_CHARS = 60                  # force-split long phrases at word boundary


def _split_into_phrases(text: str) -> list:
    """Split text into phrases by punctuation, merging short fragments."""
    if not text:
        return []

    # First pass: split on hard punctuation
    raw = re.split(r"(?<=[.!?])\s+", text.strip())
    phrases = []
    for segment in raw:
        segment = segment.strip()
        if not segment:
            continue
        # Second pass: split on soft punctuation if segment is long enough
        if len(segment) > MAX_PHRASE_CHARS:
            sub = re.split(r"(?<=[,;:\u2014\u2013])\s*", segment)
            buf = ""
            for part in sub:
                part = part.strip()
                if not part:
                    continue
                if len(buf) + len(part) <= MAX_PHRASE_CHARS:
                    buf = (buf + " " + part).strip() if buf else part
                else:
                    if buf:
                        phrases.append(buf)
                    buf = part
            if buf:
                phrases.append(buf)
        else:
            phrases.append(segment)

    # Merge very short fragments into neighbours
    merged = []
    for p in phrases:
        if merged and len(p) < MIN_PHRASE_CHARS:
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    return merged


def _estimate_phrase_timings(text: str, total_duration: float) -> list:
    """
    Estimate start/duration for each phrase proportional to character count.
    Returns list of {"word": phrase, "start": seconds, "duration": seconds}.
    """
    phrases = _split_into_phrases(text)
    if not phrases:
        return []

    total_chars = sum(len(p) for p in phrases)
    if total_chars == 0:
        return []

    timings = []
    cursor = 0.0
    for phrase in phrases:
        share = len(phrase) / total_chars
        dur = max(total_duration * share, 0.05)  # minimum 50ms per phrase
        timings.append({"word": phrase, "start": round(cursor, 3), "duration": round(dur, 3)})
        cursor += dur
    return timings
