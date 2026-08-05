---
name: arti-build-auto
description: >-
  Cursor port of addyosmani /build and /build auto. Implements tasks from tasks/plan.md
  incrementally with TDD, one commit per task, and Arti guardrails. Use when the user
  says "build auto", "jalankan plan", "implement semua task", or "build" (next task only).
---

# Arti Build Auto (Cursor)

Port of [agent-skills build command](https://github.com/addyosmani/agent-skills/blob/main/.claude/commands/build.md) for Hermes VTuber Host.

**Always read** `arti-vtuber-guardrails` before implementing.

Also apply: `incremental-implementation`, `test-driven-development`, `git-workflow-and-versioning`, `debugging-and-error-recovery`.

## Modes

| User says | Mode |
|-----------|------|
| `build auto`, `jalankan plan`, `implement semua task` | **Autonomous** — all pending tasks after one approval |
| `build`, `next task`, `lanjut task` | **Single task** — one slice, then stop |

Autonomous mode removes human stepping **between** tasks, not verification per task.

## Prerequisites (autonomous)

1. **Spec/plan exists** at one of:
   - `tasks/plan.md` (primary)
   - `docs/SPEC-arti-emotion.md`
   - `docs/SPEC.md`
   If none exist, stop — run planning first.

2. **Clean baseline:**
   ```powershell
   git status --porcelain
   ```
   Stop if uncommitted changes exist outside: `tasks/`, `docs/SPEC*.md`, `.cursor/skills/`, `AGENTS.md`, `.gitignore`.
   Ask user to commit, stash, or confirm before continuing.

3. **`tasks/todo.md`** tracks task status. Update after each task.

## Autonomous flow (`build auto`)

1. Read `tasks/plan.md` and `tasks/todo.md`.
2. **Present full pending task list** — wait for explicit approval (`approve`, `go`, `yes`). Hedged answers are NOT approval.
3. For each pending task in dependency order:
   - Read acceptance criteria in plan/spec
   - **RED:** write failing test in `tests/`
   - **GREEN:** minimum implementation
   - Run `pytest tests/` (full suite)
   - Stage **only files this task touched** + `tasks/todo.md` — never blind `git add -A`
   - One atomic commit per task
   - Mark task done in `tasks/todo.md`
4. **STOP and ask user** (do not continue autonomous) when:
   - Test fails twice without obvious fix → `debugging-and-error-recovery`
   - Spec/plan ambiguous
   - **High-risk** (see below)
   - Touching idle/VTS without smoke test plan
5. End summary: tasks done, commits, tests added, blockers, skipped items.

## Single-task flow (`build`)

Same as steps 3–5 for **one** pending task only, then stop.

## High-risk — mandatory pause

Stop autonomous run and get explicit user OK before:

- Editing `idle_animation_worker`, `_motion_track`, `_expression_track`
- Changing `trigger_expression_state` lifecycle or post-TTS flow
- Refactoring >200 lines of `hermes_vtuber_bridge.py` in one task
- Deleting or renaming VTS expression files
- Changing default CONFIG to enable new live features

Use `doubt-driven-development` for these.

## Commit message format

```
<type>: <what> — task <id> from tasks/plan.md

<one sentence why>
```

Types: `fix`, `feat`, `test`, `refactor`, `chore`

## Rollback after autonomous run

```powershell
git log --oneline v0.5.2-stable..HEAD    # see each task commit
git revert <hash>                       # undo one task
git reset --hard v0.5.2-stable          # undo everything since stable tag
```

## Rationalizations (reject these)

| Excuse | Truth |
|--------|-------|
| "I'll add tests after" | RED first. No test, no commit. |
| "git add -A is faster" | Breaks per-task rollback. Stage explicitly. |
| "Small idle tweak is fine" | Idle regressions already happened. Pause or follow guardrails. |
| "CONFIG on by default is easier to test" | Defaults OFF; opt-in after smoke test. |
| "Seems right, skip pytest" | Verification is non-negotiable. |
