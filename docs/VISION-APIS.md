# Vision APIs — Arti v0.6

Multi-provider screenshot describe chain for `[LAYAR:]` inject and Curious.

## Chain order

1. **NVIDIA** — `google/diffusiongemma-26b-a4b-it` (`NVIDIA_API_KEY`)
2. **Google Gemma** — `gemma-4-26b-a4b-it` → fallback `gemma-4-31b-it` (`GEMINI_API_KEY`)
3. **Gemini Flash Lite** — `gemini-3.1-flash-lite`
4. **Cloudflare** — `@cf/google/gemma-4-26b-a4b-it` (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`)
5. **OpenRouter** — `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`
6. **Groq** — `meta-llama/llama-4-scout-17b-16e-instruct`
7. **GitHub** (optional) — `meta/llama-3.2-11b-vision-instruct` (`vision_github_enabled`)
8. **Z.ai** — `glm-4.6v-flash` (`ZAI_API_KEY`)
9. **Ollama Cloud** — `gemma4:31b-cloud` (`OLLAMA_API_KEY`)

## CONFIG (`arti_bridge.py`)

- `vision_enabled` — master switch (fitur ada di code)
- `vision_runtime_on_start` — awal stream: `false` = layar off sampai di-toggle
- `vision_hotkey_key` — default `mouse_x` (Mouse4); PTT biasanya `mouse_x2`
- `vision_background_poll` — `false` = tidak poll 10s; on-demand saat jawab + Curious
- `vision_refresh_sec` — interval poll kalau `vision_background_poll: true`
- `vision_stale_sec` — Curious requires snapshot newer than this
- `vision_provider_chain` — ordered provider ids
- `curious_enabled`, `curious_interval_sec` (75), `curious_cooldown_sec` (120)

## Health check

```bash
python bridge_health.py
```

With vision enabled, bridge startup prints **VISION PROVIDERS** key probes + mss capture test.

## Vault shutdown summary (separate from vision)

`session_transcript.summarize_session_for_vault()`:

Groq fast → OpenRouter Laguna M.1 / Nemotron Super → Gemini Flash Lite

## Scouter (live digest — separate chain)

Background semantic analysis of `stream_history` (not screenshots). Multi-provider text chain — **no Groq**.

See [SCOUTER.md](SCOUTER.md) for chain order, JSON fields, cadence, and auto-vision window.

| | Vision | Scouter |
|---|--------|---------|
| Input | JPEG | Chat/speech text |
| Groq in chain | Removed from default | Never |
| Drives `[LAYAR:]` | Yes (describe) | Indirect (opens auto-window) |

## Limits (approx)

| Provider | Notes |
|----------|-------|
| NVIDIA | ~40 RPM shared |
| Google Gemma/Gemini | 15 RPM, 500–1500 RPD |
| Cloudflare | 10K neurons/day |
| OpenRouter free | 20 RPM |
| Groq Scout | 30 RPM — shared with chat |
| GitHub | 150 RPD when enabled |
| Z.ai Flash | concurrency 1 |
| Ollama | GPU-time quota |
