# PLAN v0.6 — Feature Deep Dive: Fitur-Fitur yang Bisa Diterapin

> Created: 2026-05-28
> Status: PLANNING (no execution yet)
> Source: Deep dive into hermes_vtuber_bridge.py (1,893 lines) + all existing plans

---

## CURRENT STATE ANALYSIS

### Yang udh jalan (v0.3.0):
1. ✅ ASR Mic (Whisper Groq + local fallback) — push-to-talk + wake word
2. ✅ LLM Response (Groq rolling 4-model + OpenRouter fallback)
3. ✅ TTS Output (edge-tts GadisNeural)
4. ✅ VTS Expression API (Play Expression via WebSocket)
5. ✅ YouTube Chat (Innertube API polling)
6. ✅ Summarizer (owl-alpha via OpenRouter, setiap 5 trigger)
7. ✅ Emotion Detection → Mood Mapping
8. ✅ Cancel/Interrupt System
9. ✅ Context Optimization (categorized history)
10. ✅ 16 exp3.json Templates
11. ✅ Hermes Vault Integration (memory + session logs)
12. ✅ Hermes Soul System (ARTI_SOUL.md, ARTI_VIEWERS.md, ARTI_MOOD_STATE.json)

### Yang belum ada / bisa di-improve:

---

## FEATURE 1: Fix Mic Toggle + Priority (HIGH PRIORITY)

### Problem:
- Toggle kadang mati sendiri
- Delay 3+ detik sebelum bisa ngomong
- Harus trigger kata "arti" dulu
- Mic vs chat bisa bentrok

### Solution:
```
1. Fix hotkey_active state — persistent, nggak ke-reset
2. Kurangi silence_duration: 1.2s → 0.6s
3. Push-to-talk mode: toggle ON = SEMUA suara langsung kirim (no keyword)
4. Priority queue: mic (HIGH) > chat (LOW)
5. Visual indicator pas toggle ON/OFF
```

### Implementation:
- File: `hermes_vtuber_bridge.py`
- Lines to modify: 1137-1149 (trigger logic), 1120-1131 (silence detection)
- Estimated: 1-2 hours

---

## FEATURE 2: OBS Subtitle Word-by-Word (HIGH PRIORITY)

### Problem:
- Belum ada subtitle/caption pas Arti ngomong
- Butuh visual feedback buat viewer

### Solution:
```
1. Extract word boundaries dari edge_tts (native support!)
2. WebSocket server → Browser Source di OBS
3. JS render per-kata dengan timing
4. 3 style options: classic, VTuber, minimal
```

### Implementation:
- Files: `subtitle_server.py` (NEW), `subtitle.html` (NEW)
- Bridge modification: TTSEngine return word_timings
- Estimated: 4-5 hours

---

## FEATURE 3: Idle Animation System with RNG (MEDIUM PRIORITY)

### Problem:
- Arti pas diem nggak gerak — kayak statue
- Butuh idle animation random kayak Genshin Impact character

### Solution:
```
1. Idle Timer Thread — jalan di background
2. RNG timer: setiap 5-15 detik, 30-50% chance gerak
3. Random pilih dari 4 idle expressions:
   - Idle1: celingak kanan
   - Idle2: celingak kiri
   - Idle3: lihat atas
   - Idle4: lihat bawah/merenung
4. Hold duration: 2-4 detik per pose
5. Return to default setelah idle selesai
```

### Implementation:
- File: `hermes_vtuber_bridge.py`
- Tambah `idle_timer_thread()` function
- Config: `IDLE_CHECK_INTERVAL`, `IDLE_ACTION_CHANCE`, `IDLE_HOLD_DURATION`
- Expressions: ArtiIdle1-4.exp3.json (udh ada di templates/)
- Estimated: 2-3 hours

---

## FEATURE 4: Eye Tracking — Follow Mouse (MEDIUM PRIORITY)

### Problem:
- Mata Arti nggak ngikutin kursor mouse
- Kayak Genshin character yang liat ke mana mouse pointing

### Solution:
```
1. Mouse position thread — baca cursor position real-time
2. Convert mouse X/Y → ParamEyeBallX/Y values
3. Smooth movement (nggak kaku)
4. Optional: head ikut gerak sedikit (ParamAngleX/Y)
```

### Implementation:
- File: `hermes_vtuber_bridge.py`
- Tambah `mouse_tracker_thread()` function
- Use `pyautogui` atau `win32api` untuk baca mouse position
- Convert: mouse X (-1 to 1) → ParamEyeBallX (-1 to 1)
- Config: `FOLLOW_MOUSE_INTERVAL`, `FOLLOW_MOUSE_SMOOTH`
- Estimated: 2-3 hours

---

## FEATURE 5: Offline State Detection (LOW PRIORITY)

### Problem:
- Pas bridge mati/crash, Arti tetep muncul di VTS
- Nggak ada "offline" state

### Solution:
```
1. Pas bridge startup → load ArtiDefault1 expression
2. Pas bridge shutdown → load ArtiOffline expression
3. VTS hide/show model via API
```

### Implementation:
- File: `hermes_vtuber_bridge.py`
- Tambah `vts.show_model()` dan `vts.hide_model()` methods
- Expression: ArtiOffline.exp3.json (udh ada)
- Estimated: 1 hour

---

## FEATURE 6: Multi-Language TTS (v0.4 — Voice Upgrade)

### Problem:
- edge-tts GadisNeural terlalu flat/basic
- Karakter Arti butuh suara yang lebih "hidup"

### Solution:
```
1. Qwen-3 TTS via ComfyUI di laptop kedua
2. Pre-built voices + tweak parameters (pitch, speed, Hz)
3. Voice cloning dari suara Bohan (etis)
4. Emotion-aware TTS — tone sesuai mood
```

### Research Summary:
- 9 pre-built voices diketahui: 3 Chinese, 2 English, 1 Japanese, 1 Korean, 2 Chinese dialects
- Other languages (Indonesia?) — perlu verify
- Model: 0.6B (<2GB) atau 1.7B (<4GB)
- ComfyUI integration available

### Implementation:
- Laptop kedua: Docker + ComfyUI + Qwen-3 TTS
- Bridge: HTTP API call ke ComfyUI
- Estimated: 6-12 hours

---

## FEATURE 7: Emotion-Aware TTS (POST VOICE UPGRADE)

### Problem:
- Suara Arti monotone — nggak sesuai mood/konteks

### Solution (setelah Voice Upgrade):
```
1. Hasil emotion dari summarizer → voice prompt modifier
2. Mapping:
   - Senang → "happy, upbeat, energetic"
   - Sedih → "sad, soft, slow"
   - Marah → "angry, forceful, loud"
   - Excited → "excited, energetic, fast"
   - Feisty → "sassy, bold, confident"
   - Lazy → "relaxed, slow, casual"
3. Inject voice prompt ke TTS request
```

### Implementation:
- Modify TTSEngine.voice_prompt based on current_mood
- Config: emotion_to_voice_prompt mapping
- Estimated: 30 menit (setelah Voice Upgrade done)

---

## FEATURE 8: Facial Expression Sync with Mood (LOW PRIORITY)

### Problem:
- Mood berubah tapi ekspresi wajah Arti nggak ngikutin
- Butuh auto-switch expression berdasarkan mood

### Solution:
```
1. Pas mood berubah → trigger VTS expression
2. Mapping:
   - Happy → ArtiSenyum
   - Sad → ArtiSedih
   - Angry → ArtiMarah
   - Confused → ArtiBingung
   - Excited → ArtiExcited
   - Lazy/Fiesty → ArtiDefault1
3. Transition smooth (nggak langsung switch)
```

### Implementation:
- Hook di `set_mood()` function
- Call `vts.play_expression(mood_to_expression[mood])`
- Config: mood_to_expression mapping
- Estimated: 1 hour

---

## FEATURE 9: Sound Effects System (NICE TO HAVE)

### Problem:
- Arti nggak punya sound effects (SFX)
- Butuh feedback sounds (notif, alert, dll)

### Solution:
```
1. Tambah sound effects (WAV files):
   - Notif sound saat ada chat masuk
   - Error sound saat LLM gagal
   - Intro sound pas Arti muncul
2. Play via sounddevice ke Virtual Cable
3. Config: enable/disable per SFX
```

### Implementation:
- Folder: `assets/sfx/`
- Config: SFX settings di CONFIG
- Estimated: 1-2 hours

---

## FEATURE 10: Viewer Interaction System (NICE TO HAVE)

### Problem:
- Viewer cuma bisa chat, nggak ada interaksi khusus
- Butuh sistem engagement (polls, commands, dll)

### Solution:
```
1. Chat commands:
   - !mood → tampilkan mood Arti saat ini
   - !topic → tampilkan topik pembicaraan
   - !fact → tampilkan fakta menarik dari memory
   - !roast [nama] → Arti nge-roast viewer
2. Superchat/membership detection
3. Viewer greeting (welcome message)
```

### Implementation:
- Parse chat commands di `process_message()`
- Superchat detection via YouTube API
- Estimated: 2-3 hours

---

## FEATURE 11: Lyrics/Text Display (NICE TO HAVE)

### Problem:
- Butuh display lirik/text yang lebih visual
- Bisa untuk intro, outro, atau quotes

### Solution:
```
1. HTML overlay untuk OBS Browser Source
2. Display:
   - Arti's last response
   - Current mood
   - Current topic
   - Viewer count
   - Chat highlights
3. Custom styling (font, color, animation)
```

### Implementation:
- File: `overlay.html` (NEW)
- WebSocket untuk real-time updates
- Estimated: 2-3 hours

---

## FEATURE 12: Session Replay / Highlight System (NICE TO HAVE)

### Problem:
- Nggak ada cara replay moment-moment seru
- Butuh highlight clips

### Solution:
```
1. Record session highlights (audio + chat + expressions)
2. Save ke folder dengan timestamp
3. Replay via simple HTML player
4. Export ke video (optional)
```

### Implementation:
- Tambah recording flag di bridge
- Save session data ke JSON
- HTML player untuk replay
- Estimated: 3-4 hours

---

## PRIORITY ORDER

| Priority | Feature | Est. Time |
|----------|---------|-----------|
| 🔴 HIGH | Fix Mic Toggle + Priority | 1-2 jam |
| 🔴 HIGH | OBS Subtitle Word-by-Word | 4-5 jam |
| 🟡 MEDIUM | Idle Animation System (RNG) | 2-3 jam |
| 🟡 MEDIUM | Eye Tracking (Follow Mouse) | 2-3 jam |
| 🟢 LOW | Offline State Detection | 1 jam |
| 🟢 LOW | Facial Expression Sync | 1 jam |
| 🔮 FUTURE | Voice Upgrade (Qwen-3 TTS) | 6-12 jam |
| 🔮 FUTURE | Emotion-Aware TTS | 30 menit |
| 🔮 FUTURE | Sound Effects System | 1-2 jam |
| 🔮 FUTURE | Viewer Interaction System | 2-3 jam |
| 🔮 FUTURE | Lyrics/Text Display | 2-3 jam |
| 🔮 FUTURE | Session Replay | 3-4 jam |

---

## RECOMMENDED EXECUTION ORDER

### Sprint 1 (Quick Wins — ~8 jam):
1. Fix Mic Toggle + Priority
2. Offline State Detection
3. Idle Animation System (RNG)
4. Facial Expression Sync

### Sprint 2 (Core Features — ~10 jam):
5. OBS Subtitle Word-by-Word
6. Eye Tracking (Follow Mouse)
7. Sound Effects System

### Sprint 3 (Advanced — ~15 jam):
8. Voice Upgrade (Qwen-3 TTS)
9. Emotion-Aware TTS
10. Viewer Interaction System
11. Lyrics/Text Display
12. Session Replay

---

## FILES YANG BERUBAH/DIBUAT

| File | Change |
|------|--------|
| `hermes_vtuber_bridge.py` | Fix mic, idle timer, mouse tracker, expression sync |
| `subtitle_server.py` | NEW: WebSocket server |
| `subtitle.html` | NEW: Browser Source subtitle |
| `overlay.html` | NEW: OBS overlay (future) |
| `assets/sfx/` | NEW: Sound effects folder |
| CONFIG | Tambah idle, mouse, SFX settings |

---

## OPEN QUESTIONS

1. **Idle animation frequency?** 5-15 detik check, 30-50% chance — pas nggak?
2. **Mouse tracking smooth?** 0.3 smooth factor — terlalu cepat/lambat?
3. **Subtitle style?** Classic, VTuber, atau minimal?
4. **Voice upgrade timeline?** Kalo laptop kedua belum ready, skip dulu?
5. **SFX sounds?** Bohan mau record sendiri atau pakai free sounds?

---

*Plan saved. Ready for execution!*
