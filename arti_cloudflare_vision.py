"""Cloudflare Workers AI vision — OpenAI-compatible chat/completions."""

from __future__ import annotations

import os

import arti_vision_openai as oai


def resolve_token(config: dict | None = None) -> str:
    cfg = config or {}
    return (
        cfg.get("cloudflare_api_token")
        or os.environ.get("CLOUDFLARE_API_TOKEN")
        or ""
    ).strip()


def resolve_account_id(config: dict | None = None) -> str:
    cfg = config or {}
    return (
        cfg.get("cloudflare_account_id")
        or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
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
    timeout: float = 60.0,
) -> tuple[str, int]:
    cfg = config or {}
    token = resolve_token(cfg)
    account_id = resolve_account_id(cfg)
    if not token or not account_id:
        raise ValueError("CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID missing")

    model_id = model or cfg.get("vision_cloudflare_model", "@cf/google/gemma-4-26b-a4b-it")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
    return oai.vision_chat(
        url,
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
    timeout: float = 60.0,
) -> tuple[str, int]:
    """Text-only Workers AI chat/completions."""
    import arti_text_openai as text_oai

    cfg = config or {}
    token = resolve_token(cfg)
    account_id = resolve_account_id(cfg)
    if not token or not account_id:
        raise ValueError("CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID missing")

    model_id = model or cfg.get("scouter_cloudflare_model") or cfg.get(
        "vision_cloudflare_model", "@cf/google/gemma-4-26b-a4b-it"
    )
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
    return text_oai.text_chat(
        url,
        token,
        model_id,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
