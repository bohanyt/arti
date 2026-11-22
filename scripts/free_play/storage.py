"""Lossless evidence storage. Mutable game files are never hardlinked.

ADR 61: original relative names and SHA256 remain the evidence identity. Only
closed harness copies enter the store; raw journals/native evidence stay raw.
ADR 193: new runs write those objects into one shared content-addressed store;
each run keeps a manifest. Old run-local `objects/` remains readable.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import threading
import time
import zipfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from types import SimpleNamespace
from arti_runtime import ipc
from scripts.free_play.duration import parse_duration

INDEX = ".storage.json"
SCHEMA = "arti.free_play.storage/1"
OBJECTS_DIR = "objects"
MANIFEST = "objects-manifest.json"
MANIFEST_SCHEMA = "arti.free_play.objects_manifest/1"
FACTS = "storage-facts.jsonl"
FACT_SCHEMA = "arti.free_play.storage_fact/1"
SHARED_STORE_ENV = "ARTI_FREE_PLAY_OBJECTS"
SHARED_SWITCH_ENV = "ARTI_SHARED_OBJECTS"
CLOSED_LOGS_ENV = "ARTI_PACK_CLOSED_LOGS"
RUNS_PARENTS = {"free-play-runs", "mabar-runs"}
FLOOR = 10 * 1024**3
_manifest_lock = threading.Lock()
# a36 lossless retained-run packing: 360,216,492 bytes / 84.574 active minutes.
# Rounded up; this is an offline compact-layout estimate for pre-dispatch admission.
MEASURED_BYTES_PER_HOUR = 260_000_000
PROVIDER_LIMIT = 256 * 1024**2  # stop admission, never silently drop evidence
WARMUP_S = 120
RATE_WINDOW_S = 600
STOP_HORIZON_S = 1800
NEAR_FLOOR_BYTES = 512 * 1024**2
HARD_FLOOR = 5 * 1024**3  # plus the full restore reserve, even during warmup
TRANSIENT_HOLD_S = 1200
# ADR 123: >= 1 GiB/h is >11x a53's measured 0.093 GB/h. At this rate
# twenty minutes consumes >=426 MiB with margin; do not defer a near-floor stop.
HIGH_GROWTH_BYTES_PER_HOUR = 1024**3


def shared_objects_enabled():
    return os.environ.get(SHARED_SWITCH_ENV, "1").strip().lower() not in {"0", "false", "off", "no"}


def shared_store_for(run):
    """Configurable shared store. Default: sibling of free-play-runs, else beside the run."""
    run = Path(run).resolve()
    env = os.environ.get(SHARED_STORE_ENV, "").strip()
    if env:
        return Path(env)
    parent = run.parent
    if parent.name in RUNS_PARENTS:
        return parent.parent / "free-play-objects"
    return parent / "free-play-objects"


def object_zip_name(sha256):
    return sha256 + ".zip"


def local_object(run, sha256):
    return Path(run).resolve() / OBJECTS_DIR / object_zip_name(sha256)


def resolve_object(run, sha256):
    """Run-local zip first (old layout / fallback), then the shared store."""
    local = local_object(run, sha256)
    if local.is_file():
        return local
    shared = shared_store_for(run) / object_zip_name(sha256)
    if shared.is_file():
        return shared
    raise ValueError("evidence object missing")


def infer_kind(source, run):
    try:
        rel = Path(source).resolve().as_posix().lower()
    except OSError:
        rel = str(source).replace("\\", "/").lower()
    if "provider_archive" in rel:
        return "provider"
    if any(part in rel for part in ("preserved-saves", "/saves/", "continuation-start", "/checkpoints/")):
        return "save"
    if any(part in rel for part in ("command-settled-journal", "command_journal", "command_segment", "command_archive")):
        return "journal"
    if any(part in rel for part in ("farm-memory", "memory-checkpoint", "discoveries")):
        return "memory"
    if "occurrences" in rel or "/nights" in rel or "nights." in rel:
        return "mod_state"
    if rel.endswith("/report.json") or rel.endswith("report.json"):
        return "report"
    if "farmhand-stdout" in rel and rel.endswith(".raw"):
        return "stdout"
    if "/template" in rel or rel.endswith("/template") or "host-mods" in rel:
        return "template"
    return "other"


def record_fact(run, fact, **fields):
    try:
        run = Path(run)
        run.mkdir(parents=True, exist_ok=True)
        row = dict(schema=FACT_SCHEMA, utc=datetime.now(timezone.utc).isoformat(), fact=fact, **fields)
        ipc.append_text(run / FACTS, json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def empty_manifest(run):
    return dict(schema=MANIFEST_SCHEMA, store=str(shared_store_for(run)), layout="shared", objects={})


def read_manifest(run):
    path = Path(run) / MANIFEST
    if not path.is_file():
        return empty_manifest(run)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != MANIFEST_SCHEMA or not isinstance(doc.get("objects"), dict):
        raise ValueError("unknown objects manifest schema")
    return doc


def record_manifest(run, sha256, *, size, kind, location):
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    with _manifest_lock:
        doc = read_manifest(run)
        doc["store"] = str(shared_store_for(run))
        doc["objects"][sha256] = dict(bytes=size, kind=kind, location=location)
        locations = {row.get("location") for row in doc["objects"].values()}
        doc["layout"] = "hybrid" if locations == {"shared", "local"} else next(iter(locations), "shared")
        temp = run / (MANIFEST + ".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(doc, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, run / MANIFEST)


def verify_object_zip(path, sha256, size):
    with zipfile.ZipFile(path) as archive:
        if archive.namelist() != ["data"]:
            raise ValueError("invalid evidence object members")
        with archive.open("data") as stream:
            if digest_stream(stream) != (sha256, size):
                raise ValueError("evidence object hash mismatch")


def write_object_zip(store, sha256, size, source):
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    target = store / object_zip_name(sha256)
    if target.is_file():
        verify_object_zip(target, sha256, size)
        return target
    fd, temp = tempfile.mkstemp(prefix=".object-", suffix=".zip", dir=store)
    os.close(fd)
    temp = Path(temp)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            with archive.open("data", "w", force_zip64=True) as out, open_file(source) as stream:
                shutil.copyfileobj(stream, out, 1024 * 1024)
        verify_object_zip(temp, sha256, size)
        try:
            os.replace(temp, target)
        except OSError:
            if not target.is_file():
                raise
        verify_object_zip(target, sha256, size)
    finally:
        temp.unlink(missing_ok=True)
    return target


def digest_stream(stream):
    h = hashlib.sha256()
    size = 0
    while block := stream.read(1024 * 1024):
        h.update(block)
        size += len(block)
    return h.hexdigest(), size


def safe_child(root, name):
    rel = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or rel.is_absolute() or any(x in ("", ".", "..") for x in name.split("/")):
        raise ValueError("unsafe storage member")
    target = Path(root).joinpath(*rel.parts)
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("storage member escapes root")
    return target


def index(folder):
    folder = Path(folder)
    doc = json.loads((folder / INDEX).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise ValueError("unknown evidence storage schema")
    store = (folder / doc["objects"]).resolve()
    if store.name != "objects" or not folder.resolve().is_relative_to(store.parent):
        raise ValueError("object store outside run")
    for name, row in doc["files"].items():
        safe_child(folder, name)
        h = row["sha256"]
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h) or type(row["bytes"]) is not int or row["bytes"] < 0:
            raise ValueError("invalid evidence object")
    return doc, store


def locate(path):
    path = Path(path)
    for folder in path.parents:
        if (folder / INDEX).is_file():
            doc, store = index(folder)
            row = doc["files"].get(path.relative_to(folder).as_posix())
            return (row, store) if row else (None, None)
    return None, None


@contextmanager
def open_file(path):
    row, store = locate(path)
    if row is None:
        with ipc.open_shared(path, "rb") as stream:
            yield stream
    else:
        if Path(path).is_file():
            with Path(path).open("rb") as raw:
                if digest_stream(raw) != (row["sha256"], row["bytes"]):
                    raise ValueError("closed evidence changed during compaction")
        zip_path = resolve_object(store.parent, row["sha256"])
        verify_object_zip(zip_path, row["sha256"], row["bytes"])
        with zipfile.ZipFile(zip_path) as archive, archive.open("data") as stream:
            yield stream


def read_bytes(path):
    with open_file(path) as stream:
        return stream.read()


def exists(path):
    return locate(path)[0] is not None or Path(path).is_file()


def file_size(path):
    row, _ = locate(path)
    return row["bytes"] if row else Path(path).stat().st_size


def files(folder, pattern="*"):
    folder = Path(folder)
    if (folder / INDEX).is_file():
        doc, _ = index(folder)
        raw = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and p.name != INDEX}
        if raw - doc["files"].keys():
            raise ValueError("unindexed file in packed evidence")
        return sorted(safe_child(folder, n) for n in doc["files"] if fnmatch.fnmatchcase(n, pattern))
    return sorted(p for p in folder.rglob(pattern) if p.is_file())


def file_stat(path):
    row, _ = locate(path)
    return (SimpleNamespace(st_size=row["bytes"], st_mtime=row.get("mtime_ns", 0)/1e9)
            if row else Path(path).stat())


METADATA = {INDEX, MANIFEST, FACTS, MANIFEST + ".tmp"}


def walk_files(root):
    """Logical evidence census for archive/index tools, excluding physical storage metadata.

    A folder may mix an index (packed report.json / stdout) with raw siblings
    (g1_journal.jsonl). Indexed names come from the index; unindexed names and
    subdirectories stay visible.
    """
    root = Path(root)
    stores = {index(p.parent)[1] for p in root.rglob(INDEX)}
    result = []
    for folder, dirs, names in os.walk(root):
        folder = Path(folder)
        if folder.resolve() in stores:
            dirs[:] = []
            continue
        if INDEX in names:
            doc, _ = index(folder)
            indexed = doc["files"]
            result.extend(safe_child(folder, n) for n in indexed)
            result.extend(folder / n for n in names
                          if n not in METADATA and n != INDEX and n not in indexed)
        else:
            result.extend(folder / n for n in names if n not in METADATA)
    return sorted(result)


def hashes(folder):
    folder = Path(folder)
    return {str(p.relative_to(folder)): digest_file(p) for p in files(folder)}


def digest_file(path):
    with open_file(path) as stream:
        return digest_stream(stream)[0]


def put(source, store):
    store = Path(store)
    run = store.resolve().parent if store.name == OBJECTS_DIR else store.parent
    Path(run).mkdir(parents=True, exist_ok=True)
    with open_file(source) as stream:
        h, size = digest_stream(stream)
    kind = infer_kind(source, run)
    location = "local"
    if shared_objects_enabled():
        try:
            write_object_zip(shared_store_for(run), h, size, source)
            location = "shared"
        except (OSError, ValueError) as exc:
            record_fact(run, "shared_object_store_fallback", sha256=h, bytes=size, kind=kind,
                        error=type(exc).__name__, detail=str(exc)[:300])
            write_object_zip(store, h, size, source)
            location = "local"
    else:
        write_object_zip(store, h, size, source)
    row, _ = locate(source)
    mtime_ns = row.get("mtime_ns", 0) if row else Path(source).stat().st_mtime_ns
    try:
        # The manifest is advisory (the packed-tree indexes are the evidence identity); a second
        # process replacing it at the same moment must never fail a capture.
        record_manifest(run, h, size=size, kind=kind, location=location)
    except (OSError, ValueError) as exc:
        record_fact(run, "objects_manifest_write_failed", sha256=h, bytes=size, kind=kind,
                    error=type(exc).__name__, detail=str(exc)[:300])
    return dict(sha256=h, bytes=size, mtime_ns=mtime_ns)


def capture(source, destination, run, *, members=None, directories=None):
    source, destination, run = Path(source), Path(destination), Path(run).resolve()
    if not destination.resolve().is_relative_to(run) or source.resolve() == destination.resolve():
        raise ValueError("unsafe snapshot destination")
    if source.is_symlink() or (members is None and any(p.is_symlink() for p in source.rglob("*"))):
        raise ValueError("snapshot refuses symlinks")
    candidates = list(members) if members is not None else files(source)
    if any(p.is_symlink() or any(a.is_symlink() for a in p.parents if a.is_relative_to(source)) for p in candidates):
        raise ValueError("snapshot refuses symlinks")
    rows = {p.relative_to(source).as_posix(): put(p, run / "objects") for p in candidates}
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / INDEX).exists():
        raise ValueError("snapshot already exists")
    directories = (sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_dir())
                   if members is None else list(directories or ()))
    doc = dict(schema=SCHEMA, objects=os.path.relpath(run / "objects", destination), files=rows, directories=directories)
    temp = destination / (INDEX + ".tmp")
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(doc, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, destination / INDEX)
    return doc


def compact(folder, run):
    """Publish/validate index before unlinking closed copies. Interrupted cleanup is readable."""
    folder = Path(folder)
    if not folder.exists():
        return
    if not (folder / INDEX).exists():
        snapshot = folder.with_name(folder.name + ".packing")
        capture(folder, snapshot, run)
        os.replace(snapshot / INDEX, folder / INDEX)
        snapshot.rmdir()
    doc, _ = index(folder)
    for name, row in doc["files"].items():
        path = safe_child(folder, name)
        digest_file(path)  # validate object, including resumed cleanup
        if path.is_file():
            with path.open("rb") as stream:
                if digest_stream(stream) != (row["sha256"], row["bytes"]):
                    raise ValueError("closed evidence changed during compaction")
            path.unlink()


def closed_logs_enabled():
    return os.environ.get(CLOSED_LOGS_ENV, "1").strip().lower() not in {"0", "false", "off", "no"}


def pack_closed_file(path, run):
    """Pack one closed file through put+index. Failure leaves the raw file and records a fact."""
    path, run = Path(path), Path(run)
    if not path.is_file() or not closed_logs_enabled():
        return
    try:
        _pack_file(path, run)
    except (OSError, ValueError) as exc:
        record_fact(run, "closed_log_pack_failed", path=str(path),
                    error=type(exc).__name__, detail=str(exc)[:300])


def pack_session_logs(run):
    """After games have exited: pack per-day report.json and farmhand-stdout-*.raw."""
    run = Path(run)
    for path in sorted(run.glob("runtime/live7/*/day-*/report.json")):
        pack_closed_file(path, run)
    for path in sorted(run.glob("runtime/farmhand-stdout-*.raw")):
        pack_closed_file(path, run)


def _pack_file(path, run):
    path, run = Path(path), Path(run).resolve()
    folder = path.parent
    name = path.name
    row = put(path, run / OBJECTS_DIR)
    # Verify the raw file against the stored object BEFORE the index names it: an index row beside a
    # different raw file would make every later read refuse. A file that changed stays raw (fact).
    with path.open("rb") as stream:
        if digest_stream(stream) != (row["sha256"], row["bytes"]):
            raise ValueError("closed evidence changed during compaction")
    if (folder / INDEX).is_file():
        doc, _ = index(folder)
    else:
        doc = dict(schema=SCHEMA, objects=os.path.relpath(run / OBJECTS_DIR, folder),
                   files={}, directories=[])
    doc["files"][name] = dict(sha256=row["sha256"], bytes=row["bytes"], mtime_ns=row["mtime_ns"])
    temp = folder / (INDEX + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(doc, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, folder / INDEX)
    path.unlink()


def materialize(source, destination, *, roots=None):
    """Write independent copies only, validating all inputs before the first output."""
    source, destination = Path(source), Path(destination)
    doc = index(source)[0] if (source / INDEX).is_file() else None
    if roots is None:
        paths = files(source)
    elif doc is not None:
        paths = [safe_child(source, name) for name in doc["files"]
                 if name.split('/')[0] in roots]
    else:
        paths = []
        for name in roots:
            child = safe_child(source, name)
            paths.extend([child] if child.is_file() else files(child))
    for p in paths:
        digest_file(p)
    if doc is not None:
        for name in doc.get("directories", []):
            if roots is None or name.split('/')[0] in roots:
                safe_child(destination, name).mkdir(parents=True, exist_ok=True)
    for p in paths:
        target = safe_child(destination, p.relative_to(source).as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        with open_file(p) as stream, target.open("wb") as out:
            shutil.copyfileobj(stream, out, 1024 * 1024)
        row, _ = locate(p)
        if row and row.get("mtime_ns"):
            os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
        elif not row:
            shutil.copystat(p, target)


def tree_bytes(root):
    total = 0
    for path in Path(root).rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # a writer atomically published/deleted a temporary; disk_usage covers the volume
    return total


def _is_link(path):
    """A symlink or a Windows junction. Path.is_junction needs Python 3.12; the live-path environment is 3.11,
    so there any reparse point counts as a link (the same refusal, never a weaker one)."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        return is_junction()
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def save_members(saves, names):
    """Enumerate installed names only; reject links before descending (ADR 173)."""
    saves = Path(saves)
    members, directories = [], []
    def visit(path):
        if _is_link(path):
            raise ValueError('owned save refuses links: ' + path.name)
        if path.is_dir():
            directories.append(path.relative_to(saves).as_posix())
            for child in path.iterdir():
                visit(child)
        elif path.is_file():
            members.append(path)
    for name in names:
        if not name or name in ('.', '..') or any(c in name for c in '/\\:'):
            raise ValueError('invalid owned save folder name')
        visit(saves / name)
    return members, directories


def restore_bytes(saves, native, *, owned_saves=()):
    """Scoped original-state estimate; restoration needs independent writable copies."""
    native = Path(native)
    return (sum(p.stat().st_size for p in save_members(saves, owned_saves)[0])
            + tree_bytes(native / "command-settled-journal-v1.json.segments")
            + tree_bytes(native / "command-settled-journal-v1.json.archive")
            + sum(p.stat().st_size for p in native.glob("command-settled-journal-v1.json*") if p.is_file()))


def admission(volume, hours, original_bytes=0):
    return projection(shutil.disk_usage(volume).free, hours,
                      preparation=max(512 * 1024**2, math.ceil(original_bytes * 1.01)),
                      reserve=original_bytes + 512 * 1024**2)


def projection(free, hours, *, rate=MEASURED_BYTES_PER_HOUR, preparation=512 * 1024**2, reserve=512 * 1024**2):
    if any(not math.isfinite(v) or v < 0 for v in (free, hours, rate, preparation, reserve)) or rate == 0:
        raise ValueError("invalid disk projection")
    # Margin covers bursts, final preservation and activity outside this run.
    required = math.ceil(rate * 1.25 * hours + preparation + reserve)
    return dict(free_bytes=free, floor_bytes=FLOOR, bytes_per_hour=rate, margin=1.25,
                hours=hours, preparation_bytes=preparation, restore_reserve_bytes=reserve,
                required_bytes=required, projected_free_bytes=free-required,
                allowed=free-required >= FLOOR)


class Monitor:
    """Post-ARM run growth and sustained volume pressure; see ADR 123."""

    def __init__(self, run, volume, hours, *, reserve=512 * 1024**2, clock=time.monotonic, armed_at=None):
        if any(not math.isfinite(v) or v < 0 for v in (hours, reserve)):
            raise ValueError("invalid disk monitor budget")
        self.run, self.volume, self.hours, self.clock = Path(run), volume, hours, clock
        self.start = clock() if armed_at is None else armed_at
        self.last = None
        self.reserve = reserve
        self.rate = 0
        self.samples = deque()
        self.growth = 0
        self.hold_start = None
        self.hold_finished = False
        self.transient_holds = 0

    def sample(self):
        now = self.clock()
        elapsed = now-self.start
        # Free space is always fresh (the caller samples every second), including
        # warm-up and the intervals between the more expensive run censuses.
        free = shutil.disk_usage(self.volume).free
        if self.last is None or now-self.last >= 60:
            self.written = tree_bytes(self.run)
            self.provider_bytes = self._provider_bytes()
            if elapsed >= WARMUP_S:
                if self.samples:
                    # Packing/deletion must not cancel later growth. Count only
                    # positive run-local deltas; volume loss never estimates rate.
                    self.growth += max(0, self.written-self.samples[-1][1])
                self.samples.append((now, self.written, self.growth))
                while len(self.samples) > 1 and self.samples[1][0] <= now-RATE_WINDOW_S:
                    self.samples.popleft()
            self.last = now
        span = self.samples[-1][0]-self.samples[0][0] if self.samples else 0
        growth = self.samples[-1][2]-self.samples[0][2] if self.samples else 0
        ready = span >= RATE_WINDOW_S
        self.rate = growth*3600/span if ready else 0
        floor = FLOOR + self.reserve
        hard_floor = HARD_FLOOR + self.reserve
        headroom = free-floor
        eta = max(0, headroom)*3600/(self.rate*1.25) if self.rate else None
        near = headroom <= NEAR_FLOOR_BYTES
        high_growth = self.rate >= HIGH_GROWTH_BYTES_PER_HOUR
        hold_seconds = now-self.hold_start if self.hold_start is not None else 0
        reasons = []
        if free < hard_floor:
            reasons.append("hard_floor")
        else:
            if near and (high_growth or (self.hold_start is not None and hold_seconds >= TRANSIENT_HOLD_S)):
                reasons.append("near_floor")
            # A host drop below the reserve floor gives eta=0 even at a53's tiny
            # run rate. Only high own growth bypasses the hold in the near band.
            # Outside that band the existing short-horizon projection still stops.
            if eta is not None and eta <= STOP_HORIZON_S and (not near or high_growth):
                reasons.append("projected_floor")
        if self.provider_bytes > PROVIDER_LIMIT:
            reasons.append("provider_budget")
        transition = None
        if self.hold_start is not None:
            if not near:
                if not self.hold_finished:
                    transition = "recovered"
                self.hold_start = None
                self.hold_finished = False
            elif reasons and not self.hold_finished:
                transition = "stopped"
                self.hold_finished = True
        elif near and not reasons:
            self.hold_start = now
            self.transient_holds += 1
            hold_seconds = 0
            transition = "started"
        decision = (reasons[0] if reasons else "disk_transient_hold" if near else "warmup" if elapsed < WARMUP_S else
                    "collecting_window" if not ready else "no_growth" if not self.rate else "headroom")
        self.result = dict(
            schema="arti.free_play.disk_monitor/3", utc=datetime.now(timezone.utc).isoformat(),
            allowed=not reasons, decision=decision, reasons=reasons, elapsed_s=elapsed,
            free_bytes=free, floor_bytes=FLOOR, restore_reserve_bytes=self.reserve,
            reserve_floor_bytes=floor, headroom_bytes=headroom, near_floor_bytes=NEAR_FLOOR_BYTES,
            hard_floor_bytes=hard_floor, hold_seconds=hold_seconds, transient_hold_s=TRANSIENT_HOLD_S,
            transient_holds=self.transient_holds, high_growth_bytes_per_hour=HIGH_GROWTH_BYTES_PER_HOUR,
            bytes_per_hour=self.rate, rate_source="post_arm_run_growth", margin=1.25,
            warmup_s=WARMUP_S, minimum_window_s=RATE_WINDOW_S, window_s=span,
            window_growth_bytes=growth, window_samples=len(self.samples),
            time_to_floor_s=eta, stop_horizon_s=STOP_HORIZON_S,
            hours=max(0, self.hours-elapsed/3600), run_bytes=self.written,
            census_age_s=now-self.last, provider_bytes=self.provider_bytes,
            provider_limit_bytes=PROVIDER_LIMIT)
        if transition is not None:
            # One event per transition, on the regular sample row; gameplay continues.
            self.result.update(event="disk_transient_hold", transition=transition,
                               recovered=transition == "recovered")
        # Log every decision, even those reusing the latest scoped census.
        ipc.append_text(self.run / "disk-projection.jsonl", json.dumps(self.result, sort_keys=True, allow_nan=False) + "\n")
        return self.result

    def _provider_bytes(self):
        provider_bytes = 0
        provider_objects = set()
        for folder in self.run.glob("runtime/live7/*/day-*/journal/provider_archive"):
            provider_bytes += tree_bytes(folder)
            if (folder / INDEX).exists():
                doc, _ = index(folder)
                provider_objects.update(resolve_object(self.run, row["sha256"]) for row in doc["files"].values())
        provider_bytes += sum(p.stat().st_size for p in provider_objects)
        return provider_bytes


def referenced_hashes(run):
    """Union of manifest rows and every packed-tree index in the run."""
    run = Path(run)
    found = {}
    manifest_path = run / MANIFEST
    if manifest_path.is_file():
        for sha256, row in read_manifest(run)["objects"].items():
            found[sha256] = int(row["bytes"])
    for path in run.rglob(INDEX):
        doc, _ = index(path.parent)
        for row in doc["files"].values():
            found[row["sha256"]] = row["bytes"]
    return found


def export_objects(run, destination=None):
    """Copy referenced object zips into destination/objects (default: this run) and verify."""
    run = Path(run).resolve()
    dest_run = Path(destination).resolve() if destination is not None else run
    dest_store = dest_run / OBJECTS_DIR
    dest_store.mkdir(parents=True, exist_ok=True)
    hashes = referenced_hashes(run)
    for sha256, size in hashes.items():
        src = resolve_object(run, sha256)
        verify_object_zip(src, sha256, size)
        target = dest_store / object_zip_name(sha256)
        if target.is_file() and target.resolve() == src.resolve():
            verify_object_zip(target, sha256, size)
            continue
        fd, temp = tempfile.mkstemp(prefix=".object-", suffix=".zip", dir=dest_store)
        os.close(fd)
        temp = Path(temp)
        try:
            shutil.copyfile(src, temp)
            verify_object_zip(temp, sha256, size)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        verify_object_zip(target, sha256, size)
    return dict(objects=len(hashes), destination=str(dest_store), run=str(run))


def object_zip_digest(path):
    with zipfile.ZipFile(path) as archive:
        if archive.namelist() != ["data"]:
            raise ValueError("invalid evidence object members")
        with archive.open("data") as stream:
            return digest_stream(stream)


def object_name_hash(path):
    name = Path(path).name
    if not name.endswith(".zip"):
        return None
    stem = name[:-4]
    if len(stem) != 64 or any(c not in "0123456789abcdef" for c in stem):
        return None
    return stem


def publish_shared_zip(store, sha256, size, source, *, replace_corrupt=False):
    """Install an already-formed object zip into the shared store. Never unlinks source."""
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    target = store / object_zip_name(sha256)
    if target.is_file() and not replace_corrupt:
        verify_object_zip(target, sha256, size)
        return dict(status="present")
    if target.is_file() and replace_corrupt:
        try:
            verify_object_zip(target, sha256, size)
            return dict(status="present")
        except ValueError:
            pass  # same hash: replace the corrupt zip with this verified copy
    fd, temp = tempfile.mkstemp(prefix=".object-", suffix=".zip", dir=store)
    os.close(fd)
    temp = Path(temp)
    try:
        shutil.copyfile(source, temp)
        verify_object_zip(temp, sha256, size)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    verify_object_zip(target, sha256, size)
    return dict(status="installed")


_RUN_STAMP = re.compile(r"(\d{8}T\d{6}Z)")


def run_stamp(name):
    match = _RUN_STAMP.search(name)
    return match.group(1) if match else None


def run_order_key(name):
    stamp = run_stamp(name)
    if stamp:
        return stamp
    day = re.search(r"(\d{8})$", name)
    if day:
        return day.group(1) + "T000000Z"
    return "99999999T999999Z"


def runs_for_dedupe(root, exclude_from=None):
    """Run folders under root, oldest name first.

    --exclude-from skips that folder and any run whose compact UTC stamp is at
    or after that name's stamp (so a Standard cutoff does not drop earlier
    Witness/livepath folders whose names sort after `free-play-standard-`).
    Folders with no stamp stay eligible. Without a stamp in the cutoff, skip
    names at or after it lexicographically.
    """
    root = Path(root)
    names = sorted(
        (p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda n: (run_order_key(n), n),
    )
    if exclude_from:
        cutoff = run_stamp(exclude_from)
        kept = []
        for name in names:
            if name == exclude_from:
                continue
            stamp = run_stamp(name)
            if cutoff:
                if stamp is not None and stamp >= cutoff:
                    continue
            elif name >= exclude_from:
                continue
            kept.append(name)
        names = kept
    return [root / n for n in names]


def _cached_verify(path, sha256, size, cache):
    path = Path(path)
    st = path.stat()
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns, sha256, size)
    cached = cache.get(key)
    if cached is True:
        return
    if isinstance(cached, str):
        raise ValueError(cached)
    try:
        verify_object_zip(path, sha256, size)
        cache[key] = True
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        cache[key] = type(exc).__name__ + ": " + str(exc)[:200]
        raise


def dedupe_run(run, *, dry_run=False, adopted=None, verified=None):
    """Move run-local objects/<sha>.zip into the shared store; one verified copy. Interrupt-safe.

    `adopted` is the set of hashes this invocation has already installed (or
    counted as installed) so a --root dry-run matches a real pass. `verified`
    caches shared-zip hash checks so the same object is not decompressed once
    per run.
    """
    run = Path(run).resolve()
    folder = run / OBJECTS_DIR
    if adopted is None:
        adopted = set()
    if verified is None:
        verified = {}
    result = dict(run=str(run), moved=0, removed=0, kept=0, promoted=0,
                  bytes_moved=0, bytes_removed=0, bytes_kept=0, kept_reasons=[], ok=True)
    if not folder.is_dir():
        if not dry_run:
            record_fact(run, "objects_deduped", moved=0, removed=0, kept=0, promoted=0,
                        bytes_moved=0, bytes_removed=0, bytes_kept=0)
        return result
    for path in sorted(folder.glob("*.zip")):
        action, size, reason = _dedupe_object(run, path, dry_run=dry_run,
                                              adopted=adopted, verified=verified)
        if action == "moved":
            result["moved"] += 1
            result["bytes_moved"] += size
        elif action == "removed":
            result["removed"] += 1
            result["bytes_removed"] += size
        elif action == "promoted":
            result["promoted"] += 1
            result["bytes_moved"] += size
        else:
            result["kept"] += 1
            result["bytes_kept"] += size
            result["kept_reasons"].append(dict(file=path.name, reason=reason, bytes=size))
    if not dry_run:
        record_fact(run, "objects_deduped", moved=result["moved"], removed=result["removed"],
                    kept=result["kept"], promoted=result["promoted"],
                    bytes_moved=result["bytes_moved"], bytes_removed=result["bytes_removed"],
                    bytes_kept=result["bytes_kept"])
    return result


def _dedupe_object(run, path, *, dry_run, adopted=None, verified=None):
    path = Path(path)
    if adopted is None:
        adopted = set()
    if verified is None:
        verified = {}
    size = path.stat().st_size if path.is_file() else 0
    sha256 = object_name_hash(path)
    if sha256 is None:
        return "kept", size, "invalid object name"
    try:
        digest, payload = object_zip_digest(path)
        verify_object_zip(path, sha256, payload)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return "kept", size, type(exc).__name__ + ": " + str(exc)[:200]
    if digest != sha256:
        return "kept", size, "content hash does not match filename"
    shared = shared_store_for(run)
    target = shared / object_zip_name(sha256)
    if dry_run:
        if sha256 in adopted:
            return "removed", size, ""
        if target.is_file():
            try:
                _cached_verify(target, sha256, payload, verified)
                adopted.add(sha256)
                return "removed", size, ""
            except (OSError, ValueError, zipfile.BadZipFile):
                adopted.add(sha256)
                return "promoted", size, "shared copy would be replaced"
        adopted.add(sha256)
        return "moved", size, ""
    try:
        if sha256 in adopted and target.is_file():
            _cached_verify(target, sha256, payload, verified)
            action = "removed"
        elif target.is_file():
            try:
                _cached_verify(target, sha256, payload, verified)
                action = "removed"
            except ValueError:
                publish_shared_zip(shared, sha256, payload, path, replace_corrupt=True)
                action = "promoted"
        else:
            publish_shared_zip(shared, sha256, payload, path)
            action = "moved"
        adopted.add(sha256)
        kind = "other"
        if (run / MANIFEST).is_file():
            kind = read_manifest(run).get("objects", {}).get(sha256, {}).get("kind") or "other"
        record_manifest(run, sha256, size=payload, kind=kind, location="shared")
        try:
            path.chmod(stat.S_IWRITE)
        except OSError:
            pass
        path.unlink()
        return action, size, ""
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return "kept", size, type(exc).__name__ + ": " + str(exc)[:200]


def verify_run(run):
    """Confirm every referenced object resolves and hashes; leftover local zips are listed."""
    run = Path(run).resolve()
    errors = []
    hashes = referenced_hashes(run)
    for sha256, size in hashes.items():
        try:
            src = resolve_object(run, sha256)
            verify_object_zip(src, sha256, size)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            errors.append(dict(sha256=sha256, error=type(exc).__name__, detail=str(exc)[:300]))
    leftovers = []
    folder = run / OBJECTS_DIR
    if folder.is_dir():
        for path in sorted(folder.glob("*.zip")):
            leftovers.append(dict(file=path.name, bytes=path.stat().st_size))
    return dict(run=str(run), ok=not errors, objects=len(hashes), errors=errors,
                leftover_local=len(leftovers), leftovers=leftovers)


def compact_day(run, cycle, day):
    """Called only after saved_day_outcome, with its runner exited and checkpoint verified."""
    if day.get("status") != "ended" or day.get("outcome") not in {"verified", "lost"}:
        raise ValueError("only a verified saved day may be compacted")
    checkpoint = Path(day["checkpoint"])
    if not checkpoint.resolve().is_relative_to(Path(cycle).resolve() / "checkpoints"):
        raise ValueError("checkpoint outside owned cycle")
    receipt = json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8"))
    if (receipt.get("status") != "copied" or receipt.get("copy_state") != "after_night_save"
            or hashes(checkpoint / "save") != receipt.get("files")):
        raise ValueError("checkpoint bytes lack verified night-save receipt")
    compact(checkpoint / "save", run)
    # The live bridge may still read this day's final provider pair. Preserve
    # all provider filenames until bridge/game shutdown; Monitor bounds hot bytes.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("preflight")
    gate.add_argument("--root", type=Path, required=True)
    gate.add_argument("--hours", type=parse_duration, required=True,
                      help="duration: Nh, N.Nh, Nm, or XhYm")
    gate.add_argument("--spec", type=Path, help="read only rig paths to reserve original-state restore space")
    gate.add_argument("--farm", choices=("Standard", "Wilderness"), default="Standard")
    gate.add_argument("--continue-from")
    gate.add_argument("--checkpoint")
    unpack = commands.add_parser("materialize")
    unpack.add_argument("source", type=Path)
    unpack.add_argument("destination", type=Path)
    exported = commands.add_parser("export")
    exported.add_argument("run", type=Path)
    exported.add_argument("--destination", type=Path,
                          help="folder that receives objects/; default is the run itself")
    show = commands.add_parser("cat", help="stream one packed or raw evidence file to stdout")
    show.add_argument("path", type=Path)
    adopt = commands.add_parser("dedupe", help="move run-local objects/ zips into the shared store")
    adopt.add_argument("runs", nargs="*", type=Path)
    adopt.add_argument("--root", type=Path, help="free-play-runs (or mabar-runs) folder")
    adopt.add_argument("--exclude-from", help="skip this run folder name and any later name")
    adopt.add_argument("--dry-run", action="store_true")
    adopt.add_argument("--verify", action="store_true", help="check manifests after dedupe; do not move")
    args = parser.parse_args()
    if args.command == "materialize":
        if args.destination.exists():
            raise ValueError("materialize destination must be new")
        materialize(args.source, args.destination)
        return
    if args.command == "export":
        print(json.dumps(export_objects(args.run, args.destination), sort_keys=True))
        return
    if args.command == "cat":
        sys.stdout.buffer.write(read_bytes(args.path))
        return
    if args.command == "dedupe":
        if args.runs:
            targets = [p.resolve() for p in args.runs]
        elif args.root:
            targets = runs_for_dedupe(args.root, args.exclude_from)
        else:
            raise SystemExit("dedupe requires --root or at least one run")
        rows = []
        failed = False
        adopted = set()
        verified = {}
        for run in targets:
            if args.verify:
                row = verify_run(run)
                print(json.dumps(row, sort_keys=True))
                rows.append(row)
                if not row["ok"]:
                    failed = True
                    break
            else:
                row = dedupe_run(run, dry_run=args.dry_run, adopted=adopted, verified=verified)
                print(json.dumps(row, sort_keys=True))
                rows.append(row)
        summary = dict(
            command="verify" if args.verify else "dedupe",
            dry_run=bool(args.dry_run and not args.verify),
            runs=len(rows),
            moved=sum(r.get("moved", 0) for r in rows),
            removed=sum(r.get("removed", 0) for r in rows),
            kept=sum(r.get("kept", 0) for r in rows),
            promoted=sum(r.get("promoted", 0) for r in rows),
            bytes_moved=sum(r.get("bytes_moved", 0) for r in rows),
            bytes_removed=sum(r.get("bytes_removed", 0) for r in rows),
            bytes_kept=sum(r.get("bytes_kept", 0) for r in rows),
            objects=sum(r.get("objects", 0) for r in rows),
            ok=not failed,
        )
        print(json.dumps(summary, sort_keys=True))
        if failed:
            raise SystemExit(1)
        return
    root = args.root.resolve()
    while not root.exists():
        root = root.parent
    original_bytes = 0
    if args.spec:
        spec = json.loads(args.spec.read_text(encoding="utf-8-sig"))
        if args.continue_from:
            from scripts.free_play.carryover import inspect_candidate
            from scripts.free_play.savestate import resolve_source
            owned = (inspect_candidate(resolve_source(args.continue_from), args.farm, args.checkpoint)['save_folder'],)
        else:
            owned = (Path(spec['templates'][args.farm]).resolve().name,)
        if spec.get("rigConfig"):
            rig = json.loads(Path(spec["rigConfig"]).read_text(encoding="utf-8-sig"))
            original_bytes = restore_bytes(Path(rig["appData"]) / "Saves",
                                           Path(os.environ["LOCALAPPDATA"]) / "ArtiStardewBridge", owned_saves=owned)
        else:
            original_bytes = restore_bytes(Path(os.environ["APPDATA"]) / "StardewValley/Saves",
                                           Path(os.environ["LOCALAPPDATA"]) / "ArtiStardewBridge", owned_saves=owned)
    result = admission(root, args.hours, original_bytes)
    print(json.dumps(result, sort_keys=True))
    if not result["allowed"]:
        raise SystemExit("disk projection would cross 10 GiB; session refused before dispatch")


if __name__ == "__main__":
    main()
