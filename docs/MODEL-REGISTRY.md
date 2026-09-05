# Model and provider registry

This page explains the provider/model roles referenced by the public ARTI runtime and how those roles should be maintained when third-party model catalogs change.

**Status:** this is a revision-scoped technical reference, not an evergreen provider catalog. Exact model slugs and enabled chains live in the checked-out source/configuration; external availability, pricing, quotas, and free tiers can change without a repository update.

## Source of truth

Runtime defaults live in [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) and provider-specific modules such as:

- [`arti_openrouter.py`](../arti_openrouter.py)
- [`arti_vision_client.py`](../arti_vision_client.py)
- [`arti_scouter_client.py`](../arti_scouter_client.py)
- [`arti_ollama_vision.py`](../arti_ollama_vision.py)
- [`arti_cursor_agent.py`](../arti_cursor_agent.py)

`config_local.json` can override supported defaults without modifying tracked source. Keep that file local.

## Conversational roles

The bridge separates latency-sensitive live turns from heavier/background work.

| Role | Runtime intent | Notes |
|---|---|---|
| live / fast | low-latency conversational path | Uses configured fast candidates and small/adaptive output budgets. |
| live / strong | more complex live questions | Uses the stronger configured live candidate when routing policy selects it. |
| OpenRouter live fallback | fallback after primary live providers fail | Must return usable content within the live token budget; see [`OPENROUTER_MODELS.md`](OPENROUTER_MODELS.md). |
| OpenRouter summarizer | compact background summaries | Uses its own configured model/fallback pair and token budget. |
| OpenRouter reflection | post-stream/background reflection | Uses a larger output budget than live turns and may use a different fallback chain. |
| local/cloud adapters | optional configured providers | Availability depends on local credentials/endpoints and explicit configuration. |
| external agent adapters | optional agent-backed paths | Disabled unless the corresponding local integration is explicitly configured. |

The exact model slugs are intentionally kept in code/config instead of duplicated here. That prevents a documentation table from silently becoming more authoritative than the runtime.

## Vision and Scouter roles

[`arti_vision_client.py`](../arti_vision_client.py) owns the main screenshot/vision chain. [`arti_scouter_client.py`](../arti_scouter_client.py) owns a separate compact digest chain that can mark screen context as relevant. They solve different latency/context tasks and therefore do not need identical provider order.

See [`VISION-APIS.md`](VISION-APIS.md) and [`SCOUTER.md`](SCOUTER.md) for the current shipped chains.

A provider adapter existing in the repository does **not** mean it is enabled or accepted by the current chain. Retired providers can remain as compatibility code while the resolver skips or rejects them.

## Model retirement behavior

External providers regularly retire or rename models. ARTI therefore treats model/provider failure as a routing problem rather than assuming one permanent model:

1. provider-specific failures are normalized where supported;
2. unavailable, retired, or locally disabled candidates are skipped;
3. the configured chain advances while its time/output budget remains;
4. if no optional provider succeeds, the caller should continue without pretending fallback succeeded.

When a model disappears, update the configured model slug or chain and verify it on the exact runtime path that uses it. Do not encode provider marketing claims or temporary free-tier limits as permanent invariants.

## Configuration hygiene

Keep secrets in `.env`, never in tracked config. Start from [`.env.example`](../.env.example) and [`config_local.json.example`](../config_local.json.example).

For a public bug report, include the provider name, model slug, normalized error/status, and whether the failure reproduces with a minimal request. Do not paste API keys, private prompts, session logs, viewer data, or local absolute paths.
