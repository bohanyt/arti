# Smoke Test — Arti Emotion System

Manual gate before setting `expression_emotion_enabled` or `expression_nod_enabled` to `True` in CONFIG.

## Prerequisites

- VTS running with expressions: `ArtiMikir`, `ArtiBicara`, `ArtiAware`, `ArtiDefault1`, mood files (`ArtiSenyum`, `ArtiSedih`, `ArtiMarah`, `ArtiBingung`)
- Bridge on tag `v0.5.2-stable` or later with emotion commits
- `pytest tests/test_arti_wake.py tests/test_expression_runtime.py` green

## Checklist (7 items)

1. **PTT baseline** — `expression_emotion_enabled: False`, trigger PTT → Arti replies; idle resumes after TTS (`start_idle_animation`, not pause/resume).
2. **False trigger** — YouTube or wake ASR: `"berarti bang bohan ganteng"` does **not** queue Arti.
3. **Emotion tag** — Enable `expression_emotion_enabled: True`; reply includes mood; TTS does **not** speak `[EMOTION:...]`.
4. **Mood overlay** — With `senang` tag, `ArtiSenyum` overlays during `bicara`; lamp returns `default` after turn.
5. **Nod** — Enable `expression_nod_enabled: True`; head nods during TTS; `FaceAngleY` returns to 0 after.
6. **Turn lifecycle** — Sequence: aware → mikir → bicara (+mood) → default → idle; no stuck `mikir` or lamp.
7. **Rollback** — Set both CONFIG flags back to `False`; behavior matches pre-emotion baseline on next trigger.

## Sign-off

| Tester | Date | Pass? |
|--------|------|-------|
|        |      |       |
