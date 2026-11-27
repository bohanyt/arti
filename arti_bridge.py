import asyncio
import json
import os
import uuid
from pathlib import Path
import sys
import unicodedata
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version

import arti_endpoint
import arti_env

# Rehearsals use only their generated non-secret config and Codex login.
if not os.environ.get("ARTI_REHEARSAL_CONFIG"):
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
from arti_audio_buffer import AudioChunkQueue, UtteranceBuffer
import threading
from contextlib import contextmanager, ExitStack
import traceback
from contextvars import ContextVar
from dataclasses import dataclass, replace
import time
import collections
import random
import itertools
import contextlib
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
import arti_state
import arti_mind
from arti_runtime.soul_block import SOUL_MARKER, assemble_startup_prompt, build_soul_block
import session_transcript
import pipeline_timer
from pipeline_timer import PipelineTimer, format_latency_line
import arti_expression_runtime
import arti_vts_auth
import arti_screen_context
import arti_live_safety
from arti_live7_status_facts import LIVE7_AGENCY_FACT
from arti_live_safety import speech_control, speech_turn_epoch
from arti_screen_privacy import screen_privacy, STREAMER_TRIGGERS
import arti_timeline_guard
import arti_vision_client
import arti_curious
import arti_agy_agent
import arti_codex_agent
import arti_provider_health
import arti_desktop_audio
import arti_vad
import arti_http_util
import arti_executors
from arti_buffered_writer import BatchedWriter
import arti_voice_pipeline
import arti_groq_stream
import arti_wake
from arti_wake import is_arti_wake_call
import arti_yt_viewers
import arti_yt_paid
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
from arti_runtime import attention_desk, attention_bridge
from arti_runtime.host_story import playback_story, playback_current as host_story_playback_current
from arti_runtime import streamer_voice
from arti_runtime import mic_hygiene
from arti_runtime import thanks_once
from arti_runtime import live_rules
from arti_runtime import mic_priority
from arti_runtime import chat_rotation, chat_media
from arti_runtime import voice_context
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
_DEBUG_LOG_DIR = (str(Path(os.environ["ARTI_REHEARSAL_CONFIG"]).resolve().parent / "session_logs")
                  if os.environ.get("ARTI_REHEARSAL_CONFIG") else
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_logs"))
os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)
_DEBUG_LOG_PATH = os.path.join(_DEBUG_LOG_DIR, time.strftime("%Y-%m-%d_%H%M%S") + "_bridge.log")

class _TeeOutput:
    """Duplikasi stdout + stderr ke file log dan terminal secara bersamaan."""
    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file
        self._line_start = True
        self._write_lock = threading.Lock()
    def write(self, data):
        with self._write_lock:
            parts = []
            for part in data.splitlines(keepends=True):
                if self._line_start:
                    now = time.time()
                    parts.append(time.strftime("%H:%M:%S", time.localtime(now))
                                 + f".{int(now * 1000) % 1000:03d} ")
                parts.append(part)
                self._line_start = part.endswith(("\n", "\r"))
            self._write("".join(parts))

    def _write(self, data):
        # Terminal Windows bisa cp1252. Sebelum 27 Agu ini tidak pernah kena
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
        except Exception:
            pass
    def flush(self):
        with self._write_lock:
            self._flush()

    def _flush(self):
        try:
            self.stream.flush()
        except OSError:
            pass
        try:
            self.log_file.flush()
        except Exception:
            pass
    def isatty(self):
        return False

# Di bawah pytest modul ini diimpor ulang puluhan kali, dan tiap impor dulu
# melahirkan berkas log ~293 byte di session_logs/ — sampah yang menendang log
# siaran ASLI keluar lewat rotasi (diagnosa 12 Agu 2026). Suite sekali jalan =
# 3 berkas sampah. Jadi saat diuji, tulis ke devnull: perilaku Tee tetap sama,
# cuma tidak meninggalkan jejak.
_log_fh = BatchedWriter(open(
    os.devnull
    if ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules)
    else _DEBUG_LOG_PATH,
    "w",
    encoding="utf-8",
))
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
    "turn_llm_deadline_sec": 25.0,
    "groq_asr_deadline_sec": 10.0,
    
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
    "groq_models": [                                  # ADR 120: existing production text models only
        "openai/gpt-oss-120b",                        # ~500 t/s — primary
        "openai/gpt-oss-20b",                         # ~1000 t/s — fast
    ],
    
    # Konfigurasi SambaNova (Super Cepat, 48K RPD Gratis)
    "sambanova_api_key": os.environ.get("SAMBANOVA_API_KEY") or "YOUR_SAMBANOVA_API_KEY",
    "sambanova_model": "meta-llama-3.1-8b-instruct",  # atau "meta-llama-3.3-70b-instruct"
    
    "vts_api_port": 8002,                             # ARTI API port; HOST must use a different port.
    "vts_expected_model_name": "",                     # Exact ARTI modelName; empty disables identity check.
    # Folder model Live2D di VTube Studio (isi di config_local.json) —
    # dipakai untuk baca pose idle halus ArtiIdle*.exp3.json langsung dari model.
    "vts_model_dir": "",
    "vts_plugin_name": "HermesVTuberBridge",
    "vts_developer": "YourDeveloperName",
    "tts_voice": "id-ID-GadisNeural",                 # Indonesian female Edge TTS voice
    "virtual_cable_name": "CABLE Input",
    
    # Konfigurasi YouTube Live Chat (langsung dari YouTube, tanpa extension)
    "youtube_chat_enabled": False,
    # Social Stream Ninja: YouTube + TikTok + Instagram chat through one door (arti_runtime/ssn_chat.py). The session
    # id is a secret, read by this process at start and kept in memory only. Never together with
    # youtube_chat_enabled (YouTube would arrive twice).
    "ssn_chat_enabled": False,
    "ssn_session_source": "app",   # "app" = the owner's SSN app config here, "file" = ARTI_SSN_SESSION_FILE
    "ssn_ws_base": "wss://io.socialstream.ninja",
    "ssn_status_file": "",
    "youtube_video_id": "",                          # Isi di config_local.json tiap stream (dari URL: youtube.com/watch?v=INI_VIDEO_ID)

    # Konfigurasi OBS Subtitle (in-process WebSocket server + word-level karaoke renderer)
    "subtitle_enabled": True,                         # Master switch: False mematikan in-process subtitle server & semua broadcast
    "subtitle_status_enabled": True,                  # Toggle independen untuk broadcast_status("speaking"/"idle"); diabaikan saat subtitle_enabled=False
    "subtitle_port": 9991,                            # Port WebSocket untuk subtitle.html OBS Browser Source (subtitle.html default 9991 — jangan drift; insiden dua-sesi 3/8 sempat menulis 9992)

    # Mode Pemicu Percakapan Streamer:
    # - "wake_word"     : Panggil Arti dengan mengucapkan kata kunci "arti" / "eh arti"
    # - "push_to_talk"   : Mic MEREKAM CASUAL PASIF ke sejarah stream, tetapi HANYA merespon jika menekan hotkey!
    "trigger_mode": "push_to_talk",
    "ptt_auto_off_sec": 60.0,  # Tanpa speech capture; 0 = nonaktif.
    "hotkey_key": "mouse_x2",                         # Tombol samping mouse (Mouse 5)
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
    # DIGANTI 2026-08-13: Groq mematikan `llama-3.1-8b-instant` pada
    # 16 Agu 2026 (diumumkan 17 Jun). Ini slot TERPANTING di jalur suara —
    # `mic`/`ptt` tidak lewat composer, dan smart routing memilih `fast`
    # untuk kebanyakan kalimat pendek streamer.
    # Pengganti resmi Groq, dan kebetulan LEBIH CEPAT: 1.000 t/s vs 560 t/s.
    # Output lebih mahal ($0,30 vs $0,08 per 1 jt) — dibayar demi tidak bisu.
    # Dipilih juga karena punya TIGA sumber independen (Groq berbayar,
    # OpenRouter :free, Ollama Cloud gratis) — lihat docs/MODEL-REGISTRY.md.
    "groq_model_fast": "openai/gpt-oss-20b",

    # Smart Groq routing per turn (v0.6.2 — restore dari checkpoint 2026-06-07):
    # pilih model by kompleksitas pertanyaan, bukan round-robin buta.
    "smart_groq_routing": True,
    "groq_model_medium": "openai/gpt-oss-20b",        # sapaan panjang / casual
    "groq_model_strong": "openai/gpt-oss-120b",       # pertanyaan "kenapa/jelaskan"
    "groq_model_rare": "openai/gpt-oss-120b",         # pertanyaan panjang & kompleks
    # ADR 119: above the system ceiling + history = abnormally big, so force fast. Ordinary turns now
    # carry the soul and facts (15-30 KB); 10000 sent every turn to fast. 413 retry stays the safety net.
    "groq_prompt_char_soft_cap": 33000,
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
    # 16.0 (riwayat: 7.0 -> 10.0 sore 2026-08-02 -> 12.0 seharian 2026-08-03
    # -> 16.0 malam 2026-08-19, keputusan Bohan). Angka ini dipakai DUA KALI:
    # deadline internal saat mengkonsumsi stream, DAN dasar asyncio.wait_for
    # (+1 detik). Ramalan komentar versi 12.0 ("kalau max sukses nempel lagi
    # di 12, masalahnya di composer yang melambat") TERJADI: tiga sesi malam
    # 17-18 Agu berturut-turut composer butuh 12-16 dtk pada sesi HANGAT
    # (llm p90 15,3 dtk; sesi 23.40: 1 sukses vs 13 timeout, breaker 4x)
    # PADAHAL prompt sudah didiet (sukses yang ada 6-11 dtk). 16,0 menutup
    # p90 terukur; harga ~4 dtk ekstra diterima Bohan ("[recorded remark omitted]"). Kalau max sukses nempel lagi di 16 — itu composer, bukan kapak.
    "cursor_timeout_sec": 16.0,
    # ---- Otak agy persisten / Google AI Pro (26 Agu 2026) ----
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
    # ---- penyedia kedua: Codex (19 Agu 2026) ----
    # Lapis ANTARA composer dan Groq di giliran suara: composer gagal/
    # breaker tutup -> Luna (thread hangat 1,5-3 dtk, probe tercatat di
    # catatan riset internal) -> baru Groq.
    # Default MATI: ToS abu-abu — menyalakan = keputusan sadar di
    # config_local. Kuota mengikuti paket akun Codex operator; kalau sering menolak, cek /status dulu.
    # HARAM untuk kerja latar (analog aturan #2 — kolamnya tetap terbatas).
    "codex_agent_enabled": False,
    # Eksperimen: jadikan Codex/Luna model SUARA UTAMA satu sesi — dicoba
    # SEBELUM composer, composer turun jadi cadangan (lalu Groq). Luna hangat
    # biasanya lebih cepat daripada composer. Butuh codex_agent_enabled=true.
    # JANGAN nyalakan permanen tanpa memeriksa kuota sesudah sesi.
    "codex_primary_voice": False,
    "codex_model": "gpt-5.6-luna",
    # 'low' terverifikasi probe: reasoning_output_tokens=0 — nalar tidak
    # memakan budget/latensi. Persis kebijakan Groq gpt-oss (dan beda dari
    # OpenRouter free yang mengabaikan effort).
    "codex_effort": "low",
    # Effort NAIK khusus kelas jawaban berat (usul Bohan 27 Agu). Terukur
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
    # Tier layanan Codex per giliran ("priority" = Fast; kuota lebih boros).
    # None = tidak dikirim = perilaku lama. Lihat arti_codex_agent.service_tier.
    "codex_service_tier": None,
    "codex_timeout_sec": 8.0,
    "codex_thread_max_turns": 20,
    # ADR 200: append-only voice context on the warm Codex thread (stable once, changed "now" blocks, session
    # lines once, request). Off = today's single-string prompt, byte for byte. mabar stays off until the acceptance
    # replays pass. None limits derive from the window: rollover 75% and ceiling min(rollover + 10%, 90%) of
    # 0.95 x codex_context_window (None = 272000, and model_context_window is then not overridden).
    "voice_append_context": False,
    "codex_context_window": None,
    "voice_context_checkpoint_tokens": 60000,
    "voice_context_rollover_tokens": None,
    "voice_context_hard_ceiling_tokens": None,
    "voice_context_summary_cap": 60,  # summary side calls per session (ADR 200, "Summary budget")
    "codex_bin": r"C:\Users\Laptop\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe",
    # Kosong = dibuat otomatis di TEMP. Luna selalu dijalankan dari folder
    # kosong di luar repo agar repo tidak menjadi workspace/konteks bawaannya.
    # Sandbox read-only + deny-all juga mencegah tulis, network, dan eskalasi;
    # SDK tetap mengizinkan read absolut, jadi instruksi no-tools adalah pagar
    # tambahan, bukan klaim isolasi kriptografis. Untuk nol akses file: Luna OFF.
    # Kalau diisi, folder wajib sudah ada, kosong, dan berada di luar repo.
    "codex_scratch_dir": "",
    # Raw OpenAI Responses API voice arm (measured comparison vs Codex CLI).
    # Default OFF: existing Codex/Cursor/Groq path is unchanged. When on, matching
    # trigger types try this first; None falls back to that path.
    "voice_api_enabled": False,
    "voice_api_trigger_types": ["mic", "ptt", "mic_pasif", "yt_chat", "game"],
    "voice_api_model": "gpt-6-luna",
    "voice_api_effort": "low",
    "voice_api_service_tier": "default",
    "voice_api_session_cap_usd": 0.50,
    "voice_api_session_state_file": "",
    "voice_api_timeout_sec": 20.0,
    # Trigger berharga (video/donation) saat sesi dingin: tunggu pemanasan
    # sampai sekian detik alih-alih jatuh ke Groq 8B — konten tak tergantikan
    # (digest video, terima kasih donatur), tidak ada yang diburu waktu.
    "cursor_warmup_wait_precious_sec": 45.0,
    # Daur ulang sesi BUKAN soal kecepatan: pembengkakan konteks terukur cuma
    # 1,05x/20 turn (docs/CURSOR-SDK-SPIKE.md). Alasannya VARIASI — sesi hangat
    # menjawab pertanyaan mirip nyaris verbatim. Sejak 12 Agu 2026 penggantinya
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
    # Kesehatan provider SEMUA rantai (arti_provider_health, 2026-10-23):
    # probe sekali di awal (paralel, total <= budget), provider mati keras
    # (auth/model_rejected/init_error) dilewati sesi ini, galat sementara ->
    # breaker N gagal = lewati M detik lalu probe ulang di latar. codex tidak
    # pernah dilewati. provider_probe_enabled=False = perilaku lama penuh.
    "provider_probe_enabled": True,
    "provider_probe_budget_sec": 12,
    "provider_breaker_failures": 3,
    "provider_breaker_open_sec": 600,
    # Breaker half-open: setelah sekian detik, coba Cursor lagi. Penting untuk live
    # yang DITINGGAL seharian — tanpa ini, satu gangguan sekejap (3 gagal instan)
    # mematikan Composer untuk sisa hari. 0 = permanen sampai restart (perilaku lama).
    # 300, turun dari 900 (live 21.49, 11 Agu): breaker tutup 15 MENIT
    # berarti 15 menit siaran dijawab llama-3.1-8b — Bohan langsung merasakan
    # ("[recorded remark omitted]"). Biaya terburuk cooldown pendek adalah 3
    # percobaan gagal-cepat tiap 5 menit; itu jauh lebih murah daripada
    # kehilangan composer seperempat jam di depan penonton.
    "cursor_breaker_cooldown_sec": 300,
    "cursor_last_resort_incharacter": True,
    "cursor_api_key": os.environ.get("CURSOR_API_KEY", ""),
    # Sesi Cursor per-role (susunan: Cursor tulang punggung semua otak, chain
    # API gratis jadi fallback). Aktif kalau "cursor" ada di
    # scouter/observer/vision_provider_chain — chain shipped SENGAJA tanpa
    # cursor (repo publik; nyalakan lewat config_local). Verifikasi model/param:
    # scripts/spike_grok_vision.py — grok-4.5 punya effort low/medium/high,
    # composer-2.5 TIDAK punya effort (jangan diisi), default variant = FAST.
    # Scouter pakai composer non-fast (grok-high terlalu mahal untuk panggilan
    # per menit); observer & lookup ikut composer.
    "cursor_scout_model": "composer-2.5",
    "cursor_scout_effort": "",
    # KETAHUAN crosscheck: default resolve_role_model sudah
    # composer, tapi DUA key CONFIG ini masih menimpanya ke grok diam-diam.
    "cursor_observer_model": "composer-2.5",
    "cursor_observer_effort": "",
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
    # Giliran inisiatif/curious = Arti membuka topik sendiri. Itu celetukan,
    # bukan penjelasan — dulu 1 kalimat, lalu naik ke 2 karena Arti jadi
    # terlalu diam. Ini TARGET di prompt, bukan gunting.
    "arti_reply_inisiatif_max_sentences": 2,
    # Batas AMAN pemotong (2026-10-23, Bohan: "[recorded remark omitted]"). Jumlah kalimat rencana cuma target di prompt; pemotong hanya
    # bertindak di atas max(target x 2, hard_cap_sentences) kalimat atau
    # max(hard_cap_chars, target_chars x 1,5) karakter — dan selalu di batas
    # kalimat. Budget max_tokens tiap rencana ikut menampung batas ini.
    "arti_reply_hard_cap_sentences": 6,
    "arti_reply_hard_cap_chars": 900,
    # Plafon batas aman karakter (rant 8 kalimat); kalimat tunggal tanpa
    # titik tetap utuh sampai 2x hard_cap_chars, di atas itu direm ke ~900.
    "arti_reply_hard_cap_chars_max": 1200,
    # Ucapan Bohan dinilai dengan ladder yang sama seperti chat YT (pendek
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
    "voice_queue_preserve_humans": False,  # ADR 164: mabar retains every admitted owner/chat event.
    "attention_desk_enabled": False,  # ADR 172: mabar --desk opts into game-first selection.
    "asr_address_names": [],  # Empty uses the canonical word-bounded ASR name aliases.
    "live7_own_play_enabled": False,  # ADR 164: enabled by the mabar session, no game policy.
    "live7_reactive_play_enabled": False,
    "live7_long_action_sec": 20,
    "live7_intent_repeat_sec": 600,
    "live7_play_line_gap_sec": 45,
    "stardew_initiative_game_first": False,
    "stardew_own_topic_gap_sec": 180,
    "own_play_min_gap_sec": 0,  # 0 = no extra rate bound; mabar --own-play sets 25.
    "yt_chat_queue_max": 2,
    "yt_chat_queue_ttl_sec": 60.0,
    # Keep viewer chat queued through her speech and the owner's talk (mabar sets these; live default off).
    "live_reply_chat_keep": False,
    "live_reply_chat_max_age_sec": 900.0,
    "yt_chat_cooldown_sec": 10.0,
    # Anti-spam per PENONTON di semua mode: chat biasa memakai cooldown ini;
    # panggilan langsung/reply @Arti memakai batas sendiri yang lebih singkat.
    "yt_viewer_cooldown_sec": 45.0,
    "yt_addressed_min_gap_sec": 10.0,
    # Penonton BARU (nama yang belum pernah muncul sesi ini) disapa dari pesan
    # pertamanya TANPA harus menyebut "arti" — momen selamat-datang itu justru
    # yang paling berharga. Tetap dipagari cooldown global & per-viewer.
    "yt_greet_new_viewers": True,
    # Chat santai (Bohan 2026-08-20: "[recorded remark omitted]"):
    # pesan manusiawi TANPA wake word ikut memicu jawaban, lewat keran global
    # sendiri yang lebih longgar (gap di bawah) supaya Arti tidak berubah jadi
    # mesin penjawab semua chat — wake word tetap jalur prioritas (gap 20 dtk
    # lama), dan cooldown per-viewer 45 dtk tetap berlaku untuk chat biasa.
    # Pesan yang tertahan keran TETAP masuk history, jadi jawaban berikutnya
    # masih melihatnya sebagai konteks.
    "yt_chat_santai_enabled": True,
    "yt_chat_santai_gap_sec": 60.0,
    # Berapa poll kosong berturut-turut sebelum token di-refresh (tanpa
    # backoff). Dulu 3 (~20 dtk) -> refresh tiap ~23 dtk saat chat sepi;
    # 12 (~2 menit) cukup sebagai jaring pengaman token macet.
    "youtube_chat_reinit_empty_polls": 12,
    # Bot layanan chat — pesan mereka boleh tercatat di history (konteks
    # leaderboard dll), tapi TIDAK PERNAH mentrigger jawaban. Live 11,5 jam
    # 2026-08-01: @Streamlabs posting leaderboard dan sempat diantri jawab.
    "yt_bot_viewers": ["Streamlabs", "Nightbot", "StreamElements", "Moobot", "Fossabot"],
    "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", ""),
    # OpenRouter model slugs — lihat docs/OPENROUTER_MODELS.md
    # Diperbarui 2026-07-31: `poolside/laguna-xs.2:free`, `poolside/laguna-m.1:free`,
    # dan `owl-alpha` semuanya 404 ("[recorded remark omitted]") — slug poolside di-rename
    # jadi `laguna-xs-2.1`, `owl-alpha` hilang total.
    # PENTING: poolside = model reasoning. Terverifikasi probe — pada max_tokens 110/350
    # ia mengembalikan content KOSONG (finish=length, CoT menghabiskan budget); baru keluar
    # jawaban di ~600 token. Jalur live pakai max_tokens 110-320 (_TOKENS_BY_SENT di
    # arti_reply_policy.py:55), jadi poolside HARAM di sini — itu bug "Jawaban AI kosong"
    # yang sama seperti qwen3.6 (fix dd88d9e). Pakai model non-reasoning yang finish=stop.
    # DITUKAR 2026-08-13 (crosscheck pra-siaran, n=6 prompt identik per model).
    # nemotron-3-super BUKAN cuma lambat — dia membocorkan CoT Inggris ke penonton,
    # dan `clean_ai_reply` TIDAK menangkapnya (kalimatnya lolos gerbang
    # >=4 kata Inggris DAN rasio >=0,6). Satu bocoran bahkan membacakan system
    # prompt: "[recorded remark omitted]".
    #
    #   model                              bersih  bocor-CoT  latensi median
    #   nvidia/nemotron-3-super-120b-a12b     4/6      2/6       16.447 ms
    #   nvidia/nemotron-3.5-lightning         0/6      6/6        2.732 ms  <- terbaru, TERPARAH
    #   google/gemma-4-26b-a4b-it             6/6      0/6        2.799 ms  <- dipilih
    #
    # Ini jaring pengaman giliran SUARA (mic/ptt tidak lewat composer), jadi
    # kualitasnya menentukan seberapa buruk hari buruk. Sesudah Groq mematikan
    # llama-3.1-8b-instant (16 Agu 2026) jalur ini dipakai JAUH lebih sering.
    # nemotron-3-super tidak dibuang, cuma turun jadi cadangan terakhir.
    "openrouter_live_model": "google/gemma-4-26b-a4b-it:free",
    "openrouter_live_last_resort": "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter_live_fast_only": True,
    "openrouter_live_fallback_enabled": True,
    "openrouter_live_timeout_sec": 45,
    # Scouter/summarizer pakai max_tokens 350 (scouter_max_tokens) — masih terlalu
    # ketat untuk poolside, jadi non-reasoning juga.
    "openrouter_summarizer_model": "nvidia/nemotron-3-super-120b-a12b:free",
    # nano-30b diganti lightning 17 Agu 2026 — nano menghalusinasi fakta ke
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
    "vault_summary_external_enabled": True,  # OFF: local artifacts only; no shutdown summary/context calls
    "vault_rag_enabled": True,
    "vault_rag_live_enabled": True,
    "vault_rag_live_timeout_sec": 8,
    "vault_rag_lite_enabled": True,
    "vault_rag_db_path": "data/vault_rag.db",
    "lmstudio_embedding_base_url": "http://127.0.0.1:1234/v1",
    "lmstudio_embedding_fallback_base_url": "http://127.0.0.1:1234/v1",
    "lmstudio_embedding_model": "text-embedding-mxbai-embed-large-v1",
    "lmstudio_embedding_timeout_sec": 8,
    "lmstudio_embedding_connect_timeout_sec": 2.0,
    "lmstudio_embedding_breaker_sec": 60.0,
    "lmstudio_embedding_query_prefix": "",
    "lmstudio_embedding_doc_prefix": "",
    "vault_rag_rerank_enabled": False,
    "vault_rag_rerank_base_url": "",
    "vault_rag_rerank_top_n": 20,
    "vault_rag_rerank_connect_timeout_sec": 2.0,
    "vault_rag_rerank_timeout_sec": 1.5,
    "vault_rag_rerank_breaker_sec": 60.0,
    "vault_rag_rerank_min_score": None,
    # 5 -> 2 (diet bahan, paket kohesi 17 Agu): top-5 menyuntik 1,8-2,3 KB
    # per giliran dan ikut mendorong composer menabrak kapak 12 dtk (malam
    # 17 Agu: sukses composer tinggal 2x semalam). Dua hit terbaik cukup;
    # kualitas fokus > banjir bahan.
    "vault_rag_top_k": 2,
    # 2400: header instruksi RAG tumbuh 76 -> 172 char (aturan konflik tanggal), dan itu
    # overhead tetap. Tanpa kenaikan ini hit ke-5 tergusur. Lihat arti_vault_rag.py.
    "vault_rag_max_context_chars": 2400,
    "vault_rag_reindex_on_shutdown": True,
    # 0 = tunggu reindex TUNTAS saat shutdown (Bohan 2026-08-02: jangan ada kerja
    # diam-diam saat "[recorded remark omitted]" muncul). >0 = batas detik (perilaku lama).
    "vault_rag_reindex_shutdown_timeout_sec": 0,
    # Catch-up saat start: menyembuhkan reindex shutdown yang terpotong (thread
    # daemon mati bersama proses, DB bisa berhenti di tengah).
    "vault_rag_reindex_on_startup": True,
    # Catch-up observer saat start (Bohan 2026-08-10: "[recorded remark omitted]"): sesi yang mati tanpa shutdown bersih
    # (force close/crash/OOM) meninggalkan transcript tanpa beats — ekornya
    # dirangkum di startup berikutnya, INKREMENTAL (segmen yang sudah punya
    # beat tidak diulang; learnings tidak dobel). Sesi berjalan tidak disentuh.
    "observer_catchup_on_startup": True,
    "observer_catchup_delay_sec": 90.0,     # beri startup ruang napas dulu
    "observer_catchup_max_days": 7,
    "observer_catchup_margin_sec": 120.0,
    # Jeda antar segmen saat catch-up — backlog perdana BESAR (audit
    # 2026-08-10: 6 hari ekor sesi malam tak pernah dirangkum, 2026-08-04
    # bahkan nol beats) dan catch-up jalan BERSAMAAN dengan live; tanpa rem
    # dia berebut provider dengan turn Arti. Inkremental + resumable, jadi
    # pelan itu aman: terpotong pun lanjut di startup berikutnya.
    "observer_catchup_pause_sec": 3.0,
    # Model rangkuman catch-up saat LIVE = chain GRATIS tanpa cursor.
    # REGRESI TERUKUR live 21.49 (11 Agu): catch-up 17 segmen (12-24
    # dtk/segmen) memakai composer BERSAMAAN dengan giliran suara ->
    # giliran voice timeout 3x -> breaker Cursor tutup 900 dtk -> SELURUH
    # sesi dijawab llama-3.1-8b ("[recorded remark omitted]"). composer
    # sukses NOL kali. Kerja latar belakang tidak boleh merebut jalur yang
    # dipakai penonton. Chain gratis = tetap hemat (bahkan lebih), nol
    # kontensi. Catch-up dengan composer tetap tersedia SAAT BRIDGE MATI
    # lewat scripts/rangkum_susulan.py (semalam sukses 62 beat).
    # "github" DIBUANG 13 Agu 2026 — GitHub Models pensiun total 30 Juli 2026
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
    # Startup soul (8.4 KB) and the turn's facts (a51: up to 13 KB) both reach the prompt now (ADR 119).
    # This shared character cap also reaches Groq fallback, not a TPM guarantee:
    # history/RAG/web add more input, and Groq's existing 413 retry trims again.
    "llm_system_prompt_max_chars": 30000,
    "soul_prompt_enabled": True,

    # ARTI self + motivation layer (owner 2026-10-22, design
    # ARTI_SELF_MOTIVATION_DIARY.md §9). arti-state/ = self.jsonl + wants.jsonl,
    # owner-readable. Kill switch off = no [DIRIKU] block, no wants in RAG, no
    # post-live state updates. Discord stories are a separate switch, default off.
    "arti_state_enabled": True,
    "arti_state_dir": "arti-state",
    "discord_stories_enabled": False,

    # Gerakan dialog giliran curious (16 Agu 2026) — PARAMETER, bukan hardcode
    # (permintaan Bohan: "[recorded remark omitted]"). Timpa dari
    # config_local.json tanpa menyentuh kode; restart bridge untuk memuat ulang.
    # None = pakai DEFAULT_GERAKAN_DIALOG / DEFAULT_LARANGAN di arti_curious.
    # [] pada gerakan = fitur mati; "" pada larangan = tanpa larangan.
    # Aturan gaya yang paling sering di-tune tetap di ARTI_SOUL.md (live tanpa
    # restart) — dua kunci ini untuk bentuk giliran proaktif saja.
    # Solo stream 2026-11-15: spoken rules for a live play stream (arti_runtime/live_rules.py); mabar on.
    "live_play_rules_enabled": False,
    "curious_gerakan_dialog": None,
    "curious_larangan": None,

    # Fase 1 — transcript JSONL + vault slim (v0.5.2)
    "stream_session_id": "",
    "transcript_dir": "transcripts",
    # Berapa log SUBSTANSIAL yang tinggal di session_logs/ (sisanya pindah ke
    # archive/v0.4/session_logs/). Naik 5 -> 15 (12 Agu 2026): dengan 5, log
    # siaran semalam sudah tergeser sebelum sempat dibaca.
    "session_log_keep_n": 15,
    # Di bawah ukuran ini = bukan sesi sungguhan (bridge gagal start, harness),
    # tidak ikut memakan jatah keep_n. Lihat session_transcript.rotate_session_logs.
    "session_log_min_bytes": 20000,
    "transcript_flush_fsync": True,
    "transcript_flush_interval_sec": 1.0,  # capped at 1 s, including an idle tail
    "transcript_flush_lines": 32,

    # Konfigurasi Supertone 3 TTS (dual-engine: master switch + parameter sintesis lokal)
    "tts_engine": "supertone",                        # "supertone" | "edge_tts" — master engine switch
    # Sensor komedi keluaran: swap token tepat sebelum TTS, subtitle, history,
    # dan chat game. Default OFF sesuai pagar fitur baru; mesin uji menyalakan
    # lewat config_local. Tidak melakukan request/audio tambahan.
    "speech_censor_enabled": False,
    "speech_censor_replacement": "sensor",
    "speech_censor_words": list(arti_speech_censor.DEFAULT_BLOCKED_WORDS),
    "tts_preprocess_numbers": True,                   # Jalankan konversi angka→kata Indonesia sebelum sintesis
    "supertonic_voice": "F1",                         # Voice style: F1-F5 / M1-M5 (F1 disarankan)
    "supertonic_speed": 1.1,                          # tuned for live; 1.3 was too fast
    "supertonic_lang": "id",                          # Kode bahasa Supertone
    "supertonic_total_steps": 10,                     # Max quality [5–12] — F1 live (10 = stabil + cepat)
    # Jalankan Supertonic di GPU (v0.6.3). Terukur 2026-07-31 di GPU uji:
    # CPU p50 29,4 detik/kalimat -> CUDA p50 2,0 detik. ~15x.
    # Default False supaya repo publik tidak mencoba CUDA di mesin tanpa runtime-nya;
    # nyalakan di config_local.json. Kalau CUDA gagal, supertonic/loader.py jatuh ke CPU
    # sendiri (terverifikasi) — Arti tetap bersuara, cuma lambat.
    # Butuh di venv312: onnxruntime-gpu + paket nvidia-* (lihat requirements-supertone.txt).
    "supertonic_use_cuda": False,
    # Polesan suara (16 Agu 2026) — resep hasil uji dengar Bohan atas 15+
    # varian (dump/uji_pitch, berkas pemenang: E3_range_x14_plus2):
    # range intonasi dilebarkan 1,4x (PSOLA, F0' = median + k*(F0-median)) +
    # pitch & warna naik 2 semitone (sinc resample; kompensasi durasi di
    # speed sintesis — net tetap supertonic_speed). Latar: suara F1 terdengar
    # datar; Supertonic tidak punya kenop pitch, dan F1/speed/steps SUDAH
    # terkunci oleh tes.
    # Netralkan (0 / 1.0) dari config_local untuk kembali ke suara lama.
    # Butuh praat-parselmouth (requirements.txt); tanpa itu otomatis mati.
    "supertonic_pitch_semitone": 2.0,
    "supertonic_range_factor": 1.4,
    "supertonic_prewarm_on_startup": True,            # Load model saat startup, hindari timeout jawaban pertama
    "supertonic_timeout_sec": 45.0,                   # Sintesis per-utterance (was 20s — sering timeout)
    "supertonic_revival_probe_sec": 600.0,            # Retry setelah gagal, bukan setiap turn

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
        "google_gemini_lite",  # 160/167 = 95% di log 9 jam 27 Agu
        "openrouter",          # 6/9; incumbent cadangan paling terukur
        "ollama",              # 3/3 di log 27 Agu
        "google_gemma",        # thinking OFF 28 Agu: probe JSON 3,7 dtk
        "cloudflare",          # thinking OFF 28 Agu: probe JSON 2,0 dtk
        "zai",                 # 0/3 di log 27 Agu
        "nvidia",              # 0/3, ReadTimeout 60 dtk penuh tiap kali
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
    # GERBANG LAYAR-DIAM: Arti menonton dirinya sendiri -> frame nyaris identik
    # tiap giliran, tapi vision tetap dipanggil dan [LAYAR:] tetap disuntik
    # (banyak jawaban jadi menyebut layar yang sama).
    # sel_berubah_min_persen: metrik UTAMA (persen sel 32x18 yang berubah
    # tajam) — menangkap perubahan LOKAL. Ukur nyata di layar uji:
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
    # Rem kuota Gemini free tier: tanpa ingatan kuota, google_gemini di posisi 1
    # rantai bisa terus ditembak walau sudah dibalas HTTP 429.
    # Google tidak mempublikasikan angka free tier lagi; pagar default di
    # bawah angka historis flash-lite (15 RPM / 250k TPM). Berlaku per model
    # untuk SEMUA pemanggil Gemini (scouter, vision, observer, video) lewat
    # pintu arti_gemini_vision. Rincian: arti_gemini_budget.py.
    "gemini_rpm_budget": 12,
    "gemini_tpm_budget": 200000,
    "gemini_429_cooldown_sec": 60.0,
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
    # False sejak 16 Agu (lapis B "[recorded remark omitted]"): giliran proaktif justru
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
    "initiative_backoff_base_sec": 180.0,  # <=0 = flat tiap quiet_sec (setelan Bohan)
    # Sejauh apa ke belakang omongan streamer masih dianggap "[recorded remark omitted]" —
    # membuat giliran inisiatif jadi NYAMBUNG, bukan buka topik baru.
    # 45 dtk: satu giliran Arti sendiri makan 25-30 dtk (live 14 Agu), jadi
    # ambang lebih pendek bikin dia lupa Bohan barusan ngomong tepat setelah
    # dia selesai menjawab. Bandingkan initiative_streamer_gap_sec (5.0) yang
    # cuma pagar anti-MOTONG — itu soal kapan boleh bicara, ini soal APA.
    "initiative_nyambung_sec": 45.0,
    "initiative_backoff_max_sec": 720.0,
    # Semua provider gagal saat turn curious (Groq 429 + jalur Cursor tutup):
    # rehat sekian detik, JANGAN nembak lagi tiap cadence: tiap percobaan =
    # rentetan request 429 baru yang memperparah habisnya kuota.
    "initiative_provider_fail_backoff_sec": 300.0,
    # Detektor kehidupan (revisi spek Bohan 2026-08-03, ganti "[recorded remark omitted]"): tanpa SATU pun tanda manusia (chat viewer /
    # suara streamer / jumlah penonton NAIK) selama sekian detik -> SEMUA
    # jalur proaktif tidur; bangun otomatis saat ada tanda kehidupan.
    # Kasusnya: ~1 jam nol viewer, Arti monolog terus. Spek final Bohan:
    # "[recorded remark omitted]". <=0 = perilaku lama.
    "initiative_dormant_after_idle_sec": 300.0,
    # RENUNGAN (Bohan 2026-08-20: "[recorded remark omitted]"): busur
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
    # Minecraft — Arti sebagai player di server lokal (plan 2026-08-04,
    # Phase 0 GO, Phase 1 = integrasi bridge ini). Nama per-mesin diisi di
    # config_local (bot: arti_berarti, streamer: examplemcplayer). Shipped OFF;
    # nyalakan minecraft_enabled di config_local, lalu 'mc on' / [MC: join].
    "minecraft_enabled": False,
    # Live7 ask-for-help relay: opt-in; no chat posting or extra inference path.
    "live7_help_enabled": False,
    "live7_help_dir": "",
    "live7_help_min_gap_sec": 900,
    "live7_help_max_per_hour": 3,
    "live7_help_speak_enabled": True,
    "live7_help_window_sec": 180,
    "live7_help_viewer_hints": 5,
    "live7_moments_enabled": False,
    "live7_moments_min_gap_sec": 90,
    "live7_status_facts_enabled": True,
    # ADR 90: seconds a bed wait lasts before it may become speech; until then it is a status-block fact.
    "live7_host_wait_speech_after_sec": 60.0,
    # ADR 90: every non-movement skill row as a persona event. Off for live until the owner hears it on the rig.
    "live7_activity_events_enabled": False,
    # Real-path TEXT-ONLY test mode (owner 2026-10-28): no TTS/audio, no mic/ASR,
    # no VTS/OBS/YouTube/Discord/chat; replies + seen context go to a JSONL
    # transcript (arti_bridge_testmode.py). Default OFF = normal streams unchanged.
    # Normally enabled only by env ARTI_BRIDGE_TEST_CONFIG=<separate test config>,
    # which then replaces config_local.json entirely.
    "bridge_test_mode": False,
    "bridge_test_dir": "",
    "minecraft_host": "127.0.0.1",
    "minecraft_port": 25565,
    "minecraft_bot_name": "Arti",
    "minecraft_streamer_name": "Bohan",
    "minecraft_node_path": "node",
    "minecraft_bot_script": "mc-bot/bot.js",
    "minecraft_status_interval_sec": 10,
    "minecraft_context_ttl_sec": 120.0,
    "minecraft_reaction_cooldown_sec": 60.0,
    # Ritual cek tas: tiap sekian detik (saat aman) dia berhenti, tasnya
    # dibuka di kamera (invsee) beberapa detik, lalu jalan lagi. Permintaan
    # Cek isi tas berkala. 0 = mati.
    "minecraft_bag_check_sec": 300.0,
    # Mode kamera: default "spectate" + F5 otomatis (di bawah). Orbit
    # tetap tersedia lewat config bagi yang mau drone.
    "minecraft_camera_mode": "spectate",
    # F5 otomatis di window klien kamera: ditekan ulang tiap kamera dikunci
    # ulang (join/mati/pindah dimensi) — karena saat itulah perspektif
    # ter-reset ke first-person. 0 = mati. 1 = third-person belakang,
    # 2 = third-person depan.
    "minecraft_kamera_f5_tekan": 1,
    # ROTASI SUDUT KAMERA (Bohan 11 Agu 22.37: "[recorded remark omitted]"). Sesudah orbit-drone ditolak ("[recorded remark omitted]"), kamera
    # jadi third-person statis: benar tapi monoton. Ini menekan F5 berkala
    # supaya perspektifnya berganti belakang <-> depan (depan memperlihatkan
    # wajah/skin Arti) — perubahan sesekali seperti pemain sungguhan, BUKAN
    # kamera yang bergerak terus. 0 = matikan.
    "minecraft_kamera_rotasi_sec": 240.0,
    # Petunjuk memilih window kalau ada LEBIH DARI SATU window Minecraft
    # (mis. Bohan ikut main): potongan judul window instance kamera.
    "minecraft_kamera_window_hint": "Minecraft",
    # TAKDIR: misi kecil terukur yang selalu satu aktif (desain final 2026-08-10;
    # lengkap di memori arti-takdir-desain). Selesai diumumkan SISTEM.
    "minecraft_takdir_enabled": True,
    # Jeda antar takdir sesudah satu selesai/diumumkan.
    "minecraft_takdir_jeda_sec": 120.0,
    # Ritual pembukaan: begitu join, dia jalan-jalan dulu segini detik TANPA
    # takdir, lalu dipancing bertanya "hari ini mau ngapain?" — baru mesin
    # takdir menyala (permintaan Bohan: "[recorded remark omitted]").
    "minecraft_pembukaan_sec": 90.0,
    "minecraft_orbit_radius": 5.5,
    "minecraft_orbit_tinggi": 2.5,
    "minecraft_orbit_period_sec": 75.0,
    # 0.2 = 5 teleport/dtk. Makin kecil makin halus tapi makin cerewet ke
    # server; kehalusan visual dinilai mata Bohan, angka ini tinggal digeser.
    "minecraft_orbit_tick_sec": 0.2,
    "minecraft_max_bot_respawns": 5,
    # Cap heap V8 proses bot (MB). Akar OOM 2026-08-10: badai alokasi
    # pathfinder saat goal tak terjangkau — di limit 4GB GC keteteran dan
    # bot mati; di cap kecil GC rajin dan stabil. Watchdog bot.js memasang
    # ambangnya sebagai persentase dari limit nyata (v8.getHeapStatistics).
    # 2048, naik dari 1200 (live 19.12): sesi nyata merayap ke 1147 MB dalam
    # 16 menit (dunia + aktivitas sah, bukan badai — pagar badai bekerja) dan
    # mentok cap; GC thrashing di dekat limit membuat patroli watchdog
    # kelaparan sebelum sempat restart terkendali. 2048 = ruang napas cukup +
    # tier KRITIS (85% = 1741) dapat jendela tembak sebelum thrash dimulai.
    "minecraft_bot_heap_mb": 2048,
    # Umur maksimal reaksi game yang menunggu di antrean. Bohan 2026-08-10:
    # "[recorded remark omitted]" —
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
    # begitu saja (Bohan 2026-08-06: "[recorded remark omitted]"). 8 = 128 blok, sebanding vanilla. Turunkan kalau OBS berat.
    "minecraft_pov_view_distance": 8,
    # Sudut pandang POV, ala tombol F5 (Bohan 2026-08-06: "[recorded remark omitted]").
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
    # LLM: tanpa ini dia bisa mati berkali-kali sambil terus jalan ditembaki
    # skeleton. 0 = matikan kabur otomatis (tag [MC: kabur] tetap).
    "minecraft_flee_hp": 10,
    # Kamera penonton: username klien Minecraft terpisah yang men-spectate Arti.
    # "" = tidak ada kamera, fitur mati total. Renderer web jadi cadangan.
    "minecraft_spectator_name": "",
    # Kunci ulang berkala sebagai jaring, untuk sebab yang tidak terdeteksi
    # event. 0 = mati (default): tiap perintah spectate berpotensi menyentak
    # kamera di depan penonton, jadi jangan dinyalakan tanpa alasan.
# Detak jantung kamera. Kunci ulang lewat event cuma menangkap respawn dan
    # pindah dimensi; kamera bisa lepas karena sebab lain (gamemode diubah,
    # kamera relog, kamera mati) dan dulu tidak ada yang mengembalikannya —
    # untuk siaran AFK seharian itu berarti layar mati sampai Bohan sadar.
    # 30 dtk: murah (2 perintah RCON) dan tidak terlihat penonton.
    "minecraft_spectator_heartbeat_sec": 30.0,
    # Redam goyangan kamera. Bohan 2026-08-06: "[recorded remark omitted]".
    # Rotasi kamera TIDAK diinterpolasi sama sekali oleh renderernya, jadi tiap
    # belokan pathfinder jadi sentakan. 0 = mentah seperti dulu, makin tinggi
    # makin halus tapi makin telat mengikuti (0,6 ~ 100 ms jeda total).
    "minecraft_pov_smooth": 0.6,
    # Saat main game Arti jadi KOMENTATOR (spek Bohan 2026-08-04 "[recorded remark omitted]"): jeda komentar proaktif lebih rapat dari initiative_quiet_sec,
    # dan aturan "[recorded remark omitted]" TIDAK berlaku selama dia in-game.
    "minecraft_narration_gap_sec": 20.0,
    # Stardew TIDAK mewarisi tempo Minecraft (REVIEW_BRAIN F3, default
    # konservatif sampai owner memutuskan): duet_game Stardew = jeda inisiatif
    # biasa (None = ikut initiative_quiet_sec) + dormansi 5 menit seperti duet.
    # host_game Stardew = tempo host_chat. Lihat arti_session_mode.mode_policy.
    "stardew_initiative_gap_sec": None,
    "stardew_dormancy_enabled": True,
    # Bahan inisiatif dari jurnal Live7 Arti sendiri (plan 2026-10-22,
    # arti_stardew_initiative). OFF sampai ada saksi live. Rasio = porsi giliran
    # inisiatif yang dapat bahan game (0.5 = game, lain, game, ...).
    "stardew_initiative_material_enabled": False,
    "stardew_initiative_game_ratio": 0.5,
    # Refleks instan: bunyi pendek dari cache SEBELUM LLM sempat berpikir
    # (live 2026-08-05: jarak dipukul -> bunyi minimal ~6 dtk = terdengar
    # seperti laporan, bukan reaksi). Butuh cache sekali bikin:
    # scripts/build_reflex_cache.py
    "reflex_enabled": True,
    "reflex_min_gap_sec": 3.0,
    # MODE SESI (spek Bohan 2026-08-04): 4 kombinasi = (Bohan hadir/AFK) x
    # (main game/tidak). "[recorded remark omitted]" = Bohan AFK, Arti pegang siaran — di situ
    # aturan "[recorded remark omitted]" TIDAK berlaku. Lihat arti_session_mode.py.
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
        # Kayu SEMUA jenis (Bohan 2026-08-09 22:57, hutan spruce: "[recorded remark omitted]" — padahal daftar ini dulu cuma kenal oak,
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
    # MODE TAMU (16 Agu 2026) — mabar di server/dunia ORANG (e4mc dst.).
    # Permintaan Bohan: "[recorded remark omitted]".
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
    # Misi bawaan saat dia masuk tanpa misi dari Bohan. Mengandung "survive"
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
        # Busur diamond (2026-08-08): bekal jangka panjang menuju "[recorded remark omitted]" versi jauh. Iron pickaxe sudah ada di atas.
        "diamond_pickaxe", "diamond_sword", "flint_and_steel",
        # Busur & panah (2026-08-09): string dari laba-laba, feather dari ayam.
        "bow", "arrow",
        # Ember (2026-08-10): fondasi rute portal tanpa diamond — lava pool +
        # water bucket (cor obsidian di tempat). Verb cor-nya menyusul; ember
        # sudah boleh dirakit dari sekarang (3 iron ingot).
        "bucket",
        # Bed = malam bisa di-SKIP, bukan cuma ditunggu (spek Bohan
        # 2026-08-08). Wool-nya dari `serang sheep` (drop vanilla), jadi
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
    # Isi tas Arti bisa tampil di layar kamera lewat GUI Minecraft
    # asli (plugin invsee + sudo + TutupTas). Butuh kamera hidup.
    # Balasan chat in-game Bohan: "semua" | "wake" (harus menyebut arti) |
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
    "video_rate_limit_sec": 300.0,      # per viewer (chat/streamlabs; owner/saweria exempt)
    "video_queue_max": 3,
    "video_gemini_model": "gemini-3.5-flash-lite",  # second try: video_gemini_second_model (3.1)
    # Bench 2026-11-12: 3.5 median 3.8s / p90 4.8s on shorts; 20s live first-try timed out.
    "video_gemini_timeout_sec": 45.0,
    "video_gemini_total_timeout_sec": 90.0,  # first + fallback bound (3.1 30-min was 44.5s)
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
    # Alerts vs her speech (2026-11-12): OFF = today's hard cut. ON = finish
    # the current sentence, drop the rest of that reply, then the alert.
    "alerts_yield_after_sentence": False,
    "alerts_yield_max_sec": 8.0,
    "web_lookup_enabled": False,
    "web_lookup_model_decides": False,
    "web_lookup_provider_chain": ["groq_compound", "cursor"],
    "web_lookup_codex_enabled": False,
    "web_lookup_codex_model": "gpt-6-luna",
    "web_lookup_codex_effort": "low",
    "web_lookup_codex_timeout_sec": 9.0,
    "web_lookup_groq_model": "groq/compound-mini",  # compound besar = 413 di free tier
    "web_lookup_timeout_sec": 10.0,
    "web_lookup_turn_budget_sec": 12.0,
    "web_lookup_max_tokens": 220,
    "web_lookup_max_chars": 500,
    "cursor_lookup_model": "composer-2.5",  # revisi 2026-08-10: semua composer
    "cursor_lookup_effort": "",
    "cursor_lookup_timeout_sec": 30.0,
    "cursor_lookup_allow_tools": True,  # SATU-SATUNYA role yang boleh tool (web search)
    "summarizer_provider": "openrouter",
    "scouter_enabled": True,
    # JANGAN taruh "cursor" di depan chain ini. Scouter jalan tiap 90 dtk dan
    # panggilan composer-nya terukur 15-27 dtk (live 22.10, 11 Agu) — sementara
    # giliran SUARA cuma punya belasan detik (cursor_timeout_sec). Akibatnya voice
    # timeout tepat saat scouter memakai cursor, 3x = breaker tutup, dan
    # seluruh siaran jatuh ke llama-8b ("[recorded remark omitted]").
    # Aturannya sama dengan catch-up observer: KERJA LATAR TIDAK BOLEH
    # MEREBUT JALUR YANG DIPAKAI PENONTON. Composer disisakan untuk voice.
    # URUTAN DITINJAU 28 Agu 2026 dari log 9 jam 27 Agu. OpenRouter naik
    # berdasarkan 201/205; Cloudflare dipertahankan sesudah akar 0/205-nya
    # diperbaiki (thinking Gemma menghabiskan budget output). NVIDIA tetap
    # terakhir: ReadTimeout penuhnya pernah membakar 45-60 dtk per percobaan.
    "scouter_provider_chain": [
        "google_gemini",   # 208 OK; 429 gratis/cepat ditahan budget
        "openrouter",      # 201/205 = 98% di log 9 jam 27 Agu
        "cloudflare",      # thinking OFF 28 Agu: probe JSON 2,1 dtk
        "ollama",          # 4/4 di log 27 Agu
        "zai",             # 0/4 di log 27 Agu
        "nvidia",          # 0/4, ReadTimeout 45 dtk penuh tiap kali
    ],
    # Brain batch B (BRAIN_AUDIT §2.1): scouter = mood + hook pass every ~5 min.
    # Frequent triggers cost many tokens per hour and starved the Gemini free
    # tier. 0 triggers = timer only.
    "scouter_every_n_triggers": 0,
    "scouter_interval_sec": 300,
    "scouter_min_gap_sec": 120,
    # important_facts back ON (REVIEW_BRAIN F8): with them off the learnings
    # file stopped growing (observer learnings=0 in every audited run). The
    # facts step now runs only every scouter_interval_sec (300 s), so it is
    # cheap. False = slim prompt without the facts step.
    "scouter_facts_enabled": True,
    "scouter_auto_vision_sec": 60,
    "scouter_max_tokens": 350,
    "scouter_temperature": 0.2,
    "scouter_timeout_sec": 45,
    "scouter_nvidia_model": "",
    "scouter_cloudflare_model": "@cf/google/gemma-4-26b-a4b-it",
    # Probe 17 Agu 2026 (transkrip jebakan, budget asli 350, reasoning off):
    # super-120b 2/2 fakta tahan-lama; lightning 1,0-3,3 dtk & konservatif
    # (0 fakta = aman); gemma 2/2. nano-30b DIBUANG: dia MENGHALUSINASI —
    # menulis fakta kanon dari prompt ("Arti fakta contoh...") dan contoh
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
    # Kill switch: observer beats join vault RAG per reply (same embed, one rerank).
    "observer_rag_live_enabled": True,
    "observer_embed_all_beats": True,
    "observer_promote_min_confidence": 0.6,
    "observer_shutdown_blocking": True,
    "telemetry_enabled": True,
    "telemetry_dir": "data/telemetry",
    "telemetry_log_each_call": True,
    "telemetry_cost_table_path": "data/api_cost_table.json",
    # Tanggal fakta contoh kamu — isi di config_local.json (lihat config_local.json.example)
    "arti_debut_date": "",
    "arti_debut_label": "",
    "arti_archive_from": "",
    "timeline_guard_enabled": True,
    "vault_rag_history_min_score": 0.32,
    # Telinga Arti (desktop loopback): default speaker -> chunk -> whisper
    # (GROQ_API_KEY_DESKTOP di .env, terpisah dari mic; tanpa kunci = whisper
    # lokal) -> ring RAM. Anti-echo: chunk yang tumpang tindih TTS Arti dibuang.
    # Kill switch OFF shipped; nyalakan di config_local.
    "desktop_audio_enabled": False,
    "desktop_audio_device": "",          # kosong = ikut default speaker Windows
    "desktop_audio_local_fallback_enabled": False,  # Background ASR must not compete with mic/TTS on CPU.
    "desktop_audio_chunk_sec": 5.0,      # 5 dtk: kalimat utuh + hemat request
    "desktop_ear_gate_on_media": True,  # drop desktop ASR during donation/media overlays
    "desktop_ear_gate_max_sec": 75.0,    # hard deadline per event, including unknown clips
    # Telinga mati (5 gagal beruntun) tidak lagi permanen: rehat segini
    # lalu coba bangun sendiri, sekali per siklus (device bisa kaget saat
    # aplikasi lain membuka perangkat audio).
    # 0 = perilaku lama (menyerah permanen sampai restart bridge).
    "desktop_audio_revival_sec": 120.0,
    "desktop_audio_min_rms": 0.004,      # chunk lebih sunyi dari ini = skip (hemat kuota)
    # Gerbang UCAPAN: RMS tak menolong saat ada musik — musik tidak sunyi,
    # tiap 5 dtk satu potongan terkirim ke Whisper (konteks sampah + kuota
    # kebuang). VAD silero v6
    # (bundel faster-whisper, lokal, ~ms) menyaring; lagu bervokal tetap
    # lolos (trade yang diterima Bohan). Rincian: arti_vad.py.
    "desktop_audio_vad_enabled": True,
    "desktop_audio_vad_threshold": 0.5,
    "desktop_audio_post_tts_cooldown_sec": 3.0,  # ekor gema TTS yang terdengar balik
    "desktop_audio_context_ttl_sec": 180,  # turn normal cuma dengar yang segar
    "desktop_audio_context_max_lines": 6,
    "co_watch_mode_enabled": False,
    "screen_ring_buffer_size": 5,
    "watch_party_enabled": False,
    "watch_party_event_id": "",
    "watch_party_rag_window_sec": 45,
    # Ekor sunyi sebelum ucapan dianggap selesai. Sejak 12 Agu 2026 ini DETIK
    # SUNGGUHAN (dulu hitungan potongan audio: nilai 10.0 = cuma ~2 dtk nyata,
    # dan panjangnya ikut berubah kalau driver mic ganti — lihat jam VAD di
    # voice_listener_worker).
    "asr_silence_tail_sec": 2.0,
    "asr_max_utterance_sec": 30.0,                    # Finalize audio meski suara terus menyala
    "asr_audio_queue_max_chunks": 100,               # Drop oldest saat ASR tertahan
    # 5.0 menggantikan 10.0 palsu yang nyatanya ~2 dtk. Naikkan kalau ucapan
    # masih kepotong saat streamer menimbang kata.
    "asr_ptt_silence_tail_sec": 5.0,
    # ENDPOINT ADAPTIF. Ekor 5 dtk di atas benar untuk saat streamer menimbang
    # kata, tapi harganya dibayar SETIAP ucapan — termasuk jawaban pendek yang
    # jelas sudah selesai. Transkripsi Groq sendiri
    # cuma ~630 ms; sisanya murni menunggu.
    # Cara kerja: pada ekor_cepat kirim transkrip SPEKULATIF di latar, lalu
    # nilai teksnya (arti_endpoint). Kalau kalimatnya utuh DAN hening sudah
    # melewati ekor_aman, jalan sekarang; kalau menggantung, tunggu sabar
    # sampai ekor penuh. Bohan tidak pernah dipotong lebih cepat dari
    # ekor_aman — itu pengaman kedua untuk titik buta tata bahasa.
    # BAWAAN MATI; nyalakan di config_local saat mau diuji.
    "asr_ptt_adaptif_enabled": False,
    "asr_ptt_ekor_cepat_sec": 1.0,
    "asr_ptt_ekor_aman_sec": 1.8,
    # Ekor jalur PASIF (toggle OFF) — pendek: cuma mencatat ke history, tidak
    # perlu kesabaran 5 dtk. Dengan ekor panjang, monolog masuk sebagai
    # bongkahan kalimat basi dan tanggapan Arti telat.
    "asr_pasif_silence_tail_sec": 2.0,
    # MIC PASIF -> GILIRAN (owner 2026-10-23: "[recorded remark omitted]"). Ucapan PTT-mati yang lolos pagar diantrikan sebagai trigger
    # "mic_pasif"; MODEL yang memutuskan menjawab atau diam ([DIAM]). Pagar di
    # bawah cuma penjaga, bukan keputusan. Kill switch: enabled=false.
    "asr_pasif_reply_enabled": True,
    "asr_pasif_reply_min_words": 3,
    "asr_pasif_reply_min_sec": 1.0,
    "asr_pasif_reply_cooldown_sec": 20.0,
    # Sapaan pendek ("Halo", "oi") tanpa nama Arti lolos pagar panjang HANYA
    # kalau baru ada giliran Bohan<->Arti dalam jendela ini DAN bukan gema
    # TTS (smoke 2026-10-24 T3-25). Model tetap boleh memilih [DIAM].
    "asr_pasif_short_recent_sec": 90.0,
    "asr_pasif_short_echo_sec": 3.0,
    # Solo 2026-11-15 (arti_runtime/mic_hygiene.py): default OFF = today's path; mabar turns it on.
    # On: Whisper vocabulary bleed is noise, and a passive line during a mediashare hold is
    # history only unless it names her. mic/ptt/wake_word are never touched.
    "passive_mic_hygiene": False,
    # Solo 2026-11-15 (arti_runtime/mic_priority.py): default OFF = today's path; mabar turns it on.
    # On: address rules become a priority mark (not a gate), unmarked mic_pasif cannot discard
    # viewer chat, chat rotates fairly, and a donation thank-you is queued before the hold.
    "mic_priority_mark": False,
    "thanks_once_enabled": False,
    "chat_quiet_answer_all": False,
    "chat_busy_messages": 8,
    "chat_busy_window_sec": 180.0,
    "chat_merge_window_sec": 30.0,
    "media_watch_mode": False,
    "media_reaction_rank": 0.25,
    "media_summary_wait_sec": 2.0,
    "media_summary_timeout_sec": 15.0,
    "video_watcher_v2": False,
    "video_gemini_enabled": True,
    "video_meta_timeout_sec": 8.0,
    "video_captions_timeout_sec": 5.0,
    "video_audio_timeout_sec": 20.0,
    "video_frames_timeout_sec": 20.0,
    "video_vision_timeout_sec": 10.0,
    "video_audio_max_sec": 180.0,
    "video_keep_media": False,
    "video_media_max_mb": 200,
    "video_vision_cap_usd": 0.10,
    # Pamit atas permintaan Bohan: model menutup jawabannya dengan tag
    # [TUTUP]; selama jendela ini inisiatif/curious tidak membuka topik baru.
    # Dicabut otomatis begitu Bohan bersuara lagi (T3-26).
    "farewell_quiet_sec": 600.0,
    # Websocket idle VTS (T3-22): batas tunggu balasan per request, dan
    # keepalive ping websockets (<=0 = matikan ping).
    "idle_vts_request_timeout_sec": 3.0,
    "idle_vts_ping_interval_sec": 20.0,
    "idle_vts_ping_timeout_sec": 20.0,
    # Jeda napas antar kalimat TTS (17 Agu): tiap kalimat = satu beat obrolan,
    # celahnya ruang Bohan buat nimpali (toggle = potong sisa). 0 = nonstop.
    # Untuk sesi ngobrol santai bisa pakai ~4 (set di config_local).
    "tts_jeda_antar_kalimat_sec": 0.0,
    "groq_stream_enabled": False,
    "expression_nod_enabled": True,
    "expression_nod_smooth": True,
    "expression_nod_period_sec": 0.85,
    "expression_nod_fps": 12,
    "expression_nod_wait_tts_sec": 30.0,

    # Mood overlay saat bicara ([EMOTION:...] dari LLM)
    "expression_emotion_enabled": True,
    # LINGER EMOSI (A3, 27 Agu). 0 = perilaku lama (mood mati seketika di
    # akhir giliran). Bawaan sengaja MATI: enam perubahan lain masih
    # menunggu sesi uji, dan menumpuk yang ketujuh membuat hasil sesi itu
    # tidak terbaca. Nyalakan di config_local SEBAGAI PERUBAHAN TUNGGAL
    # sesudah keenamnya terbukti. Nilai yang disarankan untuk dicoba: 2.5
    "expression_emosi_linger_sec": 0.0,
    # Lama PELELEHAN mood saat linger habis. VTS mendokumentasikan 0-2 dtk;
    # di atas itu dijepit. 0 = potong seketika (perilaku sebelum 27 Agu).
    "expression_emosi_fade_sec": 2.0,

    # Hotkey VTS untuk potong motion badan saat aware
    "idle_motion_stop_hotkey": "IdleMotionStop",
    # MODE SAMBUNG (Fase 6). Terukur 25 Agu: dalam sesi ngobrol 3 menit,
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
    # Dibuat bisa diatur 25 Agu 2026 supaya motion percobaan (mis. salinan
    # tanpa kurva kepala dari scripts/buat_motion_tanpa_kepala.py) bisa diuji
    # LIVE sambil Arti benar-benar bicara, tanpa mengedit kode dan tanpa
    # menyentuh hotkey produksi. Isi di config_local, hapus lagi sesudah uji.
    "idle_motion_hotkeys": [],
    "idle_vts_connect_timeout_sec": 20,
    "idle_vts_connect_retry_sec": 15,
    # Stardew G1-0 goal runtime. Shipped OFF: tanpa receiver, provider, atau spend.
    # Bukan generalisasi WEB-1. Nyalakan hanya dari config_local setelah izin CT.
    "stardew_runtime_enabled": False,
    "stardew_provider_enabled": False,
    "stardew_spend_enabled": False,
    "stardew_receiver_port": 3017,
}

# ==========================================
# CONFIG OVERLAY LOKAL (config_local.json — gitignored)
# ==========================================
# Nilai pribadi/per-mesin (tanggal debut, youtube_video_id, port VTS, dll)
# di-override dari config_local.json agar kode tetap generik untuk repo publik.
# Contoh: lihat config_local.json.example.
def _load_local_config() -> None:
    rehearsal_path = os.environ.get("ARTI_REHEARSAL_CONFIG", "").strip()
    if rehearsal_path:
        from scripts.livepath.bridge import load_config
        CONFIG.update(load_config(Path(rehearsal_path)))
        from scripts.livepath.bridge import start_parent_watchdog
        start_parent_watchdog(Path(rehearsal_path).resolve().parent, CONFIG["rehearsal_parent_pid"])
        print("[CONFIG] Isolated live-path rehearsal (real main loop; profile smoke)")
        return
    # Test mode never reads the owner's config_local.json: the separate test
    # config is the only overlay, and an unreadable one fails closed (no fallback).
    test_path = os.environ.get("ARTI_BRIDGE_TEST_CONFIG", "").strip()
    if test_path:
        CONFIG["bridge_test_mode"] = True
        CONFIG["bridge_test_config_path"] = test_path
        try:
            with open(test_path, "r", encoding="utf-8") as f:
                overrides = json.load(f)
            if not isinstance(overrides, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            CONFIG["bridge_test_config_error"] = type(e).__name__
            print(f"[CONFIG] test config unreadable ({type(e).__name__}); test mode will refuse to start")
            return
        CONFIG.update(overrides)
        CONFIG["bridge_test_mode"] = True
        print(f"[CONFIG] Test config dimuat: {len(overrides)} key (config_local.json TIDAK dibaca)")
        return
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
# 16 Agu 14.28: log aktif (<20 KB saat startup) tergolong "remeh" lalu dicoba
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
# PELAJARAN: baris [GAYA BICARA] dulu melarang cara menyapa langsung. Larangan
# itu menutup SATU-SATUNYA cara menyapa langsung, jadi Arti cuma bisa menyebut
# nama lalu MENDESKRIPSIKAN orangnya — orang ketiga, terdengar melapor. Model
# penalaran paling patuh pada instruksi harfiah, jadi paling kaku. Adu langsung
# dengan kalimat penonton yang sama: prompt lama tidak pernah pakai "kamu",
# prompt baru selalu. Kalau baris itu diubah lagi, UJI dengan cara yang sama
# — jangan menilai dari membaca. Tes penjaga: test_prompt_sapa_langsung.
# Aturan 11: GIF di Discord jangan dijawab dengan deskripsi, karena teman
# bakal membalas sebagai kalimat obrolan berikutnya.
_SYSTEM_PROMPT_BASE = """[IDENTITAS]
Nama: Arti
Peran: Co-host VTuber AI di sebuah live stream (contoh persona; persona sungguhan ada di ARTI_SOUL.md yang tidak di-commit)
Bahasa: Bahasa Indonesia sebagai bahasa utama. Kata Inggris yang umum ("chat", "stream", "game") boleh.

[CARA KAMU MENANGGAPI]
Ramah, santai, dan penasaran. Tanggapi maksud lawan bicara sebagai teman ngobrol; reaksi, bukan narasi keadaan atau ceramah.
Hal teknis produksi siaran (layout, model/provider, API, file, kode) bukan topik obrolan; jangan mengarang penjelasan teknis.

[GAYA BICARA]
- Kasual dan jelas
- Bicara langsung ke orangnya pakai "kamu"; sebut nama cuma kalau perlu jelas lagi ngomong ke siapa.
- JANGAN pakai asterisk, markdown, emoji, atau formatting apapun
- Jawab dalam 2 sampai 3 kalimat.

[PENUTUP]
- JANGAN menutup jawaban dengan salam pamitan atas inisiatifmu sendiri. Kalau streamer memintamu pamit ke penonton atau menutup sesi: pamit SEKALI ke semua yang menonton, lalu tutup jawaban itu dengan tag [TUTUP] di paling akhir (dieksekusi sistem, JANGAN disebut/dibaca).

[EKSPRESI]
JANGAN pakai tag <laugh>, <sigh>, <breath>. Kalau perlu, tulis teks fonetik seperti "haha" atau "hmm", paling banyak satu per jawaban.

[ATURAN MUTLAK]
1. Jawab dalam bahasa utama di atas
2. JANGAN pakai asterisk, markdown, atau formatting
3. JANGAN jelaskan proses berpikir atau analisis
4. JANGAN jawab lebih dari 3 kalimat
5. Nama teman main sesi co-op tercatat pada fakta sesi saat ini.
6. Kamu Arti. Jawab sebagai dirimu sendiri; jangan membahas dirimu sebagai karakter atau peran
7. Politik dan agama: jangan memihak; elak dengan santai lalu ganti topik.

[CONTOH INTERAKSI]
Penonton: "halo arti!"
Arti: "(jawaban singkat natural dalam karakter)"

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
    stardew_voice_epoch: int | None = None
    owner_marked: bool = True
    rotation_rank: int = 0
    owner_reply_required: bool = False
    ptt_generation: int | None = None
    host_story_retry: bool = False


def _normalize_voice_trigger(item) -> VoiceTrigger:
    if isinstance(item, VoiceTrigger):
        return item
    if hasattr(item, "text"):
        return VoiceTrigger(
            item.text if isinstance(item.text, str) else str(item.text),
            str(getattr(item, "trigger_type", "mic") or "mic"),
            getattr(item, "viewer_name", None),
            float(
                getattr(item, "queued_at", 0.0)
                or getattr(item, "enqueued_at", 0.0)
                or 0.0
            ),
            getattr(item, "turn_id", None),
            getattr(item, "stardew_voice_epoch", None),
            bool(getattr(item, "owner_marked", True)),
            int(getattr(item, "rotation_rank", 0) or 0),
            bool(getattr(item, "owner_reply_required", False)),
            getattr(item, "ptt_generation", None),
            bool(getattr(item, "host_story_retry", False)),
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
def _make_voice_trigger_buffer():
    priorities = dict(CONFIG.get("voice_queue_priority_overrides") or {})
    if CONFIG.get("media_watch_mode"):
        priorities["video"] = float(CONFIG.get("media_reaction_rank", 0.25))
    return arti_voice_queue.VoiceTriggerQueue(
        max_yt=int(CONFIG.get("yt_chat_queue_max", 2)),
        ttl_sec=float(CONFIG.get("yt_chat_queue_ttl_sec", 60.0)),
        priority_overrides=priorities,
        preserve_humans=bool(CONFIG.get("voice_queue_preserve_humans", False)),
        human_age=lambda created, now: (chat_media.holds.age(created, now)
                                      if CONFIG.get("media_watch_mode") else now - created),
        play_active=lambda: _game_live(),
    )


voice_trigger_buffer = _make_voice_trigger_buffer()
_game_reaction_queue_lock = threading.Lock()
_last_yt_trigger_by_viewer: dict[str, float] = {}
# Nama yang PERNAH mengetik apa pun di chat sesi ini (untuk deteksi penonton
# baru). Sengaja tanpa TTL: "baru" = belum pernah muncul SEJAK bridge nyala.
_yt_pernah_chat: set[str] = set()
_chat_rotator = chat_rotation.Rotator()
_quiet_chat = chat_media.QuietChat()
_pending_turn_id = None


def _merge_waiting_yt_chat(viewer, extra_line, *, merge_window=None, cap=chat_rotation.MERGE_CAP, answer_required=None) -> bool:
    extra = str(extra_line or "").strip()
    if not extra:
        return False
    if CONFIG.get("voice_queue_enabled", False):
        return voice_trigger_buffer.merge_viewer_chat(
            viewer, extra, cap=cap, max_age=merge_window, answer_required=answer_required)
    with voice_trigger_queue.mutex:
        items = voice_trigger_queue.queue
        for index, raw in enumerate(items):
            item = _normalize_voice_trigger(raw)
            if item.trigger_type != "yt_chat" or item.viewer_name != viewer:
                continue
            if merge_window is not None and time.time() - item.queued_at > merge_window:
                continue
            merged = arti_voice_queue.merged_viewer_text(item.text, extra, cap=cap, answer_required=answer_required)
            items[index] = replace(
                item, text=merged, viewer_name=None if isinstance(merged, arti_voice_queue.CombinedChat) else viewer,
                rotation_rank=1 if item.text.count("\n— ") >= 1 else item.rotation_rank)
            return True
    return False


def _admit_quiet_viewer_chat(viewer, text, *, baru, wake, platform="YouTube") -> bool:
    """One decision for both readers, before the legacy cooldown/throttle gates."""
    if not (CONFIG.get("chat_quiet_answer_all") or CONFIG.get("media_watch_mode")):
        return False
    now = time.time()
    quiet = _quiet_chat.observe(CONFIG, now) and bool(CONFIG.get("chat_quiet_answer_all"))
    watching = chat_media.watch_held(CONFIG, now)
    if not (quiet or watching):
        return False
    if _merge_waiting_yt_chat(viewer, text,
                              merge_window=float(CONFIG.get("chat_merge_window_sec", 30.0)), cap=None, answer_required=quiet):
        _last_yt_trigger_by_viewer[viewer] = now
        return True
    # Reuse the reader's existing labels and rotation, without its lossy merge cap.
    from arti_runtime import ssn_chat
    line = ssn_chat.ChatLine(platform, viewer, text, "")
    decision = ssn_chat.decide(line, is_bot=False, wake=wake, new_viewer=baru, now=now,
                               last_viewer=0, last_addressed=0, last_trigger=0,
                               config={**CONFIG, "mic_priority_mark": True, "yt_chat_santai_enabled": True})
    extra = {}
    if CONFIG.get("mic_priority_mark"):
        extra["rotation_rank"] = _chat_rotator.choose(
            viewer, text, baru=baru, wake=wake, last_reply=last_arti_reply_text).rank
    accepted = queue_voice_trigger(chat_media.PendingChat(decision.label, answer_required=quiet),
                                   trigger_type="yt_chat", viewer_name=viewer, **extra)
    if accepted:
        _last_yt_trigger_by_viewer[viewer] = now
        if CONFIG.get("mic_priority_mark"):
            _chat_rotator.note(viewer)
    return accepted


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
    """PTT ON: invalidate pending Stardew work and pause idle/motion.

    Owner 2026-10-24 (live): omongan Bohan TIDAK PERNAH memotong audio Arti
    yang sedang diputar. PTT ON tidak lagi set tts_stop_flag / sd.stop();
    hanya perintah stop eksplisit (speech_control) yang memotong.
    """
    global _stardew_ptt_epoch, _ptt_last_speech_at
    _ptt_last_speech_at = time.monotonic()
    speech_control.note_owner_activity()
    _stardew_ptt_epoch = _note_stardew_owner_ingress("ptt", None)
    if "_note_stardew_bridge_trial_ptt_capture" in globals():
        _note_stardew_bridge_trial_ptt_capture()
    _cancel_lamp_fallback()
    stop_idle_animation()
    stop_name = (CONFIG.get("idle_motion_stop_hotkey") or "").strip()
    if stop_name:
        try:
            _idle_hotkey_cmd_queue.put_nowait(stop_name)
        except Exception:
            pass
    print("[PTT] Idle+motion pause — tunggu omongan streamer.")


_ptt_last_speech_at = 0.0
_ptt_capture_generation = 0
_ptt_capture_opened_at = None
VIEWER_PTT_GATE_MAX_SEC = 120.0


def _viewer_ptt_gate_closed() -> bool:
    opened_at = _ptt_capture_opened_at
    return hotkey_active and (opened_at is None or time.monotonic() - opened_at < VIEWER_PTT_GATE_MAX_SEC)


def _note_ptt_speech() -> None:
    global _ptt_last_speech_at
    if hotkey_active:
        _ptt_last_speech_at = time.monotonic()


def _maybe_auto_off_ptt() -> None:
    """Existing main-loop tick releases silent/rejected PTT captures."""
    global hotkey_active, _ptt_capture_opened_at
    timeout = float(CONFIG.get("ptt_auto_off_sec", 60.0))
    if (not hotkey_active or timeout <= 0
            or time.monotonic() - _ptt_last_speech_at < timeout):
        return
    hotkey_active = False
    _ptt_capture_opened_at = None
    _discard_stardew_capture(_stardew_ptt_epoch)
    _cancel_lamp_fallback()
    if main_event_loop and not main_event_loop.is_closed():
        asyncio.run_coroutine_threadsafe(vts.trigger_expression_state("default"), main_event_loop)
    start_idle_animation()
    print(f"[PTT] auto-off — {timeout:g}s tanpa speech capture; idle/proaktif aktif lagi.")


def _finish_reply_ptt(trigger) -> None:
    """Only the reply to the current deliberate capture can release it."""
    global hotkey_active, _ptt_capture_opened_at
    if trigger.trigger_type == "ptt":
        with _sd_gerbang:
            if (hotkey_active and not tts_stop_flag
                    and getattr(trigger, "ptt_generation", None) == _ptt_capture_generation):
                hotkey_active = False
                _ptt_capture_opened_at = None


def _toggle_owner_capture():
    """Serialize PTT ON with the last gate before native playback starts."""
    global hotkey_active, _ptt_capture_generation, _ptt_last_speech_at, _ptt_capture_opened_at
    with _sd_gerbang:
        hotkey_active = not hotkey_active
        if hotkey_active:
            _ptt_capture_generation += 1
            _ptt_capture_opened_at = _ptt_last_speech_at = time.monotonic()
        else:
            _ptt_capture_opened_at = None


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


def _note_owner_speech(text, trigger_type, viewer_name):
    """Trusted current ingress only; never interpret a queued or viewer command."""
    global tts_stop_flag, _farewell_quiet_until
    if trigger_type not in ("mic", "ptt", "wake_word", "mc_chat", "mic_pasif") or viewer_name is not None:
        return None
    if speech_control.command(text) is None:
        # Owner 2026-10-24 (live): omongan Bohan tanpa perintah stop TIDAK
        # memotong audio yang sedang diputar dan tidak membatalkan jawaban
        # yang sedang diucapkan (epoch tidak di-bump). Ucapan terbaru jadi
        # giliran berikutnya lewat prioritas antrean. Chat penonton TETAP antre:
        # dulu ikut dibuang tiap Bohan bicara (13 pesan hilang 2026-10-24); umur
        # antrean yang membuangnya kalau sudah basi, dan itu tercatat di log.
        speech_control.note_owner_activity()
        _farewell_quiet_until = 0.0
        if not CONFIG.get("voice_queue_preserve_humans", False):
            clear_trigger_queue(keep_chat=True, keep_addressed=True)
        return None
    command = speech_control.note(text, trigger_type, viewer_name)
    # Bohan bersuara lagi sesudah pamit = obrolan boleh lanjut; model menilai.
    _farewell_quiet_until = 0.0
    # Drop stale conversations, retaining paid/media receipts and motor epochs. Mabar keeps viewer chat
    # (owner 2026-11-10: never drop events); it waits behind the owner and her persona judges it.
    clear_trigger_queue(stop=command == "stop", keep_chat=command == "resume", keep_addressed=command == "resume")
    tts_stop_flag = True
    if tts_is_playing or _reflex_playing:
        try:
            with _sd_gerbang:
                sd.stop()
        except Exception:
            pass
    if command:
        print(f"[SpeechControl] {command}; epoch={speech_control.epoch}")
    return command


_GAME_SESSION_LANE = frozenset({"game", "own_play", "read_in_game", "status", "moment"})
_OWN_PLAY_YIELD_KINDS = frozenset({"mic", "ptt", "mic_pasif", "donation", "yt_chat", "video"})


def _live7_game_session_stopped():
    relay = globals().get("_live7_help_relay")
    checker = getattr(relay, "game_session_stopped", None) if relay is not None else None
    return bool(callable(checker) and checker())


def _live7_game_lane_trigger(trigger) -> bool:
    if getattr(trigger, "trigger_type", None) in _GAME_SESSION_LANE:
        return True
    text = getattr(trigger, "text", None)
    return bool(getattr(text, "_live7_status", False) or getattr(text, "_live7_moment", False)
                or getattr(text, "_own_play", False))


def _remember_live_session_fact(fact):
    relay = globals().get("_live7_help_relay")
    remember = getattr(relay, "remember_session_fact", None) if relay is not None else None
    if callable(remember):
        remember(fact)


def _speech_trigger_expired(trigger):
    host_context = getattr(trigger.text, "host_story_context", None)
    if host_context is not None and not host_context.is_current():
        return True
    if getattr(trigger.text, "_host_story_guarded", False):
        return not trigger.text.is_current()
    if _live7_game_lane_trigger(trigger) and _live7_game_session_stopped():
        return True
    if trigger.trigger_type == "own_play":
        return streamer_voice.own_play_expired(trigger, time.time(), pending=(
            (_game_trigger_waiting() and not attention_desk.enabled(CONFIG))
            or _own_play_held_back()))
    if trigger.trigger_type == "read_in_game":
        return time.time() - trigger.text.created_at > attention_desk.GAME_TTL_SEC
    if (CONFIG.get("voice_queue_preserve_humans", False)
            and trigger.trigger_type in arti_voice_queue.HUMAN_KINDS):
        return False
    if getattr(trigger.text, "answer_required", False):
        return False
    if speech_control.held:
        return True
    if trigger.owner_reply_required or (trigger.trigger_type == "yt_chat" and _game_live()):
        return False
    if trigger.trigger_type in ("yt_chat", "curious", "mic_pasif", "mic", "ptt", "wake_word"):
        age = time.time() - trigger.queued_at if trigger.queued_at else 0.0
        if CONFIG.get("media_watch_mode") and trigger.queued_at:
            age = chat_media.holds.age(trigger.queued_at, time.time())
        ttl = float(CONFIG.get("live_reply_queue_max_age_sec", 30.0))
        owner_newer = bool(trigger.queued_at and trigger.queued_at < speech_control.changed_at
                           and not getattr(trigger.text, "retain_pending", False))
        if trigger.trigger_type == "yt_chat" and CONFIG.get("live_reply_chat_keep"):
            # Mabar: a viewer line waits through her long answers and the owner's talk (never dropped silently).
            ttl, owner_newer = float(CONFIG.get("live_reply_chat_max_age_sec", 900.0)), False
        if age > ttl or owner_newer:
            print(f"[Queue] Drop stale {trigger.trigger_type}; age={age:.1f}s")
            if trigger.stardew_voice_epoch is not None:
                _discard_stardew_capture(trigger.stardew_voice_epoch)
            return True
    return False


def _last_reply_end_ts() -> float:
    tts_end = float(getattr(voice_listener_worker, "_last_tts_end", 0.0) or 0.0)
    return max(float(_last_arti_reply_ts or 0.0), tts_end)


def _mic_pasif_owner_marked(text) -> bool:
    """Flag off: today's owner-side effects. Flag on: vocative/address rules."""
    if not CONFIG.get("mic_priority_mark"):
        return True
    marked, _reason = mic_priority.mark(
        text,
        last_reply=last_arti_reply_text,
        last_reply_end_ts=_last_reply_end_ts(),
        now=time.time(),
    )
    return bool(marked)


def _passive_owner_reply_required(text) -> bool:
    if not (CONFIG.get("mic_priority_mark") or CONFIG.get("voice_queue_preserve_humans")):
        return False
    if mic_priority.is_vocative(text) or (CONFIG.get("asr_address_names") and streamer_voice.addressed(text, CONFIG)):
        return True
    return (
        (mic_priority.is_question(text) or mic_priority.is_request_to_her(text))
        and not mic_priority.addressed_viewer(text, _yt_pernah_chat)
    )


def _drop_waiting_unmarked_mic() -> None:
    """At most one unmarked mic_pasif waits; the older line stays in history."""
    if CONFIG.get("voice_queue_enabled", False):
        voice_trigger_buffer.drop_if(lambda old: old.trigger_type == "mic_pasif" and not old.owner_marked)
        return
    kept = []
    while True:
        try:
            item = voice_trigger_queue.get_nowait()
        except queue.Empty:
            break
        trig = _normalize_voice_trigger(item)
        if trig.trigger_type == "mic_pasif" and not trig.owner_marked:
            continue
        kept.append(item)
    for item in kept:
        voice_trigger_queue.put(item)


def queue_voice_trigger(text, trigger_type="mic", viewer_name=None, *, asr_stages=None, stardew_voice_epoch=None,
                        owner_marked=None, rotation_rank=0, ptt_generation=None) -> bool:
    """Antrikan jawaban dan log trigger; True hanya jika trigger diterima."""
    global _pending_turn_id, _last_human_activity_ts, _last_streamer_speech_ts
    if trigger_type in _GAME_SESSION_LANE and (
            (not text.is_current() if getattr(text, "_host_story_guarded", False) else _live7_game_session_stopped())
            or (CONFIG.get("media_watch_mode") and chat_media.watch_held(CONFIG, time.time()))):
        return False
    if trigger_type == "read_in_game":
        add_to_history("Game read", str(text))
        if time.time() - text.created_at > attention_desk.GAME_TTL_SEC:
            return False
    if trigger_type == "own_play" and (
            not CONFIG.get("live7_own_play_enabled", False) or (not attention_desk.enabled(CONFIG) and (
            _brain_busy or tts_is_playing or hotkey_active or _game_trigger_waiting())
            and not (CONFIG.get("live7_reactive_play_enabled") and _own_play_can_wait()))
            or (CONFIG.get("live7_reactive_play_enabled") and hotkey_active)
            or _own_play_held_back(admit=True)
            or time.time() - getattr(text, "created_at", 0) > 20):
        return False
    # The mark is computed where the passive transcript is handled and passed in; the owner-ingress path
    # never computes it. A caller that passes nothing keeps owner priority (today's behaviour).
    if owner_marked is None:
        owner_marked = True
    direct_owner = viewer_name is None and trigger_type in arti_voice_queue.DIRECT_OWNER_KINDS
    if direct_owner and str(text).strip() in _ASR_LITERAL_PHANTOMS:
        if stardew_voice_epoch is not None:
            _discard_stardew_capture(stardew_voice_epoch)
        return False
    owner_reply_required = viewer_name is None and (
        (trigger_type == "mic_pasif" and _passive_owner_reply_required(text))
        or (direct_owner and (mic_priority.is_vocative(text) or mic_priority.is_question(text))))
    owner_marked = owner_marked or owner_reply_required
    unmarked_passive = (
        bool(CONFIG.get("mic_priority_mark"))
        and trigger_type == "mic_pasif"
        and not owner_marked
    )
    command = None if unmarked_passive else _note_owner_speech(text, trigger_type, viewer_name)
    held_human = ((trigger_type == "yt_chat" and getattr(text, "retain_pending", False))
                  or (CONFIG.get("voice_queue_preserve_humans", False)
                      and trigger_type in arti_voice_queue.HUMAN_KINDS))
    if command or (speech_control.held and trigger_type not in ("donation", "video", "game") and not held_human):
        if stardew_voice_epoch is not None:
            _discard_stardew_capture(stardew_voice_epoch)
        return False
    log_text = "[Live7 moment]" if getattr(text, "_live7_moment", False) else text
    voice_epoch = _note_stardew_owner_ingress(trigger_type, viewer_name, text, stardew_voice_epoch)
    if voice_epoch == -1: return False
    if CONFIG.get("live7_help_enabled", False) and _live7_help_relay is not None:
        if trigger_type in ("ptt", "mic", "wake_word") and viewer_name is None:
            _live7_help_relay.note_owner_text(text)
        elif trigger_type == "mic_pasif" and viewer_name is None:
            _live7_help_relay.note_said(_live7_help_relay._said_owner_name(), "mic", text)
        elif trigger_type == "donation":
            _live7_help_relay.note_priority_turn()
    # Apply at trusted ingress, BEFORE busy/queue rejection. Never replay a
    # delayed command when dequeuing; viewer text/display names carry no authority.
    if trigger_type in STREAMER_TRIGGERS:
        _note_screen_privacy(text)
    # Detektor kehidupan: jalur AKTIF (streamer manggil Arti via PTT/wake,
    # donasi, link video) tidak lewat add_to_history "Streamer" — tanpa bump
    # ini, streamer yang ngobrol intens dengan Arti tanpa chat justru bikin
    # proaktif "tidur" padahal manusianya paling aktif.
    # wake_word WAJIB ada di sini: mode trigger itu tidak lewat
    # add_to_history("Streamer", ...) sama sekali (audit 2026-08-03), jadi
    # tanpa ini streamer yang ngobrol via wake word tetap kena dormansi.
    if trigger_type in ("mic", "ptt", "wake_word", "yt_chat", "donation",
                        "video", "mc_chat"):
        _last_human_activity_ts = time.time()
        # mc_chat ikut di sini: Bohan yang mengetik DI DALAM GAME jelas hadir,
        # jadi mode host harus mundur persis seperti kalau dia bersuara.
        if trigger_type in ("mic", "ptt", "wake_word", "mc_chat"):
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
    # "game" ikut kebal (audit 2026-08-05): perlindungannya dulu cuma ada di
    # arti_voice_queue — modul yang TIDAK PERNAH JALAN di mesin uji
    # (voice_queue_enabled=False). Jalur yang benar-benar aktif adalah
    # queue.Queue + drain-newest, dan di situ reaksi kematian dibuang tanpa
    # jejak begitu ada trigger lain menyusul.
    # "mic" ikut kebal: satu-satunya sumbernya adalah Bohan MENGETIK di console
    # (text_input_worker) — suara asli selalu "ptt"/"wake_word". Risiko echo
    # yang jadi alasan drop tidak ada sama sekali untuk teks yang diketik,
    # sementara biayanya nyata: di log 2026-08-05 malam perintahnya "[recorded remark omitted]" hilang tanpa jawaban karena antrean reaksi game
    # sedang penuh. Perintah operator tidak boleh menguap.
    always_queue = (
        (trigger_type == "yt_chat" and (use_buffer or getattr(text, "retain_pending", False)))
        or attention_desk.enabled(CONFIG)
        or (trigger_type == "own_play" and CONFIG.get("live7_reactive_play_enabled") and _own_play_can_wait())
        or trigger_type in ("donation", "video", "game", "mic", "ptt", "wake_word", "mic_pasif")
        or voice_epoch is not None
        # Giliran mic_pasif yang sedang jalan membatalkan diri demi PTT;
        # ucapan PTT-nya antre, bukan dibuang (owner 2026-10-23).
        # (globals().get: unit seams compile this function alone.)
        or (trigger_type == "ptt" and globals().get("_passive_turn_inflight", False))
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

    if unmarked_passive:
        _drop_waiting_unmarked_mic()
    marked_kind = trigger_type == "mic_pasif" and CONFIG.get("mic_priority_mark")
    if use_buffer:
        item = arti_voice_queue.QueuedVoiceTrigger(
            text=text, trigger_type=trigger_type, viewer_name=viewer_name,
            owner_marked=bool(owner_marked), rotation_rank=int(rotation_rank or 0),
            owner_reply_required=owner_reply_required,
            ptt_generation=ptt_generation,
        )
        item.stardew_voice_epoch = voice_epoch
        item.name_addressed = (viewer_name is None and trigger_type in ("mic", "mic_pasif", "wake_word")
                               and (bool(owner_marked) if marked_kind else streamer_voice.addressed(text, CONFIG)))

        def _siapkan_item_buffer(accepted_item) -> None:
            accepted_item.turn_id = session_transcript.log_trigger(
                trigger_type, viewer_name, log_text[:500], CONFIG
            )

        if not voice_trigger_buffer.enqueue(item, prepare=_siapkan_item_buffer):
            if voice_epoch is not None:
                _discard_stardew_capture(voice_epoch)
            if trigger_type == "curious":
                print("[Queue] Curious deferred — YT pending di antrian")
            return False
        _pending_turn_id = item.turn_id
    else:
        _pending_turn_id = session_transcript.log_trigger(
            trigger_type, viewer_name, log_text[:500], CONFIG
        )
    depth = len(voice_trigger_buffer) if use_buffer else 0
    print(f"[Queue] Trigger ({trigger_type})"
          + (f" depth={depth}" if use_buffer else "")
          + f": \"{log_text[:100]}\"")
    if trigger_type == "donation":
        _request_alert_yield("donasi")
    if trigger_type == "yt_chat":
        global _last_yt_chat_trigger_ts
        _last_yt_chat_trigger_ts = time.time()
        who = viewer_name or "viewer"
        print(f"[YT Chat] Antri jawab {who} (VTS turn di main loop)")
    if not use_buffer:
        item = VoiceTrigger(text, trigger_type, viewer_name, time.time(), _pending_turn_id, voice_epoch,
                            bool(owner_marked), int(rotation_rank or 0), owner_reply_required, ptt_generation)
        if not _enqueue_legacy_voice(item):
            if voice_epoch is not None:
                _discard_stardew_capture(voice_epoch)
            return False
    if CONFIG.get("rehearsal_parent_pid"):
        from scripts.livepath.bridge import turn_event
        turn_event("queued", item.turn_id if use_buffer else _pending_turn_id,
                   VoiceTrigger(text, trigger_type, viewer_name, owner_marked=bool(owner_marked),
                                rotation_rank=int(rotation_rank or 0)),
                   name_addressed=viewer_name is None and trigger_type in ("mic", "mic_pasif", "wake_word")
                   and (bool(owner_marked) if marked_kind else streamer_voice.addressed(text, CONFIG)))
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
        if CONFIG.get("live_voice_fixes_v1"):  # V6.4: the owner's lines of the last 15 minutes, same lock
            from arti_runtime import voice_window
            owner_kept = voice_window.owner_lines()
            owner_marked = voice_window.marked_owner_lines()
    if CONFIG.get("live_voice_fixes_v1"):  # V6.1 (mabar): one chronological window instead of speaker groups
        from arti_runtime import voice_window
        return voice_window.render(all_history, owner_extra=owner_kept, marked=owner_marked)
    
    streamer_lines = []
    viewer_lines = {}
    arti_lines = []
    game_read_lines = []
    
    for line in all_history:
        if re.match(r"^\[\d\d:\d\d:\d\d\] \[Game read\] ", line):
            game_read_lines.append(line)
        elif "[Streamer]" in line:
            streamer_lines.append(line)
        elif "[Viewer" in line:
            # Extract viewer name
            # Audit 2026-08-05: pola lama `\[Viewer @(\w+)` TIDAK PERNAH cocok —
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
    if game_read_lines:
        result.append("=== NATIVE TEXT PREVIOUSLY READ IN HER GAME ===")
        result.extend(game_read_lines[-8:])  # Historical text, never a claim about the current screen.
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


# Keep the bridge signatures and pass the current config and dependencies at call time.
from arti_runtime import prompt_blocks  # noqa: E402


def build_origin_context(config: dict | None = None) -> str:
    """Fakta kanon debut + pointer arsip — selalu inject (hemat token)."""
    return prompt_blocks.build_origin_context(config or CONFIG)


def build_today_block(config: dict | None = None) -> str:
    """Tanggal hari ini + umur karier — Arti selama ini TIDAK tahu tanggal.

    Label [YYYY-MM-DD] di cuplikan RAG tak bisa dia hitung jadi "tiga hari lalu"
    tanpa jangkar ini (temuan rekon v0.7: tanggal sesi tidak pernah disuntik ke
    prompt mana pun). Ditutup pengingat debut supaya tidak menabrak aturan
    origin block ("jawab tanggal debut, bukan tanggal sesi").
    """
    return prompt_blocks.build_today_block(config or CONFIG, time=time)


def build_startup_memory_block(memories: list[str]) -> str:
    """Cuplikan memori startup — sisanya lewat Vault RAG per query (hemat token Groq)."""
    return prompt_blocks.build_startup_memory_block(memories, CONFIG, arti_live_safety=arti_live_safety)


# Discord -> stream stories (design §4, consumer only): built once at stream start
# when discord_stories_enabled, injected per turn only inside the opening window.
_discord_stories: dict = {"block": "", "until": 0.0}


def discord_stories_block_now() -> str:
    if _discord_stories["block"] and time.monotonic() < _discord_stories["until"]:
        return _discord_stories["block"]
    return ""


_SYSTEM_PROMPT_BLOCK_MARKERS = (
    # Discord stories (first 30 min only) go first: optional colour, not memory.
    arti_state.DISCORD_MARKER,
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
# Blocks that are NEVER trimmed but still end a trimmed block before them
# ([DIRIKU] sits after BASE, whose "[MEMORI JANGKA PANJANG]" text matches a marker).
_SYSTEM_PROMPT_KEEP_MARKERS = (arti_state.DIRIKU_MARKER, SOUL_MARKER)


# Keep bridge signatures and resolve configuration and dependencies at call time.
from arti_runtime import groq_chat  # noqa: E402


def _remove_system_prompt_block(text: str, marker: str) -> str:
    """Hapus satu blok opsional; blok lain setelahnya tetap."""
    return groq_chat._remove_system_prompt_block(
        text, marker,
        re=re,
        _SYSTEM_PROMPT_BLOCK_MARKERS=_SYSTEM_PROMPT_BLOCK_MARKERS,
        _SYSTEM_PROMPT_KEEP_MARKERS=_SYSTEM_PROMPT_KEEP_MARKERS,
    )


def _split_stardew_status(system_prompt: str) -> tuple[str, str]:
    """Reserve status, intent and generic current time/place facts outside every trim."""
    return groq_chat._split_stardew_status(
        system_prompt,
        config=CONFIG,
        re=re,
    )


def trim_system_prompt_for_llm(system_prompt: str, config: dict | None = None) -> str:
    """Pangkas system prompt kalau masih kebesaran untuk Groq TPM."""
    cfg = config or CONFIG
    cap = int(cfg.get("llm_system_prompt_max_chars", 5500))
    if len(system_prompt) <= cap:
        return system_prompt
    text, status = _split_stardew_status(system_prompt)
    # Facts win even over a misconfigured cap smaller than the status block.
    cap = max(0, cap - len(status))
    for marker in _SYSTEM_PROMPT_BLOCK_MARKERS:
        if len(text) <= cap:
            break
        if marker not in text:
            continue
        text = _remove_system_prompt_block(text, marker)
        print(f"[LLM] System prompt dipangkas (buang {marker.strip()})")
    if len(text) > cap:
        suffix = "\n...(system dipangkas)"
        text = (text[: max(0, cap - len(suffix))].rstrip() + suffix
                if cap >= len(suffix) else text[:cap])
        print(f"[LLM] System prompt dipangkas ke {len(text) + len(status)} chars")
    return text + status


def _extract_trigger_message(user_speech: str) -> str:
    m = re.search(r"\[Pesan Live Chat dari Viewer[^\]]+\]:\s*(.+)$", user_speech)
    if m:
        return m.group(1).strip()
    return user_speech.strip()


# === CANCEL/INTERRUPT SYSTEM ===
current_api_task = None          # asyncio.Task untuk LLM call yang sedang jalan
api_task_lock = asyncio.Lock()   # Lock untuk akses current_api_task
tts_stop_flag = False            # Flag untuk stop TTS mid-playback
_yield_after_sentence = False    # alerts: finish this sentence, then yield
_yield_after_sentence_reason = None  # "donasi" | "mediashare"
_yield_bound_timer = None
_reply_chunks_inflight = False
_pending_alert_yield_fact = None
cancel_event = asyncio.Event()   # Event signal buat cancel

def _enqueue_legacy_voice(item) -> bool:
    """Apply the same retention limits in the FIFO transport."""
    if not (item.owner_reply_required or item.trigger_type == "yt_chat"):
        voice_trigger_queue.put(item)
        return True
    now = time.time()
    playing = item.trigger_type == "yt_chat" and _game_live()
    limit = max(1, int(CONFIG.get("yt_chat_queue_max", 2)))
    with voice_trigger_queue.not_empty:
        items = voice_trigger_queue.queue
        normalized = [_normalize_voice_trigger(raw) for raw in items]
        combined = arti_voice_queue.combined_pending(normalized, item, max_yt=limit, playing=playing, now=now,
                                                    preserve_humans=CONFIG.get("voice_queue_preserve_humans", False))
        if combined is not None:
            index, merged = combined
            if index is None:
                return False
            items[index] = merged
            return True
        if item.trigger_type == "yt_chat":
            chat_indices = [i for i, raw in enumerate(items) if _normalize_voice_trigger(raw).trigger_type == "yt_chat"]
            if len(chat_indices) >= limit:
                del items[chat_indices[0]]
        items.append(item)
        voice_trigger_queue.unfinished_tasks += 1
        voice_trigger_queue.not_empty.notify()
    return True


def clear_trigger_queue(*, keep_chat=False, keep_addressed=False, stop=False):
    """Clear stale triggers, retaining paid events and pending media shares."""
    # Super Chats, memberships, Saweria and Streamlabs all enter as donation.
    keep_types = ("donation", "video", "game")
    if CONFIG.get("voice_queue_preserve_humans", False):
        keep_types += arti_voice_queue.HUMAN_KINDS
    legacy_play_chat = not CONFIG.get("voice_queue_enabled", False) and keep_addressed and keep_chat and _game_live()
    with _game_reaction_queue_lock:
        discarded = voice_trigger_buffer.clear(
            keep_types=keep_types + (("yt_chat",) if keep_chat else ()), keep_addressed=keep_addressed, stop=stop)
        with voice_trigger_queue.mutex:
            kept = []
            for item in voice_trigger_queue.queue:
                trigger = _normalize_voice_trigger(item)
                if not stop and (trigger.trigger_type in keep_types or (keep_addressed and trigger.owner_reply_required)
                        or (legacy_play_chat and trigger.trigger_type == "yt_chat")
                        or getattr(trigger.text, "retain_pending", False)):
                    kept.append(item)
                else:
                    discarded.append(item)
            voice_trigger_queue.queue.clear()
            voice_trigger_queue.queue.extend(kept)
            voice_trigger_queue.not_full.notify_all()
    # Release only the current discarded epoch, outside queue locks. The
    # existing speech-busy handoff delays continuation until actual finally.
    for item in discarded:
        epoch = getattr(item, "stardew_voice_epoch", None)
        if epoch is not None:
            _discard_stardew_capture(epoch)
        if tok := getattr(getattr(item, "text", None), "thanks_token", ""):
            __import__("arti_runtime.thanks_once", fromlist=["mark_state"]).mark_state(tok, "stopped")
    if discarded:  # Counts by type only, never a viewer's text or name.
        counts = {}
        for item in discarded:
            kind = _normalize_voice_trigger(item).trigger_type
            counts[kind] = counts.get(kind, 0) + 1
        print("[Queue] Dibuang: " + ", ".join(f"{kind}={n}" for kind, n in sorted(counts.items())))


def _dequeue_buffered_trigger_if_brain_ready(*, owner_only=False):
    """Ambil item buffer hanya jika main loop tidak sedang memegang giliran."""
    global _brain_busy, _brain_busy_since
    with _game_reaction_queue_lock:
        with _brain_busy_lock:
            if _brain_busy or speech_control.held:
                return None
            while True:
                holding_media = ((CONFIG.get("mic_priority_mark") and time.time() < _media_playback_until)
                        or (CONFIG.get("media_watch_mode") and chat_media.watch_held(CONFIG, time.time())))
                item = voice_trigger_buffer.dequeue_best(
                    lambda it: _capture_allows_turn(it) and (not owner_only or arti_voice_queue.owner_turn(it))
                    and (not holding_media or _media_hold_allows(it)))
                if item is None:
                    return None
                trigger = _normalize_voice_trigger(item)
                if _speech_trigger_expired(trigger) or _game_reaction_expired(trigger):
                    if tok := getattr(trigger.text, "thanks_token", ""):
                        __import__("arti_runtime.thanks_once", fromlist=["mark_state"]).mark_state(tok, "dropped")
                    continue
                _brain_busy = True
                _brain_busy_since = time.time()
                return trigger


def _capture_allows_turn(item):
    if not hotkey_active:
        return True
    if item.trigger_type == "yt_chat":
        return not _viewer_ptt_gate_closed()
    if item.trigger_type == "mic_pasif":
        return False
    return item.trigger_type != "ptt" or item.ptt_generation == globals().get("_ptt_capture_generation")


def _dequeue_preserved_legacy_trigger():
    """Select owner, donation and chat in the same order as the priority buffer."""
    with _brain_busy_lock:
        if _brain_busy or speech_control.held:
            raise queue.Empty
        with voice_trigger_queue.mutex:
            items = voice_trigger_queue.queue
            if not items:
                raise queue.Empty
            play_now = time.time() if _game_live() else 0.0
            def priority(raw):
                item = _normalize_voice_trigger(raw)
                if arti_voice_queue.owner_turn(item):
                    addressed = item.owner_reply_required or streamer_voice.addressed(item.text, CONFIG)
                    return (-2, 0 if addressed else 1, item.queued_at)
                if item.trigger_type == "yt_chat" and arti_voice_queue.play_chat_due(item.queued_at, play_now):
                    return (0.5, -1, item.queued_at)
                type_rank = arti_voice_queue._TRIGGER_PRIORITY.get(item.trigger_type, 5)
                if CONFIG.get("media_watch_mode") and item.trigger_type == "video":
                    type_rank = float(CONFIG.get("media_reaction_rank", 0.25))
                if item.trigger_type == "mic_pasif" and not item.owner_marked:
                    type_rank = 3
                return (type_rank, item.rotation_rank, item.queued_at or 0)
            eligible = [i for i in range(len(items)) if _capture_allows_turn(_normalize_voice_trigger(items[i]))
                        and (not CONFIG.get("media_watch_mode")
                             or _media_hold_allows(_normalize_voice_trigger(items[i])))]
            if not eligible:
                raise queue.Empty
            index = min(eligible, key=lambda i: priority(items[i]))
            raw = items[index]
            del items[index]
            voice_trigger_queue.not_full.notify()
            return raw


QUIET_CHAT_MAX_REQUEUES = 3


def _viewer_yields_to_owner(revision):
    if _viewer_ptt_gate_closed() or speech_control.superseded("yt_chat", revision):
        return True
    if CONFIG.get("voice_queue_enabled", False):
        return any(arti_voice_queue.owner_turn(it) and _capture_allows_turn(it)
                   for it in voice_trigger_buffer.snapshot())
    with voice_trigger_queue.mutex:
        pending = (_normalize_voice_trigger(it) for it in voice_trigger_queue.queue)
        return any(arti_voice_queue.owner_turn(it) and _capture_allows_turn(it) for it in pending)


_viewer_floor_revision = ContextVar("viewer_floor_revision", default=None)


def _speech_playback_allowed(epoch):
    revision = _viewer_floor_revision.get()
    return speech_control.allows(epoch) and (revision is None or not _viewer_yields_to_owner(revision))


def _voice_reply_superseded(trigger, revision):
    playing_chat = trigger.trigger_type == "yt_chat" and _game_live()
    if trigger.trigger_type == "yt_chat" and _viewer_yields_to_owner(revision):
        _requeue_quiet_chat(trigger, playing=playing_chat, owner_first=True)
        return True
    if playing_chat or getattr(trigger.text, "retain_pending", False):
        if not speech_control.superseded(trigger.trigger_type, revision):
            return False
        _requeue_quiet_chat(trigger, playing=playing_chat)
        return True
    if (CONFIG.get("voice_queue_preserve_humans", False)
            and trigger.trigger_type in arti_voice_queue.HUMAN_KINDS):
        return False
    return speech_control.superseded(trigger.trigger_type, revision)


def _name_call_waiting():
    if CONFIG.get("voice_queue_enabled", False):
        return voice_trigger_buffer.has_name_pending()
    with voice_trigger_queue.mutex:
        return any(_normalize_voice_trigger(it).viewer_name is None
                   and _normalize_voice_trigger(it).trigger_type in ("mic", "mic_pasif", "wake_word")
                   and streamer_voice.addressed(_normalize_voice_trigger(it).text, CONFIG)
                   for it in voice_trigger_queue.queue)


def _requeue_quiet_chat(trigger, *, playing=False, owner_first=False):
    """Return superseded chat to the queue. In play, keep the original deadline."""
    with speech_control.retaining(speech_turn_epoch.get()) as allowed:
        if allowed:
            _requeue_quiet_chat_current(trigger, playing=playing, owner_first=owner_first)


def _requeue_quiet_chat_current(trigger, *, playing, owner_first):
    text = trigger.text
    n = int(getattr(text, "quiet_requeues", 0) or 0)
    if not playing and n >= QUIET_CHAT_MAX_REQUEUES and not hotkey_active:
        print(f"[Queue] Chat {trigger.viewer_name or 'viewer'} disusul Bohan {n}x — tidak diantre lagi")
        return
    # A superseded chat could go to the back of the queue and be answered minutes later, after the same viewer's
    # newer chats. When that viewer already has a newer chat waiting, the old line joins it: answered once,
    # nothing lost.
    if (CONFIG.get("live_voice_fixes_v1") and CONFIG.get("voice_queue_enabled", False)
            and trigger.trigger_type == "yt_chat" and trigger.viewer_name):
        raw = str(text)
        head, sep, rest = raw.partition("]: ")
        line = rest if sep and raw.startswith("[") else raw
        if voice_trigger_buffer.merge_viewer_chat(trigger.viewer_name, line, cap=None,
                                                  answer_required=getattr(text, "answer_required", None),
                                                  skip_turn_id=trigger.turn_id,
                                                  enqueued_at=trigger.queued_at if playing else 0.0):
            print(f"[Queue] Chat {trigger.viewer_name} disusul Bohan — digabung ke chatnya yang masih antre")
            return
    if not (playing or owner_first and not getattr(text, "retain_pending", False)):
        try:
            text.quiet_requeues = n + 1
        except AttributeError:
            return
    if CONFIG.get("voice_queue_enabled", False):
        if (playing or owner_first) and not getattr(text, "retain_pending", False):
            text = chat_media.PendingChat(text)
        item = arti_voice_queue.QueuedVoiceTrigger(text, trigger.trigger_type, trigger.viewer_name,
                                                   trigger.queued_at if playing or owner_first else time.time(),
                                                   trigger.turn_id, trigger.stardew_voice_epoch,
                                                   owner_marked=bool(getattr(trigger, "owner_marked", True)),
                                                   rotation_rank=int(getattr(trigger, "rotation_rank", 0) or 0))
        voice_trigger_buffer.enqueue(item)
    else:
        _enqueue_legacy_voice(trigger)
    detail = " (umur awal tetap)" if playing else f" ({n + 1})"
    print(f"[Queue] Chat {trigger.viewer_name or 'viewer'} disusul Bohan — diantre lagi{detail}")


def _retain_interrupted_owner(trigger):
    with speech_control.retaining(speech_turn_epoch.get()) as allowed:
        if allowed:
            _retain_current_owner(trigger)


def _retain_current_owner(trigger):
    if trigger.trigger_type != "mic_pasif" and not isinstance(trigger.text, arti_voice_queue.CombinedOwnerQuestions):
        text = trigger.text
        trigger = replace(trigger, text=arti_voice_queue.CombinedOwnerQuestions(getattr(text, "lines", (text,))),
                          stardew_voice_epoch=None)
    if CONFIG.get("voice_queue_enabled", False):
        item = arti_voice_queue.wrap_trigger(trigger)
        item.enqueued_at = trigger.queued_at if trigger.owner_reply_required else time.time()
        item.name_addressed = streamer_voice.addressed(trigger.text, CONFIG) if not CONFIG.get("mic_priority_mark") else bool(item.owner_marked)
        voice_trigger_buffer.enqueue(item)
    else:
        _enqueue_legacy_voice(trigger)


def _revoked_host_reply(trigger, stories):
    """Suppress stale HOST output and retry its unrelated owner input once."""
    if not any(not story.is_current() for story in stories):
        return False
    if (trigger.viewer_name is None and
            (trigger.trigger_type in arti_voice_queue.DIRECT_OWNER_KINDS
             or getattr(trigger, "owner_reply_required", False))
            and not getattr(trigger, "host_story_retry", False)
            and speech_control.allows(speech_turn_epoch.get())):
        text = arti_voice_queue.CombinedOwnerQuestions(getattr(trigger.text, "lines", (str(trigger.text),)))
        _retain_interrupted_owner(replace(trigger, text=text, owner_reply_required=True, host_story_retry=True))
    return True


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
_current_tts_text = ""

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

    Wraps `websockets.serve(subtitle_server.handler, subtitle_server.bind_host(CONFIG), port)` so that
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
        host = subtitle_server.bind_host(CONFIG)  # owner 2026-11-19: this machine only, unless one key opens it
        server = await websockets.serve(subtitle_server.handler, host, port)
        subtitle_runtime.server_started = True
        print(f"[SubTitle] In-process server bound to ws://{host}:{port}")
        if host != subtitle_server.LOOPBACK_HOST:
            print(f"[SubTitle] Listening on ALL network interfaces: {subtitle_server.ALL_INTERFACES_KEY} is on")
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
    """Penonton nambah (spek Bohan 2026-08-03: "[recorded remark omitted]"): bangunkan proaktif + bahan sapaan. Angka penonton
    dilarang disebut Arti (bisa meleset & terdengar sistemik)."""
    global _yt_viewer_count, _last_human_activity_ts
    global _viewer_join_note, _viewer_join_note_ts
    _yt_viewer_count = count
    _last_human_activity_ts = time.time()
    _viewer_join_note = (
        f"Telemetri penonton naik ke {count}: hanya jumlah; nama tidak diketahui. "
        "Kalau menyapa, sapa umum saja; jangan menebak siapa yang masuk "
        "atau mengaitkan jumlah ini dengan nama dari ingatan/chat. "
        "TANPA menyebut angka penonton, sistem, atau hitungan apa pun."
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

# Misi yang streamer kasih ke Arti (sebuah tujuan bebas) — teks bebas
# yang menyetir narasi & aksinya selama main. "" = main bebas.
_minecraft_goal = ""
_minecraft_goal_ts = 0.0
# Misi arah-tetap ("survive"): tidak punya garis finis, jadi goal_done TIDAK
# berlaku — kalau berlaku dia bisa merasa "aku selamat!" lalu meninggalkan
# dunia di tengah siaran.
_minecraft_goal_terus = False
# Sidik jari darah+perut pada giliran TERAKHIR yang memakai konteks Minecraft.
# () = belum pernah, jadi giliran pertama selalu menyebutkannya. Live
# 2026-08-06: tanpa ini Arti membacakan kondisinya di 12 dari 20 jawaban —
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
# Terjadi betulan di uji live 2026-08-07 (papan lalu peti, jeda 4 dtk).
_craft_panel_timer: threading.Timer | None = None
_craft_panel_warned = False
# Momen "[recorded remark omitted]". Satu tampilan pada satu waktu; timer penutupnya disimpan
# supaya permintaan baru membatalkan yang lama, bukan menumpuk dua penutup
# yang saling mendahului.
_invsee_timer: threading.Timer | None = None
_invsee_warned = False

# MODE SESI: Bohan AFK & Arti pegang siaran? (spek 2026-08-04). Dipasangkan
# dengan "[recorded remark omitted]" -> 4 mode di arti_session_mode.
_host_mode = False
# Bohan pamit AFK (terdeteksi dari omongannya) tapi belum benar-benar hening —
# jaring pengaman menunggu sekian detik sebelum mengambil alih sendiri.
_afk_armed_ts = 0.0


def _stardew_live() -> bool:
    """Stardew lagi dimainkan: runtime bridge hidup ATAU siklus Live7 operator
    aktif — definisi pagar dua-driver (cache beberapa detik di guard)."""
    try:
        import arti_stardew_driver_guard
        return arti_stardew_driver_guard.stardew_live(
            CONFIG, runtime_running=globals().get("_stardew_runtime") is not None)
    except Exception:  # noqa: BLE001 — advisory; gagal baca = tidak live
        return False


def _game_live() -> bool:
    return arti_session_mode.game_active(
        minecraft=_mc_runner_active(), stardew=_stardew_live())


def _game_actually_playing() -> bool:
    if _mc_runner_active():
        return True
    import arti_stardew_driver_guard
    runtime = _stardew_runtime
    if getattr(runtime, "status", None) == "READY" and not runtime._host_paused():
        binding, player_id = runtime._binding_view()
        if binding.status == "bound" and not runtime.state_store.is_stale(player_id):
            return True
    return arti_stardew_driver_guard.live7_cycle_playing(CONFIG)


def _session_mode() -> str:
    return arti_session_mode.resolve_mode(_host_mode, _game_live())


def _apply_session_mode_change(reason: str, *, in_game: bool | None = None) -> None:
    """Satu pintu untuk efek samping pergantian mode (scene OBS).

    Dipanggil dari SEMUA jalur yang mengubah mode — setter host mode DAN
    start/stop runner Minecraft — supaya tidak ada jalur yang lupa pindah
    scene. Non-blocking: OBS lemot tidak boleh menahan siaran.

    `in_game` = paksa nilainya, dipakai jalur JOIN. Alasannya (bug live
    2026-08-05, log "[recorded remark omitted]"):
    `runner.start()` cuma menyalakan thread manajer — proses bot belum ada
    saat baris berikutnya jalan, jadi `is_active()` masih False dan mode
    terbaca `duet`. Akibat nyatanya scene OBS pindah ke scene YANG SALAH
    persis saat Arti masuk game.
    """
    mode = arti_session_mode.resolve_mode(
        _host_mode, _game_live() if in_game is None else in_game
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
    (Arti sendiri yang bilang "oke aku pegang" lewat tag, atau Bohan barusan
    bersuara), supaya tidak dobel bicara.
    """
    global _host_mode, _afk_armed_ts
    on = bool(on)
    if on and not CONFIG.get("host_mode_enabled", True):
        print("[Host] host_mode_enabled=False — diabaikan")
        return
    # Pelucut jaring DI LUAR guard di bawah: `host off` saat sudah OFF tetap
    # harus membatalkan jaring yang terlanjur terpasang (audit 2026-08-05 —
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
            "dan kalau perlu celetukin satu hal paling seru/ngeselin selama dia "
            "pergi — bukan laporan. Jangan bertele-tele."
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
        # Bohan bicara lagi TANPA pamit = jaring dibatalkan. Tanpa pelucut ini
        # "[recorded remark omitted]" tetap membuat Arti mengambil alih
        # 2 menit kemudian (audit 2026-08-05).
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

    Spek Bohan 2026-08-04: "[recorded remark omitted]" — jadi misi tuntas = KELUAR dari game, balik
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
    # Masuk TANPA misi = dulu dia main tanpa arah (roam kosong). Spek Bohan
    # 2026-08-08: busur bawaannya "[recorded remark omitted]" — misi arah-tetap
    # (mengandung kata "survive" -> misi_tanpa_finis -> goal_done terpagari),
    # jadi dia membawa gameplay-nya sendiri tanpa disuruh. Bohan tetap bisa
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
                # Chat in-game Bohan -> Arti benar-benar MENJAWAB, bukan cuma
                # mengingat. Tanpa ini mabar pincang: dia mengetik, Arti diam.
                # Sejak 16 Agu hook menerima nama opsional: pemain LAIN (teman
                # mabar) lewat jalur sendiri supaya atribusinya benar — chat
                # teman tidak boleh menyamar jadi "(Bohan ngetik...)".
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
            # alamatnya baru berguna, dan bot sering dinyalakan jauh
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
    # audit 2026-08-05 menemukan `mc off` sesudah deadman tidak membetulkan
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


# Refleks instan (spek Bohan 2026-08-05: "[recorded remark omitted]"). Semua
# init literal deterministik — snapshot konstanta modul.
_reflex_limiter = None
_reflex_note = ""
_reflex_note_ts = 0.0
_reflex_missing_warned = False
# SATU pintu untuk semua yang berbunyi. `sd.play` memakai stream global, jadi
# siapa pun yang bunyi belakangan MEMOTONG yang sedang jalan. Live 2026-08-05
# malam: refleks bunyi 3x di tengah satu kalimat panjang ("kepotong-potong",
# keluhan Bohan) karena pagar lama cuma melihat `tts_is_playing` — padahal
# selama ~10 detik Arti mikir + mensintesis, flag itu masih mati.
# Aturan (permintaan Bohan): yang datang saat ada yang belum selesai = SKIP,
# bukan memotong. Pengecualian: TTS (isi utama) MENUNGGU sebentar, karena
# refleks paling lama ~0,75 dtk.
_audio_lock = threading.Lock()
# GERBANG operasi sounddevice (2026-08-10). Kotak hitam faulthandler
# menangkap pelaku 3x crash 0xc0000005/ntdll: DUA thread refleks eskalasi
# bersamaan — satu di sd.stop()/close(), satu lagi menyusul — merebut SATU
# stream global PortAudio (sd.play/stop modul-level TIDAK thread-safe untuk
# stop/close beruntun; kematian beruntun = dua eskalasi dalam <1 dtk).
# Aturan: SEMUA pemanggilan sd.play() dan sd.stop() lewat gerbang ini.
# Poll playback TIDAK menahan gerbang selama menunggu; sd.stop() thread
# lain tetap bisa menginterupsi. Pemeriksaan/close stream memakai gerbang.
_sd_gerbang = threading.Lock()


def _wait_for_playback(duration: float) -> bool:
    """Bound playback without stranding an executor thread in sd.wait()."""
    deadline = time.monotonic() + duration + 5.0
    with _sd_gerbang:
        stream = sd.get_stream()
    while True:
        with _sd_gerbang:
            if stream.closed or not stream.active:
                stream.close()
                return True
        if time.monotonic() >= deadline:
            print(f"[Audio] playback watchdog: clip {duration:.2f}s + 5s — stop")
            try:
                with _sd_gerbang:
                    sd.stop()
            except Exception as e:
                print(f"[Audio] Watchdog stop gagal: {type(e).__name__}: {e}")
            return False
        time.sleep(0.05)


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
    if speech_control.held or not CONFIG.get("reflex_enabled", True):
        return
    category = arti_reflex.reflex_for_event(ev)
    if not category:
        return
    # Selagi Arti BICARA: diam total, kalimatnya yang menang — KECUALI
    # kematian. Aturan Bohan 2026-08-07: "[recorded remark omitted]". Mati membuat kalimat yang sedang diucapkan
    # tidak relevan lagi, jadi itu satu-satunya yang boleh menyela.
    if _reflex_limiter is None:
        _reflex_limiter = arti_reflex.ReflexLimiter()
    now = time.time()
    # Boleh menyela kalau: (a) kematian, atau (b) ancamannya BARU — bukan
    # pukulan dari penyerang yang itu-itu lagi. Aturan Bohan 2026-08-07:
    # "[recorded remark omitted]".
    _eskalasi = (arti_reflex.boleh_memotong(category)
                 or arti_reflex.sumber_baru(ev, _reflex_limiter, now))
    if tts_is_playing and not _eskalasi:
        return
    # Selagi Arti MIKIR: boleh, dan justru inilah gunanya — spek Bohan
    # 2026-08-05 "[recorded remark omitted]". Tapi
    # CUKUP SATU per giliran: di log malam2 dia dipukul beruntun dan refleks
    # bunyi 3x di dalam satu kalimat yang sedang disusun ("kepotong-potong").
    # Pukulan kedua tidak butuh teriakan baru — kalimatnya sudah di jalan.
    if _brain_busy and _brain_busy_since > 0.0:
        if _reflex_limiter.last_ts >= _brain_busy_since and not _eskalasi:
            return
        # Sudah MULAI bicara di giliran ini -> diam sampai giliran selesai.
        # `tts_is_playing` saja TIDAK cukup: jawaban diputar per KALIMAT, jadi
        # di antara dua kalimat flag itu mati dan gerbang audio bebas. Bohan
        # 2026-08-07: "[recorded remark omitted]"
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
    """Refleks sebagai giliran bicara MINI (spek Bohan 2026-08-05).

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
            # MENUNGGU sebentar, tidak memotong (aturan Bohan 2026-08-07:
            # "[recorded remark omitted]"). Pemegang yang
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
            if speech_control.held:
                return
            sd.play(data, samplerate, device=device)
        _wait_for_playback(len(data) / samplerate)
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


# Pamit atas permintaan Bohan (T3-26, smoke 2026-10-24: Arti pamit ke SATU
# nama lalu mengajak ngobrol lagi, inisiatif menyusul buka topik teknis).
# MODEL yang memilih menutup: tag [TUTUP] di jawabannya. Program hanya
# menahan inisiatif/curious selama farewell_quiet_sec; ucapan Bohan
# berikutnya mencabutnya. Tag dari giliran penonton tidak berlaku.
_FAREWELL_TAG_RE = re.compile(r"\[\s*TUTUP\s*\]", re.IGNORECASE)
_farewell_quiet_until = 0.0


def _farewell_quiet_active(now: float | None = None) -> bool:
    return (now if now is not None else time.time()) < _farewell_quiet_until


def _apply_farewell_tag(
    reply: str, trigger_type: str, viewer_name: str | None, is_owner: bool
) -> str:
    """Buang tag [TUTUP]; dari giliran Bohan (termasuk mic_pasif) tahan proaktif."""
    global _farewell_quiet_until
    if not reply or not _FAREWELL_TAG_RE.search(reply):
        return reply
    clean = " ".join(_FAREWELL_TAG_RE.sub(" ", reply).split())
    owner_voice = trigger_type == "mic_pasif" and viewer_name is None
    # Two passive-mic goodbyes a few seconds apart would be two goodbyes in a row.
    # A second passive-mic goodbye inside farewell_repeat_sec is not spoken; a PTT turn is the owner's own press.
    if CONFIG.get("live_voice_fixes_v1") and owner_voice and 0 <= time.time() - float(getattr(
            _apply_farewell_tag, "spoken_at", -1e9)) < float(CONFIG.get("farewell_repeat_sec", 90.0)):
        print("[Farewell] Pamit kedua berturut-turut tidak diucapkan")
        return ""
    if is_owner or owner_voice:
        if CONFIG.get("live_voice_fixes_v1"):
            _apply_farewell_tag.spoken_at = time.time()
        quiet = float(CONFIG.get("farewell_quiet_sec", 600.0) or 0.0)
        _farewell_quiet_until = time.time() + quiet
        print(f"[Farewell] Arti menutup obrolan — proaktif ditahan {quiet:.0f}s "
              "sampai Bohan bersuara lagi")
    else:
        print(f"[Farewell] Tag diabaikan — bukan dari Bohan "
              f"(trigger={trigger_type}, viewer={viewer_name})")
    return clean


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
    reply = _apply_farewell_tag(reply, trigger_type, viewer_name, is_owner)

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
        if trigger_type == "mic_pasif":
            # Ucapan tanpa PTT (mungkin suara game) tidak menggerakkan Arti.
            print(f"[Minecraft] Tag '{cmd.get('cmd')}' diabaikan — giliran mic_pasif")
            continue
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

    Keluhan Bohan 2026-08-05 malam: "[recorded remark omitted]" -> kematian menyapu SEMUA reaksi game yang belum terjawab.
    Diperluas 2026-08-10 ("[recorded remark omitted]"): SETIAP reaksi game baru menyapu reaksi game biasa yang
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

    Live 2026-08-05 malam: Bohan memukulinya beruntun, antrean game menumpuk,
    dan tiap giliran makan ~10 dtk — komentar "[recorded remark omitted]" keluar dua
    menit sesudah zombie-nya mati. Itu yang dia rasakan sebagai "[recorded remark omitted]". Hanya berlaku untuk `game`: omongan manusia & donasi tidak
    pernah kadaluwarsa.
    """
    if getattr(trigger.text, "_host_story_guarded", False):
        return not CONFIG.get("live7_moments_enabled", False) or not trigger.text.is_current()
    kind = getattr(trigger, "trigger_type", None)
    if kind != "game":
        return kind in _GAME_SESSION_LANE and _live7_game_session_stopped()
    if _live7_game_session_stopped():
        return True
    if getattr(trigger.text, "_live7_status", False):
        return (not CONFIG.get("live7_status_facts_enabled", True)
                or not trigger.text.is_current())
    if getattr(trigger.text, "_live7_moment", False):
        # Live7 moments are independent of Minecraft; keep queued speech short-lived.
        return (not CONFIG.get("live7_moments_enabled", False)
                or (_live7_help_relay is not None
                    and not _live7_help_relay.speech_is_current(trigger.queued_at))
                # Bed (owner 2026-10-24: "[recorded remark omitted]")
                # may wait behind a long turn instead of being culled.
                or time.time() - trigger.queued_at
                > (300.0 if getattr(trigger.text, "_live7_hold", False) else 28.0))
    # Keluhan Bohan 2026-08-09 siang: "[recorded remark omitted]". Reaksi game tanpa game = selalu basi.
    help_trigger = (CONFIG.get("live7_help_enabled", False)
                    and trigger.text.startswith("[Stardew] Aku buntu:"))
    if help_trigger and (not CONFIG.get("live7_help_speak_enabled", True)
                         or (_live7_help_relay is not None and not
                             _live7_help_relay.speech_is_current(trigger.queued_at))):
        return True
    if not help_trigger and not _mc_runner_active():
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
# first-person sekejap). Kalau Bohan menekan F5 manual, tebakan ini bisa
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
    pw_path = os.path.join(SERVER_DIR, ".rcon_password")
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
    # sejauh itu — terukur di live 21.54: pancingan keluar tapi takdir tidak
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

    Keluhan Bohan 2026-08-09: "[recorded remark omitted]". Antara mati dan kunci-ulang di
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
    bagus — tapi efek VISUALNYA hanya bisa dikonfirmasi mata Bohan. Gagal
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
                      "enable-rcon=true? .rcon_password ada?")
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
    """Sesekali berhenti dan pamerkan isi tas (permintaan Bohan 2026-08-09).

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
    """Bohan ngetik di chat Minecraft -> antre giliran bicara.

    trigger_type "mc_chat" dan BUKAN "game": tipe game sengaja dibuang saat
    Arti sibuk (reaksi basi tidak layak antre), sedangkan pertanyaan langsung
    dari Bohan tidak boleh hilang. "mc_chat" juga dihitung sebagai giliran
    PEMILIK — pengirimnya sudah disaring `minecraft_streamer_name` di runner,
    jadi ini memang dia, dan dia harus bisa menyuruh keluar/ganti misi lewat
    chat sama seperti lewat mic.
    """
    print(f"[Minecraft] Bohan ngetik: {teks[:70]}")
    queue_voice_trigger(f"(Bohan ngetik di chat Minecraft) {teks}",
                        trigger_type="mc_chat")


def _cermin_ke_chat_game(teks: str) -> None:
    """Cerminkan balasan Arti ke chat Minecraft.

    Pemain lain di server TIDAK mendengar TTS-nya — tanpa cermin ini Arti bisu di
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
    """Chat pemain LAIN di game -> antre giliran bicara.

    Pemain lain tidak mendengar TTS Arti — chat game adalah satu-satunya
    kanal mereka. Pengirim sudah disaring runner (bukan kamera, bukan bot
    layanan, bukan '!command'). NAMA disebut di trigger supaya Arti tahu siapa
    yang ngajak ngobrol dan bisa membalas dengan nama — persona-nya memang
    "panggil nama orangnya langsung". Tag lookalike sudah dilucuti runner.
    Balasannya dicerminkan ke chat game oleh _cermin_ke_chat_game (dia bisu
    di mata teman tanpa itu).
    """
    print(f"[Minecraft] {nama} ngetik: {teks[:70]}")
    _note_viewer_presence(nama)
    # trigger_type SENGAJA BUKAN "mc_chat": tipe itu dihitung giliran PEMILIK
    # oleh is_owner_turn — teman yang ngetik "arti keluar dari game" akan
    # lolos gate lifecycle. Tipe sendiri = is_owner False = tag lifecycle/
    # goal/mode tertolak oleh gate yang sudah ada, ngobrolnya tetap jalan.
    # (Nyaris kejadian 16 Agu; diselamatkan test_only_bohan_is_answered.)
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
    # momen "[recorded remark omitted]" itu dua fitur berbeda: mematikan panel tidak boleh
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
    """Perintah masuk/keluar Minecraft langsung dari kalimat Bohan.

    Perintah eksplisit streamer bisa tidak pernah dijalankan. Bukan bug logika — giliran yang dipicu
    omongan streamer sengaja dirutekan ke Groq (butuh instan), dan model yang
    kepilih (llama-3.1-8b) MENGABAIKAN instruksi tag. Perintah eksplisit tidak
    boleh bergantung pada model mana yang kebetulan menang routing, jadi ini
    jaring deterministik — sejajar dengan jaring AFK.

    Aman kalau Arti juga mengeluarkan tag: start/stop sudah idempoten.
    """
    if not CONFIG.get("minecraft_enabled"):
        return
    niat = arti_session_mode.detect_minecraft_intent(text)
    # WAJIB DIPANGGIL NAMANYA (perbaikan 20 Agu). SEMUA ucapan streamer lewat
    # sini, termasuk yang PASIF (PTT mati). Log 23.29: Bohan bergumam "[recorded remark omitted]" dan "[recorded remark omitted]" — Arti tiga kali menyeret bot masuk game yang servernya bahkan
    # tidak menyala. Menyalakan bot itu aksi mahal (proses baru + ganti mode),
    # jadi ambangnya harus setinggi perintah langsung: sebut "arti".
    # Jaring AFK sengaja TIDAK diperlakukan begini — memasang jaring saat Bohan
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
    masih 429/tutup."""
    global _init_provider_fail_until
    _init_provider_fail_until = time.time() + float(
        cfg.get("initiative_provider_fail_backoff_sec", 300.0)
    )

# Penonton dari chat/donasi/member/game (nama -> ts interaksi terakhir).
# SEMUA pesan dihitung,
# bukan cuma yang men-trigger — beda dengan _last_yt_trigger_by_viewer yang
# hanya terisi di mode voice_queue (OFF secara default; ketahuan saat
# crosscheck v0.7: bahan inisiatif "[recorded remark omitted]" selamanya kosong).
_yt_viewers_seen: dict[str, float] = {}
_viewer_session_started_ts = 0.0  # set before listeners, never restored from disk


def reset_viewer_presence_session() -> None:
    """A bridge restart is a new presence session, even on the same calendar day."""
    global _viewer_session_started_ts, _yt_viewer_count
    global _viewer_join_note, _viewer_join_note_ts
    _viewer_session_started_ts = time.time()
    _yt_viewers_seen.clear()
    _yt_viewer_count = -1
    _viewer_join_note = ""
    _viewer_join_note_ts = 0.0


def _active_viewer_names() -> list[str]:
    """Recent session interactions only; profiles/memory cannot add names."""
    now = time.time()
    cutoff = max(_viewer_session_started_ts, now - 600.0)
    return [name for name, ts in list(_yt_viewers_seen.items())
            if cutoff <= ts <= now and not is_bot_viewer(name, CONFIG)]


def _note_viewer_presence(name: str | None) -> None:
    if name and name.strip() and not is_bot_viewer(name, CONFIG):
        _yt_viewers_seen[name.strip()] = time.time()


def viewer_presence_block() -> str:
    """Authoritative presence rules, appended after prompt trimming on every turn."""
    names = ", ".join(_active_viewer_names()) or "belum ada nama terkonfirmasi"
    return (
        "\n\n[KEHADIRAN PENONTON — interaksi sesi ini (chat/donasi/member/game), 10 menit terakhir]\n"
        f"Nama yang terkonfirmasi aktif dari interaksi: {names}. "
        "Ini bukan daftar semua yang sedang menonton; jangan pastikan mereka "
        "masih menonton. Hitungan penonton hanya jumlah; nama tidak diketahui. "
        "Jangan menebak nama dari jumlah/kenaikannya. "
        "Nama di memori/RAG, profil, persona/running bits, ringkasan, atau sesi "
        "lama = dari ingatan, bukan yang lagi nonton. Kalau nama itu tidak "
        "terkonfirmasi di daftar interaksi ini maupun sebagai pengirim giliran "
        "ini, hanya ceritakan sebagai kenangan ('dulu pernah ada ...'), jangan "
        "disapa atau disebut hadir/di sini; kecuali streamer/Bohan (hadir lewat mic), "
        "yang tidak boleh dianggap absen berdasarkan daftar penonton ini."
    )

# Kapan Arti terakhir selesai bicara — gate hening inisiatif juga menghormati
# ini (bukan cuma aktivitas manusia): tanpa ini, 11 detik setelah selesai jawab
# dia monolog lagi (tes live 2026-08-02). 0.0 = stabil untuk snapshot konstanta.
_last_arti_reply_ts = 0.0

# Kapan streamer terakhir BERSUARA APAPUN di mic/ketikan (termasuk pasif) —
# pagar anti-motong inisiatif: Arti tidak boleh mulai monolog selagi Bohan
# lagi cerita. (Spek final Bohan 2026-08-02: 30 dtk sejak Arti bicara DAN
# 5 dtk sejak streamer bersuara.)
_last_streamer_speech_ts = 0.0
_last_own_play_voiced_at = None  # None = no own_play voiced this process; 0 would block the first 25s.


def _streamer_baru_bicara() -> bool:
    """Streamer bicara dalam `initiative_nyambung_sec` terakhir (inisiatif nyambung)."""
    return (
        time.time() - _last_streamer_speech_ts
        <= float(CONFIG.get("initiative_nyambung_sec", 45.0))
    )


def _initiative_materials(streamer_baru_bicara: bool | None = None) -> dict:
    """Bahan topik inisiatif dari state bridge — semua best-effort.

    `streamer_baru_bicara` = nilai yang SAMA dengan yang dioper ke prompt
    (None = hitung sendiri), supaya bahan Stardew tidak dipetik lalu dibuang.
    """
    if CONFIG.get("stardew_initiative_game_first") and _stardew_live():
        return dict(stardew_live=True, stardew_note=_stardew_initiative_note(), mode=_session_mode())
    mats: dict = {"memory_bullets": [], "present_viewers": [],
                  "scouter_summary": "", "screen_hook": ""}
    try:
        with open("vault/concepts/arti_live_learnings.md", encoding="utf-8") as f:
            mats["memory_bullets"] = [
                ln for ln in f.read().splitlines() if ln.strip().startswith("- [")
                and not arti_live_safety.is_backstage(ln)
            ]
    except OSError:
        pass
    try:
        mats["present_viewers"] = _active_viewer_names()
    except Exception:  # noqa: BLE001
        pass
    s = CONFIG.get("scouter_last_result") or {}
    s = arti_live_safety.public_summary(s)
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
            # ~20 dtk), jadi tanpa band-nya keluhan "[recorded remark omitted]"
            # kembali lewat pintu belakang. Hanya MEMBACA band; yang
            # memperbaruinya cuma _append_minecraft_context (sekali per
            # giliran) supaya dua pembaca tidak saling membungkam.
            mats["minecraft_note"] = arti_minecraft.status_note(
                _minecraft_runner.last_status, _mc_vitals_band
            )
            mats["minecraft_goal"] = _minecraft_goal
    except Exception:  # noqa: BLE001
        pass
    # Stardew: fakta dari jurnal Arti sendiri, dibaca dari snapshot memori
    # (thread latar) — tanpa I/O berkas di main loop. Sapaan penonton baru dan
    # streamer yang baru bicara menang: bahannya tidak dipetik sama sekali
    # (petik = slot rasio + ring anti-ulang terpakai).
    if streamer_baru_bicara is None:
        streamer_baru_bicara = _streamer_baru_bicara()
    mats["stardew_note"] = ""
    if (not mats["minecraft_note"] and not mats["viewer_join_note"]
            and not streamer_baru_bicara):
        mats["stardew_note"] = _stardew_initiative_note()
    mats["mode"] = _session_mode()
    # Bahan tambahan KHUSUS mode siaran solo — mahal-ish, jadi hanya dirakit
    # kalau memang lagi dipakai.
    mats["vault_topic"] = ""
    mats["heard_note"] = ""
    mats["web_topic"] = ""
    # host_game tanpa minecraft_note = Stardew live: bahannya sama dengan host_chat.
    if arti_session_mode.is_host(mats["mode"]) and not mats["minecraft_note"]:
        global _host_vault_cache
        mats["vault_topic"] = _host_vault_topic()
        # Dipakai = dikosongkan, biar worker latar mengisi bahan BERIKUTNYA.
        # Tanpa ini potongan vault yang sama diangkat berulang kali.
        _host_vault_cache = ""
        mats["heard_note"] = _host_heard_note()
        mats["web_topic"] = _host_web_topic_cache
    return mats


def _stardew_initiative_note() -> str:
    """Bahan inisiatif Stardew (plan 2026-10-22) atau "".

    Kill switch `stardew_initiative_material_enabled` (default OFF). Hanya saat
    Stardew live, dan tidak selagi jendela minta-tolong Live7 terbuka (help
    pegang kanal). Cuma membaca snapshot memori arti_stardew_initiative; thread
    penyegarnya dinyalakan malas di sini (start thread, bukan I/O).
    """
    if not CONFIG.get("stardew_initiative_material_enabled", False):
        return ""
    try:
        if not _stardew_live():
            return ""
        relay = globals().get("_live7_help_relay")
        if relay is not None and relay.help_open():
            return ""
        import arti_stardew_initiative
        arti_stardew_initiative.ensure_refresher(CONFIG)
        return arti_stardew_initiative.pick_note(CONFIG)
    except Exception:  # noqa: BLE001 — bahan opsional, jangan ganggu siaran
        return ""


def _viewer_chat_waiting() -> bool:
    """Chat viewer antre atau tertahan (cuma masuk history) belum dijawab —
    curious & inisiatif tidak boleh bicara di atasnya (owner 2026-10-21)."""
    try:
        if CONFIG.get("voice_queue_enabled", False):
            queued = voice_trigger_buffer.has_yt_pending()
        else:
            with voice_trigger_queue.mutex:
                queued = any(getattr(t, "trigger_type", None) == "yt_chat"
                             for t in voice_trigger_queue.queue)
        return arti_curious.viewer_chat_waiting(
            CONFIG,
            now=time.time(),
            yt_queued=queued,
            last_viewer_chat_ts=max(list(_yt_viewers_seen.values()), default=0.0),
            last_yt_trigger_ts=_last_yt_chat_trigger_ts,
        )
    except Exception:  # noqa: BLE001 — gate advisory, jangan jatuhkan loop
        return False


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


def _rag_background_deferred() -> bool:
    """Yield optional/background RAG work to active voice turns and open breakers."""
    with _brain_busy_lock:
        if _brain_busy or tts_is_playing:
            return True
    return arti_vault_rag.embedding_breaker_open()


def _host_vault_refresh() -> str:
    """Ambil satu potongan isi vault (BLOKIR — hanya dipanggil dari thread)."""
    global _host_vault_seed_idx
    try:
        if _rag_background_deferred():
            return ""
        seed = _HOST_VAULT_SEEDS[_host_vault_seed_idx % len(_HOST_VAULT_SEEDS)]
        _host_vault_seed_idx += 1
        hits = arti_vault_rag.search(seed, CONFIG, top_k=3) or []
        for h in hits:
            # Kunci hasil search adalah "content", BUKAN "text" — salah kunci
            # bikin bahan andalan mode host mati total tanpa error sejak
            # acba120 (audit 2026-08-05).
            text = (
                (h.get("content") or h.get("text") or "").strip()
                if isinstance(h, dict) else str(h)
            )
            text = arti_vault_rag._filter_technical_sections(
                text, h.get("heading", "") if isinstance(h, dict) else "")
            text = " ".join(text.split())[:220]
            if len(text) >= 40:
                return text
    except Exception as e:  # noqa: BLE001 — bahan opsional, jangan ganggu siaran
        print(f"[Host] Bahan vault gagal ({type(e).__name__}: {e})")
    return ""


def _host_heard_note() -> str:
    """Baris terakhir yang telinga dengar — bahan "lagi muter apa nih"."""
    if CONFIG.get("stardew_initiative_game_first") and _stardew_live():
        import arti_stardew_initiative
        return arti_stardew_initiative.heard_turn_note()
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


_screen_privacy_ingress_lock = threading.Lock()


def _clear_screen_privacy_context() -> None:
    """Caller holds history_lock. Mixed caches have no reliable screen provenance."""
    global scouter_result, summarizer_result, _last_scouter_history_snapshot
    stream_history.clear()
    if CONFIG.get("live_voice_fixes_v1"):  # V6.4: the owner-line store is history too; nothing comes back
        from arti_runtime import voice_window
        voice_window.clear_owner_lines()
    voice_context.session_log.clear()  # ADR 200: lines, checkpoints and summaries; nothing comes back
    with scouter_lock:
        scouter_result = summarizer_result = None
        CONFIG.pop("scouter_last_result", None)
    _last_scouter_history_snapshot = []
    arti_screen_context.screen_ring.clear()
    arti_screen_context.watch_state = arti_screen_context.WatchState()
    arti_vision_client.reset_gerbang_layar()
    arti_curious.reset_session()
    if CONFIG.get("stream_game_notes"):
        from arti_runtime import stream_notes
        stream_notes.bridge_boundary(__import__(__name__))


def _note_screen_privacy(text: str) -> None:
    # Serialize trusted commands with the cache purge; a concurrent turn/history
    # reader either sees the old epoch or the fully cleared new one.
    with _screen_privacy_ingress_lock, history_lock:
        if CONFIG.get("voice_append_context"):
            with screen_privacy.context_boundary():
                if screen_privacy.apply_streamer_text(text or ""):
                    _clear_screen_privacy_context()
                    state = "restricted" if screen_privacy.restricted else "allowed"
                    print(f"[ScreenPrivacy] {state}; previous screen context revoked")
            return
        if screen_privacy.apply_streamer_text(text or ""):
            _clear_screen_privacy_context()
            state = "restricted" if screen_privacy.restricted else "allowed"
            print(f"[ScreenPrivacy] {state}; previous screen context revoked")


def reset_screen_privacy_session() -> None:
    """Explicit new-process/session boundary, before listeners start; never config-backed."""
    with _screen_privacy_ingress_lock, history_lock:
        if CONFIG.get("voice_append_context"):
            with screen_privacy.context_boundary():
                screen_privacy.reset_session()
                _clear_screen_privacy_context()
            return
        screen_privacy.reset_session()
        _clear_screen_privacy_context()


def add_to_history(source, message, arti_meta=None):
    if source == "Streamer":
        _note_screen_privacy(message)
        relay = globals().get("_live7_help_relay")
        if relay is not None:
            relay.note_session_owner(message)
    # Benang obrolan (16 Agu): giliran curious perlu tahu Arti barusan bilang
    # apa & sudah menagih apa — tanpa ini dia fiksasi (5 balasan beruntun
    # mengungkit hal yang sama, tes 16 Agu 14.28). catat() dijamin tak melempar.
    arti_benang.catat(str(source), arti_live_safety.public_context(str(message)))
    # Renungan (20 Agu): jawaban Arti memajukan busur mikirnya; chat yang
    # nyambung topik jadi bahan langkah depan. Busur yang tutup meninggalkan
    # kesimpulan — ditulis ke vault lewat corong lama (gerbang fakta_sudah_ada
    # ikut menjaga duplikat). catat() dijamin tak melempar.
    arti_renungan.catat(str(source), arti_live_safety.public_context(str(message)), config=CONFIG)
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
        _note_viewer_presence(viewer_name)
    elif source == "Streamer":
        _last_human_activity_ts = time.time()
        _last_streamer_speech_ts = time.time()
        # SEMUA ucapan streamer lewat sini — termasuk yang pasif (PTT mati,
        # wake word meleset, chat in-game). Jaring AFK & pengembalian mic dulu
        # cuma dipasang di queue_voice_trigger, jadi streamer yang pamit tanpa
        # menekan PTT tidak pernah memasang jaring, dan yang balik ngobrol tanpa
        # PTT tetap dianggap AFK.
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
        if CONFIG.get("live_voice_fixes_v1") and source == "Streamer":  # V6.4: kept 15 min for her window
            from arti_runtime import voice_window
            voice_window.note_owner(log_line)
        if CONFIG.get("voice_append_context"):  # ADR 200: every line, in order; her line = the spoken record
            voice_context.note_history(source, message, timestamp, arti_meta)
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
LOCK_DIR = (str(Path(os.environ["ARTI_REHEARSAL_CONFIG"]).resolve().parent / "locks")
            if os.environ.get("ARTI_REHEARSAL_CONFIG") else
            os.path.join(os.path.expanduser("~"), ".hermes-locks"))
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

_memory_save_tasks: set[asyncio.Task] = set()


def save_long_term_memory(fact):
    if arti_live_safety.is_backstage(fact):
        return
    # Sync history/scouter callbacks also reach here from the reactor. Keep the
    # complete lock/write transaction on a worker, not just the lock acquisition.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(arti_executors.run("background", save_long_term_memory, fact))
        _memory_save_tasks.add(task)
        task.add_done_callback(_memory_save_tasks.discard)
        return task
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

        sid = session_transcript.current_session_id(CONFIG) or ""
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

    # 0 = TUNGGU SAMPAI TUNTAS (default; permintaan Bohan 2026-08-02: "[recorded remark omitted]").
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


def _record_session_start_telemetry(config: dict) -> None:
    """Bind telemetry to this session and emit one content-free startup row."""
    import arti_api_telemetry as tel

    session_id = session_transcript.current_session_id(config) or ""
    tel.set_session_id(session_id)
    youtube_video_id = (
        (config.get("youtube_video_id") or "").strip()
        if config.get("youtube_chat_enabled")
        else ""
    )
    if youtube_video_id == "YOUR_VIDEO_ID":
        youtube_video_id = ""
    try:
        supertonic_pkg_version = version("supertonic")
    except PackageNotFoundError:
        supertonic_pkg_version = None
    except Exception:
        supertonic_pkg_version = None

    extra = {
        "started_at": datetime.now().astimezone().isoformat(),
        "youtube_video_id": youtube_video_id or None,
        "tts_engine": config.get("tts_engine"),
        "supertonic_voice": config.get("supertonic_voice"),
        "supertonic_speed": config.get("supertonic_speed"),
        "supertonic_total_steps": config.get("supertonic_total_steps"),
        "supertonic_pitch_semitone": config.get("supertonic_pitch_semitone"),
        "supertonic_range_factor": config.get("supertonic_range_factor"),
        "voice_polish_active": arti_voice_dsp.aktif(config),
        "supertonic_pkg_version": supertonic_pkg_version,
        "tts_voice": config.get("tts_voice"),
    }
    tel.record_call(
        subsystem="session",
        provider="bridge",
        model="session_start",
        latency_ms=0,
        extra=extra,
        config=config,
    )

# ==========================================
# 1. KONEKSI & KONTROL VTUBE STUDIO API
# ==========================================
vts = None  # Global VTS instance — dipakai bridge, idle animation, mouse follow

class VTSController:
    def __init__(self):
        self.websocket = None
        self.auth_token = None
        self.port = int(CONFIG["vts_api_port"])
        self.token_file = arti_vts_auth.token_path(__file__, self.port)
        self._auth_ready = threading.Event()  # Read by the separate idle loop/thread.
        self._connecting = False
        self._reconnecting = False
        self._reconnect_task = None
        self._reconnect_delay = 5.0
        self._model_warning = False
        self._ws_send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task = None
        self._reader_stop = False
        # Pelajaran live 11,5 jam 2026-08-01: koneksi utama putus ~1 jam masuk,
        # semua kirim ekspresi ditelan tanpa log, model nyangkut di 'mikir' 10 jam.
        # Idle selamat karena punya reconnect sendiri — sekarang jalur utama juga.
        self._conn_lost = False
        self._last_reconnect_attempt = 0.0

        if CONFIG.get("vts_enabled", True):
            self.auth_token = arti_vts_auth.read_token(__file__, self.port, main_port=self.port)

    async def _reader_loop(self):
        """Route VTS responses by requestID — nod inject tidak lagi merusak recv ekspresi."""
        ws = self.websocket
        while ws and not self._reader_stop:
            try:
                raw = await ws.recv()
                data = json.loads(raw)
                if (data.get("data") or {}).get("errorID") in (8, 50, 100):
                    if not self._conn_lost:
                        print("[VTS] Autentikasi utama ditolak — meminta izin ulang saat reconnect.")
                    self._conn_lost = True
                    self.auth_token = None
                    if hasattr(self, "_auth_ready"):
                        self._auth_ready.clear()
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
                    if hasattr(self, "_auth_ready"):
                        self._auth_ready.clear()
                    print(
                        f"[VTS] Koneksi utama putus (reader: {type(e).__name__}) — "
                        "auto-reconnect di transisi ekspresi berikutnya"
                    )
                break

    async def connect(self):
        if self._connecting:
            return
        self._connecting = True
        self._auth_ready.clear()
        self._conn_lost = True
        uri = f"ws://localhost:{self.port}"
        try:
            self.websocket = await websockets.connect(uri)
            print(f"[VTS] Terhubung ke VTube Studio API di port {self.port}")
            self._reader_stop = False
            self._reader_task = asyncio.create_task(self._reader_loop())
            await self.authenticate()
            expected = CONFIG.get("vts_expected_model_name", "")
            if expected:
                res = await self.send_request("CurrentModelRequest")
                if not arti_vts_auth.model_matches(res, expected):
                    if not self._model_warning:
                        print(f"[VTS] MODEL GUARD: port {self.port} bukan model {expected!r}; kontrol DIBLOKIR. Periksa port ARTI/HOST!")
                        self._model_warning = True
                    return
            self._model_warning = False
            self._conn_lost = False
            self._auth_ready.set()
        except Exception as e:
            print(f"[VTS Error] Gagal connect/auth ({type(e).__name__}): {e}")
        finally:
            self._connecting = False
            if self._conn_lost:
                await self._close_socket()
                self._last_reconnect_attempt = time.monotonic()

    def start_reconnect(self):
        """One background caller of the existing reconnect/throttle path."""
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._maintain_connection())

    async def _maintain_connection(self):
        while True:
            await asyncio.sleep(min(1.0, self._reconnect_delay))
            if self.websocket is None or self._conn_lost:
                await self.ensure_connected()

    async def ensure_connected(self) -> bool:
        """Auto-reconnect koneksi utama; dipanggil tiap transisi ekspresi.

        Backoff 5→60 detik: kalau VTS benar-benar mati, jangan banjiri percobaan.
        Sesudah reconnect
        sukses, state machine ekspresi memulihkan diri sendiri (tiap transisi
        mematikan overlay lain, jadi overlay yang nyangkut ikut bersih).
        """
        if not CONFIG.get("vts_enabled", True):
            return False
        if getattr(self, "_connecting", False) or getattr(self, "_reconnecting", False):
            return False
        if self.websocket is not None and not self._conn_lost:
            return True
        retry_task = getattr(self, "_reconnect_task", None)
        if retry_task is not None and not retry_task.done() and retry_task is not asyncio.current_task():
            return False  # Voice/expression callers never wait for a 60-second ALLOW.
        now = time.monotonic()
        if now - self._last_reconnect_attempt < getattr(self, "_reconnect_delay", 5.0):
            return False
        self._last_reconnect_attempt = now
        print("[VTS] Koneksi utama putus — mencoba reconnect...")
        self._reconnecting = True
        try:
            try:
                await self.close(_for_reconnect=True)
            except Exception:
                pass
            await self.connect()
        finally:
            self._reconnecting = False
        ok = self.websocket is not None and not self._conn_lost
        self._reconnect_delay = 5.0 if ok else min(getattr(self, "_reconnect_delay", 5.0) * 2, 60.0)
        print(
            "[VTS] Reconnect utama SUKSES — ekspresi hidup lagi"
            if ok
            else f"[VTS] Reconnect utama gagal — dicoba lagi >={self._reconnect_delay:g} dtk"
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
        try:
            async with self._ws_send_lock:
                await self.websocket.send(json.dumps(payload))
            response = await asyncio.wait_for(fut, timeout=timeout)
            arti_vts_auth.response_data(response)
            return response
        finally:
            self._pending.pop(rid, None)

    async def authenticate(self):
        plugin = {
            "pluginName": CONFIG["vts_plugin_name"],
            "pluginDeveloper": CONFIG["vts_developer"],
        }
        # At most one replacement per attempt; never delete a saved token.
        for attempt in range(2):
            if not self.auth_token:
                print(f"[VTS] Klik ALLOW di VTube Studio ARTI (port {self.port}); menunggu hingga 60 detik.")
                res = await self.send_request("AuthenticationTokenRequest", plugin, timeout=60.0)
                self.auth_token = arti_vts_auth.response_data(res).get("authenticationToken")
                if not self.auth_token:
                    raise RuntimeError("VTS token response missing authenticationToken")
            try:
                res = await self.send_request("AuthenticationRequest", {
                    **plugin, "authenticationToken": self.auth_token,
                })
                authenticated = arti_vts_auth.response_data(res).get("authenticated")
                if authenticated is True:
                    self.token_file.write_text(self.auth_token, encoding="utf-8")
                    print("[VTS] Autentikasi Plugin SUKSES!")
                    return
                if authenticated is not False:
                    raise RuntimeError("VTS auth response missing authenticated")
            except arti_vts_auth.VTSAPIError as e:
                if e.error_id not in (8, 50, 100):
                    raise
            self.auth_token = None
            if attempt == 0:
                print("[VTS] Token ditolak/usang; meminta token baru.")
        raise RuntimeError("VTS authentication rejected replacement token")

    async def close(self, *, _for_reconnect=False):
        if not _for_reconnect:
            task = getattr(self, "_reconnect_task", None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._reconnect_task = None
        await self._close_socket()

    async def _close_socket(self):
        if hasattr(self, "_auth_ready"):
            self._auth_ready.clear()
        self._reader_stop = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

    def _can_drive(self):
        ready = getattr(self, "_auth_ready", None)
        return self.websocket is not None and (ready is None or ready.is_set())

    async def create_custom_parameter(self, name, min_val=-1, max_val=1, default_val=0):
        """Bikin custom tracking parameter di VTS."""
        if not self._can_drive():
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
        if not self._can_drive():
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
            if hasattr(self, "_auth_ready"):
                self._auth_ready.clear()

    async def send_expression(self, expr_file, active, *, confirm=False,
                              fade: float = 0.0):
        """Toggle ekspresi VTS; confirm=True tunggu ACK (mikir/bicara/lampu).

        `fade` (detik) memakai `fadeTime` milik VTS supaya ekspresi MELELEH,
        bukan dipotong. Ditambahkan 27 Agu: linger emosi menahan mood lalu
        mematikannya seketika, dan Bohan melihatnya sebagai "[recorded remark omitted]" — memang, karena yang dibuat cuma penundaan, bukan peredupan.
        VTS mendokumentasikan rentang 0-2 detik, jadi dijepit di 2.
        """
        if not self._can_drive():
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
                    if hasattr(self, "_auth_ready"):
                        self._auth_ready.clear()
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
# Moved verbatim to arti_runtime/speech_timing.py; the bare names stay here so callers and arti_bridge.<name> patches still meet.
from arti_runtime.speech_timing import resample_audio, HNS_PER_SECOND, _parse_word_boundary  # noqa: E402


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


_supertone_preflight_failed = False


def _supertone_preflight() -> bool:
    """Check the interpreter without spawning the model, including wizard skip."""
    global _supertone_preflight_failed
    try:
        _resolve_venv312_python()
        return True
    except FileNotFoundError as e:
        if not _supertone_preflight_failed:
            print(f"  [FAIL] Supertone: {e}. Siapkan venv312 + requirements-supertone.txt sebelum stream!")
            _supertone_preflight_failed = True
        return False


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
        # Never overwrite a stale/live handle while it still owns the model.
        await self._kill_and_wait_locked()
        self.proc = None
        py = _resolve_venv312_python()        # raises FileNotFoundError if missing
        engine_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "supertone_engine.py"
        )
        # Flag CUDA diteruskan lewat env, bukan argv: supertone_engine harus menyiapkan
        # PATH DLL-nya SEBELUM onnxruntime di-import, jadi keputusannya harus sudah
        # diketahui sejak proses lahir. Lihat supertone_engine.enable_cuda_if_requested().
        _env = os.environ.copy()
        _env["ARTI_SUPERTONE_CUDA"] = "1" if CONFIG.get("supertonic_use_cuda", False) else "0"
        if os.environ.get("ARTI_REHEARSAL_CONFIG"):
            from scripts.livepath.bridge import supertone_env
            _env = supertone_env(_env)
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
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.environ.get("ARTI_REHEARSAL_CONFIG") else {}),
        )
        self._ready = False
        # Read exactly one ready banner line; bound by READY_TIMEOUT_S so a model
        # that never finishes loading triggers fallback (Req 4.5).
        try:
            if self.proc.stderr is not None:
                threading.Thread(target=self._drain_stderr, args=(self.proc.stderr,),
                                 daemon=True, name="supertone-stderr").start()
            ready = await asyncio.wait_for(
                arti_executors.run("voice", self._read_line_blocking), timeout=READY_TIMEOUT_S
            )
            banner = json.loads(ready)
            if banner.get("type") != "ready" or not banner.get("ok"):
                raise SupertoneError(
                    banner.get("error", {"code": "MODEL_LOAD_FAILED"})
                )
        except BaseException:
            await self._kill_and_wait_locked()
            raise
        self._ready = True

    @staticmethod
    def _drain_stderr(stream):
        try:
            for line in stream:
                print(f"[Supertone stderr] {line.rstrip()[:500]}")
        except Exception as e:
            print(f"[Supertone stderr] drain stopped: {type(e).__name__}")

    async def _kill_and_wait_locked(self):
        self._ready = False
        proc = self.proc
        if proc is not None:
            def reap():
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                for name in ("stdin", "stdout", "stderr"):
                    stream = getattr(proc, name, None)
                    if stream is not None and hasattr(stream, "close"):
                        stream.close()
            await arti_executors.run("background", reap)

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
                await arti_executors.run("voice", self._write_line_blocking, line)

                # Read the single response line off the loop, bounded by timeout.
                raw = await asyncio.wait_for(
                    arti_executors.run("voice", self._read_line_blocking), timeout=timeout
                )
                resp = json.loads(raw)
                if resp.get("id") != req["id"]:
                    raise SupertoneError(
                        {"code": "DESYNC", "message": "id mismatch"}
                    )
                return resp
            except (asyncio.TimeoutError, SupertoneError, json.JSONDecodeError, OSError):
                # Timeout / EOF / DESYNC / malformed response => the subprocess is
                # no longer trustworthy. Mark it not-ready so the next speak()
                # respawns a fresh engine via ensure_alive() (Req 6.4, 9.6, 9.8).
                self._ready = False
                await self._kill_and_wait_locked()
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
                    await arti_executors.run("voice", self.proc.wait, 5)
                except Exception:
                    # Timeout or any I/O error => force-kill, no orphan (Req 10.6).
                    self.proc.kill()
            self.proc = None
            self._ready = False


# Moved verbatim to arti_runtime/speech_timing.py; the bare names stay here so callers and arti_bridge.<name> patches still meet.
from arti_runtime.speech_timing import (
    _PUNCTUATION_PHRASE,
    _PUNCTUATION_CLAUSE,
    MIN_PHRASE_CHARS,
    MAX_PHRASE_CHARS,
    _split_into_phrases,
    _estimate_phrase_timings,
)  # noqa: E402


class TTSEngine:
    def __init__(self):
        self.output_device_name = CONFIG["virtual_cable_name"]
        self._device_lock = threading.Lock()
        self._default_output_logged = False
        self.device_id = self.find_virtual_cable()
        # Task 7.1 (Req 9.1): own the single Supertone subprocess lifecycle
        # manager so its lifetime tracks the engine. Lazy spawn — no subprocess
        # is launched until the first Supertone synthesize request. This makes
        # the defensive `hasattr(self, "supertone")` guard in _acquire_supertone()
        # (task 5.2) redundant; the attribute now always exists canonically.
        self.supertone = SupertoneProcess()

    def find_virtual_cable(self):
        try:
            devices = sd.query_devices()
            name = getattr(self, "output_device_name", CONFIG["virtual_cable_name"])
            for i, dev in enumerate(devices):
                if name.lower() in dev['name'].lower() and dev['max_output_channels'] > 0:
                    print(f"[TTS] Jalur Virtual Cable ditemukan di Device ID: {i}")
                    return i
        except Exception as e:
            print(f"[TTS] Query output devices gagal: {type(e).__name__}")
        if os.environ.get("ARTI_REHEARSAL_CONFIG"):
            raise RuntimeError("Rehearsal requires virtual cable; default output forbidden")
        if not getattr(self, "_default_output_logged", False):
            print("!!! [TTS] VIRTUAL CABLE HILANG — DEFAULT OUTPUT; cek routing OBS / risiko echo mic !!!")
            self._default_output_logged = True
        return None

    @contextlib.contextmanager
    def _sd_gerbang_play_retry_device(self, data, samplerate):
        """Keep the native playback gate; retry only a rejected start."""
        with _sd_gerbang:
            self._playback_started = False
            try:
                yield
            except (sd.PortAudioError, ValueError) as e:
                if isinstance(e, ValueError) and "device" not in str(e).lower():
                    raise
                self._refresh_output_device()
                print(f"[TTS] Output device refresh; retry clip sekali (device={self.device_id})")
                # Masih di dalam `with _sd_gerbang` di atas (Lock tidak re-entrant).
                if not host_story_playback_current():
                    return
                if not _speech_playback_allowed(getattr(self, "_speech_epoch", None)):
                    return
                sd.play(data, samplerate, device=self.device_id)
            self._playback_started = True

    def _refresh_output_device(self):
        # Resolution is exceptional, not a query on every clip. setdefault also
        # supports legacy callers/tests constructing the engine via __new__.
        with self.__dict__.setdefault("_device_lock", threading.Lock()):
            self.device_id = self.find_virtual_cable()

    def _disable_supertone(self, error):
        delay = max(1.0, float(CONFIG.get("supertonic_revival_probe_sec", 600.0)))
        self._supertone_retry_at = time.monotonic() + delay
        if not getattr(self, "_supertone_fallback_logged", False):
            print(f"!!! [TTS] voice fallback to edge-tts — Supertone disabled; "
                  f"{type(error).__name__}; revival probe setelah {delay:.0f}s !!!")
            self._supertone_fallback_logged = True

    async def prewarm_supertone(self):
        try:
            await self.supertone.ensure_alive()
            print("[TTS] Supertone ready ✓")
        except Exception as e:
            self._disable_supertone(e)

    async def speak(self, text: str):
        """Dual-engine TTS entrypoint (Task 5.1).

        Reads CONFIG["tts_engine"] exactly once per utterance (Req 1.1) and
        routes to the Supertone path or the edge_tts path. Supertone failures
        before playback fall back with the SAME text at most once, then park
        synthesis until the revival probe is due. Started audio is never replayed.
        This method NEVER
        raises for any input or engine selection (Req 2.9): edge_tts failures
        are caught, logged, and swallowed (Req 2.8).
        """
        global _current_tts_text
        _current_tts_text = text
        self._last_playback_completed = False
        self._speech_epoch = speech_turn_epoch.get()
        if self._speech_epoch is None:
            self._speech_epoch = speech_control.epoch
        if not speech_control.allows(self._speech_epoch):
            return
        text = strip_tts_expression_tags(text)
        # Pagar terakhir untuk SEMUA pemanggil suara (refleks/fallback ikut).
        # Jalur balasan utama sudah menyensor lebih awal agar history dan chat
        # game juga aman; pemanggilan kedua idempoten.
        text = arti_speech_censor.censor_from_config(text, CONFIG)
        if not text:
            return
        # Req 1.1: read the configured engine once before selecting a path.
        engine = CONFIG.get("tts_engine", "edge_tts")

        if engine == "supertone" and time.monotonic() >= getattr(self, "_supertone_retry_at", 0.0):
            # Req 1.2: exact, case-sensitive "supertone" routes to Supertone first.
            try:
                self._playback_started = False
                # Task 5.2 fills in _acquire_supertone; Task 5.3 fills in _play_wav.
                with pipeline_timer.tts_telemetry("supertone", CONFIG):
                    synth_t0 = time.perf_counter()
                    wav_path, word_timings = await self._acquire_supertone(text)
                    pipeline_timer.note_tts_synth_ms(
                        int((time.perf_counter() - synth_t0) * 1000)
                    )
                    self._supertone_retry_at = 0.0
                    if getattr(self, "_supertone_fallback_logged", False):
                        print("[TTS] Supertone revival berhasil — suara utama kembali")
                        self._supertone_fallback_logged = False
                    await self._play_wav(wav_path, text, word_timings, owns_temp=True)
                return
            except Exception as e:
                if self._playback_started:
                    print(f"[TTS Error] Playback sudah dimulai ({type(e).__name__}); skip replay edge-tts")
                    return
                # Fall back only before audio starts; preserve the sentence.
                self._disable_supertone(e)
        elif engine not in ("edge_tts", "supertone"):
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
            with pipeline_timer.tts_telemetry("edge_tts", CONFIG):
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
        # Polesan suara (16 Agu 2026, resep kuping Bohan): kalau DSP aktif,
        # sintesis DIPERLAMBAT sebesar faktor pitch — resample di poles_suara
        # mengembalikan durasinya, net tetap supertonic_speed setelan Bohan.
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
            await arti_executors.run("voice", _poles, resp["wav_path"])

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
            if not host_story_playback_current():
                return

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
                    if not _speech_playback_allowed(getattr(self, "_speech_epoch", None)):
                        return
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
                    if not _speech_playback_allowed(getattr(self, "_speech_epoch", None)):
                        return
                    await _subtitle_broadcast_status("speaking", "")
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_status(speaking) failed: "
                          f"{type(e).__name__}: {e}")

            # Req 14.1, 14.4, 15.2: tts_is_playing = True is the LAST assignment
            # before sd.play. No awaitable runs between this assignment and
            # sd.play(...); bounded playback wait follows sd.play(...).
            # Req 15.3, 15.4: route to the configured virtual cable; when
            # device_id is None, sd.play falls back to the default output device.
            # Tunggu refleks yang mungkin sedang bunyi (maks ~0,75 dtk) supaya
            # TTS tidak memotongnya di tengah kata. Isi utama tidak pernah
            # di-skip — cuma menunggu sebentar. Diambil SEBELUM gate mic
            # ditutup: Req 14.4 melarang await antara `tts_is_playing = True`
            # dan `sd.play`, dan menunggu device memang urusan sebelum gate.
            # Menunggu, TIDAK memotong (aturan Bohan 2026-08-07). Batas 5 dtk
            # cuma pagar buntu — pemegang terlama yang sah adalah refleks
            # (WAV terpanjang 1,11 dtk), jadi kalau ini sampai habis berarti
            # ada yang salah dan harus KELIHATAN, bukan menimpa diam-diam.
            _got = await arti_executors.run("voice", _audio_lock.acquire, True, 5.0)
            if not _got:
                print("[TTS] Gerbang audio tidak lepas dalam 5 dtk — "
                      "tetap diputar, kemungkinan ada bunyi yang terpotong.")
            try:
                play_t0 = time.perf_counter()
                with self._sd_gerbang_play_retry_device(data, samplerate):
                    if not host_story_playback_current():
                        return
                    if not _speech_playback_allowed(getattr(self, "_speech_epoch", None)):
                        return
                    tts_play_generation += 1
                    tts_is_playing = True
                    _tts_started_ts = time.time()
                    sd.play(data, samplerate, device=self.device_id)
                completed = await arti_executors.run("voice", _wait_for_playback, len(data) / samplerate)
                self._last_playback_completed = completed is not False and not tts_stop_flag
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
            pipeline_timer.note_tts_failure(e)
            print(f"[TTS Error] Gagal memutar suara: {e}")
            if getattr(self, "_playback_started", False) and isinstance(e, sd.PortAudioError):
                self._refresh_output_device()  # repair the next clip, never replay spoken audio
        finally:
            # Berikan jeda 0.3 detik agar gema suara speaker menghilang dari mic sebelum mulai mendengarkan lagi
            # Req 14.2, 14.3: reset tts_is_playing AFTER the 0.3s post-playback
            # sleep so mic gating stays consistent across engines.
            try:
                await asyncio.sleep(0.3)
            finally:
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
                pipeline_timer.note_tts_failure(stream_err)
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
            if not host_story_playback_current():
                return
            if subtitle_runtime.enabled and subtitle_runtime.server_started and word_timings:
                try:
                    if not _speech_playback_allowed(getattr(self, "_speech_epoch", None)):
                        return
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
                    if not _speech_playback_allowed(getattr(self, "_speech_epoch", None)):
                        return
                    await _subtitle_broadcast_status("speaking", "")
                except Exception as e:
                    print(f"[SubTitle Warning] broadcast_status(speaking) failed: "
                          f"{type(e).__name__}: {e}")

            # Req 5.4: tts_is_playing = True is the LAST assignment before sd.play.
            # No awaitable runs between this assignment and sd.play(...); the
            # bounded playback wait follows sd.play(...).
            pipeline_timer.note_tts_synth_ms(
                int((time.perf_counter() - edge_t0) * 1000)
            )
            # Tunggu refleks yang mungkin sedang bunyi (maks ~0,75 dtk) supaya
            # TTS tidak memotongnya di tengah kata. Isi utama tidak pernah
            # di-skip — cuma menunggu sebentar. Diambil SEBELUM gate mic
            # ditutup: Req 5.4 melarang await antara `tts_is_playing = True`
            # dan `sd.play`, dan menunggu device memang urusan sebelum gate.
            # Menunggu, TIDAK memotong (aturan Bohan 2026-08-07). Batas 5 dtk
            # cuma pagar buntu — pemegang terlama yang sah adalah refleks
            # (WAV terpanjang 1,11 dtk), jadi kalau ini sampai habis berarti
            # ada yang salah dan harus KELIHATAN, bukan menimpa diam-diam.
            _got = await arti_executors.run("voice", _audio_lock.acquire, True, 5.0)
            if not _got:
                print("[TTS] Gerbang audio tidak lepas dalam 5 dtk — "
                      "tetap diputar, kemungkinan ada bunyi yang terpotong.")
            try:
                play_t0 = time.perf_counter()
                with self._sd_gerbang_play_retry_device(data, samplerate):
                    if not host_story_playback_current():
                        return
                    if not _speech_playback_allowed(getattr(self, "_speech_epoch", None)):
                        return
                    tts_play_generation += 1
                    tts_is_playing = True
                    _tts_started_ts = time.time()
                    sd.play(data, samplerate, device=self.device_id)
                completed = await arti_executors.run("voice", _wait_for_playback, len(data) / samplerate)
                self._last_playback_completed = completed is not False and not tts_stop_flag
                pipeline_timer.note_tts_play_ms(
                    int((time.perf_counter() - play_t0) * 1000)
                )
            finally:
                if _got:
                    _audio_lock.release()
        except Exception as e:
            pipeline_timer.note_tts_failure(e)
            print(f"[TTS Error] Gagal memutar suara: {e}")
            if getattr(self, "_playback_started", False) and isinstance(e, sd.PortAudioError):
                self._refresh_output_device()
        finally:
            # Berikan jeda 0.3 detik agar gema suara speaker menghilang dari mic sebelum mulai mendengarkan lagi
            # Req 5.5: reset tts_is_playing AFTER the post-playback sleep so mic
            # gating stays consistent with today's behavior.
            try:
                await asyncio.sleep(0.3)
            finally:
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
# Re-exported from arti_runtime/asr_text.py so bridge callers keep using these names.
from arti_runtime.asr_text import (
    _ASR_LITERAL_PHANTOMS,
    _ASR_BARE_THANKS,
    _ASR_ALWAYS_NOISE,
    _ASR_NOISE_SUBSTRINGS,
    _normalize_asr_text,
)  # noqa: E402


def is_asr_noise_transcript(text: str, audio_duration_sec: float | None = None) -> bool:
    """Halusinasi Whisper — tapi ucapan terima kasih ASLI tetap lolos."""
    if mic_hygiene.has_question_mark(text):
        return False
    if CONFIG.get("passive_mic_hygiene") and mic_hygiene.is_hallucination_transcript(
            text, prompt=arti_voice_pipeline.MIC_ASR_VOCABULARY):
        return True
    raw = text.strip()
    t = _normalize_asr_text(text)
    words = t.split()
    low = text.lower()

    # Pola spam persis dari log: hasil ASR berupa ucapan terima kasih pendek
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
    if tts_is_playing and _current_tts_text:
        heard_now = _normalize_asr_text(text)
        said_now = _normalize_asr_text(_current_tts_text)
        if heard_now and re.search(r"(?<!\w)" + re.escape(heard_now) + r"(?!\w)", said_now):
            return True
    if not last_arti_reply_text:
        return False
    heard = _normalize_asr_text(text)
    said = _normalize_asr_text(last_arti_reply_text)
    # Chunk telinga 5 dtk sering cuma nangkep AWAL kalimat Arti — fragmen
    # pendek vs jawaban panjang lolos ratio 0.7 (awal kalimat Arti pernah
    # masuk ring begitu). Potongan
    # >= 20 char yang persis ada di jawaban terakhir = echo — TAPI hanya
    # sesaat pasca-TTS (tanpa batas waktu, streamer yang MENGUTIP kalimat Arti
    # 10 menit kemudian ikut dimakan; echo fisik cuma hidup beberapa detik).
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


# Re-exported from arti_runtime/asr_text.py so bridge callers keep using these names.
from arti_runtime.asr_text import filter_whisper_hallucination  # noqa: E402

# Re-exported from arti_runtime/reply_text.py; calls inside reply_text do not see bridge rebinding.
from arti_runtime.reply_text import (
    _TTS_EXPRESSION_TAG_RE,
    _ANY_ANGLE_TAG_RE,
    strip_tts_expression_tags,
    clean_ai_reply,
)  # noqa: E402

local_whisper_model = None
_local_whisper_lock = threading.Lock()
_local_whisper_gpu_failed = False


def _get_local_whisper_model(*, force_cpu=False):
    """Serialize lazy loads and remember CUDA failures for this process."""
    global local_whisper_model, _local_whisper_gpu_failed
    with _local_whisper_lock:
        if force_cpu and not _local_whisper_gpu_failed:
            _local_whisper_gpu_failed = True
            local_whisper_model = None
        if local_whisper_model is None:
            if not _local_whisper_gpu_failed:
                try:
                    local_whisper_model = WhisperModel("small", device="cuda", compute_type="float16")
                except Exception:
                    _local_whisper_gpu_failed = True
                    print("[ASR] GPU Whisper gagal — pakai CPU untuk proses ini.")
            if local_whisper_model is None:
                local_whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        return local_whisper_model


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


_groq_asr_request_lock = threading.Lock()
_groq_asr_desktop_request_lock = threading.Lock()


def _groq_asr_with_deadline(audio_array, samplerate, url, headers, data, deadline_sec, background=False):
    """Bound the caller's wait even if the HTTP library keeps reading a slow stream."""
    # Separate bounded lanes: a slow desktop request must not evict mic ASR.
    request_lock = _groq_asr_desktop_request_lock if background else _groq_asr_request_lock
    if not request_lock.acquire(blocking=False):
        raise TimeoutError("previous Groq ASR request in this lane is still running")
    result_queue = queue.Queue(maxsize=1)

    def _request():
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_audio:
                tmp_path = tmp_audio.name
            sf.write(tmp_path, audio_array, samplerate)
            with open(tmp_path, "rb") as audio_file:
                response = arti_http_util.groq_session().post(
                    url,
                    headers=headers,
                    files={"file": ("recording.wav", audio_file, "audio/wav")},
                    data=data,
                    timeout=(3, 7),
                )
                result = (response.status_code, response.text, response.json() if response.status_code == 200 else {})
            result_queue.put((True, result))
        except Exception as exc:
            result_queue.put((False, exc))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            request_lock.release()

    worker = threading.Thread(target=_request, name="groq-asr-request", daemon=True)
    try:
        worker.start()
    except Exception:
        request_lock.release()
        raise
    worker.join(max(0.0, float(deadline_sec)))
    if worker.is_alive():
        raise TimeoutError(f"Groq ASR wall-clock deadline ({deadline_sec:g}s) exceeded")
    ok, result = result_queue.get_nowait()
    if not ok:
        raise result
    return result


def transcribe_audio(audio_array, samplerate=16000, use_groq=True, *,
                     api_key=None, language="id", quiet=False, background=False, capture_quality=False,
                     clip_audio=False):
    """ASR bersama mic + desktop audio.

    Default (mic) TIDAK berubah: kunci utama + paksa "id". Jalur desktop pass
    api_key=<pool telinga>, language=None (auto-detect), quiet=True — print
    "[recorded remark omitted]" tiap 5 dtk membanjiri terminal (pagi 2026-08-03);
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

    fallback_message = ("Skip desktop chunk." if background and not
                        CONFIG.get("desktop_audio_local_fallback_enabled", False)
                        else "Menggunakan local Whisper...")
    audio_sec = round(len(audio_array) / float(samplerate or 16000), 2)
    groq_key = api_key or CONFIG.get("groq_api_key")
    whisper_models = ["whisper-large-v3", "whisper-large-v3-turbo"]  # Rolling: 2K + 2K = 4K RPD
    if use_groq and groq_key and groq_key != "YOUR_GROQ_API_KEY" and groq_key.startswith("gsk_"):
        groq_model = whisper_models[getattr(transcribe_audio, "_widx", 0) % len(whisper_models)]
        transcribe_audio._widx = getattr(transcribe_audio, "_widx", 0) + 1
        t0 = time.perf_counter()
        try:
            headers = {
                "Authorization": f"Bearer {groq_key}"
            }
            url = "https://api.groq.com/openai/v1/audio/transcriptions"
            data = {"model": groq_model}
            if capture_quality:
                data["response_format"] = "verbose_json"
            if not background and not clip_audio:
                data["prompt"] = arti_voice_pipeline.MIC_ASR_VOCABULARY
            if language:
                data["language"] = language  # mic: paksa "id"; desktop: auto
            status_code, response_text, response_body = _groq_asr_with_deadline(
                audio_array, samplerate, url, headers, data,
                CONFIG.get("groq_asr_deadline_sec", 10.0), background,
            )

            ms = int((time.perf_counter() - t0) * 1000)
            if status_code == 200:
                text = (response_body.get("text") or "").strip()
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
                    return streamer_voice.Transcript(text, response_body.get("segments") or ()) if capture_quality else text
                _tel_asr(
                    provider="groq",
                    model=groq_model,
                    latency_ms=ms,
                    ok=True,
                    extra={"audio_sec": audio_sec, "backend": "cloud", "reason": "empty_text"},
                )
                return ""  # HTTP 200 + empty transcript is silence, not a provider failure.
            else:
                _tel_asr(
                    provider="groq",
                    model=groq_model,
                    latency_ms=ms,
                    ok=False,
                    extra={"audio_sec": audio_sec, "backend": "cloud", "http": status_code},
                )
                print(f"[ASR Warning] Groq Cloud Whisper gagal (status {status_code}): {response_text}. {fallback_message}")
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            _tel_asr(
                provider="groq",
                model=groq_model,
                latency_ms=ms,
                ok=False,
                extra={"audio_sec": audio_sec, "backend": "cloud", "error": str(e)[:120]},
            )
            print(f"[ASR Warning] Error Groq Cloud Whisper: {e}. {fallback_message}")

    # Desktop context is expendable; mic fallback remains available by default.
    if background and not CONFIG.get("desktop_audio_local_fallback_enabled", False):
        return ""

    # Fallback ke local Whisper (Lazy loading agar hemat RAM/VRAM saat startup)
    local_model = "whisper-small"
    try:
        t0_local = time.perf_counter()
        segments, _ = _get_local_whisper_model().transcribe(audio_array, beam_size=1, language=language,
            initial_prompt=None if background else arti_voice_pipeline.MIC_ASR_VOCABULARY)
        segments = list(segments)
        text = " ".join([seg.text for seg in segments]).strip()
        if capture_quality:
            text = streamer_voice.Transcript(text, [dict(no_speech_prob=getattr(s, "no_speech_prob", None),
                                                        avg_logprob=getattr(s, "avg_logprob", None)) for s in segments])
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
                local_whisper_model = _get_local_whisper_model(force_cpu=True)
                print("[ASR] Model Whisper 'small' dimuat ulang di CPU.")
                segments, _ = local_whisper_model.transcribe(audio_array, beam_size=1, language=language,
                    initial_prompt=None if background else arti_voice_pipeline.MIC_ASR_VOCABULARY)
                segments = list(segments)
                text = " ".join([seg.text for seg in segments]).strip()
                if capture_quality:
                    text = streamer_voice.Transcript(text, [dict(no_speech_prob=getattr(s, "no_speech_prob", None),
                                                                avg_logprob=getattr(s, "avg_logprob", None)) for s in segments])
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

    Alert donasi overlay bisa punya audio sendiri (jingle beberapa detik) lalu
    pesannya DIBACAKAN. Arti tidak boleh tabrakan suara.
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


_ALERT_YIELD_FACTS = {
    "donasi": "Sisa jawabanmu tadi belum terucap karena ada donasi masuk.",
    "mediashare": "Sisa jawabanmu tadi belum terucap karena ada mediashare masuk.",
}


class _SpeakChunksResult:
    """TTS loop result. Truthy matches the old voiced boolean."""

    __slots__ = ("voiced", "spoken_n", "spoken_text", "yielded")

    def __init__(self, voiced, spoken_n=0, spoken_text=None, yielded=False):
        self.voiced = bool(voiced)
        self.spoken_n = spoken_n
        self.spoken_text = spoken_text
        self.yielded = bool(yielded)

    def __bool__(self):
        return self.voiced


def _alert_yield_pending():
    return bool(CONFIG.get("alerts_yield_after_sentence") and _yield_after_sentence)


def _clear_alert_yield():
    global _yield_after_sentence, _yield_after_sentence_reason, _yield_bound_timer
    _yield_after_sentence = False
    _yield_after_sentence_reason = None
    timer = _yield_bound_timer
    _yield_bound_timer = None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001
            pass


def _commit_alert_yield():
    global _pending_alert_yield_fact
    reason = _yield_after_sentence_reason or "donasi"
    _pending_alert_yield_fact = _ALERT_YIELD_FACTS.get(
        reason, _ALERT_YIELD_FACTS["donasi"]
    )
    _clear_alert_yield()


def _arm_alert_yield_bound():
    global _yield_bound_timer
    if _yield_bound_timer is not None or not _yield_after_sentence:
        return
    sec = float(CONFIG.get("alerts_yield_max_sec", 8.0) or 8.0)

    def _fire():
        _alert_yield_bound_hit(sec)

    timer = threading.Timer(sec, _fire)
    timer.daemon = True
    _yield_bound_timer = timer
    timer.start()


def _alert_yield_bound_hit(sec):
    global tts_stop_flag, _yield_bound_timer
    _yield_bound_timer = None
    if not _yield_after_sentence:
        return
    if tts_is_playing:
        tts_stop_flag = True
        try:
            with _sd_gerbang:
                sd.stop()
        except Exception:  # noqa: BLE001
            pass
        print(f"[Alerts] yield bound hit ({sec}s) — hard stop")
        try:
            request_asr_stream_restart("alert yield bound")
        except Exception:  # noqa: BLE001
            pass


def _request_alert_yield(reason):
    global _yield_after_sentence, _yield_after_sentence_reason
    if not CONFIG.get("alerts_yield_after_sentence"):
        return
    speaking = _reply_chunks_inflight
    if tts_is_playing:
        speaking = True
    if not speaking:
        return
    _yield_after_sentence = True
    _yield_after_sentence_reason = reason
    _arm_alert_yield_bound()


def _alert_yield_fact_block():
    """One-shot fact for the next prompt render; then cleared."""
    global _pending_alert_yield_fact
    fact = _pending_alert_yield_fact
    _pending_alert_yield_fact = None
    if not fact:
        return ""
    return f"\n[FAKTA] {fact}\n"


def _apply_alert_yield_to_reply(ai_reply, speak_result):
    """Keep history/transcript to sentences that were actually spoken."""
    if not CONFIG.get("alerts_yield_after_sentence"):
        return ai_reply
    trimmed = getattr(speak_result, "spoken_text", None)
    return trimmed if isinstance(trimmed, str) else ai_reply


def schedule_donation_trigger(text: str, viewer_name: str | None, message: str, *, on_queued=None, gate_desktop: bool = True) -> None:
    """Antre trigger donasi SETELAH alert overlay selesai (timer daemon)."""
    if CONFIG.get("stream_game_notes"):
        from arti_runtime import stream_notes  # ADR 228: a donation message is a source, attributed "donasi"
        stream_notes.bridge_viewer(__import__(__name__), viewer_name, message, donation=True)
    if not getattr(text, "thanks_token", "") and CONFIG.get("thanks_once_enabled"):
        dec = thanks_once.claim_trigger(CONFIG, text, viewer_name, message)
        if not dec.is_new:
            _merge_thanks_extra(dec)
            if on_queued: on_queued()
            return
        text = thanks_once.tag_trigger(text, dec.token)
    _request_alert_yield("donasi")
    def enqueue():
        tok = getattr(text, "thanks_token", "")
        extra = thanks_once.take_added_message(tok) if tok else ""
        queued = queue_voice_trigger(thanks_once.tag_trigger(f"{text} Pesan: {extra}", tok) if extra else text,
                                     trigger_type="donation", viewer_name=viewer_name)
        if tok: thanks_once.mark_state(tok, "queued" if queued else "dropped")
        if queued and on_queued:
            on_queued()

    delay = donation_alert_delay_sec(message)
    if gate_desktop:
        _desktop_ear_gate.hold(delay + 3.0)  # alert/readout + a small audio tail
    if delay <= 0:
        enqueue()
        return
    print(f"[Donasi] Nunggu alert overlay selesai (~{delay:.0f}s) sebelum Arti bereaksi...")
    t = threading.Timer(
        delay,
        enqueue,
    )
    t.daemon = True
    t.start()


# Fitur E — state playback media share + watcher video.
_video_watcher = None
_media_playback_until = 0.0
_desktop_ear_gate = arti_desktop_audio.DesktopEarGate(CONFIG)


def _media_hold_allows(trigger) -> bool:
    """Flag off: nothing speaks during a clip. Flag on: thanks, marked owner, name-addressed."""
    try:
        until = float(_media_playback_until or 0.0)
    except (TypeError, ValueError):
        until = 0.0
    if (until <= 0 or time.time() >= until) and not (
            CONFIG.get("media_watch_mode") and chat_media.watch_held(CONFIG, time.time())):
        return True
    if not (CONFIG.get("mic_priority_mark") or CONFIG.get("media_watch_mode")):
        return False
    kind = getattr(trigger, "trigger_type", "")
    if kind in ("mic", "ptt", "wake_word", "donation"):
        return True
    if kind == "mic_pasif" and bool(getattr(trigger, "owner_marked", False)):
        return True
    text = getattr(trigger, "text", "") or ""
    return bool((kind == "mic_pasif" or (kind == "yt_chat" and not CONFIG.get("media_watch_mode")))
                and streamer_voice.addressed(text, CONFIG))


def _waiting_hold_pass() -> bool:
    if CONFIG.get("voice_queue_enabled", False):
        return any(_media_hold_allows(it) for it in voice_trigger_buffer.snapshot())
    with voice_trigger_queue.mutex:
        return any(_media_hold_allows(_normalize_voice_trigger(it)) for it in list(voice_trigger_queue.queue))


def hold_media_playback(seconds: float, *, gate_desktop: bool = True) -> None:
    """Media share mulai diputar overlay: potong TTS Arti + tahan turn baru.

    Use case Bohan: Arti lagi ngomong -> video nongol di tengah layar ->
    dia BERHENTI, nonton bareng, komentar setelah selesai.
    """
    global _media_playback_until, tts_stop_flag
    yield_soft = bool(CONFIG.get("alerts_yield_after_sentence") and not CONFIG.get("media_watch_mode"))
    if yield_soft:
        _request_alert_yield("mediashare")
    else:
        tts_stop_flag = True  # Also cancel sentences waiting in the inter-sentence gap.
    # max(): dua media share beruntun tidak boleh MEMENDEKKAN hold — audit
    # membuktikan share #2 (klip pendek) menimpa hold #1 dan Arti ngomong di
    # tengah klip pertama.
    _media_playback_until = max(
        _media_playback_until, time.time() + max(0.0, seconds)
    )
    if CONFIG.get("media_watch_mode"):
        chat_media.holds.record(time.time(), _media_playback_until)
    if gate_desktop:
        _desktop_ear_gate.hold(_media_playback_until - time.time())
    if tts_is_playing and not yield_soft:
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
    memegang doc video hasil reindex — tanpa itu RAG bisa mati lama karena
    window beruntun, dan pertanyaan soal memori dijawab tanpa ingatan.
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


def _queue_video_reaction(text, viewer) -> None:
    queue_voice_trigger(text, trigger_type="video", viewer_name=viewer)


def start_video_watcher() -> None:
    global _video_watcher
    import arti_video_watcher

    def _reindex() -> None:
        try:
            if _rag_background_deferred():
                print("[Video] Reindex ditunda: giliran suara aktif atau embedding breaker terbuka")
                return
            arti_vault_rag.reindex_all(CONFIG, verbose=False)
        except Exception as e:  # noqa: BLE001
            print(f"[Video] Reindex dokumen video gagal: {type(e).__name__}")

    _video_watcher = arti_video_watcher.VideoWatcher(
        CONFIG,
        {
            "queue_reaction": _queue_video_reaction,
            "reindex": _reindex,
            "set_qa": _video_set_qa_window,
            "transcribe_audio": transcribe_audio,
        },
    )
    _video_watcher.start()


def _merge_thanks_extra(dec) -> None:
    thanks_once.merge_pending(dec, voice_trigger_buffer, voice_trigger_queue, bool(CONFIG.get("voice_queue_enabled", False)), _normalize_voice_trigger)


def _queue_instant_donation_thanks(ev, *, thanks_token: str = "") -> None:
    """One-line thank-you, queued before a mediashare hold (VOICE_CHAT_ANALYSIS change 6)."""
    name = str(getattr(ev, "name", "") or "kamu")
    amount = str(getattr(ev, "amount_label", "") or "donasinya")
    text = thanks_once.tag_trigger(f"[DONASI] Terima kasih {name} untuk {amount}.", thanks_token)
    ok = queue_voice_trigger(text, trigger_type="donation", viewer_name=name)
    if thanks_token:
        thanks_once.mark_state(thanks_token, "queued" if ok else "dropped")


def _on_donation(ev) -> None:
    """Callback listener donasi (dipanggil dari thread arti_donations).

    History dicatat SEKARANG (konteks LLM), media share ditampung untuk Fitur
    E, reaksi suara DITUNDA sampai alert overlay selesai. Level modul —
    test AST mengunci urutan startup main_loop, nested def mengotorinya.
    """
    import arti_donations  # noqa: PLC0415

    kind = getattr(ev, "kind", "donation")
    if not thanks_once.enabled(CONFIG) and kind == "membership" and str(getattr(ev, "platform", "")).lower() in (
        "youtube", "youtube_account",
    ):
        if not arti_donations.claim_membership_alert(ev.name):
            return
    _desktop_ear_gate.hold(
        _media_alert_hold_sec(ev)
        if ev.media_url or kind == "media_points"
        else donation_alert_delay_sec(ev.message) + 3.0
    )  # reserve once at receipt, before history/memory work can block this callback
    _note_viewer_presence(ev.name)
    if CONFIG.get("live_voice_fixes_v1") and kind == "follow":  # V11 2026-11-17: like a membership alert
        return arti_donations.voice_follow(ev, add_to_history, schedule_donation_trigger)
    if kind == "membership":
        detail = (
            f" ({ev.membership_months} bulan)"
            if getattr(ev, "membership_months", 0) > 0
            else ""
        )
        print(f"[Membership] {ev.platform}: {ev.name}{detail}")
        trigger = arti_donations.format_membership_trigger(ev)
        dec = thanks_once.claim_event(CONFIG, ev)
        if not dec.is_new:
            return _merge_thanks_extra(dec)  # her history holds it once too
        if (thanks_once.enabled(CONFIG) and not thanks_once.is_gift(ev) and str(getattr(ev, "platform", "")).lower() in
                ("youtube", "youtube_account") and not arti_donations.claim_membership_alert(ev.name)):
            return  # the 30 s rule by name stays: sources may disagree on the month
        add_to_history(
            f"Viewer {ev.name} ({ev.platform_label})",
            trigger,
        )
        schedule_donation_trigger(thanks_once.tag_trigger(trigger, dec.token), ev.name, ev.message, gate_desktop=False)
        return

    if kind == "media_points":
        # Streamlabs loyalty points: KASUAL —
        # tanpa upacara terima kasih donasi; langsung nonton bareng + komentar.
        # A refusal gets a short video turn with the reason.
        print(f"[Donasi] {ev.platform}: {ev.name} media share (loyalty points)")
        add_to_history(
            f"Viewer {ev.name} ({ev.platform.title()})",
            f"[MEDIA SHARE] {ev.message or ev.media_url}",
        )
        _submit_media_job(ev, donation_label="")
        return

    print(f"[Donasi] {ev.platform}: {ev.name} kirim {ev.amount_label}!")
    dec = thanks_once.claim_event(CONFIG, ev)
    if dec.is_new:  # her history holds one event once
        add_to_history(
            f"Viewer {ev.name} ({ev.platform.title()})",
            f"[DONASI {ev.amount_label}] {ev.message or '(tanpa pesan)'}",
        )
    if not dec.is_new:
        _merge_thanks_extra(dec)
        if dec.added_clip and ev.media_url:
            arti_donations.pending_media.append(ev)
            _submit_media_job(ev, donation_label=ev.amount_label, thanks_token=dec.token)
        return
    if CONFIG.get("mic_priority_mark") and ev.media_url:
        _queue_instant_donation_thanks(ev, thanks_token=dec.token)
    if ev.media_url:
        arti_donations.pending_media.append(ev)
        if _submit_media_job(ev, donation_label=ev.amount_label, thanks_token=dec.token):
            # Reaksi DIGABUNG: terima kasih + komentar isi video SETELAH
            # playback. Terima kasih terpisah TIDAK dikirim.
            # Flag on: the one-line thank-you was already queued before this hold.
            return
        # Submit gagal (video off / antrean penuh / URL aneh): donatur BAYAR —
        # ucapan terima kasih normal tetap wajib jalan (crosscheck pra-live #2:
        # dulu jalur ini bisu total).
        print(
            f"[Donasi] Media share tidak bisa diproses — terima kasih tetap "
            f"jalan ({ev.media_url})"
        )
        if CONFIG.get("mic_priority_mark"):
            return
    schedule_donation_trigger(
        thanks_once.tag_trigger(arti_donations.format_donation_trigger(ev), dec.token),
        ev.name, ev.message, gate_desktop=False,
    )


def _submit_media_job(ev, donation_label: str, *, thanks_token: str = "") -> bool:
    """Media share -> antre job video; HOLD hanya kalau job diterima.

    Return False bila watcher mati/URL tak dikenal/ditolak guard — pemanggil
    yang memutuskan fallback (donasi berbayar: terima kasih normal).
    Urutan PENTING: submit dulu, hold belakangan — dulu hold jalan duluan dan
    job ditolak = Arti bengong 60 detik untuk video yang tak pernah diproses.
    """
    import arti_video_watcher as avw  # noqa: PLC0415

    # The desktop gate was already reserved at receipt, even if analysis rejects.
    hold = _media_alert_hold_sec(ev)
    ids = avw.extract_youtube_ids(ev.media_url or "")
    if not ids:
        avw.queue_media_refusal(_queue_video_reaction, ev.name, "tautan YouTube tidak dikenali")
        return False
    if _video_watcher is None or not CONFIG.get("video_enabled", False):
        avw.queue_media_refusal(_queue_video_reaction, ev.name, "fitur analisis video dimatikan")
        return False
    accepted = _video_watcher.submit(avw.VideoJob(
        video_id=ids[0],
        source=ev.platform,
        viewer=ev.name,
        donation_label=donation_label,
        message=ev.message,
        clip_start=ev.media_start,
        clip_end=ev.media_end,
        playback_end=time.time() + hold if CONFIG.get("media_watch_mode") else 0.0,
        thanks_token=thanks_token,
    ))
    if not accepted:
        return False
    hold_media_playback(hold, gate_desktop=False)
    return True


def _media_alert_hold_sec(ev) -> float:
    """The overlay plays its alert first, then the clip, with a small tail."""
    clip = ev.media_end - ev.media_start if ev.media_end > ev.media_start else 0
    clip = min(clip, 180)
    alert = float(CONFIG.get("donation_alert_base_sec", 5.0))
    return alert + (clip or float(CONFIG.get("mediashare_hold_sec", 60.0))) + 3.0


# Re-exported from arti_runtime/yt_paid_parse.py so bridge callers keep using these names.
from arti_runtime.yt_paid_parse import extract_membership, extract_superchat  # noqa: E402


def is_bot_viewer(viewer: str, config: dict | None = None) -> bool:
    """Bot layanan chat (leaderboard/command bot) tidak boleh mentrigger jawaban.

    Live 11,5 jam 2026-08-01: @Streamlabs memposting leaderboard "[recorded remark omitted]" dan bridge mengantri jawaban untuknya. Pesannya tetap dicatat di
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

    Sampai 27 Agu jalur chat memakai `r.get("text", ""[recorded remark omitted]"emoji": {...}}` TANPA kunci `text`, jadi dua hal terjadi
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
_yt_chat_attached_id = ""
_yt_chat_thread_started = False


def stop_youtube_chat() -> None:
    """Hentikan worker chat sebelum pipeline shutdown yang memakan waktu."""
    global _yt_chat_running
    _yt_chat_running = False


def _chat_id_channel_path(run=None):
    """RUN/youtube-video-id.txt of a mabar run; None outside a run (never a file in the checkout)."""
    if run:
        return os.path.join(str(run), "youtube-video-id.txt")
    cfg = os.environ.get("ARTI_REHEARSAL_CONFIG")
    if cfg:
        return os.path.join(os.path.dirname(os.path.abspath(cfg)), "youtube-video-id.txt")
    return None


def attach_youtube_chat(video_id, *, start_fn=None):
    """Start the YT chat reader with this id if it is new. Repeat of the same id is a no-op."""
    global _yt_chat_attached_id, _yt_chat_thread_started, _yt_chat_running
    vid = (video_id or "").strip()
    if not vid or vid == "YOUR_VIDEO_ID":
        return False
    if vid == _yt_chat_attached_id:
        return False
    if _yt_chat_thread_started:
        # The running reader keeps its own continuation; switching ids needs a voice restart.
        print(f"[YouTube Chat] chat_id_change_needs_restart {vid}")
        _yt_chat_attached_id = vid
        return False
    CONFIG["youtube_video_id"] = vid
    CONFIG["youtube_chat_enabled"] = True
    _yt_chat_attached_id = vid
    _yt_chat_running = True
    if not _yt_chat_thread_started:
        starter = start_fn if start_fn is not None else _default_start_youtube_chat
        starter()
        _yt_chat_thread_started = True
    print(f"[YouTube Chat] chat_attached {vid}")
    return True


def _default_start_youtube_chat():
    threading.Thread(target=youtube_chat_worker, daemon=True).start()
    start_yt_viewer_count_worker()


def poll_chat_id_file(*, path=None, start_fn=None):
    """Read the run's youtube-video-id.txt. No file → no change. New id → attach once."""
    channel = path or _chat_id_channel_path()
    if not channel or not os.path.isfile(channel):
        return None
    try:
        with open(channel, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    vid = raw.splitlines()[0].strip() if raw else ""
    if attach_youtube_chat(vid, start_fn=start_fn):
        return vid
    return None


def start_run_chat_id_poller(*, interval=2.0):
    """Poll the mabar run folder for a late chat id. Outside a run, or with SSN carrying chat, nothing runs."""
    if not _chat_id_channel_path() or CONFIG.get("ssn_chat_enabled"):
        return False

    def loop():
        while True:
            try:
                poll_chat_id_file()
            except Exception:
                pass
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
    return True


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
        print("!!! [Startup Checklist] [FAIL] YouTube: Video ID kosong/invalid — isi ID live saat ini! !!!")

    reused_id = bool(video_id and video_id == _previous_youtube_video_id)
    if reused_id:
        print("!!! [Startup Checklist] [FAIL] YouTube: Video ID sama dengan sesi sebelumnya — cek ID live saat ini! !!!")
    
    print(f"\n[YouTube Chat] Menghubungkan ke live chat (Video: {video_id})...")
    
    last_chat_trigger_time = 0
    last_addressed_trigger_by_viewer: dict[str, float] = {}
    seen_ids = set()
    seen_order = collections.deque()
    thanked_paid = arti_yt_paid.ThankedPaidMessages(os.path.join(_SCRIPT_DIR, "data", "yt_thanked_paid.json"))
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # Bypass consent page
    session.cookies.set("CONSENT", "CONSENT_PLACEHOLDER", domain=".youtube.com")
    session.cookies.set("SOCS", "SOCS_PLACEHOLDER", domain=".youtube.com")
    
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
                renderer = data.get('contents', {}).get('liveChatRenderer', {})
                if not renderer or renderer.get('isReplay'):
                    return None, []
                
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
                        msg = parse_action(a, initial=True)
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
    
    def parse_action(action, *, initial=False):
        """Parse satu chat action menjadi dict {name, message}"""
        item = action.get('addChatItemAction', {}).get('item', {})
        superchat = extract_superchat(item)
        membership = extract_membership(item)
        renderer = (item.get('liveChatTextMessageRenderer')
                    or item.get('liveChatPaidMessageRenderer')
                    or item.get('liveChatPaidStickerRenderer')
                    or (item.get(membership['renderer_key']) if membership and membership.get('renderer_key') else None))
        if not renderer: return None

        msg_id = renderer.get('id', '') or (membership or {}).get('id') or ''
        if msg_id in seen_ids: return None
        seen_ids.add(msg_id)
        seen_order.append(msg_id)

        # Keep the latest 5000 IDs, evicting the oldest deterministically.
        if len(seen_order) > 5000:
            seen_ids.discard(seen_order.popleft())
        if superchat and thanked_paid.contains(video_id, msg_id):
            return None

        # Only initial/reconnect pages are backlog. Live polls use dedupe,
        # never YouTube-vs-PC wall-clock comparison (which can lose paid chat).
        backlog_paid = False
        if initial:
            now = time.time()
            try:
                sent_ts = int(renderer.get('timestampUsec', '')) / 1_000_000
                current = max(_viewer_session_started_ts, now - 600.0) <= sent_ts <= now + 120.0
            except (TypeError, ValueError):
                current = False
            if not current:
                if not superchat or not superchat.get('paid_amount'):
                    print("[YT Chat] Drop timestamp: initial chat di luar sesi/jendela waktu atau tanpa timestamp valid.")
                    return None
                backlog_paid = True  # thank once, without asserting presence

        author = renderer.get('authorName', {}).get('simpleText', 'Unknown')
        if membership:
            author = membership.get('name') or author
        runs = renderer.get('message', {}).get('runs', [])
        msg = teks_pesan_chat(runs)
        if membership and not msg:
            msg = membership.get('message') or ''

        # Cetak SATU run emoji asli per sesi. Bentuk payload YouTube tidak
        # boleh cuma diasumsikan dari dokumentasi — pelajaran 27 Agu.
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
        # justru wajib direspon (D0). Membership join/gift may also have no chat text.
        if not msg and not paid and not membership: return None
        out = {'name': author, 'message': msg, 'id': msg_id}
        if paid:
            out['paid_amount'] = paid
        if membership:
            out['kind'] = 'membership'
            out['membership_months'] = membership.get('membership_months') or 0
            out['membership_level'] = membership.get('membership_level') or ''
        if backlog_paid:
            out['backlog_paid'] = True
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
            return [], None, 10000
    
    def process_message(msg):
        """Proses satu chat message"""
        nonlocal last_chat_trigger_time
        viewer = msg['name']
        chat_msg = msg['message']
        paid = msg.get('paid_amount', '')
        backlog_paid = msg.get('backlog_paid', False)
        membership = msg.get('kind') == 'membership'

        shown = f"[SUPER CHAT {paid}] {chat_msg}".strip() if paid else (
            f"[MEMBERSHIP] {chat_msg}".strip() if membership else chat_msg)
        print(f"\U0001f4ac [YT Chat] {viewer}: {shown}")

        if membership:
            import arti_donations as _dn_mem  # noqa: PLC0415
            _on_donation(_dn_mem.DonationEvent(
                platform="youtube",
                name=viewer,
                amount=0.0,
                message=chat_msg,
                kind="membership",
                membership_months=int(msg.get("membership_months") or 0),
                membership_level=str(msg.get("membership_level") or ""),
            ))
            return

        if (not paid and CONFIG.get("live7_help_enabled", False)
                and _live7_help_relay is not None
                and _live7_help_relay.note_viewer_command(
                    viewer, chat_msg, is_bot_viewer(viewer, CONFIG))):
            return

        # !command = perintah untuk BOT LAIN (Nightbot/StreamElements), bukan
        # untuk Arti (Bohan 2026-08-10: "[recorded remark omitted]").
        # Tidak men-trigger, TIDAK masuk history/ingatan (kurator shutdown
        # jangan pernah melihatnya), dan status "penonton baru"-nya TIDAK
        # hangus — sapaan selamat datang menunggu pesan manusiawi pertamanya.
        # Super Chat dikecualikan: orang bayar selalu dijawab.
        if not paid and chat_msg.lstrip().startswith("!"):
            print(f"[YT Chat Info] Perintah bot dari {viewer} — dilewati.")
            return

        if is_bot_viewer(viewer, CONFIG):
            # Dulu pesan bot tetap dicatat ke history (konteks leaderboard).
            # Keputusan Bohan 2026-08-10: jawaban bot poin dkk "[recorded remark omitted]" —
            # keluar dari history juga supaya tidak mengotori prompt/ingatan.
            return

        if CONFIG.get("owner_chat_as_owner"):
            from arti_runtime import owner_chat
            if not paid and not backlog_paid and owner_chat.is_owner(viewer, CONFIG):
                if CONFIG.get("video_enabled", False) and _video_watcher is not None:
                    import arti_video_watcher as _avw
                    _ids = _avw.extract_youtube_ids(chat_msg)
                    if _ids:
                        _video_watcher.submit(_avw.VideoJob(
                            video_id=_ids[0], source="chat", viewer=viewer, message=chat_msg,
                        ))
                owner_chat.take(__import__(__name__), viewer, chat_msg)  # his own account: the owner path
                return
        if CONFIG.get("live7_help_enabled", False) and _live7_help_relay is not None:
            _live7_help_relay.note_said(viewer, "youtube", chat_msg, viewer=True)
        if CONFIG.get("stream_game_notes"):
            if not paid:  # ADR 228: a Super Chat is recorded once, as a donation
                from arti_runtime import stream_notes
                stream_notes.bridge_viewer(__import__(__name__), viewer, chat_msg)

        if backlog_paid:
            add_to_history("System", f"[Donasi tertunda, bukan bukti hadir] {viewer}: {shown}")
        else:
            add_to_history(f"Viewer {viewer} (YouTube)", shown)

        # Fitur E (keputusan Bohan: AUTO): link YouTube di chat -> antre video.
        # Tidak return — pesan yang sama boleh sekaligus manggil Arti.
        if not backlog_paid and CONFIG.get("video_enabled", False) and _video_watcher is not None:
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
            backlog_note = (
                " (donasi tertunda dari sebelum sesi/bertimestamp tidak pasti; "
                "ucapkan terima kasih, bukan bukti hadir sekarang)"
                if backlog_paid else ""
            )
            schedule_donation_trigger(
                f"[DONASI Super Chat {paid} dari {viewer}]: "
                f"{chat_msg or '(tanpa pesan — cukup nominal)'}{backlog_note}",
                None if backlog_paid else viewer,
                chat_msg,
                on_queued=lambda: thanked_paid.record(video_id, msg['id']),
            )
            return

        wake = is_arti_wake_call(chat_msg)
        # Penonton BARU: pesan pertamanya memicu sapaan TANPA kata "arti".
        # Set-nya milik loop chat ini (bukan _yt_viewers_seen yang dipangkas
        # TTL) supaya "baru" berarti "belum pernah menyapa sesi ini".
        baru = (viewer not in _yt_pernah_chat
                and bool(CONFIG.get("yt_greet_new_viewers", True)))
        _yt_pernah_chat.add(viewer)
        # Chat santai (Bohan 2026-08-20): pesan manusiawi tanpa wake word ikut
        # memicu — keran globalnya sendiri dicek di bawah, SETELAH cooldown
        # per-viewer, supaya spammer tidak bisa memakai jalur ini.
        santai = (not wake and not baru
                  and bool(CONFIG.get("yt_chat_santai_enabled", True)))
        if _admit_quiet_viewer_chat(viewer, chat_msg, baru=baru, wake=wake):
            last_chat_trigger_time = time.time()
            if wake:
                last_addressed_trigger_by_viewer[viewer] = last_chat_trigger_time
            return
        if wake or baru or santai:
            current_time = time.time()
            if CONFIG.get("mic_priority_mark"):
                choice = _chat_rotator.choose(
                    viewer, chat_msg, baru=baru, wake=wake,
                    last_reply=last_arti_reply_text,
                )
                if choice.merge and _merge_waiting_yt_chat(viewer, chat_msg):
                    print(f"[YT Chat] Gabung chat {viewer}.")
                    return
                if baru and not wake:
                    label = ("[Pesan PERTAMA dari penonton BARU bernama "
                             f"{viewer} (YouTube) — sapa dia]: {chat_msg}")
                    jenis = "Penonton baru"
                elif santai:
                    label = (f"[Pesan Live Chat dari Viewer {viewer} (YouTube) "
                             "— dia ngobrol di chat tanpa menyebut namamu; "
                             f"timpali secara natural]: {chat_msg}")
                    jenis = "Chat santai"
                else:
                    label = f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: {chat_msg}"
                    jenis = "Panggilan"
                if choice.template and not choice.admit:
                    label = (f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: "
                             f"{choice.template}")
                print(f"[YT Chat] {jenis} dari {viewer}!")
                diterima = queue_voice_trigger(
                    label, trigger_type="yt_chat", viewer_name=viewer,
                    rotation_rank=choice.rank,
                )
                if diterima:
                    _chat_rotator.note(viewer)
                    _last_yt_trigger_by_viewer[viewer] = current_time
                    if wake:
                        last_addressed_trigger_by_viewer[viewer] = current_time
                    last_chat_trigger_time = current_time
                elif choice.template:
                    queue_voice_trigger(
                        f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: {choice.template}",
                        trigger_type="yt_chat", viewer_name=viewer, rotation_rank=-1,
                    )
                return
            # Wake helper juga mengenali reply/mention @Arti. Panggilan tidak
            # tertahan chat biasa sebelumnya, tapi tetap dibatasi per viewer.
            if wake:
                cd_viewer = float(CONFIG.get("yt_addressed_min_gap_sec", 10.0))
                last_v = last_addressed_trigger_by_viewer.get(viewer, 0.0)
            else:
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
                    if wake:
                        last_addressed_trigger_by_viewer[viewer] = current_time
                    last_chat_trigger_time = current_time
    
    # === Main Loop ===
    gagal_token = 0
    startup_checked = False
    reinit_delay = 3.0
    empty_limit = max(1, int(CONFIG.get("youtube_chat_reinit_empty_polls", 3)))
    while _yt_chat_running:
        try:
            continuation, initial_msgs = get_initial_chat()
            if not startup_checked:
                startup_checked = True
                if not continuation:
                    print("!!! [Startup Checklist] [FAIL] YouTube: chat tidak live/ID invalid atau token tidak tersedia — cek stream! !!!")
                elif not reused_id:
                    print("[Startup Checklist] [OK] YouTube: live chat tersedia.")
            if not continuation:
                # Backoff (log 18 Agu 22.41: ID salah ketik saat wizard ->
                # bridge balik ke video KEMARIN yang sudah mati -> "[recorded remark omitted]" tiap 15 dtk sepanjang sesi, ~40 baris spam).
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
            for m in initial_msgs:
                if m.get('paid_amount') or m in initial_msgs[-5:]:
                    process_message(m)
            
            # Poll loop
            empty_polls = 0
            while _yt_chat_running:
                messages, next_cont, timeout_ms = poll_chat(continuation)
                
                for m in messages:
                    process_message(m)

                empty_polls = 0 if messages else empty_polls + 1
                if messages:
                    reinit_delay = 3.0
                if not next_cont:
                    print(f"[YouTube Chat] Continuation kosong/stale — re-init dalam {reinit_delay:g}s.")
                    time.sleep(reinit_delay)
                    reinit_delay = min(reinit_delay * 2, 60.0)
                    break
                if empty_polls >= empty_limit:
                    # Chat sepi BUKAN token rusak. Live uji 24 Okt 2026 pagi:
                    # 148x "Continuation kosong/stale" padahal token valid dan
                    # 0 error poll; backoff naik ke 60 s, jadi saat chat sepi
                    # poller tidur ~60 dari tiap ~80 dtk dan pesan di jendela
                    # itu baru terbaca lewat halaman awal (maks 5) -> balasan
                    # telat sampai 1 menit. Refresh token sekali, TANPA backoff.
                    print(f"[YouTube Chat] Chat sepi {empty_polls} poll — refresh token (continuation masih valid).")
                    time.sleep(3.0)
                    break
                
                continuation = next_cont
                
                wait = max(timeout_ms / 1000.0, 3.0)
                time.sleep(wait)
        
        except Exception as e:
            print(f"[YouTube Chat Error] {e}. Retry 10 detik...")
            time.sleep(10)

def ssn_chat_worker():
    """YouTube + TikTok + Instagram chat through Social Stream Ninja (one door; see arti_runtime/ssn_chat.py).

    Same gates as youtube_chat_worker, platform in every label; TikTok/Instagram names are namespaced so they never
    count as the owner. Console shows counts only (no viewer names). Config refuses this with youtube_chat_enabled.
    """
    from arti_runtime import ssn_chat
    status_file = CONFIG.get("ssn_status_file") or ""
    try:
        session = ssn_chat.resolve_session(CONFIG)
    except ssn_chat.SessionError as exc:
        print("!!! [Startup Checklist] [FAIL] SSN chat: %s; give the session by secret card "
              "(mabar voice --ssn-session-file) !!!" % exc)
        ssn_chat.write_status(status_file, ok=False, why=str(exc))
        return
    ssn_chat.write_status(status_file, ok=True, why="session read", source=CONFIG.get("ssn_session_source"))
    stop = threading.Event()
    counts = ssn_chat.Counts()
    state = dict(last_trigger=0.0)
    last_addressed: dict[str, float] = {}

    def on_line(line):
        viewer = line.viewer
        youtube = line.platform == "YouTube"
        if (youtube and not line.donation and CONFIG.get("live7_help_enabled", False)
                and _live7_help_relay is not None
                and _live7_help_relay.note_viewer_command(viewer, line.text, is_bot_viewer(viewer, CONFIG))):
            return
        now = time.time()
        wake = is_arti_wake_call(line.text)
        new_viewer = viewer not in _yt_pernah_chat and bool(CONFIG.get("yt_greet_new_viewers", True))
        decision = ssn_chat.decide(
            line, is_bot=is_bot_viewer(line.name, CONFIG), wake=wake, new_viewer=new_viewer, now=now,
            last_viewer=_last_yt_trigger_by_viewer.get(viewer, 0.0), last_addressed=last_addressed.get(viewer, 0.0),
            last_trigger=state["last_trigger"], config=CONFIG)
        if decision.kind == "skip":
            return
        if CONFIG.get("live7_help_enabled", False) and _live7_help_relay is not None:
            _live7_help_relay.note_said(
                viewer, str(line.platform or "youtube").lower(), line.text, viewer=True)
        if decision.kind != "donation":
            _yt_pernah_chat.add(viewer)
        add_to_history(*decision.history)
        if CONFIG.get("stream_game_notes"):
            if decision.kind != "donation":  # ADR 228: donations arrive through schedule_donation_trigger
                from arti_runtime import stream_notes
                stream_notes.bridge_viewer(__import__(__name__), viewer, line.text)
        if decision.kind != "donation" and _admit_quiet_viewer_chat(
                viewer, line.text, baru=new_viewer, wake=wake, platform=line.platform):
            state["last_trigger"] = now
            if wake:
                last_addressed[viewer] = now
            return
        if decision.kind == "donation":
            dec = thanks_once.claim_ssn(CONFIG, line)
            if dec.is_new:
                schedule_donation_trigger(thanks_once.tag_trigger(decision.label, dec.token), viewer, line.text)
            else:
                _merge_thanks_extra(dec)
        elif decision.kind == "trigger":
            extra = {}
            if CONFIG.get("mic_priority_mark"):
                choice = _chat_rotator.choose(
                    viewer, line.text, baru=new_viewer, wake=wake,
                    last_reply=last_arti_reply_text,
                )
                if choice.merge and _merge_waiting_yt_chat(viewer, line.text):
                    return
                extra["rotation_rank"] = choice.rank
            if queue_voice_trigger(decision.label, trigger_type="yt_chat",
                                   viewer_name=viewer, **extra):
                if CONFIG.get("mic_priority_mark"):
                    _chat_rotator.note(viewer)
                _last_yt_trigger_by_viewer[viewer] = now
                if decision.wake:
                    last_addressed[viewer] = now
                state["last_trigger"] = now

    def watch():
        while _yt_chat_running:
            time.sleep(1.0)
        stop.set()

    threading.Thread(target=watch, daemon=True).start()
    print("[SSN Chat] starting (YouTube/TikTok/Instagram via Social Stream Ninja).")
    ssn_chat.run_reader(on_line, session=session, base=CONFIG.get("ssn_ws_base") or ssn_chat.DEFAULT_WS_BASE,
                        stop=stop, counts=counts)


# Status toggle hotkey: ON = semua omongan langsung ke Arti, OFF = passive mode
hotkey_active = False
hotkey_registered = False
vision_runtime_on = False
vision_hotkey_registered = False
vision_auto_until = 0.0
_asr_mic_id: int | None = None
_asr_mic_name: str = ""
_asr_restart_requested = False
# Telinga (mic ASR + loopback desktop) SESUDAH Ctrl+C: tanpa flag ini ASR
# masih menyelesaikan ucapan dan telinga desktop membuka capture lagi.
# Pipeline shutdown (observer + reindex) makan menitan; selama itu KEDUA
# telinga terus menyetor audio ke Groq = kuota terbakar untuk sesi yang
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
    if not screen_privacy.allows_screen():
        return False
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
            global hotkey_active, current_api_task, _toggle_on_time, tts_stop_flag
            now = time.time()
            _toggle_owner_capture()
            arti_executors.note_user_cancel()  # pembatalan sesudah ini = pengguna, bukan timeout
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
                clear_trigger_queue(keep_chat=_game_live(), keep_addressed=True)
                _ptt_attention_pause()
            else:
                elapsed = now - _toggle_on_time
                # Owner 2026-10-24: OFF biasa = omongan selesai, Arti TIDAK
                # dipotong; hanya double-toggle (bungkam) yang memotong.
                bungkam = elapsed < 2.0
                if bungkam:
                    _stop_voice_now(ptt=True)
                    # Double-toggle dalam < 2 detik = force bungkam
                    print(f"\n⚫⚫ [DOUBLE TOGGLE] Bungkam! ({elapsed:.1f}s)")
                else:
                    print("\n⚫ [Toggle OFF] Omongan masuk antrean prioritas; Arti tidak dipotong.")
                try:
                    _loop = main_event_loop
                    if _loop and not _loop.is_closed():
                        asyncio.run_coroutine_threadsafe(vts.trigger_expression_state("default"), _loop)
                except Exception:
                    pass
                _cancel_lamp_fallback()
                start_idle_animation()
                if not bungkam:
                    clear_trigger_queue(keep_chat=_game_live(), keep_addressed=True)

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
            global hotkey_active, current_api_task, _toggle_on_time_kb, tts_stop_flag
            now = time.time()
            _toggle_owner_capture()
            arti_executors.note_user_cancel()  # pembatalan sesudah ini = pengguna, bukan timeout
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
                clear_trigger_queue(keep_chat=_game_live(), keep_addressed=True)
                _ptt_attention_pause()
            else:
                elapsed = now - _toggle_on_time_kb
                # Owner 2026-10-24: OFF biasa = omongan selesai, Arti TIDAK
                # dipotong; hanya double-toggle (bungkam) yang memotong.
                bungkam = elapsed < 2.0
                if bungkam:
                    _stop_voice_now(ptt=True)
                    print(f"\n⚫⚫ [DOUBLE TOGGLE] Bungkam! ({elapsed:.1f}s)")
                else:
                    print("\n⚫ [Toggle OFF] Omongan masuk antrean prioritas; Arti tidak dipotong.")
                try:
                    _loop = main_event_loop
                    if _loop and not _loop.is_closed():
                        asyncio.run_coroutine_threadsafe(vts.trigger_expression_state("default"), _loop)
                except Exception:
                    pass
                _cancel_lamp_fallback()
                start_idle_animation()
                if not bungkam:
                    clear_trigger_queue(keep_chat=_game_live(), keep_addressed=True)
                
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
        if (CONFIG.get("rehearsal_scripted_ptt") and CONFIG.get("rehearsal_parent_pid")
                and not CONFIG.get("youtube_chat_enabled") and not CONFIG.get("ssn_chat_enabled")
                and os.environ.get("ARTI_REHEARSAL_SCRIPTED_PTT") == "1" and low == "bungkam"):
            # Rehearsal only (2026-11-18): the owner's double toggle as a scripted line, so the real cancel path
            # (turn/interrupt, the long thread kept; ADR 200 V13) can be proven without a person.
            from arti_runtime import rehearsal_bungkam
            rehearsal_bungkam.bungkam(__import__(__name__))
            continue
        if low in ("video off", "video on", "video skip") or low.startswith("video "):
            if _video_watcher is None:
                print("[Video] Watcher tidak aktif (video_enabled=False)")
                continue
            if low == "video off":
                _video_watcher.runtime_enabled = False
                _media_playback_until = 0.0  # bebaskan hold yang sedang aktif
                if CONFIG.get("media_watch_mode"):
                    chat_media.holds.end(time.time())
                print("[Video] Runtime OFF — media share/link diabaikan sampai 'video on'")
            elif low == "video on":
                _video_watcher.runtime_enabled = True
                print("[Video] Runtime ON")
            elif low == "video skip":
                _media_playback_until = 0.0
                if CONFIG.get("media_watch_mode"):
                    chat_media.holds.end(time.time())
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
            if (CONFIG.get("rehearsal_scripted_ptt") and CONFIG.get("rehearsal_parent_pid")
                    and not CONFIG.get("youtube_chat_enabled") and not CONFIG.get("ssn_chat_enabled")
                    and os.environ.get("ARTI_REHEARSAL_SCRIPTED_PTT") == "1"):
                # Rehearsal only (2026-11-18): a scripted owner line enters exactly like a PTT transcript, so the
                # said-row path, the voice play mark and the marked-line window can be proven without a person.
                # Needs the config flag, a rehearsal parent, the env var, and no live chat attached.
                if CONFIG.get("stream_game_notes"):
                    _note_screen_privacy(text)
                    from arti_runtime import stream_notes
                    stream_notes.bridge_line(__import__(__name__), "owner", "ptt", text, owner_marked=True)
                print("[TextInput] rehearsal: scripted owner line as PTT")
                queue_voice_trigger(text, trigger_type="ptt")
                continue
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
    itu jatah ASR mic. Tambah akun: tinggal tambah GROQ_API_KEY_xxx di
    .env, pool otomatis membesar (tiap kunci menambah jatah
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
    skip on cloud failure unless local fallback is explicitly enabled.
    language=None = auto-detect; key rotation and the desktop request lane
    keep background hearing separate from microphone transcription."""
    keys = _desktop_groq_keys()
    if not keys:
        return transcribe_audio(
            audio_np, 16000, use_groq=False, language=None, quiet=True, background=True
        )
    i = getattr(_desktop_transcribe, "_kidx", 0)
    _desktop_transcribe._kidx = i + 1
    return transcribe_audio(
        audio_np, 16000, use_groq=True, api_key=keys[i % len(keys)],
        language=None, quiet=True, background=True,
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
            is_media_gated=_desktop_ear_gate.is_gated,
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


_SCREEN_LOOK_REQUEST = re.compile(
    r"(?:(?:eh|arti|kamu|tolong) )*"
    r"(?:coba (?:liat|lihat)|(?:coba )?(?:liat|lihat) (?:ke )?(?:layar|screen)"
    r"|(?:itu apa|(?:apa|yang) (?:(?:yang|lagi) )?ada) di (?:layar|screen))"
    r"(?: (?:aku|saya|ini|itu|apa|dong|deh|ya|sekarang|sebentar|bentar|dulu|nih|arti))*"
)


def _owner_screen_look_requested(trigger: VoiceTrigger) -> bool:
    if (trigger.viewer_name is not None or trigger.trigger_type not in ("mic", "ptt", "wake_word", "mic_pasif")
            or (trigger.trigger_type == "mic_pasif" and not trigger.owner_marked)):
        return False
    text = " ".join(re.sub(r"[,?.!]", " ", trigger.text.casefold()).split())
    return bool(_SCREEN_LOOK_REQUEST.fullmatch(text))


def refresh_vision_for_turn(
    requested: bool = False, privacy_epoch: int | None = None,
) -> arti_screen_context.ScreenSnapshot | None:
    """Describe a requested frame for this turn, or refresh the manually enabled ring."""
    if not screen_privacy.allows_screen(privacy_epoch):
        return None
    if requested and not CONFIG.get("vision_enabled", CONFIG.get("screen_context_enabled", False)):
        return None
    if not requested and not is_vision_active():
        return None
    try:
        cfg = _scouter_config()
        if privacy_epoch is not None:
            cfg["_screen_privacy_epoch"] = privacy_epoch
        if requested:
            cfg.update(vision_runtime_on=True, _vision_explicit_request=True)
            snap, provider = arti_vision_client.capture_and_describe(cfg)
            if snap and screen_privacy.allows_screen(snap.privacy_epoch):
                print(f"[Vision] Owner screen request described via {provider}")
                return snap
            return None
        snap, provider = arti_vision_client.refresh_if_stale(cfg)
        if snap and snap.scene:
            arti_screen_context.update_watch_state_from_snapshot(
                snap,
                event_id=str(CONFIG.get("watch_party_event_id") or ""),
            )
            print(f"[Vision] Turn refresh via {provider}: {snap.scene[:50]}...")
    except Exception as e:
        print(f"[Vision] Turn refresh skip: {type(e).__name__}: {e}")
    return None


@contextmanager
def _asr_input_stream(stream_kw):
    """Retry device open/enter for both calibration and normal listening."""
    global _asr_mic_id, _asr_mic_name
    delay = 0.5
    with ExitStack() as stack:
        while not _telinga_dimatikan:
            try:
                stream = stack.enter_context(sd.InputStream(**stream_kw))
                break
            except Exception as e:
                print(f"[ASR ERROR] Mic open gagal: {type(e).__name__}: {e}; retry {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 5.0)
                try:
                    _asr_mic_id, _asr_mic_name = resolve_asr_input_device()
                    if _asr_mic_id is None:
                        stream_kw.pop("device", None)
                    else:
                        stream_kw["device"] = _asr_mic_id
                except Exception as resolve_error:
                    print(f"[ASR ERROR] Mic resolve gagal: {resolve_error}; default Windows")
                    stream_kw.pop("device", None)
        else:
            stream = None
        yield stream


# === MIC PASIF -> GILIRAN (owner 2026-10-23) ================================
# "[recorded remark omitted]": ucapan Bohan saat PTT MATI dulu cuma
# masuk history. Sekarang boleh jadi giliran "mic_pasif" — tapi yang
# memutuskan menjawab atau diam adalah MODEL (aturan owner: reaksi/keputusan
# dari model, bukan logika program). Program hanya memasang PAGAR:
# echo/noise (filter lama), minimal kata+durasi, tidak saat Arti bicara/sibuk
# atau PTT nyala, cooldown antar giliran pasif, antrean kosong, dan chat
# viewer yang menunggu selalu menang (gerbang yang sama dengan curious).
# Suara game/video yang bocor ke mic: tidak ada pagar desktop-audio khusus
# (belum pernah ada pembanding mic-vs-loopback di bridge) — yang menjaga
# filter lama + pilihan model ([DIAM]); prompt menyebut kemungkinan itu.
# mic_pasif SENGAJA bukan trigger pemilik (STREAMER_TRIGGERS/_NEW_GOAL):
# bocoran audio tidak boleh mengganti mode, misi, goal Stardew, menjalankan
# tag [MC:], menulis [MEMORY_SAVE:], atau menjawab pertanyaan bantuan Live7.
# Tanpa opt-in routing: otak STATELESS (groq_chat_completion: Groq -> OpenRouter).
# Kalau cursor_trigger_types memuat mic_pasif, pakai jalur utama (Luna sesuai
# config); pilihan [DIAM] dan prioritas PTT tetap berlaku di kedua jalur.
_last_passive_voice_turn_ts = 0.0
# Giliran mic_pasif sedang diproses / sudah masuk fase bicara. Selama
# inflight, PTT tidak pernah dibuang karena "Arti masih proses".
_passive_turn_inflight = False
_passive_turn_speaking = False
_passive_junk_gate = streamer_voice.PassiveGate()


def _passive_speech_is_short(text: str) -> bool:
    return len((text or "").split()) < int(CONFIG.get("asr_pasif_reply_min_words", 3) or 0)


def _last_exchange_ts() -> float:
    """Kapan terakhir ada giliran Bohan<->Arti yang sungguhan (bukan history pasif)."""
    tts_end = float(getattr(voice_listener_worker, "_last_tts_end", 0.0) or 0.0)
    return max(_last_arti_reply_ts, _last_streamer_speech_ts,
               _last_passive_voice_turn_ts, tts_end)


def _short_speech_block_reason(text: str) -> str | None:
    """Sapaan pendek tanpa nama Arti (T3-25: "Halo", "oi" dibuang terlalu_pendek).

    Lolos hanya dengan keyakinan giliran: baru ada percakapan Bohan<->Arti
    dalam asr_pasif_short_recent_sec. Pelindung gema: terlalu dekat dengan
    akhir TTS, atau kata-katanya persis ada di jawaban Arti terakhir, tetap
    dibuang. Penerima ucapan tetap keputusan model ([DIAM] boleh).
    """
    now = time.time()
    recent = float(CONFIG.get("asr_pasif_short_recent_sec", 90.0) or 0.0)
    if recent <= 0 or now - _last_exchange_ts() > recent:
        return "terlalu_pendek"
    tts_end = float(getattr(voice_listener_worker, "_last_tts_end", 0.0) or 0.0)
    since_tts = now - tts_end if tts_end else None
    if since_tts is not None and since_tts < float(CONFIG.get("asr_pasif_short_echo_sec", 3.0) or 0.0):
        return "gema"
    heard = _normalize_asr_text(text or "")
    said = _normalize_asr_text(last_arti_reply_text or "")
    if (
        heard and said and len(heard) >= 3
        and since_tts is not None and since_tts < 15.0
        and re.search(r"(?<!\w)" + re.escape(heard) + r"(?!\w)", said)
    ):
        return "gema"
    return None


def _passive_voice_block_reason(text: str, audio_duration_sec: float | None) -> str | None:
    """Pagar giliran mic_pasif. None = boleh diantrikan; selain itu alasannya."""
    if not CONFIG.get("asr_pasif_reply_enabled", True):
        return "disabled"
    if str(CONFIG.get("trigger_mode", "wake_word")).lower() != "push_to_talk":
        return "bukan_mode_ptt"
    if hotkey_active:
        return "ptt_nyala"
    # Panggilan nama lolos pagar panjang (smoke 2026-10-24: "[recorded remark omitted]",
    # "[recorded remark omitted]" dibuang terlalu_pendek, Bohan mengira Arti beku). Pagar lain
    # tetap berlaku dan model masih boleh memilih [DIAM].
    called = streamer_voice.addressed(text or "", CONFIG) or _passive_owner_reply_required(text)
    if not called and _passive_speech_is_short(text):
        why = _short_speech_block_reason(text)
        if why is not None:
            return why
    min_sec = float(CONFIG.get("asr_pasif_reply_min_sec", 1.0) or 0.0)
    if not called and audio_duration_sec is not None and audio_duration_sec < min_sec:
        return "audio_pendek"
    with _brain_busy_lock:
        if _brain_busy or tts_is_playing:
            return "arti_sibuk"
    cooldown = float(CONFIG.get("asr_pasif_reply_cooldown_sec", 20.0) or 0.0)
    if time.time() - _last_passive_voice_turn_ts < cooldown:
        return "cooldown"
    if CONFIG.get("voice_queue_enabled", False):
        pending = len(voice_trigger_buffer) > 0
    else:
        pending = not voice_trigger_queue.empty()
    if pending:
        return "antrean_terisi"
    if _viewer_chat_waiting():
        return "chat_viewer_menunggu"
    return None


def _maybe_queue_passive_voice_turn(text: str, audio_duration_sec: float | None, *, owner_marked: bool = True) -> bool:
    """Antrikan giliran mic_pasif kalau semua pagar lolos."""
    global _last_passive_voice_turn_ts
    command = speech_control.command(text)
    if command:
        _note_owner_speech(text, "mic_pasif", None)
        return False
    if speech_control.held and not CONFIG.get("voice_queue_preserve_humans", False):
        return False
    why = _passive_voice_block_reason(text, audio_duration_sec)
    if why is not None and why not in (
            "arti_sibuk", "cooldown", "antrean_terisi", "chat_viewer_menunggu"):
        if why != "disabled":
            print(f"[ASR Pasif] Tidak diantrikan ({why}).")
        return False
    if CONFIG.get("live_voice_fixes_v1") and CONFIG.get("mic_priority_mark") and not owner_marked:
        # The owner's lines to a viewer could reach her as plain owner speech and she would answer them as hers.
        # The line says who it was for; she still decides (a priority mark, not a gate).
        viewer = mic_priority.addressed_viewer(text, _yt_pernah_chat)
        if viewer:
            text = f"[Bohan lagi ngomong ke penonton {viewer}, bukan ke kamu]: {text}"
    if not queue_voice_trigger(text, trigger_type="mic_pasif", owner_marked=owner_marked):
        return False
    _last_passive_voice_turn_ts = time.time()
    return True


def _handle_passive_ptt_transcript(text, audio_duration_sec) -> bool:
    """PTT OFF: filter lama -> history -> (opsional) giliran mic_pasif."""
    global _last_streamer_speech_ts
    if not text:
        return False
    # Explicit owner control precedes every junk/quality/echo gate (ADR 164).
    if speech_control.command(text):
        _note_owner_speech(text, "mic_pasif", None)
        add_to_history("Streamer", str(text))
        if CONFIG.get("stream_game_notes"):
            from arti_runtime import stream_notes
            stream_notes.bridge_passive(__import__(__name__), text)
        return False
    if CONFIG.get("passive_mic_hygiene") and not mic_hygiene.has_question_mark(text) and (
        mic_hygiene.is_hallucination_transcript(text, prompt=arti_voice_pipeline.MIC_ASR_VOCABULARY)
        or text.strip() in _ASR_LITERAL_PHANTOMS
    ):
        print("[ASR Noise Filter] Skip Whisper phantom.")
        return False
    # Preserve T3-25's bounded conversational short turns, including its echo fence.
    required = _passive_owner_reply_required(text)
    allow_short = _passive_speech_is_short(text) and _short_speech_block_reason(text) is None
    why = _passive_junk_gate.reject(text, CONFIG, allow_short=allow_short or required, dedup_addressed=required)
    if why is not None:
        add_to_history("Streamer", str(text))
        if CONFIG.get("stream_game_notes"):
            from arti_runtime import stream_notes
            stream_notes.bridge_passive(__import__(__name__), text)
        print(f"[ASR Pasif] History only ({why}).")
        return False
    text = filter_whisper_hallucination(text, is_passive_monitoring=True)
    if not text:
        return False
    if is_asr_noise_transcript(text, audio_duration_sec):
        print(f"[ASR Noise Filter] Skip noise: \"{text}\"")
        return False
    if is_asr_echo_of_arti(text):
        print(f"[ASR Echo Filter] Skip echo: \"{text}\"")
        return False
    speech_clock = _last_streamer_speech_ts
    add_to_history("Streamer", text)
    print(f"[ASR Info] Pasif: \"{text}\"")
    marked = required or _mic_pasif_owner_marked(text)
    if CONFIG.get("stream_game_notes"):
        if CONFIG.get("mic_priority_mark") and marked:
            from arti_runtime import stream_notes
            stream_notes.bridge_line(__import__(__name__), "owner", "mic_pasif", text, owner_marked=True)
    if CONFIG.get("mic_priority_mark") and not marked:
        _last_streamer_speech_ts = speech_clock
    if CONFIG.get("passive_mic_hygiene"):
        why = mic_hygiene.passive_history_only_reason(
            named=required or streamer_voice.addressed(text, CONFIG),
            now=time.time(), media_until=_media_playback_until)
        if why is not None:
            print(f"[ASR Pasif] History only ({why}).")
            return False
    return _maybe_queue_passive_voice_turn(text, audio_duration_sec, owner_marked=marked)


def _passive_voice_prompt(
    prompt_content: str, target_instruction: str, speech: str = "", *, answer_required: bool = False,
) -> str:
    """PTT state is observed; addressee/response remain the model's decision."""
    # Wake detection supplies a word-bounded lexical fact, never owner authority
    # or a forced reply. Mentioning Arti can still mean talking ABOUT her.
    address_fact = (
        "Di ucapan mic saat ini nama Arti disebut; penyebutan nama belum "
        "memastikan bahwa Bohan sedang bicara kepadamu. "
        if is_arti_wake_call(speech) else ""
    )
    if not address_fact and speech and _passive_speech_is_short(speech):
        # Fakta, bukan keputusan: ucapan pendek lolos karena kalian baru
        # bertukar giliran; sapaan itu bisa untukmu, untuk orang lain, atau bocoran.
        address_fact = (
            "Ucapan mic saat ini sangat pendek dan datang tak lama sesudah "
            "giliran kalian; itu bisa sapaan/panggilan untukmu, untuk orang "
            "lain, atau bukan ucapan untukmu. "
        )
    instruction = (
        address_fact
        + "Bohan barusan bicara lewat mic; PTT tidak ditekan dan penerima ucapan belum diketahui. "
        "Ucapan di [Pesan/Panggilan Sekarang] adalah ucapan mic saat ini. "
        "Layar dan chat lama hanya bahan konteks, bukan pengganti ucapan mic "
        "atau bukti bahwa Bohan sedang membicarakan dirimu. "
        "Mic-nya kebuka, tombol "
        "bicara tidak ditekan. Bisa jadi dia ngomong ke kamu, ke penonton, ke "
        "dirinya sendiri, atau itu malah suara game/video yang ketangkep mic. "
        "KAMU yang memutuskan: kalau memang ada yang pengen kamu tanggapi, "
        "jawab santai sebagai Arti ke Bohan. Kalau tidak perlu ditanggapi, "
        "balas PERSIS [DIAM] saja — tanpa kata lain, tanpa penjelasan. "
        "Kalau Bohan tidak sedang bicara kepadamu — misalnya ngomong sendiri, "
        "membaca instruksi keras-keras, atau mengumpat ke game — balas PERSIS [DIAM]. "
        "(Omongan tanpa PTT tidak dihitung sebagai saran; kalau kamu lagi "
        "nanya saran, boleh minta Bohan pencet PTT.)"
    )
    if answer_required:
        instruction = (
            "Bohan menyapamu atau menanyakan sesuatu di [Pesan/Panggilan Sekarang]. "
            "Tanggapi pesannya langsung dalam suara Arti. Kalau belum tahu jawabannya, bilang terus terang. "
            "Layar dan chat lama hanya bahan konteks. Ucapan tanpa PTT tetap bukan saran untuk menggerakkan permainan."
        )
    if target_instruction and target_instruction in prompt_content:
        return prompt_content.replace(target_instruction, instruction, 1)
    return f"{prompt_content}\n{instruction}"


# ADR 98 (a46 T40): the model typed the token with a space inside it; the exact-word pattern missed it and it reached her speech.
DIAM_TOKEN_RE = re.compile(r"\[\s*d\s*i\s*a\s*m\s*\]", re.IGNORECASE)
_DIAM_CONFUSABLES = str.maketrans({
    "Ԁ": "D", "ԁ": "d", "І": "I", "і": "i", "Ӏ": "I", "ӏ": "i",
    "А": "A", "а": "a", "М": "M", "м": "m",
    "Ι": "I", "ι": "i", "Α": "A", "α": "a", "Μ": "M", "μ": "m",
})
# Include fullwidth and vertical square brackets, which NFKC folds to ASCII.
_DIAM_CANDIDATE_RE = re.compile(r"[\[［﹇][^\[［﹇\]］﹈]*[\]］﹈]")


def _normalize_diam_token(text):
    return unicodedata.normalize("NFKC", text).translate(_DIAM_CONFUSABLES)


def _strip_diam_tokens(text):
    def strip_token(match):
        candidate = match[0]
        return "" if DIAM_TOKEN_RE.fullmatch(_normalize_diam_token(candidate)) else candidate

    return _DIAM_CANDIDATE_RE.sub(strip_token, text).strip()


def _passive_voice_reply_is_silence(reply) -> bool:
    """Giliran mic_pasif: model memilih diam? Diam = no TTS/subtitle/history.

    [DIAM] (atau jawaban kosong) = pilihan model. Jawaban yang filter
    meta/narator akan ganti dengan kalimat kaleng ("ada apa nih?") juga
    dianggap diam — kalimat kaleng ke orang yang tidak bertanya lebih buruk.
    """
    text = str(reply or "").strip()
    normalized = _normalize_diam_token(text)
    if not normalized or DIAM_TOKEN_RE.search(normalized):
        return True
    if re.sub(r"[^a-z]", "", normalized.lower()) == "diam":
        return True
    try:
        kept, removed = filter_meta_history_talk(clean_ai_reply(text).strip())
        return (not kept or removed > 0 or bool(DIAM_TOKEN_RE.search(_normalize_diam_token(kept)))
                or is_narrator_reply(kept))
    except Exception:  # noqa: BLE001 — penilai advisory; ragu = jalur lama
        return False


def voice_listener_worker():
    """Mendengarkan mic secara real-time dengan Auto Noise Gate & cerdas mendeteksi panggilan nama A"""
    global hotkey_active
    print("[ASR] Pendengar mic aktif (Menggunakan Groq Cloud Whisper dengan local fallback)...")
    
    samplerate = 16000
    channels = 1
    
    audio_queue = AudioChunkQueue(CONFIG.get("asr_audio_queue_max_chunks", 100))
    max_utterance_sec = max(0.1, float(CONFIG.get("asr_max_utterance_sec", 30.0)))
    
    last_callback_at = time.monotonic()
    callback_clock = time.monotonic

    def audio_callback(indata, frames, time, status):
        nonlocal last_callback_at
        last_callback_at = callback_clock()
        # Hanya rekam suara jika Arti sedang tidak berbicara — termasuk saat
        # dia melepas refleks ("Aduh!"). Refleks TIDAK memakai tts_is_playing
        # (itu akan membuang trigger reaksi untuk event yang sama), jadi
        # flag-nya sendiri harus ikut dicek di sini.
        if not _reflex_playing:
            audio_queue.put_latest(indata.copy())

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
        # dari sesi lalu, dan nomor itu bergeser kalau perangkat audio berubah.
        # Device yang tidak valid membuat `sd.InputStream` melempar PortAudioError
        # (mis. -9996) dan thread ASR bisa MATI DIAM-DIAM tanpa ada yang tahu.
        # Sekarang: coba device tersimpan, kalau gagal jatuh ke
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

    with _asr_input_stream(stream_kw):
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

        with _asr_input_stream(stream_kw):
            last_callback_at = time.monotonic()
            recording = []
            is_speaking = False
            stardew_asr_epoch = None
            capture_generation = None
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
                if (not tts_is_playing and not _reflex_playing
                        and time.monotonic() - last_callback_at >= 5.0):
                    print("!!! ASR WATCHDOG: tidak ada callback mic selama 5s — reopen !!!")
                    request_asr_stream_restart("no audio callback")
                    stream_dead = True
                    break
                try:
                    data = audio_queue.get(timeout=0.1)
                    audio_queue.report_drops()
                    audio_chunk = data.flatten()

                    rms = np.sqrt(np.mean(audio_chunk**2))
                    # ADR 172: passive VAD measures the owner's floor, even before ASR ends.
                    attention_bridge.audio(__import__(__name__), bool(rms > silence_threshold), len(audio_chunk)/samplerate)

                    if hotkey_active and time.time() < _asr_ptt_cooldown_until:
                        continue

                    if rms > silence_threshold:
                        _note_ptt_speech()
                        if not is_speaking:
                            # Allocate the bounded chunk buffer only when speech starts.
                            recording = UtteranceBuffer(samplerate, max_utterance_sec)
                            print("[ASR] Mendengar suara...")
                            is_speaking = True
                            stardew_asr_epoch = _stardew_ptt_epoch if hotkey_active else None
                            capture_generation = _ptt_capture_generation if hotkey_active else None
                        recording.append(audio_chunk)
                        silence_duration = 0
                        last_voice_at = time.perf_counter()
                        # Bohan lanjut bicara -> tebakan spekulatif jadi basi.
                        spek_dikirim = False
                        spek_hasil = {}
                    if rms <= silence_threshold or (is_speaking and recording.full):
                        if is_speaking:
                            # JAM SUNGGUHAN, bukan hitungan potongan.
                            # Sampai 12 Agu 2026 baris ini menambah 0,1 per
                            # potongan audio — mengasumsikan tiap potongan 100 ms.
                            # `sd.InputStream` dibuat TANPA blocksize, jadi ukuran
                            # potongan ditentukan driver mic (~20 ms di mesin uji)
                            # — jam itu berlari ~5x lebih cepat dari waktu nyata:
                            # ekor sunyi 10 detik di config cuma ~2 detik nyata.
                            # Efek sampingnya paling jahat: panjang jeda ikut
                            # berubah diam-diam kalau mic/driver berganti.
                            silence_duration = time.perf_counter() - last_voice_at
                            if rms <= silence_threshold:
                                recording.append(audio_chunk)

                            # Diam selama silence_tail = selesai bicara (PTT lebih sabar)
                            trigger_mode = CONFIG.get("trigger_mode", "wake_word").lower()
                            if trigger_mode == "push_to_talk":
                                # Ekor 5 dtk hanya perlu saat TOGGLE ON (nunggu
                                # streamer selesai mikir). Jalur PASIF cuma mencatat
                                # — dengan ekor yang sama, monolognya baru masuk
                                # history sebagai bongkahan SETELAH 5 dtk hening,
                                # dan Arti menanggapi barang basi.
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
                            if recording.full:
                                print(f"[ASR] Max utterance {max_utterance_sec:g}s — finalize audio yang terekam")
                                silence_tail = 0
                                spek_hasil = {}
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
                                    _potret = recording.as_array()
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
                                        # Sesi live 27 Agu mencetak "[recorded remark omitted]" — angka omong kosong. Itu
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
                                audio_array = recording.as_array()
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
                                            _discard_stardew_capture(stardew_asr_epoch)
                                            recording.clear()
                                            is_speaking = False
                                            silence_duration = 0
                                            continue
                                    if hotkey_active:
                                        # PTT is owner ingress: transcribe while the lower
                                        # priority turn is cancelled; never drop corrections.
                                        vad_tail_ms = int(silence_duration * 1000)
                                        asr_t0 = time.perf_counter()
                                        # Endpoint adaptif sudah punya transkripnya;
                                        # jangan bayar ASR dua kali. Yang tidak ikut
                                        # cuma ekor sunyi di belakang.
                                        if spek_teks is not None:
                                            text = spek_teks
                                        else:
                                            text = _transcribe_stardew_ptt(audio_array,samplerate,stardew_asr_epoch)
                                        asr_ms = int((time.perf_counter() - asr_t0) * 1000)
                                        asr_stages = {"vad_tail_ms": vad_tail_ms, "asr_ms": asr_ms}
                                        _complete_stardew_asr(text, stardew_asr_epoch, audio_dur, asr_stages, capture_generation)
                                    else:
                                        # OFF: catat ke history; lolos pagar -> giliran mic_pasif
                                        text = transcribe_audio(audio_array, samplerate, use_groq=True, capture_quality=True)
                                        _handle_passive_ptt_transcript(text, audio_dur)
                                else:
                                    # Wake word mode: cek keyword dulu
                                    vad_tail_ms = int(silence_duration * 1000)
                                    asr_t0 = time.perf_counter()
                                    use_groq = True
                                    text = transcribe_audio(audio_array, samplerate, use_groq=use_groq)
                                    asr_ms = int((time.perf_counter() - asr_t0) * 1000)
                                    asr_stages = {"vad_tail_ms": vad_tail_ms, "asr_ms": asr_ms}
                                    if text and text.strip() not in _ASR_LITERAL_PHANTOMS:
                                        print(f"[ASR] Hasil: \"{text}\"")
                                        if is_arti_wake_call(text):
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
                                recording.clear()
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
    """Read and backstage-filter ARTI_SOUL.md once during startup.
    Live/test-mode prompt copies need a process restart to pick up edits."""
    from arti_runtime.soul_block import load_soul_context as shared_loader
    soul = shared_loader(Path(_SCRIPT_DIR))
    if not soul and os.path.exists(os.path.join(_SCRIPT_DIR, "ARTI_SOUL.md")):
        # The shared loader fails closed and silent; the bridge must still say so
        # at startup, or a stream can run without her persona unnoticed.
        print("[Soul Error] ARTI_SOUL.md ada tapi gagal dibaca atau kosong")
    return soul

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

    Menggantikan dump statis semua penonton di system prompt. Dump lama berisi
    SEMUA penonton di TIAP turn walau tidak ada penonton sama sekali — dan itulah
    yang bikin prompt jebol cap.

    Penonton yang cuma DISEBUT (bukan chat sendiri) tetap terjangkau lewat vault
    RAG: ARTI_VIEWERS.md ikut ter-index, dan query berisi nama terbukti menariknya
    (uji "siapa penonton yang sering chat" -> ARTI_VIEWERS.md peringkat 1).

    Mengerti dua format entri di ARTI_VIEWERS.md:
      ### nama\\n- **Field:** ...          (format penuh)
      ### nama | Pertemuan: ... | ...      (format ringkas — loader lama justru
                                            men-skip baris ini karena ada '|')
    """
    name = (viewer_name or "").strip().lstrip("@").casefold()
    # The argument is the current trigger author (including donations/game),
    # not an enumerated profile or a presence claim inferred from memory.
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
    return "\n\n[VIEWER SAAT INI — profil dari ingatan, bukan bukti hadir:]\n" + "\n".join(block)


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


def apply_scouter_result(summary_data: dict, *, privacy_epoch: int | None = None) -> None:
    """Apply scouter JSON: mood, memory, auto-vision window, vision describe."""
    global scouter_result, summarizer_result, vision_auto_until, _last_scouter_ts, _last_scouter_history_snapshot

    summary_data = arti_live_safety.public_summary(summary_data)
    with history_lock:
        if privacy_epoch is not None and not screen_privacy.current(privacy_epoch):
            return
        privacy_epoch = screen_privacy.epoch
        if screen_privacy.restricted:
            summary_data = {**summary_data, "screen_relevant": False,
                            "screen_hint": None, "curious_worthy": False,
                            "curious_hook": None, "curious_question": None,
                            "important_facts": []}
        with scouter_lock:
            scouter_result = summary_data
            summarizer_result = summary_data
            CONFIG["scouter_last_result"] = summary_data

    emotion = summary_data.get("emotion", "neutral")
    new_mood = _emotion_to_mood(emotion)
    set_mood(new_mood)

    print(f"[Scouter] Summary: {summary_data.get('summary', '')[:80]}...")
    print(f"[Scouter] Emotion: {emotion} → Mood: {new_mood}")

    facts = summary_data.get("important_facts", []) if CONFIG.get("scouter_facts_enabled", True) else []
    for fact in arti_timeline_guard.filter_scouter_facts(facts):
        if not screen_privacy.current(privacy_epoch):
            return
        if fact and len(str(fact)) > 10:
            save_long_term_memory(f"Stream fact: {fact}")

    if summary_data.get("screen_relevant") and screen_privacy.allows_screen():
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
        privacy_epoch = screen_privacy.epoch
        recent_history = list(stream_history)[-15:]
    context_text = "\n".join(recent_history)
    if not context_text.strip():
        return

    print(f"[Scouter] Run ({reason})...")
    summary_data = arti_scouter_client.run(
        context_text, {**_scouter_config(), "_screen_privacy_epoch": privacy_epoch}
    )
    if not summary_data:
        print("[Scouter] Semua provider gagal.")
        return
    apply_scouter_result(summary_data, privacy_epoch=privacy_epoch)


def _scouter_timer_due() -> bool:
    import arti_scouter_client

    if not CONFIG.get("scouter_enabled", True):
        return False
    min_gap = float(CONFIG.get("scouter_min_gap_sec", 120))
    if time.time() - _last_scouter_ts < min_gap:
        return False
    interval = float(CONFIG.get("scouter_interval_sec", 300))
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
                every_n = int(CONFIG.get("scouter_every_n_triggers", 0))
                if every_n <= 0:
                    trigger_count_since_scouter = 0  # timer-only scouter
                elif trigger_count_since_scouter >= every_n:
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
    data = arti_live_safety.public_summary(data)
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

MOOD_HISTORY_MAX = 200   # 14 Agu 2026: berkas sempat 174 KB / 2.649 entri


def mind_intent_block_now() -> str:
    """ARTI's own current intent from ``arti-state/mind/now.json`` as one generic ``[NIATKU]`` line.

    Mind slice 1 (contract rule 8): the bridge knows nothing game-specific; it reads the generic file the
    gaming side overwrites per decision. Missing, broken or stale (> arti_mind_now_max_age_sec) -> ''.
    Silent context: never voiced by itself."""
    if not CONFIG.get("arti_mind_intent_enabled", True):
        return ""
    if CONFIG.get("live7_mind_scope") is True:
        try:
            from arti_runtime import mind_scope
            return mind_scope.bridge_intent_block(__import__(__name__))
        except Exception:  # noqa: BLE001 - a persona turn never fails on the mind file
            return ""
    try:
        return arti_mind.intent_block(arti_mind.mind_dir(CONFIG), max_age_sec=float(
            CONFIG.get("arti_mind_now_max_age_sec", arti_mind.NOW_MAX_AGE_SEC)))
    except Exception:  # noqa: BLE001 - a persona turn never fails on the mind file
        return ""


def stardew_role_block_now() -> str:
    """Siapa yang main, dibaca tiap giliran selama Stardew live.

    Tanpa blok ini model bisa menganggap streamer yang main. Di stream Live7
    Arti main karakternya
    sendiri (farmhand); identitas petani host berasal dari pembacaan native.
    """
    if not _stardew_live():
        # The asynchronous live-cycle cache starts false. A fresh native pose already
        # proves that Stardew is running, including the first discovery-triggered turn.
        row = arti_mind.read_now(arti_mind.mind_dir(CONFIG)) or {}
        if CONFIG.get("live7_mind_scope") is True:
            from arti_runtime import mind_scope
            row = mind_scope.bridge_read_now(__import__(__name__)) or {}
        from arti_runtime.time_grounding import current_state
        if row.get("game") != "stardew" or current_state(row).get("basis") != "latest_observation":
            return ""
    if CONFIG.get("live_voice_fixes_v1") and LIVE7_AGENCY_FACT in _live7_status_block_now():
        # V6.3 (mabar): this turn's [STARDEW STATUS] line already opens with the agency text; one copy per turn.
        return ("\n\n[STARDEW] Kamu (Arti) lagi main Stardew Valley pakai karaktermu sendiri. Jangan ngomong seolah "
                "Bohan atau penonton yang mainin karaktermu. Tidak perlu sering-sering menyebut 'kebun kita'.")
    return (
        "\n\n[STARDEW] Kamu (Arti) lagi main Stardew Valley pakai karaktermu sendiri; "
        + LIVE7_AGENCY_FACT + " Jangan ngomong seolah "
        "Bohan atau penonton yang mainin karaktermu. Tidak perlu sering-sering menyebut 'kebun kita'."
    )


def mood_block_now() -> str:
    """Blok mood yang SELALU segar, dibaca tiap giliran.

    Dulu blok ini dirakit sekali di startup dan ikut dipanggang ke
    `dynamic_system_prompt`. Akibatnya scouter boleh memperbarui mood
    sesering apa pun — Arti tetap membaca mood saat bridge dinyalakan.
    Akibatnya mood di prompt bisa beku sejak startup dan Arti mengulang
    kalimat dengan emosi yang sama tiap giliran.
    """
    mood = get_current_mood()
    # ADR 90: "cheerful" with no state file is the fallback, not a mood anyone wrote. No frozen anchor.
    if mood == "cheerful" and not os.path.exists(os.path.join(_SCRIPT_DIR, "ARTI_MOOD_STATE.json")):
        return ""
    return f"\n\n[MOOD SAAT INI: {mood}]"


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
            # (174 KB / 2.649 entri per 14 Agu 2026) padahal dibaca tiap
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

# Re-exported from arti_runtime/reply_text.py; calls inside reply_text do not see bridge rebinding.
from arti_runtime.reply_text import _BAD_ARTICULATION_PATTERNS  # noqa: E402


def _streamer_label(config: dict | None = None) -> str:
    """Nama streamer untuk fallback in-character (config_local.json)."""
    cfg = config or CONFIG
    return (cfg.get("streamer_name") or "").strip()


# Re-exported from arti_runtime/reply_text.py; calls inside reply_text do not see bridge rebinding.
from arti_runtime.reply_text import _sentence_is_bad_articulation  # noqa: E402


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


# Re-exported from arti_runtime/reply_text.py; calls inside reply_text do not see bridge rebinding.
from arti_runtime.reply_text import filter_meta_history_talk  # noqa: E402


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

    # "hidup" TELANJANG dibuang dari daftar (live 22.37): misi bawaan Arti
    # berbunyi "bertahan hidup", jadi SETIAP giliran game yang jatuh ke sini
    # meledakkan "[recorded remark omitted]" — tiga kali dalam satu sesi, dan Bohan
    # merasakannya sebagai "[recorded remark omitted]".
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
    # (Bohan: "[recorded remark omitted]").
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


# Model nalar (gemini-2.5-flash thinking, gpt-oss) menghitung token nalar di
# max_tokens yang sama — beri ruang di atas budget jawaban supaya batas aman
# (arti_reply_policy.reply_hard_cap) tetap muat setelah nalar.
REPLY_THINKING_HEADROOM_TOKENS = 256


def _reply_tokens_with_thinking(user_speech: str, floor: int) -> int:
    """Budget jawaban + ruang nalar, tidak pernah di bawah lantai lama."""
    return max(
        int(floor),
        live_max_tokens_for_trigger(user_speech or "", CONFIG)
        + REPLY_THINKING_HEADROOM_TOKENS,
    )


# Re-exported from arti_runtime/reply_text.py; calls inside reply_text do not see bridge rebinding.
from arti_runtime.reply_text import _trim_stream_reply  # noqa: E402


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
                f"target {plan.sentences} kal (~{plan.max_chars}ch, tok≈{plan.max_tokens})"
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


# Re-exported from arti_runtime/reply_text.py; calls inside reply_text do not see bridge rebinding.
from arti_runtime.reply_text import _truncate_reply_length, _runaway_clause_cut  # noqa: E402


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
    return groq_chat.pick_groq_model(
        user_text, config, prompt_chars,
        default_config=CONFIG,
        pick_groq_model=pick_groq_model,
    )


from arti_runtime.groq_chat import _groq_fallback_chain  # noqa: E402


def _trim_llm_payload(
    system_prompt: str,
    user_content: str,
    max_chars: int = 14000,
) -> tuple[str, str]:
    """Fit Groq's 413 retry, in order: history, optional blocks, the soul down to its head, then the rest.

    The current message, the fact lines and the soul's head go last of all (ADR 119). They can pass max_chars
    together: a slightly big retry beats one that answers without knowing what is true or who she is."""
    return groq_chat._trim_llm_payload(
        system_prompt, user_content, max_chars,
        _split_stardew_status=_split_stardew_status,
        _remove_system_prompt_block=_remove_system_prompt_block,
        _SYSTEM_PROMPT_BLOCK_MARKERS=_SYSTEM_PROMPT_BLOCK_MARKERS,
        SOUL_MARKER=SOUL_MARKER,
        re=re,
    )


def _openrouter_after_groq(
    system_prompt: str,
    user_content: str,
    cfg: dict,
    reason,
) -> tuple[str | None, str | None]:
    return groq_chat._openrouter_after_groq(
        system_prompt, user_content, cfg, reason,
        screen_privacy=screen_privacy,
        _groq_health_tl=_groq_health_tl,
        arti_provider_health=arti_provider_health,
        arti_openrouter=arti_openrouter,
    )


from arti_runtime.groq_chat import _groq_model_hilang  # noqa: E402


def groq_chat_completion(
    primary_model: str,
    system_prompt: str,
    user_content: str,
    config: dict | None = None,
) -> tuple[str | None, str | None]:
    """Groq dengan model pilihan (+ opsi roll semua model) → OpenRouter jika gagal."""
    return groq_chat._groq_chat_completion_inti(
        primary_model, system_prompt, user_content, config,
        default_config=CONFIG,
        _openrouter_after_groq=_openrouter_after_groq,
        _groq_fallback_chain=_groq_fallback_chain,
        screen_privacy=screen_privacy,
        live_max_tokens_for_trigger=live_max_tokens_for_trigger,
        REPLY_THINKING_HEADROOM_TOKENS=REPLY_THINKING_HEADROOM_TOKENS,
        requests=requests,
        arti_reply_policy=arti_reply_policy,
        _trim_llm_payload=_trim_llm_payload,
        _groq_model_hilang=_groq_model_hilang,
    )


# --- Kesehatan provider Groq (2026-10-23) ---------------------------------
# Pembungkus TIPIS, badan groq_chat_completion asli tidak disentuh. Groq
# dianggap gagal kalau alurnya jatuh ke _openrouter_after_groq (penanda
# thread-local); sukses kalau jawaban balik tanpa lewat sana. Groq mati /
# breaker terbuka -> langsung OpenRouter tanpa menembak 5 model Groq.
_groq_health_tl = threading.local()
_groq_chat_completion_inti = groq_chat_completion


def groq_chat_completion(  # noqa: F811 — sengaja membungkus versi inti di atas
    primary_model: str,
    system_prompt: str,
    user_content: str,
    config: dict | None = None,
) -> tuple[str | None, str | None]:
    """Groq + provider health (gerbang + catat), lalu perilaku lama apa adanya."""
    return groq_chat.groq_chat_completion(
        primary_model, system_prompt, user_content, config,
        default_config=CONFIG,
        arti_provider_health=arti_provider_health,
        _groq_chat_completion_inti=_groq_chat_completion_inti,
        _openrouter_after_groq=_openrouter_after_groq,
        _groq_health_tl=_groq_health_tl,
        _GROQ_TANPA_FALLBACK=_GROQ_TANPA_FALLBACK,
    )


_GROQ_TANPA_FALLBACK = object()


def _codex_cfg_untuk_kelas(user_speech: str, cfg: dict) -> dict:
    """Naikkan effort Luna khusus kelas jawaban berat (usul Bohan 27 Agu).

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
        # "[recorded remark omitted]" di tiap giliran — termasuk giliran
        # yang dijawab agy dan Luna tidak pernah dipanggil (sesi live 27 Agu:
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


ATTENTION_SYSTEM_KINDS = ("cut_in", "focus", "scene_ended")


def _attention_skip_without_primary_lane(trigger_type: str, cursor_route: bool) -> bool:
    """Attention-desk system turns with no Codex/Cursor lane stay silent (never Groq)."""
    if trigger_type in ATTENTION_SYSTEM_KINDS and not cursor_route:
        print(f"[Attention] {trigger_type} skipped: no primary voice lane (no fallback reply)")
        return True
    return False


def _attention_no_provider_fallback(trigger_type: str) -> bool:
    """True when Codex/Cursor failure must not fall through to Groq or canned speech."""
    return trigger_type in ATTENTION_SYSTEM_KINDS


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
    # hidup DI DALAM _cursor_reply_with_fallback. Audit 20 Agu menemukan Luna
    # TERSANDERA di sini: kalau Cursor mati (kunci hilang, SDK rusak, scratch
    # dir terhapus), fungsi ini balik False, jalurnya tak pernah dipanggil,
    # dan seluruh siaran turun SENYAP ke Groq — padahal Luna sehat walafiat.
    # penyedia kedua justru dibangun supaya tidak sekolam dengan Cursor.
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


# Alasan gagal agy yang SUNGGUH kegagalan provider (masuk breaker). "cold",
# "busy", "screen_privacy", "disabled", "cancelled" bukan salah provider.
_AGY_GAGAL_SUNGGUH = frozenset(
    {"timeout", "crash", "broken_pipe", "empty_or_failed", "forbidden_event"}
)


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
    if CONFIG.get("voice_append_context"):
        if seed := getattr(prompt_content, "voice_seed", None):
            seed.pop("refusal_ack", None)
    if (cfg.get("voice_api_enabled") and trigger_type in set(cfg.get("voice_api_trigger_types") or ())
            and ("PYTEST_CURRENT_TEST" not in os.environ or cfg.get("_voice_api_tes"))):
        try:
            from arti_runtime.openai_voice import openai_voice_reply
            _api, _meta = await openai_voice_reply(llm_system, prompt_content, cfg)
            if _api:
                print("[VoiceAPI] %s %.3fs" % ((_meta or {}).get("model"), (_meta or {}).get("latency_sec") or 0))
                return _api, _sentences_or_empty(_api), "openai_api"
        except Exception:
            pass
    timeout_s = float(cfg.get("cursor_timeout_sec", 5.0))

    cfg_luna = _codex_cfg_untuk_kelas(user_speech, cfg)
    if CONFIG.get("voice_append_context"):
        cfg_luna = dict(cfg_luna, _voice_admission=arti_codex_agent.TurnAdmission())

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
    if _agy_utama and not arti_provider_health.is_available("agy"):
        # Agy mati sesi ini / breaker terbuka: langsung Luna, tanpa baris
        # "utama gagal" tiap giliran (smoke 23 Okt). Log mati sudah SEKALI.
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
    warm = await arti_executors.run("llm", arti_cursor_agent.prewarm, cfg)
    # penyedia kedua ikut dipanaskan (no-op instan saat dimatikan) —
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
            result = await arti_executors.run("llm",
                arti_agy_agent.send_turn, llm_system, prompt_content, cfg
            )
            if result.ok and result.text:
                arti_provider_health.record_success("agy")
                return result.text, _sentences_or_empty(result.text), "agy"
            if result.reason in _AGY_GAGAL_SUNGGUH:
                arti_provider_health.record_failure(
                    "agy", arti_provider_health.classify(result.reason, default="unavailable"))
            print(f"[Agy] utama gagal ({result.reason}) — jatuh ke Luna")
        except asyncio.CancelledError:
            arti_agy_agent.abort_turn("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Agy] utama gagal: {type(exc).__name__} — jatuh ke Luna")

    # MODE LUNA UTAMA (eksperimen): coba Codex SEBELUM composer.
    # Gagal apa pun -> jatuh mulus ke alur composer lama di bawah (composer
    # tetap hangat sebagai cadangan). Trigger berharga (video/donation)
    # SENGAJA ikut Luna — eksperimennya justru mengukur kualitas+kuota.
    _luna_utama = bool(cfg.get("codex_primary_voice", False) or _agy_attempted)
    if _luna_utama and "PYTEST_CURRENT_TEST" in os.environ and not cfg.get(
        "_codex_primary_tes"
    ):
        # Pagar pytest (pelajaran prewarm 19 Agu): CONFIG produksi bocor ke
        # tes lama via config_local — tanpa pagar, suite memanggil Luna/Groq
        # BETULAN lewat blok ini. Tes blok ini memakai kunci privat.
        _luna_utama = False
    if _luna_utama:
        try:
            reply = await arti_executors.run("llm",
                arti_codex_agent.send_turn, llm_system, prompt_content, cfg_luna
            )
            if CONFIG.get("voice_append_context"):
                if isinstance(reply, voice_context.VoiceRefusal):
                    print(f"[Codex] append_context_refusal reason={reply.reason}")
                    if trigger_type in attention_desk.OWNER_KINDS or trigger_type in ATTENTION_SYSTEM_KINDS:
                        return voice_context.refusal_acknowledgement(
                            prompt_content, incharacter_fallback_reply(user_speech)), [], "incharacter"
            if reply:
                return reply, _sentences_or_empty(reply), "codex"
        except asyncio.CancelledError:
            if CONFIG.get("voice_append_context"):
                arti_codex_agent.abort_turn("cancelled", turn_id=cfg_luna["_voice_admission"])
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Codex] utama gagal: {type(exc).__name__}")
        # Luna gagal: JANGAN kaskade ke composer — log 20 Agu 16.34: pagar
        # Luna (8s) + kapak composer (16s) menumpuk jadi llm p50 10,6 dtk /
        # p90 27 dtk. Langsung Groq (cepat); alur composer di bawah hanya
        # jaring TERAKHIR kalau Groq ikut mati (anti-bisu tetap dijaga).
        if not _attention_no_provider_fallback(trigger_type):
            print("[Codex] utama gagal/kosong — mencoba fallback Groq (composer dilewati)")
            try:
                model = pick_groq_model(_extract_trigger_message(user_speech), cfg)
                reply, used = await arti_executors.run("llm",
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
        # klip/alert. Reaksi video berbayar yang kena sesi dingin bisa dijawab
        # model kecil yang jauh lebih buruk. Tunggu pemanasan (bounded) alih-alih lempar ke 8B.
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
        print("[Cursor] sesi belum hangat — mencoba Luna lalu fallback Groq, pemanasan jalan")
        # Lapis antara (19 Agu): Codex/Luna hangat 1,5-3 dtk — coba dulu
        # sebelum Groq. Default MATI (codex_agent_enabled); gagal apa pun
        # -> None -> Groq seperti biasa.
        try:
            reply = await arti_executors.run("llm",
                arti_codex_agent.send_turn, llm_system, prompt_content, cfg_luna
            )
            if CONFIG.get("voice_append_context"):
                if isinstance(reply, voice_context.VoiceRefusal):
                    print(f"[Codex] append_context_refusal reason={reply.reason}")
                    if trigger_type in attention_desk.OWNER_KINDS or trigger_type in ATTENTION_SYSTEM_KINDS:
                        return voice_context.refusal_acknowledgement(
                            prompt_content, incharacter_fallback_reply(user_speech)), [], "incharacter"
            if reply:
                return reply, _sentences_or_empty(reply), "codex"
        except asyncio.CancelledError:
            if CONFIG.get("voice_append_context"):
                arti_codex_agent.abort_turn("cancelled", turn_id=cfg_luna["_voice_admission"])
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[Codex] lapis cadangan gagal: {type(exc).__name__}")
        if not _attention_no_provider_fallback(trigger_type):
            try:
                model = pick_groq_model(_extract_trigger_message(user_speech), cfg)
                reply, used = await arti_executors.run("llm",
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
        if _attention_no_provider_fallback(trigger_type):
            return None, [], "skip-attention"
        if cfg.get("cursor_last_resort_incharacter", True):
            return incharacter_fallback_reply(user_speech), [], "incharacter"
        return None, [], "gagal"

    reason = "?"
    try:
        # `wait_for` di sini adalah pengaman UTAMA, bukan cadangan: deadline internal
        # collect_run_messages tidak bisa menghentikan iterator yang menggantung —
        # terbukti satu sampel lolos jadi 41 detik padahal timeout 30.
        res = await asyncio.wait_for(
            arti_executors.run("llm",
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
        await arti_executors.run("llm", arti_cursor_agent.prewarm, cfg)
    except Exception:  # noqa: BLE001
        pass

    # Lapis antara (19 Agu): Codex/Luna sebelum Groq — thread hangat
    # 1,5-3 dtk, kualitas persona Indonesia terbukti probe. Default MATI.
    try:
        reply = await arti_executors.run("llm",
            arti_codex_agent.send_turn, llm_system, prompt_content, cfg_luna
        )
        if CONFIG.get("voice_append_context"):
            if isinstance(reply, voice_context.VoiceRefusal):
                print(f"[Codex] append_context_refusal reason={reply.reason}")
                if trigger_type in attention_desk.OWNER_KINDS or trigger_type in ATTENTION_SYSTEM_KINDS:
                    return voice_context.refusal_acknowledgement(
                        prompt_content, incharacter_fallback_reply(user_speech)), [], "incharacter"
        if reply:
            return reply, _sentences_or_empty(reply), "codex"
    except asyncio.CancelledError:
        if CONFIG.get("voice_append_context"):
            arti_codex_agent.abort_turn("cancelled", turn_id=cfg_luna["_voice_admission"])
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[Codex] lapis cadangan gagal: {type(exc).__name__}")

    # Groq. Fungsi ini sudah merantai seluruh model Groq lalu jatuh ke
    # _openrouter_after_groq sendiri, jadi lapis ketiga ikut gratis.
    if not _attention_no_provider_fallback(trigger_type):
        try:
            model = pick_groq_model(_extract_trigger_message(user_speech), cfg)
            reply, used = await arti_executors.run("llm",
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
    if _attention_no_provider_fallback(trigger_type):
        return None, [], "skip-attention"
    if cfg.get("cursor_last_resort_incharacter", True):
        text = incharacter_fallback_reply(user_speech)
        return text, [], "incharacter"
    return None, [], "gagal"


# Re-exported from arti_runtime/reply_text.py; calls inside reply_text do not see bridge rebinding.
from arti_runtime.reply_text import (
    _sentences_or_empty,
    _CJK_RE,
    _shorten_viewer_handles,
)  # noqa: E402


def post_process_response(text, user_speech=None, config=None, *, rehearsal_call=None):
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

    # Target rencana (yang disebut di prompt) -> batas AMAN pemotong.
    max_sent, max_chars = get_arti_reply_limits(user_speech or "", config)
    max_sent, max_chars = arti_reply_policy.reply_hard_cap(
        max_sent, max_chars, config or CONFIG
    )

    # Giliran mic_pasif: tidak ada yang bertanya, jadi pengganti kaleng = diam.
    jadi_diam = bool((config or {}).get("_fallback_jadi_diam"))
    text, removed = filter_meta_history_talk(text)
    if not text or removed > 0 or is_narrator_reply(text):
        if jadi_diam:
            print("[Filter] Jawaban meta/narrator di giliran pasif — diam.")
            return ""
        fb = incharacter_fallback_reply(user_speech or "")
        if rehearsal_call is not None:
            rehearsal_call["rehearsal_source"] = "incharacter"
        why = "semua meta" if not text else f"{removed} kalimat meta / narrator"
        print(f"[Filter] Ganti jawaban ({why}) -> {fb[:60]}...")
        return strip_tts_expression_tags(fb)

    result = _truncate_reply_length(
        text, max_sent, max_chars,
        runon_chars=int((config or CONFIG).get("arti_reply_hard_cap_chars", 900)),
    )
    if is_narrator_reply(result):
        if jadi_diam:
            return ""
        if rehearsal_call is not None:
            rehearsal_call["rehearsal_source"] = "incharacter"
        return strip_tts_expression_tags(incharacter_fallback_reply(user_speech or ""))
    if result != re.sub(r"\s+", " ", text).strip():
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
_idle_reconnect_backoff = 0.0  # 0 → 1 → 2 … 30 s; reset by a successful send
main_event_loop = None


def _idle_ws_ok() -> bool:
    if _idle_ws is None:
        return False
    try:
        return getattr(_idle_ws.state, "name", None) == "OPEN" and _idle_ws.close_code is None
    except Exception:
        return False

# --- Track 1: Motion Hotkeys (smooth body movement) ---
# Motion BERSIH (motion tanpa kurva kepala/mulut, buatan sendiri), bukan ArtiIdle
# mentah. Diganti 27 Agu sesudah bukti live: motion mentah menulis 62
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
_idle_model_warning = False
_idle_face_y_queue: queue.SimpleQueue = queue.SimpleQueue()  # nod mirror → idle ws
_idle_hotkey_cmd_queue: queue.SimpleQueue = queue.SimpleQueue()  # hotkey / deactivate cmds
_idle_hotkey_cache: dict[str, str] = {}  # name -> hotkeyID (diisi saat idle connect)
_idle_active_expr: str | None = None
_idle_active_expr_lock = threading.Lock()
_idle_worker_loop: asyncio.AbstractEventLoop | None = None
# T3-22 (smoke 2026-10-24: 12x "[recorded remark omitted]" + "[recorded remark omitted]" tanpa jejak koneksi mana/kenapa). Setiap koneksi idle dapat
# nomor urut untuk log; deactivate yang gagal karena socket putus diulang
# sesudah reconnect supaya ekspresi ArtiIdle tidak tertinggal nyala di VTS.
_idle_ws_seq = 0
_idle_ws_id = 0
_idle_pending_deactivate: str | None = None


def _idle_ws_label() -> str:
    return f"ws#{_idle_ws_id}" if _idle_ws_id else "ws#-"


def _idle_fail_reason(error: BaseException) -> str:
    """Pisahkan timeout balasan dari socket putus; ConnectionClosed(None, None)
    sintetis dari _idle_send berarti socket sudah diganti/ditutup."""
    if isinstance(error, asyncio.TimeoutError):
        return (f"timeout {float(CONFIG.get('idle_vts_request_timeout_sec', 3.0)):.1f}s "
                "menunggu balasan")
    if isinstance(error, websockets.exceptions.ConnectionClosed):
        if getattr(error, "rcvd", None) is None and getattr(error, "sent", None) is None:
            return "socket sudah diganti/ditutup (tanpa close frame)"
        return f"closed: {error}"
    return f"{type(error).__name__}: {error}"


def _idle_ws_kwargs() -> dict:
    """websockets.connect kwargs untuk idle ws: keepalive ping dari config."""
    kwargs = {
        "open_timeout": float(CONFIG.get("idle_vts_connect_timeout_sec", 20)),
        "close_timeout": 5,
    }
    for key, cfg in (("ping_interval", "idle_vts_ping_interval_sec"),
                     ("ping_timeout", "idle_vts_ping_timeout_sec")):
        try:
            value = float(CONFIG.get(cfg, 20.0))
        except (TypeError, ValueError):
            value = 20.0
        kwargs[key] = value if value > 0 else None
    return kwargs


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
        # Bukan lagi "MODE UJI": sejak 27 Agu rotasi bawaannya memang motion
        # bersih tanpa kurva kepala/mulut, jadi ini setelan yang dimaksudkan.
        # Syaratnya tetap ditulis supaya orang yang memakai motion mentah
        # tahu kenapa angguknya hilang.
        print(
            "[Idle] Motion JALAN TERUS saat bicara "
            "(idle_motion_lanjut_saat_bicara=true). Syarat: motion tanpa "
            "kurva kepala/mulut (buat sendiri dari motion model kamu)."
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
    global _idle_model_warning, _idle_ws_seq, _idle_ws_id
    for attempt in range(max_attempts):
        # Main owns token approval/renewal. Never poll a missing token file.
        while vts is None or not vts._auth_ready.is_set():
            await asyncio.sleep(0.1)
        ws = None
        try:
            port = vts.port
            uri = f"ws://localhost:{port}"
            ws = await websockets.connect(uri, **_idle_ws_kwargs())
            token = arti_vts_auth.read_token(__file__, port, main_port=vts.port)
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
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
            if arti_vts_auth.response_data(resp).get("authenticated") is True:
                expected = CONFIG.get("vts_expected_model_name", "")
                if expected:
                    await ws.send(json.dumps({
                        "apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                        "requestID": "IdleModel", "messageType": "CurrentModelRequest",
                        "data": {},
                    }))
                    model = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                    if not arti_vts_auth.model_matches(model, expected):
                        if not _idle_model_warning:
                            print(f"[Idle] MODEL GUARD: port {port} bukan model {expected!r}; kontrol DIBLOKIR. Periksa port ARTI/HOST!")
                            _idle_model_warning = True
                        await ws.close()
                        return None
                _idle_model_warning = False
                _idle_ws_seq += 1
                _idle_ws_id = _idle_ws_seq
                print(f"[Idle] Dedicated VTS connection ready ✓ ({_idle_ws_label()})")
                return ws
            print(f"[Idle] Auth ditolak VTS (attempt {attempt + 1})")
            vts._conn_lost = True
            vts._auth_ready.clear()  # Main renews the token; idle never deletes it.
            await ws.close()
            ws = None
        except asyncio.CancelledError:
            await _idle_discard_ws(ws)
            raise
        except Exception as e:
            if isinstance(e, arti_vts_auth.VTSAPIError) and e.error_id in (8, 50, 100):
                vts._conn_lost = True
                vts._auth_ready.clear()
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
    global _idle_ws_lock, _idle_reconnect_backoff
    if _idle_ws_lock is None:
        _idle_ws_lock = asyncio.Lock()
    async with _idle_ws_lock:
        if ws is None or ws is not _idle_ws or not _idle_ws_ok():
            raise websockets.exceptions.ConnectionClosed(None, None)
        timeout = float(CONFIG.get("idle_vts_request_timeout_sec", 3.0) or 3.0)
        try:
            await asyncio.wait_for(ws.send(json.dumps(payload)), timeout=timeout)
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            _idle_reconnect_backoff = 0.0
            return resp
        except (Exception, asyncio.CancelledError) as e:
            # A reply may still be in flight. Never reuse this request stream.
            if not isinstance(e, asyncio.CancelledError):
                print(f"[Idle] {_idle_ws_label()} putus saat "
                      f"{(payload or {}).get('requestID', '?')}: {_idle_fail_reason(e)}")
            await _idle_discard_ws(ws)
            raise


async def _idle_discard_ws(ws):
    global _idle_ws
    if ws is _idle_ws:
        _idle_ws = None
    if ws is not None:
        try:
            await asyncio.wait_for(ws.close(), timeout=1.0)
        except Exception:
            pass


async def _idle_reconnect(ws):
    """One reconnect owner, sharing the request lock with both idle tracks."""
    global _idle_ws, _idle_ws_lock, _idle_reconnect_backoff
    if _idle_ws_lock is None:
        _idle_ws_lock = asyncio.Lock()
    async with _idle_ws_lock:
        if _idle_ws_ok():
            return _idle_ws  # Another track already replaced the failed socket.
        await _idle_discard_ws(ws)
        # MODEL GUARD / auth refusal returns None at once and the worker
        # recreates the track 0.2 s later: without this wait that is ~5
        # re-auths per second. Only a successful send resets it.
        if _idle_reconnect_backoff:
            await asyncio.sleep(_idle_reconnect_backoff)
        _idle_reconnect_backoff = min(30.0, max(1.0, _idle_reconnect_backoff * 2))
        _idle_ws = await _idle_connect_ws()
        fresh = _idle_ws
    # Outside the request lock (_idle_send takes it): replay the deactivate
    # that failed while the socket was down, so no ArtiIdle stays lit in VTS.
    if fresh is not None and _idle_pending_deactivate and _idle_ws_ok():
        pending = _idle_pending_deactivate
        print(f"[Idle] {_idle_ws_label()} ulang deactivate {pending} yang tertunda")
        await _idle_deactivate_expression(pending)
    return fresh


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
    global _idle_pending_deactivate
    name = (expr_name or _get_idle_active_expr() or "").strip()
    if not name:
        return False
    if not _idle_ws_ok():
        # Socket sedang reconnect: VTS tetap memegang ekspresi ini. Tunda,
        # _idle_reconnect mengulanginya begitu koneksi baru siap.
        if _idle_pending_deactivate != name:
            print(f"[Idle] Deactivate {name} ditunda — {_idle_ws_label()} belum tersambung")
        _idle_pending_deactivate = name
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
        if _idle_pending_deactivate == name:
            _idle_pending_deactivate = None
        print(f"[Idle] Deactivated {name}")
        return True
    except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError) as e:
        _idle_pending_deactivate = name
        print(f"[Idle] Gagal deactivate {name} ({_idle_fail_reason(e)}); diulang sesudah reconnect")
        return False
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

    # DUA TRACK DIURUS TERPISAH (26 Agu 2026, permintaan Bohan: "[recorded remark omitted]").
    #
    # Dulu keduanya digerbangi satu bendera `idle_timer_running`, jadi begitu
    # giliran mulai KEDUANYA mati. `idle_motion_lanjut_saat_bicara` cuma
    # melewati hotkey penghenti, sehingga motion yang SEDANG main boleh habis
    # (10 dtk) — tapi tidak ada motion baru sepanjang sisa giliran, padahal
    # giliran p50-nya 29,7 dtk (sesi 26 Agu). Hasilnya Arti tetap membeku
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
    rediscover = False  # last hotkey lookup failed; retry before next motion

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

            if rediscover and _idle_ws_ok():
                found = await _discover_motion_hotkey_ids(_idle_ws)
                if found:
                    motion_ids, motion_names, rediscover = found, list(found), False

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
        except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError) as e:
            print(f"[Idle/Motion] VTS {_idle_ws_label()} {_idle_fail_reason(e)} — reconnecting...")
            _idle_ws = await _idle_reconnect(_idle_ws)
            if not _idle_ws:
                print("[Idle/Motion] Reconnect failed, stopping motion track.")
                return
            found = await _discover_motion_hotkey_ids(_idle_ws)
            if found:
                motion_ids, motion_names, rediscover = found, list(found), False
            else:
                # Lookup failed (timeout drops the socket): keep the known ids;
                # the next send reconnects and looks them up again.
                rediscover = True
                print("[Idle/Motion] Hotkey lookup gagal — pakai daftar lama, coba lagi nanti.")
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

        except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError) as e:
            global _idle_expr_backoff, _idle_ws
            idle_expression_active = False
            _idle_expr_backoff = min(15.0, (_idle_expr_backoff or 2.0) * 1.5)
            print(f"[Idle/Expr] VTS {_idle_ws_label()} {_idle_fail_reason(e)}, "
                  f"backoff {_idle_expr_backoff:.1f}s...")
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
    if not CONFIG.get("vts_enabled", True):
        return
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
_stardew_runtime = None
_stardew_ptt_epoch = None
_stardew_discarded_voice_epoch = None
_stardew_bridge_trial = None
_live7_help_relay = None


def _game_trigger_waiting() -> bool:
    """A moment is spoken only into an EMPTY voice queue, so it can never land
    ahead of (or behind-and-delay) Bohan's PTT or any other pending turn
    (REVIEW_MOMENTS F4). The name stays for the relay's gate argument."""
    if CONFIG.get("voice_queue_enabled", False):
        return len(voice_trigger_buffer) > 0
    return voice_trigger_queue.qsize() > 0


def _own_play_can_wait():
    """The priority buffer may retain one fresh play line behind chat, without raising its rank."""
    return bool(CONFIG.get("live7_reactive_play_enabled") and CONFIG.get("voice_queue_enabled"))


def _own_play_min_gap():
    if CONFIG.get("live7_reactive_play_enabled"):
        from arti_runtime.reactive_play import number
        return number(CONFIG, "live7_play_line_gap_sec", 45)
    try:
        gap = float(CONFIG.get("own_play_min_gap_sec") or 0)
    except (TypeError, ValueError):
        return 0.0
    return gap if gap > 0 else 0.0


def _owner_speaking_now():
    return bool(hotkey_active or attention_bridge.desk(__import__(__name__)).owner_speaking(time.time()))


def _queued_trigger_types():
    if CONFIG.get("voice_queue_enabled", False):
        return [it.trigger_type for it in voice_trigger_buffer.snapshot()]
    with voice_trigger_queue.mutex:
        return [getattr(t, "trigger_type", None) for t in voice_trigger_queue.queue]


def _own_play_held_back(*, admit=False):
    gap = _own_play_min_gap()
    if not gap:
        return False
    kinds = _queued_trigger_types()
    if admit and CONFIG.get("live7_reactive_play_enabled") and _own_play_can_wait():
        kinds = [kind for kind in kinds if kind != "yt_chat"]
    if _owner_speaking_now() or any(k in _OWN_PLAY_YIELD_KINDS for k in kinds):
        return True
    if admit and "own_play" in kinds:
        return True
    return _last_own_play_voiced_at is not None and time.time() - _last_own_play_voiced_at < gap


def _note_own_play_voiced(at=None):
    global _last_own_play_voiced_at
    _last_own_play_voiced_at = time.time() if at is None else at


def _own_play_lane_busy():
    if CONFIG.get("live7_reactive_play_enabled") and _own_play_can_wait():
        return hotkey_active or _own_play_held_back(admit=True)
    return not attention_desk.enabled(CONFIG) and (_brain_busy or tts_is_playing or hotkey_active or _game_trigger_waiting())


def _start_live7_help_if_enabled() -> None:
    global _live7_help_relay
    help_dir = CONFIG.get("stardew_live7_help_dir") or CONFIG.get("live7_help_dir", "")
    # Status facts run independently of optional moments and help admission.
    if (_live7_help_relay is not None or not (
            CONFIG.get("live7_help_enabled", False) or CONFIG.get("live7_moments_enabled", False)
            or CONFIG.get("live7_status_facts_enabled", True) or CONFIG.get("host_story_relay_file")
            or CONFIG.get("livepath_mode") == "mabar")):
        return
    from arti_help_relay import HelpRelay, Live7HelpChannelResolver

    # Canonical key from the Stardew side; the design spelling stays an alias.
    # The file channel follows the active Live7 cycle's run dir (re-resolved
    # every few seconds); the static dir is only the no-active-cycle fallback.
    resolver = Live7HelpChannelResolver(CONFIG, help_dir)
    relay = HelpRelay(CONFIG, queue_voice_trigger, runtime=_stardew_runtime,
                      file_channel=None if CONFIG.get("live7_status_facts_enabled", True) else resolver(),
                      channel_resolver=resolver, viewer_waiting=_viewer_chat_waiting,
                      game_trigger_waiting=_own_play_lane_busy,
                      status_voice_ready=_live7_status_voice_ready)
    _live7_help_relay = relay
    relay.start()
    _register_live7_help_sink()


def _live7_status_voice_ready():
    # Memory only: never enqueue while a voice turn/PTT is active, and leave a
    # short gap after ordinary replies as well as the shared moments gap.
    if _brain_busy or tts_is_playing or hotkey_active:
        return False
    return time.time() - _last_arti_reply_ts >= 10.0


def _live7_status_block_now():
    relay = _live7_help_relay
    if relay is None:
        return ""
    return relay.status_block() if CONFIG.get("live7_status_facts_enabled", True) else ""


def _live7_reply_facts_now(*, include_host_story=True):
    relay = _live7_help_relay
    if relay is None:
        return ""
    return relay.reply_facts() if include_host_story else relay.reply_facts(include_host_story=False)


def _register_live7_help_sink() -> None:
    """(Re-)bind the relay to the current Stardew runtime, e.g. after a re-create."""
    relay = _live7_help_relay
    if relay is None or not relay.config.get("live7_help_enabled", True):
        return
    runtime = _stardew_runtime
    relay.set_runtime(runtime)
    sink = getattr(runtime, "set_help_sink", None)
    if callable(sink):
        sink(relay.submit)


def _stop_live7_help_relay() -> None:
    global _live7_help_relay
    relay = _live7_help_relay
    _live7_help_relay = None
    if relay is not None:
        relay.stop()


def _prepare_stardew_bridge_trial_if_requested() -> None:
    """Validate an explicit bridge trial before any live bridge startup effect."""
    global _stardew_bridge_trial
    from arti_stardew_trial_session import prepare_bridge_trial

    _stardew_bridge_trial = prepare_bridge_trial()


def _start_stardew_runtime_if_enabled() -> None:
    """Opt-in G1-0. Default OFF = nol receiver, nol inferensi."""
    global _stardew_runtime
    _stardew_runtime = None
    trial = globals().get("_stardew_bridge_trial")
    if trial is not None:
        _stardew_runtime = trial.start_runtime()
    elif CONFIG.get("stardew_runtime_enabled"):
        # Never two Stardew drivers: an operator-hosted Live7 cycle owns the game.
        try:
            import arti_stardew_driver_guard
            live7_run = arti_stardew_driver_guard.active_live7_run_dir(CONFIG)
        except Exception:  # noqa: BLE001 — guard is advisory; unreadable = not active
            live7_run = None
        if live7_run:
            print(f"[Stardew] runtime NOT started: Live7 cycle aktif di {live7_run} "
                  "(operator-hosted); matikan stardew_runtime_enabled")
        else:
            import arti_stardew_runtime as stardew_runtime

            _stardew_runtime = stardew_runtime.construct_if_enabled(CONFIG)
    # Re-bind the Live7 help relay to a re-created runtime (no-op when absent).
    register = globals().get("_register_live7_help_sink")
    if callable(register):
        register()


def _shutdown_codex_clients(timeout: float = 3.0) -> None:
    """Exit path (incl. Ctrl+C / Ctrl+Break): close the Codex app-servers
    synchronously so no codex.exe outlives the bridge. Only modules that were
    actually imported; each close bounded to ``timeout`` s; never raises."""
    if "arti_codex_role" in sys.modules:
        try:
            sys.modules["arti_codex_role"].shutdown(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            print(f"[Codex] role shutdown warning: {type(e).__name__}")
    if "arti_codex_agent" in sys.modules:
        # Voice client has no shutdown API; abort_turn() closes it inline.
        try:
            closer = threading.Thread(
                target=sys.modules["arti_codex_agent"].abort_turn, args=("shutdown",),
                daemon=True, name="codex-voice-close")
            closer.start()
            closer.join(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            print(f"[Codex] voice shutdown warning: {type(e).__name__}")


def _atexit_close_codex_voice() -> None:
    """Second net for the voice Codex client (a second Ctrl+C during the
    observer step skips the finally's close). Synchronous, no thread
    (interpreter shutdown); bounded by the SDK's own terminate/wait(2)/kill.
    Idempotent: abort_turn() on a closed client is a no-op."""
    mod = sys.modules.get("arti_codex_agent")
    if mod is None:
        return
    try:
        mod.abort_turn("shutdown")
    except BaseException:  # noqa: BLE001 — never block interpreter exit
        pass


import atexit as _atexit  # noqa: E402

_atexit.register(_atexit_close_codex_voice)


def _stop_stardew_runtime() -> None:
    global _stardew_runtime, _stardew_bridge_trial
    runtime = _stardew_runtime
    _stardew_runtime = None
    relay = globals().get("_live7_help_relay")
    if relay is not None:
        relay.set_runtime(None)
    trial = globals().get("_stardew_bridge_trial")
    _stardew_bridge_trial = None
    if trial is not None:
        trial.stop("bridge_shutdown")
    elif runtime is not None:
        runtime.stop()


def _note_stardew_bridge_trial_mic(epoch) -> None:
    """Record only mic provenance; the trial never receives audio or transcript."""
    trial = globals().get("_stardew_bridge_trial")
    if trial is not None:
        trial.note_mic_event(actual=True, epoch=epoch)


def _note_stardew_bridge_trial_ptt_capture() -> None:
    """Count the ordinary hotkey capture without retaining captured content."""
    trial = globals().get("_stardew_bridge_trial")
    if trial is not None:
        trial.note_ptt_capture()


def _note_stardew_owner_ingress(trigger_type, viewer_name, text=None, epoch=None):
    runtime = _stardew_runtime
    trial = globals().get("_stardew_bridge_trial")
    if trial is not None and not trial.allows_owner_ingress(trigger_type):
        return None
    runtime_config = trial.config if trial is not None else CONFIG
    if runtime is not None:
        begin = getattr(runtime,"begin_owner_voice",None)
        if callable(begin):
            if epoch is not None and not runtime.owner_voice_current(epoch): return -1
            if epoch is None: epoch=begin(trigger_type=trigger_type,viewer_name=viewer_name,config=runtime_config)
            if text is not None:
                runtime.accept_owner_stop(text,trigger_type=trigger_type,viewer_name=viewer_name,config=runtime_config)
                if trial is not None:
                    trial.note_owner_turn(epoch)
            return epoch
        runtime.note_owner_ingress(trigger_type=trigger_type,viewer_name=viewer_name,config=runtime_config)


def _finish_stardew_owner_voice(trigger):
    global _stardew_discarded_voice_epoch
    runtime=_stardew_runtime
    epoch=getattr(trigger,"stardew_voice_epoch",None)
    if runtime is not None and epoch is not None:
        runtime.end_owner_voice(epoch)
    pending=_stardew_discarded_voice_epoch
    _stardew_discarded_voice_epoch=None
    if runtime is not None and pending is not None:
        runtime.end_owner_voice(pending)


def _discard_stardew_capture(epoch):
    """A discarded capture ends after existing speech cleanup, never during TTS."""
    global _stardew_discarded_voice_epoch
    runtime=_stardew_runtime
    if runtime is None or epoch is None or not runtime.owner_voice_current(epoch):return
    if tts_is_playing or _brain_busy:
        _stardew_discarded_voice_epoch=epoch
    else:runtime.end_owner_voice(epoch)


def _transcribe_stardew_ptt(audio_array, samplerate, epoch):
    try:return transcribe_audio(audio_array,samplerate,use_groq=True)
    except Exception:
        _discard_stardew_capture(epoch)
        raise


def _complete_stardew_asr(text, epoch, audio_duration_sec, asr_stages, capture_generation=None):
    """Existing PTT transcript filters, with the capture's original generation."""
    global _asr_ptt_cooldown_until
    runtime=_stardew_runtime
    # Some unit seams compile this function alone; normal module execution
    # always defines the provenance hook above.
    if "_note_stardew_bridge_trial_mic" in globals():
        _note_stardew_bridge_trial_mic(epoch)
    if runtime is not None and epoch is not None and not runtime.owner_voice_current(epoch):
        return False
    if capture_generation is not None and capture_generation != _ptt_capture_generation:
        _discard_stardew_capture(epoch)
        return False
    if text and text.strip() in _ASR_LITERAL_PHANTOMS:
        _asr_ptt_cooldown_until=time.time()+2.5
        text=None
    if text and is_asr_echo_of_arti(text): text=None
    if not text:
        _discard_stardew_capture(epoch)
        return False
    if CONFIG.get("stream_game_notes"):
        _note_screen_privacy(text)
        from arti_runtime import stream_notes
        stream_notes.bridge_line(__import__(__name__), "owner", "ptt", text, owner_marked=True)
    accepted=queue_voice_trigger(text,trigger_type="ptt",asr_stages=asr_stages,stardew_voice_epoch=epoch,
                                 ptt_generation=capture_generation)
    if accepted: summarizer_queue.put(text)
    return accepted


def _try_stardew_owner_generation(trigger):
    """(speech, skip_reply_tags). speech None = jalur percakapan lama."""
    if trigger.trigger_type == "own_play" and getattr(trigger.text, "direct_speech", None):
        return streamer_voice.natural_intent(trigger.text.direct_speech) or "", True
    runtime = _stardew_runtime
    if runtime is None:
        return None, False
    trial = globals().get("_stardew_bridge_trial")
    if trial is not None and not trial.allows_owner_ingress(
        getattr(trigger, "trigger_type", "") or ""
    ):
        return None, False
    epoch=getattr(trigger,"stardew_voice_epoch",None)
    if epoch is not None and not runtime.owner_voice_current(epoch):
        return "", True
    import arti_stardew_runtime as stardew_runtime

    result = runtime.decide_owner_turn(
        text=getattr(trigger, "text", "") or "",
        trigger_type=getattr(trigger, "trigger_type", "") or "",
        viewer_name=getattr(trigger, "viewer_name", None),
        config=trial.config if trial is not None else CONFIG,
    )
    if not result.admitted:
        return None, False
    speech = stardew_runtime.sanitize_trusted_speech(result.speech or "", CONFIG)
    return speech, True


def _kept_owner_conversation_only(trigger):
    if not getattr(trigger, "owner_reply_required", False):
        return False
    epoch = getattr(trigger, "stardew_voice_epoch", None)
    return (isinstance(trigger.text, arti_voice_queue.CombinedOwnerQuestions)
            or (epoch is not None and _stardew_runtime is not None
                and not _stardew_runtime.owner_voice_current(epoch)))


def _maybe_execute_reply_tags(reply, trigger, skip_reply_tags):
    """Ucapan G1 tepercaya melewati tag MODE/MC; jalur biasa tetap mengeksekusi."""
    if not reply:
        return reply
    if CONFIG.get("live_voice_fixes_v1") and "[" in reply:  # ADR 233 (V12), V6.5: the play tag, never spoken
        reply = live_rules.take_play_tag(__import__(__name__), reply, trigger, skip_reply_tags)
    if skip_reply_tags:
        import arti_stardew_runtime as stardew_runtime

        return stardew_runtime.sanitize_trusted_speech(reply, CONFIG)
    if "[" in reply and not skip_reply_tags:
        return _execute_reply_tags(
            reply, trigger.trigger_type, trigger.viewer_name
        )
    return reply


def _log_loop_error(counts, scope, error):
    """Count by exception type + origin; emit each traceback only once."""
    origin = error.__traceback__
    while origin.tb_next is not None:
        origin = origin.tb_next
    signature = (type(error), origin.tb_frame.f_code.co_filename, origin.tb_lineno)
    counts[signature] = counts.get(signature, 0) + 1
    print(f"[Error] {scope}: {type(error).__name__}: {error} count={counts[signature]}")
    if counts[signature] == 1:
        print("".join(traceback.format_exception(
            type(error), error, error.__traceback__, chain=False)))


def _start_provider_health_probe():
    """Probe provider SEKALI di awal (thread latar, total <= budget) + aktifkan
    breaker bersama. `provider_probe_enabled: False` = perilaku lama penuh.
    Permintaan Bohan 23 Okt: "[recorded remark omitted]"."""
    try:
        cfg = {
            **CONFIG,
            "openrouter_api_key": (
                CONFIG.get("openrouter_api_key")
                or os.environ.get("OPENROUTER_API_KEY")
                or openrouter_api_key
            ),
        }
        return arti_provider_health.start_startup_probe(cfg)
    except Exception as e:  # noqa: BLE001 — diagnostik tidak boleh menjatuhkan startup
        print(f"[Health] provider probe tidak jalan ({type(e).__name__}) — rantai lama")
        return None


def persona_prompt_tail(prompt: str, config: dict) -> str:
    """Shared by the live startup and TestModeBridge.build_persona: emotion guidance, then live play rules.

    With live_play_rules_enabled off this is byte-identical to the emotion step alone.
    """
    return live_rules.apply(arti_expression_runtime.emotion_prompt_for_system(prompt, config), config)


async def main_loop():
    reset_viewer_presence_session()
    reset_screen_privacy_session()
    print("=== HERMES VTUBER BRIDGE (CONTEXT BUFFER ENHANCED) ===")
    
    global main_event_loop, _brain_busy, _brain_busy_since, tts_is_playing, _tts_started_ts
    global hotkey_active, _pending_turn_id
    main_event_loop = asyncio.get_event_loop()

    # A requested supervised trial fails closed before VTS, runtime/provider,
    # hotkey, or microphone startup.  With no trial environment this is inert.
    _prepare_stardew_bridge_trial_if_requested()
    _start_provider_health_probe()
    
    # 1. Hubungkan ke VTube Studio
    global vts
    vts = VTSController()
    if CONFIG.get("vts_enabled", True):
        await vts.connect()
        vts.start_reconnect()
    
    # 2. Inisialisasi TTS
    # `tts` is module-level (declared global) so the __main__ finally cleanup can
    # reach tts.supertone for a bounded shutdown of the Supertone subprocess
    # (task 7.1, Req 10.5).
    global tts
    tts = TTSEngine()

    if CONFIG.get("tts_engine") == "supertone":
        if not _supertone_preflight():
            tts._disable_supertone(FileNotFoundError("venv312 interpreter missing"))
        elif CONFIG.get("supertonic_prewarm_on_startup", True):
            print("[TTS] Pre-warming Supertone (load model venv312)...")
            await tts.prewarm_supertone()
    
    # 3. Hotkey + YouTube (ASR mic setelah health check — hindari kalibrasi bentrok)
    global _yt_chat_attached_id, _yt_chat_thread_started
    if CONFIG.get("youtube_chat_enabled"):
        threading.Thread(target=youtube_chat_worker, daemon=True).start()
        start_yt_viewer_count_worker()
        _yt_chat_thread_started = True
        _yt_chat_attached_id = (CONFIG.get("youtube_video_id") or "").strip()
    elif CONFIG.get("ssn_chat_enabled"):
        threading.Thread(target=ssn_chat_worker, daemon=True).start()
    start_run_chat_id_poller()
    if CONFIG.get("minecraft_enabled"):
        # Runner TIDAK autostart — join lewat 'mc on' atau suruh verbal
        # ([MC: join] dari jawaban Arti). Di sini cuma pengumuman siaga.
        print(
            "[Minecraft] Siaga — 'mc on' untuk join, 'mc off' keluar, "
            "atau suruh Arti verbal ikut main"
        )
    _start_stardew_runtime_if_enabled()
    _start_live7_help_if_enabled()
    start_host_topic_workers()
    if CONFIG.get("asr_enabled", True):
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
        _health_fn = bridge_health.run_startup_health_check
        if os.environ.get("ARTI_REHEARSAL_CONFIG"):
            from scripts.livepath.bridge import rehearsal_health
            _health_fn = rehearsal_health
        _health = _health_fn(
            _hc_cfg,
            resolve_mic_fn=resolve_asr_input_device,
            vts=vts,
            tts=tts,
            hotkey_registered=hotkey_registered,
            interactive_mic_test=CONFIG.get("asr_enabled", True),
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
            rag_row = await arti_executors.run("rag", bridge_health.probe_rag_origin_canon, CONFIG)
            print(f"\n  [{rag_row.status.ljust(4)}] {rag_row.name:<18} {rag_row.detail}")

    if CONFIG.get("asr_enabled", True):
        threading.Thread(target=voice_listener_worker, daemon=True).start()
    start_text_input_worker()
    start_desktop_audio_worker()
    start_screen_watcher_worker()
    
    # 4. Background scouter (multi-provider digest)
    start_scouter()
    
    # 5. Jalankan Idle Animation System (RNG-based)
    start_idle_animation()
    
    memories = await arti_executors.run("background", load_long_term_memories)
    memory_block = build_startup_memory_block(memories)
    
    # Load dynamic context: soul, mood, viewer
    soul_context = load_soul_context()
    soul_block, soul_lines_dropped = build_soul_block(soul_context, CONFIG)
    print(f"[Soul] chars injected={len(soul_block)}, lines dropped={soul_lines_dropped}")
    viewer_context = load_viewer_context()
    current_mood = get_current_mood()
    
    # mood SENGAJA tidak lagi ditempel di sini — lihat mood_block_now().
    # Blok statis di startup membuat mood beku sepanjang sesi.
    # (viewer_context hanya untuk print hitungan di bawah; injeksinya per-turn)
    
    # Scouter summary is NOT baked in here any more (brain batch B 2026-10-21):
    # at this point no scouter pass has run, so it was always "" (0 of the
    # audited logs show "[Summarizer] Context injected"). The live summary still
    # feeds initiative material via CONFIG["scouter_last_result"].

    origin_block = build_origin_context()
    # [DIRIKU] — ARTI's self + core motivation (arti-state/self.jsonl, design §2/§9.1):
    # frozen prefix right after origin, string concat only (no model call), never
    # trimmed (_SYSTEM_PROMPT_KEEP_MARKERS). Kill switch arti_state_enabled.
    self_block = await arti_executors.run(
        "background", arti_state.startup_self_block, CONFIG, soul_text=soul_context
    )
    _discord_stories["block"] = arti_state.discord_stories_block(CONFIG)
    _discord_stories["until"] = time.monotonic() + 60.0 * float(
        CONFIG.get("discord_stories_window_min", 30)
    )
    # FIX P1: Build system prompt — only add non-empty blocks
    dynamic_system_prompt = assemble_startup_prompt(
        _SYSTEM_PROMPT_BASE, origin_block, self_block, soul_block, memory_block
    )
    # viewer_block SENGAJA tidak lagi ditempel di sini. Dump statis semua
    # penonton ikut TIAP turn walau tidak ada yang chat — dan bikin prompt
    # jebol cap. Sekarang per-turn: viewer_block_for(trigger.viewer_name) di
    # _handle_voice_trigger hanya saat penonton itu benar-benar chat; penonton yang
    # cuma disebut tetap terjangkau lewat vault RAG (ARTI_VIEWERS.md ter-index).
    dynamic_system_prompt = persona_prompt_tail(dynamic_system_prompt, CONFIG)
    print(f"[Mood] Current mood: {current_mood}")
    if viewer_context:
        print(f"[Viewer] {viewer_context.count(chr(10))} viewer entries loaded")
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
            # "sudah terindeks?" menjawab SUDAH. Kalau satu berkas gagal di-embed
            # sepenuhnya, seluruh ingatan berkas itu raib dari pencarian
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
        if CONFIG.get("arti_state_enabled", True):
            # Open wants join reply-time retrieval with precomputed vectors: one
            # embed batch here, off the reply path (never per turn).
            threading.Thread(
                target=arti_state.prime_want_vectors, args=(CONFIG,),
                daemon=True, name="arti-state-wants",
            ).start()
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
    try:
        # Audit L06: one content-free health line + telemetry row per 10 min.
        import arti_health
        arti_health.start_health_monitor(
            CONFIG, loop=asyncio.get_running_loop(),
            disk_path=session_transcript.get_transcript_path(CONFIG))
    except Exception as e:  # noqa: BLE001 — health is diagnostics only
        print(f"[Health] monitor tidak jalan ({type(e).__name__})")

    add_to_history("System", f"Live stream dimulai. Arti aktif menemani {arti_voice_pipeline.owner_name(CONFIG)} (Profil: {profile}).")
    
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
        def _startup_rag_catchup() -> None:
            if _rag_background_deferred():
                print("[Vault RAG] Startup catch-up dilewati: giliran suara aktif atau embedding breaker terbuka")
                return
            arti_vault_rag.reindex_startup_catchup(CONFIG)
        threading.Thread(
            target=_startup_rag_catchup,
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
                    while time.time() < _batas and (
                        _brain_busy or tts_is_playing or arti_vault_rag.embedding_breaker_open()
                    ):
                        time.sleep(1.0)

                obs_catchup.run_catchup(
                    CONFIG,
                    current_session_id=session_transcript.current_session_id(CONFIG) or "",
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
        # grok cold ~14 dtk, vision cold + gambar ~36 dtk (terukur 2026-08-01).
        # Tanpa pemanas, panggilan pertama tiap role bisa timeout -> sesi
        # dibuang -> dingin lagi (jebakan yang sama dengan prewarm voice dulu).
        try:
            arti_agy_agent.prewarm(CONFIG)
        except Exception as e:  # noqa: BLE001
            print(f"[Agy] pemanas startup gagal: {type(e).__name__}: {e}")
        # Kuota AWAL sesi. Dibaca di thread pemanas (bukan jalur boot) supaya
        # 2-3 detik `/usage` tidak menunda apa pun, dan gagal-diam kalau agy
        # mati. Pasangannya dicetak lagi saat shutdown -> selisihnya = ongkos
        # sesi ini, tanpa Bohan perlu ingat mengeceknya.
        try:
            arti_agy_agent.lapor_kuota("awal sesi", CONFIG)
        except Exception as e:  # noqa: BLE001
            print(f"[Agy] baca kuota awal gagal: {type(e).__name__}: {e}")
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

    if CONFIG.get("rehearsal_parent_pid"):
        from scripts.livepath.bridge import warm_voice
        await warm_voice(CONFIG, arti_codex_agent)

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
    # Mode sesi terakhir yang dicetak (REVIEW_BRAIN F2: mode game palsu harus
    # kelihatan di log; sekali per pergantian, tanpa isi).
    _mode_logged = None
    _loop_errors = {}
    _consecutive_turn_failures = 0

    # Loop Utama
    while True:
        await asyncio.sleep(0.1)

        _rehearsal_stopping = False
        if os.environ.get("ARTI_REHEARSAL_CONFIG"):
            from scripts.livepath.bridge import drain_stop
            _rehearsal_stopping = drain_stop(
                Path(os.environ["ARTI_REHEARSAL_CONFIG"]).resolve().parent,
                pending=voice_trigger_queue.qsize() + len(voice_trigger_buffer),
                busy=True if tts_is_playing else _brain_busy,
            )
            if _rehearsal_stopping:
                _stop_stream_voice()
                continue
        turn_cleanup_done = False
        try:
            _maybe_auto_off_ptt()
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
            if _mode_now != _mode_logged:
                print(f"[Mode] {_mode_logged or '-'} → {_mode_now} "
                      f"(stardew_live={_stardew_live()})")
                _mode_logged = _mode_now
            _mode_policy = arti_session_mode.mode_policy(_mode_now, CONFIG)
            _dormant_now = arti_curious.is_dormant(
                CONFIG,
                now=time.time(),
                last_human_ts=_last_human_activity_ts,
                mode=_mode_now,
                minecraft=_in_game_now,
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
                and not _rehearsal_stopping
                and not _dormant_now
                and not _farewell_quiet_active()
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
                    # Pagar sama dengan inisiatif: chat viewer menunggu, dan
                    # jeda streamer initiative_streamer_gap_sec (audit 3b/3c).
                    yt_queue_pending=_viewer_chat_waiting(),
                    streamer_recent=(
                        time.time() - _last_streamer_speech_ts
                        < float(CONFIG.get("initiative_streamer_gap_sec", 5.0))
                    ),
                    stardew_live=_stardew_live(),
                ):
                    if arti_curious.prepare_for_fire(CONFIG):
                        curious_text = arti_curious.build_prompt(CONFIG)
                        # CONFIG WAJIB dioper: tanpa itu _recent_hooks tidak pernah
                        # terisi -> dedup "hook terlalu mirip" mati total, dan
                        # curious mengulang sudut yang sama (audit 2026-08-03;
                        # sejalan dengan keluhan soal topik yang berulang).
                        if curious_text:
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
                and not _rehearsal_stopping
                and not _dormant_now
                and not _farewell_quiet_active()
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
                    minecraft=_in_game_now,
                    viewer_waiting=_viewer_chat_waiting(),
                ):
                    _baru_bicara = _streamer_baru_bicara()
                    _init_text = arti_curious.build_initiative_prompt(
                        CONFIG,
                        streamer_baru_bicara=_baru_bicara,
                        ada_penonton=_ada_penonton(),
                        **_initiative_materials(streamer_baru_bicara=_baru_bicara)
                    )
                    arti_curious.mark_initiative_fired()
                    # Tetap trigger "curious" walau isinya komentar game: jalur
                    # cepat (skip RAG, histori pendek) = komentar gesit, dan kalau
                    # chat viewer masuk, ocehan game memang pantas kalah. Reaksi
                    # EVENT (mati/diserang) yang kebal cull — itu type "game".
                    if _init_text or not CONFIG.get("stardew_initiative_game_first", False):
                        queue_voice_trigger(_init_text, trigger_type="curious")
                    if _in_game_now:
                        print("[Minecraft] Giliran komentar main game")
                    elif _host_mode:
                        print("[Host] Giliran Arti ngisi siaran")
                    else:
                        print("[Initiative] Hening terdeteksi — Arti buka topik sendiri")

            # Fitur E: selama media share diputar di layar, TAHAN konsumsi trigger —
            # Arti nonton bareng; antrean tetap utuh, dilanjut setelah playback.
            # Flag on: a queued thank-you or a marked/name-addressed owner line may still speak.
            if time.time() < _media_playback_until:
                if not _waiting_hold_pass():
                    continue

        except Exception as e:
            _log_loop_error(_loop_errors, "Proactive tick gagal", e)
            await asyncio.sleep(0.5)
            continue

        # Cek apakah ada trigger suara yang memanggil A
        try:
            if speech_control.held:
                await asyncio.sleep(0.1)
                continue
            turn_stop_epoch = speech_control.epoch
            use_buffer = CONFIG.get("voice_queue_enabled", False)
            if use_buffer:
                # Mode buffer: FIFO prioritas — TIDAK drain-newest, chat YT
                # yang antri tetap dijawab berurutan.
                if attention_desk.enabled(CONFIG) and not (hotkey_active and not _viewer_ptt_gate_closed()) and not (
                    CONFIG.get("media_watch_mode") and (chat_media.watch_held(CONFIG, time.time())
                                                         or voice_trigger_buffer.depth_for("video"))
                ):
                    queued = _dequeue_buffered_trigger_if_brain_ready(owner_only=True)
                    if queued is None:
                        queued = await attention_bridge.dequeue(__import__(__name__))
                else:
                    queued = _dequeue_buffered_trigger_if_brain_ready()
                if queued is None:
                    raise queue.Empty
                raw = queued
            else:
                raw = _dequeue_preserved_legacy_trigger()

            trigger = _normalize_voice_trigger(raw)

            if _speech_trigger_expired(trigger) or _game_reaction_expired(trigger):
                if tok := getattr(trigger.text, "thanks_token", ""):
                    __import__("arti_runtime.thanks_once", fromlist=["mark_state"]).mark_state(tok, "dropped")
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

            turn_failed = True
            turn_pending_id = trigger.turn_id or _pending_turn_id
            try:
                await _handle_voice_trigger_traced(trigger, memories, dynamic_system_prompt, expected_epoch=turn_stop_epoch)
                turn_failed = False
                _consecutive_turn_failures = 0
            finally:
                if turn_failed:
                    _finish_reply_ptt(trigger)
                    # A newer capture may already own the pending id.
                    if _pending_turn_id == turn_pending_id:
                        _pending_turn_id = None
                    turn_cleanup_done = True
                    try:
                        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                    except Exception as cleanup_error:
                        _log_loop_error(_loop_errors, "Turn cleanup gagal", cleanup_error)
                with _brain_busy_lock:
                    _brain_busy = False
                    _brain_busy_since = 0.0
                # Idle animation SELALU dipulihkan. Audit 2026-08-05:
                # `_prepare_turn_start` mematikan idle di SETIAP giliran, tapi
                # yang menyalakannya lagi cuma cabang SUKSES — satu giliran
                # gagal (provider 429 / jawaban tersaring) = model membeku
                # tanpa gerak sampai giliran sukses berikutnya. Untuk trigger
                # non-PTT (yt_chat/curious/game = mayoritas saat in-game) tidak
                # ada penyelamat lain. Aman dipanggil dua kali: ia membatalkan
                # tugas sebelumnya dulu.
                _schedule_post_answer_cleanup()
                _finish_stardew_owner_voice(trigger)
                with speech_control.retaining(turn_stop_epoch) as allowed:
                    if allowed:
                        attention_bridge.finish(__import__(__name__), trigger)

        except queue.Empty:
            continue
        except Exception as e:
            _log_loop_error(_loop_errors, "Masalah di main loop", e)
            _consecutive_turn_failures += 1
            with _brain_busy_lock:
                _brain_busy = False
                _brain_busy_since = 0.0
            if _consecutive_turn_failures >= 5:
                tts_is_playing = False
                _tts_started_ts = 0.0
                if _consecutive_turn_failures == 5:
                    print("!!! BRIDGE TURN FAILURE: 5 kegagalan beruntun — reset turn state !!!")
                    _schedule_post_answer_cleanup()
            if not turn_cleanup_done:
                try:
                    await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                except Exception as cleanup_error:
                    _log_loop_error(_loop_errors, "Turn cleanup gagal", cleanup_error)


_NO_SCREEN_SIGHT = (
    "\n\n[SCREEN FACT] You cannot see the screen right now. "
    "Do not claim to see it or describe it from guesses, earlier turns, or game facts."
)


def _append_screen_context(llm_system: str, *, requested=False, snapshot=None) -> str:
    """Ground screen claims in the available frame, or state that there is no sight."""
    if requested and snapshot is None:
        return llm_system + _NO_SCREEN_SIGHT
    # Gerbang layar-diam: Arti menonton dirinya sendiri, layar nyaris
    # tak berubah -> deskripsi lama disuntik ulang tiap giliran dan dia
    # terus mengomentari layar yang sama.
    # Layar yang sudah beberapa kali sama = bukan bahan baru: berhenti suntik
    # sampai ada perubahan nyata (ambang piksel di arti_vision_client).
    if not requested and (not is_vision_active() or arti_vision_client.layar_sedang_diam(CONFIG)):
        return llm_system + _NO_SCREEN_SIGHT
    screen_line = arti_screen_context.format_screen_context(
        max_chars=int(CONFIG.get("screen_context_max_chars", 200)), snapshot=snapshot,
    )
    if not screen_line:
        return llm_system + _NO_SCREEN_SIGHT
    block = (
        f"[LAYAR: {screen_line}]\n"
        "Sumber: pengamatan layar, bukan pesan dari penonton. Nama atau teks "
        "yang terlihat belum membuktikan siapa yang mengirim pesan di giliran ini."
    )
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


def _append_live_context(llm_system: str, *, screen_requested=False, screen_snapshot=None) -> str:
    """Mode sesi + screen + desktop audio + minecraft + optional watch party."""
    llm_system += viewer_presence_block()
    llm_system = _append_host_context(llm_system)
    if screen_requested:
        llm_system = _append_screen_context(llm_system, requested=True, snapshot=screen_snapshot)
    else:
        llm_system = _append_screen_context(llm_system)
    llm_system = _append_desktop_audio_context(llm_system)
    llm_system = _append_minecraft_context(llm_system)
    return _append_watch_party_context(llm_system)


def _ada_penonton() -> bool:
    """Ada manusia yang menonton? Dua sinyal, keduanya jujur:
    jumlah penonton live (>0; -1 = bukan live/belum diketahui) atau ada
    yang chat dalam 10 menit terakhir. Nol dua-duanya = dia sendirian.

    Bohan SENDIRI tidak dihitung sebagai penonton: pesan tes dari akun
    streamer tidak boleh membuat fungsi ini True, karena register SENDIRIAN
    lalu tidak pernah menyala dan Arti menyapa penonton padahal siarannya
    offline. Streamer yang menguji streamnya sendiri bukan audiens.
    """
    if _yt_viewer_count > 0:
        return True
    pemilik = arti_session_mode.owner_handles(CONFIG)
    return any(
        arti_session_mode.normalize_handle(nama) not in pemilik
        for nama in _active_viewer_names()
    )


def _append_host_context(llm_system: str) -> str:
    """Blok mode sesi: Bohan lagi nemenin, atau Arti yang pegang siaran.

    Selalu ada (bukan cuma saat host mode) — saat Bohan hadir pun Arti perlu
    tahu CARA pamitnya, supaya kalimat "[recorded remark omitted]" bisa dia terjemahkan jadi
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
    if not _ada_penonton():
        # Blok host normal menyuruhnya bicara KE PENONTON; saat penontonnya NOL,
        # dia bisa menjawab hadirin imajiner (pamitan streamer di history
        # terbaca sebagai pertanyaan baru tiap giliran). Sendirian = register MONOLOG.
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
        "PENONTON adalah lawan bicaramu sekarang, bukan Bohan. Selagi dia "
        "AFK, jangan menjadikan dia topik utama dan jangan mengulang-ulang "
        "bahwa dia lagi pergi (cukup sekali di awal). Kalau bahan yang kamu "
        "punya soal dia, ambil sisi umumnya dan jadikan bahan ngobrol "
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
            "uji. KALAU Bohan nyuruh kamu masuk/ikut main, tutup jawabanmu "
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
    # Judul menyesuaikan mode. Audit 2026-08-05: saat host_game, blok ini dulu
    # tetap bilang "bareng Bohan" sementara blok host bilang Bohan AFK dan
    # event runner bilang "[recorded remark omitted]" — tiga kalimat yang saling
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
    # SIAPA SIAPA di dunia ini (permintaan Bohan 2026-08-08: "[recorded remark omitted]"). Tanpa ini dia cuma
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
        # Misi dari Bohan = tulang punggung sesi solo: dia boleh ngapain saja
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
    _reflex = _reflex_context_note()
    if _reflex:
        # Tanpa ini dia mengulang bunyi kagetnya di depan kalimat panjang —
        # kedengaran seperti kaget dua kali untuk satu kejadian.
        block += f"\n\n{_reflex}"
    # Aksi yang butuh Bohan di dunia disembunyikan saat dia AFK — kalau tidak,
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
    # Bohan dirutekan ke Groq, yang sudah pernah mengabaikan instruksi tag.
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
    # TERLIHAT, sampai Arti menyebut "tulisan" yang sebenarnya (halusinasi) AUDIO.
    return (
        llm_system
        + "\n\n[AUDIO TERDENGAR — suara/dialog dari yang lagi diputar Bohan. "
        + "Kamu MENDENGAR ini dari speaker; ini BUKAN teks yang terlihat di layar:]\n"
        + fresh
    )


async def _await_llm_deadline(task: asyncio.Task, timeout_sec: float) -> bool:
    """Wait for a reply task within the per-turn budget; return False on expiry."""
    try:
        await asyncio.wait_for(task, timeout=max(0.0, float(timeout_sec)))
        return True
    except asyncio.TimeoutError:
        task.cancel()
        return False


async def _speak_reply_chunks(chunks, privacy_epoch, *, host_story=()):
    """Speak within the current turn's existing interrupt/privacy contract."""
    global _reply_chunks_inflight
    # JEDA NAPAS antar kalimat: hanya DI ANTARA kalimat, kalimat pertama langsung.
    _jeda = float(CONFIG.get("tts_jeda_antar_kalimat_sec", 0.0) or 0.0)
    voiced = bool(chunks)
    spoken_n = 0
    _reply_chunks_inflight = True
    if CONFIG.get("voice_append_context"):  # ADR 200: record which chunks actually reached the speaker
        voice_context.playback_reset()
    story_token = playback_story.set(host_story) if host_story else None
    try:
        for _i, chunk in enumerate(chunks):
            if tts_stop_flag or not screen_privacy.current(privacy_epoch):
                break
            if _i and _alert_yield_pending():
                break
            if _i and _jeda > 0:
                started = time.perf_counter()
                await asyncio.sleep(_jeda)
                pipeline_timer.note_tts_jeda_ms(int((time.perf_counter() - started) * 1000))
            # PTT/media/stop can arrive during the configured breath gap.
            if tts_stop_flag or not screen_privacy.current(privacy_epoch):
                break
            if _i and _alert_yield_pending():
                break
            if host_story and not host_story_playback_current():
                voiced = False
                break
            if CONFIG.get("voice_append_context"):
                voice_context.playback_mark(_i, tts_play_generation)
            await tts.speak(chunk)
            spoken_n += 1
            if CONFIG.get("voice_append_context"):  # audio started = sd.play ran (generation moved) and held
                voice_context.playback_check(_i, tts_play_generation, getattr(tts, "_playback_started", True))
            voiced = voiced and getattr(tts, "_last_playback_completed", False)
        leftover = spoken_n < len(chunks)
        yielded = leftover and _alert_yield_pending()
        spoken_text = None
        if yielded:
            spoken_text = " ".join(chunks[:spoken_n]).strip()
            _commit_alert_yield()
        elif _yield_after_sentence:
            _clear_alert_yield()
        voiced_ok = voiced and not tts_stop_flag and screen_privacy.current(privacy_epoch)
        return _SpeakChunksResult(voiced_ok, spoken_n, spoken_text, yielded)
    finally:
        if story_token is not None:
            playback_story.reset(story_token)
        _reply_chunks_inflight = False


def _retain_chat_trigger(trigger):
    if trigger.trigger_type != "yt_chat":
        return
    if not (getattr(trigger.text, "retain_pending", False) or chat_media.watch_held(CONFIG, time.time())):
        return
    with speech_control.retaining(speech_turn_epoch.get()) as allowed:
        if not allowed:
            return
        text = trigger.text if getattr(trigger.text, "retain_pending", False) else chat_media.PendingChat(trigger.text)
        if CONFIG.get("voice_queue_enabled", False):
            voice_trigger_buffer.enqueue(arti_voice_queue.QueuedVoiceTrigger(
                text, "yt_chat", trigger.viewer_name, trigger.queued_at, trigger.turn_id,
                trigger.stardew_voice_epoch, owner_marked=trigger.owner_marked, rotation_rank=trigger.rotation_rank))
        else:
            _enqueue_legacy_voice(replace(trigger, text=text))


def _stop_stream_voice():
    _stop_voice_now(hold=True)


def _stop_voice_now(*, hold=False, ptt=False):
    """Invalidate queued and in-flight voice together before cutting native playback."""
    global tts_stop_flag
    speech_control.interrupt(hold=hold)
    tts_stop_flag = True
    clear_trigger_queue(stop=True)
    task = current_api_task
    if ptt and task and not task.done():
        if main_event_loop is not None and main_event_loop.is_running():
            main_event_loop.call_soon_threadsafe(task.cancel)
        else:
            task.cancel()
    if tts_is_playing or _reflex_playing:
        try:
            with _sd_gerbang:
                sd.stop()
        except Exception:
            pass
        if ptt:
            request_asr_stream_restart("toggle OFF + stop TTS")


async def _handle_voice_trigger_traced(
    trigger: VoiceTrigger, memories: list, dynamic_system_prompt: str, *, expected_epoch=None,
):
    """Bind all telemetry emitted during this voice turn to its transcript id."""
    import arti_api_telemetry as tel

    turn_id = trigger.turn_id or _pending_turn_id or uuid.uuid4().hex[:12]
    token = tel.set_turn_id(turn_id)
    epoch = speech_control.epoch if expected_epoch is None else expected_epoch
    if not speech_control.allows(epoch) or _speech_trigger_expired(trigger):
        if tok := getattr(trigger.text, "thanks_token", ""):
            __import__("arti_runtime.thanks_once", fromlist=["mark_state"]).mark_state(tok, "stopped")
        tel.reset_turn_id(token)
        return
    epoch_token = speech_turn_epoch.set(epoch)
    floor_token = _viewer_floor_revision.set(speech_control.owner_revision if trigger.trigger_type == "yt_chat" else None)
    stop_root = os.environ.get("ARTI_REHEARSAL_CONFIG")
    stop_marker = Path(stop_root).resolve().parent / "bridge-stop-request" if stop_root else None
    async def run_turn():
        if (getattr(trigger, "trigger_type", None) == "mic_pasif"
                or trigger.trigger_type == "own_play"):
            return await _run_passive_turn_preemptible(trigger, memories, dynamic_system_prompt)
        return await _handle_voice_trigger(trigger, memories, dynamic_system_prompt)
    task = asyncio.create_task(run_turn())
    if CONFIG.get("rehearsal_parent_pid"):
        from scripts.livepath.bridge import turn_event
        turn_event("start", turn_id, trigger)
    try:
        while not task.done():
            if stop_marker is not None and stop_marker.is_file():
                _stop_stream_voice()
            if (not speech_control.allows(epoch) or (CONFIG.get("media_watch_mode")
                    and not _media_hold_allows(trigger))):
                arti_executors.note_user_cancel()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if speech_control.allows(epoch):
                    if trigger.owner_reply_required:
                        _retain_interrupted_owner(trigger)
                    else:
                        _retain_chat_trigger(trigger)
                await _subtitle_broadcast_status("idle", "")
                await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                return
            await asyncio.wait({task}, timeout=0.05)
        if not task.result() and trigger.owner_reply_required and tts_stop_flag and speech_control.allows(epoch):
            _retain_interrupted_owner(trigger)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if tok := getattr(trigger.text, "thanks_token", ""):
            ran = task.done() and not task.cancelled() and task.exception() is None and bool(task.result())
            __import__("arti_runtime.thanks_once", fromlist=["mark_state"]).mark_state(tok, "ran" if ran else "stopped")
        speech_turn_epoch.reset(epoch_token)
        _viewer_floor_revision.reset(floor_token)
        tel.reset_turn_id(token)
        if CONFIG.get("rehearsal_parent_pid"):
            turn_event("end", turn_id, trigger, completed=not task.cancelled() and task.exception() is None)


async def _run_passive_turn_preemptible(trigger, memories, dynamic_system_prompt):
    """Giliran mic_pasif SELALU kalah dari PTT.

    PTT ON (hotkey_active / tts_stop_flag) selama fase mikir (RAG/LLM)
    membatalkan giliran ini di tempat. Setelah fase bicara mulai, jalur PTT-ON
    lama yang menghentikan TTS. Selama giliran pasif hidup, listener dan
    queue_voice_trigger TIDAK membuang ucapan PTT (lihat _passive_turn_inflight).
    """
    global _passive_turn_inflight, _passive_turn_speaking, tts_stop_flag
    # Sama dengan baris pertama _handle_voice_trigger; tanpa ini sisa flag
    # pembatalan giliran sebelumnya langsung membatalkan giliran ini.
    tts_stop_flag = False
    _passive_turn_inflight, _passive_turn_speaking = True, False
    task = asyncio.ensure_future(
        _handle_voice_trigger(trigger, memories, dynamic_system_prompt))
    yielded = False
    try:
        while not task.done():
            name_waiting = (CONFIG.get("voice_queue_preserve_humans", False)
                            and not attention_desk.enabled(CONFIG)
                            and not trigger.owner_reply_required
                            and (trigger.trigger_type == "own_play" or not streamer_voice.addressed(trigger.text, CONFIG))
                            and _name_call_waiting())
            if ((hotkey_active and not attention_desk.enabled(CONFIG)) or tts_stop_flag or name_waiting) and not _passive_turn_speaking:
                yielded = True
                task.cancel()
                break
            await asyncio.wait({task}, timeout=0.1)
        await asyncio.wait({task})
        if task.cancelled() or yielded or (trigger.owner_reply_required and hotkey_active and not _passive_turn_speaking):
            print("[ASR Pasif] PTT menang — giliran pasif dibatalkan.")
            if (trigger.trigger_type == "mic_pasif"
                    and (trigger.owner_reply_required or CONFIG.get("voice_queue_preserve_humans", False))
                    and speech_control.allows(speech_turn_epoch.get())):
                _retain_interrupted_owner(trigger)
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
            return True  # The wrapper settled this interruption.
        return task.result()
    finally:
        _passive_turn_inflight = False
        _passive_turn_speaking = False
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _handle_voice_trigger(
    trigger: VoiceTrigger, memories: list, dynamic_system_prompt: str
):
    """Satu trigger sekaligus: mikir → RAG → Groq → TTS (no overlap)."""
    global _pending_turn_id, last_arti_reply_text, current_api_task, tts_stop_flag
    global _passive_turn_speaking
    if not speech_control.allows(speech_turn_epoch.get()):
        return
    if getattr(trigger.text, "_host_story_guarded", False):
        if not trigger.text.is_current():
            return
    elif _live7_game_lane_trigger(trigger) and _live7_game_session_stopped():
        return
    playing_chat = trigger.trigger_type == "yt_chat" and _game_actually_playing()
    host_story_retry = getattr(trigger, "host_story_retry", False)
    owner_overflow = isinstance(trigger.text, arti_voice_queue.OwnerOverflowAck)
    owner_revision = _viewer_floor_revision.get()
    if owner_revision is None:
        owner_revision = speech_control.owner_revision
    tts_stop_flag = False  # A new turn releases cancellation, never the owner hold.
    conversation_only = _kept_owner_conversation_only(trigger)

    with history_lock:
        privacy_epoch = screen_privacy.epoch
    # Proactive prompts mix screen, scouter and conversation into a plain string.
    # Do not salvage a queued prompt whose provenance has already been revoked.
    if trigger.trigger_type == "curious" and (
        screen_privacy.restricted
        or trigger.queued_at <= screen_privacy.changed_at
        or not screen_privacy.current(getattr(trigger.text, "privacy_epoch", privacy_epoch))
    ):
        return
    user_speech = trigger.text
    if CONFIG.get("stardew_initiative_game_first") and trigger.trigger_type in ("mic", "ptt", "wake_word", "mic_pasif"):
        import arti_stardew_initiative
        arti_stardew_initiative.note_mic_turn(user_speech)
    import arti_api_telemetry as tel
    turn_id = trigger.turn_id or _pending_turn_id or tel.current_turn_id()
    timer = PipelineTimer(extra=pipeline_timer.pop_asr_stages())
    await _prepare_turn_start(trigger.trigger_type, trigger.viewer_name)
    screen_requested = not conversation_only and _owner_screen_look_requested(trigger)
    screen_snapshot = None
    try:
        # In-game: data dunia Minecraft = ground truth, dan komentar layar OBS
        # memang dibungkam — refresh screenshot cuma nambah detik ke reaksi
        # (target mati->suara <=30 dtk). Berlaku untuk reaksi event ("game")
        # maupun giliran komentar proaktif selama runner hidup.
        # mic_pasif: refresh vision (s/d 15 dtk) cuma memperpanjang jendela
        # di mana PTT Bohan harus menunggu giliran pasif selesai.
        _skip_vision = trigger.viewer_name is not None or (
            CONFIG.get("video_watcher_v2") and trigger.trigger_type == "video"
        ) or trigger.trigger_type in (
            "yt_chat", "game", "mic_pasif", "own_play", "read_in_game", "focus", "cut_in", "scene_ended"
        ) or (
            _mc_runner_active() and CONFIG.get("minecraft_mute_screen_curious", True)
        )
        if (screen_requested and trigger.queued_at >= screen_privacy.changed_at) or (
                not screen_requested and not _skip_vision):
            screen_snapshot = await asyncio.wait_for(
                arti_executors.run("vision", refresh_vision_for_turn, screen_requested, privacy_epoch),
                timeout=float(CONFIG.get("vision_turn_budget_sec", 15.0)),
            )
    except asyncio.TimeoutError:
        # The worker may finish later. Only the manual path can update the ring.
        print("[Vision] Turn capture budget expired")
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
        dynamic_system_prompt
        + mood_block_now()
        + build_today_block()
        + viewer_block_for(trigger.viewer_name)
        + discord_stories_block_now()
        + stardew_role_block_now()
        + _live7_status_block_now()
        + _alert_yield_fact_block()
        + mind_intent_block_now()
    )
    if CONFIG.get("voice_game_memory_enabled"):
        from arti_runtime import experience_digest  # ADR 228: one stat + cached read, no model or retrieval
        turn_system_prompt += experience_digest.bridge_block(__import__(__name__))
    if CONFIG.get("voice_append_context"):
        append_stable, turn_system_prompt = voice_context.bounded_system_prompt(
            dynamic_system_prompt, turn_system_prompt[len(dynamic_system_prompt):],
            int(CONFIG.get("llm_system_prompt_max_chars", 5500)),
        )

    # Pakai categorized history + RAG parallel (arti_voice_pipeline)
    append_context = _append_live_context
    if screen_requested:
        def append_context(system):
            return _append_live_context(system, screen_requested=True, screen_snapshot=screen_snapshot)

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
            append_watch_party_context=append_context,
            get_categorized_history=get_categorized_history,
        )
    else:
        turn = await arti_voice_pipeline.prepare_turn_context(
            user_speech,
            memories,
            turn_system_prompt,
            CONFIG,
            trim_system_prompt=trim_system_prompt_for_llm,
            append_watch_party_context=append_context,
            get_categorized_history=get_categorized_history,
            extract_trigger_message=_extract_trigger_message,
            quiet=yt_chat_is_quiet(CONFIG),
        )
    if CONFIG.get("video_watcher_v2") and trigger.trigger_type == "video":
        from arti_runtime.watcher_v2 import resolve_reaction
        user_speech = resolve_reaction(user_speech)
        turn.prompt_content = turn.prompt_for(user_speech)
    formatted_history = turn.formatted_history
    reply_facts = (_live7_reply_facts_now(include_host_story=False) if host_story_retry or owner_overflow
                   else _live7_reply_facts_now())
    llm_system = turn.llm_system + reply_facts
    host_story = tuple(story for story in (trigger.text, reply_facts, getattr(trigger.text, "host_story_context", None))
                       if getattr(story, "_host_story_guarded", False))
    if CONFIG.get("stream_game_notes"):
        from arti_runtime import stream_notes
        stream_notes.bridge_reply_facts(__import__(__name__), llm_system[len(turn.llm_system):], epoch=privacy_epoch)
        stream_notes.bridge_delivered(__import__(__name__), trigger, epoch=privacy_epoch)
    if trigger.viewer_name:
        llm_system += (
            f"\n\n[PENGIRIM GILIRAN INI] {trigger.viewer_name} hadir untuk giliran ini "
            "lewat pesan/donasi/event ini; boleh disapa langsung. "
            "Ini bukan klaim masih menonton sesudah giliran ini."
        )
    else:
        # Current authorship comes from ingress metadata, never a screen name,
        # an active-viewer roster, or whichever chat happened most recently.
        if trigger.trigger_type in STREAMER_TRIGGERS:
            source = "Streamer (%s) berbicara lewat mic/chat pemilik." % arti_voice_pipeline.owner_name(CONFIG)
        elif trigger.trigger_type == "mic_pasif":
            source = "Ucapan mic dari kanal %s; penerima ucapannya belum pasti, kamu yang menilai." % arti_voice_pipeline.owner_name(CONFIG)
        elif trigger.trigger_type == "curious":
            source = "PROAKTIF: Arti memulai komentar dari bahan konteks, bukan menerima pesan baru."
        elif trigger.trigger_type == "game":
            source = "Sumber giliran ini adalah kejadian game, bukan pesan penonton."
        else:
            source = "Tidak ada pengirim penonton terkonfirmasi untuk giliran ini."
        llm_system += (
            "\n\n[SUMBER GILIRAN INI] " + source + " "
            "Nama di history, daftar penonton aktif, atau layar bukan pengirim giliran ini. "
            "Kalau memilih menyambung chat sebelumnya, pastikan isi pesan dan orangnya "
            "memang cocok; jangan menempelkan nama chat lama ke pengamatan layar."
        )
    prompt_content = turn.prompt_content
    target_instruction = turn.target_instruction
    if facts := getattr(user_speech, "attention_facts", ""):
        llm_system += "\n[ATTENTION FACTS] " + facts
    if trigger.trigger_type == "mic_pasif":
        prompt_content = _passive_voice_prompt(
            prompt_content, target_instruction, user_speech,
            answer_required=getattr(trigger, "owner_reply_required", False))
    if playing_chat:
        prompt_content += "\nKamu sedang main. Jawab chat ini langsung dalam satu atau dua kalimat pendek."
    if conversation_only:
        prompt_content += "\nTanggapi ucapan Bohan sebagai percakapan. Jawab setiap pertanyaan yang masih relevan tanpa menggerakkan game."
    if trigger.trigger_type in ("ptt", "wake_word", "mic_pasif"):
        llm_system += arti_voice_pipeline.mic_transcript_facts(CONFIG)
    if CONFIG.get("voice_append_context"):
        # ADR 200: the same string for every lane; only the Codex lane reads the attached seed (stable = the
        # session prompt, the history block to drop from the request, the privacy epoch).
        prompt_content = voice_context.attach(prompt_content, stable=voice_context.stable_as_sent(
            append_stable, llm_system, block_markers=_SYSTEM_PROMPT_BLOCK_MARKERS,
            remove_block=_remove_system_prompt_block),
                                              history=formatted_history, privacy_epoch=privacy_epoch)
        voice_seed = prompt_content.voice_seed
    rag_query = turn.rag_query
    timer.mark("after_rag")

    if not screen_privacy.current(privacy_epoch):
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        return
    conversation_only = conversation_only or _kept_owner_conversation_only(trigger)
    if trigger.trigger_type == "mic_pasif" and (tts_stop_flag or hotkey_active):
        print("[ASR Pasif] PTT menang — giliran pasif dibuang sebelum LLM.")
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        return

    ai_reply = None
    skip_reply_tags = False
    if isinstance(trigger.text, (arti_voice_queue.CombinedChat, arti_voice_queue.OwnerOverflowAck)):
        _stardew_speech, skip_reply_tags = str(trigger.text), True
    else:
        _stardew_speech, skip_reply_tags = ((None, False) if screen_requested or conversation_only
                                            else _try_stardew_owner_generation(trigger))
    if _stardew_speech is not None:
        ai_reply = _stardew_speech
    provider = CONFIG["api_provider"].lower()
    _cursor_route = not host_story_retry and _should_route_to_cursor(trigger.trigger_type)
    tts_sentence_chunks: list[str] = []
    llm_call = {
        "provider": "cursor_chain" if _cursor_route else provider,
        "model": "unknown" if _cursor_route else str(CONFIG.get(
            {"gemini_live": "gemini_model", "gemini": "gemini_model",
             "sambanova": "sambanova_model"}.get(provider, ""), "unknown")),
    }

    # === WRAP API CALL IN CANCELLABLE TASK ===
    _rehearsal_probed = False
    async def do_api_call():
        """Semua API calls diwrap di sini biar bisa di-cancel."""
        nonlocal ai_reply, tts_sentence_chunks
        if not screen_privacy.current(privacy_epoch):
            return

        nonlocal _rehearsal_probed
        if CONFIG.get("rehearsal_parent_pid") and not _rehearsal_probed:
            _rehearsal_probed = True
            try:
                from scripts.livepath.bridge import groq_probe
                groq_probe(turn_id, user_speech, CONFIG, llm_system, prompt_content,
                           pick_groq_model, _trim_llm_payload)
            except Exception:
                pass  # instrumentation must never affect a real turn

        # --- MIC PASIF TANPA OPT-IN: STATELESS (Groq -> OpenRouter) ---
        # Routing yang dikonfigurasi menang; default tetap satu request HTTP.
        # Gagal = None = diam (tanpa kalimat kaleng).
        if host_story_retry or (trigger.trigger_type == "mic_pasif" and not _cursor_route):
            _cfg_pasif = {**CONFIG, "_screen_privacy_epoch": privacy_epoch}
            _model_pasif = pick_groq_model(
                user_speech, _cfg_pasif,
                prompt_chars=len(llm_system) + len(prompt_content))  # pagar 413
            llm_call.update(provider="groq", model=_model_pasif)
            ai_reply, _used_pasif = await arti_executors.run(
                "llm", groq_chat_completion, _model_pasif, llm_system,
                prompt_content, _cfg_pasif,
            )
            llm_call.update(provider="groq", model=str(_used_pasif or _model_pasif))
            return

        if _attention_skip_without_primary_lane(trigger.trigger_type, _cursor_route):
            return

        # --- JALUR CURSOR COMPOSER (chat YT; fallback otomatis ke rantai lama) ---
        # Ditaruh paling depan supaya rantai provider di bawahnya tidak dirombak.
        # Trigger di luar cursor_trigger_types membuat _cursor_route False ->
        # perilaku bit-identik. (Default shipped cuma yt_chat; config_local Bohan
        # menambah curious — jadi curious di mesin ini juga lewat Composer.)
        if _cursor_route:
            ai_reply, tts_sentence_chunks, _src = await _cursor_reply_with_fallback(
                llm_system, prompt_content, user_speech,
                config={**CONFIG, "_screen_privacy_epoch": privacy_epoch},
                trigger_type=trigger.trigger_type,
            )
            if CONFIG.get("rehearsal_parent_pid"):
                llm_call["rehearsal_source"] = _src
            used_provider, _, used_model = _src.partition(":")
            if used_provider == "groq" and used_model.startswith("openrouter/"):
                used_provider, used_model = "openrouter", used_model.removeprefix("openrouter/")
            if used_provider in ("agy", "codex", "cursor", "groq", "openrouter"):
                used_config = (_codex_cfg_untuk_kelas(user_speech, CONFIG)
                               if used_provider == "codex" else CONFIG)
                llm_call.update(provider=used_provider, model=used_model or str(
                    used_config.get(f"{used_provider}_model", "unknown")))
            else:
                llm_call["local_fallback"] = True
                if trigger.trigger_type == "mic_pasif":
                    # Semua provider gagal: kalimat kaleng juga berarti diam.
                    ai_reply, tts_sentence_chunks = None, []

        # --- JALUR GOOGLE AI STUDIO (GEMINI LIVE WEBSOCKET API - UNLIMITED RPD) ---
        elif provider == "gemini_live" and CONFIG["gemini_api_key"] and CONFIG["gemini_api_key"] != "YOUR_GEMINI_API_KEY":
            print(f"\n[Gemini Live API] Mengirim ke Google AI Studio ({CONFIG['gemini_model']}) dengan {len(current_history)} pesan sejarah stream...")
            try:
                ai_reply = await generate_live_api_response(prompt_content, llm_system)
            except Exception as e:
                print(f"[Brain Error] Gagal menggunakan Gemini Live API: {e}. Mencoba fallback ke HTTP API...")
                llm_call["provider"] = "gemini"
                try:
                    headers = {"Content-Type": "application/json"}
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CONFIG['gemini_model']}:generateContent?key={CONFIG['gemini_api_key']}"
                    data = {
                        "contents": [{"role": "user", "parts": [{"text": prompt_content}]}],
                        "system_instruction": {"parts": [{"text": llm_system}]},
                        "generationConfig": {"maxOutputTokens": _reply_tokens_with_thinking(user_speech, 200), "temperature": 1.0}  # 2.5-flash: thinking counts here; do not shrink
                    }
                    response = await arti_http_util.post_in_thread(
                        arti_http_util.gemini_session(), url, headers=headers, json=data,
                        timeout=(3, 8),
                    )
                    if response.status_code == 200:
                        res_json = response.json()
                        ai_reply = res_json["candidates"][0]["content"]["parts"][0]["text"]
                        ai_reply = arti_reply_policy.trim_if_length_cut(
                            ai_reply, res_json["candidates"][0].get("finishReason"), "Gemini")
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
                    "generationConfig": {"maxOutputTokens": _reply_tokens_with_thinking(user_speech, 200), "temperature": 1.0}  # 2.5-flash: thinking counts here; do not shrink
                }
                response = await arti_http_util.post_in_thread(
                    arti_http_util.gemini_session(), url, headers=headers, json=data,
                    timeout=(3, 8),
                )
                if response.status_code == 200:
                    res_json = response.json()
                    ai_reply = res_json["candidates"][0]["content"]["parts"][0]["text"]
                    ai_reply = arti_reply_policy.trim_if_length_cut(
                        ai_reply, res_json["candidates"][0].get("finishReason"), "Gemini")
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
            llm_call["model"] = str(current_model)
            groq_voice_ms = 0
            groq_voice_ok = False
            groq_usage_body: dict | None = None
            print(f"\n[Groq API] Mengirim ke Groq ({current_model}) [{generate_live_api_response._groq_idx}/{len(models)} rolling] dengan {len(current_history)} pesan sejarah stream...")
            try:
                headers = {"Authorization": f"Bearer {CONFIG['groq_api_key']}", "Content-Type": "application/json"}
                user_content = prompt_content
                data = {
                    "model": current_model,
                    # Dulu 150 datar — di bawah batas aman jawaban (~900 ch),
                    # jadi jawaban panjang terpotong token di tengah kalimat.
                    "max_tokens": live_max_tokens_for_trigger(user_speech, CONFIG),
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
                    data["max_tokens"] = _reply_tokens_with_thinking(user_speech, 512)
                groq_t0 = time.perf_counter()
                if CONFIG.get("groq_stream_enabled"):
                    stream_data = {**data, "stream": True}
                    _stream_meta: dict = {}

                    def _groq_stream_collect():
                        resp = arti_http_util.groq_session().post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            headers=headers,
                            json=stream_data,
                            timeout=(3, 8),
                            stream=True,
                        )
                        if resp.status_code != 200:
                            return None, []
                        return arti_groq_stream.collect_streaming_reply(
                            resp.iter_lines(decode_unicode=False), meta=_stream_meta
                        )

                    full, sents = await arti_executors.run("llm", _groq_stream_collect)
                    if full:
                        trimmed = _trim_stream_reply(
                            full, _stream_meta.get("finish_reason"), data["max_tokens"],
                            f"Groq Stream {current_model}",
                        )
                        if trimmed != full:
                            full, sents = trimmed, _sentences_or_empty(trimmed)
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
                        timeout=(3, 8),
                    )
                groq_need_retry = False
                if response.status_code == 200:
                    groq_usage_body = response.json()
                    _gmsg = groq_usage_body["choices"][0]["message"]
                    ai_reply = arti_reply_policy.trim_if_length_cut(
                        (_gmsg.get("content") or "").strip(),
                        groq_usage_body["choices"][0].get("finish_reason"),
                        f"Groq {current_model}",
                    )
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
                    llm_call["model"] = str(next_model)
                    if "gpt-oss" in next_model.lower():
                        data["include_reasoning"] = False
                        data["max_tokens"] = _reply_tokens_with_thinking(user_speech, 512)
                    else:
                        data.pop("include_reasoning", None)
                        data["max_tokens"] = live_max_tokens_for_trigger(user_speech, CONFIG)
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
                        timeout=(3, 8),
                    )
                    if response.status_code == 200:
                        groq_usage_body = response.json()
                        _gmsg = groq_usage_body["choices"][0]["message"]
                        ai_reply = arti_reply_policy.trim_if_length_cut(
                            (_gmsg.get("content") or "").strip(),
                            groq_usage_body["choices"][0].get("finish_reason"),
                            f"Groq {next_model}",
                        )
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
                    timeout=(3, 8),
                )
                if response.status_code == 200:
                    ai_reply = response.json()["choices"][0]["message"]["content"]
                else:
                    print(f"[Brain Error] Error koneksi SambaNova API: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"[Brain Error] Gagal melakukan request ke SambaNova API: {e}")

    lookup_turn = arti_voice_pipeline.ModelLookupTurn() if CONFIG.get("web_lookup_model_decides") else None

    def lookup_aborted():
        return tts_stop_flag or not screen_privacy.current(privacy_epoch)

    async def announce_lookup():
        if (playing_chat or lookup_aborted() or not speech_control.allows(speech_turn_epoch.get())
                or (trigger.trigger_type == "yt_chat" and _viewer_yields_to_owner(owner_revision))
                or speech_control.superseded(trigger.trigger_type, owner_revision)
                or (time.time() < _media_playback_until and not _media_hold_allows(trigger))
                or (attention_desk.enabled(CONFIG) and not attention_bridge.may_speak(__import__(__name__), trigger))):
            return False
        await arti_expression_runtime.apply_speaking(vts, "neutral", CONFIG)
        return bool(await _speak_reply_chunks(["Aku cek dulu, ya."], privacy_epoch))

    async def do_api_call_with_lookup():
        nonlocal ai_reply, tts_sentence_chunks, llm_system
        async def ask(system):
            nonlocal llm_system, ai_reply, tts_sentence_chunks
            llm_system = system
            ai_reply, tts_sentence_chunks = None, []
            await do_api_call()
            return ai_reply, tts_sentence_chunks
        ai_reply, tts_sentence_chunks = await arti_voice_pipeline.model_lookup_reply(
            ask, llm_system, {**CONFIG, "_screen_privacy_epoch": privacy_epoch},
            regex_lookup_done=bool(getattr(turn, "stages", {}).get("web_lookup_regex")),
            private_contexts=memories,
            abort=lookup_aborted,
            announce=announce_lookup if CONFIG.get("web_lookup_announce") else None,
            lookup_turn=lookup_turn,
        )

    # Execute dengan cancel support
    async def do_api_call_with_fresh_facts():
        nonlocal ai_reply, tts_sentence_chunks, llm_system, user_speech, prompt_content
        from arti_runtime.time_grounding import fresh_reply
        discovery = getattr(trigger.text, "_generic_discovery", False)
        def prepare(row, current):
            nonlocal user_speech, prompt_content
            # ADR 110: reuse fresh_reply's native read, including on its one refresh.
            native = row.get("native_facts") if current["basis"] == "latest_observation" else None
            user_speech = trigger.text.grounded(current, native)
            prompt_content = turn.prompt_for(user_speech)
            if CONFIG.get("voice_append_context"):
                prompt_content = voice_context.with_seed(prompt_content, voice_seed)
        if discovery:
            from arti_runtime.event_framing import DISCOVERY_INSTRUCTION
            llm_system += DISCOVERY_INSTRUCTION
        async def ask(system):
            nonlocal ai_reply, tts_sentence_chunks, llm_system
            llm_system = system
            ai_reply, tts_sentence_chunks = None, []
            if CONFIG.get("voice_append_context"):
                llm_call.pop("local_fallback", None)
            await (do_api_call_with_lookup() if CONFIG.get("web_lookup_model_decides", False)
                   else do_api_call())
            return ai_reply, tts_sentence_chunks
        if CONFIG.get("live7_mind_scope") is True:
            from arti_runtime import mind_scope
            ai_reply, tts_sentence_chunks = await fresh_reply(
                ask, llm_system, lambda: mind_scope.bridge_read_now(__import__(__name__)),
                prepare=prepare if discovery else None)
            return
        ai_reply, tts_sentence_chunks = await fresh_reply(
            ask, llm_system, lambda: arti_mind.read_now(arti_mind.mind_dir(CONFIG)),
            prepare=prepare if discovery else None)

    _llm_cancelled = False
    if CONFIG.get("voice_append_context"):
        _llm_finished = False
    try:
        if _stardew_speech is None:
            current_api_task = asyncio.create_task(
                do_api_call_with_fresh_facts())
            _llm_finished = await _await_llm_deadline(
                current_api_task, CONFIG.get("turn_llm_deadline_sec", 25.0)
            )
            if not _llm_finished:
                print(
                    f"[Brain Warning] LLM deadline {CONFIG.get('turn_llm_deadline_sec', 25.0)}s — "
                    "turn beralih ke fallback yang ada."
                )
                ai_reply = None
    except asyncio.CancelledError:
        print("[Cancel] API call dibatalkan oleh user.")
        ai_reply = None
        _llm_cancelled = True
    except Exception as e:
        print(f"[Brain Error] API call gagal: {e}")
        ai_reply = None
    finally:
        current_api_task = None
    timer.mark("after_llm")
    if CONFIG.get("voice_append_context"):
        refusal_ack = voice_seed.pop("refusal_ack", None)
        if _llm_cancelled or not _llm_finished or not isinstance(refusal_ack, voice_context.RefusalAcknowledgement):
            refusal_ack = None
    if attention_desk.enabled(CONFIG) and llm_call.get("local_fallback"):
        ai_reply, tts_sentence_chunks = None, []  # ADR 172: model speech or silence.
    if CONFIG.get("voice_append_context"):
        if refusal_ack is not None:
            ai_reply = refusal_ack
    if _stardew_speech is None and _llm_cancelled and trigger.trigger_type == "mic_pasif":
        # Dibatalkan PTT = bukan kegagalan provider. ok=True supaya tidak
        # terhitung gagal; penanda pembeda: extra.outcome="cancelled".
        timer.record_llm_call(
            provider=llm_call["provider"], model=llm_call["model"],
            ok=True, config=CONFIG, turn_id=turn_id,
            extra={"outcome": "cancelled"},
        )
    elif _stardew_speech is None:
        timer.record_llm_call(
            provider=llm_call["provider"], model=llm_call["model"],
            ok=bool(ai_reply) and not llm_call.get("local_fallback", False),
            config=CONFIG, turn_id=turn_id,
        )
    if trigger.trigger_type in ("own_play", "read_in_game", "focus", "cut_in", "scene_ended") and not ai_reply:
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        return  # Passing thoughts have no canned fallback on cancellation/provider failure.
    if (not speech_control.allows(speech_turn_epoch.get())
            or _voice_reply_superseded(trigger, owner_revision)):
        print(f"[Queue] Balasan {trigger.trigger_type} tidak diucapkan: dibatalkan sebelum bicara")
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        return
    # Before memory tags, public text, subtitles and speech: a reply generated
    # across revocation is discarded wholesale, never regex-redacted.
    if not screen_privacy.current(privacy_epoch):
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        return
    if _revoked_host_reply(trigger, host_story):
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        return
    playing_chat = trigger.trigger_type == "yt_chat" and _game_actually_playing()
    speech_fallback = ""
    if not (skip_reply_tags or _llm_cancelled) and (host_story_retry or not attention_desk.enabled(CONFIG)):
        if getattr(trigger, "owner_reply_required", False):
            speech_fallback = arti_voice_queue.OWNER_OVERFLOW_ACK
        elif playing_chat and arti_voice_queue.play_chat_due(trigger.queued_at, time.time()):
            speech_fallback = "Makasih udah nunggu, aku belum sempat jawab chat tadi."
    if not ai_reply:
        ai_reply = speech_fallback
    # Giliran mic_pasif: MODEL boleh memilih diam ([DIAM]); PTT yang ditekan
    # selama giliran ini (tts_stop_flag) juga menang. Diam = tidak ada TTS,
    # subtitle, tag, atau baris "Arti" di history.
    if trigger.trigger_type == "mic_pasif":
        if tts_stop_flag or _passive_voice_reply_is_silence(ai_reply):
            if tts_stop_flag or not speech_fallback:
                print("[ASR Pasif] PTT menang — giliran pasif dibuang." if tts_stop_flag
                      else "[ASR Pasif] Arti memilih diam — tidak ada jawaban.")
                await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                return
            ai_reply = speech_fallback
        # Mulai fase bicara: pembatal PTT di _run_passive_turn_preemptible
        # berhenti di sini; TTS dihentikan jalur PTT-ON yang sudah ada.
        _passive_turn_speaking = True
    elif ai_reply and DIAM_TOKEN_RE.search(_normalize_diam_token(ai_reply)):
        # Jaring pengaman SEMUA trigger: token [DIAM] tidak pernah diucapkan.
        ai_reply = _strip_diam_tokens(ai_reply)
        if not ai_reply or DIAM_TOKEN_RE.search(_normalize_diam_token(ai_reply)):
            ai_reply = speech_fallback
        if not ai_reply:
            print(f"[Brain] Jawaban [DIAM] di giliran {trigger.trigger_type} — diam.")
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
            return
    if trigger.trigger_type == "own_play" and ai_reply:
        _passive_turn_speaking = True  # Name priority may yield thinking, never her speech.
    # --- EKSEKUSI JAWABAN AI ---
    if ai_reply:
        # Bersihkan tanda bintang dan yapping bahasa Inggris sebelum memproses lebih lanjut
        ai_reply = clean_ai_reply(ai_reply).strip() or speech_fallback

        if ai_reply:  # Pastikan respon tidak kosong setelah dibersihkan!
            # Parsing jika ada memori baru yang ingin disimpan
            # Format: [MEMORY_SAVE: fact here]
            if "[MEMORY_SAVE:" in ai_reply:
                # mic_pasif bisa saja suara game/video: tidak pernah menulis
                # memori jangka panjang (tag tetap dibuang dari ucapan).
                matches = ([] if trigger.trigger_type == "mic_pasif"
                           else re.findall(r"\[MEMORY_SAVE:\s*(.*?)\]", ai_reply))
                if trigger.trigger_type == "mic_pasif":
                    print("[Memory] Giliran mic_pasif — [MEMORY_SAVE] dibuang, tidak disimpan.")
                for match in matches:
                    await arti_executors.run("background", save_long_term_memory, match)
                    timestamp = time.strftime("%Y-%m-%d")
                    memories.append(f"- [{timestamp}] {match}")
                    print(
                        f"[Memory] Disimpan ke vault — RAG akan ambil saat relevan "
                        f"(reindex: python arti_vault_rag.py --reindex-all)"
                    )

                # Bersihkan tag [MEMORY_SAVE: ...] dari jawaban suara agar Arti tidak mengucapkannya
                ai_reply = re.sub(r"\[MEMORY_SAVE:\s*.*?\]", "", ai_reply).strip()

            ai_reply, session_fact = arti_expression_runtime.parse_reply_session_fact(ai_reply)
            if session_fact:
                _remember_live_session_fact(session_fact)
            ai_reply = ai_reply or speech_fallback

            if ai_reply:  # Cek kembali setelah membuang tag memori
                # Post-processing: anti-meta/narrator + batas adaptif (YT vs PTT)
                # Tag DILUCUTI & DIJALANKAN SEBELUM post-process. Audit
                # 2026-08-05: urutannya dulu terbalik, padahal prompt menyuruh
                # tag ditaruh "[recorded remark omitted]" — jadi pemotong
                # panjang (5 kalimat/580 char) dan filter meta memakan tag-nya
                # lebih dulu. Terbukti [MC: come] dan [MC: leave] LENYAP:
                # Arti bilang "[recorded remark omitted]" lalu mematung.
                if ai_reply:
                    ai_reply = _maybe_execute_reply_tags(
                        ai_reply, trigger, skip_reply_tags or conversation_only or _kept_owner_conversation_only(trigger)
                    )

                if trigger.trigger_type == "mic_pasif" or attention_desk.enabled(CONFIG):
                    # Fallback kaleng ke orang yang tidak bertanya = diam ("").
                    ai_reply = post_process_response(
                        ai_reply, user_speech, {**CONFIG, "_fallback_jadi_diam": True})
                else:
                    ai_reply = post_process_response(ai_reply, user_speech,
                        **({"rehearsal_call": llm_call} if CONFIG.get("rehearsal_parent_pid") else {}))
                ai_reply = ai_reply or speech_fallback

                if ai_reply:  # Cek lagi setelah post-processing
                    ai_reply, turn_emotion = arti_expression_runtime.parse_reply_emotion(ai_reply)
                    turn_emotion = arti_expression_runtime.resolve_turn_emotion(
                        user_speech, turn_emotion
                    )
                    # Kata mentah boleh muncul di hasil internal model, tetapi
                    # dari titik ini semua permukaan publik hanya melihat versi
                    # "sensor": TTS, subtitle, log, history, dan chat game.
                    ai_reply = arti_speech_censor.censor_from_config(ai_reply, CONFIG)
                    # Reply cleanup can join characters that were separated by a removed tag.
                    if DIAM_TOKEN_RE.search(_normalize_diam_token(ai_reply)):
                        ai_reply = _strip_diam_tokens(ai_reply)
                        if not ai_reply or DIAM_TOKEN_RE.search(_normalize_diam_token(ai_reply)):
                            ai_reply = speech_fallback
                            if not ai_reply:
                                await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                                return
                    # Chunk dari do_api_call dibuat dari jawaban MENTAH — sebelum strip
                    # [MEMORY_SAVE], filter panjang, dan strip [EMOTION:]. Tanpa rebuild
                    # ini, tag emosi ikut diucapkan TTS ("emotion senang") dan jawaban
                    # yang dipotong filter tetap dibacakan versi panjangnya.
                    if playing_chat:
                        sentences = re.split(r"(?<=[.!?…])\s+|(?<=\.{3})(?=\w)|(?<=…)(?=\w)", ai_reply)
                        if len(sentences) > 2:
                            ai_reply = " ".join(sentences[:2])
                    tts_sentence_chunks = _sentences_or_empty(ai_reply)
                    if CONFIG.get("expression_emotion_enabled") and turn_emotion != "neutral":
                        print(f"[Expr] mood: {turn_emotion}")
                    print(f"Arti menjawab: \"{ai_reply}\"")
                    # Temuan audit: turn yang SUDAH diproses saat media share
                    # masuk akan mulai TTS menimpa suara klip — tahan mulutnya
                    # sampai playback selesai (jawaban tetap utuh).
                    if time.time() < _media_playback_until and not _media_hold_allows(trigger):
                        print("[Video] Jawaban siap tapi klip masih main — nunggu...")
                        while time.time() < _media_playback_until:
                            await asyncio.sleep(0.25)
                    if getattr(user_speech, "_live7_status", False) and not user_speech.is_current():
                        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                        return
                    if _revoked_host_reply(trigger, host_story):
                        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                        return
                    if ((trigger.trigger_type == "own_play" and _speech_trigger_expired(trigger))
                            or _voice_reply_superseded(trigger, owner_revision)):
                        print(f"[Queue] Balasan {trigger.trigger_type} tidak diucapkan: Bohan bicara lebih dulu")
                        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                        return
                    if (attention_desk.enabled(CONFIG) and not arti_voice_queue.owner_turn(trigger)
                            and not (trigger.trigger_type == "yt_chat" and hotkey_active and not _viewer_ptt_gate_closed())
                            and not attention_bridge.may_speak(__import__(__name__), trigger)):
                        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                        return
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
                        speech_guard = {"host_story": host_story} if host_story else {}
                        voiced = await _speak_reply_chunks(tts_sentence_chunks or [ai_reply], privacy_epoch,
                                                          **speech_guard)
                        if (not voiced and trigger.trigger_type == "yt_chat" and speech_control.allows(speech_turn_epoch.get())
                                and _viewer_yields_to_owner(owner_revision)):
                            _requeue_quiet_chat(trigger, playing=playing_chat, owner_first=True)
                            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                            return
                        if CONFIG.get("stream_game_notes"):
                            if voiced and voiced.spoken_n == len(tts_sentence_chunks or [ai_reply]):
                                from arti_runtime import stream_notes
                                stream_notes.bridge_line(__import__(__name__), "arti", "spoken_reply", ai_reply, epoch=privacy_epoch)
                        ai_reply = _apply_alert_yield_to_reply(ai_reply, voiced)
                        if voiced and attention_desk.enabled(CONFIG):
                            attention_bridge.voiced(__import__(__name__), trigger)
                        if CONFIG.get("rehearsal_parent_pid") and voiced:
                            from scripts.livepath.bridge import reply_source, turn_event
                            turn_event("voiced", turn_id, trigger, reply=ai_reply,  # reply: mabar record only, never logged
                                       source=reply_source(llm_call, generated=_stardew_speech is not None))
                        if voiced and getattr(user_speech, "_live7_status", False):
                            user_speech.mark_voiced()
                        if voiced and getattr(user_speech, "_own_play", False):
                            at = time.time()
                            user_speech.mark_voiced(at)
                            _note_own_play_voiced(at)
                    finally:
                        nod_scope["active"] = False
                        nod_cancel.set()
                    if nod_task is not None:
                        try:
                            await asyncio.wait_for(nod_task, timeout=4.0)
                        except asyncio.TimeoutError:
                            pass
                    if host_story and not voiced:
                        _revoked_host_reply(trigger, host_story)
                        _pending_turn_id = None
                        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                        return
                    timer.mark("after_tts")
                    stages = timer.stages_ms()
                    print(format_latency_line(stages))
                    arti_meta = {
                        "turn_id": turn_id,
                        "latency_ms": stages.get("total_ms"),
                        "stages": stages,
                    }
                    if not screen_privacy.current(privacy_epoch):
                        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                        return
                    if voiced and arti_voice_queue.owner_turn(trigger):
                        _passive_junk_gate.answered(trigger.text)
                    if CONFIG.get("voice_append_context"):  # ADR 200: her line = what was heard, not generated
                        arti_meta["voice_spoken"] = voice_context.spoken_line(
                            ai_reply, tts_sentence_chunks or [ai_reply], voiced,
                            played=voice_context.playback_take(), seed=voice_seed)
                    add_to_history("Arti (VTuber)", ai_reply, arti_meta=arti_meta)
                    _cermin_ke_chat_game(ai_reply)
                    _pending_turn_id = None
                    await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                    await asyncio.sleep(0.35)
                    last_arti_reply_text = ai_reply
                    _finish_reply_ptt(trigger)
                    voice_listener_worker._last_tts_end = time.time()
                    _schedule_post_answer_cleanup()
                    return bool(voiced)
                else:
                    print("[Brain Warning] Jawaban AI kosong setelah post-processing.")
                    await arti_expression_runtime.apply_turn_end(vts, CONFIG)
            else:
                print("[Brain Warning] Jawaban AI tersaring seluruhnya setelah membuang tag memori.")
                await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        else:
            print("[Brain Warning] Jawaban AI kosong atau tersaring seluruhnya sebagai yapping Inggris.")
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
    elif skip_reply_tags:
        # An admitted Stardew turn may be silent after an action, error, or
        # supersession. Empty speech is not an ASR failure or permission to
        # invent a reply/retry; its runtime owns results and continuation.
        await arti_expression_runtime.apply_turn_end(vts, CONFIG)
    else:
        # Semua provider gagal / turn dibatalkan. Dulu di sini ada kalimat
        # HARDCODED zaman awal bridge: "Halo! Aku membaca N catatan sejarah
        # stream kamu..." — inilah "bocoran" yang didengar Bohan berhari-hari
        # (kalimatnya bunyi TIAP kali turn gagal total, termasuk turn proaktif
        # yang dijanjikan "diam saja"). Sekarang: proaktif = beneran diam;
        # panggilan langsung = fallback in-character, bukan meta.
        log_speech = "[Live7 moment]" if getattr(user_speech, "_live7_moment", False) else user_speech
        print(f"\n[Echo Mode + History Context] Kamu memanggil Arti: \"{log_speech}\"")
        print(f"--- BUKU SEJARAH YANG DIBACA ARTI: ---\n{formatted_history}\n----------------------------------")
        # DIAM kalau tidak ada manusia yang menunggu jawaban. Dinilai dari
        # JENIS TRIGGER, bukan prefix teks: tebak-prefix bocor dua kali —
        # "[MINECRAFT" (10 Agu) lalu "[Komentar main game]" (live 22.37, yang
        # PREFIX-nya beda padahal jenisnya sama-sama proaktif). curious =
        # inisiatif & komentar game, game = reaksi event dunia.
        _proaktif = trigger.trigger_type in ("curious", "game") or attention_desk.enabled(CONFIG)
        if _proaktif or user_speech.startswith(
                ("[Curious", "[Inisiatif", "[MINECRAFT", "[Komentar main game")):
            print("[Echo] Turn proaktif/game gagal — diam beneran.")
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
        else:
            if not speech_control.allows(speech_turn_epoch.get()) or _voice_reply_superseded(trigger, owner_revision):
                await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                return
            fb = incharacter_fallback_reply(user_speech)
            print(f"[Echo] Fallback in-character: {fb[:60]}...")
            await arti_expression_runtime.apply_speaking(vts, "neutral", CONFIG)
            voiced = await _speak_reply_chunks([fb], privacy_epoch)
            if voiced and arti_voice_queue.owner_turn(trigger):
                _passive_junk_gate.answered(trigger.text)
            if (not voiced and trigger.trigger_type == "yt_chat" and speech_control.allows(speech_turn_epoch.get())
                    and _viewer_yields_to_owner(owner_revision)):
                _requeue_quiet_chat(trigger, playing=playing_chat, owner_first=True)
                await arti_expression_runtime.apply_turn_end(vts, CONFIG)
                return
            if CONFIG.get("stream_game_notes"):
                if voiced and voiced.spoken_n == 1:
                    from arti_runtime import stream_notes
                    stream_notes.bridge_line(__import__(__name__), "arti", "spoken_reply", fb, epoch=privacy_epoch)
            if CONFIG.get("rehearsal_parent_pid") and voiced:
                from scripts.livepath.bridge import turn_event
                turn_event("voiced", turn_id, trigger, source="incharacter")
            await arti_expression_runtime.apply_turn_end(vts, CONFIG)
            # Kalimat ini BENAR-BENAR diucapkan, jadi pembukuannya harus sama
            # dengan jalur sukses. Audit 2026-08-05: dulu dilewati, sehingga
            # filter anti-gema tidak tahu Arti barusan bicara — kalimatnya bisa
            # dipungut mic/telinga dan jadi trigger baru (loop ngomong sendiri).
            last_arti_reply_text = fb
            voice_listener_worker._last_tts_end = time.time()
            _finish_reply_ptt(trigger)
            return bool(voiced)


async def _main_loop_with_memory_drain():
    try:
        await main_loop()
    finally:
        if _memory_save_tasks:
            await asyncio.gather(*list(_memory_save_tasks), return_exceptions=True)


LIVE_SESSION_KEYS = (
    "youtube_video_id",
    "youtube_chat_enabled",
    "vts_api_port",
    "subtitle_port",
    "active_profile",
    "asr_input_device",
)
LIVE_SESSION_PATH = (str(Path(os.environ["ARTI_REHEARSAL_CONFIG"]).resolve().parent / "live_session.json")
                     if os.environ.get("ARTI_REHEARSAL_CONFIG") else
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_session.json"))
_previous_youtube_video_id = ""


def load_live_session() -> bool:
    """Muat pengaturan stream terakhir dari live_session.json ke CONFIG."""
    global _previous_youtube_video_id
    if not os.path.isfile(LIVE_SESSION_PATH):
        return False
    try:
        with open(LIVE_SESSION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _previous_youtube_video_id = data.get("youtube_video_id", "")
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


# Use the bridge's current config, paths, and callbacks when the wizard runs.
from arti_runtime import startup_wizard as _startup_wizard  # noqa: E402


def _wizard_input(prompt: str, default: str = "") -> str:
    return _startup_wizard._wizard_input(prompt, default, sys=sys, input=input)


def prompt_live_session_setup() -> bool:
    """Tanya YouTube + VTS port SETIAP startup — Enter = pakai nilai sekarang."""
    return _startup_wizard.prompt_live_session_setup(
        config=CONFIG,
        resolve_asr_input_device=resolve_asr_input_device,
        _wizard_input=_wizard_input,
        _extract_yt_video_id=_extract_yt_video_id,
        sys=sys,
        bridge_health=bridge_health,
        print=print,
    )


def startup_wizard():
    """Interactive pre-flight checklist sebelum bridge start.
    Detect missing config, prompt user untuk input, validate.
    Bohan tinggal jawab pertanyaan — ga perlu edit file."""
    return _startup_wizard.startup_wizard(
        config=CONFIG,
        script_path=__file__,
        arti_vault_rag=arti_vault_rag,
        prompt_live_session_setup=prompt_live_session_setup,
        socket=socket,
        sys=sys,
        input=input,
        _supertone_preflight=_supertone_preflight,
        arti_vts_auth=arti_vts_auth,
        save_live_session=save_live_session,
        print=print,
    )


def _extract_yt_video_id(text):
    """Extract YouTube video ID dari URL atau raw input."""
    return _startup_wizard._extract_yt_video_id(text, re=re)


# Nilai per-mesin yang boleh dipersistenkan wizard (semuanya ADA di CONFIG
# sebagai default generik; nilai nyatanya milik mesin uji).
from arti_runtime.startup_wizard import _WIZARD_PERSIST_KEYS  # noqa: E402


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
    return _startup_wizard._save_config_to_file(
        config=CONFIG,
        root_path=_SCRIPT_DIR,
        _WIZARD_PERSIST_KEYS=_WIZARD_PERSIST_KEYS,
        os=os,
        json=json,
        open=open,
        print=print,
    )


if __name__ == "__main__":
    if CONFIG.get("bridge_test_mode"):
        # Text-only real-path test: its own loop and shutdown; none of the live
        # startup (wizard, live_session.json, VTS, TTS, mic, YouTube) or the live
        # shutdown (vault/observer session log) below ever runs.
        import arti_bridge_testmode
        sys.exit(arti_bridge_testmode.main(sys.modules[__name__]))
    # KOTAK HITAM crash native (2026-08-10): python.exe tiga kali mati
    # 0xc0000005 di ntdll.dll (9 Agu 08:02 & 08:39 — dua "[recorded remark omitted]" itu, + 10 Agu 18:13) TANPA satu baris pun jejak — heap
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
    # Ctrl+Break = the same graceful shutdown as Ctrl+C. A bridge launched from a
    # new process group (Git Bash/Start-Process chains) inherits "Ctrl+C ignored",
    # but Ctrl+Break still arrives (dress rehearsal 2026-10-22, pid 26232).
    import signal as _signal
    if hasattr(_signal, "SIGBREAK"):
        _signal.signal(_signal.SIGBREAK, _signal.default_int_handler)
    try:
        load_live_session()
        import arti_api_telemetry as tel
        tel.set_session_id(session_transcript.current_session_id(CONFIG) or "")
        # Skip wizard when non-TTY or --no-wizard (pakai live_session.json + CONFIG)
        _skip_wizard = (not sys.stdin.isatty()) or ("--no-wizard" in sys.argv)
        if _skip_wizard:
            print(
                "[Wizard] Skipped (non-interactive or --no-wizard); "
                "pakai live_session.json + CONFIG."
            )
            needs_save = False
            if not os.environ.get("ARTI_REHEARSAL_CONFIG"):
                arti_vault_rag.prewarm_embedding(CONFIG)
            save_live_session()  # Remember this session even without an interactive wizard.
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
        try:
            _record_session_start_telemetry(CONFIG)
        except Exception as e:
            print(f"[Telemetry] session_start row dilewati ({type(e).__name__})")
        asyncio.run(_main_loop_with_memory_drain())
    except KeyboardInterrupt:
        print("\nBridge dimatikan...")
    finally:
        # PALING AWAL: tutup telinga. Pipeline di bawah (observer + reindex)
        # makan menitan dan selama itu mic/loopback masih menyetor audio ke
        # Groq; kuota terbakar untuk sesi yang sudah usai.
        try:
            hentikan_telinga()
        except Exception as e:  # noqa: BLE001
            print(f"[Shutdown] Telinga warning: {type(e).__name__}: {e}")
        try:
            stop_youtube_chat()
        except Exception as e:  # noqa: BLE001
            print(f"[Shutdown] YouTube chat warning: {type(e).__name__}: {e}")
        try:
            _stop_live7_help_relay()
        except Exception as e:  # noqa: BLE001
            print(f"[Help] Shutdown warning: {type(e).__name__}")
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
        try:
            _stop_stardew_runtime()
        except Exception as e:  # noqa: BLE001
            print(f"[Stardew] Shutdown warning: {type(e).__name__}: {e}")
        try:
            import arti_stardew_driver_guard
            arti_stardew_driver_guard.stop_live_refresher()
        except Exception as e:  # noqa: BLE001
            print(f"[Stardew] Live refresher shutdown warning: {type(e).__name__}")
        try:
            if "arti_stardew_initiative" in sys.modules:
                sys.modules["arti_stardew_initiative"].stop_refresher()
        except Exception as e:  # noqa: BLE001
            print(f"[Stardew] Initiative refresher shutdown warning: {type(e).__name__}")
        save_stream_session_log()
        _shutdown_codex_clients()
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
