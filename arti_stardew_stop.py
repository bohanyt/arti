"""Shared Python STOP scope and retention. No game/process effects on import.

Native stop.json uses runtime + session; Live7 sentinels additionally bind a
unique cycle (the resolved cycle folder). Unknown/legacy records fail closed.
Only the supervisor establishes that a foreign session ended before startup.
"""
from __future__ import annotations

from arti_runtime import ipc
import base64
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import uuid


class StopRefused(ValueError):
    pass


def scope(value):
    value = value if isinstance(value, dict) else {}
    return {k: value.get(k) for k in ("runtime_instance_id", "session_id", "cycle_id")}


def scoped(value, *, native=False):
    return (isinstance(value, dict) and type(value.get("protocol")) is int and value["protocol"] == 1
            and all(isinstance(value.get(k), str) and 0 < len(value[k]) <= 128
                    for k in ("runtime_instance_id", "session_id"))
            and (not native or type(value.get("generation")) is int and 0 < value["generation"] <= 2**63 - 1
                 and isinstance(value.get("owner_id"), str) and len(value["owner_id"]) <= 128))


def read_stop(path):
    try:
        with ipc.open_shared(Path(path), "rb") as stream:
            raw = stream.read(4097)  # native MenuStepCommandInbox.MaxRequestBytes
    except FileNotFoundError:
        return None, None
    if len(raw) > 4096:
        raise StopRefused("STOP oversize; operator must inspect and archive it explicitly")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        value = None
    return raw, value


def pending(path, current):
    """Read-only: a known foreign scope cannot halt this consumer; unknown can."""
    try:
        raw, value = read_stop(path)
        if raw is None:
            return False
        if not scoped(value, native=Path(path).name == "stop.json"):
            return True
        own = scope(current)
        if Path(path).name != "stop.json" and own.get("cycle_id") and value.get("cycle_id") == own["cycle_id"]:
            return True  # a cycle STOP remains active through its crash/rejoin
        if own.get("session_id") == value["session_id"]:
            return True  # including an ambiguous same-session/different-runtime pair
        if not all(own.get(k) for k in ("runtime_instance_id", "session_id")):
            # Cycle-only consumers can still recognize a known foreign cycle.
            return not (not own.get("session_id") and value.get("cycle_id") and own.get("cycle_id")
                        and value["cycle_id"] != own["cycle_id"])
        return False
    except (OSError, ValueError):
        return True


def provenance(writer):
    return dict(writer=writer, created_at=datetime.now(timezone.utc).isoformat(),
                created_by=getpass.getuser(), writer_pid=os.getpid())


def archive_stop(path, *, raw, reason, current, evidence, archive_dir=None):
    """Keep exact bytes + durable audit before moving, and refuse any raced write.

    No unlink and no overwrite of an existing STOP. If replacement races the
    move, restore it when possible and refuse play even if restoration fails.
    """
    path = Path(path)
    archive = Path(archive_dir) if archive_dir else path.parent / "stop-archive"
    archive.mkdir(parents=True, exist_ok=True)
    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex
    target = archive / (token + ".stop")
    audit_path = archive / (token + ".audit.json")
    try:
        original = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        original = None
    audit = dict(schema="stardew_stop_archive.v1", who=getpass.getuser(),
                 when=datetime.now(timezone.utc).isoformat(), why=reason, reason=reason,
                 source=str(path.resolve()), archive=str(target.resolve()),
                 original=original, original_base64=base64.b64encode(raw).decode("ascii"),
                 sha256=hashlib.sha256(raw).hexdigest(), current_scope=scope(current), evidence=evidence)
    ipc.write_json(audit_path, audit, create_only=True, fsync=True)
    if ipc.read_bytes(path) != raw:
        raise StopRefused("STOP changed during archival; operator must reconcile retained audit")
    path.rename(target)
    if ipc.read_bytes(target) != raw:
        # A native STOP won the last read/rename race. Do not start anything.
        if not path.exists():
            target.rename(path)
        raise StopRefused("STOP changed during archival move; operator must reconcile " + str(audit_path))
    return dict(archive=str(target), audit=str(audit_path), original=original, reason=reason)


def write_sentinel(path, current, *, writer):
    """Publish once: an existing STOP (including legacy) must never be overwritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(protocol=1, **scope(current), **provenance(writer))
    return ipc.create_json_once(path, record, fsync=True)


# PM-1 fix10f (a15 day-13 refusal): persistent STOP classes by provenance. An owner hard stop (HP/KO, mutation
# risk, stuck input with native held evidence, crash, 10 min without progress, the owner/operator's own STOP) is
# retained and refuses every start until the operator re-arms. A runtime writer that only ever wrote for a lease
# fault (fix10f removed it) is recoverable: it can never block the next-day startup once native shows input idle.
OWNER_STOP_WRITER_PREFIXES = ("arti_stop", "safety", "supervisor.", "runtime.owner_stop:")
RECOVERABLE_STOP_WRITERS = frozenset({"runtime.water_stop"})


def stop_class(value):
    """'owner_hard_stop' | 'recoverable' | 'unknown' (unknown provenance fails closed like an owner stop)."""
    writer = value.get("writer") if isinstance(value, dict) else None
    if not isinstance(writer, str):
        return "unknown"
    if writer in RECOVERABLE_STOP_WRITERS:
        return "recoverable"
    if writer.startswith(OWNER_STOP_WRITER_PREFIXES):
        return "owner_hard_stop"
    return "unknown"


def recoverable_native_state(session, raw):
    """None when native shows this recoverable STOP latched and input idle, else the named blocker.

    Native held evidence (input busy, an inflight request, an unacked release, outstanding pad buttons) or any
    halt entry that is not exactly this file keeps the refusal: those belong to the hard-stop classes."""
    if not isinstance(session, dict):
        return "session_unreadable"
    for key, ok in (("input_busy", session.get("input_busy") is False),
                    ("inflight_request_id", session.get("inflight_request_id") is None),
                    ("release_acked", session.get("release_acked") is True),
                    ("pad_outstanding_count", session.get("pad_outstanding_count") in (None, 0))):
        if not ok:
            return "native_held_evidence:" + key
    halt = session.get("halt")
    if not isinstance(halt, dict) or not halt.get("entries"):
        return "halt_not_latched"
    sha = hashlib.sha256(raw).hexdigest()
    if not all(isinstance(e, dict) and e.get("source") == "stop" and e.get("stop_sha256") == sha
               for e in halt["entries"]):
        return "halt_has_other_entries"
    return None
