"""Scouter provider chain — semantic digest of streamer speech + YT chat (no Groq)."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import arti_cloudflare_vision
import arti_gemini_vision
import arti_github_vision
import arti_nvidia_client
import arti_ollama_vision
import arti_openrouter
import arti_text_openai as text_oai
import arti_zai_vision
from arti_vision_openai import is_rate_limit_error

_scouter_lock = threading.Lock()
_last_uptime_log_ts = 0.0

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_CHAIN = [
    "nvidia",
    "cloudflare",
    "openrouter",
    "google_gemini",
    "github",
    "zai",
    "ollama",
]

SCREEN_KEYWORDS = re.compile(
    r"\b(layar|screen|tampilan|lihat|liat|boss|scene|visual|gambar|video|"
    r"apa yang (terlihat|keliatan)|di layar|on screen)\b",
    re.IGNORECASE,
)

SCOUTER_JSON_SCHEMA = """{
  "summary": "1-2 kalimat ID — omongan streamer + chat",
  "emotion": "senang|sedih|marah|bingung|excited|neutral",
  "topic": "1-3 kata",
  "important_facts": ["fakta singkat"],
  "screen_relevant": false,
  "screen_hint": null,
  "curious_worthy": false,
  "curious_hook": null
}"""


@dataclass
class ScouterUptime:
    last_ok_ts: float = 0.0
    last_provider: str = ""
    consecutive_failures: int = 0
    total_ok: int = 0
    total_fail: int = 0
    chain_fallback_count: int = 0


@dataclass
class ScouterResult:
    summary: str = ""
    emotion: str = "neutral"
    topic: str = ""
    important_facts: list[str] = field(default_factory=list)
    screen_relevant: bool = False
    screen_hint: str | None = None
    curious_worthy: bool = False
    curious_hook: str | None = None
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "emotion": self.emotion,
            "topic": self.topic,
            "important_facts": list(self.important_facts),
            "screen_relevant": self.screen_relevant,
            "screen_hint": self.screen_hint,
            "curious_worthy": self.curious_worthy,
            "curious_hook": self.curious_hook,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, provider: str = "") -> ScouterResult:
        facts = data.get("important_facts") or []
        if not isinstance(facts, list):
            facts = [str(facts)]
        return cls(
            summary=str(data.get("summary") or "").strip(),
            emotion=str(data.get("emotion") or "neutral").strip() or "neutral",
            topic=str(data.get("topic") or "").strip(),
            important_facts=[str(f).strip() for f in facts if str(f).strip()],
            screen_relevant=_as_bool(data.get("screen_relevant")),
            screen_hint=_null_str(data.get("screen_hint")),
            curious_worthy=_as_bool(data.get("curious_worthy")),
            curious_hook=_null_str(data.get("curious_hook")),
            provider=provider,
        )


scouter_uptime = ScouterUptime()


def _as_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "ya")
    return bool(val)


def _null_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none", "n/a", "-"):
        return None
    return s


def _scouter_params(config: dict) -> tuple[int, float, float]:
    max_tokens = int(config.get("scouter_max_tokens", 350))
    temperature = float(config.get("scouter_temperature", 0.2))
    timeout = float(config.get("scouter_timeout_sec", 45))
    return max_tokens, temperature, timeout


def has_screen_keywords(text: str) -> bool:
    """Cheap pre-gate for timer priority — not a substitute for LLM."""
    return bool(SCREEN_KEYWORDS.search(text or ""))


def build_scouter_prompt(context_text: str) -> str:
    return f"""Analisis percakapan live stream VTuber (15 kejadian terakhir):

{context_text}

Tugas:
- Ringkas omongan streamer DAN chat penonton.
- Deteksi apakah pembicaraan merujuk ke apa yang terlihat di layar (game, video, UI).
- curious_worthy = true hanya jika layar relevan DAN ada sudut komentar proaktif yang menarik.
- Fakta kanon: co-host debut (atur di CONFIG). Frasa "baru mulai live stream" di summary = sesi hari ini, bukan tanggal lahir karakter.

Output HANYA JSON:
{SCOUTER_JSON_SCHEMA}"""


def parse_scouter_response(raw: str) -> ScouterResult | None:
    data = _parse_json_blob(raw)
    if not data:
        return None
    result = ScouterResult.from_dict(data)
    if not result.summary:
        return None
    return result


def _parse_json_blob(text: str) -> dict | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


def _resolve_chain(config: dict) -> list[str]:
    chain = list(config.get("scouter_provider_chain") or DEFAULT_CHAIN)
    out: list[str] = []
    for name in chain:
        if name == "groq":
            continue
        if name == "nvidia" and not arti_nvidia_client.resolve_api_key(config):
            continue
        if name == "cloudflare":
            if not arti_cloudflare_vision.resolve_token(config):
                continue
            if not arti_cloudflare_vision.resolve_account_id(config):
                continue
        if name == "openrouter" and not (
            config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")
        ):
            continue
        if name == "google_gemini" and not arti_gemini_vision.resolve_api_key(config):
            continue
        if name == "github":
            if not config.get("vision_github_enabled", False):
                continue
            if not arti_github_vision.resolve_token(config):
                continue
        if name == "zai" and not arti_zai_vision.resolve_api_key(config):
            continue
        if name == "ollama" and not arti_ollama_vision.resolve_api_key(config):
            continue
        out.append(name)
    return out


def _record_success(provider: str) -> None:
    scouter_uptime.last_ok_ts = time.time()
    scouter_uptime.last_provider = provider
    scouter_uptime.consecutive_failures = 0
    scouter_uptime.total_ok += 1


def _record_failure() -> None:
    scouter_uptime.consecutive_failures += 1
    scouter_uptime.total_fail += 1


def _maybe_log_uptime(result: ScouterResult | None = None) -> None:
    global _last_uptime_log_ts
    now = time.time()
    if now - _last_uptime_log_ts < 60.0:
        return
    _last_uptime_log_ts = now
    u = scouter_uptime
    extra = ""
    if result:
        extra = (
            f" screen_relevant={result.screen_relevant}"
            f" curious_worthy={result.curious_worthy}"
        )
    print(
        f"[Scouter] uptime ok={u.total_ok} fail={u.total_fail} "
        f"provider={u.last_provider or '-'}{extra}"
    )


ProviderFn = Callable[[str, dict], tuple[str, int]]


def _messages(prompt: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": prompt}]


def _call_nvidia(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    model = config.get("scouter_nvidia_model") or config.get("nvidia_model")
    return arti_nvidia_client.chat_completion(
        _messages(prompt),
        config=config,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        telemetry_subsystem="scouter",
    )


def _call_cloudflare(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_cloudflare_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def _call_openrouter(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    key = (config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    models = list(config.get("scouter_openrouter_models") or [])
    if not models:
        models = [
            config.get("openrouter_summarizer_model", "poolside/laguna-xs.2:free"),
            config.get("openrouter_summarizer_fallback", "nvidia/nemotron-3-nano-30b-a3b:free"),
            "owl-alpha",
        ]
    last_err = ""
    for model in models:
        if not model:
            continue
        try:
            text, ms = text_oai.text_chat(
                OPENROUTER_URL,
                key,
                model,
                _messages(prompt),
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                extra_headers={
                    "HTTP-Referer": "https://github.com/hermes-vtuber-host",
                    "X-Title": "Arti Scouter",
                },
            )
            if text:
                return text, ms
        except Exception as e:
            last_err = str(e)
            if is_rate_limit_error(e):
                continue
            continue
    if last_err:
        raise RuntimeError(last_err)
    raise RuntimeError("OpenRouter scouter: all models failed")


def _call_google_gemini(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_gemini_vision.text_generate(
        prompt,
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        telemetry_subsystem="scouter",
    )


def _call_github(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_github_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def _call_zai(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_zai_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def _call_ollama(prompt: str, config: dict) -> tuple[str, int]:
    max_tokens, temperature, timeout = _scouter_params(config)
    return arti_ollama_vision.text_chat(
        _messages(prompt),
        config=config,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


_PROVIDERS: dict[str, ProviderFn] = {
    "nvidia": _call_nvidia,
    "cloudflare": _call_cloudflare,
    "openrouter": _call_openrouter,
    "google_gemini": _call_google_gemini,
    "github": _call_github,
    "zai": _call_zai,
    "ollama": _call_ollama,
}


def run_chain(context_text: str, config: dict) -> ScouterResult | None:
    """Run scouter chain on history text; returns parsed result or None."""
    if not context_text.strip():
        return None

    prompt = build_scouter_prompt(context_text)
    chain = _resolve_chain(config)
    if not chain:
        print("[Scouter] No providers available (check API keys).")
        return None

    last_err = ""
    acquired = _scouter_lock.acquire(blocking=False)
    if not acquired:
        _scouter_lock.acquire()
        acquired = True

    try:
        for idx, name in enumerate(chain):
            fn = _PROVIDERS.get(name)
            if not fn:
                continue
            if idx > 0:
                scouter_uptime.chain_fallback_count += 1
            try:
                print(f"[Scouter] Trying {name}...")
                raw, ms = fn(prompt, config)
                result = parse_scouter_response(raw)
                if result is None:
                    last_err = f"{name}: bad JSON"
                    print(f"[Scouter] {name} parse fail ({ms}ms)")
                    continue
                result.provider = name
                _record_success(name)
                _maybe_log_uptime(result)
                print(
                    f"[Scouter] OK {name} ({ms}ms) "
                    f"screen_relevant={result.screen_relevant} "
                    f"curious_worthy={result.curious_worthy}"
                )
                return result
            except Exception as e:
                last_err = f"{name}: {e}"
                print(f"[Scouter] {name} fail: {type(e).__name__}: {e}")
                if is_rate_limit_error(e):
                    continue
                continue

        _record_failure()
        _maybe_log_uptime()
        if last_err:
            print(f"[Scouter] All providers failed — last: {last_err}")
        return None
    finally:
        if acquired:
            _scouter_lock.release()


def run(context_text: str, config: dict) -> dict | None:
    """Backward-compat dict return for summarizer callers."""
    result = run_chain(context_text, config)
    return result.to_dict() if result else None
