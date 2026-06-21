"""Shared OpenAI-compatible text chat helper for scouter adapters."""

from __future__ import annotations

import time
from typing import Any

import requests

from arti_vision_openai import get_session, is_rate_limit_error

__all__ = ["text_chat", "is_rate_limit_error"]


def text_chat(
    url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 300,
    temperature: float = 0.2,
    timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> tuple[str, int]:
    """POST chat/completions text-only. Returns (text, latency_ms)."""
    if not api_key:
        raise ValueError("API key missing")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    http = session or get_session()
    t0 = time.perf_counter()
    res = http.post(url, headers=headers, json=payload, timeout=timeout)
    ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code == 429:
        raise RuntimeError(f"HTTP 429: {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:400]}")
    body = res.json()
    text = body["choices"][0]["message"]["content"]
    text = str(text).strip()
    try:
        import arti_api_telemetry as tel

        provider = "openrouter" if "openrouter" in url else "openai_compat"
        tel.record_openai_response(
            subsystem="scouter",
            provider=provider,
            model=model,
            body=body,
            latency_ms=ms,
            config=None,
        )
    except Exception:
        pass
    return text, ms
