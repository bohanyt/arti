# PLAN: Bridge-Triggered Idle Motion System (v0.5)

> Created: 2026-05-31
> Status: PLAN ONLY — no code changes yet

---

## 🎯 Goal

Migrasi idle animation dari **expression-only** (50 ArtiIdleX sequential → kaku) ke **dual-track system**:
- **Track 1 — Motion**: Bridge triggers `.motion3.json` via VTS Hotkey (body/head/eye movement)
- **Track 2 — Expression**: Existing 50 ArtiIdleX.exp3.json (micro-expression/face)

Hasil: idle yang lebih natural — body gerak + wajah ekspresif.

---

## 📊 Motion Files Analysis

### File: `idle1.motion3.json` (1000KB)
- **Duration: 9.99s** | Loop: True | FPS: 60
- **309 parameter curves** | 30,256 total segments
- **Key movements:**
  - `ParamAngleX/Y/Z` — head rotation (93-177 segments, smooth)
  - `ParamBodyAngleX/Y` — body lean (93-135 segments)
  - `ParamEyeBallX/Y` — eye movement (114-198 segments)
  - `ParamEyeLOpen/ROpen` — eye open/close/blink (261 segments)
  - `ParamBrowLY/RY/LForm/RForm` — eyebrow movement (191-212 segments)
  - `ParamMouthForm` — mouth shape (51 segments)
  - `ParamBreath` — breathing (30 segments, 0→0.9)
  - `ParamHairFront/Side` — hair physics (23-261 segments)
  - Many `Param_Angle_Rotation*` — hair/cloth physics (44-338 segments each)
- **Character:** Full-body idle with breathing, head movement, eye movement, blinking, hair physics

### File: `idle2.motion3.json` (1133KB)
- **Duration: 9.99s** | Loop: True | FPS: 60
- **309 parameter curves** | 34,351 total segments
- Same parameter set as idle1, different motion data (more segments = more complex movement)

### File: `idle3.motion3.json` (1276KB)
- **Duration: 10.00s** | Loop: True | FPS: 60
- **309 parameter curves** | 38,915 total segments
- Same parameter set, most complex motion

### Legacy files (1.motion3.json, 2.motion3.json, 3.motion3.json)
- **Duration: 0.5-0.83s** | Loop: True
- Only 3 params: Param28, Param29, Param33 (simple toggle/blink)
- Not useful for idle — too short

### Summary Table

| File | Duration | Size | Curves | Segments | Complexity |
|---|---|---|---|---|---|
| idle1 | 9.99s | 1000KB | 309 | 30,256 | Medium |
| idle2 | 9.99s | 1133KB | 309 | 34,351 | High |
| idle3 | 10.00s | 1276KB | 309 | 38,915 | Highest |
| 1/2/3 | 0.5-0.83s | <1KB | 3 | 45 | Trivial |

### What the motions actually do:
All 3 idle motions are **full-body idle animations** covering:
- Head rotation (X/Y/Z)
- Body lean (X/Y)
- Eye movement + blinking
- Eyebrow animation
- Mouth shape
- Breathing cycle
- Hair physics (front, side, back — 20+ rotation params)
- Cloth/accessory physics

**These are NOT looped by VTS natively** — they're triggered once and play for ~10s. The `Loop: True` flag means VTS will loop them if triggered via hotkey with loop enabled.

---

## 🏗️ Proposed Architecture

### Dual-Track Idle System

```
┌─────────────────────────────────────────────────────────────┐
│                    MAIN BRIDGE LOOP                         │
│                                                             │
│  ┌──────────────────────┐   ┌───────────────────────────┐  │
│  │  TRACK 1: Motion     │   │  TRACK 2: Expression      │  │
│  │  (body movement)     │   │  (face micro-expression)  │  │
│  │                      │   │                           │  │
│  │  Every 12-18s:       │   │  Every 5-12s:             │  │
│  │  Pick random motion  │   │  Pick random of 50 expr   │  │
│  │  → HotkeyTrigger     │   │  → ExpressionActivation   │  │
│  │                      │   │                           │  │
│  │  Plays for ~10s      │   │  Holds for 4-10s          │  │
│  │  (motion duration)   │   │                           │  │
│  └──────────────────────┘   └───────────────────────────┘  │
│                                                             │
│  Both skip when tts_is_playing = True                      │
│  Both use SAME dedicated idle VTS WebSocket                │
└─────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Motion trigger | HotkeyTriggerRequest | VTS API doesn't support direct motion3.json playback |
| Motion files | idle1, idle2, idle3 | ~10s each, full-body, good variety |
| Track timing | Independent cycles | Motion 12-18s, Expression 5-12s → not synced = natural |
| VTS Connection | Shared dedicated WS | Idle thread has own WS, doesn't interfere with main bridge |
| Motion interval | 12-18s (not 10s) | Motion itself is 10s, need gap between triggers |
| Expression hold | 4-10s (existing) | Unchanged |
| Skip condition | tts_is_playing = True | Don't interrupt speech |

---

## 📋 Step-by-Step Plan

### Phase 0: Manual VTS Setup (Bohan must do FIRST)

**⚠️ PREREQUISITE — Bridge code won't work without this**

1. **Copy motion files to VTS hotkey folder:**
   The files are already in the right location:
   ```
   C:\Program Files (x86)\Steam\steamapps\common\VTube Studio\VTube Studio_Data\StreamingAssets\Live2DModels\A_vts\motion\
   ├── idle1.motion3.json  (9.99s, medium complexity)
   ├── idle2.motion3.json  (9.99s, high complexity)
   └── idle3.motion3.json  (10.00s, highest complexity)
   ```

2. **Setup Hotkeys in VTS:**
   - Open VTS → Settings → Hotkey Setup
   - Add (+) new hotkey
   - Action: **Play Animation**
   - Select file: `idle1.motion3.json`
   - Name: `IdleMotion1`
   - Repeat for `idle2` → `IdleMotion2`, `idle3` → `IdleMotion3`

3. **Verify:** Press hotkey in VTS → model should move

### Phase 1: Bridge Code Changes

**File: `hermes_vtuber_bridge.py`**

#### 1a. Add CONFIG keys
```python
# In CONFIG dict, add:
"idle_motions_enabled": True,
"idle_motion_hotkeys": ["IdleMotion1", "IdleMotion2", "IdleMotion3"],
"idle_motion_interval_min": 12,
"idle_motion_interval_max": 18,
```

#### 1b. Add hotkey trigger method to VTSController class
```python
async def trigger_hotkey(self, hotkey_name):
    """Trigger a VTS hotkey by name (for motion playback)."""
    if not self.websocket:
        return
    payload = {
        "apiName": "VTubeStudioPublicAPI",
        "apiVersion": "1.0",
        "requestID": f"Hotkey_{hotkey_name}",
        "messageType": "HotkeyTriggerRequest",
        "data": {"hotkeyID": hotkey_name}
    }
    try:
        await self.websocket.send(json.dumps(payload))
        resp = json.loads(await self.websocket.recv())
        if resp.get("messageType") == "APIError":
            print(f"[VTS] Hotkey error '{hotkey_name}': {resp.get('data',{}).get('message','?')}")
    except Exception as e:
        print(f"[VTS] Hotkey trigger failed '{hotkey_name}': {e}")
```

#### 1c. Rewrite idle_animation_worker() for dual-track

Replace the existing `_idle_expr_loop()` with a dual-track version:

```python
IDLE_MOTIONS = CONFIG.get("idle_motion_hotkeys", [])
IDLE_MOTIONS_ENABLED = CONFIG.get("idle_motions_enabled", True)

async def _idle_expr_loop():
    """Dual-track idle: motion (hotkey) + expression (activation)."""
    # ... existing VTS connection + auth code (unchanged) ...
    
    last_expr = None
    last_motion = None
    motion_timer = 0
    expr_timer = 0
    motion_interval = random.uniform(
        CONFIG.get("idle_motion_interval_min", 12),
        CONFIG.get("idle_motion_interval_max", 18)
    )
    expr_interval = random.uniform(IDLE_CHECK_MIN, IDLE_CHECK_MAX)
    
    while idle_timer_running:
        await asyncio.sleep(1)  # 1-second tick
        motion_timer += 1
        expr_timer += 1
        
        if tts_is_playing:
            continue
        
        # TRACK 1: Motion (body) — every 12-18s
        if (IDLE_MOTIONS_ENABLED and IDLE_MOTIONS and 
                motion_timer >= motion_interval):
            motion_timer = 0
            motion_interval = random.uniform(
                CONFIG.get("idle_motion_interval_min", 12),
                CONFIG.get("idle_motion_interval_max", 18)
            )
            motion = random.choice(IDLE_MOTIONS)
            while motion == last_motion and len(IDLE_MOTIONS) > 1:
                motion = random.choice(IDLE_MOTIONS)
            last_motion = motion
            await _trigger_hotkey(idle_ws, motion)
            print(f"[Idle] Motion: {motion}")
        
        # TRACK 2: Expression (face) — every 5-12s
        if expr_timer >= expr_interval:
            expr_timer = 0
            expr_interval = random.uniform(IDLE_CHECK_MIN, IDLE_CHECK_MAX)
            expr = random.choice(IDLE_EXPRESSIONS)
            while expr == last_expr and len(IDLE_EXPRESSIONS) > 1:
                expr = random.choice(IDLE_EXPRESSIONS)
            last_expr = expr
            idle_expression_active = True
            await _activate_expression(idle_ws, expr, True)
            hold = random.uniform(IDLE_HOLD_MIN, IDLE_HOLD_MAX)
            await asyncio.sleep(hold)
            await _activate_expression(idle_ws, expr, False)
            idle_expression_active = False
```

#### 1d. Helper functions
```python
async def _trigger_hotkey(ws, hotkey_name):
    payload = {
        "apiName": "VTubeStudioPublicAPI",
        "apiVersion": "1.0",
        "requestID": f"Hotkey_{hotkey_name}",
        "messageType": "HotkeyTriggerRequest",
        "data": {"hotkeyID": hotkey_name}
    }
    try:
        await ws.send(json.dumps(payload))
        await ws.recv()
    except Exception as e:
        print(f"[Idle] Motion error: {e}")

async def _activate_expression(ws, expr_name, active):
    payload = {
        "apiName": "VTubeStudioPublicAPI",
        "apiVersion": "1.0",
        "requestID": f"IdleExpr_{'On' if active else 'Off'}",
        "messageType": "ExpressionActivationRequest",
        "data": {"expressionFile": f"{expr_name}.exp3.json", "active": active}
    }
    try:
        await ws.send(json.dumps(payload))
        await ws.recv()
    except Exception as e:
        print(f"[Idle] Expression error: {e}")
```

### Phase 2: Testing

1. **Prerequisites:**
   - [ ] Hotkeys setup in VTS (IdleMotion1, 2, 3)
   - [ ] VTS API enabled on port 8002

2. **Start bridge:**
   ```powershell
   taskkill /F /IM python.exe
   python hermes_vtuber_bridge.py
   ```

3. **Verify:**
   - [ ] `[Idle] Dedicated VTS connection ready ✓`
   - [ ] `[Idle] Motion: IdleMotionX` every 12-18s
   - [ ] Model moves (body/head/eyes) for ~10s
   - [ ] Expression changes every 5-12s on top of motion
   - [ ] Motion + expression don't conflict
   - [ ] Idle stops during TTS, resumes after

---

## 📁 Files That Change

| File | Change |
|---|---|
| `hermes_vtuber_bridge.py` | Add hotkey trigger, dual-track idle, CONFIG keys |

**Unchanged:** subtitle_server.py, supertone_engine.py, text_preprocessor.py, ARTI_SOUL.md, templates/*

---

## ⚠️ Risks & Open Questions

### Risks
| Risk | Mitigation |
|---|---|
| VTS rejects HotkeyTrigger from plugin | Test 1 hotkey first |
| Motion + expression conflict | Independent timing, no sync |
| Hotkey name mismatch | Clear error logging |
| WS connection drops | Existing reconnect logic |

### Open Questions
1. **Motion interval:** 12-18s OK? (motion is 10s, so 12s = 2s gap, 18s = 8s gap)
2. **Expression frequency:** Keep 5-12s or reduce?
3. **Priority:** If both trigger same tick, motion first then expression?

---

## ✅ Success Criteria

- [ ] Model has body motion every ~15s
- [ ] Model has facial expression changes every ~8s
- [ ] Both run together without issues
- [ ] Idle pauses during TTS
- [ ] No crashes

---

*Plan by OWL — 2026-05-31*
*Motion files analyzed: idle1 (9.99s medium), idle2 (9.99s high), idle3 (10.00s highest)*
*Next: Bohan setup hotkeys → OWL implements code*
