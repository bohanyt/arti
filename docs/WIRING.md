# Wiring Guide

Panduan menghubungkan bridge ke stack kamu. **Tidak** menyertakan karakter atau model VTuber spesifik — sesuaikan sendiri.

## 1. Prerequisites

- Windows 10/11 (path di README contoh PowerShell)
- Python 3.11+ (3.12 untuk Supertone disarankan)
- [VTube Studio](https://denchisoft.com/) + plugin API enabled
- Virtual audio cable (opsional, untuk routing TTS ke VTS)
- API key: minimal **Groq** atau **Gemini** (lihat `.env.example`)

## 2. Environment (`.env`)

Salin `.env.example` → `.env`. Isi yang dipakai:

| Variable | Untuk |
|----------|--------|
| `GROQ_API_KEY` | LLM utama (voice turn) |
| `GEMINI_API_KEY` | Alternatif / vision |
| `OPENROUTER_API_KEY` | Fallback / scouter chain |
| `CLOUDFLARE_*` | Vision Workers (opsional) |

Jangan commit `.env`.

## 3. Karakter (prompt)

Bridge membaca **`ARTI_SOUL.md`** saat runtime.

```powershell
copy ARTI_SOUL.example.md ARTI_SOUL.md
```

Edit: nama co-host, gaya bicara, panggilan streamer, aturan bahasa. File asli **gitignored**.

Opsional: `ARTI_VIEWERS.md` (profil viewer), `ARTI_MOOD_STATE.json` (mood runtime).

## 4. VTube Studio

1. Buka VTS → Settings → **API** → allow plugins.
2. Jalankan bridge; pertama kali akan minta **Allow** di VTS.
3. Token disimpan ke `vts_token.txt` (lokal, gitignored).

CONFIG relevan di `hermes_vtuber_bridge.py`:

```python
"vts_api_port": 8002,
```

### Animasi & ekspresi

Lihat [`VTS-ANIMATION.md`](VTS-ANIMATION.md).  
**Penting:** nama hotkey, file `.exp3.json`, dan parameter Live2D **berbeda per model**. Nilai di CONFIG hanya contoh wiring — map ke setup kamu.

## 5. TTS

Default CONFIG: `tts_engine: "supertone"`.

| Key | Arti |
|-----|------|
| `supertonic_voice` | Preset Supertone (`F1`–`F5`, `M1`–`M5`) |
| `supertonic_speed` | Speech rate (bukan pitch) |
| `supertonic_total_steps` | Kualitas vs kecepatan (5–12) |
| `supertonic_lang` | `id`, `en`, dll. |

Fallback otomatis ke Edge TTS jika Supertone gagal (`tts_voice` di CONFIG).

Preset dokumentasi: `data/voice_presets/arti_stable.json` (tidak auto-load).

Lab offline: `python scripts/voice_ab_test.py --matrix` → WAV di `data/voice_samples/ab/`.

## 6. LLM provider

Set di CONFIG:

```python
"api_provider": "groq",   # groq | gemini | gemini_live | sambanova
"smart_groq_routing": True,
```

Routing voice turn (tanpa round-robin): YT chat → model kuat, curious → model cepat, mic → heuristic panjang/pendek.

## 7. YouTube live chat

```python
"youtube_chat_enabled": True,
"youtube_video_id": "VIDEO_ID_LIVE",
```

Wake phrase di chat memicu jawaban (sama seperti mic wake). Cooldown per-viewer: `yt_chat_cooldown_sec`.

## 8. Vault / memori

Folder `vault/sessions/` terisi saat shutdown bersih. Repo hanya menyertakan struktur kosong.

RAG: `python arti_vault_rag.py --reindex-all` setelah sesi panjang.

## 9. Cek sehat

Jalankan bridge, tes PTT singkat, pastikan TTS + VTS merespons. Lihat log `[Brain]`, `[TTS]`, `[VTS]` untuk error.
