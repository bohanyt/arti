"""Groq streaming helpers — sentence chunks for early TTS (Fase 4)."""

from __future__ import annotations

import json
import re
from typing import Iterator

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Chunk lebih pendek dari ini digabung ke tetangganya. Keluhan Bohan 17 Agu
# ("[recorded remark omitted]"): pemotong polos menjadikan "[kata pendek]" chunk
# sendiri -> dapat jeda napas 4 dtk sendiri -> terdengar macet, bukan napas —
# dan aturan gaya tentang tanda titik justru memperbanyak titik.
_MIN_CHUNK_CHARS = 60

_BATAS_KALIMAT = re.compile(r"[.!?…]+\s+")

# Chunk TTS terpanjang (2026-10-23). Jawaban kini boleh berisi kalimat panjang
# (batas aman 900 ch, kalimat kabur utuh sampai 1800 ch), padahal Supertone
# mensintesis per chunk dengan supertonic_timeout_sec = 45 dtk. Patokan:
# satu kalimat rencana ~95 ch (arti_reply_yt_chars_per_sentence) dan sintesis
# GPU p50 2,0 dtk/kalimat (komentar supertonic_use_cuda, GPU uji) -> 280 ch
# ~6 dtk, >7x di bawah timeout. 280 ~ 3x kalimat normal, jadi jawaban biasa
# tidak pernah dipecah (prosodi tetap). Pemecahan HANYA untuk sintesis: teks
# jawaban (subtitle/history) tidak diubah; chunk memuat kata yang sama persis.
# (Fallback CPU 29,4 dtk/kalimat tidak tertolong ukuran chunk mana pun.)
_MAX_CHUNK_CHARS = 280
_BATAS_KLAUSA = re.compile(r"[,;:]\s+|\s+[—–-]\s+|—\s*")


def _pecah_chunk_panjang(chunk: str, max_chars: int) -> list[str]:
    """Pecah chunk > max_chars di koma/titik koma/titik dua/tanda pisah
    terakhir yang muat; tanpa itu, di spasi terakhir. Elipsis tidak dipakai."""
    out: list[str] = []
    sisa = chunk.strip()
    while len(sisa) > max_chars:
        jendela = sisa[: max_chars + 1]
        potong = 0
        for m in _BATAS_KLAUSA.finditer(jendela):
            if m.end() <= len(jendela) and m.start() > 0:
                potong = m.end()
        if potong < max_chars // 3:
            spasi = jendela.rfind(" ")
            potong = spasi + 1 if spasi > 0 else max_chars
        bagian = sisa[:potong].strip()
        if bagian:
            out.append(bagian)
        sisa = sisa[potong:].strip()
    if sisa:
        out.append(sisa)
    return out


def split_indonesian_sentences(
    text: str,
    min_chars: int = _MIN_CHUNK_CHARS,
    max_chars: int = _MAX_CHUNK_CHARS,
) -> list[str]:
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
    if max_chars and max_chars > 0:
        # 3. Chunk kepanjangan dipecah di batas klausa — untuk SINTESIS saja.
        merged = [p for chunk in merged for p in _pecah_chunk_panjang(chunk, max_chars)]
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


def extract_finish_reason(chunk: dict):
    """finish_reason chunk SSE (biasanya cuma di chunk terakhir), atau None."""
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    return choices[0].get("finish_reason")


def collect_streaming_reply(
    response_iter: Iterator[bytes],
    meta: dict | None = None,
) -> tuple[str, list[str]]:
    """Collect SSE stream into full text and sentence chunks.

    `meta` (opsional) diisi `finish_reason` dari chunk terakhir yang
    membawanya ("stop" / "length" / None kalau stream tidak melapor) —
    supaya pemanggil bisa membuang kalimat yang kepotong max_tokens tanpa
    mengubah bentuk nilai kembalian.
    """
    buffer = ""
    sentences: list[str] = []
    finish = None
    for chunk in iter_sse_json_lines(response_iter):
        buffer += extract_delta_text(chunk)
        finish = extract_finish_reason(chunk) or finish
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
    # Chunk TTS dibentuk ULANG dari teks utuh lewat pemotong pintar (17 Agu):
    # loop separator di atas masih memotong di elipsis & kalimat 1-kata —
    # dia cuma penampung stream, bukan penentu batas napas.
    sentences = split_indonesian_sentences(full) if full else []
    if meta is not None:
        meta["finish_reason"] = finish
    return full, sentences
