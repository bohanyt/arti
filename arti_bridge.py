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
import arti_memory_quality
import arti_voice_queue
import arti_screen_context
import arti_timeline_guard
import arti_vision_client
import arti_curious
import arti_http_util
import arti_voice_pipeline
import arti_groq_stream
import arti_wake
from arti_wake import is_arti_wake_call
import arti_nod

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
    "gemini_api_key": os.environ.get("GEMINI_API_KEY") or "YOUR_GEMINI_API_KEY",
    
    # Model Google AI Studio yang digunakan:
    # - "gemini-2.5-flash"      (Sangat disarankan untuk gemini_live karena Unlimited RPD)
    # - "gemma-4-26b-a4b-it"    (Gemma 4 MoE 26B, Limit 1.5K RPD di Google AI Studio)
    # - "gemma-4-31b-it"        (Gemma 4 Dense 31B, Limit 1.5K RPD di Google AI Studio)
    "gemini_model": "gemini-2.5-flash",              
    
    # Konfigurasi Groq (Super Cepat, Rolling Model = limit gabungan!)
    "groq_api_key": os.environ.get("GROQ_API_KEY") or "YOUR_GROQ_API_KEY",
    "groq_models": [                                  # Rolling round-robin, total RPD = sum semua model
        "qwen/qwen3-32b",                             # 1K RPD - terkuat, multilingual
        "meta-llama/llama-4-scout-17b-16e-instruct",  # 1K RPD - Llama 4 baru
        "llama-3.3-70b-versatile",                    # 1K RPD - 70B besar
        "llama-3.1-8b-instant",                       # 14.4K RPD - backup cepat
    ],
    
    # Konfigurasi SambaNova (Super Cepat, 48K RPD Gratis)
    "sambanova_api_key": os.environ.get("SAMBANOVA_API_KEY") or "YOUR_SAMBANOVA_API_KEY",
    "sambanova_model": "meta-llama-3.1-8b-instruct",  # atau "meta-llama-3.3-70b-instruct"
    
    "vts_api_port": 8002,                             # Port VTS API
    "vts_plugin_name": "ArtiVTuberBridge",
    "vts_developer": "YourDeveloperName",
    "tts_voice": "id-ID-GadisNeural",                 # Indonesian female Edge TTS voice
    "virtual_cable_name": "CABLE Input",
    
    # Konfigurasi YouTube Live Chat (langsung dari YouTube, tanpa extension)
    "youtube_chat_enabled": True,
    "youtube_video_id": "YOUR_VIDEO_ID",              # Ganti dengan Video ID tiap stream (dari URL: youtube.com/watch?v=INI_VIDEO_ID)

    # Konfigurasi OBS Subtitle (in-process WebSocket server + word-level karaoke renderer)
    "subtitle_enabled": True,                         # Master switch: False mematikan in-process subtitle server & semua broadcast
    "subtitle_status_enabled": True,                  # Toggle independen untuk broadcast_status("speaking"/"idle"); diabaikan saat subtitle_enabled=False
    "subtitle_port": 9988,                            # Port WebSocket untuk subtitle.html OBS Browser Source

    # Mode Pemicu Percakapan Streamer:
    # - "wake_word"     : Panggil Arti dengan mengucapkan kata kunci "arti" / "eh arti"
    # - "push_to_talk"   : Mic MEREKAM CASUAL PASIF ke sejarah stream, tetapi HANYA merespon jika menekan hotkey!
    "trigger_mode": "push_to_talk",
    "hotkey_key": "mouse_x2",                         # Side mouse button / hotkey untuk PTT
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
    "groq_model_medium": "meta-llama/llama-4-scout-17b-16e-instruct",
    "groq_model_strong": "qwen/qwen3-32b",
    "groq_model_rare": "llama-3.3-70b-versatile",
    "smart_groq_routing": True,
    "groq_prompt_char_soft_cap": 10000,
    "yt_chat_queue_max": 2,
    "yt_chat_cooldown_sec": 10.0,
    "yt_chat_queue_ttl_sec": 60.0,
    "curious_streamer_quiet_sec": 45.0,
    "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", ""),
    # OpenRouter model slugs — lihat docs/OPENROUTER_MODELS.md
    "openrouter_live_model": "poolside/laguna-xs.2:free",
    "openrouter_live_last_resort": "poolside/laguna-m.1:free",
    "openrouter_live_fast_only": True,
    "openrouter_live_fallback_enabled": True,
    "openrouter_live_timeout_sec": 45,
    "openrouter_summarizer_model": "poolside/laguna-xs.2:free",
    "openrouter_summarizer_fallback": "nvidia/nemotron-3-nano-30b-a3b:free",
    "openrouter_reflection_model": "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter_reflection_fallback_model": "poolside/laguna-m.1:free",
    "openrouter_reflection_last_resort": "poolside/laguna-xs.2:free",
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
    "vault_rag_max_context_chars": 2200,
    "vault_rag_reindex_on_shutdown": True,
    "vault_rag_reindex_shutdown_timeout_sec": 90,
    "memory_startup_max_bullets": 5,
    "llm_system_prompt_max_chars": 5500,

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
    "vision_max_tokens": 256,
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
    "vision_groq_model": "meta-llama/llama-4-scout-17b-16e-instruct",
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
        "poolside/laguna-xs.2:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "owl-alpha",
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
    "arti_debut_date": "YYYY-MM-DD",
    "arti_debut_label": "debut date",
    "arti_archive_from": "YYYY-MM-DD",
    "timeline_guard_enabled": True,
    "vault_rag_history_min_score": 0.32,
    "desktop_audio_enabled": False,
    "desktop_audio_device": "",
    "desktop_audio_chunk_sec": 3.0,
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

    # Mood overlay: param ID yang tidak boleh disentuh saat ekspresi mood (beda tiap model Live2D)
    "expression_mood_strip_param_ids": [],  # tambah Param48, Param122, … sesuai model — lihat docs/VTS-ANIMATION.md

    # Voice prompt tuning (ganti tone/style tanpa edit kode)
    "cohost_name": "Arti",
    "voice_tone_adjectives": "ramah dan natural",
    "voice_reply_style_hint": (
        "Jawab dalam 2-3 kalimat penuh dalam Bahasa Indonesia. "
        "Jangan terlalu pendek atau terlalu panjang."
    ),

    # Vault RAG query boost untuk pertanyaan timeline (suffix pencarian, bukan teks ke viewer)
    "vault_rag_enrich_enabled": True,

    # Hotkey VTS untuk potong motion badan saat aware
    "idle_motion_stop_hotkey": "IdleMotionStop",
    "idle_vts_connect_timeout_sec": 20,
    "idle_vts_connect_retry_sec": 15,
}

# ==========================================
# KONSTANTA PROTOKOL SUPERTONE (NDJSON over stdin/stdout)
# ==========================================
PROTOCOL_VERSION = 1            # Versi protokol NDJSON; hardcoded di bridge & subprocess
SUPERTONE_TIMEOUT_S = 20.0      # Batas waktu sintesis per-utterance
READY_TIMEOUT_S = 60.0          # Batas waktu menunggu ready banner (izinkan download model pertama)
PING_TIMEOUT_S = 5.0            # Batas waktu health-check ping

# Base system prompt — soul/mood/viewer diinject secara dynamic di main_loop()
_SYSTEM_PROMPT_BASE = """[IDENTITAS]
Nama co-host: (lihat ARTI_SOUL.md — edit lokal, tidak di-commit)
Peran: Co-host VTuber AI di live stream
Bahasa: Utama Bahasa Indonesia. Campur slang Inggris boleh ("chat", "stream"), kalimat utama Indonesia.

[KARAKTER]
Lihat ARTI_SOUL.md untuk kepribadian lengkap. Ringkas: ramah, natural, punya opini, patuh instruksi streamer.

[GAYA BICARA]
- Kasual, 2–3 kalimat per jawaban
- Panggil viewer dengan nama mereka
- JANGAN asterisk, markdown, emoji, atau tag <laugh> — pakai "haha"/"hehe" jika perlu
- Jangan kutip format log, timestamp, atau label [Streamer]/[Arti]

[ATURAN MUTLAK]
1. Jangan jawab bahasa Inggris penuh
2. Jangan jelaskan proses berpikir atau sistem (RAG, prompt, file)
3. Maks 3 kalimat
4. Streamer adalah host — patuhi instruksi langsungnya
5. Tetap dalam karakter co-host

[FEW-SHOT — contoh generik]
Viewer: "eh co-host, kamu pakai AI apa?"
Co-host: "Kok nanya gitu sih? Rahasia dong, kepo banget deh~"

Streamer: "menurut kamu gimana?"
Co-host: "Hmm, menurutku sih oke aja — tapi tergantung konteksnya ya."

Viewer: "halo!"
Co-host: "Halo juga! Ada apa nih?"

[KONTEKS]
Gunakan konteks stream untuk jawab relevan, bukan sapaan generik.

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


voice_trigger_queue = queue.Queue()  # legacy alias; use voice_trigger_buffer
voice_trigger_buffer = arti_voice_queue.VoiceTriggerQueue(
    max_yt=int(CONFIG.get("yt_chat_queue_max", 2)),
    ttl_sec=float(CONFIG.get("yt_chat_queue_ttl_sec", 60.0)),
)
_pending_turn_id = None
_bridge_shutting_down = False
_last_yt_trigger_by_viewer: dict[str, float] = {}
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
    global _pending_turn_id

    normalized_type = trigger_type
    if trigger_type in ("ptt", "wake_word", "push_to_talk"):
        normalized_type = "mic"
    always_queue = normalized_type in ("yt_chat", "mic")

    with _brain_busy_lock:
        if (_brain_busy or tts_is_playing) and not always_queue:
            print(
                f"[Queue] Skip trigger ({trigger_type}) — Arti masih proses/TTS: "
                f"\"{text[:200]}\""
            )
            return

    if asr_stages:
        pipeline_timer.set_pending_asr_stages(asr_stages)

    item = arti_voice_queue.QueuedVoiceTrigger(
        text=text,
        trigger_type=normalized_type,
        viewer_name=viewer_name,
    )
    if not voice_trigger_buffer.enqueue(item):
        if normalized_type == "curious":
            print("[Queue] Curious deferred — YT pending di antrian")
        return

    _pending_turn_id = session_transcript.log_trigger(
        normalized_type, viewer_name, text[:500], CONFIG
    )
    depth = len(voice_trigger_buffer)
    print(f"[Queue] Trigger ({normalized_type}) depth={depth}: \"{text[:200]}\"")
    if normalized_type == "yt_chat":
        global _last_yt_chat_trigger_ts
        _last_yt_chat_trigger_ts = time.time()
        who = viewer_name or "viewer"
        print(f"[YT Chat] Antri jawab {who} (VTS turn di main loop)")


# === CATEGORIZED CONTEXT (Phase 4: optimized) ===
_LOG_LINE_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+\[([^\]]+)\]\s+(.*)$"
)


def _format_history_line_for_prompt(line: str) -> str:
    """Parse internal log line; emit clean dialog without timestamps."""
    m = _LOG_LINE_RE.match(line.strip())
    if not m:
        return line.strip()
    source, msg = m.group(2), m.group(3).strip()
    if source == "Streamer":
        return f"Streamer: {msg}"
    if source.startswith("Viewer"):
        vm = re.search(r"Viewer\s+([^\s(]+)", source)
        name = vm.group(1) if vm else "viewer"
        return f"{name}: {msg}"
    if "Arti" in source:
        return f"Arti: {msg}"
    return f"{source}: {msg}"


STREAMER_HISTORY_MAX = 5   # reduced from 10 to save tokens
VIEWER_HISTORY_MAX = 3     # per viewer (ringkasan)
ARTI_HISTORY_MAX = 3       # reduced from 5 to save tokens


def get_categorized_history():
    """Return history yang sudah di-categorize untuk prompt (tanpa timestamp log)."""
    with history_lock:
        all_history = list(stream_history)

    streamer_lines = []
    viewer_lines = {}
    arti_lines = []

    for line in all_history:
        if "[Streamer]" in line:
            streamer_lines.append(line)
        elif "[Viewer" in line:
            re_match = re.search(r"\[Viewer @?(\w+)", line)
            if re_match:
                vname = re_match.group(1)
                if vname not in viewer_lines:
                    viewer_lines[vname] = []
                viewer_lines[vname].append(line)
        elif "[Arti (VTuber)]" in line:
            arti_lines.append(line)

    result = []

    if streamer_lines:
        result.append("=== OMONGAN STREAMER TERAKHIR ===")
        for line in streamer_lines[-STREAMER_HISTORY_MAX:]:
            result.append(_format_history_line_for_prompt(line))

    if viewer_lines:
        result.append("\n=== CHAT VIEWER TERAKHIR ===")
        for vname, lines in viewer_lines.items():
            result.append(_format_history_line_for_prompt(lines[-1]))

    if arti_lines:
        result.append("\n=== JAWABAN ARTI TERAKHIR ===")
        for line in arti_lines[-ARTI_HISTORY_MAX:]:
            result.append(_format_history_line_for_prompt(line))

    return "\n".join(result) if result else "(Belum ada history)"


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

    if prompt_chars > int(cfg.get("groq_prompt_char_soft_cap", 10000)):
        return _pick(fast)

    if not cfg.get("smart_groq_routing", True):
        if not hasattr(pick_groq_model, "_rr_idx"):
            pick_groq_model._rr_idx = 0
        m = models[pick_groq_model._rr_idx % len(models)]
        pick_groq_model._rr_idx += 1
        return m

    medium = cfg.get("groq_model_medium", "meta-llama/llama-4-scout-17b-16e-instruct")
    strong = cfg.get("groq_model_strong", "qwen/qwen3-32b")
    rare = cfg.get("groq_model_rare", "llama-3.3-70b-versatile")

    t = (user_text or "").lower()
    complex_kw = (
        "kenapa", "jelaskan", "bagaimana", "bandingkan",
        "explain", "detail", "ceritain", "maksudnya",
    )
    if len(user_text) > 180 or sum(1 for k in complex_kw if k in t) >= 2:
        return _pick(rare)
    if "?" in user_text or len(user_text) > 100 or any(k in t for k in complex_kw):
        return _pick(strong)
    if len(user_text) > 55:
        return _pick(medium)
    return _pick(fast)


def pick_groq_model_for_turn(
    user_text: str,
    config: dict | None = None,
    *,
    trigger_type: str = "mic",
    prompt_chars: int = 0,
    queue_depth: int = 0,
) -> str:
    """Route Groq model per turn type (no rolling for voice)."""
    cfg = config or CONFIG
    models = cfg.get("groq_models", ["llama-3.1-8b-instant"])
    fast = cfg.get("groq_model_fast", "llama-3.1-8b-instant")
    strong = cfg.get("groq_model_strong", "qwen/qwen3-32b")

    def _pick(preferred: str) -> str:
        return preferred if preferred in models else (models[0] if models else preferred)

    if not cfg.get("smart_groq_routing", True):
        return pick_groq_model(user_text, cfg, prompt_chars)

    if trigger_type == "curious":
        return _pick(fast)
    if trigger_type == "yt_chat":
        return _pick(strong)
    return pick_groq_model(user_text, cfg, prompt_chars)


def _groq_fallback_chain(primary: str, config: dict) -> list[str]:
    """Urutan coba: model pilihan dulu, lalu sisanya (8b-instant diutamakan saat limit)."""
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


def _streamer_spoke_within_sec(sec: float) -> bool:
    """True if streamer spoke within the last sec seconds (from history timestamps)."""
    with history_lock:
        lines = list(stream_history)
    for line in reversed(lines):
        if "[Streamer]" not in line:
            continue
        m = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]", line)
        if not m:
            return True
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        now = time.localtime()
        line_sec = h * 3600 + mi * 60 + s
        now_sec = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
        diff = now_sec - line_sec
        if diff < 0:
            diff += 86400
        return diff < sec
    return False


def build_origin_context(config: dict | None = None) -> str:
    """Fakta kanon debut + pointer arsip — selalu inject (hemat token)."""
    cfg = config or CONFIG
    label = cfg.get("arti_debut_label", "debut date")
    archive = cfg.get("arti_archive_from", "YYYY-MM-DD")
    return (
        f"\n\n[ASAL USUL ARTI]\n"
        f"Debut co-host live: {label} ({cfg.get('arti_debut_date', 'YYYY-MM-DD')}).\n"
        f"Arsip sesi per hari: vault/sessions/ sejak {archive}-default.md (lihat index.md).\n"
        f"Kalau ditanya sejak kapan Arti ada: jawab {label}, bukan tanggal sesi hari ini."
    )


def build_startup_memory_block(memories: list[str]) -> str:
    """Cuplikan memori startup — sisanya lewat Vault RAG per query (hemat token Groq)."""
    import arti_memory_quality

    max_b = int(CONFIG.get("memory_startup_max_bullets", 0))
    if max_b <= 0:
        if CONFIG.get("vault_rag_enabled", True):
            return (
                "\n\n[MEMORI JANGKA PANJANG: otomatis dari Vault RAG saat jawab — "
                "jangan sebut database/RAG/file vault.]"
            )
        return ""
    today_memories = arti_memory_quality.filter_memories_for_startup(memories)
    if not today_memories:
        if CONFIG.get("vault_rag_enabled", True):
            return (
                "\n\n[MEMORI JANGKA PANJANG: otomatis dari Vault RAG saat jawab — "
                "jangan sebut database/RAG/file vault.]"
            )
        return ""
    return "\n\n[MEMORI TERBARU (hari ini):]\n" + "\n".join(today_memories[-max_b:])


_SYSTEM_PROMPT_BLOCK_MARKERS = (
    "\n\n[RINGKASAN KONTEKS TERAKHIR]",
    "\n\n[MEMORI TERBARU",
    "\n\n[MEMORI JANGKA PANJANG",
    "\n\n[ARTI'S LONG-TERM MEMORY",
    "\n\n[VAULT RAG",
    "\n\n[VIEWER YANG DIKETAHUI:]",
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
    voice_trigger_buffer.clear()

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


def add_to_history(source, message, arti_meta=None):
    """Menambahkan aktivitas ke dalam buku catatan sejarah stream secara aman"""
    if not message or not message.strip():
        return
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
# DYNAMIC LEARNING & VAULT INTEGRATION (LOCK-AWARE HARNESS)
# ==========================================
# Paths Setup untuk Locking Protocol
LOCK_DIR = os.path.join(os.path.expanduser("~"), ".arti-locks")
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

    timeout = int(CONFIG.get("vault_rag_reindex_shutdown_timeout_sec", 120))

    def _reindex_worker():
        arti_vault_rag.reindex_shutdown(CONFIG)

    print(
        f"[Vault RAG] Shutdown reindex di background (max wait {timeout}s). "
        "Manual: python arti_vault_rag.py --reindex-all"
    )
    t = threading.Thread(target=_reindex_worker, name="vault-rag-reindex", daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        print("[Vault RAG] Reindex lanjut di background — Ctrl+C tidak perlu tunggu.")

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
            except Exception:
                break

    async def connect(self):
        uri = f"ws://localhost:{CONFIG['vts_api_port']}"
        try:
            self.websocket = await websockets.connect(uri)
            print(f"[VTS] Terhubung ke VTube Studio API di port {CONFIG['vts_api_port']}")
            self._reader_stop = False
            self._reader_task = asyncio.create_task(self._reader_loop())
            await self.authenticate()
        except Exception as e:
            print(f"[VTS Error] Gagal connect ke VTS. Pastikan 'Start API' di VTS Settings aktif! Error: {e}")

    async def send_request(self, message_type, data=None, *, timeout=3.0):
        if not self.websocket:
            raise RuntimeError("VTS not connected")
        rid = f"Arti_{time.time_ns()}"
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
                "explanation": f"Arti Bridge: {name}",
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
            pass  # avoid crashing bridge on eye-tracking errors

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
            async with self._ws_send_lock:
                await self.websocket.send(json.dumps(payload))
            if fut:
                await asyncio.wait_for(fut, timeout=0.6)
        except Exception:
            pass
        finally:
            if fut:
                self._pending.pop(rid, None)

    _EXPR_MIKIR = "VtuberMikir.exp3.json"
    _EXPR_BICARA = "VtuberBicara.exp3.json"
    _EXPR_AWARE = "VtuberAware.exp3.json"
    _EXPR_DEFAULT = "VtuberDefault1.exp3.json"

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
        # argv list, NEVER shell=True → no shell injection (Req 9.2, 18.1/18.3).
        self.proc = subprocess.Popen(
            [py, engine_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,                        # line-buffered
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
    """Mic ke-detect suara speaker / jawaban Arti sendiri."""
    if not last_arti_reply_text:
        return False
    import difflib
    ratio = difflib.SequenceMatcher(
        None, _normalize_asr_text(text), _normalize_asr_text(last_arti_reply_text)
    ).ratio()
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
    
    # Phrases yang HANYA hallucination kalau ini hasil transkrip pasif (bukan streamer ngomong langsung)
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
    # Kalau streamer beneran bilang "tidak" atau "bye" sendiri, biarin masuk
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


def transcribe_audio(audio_array, samplerate=16000, use_groq=True):
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
    groq_key = CONFIG.get("groq_api_key")
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
                data = {
                    "model": groq_model,
                    "language": "id"  # Paksa Bahasa Indonesia
                }
                res = arti_http_util.groq_session().post(
                    url, headers=headers, files=files, data=data, timeout=8
                )

            if os.path.exists(tmp_audio_path):
                os.unlink(tmp_audio_path)

            ms = int((time.perf_counter() - t0) * 1000)
            if res.status_code == 200:
                text = res.json().get("text", "").strip()
                if text:
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
        segments, _ = local_whisper_model.transcribe(audio_array, beam_size=1, language="id")
        text = " ".join([seg.text for seg in segments]).strip()
        ms_local = int((time.perf_counter() - t0_local) * 1000)
        if text:
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
                segments, _ = local_whisper_model.transcribe(audio_array, beam_size=1, language="id")
                text = " ".join([seg.text for seg in segments]).strip()
                ms_cpu = int((time.perf_counter() - t0_local) * 1000)
                if text:
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
        renderer = item.get('liveChatTextMessageRenderer') or item.get('liveChatPaidMessageRenderer')
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
        
        if not msg: return None
        return {'name': author, 'message': msg}
    
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
        viewer = msg['name']
        chat_msg = msg['message']

        print(f"\U0001f4ac [YT Chat] {viewer}: {chat_msg}")
        add_to_history(f"Viewer {viewer} (YouTube)", chat_msg)

        if is_arti_wake_call(chat_msg):
            cooldown = float(CONFIG.get("yt_chat_cooldown_sec", 10.0))
            last = _last_yt_trigger_by_viewer.get(viewer, 0.0)
            if time.time() - last >= cooldown:
                print(f"[YT Chat] Panggilan dari {viewer} terdeteksi!")
                queue_voice_trigger(
                    f"[Pesan Live Chat dari Viewer {viewer} (YouTube)]: {chat_msg}",
                    trigger_type="yt_chat",
                    viewer_name=viewer,
                )
                _last_yt_trigger_by_viewer[viewer] = time.time()
            else:
                remain = cooldown - (time.time() - last)
                print(
                    f"[YT Chat Info] Panggilan dari {viewer} diabaikan "
                    f"(cooldown {remain:.0f}s)."
                )
    
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

def start_desktop_audio_worker():
    if not CONFIG.get("desktop_audio_enabled"):
        return

    def _run():
        arti_desktop_audio.desktop_audio_worker(
            CONFIG,
            get_tts_is_playing=lambda: tts_is_playing,
            get_last_tts_end=lambda: getattr(
                voice_listener_worker, "_last_tts_end", None
            ),
            is_echo_of_arti=is_asr_echo_of_arti,
            transcribe_chunk=None,
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


def refresh_vision_for_turn() -> None:
    """On-demand screenshot describe (bukan Groq) sebelum jawaban."""
    if not is_vision_active():
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
                                        if _bridge_shutting_down:
                                            recording.clear()
                                            is_speaking = False
                                            silence_duration = 0
                                            continue
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
                                        # OFF: catat ke history aja (skip saat TTS/sibuk/shutdown)
                                        with _brain_busy_lock:
                                            asr_busy = _brain_busy
                                        if _bridge_shutting_down or tts_is_playing or asr_busy:
                                            recording.clear()
                                            is_speaking = False
                                            silence_duration = 0
                                            continue
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
                                    with _brain_busy_lock:
                                        asr_busy = _brain_busy
                                    if _bridge_shutting_down or tts_is_playing or asr_busy:
                                        recording.clear()
                                        is_speaking = False
                                        silence_duration = 0
                                        continue
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
            or ""
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
        if _bridge_shutting_down:
            break
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
    if _bridge_shutting_down:
        return
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
        if _bridge_shutting_down:
            break
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

def _viewer_names_for_fallback(max_names: int = 3) -> list[str]:
    viewers_path = os.path.join(_SCRIPT_DIR, "ARTI_VIEWERS.md")
    if not os.path.isfile(viewers_path):
        return []
    names = []
    with open(viewers_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("### "):
                names.append(line[4:].strip())
    return names[:max_names]


def incharacter_fallback_reply(user_speech: str) -> str:
    """Jawaban darurat kalau LLM keluar narrator/meta atau strip menghapus semua."""
    msg = _extract_trigger_message(user_speech).lower()
    low = (user_speech or "").lower()

    if any(k in msg for k in ("nyala", "hidup", "on gak", "on ga", "on gk", "masih hidup", "nyala gk", "nyala gak")):
        return "Iya nyala kok! Masih on di sini, ada apa nih?"
    if "ngelag" in msg or ("otak" in msg and "lag" in msg):
        return "Iya kadang lemot sih, tapi masih bisa ngobrol—ada apa?"
    if any(k in msg for k in ("sampai jumpa", "dadah", "bye", "goodbye")):
        return "Oke guys, co-host dulu ya! Bye~"
    if "cita" in msg:
        return "Cita-citaku? Jadi co-host terkeren lah, haha!"
    if "ayah" in msg and "streamer" in msg:
        return "Siap, mode serius ON deh, haha!"
    if "dengar" in msg or "dengar" in low:
        return "Iya dengar kok! Jelas banget, ada apa nih?"
    if "viewer" in low and ("siapa" in low or "ingat" in low or "inget" in low):
        names = _viewer_names_for_fallback(3)
        if names:
            joined = ", ".join(names)
            return f"Yang sering keinget tuh {joined}—ada lagi yang baru nongol nanti."
    return "Eh bentar, otakku ngelag—ulang pertanyaannya dong?"


def post_process_response(text, user_speech=None):
    """Post-processing response: enforce 2 kalimat max, clean up artifacts."""
    _ = user_speech  # opsional — dipakai versi filter lanjutan / tes
    if not text:
        return ""
    
    # Split ke kalimat
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    # Enforce max 2 kalimat
    if len(sentences) > 2:
        sentences = sentences[:2]
    
    result = " ".join(sentences).strip()
    result = strip_tts_expression_tags(result)
    result = re.sub(r"\s+", " ", result)
    return result


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
IDLE_EXPRESSIONS = [f"VtuberIdle{i}" for i in range(1, 51)]
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
    """OFF-kan VtuberIdle aktif segera (idle worker thread), hindari bentrok ekspresi jawaban."""
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
    """Matikan satu file VtuberIdle{N} di idle websocket."""
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
    """Proses hotkey + deactivate VtuberIdle dari thread utama."""
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

    # Load target poses from expression files
    MODEL_DIR = os.environ.get(
        "VTS_MODEL_DIR",
        r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio"
        r"\VTube Studio_Data\StreamingAssets\Live2DModels\YOUR_MODEL",
    )
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
    """Pause idle tracks + OFF VtuberIdle aktif + reset head di idle ws."""
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
    print("=== ARTI VTUBER CO-HOST BRIDGE ===")
    
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
    init_global_hotkey()
    init_vision_hotkey()

    if CONFIG.get("health_check_on_startup", True):
        _hc_cfg = {
            **CONFIG,
            "openrouter_api_key": (
                CONFIG.get("openrouter_api_key")
                or os.environ.get("OPENROUTER_API_KEY")
                or ""
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
    viewer_block = f"\n\n[VIEWER YANG DIKETAHUI:]\n{viewer_context}" if viewer_context else ""
    
    # Summarizer context (update tiap 5 trigger, dari OpenRouter)
    summarizer_context = get_summarizer_context()
    
    origin_block = build_origin_context()
    # FIX P1: Build system prompt — only add non-empty blocks
    dynamic_system_prompt = _SYSTEM_PROMPT_BASE + origin_block + memory_block + mood_block
    if viewer_block:
        dynamic_system_prompt += viewer_block
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
            models = CONFIG.get('groq_models', ['qwen/qwen3-32b'])
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
    
    print(f"\n🟢 SISTEM SIAP! [Profil: {profile}] Panggil Arti dengan 'eh arti' atau 'arti'...")
    print("--------------------------------------------------------------------------------")
    
    # Loop Utama
    while True:
        await asyncio.sleep(0.1)

        # Curious proactive (idle commentary on screen)
        if CONFIG.get("curious_enabled") and is_vision_active() and not _bridge_shutting_down:
            quiet_sec = float(CONFIG.get("curious_streamer_quiet_sec", 45.0))
            streamer_recent = _streamer_spoke_within_sec(quiet_sec)
            with _brain_busy_lock:
                brain_busy = _brain_busy
            if arti_curious.should_fire(
                CONFIG,
                brain_busy=brain_busy,
                tts_playing=tts_is_playing,
                ptt_active=hotkey_active,
                yt_queue_pending=voice_trigger_buffer.has_yt_pending(),
                streamer_recent=streamer_recent,
            ):
                if arti_curious.prepare_for_fire(CONFIG):
                    curious_text = arti_curious.build_prompt(CONFIG)
                    arti_curious.mark_fired()
                    queue_voice_trigger(curious_text, trigger_type="curious")
                    print("[Curious] Proactive trigger queued")

        queued = voice_trigger_buffer.dequeue()
        if queued is None:
            continue

        queue_depth = len(voice_trigger_buffer)
        trigger = VoiceTrigger(queued.text, queued.trigger_type, queued.viewer_name)

        with _brain_busy_lock:
            if _brain_busy:
                voice_trigger_buffer.enqueue(
                    arti_voice_queue.QueuedVoiceTrigger(
                        text=trigger.text,
                        trigger_type=trigger.trigger_type,
                        viewer_name=trigger.viewer_name,
                    )
                )
                continue
            _brain_busy = True

        try:
            await _handle_voice_trigger(
                trigger, memories, dynamic_system_prompt, queue_depth=queue_depth
            )
        except Exception as e:
            print(f"[Error] Masalah di main loop: {e}")
            with _brain_busy_lock:
                _brain_busy = False
            await vts.trigger_expression_state("default")
        else:
            with _brain_busy_lock:
                _brain_busy = False


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
    """Screen + optional watch party blocks."""
    llm_system = _append_screen_context(llm_system)
    return _append_watch_party_context(llm_system)


async def _handle_voice_trigger(
    trigger: VoiceTrigger,
    memories: list,
    dynamic_system_prompt: str,
    *,
    queue_depth: int = 0,
):
    """Satu trigger sekaligus: mikir → RAG → Groq → TTS (no overlap)."""
    global _pending_turn_id, hotkey_active, last_arti_reply_text, current_api_task

    user_speech = trigger.text
    timer = PipelineTimer(extra=pipeline_timer.pop_asr_stages())
    await _prepare_turn_start(trigger.trigger_type, trigger.viewer_name)
    await asyncio.to_thread(refresh_vision_for_turn)
    timer.mark("after_mikir")

    # Kumpulkan seluruh catatan sejarah 50 aktivitas sebelumnya untuk dikirim ke LLM
    with history_lock:
        current_history = list(stream_history)

    # Pakai categorized history + RAG parallel (arti_voice_pipeline)
    turn = await arti_voice_pipeline.prepare_turn_context(
        user_speech,
        memories,
        dynamic_system_prompt,
        CONFIG,
        trim_system_prompt=trim_system_prompt_for_llm,
        append_watch_party_context=_append_live_context,
        get_categorized_history=get_categorized_history,
        extract_trigger_message=_extract_trigger_message,
    )
    formatted_history = turn.formatted_history
    llm_system = turn.llm_system
    prompt_content = turn.prompt_content
    target_instruction = turn.target_instruction
    rag_query = turn.rag_query
    timer.mark("after_rag")

    ai_reply = None
    provider = CONFIG["api_provider"].lower()
    tts_sentence_chunks: list[str] = []

    # === WRAP API CALL IN CANCELLABLE TASK ===
    async def do_api_call():
        """Semua API calls diwrap di sini biar bisa di-cancel."""
        nonlocal ai_reply, tts_sentence_chunks

        # --- JALUR GOOGLE AI STUDIO (GEMINI LIVE WEBSOCKET API - UNLIMITED RPD) ---
        if provider == "gemini_live" and CONFIG["gemini_api_key"] and CONFIG["gemini_api_key"] != "YOUR_GEMINI_API_KEY":
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
            est_chars = len(llm_system) + len(prompt_content)
            current_model = pick_groq_model_for_turn(
                user_speech,
                CONFIG,
                trigger_type=trigger.trigger_type,
                prompt_chars=est_chars,
                queue_depth=queue_depth,
            )
            groq_max_tokens = 100 if trigger.trigger_type == "yt_chat" and queue_depth >= 1 else 150
            groq_model_used = current_model
            groq_voice_ms = 0
            groq_voice_ok = False
            groq_usage_body: dict | None = None
            print(
                f"\n[Groq API] {trigger.trigger_type} depth={queue_depth} → {current_model} "
                f"dengan {len(current_history)} pesan sejarah stream..."
            )
            try:
                headers = {"Authorization": f"Bearer {CONFIG['groq_api_key']}", "Content-Type": "application/json"}
                user_content = prompt_content
                if "qwen" in current_model.lower():
                    user_content = prompt_content + "\n/no_think"
                data = {
                    "model": current_model,
                    "max_tokens": groq_max_tokens,
                    "messages": [
                        {"role": "system", "content": llm_system},
                        {"role": "user", "content": user_content}
                    ]
                }
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
                    if response.status_code == 200:
                        groq_usage_body = response.json()
                        ai_reply = groq_usage_body["choices"][0]["message"]["content"]
                        groq_model_used = data.get("model", current_model)
                        groq_voice_ok = True
                    elif response.status_code == 429:
                        fallback_models = _groq_fallback_chain(current_model, CONFIG)[1:]
                        for next_model in fallback_models:
                            print(f"[Groq] Rate limit di {data['model']}, coba {next_model}...")
                            data["model"] = next_model
                            response = await arti_http_util.post_in_thread(
                                arti_http_util.groq_session(),
                                "https://api.groq.com/openai/v1/chat/completions",
                                headers=headers,
                                json=data,
                            )
                            if response.status_code == 200:
                                groq_usage_body = response.json()
                                ai_reply = groq_usage_body["choices"][0]["message"]["content"]
                                groq_model_used = next_model
                                groq_voice_ok = True
                                break
                        if not groq_voice_ok:
                            print("[Brain Error] Groq retry chain gagal setelah rate limit")
                    else:
                        print(
                            f"[Brain Error] Error koneksi Groq API: "
                            f"{response.status_code} - {response.text}"
                        )
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
        ai_reply = clean_ai_reply(ai_reply).strip()
        ai_reply = arti_memory_quality.strip_history_echo(ai_reply)
        if not ai_reply or len(ai_reply) < 8:
            fb = incharacter_fallback_reply(user_speech)
            if fb:
                print(f"[Filter] History echo / jawaban kosong — fallback in-character")
                ai_reply = fb

        if ai_reply:
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
                # Post-processing: enforce 2 kalimat max
                ai_reply = post_process_response(ai_reply)

                if ai_reply:  # Cek lagi setelah post-processing
                    ai_reply, turn_emotion = arti_expression_runtime.parse_reply_emotion(ai_reply)
                    turn_emotion = arti_expression_runtime.resolve_turn_emotion(
                        user_speech, turn_emotion
                    )
                    if CONFIG.get("expression_emotion_enabled") and turn_emotion != "neutral":
                        print(f"[Expr] mood: {turn_emotion}")
                    print(f"Arti menjawab: \"{ai_reply}\"")
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
        # Mode offline echo jika API Key kosong atau terjadi error API
        print(f"\n[Echo Mode + History Context] Kamu memanggil Arti: \"{user_speech}\"")
        print(f"--- BUKU SEJARAH YANG DIBACA ARTI: ---\n{formatted_history}\n----------------------------------")
        await arti_expression_runtime.apply_speaking(vts, "neutral", CONFIG)
        await tts.speak(f"Halo! Aku membaca {len(current_history)} catatan sejarah stream kamu, dan mendengar kamu memanggil namaku!")
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
    Streamer tinggal jawab pertanyaan — ga perlu edit file."""
    
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


def _save_config_to_file():
    """Update CONFIG dict values back to file."""
    # Find the CONFIG block in file and update specific keys
    config_path = os.path.join(_SCRIPT_DIR, "arti_bridge.py")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Update youtube_video_id — dynamic from CONFIG
        if CONFIG.get("youtube_video_id"):
            new_id = CONFIG["youtube_video_id"]
            content = re.sub(
                r'"youtube_video_id"\s*:\s*"[^"]*"',
                f'"youtube_video_id": "YOUR_VIDEO_ID"',
                content
            )
        
        # Update vts_api_port — dynamic from CONFIG
        content = re.sub(
            r'"vts_api_port":\s*\d+',
            f'"vts_api_port": {CONFIG["vts_api_port"]}',
            content
        )

        # Update subtitle_port — dynamic from CONFIG
        content = re.sub(
            r'"subtitle_port":\s*\d+',
            f'"subtitle_port": {CONFIG["subtitle_port"]}',
            content
        )
        
        # Update youtube_chat_enabled — dynamic from CONFIG
        content = re.sub(
            r'"youtube_chat_enabled":\s*(True|False)',
            f'"youtube_chat_enabled": {CONFIG["youtube_chat_enabled"]}',
            content
        )
        
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
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
                    "Simpan juga ke arti_bridge.py sebagai default? (y/N): "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                save_choice = ""
            if save_choice == "y":
                _save_config_to_file()
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\nBridge dimatikan...")
        _bridge_shutting_down = True
    finally:
        _bridge_shutting_down = True
        stop_scouter()
        if scouter_thread is not None and scouter_thread.is_alive():
            scouter_thread.join(timeout=3.0)
        stop_idle_animation()
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
