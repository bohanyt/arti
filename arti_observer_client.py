"""Observer LLM client — per-segment summarize (no Groq)."""

from __future__ import annotations

import json
import re
from typing import Any

import arti_scouter_client as scouter

OBSERVER_JSON_SCHEMA = """{
  "summary": "2-3 kalimat ID ringkas segmen",
  "topics": ["topik"],
  "facts": [{"text": "...", "confidence": 0.8}],
  "worth_embed": true,
  "worth_learning": false,
  "noise_level": "low|high"
}"""


def build_observer_prompt(segment_text: str) -> str:
    return (
        "Kamu Observer — ringkas segmen live stream Arti (10 menit) untuk vault.\n"
        "Output HANYA JSON valid:\n"
        f"{OBSERVER_JSON_SCHEMA}\n\n"
        "Segmen transcript:\n"
        f"{segment_text[:12000]}"
    )


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {"summary": text[:500], "noise_level": "low"}


def summarize_segment(segment_text: str, config: dict) -> dict[str, Any]:
    """Run text chain on segment; returns parsed observer JSON + provider."""
    if not segment_text.strip():
        return {"summary": "", "noise_level": "high"}

    prompt = build_observer_prompt(segment_text)
    chain_key = "observer_provider_chain"
    chain = list(config.get(chain_key) or config.get("scouter_provider_chain") or scouter.DEFAULT_CHAIN)
    cfg = {**config, "scouter_provider_chain": chain}

    last_err = ""
    for name in chain:
        if name.lower() == "groq":
            continue
        fn = scouter._PROVIDERS.get(name)
        if not fn:
            continue
        try:
            raw, ms = fn(prompt, cfg)
            data = _parse_json(raw)
            data["provider"] = name
            try:
                import arti_api_telemetry as tel

                tel.record_call(
                    subsystem="observer",
                    provider=name,
                    model=str(config.get(f"scouter_{name}_model") or name),
                    latency_ms=ms,
                    ok=bool(data.get("summary")),
                    config=config,
                )
            except Exception:
                pass
            return data
        except Exception as e:
            last_err = str(e)
            continue

    return {"summary": f"(observer gagal: {last_err[:80]})", "noise_level": "high", "provider": ""}
