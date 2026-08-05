# Smoke test — Screen + Curious (v0.6)

Manual checklist (~20 min) before live stream.

## Prerequisites

Env vars: `NVIDIA_API_KEY`, `GEMINI_API_KEY`, `CLOUDFLARE_*`, `OPENROUTER_API_KEY`, `GROQ_API_KEY` (voice only), optional `ZAI_API_KEY`, `OLLAMA_API_KEY`.

```bash
python bridge_health.py
python bridge_health.py --deep
```

Expect mic + Groq OK; deep probe shows vision + text model OK per provider with keys.

## 1. Vision on-demand (default)

1. `vision_enabled: true`, `vision_background_poll: false`, `vision_runtime_on_start: false`
2. Start bridge: `python hermes_vtuber_bridge.py`
3. Log: `[Vision] Background poll OFF — describe on-demand...`
4. **Mouse4** (`vision_hotkey_key: mouse_x`) → `👁️ [Vision ON]`
5. PTT or wait → `[Vision] Trying nvidia...` → `[Vision] OK ...`

## 2. PTT + [LAYAR:]

1. YouTube/game on primary monitor, Vision ON
2. PTT → jawaban inject `[LAYAR: ...]`

## 3. Failover

Unset `NVIDIA_API_KEY`, restart → next provider in chain.

## 4. Curious

Vision ON, idle ~90s → `[Curious] Proactive trigger queued`

## 5. Scouter auto-window

Vision OFF, 5× PTT or ~90s chat → `screen_relevant` opens vision ~60s

## 6. Observer shutdown

Ctrl+C → progress bar Observer → `vault/sessions/{id}_beats.jsonl` + `## API Usage` in session md

## Unit tests

```bash
python -m pytest tests/test_vision_client.py tests/test_curious.py tests/test_screen_context.py tests/test_scouter_bridge.py tests/test_observer_segment.py tests/test_health_probes.py tests/test_telemetry.py -q
```
