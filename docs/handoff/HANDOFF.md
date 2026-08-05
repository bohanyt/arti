# Handoff — Arti VTuber Bridge

Dokumen ini untuk **agent/session baru** setelah context window penuh. Baca file fase terakhir + checklist test.

## Status fase (Jun 2026)

| Fase | Status | Handoff detail |
|------|--------|----------------|
| **0** Workspace | DONE | Vault 1/hari, `docs/`, `archive/`, `folder-kosong/` |
| **Sprint 0** Suara | DONE | F1, `speed: 1.3`, `steps: 12` |
| **1** Logging | DONE | [FASE-1.md](FASE-1.md) |
| **2** Reflection + viewer scope + smart Groq | DONE (kode) | [FASE-2.md](FASE-2.md) — Laguna/filter tes later |
| **3** Stabilitas | DONE (kode) | [FASE-3.md](FASE-3.md) — tes live nanti |
| **4** Vault RAG | DONE (kode) | [FASE-4-RAG.md](FASE-4-RAG.md) — reindex + LM Studio |

## Konfigurasi penting

- API keys: file **`.env`** di root (gitignored) — `GROQ_API_KEY`, `OPENROUTER_API_KEY`
- Jangan commit `.env`. Keys pernah di chat → pertimbangkan rotate di dashboard Groq/OpenRouter.
- Supertone: `tts_fallback_to_edge: False`, `tts_robot_mode_on_failure: True`
- Live LLM: `api_provider: groq`

## Jalankan

```powershell
cd "C:\Users\<user>\Documents\hermes-vtuber-host"
.\venv\Scripts\Activate.ps1
python hermes_vtuber_bridge.py
```

## Struktur folder (singkat)

- Root: program + `ARTI_*.md` + `vault/`
- `docs/` — plans, IMPLEMENTATION_PLAN, handoff
- `transcripts/` — JSONL per sesi (gitignored)
- `data/session_manifest.json` — manifest aktif (gitignored)
- `session_logs/` — debug tee (gitignored)

## Plan induk

- Cursor: `arti_next_steps_993f9489.plan.md`
- Repo: `docs/IMPLEMENTATION_PLAN.md`
