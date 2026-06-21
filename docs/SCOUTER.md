# Scouter — Arti v0.6

Semantic digest of streamer speech + YT chat. Drives mood, context inject, auto-vision window, and Curious hooks.

**Groq policy:** Scouter does **not** use Groq. Groq is reserved for Arti voice (PTT / YT / Curious response only).

## Chain order (`scouter_provider_chain`)

1. **NVIDIA** — DiffusionGemma via NIM (`NVIDIA_API_KEY`)
2. **Cloudflare** — Workers AI text (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`)
3. **OpenRouter** — Laguna XS → Nemotron nano → owl (`OPENROUTER_API_KEY`)
4. **Google Gemini** — Flash Lite text (`GEMINI_API_KEY`)
5. **GitHub** (optional) — `vision_github_enabled` + `GITHUB_TOKEN`
6. **Z.ai** — GLM-4-Flash text (`ZAI_API_KEY`)
7. **Ollama Cloud** — last resort (`OLLAMA_API_KEY`)

## JSON output

| Field | Purpose |
|-------|---------|
| `summary` | 1–2 kalimat ringkas chat + streamer |
| `emotion` | Mood overlay → `set_mood()` |
| `topic` | Inject `[RINGKASAN KONTEKS TERAKHIR]` |
| `important_facts` | Long-term memory jika panjang |
| `screen_relevant` | Buka auto-vision ~60s |
| `screen_hint` | Inject `[LAYAR RELEVAN: …]` |
| `curious_worthy` | Gate Curious saat auto-window |
| `curious_hook` | Sudut komentar proaktif |

## Cadence

| Trigger | CONFIG | Default |
|---------|--------|---------|
| Setiap N jawaban Arti | `scouter_every_n_triggers` | 5 |
| Timer jika history baru | `scouter_interval_sec` | 90 |
| Min gap antar LLM call | `scouter_min_gap_sec` | 30 |
| Durasi auto-vision | `scouter_auto_vision_sec` | 60 |

Keyword pre-gate (`layar`, `screen`, `lihat`, …) mempercepat timer — tetap panggil LLM chain.

## Vision vs Scouter

| | Vision | Scouter |
|---|--------|---------|
| Input | Screenshot JPEG | 15 baris `stream_history` |
| Output | `scene`, `playback_mmss`, `ocr` | JSON digest |
| Groq | Tidak di chain default | Tidak pernah |
| Toggle | Mouse4 manual + scouter auto-window | Always on (`scouter_enabled`) |

Manual vision (`vision_hotkey_key`) dan scouter auto-window (`vision_auto_until`) keduanya mengaktifkan `[LAYAR:]` inject dan Curious.

## Health check

Bridge startup prints **SCOUTER PROVIDERS** when `scouter_enabled: true`.

```bash
python bridge_health.py
```

## Module map

- `arti_scouter_client.py` — chain orchestrator
- `arti_bridge.py` — `scouter_worker`, `apply_scouter_result`, `is_vision_active()`
- `arti_curious.py` — `curious_worthy` / `curious_hook` guards
