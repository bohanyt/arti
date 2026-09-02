# Arti VTuber — Expression & Motion System (Full Technical Reference)
> **Last Updated:** 2026-06-01  
> **Status:** ✅ PRODUCTION — All systems working  
> **Bridge File:** `hermes_vtuber_bridge.py` (3616 lines)

---

## Table of Contents
1. [System Architecture Overview](#1-system-architecture-overview)
2. [Expression States (Emotion System)](#2-expression-states-emotion-system)
3. [Idle Animation System (2-Track)](#3-idle-animation-system-2-track)
4. [VTS Parameter Injection (Smooth Head Movement)](#4-vts-parameter-injection-smooth-head-movement)
5. [Motion Hotkeys (Body Movement)](#5-motion-hotkeys-body-movement)
6. [Expression Files (.exp3.json)](#6-expression-files-exp3json)
7. [Lifecycle & State Machine](#7-lifecycle--state-machine)
8. [Lamp Fallback System](#8-lamp-fallback-system)
9. [TTS Integration & Mic Gating](#9-tts-integration--mic-gating)
10. [Known Constraints & Lessons Learned](#10-known-constraints--lessons-learned)
11. [File Inventory](#11-file-inventory)

---

## 1. System Architecture Overview

Arti's animation runs on **3 independent layers** that work together:

```
┌─────────────────────────────────────────────────────────────────┐
│                    ARTI ANIMATION STACK                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Layer 3: EMOTION EXPRESSIONS (trigger_expression_state)        │
│  ├─ ArtiMikir.exp3.json    → "mikir" (brow raise, thinking)    │
│  ├─ ArtiBicara.exp3.json   → "bicara" (talking, lamp on)       │
│  ├─ ArtiAware.exp3.json    → "aware" (alert, focused)          │
│  └─ ArtiDefault1.exp3.json → "default" (neutral, nametag on)   │
│     Triggered by: main loop events (API call, TTS, toggle)     │
│                                                                 │
│  Layer 2: IDLE HEAD MOVEMENT (FaceAngle injection @ 10fps)      │
│  ├─ FaceAngleX (horizontal: left/right, -30 to 30)             │
│  ├─ FaceAngleY (vertical: up/down, -30 to 30)                  │
│  └─ FaceAngleZ (tilt: head tilt, -90 to 90)                    │
│     Source: 50 pose targets from ArtiIdle1-50.exp3.json files   │
│     Transition: 2.5s smoothstep interpolation                   │
│     Hold: 8-18 seconds per pose                                 │
│     Triggered by: background thread (_expression_track)         │
│                                                                 │
│  Layer 1: IDLE BODY MOTION (VTS Hotkey triggers)                │
│  ├─ IdleMotion1.motion3.json                                    │
│  ├─ IdleMotion2.motion3.json                                    │
│  ├─ IdleMotion3.motion3.json                                    │
│  ├─ IdleMotion4.motion3.json                                    │
│  └─ IdleMotion5.motion3.json                                    │
│     Interval: 25-40 seconds between triggers                   │
│     Triggered by: background thread (_motion_track)             │
│                                                                 │
│  Layer 0: VTS BASE (Lip sync, physics, breathing)               │
│  └─ Always active via VTube Studio's built-in systems           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Key principle:** All 3 layers are **additive** — they don't fight each other. Layer 2 uses `mode: "add"` for injection, meaning it ADDS to whatever Layer 1 is doing, creating natural compound movement.

---

## 2. Expression States (Emotion System)

**Code location:** `hermes_vtuber_bridge.py` lines 722-750 (`trigger_expression_state`)

The bridge has **4 mutually exclusive expression states** that control Arti's facial expression during conversations. Only ONE is active at a time — when activating one, the other 3 are deactivated.

### State Definitions

| State | Expression File | What It Looks Like | When Triggered |
|-------|----------------|-------------------|----------------|
| `"mikir"` | `ArtiMikir.exp3.json` | Brow raised, thoughtful look | When API call starts (Arti is "thinking") |
| `"bicara"` | `ArtiBicara.exp3.json` | Lamp/bohlam on, talking face | When AI reply is ready, before TTS plays |
| `"aware"` | `ArtiAware.exp3.json` | Alert, focused eyes, mendongak | When mouse toggle ON (Arti "hears" streamer) |
| `"default"` | `ArtiDefault1.exp3.json` | Neutral face, nametag visible | After TTS finishes, when toggle OFF |

### State Transition Flow

```
[User presses mouse X2]
  → stop_idle_animation()
  → trigger_expression_state("aware")      ← Arti looks alert

[User speaks → ASR transcribes]
  → trigger_expression_state("mikir")      ← Arti raises brow (thinking)

[API returns response]  
  → trigger_expression_state("bicara")     ← Lamp on, talking mode
  → tts.speak(reply)                       ← Audio plays with lip sync
  
[TTS finishes]
  → trigger_expression_state("default")    ← Back to neutral
  → start_idle_animation()                 ← Resume idle movement
  → _fallback_reset_lamp() scheduled       ← Safety reset after 5s
```

### Implementation Detail

Each state transition uses `send_expression()` which is a **fire-and-forget** pattern:
- Activate the target expression file
- Sleep 0.05s (tiny gap to prevent VTS race condition)
- Deactivate all other 3 expression files

```python
async def trigger_expression_state(self, state):
    if state == "mikir":
        await self.send_expression("ArtiMikir.exp3.json", True)
        await asyncio.sleep(0.05)
        await self.send_expression("ArtiBicara.exp3.json", False)
        await self.send_expression("ArtiDefault1.exp3.json", False)
        await self.send_expression("ArtiAware.exp3.json", False)
    # ... similar for other states
```

> **⚠️ Important:** These expression toggles are **instant** (no fade). VTS does NOT respect FadeInTime/FadeOutTime when toggling via API. This was confirmed through testing on 2026-06-01.

---

## 3. Idle Animation System (2-Track)

**Code location:** `hermes_vtuber_bridge.py` lines 2564-2936

The idle system creates **organic, lifelike movement** when Arti is not talking. It runs on a **dedicated background thread** with its own asyncio event loop and its own VTS websocket connection (separate from the main bridge websocket).

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  idle_animation_worker() — Background Thread (daemon)    │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  _idle_dual_track() — Main Async Entry Point      │   │
│  │                                                    │   │
│  │  1. Connect dedicated VTS websocket               │   │
│  │  2. Cleanup stale expressions from prev session   │   │
│  │  3. Discover motion hotkey IDs                    │   │
│  │  4. asyncio.gather(                               │   │
│  │       _motion_track(motion_ids),  ← Track 1       │   │
│  │       _expression_track(),        ← Track 2       │   │
│  │     )                                              │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  Shared: _idle_ws (websocket), _idle_ws_lock (Lock)      │
│  Control: idle_timer_running (bool), tts_is_playing      │
└─────────────────────────────────────────────────────────┘
```

### Configuration Constants

```python
# Track 1: Motion Hotkeys
IDLE_MOTION_HOTKEYS = ["IdleMotion1", ..., "IdleMotion5"]
MOTION_INTERVAL_MIN = 25   # seconds between motions
MOTION_INTERVAL_MAX = 40   # seconds between motions

# Track 2: Expression Poses (used as FaceAngle targets)
IDLE_EXPRESSIONS = [f"ArtiIdle{i}" for i in range(1, 51)]   # 50 poses
EXPR_HOLD_MIN = 8      # Hold each pose for 8-18 seconds
EXPR_HOLD_MAX = 18     # Longer holds = more natural
```

### Startup Sequence

1. **Connect** dedicated websocket to VTS (port 8002)
2. **Authenticate** using same token as main bridge
3. **Cleanup** — deactivate all 50 ArtiIdle expressions (prevents stuck poses from crash/restart)
4. **Discover** motion hotkey IDs by querying `HotkeysInCurrentModelRequest`
5. **Launch** both tracks concurrently via `asyncio.gather()`

### Pause/Resume Mechanism

Both tracks check `idle_timer_running` and `tts_is_playing` every cycle:
- When `tts_is_playing = True` → both tracks skip/pause
- When `idle_timer_running = False` → both tracks exit their loops
- FaceAngle injection values **naturally decay** when not being refreshed (VTS behavior), so the head smoothly returns toward neutral when idle pauses

---

## 4. VTS Parameter Injection (Smooth Head Movement)

**Code location:** `hermes_vtuber_bridge.py` lines 2780-2907 (`_expression_track`)

This is the **crown jewel** of the animation system. Instead of toggling expression files (which snap instantly), it **directly injects tracking parameter values** at 10fps with smooth interpolation.

### How It Works

```
ArtiIdle1.exp3.json     →  Read ParamAngleX=8, ParamAngleY=0, ParamAngleZ=2
                            ↓ (map to tracking params)
                         FaceAngleX=8, FaceAngleY=0, FaceAngleZ=2
                            ↓ (smoothstep interpolation over 2.5s)
                         InjectParameterDataRequest @ 10fps
                            ↓ (VTS applies to model)
                         Head smoothly turns right and tilts slightly
```

### Critical Discovery: Tracking vs Live2D Parameters

| Parameter Type | Example | Can Inject? | Notes |
|---------------|---------|-------------|-------|
| **Tracking params** | `FaceAngleX`, `FaceAngleY`, `FaceAngleZ` | ✅ YES | These are INPUT parameters that VTS maps to the model |
| **Live2D params** | `ParamAngleX`, `ParamAngleY`, `ParamAngleZ` | ❌ NO | VTS Error 453: "Only for tracking parameters" |

The expression files store values as `ParamAngleX/Y/Z`, but injection must use `FaceAngleX/Y/Z`. The code maps between them:

```python
PARAM_MAP = {
    "ParamAngleX": "FaceAngleX",   # Head horizontal (-30 to 30)
    "ParamAngleY": "FaceAngleY",   # Head vertical (-30 to 30)
    "ParamAngleZ": "FaceAngleZ",   # Head tilt (-90 to 90)
}
```

### Smoothstep Interpolation

Each transition uses a **smoothstep** curve (cubic Hermite — no jerk at start/end):

```python
def _smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)
```

- **Duration:** 2.5 seconds per transition
- **Frame rate:** 10 FPS (25 frames per transition)
- **Injection mode:** `"add"` (adds to existing tracking values, doesn't override)

### Hold & Decay Prevention

After reaching a target pose, the system **keeps injecting** the same values at 2fps (every 0.5s) to prevent VTS from decaying the parameter values back to neutral. This continues for 8-18 seconds (randomized) before transitioning to the next pose.

### Injection Payload Format

```json
{
    "apiName": "VTubeStudioPublicAPI",
    "apiVersion": "1.0",
    "requestID": "SmoothIdle",
    "messageType": "InjectParameterDataRequest",
    "data": {
        "faceFound": false,
        "mode": "add",
        "parameterValues": [
            {"id": "FaceAngleX", "weight": 1.0, "value": 5.2},
            {"id": "FaceAngleY", "weight": 1.0, "value": -2.1},
            {"id": "FaceAngleZ", "weight": 1.0, "value": 1.8}
        ]
    }
}
```

### Cleanup on Exit

When idle stops, the system fades back to neutral (0, 0, 0) over 1 second using the same smoothstep interpolation, so the head doesn't snap back.

---

## 5. Motion Hotkeys (Body Movement)

**Code location:** `hermes_vtuber_bridge.py` lines 2728-2777 (`_motion_track`)

Motions provide **body/torso movement** (sway, lean, shift) that complements the head movement from Track 2.

### How It Works

1. At startup, query VTS for available hotkeys via `HotkeysInCurrentModelRequest`
2. Match hotkey names against `IDLE_MOTION_HOTKEYS` list
3. Every 25-40 seconds, trigger a random motion via `HotkeyTriggerRequest`
4. VTS plays the `.motion3.json` animation file (inherently smooth — keyframe-based)

### VTS Hotkey IDs (Discovered at Runtime)

| Hotkey Name | VTS ID (changes per session) | Motion File |
|-------------|------------------------------|-------------|
| IdleMotion1 | `ec609c9f...` | `IdleMotion1.motion3.json` |
| IdleMotion2 | `fbd6182f...` | `IdleMotion2.motion3.json` |
| IdleMotion3 | `b8124395...` | `IdleMotion3.motion3.json` |
| IdleMotion4 | `393c4aaa...` | `IdleMotion4.motion3.json` |
| IdleMotion5 | `95ad6b59...` | `IdleMotion5.motion3.json` |

### Error Handling

- **VTS disconnected:** Reconnect and re-discover hotkeys
- **Hotkey execution failed:** Log error and skip (happens when VTS config windows are open)
- **No hotkeys found:** Motion track silently disabled; expression track still runs

---

## 6. Expression Files (.exp3.json)

All expression files live in:
```
C:\Program Files (x86)\Steam\steamapps\common\VTube Studio\VTube Studio_Data\StreamingAssets\Live2DModels\A_vts\
```

### File Categories

#### Emotion Expressions (4 files — used by trigger_expression_state)

| File | Purpose | Key Parameters |
|------|---------|----------------|
| `ArtiDefault1.exp3.json` | Neutral/default state | Param50=8 (nametag ON) |
| `ArtiMikir.exp3.json` | Thinking (brow raised) | Eyebrow up, eyes slightly narrowed |
| `ArtiBicara.exp3.json` | Talking (lamp bohlam on) | Lamp effect, mouth expression |
| `ArtiAware.exp3.json` | Alert/listening (focused) | Eyes wide, head slightly up |

#### Idle Pose Expressions (50 files — used as FaceAngle injection targets)

| File Range | Count | Purpose |
|-----------|-------|---------|
| `ArtiIdle1.exp3.json` — `ArtiIdle50.exp3.json` | 50 | Head angle targets for smooth injection |

Each file contains ParamAngleX/Y/Z values representing a head position. Examples:

| File | ParamAngleX | ParamAngleY | ParamAngleZ | Description |
|------|-------------|-------------|-------------|-------------|
| ArtiIdle1 | +8 | 0 | +2 | Nengok kanan sedikit |
| ArtiIdle5 | -6 | +3 | -1 | Noleh kiri, dongak sedikit |
| ArtiIdle14 | 0 | -4 | +3 | Nunduk, miring kanan |
| ArtiIdle30 | +12 | -2 | -2 | Nengok kanan lebih jauh |

#### Shared Parameter: Emblem/Nametag

All 50 ArtiIdle files contain:
```json
{"Id": "Param50", "Value": 8.0, "Blend": "Add"}
```
This ensures the "ARTI" nametag (校徽样式 Style of emblem) stays visible at all times. Added on 2026-06-01.

#### FadeIn/FadeOut Times

All 50 ArtiIdle files have been updated to:
```json
"FadeInTime": 2.5,
"FadeOutTime": 2.0
```

> **⚠️ Note:** These values are **NOT used** by the current system since we switched from expression toggles to parameter injection. They're kept in case we ever need to revert. Expression toggle via API always snaps instantly regardless of these values.

---

## 7. Lifecycle & State Machine

### Complete State Diagram

```
                    ┌──────────────────────┐
                    │     BRIDGE START      │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  IDLE (animating)     │◄──────────────────┐
                    │  - Track 1: motions   │                   │
                    │  - Track 2: head inj  │                   │
                    │  - Expression: default │                   │
                    └──────────┬───────────┘                   │
                               │                               │
                    [Mouse X2 pressed]                         │
                               │                               │
                    ┌──────────▼───────────┐                   │
                    │  AWARE (listening)    │                   │
                    │  - Idle: STOPPED      │                   │
                    │  - Expression: aware  │                   │
                    │  - ASR: active        │                   │
                    └──────────┬───────────┘                   │
                               │                               │
                    [Speech detected]                          │
                               │                               │
                    ┌──────────▼───────────┐                   │
                    │  THINKING (API call)  │                   │
                    │  - Expression: mikir  │                   │
                    │  - API: processing    │                   │
                    └──────────┬───────────┘                   │
                               │                               │
                    [API response received]                    │
                               │                               │
                    ┌──────────▼───────────┐                   │
                    │  TALKING (TTS playing)│                   │
                    │  - Expression: bicara │                   │
                    │  - tts_is_playing=T   │                   │
                    │  - Lip sync: active   │                   │
                    └──────────┬───────────┘                   │
                               │                               │
                    [TTS finished]                             │
                               │                               │
                    ┌──────────▼───────────┐                   │
                    │  AUTO-OFF            │───────────────────┘
                    │  - Expression: default│
                    │  - start_idle_anim() │
                    │  - _fallback_reset() │
                    └─────────────────────┘
```

### Cancel Flow (Double Toggle)

```
[Mouse X2 pressed AGAIN within <2s]
  → "DOUBLE TOGGLE - Bungkam!"
  → Cancel API task
  → Stop TTS (sd.stop())
  → Expression → default
  → Restart idle animation
```

---

## 8. Lamp Fallback System

**Code location:** `hermes_vtuber_bridge.py` lines 3281-3296

After TTS finishes and expression resets to "default", sometimes VTS fails to process the reset (error 1002 race condition). The fallback is a scheduled background task:

```python
async def _fallback_reset_lamp():
    await asyncio.sleep(5.0)                    # Wait 5 seconds
    if idle_expression_active:                   # If idle already took over...
        print("[Lamp Fallback] Skip — idle expression aktif.")
        return                                   # ...don't interfere
    await vts.trigger_expression_state("default") # Force reset
```

**Logic:** If idle animation is already running and injecting head poses, the fallback skips because the idle system's expression cleanup handles it. Otherwise, it force-resets to prevent the "lamp" (ArtiBicara) expression from getting stuck.

---

## 9. TTS Integration & Mic Gating

### tts_is_playing Flag

**Defined at:** line 311  
**Set to True:** Just before `sd.play()` starts audio playback (lines 1326, 1445)  
**Set to False:** After post-playback sleep (0.3s buffer for mic echo) (lines 1339, 1455)

This flag gates:
- **Track 1 (_motion_track):** Skips motion trigger when TTS playing
- **Track 2 (_expression_track):** Skips injection when TTS playing, which lets FaceAngle values decay back to neutral during speech

### Audio Pipeline

```
AI Reply → clean_ai_reply() → post_process_response()
  → trigger_expression_state("bicara")
  → tts.speak(reply)
    → Edge TTS (id-ID-GadisNeural) generates audio
    → Route to Virtual Cable (Device ID: 5)
    → VTS picks up audio for lip sync
    → sd.play() outputs to virtual cable
    → tts_is_playing = True
    → [Audio plays with lip sync]
    → sd.wait()
    → asyncio.sleep(0.3)  ← Echo prevention buffer
    → tts_is_playing = False
  → trigger_expression_state("default")
```

---

## 10. Known Constraints & Lessons Learned

### VTS API Constraints

| Constraint | Discovery Date | Impact |
|-----------|---------------|---------|
| `InjectParameterDataRequest` cannot inject into Live2D params (ParamAngleX) | 2026-06-01 | Must use tracking params (FaceAngleX) |
| Expression FadeInTime/FadeOutTime ignored via API | 2026-06-01 | Toggles always snap instantly |
| `ExpressionActivationRequest` is instant | 2026-06-01 | Cannot achieve smooth expression transitions via toggles |
| Hotkey execution fails when VTS config windows are open | Ongoing | Motion track logs error and retries |
| Websocket 1002 errors on race conditions | Ongoing | Solved with fire-and-forget pattern |

### Concurrency Constraints

| Issue | Solution |
|-------|----------|
| Idle thread uses separate event loop | `_idle_ws_lock` (asyncio.Lock) prevents websocket race |
| Old idle thread may still be alive on restart | `join(timeout=2.0)` then start new daemon thread anyway |
| `Task was destroyed but it is pending` on shutdown | Harmless warning from websockets keepalive — doesn't affect functionality |
| Cross-event-loop websocket access | Idle cleanup happens at startup (same event loop), not at stop time |

### Performance Notes

- **FaceAngle injection at 10fps** is sufficient — higher FPS shows no visible improvement
- **Hold injection at 2fps (0.5s interval)** prevents parameter decay without wasting bandwidth
- **50 expression poses** provide enough variety — head never looks repetitive
- **2.5 second transition** feels natural — faster looks robotic, slower looks laggy

---

## 11. File Inventory

### Bridge Code

| File | Lines | Purpose |
|------|-------|---------|
| `hermes_vtuber_bridge.py` | 3616 | Main bridge — all animation, TTS, ASR, API |

### VTS Model Directory

```
C:\Program Files (x86)\Steam\steamapps\common\VTube Studio\
  VTube Studio_Data\StreamingAssets\Live2DModels\A_vts\
```

| Files | Count | Purpose |
|-------|-------|---------|
| `ArtiDefault1.exp3.json` | 1 | Default/neutral state (nametag ON) |
| `ArtiMikir.exp3.json` | 1 | Thinking state (brow raised) |
| `ArtiBicara.exp3.json` | 1 | Talking state (lamp bohlam on) |
| `ArtiAware.exp3.json` | 1 | Alert/listening state |
| `ArtiIdle1.exp3.json` — `ArtiIdle50.exp3.json` | 50 | Head pose targets (FaceAngle injection source) |
| `IdleMotion1.motion3.json` — `IdleMotion5.motion3.json` | 5 | Body motion animations (keyframe-based) |

### Key Parameters

| VTS Parameter | Type | Range | Used For |
|--------------|------|-------|----------|
| `FaceAngleX` | Tracking | -30 to 30 | Head horizontal (left/right noleh) |
| `FaceAngleY` | Tracking | -30 to 30 | Head vertical (up/down dongak/nunduk) |
| `FaceAngleZ` | Tracking | -90 to 90 | Head tilt (miring kanan/kiri) |
| `Param50` | Live2D | 0 to 8 | Emblem/nametag (8 = ARTI nametag visible) |
| `EyeOpenLeft` | Tracking | 0 to 1 | Left eye openness |
| `EyeOpenRight` | Tracking | 0 to 1 | Right eye openness |
| `EyeLeftX/Y` | Tracking | -1 to 1 | Left eye gaze direction |
| `EyeRightX/Y` | Tracking | -1 to 1 | Right eye gaze direction |

### VTS Connection Details

| Connection | Port | Purpose |
|-----------|------|---------|
| Main bridge websocket | 8002 | Expression triggers, VTS control |
| Idle dedicated websocket | 8002 | FaceAngle injection, motion hotkeys |
| Subtitle server | 9988 | OBS subtitle overlay |

### Token & Auth

| File | Purpose |
|------|---------|
| `vts_token.txt` | VTS API authentication token (shared by both websockets) |
| Plugin name: `HermesVTuberBridge` | Registered in VTS |
| Plugin developer: `YourDeveloperName` | Registered in VTS |

---

## Appendix: Streamer Feedback (2026-06-01)

After implementing the smooth FaceAngle injection system, the streamer (streamer) gave live feedback:

> - "Eh bagus gerakannya" ✅
> - "...pelan gitu..." ✅  
> - "nggak snap" ✅
> - "Itu sangat tenang" ✅
> - "Bagus loh" ✅
> - **"Weeey, ini adalah motion ideal. Oh my god, ini terlihat sangat bagus"** ✅
> - "Oh my god, ini terlihat bagus" ✅

**Conclusion:** Smooth FaceAngle injection at 10fps with 2.5s smoothstep transitions successfully eliminated the "snappy/robotic" expression toggle behavior. The system is production-ready.
