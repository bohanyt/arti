# Plan: Supertone 3 TTS Upgrade — Arti VTuber Bridge

## Overview
Replace edge_tts with Supertone 3 as the TTS engine for Arti. Supertone 3 runs locally on CPU, supports 31 languages including Indonesian, has 10 built-in voices, and supports expression tags (`<laugh>`, `<breath>`, `<sigh>`).

## Current State (Done)
- ✅ Supertone 3 installed in venv312 (Python 3.12)
- ✅ Voice F1 selected (Female 1 — default, natural)
- ✅ Speed 1.0 confirmed best (higher = repetitive artifacts)
- ✅ Expression tags `<laugh>`, `<sigh>` confirmed working
- ✅ Number-to-words preprocessor (`text_preprocessor.py`) written and tested 10/11
- ✅ OBS subtitle integration complete (43/43 tasks, 35/35 tests)
- ✅ Startup wizard (pre-flight checklist)
- ✅ Debug session logger

## Architecture Decision: Subprocess Bridge
Supertone 3 requires Python 3.12 but project runs on Python 3.11 venv.
**Solution:** Run Supertone as a subprocess (Python 3.12 venv312), communicate via stdin/stdout JSON.

```
bridge.py (Python 3.11)
  → subprocess: venv312/python supertone_engine.py '{"text": "...", "voice": "F1"}'
  ← subprocess: {"wav_path": "temp_xxx.wav", "duration": 3.2}
  → sd.play(wav_path)  # plays through Virtual Cable → VTS
```

This avoids:
- Python version conflicts
- asyncio blocking (Supertone synthesize is synchronous/CPI intensive)
- Complex dependency management

## Files to Create
1. `supertone_engine.py` — Subprocess TTS server (Python 3.12, runs in venv312)
   - Reads JSON from stdin: `{"text": "...", "voice": "F1", "speed": 1.0}`
   - Returns JSON to stdout: `{"wav_path": "...", "duration": 3.2}`
   - Preprocesses numbers via `text_preprocessor.py` before synthesis
   - Handles expression tags natively (no preprocessing needed)
   - Cleans up temp WAV files after playback

## CONFIG Keys to Add
```python
"tts_engine": "supertone",          # "supertone" or "edge_tts"
"tts_preprocess_numbers": True,      # number-to-words preprocessing
"supertonic_voice": "F1",           # voice style: M1-M5, F1-F5
"supertonic_speed": 1.0,             # speed/pitch: 0.7-2.0
"supertonic_lang": "id",             # language code
"supertonic_total_steps": 8,         # quality: 5-12
```

## Integration Points in bridge.py
1. **TTSEngine.speak()** — Modified to support both engines:
   - If `tts_engine == "supertone"`: spawn subprocess, read JSON, play WAV
   - If `tts_engine == "edge_tts"`: existing implementation (fallback)

2. **Number Preprocessor** — `text_preprocessor.integrate()` before send to TTS:
   - `"Rp 10.000"` → `"sepuluh ribu rupiah"`
   - `"15/06/2026"` → `"lima belas Juni dua ribu dua puluh enam"`
   - Preserves expression tags: `<laugh>`, `<breath>`, `<sigh>` untouched

3. **Subtitle Integration** — Keep existing word boundary extraction for edge_tts path
   - Supertone path: empty word boundaries (no timing metadata from Supertone)
   - Subtitle still works but shows full text (not word-by-word)

## Expression Tags Strategy
Arti personality contexts and suggested tags:
| Context | Tag | Example |
|---|---|---|
| Happy/Excited | `<laugh>` | "<laugh> Halo guys!" |
| Sad/Emotional | `<sigh>` | "Yaelah... <sigh>" |
| Surprised | `<breath>` | "Hah? <breath> Gitu?" |
| Thinking | (none) | Use VTS ArtiMikir expression instead |

Tags are injected by the LLM in responses (LLM learns from examples in system prompt).

## Adding Expression Examples to System Prompt
Add to `_SYSTEM_PROMPT_BASE` in bridge.py:
```
[EXPRESSION TAGS]
Kamu bisa pakai tag suara spontan di jawabanmu:
- <laugh> = tawa (kalau lucu atau bercanda)
- <sigh> = henap napas (kalau sedih atau capek)
- <breath> = tarik napas (kalau kaget atau terkesan)
Gunakan secara natural dan jangan berlebihan. Max 1-2 tag per jawaban.

Contoh: "<laugh> Dasar Bohan..." atau "Hmm... <sigh> bingung aku."
```

## Implementation Steps
1. Create `supertone_engine.py` (subprocess server)
2. Update `TTSEngine.speak()` in bridge.py (dual-engine support)
3. Integrate number preprocessor
4. Add expression tag examples to system prompt
5. Update CONFIG dict with new keys
6. Add `--no-wizard` flag to startup_wizard()
7. Tests: number preprocessing, expression tags, fallback to edge_tts

## Risk Mitigation
- **Fallback:** If Supertone subprocess fails, auto-fallback to edge_tts
- **Performance:** Supertone subprocess keeps model loaded — no cold start per utterance
- **Cleanup:** Temp WAV files deleted after playback (max 3 temp files at a time)
- **Version:** Pin `supertonic==1.3.1` in requirements

## Pending
- [ ] Confirm `<breath>` tag behavior (not yet tested)
- [ ] Test with VTS lipsync (Virtual Cable → VTS expression trigger)
- [ ] Language model integration (LLM generates expression tags natively)
