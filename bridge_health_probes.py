"""Deep API model probes — vision + text per provider (health check)."""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from typing import Any

from PIL import Image

PROBE_VISION_PROMPT = (
    'Reply JSON only: {"scene":"test","playback_mmss":null,"ocr":""} '
    "Describe this tiny test image briefly."
)
PROBE_TEXT_PROMPT = 'Reply JSON only: {"ping":true,"summary":"ok"}'


@dataclass
class DeepProbeRow:
    provider: str
    model: str
    lane: str  # vision | text
    status: str
    detail: str
    latency_ms: int = 0


def tiny_jpeg_b64(size: int = 64) -> str:
    """Minimal JPEG for vision probes (no mss)."""
    img = Image.new("RGB", (size, size), color=(200, 40, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _has_key(config: dict, provider: str) -> bool:
    p = provider.lower()
    if p == "nvidia":
        return bool((config.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY") or "").strip())
    if p in ("google_gemma", "google_gemini", "google_gemini_lite"):
        k = (config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
        return bool(k and k != "YOUR_GEMINI_API_KEY")
    if p == "cloudflare":
        return bool(
            (config.get("cloudflare_api_token") or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
            and (config.get("cloudflare_account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        )
    if p == "openrouter":
        return bool((config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip())
    if p == "groq":
        k = (config.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
        return bool(k and k.startswith("gsk_"))
    if p == "github":
        if not config.get("vision_github_enabled", False):
            return False
        return bool((config.get("github_models_token") or os.environ.get("GITHUB_TOKEN") or "").strip())
    if p == "zai":
        return bool(
            (config.get("zai_api_key") or os.environ.get("ZAI_API_KEY") or os.environ.get("ZHIPU_API_KEY") or "").strip()
        )
    if p == "ollama":
        return bool((config.get("ollama_api_key") or os.environ.get("OLLAMA_API_KEY") or "").strip())
    return False


def vision_models_for_provider(config: dict, provider: str) -> list[str]:
    """Up to 2 vision models per provider."""
    p = provider.lower()
    if p == "nvidia":
        m = config.get("vision_nvidia_model") or config.get("nvidia_model", "google/diffusiongemma-26b-a4b-it")
        return [m] if m else []
    if p == "google_gemma":
        primary = config.get("vision_google_gemma_model", "gemma-4-26b-a4b-it")
        fb = config.get("vision_google_gemma_fallback_model", "gemma-4-31b-it")
        out = [primary]
        if fb and fb != primary:
            out.append(fb)
        return out[:2]
    if p == "google_gemini_lite":
        m = config.get("vision_google_gemini_model", "gemini-3.1-flash-lite")
        return [m] if m else []
    if p == "cloudflare":
        m = config.get("vision_cloudflare_model", "@cf/google/gemma-4-26b-a4b-it")
        return [m] if m else []
    if p == "openrouter":
        m = config.get("vision_openrouter_model", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
        return [m] if m else []
    if p == "groq":
        m = config.get("vision_groq_model", "meta-llama/llama-4-scout-17b-16e-instruct")
        return [m] if m else []
    if p == "github":
        m = config.get("vision_github_model", "meta/llama-3.2-11b-vision-instruct")
        return [m] if m else []
    if p == "zai":
        m = config.get("vision_zai_model", "glm-4.6v-flash")
        return [m] if m else []
    if p == "ollama":
        m = config.get("vision_ollama_model", "gemma4:31b-cloud")
        return [m] if m else []
    return []


def text_models_for_provider(config: dict, provider: str) -> list[str]:
    """Up to 2 text models per provider (scouter/observer chain)."""
    p = provider.lower()
    if p == "nvidia":
        m = config.get("scouter_nvidia_model") or config.get("nvidia_model", "google/diffusiongemma-26b-a4b-it")
        return [m] if m else []
    if p in ("google_gemini", "google_gemini_lite"):
        m = config.get("scouter_gemini_model", "gemini-3.1-flash-lite")
        return [m] if m else []
    if p == "cloudflare":
        m = config.get("scouter_cloudflare_model", "@cf/google/gemma-4-26b-a4b-it")
        return [m] if m else []
    if p == "openrouter":
        models = list(config.get("scouter_openrouter_models") or [])[:2]
        if not models:
            models = [
                config.get("openrouter_summarizer_model", "poolside/laguna-xs.2:free"),
                config.get("openrouter_summarizer_fallback", "nvidia/nemotron-3-nano-30b-a3b:free"),
            ]
        return [m for m in models if m][:2]
    if p == "github":
        m = config.get("scouter_github_model", "meta/llama-3.2-3b-instruct")
        return [m] if m else []
    if p == "zai":
        m = config.get("scouter_zai_model", "glm-4.5-flash")
        return [m] if m else []
    if p == "ollama":
        m = config.get("scouter_ollama_model", "gemma4:31b-cloud")
        return [m] if m else []
    return []


def _telemetry_probe(row: DeepProbeRow) -> None:
    try:
        import arti_api_telemetry as tel

        tel.record_call(
            subsystem="health_probe",
            provider=row.provider,
            model=row.model,
            latency_ms=row.latency_ms,
            ok=row.status == "OK",
            extra={"lane": row.lane, "detail": row.detail[:120]},
        )
    except Exception:
        pass


def probe_vision_model(provider: str, model: str, config: dict, jpeg_b64: str | None = None) -> DeepProbeRow:
    """Live vision describe probe for one model."""
    import arti_cloudflare_vision
    import arti_gemini_vision
    import arti_github_vision
    import arti_nvidia_client
    import arti_ollama_vision
    import arti_vision_openai as oai
    import arti_zai_vision

    b64 = jpeg_b64 or tiny_jpeg_b64()
    p = provider.lower()
    cfg = {**config, "vision_max_tokens": 64, "vision_temperature": 0.1}
    try:
        if p == "nvidia":
            _, ms = arti_nvidia_client.vision_chat(
                PROBE_VISION_PROMPT, b64, config=cfg, model=model, max_tokens=64, temperature=0.1, timeout=45
            )
        elif p == "google_gemma":
            _, ms = arti_gemini_vision.vision_generate(
                PROBE_VISION_PROMPT, b64, config=cfg, model=model, max_tokens=64, temperature=0.1, timeout=45
            )
        elif p == "google_gemini_lite":
            _, ms = arti_gemini_vision.vision_generate(
                PROBE_VISION_PROMPT, b64, config=cfg, model=model, max_tokens=64, temperature=0.1, timeout=45
            )
        elif p == "cloudflare":
            _, ms = arti_cloudflare_vision.vision_chat(
                PROBE_VISION_PROMPT, b64, config=cfg, max_tokens=64, temperature=0.1, timeout=45
            )
        elif p == "openrouter":
            key = (cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
            _, ms = oai.vision_chat(
                "https://openrouter.ai/api/v1/chat/completions",
                key,
                model,
                PROBE_VISION_PROMPT,
                b64,
                max_tokens=64,
                temperature=0.1,
                timeout=45,
                extra_headers={"HTTP-Referer": "https://github.com/hermes-vtuber-host", "X-Title": "Arti Health"},
            )
        elif p == "groq":
            key = (cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
            _, ms = oai.vision_chat(
                "https://api.groq.com/openai/v1/chat/completions",
                key,
                model,
                PROBE_VISION_PROMPT,
                b64,
                max_tokens=64,
                temperature=0.1,
                timeout=45,
            )
        elif p == "github":
            _, ms = arti_github_vision.vision_chat(
                PROBE_VISION_PROMPT, b64, config=cfg, max_tokens=64, temperature=0.1, timeout=45
            )
        elif p == "zai":
            _, ms = arti_zai_vision.vision_chat(
                PROBE_VISION_PROMPT, b64, config=cfg, max_tokens=64, temperature=0.1, timeout=45
            )
        elif p == "ollama":
            _, ms = arti_ollama_vision.vision_chat(
                PROBE_VISION_PROMPT, b64, config=cfg, max_tokens=64, temperature=0.1, timeout=60
            )
        else:
            row = DeepProbeRow(provider, model, "vision", "SKIP", f"unknown provider {provider}")
            _telemetry_probe(row)
            return row
        row = DeepProbeRow(provider, model, "vision", "OK", "describe OK", ms)
        _telemetry_probe(row)
        return row
    except Exception as e:
        row = DeepProbeRow(provider, model, "vision", "FAIL", str(e)[:160], 0)
        _telemetry_probe(row)
        return row


def probe_text_model(provider: str, model: str, config: dict) -> DeepProbeRow:
    """Live text JSON probe for one model."""
    import arti_cloudflare_vision
    import arti_gemini_vision
    import arti_github_vision
    import arti_nvidia_client
    import arti_ollama_vision
    import arti_text_openai as text_oai
    import arti_zai_vision

    p = provider.lower()
    cfg = {**config, "scouter_max_tokens": 32, "scouter_temperature": 0.1, "scouter_timeout_sec": 45}
    msgs = [{"role": "user", "content": PROBE_TEXT_PROMPT}]
    try:
        if p == "nvidia":
            _, ms = arti_nvidia_client.chat_completion(
                msgs, config=cfg, model=model, max_tokens=32, temperature=0.1, timeout=45
            )
        elif p in ("google_gemini", "google_gemini_lite"):
            _, ms = arti_gemini_vision.text_generate(
                PROBE_TEXT_PROMPT, config=cfg, max_tokens=32, temperature=0.1, timeout=45
            )
        elif p == "cloudflare":
            _, ms = arti_cloudflare_vision.text_chat(
                msgs, config=cfg, max_tokens=32, temperature=0.1, timeout=45
            )
        elif p == "openrouter":
            key = (cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
            _, ms = text_oai.text_chat(
                "https://openrouter.ai/api/v1/chat/completions",
                key,
                model,
                msgs,
                max_tokens=32,
                temperature=0.1,
                timeout=45,
                extra_headers={"HTTP-Referer": "https://github.com/hermes-vtuber-host", "X-Title": "Arti Health"},
            )
        elif p == "github":
            _, ms = arti_github_vision.text_chat(
                msgs, config=cfg, max_tokens=32, temperature=0.1, timeout=45
            )
        elif p == "zai":
            _, ms = arti_zai_vision.text_chat(
                msgs, config=cfg, max_tokens=32, temperature=0.1, timeout=45
            )
        elif p == "ollama":
            _, ms = arti_ollama_vision.text_chat(
                msgs, config=cfg, max_tokens=32, temperature=0.1, timeout=60
            )
        else:
            row = DeepProbeRow(provider, model, "text", "SKIP", f"unknown provider {provider}")
            _telemetry_probe(row)
            return row
        row = DeepProbeRow(provider, model, "text", "OK", "text OK", ms)
        _telemetry_probe(row)
        return row
    except Exception as e:
        row = DeepProbeRow(provider, model, "text", "FAIL", str(e)[:160], 0)
        _telemetry_probe(row)
        return row


def probe_vision_providers_deep(config: dict) -> list[DeepProbeRow]:
    rows: list[DeepProbeRow] = []
    b64 = tiny_jpeg_b64()
    chain = list(config.get("vision_provider_chain") or [])
    for provider in chain:
        if not _has_key(config, provider):
            continue
        for model in vision_models_for_provider(config, provider):
            rows.append(probe_vision_model(provider, model, config, b64))
    return rows


def probe_text_providers_deep(config: dict, chain_key: str = "scouter_provider_chain") -> list[DeepProbeRow]:
    rows: list[DeepProbeRow] = []
    chain = list(config.get(chain_key) or config.get("observer_provider_chain") or [])
    for provider in chain:
        if provider.lower() == "groq":
            continue
        if not _has_key(config, provider):
            continue
        for model in text_models_for_provider(config, provider):
            rows.append(probe_text_model(provider, model, config))
    return rows


def print_deep_probe_table(rows: list[DeepProbeRow]) -> None:
    if not rows:
        print("  (no providers with keys — set env vars for deep probe)")
        return
    print(f"  {'Provider':<14} {'Model':<42} {'Lane':<6} {'Status':<5} {'ms':>6}  Detail")
    print("  " + "-" * 100)
    for r in rows:
        model_s = r.model[:40] + (".." if len(r.model) > 42 else "")
        print(f"  {r.provider:<14} {model_s:<42} {r.lane:<6} {r.status:<5} {r.latency_ms:>6}  {r.detail[:48]}")
