# Spike Cursor SDK — hasil Tahap 0 (2026-07-31)

Gerbang GO/NO-GO sebelum menyentuh `hermes_vtuber_bridge.py`. Reproduksi:

```powershell
.\venv\Scripts\python.exe scripts\spike_cursor_latency.py --cwd <folder-kosong> --n 20
```

Lingkungan: Windows 11, Python 3.11.9, `cursor-sdk` 1.0.26, `composer-2.5` (`fast=true`),
mesin idle (bridge Arti tidak jalan).

## Verdict: MARGINAL — lanjut dengan `cursor_timeout_sec = 5.0`

## Angka

| Skenario | p50 | p95 | max | n |
|---|---|---|---|---|
| Sesi hangat, semua sampel | 2,66 s | 4,92 s | 13,11 s | 20 |
| **Sesi hangat, tanpa turn pertama** | **2,66 s** | **3,73 s** | **4,48 s** | 19 |
| Dingin (agen baru tiap turn) | 4,62 s | 9,77 s | 10,72 s | 5 |

**Turn pertama = 13,11 detik.** Ini biaya sekali seumur sesi, bukan per jawaban — dan
bisa dibayar di luar siaran dengan mengirim satu pesan pemanasan saat bridge start.
Seluruh p95 4,92 s pada baris pertama berasal dari sampel ini saja.

**Sesi hangat menghemat 1,97 detik per turn** (p50 dingin 4,62 vs hangat 2,66).
Ambang di rencana adalah 0,30 detik, jadi mesin manajemen sesi (`CursorSession`)
jelas layak dibangun — tanpa itu setiap chat viewer bayar ulang cold start.

## Kualitas

- **25/25 sukses**, nol timeout, nol error.
- **Nol `tool_call`.** Agen patuh pada instruksi "jangan panggil tool" di header prompt.
- **Nol berkas berubah** di folder scratch setelah 45 turn.
- **20/20 jawaban unik**, bahasa Indonesia santai sesuai persona, panjang 103–294 karakter.
- Pembengkakan konteks setelah 20 turn: **1,05×** — dapat diabaikan. `cursor_session_max_turns = 8`
  di rencana ternyata terlalu konservatif; boleh dinaikkan.
- Blok `thinking` 5–22 per turn: Composer memang bernalar, tapi di server dan sudah
  terhitung dalam angka di atas.

## Jebakan metodologi (jangan diulang)

Run pertama memakai 10 pertanyaan yang berputar untuk 20 sampel. Karena sesi hangat
menyimpan konteks, agen **mengulang jawaban lamanya verbatim** — sampel 11–20 kembali
dengan teks dan jumlah karakter identik, dan waktunya cepat palsu. Chat viewer asli
selalu baru, jadi daftar pertanyaan harus lebih banyak daripada `--n` dan semuanya
berbeda. Script sekarang memperingatkan kalau `--n` melebihi jumlah pertanyaan unik.

Implikasi produk: dalam satu sesi hangat, pertanyaan viewer yang mirip akan dijawab
nyaris sama persis. Daur ulang sesi berkala meredakan ini.

## Bug Windows di `cursor-sdk` 1.0.26 dan penanganannya

`Agent.create()` gagal di Windows sebelum menyentuh jaringan:

```
AttributeError: module 'os' has no attribute 'get_blocking'
  cursor_sdk/_bridge.py:233 in _read_discovery
```

`Bridge.launch()` membaca baris discovery dari stderr memakai `os.get_blocking()` +
`selectors` — dua-duanya POSIX-only. Ini **bukan sekadar fungsi yang hilang**: di Windows
pipe tidak bisa dijadikan non-blocking lewat `os.set_blocking`, dan `selectors` tidak bisa
memantau handle pipe (hanya socket). Menambal `os.get_blocking` saja tidak menolong.

Kena **semua mode**, bukan cuma agen lokal: `_default_client()` selalu memanggil
`Bridge.launch()` kecuali `CURSOR_SDK_BRIDGE_URL` dan `CURSOR_SDK_BRIDGE_TOKEN` sudah
ter-set, jadi cloud agent pun ikut gagal. Wheel `win_amd64` tetap dipublikasikan, jadi
Windows memang diniatkan didukung — ini bug hulu, bukan platform yang tidak didukung.

**Penanganan** (`launch_bridge()` di `scripts/spike_cursor_latency.py`): nyalakan
`cursor-sdk-bridge` sendiri, baca baris `cursor-sdk-bridge ready {json}` dari stderr
dengan `readline` blocking biasa di thread terpisah, bentuk `BridgeEndpoint.from_discovery()`,
lalu suntikkan `Client` hasilnya lewat `Agent.create(client=...)` — parameter yang memang
disediakan SDK. Tidak ada monkeypatch. Bridge siap dalam ~0,7 detik.

Kode ini harus ikut pindah ke `arti_cursor_agent.py` di Tahap 1, dengan pengecekan versi
supaya bisa dilepas kalau upstream memperbaikinya.

## Yang belum diketahui

Semua angka di atas diambil saat mesin **idle**. Saat live sungguhan, bridge Arti juga
menjalankan ASR, TTS, VTS, scouter, dan RAG di mesin yang sama. Angka nyata akan lebih
buruk — seberapa buruk belum terukur.

Perbandingan kasar dengan jalur sekarang: Groq menjawab dalam 348–645 ms, jadi Cursor
**menambah sekitar 2 detik per jawaban**. Ditambah sintesis TTS ~3,4 detik (CPU-only),
total bisu untuk chat YT jadi sekitar 6 detik. Ini memperkuat alasan restore CUDA:
di jalur Cursor, TTS tetap komponen terbesar.
