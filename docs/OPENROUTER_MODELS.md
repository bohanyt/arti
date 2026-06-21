# OpenRouter models — Arti bridge

Semua slug di bawah pakai prefix provider, contoh: `poolside/laguna-xs.2:free`.

Edit di `CONFIG` [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) atau override di `live_session.json`.

## Peran di bridge

| Peran | CONFIG key | Default (Jun 2026) |
|-------|------------|-------------------|
| Health check probe | `openrouter_live_model` | `poolside/laguna-xs.2:free` |
| Live fallback (setelah Groq gagal) | `openrouter_live_model` → `openrouter_live_last_resort` | XS.2 → M.1 |
| Summarizer tiap 5 trigger | `openrouter_summarizer_model` → `openrouter_summarizer_fallback` | XS.2 → Nemotron Nano |
| Post-stream reflection | `openrouter_reflection_model` → fallback → last_resort | Super → M.1 → XS.2 |
| Reflection opsional berat | `openrouter_reflection_ultra_model` (`reflection_try_ultra`) | Nemotron Ultra |

**Main LLM live tetap Groq** (`groq_models` rolling). OpenRouter = fallback + offline brain.

## Rekomendasi rilis ~2 bulan terakhir (free tier)

### Poolside Laguna (Apr 2026) — coding/agent, cepat

| Model | Slug | Cocok untuk |
|-------|------|-------------|
| **Laguna XS.2** | `poolside/laguna-xs.2:free` | Summarizer, live fallback cepat, health check |
| **Laguna M.1** | `poolside/laguna-m.1:free` | Last resort live, reflection fallback |

### NVIDIA Nemotron 3 (Mar–Jun 2026)

| Model | Slug | Cocok untuk |
|-------|------|-------------|
| **Nemotron 3 Nano** | `nvidia/nemotron-3-nano-30b-a3b:free` | Summarizer fallback, ringan |
| **Nemotron 3 Super** | `nvidia/nemotron-3-super-120b-a12b:free` | Reflection post-stream (kualitas) |
| **Nemotron 3 Ultra** | `nvidia/nemotron-3-ultra-550b-a55b:free` | Reflection berat (`reflection_try_ultra: true`) |

### Legacy (masih bisa, tapi tua)

| Model | Slug | Catatan |
|-------|------|---------|
| owl-alpha | `owl-alpha` | Dulu default summarizer; diganti Laguna XS.2 |

## Bukan OpenRouter

| Provider | Dipakai untuk |
|----------|----------------|
| **Groq** | LLM utama PTT/YT (`groq_models`) |
| **Groq Whisper** | ASR mic |
| **NVIDIA NIM** | DiffusionGemma vision layar (`nvidia_model`) — bukan chat OpenRouter |

## Tips ganti model

1. Cek di [openrouter.ai/models](https://openrouter.ai/models?q=free) slug masih `:free` atau paid.
2. Set satu key di CONFIG, restart bridge.
3. Health check startup akan probe `openrouter_live_model`.
4. Laguna = coding/agent (bagus JSON summarizer). Nemotron Super/Ultra = reasoning panjang (reflection).
