"""Fitur E: Arti mengerti video — worker ingest + reaksi "nonton bareng".

Terukur di spike 2026-08-02:
  T3 Gemini flash-lite makan URL YouTube langsung : 2,6 dtk, NOL bandwidth lokal
  T1 metadata yt-dlp (tanpa download)             : 2,5 dtk
  T1 transkrip json3 (tanpa download)             : 0,6 dtk

Tangga ingest (internet tidak stabil saat playback — concern Bohan):
  T3 Gemini (server-side) -> T1 transkrip (KB-an) -> [T2 frame, HANYA pasca-
  playback & bila dua-duanya gagal] -> metadata saja -> gagal jujur.

Worker thread sendiri; bridge menyuntik hooks (queue reaksi, hold playback,
QA window) supaya modul ini tetap bisa diuji tanpa bridge.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Helper MURNI
# --------------------------------------------------------------------------- #

_YT_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?[^ ]*v=|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


def extract_youtube_ids(text: str) -> list[str]:
    """Semua ID video YouTube unik dari sebuah teks (urutan kemunculan)."""
    seen: list[str] = []
    for m in _YT_URL_RE.finditer(text or ""):
        vid = m.group(1)
        if vid not in seen:
            seen.append(vid)
    return seen


def mmss(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def frame_times(duration_sec: float, n: int = 10) -> list[int]:
    """Titik frame per 10% durasi (ide Bohan): video 59 dtk -> 6,12,18,...

    t_k = round(durasi * k/10); titik 0 dan >= durasi dibuang. Otomatis adil
    untuk durasi berapa pun.
    """
    d = max(1, int(duration_sec))
    out: list[int] = []
    for k in range(1, n + 1):
        t = round(d * k / 10)
        if 0 < t < d and t not in out:
            out.append(t)
    return out


def parse_json3_captions(data: dict) -> list[tuple[float, str]]:
    """Track caption json3 YouTube -> [(detik, teks), ...]. Anti-crash."""
    out: list[tuple[float, str]] = []
    try:
        for ev in (data or {}).get("events") or []:
            segs = ev.get("segs")
            if not segs:
                continue
            text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
            if text:
                out.append((float(ev.get("tStartMs", 0)) / 1000.0, text))
    except Exception:  # noqa: BLE001
        return out
    return out


def compress_transcript(segments: list[tuple[float, str]], max_chars: int = 4000) -> str:
    """Gabung segmen jadi baris '[MM:SS] teks', dipotong adil dari tengah."""
    lines = [f"[{mmss(t)}] {txt}" for t, txt in segments]
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    head = lines[: len(lines) // 3]
    tail = lines[-len(lines) // 3 :]
    return "\n".join(head) + "\n[...]\n" + "\n".join(tail)


def build_timeline_doc(
    video_id: str,
    title: str,
    channel: str,
    duration_sec: float,
    blocks: list[tuple[float, str]],
    *,
    submitted_by: str = "",
    source: str = "",
) -> str:
    """Dokumen vault/watch-parties dengan heading `## [MM:SS]` (format sample)."""
    header = [
        f"# Video: {title}",
        "",
        f"- Channel: {channel}",
        f"- Durasi: {mmss(duration_sec)}",
        f"- Video ID: {video_id}",
    ]
    if submitted_by:
        header.append(f"- Dikirim oleh: {submitted_by} ({source})")
    body = [f"## [{mmss(t)}] {txt.splitlines()[0][:80]}\n\n{txt}" for t, txt in blocks]
    return "\n".join(header) + "\n\n" + "\n\n".join(body) + "\n"


@dataclass
class VideoJob:
    video_id: str
    source: str = "console"      # saweria | streamlabs | chat | console
    viewer: str = ""
    donation_label: str = ""     # "Rp 20.000" bila dari donasi berbayar
    message: str = ""
    clip_start: int = 0
    clip_end: int = 0
    enqueued_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def clip_seconds(self) -> int:
        if self.clip_end > self.clip_start:
            return self.clip_end - self.clip_start
        return 0


def check_submit_allowed(
    job: VideoJob,
    *,
    now: float,
    last_by_viewer: dict[str, float],
    queue_depth: int,
    config: dict,
) -> tuple[bool, str]:
    """Gerbang antrean — pure. Donasi berbayar (saweria) bebas rate limit viewer."""
    if queue_depth >= int(config.get("video_queue_max", 3)):
        return False, "antrean penuh"
    if job.source in ("chat", "streamlabs") and job.viewer:
        gap = float(config.get("video_rate_limit_sec", 300.0))
        last = last_by_viewer.get(job.viewer, 0.0)
        if now - last < gap:
            return False, f"rate limit {job.viewer}"
    return True, ""


def format_reaction_trigger(job: VideoJob, digest: str, title: str) -> str:
    """Trigger reaksi — nada per sumber (keputusan Bohan):
    saweria = donasi berbayar -> terima kasih + reaksi; streamlabs/chat = kasual.
    """
    who = job.viewer or "seseorang"
    if job.source == "saweria" and job.donation_label:
        head = (
            f"[VIDEO SELESAI DITONTON — media share DONASI {job.donation_label} "
            f"dari {who}] Judul: {title}."
        )
        tail = (
            "Ucapkan terima kasih donasinya dengan hangat LALU komentari isi "
            "videonya dengan spesifik."
        )
    else:
        head = f"[VIDEO SELESAI DITONTON — dikirim {who}] Judul: {title}."
        tail = (
            "Komentari isi videonya dengan santai dan spesifik seperti barusan "
            "nonton bareng — tanpa upacara terima kasih."
        )
    # URL dibuang dari pesan — TTS jangan pernah mengeja "https youtu be..."
    clean_msg = _YT_URL_RE.sub("", job.message or "").strip(" -:,")
    if clean_msg:
        head += f" Pesan pengirim: {clean_msg}"
    return (
        f"{head}\nApa yang kamu tonton:\n{digest[:900]}\n{tail} "
        "2-4 kalimat + boleh 1 pertanyaan balik. Jangan sebut sistem/transkrip/AI."
    )


# --------------------------------------------------------------------------- #
# Ingest (jaringan) — tiap tingkat anti-crash, timeout ketat
# --------------------------------------------------------------------------- #

def fetch_metadata(video_id: str, timeout: float = 20.0) -> dict:
    """Judul/durasi/channel + info caption — TANPA download (2,5 dtk terukur)."""
    import yt_dlp  # noqa: PLC0415

    opts = {"quiet": True, "skip_download": True, "no_warnings": True,
            "socket_timeout": timeout}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}",
                              download=False)
    return {
        "title": info.get("title") or video_id,
        "channel": info.get("channel") or info.get("uploader") or "?",
        "duration": float(info.get("duration") or 0),
        "subtitles": info.get("subtitles") or {},
        "automatic_captions": info.get("automatic_captions") or {},
    }


def gemini_video_digest(video_id: str, config: dict, *, clip: tuple[int, int] | None = None) -> str:
    """T3: Gemini menonton URL YouTube di sisi server (2,6 dtk terukur, 0 bandwidth)."""
    import requests  # noqa: PLC0415

    key = (config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY kosong")
    model = str(config.get("video_gemini_model", "gemini-3.1-flash-lite"))
    fokus = (
        f" Fokus ke rentang detik {clip[0]}-{clip[1]}." if clip and clip[1] > clip[0] else ""
    )
    prompt = (
        "Tonton video ini. Tulis dalam Bahasa Indonesia: 2-3 kalimat ringkasan isi, "
        "lalu 3-5 baris momen menarik berformat '[MM:SS] deskripsi singkat'."
        + fokus
    )
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        json={"contents": [{"parts": [
            {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={video_id}"}},
            {"text": prompt},
        ]}]},
        timeout=float(config.get("video_gemini_timeout_sec", 45.0)),
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if not text:
        raise RuntimeError("jawaban kosong")
    return text


def fetch_transcript_text(meta: dict, timeout: float = 15.0) -> str:
    """T1: caption json3 tanpa download (0,6 dtk terukur). '' bila tak ada."""
    import requests  # noqa: PLC0415

    for src in (meta.get("subtitles") or {}, meta.get("automatic_captions") or {}):
        for lang in ("id", "en"):
            for track in src.get(lang) or []:
                if track.get("ext") != "json3":
                    continue
                try:
                    data = requests.get(track["url"], timeout=timeout).json()
                    segs = parse_json3_captions(data)
                    if segs:
                        return compress_transcript(segs)
                except Exception:  # noqa: BLE001
                    continue
    return ""


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

class VideoWatcher(threading.Thread):
    """Thread worker: antrean job -> ingest -> dokumen vault -> reaksi."""

    def __init__(self, config: dict, hooks: dict, *, name: str = "video-watcher"):
        super().__init__(name=name, daemon=True)
        self.config = config
        self.hooks = hooks  # queue_reaction(text, viewer) | reindex() | set_qa(event_id|None)
        self.jobs: "queue.Queue[VideoJob]" = queue.Queue()
        self.last_by_viewer: dict[str, float] = {}
        self.runtime_enabled = True   # veto console: video off/on
        self._skip_current = threading.Event()
        self._stop_event = threading.Event()

    # --- API dipanggil bridge (thread mana pun) ---

    def submit(self, job: VideoJob) -> bool:
        if not self.runtime_enabled:
            print("[Video] runtime OFF (veto) — job diabaikan")
            return False
        ok, why = check_submit_allowed(
            job, now=time.time(), last_by_viewer=self.last_by_viewer,
            queue_depth=self.jobs.qsize(), config=self.config,
        )
        if not ok:
            print(f"[Video] Ditolak: {why} ({job.video_id} dari {job.viewer or job.source})")
            return False
        if job.viewer:
            self.last_by_viewer[job.viewer] = time.time()
        self.jobs.put(job)
        print(f"[Video] Antre: {job.video_id} ({job.source}"
              + (f", {job.viewer}" if job.viewer else "") + ")")
        return True

    def skip_current(self) -> None:
        self._skip_current.set()
        print("[Video] Skip diminta — job berjalan dibatalkan begitu memungkinkan")

    def stop(self) -> None:
        self._stop_event.set()

    # --- loop ---

    def run(self) -> None:
        print("[Video] Watcher aktif — nunggu media share / link / console")
        while not self._stop_event.is_set():
            try:
                job = self.jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            self._skip_current.clear()
            try:
                self._process(job)
            except Exception as e:  # noqa: BLE001
                print(f"[Video] Job {job.video_id} gagal: {type(e).__name__}: {e}")
                self._fail_voice(job)

    # --- tahapan ---

    def _process(self, job: VideoJob) -> None:
        cfg = self.config
        playback_end = 0.0
        if job.source in ("saweria", "streamlabs"):
            hold = job.clip_seconds or float(cfg.get("mediashare_hold_sec", 60.0))
            playback_end = time.time() + hold

        meta = {"title": job.video_id, "channel": "?", "duration": 0.0}
        try:
            meta = fetch_metadata(job.video_id)
        except Exception as e:  # noqa: BLE001
            print(f"[Video] Metadata gagal ({type(e).__name__}) — lanjut seadanya")

        dur = meta.get("duration") or 0
        if job.source in ("chat", "console") and dur > float(
            cfg.get("video_max_duration_sec", 600)
        ):
            print(f"[Video] {job.video_id} kepanjangan ({mmss(dur)}) — ditolak")
            return

        # Tangga ingest: T3 -> T1 -> metadata saja. (T2 frame ditunda sampai ada
        # kebutuhan nyata — Gemini + transkrip dua-duanya harus tumbang dulu,
        # dan bandwidth adalah raja saat playback.)
        digest, tier = "", "metadata"
        clip = (job.clip_start, job.clip_end) if job.clip_seconds else None
        if self._skip_current.is_set():
            return
        try:
            digest, tier = gemini_video_digest(job.video_id, cfg, clip=clip), "gemini"
        except Exception as e:  # noqa: BLE001
            print(f"[Video] Gemini gagal ({type(e).__name__}) — coba transkrip")
            try:
                digest = fetch_transcript_text(meta)
                tier = "transkrip" if digest else "metadata"
            except Exception:  # noqa: BLE001
                digest = ""
        if not digest:
            digest = (
                f"(Hanya info dasar) Judul: {meta['title']}. Channel: {meta['channel']}. "
                f"Durasi: {mmss(dur)}."
            )
        print(f"[Video] Ingest {job.video_id} via {tier} ({len(digest)} char)")

        if self._skip_current.is_set():
            return

        # Dokumen vault (Q&A timecode) — best effort, reaksi tidak bergantung ini.
        try:
            self._write_doc(job, meta, digest)
        except Exception as e:  # noqa: BLE001
            print(f"[Video] Tulis dokumen gagal: {type(e).__name__}: {e}")

        # Reaksi PAS playback selesai (nonton bareng), atau sekarang kalau
        # ingest lebih lambat dari klip.
        wait = max(0.0, playback_end - time.time())
        if wait > 0:
            print(f"[Video] Nunggu playback selesai ({wait:.0f}s) sebelum komentar...")
            if self._stop_event.wait(wait):
                return
        if self._skip_current.is_set():
            return
        trigger = format_reaction_trigger(job, digest, meta["title"])
        self.hooks["queue_reaction"](trigger, job.viewer or None)

        qa_window = float(self.config.get("video_qa_window_sec", 300.0))
        if qa_window > 0 and self.hooks.get("set_qa"):
            self.hooks["set_qa"](f"video-{job.video_id}", qa_window)

    def _write_doc(self, job: VideoJob, meta: dict, digest: str) -> None:
        blocks: list[tuple[float, str]] = []
        for line in digest.splitlines():
            m = re.match(r"\[?(\d{1,2}):(\d{2})\]?\s+(.+)", line.strip())
            if m:
                blocks.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3)))
        if not blocks:
            blocks = [(0.0, digest[:600])]
        doc = build_timeline_doc(
            job.video_id, meta["title"], meta["channel"], meta.get("duration") or 0,
            blocks, submitted_by=job.viewer, source=job.source,
        )
        path = os.path.join("vault", "watch-parties", f"video-{job.video_id}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(doc)
        if self.hooks.get("reindex"):
            self.hooks["reindex"]()

    def _fail_voice(self, job: VideoJob) -> None:
        """Gagal total: donasi berbayar dapat pengakuan jujur; sisanya diam."""
        if job.source == "saweria" and job.donation_label:
            self.hooks["queue_reaction"](
                f"[VIDEO GAGAL DITONTON — media share DONASI {job.donation_label} "
                f"dari {job.viewer}] Videonya gagal kamu tonton (koneksi/video "
                "bermasalah). Minta maaf singkat dengan jujur dan tetap ucapkan "
                "terima kasih donasinya. 2 kalimat.",
                job.viewer or None,
            )
