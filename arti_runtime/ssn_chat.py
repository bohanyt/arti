"""Social Stream Ninja (SSN) chat: one door for YouTube, TikTok and Instagram live chat.

SSN (the owner's desktop app) relays every chat line it reads to its websocket API server; a listener joins
`<base>/join/<session>/4` (channel 4 = chat broadcast). This module is the pure part: parse SSN payloads into
`ChatLine`s, drop duplicates, decide what the bridge does with each line, and run the reconnecting reader.

Rules (owner, 2026-11-10 mabar order; 2026-08-29 SSN handoff):
- Every line carries its platform and the viewer's name, so she knows who said what and where.
- TikTok and Instagram lines are conversation only: their viewer name is namespaced ("name (TikTok)") so it can
  never match an owner YouTube handle, and they skip the YouTube-only viewer features (Live7 help hints, video links).
- YouTube lines keep today's YouTube viewer behavior (the innertube reader's rules); SSN replaces that reader,
  config refuses both together so YouTube never arrives twice.
- Nothing is silently lost: a line that does not trigger a voiced turn still lands in her chat history.
- The session id is a secret: the bridge process reads it at start (`resolve_session`: the owner's SSN app config, or
  the operator's one-use secret file) and keeps it in memory only; it never appears in logs, files or error text.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import html
import json
import os
import re
import threading
import time

DEFAULT_WS_BASE = "wss://io.socialstream.ninja"
CHAT_CHANNEL = 4
MAX_TEXT = 300
MAX_NAME = 40
# SSN `type` -> the platform name she hears. Anything else (Twitch, Kick, SSN's own test source...) is ignored.
PLATFORMS = {"youtube": "YouTube", "youtubeshorts": "YouTube", "tiktok": "TikTok",
             "instagram": "Instagram", "instagramlive": "Instagram"}
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


@dataclasses.dataclass(frozen=True)
class ChatLine:
    platform: str          # "YouTube" | "TikTok" | "Instagram"
    name: str              # display name as SSN gives it
    text: str              # plain text (HTML stripped)
    msg_id: str            # SSN id, or a content hash when SSN gives none
    donation: str = ""     # SSN hasDonation ("$5.00", "[recorded remark omitted]"), empty when none

    @property
    def viewer(self) -> str:
        """The viewer identity the bridge uses (cooldowns, owner check). Non-YouTube names are namespaced."""
        return self.name if self.platform == "YouTube" else "%s (%s)" % (self.name, self.platform)


def plain(value) -> str:
    text = html.unescape(_TAG.sub(" ", str(value or "")))
    return _SPACE.sub(" ", text).strip()


def _candidates(payload):
    """SSN sends a chat object, sometimes wrapped ({"overlayNinja": {...}}, {"contents": {...}}) or batched."""
    if isinstance(payload, list):
        for item in payload:
            yield from _candidates(item)
        return
    if not isinstance(payload, dict):
        return
    if "chatmessage" in payload or "chatname" in payload:
        yield payload
        return
    for key in ("overlayNinja", "contents", "content", "data", "message"):
        inner = payload.get(key)
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                continue
        if isinstance(inner, (dict, list)):
            yield from _candidates(inner)


def parse(raw) -> list[ChatLine]:
    """Chat lines in one websocket frame (str/bytes JSON or an already-decoded object). Never raises."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    lines = []
    for item in _candidates(raw):
        platform = PLATFORMS.get(str(item.get("type") or "").strip().lower())
        if platform is None or item.get("event"):   # joins/follows/likes are events, not chat
            continue
        name = plain(item.get("chatname"))[:MAX_NAME]
        text = plain(item.get("chatmessage"))[:MAX_TEXT]
        donation = plain(item.get("hasDonation"))[:40]
        if not name or not (text or donation):
            continue
        msg_id = str(item.get("id") or "").strip()
        if not msg_id:
            msg_id = "h:" + hashlib.sha256(("%s|%s|%s" % (platform, name, text)).encode("utf-8")).hexdigest()[:16]
        lines.append(ChatLine(platform, name, text, "%s:%s" % (platform, msg_id), donation))
    return lines


class Deduper:
    """SSN may resend a line (reconnect, dock refresh). Id-based; content-hash ids expire after `window` seconds
    so a viewer who really types the same thing twice is heard again later."""

    def __init__(self, keep=5000, window=30.0, clock=time.monotonic):
        self._seen = {}
        self._order = collections.deque()
        self._keep, self._window, self._clock = keep, window, clock

    def fresh(self, line: ChatLine) -> bool:
        now = self._clock()
        seen = self._seen.get(line.msg_id)
        hashed = line.msg_id.split(":", 1)[1].startswith("h:")
        if seen is not None and (not hashed or now - seen < self._window):
            return False
        self._seen[line.msg_id] = now
        self._order.append(line.msg_id)
        while len(self._order) > self._keep:
            self._seen.pop(self._order.popleft(), None)
        return True


def scrub(text, session: str | None) -> str:
    text = str(text)
    return text.replace(session, "***") if session else text


def join_url(base: str, session: str) -> str:
    return "%s/join/%s/%d" % (base.rstrip("/"), session, CHAT_CHANNEL)


class Counts:
    """Operator telemetry: per-platform counts only, never names or text."""

    def __init__(self):
        self._lock = threading.Lock()
        self.received = collections.Counter()
        self.duplicates = 0
        self.connects = 0
        self.errors = 0

    def note(self, platform):
        with self._lock:
            self.received[platform] += 1

    def summary(self) -> str:
        with self._lock:
            per = ", ".join("%s %d" % kv for kv in sorted(self.received.items())) or "none"
            return "[SSN Chat] lines: %s; duplicates %d; connects %d; errors %d" % (
                per, self.duplicates, self.connects, self.errors)


def run_reader(on_line, *, session: str, base: str = DEFAULT_WS_BASE, stop: threading.Event | None = None,
               counts: Counts | None = None, log=print, connect=None, backoff=(1.0, 2.0, 5.0, 10.0, 30.0),
               recv_timeout=5.0, sleep=time.sleep):
    """Listen until `stop` is set: connect, parse, dedupe, hand each fresh line to `on_line(line)`; reconnect with
    backoff on any failure. The session id never reaches `log`."""
    if not session:
        raise ValueError("SSN session id missing")
    stop = stop or threading.Event()
    counts = counts or Counts()
    dedupe = Deduper()
    if connect is None:
        from websockets.sync.client import connect as ws_connect

        def connect(url):
            return ws_connect(url, open_timeout=10, close_timeout=2, max_size=2 ** 20)
    failures = 0
    while not stop.is_set():
        try:
            with connect(join_url(base, session)) as ws:
                counts.connects += 1
                failures = 0
                log("[SSN Chat] connected (channel %d)." % CHAT_CHANNEL)
                while not stop.is_set():
                    try:
                        frame = ws.recv(timeout=recv_timeout)
                    except TimeoutError:
                        continue
                    for line in parse(frame):
                        if not dedupe.fresh(line):
                            counts.duplicates += 1
                            continue
                        counts.note(line.platform)
                        try:
                            on_line(line)
                        except Exception as exc:  # one bad line never kills the reader
                            counts.errors += 1
                            log("[SSN Chat] line handler error: %s" % type(exc).__name__)
        except Exception as exc:
            if stop.is_set():
                break
            counts.errors += 1
            delay = backoff[min(failures, len(backoff) - 1)]
            failures += 1
            log("[SSN Chat] disconnected (%s: %s); retry in %gs." % (
                type(exc).__name__, scrub(exc, session)[:160], delay))
            sleep(delay)
    log(counts.summary())


# --- what the bridge does with a line (pure; the bridge applies it) ---------------------------------------------

@dataclasses.dataclass(frozen=True)
class Decision:
    kind: str                  # "skip" | "history" | "trigger" | "donation"
    history: tuple | None      # (speaker, text) for add_to_history, None = not recorded
    label: str = ""            # trigger text she gets
    jenis: str = ""            # trigger kind, for the counts log
    wake: bool = False


def decide(line: ChatLine, *, is_bot: bool, wake: bool, new_viewer: bool, now: float, last_viewer: float,
           last_addressed: float, last_trigger: float, config: dict) -> Decision:
    """Same gates as the YouTube innertube reader (arti_bridge.youtube_chat_worker.process_message), with the
    platform in every label. Bot commands (!x) and chat bots are skipped on purpose (owner 2026-08-10)."""
    shown = ("[DONASI %s] %s" % (line.donation, line.text)).strip() if line.donation else line.text
    speaker = "Viewer %s (%s)" % (line.name, line.platform)
    if not line.donation and line.text.lstrip().startswith("!"):
        return Decision("skip", None)
    if is_bot:
        return Decision("skip", None)
    history = (speaker, shown)
    if line.donation:
        label = "[DONASI %s dari %s (%s)]: %s" % (line.donation, line.name, line.platform,
                                                  line.text or "(tanpa pesan, cukup nominal)")
        return Decision("donation", history, label, "Donasi")
    santai = not wake and not new_viewer and bool(config.get("yt_chat_santai_enabled", True))
    if not (wake or new_viewer or santai):
        return Decision("history", history)
    if not config.get("mic_priority_mark"):
        if wake:
            gap, last = float(config.get("yt_addressed_min_gap_sec", 10.0)), last_addressed
        else:
            gap, last = float(config.get("yt_viewer_cooldown_sec", 45.0)), last_viewer
        if now - last < gap:
            return Decision("history", history)
        if santai and now - last_trigger < float(config.get("yt_chat_santai_gap_sec", 60.0)):
            return Decision("history", history)
        if not config.get("voice_queue_enabled", False) and now - last_trigger < 20:
            return Decision("history", history)
    if new_viewer and not wake:
        label = "[Pesan PERTAMA dari penonton BARU bernama %s (%s), sapa dia]: %s" % (line.name, line.platform, line.text)
        jenis = "Penonton baru"
    elif santai:
        label = ("[Pesan Live Chat dari Viewer %s (%s), dia ngobrol di chat tanpa menyebut namamu; "
                 "timpali secara natural]: %s" % (line.name, line.platform, line.text))
        jenis = "Chat santai"
    else:
        label = "[Pesan Live Chat dari Viewer %s (%s)]: %s" % (line.name, line.platform, line.text)
        jenis = "Panggilan"
    return Decision("trigger", history, label, jenis, wake)


class SessionError(RuntimeError):
    """Why the session id could not be read. The message never contains the id."""


SESSION_FILE_ENV = "ARTI_SSN_SESSION_FILE"
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{4,128}")


def app_config_path(env=None):
    env = env if env is not None else os.environ
    return os.path.join(env.get("APPDATA", ""), "SocialStream", "config.json")


def session_from_app(path) -> str:
    """The session id the owner's SSN desktop app uses (its saved streamID). Exactly one distinct id, or refuse."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise SessionError("the SSN app config could not be read (%s)" % type(exc).__name__) from None
    ids = {str((data.get(k) or {}).get("streamID") or "").strip()
           for k in ("localStorageBackup", "cachedStateBackup") if isinstance(data.get(k), dict)}
    ids.discard("")
    if not ids:
        raise SessionError("the SSN app config holds no session id")
    if len(ids) > 1:
        raise SessionError("the SSN app config holds more than one session id")
    value = ids.pop()
    if not _SESSION_ID.fullmatch(value):
        raise SessionError("the SSN app session id has an unexpected shape")
    return value


def session_from_file(path) -> str:
    try:
        with open(path, encoding="utf-8-sig") as f:
            value = f.read().strip()
    except OSError as exc:
        raise SessionError("the SSN session file could not be read (%s)" % type(exc).__name__) from None
    if not _SESSION_ID.fullmatch(value):
        raise SessionError("the SSN session file does not hold a session id (letters, digits, - or _)")
    return value


def resolve_session(config, env=None) -> str:
    """Called inside the bridge process only (CT 2026-11-10): `ssn_session_source` "app" reads the owner's SSN app
    config on this machine, "file" reads the operator's one-use secret file named in ARTI_SSN_SESSION_FILE."""
    env = env if env is not None else os.environ
    source = (config or {}).get("ssn_session_source")
    if source == "app":
        return session_from_app(app_config_path(env))
    if source == "file":
        path = str(env.get(SESSION_FILE_ENV) or "").strip()
        if not path:
            raise SessionError("no SSN session file given (%s)" % SESSION_FILE_ENV)
        return session_from_file(path)
    raise SessionError("unknown ssn_session_source (app or file)")


def write_status(path, **fields):
    """Plain status for the operator (`mabar.py status`): ok/why/source, never the id or viewer data."""
    if not path:
        return
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(fields, utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())), f)
    os.replace(tmp, path)
