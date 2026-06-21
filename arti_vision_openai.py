"""Shared OpenAI-compatible multimodal chat helper for vision adapters."""

from __future__ import annotations

import time
from typing import Any

import requests

_session: requests.Session | None = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def vision_chat(
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    jpeg_b64: str,
    *,
    max_tokens: int = 256,
    temperature: float = 0.2,
    timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> tuple[str, int]:
    """POST chat/completions with image. Returns (text, latency_ms)."""
    if not api_key:
        raise ValueError("API key missing")

    data_url = f"data:image/jpeg;base64,{jpeg_b64}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
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

        provider = "openrouter" if "openrouter" in url else "groq" if "groq" in url else "openai_compat"
        tel.record_openai_response(
            subsystem="vision",
            provider=provider,
            model=model,
            body=body,
            latency_ms=ms,
            config=None,
        )
    except Exception:
        pass
    return text, ms


def is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg
