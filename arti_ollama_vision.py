"""Ollama Cloud vision — tier 8 fallback akhir."""

from __future__ import annotations

import os
import time

import requests

OLLAMA_CHAT_URL = "https://ollama.com/api/chat"

# KETUJUH model gratis Ollama Cloud punya kemampuan "thinking" (diperiksa lewat
# /api/show, [date removed]), dan penalarannya dikembalikan di field TERPISAH
# (`message.thinking`) sambil TETAP memakan jatah `num_predict`. Tanpa flag ini
# `nemotron-3-nano:30b` mengembalikan `content` KOSONG TOTAL — 603 karakter
# habis di kepala model, nol sampai ke penonton. Ini penyakit yang sama persis
# dengan qwen3.6 yang butuh `reasoning_effort: "none"` (fix 3baa8ef, "balasan
# tidak lagi kosong"), cuma beda nama parameter.
#
# Terukur: `think=False` juga LEBIH CEPAT di ketiga model yang diuji —
# nano 3.803->2.155ms, minimax 8.571->3.242ms, gpt-oss:20b 2.273->1.389ms.
# Bukan trade-off, murni untung. Lihat docs/MODEL-REGISTRY.md §5.0.
THINK_OFF = False


def _ambil_balasan(body: dict, model_id: str) -> str:
    """Ambil `content`, dan BERSUARA kalau model tetap menjawab lewat `thinking`.

    Kalau suatu model mengabaikan `think: False`, gejalanya adalah balasan
    kosong — jenis kegagalan paling mahal di Arti karena ia SENYAP: penonton
    cuma melihat Arti diam. Lebih baik berisik di log daripada bisu di siaran.
    """
    msg = (body or {}).get("message") or {}
    content = str(msg.get("content") or "").strip()
    if content:
        return content
    thinking = str(msg.get("thinking") or "").strip()
    if thinking:
        print(
            f"[Ollama] {model_id} MENGABAIKAN think=False — balasan kosong, "
            f"{len(thinking)} karakter tertahan di 'thinking'. "
            f"Model ini tidak layak dipakai; lihat docs/MODEL-REGISTRY.md §5.0."
        )
    return ""


def resolve_api_key(config: dict | None = None) -> str:
    cfg = config or {}
    return (
        cfg.get("ollama_api_key")
        or os.environ.get("OLLAMA_API_KEY")
        or ""
    ).strip()


def vision_chat(
    prompt: str,
    jpeg_b64: str,
    *,
    config: dict | None = None,
    model: str | None = None,
    max_tokens: int = 256,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> tuple[str, int]:
    cfg = config or {}
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("OLLAMA_API_KEY missing")

    model_id = model or cfg.get("vision_ollama_model", "gemma4:31b-cloud")
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [jpeg_b64],
            }
        ],
        "stream": False,
        "think": THINK_OFF,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()
    res = requests.post(OLLAMA_CHAT_URL, headers=headers, json=payload, timeout=timeout)
    ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code == 429:
        raise RuntimeError(f"HTTP 429: {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:400]}")
    return _ambil_balasan(res.json(), model_id), ms


def text_chat(
    messages: list,
    *,
    config: dict | None = None,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> tuple[str, int]:
    """Text-only Ollama Cloud chat."""
    cfg = config or {}
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("OLLAMA_API_KEY missing")

    model_id = model or cfg.get("scouter_ollama_model") or cfg.get(
        "vision_ollama_model", "gemma4:31b-cloud"
    )
    payload = {
        "model": model_id,
        "messages": messages,
        "stream": False,
        "think": THINK_OFF,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()
    res = requests.post(OLLAMA_CHAT_URL, headers=headers, json=payload, timeout=timeout)
    ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code == 429:
        raise RuntimeError(f"HTTP 429: {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:400]}")
    return _ambil_balasan(res.json(), model_id), ms
