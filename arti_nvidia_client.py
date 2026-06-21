"""NVIDIA NIM OpenAI-compatible client — auxiliary brain (DiffusionGemma), not main LLM."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_NVIDIA_MODEL = "google/diffusiongemma-26b-a4b-it"

_session: requests.Session | None = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def resolve_api_key(config: dict | None = None) -> str:
    cfg = config or {}
    return (cfg.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY") or "").strip()


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    config: dict | None = None,
    model: str | None = None,
    max_tokens: int = 150,
    temperature: float = 1.0,
    timeout: float = 30.0,
    stream: bool = False,
    session: requests.Session | None = None,
    telemetry_subsystem: str = "llm",
) -> tuple[str, int]:
    """Call NVIDIA NIM chat/completions. Returns (reply_text, llm_ms)."""
    cfg = config or {}
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("NVIDIA API key missing (nvidia_api_key / NVIDIA_API_KEY)")

    model_id = model or cfg.get("nvidia_model") or DEFAULT_NVIDIA_MODEL
    http = session or get_session()
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()
    res = http.post(
        f"{NVIDIA_API_BASE}/chat/completions",
        headers=headers,
        json=payload,
        timeout=timeout,
        stream=stream,
    )
    llm_ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code != 200:
        raise RuntimeError(f"NVIDIA HTTP {res.status_code}: {res.text[:400]}")
    if stream:
        raise NotImplementedError("NVIDIA stream parsing not implemented in this client")
    body = res.json()
    text = body["choices"][0]["message"]["content"]
    try:
        import arti_api_telemetry as tel

        tel.record_openai_response(
            subsystem=telemetry_subsystem,
            provider="nvidia",
            model=model_id,
            body=body,
            latency_ms=llm_ms,
            config=cfg,
        )
    except Exception:
        pass
    return str(text).strip(), llm_ms


def vision_chat(
    prompt: str,
    jpeg_b64: str,
    *,
    config: dict | None = None,
    model: str | None = None,
    max_tokens: int = 256,
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> tuple[str, int]:
    """Multimodal describe via NVIDIA NIM."""
    data_url = f"data:image/jpeg;base64,{jpeg_b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    cfg = config or {}
    model_id = model or cfg.get("vision_nvidia_model") or cfg.get("nvidia_model") or DEFAULT_NVIDIA_MODEL
    return chat_completion(
        messages,
        config=cfg,
        model=model_id,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        telemetry_subsystem="vision",
    )
