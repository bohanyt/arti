"""Vision provider chain — screenshot describe with failover + uptime counters."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import arti_cloudflare_vision
import arti_gemini_vision
import arti_github_vision
import arti_nvidia_client
import arti_ollama_vision
import arti_screen_context as sc
import arti_vision_capture
import arti_vision_openai as oai
import arti_zai_vision

_vision_lock = threading.Lock()
_last_uptime_log_ts = 0.0

GROQ_VISION_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_VISION_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_CHAIN = [
    "nvidia",
    "google_gemma",
    "google_gemini_lite",
    "cloudflare",
    "openrouter",
    "groq",
    "github",
    "zai",
    "ollama",
]


@dataclass
class VisionUptime:
    last_ok_ts: float = 0.0
    last_provider: str = ""
    consecutive_failures: int = 0
    total_ok: int = 0
    total_fail: int = 0
    chain_fallback_count: int = 0


vision_uptime = VisionUptime()


def _vision_params(config: dict) -> tuple[int, float, int, int]:
    max_tokens = int(config.get("vision_max_tokens", 256))
    temperature = float(config.get("vision_temperature", 0.2))
    scene_max = int(config.get("vision_scene_max_chars", 300))
    ocr_max = int(config.get("vision_ocr_max_chars", 200))
    return max_tokens, temperature, scene_max, ocr_max


def _resolve_chain(config: dict) -> list[str]:
    chain = list(config.get("vision_provider_chain") or DEFAULT_CHAIN)
    out: list[str] = []
    for name in chain:
        if name == "github" and not config.get("vision_github_enabled", False):
            continue
        if name == "zai" and not (
            config.get("zai_api_key") or os.environ.get("ZAI_API_KEY") or os.environ.get("ZHIPU_API_KEY")
        ):
            continue
        if name == "ollama" and not (
            config.get("ollama_api_key") or os.environ.get("OLLAMA_API_KEY")
        ):
            continue
        out.append(name)
    return out or ["groq"]


def _record_success(provider: str) -> None:
    vision_uptime.last_ok_ts = time.time()
    vision_uptime.last_provider = provider
    vision_uptime.consecutive_failures = 0
    vision_uptime.total_ok += 1


def _record_failure() -> None:
    vision_uptime.consecutive_failures += 1
    vision_uptime.total_fail += 1


def _maybe_log_uptime() -> None:
    global _last_uptime_log_ts
    now = time.time()
    if now - _last_uptime_log_ts < 60.0:
        return
    _last_uptime_log_ts = now
    u = vision_uptime
    print(
        f"[Vision] uptime ok={u.total_ok} fail={u.total_fail} "
        f"provider={u.last_provider or '-'} streak_fail={u.consecutive_failures} "
        f"fallbacks={u.chain_fallback_count}"
    )
    if u.consecutive_failures >= 3:
        print("[Vision] WARN: 3+ consecutive failures — check API keys / quotas")


def _parse_raw(raw: str, config: dict) -> sc.ScreenSnapshot | None:
    max_tokens, _, scene_max, ocr_max = _vision_params(config)
    del max_tokens
    snap = sc.parse_vision_response(raw, scene_max_chars=scene_max, ocr_max_chars=ocr_max)
    if not snap.scene.strip():
        return None
    return snap


ProviderFn = Callable[[str, str, dict], tuple[str, int]]


def _call_nvidia(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    return arti_nvidia_client.vision_chat(
        prompt, jpeg_b64, config=config, max_tokens=max_tokens, temperature=temperature
    )


def _call_google_gemma(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    model = config.get("vision_google_gemma_model", "gemma-4-26b-a4b-it")
    try:
        return arti_gemini_vision.vision_generate(
            prompt, jpeg_b64, config=config, model=model, max_tokens=max_tokens, temperature=temperature
        )
    except Exception:
        fallback = config.get("vision_google_gemma_fallback_model", "gemma-4-31b-it")
        if fallback and fallback != model:
            return arti_gemini_vision.vision_generate(
                prompt,
                jpeg_b64,
                config=config,
                model=fallback,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        raise


def _call_google_gemini_lite(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    model = config.get("vision_google_gemini_model", "gemini-3.1-flash-lite")
    return arti_gemini_vision.vision_generate(
        prompt, jpeg_b64, config=config, model=model, max_tokens=max_tokens, temperature=temperature
    )


def _call_cloudflare(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    return arti_cloudflare_vision.vision_chat(
        prompt, jpeg_b64, config=config, max_tokens=max_tokens, temperature=temperature
    )


def _call_openrouter(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    key = (config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    model = config.get(
        "vision_openrouter_model", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    )
    return oai.vision_chat(
        OPENROUTER_VISION_URL,
        key,
        model,
        prompt,
        jpeg_b64,
        max_tokens=max_tokens,
        temperature=temperature,
        extra_headers={
            "HTTP-Referer": "https://github.com/hermes-vtuber-host",
            "X-Title": "Arti Vision",
        },
    )


def _call_groq(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    key = (config.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
    model = config.get("vision_groq_model", "meta-llama/llama-4-scout-17b-16e-instruct")
    return oai.vision_chat(
        GROQ_VISION_URL,
        key,
        model,
        prompt,
        jpeg_b64,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _call_github(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    return arti_github_vision.vision_chat(
        prompt, jpeg_b64, config=config, max_tokens=max_tokens, temperature=temperature
    )


def _call_zai(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    return arti_zai_vision.vision_chat(
        prompt, jpeg_b64, config=config, max_tokens=max_tokens, temperature=temperature
    )


def _call_ollama(prompt: str, jpeg_b64: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, _, _ = _vision_params(config)
    return arti_ollama_vision.vision_chat(
        prompt, jpeg_b64, config=config, max_tokens=max_tokens, temperature=temperature
    )


_PROVIDERS: dict[str, ProviderFn] = {
    "nvidia": _call_nvidia,
    "google_gemma": _call_google_gemma,
    "google_gemini_lite": _call_google_gemini_lite,
    "cloudflare": _call_cloudflare,
    "openrouter": _call_openrouter,
    "groq": _call_groq,
    "github": _call_github,
    "zai": _call_zai,
    "ollama": _call_ollama,
}


def describe_with_chain(
    config: dict,
    *,
    prompt: str | None = None,
    jpeg_b64: str | None = None,
) -> tuple[sc.ScreenSnapshot | None, str]:
    """Run vision chain; returns (snapshot, provider_name or '')."""
    if not config.get("vision_enabled", config.get("screen_context_enabled", False)):
        return None, ""
    runtime_on = config.get("vision_runtime_on", False)
    auto_until = float(config.get("vision_auto_until", 0))
    if not runtime_on and time.time() >= auto_until:
        return None, ""

    user_prompt = prompt or sc.build_vision_prompt()
    if jpeg_b64 is None:
        jpeg_b64, _ = arti_vision_capture.capture_jpeg_b64(config)

    chain = _resolve_chain(config)
    last_err = ""

    acquired = _vision_lock.acquire(blocking=False)
    if not acquired:
        latest = sc.screen_ring.latest()
        if latest:
            return latest, vision_uptime.last_provider or "cached"
        _vision_lock.acquire()
        acquired = True

    try:
        for idx, name in enumerate(chain):
            fn = _PROVIDERS.get(name)
            if not fn:
                continue
            if idx > 0:
                vision_uptime.chain_fallback_count += 1
            try:
                print(f"[Vision] Trying {name}...")
                raw, ms = fn(user_prompt, jpeg_b64, config)
                snap = _parse_raw(raw, config)
                if snap is None:
                    last_err = f"{name}: empty scene"
                    print(f"[Vision] {name} parse fail ({ms}ms)")
                    continue
                _record_success(name)
                _maybe_log_uptime()
                print(f"[Vision] OK {name} ({ms}ms)")
                return snap, name
            except Exception as e:
                last_err = f"{name}: {e}"
                print(f"[Vision] {name} fail: {type(e).__name__}: {e}")
                if oai.is_rate_limit_error(e):
                    continue
                continue

        _record_failure()
        _maybe_log_uptime()
        if last_err:
            print(f"[Vision] All providers failed — last: {last_err}")
        return None, ""
    finally:
        if acquired:
            _vision_lock.release()


def capture_and_describe(config: dict | None = None) -> tuple[sc.ScreenSnapshot | None, str]:
    """Entry for screen_watcher_worker — no args, uses CONFIG dict."""
    cfg = config or {}
    return describe_with_chain(cfg)


def make_watcher_fn(config: dict) -> Callable[[], tuple[sc.ScreenSnapshot | None, str]]:
    """Closure for screen_watcher_worker."""

    def _fn() -> tuple[sc.ScreenSnapshot | None, str]:
        return describe_with_chain(config)

    return _fn


def refresh_if_stale(config: dict) -> tuple[sc.ScreenSnapshot | None, str]:
    """On-demand describe when ring snapshot older than vision_stale_sec."""
    stale_sec = float(config.get("vision_stale_sec", 30))
    latest = sc.screen_ring.latest()
    if latest and (time.time() - latest.wall_ts) <= stale_sec:
        return latest, vision_uptime.last_provider or "cached"
    return describe_with_chain(config)


def is_vision_fresh(config: dict) -> bool:
    stale_sec = float(config.get("vision_stale_sec", 30))
    latest = sc.screen_ring.latest()
    if not latest or not latest.scene:
        return False
    return (time.time() - latest.wall_ts) <= stale_sec
