"""Listener donasi realtime — Saweria / Streamlabs / Trakteer via WebSocket.

Saweria (diverifikasi dari source wrapper Node saweria-api,
endpoints.ts + Client.ts + interfaces.ts, 2026-08-02):

  wss://events.saweria.co/stream?streamKey=<32-char>
  pesan  : {"type": "donation", "data": [ {...}, ... ]}   (data = LIST)
  normal : {amount, donator, message, tts, sound}
  media  : {amount, donator, message, media: {id, start, end, type}}
           -> media.id = ID VIDEO YOUTUBE + detik start/end (jembatan Fitur E)

Trakteer (THIRD-PARTY, unofficial overlay Pusher — no public developer API):
  trakteerdonate src/trakteerdonate/client.py:96 URL, :129-144 subscribe,
  :177-182 tip event; data_types.py:29-37 fields. trakteer-websocket-wrapper
  src/Client/client.ts:26 URL, :40-55 handshake, :75-77 BroadcastNotificationCreated;
  index.d.ts:12-23 Donation. A public gist media.video {type,id,start}.

  wss://socket.trakteer.id/app/2ae25d102cc6cd41100a?protocol=7&client=js&version=5.1.1&flash=false
  subscribe: {"event":"pusher:subscribe","data":{"auth":"",
              "channel":"creator-stream.{hash}.trstream-{key}"}}
  tip: event contains BroadcastNotificationCreated; data JSON string with
       supporter_name, quantity, unit, price, supporter_message, media

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
# THIRD-PARTY unofficial overlay (trakteerdonate client.py:96).
TRAKTEER_WS_URL = (
    "wss://socket.trakteer.id/app/2ae25d102cc6cd41100a"
    "?protocol=7&client=js&version=5.1.1&flash=false"
)

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
    kind: str = "donation"  # donation | media_points | membership | follow
    membership_months: int = 0
    membership_level: str = ""
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
        return {
            "saweria": "Saweria",
            "streamlabs": "Streamlabs",
            "trakteer": "Trakteer",
            "youtube": "YouTube",
        }.get(
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


_TRAKTEER_TIP_KEYS = (
    "supporter_name", "supporter_message", "quantity", "unit", "price", "media",
)
_TRAKTEER_YT_TYPES = {"", "youtube", "yt", "youtu.be", "youtubevideo"}


def _parse_trakteer_idr(value) -> float | None:
    """Parse Trakteer `price` (e.g. 'Rp 5.000') to IDR. None if not derivable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    s = re.sub(r"(?i)rp\.?|idr", "", s).strip()
    s = s.replace("\xa0", "").replace(" ", "")
    s = s.replace(",-", "").replace(".-", "")
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", s):
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _trakteer_unit_text(d: dict) -> str:
    unit = str(d.get("unit") or "").strip()
    qty = d.get("quantity")
    qty_s = "" if qty is None else str(qty).strip()
    if qty_s and unit:
        return f"{qty_s} {unit}"
    return unit or (f"{qty_s} unit" if qty_s else "")


def _trakteer_media(media) -> tuple[str, int, int]:
    """YouTube clip URL + start/end if the unofficial media.video fields exist.

    A public gist: media.video {type, id, start}; type empty = YouTube, 'tiktok'
    otherwise. trakteerdonate data_types.py:83-84 types media as Optional(str). GIF/voice
    only is not a clip (same rule as Saweria media.src). TikTok is left out —
    the existing watcher only accepts YouTube URLs.
    """
    if isinstance(media, str):
        m = re.search(
            r"(?:youtube\.com/watch\?[^\"'\s]*v=|youtu\.be/)([A-Za-z0-9_-]{11})",
            media,
        )
        return (f"https://youtu.be/{m.group(1)}", 0, 0) if m else ("", 0, 0)
    if not isinstance(media, dict):
        return "", 0, 0
    video = media.get("video")
    if isinstance(video, str):
        return _trakteer_media(video)
    if not isinstance(video, dict):
        return "", 0, 0
    typ = str(video.get("type") or "").strip().lower()
    vid = str(video.get("id") or "").strip()
    if not vid or typ not in _TRAKTEER_YT_TYPES:
        return "", 0, 0
    m = re.search(
        r"(?:youtube\.com/watch\?[^\"'\s]*v=|youtu\.be/)([A-Za-z0-9_-]{11})",
        vid,
    )
    url = f"https://youtu.be/{m.group(1) if m else vid}"
    try:
        start = int(video.get("start") or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        end = int(video.get("end") or 0)
    except (TypeError, ValueError):
        end = 0
    return url, start, end


def _trakteer_payload_dict(raw):
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    if isinstance(event, str):
        if "BroadcastNotificationCreated" in event:
            inner = data.get("data")
            if isinstance(inner, (str, bytes, bytearray)):
                try:
                    inner = json.loads(inner)
                except Exception:  # noqa: BLE001
                    return None
            return inner if isinstance(inner, dict) else None
        return None
    if any(k in data for k in _TRAKTEER_TIP_KEYS):
        return data
    inner = data.get("data")
    if isinstance(inner, (str, bytes, bytearray)):
        try:
            inner = json.loads(inner)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(inner, dict) and any(k in inner for k in _TRAKTEER_TIP_KEYS):
        return inner
    return None


def parse_trakteer_event(raw) -> list[DonationEvent]:
    """Normalisasi satu pesan overlay Trakteer -> list DonationEvent. Pure, anti-crash.

    THIRD-PARTY unofficial protocol (see module docstring). Never raises.
    """
    try:
        d = _trakteer_payload_dict(raw)
        if not isinstance(d, dict):
            return []
        if not any(k in d for k in _TRAKTEER_TIP_KEYS):
            return []
        name = str(d.get("supporter_name") or "Seseorang").strip() or "Seseorang"
        msg = str(d.get("supporter_message") or "").strip()
        price_raw = d.get("price")
        parsed = _parse_trakteer_idr(price_raw)
        amount_text = (
            str(price_raw).strip() if isinstance(price_raw, str) and str(price_raw).strip() else ""
        )
        if parsed is None:
            amount = 0.0
            amount_text = ""
            unit_text = _trakteer_unit_text(d)
            if unit_text:
                msg = f"{msg} ({unit_text})".strip() if msg else unit_text
        else:
            amount = parsed
        ev = DonationEvent(
            platform="trakteer",
            name=name,
            amount=amount,
            message=msg,
            amount_text=amount_text,
            kind="donation",
            raw=d,
        )
        media_url, media_start, media_end = _trakteer_media(d.get("media"))
        if media_url:
            ev.media_url = media_url
            ev.media_start = media_start
            ev.media_end = media_end
        return [ev]
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


def format_membership_trigger(ev: DonationEvent) -> str:
    """Teks trigger membership tanpa menyebut nominal donasi palsu Rp 0."""
    detail = []
    if ev.membership_level:
        detail.append(f"level {ev.membership_level}")
    if ev.membership_months > 0:
        detail.append(f"{ev.membership_months} bulan")
    suffix = f" — {', '.join(detail)}" if detail else ""
    aksi = (
        "melanjutkan membership"
        if ev.membership_months > 1
        else "bergabung sebagai member"
    )
    return (
        f"[MEMBERSHIP {ev.platform_label} dari {ev.name}{suffix}]: "
        f"{ev.message or f'ucapkan terima kasih karena dia {aksi}'}"
    )


# --------------------------------------------------------------------------- #
# D2: Streamlabs — Socket.IO resmi (sockets.streamlabs.com?token=X)
# --------------------------------------------------------------------------- #

STREAMLABS_SOCKET_URL = "https://sockets.streamlabs.com"


def parse_streamlabs_event(payload) -> list[DonationEvent]:
    """Normalisasi satu event Socket.IO Streamlabs -> list DonationEvent.

    HANYA type 'donation' (tip Streamlabs) yang diproses. Super Chat yang
    di-relay Streamlabs diabaikan karena D0 menangkapnya dari innertube.
    Membership punya parser sendiri: innertube saat ini tidak menangkap
    renderer membership, dan satu event muncul dalam dua format Streamlabs.
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


_sl_membership_seen: deque = deque(maxlen=64)

# YouTube innertube membership + Streamlabs socket for the same member = one alert.
MEMBERSHIP_DEDUPE_WINDOW_SEC = 30.0
_membership_cross_seen: deque = deque(maxlen=64)

_YT_MEMBERSHIP_ITEM = "liveChatMembershipItemRenderer"
_YT_GIFT_PURCHASE = (
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer",
    "liveChatMembershipGiftingRenderer",
)
_YT_GIFT_RECEIVED = (
    "liveChatSponsorshipsGiftRedemptionAnnouncementRenderer",
    "liveChatGiftedMembershipReceivedRenderer",
)


def reset_membership_alerts() -> None:
    """Test seam: clear Streamlabs format-dedup and the cross-source window."""
    _sl_membership_seen.clear()
    _membership_cross_seen.clear()


def claim_membership_alert(name: str, *, now: float | None = None,
                           window: float = MEMBERSHIP_DEDUPE_WINDOW_SEC) -> bool:
    """True if this YouTube member should fire an alert; False if seen within the window.

    Window: MEMBERSHIP_DEDUPE_WINDOW_SEC (30s). Same member from innertube and the
    Streamlabs socket within that window is one alert.
    """
    key = (name or "").strip().lstrip("@").casefold() or "seseorang"
    t = time.monotonic() if now is None else now
    while _membership_cross_seen and t - _membership_cross_seen[0][1] > window:
        _membership_cross_seen.popleft()
    if any(k == key for k, _ in _membership_cross_seen):
        return False
    _membership_cross_seen.append((key, t))
    return True


def _yt_runs_text(obj) -> str:
    if not isinstance(obj, dict):
        return str(obj).strip() if obj else ""
    simple = obj.get("simpleText")
    if simple:
        return str(simple).strip()
    runs = obj.get("runs")
    if isinstance(runs, list):
        return "".join(
            str(r.get("text") or "") for r in runs if isinstance(r, dict)
        ).strip()
    return ""


def _yt_months_from_text(text: str) -> int:
    m = re.search(r"(\d+)", text or "")
    if not m:
        return 0
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return 0


def _membership_event(name: str, *, message: str = "", months: int = 0,
                      level: str = "", raw: dict | None = None) -> DonationEvent:
    return DonationEvent(
        platform="youtube",
        name=(name or "").strip() or "Seseorang",
        amount=0.0,
        message=(message or "").strip(),
        kind="membership",
        membership_months=months,
        membership_level=(level or "").strip(),
        raw=raw or {},
    )


def parse_youtube_membership_item(item) -> list[DonationEvent]:
    """Normalize one innertube chat item with a membership/gifted renderer.

    Handles liveChatMembershipItemRenderer plus gifted purchase announcement
    and gifted-membership received renderers (current sponsorships names and
    the older gifted-membership names). Super Chat / Super Sticker are not
    parsed here.
    """
    try:
        if not isinstance(item, dict):
            return []
        if _YT_MEMBERSHIP_ITEM in item:
            r = item.get(_YT_MEMBERSHIP_ITEM) or {}
            if not isinstance(r, dict):
                return []
            header = _yt_runs_text(r.get("headerPrimaryText") or {})
            sub = _yt_runs_text(r.get("headerSubtext") or {})
            ev = _membership_event(
                _yt_runs_text(r.get("authorName") or {}),
                message=_yt_runs_text(r.get("message") or {}),
                months=_yt_months_from_text(header),
                level=sub,
                raw=dict(r, _renderer=_YT_MEMBERSHIP_ITEM),
            )
            return [ev]
        for key in _YT_GIFT_PURCHASE:
            if key not in item:
                continue
            r = item.get(key) or {}
            if not isinstance(r, dict):
                return []
            header = r.get("header") if isinstance(r.get("header"), dict) else {}
            hdr = header.get("liveChatSponsorshipsHeaderRenderer") or header or r
            if not isinstance(hdr, dict):
                hdr = r
            name = (
                _yt_runs_text(hdr.get("authorName") or {})
                or _yt_runs_text(r.get("authorName") or {})
            )
            message = (
                _yt_runs_text(hdr.get("primaryText") or {})
                or _yt_runs_text(r.get("primaryText") or {})
                or _yt_runs_text(r.get("message") or {})
            )
            return [_membership_event(
                name, message=message, raw=dict(r, _renderer=key),
            )]
        for key in _YT_GIFT_RECEIVED:
            if key not in item:
                continue
            r = item.get(key) or {}
            if not isinstance(r, dict):
                return []
            name = _yt_runs_text(r.get("authorName") or {})
            message = _yt_runs_text(r.get("message") or r.get("headerSubtext") or {})
            return [_membership_event(
                name, message=message, raw=dict(r, _renderer=key),
            )]
        return []
    except Exception:  # noqa: BLE001 — payload aneh tidak boleh merobohkan reader
        return []


def parse_streamlabs_membership(payload) -> list[DonationEvent]:
    """Normalisasi YouTube membership Streamlabs dan dedup dua formatnya."""
    try:
        if not isinstance(payload, dict) or payload.get("type") != "subscription":
            return []
        raw_items = payload.get("message")
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        out: list[DonationEvent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            platform = str(
                payload.get("for") or item.get("platform") or ""
            ).strip().lower()
            if platform not in ("youtube", "youtube_account"):
                continue
            name = str(
                item.get("name")
                or item.get("from_display_name")
                or item.get("display_name")
                or item.get("from")
                or "Seseorang"
            ).strip() or "Seseorang"
            try:
                months = int(item.get("months") or 0)
            except (TypeError, ValueError):
                months = 0
            level = str(
                item.get("membershipLevelName")
                or item.get("levelName")
                or item.get("planName")
                or item.get("subPlan")
                or ""
            ).strip()
            raw_message = item.get("message")
            message = raw_message.strip() if isinstance(raw_message, str) else ""
            dedupe_key = (name.casefold(), months, level.casefold())
            if dedupe_key in _sl_membership_seen:
                continue
            _sl_membership_seen.append(dedupe_key)
            out.append(DonationEvent(
                platform="youtube",
                name=name,
                amount=0.0,
                message=message,
                kind="membership",
                membership_months=months,
                membership_level=level,
                raw=item,
            ))
        return out
    except Exception:  # noqa: BLE001 — payload aneh tidak boleh merobohkan listener
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

    Temuan audit: string 11-char SEMBARANG (nama viewer "viewerxyz01"!) dulu
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
    """Bentuk PASTI type "mediaShareEvent" — dikunci dari sampel event asli
    (data/streamlabs_events_sample.jsonl):

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
    """Parser media share Streamlabs (loyalty points, durasi dibatasi channel).

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


# V11: a new YouTube subscriber arrives as Streamlabs type "follow" (message: a list of {name, id, _id, event_id,
# publishedAt}, "for": "youtube_account"); before this it fell to the unknown-type sample and never reached her.
# On only when the bridge config enables the live voice fixes
# (start_donation_listeners), so a follow can never take the donation path elsewhere.
FOLLOW_ALERTS = False
_sl_follow_seen: deque = deque(maxlen=128)


def parse_streamlabs_follow(payload) -> list[DonationEvent]:
    """A new YouTube subscriber (Streamlabs type "follow"); one event per id."""
    try:
        if not isinstance(payload, dict) or payload.get("type") != "follow":
            return []
        raw_items = payload.get("message")
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        out: list[DonationEvent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            platform = str(payload.get("for") or item.get("platform") or "").strip().lower()
            if platform not in ("youtube", "youtube_account"):
                continue
            name = str(item.get("name") or item.get("display_name") or "").strip()
            if not name:
                continue
            key = str(item.get("event_id") or item.get("_id") or item.get("id") or name.casefold())
            if key in _sl_follow_seen:
                continue
            _sl_follow_seen.append(key)
            out.append(DonationEvent(platform="youtube", name=name, amount=0.0, kind="follow", raw=item))
        return out
    except Exception:  # noqa: BLE001
        return []


def format_follow_trigger(ev: DonationEvent) -> str:
    """Who and what, like a membership alert; no amount."""
    return (f"[SUBSCRIBER BARU {ev.platform_label} dari {ev.name}]: "
            "dia baru subscribe channel; ucapkan terima kasih ke dia, sebut namanya")


def voice_follow(ev: DonationEvent, add_to_history, schedule) -> None:
    """The bridge's follow alert (V11): history now, then the same delayed alert turn as a membership."""
    print(f"[Subscriber] {ev.platform}: {ev.name}")
    trigger = format_follow_trigger(ev)
    add_to_history(f"Viewer {ev.name} ({ev.platform_label})", trigger)
    schedule(trigger, ev.name, ev.message, gate_desktop=False)


def parse_streamlabs_payload(payload) -> list[DonationEvent]:
    """Pintu tunggal semua event Streamlabs yang didukung."""
    return (
        parse_streamlabs_event(payload)
        or parse_streamlabs_mediashare(payload)
        or parse_streamlabs_membership(payload)
        or (parse_streamlabs_follow(payload) if FOLLOW_ALERTS else [])
    )


_KNOWN_SL_TYPES = {"donation", "subscription"} | set(_MEDIASHARE_TYPE_CANDIDATES)
# Streamlabs housekeeping with no new viewer action: the overlay's own replay of an alert, label updates.
_SL_HOUSEKEEPING_TYPES = {"alertplaying", "streamlabels", "streamlabels.underlying"}
_SL_SAMPLE_PATH = os.path.join("data", "streamlabs_events_sample.jsonl")


def record_unknown_streamlabs(payload) -> None:
    """Foto payload tipe tak dikenal — sampel asli mengunci parser nantinya."""
    try:
        typ = str((payload or {}).get("type") or "?") if isinstance(payload, dict) else "?"
        norm = typ.lower().replace(" ", "")
        if norm in _KNOWN_SL_TYPES or norm in _SL_HOUSEKEEPING_TYPES or (norm == "follow" and FOLLOW_ALERTS):
            return
        os.makedirs(os.path.dirname(_SL_SAMPLE_PATH), exist_ok=True)
        # One valid JSON row per event (2026-11-17: rows cut at 4000 characters were unreadable): the type, the
        # top-level keys, and the payload or its first 3000 characters.
        body = json.dumps(payload, ensure_ascii=False, default=str)
        row = dict(type=typ, keys=sorted(payload) if isinstance(payload, dict) else [],
                   utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if len(body) <= 3000:
            row["payload"] = payload
        else:
            row["payload_head"] = body[:3000]
        with open(_SL_SAMPLE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
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

    def __init__(self, token: str, on_event, *, name: str = "streamlabs-listener",
                 on_connect=None, on_error=None):
        super().__init__(name=name, daemon=True)
        self.token = token
        self.on_event = on_event
        self.on_connect = on_connect
        self.on_error = on_error
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
            if self.on_connect:
                try:
                    self.on_connect()
                except Exception:  # noqa: BLE001
                    pass

        @sio.on("event")
        def on_sl_event(data):  # noqa: ANN001, ANN202
            events = parse_streamlabs_payload(data)
            if not events:
                record_unknown_streamlabs(data)
                return
            for ev in events:
                try:
                    self.on_event(ev)
                except Exception as e:  # noqa: BLE001
                    print(f"[Donasi] Callback error: {type(e).__name__}: {e}")

        backoff = 5.0
        while not self._stop_event.is_set():
            try:
                sio.connect(f"{STREAMLABS_SOCKET_URL}?token={self.token}",
                            transports=["websocket"])
                backoff = 5.0
                sio.wait()
            except Exception as e:  # noqa: BLE001
                if not self._stop_event.is_set():
                    # Exceptions may embed the credential-bearing socket URL.
                    err_name = type(e).__name__
                    print(f"[Donasi] Streamlabs gagal connect ({err_name}) — retry {backoff:g}s")
                    if self.on_error:
                        try:
                            self.on_error(err_name)
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                try:
                    sio.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if not self._stop_event.is_set():
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
        print("[Donasi] Listener Streamlabs berhenti.")


def resolve_saweria_key(config: dict | None = None) -> str:
    cfg = config or {}
    return (cfg.get("saweria_stream_key") or os.environ.get("SAWERIA_STREAM_KEY") or "").strip()


class SaweriaListener(threading.Thread):
    """Thread daemon: connect -> terima event -> callback; reconnect backoff."""

    def __init__(self, stream_key: str, on_event, *, name: str = "saweria-listener",
                 on_connect=None, on_error=None):
        super().__init__(name=name, daemon=True)
        self.stream_key = stream_key
        self.on_event = on_event
        self.on_connect = on_connect
        self.on_error = on_error
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover — jaringan; logika murni diuji terpisah
        from websockets.sync.client import connect  # noqa: PLC0415
        from websockets.exceptions import ConnectionClosedOK  # noqa: PLC0415

        backoff = 5.0
        url = f"{SAWERIA_WS_URL}?streamKey={self.stream_key}"
        while not self._stop_event.is_set():
            connected_at = None
            try:
                with connect(url, open_timeout=15) as ws:
                    print("[Donasi] Saweria tersambung — nunggu donasi masuk...")
                    if self.on_connect:
                        try:
                            self.on_connect()
                        except Exception:  # noqa: BLE001
                            pass
                    backoff = 5.0  # blind window after any connect stays <= 5 s
                    connected_at = time.monotonic()
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
                err_name = type(e).__name__
                # Only a connection that actually lived gets the instant retry;
                # a socket that is closed right after the handshake (bad key,
                # server refusing) must not turn into a tight reconnect loop.
                if (isinstance(e, ConnectionClosedOK) and connected_at is not None
                        and time.monotonic() - connected_at >= 5.0):
                    print("[Donasi] Saweria ditutup normal — reconnect segera")
                    continue
                print(
                    f"[Donasi] Saweria putus ({err_name}) — "
                    f"reconnect dalam {backoff:.0f}s"
                )
                if self.on_error:
                    try:
                        self.on_error(err_name)
                    except Exception:  # noqa: BLE001
                        pass
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
        print("[Donasi] Listener Saweria berhenti.")


def _normalize_trakteer_stream_key(stream_key: str) -> str:
    # trakteerdonate client.py:65-69 — Widget URL may include the prefix.
    key = (stream_key or "").strip()
    if key.startswith("trstream-"):
        key = key[9:]
    return key


def resolve_trakteer_hash(config: dict | None = None) -> str:
    cfg = config or {}
    return (cfg.get("trakteer_hash") or os.environ.get("TRAKTEER_HASH") or "").strip()


def resolve_trakteer_stream_key(config: dict | None = None) -> str:
    cfg = config or {}
    return _normalize_trakteer_stream_key(
        cfg.get("trakteer_stream_key") or os.environ.get("TRAKTEER_STREAM_KEY") or ""
    )


class TrakteerListener(threading.Thread):
    """Thread daemon: Pusher overlay WS -> tip events; reconnect backoff like Saweria.

    THIRD-PARTY unofficial protocol. Never logs hash/key values, only lengths.
    """

    def __init__(self, user_hash: str, stream_key: str, on_event,
                 *, name: str = "trakteer-listener", on_connect=None, on_error=None):
        super().__init__(name=name, daemon=True)
        self.user_hash = (user_hash or "").strip()
        self.stream_key = _normalize_trakteer_stream_key(stream_key)
        self.on_event = on_event
        self.on_connect = on_connect
        self.on_error = on_error
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover — jaringan; parser murni diuji terpisah
        from websockets.sync.client import connect  # noqa: PLC0415
        from websockets.exceptions import ConnectionClosedOK  # noqa: PLC0415

        backoff = 5.0
        url = TRAKTEER_WS_URL
        live_ch = f"creator-stream.{self.user_hash}.trstream-{self.stream_key}"
        test_ch = f"creator-stream-test.{self.user_hash}.trstream-{self.stream_key}"
        subscribe_msgs = (
            json.dumps({"event": "pusher:subscribe",
                        "data": {"auth": "", "channel": live_ch}}),
            json.dumps({"event": "pusher:subscribe",
                        "data": {"auth": "", "channel": test_ch}}),
        )
        ping_msg = json.dumps({"event": "pusher:ping", "data": {}})
        while not self._stop_event.is_set():
            connected_at = None
            try:
                with connect(url, open_timeout=15) as ws:
                    print("[Donasi] Trakteer tersambung — nunggu donasi masuk...")
                    if self.on_connect:
                        try:
                            self.on_connect()
                        except Exception:  # noqa: BLE001
                            pass
                    backoff = 5.0
                    connected_at = time.monotonic()
                    subscribed = False
                    while not self._stop_event.is_set():
                        try:
                            raw = ws.recv(timeout=15)
                        except TimeoutError:
                            if subscribed:
                                try:
                                    ws.send(ping_msg)
                                except Exception:  # noqa: BLE001
                                    break
                            continue
                        event_name = ""
                        try:
                            peek = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                            if isinstance(peek, dict):
                                event_name = str(peek.get("event") or "")
                        except Exception:  # noqa: BLE001
                            peek = None
                        if event_name == "pusher:connection_established" and not subscribed:
                            for msg in subscribe_msgs:
                                ws.send(msg)
                            subscribed = True
                        for ev in parse_trakteer_event(raw):
                            try:
                                self.on_event(ev)
                            except Exception as e:  # noqa: BLE001
                                print(f"[Donasi] Callback error: {type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                err_name = type(e).__name__
                if (isinstance(e, ConnectionClosedOK) and connected_at is not None
                        and time.monotonic() - connected_at >= 5.0):
                    print("[Donasi] Trakteer ditutup normal — reconnect segera")
                    continue
                print(
                    f"[Donasi] Trakteer putus ({err_name}) — "
                    f"reconnect dalam {backoff:.0f}s"
                )
                if self.on_error:
                    try:
                        self.on_error(err_name)
                    except Exception:  # noqa: BLE001
                        pass
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
        print("[Donasi] Listener Trakteer berhenti.")


def start_donation_listeners(config: dict, on_event) -> list[threading.Thread]:
    """Nyalakan semua listener platform yang kredensialnya tersedia."""
    global FOLLOW_ALERTS
    if not config.get("donation_enabled", False):
        return []
    FOLLOW_ALERTS = bool(config.get("live_voice_fixes_v1"))
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

    th = resolve_trakteer_hash(config)
    tk = resolve_trakteer_stream_key(config)
    if th and tk:
        t3 = TrakteerListener(th, tk, on_event)
        t3.start()
        started.append(t3)
        print(
            f"[Donasi] Listener Trakteer dinyalakan "
            f"(hash {len(th)} char, stream_key {len(tk)} char)."
        )
    else:
        print("[Donasi] TRAKTEER_HASH/TRAKTEER_STREAM_KEY kosong — listener Trakteer dilewati.")
    return started
