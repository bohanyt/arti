# OpenRouter model selection

This page explains how to choose and maintain OpenRouter models for ARTI without treating a dated provider snapshot as permanent truth.

**Status:** the checked-out runtime configuration is the source of truth for exact model slugs. OpenRouter models can be renamed, retired, rate-limited, or moved between tiers independently of this repository.

For the cross-provider view, read [`MODEL-REGISTRY.md`](MODEL-REGISTRY.md).

## Runtime roles

OpenRouter is used as a fallback/background provider, not as a single universal model for every path. The public bridge currently exposes role-specific keys including:

| Runtime role | Configuration keys |
|---|---|
| live fallback | `openrouter_live_model`, `openrouter_live_last_resort` |
| compact summarizer | `openrouter_summarizer_model`, `openrouter_summarizer_fallback` |
| reflection | `openrouter_reflection_model`, `openrouter_reflection_fallback_model`, `openrouter_reflection_last_resort` |
| optional heavier reflection | `openrouter_reflection_ultra_model`, gated by `reflection_try_ultra` |

Inspect the `CONFIG` block in [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) and the routing code in [`arti_openrouter.py`](../arti_openrouter.py) for the exact values in your revision.

The main live conversational provider path is configured separately. OpenRouter should be understood as one fallback/background layer in the broader provider registry, not the primary provider by definition.

## Match models to the path budget

A model that works with a generous output budget can still be unsuitable for a latency-sensitive path.

ARTI's live reply policy uses small/adaptive output budgets based on the requested reply length, while background summarization and reflection use different budgets. The exact live mapping is defined in [`arti_reply_policy.py`](../arti_reply_policy.py); Scouter and reflection limits are defined by their corresponding runtime configuration/code.

When evaluating an OpenRouter candidate, test it on the actual path budget and verify that it:

1. returns non-empty user-facing content;
2. finishes within the path's timeout and output budget;
3. does not leak hidden reasoning or provider-side metadata into the reply;
4. fails in a way that allows the configured fallback chain to continue.

Do not promote a model to the live fallback just because it succeeds with a much larger test budget.

## Model and slug changes

Do not maintain a long-lived "dead model" table in public documentation. Provider catalogs age faster than this repository.

When replacing a model:

1. confirm the current model identifier from the provider;
2. update the local/configured role rather than scattering the slug across docs;
3. exercise the real runtime path with its normal token and timeout limits;
4. verify fallback behavior by making the first candidate unavailable;
5. keep evidence language scoped to the revision and path you actually tested.

If a provider model disappears, the correct response is to advance or update the configured chain—not to assume an old benchmark or free-tier status is still valid.

## Configuration

Set the OpenRouter credential through `.env` using the placeholder documented in [`.env.example`](../.env.example). Keep account-specific settings and model overrides in local configuration rather than committing credentials.

For provider-independent routing behavior, return to [`MODEL-REGISTRY.md`](MODEL-REGISTRY.md).
