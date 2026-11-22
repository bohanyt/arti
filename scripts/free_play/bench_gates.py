"""The bench's own admission gates and its scoped hold on the rig's resting STOP marker (ADR 242).

Each gate returns one row: its name, whether it passed and what it saw. The bench runs every gate, prints one
line per refusal and starts nothing while any gate refuses. Reading the rig is read-only; the only writes here are
the bench's own slot HOLD (bench.slot) and the STOP marker it releases and restores byte for byte.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from arti_runtime import ipc

DISK_FLOOR_GIB = 16.5
COMMIT_CEILING_PCT = 75.0
GAME = {"stardew valley.exe", "stardewmoddingapi.exe"}
STREAM = re.compile(r"(?i)^(obs(32|64)?\.exe|vtube studio\.exe|vtubestudio\.exe)$")
TESTS = {"pytest.exe", "py.test.exe", "testhost.exe", "vstest.console.exe", "msbuild.exe"}
TEST_COMMAND = re.compile(r"(?i)(?:\bpytest\b|\bpy\.test\b|\bvstest\b|\btesthost\b|"
                          r"\bdotnet(?:\.exe)?[\"']?\s+(?:test|build|run|msbuild)\b)")


@dataclass(frozen=True)
class Gate:
    name: str
    ok: bool
    saw: str

    def line(self):
        return f"GATE {self.name} {'PASS' if self.ok else 'REFUSED'}: {self.saw}"


def census():
    """One process read for every process gate: [(name, command line)]."""
    from scripts.free_play import soak
    rows = json.loads(soak.ps("Get-CimInstance Win32_Process -ErrorAction Stop | "
                              "Select-Object Name,CommandLine | ConvertTo-Json -Compress") or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    if not rows or any(not isinstance(r, dict) or not r.get("Name") for r in rows):
        raise RuntimeError("process census unavailable")
    return [(r["Name"].casefold(), r.get("CommandLine") or "") for r in rows]


def process_gates(processes, *, own_pids=()):
    games = sorted({n for n, _ in processes if n in GAME})
    stream = sorted({n for n, _ in processes if STREAM.match(n)})
    tests = sorted({n if n in TESTS else f"{n} ({c[:80]})" for n, c in processes
                    if n in TESTS or (n in {"python.exe", "pythonw.exe", "py.exe", "dotnet.exe"}
                                      and TEST_COMMAND.search(c))})
    return [Gate("no-game", not games, ", ".join(games) or "no Stardew process"),
            Gate("no-obs-vtube", not stream, ", ".join(stream) or "no OBS or VTube Studio"),
            Gate("no-pytest-dotnet", not tests, "; ".join(tests) or "no pytest, dotnet test/build or testhost")]


def owner_idle_gate(cfg, run=subprocess.run):
    script = cfg.get("ownerIdleScript")
    if not script:
        return Gate("owner-idle", True, "no ownerIdleScript in the rig config")
    child = run(["powershell", "-NoProfile", "-File", script], capture_output=True, text=True)
    words = " ".join((child.stdout + " " + child.stderr).split())[:300]
    return Gate("owner-idle", child.returncode == 0, words or f"exit {child.returncode}")


def disk_gate(drive="C:/", usage=shutil.disk_usage):
    free = usage(drive).free / 1024**3
    return Gate("disk", free >= DISK_FLOOR_GIB, f"C: {free:.2f} GiB free (floor {DISK_FLOOR_GIB})")


def commit_percent():
    """Memory commit in use, the same figure as \\Memory\\% Committed Bytes In Use."""
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong),
                    ("avail_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                    ("avail_virtual", ctypes.c_ulonglong), ("avail_extended", ctypes.c_ulonglong)]
    status = Status(length=ctypes.sizeof(Status))
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) or not status.total_page:
        raise OSError("GlobalMemoryStatusEx failed")
    return 100.0 * (status.total_page - status.avail_page) / status.total_page


def memory_gate(read=commit_percent):
    try:
        pct = read()
    except OSError as exc:
        return Gate("memory", False, f"commit unreadable ({exc})")
    return Gate("memory", pct < COMMIT_CEILING_PCT, f"commit {pct:.1f}% (ceiling {COMMIT_CEILING_PCT:.0f}%)")


def slot_gate(hold, *, own=None):
    if not hold.is_file():
        return Gate("slot", True, f"{hold.name} absent")
    data = hold.read_bytes()
    if own is not None and data == own:
        return Gate("slot", True, "held by this bench run")
    first = data.decode("utf-8", "replace").splitlines()[:1]
    return Gate("slot", False, f"{hold} held by another worker: {first[0][:120] if first else 'empty'}")


def lock_gates(rig, *, stop_released=False):
    rows = []
    for name, path in (("free-play-lock", rig / "rig/free-play.lock"), ("witness-lock", rig / "rig/witness.lock"),
                       ("soak-stop", rig / "operator-staging/run/SOAK_STOP")):
        rows.append(Gate(name, not path.exists(), f"{path} {'present' if path.exists() else 'absent'}"))
    stop = rig / "STOP"
    if not stop.exists():
        rows.append(Gate("rig-stop", True, "no resting STOP marker" if not stop_released
                         else "resting STOP released by this bench run"))
    elif stop_released:
        rows.append(Gate("rig-stop", False, "a new STOP appeared after the bench released the resting one"))
    else:
        rows.append(Gate("rig-stop", True, f"resting STOP sha256 {sha256(stop)[:12]}; the bench releases it "
                                           "for this run and restores it byte for byte"))
    return rows


def sha256(path):
    return hashlib.sha256(ipc.read_bytes(Path(path))).hexdigest()


class RestingStop:
    """The bench owns the rig's resting STOP marker for one run: released after admission, restored on every exit.

    The bytes and their hash live in the run folder (STOP.before, stop-marker.json). Restoring never overwrites a
    STOP that someone else wrote meanwhile; it records that and keeps both.
    """

    def __init__(self, rig, out):
        self.marker = Path(rig) / "STOP"
        self.out = Path(out)
        self.copy = self.out / "STOP.before"
        self.record = self.out / "stop-marker.json"

    def _write(self, doc):
        ipc.publish(self.record, json.dumps(doc, indent=2) + "\n", fsync=True)

    def state(self):
        try:
            return json.loads(ipc.read_text(self.record))
        except (OSError, ValueError):
            return {}

    @property
    def released(self):
        doc = self.state()
        return doc.get("released") is True and doc.get("restored") is not True

    def release(self):
        if self.state().get("released"):
            return self.state()
        if not self.marker.exists():
            doc = dict(released=False, present=False)
            self._write(doc)
            return doc
        data = ipc.read_bytes(self.marker)
        digest = hashlib.sha256(data).hexdigest()
        with self.copy.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if sha256(self.copy) != digest:
            raise RuntimeError("STOP copy hash mismatch; resting marker left in place")
        doc = dict(released=True, present=True, sha256=digest, bytes=len(data), path=str(self.marker))
        self._write(doc)  # recorded before the marker goes, so every exit path can restore it
        self.marker.unlink()
        return doc

    def restore(self):
        doc = self.state()
        if doc.get("released") is not True or doc.get("restored") is True:
            return doc
        digest = doc["sha256"]
        if sha256(self.copy) != digest:
            raise RuntimeError("bench STOP.before changed; restore the resting marker by hand")
        if self.marker.exists():
            doc.update(restored=True, outcome="kept_existing", existing_sha256=sha256(self.marker),
                       identical=sha256(self.marker) == digest)
        else:
            try:
                with self.marker.open("xb") as stream:
                    stream.write(ipc.read_bytes(self.copy))
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass  # written by someone else between the check and the create; theirs is kept
            doc.update(restored=True, identical=sha256(self.marker) == digest,
                       outcome="restored" if sha256(self.marker) == digest else "kept_existing")
        self._write(doc)
        if doc["outcome"] == "restored" and not doc["identical"]:
            raise RuntimeError("restored STOP hash mismatch")
        return doc
