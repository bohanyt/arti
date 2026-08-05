"""Observer LLM client — per-segment summarize (no Groq)."""

from __future__ import annotations

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
    # Parser keras milik scouter (2026-08-04): regex lama "{ pertama .. }
    # terakhir" pecah oleh dua objek / prosa ber-kurung, lalu fallback di
    # bawah memasukkan TEKS MENTAH sebagai summary (noise_level low) — sampah
    # yang lolos kurasi ke vault tanpa kelihatan gagal.
    data = scouter._parse_json_blob(text)
    if isinstance(data, dict):
        return data
    return {"summary": text[:500], "noise_level": "low"}


def summarize_segment(segment_text: str, config: dict) -> dict[str, Any]:
    """Run text chain on segment; returns parsed observer JSON + provider."""
    if not segment_text.strip():
        return {"summary": "", "noise_level": "high"}

    prompt = build_observer_prompt(segment_text)
    chain_key = "observer_provider_chain"
    chain = list(config.get(chain_key) or config.get("scouter_provider_chain") or scouter.DEFAULT_CHAIN)
    # Observer pakai role Cursor sendiri (grok-4.5/high) — scouter sudah turun
    # ke composer (revisi biaya 2026-08-03), kualitas kurasi tetap dijaga.
    # telemetry_subsystem mengalir ke SEMUA lapisan provider (audit ronde-3:
    # satu panggilan observer sempat tercatat DUA baris — di sini dan di
    # provider — dan jalur fallback nvidia/openrouter masih berlabel
    # "scouter"; kini provider yang mencatat, dengan label benar).
    cfg = {
        **config,
        "scouter_provider_chain": chain,
        "cursor_role": "observer",
        "telemetry_subsystem": "observer",
    }

    # Provider yang TIDAK punya telemetri internal — hanya untuk mereka
    # lapisan ini mencatat sendiri (yang lain mencatat di dalam, sekali).
    _mute_providers = {"cloudflare", "github"}

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
            if name.lower() in _mute_providers:
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
