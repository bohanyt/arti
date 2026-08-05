# Changelog

## v0.6.2 (belum live-smoked)

Restore fitur yang hilang saat rollback Juni (sumber: checkpoint 2026-06-07 +
arsip 00ebcdd). Jalur idle/expression/VTS TIDAK disentuh.

- Anti-narrator/meta filter: `filter_meta_history_talk`, `is_narrator_reply`,
  `incharacter_fallback_reply` — jawaban "aku membaca 12 catatan..." tidak bocor ke TTS
- `post_process_response` baru: batas adaptif YT vs PTT, truncate-bukan-fallback
- Smart Groq routing per turn (`pick_groq_model` by kompleksitas; kill switch
  `smart_groq_routing=false`) di atas retry loop stabil v0.6.1
- Konteks ringkas: `get_compact_llm_context` + `get_viewer_scoped_context` + profil viewer
- YT chat queue FIFO prioritas + cooldown per viewer (`arti_voice_queue.py`) —
  **default OFF** (`voice_queue_enabled`) sampai lolos live smoke
- Curious guards (dedup hook, generic filter) + fast path (skip RAG, history pendek;
  kill switch `curious_fast_path_enabled`)
- Fix history-log leak: instruksi anti kutip-log/timestamp di prompt turn
- Genericized: `streamer_name` config-driven, model routing pakai model Groq hidup
- Suite: 247 passed, exit 0 (test_fase2 17/17 + test_voice_queue + test_curious_fast_path)

## v0.6.1

Basis: `hermes-vtuber-host` tag `v0.6.1-stable` (terverifikasi live), riwayat git baru yang bersih.

- Groq rolling models hidup: gpt-oss-120b/20b, qwen3.6-27b (drop qwen3-32b/scout yang 404)
- Retry on empty-content / 404 / 429 (bukan langsung Echo); TTS = `message.content` saja
- API key & data pribadi keluar dari kode: `.env` + `config_local.json` (gitignored)
- Fix infinite loop `collect_streaming_reply` saat kalimat berakhir `!`/`?` tanpa `. `
- Test suite jalan penuh: 230 passed (script diagnostik dipindah ke `scripts/diag_*.py`,
  guard/baseline stale disinkronkan ke perilaku sekarang)

### Belum di-port dari arsip

- CUDA Supertone (ONNX GPU) — tersedia di arsip `hermes-vtuber-host`, belum lolos smoke live

## v0.6.0

- Observer pipeline, telemetry dashboard, vision/scouter chains
