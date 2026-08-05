# PLAN v0.4 — Arti Voice Upgrade: Custom TTS with Voice Merging

> Created: 2026-05-28
> Status: PLANNING (no execution yet)
> Requested by: Bohan (via plan mode)

---

## GOAL

Upgrade TTS Arti dari edge-tts (GadisNeural, flat/basic) ke suara yang lebih "hidup" dan karakteristik — tanpa clone suara orang lain (etis). Opsi utama: **voice merging** (gabung beberapa voice model) atau **fine-tune model sendiri** pakai suara Bohan.

---

## CURRENT CONTEXT

### Yang sekarang dipakai:
- **edge-tts** dengan voice `id-ID-GadisNeural`
- Output: Cepat, stabil, tapi flat/robotik, nggak karakteristik
- Pipeline: `hermes_vtuber_bridge.py` → `TTSEngine.speak()` → `edge_tts.Communicate()` → `sd.play()` via Virtual Cable

### Problem:
- Suara GadisNeural terlalu "generic" — nggak matching sama karakter Arti (feisty, sassy, bold)
- Nada monoton, kurang ekspresi
- Nggak ada "personality" di suara

### Constraints:
- ❌ JANGAN clone suara orang lain (etis)
- ✅ Boleh pakai suara Bohan sendiri sebagai base
- ✅ Boleh merge/gabung multiple voice models
- ✅ Boleh fine-tune model TTS dengan data suara Bohan
- ⚠️ VRAM laptop utama penuh → butuh laptop kedua untuk heavy inference
- ⚠️ Model Qwen TTS ~3.5GB → muat di laptop kedua dengan Docker

---

## RESEARCH FINDINGS

### Opsi 1: GPT-SoVITS (RECOMMENDED)
**Repo:** https://github.com/RVC-Boss/GPT-SoVITS (58.1k stars)
**Approach:** Few-shot voice cloning — 1 menit data suara bisa train model TTS

**Pros:**
- ✅ Bisa pakai suara Bohan sendiri (etis — suara sendiri)
- ✅ 1 menit data suara cukup untuk hasil bagus
- ✅ Support Indonesia (via custom training)
- ✅ Docker support tersedia
- ✅ Bisa "merge" beberapa voice sample jadi satu karakter
- ✅ Inference cepat (~1-2 detik per kalimat)
- ✅ Model size ~1-2GB (muat di laptop kedua)

**Cons:**
- ❌ Butuh training data (record suara Bohan ~1-5 menit)
- ❌ Setup agak complex (Docker + model download)
- ❌ GPU recommended (tapi bisa CPU dengan speed penalty)

**Docker:** Ada `Docker/` folder di repo, pakai miniforge

---

### Opsi 2: XTTS v2 (Coqui)
**Repo:** https://github.com/coqui-ai/TTS (45.4k stars, ARCHIVED)
**Approach:** Zero-shot voice cloning dengan 3-6 detik audio reference

**Pros:**
- ✅ Zero-shot — cuma butuh 3-6 detik audio reference
- ✅ Bisa pakai suara Bohan sendiri
- ✅ Multi-language termasuk Indonesia
- ✅ Docker support

**Cons:**
- ❌ Repo udh archived (2 tahun nggak update)
- ❌ VRAM butuh ~4GB untuk inference
- ❌ Quality nggak sebaik GPT-SoVITS untuk custom voice

---

### Opsi 3: ChatTTS
**Repo:** https://github.com/2noise/ChatTTS (39.3k stars)
**Approach:** Generative speech model untuk daily dialogue

**Pros:**
- ✅ Sangat natural untuk dialogue
- ✅ Bisa kontrol emotion/prosody
- ✅ Ringan (~2GB model)

**Cons:**
- ❌ Primary support Mandarin/Cina
- ❌ Indonesia support terbatas
- ❌ Nggak ada voice cloning built-in

---

### Opsi 4: Qwen TTS (Local)
**Model:** Qwen2.5-TTS (~3.5GB)
**Approach:** Local TTS model dengan voice cloning capability

**Pros:**
- ✅ Bisa jalan lokal (laptop kedua via Docker)
- ✅ Support Indonesia
- ✅ Voice cloning support

**Cons:**
- ❌ Model besar (3.5GB)
- ❌ VRAM butuh ~6-8GB untuk inference cepat
- ❌ Setup complex

---

## RECOMMENDED APPROACH: GPT-SoVITS + Laptop Kedua

### Arsitektur:
```
Laptop Utama (mesin utama)          Laptop Kedua (Docker Host)
┌─────────────────────┐             ┌──────────────────────────┐
│ hermes_vtuber_      │             │ Docker: GPT-SoVITS       │
│ bridge.py           │             │                          │
│                     │  HTTP API   │ POST /tts                │
│ TTSEngine.speak() ──┼────────────→│ {text, voice_id}         │
│                     │             │                          │
│ ←── audio file ─────┼─────────────│ → returns WAV/MP3       │
│                     │             │                          │
│ sd.play(audio)      │             │ Model: trained on        │
│                     │             │ Bohan's voice samples    │
└─────────────────────┘             └──────────────────────────┘
```

### Alur Data:
1. Bohan record suara ~1-5 menit (baca teks random, ngobrol santai)
2. Upload ke laptop kedua
3. Train GPT-SoVITS model (~30 menit - 2 jam tergantung GPU)
4. Export model (~1-2GB)
5. Serve via Docker HTTP API
6. Bridge panggil API setiap mau ngomong
7. Audio balik ke bridge → sd.play() via Virtual Cable

---

## STEP-BY-STEP PLAN

### Phase A: Persiapan Data Suara

**Goal:** Kumpulkan training data suara Bohan

**Steps:**
1. Record suara Bohan ~5 menit total
   - Baca teks berita/artikel (formal)
   - Ngobrol santai (casual)
   - Baca dialog Arti (feisty, sassy)
   - Baca catchphrase Arti ("Oke guys, Arti dulu ya!", dll)
2. Format: WAV/MP3, 16kHz, mono, clear (no background noise)
3. Split per kalimat (1 file per kalimat, ~5-15 detik each)
4. Buat transcript file (text per audio file)
5. Upload ke laptop kedua

**Files yang diperlukan:**
- `dataset/audio/*.wav` — audio files
- `dataset/transcript.txt` — transcript per line

---

### Phase B: Setup GPT-SoVITS di Laptop Kedua

**Goal:** Install dan setup GPT-SoVITS via Docker

**Steps:**
1. Install Docker Desktop di laptop kedua
2. Clone GPT-SoVITS repo
3. Download pre-trained models (~5GB total)
4. Build Docker image
5. Test inference dengan default voice

**Docker command (estimasi):**
```bash
git clone https://github.com/RVC-Boss/GPT-SoVITS.git
cd GPT-SoVITS
# Download models ke checkpoints/
docker-compose up -d
```

**Resource requirements:**
- RAM: 8GB minimum
- GPU: NVIDIA with 4GB+ VRAM (atau CPU mode, lebih lambat)
- Storage: ~10GB (models + Docker image)

---

### Phase C: Training Custom Voice

**Goal:** Train GPT-SoVITS dengan suara Bohan

**Steps:**
1. Masukkan dataset ke container
2. Preprocess audio (split, clean, format)
3. Train SoVITS model (~1-2 jam)
4. Train GPT model (~30 menit - 1 jam)
5. Export combined model
6. Test inference dengan sample text

**Output:**
- Model file (~1-2GB)
- API endpoint untuk inference

---

### Phase D: Integrasi ke Bridge

**Goal:** Hubungkan GPT-SoVITS API ke hermes_vtuber_bridge.py

**Steps:**
1. Buat `tts_gptsovits.py` module
   - Class `GPTSoVITSEngine`
   - Method `speak(text)` → call HTTP API → save temp WAV → return path
   - Method `set_voice(voice_id)` — untuk switch voice/character
2. Modifikasi `TTSEngine` di bridge:
   - Tambah config `tts_backend` ("edge" atau "gptsovits")
   - Jika gptsovits: panggil GPTSoVITSEngine
   - Jika edge: panggil edge_tts (fallback)
3. Tambah config di CONFIG:
   - `tts_backend`: "gptsovits"
   - `gptsovits_url`: "http://LAPTOP2_IP:9880"
   - `gptsovits_voice`: "bohan_arti"

**Files yang berubah:**
- `hermes_vtuber_bridge.py` — TTSEngine class
- `tts_gptsovits.py` — new module (NEW)
- `CONFIG` — tambah tts_backend config

---

### Phase E: Voice Merging (Advanced)

**Goal:** Gabung beberapa voice style jadi satu karakter Arti

**Approach:**
1. Train multiple voice styles:
   - `bohan_casual` — suara Bohan santai
   - `bohan_formal` — suara Bohan formal
   - `bohan_excited` — suara Bohan excited
2. Buat "merged" voice dengan interpolate weights
3. Atau: train satu model dengan semua style, kontrol via prompt

**GPT-SoVITS mendukung:**
- Multi-speaker training
- Style transfer via reference audio
- Emotion control via text prompt

---

## FILES YANG BERUBAH/DIBUAT

| File | Change |
|------|--------|
| `hermes_vtuber_bridge.py` | TTSEngine: tambah GPT-SoVITS backend |
| `tts_gptsovits.py` | NEW: GPT-SoVITS HTTP client |
| `tts_edge.py` | NEW: extract existing edge_tts logic |
| `CONFIG` | Tambah tts_backend, gptsovits_url, gptsovits_voice |
| `requirements.txt` | Tambah `requests` (untuk HTTP API) |

---

## TESTING / VALIDATION

1. **Data quality test:** Cek apakah recording cukup jelas
2. **Training test:** Cek apakah model converge (loss turun)
3. **Inference test:** Test dengan sample text Indonesia
4. **Integration test:** Test full pipeline bridge → GPT-SoVITS → VTS
5. **Quality test:** Bandingkan hasil GPT-SoVITS vs edge-tts
6. **Latency test:** Measure response time (target < 3 detik)

---

## RISKS & MITIGATIONS

| Risk | Mitigation |
|------|------------|
| Training data kurang bagus | Record minimal 5 menit, multiple styles |
| Laptop kedua nggak punya GPU | Pakai CPU mode (lebih lambat tapi works) |
| GPT-SoVITS setup complex | Ikuti Docker guide step-by-step |
| Voice hasil training nggak cocok | Re-record dengan style berbeda, re-train |
| Latency terlalu tinggi | Cache frequent phrases, pre-generate |
| Docker networking issue | Test dengan localhost dulu, then LAN |

---

## OPEN QUESTIONS

1. **Laptop kedua spec-nya apa?** (RAM, GPU, OS) — menentukan apakah bisa run GPT-SoVITS
2. **Bohan mau record sendiri atau bantu record?** — kualitas data = kualitas hasil
3. **Target kapan mau mulai training?** — butuh dedicated time ~2-3 jam
4. **Mau pakai voice merging atau single voice?** — merging lebih complex tapi lebih karakteristik

---

## ESTIMATED TIMELINE

| Task | Duration |
|------|----------|
| Record training data | 30 menit |
| Setup Docker + GPT-SoVITS | 1-2 jam |
| Training model | 2-4 jam |
| Integrasi ke bridge | 1 jam |
| Testing + tuning | 1-2 jam |
| **Total** | **~5-10 jam** |

---

*Plan saved. Ready for execution after user approval and laptop kedua spec confirmation.*
