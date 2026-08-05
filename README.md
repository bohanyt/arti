# Arti — VTuber AI Co-host

**Co-host VTuber yang benar-benar hadir di siaranmu.** Dia dengar suaramu, baca
chat YouTube, lihat layarmu, ingat obrolan minggu lalu, bereaksi ke donasi,
nonton video yang di-share penonton, ngoceh sendiri kalau kamu diam — dan kalau
kamu bosan, dia bisa masuk Minecraft dan main beneran di sana.

Semuanya jalan di satu laptop, dan **bisa nol rupiah** — tiap bagian punya jalur
provider gratis (lihat [Jalan gratis](#jalan-gratis-buat-yang-baru-mulai)).

> **English:** A Python bridge that turns an LLM into a live VTuber co-host —
> speech-to-text, YouTube chat, screen vision, desktop-audio hearing, vault RAG
> memory, donation reactions, video understanding, VTube Studio puppeteering,
> OBS scene control, and an actual Minecraft player body. Comments, prompts, and
> docs are in Indonesian; the architecture is language-agnostic.

```
40.787 baris Python  ·  692 test  ·  9 provider LLM dengan fallback berlapis
```

---

## Kenapa ini beda dari "chatbot yang dikasih suara"

Kebanyakan VTuber AI adalah satu loop: *input → LLM → TTS*. Yang ini punya
**indra dan kemauan sendiri**, dan semuanya disatukan tanpa menambah satu pun
panggilan LLM di jalur kritis.

Contoh satu momen nyata di siaran:

> Penonton nge-chat sambil ada musik jalan di layar. Arti **mendengar** lirik
> lagunya lewat audio desktop, **melihat** video yang lagi diputar, **ingat**
> bahwa penonton itu pernah cerita soal helikopter dua minggu lalu, lalu
> menjawab sambil nyambungin ketiganya — dengan ekspresi wajah yang berubah dan
> subtitle karaoke per-kata di OBS. Beberapa detik kemudian dia **mati kena
> creeper** di Minecraft dan langsung teriak sendiri.

---

## Fitur

### 🎙️ Mulut & tubuh
| | |
|---|---|
| **TTS** | Supertonic lokal (CUDA: ±0,3 dtk/kalimat) atau Edge TTS gratis |
| **Puppeteering VTube Studio** | WebSocket API — ekspresi, mood, anggukan sadar-konteks |
| **Ekspresi per-turn** | LLM menandai emosinya sendiri; sedih/marah/bingung tampil di wajah |
| **Anggukan hidup** | Kepala mengangguk selagi bicara, berhenti sendiri saat selesai |
| **Subtitle karaoke** | Server subtitle sendiri → OBS Browser Source, sorot per kata |
| **Mulai bicara lebih awal** | Jawaban dipecah per kalimat saat masih mengalir — TTS jalan sebelum LLM selesai |
| **Angka dibaca manusiawi** | "Rp15.000", "27/05", "19:30", "75%" diucapkan sebagai kata, bukan dieja |
| **Nama panggilan pendek** | Handle `penontonsetia241` disapa "penonton" — bukan dibaca bulat-bulat sama angkanya |

### 👂 Telinga
| | |
|---|---|
| **Mic streamer** | Whisper (lokal atau Groq) — mode push-to-talk / wake word |
| **Audio desktop** | Loopback WASAPI: dia dengar video/musik yang kamu putar |
| **Anti-dengar-suara-sendiri** | Suara TTS Arti disaring dari telinganya (overlap chunk + deteksi gema) |
| **Anti-halusinasi Whisper** | Filter "sampah" ASR multibahasa (subtitle hantu, titik-titik) |

### 👁️ Mata & tahu dunia luar
| | |
|---|---|
| **Vision layar** | Screenshot on-demand saat ditanya "ini apa di layarku?" |
| **Scouter** | Pengintai berkala: ringkasan layar + mood + umpan topik |
| **Cek internet** | Ditanya berita/harga/skor → dia **intip web dulu** baru jawab. Pemicunya konservatif (bukan tiap pertanyaan), jalan paralel dengan RAG jadi hampir tak menambah waktu |
| **Anti-bocor** | Terminal/log yang kelihatan di layar di-scrub, tidak jadi bahan omongan |

### 🧠 Ingatan
| | |
|---|---|
| **Vault RAG** | Hybrid semantic + full-text search, embedding lokal (LM Studio) |
| **Peluruhan recency** | Ingatan lama meredup halus (half-life 14 hari); pertanyaan temporal di-boost |
| **Observer pasca-sesi** | Setelah siaran, transkrip dikurasi jadi "beat" & pelajaran, lalu di-index |
| **Profil penonton** | Dia ingat siapa yang sering datang dan pernah ngomong apa |
| **Kualitas memori** | Penyaring duplikat/parafrase supaya ingatan tidak jadi sampah |
| **Penjaga garis waktu** | Ditanya "kapan terakhir…" — tanggal debut & kanon dijaga, bukan dikarang |
| **Transkrip sesi** | Tiap giliran dicatat JSONL (pemicu, latensi per tahap, jawaban) — bahan mentah observer |
| **Profil ganda** | Beberapa persona dengan ingatan terpisah — ganti `active_profile`, dia jadi "orang" lain |

### 💬 Panggung live
| | |
|---|---|
| **Chat YouTube** | Innertube langsung (tanpa API key), antrean prioritas + anti-spam |
| **Super Chat** | Reaksi terima kasih, ditahan sampai alert overlay selesai bunyi |
| **Saweria & Streamlabs** | Listener realtime (WebSocket / Socket.IO) |
| **Media share** | Penonton kirim video → Arti **nonton** lalu komentar |
| **Paham video** | Gemini menonton URL YouTube di sisi server + transkrip — nol bandwidth |
| **Nonton bareng ber-timecode** | Selama nonton, ingatannya diambil sesuai **menit yang lagi diputar** — dia nyambung ke adegan yang barusan lewat |
| **Telemetri penonton** | Jumlah viewer naik = tanda kehidupan + bahan sapaan |

### ✨ Kemauan sendiri
| | |
|---|---|
| **Curious** | Komentar spontan soal apa yang dia lihat di layar |
| **Inisiatif** | Buka topik sendiri saat hening — bahan acak-berbobot, anti-mengulang topik |
| **Detektor kehidupan** | Ruangan benar-benar kosong = dia diam, bukan monolog ke tembok |
| **4 mode sesi** | Ngobrol berdua · main bareng · **Arti pegang siaran sendiri** · Arti solo main game |
| **Saklar natural** | Cukup bilang "aku AFK ya, kamu pegang" — dia yang ambil alih |
| **Panjang jawaban adaptif** | Sapaan dijawab pendek, pertanyaan "kenapa/jelaskan" dijawab dalam — dan sesekali **mode nyerocos**: dadu deterministik bikin dia tiba-tiba panjang lebar |

### ⛏️ Minecraft — dia main beneran
| | |
|---|---|
| **Player sungguhan** | mineflayer: server melihatnya sebagai pemain, bukan skrip |
| **Ngerti isi dunia** | HP, lapar, siang/malam, mob dekat, posisi — data langsung, bukan tebakan dari gambar |
| **Reaksi bersuara** | Mati, diserang creeper, nyangkut — semuanya jadi komentar spontan |
| **Jelajah mandiri** | Ditinggal sendirian? Dia jalan-jalan sendiri, bukan mematung |
| **Misi dari streamer** | "Cari desa" → dikejar, diceritakan, dan dia sendiri yang menyatakan selesai |
| **Aksi lewat tag** | Perintah keluar dari jawaban biasa — **nol panggilan LLM tambahan** |

### 🎛️ Panggung & operasi
| | |
|---|---|
| **Scene OBS otomatis** | obs-websocket v5 — scene ganti sendiri mengikuti mode sesi |
| **Telemetri biaya** | Tiap panggilan API dicatat: model, latensi, token, perkiraan biaya — plus dashboard HTML per provider/subsistem |
| **Ketik kalau tak bisa ngomong** | Panggil Arti lewat ketikan di console (mic mati, remote, atau lagi tes) — termasuk simulasi chat penonton |
| **Health check** | Cek mic/TTS/VTS/provider saat start — masalah ketahuan sebelum live |
| **Shutdown tuntas** | Reindex memori diselesaikan dulu, ada banner "aman ditutup" |

---

## Yang bikin ini menarik secara teknis

- **Rantai provider berlapis.** 9 penyedia LLM dalam satu chain. Provider tumbang
  → turun ke berikutnya tanpa siaran terganggu. Ada circuit breaker, backoff, dan
  "rehat" otomatis saat semua provider kena limit.
- **Nol panggilan LLM tambahan untuk bertindak.** Arti bertindak lewat tag
  tersembunyi di dalam jawabannya (`[MC: join]`, `[MODE: host]`,
  `[EMOTION: senang]`) yang divalidasi keras di sisi kode. Tidak ada round-trip
  tool-calling, tidak ada giliran bicara yang hangus gara-gara itu.
- **Gate pemilik.** Perintah yang mengubah sesi cuma diterima dari streamer —
  penonton iseng tidak bisa menyuruh dia keluar dari game.
- **Antrean prioritas.** Donasi > chat > mic > komentar spontan, lengkap dengan
  TTL, dedup per penonton, dan aturan siapa yang boleh dibuang saat sibuk.
- **Deadman switch di mana-mana.** Capture gagal, bot game putus, provider mati —
  semua menyerah dengan tenang dan melapor, bukan spam tak berujung.
- **692 test tanpa jaringan.** Termasuk property-based test, snapshot konstanta
  (supaya setelan pribadi tidak bocor ke commit), dan pemeriksaan urutan gerbang.

```
                 ┌──────────── Indra ────────────┐
   mic ──────────┤ Whisper ASR                   │
   audio desktop ┤ loopback → transkrip          │
   layar ────────┤ vision + scouter              │──┐
   chat YT ──────┤ innertube                     │  │
   donasi ───────┤ Saweria / Streamlabs / SC     │  │
   Minecraft ────┤ mineflayer (NDJSON)           │  │
                 └───────────────────────────────┘  │
                                                    ▼
                          ┌─────────────────────────────────┐
   vault RAG ────────────►│  Orkestrator (prioritas, mode,  │
   profil penonton ──────►│  persona, gerbang proaktif)     │
   SOUL / mood ──────────►└─────────────┬───────────────────┘
                                        ▼
                          rantai LLM (9 provider, fallback)
                                        ▼
              ┌─────────────────────────┴──────────────────────┐
              ▼             ▼              ▼                   ▼
            TTS      VTube Studio     subtitle OBS      aksi Minecraft
```

---

## Jalan gratis (buat yang baru mulai)

Kamu **tidak perlu bayar apa pun** untuk menjalankan ini. Tiap bagian punya
pilihan gratis — daftar, ambil API key, tempel ke `.env`.

### Otak (LLM)
| Layanan | Daftar di | Catatan |
|---|---|---|
| **Groq** | [console.groq.com](https://console.groq.com) | Paling gesit untuk balasan live; sekalian dipakai untuk Whisper ASR. **Mulai dari sini.** |
| **Google AI Studio** | [aistudio.google.com](https://aistudio.google.com) | Gemini & Gemma — juga yang dipakai untuk "nonton video YouTube". |
| **Cloudflare Workers AI** | [dash.cloudflare.com](https://dash.cloudflare.com) | Ada jatah harian gratis. Butuh Account ID + API Token. |
| **GitHub Models** | [github.com/marketplace/models](https://github.com/marketplace/models) | Gratis dengan akun GitHub biasa. |
| **NVIDIA NIM** | [build.nvidia.com](https://build.nvidia.com) | Kredit gratis; model vision-nya kencang. |
| **OpenRouter** | [openrouter.ai](https://openrouter.ai) | Cari model berakhiran `:free`. |
| **SambaNova** | [cloud.sambanova.ai](https://cloud.sambanova.ai) | Tier gratis. |
| **Ollama** | [ollama.com](https://ollama.com) | Jalan di komputermu sendiri, tanpa akun. |

Kamu tidak harus punya semuanya. **Satu kunci Groq saja sudah cukup buat hidup** —
sisanya jadi cadangan otomatis kalau yang utama kena limit.

### Suara, mata, ingatan
| Bagian | Pilihan gratis |
|---|---|
| **TTS** | `edge_tts` — suara Microsoft, tanpa akun. Atau Supertonic lokal (lebih bagus; butuh GPU biar cepat). |
| **ASR mic** | `faster-whisper` lokal (tanpa akun) atau Whisper lewat Groq. |
| **Embedding RAG** | [LM Studio](https://lmstudio.ai) — server embedding lokal, gratis selamanya. |
| **Avatar** | [VTube Studio](https://store.steampowered.com/app/1325860/) — versi gratis cukup; kamu perlu model Live2D. |
| **Minecraft** | [PaperMC](https://papermc.io) + [Temurin JDK 21](https://adoptium.net), dua-duanya gratis. Bot **tidak butuh akun Minecraft** (server lokal offline-mode). |
| **OBS** | [obsproject.com](https://obsproject.com) — WebSocket sudah bawaan sejak OBS 28. |

### Yang berbayar (opsional, boleh dilewati)
Cursor SDK dipakai sebagai "otak premium" untuk jawaban utama, tapi **seluruh
sistem tetap jalan tanpanya** — chain provider gratis di atas adalah fallback
penuh, bukan mode darurat.

---

## Mulai cepat

```powershell
git clone <repo-url>
cd <folder>

# venv bridge (Python 3.11)
py -3.11 -m venv venv
venv\Scripts\python -m pip install -r requirements.txt

# venv TTS Supertonic (Python 3.12, terpisah) — opsional, bisa pakai edge_tts
py -3.12 -m venv venv312
venv312\Scripts\python -m pip install -r requirements-supertone.txt

copy .env.example .env                                  # isi API key
copy config_local.json.example config_local.json        # video ID YT, nama kamu, dll
copy ARTI_SOUL.example.md ARTI_SOUL.md                  # KARAKTER co-host kamu
copy ARTI_VIEWERS.example.md ARTI_VIEWERS.md
copy ARTI_MOOD_STATE.example.json ARTI_MOOD_STATE.json

venv\Scripts\python hermes_vtuber_bridge.py
```

Atau `start_arti.bat` (bridge + dashboard telemetri sekaligus).

**Semua fitur berat default MATI.** Nyalakan satu per satu di `config_local.json`
setelah yang dasar jalan — donasi, video, telinga, Minecraft, scene OBS.

### Karakternya milikmu
`ARTI_SOUL.md` adalah jiwa co-host: nama, gaya bicara, hubungannya denganmu, hal
yang dia suka dan hindari. Repo ini cuma membawa contohnya. Karakter, nama, model
Live2D, dan API key **tidak disertakan** — itu punyamu.

---

## Dokumentasi

| File | Isi |
|------|-----|
| [`docs/WIRING.md`](docs/WIRING.md) | Setup lengkap: env, VTS, TTS, LLM, chat YT |
| [`docs/VTS-ANIMATION.md`](docs/VTS-ANIMATION.md) | Idle / ekspresi / parameter — beda tiap model |
| [`docs/SCOUTER.md`](docs/SCOUTER.md) | Pengintai layar & mood |
| [`docs/OBSERVER.md`](docs/OBSERVER.md) | Pipeline memori pasca-siaran |
| [`docs/MINECRAFT-SETUP.md`](docs/MINECRAFT-SETUP.md) | Server lokal + bot Minecraft |
| [`CHANGELOG.md`](CHANGELOG.md) | Versi & catatan patch |

## Struktur

```
hermes_vtuber_bridge.py   # entry point live (orkestrator)
arti_*.py                 # modul: voice, RAG, scouter, vision, minecraft, obs, …
mc-bot/                   # bot Minecraft (Node + mineflayer), protokol NDJSON
supertone_engine.py       # TTS subprocess (venv312)
subtitle_server.py        # server subtitle OBS
templates/                # contoh file ekspresi VTS (.exp3.json) — titik mulai
start_arti.bat            # jalankan bridge + dashboard telemetri sekaligus
tests/                    # 692 test, tanpa jaringan
scripts/                  # utilitas + spike (uji nyata ke provider)
docs/                     # wiring & referensi teknis
vault/  data/             # memori & DB (gitignored, dibangun ulang)
```

## Tes

```powershell
venv\Scripts\python -m pytest tests/ --timeout=180 --ignore=tests/test_supertone_integration.py
```

`test_supertone_integration.py` butuh model Supertone asli di venv312 — jalankan
terpisah kalau perlu.

---

## Jujur soal batasannya

- **Bahasa Indonesia** adalah bahasa utama prompt & persona. Arsitekturnya netral,
  tapi kamu perlu menerjemahkan prompt kalau mau bahasa lain.
- **Windows-first.** Audio loopback dan beberapa jalur memakai API Windows.
- **Butuh model Live2D sendiri** untuk VTube Studio, dan nama parameter ekspresi
  berbeda antar model — bagian ini pasti perlu penyesuaian.
- **Minecraft masih tahap awal.** Arti bisa menjelajah, bereaksi, mengikuti, dan
  ngobrol di dalam game; dia **belum bisa menambang atau membangun** — itu
  pekerjaan berikutnya.
- Ini **peralatan siaran satu orang**, bukan produk. Ada bagian yang berantakan,
  dan banyak keputusan desain lahir dari kejadian nyata di siaran — komentarnya
  menceritakan itu, dan sengaja tidak dibersihkan karena di situ pelajarannya.

## Keamanan

Jangan commit `.env`, `vts_token.txt`, `config_local.json`, atau `ARTI_SOUL.md`
(semuanya sudah di-gitignore). Kalau kamu pernah push repo yang berisi API key,
**rotate** kuncinya di konsol provider sebelum go public.

## Lisensi

MIT — lihat [LICENSE](LICENSE).
