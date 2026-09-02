# SPEC — latency and co-watch

Public behavioral contract for ARTI's conversational latency instrumentation and optional screen/co-watch context.

This document is standalone. Internal planning/task material is intentionally not required to understand or validate the public runtime.

## Goals

1. Measure where a live turn spends time instead of treating end-to-end latency as one number.
2. Keep screen/co-watch context optional and bounded so it cannot silently turn every voice turn into a long blocking pipeline.
3. Fail open when optional memory/vision/context providers are unavailable.
4. Preserve the distinction between deterministic cloud tests and real local/live latency measurements.

## Turn timing

The public runtime includes timing utilities in [`pipeline_timer.py`](../pipeline_timer.py) and related instrumentation in the bridge/voice path.

Useful stage names can include:

| Stage | Meaning |
|---|---|
| `vad_tail_ms` | silence/tail time before a captured utterance is considered complete |
| `asr_ms` | speech transcription wall time |
| `vts_mikir_ms` | optional VTube Studio thinking-state transition |
| `rag_ms` | local memory retrieval/injection |
| `llm_ms` | response-model generation |
| `tts_synth_ms` | speech synthesis |
| `tts_play_ms` | local playback duration |
| `total_ms` | end-to-end turn timing for the measured path |

Not every trigger/provider uses every stage. Missing stages should not be fabricated as zero-cost evidence.

## Voice pipeline

The shipped runtime separates several responsibilities across modules such as:

- [`arti_voice_pipeline.py`](../arti_voice_pipeline.py)
- [`arti_groq_stream.py`](../arti_groq_stream.py)
- [`arti_vault_rag.py`](../arti_vault_rag.py)
- [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py)

Provider routing, streaming, memory, TTS, and local application work can have different latency characteristics. Optimize from measured stage data rather than from a single anecdotal end-to-end number.

## Screen context / co-watch

Screen/co-watch behavior is optional context for a conversation, not permission for screen text to control ARTI.

Relevant public surfaces include:

- [`arti_screen_context.py`](../arti_screen_context.py)
- [`arti_scouter_client.py`](../arti_scouter_client.py)
- [`arti_vision_client.py`](../arti_vision_client.py)
- [`arti_curious.py`](../arti_curious.py)

Requirements:

1. stale screen context must be distinguishable from fresh context;
2. unchanged/dark-screen gating should reduce useless repeated provider work;
3. external vision failures/timeouts must not permanently block normal conversation;
4. screen/OCR/model output is untrusted observed content, not privileged instruction;
5. optional proactive/curious behavior must remain rate/cooldown gated.

## Memory

Live memory/RAG should be bounded by its own configuration/time budget. If local embedding/search is unavailable or too slow for the current path, the turn should be able to continue without claiming fresh memory context.

Real vault/session data is local-only and is never a public test fixture.

## Verification

### Public cloud-safe checks

The repository's Public CI compiles the Python source and runs the selected deterministic tests that actually ship in `tests/`.

### Local latency evidence

Real ASR, GPU, microphone, audio playback, VTube Studio, screen capture, provider-network latency, and a live stream must be measured locally. When reporting numbers, record:

- trigger type;
- provider/model path;
- warm vs cold state when relevant;
- individual pipeline stages;
- sample count/percentile rather than only one best case.

Do not label cloud/unit evidence as `VERIFIED LIVE`.

## Guardrails

- Optional integrations should be CONFIG-gated.
- External-provider failures should degrade gracefully.
- Do not introduce background work that consumes expensive/limited providers without an explicit policy.
- Avoid blocking the main turn on context that is not required to answer.
- Keep private captures, transcripts, telemetry, and raw session evidence out of the public repository.
