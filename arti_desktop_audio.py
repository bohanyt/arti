"""Desktop audio loopback → dialogue ring (RAM). No auto-trigger to voice pipeline.

Telinga Arti (keputusan Bohan 2026-08-02): SELALU nyala saat live. Anti dobel
suara Arti sendiri (routing dia: TTS → CABLE → di-"listen" balik ke headset,
jadi loopback PASTI berisi suara Arti): chunk dicek TTS SEBELUM rekam (skip),
SESUDAH rekam (ingest menolak), cooldown pasca-TTS, plus jaring similarity
is_echo_of_arti. Chunk tumpang tindih omongan Arti = DIBUANG.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_MAX_LINES = 50
DEFAULT_POST_TTS_COOLDOWN_SEC = 3.0
DEFAULT_SAMPLERATE = 16000


def chunk_rms(audio) -> float:
    """RMS chunk audio (float mono). Murni — gerbang hemat kuota: sunyi = skip."""
    import numpy as np

    arr = np.asarray(audio, dtype="float32").reshape(-1)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr * arr)))


MAX_CAPTURE_FAILS = 5

# Halusinasi Whisper di atas musik/sunyi dengan bahasa auto-detect: kredit
# subtitler, ajakan subscribe, "bersambung". Live seharian 2026-08-03:
# 516/679 baris [Dengar] junk; "Продолжение следует..." sampai dikomentari
# Arti on-stream seolah teksnya TERLIHAT di layar. Frasa di sini tidak pernah
# jadi dialog bernilai konteks.
_JUNK_SUBSTRINGS = (
    "субтитры",              # kredit subtitler RU
    "dimatorzok",
    "продолжение следует",   # "bersambung..." RU
    "подпишись",             # "subscribe" RU
    "ご視聴ありがとう",        # "makasih sudah nonton" JP
    "チャンネル登録",          # "subscribe channel" JP
    "구독",                  # "subscribe" KR
    "thanks for watching",
    "thank you for watching",
    "terima kasih telah menonton",
    "terima kasih sudah menonton",
)


def looks_like_whisper_junk(text: str) -> bool:
    """Halusinasi khas jalur desktop — dua gerbang murni.

    (1) < 4 huruf = bukan dialog ("." x446, "¶¶" x51, "you", "Bye.");
    (2) frasa kredit-subtitle/subscribe multibahasa (_JUNK_SUBSTRINGS).
    Saringan bridge (filter_whisper_hallucination) di-tune untuk mic bahasa
    Indonesia dan tidak kenal pola-pola ini.
    """
    t = (text or "").strip().lower()
    if sum(1 for ch in t if ch.isalpha()) < 4:
        return True
    return any(s in t for s in _JUNK_SUBSTRINGS)


def make_loopback_record_chunk(
    config: dict, open_recorder: Callable | None = None
) -> Callable[[], "object | None"]:
    """Bangun callable () -> np.float32 mono | None dari loopback WASAPI.

    - `desktop_audio_device` kosong = IKUT default speaker Windows (kalau Bohan
      pindah ke EarPods, capture ikut pindah — dicek ulang tiap chunk).
    - Recorder dibuka sekali dan dipertahankan; error/ganti device = reopen.
    - Import soundcard DI DALAM closure: modul tetap importable tanpa
      dependensi, dan COM di-init soundcard SENDIRI di thread pemakai.
      JANGAN pre-init COM via ctypes di sini — live pagi 2026-08-03:
      CoInitializeEx manual bikin _COMLibrary soundcard dapat S_FALSE ->
      check_error melempar -> IMPORT modul gagal (dan import gagal tidak
      di-cache) -> "Error 0x100000001" + AttributeError __del__ tiap 5 dtk.
    - `open_recorder(want, samplerate)` -> (obj_dengan_.record, close_fn):
      titik injeksi test; default = soundcard asli.
    - Deadman: MAX_CAPTURE_FAILS gagal beruntun = MENYERAH diam-diam
      (return None cepat), bukan spam error tiap 5 detik selamanya.
    """
    state: dict = {"rec": None, "close": None, "name": None, "fails": 0, "dead": False}
    sr = DEFAULT_SAMPLERATE
    chunk_sec = float(config.get("desktop_audio_chunk_sec", 5.0))
    configured = (config.get("desktop_audio_device") or "").strip()

    def _open_real(want: str, samplerate: int):
        import warnings  # noqa: PLC0415

        import soundcard as sc  # noqa: PLC0415

        # "data discontinuity in recording" = jeda buffer kecil tiap worker
        # sibuk mentranskrip antar-chunk — jinak untuk konteks 5-detikan,
        # tapi spam-nya membanjiri terminal (live pagi 2026-08-03).
        warnings.filterwarnings("ignore", message="data discontinuity in recording")
        mic = sc.get_microphone(want, include_loopback=True)
        ctx = mic.recorder(samplerate=samplerate, channels=1)
        rec = ctx.__enter__()
        print(f"[Desktop Audio] Capture: {mic.name}")
        return rec, lambda: ctx.__exit__(None, None, None)

    opener = open_recorder or _open_real

    def _target_name() -> str:
        if configured:
            return configured
        import soundcard as sc  # noqa: PLC0415

        return str(sc.default_speaker().name)

    def _close() -> None:
        if state["close"] is not None:
            try:
                state["close"]()
            except Exception:  # noqa: BLE001
                pass
        state["rec"] = state["close"] = state["name"] = None

    def _record():
        if state["dead"]:
            time.sleep(2.0)
            return None
        try:
            import numpy as np  # noqa: PLC0415

            want = _target_name()
            if state["rec"] is None or state["name"] != want:
                _close()
                state["rec"], state["close"] = opener(want, sr)
                state["name"] = want
            data = state["rec"].record(numframes=int(chunk_sec * sr))
            state["fails"] = 0
            return np.asarray(data, dtype="float32").reshape(-1)
        except Exception as e:  # noqa: BLE001
            _close()
            state["fails"] += 1
            if state["fails"] >= MAX_CAPTURE_FAILS:
                state["dead"] = True
                print(
                    f"[Desktop Audio] Capture MENYERAH setelah {state['fails']} "
                    f"gagal beruntun ({type(e).__name__}: {e}) — telinga idle. "
                    "Cek device / restart bridge."
                )
                return None
            print(
                f"[Desktop Audio] Capture error ({type(e).__name__}: {e}) — "
                f"coba lagi 5 dtk ({state['fails']}/{MAX_CAPTURE_FAILS})"
            )
            time.sleep(5.0)
            return None

    return _record


@dataclass(frozen=True)
class DialogueEntry:
    wall_ts: float
    text: str


class DialogueRing:
    """Fixed-size RAM ring of recent desktop-audio transcript lines."""

    def __init__(self, max_lines: int = DEFAULT_MAX_LINES):
        self._max = max(1, int(max_lines))
        self._entries: collections.deque[DialogueEntry] = collections.deque(maxlen=self._max)
        self._lock = threading.Lock()

    def append(self, text: str, *, wall_ts: float | None = None) -> None:
        line = (text or "").strip()
        if not line:
            return
        ts = wall_ts if wall_ts is not None else time.time()
        with self._lock:
            self._entries.append(DialogueEntry(wall_ts=ts, text=line))

    def snapshot(self) -> list[DialogueEntry]:
        with self._lock:
            return list(self._entries)

    def format_context(self, max_lines: int = 20, max_chars: int = 2000) -> str:
        lines = self.snapshot()[-max_lines:]
        if not lines:
            return ""
        parts: list[str] = []
        total = 0
        for entry in reversed(lines):
            chunk = entry.text
            if total + len(chunk) > max_chars:
                break
            parts.append(chunk)
            total += len(chunk)
        parts.reverse()
        return "\n".join(parts)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Module-level ring for bridge read access
dialogue_ring = DialogueRing()


def format_context_fresh(
    max_lines: int = 6,
    ttl_sec: float = 180.0,
    *,
    max_chars: int = 800,
    now: float | None = None,
    ring: DialogueRing | None = None,
) -> str:
    """Baris yang masih SEGAR saja (umur < ttl_sec) — untuk injeksi turn normal.

    format_context() biasa dipakai jalur watch-party (20 baris tanpa TTL);
    untuk turn sehari-hari lirik lagu 20 menit lalu tidak boleh bocor ke
    jawaban sekarang — wall_ts (yang dulu tak terpakai) akhirnya bekerja.
    """
    t = now if now is not None else time.time()
    entries = [
        e for e in (ring or dialogue_ring).snapshot()
        if t - e.wall_ts <= ttl_sec
    ][-max_lines:]
    if not entries:
        return ""
    parts: list[str] = []
    total = 0
    for entry in reversed(entries):
        if total + len(entry.text) > max_chars:
            break
        parts.append(entry.text)
        total += len(entry.text)
    parts.reverse()
    return "\n".join(parts)


def should_accept_desktop_transcript(
    text: str,
    *,
    tts_is_playing: bool,
    last_tts_end: float | None,
    is_echo_of_arti: Callable[[str], bool],
    post_tts_cooldown_sec: float = DEFAULT_POST_TTS_COOLDOWN_SEC,
    now: float | None = None,
    chunk_start_ts: float | None = None,
) -> bool:
    """Guards: never accept while Arti TTS; skip echo; cooldown after TTS.

    chunk_start_ts = kapan chunk MULAI direkam. Transkripsi makan 1-2 dtk,
    jadi cek berbasis "sekarang" bisa lolos padahal chunk-nya sendiri
    tumpang tindih omongan Arti (2 bocor echo live seharian 2026-08-03:
    "Wah, masa semangat cari uang bohan gitu sih?" masuk ring). Aturan:
    TTS selesai SETELAH chunk mulai = overlap = buang; cooldown dihitung
    dari awal chunk, bukan dari selesai transkrip.
    """
    if not (text or "").strip():
        return False
    if tts_is_playing:
        return False
    if is_echo_of_arti(text):
        return False
    if last_tts_end is not None:
        if chunk_start_ts is not None and last_tts_end >= chunk_start_ts:
            return False
        ref = (
            chunk_start_ts
            if chunk_start_ts is not None
            else (now if now is not None else time.time())
        )
        if ref - last_tts_end < post_tts_cooldown_sec:
            return False
    return True


def ingest_desktop_transcript(
    text: str,
    *,
    tts_is_playing: bool,
    last_tts_end: float | None,
    is_echo_of_arti: Callable[[str], bool],
    post_tts_cooldown_sec: float = DEFAULT_POST_TTS_COOLDOWN_SEC,
    ring: DialogueRing | None = None,
    chunk_start_ts: float | None = None,
) -> bool:
    """Append to ring if guards pass. Never queues voice triggers.

    post_tts_cooldown_sec dulu tidak diteruskan (selalu 3.0) — kini bisa
    diatur via CONFIG desktop_audio_post_tts_cooldown_sec.
    """
    if not should_accept_desktop_transcript(
        text,
        tts_is_playing=tts_is_playing,
        last_tts_end=last_tts_end,
        is_echo_of_arti=is_echo_of_arti,
        post_tts_cooldown_sec=post_tts_cooldown_sec,
        chunk_start_ts=chunk_start_ts,
    ):
        return False
    target = ring or dialogue_ring
    target.append(text)
    return True


def desktop_audio_worker(
    config: dict,
    *,
    get_tts_is_playing: Callable[[], bool],
    get_last_tts_end: Callable[[], float | None],
    is_echo_of_arti: Callable[[str], bool],
    record_chunk: Callable[[], "object | None"] | None = None,
    transcribe_chunk: Callable[[object], str | None] | None = None,
    filter_text: Callable[[str], str | None] | None = None,
    is_listening: Callable[[], bool] | None = None,
    sleep_sec: float = 0.5,
) -> None:
    """Background thread entry — telinga selalu nyala saat live.

    record_chunk() -> audio np | None; transcribe_chunk(audio) -> teks;
    filter_text = saringan halusinasi whisper (reuse punya bridge);
    is_listening = flag runtime console `dengar on/off`. Semua callable
    diinjeksi supaya loop teruji penuh tanpa hardware/jaringan.
    """
    if not config.get("desktop_audio_enabled"):
        return
    if record_chunk is None or transcribe_chunk is None:
        print("[Desktop Audio] capture/transcribe belum terpasang — idle")
        while config.get("desktop_audio_enabled"):
            time.sleep(2.0)
        return

    min_rms = float(config.get("desktop_audio_min_rms", 0.004))
    cooldown = float(
        config.get("desktop_audio_post_tts_cooldown_sec", DEFAULT_POST_TTS_COOLDOWN_SEC)
    )
    chunk_sec = float(config.get("desktop_audio_chunk_sec", 5.0))
    print(f"[Desktop Audio] Telinga aktif (chunk={chunk_sec}s, min_rms={min_rms})")
    last_accepted = ""
    while config.get("desktop_audio_enabled"):
        try:
            if is_listening is not None and not is_listening():
                time.sleep(1.0)
                continue
            if get_tts_is_playing():
                # Arti lagi ngomong — loopback pasti berisi suaranya: tuli sebentar.
                time.sleep(sleep_sec)
                continue
            chunk_start = time.time()
            audio = record_chunk()
            if audio is None:
                time.sleep(1.0)
                continue
            if chunk_rms(audio) < min_rms:
                continue  # sunyi/musik pelan — nol biaya transkrip
            text = transcribe_chunk(audio)
            if text and filter_text is not None:
                text = filter_text(text)
            if text and looks_like_whisper_junk(text):
                continue
            if text and text.strip() == last_accepted:
                # Musik/loop bikin Whisper mengulang kalimat sama persis
                # tiap chunk — satu salinan di ring sudah cukup konteks.
                continue
            if text:
                accepted = ingest_desktop_transcript(
                    text,
                    tts_is_playing=get_tts_is_playing(),
                    last_tts_end=get_last_tts_end(),
                    is_echo_of_arti=is_echo_of_arti,
                    post_tts_cooldown_sec=cooldown,
                    chunk_start_ts=chunk_start,
                )
                if accepted:
                    last_accepted = text.strip()
                    # Satu baris ringkas per baris yang MASUK ring — supaya
                    # Bohan lihat apa yang Arti dengar (pengganti spam
                    # "Sukses mentranskrip!" tanpa isi).
                    print(f"[Dengar] {text[:90]}")
        except Exception as e:  # noqa: BLE001
            print(f"[Desktop Audio] Error: {type(e).__name__}: {e}")
            time.sleep(2.0)
