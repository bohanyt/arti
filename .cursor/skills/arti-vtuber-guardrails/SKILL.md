---
name: arti-vtuber-guardrails
description: >-
  Hard engineering guardrails for the Arti VTuber bridge (hermes-vtuber-host).
  Use before any change to hermes_vtuber_bridge.py, idle animation, VTS expressions,
  or live trigger paths. Enforces rollback tag, idle lifecycle, and minimal scope.
---

# Arti VTuber Guardrails

Read this skill **before** editing live bridge code. These rules come from production regressions (June 2026).

## Rollback baseline

| Tag | Commit role |
|-----|-------------|
| `v0.5.2-stable` | Last known-good idle + brain_busy before emotion/co-watch work |

Rollback commands:

```powershell
git reset --hard v0.5.2-stable          # full revert
git checkout v0.5.2-stable -- hermes_vtuber_bridge.py   # bridge only
```

Before risky tasks, verify: `git tag -l v0.5.2-stable`

## Idle animation — DO NOT REGRESS

| Allowed | Forbidden |
|---------|-----------|
| `stop_idle_animation()` at turn start | `pause_idle_animation()` |
| `start_idle_animation()` after TTS success | `resume_idle_animation()` |
| v0.5.1 stop/start thread pattern | Moving idle logic into `main_loop` inline |

**Do not edit** these unless the task explicitly targets idle:

- `idle_animation_worker`, `_idle_dual_track`, `_motion_track`, `_expression_track`
- `start_idle_animation`, `stop_idle_animation`

If idle must change: invoke `doubt-driven-development` skill and require live VTS smoke test.

## Expression lifecycle

States: `aware` → `mikir` → `bicara` (+ optional mood overlay) → `default` → `start_idle`.

- Mood expressions are **overlays on `bicara`**, not replacements.
- `default` must turn off `ArtiBicara`, mood files, and lamp.
- Never add `resume_idle` after TTS.

## Scope discipline

- **Minimal diff** — no drive-by refactors in `hermes_vtuber_bridge.py` (~3800 lines).
- New features go in **new modules** first (`arti_expression_runtime.py`, `arti_screen_context.py`, etc.).
- **CONFIG defaults OFF** for new features: `expression_emotion_enabled`, `expression_nod_enabled`, `co_watch_mode_enabled`, `screen_context_enabled`.
- Flip CONFIG to `True` only after unit tests + live smoke checklist pass.

## Trigger / false positives

- YouTube chat must **not** use `"arti" in msg` (matches `berarti`).
- Use word-boundary helper `is_arti_wake_call()` — see `tasks/plan.md` Fase 0.
- Proven false triggers from `2026-06-14-default` transcript: `"berarti dia gak bodoh"`, `"berarti bang bohan ganteng"`.

## Key files

| File | Role |
|------|------|
| `hermes_vtuber_bridge.py` | Orchestrator — touch sparingly |
| `arti_vault_rag.py` | RAG |
| `session_transcript.py` | Transcript JSONL |
| `templates/Arti*.exp3.json` | VTS expressions |
| `docs/Expression-Motion-System.md` | Animation reference |

## Verification before "done"

- [ ] `pytest tests/` passes
- [ ] No new `attached to a different loop` errors in idle (if idle touched)
- [ ] PTT trigger still works
- [ ] YT `"berarti ..."` does not trigger (if trigger path touched)
- [ ] Lamp returns to default after TTS (if expression path touched)
