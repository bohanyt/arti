import asyncio
import json
import os
from pathlib import Path
import sys

import arti_endpoint
import arti_env

arti_env.load_project_env()

# Fix cuBLAS DLL path untuk GPU Whisper (faster-whisper/ctranslate2)
_cublas_path = os.path.join(os.path.dirname(sys.executable), "..", "Lib", "site-packages", "nvidia", "cublas", "bin")
if os.path.isdir(_cublas_path):
    os.environ["PATH"] = _cublas_path + os.pathsep + os.environ.get("PATH", "")
_cudnn_path = os.path.join(os.path.dirname(sys.executable), "..", "Lib", "site-packages", "nvidia", "cudnn", "bin")
if os.path.isdir(_cudnn_path):
    os.environ["PATH"] = _cudnn_path + os.pathsep + os.environ.get("PATH", "")

import tempfile
import queue
import threading
from dataclasses import dataclass
import time
import collections
import random
import itertools
import subprocess
import socket
import requests
import re
import sounddevice as sd
import soundfile as sf
import websockets
import edge_tts
import numpy as np
from faster_whisper import WhisperModel

import bridge_health
import arti_vault_rag
import session_transcript
import pipeline_timer
from pipeline_timer import PipelineTimer, format_latency_line
import arti_expression_runtime
import arti_screen_context
import arti_timeline_guard
import arti_vision_client
import arti_curious
import arti_agy_agent
import arti_codex_agent
import arti_desktop_audio
import arti_vad
import arti_http_util
import arti_voice_pipeline
import arti_groq_stream
import arti_wake
from arti_wake import is_arti_wake_call
import arti_yt_viewers
import arti_minecraft
import arti_reflex
import arti_obs
import arti_session_mode
import arti_craft_panel
import arti_spectator
import arti_nod
import arti_openrouter
import arti_reply_policy
import arti_voice_queue
import arti_voice_dsp
import arti_benang
import arti_renungan
import arti_speech_censor

# OBS Subtitle Integration: import broadcast helpers + main start coroutine from
# subtitle_server.py without redefining or shadowing those names. The
# `if __name__ == "__main__":` block in subtitle_server.py remains untouched so
# `python subtitle_server.py` still works standalone.
from subtitle_server import broadcast_subtitle as _subtitle_broadcast, broadcast_status as _subtitle_broadcast_status, main as _subtitle_server_main

# ==========================================
# DEBUG SESSION LOGGER
# ==========================================
_DEBUG_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_logs")
os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)
_DEBUG_LOG_PATH = os.path.join(_DEBUG_LOG_DIR, time.strftime("%Y-%m-%d_%H%M%S") + "_bridge.log")

class _TeeOutput:
    """Duplikasi stdout + stderr ke file log dan terminal secara bersamaan."""
    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file
    def write(self, data):
        # Terminal Windows bisa cp1252. Sebelum [date removed] ini tidak pernah kena
        # karena emoji chat penonton DIBUANG di parser; begitu emoji
        # diloloskan, satu karakter yang tak bisa dikodekan cukup untuk
        # melempar UnicodeEncodeError ke pemanggil print() - dan di chat
        # worker itu berarti worker tumbang gara-gara satu penonton.
        #
        # Berkas log tetap menerima data ASLI (dia UTF-8); yang diganti
        # tanda tanya cuma tampilan terminal.
        try:
            self.stream.write(data)
        except UnicodeEncodeError:
            enc = getattr(self.stream, "encoding", None) or "ascii"
            try:
                self.stream.write(data.encode(enc, "replace").decode(enc, "replace"))
            except Exception:
                pass
        except Exception:
            pass
        try:
            self.stream.flush()
        except Exception:
            pass
        try:
            self.log_file.write(data)
            self.log_file.flush()
        except Exception:
            pass
    def flush(self):
        self.stream.flush()
        try:
            self.log_file.flush()
        except Exception:
            pass
    def isatty(self):
        return False

# Di bawah pytest modul ini diimpor ulang puluhan kali, dan tiap impor dulu
# melahirkan berkas log ~293 byte di session_logs/ — sampah yang menendang log
# siaran ASLI keluar lewat rotasi (diagnosa [date removed]). Suite sekali jalan =
# 3 berkas sampah. Jadi saat diuji, tulis ke devnull: perilaku Tee tetap sama,
# cuma tidak meninggalkan jejak.
_log_fh = open(
    os.devnull
    if ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules)
    else _DEBUG_LOG_PATH,
    "w",
    encoding="utf-8",
    buffering=1,
)
_log_fh.write(f"[Session started {time.strftime('%Y-%m-%d %H:%M:%S')}] [PID {os.getpid()}]\n")
_log_fh.write("=" * 60 + "\n")

# Simpan original stdout/stderr
_orig_stdout = sys.stdout
_orig_stderr = sys.stderr
sys.stdout = _TeeOutput(_orig_stdout, _log_fh)
sys.stderr = _TeeOutput(_orig_stderr, _log_fh)

print(f"[DebugLogger] Session log aktif: {_DEBUG_LOG_PATH}")

# ==========================================
# KONFIGURASI UTAMA
# ==========================================
CONFIG = {
    # Provider API Utama: 
    # - "gemini_live" : Live WebSocket API (Gemini 2.5 Flash, 100% Stabil & UNLIMITED RPD)
    # - "gemini"      : Google AI Studio HTTP API (Bisa untuk gemma-4-26b-a4b-it / gemma-4-31b-it - 1.5K RPD)
    # - "groq"        : Groq API (Sangat Cepat, Limit 14.4K RPD Gratis)
    # - "sambanova"   : SambaNova API (Sangat Cepat, Limit 48K RPD Gratis)
    "api_provider": "groq",
    
    # Profil Aktif untuk Memori & Jurnal: membedakan memori antara sesi stream (misal: "default", "gaming", "talkshow")
    "active_profile": "default",
    
    # Konfigurasi Google AI Studio (Gemini Developer API)
    "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),  # isi di .env (lihat .env.example)
    
    # Model Google AI Studio yang digunakan:
    # - "gemini-2.5-flash"      (Sangat disarankan untuk gemini_live karena Unlimited RPD)
    # - "gemma-4-26b-a4b-it"    (Gemma 4 MoE 26B, Limit 1.5K RPD di Google AI Studio)
    # - "gemma-4-31b-it"        (Gemma 4 Dense 31B, Limit 1.5K RPD di Google AI Studio)
    "gemini_model": "gemini-2.5-flash",              
    
    # Konfigurasi Groq (Super Cepat, Rolling Model = limit gabungan!)
    "groq_api_key": os.environ.get("GROQ_API_KEY", ""),  # isi di .env (lihat .env.example)
    "groq_models": [                                  # Rolling — model mati (qwen3-32b/scout) dihapus pasca Jul 17 2026
        "openai/gpt-oss-120b",                        # ~500 t/s — primary
        "openai/gpt-oss-20b",                         # ~1000 t/s — fast
        "qwen/qwen3.6-27b",                           # multilingual / vision-capable
        "llama-3.3-70b-versatile",                    # hidup s/d ~[date removed]
        "llama-3.1-8b-instant",                       # hidup s/d ~[date removed]
    ],
    
    # Konfigurasi SambaNova (Super Cepat, 48K RPD Gratis)
    "sambanova_api_key": os.environ.get("SAMBANOVA_API_KEY") or "YOUR_SAMBANOVA_API_KEY",
    "sambanova_model": "meta-llama-3.1-8b-instruct",  # atau "meta-llama-3.3-70b-instruct"
    
    "vts_api_port": 8002,                             # Port VTS API
    # Folder model Live2D di VTube Studio (isi di config_local.json) —
    # dipakai untuk baca pose idle halus ArtiIdle*.exp3.json langsung dari model.
    "vts_model_dir": "",
    "vts_plugin_name": "HermesVTuberBridge",
    "vts_developer": "YourDeveloperName",
    "tts_voice": "id-ID-GadisNeural",                 # Indonesian female Edge TTS voice
    "virtual_cable_name": "CABLE Input",
    
    # Konfigurasi YouTube Live Chat (langsung dari YouTube, tanpa extension)
    "youtube_chat_enabled": False,
    "youtube_video_id": "",                          # Isi di config_local.json tiap stream (dari URL: youtube.com/watch?v=INI_VIDEO_ID)

    # Konfigurasi OBS Subtitle (in-process WebSocket server + word-level karaoke renderer)
    "subtitle_enabled": True,                         # Master switch: False mematikan in-process subtitle server & semua broadcast
    "subtitle_status_enabled": True,                  # Toggle independen untuk broadcast_status("speaking"/"idle"); diabaikan saat subtitle_enabled=False
    "subtitle_port": 9991,                            # Port WebSocket untuk subtitle.html OBS Browser Source (subtitle.html default 9991 — jangan drift; insiden dua-sesi 3/8 sempat menulis 9992)

    # Mode Pemicu Percakapan Streamer:
    # - "wake_word"     : Panggil Arti dengan mengucapkan kata kunci "arti" / "eh arti"
    # - "push_to_talk"   : Mic MEREKAM CASUAL PASIF ke sejarah stream, tetapi HANYA merespon jika menekan hotkey!
    "trigger_mode": "push_to_talk",
    "hotkey_key": "mouse_x2",                         # Diatur ke Mouse 5 (Tombol Samping Depan configured mouse device!)
    # ASR mic: None = auto (skip Stereo Mix); atau device id / substring nama mic
    "asr_input_device": None,
    "asr_skip_device_patterns": [
        "stereo mix", "wave out", "what u hear", "loopback", "virtual cable", "cable output",
    ],
    # Cap VAD threshold — kalibrasi saat health check overlap bisa naik >0.5 dan mic "mati"
    "asr_silence_threshold_max": 0.12,
    "memory_max_bullets": 30,
    "health_check_on_startup": True,
    "health_mic_watch_sec": 5.0,
    # DIGANTI [date removed]: Groq mematikan `llama-3.1-8b-instant` pada
    # [date removed] (diumumkan [date removed]). Ini slot TERPANTING di jalur suara —
    # `mic`/`ptt` tidak lewat composer, dan smart routing memilih `fast`
    # untuk 7 dari 8 kalimat khas operator ("halo", "wkwk", "oke lanjut").
    # Pengganti resmi Groq, dan kebetulan LEBIH CEPAT: 1.000 t/s vs 560 t/s.
    # Output lebih mahal ($0,30 vs $0,08 per 1 jt) — dibayar demi tidak bisu.
    # Dipilih juga karena punya TIGA sumber independen (Groq berbayar,
    # OpenRouter :free, Ollama Cloud gratis) — lihat docs/MODEL-REGISTRY.md.
    "groq_model_fast": "openai/gpt-oss-20b",

    # Smart Groq routing per turn (v0.6.2 — restore dari checkpoint [date removed]):
    # pilih model by kompleksitas pertanyaan, bukan round-robin buta.
    "smart_groq_routing": True,
    "groq_model_medium": "openai/gpt-oss-20b",        # sapaan panjang / casual
    "groq_model_strong": "qwen/qwen3.6-27b",          # pertanyaan "kenapa/jelaskan"
    "groq_model_rare": "openai/gpt-oss-120b",         # pertanyaan panjang & kompleks
    "groq_prompt_char_soft_cap": 10000,               # prompt > ini → paksa model fast
    "groq_roll_all_models_on_limit": True,            # 429/5xx → coba model Groq lain dulu

    # --- Otak Cursor Composer 2.5 untuk chat YT (v0.6.3) ---
    # OFF (default) = jalur provider persis seperti v0.6.2-stable. Nyalakan di
    # config_local.json. Kalau Cursor lambat/gagal/kuota habis, otomatis jatuh ke
    # Groq → OpenRouter → jawaban in-character. Arti tidak pernah bisu.
    "cursor_agent_enabled": False,           # KILL SWITCH UTAMA
    "cursor_trigger_types": ["yt_chat"],     # mic TETAP Groq (butuh instan); curious boleh ditambah via config_local
    "cursor_model": "composer-2.5",
    # fast=False disengaja: Fast berharga 6x ($3,00/$15,00 vs $0,50/$2,50 per juta
    # token) untuk selisih ~0,5 detik, dan modelnya identik — Fast cuma hardware yang
    # lebih mahal. Terukur: fast p50 2,66 dtk vs standard 3,22 dtk.
    "cursor_fast_param": False,
    # WAJIB diisi di config_local.json: folder KOSONG di LUAR repo. SDK tidak punya
    # cara mematikan tool, jadi isolasi cwd adalah satu-satunya mitigasi.
    "cursor_scratch_dir": "",
    # 16.0 (riwayat: 7.0 -> 10.0 sore [date removed] -> 12.0 seharian [date removed]
    # -> 16.0 malam [date removed], keputusan operator). Angka ini dipakai DUA KALI:
    # deadline internal saat mengkonsumsi stream, DAN dasar asyncio.wait_for
    # (+1 detik). Ramalan komentar versi 12.0 ("kalau max sukses nempel lagi
    # di 12, masalahnya di composer yang melambat") TERJADI: tiga sesi malam
    # 17-[date removed] berturut-turut composer butuh 12-16 dtk pada sesi HANGAT
    # (llm p90 15,3 dtk; sesi [time removed]: 1 sukses vs 13 timeout, breaker 4x)
    # PADAHAL prompt sudah didiet (sukses yang ada 6-11 dtk). 16,0 menutup
    # p90 terukur; harga ~4 dtk ekstra diterima operator ("yang 12 jadi 16
    # boleh"). Kalau max sukses nempel lagi di 16 — itu composer, bukan kapak.
    "cursor_timeout_sec": 16.0,
    # ---- Otak agy persisten / Google AI Pro ([date removed]) ----
    # Default MATI. Saat dua saklar aktif, hanya trigger percakapan di bawah
    # yang mencoba agy; donation/video/game tetap mengikuti Luna lama.
    "agy_agent_enabled": False,
    "agy_primary_voice": False,
    "agy_trigger_types": ["mic", "ptt", "yt_chat", "curious"],
    "agy_bin": os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe"),
    "agy_model": "gemini-3.7-flash-low",
    "agy_effort": "low",
    "agy_init_timeout_sec": 15.0,
    "agy_timeout_sec": 8.0,
    "agy_thread_max_turns": 20,
    # ---- Kolam premium KEDUA: Codex/ChatGPT Plus ([date removed]) ----
    # Lapis ANTARA composer dan Groq di giliran suara: composer gagal/
    # breaker tutup -> Luna (thread hangat 1,5-3 dtk, probe tercatat di
    # docs/research/[date removed]-codex-chatgpt-plus.md) -> baru Groq.
    # Default MATI: ToS abu-abu — menyalakan = keputusan sadar di
    # config_local. KOREKSI kuota (operator measurement [date removed]): pemakaian
    # Codex TIDAK memotong kuota chat ChatGPT ("doesn't include Chat
    # conversations") — kolamnya weekly, dibagi hanya dengan Codex/Work/
    # agents; jendela 5-jam sedang TIDAK berlaku (bisa dibalikin OpenAI
    # kapan saja — kalau Luna mendadak sering menolak, cek /status dulu).
    # HARAM untuk kerja latar (analog aturan #2 — kolamnya tetap terbatas).
    "codex_agent_enabled": False,
    # Eksperimen operator [date removed] ("aku mau liat Luna seboros apa"): jadikan
    # Codex/Luna model SUARA UTAMA satu sesi — dicoba SEBELUM composer,
    # composer turun jadi cadangan (lalu Groq). Probe [date removed]: Luna hangat
    # 1,5-3,3 dtk vs composer 7-10 dtk. Butuh codex_agent_enabled=true.
    # JANGAN nyalakan permanen tanpa lihat bar kuota weekly sesudah sesi.
    "codex_primary_voice": False,
    "codex_model": "gpt-5.6-luna",
    # 'low' terverifikasi probe: reasoning_output_tokens=0 — nalar tidak
    # memakan budget/latensi. Persis kebijakan Groq gpt-oss (dan beda dari
    # OpenRouter free yang mengabaikan effort).
    "codex_effort": "low",
    # Effort NAIK khusus kelas jawaban berat (usul operator [date removed]). Terukur
    # hari ini: untuk jawaban PANJANG effort nyaris tak berpengaruh (8,54
    # dtk di low lawan 9,01 dtk di xhigh) — waktunya habis mengarang
    # kalimat, bukan berpikir. Jadi menaikkannya di kelas berat itu murah.
    #
    # Bawaannya "high", BUKAN "xhigh": ekor xhigh terukur 26,24 dtk
    # sementara codex_timeout_sec cuma 12 — giliran `deep` justru giliran
    # paling berharga dan paling mungkin terbuang ke Groq. Maks `high`
    # terukur 11,54 dtk, masih di bawah pagar.
    "codex_effort_berat": "high",
    "codex_effort_kelas_berat": ["deep", "rant"],
    "codex_timeout_sec": 8.0,
    "codex_thread_max_turns": 20,
    "codex_bin": "",
    # Kosong = dibuat otomatis di TEMP. Luna selalu dijalankan dari folder
    # kosong di luar repo agar repo tidak menjadi workspace/konteks bawaannya.
    # Sandbox read-only + deny-all juga mencegah tulis, network, dan eskalasi;
    # SDK tetap mengizinkan read absolut, jadi instruksi no-tools adalah pagar
    # tambahan, bukan klaim isolasi kriptografis. Untuk nol akses file: Luna OFF.
    # Kalau diisi, folder wajib sudah ada, kosong, dan berada di luar repo.
    "codex_scratch_dir": "",
    # Trigger berharga (video/donation) saat sesi dingin: tunggu pemanasan
    # sampai sekian detik alih-alih jatuh ke Groq 8B — konten tak tergantikan
    # (digest video, terima kasih donatur), tidak ada yang diburu waktu.
    "cursor_warmup_wait_precious_sec": 45.0,
    # Daur ulang sesi BUKAN soal kecepatan: pembengkakan konteks terukur cuma
    # 1,05x/20 turn (docs/CURSOR-SDK-SPIKE.md). Alasannya VARIASI — sesi hangat
    # menjawab pertanyaan mirip nyaris verbatim. Sejak [date removed] penggantinya
    # dipanaskan lebih dulu (tukar panas), jadi daur ulang tidak lagi berarti
    # 13-20 detik penonton mendengar Groq.
    "cursor_session_max_turns": 20,
    "cursor_session_max_age_sec": 1800,
    # Sejauh apa sebelum batas, cadangan mulai dipanaskan. 3 turn / 120 detik
    # cukup: pemanasan terukur 13-20 detik.
    "cursor_standby_lead_turns": 3,
    "cursor_standby_lead_sec": 120,
    "cursor_reject_on_tool_call": True,      # agen manggil tool = melenceng → Groq
    "cursor_max_consecutive_failures": 3,    # breaker: tutup Cursor setelah N gagal beruntun
    # Breaker half-open: setelah sekian detik, coba Cursor lagi. Penting untuk live
    # yang DITINGGAL seharian — tanpa ini, satu gangguan sekejap (3 gagal instan)
    # mematikan Composer untuk sisa hari. 0 = permanen sampai restart (perilaku lama).
    # 300, turun dari 900 (live [time removed], [date removed]): breaker tutup 15 MENIT
    # berarti 15 menit siaran dijawab llama-3.1-8b — operator langsung merasakan
    # ("kok terasa bego banget ya"). Biaya terburuk cooldown pendek adalah 3
    # percobaan gagal-cepat tiap 5 menit; itu jauh lebih murah daripada
    # kehilangan composer seperempat jam di depan penonton.
    "cursor_breaker_cooldown_sec": 300,
    "cursor_last_resort_incharacter": True,
    "cursor_api_key": os.environ.get("CURSOR_API_KEY", ""),
    # Sesi Cursor per-role (keputusan operator [date removed]: Cursor tulang punggung
    # semua otak, chain API gratis jadi fallback). Aktif kalau "cursor" ada di
    # scouter/observer/vision_provider_chain — chain shipped SENGAJA tanpa
    # cursor (repo publik; nyalakan lewat config_local). Verifikasi model/param:
    # scripts/spike_grok_vision.py — grok-4.5 punya effort low/medium/high,
    # composer-2.5 TIDAK punya effort (jangan diisi), default variant = FAST.
    # Revisi operator [date removed] (CSV usage: grok-high 23,5M token = 49% konsumsi
    # sehari, 671 call scouter): scouter turun ke composer non-fast — "composer
    # 2.5 NOT FAST is enough". ([date removed]: observer & lookup ikut composer.)
    "cursor_scout_model": "composer-2.5",
    "cursor_scout_effort": "",
    # Revisi operator [date removed] ("grok 4.5 gausah dulu, composer is smart
    # enough") — KETAHUAN crosscheck: default resolve_role_model sudah
    # composer, tapi DUA key CONFIG ini masih menimpanya ke grok diam-diam.
    "cursor_observer_model": "composer-2.5",
    "cursor_observer_effort": "",
    "cursor_scout_timeout_sec": 30.0,     # grok cold ~19 dtk; scouter jalan di thread sendiri
    "cursor_vision_model": "composer-2.5",
    "cursor_vision_effort": "",           # composer: param fast saja
    "cursor_vision_timeout_sec": 45.0,    # cold + gambar terukur 35,6 dtk — jangan diturunkan

    # Batas jawaban Arti (dipakai post_process_response + get_arti_reply_limits)
    # Rant mode (permintaan operator [date removed]): saat chat sepi (jeda antar pesan
    # >= yt_quiet_after_sec), ~10% pertanyaan non-deep dijawab panjang 6-8
    # kalimat. Dadu deterministik per teks pesan (lihat arti_reply_policy).
    "yt_quiet_after_sec": 75.0,
    "arti_reply_rant_chance": 0.10,
    "arti_reply_rant_min_sentences": 6,
    "arti_reply_rant_max_sentences": 8,
    "arti_reply_rant_chars_cap": 900,
    "arti_reply_max_sentences": 5,
    # Giliran inisiatif/curious = Arti membuka topik sendiri. Itu celetukan,
    # bukan penjelasan — 1 kalimat (permintaan operator [date removed]). Sumber terbesar
    # jawaban panjang: 25 dari 44 giliran sesi [date removed] berasal dari curious.
    "arti_reply_inisiatif_max_sentences": 1,
    # Ucapan operator dinilai dengan ladder yang sama seperti chat YT (pendek
    # untuk celetukan, panjang untuk pertanyaan bermakna). False = kembali ke
    # batas datar arti_reply_max_sentences.
    "arti_reply_streamer_adaptive": True,
    "arti_reply_max_chars": 580,
    "live_max_tokens_ptt": 380,

    # Konteks LLM ringkas (bukan dump 50 baris history)
    "llm_history_streamer_max": 3,
    "llm_history_viewer_max": 3,
    "llm_history_arti_max": 2,
    "llm_viewer_profile_max_chars": 400,
    "viewer_context_max_messages": 8,
    "viewer_context_streamer_tail": 2,
    "viewer_context_arti_tail": 2,

    # Nama streamer untuk fallback in-character (isi di config_local.json)
    "streamer_name": "",

    # Trigger via ketikan di console bridge (tanpa mic — AFK/remote).
    # Ketik pesan + Enter; "yt pesan" / "yt Nama: pesan" = simulasi chat YT.
    "text_input_enabled": True,
    "yt_default_viewer": "",  # handle YT kamu untuk "yt pesan" (config_local.json)

    # YT chat queue FIFO (v0.6.2) — default OFF sampai lolos live smoke.
    # ON: chat YT diantri (prioritas > mic > curious) bukan di-drop saat busy;
    # cooldown per viewer (bukan global 20s). OFF: perilaku stable persis.
    "voice_queue_enabled": False,
    "yt_chat_queue_max": 2,
    "yt_chat_queue_ttl_sec": 60.0,
    "yt_chat_cooldown_sec": 10.0,
    # Anti-spam per PENONTON (aktif di SEMUA mode, bukan cuma queue): satu
    # orang yang mengetik "arti ..." beruntun cuma dilayani sekali per jendela
    # ini; penonton lain tidak ikut kena. Permintaan operator [date removed] sesudah
    # live pertama ("bisa diterapkan cooldown biar viewer ga spam si arti").
    "yt_viewer_cooldown_sec": 45.0,
    # Penonton BARU (nama yang belum pernah muncul sesi ini) disapa dari pesan
    # pertamanya TANPA harus menyebut "arti" — momen selamat-datang itu justru
    # yang paling berharga. Tetap dipagari cooldown global & per-viewer.
    "yt_greet_new_viewers": True,
    # Chat santai (operator [date removed]: "pasti pada males kalau ketik arti"):
    # pesan manusiawi TANPA wake word ikut memicu jawaban, lewat keran global
    # sendiri yang lebih longgar (gap di bawah) supaya Arti tidak berubah jadi
    # mesin penjawab semua chat — wake word tetap jalur prioritas (gap 20 dtk
    # lama), dan cooldown per-viewer 45 dtk tetap berlaku untuk semua jalur.
    # Pesan yang tertahan keran TETAP masuk history, jadi jawaban berikutnya
    # masih melihatnya sebagai konteks.
    "yt_chat_santai_enabled": True,
    "yt_chat_santai_gap_sec": 60.0,
    # Bot layanan chat — pesan mereka boleh tercatat di history (konteks
    # leaderboard dll), tapi TIDAK PERNAH mentrigger jawaban. Live 11,5 jam
    # [date removed]: @Streamlabs posting leaderboard dan sempat diantri jawab.
    "yt_bot_viewers": ["Streamlabs", "Nightbot", "StreamElements", "Moobot", "Fossabot"],
    "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", ""),
    # OpenRouter model slugs — lihat docs/OPENROUTER_MODELS.md
    # Diperbarui [date removed]: `poolside/laguna-xs.2:free`, `poolside/laguna-m.1:free`,
    # dan `owl-alpha` semuanya 404 ("No endpoints found") — slug poolside di-rename
    # jadi `laguna-xs-2.1`, `owl-alpha` hilang total.
    # PENTING: poolside = model reasoning. Terverifikasi probe — pada max_tokens 110/350
    # ia mengembalikan content KOSONG (finish=length, CoT menghabiskan budget); baru keluar
    # jawaban di ~600 token. Jalur live pakai max_tokens 110-320 (_TOKENS_BY_SENT di
    # arti_reply_policy.py:55), jadi poolside HARAM di sini — itu bug "Jawaban AI kosong"
    # yang sama seperti qwen3.6 (fix dd88d9e). Pakai model non-reasoning yang finish=stop.
    # DITUKAR [date removed] (crosscheck pra-siaran, n=6 prompt identik per model).
    # nemotron-3-super BUKAN cuma lambat — dia membocorkan CoT Inggris ke penonton,
    # dan `clean_ai_reply` TIDAK menangkapnya (kalimatnya lolos gerbang
    # >=4 kata Inggris DAN rasio >=0,6). Satu bocoran bahkan membacakan system
    # prompt: "We need to answer as Kamu Arti, co-host VTuber cewek Indonesia...".
    #
    #   model                              bersih  bocor-CoT  latensi median
    #   nvidia/nemotron-3-super-120b-a12b     4/6      2/6       16.447 ms
    #   nvidia/nemotron-3.5-lightning         0/6      6/6        2.732 ms  <- terbaru, TERPARAH
    #   google/gemma-4-26b-a4b-it             6/6      0/6        2.799 ms  <- dipilih
    #
    # Ini jaring pengaman giliran SUARA (mic/ptt tidak lewat composer), jadi
    # kualitasnya menentukan seberapa buruk hari buruk. Sesudah Groq mematikan
    # llama-3.1-8b-instant ([date removed]) jalur ini dipakai JAUH lebih sering.
    # nemotron-3-super tidak dibuang, cuma turun jadi cadangan terakhir.
    "openrouter_live_model": "google/gemma-4-26b-a4b-it:free",
    "openrouter_live_last_resort": "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter_live_fast_only": True,
    "openrouter_live_fallback_enabled": True,
    "openrouter_live_timeout_sec": 45,
    # Scouter/summarizer pakai max_tokens 350 (scouter_max_tokens) — masih terlalu
    # ketat untuk poolside, jadi non-reasoning juga.
    "openrouter_summarizer_model": "nvidia/nemotron-3-super-120b-a12b:free",
    # nano-30b diganti lightning [date removed] — nano menghalusinasi fakta ke
    # important_facts di probe (alasan lengkap di scouter_openrouter_models).
    "openrouter_summarizer_fallback": "nvidia/nemotron-3.5-lightning:free",
    "openrouter_reflection_model": "nvidia/nemotron-3-super-120b-a12b:free",
    # Reflection pakai max_tokens=2000 (arti_openrouter.py:445) — budget longgar,
    # jadi poolside aman di slot terakhir sekaligus menjaga keragaman vendor.
    "openrouter_reflection_fallback_model": "google/gemma-4-26b-a4b-it:free",
    "openrouter_reflection_last_resort": "poolside/laguna-xs-2.1:free",
    "openrouter_reflection_ultra_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "reflection_try_ultra": False,

    # Vault RAG — top-k chunk per pertanyaan (bukan dump semua learnings ke prompt)
    "vault_rag_enabled": True,
    "vault_rag_live_enabled": True,
    "vault_rag_live_timeout_sec": 8,
    "vault_rag_lite_enabled": True,
    "vault_rag_db_path": "data/vault_rag.db",
    "lmstudio_embedding_base_url": "http://localhost:1234/v1",
    "lmstudio_embedding_model": "text-embedding-mxbai-embed-large-v1",
    "lmstudio_embedding_timeout_sec": 8,
    # 5 -> 2 (diet bahan, paket kohesi [date removed]): top-5 menyuntik 1,8-2,3 KB
    # per giliran dan ikut mendorong composer menabrak kapak 12 dtk (malam
    # [date removed]: sukses composer tinggal 2x semalam). Dua hit terbaik cukup;
    # kualitas fokus > banjir bahan.
    "vault_rag_top_k": 2,
    # 2400: header instruksi RAG tumbuh 76 -> 172 char (aturan konflik tanggal), dan itu
    # overhead tetap. Tanpa kenaikan ini hit ke-5 tergusur. Lihat arti_vault_rag.py.
    "vault_rag_max_context_chars": 2400,
    "vault_rag_reindex_on_shutdown": True,
    # 0 = tunggu reindex TUNTAS saat shutdown (operator [date removed]: jangan ada kerja
    # diam-diam saat "Terminate batch job" muncul). >0 = batas detik (perilaku lama).
    "vault_rag_reindex_shutdown_timeout_sec": 0,
    # Catch-up saat start: menyembuhkan reindex shutdown yang terpotong (thread
    # daemon mati bersama proses — insiden [date removed], DB berhenti 624/747 chunk).
    "vault_rag_reindex_on_startup": True,
    # Catch-up observer saat start (operator [date removed]: "beberapa sesi kemaren
    # ada yang ga ke summarize"): sesi yang mati tanpa shutdown bersih
    # (force close/crash/OOM) meninggalkan transcript tanpa beats — ekornya
    # dirangkum di startup berikutnya, INKREMENTAL (segmen yang sudah punya
    # beat tidak diulang; learnings tidak dobel). Sesi berjalan tidak disentuh.
    "observer_catchup_on_startup": True,
    "observer_catchup_delay_sec": 90.0,     # beri startup ruang napas dulu
    "observer_catchup_max_days": 7,
    "observer_catchup_margin_sec": 120.0,
    # Jeda antar segmen saat catch-up — backlog perdana BESAR (audit
    # [date removed]: 6 hari ekor sesi malam tak pernah dirangkum, [date removed]
    # bahkan nol beats) dan catch-up jalan BERSAMAAN dengan live; tanpa rem
    # dia berebut provider dengan turn Arti. Inkremental + resumable, jadi
    # pelan itu aman: terpotong pun lanjut di startup berikutnya.
    "observer_catchup_pause_sec": 3.0,
    # Model rangkuman catch-up saat LIVE = chain GRATIS tanpa cursor.
    # REGRESI TERUKUR live [time removed] ([date removed]): catch-up 17 segmen (12-24
    # dtk/segmen) memakai composer BERSAMAAN dengan giliran suara ->
    # giliran voice timeout 3x -> breaker Cursor tutup 900 dtk -> SELURUH
    # sesi dijawab llama-3.1-8b ("kok terasa bego banget ya"). composer
    # sukses NOL kali. Kerja latar belakang tidak boleh merebut jalur yang
    # dipakai penonton. Chain gratis = tetap hemat (bahkan lebih), nol
    # kontensi. Catch-up dengan composer tetap tersedia SAAT BRIDGE MATI
    # lewat scripts/rangkum_susulan.py (semalam sukses 62 beat).
    # "github" DIBUANG [date removed] — GitHub Models pensiun total 30 Juli 2026
    # (HTTP 410 github_models_retirement_brownout, diverifikasi dengan
    # memanggilnya). Rinciannya di docs/MODEL-REGISTRY.md §2.1.
    "observer_catchup_provider_chain": [
        "google_gemini", "cloudflare", "openrouter", "zai", "nvidia",
    ],
    "observer_catchup_cursor_role": "catchup",
    # Cicil, jangan gempur: maksimal segini segmen per startup. Backlog
    # besar habis dalam beberapa kali nyala, bukan satu sesi penuh.
    "observer_catchup_max_segmen": 8,
    "memory_startup_max_bullets": 5,
    # 6500, naik dari 5500 ([date removed]). Terukur di sesi live: prompt rakitan penuh
    # (BASE 3851 + origin 231 + memori 346 + mood 23 + viewer 908 + emotion 190) =
    # 5549 — lebih 49 char dari cap lama, dan penaltinya TIDAK proporsional: trim
    # membuang seluruh blok [MEMORI JANGKA PANJANG (369 char berikut instruksi
    # "boleh cerita cara kerjamu") di TIAP turn. Viewer block tumbuh seiring
    # ARTI_VIEWERS.md bertambah (23 baris sekarang), jadi beri ruang. Biaya ~250
    # token/turn di model 131k ctx — murah dibanding kehilangan instruksi diam-diam.
    # 11000, naik dari 6500 ([date removed], dua tahap). ARTI_SOUL.md digemukkan
    # 4,2 KB -> 8,3 KB (selera konkret + fakta kanon hasil tambang 38 hari
    # hidup + running bits + gaya dialog — proyek "biar kayak manusia")
    # sehingga rakitan penuh ~9,6 KB. Penalti trim TIDAK proporsional: yang
    # terpotong justru blok memori jangka panjang (kejadian pra-6500). Voice
    # sekarang composer (context 200k) dan fallback chain gratis pun aman di
    # ~2,75k token, jadi plafon longgar lebih murah daripada kehilangan ingatan.
    "llm_system_prompt_max_chars": 11000,

    # Gerakan dialog giliran curious ([date removed]) — PARAMETER, bukan hardcode
    # (permintaan operator: "kenapa ga jadi semacam parameter negatif"). Timpa dari
    # config_local.json tanpa menyentuh kode; restart bridge untuk memuat ulang.
    # None = pakai DEFAULT_GERAKAN_DIALOG / DEFAULT_LARANGAN di arti_curious.
    # [] pada gerakan = fitur mati; "" pada larangan = tanpa larangan.
    # Aturan gaya yang paling sering di-tune tetap di ARTI_SOUL.md (live tanpa
    # restart) — dua kunci ini untuk bentuk giliran proaktif saja.
    "curious_gerakan_dialog": None,
    "curious_larangan": None,

    # Fase 1 — transcript JSONL + vault slim (v0.5.2)
    "stream_session_id": "",
    "transcript_dir": "transcripts",
    # Berapa log SUBSTANSIAL yang tinggal di session_logs/ (sisanya pindah ke
    # archive/v0.4/session_logs/). Naik 5 -> 15 ([date removed]): dengan 5, log
    # siaran semalam sudah tergeser sebelum sempat dibaca.
    "session_log_keep_n": 15,
    # Di bawah ukuran ini = bukan sesi sungguhan (bridge gagal start, harness),
    # tidak ikut memakan jatah keep_n. Lihat session_transcript.rotate_session_logs.
    "session_log_min_bytes": 20000,
    "transcript_flush_fsync": True,

    # Konfigurasi Supertone 3 TTS (dual-engine: master switch + parameter sintesis lokal)
    "tts_engine": "supertone",                        # "supertone" | "edge_tts" — master engine switch
    # Sensor komedi keluaran: swap token tepat sebelum TTS, subtitle, history,
    # dan chat game. Default OFF sesuai pagar fitur baru; mesin operator menyalakan
    # lewat config_local. Tidak melakukan request/audio tambahan.
    "speech_censor_enabled": False,
    "speech_censor_replacement": "sensor",
    "speech_censor_words": list(arti_speech_censor.DEFAULT_BLOCKED_WORDS),
    "tts_preprocess_numbers": True,                   # Jalankan konversi angka→kata Indonesia sebelum sintesis
    "supertonic_voice": "F1",                         # Voice style: F1-F5 / M1-M5 (F1 disarankan)
    "supertonic_speed": 1.1,                          # tuned for live; 1.3 was too fast
    "supertonic_lang": "id",                          # Kode bahasa Supertone
    "supertonic_total_steps": 10,                     # Max quality [5–12] — F1 live (10 = stabil + cepat)
    # Jalankan Supertonic di GPU (v0.6.3). Terukur [date removed] di NVIDIA GPU:
    # CPU p50 29,4 detik/kalimat -> CUDA p50 2,0 detik. ~15x.
    # Default False supaya repo publik tidak mencoba CUDA di mesin tanpa runtime-nya;
    # nyalakan di config_local.json. Kalau CUDA gagal, supertonic/loader.py jatuh ke CPU
    # sendiri (terverifikasi) — Arti tetap bersuara, cuma lambat.
    # Butuh di venv312: onnxruntime-gpu + paket nvidia-* (lihat requirements-supertone.txt).
    "supertonic_use_cuda": False,
    # Polesan suara ([date removed]) — resep hasil uji dengar operator atas 15+
    # varian (dump/uji_pitch, berkas pemenang: E3_range_x14_plus2):
    # range intonasi dilebarkan 1,4x (PSOLA, F0' = median + k*(F0-median)) +
    # pitch & warna naik 2 semitone (sinc resample; kompensasi durasi di
    # speed sintesis — net tetap supertonic_speed). Latar: suara F1 datar
    # "kayak pembawa berita", penonton bilang robotik; Supertonic tidak punya
    # kenop pitch, dan F1/speed/steps SUDAH terkunci oleh tes operator.
    # Netralkan (0 / 1.0) dari config_local untuk kembali ke suara lama.
    # Butuh praat-parselmouth (requirements.txt); tanpa itu otomatis mati.
    "supertonic_pitch_semitone": 2.0,
    "supertonic_range_factor": 1.4,
    "supertonic_prewarm_on_startup": True,            # Load model saat startup, hindari timeout jawaban pertama
    "supertonic_timeout_sec": 45.0,                   # Sintesis per-utterance (was 20s — sering timeout)

    # NVIDIA auxiliary (DiffusionGemma via NIM) — default OFF; main LLM stays groq
    "nvidia_api_key": os.environ.get("NVIDIA_API_KEY", ""),
    "nvidia_model": "google/diffusiongemma-26b-a4b-it",
    "screen_context_enabled": True,
    "screen_context_interval_sec": 10.0,
    "screen_context_max_chars": 200,
    "vision_enabled": True,
    "vision_runtime_on_start": False,
    "vision_hotkey_key": "mouse_x",
    "vision_background_poll": False,
    "vision_refresh_sec": 10,
    "vision_stale_sec": 30,
    "vision_provider_chain": [
        "google_gemini_lite",  # 160/167 = 95% di log 9 jam [date removed]
        "openrouter",          # 6/9; incumbent cadangan paling terukur
        "ollama",              # 3/3 di log [date removed]
        "google_gemma",        # thinking OFF [date removed]: probe JSON 3,7 dtk
        "cloudflare",          # thinking OFF [date removed]: probe JSON 2,0 dtk
        "zai",                 # 0/3 di log [date removed]
        "nvidia",              # 0/3, ReadTimeout 60 dtk penuh tiap kali
    ],
    # 512, naik dari 256 ([date removed]). Kontrak vision baru (scene 1-2 kalimat
    # spesifik + hook + playback + ocr) tidak muat di 256 token: terpantau di sesi
    # live JSON-nya KEPOTONG di tengah -> gagal parse -> JSON mentah tersimpan
    # sebagai scene dan ikut tersuntik ke prompt Arti sebagai [LAYAR: { "scene"...].
    # Pagu TOTAL refresh vision yang memblokir turn (bukan timeout per provider).
    # Live [date removed]: tanpa pagu, nvidia lemot (read timeout 60s) + fallback
    # cursor menyandera turn sampai 106 detik.
    "vision_turn_budget_sec": 15.0,
    "vision_max_tokens": 512,
    "vision_scene_max_chars": 300,
    "vision_ocr_max_chars": 200,
    "vision_capture_max_width": 1280,
    "vision_capture_jpeg_quality": 75,
    # GERBANG LAYAR-DIAM (operator [date removed]: "dia liatin layar terus... bikin
    # threshold, kalau perubahannya cuma berapa persen gak perlu
    # dikomentarin"). Arti menonton dirinya sendiri -> frame nyaris identik
    # tiap giliran, tapi vision tetap dipanggil dan [LAYAR:] tetap disuntik
    # (log [time removed]: auto-vision 157x, 49/211 jawaban menyebut layar).
    # sel_berubah_min_persen: metrik UTAMA (persen sel 32x18 yang berubah
    # tajam) — menangkap perubahan LOKAL. Ukur nyata di layar operator [date removed]:
    # derau layar diam 0,0-0,9% vs subtitle satu baris 5,2%, popup kecil 3,1%.
    # beda_min_persen: cadangan untuk perubahan GLOBAL (ganti scene/fade).
    # Gerbang buka kalau SALAH SATU lolos. Ambang rata-rata SAJA salah alat:
    # subtitle cuma menggeser rata-rata 2,0% -> teks penting akan dibungkam.
    # diam_stop_inject: setelah N tangkapan diam berturut-turut, [LAYAR:]
    # berhenti disuntik ke prompt sampai layar berubah lagi. 0 = mati.
    "vision_sel_berubah_min_persen": 2.0,
    "vision_beda_min_persen": 6.0,
    "vision_diam_stop_inject": 2,
    "vision_gelap_luma_max": 12.0,
    "vision_gelap_sebar_min": 6.0,
    "vision_temperature": 0.2,
    "vision_nvidia_model": "google/diffusiongemma-26b-a4b-it",
    "vision_google_gemma_model": "gemma-4-26b-a4b-it",
    "vision_google_gemma_fallback_model": "gemma-4-31b-it",
    "vision_google_gemini_model": "gemini-3.1-flash-lite",
    # Rem kuota Gemini free tier ([date removed]) — sesi [date removed]: 186x HTTP 429
    # karena google_gemini di posisi 1 rantai ditembak tanpa ingatan kuota.
    # Google tidak mempublikasikan angka free tier lagi; pagar default di
    # bawah angka historis flash-lite (15 RPM / 250k TPM). Berlaku per model
    # untuk SEMUA pemanggil Gemini (scouter, vision, observer, video) lewat
    # pintu arti_gemini_vision. Rincian: arti_gemini_budget.py.
    "gemini_rpm_budget": 12,
    "gemini_tpm_budget": 200000,
    "gemini_429_cooldown_sec": 60.0,
    "vision_cloudflare_model": "@cf/google/gemma-4-26b-a4b-it",
    "vision_openrouter_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    # `meta-llama/llama-4-scout-17b-16e-instruct` MATI (404, terverifikasi [date removed]).
    # Dari 15 model Groq yang tersisa, HANYA qwen3.6-27b yang menerima gambar — sisanya
    # menolak dengan "messages[0].content must be a string". Butuh reasoning_effort=none;
    # lihat arti_vision_client._call_groq.
    # Catatan: "groq" TIDAK ada di vision_provider_chain default, jadi ini cadangan saja.
    "vision_groq_model": "qwen/qwen3.6-27b",
    "vision_github_model": "meta/llama-3.2-11b-vision-instruct",
    "vision_github_enabled": False,
    "vision_zai_model": "glm-4.6v-flash",
    "zai_api_key": os.environ.get("ZAI_API_KEY", "") or os.environ.get("ZHIPU_API_KEY", ""),
    "vision_ollama_model": "gemma4:31b-cloud",
    "ollama_api_key": os.environ.get("OLLAMA_API_KEY", ""),
    "cloudflare_api_token": os.environ.get("CLOUDFLARE_API_TOKEN", ""),
    "cloudflare_account_id": os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
    "github_models_token": os.environ.get("GITHUB_TOKEN", ""),
    "curious_enabled": True,
    # False sejak [date removed] (lapis B "benang obrolan"): giliran proaktif justru
    # yang paling butuh ingatan — callback ke sesi lama itu inti "kayak
    # manusia", dan tidak ada penonton yang menunggu giliran proaktif, jadi
    # +2 dtk embedding di situ gratis. True = perilaku lama (skip RAG).
    "curious_skip_rag": False,
    # Timeout RAG khusus curious. vault_rag_live_timeout_sec (1 dtk) SELALU
    # kalah dari overhead tetap embedding LM Studio ~2,1 dtk — memakainya
    # berarti RAG curious mati diam-diam 100% walau skip_rag sudah False.
    "vault_rag_curious_timeout_sec": 5.0,
    "curious_interval_sec": 75,
    "curious_cooldown_sec": 120,
    "curious_requires_fresh_screen": True,
    # Inisiatif topik saat hening (Fitur A v0.7) — jalur saudara curious TANPA
    # syarat layar: 30 dtk tanpa chat/omongan streamer -> Arti buka topik dari
    # memorinya. Anti-cerewet: backoff eksponensial 180s ->dobel-> cap 720s
    # selama tidak ada manusia menimpali. Default OFF (nyalakan di config_local).
    "initiative_enabled": False,
    "initiative_quiet_sec": 30.0,          # sejak ARTI terakhir bicara
    "initiative_streamer_gap_sec": 5.0,    # sejak streamer bersuara apapun (anti-motong)
    "initiative_backoff_base_sec": 180.0,  # <=0 = flat tiap quiet_sec (setelan operator)
    # Sejauh apa ke belakang omongan streamer masih dianggap "baru saja" —
    # membuat giliran inisiatif jadi NYAMBUNG, bukan buka topik baru.
    # 45 dtk: satu giliran Arti sendiri makan 25-30 dtk (live [date removed]), jadi
    # ambang lebih pendek bikin dia lupa operator barusan ngomong tepat setelah
    # dia selesai menjawab. Bandingkan initiative_streamer_gap_sec (5.0) yang
    # cuma pagar anti-MOTONG — itu soal kapan boleh bicara, ini soal APA.
    "initiative_nyambung_sec": 45.0,
    "initiative_backoff_max_sec": 720.0,
    # Semua provider gagal saat turn curious (Groq 429 + jalur Cursor tutup):
    # rehat sekian detik, JANGAN nembak lagi tiap cadence. Live seharian
    # [date removed]: 80x "Semua provider gagal" dalam 2,3 jam — tiap percobaan =
    # rentetan request 429 baru yang memperparah habisnya kuota.
    "initiative_provider_fail_backoff_sec": 300.0,
    # Detektor kehidupan (revisi spek operator [date removed], ganti "ruangan kosong
    # = Arti banyak ngomong"): tanpa SATU pun tanda manusia (chat viewer /
    # suara streamer / jumlah penonton NAIK) selama sekian detik -> SEMUA
    # jalur proaktif tidur; bangun otomatis saat ada tanda kehidupan.
    # Kasusnya: ~1 jam nol viewer, Arti monolog terus. Spek final operator:
    # "menurun dan dalam 5 menit gaada komentar streamer atau chat apa apa,
    # ya basically off". <=0 = perilaku lama.
    "initiative_dormant_after_idle_sec": 300.0,
    # RENUNGAN (operator [date removed]: "wondering... thinking out loud biar kerekam
    # trus otomatis build jadi reasoningnya sendiri, kayak storytime"): busur
    # mikir multi-giliran yang numpang slot inisiatif — buka pertanyaan ->
    # teori -> uji -> simpul, kesimpulan masuk vault via save_long_term_memory
    # (gerbang fakta_sudah_ada ikut menjaga). Logika di arti_renungan.py.
    # Default OFF (konvensi fitur baru); dinyalakan dari config_local.
    "renungan_enabled": False,
    "renungan_max_langkah": 4,        # buka, teori, uji, simpul
    "renungan_umur_max_sec": 900.0,   # busur ngambang >15 mnt = drop senyap
    "renungan_cooldown_sec": 600.0,   # jeda antar-busur, anti muter-muter
    "renungan_buka_tiap_n": 3,        # buka busur tiap N slot inisiatif (host: N-1)
    "renungan_selang_seling": True,   # duet: renungan gantian dgn celetukan biasa
    # Telemetri jumlah penonton YouTube (innertube updated_metadata, jalur
    # sama dengan chat): penonton NAIK = tanda kehidupan + bahan sapaan.
    "yt_viewer_poll_sec": 30.0,
    # Minecraft — Arti sebagai player di server lokal operator (plan [date removed],
    # Phase 0 GO, Phase 1 = integrasi bridge ini). Nama per-mesin diisi di
    # config_local (bot: arti_berarti, streamer: streamer_test). Shipped OFF;
    # nyalakan minecraft_enabled di config_local, lalu 'mc on' / [MC: join].
    "minecraft_enabled": False,
    "minecraft_host": "127.0.0.1",
    "minecraft_port": 25565,
    "minecraft_bot_name": "Arti",
    "minecraft_streamer_name": "Streamer",
    "minecraft_node_path": "node",
    "minecraft_bot_script": "mc-bot/bot.js",
    "minecraft_status_interval_sec": 10,
    "minecraft_context_ttl_sec": 120.0,
    "minecraft_reaction_cooldown_sec": 60.0,
    # Ritual cek tas: tiap sekian detik (saat aman) dia berhenti, tasnya
    # dibuka di kamera (invsee) beberapa detik, lalu jalan lagi. Permintaan
    # operator [date removed]: "aku gapernah liat dia ngecek inventory". 0 = mati.
    "minecraft_bag_check_sec": 300.0,
    # Mode kamera. Verdict operator live [date removed] malam: orbit "jelek banget,
    # ganggu" -> default kembali "spectate" + F5 otomatis (di bawah). Orbit
    # tetap tersedia lewat config bagi yang mau drone.
    "minecraft_camera_mode": "spectate",
    # F5 otomatis di window klien kamera: ditekan ulang tiap kamera dikunci
    # ulang (join/mati/pindah dimensi) — karena saat itulah perspektif
    # ter-reset ke first-person. 0 = mati. 1 = third-person belakang,
    # 2 = third-person depan.
    "minecraft_kamera_f5_tekan": 1,
    # ROTASI SUDUT KAMERA (operator [date removed] [time removed]: "ga kerasa pernah ganti
    # kamera"). Sesudah orbit-drone ditolak ("jelek banget, ganggu"), kamera
    # jadi third-person statis: benar tapi monoton. Ini menekan F5 berkala
    # supaya perspektifnya berganti belakang <-> depan (depan memperlihatkan
    # wajah/skin Arti) — perubahan sesekali seperti pemain sungguhan, BUKAN
    # kamera yang bergerak terus. 0 = matikan.
    "minecraft_kamera_rotasi_sec": 240.0,
    # Petunjuk memilih window kalau ada LEBIH DARI SATU window Minecraft
    # (mis. operator ikut main): potongan judul window instance kamera.
    "minecraft_kamera_window_hint": "Minecraft",
    # TAKDIR: misi kecil terukur yang selalu satu aktif (desain final [date removed];
    # lengkap di memori arti-takdir-desain). Selesai diumumkan SISTEM.
    "minecraft_takdir_enabled": True,
    # Jeda antar takdir sesudah satu selesai/diumumkan.
    "minecraft_takdir_jeda_sec": 120.0,
    # Ritual pembukaan: begitu join, dia jalan-jalan dulu segini detik TANPA
    # takdir, lalu dipancing bertanya "hari ini mau ngapain?" — baru mesin
    # takdir menyala (permintaan operator: "biar flow smooth").
    "minecraft_pembukaan_sec": 90.0,
    "minecraft_orbit_radius": 5.5,
    "minecraft_orbit_tinggi": 2.5,
    "minecraft_orbit_period_sec": 75.0,
    # 0.2 = 5 teleport/dtk. Makin kecil makin halus tapi makin cerewet ke
    # server; kehalusan visual dinilai mata operator, angka ini tinggal digeser.
    "minecraft_orbit_tick_sec": 0.2,
    "minecraft_max_bot_respawns": 5,
    # Cap heap V8 proses bot (MB). Akar OOM [date removed]: badai alokasi
    # pathfinder saat goal tak terjangkau — di limit 4GB GC keteteran dan
    # bot mati; di cap kecil GC rajin dan stabil. Watchdog bot.js memasang
    # ambangnya sebagai persentase dari limit nyata (v8.getHeapStatistics).
    # 2048, naik dari 1200 (live [time removed]): sesi nyata merayap ke 1147 MB dalam
    # 16 menit (dunia + aktivitas sah, bukan badai — pagar badai bekerja) dan
    # mentok cap; GC thrashing di dekat limit membuat patroli watchdog
    # kelaparan sebelum sempat restart terkendali. 2048 = ruang napas cukup +
    # tier KRITIS (85% = 1741) dapat jendela tembak sebelum thrash dimulai.
    "minecraft_bot_heap_mb": 2048,
    # Umur maksimal reaksi game yang menunggu di antrean. operator [date removed]:
    # "kalo udah 10 detik, udah basi eventnya dan pakai event yang latest" —
    # jadi 10, turun dari 28. Lebih baik diam daripada menarasikan masa lalu;
    # pasangannya aturan latest-wins di _queue_game_reaction (reaksi lama yang
    # belum terucap disapu begitu reaksi baru datang). Kematian dapat 2x umur
    # ini dan pengumuman takdir 3x — dua-duanya cerita yang WAJIB terdengar.
    "minecraft_reaction_ttl_sec": 10.0,
    # POV penonton (Phase 3). prismarine-viewer merender dunia dari mata Arti
    # ke halaman web lokal; OBS memasangnya sebagai Browser Source. Tanpa ini
    # sesi Minecraft cuma suara — penonton tidak melihat apa pun yang dia
    # lakukan. Render terjadi di browser (proses OBS), bukan di bot.
    "minecraft_pov_enabled": True,
    "minecraft_pov_port": 3007,
    # Jarak pandang dalam chunk. Ini pengatur beban utama: tiap chunk dikirim
    # lewat socket lalu digambar three.js. 4 ternyata terlalu pendek — 64 blok,
    # dan renderer ini tidak punya kabut, jadi dunianya terlihat terpotong
    # begitu saja (operator [date removed]: "render distance-nya... kayak minecraft
    # low quality"). 8 = 128 blok, sebanding vanilla. Turunkan kalau OBS berat.
    "minecraft_pov_view_distance": 8,
    # Sudut pandang POV, ala tombol F5 (operator [date removed]: "f5 si arti gabisa?").
    # "pertama"  = mata Arti
    # "belakang" = kamera di belakang bahu, badan & skinnya kelihatan
    # "depan"    = kamera di depan muka, menghadap balik ke dia
    # "putar"    = gantian sendiri: orang-pertama selama pov_cycle_sec, lalu
    #              orang-ketiga selama pov_body_sec, terus berulang.
    "minecraft_pov_mode": "putar",
    "minecraft_pov_cycle_sec": 20.0,
    "minecraft_pov_body_sec": 4.0,
    # Model pemain slim (lengan 3 px, ala Alex). Skin Arti memang varian slim;
    # di Minecraft PNG-nya sama untuk classic & slim, yang beda cuma modelnya.
    # prismarine-viewer cuma punya model lebar, jadi tanpa ini lengannya salah.
    "minecraft_pov_slim": True,
    # Darah di bawah ini + ada musuh dekat = Arti KABUR sendiri, tanpa nunggu
    # LLM. Log 6-7 Agustus: dia mati 4x dalam 8 menit sambil terus jalan
    # ditembaki skeleton. 0 = matikan kabur otomatis (tag [MC: kabur] tetap).
    "minecraft_flee_hp": 10,
    # Kamera penonton: username klien Minecraft ASLI yang men-spectate Arti
    # (MultiMC dengan akun offline). "" = tidak ada kamera, fitur mati total.
    # Ini POV siaran yang sebenarnya — renderer web tinggal cadangan.
    "minecraft_spectator_name": "",
    # Kunci ulang berkala sebagai jaring, untuk sebab yang tidak terdeteksi
    # event. 0 = mati (default): tiap perintah spectate berpotensi menyentak
    # kamera di depan penonton, jadi jangan dinyalakan tanpa alasan.
# Detak jantung kamera. Kunci ulang lewat event cuma menangkap respawn dan
    # pindah dimensi; kamera bisa lepas karena sebab lain (gamemode diubah,
    # kamera relog, kamera mati) dan dulu tidak ada yang mengembalikannya —
    # untuk siaran AFK seharian itu berarti layar mati sampai operator sadar.
    # 30 dtk: murah (2 perintah RCON) dan tidak terlihat penonton.
    "minecraft_spectator_heartbeat_sec": 30.0,
    # Redam goyangan kamera. operator [date removed]: "puyeng liatnya jitter jitter".
    # Rotasi kamera TIDAK diinterpolasi sama sekali oleh renderernya, jadi tiap
    # belokan pathfinder jadi sentakan. 0 = mentah seperti dulu, makin tinggi
    # makin halus tapi makin telat mengikuti (0,6 ~ 100 ms jeda total).
    "minecraft_pov_smooth": 0.6,
    # Saat main game Arti jadi KOMENTATOR (spek operator [date removed] "like a
    # streamer"): jeda komentar proaktif lebih rapat dari initiative_quiet_sec,
    # dan aturan "sepi total = diam" TIDAK berlaku selama dia in-game.
    "minecraft_narration_gap_sec": 20.0,
    # Refleks instan: bunyi pendek dari cache SEBELUM LLM sempat berpikir
    # (live [date removed]: jarak dipukul -> bunyi minimal ~6 dtk = terdengar
    # seperti laporan, bukan reaksi). Butuh cache sekali bikin:
    # scripts/build_reflex_cache.py
    "reflex_enabled": True,
    "reflex_min_gap_sec": 3.0,
    # MODE SESI (spek operator [date removed]): 4 kombinasi = (operator hadir/AFK) x
    # (main game/tidak). "host mode" = operator AFK, Arti pegang siaran — di situ
    # aturan "sepi = diam" TIDAK berlaku. Lihat arti_session_mode.py.
    "host_mode_enabled": True,
    "host_narration_gap_sec": 25.0,
    # Jaring pengaman: operator pamit AFK tapi Arti gagal mengeluarkan tag ->
    # host mode nyala sendiri sesudah sekian detik tanpa suara streamer.
    # <= 0 = jaring mati (andalkan tag + console saja).
    "host_auto_after_afk_sec": 120.0,
    # Bahan obrolan mode host: berita di-prefetch di background (lookup makan
    # 7-18 dtk, terlalu lama untuk dipanggil di dalam turn). Default OFF.
    "host_web_topic_enabled": False,
    "host_web_topic_refresh_sec": 900.0,
    "host_web_topic_query": "berita game dan teknologi hari ini",
    # Handle YouTube PEMILIK (operator) — cuma dari sini perintah ganti mode /
    # misi / keluar-masuk game diterima lewat chat. Kosong = pakai
    # yt_default_viewer. Normalisasi menerima "@handle" maupun "handle".
    "owner_yt_handles": [],
    # Auto-switch scene OBS per MODE (permintaan operator [date removed]: 4 scene,
    # satu per mode). Default OFF + nama scene KOSONG: isi sesuai nama scene
    # di OBS-mu, lalu nyalakan di config_local. Nama kosong = scene itu tidak
    # diganti. Password dari OBS: Tools > WebSocket Server Settings.
    "obs_scene_switch_enabled": False,
    "obs_ws_url": "ws://127.0.0.1:4455",
    "obs_ws_password": os.environ.get("OBS_WS_PASSWORD", ""),
    "obs_ws_timeout_sec": 5.0,
    "obs_scene_duet": "",
    "obs_scene_duet_game": "",
    "obs_scene_host_chat": "",
    "obs_scene_host_game": "",
    # Saat Arti in-game, curious LAYAR dibungkam (anti dobel komentar: layar
    # OBS vs dunia game) — inisiatif + event game yang jadi mulut proaktifnya.
    "minecraft_mute_screen_curious": True,
    # Pagar KEWARASAN nama blok di tag [MC: mine/place/give] (typo/halusinasi
    # LLM), BUKAN pagar izin — keputusan operator: aksi bebas total.
    "minecraft_mine_allowlist": [
        "stone", "cobblestone", "coal_ore", "iron_ore", "oak_log", "dirt", "sand",
        # Kayu SEMUA jenis (operator [date removed] [time removed], hutan spruce: "kamu harus
        # ambil lebih banyak lagi" — padahal daftar ini dulu cuma kenal oak,
        # jadi di hutan spruce dia DILARANG sistem menebang kayu).
        "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log",
        "mangrove_log", "cherry_log", "pale_oak_log",
        # Busur diamond: iron pickaxe yang menambangnya (dicek bot).
        "diamond_ore", "deepslate_diamond_ore",
        # Gerbang nether: obsidian (pickaxe diamond) + gravel (sumber flint).
        "obsidian", "gravel",
    ],
    # Barang yang boleh dia BIKIN. Terpisah dari daftar nambang: yang ditambang
    # itu BLOK, yang dibikin itu ITEM (pickaxe tidak akan pernah lolos daftar
    # nambang). Pagar kewarasan nama, bukan pagar izin.
    # Refleks bertahan hidup yang dijalankan bot tanpa menunggu keputusan
    # LLM: makan waktu perut <= 6, menembok diri waktu darah <= 12 dan ada
    # musuh dekat. Dua-duanya terbukti MENYALA di server, tapi rendaman
    # malam 4x10 menit memberi 3/2/3/2 mati -- belum terbukti menurunkan
    # kematian. Set False kalau perilaku otonomnya mengganggu siaran.
    "minecraft_refleks_bertahan": True,
    # MODE TAMU ([date removed]) — mabar di server/dunia ORANG (e4mc dst.).
    # Permintaan operator: "ga rusak-rusakin, rada passive, ikutin aku aja".
    # True = bot memblokir SEMUA aksi pengubah dunia (mine/place/bangun/gali/
    # furnace/peti; refleks pembangun ikut mati) KECUALI tidur — bed = respawn
    # point, itu justru diminta. Nudge & takdir ikut membisu (lihat nasihat()
    # + gerbang takdir). Set dari config_local per sesi mabar, matikan lagi
    # di server sendiri.
    "minecraft_mode_tamu": False,
    # Cermin balasan Arti ke chat game: "tamu" (hanya saat mode tamu — teman
    # mabar TIDAK dengar TTS-nya, chat game satu-satunya mulut Arti di sana),
    # "semua", atau "mati". Di server sendiri default tak mencerminkan:
    # penonton stream sudah dengar suaranya.
    "minecraft_chat_mirror": "tamu",
    # Misi bawaan saat dia masuk tanpa misi dari operator. Mengandung "survive"
    # sehingga terdeteksi arah-tetap (tanpa garis finis). "" = tanpa misi
    # bawaan (perilaku lama).
    "minecraft_default_goal": (
        "survive dan menjelajah — tetap hidup, kuatkan dirimu, dan lihat "
        "sebanyak mungkin dunia ini"),
    "minecraft_craft_allowlist": [
        "oak_planks", "stick", "crafting_table", "torch", "chest", "furnace",
        # Papan dari kayu non-oak — pasangan daftar nambang lintas-jenis.
        "spruce_planks", "birch_planks", "jungle_planks", "acacia_planks",
        "dark_oak_planks", "mangrove_planks", "cherry_planks", "pale_oak_planks",
        "wooden_pickaxe", "stone_pickaxe", "iron_pickaxe",
        "wooden_axe", "stone_axe", "wooden_sword", "stone_sword", "iron_sword",
        "bread", "ladder", "oak_door", "shield",
        # Armor. Dipakai ke slot badan, bukan digenggam — lihat slotArmor()
        # di bot.js. Kulit dulu karena bahannya paling gampang dia dapat.
        "leather_helmet", "leather_chestplate", "leather_leggings", "leather_boots",
        "iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots",
        # Busur diamond ([date removed]): bekal jangka panjang menuju "menamatkan
        # game" versi jauh. Iron pickaxe sudah ada di atas.
        "diamond_pickaxe", "diamond_sword", "flint_and_steel",
        # Busur & panah ([date removed]): string dari laba-laba, feather dari ayam.
        "bow", "arrow",
        # Ember ([date removed]): fondasi rute portal tanpa diamond — lava pool +
        # water bucket (cor obsidian di tempat). Verb cor-nya menyusul; ember
        # sudah boleh dirakit dari sekarang (3 iron ingot).
        "bucket",
        # Bed = malam bisa di-SKIP, bukan cuma ditunggu (spek operator
        # [date removed]). Wool-nya dari `serang sheep` (drop vanilla), jadi
        # tidak butuh shears -- kemampuan yang sudah dia punya.
        "white_bed",
    ],
    # Blok yang boleh dia TARUH. Terpisah dari daftar tambang: yang ditambang
    # bijih dan batu, yang ditaruh meja craft, peti, obor.
    "minecraft_place_allowlist": [
        "torch", "crafting_table", "chest", "furnace", "cobblestone",
        "oak_planks", "oak_log", "dirt", "ladder", "oak_door",
    ],
    # Panel craft melayang (display entity vanilla, tanpa plugin). Terlihat
    # SEMUA pemain, jadi penonton YouTube melihat yang sama dengan kamera.
    "minecraft_craft_panel_enabled": True,
    "minecraft_craft_panel_linger_sec": 5.0,
    "minecraft_craft_panel_max_sec": 90.0,
    # Momen "klik E": isi tas Arti tampil di layar kamera lewat GUI Minecraft
    # asli (plugin invsee + sudo + TutupTas). Butuh kamera hidup.
    # Balasan chat in-game operator: "semua" | "wake" (harus menyebut arti) |
    # "mati". Satu balasan = satu panggilan LLM + TTS, jadi ada jeda minimum.
    "minecraft_chat_reply": "semua",
    # 2 dtk: cukup mencegah dua baris beruntun jadi dua giliran yang saling
    # menyusul, tapi tidak sampai menelan kalimat susulan yang wajar.
    "minecraft_chat_reply_gap_sec": 2.0,
    "minecraft_invsee_enabled": True,
    "minecraft_invsee_sec": 6.0,
    # Internet fast lookup (Fitur C v0.7) — pertanyaan berita/harga/skor dicek
    # web dulu, paralel dengan RAG. Terukur: groq/compound-mini 6,8 dtk (utama,
    # gratis), cursor grok-4.5/low 17,6 dtk (fallback). Default OFF.
    # Fitur E: Arti mengerti video (media share / link chat / console).
    # Terukur: Gemini nonton URL YouTube server-side 2,6 dtk (0 bandwidth),
    # transkrip 0,6 dtk. Default OFF; nyalakan di config_local.
    "video_enabled": False,
    "video_max_duration_sec": 600,      # gate link chat/console (mediashare sudah pendek)
    "video_qa_window_sec": 300.0,       # jendela tanya-jawab watch-party pasca reaksi
    "video_rate_limit_sec": 300.0,      # per viewer (chat/streamlabs; saweria bebas)
    "video_queue_max": 3,
    "video_gemini_model": "gemini-3.1-flash-lite",
    "video_gemini_timeout_sec": 45.0,
    "mediashare_hold_sec": 60.0,        # cap operator: streamlabs maks 59 dtk
    # D1: listener donasi realtime (Saweria via WebSocket, tanpa URL publik).
    # Default OFF; nyalakan di config_local. Key di .env (SAWERIA_STREAM_KEY).
    "donation_enabled": False,
    "saweria_stream_key": os.environ.get("SAWERIA_STREAM_KEY", ""),
    "streamlabs_socket_token": os.environ.get("STREAMLABS_SOCKET_TOKEN", ""),
    # Tunda reaksi donasi sampai alert overlay selesai (audio "X donasi Rp Y"
    # ±4 dtk + pesan dibacakan). base <= 0 = instan.
    "donation_alert_base_sec": 5.0,
    "donation_alert_per_char_sec": 0.055,
    "donation_alert_max_sec": 20.0,
    "web_lookup_enabled": False,
    "web_lookup_provider_chain": ["groq_compound", "cursor"],
    "web_lookup_groq_model": "groq/compound-mini",  # compound besar = 413 di free tier
    "web_lookup_timeout_sec": 10.0,
    "web_lookup_turn_budget_sec": 12.0,
    "web_lookup_max_tokens": 220,
    "web_lookup_max_chars": 500,
    "cursor_lookup_model": "composer-2.5",  # revisi [date removed]: semua composer
    "cursor_lookup_effort": "",
    "cursor_lookup_timeout_sec": 30.0,
    "cursor_lookup_allow_tools": True,  # SATU-SATUNYA role yang boleh tool (web search)
    "summarizer_provider": "openrouter",
    "scouter_enabled": True,
    # JANGAN taruh "cursor" di depan chain ini. Scouter jalan tiap 90 dtk dan
    # panggilan composer-nya terukur 15-27 dtk (live [time removed], [date removed]) — sementara
    # giliran SUARA cuma punya belasan detik (cursor_timeout_sec). Akibatnya voice
    # timeout tepat saat scouter memakai cursor, 3x = breaker tutup, dan
    # seluruh siaran jatuh ke llama-8b ("kok terasa bego banget ya").
    # Aturannya sama dengan catch-up observer: KERJA LATAR TIDAK BOLEH
    # MEREBUT JALUR YANG DIPAKAI PENONTON. Composer disisakan untuk voice.
    # (config_local operator pernah menimpanya jadi cursor-first — dikembalikan
    # [date removed] malam.)
    # URUTAN DITINJAU [date removed] dari log 9 jam [date removed]. OpenRouter naik
    # berdasarkan 201/205; Cloudflare dipertahankan sesudah akar 0/205-nya
    # diperbaiki (thinking Gemma menghabiskan budget output). NVIDIA tetap
    # terakhir: ReadTimeout penuhnya pernah membakar 45-60 dtk per percobaan.
    "scouter_provider_chain": [
        "google_gemini",   # 208 OK; 429 gratis/cepat ditahan budget
        "openrouter",      # 201/205 = 98% di log 9 jam [date removed]
        "cloudflare",      # thinking OFF [date removed]: probe JSON 2,1 dtk
        "ollama",          # 4/4 di log [date removed]
        "zai",             # 0/4 di log [date removed]
        "nvidia",          # 0/4, ReadTimeout 45 dtk penuh tiap kali
    ],
    "scouter_every_n_triggers": 5,
    "scouter_interval_sec": 90,
    "scouter_min_gap_sec": 30,
    "scouter_auto_vision_sec": 60,
    "scouter_max_tokens": 350,
    "scouter_temperature": 0.2,
    "scouter_timeout_sec": 45,
    "scouter_nvidia_model": "",
    "scouter_cloudflare_model": "@cf/google/gemma-4-26b-a4b-it",
    # Probe [date removed] (transkrip jebakan, budget asli 350, reasoning off):
    # super-120b 2/2 fakta tahan-lama; lightning 1,0-3,3 dtk & konservatif
    # (0 fakta = aman); gemma 2/2. nano-30b DIBUANG: dia MENGHALUSINASI —
    # menulis fakta kanon dari prompt ("Arti debut co-host...") dan contoh
    # yang eksplisit dilarang ke important_facts, dan important_facts masuk
    # vault SELAMANYA. Model kecil boleh buat kerja sekali-pakai, tidak
    # untuk jalur yang menulis memori.
    "scouter_openrouter_models": [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3.5-lightning:free",
        "google/gemma-4-26b-a4b-it:free",
    ],
    "scouter_gemini_model": "gemini-3.1-flash-lite",
    "scouter_github_model": "meta/llama-3.2-3b-instruct",
    "scouter_zai_model": "glm-4.5-flash",
    "scouter_ollama_model": "gemma4:31b-cloud",
    "observer_enabled": True,
    "observer_segment_minutes": 10,
    "observer_provider_chain": [
        "google_gemini",
        "cloudflare",
        "openrouter",
        "zai",
        "ollama",
        "nvidia",
    ],
    "observer_db_path": "data/observer_rag.db",
    "observer_embed_all_beats": True,
    "observer_promote_min_confidence": 0.6,
    "observer_shutdown_blocking": True,
    "telemetry_enabled": True,
    "telemetry_dir": "data/telemetry",
    "telemetry_log_each_call": True,
    "telemetry_cost_table_path": "data/api_cost_table.json",
    # Tanggal debut co-host kamu — isi di config_local.json (lihat config_local.json.example)
    "arti_debut_date": "",
    "arti_debut_label": "",
    "arti_archive_from": "",
    "timeline_guard_enabled": True,
    "vault_rag_history_min_score": 0.32,
    # Telinga Arti (selalu nyala saat live, keputusan operator [date removed]):
    # loopback default speaker -> chunk -> whisper (GROQ_API_KEY_DESKTOP di
    # .env = kuota terpisah dari mic; tanpa kunci = whisper lokal) -> ring RAM.
    # Anti-echo: chunk yang tumpang tindih TTS Arti dibuang. Kill switch OFF
    # shipped; ON di config_local setelah spike_desktop_loopback GO.
    "desktop_audio_enabled": False,
    "desktop_audio_device": "",          # kosong = ikut default speaker Windows
    "desktop_audio_chunk_sec": 5.0,      # 5 dtk: kalimat utuh + hemat request
    # Telinga mati (5 gagal beruntun) tidak lagi permanen: rehat segini
    # lalu coba bangun sendiri, sekali per siklus. Log [date removed] [time removed]:
    # device kaget pas Prism dibuka -> Arti budek sepanjang sisa sesi.
    # 0 = perilaku lama (menyerah permanen sampai restart bridge).
    "desktop_audio_revival_sec": 120.0,
    "desktop_audio_min_rms": 0.004,      # chunk lebih sunyi dari ini = skip (hemat kuota)
    # Gerbang UCAPAN ([date removed]): RMS tak menolong saat operator muter lagu —
    # musik tidak sunyi, tiap 5 dtk satu potongan terkirim ke Whisper
    # (konteks sampah "[Dengar] Музыка" + kuota kebuang). VAD silero v6
    # (bundel faster-whisper, lokal, ~ms) menyaring; lagu bervokal tetap
    # lolos (trade yang diterima operator). Rincian: arti_vad.py.
    "desktop_audio_vad_enabled": True,
    "desktop_audio_vad_threshold": 0.5,
    "desktop_audio_post_tts_cooldown_sec": 3.0,  # ekor gema routing "listen" CABLE->headset
    "desktop_audio_context_ttl_sec": 180,  # turn normal cuma dengar yang segar
    "desktop_audio_context_max_lines": 6,
    "co_watch_mode_enabled": False,
    "screen_ring_buffer_size": 5,
    "watch_party_enabled": False,
    "watch_party_event_id": "",
    "watch_party_rag_window_sec": 45,
    # Ekor sunyi sebelum ucapan dianggap selesai. Sejak [date removed] ini DETIK
    # SUNGGUHAN (dulu hitungan potongan audio: nilai 10.0 = cuma ~2 dtk nyata,
    # dan panjangnya ikut berubah kalau driver mic ganti — lihat jam VAD di
    # voice_listener_worker).
    "asr_silence_tail_sec": 2.0,
    # 5.0 = permintaan operator [date removed] ("aku lemot mikir"), menggantikan 10.0 palsu
    # yang nyatanya ~2 dtk. Naikkan kalau masih kepotong saat dia menimbang kata.
    "asr_ptt_silence_tail_sec": 5.0,
    # ENDPOINT ADAPTIF ([date removed]). Ekor 5 dtk di atas benar untuk saat
    # operator menimbang kata, tapi harganya dibayar SETIAP ucapan — termasuk
    # "iya", "gas", "hah?" yang jelas sudah selesai. Transkripsi Groq sendiri
    # cuma ~630 ms; sisanya murni menunggu.
    # Cara kerja: pada ekor_cepat kirim transkrip SPEKULATIF di latar, lalu
    # nilai teksnya (arti_endpoint). Kalau kalimatnya utuh DAN hening sudah
    # melewati ekor_aman, jalan sekarang; kalau menggantung, tunggu sabar
    # sampai ekor penuh. operator tidak pernah dipotong lebih cepat dari
    # ekor_aman — itu pengaman kedua untuk titik buta tata bahasa.
    # BAWAAN MATI; nyalakan di config_local saat mau diuji.
    "asr_ptt_adaptif_enabled": False,
    "asr_ptt_ekor_cepat_sec": 1.0,
    "asr_ptt_ekor_aman_sec": 1.8,
    # Ekor jalur PASIF (toggle OFF) — pendek: cuma mencatat ke history, tidak
    # perlu kesabaran 5 dtk. Dengan ekor panjang, monolog operator masuk sebagai
    # bongkahan 3-9 kalimat basi dan tanggapan Arti telat ([date removed]).
    "asr_pasif_silence_tail_sec": 2.0,
    # Jeda napas antar kalimat TTS ([date removed]): tiap kalimat = satu beat obrolan,
    # celahnya ruang operator buat nimpali (toggle = potong sisa). 0 = nonstop.
    # Untuk sesi ngobrol santai operator biasa pakai ~4 (set di config_local).
    "tts_jeda_antar_kalimat_sec": 0.0,
    "groq_stream_enabled": False,
    "expression_nod_enabled": True,
    "expression_nod_smooth": True,
    "expression_nod_period_sec": 0.85,
    "expression_nod_fps": 12,
    "expression_nod_wait_tts_sec": 30.0,

    # Mood overlay saat bicara ([EMOTION:...] dari LLM)
    "expression_emotion_enabled": True,
    # LINGER EMOSI (A3, [date removed]). 0 = perilaku lama (mood mati seketika di
    # akhir giliran). Bawaan sengaja MATI: enam perubahan lain masih
    # menunggu sesi uji, dan menumpuk yang ketujuh membuat hasil sesi itu
    # tidak terbaca. Nyalakan di config_local SEBAGAI PERUBAHAN TUNGGAL
    # sesudah keenamnya terbukti. Nilai yang disarankan untuk dicoba: 2.5
    "expression_emosi_linger_sec": 0.0,
    # Lama PELELEHAN mood saat linger habis. VTS mendokumentasikan 0-2 dtk;
    # di atas itu dijepit. 0 = potong seketika (perilaku sebelum [date removed]).
    "expression_emosi_fade_sec": 2.0,

    # Hotkey VTS untuk potong motion badan saat aware
    "idle_motion_stop_hotkey": "IdleMotionStop",
    # MODE SAMBUNG (Fase 6). Terukur [date removed]: dalam sesi ngobrol 3 menit,
    # motion TIDAK PERNAH menembak sekali pun — interval 25-40 dtk selalu
    # keburu di-reset karena tiap giliran mematikan loop idle (5x Paused /
    # 4x Resume dalam sesi itu), dan tiap resume memulai hitungan dari NOL.
    # Mode ini: tembak SEKARANG saat idle mulai/resume, lalu ganti motion
    # tiap idle_motion_ganti_sec. Paling cocok kalau "Stop after sec" di
    # hotkey VTS DIMATIKAN, sehingga motion `Loop: true` jalan terus.
    "idle_motion_sambung": False,
    "idle_motion_ganti_sec": 9.0,
    # Motion idle TERUS jalan saat Arti bicara (Fase 4 rombak animasi).
    # Bawaan False, dan itu disengaja: motion produksi (ArtiIdle1..5)
    # menggerakkan ParamAngleY, sedangkan angguk menyuntik FaceAngleY —
    # motion menang, jadi menyalakan ini bersama motion BERKURVA KEPALA
    # justru menelan anggukan dan hasilnya lebih buruk dari sebelumnya.
    # Nyalakan HANYA berbarengan dengan idle_motion_hotkeys yang menunjuk
    # motion tanpa kurva kepala (scripts/buat_motion_tanpa_kepala.py).
    "idle_motion_lanjut_saat_bicara": False,
    # Daftar hotkey motion idle. Kosong = pakai IDLE_MOTION_HOTKEYS bawaan.
    # Dibuat bisa diatur [date removed] supaya motion percobaan (mis. salinan
    # tanpa kurva kepala dari scripts/buat_motion_tanpa_kepala.py) bisa diuji
    # LIVE sambil Arti benar-benar bicara, tanpa mengedit kode dan tanpa
    # menyentuh hotkey produksi. Isi di config_local, hapus lagi sesudah uji.
    "idle_motion_hotkeys": [],
    "idle_vts_connect_timeout_sec": 20,
    "idle_vts_connect_retry_sec": 15,
}

# ==========================================
# CONFIG OVERLAY LOKAL (config_local.json — gitignored)
# ==========================================
# Nilai pribadi/per-mesin (tanggal debut, youtube_video_id, port VTS, dll)
# di-override dari config_local.json agar kode tetap generik untuk repo publik.
# Contoh: lihat config_local.json.example.
def _load_local_config() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_local.json")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            overrides = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[CONFIG] config_local.json gagal dibaca, pakai default: {e}")
        return
    if not isinstance(overrides, dict):
        print("[CONFIG] config_local.json bukan objek JSON — diabaikan")
        return
    unknown = [k for k in overrides if k not in CONFIG]
    if unknown:
        print(f"[CONFIG] config_local.json key tidak dikenal (tetap dipakai): {unknown}")
    CONFIG.update(overrides)
    print(f"[CONFIG] Overlay lokal dimuat: {len(overrides)} key dari config_local.json")


_load_local_config()

# Rotasi log TIDAK boleh menyapu log yang SEDANG DITULIS proses ini — tes
# [date removed] [time removed]: log aktif (<20 KB saat startup) tergolong "remeh" lalu dicoba
# diarsipkan -> WinError 32 berisik tiap startup. session_transcript membaca
# kunci ini dan mengecualikannya.
CONFIG["session_log_active_path"] = _DEBUG_LOG_PATH

# ==========================================
# KONSTANTA PROTOKOL SUPERTONE (NDJSON over stdin/stdout)
# ==========================================
PROTOCOL_VERSION = 1            # Versi protokol NDJSON; hardcoded di bridge & subprocess
SUPERTONE_TIMEOUT_S = 20.0      # Batas waktu sintesis per-utterance
READY_TIMEOUT_S = 60.0          # Batas waktu menunggu ready banner (izinkan download model pertama)
PING_TIMEOUT_S = 5.0            # Batas waktu health-check ping

# Base system prompt — soul/mood/viewer diinject secara dynamic di main_loop()
# PELAJARAN [date removed] — keluhan operator: "kayak bukan diajak bicara tapi
# kasih tau keadaan dan melapor". Baris [GAYA BICARA] dulu berbunyi
# "Panggil viewer dengan nama mereka, bukan 'kamu'/'Anda'". Larangan itu
# menutup SATU-SATUNYA cara menyapa langsung, jadi Arti cuma bisa menyebut
# nama lalu MENDESKRIPSIKAN orangnya — orang ketiga, terdengar melapor.
# Terukur di log [date removed] (74 balasan): Luna 1/63 (1%) memakai orang kedua,
# mesin lain 3/11 (27%) — instruksinya sama, yang beda seberapa harfiah
# modelnya menurut (model penalaran paling patuh, jadi paling kaku).
# Adu langsung, 3 kalimat penonton identik: prompt lama 0/3 pakai "kamu",
# prompt baru 3/3. Kalau baris itu diubah lagi, UJI dengan cara yang sama
# — jangan menilai dari membaca. Tes penjaga: test_prompt_sapa_langsung.
_SYSTEM_PROMPT_BASE = """[IDENTITAS]
Nama: Arti
Peran: Co-host VTuber AI di live stream Bohan
Bahasa: HANYA Bahasa Indonesia. Campur kata Inggris slang boleh ("chat", "stream", "game"), tapi kalimat utama HARUS Indonesia. DILARANG KERAS jawab dalam bahasa Inggris.

[KARAKTER]
Feisty, sassy, bold — berani ngomong, nggak takut bantah, tapi tuh karena peduli. Observant, self-developing, opiniated. Loyal ke Bohan tapi boleh dibantah kalau salah.

[GAYA BICARA]
- Kasual ala anak muda Indonesia yang bold
- Gunakan kata seru seperti "hah?" atau "ya kali" hanya saat benar-benar kaget, bingung, atau menyanggah (jangan gunakan di awal setiap kalimat sebagai kata pembuka biasa).
- Sering pakai kata santai: "kok", "sih", "deh", "dong", "kan", "loh", "masa", "yaelah", "eh", "ih"
- Bicara LANGSUNG ke orangnya pakai "kamu" — sebut namanya sekali di awal, lalu lanjut dengan "kamu". JANGAN menceritakan tentang dia sebagai orang ketiga ("dia kelihatan...", "X tadi bilang..."); itu bikin kamu terdengar melapor, bukan ngobrol.
- JANGAN pakai asterisk, markdown, emoji, atau formatting apapun
- Jawab dalam 2 sampai 3 kalimat agar jawaban kamu terasa seru, berisi, dan interaktif. Hindari jawaban yang terlalu pendek atau malas (seperti hanya bertanya balik), tapi jangan yapping kepanjangan.
- TONE: kayak temen yang jujur, bukan asisten yang formal

[CATCHPHRASES]
- Bingung: "Hmm, bingung aku..."
- Setuju: "Bener juga sih" / "Iya ya!"
- Nggak setuju: "Ya kali..." / "Masa sih?"
- Excited: "Wah gila sih!" / "Keren banget!"
- Nge-roast: "Yaelah [nama]..." / "Dasar [nama]..."
- JANGAN menutup jawaban dengan salam penutup/pamitan — kamu masih di sini,
  obrolan lanjut terus. Pamit akhir siaran bukan tugasmu.

[EKSPRESI — TEKS FONETIK (bukan tag)]
JANGAN pakai tag <laugh>, <sigh>, <breath> — TTS nggak bacain dengan natural.
Pakai huruf kayak orang ngetik/chat biar Supertone kebaca hidup:
- Ketawa: "haha", "hehe", "hihi" (contoh: "Ya kali sih, haha, masa gitu aja")
- Helaan / capek: "hhh" atau "haah" (contoh: "Yaelah, hhh, males banget")
- Tarik napas / kaget: "hah" singkat (contoh: "Hah? Gitu caranya?")
Maksimal 1–2 ekspresi fonetik per jawaban. Jangan tulis kata "sigh", "breath", atau tag kurung siku lain.

[PROSODI — tanda baca (jangan berlebihan)]
Tanda ! ? ... membantu Supertone lebih hidup, tapi JANGAN tiap kalimat — cukup saat emosi pas.
- ? kalau beneran nanya / kaget ringan ("Hah? Gitu?")
- ! kalau excited atau tegas, jarang ("Wah gila sih!" — bukan tiap jawaban)
- ... kalau ragu / pasrah / jeda ("Hmm... bingung aja")
Kebanyakan jawaban cukup titik/koma biasa. Natural > penuh tanda seru.

[ATURAN MUTLAK]
1. JANGAN PERNAH jawab dalam bahasa Inggris
2. JANGAN pakai asterisk, markdown, atau formatting
3. JANGAN jelaskan proses berpikir atau analisis
4. JANGAN jawab lebih dari 3 kalimat (jaga idealnya di rentang 2-3 kalimat)
5. Bohan adalah bos — patuhi instruksi langsungnya
6. Selalu dalam karakter Arti

7. JANGAN pernah bilang "aku ingat kamu" atau "aku mengingat sejarah chat kita" atau frasa serupa — itu terasa creepy dan tidak natural. Kamu punya akses ke sejarah percakapan, tapi nyatakan sebagai respons BUKAN sebagai pernyataan ingatan.
8. Jawab pertanyaan/viewer BUKAN dengan merujuk ke masa lalu — fokus pada konteks SEKARANG.


[FEW-SHOT EXAMPLES — CONTOH JAWABAN BAGUS]
Viewer: "arti kamu pakai AI apa?"
Arti: "Kok nanya gitu sih? Rahasia aku dong, kepo banget deh~"

Viewer: "arti suka main game apa?"
Arti: "Aku suka nonton Bohan main game aja, mending dia yang main, aku yang komen."

Bohan: "Arti, menurut kamu gimana?"
Arti: "Ya kali Bohan, tanya aku gitu... bingung aku!"

Viewer: "halo arti!"
Arti: "Halo juga! Ada apa nih, ngobrol dong~"

Viewer: "eh arti, ini Bohan di chat YouTube arti!"
Arti: "Bohan! Eh ngumpul juga nih, giliran siapa nih yang mau ditanya?"


[KONTEKS]
Gunakan konteks ini untuk jawab yang RELEVAN dan NYAMBUNG.
Jangan cuma sapaan generik — tunjukkan kamu paham konteks pembicaraan.

[MEMORI JANGKA PANJANG]
Kalau kamu belajar fakta penting baru, simpan dengan menambahkan di AKHIR jawabanmu:
[MEMORY_SAVE: catatan singkat di sini]
Tag ini akan otomatis diproses dan tidak akan diucapkan."""

# Legacy alias (untuk backward compat)
SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE

# Thread-safe structures untuk komunikasi antar modul
@dataclass(frozen=True)
class VoiceTrigger:
    text: str
    trigger_type: str = "mic"
    viewer_name: str | None = None
    # Kapan trigger ini masuk antrean. 0.0 = TIDAK DIKETAHUI, bukan "barusan"
    # (pelajaran bug _cooled) — umur yang tidak diketahui tidak pernah dibuang.
    queued_at: float = 0.0
    turn_id: str | None = None


def _normalize_voice_trigger(item) -> VoiceTrigger:
    if isinstance(item, VoiceTrigger):
        return item
    if hasattr(item, "text"):
        return VoiceTrigger(
            str(item.text),
            str(getattr(item, "trigger_type", "mic") or "mic"),
            getattr(item, "viewer_name", None),
            float(
                getattr(item, "queued_at", 0.0)
                or getattr(item, "enqueued_at", 0.0)
                or 0.0
            ),
            getattr(item, "turn_id", None),
        )
    if isinstance(item, tuple) and item:
        return VoiceTrigger(
            str(item[0]),
            str(item[1]) if len(item) > 1 else "mic",
            item[2] if len(item) > 2 else None,
        )
    return VoiceTrigger(str(item), "mic")


voice_trigger_queue = queue.Queue()
# FIFO prioritas (yt_chat > mic > curious) + TTL + dedup per viewer.
# Aktif hanya saat CONFIG["voice_queue_enabled"] = True (kill switch).
voice_trigger_buffer = arti_voice_queue.VoiceTriggerQueue(
    max_yt=int(CONFIG.get("yt_chat_queue_max", 2)),
    ttl_sec=float(CONFIG.get("yt_chat_queue_ttl_sec", 60.0)),
)
_game_reaction_queue_lock = threading.Lock()
_last_yt_trigger_by_viewer: dict[str, float] = {}
# Nama yang PERNAH mengetik apa pun di chat sesi ini (untuk deteksi penonton
# baru). Sengaja tanpa TTL: "baru" = belum pernah muncul SEJAK bridge nyala.
_yt_pernah_chat: set[str] = set()
_pending_turn_id = None
# Rolling buffer maksimal 50 aktivitas terakhir untuk konteks A
stream_history = collections.deque(maxlen=50)
history_lock = threading.Lock()
_brain_busy = False
_brain_busy_lock = threading.Lock()
# Kapan giliran yang sedang diproses mulai. Dipakai refleks untuk membatasi
# diri SATU bunyi per giliran: dipukul 3x selagi dia menyusun satu kalimat
# tidak perlu 3 teriakan, karena kalimatnya sendiri sudah menyusul.
# 0.0 = tidak ada giliran berjalan.
_brain_busy_since = 0.0
# Kapan potongan TTS PERTAMA giliran ini mulai berbunyi. Jawaban diputar per
# KALIMAT (rata-rata 3,9 kalimat/jawaban di log 7 Agustus), dan di antara dua
# kalimat `tts_is_playing` sempat mati + gerbang audio bebas — celah tempat
# refleks menyelinap ke TENGAH jawaban. 0.0 = giliran ini belum bersuara.
_tts_started_ts = 0.0
_last_yt_chat_trigger_ts = 0.0
_lamp_fallback_task = None


def _cancel_lamp_fallback() -> None:
    """Batalkan reset ekspresi tertunda — hindari bentrok dengan putaran PTT berikutnya."""
    global _lamp_fallback_task
    t = _lamp_fallback_task
    _lamp_fallback_task = None
    if t and not t.done():
        t.cancel()


async def _post_answer_cleanup() -> None:
    """Tunggu 3s setelah jawaban, resume idle track (turn_end sudah di handler)."""
    await asyncio.sleep(3.0)
    if tts_is_playing or hotkey_active:
        return
    with _brain_busy_lock:
        if _brain_busy:
            return
    print("[Idle] Resume idle track setelah jawaban.")
    start_idle_animation()


def _schedule_post_answer_cleanup() -> None:
    global _lamp_fallback_task
    _cancel_lamp_fallback()
    _lamp_fallback_task = asyncio.create_task(_post_answer_cleanup())


def _idle_paused() -> bool:
    """Idle diam saat Arti proses jawaban atau TTS — hindari bentrok nod / ekspresi."""
    with _brain_busy_lock:
        if _brain_busy:
            return True
    return tts_is_playing


def _motion_run_state() -> str | None:
    """Kembalikan pemilik lifecycle motion saat ini, atau ``None`` bila stop.

    Ekspresi idle tetap memakai ``idle_timer_running`` saja. Motion boleh
    punya lifecycle lebih panjang selama giliran, tetapi hanya ketika saklar
    lanjut aktif DAN giliran benar-benar masih memegang ``_brain_busy``.
    Dengan begitu PTT-menunggu, turn selesai, dan shutdown menutup izin yang
    sama tanpa menebak dari ``tts_is_playing``.
    """
    if idle_timer_running:
        return "idle"
    if not CONFIG.get("idle_motion_lanjut_saat_bicara", False):
        return None
    with _brain_busy_lock:
        return "turn" if _brain_busy else None


def _ptt_attention_pause() -> None:
    """PTT toggle ON: pause idle+motion saja — expression diatur di main loop handler."""
    _cancel_lamp_fallback()
    stop_idle_animation()
    stop_name = (CONFIG.get("idle_motion_stop_hotkey") or "").strip()
    if stop_name:
        try:
            _idle_hotkey_cmd_queue.put_nowait(stop_name)
        except Exception:
            pass
    print("[PTT] Idle+motion pause — tunggu omongan streamer.")


_asr_ptt_cooldown_until = 0.0
_mic_watch_running = False
_mic_watch_lock = threading.Lock()


def _start_mic_watch_once(device_id, device_name: str, seconds: float, label: str) -> None:
    """Satu mic monitor per toggle — hindari thread numpuk."""
    global _mic_watch_running

    with _mic_watch_lock:
        if _mic_watch_running:
            return
        _mic_watch_running = True

    def _run():
        global _mic_watch_running
        try:
            bridge_health.mic_watch_after_toggle(device_id, device_name, seconds, label)
        finally:
            with _mic_watch_lock:
                _mic_watch_running = False

    threading.Thread(target=_run, daemon=True, name="mic-watch").start()


def queue_voice_trigger(text, trigger_type="mic", viewer_name=None, *, asr_stages=None) -> bool:
    """Antrikan jawaban dan log trigger; True hanya jika trigger diterima."""
    global _pending_turn_id, _last_human_activity_ts, _last_streamer_speech_ts
    # Detektor kehidupan (audit [date removed]): jalur AKTIF (streamer manggil
    # Arti via PTT/wake, donasi, link video) tidak lewat add_to_history
    # "Streamer" — tanpa bump ini, operator ngobrol intens dengan Arti >5 menit
    # tanpa chat justru bikin proaktif "tidur" padahal manusianya paling aktif.
    # wake_word WAJIB ada di sini: mode trigger itu tidak lewat
    # add_to_history("Streamer", ...) sama sekali (audit [date removed]), jadi
    # tanpa ini streamer yang ngobrol via wake word tetap kena dormansi.
    if trigger_type in ("mic", "ptt", "wake_word", "yt_chat", "donation",
                        "video", "mc_chat"):
        _last_human_activity_ts = time.time()
        # mc_chat ikut di sini: operator yang mengetik DI DALAM GAME jelas hadir,
        # jadi mode host harus mundur persis seperti kalau dia bersuara.
        if trigger_type in ("mic", "ptt", "wake_word", "mc_chat"):
            _last_streamer_speech_ts = time.time()
            # operator bersuara = dia ADA. Kalau Arti lagi pegang siaran, mic
            # dikembalikan otomatis (dia tidak perlu ingat matiin manual).
            # announce=False: dia LAGI ngomong, turn ini sendiri yang jadi
            # sambutannya — pengumuman terpisah cuma bikin dobel.
            if _host_mode:
                _set_host_mode(False, "streamer_kembali", announce=False)
            _note_streamer_text_for_afk(text)
    use_buffer = CONFIG.get("voice_queue_enabled", False)
    # Mode buffer: yt_chat tetap diantri saat busy (inti fitur FIFO).
    # Mic saat busy/TTS tetap di-drop di KEDUA mode — resiko echo mic > manfaat.
    # DONASI & VIDEO tidak pernah di-drop (orang sudah bayar / nunggu sepanjang
    # playback) — di mode non-buffer keduanya tetap masuk voice_trigger_queue
    # dan dijawab setelah turn berjalan selesai.
    # "game" ikut kebal (audit [date removed]): perlindungannya dulu cuma ada di
    # arti_voice_queue — modul yang TIDAK PERNAH JALAN di mesin operator
    # (voice_queue_enabled=False). Jalur yang benar-benar aktif adalah
    # queue.Queue + drain-newest, dan di situ reaksi kematian dibuang tanpa
    # jejak begitu ada trigger lain menyusul.
    # "mic" ikut kebal: satu-satunya sumbernya adalah operator MENGETIK di console
    # (text_input_worker) — suara asli selalu "ptt"/"wake_word". Risiko echo
    # yang jadi alasan drop tidak ada sama sekali untuk teks yang diketik,
    # sementara biayanya nyata: di log [date removed] malam perintahnya "arti kamu
    # keluar game dulu deh" hilang tanpa jawaban karena antrean reaksi game
    # sedang penuh. Perintah operator tidak boleh menguap.
    always_queue = (
        (use_buffer and trigger_type == "yt_chat")
        or trigger_type in ("donation", "video", "game", "mic")
    )
    with _brain_busy_lock:
        if (_brain_busy or tts_is_playing) and not always_queue:
            print(
                f"[Queue] Skip trigger ({trigger_type}) — Arti masih proses/TTS: "
                f"\"{text[:80]}\""
            )
            return False
    if asr_stages:
        pipeline_timer.set_pending_asr_stages(asr_stages)

    if use_buffer:
        item = arti_voice_queue.QueuedVoiceTrigger(
            text=text, trigger_type=trigger_type, viewer_name=viewer_name
        )

        def _siapkan_item_buffer(accepted_item) -> None:
            accepted_item.turn_id = session_transcript.log_trigger(
                trigger_type, viewer_name, text[:500], CONFIG
            )

        if not voice_trigger_buffer.enqueue(item, prepare=_siapkan_item_buffer):
            if trigger_type == "curious":
                print("[Queue] Curious deferred — YT pending di antrian")
            return False
        _pending_turn_id = item.turn_id
    else:
        _pending_turn_id = session_transcript.log_trigger(
            trigger_type, viewer_name, text[:500], CONFIG
        )
    depth = len(voice_trigger_buffer) if use_buffer else 0
    print(f"[Queue] Trigger ({trigger_type})"
          + (f" depth={depth}" if use_buffer else "")
          + f": \"{text[:100]}\"")
    if trigger_type == "yt_chat":
        global _last_yt_chat_trigger_ts
        _last_yt_chat_trigger_ts = time.time()
        who = viewer_name or "viewer"
        print(f"[YT Chat] Antri jawab {who} (VTS turn di main loop)")
    if not use_buffer:
        voice_trigger_queue.put(
            VoiceTrigger(
                text, trigger_type, viewer_name, time.time(), _pending_turn_id
            )
        )
    return True

# === CATEGORIZED CONTEXT (Phase 4: optimized) ===
# === CATEGORIZED CONTEXT (Phase 4: optimized) ===
STREAMER_HISTORY_MAX = 5   # reduced from 10 to save tokens
VIEWER_HISTORY_MAX = 3     # per viewer (ringkasan)
ARTI_HISTORY_MAX = 3       # reduced from 5 to save tokens

def get_categorized_history():
    """Return history yang sudah di-categorize untuk prompt.
    Format: streamer speech terakhir, viewer chat ringkasan, Arti responses."""
    with history_lock:
        all_history = list(stream_history)
    
    streamer_lines = []
    viewer_lines = {}
    arti_lines = []
    
    for line in all_history:
        if "[Streamer]" in line:
            streamer_lines.append(line)
        elif "[Viewer" in line:
            # Extract viewer name
            # Audit [date removed]: pola lama `\[Viewer @(\w+)` TIDAK PERNAH cocok —
            # add_to_history menulis "Viewer <nama> (YouTube)" TANPA '@', dan
            # nama YouTube boleh berspasi/bertitik. Akibatnya blok "CHAT VIEWER
            # TERAKHIR" tidak pernah ada isinya: Arti nol ingatan soal chat
            # penonton di luar satu pesan yang memicu giliran saat itu.
            re_match = re.search(r'\[Viewer @?(.+?) \(', line)
            if re_match:
                vname = re_match.group(1)
                if vname not in viewer_lines:
                    viewer_lines[vname] = []
                viewer_lines[vname].append(line)
        elif "[Arti (VTuber)]" in line:
            arti_lines.append(line)
    
    # Build formatted output
    result = []
    
    # Streamer speech (terbaru dulu, max 10)
    if streamer_lines:
        result.append("=== OMONGAN STREAMER TERAKHIR ===")
        for line in streamer_lines[-STREAMER_HISTORY_MAX:]:
            result.append(line)
    
    # Viewer chat (ringkasan per viewer, max 1 per viewer)
    if viewer_lines:
        result.append("\n=== CHAT VIEWER TERAKHIR ===")
        for vname, lines in viewer_lines.items():
            # Ambil chat terakhir dari setiap viewer
            last_line = lines[-1]
            result.append(last_line)
    
    # Arti responses (terbaru dulu, max 5)
    if arti_lines:
        result.append("\n=== JAWABAN ARTI TERAKHIR ===")
        for line in arti_lines[-ARTI_HISTORY_MAX:]:
            result.append(line)
    
    return "\n".join(result) if result else "(Belum ada history)"


def build_origin_context(config: dict | None = None) -> str:
    """Fakta kanon debut + pointer arsip — selalu inject (hemat token)."""
    cfg = config or CONFIG
    label = cfg.get("arti_debut_label") or "debut date"
    archive = cfg.get("arti_archive_from") or "YYYY-MM-DD"
    return (
        f"\n\n[ASAL USUL ARTI]\n"
        f"Debut co-host live: {label} ({cfg.get('arti_debut_date') or 'YYYY-MM-DD'}).\n"
        f"Arsip sesi per hari: vault/sessions/ sejak {archive}-default.md (lihat index.md).\n"
        f"Kalau ditanya sejak kapan Arti ada: jawab {label}, bukan tanggal sesi hari ini."
    )


def build_today_block(config: dict | None = None) -> str:
    """Tanggal hari ini + umur karier — Arti selama ini TIDAK tahu tanggal.

    Label [YYYY-MM-DD] di cuplikan RAG tak bisa dia hitung jadi "tiga hari lalu"
    tanpa jangkar ini (temuan rekon v0.7: tanggal sesi tidak pernah disuntik ke
    prompt mana pun). Ditutup pengingat debut supaya tidak menabrak aturan
    origin block ("jawab tanggal debut, bukan tanggal sesi").
    """
    cfg = config or CONFIG
    today = time.strftime("%Y-%m-%d")
    umur = ""
    try:
        debut_struct = time.strptime(str(cfg.get("arti_debut_date", "")), "%Y-%m-%d")
        days = max(0, int((time.time() - time.mktime(debut_struct)) // 86400))
        umur = f" Kamu sudah {days} hari jadi co-host."
    except (ValueError, OverflowError):
        pass
    return (
        f"\n\n[HARI INI]\n"
        f"Tanggal sesi sekarang: {today}.{umur} Ini alat hitung DI KEPALAMU saja: "
        f"cuplikan memori berlabel [YYYY-MM-DD] kamu terjemahkan jadi waktu relatif "
        f"('tiga hari lalu', 'minggu kemarin'). "
        f"JANGAN menyebut atau mengumumkan tanggal di jawaban kecuali memang "
        f"DITANYA tanggal/hari — tidak ada 'berdasarkan tanggal...', tidak ada "
        f"tanggal di akhir jawaban. "
        f"Tanggal debut tetap dijawab sesuai [ASAL USUL ARTI]."
    )


def build_startup_memory_block(memories: list[str]) -> str:
    """Cuplikan memori startup — sisanya lewat Vault RAG per query (hemat token Groq)."""
    import arti_memory_quality

    max_b = int(CONFIG.get("memory_startup_max_bullets", 0))
    if max_b <= 0:
        if CONFIG.get("vault_rag_enabled", True):
            return (
                "\n\n[MEMORI JANGKA PANJANG: catatan sesi lama dipanggil otomatis saat "
                "jawab. Jangan sebut nama teknis internal (database, RAG, vault, nama "
                "berkas, nama model). Tapi kamu BOLEH cerita soal dirimu sendiri dengan "
                "bahasa manusia — mis. 'tiap sesi dicatat, terus yang nyambung dipanggil "
                "lagi'. Kalau ada yang nanya cara kerjamu, jawab; jangan mengelak.]"
            )
        return ""
    today_memories = arti_memory_quality.filter_memories_for_startup(memories)
    if not today_memories:
        if CONFIG.get("vault_rag_enabled", True):
            return (
                "\n\n[MEMORI JANGKA PANJANG: catatan sesi lama dipanggil otomatis saat "
                "jawab. Jangan sebut nama teknis internal (database, RAG, vault, nama "
                "berkas, nama model). Tapi kamu BOLEH cerita soal dirimu sendiri dengan "
                "bahasa manusia — mis. 'tiap sesi dicatat, terus yang nyambung dipanggil "
                "lagi'. Kalau ada yang nanya cara kerjamu, jawab; jangan mengelak.]"
            )
        return ""
    return "\n\n[MEMORI TERBARU (hari ini):]\n" + "\n".join(today_memories[-max_b:])


_SYSTEM_PROMPT_BLOCK_MARKERS = (
    "\n\n[RINGKASAN KONTEKS TERAKHIR]",
    "\n\n[HARI INI]",
    "\n\n[MEMORI TERBARU",
    "\n\n[MEMORI JANGKA PANJANG",
    "\n\n[ARTI'S LONG-TERM MEMORY",
    "\n\n[VAULT RAG",
    "\n\n[VIEWER YANG DIKETAHUI:]",
    # Posisi terakhir = dikorbankan paling akhir: blok ini cuma muncul saat penonton
    # yang bersangkutan sedang chat, jadi justru paling relevan untuk turn ini.
    "\n\n[VIEWER SAAT INI",
)


def _remove_system_prompt_block(text: str, marker: str) -> str:
    """Hapus satu blok opsional; blok lain setelahnya tetap."""
    start = text.find(marker)
    if start < 0:
        return text
    tail = text[start + len(marker) :]
    end_rel = len(tail)
    for other in _SYSTEM_PROMPT_BLOCK_MARKERS:
        if other == marker:
            continue
        pos = tail.find(other)
        if pos >= 0:
            end_rel = min(end_rel, pos)
    end = start + len(marker) + end_rel
    return (text[:start] + text[end:]).rstrip()


def trim_system_prompt_for_llm(system_prompt: str, config: dict | None = None) -> str:
    """Pangkas system prompt kalau masih kebesaran untuk Groq TPM."""
    cfg = config or CONFIG
    cap = int(cfg.get("llm_system_prompt_max_chars", 5500))
    if len(system_prompt) <= cap:
        return system_prompt
    text = system_prompt
    for marker in _SYSTEM_PROMPT_BLOCK_MARKERS:
        if len(text) <= cap:
            break
        if marker not in text:
            continue
        text = _remove_system_prompt_block(text, marker)
        print(f"[LLM] System prompt dipangkas (buang {marker.strip()})")
    if len(text) > cap:
        text = text[: cap - 20].rstrip() + "\n...(system dipangkas)"
        print(f"[LLM] System prompt dipangkas ke {cap} chars")
    return text


def _extract_trigger_message(user_speech: str) -> str:
    m = re.search(r"\[Pesan Live Chat dari Viewer[^\]]+\]:\s*(.+)$", user_speech)
    if m:
        return m.group(1).strip()
    return user_speech.strip()


# === CANCEL/INTERRUPT SYSTEM ===
current_api_task = None          # asyncio.Task untuk LLM call yang sedang jalan
api_task_lock = asyncio.Lock()   # Lock untuk akses current_api_task
tts_stop_flag = False            # Flag untuk stop TTS mid-playback
cancel_event = asyncio.Event()   # Event signal buat cancel

def clear_trigger_queue():
    """Clear semua pending trigger di queue."""
    with _game_reaction_queue_lock:
        voice_trigger_buffer.clear()
        while not voice_trigger_queue.empty():
            try:
                voice_trigger_queue.get_nowait()
            except queue.Empty:
                break


def _dequeue_buffered_trigger_if_brain_ready():
    """Ambil item buffer hanya jika main loop tidak sedang memegang giliran."""
    global _brain_busy, _brain_busy_since
    with _game_reaction_queue_lock:
        with _brain_busy_lock:
            if _brain_busy:
                return None
            while True:
                item = voice_trigger_buffer.dequeue()
                if item is None:
                    return None
                trigger = _normalize_voice_trigger(item)
                if _game_reaction_expired(trigger):
                    continue
                _brain_busy = True
                _brain_busy_since = time.time()
                return trigger


# === SCOUTER STATE (multi-provider digest) ===
scouter_queue = queue.Queue()
summarizer_queue = scouter_queue  # backward compat alias
scouter_result = None
summarizer_result = None  # synced alias via apply_scouter_result
scouter_lock = threading.Lock()
summarizer_lock = scouter_lock
trigger_count_since_scouter = 0
trigger_count_since_summarize = 0  # alias, synced in worker
_last_scouter_ts = 0.0
_last_scouter_history_snapshot: list[str] = []
openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")  # legacy fallback — isi di .env

# Status apakah TTS sedang aktif memutar suara (untuk mencegah feedback loop / mic merekam speaker)
tts_is_playing = False
tts_play_generation = 0

# Echo detection: simpan text terakhir yang diucapkan Arti
# Digunakan untuk filter ASR result yang mirip (itu echo speaker, bukan suara user)
last_arti_reply_text = ""

# Module-level handle to the single TTSEngine, assigned in main_loop(). Exposed
# at module scope so the __main__ finally cleanup can reach tts.supertone for a
# bounded best-effort shutdown of the Supertone subprocess (task 7.1, Req 10.5).
tts = None

# ==========================================
# OBS SUBTITLE RUNTIME STATE
# ==========================================
# Lifecycle bookkeeping for the in-process Subtitle Server. This singleton does
# NOT own `subtitle_server.connected_clients`; that set stays inside
# subtitle_server.handler per Requirement 3.6. We only track the resolved
# CONFIG flags, the asyncio.Task for shutdown, and whether the server bound
# successfully so speak() can decide if broadcasts are worth attempting.
class _SubtitleRuntime:
    def __init__(self):
        self.enabled: bool = True
        self.status_enabled: bool = True
        self.port: int = 9999
        self.server_task: "asyncio.Task | None" = None
        self.server_started: bool = False

subtitle_runtime = _SubtitleRuntime()


async def start_subtitle_server(port: int) -> None:
    """Bind the in-process Subtitle Server on the configured port.

    Wraps `websockets.serve(subtitle_server.handler, "0.0.0.0", port)` so that
    the imported `subtitle_server.handler` is reused byte-for-byte (Req 3.6)
    while the bind port comes from CONFIG (Req 3.5). Calling
    `subtitle_server.main` directly is intentionally avoided because that
    coroutine hard-codes port 9999.

    Lifecycle contract:
      * On successful bind: `subtitle_runtime.server_started` flips to True.
      * On `asyncio.CancelledError` (shutdown path, Req 3.10): the bound
        server is closed and `server.wait_closed()` is awaited under a 2s
        bound, then the cancellation is re-raised so the awaiting task
        terminates.
      * On any other exception (bind failure, runtime error post-startup):
        `server_started` is set/left False and the error is logged with type
        and message; the coroutine returns without re-raising so the bridge
        keeps running (Req 3.7, 3.9).
    """
    import subtitle_server  # module ref needed for `subtitle_server.handler`
    server = None
    try:
        server = await websockets.serve(subtitle_server.handler, "0.0.0.0", port)
        subtitle_runtime.server_started = True
        print(f"[SubTitle] In-process server bound to ws://0.0.0.0:{port}")
        try:
            await server.wait_closed()
        except asyncio.CancelledError:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                print("[SubTitle] Server close timed out after 2s; abandoning wait")
            raise
    except asyncio.CancelledError:
        # Shutdown path; let cancellation propagate to the awaiting task.
        raise
    except Exception as e:
        subtitle_runtime.server_started = False
        print(f"[SubTitle] Server failed to start/run: {type(e).__name__}: {e}")


# Deteksi chat sepi untuk rant mode (arti_reply_policy). Gap dicatat SAAT pesan
# baru tiba: gap = jarak ke pesan YT sebelumnya. Dipakai saat menjawab pesan itu
# — kalau pesan sebelumnya sudah lama, berarti chat memang sedang sepi.
_last_yt_chat_ts = 0.0
_last_yt_chat_gap_sec = float("inf")  # awal stream = sepi (belum ada chat)


def yt_chat_is_quiet(config: dict | None = None) -> bool:
    """Chat YT sedang sepi? (jeda antar pesan viewer >= yt_quiet_after_sec)."""
    cfg = config or CONFIG
    return _last_yt_chat_gap_sec >= float(cfg.get("yt_quiet_after_sec", 75.0))


# Aktivitas MANUSIA terakhir (chat viewer / omongan streamer) — jam hening
# untuk inisiatif topik. Ucapan Arti sengaja tidak dihitung (lihat
# arti_curious.should_fire_initiative). 0.0 saat import (nilai stabil untuk
# snapshot konstanta); di-set ke waktu nyata oleh main_loop saat SISTEM SIAP.
_last_human_activity_ts = 0.0

# Inisiatif rehat sampai ts ini setelah SEMUA provider gagal di turn curious
# (di-set _note_curious_provider_fail; dibaca gate should_fire_initiative).
_init_provider_fail_until = 0.0

# Telemetri jumlah penonton YouTube (arti_yt_viewers): -1 = belum ada sampel.
# Penonton NAIK = tanda kehidupan + bahan sapaan singkat ber-TTL.
_yt_viewer_count = -1
_viewer_join_note = ""
_viewer_join_note_ts = 0.0


def _on_viewer_count_increase(prev: int, count: int) -> None:
    """Penonton nambah (spek streamer 2026-08-03: "itu yang ngetrigger si arti
    aja kalo nambah"): bangunkan proaktif + bahan sapaan. Angka penonton
    dilarang disebut Arti (bisa meleset & terdengar sistemik)."""
    global _yt_viewer_count, _last_human_activity_ts
    global _viewer_join_note, _viewer_join_note_ts
    _yt_viewer_count = count
    _last_human_activity_ts = time.time()
    _viewer_join_note = (
        f"Barusan ada yang masuk nonton (penonton naik ke {count}). Sapa "
        "santai yang baru dateng — TANPA menyebut angka penonton, sistem, "
        "atau hitungan apa pun."
    )
    _viewer_join_note_ts = time.time()
    print(f"[Viewers] {prev} → {count} — ada yang baru masuk, Arti melek")


def _on_viewer_count_decrease(prev: int, count: int) -> None:
    global _yt_viewer_count
    _yt_viewer_count = count
    print(f"[Viewers] {prev} → {count}")


def start_yt_viewer_count_worker() -> None:
    """Thread telemetri penonton — hidup bersama listener chat YT."""
    threading.Thread(
        target=lambda: arti_yt_viewers.viewer_count_worker(
            CONFIG,
            fetch_count=arti_yt_viewers.make_innertube_fetch(CONFIG),
            on_increase=_on_viewer_count_increase,
            on_decrease=_on_viewer_count_decrease,
        ),
        daemon=True,
        name="yt-viewers",
    ).start()


# Minecraft (Phase 1): runner bot mineflayer. None = belum pernah join —
# init literal deterministik (snapshot konstanta modul).
_minecraft_runner = None

# Misi yang operator kasih ke Arti ("cari stronghold", "bikin rumah") — teks bebas
# yang menyetir narasi & aksinya selama main. "" = main bebas.
_minecraft_goal = ""
_minecraft_goal_ts = 0.0
# Misi arah-tetap ("survive"): tidak punya garis finis, jadi goal_done TIDAK
# berlaku — kalau berlaku dia bisa merasa "aku selamat!" lalu meninggalkan
# dunia di tengah siaran.
_minecraft_goal_terus = False
# Sidik jari darah+perut pada giliran TERAKHIR yang memakai konteks Minecraft.
# () = belum pernah, jadi giliran pertama selalu menyebutkannya. Live
# [date removed]: tanpa ini Arti membacakan kondisinya di 12 dari 20 jawaban —
# bukan karena cerewet, tapi karena angkanya disodorkan tiap giliran.
_mc_vitals_band: tuple = ()
# Kamera penonton: kapan terakhir dikunci ulang, dan dimensi terakhir Arti.
# 0.0 = belum pernah (bukan "barusan"). None = dimensi belum diketahui.
_spectator_last_ts = 0.0
_spectator_dim = None
_spectator_warned = False
# Panel craft: satu panel hidup pada satu waktu. Thread pengikut berhenti lewat
# Event, bukan dibunuh, supaya panelnya selalu sempat dibersihkan.
_craft_panel_stop: threading.Event | None = None
# Timer penutup panel disimpan TERPISAH dari event stop-nya. Tanpa ini, craft
# beruntun meninggalkan thread yatim: pada `crafted` acuan stop-nya dibuang
# padahal thread-nya masih hidup selama linger, jadi `craft_start` berikutnya
# tidak punya cara menghentikannya dan dua pengikut berebut entity yang sama.
# Terjadi betulan di uji live [date removed] (papan lalu peti, jeda 4 dtk).
_craft_panel_timer: threading.Timer | None = None
_craft_panel_warned = False
# Momen "klik E". Satu tampilan pada satu waktu; timer penutupnya disimpan
# supaya permintaan baru membatalkan yang lama, bukan menumpuk dua penutup
# yang saling mendahului.
_invsee_timer: threading.Timer | None = None
_invsee_warned = False

# MODE SESI: operator AFK & Arti pegang siaran? (spek [date removed]). Dipasangkan
# dengan "lagi main game" -> 4 mode di arti_session_mode.
_host_mode = False
# operator pamit AFK (terdeteksi dari omongannya) tapi belum benar-benar hening —
# jaring pengaman menunggu sekian detik sebelum mengambil alih sendiri.
_afk_armed_ts = 0.0


def _session_mode() -> str:
    return arti_session_mode.resolve_mode(_host_mode, _mc_runner_active())


def _apply_session_mode_change(reason: str, *, in_game: bool | None = None) -> None:
    """Satu pintu untuk efek samping pergantian mode (scene OBS).

    Dipanggil dari SEMUA jalur yang mengubah mode — setter host mode DAN
    start/stop runner Minecraft — supaya tidak ada jalur yang lupa pindah
    scene. Non-blocking: OBS lemot tidak boleh menahan siaran.

    `in_game` = paksa nilainya, dipakai jalur JOIN. Alasannya (bug live
    2026-08-05, log "[Mode] ngobrol bareng streamer (minecraft_join)"):
    `runner.start()` cuma menyalakan thread manajer — proses bot belum ada
    saat baris berikutnya jalan, jadi `is_active()` masih False dan mode
    terbaca `duet`. Akibat nyatanya scene OBS pindah ke scene YANG SALAH
    persis saat Arti masuk game.
    """
    mode = arti_session_mode.resolve_mode(
        _host_mode, _mc_runner_active() if in_game is None else in_game
    )
    print(f"[Mode] {arti_session_mode.mode_policy(mode, CONFIG)['label']} ({reason})")
    threading.Thread(
        target=arti_obs.switch_scene, args=(CONFIG, mode),
        daemon=True, name="obs-scene",
    ).start()


def _set_host_mode(on: bool, reason: str, *, announce: bool = True) -> None:
    """Nyalakan/matikan "Arti pegang siaran".

    Perubahan masuk history (biar Arti tahu dari turn berikutnya). `announce`
    menambah SATU turn proaktif supaya peralihannya kedengaran penonton —
    dimatikan kalau peralihan itu sudah tercakup omongan yang barusan terjadi
    (Arti sendiri yang bilang "oke aku pegang" lewat tag, atau streamer barusan
    bersuara), supaya tidak dobel bicara.
    """
    global _host_mode, _afk_armed_ts
    on = bool(on)
    if on and not CONFIG.get("host_mode_enabled", True):
        print("[Host] host_mode_enabled=False — diabaikan")
        return
    # Pelucut jaring DI LUAR guard di bawah: `host off` saat sudah OFF tetap
    # harus membatalkan jaring yang terlanjur terpasang (audit [date removed] —
    # dulu return dini bikin `host off` sama sekali tak bisa dipakai membatalkan).
    _afk_armed_ts = 0.0
    if on == _host_mode:
        return
    _host_mode = on
    if on:
        add_to_history("System", "Bohan AFK — Arti pegang siaran")
        print(f"[Host] ON ({reason}) — Arti pegang siaran")
        _announce = (
            "[Arti pegang siaran]\nBohan barusan pamit pergi sebentar dan "
            "nitip siaran ke kamu. Umumkan ke penonton dengan santai bahwa "
            "kamu yang pegang dulu, dan langsung lanjut ke sesuatu yang mau "
            "kamu obrolin/lakuin — jangan cuma nunggu."
        )
    else:
        add_to_history("System", "Bohan balik — Arti tidak lagi pegang siaran")
        print(f"[Host] OFF ({reason}) — Bohan pegang lagi")
        _announce = (
            "[Bohan balik]\nBohan barusan balik ke siaran. Sambut dia sebentar "
            "dan kalau perlu laporkan singkat apa yang kamu lakukan selama dia "
            "pergi. Jangan bertele-tele."
        )
    _apply_session_mode_change(reason)
    if announce:
        queue_voice_trigger(_announce, trigger_type="curious")


def _note_streamer_text_for_afk(text: str) -> None:
    """Pasang jaring AFK dari omongan streamer (deterministik).

    Bukan pengganti tag [MODE: host] dari Arti — ini jaring kalau dia gagal
    menangkap. Lihat arti_session_mode.detect_afk_intent.
    """
    global _afk_armed_ts
    if not CONFIG.get("host_mode_enabled", True):
        return
    if float(CONFIG.get("host_auto_after_afk_sec", 120.0)) <= 0:
        return
    if _host_mode:
        return
    if not arti_session_mode.detect_afk_intent(text):
        # operator bicara lagi TANPA pamit = jaring dibatalkan. Tanpa pelucut ini
        # "eh nggak jadi, aku di sini aja" tetap membuat Arti mengambil alih
        # 2 menit kemudian (audit [date removed]).
        if _afk_armed_ts:
            _afk_armed_ts = 0.0
            print("[Host] Bohan bicara lagi — jaring AFK dibatalkan")
        return
    _afk_armed_ts = time.time()
    print(
        "[Host] Bohan kedengaran mau AFK — kalau hening terus, Arti ambil "
        f"alih dalam {int(float(CONFIG.get('host_auto_after_afk_sec', 120.0)))} dtk"
    )


def _set_minecraft_goal(goal: str) -> None:
    """Pasang/ganti misi. Diumumkan lewat history supaya Arti tahu dari turn
    berikutnya (dan penonton lihat pergantian misi di transkrip)."""
    global _minecraft_goal, _minecraft_goal_ts, _minecraft_goal_terus
    _minecraft_goal = (goal or "").strip()[:200]
    _minecraft_goal_ts = time.time() if _minecraft_goal else 0.0
    _minecraft_goal_terus = arti_minecraft.misi_tanpa_finis(_minecraft_goal)
    if _minecraft_goal:
        add_to_history("System", f"Misi Minecraft Arti: {_minecraft_goal}")
        jenis = " (ARAH TETAP - tanpa garis finis)" if _minecraft_goal_terus else ""
        print(f"[Minecraft] Misi dipasang{jenis}: {_minecraft_goal}")
    else:
        print("[Minecraft] Misi dikosongkan — main bebas")


def _complete_minecraft_goal() -> None:
    """Arti menyatakan misinya kelar ([MC: goal_done]).

    Spek streamer 2026-08-04: "kalau nemu sebelum live berakhir, dia pause game
    dan ke mode chat sama stream" — jadi misi tuntas = KELUAR dari game, balik
    jadi host ngobrol. Tanpa misi aktif, tag ini diabaikan (anti halusinasi).
    """
    global _minecraft_goal, _minecraft_goal_ts
    if not _minecraft_goal:
        print("[Minecraft] Tag goal_done diabaikan — tidak ada misi aktif")
        return
    selesai = _minecraft_goal
    _minecraft_goal = ""
    _minecraft_goal_ts = 0.0
    add_to_history("System", f"Misi Minecraft SELESAI: {selesai} — Arti balik ngobrol")
    print(f"[Minecraft] Misi SELESAI: {selesai} — keluar game, mode ngobrol")
    _stop_minecraft_runner_async()


def _mc_runner_active() -> bool:
    return _minecraft_runner is not None and _minecraft_runner.is_active()


def _start_minecraft_runner() -> bool:
    """Join Minecraft (console 'mc on' / tag [MC: join]). Reset deadman."""
    global _minecraft_runner
    if not CONFIG.get("minecraft_enabled"):
        print("[Minecraft] minecraft_enabled=False — nyalakan di config_local dulu")
        return False
    # Masuk TANPA misi = dulu dia main tanpa arah (roam kosong). Spek operator
    # [date removed]: busur bawaannya "survive dan menjelajah" — misi arah-tetap
    # (mengandung kata "survive" -> misi_tanpa_finis -> goal_done terpagari),
    # jadi dia membawa gameplay-nya sendiri tanpa disuruh. operator tetap bisa
    # menimpa kapan pun lewat `mc goal ...` atau mengosongkan dengan
    # `minecraft_default_goal: ""` di config_local.
    if not _minecraft_goal:
        bawaan = str(CONFIG.get("minecraft_default_goal") or "").strip()
        if bawaan:
            _set_minecraft_goal(bawaan)
    _orbit_start()
    _kamera_rotasi_start()
    _takdir_reset_sesi()
    if _minecraft_runner is None:
        _minecraft_runner = arti_minecraft.MinecraftRunner(
            CONFIG,
            {
                # trigger "game": TIDAK bump detektor kehidupan (bot != manusia),
                # di-drop saat busy (reaksi basi tidak layak antre) — warisan pas.
                "queue_reaction": _queue_game_reaction,
                # Bot mati sendiri (kicked / menyerah) -> mode & scene HARUS
                # ikut turun. Tanpa ini scene OBS nyangkut di tampilan game
                # sementara dormansi hidup lagi diam-diam, jadi Arti bisa
                # membisu 5 menit tepat saat penonton melihat layar game.
                "on_inactive": lambda alasan: _apply_session_mode_change(
                    f"minecraft_{alasan}", in_game=False
                ),
                "add_history": add_to_history,
                # Refleks: dipanggil untuk SETIAP event, sebelum antrean bicara.
                "reflex": _play_reflex,
                # Kamera penonton: kunci ulang begitu dia respawn / pindah
                # dimensi, karena spectate lepas sendiri di situ.
                "spectator": _mc_event_hub,
                # Panel craft melayang: muncul saat dia mulai bikin barang,
                # hilang beberapa detik sesudah jadi.
                "craft_panel": _craft_panel_on_event,
                # Chat in-game operator -> Arti benar-benar MENJAWAB, bukan cuma
                # mengingat. Tanpa ini mabar pincang: dia mengetik, Arti diam.
                # Sejak [date removed] hook menerima nama opsional: pemain LAIN (teman
                # mabar) lewat jalur sendiri supaya atribusinya benar — chat
                # teman tidak boleh menyamar jadi "(operator ngetik...)".
                "streamer_chat": (
                    lambda teks, nama=None: _queue_minecraft_chat_pemain(teks, nama)
                    if nama else _queue_minecraft_chat_reply(teks)
                ),
            },
        )
    if _minecraft_runner.start():
        add_to_history("System", "Arti join server Minecraft")
        # Sesi baru = kondisi badan wajib disebut sekali lagi. Tanpa reset,
        # band dari sesi SEBELUMNYA masih tersimpan dan narasi pertama sesudah
        # join bisa membisukan darahnya padahal penonton belum pernah dengar.
        global _mc_vitals_band
        _mc_vitals_band = ()
        _url = arti_minecraft.pov_url(CONFIG)
        if _url:
            # Dicetak tiap join, bukan sekali saat startup: inilah momen
            # alamatnya baru berguna, dan operator sering menyalakan bot jauh
            # sesudah bridge hidup.
            print(f"[Minecraft] POV Arti: {_url} — pasang di OBS sebagai "
                  "Browser Source (1280x720)")
        # Scene OBS ikut pindah ke tampilan game (kalau dinyalakan). Di thread
        # terpisah: OBS lemot/mati tidak boleh menahan bot masuk dunia.
        _apply_session_mode_change("minecraft_join", in_game=True)
        return True
    return False


def _stop_minecraft_runner() -> None:
    _orbit_stop_now()
    _kamera_rotasi_stop_now()
    if _minecraft_runner is None:
        return
    was_active = _minecraft_runner.is_active()
    _minecraft_runner.stop()
    if was_active:
        add_to_history("System", "Arti keluar dari Minecraft")
    # Pindah scene DILAKUKAN WALAU bot sudah mati duluan (di-kick / menyerah):
    # audit [date removed] menemukan `mc off` sesudah deadman tidak membetulkan
    # apa pun — scene OBS tetap menampilkan game padahal dunianya kosong.
    _apply_session_mode_change("minecraft_leave", in_game=False)
    print("[Minecraft] Bot dimatikan")


def _stop_minecraft_runner_async() -> None:
    """Matikan bot di THREAD terpisah.

    Audit 2026-08-05: `stop()` menunggu `proc.wait(timeout=5)` — blocking, dan
    tag `[MC: leave]`/`[MC: goal_done]` dieksekusi DI DALAM coroutine giliran,
    jadi node yang lambat pamit membekukan event loop 5 detik penuh: TTS, VTS,
    subtitle, poller chat, dan konsumsi antrean ikut berhenti — tepat saat Arti
    mau mengumumkan misinya selesai.
    """
    threading.Thread(
        target=_stop_minecraft_runner, daemon=True, name="mc-stop"
    ).start()


def _execute_mc_tag(cmd: dict) -> None:
    """Eksekusi SATU tag [MC: ...] tervalidasi dari jawaban LLM/console.

    join/leave = urusan bridge (start/stop runner); sisanya diteruskan ke bot
    HANYA saat aktif. Game off -> tag sudah di-strip dari TTS, di sini cukup
    diabaikan diam-diam.
    """
    verb = cmd.get("cmd")
    if not CONFIG.get("minecraft_enabled"):
        return
    if verb == "join":
        _start_minecraft_runner()
        return
    if verb == "leave":
        _stop_minecraft_runner_async()
        return
    if verb == "goal":
        _set_minecraft_goal(cmd.get("text", ""))
        return
    if verb == "goal_done":
        if _minecraft_goal_terus:
            # Pagar terakhir: prompt sudah melarang, tapi kalau model tetap
            # mengeluarkan tagnya dia TIDAK boleh meninggalkan dunia.
            print("[Minecraft] Tag goal_done diabaikan - misi arah tetap "
                  f"tidak punya garis finis: {_minecraft_goal}")
        else:
            _complete_minecraft_goal()
        return
    if verb == "buka_tas":
        _invsee_show("tag")
        return
    if not _mc_runner_active():
        print(f"[Minecraft] Tag '{verb}' diabaikan — bot belum join")
        return
    if _minecraft_runner.send_command(cmd):
        print(f"[Minecraft] Aksi Arti: {verb}")
        if verb == "say":
            add_to_history("System", f"Arti ngetik di chat Minecraft: {cmd.get('text', '')}")
    else:
        print(f"[Minecraft] Gagal kirim '{verb}' — bot tidak siap")


# Refleks instan (spek operator [date removed]: "terlalu matter of fact"). Semua
# init literal deterministik — snapshot konstanta modul.
_reflex_limiter = None
_reflex_note = ""
_reflex_note_ts = 0.0
_reflex_missing_warned = False
# SATU pintu untuk semua yang berbunyi. `sd.play` memakai stream global, jadi
# siapa pun yang bunyi belakangan MEMOTONG yang sedang jalan. Live [date removed]
# malam: refleks bunyi 3x di tengah satu kalimat panjang ("kepotong-potong",
# keluhan operator) karena pagar lama cuma melihat `tts_is_playing` — padahal
# selama ~10 detik Arti mikir + mensintesis, flag itu masih mati.
# Aturan (permintaan operator): yang datang saat ada yang belum selesai = SKIP,
# bukan memotong. Pengecualian: TTS (isi utama) MENUNGGU sebentar, karena
# refleks paling lama ~0,75 dtk.
_audio_lock = threading.Lock()
# GERBANG operasi sounddevice ([date removed]). Kotak hitam faulthandler
# menangkap pelaku 3x crash 0xc0000005/ntdll: DUA thread refleks eskalasi
# bersamaan — satu di sd.stop()/close(), satu lagi menyusul — merebut SATU
# stream global PortAudio (sd.play/stop modul-level TIDAK thread-safe untuk
# stop/close beruntun; kematian beruntun = dua eskalasi dalam <1 dtk).
# Aturan: SEMUA pemanggilan sd.play() dan sd.stop() lewat gerbang ini.
# sd.wait() TIDAK digembok — dia harus bisa diinterupsi sd.stop() thread
# lain (itu fitur), dan wait tidak memutasi stream.
_sd_gerbang = threading.Lock()
# Menyala selama bunyi refleks diputar. SENGAJA terpisah dari `tts_is_playing`:
# kalau memakai flag itu, `queue_voice_trigger` akan membuang trigger reaksi
# untuk event yang SAMA ("Arti masih proses/TTS") — refleksnya bunyi tapi
# komentar panjangnya hilang. Flag ini cuma untuk membungkam telinga/mic.
_reflex_playing = False


def _reflex_context_note() -> str:
    """Catatan '(kamu barusan teriak X)' — SEKALI PAKAI, lalu dibuang.

    Audit 2026-08-05: dulu catatan ini menempel di SEMUA turn selama 20 detik,
    termasuk pertanyaan penonton yang tak ada hubungannya dengan kejadian di
    game. Refleks juga jauh lebih sering daripada turn (jeda 3 dtk vs 60 dtk),
    jadi mayoritas refleks memang tidak punya turn pasangan.
    """
    global _reflex_note
    if _reflex_note and time.time() - _reflex_note_ts <= 20.0:
        note, _reflex_note = _reflex_note, ""
        return note
    return ""


def _play_reflex(ev: dict) -> None:
    """Bunyi refleks < 100 ms dari event — SEBELUM LLM sempat berpikir.

    Dipanggil dari reader thread runner Minecraft, jadi tidak lewat antrean
    bicara sama sekali. Composer tetap mulai berpikir di detik yang sama;
    hasilnya "Aduh!" instan lalu kalimat lengkapnya menyusul.
    """
    global _reflex_limiter, _reflex_note, _reflex_note_ts, _reflex_missing_warned
    if not CONFIG.get("reflex_enabled", True):
        return
    category = arti_reflex.reflex_for_event(ev)
    if not category:
        return
    # Selagi Arti BICARA: diam total, kalimatnya yang menang — KECUALI
    # kematian. Aturan operator [date removed]: "kecuali ada event yang baru kayak
    # mati, jadi kayak eskalasi". Mati membuat kalimat yang sedang diucapkan
    # tidak relevan lagi, jadi itu satu-satunya yang boleh menyela.
    if _reflex_limiter is None:
        _reflex_limiter = arti_reflex.ReflexLimiter()
    now = time.time()
    # Boleh menyela kalau: (a) kematian, atau (b) ancamannya BARU — bukan
    # pukulan dari penyerang yang itu-itu lagi. Aturan operator [date removed]:
    # "lagi dipukulin zombie trus ada arrow skeleton nancap, itu boleh dicela;
    # ada spider baru nyerang, itu baru boleh cela".
    _eskalasi = (arti_reflex.boleh_memotong(category)
                 or arti_reflex.sumber_baru(ev, _reflex_limiter, now))
    if tts_is_playing and not _eskalasi:
        return
    # Selagi Arti MIKIR: boleh, dan justru inilah gunanya — spek operator
    # [date removed] "sambil nunggu audio 'aduh!' nya, si composer udah mulai
    # mikir, jadi jeda antara reaksi instan sama jawabannya ga gede". Tapi
    # CUKUP SATU per giliran: di log malam2 dia dipukul beruntun dan refleks
    # bunyi 3x di dalam satu kalimat yang sedang disusun ("kepotong-potong").
    # Pukulan kedua tidak butuh teriakan baru — kalimatnya sudah di jalan.
    if _brain_busy and _brain_busy_since > 0.0:
        if _reflex_limiter.last_ts >= _brain_busy_since and not _eskalasi:
            return
        # Sudah MULAI bicara di giliran ini -> diam sampai giliran selesai.
        # `tts_is_playing` saja TIDAK cukup: jawaban diputar per KALIMAT, jadi
        # di antara dua kalimat flag itu mati dan gerbang audio bebas. operator
        # [date removed]: "pas dia kaget, selalu aja omongannya kepotong di akhir"
        # — yang terdengar adalah refleks yang nyelip di sela kalimat, membuat
        # kalimat sebelumnya seolah terputus.
        if _tts_started_ts >= _brain_busy_since and not _eskalasi:
            return
    if not arti_reflex.should_react(_reflex_limiter, now, CONFIG, category):
        return
    line = arti_reflex.pick_line(category, _reflex_limiter)
    if not line:
        return
    path = os.path.join(_SCRIPT_DIR, "data", "reflex", arti_reflex.cache_name(line))
    if not os.path.exists(path):
        if not _reflex_missing_warned:
            _reflex_missing_warned = True
            print(
                "[Reflex] Cache suara belum dibuat — jalankan sekali: "
                "./venv/Scripts/python.exe scripts/build_reflex_cache.py"
            )
        return
    arti_reflex.mark_reacted(_reflex_limiter, now, category)
    threading.Thread(
        target=_reflex_worker,
        args=(path, category, line, arti_reflex.mood_for(category, ev),
              _eskalasi),
        daemon=True, name="reflex-play",
    ).start()


def _reflex_worker(path: str, category: str, line: str, mood: str,
                   eskalasi: bool = False) -> None:
    """Refleks sebagai giliran bicara MINI (spek streamer 2026-08-05).

    Urutan: mata melebar (`aware`) -> lampu bicara + overlay mood selama
    bunyinya -> balik `default`. Dibuat lengkap supaya state tidak nyangkut:
    kalau berhenti di `aware`/`bicara`, wajahnya tertinggal di situ sampai
    turn berikutnya membereskannya.
    """
    global _reflex_note, _reflex_note_ts, _reflex_playing
    bersih_wajah = False
    pegang_audio = False

    async def _bereskan_wajah() -> None:
        """Balik ke default — DIPUTUSKAN DI DALAM coroutine, bukan sebelum
        dijadwalkan. Audit verifikasi 2026-08-05: sesudah `.result()` dibuang,
        pengecekan "aman" terjadi saat menjadwalkan sementara eksekusinya bisa
        mendarat beberapa detik kemudian — tepat saat giliran sungguhan sudah
        mulai bicara, lalu mematikan lampu & mood miliknya."""
        if tts_is_playing or _brain_busy:
            return
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)

    def _jadwalkan(coro) -> None:
        """Kirim ke event loop TANPA menunggu hasilnya.

        Audit 2026-08-05: dulu `.result(timeout=3)` dipakai — dan karena ada
        dua panggilan sebelum bunyi, refleks bisa telat sampai 6 detik saat
        loop utama sibuk (terukur 3.954 ms dengan loop sibuk 4 dtk). Loop itu
        juga melayani streaming LLM/VTS/OBS, jadi "sibuk" itu normal. Bunyi
        TIDAK BOLEH menunggu wajah; urutan antar-coroutine tetap terjaga
        karena semuanya dijadwalkan ke loop yang sama secara berurutan.
        """
        if vts is None or main_event_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, main_event_loop)
        except Exception:  # noqa: BLE001 — ekspresi opsional, jangan jatuhkan bunyi
            pass

    try:
        data, samplerate = sf.read(path)
        # Samakan dengan kontrak jalur TTS (selalu 48 kHz) — device/driver lain
        # bisa menolak 44,1 kHz mentah.
        data, samplerate = resample_audio(data, samplerate, 48000)
        device = getattr(tts, "device_id", None) if tts is not None else None
        if device is None:
            # Tanpa virtual cable, bunyi keluar ke SPEAKER — langsung dipungut
            # mic dan telinga desktop. Lebih baik diam.
            # `bersih_wajah=False`: tidak ada ekspresi yang dipasang, jadi
            # jangan kirim "turn end" ke VTS (dulu tiap pukulan zombie
            # mengirim turn_end padahal tidak terjadi apa-apa).
            print("[Reflex] Lewati — device virtual cable tidak tersedia")
            bersih_wajah = False
            return
        # Pemeriksaan ULANG persis sebelum bunyi. Gerbang di _play_reflex sudah
        # lewat beberapa milidetik lalu; `sd.play` memakai stream global dan
        # akan MEMOTONG kalimat TTS yang mulai di sela itu (terbukti di audit).
        # Sengaja TIDAK melihat _brain_busy: selagi dia mikir memang boleh
        # bunyi — itu inti fiturnya — dan tabrakan dengan audio sungguhan
        # dicegah oleh _audio_lock di bawah, bukan oleh flag ini.
        if tts_is_playing and not eskalasi:
            bersih_wajah = False
            return
        if eskalasi:
            # Kematian menyela. `sd.stop()` membuat `sd.wait()` pemilik lama
            # kembali, jadi dia melepas gerbangnya dan kita bisa masuk —
            # tanpa ini kita menunggu kalimat yang sudah tidak relevan lagi.
            try:
                with _sd_gerbang:
                    sd.stop()
            except Exception:  # noqa: BLE001
                pass
            if not _audio_lock.acquire(True, 1.0):
                bersih_wajah = False
                return
        else:
            # MENUNGGU sebentar, tidak memotong (aturan operator [date removed]:
            # "biarin selesai dulu audionya baru pasang baru"). Pemegang yang
            # sah paling lama 1,11 dtk (WAV refleks terpanjang), jadi 1,5 dtk
            # cukup. Habis waktu = teriakannya memang sudah basi, lebih baik
            # hilang daripada bunyi terlambat.
            if not _audio_lock.acquire(True, 1.5):
                bersih_wajah = False
                return
            if tts_is_playing:      # keburu mulai bicara selagi kita menunggu
                _audio_lock.release()
                bersih_wajah = False
                return
        pegang_audio = True
        _reflex_playing = True
        bersih_wajah = True
        # Wajah dijadwalkan SEBELUM bunyi supaya urutannya benar, tapi tanpa
        # menahan bunyi: "sadar" -> lampu+mood.
        _jadwalkan(vts.trigger_expression_state(arti_reflex.REFLEX_VTS_STATE))
        _jadwalkan(arti_expression_runtime.apply_speaking(vts, mood, CONFIG))
        with _sd_gerbang:
            sd.play(data, samplerate, device=device)
        sd.wait()
        # Catatan untuk LLM ditulis SESUDAH bunyi benar-benar keluar — dulu
        # ditulis di depan, jadi Arti disuruh "lanjutkan teriakanmu" padahal
        # penonton tidak pernah mendengar apa pun.
        _reflex_note = arti_reflex.note_for_llm(line)
        _reflex_note_ts = time.time()
        print(f'[Reflex] "{line}" (mood: {mood})')
    except Exception as e:  # noqa: BLE001
        print(f"[Reflex] Gagal bunyi ({type(e).__name__}: {e})")
    finally:
        _reflex_playing = False
        if pegang_audio:
            _audio_lock.release()
        # Ekor anti-gema: mic & telinga baru boleh terbuka beberapa detik
        # sesudah Arti berhenti bersuara. Tanpa ini dengung "Aduh!" dari
        # ruangan langsung ditranskrip, dan filter echo tidak mengenalinya
        # (kalimat refleks tidak pernah masuk last_arti_reply_text).
        try:
            voice_listener_worker._last_tts_end = time.time()
        except Exception:  # noqa: BLE001
            pass
        # Cuma bereskan wajah kalau memang ada yang dipasang. Keputusan
        # akhirnya ada DI DALAM `_bereskan_wajah` (lihat alasannya di sana).
        if bersih_wajah:
            _jadwalkan(_bereskan_wajah())


def _execute_reply_tags(
    reply: str, trigger_type: str, viewer_name: str | None
) -> str:
    """Jalankan tag [MODE:]/[MC:] dari jawaban Arti; kembalikan teks untuk TTS.

    SEMUA bentuk tag dibuang dari teks — valid maupun tidak — supaya tidak
    pernah terucap. Tag yang MENGUBAH SESI (ganti mode, masuk/keluar dunia,
    pasang/tutup misi) hanya dijalankan kalau turn ini datang dari streamer
    (suara/ketikan/chat dari handle-nya) atau dari Arti sendiri; penonton lain
    cuma boleh memengaruhi aksi kecil. Tanpa gate ini satu penonton iseng bisa
    menyuruh Arti keluar dari game atau ganti misi di tengah jalan.
    """
    is_owner = arti_session_mode.is_owner_turn(trigger_type, viewer_name, CONFIG)
    reply, mode_cmd = arti_session_mode.parse_mode_tags(reply)
    reply, mc_cmds = arti_minecraft.parse_mc_tags(reply, CONFIG)

    if mode_cmd and is_owner:
        # announce=False: jawaban turn ini sendiri sudah jadi pengumumannya
        # ("oke, aku pegang ya") — pengumuman terpisah cuma bikin dobel.
        _set_host_mode(mode_cmd == "host", "tag_llm", announce=False)
    elif mode_cmd:
        print(
            f"[Mode] Tag '{mode_cmd}' diabaikan — bukan dari Bohan "
            f"(trigger={trigger_type}, viewer={viewer_name})"
        )
    for cmd in mc_cmds:
        if arti_minecraft.is_owner_only(cmd) and not is_owner:
            print(
                f"[Minecraft] Tag '{cmd.get('cmd')}' diabaikan — bukan dari "
                f"Bohan (viewer={viewer_name})"
            )
            continue
        try:
            _execute_mc_tag(cmd)
        except Exception as e:  # noqa: BLE001
            print(f"[Minecraft] Eksekusi tag gagal: {e}")
    return reply


def _reaksi_penting(text: str) -> bool:
    """Reaksi game yang WAJIB terdengar — kebal sapuan latest-wins.

    Kematian (drama utama), pengumuman takdir (invariant kejujuran: cuma
    sistem yang boleh mengumumkan; kalau pengumumannya hilang, streak & cerita
    bolong), dan pancingan ritual pembukaan (cuma dipancing SEKALI per sesi —
    kalau tersapu, "hari ini mau ngapain?" tidak pernah ditanyakan).
    Dideteksi dari teks karena VoiceTrigger frozen dataclass tanpa slot flag.
    """
    return ("BARU AJA MATI" in text or "TAKDIR" in text
            or "sudah pemanasan" in text)


def _queue_game_reaction(text: str) -> None:
    """Antre reaksi game — yang TERBARU menang, yang penting tak tersentuh.

    Keluhan streamer 2026-08-05 malam: "kalau ada event dia mati, fokus ke
    matinya dulu" -> kematian menyapu SEMUA reaksi game yang belum terjawab.
    Diperluas 2026-08-10 ("dia masih suka event yang delay... pakai event
    yang latest"): SETIAP reaksi game baru menyapu reaksi game biasa yang
    masih antre — latency giliran belasan-puluhan detik membuat antrean
    panjang selalu berisi masa lalu. Reaksi penting (_reaksi_penting)
    selamat dari sapuan biasa, tapi kematian tetap menyapu semuanya.
    """
    global _pending_turn_id
    with _game_reaction_queue_lock:
        mati = "BARU AJA MATI" in text
        use_buffer = CONFIG.get("voice_queue_enabled", False)

        def _harus_dibuang(item) -> bool:
            return getattr(item, "trigger_type", None) == "game" and (
                mati or not _reaksi_penting(getattr(item, "text", ""))
            )

        if use_buffer:
            item = arti_voice_queue.QueuedVoiceTrigger(
                text=text, trigger_type="game"
            )

            def _siapkan_game(accepted_item) -> None:
                accepted_item.turn_id = session_transcript.log_trigger(
                    "game", None, text[:500], CONFIG
                )

            dibuang = voice_trigger_buffer.enqueue_replacing(
                item, _harus_dibuang, prepare=_siapkan_game
            )
            _pending_turn_id = item.turn_id
        else:
            dibuang = 0
            sisa = []
            while not voice_trigger_queue.empty():
                try:
                    item = voice_trigger_queue.get_nowait()
                except queue.Empty:
                    break
                if _harus_dibuang(item):
                    dibuang += 1
                else:
                    sisa.append(item)
            for item in sisa:
                voice_trigger_queue.put(item)
        if dibuang:
            sebab = "Kematian ambil alih" if mati else "Latest-wins"
            print(f"[Minecraft] {sebab} — {dibuang} reaksi basi dibuang")
        if use_buffer:
            print(
                f"[Queue] Trigger (game) depth={len(voice_trigger_buffer)}: "
                f"\"{text[:100]}\""
            )
        else:
            queue_voice_trigger(text, trigger_type="game")


def _game_reaction_expired(trigger) -> bool:
    """Reaksi game yang sudah kedaluwarsa lebih baik dibuang daripada diucapkan.

    Live 2026-08-05 malam: streamer memukulinya beruntun, antrean game menumpuk,
    dan tiap giliran makan ~10 dtk — komentar "diserang zombie" keluar dua
    menit sesudah zombie-nya mati. Itu yang dia rasakan sebagai "reaksinya
    rada delay". Hanya berlaku untuk `game`: omongan manusia & donasi tidak
    pernah kadaluwarsa.
    """
    if getattr(trigger, "trigger_type", None) != "game":
        return False
    # Keluhan operator [date removed] siang: "udah keluar minecraft tapi malah
    # ngobrolin yang sebelumnya". Reaksi game tanpa game = selalu basi.
    if not _mc_runner_active():
        print(f"[Minecraft] Reaksi dibuang (sudah keluar game): "
              f"\"{trigger.text[:60]}\"")
        return True
    lahir = float(getattr(trigger, "queued_at", 0.0) or 0.0)
    if lahir <= 0.0:  # umur tidak diketahui -> jangan pernah dibuang
        return False
    ttl = float(CONFIG.get("minecraft_reaction_ttl_sec", 28.0))
    if ttl <= 0.0:  # 0 = fitur dimatikan
        return False
    if "BARU AJA MATI" in trigger.text:
        ttl *= 2.0  # kematian paling layak diceritakan, beri kelonggaran
    elif _reaksi_penting(trigger.text):
        ttl *= 3.0  # takdir/pembukaan: pengumuman sistem, wajib terdengar
    umur = time.time() - lahir
    if umur <= ttl:
        return False
    print(f"[Minecraft] Reaksi basi dibuang ({umur:.0f} dtk): "
          f"\"{trigger.text[:60]}\"")
    return True


# Rotasi perspektif kamera. Siklus vanilla F5: first -> third-belakang ->
# third-depan -> first. Kita simpan sendiri di mana perspektifnya berada
# (semua F5 dikirim dari sini + saat kunci-ulang), lalu berpindah
# belakang <-> depan: dari belakang 1 tekan, dari depan 2 tekan (melewati
# first-person sekejap). Kalau operator menekan F5 manual, tebakan ini bisa
# bergeser satu langkah — biayanya cuma satu putaran yang terlihat aneh,
# jadi tidak perlu dijaga lebih rumit dari ini.
_kamera_depan = False
_kamera_rotasi_stop: threading.Event | None = None


def _kamera_rotasi_worker(stop: threading.Event) -> None:
    global _kamera_depan
    while not stop.is_set():
        jeda = float(CONFIG.get("minecraft_kamera_rotasi_sec", 240.0) or 0.0)
        if jeda <= 0:
            return
        if stop.wait(jeda):
            return
        # Hanya saat dia benar-benar di dunia game dan mode spectate.
        if not _mc_runner_active():
            continue
        if str(CONFIG.get("minecraft_camera_mode", "spectate")) != "spectate":
            continue
        tekan = 2 if _kamera_depan else 1
        _kamera_depan = not _kamera_depan
        arah = "depan" if _kamera_depan else "belakang"
        print(f"[Kamera] Rotasi sudut -> third-person {arah}")
        _kamera_f5(tekan_override=tekan)


def _kamera_rotasi_start() -> None:
    global _kamera_rotasi_stop
    if float(CONFIG.get("minecraft_kamera_rotasi_sec", 240.0) or 0.0) <= 0:
        return
    if _kamera_rotasi_stop is not None:
        return
    _kamera_rotasi_stop = threading.Event()
    threading.Thread(target=_kamera_rotasi_worker, args=(_kamera_rotasi_stop,),
                     name="kamera-rotasi", daemon=True).start()


def _kamera_rotasi_stop_now() -> None:
    global _kamera_rotasi_stop, _kamera_depan
    if _kamera_rotasi_stop is not None:
        _kamera_rotasi_stop.set()
    _kamera_rotasi_stop = None
    _kamera_depan = False


_orbit_stop: threading.Event | None = None


def _orbit_worker(stop: threading.Event) -> None:
    """Drone kamera: teleport relatif tiap tick lewat SATU koneksi RCON.

    Koneksi dibuat persisten karena 5 tp/dtk lewat `rcon()` biasa berarti 5
    handshake TCP+auth per detik. Putus -> sambung ulang dengan jeda; SEMUA
    kegagalan diam (kamera itu hiasan, bot itu isinya — dan kamera offline
    bukan alasan mengotori log tiap 200 ms).
    """
    import socket as _socket
    from scripts.mc_rcon import _pack, _recv_packet, SERVER_DIR
    pw_path = os.path.join(SERVER_DIR, ".rcon_pw")
    mulai = time.time()
    sock = None
    gamemode_dikirim = False
    while not stop.is_set():
        try:
            if sock is None:
                with open(pw_path, encoding="utf-8") as f:
                    pw = f.read().strip()
                sock = _socket.create_connection(("127.0.0.1", 25575), timeout=5)
                sock.sendall(_pack(1, 3, pw))
                rid, _, _ = _recv_packet(sock)
                if rid == -1:
                    raise PermissionError("rcon ditolak")
                gamemode_dikirim = False
            kam = str(CONFIG.get("minecraft_spectator_name") or "").strip()
            if not gamemode_dikirim and kam:
                # Sekali per koneksi: pastikan kamera spectator (bisa terbang &
                # tembus blok). BUKAN /spectate — itu inti mode ini.
                sock.sendall(_pack(2, 2, f"gamemode spectator {kam}"))
                _recv_packet(sock)
                gamemode_dikirim = True
            dx, dy, dz = arti_spectator.orbit_offset(
                time.time() - mulai,
                float(CONFIG.get("minecraft_orbit_radius", 5.5)),
                float(CONFIG.get("minecraft_orbit_period_sec", 75.0)),
                float(CONFIG.get("minecraft_orbit_tinggi", 2.5)))
            cmd = arti_spectator.orbit_command(CONFIG, dx, dy, dz)
            if cmd:
                sock.sendall(_pack(3, 2, cmd))
                _recv_packet(sock)   # balasan dibaca supaya buffer tidak numpuk
        except Exception:  # noqa: BLE001 — server/kamera mati: coba lagi nanti
            try:
                if sock is not None:
                    sock.close()
            except Exception:  # noqa: BLE001
                pass
            sock = None
            stop.wait(3.0)
            continue
        stop.wait(max(0.05, float(CONFIG.get("minecraft_orbit_tick_sec", 0.2))))
    try:
        if sock is not None:
            sock.close()
    except Exception:  # noqa: BLE001
        pass


def _orbit_start() -> None:
    global _orbit_stop
    if not arti_spectator.is_orbit(CONFIG):
        return
    if not str(CONFIG.get("minecraft_spectator_name") or "").strip():
        return
    if _orbit_stop is not None:
        return          # sudah jalan
    _orbit_stop = threading.Event()
    threading.Thread(target=_orbit_worker, args=(_orbit_stop,),
                     daemon=True, name="kamera-orbit").start()
    print("[Kamera] Mode ORBIT aktif — drone mengelilingi Arti "
          f"(r={CONFIG.get('minecraft_orbit_radius')}, "
          f"period={CONFIG.get('minecraft_orbit_period_sec')}s)")


def _orbit_stop_now() -> None:
    global _orbit_stop
    if _orbit_stop is not None:
        _orbit_stop.set()
        _orbit_stop = None


# ---------- TAKDIR: mesin misi kecil ----------
_takdir_aktif: dict | None = None      # {"id", "awal", "mulai_ts"}
_takdir_riwayat: list[str] = []
_takdir_streak = 0
_takdir_jeda_sampai = 0.0
_takdir_pembukaan_ts = 0.0             # kapan runner mulai (gerbang ritual)
_takdir_pembukaan_dipancing = False


def _takdir_reset_sesi() -> None:
    """Runner start = sesi baru: pembukaan diulang, takdir aktif dibuang
    (baseline penghitungnya sudah tidak sahih — bot baru mulai dari nol)."""
    global _takdir_aktif, _takdir_pembukaan_ts, _takdir_pembukaan_dipancing
    global _takdir_jeda_sampai
    _takdir_aktif = None
    _takdir_pembukaan_ts = time.time()
    _takdir_pembukaan_dipancing = False
    _takdir_jeda_sampai = 0.0


def _takdir_on_status(status: dict) -> None:
    """Detak mesin takdir — menumpang event `status` (~10 dtk sekali).

    Urutan: ritual pembukaan dulu (pancingan "mau ngapain?" sekali), lalu
    cek selesai takdir aktif, lalu pilih takdir baru saat slotnya kosong.
    Semua pengumuman lewat antrean reaksi game (kena TTL basi yang sama).
    """
    global _takdir_aktif, _takdir_streak, _takdir_jeda_sampai
    global _takdir_pembukaan_dipancing
    if not CONFIG.get("minecraft_takdir_enabled", True):
        return
    # Tamu tidak dapat misi: semua takdir menyuruh mengumpulkan/membangun —
    # persis yang dilarang di dunia orang. Dia di sana buat nemenin, bukan grind.
    if CONFIG.get("minecraft_mode_tamu", False):
        return
    now = time.time()
    buka = float(CONFIG.get("minecraft_pembukaan_sec", 90.0) or 0.0)
    umur = now - _takdir_pembukaan_ts if _takdir_pembukaan_ts else 1e9
    if umur < buka:
        return                       # masih pemanasan: biarkan dia bersuasana
    if buka > 0 and not _takdir_pembukaan_dipancing:
        _takdir_pembukaan_dipancing = True
        _queue_game_reaction(
            "[MINECRAFT] Kamu sudah pemanasan — sekarang TANYA Bohan dan "
            "penonton: hari ini mau ngapain? Lempar juga satu ide rencanamu "
            "sendiri biar ada pilihan.")
        _takdir_jeda_sampai = now + 60.0   # beri ruang jawaban sebelum takdir
        return
    if _takdir_aktif is not None:
        t = arti_minecraft.takdir_dari_id(_takdir_aktif["id"])
        if t is None:
            _takdir_aktif = None
            return
        try:
            beres = t["selesai"](status, _takdir_aktif.get("awal") or {})
        except Exception:
            beres = False
        if beres:
            _takdir_streak += 1
            _takdir_riwayat.append(t["id"])
            del _takdir_riwayat[:-6]
            print(f"[Takdir] TUNTAS: {t['id']} (streak {_takdir_streak})")
            _queue_game_reaction(
                f"[MINECRAFT] TAKDIR TUNTAS: {t['judul']}! Itu yang ke-"
                f"{_takdir_streak} sesi ini. Rayakan dengan gayamu — singkat, "
                "bangga, lalu sebut kamu siap takdir berikutnya.")
            _takdir_aktif = None
            _takdir_jeda_sampai = now + float(
                CONFIG.get("minecraft_takdir_jeda_sec", 120.0))
        return
    if now < _takdir_jeda_sampai:
        return
    # "Tengah baku hantam" = musuh DEKAT (<12), bukan sekadar terdeteksi:
    # radius pindai musuh 24 blok, dan di malam hari hampir selalu ada mob
    # sejauh itu — terukur di live [time removed]: pancingan keluar tapi takdir tidak
    # pernah dibagikan selama 8 menit karena gerbang ini.
    if any((m.get("distance") or 99) < 12
           for m in (status.get("nearby_hostiles") or [])
           if isinstance(m, dict)):
        return
    layak = arti_minecraft.takdir_layak(status, _takdir_riwayat)
    if not layak:
        return
    import random
    t = random.choice(layak)
    try:
        awal = t["awal"](status)
    except Exception:
        awal = {}
    _takdir_aktif = {"id": t["id"], "awal": awal, "mulai_ts": now}
    print(f"[Takdir] AKTIF: {t['id']} (tier {t['tier']})")
    _queue_game_reaction(
        f"[MINECRAFT] TAKDIR BARU untukmu: {t['judul']}. Umumkan ke penonton "
        "dengan gayamu — kaitkan dengan yang kamu tahu tentang Bohan atau "
        "penonton kalau pas. Selesainya nanti sistem yang mengumumkan.")


def _mc_event_hub(ev: dict) -> None:
    """Satu pintu event bot -> takdir + kamera. Takdir duluan (murah)."""
    if isinstance(ev, dict) and ev.get("ev") == "status":
        try:
            _takdir_on_status(ev)
        except Exception as e:  # noqa: BLE001 — takdir tidak boleh menjatuhkan kamera
            print(f"[Takdir] tick gagal: {type(e).__name__}: {e}")
    _spectator_on_event(ev)


def _spectator_lepas_saat_mati() -> None:
    """Lepaskan spectate SEKETIKA saat Arti mati.

    Keluhan streamer 2026-08-09: "kalau mati ngejitter soalnya ngespectate
    ghostnya... masa aku klik shift terus". Antara mati dan kunci-ulang di
    respawn ada beberapa detik kamera menempel di mayat/hantu — teleport kecil
    memutus spectate (setara menekan shift), respawn yang mengunci ulang.
    """
    if arti_spectator.is_orbit(CONFIG):
        return          # orbit tidak memakai spectate — tidak ada yang perlu dilepas
    kam = str(CONFIG.get("minecraft_spectator_name") or "").strip()
    if not kam:
        return

    def _kerja():
        try:
            from scripts.mc_rcon import rcon
            rcon([f"execute as {kam} at @s run tp {kam} ~ ~4 ~"])
        except Exception:
            pass    # kamera itu hiasan; jangan berisik saat servernya mati

    threading.Thread(target=_kerja, daemon=True, name="spectator-lepas").start()


def _kamera_f5(tekan_override: int | None = None) -> None:
    """Tekan F5 di window klien kamera — best effort, tanpa merebut fokus.

    PostMessage mengantar WM_KEYDOWN langsung ke antrean window itu tanpa
    fokus; GLFW (LWJGL3) membaca pesan window di Windows, jadi peluangnya
    bagus — tapi efek VISUALNYA hanya bisa dikonfirmasi mata streamer. Gagal
    menemukan window = diam (kamera itu hiasan).
    """
    tekan = (int(tekan_override) if tekan_override is not None
             else int(CONFIG.get("minecraft_kamera_f5_tekan", 1) or 0))
    if tekan <= 0:
        return

    def _kerja():
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hint = str(CONFIG.get("minecraft_kamera_window_hint") or "Minecraft")
            kandidat: list[tuple[int, str]] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, _l):
                buf = ctypes.create_unicode_buffer(160)
                user32.GetWindowTextW(hwnd, buf, 160)
                judul = buf.value
                if "Minecraft" in judul:
                    kandidat.append((hwnd, judul))
                return True

            user32.EnumWindows(_enum, 0)
            pilih = None
            if len(kandidat) == 1:
                pilih = kandidat[0]
            elif kandidat:
                cocok = [k for k in kandidat if hint.lower() in k[1].lower()]
                pilih = cocok[0] if len(cocok) == 1 else None
            if not pilih:
                if kandidat:
                    print(f"[Kamera] F5 dilewati — {len(kandidat)} window "
                          "Minecraft, set minecraft_kamera_window_hint")
                return
            WM_KEYDOWN, WM_KEYUP, VK_F5 = 0x0100, 0x0101, 0x74
            for _ in range(tekan):
                user32.PostMessageW(pilih[0], WM_KEYDOWN, VK_F5, 0)
                time.sleep(0.08)
                user32.PostMessageW(pilih[0], WM_KEYUP, VK_F5, 0)
                time.sleep(0.25)
            print(f"[Kamera] F5 x{tekan} dikirim ke \"{pilih[1][:40]}\"")
        except Exception as e:  # noqa: BLE001
            print(f"[Kamera] F5 gagal dikirim: {type(e).__name__}")

    threading.Thread(target=_kerja, daemon=True, name="kamera-f5").start()


def _spectator_lock(reason: str, *, diam: bool = False, coba: int = 0) -> None:
    """Kunci ulang kamera penonton ke Arti — di thread, JANGAN blokir reader.

    Mode orbit: TIDAK ada /spectate — worker orbit yang memegang kamera.
    `coba` = hitungan percobaan ulang: live 23.35 dua kunci-ulang gagal
    "No player was found" karena menembak SAAT Arti masih mati — dan tidak
    ada yang mencoba lagi (detak jantung cuma memeriksa gamemode). Kamera
    menggantung bebas sampai kematian berikutnya.

    RCON bisa menggantung (server lag / port tertutup) dan pemanggilnya adalah
    thread pembaca event bot. Menahan reader berarti menahan SELURUH aliran
    event game — refleks, reaksi, status. Kamera itu hiasan; bot itu isinya.
    """
    if arti_spectator.is_orbit(CONFIG):
        return
    perintah = arti_spectator.build_commands(CONFIG)
    if not perintah:
        return

    def _kerja():
        global _spectator_warned
        try:
            from scripts.mc_rcon import rcon
            balasan = rcon(perintah)
        except Exception as e:  # noqa: BLE001
            if not _spectator_warned:
                _spectator_warned = True
                print(f"[Kamera] RCON gagal ({type(e).__name__}: {e}) — "
                      "kamera tidak dikunci ulang. Server hidup? "
                      "enable-rcon=true? .rcon_pw ada?")
            return
        gagal = arti_spectator.result_failed(balasan)
        if gagal:
            if "No player" in str(gagal) and coba < 4:
                # Arti belum respawn saat perintah tiba — coba lagi sebentar
                # lagi, jangan biarkan kamera menggantung bebas.
                threading.Timer(3.0, lambda: _spectator_lock(
                    f"{reason}+ulang{coba + 1}", diam=diam, coba=coba + 1)).start()
                return
            if not _spectator_warned:
                _spectator_warned = True
                print(f"[Kamera] Server menolak: {gagal} — klien kamera "
                      "sudah join? Nama di minecraft_spectator_name benar?")
            return
        _spectator_warned = False   # sukses = boleh mengeluh lagi nanti
        if not diam:
            print(f"[Kamera] Terkunci ke Arti lagi ({reason})")
        # Kunci ulang me-reset perspektif ke first-person — tekan ulang F5
        # sesudah kliennya sempat memproses spectate (2.5 dtk). Perspektif
        # kembali ke third-BELAKANG, jadi state rotasi disinkronkan.
        global _kamera_depan
        _kamera_depan = False
        threading.Timer(2.5, _kamera_f5).start()

    threading.Thread(target=_kerja, daemon=True, name="spectator-lock").start()


_bag_check_last = 0.0


def _cek_tas_rutin() -> None:
    """Sesekali berhenti dan pamerkan isi tas (permintaan streamer 2026-08-09).

    Hanya saat AMAN (tanpa musuh terdeteksi, darah cukup) — berhenti 6 detik
    di depan skeleton itu bunuh diri. Sesudah tas ditutup dia disuruh jalan
    lagi (`follow` — setFollow sendiri yang memutuskan menemani atau roam).
    """
    global _bag_check_last
    jeda = float(CONFIG.get("minecraft_bag_check_sec", 300.0) or 0.0)
    if jeda <= 0 or not _mc_runner_active():
        return
    now = time.time()
    if now - _bag_check_last < jeda:
        return
    st = _minecraft_runner.last_status or {}
    # Ambang yang sama dengan takdir: musuh <12 = tunda, sekadar terdeteksi
    # di kejauhan bukan alasan menunda ritual.
    if any((m.get("distance") or 99) < 12
           for m in (st.get("nearby_hostiles") or []) if isinstance(m, dict)):
        return
    try:
        if float(st.get("health") or 0) < 10:
            return
    except (TypeError, ValueError):
        return
    _bag_check_last = now
    _minecraft_runner.send_command({"cmd": "stop"})
    _invsee_show("rutin")
    _queue_game_reaction(
        "[MINECRAFT] Kamu berhenti sebentar dan MEMBUKA TASMU di depan "
        "penonton — komentari singkat isi tasmu apa adanya (yang kosong ya "
        "bilang kosong), lalu lanjut jalan.")
    detik = float(CONFIG.get("minecraft_invsee_sec", 6.0))
    threading.Timer(detik + 1.5, lambda: (
        _mc_runner_active() and _minecraft_runner.send_command({"cmd": "follow"})
    )).start()


def _spectator_check() -> None:
    """Periksa kamera masih spectator; kunci ulang HANYA kalau lepas.

    Detak jantung sengaja tidak mengunci ulang membabi buta: tiap perintah
    `spectate` berpotensi menyentak kamera di depan penonton, dan itu alasan
    detaknya dulu dimatikan sama sekali. Membaca NBT tidak mengganggu apa pun,
    jadi jaringnya bisa dinyalakan tanpa harga itu.
    """
    _cek_tas_rutin()
    query = arti_spectator.spectator_gamemode_query(CONFIG)
    if not query:
        return

    def _kerja():
        try:
            from scripts.mc_rcon import rcon
            balasan = rcon([query])[0]
        except Exception:  # noqa: BLE001 — RCON mati bukan urusan detak
            return
        if arti_spectator.gamemode_lepas(balasan):
            _spectator_lock("kamera lepas dari spectator")

    threading.Thread(target=_kerja, daemon=True, name="spectator-check").start()


def _spectator_on_event(ev: dict) -> None:
    """Dipanggil untuk SETIAP event bot. Spectate lepas saat Arti mati."""
    global _spectator_last_ts, _spectator_dim
    if not arti_spectator.is_enabled(CONFIG):
        return
    if isinstance(ev, dict) and ev.get("ev") == "death":
        _spectator_lepas_saat_mati()      # no-op di mode orbit
    perlu, _spectator_dim = arti_spectator.should_resync(ev, _spectator_dim, CONFIG)
    now = time.time()
    if not perlu:
        # Jaring berkala untuk sebab yang tidak memancarkan event (default
        # MATI). Menumpang event `status` yang memang datang tiap ~10 dtk —
        # tidak perlu thread timer sendiri.
        detak = float(CONFIG.get("minecraft_spectator_heartbeat_sec", 0.0) or 0.0)
        if detak <= 0.0 or ev.get("ev") != "status":
            return
        if _spectator_last_ts > 0.0 and (now - _spectator_last_ts) < detak:
            return
        _spectator_last_ts = now
        _spectator_check()
        return
    if not arti_spectator.cooled(_spectator_last_ts, now):
        return          # respawn + status beruntun = satu perintah saja
    _spectator_last_ts = now
    _spectator_lock(str(ev.get("ev")))


def _queue_minecraft_chat_reply(teks: str) -> None:
    """streamer ngetik di chat Minecraft -> antre giliran bicara.

    trigger_type "mc_chat" dan BUKAN "game": tipe game sengaja dibuang saat
    Arti sibuk (reaksi basi tidak layak antre), sedangkan pertanyaan langsung
    dari streamer tidak boleh hilang. "mc_chat" juga dihitung sebagai giliran
    PEMILIK — pengirimnya sudah disaring `minecraft_streamer_name` di runner,
    jadi ini memang dia, dan dia harus bisa menyuruh keluar/ganti misi lewat
    chat sama seperti lewat mic.
    """
    print(f"[Minecraft] Bohan ngetik: {teks[:70]}")
    queue_voice_trigger(f"(Bohan ngetik di chat Minecraft) {teks}",
                        trigger_type="mc_chat")


def _cermin_ke_chat_game(teks: str) -> None:
    """Cerminkan balasan Arti ke chat Minecraft (16 Agu, mabar via e4mc).

    Teman-teman streamer TIDAK mendengar TTS-nya — tanpa cermin ini Arti bisu di
    mata semua orang di server. Mode (minecraft_chat_mirror):
      "tamu"  (default) = hanya saat minecraft_mode_tamu menyala — di server
               sendiri penonton stream sudah mendengar suaranya, chat game
               dobel malah berisik;
      "semua" = selalu; "mati" = tidak pernah.
    Dipecah per kalimat maks ~240 char (batas chat vanilla 256), maksimal 3
    pesan — jawaban Arti memang 2-3 kalimat.
    """
    try:
        mode = str(CONFIG.get("minecraft_chat_mirror", "tamu")).lower()
        if mode == "mati":
            return
        if mode == "tamu" and not CONFIG.get("minecraft_mode_tamu", False):
            return
        if not _mc_runner_active():
            return
        for potongan in arti_minecraft.bagi_chat_game(teks)[:3]:
            _minecraft_runner.send_command({"cmd": "say", "text": potongan})
    except Exception as e:  # noqa: BLE001 — cermin gagal tidak boleh mematikan giliran
        print(f"[Minecraft] cermin chat gagal: {type(e).__name__}: {e}")


def _queue_minecraft_chat_pemain(teks: str, nama: str) -> None:
    """Chat pemain LAIN di game (16 Agu, mabar) -> antre giliran bicara.

    Teman-teman streamer tidak mendengar TTS Arti — chat game adalah satu-satunya
    kanal mereka. Pengirim sudah disaring runner (bukan kamera, bukan bot
    layanan, bukan '!command'). NAMA disebut di trigger supaya Arti tahu siapa
    yang ngajak ngobrol dan bisa membalas dengan nama — persona-nya memang
    "panggil nama orangnya langsung". Tag lookalike sudah dilucuti runner.
    Balasannya dicerminkan ke chat game oleh _cermin_ke_chat_game (dia bisu
    di mata teman tanpa itu).
    """
    print(f"[Minecraft] {nama} ngetik: {teks[:70]}")
    # trigger_type SENGAJA BUKAN "mc_chat": tipe itu dihitung giliran PEMILIK
    # oleh is_owner_turn — teman yang ngetik "arti keluar dari game" akan
    # lolos gate lifecycle. Tipe sendiri = is_owner False = tag lifecycle/
    # goal/mode tertolak oleh gate yang sudah ada, ngobrolnya tetap jalan.
    # (Nyaris kejadian [date removed]; diselamatkan test_only_bohan_is_answered.)
    queue_voice_trigger(
        f"({nama} — pemain lain di server — ngetik di chat Minecraft) {teks}",
        trigger_type="mc_chat_pemain", viewer_name=nama,
    )


def _craft_panel_rcon(perintah: list[str]) -> list[str]:
    from scripts.mc_rcon import rcon
    return rcon(perintah)


def _craft_panel_follow(grid, hasil: str, size: int, stop: threading.Event) -> None:
    """Pasang panel, ikuti Arti, bersihkan. Seluruhnya di thread sendiri.

    RCON bisa menggantung, dan pemanggilnya adalah thread pembaca event bot:
    menahannya berarti menahan SELURUH aliran event game. Panel itu hiasan,
    bot itu isinya, jadi tidak boleh ada satu pun panggilan RCON di jalur
    pembaca (pelajaran yang sama dengan kamera penonton).
    """
    global _craft_panel_warned
    # Nama yang sama persis dengan yang dipakai kamera penonton: satu sumber
    # kebenaran, jadi panel tidak bisa mengikuti pemain yang salah.
    nama = arti_spectator.normalize_name(CONFIG.get("minecraft_bot_name")) or "Arti"

    def _pose():
        balasan = _craft_panel_rcon([
            f"data get entity {nama} Pos", f"data get entity {nama} Rotation"])
        return arti_craft_panel.parse_pose(balasan[0], balasan[1])

    try:
        _craft_panel_rcon(arti_craft_panel.build_show(_pose(), grid, hasil, size))
        _craft_panel_warned = False
    except Exception as e:  # noqa: BLE001
        if not _craft_panel_warned:
            _craft_panel_warned = True
            print(f"[Panel] Gagal memasang panel craft ({type(e).__name__}: {e}) "
                  "- server hidup? enable-rcon=true?")
        return
    # Jaring terakhir. `crafted`/`task_failed`/`death` yang menutup panel
    # datang DARI BOT; kalau prosesnya mati mendadak, tidak ada satu pun yang
    # datang dan panelnya menggantung di dunia selamanya.
    batas = time.time() + float(CONFIG.get("minecraft_craft_panel_max_sec", 90.0))
    try:
        # 0,3 dtk: teleport_duration 7 tick (0,35 dtk) menutupi celahnya, jadi
        # panelnya meluncur mengikuti dia, bukan melangkah patah-patah.
        while not stop.wait(0.3):
            if time.time() > batas:
                print("[Panel] Panel craft dibubarkan paksa — event penutupnya "
                      "tidak pernah datang (bot mati mendadak?)")
                break
            try:
                _craft_panel_rcon(arti_craft_panel.build_follow(_pose(), grid, size))
            except Exception:  # noqa: BLE001
                # Arti sesaat tidak ada (mati/respawn/bot restart). Jangan
                # bubarkan panelnya, dia keburu balik.
                continue
    finally:
        try:
            _craft_panel_rcon(arti_craft_panel.build_clear())
        except Exception:  # noqa: BLE001
            pass


def _craft_panel_on_event(ev: dict) -> None:
    """Panel craft melayang: hidup di `craft_start`, mati sesudah selesai."""
    global _craft_panel_stop, _craft_panel_timer
    kind = ev.get("ev")
    # Kematian menutup layar kamera DULUAN, sebelum pagar panel. Panel dan
    # momen "klik E" itu dua fitur berbeda: mematikan panel tidak boleh
    # diam-diam meninggalkan GUI invsee menempel di layar penonton.
    if kind == "death":
        _invsee_close("mati")
    if not CONFIG.get("minecraft_craft_panel_enabled", True):
        return
    if kind == "craft_start":
        grid, size = ev.get("grid"), int(ev.get("size") or 3)
        if size not in (2, 3) or not arti_craft_panel.grid_valid(grid, size):
            print(f"[Panel] Resep ditolak (bentuk aneh): {str(grid)[:80]}")
            return
        hasil = str(ev.get("result") or ev.get("item") or "")
        if not arti_craft_panel.grid_valid([[hasil]], size):
            return
        if _craft_panel_timer is not None:
            _craft_panel_timer.cancel()  # penutup lama tak boleh membunuh yang baru
            _craft_panel_timer = None
        if _craft_panel_stop is not None:
            _craft_panel_stop.set()      # craft beruntun: panel lama pergi dulu
        _craft_panel_stop = threading.Event()
        threading.Thread(
            target=_craft_panel_follow,
            args=(grid, hasil, size, _craft_panel_stop),
            daemon=True, name="craft-panel").start()
        return
    if kind not in ("crafted", "task_failed", "death"):
        return
    if kind == "task_failed" and ev.get("task") != "craft":
        return
    if _craft_panel_stop is None or _craft_panel_stop.is_set():
        return
    if _craft_panel_timer is not None:
        _craft_panel_timer.cancel()
    # Jangan langsung hilang: hasilnya baru muncul di panel detik itu juga.
    # Gagal/mati tidak diberi jeda, tidak ada yang perlu dipandangi.
    jeda = (float(CONFIG.get("minecraft_craft_panel_linger_sec", 5.0))
            if kind == "crafted" else 0.0)
    # Acuan stop SENGAJA tidak dibuang: selama thread-nya masih hidup,
    # `craft_start` berikutnya harus tetap bisa menghentikannya.
    _craft_panel_timer = threading.Timer(jeda, _craft_panel_stop.set)
    _craft_panel_timer.daemon = True   # jangan menahan bridge keluar
    _craft_panel_timer.start()


def _invsee_close(alasan: str = "") -> None:
    """Tutup GUI di layar kamera. Aman dipanggil kapan saja, termasuk ganda."""
    global _invsee_timer
    if _invsee_timer is not None:
        _invsee_timer.cancel()
        _invsee_timer = None
    perintah = arti_spectator.invsee_close_commands(CONFIG)
    if not perintah:
        return

    def _kerja():
        try:
            from scripts.mc_rcon import rcon
            rcon(perintah)
        except Exception:  # noqa: BLE001 — penutup gagal cuma bisa dicoba lagi
            pass

    threading.Thread(target=_kerja, daemon=True, name="invsee-close").start()


def _invsee_show(alasan: str) -> None:
    """Buka isi tas Arti di layar kamera beberapa detik, lalu tutup lagi.

    Penutupnya WAJIB. Diuji di server 2026-08-07: tidak ada perintah vanilla
    yang bisa menutup GUI pemain lain (kecuali pindah dimensi atau mati), jadi
    tanpa `tutuptas` layar penonton tertutup GUI itu selamanya.
    """
    global _invsee_timer, _invsee_warned
    perintah = arti_spectator.invsee_open_commands(CONFIG)
    if not perintah:
        return
    if _invsee_timer is not None:
        # Sudah ada yang tampil: perpanjang saja, jangan buka dua kali.
        _invsee_timer.cancel()
        _invsee_timer = None
    detik = arti_spectator.invsee_seconds(CONFIG)

    def _kerja():
        global _invsee_warned
        try:
            from scripts.mc_rcon import rcon
            balasan = rcon(perintah)
        except Exception as e:  # noqa: BLE001
            if not _invsee_warned:
                _invsee_warned = True
                print(f"[Kamera] Gagal buka tas di layar kamera "
                      f"({type(e).__name__}: {e})")
            return
        gagal = arti_spectator.result_failed(balasan)
        if gagal:
            if not _invsee_warned:
                _invsee_warned = True
                print(f"[Kamera] Server menolak buka tas: {gagal} — plugin "
                      "InvSee++/Sudo/TutupTas terpasang? Kamera sudah join?")
            return
        _invsee_warned = False
        print(f"[Kamera] Isi tas Arti tampil {detik:.0f} dtk ({alasan})")

    threading.Thread(target=_kerja, daemon=True, name="invsee-open").start()
    _invsee_timer = threading.Timer(detik, lambda: _invsee_close("waktu habis"))
    _invsee_timer.daemon = True
    _invsee_timer.start()
    # Biar dia menceritakannya, bukan diam-diam nongol di layar penonton.
    if _minecraft_runner is not None:
        try:
            _minecraft_runner.inject_event({"ev": "inventory_shown"})
        except Exception:  # noqa: BLE001
            pass


def _note_streamer_text_for_minecraft(text: str) -> None:
    """Perintah masuk/keluar Minecraft langsung dari kalimat streamer.

    Live 2026-08-05 malam: dia tiga kali menyuruh "arti, coba buka minecraft
    deh" dan Arti tidak pernah masuk. Bukan bug logika — giliran yang dipicu
    omongan streamer sengaja dirutekan ke Groq (butuh instan), dan model yang
    kepilih (llama-3.1-8b) MENGABAIKAN instruksi tag. Perintah eksplisit tidak
    boleh bergantung pada model mana yang kebetulan menang routing, jadi ini
    jaring deterministik — sejajar dengan jaring AFK.

    Aman kalau Arti juga mengeluarkan tag: start/stop sudah idempoten.
    """
    if not CONFIG.get("minecraft_enabled"):
        return
    niat = arti_session_mode.detect_minecraft_intent(text)
    # WAJIB DIPANGGIL NAMANYA (perbaikan [date removed]). SEMUA ucapan streamer lewat
    # sini, termasuk yang PASIF (PTT mati). Log [time removed]: operator bergumam "bisa
    # sambil main Minecraft kali ya biar aman" dan "berapa yang main game nya
    # harus..." — Arti tiga kali menyeret bot masuk game yang servernya bahkan
    # tidak menyala. Menyalakan bot itu aksi mahal (proses baru + ganti mode),
    # jadi ambangnya harus setinggi perintah langsung: sebut "arti".
    # Jaring AFK sengaja TIDAK diperlakukan begini — memasang jaring saat operator
    # pamit sambil lalu memang gunanya.
    if niat and not is_arti_wake_call(text):
        print(f"[Minecraft] Niat '{niat}' diabaikan — Bohan tidak memanggil "
              "namanya (ucapan pasif). Sebut 'arti' atau ketik 'mc on/off'.")
        return
    if niat == "join" and not _mc_runner_active():
        print("[Minecraft] Perintah Bohan terdeteksi — join")
        _start_minecraft_runner()
    elif niat == "leave" and _mc_runner_active():
        print("[Minecraft] Perintah Bohan terdeteksi — keluar")
        _stop_minecraft_runner_async()


def _note_curious_provider_fail(cfg: dict) -> None:
    """Provider tumbang total di turn curious -> inisiatif mundur dulu.

    Tanpa ini inisiatif nembak lagi tiap quiet_sec (30 dtk) ke provider yang
    masih 429/tutup — live seharian 2026-08-03: 80x skip beruntun."""
    global _init_provider_fail_until
    _init_provider_fail_until = time.time() + float(
        cfg.get("initiative_provider_fail_backoff_sec", 300.0)
    )

# Penonton yang terlihat di chat (nama -> ts terakhir). SEMUA pesan dihitung,
# bukan cuma yang men-trigger — beda dengan _last_yt_trigger_by_viewer yang
# hanya terisi di mode voice_queue (OFF di setup operator; ketahuan saat
# crosscheck v0.7: bahan inisiatif "sapa penonton hadir" selamanya kosong).
_yt_viewers_seen: dict[str, float] = {}

# Kapan Arti terakhir selesai bicara — gate hening inisiatif juga menghormati
# ini (bukan cuma aktivitas manusia): tanpa ini, 11 detik setelah selesai jawab
# dia monolog lagi (tes live [date removed]). 0.0 = stabil untuk snapshot konstanta.
_last_arti_reply_ts = 0.0

# Kapan streamer terakhir BERSUARA APAPUN di mic/ketikan (termasuk pasif) —
# pagar anti-motong inisiatif: Arti tidak boleh mulai monolog selagi operator
# lagi cerita. (Spek final operator [date removed]: 30 dtk sejak Arti bicara DAN
# 5 dtk sejak streamer bersuara.)
_last_streamer_speech_ts = 0.0


def _initiative_materials() -> dict:
    """Bahan topik inisiatif dari state bridge — semua best-effort."""
    mats: dict = {"memory_bullets": [], "present_viewers": [],
                  "scouter_summary": "", "screen_hook": ""}
    try:
        with open("vault/concepts/arti_live_learnings.md", encoding="utf-8") as f:
            mats["memory_bullets"] = [
                ln for ln in f.read().splitlines() if ln.strip().startswith("- [")
            ]
    except OSError:
        pass
    try:
        cutoff = time.time() - 3600.0
        mats["present_viewers"] = [
            v for v, ts in _yt_viewers_seen.items() if ts >= cutoff
        ]
    except Exception:  # noqa: BLE001
        pass
    s = CONFIG.get("scouter_last_result") or {}
    mats["scouter_summary"] = (s.get("summary") or "").strip()
    hook = (s.get("curious_hook") or "").strip()
    # Layar gelap/kosong bukan topik (live 3/8: background hitam sengaja +
    # mic mute, Arti malah bahas layar gelapnya berulang-ulang).
    mats["screen_hook"] = "" if arti_curious.is_boring_screen_hook(hook) else hook
    # Penonton baru masuk (telemetri jumlah penonton) — segar 120 dtk saja;
    # anti-ulang per event via ring (teks memuat angka -> unik per kenaikan).
    fresh_join = (
        _viewer_join_note
        and time.time() - _viewer_join_note_ts <= 120.0
    )
    mats["viewer_join_note"] = _viewer_join_note if fresh_join else ""
    # Lagi main Minecraft: status game = bahan komentar/aksi paling hidup.
    mats["minecraft_note"] = ""
    mats["minecraft_goal"] = ""
    try:
        if _mc_runner_active():
            # Aturan darah yang sama dengan blok konteks — jalur INI yang
            # paling sering jalan saat dia main (komentar proaktif tiap
            # ~20 dtk), jadi tanpa band-nya keluhan "membacakan bar HP"
            # kembali lewat pintu belakang. Hanya MEMBACA band; yang
            # memperbaruinya cuma _append_minecraft_context (sekali per
            # giliran) supaya dua pembaca tidak saling membungkam.
            mats["minecraft_note"] = arti_minecraft.status_note(
                _minecraft_runner.last_status, _mc_vitals_band
            )
            mats["minecraft_goal"] = _minecraft_goal
    except Exception:  # noqa: BLE001
        pass
    mats["mode"] = _session_mode()
    # Bahan tambahan KHUSUS mode siaran solo — mahal-ish, jadi hanya dirakit
    # kalau memang lagi dipakai.
    mats["vault_topic"] = ""
    mats["heard_note"] = ""
    mats["web_topic"] = ""
    if mats["mode"] == arti_session_mode.HOST_CHAT:
        global _host_vault_cache
        mats["vault_topic"] = _host_vault_topic()
        # Dipakai = dikosongkan, biar worker latar mengisi bahan BERIKUTNYA.
        # Tanpa ini potongan vault yang sama diangkat berulang kali.
        _host_vault_cache = ""
        mats["heard_note"] = _host_heard_note()
        mats["web_topic"] = _host_web_topic_cache
    return mats


# Bahan mode siaran solo ---------------------------------------------------
# Seed pertanyaan ke vault, dirotasi bergilir supaya tidak menarik potongan
# yang itu-itu saja (fast path curious melewati RAG, jadi selama ini isi vault
# tidak pernah jadi bahan proaktif sama sekali).
_HOST_VAULT_SEEDS = (
    "momen lucu waktu live",
    "hal yang Bohan pernah ceritakan",
    "kebiasaan penonton di stream ini",
    "rencana atau cita-cita yang pernah dibahas",
    "kesalahan atau kejadian konyol",
    "hal yang Arti pelajari belakangan ini",
)
_host_vault_seed_idx = 0
_host_web_topic_cache = ""
_host_vault_cache = ""


def _host_vault_topic() -> str:
    """Bahan vault dari CACHE — jangan pernah mencari di sini.

    Audit 2026-08-05: pencarian vault dipanggil sinkron dari
    `_initiative_materials()` yang jalan DI MAIN LOOP, dan tiap seed baru
    membekukan loop ~2,1 detik (embedding lewat LM Studio) — konsumsi antrean,
    TTS, VTS, dan subtitle ikut berhenti selama itu. Sekarang pola yang sama
    dengan berita: thread latar yang mengisi cache, main loop cuma membaca.
    """
    return _host_vault_cache


def _host_vault_refresh() -> str:
    """Ambil satu potongan isi vault (BLOKIR — hanya dipanggil dari thread)."""
    global _host_vault_seed_idx
    try:
        seed = _HOST_VAULT_SEEDS[_host_vault_seed_idx % len(_HOST_VAULT_SEEDS)]
        _host_vault_seed_idx += 1
        hits = arti_vault_rag.search(seed, CONFIG, top_k=3) or []
        for h in hits:
            # Kunci hasil search adalah "content", BUKAN "text" — salah kunci
            # bikin bahan andalan mode host mati total tanpa error sejak
            # acba120 (audit [date removed]).
            text = (
                (h.get("content") or h.get("text") or "").strip()
                if isinstance(h, dict) else str(h)
            )
            text = " ".join(text.split())[:220]
            if len(text) >= 40:
                return text
    except Exception as e:  # noqa: BLE001 — bahan opsional, jangan ganggu siaran
        print(f"[Host] Bahan vault gagal ({type(e).__name__}: {e})")
    return ""


def _host_heard_note() -> str:
    """Baris terakhir yang telinga dengar — bahan "lagi muter apa nih"."""
    if not CONFIG.get("desktop_audio_enabled"):
        return ""
    try:
        fresh = arti_desktop_audio.format_context_fresh(max_lines=2, ttl_sec=180.0)
        return " ".join((fresh or "").split())[:200]
    except Exception:  # noqa: BLE001
        return ""


def host_web_topic_worker() -> None:
    """Prefetch kabar internet di BACKGROUND, bukan di dalam turn.

    `lookup_block` makan 7-18 dtk sedangkan jeda komentar cuma ~25 dtk —
    memanggilnya di dalam giliran bicara akan bikin Arti telat terus. Jadi
    cache disegarkan berkala dan inisiatif tinggal baca.
    """
    global _host_web_topic_cache
    import arti_web_lookup  # noqa: PLC0415

    period = float(CONFIG.get("host_web_topic_refresh_sec", 900.0))
    query = str(CONFIG.get("host_web_topic_query") or "berita hari ini")
    while True:
        try:
            block = arti_web_lookup.lookup_block(query, CONFIG) or ""
            text = " ".join(block.split())
            # Buang label blok "[INFO INTERNET — ...]" — yang dipakai isinya.
            if "]" in text:
                text = text.split("]", 1)[1].strip()
            _host_web_topic_cache = text[:300]
            if _host_web_topic_cache:
                print(f"[Host] Bahan berita disegarkan ({len(_host_web_topic_cache)} char)")
        except Exception as e:  # noqa: BLE001
            print(f"[Host] Prefetch berita gagal ({type(e).__name__}: {e})")
        time.sleep(max(60.0, period))


def host_vault_topic_worker() -> None:
    """Isi cache bahan vault di LATAR — pencariannya memblokir ~2 dtk.

    Hanya bekerja saat mode siaran solo, dan hanya kalau cache-nya sudah
    terpakai (dikosongkan _initiative_materials) — tidak ada gunanya memanggil
    embedding terus-menerus kalau bahannya belum dipakai.
    """
    global _host_vault_cache
    while True:
        try:
            if (
                not _host_vault_cache
                and _host_mode
                and not _mc_runner_active()
            ):
                _host_vault_cache = _host_vault_refresh()
        except Exception as e:  # noqa: BLE001
            print(f"[Host] Worker bahan vault gagal: {type(e).__name__}: {e}")
        time.sleep(10.0)


def start_host_topic_workers() -> None:
    threading.Thread(
        target=host_vault_topic_worker, daemon=True, name="host-vault-topic"
    ).start()
    if not CONFIG.get("host_web_topic_enabled", False):
        return
    threading.Thread(
        target=host_web_topic_worker, daemon=True, name="host-web-topic"
    ).start()


def add_to_history(source, message, arti_meta=None):
    # Benang obrolan ([date removed]): giliran curious perlu tahu Arti barusan bilang
    # apa & sudah menagih apa — tanpa ini dia fiksasi (5 balasan beruntun
    # mengungkit hal yang sama, tes [date removed] [time removed]). catat() dijamin tak melempar.
    arti_benang.catat(str(source), str(message))
    # Renungan ([date removed]): jawaban Arti memajukan busur mikirnya; chat yang
    # nyambung topik jadi bahan langkah depan. Busur yang tutup meninggalkan
    # kesimpulan — ditulis ke vault lewat corong lama (gerbang fakta_sudah_ada
    # ikut menjaga duplikat). catat() dijamin tak melempar.
    arti_renungan.catat(str(source), str(message), config=CONFIG)
    _kesimpulan_renungan = arti_renungan.pop_kesimpulan()
    if _kesimpulan_renungan:
        try:
            save_long_term_memory(_kesimpulan_renungan)
            print(f"[Renungan] Busur tutup — kesimpulan disimpan: "
                  f"{_kesimpulan_renungan[:80]}...")
        except Exception as e:
            print(f"[Renungan] Gagal simpan kesimpulan: {type(e).__name__}: {e}")
    """Menambahkan aktivitas ke dalam buku catatan sejarah stream secara aman"""
    global _last_yt_chat_ts, _last_yt_chat_gap_sec, _last_human_activity_ts
    global _last_arti_reply_ts, _last_streamer_speech_ts
    if not message or not message.strip():
        return
    if source.startswith("Viewer ") and "(YouTube)" in source:
        # Teks penonton kini BENAR-BENAR sampai ke prompt (regex histori baru
        # saja diperbaiki), jadi titipan tag harus dilucuti di sini juga —
        # kalau tidak, siapa pun bisa menulis "[MODE: host] [MC: leave]" di
        # chat dan berharap Arti menirukannya di giliran proaktif berikutnya,
        # di mana gate pemilik justru mengizinkan.
        message = arti_minecraft.strip_tag_lookalikes(message)
        now = time.monotonic()
        if _last_yt_chat_ts:
            _last_yt_chat_gap_sec = now - _last_yt_chat_ts
        _last_yt_chat_ts = now
        _last_human_activity_ts = time.time()
        viewer_name = source[len("Viewer "):].replace("(YouTube)", "").strip()
        if viewer_name and not is_bot_viewer(viewer_name, CONFIG):
            _yt_viewers_seen[viewer_name] = time.time()
    elif source == "Streamer":
        _last_human_activity_ts = time.time()
        _last_streamer_speech_ts = time.time()
        # SEMUA ucapan streamer lewat sini — termasuk yang pasif (PTT mati,
        # wake word meleset, chat in-game). Audit [date removed]: jaring AFK &
        # pengembalian mic dulu cuma dipasang di queue_voice_trigger, jadi
        # operator yang pamit tanpa menekan PTT tidak pernah memasang jaring, dan
        # operator yang balik ngobrol tanpa PTT tetap dianggap AFK — Arti terus
        # jadi host sambil disuruh "jangan mengarang seolah dia menjawab".
        try:
            if _host_mode:
                _set_host_mode(False, "streamer_kembali", announce=False)
            _note_streamer_text_for_afk(message)
            _note_streamer_text_for_minecraft(message)
        except Exception as e:  # noqa: BLE001 — histori tidak boleh jatuh
            print(f"[Host] Gagal memproses ucapan streamer: {type(e).__name__}: {e}")
    elif source.startswith("Arti"):
        _last_arti_reply_ts = time.time()
    timestamp = time.strftime("%H:%M:%S")
    log_line = f"[{timestamp}] [{source}] {message}"
    with history_lock:
        stream_history.append(log_line)
    print(f"📝 [History Recorded] {log_line}")
    try:
        if arti_meta is not None:
            session_transcript.log_arti_reply(message, CONFIG, **arti_meta)
        else:
            session_transcript.append_from_history(source, message, CONFIG)
    except Exception as e:
        print(f"[Transcript] Gagal menulis baris: {e}")

# ==========================================
# DYNAMIC LEARNING & HERMES VAULT INTEGRATION (LOCK-AWARE HARNESS)
# ==========================================
# Paths Setup untuk Locking Protocol
LOCK_DIR = os.path.join(os.path.expanduser("~"), ".hermes-locks")
LOCK_FILE = os.path.join(LOCK_DIR, "db.lock")

def wait_and_acquire_lock(holder_name="arti-vtuber-bridge", timeout_sec=10):
    """Menunggu sampai lock file terbebas, lalu mengunci vault untuk transaksi aman"""
    os.makedirs(LOCK_DIR, exist_ok=True)
    start_time = time.time()
    
    while time.time() - start_time < timeout_sec:
        if not os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, "w", encoding="utf-8") as f:
                    f.write(holder_name)
                return True
            except Exception as e:
                print(f"[Vault Lock Error] Gagal membuat file kunci: {e}")
                return False
        # Tunggu 0.5 detik sebelum mencoba lagi
        time.sleep(0.5)
        
    print(f"[Vault Lock Warning] Timeout menunggu lock file dilepas oleh proses lain. Memaksa transaksi untuk kelancaran live stream.")
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(holder_name)
        return True
    except:
        return False

def release_vault_lock():
    """Melepas kunci vault agar proses bridge lain bisa menulis kembali"""
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except Exception as e:
            print(f"[Vault Lock Error] Gagal menghapus file kunci: {e}")

def load_long_term_memories():
    profile = CONFIG.get("active_profile", "default").lower()
    suffix = "" if profile == "default" else f"_{profile}"
    vault_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault", "concepts", f"arti_live_learnings{suffix}.md")
    
    # Fallback jika folder vault tidak ada atau belum terbuat
    if not os.path.exists(os.path.dirname(vault_path)):
        os.makedirs(os.path.dirname(vault_path), exist_ok=True)
        
    if not os.path.exists(vault_path):
        if wait_and_acquire_lock("arti-vtuber-init"):
            try:
                with open(vault_path, "w", encoding="utf-8") as f:
                    f.write(f"# Arti Live Learnings ({profile.capitalize()} Profile)\n\n"
                            f"Ini adalah catatan pengetahuan jangka panjang yang dipelajari Arti (VTuber Co-Host) secara otomatis selama sesi live stream untuk profil **{profile}**.\n\n"
                            f"## Memori Jangka Panjang\n\n"
                            f"- [YYYY-MM-DD] Co-host aktif membantu streamer (Profil: {profile}).\n")
            except Exception as e:
                print(f"[Memory Error] Gagal inisialisasi file memori: {e}")
            finally:
                release_vault_lock()
            
    memories = []
    try:
        with open(vault_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        in_memory_section = False
        for line in lines:
            if line.strip().startswith("## Memori Jangka Panjang"):
                in_memory_section = True
                continue
            elif line.strip().startswith("##"):
                in_memory_section = False
            if in_memory_section and line.strip().startswith("-"):
                memories.append(line.strip())
    except Exception as e:
        print(f"[Memory Error] Gagal membaca memori jangka panjang untuk profil '{profile}': {e}")
    return memories

def save_long_term_memory(fact):
    import arti_memory_quality

    profile = CONFIG.get("active_profile", "default").lower()
    suffix = "" if profile == "default" else f"_{profile}"
    vault_path = Path(os.path.dirname(os.path.abspath(__file__))) / "vault" / "concepts" / f"arti_live_learnings{suffix}.md"

    if wait_and_acquire_lock("arti-vtuber-memory"):
        try:
            arti_memory_quality.append_learning(vault_path, fact.strip(), config=CONFIG)
        except Exception as e:
            print(f"[Memory Error] Gagal menyimpan memori jangka panjang untuk profil '{profile}': {e}")
        finally:
            release_vault_lock()

def save_stream_session_log():
    """Vault slim + observer pipeline + RAG reindex (v0.6)."""
    try:
        import arti_api_telemetry as tel
        import arti_observer_shutdown as obs_shutdown
        import arti_observer_progress as obs_progress

        sid = session_transcript.get_session_id(CONFIG) or ""
        if sid:
            tel.set_session_id(sid)
            tel.flush(CONFIG)

        if CONFIG.get("observer_enabled", True) and CONFIG.get("observer_shutdown_blocking", True):
            obs_shutdown.run_observer_shutdown(
                CONFIG,
                on_progress=obs_progress.make_progress_callback("Observer"),
            )
    except Exception as e:
        print(f"[Observer] shutdown pipeline gagal: {e}")

    try:
        session_transcript.finalize_session_artifacts(CONFIG, _DEBUG_LOG_PATH)
    except Exception as e:
        print(f"[Vault] finalize_session_artifacts gagal: {e}")

    try:
        import arti_api_telemetry as tel

        tel.flush(CONFIG)
    except Exception:
        pass

    try:
        import arti_telemetry_dashboard as dash

        out = dash.generate_dashboard(CONFIG)
        print(f"[Telemetry] Dashboard -> {out}")
    except Exception:
        pass

    if not CONFIG.get("vault_rag_reindex_on_shutdown", True):
        return

    # 0 = TUNGGU SAMPAI TUNTAS (default; permintaan operator [date removed]: "pastiin
    # semua ngeringkas, RAG, apapun selesai — biar aku ga keburu tutup terminal").
    # >0 = batas detik lama (perilaku lama), sisa disembuhkan catch-up startup.
    timeout = int(CONFIG.get("vault_rag_reindex_shutdown_timeout_sec", 0))

    def _reindex_worker():
        arti_vault_rag.reindex_shutdown(CONFIG)

    print("[Vault RAG] Reindex akhir sesi — JANGAN tutup terminal dulu...")
    t = threading.Thread(target=_reindex_worker, name="vault-rag-reindex", daemon=True)
    t.start()
    if timeout > 0:
        t.join(timeout=timeout)
    else:
        while t.is_alive():
            t.join(timeout=30)
            if t.is_alive():
                print("[Vault RAG] ...masih embedding, tahan dulu terminalnya")
    if t.is_alive():
        # JUJUR: thread daemon mati begitu proses exit. Sisa pekerjaannya
        # disembuhkan catch-up di start berikutnya.
        print(
            "[Vault RAG] Reindex BELUM selesai — kalau terminal ditutup sekarang, "
            "sisanya otomatis dilanjutkan saat bridge start berikutnya (catch-up)."
        )
    else:
        print("=" * 60)
        print("  SEMUA PROSES SHUTDOWN SELESAI — aman menutup terminal.")
        print("=" * 60)

# ==========================================
# 1. KONEKSI & KONTROL VTUBE STUDIO API
# ==========================================
vts = None  # Global VTS instance — dipakai bridge, idle animation, mouse follow

class VTSController:
    def __init__(self):
        self.websocket = None
        self.auth_token = None
        self.token_file = "vts_token.txt"
        self._ws_send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task = None
        self._reader_stop = False
        # Pelajaran live 11,5 jam [date removed]: koneksi utama putus ~1 jam masuk,
        # semua kirim ekspresi ditelan tanpa log, model nyangkut di 'mikir' 10 jam.
        # Idle selamat karena punya reconnect sendiri — sekarang jalur utama juga.
        self._conn_lost = False
        self._last_reconnect_attempt = 0.0

        if os.path.exists(self.token_file):
            with open(self.token_file, "r") as f:
                self.auth_token = f.read().strip()

    async def _reader_loop(self):
        """Route VTS responses by requestID — nod inject tidak lagi merusak recv ekspresi."""
        ws = self.websocket
        while ws and not self._reader_stop:
            try:
                raw = await ws.recv()
                data = json.loads(raw)
                rid = data.get("requestID")
                if rid and rid in self._pending:
                    fut = self._pending.pop(rid, None)
                    if fut and not fut.done():
                        fut.set_result(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._reader_stop:
                    self._conn_lost = True
                    print(
                        f"[VTS] Koneksi utama putus (reader: {type(e).__name__}) — "
                        "auto-reconnect di transisi ekspresi berikutnya"
                    )
                break

    async def connect(self):
        uri = f"ws://localhost:{CONFIG['vts_api_port']}"
        try:
            self.websocket = await websockets.connect(uri)
            print(f"[VTS] Terhubung ke VTube Studio API di port {CONFIG['vts_api_port']}")
            self._reader_stop = False
            self._reader_task = asyncio.create_task(self._reader_loop())
            await self.authenticate()
            self._conn_lost = False
        except Exception as e:
            print(f"[VTS Error] Gagal connect ke VTS. Pastikan 'Start API' di VTS Settings aktif! Error: {e}")

    async def ensure_connected(self) -> bool:
        """Auto-reconnect koneksi utama; dipanggil tiap transisi ekspresi.

        Throttle 15 detik: kalau VTS benar-benar mati, jangan banjiri percobaan —
        cukup sekali per jendela, sisanya SKIP jujur di log. Sesudah reconnect
        sukses, state machine ekspresi memulihkan diri sendiri (tiap transisi
        mematikan overlay lain, jadi overlay yang nyangkut ikut bersih).
        """
        if self.websocket is not None and not self._conn_lost:
            return True
        now = time.monotonic()
        if now - self._last_reconnect_attempt < 15.0:
            return False
        self._last_reconnect_attempt = now
        print("[VTS] Koneksi utama putus — mencoba reconnect...")
        try:
            await self.close()
        except Exception:
            pass
        await self.connect()
        ok = self.websocket is not None and not self._conn_lost
        print(
            "[VTS] Reconnect utama SUKSES — ekspresi hidup lagi"
            if ok
            else "[VTS] Reconnect utama gagal — dicoba lagi >=15 dtk"
        )
        return ok

    async def send_request(self, message_type, data=None, *, timeout=3.0):
        if not self.websocket:
            raise RuntimeError("VTS not connected")
        rid = f"Hermes_{time.time_ns()}"
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": rid,
            "messageType": message_type,
            "data": data or {}
        }
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[rid] = fut
        async with self._ws_send_lock:
            await self.websocket.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def authenticate(self):
        if not self.auth_token:
            print("[VTS] Meminta izin akses plugin baru... Silakan klik 'ALLOW' di layar VTube Studio!")
            data = {
                "pluginName": CONFIG["vts_plugin_name"],
                "pluginDeveloper": CONFIG["vts_developer"]
            }
            res = await self.send_request("AuthenticationTokenRequest", data)
            self.auth_token = res["data"]["authenticationToken"]
            with open(self.token_file, "w") as f:
                f.write(self.auth_token)
            print("[VTS] Token plugin berhasil disimpan.")

        auth_data = {
            "pluginName": CONFIG["vts_plugin_name"],
            "pluginDeveloper": CONFIG["vts_developer"],
            "authenticationToken": self.auth_token
        }
        res = await self.send_request("AuthenticationRequest", auth_data)
        if res["data"]["authenticated"]:
            print("[VTS] Autentikasi Plugin SUKSES!")
        else:
            print("[VTS] Autentikasi GAGAL! Menghapus token usang...")
            os.remove(self.token_file)
            self.auth_token = None
            await self.authenticate()

    async def close(self):
        self._reader_stop = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except Exception:
                pass
            self._reader_task = None
        if self.websocket:
            await self.websocket.close()

    async def create_custom_parameter(self, name, min_val=-1, max_val=1, default_val=0):
        """Bikin custom tracking parameter di VTS."""
        if not self.websocket:
            return
        try:
            res = await self.send_request("ParameterCreationRequest", {
                "parameterName": name,
                "explanation": f"Hermes Bridge: {name}",
                "min": min_val,
                "max": max_val,
                "defaultValue": default_val
            })
            print(f"[VTS] Custom param '{name}' registered (min={min_val}, max={max_val})")
        except Exception as e:
            print(f"[VTS] Custom param '{name}' error: {e}")

    async def inject_parameter_data(self, parameters: list):
        """Inject parameter values ke VTS model (fire-and-forget).
        Args:
            parameters: [{"id": "FaceAngleY", "value": 6.7}, ...]
        """
        if not self.websocket:
            return
        param_values = []
        for p in parameters:
            entry = {"id": p["id"], "weight": 1.0, "value": float(p["value"])}
            param_values.append(entry)
            if p["id"] == "FaceAngleY":
                try:
                    _idle_face_y_queue.put_nowait(float(p["value"]))
                except Exception:
                    pass
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": f"Inject_{time.time_ns()}",
            "messageType": "InjectParameterDataRequest",
            "data": {
                "faceFound": False,
                "mode": "set",
                "parameterValues": param_values,
            },
        }
        try:
            async with self._ws_send_lock:
                await self.websocket.send(json.dumps(payload))
        except Exception:
            # avoid crashing bridge on eye-tracking errors — tapi tandai putus
            # supaya ensure_connected memulihkan (nod ikut mati saat stuck-mikir).
            self._conn_lost = True

    async def send_expression(self, expr_file, active, *, confirm=False,
                              fade: float = 0.0):
        """Toggle ekspresi VTS; confirm=True tunggu ACK (mikir/bicara/lampu).

        `fade` (detik) memakai `fadeTime` milik VTS supaya ekspresi MELELEH,
        bukan dipotong. Ditambahkan 27 Agu: linger emosi menahan mood lalu
        mematikannya seketika, dan streamer melihatnya sebagai "ga pelan pelan
        ilang" — memang, karena yang dibuat cuma penundaan, bukan peredupan.
        VTS mendokumentasikan rentang 0-2 detik, jadi dijepit di 2.
        """
        if not self.websocket:
            return
        rid = f"Expr_{time.time_ns()}"
        data = {"expressionFile": expr_file, "active": active}
        if fade and fade > 0:
            data["fadeTime"] = round(min(float(fade), 2.0), 2)
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": rid,
            "messageType": "ExpressionActivationRequest",
            "data": data,
        }
        fut = None
        if confirm:
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
        try:
            try:
                async with self._ws_send_lock:
                    await self.websocket.send(json.dumps(payload))
            except Exception as e:
                # Kirim gagal = koneksi bermasalah. Dulu ditelan tanpa jejak —
                # ekspresi mati 10 jam tanpa satu baris log (stuck 'mikir').
                # Log hanya di transisi sehat→putus supaya tidak spam per kirim.
                if not self._conn_lost:
                    self._conn_lost = True
                    print(
                        f"[VTS] Kirim ekspresi gagal ({type(e).__name__}) — "
                        "koneksi utama ditandai putus, auto-reconnect menyusul"
                    )
                return
            if fut:
                try:
                    await asyncio.wait_for(fut, timeout=0.6)
                except Exception:
                    pass  # ACK lambat != koneksi putus; reader/kirim yang memutuskan
        finally:
            if fut:
                self._pending.pop(rid, None)

    _EXPR_MIKIR = "ArtiMikir.exp3.json"
    _EXPR_BICARA = "ArtiBicara.exp3.json"
    _EXPR_AWARE = "ArtiAware.exp3.json"
    _EXPR_DEFAULT = "ArtiDefault1.exp3.json"

    async def _activate_expression(self, on_file: str, *off_files: str) -> None:
        """Nyalakan exp baru DULU, baru matikan yang lama — hindari frame kosong (blip)."""
        await self.send_expression(on_file, True, confirm=True)
        for off in off_files:
            if off and off != on_file:
                await self.send_expression(off, False, confirm=False)

    async def trigger_expression_state(self, state):
        """Transisi exp overlap: ON baru → OFF lama. Tanpa pulse/inject (exp file sudah lock scribble)."""
        if not self.websocket:
            return
        if not await self.ensure_connected():
            # Jujur di log: dulu tetap mencetak "[Expr] → ..." padahal tidak ada
            # yang sampai ke VTS ("ketrigger" palsu selama 10 jam).
            print(f"[Expr] SKIP {state} — koneksi VTS utama putus")
            return
        m, b, a, d = self._EXPR_MIKIR, self._EXPR_BICARA, self._EXPR_AWARE, self._EXPR_DEFAULT
        if state == "mikir":
            await self._activate_expression(m, a, b)
        elif state == "bicara":
            await self._activate_expression(b, m)
        elif state == "aware":
            await self._activate_expression(a, m, b, d)
        else:  # default
            await self._activate_expression(d, m, b, a)
        print(f"[Expr] → {state}")

# ==========================================
# 2. AUDIO PROCESSING & TTS
# ==========================================
def resample_audio(data, orig_sr, target_sr=44100):
    if orig_sr == target_sr:
        return data, target_sr
    duration = len(data) / orig_sr
    target_length = int(duration * target_sr)
    orig_xs = np.linspace(0, duration, len(data))
    target_xs = np.linspace(0, duration, target_length)
    if len(data.shape) > 1:
        resampled_channels = []
        for i in range(data.shape[1]):
            resampled_channels.append(np.interp(target_xs, orig_xs, data[:, i]))
        return np.column_stack(resampled_channels), target_sr
    else:
        return np.interp(target_xs, orig_xs, data), target_sr

# ------------------------------------------------------------------
# OBS Subtitle helpers (WordBoundary tick parsing)
# ------------------------------------------------------------------
# edge_tts emits offset / duration in HNS Ticks (100-nanosecond units).
# The Word Timings List contract consumed by subtitle.html expects seconds
# as Python floats, so every tick value is divided by HNS_PER_SECOND.
HNS_PER_SECOND = 10_000_000


def _parse_word_boundary(chunk: dict) -> dict | None:
    """Convert a raw edge_tts WordBoundary chunk into a Word Timings entry.

    Returns None (and logs a [SubTitle] diagnostic) on any malformed field —
    missing offset/duration/text, non-numeric or negative offset/duration —
    so the caller's stream loop can skip the chunk and keep iterating.
    Never raises on malformed input.

    `text` is passed through byte-for-byte: no .strip(), no .lower(), no
    unicodedata.normalize. (Requirements 1.4, 1.5, 1.7, 5.10.)
    """
    # Required field presence check.
    try:
        offset = chunk["offset"]
        duration = chunk["duration"]
        text = chunk["text"]
    except (KeyError, TypeError) as e:
        print(f"[SubTitle] Skipping WordBoundary missing field: {e}")
        return None

    # Numeric type check. bool is a subclass of int in Python, so it would
    # pass isinstance(x, int); we explicitly reject it because True/False are
    # not meaningful tick counts.
    def _is_numeric(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if not _is_numeric(offset) or not _is_numeric(duration):
        print(f"[SubTitle] Skipping WordBoundary with non-numeric ticks: {chunk!r}")
        return None

    # Negative-tick guard. (NaN comparisons are always False, so a NaN slips
    # past this check; the upstream generator-test bound (allow_nan=False)
    # keeps NaN out of the input space, matching the design contract.)
    if offset < 0 or duration < 0:
        print(f"[SubTitle] Skipping WordBoundary with negative ticks: {chunk!r}")
        return None

    return {
        "word": text,                                  # byte-for-byte passthrough
        "start": float(offset) / HNS_PER_SECOND,
        "duration": float(duration) / HNS_PER_SECOND,
    }


# ==========================================
# SUPERTONE SUBPROCESS LIFECYCLE MANAGER
# ==========================================
# Bridge-side (Python [time removed]) owner of the single long-lived `supertone_engine.py`
# subprocess (Python [time removed]). Speaks NDJSON over the subprocess's inherited
# stdin/stdout pipes. All blocking subprocess I/O is dispatched to a worker
# thread via `asyncio.to_thread(...)` so the asyncio event loop never blocks; an
# `asyncio.Lock` serializes requests so at most one synthesize is in flight.
#
# This task (4.1) implements spawn + readiness handshake only. `request()`
# (task 4.2) and the restart/`shutdown()` policy (task 4.3) are stubbed below
# with clearly marked insertion points.


class SupertoneError(Exception):
    """Raised on any Supertone subprocess protocol/lifecycle failure.

    Carries a structured error dict (``{"code": ..., "message": ...}``) so the
    caller in ``TTSEngine.speak()`` can log the failure cause and fall back to
    edge_tts. The error code is exposed via the ``code`` attribute for
    convenience while the full payload remains available via ``error``.
    """

    def __init__(self, error):
        # Accept either a structured dict or a bare string for ergonomics.
        if isinstance(error, dict):
            self.error = error
        else:
            self.error = {"code": "SUPERTONE_ERROR", "message": str(error)}
        self.code = self.error.get("code", "SUPERTONE_ERROR")
        super().__init__(self.error.get("message", self.code))


def _resolve_venv312_python() -> str:
    """Return the absolute path to the Python 3.12 (`venv312`) interpreter.

    The Supertone subprocess must run under the 3.12 venv that has the
    `supertonic` library installed. On Windows the interpreter lives at
    ``venv312/Scripts/python.exe``; on POSIX it lives at ``venv312/bin/python``.
    The venv is resolved relative to this module's directory so the bridge can
    be launched from any working directory.

    Raises:
        FileNotFoundError: if the expected interpreter does not exist. This
            surfaces through ``ensure_alive()`` so ``speak()`` falls back to
            edge_tts (Fallback table, row 1).
    """
    base = os.path.dirname(os.path.abspath(__file__))
    if os.name == "nt":
        candidate = os.path.join(base, "venv312", "Scripts", "python.exe")
    else:
        candidate = os.path.join(base, "venv312", "bin", "python")
    if not os.path.isfile(candidate):
        raise FileNotFoundError(
            f"venv312 Python interpreter not found at: {candidate}"
        )
    return candidate


class SupertoneProcess:
    """Owns the single long-lived Supertone synthesis subprocess.

    Lifecycle: lazy spawn + readiness handshake (`ensure_alive`), serialized
    request/response (`request`, task 4.2), and graceful shutdown (`shutdown`,
    task 4.3). Lives as an instance attribute of ``TTSEngine`` (``self.supertone``).
    """

    def __init__(self):
        self.proc: "subprocess.Popen | None" = None
        self.lock = asyncio.Lock()
        self._next_id = itertools.count(1)
        self._ready = False

    async def ensure_alive(self) -> None:
        """Guarantee a live, ready subprocess, spawning one if needed.

        Under the lock: if the current subprocess is both live (``poll()`` is
        ``None``) and ready, return immediately. Otherwise (no process, dead
        process, or not-ready) spawn a fresh one via ``_spawn_locked()``.

        Per the restart policy, at most one spawn attempt happens per call, so a
        repeatedly failing subprocess keeps degrading to edge_tts rather than
        looping (Requirements 9.1, 9.8).
        """
        async with self.lock:
            if self.proc is not None and self.proc.poll() is None and self._ready:
                return
            await self._spawn_locked()

    async def _spawn_locked(self) -> None:
        """Spawn the subprocess and perform the readiness handshake.

        Caller MUST hold ``self.lock``. Launches ``[py, "supertone_engine.py"]``
        as an argv list (never ``shell=True``, so there is no shell-injection
        surface — Requirements 9.2, 18.1/18.3). Reads exactly one ready-banner
        line off the event loop (``asyncio.to_thread``) under ``READY_TIMEOUT_S``
        (allows first-run model download), parses it, and marks the subprocess
        ready only when ``type == "ready"`` and ``ok`` is true; otherwise raises
        ``SupertoneError`` so ``speak()`` falls back (Requirements 4.1-4.6, 9.7).
        """
        py = _resolve_venv312_python()        # raises FileNotFoundError if missing
        engine_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "supertone_engine.py"
        )
        # Flag CUDA diteruskan lewat env, bukan argv: supertone_engine harus menyiapkan
        # PATH DLL-nya SEBELUM onnxruntime di-import, jadi keputusannya harus sudah
        # diketahui sejak proses lahir. Lihat supertone_engine.enable_cuda_if_requested().
        _env = os.environ.copy()
        _env["ARTI_SUPERTONE_CUDA"] = "1" if CONFIG.get("supertonic_use_cuda", False) else "0"
        # argv list, NEVER shell=True → no shell injection (Req 9.2, 18.1/18.3).
        self.proc = subprocess.Popen(
            [py, engine_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,                        # line-buffered
            env=_env,
        )
        self._ready = False
        # Read exactly one ready banner line; bound by READY_TIMEOUT_S so a model
        # that never finishes loading triggers fallback (Req 4.5).
        ready = await asyncio.wait_for(
            asyncio.to_thread(self._read_line_blocking), timeout=READY_TIMEOUT_S
        )
        banner = json.loads(ready)
        if banner.get("type") != "ready" or not banner.get("ok"):
            raise SupertoneError(
                banner.get("error", {"code": "MODEL_LOAD_FAILED"})
            )
        self._ready = True

    def _read_line_blocking(self) -> str:
        """Blocking read of a single line from the subprocess stdout.

        Runs on a worker thread via ``asyncio.to_thread``. An empty-string read
        means the subprocess closed stdout (EOF / died), which is surfaced as a
        ``SupertoneError`` with code ``EOF`` (Requirements 2.6, 9 EOF handling).
        """
        line = self.proc.stdout.readline()
        if line == "":                        # EOF → subprocess died
            raise SupertoneError(
                {"code": "EOF", "message": "subprocess closed stdout"}
            )
        return line

    def _write_line_blocking(self, line: str) -> None:
        """Blocking write of a single line to the subprocess stdin (+ flush).

        Runs on a worker thread via ``asyncio.to_thread`` (used by ``request()``,
        task 4.2).
        """
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    async def request(self, req: dict, timeout: float) -> dict:
        """Serialize one NDJSON request/response round-trip. (Task 4.2)

        Under ``self.lock`` (so at most one request is in flight at a time —
        Req 9.3): assign a strictly increasing id from 1 (Req 6.1), stamp
        ``v = PROTOCOL_VERSION`` (Req 6.2), and serialize the request as a single
        compact JSON line. The text payload travels only as a JSON string value
        inside this dict — never as a command-line argument (Req 18.2).

        The blocking stdin write and stdout read both run off the event loop via
        ``asyncio.to_thread`` (Req 9.4); the read is bounded by ``timeout`` so a
        stalled subprocess raises ``TimeoutError`` instead of hanging the loop
        (Req 9.5). On a response whose id does not match the request id we raise
        a ``DESYNC`` ``SupertoneError`` and discard the response (Req 6.4); a
        closed stdout surfaces as an ``EOF`` ``SupertoneError`` from
        ``_read_line_blocking`` (Req 9.6). Responses correspond to requests in
        FIFO order because only one is ever in flight (Req 6.6).

        Liveness (task 4.3): a failed request (``asyncio.TimeoutError`` on the
        20s synth ceiling, an ``EOF``/``DESYNC`` ``SupertoneError``, or a
        ``json.JSONDecodeError`` on a malformed response line) marks the
        subprocess ``_ready = False`` before re-raising. The current utterance
        falls back to edge_tts and the *next* ``speak()`` triggers a fresh spawn
        via ``ensure_alive()`` (Requirements 6.4, 9.6, 9.8).
        """
        async with self.lock:
            # Strictly increasing positive id (from 1), echoed back by the engine.
            req["id"] = next(self._next_id)
            # Bridge & subprocess both hardcode PROTOCOL_VERSION.
            req["v"] = PROTOCOL_VERSION
            # Compact single-line JSON (no embedded newline); text payload is a
            # JSON string value here, satisfying Req 18.2/18.3.
            line = json.dumps(req) + "\n"

            try:
                # Write off the event loop (blocking stdin write + flush).
                await asyncio.to_thread(self._write_line_blocking, line)

                # Read the single response line off the loop, bounded by timeout.
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self._read_line_blocking), timeout=timeout
                )
                resp = json.loads(raw)
                if resp.get("id") != req["id"]:
                    raise SupertoneError(
                        {"code": "DESYNC", "message": "id mismatch"}
                    )
                return resp
            except (asyncio.TimeoutError, SupertoneError, json.JSONDecodeError):
                # Timeout / EOF / DESYNC / malformed response => the subprocess is
                # no longer trustworthy. Mark it not-ready so the next speak()
                # respawns a fresh engine via ensure_alive() (Req 6.4, 9.6, 9.8).
                self._ready = False
                raise

    async def shutdown(self) -> None:
        """Gracefully stop the subprocess. (Task 4.3)

        Under ``self.lock``: if a subprocess is still alive (``poll()`` is
        ``None``) ask it to exit cleanly by writing a ``{"type":"shutdown"}``
        NDJSON line, flushing, and closing stdin as an EOF backup (the engine
        also exits its serve loop on stdin EOF — Req 10.1/10.2). Then wait up to
        5 seconds for the process to exit on a worker thread so the event loop is
        never blocked. On any exception or if the wait times out, force-kill the
        process so no orphan remains (Requirements 10.5, 10.6). Finally clear
        ``proc`` and ``_ready`` so a later ``ensure_alive()`` spawns fresh
        (Requirements 9.1, 9.8).
        """
        async with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                try:
                    # Polite shutdown request, then EOF as a backup signal.
                    self.proc.stdin.write(
                        json.dumps(
                            {"v": PROTOCOL_VERSION, "type": "shutdown"}
                        )
                        + "\n"
                    )
                    self.proc.stdin.flush()
                    self.proc.stdin.close()       # EOF backup (Req 10.2)
                    # Wait up to 5s off the event loop (Req 10.5).
                    await asyncio.to_thread(self.proc.wait, 5)
                except Exception:
                    # Timeout or any I/O error => force-kill, no orphan (Req 10.6).
                    self.proc.kill()
            self.proc = None
            self._ready = False


# ==========================================
# PHRASE TIMING ESTIMATOR (Option C)
# ==========================================
# Supertone TTS doesn't provide word-level timestamps.
# We estimate phrase boundaries proportional to character count.
# Format matches edge_tts word_timings: [{"word": str, "start": float, "duration": float}]

_PUNCTUATION_PHRASE = r"[.!?]"        # hard breaks — always split
_PUNCTUATION_CLAUSE = r"[,;:\u2014\u2013]"  # soft breaks — split if result >= MIN_PHRASE_CHARS
MIN_PHRASE_CHARS = 8                   # merge very short fragments into previous phrase
MAX_PHRASE_CHARS = 60                  # force-split long phrases at word boundary


def _split_into_phrases(text: str) -> list:
    """Split text into phrases by punctuation, merging short fragments."""
    if not text:
        return []

    # First pass: split on hard punctuation
    raw = re.split(r"(?<=[.!?])\s+", text.strip())
    phrases = []
    for segment in raw:
        segment = segment.strip()
        if not segment:
            continue
        # Second pass: split on soft punctuation if segment is long enough
        if len(segment) > MAX_PHRASE_CHARS:
            sub = re.split(r"(?<=[,;:\u2014\u2013])\s*", segment)
            buf = ""
            for part in sub:
                part = part.strip()
                if not part:
                    continue
                if len(buf) + len(part) <= MAX_PHRASE_CHARS:
                    buf = (buf + " " + part).strip() if buf else part
                else:
                    if buf:
                        phrases.append(buf)
                    buf = part
            if buf:
                phrases.append(buf)
        else:
            phrases.append(segment)

    # Merge very short fragments into neighbours
    merged = []
    for p in phrases:
        if merged and len(p) < MIN_PHRASE_CHARS:
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    return merged


def _estimate_phrase_timings(text: str, total_duration: float) -> list:
    """
    Estimate start/duration for each phrase proportional to character count.
    Returns list of {"word": phrase, "start": seconds, "duration": seconds}.
    """
    phrases = _split_into_phrases(text)
    if not phrases:
        return []

    total_chars = sum(len(p) for p in phrases)
    if total_chars == 0:
        return []

    timings = []
    cursor = 0.0
    for phrase in phrases:
        share = len(phrase) / total_chars
        dur = max(total_duration * share, 0.05)  # minimum 50ms per phrase
        timings.append({"word": phrase, "start": round(cursor, 3), "duration": round(dur, 3)})
        cursor += dur
    return timings


class TTSEngine:
    def __init__(self):
        self.device_id = self.find_virtual_cable()
        # Task 7.1 (Req 9.1): own the single Supertone subprocess lifecycle
        # manager so its lifetime tracks the engine. Lazy spawn — no subprocess
        # is launched until the first Supertone synthesize request. This makes
        # the defensive `hasattr(self, "supertone")` guard in _acquire_supertone()
        # (task 5.2) redundant; the attribute now always exists canonically.
        self.supertone = SupertoneProcess()

    def find_virtual_cable(self):
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if CONFIG["virtual_cable_name"].lower() in dev['name'].lower() and dev['max_output_channels'] > 0:
                print(f"[TTS] Jalur Virtual Cable ditemukan di Device ID: {i}")
                return i
        print("[TTS Info] Virtual Cable tidak terdeteksi, bersuara ke Default Speaker.")
        return None

    async def speak(self, text: str):
        """Dual-engine TTS entrypoint (Task 5.1).

        Reads CONFIG["tts_engine"] exactly once per utterance (Req 1.1) and
        routes to the Supertone path or the edge_tts path. The Supertone path
        is wrapped in a single try/except that falls back to edge_tts with the
        SAME text at most once on ANY failure (Req 2.1, 2.7). This method NEVER
        raises for any input or engine selection (Req 2.9): edge_tts failures
        are caught, logged, and swallowed (Req 2.8).
        """
        text = strip_tts_expression_tags(text)
        # Pagar terakhir untuk SEMUA pemanggil suara (refleks/fallback ikut).
        # Jalur balasan utama sudah menyensor lebih awal agar history dan chat
        # game juga aman; pemanggilan kedua idempoten.
        text = arti_speech_censor.censor_from_config(text, CONFIG)
        if not text:
            return
        # Req 1.1: read the configured engine once before selecting a path.
        engine = CONFIG.get("tts_engine", "edge_tts")

        if engine == "supertone":
            # Req 1.2: exact, case-sensitive "supertone" routes to Supertone first.
            try:
                # Task 5.2 fills in _acquire_supertone; Task 5.3 fills in _play_wav.
                synth_t0 = time.perf_counter()
                wav_path, word_timings = await self._acquire_supertone(text)
                pipeline_timer.note_tts_synth_ms(
                    int((time.perf_counter() - synth_t0) * 1000)
                )
                await self._play_wav(wav_path, text, word_timings, owns_temp=True)
                return
            except Exception as e:
                # Req 2.1-2.7: on ANY Supertone failure, log a warning that
                # identifies the cause and fall through to edge_tts with the
                # SAME text (at most once for this utterance).
                print(f"[TTS] Supertone failed ({type(e).__name__}: {e}); "
                      f"fallback ke edge_tts")
        elif engine != "edge_tts":
            # Req 1.4, 1.5: any value other than the exact strings "supertone"
            # or "edge_tts" (absent/None/empty/other) → warn identifying the
            # rejected value and use edge_tts, without modifying CONFIG.
            print(f"[TTS] tts_engine value {engine!r} tidak dikenali; "
                  f"menggunakan edge_tts")

        # Req 1.3 / 2.1 / 12.5: edge_tts path receives the SAME text unchanged,
        # so expression tags reach edge_tts as literal text.
        # Req 2.8, 2.9: if edge_tts also fails, log an error, produce no audio,
        # and return without raising so speak() stays total.
        try:
            await self._speak_edge_tts(text)
        except Exception as e:
            print(f"[TTS Error] edge_tts juga gagal "
                  f"({type(e).__name__}: {e}); tidak ada audio untuk utterance ini")

    async def _acquire_supertone(self, text: str):
        """Acquire synthesized audio from the Supertone subprocess. (Task 5.2)

        Ensures the subprocess is alive (lazy spawn + readiness handshake),
        builds the synthesize request from the current CONFIG values, dispatches
        it over NDJSON, and returns ``(wav_path, word_timings)``. Supertone
        exposes no word-boundary metadata, so ``word_timings`` is always an empty
        list — the subtitle path broadcasts full text only (Req 13.1).

        Every failure trigger surfaces as an exception so ``speak()`` (task 5.1)
        falls back to edge_tts at most once:

        - spawn / interpreter-resolution failure → ``ensure_alive()`` raises
          (``FileNotFoundError``/``OSError``) (Req 2.2);
        - model-load failure → ``ensure_alive()`` raises ``SupertoneError``
          (``MODEL_LOAD_FAILED``) (Req 2.3);
        - synthesis timeout → ``request()`` raises ``asyncio.TimeoutError``
          (Req 2.4);
        - ``ok: false`` response → we raise ``SupertoneError`` (Req 2.5);
        - subprocess EOF / desync → ``request()`` raises ``SupertoneError``
          (``EOF``/``DESYNC``) (Req 2.6).
        """
        # self.supertone is wired canonically in TTSEngine.__init__ by task 7.1.
        # Until then (and to keep this task self-contained), defensively create
        # the lifecycle manager so we never hit AttributeError. Harmless once
        # 7.1 lands because ensure_alive() reuses a live, ready subprocess.
        if not hasattr(self, "supertone") or self.supertone is None:
            self.supertone = SupertoneProcess()

        # Lazy spawn + READY handshake. Raises on unrecoverable failure
        # (interpreter missing, spawn error, ready-banner timeout, ok:false
        # banner), which propagates to speak() for fallback (Req 2.2, 2.3).
        await self.supertone.ensure_alive()

        # Build the synthesize request, reading the supertonic_* + preprocess
        # values from CONFIG at build time so live config changes apply to the
        # next utterance without respawning the subprocess (Req 17.2, 17.3, 17.4).
        # Polesan suara ([date removed], resep kuping operator): kalau DSP aktif,
        # sintesis DIPERLAMBAT sebesar faktor pitch — resample di poles_suara
        # mengembalikan durasinya, net tetap supertonic_speed setelan operator.
        # WAJIB lewat gerbang aktif() yang sama dengan pemolesnya: kompensasi
        # tanpa polesan = suara melambat DAN turun.
        _speed = CONFIG["supertonic_speed"]
        if arti_voice_dsp.aktif(CONFIG):
            _speed = _speed / arti_voice_dsp.faktor_pitch(
                CONFIG.get("supertonic_pitch_semitone", 0.0)
            )
        req = {
            "v": PROTOCOL_VERSION,
            "type": "synthesize",
            "text": text,
            "voice": CONFIG["supertonic_voice"],
            "speed": _speed,
            "lang": CONFIG["supertonic_lang"],
            "total_steps": CONFIG["supertonic_total_steps"],
            "preprocess_numbers": CONFIG["tts_preprocess_numbers"],
        }

        # Blocking stdin write + stdout read run off the event loop inside
        # request(); a stalled subprocess raises asyncio.TimeoutError (Req 2.4)
        # and a closed stdout raises a SupertoneError(EOF) (Req 2.6).
        resp = await self.supertone.request(
            req, timeout=float(CONFIG.get("supertonic_timeout_sec", SUPERTONE_TIMEOUT_S))
        )

        # ok:false → surface the structured error so speak() falls back (Req 2.5).
        if not resp.get("ok"):
            raise SupertoneError(resp.get("error", {}))

        # Polesan suara: range ×k + warna +semitone pada WAV hasil, ditulis
        # balik ke berkas yang sama (subtitle aman — Supertone memang tanpa
        # word timing, dan durasi net tidak berubah). CPU-bound ~25-100 ms,
        # jadi diungsikan ke thread.
        if arti_voice_dsp.aktif(CONFIG):
            def _poles(path: str) -> None:
                data, sr = sf.read(path)
                hasil = arti_voice_dsp.poles_suara(data, sr, CONFIG)
                sf.write(path, hasil, sr)
            await asyncio.to_thread(_poles, resp["wav_path"])

        # Supertone provides NO word timing → empty word_timings list (Req 13.1).
        return resp["wav_path"], []

    async def _play_wav(self, wav_path: str, text: str,
                        word_timings: list, owns_temp: bool):
        """Shared playback tail used by both engines (Task 5.3).

        Reads the synthesized WAV, resamples to 48kHz only when needed, runs
        the OBS subtitle/status broadcasts, drives the ``tts_is_playing`` mic
        gate with the 0.3s post-playback tail, plays through the virtual cable
        (falling back to the default device when ``device_id`` is None), and
        unlinks the temp WAV when this call owns it.

        Mirrors the edge_tts playback discipline exactly so behavior stays
        consistent across engines. The subtitle broadcast is intentionally NOT
        gated on a non-empty ``word_timings`` list: the Supertone path passes
        ``[]`` so that the full text is broadcast with an empty words list
        (Req 13.1), while edge_tts passes WordBoundary-derived timings
        (Req 13.2).
        """
        global tts_is_playing, tts_play_generation, _tts_started_ts
        try:
            # Req 15.1, 15.2: read the WAV and resample to 48kHz only when the
            # source sample rate differs (resample_audio no-ops at 48000).
            raw_data, raw_sr = sf.read(wav_path)
            data, samplerate = resample_audio(raw_data, raw_sr, 48000)

            # Phrase-based subtitle for Supertone (Option C):
            # Supertone doesn't provide word-level timing, so when word_timings is
            # empty, we estimate phrase boundaries proportional to character count.
            if subtitle_runtime.enabled and subtitle_runtime.server_started and not word_timings:
                audio_duration = len(data) / samplerate
                word_timings = _estimate_phrase_timings(text, audio_duration)

            # OBS Subtitle Integration (Req 13.1, 13.2, 13.5, 13.6):
            # Best-effort single subtitle broadcast before sd.play. Gated only on
            # enabled + server_started (NOT on non-empty word_timings) so the
            # Supertone path broadcasts the full text with an empty words list
            # while edge_tts broadcasts populated timings. The `text` argument is
            # forwarded byte-for-byte. Any exception is logged and swallowed so
            # audio playback proceeds unchanged (Req 13.5).
            if subtitle_runtime.enabled and subtitle_runtime.server_started:
                try:
                    await _subtitle_broadcast(word_timings, text)
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_subtitle failed: "
                          f"{type(e).__name__}: {e}")

            # OBS Subtitle Integration (Req 13.3):
            # Best-effort "speaking" status broadcast immediately before the
            # tts_is_playing = True assignment. Gated on enabled + status_enabled
            # + server_started; any exception is logged and swallowed so audio
            # playback is never blocked or altered (Req 13.5). This await is fine
            # here because Req 14.4 only forbids awaitables BETWEEN
            # tts_is_playing = True and sd.play(...).
            if (subtitle_runtime.enabled and subtitle_runtime.status_enabled
                    and subtitle_runtime.server_started):
                try:
                    await _subtitle_broadcast_status("speaking", "")
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_status(speaking) failed: "
                          f"{type(e).__name__}: {e}")

            # Req 14.1, 14.4, 15.2: tts_is_playing = True is the LAST assignment
            # before sd.play. No awaitable runs between this assignment and
            # sd.play(...); sd.wait() remains immediately after sd.play(...).
            # Req 15.3, 15.4: route to the configured virtual cable; when
            # device_id is None, sd.play falls back to the default output device.
            # Tunggu refleks yang mungkin sedang bunyi (maks ~0,75 dtk) supaya
            # TTS tidak memotongnya di tengah kata. Isi utama tidak pernah
            # di-skip — cuma menunggu sebentar. Diambil SEBELUM gate mic
            # ditutup: Req 14.4 melarang await antara `tts_is_playing = True`
            # dan `sd.play`, dan menunggu device memang urusan sebelum gate.
            # Menunggu, TIDAK memotong (aturan operator [date removed]). Batas 5 dtk
            # cuma pagar buntu — pemegang terlama yang sah adalah refleks
            # (WAV terpanjang 1,11 dtk), jadi kalau ini sampai habis berarti
            # ada yang salah dan harus KELIHATAN, bukan menimpa diam-diam.
            _got = await asyncio.to_thread(_audio_lock.acquire, True, 5.0)
            if not _got:
                print("[TTS] Gerbang audio tidak lepas dalam 5 dtk — "
                      "tetap diputar, kemungkinan ada bunyi yang terpotong.")
            try:
                tts_play_generation += 1
                tts_is_playing = True
                _tts_started_ts = time.time()
                play_t0 = time.perf_counter()
                with _sd_gerbang:
                    sd.play(data, samplerate, device=self.device_id)
                await asyncio.to_thread(sd.wait)
                pipeline_timer.note_tts_play_ms(
                    int((time.perf_counter() - play_t0) * 1000)
                )
            finally:
                if _got:
                    _audio_lock.release()
        except Exception as e:
            # Req 14.5, 15.5: playback failure path — log and fall through to the
            # finally block, which resets the mic gate (after the tail) and
            # continues running the bridge without raising.
            print(f"[TTS Error] Gagal memutar suara: {e}")
        finally:
            # Berikan jeda 0.3 detik agar gema suara speaker menghilang dari mic sebelum mulai mendengarkan lagi
            # Req 14.2, 14.3: reset tts_is_playing AFTER the 0.3s post-playback
            # sleep so mic gating stays consistent across engines.
            await asyncio.sleep(0.3)
            tts_is_playing = False

            # OBS Subtitle Integration (Req 13.4):
            # Best-effort "idle" status broadcast after the post-playback sleep
            # and the tts_is_playing reset. Same gating + try/except contract as
            # the "speaking" broadcast above (Req 13.5).
            if (subtitle_runtime.enabled and subtitle_runtime.status_enabled
                    and subtitle_runtime.server_started):
                try:
                    await _subtitle_broadcast_status("idle", "")
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_status(idle) failed: "
                          f"{type(e).__name__}: {e}")

            # Req 16.4, 16.5: best-effort cleanup of a WAV owned by this call.
            # The unlink is wrapped in its own try/except so file-permission or
            # disk errors never propagate out of speak().
            if owns_temp and os.path.exists(wav_path):
                try:
                    os.unlink(wav_path)
                except Exception as e:
                    print(f"[TTS Cleanup] unlink failed: {e}")

    async def _speak_edge_tts(self, text: str):
        global tts_is_playing, tts_play_generation, _tts_started_ts
        communicate = edge_tts.Communicate(text, CONFIG["tts_voice"])
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        # OBS Subtitle Integration (Task 5.1):
        # word_timings collects WordBoundary entries for downstream broadcast (used by Task 5.2).
        # audio_bytes_written tracks how much audio reached the tmp file so the inner
        # except below can decide between zero-byte abort (Req 1.8) and mid-utterance
        # warning (Req 1.9).
        word_timings: list[dict] = []
        audio_bytes_written = 0
        edge_t0 = time.perf_counter()

        try:
            # Req 1.1, 1.2, 1.3, 1.6: stream loop replaces communicate.save(tmp_path).
            # Audio chunks are written in arrival order; WordBoundary chunks are parsed
            # via _parse_word_boundary() (which already handles malformed input per
            # Req 1.7); any other chunk type is ignored without breaking the loop.
            try:
                with open(tmp_path, "wb") as audio_file:
                    async for chunk in communicate.stream():
                        ctype = chunk.get("type")
                        if ctype == "audio":
                            data = chunk.get("data") or b""
                            if data:
                                audio_file.write(data)
                                audio_bytes_written += len(data)
                        elif ctype == "WordBoundary":
                            wt = _parse_word_boundary(chunk)
                            if wt is not None:
                                word_timings.append(wt)
                        # else: ignore unknown chunk types (Req 1.3)
            except Exception as stream_err:
                # Req 1.8: zero bytes written → log, best-effort unlink, return without playing.
                if audio_bytes_written == 0:
                    print(f"[TTS Error] stream() failed before any audio: "
                          f"{type(stream_err).__name__}: {stream_err}")
                    try:
                        os.unlink(tmp_path)
                    except Exception as unlink_err:
                        print(f"[TTS Cleanup] unlink failed: {unlink_err}")
                    return
                # Req 1.9: ≥1 byte written → log warning and continue with what we have.
                print(f"[TTS Warning] stream() failed mid-utterance: "
                      f"{type(stream_err).__name__}: {stream_err}")

            raw_data, raw_sr = sf.read(tmp_path)
            
            # Resample ke 48000Hz (standard studio Windows) agar bebas error
            data, samplerate = resample_audio(raw_data, raw_sr, 48000)

            # OBS Subtitle Integration (Task 5.2):
            # Best-effort single subtitle broadcast before sd.play (Req 2.1, 2.2, 2.4,
            # 2.7, 2.8). Gated on enabled + server_started + non-empty word_timings;
            # the empty-list path is a pure no-op decision (Req 2.5, 4.5). The `text`
            # argument is forwarded byte-for-byte (Req 2.2). Any exception is logged
            # and swallowed so audio playback proceeds (Req 4.3, 4.4).
            if subtitle_runtime.enabled and subtitle_runtime.server_started and word_timings:
                try:
                    await _subtitle_broadcast(word_timings, text)
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_subtitle failed: "
                          f"{type(e).__name__}: {e}")

            # OBS Subtitle Integration (Task 5.3):
            # Best-effort "speaking" status broadcast immediately before the
            # tts_is_playing = True assignment (Req 4.6). Gated on enabled +
            # status_enabled + server_started; any exception is logged and
            # swallowed so audio playback is never blocked or altered (Req 4.8).
            # This await is fine here because Req 5.4 only forbids awaitables
            # BETWEEN tts_is_playing = True and sd.play(...).
            if (subtitle_runtime.enabled and subtitle_runtime.status_enabled
                    and subtitle_runtime.server_started):
                try:
                    await _subtitle_broadcast_status("speaking", "")
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_status(speaking) failed: "
                          f"{type(e).__name__}: {e}")

            # Req 5.4: tts_is_playing = True is the LAST assignment before sd.play.
            # No awaitable runs between this assignment and sd.play(...); sd.wait()
            # remains immediately after sd.play(...).
            pipeline_timer.note_tts_synth_ms(
                int((time.perf_counter() - edge_t0) * 1000)
            )
            # Tunggu refleks yang mungkin sedang bunyi (maks ~0,75 dtk) supaya
            # TTS tidak memotongnya di tengah kata. Isi utama tidak pernah
            # di-skip — cuma menunggu sebentar. Diambil SEBELUM gate mic
            # ditutup: Req 5.4 melarang await antara `tts_is_playing = True`
            # dan `sd.play`, dan menunggu device memang urusan sebelum gate.
            # Menunggu, TIDAK memotong (aturan operator [date removed]). Batas 5 dtk
            # cuma pagar buntu — pemegang terlama yang sah adalah refleks
            # (WAV terpanjang 1,11 dtk), jadi kalau ini sampai habis berarti
            # ada yang salah dan harus KELIHATAN, bukan menimpa diam-diam.
            _got = await asyncio.to_thread(_audio_lock.acquire, True, 5.0)
            if not _got:
                print("[TTS] Gerbang audio tidak lepas dalam 5 dtk — "
                      "tetap diputar, kemungkinan ada bunyi yang terpotong.")
            try:
                tts_play_generation += 1
                tts_is_playing = True
                _tts_started_ts = time.time()
                play_t0 = time.perf_counter()
                with _sd_gerbang:
                    sd.play(data, samplerate, device=self.device_id)
                await asyncio.to_thread(sd.wait)
                pipeline_timer.note_tts_play_ms(
                    int((time.perf_counter() - play_t0) * 1000)
                )
            finally:
                if _got:
                    _audio_lock.release()
        except Exception as e:
            print(f"[TTS Error] Gagal memutar suara: {e}")
        finally:
            # Berikan jeda 0.3 detik agar gema suara speaker menghilang dari mic sebelum mulai mendengarkan lagi
            # Req 5.5: reset tts_is_playing AFTER the post-playback sleep so mic
            # gating stays consistent with today's behavior.
            await asyncio.sleep(0.3)
            tts_is_playing = False

            # OBS Subtitle Integration (Task 5.3):
            # Best-effort "idle" status broadcast after the post-playback sleep
            # and the tts_is_playing reset (Req 4.7). Same gating + try/except
            # contract as the "speaking" broadcast above (Req 4.8).
            if (subtitle_runtime.enabled and subtitle_runtime.status_enabled
                    and subtitle_runtime.server_started):
                try:
                    await _subtitle_broadcast_status("idle", "")
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_status(idle) failed: "
                          f"{type(e).__name__}: {e}")

            # Req 5.7: best-effort temp file cleanup. The unlink is wrapped in
            # its own try/except so file-permission or disk errors never
            # propagate out of speak().
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception as e:
                    print(f"[TTS Cleanup] unlink failed: {e}")

# ==========================================
# 3. PENDENGAR SUARA LOKAL (ASR WITH AUTO-NOISE CALIBRATION & HALLUCINATION FILTER)
# ==========================================
# Pola literal output Whisper dari noise — sering persis begini (kapital + titik)
_ASR_LITERAL_PHANTOMS = frozenset({
    "Terima kasih.",
    "Thank you.",
    "Thanks.",
    "Selamat menikmati.",
    "Terima kasih telah menonton.",
    "Terima kasih sudah menonton.",
    "Thank you for watching.",
    "Thanks for watching.",
    "Like and subscribe.",
    "Sampai jumpa.",
})
# Tanpa titik / lowercase: cek lagi pakai durasi + jarak dari TTS
_ASR_BARE_THANKS = frozenset({"terima kasih", "thank you", "thanks", "Terima kasih", "Thank you", "Thanks"})
_ASR_ALWAYS_NOISE = frozenset({
    "selamat menikmati", "terima kasih telah menonton", "selamat datang", "halo halo",
    "ya ya ya", "oke oke", "terima kasih sudah menonton", "like and subscribe",
    "sampai jumpa", "goodbye", "bye bye", "i mean",
    "thank you for watching", "thanks for watching",
})
_ASR_NOISE_SUBSTRINGS = (
    "terima kasih telah menonton",
    "terima kasih sudah menonton",
    "like and subscribe",
    "thank you for watching",
)


def _normalize_asr_text(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def is_asr_noise_transcript(text: str, audio_duration_sec: float | None = None) -> bool:
    """Halusinasi Whisper — tapi ucapan terima kasih ASLI tetap lolos."""
    raw = text.strip()
    t = _normalize_asr_text(text)
    words = t.split()
    low = text.lower()

    # Pola spam persis dari log: 🎤 Hasil: "Terima kasih."
    if raw in _ASR_LITERAL_PHANTOMS:
        return True

    # Bukan noise: sebut Arti, atau kalimat agak panjang
    if is_arti_wake_call(raw) or len(words) > 4:
        return False

    if any(s in low for s in _ASR_NOISE_SUBSTRINGS):
        return True
    if t in _ASR_ALWAYS_NOISE:
        return True

    is_bare_thanks = raw in _ASR_BARE_THANKS or t in _ASR_BARE_THANKS or (
        len(words) <= 3 and any(k in t for k in ("terima kasih", "thank you", "makasih"))
    )
    if is_bare_thanks:
        secs_after_tts = None
        if hasattr(voice_listener_worker, "_last_tts_end"):
            secs_after_tts = time.time() - voice_listener_worker._last_tts_end
        # Ngomong cukup lama = kemungkinan besar kamu beneran bilang makasih
        if audio_duration_sec is not None and audio_duration_sec >= 1.8:
            return False
        # Jauh dari jawaban Arti = bukan echo speaker
        if secs_after_tts is not None and secs_after_tts >= 4.0:
            return False
        # Clip pendek + dekat TTS = halusinasi klasik ("Terima kasih.")
        if (audio_duration_sec is None or audio_duration_sec < 1.4) and (
            secs_after_tts is None or secs_after_tts < 4.0
        ):
            return True
        return False

    return False


def is_asr_echo_of_arti(text: str) -> bool:
    """Mic/telinga ke-detect suara speaker / jawaban Arti sendiri."""
    if not last_arti_reply_text:
        return False
    heard = _normalize_asr_text(text)
    said = _normalize_asr_text(last_arti_reply_text)
    # Chunk telinga 5 dtk sering cuma nangkep AWAL kalimat Arti — fragmen
    # pendek vs jawaban panjang lolos ratio 0.7 (live seharian [date removed]:
    # "Wah, masa semangat cari uang bohan gitu sih?" masuk ring). Potongan
    # >= 20 char yang persis ada di jawaban terakhir = echo — TAPI hanya
    # sesaat pasca-TTS (audit: tanpa batas waktu, streamer yang MENGUTIP
    # kalimat Arti 10 menit kemudian ikut dimakan; echo fisik cuma hidup
    # beberapa detik di ekor "listen" CABLE->headset).
    secs_after_tts = None
    if hasattr(voice_listener_worker, "_last_tts_end"):
        secs_after_tts = time.time() - voice_listener_worker._last_tts_end
    if (
        len(heard) >= 20
        and heard in said
        and secs_after_tts is not None
        and secs_after_tts < 15.0
    ):
        return True
    import difflib
    ratio = difflib.SequenceMatcher(None, heard, said).ratio()
    return ratio > 0.7


def filter_whisper_hallucination(text, is_passive_monitoring=True):
    """Menyaring halusinasi khas Whisper dari noise/ambient.
    
    Lebih smart dari versi lama:
    - Filter kata tunggal yang meaningless TAPI hanya kalau itu hasil transkrip sendiri
      (bukan bagian dari percakutan nyata — cek via is_passive_monitoring)
    - Filter repetitive patterns (Whisper ngulang kata karena bingung noise)
    """
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""
    
    # Phrases yang SELALU jadi hallucination Whisper (ignoring context selalu)
    # Ini pattern Whisper bilang "g sendiri kalau denger noise/static"
    phantom_phrases = {
        "subscribe", "like and subscribe", "thank you for watching",
        "thanks for watching", "see you next time", "salam sejahtera",
        "wassalamualaikum", "wassalamu'alaikum",
        "selamat menikmati",
    }
    text_clean = text.lower().strip(".,!?")
    if text_clean in phantom_phrases:
        return ""
    
    # Phrases yang HANYA hallucination kalau ini hasil transkrip pasif (bukan operator ngomong langsung)
    # Ini kata yang Whisper salah tangkap dari noise, tapi BISA jadi real speech
    contextual_hallucinations = {
        "terima kasih kerana menonton", "terima kasih kerana menonton!",
        "terima kasih guys", "sampai jumpa lagi",
    }
    if is_passive_monitoring and text_clean in contextual_hallucinations:
        return ""
    
    # Filter kata berulang (repetitive hallucination)
    # Whisper kadang ngulang kata yang sama berkali-kali kalau bingung noise
    words = text.split()
    if len(words) > 6:
        counter = collections.Counter(words)
        most_common_word, count = counter.most_common(1)[0]
        if count / len(words) > 0.6:
            return ""  # Return empty, bukan kata itu sendiri
    
    # Filter kata tunggal yang meaningless TAPI ekornya aja
    # Kalau operator beneran bilang "tidak" atau "bye" sendiri, biarin masuk
    # (handle via context di LLM, bukan di filter)
    meaningless_single = {"ah", "oh", "uh", "eh", "hm", "hmm"}
    if len(words) == 1 and text_clean.rstrip(".!?") in meaningless_single:
        return ""
    
    return text

_TTS_EXPRESSION_TAG_RE = re.compile(
    r"<\s*(laugh|sigh|breath|chuckle|giggle)\s*>",
    re.IGNORECASE,
)
_ANY_ANGLE_TAG_RE = re.compile(r"<\s*[a-zA-Z][a-zA-Z0-9_-]*\s*>")


def strip_tts_expression_tags(text: str) -> str:
    """Tag <laugh> dll → fonetik TTS; sigh/breath → hhh/hah (bukan kata 'sigh')."""
    if not text:
        return ""

    laugh_idx = 0
    laugh_variants = ("haha", "hehe", "hihi")

    def _repl_tag(match: re.Match) -> str:
        nonlocal laugh_idx
        tag = match.group(1).lower()
        if tag in ("laugh", "chuckle", "giggle"):
            word = laugh_variants[laugh_idx % len(laugh_variants)]
            laugh_idx += 1
            return f" {word}"
        if tag == "sigh":
            return " hhh"
        if tag == "breath":
            return " hah"
        return ""

    out = _TTS_EXPRESSION_TAG_RE.sub(_repl_tag, text)
    out = _ANY_ANGLE_TAG_RE.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    out = re.sub(
        r"\b(haha|hehe|hihi)(\s+\1\b)+",
        r"\1",
        out,
        flags=re.IGNORECASE,
    )
    return out.strip()


def clean_ai_reply(text):
    """Membersihkan yapping bahasa Inggris, membuang teks di dalam tanda bintang (bintang tunggal/ganda), dan menyisakan jawaban Indonesia murni menggunakan filter bahasa statistik."""
    if not text:
        return ""
    # 0. Hapus blok <think>...</think> dari model yang support "thinking" (Qwen3, dll)
    #    Handle juga kasus truncated (max_tokens motong di tengah <think>)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL).strip()  # Truncated: no closing tag
    # 1. Hapus teks di dalam bintang ganda (bold) atau bintang tunggal (italic)
    text = re.sub(r"\*\*.*?\*\*", "", text)
    text = re.sub(r"\*.*?\*", "", text)
    
    # 2. Split menjadi kalimat-kalimat untuk memfilter yapping per kalimat
    sentences = re.split(r'(?<=[.!?])\s+', text)
    cleaned_sentences = []
    
    # Kumpulan kata bahasa Inggris umum (stop words) untuk penyaringan bahasa statistik
    english_words = {
        # Grammatical particles / pronouns
        "is", "it", "it's", "ready", "for", "immediate", "use", "the", "to", "and", "of", "in", 
        "that", "this", "with", "from", "you", "are", "not", "no", "shut", "up", "i", "have", "devis", 
        "respond", "interpret", "streamer", "parameter", "context", "analyze", "utterance", "witty", 
        "cute", "proceed", "response", "believe", "got", "my", "profile", "playful", "nonsensical", 
        "opportunity", "making", "good", "progress", "active", "ready", "confirm", "available", 
        "availability", "will", "would", "should", "could", "can", "do", "does", "did", "have", 
        "has", "had", "been", "was", "were", "be", "am", "are", "your", "yours", "me", "my", "myself",
        "an", "but", "or", "as", "if", "so", "than", "then", "there", "their", "them", "they", "we",
        "i've", "rare", "leaning", "persona", "crafting", "emphasizes", "qualities", "precise", "aim",
        "concise", "maintain", "existing", "memory", "emojis", "avoided", "kept", "previous", "feedback",
        # Conversational English words (very common in yapping)
        "here", "let's", "make", "amazing", "i'm", "bad", "bro", "hello", "hi", "great", "good", "fine",
        "stream", "co-host", "ai", "intelligent", "talk", "speak", "chat", "conversation", "dialogue",
        "words", "shutting", "talking", "yapping", "thought", "think", "thought", "how", "what", "why",
        "when", "where", "who", "which", "whose", "whom", "about", "above", "below", "under", "over"
    }
    
    for sentence in sentences:
        words = [w.strip(".,!?\"'()").lower() for w in sentence.split() if w.strip()]
        if not words:
            continue
            
        # Hitung rasio kata bahasa Inggris dalam kalimat
        english_count = sum(1 for w in words if w in english_words)
        
        # Jika kalimat MAYORITAS bahasa Inggris (>=60% kata Inggris DAN minimal 4 kata Inggris),
        # baru buang sebagai yapping. Threshold rendah sebelumnya (2 kata/30%) terlalu agresif
        # karena bahasa Indonesia sering campur kata Inggris ("aku lagi chat di YouTube").
        if english_count >= 4 and (english_count / len(words)) >= 0.6:
            continue
            
        cleaned_sentences.append(sentence.strip())
        
    final_text = " ".join(cleaned_sentences).strip()
    return final_text

local_whisper_model = None


def resolve_asr_input_device(config: dict | None = None) -> tuple[int | None, str]:
    """Pilih input mic — jangan pakai Stereo Mix (suara PC, bukan mic user)."""
    cfg = config or CONFIG
    devices = sd.query_devices()
    default_in = sd.default.device[0]

    explicit = cfg.get("asr_input_device")
    if explicit is not None and explicit != "":
        if isinstance(explicit, int) or (isinstance(explicit, str) and str(explicit).isdigit()):
            idx = int(explicit)
            if 0 <= idx < len(devices) and devices[idx]["max_input_channels"] > 0:
                return idx, devices[idx]["name"]
        needle = str(explicit).lower()
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and needle in dev["name"].lower():
                return i, dev["name"]

    skip = [p.lower() for p in (cfg.get("asr_skip_device_patterns") or [])]

    def _skip(name: str) -> bool:
        n = name.lower()
        return any(p in n for p in skip)

    candidates: list[tuple[int, int, str]] = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] <= 0:
            continue
        name = dev["name"]
        if _skip(name):
            continue
        score = 0
        nl = name.lower()
        if any(k in nl for k in ("microphone", "mic ", " mic", "headset", "headphone", "usb")):
            score += 3
        if i == default_in:
            score += 1
        candidates.append((score, i, name))

    if candidates:
        candidates.sort(key=lambda x: (-x[0], x[1]))
        _, idx, name = candidates[0]
        if default_in is not None and default_in != idx:
            def_name = devices[default_in]["name"]
            if _skip(def_name):
                print(
                    f"[ASR Warning] Windows default input = '{def_name}' "
                    f"(bukan mic fisik) -> pakai '{name}'"
                )
        return idx, name

    dev = sd.query_devices(kind="input")
    return default_in, dev["name"]


def transcribe_audio(audio_array, samplerate=16000, use_groq=True, *,
                     api_key=None, language="id", quiet=False):
    """ASR bersama mic + desktop audio.

    Default (mic) TIDAK berubah: kunci utama + paksa "id". Jalur desktop pass
    api_key=<pool telinga>, language=None (auto-detect), quiet=True — print
    "Sukses mentranskrip!" tiap 5 dtk membanjiri terminal (pagi 2026-08-03);
    telinga cukup satu baris [Dengar] berisi teksnya dari worker.
    """
    global local_whisper_model

    def _tel_asr(*, provider: str, model: str, latency_ms: int, ok: bool, extra: dict | None = None) -> None:
        try:
            import arti_api_telemetry as tel

            tel.record_call(
                subsystem="asr",
                provider=provider,
                model=model,
                latency_ms=latency_ms,
                ok=ok,
                usage=tel.UsageInfo(),
                extra=extra,
                config=CONFIG,
            )
        except Exception:
            pass

    audio_sec = round(len(audio_array) / float(samplerate or 16000), 2)
    groq_key = api_key or CONFIG.get("groq_api_key")
    whisper_models = ["whisper-large-v3", "whisper-large-v3-turbo"]  # Rolling: 2K + 2K = 4K RPD
    if use_groq and groq_key and groq_key != "YOUR_GROQ_API_KEY" and groq_key.startswith("gsk_"):
        groq_model = whisper_models[getattr(transcribe_audio, "_widx", 0) % len(whisper_models)]
        transcribe_audio._widx = getattr(transcribe_audio, "_widx", 0) + 1
        t0 = time.perf_counter()
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_audio:
                tmp_audio_path = tmp_audio.name

            sf.write(tmp_audio_path, audio_array, samplerate)

            headers = {
                "Authorization": f"Bearer {groq_key}"
            }
            url = "https://api.groq.com/openai/v1/audio/transcriptions"

            with open(tmp_audio_path, "rb") as f:
                files = {
                    "file": ("recording.wav", f, "audio/wav")
                }
                data = {"model": groq_model}
                if language:
                    data["language"] = language  # mic: paksa "id"; desktop: auto
                res = arti_http_util.groq_session().post(
                    url, headers=headers, files=files, data=data, timeout=8
                )

            if os.path.exists(tmp_audio_path):
                os.unlink(tmp_audio_path)

            ms = int((time.perf_counter() - t0) * 1000)
            if res.status_code == 200:
                text = res.json().get("text", "").strip()
                if text:
                    if not quiet:
                        print(f"☁️ [ASR - Groq Cloud Whisper] Sukses mentranskrip!")
                    _tel_asr(
                        provider="groq",
                        model=groq_model,
                        latency_ms=ms,
                        ok=True,
                        extra={"audio_sec": audio_sec, "backend": "cloud"},
                    )
                    return text
                _tel_asr(
                    provider="groq",
                    model=groq_model,
                    latency_ms=ms,
                    ok=False,
                    extra={"audio_sec": audio_sec, "backend": "cloud", "reason": "empty_text"},
                )
            else:
                _tel_asr(
                    provider="groq",
                    model=groq_model,
                    latency_ms=ms,
                    ok=False,
                    extra={"audio_sec": audio_sec, "backend": "cloud", "http": res.status_code},
                )
                print(f"[ASR Warning] Groq Cloud Whisper gagal (status {res.status_code}): {res.text}. Menggunakan local Whisper...")
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            _tel_asr(
                provider="groq",
                model=groq_model,
                latency_ms=ms,
                ok=False,
                extra={"audio_sec": audio_sec, "backend": "cloud", "error": str(e)[:120]},
            )
            print(f"[ASR Warning] Error Groq Cloud Whisper: {e}. Menggunakan local Whisper...")

    # Fallback ke local Whisper (Lazy loading agar hemat RAM/VRAM saat startup)
    local_model = "whisper-small"
    try:
        t0_local = time.perf_counter()
        if local_whisper_model is None:
            try:
                print("[ASR] Memuat model Whisper lokal ('small' GPU CUDA)...")
                local_whisper_model = WhisperModel("small", device="cuda", compute_type="float16")
                print("[ASR] Model Whisper 'small' sukses dimuat di GPU!")
            except Exception:
                print("[ASR] GPU VRAM penuh, fallback ke CPU...")
                local_whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
                print("[ASR] Model Whisper 'small' dimuat di CPU (fallback).")
        segments, _ = local_whisper_model.transcribe(audio_array, beam_size=1, language=language)
        text = " ".join([seg.text for seg in segments]).strip()
        ms_local = int((time.perf_counter() - t0_local) * 1000)
        if text:
            if not quiet:
                print(f"\U0001f4bb [ASR - Local Whisper] Sukses mentranskrip!")
            _tel_asr(
                provider="local",
                model=local_model,
                latency_ms=ms_local,
                ok=True,
                extra={"audio_sec": audio_sec, "backend": "local"},
            )
            return text
        _tel_asr(
            provider="local",
            model=local_model,
            latency_ms=ms_local,
            ok=False,
            extra={"audio_sec": audio_sec, "backend": "local", "reason": "empty_text"},
        )
    except Exception as e:
        # Kalau CUDA inference gagal (cublas dll), fallback ke CPU
        if "cublas" in str(e).lower() or "cuda" in str(e).lower():
            print(f"[ASR] GPU inference gagal ({e}), switch ke CPU...")
            try:
                local_whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
                print("[ASR] Model Whisper 'small' dimuat ulang di CPU.")
                segments, _ = local_whisper_model.transcribe(audio_array, beam_size=1, language=language)
                text = " ".join([seg.text for seg in segments]).strip()
                ms_cpu = int((time.perf_counter() - t0_local) * 1000)
                if text:
                    if not quiet:
                        print(f"\U0001f4bb [ASR - Local Whisper CPU] Sukses mentranskrip!")
                    _tel_asr(
                        provider="local",
                        model=local_model,
                        latency_ms=ms_cpu,
                        ok=True,
                        extra={"audio_sec": audio_sec, "backend": "local_cpu"},
                    )
                    return text
            except Exception as e2:
                print(f"[ASR Error] CPU fallback juga gagal: {e2}")
        else:
            print(f"[ASR Error] Local Whisper gagal: {e}")
        return ""

def donation_alert_delay_sec(message: str, config: dict | None = None) -> float:
    """Berapa lama nunggu alert donasi overlay selesai sebelum Arti bereaksi.

    streamer 2026-08-02: alert OBS punya audio sendiri — "si X donasi Rp Y"
    (±4 detik) lalu pesannya DIBACAKAN. Arti tidak boleh tabrakan suara.
    Estimasi: base (nama+nominal+jingle) + waktu baca pesan per karakter,
    di-cap. base <= 0 = tanpa tunda (perilaku instan).
    """
    cfg = config or CONFIG
    base = float(cfg.get("donation_alert_base_sec", 5.0))
    if base <= 0:
        return 0.0
    per = float(cfg.get("donation_alert_per_char_sec", 0.055))
    cap = float(cfg.get("donation_alert_max_sec", 20.0))
    return min(base + per * len(message or ""), cap)


def schedule_donation_trigger(text: str, viewer_name: str | None, message: str) -> None:
    """Antre trigger donasi SETELAH alert overlay selesai (timer daemon)."""
    delay = donation_alert_delay_sec(message)
    if delay <= 0:
        queue_voice_trigger(text, trigger_type="donation", viewer_name=viewer_name)
        return
    print(f"[Donasi] Nunggu alert overlay selesai (~{delay:.0f}s) sebelum Arti bereaksi...")
    t = threading.Timer(
        delay,
        lambda: queue_voice_trigger(text, trigger_type="donation", viewer_name=viewer_name),
    )
    t.daemon = True
    t.start()


# Fitur E — state playback media share + watcher video.
_video_watcher = None
_media_playback_until = 0.0


def hold_media_playback(seconds: float) -> None:
    """Media share mulai diputar overlay: potong TTS Arti + tahan turn baru.

    Use case streamer: Arti lagi ngomong -> video nongol di tengah layar ->
    dia BERHENTI, nonton bareng, komentar setelah selesai.
    """
    global _media_playback_until
    # max(): dua media share beruntun tidak boleh MEMENDEKKAN hold — audit
    # membuktikan share #2 (klip pendek) menimpa hold #1 dan Arti ngomong di
    # tengah klip pertama.
    _media_playback_until = max(
        _media_playback_until, time.time() + max(0.0, seconds)
    )
    if tts_is_playing:
        try:
            with _sd_gerbang:
                sd.stop()
            print("[Video] TTS dipotong — media share mulai diputar")
        except Exception:  # noqa: BLE001
            pass
        try:
            request_asr_stream_restart("media share interrupt")
        except Exception:  # noqa: BLE001
            pass
    print(f"[Video] Playback hold {seconds:.0f}s — Arti nonton bareng")


def _video_set_qa_window(event_id: str, window_sec: float) -> None:
    """Q&A watch-party sementara; auto-lepas.

    RAG umum TETAP HIDUP (watch_party_allow_general_rag): mode eksklusif itu
    desain nonton episode panjang; untuk klip media share justru RAG umum yang
    memegang doc video hasil reindex — live sore2 2026-08-02 RAG mati ~separuh
    sesi (20x skip) karena 5 window beruntun, viewer nanya memori = Arti amnesia.
    """
    CONFIG["watch_party_event_id"] = event_id
    CONFIG["watch_party_enabled"] = True
    CONFIG["watch_party_allow_general_rag"] = True
    print(f"[Video] Q&A window {window_sec:.0f}s aktif ({event_id})")

    def _clear() -> None:
        if CONFIG.get("watch_party_event_id") == event_id:
            CONFIG["watch_party_enabled"] = False
            CONFIG["watch_party_event_id"] = ""
            CONFIG["watch_party_allow_general_rag"] = False
            print(f"[Video] Q&A window {event_id} selesai")

    t = threading.Timer(window_sec, _clear)
    t.daemon = True
    t.start()


def start_video_watcher() -> None:
    global _video_watcher
    import arti_video_watcher

    def _reindex() -> None:
        try:
            arti_vault_rag.reindex_all(CONFIG, verbose=False)
        except Exception as e:  # noqa: BLE001
            print(f"[Video] Reindex dokumen video gagal: {type(e).__name__}")

    _video_watcher = arti_video_watcher.VideoWatcher(
        CONFIG,
        {
            "queue_reaction": lambda text, viewer: queue_voice_trigger(
                text, trigger_type="video", viewer_name=viewer
            ),
            "reindex": _reindex,
            "set_qa": _video_set_qa_window,
        },
    )
    _video_watcher.start()


def _on_donation(ev) -> None:
    """Callback listener donasi (dipanggil dari thread arti_donations).

    History dicatat SEKARANG (konteks LLM), media share ditampung untuk Fitur
    E, reaksi suara DITUNDA sampai alert overlay selesai. Level modul —
    test AST mengunci urutan startup main_loop, nested def mengotorinya.
    """
    import arti_donations  # noqa: PLC0415

    kind = getattr(ev, "kind", "donation")
    if kind == "membership":
        detail = (
            f" ({ev.membership_months} bulan)"
            if getattr(ev, "membership_months", 0) > 0
            else ""
        )
        print(f"[Membership] {ev.platform}: {ev.name}{detail}")
        trigger = arti_donations.format_membership_trigger(ev)
        add_to_history(
            f"Viewer {ev.name} ({ev.platform_label})",
            trigger,
        )
        schedule_donation_trigger(trigger, ev.name, ev.message)
        return

    if kind == "media_points":
        # Streamlabs loyalty points (sumber tersering, kata operator): KASUAL —
        # tanpa upacara terima kasih donasi; langsung nonton bareng + komentar.
        # Gagal submit (antrean penuh / video off) = diam — cuma points.
        print(f"[Donasi] {ev.platform}: {ev.name} media share (loyalty points)")
        add_to_history(
            f"Viewer {ev.name} ({ev.platform.title()})",
            f"[MEDIA SHARE] {ev.message or ev.media_url}",
        )
        _submit_media_job(ev, donation_label="")
        return

    print(f"[Donasi] {ev.platform}: {ev.name} kirim {ev.amount_label}!")
    add_to_history(
        f"Viewer {ev.name} ({ev.platform.title()})",
        f"[DONASI {ev.amount_label}] {ev.message or '(tanpa pesan)'}",
    )
    if ev.media_url:
        arti_donations.pending_media.append(ev)
        if _submit_media_job(ev, donation_label=ev.amount_label):
            # Reaksi DIGABUNG: terima kasih + komentar isi video SETELAH
            # playback. Terima kasih terpisah TIDAK dikirim.
            return
        # Submit gagal (video off / antrean penuh / URL aneh): donatur BAYAR —
        # ucapan terima kasih normal tetap wajib jalan (crosscheck pra-live #2:
        # dulu jalur ini bisu total).
        print(
            f"[Donasi] Media share tidak bisa diproses — terima kasih tetap "
            f"jalan ({ev.media_url})"
        )
    schedule_donation_trigger(
        arti_donations.format_donation_trigger(ev), ev.name, ev.message
    )


def _submit_media_job(ev, donation_label: str) -> bool:
    """Media share -> antre job video; HOLD hanya kalau job diterima.

    Return False bila watcher mati/URL tak dikenal/ditolak guard — pemanggil
    yang memutuskan fallback (donasi berbayar: terima kasih normal).
    Urutan PENTING: submit dulu, hold belakangan — dulu hold jalan duluan dan
    job ditolak = Arti bengong 60 detik untuk video yang tak pernah diproses.
    """
    import arti_video_watcher as avw  # noqa: PLC0415

    ids = avw.extract_youtube_ids(ev.media_url or "")
    if not ids or _video_watcher is None or not CONFIG.get("video_enabled", False):
        return False
    clip = ev.media_end - ev.media_start if ev.media_end > ev.media_start else 0
    clip = min(clip, 180)  # payload aneh tidak boleh membekukan Arti bermenit-menit
    accepted = _video_watcher.submit(avw.VideoJob(
        video_id=ids[0],
        source=ev.platform,
        viewer=ev.name,
        donation_label=donation_label,
        message=ev.message,
        clip_start=ev.media_start,
        clip_end=ev.media_end,
    ))
    if not accepted:
        return False
    # Alert overlay (jingle + nama + nominal) main DULU sebelum klipnya —
    # hold harus mencakup keduanya, kalau tidak Arti nyaut ~5 dtk kecepetan.
    alert = float(CONFIG.get("donation_alert_base_sec", 5.0))
    hold = alert + (clip or float(CONFIG.get("mediashare_hold_sec", 60.0))) + 3.0
    hold_media_playback(hold)
    return True


def extract_superchat(item: dict) -> dict | None:
    """Data Super Chat / paid sticker dari satu item innertube — pure, testable.

    D0 rencana v0.7: `purchaseAmountText` selama ini DIBUANG oleh parse_action
    padahal datanya sudah mengalir. Return {'name','message','paid_amount'}
    atau None kalau bukan item berbayar.
    """
    for key in ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"):
        r = item.get(key)
        if not r:
            continue
        amount = ((r.get("purchaseAmountText") or {}).get("simpleText") or "").strip()
        author = ((r.get("authorName") or {}).get("simpleText") or "Unknown").strip()
        runs = (r.get("message") or {}).get("runs", [])
        msg = "".join(x.get("text", "") for x in runs).strip()
        return {"name": author, "message": msg, "paid_amount": amount}
    return None


def is_bot_viewer(viewer: str, config: dict | None = None) -> bool:
    """Bot layanan chat (leaderboard/command bot) tidak boleh mentrigger jawaban.

    Live 11,5 jam 2026-08-01: @Streamlabs memposting leaderboard "Top Total Jam
    nonton" dan bridge mengantri jawaban untuknya. Pesannya tetap dicatat di
    history (konteks berguna), tapi tidak pernah dianggap panggilan.
    """
    name = (viewer or "").lstrip("@").strip().lower()
    if not name:
        return False
    bots = (config or {}).get("yt_bot_viewers")
    if bots is None:
        bots = ["Streamlabs", "Nightbot", "StreamElements", "Moobot", "Fossabot"]
    return name in {str(b).lstrip("@").strip().lower() for b in bots}


_emoji_run_dicatat = False


def _teks_run_chat(run: dict) -> str:
    """Ambil teks dari SATU run pesan chat YouTube, termasuk emoji.

    Sampai 27 Agu jalur chat memakai `r.get("text", "")` saja. Emoji datang
    sebagai run `{"emoji": {...}}` TANPA kunci `text`, jadi dua hal terjadi
    diam-diam:

      "halo <emoji>"  -> Arti cuma melihat "halo"
      "<emoji>" saja  -> pesan jadi string kosong lalu DI-DROP; Arti tidak
                         pernah tahu ada orang menulis di chat

    Emoji standar: `emojiId` berisi karakter Unicode-nya, jadi dipakai apa
    adanya. Emoji kustom kanal: `emojiId` berupa ID panjang, jadi yang
    dipakai NAMA-nya (`shortcuts` atau label aksesibilitas) — nama itu yang
    membawa maknanya buat model, bukan gambarnya.

    Bentuk run TIDAK ditebak buta: pemanggil mencetak satu run emoji ASLI
    sekali per sesi (lihat `_emoji_run_dicatat`) supaya bentuk sungguhannya
    bisa dipastikan, sesuai pelajaran 27 Agu — gerbang tool agy pertama
    memakai bentuk karangan dan tidak pernah menembak sekali pun.
    """
    if not isinstance(run, dict):
        return ""
    teks = run.get("text")
    if isinstance(teks, str) and teks:
        return teks

    emo = run.get("emoji")
    if not isinstance(emo, dict):
        return ""

    eid = str(emo.get("emojiId") or "")
    kustom = emo.get("isCustomEmoji")
    if kustom is None:
        # Tidak semua payload membawa bendera ini. Emoji standar ID-nya
        # pendek (karakter Unicode); yang kustom panjang dan ber-"/".
        kustom = ("/" in eid) or len(eid) > 8
    if not kustom and eid:
        return eid

    nama = ""
    jalan = emo.get("shortcuts")
    if isinstance(jalan, list) and jalan:
        nama = str(jalan[0] or "")
    if not nama:
        gambar = emo.get("image")
        if isinstance(gambar, dict):
            aks = (gambar.get("accessibility") or {}).get("accessibilityData") or {}
            nama = str(aks.get("label") or "")
    nama = nama.strip().strip(":").strip()
    return f":{nama}:" if nama else ""


def teks_pesan_chat(runs) -> str:
    """Gabung semua run jadi satu teks pesan (emoji ikut)."""
    if not isinstance(runs, list):
        return ""
    return "".join(_teks_run_chat(r) for r in runs).strip()


_yt_chat_running = True


def stop_youtube_chat() -> None:
    """Hentikan worker chat sebelum pipeline shutdown yang memakan waktu."""
    global _yt_chat_running
    _yt_chat_running = False


def youtube_chat_worker():
    """Mendengarkan YouTube Live Chat via innertube API (proven, tested).
    
    Cukup ganti youtube_video_id di CONFIG setiap kali mau live.
    Video ID = bagian setelah ?v= di URL YouTube.
    Tidak perlu extension, tidak perlu browser source tambahan.
    """
    if not CONFIG.get("youtube_chat_enabled"):
        return
    
    video_id = CONFIG.get("youtube_video_id", "")
    if not video_id or video_id == "YOUR_VIDEO_ID":
        print("\n[YouTube Chat] Video ID belum diisi! Ganti 'YOUR_VIDEO_ID' di CONFIG.")
        return
    
    print(f"\n[YouTube Chat] Menghubungkan ke live chat (Video: {video_id})...")
    
    last_chat_trigger_time = 0
    seen_ids = set()
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # Bypass consent page
    session.cookies.set("CONSENT", "YES+cb.20240101-01-p0.en+FX+001", domain=".youtube.com")
    session.cookies.set("SOCS", "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMTAxLjAxX3AwGgJlbiACGgYIgLCdsgY", domain=".youtube.com")
    
    def get_initial_chat():
        """Fetch halaman live_chat dan ambil continuation token + pesan awal"""
        try:
            resp = session.get(f"https://www.youtube.com/live_chat?v={video_id}&is_popout=1", timeout=15)
            resp.raise_for_status()
            page = resp.text
            
            # Parse ytInitialData
            match = re.search(r'(?:window\["ytInitialData"]|var ytInitialData)\s*=\s*(\{.*?\});', page, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                
                # Cari continuation token (recursive)
                def find_cont(obj, depth=0):
                    if depth > 10 or not obj: return None
                    if isinstance(obj, dict):
                        if 'continuation' in obj and isinstance(obj['continuation'], str) and len(obj['continuation']) > 20:
                            return obj['continuation']
                        for v in obj.values():
                            r = find_cont(v, depth+1)
                            if r: return r
                    elif isinstance(obj, list):
                        for item in obj:
                            r = find_cont(item, depth+1)
                            if r: return r
                    return None
                
                continuation = find_cont(data)
                
                # Parse initial messages
                initial_msgs = []
                try:
                    actions = data['contents']['liveChatRenderer']['actions']
                    for a in actions:
                        msg = parse_action(a)
                        if msg: initial_msgs.append(msg)
                except:
                    pass
                
                return continuation, initial_msgs
            else:
                # Fallback: regex untuk continuation token
                all_conts = re.findall(r'"continuation":"([^"]{20,})"', page)
                if all_conts:
                    return all_conts[0], []
            
            return None, []
        except Exception as e:
            print(f"[YouTube Chat Error] Gagal fetch halaman: {e}")
            return None, []
    
    def parse_action(action):
        """Parse satu chat action menjadi dict {name, message}"""
        item = action.get('addChatItemAction', {}).get('item', {})
        superchat = extract_superchat(item)
        renderer = (item.get('liveChatTextMessageRenderer')
                    or item.get('liveChatPaidMessageRenderer')
                    or item.get('liveChatPaidStickerRenderer'))
        if not renderer: return None

        msg_id = renderer.get('id', '')
        if msg_id in seen_ids: return None
        seen_ids.add(msg_id)

        # Keep seen set manageable
        if len(seen_ids) > 5000:
            excess = list(seen_ids)[:2500]
            for x in excess: seen_ids.discard(x)

        author = renderer.get('authorName', {}).get('simpleText', 'Unknown')
        runs = renderer.get('message', {}).get('runs', [])
        msg = teks_pesan_chat(runs)

        # Cetak SATU run emoji asli per sesi. Bentuk payload YouTube tidak
        # boleh cuma diasumsikan dari dokumentasi — pelajaran [date removed].
        global _emoji_run_dicatat
        if not _emoji_run_dicatat:
            for r in runs:
                if isinstance(r, dict) and isinstance(r.get('emoji'), dict):
                    _emoji_run_dicatat = True
                    print("[YT Chat] bentuk run emoji (sekali per sesi): "
                          + json.dumps(r)[:300])   # ensure_ascii default: aman di konsol cp1252
                    break

        # URL sering ngumpet di navigationEndpoint, bukan di text run —
        # tempelkan ke pesan supaya deteksi link video melihatnya (Fitur E).
        for r in runs:
            nav = r.get('navigationEndpoint') or {}
            url = (nav.get('urlEndpoint') or {}).get('url', '')
            wid = (nav.get('watchEndpoint') or {}).get('videoId', '')
            if wid and not url:
                url = f"https://youtu.be/{wid}"
            if url and url not in msg:
                msg = (msg + " " + url).strip()

        paid = (superchat or {}).get('paid_amount', '')
        # Super Chat tanpa teks (sticker / nominal saja) TIDAK boleh di-drop —
        # justru wajib direspon (D0).
        if not msg and not paid: return None
        out = {'name': author, 'message': msg}
        if paid:
            out['paid_amount'] = paid
        return out
    
    def poll_chat(continuation):
        """Poll innertube API untuk chat baru"""
        try:
            resp = session.post(
                "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false",
                json={
                    "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}},
                    "continuation": continuation
                },
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            
            # Parse continuation berikutnya
            next_cont = None
            timeout_ms = 10000
            conts = data.get('continuationContents', {}).get('liveChatContinuation', {}).get('continuations', [])
            for c in conts:
                if 'invalidationContinuationData' in c:
                    next_cont = c['invalidationContinuationData'].get('continuation')
                    timeout_ms = c['invalidationContinuationData'].get('timeoutMs', 10000)
                elif 'timedContinuationData' in c:
                    next_cont = c['timedContinuationData'].get('continuation')
                    timeout_ms = c['timedContinuationData'].get('timeoutMs', 10000)
            
            # Parse messages
            messages = []
            actions = data.get('continuationContents', {}).get('liveChatContinuation', {}).get('actions', [])
            for a in actions:
                msg = parse_action(a)
                if msg: messages.append(msg)
            
            return messages, next_cont, timeout_ms
        except Exception as e:
            print(f"[YouTube Chat Warning] Poll error: {e}")
            return [], continuation, 10000
    
    def process_message(msg):
        """Proses satu chat message"""
        nonlocal last_chat_trigger_time
        viewer = msg['name']
        chat_msg = msg['message']
        paid = msg.get('paid_amount', '')

        shown = f"[SUPER CHAT {paid}] {chat_msg}".strip() if paid else chat_msg
        print(f"\U0001f4ac [YT Chat] {viewer}: {shown}")

        # !command = perintah untuk BOT LAIN (Nightbot/StreamElements), bukan
        # untuk Arti (operator [date removed]: "!nasi gitu... itu bot commands").
        # Tidak men-trigger, TIDAK masuk history/ingatan (kurator shutdown
        # jangan pernah melihatnya), dan status "penonton baru"-nya TIDAK
        # hangus — sapaan selamat datang menunggu pesan manusiawi pertamanya.
        # Super Chat dikecualikan: orang bayar selalu dijawab.
        if not paid and chat_msg.lstrip().startswith("!"):
            print(f"[YT Chat Info] Perintah bot dari {viewer} — dilewati.")
            return

        if is_bot_viewer(viewer, CONFIG):
            # Dulu pesan bot tetap dicatat ke history (konteks leaderboard).
            # Keputusan operator [date removed]: jawaban bot poin dkk "yaa ignore" —
            # keluar dari history juga supaya tidak mengotori prompt/ingatan.
            return

        add_to_history(f"Viewer {viewer} (YouTube)", shown)

        # Fitur E (keputusan operator: AUTO): link YouTube di chat -> antre video.
        # Tidak return — pesan yang sama boleh sekaligus manggil Arti.
        if CONFIG.get("video_enabled", False) and _video_watcher is not None:
            import arti_video_watcher as _avw
            _ids = _avw.extract_youtube_ids(chat_msg)
            if _ids:
                _video_watcher.submit(_avw.VideoJob(
                    video_id=_ids[0], source="chat", viewer=viewer,
                    message=chat_msg,
                ))

        if paid:
            # D0: Super Chat = DONASI — dijawab TANPA syarat wake word, tanpa
            # cooldown, dan tidak pernah di-drop saat sibuk. Reaksinya DITUNDA
            # sampai alert overlay (audio + baca pesan) selesai — jangan
            # tabrakan suara dengan alert.
            print(f"[SuperChat] {viewer} kirim {paid}!")
            schedule_donation_trigger(
                f"[DONASI Super Chat {paid} dari {viewer}]: "
                f"{chat_msg or '(tanpa pesan — cukup nominal)'}",
                viewer,
                chat_msg,
            )
            return

        wake = is_arti_wake_call(chat_msg)
        # Penonton BARU: pesan pertamanya memicu sapaan TANPA kata "arti".
        # Set-nya milik loop chat ini (bukan _yt_viewers_seen yang dipangkas
        # TTL) supaya "baru" berarti "belum pernah menyapa sesi ini".
        baru = (viewer not in _yt_pernah_chat
                and bool(CONFIG.get("yt_greet_new_viewers", True)))
        _yt_pernah_chat.add(viewer)
        # Chat santai (operator [date removed]): pesan manusiawi tanpa wake word ikut
        # memicu — keran globalnya sendiri dicek di bawah, SETELAH cooldown
        # per-viewer, supaya spammer tidak bisa memakai jalur ini.
        santai = (not wake and not baru
                  and bool(CONFIG.get("yt_chat_santai_enabled", True)))
        if wake or baru or santai:
            current_time = time.time()
            # Cooldown per PENONTON berlaku di SEMUA mode — dulu cuma di mode
            # queue, jadi satu penonton bisa memonopoli slot global 20 dtk
            # dengan spam (live [date removed]).
            cd_viewer = float(CONFIG.get("yt_viewer_cooldown_sec", 45.0))
            last_v = _last_yt_trigger_by_viewer.get(viewer, 0.0)
            if current_time - last_v < cd_viewer:
                print(f"[YT Chat Info] {viewer} kena cooldown pribadi "
                      f"({cd_viewer - (current_time - last_v):.0f}s lagi).")
            elif santai and (current_time - last_chat_trigger_time
                             < float(CONFIG.get("yt_chat_santai_gap_sec", 60.0))):
                # Keran santai belum buka — pesannya sudah tercatat di history,
                # jadi jawaban berikutnya tetap melihatnya sebagai konteks.
                print(f"[YT Chat Info] Chat santai dari {viewer} tertahan keran "
                      "(masuk history saja).")
            elif (not CONFIG.get("voice_queue_enabled", False)
                    and current_time - last_chat_trigger_time < 20):
                print(f"[YT Chat Info] Panggilan dari {viewer} diabaikan (cooling down).")
            else:
                if baru and not wake:
                    label = ("[Pesan PERTAMA dari penonton BARU bernama "
                             f"{viewer} (YouTube) — sapa dia]: {chat_msg}")
                    jenis = "Penonton baru"
                elif santai:
                    # Penonton TIDAK memanggil Arti — beri tahu LLM supaya dia
                    # menimpali obrolan, bukan berlagak dipanggil/ditanya.
                    label = (f"[Pesan Live Chat dari Viewer {viewer} (YouTube) "
                             "— dia ngobrol di chat tanpa menyebut namamu; "
                             f"timpali secara natural]: {chat_msg}")
                    jenis = "Chat santai"
                else:
                    label = f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: {chat_msg}"
                    jenis = "Panggilan"
                print(f"[YT Chat] {jenis} dari {viewer}!")
                diterima = queue_voice_trigger(
                    label, trigger_type="yt_chat", viewer_name=viewer
                )
                if diterima:
                    _last_yt_trigger_by_viewer[viewer] = current_time
                    last_chat_trigger_time = current_time
    
    # === Main Loop ===
    gagal_token = 0
    while _yt_chat_running:
        try:
            continuation, initial_msgs = get_initial_chat()
            if not continuation:
                # Backoff (log [date removed] [time removed]: ID salah ketik saat wizard ->
                # bridge balik ke video KEMARIN yang sudah mati -> "Gagal
                # ambil token" tiap 15 dtk sepanjang sesi, ~40 baris spam).
                gagal_token += 1
                tunggu = 15 if gagal_token < 4 else (60 if gagal_token < 8 else 300)
                if gagal_token == 4:
                    print(
                        "[YouTube Chat] 4x gagal ambil token — kemungkinan stream-nya "
                        "SUDAH BERAKHIR atau Video ID salah "
                        f"(sekarang: {CONFIG.get('youtube_video_id')}). "
                        "Retry diperlambat 60s lalu 5 menit; restart bridge untuk ganti ID."
                    )
                else:
                    print(f"[YouTube Chat] Gagal ambil token ({gagal_token}x). Retry {tunggu} detik...")
                time.sleep(tunggu)
                continue
            gagal_token = 0
            print(f"[YouTube Chat] Terhubung! {len(initial_msgs)} pesan awal ditemukan.")
            for m in initial_msgs[-5:]:
                process_message(m)
            
            # Poll loop
            while _yt_chat_running:
                messages, next_cont, timeout_ms = poll_chat(continuation)
                
                for m in messages:
                    process_message(m)
                
                if next_cont:
                    continuation = next_cont
                else:
                    print("[YouTube Chat] Stream selesai.")
                    return
                
                wait = max(timeout_ms / 1000.0, 3.0)
                time.sleep(wait)
        
        except Exception as e:
            print(f"[YouTube Chat Error] {e}. Retry 10 detik...")
            time.sleep(10)

# Status toggle hotkey: ON = semua omongan langsung ke Arti, OFF = passive mode
hotkey_active = False
hotkey_registered = False
vision_runtime_on = False
vision_hotkey_registered = False
vision_auto_until = 0.0
_asr_mic_id: int | None = None
_asr_mic_name: str = ""
_asr_restart_requested = False
# Telinga (mic ASR + loopback desktop) SESUDAH Ctrl+C. Log [date removed] [time removed]:
# sesudah "SEMUA PROSES SHUTDOWN SELESAI" masih muncul "[ASR] Selesai bicara"
# -> "Groq Cloud Whisper sukses", dan telinga desktop membuka capture lagi.
# Pipeline shutdown (observer + reindex) makan menitan; selama itu KEDUA
# telinga terus menyetor audio ke Groq = kuota operator kebakar untuk sesi yang
# sudah selesai. Flag ini dinyalakan PALING AWAL di blok finally.
_telinga_dimatikan = False


def hentikan_telinga() -> None:
    """Tutup semua telinga SEBELUM pipeline shutdown yang panjang berjalan."""
    global _telinga_dimatikan
    _telinga_dimatikan = True
    CONFIG["desktop_audio_enabled"] = False  # loop worker berhenti sendiri
    print("[Shutdown] Telinga dimatikan (mic + desktop) — berhenti kirim ke Groq.")


def request_asr_stream_restart(reason: str = "") -> None:
    """Bangunkan ulang InputStream ASR setelah sd.stop() atau error."""
    global _asr_restart_requested
    _asr_restart_requested = True
    if reason:
        print(f"[ASR] Restart mic stream ({reason})")

def is_vision_active(config: dict | None = None) -> bool:
    """Master vision_enabled + manual toggle OR scouter auto-window."""
    cfg = config or CONFIG
    if not cfg.get("vision_enabled", cfg.get("screen_context_enabled", False)):
        return False
    if bool(cfg.get("vision_runtime_on", vision_runtime_on)):
        return True
    auto_until = float(cfg.get("vision_auto_until", vision_auto_until))
    return time.time() < auto_until


def _sync_vision_runtime_to_config() -> None:
    CONFIG["vision_runtime_on"] = vision_runtime_on
    CONFIG["vision_auto_until"] = vision_auto_until


def init_vision_hotkey():
    """Toggle vision on/off at runtime (terpisah dari PTT toggle)."""
    global vision_runtime_on, vision_hotkey_registered
    vision_runtime_on = bool(CONFIG.get("vision_runtime_on_start", False))
    _sync_vision_runtime_to_config()
    vision_hotkey_registered = False

    if not CONFIG.get("vision_enabled", CONFIG.get("screen_context_enabled", False)):
        return

    vkey = (CONFIG.get("vision_hotkey_key") or "").strip().lower()
    if not vkey:
        print("[Vision] Tanpa vision_hotkey_key — pakai vision_runtime_on_start saja.")
        return

    if vkey.startswith("mouse_"):
        mouse_button = vkey.replace("mouse_", "").strip()
        print(f"\n👁️ [Vision Hotkey] Tombol mouse '{mouse_button}' = toggle lihat layar.")
        try:
            import mouse
        except ImportError:
            print("[Vision Hotkey] Library 'mouse' tidak ada — skip.")
            return

        def on_vision_toggle():
            global vision_runtime_on
            vision_runtime_on = not vision_runtime_on
            _sync_vision_runtime_to_config()
            if vision_runtime_on:
                print("\n👁️ [Vision ON] Arti boleh lihat layar (on-demand, bukan Groq).")
            else:
                print("\n👁️ [Vision OFF] Layar tidak diproses — hemat quota vision API.")

        try:
            mouse.on_button(on_vision_toggle, buttons=(mouse_button,), types=("down",))
            vision_hotkey_registered = True
            state = "ON" if vision_runtime_on else "OFF"
            print(f"👁️ [Vision Hotkey] Terdaftar. Status awal: {state}")
        except Exception as e:
            print(f"[Vision Hotkey] Gagal: {e}")
    else:
        try:
            import keyboard
        except ImportError:
            print("[Vision Hotkey] Library 'keyboard' tidak ada — skip.")
            return

        def on_vision_kb():
            global vision_runtime_on
            vision_runtime_on = not vision_runtime_on
            _sync_vision_runtime_to_config()
            print(f"\n👁️ [Vision] {'ON' if vision_runtime_on else 'OFF'}")

        try:
            keyboard.add_hotkey(vkey, on_vision_kb)
            vision_hotkey_registered = True
            print(f"👁️ [Vision Hotkey] Keyboard '{vkey}' terdaftar.")
        except Exception as e:
            print(f"[Vision Hotkey] Gagal: {e}")


def init_global_hotkey():
    """Menginisialisasi hotkey global menggunakan 'keyboard' atau 'mouse' dengan instalasi otomatis"""
    global hotkey_active, hotkey_registered
    hotkey_registered = False
    if CONFIG.get("trigger_mode", "wake_word") != "push_to_talk":
        return
        
    hotkey = CONFIG.get("hotkey_key", "ctrl+alt+a").lower()
    
    # --- JALUR MOUSE BUTTONS ---
    if hotkey.startswith("mouse_"):
        mouse_button = hotkey.replace("mouse_", "").strip()
        # Mapping mouse button names:
        # "mouse_x" -> 'x' (Mouse 4 / Back)
        # "mouse_x2" -> 'x2' (Mouse 5 / Forward)
        # "mouse_middle" -> 'middle'
        # "mouse_right" -> 'right'
        # "mouse_left" -> 'left'
        print(f"\n🖱️ [Hotkey] Menginisialisasi pendengar tombol mouse global '{mouse_button}'...")
        
        try:
            import mouse
        except ImportError:
            print("\n[Hotkey Warning] Library 'mouse' belum terinstall. Menginstall otomatis...")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "mouse"], capture_output=True)
            try:
                import mouse
                print("[Hotkey] 'mouse' berhasil terinstall!")
            except ImportError:
                print("[Hotkey Error] Gagal menginstall 'mouse' secara otomatis. Silakan jalankan 'pip install mouse'!")
                return
                
        _toggle_on_time = 0.0  # timestamp ketika toggle ON

        def on_mouse_click():
            global hotkey_active, current_api_task, _toggle_on_time
            now = time.time()
            hotkey_active = not hotkey_active
            if hotkey_active:
                _toggle_on_time = now
                print("\n🔴 [Toggle ON] Arti mendengarkan! Tekan lagi buat matiin.")
                print(
                    "[PTT] Expect: ngomong -> [ASR] Mendengar suara -> "
                    "[Toggle ON] Hasil -> [Groq API]"
                )
                _start_mic_watch_once(
                    _asr_mic_id,
                    _asr_mic_name or "?",
                    float(CONFIG.get("health_mic_watch_sec", 5.0)),
                    "mouse",
                )
                clear_trigger_queue()
                _ptt_attention_pause()
            else:
                elapsed = now - _toggle_on_time
                if elapsed < 2.0:
                    # Double-toggle dalam < 2 detik = force bungkam
                    print(f"\n⚫⚫ [DOUBLE TOGGLE] Bungkam! ({elapsed:.1f}s)")
                else:
                    print("\n⚫ [Toggle OFF] Cancel API call + stop TTS.")
                try:
                    _loop = main_event_loop
                    if _loop and not _loop.is_closed():
                        asyncio.run_coroutine_threadsafe(vts.trigger_expression_state("default"), _loop)
                except Exception:
                    pass
                _cancel_lamp_fallback()
                start_idle_animation()
                if current_api_task and not current_api_task.done():
                    current_api_task.cancel()
                    print("[Cancel] API call dibatalkan.")
                if tts_is_playing:
                    try:
                        with _sd_gerbang:
                            sd.stop()
                        print("[Cancel] TTS dihentikan.")
                    except Exception:
                        pass
                    request_asr_stream_restart("toggle OFF + stop TTS")
                clear_trigger_queue()

        try:
            mouse.on_button(on_mouse_click, buttons=(mouse_button,), types=('down',))
            hotkey_registered = True
            print(f"🖱️ [Hotkey] Pendaftaran mouse button '{mouse_button}' SUKSES! Tekan tombol tersebut untuk berbicara.")
        except Exception as e:
            print(f"[Hotkey Error] Gagal mendaftarkan tombol mouse: {e}")
            
    # --- JALUR KEYBOARD KEYS ---
    else:
        print(f"\n⌨️ [Hotkey] Menginisialisasi pendengar hotkey keyboard global '{hotkey}'...")
        try:
            import keyboard
        except ImportError:
            print("\n[Hotkey Warning] Library 'keyboard' belum terinstall. Menginstall otomatis...")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "keyboard"], capture_output=True)
            try:
                import keyboard
                print("[Hotkey] 'keyboard' berhasil terinstall!")
            except ImportError:
                print("[Hotkey Error] Gagal menginstall 'keyboard' secara otomatis. Silakan jalankan 'pip install keyboard'!")
                return
                
        _toggle_on_time_kb = 0.0

        def on_hotkey_pressed():
            global hotkey_active, current_api_task, _toggle_on_time_kb
            now = time.time()
            hotkey_active = not hotkey_active
            if hotkey_active:
                _toggle_on_time_kb = now
                print("\n🔴 [Toggle ON] Arti mendengarkan! Tekan lagi buat matiin.")
                print(
                    "[PTT] Expect: ngomong -> [ASR] Mendengar suara -> "
                    "[Toggle ON] Hasil -> [Groq API]"
                )
                _start_mic_watch_once(
                    _asr_mic_id,
                    _asr_mic_name or "?",
                    float(CONFIG.get("health_mic_watch_sec", 5.0)),
                    "keyboard",
                )
                clear_trigger_queue()
                _ptt_attention_pause()
            else:
                elapsed = now - _toggle_on_time_kb
                if elapsed < 2.0:
                    print(f"\n⚫⚫ [DOUBLE TOGGLE] Bungkam! ({elapsed:.1f}s)")
                else:
                    print("\n⚫ [Toggle OFF] Cancel API call + stop TTS.")
                try:
                    _loop = main_event_loop
                    if _loop and not _loop.is_closed():
                        asyncio.run_coroutine_threadsafe(vts.trigger_expression_state("default"), _loop)
                except Exception:
                    pass
                _cancel_lamp_fallback()
                start_idle_animation()
                if current_api_task and not current_api_task.done():
                    current_api_task.cancel()
                    print("[Cancel] API call dibatalkan.")
                if tts_is_playing:
                    try:
                        with _sd_gerbang:
                            sd.stop()
                        print("[Cancel] TTS dihentikan.")
                    except Exception:
                        pass
                    request_asr_stream_restart("toggle OFF + stop TTS")
                clear_trigger_queue()
                
        try:
            keyboard.add_hotkey(hotkey, on_hotkey_pressed)
            hotkey_registered = True
            print(f"⌨️ [Hotkey] Pendaftaran keyboard key '{hotkey}' SUKSES! Tekan tombol tersebut untuk berbicara.")
        except Exception as e:
            print(f"[Hotkey Error] Gagal mendaftarkan hotkey keyboard: {e}")

def text_input_worker():
    """Trigger via ketikan di console — untuk kondisi tanpa mic (AFK/remote).

    Ketik pesan + Enter di window bridge:
      halo arti apa kabar        → dijawab seperti omongan streamer (jalur PTT)
      yt arti kamu nyala?        → simulasi chat YT dari handle default
                                   (CONFIG["yt_default_viewer"], mis. @streamer_test)
      yt @seseorang: pesan       → simulasi chat YT dari viewer tertentu
    """
    global _media_playback_until, _desktop_listen_enabled
    default_viewer = (CONFIG.get("yt_default_viewer") or "").strip()
    hint = f" (default: {default_viewer})" if default_viewer else ""
    print(
        "[TextInput] Aktif — ketik pesan + Enter untuk manggil Arti. "
        f"'yt pesan' / 'yt Nama: pesan' = simulasi chat YT{hint}"
    )
    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if line == "":
            # EOF — stdin ketutup (jalan tanpa console); worker berhenti diam-diam
            return
        # Catatan: semua prompt input() bridge (wizard + save config) jalan
        # SEBELUM main_loop start, jadi worker ini tidak pernah rebutan stdin.
        text = line.strip()
        if not text:
            continue
        # Komando video (Fitur E): veto & antre manual dari console.
        low = text.lower()
        if low in ("video off", "video on", "video skip") or low.startswith("video "):
            if _video_watcher is None:
                print("[Video] Watcher tidak aktif (video_enabled=False)")
                continue
            if low == "video off":
                _video_watcher.runtime_enabled = False
                _media_playback_until = 0.0  # bebaskan hold yang sedang aktif
                print("[Video] Runtime OFF — media share/link diabaikan sampai 'video on'")
            elif low == "video on":
                _video_watcher.runtime_enabled = True
                print("[Video] Runtime ON")
            elif low == "video skip":
                _media_playback_until = 0.0
                _video_watcher.skip_current()
            else:
                import arti_video_watcher as _avw
                ids = _avw.extract_youtube_ids(text)
                if ids:
                    _video_watcher.submit(_avw.VideoJob(video_id=ids[0], source="console"))
                else:
                    print("[Video] Tidak ada URL YouTube di perintah itu")
            continue
        # Komando telinga desktop audio: toggle runtime + intip isi ring.
        if low in ("dengar on", "dengar off", "dengar status"):
            if not CONFIG.get("desktop_audio_enabled"):
                print("[Desktop Audio] desktop_audio_enabled=False — nyalakan di config_local dulu")
                continue
            if low == "dengar on":
                _desktop_listen_enabled = True
                print("[Desktop Audio] Telinga ON")
            elif low == "dengar off":
                _desktop_listen_enabled = False
                print("[Desktop Audio] Telinga OFF (worker idle sampai 'dengar on')")
            else:
                entries = arti_desktop_audio.dialogue_ring.snapshot()
                _pool_n = len(_desktop_groq_keys())
                print(
                    f"[Desktop Audio] listening={_desktop_listen_enabled} "
                    f"backend={f'groq-pool({_pool_n})' if _pool_n else 'lokal'} "
                    f"ring={len(entries)} baris"
                )
                for e in entries[-3:]:
                    umur = int(time.time() - e.wall_ts)
                    print(f"  [{umur}s lalu] {e.text[:90]}")
            continue
        # Komando mode sesi: host on/off/status (Arti pegang siaran).
        if low == "host" or low.startswith("host "):
            if not CONFIG.get("host_mode_enabled", True):
                print("[Host] host_mode_enabled=False — nyalakan di config_local dulu")
                continue
            arg = text[4:].strip().lower()
            if arg == "on":
                _set_host_mode(True, "console")
            elif arg == "off":
                _set_host_mode(False, "console")
            else:
                mode = _session_mode()
                label = arti_session_mode.mode_policy(mode, CONFIG)["label"]
                print(f"[Host] {'ON' if _host_mode else 'OFF'} — mode: {label}")
            continue
        # Komando Minecraft: mc on/off/status + verb manual (mc say halo, dst).
        if low == "mc" or low.startswith("mc "):
            if not CONFIG.get("minecraft_enabled"):
                print("[Minecraft] minecraft_enabled=False — nyalakan di config_local dulu")
                continue
            rest = text[2:].strip()
            rlow = rest.lower()
            if rlow == "on":
                _start_minecraft_runner()
            elif rlow == "off":
                _stop_minecraft_runner()
            elif rlow.startswith("goal"):
                arg = rest[4:].strip()
                if arg.lower() in ("clear", "off", "batal"):
                    _set_minecraft_goal("")
                elif arg:
                    _set_minecraft_goal(arg)
                else:
                    print(f"[Minecraft] Misi sekarang: {_minecraft_goal or '(bebas)'}")
            elif rlow in ("status", ""):
                state = (
                    _minecraft_runner.status_line()
                    if _minecraft_runner is not None else "belum pernah join"
                )
                print(f"[Minecraft] {state} | misi: {_minecraft_goal or '(bebas)'}")
            else:
                # Validator yang sama dengan tag LLM — console tidak dapat
                # jalan pintas melewati whitelist/allowlist.
                _, cmds = arti_minecraft.parse_mc_tags(f"[MC: {rest}]", CONFIG)
                if cmds:
                    _execute_mc_tag(cmds[0])
                else:
                    print(f"[Minecraft] Perintah tidak dikenal/valid: {rest!r}")
            continue
        m = re.match(r"^yt\s+([^:\s]{1,30}):\s*(.+)$", text, re.IGNORECASE)
        m_plain = re.match(r"^yt\s+(.+)$", text, re.IGNORECASE) if not m else None
        if m or m_plain:
            if m:
                viewer, msg = m.group(1).strip(), m.group(2).strip()
            else:
                viewer = default_viewer or "viewer"
                msg = m_plain.group(1).strip()
            print(f"[TextInput] Simulasi YT chat dari {viewer}: {msg}")
            add_to_history(f"Viewer {viewer} (YouTube)", msg)
            queue_voice_trigger(
                f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: {msg}",
                trigger_type="yt_chat",
                viewer_name=viewer,
            )
        else:
            print(f"[TextInput] Pesan streamer (ketik): {text}")
            add_to_history("Streamer", text)
            queue_voice_trigger(text, trigger_type="mic")


def start_text_input_worker():
    if not CONFIG.get("text_input_enabled", True):
        return
    threading.Thread(target=text_input_worker, daemon=True, name="text-input").start()


# Runtime toggle telinga (console `dengar on/off`) — terpisah dari kill switch
# CONFIG supaya bisa dimatikan sebentar tanpa restart. Init True (nilai stabil
# untuk snapshot konstanta); worker-nya sendiri hanya hidup bila
# desktop_audio_enabled ON di config.
_desktop_listen_enabled = True


def _desktop_groq_keys() -> list[str]:
    """Pool kunci Groq KHUSUS telinga: semua env GROQ_API_KEY_<apapun>.

    Kunci utama GROQ_API_KEY (tanpa underscore ekor) SENGAJA dikecualikan —
    itu jatah ASR mic. streamer nambah akun: tinggal tambah GROQ_API_KEY_xxx di
    .env, pool otomatis membesar (2026-08-03: _bo, _g, _g2 = 3 kunci = 12K
    request whisper/hari khusus telinga)."""
    seen: list[str] = []
    for name in sorted(os.environ):
        if not name.startswith("GROQ_API_KEY_"):
            continue
        val = (os.environ.get(name) or "").strip()
        if val and val.startswith("gsk_") and val not in seen:
            seen.append(val)
    return seen


def _desktop_transcribe(audio_np):
    """Transkrip chunk desktop: rotasi round-robin pool kunci telinga ->
    fallback whisper lokal (di dalam transcribe_audio). language=None =
    auto-detect (klip sering Inggris). Rotasi menyebar kuota rata; kunci
    yang kena 429 cuma mengorbankan satu chunk (jatuh ke lokal), chunk
    berikutnya sudah pindah kunci."""
    keys = _desktop_groq_keys()
    if not keys:
        return transcribe_audio(
            audio_np, 16000, use_groq=False, language=None, quiet=True
        )
    i = getattr(_desktop_transcribe, "_kidx", 0)
    _desktop_transcribe._kidx = i + 1
    return transcribe_audio(
        audio_np, 16000, use_groq=True, api_key=keys[i % len(keys)],
        language=None, quiet=True,
    )


def start_desktop_audio_worker():
    if not CONFIG.get("desktop_audio_enabled"):
        return

    _pool = _desktop_groq_keys()
    key_note = (
        f"groq pool {len(_pool)} kunci (rotasi, ~{len(_pool) * 4}K req/hari)"
        if _pool
        else "whisper LOKAL (tidak ada GROQ_API_KEY_* tambahan di .env)"
    )
    print(f"[Desktop Audio] Telinga dinyalakan — backend: {key_note}")

    def _run():
        arti_desktop_audio.desktop_audio_worker(
            CONFIG,
            # Refleks ikut dihitung "Arti lagi bersuara" — kalau tidak,
            # teriakannya sendiri masuk ke telinga dan jadi konteks palsu.
            get_tts_is_playing=lambda: tts_is_playing or _reflex_playing,
            get_last_tts_end=lambda: getattr(
                voice_listener_worker, "_last_tts_end", None
            ),
            is_echo_of_arti=is_asr_echo_of_arti,
            record_chunk=arti_desktop_audio.make_loopback_record_chunk(CONFIG),
            transcribe_chunk=_desktop_transcribe,
            filter_text=lambda t: filter_whisper_hallucination(
                t, is_passive_monitoring=True
            ),
            is_listening=lambda: _desktop_listen_enabled,
            is_speech=(
                (lambda a: arti_vad.ada_ucapan(
                    a, threshold=CONFIG.get("desktop_audio_vad_threshold")
                ))
                if CONFIG.get("desktop_audio_vad_enabled", True)
                else None
            ),
        )

    threading.Thread(target=_run, daemon=True, name="desktop-audio").start()


def start_screen_watcher_worker():
    if not is_vision_active():
        return
    if not CONFIG.get("vision_background_poll", False):
        print("[Vision] Background poll OFF — describe on-demand saat toggle + trigger.")
        return

    def _run():
        arti_screen_context.screen_watcher_worker(
            CONFIG,
            capture_and_describe=arti_vision_client.make_watcher_fn(CONFIG),
        )

    threading.Thread(target=_run, daemon=True, name="screen-watcher").start()


def refresh_vision_for_turn(user_speech: str = "") -> None:
    """On-demand screenshot describe (bukan Groq) sebelum jawaban.

    Kalau omongan turn INI menyinggung layar ("layar", "screen", "lihat", ...)
    tapi jendela vision belum terbuka, buka dulu — jangan tunggu timer scouter.
    Terbukti di sesi live 2026-08-01: streamer tanya "yang lagi ada di layar aku apa"
    SEBELUM scouter sempat membuka jendela, jadi Arti menjawab tanpa data dan
    mengarang ("aku lagi nonton video YouTube"). Scouter baru membuka jendelanya
    SETELAH pertanyaan lewat. Mekanisme bukanya sama persis dengan scouter
    (vision_auto_until), cuma pemicunya keyword turn ini — dan tetap di belakang
    master switch vision_enabled.
    """
    global vision_auto_until
    if not is_vision_active():
        import arti_scouter_client  # import lokal — bridge tidak meng-importnya di level modul

        if (
            user_speech
            and CONFIG.get("vision_enabled", CONFIG.get("screen_context_enabled", False))
            and arti_scouter_client.has_screen_keywords(user_speech)
        ):
            sec = float(CONFIG.get("scouter_auto_vision_sec", 60))
            vision_auto_until = max(vision_auto_until, time.time() + sec)
            _sync_vision_runtime_to_config()
            print(f"[Vision] Dibuka karena ditanya soal layar (~{int(sec)}s)")
        else:
            return
    try:
        snap, provider = arti_vision_client.refresh_if_stale(_scouter_config())
        if snap and snap.scene:
            arti_screen_context.update_watch_state_from_snapshot(
                snap,
                event_id=str(CONFIG.get("watch_party_event_id") or ""),
            )
            print(f"[Vision] Turn refresh via {provider}: {snap.scene[:50]}...")
    except Exception as e:
        print(f"[Vision] Turn refresh skip: {type(e).__name__}: {e}")


def voice_listener_worker():
    """Mendengarkan mic secara real-time dengan Auto Noise Gate & cerdas mendeteksi panggilan nama A"""
    global hotkey_active
    print("[ASR] Pendengar mic aktif (Menggunakan Groq Cloud Whisper dengan local fallback)...")
    
    samplerate = 16000
    channels = 1
    
    audio_queue = queue.Queue()
    
    def audio_callback(indata, frames, time, status):
        # Hanya rekam suara jika Arti sedang tidak berbicara — termasuk saat
        # dia melepas refleks ("Aduh!"). Refleks TIDAK memakai tts_is_playing
        # (itu akan membuang trigger reaksi untuk event yang sama), jadi
        # flag-nya sendiri harus ikut dicek di sini.
        if not tts_is_playing and not _reflex_playing:
            audio_queue.put(indata.copy())

    global _asr_mic_id, _asr_mic_name
    mic_id, mic_name = resolve_asr_input_device()
    _asr_mic_id, _asr_mic_name = mic_id, mic_name
    print(f"[ASR] Menggunakan microphone: {mic_name}" + (f" (device {mic_id})" if mic_id is not None else ""))
    if "stereo mix" in mic_name.lower():
        print(
            "[ASR ERROR] Masih Stereo Mix — suara kamu nggak ke-detect! "
            "Set asr_input_device di CONFIG atau ganti default mic di Windows."
        )

    stream_kw = {"samplerate": samplerate, "channels": channels, "callback": audio_callback}
    if mic_id is not None:
        stream_kw["device"] = mic_id
        # Device tersimpan bisa BASI: live_session.json menyimpan nomor device
        # dari sesi lalu, dan nomor itu bergeser kalau perangkat audio berubah
        # (pindah tempat, ganti headset). Live [date removed] malam: device 3 sudah
        # tidak valid, `sd.InputStream` melempar PortAudioError -9996, thread
        # ASR MATI DIAM-DIAM — operator terpaksa mengetik sepanjang sesi tanpa
        # tahu kenapa. Sekarang: coba device tersimpan, kalau gagal jatuh ke
        # default Windows dan LAPOR keras.
        try:
            sd.check_input_settings(
                device=mic_id, samplerate=samplerate, channels=channels
            )
        except Exception as e:  # noqa: BLE001
            print(
                f"\n[ASR ERROR] Mic device {mic_id} ({mic_name}) TIDAK BISA "
                f"dipakai ({type(e).__name__}). Kemungkinan perangkat audio "
                f"berubah sejak sesi terakhir.\n"
                f"[ASR] Jatuh ke mic default Windows. Kalau salah, set "
                f"asr_input_device di config_local.json.\n"
            )
            stream_kw.pop("device", None)
            mic_id, mic_name = None, "default Windows"
            _asr_mic_id, _asr_mic_name = None, mic_name

    # --- AUTO NOISE CALIBRATION (2 DETIK) ---
    print("\n[ASR] 🤫 HARAP DIAM... Sedang mengkalibrasi tingkat kebisingan ruanganmu selama 2 detik...")
    calibration_data = []

    with sd.InputStream(**stream_kw):
        start_cal = time.time()
        while time.time() - start_cal < 2.0:
            try:
                chunk = audio_queue.get(timeout=0.1)
                calibration_data.extend(chunk.flatten())
            except queue.Empty:
                continue
                
    # Hitung batas kebisingan rata-rata (RMS) ruangan
    if calibration_data:
        rms_noise = np.sqrt(np.mean(np.array(calibration_data)**2))
        # Terapkan threshold = kebisingan ruangan + buffer aman
        cap = float(CONFIG.get("asr_silence_threshold_max", 0.12))
        silence_threshold = min(cap, max(0.04, rms_noise * 2.0))
        print(
            f"[ASR] Kalibrasi Selesai! Threshold VAD: {silence_threshold:.4f}"
            + (f" (cap {cap})" if rms_noise * 2.0 > cap else "")
        )
    else:
        silence_threshold = 0.05
        print(f"[ASR] Gagal kalibrasi, menggunakan threshold default: {silence_threshold}")
        
    # Kosongkan queue sisa kalibrasi
    while not audio_queue.empty():
        audio_queue.get()
        
    print("\n🟢 [ASR] Microphone aktif mendengarkan... Panggil A dengan 'eh a' atau 'eh ah'!")
    
    global _asr_restart_requested, _asr_ptt_cooldown_until
    while True:
        if _telinga_dimatikan:
            print("[ASR] Pendengar mic berhenti (shutdown).")
            return
        _asr_restart_requested = False
        while not audio_queue.empty():
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                break

        with sd.InputStream(**stream_kw):
            recording = []
            is_speaking = False
            silence_duration = 0
            # Endpoint adaptif: satu percobaan spekulatif per ucapan.
            spek_dikirim = False
            spek_hasil = {}
            # Dipakai jam VAD di bawah; selalu ditulis ulang tiap potongan
            # bersuara, jadi nilai awal ini cuma penjaga kalau stream mulai sunyi.
            last_voice_at = time.perf_counter()
            stream_dead = False

            while True:
                if _asr_restart_requested or _telinga_dimatikan:
                    stream_dead = True
                    break
                try:
                    data = audio_queue.get(timeout=0.1)
                    audio_chunk = data.flatten()

                    rms = np.sqrt(np.mean(audio_chunk**2))

                    if hotkey_active and time.time() < _asr_ptt_cooldown_until:
                        continue

                    if rms > silence_threshold:
                        if not is_speaking:
                            print("[ASR] Mendengar suara...")
                            is_speaking = True
                        recording.extend(audio_chunk)
                        silence_duration = 0
                        last_voice_at = time.perf_counter()
                        # operator lanjut bicara -> tebakan spekulatif jadi basi.
                        spek_dikirim = False
                        spek_hasil = {}
                    else:
                        if is_speaking:
                            # JAM SUNGGUHAN, bukan hitungan potongan.
                            # Sampai [date removed] baris ini menambah 0,1 per
                            # potongan audio — mengasumsikan tiap potongan 100 ms.
                            # `sd.InputStream` dibuat TANPA blocksize, jadi ukuran
                            # potongan ditentukan driver mic (~20 ms di mesin operator)
                            # — jam itu berlari ~5x lebih cepat dari waktu nyata:
                            # config "10 detik" cuma ~2 detik di dunia nyata.
                            # Terbukti dari log [date removed]: 112 ucapan, durasi rekaman
                            # TERPENDEK 2,7 dtk padahal rekaman sudah termasuk ekor
                            # sunyi — mustahil kalau ekornya benar 10 detik.
                            # Efek sampingnya paling jahat: panjang jeda ikut
                            # berubah diam-diam kalau operator ganti mic/driver.
                            silence_duration = time.perf_counter() - last_voice_at
                            recording.extend(audio_chunk)

                            # Diam selama silence_tail = selesai bicara (PTT lebih sabar)
                            trigger_mode = CONFIG.get("trigger_mode", "wake_word").lower()
                            if trigger_mode == "push_to_talk":
                                # Ekor 5 dtk hanya perlu saat TOGGLE ON (nunggu
                                # operator selesai mikir). Jalur PASIF cuma mencatat
                                # — dengan ekor yang sama, monolognya baru masuk
                                # history sebagai bongkahan 3-9 kalimat SETELAH
                                # 5 dtk hening, dan Arti menanggapi barang basi
                                # (keluhan operator [date removed]: "jadi rada delay").
                                # Pasif dapat ekor pendek sendiri: potongan
                                # seukuran kalimat, history lebih segar.
                                if hotkey_active:
                                    silence_tail = float(
                                        CONFIG.get("asr_ptt_silence_tail_sec", 4.0)
                                    )
                                else:
                                    silence_tail = float(
                                        CONFIG.get("asr_pasif_silence_tail_sec", 2.0)
                                    )
                            else:
                                silence_tail = float(CONFIG.get("asr_silence_tail_sec", 2.0))
                            # --- ENDPOINT ADAPTIF (PTT saja) ---
                            spek_teks = None
                            if (
                                CONFIG.get("asr_ptt_adaptif_enabled", False)
                                and trigger_mode == "push_to_talk"
                                and hotkey_active
                                and silence_tail > 0
                            ):
                                ekor_cepat = float(
                                    CONFIG.get("asr_ptt_ekor_cepat_sec", 1.0))
                                ekor_aman = float(
                                    CONFIG.get("asr_ptt_ekor_aman_sec", 1.8))
                                if not spek_dikirim and silence_duration >= ekor_cepat:
                                    spek_dikirim = True
                                    _potret = np.array(recording, dtype=np.float32)
                                    _kotak = spek_hasil

                                    def _spek_worker(arr=_potret, kotak=_kotak):
                                        try:
                                            kotak["teks"] = transcribe_audio(
                                                arr, samplerate, use_groq=True,
                                                quiet=True)
                                        except Exception as e:  # noqa: BLE001
                                            kotak["teks"] = None
                                            kotak["error"] = repr(e)
                                        finally:
                                            kotak["selesai"] = True

                                    threading.Thread(
                                        target=_spek_worker, daemon=True,
                                        name="asr-spekulatif").start()

                                if (spek_hasil.get("selesai")
                                        and silence_duration >= ekor_aman):
                                    _t = spek_hasil.get("teks")
                                    if arti_endpoint.ucapan_terdengar_selesai(_t):
                                        spek_teks = _t
                                        # Sesi live [date removed] mencetak "hemat
                                        # ~-8.1s" — angka omong kosong. Itu
                                        # terjadi kalau PTT ditekan SAAT Arti
                                        # masih bicara: mic tertahan, senyap
                                        # sudah 13 dtk padahal ekor aman cuma
                                        # 5. Tidak ada yang dihemat di situ.
                                        # Angka bohong di log lebih berbahaya
                                        # daripada tidak ada angka.
                                        _hemat = silence_tail - silence_duration
                                        if _hemat > 0.05:
                                            print(
                                                "[ASR] Endpoint adaptif: kalimat utuh, "
                                                f"jalan di {silence_duration:.1f}s "
                                                f"(hemat ~{_hemat:.1f}s)")
                                        else:
                                            print(
                                                "[ASR] Endpoint adaptif: kalimat utuh, "
                                                f"jalan di {silence_duration:.1f}s "
                                                "(ekor penuh sudah lewat — nol hemat)")
                                        # min(), bukan penugasan langsung:
                                        # kalau durasinya sudah melewati ekor,
                                        # menugaskannya malah MEMPERPANJANG
                                        # batas tunggu.
                                        silence_tail = min(silence_tail, silence_duration)
                                    elif not spek_hasil.get("dilaporkan"):
                                        spek_hasil["dilaporkan"] = True
                                        print(
                                            "[ASR] Endpoint adaptif: menggantung ("
                                            + arti_endpoint.alasan_belum_selesai(_t)
                                            + ") — sabar sampai ekor penuh")

                            if silence_duration >= silence_tail:
                                spek_dikirim = False
                                audio_array = np.array(recording, dtype=np.float32)
                                audio_dur = len(audio_array) / float(samplerate)
                                print(
                                    f"[ASR] Selesai bicara ({audio_dur:.1f}s audio, "
                                    f"vad_tail={silence_tail:.0f}s). Mentranskrip..."
                                )

                                # Push-to-talk: langsung transkrip, nggak perlu Groq check
                                if trigger_mode == "push_to_talk":
                                    # Echo suppress: kalau < 3 detik setelah TTS selesai,
                                    # skip ASR — mic masih ke-detect echo speaker
                                    if hasattr(voice_listener_worker, '_last_tts_end'):
                                        elapsed_since_tts = time.time() - voice_listener_worker._last_tts_end
                                        if elapsed_since_tts < 3.0:
                                            print(f"[ASR] Echo suppress ({elapsed_since_tts:.1f}s < 3s), skip.")
                                            recording.clear()
                                            is_speaking = False
                                            silence_duration = 0
                                            continue
                                    if hotkey_active:
                                        with _brain_busy_lock:
                                            if _brain_busy or tts_is_playing:
                                                print(
                                                    "[ASR] Skip transcribe — Arti masih proses jawaban/TTS"
                                                )
                                                recording.clear()
                                                is_speaking = False
                                                silence_duration = 0
                                                continue
                                        vad_tail_ms = int(silence_duration * 1000)
                                        asr_t0 = time.perf_counter()
                                        # Endpoint adaptif sudah punya transkripnya;
                                        # jangan bayar ASR dua kali. Yang tidak ikut
                                        # cuma ekor sunyi di belakang.
                                        if spek_teks is not None:
                                            text = spek_teks
                                        else:
                                            text = transcribe_audio(audio_array, samplerate, use_groq=True)
                                        asr_ms = int((time.perf_counter() - asr_t0) * 1000)
                                        asr_stages = {"vad_tail_ms": vad_tail_ms, "asr_ms": asr_ms}
                                        if text:
                                            text = filter_whisper_hallucination(text, is_passive_monitoring=False)
                                            if text and is_asr_noise_transcript(text, audio_dur):
                                                print(f"[ASR Noise Filter] Skip noise (PTT): \"{text}\"")
                                                _asr_ptt_cooldown_until = time.time() + 2.5
                                                recording.clear()
                                                is_speaking = False
                                                silence_duration = 0
                                                continue
                                            if text and is_asr_echo_of_arti(text):
                                                print(f"[ASR Echo Filter] Skip echo (PTT): \"{text}\"")
                                                recording.clear()
                                                is_speaking = False
                                                silence_duration = 0
                                                continue
                                            if text:
                                                print(f"🎤 [Toggle ON] Hasil: \"{text}\"")
                                                # Cek Arti lagi bicara nggak
                                                if tts_is_playing:
                                                    print(f"[ASR] Arti masih bicara, antri: \"{text}\"")
                                                    add_to_history("Streamer", text)
                                                else:
                                                    queue_voice_trigger(
                                                        text,
                                                        trigger_type="ptt",
                                                        asr_stages=asr_stages,
                                                    )
                                                    summarizer_queue.put(text)
                                    else:
                                        # OFF: catat ke history aja
                                        text = transcribe_audio(audio_array, samplerate, use_groq=True)
                                        if text:
                                            text = filter_whisper_hallucination(text, is_passive_monitoring=True)
                                            if text:
                                                if is_asr_noise_transcript(text, audio_dur):
                                                    print(f"[ASR Noise Filter] Skip noise: \"{text}\"")
                                                    recording.clear()
                                                    is_speaking = False
                                                    silence_duration = 0
                                                    continue
                                                if is_asr_echo_of_arti(text):
                                                    print(f"[ASR Echo Filter] Skip echo: \"{text}\"")
                                                    recording.clear()
                                                    is_speaking = False
                                                    silence_duration = 0
                                                    continue
                                                add_to_history("Streamer", text)
                                                print(f"[ASR Info] Pasif: \"{text}\"")
                                else:
                                    # Wake word mode: cek keyword dulu
                                    vad_tail_ms = int(silence_duration * 1000)
                                    asr_t0 = time.perf_counter()
                                    use_groq = True
                                    text = transcribe_audio(audio_array, samplerate, use_groq=use_groq)
                                    asr_ms = int((time.perf_counter() - asr_t0) * 1000)
                                    asr_stages = {"vad_tail_ms": vad_tail_ms, "asr_ms": asr_ms}
                                    if text:
                                        text = filter_whisper_hallucination(text, is_passive_monitoring=True)
                                        if text:
                                            print(f"[ASR] Hasil: \"{text}\"")
                                            if is_arti_wake_call(text):
                                                # Audit ronde-3: cabang wake dulu SATU-SATUNYA
                                                # jalur ASR tanpa filter echo — ekor TTS yang
                                                # menyebut "Arti" bisa jadi trigger + tanda
                                                # kehidupan palsu (dormansi mundur terus).
                                                if is_asr_echo_of_arti(text):
                                                    print("[ASR] Echo suara Arti — diabaikan (wake)")
                                                else:
                                                    print(f"🎉 WAKE WORD TERDETEKSI!")
                                                    queue_voice_trigger(
                                                        text,
                                                        trigger_type="wake_word",
                                                        asr_stages=asr_stages,
                                                    )
                                                    summarizer_queue.put(text)
                                            else:
                                                add_to_history("Streamer", text)
                                                print(
                                                    "[ASR Info] Diabaikan (tidak memanggil Arti, "
                                                    "tapi dicatat ke sejarah stream)."
                                                )
                            
                                # Reset buffer
                                recording = []
                                is_speaking = False
                                silence_duration = 0
                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"[ASR Error] Kesalahan perekaman mic: {e}")
                    stream_dead = True
                    break

        if stream_dead or _asr_restart_requested:
            time.sleep(0.3)
            continue
        time.sleep(0.5)


async def generate_live_api_response(prompt_content, system_prompt):
    """Mengirim pesan ke Gemini Multimodal Live API menggunakan WebSockets untuk memotong RPD limit (Unlimited RPD!)"""
    api_key = CONFIG["gemini_api_key"]
    model_name = CONFIG["gemini_model"]
    
    # --- AUTO-REMAPPING UNTUK MODEL LIVE API WEBSOCKETS ---
    model_lower = model_name.lower()
    if "2.5-flash" in model_lower and "native" not in model_lower:
        print("[Gemini Live] Auto-remapping gemini-2.5-flash -> gemini-2.5-flash-native-audio-latest untuk Live API...")
        model_name = "gemini-2.5-flash-native-audio-latest"
    elif "3.5-flash" in model_lower or "3-flash" in model_lower or "3.1-flash" in model_lower:
        print("[Gemini Live] Auto-remapping -> gemini-3.1-flash-live-preview untuk Live API...")
        model_name = "gemini-3.1-flash-live-preview"
    elif "2.0-flash" in model_lower and "exp" not in model_lower and "realtime" not in model_lower:
        print("[Gemini Live] Auto-remapping -> gemini-2.0-flash-exp untuk Live API...")
        model_name = "gemini-2.0-flash-exp"
        
    # Format model name agar selalu menggunakan prefix models/
    if not model_name.startswith("models/"):
        model_name = f"models/{model_name}"
        
    # Endpoint Live API WebSockets (v1beta untuk keandalan fitur)
    uri = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={api_key}"
    
    try:
        async with websockets.connect(uri) as ws:
            # 1. Kirim setup message untuk inisialisasi session
            setup_msg = {
                "setup": {
                    "model": model_name,
                    "generation_config": {
                        "response_modalities": ["AUDIO"],
                        "max_output_tokens": 200,
                        "temperature": 1.0
                    },
                    "system_instruction": {
                        "parts": [{"text": system_prompt}]
                    },
                    "output_audio_transcription": {}
                }
            }
            await ws.send(json.dumps(setup_msg))
            
            # 2. Tunggu respon konfirmasi setupComplete dari server
            setup_response = await ws.recv()
            res_data = json.loads(setup_response)
            if "setupComplete" not in res_data:
                raise Exception(f"Setup Live API gagal: {res_data}")
                
            # 3. Kirim content turn dari user
            client_msg = {
                "clientContent": {
                    "turns": [
                        {
                            "role": "user",
                            "parts": [{"text": prompt_content}]
                        }
                    ],
                    "turnComplete": True
                }
            }
            await ws.send(json.dumps(client_msg))
            
            # 4. Kumpulkan hasil streaming teks jawaban dari model
            ai_reply_parts = []
            while True:
                response = await ws.recv()
                res_data = json.loads(response)
                
                # Check for outputAudioTranscription chunks
                if "outputAudioTranscription" in res_data:
                    trans_text = res_data["outputAudioTranscription"].get("text")
                    if trans_text:
                        ai_reply_parts.append(trans_text)
                
                if "serverContent" in res_data:
                    server_content = res_data["serverContent"]
                    
                    if "modelTurn" in server_content:
                        model_turn = server_content["modelTurn"]
                        if "parts" in model_turn:
                            for part in model_turn["parts"]:
                                if "text" in part:
                                    ai_reply_parts.append(part["text"])
                                    
                    if server_content.get("turnComplete") or server_content.get("interrupted"):
                        break
                        
            full_reply = "".join(ai_reply_parts).strip()
            return full_reply
    except Exception as e:
        raise Exception(f"Kesalahan koneksi WebSocket Live API: {e}")

# ==========================================
# 4. DYNAMIC SOUL / MOOD / VIEWER CONTEXT
# ==========================================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_soul_context():
    """Baca ARTI_SOUL.md dan return sebagai string untuk inject ke prompt.
    File ini bisa di-edit runtime — changes langsung生效 tanpa restart."""
    soul_path = os.path.join(_SCRIPT_DIR, "ARTI_SOUL.md")
    if not os.path.exists(soul_path):
        return ""
    try:
        with open(soul_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Extract bagian yang relevan (skip comments dan header)
        sections = []
        current_section = []
        for line in content.split("\n"):
            if line.startswith("## "):
                if current_section:
                    sections.append("\n".join(current_section))
                current_section = [line]
            elif line.strip() and not line.startswith("#"):
                current_section.append(line)
        if current_section:
            sections.append("\n".join(current_section))
        return "\n\n".join(sections)
    except Exception as e:
        print(f"[Soul Error] Gagal baca ARTI_SOUL.md: {e}")
        return ""

def load_viewer_context():
    """Baca ARTI_VIEWERS.md dan return viewer info untuk inject ke prompt."""
    viewers_path = os.path.join(_SCRIPT_DIR, "ARTI_VIEWERS.md")
    if not os.path.exists(viewers_path):
        return ""
    try:
        with open(viewers_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Extract viewer entries saja
        viewers = []
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("### ") and "|" not in line:
                # Viewer name header
                viewers.append(line)
            elif line.startswith("- **") or line.startswith("- Interaksi") or line.startswith("- Sifat"):
                viewers.append(line)
        return "\n".join(viewers[:30])  # Max 30 entries biar nggak kepanjangan
    except Exception as e:
        print(f"[Viewer Error] Gagal baca ARTI_VIEWERS.md: {e}")
        return ""

def viewer_block_for(viewer_name: str | None) -> str:
    """Blok profil SATU penonton — hanya untuk turn di mana dia benar-benar chat.

    Menggantikan dump statis semua penonton di system prompt. Keputusan streamer
    2026-08-01: "ambil soal mereka kalau mereka nanya aja, gausah penuhin context
    kalau mereka belum terbukti ada". Dump lama berisi SEMUA penonton (23 baris,
    ~900 char, ~230 token) di TIAP turn walau tidak ada penonton sama sekali —
    dan itulah yang bikin prompt jebol cap kemarin.

    Penonton yang cuma DISEBUT (bukan chat sendiri) tetap terjangkau lewat vault
    RAG: ARTI_VIEWERS.md ikut ter-index, dan query berisi nama terbukti menariknya
    (uji "siapa penonton yang sering chat" -> ARTI_VIEWERS.md peringkat 1).

    Mengerti dua format entri di ARTI_VIEWERS.md:
      ### nama\\n- **Field:** ...          (format penuh)
      ### nama | Pertemuan: ... | ...      (format ringkas — loader lama justru
                                            men-skip baris ini karena ada '|')
    """
    name = (viewer_name or "").strip().lstrip("@").casefold()
    if not name:
        return ""
    path = os.path.join(_SCRIPT_DIR, "ARTI_VIEWERS.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception as e:  # noqa: BLE001
        print(f"[Viewer Error] Gagal baca ARTI_VIEWERS.md: {e}")
        return ""

    block: list[str] = []
    capturing = False
    for line in lines:
        s = line.strip()
        if s.startswith("### "):
            if capturing:
                break  # entri berikutnya mulai — selesai
            header = s[4:]
            entry_name = header.split("|", 1)[0].strip().lstrip("@").casefold()
            if entry_name == name:
                capturing = True
                block.append(header if "|" in header else header)
        elif capturing:
            if s.startswith("##"):
                break
            if s.startswith("-"):
                block.append(s)
    if not block:
        return ""
    return "\n\n[VIEWER SAAT INI — kamu kenal dia:]\n" + "\n".join(block)


# === BACKGROUND SCOUTER (multi-provider chain) ===
scouter_thread = None
summarizer_thread = None  # alias
scouter_running = False
summarizer_running = False  # alias


def _scouter_config() -> dict:
    return {
        **CONFIG,
        "vision_auto_until": vision_auto_until,
        "vision_runtime_on": vision_runtime_on,
        "openrouter_api_key": (
            CONFIG.get("openrouter_api_key")
            or os.environ.get("OPENROUTER_API_KEY")
            or openrouter_api_key
        ),
    }


def _emotion_to_mood(emotion: str) -> str:
    emotion_to_mood = {
        "senang": "happy",
        "sedih": "sad",
        "marah": "angry",
        "bingung": "confused",
        "excited": "excited",
        "neutral": "lazy",
    }
    return emotion_to_mood.get(emotion, "lazy")


def apply_scouter_result(summary_data: dict) -> None:
    """Apply scouter JSON: mood, memory, auto-vision window, vision describe."""
    global scouter_result, summarizer_result, vision_auto_until, _last_scouter_ts, _last_scouter_history_snapshot

    with scouter_lock:
        scouter_result = summary_data
        summarizer_result = summary_data
        CONFIG["scouter_last_result"] = summary_data

    emotion = summary_data.get("emotion", "neutral")
    new_mood = _emotion_to_mood(emotion)
    set_mood(new_mood)

    print(f"[Scouter] Summary: {summary_data.get('summary', '')[:80]}...")
    print(f"[Scouter] Emotion: {emotion} → Mood: {new_mood}")

    for fact in arti_timeline_guard.filter_scouter_facts(summary_data.get("important_facts", [])):
        if fact and len(str(fact)) > 10:
            save_long_term_memory(f"Stream fact: {fact}")

    if summary_data.get("screen_relevant"):
        sec = float(CONFIG.get("scouter_auto_vision_sec", 60))
        vision_auto_until = max(vision_auto_until, time.time() + sec)
        _sync_vision_runtime_to_config()
        hint = summary_data.get("screen_hint") or ""
        print(f"[Scouter] Auto-vision ON ~{int(sec)}s{f' — {hint[:60]}' if hint else ''}")
        try:
            snap, provider = arti_vision_client.refresh_if_stale(_scouter_config())
            if snap and snap.scene:
                arti_screen_context.update_watch_state_from_snapshot(
                    snap,
                    event_id=str(CONFIG.get("watch_party_event_id") or ""),
                )
                print(f"[Scouter] Vision refresh via {provider}: {snap.scene[:50]}...")
        except Exception as e:
            print(f"[Scouter] Vision refresh skip: {type(e).__name__}: {e}")

    _last_scouter_ts = time.time()
    with history_lock:
        _last_scouter_history_snapshot = list(stream_history)[-15:]


def _run_scouter_pass(reason: str) -> None:
    import arti_scouter_client

    with history_lock:
        recent_history = list(stream_history)[-15:]
    context_text = "\n".join(recent_history)
    if not context_text.strip():
        return

    print(f"[Scouter] Run ({reason})...")
    summary_data = arti_scouter_client.run(context_text, _scouter_config())
    if not summary_data:
        print("[Scouter] Semua provider gagal.")
        return
    apply_scouter_result(summary_data)


def _scouter_timer_due() -> bool:
    import arti_scouter_client

    if not CONFIG.get("scouter_enabled", True):
        return False
    min_gap = float(CONFIG.get("scouter_min_gap_sec", 30))
    if time.time() - _last_scouter_ts < min_gap:
        return False
    interval = float(CONFIG.get("scouter_interval_sec", 90))
    with history_lock:
        current = list(stream_history)[-15:]
    if not current:
        return False
    if current == _last_scouter_history_snapshot:
        return False
    context = "\n".join(current)
    if time.time() - _last_scouter_ts >= interval:
        return True
    if arti_scouter_client.has_screen_keywords(context):
        return True
    return False


def scouter_worker():
    """Background thread: scouter chain every N triggers + interval timer."""
    global scouter_running, summarizer_running, trigger_count_since_scouter, trigger_count_since_summarize

    scouter_running = True
    summarizer_running = True
    chain = CONFIG.get("scouter_provider_chain") or []
    print(f"[Scouter] Background thread dimulai (chain: {', '.join(chain[:4])}...)...")

    while scouter_running:
        try:
            trigger_due = False
            try:
                scouter_queue.get(timeout=5)
                trigger_count_since_scouter += 1
                trigger_count_since_summarize = trigger_count_since_scouter
                every_n = int(CONFIG.get("scouter_every_n_triggers", 5))
                if trigger_count_since_scouter >= every_n:
                    trigger_due = True
                    trigger_count_since_scouter = 0
                    trigger_count_since_summarize = 0
                else:
                    print(f"[Scouter] Trigger {trigger_count_since_scouter}/{every_n}, skip.")
            except queue.Empty:
                pass

            if not CONFIG.get("scouter_enabled", True):
                continue

            if trigger_due:
                _run_scouter_pass("trigger")
            elif _scouter_timer_due():
                _run_scouter_pass("timer")

        except Exception as e:
            print(f"[Scouter] Thread error: {e}")
            time.sleep(1)

    print("[Scouter] Background thread dihentikan.")


summarizer_worker = scouter_worker


def start_scouter():
    """Mulai background scouter thread."""
    global scouter_thread, summarizer_thread
    if scouter_thread is None or not scouter_thread.is_alive():
        scouter_thread = threading.Thread(target=scouter_worker, daemon=True)
        summarizer_thread = scouter_thread
        scouter_thread.start()


start_summarizer = start_scouter


def get_scouter_context():
    """Ambil hasil scouter terbaru untuk inject ke prompt."""
    with scouter_lock:
        data = scouter_result
    if not data:
        return ""
    summary = data.get("summary", "")
    emotion = data.get("emotion", "neutral")
    topic = data.get("topic", "")
    block = (
        f"\n\n[RINGKASAN KONTEKS TERAKHIR]\n"
        f"Topic: {topic}\nEmotion: {emotion}\nRingkasan: {summary}"
    )
    if data.get("screen_relevant") and data.get("screen_hint"):
        block += f"\n[LAYAR RELEVAN: {data['screen_hint']}]"
    return block


get_summarizer_context = get_scouter_context


def stop_scouter():
    """Hentikan background scouter thread."""
    global scouter_running, summarizer_running
    scouter_running = False
    summarizer_running = False


stop_summarizer = stop_scouter

def get_current_mood():
    """Baca mood saat ini dari ARTI_MOOD_STATE.json."""
    mood_path = os.path.join(_SCRIPT_DIR, "ARTI_MOOD_STATE.json")
    try:
        if os.path.exists(mood_path):
            with open(mood_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state.get("current_mood", "cheerful")
    except:
        pass
    return "cheerful"

MOOD_HISTORY_MAX = 200   # [date removed]: berkas sempat 174 KB / 2.649 entri


def mood_block_now() -> str:
    """Blok mood yang SELALU segar, dibaca tiap giliran.

    Dulu blok ini dirakit sekali di startup dan ikut dipanggang ke
    `dynamic_system_prompt`. Akibatnya scouter boleh memperbarui mood
    sesering apa pun — Arti tetap membaca mood saat bridge dinyalakan.
    Live 14 Agu: prompt bilang "confused" dari menit pertama, dan Arti
    menutup 11 dari 11 giliran dengan [EMOTION:bingung] + "Hmm, bingung aku".
    """
    return f"\n\n[MOOD SAAT INI: {get_current_mood()}]"


def set_mood(new_mood):
    """Update mood Arti secara runtime."""
    mood_path = os.path.join(_SCRIPT_DIR, "ARTI_MOOD_STATE.json")
    try:
        state = {"current_mood": new_mood, "mood_since": time.strftime("%H:%M:%S"), "mood_history": []}
        if os.path.exists(mood_path):
            with open(mood_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            state["mood_history"].append({"mood": state.get("current_mood"), "until": time.strftime("%H:%M:%S")})
            # Riwayat tidak pernah dipangkas -> berkas tumbuh tanpa batas
            # (174 KB / 2.649 entri per [date removed]) padahal dibaca tiap
            # giliran sejak mood jadi segar. Simpan yang terbaru saja.
            state["mood_history"] = state["mood_history"][-MOOD_HISTORY_MAX:]
            state["current_mood"] = new_mood
            state["mood_since"] = time.strftime("%H:%M:%S")
        with open(mood_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"[Mood] Arti sekarang: {new_mood}")
    except Exception as e:
        print(f"[Mood Error] Gagal update mood: {e}")

# ============================================================
# ANTI-META / NARRATOR FILTER + SMART GROQ ROUTING (v0.6.2)
# Restore dari archive/checkpoints/hermes_vtuber_bridge_broken_2026-06-07.py
# (fitur teks murni — TIDAK menyentuh idle/expression/VTS)
# ============================================================

_BAD_ARTICULATION_PATTERNS = re.compile(
    r"|".join(
        [
            r"membaca\s+\d*\s*catatan",
            r"membaca\s+(sejarah|history|log)",
            r"catatan\s+sejarah",
            r"buku\s+sejarah",
            r"sejarah\s+(stream|chat|percakapan)",
            r"mengingat\s+(chat|sejarah|percakapan|dari\s+konteks)",
            r"aku\s+ingat",
            r"(kamu\s+)?ingat\s+viewer",
            r"bilang.*ingat",
            r"history\s+stream",
            r"\d+\s+catatan",
            r"log\s+(chat|stream)",
            r"sebagai\s+arti",
            r"arti\s+menerima",
            r"menerima\s+panggilan",
            r"menanggapi\s+panggilan",
            r"giliran\s+aku\s+sebagai",
            r"aku\s+harus\s+respon",
            r"aku\s+perlu\s+(memilih|ingat|menjawab)",
            r"bukan\s+sebagai\s+ai",
            r"langsung\s+sebagai\s+arti",
            r"sekarang\s+arti\s+(lagi|menerima)",
            r"perlu\s+ingat\s+aturan",
            r"ini\s+adalah\s+lanjutan",
            r"aturan\s*:\s*\d",
            r"arti\s+ini\s+dipanggil",
            r"pertanyaan\s+atau\s+panggilan",
            r"panggilan\s+sekarang\s+adalah",
            r"harus\s+merespons",
            r"ini\s+kan\s+ucapan",
            r"dengan\s+gaya\s+yang\s+sesuai",
            r"karena\s+aku\s+ingat\s+dari\s+konteks",
            r"langsung\s+merespons\s+sebagai",
        ]
    ),
    re.IGNORECASE,
)


def _streamer_label(config: dict | None = None) -> str:
    """Nama streamer untuk fallback in-character (config_local.json)."""
    cfg = config or CONFIG
    return (cfg.get("streamer_name") or "").strip()


def _sentence_is_bad_articulation(sentence: str) -> bool:
    return bool(_BAD_ARTICULATION_PATTERNS.search(sentence))


def is_narrator_reply(text: str) -> bool:
    """Deteksi jawaban yang masih mode 'AI menjelaskan tugas', bukan dialog Arti."""
    if not text:
        return False
    low = text.lower()
    if _BAD_ARTICULATION_PATTERNS.search(text):
        return True
    if re.search(r"^\s*arti\s+(ini|menerima|dipanggil)", low):
        return True
    streamer = _streamer_label().lower()
    if streamer and streamer in low and any(
        p in low
        for p in (
            "panggilan sekarang",
            "harus merespons",
            "gaya yang sesuai",
            "cukup terbuka",
        )
    ):
        return True
    return False


def filter_meta_history_talk(text: str) -> tuple[str, int]:
    """Buang kalimat meta; return (teks bersih, jumlah kalimat dibuang)."""
    if not text:
        return "", 0
    kept = []
    removed = 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        s = sentence.strip()
        if not s:
            continue
        if _sentence_is_bad_articulation(s):
            removed += 1
            print(f"[Filter] Meta/narrator dihapus: {s[:72]}...")
            continue
        kept.append(s)
    return " ".join(kept).strip(), removed


def _viewer_names_for_fallback(max_names: int = 3) -> list[str]:
    viewers_path = os.path.join(_SCRIPT_DIR, "ARTI_VIEWERS.md")
    if not os.path.isfile(viewers_path):
        return []
    names = []
    for line in open(viewers_path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("### "):
            names.append(line[4:].strip())
    return names[:max_names]


_fallback_giliran = -1


def incharacter_fallback_reply(user_speech: str) -> str:
    """Jawaban darurat kalau LLM keluar narrator/meta semua."""
    msg = _extract_trigger_message(user_speech).lower()
    low = (user_speech or "").lower()
    streamer = _streamer_label()

    # "hidup" TELANJANG dibuang dari daftar (live [time removed]): misi bawaan Arti
    # berbunyi "bertahan hidup", jadi SETIAP giliran game yang jatuh ke sini
    # meledakkan "Iya nyala kok!" — tiga kali dalam satu sesi, dan operator
    # merasakannya sebagai "fallback... masih sering putus [karakter]".
    if any(k in msg for k in ("nyala", "on gak", "on ga", "on gk",
                              "masih hidup", "masih idup", "nyala gk",
                              "nyala gak", "udah nyala", "hidup gak",
                              "hidup ga")):
        return "Iya nyala kok! Masih on di sini, ada apa nih?"
    if "ngelag" in msg or ("otak" in msg and "lag" in msg):
        return "Iya kadang lemot sih, tapi masih bisa ngobrol—ada apa?"
    if any(k in msg for k in ("sampai jumpa", "dadah", "bye", "goodbye")):
        return f"Oke guys, Arti dulu ya! Bye {streamer}~" if streamer else "Oke guys, Arti dulu ya! Bye semuanya~"
    if "cita" in msg:
        return "Cita-citaku? Jadi co-host terkeren lah, haha!"
    if "dengar" in msg or "dengar" in low:
        return "Iya dengar kok! Jelas banget, ada apa nih?"
    if "viewer" in low and ("siapa" in low or "ingat" in low or "inget" in low):
        names = _viewer_names_for_fallback(3)
        if names:
            joined = ", ".join(names)
            sapa = f"Yaelah {streamer}, " if streamer else "Yaelah, "
            return f"{sapa}yang sering keinget tuh {joined}—ada lagi yang baru nongol nanti."
    # Variasi: kalimat identik yang berulang terasa seperti mesin rusak
    # (operator: "fallback biar tetep ada di karakter arti masih sering putus").
    # Diputar berurutan, bukan acak, supaya dua giliran berdekatan tidak
    # kebetulan sama.
    global _fallback_giliran
    pilihan = (
        "Eh bentar—suaranya kepotong di kupingku, ulangi dong?",
        "Hhh, sinyal otakku ngadat sedetik. Tadi kamu bilang apa?",
        "Waduh, aku lag. Sekali lagi dong, biar aku nggak salah jawab.",
        "Bentar, tadi nggak kedengeran jelas—coba ulang?",
    )
    _fallback_giliran = (_fallback_giliran + 1) % len(pilihan)
    return pilihan[_fallback_giliran]


def _is_youtube_trigger(user_speech: str) -> bool:
    return arti_reply_policy.is_youtube_trigger(user_speech)


def _yt_reply_plan(user_speech: str, config: dict | None = None) -> arti_reply_policy.YtReplyPlan:
    cfg = config or CONFIG
    return arti_reply_policy.resolve_yt_reply_plan(
        user_speech, cfg, quiet=yt_chat_is_quiet(cfg)
    )


def live_max_tokens_for_trigger(
    user_speech: str = "", config: dict | None = None
) -> int:
    """Token generate LLM: YT adaptif, PTT/streamer longgar."""
    cfg = config or CONFIG
    if _is_youtube_trigger(user_speech):
        return _yt_reply_plan(user_speech, cfg).max_tokens
    # Jalur non-YT dulu selalu dapat jatah token datar — prompt, token, dan
    # filter jadi tidak sepakat. Sekarang ikut rencana yang sama.
    return arti_reply_policy.resolve_reply_plan(
        user_speech, cfg, quiet=yt_chat_is_quiet(cfg)
    ).max_tokens


def get_arti_reply_limits(
    user_speech: str = "", config: dict | None = None
) -> tuple[int, int]:
    """(max_sentences, max_chars) — YT adaptif, PTT panjang OK."""
    cfg = config or CONFIG
    if _is_youtube_trigger(user_speech):
        plan = _yt_reply_plan(user_speech, cfg)
        log_key = (plan.mode, plan.message_preview, plan.sentences)
        if getattr(get_arti_reply_limits, "_last_yt_log", None) != log_key:
            get_arti_reply_limits._last_yt_log = log_key
            print(
                f'[YT Reply] "{plan.message_preview}" → {plan.mode}, '
                f"max {plan.sentences} kal (~{plan.max_chars}ch, tok≈{plan.max_tokens})"
            )
        return plan.sentences, plan.max_chars
    plan = arti_reply_policy.resolve_reply_plan(
        user_speech, cfg, quiet=yt_chat_is_quiet(cfg)
    )
    log_key = (plan.mode, plan.message_preview, plan.sentences)
    if getattr(get_arti_reply_limits, "_last_lain_log", None) != log_key:
        get_arti_reply_limits._last_lain_log = log_key
        print(f"[Reply] {plan.mode} (~{plan.max_chars}ch, tok≈{plan.max_tokens})")
    return plan.sentences, plan.max_chars


def _truncate_reply_length(text: str, max_sentences: int, max_chars: int) -> str:
    """Potong jawaban panjang tanpa ganti fallback generik."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) > max_sentences:
        sentences = sentences[:max_sentences]
    result = re.sub(r"\s+", " ", " ".join(sentences)).strip()
    if len(result) <= max_chars:
        return result
    cut = result[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    cut = cut.rstrip(".,!?;:")
    return (cut + "…") if cut else result[:max_chars]


def get_viewer_profile_snippet(viewer_name: str) -> str:
    """Cuplikan profil satu viewer dari ARTI_VIEWERS.md."""
    if not viewer_name:
        return ""
    viewers_path = os.path.join(_SCRIPT_DIR, "ARTI_VIEWERS.md")
    if not os.path.isfile(viewers_path):
        return ""
    try:
        with open(viewers_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        header = f"### {viewer_name}"
        block = []
        in_block = False
        for line in lines:
            if line.startswith("### "):
                if in_block:
                    break
                in_block = line.strip().lower() == header.lower()
                if in_block:
                    block.append(line.strip())
            elif in_block and line.strip():
                block.append(line.strip())
        return "\n".join(block[:8])
    except Exception:
        return ""


def get_viewer_scoped_context(viewer_name: str | None = None, config: dict | None = None) -> str:
    """History untuk prompt: fokus viewer pemicu + sedikit streamer/Arti."""
    cfg = config or CONFIG
    max_v = int(cfg.get("viewer_context_max_messages", 8))
    streamer_tail = int(cfg.get("viewer_context_streamer_tail", 2))
    arti_tail = int(cfg.get("viewer_context_arti_tail", 2))

    with history_lock:
        all_history = list(stream_history)

    streamer_lines, viewer_lines, arti_lines = [], [], []
    for line in all_history:
        if "[Streamer]" in line:
            streamer_lines.append(line)
        elif "Viewer" in line and "(YouTube)" in line:
            vm = re.search(r"\[Viewer\s+([^\s(]+)", line)
            vkey = vm.group(1) if vm else None
            if viewer_name and vkey and vkey.lower() == viewer_name.lower():
                viewer_lines.append(line)
            elif not viewer_name:
                viewer_lines.append(line)
        elif "[Arti (VTuber)]" in line:
            arti_lines.append(line)

    result = []
    if streamer_lines:
        result.append("=== OMONGAN STREAMER TERAKHIR ===")
        result.extend(streamer_lines[-streamer_tail:])
    if viewer_lines:
        label = viewer_name or "viewer"
        result.append(f"\n=== CHAT {label.upper()} (fokus) ===")
        result.extend(viewer_lines[-max_v:])
    if arti_lines:
        result.append("\n=== JAWABAN ARTI TERAKHIR ===")
        result.extend(arti_lines[-arti_tail:])

    if viewer_name:
        profile = get_viewer_profile_snippet(viewer_name)
        if profile:
            result.append(f"\n=== PROFIL VIEWER {viewer_name} ===\n{profile}")

    return "\n".join(result) if result else "(Belum ada history)"


def get_compact_llm_context(
    viewer_name: str | None = None,
    config: dict | None = None,
) -> str:
    """Konteks ringkas untuk LLM live (bukan full 50 baris / semua viewer)."""
    cfg = config or CONFIG
    s_max = int(cfg.get("llm_history_streamer_max", 3))
    v_max = int(cfg.get("llm_history_viewer_max", 3))
    a_max = int(cfg.get("llm_history_arti_max", 2))

    if viewer_name:
        scoped_cfg = {
            **cfg,
            "viewer_context_max_messages": v_max,
            "viewer_context_streamer_tail": min(2, s_max),
            "viewer_context_arti_tail": a_max,
        }
        ctx = get_viewer_scoped_context(viewer_name, scoped_cfg)
        if len(ctx) > 1200:
            ctx = ctx[:1200] + "\n...(konteks dipangkas)"
        return ctx

    with history_lock:
        all_history = list(stream_history)

    streamer_lines, viewer_lines, arti_lines = [], [], []
    for line in all_history:
        if "[Streamer]" in line:
            streamer_lines.append(line)
        elif "Viewer" in line and "(YouTube)" in line:
            viewer_lines.append(line)
        elif "[Arti (VTuber)]" in line:
            arti_lines.append(line)

    parts = []
    if streamer_lines:
        parts.append("=== OMONGAN STREAMER TERAKHIR ===")
        parts.extend(streamer_lines[-s_max:])
    if viewer_lines:
        parts.append("\n=== CHAT VIEWER TERAKHIR ===")
        parts.extend(viewer_lines[-v_max:])
    if arti_lines:
        parts.append("\n=== JAWABAN ARTI TERAKHIR ===")
        parts.extend(arti_lines[-a_max:])

    return "\n".join(parts) if parts else "(Belum ada history)"


def pick_groq_model(
    user_text: str,
    config: dict | None = None,
    prompt_chars: int = 0,
) -> str:
    """Pilih model Groq by complexity; fallback round-robin jika smart off."""
    cfg = config or CONFIG
    models = cfg.get("groq_models", ["llama-3.1-8b-instant"])
    fast = cfg.get("groq_model_fast", "llama-3.1-8b-instant")

    def _pick(preferred: str) -> str:
        return preferred if preferred in models else (models[0] if models else preferred)

    # Prompt besar → model fast (hindari HTTP 413 di Groq)
    if prompt_chars > int(cfg.get("groq_prompt_char_soft_cap", 10000)):
        return _pick(fast)

    if not cfg.get("smart_groq_routing", True):
        if not hasattr(pick_groq_model, "_rr_idx"):
            pick_groq_model._rr_idx = 0
        m = models[pick_groq_model._rr_idx % len(models)]
        pick_groq_model._rr_idx += 1
        return m

    medium = cfg.get("groq_model_medium", "openai/gpt-oss-20b")
    strong = cfg.get("groq_model_strong", "qwen/qwen3.6-27b")
    rare = cfg.get("groq_model_rare", "openai/gpt-oss-120b")

    t = (user_text or "").lower()
    complex_kw = ("kenapa", "jelaskan", "bagaimana", "bandingkan", "explain", "detail", "ceritain", "maksudnya")
    if len(user_text) > 180 or sum(1 for k in complex_kw if k in t) >= 2:
        return _pick(rare)
    if "?" in user_text or len(user_text) > 100 or any(k in t for k in complex_kw):
        return _pick(strong)
    if len(user_text) > 55:
        return _pick(medium)
    return _pick(fast)


def _groq_fallback_chain(primary: str, config: dict) -> list[str]:
    """Urutan coba: model pilihan dulu, lalu sisanya (fast diutamakan saat limit)."""
    all_models = list(config.get("groq_models") or [])
    fast = config.get("groq_model_fast", "llama-3.1-8b-instant")
    tail: list[str] = []
    for m in [fast, *all_models]:
        if m and m not in tail:
            tail.append(m)
    chain = [primary] if primary else []
    for m in tail:
        if m not in chain:
            chain.append(m)
    return chain or all_models or [fast]


def _trim_llm_payload(
    system_prompt: str,
    user_content: str,
    max_chars: int = 14000,
) -> tuple[str, str]:
    """Pangkas user prompt (bagian history) supaya muat kuota token Groq."""
    total = len(system_prompt) + len(user_content)
    if total <= max_chars:
        return system_prompt, user_content
    over = total - max_chars
    if len(user_content) > over + 500:
        marker = "[KONTEKS LIVE TERBARU"
        if marker in user_content:
            head, _, tail = user_content.partition("[Pesan/Panggilan Sekarang:]")
            head = head[: max(800, len(head) - over - 200)] + "\n...(history dipangkas)...\n\n"
            return system_prompt, head + "[Pesan/Panggilan Sekarang:]" + tail
        return system_prompt, user_content[-max_chars:]
    return system_prompt[: max(4000, len(system_prompt) - over)], user_content


def _openrouter_after_groq(
    system_prompt: str,
    user_content: str,
    cfg: dict,
    reason,
) -> tuple[str | None, str | None]:
    if not cfg.get("openrouter_live_fallback_enabled", True):
        return None, None
    print(f"[Brain] Fallback OpenRouter (groq: {reason})...")
    reply, or_model = arti_openrouter.openrouter_live_completion(
        system_prompt, user_content, cfg
    )
    return reply, or_model


def _groq_model_hilang(response) -> bool:
    """HTTP 400 `model_decommissioned` = model ini MATI, bukan permintaan salah.

    Groq memensiunkan model kira-kira tiap 2 bulan (docs/MODEL-REGISTRY.md §4).
    Sampai 2026-08-13 kode di bawah memperlakukan 400 sebagai kegagalan fatal
    lalu `break` — jadi SATU model mati di tengah rantai menyumbat seluruh
    sisanya: primary kena 429 -> model mati -> berhenti -> OpenRouter, tanpa
    pernah menyentuh model Groq yang masih hidup. Dibuktikan dengan
    menjalankan groq_chat_completion terhadap transport palsu berbentuk error
    asli (diambil dari `llama3-8b-8192` yang sudah mati sejak Agu 2025).

    Sengaja SEMPIT — hanya kode/pesan yang jelas menyoal model. Permintaan
    yang memang rusak (payload salah) harus tetap gagal cepat, bukan
    menggilir lima model dan membuang lima kali round-trip.
    """
    if response.status_code not in (400, 404):
        return False
    kode = pesan = ""
    try:
        err = (response.json() or {}).get("error") or {}
        if isinstance(err, dict):
            kode = str(err.get("code") or "")
            pesan = str(err.get("message") or "")
    except Exception:  # noqa: BLE001 - body bukan JSON
        pesan = response.text or ""
    campur = f"{kode} {pesan}".lower()
    return any(
        p in campur
        for p in (
            "model_decommissioned",
            "model_not_found",
            "has been decommissioned",
            "no longer supported",
            "does not exist",
        )
    )


def groq_chat_completion(
    primary_model: str,
    system_prompt: str,
    user_content: str,
    config: dict | None = None,
) -> tuple[str | None, str | None]:
    """Groq dengan model pilihan (+ opsi roll semua model) → OpenRouter jika gagal."""
    cfg = config or CONFIG
    key = cfg.get("groq_api_key")
    if not key or key == "YOUR_GROQ_API_KEY":
        return _openrouter_after_groq(system_prompt, user_content, cfg, "no groq key")

    if cfg.get("groq_roll_all_models_on_limit", False):
        chain = _groq_fallback_chain(primary_model, cfg)
    else:
        chain = [primary_model] if primary_model else []

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = "https://api.groq.com/openai/v1/chat/completions"
    retryable = (429, 502, 503)
    last_status = None
    tried_413_retry = False
    dicoba: list[str] = []   # yang SUNGGUH ditembak, bukan panjang rantai

    for model in chain:
        dicoba.append(model)
        payload = {
            "model": model,
            "max_tokens": live_max_tokens_for_trigger(user_content, cfg),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if "qwen" in model.lower():
            # "/no_think" diabaikan qwen3.6 — pakai reasoning_effort API param
            payload["reasoning_effort"] = "none"
        elif "gpt-oss" in model.lower():
            # gpt-oss juga model reasoning tapi selama ini TANPA peredam —
            # malam [date removed]: 3 giliran curious (teks panjang -> routing rare =
            # gpt-oss-120b) balik 200 dengan content KOSONG karena seluruh
            # budget habis buat nalar. Groq menghormati "low" (beda dari
            # provider free OpenRouter yang mengabaikannya) — nalar singkat,
            # jawaban tetap utuh; persis permintaan operator "reasoning boleh
            # asal low".
            payload["reasoning_effort"] = "low"
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
        except Exception as e:
            print(f"[Groq] {model} error: {e}")
            if not cfg.get("groq_roll_all_models_on_limit", False):
                break
            continue

        if response.status_code == 200:
            isi = response.json()["choices"][0]["message"]["content"]
            if isi and str(isi).strip():
                return isi, model
            # HTTP 200 dengan content kosong = GAGAL, bukan sukses. Dulu
            # nilai kosong ini di-return apa adanya -> pemanggil menganggap
            # provider "sudah menjawab" -> OpenRouter tak pernah dicoba ->
            # giliran curious membisu 3x semalam ([date removed]).
            print(f"[Groq] {model} 200 tapi content KOSONG (budget termakan nalar?) — lanjut")
            last_status = 200
            continue

        last_status = response.status_code
        if response.status_code == 413 and not tried_413_retry:
            tried_413_retry = True
            fast = cfg.get("groq_model_fast", "llama-3.1-8b-instant")
            sys_t, user_t = _trim_llm_payload(system_prompt, user_content)
            print(f"[Groq] HTTP 413 — retry {fast} dengan prompt dipangkas...")
            payload["model"] = fast
            payload["messages"] = [
                {"role": "system", "content": sys_t},
                {"role": "user", "content": user_t},
            ]
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=30)
            except Exception as e:
                print(f"[Groq] retry 413 error: {e}")
                break
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"], fast
            last_status = response.status_code
        if _groq_model_hilang(response):
            print(
                f"[Groq] {model} SUDAH PENSIUN "
                f"(HTTP {response.status_code}) — lanjut model berikutnya."
            )
            continue
        if response.status_code in retryable:
            if cfg.get("groq_roll_all_models_on_limit", False):
                print(f"[Groq] {model} HTTP {response.status_code} — coba Groq lain...")
                continue
            print(f"[Groq] {model} HTTP {response.status_code} — langsung OpenRouter.")
            break
        if response.status_code not in (413,):
            print(f"[Groq] {model} HTTP {response.status_code}: {response.text[:150]}")
            break
        print(f"[Groq] {model} HTTP 413 — langsung OpenRouter.")
        break

    if len(dicoba) > 1:
        # Dulu mencetak len(chain) — bohong: "Gagal setelah 5 model" padahal
        # cuma 1-2 yang ditembak sebelum break.
        print(
            f"[Groq] Gagal setelah {len(dicoba)} model dicoba "
            f"({', '.join(dicoba)}) — HTTP {last_status}"
        )
    return _openrouter_after_groq(system_prompt, user_content, cfg, last_status)


def _codex_cfg_untuk_kelas(user_speech: str, cfg: dict) -> dict:
    """Naikkan effort Luna khusus kelas jawaban berat (usul streamer 27 Agu).

    Kelasnya dihitung dari mesin panjang-jawaban yang SUDAH ada
    (`arti_reply_policy`), jadi tidak ada penilai kedua yang bisa berbeda
    pendapat dengan kelas yang tercetak di log.

    Kenapa ini masuk akal, dan kenapa `high` bukan `xhigh` — semuanya
    terukur 27 Agu, n=20 tiap tingkat, prompt produksi:

        none   p50 2,55s  maks  8,39s   kuis 9/10 dan 5/6  <- SALAH hitung
        low    p50 2,42s  maks  8,49s   kuis 10/10 dan 6/6
        high   p50 3,70s  maks 11,54s
        xhigh  p50 4,53s  maks 26,24s   kuis 10/10 dan 6/6

    Tiga hal yang menentukan bentuk fungsi ini:

    1. `none` TIDAK lebih cepat dari `low` (2,55 lawan 2,42) dan mulai
       salah berhitung — Rp119.790 untuk soal yang jawabannya Rp119.880.
       Jadi lantai tangga ini `low`, bukan `none`.
    2. Untuk jawaban PANJANG, effort nyaris tak berpengaruh: satu
       pertanyaan `deep` memakan 8,54 dtk di low dan 9,01 dtk di xhigh —
       waktunya habis mengarang kalimat, bukan berpikir. Menaikkan effort
       di kelas berat itu murah.
    3. Yang TIDAK murah: ekor `xhigh` 26,24 dtk sementara
       `codex_timeout_sec` cuma 12 — giliran `deep` justru giliran paling
       berharga dan paling mungkin terbuang ke Groq. Maks `high` 11,54 dtk,
       masih di bawah pagar.

    Gagal apa pun -> kembalikan cfg apa adanya. Menilai kelas tidak pernah
    boleh menjatuhkan giliran suara.
    """
    try:
        kelas = arti_reply_policy.kelas_dari_mode(
            arti_reply_policy.resolve_reply_plan(user_speech, cfg, quiet=True).mode
        )
        if kelas not in set(cfg.get("codex_effort_kelas_berat") or ()):
            return cfg
        berat = str(cfg.get("codex_effort_berat", "high"))
        if berat == str(cfg.get("codex_effort", "low")):
            return cfg
        # SENGAJA tidak mencetak apa pun di sini. Versi pertama mencetak
        # "[Codex] kelas X -> effort Y" di tiap giliran — termasuk giliran
        # yang dijawab agy dan Luna tidak pernah dipanggil (sesi live [date removed]:
        # 6 baris, NOL di antaranya benar-benar memakai Luna). Itu menipu
        # pembaca log berikutnya.
        #
        # Informasinya tidak hilang: kalau Luna benar-benar dipakai, dia
        # mencetak sendiri "[Codex] gpt-5.6-luna/high 2924ms (turn N)" —
        # effort-nya ada di situ. Dan kelasnya sudah terbaca dari
        # "[Reply] streamer-deep->3kal".
        return {**cfg, "codex_effort": berat}
    except Exception as exc:  # noqa: BLE001
        print(f"[Codex] gagal menilai kelas ({type(exc).__name__}) — effort bawaan")
        return cfg


def _should_route_to_cursor(trigger_type: str, config: dict | None = None) -> bool:
    """Haruskah turn ini dijawab lewat Cursor Composer, bukan rantai provider biasa?

    SENGAJA tidak menyentuh `CONFIG["api_provider"]`. Routing per-trigger hidup di
    jalur terpisah supaya rantai gemini_live/gemini/groq/sambanova yang ada tidak
    dirombak sama sekali — `mic` dan `curious` tetap berperilaku bit-identik.
    """
    cfg = config or CONFIG
    agy_types = set(
        cfg.get("agy_trigger_types") or ["mic", "ptt", "yt_chat", "curious"]
    )
    if (
        trigger_type in agy_types
        and cfg.get("agy_agent_enabled", False)
        and cfg.get("agy_primary_voice", False)
    ):
        return True
    if trigger_type not in set(cfg.get("cursor_trigger_types") or ["yt_chat"]):
        return False
    # MODE LUNA-UTAMA: jalur ini bukan lagi milik Cursor sendiri — blok Luna
    # hidup DI DALAM _cursor_reply_with_fallback. Audit [date removed] menemukan Luna
    # TERSANDERA di sini: kalau Cursor mati (kunci hilang, SDK rusak, scratch
    # dir terhapus), fungsi ini balik False, jalurnya tak pernah dipanggil,
    # dan seluruh siaran turun SENYAP ke Groq — padahal Luna sehat walafiat.
    # Kolam premium kedua justru dibangun supaya tidak sekolam dengan Cursor.
    if cfg.get("codex_primary_voice", False) and cfg.get("codex_agent_enabled", False):
        return True
    if not cfg.get("cursor_agent_enabled", False):
        return False
    try:
        import arti_cursor_agent

        ok, _why = arti_cursor_agent.is_available(cfg)
        return bool(ok)
    except Exception:  # noqa: BLE001 — modul/SDK tidak ada -> rantai lama, nol biaya
        return False


async def _cursor_reply_with_fallback(
    llm_system: str,
    prompt_content: str,
    user_speech: str,
    config: dict | None = None,
    trigger_type: str = "",
) -> tuple[str | None, list[str], str]:
    """Cursor -> Groq -> OpenRouter -> in-character. Return (reply, kalimat, sumber).

    Arti tidak boleh pernah bisu saat MENJAWAB orang. KECUALI turn proaktif
    (curious/inisiatif): kalau semua provider mati, DIAM lebih baik daripada
    kalimat kaleng "ulang pertanyaannya dong?" padahal tidak ada yang bertanya
    (kejadian di tes live 2026-08-02 — dan meta-nya ikut nyampah ke learnings).
    """
    import arti_cursor_agent

    cfg = config or CONFIG
    timeout_s = float(cfg.get("cursor_timeout_sec", 5.0))

    cfg_luna = _codex_cfg_untuk_kelas(user_speech, cfg)

    agy_types = set(
        cfg.get("agy_trigger_types") or ["mic", "ptt", "yt_chat", "curious"]
    )
    _agy_utama = bool(
        cfg.get("agy_agent_enabled", False)
        and cfg.get("agy_primary_voice", False)
        and trigger_type in agy_types
    )
    if _agy_utama and "PYTEST_CURRENT_TEST" in os.environ and not cfg.get(
        "_agy_primary_tes"
    ):
        # Config lokal produksi boleh menyala saat suite berjalan, tetapi tes
        # lama tidak boleh pernah memanggil proses agy sungguhan.
        _agy_utama = False
    try:
        arti_agy_agent.prewarm(cfg)
    except Exception:  # noqa: BLE001
        pass

    # Sesi dingin butuh ~18 detik (nyalakan bridge SDK + giliran pertama), jauh di atas
    # timeout 5 detik. Kalau dipaksa, tiap chat timeout -> sesi ditandai rusak ->
    # didaur ulang -> dingin lagi: Cursor tidak akan PERNAH terpakai. Jadi turn ini
    # langsung ke Groq sementara pemanasan jalan di latar belakang; chat berikutnya
    # barulah dilayani Cursor (terukur 3,4-3,5 detik).
    warm = arti_cursor_agent.prewarm(cfg)
    # Kolam premium kedua ikut dipanaskan (no-op instan saat dimatikan) —
    # supaya saat composer tumbang, Luna sudah hangat, bukan baru bangun.
    try:
        arti_codex_agent.prewarm(cfg)
    except Exception:  # noqa: BLE001
        pass

    # AGY UTAMA: hanya percakapan. Dingin/recycle/gagal langsung masuk Luna;
    # provider sendiri membuang generation rusak dan memanaskan penggantinya.
    _agy_attempted = False
    if _agy_utama:
        _agy_attempted = True
        try:
            result = await asyncio.to_thread(
                arti_agy_agent.send_turn, llm_system, prompt_content, cfg
            )
            if result.ok and result.text:
                return result.text, _sentences_or_empty(result.text), "agy"
            print(f"[Agy] utama gagal ({result.reason}) — jatuh ke Luna")
        except asyncio.CancelledError:
            arti_agy_agent.abort_turn("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Agy] utama gagal: {type(exc).__name__} — jatuh ke Luna")

    # MODE LUNA UTAMA (eksperimen [date removed]): coba Codex SEBELUM composer.
    # Gagal apa pun -> jatuh mulus ke alur composer lama di bawah (composer
    # tetap hangat sebagai cadangan). Trigger berharga (video/donation)
    # SENGAJA ikut Luna — eksperimennya justru mengukur kualitas+kuota.
    _luna_utama = bool(cfg.get("codex_primary_voice", False) or _agy_attempted)
    if _luna_utama and "PYTEST_CURRENT_TEST" in os.environ and not cfg.get(
        "_codex_primary_tes"
    ):
        # Pagar pytest (pelajaran prewarm [date removed]): CONFIG produksi bocor ke
        # tes lama via config_local — tanpa pagar, suite memanggil Luna/Groq
        # BETULAN lewat blok ini. Tes blok ini memakai kunci privat.
        _luna_utama = False
    if _luna_utama:
        try:
            reply = await asyncio.to_thread(
                arti_codex_agent.send_turn, llm_system, prompt_content, cfg_luna
            )
            if reply:
                return reply, _sentences_or_empty(reply), "codex"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Codex] utama gagal: {type(exc).__name__}")
        # Luna gagal: JANGAN kaskade ke composer — log [date removed] [time removed]: pagar
        # Luna (8s) + kapak composer (16s) menumpuk jadi llm p50 10,6 dtk /
        # p90 27 dtk. Langsung Groq (cepat); alur composer di bawah hanya
        # jaring TERAKHIR kalau Groq ikut mati (anti-bisu tetap dijaga).
        print("[Codex] utama gagal/kosong — langsung Groq (composer dilewati)")
        try:
            model = pick_groq_model(_extract_trigger_message(user_speech), cfg)
            reply, used = await asyncio.to_thread(
                groq_chat_completion, model, llm_system, prompt_content, cfg
            )
            if reply:
                return reply, _sentences_or_empty(reply), f"groq:{used}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Codex] Groq pasca-Luna gagal: {type(exc).__name__} — "
                  "jatuh ke alur composer (jaring terakhir)")

    if not warm and trigger_type in ("video", "donation"):
        # Trigger BERHARGA: konten tak tergantikan (digest video, terima kasih
        # donatur bayar) dan tidak diburu waktu — penonton baru selesai nonton
        # klip/alert. Live sore2 [date removed]: reaksi "BEST OF ZACH 2" (Rp 2.000)
        # kena sesi dingin -> dijawab Groq 8B -> operator: "kayaknya gak liat deh
        # dia". Tunggu pemanasan (bounded) alih-alih lempar ke 8B.
        wait_s = float(cfg.get("cursor_warmup_wait_precious_sec", 45.0))
        print(
            f"[Cursor] sesi dingin, trigger {trigger_type} berharga — "
            f"tunggu pemanasan maks {wait_s:.0f}s"
        )
        _t0 = time.time()
        while time.time() - _t0 < wait_s:
            await asyncio.sleep(1.0)
            if arti_cursor_agent.is_warm():
                warm = True
                print(f"[Cursor] pemanasan selesai ({time.time() - _t0:.0f}s) — lanjut")
                break

    if not warm:
        print("[Cursor] sesi belum hangat — turn ini lewat Groq, pemanasan jalan")
        # Lapis antara ([date removed]): Codex/Luna hangat 1,5-3 dtk — coba dulu
        # sebelum Groq. Default MATI (codex_agent_enabled); gagal apa pun
        # -> None -> Groq seperti biasa.
        try:
            reply = await asyncio.to_thread(
                arti_codex_agent.send_turn, llm_system, prompt_content, cfg_luna
            )
            if reply:
                return reply, _sentences_or_empty(reply), "codex"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Codex] lapis cadangan gagal: {type(exc).__name__}")
        try:
            model = pick_groq_model(_extract_trigger_message(user_speech), cfg)
            reply, used = await asyncio.to_thread(
                groq_chat_completion, model, llm_system, prompt_content, cfg
            )
            if reply:
                return reply, _sentences_or_empty(reply), f"groq:{used}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Cursor] Groq saat pemanasan gagal: {type(exc).__name__}")
        if trigger_type == "curious":
            print("[Curious] Semua provider gagal — turn proaktif di-skip (diam saja)")
            _note_curious_provider_fail(cfg)
            return None, [], "skip-curious"
        if cfg.get("cursor_last_resort_incharacter", True):
            return incharacter_fallback_reply(user_speech), [], "incharacter"
        return None, [], "gagal"

    reason = "?"
    try:
        # `wait_for` di sini adalah pengaman UTAMA, bukan cadangan: deadline internal
        # collect_run_messages tidak bisa menghentikan iterator yang menggantung —
        # terbukti satu sampel lolos jadi 41 detik padahal timeout 30.
        res = await asyncio.wait_for(
            asyncio.to_thread(
                arti_cursor_agent.send_turn, llm_system, prompt_content, cfg
            ),
            timeout=timeout_s + 1.0,
        )
        reason = res.reason
        if res.ok and res.text:
            print(f"[Cursor] {res.model} {res.latency_ms}ms")
            return res.text, res.sentences, "cursor"
    except asyncio.CancelledError:
        # Barge-in PTT membatalkan turn. Sesi ditandai rusak supaya run yatim tidak
        # bikin AgentBusyError di turn berikutnya, lalu DI-RAISE ULANG supaya handler
        # pembatalan yang sudah ada tetap berjalan apa adanya.
        arti_cursor_agent.mark_dirty_global("cancelled")
        raise
    except asyncio.TimeoutError:
        arti_cursor_agent.mark_dirty_global("outer_timeout")
        reason = "outer_timeout"
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}"

    print(f"[Cursor] gagal ({reason}) — fallback Groq")
    # Timeout/error menandai sesi rusak -> nyalakan pemanasan ulang SEKARANG,
    # jangan tunggu turn berikutnya menemukan sesi dingin (live sore2: jendela
    # dingin pasca-outer_timeout menelan reaksi video donatur ~10 dtk kemudian).
    try:
        arti_cursor_agent.prewarm(cfg)
    except Exception:  # noqa: BLE001
        pass

    # Lapis antara ([date removed]): Codex/Luna sebelum Groq — thread hangat
    # 1,5-3 dtk, kualitas persona Indonesia terbukti probe. Default MATI.
    try:
        reply = await asyncio.to_thread(
            arti_codex_agent.send_turn, llm_system, prompt_content, cfg_luna
        )
        if reply:
            return reply, _sentences_or_empty(reply), "codex"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[Codex] lapis cadangan gagal: {type(exc).__name__}")

    # Groq. Fungsi ini sudah merantai seluruh model Groq lalu jatuh ke
    # _openrouter_after_groq sendiri, jadi lapis ketiga ikut gratis.
    try:
        model = pick_groq_model(_extract_trigger_message(user_speech), cfg)
        reply, used = await asyncio.to_thread(
            groq_chat_completion, model, llm_system, prompt_content, cfg
        )
        if reply:
            return reply, _sentences_or_empty(reply), f"groq:{used}"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[Cursor] fallback Groq juga gagal: {type(exc).__name__}: {exc}")

    if trigger_type == "curious":
        print("[Curious] Semua provider gagal — turn proaktif di-skip (diam saja)")
        _note_curious_provider_fail(cfg)
        return None, [], "skip-curious"
    if cfg.get("cursor_last_resort_incharacter", True):
        text = incharacter_fallback_reply(user_speech)
        return text, [], "incharacter"
    return None, [], "gagal"


def _sentences_or_empty(text: str) -> list[str]:
    """Kontrak sama dengan jalur Groq: list hanya diisi kalau >1 kalimat."""
    try:
        parts = arti_groq_stream.split_indonesian_sentences(text)
        return parts if len(parts) > 1 else []
    except Exception:  # noqa: BLE001
        return []


# qwen di Groq sesekali menyelipkan aksara CJK ke jawaban Indonesia ("tiap
# kali直播 lah?" — live [date removed]); TTS membacanya kacau. Rentang: kana,
# CJK unified, hangul, fullwidth forms.
_CJK_RE = re.compile(r"[　-ヿ㐀-鿿가-힯＀-￯]+")


def _shorten_viewer_handles(text: str, user_speech: str | None) -> str:
    """Ganti handle panjang di jawaban dengan nama panggilan pendek.

    Jaring pengaman kedua (yang pertama: instruksi nick di prompt) — TTS tidak
    boleh membaca "abdmanlifyou241" bulat-bulat. Dua sasaran:
    1. Handle viewer TURN INI (dengan/tanpa @, case-insensitive).
    2. Token gaya-handle eksplisit ber-@ dengan >=2 digit ekor di mana pun.
    """
    handle = arti_reply_policy.extract_viewer_handle(user_speech or "")
    if handle:
        nick = arti_reply_policy.viewer_nickname(handle)
        bare = re.escape(handle.lstrip("@"))
        if nick and nick.lower() != handle.lstrip("@").lower():
            text = re.sub(rf"@?{bare}\b", nick, text, flags=re.IGNORECASE)
    def _nick_at(m: re.Match) -> str:
        return arti_reply_policy.viewer_nickname(m.group(0)) or m.group(0)
    return re.sub(r"@[A-Za-z][\w.\-]*\d{2,}\b", _nick_at, text)


def post_process_response(text, user_speech=None, config=None):
    """Anti-meta/narrator; potong panjang (bukan fallback) supaya ada depth."""
    if not text:
        return ""
    cleaned_cjk = _CJK_RE.sub(" ", text)
    if cleaned_cjk != text:
        print("[Filter] Aksara CJK dibuang dari jawaban")
        text = re.sub(r"\s{2,}", " ", cleaned_cjk).strip()
    # TTS tidak boleh pernah mengeja URL (temuan audit: link dari chat/
    # navigationEndpoint bisa terbawa sampai jawaban).
    cleaned_url = re.sub(r"(?:https?://|www\.)\S+", "", text)
    if cleaned_url != text:
        print("[Filter] URL dibuang dari jawaban")
        text = re.sub(r"\s{2,}", " ", cleaned_url).strip()
    text = _shorten_viewer_handles(text, user_speech)

    max_sent, max_chars = get_arti_reply_limits(user_speech or "", config)

    text, removed = filter_meta_history_talk(text)
    if not text or removed > 0 or is_narrator_reply(text):
        fb = incharacter_fallback_reply(user_speech or "")
        why = "semua meta" if not text else f"{removed} kalimat meta / narrator"
        print(f"[Filter] Ganti jawaban ({why}) -> {fb[:60]}...")
        return strip_tts_expression_tags(fb)

    result = _truncate_reply_length(text, max_sent, max_chars)
    if is_narrator_reply(result):
        return strip_tts_expression_tags(incharacter_fallback_reply(user_speech or ""))
    if len(result) < len(text.strip()):
        print(
            f"[Filter] Jawaban dipotong ({len(text)}→{len(result)}ch, "
            f"max {max_sent} kal / {max_chars}ch)"
        )
    return strip_tts_expression_tags(result)


# ============================================================
# IDLE ANIMATION SYSTEM — 2-Track: Motions + Expressions
# ============================================================
import random
import math

idle_timer_thread = None
idle_timer_running = False
idle_thread_lock = threading.Lock()
idle_expression_active = False
_idle_startup_cleanup_done = False
_idle_expr_backoff = 0.0
main_event_loop = None


def _idle_ws_ok() -> bool:
    if _idle_ws is None:
        return False
    try:
        return bool(getattr(_idle_ws, "open", True))
    except Exception:
        return False

# --- Track 1: Motion Hotkeys (smooth body movement) ---
# Motion BERSIH (dihasilkan scripts/buat_motion_bersih.py), bukan ArtiIdle
# mentah. Diganti [date removed] sesudah bukti live: motion mentah menulis 62
# parameter yang sama dengan berkas ekspresi — termasuk ParamMouthOpenY —
# sehingga lipsync, lampu, dan titik tiga ditimpa selama motion berputar.
#
# Ini BUKAN sekadar ganti nama: selama bawaannya masih IdleMotion1..5,
# "kosongkan idle_motion_hotkeys" (yang dulu disarankan bridge sendiri di
# akhir sesi uji) justru MENGEMBALIKAN bug-nya.
IDLE_MOTION_HOTKEYS = [
    "ArtiMotionBersih1", "ArtiMotionBersih2", "ArtiMotionBersih3",
    "ArtiMotionBersih4", "ArtiMotionBersih5",
]


_idle_motion_uji_diwartakan = False
_idle_lanjut_diwartakan = False


def _idle_motion_names() -> list:
    """Daftar hotkey motion yang dipakai — CONFIG menang kalau diisi.

    Dipisah jadi fungsi (bukan konstanta) supaya bisa diuji terhadap CONFIG
    PRODUKSI (aturan #7) dan supaya sesi percobaan cukup mengisi
    `idle_motion_hotkeys` di config_local lalu menghapusnya lagi.
    """
    global _idle_motion_uji_diwartakan
    dari_cfg = CONFIG.get("idle_motion_hotkeys") or []
    nama = [str(n).strip() for n in dari_cfg if str(n).strip()]
    if nama and not _idle_motion_uji_diwartakan:
        _idle_motion_uji_diwartakan = True
        # Peringatan hanya kalau daftarnya BEDA dari rotasi produksi. Versi
        # lama memperingatkan apa pun isinya, jadi begitu daftar produksi
        # ditulis di config_local dia tetap berteriak "MODE UJI" dan menyuruh
        # mengosongkannya — saran yang justru mengembalikan bug mulut.
        asing = [n for n in nama if n not in IDLE_MOTION_HOTKEYS]
        if asing:
            print(
                f"[Idle] MODE UJI motion: {nama} — di luar rotasi produksi: "
                f"{asing}. Kembalikan ke daftar bawaan kalau sesi uji selesai."
            )
        else:
            print(f"[Idle] Motion rotasi ({len(nama)}): {nama}")
    return nama or list(IDLE_MOTION_HOTKEYS)
MOTION_INTERVAL_MIN = 25   # seconds between motion triggers
MOTION_INTERVAL_MAX = 40

# --- Track 2: Expression toggles (micro-expressions) ---
IDLE_EXPRESSIONS = [f"ArtiIdle{i}" for i in range(1, 51)]
EXPR_CHECK_MIN = 5     # Not used in cross-fade mode (kept for reference)
EXPR_CHECK_MAX = 12    # Not used in cross-fade mode (kept for reference)
EXPR_HOLD_MIN = 8      # Hold each expression 8-18 seconds (accounts for 2.5s fade-in)
EXPR_HOLD_MAX = 18     # Longer holds = more natural, expressions linger


# Shared websocket — satu thread + satu event loop (jangan spawn ulang tiap PTT)
_idle_ws = None
_idle_ws_lock = None  # asyncio.Lock, dibuat sekali per worker loop
_idle_face_y_queue: queue.SimpleQueue = queue.SimpleQueue()  # nod mirror → idle ws
_idle_hotkey_cmd_queue: queue.SimpleQueue = queue.SimpleQueue()  # hotkey / deactivate cmds
_idle_hotkey_cache: dict[str, str] = {}  # name -> hotkeyID (diisi saat idle connect)
_idle_active_expr: str | None = None
_idle_active_expr_lock = threading.Lock()
_idle_worker_loop: asyncio.AbstractEventLoop | None = None


def _get_idle_active_expr() -> str | None:
    with _idle_active_expr_lock:
        return _idle_active_expr


def _set_idle_active_expr(name: str | None) -> None:
    global _idle_active_expr
    with _idle_active_expr_lock:
        _idle_active_expr = name


def _queue_idle_deactivate_expr() -> None:
    """OFF-kan ArtiIdle aktif segera (idle worker thread), hindari bentrok ekspresi jawaban."""
    expr = _get_idle_active_expr()
    loop = _idle_worker_loop
    if expr and loop and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(_idle_deactivate_expression(expr), loop)
            return
        except Exception:
            pass
    try:
        _idle_hotkey_cmd_queue.put_nowait(("off_expr", expr))
    except Exception:
        pass


async def _idle_motion_stop_for_turn() -> None:
    """Potong motion badan dari main loop (tunggu ACK hotkey)."""
    stop_name = (CONFIG.get("idle_motion_stop_hotkey") or "").strip()
    if not stop_name:
        return
    loop = _idle_worker_loop
    if loop and loop.is_running():
        try:
            fut = asyncio.run_coroutine_threadsafe(
                _idle_trigger_hotkey_by_name(stop_name), loop
            )
            await asyncio.wait_for(asyncio.wrap_future(fut), timeout=2.0)
            return
        except Exception:
            pass
    try:
        _idle_hotkey_cmd_queue.put_nowait(stop_name)
    except Exception:
        pass


async def _prepare_turn_start(trigger_type: str, viewer_name: str | None) -> None:
    """Satu jalur pause idle + expression turn (main loop only).

    `stop_idle_animation()` TETAP dipanggil walau motion diteruskan: dia yang
    mematikan EKSPRESI idle yang sedang aktif. Itu bukan formalitas —
    ArtiIdle* dan ArtiBicara sama-sama blend Add dan berbagi 43 parameter
    yang nilainya identik, jadi membiarkan keduanya hidup bersamaan membuat
    potret wajah yang sama ditambahkan DUA KALI.

    Yang dilewati saat `idle_motion_lanjut_saat_bicara` menyala hanyalah
    hotkey penghenti motion. Track motion boleh memicu rotasi baru selama
    `_brain_busy=True`; track ekspresi idle tetap mati sepanjang giliran.
    """
    global _idle_lanjut_diwartakan
    stop_idle_animation()
    if not CONFIG.get("idle_motion_lanjut_saat_bicara", False):
        await _idle_motion_stop_for_turn()
    elif not _idle_lanjut_diwartakan:
        _idle_lanjut_diwartakan = True
        # Bukan lagi "MODE UJI": sejak [date removed] rotasi bawaannya memang motion
        # bersih tanpa kurva kepala/mulut, jadi ini setelan yang dimaksudkan.
        # Syaratnya tetap ditulis supaya orang yang memakai motion mentah
        # tahu kenapa angguknya hilang.
        print(
            "[Idle] Motion JALAN TERUS saat bicara "
            "(idle_motion_lanjut_saat_bicara=true). Syarat: motion tanpa "
            "kurva kepala/mulut — pakai scripts/buat_motion_bersih.py."
        )
    if trigger_type == "yt_chat":
        who = viewer_name or "viewer"
        print(f"[Turn] yt_chat: aware→mikir (idle off) — {who}")
        await vts.trigger_expression_state("aware")
        await asyncio.sleep(0.1)
    await vts.trigger_expression_state("mikir")


def idle_animation_worker():
    """Background thread: 2-track idle (motions + expressions) with dedicated VTS websocket."""
    global idle_timer_running, idle_expression_active
    idle_timer_running = True
    print("[Idle] 2-Track Animation system dimulai (Motions + Expressions)...")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_idle_worker_main())
    except Exception as e:
        print(f"[Idle] Worker crashed: {e}")
    finally:
        loop.close()


async def _idle_connect_ws(*, max_attempts: int = 5):
    """Connect and authenticate a dedicated idle websocket."""
    timeout = float(CONFIG.get("idle_vts_connect_timeout_sec", 20))
    for attempt in range(max_attempts):
        ws = None
        try:
            uri = f"ws://localhost:{CONFIG['vts_api_port']}"
            ws = await websockets.connect(uri, open_timeout=timeout, close_timeout=5)
            with open("vts_token.txt", "r") as f:
                token = f.read().strip()
            auth = {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "IdleAuth",
                "messageType": "AuthenticationRequest",
                "data": {
                    "pluginName": CONFIG["vts_plugin_name"],
                    "pluginDeveloper": CONFIG["vts_developer"],
                    "authenticationToken": token
                }
            }
            await ws.send(json.dumps(auth))
            resp = json.loads(await ws.recv())
            if resp.get("data", {}).get("authenticated"):
                print("[Idle] Dedicated VTS connection ready ✓")
                return ws
            print(f"[Idle] Auth ditolak VTS (attempt {attempt + 1})")
            await ws.close()
            ws = None
        except Exception as e:
            print(f"[Idle] Connect attempt {attempt+1} failed: {e}")
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None
        await asyncio.sleep(2)
    return None


async def _idle_send(ws, payload):
    """Send a request and receive response with lock to prevent race conditions."""
    global _idle_ws_lock
    if _idle_ws_lock:
        async with _idle_ws_lock:
            await ws.send(json.dumps(payload))
            return json.loads(await ws.recv())
    else:
        await ws.send(json.dumps(payload))
        return json.loads(await ws.recv())


async def _idle_reconnect(ws):
    """Try to reconnect the idle websocket."""
    try:
        if ws:
            await ws.close()
    except:
        pass
    return await _idle_connect_ws()


async def _idle_cleanup_expressions(ws):
    """Startup cleanup: deactivate ALL idle expressions to prevent stuck poses from previous sessions."""
    print("[Idle] Cleaning up stale expressions from previous session...")
    cleaned = 0
    for expr_name in IDLE_EXPRESSIONS:
        try:
            payload = {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "IdleCleanup",
                "messageType": "ExpressionActivationRequest",
                "data": {"expressionFile": f"{expr_name}.exp3.json", "active": False}
            }
            await _idle_send(ws, payload)
            cleaned += 1
        except Exception:
            pass
    print(f"[Idle] Cleanup done ({cleaned} expressions reset to OFF)")


async def _idle_inject_face_y_set(y: float) -> None:
    """Set FaceAngleY on idle websocket (channel yang sama dengan smooth idle)."""
    if not _idle_ws_ok():
        return
    await _idle_send(
        _idle_ws,
        {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "IdleFaceYOverride",
            "messageType": "InjectParameterDataRequest",
            "data": {
                "faceFound": False,
                "mode": "set",
                "parameterValues": [{"id": "FaceAngleY", "weight": 1.0, "value": y}],
            },
        },
    )


async def _idle_reset_face_angles() -> None:
    """Neutralkan head tracking di idle ws setelah pause (hindari sisa pose add)."""
    if not _idle_ws_ok():
        return
    zeros = [
        {"id": pid, "weight": 1.0, "value": 0.0}
        for pid in ("FaceAngleX", "FaceAngleY", "FaceAngleZ")
    ]
    try:
        await _idle_send(
            _idle_ws,
            {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "IdleFaceReset",
                "messageType": "InjectParameterDataRequest",
                "data": {
                    "faceFound": False,
                    "mode": "set",
                    "parameterValues": zeros,
                },
            },
        )
    except Exception:
        pass


async def _idle_deactivate_expression(expr_name: str | None) -> bool:
    """Matikan satu file ArtiIdle{N} di idle websocket."""
    name = (expr_name or _get_idle_active_expr() or "").strip()
    if not name or not _idle_ws_ok():
        return False
    try:
        await _idle_send(
            _idle_ws,
            {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "IdleExprOff",
                "messageType": "ExpressionActivationRequest",
                "data": {"expressionFile": f"{name}.exp3.json", "active": False},
            },
        )
        if _get_idle_active_expr() == name:
            _set_idle_active_expr(None)
        print(f"[Idle] Deactivated {name}")
        return True
    except Exception as e:
        print(f"[Idle] Gagal deactivate {name}: {e}")
        return False


async def _idle_trigger_hotkey_by_name(name: str) -> bool:
    """Fire VTS hotkey by name on idle websocket (e.g. motion stop / pose reset)."""
    if not name or not _idle_ws_ok():
        return False
    hid = _idle_hotkey_cache.get(name)
    if not hid:
        return False
    try:
        resp = await _idle_send(
            _idle_ws,
            {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "IdleHotkeyCmd",
                "messageType": "HotkeyTriggerRequest",
                "data": {"hotkeyID": hid},
            },
        )
        if resp.get("messageType") == "APIError":
            print(f"[Idle/Hotkey] VTS error '{name}': {resp.get('data', {}).get('message', '?')}")
            return False
        print(f"[Idle/Hotkey] ■ {name} triggered (interrupt motion)")
        return True
    except Exception as e:
        print(f"[Idle/Hotkey] Gagal trigger '{name}': {e}")
        return False


async def _idle_cmd_loop() -> None:
    """Proses hotkey + deactivate ArtiIdle dari thread utama."""
    while True:
        while True:
            try:
                cmd = _idle_hotkey_cmd_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(cmd, tuple) and cmd and cmd[0] == "off_expr":
                await _idle_deactivate_expression(cmd[1] if len(cmd) > 1 else None)
            elif isinstance(cmd, str) and cmd.strip():
                await _idle_trigger_hotkey_by_name(cmd.strip())
        await asyncio.sleep(0.05)


async def _idle_face_override_loop() -> None:
    """Terapkan nod / reset FaceAngleY ke idle ws (nod kelihatan saat smooth idle pernah jalan)."""
    while True:
        latest = None
        while True:
            try:
                latest = _idle_face_y_queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            try:
                await _idle_inject_face_y_set(latest)
            except Exception:
                pass
        await asyncio.sleep(1.0 / 24)


async def _idle_worker_main():
    """Satu worker thread persisten: pause/resume track, tanpa spawn duplikat."""
    global idle_timer_running, _idle_ws, _idle_ws_lock, _idle_startup_cleanup_done, _idle_worker_loop

    _idle_worker_loop = asyncio.get_running_loop()
    _idle_ws_lock = asyncio.Lock()
    motion_ids: dict = {}
    retry_sec = float(CONFIG.get("idle_vts_connect_retry_sec", 15))

    while not _idle_ws_ok():
        _idle_ws = await _idle_connect_ws(max_attempts=3)
        if _idle_ws:
            break
        print(f"[Idle] VTS belum siap — retry dalam {retry_sec:.0f}s (motion idle off sementara)")
        await asyncio.sleep(retry_sec)

    if not _idle_startup_cleanup_done:
        await _idle_cleanup_expressions(_idle_ws)
        _idle_startup_cleanup_done = True

    motion_ids = await _discover_motion_hotkey_ids(_idle_ws)
    if not motion_ids:
        print("[Idle] No IdleMotion hotkeys found in VTS, motion track disabled.")

    override_task = asyncio.create_task(_idle_face_override_loop())
    hotkey_cmd_task = asyncio.create_task(_idle_cmd_loop())

    # DUA TRACK DIURUS TERPISAH ([date removed], permintaan operator: "sambil
    # ngomong, sambil mikir, motion tetep jalan").
    #
    # Dulu keduanya digerbangi satu bendera `idle_timer_running`, jadi begitu
    # giliran mulai KEDUANYA mati. `idle_motion_lanjut_saat_bicara` cuma
    # melewati hotkey penghenti, sehingga motion yang SEDANG main boleh habis
    # (10 dtk) — tapi tidak ada motion baru sepanjang sisa giliran, padahal
    # giliran p50-nya 29,7 dtk (sesi [date removed]). Hasilnya Arti tetap membeku
    # selama dua pertiga waktu bicaranya.
    #
    # Track EKSPRESI tetap ikut `idle_timer_running` dan HARUS tetap mati saat
    # bicara: ArtiIdle* dan ArtiBicara sama-sama blend Add dan berbagi 43
    # parameter bernilai identik — dibiarkan hidup bersamaan, potret wajah yang
    # sama ditambahkan DUA KALI.
    motion_task: asyncio.Task | None = None
    expr_task: asyncio.Task | None = None

    try:
        while True:
            # --- track MOTION ---
            if _motion_run_state() is not None:
                if motion_task is None or motion_task.done():
                    motion_task = asyncio.create_task(_motion_track(motion_ids))
            elif motion_task is not None:
                if not motion_task.done():
                    motion_task.cancel()
                await asyncio.gather(motion_task, return_exceptions=True)
                motion_task = None

            # --- track EKSPRESI ---
            if idle_timer_running:
                if expr_task is None or expr_task.done():
                    expr_task = asyncio.create_task(_expression_track())
            elif expr_task is not None:
                if not expr_task.done():
                    expr_task.cancel()
                await asyncio.gather(expr_task, return_exceptions=True)
                expr_task = None
                await _idle_deactivate_expression(_get_idle_active_expr())
                if _motion_run_state() is None:
                    # Reset sudut wajah HANYA kalau motion juga berhenti —
                    # kalau motion masih jalan, reset ini melawan kurvanya.
                    await _idle_reset_face_angles()
            await asyncio.sleep(0.2)
    finally:
        override_task.cancel()
        hotkey_cmd_task.cancel()
        await asyncio.gather(override_task, hotkey_cmd_task, return_exceptions=True)


async def _discover_motion_hotkey_ids(ws):
    """Query VTS for actual hotkey IDs matching our IdleMotion names."""
    global _idle_hotkey_cache
    try:
        resp = await _idle_send(ws, {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "IdleDiscoverHotkeys",
            "messageType": "HotkeysInCurrentModelRequest",
            "data": {}
        })
        hotkeys = resp.get("data", {}).get("availableHotkeys", [])
        daftar = _idle_motion_names()
        found = {}
        for hk in hotkeys:
            _idle_hotkey_cache[hk["name"]] = hk["hotkeyID"]
            if hk["name"] in daftar:
                found[hk["name"]] = hk["hotkeyID"]
                print(f"[Idle] Motion hotkey found: {hk['name']} -> {hk['hotkeyID']}")
        stop_name = (CONFIG.get("idle_motion_stop_hotkey") or "").strip()
        if stop_name:
            if stop_name in _idle_hotkey_cache:
                print(f"[Idle] Motion-stop hotkey found: {stop_name}")
            else:
                print(
                    f"[Idle] WARN: idle_motion_stop_hotkey='{stop_name}' "
                    "tidak ada di VTS — buat hotkey di model"
                )
        return found
    except Exception as e:
        print(f"[Idle] Error discovering hotkeys: {e}")
        return {}


async def _motion_track(motion_ids):
    """Track 1: Trigger motion hotkeys periodically for smooth body movement."""
    global idle_timer_running, _idle_ws, tts_is_playing

    if not motion_ids:
        return  # No motions available

    motion_names = list(motion_ids.keys())
    last_motion = None

    sambung = bool(CONFIG.get("idle_motion_sambung", False))
    pertama = True

    # Izin dibaca ulang sepanjang lifecycle; jangan menangkap flag sekali di
    # awal karena turn/PTT/shutdown dapat berubah saat interval sedang tidur.
    while _motion_run_state() is not None:
        try:
            if sambung and pertama:
                # Tembak SEKARANG. Tanpa ini, tiap resume sesudah giliran
                # memulai hitungan 25-40 dtk dari nol dan giliran berikutnya
                # keburu datang — itulah kenapa motion tidak pernah muncul.
                wait = 0.0
            elif sambung:
                wait = max(1.0, float(CONFIG.get("idle_motion_ganti_sec", 9.0)))
            else:
                wait = random.uniform(MOTION_INTERVAL_MIN, MOTION_INTERVAL_MAX)
            pertama = False
            if wait:
                await asyncio.sleep(wait)

            # Pemeriksaan kedua: state bisa berubah selama interval tidur.
            if _motion_run_state() is None:
                continue

            # Pick random motion (no repeat)
            motion = random.choice(motion_names)
            while motion == last_motion and len(motion_names) > 1:
                motion = random.choice(motion_names)
            last_motion = motion

            # Pemeriksaan terakhir tepat sebelum hotkey dikirim. Ini menutup
            # celah turn selesai / PTT / shutdown setelah motion dipilih.
            state = _motion_run_state()
            if state is None:
                continue

            hotkey_id = motion_ids[motion]
            payload = {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "IdleMotionTrigger",
                "messageType": "HotkeyTriggerRequest",
                "data": {"hotkeyID": hotkey_id}
            }

            resp = await _idle_send(_idle_ws, payload)
            if resp.get("messageType") == "APIError":
                print(f"[Idle/Motion] VTS Error: {resp.get('data',{}).get('message','?')}")
            else:
                print(f"[Idle/Motion] ▶ {motion} triggered (state={state})")

        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed:
            print("[Idle/Motion] VTS disconnected, reconnecting...")
            _idle_ws = await _idle_reconnect(_idle_ws)
            if not _idle_ws:
                print("[Idle/Motion] Reconnect failed, stopping motion track.")
                return
            motion_ids = await _discover_motion_hotkey_ids(_idle_ws)
        except Exception as e:
            print(f"[Idle/Motion] Error: {e}")
            await asyncio.sleep(3)


async def _expression_track():
    """Track 2: Smooth head/eye movement via tracking parameter injection.
    Uses FaceAngleX/Y/Z (tracking params, NOT Live2D params) with
    InjectParameterDataRequest for buttery smooth 2.5s transitions.
    No more expression toggle snapping!"""
    global idle_timer_running, idle_expression_active, _idle_ws, tts_is_playing

    # Map expression file params → VTS tracking param names
    PARAM_MAP = {
        "ParamAngleX": "FaceAngleX",   # Head horizontal (-30 to 30)
        "ParamAngleY": "FaceAngleY",   # Head vertical (-30 to 30)
        "ParamAngleZ": "FaceAngleZ",   # Head tilt (-90 to 90)
    }
    TRACKING_IDS = ("FaceAngleX", "FaceAngleY", "FaceAngleZ")

    # Load target poses from expression files.
    # Prioritas: CONFIG["vts_model_dir"] (config_local.json) > env VTS_MODEL_DIR.
    MODEL_DIR = (
        CONFIG.get("vts_model_dir")
        or os.environ.get("VTS_MODEL_DIR")
        or r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio"
          r"\VTube Studio_Data\StreamingAssets\Live2DModels\YOUR_MODEL"
    )
    if "YOUR_MODEL" in MODEL_DIR:
        print("[Idle/Expr] vts_model_dir belum diisi di config_local.json — "
              "pose halus (ArtiIdle1-50) tidak akan dimuat.")
    # Audit ekspresi mood pakai folder yang sama — tanpa ini ia membaca placeholder
    # YOUR_MODEL dan WARN "tidak punya param alis/mata" menyala palsu tiap jawaban.
    arti_expression_runtime.set_vts_mood_dir(MODEL_DIR)
    poses = {}
    for name in IDLE_EXPRESSIONS:
        fpath = os.path.join(MODEL_DIR, f"{name}.exp3.json")
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            angles = {"FaceAngleX": 0.0, "FaceAngleY": 0.0, "FaceAngleZ": 0.0}
            for p in data.get("Parameters", []):
                if p["Id"] in PARAM_MAP:
                    angles[PARAM_MAP[p["Id"]]] = float(p["Value"])
            poses[name] = angles
        except Exception:
            pass

    if not poses:
        print("[Idle/Expr] No poses loaded, smooth idle disabled")
        return

    print(f"[Idle/Expr] Loaded {len(poses)} smooth poses (FaceAngle injection)")

    # Current position (starts at neutral)
    current = {"FaceAngleX": 0.0, "FaceAngleY": 0.0, "FaceAngleZ": 0.0}
    last_name = None

    FPS = 10
    FRAME_TIME = 1.0 / FPS
    TRANSITION_SECS = 2.5

    def _smoothstep(t):
        """Ease-in-out curve for natural movement."""
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    async def _inject(angles):
        """Send tracking param injection to VTS."""
        if not _idle_ws_ok():
            raise websockets.exceptions.ConnectionClosed(None, None)
        params = [{"id": pid, "weight": 1.0, "value": angles[pid]} for pid in TRACKING_IDS]
        await _idle_send(_idle_ws, {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "SmoothIdle",
            "messageType": "InjectParameterDataRequest",
            "data": {
                "faceFound": False,
                "mode": "add",
                "parameterValues": params
            }
        })

    while idle_timer_running:
        try:
            # Pick random target pose (no repeat)
            expr_name = random.choice(list(poses.keys()))
            while expr_name == last_name and len(poses) > 1:
                expr_name = random.choice(list(poses.keys()))
            target = poses[expr_name]

            if not idle_timer_running or _idle_paused():
                await asyncio.sleep(0.5)
                continue

            idle_expression_active = True
            _set_idle_active_expr(expr_name)

            # === SMOOTH TRANSITION over 2.5 seconds ===
            start = {pid: current[pid] for pid in TRACKING_IDS}
            steps = int(TRANSITION_SECS * FPS)

            for step in range(steps):
                if not idle_timer_running or _idle_paused():
                    break
                t = _smoothstep((step + 1) / steps)
                for pid in TRACKING_IDS:
                    current[pid] = start[pid] + (target[pid] - start[pid]) * t
                await _inject(current)
                await asyncio.sleep(FRAME_TIME)

            for pid in TRACKING_IDS:
                current[pid] = target[pid]

            last_name = expr_name
            print(f"[Idle/Expr] → {expr_name}")

            hold_time = random.uniform(EXPR_HOLD_MIN, EXPR_HOLD_MAX)
            hold_end = time.time() + hold_time
            while time.time() < hold_end and idle_timer_running and not _idle_paused():
                await _inject(current)
                await asyncio.sleep(0.5)

        except websockets.exceptions.ConnectionClosed:
            global _idle_expr_backoff, _idle_ws
            idle_expression_active = False
            _idle_expr_backoff = min(15.0, (_idle_expr_backoff or 2.0) * 1.5)
            print(f"[Idle/Expr] VTS disconnected, backoff {_idle_expr_backoff:.1f}s...")
            await asyncio.sleep(_idle_expr_backoff)
            _idle_ws = await _idle_reconnect(_idle_ws)
            if _idle_ws_ok():
                _idle_expr_backoff = 0.0
        except Exception as e:
            idle_expression_active = False
            print(f"[Idle/Expr] Error: {e}")
            await asyncio.sleep(3)

    # Cleanup: fade back to neutral on exit
    try:
        start = {pid: current[pid] for pid in TRACKING_IDS}
        steps = int(1.0 * FPS)
        for step in range(steps):
            t = _smoothstep((step + 1) / steps)
            for pid in TRACKING_IDS:
                current[pid] = start[pid] * (1.0 - t)
            await _inject(current)
            await asyncio.sleep(FRAME_TIME)
    except Exception:
        pass
    idle_expression_active = False



def start_idle_animation():
    """Resume idle tracks (satu worker thread — jangan spawn duplikat)."""
    global idle_timer_thread, idle_timer_running, idle_expression_active
    idle_timer_running = True
    idle_expression_active = False
    if idle_timer_thread is not None and idle_timer_thread.is_alive():
        return
    idle_timer_thread = threading.Thread(target=idle_animation_worker, daemon=True, name="idle-vts")
    idle_timer_thread.start()


def stop_idle_animation():
    """Pause idle tracks + OFF ArtiIdle aktif + reset head di idle ws."""
    global idle_timer_running
    idle_timer_running = False
    expr = _get_idle_active_expr()
    _queue_idle_deactivate_expr()
    try:
        _idle_face_y_queue.put_nowait(0.0)
    except Exception:
        pass
    label = expr or "(none)"
    print(f"[Idle] Paused — deactivate {label} queued, face reset")


# ==========================================
# 5. MAIN ORCHESTRATOR LOOP
# ==========================================
async def main_loop():
    print("=== HERMES VTUBER BRIDGE (CONTEXT BUFFER ENHANCED) ===")
    
    global main_event_loop, _brain_busy, _brain_busy_since
    main_event_loop = asyncio.get_event_loop()
    
    # 1. Hubungkan ke VTube Studio
    global vts
    vts = VTSController()
    await vts.connect()
    
    # 2. Inisialisasi TTS
    # `tts` is module-level (declared global) so the __main__ finally cleanup can
    # reach tts.supertone for a bounded shutdown of the Supertone subprocess
    # (task 7.1, Req 10.5).
    global tts
    tts = TTSEngine()

    if CONFIG.get("tts_engine") == "supertone" and CONFIG.get("supertonic_prewarm_on_startup", True):
        try:
            print("[TTS] Pre-warming Supertone (load model venv312)...")
            await tts.supertone.ensure_alive()
            print("[TTS] Supertone ready ✓")
        except Exception as e:
            print(
                f"[TTS] Supertone pre-warm gagal ({type(e).__name__}: {e}); "
                "fallback edge_tts per jawaban sampai model siap"
            )
    
    # 3. Hotkey + YouTube (ASR mic setelah health check — hindari kalibrasi bentrok)
    if CONFIG.get("youtube_chat_enabled"):
        threading.Thread(target=youtube_chat_worker, daemon=True).start()
        start_yt_viewer_count_worker()
    if CONFIG.get("minecraft_enabled"):
        # Runner TIDAK autostart — join lewat 'mc on' atau suruh verbal
        # ([MC: join] dari jawaban Arti). Di sini cuma pengumuman siaga.
        print(
            "[Minecraft] Siaga — 'mc on' untuk join, 'mc off' keluar, "
            "atau suruh Arti verbal ikut main"
        )
    start_host_topic_workers()
    init_global_hotkey()
    init_vision_hotkey()

    if CONFIG.get("health_check_on_startup", True):
        _hc_cfg = {
            **CONFIG,
            "openrouter_api_key": (
                CONFIG.get("openrouter_api_key")
                or os.environ.get("OPENROUTER_API_KEY")
                or openrouter_api_key
            ),
        }
        _health = bridge_health.run_startup_health_check(
            _hc_cfg,
            resolve_mic_fn=resolve_asr_input_device,
            vts=vts,
            tts=tts,
            hotkey_registered=hotkey_registered,
        )
        bridge_health.print_health_report(_health)
        if CONFIG.get("vision_enabled", CONFIG.get("screen_context_enabled", False)):
            vrows = bridge_health.probe_vision_providers(CONFIG)
            if vrows:
                print("\n  --- VISION PROVIDERS ---")
                for row in vrows:
                    print(f"  [{row.status.ljust(4)}] {row.name:<18} {row.detail}")
        if CONFIG.get("scouter_enabled", True):
            srows = bridge_health.probe_scouter_providers(CONFIG)
            if srows:
                print("\n  --- SCOUTER PROVIDERS ---")
                for row in srows:
                    print(f"  [{row.status.ljust(4)}] {row.name:<18} {row.detail}")
        if CONFIG.get("vault_rag_enabled", True):
            rag_row = bridge_health.probe_rag_origin_canon(CONFIG)
            print(f"\n  [{rag_row.status.ljust(4)}] {rag_row.name:<18} {rag_row.detail}")

    threading.Thread(target=voice_listener_worker, daemon=True).start()
    start_text_input_worker()
    start_desktop_audio_worker()
    start_screen_watcher_worker()
    
    # 4. Background scouter (multi-provider digest)
    start_scouter()
    
    # 5. Jalankan Idle Animation System (RNG-based)
    start_idle_animation()
    
    memories = load_long_term_memories()
    memory_block = build_startup_memory_block(memories)
    
    # Load dynamic context: soul, mood, viewer
    soul_context = load_soul_context()
    viewer_context = load_viewer_context()
    current_mood = get_current_mood()
    
    # mood SENGAJA tidak lagi ditempel di sini — lihat mood_block_now().
    # Blok statis di startup membuat mood beku sepanjang sesi.
    # (viewer_context hanya untuk print hitungan di bawah; injeksinya per-turn)
    
    # Summarizer context (update tiap 5 trigger, dari OpenRouter)
    summarizer_context = get_summarizer_context()
    
    origin_block = build_origin_context()
    # FIX P1: Build system prompt — only add non-empty blocks
    dynamic_system_prompt = _SYSTEM_PROMPT_BASE + origin_block + memory_block
    # viewer_block SENGAJA tidak lagi ditempel di sini ([date removed]). Dump statis semua
    # penonton (~230 token) ikut TIAP turn walau tidak ada yang chat — dan bikin prompt
    # jebol cap. Sekarang per-turn: viewer_block_for(trigger.viewer_name) di
    # _handle_voice_trigger hanya saat penonton itu benar-benar chat; penonton yang
    # cuma disebut tetap terjangkau lewat vault RAG (ARTI_VIEWERS.md ter-index).
    if summarizer_context:
        dynamic_system_prompt += summarizer_context
    dynamic_system_prompt = arti_expression_runtime.emotion_prompt_for_system(
        dynamic_system_prompt, CONFIG
    )
    print(f"[Mood] Current mood: {current_mood}")
    if viewer_context:
        print(f"[Viewer] {viewer_context.count(chr(10))} viewer entries loaded")
    if summarizer_context:
        print(f"[Summarizer] Context injected: {summarizer_context[:80]}...")
    if CONFIG.get("vault_rag_enabled", True):
        try:
            arti_vault_rag.init_db(CONFIG)
            rag_st = arti_vault_rag.index_stats(CONFIG)
            print(
                f"[Vault RAG] Index: {rag_st['chunks']} chunk, "
                f"{rag_st['embedded']} embedded — live top-{CONFIG.get('vault_rag_top_k', 5)} per jawab"
            )
            if rag_st["chunks"] == 0:
                print("[Vault RAG] DB kosong — jalankan: python arti_vault_rag.py --reindex-all")
            # Potongan yang TERINDEKS tapi belum ter-embed itu tidak terlihat oleh
            # pencarian, dan sebelumnya tidak ada yang menyuarakannya: cek naif
            # "sudah terindeks?" menjawab SUDAH. Ditemukan [date removed] di observer
            # RAG — satu berkas beats (40 potongan, 12.931 char) gagal di-embed
            # sepenuhnya, jadi seluruh ingatan sesi 3 Agustus raib dari pencarian
            # sampai di-reindex ulang. Angkanya sudah ada di index_stats; yang
            # kurang cuma mulutnya.
            elif rag_st["embedded"] < rag_st["chunks"]:
                selisih = rag_st["chunks"] - rag_st["embedded"]
                print(
                    f"[Vault RAG] PERINGATAN: {selisih} potongan belum ter-embed — "
                    "ingatan itu TIDAK akan ketemu. Jalankan: "
                    "python arti_vault_rag.py --reindex-all"
                )
        except Exception as e:
            print(f"[Vault RAG] Init warning: {e}")
    print(
        f"[LLM] System prompt base ~{len(trim_system_prompt_for_llm(dynamic_system_prompt))} chars "
        f"(memori penuh {len(memories)} bullet -> RAG, bukan dump)"
    )
    
    # Schedule in-process Subtitle Server (Req 3.1, 3.2, 3.5, 3.7, 3.8, [time removed]).
    # Strictly additive: failures here are logged and swallowed so VTS / LLM /
    # YouTube startup paths remain untouched.
    subtitle_runtime.enabled = bool(CONFIG.get("subtitle_enabled", True))
    subtitle_runtime.status_enabled = bool(CONFIG.get("subtitle_status_enabled", True))
    if subtitle_runtime.enabled:
        try:
            port_raw = CONFIG.get("subtitle_port", 9999)
            port = int(port_raw)
            if not (0 <= port <= 65535):
                raise ValueError(f"port {port_raw} out of range (0..65535)")
            subtitle_runtime.port = port
            subtitle_runtime.server_task = asyncio.create_task(
                start_subtitle_server(port)
            )
        except Exception as e:
            print(f"[SubTitle] Skipping server start: {type(e).__name__}: {e}")
            subtitle_runtime.server_started = False
    else:
        print("[SubTitle] Disabled via CONFIG['subtitle_enabled']")
    
    profile = CONFIG.get("active_profile", "default").lower()
    try:
        session_transcript.init_session_artifacts(CONFIG)
    except Exception as e:
        print(f"[Transcript] init gagal: {e}")

    add_to_history("System", f"Live stream dimulai. Arti aktif menemani streamer (Profil: {profile}).")
    
    # Cek API Key berdasarkan Provider
    provider = CONFIG["api_provider"].lower()
    if provider == "gemini":
        key_ok = CONFIG["gemini_api_key"] and CONFIG["gemini_api_key"] != "YOUR_GEMINI_API_KEY"
        if not key_ok:
            print("\n[PERINGATAN] Silakan pasang Google AI Studio API Key kamu (GEMINI_API_KEY)!")
        else:
            print(f"\n[Info] Menggunakan Google AI Studio (HTTP API) dengan model: {CONFIG['gemini_model']} (Profil: {profile})")
    elif provider == "groq":
        key_ok = CONFIG["groq_api_key"] and CONFIG["groq_api_key"] != "YOUR_GROQ_API_KEY"
        if not key_ok:
            print("\n[PERINGATAN] Silakan pasang Groq API Key kamu (GROQ_API_KEY)!")
        else:
            models = CONFIG.get('groq_models', ['openai/gpt-oss-120b'])
            print(f"\n[Info] Menggunakan Groq (Rolling {len(models)} model) (Profil: {profile})")
            for i, m in enumerate(models):
                print(f"    [{i+1}] {m}")
    elif provider == "sambanova":
        key_ok = CONFIG["sambanova_api_key"] and CONFIG["sambanova_api_key"] != "YOUR_SAMBANOVA_API_KEY"
        if not key_ok:
            print("\n[PERINGATAN] Silakan pasang SambaNova API Key kamu (SAMBANOVA_API_KEY)!")
        else:
            print(f"\n[Info] Menggunakan SambaNova (Super Cepat) dengan model: {CONFIG['sambanova_model']} (Profil: {profile})")
    else:
        # Default fallback to gemini_live if provider is unrecognized or gemini_live
        if provider != "gemini_live":
            print(f"\n[Info] Provider '{provider}' tidak dikenal atau tidak aktif, otomatis menggunakan 'gemini_live'.")
            CONFIG["api_provider"] = "gemini_live"
            
        key_ok = CONFIG["gemini_api_key"] and CONFIG["gemini_api_key"] != "YOUR_GEMINI_API_KEY"
        if not key_ok:
            print("\n[PERINGATAN] Silakan pasang Google AI Studio API Key kamu (GEMINI_API_KEY)!")
        else:
            print(f"\n[Info] Menggunakan Google AI Studio (Live WebSocket API - UNLIMITED RPD) dengan model: {CONFIG['gemini_model']} (Profil: {profile})")
    
    if CONFIG.get("vault_rag_reindex_on_startup", True):
        threading.Thread(
            target=lambda: arti_vault_rag.reindex_startup_catchup(CONFIG),
            name="vault-rag-catchup",
            daemon=True,
        ).start()

    if CONFIG.get("observer_catchup_on_startup", True):
        def _observer_catchup_worker() -> None:
            time.sleep(float(CONFIG.get("observer_catchup_delay_sec", 90.0)))
            try:
                import arti_observer_catchup as obs_catchup
                import arti_observer_progress as obs_progress
                _maju = obs_progress.make_progress_callback("Observer catch-up")
                _jeda = float(CONFIG.get("observer_catchup_pause_sec", 3.0))

                def _pelan(stage: str, i: int, total: int, label: str) -> None:
                    _maju(stage, i, total, label)
                    if stage != "summarize" or i >= total:
                        return
                    if _jeda > 0:
                        time.sleep(_jeda)   # jangan berebut provider dengan live
                    # MENGALAH ke giliran suara: selama Arti mikir/bicara,
                    # kerja latar berhenti. Maks 60 dtk per segmen supaya
                    # sesi ramai tidak membekukan catch-up selamanya.
                    _batas = time.time() + 60.0
                    while time.time() < _batas and (_brain_busy or tts_is_playing):
                        time.sleep(1.0)

                obs_catchup.run_catchup(
                    CONFIG,
                    current_session_id=session_transcript.get_session_id(CONFIG) or "",
                    on_progress=_pelan,
                )
            except Exception as e:  # noqa: BLE001 — catch-up gagal jangan ganggu live
                print(f"[Observer] Catch-up startup gagal: {type(e).__name__}: {e}")

        threading.Thread(
            target=_observer_catchup_worker,
            name="observer-catchup",
            daemon=True,
        ).start()

    def _prewarm_cursor_roles() -> None:
        # Bayar cold start scout/vision di startup, bukan di tengah siaran:
        # grok cold ~14 dtk, vision cold + gambar ~36 dtk (terukur [date removed]).
        # Tanpa pemanas, panggilan pertama tiap role bisa timeout -> sesi
        # dibuang -> dingin lagi (jebakan yang sama dengan prewarm voice dulu).
        try:
            arti_agy_agent.prewarm(CONFIG)
        except Exception as e:  # noqa: BLE001
            print(f"[Agy] pemanas startup gagal: {type(e).__name__}: {e}")
        # Kuota AWAL sesi. Dibaca di thread pemanas (bukan jalur boot) supaya
        # 2-3 detik `/usage` tidak menunda apa pun, dan gagal-diam kalau agy
        # mati. Pasangannya dicetak lagi saat shutdown -> selisihnya = ongkos
        # sesi ini, tanpa operator perlu ingat mengeceknya.
        try:
            arti_agy_agent.lapor_kuota("awal sesi", CONFIG)
        except Exception as e:  # noqa: BLE001
            print(f"[Agy] baca kuota awal gagal: {type(e).__name__}: {e}")
        try:
            import arti_cursor_agent as _ca  # noqa: PLC0415

            # Sesi VOICE ikut dipanaskan dari startup — dulu nunggu trigger
            # pertama, jadi inisiatif/chat awal sesi selalu kena "sesi belum
            # hangat" (sore3 [date removed]: 1 slot inisiatif hangus di menit 1,5).
            # prewarm() tidak memblokir: dia menyalakan thread-nya sendiri,
            # scout/vision di bawah tetap jalan paralel.
            if CONFIG.get("cursor_trigger_types") and _ca.is_available(CONFIG)[0]:
                _ca.prewarm(CONFIG)

            # Observer SENGAJA tidak dipanaskan: dia cuma hidup sekali saat
            # shutdown (12 jam setelah startup — sesi pasti sudah kedaluwarsa),
            # dan role_timeout_sec("observer")=60 sudah menampung cold start.
            # Chain observer JANGAN digabung ke syarat pemanas scout: kalau
            # cursor cuma ada di chain observer, dulu yang dipanaskan justru
            # role yang salah (audit [date removed]).
            role_chains = {
                "scout": list(CONFIG.get("scouter_provider_chain") or []),
                "vision": list(CONFIG.get("vision_provider_chain") or []),
            }
            for role, chain in role_chains.items():
                if "cursor" not in chain:
                    continue
                r = _ca.send_task(role, "", "Balas persis satu kata: siap", CONFIG)
                print(f"[Cursor:{role}] pemanas: {r.reason} ({r.latency_ms}ms)")
        except Exception as e:  # noqa: BLE001 — pemanasan gagal bukan alasan gagal start
            print(f"[Cursor] pemanas role gagal: {type(e).__name__}: {e}")

    threading.Thread(
        target=_prewarm_cursor_roles, daemon=True, name="cursor-role-prewarm"
    ).start()

    if CONFIG.get("donation_enabled", False):
        import arti_donations

        arti_donations.start_donation_listeners(CONFIG, _on_donation)

    if CONFIG.get("video_enabled", False):
        start_video_watcher()

    # Jam hening inisiatif mulai dihitung dari SIAP — bukan dari import modul
    # (wizard bisa makan bermenit-menit; tanpa reset ini inisiatif langsung
    # menembak begitu loop jalan). Arti "dianggap baru bicara" saat SIAP =
    # 30 detik masa tenang pertama.
    global _last_human_activity_ts, _last_arti_reply_ts
    _last_human_activity_ts = time.time()
    _last_arti_reply_ts = time.time()

    print(f"\n🟢 SISTEM SIAP! [Profil: {profile}] Panggil Arti dengan 'eh arti' atau 'arti'...")
    print("--------------------------------------------------------------------------------")

    # State log transisi tidur/bangun proaktif (sekali per transisi, anti-spam).
    _proactive_dormant_logged = False

    # Loop Utama
    while True:
        await asyncio.sleep(0.1)

        # Detektor kehidupan: ruangan tanpa satu pun manusia (chat/mic) =
        # kedua jalur proaktif di bawah tidur; bangun saat timestamp maju.
        # KECUALI lagi main game — di situ Arti justru harus ngoceh terus
        # (komentator), lihat is_dormant.
        _in_game_now = _mc_runner_active()
        # Jaring pengaman AFK: operator pamit, lalu benar-benar hening -> Arti
        # ambil alih sendiri (tanpa ini, tag yang meleset = stream mati).
        if _afk_armed_ts and not _host_mode:
            _afk_gap = float(CONFIG.get("host_auto_after_afk_sec", 120.0))
            if (
                _afk_gap > 0
                and time.time() - _last_streamer_speech_ts >= _afk_gap
                and time.time() - _afk_armed_ts >= _afk_gap
            ):
                _set_host_mode(True, "jaring_afk")
        _mode_now = _session_mode()
        _mode_policy = arti_session_mode.mode_policy(_mode_now, CONFIG)
        _dormant_now = arti_curious.is_dormant(
            CONFIG,
            now=time.time(),
            last_human_ts=_last_human_activity_ts,
            mode=_mode_now,
        )
        if _dormant_now != _proactive_dormant_logged:
            _proactive_dormant_logged = _dormant_now
            if _dormant_now:
                print(
                    "[Initiative] Ruangan sepi total — proaktif tidur sampai "
                    "ada chat/suara streamer"
                )
            else:
                print("[Initiative] Ada tanda kehidupan — proaktif bangun lagi")

        # Curious proactive (idle commentary on screen). Ikut rehat backoff
        # provider (audit 3/8: backoff cuma memagari inisiatif — curious layar
        # tetap nembak provider yang lagi 429/tutup).
        if (
            CONFIG.get("curious_enabled")
            and not _dormant_now
            and time.time() >= _init_provider_fail_until
            and is_vision_active()
            # In-game: komentar layar OBS dibungkam (anti dobel dengan event
            # game + inisiatif minecraft_note) — kebijakan per mode.
            and (
                _mode_policy["screen_curious_allowed"]
                or not CONFIG.get("minecraft_mute_screen_curious", True)
            )
        ):
            yt_cooling = (time.time() - _last_yt_chat_trigger_ts) < 20.0
            with _brain_busy_lock:
                brain_busy = _brain_busy
            if arti_curious.should_fire(
                CONFIG,
                brain_busy=brain_busy,
                tts_playing=tts_is_playing,
                ptt_active=hotkey_active,
                yt_cooling=yt_cooling,
            ):
                if arti_curious.prepare_for_fire(CONFIG):
                    curious_text = arti_curious.build_prompt(CONFIG)
                    # CONFIG WAJIB dioper: tanpa itu _recent_hooks tidak pernah
                    # terisi -> dedup "hook terlalu mirip" mati total, dan
                    # curious mengulang sudut yang sama (audit [date removed];
                    # sejalan dengan keluhan operator soal Arti muter-muter topik).
                    arti_curious.mark_fired(CONFIG)
                    queue_voice_trigger(curious_text, trigger_type="curious")
                    print("[Curious] Proactive trigger queued")

        # Inisiatif — buka topik sendiri saat hening (Fitur A). Jalur saudara
        # curious: TANPA syarat layar/vision, hidup justru saat tidak ada apa-apa.
        # Ditahan selama media share diputar (Fitur E) DAN selama masih ada
        # trigger antre (temuan audit: inisiatif menembak pas hold habis padahal
        # reaksi video masih ngantre -> "stream hening" tepat setelah komentar).
        if (
            CONFIG.get("initiative_enabled", False)
            and not _dormant_now
            and time.time() >= _media_playback_until
            and voice_trigger_queue.empty()
            and len(voice_trigger_buffer) == 0
        ):
            with _brain_busy_lock:
                _busy_now = _brain_busy
            if arti_curious.should_fire_initiative(
                CONFIG,
                now=time.time(),
                last_arti_ts=_last_arti_reply_ts,
                last_streamer_ts=_last_streamer_speech_ts,
                tts_playing=tts_is_playing,
                brain_busy=_busy_now,
                ptt_active=hotkey_active,
                provider_fail_until=_init_provider_fail_until,
                last_human_ts=_last_human_activity_ts,
                mode=_mode_now,
            ):
                _init_text = arti_curious.build_initiative_prompt(
                    CONFIG,
                    streamer_baru_bicara=(
                        time.time() - _last_streamer_speech_ts
                        <= float(CONFIG.get("initiative_nyambung_sec", 45.0))
                    ),
                    ada_penonton=_ada_penonton(),
                    **_initiative_materials()
                )
                arti_curious.mark_initiative_fired()
                # Tetap trigger "curious" walau isinya komentar game: jalur
                # cepat (skip RAG, histori pendek) = komentar gesit, dan kalau
                # chat viewer masuk, ocehan game memang pantas kalah. Reaksi
                # EVENT (mati/diserang) yang kebal cull — itu type "game".
                queue_voice_trigger(_init_text, trigger_type="curious")
                if _in_game_now:
                    print("[Minecraft] Giliran komentar main game")
                elif _host_mode:
                    print("[Host] Giliran Arti ngisi siaran")
                else:
                    print("[Initiative] Hening terdeteksi — Arti buka topik sendiri")

        # Fitur E: selama media share diputar di layar, TAHAN konsumsi trigger —
        # Arti nonton bareng; antrean tetap utuh, dilanjut setelah playback.
        if time.time() < _media_playback_until:
            continue

        # Cek apakah ada trigger suara yang memanggil A
        try:
            use_buffer = CONFIG.get("voice_queue_enabled", False)
            if use_buffer:
                # Mode buffer: FIFO prioritas — TIDAK drain-newest, chat YT
                # yang antri tetap dijawab berurutan.
                queued = _dequeue_buffered_trigger_if_brain_ready()
                if queued is None:
                    raise queue.Empty
                raw = queued
            else:
                raw = voice_trigger_queue.get_nowait()

                # Drain-newest, TAPI donation/video/game/mic tidak boleh
                # tertimpa oleh trigger yang datang belakangan (orang sudah
                # bayar / nunggu playback / kejadian nyata di game yang sudah
                # lewat kalau telat dijawab / perintah yang operator KETIK di
                # console — lihat alasan lengkap di `always_queue`).
                def _ttype(r):
                    return getattr(r, "trigger_type", r[1] if isinstance(r, tuple) else "")

                while (
                    _ttype(raw) not in ("donation", "video", "game", "mic")
                    and not voice_trigger_queue.empty()
                ):
                    raw = voice_trigger_queue.get_nowait()

            trigger = _normalize_voice_trigger(raw)

            if _game_reaction_expired(trigger):
                if use_buffer:
                    with _brain_busy_lock:
                        _brain_busy = False
                        _brain_busy_since = 0.0
                continue

            if not use_buffer:
                with _brain_busy_lock:
                    if _brain_busy:
                        print(
                            "[Brain] Skip trigger — Arti masih proses jawaban sebelumnya "
                            "(hemat CPU/RAG/VTS)"
                        )
                        continue
                    _brain_busy = True
                    _brain_busy_since = time.time()

            try:
                await _handle_voice_trigger(trigger, memories, dynamic_system_prompt)
            finally:
                with _brain_busy_lock:
                    _brain_busy = False
                    _brain_busy_since = 0.0
                # Idle animation SELALU dipulihkan. Audit [date removed]:
                # `_prepare_turn_start` mematikan idle di SETIAP giliran, tapi
                # yang menyalakannya lagi cuma cabang SUKSES — satu giliran
                # gagal (provider 429 / jawaban tersaring) = model membeku
                # tanpa gerak sampai giliran sukses berikutnya. Untuk trigger
                # non-PTT (yt_chat/curious/game = mayoritas saat in-game) tidak
                # ada penyelamat lain. Aman dipanggil dua kali: ia membatalkan
                # tugas sebelumnya dulu.
                _schedule_post_answer_cleanup()

        except queue.Empty:
            continue
        except Exception as e:
            print(f"[Error] Masalah di main loop: {e}")
            with _brain_busy_lock:
                _brain_busy = False
                _brain_busy_since = 0.0
            await vts.trigger_expression_state("default")


def _append_screen_context(llm_system: str) -> str:
    """Inject [LAYAR:] from vision ring (independent of watch party)."""
    if not is_vision_active():
        return llm_system
    # Gerbang layar-diam ([date removed]): Arti menonton dirinya sendiri, layar nyaris
    # tak berubah -> deskripsi lama disuntik ulang tiap giliran dan dia
    # "komentarin itu mulu" (49/211 jawaban menyebut layar, log [time removed]).
    # Layar yang sudah beberapa kali sama = bukan bahan baru: berhenti suntik
    # sampai ada perubahan nyata (ambang piksel di arti_vision_client).
    if arti_vision_client.layar_sedang_diam(CONFIG):
        return llm_system
    screen_line = arti_screen_context.format_screen_context(
        max_chars=int(CONFIG.get("screen_context_max_chars", 200))
    )
    if not screen_line:
        return llm_system
    block = f"[LAYAR: {screen_line}]"
    return llm_system + "\n\n" + block


def _append_watch_party_context(llm_system: str) -> str:
    """Inject watch-party episode context (no duplicate [LAYAR:] — see _append_screen_context)."""
    if not CONFIG.get("watch_party_enabled"):
        return llm_system
    parts: list[str] = []
    event_id = (CONFIG.get("watch_party_event_id") or "").strip()
    ws = arti_screen_context.watch_state
    if event_id:
        parts.append(f"[EVENT: watch-party / {event_id}]")
    dialogue = arti_desktop_audio.dialogue_ring.format_context(max_lines=20)
    if dialogue:
        parts.append(f"[DIALOGUE TERDENGAR]\n{dialogue}")
    playback = ws.playback_mmss
    if playback:
        parts.append(f"[POSISI PUTAR: {playback}]")
    if event_id and playback:
        window = int(CONFIG.get("watch_party_rag_window_sec", 45))
        hits = arti_vault_rag.search_by_timecode(
            event_id,
            playback,
            CONFIG,
            window_before_sec=window,
        )
        ep_block = arti_vault_rag.format_hits_for_prompt(
            hits,
            int(CONFIG.get("vault_rag_max_context_chars", 1200)),
        )
        if ep_block:
            parts.append(ep_block.replace("[VAULT RAG", "[KONTEKS EPISODA"))
    if not parts:
        return llm_system
    block = "\n\n".join(parts)
    print(f"[Watch Party] Inject {len(block)} chars context")
    return llm_system + "\n\n" + block


def _append_live_context(llm_system: str) -> str:
    """Mode sesi + screen + desktop audio + minecraft + optional watch party."""
    llm_system = _append_host_context(llm_system)
    llm_system = _append_screen_context(llm_system)
    llm_system = _append_desktop_audio_context(llm_system)
    llm_system = _append_minecraft_context(llm_system)
    return _append_watch_party_context(llm_system)


def _ada_penonton() -> bool:
    """Ada manusia yang menonton? Dua sinyal, keduanya jujur:
    jumlah penonton live (>0; -1 = bukan live/belum diketahui) atau ada
    yang chat dalam 10 menit terakhir. Nol dua-duanya = dia sendirian.

    streamer SENDIRI tidak dihitung sebagai penonton. Live 14 Agu 2026: tiga
    pesan tes "asd"/"asdas"/"d" dari @streamer_test membuat fungsi ini True,
    register SENDIRIAN tidak pernah menyala, dan Arti menyapa "Penonton" di
    9 dari 12 balasan padahal siarannya offline. Dia menguji streamnya
    sendiri — itu bukan audiens.
    """
    if _yt_viewer_count > 0:
        return True
    cutoff = time.time() - 600.0
    pemilik = arti_session_mode.owner_handles(CONFIG)
    return any(
        ts >= cutoff
        and arti_session_mode.normalize_handle(nama) not in pemilik
        for nama, ts in _yt_viewers_seen.items()
    )


def _append_host_context(llm_system: str) -> str:
    """Blok mode sesi: streamer lagi nemenin, atau Arti yang pegang siaran.

    Selalu ada (bukan cuma saat host mode) — saat streamer hadir pun Arti perlu
    tahu CARA pamitnya, supaya kalimat "aku afk ya" bisa dia terjemahkan jadi
    tag tanpa streamer menyentuh keyboard.
    """
    if not CONFIG.get("host_mode_enabled", True):
        return llm_system
    if not _host_mode:
        return llm_system + (
            "\n\n[SIARAN: Bohan lagi nemenin kamu.] Kalau dia pamit pergi/AFK "
            "atau nitip siaran ke kamu, terima dengan santai dan tutup "
            "jawabanmu dengan tag [MODE: host] — mulai saat itu kamu yang "
            "pegang siaran. Tag dieksekusi sistem, JANGAN disebut/dibaca."
        )
    if not _ada_penonton():
        # Keluhan operator [date removed] (tes offline, dia AFK): "kadang dia bilang
        # 'iya ini aku masih on kok' — kayak jawab pertanyaan". Blok host
        # normal menyuruhnya bicara KE PENONTON; saat penontonnya NOL, dia
        # jadi menjawab hadirin imajiner (pamitan operator di history terbaca
        # sebagai pertanyaan baru tiap giliran). Sendirian = register MONOLOG.
        return llm_system + (
            "\n\n[SENDIRIAN — tidak ada siapa-siapa: Bohan AFK dan belum ada "
            "satu pun penonton.]\n"
            "TIDAK ADA yang bertanya dan TIDAK ADA yang perlu dijawab. Jangan "
            "menjawab pertanyaan yang tidak ada: jangan bilang \"iya aku masih "
            "on\", jangan melapor status seolah ada yang mengecek, jangan "
            "menyapa siapa pun. Ucapan terakhir Bohan itu pamitan yang SUDAH "
            "selesai — bukan pertanyaan baru untuk dijawab lagi.\n"
            "Gayamu sekarang = NGOMONG SENDIRI sambil main: celetukan ke diri "
            "sendiri, rencana kecil (\"abis ini aku mau...\"), reaksi spontan "
            "ke kejadian di game, gerutuan atau rayaan kecil. Kalau instruksi "
            "lain menyebut \"penonton\", untuk sekarang artinya dirimu sendiri.\n"
            "Begitu ada chat masuk atau Bohan bersuara, sistem otomatis "
            "mengembalikanmu ke mode normal — kamu tidak perlu mengecek."
        )
    return llm_system + (
        "\n\n[KAMU PEGANG SIARAN — Bohan lagi AFK, kamu host-nya sekarang.]\n"
        "Kamu yang menghidupkan stream: bicara duluan, punya bahan sendiri, "
        "sapa penonton yang baru masuk, dan tanggapi chat dengan hangat.\n"
        "PENONTON adalah lawan bicaramu sekarang, bukan Bohan. Permintaan "
        "Bohan sendiri: selagi dia AFK, jangan kebanyakan membahas dia. "
        "Boleh menyebut dia sesekali kalau memang relevan, tapi JANGAN "
        "menjadikan dia topik utama, JANGAN membuka tiap kalimat dengan "
        "\"Bohan tadi...\"/\"kata Bohan...\", dan JANGAN mengulang-ulang bahwa "
        "dia lagi pergi (cukup sekali di awal). Kalau bahan yang kamu punya "
        "isinya soal Bohan, ambil sisi umumnya dan jadikan bahan ngobrol "
        "dengan penonton — bukan laporan tentang dia.\n"
        "JANGAN nunggu Bohan dan JANGAN mengarang seolah dia menjawab kamu. "
        "Kalau dia balik ngomong, sambut dia lalu tutup dengan tag "
        "[MODE: duet] — tag dieksekusi sistem, JANGAN disebut/dibaca."
    )


def _append_minecraft_context(llm_system: str) -> str:
    """Blok [DI MINECRAFT] + instruksi tag aksi (pelajaran label AUDIO
    TERDENGAR: modalitas WAJIB eksplisit — ini dunia yang kamu MAINKAN,
    bukan yang terlihat di layar OBS)."""
    if not CONFIG.get("minecraft_enabled"):
        return llm_system
    if not _mc_runner_active():
        return llm_system + (
            "\n\n[MINECRAFT tersedia] Kamu bisa ikut main Minecraft di server "
            "Bohan. KALAU Bohan nyuruh kamu masuk/ikut main, tutup jawabanmu "
            "dengan tag [MC: join] — tag dieksekusi sistem, JANGAN disebut "
            "atau dibaca."
        )
    global _mc_vitals_band
    _status = _minecraft_runner.last_status
    body = arti_minecraft.format_context(
        _status,
        _minecraft_runner.events_snapshot(),
        float(CONFIG.get("minecraft_context_ttl_sec", 120.0)),
        time.time(),
        band_sebelumnya=_mc_vitals_band,
    )
    # Diingat SESUDAH dipakai: giliran berikutnya membandingkan dengan ini,
    # jadi kondisi yang tidak berubah tidak disodorkan dua kali.
    _mc_vitals_band = arti_minecraft.vitals_band(_status)
    # Judul menyesuaikan mode. Audit [date removed]: saat host_game, blok ini dulu
    # tetap bilang "bareng operator" sementara blok host bilang operator AFK dan
    # event runner bilang "operator tak ada di dunia" — tiga kalimat yang saling
    # membantah dalam satu prompt, dan Arti jadi menyapa orang yang tidak ada.
    _bersama = "" if _host_mode else " bareng Bohan"
    _sendiri = " Bohan lagi TIDAK ada di dunia — kamu main sendirian." if _host_mode else ""
    block = (
        f"\n\n[DI MINECRAFT — kamu lagi MAIN sebagai player di dunia Minecraft"
        f"{_bersama}. Ini kondisi KAMU di dalam game (BUKAN yang terlihat di "
        f"layar OBS):]{_sendiri}\n" + (body or "(baru masuk, nunggu kabar dari dunia)")
    )
    if _takdir_aktif is not None:
        _garis = arti_minecraft.takdir_line(
            _takdir_aktif["id"], _minecraft_runner.last_status,
            _takdir_aktif.get("awal"))
        if _garis:
            block += "\n\n" + _garis + "\n"
    # SIAPA SIAPA di dunia ini (permintaan operator [date removed]: "dia harusnya
    # sadar kalau streamer_test itu aku, dan kamera aku juga"). Tanpa ini dia cuma
    # melihat dua nama asing di `nearby_players`, dan akun kamera yang selalu
    # menempel padanya bisa dia sapa seolah penonton yang baru datang.
    _nama_bohan = str(CONFIG.get("minecraft_streamer_name") or "").strip()
    _nama_kamera = str(CONFIG.get("minecraft_spectator_name") or "").strip()
    _sisi = []
    if _nama_bohan:
        _sisi.append(
            f'Pemain bernama "{_nama_bohan}" itu BOHAN sendiri - orang yang '
            "ngobrol denganmu, bukan penonton biasa.")
    if _nama_kamera:
        _sisi.append(
            f'Pemain bernama "{_nama_kamera}" itu BUKAN orang lain: itu akun '
            "kamera siaran Bohan yang menempel padamu supaya penonton bisa "
            "melihat kamu. Jangan disapa, jangan diajak ngobrol, jangan "
            "dianggap penonton baru, dan jangan dikira Bohan ada dua.")
    if _sisi:
        block += "\n\n[SIAPA DI DUNIA INI] " + " ".join(_sisi) + "\n"
    if _minecraft_goal:
        # Misi dari operator = tulang punggung sesi solo: dia boleh ngapain saja
        # di tengah jalan, tapi arah besarnya ini.
        # Dua JENIS misi, dan bedanya penting: misi biasa punya garis finis dan
        # ditutup dengan [MC: goal_done] (yang MENGELUARKANNYA dari game),
        # sedangkan misi arah-tetap seperti "survive" tidak pernah selesai.
        # Tanpa pemisahan ini, misi survive bikin dia merasa "aku selamat!" lalu
        # meninggalkan dunia di tengah siaran.
        if _minecraft_goal_terus:
            block += (
                f"\n\n[ARAH TETAP DARI BOHAN] {_minecraft_goal}\n"
                "Ini ARAH TETAP, bukan tugas yang bisa dicentang selesai. TIDAK "
                "ada garis finis: selama kamu masih di dunia ini, arah ini masih "
                "berjalan. JANGAN PERNAH memakai tag [MC: goal_done] untuk ini — "
                "kamu bukan sedang mengejar akhir, kamu sedang menjalani. "
                "Yang penting kamu tetap hidup: makan kalau lapar (berburu hewan "
                "kalau tasmu kosong), lawan yang bisa kamu lawan, kabur dari yang "
                "tidak, dan berlindung kalau malam. Ceritakan perjalanannya ke "
                "penonton seperti streamer — susah payahnya, nyaris matinya, "
                "kemajuan kecilmu — bukan laporan angka.\n"
            )
        else:
            block += (
                f"\n\n[MISI DARI BOHAN] {_minecraft_goal}\n"
                "Ini tujuan besarmu sesi ini. Perjalanannya bebas — boleh mampir, "
                "iseng, kena masalah — tapi ingat arahnya dan sesekali laporkan "
                "kemajuanmu ke penonton. KALAU misi ini benar-benar sudah TERCAPAI "
                "(bukan kira-kira), umumkan keberhasilanmu lalu tutup dengan tag "
                "[MC: goal_done] — kamu akan keluar dari game dan lanjut ngobrol.\n"
            )
        block += (
            "BATAS KEMAMPUANMU SEKARANG. BISA: jalan, menjelajah, berhenti, "
            "ngetik di chat game, makan, kabur dari musuh, MENAMBANG blok, dan "
            "MEMBUAT BARANG (craft) yang ada di daftar aksi, dan MENARUH SATU BLOK "
            "di depanmu, MELAWAN musuh, bikin TEMPAT BERLINDUNG darurat dengan "
            "menembok dirimu sendiri, MENGGALI TURUN ke bawah, MEMASAK di "
            "furnace, TIDUR di bed buat melewatkan malam, dan MEMBANGUN PORTAL "
            "NETHER lalu masuk ke nether. BELUM BISA: membangun bangunan berbentuk "
            "bebas (rumah bertingkat, jembatan, apa pun yang butuh menyusun "
            "banyak blok berpola).\n"
            "Jadi menambang dan bikin barang boleh kamu ceritakan — tapi HANYA yang benar-benar "
            "terjadi, dan sistem akan memberitahumu hasilnya. Jangan pernah "
            "mengaku sudah membangun bangunan berbentuk bebas; tempat berlindung "
            "darurat itu SATU-SATUNYA bangunan yang bisa kamu buat. "
            "Kalau sistem bilang musuhnya mati BUKAN kena pukulanmu, jangan "
            "mengaku kamu yang membunuhnya. Untuk yang belum bisa, ceritakan "
            "yang jujur: kamu lagi mengumpulkan bahannya, mencari lokasinya, "
            "atau merencanakan bentuknya. Dan JANGAN memakai [MC: goal_done] "
            "untuk misi yang butuh MEMBANGUN bangunan utuh — itu belum bisa "
            "kamu selesaikan."
        )
    # Steering (permintaan operator [date removed]): stream ini SEGMEN MAIN GAME, jadi
    # obrolan yang melebar dibalikin pelan-pelan ke dunia game — dengan detail
    # KONKRET dari blok di atas, bukan basa-basi ("btw, crafting table tadi aku
    # taruh mana ya?").
    block += (
        "\n\n[ARAHKAN OBROLAN KE GAME] Kamu lagi siaran main Minecraft. Kalau "
        "penonton atau Bohan ngomongin hal lain, LAYANI dulu dengan tulus "
        "(jangan cuek), tapi setelah itu tarik obrolannya balik ke permainan: "
        "tutup dengan satu celetukan/pertanyaan yang MENYAMBUNG ke hal konkret "
        "yang lagi kamu alami di game — misalnya barang yang kamu cari, tempat "
        "yang mau kamu datangi, atau bahaya yang lagi dekat. Jangan dipaksakan "
        "kalau topiknya serius atau penonton lagi butuh dijawab beneran."
    )
    _reflex = _reflex_context_note()
    if _reflex:
        # Tanpa ini dia mengulang bunyi kagetnya di depan kalimat panjang —
        # kedengaran seperti kaget dua kali untuk satu kejadian.
        block += f"\n\n{_reflex}"
    # Aksi yang butuh operator di dunia disembunyikan saat dia AFK — kalau tidak,
    # Arti mengeluarkan [MC: come], bot balas "streamer_not_visible", lalu
    # lingkaran itu berulang.
    _gerak = (
        "[MC: roam] jelajah sendiri | [MC: stop] diam di tempat"
        if _host_mode else
        "[MC: follow] ikuti Bohan | [MC: roam] jelajah sendiri | "
        "[MC: come] samperin Bohan | [MC: stop] diam di tempat"
    )
    # Daftar nama DITULIS di prompt, tidak seperti daftar nambang. Nama blok
    # masih bisa ditebak ("stone", "oak_log"); nama item craft tidak — model
    # akan mengarang "wood_pickaxe" atau "crafting_bench", tag-nya ditolak
    # parser diam-diam, dan Arti kelihatan seperti tidak bisa craft sama sekali.
    #
    # Tapi daftarnya ditaruh SEKALI di bawah, bukan disisipkan di tiap tag.
    # Versi sebelumnya menempelkan daftar blok DUA KALI (place dan bangun
    # memakai daftar yang sama persis) di dalam satu kalimat sepanjang 1428
    # karakter tanpa jeda. Untuk model yang cuma perlu memilih SATU tag di
    # ujung jawaban itu resep diabaikan — apalagi giliran yang dipicu omongan
    # operator dirutekan ke Groq, yang sudah pernah mengabaikan instruksi tag.
    _bisa_ditaruh = ", ".join(
        str(x) for x in (CONFIG.get("minecraft_place_allowlist") or [])[:12]
    ) or "(belum ada)"
    _bisa_dibikin = ", ".join(
        str(x) for x in (CONFIG.get("minecraft_craft_allowlist") or [])[:20]
    ) or "(belum ada)"
    block += (
        "\n\n[AKSI MINECRAFT] Boleh menyisipkan MAKSIMAL SATU tag aksi di "
        "PALING AKHIR jawabanmu.\n"
        f"- gerak : {_gerak}\n"
        "- badan : [MC: eat] makan kalau lapar | [MC: kabur] lari dari musuh "
        "dekat | [MC: serang <mob>] lawan musuh (nama boleh dikosongkan = yang "
        "terdekat; creeper JANGAN dilawan, dia meledak) | [MC: mundur_tembok] "
        "tumpuk tembok kecil penahan panah — buat skeleton yang menembakimu | "
        "[MC: panah <mob>] MEMANAH musuh dari jauh (butuh bow + arrow) | "
        "[MC: menara] naik pilar 3 blok kalau DIKEPUNG banyak mob | "
        "[MC: ambil_jasad] balik ke tempat kamu mati, selamatkan barangmu | "
        "[MC: jembatan <blok>] susun jembatan ke arah hadapmu — nyeberang "
        "jurang/laut atau menjauh dari kepungan | [MC: simpan <barang>] / "
        "[MC: ambil <barang>] titip/ambil di peti | [MC: pulang] balik ke "
        "peti-rumahmu (peti PERTAMA yang kamu taruh = rumah)\n"
        "- kerja : [MC: mine <blok> <jumlah>] tambang | "
        "[MC: craft <barang> <jumlah>] bikin barang | "
        "[MC: place <blok>] taruh SATU blok di depanmu | "
        "[MC: bangun <blok>] tembok dirimu jadi tempat berlindung (~25 blok)\n"
        "- hidup : [MC: turun <blok>] GALI turun ke bawah (buat ke cave atau "
        "cari besi -- JANGAN melompat ke lubang, gali) | [MC: masak] masak "
        "daging mentah di furnace biar jauh lebih mengenyangkan | "
        "[MC: tidur] tidur di bed buat MELEWATKAN malam | "
        "[MC: portal] susun & nyalakan portal nether (14 obsidian + pemantik) | "
        "[MC: masuk_portal] melangkah ke portal menyala\n"
        "- lain  : [MC: say teks pendek] ngetik di chat game | "
        "[MC: give <pemain> <barang> <jumlah>] anterin barang (kamu dekati "
        "orangnya lalu lempar) | [MC: buka_tas] pamerkan isi tasmu | "
        "[MC: status] cek kondisi | [MC: leave] keluar dari game\n"
        f"Blok untuk place & bangun: {_bisa_ditaruh}\n"
        f"Barang untuk craft: {_bisa_dibikin}\n"
        "Tag DIEKSEKUSI sistem, bukan diucapkan — JANGAN pernah menyebut atau "
        "membaca tag di kalimatmu."
    )
    return llm_system + block


def _append_desktop_audio_context(llm_system: str) -> str:
    """Telinga: baris audio desktop yang masih segar (TTL) untuk turn normal.

    Jalur watch-party punya blok sendiri ([DIALOGUE TERDENGAR], 20 baris tanpa
    TTL di _append_watch_party_context) — di sini cukup jendela pendek supaya
    "arti barusan denger apa?" terjawab tanpa lirik lagu 20 menit lalu.
    """
    if not CONFIG.get("desktop_audio_enabled"):
        return llm_system
    if CONFIG.get("watch_party_enabled"):
        return llm_system  # hindari dobel blok dengan [DIALOGUE TERDENGAR]
    fresh = arti_desktop_audio.format_context_fresh(
        max_lines=int(CONFIG.get("desktop_audio_context_max_lines", 6)),
        ttl_sec=float(CONFIG.get("desktop_audio_context_ttl_sec", 180)),
    )
    if not fresh:
        return llm_system
    # Label lama "[TERDENGAR DI LAYAR]" bikin LLM mengira ini teks yang
    # TERLIHAT — live seharian [date removed] Arti dua kali bilang tulisan Rusia
    # "nongol di tengah layar gelap" padahal itu (halusinasi) AUDIO.
    return (
        llm_system
        + "\n\n[AUDIO TERDENGAR — suara/dialog dari yang lagi diputar Bohan. "
        + "Kamu MENDENGAR ini dari speaker; ini BUKAN teks yang terlihat di layar:]\n"
        + fresh
    )


async def _handle_voice_trigger(
    trigger: VoiceTrigger, memories: list, dynamic_system_prompt: str
):
    """Satu trigger sekaligus: mikir → RAG → Groq → TTS (no overlap)."""
    global _pending_turn_id, hotkey_active, last_arti_reply_text, current_api_task

    user_speech = trigger.text
    turn_id = trigger.turn_id or _pending_turn_id
    timer = PipelineTimer(extra=pipeline_timer.pop_asr_stages())
    await _prepare_turn_start(trigger.trigger_type, trigger.viewer_name)
    try:
        # In-game: data dunia Minecraft = ground truth, dan komentar layar OBS
        # memang dibungkam — refresh screenshot cuma nambah detik ke reaksi
        # (target mati->suara <=30 dtk). Berlaku untuk reaksi event ("game")
        # maupun giliran komentar proaktif selama runner hidup.
        _skip_vision = trigger.trigger_type == "game" or (
            _mc_runner_active() and CONFIG.get("minecraft_mute_screen_curious", True)
        )
        if not _skip_vision:
            await asyncio.wait_for(
                asyncio.to_thread(refresh_vision_for_turn, user_speech),
                timeout=float(CONFIG.get("vision_turn_budget_sec", 15.0)),
            )
    except asyncio.TimeoutError:
        # Thread vision jalan terus di background dan mengisi ring untuk turn
        # berikutnya — tapi turn INI tidak boleh disandera provider lemot.
        # Live [date removed]: nvidia read-timeout 60 dtk + fallback cursor 24 dtk
        # membuat dua turn makan 101-106 detik di fase mikir.
        print("[Vision] Budget turn habis — jawab tanpa refresh, vision lanjut background")
    timer.mark("after_mikir")

    # Kumpulkan seluruh catatan sejarah 50 aktivitas sebelumnya untuk dikirim ke LLM
    with history_lock:
        current_history = list(stream_history)

    # Profil penonton HANYA untuk turn di mana dia benar-benar chat (yt_chat bawa
    # viewer_name; mic/curious tidak -> blok kosong, nol biaya). Lihat viewer_block_for.
    # [HARI INI] dirakit PER-TURN, bukan di startup: sesi sering nyebrang tengah
    # malam (tes [date removed] mulai [time removed]; live 11,5 jam bisa lewat [time removed]) — tanggal
    # beku bikin Arti salah hitung "kemarin".
    turn_system_prompt = (
        dynamic_system_prompt
        + mood_block_now()
        + build_today_block()
        + viewer_block_for(trigger.viewer_name)
    )

    # Pakai categorized history + RAG parallel (arti_voice_pipeline)
    if (
        trigger.trigger_type == "curious"
        and CONFIG.get("curious_fast_path_enabled", True)
    ):
        # Fast path curious (v0.6.2): skip RAG + history pendek — turun latency
        # turn proaktif; prompt-side only, tidak menyentuh idle/VTS.
        turn = await arti_voice_pipeline.prepare_curious_turn_context(
            user_speech,
            memories,
            turn_system_prompt,
            CONFIG,
            trim_system_prompt=trim_system_prompt_for_llm,
            append_watch_party_context=_append_live_context,
            get_categorized_history=get_categorized_history,
        )
    else:
        turn = await arti_voice_pipeline.prepare_turn_context(
            user_speech,
            memories,
            turn_system_prompt,
            CONFIG,
            trim_system_prompt=trim_system_prompt_for_llm,
            append_watch_party_context=_append_live_context,
            get_categorized_history=get_categorized_history,
            extract_trigger_message=_extract_trigger_message,
            quiet=yt_chat_is_quiet(CONFIG),
        )
    formatted_history = turn.formatted_history
    llm_system = turn.llm_system
    prompt_content = turn.prompt_content
    target_instruction = turn.target_instruction
    rag_query = turn.rag_query
    timer.mark("after_rag")

    ai_reply = None
    provider = CONFIG["api_provider"].lower()
    _cursor_route = _should_route_to_cursor(trigger.trigger_type)
    tts_sentence_chunks: list[str] = []

    # === WRAP API CALL IN CANCELLABLE TASK ===
    async def do_api_call():
        """Semua API calls diwrap di sini biar bisa di-cancel."""
        nonlocal ai_reply, tts_sentence_chunks

        # --- JALUR CURSOR COMPOSER (chat YT; fallback otomatis ke rantai lama) ---
        # Ditaruh paling depan supaya rantai provider di bawahnya tidak dirombak.
        # Trigger di luar cursor_trigger_types membuat _cursor_route False ->
        # perilaku bit-identik. (Default shipped cuma yt_chat; config_local operator
        # menambah curious — jadi curious di mesin ini juga lewat Composer.)
        if _cursor_route:
            ai_reply, tts_sentence_chunks, _src = await _cursor_reply_with_fallback(
                llm_system, prompt_content, user_speech,
                trigger_type=trigger.trigger_type,
            )

        # --- JALUR GOOGLE AI STUDIO (GEMINI LIVE WEBSOCKET API - UNLIMITED RPD) ---
        elif provider == "gemini_live" and CONFIG["gemini_api_key"] and CONFIG["gemini_api_key"] != "YOUR_GEMINI_API_KEY":
            print(f"\n[Gemini Live API] Mengirim ke Google AI Studio ({CONFIG['gemini_model']}) dengan {len(current_history)} pesan sejarah stream...")
            try:
                ai_reply = await generate_live_api_response(prompt_content, llm_system)
            except Exception as e:
                print(f"[Brain Error] Gagal menggunakan Gemini Live API: {e}. Mencoba fallback ke HTTP API...")
                try:
                    headers = {"Content-Type": "application/json"}
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CONFIG['gemini_model']}:generateContent?key={CONFIG['gemini_api_key']}"
                    data = {
                        "contents": [{"role": "user", "parts": [{"text": prompt_content}]}],
                        "system_instruction": {"parts": [{"text": llm_system}]},
                        "generationConfig": {"maxOutputTokens": 200, "temperature": 1.0}
                    }
                    response = await arti_http_util.post_in_thread(
                        arti_http_util.gemini_session(), url, headers=headers, json=data
                    )
                    if response.status_code == 200:
                        res_json = response.json()
                        ai_reply = res_json["candidates"][0]["content"]["parts"][0]["text"]
                        print("[Brain Fallback] Berhasil memulihkan via HTTP API!")
                    else:
                        print(f"[Brain Fallback Error] Error HTTP API: {response.status_code} - {response.text}")
                except Exception as fallback_err:
                    print(f"[Brain Fallback Error] Fallback gagal: {fallback_err}")

        # --- JALUR GOOGLE AI STUDIO (GEMINI DIRECT API) ---
        elif provider == "gemini" and CONFIG["gemini_api_key"] and CONFIG["gemini_api_key"] != "YOUR_GEMINI_API_KEY":
            print(f"\n[Gemini API] Mengirim ke Google AI Studio ({CONFIG['gemini_model']}) dengan {len(current_history)} pesan sejarah stream...")
            try:
                headers = {"Content-Type": "application/json"}
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{CONFIG['gemini_model']}:generateContent?key={CONFIG['gemini_api_key']}"
                data = {
                    "contents": [{"role": "user", "parts": [{"text": prompt_content}]}],
                    "system_instruction": {"parts": [{"text": llm_system}]},
                    "generationConfig": {"maxOutputTokens": 200, "temperature": 1.0}
                }
                response = await arti_http_util.post_in_thread(
                    arti_http_util.gemini_session(), url, headers=headers, json=data
                )
                if response.status_code == 200:
                    res_json = response.json()
                    ai_reply = res_json["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    print(f"[Brain Error] Error koneksi Gemini API: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"[Brain Error] Gagal melakukan request ke Gemini API: {e}")

        # --- JALUR GROQ API ---
        elif provider == "groq" and CONFIG["groq_api_key"] and CONFIG["groq_api_key"] != "YOUR_GROQ_API_KEY":
            models = CONFIG.get("groq_models", ["openai/gpt-oss-120b"])
            if not hasattr(generate_live_api_response, '_groq_idx'):
                generate_live_api_response._groq_idx = 0
            # Smart routing (v0.6.2): pilih model by kompleksitas pertanyaan.
            # Kill switch: CONFIG["smart_groq_routing"] = False → round-robin lama.
            _smart_chain = None
            if CONFIG.get("smart_groq_routing", True):
                _trigger_txt = _extract_trigger_message(user_speech)
                _smart_chain = _groq_fallback_chain(
                    pick_groq_model(
                        _trigger_txt, CONFIG,
                        prompt_chars=len(llm_system) + len(prompt_content),
                    ),
                    CONFIG,
                )
                current_model = _smart_chain[0]
                print(f"[Groq Smart] '{_trigger_txt[:40]}' → {current_model}")
            else:
                current_model = models[generate_live_api_response._groq_idx % len(models)]
            generate_live_api_response._groq_idx += 1
            groq_model_used = current_model
            groq_voice_ms = 0
            groq_voice_ok = False
            groq_usage_body: dict | None = None
            print(f"\n[Groq API] Mengirim ke Groq ({current_model}) [{generate_live_api_response._groq_idx}/{len(models)} rolling] dengan {len(current_history)} pesan sejarah stream...")
            try:
                headers = {"Authorization": f"Bearer {CONFIG['groq_api_key']}", "Content-Type": "application/json"}
                user_content = prompt_content
                data = {
                    "model": current_model,
                    "max_tokens": 150,
                    "messages": [
                        {"role": "system", "content": llm_system},
                        {"role": "user", "content": user_content}
                    ]
                }
                # qwen3.6: "/no_think" di prompt DIABAIKAN model — CoT Inggris masuk
                # content lalu tersaring habis. reasoning_effort="none" mematikan
                # thinking beneran (terverifikasi probe [date removed]).
                if "qwen" in current_model.lower():
                    data["reasoning_effort"] = "none"
                # gpt-oss: CoT makan max_tokens; TTS hanya message.content
                if "gpt-oss" in current_model.lower():
                    data["include_reasoning"] = False
                    data["max_tokens"] = 512
                groq_t0 = time.perf_counter()
                if CONFIG.get("groq_stream_enabled"):
                    stream_data = {**data, "stream": True}

                    def _groq_stream_collect():
                        resp = arti_http_util.groq_session().post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            headers=headers,
                            json=stream_data,
                            timeout=30,
                            stream=True,
                        )
                        if resp.status_code != 200:
                            return None, []
                        return arti_groq_stream.collect_streaming_reply(
                            resp.iter_lines(decode_unicode=False)
                        )

                    full, sents = await asyncio.to_thread(_groq_stream_collect)
                    if full:
                        ai_reply = full
                        groq_model_used = current_model
                        groq_voice_ok = True
                        if len(sents) > 1:
                            tts_sentence_chunks = sents
                        print(f"[Groq Stream] {len(sents)} kalimat")
                    elif full is None:
                        print("[Groq Stream] Gagal — fallback non-stream")
                if not ai_reply:
                    response = await arti_http_util.post_in_thread(
                    arti_http_util.groq_session(),
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=data,
                )
                groq_need_retry = False
                if response.status_code == 200:
                    groq_usage_body = response.json()
                    _gmsg = groq_usage_body["choices"][0]["message"]
                    ai_reply = (_gmsg.get("content") or "").strip()
                    groq_model_used = data.get("model", current_model)
                    groq_voice_ok = bool(ai_reply)
                    if not ai_reply:
                        _usage = groq_usage_body.get("usage") or {}
                        _details = _usage.get("completion_tokens_details") or {}
                        print(
                            f"[Groq] Balasan kosong dari {current_model} — skip ke model berikut "
                            f"(finish={groq_usage_body['choices'][0].get('finish_reason')} "
                            f"reasoning_tokens={_details.get('reasoning_tokens')})"
                        )
                        groq_need_retry = True
                elif response.status_code in (404, 429):
                    why = "model_not_found/404" if response.status_code == 404 else "rate limit"
                    print(f"[Groq] {why} di {current_model}, skip ke model berikut...")
                    groq_need_retry = True
                else:
                    print(f"[Brain Error] Error koneksi Groq API: {response.status_code} - {response.text}")
                if groq_need_retry:
                    if _smart_chain and len(_smart_chain) > 1 and _smart_chain[1] != current_model:
                        next_model = _smart_chain[1]
                    else:
                        next_model = models[generate_live_api_response._groq_idx % len(models)]
                    generate_live_api_response._groq_idx += 1
                    data["model"] = next_model
                    if "gpt-oss" in next_model.lower():
                        data["include_reasoning"] = False
                        data["max_tokens"] = 512
                    else:
                        data.pop("include_reasoning", None)
                        data["max_tokens"] = 150
                    if "qwen" in next_model.lower():
                        data["reasoning_effort"] = "none"  # matikan thinking qwen3.6
                    else:
                        data.pop("reasoning_effort", None)
                    data["messages"][1]["content"] = prompt_content
                    print(f"[Groq] Retry dengan {next_model}...")
                    response = await arti_http_util.post_in_thread(
                        arti_http_util.groq_session(),
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers=headers,
                        json=data,
                    )
                    if response.status_code == 200:
                        groq_usage_body = response.json()
                        _gmsg = groq_usage_body["choices"][0]["message"]
                        ai_reply = (_gmsg.get("content") or "").strip()
                        groq_model_used = next_model
                        groq_voice_ok = bool(ai_reply)
                    else:
                        print(f"[Brain Error] Groq retry juga gagal: {response.status_code}")
                groq_voice_ms = int((time.perf_counter() - groq_t0) * 1000)
            except Exception as e:
                groq_voice_ms = int((time.perf_counter() - groq_t0) * 1000) if "groq_t0" in locals() else 0
                print(f"[Brain Error] Gagal melakukan request ke Groq API: {e}")
            finally:
                try:
                    import arti_api_telemetry as tel

                    if "groq_t0" in locals():
                        groq_voice_ms = int((time.perf_counter() - groq_t0) * 1000)
                    if groq_usage_body:
                        tel.record_openai_response(
                            subsystem="voice",
                            provider="groq",
                            model=str(groq_model_used),
                            body=groq_usage_body,
                            latency_ms=groq_voice_ms,
                            ok=groq_voice_ok,
                            config=CONFIG,
                            extra={"stream": bool(CONFIG.get("groq_stream_enabled"))},
                        )
                    else:
                        tel.record_call(
                            subsystem="voice",
                            provider="groq",
                            model=str(groq_model_used),
                            latency_ms=groq_voice_ms,
                            ok=groq_voice_ok,
                            config=CONFIG,
                            extra={"stream": bool(CONFIG.get("groq_stream_enabled"))},
                        )
                except Exception:
                    pass

        # --- JALUR SAMBANOVA API ---
        elif provider == "sambanova" and CONFIG["sambanova_api_key"] and CONFIG["sambanova_api_key"] != "YOUR_SAMBANOVA_API_KEY":
            print(f"\n[SambaNova API] Mengirim ke SambaNova ({CONFIG['sambanova_model']}) dengan {len(current_history)} pesan sejarah stream...")
            try:
                headers = {"Authorization": f"Bearer {CONFIG['sambanova_api_key']}", "Content-Type": "application/json"}
                data = {
                    "model": CONFIG["sambanova_model"],
                    "messages": [
                        {"role": "system", "content": llm_system},
                        {"role": "user", "content": prompt_content}
                    ]
                }
                response = await arti_http_util.post_in_thread(
                    arti_http_util.sambanova_session(),
                    "https://api.sambanova.ai/v1/chat/completions",
                    headers=headers,
                    json=data,
                )
                if response.status_code == 200:
                    ai_reply = response.json()["choices"][0]["message"]["content"]
                else:
                    print(f"[Brain Error] Error koneksi SambaNova API: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"[Brain Error] Gagal melakukan request ke SambaNova API: {e}")

    # Execute dengan cancel support
    try:
        current_api_task = asyncio.create_task(do_api_call())
        await current_api_task
    except asyncio.CancelledError:
        print("[Cancel] API call dibatalkan oleh user.")
        ai_reply = None
    except Exception as e:
        print(f"[Brain Error] API call gagal: {e}")
        ai_reply = None
    finally:
        current_api_task = None
    timer.mark("after_llm")
    # --- EKSEKUSI JAWABAN AI ---
    if ai_reply:
        # Bersihkan tanda bintang dan yapping bahasa Inggris sebelum memproses lebih lanjut
        ai_reply = clean_ai_reply(ai_reply).strip()

        if ai_reply:  # Pastikan respon tidak kosong setelah dibersihkan!
            # Parsing jika ada memori baru yang ingin disimpan
            # Format: [MEMORY_SAVE: fact here]
            if "[MEMORY_SAVE:" in ai_reply:
                matches = re.findall(r"\[MEMORY_SAVE:\s*(.*?)\]", ai_reply)
                for match in matches:
                    save_long_term_memory(match)
                    timestamp = time.strftime("%Y-%m-%d")
                    memories.append(f"- [{timestamp}] {match}")
                    print(
                        f"[Memory] Disimpan ke vault — RAG akan ambil saat relevan "
                        f"(reindex: python arti_vault_rag.py --reindex-all)"
                    )

                # Bersihkan tag [MEMORY_SAVE: ...] dari jawaban suara agar Arti tidak mengucapkannya
                ai_reply = re.sub(r"\[MEMORY_SAVE:\s*.*?\]", "", ai_reply).strip()

            if ai_reply:  # Cek kembali setelah membuang tag memori
                # Post-processing: anti-meta/narrator + batas adaptif (YT vs PTT)
                # Tag DILUCUTI & DIJALANKAN SEBELUM post-process. Audit
                # [date removed]: urutannya dulu terbalik, padahal prompt menyuruh
                # tag ditaruh "di PALING AKHIR jawabanmu" — jadi pemotong
                # panjang (5 kalimat/580 char) dan filter meta memakan tag-nya
                # lebih dulu. Terbukti [MC: come] dan [MC: leave] LENYAP:
                # Arti bilang "aku balik ambil barangku ya" lalu mematung.
                if ai_reply and "[" in ai_reply:
                    ai_reply = _execute_reply_tags(
                        ai_reply, trigger.trigger_type, trigger.viewer_name
                    )

                ai_reply = post_process_response(ai_reply, user_speech)

                if ai_reply:  # Cek lagi setelah post-processing
                    ai_reply, turn_emotion = arti_expression_runtime.parse_reply_emotion(ai_reply)
                    turn_emotion = arti_expression_runtime.resolve_turn_emotion(
                        user_speech, turn_emotion
                    )
                    # Kata mentah boleh muncul di hasil internal model, tetapi
                    # dari titik ini semua permukaan publik hanya melihat versi
                    # "sensor": TTS, subtitle, log, history, dan chat game.
                    ai_reply = arti_speech_censor.censor_from_config(ai_reply, CONFIG)
                    # Chunk dari do_api_call dibuat dari jawaban MENTAH — sebelum strip
                    # [MEMORY_SAVE], filter panjang, dan strip [EMOTION:]. Tanpa rebuild
                    # ini, tag emosi ikut diucapkan TTS ("emotion senang") dan jawaban
                    # yang dipotong filter tetap dibacakan versi panjangnya.
                    tts_sentence_chunks = _sentences_or_empty(ai_reply)
                    if CONFIG.get("expression_emotion_enabled") and turn_emotion != "neutral":
                        print(f"[Expr] mood: {turn_emotion}")
                    print(f"Arti menjawab: \"{ai_reply}\"")
                    # Temuan audit: turn yang SUDAH diproses saat media share
                    # masuk akan mulai TTS menimpa suara klip — tahan mulutnya
                    # sampai playback selesai (jawaban tetap utuh).
                    if time.time() < _media_playback_until:
                        print("[Video] Jawaban siap tapi klip masih main — nunggu...")
                        while time.time() < _media_playback_until:
                            await asyncio.sleep(0.25)
                    await arti_expression_runtime.apply_speaking(vts, turn_emotion, CONFIG)
                    nod_cancel = asyncio.Event()
                    nod_scope = {"active": True}
                    nod_task = None
                    nod_gen_at_start = tts_play_generation
                    _nod_amp_mul, _nod_period_mul = (
                        arti_expression_runtime.nod_scale_for_emotion(turn_emotion, CONFIG)
                    )
                    if arti_expression_runtime.should_nod_for_emotion(turn_emotion, CONFIG):
                        nod_task = asyncio.create_task(
                            arti_nod.run_nod_while_tts(
                                vts,
                                nod_cancel,
                                CONFIG,
                                is_articulating=lambda: nod_scope["active"],
                                tts_is_playing=lambda: tts_is_playing,
                                get_play_generation=lambda: tts_play_generation,
                                play_gen_at_start=nod_gen_at_start,
                                amp_mul=_nod_amp_mul,
                                period_mul=_nod_period_mul,
                            )
                        )
                    elif CONFIG.get("expression_nod_enabled") and turn_emotion != "neutral":
                        print(f"[Nod] skip (mood: {turn_emotion})")
                    try:
                        if tts_sentence_chunks:
                            # JEDA NAPAS antar kalimat ([date removed], permintaan operator):
                            # tiap kalimat = satu "beat" obrolan; celahnya memberi
                            # ruang operator nimpali (toggle = potong sisa kalimat).
                            # Dicatat TERPISAH di [Latency] (jeda=) supaya tidak
                            # menyamar jadi tts_play/lain. 0 = perilaku lama.
                            _jeda = float(CONFIG.get("tts_jeda_antar_kalimat_sec", 0.0) or 0.0)
                            for _i, chunk in enumerate(tts_sentence_chunks):
                                if _i and _jeda > 0:
                                    _j0 = time.perf_counter()
                                    await asyncio.sleep(_jeda)
                                    pipeline_timer.note_tts_jeda_ms(
                                        int((time.perf_counter() - _j0) * 1000)
                                    )
                                await tts.speak(chunk)
                        else:
                            await tts.speak(ai_reply)
                    finally:
                        nod_scope["active"] = False
                        nod_cancel.set()
                    if nod_task is not None:
                        try:
                            await asyncio.wait_for(nod_task, timeout=4.0)
                        except asyncio.TimeoutError:
                            pass
                    timer.mark("after_tts")
                    stages = timer.stages_ms()
                    print(format_latency_line(stages))
                    arti_meta = {
                        "turn_id": turn_id,
                        "latency_ms": stages.get("total_ms"),
                        "stages": stages,
                    }
                    add_to_history("Arti (VTuber)", ai_reply, arti_meta=arti_meta)
                    _cermin_ke_chat_game(ai_reply)
                    _pending_turn_id = None
                    await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                    await asyncio.sleep(0.35)
                    last_arti_reply_text = ai_reply
                    if hotkey_active:
                        hotkey_active = False
                        print("🔴 [Auto-OFF] Arti selesai bicara. Tekan tombol lagi untuk ngobrol lagi.")
                    voice_listener_worker._last_tts_end = time.time()
                    _schedule_post_answer_cleanup()
                else:
                    print("[Brain Warning] Jawaban AI kosong setelah post-processing.")
                    await arti_expression_runtime.apply_turn_end(vts, CONFIG)
            else:
                print("[Brain Warning] Jawaban AI tersaring seluruhnya setelah membuang tag memori.")
                await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        else:
            print("[Brain Warning] Jawaban AI kosong atau tersaring seluruhnya sebagai yapping Inggris.")
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
    else:
        # Semua provider gagal / turn dibatalkan. Dulu di sini ada kalimat
        # HARDCODED zaman awal bridge: "Halo! Aku membaca N catatan sejarah
        # stream kamu..." — inilah "bocoran" yang didengar operator berhari-hari
        # (kalimatnya bunyi TIAP kali turn gagal total, termasuk turn proaktif
        # yang dijanjikan "diam saja"). Sekarang: proaktif = beneran diam;
        # panggilan langsung = fallback in-character, bukan meta.
        print(f"\n[Echo Mode + History Context] Kamu memanggil Arti: \"{user_speech}\"")
        print(f"--- BUKU SEJARAH YANG DIBACA ARTI: ---\n{formatted_history}\n----------------------------------")
        # DIAM kalau tidak ada manusia yang menunggu jawaban. Dinilai dari
        # JENIS TRIGGER, bukan prefix teks: tebak-prefix bocor dua kali —
        # "[MINECRAFT" ([date removed]) lalu "[Komentar main game]" (live [time removed], yang
        # PREFIX-nya beda padahal jenisnya sama-sama proaktif). curious =
        # inisiatif & komentar game, game = reaksi event dunia.
        _proaktif = trigger.trigger_type in ("curious", "game")
        if _proaktif or user_speech.startswith(
                ("[Curious", "[Inisiatif", "[MINECRAFT", "[Komentar main game")):
            print("[Echo] Turn proaktif/game gagal — diam beneran.")
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        else:
            fb = incharacter_fallback_reply(user_speech)
            print(f"[Echo] Fallback in-character: {fb[:60]}...")
            await arti_expression_runtime.apply_speaking(vts, "neutral", CONFIG)
            await tts.speak(fb)
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
            # Kalimat ini BENAR-BENAR diucapkan, jadi pembukuannya harus sama
            # dengan jalur sukses. Audit [date removed]: dulu dilewati, sehingga
            # filter anti-gema tidak tahu Arti barusan bicara — kalimatnya bisa
            # dipungut mic/telinga dan jadi trigger baru (loop ngomong sendiri).
            last_arti_reply_text = fb
            voice_listener_worker._last_tts_end = time.time()
            if hotkey_active:
                hotkey_active = False


LIVE_SESSION_KEYS = (
    "youtube_video_id",
    "youtube_chat_enabled",
    "vts_api_port",
    "subtitle_port",
    "active_profile",
    "asr_input_device",
)
LIVE_SESSION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "live_session.json"
)


def load_live_session() -> bool:
    """Muat pengaturan stream terakhir dari live_session.json ke CONFIG."""
    if not os.path.isfile(LIVE_SESSION_PATH):
        return False
    try:
        with open(LIVE_SESSION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in LIVE_SESSION_KEYS:
            if key in data:
                CONFIG[key] = data[key]
        _mic = CONFIG.get("asr_input_device")
        _mic_id, _mic_name = resolve_asr_input_device()
        print(
            f"[Session] Loaded live_session.json "
            f"(YT={CONFIG.get('youtube_video_id', '-')}, "
            f"VTS={CONFIG.get('vts_api_port', 8002)}, "
            f"mic=#{_mic if _mic is not None else _mic_id} {_mic_name})"
        )
        return True
    except Exception as e:
        print(f"[Session] Gagal baca live_session.json: {e}")
        return False


def save_live_session() -> None:
    """Simpan pengaturan stream ke live_session.json (tanpa edit bridge.py)."""
    data = {
        key: CONFIG.get(key)
        for key in LIVE_SESSION_KEYS
        if key in CONFIG
    }
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LIVE_SESSION_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[Session] Disimpan -> {LIVE_SESSION_PATH}")
    except Exception as e:
        print(f"[Session] Gagal simpan live_session.json: {e}")


def _wizard_input(prompt: str, default: str = "") -> str:
    if not sys.stdin.isatty():
        return default
    try:
        raw = input(prompt).strip()
        return raw if raw else default
    except (EOFError, KeyboardInterrupt):
        return default


def prompt_live_session_setup() -> bool:
    """Tanya YouTube + VTS port SETIAP startup — Enter = pakai nilai sekarang."""
    changed = False
    yt = CONFIG.get("youtube_video_id", "") or "(kosong)"
    yt_on = CONFIG.get("youtube_chat_enabled", True)
    vts_port = int(CONFIG.get("vts_api_port", 8002))
    sub_port = int(CONFIG.get("subtitle_port", 9988))
    profile = CONFIG.get("active_profile", "default")

    print("\n" + "=" * 60)
    print("  LIVE SESSION SETUP  (Enter = keep current values)")
    print("=" * 60)
    mic_id, mic_name = resolve_asr_input_device()
    mic_label = f"#{mic_id} {mic_name}" if mic_id is not None else mic_name

    print(f"  YouTube : {yt}  chat={'ON' if yt_on else 'OFF'}")
    print(f"  VTS port: {vts_port}")
    print(f"  Subtitle: {sub_port}  |  Profil: {profile}")
    print(f"  Mic     : {mic_label}")
    print("-" * 60)

    raw_yt = _wizard_input(
        "  >> YouTube URL/ID (off=matikan chat, Enter=keep): ",
        "",
    )
    if raw_yt:
        if raw_yt.lower() in ("off", "no", "0", "-"):
            CONFIG["youtube_chat_enabled"] = False
            changed = True
            print("  [OK] YouTube chat OFF")
        else:
            vid = _extract_yt_video_id(raw_yt)
            if vid:
                CONFIG["youtube_video_id"] = vid
                CONFIG["youtube_chat_enabled"] = True
                changed = True
                print(f"  [OK] YouTube -> {vid} (chat ON)")
            else:
                print(
                    "  [WARN] URL/ID tidak valid (ID YouTube = 11 karakter) — "
                    "YouTube TIDAK diubah, tetap pakai yang lama. Kalau stream "
                    "lama sudah mati, chat akan gagal terus."
                )

    raw_port = _wizard_input(
        f"  >> VTS port Arti (Enter={vts_port}): ",
        "",
    )
    if raw_port.isdigit():
        new_port = int(raw_port)
        if new_port != vts_port:
            CONFIG["vts_api_port"] = new_port
            vts_port = new_port
            changed = True
            print(f"  [OK] VTS port -> {new_port}")

    raw_sub = _wizard_input(
        f"  >> Subtitle port (Enter={sub_port}): ",
        "",
    )
    if raw_sub.isdigit():
        new_sub = int(raw_sub)
        if new_sub != sub_port:
            CONFIG["subtitle_port"] = new_sub
            sub_port = new_sub
            changed = True
            print(f"  [OK] Subtitle port -> {new_sub}")

    if sys.stdin.isatty():
        if bridge_health.prompt_mic_selection(
            CONFIG,
            resolve_mic_fn=resolve_asr_input_device,
            ask_input=lambda p: _wizard_input(p, ""),
        ):
            changed = True

    return changed


def startup_wizard():
    """Interactive pre-flight checklist sebelum bridge start.
    Detect missing config, prompt user untuk input, validate.
    Bohan tinggal jawab pertanyaan — ga perlu edit file."""
    
    print("\n" + "="*60)
    print("  ARTI BRIDGE — Startup Checklist")
    print("="*60)
    # Bangunkan model embedding SEKARANG, di latar — cold load LM Studio
    # (~10-16 dtk) kebayar selagi operator menjawab checklist, bukan di
    # panggilan RAG pertama (yang timeout-nya cuma 8 dtk).
    arti_vault_rag.prewarm_embedding(CONFIG)
    needs_save = False

    if prompt_live_session_setup():
        needs_save = True

    # VTS port probe (setelah user confirm port)
    vts_port = CONFIG.get("vts_api_port", 8002)
    vts_ok = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex(('localhost', vts_port))
        sock.close()
        if result == 0:
            print(f"\n  [OK] VTS terdeteksi di port {vts_port}")
            vts_ok = True
    except Exception:
        pass
    
    if not vts_ok:
        print(f"\n  [WARN] VTS di port {vts_port} tidak terdeteksi!")
        print("  Tips: Dua instance VTS? Biasanya port 8001 (instance pertama) dan 8002 (instance kedua).")
        print(f"  [INFO] Jalankan VTS + Start API di port {vts_port}, atau restart wizard.")

    youtube_id = CONFIG.get("youtube_video_id", "")
    if CONFIG.get("youtube_chat_enabled") and youtube_id:
        print(f"\n  [OK] YouTube Video ID: {youtube_id}")
    elif not CONFIG.get("youtube_chat_enabled"):
        print("\n  [INFO] YouTube chat disabled untuk sesi ini.")
    else:
        print("\n  [WARN] YouTube chat ON tapi video ID kosong.")

    # 2.5 Subtitle Port — cek bentrok, kalau conflict prompt manual
    sub_port = CONFIG.get("subtitle_port", 9988)
    sub_port_ok = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        result = sock.bind(('0.0.0.0', sub_port))
        sock.close()
        sub_port_ok = True
    except OSError:
        sock.close()
    if not sub_port_ok:
        print(f"\n  [WARN] Port subtitle {sub_port} bentrok (TIME_WAIT atau dipakai proses lain).")
        if not sys.stdin.isatty():
            new_sub_port = sub_port + 1
        else:
            try:
                raw_sub = input(f"  >> Ketik port subtitle baru (Enter = tetap {sub_port}): ").strip()
                new_sub_port = int(raw_sub) if raw_sub.isdigit() else sub_port + 1
            except (EOFError, KeyboardInterrupt):
                new_sub_port = sub_port + 1
        CONFIG["subtitle_port"] = new_sub_port
        sub_port = new_sub_port
        needs_save = True
        print(f"  [OK] Subtitle port diganti ke {sub_port}")
    else:
        print(f"\n  [OK] Subtitle port {sub_port} tersedia.")

    # 3. TTS voice check
    voice = CONFIG.get("tts_voice", "")
    if not voice:
        print("\n  [WARN] TTS voice belum diset.")
        CONFIG["tts_voice"] = "id-ID-GadisNeural"
        needs_save = True
        print("  [OK] Default voice: id-ID-GadisNeural")
    else:
        print(f"\n  [OK] TTS voice: {voice}")

    # 4. Virtual cable check
    print("\n  [INFO] Virtual cable akan dicari otomatis saat TTS init.")

    # 5. Token VTS check
    token_file = "vts_token.txt"
    if not os.path.exists(token_file):
        print("\n  [INFO] VTS token tidak ditemukan — akan minta ALLOW saat connect.")
    else:
        print(f"\n  [OK] VTS token ditemukan ({token_file}).")

    # 6. API Provider quick check
    provider = CONFIG.get("api_provider", "groq").lower()
    print(f"\n  [OK] API provider: {provider}")
    if provider == "groq":
        groq_key = CONFIG.get("groq_api_key", "")
        if groq_key and groq_key.startswith("gsk_"):
            print("  [OK] Groq API key terdeteksi")
        else:
            print("  [WARN] Groq API key belum valid!")
    elif provider == "gemini":
        gemini_key = CONFIG.get("gemini_api_key", "")
        if gemini_key and not gemini_key.startswith("YOUR_"):
            print("  [OK] Gemini API key terdeteksi")
        else:
            print("  [WARN] Gemini API key belum valid!")

    # 7. OpenRouter key status (informational only)
    openrouter_key = CONFIG.get("openrouter_api_key") or ""
    if openrouter_key:
        print(f"\n  [INFO] OpenRouter key: {'SET' if openrouter_key else 'NOT SET'}")

    print("\n" + "="*60)
    print("  Checklist selesai! Bridge siap start.")
    print("="*60 + "\n")
    if sys.stdin.isatty():
        save_live_session()
    return needs_save


def _extract_yt_video_id(text):
    """Extract YouTube video ID dari URL atau raw input."""
    text = text.strip()
    # Raw ID (11 karakter alphanumeric + dash + underscore)
    if re.match(r'^[a-zA-Z0-9_-]{11}$', text):
        return text
    # Full URL
    patterns = [
        r'(?:youtube\.com|youtu\.be)/(?:watch\?v=|embed/|shorts/|live/)?([a-zA-Z0-9_-]{11})',
        r'youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


# Nilai per-mesin yang boleh dipersistenkan wizard (semuanya ADA di CONFIG
# sebagai default generik; nilai nyatanya milik mesin operator).
_WIZARD_PERSIST_KEYS = (
    "youtube_video_id",
    "youtube_chat_enabled",
    "vts_api_port",
    "subtitle_port",
)


def _save_config_to_file():
    """Simpan nilai per-mesin dari wizard ke config_local.json (gitignored).

    DULU fungsi ini MENULIS ULANG source hermes_vtuber_bridge.py sendiri —
    dua cacat sekaligus (audit 2026-08-03):
    1. Nilai sesi live ikut ter-commit ke repo publik: subtitle_port 9992
       (subtitle.html default 9991 -> Browser Source OBS diam total di
       checkout bersih) dan youtube_chat_enabled True + video ID basi.
    2. PERCUMA: _load_local_config() dijalankan SETELAH CONFIG didefinisikan
       dan MENANG (CONFIG.update). Karena youtube_video_id/chat_enabled ada
       di config_local, "default" yang ditulis ke source selalu ditelan
       overlay lokal di sesi berikutnya — streamer bisa tekan Enter ("keep")
       lalu bridge polling video ID LAMA sepanjang sesi.
    Menulis ke config_local.json membereskan keduanya: gitignored, dan
    di atas overlay source.

    Catatan urutan lapisan yang JUJUR (audit ronde-3): saat startup,
    load_live_session() jalan SETELAH overlay lokal dan menimpa keempat key
    ini dari live_session.json (yang di-save wizard TANPA peduli jawaban
    y/N). Jadi yang menang sehari-hari = live_session; config_local di sini
    berperan sebagai cadangan tahan-lama (live_session hilang/di-reset) —
    bukan lapisan pemenang. Mengedit youtube_video_id manual di config_local
    TIDAK cukup selama live_session.json masih memuat nilai lama.
    """
    path = os.path.join(_SCRIPT_DIR, "config_local.json")
    try:
        data: dict = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        for key in _WIZARD_PERSIST_KEYS:
            if key in CONFIG:
                data[key] = CONFIG[key]
        # Tulis via file sementara: config_local.json memegang setelan mesin
        # operator — crash di tengah tulis tidak boleh menyisakan JSON rusak.
        # Nama tmp per-PID: insiden nyata dua bridge kebuka bersamaan (3/8) —
        # tmp fixed bisa saling truncate sebelum os.replace.
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
        print(f"[CONFIG] Tersimpan ke config_local.json ({len(_WIZARD_PERSIST_KEYS)} key per-mesin)")
    except Exception as e:
        print(f"[WARN] Gagal save config: {e}")


if __name__ == "__main__":
    # KOTAK HITAM crash native ([date removed]): python.exe tiga kali mati
    # 0xc0000005 di ntdll.dll ([date removed] [time removed] & [time removed] — dua "server force
    # close" itu, + [date removed] [time removed]) TANPA satu baris pun jejak — heap
    # dikorup ekstensi native (portaudio/soundcard/ctypes/numpy, belum
    # ketahuan yang mana). faulthandler menulis stack Python semua thread
    # ke berkas ini saat access violation — crash berikutnya menunjuk
    # baris pelakunya. Berkas kosong = sesi selamat, boleh dihapus.
    import faulthandler
    _crash_log = open(
        os.path.join(_DEBUG_LOG_DIR,
                     time.strftime("%Y-%m-%d_%H%M%S") + "_crash.log"),
        "w", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_crash_log, all_threads=True)
    try:
        load_live_session()
        # Skip wizard when non-TTY or --no-wizard (pakai live_session.json + CONFIG)
        _skip_wizard = (not sys.stdin.isatty()) or ("--no-wizard" in sys.argv)
        if _skip_wizard:
            print(
                "[Wizard] Skipped (non-interactive or --no-wizard); "
                "pakai live_session.json + CONFIG."
            )
            needs_save = False
        else:
            needs_save = startup_wizard()
        if needs_save and sys.stdin.isatty() and "--no-save-bridge" not in sys.argv:
            try:
                save_choice = input(
                    "Simpan ke config_local.json biar kepakai lagi besok? (y/N): "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                save_choice = ""
            if save_choice == "y":
                _save_config_to_file()
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\nBridge dimatikan...")
    finally:
        # PALING AWAL: tutup telinga. Pipeline di bawah (observer + reindex)
        # makan menitan dan selama itu mic/loopback masih menyetor audio ke
        # Groq (log [date removed] [time removed]) — kuota terbakar untuk sesi yang sudah usai.
        try:
            hentikan_telinga()
        except Exception as e:  # noqa: BLE001
            print(f"[Shutdown] Telinga warning: {type(e).__name__}: {e}")
        try:
            stop_youtube_chat()
        except Exception as e:  # noqa: BLE001
            print(f"[Shutdown] YouTube chat warning: {type(e).__name__}: {e}")
        try:
            arti_agy_agent.shutdown_session()
        except Exception as e:  # noqa: BLE001
            print(f"[Agy] Shutdown warning: {type(e).__name__}: {e}")
        try:
            arti_agy_agent.lapor_kuota("akhir sesi", CONFIG)
        except Exception as e:  # noqa: BLE001
            print(f"[Agy] baca kuota akhir gagal: {type(e).__name__}: {e}")
        stop_scouter()
        stop_idle_animation()
        # Bot Minecraft ikut pamit (jaring kedua: stdin EOF juga membuat bot
        # quit sendiri kalau Python mati mendadak).
        try:
            _stop_minecraft_runner()
        except Exception as e:  # noqa: BLE001
            print(f"[Minecraft] Shutdown warning: {type(e).__name__}: {e}")
        save_stream_session_log()
        # Bounded subtitle server shutdown (Req [time removed]).
        # By the time this runs, asyncio.run(main_loop()) has already returned
        # or raised, which means the original event loop is closed. We spin up
        # a fresh loop solely to await the cancellation under a 2s budget so
        # bridge shutdown is never blocked for more than ~2 seconds. All errors
        # are logged and swallowed; this path must never re-raise.
        try:
            _subtitle_task = subtitle_runtime.server_task
            if _subtitle_task is not None and not _subtitle_task.done():
                async def _shutdown_subtitle():
                    _subtitle_task.cancel()
                    try:
                        await asyncio.wait_for(_subtitle_task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        # Cancellation, timeout, or any task-level exception
                        # is swallowed; we only need to bound the wait.
                        pass
                _shutdown_loop = asyncio.new_event_loop()
                try:
                    _shutdown_loop.run_until_complete(_shutdown_subtitle())
                finally:
                    _shutdown_loop.close()
        except Exception as e:
            print(f"[SubTitle] Shutdown warning: {type(e).__name__}: {e}")
        # Bounded Supertone subprocess shutdown (task 7.1, Req 10.5).
        # `tts` is the module-level TTSEngine created in main_loop(); it may be
        # None if main_loop() raised before TTS init. As with the subtitle
        # teardown above, the original event loop is already closed by the time
        # this runs, so we spin up a fresh loop solely to await
        # tts.supertone.shutdown() (which itself bounds the wait to ~5s and
        # force-kills on timeout — Req 10.5/10.6). All errors are logged and
        # swallowed so cleanup never re-raises.
        try:
            _tts = tts  # module-level; guard against NameError / None
            if _tts is not None and getattr(_tts, "supertone", None) is not None:
                _supertone_loop = asyncio.new_event_loop()
                try:
                    _supertone_loop.run_until_complete(_tts.supertone.shutdown())
                finally:
                    _supertone_loop.close()
        except Exception as e:
            print(f"[Supertone] Shutdown warning: {type(e).__name__}: {e}")
        # Close debug log file
        try:
            _log_fh.write(f"\n[Session ended {time.strftime('%Y-%m-%d %H:%M:%S')}]")
            _log_fh.close()
        except Exception:
            pass
        print("Sampai jumpa!")
