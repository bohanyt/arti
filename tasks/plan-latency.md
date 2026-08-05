# Implementation Plan — Latency & Co-watch Pipeline

> Source: `.cursor/plans/latency_pipeline_refactor_d1f6fc2f.plan.md`
> Baseline tag: `v0.5.2-stable`
> Skills: read `arti-vtuber-guardrails` before every task
> Spec: `docs/SPEC-latency-cowatch.md`

## Task 1 — PipelineTimer instrumentation (Fase 1)

**ID:** `phase1-timer`

**Goal:** Measure per-stage latency on live triggers; log to terminal + JSONL transcript.

**Changes:**
- New `pipeline_timer.py`: `PipelineTimer`, ASR/TTS stage helpers, `format_latency_line`
- Wire `_handle_voice_trigger`: marks for mikir, RAG, LLM, TTS
- ASR path: `vad_tail_ms`, `asr_ms` on PTT/wake before `queue_voice_trigger`
- TTS: `tts_synth_ms` + `tts_play_ms` in `speak` / `_play_wav` / `_speak_edge_tts`
- Pass `latency_ms` + `stages` to `session_transcript.log_arti_reply`
- `tests/test_pipeline_timer.py`

**Acceptance:**
- [ ] `[Latency]` line printed per successful Arti reply
- [ ] `transcripts/*.jsonl` arti rows include `latency_ms` and `stages`
- [ ] Stage keys: `vad_tail_ms`, `asr_ms`, `vts_mikir_ms`, `rag_ms`, `llm_ms`, `tts_synth_ms`, `tts_play_ms`, `total_ms`
- [ ] `pytest tests/test_pipeline_timer.py` passes; full `pytest tests/` passes
- [ ] No idle/VTS lifecycle changes

**Risk:** None (diagnostic only)

---

## Task 2 — NVIDIA DiffusionGemma POC (Fase 0)

**ID:** `phase0-nvidia-poc`

**Goal:** Script + optional auxiliary client; benchmark vs Groq; not main LLM swap.

**Changes:**
- `scripts/test_diffusiongemma_nvidia.py`
- Optional `arti_nvidia_client.py` stub
- CONFIG keys default OFF

**Acceptance:**
- [ ] Script runs 5 ID prompts, prints `llm_ms`
- [ ] CONFIG defaults unchanged for `api_provider`

**Risk:** Low

---

## Task 3 — Desktop audio worker (Layer 2)

**ID:** `phase0b-desktop-audio`

**Goal:** WASAPI loopback ASR → `dialogue_ring` RAM; no auto-trigger.

**Changes:**
- New `arti_desktop_audio.py` + thread in bridge (CONFIG `desktop_audio_enabled: False`)

**Acceptance:**
- [ ] Guards: `tts_is_playing`, echo filter, no `queue_voice_trigger`
- [ ] Unit tests for ring buffer

**Risk:** Medium

---

## Task 4 — Screen watcher (Layer 1)

**ID:** `phase0c-screen-vision`

**Goal:** Screenshot + semantic + OCR timecode → `screen_ring` RAM.

**Changes:**
- New `arti_screen_context.py` (CONFIG `screen_context_enabled: False`)

**Acceptance:**
- [ ] No vault embed on screenshots
- [ ] Tests for ring buffer + prompt shape

**Risk:** Medium

---

## Task 5 — Timecode RAG (Layer 4 prep)

**ID:** `phase0c-rag-timecode`

**Goal:** `search_by_timecode` + scoped watch-parties FTS.

**Changes:**
- Extend `arti_vault_rag.py`

**Acceptance:**
- [ ] Unit tests for timecode window parse
- [ ] General RAG skippable when watch party active

**Risk:** Medium

---

## Task 6 — Watch Party wire (Layer 4)

**ID:** `phase0d-watch-party`

**Goal:** Pre-fed episode + OCR playback + insight on pause/PTT.

**Changes:**
- `watch_state`, CONFIG `watch_party_enabled: False`
- Wire to `_handle_voice_trigger` when enabled

**Acceptance:**
- [ ] Sample `vault/watch-parties/` chunk format
- [ ] CONFIG default False

**Risk:** Medium–High

---

## Task 7 — Async I/O quick wins (Fase 2a)

**ID:** `phase2-async-io`

**Goal:** `asyncio.to_thread` for blocking LLM HTTP + `sd.wait`; fire-and-forget mikir.

**Acceptance:**
- [ ] Event loop not blocked during LLM/TTS
- [ ] **HIGH-RISK PAUSE** if changing `trigger_expression_state` await behavior

**Risk:** Medium

---

## Task 8 — Parallel RAG + history (Fase 2b)

**ID:** `phase2-parallel-rag`

**Goal:** `asyncio.gather` history + RAG before LLM.

**Risk:** Low–Medium

---

## Task 9 — HTTP cache + CONFIG tuning (Fase 2c–2e)

**ID:** `phase2-http-cache`

**Goal:** `requests.Session`, `asr_silence_tail_sec`, RAG embed LRU cache.

**Risk:** Low

---

## Task 10 — Pipeline module extract (Fase 3)

**ID:** `phase3-module`

**Goal:** `arti_voice_pipeline.py` with `TurnContext` / `run_turn`.

**Risk:** Medium (large bridge touch — split across tasks)

---

## Task 11 — Streaming LLM → chunked TTS (Fase 4)

**ID:** `phase4-streaming`

**Goal:** Groq stream + sentence-chunked TTS queue; non-stream fallback.

**Risk:** Medium–High

---

## Dependency order

```
phase1-timer → phase0-nvidia-poc (parallel OK)
phase1-timer → phase0b → phase0c-screen → phase0c-rag → phase0d
phase1-timer → phase2-async-io → phase2-parallel-rag → phase2-http-cache
phase2-* → phase3-module → phase4-streaming
```

Emotion plan (`tasks/plan-emotion.md`) is independent; run `trigger-false-positive-berarti` anytime (low risk).

## Out of scope

- Disabling RAG live, ASR pasif, or summarizer
- CONFIG defaults ON for new features without smoke test
- Idle thread changes unless explicit task
