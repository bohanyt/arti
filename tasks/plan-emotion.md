# Implementation Plan — Arti Emotion + Guardrails

> Source: emotion/nodding plan + live session learnings (2026-06-14)
> Baseline tag: `v0.5.2-stable`
> Skills: read `arti-vtuber-guardrails` before every task

## Task 0 — False trigger `berarti` (Fase 0)

**ID:** `trigger-false-positive-berarti`

**Goal:** Stop YouTube/wake-word from triggering on substring `berarti`.

**Changes:**
- Add `is_arti_wake_call(text) -> bool` + `WAKE_FALSE_POSITIVES` frozenset
- YouTube `process_message`: replace `"arti" in msg` with helper
- Wake word ASR: use same helper; remove duplicate loop ~L2533–2563
- Add `tests/test_arti_wake.py`

**Acceptance:**
- [ ] `"ya berarti belum bisa"` → `is_arti_wake_call` returns False
- [ ] `"eh arti halo"` → True
- [ ] `"halo arti!"` → True
- [ ] `pytest tests/test_arti_wake.py` passes
- [ ] No changes to idle thread

**Risk:** Low

---

## Task 1 — `arti_expression_runtime.py` module (Fase A)

**ID:** `expr-module`

**Goal:** Expression logic in separate module; bridge not touched yet except import stub.

**Changes:**
- New `arti_expression_runtime.py`: `EMOTION_MAP`, `parse_reply_emotion()`, stub `apply_turn_*` API
- `tests/test_expression_runtime.py` for parser only

**Acceptance:**
- [ ] `parse_reply_emotion("halo [EMOTION:senang]")` → `("halo", "senang")`
- [ ] Invalid/missing tag → `neutral`
- [ ] `pytest tests/test_expression_runtime.py` passes

**Risk:** Low

---

## Task 2 — Emotion prompt tag (Fase B)

**ID:** `expr-prompt-tag`

**Goal:** LLM outputs `[EMOTION:...]`; stripped before TTS.

**Changes:**
- System prompt addition (when `expression_emotion_enabled` — default `False`)
- Wire `parse_reply_emotion` in `_handle_voice_trigger` after `clean_ai_reply`
- CONFIG key `expression_emotion_enabled: False`

**Acceptance:**
- [ ] Tag stripped from TTS text
- [ ] CONFIG default False
- [ ] Unit test for strip path
- [ ] `pytest tests/` passes

**Risk:** Medium — touches bridge prompt path only, not idle

---

## Task 3 — Mood overlay + turn end (Fase C)

**ID:** `expr-overlay`

**Goal:** `apply_speaking(emotion)` overlays mood on `bicara`; `apply_turn_end` resets all.

**Changes:**
- Implement `apply_speaking`, `apply_turn_end` in `arti_expression_runtime.py`
- Wire in `_handle_voice_trigger` (CONFIG gated)

**Acceptance:**
- [ ] `senang` → `ArtiSenyum.exp3.json` overlay when enabled
- [ ] `default` turns off mood + bicara
- [ ] CONFIG default False
- [ ] **HIGH-RISK PAUSE** — expression lifecycle; user smoke test before enabling CONFIG

**Risk:** High — requires `doubt-driven-development` + manual VTS test

---

## Task 4 — Nod during TTS (Fase C nod)

**ID:** `expr-nod`

**Goal:** `FaceAngleY` pulse on main VTS websocket while `tts_is_playing`.

**Changes:**
- `run_nod_while_tts(vts, cancel_event)` in `arti_expression_runtime.py`
- CONFIG `expression_nod_enabled: False`, `expression_nod_amplitude`, `expression_nod_period_sec`

**Acceptance:**
- [ ] Nod task cancelled after TTS
- [ ] FaceAngleY reset to 0 on end
- [ ] CONFIG default False
- [ ] Does not run from idle thread

**Risk:** Medium

---

## Task 5 — Wire full turn flow (Fase D)

**ID:** `expr-wire-bridge`

**Goal:** `aware → mikir → bicara+mood → nod → default → start_idle`

**Changes:**
- `apply_turn_start` before mikir (stop_idle + aware where needed)
- Full wire in `_handle_voice_trigger` + hotkey paths
- Use `stop_idle_animation` / `start_idle_animation` only — **never** pause/resume idle

**Acceptance:**
- [ ] PTT flow uses new module when CONFIG enabled
- [ ] `start_idle_animation()` after successful TTS (not `resume_idle`)
- [ ] **HIGH-RISK PAUSE** — idle + expression; mandatory smoke test

**Risk:** High

---

## Task 6 — Smoke test checklist (Fase E)

**ID:** `expr-smoke-test`

**Goal:** Documented manual gate before CONFIG defaults flip to True.

**Changes:**
- Add `docs/SMOKE-TEST-emotion.md` checklist (7 items from plan)
- No code unless checklist reveals gaps

**Acceptance:**
- [ ] Checklist file exists with 7 items
- [ ] CONFIG remains False in committed CONFIG defaults

**Risk:** Low (docs only)

---

## Dependency order

```
Task 0 → Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6
```

Task 3 and 4 can run in parallel after Task 2 if needed.

## Out of scope (this plan)

- Co-watch / screen / desktop audio
- Latency PipelineTimer (separate plan)
- Enabling CONFIG defaults to True without user sign-off
