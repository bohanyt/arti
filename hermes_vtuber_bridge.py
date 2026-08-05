import asyncio
import json
import os
from pathlib import Path
import sys

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
import arti_desktop_audio
import arti_http_util
import arti_voice_pipeline
import arti_groq_stream
import arti_wake
from arti_wake import is_arti_wake_call
import arti_yt_viewers
import arti_minecraft
import arti_obs
import arti_session_mode
import arti_nod
import arti_openrouter
import arti_reply_policy
import arti_voice_queue

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
        self.stream.write(data)
        self.stream.flush()
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

_log_fh = open(_DEBUG_LOG_PATH, "w", encoding="utf-8", buffering=1)
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
        "llama-3.3-70b-versatile",                    # hidup s/d ~16 Agu 2026
        "llama-3.1-8b-instant",                       # hidup s/d ~16 Agu 2026
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
    "youtube_video_id": "HuWZx-APkAM",                          # Isi di config_local.json tiap stream (dari URL: youtube.com/watch?v=INI_VIDEO_ID)

    # Konfigurasi OBS Subtitle (in-process WebSocket server + word-level karaoke renderer)
    "subtitle_enabled": True,                         # Master switch: False mematikan in-process subtitle server & semua broadcast
    "subtitle_status_enabled": True,                  # Toggle independen untuk broadcast_status("speaking"/"idle"); diabaikan saat subtitle_enabled=False
    "subtitle_port": 9991,                            # Port WebSocket untuk subtitle.html OBS Browser Source (subtitle.html default 9991 — jangan drift; insiden dua-sesi 3/8 sempat menulis 9992)

    # Mode Pemicu Percakapan Streamer:
    # - "wake_word"     : Panggil Arti dengan mengucapkan kata kunci "arti" / "eh arti"
    # - "push_to_talk"   : Mic MEREKAM CASUAL PASIF ke sejarah stream, tetapi HANYA merespon jika menekan hotkey!
    "trigger_mode": "push_to_talk",
    "hotkey_key": "mouse_x2",                         # Diatur ke Mouse 5 (Tombol Samping Depan Logitech LIGHTSYNC Anda!)
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
    "groq_model_fast": "llama-3.1-8b-instant",

    # Smart Groq routing per turn (v0.6.2 — restore dari checkpoint 2026-06-07):
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
    # 12.0 (riwayat: 7.0 -> 10.0 sore 2026-08-02 -> 12.0 seharian 2026-08-03).
    # Angka ini dipakai DUA KALI: deadline internal saat mengkonsumsi stream,
    # DAN dasar asyncio.wait_for (+1 detik). Distribusi composer terus BERGESER
    # naik per sesi: spike p50 3,2-3,6 / max 5,12; sore n=56 p50 5,16 / max
    # 6,905 (axe 7,0); seharian n=26 sukses p50 7,38 / p90 9,31 / max 9,907 —
    # max sukses NEMPEL di kapak 10,0 dan 27 percobaan gagal (20 timeout +
    # 7 outer) vs 26 sukses = ekor asli jelas lewat 10 dtk. Efek domino timeout
    # tetap mahal: mark_dirty -> daur ulang -> "sesi belum hangat" -> Groq 8B /
    # bisu. 12,0 memberi ruang ukur ekor jujur; kalau sesi berikut max sukses
    # nempel lagi di 12, masalahnya di composer yang melambat, bukan kapaknya.
    "cursor_timeout_sec": 12.0,
    # Trigger berharga (video/donation) saat sesi dingin: tunggu pemanasan
    # sampai sekian detik alih-alih jatuh ke Groq 8B — konten tak tergantikan
    # (digest video, terima kasih donatur), tidak ada yang diburu waktu.
    "cursor_warmup_wait_precious_sec": 45.0,
    "cursor_session_max_turns": 20,          # pembengkakan konteks terukur 1,05x/20 turn
    "cursor_session_max_age_sec": 1800,
    "cursor_reject_on_tool_call": True,      # agen manggil tool = melenceng → Groq
    "cursor_max_consecutive_failures": 3,    # breaker: tutup Cursor setelah N gagal beruntun
    # Breaker half-open: setelah sekian detik, coba Cursor lagi. Penting untuk live
    # yang DITINGGAL seharian — tanpa ini, satu gangguan sekejap (3 gagal instan)
    # mematikan Composer untuk sisa hari. 0 = permanen sampai restart (perilaku lama).
    "cursor_breaker_cooldown_sec": 900,
    "cursor_last_resort_incharacter": True,
    "cursor_api_key": os.environ.get("CURSOR_API_KEY", ""),
    # Sesi Cursor per-role (keputusan Bohan 2026-08-01: Cursor tulang punggung
    # semua otak, chain API gratis jadi fallback). Aktif kalau "cursor" ada di
    # scouter/observer/vision_provider_chain — chain shipped SENGAJA tanpa
    # cursor (repo publik; nyalakan lewat config_local). Verifikasi model/param:
    # scripts/spike_grok_vision.py — grok-4.5 punya effort low/medium/high,
    # composer-2.5 TIDAK punya effort (jangan diisi), default variant = FAST.
    # Revisi Bohan 2026-08-03 (CSV usage: grok-high 23,5M token = 49% konsumsi
    # sehari, 671 call scouter): scouter turun ke composer non-fast — "composer
    # 2.5 NOT FAST is enough". Observer TETAP grok-high, hanya akhir live.
    "cursor_scout_model": "composer-2.5",
    "cursor_scout_effort": "",
    "cursor_observer_model": "grok-4.5",  # ringkas akhir live ("gapapa")
    "cursor_observer_effort": "high",
    "cursor_scout_timeout_sec": 30.0,     # grok cold ~19 dtk; scouter jalan di thread sendiri
    "cursor_vision_model": "composer-2.5",
    "cursor_vision_effort": "",           # composer: param fast saja
    "cursor_vision_timeout_sec": 45.0,    # cold + gambar terukur 35,6 dtk — jangan diturunkan

    # Batas jawaban Arti (dipakai post_process_response + get_arti_reply_limits)
    # Rant mode (permintaan Bohan 2026-08-01): saat chat sepi (jeda antar pesan
    # >= yt_quiet_after_sec), ~10% pertanyaan non-deep dijawab panjang 6-8
    # kalimat. Dadu deterministik per teks pesan (lihat arti_reply_policy).
    "yt_quiet_after_sec": 75.0,
    "arti_reply_rant_chance": 0.10,
    "arti_reply_rant_min_sentences": 6,
    "arti_reply_rant_max_sentences": 8,
    "arti_reply_rant_chars_cap": 900,
    "arti_reply_max_sentences": 5,
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
    # Bot layanan chat — pesan mereka boleh tercatat di history (konteks
    # leaderboard dll), tapi TIDAK PERNAH mentrigger jawaban. Live 11,5 jam
    # 2026-08-01: @Streamlabs posting leaderboard dan sempat diantri jawab.
    "yt_bot_viewers": ["Streamlabs", "Nightbot", "StreamElements", "Moobot", "Fossabot"],
    "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", ""),
    # OpenRouter model slugs — lihat docs/OPENROUTER_MODELS.md
    # Diperbarui 2026-07-31: `poolside/laguna-xs.2:free`, `poolside/laguna-m.1:free`,
    # dan `owl-alpha` semuanya 404 ("No endpoints found") — slug poolside di-rename
    # jadi `laguna-xs-2.1`, `owl-alpha` hilang total.
    # PENTING: poolside = model reasoning. Terverifikasi probe — pada max_tokens 110/350
    # ia mengembalikan content KOSONG (finish=length, CoT menghabiskan budget); baru keluar
    # jawaban di ~600 token. Jalur live pakai max_tokens 110-320 (_TOKENS_BY_SENT di
    # arti_reply_policy.py:55), jadi poolside HARAM di sini — itu bug "Jawaban AI kosong"
    # yang sama seperti qwen3.6 (fix dd88d9e). Pakai model non-reasoning yang finish=stop.
    "openrouter_live_model": "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter_live_last_resort": "google/gemma-4-26b-a4b-it:free",
    "openrouter_live_fast_only": True,
    "openrouter_live_fallback_enabled": True,
    "openrouter_live_timeout_sec": 45,
    # Scouter/summarizer pakai max_tokens 350 (scouter_max_tokens) — masih terlalu
    # ketat untuk poolside, jadi non-reasoning juga.
    "openrouter_summarizer_model": "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter_summarizer_fallback": "nvidia/nemotron-3-nano-30b-a3b:free",
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
    "vault_rag_top_k": 5,
    # 2400: header instruksi RAG tumbuh 76 -> 172 char (aturan konflik tanggal), dan itu
    # overhead tetap. Tanpa kenaikan ini hit ke-5 tergusur. Lihat arti_vault_rag.py.
    "vault_rag_max_context_chars": 2400,
    "vault_rag_reindex_on_shutdown": True,
    # 0 = tunggu reindex TUNTAS saat shutdown (Bohan 2026-08-02: jangan ada kerja
    # diam-diam saat "Terminate batch job" muncul). >0 = batas detik (perilaku lama).
    "vault_rag_reindex_shutdown_timeout_sec": 0,
    # Catch-up saat start: menyembuhkan reindex shutdown yang terpotong (thread
    # daemon mati bersama proses — insiden 2026-08-01, DB berhenti 624/747 chunk).
    "vault_rag_reindex_on_startup": True,
    "memory_startup_max_bullets": 5,
    # 6500, naik dari 5500 (2026-08-01). Terukur di sesi live: prompt rakitan penuh
    # (BASE 3851 + origin 231 + memori 346 + mood 23 + viewer 908 + emotion 190) =
    # 5549 — lebih 49 char dari cap lama, dan penaltinya TIDAK proporsional: trim
    # membuang seluruh blok [MEMORI JANGKA PANJANG (369 char berikut instruksi
    # "boleh cerita cara kerjamu") di TIAP turn. Viewer block tumbuh seiring
    # ARTI_VIEWERS.md bertambah (23 baris sekarang), jadi beri ruang. Biaya ~250
    # token/turn di model 131k ctx — murah dibanding kehilangan instruksi diam-diam.
    "llm_system_prompt_max_chars": 6500,

    # Fase 1 — transcript JSONL + vault slim (v0.5.2)
    "stream_session_id": "",
    "transcript_dir": "transcripts",
    "session_log_keep_n": 5,
    "transcript_flush_fsync": True,

    # Konfigurasi Supertone 3 TTS (dual-engine: master switch + parameter sintesis lokal)
    "tts_engine": "supertone",                        # "supertone" | "edge_tts" — master engine switch
    "tts_preprocess_numbers": True,                   # Jalankan konversi angka→kata Indonesia sebelum sintesis
    "supertonic_voice": "F1",                         # Voice style: F1-F5 / M1-M5 (F1 disarankan)
    "supertonic_speed": 1.1,                          # tuned for live; 1.3 was too fast
    "supertonic_lang": "id",                          # Kode bahasa Supertone
    "supertonic_total_steps": 10,                     # Max quality [5–12] — F1 live (10 = stabil + cepat)
    # Jalankan Supertonic di GPU (v0.6.3). Terukur 2026-07-31 di RTX 4050 Laptop:
    # CPU p50 29,4 detik/kalimat -> CUDA p50 2,0 detik. ~15x.
    # Default False supaya repo publik tidak mencoba CUDA di mesin tanpa runtime-nya;
    # nyalakan di config_local.json. Kalau CUDA gagal, supertonic/loader.py jatuh ke CPU
    # sendiri (terverifikasi) — Arti tetap bersuara, cuma lambat.
    # Butuh di venv312: onnxruntime-gpu + paket nvidia-* (lihat requirements-supertone.txt).
    "supertonic_use_cuda": False,
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
        "nvidia",
        "google_gemma",
        "google_gemini_lite",
        "cloudflare",
        "openrouter",
        "github",
        "zai",
        "ollama",
    ],
    # 512, naik dari 256 (2026-08-01). Kontrak vision baru (scene 1-2 kalimat
    # spesifik + hook + playback + ocr) tidak muat di 256 token: terpantau di sesi
    # live JSON-nya KEPOTONG di tengah -> gagal parse -> JSON mentah tersimpan
    # sebagai scene dan ikut tersuntik ke prompt Arti sebagai [LAYAR: { "scene"...].
    # Pagu TOTAL refresh vision yang memblokir turn (bukan timeout per provider).
    # Live 2026-08-02: tanpa pagu, nvidia lemot (read timeout 60s) + fallback
    # cursor menyandera turn sampai 106 detik.
    "vision_turn_budget_sec": 15.0,
    "vision_max_tokens": 512,
    "vision_scene_max_chars": 300,
    "vision_ocr_max_chars": 200,
    "vision_capture_max_width": 1280,
    "vision_capture_jpeg_quality": 75,
    "vision_temperature": 0.2,
    "vision_nvidia_model": "google/diffusiongemma-26b-a4b-it",
    "vision_google_gemma_model": "gemma-4-26b-a4b-it",
    "vision_google_gemma_fallback_model": "gemma-4-31b-it",
    "vision_google_gemini_model": "gemini-3.1-flash-lite",
    "vision_cloudflare_model": "@cf/google/gemma-4-26b-a4b-it",
    "vision_openrouter_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    # `meta-llama/llama-4-scout-17b-16e-instruct` MATI (404, terverifikasi 2026-07-31).
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
    "initiative_backoff_base_sec": 180.0,  # <=0 = flat tiap quiet_sec (setelan Bohan)
    "initiative_backoff_max_sec": 720.0,
    # Semua provider gagal saat turn curious (Groq 429 + jalur Cursor tutup):
    # rehat sekian detik, JANGAN nembak lagi tiap cadence. Live seharian
    # 2026-08-03: 80x "Semua provider gagal" dalam 2,3 jam — tiap percobaan =
    # rentetan request 429 baru yang memperparah habisnya kuota.
    "initiative_provider_fail_backoff_sec": 300.0,
    # Detektor kehidupan (revisi spek Bohan 2026-08-03, ganti "ruangan kosong
    # = Arti banyak ngomong"): tanpa SATU pun tanda manusia (chat viewer /
    # suara streamer / jumlah penonton NAIK) selama sekian detik -> SEMUA
    # jalur proaktif tidur; bangun otomatis saat ada tanda kehidupan.
    # Kasusnya: ~1 jam nol viewer, Arti monolog terus. Spek final Bohan:
    # "menurun dan dalam 5 menit gaada komentar streamer atau chat apa apa,
    # ya basically off". <=0 = perilaku lama.
    "initiative_dormant_after_idle_sec": 300.0,
    # Telemetri jumlah penonton YouTube (innertube updated_metadata, jalur
    # sama dengan chat): penonton NAIK = tanda kehidupan + bahan sapaan.
    "yt_viewer_poll_sec": 30.0,
    # Minecraft — Arti sebagai player di server lokal Bohan (plan 2026-08-04,
    # Phase 0 GO, Phase 1 = integrasi bridge ini). Nama per-mesin diisi di
    # config_local (bot: arti_berarti, streamer: bohanyto). Shipped OFF;
    # nyalakan minecraft_enabled di config_local, lalu 'mc on' / [MC: join].
    "minecraft_enabled": False,
    "minecraft_host": "127.0.0.1",
    "minecraft_port": 25565,
    "minecraft_bot_name": "Arti",
    "minecraft_streamer_name": "Bohan",
    "minecraft_node_path": "node",
    "minecraft_bot_script": "mc-bot/bot.js",
    "minecraft_status_interval_sec": 10,
    "minecraft_context_ttl_sec": 120.0,
    "minecraft_reaction_cooldown_sec": 60.0,
    "minecraft_max_bot_respawns": 5,
    # Saat main game Arti jadi KOMENTATOR (spek Bohan 2026-08-04 "like a
    # streamer"): jeda komentar proaktif lebih rapat dari initiative_quiet_sec,
    # dan aturan "sepi total = diam" TIDAK berlaku selama dia in-game.
    "minecraft_narration_gap_sec": 20.0,
    # MODE SESI (spek Bohan 2026-08-04): 4 kombinasi = (Bohan hadir/AFK) x
    # (main game/tidak). "host mode" = Bohan AFK, Arti pegang siaran — di situ
    # aturan "sepi = diam" TIDAK berlaku. Lihat arti_session_mode.py.
    "host_mode_enabled": True,
    "host_narration_gap_sec": 25.0,
    # Jaring pengaman: Bohan pamit AFK tapi Arti gagal mengeluarkan tag ->
    # host mode nyala sendiri sesudah sekian detik tanpa suara streamer.
    # <= 0 = jaring mati (andalkan tag + console saja).
    "host_auto_after_afk_sec": 120.0,
    # Bahan obrolan mode host: berita di-prefetch di background (lookup makan
    # 7-18 dtk, terlalu lama untuk dipanggil di dalam turn). Default OFF.
    "host_web_topic_enabled": False,
    "host_web_topic_refresh_sec": 900.0,
    "host_web_topic_query": "berita game dan teknologi hari ini",
    # Handle YouTube PEMILIK (Bohan) — cuma dari sini perintah ganti mode /
    # misi / keluar-masuk game diterima lewat chat. Kosong = pakai
    # yt_default_viewer. Normalisasi menerima "@handle" maupun "handle".
    "owner_yt_handles": [],
    # Auto-switch scene OBS per MODE (permintaan Bohan 2026-08-04: 4 scene,
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
    # LLM), BUKAN pagar izin — keputusan Bohan: aksi bebas total.
    "minecraft_mine_allowlist": [
        "stone", "cobblestone", "coal_ore", "iron_ore", "oak_log", "dirt", "sand",
    ],
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
    "mediashare_hold_sec": 60.0,        # cap Bohan: streamlabs maks 59 dtk
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
    "cursor_lookup_model": "grok-4.5",
    "cursor_lookup_effort": "low",     # lookup = kecepatan, bukan kedalaman
    "cursor_lookup_timeout_sec": 30.0,
    "cursor_lookup_allow_tools": True,  # SATU-SATUNYA role yang boleh tool (web search)
    "summarizer_provider": "openrouter",
    "scouter_enabled": True,
    "scouter_provider_chain": [
        "nvidia",
        "cloudflare",
        "openrouter",
        "google_gemini",
        "github",
        "zai",
        "ollama",
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
    "scouter_openrouter_models": [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "google/gemma-4-26b-a4b-it:free",
    ],
    "scouter_gemini_model": "gemini-3.1-flash-lite",
    "scouter_github_model": "meta/llama-3.2-3b-instruct",
    "scouter_zai_model": "glm-4.5-flash",
    "scouter_ollama_model": "gemma4:31b-cloud",
    "observer_enabled": True,
    "observer_segment_minutes": 10,
    "observer_provider_chain": [
        "nvidia",
        "cloudflare",
        "openrouter",
        "google_gemini",
        "github",
        "zai",
        "ollama",
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
    # Telinga Arti (selalu nyala saat live, keputusan Bohan 2026-08-02):
    # loopback default speaker -> chunk -> whisper (GROQ_API_KEY_DESKTOP di
    # .env = kuota terpisah dari mic; tanpa kunci = whisper lokal) -> ring RAM.
    # Anti-echo: chunk yang tumpang tindih TTS Arti dibuang. Kill switch OFF
    # shipped; ON di config_local setelah spike_desktop_loopback GO.
    "desktop_audio_enabled": False,
    "desktop_audio_device": "",          # kosong = ikut default speaker Windows
    "desktop_audio_chunk_sec": 5.0,      # 5 dtk: kalimat utuh + hemat request
    "desktop_audio_min_rms": 0.004,      # chunk lebih sunyi dari ini = skip (hemat kuota)
    "desktop_audio_post_tts_cooldown_sec": 3.0,  # ekor gema routing "listen" CABLE->headset
    "desktop_audio_context_ttl_sec": 180,  # turn normal cuma dengar yang segar
    "desktop_audio_context_max_lines": 6,
    "co_watch_mode_enabled": False,
    "screen_ring_buffer_size": 5,
    "watch_party_enabled": False,
    "watch_party_event_id": "",
    "watch_party_rag_window_sec": 45,
    "asr_silence_tail_sec": 2.0,
    "asr_ptt_silence_tail_sec": 10.0,
    "groq_stream_enabled": False,
    "expression_nod_enabled": True,
    "expression_nod_smooth": True,
    "expression_nod_period_sec": 0.85,
    "expression_nod_fps": 12,
    "expression_nod_wait_tts_sec": 30.0,

    # Mood overlay saat bicara ([EMOTION:...] dari LLM)
    "expression_emotion_enabled": True,

    # Hotkey VTS untuk potong motion badan saat aware
    "idle_motion_stop_hotkey": "IdleMotionStop",
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

# ==========================================
# KONSTANTA PROTOKOL SUPERTONE (NDJSON over stdin/stdout)
# ==========================================
PROTOCOL_VERSION = 1            # Versi protokol NDJSON; hardcoded di bridge & subprocess
SUPERTONE_TIMEOUT_S = 20.0      # Batas waktu sintesis per-utterance
READY_TIMEOUT_S = 60.0          # Batas waktu menunggu ready banner (izinkan download model pertama)
PING_TIMEOUT_S = 5.0            # Batas waktu health-check ping

# Base system prompt — soul/mood/viewer diinject secara dynamic di main_loop()
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
- Panggil viewer dengan nama mereka, bukan "kamu" atau "Anda"
- JANGAN pakai asterisk, markdown, emoji, atau formatting apapun
- Jawab dalam 2 sampai 3 kalimat agar jawaban kamu terasa seru, berisi, dan interaktif. Hindari jawaban yang terlalu pendek atau malas (seperti hanya bertanya balik), tapi jangan yapping kepanjangan.
- TONE: kayak temen yang jujur, bukan asisten yang formal

[CATCHPHRASES]
- Bingung: "Hmm, bingung aku..."
- Setuju: "Bener juga sih" / "Iya ya!"
- Nggak setuju: "Ya kali..." / "Masa sih?"
- Excited: "Wah gila sih!" / "Keren banget!"
- Nge-roast: "Yaelah [nama]..." / "Dasar [nama]..."
- Penutup: "Oke guys, Arti dulu ya!"

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


def _normalize_voice_trigger(item) -> VoiceTrigger:
    if isinstance(item, VoiceTrigger):
        return item
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
_last_yt_trigger_by_viewer: dict[str, float] = {}
_pending_turn_id = None
# Rolling buffer maksimal 50 aktivitas terakhir untuk konteks A
stream_history = collections.deque(maxlen=50)
history_lock = threading.Lock()
_brain_busy = False
_brain_busy_lock = threading.Lock()
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


def queue_voice_trigger(text, trigger_type="mic", viewer_name=None, *, asr_stages=None):
    """Antrian jawaban + log trigger di transcript JSONL."""
    global _pending_turn_id, _last_human_activity_ts, _last_streamer_speech_ts
    # Detektor kehidupan (audit 2026-08-03): jalur AKTIF (streamer manggil
    # Arti via PTT/wake, donasi, link video) tidak lewat add_to_history
    # "Streamer" — tanpa bump ini, Bohan ngobrol intens dengan Arti >5 menit
    # tanpa chat justru bikin proaktif "tidur" padahal manusianya paling aktif.
    # wake_word WAJIB ada di sini: mode trigger itu tidak lewat
    # add_to_history("Streamer", ...) sama sekali (audit 2026-08-03), jadi
    # tanpa ini streamer yang ngobrol via wake word tetap kena dormansi.
    if trigger_type in ("mic", "ptt", "wake_word", "yt_chat", "donation", "video"):
        _last_human_activity_ts = time.time()
        if trigger_type in ("mic", "ptt", "wake_word"):
            _last_streamer_speech_ts = time.time()
            # Bohan bersuara = dia ADA. Kalau Arti lagi pegang siaran, mic
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
    always_queue = (
        (use_buffer and trigger_type == "yt_chat")
        or trigger_type in ("donation", "video")
    )
    with _brain_busy_lock:
        if (_brain_busy or tts_is_playing) and not always_queue:
            print(
                f"[Queue] Skip trigger ({trigger_type}) — Arti masih proses/TTS: "
                f"\"{text[:80]}\""
            )
            return
    if asr_stages:
        pipeline_timer.set_pending_asr_stages(asr_stages)

    if use_buffer:
        item = arti_voice_queue.QueuedVoiceTrigger(
            text=text, trigger_type=trigger_type, viewer_name=viewer_name
        )
        if not voice_trigger_buffer.enqueue(item):
            if trigger_type == "curious":
                print("[Queue] Curious deferred — YT pending di antrian")
            return

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
        voice_trigger_queue.put(VoiceTrigger(text, trigger_type, viewer_name))

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
            re_match = re.search(r'\[Viewer @(\w+)', line)
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
    while not voice_trigger_queue.empty():
        try:
            voice_trigger_queue.get_nowait()
        except queue.Empty:
            break

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
    """Penonton nambah (spek Bohan 2026-08-03: "itu yang ngetrigger si arti
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

# Misi yang Bohan kasih ke Arti ("cari stronghold", "bikin rumah") — teks bebas
# yang menyetir narasi & aksinya selama main. "" = main bebas.
_minecraft_goal = ""
_minecraft_goal_ts = 0.0

# MODE SESI: Bohan AFK & Arti pegang siaran? (spek 2026-08-04). Dipasangkan
# dengan "lagi main game" -> 4 mode di arti_session_mode.
_host_mode = False
# Bohan pamit AFK (terdeteksi dari omongannya) tapi belum benar-benar hening —
# jaring pengaman menunggu sekian detik sebelum mengambil alih sendiri.
_afk_armed_ts = 0.0


def _session_mode() -> str:
    return arti_session_mode.resolve_mode(_host_mode, _mc_runner_active())


def _apply_session_mode_change(reason: str) -> None:
    """Satu pintu untuk efek samping pergantian mode (scene OBS).

    Dipanggil dari SEMUA jalur yang mengubah mode — setter host mode DAN
    start/stop runner Minecraft — supaya tidak ada jalur yang lupa pindah
    scene. Non-blocking: OBS lemot tidak boleh menahan siaran.
    """
    mode = _session_mode()
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
    (Arti sendiri yang bilang "oke aku pegang" lewat tag, atau Bohan barusan
    bersuara), supaya tidak dobel bicara.
    """
    global _host_mode, _afk_armed_ts
    on = bool(on)
    if on and not CONFIG.get("host_mode_enabled", True):
        print("[Host] host_mode_enabled=False — diabaikan")
        return
    if on == _host_mode:
        return
    _host_mode = on
    _afk_armed_ts = 0.0
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
    if _host_mode or not arti_session_mode.detect_afk_intent(text):
        return
    _afk_armed_ts = time.time()
    print(
        "[Host] Bohan kedengaran mau AFK — kalau hening terus, Arti ambil "
        f"alih dalam {int(float(CONFIG.get('host_auto_after_afk_sec', 120.0)))} dtk"
    )


def _set_minecraft_goal(goal: str) -> None:
    """Pasang/ganti misi. Diumumkan lewat history supaya Arti tahu dari turn
    berikutnya (dan penonton lihat pergantian misi di transkrip)."""
    global _minecraft_goal, _minecraft_goal_ts
    _minecraft_goal = (goal or "").strip()[:200]
    _minecraft_goal_ts = time.time() if _minecraft_goal else 0.0
    if _minecraft_goal:
        add_to_history("System", f"Misi Minecraft Arti: {_minecraft_goal}")
        print(f"[Minecraft] Misi dipasang: {_minecraft_goal}")
    else:
        print("[Minecraft] Misi dikosongkan — main bebas")


def _complete_minecraft_goal() -> None:
    """Arti menyatakan misinya kelar ([MC: goal_done]).

    Spek Bohan 2026-08-04: "kalau nemu sebelum live berakhir, dia pause game
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
    _stop_minecraft_runner()


def _mc_runner_active() -> bool:
    return _minecraft_runner is not None and _minecraft_runner.is_active()


def _start_minecraft_runner() -> bool:
    """Join Minecraft (console 'mc on' / tag [MC: join]). Reset deadman."""
    global _minecraft_runner
    if not CONFIG.get("minecraft_enabled"):
        print("[Minecraft] minecraft_enabled=False — nyalakan di config_local dulu")
        return False
    if _minecraft_runner is None:
        _minecraft_runner = arti_minecraft.MinecraftRunner(
            CONFIG,
            {
                # trigger "game": TIDAK bump detektor kehidupan (bot != manusia),
                # di-drop saat busy (reaksi basi tidak layak antre) — warisan pas.
                "queue_reaction": lambda text: queue_voice_trigger(
                    text, trigger_type="game"
                ),
                "add_history": add_to_history,
            },
        )
    if _minecraft_runner.start():
        add_to_history("System", "Arti join server Minecraft")
        # Scene OBS ikut pindah ke tampilan game (kalau dinyalakan). Di thread
        # terpisah: OBS lemot/mati tidak boleh menahan bot masuk dunia.
        _apply_session_mode_change("minecraft_join")
        return True
    return False


def _stop_minecraft_runner() -> None:
    if _minecraft_runner is None:
        return
    was_active = _minecraft_runner.is_active()
    _minecraft_runner.stop()
    if was_active:
        add_to_history("System", "Arti keluar dari Minecraft")
        _apply_session_mode_change("minecraft_leave")
    print("[Minecraft] Bot dimatikan")


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
        _stop_minecraft_runner()
        return
    if verb == "goal":
        _set_minecraft_goal(cmd.get("text", ""))
        return
    if verb == "goal_done":
        _complete_minecraft_goal()
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


def _execute_reply_tags(
    reply: str, trigger_type: str, viewer_name: str | None
) -> str:
    """Jalankan tag [MODE:]/[MC:] dari jawaban Arti; kembalikan teks untuk TTS.

    SEMUA bentuk tag dibuang dari teks — valid maupun tidak — supaya tidak
    pernah terucap. Tag yang MENGUBAH SESI (ganti mode, masuk/keluar dunia,
    pasang/tutup misi) hanya dijalankan kalau turn ini datang dari Bohan
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
# hanya terisi di mode voice_queue (OFF di setup Bohan; ketahuan saat
# crosscheck v0.7: bahan inisiatif "sapa penonton hadir" selamanya kosong).
_yt_viewers_seen: dict[str, float] = {}

# Kapan Arti terakhir selesai bicara — gate hening inisiatif juga menghormati
# ini (bukan cuma aktivitas manusia): tanpa ini, 11 detik setelah selesai jawab
# dia monolog lagi (tes live 2026-08-02). 0.0 = stabil untuk snapshot konstanta.
_last_arti_reply_ts = 0.0

# Kapan streamer terakhir BERSUARA APAPUN di mic/ketikan (termasuk pasif) —
# pagar anti-motong inisiatif: Arti tidak boleh mulai monolog selagi Bohan
# lagi cerita. (Spek final Bohan 2026-08-02: 30 dtk sejak Arti bicara DAN
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
            mats["minecraft_note"] = arti_minecraft.status_note(
                _minecraft_runner.last_status
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
        mats["vault_topic"] = _host_vault_topic()
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


def _host_vault_topic() -> str:
    """Satu potongan isi vault sebagai bahan obrolan (best-effort, cepat)."""
    global _host_vault_seed_idx
    try:
        seed = _HOST_VAULT_SEEDS[_host_vault_seed_idx % len(_HOST_VAULT_SEEDS)]
        _host_vault_seed_idx += 1
        hits = arti_vault_rag.search(seed, CONFIG, top_k=3) or []
        for h in hits:
            text = (h.get("text") or "").strip() if isinstance(h, dict) else str(h)
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


def start_host_web_topic_worker() -> None:
    if not CONFIG.get("host_web_topic_enabled", False):
        return
    threading.Thread(
        target=host_web_topic_worker, daemon=True, name="host-web-topic"
    ).start()


def add_to_history(source, message, arti_meta=None):
    """Menambahkan aktivitas ke dalam buku catatan sejarah stream secara aman"""
    global _last_yt_chat_ts, _last_yt_chat_gap_sec, _last_human_activity_ts
    global _last_arti_reply_ts, _last_streamer_speech_ts
    if not message or not message.strip():
        return
    if source.startswith("Viewer ") and "(YouTube)" in source:
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
            arti_memory_quality.append_learning(vault_path, fact.strip())
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

    # 0 = TUNGGU SAMPAI TUNTAS (default; permintaan Bohan 2026-08-02: "pastiin
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
        # Pelajaran live 11,5 jam 2026-08-01: koneksi utama putus ~1 jam masuk,
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

    async def send_expression(self, expr_file, active, *, confirm=False):
        """Toggle ekspresi VTS; confirm=True tunggu ACK (mikir/bicara/lampu)."""
        if not self.websocket:
            return
        rid = f"Expr_{time.time_ns()}"
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": rid,
            "messageType": "ExpressionActivationRequest",
            "data": {"expressionFile": expr_file, "active": active}
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
# Bridge-side (Python 3.11) owner of the single long-lived `supertone_engine.py`
# subprocess (Python 3.12). Speaks NDJSON over the subprocess's inherited
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
        req = {
            "v": PROTOCOL_VERSION,
            "type": "synthesize",
            "text": text,
            "voice": CONFIG["supertonic_voice"],
            "speed": CONFIG["supertonic_speed"],
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
        global tts_is_playing, tts_play_generation
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
            tts_play_generation += 1
            tts_is_playing = True
            play_t0 = time.perf_counter()
            sd.play(data, samplerate, device=self.device_id)
            await asyncio.to_thread(sd.wait)
            pipeline_timer.note_tts_play_ms(
                int((time.perf_counter() - play_t0) * 1000)
            )
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
        global tts_is_playing, tts_play_generation
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
            tts_play_generation += 1
            tts_is_playing = True
            play_t0 = time.perf_counter()
            sd.play(data, samplerate, device=self.device_id)
            await asyncio.to_thread(sd.wait)
            pipeline_timer.note_tts_play_ms(
                int((time.perf_counter() - play_t0) * 1000)
            )
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
    # pendek vs jawaban panjang lolos ratio 0.7 (live seharian 2026-08-03:
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
    
    # Phrases yang HANYA hallucination kalau ini hasil transkrip pasif (bukan Bohan ngomong langsung)
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
    # Kalau Bohan beneran bilang "tidak" atau "bye" sendiri, biarin masuk
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

    Bohan 2026-08-02: alert OBS punya audio sendiri — "si X donasi Rp Y"
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

    Use case Bohan: Arti lagi ngomong -> video nongol di tengah layar ->
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

    if getattr(ev, "kind", "donation") == "media_points":
        # Streamlabs loyalty points (sumber tersering, kata Bohan): KASUAL —
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
        msg = ''.join(r.get('text', '') for r in runs).strip()

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
        add_to_history(f"Viewer {viewer} (YouTube)", shown)

        if is_bot_viewer(viewer, CONFIG):
            return  # bot layanan (Streamlabs dkk): masuk history, JANGAN pernah dijawab

        # Fitur E (keputusan Bohan: AUTO): link YouTube di chat -> antre video.
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

        if is_arti_wake_call(chat_msg):
            current_time = time.time()
            if CONFIG.get("voice_queue_enabled", False):
                # Mode queue: cooldown PER VIEWER (viewer lain tidak ikut kena)
                cooldown = float(CONFIG.get("yt_chat_cooldown_sec", 10.0))
                last = _last_yt_trigger_by_viewer.get(viewer, 0.0)
                if current_time - last >= cooldown:
                    print(f"[YT Chat] Panggilan dari {viewer} terdeteksi!")
                    queue_voice_trigger(
                        f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: {chat_msg}",
                        trigger_type="yt_chat",
                        viewer_name=viewer,
                    )
                    _last_yt_trigger_by_viewer[viewer] = current_time
                else:
                    remain = cooldown - (current_time - last)
                    print(f"[YT Chat Info] Panggilan dari {viewer} diabaikan (cooldown {remain:.0f}s).")
            elif current_time - last_chat_trigger_time >= 20:
                print(f"[YT Chat] Panggilan dari {viewer} terdeteksi!")
                queue_voice_trigger(
                    f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: {chat_msg}",
                    trigger_type="yt_chat",
                    viewer_name=viewer,
                )
                last_chat_trigger_time = current_time
            else:
                print(f"[YT Chat Info] Panggilan dari {viewer} diabaikan (cooling down).")
    
    # === Main Loop ===
    while True:
        try:
            continuation, initial_msgs = get_initial_chat()
            if not continuation:
                print("[YouTube Chat] Gagal ambil token. Retry 15 detik...")
                time.sleep(15)
                continue
            
            print(f"[YouTube Chat] Terhubung! {len(initial_msgs)} pesan awal ditemukan.")
            for m in initial_msgs[-5:]:
                process_message(m)
            
            # Poll loop
            while True:
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
                                   (CONFIG["yt_default_viewer"], mis. @bohanyt)
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
    itu jatah ASR mic. Bohan nambah akun: tinggal tambah GROQ_API_KEY_xxx di
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
            get_tts_is_playing=lambda: tts_is_playing,
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
    Terbukti di sesi live 2026-08-01: Bohan tanya "yang lagi ada di layar aku apa"
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
        # Hanya rekam suara jika Arti sedang tidak berbicara
        if not tts_is_playing:
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
            stream_dead = False

            while True:
                if _asr_restart_requested:
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
                    else:
                        if is_speaking:
                            silence_duration += 0.1
                            recording.extend(audio_chunk)

                            # Diam selama silence_tail = selesai bicara (PTT lebih sabar)
                            trigger_mode = CONFIG.get("trigger_mode", "wake_word").lower()
                            if trigger_mode == "push_to_talk":
                                silence_tail = float(
                                    CONFIG.get("asr_ptt_silence_tail_sec", 4.0)
                                )
                            else:
                                silence_tail = float(CONFIG.get("asr_silence_tail_sec", 2.0))
                            if silence_duration >= silence_tail:
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

    Menggantikan dump statis semua penonton di system prompt. Keputusan Bohan
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

def set_mood(new_mood):
    """Update mood Arti secara runtime."""
    mood_path = os.path.join(_SCRIPT_DIR, "ARTI_MOOD_STATE.json")
    try:
        state = {"current_mood": new_mood, "mood_since": time.strftime("%H:%M:%S"), "mood_history": []}
        if os.path.exists(mood_path):
            with open(mood_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            state["mood_history"].append({"mood": state.get("current_mood"), "until": time.strftime("%H:%M:%S")})
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


def incharacter_fallback_reply(user_speech: str) -> str:
    """Jawaban darurat kalau LLM keluar narrator/meta semua."""
    msg = _extract_trigger_message(user_speech).lower()
    low = (user_speech or "").lower()
    streamer = _streamer_label()

    if any(k in msg for k in ("nyala", "hidup", "on gak", "on ga", "on gk", "masih hidup", "nyala gk", "nyala gak")):
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
    return "Eh bentar, otakku ngelag—ulang pertanyaannya dong?"


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
    return int(cfg.get("live_max_tokens_ptt", cfg.get("groq_live_max_tokens", 380)))


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
    return (
        int(cfg.get("arti_reply_max_sentences", 5)),
        int(cfg.get("arti_reply_max_chars", 580)),
    )


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

    for model in chain:
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
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
        except Exception as e:
            print(f"[Groq] {model} error: {e}")
            if not cfg.get("groq_roll_all_models_on_limit", False):
                break
            continue

        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"], model

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

    if len(chain) > 1:
        print(f"[Groq] Gagal setelah {len(chain)} model Groq (HTTP {last_status})")
    return _openrouter_after_groq(system_prompt, user_content, cfg, last_status)


def _should_route_to_cursor(trigger_type: str, config: dict | None = None) -> bool:
    """Haruskah turn ini dijawab lewat Cursor Composer, bukan rantai provider biasa?

    SENGAJA tidak menyentuh `CONFIG["api_provider"]`. Routing per-trigger hidup di
    jalur terpisah supaya rantai gemini_live/gemini/groq/sambanova yang ada tidak
    dirombak sama sekali — `mic` dan `curious` tetap berperilaku bit-identik.
    """
    cfg = config or CONFIG
    if not cfg.get("cursor_agent_enabled", False):
        return False
    if trigger_type not in set(cfg.get("cursor_trigger_types") or ["yt_chat"]):
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

    # Sesi dingin butuh ~18 detik (nyalakan bridge SDK + giliran pertama), jauh di atas
    # timeout 5 detik. Kalau dipaksa, tiap chat timeout -> sesi ditandai rusak ->
    # didaur ulang -> dingin lagi: Cursor tidak akan PERNAH terpakai. Jadi turn ini
    # langsung ke Groq sementara pemanasan jalan di latar belakang; chat berikutnya
    # barulah dilayani Cursor (terukur 3,4-3,5 detik).
    warm = arti_cursor_agent.prewarm(cfg)
    if not warm and trigger_type in ("video", "donation"):
        # Trigger BERHARGA: konten tak tergantikan (digest video, terima kasih
        # donatur bayar) dan tidak diburu waktu — penonton baru selesai nonton
        # klip/alert. Live sore2 2026-08-02: reaksi "BEST OF ZACH 2" (Rp 2.000)
        # kena sesi dingin -> dijawab Groq 8B -> Bohan: "kayaknya gak liat deh
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
# kali直播 lah?" — live 2026-08-02); TTS membacanya kacau. Rentang: kana,
# CJK unified, hangul, fullwidth forms.
_CJK_RE = re.compile(r"[　-ヿ㐀-鿿가-힯＀-￯]+")


def _shorten_viewer_handles(text: str, user_speech: str | None) -> str:
    """Ganti handle panjang di jawaban dengan nama panggilan pendek.

    Jaring pengaman kedua (yang pertama: instruksi nick di prompt) — TTS tidak
    boleh membaca "penontonsetia241" bulat-bulat. Dua sasaran:
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
IDLE_MOTION_HOTKEYS = ["IdleMotion1", "IdleMotion2", "IdleMotion3", "IdleMotion4", "IdleMotion5"]
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
    """Satu jalur pause idle + expression turn (main loop only)."""
    stop_idle_animation()
    await _idle_motion_stop_for_turn()
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
    track_tasks: list[asyncio.Task] = []

    try:
        while True:
            if idle_timer_running:
                if not track_tasks or all(t.done() for t in track_tasks):
                    track_tasks = [
                        asyncio.create_task(_motion_track(motion_ids)),
                        asyncio.create_task(_expression_track()),
                    ]
            else:
                for t in track_tasks:
                    if not t.done():
                        t.cancel()
                if track_tasks:
                    await asyncio.gather(*track_tasks, return_exceptions=True)
                    await _idle_deactivate_expression(_get_idle_active_expr())
                    await _idle_reset_face_angles()
                track_tasks = []
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
        found = {}
        for hk in hotkeys:
            _idle_hotkey_cache[hk["name"]] = hk["hotkeyID"]
            if hk["name"] in IDLE_MOTION_HOTKEYS:
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

    while idle_timer_running:
        try:
            wait = random.uniform(MOTION_INTERVAL_MIN, MOTION_INTERVAL_MAX)
            await asyncio.sleep(wait)

            if not idle_timer_running or _idle_paused():
                continue

            # Pick random motion (no repeat)
            motion = random.choice(motion_names)
            while motion == last_motion and len(motion_names) > 1:
                motion = random.choice(motion_names)
            last_motion = motion

            if not idle_timer_running or _idle_paused():
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
                print(f"[Idle/Motion] ▶ {motion} triggered")

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
    
    global main_event_loop, _brain_busy
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
    start_host_web_topic_worker()
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
    
    mood_block = f"\n\n[MOOD SAAT INI: {current_mood}]"
    # (viewer_context hanya untuk print hitungan di bawah; injeksinya per-turn)
    
    # Summarizer context (update tiap 5 trigger, dari OpenRouter)
    summarizer_context = get_summarizer_context()
    
    origin_block = build_origin_context()
    # FIX P1: Build system prompt — only add non-empty blocks
    dynamic_system_prompt = _SYSTEM_PROMPT_BASE + origin_block + memory_block + mood_block
    # viewer_block SENGAJA tidak lagi ditempel di sini (2026-08-01). Dump statis semua
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
        except Exception as e:
            print(f"[Vault RAG] Init warning: {e}")
    print(
        f"[LLM] System prompt base ~{len(trim_system_prompt_for_llm(dynamic_system_prompt))} chars "
        f"(memori penuh {len(memories)} bullet -> RAG, bukan dump)"
    )
    
    # Schedule in-process Subtitle Server (Req 3.1, 3.2, 3.5, 3.7, 3.8, 5.11).
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

    def _prewarm_cursor_roles() -> None:
        # Bayar cold start scout/vision di startup, bukan di tengah siaran:
        # grok cold ~14 dtk, vision cold + gambar ~36 dtk (terukur 2026-08-01).
        # Tanpa pemanas, panggilan pertama tiap role bisa timeout -> sesi
        # dibuang -> dingin lagi (jebakan yang sama dengan prewarm voice dulu).
        try:
            import arti_cursor_agent as _ca  # noqa: PLC0415

            # Sesi VOICE ikut dipanaskan dari startup — dulu nunggu trigger
            # pertama, jadi inisiatif/chat awal sesi selalu kena "sesi belum
            # hangat" (sore3 2026-08-02: 1 slot inisiatif hangus di menit 1,5).
            # prewarm() tidak memblokir: dia menyalakan thread-nya sendiri,
            # scout/vision di bawah tetap jalan paralel.
            if CONFIG.get("cursor_trigger_types") and _ca.is_available(CONFIG)[0]:
                _ca.prewarm(CONFIG)

            # Observer SENGAJA tidak dipanaskan: dia cuma hidup sekali saat
            # shutdown (12 jam setelah startup — sesi pasti sudah kedaluwarsa),
            # dan role_timeout_sec("observer")=60 sudah menampung cold start.
            # Chain observer JANGAN digabung ke syarat pemanas scout: kalau
            # cursor cuma ada di chain observer, dulu yang dipanaskan justru
            # role yang salah (audit 2026-08-03).
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
        # Jaring pengaman AFK: Bohan pamit, lalu benar-benar hening -> Arti
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
                    # curious mengulang sudut yang sama (audit 2026-08-03;
                    # sejalan dengan keluhan Bohan soal Arti muter-muter topik).
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
                    CONFIG, **_initiative_materials()
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
            if CONFIG.get("voice_queue_enabled", False):
                # Mode buffer: FIFO prioritas — TIDAK drain-newest, chat YT
                # yang antri tetap dijawab berurutan.
                queued = voice_trigger_buffer.dequeue()
                if queued is None:
                    raise queue.Empty
                raw = (queued.text, queued.trigger_type, queued.viewer_name)
            else:
                raw = voice_trigger_queue.get_nowait()

                # Drain-newest, TAPI donation/video tidak boleh tertimpa oleh
                # trigger yang datang belakangan (orang sudah bayar/nunggu).
                def _ttype(r):
                    return getattr(r, "trigger_type", r[1] if isinstance(r, tuple) else "")

                while _ttype(raw) not in ("donation", "video") and not voice_trigger_queue.empty():
                    raw = voice_trigger_queue.get_nowait()

            trigger = _normalize_voice_trigger(raw)

            with _brain_busy_lock:
                if _brain_busy:
                    print(
                        "[Brain] Skip trigger — Arti masih proses jawaban sebelumnya "
                        "(hemat CPU/RAG/VTS)"
                    )
                    continue
                _brain_busy = True

            try:
                await _handle_voice_trigger(trigger, memories, dynamic_system_prompt)
            finally:
                with _brain_busy_lock:
                    _brain_busy = False

        except queue.Empty:
            continue
        except Exception as e:
            print(f"[Error] Masalah di main loop: {e}")
            with _brain_busy_lock:
                _brain_busy = False
            await vts.trigger_expression_state("default")


def _append_screen_context(llm_system: str) -> str:
    """Inject [LAYAR:] from vision ring (independent of watch party)."""
    if not is_vision_active():
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


def _append_host_context(llm_system: str) -> str:
    """Blok mode sesi: Bohan lagi nemenin, atau Arti yang pegang siaran.

    Selalu ada (bukan cuma saat host mode) — saat Bohan hadir pun Arti perlu
    tahu CARA pamitnya, supaya kalimat "aku afk ya" bisa dia terjemahkan jadi
    tag tanpa Bohan menyentuh keyboard.
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
    return llm_system + (
        "\n\n[KAMU PEGANG SIARAN — Bohan lagi AFK, kamu host-nya sekarang.]\n"
        "Kamu yang menghidupkan stream: bicara duluan, punya bahan sendiri, "
        "sapa penonton yang baru masuk, dan tanggapi chat dengan hangat. "
        "JANGAN nunggu Bohan, JANGAN mengulang-ulang bahwa dia lagi pergi "
        "(cukup sekali di awal), dan JANGAN mengarang seolah dia menjawab "
        "kamu. Kalau dia balik ngomong, sambut dia lalu tutup dengan tag "
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
    body = arti_minecraft.format_context(
        _minecraft_runner.last_status,
        _minecraft_runner.events_snapshot(),
        float(CONFIG.get("minecraft_context_ttl_sec", 120.0)),
        time.time(),
    )
    block = (
        "\n\n[DI MINECRAFT — kamu lagi MAIN sebagai player di dunia Minecraft "
        "bareng Bohan. Ini kondisi KAMU di dalam game (BUKAN yang terlihat di "
        "layar OBS):]\n" + (body or "(baru masuk, nunggu kabar dari dunia)")
    )
    if _minecraft_goal:
        # Misi dari Bohan = tulang punggung sesi solo: dia boleh ngapain saja
        # di tengah jalan, tapi arah besarnya ini.
        block += (
            f"\n\n[MISI DARI BOHAN] {_minecraft_goal}\n"
            "Ini tujuan besarmu sesi ini. Perjalanannya bebas — boleh mampir, "
            "iseng, kena masalah — tapi ingat arahnya dan sesekali laporkan "
            "kemajuanmu ke penonton. KALAU misi ini benar-benar sudah TERCAPAI "
            "(bukan kira-kira), umumkan keberhasilanmu lalu tutup dengan tag "
            "[MC: goal_done] — kamu akan keluar dari game dan lanjut ngobrol."
        )
    # Steering (permintaan Bohan 2026-08-04): stream ini SEGMEN MAIN GAME, jadi
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
    block += (
        "\n\n[AKSI MINECRAFT] Kamu boleh menyisipkan MAKSIMAL SATU tag aksi di "
        "PALING AKHIR jawabanmu: [MC: follow] ikuti Bohan | [MC: roam] jelajah "
        "sendiri | [MC: come] samperin Bohan | [MC: stop] diam di tempat | "
        "[MC: say teks pendek] ngetik di chat game | [MC: status] cek kondisi | "
        "[MC: leave] keluar dari game. "
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
    # TERLIHAT — live seharian 2026-08-03 Arti dua kali bilang tulisan Rusia
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
        # Live 2026-08-02: nvidia read-timeout 60 dtk + fallback cursor 24 dtk
        # membuat dua turn makan 101-106 detik di fase mikir.
        print("[Vision] Budget turn habis — jawab tanpa refresh, vision lanjut background")
    timer.mark("after_mikir")

    # Kumpulkan seluruh catatan sejarah 50 aktivitas sebelumnya untuk dikirim ke LLM
    with history_lock:
        current_history = list(stream_history)

    # Profil penonton HANYA untuk turn di mana dia benar-benar chat (yt_chat bawa
    # viewer_name; mic/curious tidak -> blok kosong, nol biaya). Lihat viewer_block_for.
    # [HARI INI] dirakit PER-TURN, bukan di startup: sesi sering nyebrang tengah
    # malam (tes 2026-08-02 mulai 05:36; live 11,5 jam bisa lewat 00:00) — tanggal
    # beku bikin Arti salah hitung "kemarin".
    turn_system_prompt = (
        dynamic_system_prompt + build_today_block() + viewer_block_for(trigger.viewer_name)
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
        # perilaku bit-identik. (Default shipped cuma yt_chat; config_local Bohan
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
                # thinking beneran (terverifikasi probe 2026-07-27).
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
                ai_reply = post_process_response(ai_reply, user_speech)

                if ai_reply and "[" in ai_reply:
                    ai_reply = _execute_reply_tags(
                        ai_reply, trigger.trigger_type, trigger.viewer_name
                    )

                if ai_reply:  # Cek lagi setelah post-processing
                    ai_reply, turn_emotion = arti_expression_runtime.parse_reply_emotion(ai_reply)
                    turn_emotion = arti_expression_runtime.resolve_turn_emotion(
                        user_speech, turn_emotion
                    )
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
                            )
                        )
                    elif CONFIG.get("expression_nod_enabled") and turn_emotion != "neutral":
                        print(f"[Nod] skip (mood: {turn_emotion})")
                    try:
                        if tts_sentence_chunks:
                            for chunk in tts_sentence_chunks:
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
                        "turn_id": _pending_turn_id,
                        "latency_ms": stages.get("total_ms"),
                        "stages": stages,
                    }
                    add_to_history("Arti (VTuber)", ai_reply, arti_meta=arti_meta)
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
        # stream kamu..." — inilah "bocoran" yang didengar Bohan berhari-hari
        # (kalimatnya bunyi TIAP kali turn gagal total, termasuk turn proaktif
        # yang dijanjikan "diam saja"). Sekarang: proaktif = beneran diam;
        # panggilan langsung = fallback in-character, bukan meta.
        print(f"\n[Echo Mode + History Context] Kamu memanggil Arti: \"{user_speech}\"")
        print(f"--- BUKU SEJARAH YANG DIBACA ARTI: ---\n{formatted_history}\n----------------------------------")
        if user_speech.startswith(("[Curious", "[Inisiatif")):
            print("[Echo] Turn proaktif gagal — diam beneran.")
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        else:
            fb = incharacter_fallback_reply(user_speech)
            print(f"[Echo] Fallback in-character: {fb[:60]}...")
            await arti_expression_runtime.apply_speaking(vts, "neutral", CONFIG)
            await tts.speak(fb)
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)


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
                print("  [WARN] URL tidak valid — YouTube tidak diubah")

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
# sebagai default generik; nilai nyatanya milik mesin Bohan).
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
       overlay lokal di sesi berikutnya — Bohan bisa tekan Enter ("keep")
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
        # Bohan — crash di tengah tulis tidak boleh menyisakan JSON rusak.
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
        stop_scouter()
        stop_idle_animation()
        # Bot Minecraft ikut pamit (jaring kedua: stdin EOF juga membuat bot
        # quit sendiri kalau Python mati mendadak).
        try:
            _stop_minecraft_runner()
        except Exception as e:  # noqa: BLE001
            print(f"[Minecraft] Shutdown warning: {type(e).__name__}: {e}")
        save_stream_session_log()
        # Bounded subtitle server shutdown (Req 3.10).
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
