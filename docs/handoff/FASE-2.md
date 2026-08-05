# Fase 2 — Reflection + viewer scope + smart Groq (SELESAI kode, perlu test live)

## Apa yang ditambahkan

| Fitur | File | Perilaku |
|--------|------|----------|
| Summarizer Laguna | `arti_openrouter.py` + `summarizer_worker` | Live ringkasan tiap 5 trigger: `poolside/laguna-xs.2:free` → `owl-alpha` |
| Post-stream reflection | `arti_openrouter.run_post_stream_reflection` | Shutdown: Nemotron Super → Laguna M.1 → owl; `vault/sessions/{id}_reflection.md` + append vault |
| Viewer-scoped prompt | `get_viewer_scoped_context()` | Trigger YT: history fokus viewer + profil dari `ARTI_VIEWERS.md` |
| Smart Groq | `pick_groq_model()` | Pendek → 8b-instant; medium → scout; tanya panjang → qwen; sangat kompleks → 70b |
| Groq → OpenRouter | `groq_chat_completion()` | **1x** Groq (smart pick) → langsung Laguna XS.2 → owl. Muter 4 Groq: `groq_roll_all_models_on_limit: True` |

**Module baru:** [`arti_openrouter.py`](../../arti_openrouter.py)

## CONFIG baru (di `CONFIG` bridge)

```python
"openrouter_summarizer_model": "poolside/laguna-xs.2:free",
"reflection_enabled": True,
"smart_groq_routing": True,
"groq_model_fast": "llama-3.1-8b-instant",
"groq_model_medium": "meta-llama/llama-4-scout-17b-16e-instruct",
"groq_model_strong": "qwen/qwen3-32b",
"groq_model_rare": "llama-3.3-70b-versatile",
```

Matikan routing pintar: `"smart_groq_routing": False` (kembali round-robin).

## Test otomatis (tanpa live stream)

```powershell
cd "C:\Users\<user>\Documents\hermes-vtuber-host"
.\venv\Scripts\Activate.ps1
python -m pytest tests/test_fase2.py -q
```

## Test live (user AFK — checklist saat kembali)

- [ ] Stream ~10 menit, 2x panggil viewer berbeda → log `[Groq API] (smart) llama-3.1-8b-instant` vs `qwen/...` untuk pertanyaan panjang
- [ ] Viewer A trigger → prompt tidak memuat chat viewer B (cek debug log history count / isi)
- [ ] Setelah 5 trigger → `[Summarizer] Trying poolside/laguna-xs.2:free`
- [ ] Ctrl+C → `vault/sessions/{session}_reflection.md` terbuat (butuh `OPENROUTER_API_KEY`)
- [ ] Vault session MD punya section `## Reflection` (link + cuplikan)

## Known / belum (Fase 3+)

- YT emotes di JSONL
- Subtitle/event loop bugs
- `reflection_try_ultra: True` opsional (lambat)

## Fase berikutnya

**Fase 3** — [FASE-3.md](FASE-3.md). Tes Laguna + filter narrator tetap di checklist Fase 2 saat kamu sempat.
