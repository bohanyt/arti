# PLAN v0.3 — Arti VTuber Improve: Real-Time Speech + Emotion Expression

> Created: 2026-05-28
> Status: PLANNING (no execution yet)
> Backup: v0.1 stable state already committed (see below)

---

## GOAL

1. **Backup** current stable code as v0.1
2. **Reduce speech delay** — Bohan toggle ON → ngomong → Arti respon < 2 detik
3. **Cancel/Interrupt** — Toggle OFF langsung cancel API call + stop TTS
4. **Emotion detection** → Arti nangkep emosi Bohan → ubah expression VTS
5. **Context optimization** — Fix Groq TPM limit, lebih relevan

---

## CURRENT CONTEXT

### Yang udah jalan (stable):
- `hermes_vtuber_bridge.py` — main script, ASR + YouTube chat + TTS + LLM
- `ARTI_SOUL.md` — personality definition
- `ARTI_VIEWERS.md` — viewer tracker
- `ARTI_MOOD_STATE.json` — mood state
- VTS efek titik-3 (mikir) + lampu bohlam (ngomong) — udah jalan!
- Hermes Vault integration (memory + session log)
- Watermark off, default expression ArtiDefault1

### Yang bermasalah:
- Delay speech 5-10 detik (ASR → LLM → TTS pipeline terlalu panjang)
- Tidak bisa cancel kalau toggle OFF
- Bohan ngomong panjang, yang ke-LLM cuma transkrip terakhir
- Emotion/expression belum ada
- Groq TPM limit kadang crash (413 Request too large)
- Context 50 history penuh sama transkrip pasif nggak penting

---

## STEP-BY-STEP PLAN

### Phase 0: Backup v0.1 (STABLE)

**Tujuan:** Simpan snapshot kode yang sekarang (pre-v0.2 patch) sebagai backup.

```
1. Git init di hermes-vtuber-host (kalau belum)
2. Git add semua file
3. Git commit -m "v0.1-stable: pre-real-time-improvements"
4. Git tag v0.1.0
```

**Files:** Semua file di `hermes-vtuber-host/`

---

### Phase 1: Cancel/Interrupt System

**Tujuan:** Toggle OFF langsung cancel API call + stop TTS.

**Approach:**
- Bungkus API call ke `asyncio.Task`
- Toggle OFF → `task.cancel()` + `sd.stop()` (stop TTS)
- Bohan bisa toggle ON lagi tanpa nunggu

**Files yang berubah:**
- `hermes_vtuber_bridge.py` — main_loop, TTS interrupt

**Steps:**
1. Buat global `current_api_task = None`
2. Di main_loop, bungkus LLM call: `current_api_task = asyncio.create_task(call_llm())`
3. Toggle OFF handler: 
   - if `current_api_task` and not done → `current_api_task.cancel()`
   - `sd.stop()` untuk stop TTS langsung
   - Clear voice_trigger_queue
4. Toggle ON handler: clear queue, mulai listen lagi
5. Error handling: `asyncio.CancelledError` → print "[Interrupted]"

---

### Phase 2: Dual-API Architecture (Main + Summarizer)

**Tujuan:** Reduce delay dari 5-10s ke < 2s.

**Approach:**
```
API 1 — "RESPONDER" (cepat, real-time)
- Model: llama-3.1-8b-instant (Groq) — super cepat
- Input: HANYA trigger terakhir (1-2 kalimat)
- Output: Jawaban pendek 1-2 kalimat
- Delay target: < 2 detik

API 2 — "SUMMARIZER" (berat, berkala)
- Model: owl-alpha (OpenRouter) via hermes
- Input: 5-10 trigger terakhir + emotion context
- Output: Ringkasan konteks + emotion label
- Dipanggil: Setiap 5 trigger atau 30 detik idle
- Hasil: Inject ke memory + update emotion state
```

**Files yang berubah:**
- `hermes_vtuber_bridge.py` — tambah `call_summarizer()`, `build_responder_prompt()`
- `ARTI_SOUL.md` — tambah emotion detection rules

**Steps:**
1. Pisahkan prompt jadi 2: `RESPONDER_PROMPT` (singkat) dan `SUMMARIZER_PROMPT` (detail)
2. Responder: cuma kirim last trigger + 5 history terakhir
3. Summarizer: jalan di background thread, setiap 5 trigger
4. Summarizer output → update `ARTI_MOOD_STATE.json` + memory
5. Responder pakai model tercepat (llama-3.1-8b-instant)

---

### Phase 3: Emotion Detection + Expression Mapping

**Tujuan:** Arti nangkep emosi Bohan → ubah expression VTS.

**Approach:**
```
Emotion Detection (dari Summarizer):
- Bohan bilang "sedih", "galau", "down" → emotion: sad
- Bohan bilang "gila", "anjir", "wkwk", "haha" → emotion: happy
- Bohan bilang "marah", "kesel", "bangsat" → emotion: angry
- Bohan bilang "bingung", "gimana ya", "idk" → emotion: confused
- Default: neutral

VTS Expression Mapping:
- happy → senyum (ekspresi mata + mulut)
- sad → sedih (ekspresi mata + alis)
- angry → marah (ekspresi alis + mulut)
- confused → bingung (mata + kepala miring)
- neutral → default (ArtiDefault1)
```

**Files yang berubah:**
- `hermes_vtuber_bridge.py` — tambah `detect_emotion()`, `set_vts_expression()`
- `ARTI_MOOD_STATE.json` — tambah emotion field
- VTube Studio — buat ekspresi per emotion (happy/sad/angry/confused)

**Steps:**
1. Tambah emotion keywords detection di Summarizer
2. Setiap response, detect emotion dari trigger text
3. Update `ARTI_MOOD_STATE.json` dengan emotion label
4. Map emotion → VTS expression name
5. Call VTS Expression API untuk ganti expression
6. Expression revert ke default setelah 10 detik idle

---

### Phase 4: Context Window Optimization

**Tujuan:** Fix Groq TPM limit, context lebih relevan.

**Approach:**
```
- Dari 50 history → 20 history (yang relevan)
- Pisahkan: streamer speech vs viewer chat vs Arti response
- Prioritaskan streamer speech terakhir
- Viewer chat: ringkas jadi 1 line per viewer
- Token usage: ~40% lebih kecil
```

**Files yang berubah:**
- `hermes_vtuber_bridge.py` — `stream_history` logic, prompt builder

**Steps:**
1. Ubah `stream_history` dari single deque jadi categorized:
   - `streamer_speech` (deque maxlen=10)
   - `viewer_chat` (deque maxlen=5 per viewer)
   - `arti_responses` (deque maxlen=5)
2. Build prompt: streamer speech dulu, lalu viewer summary, lalu Arti responses
3. Estimated token: ~2000 token (dari ~3500)

---

### Phase 5: Streaming ASR (Advanced, Optional)

**Tujuan:** Partial transcription → LLM mulai generate sebelum Bohan selesai.

**Approach:**
```
- Whisper partial output setiap 1-2 detik
- Kirim partial ke LLM sebagai "preview"
- LLM mulai generate jawaban
- Kalau Bohan masih ngomong → update partial
- Kalau Bohan selesai → finalize jawaban
- Delay target: < 1 detik dari akhir bicara
```

**Files yang berubah:**
- `hermes_vtuber_bridge.py` — `voice_listener_worker` ASR logic

**RISK:** Complex, perlu riset Whisper partial output API. Skip dulu kalau Phase 1-4 udh cukup.

---

## FILES YANG BERUBAH

| File | Change |
|------|--------|
| `hermes_vtuber_bridge.py` | Main: cancel system, dual-API, emotion, context optimization |
| `ARTI_SOUL.md` | Tambah emotion detection rules |
| `ARTI_MOOD_STATE.json` | Tambah emotion field |
| VTS Expressions | Buat ekspresi per emotion |

---

## TESTING / VALIDATION

1. **Cancel test:** Toggle ON → ngomong → toggle OFF → harus langsung stop
2. **Delay test:** Toggle ON → ngomong "halo arti" → measure waktu respon
3. **Emotion test:** Bilang "aku sedih" → cek VTS expression berubah
4. **Context test:** Ngobrol 10 menit → cek nggak crash TPM
5. **Stability test:** Stream 1 jam → cek memory leak, queue overflow

---

## RISKS & TRADEOFFS

| Risk | Mitigation |
|------|------------|
| Dual-API complexity | Start with cancel system dulu, then add dual-API |
| Emotion detection akurasi | Fallback ke neutral kalau nggak yakin |
| VTS expression belum ada | Buat ekspresi dulu di VTS sebelum code |
| Streaming ASR complex | Skip dulu, Phase 1-4 dulu |

---

## OPEN QUESTIONS

1. Summarizer API — owl-alpha (OpenRouter) atau model lain?
2. VTS emotion expressions — udh ada atau perlu bikin baru?
3. Streaming ASR — mau di-try atau skip dulu?
4. Backup — git init + commit, atau zip manual?

---

## EXECUTION ORDER

1. **Phase 0:** Backup v0.1 (git init + commit + tag)
2. **Phase 1:** Cancel/Interrupt System
3. **Phase 2:** Dual-API Architecture
4. **Phase 3:** Emotion Detection + Expression
5. **Phase 4:** Context Optimization
6. **Phase 5:** Streaming ASR (optional, last)

---

*Plan saved. Ready for execution after user approval.*
