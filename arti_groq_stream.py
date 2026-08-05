"""Groq streaming helpers — sentence chunks for early TTS (Fase 4)."""

from __future__ import annotations

import json
import re
from typing import Iterator

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def split_indonesian_sentences(text: str) -> list[str]:
    """Split reply into speakable sentence chunks."""
    text = (text or "").strip()
    if not text:
        return []
    parts = _SENTENCE_SPLIT.split(text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out or [text]


def iter_sse_json_lines(raw_iter: Iterator[bytes]) -> Iterator[dict]:
    """Parse OpenAI-compatible SSE data lines from a streaming HTTP body."""
    for raw_line in raw_iter:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def extract_delta_text(chunk: dict) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")


def collect_streaming_reply(
    response_iter: Iterator[bytes],
) -> tuple[str, list[str]]:
    """Collect SSE stream into full text and sentence chunks."""
    buffer = ""
    sentences: list[str] = []
    for chunk in iter_sse_json_lines(response_iter):
        buffer += extract_delta_text(chunk)
        while True:
            # Cari separator kalimat yang muncul PALING AWAL di buffer.
            # (Versi lama cuma cek ". " lalu break — infinite loop kalau
            # buffer berisi "! "/"? " tanpa ". ".)
            best_idx = -1
            best_sep = ""
            for sep in (". ", "! ", "? ", "… "):
                idx = buffer.find(sep)
                if idx != -1 and (best_idx == -1 or idx < best_idx):
                    best_idx = idx
                    best_sep = sep
            if best_idx == -1:
                break
            sentence = buffer[: best_idx + len(best_sep)].strip()
            buffer = buffer[best_idx + len(best_sep) :]
            if sentence:
                sentences.append(sentence)
    tail = buffer.strip()
    if tail:
        sentences.append(tail)
    full = " ".join(sentences) if sentences else buffer.strip()
    if not sentences and full:
        sentences = split_indonesian_sentences(full)
    return full, sentences
