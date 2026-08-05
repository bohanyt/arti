# 🤖 CURSOR HANDOFF — ARTI VTUBER AI CO-HOST

> **Read this ENTIRE document before making ANY changes.**
> Last updated: 4 Juni 2026 18:17 WIB
> Handoff from: Google Antigravity AI × Hermes Agent (planning + architecture + implementation)
> Handoff to: Cursor AI (continuation)

---

## 📌 TL;DR — What Is This Project?

**Arti** is an AI-powered VTuber co-host that:
- Listens to a streamer (Bohan) via microphone (ASR)
- Reads YouTube live chat in real-time
- Generates responses via LLM (Groq API)
- Speaks via TTS (Supertone 3 local / Edge TTS fallback)
- Animates a Live2D avatar in VTube Studio via WebSocket
- Shows subtitles as OBS browser overlay

The system is **v0.5 and working in production** — it was used in a live stream on June 1, 2026. **Do NOT break what works.** All changes are additive improvements.

---

## 📁 PROJECT FILE MAP

### Root: `C:\Users\<user>\Documents\hermes-vtuber-host\`

```
hermes-vtuber-host/
├── hermes_vtuber_bridge.py    ← 🔴 THE MAIN FILE (3616 lines, 174KB)
│                                  Everything runs from here.
│                                  Python 3.11 (venv)
│
├── supertone_engine.py        ← 🟡 Supertone TTS subprocess (425 lines)
│                                  Runs in Python 3.12 (venv312)
│                                  NDJSON protocol over stdin/stdout
│
├── text_preprocessor.py       ← 🟢 Indonesian number-to-words (14KB)
│                                  Converts "123" → "seratus dua puluh tiga"
│
├── subtitle.html              ← 🟡 OBS Browser Source overlay (354 lines)
│                                  WebSocket client, karaoke-style subtitles
│
├── subtitle_server.py         ← 🟢 WebSocket server for subtitles (112 lines)
│
├── ARTI_SOUL.md               ← 🟢 Arti's personality/system prompt seed
├── ARTI_MOOD_STATE.json       ← 🟢 Persisted mood state between sessions
├── ARTI_VIEWERS.md            ← 🟢 Known viewer database
├── vts_token.txt              ← 🔒 VTube Studio auth token
│
├── venv/                      ← Python 3.11 virtual environment (main bridge)
├── venv312/                   ← Python 3.12 virtual environment (Supertone only)
│
├── vault/                     ← Arti's memory vault
│   ├── concepts/              ← Topic-based knowledge files
│   └── sessions/              ← One .md per calendar day (see index.md)
│
├── docs/                      ← Plans, handoff, technical notes (not runtime)
│   ├── IMPLEMENTATION_PLAN.md
│   ├── CURSOR_HANDOFF_by_Antigravity_x_Hermes.md
│   ├── Expression-Motion-System.md
│   └── plans/                 ← Historical feature plans
│
├── session_logs/              ← Debug logs per bridge run (gitignored)
├── transcripts/               ← Full chat JSONL per session (gitignored)
├── archive/                   ← Old code, raw vault, Hermes IDE sessions
│   └── v0.4/
│       └── supertone_voice_samples/  ← Voice samples for F1-F5, M1-M5
│
├── tests/                     ← Unit tests (pytest)
├── templates/                 ← HTML templates
├── requirements.txt           ← Main venv dependencies
└── requirements-supertone.txt ← venv312 dependencies
```

### Planning & Progress Docs

```
Desktop/Arti-VTuber-Progress/
├── Expression-Motion-System.md     ← 🔴 DETAILED expression/animation system docs
├── Catatan_Perkembangan_Arti_VTuber.md
├── Ringkasan_Progres_Arti_VTuber.md  ← Massive progress recap
├── PLAN-v04-voice-upgrade.md
├── PLAN-v05-obs-subtitle.md
├── PLAN-v06-features.md            ← Feature plans
├── progress-recap-2026-05-30.md
└── progress-recap-2026-05-31.md
```

### Implementation Plan (THE SOURCE OF TRUTH)

```
C:\Users\<user>\.gemini\antigravity\brain\
  cde1be86-8e5a-449d-911d-ec498d06073b\
    implementation_plan.md    ← 🔴🔴🔴 THE MASTER PLAN (738 lines)
                                 READ THIS. Contains ALL bugs + features
                                 with detailed root cause analysis,
                                 code line references, and fix approaches.
```

> **CRITICAL**: Copy `implementation_plan.md` into `docs/` so Cursor can access it easily:
> ```
> copy "C:\Users\<user>\.gemini\antigravity\brain\cde1be86-8e5a-449d-911d-ec498d06073b\implementation_plan.md" "C:\Users\<user>\Documents\hermes-vtuber-host\docs\IMPLEMENTATION_PLAN.md"
> ```

---

## 🏗️ ARCHITECTURE OVERVIEW

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Microphone  │────▶│  ASR (Groq   │────▶│  LLM (Groq)  │
│  + Wake Word │     │  Whisper)    │     │  4-model      │
│              │     │  Cloud       │     │  rotation     │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                 │
┌─────────────┐     ┌──────────────┐             │
│  YouTube     │────▶│  Chat Parser │─────────────┤
│  Live Chat   │     │  (pytchat)   │             │
└─────────────┘     └──────────────┘             │
                                                 ▼
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  VTube       │◀───│  VTS Control │◀────│  TTS Engine  │
│  Studio      │     │  (WebSocket) │     │  Supertone/  │
│  (Live2D)    │     │  port 8002   │     │  Edge TTS    │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                 │
                    ┌──────────────┐              │
                    │  Subtitle    │◀─────────────┘
                    │  (WebSocket) │
                    │  port 9988   │
                    └──────────────┘
```

### Key Technical Details

| Component | Tech | Port/Protocol |
|-----------|------|--------------|
| Main bridge | Python 3.11 asyncio | — |
| Supertone TTS | Python 3.12 subprocess | stdin/stdout NDJSON |
| Edge TTS | `edge_tts` library | HTTPS (Microsoft) |
| VTube Studio | WebSocket client | ws://localhost:8002 |
| Subtitle server | WebSocket server | ws://localhost:9988 |
| ASR | Groq Whisper API | HTTPS |
| LLM | Groq API (4 models) | HTTPS |
| YouTube Chat | `pytchat` library | HTTPS polling |
| Audio output | `sounddevice` → VB-Cable | Virtual audio device |
| Audio input | `sounddevice` (mic) | Physical mic |

### LLM Model Rotation (Groq)
The bridge rotates through 4 models to handle rate limits:
1. `qwen3-32b` (primary)
2. `llama-4-scout-17b-16e-instruct` (multimodal capable!)
3. `llama-3.3-70b-versatile`
4. `llama-3.1-8b-instant` (fastest, fallback)

### Expression System (VTube Studio)
4 expression states managed via `.exp3.json` files:

| State | File | When |
|-------|------|------|
| `mikir` | `ArtiMikir.exp3.json` | Thinking (dots + lamp ON) |
| `bicara` | `ArtiBicara.exp3.json` | Speaking |
| `aware` | `ArtiAware.exp3.json` | Noticed streamer (push-to-talk) |
| `default` | `ArtiDefault1.exp3.json` | Idle state |

Plus 50 idle expressions (`ArtiIdle1-50.exp3.json`) for natural movement.

**IMPORTANT**: The "emblem" parameter (nametag lamp) is inside these expression files. If an expression sets emblem=1 but the next expression doesn't explicitly set emblem=0, the lamp stays stuck ON. See Bug 6 in the implementation plan.

---

## 📊 CURRENT STATE — What's Done vs What's Not

### ✅ DONE (v0.5 — Production Ready)
- Full ASR → LLM → TTS → VTS pipeline
- YouTube live chat reading + responses
- Supertone 3 local TTS with Edge TTS fallback
- 50 idle animations (expression + motion dual-track)
- OBS subtitle overlay with karaoke timing
- Push-to-talk (keyboard/mouse hotkey)
- Wake word detection ("Arti")
- Context buffer (50-item rolling history)
- Mood state persistence
- Session logging
- Viewer memory
- Expression tags (`<laugh>`, `<sigh>`, `<breath>`)
- Startup wizard (config validation)

### 🔴 NOT DONE — Bugs (in priority order)

| # | Bug | Effort | Status |
|---|-----|--------|--------|
| 7 | Supertone voice "rendah" — tweak `total_steps=8→10, speed=1.0→0.95` | **5 min** | NOT STARTED |
| 8 | Subtitle too long — show only current sentence | 1-2 jam | NOT STARTED |
| 5 | Event loop/thread leak — 73+ errors per session | 4-6 jam | NOT STARTED |
| 6 | Lamp/nametag stuck 45 min | 30 min-2 jam | NOT STARTED |
| 2 | Lip sync delay during motion | 2 jam | NOT STARTED |
| 3 | No latency logging — can't measure bottlenecks | 1 jam | NOT STARTED |
| 4 | Supertone timeout → Edge TTS fallback | 1-3 jam | NOT STARTED |

### 🔴 NOT DONE — Features (in priority order)

| # | Feature | Effort | Status |
|---|---------|--------|--------|
| 4 | Memory system fix (vault, session logs) | 3-4 jam | NOT STARTED |
| 7 | Read YouTube emotes | 1-2 jam | NOT STARTED |
| 10 | Smart model selection (8B for chat, 70B for complex) | 2 jam | NOT STARTED |
| 9 | Internet search (DuckDuckGo) | 3-4 jam | NOT STARTED |
| 6 | Proactive curiosity (Arti speaks unprompted) | 3-4 jam | NOT STARTED |
| 3 | Custom voice (Edge TTS → RVC pipeline) | 6-10 jam | NOT STARTED |
| 5 | Screen vision (Groq Vision, free) | 8-12 jam | NOT STARTED |
| 8 | Game playing (Minecraft bot) | 20-40 jam | NOT STARTED |
| 1 | Singing (RVC + MusicGen + DiffSinger) | 15-25 jam | NOT STARTED |
| 2 | AR (OBS composite) | 30 min | NOT STARTED |

---

## 🎯 WHAT TO DO FIRST (Sprint 0 + Sprint 1)

### Sprint 0: Instant Win (5 minutes)

**Bug 7 — Supertone voice quality tweak**

File: `hermes_vtuber_bridge.py`, lines 139-142

```python
# BEFORE:
"supertonic_speed": 1.0,
"supertonic_total_steps": 8,

# AFTER:
"supertonic_speed": 0.95,
"supertonic_total_steps": 10,
```

That's it. Save, restart bridge. Voice should sound richer/higher.

---

### Sprint 1: Stability Fixes

**Do these in order:**

#### 1. Bug 8 — Subtitle fix (subtitle.html ONLY)

The subtitle currently dumps ALL text at once. Fix: show only current sentence.

**File**: `subtitle.html` — **NO backend changes needed**

Changes:
- Lines 57-72: Simplify phrase CSS (remove `.said` class)
- Line 40: Bump font-size from 28px → 36px
- Lines 267-306: Replace phrase renderer — show only current phrase
- Lines 308-342: Replace word renderer — show only current sentence

The new JS for phrase mode (Supertone) should be:
```javascript
if (isPhraseMode) {
    words.forEach((phraseData, index) => {
        setTimeout(() => {
            subtitle.innerHTML = '';
            subtitle.className = '';
            const span = document.createElement('span');
            span.className = 'phrase active';
            span.textContent = phraseData.word;
            subtitle.appendChild(span);
        }, phraseData.start * 1000);
    });
    // Clear after last phrase
    const lastPhrase = words[words.length - 1];
    const clearDelay = (lastPhrase.start + lastPhrase.duration + 0.5) * 1000;
    clearTimer = setTimeout(() => {
        subtitle.classList.add('fade-out');
        setTimeout(() => { subtitle.innerHTML = ''; subtitle.className = ''; }, 500);
    }, clearDelay);
}
```

#### 2. Bug 5 — Event Loop Fix (BIGGEST stability issue)

**Problem**: `idle_timer_thread` (around line 2563-2907) spawns new threads with `asyncio.run()`, creating NEW event loops. This causes 73+ "different loop" errors and 103 "Task destroyed" warnings per session.

**Root cause lines**:
- `start_idle_animation()` creates a new `threading.Thread` with `asyncio.run(_idle_dual_track())`
- Each new thread creates a new event loop
- `asyncio.Lock` objects from old threads are bound to old loops
- WebSocket connections from old threads aren't closed

**Fix approach**:
- Reuse a single dedicated thread/loop for idle animations
- Properly close WebSocket connections when idle restarts
- Use `threading.Lock` instead of `asyncio.Lock` for cross-thread resources
- Kill old idle tasks before starting new ones

#### 3. Bug 6 — Lamp/Nametag Stuck

**Quick fix (no code)**: Open VTube Studio, edit `ArtiDefault1.exp3.json` and all `ArtiIdle1-50.exp3.json`, set "emblem" parameter explicitly to 0.

**Code fix**: In `trigger_expression_state("default")` (line 745-750), add explicit emblem parameter reset via `InjectParameterDataRequest`.

#### 4. Bug 2 — Lip Sync Delay

Set `tts_is_playing = True` EARLIER — when API call starts (line ~3078), not when audio plays (line 1326).

#### 5. Bug 3 — Latency Logging

Add `time.perf_counter()` measurements at each stage of the pipeline:
- ASR start → ASR end
- LLM call start → first token → complete
- TTS synthesis start → end
- Audio playback start → end

#### 6. Bug 4 — Supertone Resilience

- Increase timeout from 20s → 30s
- Add auto-retry once before fallback
- Log synthesis time per utterance

---

## ⚠️ CRITICAL RULES & GOTCHAS

### DO NOT:
1. **Break the main loop** (line 3065+). Everything hangs off this `while True` loop.
2. **Import asyncio in supertone_engine.py**. It runs in a separate Python 3.12 subprocess and must stay synchronous.
3. **Remove expression tag support** (`<laugh>`, `<sigh>`, `<breath>`). The `text_preprocessor.py` specifically preserves these tags during number preprocessing.
4. **Change VTS WebSocket port** (8002) or **subtitle port** (9988) without updating all references.
5. **Edit files while bridge is running during a live stream**. Wait for stream to end.
6. **Remove any existing comments/docstrings** unless directly related to your code change.

### WATCH OUT FOR:
1. **Two Python environments**: Main bridge = `venv` (3.11), Supertone = `venv312` (3.12). Don't mix them.
2. **CONFIG dict** (line ~100-150) is the central config. Many settings are runtime-changeable.
3. **`tts_is_playing` global** gates the microphone. If it gets stuck `True`, Arti goes deaf.
4. **`idle_expression_active` global** controls whether the lamp fallback fires. If stuck `True`, lamp won't reset.
5. **VTS error 1002** means "expression file not found" or race condition. The code handles this silently.
6. **The bridge is 3616 lines in a single file.** Use search, not scrolling.

### HOW TO RUN:
```powershell
cd "C:\Users\<user>\Documents\hermes-vtuber-host"
# Activate main venv
.\venv\Scripts\Activate.ps1
# Run bridge (Supertone subprocess auto-launches via venv312)
python hermes_vtuber_bridge.py
```

### HOW TO TEST:
```powershell
cd "C:\Users\<user>\Documents\hermes-vtuber-host"
.\venv\Scripts\Activate.ps1
pytest tests/ -v
```

---

## 🔑 KEY CODE LANDMARKS (hermes_vtuber_bridge.py)

| Line Range | What |
|-----------|------|
| 1-150 | CONFIG dict + imports |
| 150-230 | System prompt (Arti's personality) |
| 397-580 | Dynamic learning + vault integration |
| 580-720 | VTS WebSocket connection class |
| 722-750 | **`trigger_expression_state()`** — the 4-state expression machine |
| 755-768 | `resample_audio()` — sample rate conversion |
| 779-820 | Word boundary parsing (edge_tts timing) |
| 1075-1136 | `_split_into_phrases()` + `_estimate_phrase_timings()` |
| 1139-1360 | **TTSEngine class** — dual-engine TTS (Supertone + Edge TTS) |
| 1240-1260 | Supertone synthesis request (NDJSON) |
| 1261-1360 | `_play_wav()` — shared playback (subtitle + audio + mic gate) |
| 1362-1450 | Edge TTS path (`_speak_edge_tts`) |
| 1700-1850 | YouTube chat poller |
| 1850-2010 | Hotkey/push-to-talk registration |
| 2010-2130 | Voice listener worker (ASR) |
| 2563-2907 | **Idle animation system** (the buggy part — Bug 5) |
| 2661-2678 | Idle cleanup (stale expression reset) |
| 2681-2850 | Dual-track animation (motion + expression) |
| 3065-3318 | **MAIN LOOP** — the heart of everything |
| 3077-3078 | "mikir" expression trigger |
| 3265-3269 | "bicara" → "default" expression transition |
| 3280-3296 | Lamp fallback (the Bug 6 area) |
| 3320-3616 | Startup wizard |

---

## 📋 IMPLEMENTATION PLAN LOCATION

The full, detailed implementation plan with all bugs, features, root cause analysis, code references, comparison tables, and sprint plans is at:

```
PRIMARY (738 lines, most up-to-date):
C:\Users\<user>\.gemini\antigravity\brain\cde1be86-8e5a-449d-911d-ec498d06073b\implementation_plan.md

COPY THIS TO PROJECT ROOT:
C:\Users\<user>\Documents\hermes-vtuber-host\docs\IMPLEMENTATION_PLAN.md
```

**Run this command to copy it:**
```powershell
Copy-Item "C:\Users\<user>\.gemini\antigravity\brain\cde1be86-8e5a-449d-911d-ec498d06073b\implementation_plan.md" "C:\Users\<user>\Documents\hermes-vtuber-host\docs\IMPLEMENTATION_PLAN.md"
```

### Also reference:
- `Desktop/Arti-VTuber-Progress/Expression-Motion-System.md` — detailed docs on how all 50 idle expressions + 4 state expressions work, parameter names, VTS API calls
- `Desktop/Arti-VTuber-Progress/Ringkasan_Progres_Arti_VTuber.md` — massive progress recap
- `archive/v0.4/supertone_voice_guide.md` — Supertone 3 API reference, voice styles, expression tags

---

## 💰 COST CONSTRAINTS

**Everything must be FREE or use existing paid services:**
- ✅ Groq API — already have key, free tier
- ✅ Edge TTS — free (Microsoft)
- ✅ Supertone 3 — free (OpenRAIL-M, local)
- ✅ VTube Studio — free (with watermark)
- ❌ NO new paid subscriptions
- ❌ NO cloud services that cost money

All new features have been researched with $0 alternatives. See the implementation plan for details.

---

## 🎯 SUCCESS CRITERIA

After Sprint 0 + Sprint 1, the bridge should:
1. ✅ Zero "different loop" errors in a 5-minute test
2. ✅ Zero "Task destroyed" warnings
3. ✅ Lamp/nametag always turns off after speaking
4. ✅ Subtitle shows only current sentence (36px, yellow, readable)
5. ✅ Supertone voice sounds richer (steps=10, speed=0.95)
6. ✅ Latency numbers logged for each stage
7. ✅ Supertone timeout → retry once → then fallback

Good luck! 🚀
