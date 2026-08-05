"""Z.ai GLM-4.6V-Flash vision — tier 7 penultimat."""

from __future__ import annotations

import os

import arti_vision_openai as oai

ZAI_VISION_URL = "https://api.z.ai/api/paas/v4/chat/completions"


def resolve_api_key(config: dict | None = None) -> str:
    cfg = config or {}
    return (
        cfg.get("zai_api_key")
        or os.environ.get("ZAI_API_KEY")
        or os.environ.get("ZHIPU_API_KEY")
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
    timeout: float = 90.0,
) -> tuple[str, int]:
    cfg = config or {}
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("ZAI_API_KEY missing")

    model_id = model or cfg.get("vision_zai_model", "glm-4.6v-flash")
    return oai.vision_chat(
        ZAI_VISION_URL,
        api_key,
        model_id,
        prompt,
        jpeg_b64,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        extra_headers={"Accept-Language": "en-US,en"},
    )


def text_chat(
    messages: list,
    *,
    config: dict | None = None,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> tuple[str, int]:
    """Text-only Z.ai chat."""
    import arti_text_openai as text_oai

    cfg = config or {}
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("ZAI_API_KEY missing")

    model_id = model or cfg.get("scouter_zai_model", "glm-4.5-flash")
    return text_oai.text_chat(
        ZAI_VISION_URL,
        api_key,
        model_id,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        extra_headers={"Accept-Language": "en-US,en"},
    )
