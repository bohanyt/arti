"""Groq streaming helpers — sentence chunks for early TTS (Fase 4)."""

from __future__ import annotations

import json
import re
from typing import Iterator

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Chunk lebih pendek dari ini digabung ke tetangganya. Keluhan operator [date removed]
# ("motongnya ga pas, delay banget"): pemotong polos menjadikan "Wkwk." chunk
# sendiri -> dapat jeda napas 4 dtk sendiri -> terdengar macet, bukan napas —
# dan aturan jiwa "titik per pikiran" justru memperbanyak titik.
_MIN_CHUNK_CHARS = 60

_BATAS_KALIMAT = re.compile(r"[.!?…]+\s+")


def split_indonesian_sentences(text: str, min_chars: int = _MIN_CHUNK_CHARS) -> list[str]:
    """Potong jawaban jadi chunk TTS di BATAS PIKIRAN, bukan tiap titik.

    Dua aturan (17 Agu 2026):
    1. Elipsis ("..."/"…") = pikiran menggantung, tempat TERBURUK untuk jeda
       napas — jangan pernah dipotong di situ.
    2. Kalimat pendek digabung sampai chunk minimal `min_chars` supaya jeda
       antar kalimat jatuh di pergantian pikiran sungguhan.
    """
    text = (text or "").strip()
    if not text:
        return []

    parts: list[str] = []
    last = 0
    for m in _BATAS_KALIMAT.finditer(text):
        if "…" in m.group(0) or ".." in m.group(0):
            continue
        potongan = text[last:m.end()].strip()
        if potongan:
            parts.append(potongan)
        last = m.end()
    ekor = text[last:].strip()
    if ekor:
        parts.append(ekor)

    merged: list[str] = []
    for p in parts:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]} {p}"
        else:
            merged.append(p)
    if len(merged) > 1 and len(merged[-1]) < min_chars:
        merged[-2] = f"{merged[-2]} {merged[-1]}"
        merged.pop()
    return merged or [text]


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
    # Chunk TTS dibentuk ULANG dari teks utuh lewat pemotong pintar ([date removed]):
    # loop separator di atas masih memotong di elipsis & kalimat 1-kata —
    # dia cuma penampung stream, bukan penentu batas napas.
    sentences = split_indonesian_sentences(full) if full else []
    return full, sentences
