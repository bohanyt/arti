"""Listener donasi realtime — D1: Saweria via WebSocket (tanpa URL publik).

Protokol (diverifikasi dari source wrapper Node SuspiciousLookingOwl/saweria-api,
endpoints.ts + Client.ts + interfaces.ts, 2026-08-02):

  wss://events.saweria.co/stream?streamKey=<32-char>
  pesan  : {"type": "donation", "data": [ {...}, ... ]}   (data = LIST)
  normal : {amount, donator, message, tts, sound}
  media  : {amount, donator, message, media: {id, start, end, type}}
           -> media.id = ID VIDEO YOUTUBE + detik start/end (jembatan Fitur E)

Listener jalan di thread daemon dengan reconnect backoff. Event dinormalisasi
ke DonationEvent lalu diserahkan ke callback bridge (yang menunda reaksi
sampai alert overlay selesai — schedule_donation_trigger).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

SAWERIA_WS_URL = "wss://events.saweria.co/stream"

# Media share yang belum ditonton — Fitur E akan mengonsumsi ini.
pending_media: deque = deque(maxlen=10)


@dataclass
class DonationEvent:
    platform: str
    name: str
    amount: float
    message: str = ""
    media_url: str = ""
    media_start: int = 0
    media_end: int = 0
    amount_text: str = ""  # label siap pakai dari platform (mis. "$5.00")
    kind: str = "donation"  # donation | media_points (loyalty points, kasual)
    raw: dict = field(default_factory=dict)

    @property
    def amount_label(self) -> str:
        """Label nominal: pakai punya platform kalau ada; Saweria selalu IDR."""
        if self.amount_text:
            return self.amount_text
        try:
            return "Rp " + f"{int(self.amount):,}".replace(",", ".")
        except (TypeError, ValueError):
            return f"Rp {self.amount}"

    @property
    def platform_label(self) -> str:
        return {"saweria": "Saweria", "streamlabs": "Streamlabs"}.get(
            self.platform, self.platform.title()
        )


def parse_saweria_event(raw) -> list[DonationEvent]:
    """Normalisasi satu pesan WS Saweria -> list DonationEvent. Pure, anti-crash."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
        if not isinstance(data, dict) or data.get("type") != "donation":
            return []
        out: list[DonationEvent] = []
        for d in data.get("data") or []:
            if not isinstance(d, dict):
                continue
            try:
                amount = float(d.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            ev = DonationEvent(
                platform="saweria",
                name=str(d.get("donator") or "Seseorang").strip() or "Seseorang",
                amount=amount,
                message=str(d.get("message") or "").strip(),
                raw=d,
            )
            media = d.get("media")
            # Media share: dict dengan id video YouTube. (Donasi normal bisa punya
            # media.src berisi gif — itu BUKAN video share, tidak ada 'id'.)
            if isinstance(media, dict) and media.get("id"):
                ev.media_url = f"https://youtu.be/{media['id']}"
                try:
                    ev.media_start = int(media.get("start") or 0)
                    ev.media_end = int(media.get("end") or 0)
                except (TypeError, ValueError):
                    pass
            out.append(ev)
        return out
    except Exception:  # noqa: BLE001 — payload aneh tidak boleh merobohkan listener
        return []


def format_donation_trigger(ev: DonationEvent) -> str:
    """Teks trigger untuk LLM — konsisten dengan wrapper Super Chat (D0)."""
    msg = ev.message or "(tanpa pesan — cukup nominal)"
    extra = ""
    if ev.media_url:
        extra = (
            " (dia juga nge-share video — bilang makasih videonya dan kamu "
            "simpan buat ditonton)"
        )
    return f"[DONASI {ev.platform_label} {ev.amount_label} dari {ev.name}]: {msg}{extra}"


# --------------------------------------------------------------------------- #
# D2: Streamlabs — Socket.IO resmi (sockets.streamlabs.com?token=X)
# --------------------------------------------------------------------------- #

STREAMLABS_SOCKET_URL = "https://sockets.streamlabs.com"


def parse_streamlabs_event(payload) -> list[DonationEvent]:
    """Normalisasi satu event Socket.IO Streamlabs -> list DonationEvent.

    HANYA type 'donation' (tip Streamlabs) yang diproses. Super Chat/membership
    YouTube yang di-relay Streamlabs SENGAJA DIABAIKAN — D0 sudah menangkapnya
    langsung dari innertube; tanpa filter ini Arti terima kasih DUA KALI untuk
    donasi yang sama.
    """
    try:
        if not isinstance(payload, dict) or payload.get("type") != "donation":
            return []
        out: list[DonationEvent] = []
        for d in payload.get("message") or []:
            if not isinstance(d, dict):
                continue
            try:
                amount = float(d.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            out.append(DonationEvent(
                platform="streamlabs",
                name=str(d.get("name") or d.get("from") or "Seseorang").strip()
                or "Seseorang",
                amount=amount,
                message=str(d.get("message") or "").strip(),
                amount_text=str(d.get("formatted_amount") or "").strip(),
                raw=d,
            ))
        return out
    except Exception:  # noqa: BLE001
        return []


_MEDIASHARE_TYPE_CANDIDATES = ("mediashareevent", "mediashare", "media_share",
                               "media-share", "mediashareplay", "media")

# Dedupe request media share: event "play" nembak lagi saat resume/seek —
# media.id (id request Streamlabs, unik per share) yang sudah diproses di-skip.
_sl_media_req_seen: deque = deque(maxlen=8)
_YT_ID_IN_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?[^\"'\s]*v=|youtu\.be/)([A-Za-z0-9_-]{11})"
)


_ID_KEYS = {"id", "videoid", "video_id", "media_id", "mediaid"}


def _find_youtube_ref(obj, depth: int = 0, key: str = ""):
    """Cari ID/URL YouTube di payload sembarang bentuk (defensif, rekursif).

    Temuan audit: string 11-char SEMBARANG (nama viewer "medialover1"!) dulu
    dianggap ID video -> job hantu + hold 68 dtk. ID telanjang kini hanya
    diterima bila KEY-nya jelas id-ish; selain itu wajib URL YouTube penuh.
    """
    if depth > 6 or obj is None:
        return ""
    if isinstance(obj, str):
        m = _YT_ID_IN_URL_RE.search(obj)
        if m:
            return m.group(1)
        if key.lower() in _ID_KEYS and re.fullmatch(r"[A-Za-z0-9_-]{11}", obj.strip()):
            return obj.strip()
        return ""
    if isinstance(obj, dict):
        for k in ("media", "url", "video", "id", "videoId", "link", "href"):
            if k in obj:
                r = _find_youtube_ref(obj[k], depth + 1, key=k)
                if r:
                    return r
        for k, v in obj.items():
            r = _find_youtube_ref(v, depth + 1, key=str(k))
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_youtube_ref(v, depth + 1, key=key)
            if r:
                return r
    return ""


def _parse_sl_mediashare_locked(msg: dict) -> DonationEvent | None:
    """Bentuk PASTI type "mediaShareEvent" — dikunci dari sampel asli live
    2026-08-02 (data/streamlabs_events_sample.jsonl, share @bohanyt):

      message.event : "newMedia" (baru antre) / "play" (MULAI DIPUTAR di stream)
      message.media : {media: "<ID YouTube 11 char>", media_title, duration(MS!),
                       start_time, requester_name "@handle", id: <id request>}
      "play" tanpa media (media: null, modMoveToNext) = kontrol player — skip.

    Reaksi hanya di "play" pertama per id request: itulah momen klip tampil di
    layar (pas untuk playback hold); resume/seek nembak "play" lagi -> dedupe.
    """
    event = str(msg.get("event") or "").strip().lower()
    media = msg.get("media")
    if not isinstance(media, dict):
        return None
    vid = str(media.get("media") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        vid = _find_youtube_ref(media)
        if not vid:
            return None
    title = str(media.get("media_title") or "").strip()
    if event == "newmedia":
        print(f"[Donasi] Streamlabs media antre: {title or vid} — nunggu play")
        return None
    if event != "play":
        return None
    req_id = media.get("id")
    if req_id is not None:
        if req_id in _sl_media_req_seen:
            return None  # resume/seek dari share yang sama
        _sl_media_req_seen.append(req_id)
    try:
        dur = float(media.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur > 600:  # sampel asli: MILIDETIK (9000 utk video 9 dtk / PT9S)
        dur = dur / 1000.0
    try:
        start = float(media.get("start_time") or 0)
    except (TypeError, ValueError):
        start = 0.0
    if start > 600:
        start = start / 1000.0
    name = str(media.get("requester_name") or media.get("action_by")
               or "Seseorang").strip() or "Seseorang"
    return DonationEvent(
        platform="streamlabs",
        name=name,
        amount=0.0,
        message=title,
        media_url=f"https://youtu.be/{vid}",
        media_start=int(start),
        media_end=int(start + dur) if dur > 0 else 0,
        kind="media_points",
        raw=media,
    )


def parse_streamlabs_mediashare(payload) -> list[DonationEvent]:
    """Parser media share Streamlabs (loyalty points, cap 59 dtk Bohan).

    Type "mediaShareEvent" = bentuk PASTI (terkunci dari sampel asli, lihat
    _parse_sl_mediashare_locked). Nama type lain tetap lewat jalur defensif
    lama — jaga-jaga varian dashboard/versi.
    """
    try:
        if not isinstance(payload, dict):
            return []
        typ = str(payload.get("type") or "").lower().replace(" ", "")
        if typ not in _MEDIASHARE_TYPE_CANDIDATES:
            return []
        if typ == "mediashareevent":
            msg = payload.get("message")
            if not isinstance(msg, dict):
                return []
            ev = _parse_sl_mediashare_locked(msg)
            return [ev] if ev else []
        msgs = payload.get("message")
        items = msgs if isinstance(msgs, list) else [msgs if isinstance(msgs, dict) else payload]
        out: list[DonationEvent] = []
        for d in items:
            if not isinstance(d, dict):
                continue
            vid = _find_youtube_ref(d)
            if not vid:
                continue
            out.append(DonationEvent(
                platform="streamlabs",
                name=str(d.get("name") or d.get("from") or "Seseorang").strip()
                or "Seseorang",
                amount=0.0,
                message=str(d.get("message") or d.get("media_title") or "").strip(),
                media_url=f"https://youtu.be/{vid}",
                kind="media_points",
                raw=d,
            ))
        return out
    except Exception:  # noqa: BLE001
        return []


_KNOWN_SL_TYPES = {"donation"} | set(_MEDIASHARE_TYPE_CANDIDATES)
_SL_SAMPLE_PATH = os.path.join("data", "streamlabs_events_sample.jsonl")


def record_unknown_streamlabs(payload) -> None:
    """Foto payload tipe tak dikenal — sampel asli mengunci parser nantinya."""
    try:
        typ = str((payload or {}).get("type") or "?") if isinstance(payload, dict) else "?"
        if typ.lower().replace(" ", "") in _KNOWN_SL_TYPES:
            return
        os.makedirs(os.path.dirname(_SL_SAMPLE_PATH), exist_ok=True)
        with open(_SL_SAMPLE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str)[:4000] + "\n")
        print(f"[Donasi] Event Streamlabs tak dikenal '{typ}' terekam ke sampel")
    except Exception:  # noqa: BLE001
        pass


def resolve_streamlabs_token(config: dict | None = None) -> str:
    cfg = config or {}
    return (
        cfg.get("streamlabs_socket_token")
        or os.environ.get("STREAMLABS_SOCKET_TOKEN")
        or ""
    ).strip()


class StreamlabsListener(threading.Thread):
    """Thread daemon Socket.IO — reconnect bawaan python-socketio."""

    def __init__(self, token: str, on_event, *, name: str = "streamlabs-listener"):
        super().__init__(name=name, daemon=True)
        self.token = token
        self.on_event = on_event
        self._sio = None
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        if self._sio is not None:
            try:
                self._sio.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> None:  # pragma: no cover — jaringan; parser murni diuji terpisah
        import socketio  # noqa: PLC0415

        sio = socketio.Client(reconnection=True, reconnection_delay=5,
                              reconnection_delay_max=60)
        self._sio = sio

        @sio.event
        def connect():  # noqa: ANN202
            print("[Donasi] Streamlabs tersambung — nunggu donasi masuk...")

        @sio.on("event")
        def on_sl_event(data):  # noqa: ANN001, ANN202
            events = parse_streamlabs_event(data) or parse_streamlabs_mediashare(data)
            if not events:
                record_unknown_streamlabs(data)
                return
            for ev in events:
                try:
                    self.on_event(ev)
                except Exception as e:  # noqa: BLE001
                    print(f"[Donasi] Callback error: {type(e).__name__}: {e}")

        try:
            sio.connect(f"{STREAMLABS_SOCKET_URL}?token={self.token}",
                        transports=["websocket"])
            sio.wait()
        except Exception as e:  # noqa: BLE001
            print(f"[Donasi] Streamlabs gagal connect: {type(e).__name__}: {e}")
        print("[Donasi] Listener Streamlabs berhenti.")


def resolve_saweria_key(config: dict | None = None) -> str:
    cfg = config or {}
    return (cfg.get("saweria_stream_key") or os.environ.get("SAWERIA_STREAM_KEY") or "").strip()


class SaweriaListener(threading.Thread):
    """Thread daemon: connect -> terima event -> callback; reconnect backoff."""

    def __init__(self, stream_key: str, on_event, *, name: str = "saweria-listener"):
        super().__init__(name=name, daemon=True)
        self.stream_key = stream_key
        self.on_event = on_event
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover — jaringan; logika murni diuji terpisah
        from websockets.sync.client import connect  # noqa: PLC0415

        backoff = 5.0
        url = f"{SAWERIA_WS_URL}?streamKey={self.stream_key}"
        while not self._stop_event.is_set():
            try:
                with connect(url, open_timeout=15) as ws:
                    print("[Donasi] Saweria tersambung — nunggu donasi masuk...")
                    backoff = 5.0
                    while not self._stop_event.is_set():
                        try:
                            raw = ws.recv(timeout=30)
                        except TimeoutError:
                            continue  # idle — cek stop flag lalu dengar lagi
                        for ev in parse_saweria_event(raw):
                            try:
                                self.on_event(ev)
                            except Exception as e:  # noqa: BLE001
                                print(f"[Donasi] Callback error: {type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                print(
                    f"[Donasi] Saweria putus ({type(e).__name__}) — "
                    f"reconnect dalam {backoff:.0f}s"
                )
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
        print("[Donasi] Listener Saweria berhenti.")


def start_donation_listeners(config: dict, on_event) -> list[threading.Thread]:
    """Nyalakan semua listener platform yang kredensialnya tersedia."""
    if not config.get("donation_enabled", False):
        return []
    started: list[threading.Thread] = []
    key = resolve_saweria_key(config)
    if key:
        t = SaweriaListener(key, on_event)
        t.start()
        started.append(t)
        print(f"[Donasi] Listener Saweria dinyalakan (key {len(key)} char).")
    else:
        print("[Donasi] SAWERIA_STREAM_KEY kosong — listener Saweria dilewati.")

    token = resolve_streamlabs_token(config)
    if token:
        try:
            import socketio  # noqa: PLC0415, F401 — cek dependensi sebelum start

            t2 = StreamlabsListener(token, on_event)
            t2.start()
            started.append(t2)
            print(f"[Donasi] Listener Streamlabs dinyalakan (token {len(token)} char).")
        except ImportError:
            print(
                "[Donasi] python-socketio belum terpasang — "
                "pip install -r requirements-donations.txt"
            )
    else:
        print("[Donasi] STREAMLABS_SOCKET_TOKEN kosong — listener Streamlabs dilewati.")
    return started
