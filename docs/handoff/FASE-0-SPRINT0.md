# Fase 0 + Sprint 0 (SELESAI)

## Fase 0

- Vault sessions: 1 MD per hari (`vault/sessions/YYYY-MM-DD-default.md`)
- Import dari `hermes-vault/sessions.migrated`
- `docs/` untuk markdown plans; root hanya program
- `archive/folder-kosong/`, `archive/dev-cache/`

## Sprint 0 — Suara (user approved)

```python
"supertonic_voice": "F1",
"supertonic_speed": 1.1,
"supertonic_total_steps": 12,
```

A/B history: 1.05, 0.95, 1.1, 1.2, 1.6 — sweet spot **1.4 + 12**.

## TTS policy

- `tts_fallback_to_edge: False`
- `tts_robot_mode_on_failure: True` (pose default, idle off saat TTS gagal)
