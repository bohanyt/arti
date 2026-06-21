# VTuber AI Co-host Bridge

Python bridge: **mic / YouTube chat → LLM → TTS → VTube Studio**, dengan RAG memori, scouter, dan ekspresi opsional.

Repo ini berisi **kode + cara wiring**. Karakter, nama streamer, dan file model VTS **milik kamu** — tidak disertakan.

## Quick start

```powershell
git clone <repo-url>
cd <folder>
python -m venv venv312
.\venv312\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-supertone.txt   # jika pakai Supertone

copy .env.example .env
# isi API key di .env

copy ARTI_SOUL.example.md ARTI_SOUL.md
copy ARTI_VIEWERS.example.md ARTI_VIEWERS.md
copy ARTI_MOOD_STATE.example.json ARTI_MOOD_STATE.json
# edit sesuai karakter kamu

python hermes_vtuber_bridge.py
```

## Dokumentasi

| File | Isi |
|------|-----|
| [`docs/WIRING.md`](docs/WIRING.md) | Setup lengkap: env, VTS, TTS, LLM, YT chat |
| [`docs/VTS-ANIMATION.md`](docs/VTS-ANIMATION.md) | Idle / expression / parameter — **beda tiap model** |
| [`docs/SCOUTER.md`](docs/SCOUTER.md) | Background digest & mood |
| [`docs/OBSERVER.md`](docs/OBSERVER.md) | Post-stream vault pipeline |
| [`CHANGELOG.md`](CHANGELOG.md) | Versi & patch notes |

## Struktur (yang ada di repo)

```
hermes_vtuber_bridge.py   # entry point live
arti_*.py                 # modul (voice, RAG, scouter, TTS, …)
supertone_engine.py       # TTS subprocess
subtitle_server.py        # OBS subtitle (opsional)
scripts/voice_ab_test.py  # lab suara offline (opsional)
vault/                    # memori live (kosong di repo)
data/voice_presets/       # contoh preset TTS
docs/                     # wiring & referensi teknis
```

Tidak ada: `archive/`, `docs/research/` (screenshot API pribadi), riwayat sesi, template model, DB RAG lokal.

## Lisensi

MIT — lihat [LICENSE](LICENSE).

## Keamanan

Jangan commit `.env` atau `ARTI_SOUL.md`. Jika pernah push repo lama yang berisi API key, **rotate** di konsol provider (Groq, Gemini, OpenRouter) sebelum go public.
