# VTS Animation & Expression Wiring

Bridge mengontrol VTube Studio lewat **WebSocket API**: hotkey, expression state, dan inject parameter (mis. `FaceAngleX/Y/Z`).

## ⚠️ Beda tiap model VTuber

Repo ini **tidak** menyertakan file Live2D / `.exp3.json` kamu.

| Yang beda-beda | Contoh di kode (placeholder) |
|----------------|------------------------------|
| Nama expression file | `ArtiDefault1`, `ArtiBicara`, … |
| Nama hotkey motion | `IdleMotionStop`, `IdleMotion1`, … |
| Parameter yang ada | `FaceAngleX` vs `ParamAngleX` vs custom |
| Range nilai | -30..30 vs skala model lain |

**Yang perlu kamu lakukan:** buka VTS → map hotkey & expression ke nama yang **benar di model kamu**, lalu update key CONFIG / string di `arti_bridge.py` & `arti_expression_runtime.py`.

Jangan copy angka parameter dari dokumentasi lain tanpa cek di VTS Model Settings.

## Lapisan animasi (konsep)

```
┌─────────────────────────────────────┐
│ Emotion expressions (bicara/mikir) │  ← trigger saat LLM/TTS
├─────────────────────────────────────┤
│ Idle head (parameter inject)         │  ← thread background, smooth pose
├─────────────────────────────────────┤
│ Body motion (hotkey .motion3)        │  ← idle loop, pause saat aware
└─────────────────────────────────────┘
```

## CONFIG keys (sesuaikan nilai)

```python
"expression_emotion_enabled": True,
"expression_nod_enabled": True,
"expression_mood_strip_param_ids": [],  # Param ID model kamu yang jangan disentuh mood overlay
"idle_motion_stop_hotkey": "IdleMotionStop",   # ganti ke hotkey VTS kamu
"idle_vts_connect_timeout_sec": 20,
```

`expression_mood_strip_param_ids`: cek file `.exp3.json` mood kamu di VTS — kalau ada param lampu/mulut/deformasi custom (mis. `Param48`), masukkan di sini supaya overlay mood tidak merusak lip-sync.

Saat **PTT ON** atau **YT trigger**: idle body motion di-pause, expression “aware/bicara” dipakai (nama state sesuaikan).

Saat **TTS selesai**: delay ~3s (echo suppress) lalu idle resume.

## Nod saat bicara

`arti_expression_runtime` + `arti_nod.py` — inject parameter periodik selama TTS.

- `expression_nod_period_sec`, `expression_nod_fps` — tune kalau kepala terlalu kaku/cepat
- Model tanpa parameter sudut wajah: matikan `expression_nod_enabled`

## Debugging

1. VTS open → API connected (hijau).
2. Log bridge: `[Expr]`, `[Idle]`, `[VTS]`.
3. Test satu hotkey manual di VTS dulu, baru samakan string di CONFIG.

## Referensi teknis opsional

- Scouter / mood → `docs/SCOUTER.md`
- Subtitle OBS → `subtitle_server.py` + CONFIG `subtitle_*`
