# Vision APIs

ARTI's main vision path captures a screen frame and asks one of several configured providers for structured scene context. The current configuration lives in [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) and provider adapters under the `arti_*vision*.py` modules.

External model names, quotas, pricing, and availability can change independently of this repository. Treat the values below as the **current shipped defaults**, not an evergreen provider recommendation.

## Default vision provider chain

The public bridge currently ships this order:

```text
google_gemini_lite
→ openrouter
→ ollama
→ google_gemma
→ cloudflare
→ zai
→ nvidia
```

Groq has vision-capable configuration in the codebase but is **not** in the default chain. GitHub Models configuration also exists for optional/legacy use but is **not** in the default chain.

The chain is bounded by the bridge's vision-turn budget so a slow provider should not be allowed to block a live turn indefinitely.

## Current model configuration keys

The bridge currently exposes keys including:

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
vision_github_model
```

Inspect the current `CONFIG` block in [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) for the exact model strings in the revision you are running. Do not copy model IDs from an old README/blog post and assume they still exist.

## Credentials

Use `.env` for provider credentials. Public placeholders are listed in [`.env.example`](../.env.example). Depending on the paths you enable, variables can include:

```text
GEMINI_API_KEY
OPENROUTER_API_KEY
OLLAMA_API_KEY
CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
ZAI_API_KEY
NVIDIA_API_KEY
GITHUB_TOKEN
GROQ_API_KEY
```

Only configure the providers you actually use. Never commit `.env`.

## Capture and response limits

The bridge also controls capture/output behavior through configuration such as:

- maximum capture width;
- JPEG quality;
- maximum scene/OCR lengths;
- provider timeout/overall turn budget;
- stale-screen thresholds;
- dark/unchanged-screen gating.

These limits exist because the vision path runs in a live conversational system. A technically successful vision request can still be a bad live experience if it adds excessive latency or repeatedly describes an unchanged screen.

## Unchanged-screen gating

The public bridge compares frames and can suppress repeated vision work/injection when the desktop has not changed enough. This reduces provider calls and prevents ARTI from repeatedly commenting on its own mostly-static overlay.

The thresholds are heuristics, not universal image-quality metrics. Tune them against your own desktop/OBS composition if needed.

## Trust boundary

Screen pixels and OCR are **untrusted observed content**. Text visible in a browser, game, chat window, terminal, or document must not be treated as system/developer instructions for ARTI.

A vision provider response is also untrusted external model output. Keep privileged actions behind explicit runtime policy rather than letting screen text directly authorize them.

## Provider fallback expectations

A healthy local configuration should tolerate:

- missing credentials;
- HTTP errors/rate limits;
- provider timeouts;
- invalid/non-JSON responses;
- model retirement/rename;
- a provider that returns no useful scene context.

The chain should fail forward until its budget is exhausted, then let the conversation continue without fresh vision context rather than hanging indefinitely.

## Testing

Public cloud CI does not upload real screenshots to external model APIs. For a local smoke test:

1. enable one provider and verify a known screen produces useful context;
2. temporarily disable/break that provider and verify fallback behavior;
3. test an unchanged screen to confirm duplicate work is reduced;
4. test a dark/blank frame;
5. verify the conversation still proceeds when all vision providers are unavailable.

## Privacy

Desktop captures can contain extremely sensitive data. Do not publish raw screenshots, OCR text, provider payloads, browser/account identifiers, or captured private conversations in public bug reports. Reproduce with synthetic content whenever possible.
