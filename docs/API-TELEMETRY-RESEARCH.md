# API Telemetry Research — Arti v0.6

Per-provider usage/cost fields for `arti_api_telemetry`.

| Provider | Subsystem | Usage in response | Cost in response | Free tier | Notes |
|----------|-----------|-------------------|------------------|-----------|-------|
| Groq | voice, ASR | `usage.prompt_tokens`, `completion_tokens` | No — use `data/api_cost_table.json` | RPM + daily cap | OpenAI-compatible |
| OpenRouter | scouter, vision, summarizer | `usage` + `native_tokens` | `usage.cost` reported | ~20 RPM free | Best $ accuracy |
| Google Gemini | vision-lite, scouter | `usageMetadata` | Billing console only | 15 RPM, 500–1500 RPD | Map model in cost table |
| NVIDIA NIM | vision, scouter | OpenAI `usage` when available | Credits / pay-per-token | ~40 RPM shared | Check NIM response |
| Cloudflare Workers AI | vision, text | Model-dependent | Neurons/day (~10K free) | Neuron quota | Not direct USD |
| GitHub Models | optional | OpenAI `usage` | 150 RPD free | Request count | |
| Z.ai GLM | vision, text | Varies | Flash tier | concurrency 1 | |
| Ollama Cloud | fallback | N/A | Subscription GPU time | Wall-clock | Estimate only |
| LM Studio | RAG embed | Local | $0 | RAM/CPU | Log `embed_ms` only |

## Dashboard APIs (cross-check)

- OpenRouter: `GET /api/v1/auth/key` (credits)
- Groq: console usage (no stable public API in bridge)
- Gemini: Google AI Studio quota UI

## Implementation

- Events: `data/telemetry/{session_id}.jsonl`
- Rollup: `arti_api_telemetry.session_summary()`
- Shutdown: `## API Usage` in vault session md
- CLI: `python bridge_health.py --telemetry`

## Cost rules

1. `cost_source=reported` — OpenRouter `usage.cost`
2. `cost_source=estimated` — tokens × `usd_per_1m_tokens` in cost table
3. `cost_source=free` — explicit free tier models
4. `cost_source=unknown` — token count only
