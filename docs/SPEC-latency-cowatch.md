# SPEC — Latency & Co-watch

> Full design: `.cursor/plans/latency_pipeline_refactor_d1f6fc2f.plan.md`
> Build tasks: `tasks/plan.md`

## Problem

PTT → Arti voice feels **5–15s**. Pipeline is fully sequential. `session_transcript.log_arti_reply` already supports `latency_ms` and `stages` but the live path does not populate them.

## Fase 1 — Instrumentation (current)

| Stage key | Source |
|-----------|--------|
| `vad_tail_ms` | ASR silence tail before transcribe (PTT/wake) |
| `asr_ms` | `transcribe_audio` wall time |
| `vts_mikir_ms` | `trigger_expression_state("mikir")` |
| `rag_ms` | `arti_vault_rag.append_rag_to_system` |
| `llm_ms` | `do_api_call` |
| `tts_synth_ms` | Supertone/edge synthesis |
| `tts_play_ms` | `sd.play` + `sd.wait` |
| `total_ms` | Turn start → TTS end |

Terminal: `[Latency] asr=… rag=… llm=… tts=… total=…`

## Later phases (summary)

- **Fase 2:** Non-blocking HTTP/TTS, parallel prep, Session reuse, RAG embed cache
- **Fase 3:** `arti_voice_pipeline.py` module extract
- **Fase 4:** Groq streaming + sentence TTS queue
- **Layers 1–4:** Screen, desktop audio, co-watch curious, watch party + timecode RAG

## Guardrails

- CONFIG defaults **OFF** for new features
- No idle `pause`/`resume` regression
- NVIDIA DiffusionGemma = auxiliary only; Groq remains main LLM
