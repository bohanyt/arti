# OpenRouter models — Arti bridge

Semua slug di bawah pakai prefix provider, contoh: `nvidia/nemotron-3-super-120b-a12b:free`.

Edit di `CONFIG` [`hermes_vtuber_bridge.py`](../hermes_vtuber_bridge.py) atau override di `config_local.json` / `live_session.json`.

## Aturan pertama: cocokkan model dengan budget token

Ini pelajaran paling mahal di repo ini — dua kali kena bug yang sama.

**Model reasoning menghabiskan `max_tokens` untuk chain-of-thought sebelum menulis jawaban.** Kalau budgetnya ketat, `content` balik **kosong** dengan `finish_reason=length`, dan `clean_ai_reply()` membuang sisanya → *"Jawaban AI kosong"*. Ini yang terjadi pada qwen3.6 (fix `dd88d9e`, pakai `reasoning_effort=none`) dan pada Poolside Laguna (terdeteksi 2026-07-31).

Budget nyata per jalur:

| Jalur | `max_tokens` | Sumber |
|-------|-------------|--------|
| Live YT (1–5 kalimat) | **110–320** | `_TOKENS_BY_SENT`, `arti_reply_policy.py:55` |
| Live PTT | ~380 | `live_max_tokens_ptt` |
| Scouter / summarizer | **350** | `scouter_max_tokens` |
| Reflection post-stream | 2000 | `arti_openrouter.py` |

→ Jalur live dan scouter **wajib** model non-reasoning yang `finish=stop` di budget kecil. Reflection bebas.

## Peran di bridge

| Peran | CONFIG key | Default (Jul 2026) |
|-------|------------|-------------------|
| Health check probe | `openrouter_live_model` | Nemotron 3 Super |
| Live fallback (setelah Groq gagal) | `openrouter_live_model` → `openrouter_live_last_resort` | Nemotron Super → Gemma 4 |
| Summarizer tiap 5 trigger | `openrouter_summarizer_model` → `openrouter_summarizer_fallback` | Nemotron Super → Nemotron Nano |
| Scouter chain | `scouter_openrouter_models` | Nemotron Super → Nemotron Nano → Gemma 4 |
| Post-stream reflection | `openrouter_reflection_model` → fallback → last_resort | Nemotron Super → Gemma 4 → Laguna XS 2.1 |
| Reflection opsional berat | `openrouter_reflection_ultra_model` (`reflection_try_ultra`) | Nemotron Ultra |

**Main LLM live tetap Groq** (`groq_models` rolling). OpenRouter = fallback + offline brain.

## Hasil probe 2026-07-31

Diuji langsung ke `/chat/completions` dengan prompt bahasa Indonesia pendek.

| Model | Slug | 110 tok | 350 tok | Latensi | Verdict |
|-------|------|:-------:|:-------:|---------|---------|
| **Nemotron 3 Super 120B** | `nvidia/nemotron-3-super-120b-a12b:free` | ✅ stop | ✅ stop | **344–391 ms** | Terbaik — satu-satunya yang bersih **dan** cepat di budget ketat |
| **Gemma 4 26B** | `google/gemma-4-26b-a4b-it:free` | ✅ stop | ✅ stop | 1,1–3,7 dtk | Sehat tapi lambat — pakai sebagai last resort |
| **Nemotron 3 Nano 30B** | `nvidia/nemotron-3-nano-30b-a3b:free` | ⚠️ bocor CoT Inggris | ✅ stop | 342–436 ms | OK di ≥350 tok saja |
| **Ling 3.0 Flash** | `inclusionai/ling-3.0-flash:free` | ❌ kosong | ✅ stop | 0,9–1,2 dtk | Jangan di jalur live |
| **Laguna XS 2.1** | `poolside/laguna-xs-2.1:free` | ❌ kosong | ❌ kosong | 436 ms | Reasoning — butuh ~600 tok. Reflection saja |
| **Laguna S 2.1** | `poolside/laguna-s-2.1:free` | ❌ kosong | ❌ kosong | 0,6–2,7 dtk | Sama seperti XS |

## Slug MATI — jangan dipakai lagi

Semua mengembalikan `404 No endpoints found`:

| Slug mati | Pengganti | Catatan |
|-----------|-----------|---------|
| `poolside/laguna-xs.2:free` | `poolside/laguna-xs-2.1:free` | **Di-rename**, bukan dihapus — titik jadi strip, versi naik. Tapi tetap tidak cocok untuk jalur live (lihat tabel probe). |
| `poolside/laguna-m.1:free` | — | Hilang total; tier `m` sudah tidak ada. Terdekat: `poolside/laguna-s-2.1:free`. |
| `owl-alpha` | — | Hilang total. Sempat jadi default keras di `arti_openrouter.py`, `arti_scouter_client.py`, dan `scouter_openrouter_models`. |

## Bukan OpenRouter

| Provider | Dipakai untuk |
|----------|----------------|
| **Groq** | LLM utama PTT/YT (`groq_models`) |
| **Groq Whisper** | ASR mic |
| **NVIDIA NIM** | DiffusionGemma vision layar (`nvidia_model`) — bukan chat OpenRouter |

## Tips ganti model

1. Cek slug masih hidup: `GET https://openrouter.ai/api/v1/models` (tanpa auth) lalu cari id-nya. Model **sering di-rename**, bukan dihapus — cek varian dengan strip/versi berbeda sebelum menyimpulkan mati.
2. **Selalu probe di budget token asli jalurnya**, bukan budget besar. Model yang lolos di 600 token bisa mengembalikan kosong di 110.
3. Yang dinilai: `finish_reason` harus `stop` (bukan `length`), `content` tidak kosong, dan tidak ada CoT bahasa Inggris yang bocor.
4. Set key di CONFIG, restart bridge. Health check startup akan probe `openrouter_live_model`.
