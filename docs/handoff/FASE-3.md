# Fase 3 — Stabilitas (SELESAI kode, tes live nanti)

## Scope

| Item | Perubahan |
|------|-----------|
| **Idle event loop** | Jangan spawn thread idle baru tiap TTS selesai → `resume_idle_animation()`; pause saja via `stop_idle_animation()` |
| **Lampu kepala** | `trigger_expression_state("default")` inject `Param178=0` (`vts_lamp_reset_params`) |
| **Supertone** | Timeout → **1 retry** sebelum robot/fallback (`supertone_retry_on_timeout`) |
| **Subtitle** | Phrase mode (Supertone): **satu frasa** di layar, bukan dump semua teks |

Laguna / filter narrator / Groq→OpenRouter = **Fase 2** — tes terpisah nanti.

## CONFIG baru

```python
"vts_lamp_reset_params": ["Param178"],
"supertone_retry_on_timeout": True,
"idle_pause_during_ptt": True,
```

Kalau lampu masih nyangkut, cek param di VTS model & tambah ID ke list di atas.

## Test checklist (live)

- [ ] Stream 15+ menit: log **tanpa** spam `different event loop` / `Old thread still alive`
- [ ] Setelah bicara: lampu kepala **mati** (default pose)
- [ ] Subtitle OBS: Supertone hanya tampil **1 frasa** bergantian
- [ ] Paksa Supertone lambat: log `Supertone timeout — retry sekali`
- [ ] PTT beberapa kali: idle motion/expr tetap jalan setelah pause

## Known belum

- YouTube `Gagal ambil token` — ganti `youtube_video_id` saat live
- TTS ~10–15s — tuning Supertone terpisah (speed 1.3, steps 12)
