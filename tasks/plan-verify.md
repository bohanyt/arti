# Live Verification Plan — belum tes sama sekali

> **Head:** `25f06ec` (14 commit sejak `v0.5.2-stable`)  
> **Unit tests:** lulus di dev machine — **live stream belum pernah dijalankan**  
> **Rollback:** `git reset --hard v0.5.2-stable`

Tandai `[ ]` → `[x]` setelah kamu tes manual. Jangan enable CONFIG baru sebelum tier sebelumnya hijau.

---

## Tier 0 — Baseline regression (WAJIB dulu, ~15 menit)

Tanpa ubah CONFIG. Pastikan Arti masih seperti sebelum build-auto.

| # | Cek | Cara | Pass? |
|---|-----|------|-------|
| 0.1 | Bridge start | `python hermes_vtuber_bridge.py` — VTS connect, mic, TTS OK | [ ] |
| 0.2 | PTT jawaban | Hotkey ON → bicara → Arti jawab + suara keluar | [ ] |
| 0.3 | Idle setelah TTS | Setelah jawaban: gerakan idle **mulai lagi** (bukan freeze) | [ ] |
| 0.4 | Lampu default | Ekspresi kembali normal setelah TTS (tidak stuck mikir/bicara) | [ ] |
| 0.5 | PTT kedua | Trigger kedua setelah jawaban pertama — tidak overlap / tidak macet | [ ] |
| 0.6 | Terminal bersih | Tidak ada spam `attached to a different loop` saat idle | [ ] |

**Gagal di sini → stop, rollback tag `v0.5.2-stable`, laporkan.**

---

## Tier 1 — Latency instrumentation (aktif default, ~10 menit)

Sudah di code path — tidak perlu CONFIG baru.

| # | Cek | Cara | Pass? |
|---|-----|------|-------|
| 1.1 | `[Latency]` terminal | Setelah 3–5 trigger PTT, ada baris `[Latency] asr=… rag=… llm=… tts=… total=…` | [ ] |
| 1.2 | JSONL stages | `transcripts/{session}.jsonl` — baris `kind: arti` punya `latency_ms` + `stages` | [ ] |
| 1.3 | Stage dominan | Catat mana paling besar: `asr` / `rag` / `llm` / `tts` (untuk prioritas optimasi) | [ ] |
| 1.4 | Subjektif | Terasa lebih responsif / sama / lebih lambat vs ingatan sebelumnya? | [ ] |

**Catatan:** Fase 2a–2e (async HTTP, parallel RAG, Session, embed cache) sudah aktif tanpa flag — kalau Tier 0 lolos, diasumsikan OK.

---

## Tier 2 — False trigger `berarti` (~5 menit)

| # | Cek | Cara | Pass? |
|---|-----|------|-------|
| 2.1 | YT tidak trigger | Chat test / simulasi: `"berarti bang bohan ganteng"` → **tidak** queue Arti | [ ] |
| 2.2 | YT masih trigger | Chat: `"eh arti halo"` → **trigger** Arti | [ ] |
| 2.3 | Wake word (jika dipakai) | Ucapkan kalimat dengan `berarti` tanpa panggil Arti → tidak trigger | [ ] |
| 2.4 | Unit test | `pytest tests/test_arti_wake.py` | [ ] |

---

## Tier 3 — Emotion system (CONFIG masih `False` dulu, ~10 menit)

| # | Cek | Cara | Pass? |
|---|-----|------|-------|
| 3.1 | Default OFF aman | `expression_emotion_enabled: False`, `expression_nod_enabled: False` — perilaku = Tier 0 | [ ] |
| 3.2 | Enable emotion | Set `expression_emotion_enabled: True` → ikuti `docs/SMOKE-TEST-emotion.md` (7 item) | [ ] |
| 3.3 | Enable nod | Set `expression_nod_enabled: True` — nod saat TTS, `FaceAngleY` reset 0 | [ ] |
| 3.4 | Rollback CONFIG | Kembalikan kedua flag ke `False` — baseline lagi | [ ] |

---

## Tier 4 — Opsional (CONFIG default OFF, tes kalau mau)

| Feature | CONFIG key | Cek singkat | Pass? |
|---------|------------|-------------|-------|
| Groq streaming TTS | `groq_stream_enabled: True` | Suara pertama lebih cepat? fallback OK jika gagal? | [ ] |
| NVIDIA POC | `NVIDIA_API_KEY` | `python scripts/test_diffusiongemma_nvidia.py` | [ ] |
| Watch party | `watch_party_enabled` + `watch_party_event_id` | Pause + PTT → konteks episoda masuk jawaban | [ ] |
| Screen watcher | `screen_context_enabled` | Thread jalan (capture belum wired — expect idle log) | [ ] |
| Desktop audio | `desktop_audio_enabled` | Thread jalan (loopback belum wired — expect idle log) | [ ] |

---

## Tier 5 — Belum diimplementasi penuh (jangan expect fitur)

- Desktop audio WASAPI capture + transcribe
- Screen `mss` + NVIDIA vision capture
- Co-watch curious RNG proaktif
- Emotion CONFIG default ON di commit

---

## Urutan disarankan

```
Tier 0 → Tier 1 → Tier 2 → Tier 3 → (Tier 4 kalau mau)
```

Satu sesi pendek (~30 menit) cukup untuk Tier 0–2. Tier 3 butuh VTS + siap rollback CONFIG.

## Plans di repo

| File | Isi |
|------|-----|
| `tasks/plan.md` | Emotion implementation (done) |
| `tasks/plan-latency.md` | Latency + co-watch implementation (done) |
| `tasks/plan-verify.md` | **Ini** — live checklist |
| `docs/SMOKE-TEST-emotion.md` | Detail emotion Tier 3.2 |
