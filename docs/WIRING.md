# Wiring Guide

Panduan menghubungkan bridge ARTI ke stack kamu. Dokumentasi public ini tidak menyertakan karakter, model VTuber, token, viewer data, atau konfigurasi pribadi tertentu.

## 1. Prerequisites

- Windows 10/11
- Python 3.11 untuk bridge utama
- Node.js jika memakai integrasi Minecraft
- [VTube Studio](https://denchisoft.com/) + plugin API enabled jika memakai avatar VTS
- OBS Studio jika memakai perpindahan scene
- Virtual audio cable (opsional, untuk routing TTS/audio)
- API key untuk provider yang benar-benar kamu aktifkan; lihat [`.env.example`](../.env.example)

Beberapa integrasi opsional memiliki dependency manifest sendiri, misalnya `requirements-supertone.txt`.

## 2. Environment (`.env`)

Salin `.env.example` menjadi `.env`, lalu isi hanya provider yang dipakai.

| Variable | Untuk |
|---|---|
| `GROQ_API_KEY` | LLM/ASR path yang memakai Groq |
| `GEMINI_API_KEY` | Gemini / vision |
| `OPENROUTER_API_KEY` | OpenRouter routing/fallback |
| `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | Cloudflare Workers AI vision/scouter |
| `SAMBANOVA_API_KEY` | SambaNova provider path |
| `DISCORD_BOT_TOKEN` | Integrasi Discord jika diaktifkan |

Jangan commit `.env`.

## 3. Local config

Salin [`config_local.json.example`](../config_local.json.example) menjadi `config_local.json` dan isi nilai lokal yang diperlukan.

Contoh nilai yang biasanya perlu disesuaikan:

- `youtube_video_id`
- `streamer_name`
- `vts_model_dir`
- `vts_api_port`
- owner/viewer handles
- OBS scene names
- path executable lokal bila fitur tersebut dipakai

`config_local.json` harus tetap lokal dan tidak di-commit.

## 4. Karakter / prompt

Bridge membaca `ARTI_SOUL.md` saat runtime. Mulai dari template public:

```powershell
copy ARTI_SOUL.example.md ARTI_SOUL.md
```

Edit nama co-host, gaya bicara, panggilan streamer, dan aturan bahasa sesuai karakter kamu. File runtime asli digitignore.

Opsional:

```powershell
copy ARTI_VIEWERS.example.md ARTI_VIEWERS.md
copy ARTI_MOOD_STATE.example.json ARTI_MOOD_STATE.json
```

## 5. VTube Studio

1. Buka VTube Studio → Settings → API → allow plugins.
2. Jalankan `python hermes_vtuber_bridge.py`.
3. Saat diminta VTS, izinkan plugin ARTI.
4. Token lokal disimpan di file yang digitignore.

Port VTS dapat dioverride lewat `config_local.json`:

```json
{
  "vts_api_port": 8002
}
```

Untuk motion/ekspresi, lihat [`VTS-ANIMATION.md`](VTS-ANIMATION.md) dan [`Expression-Motion-System.md`](Expression-Motion-System.md). Nama hotkey, `.exp3.json`, dan parameter Live2D berbeda untuk tiap model, jadi map ke setup milikmu sendiri.

## 6. TTS

Runtime mendukung jalur TTS yang dikonfigurasi oleh bridge dan dependency opsionalnya. Jika memakai Supertone, install dependency dari manifest yang sesuai dan sesuaikan key TTS di konfigurasi lokal/runtime kamu.

Jangan menganggap preset suara, sample audio, atau hasil tuning milik maintainer sebagai bagian dari distribusi public; materi lokal tersebut sengaja tidak dipublish.

## 7. Model/provider

Provider dipilih dari konfigurasi runtime. Isi credential yang diperlukan di `.env`; jangan hard-code token ke source.

Dokumentasi tambahan:

- [`OPENROUTER_MODELS.md`](OPENROUTER_MODELS.md)
- [`VISION-APIS.md`](VISION-APIS.md)
- [`SCOUTER.md`](SCOUTER.md)

## 8. YouTube live chat

Set video ID live kamu lewat `config_local.json`:

```json
{
  "youtube_video_id": "YOUR_LIVE_VIDEO_ID"
}
```

Gunakan ID milik stream sendiri. Jangan commit ID sesi, viewer data, atau payload chat hasil capture ke repo public.

## 9. Memory / RAG

Kode RAG tersedia di [`arti_vault_rag.py`](../arti_vault_rag.py), tetapi vault, session notes, viewer profiles, transcripts, dan database runtime sebenarnya sengaja tidak ikut Git.

Setelah data lokal milikmu sendiri terbentuk, reindex dapat dijalankan sesuai kebutuhan runtime:

```bash
python arti_vault_rag.py --reindex-all
```

Pastikan semua output memory tetap berada di path yang sudah digitignore.

## 10. Minecraft

Minecraft mempunyai setup terpisah karena memakai Node/Mineflayer. Lihat [`MINECRAFT-SETUP.md`](MINECRAFT-SETUP.md).

## 11. Cek sehat

Jalankan:

```bash
python hermes_vtuber_bridge.py
```

Lalu aktifkan hanya integrasi yang memang sudah kamu konfigurasi. Tes microphone/TTS/VTS/OBS/game integration secara lokal sesuai stack masing-masing. Public cloud CI tidak membuktikan hardware atau aplikasi lokal tersebut bekerja di mesinmu.

Kembali ke [index dokumentasi public](README.md).
