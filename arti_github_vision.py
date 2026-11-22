"""GitHub Models vision — tier 6 optional."""

from __future__ import annotations

import os

import arti_vision_openai as oai

GITHUB_VISION_URL = "https://models.github.ai/inference/chat/completions"


def resolve_token(config: dict | None = None) -> str:
    cfg = config or {}
    return (
        cfg.get("github_models_token")
        or cfg.get("github_token")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_MODELS_TOKEN")
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
    token = resolve_token(cfg)
    if not token:
        raise ValueError("GITHUB_TOKEN missing")

    model_id = model or cfg.get("vision_github_model", "meta/llama-3.2-11b-vision-instruct")
    return oai.vision_chat(
        GITHUB_VISION_URL,
        token,
        model_id,
        prompt,
        jpeg_b64,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
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
    """Text-only GitHub Models chat."""
    import arti_text_openai as text_oai

    cfg = config or {}
    token = resolve_token(cfg)
    if not token:
        raise ValueError("GITHUB_TOKEN missing")

    model_id = model or cfg.get("scouter_github_model") or cfg.get(
        "vision_github_model", "meta/llama-3.2-3b-instruct"
    )
    return text_oai.text_chat(
        GITHUB_VISION_URL,
        token,
        model_id,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
