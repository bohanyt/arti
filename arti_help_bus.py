"""Shared ``arti.help/1`` objects and bounded, atomic file transport.

Public codecs return JSON strings and accept JSON strings/UTF-8 bytes. File
methods take request IDs, never paths. ``list_pending_requests`` returns IDs;
``pending_requests`` returns validated requests without an existing reply.
This module deliberately uses only the standard library.
"""

from __future__ import annotations

from arti_runtime import ipc
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata

PROTOCOL_VERSION = 1
MAX_JSON_BYTES = 32768
MAX_HINTS = 64
MAX_SAID = 3
_SAID_KIND = "said_to_you"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_URL = re.compile(
    r"\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+"
    r"|\b(?:[\w-]+\.)+[a-z]{2,63}(?:[/?#]\S*)?",
    re.IGNORECASE,
)
_MENTION = re.compile(r"@[\w.-]+")


def sanitize_hint_text(text: str) -> str:
    """Strip links, handles and control characters; return at most 200 chars."""
    if not isinstance(text, str):
        raise ValueError("hint text must be a string")
    text = "".join(
        " " if char.isspace() else char
        for char in text
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    text = _MENTION.sub(" ", _URL.sub(" ", text))
    return " ".join(text.split())[:200].rstrip()


def _text(value, field, limit, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ValueError(f"invalid {field}")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError(f"control characters in {field}")


def _request_id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("request_id must be 32 lowercase hex characters")


def _version(value):
    if type(value) is not int or value != PROTOCOL_VERSION:
        raise ValueError("unsupported arti.help version")


def _timestamp(value, field):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"invalid {field}")


def _said_row(row):
    """Contract row or None. Extra keys are dropped; invalid rows are skipped."""
    if type(row) is not dict:
        return None
    kind, who, platform, text, at = (
        row.get("kind"), row.get("who"), row.get("platform"), row.get("text"), row.get("at"),
    )
    if kind != _SAID_KIND:
        return None
    try:
        _text(who, "who", 40, empty=False)
        _text(platform, "platform", 24, empty=False)
        _text(text, "text", 140, empty=False)
        _timestamp(at, "at")
    except (TypeError, ValueError):
        return None
    return dict(kind=_SAID_KIND, who=who, platform=platform, text=text, at=float(at))


@dataclass(frozen=True, kw_only=True)
class HelpRequest:
    v: int = 1
    request_id: str
    session_id: str
    reason: str
    game_date: str
    game_time: int | None
    location: str | None
    skill: str | None
    detail: str
    created_at: float
    expires_at: float

    def __post_init__(self):
        _version(self.v)
        _request_id(self.request_id)
        for field, limit in (("session_id", 128), ("reason", 96), ("game_date", 64)):
            _text(getattr(self, field), field, limit, empty=False)
        for field, limit in (("location", 128), ("skill", 96)):
            value = getattr(self, field)
            if value is not None:
                _text(value, field, limit)
        _text(self.detail, "detail", 160)
        if self.game_time is not None and (
            type(self.game_time) is not int or not 0 <= self.game_time <= 2600
            or self.game_time % 100 >= 60
        ):
            raise ValueError("invalid game_time")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.expires_at, "expires_at")
        if self.expires_at < self.created_at:
            raise ValueError("expires_at precedes created_at")


@dataclass(frozen=True)
class HelpHint:
    source: str
    text: str
    at: float

    def __post_init__(self):
        if self.source not in ("owner", "viewer"):
            raise ValueError("invalid hint source")
        _text(self.text, "hint text", 200, empty=False)
        _timestamp(self.at, "hint timestamp")


@dataclass(frozen=True, kw_only=True)
class HelpReply:
    v: int = 1
    request_id: str
    status: str
    spoken: bool
    hints: tuple[HelpHint, ...]

    def __post_init__(self):
        _version(self.v)
        _request_id(self.request_id)
        if self.status not in ("delivered", "hinted", "suppressed", "expired"):
            raise ValueError("invalid reply status")
        if type(self.spoken) is not bool:
            raise ValueError("spoken must be a boolean")
        if type(self.hints) is not tuple or len(self.hints) > MAX_HINTS:
            raise ValueError("invalid hints")
        if any(type(hint) is not HelpHint for hint in self.hints):
            raise ValueError("invalid hint object")


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON field")
        obj[key] = value
    return obj


def _loads(payload):
    if not isinstance(payload, (str, bytes)):
        raise ValueError("expected JSON string or bytes")
    try:
        size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
        if size > MAX_JSON_BYTES:
            raise ValueError("help JSON too large")
        obj = json.loads(payload, object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid help JSON") from exc
    if type(obj) is not dict:
        raise ValueError("expected help JSON object")
    return obj


def _fields(obj, cls):
    if type(obj) is not dict or obj.keys() != cls.__dataclass_fields__.keys():
        raise ValueError(f"invalid {cls.__name__} fields")


def encode_request(request: HelpRequest) -> str:
    if type(request) is not HelpRequest:
        raise ValueError("expected HelpRequest")
    return json.dumps(asdict(request), ensure_ascii=False, allow_nan=False)


def decode_request(payload: str | bytes) -> HelpRequest:
    obj = _loads(payload)
    _fields(obj, HelpRequest)
    return HelpRequest(**obj)


def encode_reply(reply: HelpReply) -> str:
    if type(reply) is not HelpReply:
        raise ValueError("expected HelpReply")
    return json.dumps(asdict(reply), ensure_ascii=False, allow_nan=False)


def decode_reply(payload: str | bytes) -> HelpReply:
    obj = _loads(payload)
    _fields(obj, HelpReply)
    if type(obj["hints"]) is not list or len(obj["hints"]) > MAX_HINTS:
        raise ValueError("invalid hints")
    hints = []
    for hint in obj["hints"]:
        _fields(hint, HelpHint)
        hints.append(HelpHint(**hint))
    obj["hints"] = tuple(hints)
    return HelpReply(**obj)


class FileHelpChannel:
    """One request/reply pair per UUID; invalid/incomplete requests are skipped.

    Reply files are terminal acknowledgements, so requests with a reply are
    excluded from pending listings. Callers own retention/cleanup policy.
    """

    def __init__(self, help_dir: str | os.PathLike):
        self.help_dir = Path(help_dir)

    def _path(self, prefix, request_id):
        _request_id(request_id)
        return self.help_dir / f"{prefix}-{request_id}.json"

    def _write(self, path, payload):
        ipc.atomic_write(path, payload, fsync=True)

    def _read(self, prefix, request_id, decoder):
        path = self._path(prefix, request_id)
        try:
            with ipc.open_shared(path, "rb") as handle:
                payload = handle.read(MAX_JSON_BYTES + 1)
        except FileNotFoundError:
            return None
        result = decoder(payload)
        if result.request_id != request_id:
            raise ValueError("help filename/payload ID mismatch")
        return result

    def write_request(self, request: HelpRequest) -> None:
        self._write(self._path("req", request.request_id), encode_request(request))

    def write_reply(self, reply: HelpReply) -> None:
        self._write(self._path("rep", reply.request_id), encode_reply(reply))

    def read_request(self, request_id: str) -> HelpRequest | None:
        return self._read("req", request_id, decode_request)

    def read_reply(self, request_id: str) -> HelpReply | None:
        return self._read("rep", request_id, decode_reply)

    def list_pending_requests(self) -> tuple[str, ...]:
        return tuple(
            path.stem[4:] for path in sorted(self.help_dir.glob("req-*.json"))
            if _ID.fullmatch(path.stem[4:])
            and not self._path("rep", path.stem[4:]).exists()
        )

    def pending_requests(self) -> tuple[HelpRequest, ...]:
        requests = []
        for request_id in self.list_pending_requests():
            try:
                request = self.read_request(request_id)
            except (OSError, ValueError):
                continue
            if request is not None:
                requests.append(request)
        return tuple(requests)

    def write_said(self, rows) -> None:
        valid = []
        if isinstance(rows, (list, tuple)):
            for item in rows:
                row = _said_row(item)
                if row is not None:
                    valid.append(row)
        self._write(
            self.help_dir / "said.json",
            json.dumps(valid[-MAX_SAID:], ensure_ascii=False, allow_nan=False),
        )

    def read_said(self) -> list[dict]:
        path = self.help_dir / "said.json"
        try:
            with ipc.open_shared(path, "rb") as handle:
                payload = handle.read(MAX_JSON_BYTES + 1)
        except FileNotFoundError:
            return []
        except OSError:
            return []
        if len(payload) > MAX_JSON_BYTES:
            return []
        try:
            obj = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return []
        if type(obj) is not list:
            return []
        rows = []
        for item in obj:
            row = _said_row(item)
            if row is not None:
                rows.append(row)
        return rows[-MAX_SAID:]
