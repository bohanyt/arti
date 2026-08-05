# AGENTS.md — Hermes VTuber Host (Arti)

Instructions for AI coding agents working in this repository.

## First read

1. **`.cursor/skills/arti-vtuber-guardrails/SKILL.md`** — hard rules (idle, rollback, scope)
2. **`tasks/plan.md`** — current implementation plan
3. **`tasks/todo.md`** — task status for build auto

## Stable rollback

```powershell
git reset --hard v0.6.4-stable
```

Tag `v0.6.4-stable` = titik yang **terverifikasi live** (2026-08-02 subuh, checklist penuh): Cursor backbone (composer voice + grok-4.5 high scouter + vision fallback), ekspresi + reconnect VTS, catch-up reindex, shutdown tunggu-tuntas + banner, blacklist bot, rant mode, fix vision budget 15 dtk.

Rollback lebih lama: `v0.6.3-stable` (2026-08-01, pra-fitur-cursor-backbone) dan `v0.6.2-stable` (2026-07-27, pra-Cursor/CUDA).

> **`git reset --hard` saja TIDAK cukup untuk kembali utuh.** Berkas berikut ada di `.gitignore`, jadi git tidak menyimpannya sama sekali: `.env`, `config_local.json`, `vts_token.txt`, `ARTI_SOUL.md`, `ARTI_VIEWERS.md`, `ARTI_MOOD_STATE.json`, `vault/`, `PROGRESS.md`, dan DB RAG di `data/`. Tanpa berkas ini Arti hidup tapi kehilangan kepribadian, path VTS, dan API key.
>
> Salinannya ada di `..\ARTI-backups\2026-08-02_v0.6.4-stable\` (yang lama: `2026-08-01_pasca-live-11jam`, `2026-08-01_v0.6.3-stable`, `2026-07-31_v0.6.2-stable`):
> ```powershell
> git reset --hard v0.6.4-stable
> copy "..\ARTI-backups\2026-08-02_v0.6.4-stable\untracked\*" .
> ```
> Salinan `.exp3.json` model VTS (folder di luar repo) ada di `..\ARTI-backups\2026-08-01_v0.6.3-stable\vts-model-expressions\` — belum berubah sejak itu.
> Kalau folder repo hilang total: `git clone <backup>\arti-full-history.bundle "ARTI v0.6.1"`.

**Tag lama tidak ikut pindah.** Repo ini hasil `git init` baru saat pindahan 2026-07-26, jadi `v0.6.1-stable`, `v0.6.0-stable`, `v0.5.8-stable`, `v0.5.6-stable`, dan `v0.5.2-stable` **tidak ada di sini** — semuanya tertinggal di repo arsip `hermes-vtuber-host`. Jangan pakai nama-nama itu di repo ini; perintahnya akan gagal.

## Build workflow

| Intent | Invoke |
|--------|--------|
| Implement all pending tasks | Skill `arti-build-auto` — say **"build auto"** after approving `tasks/plan.md` |
| Implement next task only | Say **"build"** or **"next task"** |
| Pick right process | Skill `using-agent-skills` |

Before **build auto**: working tree must be clean (or only `tasks/` / docs changes).

## Tests

```powershell
pytest tests/
```

Every code task: failing test first (RED), then implement (GREEN).

## Project layout

| Path | Purpose |
|------|---------|
| `hermes_vtuber_bridge.py` | Main orchestrator (~3800 lines) — minimal edits |
| `arti_vault_rag.py` | Hybrid RAG |
| `session_transcript.py` | JSONL transcripts |
| `tests/` | pytest suite |
| `.cursor/skills/` | Agent skills (addyosmani pack + Arti custom) |
| `vendor/agent-skills/` | Upstream clone (gitignored) |

## Active roadmap (high level)

1. **Fase 0** — false trigger `berarti` filter
2. **Emotion + nodding** — `arti_expression_runtime.py` (CONFIG off by default)
3. **Latency** — PipelineTimer, async, streaming TTS
4. **Co-watch** — screen + desktop audio → watch party

Do not skip Fase 0 before emotion work.

## Skills in `.cursor/skills/`

**Arti custom:** `arti-vtuber-guardrails`, `arti-build-auto`

**From [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills):** `using-agent-skills`, `planning-and-task-breakdown`, `incremental-implementation`, `test-driven-development`, `debugging-and-error-recovery`, `git-workflow-and-versioning`, `doubt-driven-development`, `observability-and-instrumentation`, `performance-optimization`, `api-and-interface-design`, `code-review-and-quality`, `code-simplification`

## License note

Skills copied from addyosmani/agent-skills are MIT licensed. See `vendor/agent-skills/LICENSE`.
