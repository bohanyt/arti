"""Ollama Cloud vision — tier 8 fallback akhir."""

from __future__ import annotations

import os
import time

import requests

OLLAMA_CHAT_URL = "https://ollama.com/api/chat"


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
    body = res.json()
    text = body.get("message", {}).get("content", "")
    return str(text).strip(), ms


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
    body = res.json()
    text = body.get("message", {}).get("content", "")
    return str(text).strip(), ms
