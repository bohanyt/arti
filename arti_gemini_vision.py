"""Google AI Studio vision — Gemma 4 + Gemini Flash Lite via generateContent."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

import arti_gemini_budget as budget

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _generation_config(model_id: str, max_tokens: int, temperature: float) -> dict:
    config: dict[str, Any] = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
    }
    if model_id.startswith("gemma-4-"):
        # Gemma 4 default thinking menghabiskan budget JSON pendek sebelum
        # jawaban final keluar. Gemini API memakai level "minimal" untuk OFF.
        config["thinkingConfig"] = {"thinkingLevel": "minimal"}
    return config


def resolve_api_key(config: dict | None = None) -> str:
    cfg = config or {}
    return (cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()


def vision_generate(
    prompt: str,
    jpeg_b64: str,
    *,
    config: dict | None = None,
    model: str | None = None,
    max_tokens: int = 256,
    temperature: float = 0.2,
    timeout: float = 60.0,
    telemetry_subsystem: str = "vision",
) -> tuple[str, int]:
    cfg = config or {}
    api_key = resolve_api_key(cfg)
    if not api_key or api_key == "YOUR_GEMINI_API_KEY":
        raise ValueError("GEMINI_API_KEY missing")

    model_id = model or cfg.get("vision_google_gemma_model", "gemma-4-26b-a4b-it")

    # Pintu tunggal rem kuota ([date removed]): SEMUA pemanggil Gemini lewat
    # sini — scouter, vision, observer, video watcher — jadi rem di sini
    # menutup jalur yang melompati _resolve_chain (pelajaran GitHub Models).
    est = budget.perkiraan_token(prompt, max_tokens, ada_gambar=True)
    ok, alasan = budget.boleh(model_id, est, cfg)
    if not ok:
        raise budget.JatahPenuh(f"{model_id}: {alasan}")

    url = f"{GEMINI_API_BASE}/{model_id}:generateContent?key={api_key}"
    payload: dict[str, Any] = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": jpeg_b64}},
                ]
            }
        ],
        "generationConfig": _generation_config(model_id, max_tokens, temperature),
    }
    budget.catat_panggilan(model_id)
    t0 = time.perf_counter()
    res = requests.post(url, json=payload, timeout=timeout)
    ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code == 429:
        budget.kena_429(model_id, cfg)
        raise RuntimeError(f"HTTP 429: {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:400]}")
    body = res.json()
    parts = body["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts).strip()
    budget.catat_token(
        model_id,
        int((body.get("usageMetadata") or {}).get("totalTokenCount") or est),
    )
    try:
        import arti_api_telemetry as tel

        tel.record_gemini_response(
            subsystem=telemetry_subsystem,
            model=model_id,
            body=body,
            latency_ms=ms,
            config=cfg,
        )
    except Exception:
        pass
    return text, ms


def text_generate(
    prompt: str,
    *,
    config: dict | None = None,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.2,
    timeout: float = 60.0,
    telemetry_subsystem: str = "llm",
) -> tuple[str, int]:
    """Text-only generateContent (no image)."""
    cfg = config or {}
    api_key = resolve_api_key(cfg)
    if not api_key or api_key == "YOUR_GEMINI_API_KEY":
        raise ValueError("GEMINI_API_KEY missing")

    model_id = model or cfg.get("scouter_gemini_model") or cfg.get(
        "vision_google_gemini_model", "gemini-3.1-flash-lite"
    )

    # Rem kuota — lihat catatan di vision_generate.
    est = budget.perkiraan_token(prompt, max_tokens)
    ok, alasan = budget.boleh(model_id, est, cfg)
    if not ok:
        raise budget.JatahPenuh(f"{model_id}: {alasan}")

    url = f"{GEMINI_API_BASE}/{model_id}:generateContent?key={api_key}"
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": _generation_config(model_id, max_tokens, temperature),
    }
    budget.catat_panggilan(model_id)
    t0 = time.perf_counter()
    res = requests.post(url, json=payload, timeout=timeout)
    ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code == 429:
        budget.kena_429(model_id, cfg)
        raise RuntimeError(f"HTTP 429: {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:400]}")
    body = res.json()
    parts = body["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts).strip()
    budget.catat_token(
        model_id,
        int((body.get("usageMetadata") or {}).get("totalTokenCount") or est),
    )
    try:
        import arti_api_telemetry as tel

        tel.record_gemini_response(
            subsystem=telemetry_subsystem,
            model=model_id,
            body=body,
            latency_ms=ms,
            config=cfg,
        )
    except Exception:
        pass
    return text, ms
