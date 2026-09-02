# Model & Provider Registry

This page documents the provider/model roles referenced by the public ARTI runtime. It is a **source snapshot**, not a promise that a third-party endpoint is still available today. Provider catalogs, free tiers, rate limits, and model slugs can change independently of this repository.

## Source of truth

Runtime defaults live in `hermes_vtuber_bridge.py` and provider-specific modules such as:

- `arti_openrouter.py`
- `arti_vision_client.py`
- `arti_scouter_client.py`
- `arti_ollama_vision.py`
- `arti_cursor_agent.py`

`config_local.json` can override supported defaults without modifying tracked source.

## Conversational roles

The bridge separates latency-sensitive live turns from heavier/background work.

| Role | Typical default family | Notes |
|---|---|---|
| live / fast | Groq-hosted fast model | Used for latency-sensitive voice/chat fallbacks |
| live / strong | stronger Groq model | Selected for more complex live questions |
| OpenRouter fallback | configured free/available endpoint | Availability can change; the runtime rolls through configured fallbacks |
| local/cloud fallback | Ollama-compatible endpoint | Optional; configured by environment/local config |
| Cursor agent | Composer-family model | Optional and disabled unless explicitly configured |
| Codex/other agent adapters | configured external client | Optional and disabled unless explicitly configured |

The exact model slugs are intentionally kept in code/config instead of duplicated here so documentation cannot silently drift from runtime behavior.

## Vision roles

`arti_vision_client.py` owns the vision provider chain. The public snapshot may include adapters for Gemini, OpenRouter, Ollama-compatible endpoints, Cloudflare, NVIDIA, Z.ai, Groq, or other providers. A provider being implemented does **not** mean it is enabled by default or guaranteed to have a working free endpoint.

See `VISION-APIS.md` for configuration and smoke-test guidance.

## Model retirement behavior

External providers regularly retire or rename models. ARTI therefore treats provider/model failure as a routing problem rather than assuming one permanent model:

1. provider-specific errors are normalized;
2. known unavailable/decommissioned models are skipped where supported;
3. the configured chain advances to the next candidate;
4. optional providers can be disabled locally without editing source.

When a model disappears, prefer updating the configured model slug or chain and verifying it with the relevant smoke test. Do not encode provider marketing claims or temporary free-tier limits as permanent invariants.

## Configuration hygiene

Keep secrets in `.env`, never in tracked config. Start from `.env.example` and `config_local.json.example`.

For a public bug report, include the provider name, model slug, normalized error/status, and whether the failure reproduces with a minimal request. Do not paste API keys, full private prompts, session logs, viewer data, or local absolute paths.
