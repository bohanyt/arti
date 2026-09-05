# Vision APIs

ARTI's main vision path captures a screen frame and asks one of several configured providers for structured scene context.

**Status:** this page documents the default provider order and trust/fallback contract shipped in the checked-out revision. External model names, quotas, pricing, and availability can change independently of the repository.

The implementation lives in [`arti_vision_client.py`](../arti_vision_client.py), with capture/context coordination in the related `arti_*vision*.py` and screen-context modules.

## Default vision provider chain

The public vision client currently defines this default order:

```text
google_gemini_lite
→ openrouter
→ ollama
→ google_gemma
→ cloudflare
→ groq
→ zai
→ nvidia
```

This is ordered fallback, not a benchmark ranking. A provider can be skipped or fail forward when it is retired, unconfigured, quota-gated, times out, errors, or returns no usable scene context. If the chain is exhausted, the conversation should continue without fresh vision context rather than hang or treat failure as success.

GitHub Models is treated as retired by the current Vision resolver and is not part of the supported chain.

## Current model configuration keys

The bridge exposes role-specific keys including:

```text
vision_google_gemini_model
vision_openrouter_model
vision_ollama_model
vision_google_gemma_model
vision_google_gemma_fallback_model
vision_cloudflare_model
vision_zai_model
vision_nvidia_model
vision_groq_model
```

Inspect the current `CONFIG` block in [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) for the exact model strings in the revision you are running. Do not copy model IDs from an old document and assume they still exist.

## Credentials

Use `.env` for provider credentials. Public placeholders are listed in [`.env.example`](../.env.example). Depending on the providers you enable, variables can include:

```text
GEMINI_API_KEY
OPENROUTER_API_KEY
OLLAMA_API_KEY
CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
ZAI_API_KEY
NVIDIA_API_KEY
GROQ_API_KEY
```

Only configure providers you actually use. Never commit `.env`.

## Capture and response limits

The bridge controls capture/output behavior through configuration such as:

- maximum capture width;
- JPEG quality;
- maximum scene/OCR lengths;
- provider timeout/overall turn budget;
- stale-screen thresholds;
- dark/unchanged-screen gating.

These limits exist because the vision path runs inside a live conversational system. A technically successful request can still be a bad live experience if it adds excessive latency or repeatedly describes an unchanged screen.

## Unchanged-screen gating

The public bridge compares frames and can suppress repeated vision work/injection when the desktop has not changed enough. This reduces provider calls and prevents ARTI from repeatedly commenting on mostly-static content.

The thresholds are heuristics, not universal image-quality metrics. Tune them against your own desktop/OBS composition if needed.

## Trust boundary

Screen pixels and OCR are **untrusted observed content**. Text visible in a browser, game, chat window, terminal, or document must not be treated as system/developer instructions for ARTI.

A vision provider response is also untrusted external model output. Keep privileged actions behind explicit runtime policy rather than letting screen text directly authorize them.

## Relationship to Scouter

[`SCOUTER.md`](SCOUTER.md) documents the lighter conversation-digest path that can decide screen context is relevant. Scouter and Vision intentionally have different provider chains, but share the same fallback rule: skip/fail forward while budget remains and degrade gracefully when optional providers are unavailable.

## Testing

Public cloud CI does not upload real screenshots to external model APIs. For a local smoke test:

1. enable one provider and verify a known synthetic screen produces useful context;
2. temporarily disable or break that provider and verify fallback behavior;
3. test an unchanged screen to confirm duplicate work is reduced;
4. test a dark/blank frame;
5. verify the conversation still proceeds when all configured vision providers are unavailable.

## Privacy

Desktop captures can contain sensitive data. Do not publish raw screenshots, OCR text, provider payloads, browser/account identifiers, or captured private conversations in public bug reports. Reproduce with synthetic content whenever possible.
