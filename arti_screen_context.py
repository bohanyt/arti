"""Screen capture context — semantic scene + OCR timecode in RAM (no vault embed)."""

from __future__ import annotations

import collections
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_RING_SIZE = 5

# Penanda log bridge Arti SENDIRI di layar (temuan live 2026-08-02 sore: vision
# membaca terminal bridge -> Arti narasi dapurnya sendiri: "cursor screen
# relevant False", "aku membaca 50 pesan sejarah"). Hanya token yang TIDAK
# mungkin muncul di konten sah (Bohan ngoding ARTI di layar itu konten sah —
# jangan blokir kata umum seperti "transkripsi" / nama file arti_*.py).
_BRIDGE_LOG_MARKERS = (
    "[scouter]",
    "[vision]",
    "[vault rag]",
    "[groq api]",
    "[queue]",
    "[initiative]",
    "[watch party]",
    "[history recorded]",
    "history recorded",
    "arti menjawab",
    "screen_relevant",
    "screen relevant",
    "curious_worthy",
    "curious worthy",
    "pesan sejarah stream",
    "vad_tail",
    "uptime ok=",
    "mengirim ke groq",
    # Echo prompt vision sendiri (live 2026-08-03 pagi: google_gemma
    # mengembalikan prompt-nya dan fallback parse menyimpannya sebagai scene).
    "vtuber ai companion",
    "analyze a screenshot",
)


def looks_like_bridge_log(text: str) -> bool:
    """Teks mengutip log internal bridge? (backstage — bukan bahan omongan)."""
    t = (text or "").lower()
    if not t:
        return False
    return any(m in t for m in _BRIDGE_LOG_MARKERS)


@dataclass
class ScreenSnapshot:
    wall_ts: float
    scene: str
    playback_mmss: str | None = None
    ocr_text: str = ""
    # Sudut obrolan dari model vision — opsional & aditif. Model lama / provider yang
    # tidak mengisi field ini tetap jalan: hook kosong = perilaku persis seperti dulu.
    hook: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_ts": self.wall_ts,
            "scene": self.scene,
            "playback_mmss": self.playback_mmss,
            "ocr_text": self.ocr_text,
            "hook": self.hook,
        }


class ScreenRing:
    """RAM-only ring of recent screen snapshots."""

    def __init__(self, max_size: int = DEFAULT_RING_SIZE):
        self._max = max(1, int(max_size))
        self._items: collections.deque[ScreenSnapshot] = collections.deque(maxlen=self._max)
        self._lock = threading.Lock()

    def push(self, snapshot: ScreenSnapshot) -> None:
        with self._lock:
            self._items.append(snapshot)

    def snapshot(self) -> list[ScreenSnapshot]:
        with self._lock:
            return list(self._items)

    def latest(self) -> ScreenSnapshot | None:
        with self._lock:
            return self._items[-1] if self._items else None


screen_ring = ScreenRing()


@dataclass
class WatchState:
    event_id: str = ""
    playback_mmss: str | None = None
    scene_ring: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "playback_mmss": self.playback_mmss,
            "scene_ring": self.scene_ring,
            "updated_at": self.updated_at,
        }


watch_state = WatchState()


def build_vision_prompt() -> str:
    """Structured prompt for one-call scene + hook + timecode OCR (Vision Prompt Contract).

    `scene` masuk ke prompt Arti sebagai `[LAYAR: ...]` — itulah SATU-SATUNYA bahan
    yang dia punya tentang layar. Kontrak lama meminta "jenis layar + elemen utama",
    dan hasilnya persis seperti yang dikeluhkan: "ada VSCode kebuka, ada Chrome".
    Arti tidak bisa bikin komentar menarik dari bahan yang tidak menarik.

    Kontrak baru menuntut KEKHUSUSAN dan menambah `hook` (sudut obrolan). Vision tidak
    menulis kalimat Arti — itu tugas LLM suara — vision cuma menyediakan bahan yang layak
    diomongkan.
    """
    return (
        "Kamu mata seorang VTuber AI yang lagi nemenin streamer live. "
        "Analisis screenshot layar dia. Balas HANYA satu objek JSON valid "
        "(tanpa markdown, tanpa ```).\n\n"
        "Keys wajib:\n"
        '  "scene": string — 1-2 kalimat Bahasa Indonesia. Sebut hal SPESIFIK yang '
        "sedang dikerjakan, bukan aplikasi yang terbuka. Baca nama file, judul, nama "
        "karakter, angka, error, atau apa pun yang menunjukkan kamu benar-benar "
        "memperhatikan.\n"
        '  "hook": string ATAU null — satu sudut obrolan: hal paling menarik, aneh, '
        "atau bikin penasaran di layar yang pantas ditanyakan. Bukan pertanyaan basa-basi. "
        "null kalau layar benar-benar tidak ada yang menarik.\n"
        '  "playback_mmss": string "mm:ss" ATAU null JSON — HANYA jika bilah kontrol video '
        "YouTube/player terlihat jelas (tombol play + elapsed kiri bawah). "
        'Jika tidak ada player: null (bukan string "null", bukan "N/A").\n'
        '  "ocr_text": string — MAKS 200 karakter. Hanya: judul video, timecode on-screen, '
        "subtitle 1 baris, atau label UI penting. JANGAN salin full log terminal, "
        "JANGAN dump chat history, JANGAN ulang isi scene.\n\n"
        "DILARANG di scene dan hook — ini yang bikin komentar terasa garing:\n"
        '- Menyebut nama aplikasi sebagai isi utama ("ada VSCode kebuka", "browser Chrome terbuka").\n'
        '- Frasa kosong: "streamer sedang", "layar menampilkan", "kayaknya lagi", "sedang melihat".\n'
        "- Menghitung jumlah tab atau jendela.\n"
        "- Mendeskripsikan tata letak UI (sidebar, toolbar, posisi panel).\n"
        "- Jendela log sistem VTuber-nya SENDIRI: terminal berisi baris tag "
        "[Scouter]/[Vision]/[Groq API]/[Vault RAG], 'Arti menjawab', "
        "screen_relevant=True/False, dsb. Itu BACKSTAGE — jangan deskripsikan "
        "isinya, jangan jadikan hook. Kalau jendela itu yang dominan, "
        "deskripsikan jendela lain; kalau tidak ada, hook null.\n\n"
        "Yang BAGUS — spesifik dan bisa ditanggapi:\n"
        '- game: nama game, area/level, HP kritis, boss, item langka, skor, kematian beruntun.\n'
        "- coding: nama fungsi/file, pesan error persis, jumlah test gagal, TODO yang nongol.\n"
        "- tulisan/dokumen: judul, nama bab, kalimat yang lagi digarap.\n"
        "- video: judul, siapa yang di layar, apa yang barusan terjadi.\n"
        "- pixel art / gambar: subjek yang digambar, palet warna, bagian yang lagi dikerjain.\n\n"
        "Aturan:\n"
        "- Kalau tidak yakin membaca sesuatu, JANGAN mengarang — lebih baik sebut yang kamu yakin.\n"
        "- Jangan tebak plot atau isi video dari teks kecil.\n"
        "- Jika layar penuh teks (terminal/log): scene sebut error atau perintah yang paling "
        "menonjol, ocr_text ambil 1-2 baris teratas yang relevan saja.\n"
        "- Output total singkat; model tidak perlu reasoning panjang."
    )


_NULL_MMSS = frozenset({"", "null", "none", "n/a", "na"})


def _normalize_playback_mmss(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _NULL_MMSS:
        return None
    import re

    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        return s
    return None


def parse_vision_response(
    raw: str,
    *,
    scene_max_chars: int = 300,
    ocr_max_chars: int = 200,
) -> ScreenSnapshot:
    """Parse model JSON + saring backstage (log bridge sendiri = bukan bahan).

    Satu field saja yang mengutip log bridge -> seluruh snapshot teks
    dikosongkan: kalau vision lagi menatap terminal Arti, "konteks layar"
    turn itu memang tidak ada — lebih baik diam daripada narasi dapur.
    """
    snap = _parse_vision_response_raw(
        raw, scene_max_chars=scene_max_chars, ocr_max_chars=ocr_max_chars
    )
    if (
        looks_like_bridge_log(snap.scene)
        or looks_like_bridge_log(snap.hook)
        or looks_like_bridge_log(snap.ocr_text)
    ):
        snap.scene = ""
        snap.hook = ""
        snap.ocr_text = ""
    return snap


def _parse_vision_response_raw(
    raw: str,
    *,
    scene_max_chars: int = 300,
    ocr_max_chars: int = 200,
) -> ScreenSnapshot:
    """Parse model JSON (best-effort) with contract normalization."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # JSON rusak — paling sering karena KEPOTONG max_tokens di tengah string.
        # Terpantau di sesi live 2026-08-01: fallback lama menyimpan JSON mentah
        # sebagai scene, yang lalu tersuntik ke prompt Arti sebagai
        # [LAYAR: { "scene": "..."]. Selamatkan nilai field-nya lewat regex dulu;
        # pola `[^"]*` sengaja tanpa quote penutup supaya string yang terpotong
        # pun tetap terambil.
        import re as _re

        m_scene = _re.search(r'"scene"\s*:\s*"([^"]*)', text)
        if m_scene and m_scene.group(1).strip():
            m_hook = _re.search(r'"hook"\s*:\s*"([^"]*)', text)
            m_ocr = _re.search(r'"ocr_text"\s*:\s*"([^"]*)', text)
            m_mmss = _re.search(r'"playback_mmss"\s*:\s*"([^"]*)"', text)
            return ScreenSnapshot(
                wall_ts=time.time(),
                scene=m_scene.group(1).strip()[:scene_max_chars],
                playback_mmss=_normalize_playback_mmss(
                    m_mmss.group(1) if m_mmss else None
                ),
                ocr_text=(m_ocr.group(1).strip() if m_ocr else "")[:ocr_max_chars],
                hook=(m_hook.group(1).strip() if m_hook else "")[:scene_max_chars],
            )
        return ScreenSnapshot(
            wall_ts=time.time(),
            scene=text[:200],
            playback_mmss=None,
            ocr_text="",
        )
    hook_raw = data.get("hook")
    hook = "" if hook_raw is None else str(hook_raw).strip()
    if hook.lower() in _NULL_MMSS:
        hook = ""
    return ScreenSnapshot(
        wall_ts=time.time(),
        scene=str(data.get("scene", "")).strip()[:scene_max_chars],
        playback_mmss=_normalize_playback_mmss(data.get("playback_mmss")),
        ocr_text=str(data.get("ocr_text", "")).strip()[:ocr_max_chars],
        hook=hook[:scene_max_chars],
    )


def format_screen_context(ring: ScreenRing | None = None, max_chars: int = 200) -> str:
    """Baris `[LAYAR: ...]` untuk prompt Arti.

    Kalau model vision mengisi `hook`, ikutkan — itulah bahan yang bikin Arti punya
    sesuatu untuk ditanyakan, bukan cuma sesuatu untuk dilaporkan. `max_chars` berlaku
    untuk scene supaya perilaku lama tidak berubah saat hook kosong.
    """
    latest = (ring or screen_ring).latest()
    if not latest or not latest.scene:
        return ""
    line = latest.scene[:max_chars]
    if latest.hook:
        line += f" | menarik: {latest.hook[:max_chars]}"
    return line


def update_watch_state_from_snapshot(
    snap: ScreenSnapshot,
    *,
    event_id: str = "",
    ring: ScreenRing | None = None,
    state: WatchState | None = None,
) -> None:
    target_ring = ring or screen_ring
    target_state = state or watch_state
    target_ring.push(snap)
    target_state.playback_mmss = snap.playback_mmss
    if event_id:
        target_state.event_id = event_id
    target_state.scene_ring = [s.to_dict() for s in target_ring.snapshot()]
    target_state.updated_at = snap.wall_ts


def screen_watcher_worker(
    config: dict,
    *,
    capture_and_describe: Any | None = None,
    sleep_sec: float | None = None,
) -> None:
    """Background thread; calls vision provider when capture_fn wired."""
    enabled = config.get("vision_enabled", config.get("screen_context_enabled", False))
    if not enabled:
        return
    if not config.get("vision_background_poll", False):
        return
    if not config.get("vision_runtime_on", False):
        return
    interval = float(
        sleep_sec
        if sleep_sec is not None
        else config.get("vision_refresh_sec", config.get("screen_context_interval_sec", 10.0))
    )
    max_chars = int(config.get("vision_scene_max_chars", config.get("screen_context_max_chars", 200)))
    print(f"[Screen] Watcher started (interval={interval}s)")
    while (
        config.get("vision_enabled", config.get("screen_context_enabled", False))
        and config.get("vision_background_poll", False)
        and config.get("vision_runtime_on", False)
    ):
        if capture_and_describe is None:
            time.sleep(interval)
            continue
        try:
            snap, provider = capture_and_describe()
            if snap and snap.scene:
                if len(snap.scene) > max_chars:
                    snap.scene = snap.scene[:max_chars]
                update_watch_state_from_snapshot(
                    snap,
                    event_id=str(config.get("watch_party_event_id") or ""),
                )
                print(
                    f"[Screen] vision_provider={provider} scene={snap.scene[:60]!r} "
                    f"playback={snap.playback_mmss} ocr_len={len(snap.ocr_text)}"
                )
        except Exception as e:
            print(f"[Screen] Error: {type(e).__name__}: {e}")
        time.sleep(interval)
