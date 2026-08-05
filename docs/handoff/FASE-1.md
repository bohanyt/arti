# Fase 1 — Logging Sprint (SELESAI implementasi, perlu test live)

## Apa yang ditambahkan

| Output | Path | Isi |
|--------|------|-----|
| Transcript | `transcripts/{session_id}.jsonl` | Semua chat + trigger + jawaban Arti + `latency_ms`, `groq_model` |
| Manifest | `data/session_manifest.json` | session_id, started/ended, trigger_count |
| Vault slim | `vault/sessions/{YYYY-MM-DD-default}.md` | Ringkasan Groq, metrik, link JSONL, error grep |
| Log rotation | `archive/v0.4/session_logs/` | Keep N terakhir di `session_logs/` |

**Module:** [`session_transcript.py`](../../session_transcript.py)

**Hooks:** `add_to_history`, `queue_voice_trigger`, shutdown `save_stream_session_log()` → `finalize_session_artifacts()`

## CONFIG baru

```python
"stream_session_id": "",       # kosong = auto YYYY-MM-DD_{profile}
"transcript_dir": "transcripts",
"session_log_keep_n": 5,
"transcript_flush_fsync": True,
```

## API keys

- Dipindah ke `.env` (lihat `.env.example`)
- Hardcoded keys dihapus dari `hermes_vtuber_bridge.py`

## Test checklist (BELUM diverifikasi agent — user harus tes)

- [ ] Start bridge ~2 menit, 1x trigger YT atau PTT
- [ ] Cek `transcripts/2026-MM-DD-default.jsonl` bertambah per chat
- [ ] Kill process (Task Manager) → baris terakhir JSONL tetap ada (flush/fsync)
- [ ] Ctrl+C → `data/session_manifest.json` punya `ended_at`
- [ ] Ctrl+C → `vault/sessions/2026-MM-DD-default.md` ringkasan Groq (bukan template 1 kalimat)
- [ ] Log `[Latency] llm=... tts=... model=...` per jawaban
- [ ] `session_logs/` max ~5 file, sisanya di archive
- [ ] Summarizer OpenRouter jalan jika key ada (setiap 5 trigger)

## Known / belum

- YT emotes di JSONL → Sprint 2 / Fase 3+
- Post-stream reflection, viewer-scoped, smart Groq → **Fase 2** (lihat [FASE-2.md](FASE-2.md))

## Fase berikutnya

**Fase 3** stabilitas. Lihat [HANDOFF.md](HANDOFF.md).
