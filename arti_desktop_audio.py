"""Desktop audio loopback → dialogue ring (RAM). No auto-trigger to voice pipeline.

Telinga Arti (keputusan streamer 2026-08-02): SELALU nyala saat live. Anti dobel
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


def pilih_loopback_device(devices, want: str):
    """Pilih device yang benar saat NAMANYA kembar — loopback selalu menang.

    Bug 14 Agu 2026: `soundcard.get_microphone(nama, include_loopback=True)`
    mengembalikan device PERTAMA yang namanya cocok. Di mesin streamer nama
    "Headset (EarPods)" dipakai DUA device sekaligus:

        loopback=True   ch=2   <- loopback speaker, yang kita mau
        loopback=False  ch=1   <- mikrofon headset-nya, yang kepilih

    Merekam loopback dari device mikrofon meledak di dalam soundcard:

        soundcard/mediafoundation.py:516
        assert ppMixFormat[0][0].Format.wFormatTag == 0xFFFE

    AssertionError-nya kosong (bare assert), jadi log cuma menampilkan
    "(AssertionError: )" tanpa petunjuk apa pun. Akibatnya telinga desktop
    Arti MATI TOTAL sepanjang sesi 14 Agu: 5x gagal -> rehat 120 detik ->
    gagal lagi, berulang. Dia tuli terhadap game, video, dan musik.

    Kenapa ini menggigit justru di device default: `desktop_audio_device`
    kosong berarti "ikut speaker default Windows" — dan headset justru jenis
    device yang paling mungkin punya output DAN input bernama sama.

    Mengembalikan None kalau tidak ada yang cocok; pemanggil yang memutuskan.
    """
    if not devices or not want:
        return None
    want_l = str(want).lower()
    persis = [d for d in devices if str(getattr(d, "name", "")) == want]
    bagian = [d for d in devices if want_l in str(getattr(d, "name", "")).lower()]
    for kandidat in (persis, bagian):
        loopback = [d for d in kandidat if getattr(d, "isloopback", False)]
        if loopback:
            return loopback[0]
        if kandidat:
            # Tidak ada loopback bernama itu — kembalikan apa adanya supaya
            # perilaku lama terjaga; pemanggil yang memberi peringatan.
            return kandidat[0]
    return None

# Halusinasi Whisper di atas musik/sunyi dengan bahasa auto-detect: kredit
# subtitler, ajakan subscribe, "bersambung". Live seharian [date removed]:
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

    - `desktop_audio_device` kosong = IKUT default speaker Windows (kalau streamer
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
    - Deadman + kebangkitan: MAX_CAPTURE_FAILS gagal beruntun = telinga
      REHAT `desktop_audio_revival_sec` detik lalu coba bangun SENDIRI —
      bukan mati permanen. Log 2026-08-09 22.31: device kaget sebentar saat
      streamer buka instance Prism, 5 gagal x 5 dtk = cuma 25 detik toleransi,
      lalu Arti budek sepanjang sisa sesi live. Sesudah rehat, percobaan
      bangunnya SEKALI per siklus (gagal = langsung rehat lagi) supaya
      terminal tidak dibanjiri. revival_sec <= 0 = perilaku lama (permanen).
    """
    state: dict = {"rec": None, "close": None, "name": None, "fails": 0,
                   "tidur_sampai": 0.0, "pernah_mati": False}
    sr = DEFAULT_SAMPLERATE
    chunk_sec = float(config.get("desktop_audio_chunk_sec", 5.0))
    configured = (config.get("desktop_audio_device") or "").strip()

    def _open_real(want: str, samplerate: int):
        import warnings  # noqa: PLC0415

        import soundcard as sc  # noqa: PLC0415

        # "data discontinuity in recording" = jeda buffer kecil tiap worker
        # sibuk mentranskrip antar-chunk — jinak untuk konteks 5-detikan,
        # tapi spam-nya membanjiri terminal (live pagi [date removed]).
        warnings.filterwarnings("ignore", message="data discontinuity in recording")
        # JANGAN pakai sc.get_microphone() langsung — dia mengambil nama-cocok
        # PERTAMA dan bisa memberi device mikrofon alih-alih loopback speaker
        # waktu namanya kembar. Lihat pilih_loopback_device().
        mic = pilih_loopback_device(sc.all_microphones(include_loopback=True), want)
        if mic is None:
            mic = sc.get_microphone(want, include_loopback=True)
        if not getattr(mic, "isloopback", False):
            print(
                f"[Desktop Audio] PERINGATAN: '{mic.name}' bukan device loopback — "
                "capture kemungkinan gagal. Isi `desktop_audio_device` dengan "
                "nama speaker yang benar."
            )
        ctx = mic.recorder(samplerate=samplerate, channels=1)
        rec = ctx.__enter__()
        print(f"[Desktop Audio] Capture: {mic.name} (loopback={getattr(mic,'isloopback','?')})")
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
        if state["tidur_sampai"] > time.time():
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
            if state["pernah_mati"]:
                state["pernah_mati"] = False
                print("[Desktop Audio] Telinga BANGUN lagi — capture pulih sendiri")
            return np.asarray(data, dtype="float32").reshape(-1)
        except Exception as e:  # noqa: BLE001
            _close()
            state["fails"] += 1
            # Sesudah pernah mati, satu kegagalan = langsung rehat lagi —
            # tangga 5-percobaan cuma untuk kematian pertama.
            batas = 1 if state["pernah_mati"] else MAX_CAPTURE_FAILS
            if state["fails"] >= batas:
                rehat = float(config.get("desktop_audio_revival_sec", 120.0) or 0.0)
                state["fails"] = 0
                if rehat <= 0:
                    state["tidur_sampai"] = float("inf")
                    print(
                        f"[Desktop Audio] Capture MENYERAH permanen "
                        f"({type(e).__name__}: {e}) — revival dimatikan. "
                        "Cek device / restart bridge."
                    )
                    return None
                pertama = not state["pernah_mati"]
                state["pernah_mati"] = True
                state["tidur_sampai"] = time.time() + rehat
                if pertama:
                    print(
                        f"[Desktop Audio] Capture gagal beruntun "
                        f"({type(e).__name__}: {e}) — telinga rehat "
                        f"{rehat:.0f} dtk lalu coba bangun sendiri."
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
    is_speech: Callable[[object], bool | None] | None = None,
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
            if is_speech is not None and is_speech(audio) is False:
                # VAD bilang bukan ucapan (musik/derau) — nol biaya Whisper.
                # None = VAD tak tersedia -> fail-open, tetap dikirim.
                # (Lagu bervokal tetap lolos — trade yang diterima operator.)
                _vad_skip = getattr(desktop_audio_worker, "_vad_skip", 0) + 1
                desktop_audio_worker._vad_skip = _vad_skip
                if _vad_skip == 1 or _vad_skip % 24 == 0:
                    print(f"[Desktop Audio] VAD skip musik/derau (x{_vad_skip})")
                continue
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
                    # operator lihat apa yang Arti dengar (pengganti spam
                    # "Sukses mentranskrip!" tanpa isi).
                    print(f"[Dengar] {text[:90]}")
        except Exception as e:  # noqa: BLE001
            print(f"[Desktop Audio] Error: {type(e).__name__}: {e}")
            time.sleep(2.0)
