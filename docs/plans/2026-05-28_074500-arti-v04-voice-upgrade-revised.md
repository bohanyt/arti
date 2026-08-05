# PLAN v0.4 — Arti Voice Upgrade: Qwen-3 TTS (Coqui) via ComfyUI

> Created: 2026-05-28 (updated after watching 4 YouTube videos)
> Status: PLANNING (no execution yet)

---

## RESEARCH SUMMARY (4 Videos Watched)

### Video 1: Voicebox — Free Local Voice Cloning (Kevin Stratvert)
**Tool:** Voicebox (by LiterRita)
- ✅ Free & open-source desktop app
- ✅ Uses Qwen 3 TTS + Chatterbox under the hood
- ✅ 30-second voice sample → clone AI voice
- ✅ Multiple voice models available
- ✅ "Stories" feature — multi-speaker conversations
- ✅ Effects: robotic, radio, echo, deep voice
- ✅ GPU support for faster inference
- ✅ Works offline on PC
- ⚠️ Windows desktop app (not Docker/server)
- ⚠️ Not designed for programmatic API calls (GUI only)

### Video 2: Voicebox Clone Voice Tutorial
**Same tool**, different tutorial — confirms:
- Download Qwen TTS 1.7B model
- Whisper base for transcription
- 30-second recording → AI voice clone
- Export audio feature
- Simple GUI, no API

### Video 3: Qwen-3 TTS (Coqui) — State of the Art (Fireship-style)
**Tool:** Coqui TTS (Qwen-3 TTS)
- ✅ Open-source, runs offline
- ✅ 3 modes: (1) Voice cloning from seconds of audio, (2) Pre-built voices with emotion control, (3) Voice design from text prompt
- ✅ 2 model sizes: 0.6B (<2GB VRAM) and 1.7B (<4GB VRAM)
- ✅ Benchmarks better than ElevenLabs, MiniMax, GPT-4o, Gemini Pro
- ✅ Multilingual: English, Chinese, Japanese, Korean, Spanish, French, Hindi, German
- ✅ Emotion/expression control via text prompt (sad, angry, flirty, excited, etc.)
- ✅ Pace and tone control throughout transcript
- ✅ Multi-speaker podcast mode
- ✅ **ComfyUI integration** — graphical workflow builder
- ✅ Auto-downloads models from HuggingFace
- ✅ Super fast generation (~10-20 seconds on laptop GPU)
- ⚠️ Needs ComfyUI setup (not standalone)

### Video 4: Dogra — Open-Source Vapi Alternative (Better Stack)
**Tool:** Dogra
- ✅ Self-hosted voice AI agent platform
- ✅ Visual workflow builder (no-code canvas for devs)
- ✅ Bring your own providers (LLM, TTS, STT)
- ✅ Testing, tracing, recordings, analytics
- ✅ Docker Compose setup
- ✅ Open-source, inspectable, controllable
- ⚠️ Very new (low GitHub stars)
- ⚠️ Overkill for our use case (we just need TTS, not full voice agent pipeline)

---

## REVISED RECOMMENDATION

### Primary: Qwen-3 TTS (Coqui) via ComfyUI

**Kenapa ini yang terbaik untuk Arti:**

1. ✅ **Model size kecil** — 0.6B (<2GB) atau 1.7B (<4GB). Laptop kedua muat.
2. ✅ **Voice cloning dari suara Bohan sendiri** — etis, nggak clone orang lain.
3. ✅ **Emotion control** — bisa specify "feisty and sarcastic" atau "excited" langsung di prompt. Ini SANGAT cocok untuk karakter Arti.
4. ✅ **Voice design dari text prompt** — bisa buat suara Arti dari deskripsi: "A young Indonesian woman, feisty and sassy, slightly raspy, confident tone"
5. ✅ **Multi-speaker** — bisa buat dialog Arti + Bohan (untuk stories/intro)
6. ✅ **Super fast** — ~10-20 detik generation di laptop GPU
7. ✅ **Free & open-source** — nggak ada subscription/API cost
8. ✅ **Offline** — nggak perlu internet setelah model downloaded

### Secondary: Voicebox (jika ComfyUI terlalu complex)

**Fallback option:**
- Desktop app, simple GUI
- Bisa record suara Bohan → generate TTS
- Tapi nggak ada programmatic API — harus manual generate + export
- Cocok untuk generate pre-recorded clips (intro, outro, catchphrases)

---

## RECOMMENDED ARCHITECTURE

### Option A: ComfyUI + Qwen-3 TTS (RECOMMENDED)

```
Laptop 1 (mesin utama)                    Laptop 2 (Docker/ComfyUI Host)
┌─────────────────────────┐              ┌──────────────────────────────────┐
│ hermes_vtuber_bridge.py │              │ ComfyUI + Qwen-3 TTS Custom Node │
│                         │              │                                  │
│ TTSEngine.speak(        │              │ Workflow:                        │
│   text,                 │  HTTP API    │ 1. Voice Design Node             │
│   emotion="feisty",     │────────────→│    prompt="young Indonesian      │
│   voice_id="arti"       │              │    woman, feisty, sassy"         │
│ )                       │              │ 2. Qwen-3 TTS 1.7B model         │
│                         │              │ 3. Generate ~10-20s              │
│ ←── WAV audio ──────────┼──────────────│ 4. Save to output folder         │
│                         │              │                                  │
│ sd.play(audio)          │              │ Alternative: Voice Clone mode    │
│ via Virtual Cable       │              │ (trained on Bohan's voice)       │
└─────────────────────────┘              └──────────────────────────────────┘
```

**Network:** Both laptops on same WiFi/LAN. ComfyUI web interface accessible via `http://LAPTOP2_IP:8188`.

### Option B: Voicebox (Simple/Fallback)

```
Laptop 1                              Laptop 2
┌─────────────────────┐              ┌──────────────────────┐
│ bridge.py           │              │ Voicebox Desktop App │
│                     │  Manual      │                      │
│ Pre-recorded clips ─┼──copy───────→│ Record Bohan voice   │
│ (intro/outro)       │              │ Generate TTS         │
│                     │              │ Export WAV           │
│ Real-time: edge-tts │              │                      │
│ (fallback)          │                      │
└─────────────────────┘              └──────────────────────┘
```

---

## DETAILED COMPARISON

| Feature | Qwen-3 TTS (ComfyUI) | Voicebox | Dogra |
|---------|---------------------|----------|-------|
| Voice cloning | ✅ 3-30 sec sample | ✅ 30 sec sample | ❌ |
| Text-to-speech | ✅ | ✅ | ✅ (bring your own) |
| Emotion control | ✅ text prompt | ❌ (fixed effects) | ❌ |
| Voice design | ✅ from text prompt | ❌ | ❌ |
| API access | ✅ ComfyUI API | ❌ GUI only | ✅ REST API |
| Multi-speaker | ✅ | ✅ Stories | ❌ |
| Model size | 2-4GB | Unknown | N/A (bring your own) |
| GPU required | ✅ Recommended | ✅ Optional | ❌ |
| Setup complexity | Medium (ComfyUI) | Low (installer) | High (Docker + deps) |
| Free | ✅ | ✅ | ✅ |
| Offline | ✅ | ✅ | ✅ |
| **Best for Arti** | ✅ **PRIMARY** | Backup clips | Overkill |

---

## STEP-BY-STEP PLAN (Qwen-3 TTS via ComfyUI)

### Phase A: Setup ComfyUI di Laptop Kedua

**Prerequisites:**
- Laptop kedua: 8GB+ RAM, NVIDIA GPU with 4GB+ VRAM (recommended)
- Docker Desktop installed
- Both laptops on same network

**Steps:**
1. Install Docker Desktop di laptop kedua
2. Install ComfyUI:
   ```bash
   git clone https://github.com/comfyanonymous/ComfyUI.git
   cd ComfyUI
   # Install dependencies
   pip install -r requirements.txt
   ```
3. Install Qwen-3 TTS custom node:
   ```bash
   cd custom_nodes
   git clone https://github.com/CoquiAI/ComfyUI-CoquiTTS.git
   cd ComfyUI-CoquiTTS
   pip install -r requirements.txt
   ```
4. Start ComfyUI:
   ```bash
   python main.py --listen 0.0.0.0
   ```
5. Access ComfyUI web UI at `http://LAPTOP2_IP:8188`
6. Load Qwen-3 TTS workflow template

**Estimated time:** 1-2 hours

---

### Phase B: Check Available Pre-Built Voices List

**GOAL:** Cek daftar pre-built voices Qwen-3 TTS sebelum setup. Pastikan ada yang cocok untuk karakter Arti (Indonesia atau other languages).

**Steps:**
1. Buka Qwen-3 TTS HuggingFace page atau Coqui docs
2. Cek list pre-built voices yang available:
   - 3 Chinese voices
   - 2 English voices (Aiden, Ryan)
   - 1 Japanese voice
   - 1 Korean voice
   - 2 Chinese dialect voices
   - ❓ Other languages (need to verify — mungkin ada Indonesia/Spanish/French/Hindi)
3. Cek apakah ada voice yang bisa speak Indonesia dengan natural
4. **Jika ada voice Indonesia/other languages:** Lanjut ke Phase C (pilih + tweak)
5. **Jika nggak ada:** Fallback ke Voice Design mode (text prompt) atau Voice Clone dari suara Bohan

**Resources:**
- HuggingFace: `https://huggingface.co/Qwen/Qwen2.5-TTS`
- Coqui docs: https://docs.coqui.ai/
- ComfyUI-CoquiTTS: https://github.com/CoquiAI/ComfyUI-CoquiTTS

**Estimated time:** 15-30 menit

---

### Phase C: Pick Pre-Built Voice + Tweak Parameters

**GOAL:** Pilih pre-built voice yang paling cocok, terus tweak parameter (pitch, speed, Hz, dll) sampai suaranya matching karakter Arti.

**Jika ada voice Indonesia/other languages:**
1. Load voice di ComfyUI
2. Generate sample text Indonesia
3. Evaluasi: natural? Cocok sama Arti?
4. Jika cocok → lanjut ke Phase C2 (tweak)
5. Jika nggak cocok → coba voice lain atau fallback ke Voice Design

**Jika nggak ada voice Indonesia:**
1. Coba English voices (Aiden/Ryan) dengan text Indonesia
2. Evaluasi accent/naturalness
3. Kalau masih kurang → pakai Voice Design mode (Phase C alternatif)

### Phase C2: Tweak Voice Parameters

**GOAL:** Fine-tune suara pre-built voice biar lebih karakteristik seperti Arti.

**Parameters yang bisa di-tweak:**
- **Pitch:** Naikin/turunin (Arti = young female, pitch agak tinggi)
- **Speed:** Naikin/turunin (Arti = energetic, speed agak cepat)
- **Hz/Frequency:** Adjust base frequency
- **Emotion prompt:** "feisty", "sassy", "confident", "young"
- **Volume/Energy:** Adjust loudness

**WKWK approach:** Coba-coba parameter → generate → dengerin → ulangi sampai pas.

**Contoh workflow:**
```
1. Load pre-built voice "Aiden" (English male) → pitch naikin +2 → speed +10%
   → dengerin → masih terlalu male
   
2. Load "Ryan" (English male) → pitch naikin +3 → speed +15%
   → denerin → lebih cocok
   
3. Tambah emotion prompt "feisty and sassy"
   → generate → dengerin →ampir pas
   
4. Fine-tune lagi: pitch +3, speed +12%, emotion "confident"
   → generate → dengerin → PAS! → simpan setting
```

**Jika Voice Design mode (tanpa pre-built):**
```
1. Voice Design node → prompt: "A young Indonesian woman in her early 20s"
2. Generate sample → dengerin
3. Iterasi prompt: tambah "feisty, sassy, slightly raspy, confident"
4. Generate lagi → dengerin → tambah "playful energy, bold tone"
5. Sampai pas → simpan prompt
```

**Estimated time:** 1-3 hours (iterasi + tweaking)

---

### Phase D: Voice Design/Clone Alternatives

**Ini fallback jika pre-built voices nggak cocok:**

**Option 1: Voice Design (Text Prompt)**
- No training data needed
- Just describe Arti's voice in text
- Example: "A young Indonesian woman in her early 20s, feisty and sassy, slightly raspy voice, confident and bold tone, speaks with playful energy"

**Option 2: Voice Clone dari Suara Bohan (Lebih Personal)**
1. Record Bohan's voice ~5-10 menit total
2. Upload reference audio ke ComfyUI
3. Generate speech samples
4. Iterate on prompt until voice matches Arti's character

**Option 3: Voice Merge (Advanced)**
- Pre-built voice + sedikit tweak + emotion prompt
- Gabung kelebihan pre-built (naturalness) dengan karakter Arti (emotion control)

**Estimated time:** 2-4 hours (jika perlu record + training)

---

### Phase D: Integrasi ke Bridge

**Steps:**
1. Buat `tts_coqui.py` module:
   ```python
   class CoquiTTSEngine:
       def __init__(self, server_url="http://LAPTOP2_IP:8188"):
           self.server_url = server_url
       
       async def speak(self, text, emotion="feisty"):
           # Call ComfyUI API
           # Download generated WAV
           # Play via sounddevice
           pass
   ```
2. Modifikasi `TTSEngine` di bridge:
   - Tambah `tts_backend` config ("edge" atau "coqui")
   - Default: coqui, fallback: edge-tts
3. Tambah config:
   ```python
   "tts_backend": "coqui",
   "coqui_url": "http://192.168.1.x:8188",
   "coqui_voice_prompt": "young Indonesian woman, feisty, sassy"
   ```

**Estimated time:** 1 hour

---

### Phase E: Emotion-Aware TTS

**Integrasi dengan summarizer:**
1. Summarizer detect emotion (senang/sedih/marah/bingung/excited/neutral)
2. Map emotion → voice prompt modifier:
   - Senang → "happy, upbeat tone"
   - Sedih → "sad, soft voice"
   - Marah → "angry, forceful tone"
   - Excited → "excited, energetic voice"
   - Feisty → "sassy, bold, confident"
   - Lazy → "relaxed, slow, casual"
3. Inject voice prompt ke TTS request
4. Arti berbicara dengan tone yang sesuai konteks!

**Estimated time:** 30 menit

---

## FILES YANG BERUBAH/DIBUAT

| File | Change |
|------|--------|
| `hermes_vtuber_bridge.py` | TTSEngine: tambah Coqui TTS backend |
| `tts_coqui.py` | NEW: ComfyUI HTTP client |
| `tts_edge.py` | NEW: extract existing edge_tts logic |
| CONFIG | Tambah `tts_backend`, `coqui_url`, `coqui_voice_prompt` |

---

## TESTING / VALIDATION

1. **ComfyUI running:** Web UI accessible from laptop 1
2. **Generation test:** Send text → receive WAV → play audio
3. **Emotion test:** Verify emotion prompt changes voice tone
4. **Latency test:** Measure TTS generation time (target < 30 detik untuk first gen, < 5 detik untuk subsequent)
5. **Integration test:** Full pipeline bridge → ComfyUI → TTS → VTS
6. **Fallback test:** Disconnect laptop 2 → bridge falls back to edge-tts

---

## RISKS & MITIGATIONS

| Risk | Mitigation |
|------|------------|
| Laptop 2 nggak punya GPU | Pakai CPU mode (lebih lambat ~2x) atau pakai model 0.6B |
| ComfyUI setup complex | Ikuti video tutorial step-by-step |
| Voice quality kurang cocok | Iterate on voice prompt / re-record training data |
| Network latency | Cache frequent phrases, pre-generate common responses |
| ComfyUI crash | Auto-restart via Docker, fallback to edge-tts |
| Model download gagal | Mirror ke ModelScope (China), manual download |

---

## OPEN QUESTIONS

1. **Laptop kedua spec-nya apa?** (RAM, GPU model, OS) — determines which model size (0.6B vs 1.7B)
2. **Bohan mau record suara sendiri?** — for voice clone mode (better quality)
3. **Mulai dengan Voice Design (text prompt) dulu atau langsung Voice Clone?** — text prompt lebih cepat, voice clone lebih personal
4. **Target kapan setup?** — butuh dedicated time ~3-5 jam

---

## ESTIMATED TIMELINE

| Task | Duration |
|------|----------|
| Check available voices list (Phase B) | 15-30 menit |
| Setup Docker + ComfyUI | 1-2 jam |
| Install Qwen-3 TTS custom node | 30 menit |
| Download models (~6GB) | 30 menit - 2 jam |
| Pick pre-built voice + tweak parameters (Phase C) | 1-3 jam |
| Iterasi & tuning | 1-2 jam |
| Integrasi ke bridge | 1 jam |
| Testing + fallback | 1 jam |
| **Total** | **~6-12 jam** |

---

*Plan saved. Ready for execution after laptop kedua spec confirmation.*
