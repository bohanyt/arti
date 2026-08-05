# SPEC — Arti Emotion + False Trigger Fix

Acceptance criteria for `arti-build-auto`. Baseline: `v0.5.2-stable`.

## Fase 0 — `berarti` false trigger

### Requirements

1. `is_arti_wake_call(text)` is the single source of truth for "did someone call Arti?"
2. Word-boundary match for `arti` / `eh arti` — not substring inside `berarti`, `artinya`, `mengartikan`, etc.
3. YouTube chat uses helper instead of `"arti" in msg`
4. Duplicate wake-word loop in bridge removed
5. Unit tests cover proven false positives from session `2026-06-14-default`

### Evidence

- `pytest tests/test_arti_wake.py` — all green
- Manual: chat message `berarti bang bohan ganteng` does not queue trigger

## Emotion system (CONFIG off by default)

### Requirements

1. `expression_emotion_enabled: False` in CONFIG
2. `expression_nod_enabled: False` in CONFIG
3. LLM may emit `[EMOTION:senang|sedih|marah|bingung|neutral]` — never spoken
4. Mood files overlay on `bicara`, not replace
5. Idle uses `stop_idle` / `start_idle` only

### Evidence

- Unit tests for `parse_reply_emotion`
- `pytest tests/` full suite green
- `docs/SMOKE-TEST-emotion.md` checklist before enabling CONFIG in production

## Non-goals

- Auto-enable features in CONFIG
- Refactor entire bridge in one task
- `pause_idle` / `resume_idle`
