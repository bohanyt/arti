"""Isolated game-workstation rehearsal. No import-time effects; no runtime patching or port monitor.

Run from an interactive operator task (the SSH session itself is not a desktop).
All control is through the existing witness/live_cycle/pad_watch procedures.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
from scripts.free_play import cotenant, storage, farm_memory
from scripts.free_play.native_safety import NativeSafety, SavedCounters, counter_from_save
from scripts.free_play.duration import parse_duration
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

if __name__ == "__main__" and not __package__:
    from file_io import read_json, snapshot_exists, snapshot_stat
else:
    from scripts.free_play.file_io import read_json, snapshot_exists, snapshot_stat

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # direct script entry also needs the existing P2 tool
FAMILIES = ("movement", "tools_world", "inventory_economy", "menus_progression", "fishing", "combat", "multiplayer_events", "text")
SKILLS = {"jalan": "movement", "jelajah": "movement", "pindah": "movement", "tebang": "tools_world", "pecah_batu": "tools_world", "cangkul": "tools_world", "siram": "tools_world", "tanam": "tools_world", "panen": "tools_world", "bersihkan": "tools_world", "isi_air": "tools_world", "simpan": "inventory_economy", "ambil": "inventory_economy", "kirim": "inventory_economy", "jual": "inventory_economy", "beli": "inventory_economy", "craft": "inventory_economy", "mancing": "fishing", "pancing": "fishing", "lawan": "combat", "bertahan": "combat", "tidur": "menus_progression", "cek_pagi": "menus_progression", "bicara": "multiplayer_events"}
SKILLS.update(dict.fromkeys(("pergi_ke","jalan_ke"),"movement"))
SKILLS.update(dict.fromkeys(("isi_gembor","rawat_hewan","elus","pungut_drop","makan","pasang","pohon_buah","mesin","forage","gali_artefak","tambang_bijih","crab_pot"),"tools_world"))
SKILLS.update(dict.fromkeys(("simpan_ke_chest","buat","masak","upgrade_alat","beli_hewan","donasi"),"inventory_economy"))
SKILLS.update({"tangkap_ikan":"fishing","siapkan_pancing":"fishing","lawan_mine":"combat","ikuti_festival":"multiplayer_events","ngobrol_hadiah":"multiplayer_events","surat_quest":"menus_progression"})

def utc():
    return datetime.now(timezone.utc).isoformat()


def saved_day_outcome(day):
    """Native saved lost nights continue the campaign, with zero clean-night credit."""
    if day.get("status") != "ended" or not day.get("checkpoint") or day.get("outcome") not in ("verified", "lost"):
        raise RuntimeError("day did not reach a verified native save/checkpoint")
    return day["outcome"]

def read(path):
    return read_json(path)

def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    ipc.write_json(path, value)


HELPER_GIFT_WHEN = "idle"  # fix54K / ADR 74: free play gifts only while ARTI is idle
WITNESS_KINDS = ("bundle", "museum", "geode")  # ADR 132
WITNESS_MAX_HOURS = 0.5


def helper_started_preflight(host_out, *, gift_when=HELPER_GIFT_WHEN, max_rows=64):
    """fix55C / ADR 78 (REVIEW2 #7): the deployed HOST helper must report the staged GiftWhen in its first helper_started
    row. An old helper DLL drops the unknown key and keeps gifting while ARTI moves; the SHA alone cannot tell.
    Returns the row's facts; raises (fail closed, named reason) otherwise."""
    row = None
    try:
        with ipc.open_shared(Path(host_out) / "host.jsonl", "r", encoding="utf-8") as f:
            for _, line in zip(range(max_rows), f):
                try:
                    candidate = json.loads(line)
                except ValueError:
                    continue
                if isinstance(candidate, dict) and candidate.get("ev") == "helper_started":
                    row = candidate
                    break
    except OSError:
        row = None
    if row is None:
        raise RuntimeError("helper preflight: helper_started_missing")
    facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
    config = facts.get("config") if isinstance(facts.get("config"), dict) else {}
    if config.get("GiftWhen") != gift_when:
        raise RuntimeError("helper preflight: helper_gift_when_not_%s (reported %r)" % (gift_when, config.get("GiftWhen")))
    return dict(gift_when=config.get("GiftWhen"), gift_idle_seconds=config.get("GiftIdleSeconds"), dll=facts.get("dll"))


def startup_pause(output, run_id, *, release=False, timeout_s=20, clock=time.monotonic, sleep=time.sleep):
    """Observe this HOST's bounded native pause, or release it once before native readiness.

    ADR 65. No game port/input; only the isolated helper's existing file transport.
    """
    output = Path(output)
    if release:
        startup_pause(output, run_id, clock=clock, sleep=sleep)
        # ADR 242: one writer, once. Created, never replaced; the HOST helper alone consumes it.
        ipc.publish(output / "STARTUP_RELEASE", run_id, critical=True, create_only=True)
    expected = "released" if release else "held"
    end = clock() + timeout_s
    while True:
        if snapshot_exists(output / "HOST_STOP"):
            raise RuntimeError("startup_host_stopped:" + ipc.read_text(output / "HOST_STOP"))
        try:
            row = read(output / "startup.json")
        except FileNotFoundError:
            row = None
        if row is not None:
            if row.get("run_id") != run_id:
                raise RuntimeError("startup_pause_identity_mismatch")
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["utc"].replace("Z", "+00:00"))).total_seconds()
            if not 0 <= age <= 5:
                raise RuntimeError("startup_pause_stale_telemetry")
            if type(row.get("time")) is not int or row["time"] > 859:
                raise RuntimeError("startup_morning_window_expired:" + str(row.get("time")))
            if row.get("state") == expected and row.get("host_paused") is (not release):
                return row
        if clock() >= end:
            raise RuntimeError("startup_pause_" + expected + "_timeout")
        sleep(.25)

def sha(path):
    return hashlib.sha256(ipc.read_bytes(Path(path))).hexdigest()


def staged_mod_preflight(mods):
    """Reject incomplete SMAPI packages before any owned game launch."""
    for folder, dll, expected in mods:
        folder = Path(folder)
        manifest_path = folder / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"mod preflight: missing manifest.json in {folder}")
        try:
            entry = read(manifest_path).get("EntryDll")
        except (ValueError, OSError, AttributeError) as exc:
            raise RuntimeError(f"mod preflight: invalid manifest.json in {folder}") from exc
        if entry != dll:
            raise RuntimeError(f"mod preflight: manifest EntryDll must be {dll} in {folder}")
        binary = folder / dll
        if not binary.is_file() or sha(binary) != expected:
            raise RuntimeError(f"mod preflight: missing/mismatched pinned DLL {binary}")


def candidate_dependencies(dll, provenance):
    """Explicit, hash-pinned N1 siblings; never copy game/SMAPI assemblies by glob."""
    dependencies = {}
    for name, expected in provenance.get("dependencies", {}).items():
        if Path(name).name != name or "/" in name or "\\" in name or not name.endswith(".dll") or name == Path(dll).name:
            raise ValueError("invalid candidate dependency: " + name)
        source = Path(dll).parent / name
        if sha(source) != expected:
            raise RuntimeError("candidate dependency mismatch: " + name)
        dependencies[name] = source
    return dependencies


def rig_config():
    """Explicit local rig settings (ARTI_FREE_PLAY_RIG_CONFIG); an example layout is the default."""
    path = os.environ.get("ARTI_FREE_PLAY_RIG_CONFIG")
    if path:
        return read(path)
    return dict(root=r"C:\ARTI\rig", hostname="GAME-PC",
                game=r"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley",
                appData=str(Path(os.environ.get("APPDATA", "")) / "StardewValley"))


def checked_rig(rehearsal_checkout=None):
    cfg = rig_config()
    rig = Path(cfg["root"]).resolve()
    if (os.name != "nt" or socket.gethostname().upper() != cfg["hostname"].upper()
            or ROOT.resolve() != (Path(rehearsal_checkout).resolve() if rehearsal_checkout
                                   else (rig / "rig-checkout").resolve())):
        raise RuntimeError("game work requires the configured rig hostname and checkout")
    return cfg, rig


def owner_idle(cfg, run=None):
    """Optional rig guard. Only launch-record PIDs with matching creation times are exempt."""
    script = cfg.get("ownerIdleScript")
    if not script:
        return
    command = ["powershell", "-NoProfile", "-File", script]
    if run is not None:
        command += ["-LaunchRecordRoot", str(run)]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def _test_rearm_rig(folder, output):
    cfg = rig_config()
    rig = Path(cfg["root"])
    return (os.name == "nt" and socket.gethostname().upper() == cfg["hostname"].upper()
            and ROOT.resolve() == (rig / "rig-checkout").resolve()
            and Path(folder).resolve() == (rig / "operator-staging/run/command").resolve()
            and Path(output).resolve().is_relative_to((rig / "rig/free-play-runs").resolve()))


def _test_operator_startup_rearm(folder, output, halt, *, fresh_start=False):
    """Owner-authorized test setup only; the existing P2 tool owns every mutation."""
    from tools import arti_rearm
    output = Path(output)
    def eligible(current, _notes):
        return (fresh_start and _test_rearm_rig(folder, output)
                and not any((output / p).exists() for p in ("enroll.log", "window.json", "runtime/live7"))
                and current == halt and bool(current.get("entries"))
                and all(e.get("cause") in {"native_unclean_exit"} and e.get("source") == "native"
                        for e in current["entries"]))
    if not eligible(halt, []):
        raise RuntimeError("test-operator re-arm refused: requires fresh test-rig startup containment")
    token_id = {k: halt[k] for k in ("episode_id", "halt_seq", "entries_digest")}
    causes = [e["cause"] for e in halt["entries"]]
    with (output / "OPERATOR_REARM.log").open("x", encoding="utf-8") as log:
        outcome = arti_rearm.rearm(folder, "rig_operator", eligible, out=lambda s: log.write(s + "\n"))
    if outcome != "rearmed":
        raise RuntimeError("test-operator startup re-arm failed: " + outcome)
    witness = read(Path(folder) / "session.json")
    rearmed = witness.get("rearmed") or {}
    if witness.get("halt") is not None or any(rearmed.get(k) != token_id[k] for k in ("episode_id", "halt_seq")):
        raise RuntimeError("test-operator startup re-arm lacks matching native witness")
    write(output / "OPERATOR_REARM_SETUP.json", dict(tag="C_operator_preparation", utc=utc(),
          actor="rig_operator", causes=causes, token_id=token_id, rearmed=rearmed,
          native_halt=read(Path(folder) / "halt.json"), before_enrollment=True, not_arti_rescue=True))


def operator_rearm_before_enrollment(folder, output, *, test_operator=False, timeout_s=180, clock=time.monotonic, sleep=time.sleep):
    """Pre-enrollment preparation; automatic confirmation is restricted to the test rig."""
    session = read(Path(folder) / "session.json")
    halt = session.get("halt")
    if halt is None:
        return
    key = (halt["episode_id"], halt["halt_seq"])
    write(Path(output) / "OPERATOR_REARM_REQUIRED.json", dict(utc=utc(), tag="C_operator_preparation",
          halt=halt, folder=str(folder), command=[sys.executable, "-m", "tools.arti_rearm",
          "--folder", str(folder), "--actor", "rig_operator"], before_enrollment=True))
    if test_operator:
        _test_operator_startup_rearm(folder, output, halt, fresh_start=True)
    end = clock() + timeout_s
    while clock() < end:
        sleep(.2)
        session = read(Path(folder) / "session.json")
        current, rearmed = session.get("halt"), session.get("rearmed") or {}
        if current is None:
            if (rearmed.get("episode_id"), rearmed.get("halt_seq")) != key:
                raise RuntimeError("operator preflight: HALT vanished without matching native re-arm witness")
            write(Path(output) / "OPERATOR_REARM_COMPLETED.json", dict(utc=utc(), tag="C_operator_preparation",
                  rearmed=rearmed, before_enrollment=True, not_arti_rescue=True))
            return
        if (current.get("episode_id"), current.get("halt_seq")) != key:
            raise RuntimeError("operator preflight: HALT changed; inspect the new episode before any trial")
    raise RuntimeError("operator re-arm preflight timed out before enrollment")

# PM-1 fix11b: ONE state model for the whole day boundary (replaces the fix10a save-menu and fix10b
# acceptance->native_saved windows; a16 stopped in the gap between native_saved and the new day's first
# heartbeat). The boundary opens at the HOST's own sleep acceptance (host_answer Yes, gift=false) or at the
# native barrier (saving/newDay/end-of-night from the same heartbeat epoch). It closes only when a heartbeat
# is newer than the boundary's native_saved: the new day is loaded and the HOST ticks again. Inside it, HOST
# quiet is expected; loss counts only for HOST_STOP, a HOST_SAFETY_STOP, a gone HOST pid, a missing heartbeat
# or quiet past one hard bound from the last real HOST progress (heartbeat or any HOST row).
DAY_BOUNDARY_QUIET_MAX_S = 600
NIGHT_QUIET_MAX_S = DAY_BOUNDARY_QUIET_MAX_S  # fix10b name kept for callers/tests
HOST_TAIL_BYTES = 262144
HOST_SCAN_MAX_BYTES = 16 * HOST_TAIL_BYTES


HOST_HEARTBEAT_FRESH_S = 15
# fix56A (ADR 81): recover, not stop. a41 ended at a 16.3 s HOST heartbeat gap while BOTH games were stalled by
# one machine-wide hiccup (farmhand TICK_STALL 14.7 s in the same window, 11.8 s just before; commit 96.9 %,
# disk queue 17). A live HOST that falls quiet outside a day boundary is a named fault for this bound; a gone
# HOST pid, HOST_STOP, a HOST_SAFETY_STOP row or quiet past the bound still stops. 120 s is ~7x the worst
# observed recovered stall and well inside the 10-min no-progress stop; a30's 480 s freeze still stops.
HOST_FREEZE_RECOVER_MAX_S = 120
# ADR 128 (a54: HOST heartbeat gaps of 148 s and 324 s; the HOST came back at 43 frames a second): while the machine itself is
# demonstrably stalled, a quiet HOST gets the owner's 600 s. Evidence: this harness's own loop stalled, or the disk hold is open.
HOST_FREEZE_MACHINE_STALL_MAX_S = 600
HARNESS_LOOP_STALL_S = 10


def host_liveness(host_out, now, host_pid=None, machine_stalled=False):
    """Evidence-based HOST liveness: heartbeat file, exact pid, native save markers.

    Returns {lost, state, age_s, heartbeat_at}. States: fresh, day_boundary, freeze_grace (alive HOST quiet
    outside a boundary, within HOST_FREEZE_RECOVER_MAX_S) or a stop state: host_stop, heartbeat_missing,
    process_gone, host_safety_stop, boundary_exceeded, freeze_exceeded, pid_unknown. Never grants a
    saved-night credit or permits closing across a stale barrier.
    """
    host_out = Path(host_out)
    if snapshot_exists(host_out / "HOST_STOP"):
        return dict(lost=True, state="host_stop", age_s=None, heartbeat_at=None)
    try:
        heartbeat_mtime = snapshot_stat(host_out / "heartbeat").st_mtime
    except OSError:
        # persistent absence/contention is an honest telemetry stop
        return dict(lost=True, state="heartbeat_missing", age_s=None, heartbeat_at=None)
    age = now - heartbeat_mtime
    out = lambda lost, state: dict(lost=lost, state=state, age_s=round(age, 3), heartbeat_at=heartbeat_mtime)
    if age <= HOST_HEARTBEAT_FRESH_S:
        return out(False, "fresh")
    if host_pid is not None and not process_alive(host_pid):
        return out(True, "process_gone")  # a crashed or exited HOST is never a save wait or a freeze
    boundary = host_day_boundary(host_out, heartbeat_mtime)
    if boundary is not None:
        if boundary["safety_stop"]:
            return out(True, "host_safety_stop")
        if now - boundary["progress_at"] > DAY_BOUNDARY_QUIET_MAX_S:
            return out(True, "boundary_exceeded")
        return out(False, "day_boundary")
    if host_pid is None:
        return out(True, "pid_unknown")  # no process evidence: a crash and a freeze look the same
    if _host_safety_stop_since(host_out, heartbeat_mtime):
        return out(True, "host_safety_stop")
    if age > (HOST_FREEZE_MACHINE_STALL_MAX_S if machine_stalled else HOST_FREEZE_RECOVER_MAX_S):
        return out(True, "freeze_exceeded")
    return out(False, "freeze_grace")


def host_telemetry_lost(host_out, now, host_pid=None):
    return host_liveness(host_out, now, host_pid)["lost"]


def _host_safety_stop_since(host_out, heartbeat_mtime, tail_bytes=65536):
    """A HOST_SAFETY_STOP row from the heartbeat epoch on (bounded tail read; torn lines earn nothing)."""
    try:
        with ipc.open_shared(Path(host_out) / "host.jsonl", "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            lines = f.read().splitlines()[1 if size > tail_bytes else 0:]
    except OSError:
        return False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("ev") != "HOST_SAFETY_STOP":
            continue
        at = row.get("at")
        if not isinstance(at, (int, float)) or at >= heartbeat_mtime - 5:
            return True
    return False


def commit_snapshot():
    """System commit charge (GlobalMemoryStatusEx) for a gap row; None off Windows or on any error.

    a41's freeze came with commit at 96.9 % of the limit and a ~1.6 GB C: free drop (pagefile growth): the gap
    row carries the same fact so the next freeze names its memory state without an external sampler."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MemoryStatus(dwLength=ctypes.sizeof(MemoryStatus))
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        mb = 1024 ** 2
        limit, avail = status.ullTotalPageFile, status.ullAvailPageFile
        return dict(commit_pct=round(100 * (limit - avail) / limit, 1) if limit else None,
                    commit_used_mb=(limit - avail) // mb, commit_limit_mb=limit // mb,
                    avail_phys_mb=status.ullAvailPhys // mb)
    except Exception:  # noqa: BLE001 - a forensic fact never affects liveness
        return None


class HostTelemetryGaps:
    """fix56A: every HOST quiet gap outside a day boundary becomes one named harness fault row.

    Opens a gap at the first freeze_grace poll, closes it as RECOVER when the heartbeat is fresh again, or as
    STOP with the liveness state that ended the session. Rows go to the run's host-liveness.jsonl."""

    def __init__(self, record, memory=commit_snapshot):
        self.record = record
        self.memory = memory
        self.open = None
        self.recovered = 0
        self.max_recovered_s = 0.0

    def observe(self, liveness, now):
        state = liveness["state"]
        if state == "freeze_grace":
            if self.open is None:
                self.open = dict(heartbeat_at=liveness["heartbeat_at"], first_seen_at=now)
                self.record(dict(ev="host_telemetry_gap", heartbeat_at=liveness["heartbeat_at"],
                                 age_s=liveness["age_s"], bound_s=HOST_FREEZE_RECOVER_MAX_S, memory=self.memory()))
            return
        if self.open is None:
            return
        gap = self.open
        self.open = None
        if liveness["lost"]:
            self.record(dict(ev="FAULT", type="harness_liveness", cause="host_telemetry_gap", disposition="STOP",
                             stop_state=state, heartbeat_at=gap["heartbeat_at"], age_s=liveness["age_s"],
                             bound_s=HOST_FREEZE_RECOVER_MAX_S, memory=self.memory()))
            return
        # The newest heartbeat ends the gap; its age bounds the true gap from the quiet side.
        gap_s = round((liveness["heartbeat_at"] or now) - gap["heartbeat_at"], 3)
        self.recovered += 1
        self.max_recovered_s = max(self.max_recovered_s, gap_s)
        self.record(dict(ev="FAULT", type="harness_liveness", cause="host_telemetry_gap", disposition="RECOVER",
                         heartbeat_at=gap["heartbeat_at"], resumed_at=liveness["heartbeat_at"], gap_s=gap_s,
                         resumed_state=state, bound_s=HOST_FREEZE_RECOVER_MAX_S, memory=self.memory()))


def _host_rows_since_acceptance(host_out):
    """HOST rows from the latest sleep acceptance to EOF (backward chunked scan, bounded), else the tail."""
    try:
        with ipc.open_shared(Path(host_out) / "host.jsonl", "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            span = min(size, HOST_TAIL_BYTES)
            while True:
                f.seek(size - span)
                lines = f.read(span).splitlines()[1 if span < size else 0:]
                rows = []
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # a torn line earns no credit
                    if isinstance(row, dict):
                        rows.append(row)
                accepted = [i for i, r in enumerate(rows) if _sleep_accepted(r)]
                if accepted or span >= min(size, HOST_SCAN_MAX_BYTES):
                    return rows[accepted[-1]:] if accepted else rows
                span = min(size, HOST_SCAN_MAX_BYTES, span * 2)
    except OSError:
        return []


def _sleep_accepted(row):
    facts = row.get("facts") or {}
    return row.get("ev") == "host_answer" and facts.get("gift") is False and facts.get("answer") == "Yes"


def host_day_boundary(host_out, heartbeat_mtime):
    """The open day boundary as {opened_by, progress_at, safety_stop}, or None when no boundary is open.

    Rows are corroborating native evidence only: the HOST's last row must be from the heartbeat epoch (a
    log that stopped long before the heartbeat proves nothing), and native_saved closes the boundary only
    once a heartbeat is newer than it."""
    host_out = Path(host_out)
    rows = _host_rows_since_acceptance(host_out)
    try:
        last_at = max(float(r.get("at", -1)) for r in rows) if rows else None
    except (TypeError, ValueError):
        last_at = None
    current = last_at is not None and last_at - heartbeat_mtime > -5
    safety = any(r.get("ev") == "HOST_SAFETY_STOP" for r in rows)
    progress = max(heartbeat_mtime, last_at) if current else heartbeat_mtime
    if rows and _sleep_accepted(rows[0]) and current:
        saved = [float(r["at"]) for r in rows if r.get("ev") == "native_saved" and isinstance(r.get("at"), (int, float))]
        if not saved or heartbeat_mtime <= saved[0]:
            return dict(opened_by="sleep_accepted", progress_at=progress, safety_stop=safety)
    try:
        barrier = host_out / "barrier.json"
        state = read(barrier)
        if (state.get("saving") is True and state.get("stopped") is False
                and abs(snapshot_stat(barrier).st_mtime - heartbeat_mtime) < 5):
            return dict(opened_by="native_barrier", progress_at=heartbeat_mtime, safety_stop=safety and current)
    except (OSError, ValueError):
        pass  # no barrier evidence
    return None


def process_alive(pid):
    """Liveness of the exact launched HOST pid (no PowerShell per poll). Unknown is treated as alive."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.GetLastError() != 87  # ERROR_INVALID_PARAMETER: no such process
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def manifest(path):
    return {str(p.relative_to(path)): sha(p) for p in sorted(Path(path).rglob("*")) if p.is_file()}

def contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"unsafe target outside {root}: {path}")
    return path

# mod_exit.json is NOT reverted: it is native's clean-exit witness for the runtime that the retained
# occurrence ring names. Reverting it made every clean launch latch native_unclean_exit (PM-1 3b F#10);
# a crashed runtime still writes no witness, so a real unclean exit still HALTs.
N1_RESTORED = ("halt.json", "rearm.json", "stop_all.json", "control.json")


def journal_file_sets(folder, existing=None):
    """Include the logical journal, redundant heads, native archives and all segment generations.

    Relative paths let Baseline restore the exact file set even after rotation/pruning.
    The normal finally preserves the session's complete journal under evidence before restoring it.
    """
    return dict(existing or {}, command_journal=(Path(folder), "command-settled-journal-v1.json*"),
                command_segments=(Path(folder) / "command-settled-journal-v1.json.segments", "**/*"),
                command_archives=(Path(folder) / "command-settled-journal-v1.json.archive", "**/*"))


class Baseline:
    """Compressed immutable baseline, restore with exact file-set comparison (including missing files).

    ADR 173: only explicitly installed test-line names belong to this baseline.
    Foreign entries are listed by name only and never traversed or restored.
    """
    def __init__(self, output, files, saves, *, file_sets=None, owned_saves=()):
        self.output, self.files, self.saves = Path(output), files, Path(saves)
        self.base = self.output / "baseline"
        self.file_sets = file_sets or {}
        self.owned_saves = self.checked_names(owned_saves)

    @staticmethod
    def checked_names(names):
        names = tuple(sorted(set(names)))
        if any(not n or n in ('.', '..') or any(c in n for c in '/\\:') for n in names):
            raise ValueError('invalid owned save folder name')
        return names

    def save_members(self):
        return storage.save_members(self.saves, self.owned_saves)

    def saves_manifest(self):
        return {str(p.relative_to(self.saves)): sha(p) for p in self.save_members()[0]}

    def archive_manifest(self):
        archive = self.base / 'Saves'
        if (archive / storage.INDEX).is_file():
            names = storage.index(archive)[0]['files']
            if any(p.relative_to(archive).as_posix() not in names
                   for p in storage.save_members(archive, self.owned_saves)[0]):
                raise ValueError('unindexed file in owned save archive')
            paths = [archive / n for n in names if n.split('/')[0] in self.owned_saves]
        else:
            paths = []
            for name in self.owned_saves:
                child = archive / name
                paths.extend([child] if child.is_file() else storage.files(child))
        return {str(p.relative_to(archive)): storage.digest_file(p) for p in paths}

    @classmethod
    def from_record(cls, output, saves, *, file_sets=None):
        """Leftover recovery: legacy whole-Saves backups grant no foreign ownership."""
        output = Path(output)
        record = read(output / 'baseline.json')
        names = record.get('owned_saves')
        if names is None:
            staging = read(output / 'STAGING.json') if (output / 'STAGING.json').is_file() else {}
            if not staging.get('save_folder'):
                raise RuntimeError('save_ownership_missing: legacy recovery needs STAGING.json')
            names = [staging['save_folder']]
        if file_sets is None:
            file_sets = record.get('file_set_paths', {})
        if set(file_sets) != set(record.get('file_sets', {})):
            raise RuntimeError('file_set_paths_missing: supply legacy recovery paths')
        baseline = cls(output, {}, saves, file_sets=file_sets, owned_saves=names)
        baseline.record = dict(record, owned_saves=list(baseline.owned_saves))
        baseline.record['saves'] = {n: h for n, h in record['saves'].items()
                                    if n.replace('\\', '/').split('/')[0] in baseline.owned_saves}
        return baseline

    def set_manifest(self, folder, pattern):
        return {p.relative_to(folder).as_posix(): sha(p) for p in sorted(Path(folder).glob(pattern)) if p.is_file()}

    def capture(self):
        self.base.mkdir(exist_ok=False)
        self.record = {"files": {}, "saves": self.saves_manifest(),
                       "owned_saves": list(self.owned_saves),
                       "foreign_saves_left_untouched": sorted(p.name for p in self.saves.iterdir()
                            if os.path.normcase(p.name) not in {os.path.normcase(n) for n in self.owned_saves})}
        self.record["file_sets"] = {}
        self.record['file_set_paths'] = {n: [str(folder), pattern]
                                         for n, (folder, pattern) in self.file_sets.items()}
        for name, (folder, pattern) in self.file_sets.items():
            rows = self.set_manifest(folder, pattern)
            self.record["file_sets"][name] = rows
            archive = self.base / name
            archive.mkdir()
            storage.capture(Path(folder), archive, self.output,
                            members=[Path(folder) / filename for filename in rows])
        members, directories = self.save_members()
        storage.capture(self.saves, self.base / "Saves", self.output,
                        members=members, directories=directories)
        for name, path in self.files.items():
            path = Path(path)
            present = path.is_file()
            self.record["files"][name] = {"path": str(path), "sha256": sha(path) if present else None}
            if present:
                shutil.copy2(path, self.base / name)
        if self.archive_manifest() != self.record["saves"] or any(
                {p.relative_to(self.base / name).as_posix(): storage.digest_file(p)
                 for p in storage.files(self.base / name)} != rows
                for name, rows in self.record["file_sets"].items()):
            raise RuntimeError("baseline changed during capture; no staging allowed")
        write(self.output / "baseline.json", self.record)

    def restore(self):
        # Validate the backup before writing any target.
        if self.archive_manifest() != self.record["saves"]:
            raise RuntimeError("baseline save archive changed")
        for name, row in self.record["files"].items():
            if row["sha256"] and sha(self.base / name) != row["sha256"]:
                raise RuntimeError("baseline archive changed: " + name)
        for name, rows in self.record["file_sets"].items():
            if {p.relative_to(self.base / name).as_posix(): storage.digest_file(p)
                    for p in storage.files(self.base / name)} != rows:
                raise RuntimeError("baseline file-set archive changed: " + name)
        extra = self.output / "preserved-saves"
        extra.mkdir(exist_ok=True)
        # Preserve complete directories then restore the original set, including nested extras.
        self.save_members()  # validate owned paths before any move
        for name in self.owned_saves:
            folder = self.saves / name
            if not folder.exists():
                continue
            source = contained(folder, self.saves)
            target = contained(extra / folder.name, extra)
            if target.exists():
                target = contained(extra / (folder.name + '-recovery-' + str(time.time_ns())), extra)
            shutil.move(str(source), str(target))
        storage.materialize(self.base / "Saves", self.saves, roots=self.owned_saves)
        for name, row in self.record["files"].items():
            target = Path(row["path"])
            if row["sha256"] is None:
                if target.exists():
                    shutil.move(str(target), str(self.output / ("created-" + name)))
            else:
                shutil.copy2(self.base / name, target)
        for name, (folder, pattern) in self.file_sets.items():
            folder = Path(folder)
            preserved = self.output / ("preserved-" + name)
            if preserved.exists():
                preserved = self.output / ("preserved-" + name + '-recovery-' + str(time.time_ns()))
            preserved.mkdir()
            folder.mkdir(parents=True, exist_ok=True)
            for filename in self.set_manifest(folder, pattern):
                (preserved / filename).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder / filename), str(preserved / filename))
            storage.materialize(self.base / name, folder)
        sets_identical = all(self.set_manifest(*paths) == self.record["file_sets"][name]
                             for name, paths in self.file_sets.items())
        result = {"utc": utc(), "saves_identical": self.saves_manifest() == self.record["saves"],
                  "owned_saves": list(self.owned_saves),
                  "file_sets_identical": sets_identical,
                  "files_identical": all((sha(r["path"]) if Path(r["path"]).is_file() else None) == r["sha256"] for r in self.record["files"].values())}
        result["files_identical"] &= sets_identical
        write(self.output / "restore.json", result)
        if not all(result[k] for k in ("saves_identical", "files_identical")):
            raise RuntimeError("restore verification failed")
        return result

def stage_template(template, saves, farm, output, *, q12=False):
    template, saves = Path(template), Path(saves)
    doc = ET.parse(template / template.name)
    root = doc.getroot()
    which = int(root.findtext("whichFarm", "-1"))
    if which != {"Standard": 0, "Wilderness": 4}[farm]:
        raise ValueError(f"template farm {which} does not match {farm}; never relabel a map")
    player = root.find("player")
    host = int(player.findtext("UniqueMultiplayerID", "0"))
    peers = root.findall("./farmhands/Farmer")
    # 1.6 saves can store cabin occupants inside interior farmhand elements.
    peers += [e for e in root.findall(".//farmhand") if e.find("UniqueMultiplayerID") is not None]
    unique = {int(p.findtext("UniqueMultiplayerID", "0")): p for p in peers}
    unique.pop(host, None); unique.pop(0, None)
    if len(unique) != 1:
        raise ValueError("template must contain exactly one established farmhand")
    peer_id, peer = next(iter(unique.items()))
    if int(root.findtext("timeOfDay", "600")) != 600:
        raise ValueError("template must be a saved morning")
    destination = contained(saves / template.name, saves)
    if destination.exists(): raise ValueError("template collides with an original save")
    before = manifest(template)
    shutil.copytree(template, destination)
    # Cheats are enabled only on this disposable copy. No hidden runtime staging.
    cheats = root.find("allowChatCheats")
    if cheats is None: cheats = ET.SubElement(root, "allowChatCheats")
    cheats.text = "true"
    # HOST only: force native wake fallback to its own home/observed bed, never a saved cabin stay.
    for key in ("lastSleepLocation", "disconnectLocation"):
        element = player.find(key)
        if element is not None: element.text = ""
    if q12:
        # Disposable fixture only: make native toolbar row rotation observable.
        peer.find("maxItems").text = "36"
        items = peer.find("items")
        while len(items) < 36:
            ET.SubElement(items, "Item", {"{http://www.w3.org/2001/XMLSchema-instance}nil": "true"})
    # Native xsi:type values contain xsd QNames. ElementTree otherwise drops
    # xmlns:xsd because it appears only in values, making .NET reject the save.
    declarations = dict(value for _, value in ET.iterparse(template / template.name, events=("start-ns",)))
    used_uris = {name[1:].split("}", 1)[0] for element in root.iter()
                 for name in (element.tag, *element.attrib) if name.startswith("{")}
    for prefix, uri in declarations.items():
        ET.register_namespace(prefix, uri)
        if uri not in used_uris:
            root.set("xmlns:" + prefix if prefix else "xmlns", uri)
    doc.write(destination / template.name, encoding="utf-8", xml_declaration=False)
    result = {"tag": "STAGING", "utc": utc(), "template": str(template), "template_hashes": before,
              "staged_hashes": manifest(destination), "farm": farm, "save_folder": template.name,
              "save_id": int(root.findtext("uniqueIDForThisGame")), "host_id": host,
              "farmhand_id": peer_id, "farmhand_name": peer.findtext("name"),
              "changes": ["allowChatCheats=true on disposable copy", "HOST lastSleepLocation/disconnectLocation empty: native own-home wake fallback", "HOST helper adds 20 Wood before arm; see STAGING_host_gift_inventory"], "rare_fixtures": "not staged by default"}
    write(output / "STAGING.json", result)
    if q12:
        result["changes"].append("Q12 farmhand backpack capacity 36; existing items retained, empty rows appended")
        write(output / "STAGING.json", result)
    return result

def rows(path):
    if not Path(path).exists(): return
    for line_no, line in enumerate(ipc.read_text(Path(path), encoding="utf-8-sig").splitlines(), 1):
        try: yield line_no, json.loads(line)
        except ValueError: yield line_no, {"ev": "MALFORMED", "raw": line}

class JournalRecorder:
    """File-only wall receipt timestamps. Never resets semantic progress from HUD/clock/heartbeat."""
    def __init__(self, run, *, hp_hard_stop=False):
        self.run = Path(run); self.offsets = {}; self.lines = {}
        self.safety = NativeSafety(hp_hard_stop=hp_hard_stop)
        self.saved_counters = SavedCounters(run)
        self.stop_requested = False
        self.native_error = None
        self.progress_at = (read(self.run / "window.json")["start_at"]
                            if (self.run / "window.json").exists() else time.time())

    def poll(self):
        # Read the baseline before journal outcomes; night deltas after them.
        try:
            counters = self.saved_counters.poll()
        except (OSError, ValueError, KeyError, ET.ParseError) as exc:
            # Evidence failure must use STOP/restore, including in the final poll.
            counters = []
            if self.native_error is None:
                self.native_error = str(exc)
                write(self.run / 'native-safety-error.json', dict(utc=utc(), error=self.native_error))
        for row, ref in counters:
            if '/checkpoints/' not in ref:
                self.record_native(row, ref)
        for path in sorted((self.run / "runtime").glob("live7/*/day-*/journal/g1_journal.jsonl")):
            with ipc.open_shared(path, "rb") as f:
                f.seek(self.offsets.get(path, 0)); raw=f.read()
            complete=raw.rfind(b"\n")+1
            if not complete: continue
            self.offsets[path]=self.offsets.get(path,0)+complete
            with ipc.open_shared(self.run / "timeline.jsonl", "a", encoding="utf-8") as out:
                for rawline in raw[:complete].splitlines():
                    self.lines[path]=self.lines.get(path,0)+1
                    try: row=json.loads(rawline)
                    except ValueError: row={"ev":"MALFORMED"}
                    action=row.get("action") or {}
                    cleanup=(row.get("evidence") or {}).get("cleanup") or {}
                    semantic=(row.get("ev")=="part_live_row" and row.get("outcome_class")=="VERIFIED" and cleanup.get("input_released") is True and cleanup.get("free") is True and cleanup.get("menu") is None) or (row.get("ev")=="action_result" and action.get("world_effect_status")=="observed_change" and action.get("cleanup_confirmed") is True)
                    if semantic:
                        self.progress_at = time.time()
                    out.write(json.dumps(dict(at=time.time(), file=str(path.relative_to(self.run)),line=self.lines[path],ev=row.get("ev"),semantic_progress=semantic))+"\n")
                    self.record_native(row, f'{path.relative_to(self.run).as_posix()}:{self.lines[path]}')
        for row, ref in counters:
            if '/checkpoints/' in ref:
                self.record_native(row, ref)

    def record_native(self, row, ref):
        for outcome in self.safety.consume(row, ref, time.time()):
            with ipc.open_shared(self.run / 'native-outcomes.jsonl', 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(outcome) + '\n')

    def stop_reasons(self):
        stop = self.safety.first_stop
        return ([stop['reason']] if stop else []) + (['harness_native_evidence_unavailable'] if self.native_error else [])

    def request_stop(self, reasons, day, cycle, env):
        if self.stop_requested:
            return False
        write(self.run / f'stop-reasons-{day}.json', dict(utc=utc(), reasons=reasons,
              native_stop=self.safety.first_stop))
        call('live_cycle.py', ['stop', '--yes', '--cycle-dir', cycle],
             self.run / f'requested-stop-{day}.log', env)
        self.stop_requested = True
        return True

def metrics(run):
    run = Path(run)
    journal_root = run / "runtime" if (run / "runtime").exists() else run
    result = {f: dict(attempts=0, verified_effects=0, clean_refusals=0, uncertain=0, safety_stops=0, recovery_faults=0) for f in (*FAMILIES, "unmapped")}
    failures, dimensions, stops, times, progress, episodes = [], {}, 0, [], [], set()
    faults, fault_types = [], {}
    terminal_faults, cycle_stops = [], set()
    paths = [*journal_root.glob("live7/*/day-*/journal/g1_journal.jsonl"),
             *journal_root.glob("live7/*/cycle_events.jsonl")]
    for path in sorted(paths):
        for line, row in rows(path):
            hard_stop = (row.get("ev") != "FAULT" and any("safety_stop" in str(row.get(k, "")).lower()
                for k in ("ev", "outcome_class", "verdict", "status", "result", "reason")))
            if path.name == "cycle_events.jsonl" and row.get("ev") != "FAULT" and not hard_stop:
                continue  # cycle mirrors of episode stops are not extra stop episodes
            at = row.get("at")
            if isinstance(at, (int, float)): times.append(at)
            ev = row.get("ev", "")
            if ev == "FAULT":
                family = SKILLS.get(row.get("skill"), "unmapped")
                fault = dict(file=str(path), line=line, family=family, type=row.get("type"),
                             cause=row.get("cause"), refs=row.get("refs"),
                             disposition=row.get("disposition"), observed=row.get("observed"))
                faults.append(fault)
                name = str(row.get("type")) + ":" + str(row.get("cause"))
                fault_types[name] = fault_types.get(name, 0) + 1
                if row.get("disposition") == "RECOVER":
                    result[family]["recovery_faults"] += 1
            if ev == "part_live_row":
                family = SKILLS.get(row.get("skill"), "unmapped")
                m = result[family]; m["attempts"] += 1
                outcome = row.get("outcome_class")
                cleanup = (row.get("evidence") or {}).get("cleanup") or {}
                if outcome == "VERIFIED" and cleanup.get("input_released") is True and cleanup.get("menu") is None and cleanup.get("free") is True:
                    m["verified_effects"] += 1
                    if isinstance(at, (float, int)): progress.append(at)
                elif outcome == "CLEAN_FAIL" and cleanup.get("input_released") is True and cleanup.get("menu") is None and cleanup.get("free") is True:
                    m["clean_refusals"] += 1
                elif outcome == "SAFETY_STOP": m["safety_stops"] += 1
                else: m["uncertain"] += 1
                if outcome != "VERIFIED": failures.append(dict(file=str(path), line=line, family=family, outcome=outcome, reason=row.get("reason")))
            if hard_stop:
                stops += 1
                if path.name == "cycle_events.jsonl":
                    cycle_stops.add(path)
                else:
                    episodes.add(str(path))
                if ev != "part_live_row":
                    terminal_faults.append(dict(file=str(path), line=line, type="terminal_safety_stop",
                        cause=row.get("reason") or row.get("ev"), refs={}, family="unmapped", disposition="HARD_STOP"))
            if ev == "action_result":
                action = row.get("action") or {}
                key = "|".join(str(x) for x in (row.get("op", "unknown"), (action.get("before") or {}).get("active_menu", "unknown"), row.get("model", "unknown")))
                dimensions[key] = dimensions.get(key, 0) + 1
            if ev == "MALFORMED": failures.append(dict(file=str(path), line=line, reason="malformed_journal"))
    elapsed = max(times) - min(times) if len(times) > 1 else 0
    # Old journals omit timestamps on effect rows. Do not manufacture wall-time precision.
    gap = max((b-a for a,b in zip(sorted(progress), sorted(progress)[1:])), default=None)
    timeline = [r for _,r in rows(run / "timeline.jsonl") or ()]
    wall_progress = [r["at"] for r in timeline if r.get("semantic_progress")]
    bounds = read(run / "window.json") if (run / "window.json").exists() else {}
    if wall_progress:
        points=sorted([bounds.get("start_at",wall_progress[0]),*wall_progress,bounds.get("end_at",wall_progress[-1])])
        gap=max(b-a for a,b in zip(points,points[1:]))
    elif bounds.get("end_at") and bounds.get("start_at"):
        gap=bounds["end_at"]-bounds["start_at"]
    if bounds.get("end_at") and bounds.get("start_at"):
        elapsed=bounds["end_at"]-bounds["start_at"]
    for cycle in cycle_stops:
        if not any(Path(p).is_relative_to(cycle.parent) for p in episodes):
            episodes.add(str(cycle))  # a cycle stop outside a completed episode is still a stop
    inventory = [*faults, *terminal_faults,
        *(dict(f, type="episode_result", cause=f.get("reason"), refs={}, disposition=f.get("outcome")) for f in failures)]
    return dict(families=result, failures=failures, safety_stop_rows=stops,
                fault_inventory=inventory, fault_types=fault_types, named_fault_count=len(faults),
                zero_faults=not faults and not failures and not stops,
                recovery_faults_per_hour=len([f for f in faults if f["disposition"] == "RECOVER"])/(elapsed/3600) if elapsed else None,
                family_fault_rates={f: dict(count=m["recovery_faults"],
                    per_hour=m["recovery_faults"]/(elapsed/3600) if elapsed else None,
                    per_attempt=m["recovery_faults"]/m["attempts"] if m["attempts"] else None,
                    episode_failures=sum(x.get("family") == f for x in failures),
                    episode_failure_rate=sum(x.get("family") == f for x in failures)/m["attempts"] if m["attempts"] else None)
                    for f,m in result.items()},
                safety_stop_episodes=len(episodes), stops_per_hour=len(episodes)/(elapsed/3600) if elapsed else None,
                longest_no_progress_gap_s=gap, no_progress_basis="file receipt timestamps (1s sampling) when available; raw semantic gap includes legitimate waits; otherwise timestamped effect+cleanup rows only",
                verified_effects_basis="runtime effect+cleanup claims; independent native oracles still required for gate PASS",
                dimensions=dimensions, duration_s=elapsed)

def host_gap_summary(run):
    """Counts of fix56A harness_liveness FAULT rows; torn lines are skipped."""
    out = dict(recovered=0, stopped=0, max_gap_s=0.0)
    try:
        lines = ipc.read_text(Path(run) / "host-liveness.jsonl").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("ev") != "FAULT" or row.get("cause") != "host_telemetry_gap":
            continue
        if row.get("disposition") == "RECOVER":
            out["recovered"] += 1
            out["max_gap_s"] = max(out["max_gap_s"], float(row.get("gap_s") or 0))
        else:
            out["stopped"] += 1
    return out


def report(run):
    run = Path(run); m = metrics(run)
    write(run / "metrics.json", m)
    identity = read(run / "request.json") if (run / "request.json").exists() else {}
    cycles = list(run.glob("runtime/live7/*/cycle_state.json")) or list(run.glob("live7/*/cycle_state.json"))
    nights = 0
    if cycles:
        state = read(cycles[0])
        for day in state.get("days", []):
            if day.get("status") != "ended" or day.get("outcome") != "verified": break
            nights += 1
    completed = read(run / "completed.json") if (run / "completed.json").exists() else {}
    restore = read(run / "restore.json") if (run / "restore.json").exists() else {}
    failure=read(run / "failure.json") if (run / "failure.json").exists() else {}
    lines = ["# Free-play acceptance receipt", "", "**" + ("BLOCKED/FAIL — " + failure.get("reason", "run failed") if failure else "INCONCLUSIVE — no automatic whole-primitives PASS.") + "**", "",
             f"Candidate: `{identity.get('code_sha', 'unknown')}` / DLL `{identity.get('dll_sha256', 'unknown')}`; {identity.get('farm', 'unknown')}.",
             f"Window: {completed.get('started_utc', identity.get('started_utc', 'unknown'))} → {completed.get('ended_utc', 'not completed')}; elapsed {completed.get('elapsed_s', 'unknown')} seconds. Restore: {restore}.",
             f"Consecutive saved-night candidates: {nights}. Confirm native after-save checkpoints and no lost day independently.", "",
             "| Family | F/C/U/M | Attempts | Effect+cleanup claims | Clean refusals | Uncertain | Safety stops | Recovery faults |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for family, counts in m["families"].items():
        lines.append(f"| {family} | {'M (not sampled)' if not counts['attempts'] else 'M (oracle review pending)'} | " + " | ".join(str(v) for v in counts.values()) + " |")
    lines += ["", "Audit legend: F = independently VERIFIED LIVE in free play; C = independently VERIFIED LIVE in a controlled witness; U = UNIT_TESTED only; M = required evidence missing. Runtime claims alone never assign F/C.",
              f"Named faults: {m['named_fault_count']}; recovered-fault rate/hour: {m['recovery_faults_per_hour']}; zero-fault condition: {m['zero_faults']}.",
              "Recovered faults continue the session and remain gate failures. fault_inventory, fault_types and family_fault_rates retain every named occurrence; episode failures are separate and must not be added as distinct root causes without review.",
              f"Safety-stop episodes/hour: {m['stops_per_hour']}; longest measured semantic gap: {m['longest_no_progress_gap_s']} s.",
              "Missing timestamps, model/controller/menu dimensions and independent effects stay UNKNOWN. Stop episodes are distinct day journals with safety stops; duplicate raw rows are retained separately.", "",
              "Gate §1: STAGING.json, host/host.jsonl, native snapshots, baseline.json and restore.json retain rig evidence.",
              "Gate §2: targeted family receipts, independent effect+cleanup+next-action oracles and named unsupported tails required.",
              "Gate §3: each map needs ≥3 uninterrupted hours, ≥7 consecutive saved nights, zero rescues/crashes/leaks/unexplained stops and measured ≤30s menu / ≤60s no-progress bounds. Fault drills do not count as endurance.",
              "Gate §4: independent read-only review, STOP/resume drill and both exact-pair map receipts required before owner sign-off.", "",
              "Raw failures are in metrics.json; STAGING and helper effects never count as ARTI effects."]
    gaps = host_gap_summary(run)
    if gaps["recovered"] or gaps["stopped"]:
        lines += ["", f"HOST telemetry gaps (harness_liveness, `host-liveness.jsonl`): {gaps['recovered']} recovered "
                      f"(max {gaps['max_gap_s']} s, bound {HOST_FREEZE_RECOVER_MAX_S} s), {gaps['stopped']} stopped."]
    real = realpath_summary(run)
    if real is not None:
        state, c = real["state"], real["counts"]
        lines += ["", "## Real path (bridge persona + game, text-only)", "",
                  f"**{'REAL-PATH' if state.get('real_path') else 'NOT REAL-PATH'}** — bridge state: {state.get('reason')}"
                  + (f"; refused: {c['refused']}" if c["refused"] else "") + f"; end: {c['end_reason']}.",
                  f"Transcript `bridge/bridge-transcript.jsonl`: {c['turns']} turns ({c['probe_turns']} owner probes, {c['game_turns']} game), "
                  f"{c['replies']} replies; dropped (expired) {c['dropped']}, skipped (budget) {c['skipped']}, unanswered at end {c['unanswered']}; "
                  f"blocked side effects {c['blocked_side_effects']}.",
                  f"Owner probes answered with a fresh now.json why beside them: {c['probes_with_fresh_intent']} of {c['probe_turns']}. "
                  "Persona-vs-intent match is judged from `bridge/REALPATH_SUMMARY.json`, not automatically.",
                  f"Persona inputs in the gate checkout: {c['persona']} (no soul = base persona only; the owner's private files are never copied by the harness)."]
    ipc.publish(run / "REPORT.md", "\n".join(lines) + "\n", critical=True)
    return m

# ---------------------------------------------------------------- real path: bridge persona + game, text-only
# Owner 2026-10-28: real-bridge sessions are TEXT-ONLY (no TTS, no mic/ASR). The bridge runs from this gate
# checkout in its test mode (arti_bridge_testmode.py) with a separate test config, never config_local.json.
REALPATH_BRIDGE_DEFAULTS = dict(
    codex_agent_enabled=True, codex_primary_voice=True,          # the live voice path: Luna via Codex
    codex_model="gpt-6-luna",                                     # the rig's reviewed non-secret profile model
    # Same reply settings as live (config_local.json, non-secret), so a session measures the voice that would stream.
    codex_effort="high", codex_service_tier="priority", codex_timeout_sec=12.0, codex_thread_max_turns=8,
    cursor_trigger_types=["mic", "game", "yt_chat", "curious"],
    live7_status_facts_enabled=True, live7_moments_enabled=True,  # how the bridge sees the game live
    arti_mind_intent_enabled=True,
    bridge_test_probe_on_start=False, bridge_test_max_turns=400,
)
# ADR 90: the game side is the producer. Setting these only on the bridge above left every real-path run with 0 moments.
# ADR 243: her raw hands are on where she plays on the rig (the farmhand mod's GenericWorldInputEnabled follows).
REALPATH_TRIAL_DEFAULTS = dict(live7_moments_enabled=True, live7_activity_events_enabled=True, stardew_live7_raw_hands=True)
REALPATH_READY_TIMEOUT_S = 180
REALPATH_STOP_TIMEOUT_S = 45


def trial_mod_flags(trial_config, trial_defaults):
    """ADR 243: farmhand mod flags the effective trial config needs. Raw hands need the mod's generic ACTION/USE
    target for any faced tile (GenericWorldInputEnabled), not only beside a chest, a counter or an Action tile.
    The text delivery needs the mod's exact capture (GameTextDeliveryEnabled)."""
    effective = dict(trial_config, **trial_defaults)
    flags = {"GenericWorldInputEnabled": True} if effective.get("stardew_live7_raw_hands") is True else {}
    if effective.get("stardew_game_text_delivery_enabled") is True:
        flags["GameTextDeliveryEnabled"] = True
    return flags


def realpath_mind_dir(trial_config):
    """The runner's mind dir: the same resolution its Live7 glue uses (arti_mind.mind_dir over the trial config)."""
    import arti_mind
    return Path(arti_mind.mind_dir(read(trial_config), root=ROOT)).resolve()


def realpath_bridge_config(run, mind, live7_dir, base=None, parent_pid=None):
    """Separate test config for the bridge: defaults, optional reviewed base file, then the pinned run keys."""
    cfg = dict(REALPATH_BRIDGE_DEFAULTS)
    if base:
        cfg.update(read(base))
    bridge = Path(run) / "bridge"
    cfg.update(bridge_test_mode=True, bridge_test_dir=str(bridge), arti_mind_dir=str(mind),
               arti_state_dir=str(Path(mind).parent / 'state'),
               bridge_test_discovery_history=str(Path(mind).parent / 'persona/discoveries.json'),
               live7_help_dir=str(live7_dir), stardew_live7_help_dir="", stardew_command_dir="",  # this run only
               transcript_dir=str(bridge / "session-transcripts"), telemetry_dir=str(bridge / "telemetry"),
               bridge_test_parent_pid=parent_pid if parent_pid is not None else os.getpid())
    return cfg


def kill_own_tree(proc):
    """End OUR bridge process tree: a venv python.exe is a launcher whose child is the real interpreter
    (plus its Codex child), so terminate() alone would orphan the bridge. Never touches another PID."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    else:
        proc.terminate()


class RealPathBridge:
    """Start/stop the text-only bridge beside the session. Any start failure = session NOT real-path."""

    def __init__(self, run, env, *, python=sys.executable, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
                 kill_tree=None):
        self.run, self.env, self.python = Path(run), env, python
        self.dir = self.run / "bridge"
        self._popen, self._clock, self._sleep = popen, clock, sleep
        self._kill_tree = kill_tree or kill_own_tree
        self.proc = None
        self.state = dict(requested=True, real_path=False, reason="not_started")

    def _record(self, **fields):
        self.state.update(fields, utc=utc())
        write(self.run / "realpath.json", self.state)

    def start(self, config):
        return self.spawn(config) and self.wait_ready()

    def spawn(self, config):
        """Start the process only; READY is awaited separately so bridge warm-up overlaps game startup."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "bridge-test-config.json"
        write(path, config)
        env = dict(self.env, ARTI_BRIDGE_TEST_CONFIG=str(path), PYTHONIOENCODING="utf-8")
        try:
            with ipc.open_shared(self.dir / "bridge.log", "ab") as log:
                self.proc = self._popen([self.python, "-B", str(ROOT / "arti_bridge.py")], cwd=ROOT, env=env,
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            self._record(reason="spawn_failed:" + type(exc).__name__)
            return False
        self._record(reason="starting", pid=self.proc.pid, mind_dir=config["arti_mind_dir"],
                     live7_dir=config["live7_help_dir"], config_sha256=sha(path))
        self._spawned_at = self._clock()
        return True

    def wait_ready(self):
        if self.proc is None:
            return False
        deadline = self._spawned_at + REALPATH_READY_TIMEOUT_S
        while True:
            if snapshot_exists(self.dir / "READY.json"):
                self._record(real_path=True, reason="ready")
                return True
            if self.proc.poll() is not None:
                self._record(reason=f"bridge_exited_before_ready:rc={self.proc.returncode}")
                return False
            if self._clock() >= deadline:
                break
            self._sleep(.5)
        self._record(reason="ready_timeout")
        self.stop()
        return False

    def check(self):
        """Mid-session: a bridge that died makes the session no longer real-path (the game continues)."""
        if self.proc is not None and self.state.get("real_path") and self.proc.poll() is not None:
            self._record(real_path=False, reason=f"bridge_exited_mid_session:rc={self.proc.returncode}")

    def stop(self):
        """STOP file → the bridge finishes its turn and writes test_end; terminate only our own child, last."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            ipc.publish(self.dir / "STOP", "harness end", critical=True)
            deadline = self._clock() + REALPATH_STOP_TIMEOUT_S
            while self.proc.poll() is None and self._clock() < deadline:
                self._sleep(.5)
            if self.proc.poll() is None:
                # This run's own bridge only. A venv python.exe is a launcher whose child is the real
                # interpreter (plus its Codex child): end that whole tree, never anything else.
                self._kill_tree(self.proc)
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
                self.state["forced_stop"] = True
        self._record(stopped=True, rc=self.proc.returncode)


def realpath_summary(run):
    """Counts from the bridge transcript for the report; never the private base prompt."""
    run = Path(run)
    state = read(run / "realpath.json") if (run / "realpath.json").exists() else None
    if state is None:
        return None
    counts = dict(turns=0, probe_turns=0, game_turns=0, replies=0, probes_with_fresh_intent=0, dropped=0,
                  skipped=0, unanswered=0, blocked_side_effects=0, refused=None, end_reason=None, persona=None)
    probes = []
    for _, row in rows(run / "bridge/bridge-transcript.jsonl"):
        ev = row.get("ev")
        if ev == "turn":
            counts["turns"] += 1
            counts["replies"] += bool(row.get("reply"))
            if row.get("source") == "owner_probe":
                counts["probe_turns"] += 1
                now = row.get("now") or {}
                counts["probes_with_fresh_intent"] += bool(now.get("fresh") and now.get("why"))
                probes.append(dict(probe=row.get("input"), why=now.get("why") if now.get("fresh") else None,
                                   reply=row.get("reply"), niatku=bool((row.get("context") or {}).get("niatku"))))
            else:
                counts["game_turns"] += 1
        elif ev == "game_trigger_dropped":
            counts["dropped"] += 1
        elif ev == "turn_skipped":
            counts["skipped"] += 1
        elif ev == "turn_unanswered_at_end":
            counts["unanswered"] += 1
        elif ev == "blocked_side_effect":
            counts["blocked_side_effects"] += 1
        elif ev == "test_refused":
            counts["refused"] = row.get("reason")
        elif ev == "test_end":
            counts["end_reason"] = row.get("reason")
        elif ev == "test_start":
            counts["persona"] = {k: (row.get("persona") or {}).get(k) for k in ("soul_present", "self_block_chars", "memories")}
    summary = dict(state=state, counts=counts, probes=probes)
    write(run / "bridge/REALPATH_SUMMARY.json", summary)
    return summary


def ps(command, *, timeout=None):
    import base64
    arg = base64.b64encode(("$ProgressPreference='SilentlyContinue';" + command).encode("utf-16-le")).decode()
    options = {} if timeout is None else {"timeout": timeout}
    child=subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", arg], text=True, capture_output=True, **options)
    if child.returncode:
        raise RuntimeError(f"PowerShell reader failed ({child.returncode}): {child.stderr[:2000]}")
    return child.stdout.strip()

def smapi_running():
    return ps("@(Get-Process StardewModdingAPI -ErrorAction SilentlyContinue).Count") != "0"

def call(script, args, log, env):
    with Path(log).open("xb") as out:
        rc = subprocess.call([sys.executable, "-B", str(ROOT / "scripts/m1_operator" / script), *map(str, args)], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    if rc: raise RuntimeError(f"{script} returned {rc}; see {log}")

def run_soak(a):
    bench = getattr(a, "bench", None)
    cfg, rig = checked_rig(ROOT) if bench else (
        checked_rig(a.rehearsal_checkout) if getattr(a, "rehearsal_checkout", None) else checked_rig())
    owner_idle(cfg)
    lock=rig / "rig/free-play.lock"
    with lock.open("x",encoding="utf-8") as f:
        f.write(json.dumps(dict(pid=os.getpid(),utc=utc(),output=str(a.output))))
    try:
        return _run_soak(a)
    except Exception:
        if getattr(a, 'verify_expect', None) and a.output.is_dir():
            path = a.output / 'verify-result.json'
            result = read(path) if path.is_file() else dict(conditions=[])
            write(path, dict(result, status='FAIL', final=True, stop_reason='harness/cleanup exception'))
        raise
    finally:
        # A stale lock is never stolen on startup; reconciliation is an explicit operator step.
        if getattr(a, "rehearsal_checkout", None):
            from scripts.livepath.safety import cleanup_receipt
            if cleanup_receipt(a.output):
                lock.unlink()
        elif not bench or not (a.output / "baseline.json").exists() or bench.restored():
            lock.unlink()


def stage_bench(a, run, env):
    """Prevalidate every block through the existing witness debug validator."""
    recipes = []
    start = a.bench_start
    if "clock" in start:
        recipes.append(("host", ["/debug time " + str(start["clock"])]))
    if start.get("debug"):
        recipes.append(("farmhand", start["debug"]))
    jobs = []
    for target, commands in recipes:
        script = run / f"STAGING-{target}.txt"
        script.write_text("\n".join(commands) + "\n", encoding="utf-8")
        flags = ["--target", target, "--bench-save-id", str(a.bench_identity[0])]
        if target == "host":
            flags += ["--owner-ok", "--bench-clock"]
        call("stage_save.py", ["check-script", script, *flags], run / f"STAGING-{target}-checked.log", env)
        jobs.append((target, script, flags))
    for target, script, flags in jobs:
        call("stage_save.py", ["run-script", script, *flags, "--label", f"bench-{target}"],
             run / f"STAGING-{target}.log", env)


def stage_bench_last(a, run, runtime, env):
    """ADR 242: the check's last staging step (an open menu, a festival entry) after the final readiness read, which
    refuses both, by one checked /debug line or one key into her window; then focus goes back to the HOST before ARMED."""
    menu = a.bench_start["last"]
    farmhand = ipc.read_text(runtime / "fh-hwnd.txt").strip()
    host = ipc.read_text(runtime / "host-hwnd.txt").strip()
    try:
        if "debug" in menu:
            script = run / "STAGING-menu.txt"
            script.write_text(menu["debug"] + "\n", encoding="utf-8")
            flags = ["--target", "farmhand", "--bench-save-id", str(a.bench_identity[0])]
            call("stage_save.py", ["check-script", script, *flags], run / "STAGING-menu-checked.log", env)
            call("stage_save.py", ["run-script", script, *flags, "--label", "bench-menu"], run / "STAGING-menu.log", env)
        else:
            call("ui_op.py", ["key", menu["key"], farmhand], run / "STAGING-menu.log", env)
    finally:
        call("ui_op.py", ["fg", host], run / "STAGING-menu-host-fg.log", env)
    write(run / "STAGING-menu.json", dict(utc=utc(), expect=menu["expect"], by=menu.get("debug") or "key " + menu["key"]))


class FocusHold:
    """ADR 242: her game window made the active window for hold_s seconds, after_s after ARMED, by the recorded
    focus command (ui_op fg); then the HOST is put back in front. Every step is one row in focus.jsonl."""

    VERIFY_EVERY_S = 5  # W1 B5: while holding, the foreground is read back this often

    def __init__(self, spec, armed_at, runtime, run, ui, foreground=None):
        self.after, self.hold = spec["after_s"], spec["hold_s"]
        self.armed_at, self.runtime, self.run, self.ui = armed_at, Path(runtime), Path(run), ui
        self.state, self.since = "waiting", None
        self.foreground, self.front_hwnd, self.checked = foreground, None, None

    def _verify(self, hwnd, lost=False):
        """W1 B5: the bench PROVES her window is the active one by reading the foreground back (bench_focus front);
        a denied move or a lost focus is named, and check 27 is not reached without a front row."""
        try:
            if self.foreground is None:
                from scripts.m1_operator import win_facts
                fg = win_facts.foreground_app()
            else:
                fg = self.foreground()
            front = isinstance(fg, dict) and hwnd is not None and str(fg.get("hwnd")) == str(hwnd).strip()
        except Exception:  # noqa: BLE001 - an unreadable foreground is not a front window
            front = False
        self._row(ev="bench_focus", window="farmhand", hwnd=hwnd,
                  state="front" if front else "lost" if lost else "denied")
        return front

    def _row(self, **fields):
        with ipc.open_shared(self.run / "focus.jsonl", "a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(utc=utc(), **fields)) + "\n")

    def _fg(self, role, log):
        hwnd = None
        try:
            hwnd = ipc.read_text(self.runtime / ("fh-hwnd.txt" if role == "farmhand" else "host-hwnd.txt")).strip()
            self.ui(["fg", hwnd], self.run / f"focus-{log}-{time.time_ns()}.log")
            self._row(ev="focus", window=role, hwnd=hwnd, ok=True)
        except (RuntimeError, OSError) as exc:
            self._row(ev="focus", window=role, hwnd=hwnd, ok=False, error=str(exc)[:200])
        if role == "farmhand":
            self.front_hwnd = hwnd
            self._verify(hwnd)

    def tick(self, now):
        if self.state == "waiting" and now - self.armed_at >= self.after:
            self.state, self.since = "holding", now
            self._fg("farmhand", "farmhand")
            self.checked = now
        elif self.state == "holding" and now - self.since >= self.hold:
            self.release()
        elif self.state == "holding" and self.checked is not None and now - self.checked >= self.VERIFY_EVERY_S:
            self.checked = now
            self._verify(self.front_hwnd, lost=True)

    def release(self):
        if self.state == "holding":
            self.state = "released"
            self._fg("host", "host")


LAUNCHES = (("farmhand", "farmhand-launch.json", "fh-hwnd.txt"), ("host", "batch-host/host-launch.json", "host-hwnd.txt"))
GAME_WINDOW_WAIT_S, GAME_EXIT_WAIT_S, LAUNCH_START_SLACK_S = 180, 60, 30


def game_facts(pid):
    """One read of a process: name, start (UTC epoch), main window and its title; None when it is gone."""
    # One plain %-formatted string: an f-string on the first line only left '}}' doubled in the rest (benchrun4).
    out = ps("$p=Get-Process -Id %d -ErrorAction SilentlyContinue; if($p){[pscustomobject]@{"
             "name=$p.ProcessName;start=[DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds();"
             "hwnd=[int64]$p.MainWindowHandle;title=$p.MainWindowTitle}|ConvertTo-Json -Compress}; exit 0" % int(pid))
    if not out:
        return None
    row = json.loads(out)
    return dict(row, start=row["start"] / 1000)


def close_launched_games(run, runtime, ui, *, facts=game_facts, clock=time.monotonic, sleep=time.sleep,
                         window_wait_s=GAME_WINDOW_WAIT_S, exit_wait_s=GAME_EXIT_WAIT_S):
    """ADR 242 follow-up: close every game this run launched, by its own launch record, with the recorded close
    (WM_CLOSE to its game window, never a kill), and return the ones still running.

    Benchrun3: the farmhand launch exited rc 3 after 90 s (no fh-hwnd.txt), its process kept loading and was left
    open, so the baseline stayed unrestored. A launch record names its pid even when the launch failed; the game
    window is taken from the process (title "Stardew Valley ...") and waited for while the process lives.
    A pid is touched only when its process is StardewModdingAPI and started within 30 s of its launch record.
    """
    left, rows = [], []
    real_facts = facts

    def facts(pid):  # a failed process read is never "gone": it is named and the game counts as left open
        try:
            return real_facts(pid)
        except Exception as exc:  # noqa: BLE001
            return dict(name="unknown", start=None, hwnd=0, title="", read_error=str(exc)[:200])

    for role, record, hwnd_file in LAUNCHES:
        path = Path(runtime) / record
        if not path.is_file():
            continue
        launch = read(path)
        pid, at = launch.get("pid"), launch.get("utc")
        seen = facts(pid) if type(pid) is int else None
        if seen is None:
            rows.append(dict(role=role, pid=pid, state="not_running"))
            continue
        launched = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp() if at else None
        if seen.get("read_error"):
            rows.append(dict(role=role, pid=pid, state="read_failed", error=seen["read_error"]))
            left.append(dict(role=role, pid=pid, why="process read failed; WM_CLOSE not sent: " + seen["read_error"]))
            continue
        if (seen.get("name") != "StardewModdingAPI" or launched is None
                or abs(seen["start"] - launched) > LAUNCH_START_SLACK_S):
            rows.append(dict(role=role, pid=pid, state="not_ours", seen=seen))
            continue  # a reused pid or an unproven launch is never touched
        hwnd = ipc.read_text(Path(runtime) / hwnd_file).strip() if (Path(runtime) / hwnd_file).is_file() else None
        end = clock() + window_wait_s
        while not hwnd and seen is not None:
            if str(seen.get("title") or "").startswith("Stardew Valley") and seen.get("hwnd"):
                hwnd = str(seen["hwnd"])
                break
            if clock() >= end:
                break
            sleep(2)
            seen = facts(pid)
        if seen is None:
            rows.append(dict(role=role, pid=pid, state="exited_before_close"))
            continue
        if not hwnd:
            rows.append(dict(role=role, pid=pid, state="no_game_window", waited_s=window_wait_s))
            left.append(dict(role=role, pid=pid, why=f"no game window within {window_wait_s} s; WM_CLOSE not sent"))
            continue
        log = Path(run) / f"close-{role}.log"
        if log.exists():
            log = Path(run) / f"close-{role}-{time.time_ns()}.log"
        try:
            ui(["close", hwnd, "--expect-pid", pid], log)
            rows.append(dict(role=role, pid=pid, hwnd=hwnd, state="close_sent"))
        except RuntimeError as exc:
            rows.append(dict(role=role, pid=pid, hwnd=hwnd, state="close_refused", error=str(exc)[:200]))
    end = clock() + exit_wait_s
    sent = [r for r in rows if r["state"] in ("close_sent", "close_refused")]
    while sent and clock() < end and any(facts(r["pid"]) for r in sent):
        sleep(1)
    for row in sent:
        if facts(row["pid"]):
            left.append(dict(role=row["role"], pid=row["pid"],
                             why=f"still running {exit_wait_s} s after WM_CLOSE ({row['state']})"))
    write(Path(run) / "close-games.json", dict(utc=utc(), games=rows, left=left))
    return left


def recover_leftover(run, cfg, ui, *, facts=game_facts, running=None, **close):
    """ADR 242 follow-up: the deferred restoration of a sitting whose soak stopped before restoring (benchrun3: a
    game still up). Close what its launch records name, then the soak's own finalization: native-final witnesses,
    farm memory, Baseline.from_record(...).restore() verified, and the rig lock when it is this run's.
    A game that cannot be closed is written to LEFT_OPEN.json and raises; nothing is restored under a live game."""
    run = Path(run)
    running = running or smapi_running
    left = close_launched_games(run, run / "runtime", ui, facts=facts, **close) if (run / "runtime").is_dir() else []
    if left or running():
        write(run / "LEFT_OPEN.json", dict(utc=utc(), left=left, why="a Stardew process still runs after the close"))
        raise RuntimeError("GAME LEFT OPEN: " + ("; ".join(f"pid {g['pid']} ({g['role']}): {g['why']}" for g in left)
                                                 or "a Stardew process still runs"))
    if (run / "restore.json").is_file() or not (run / "baseline.json").is_file():
        return read(run / "restore.json") if (run / "restore.json").is_file() else None
    app, game = Path(cfg["appData"]), Path(cfg["game"])
    inbox = Path(read(game / "Mods/ArtiStardewBridge/config.json")["CommandInboxDirectory"])
    native_final = run / "native-final"
    native_final.mkdir(exist_ok=True)
    for name in ("session.json", "result.json", "halt.json", "mod_exit.json", "rearm.json", "stop.json", "stop_all.json",
                 "control.json"):
        if (inbox / name).is_file():
            shutil.copy2(inbox / name, native_final / name)
    write(native_final / "MANIFEST.json", manifest(native_final))
    owner_idle(cfg)
    baseline = Baseline.from_record(run, app / "Saves")
    try:
        if (run / "memory-runtime.json").is_file() and not farm_memory.bundle_path(run, read(run / "STAGING.json")).exists():
            farm_memory.preserve(run, read(run / "STAGING.json"))
    finally:
        result = baseline.restore()
    # As the soak's own finish: only closed, verified evidence copies (benchrun4 kept ~540 MB raw per sitting).
    for folder in (run / "preserved-saves", run / "template", *(run / ("preserved-" + n) for n in baseline.file_sets)):
        storage.compact(folder, run)
    lock = Path(cfg["root"]) / "rig/free-play.lock"
    if lock.is_file() and Path(read(lock)["output"]).resolve() == run.resolve():
        lock.unlink()
    write(run / "recovered.json", dict(utc=utc(), restore=result, lock_released=not lock.exists()))
    return result


def witness_marker(a):
    """ADR 132: the run folder says it is a witness; evaluate refuses it and the scoreboard skips it."""
    return dict(purpose="witness", tag="STAGING", credit=False, scored=False, kind=a.witness, plan="WITNESS_ISOLATED_PLAN",
                budget_minutes=round(a.hours * 60, 3), max_game_days=1, template=str(a.template))


def helper_gifts(a):
    """ADR 132: no HOST gift question or staged Wood inside a witness; it is judged alone and the bag stays as staged."""
    if getattr(a, "witness", None):
        return dict(GiftTimes=[], StageHostGiftCount=0)
    if getattr(a, "bench", None):
        at = a.bench_start.get("host_gift_at")
        return dict(GiftTimes=[at] if at else [], StageHostGiftCount=1 if at else 0)
    return dict(GiftTimes=[900, 1300], StageHostGiftCount=20)


def _run_soak(a):
    bench = getattr(a, "bench", None)
    cfg, rig = checked_rig(ROOT) if bench else (
        checked_rig(a.rehearsal_checkout) if getattr(a, "rehearsal_checkout", None) else checked_rig())
    if smapi_running(): raise RuntimeError("SMAPI occupied; no staging or launch")
    prep, game = rig / "operator-staging", Path(cfg["game"])
    for stop in (rig / "STOP", prep / "run/SOAK_STOP"):
        if stop.exists(): raise RuntimeError("global STOP: " + str(stop))
    process_json = ps("Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json")
    processes=json.loads(process_json or "[]")
    if isinstance(processes,dict): processes=[processes]
    if any(p.get("ProcessId") not in {os.getpid(),os.getppid()} and any(s in (p.get("CommandLine") or "") for s in ("prep_trial.py", "soak.py", "pad_watch.py", "poll_state.py", "live_cycle.py", "free_play/cycle.py", "free_play\\cycle.py")) for p in processes):
        raise RuntimeError("an operator reader/runner is already alive")
    original_bytes = 0
    continuation = getattr(a, 'continue_from', None)
    checkpoint = getattr(a, 'checkpoint', None)
    verify_expect = getattr(a, 'verify_expect', None)
    verifier = None
    if checkpoint and not continuation:
        raise ValueError('--checkpoint requires --continue-from')
    if verify_expect:
        if not continuation:
            raise ValueError('--verify-expect requires --continue-from')
        if getattr(a, 'enroll_only', False) or getattr(a, 'q12_drill', False) or getattr(a, 'witness', None):
            raise ValueError('--verify-expect requires a gameplay soak, not enrollment/drill/witness')
        from scripts.free_play.savestate import resolve_source, verify
        from scripts.free_play.expectations import JournalCheck
        expect_source = resolve_source(verify_expect)
        expect_manifest = verify(expect_source)
        verifier = JournalCheck(a.output, expect_manifest['expect'])
    if continuation:
        if a.staging_script or getattr(a, 'q12_drill', False) or getattr(a, 'witness', None):
            raise ValueError('continuation cannot be combined with fixture/debug staging or witnesses')
        from scripts.free_play.carryover import inspect_candidate
        from scripts.free_play.savestate import resolve_source
        continuation = resolve_source(continuation)
        candidate = inspect_candidate(continuation, a.farm, checkpoint)
        if bench and (candidate['save_id'], candidate['save_folder']) != a.bench_identity:
            raise RuntimeError("bench candidate native identity changed")
        if verify_expect and (candidate['hashes'] != expect_manifest['save_hashes']
                              or farm_memory.identity(candidate) != expect_manifest['identity']):
            raise ValueError('verification must start from the savestate native checkpoint/identity')
        bundle = farm_memory.inspect_bundle(continuation, candidate)
        if bundle:
            original_bytes += 3 * sum(storage.file_size(storage.safe_child(bundle[0], name))
                                      for name in bundle[1]['hashes'])
        snapshot = farm_memory.checkpoint_path(continuation, candidate, candidate['hashes'][candidate['save_folder']])
        saved_memory = farm_memory.inspect_folder(snapshot, candidate)
        if saved_memory:
            original_bytes += 3 * sum(storage.file_size(storage.safe_child(snapshot, name))
                                      for name in saved_memory[1]['hashes'])
        # Reserve both the immutable chosen-start archive and its mutable copy.
        original_bytes += 2 * sum(storage.file_size(storage.safe_child(candidate['source'], name))
                                  for name in candidate['hashes'])
    owned_saves = (candidate['save_folder'],) if continuation else (a.template.resolve().name,)
    original_bytes += storage.restore_bytes(Path(cfg['appData']) / 'Saves',
        Path(os.environ['LOCALAPPDATA']) / 'ArtiStardewBridge', owned_saves=owned_saves)
    disk = storage.admission(rig, a.hours, original_bytes)
    if not disk["allowed"]:
        raise RuntimeError("disk projection would cross 10 GiB before session completion: " + json.dumps(disk))
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if head != a.code_sha or subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT):
        raise RuntimeError("candidate checkout must be clean and exactly --code-sha")
    if sha(a.dll) != a.dll_sha256: raise RuntimeError("candidate DLL mismatch")
    # Require an auditable source-pair assertion, never equate an arbitrary DLL hash with a commit.
    provenance = read(a.provenance)
    if provenance.get("helper_sha256") != a.helper_sha256:
        raise RuntimeError("mod preflight: helper spec/provenance hash mismatch")
    staged_mod_preflight(((a.helper, "ArtiHostHelper.dll", a.helper_sha256),))
    dependencies = candidate_dependencies(a.dll, provenance)
    source = provenance["source_sha"]
    if provenance["dll_sha256"] != a.dll_sha256 or subprocess.check_output(["git", "diff", source, head, "--", "stardew-mod/ArtiStardewBridge", "stardew-mod/ArtiCore.Pure", "contracts"], cwd=ROOT):
        raise RuntimeError("DLL provenance/source-tree mismatch")
    run = a.output.resolve()
    if run.exists(): raise RuntimeError("output must be new; never replay an interrupted run")
    run.mkdir(parents=True)
    if bench:
        write(run / "BENCH.json", dict(tag="STAGING", scored=False, check=bench.spec["id"]))
    if verifier:
        write(run / 'verify-expect.json', dict(savestate=str(expect_source), manifest_sha256=sha(expect_source / 'manifest.json'),
              expect=expect_manifest['expect'], status='PENDING'))
    if getattr(a, "witness", None):
        write(run / "WITNESS.json", witness_marker(a))
    if getattr(a, "rehearsal_checkout", None):
        write(run / "rehearsal-state.json", dict(staging_started=False, voice_spawn_attempted=False))
        write(run / "ct-alert-target.json", dict(path=str(rig / "rig/CT-ALERT.json")))
        # Stable target for CT checks even if this process dies before cleanup can alert.
        write(rig / "rig/CT-ALERT.json", dict(state="IN_PROGRESS", run=str(run),
              requires="reconcile if the lock's owner exits before this file is cleared"))
        # Survives a hard kill, a second interrupt, or any early cleanup exception.
        write(run / "UNSAFE_NOT_RESTORED", dict(state="IN_PROGRESS", requires="verified cleanup before reuse"))
    write(run / "disk-preflight.json", disk)
    app = Path(cfg["appData"])
    if app.resolve() != (Path(os.environ["APPDATA"]) / "StardewValley").resolve():
        raise RuntimeError("configured AppData must be the actual Windows user's Stardew data")
    mod = game / "Mods/ArtiStardewBridge"
    files = {"bridge.dll": mod / "ArtiStardewBridge.dll", "mod.json": mod / "config.json", "trial.json": prep / "trial-config.json", "approval.json": prep / "approval.json", "preferences": app / "startup_preferences"}
    inbox = Path(read(mod / "config.json")["CommandInboxDirectory"])
    if (inbox / "stop.json").exists(): raise RuntimeError("command STOP exists; reconcile before staging")
    for name in ("session.json", "request.json", "result.json", "stop.json"):
        files["inbox-" + name] = inbox / name
    # Preserve the original nine entries and N1's additional persisted state/dependencies.
    for name in N1_RESTORED:
        files["n1-" + name] = inbox / name
    for name in dependencies:
        files["dependency-" + name] = mod / name
    baseline = Baseline(run, files, app / "Saves", owned_saves=owned_saves, file_sets=journal_file_sets(
        Path(os.environ["LOCALAPPDATA"]) / "ArtiStardewBridge",
        {"discoveries": (mod / "data", "discoveries-*.json"),
         "occurrences": (inbox, "occurrences.*.json"),
         "nights": (inbox, "nights.*.json")})); baseline.capture()
    env = dict(os.environ, ARTI_M1_PREP=str(prep), ARTI_M1_RUN=str(run), ARTI_M1_WORKTREE=str(ROOT), ARTI_CHECKOUT=str(ROOT), ARTI_VENV_PYTHON=sys.executable, ARTI_STARDEW_GAME=str(game), ARTI_M1_TOOLS=str(ROOT / "scripts/m1_operator"), ARTI_HOST_SMAPI_LOG=str(app / "ErrorLogs/SMAPI-latest.txt"), ARTI_FARMHAND_SMAPI_LOG=str(app / "ErrorLogs/SMAPI-latest.player-2.txt"))
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if bench:
        env["ARTI_STARDEW_SAVES"] = str(app / "Saves")
        env["ARTI_STARDEW_STARTUP_PREFS"] = str(app / "startup_preferences")
    real_path = bool(getattr(a, "real_path", False))
    owned = False
    realpath = None
    co_tenant = None
    try:
        owner_idle(cfg)
        if getattr(a, "rehearsal_checkout", None):
            write(run / "rehearsal-state.json", dict(staging_started=True, voice_spawn_attempted=False))
        if continuation:
            from scripts.free_play.carryover import stage_continuation
            fixture = stage_continuation(continuation, app / 'Saves', a.farm, run, baseline, checkpoint)
        else:
            fixture = stage_template(a.template.resolve(), app / "Saves", a.farm, run, q12=getattr(a, "q12_drill", False))
        fixture['foreign_saves_left_untouched'] = baseline.record['foreign_saves_left_untouched']
        if bench and (fixture["save_id"], fixture["save_folder"]) != a.bench_identity:
            raise RuntimeError("bench staged native identity mismatch")
        native_counter = counter_from_save(app / 'Saves' / fixture['save_folder'] / fixture['save_folder'], fixture)
        if native_counter:
            fixture['timesUnconscious'] = native_counter['value']
        write(run / 'STAGING.json', fixture)
        import arti_state
        memory = farm_memory.prepare(run, fixture, mod / 'data', prior=continuation,
            personal_mind=None if bench else realpath_mind_dir(a.trial_config),
            personal_state=None if bench else arti_state.state_dir(read(a.trial_config), root=ROOT),
            inbox=inbox, **({"later_record": a.bench_record} if bench and a.bench_record else {}))
        if not continuation:
            farm_memory.capture_checkpoint(run, fixture, app / 'Saves' / fixture['save_folder'])
        # Explicit config beats env in arti_mind; pin both consumers to this farm.
        env["ARTI_MIND_DIR"] = memory['mind']
        if a.staging_script:
            fixture["declared_debug_script"] = dict(path=str(a.staging_script.resolve()),sha256=sha(a.staging_script),target=a.staging_target)
            shutil.copy2(a.staging_script,run / "STAGING-debug.txt")
            write(run / "STAGING.json",fixture)
        env["ARTI_M1_SAVE_NAME"] = fixture["save_folder"]
        shutil.copy2(a.dll, mod / "ArtiStardewBridge.dll")
        for name, source in dependencies.items():
            shutil.copy2(source, mod / name)
        # Existing named farmhand selection requires the native join-screen facts.
        # This flag publishes those facts; no character is created by this harness.
        join_config = read(mod / "config.json")
        join_before = sha(mod / "config.json")
        previous_join_flag = join_config.get("CharacterCreationEnabled")
        join_config["CharacterCreationEnabled"] = True
        if bench:
            join_config["UiPadMenusEnabled"] = True
        flags = trial_mod_flags(read(a.trial_config), getattr(a, "rehearsal_trial_defaults",
                                                              REALPATH_TRIAL_DEFAULTS) if real_path else {})
        if "GameTextDeliveryEnabled" in join_config:
            join_config["GameTextDeliveryEnabled"] = False
        join_config.update(flags)
        if getattr(a, "q12_drill", False):
            join_config["Q12BackendEdgesEnabled"] = True
        write(mod / "config.json", join_config)
        write(run / "STAGING-join-config.json", dict(tag="STAGING", utc=utc(),
              before_sha256=join_before, after_sha256=sha(mod / "config.json"),
              field="CharacterCreationEnabled", before=previous_join_flag, after=True,
              reason="publish native existing named farmhand join-slot facts; baseline restores exact config"))
        trial_config = dict(read(a.trial_config), stardew_host_controller="harness",
                            **(getattr(a, "rehearsal_trial_defaults", REALPATH_TRIAL_DEFAULTS) if real_path else {}))
        trial_config.update(arti_mind_dir=memory['mind'], arti_state_dir=memory['state'])
        if trial_config.get("stardew_decision_provider_kind") == "openai_api":
            # One cap/fallback latch for the entire soak, including day-process restarts.
            trial_config["stardew_openai_session_state_file"] = str(run / "decision-api-session.json")
        write(prep / "trial-config.json", trial_config)
        host_template = run / "template"
        shutil.copytree(a.host_template, host_template)
        shutil.copy2(a.dll, host_template / "host-mods/ArtiStardewBridge/ArtiStardewBridge.dll")
        for name, source in dependencies.items():
            shutil.copy2(source, host_template / "host-mods/ArtiStardewBridge" / name)
        env["ARTI_M1_DLL"] = str(host_template / "host-mods/ArtiStardewBridge/ArtiStardewBridge.dll")
        # prepare-run requires an empty run directory; use a dedicated runtime subdirectory.
        runtime = run / "runtime"; env["ARTI_M1_RUN"] = str(runtime)
        call("witness_run.py", ["prepare-run", "--run-dir", runtime, "--template", host_template, "--sha256", a.dll_sha256, "--row", "free-play", "--snapshot", "unused-no-restore"], run / "prepare.log", env)
        helper_dir = runtime / "batch-host/host-mods/ArtiHostHelper"
        shutil.copytree(a.helper, helper_dir)
        host_out = run / "host"
        write(helper_dir / "config.json", dict(Enabled=True, RunId=run.name, OutputDirectory=str(host_out), SaveFolder=fixture["save_folder"], SaveId=fixture["save_id"], HostId=fixture["host_id"], FarmhandId=fixture["farmhand_id"], HostDelaySeconds=a.host_delay, StartupPauseSeconds=300, SleepAt=2400, GiftWhen="idle", GiftIdleSeconds=20, GiftItem="(O)388", **helper_gifts(a)))
        trial_profile=read(prep / "trial-config.json")
        profile={k:trial_profile.get(k) for k in ("stardew_decision_provider_kind","stardew_decision_model","stardew_decision_effort")}
        write(run / "request.json", dict(code_sha=head, dll_sha256=a.dll_sha256, helper_sha256=a.helper_sha256, farm=a.farm, hours=a.hours, days=a.days, started_utc=utc(), staging=fixture, profile=profile, trial_config_sha256=sha(prep / "trial-config.json"), mod_config_sha256=sha(mod / "config.json"), no_operator_rescue_policy=True, real_path=real_path))
        staged_mod_preflight(((mod, "ArtiStardewBridge.dll", a.dll_sha256),
            (runtime / "batch-host/host-mods/ArtiStardewBridge", "ArtiStardewBridge.dll", a.dll_sha256),
            (helper_dir, "ArtiHostHelper.dll", a.helper_sha256)))
        owned = True
        owner_idle(cfg)
        call("witness_run.py", ["launch-host", "--recovery", runtime / "batch-host"], run / "launch-host.log", env)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if snapshot_exists(host_out / "HOST_STOP"): raise RuntimeError("HOST helper stopped")
            if snapshot_exists(host_out / "status.json"): break
            time.sleep(.5)
        else: raise RuntimeError("HOST load timed out")
        write(run / "helper-preflight.json", helper_started_preflight(host_out))
        startup_pause(host_out, run.name)
        host_pid = (read(runtime / "batch-host/host-launch.json") or {}).get("pid")             if (runtime / "batch-host/host-launch.json").is_file() else None
        owner_idle(cfg, run)
        if real_path and not getattr(a, "enroll_only", False) and not getattr(a, "q12_drill", False):
            # After the LAST startup owner-idle check (its CIM scan would flag any arti_bridge python,
            # this one included) and before the first day; READY is awaited just before ARMED.
            realpath = getattr(a, "rehearsal_bridge_factory", RealPathBridge)(run, env)
            realpath.spawn(realpath_bridge_config(run, env["ARTI_MIND_DIR"], runtime,
                                                  getattr(a, "realpath_bridge_config", None)))
        call("witness_run.py", ["launch-farmhand", "--join-slot", "name:" + fixture["farmhand_name"]], run / "launch-farmhand.log", env)
        if getattr(a, "operator_rearm", False):
            operator_rearm_before_enrollment(inbox, run, test_operator=True)
        if realpath is not None:
            realpath.wait_ready()
        startup_pause(host_out, run.name, release=True)
        expected_save = ["--expected-save-id", str(a.bench_identity[0])] if bench else []
        call("witness_run.py", ["wait-ready", "--who", "farmhand", "--max-hour", "8", "--timeout", "30",
                               *expected_save], run / "ready.log", env)
        if __package__:
            from .drawable import DrawableWindows
        else:
            from drawable import DrawableWindows
        def window_record(row):
            with ipc.open_shared(run / "drawable-windows.jsonl", "a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(utc=utc(), **row)) + "\n")
        drawable = DrawableWindows.from_runtime(runtime, record=window_record)
        drawable.check()
        if bench:
            stage_bench(a, run, env)
            requirements = run / "STAGING-native.json"
            write(requirements, a.bench_start["native"])
            call("witness_run.py", ["wait-ready", "--who", "farmhand", "--max-hour", "25", "--timeout", "30",
                                   *expected_save, "--expected-pose", requirements], run / "STAGING-native.log", env)
        if a.staging_script:
            # The existing validator, screenshot gates and exact-ID UI containment remain authoritative.
            # No world override, owner-ok bypass, retry or post-approval staging is available here.
            call("stage_save.py", ["run-script",run / "STAGING-debug.txt","--target",a.staging_target,"--label","free-play-predeclared"],run / "STAGING-debug.log",env)
            call("witness_run.py", ["wait-ready", "--who", "farmhand", "--max-hour", "8", "--timeout", "30"], run / "post-staging-ready.log", env)
        # HOST in front and pad preflight are the existing containment policy.
        host_hwnd = ipc.read_text(runtime / "host-hwnd.txt").strip()
        call("ui_op.py", ["fg", host_hwnd], run / "host-fg.log", env)
        call("pad_watch.py", ["preflight"], run / "pad-preflight.log", env)
        cycle = runtime / "live7/c1"
        call("live_cycle.py", ["start-cycle", "--cycle-dir", cycle], run / "enroll.log", env)
        if bench and str(read(cycle / "cycle_state.json").get("save_id")) != expected_save[-1]:
            raise RuntimeError("bench enrolled native save identity mismatch")
        farm_memory.seed_cycle(run, cycle)
        if getattr(a, "enroll_only", False):
            # Normal finally owns WM_CLOSE and exact baseline restore; never arm or start a day.
            if read(cycle / "cycle_state.json").get("status") != "ready":
                raise RuntimeError("enrollment did not reach ready")
            write(run / "enrollment-only.json", dict(utc=utc(), status="ready", gameplay_started=False))
            return
        if getattr(a, "q12_drill", False):
            if read(cycle / "cycle_state.json").get("status") != "ready":
                raise RuntimeError("Q12 enrollment did not reach ready")
            from scripts.free_play.q12_drill import run_drill
            run_drill(inbox, run, drawable, keys=a.q12_keys.split(","), lb_start=a.q12_lb_start, rb_start=a.q12_rb_start)
            write(run / "enrollment-only.json", dict(utc=utc(), status="ready", gameplay_started=False, drill="Q12"))
            return  # normal harness finally closes only owned HWNDs and verifies baseline restore
        # Recheck after staging/enrollment as well: slow startup must never ARM a late day.
        call("witness_run.py", ["wait-ready", "--who", "farmhand", "--max-hour", "25" if bench else "8", "--timeout", "30",
                               *expected_save], run / "pre-arm-ready.log", env)
        if bench and a.bench_start.get("last"):
            stage_bench_last(a, run, runtime, env)
        ipc.publish(host_out / "ARMED", run.name, critical=True)
        start = time.monotonic(); end = start + a.hours * 3600; days = 0
        if bench and a.bench_start.get("focus"):
            a.bench_focus = FocusHold(a.bench_start["focus"], start, runtime, run,
                                      lambda args, log: call("ui_op.py", args, log, env))
        window = dict(start_at=time.time()); write(run / "window.json",window)
        recorder=JournalRecorder(run, hp_hard_stop=getattr(a, 'hp_hard_stop', False))
        def liveness_record(row):
            with ipc.open_shared(run / "host-liveness.jsonl", "a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(utc=utc(), **row)) + "\n")
        host_gaps = HostTelemetryGaps(liveness_record)
        disk_monitor = storage.Monitor(run, rig, a.hours, reserve=original_bytes + 512 * 1024**2,
                                       armed_at=start)
        # fix54J: owner-approved low priority for clip-ops (never stopped); fail-open, restored in finally.
        try:
            if not getattr(a, "rehearsal_checkout", None):
                co_tenant = cotenant.CoTenantPriority(run)
                co_tenant.start()
        except Exception as exc:  # noqa: BLE001 - a missing priority step never blocks the session
            write(run / "cotenant-priority-error.json", dict(utc=utc(), type=type(exc).__name__, detail=str(exc)[:300]))
        while time.monotonic() < end or (not getattr(a, "bench", None) and days < a.days):
            bench = getattr(a, "bench", None)
            env["ARTI_LIVE7_PROGRESS_AT"] = str(recorder.progress_at)
            stop_requested_at=None
            first_stop_reasons = None
            observed_stop_reasons = set()
            last_loop_wall, loop_stalled_until = None, 0.0
            with (run / f"day-{days+1:04d}.log").open("xb") as log:
                child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("cycle.py")), "--cycle-dir", str(cycle), "--host-output", str(host_out)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                while child.poll() is None:
                    recorder.poll()
                    if co_tenant is not None:
                        co_tenant.tick()  # per-clip ffmpeg children; internally every 20 s
                    if real_path:
                        realpath.check()
                    if getattr(a, "bench_focus", None) is not None:
                        a.bench_focus.tick(time.monotonic())
                    # Read-only files; never steal port 3017 or alter a goal/approval.
                    reasons=recorder.stop_reasons()
                    if getattr(a, "rehearsal_checkout", None):
                        realpath.game_sample(runtime, host_out)
                        if time.monotonic() >= end:
                            reasons.append("rehearsal_budget")
                    if getattr(a, "witness", None) and time.monotonic() >= end:
                        reasons.append("witness_budget")  # ADR 132: the wall budget really ends a witness
                    if verifier and time.monotonic() >= end:
                        reasons.append('verify_budget')
                    try:
                        drawable.check()
                    except RuntimeError as exc:
                        reasons.append("window_not_drawable:" + str(exc))
                    if (rig / "STOP").exists() or (prep / "run/SOAK_STOP").exists(): reasons.append("global_stop")
                    now_wall = time.time()
                    if last_loop_wall is not None and now_wall - last_loop_wall >= HARNESS_LOOP_STALL_S:
                        loop_stalled_until = now_wall + HOST_FREEZE_MACHINE_STALL_MAX_S
                    last_loop_wall = now_wall
                    liveness = host_liveness(host_out, now_wall, host_pid, machine_stalled=(
                        now_wall < loop_stalled_until or getattr(disk_monitor, "hold_start", None) is not None))
                    host_gaps.observe(liveness, now_wall)
                    if liveness["lost"]: reasons.append("host_stop_or_lost_telemetry:" + liveness["state"])
                    # ADR 123: one disk policy, including the hard restore floor;
                    # a separate low_disk cutoff would bypass the transient hold.
                    if not disk_monitor.sample()["allowed"]: reasons.append("disk_projection")
                    if time.monotonic()>end+1800: reasons.append("bounded_day_deadline")
                    if verifier:
                        result = verifier.poll(stop_reason=','.join(reasons) or None)
                        write(run / 'verify-result.json', result)
                        if result['status'] in ('PASS', 'FAIL'):
                            reasons.append('verify_' + result['status'].lower())
                        observed_stop_reasons.update(reasons)
                    if bench:
                        bench_verdict = bench.result()
                        if not reasons and bench_verdict["verdict"] != "PENDING":
                            reasons.append("bench_verdict")
                        if not reasons and time.monotonic() >= end:
                            reasons.append("bench_budget")
                        observed_stop_reasons.update(reasons)
                    if getattr(a, "rehearsal_checkout", None) and reasons:
                        observed_stop_reasons.update(reasons)
                        write(run / f"observed-stop-reasons-{days}.json", dict(reasons=sorted(observed_stop_reasons)))
                    if reasons and stop_requested_at is None:
                        first_stop_reasons = list(reasons)
                        recorder.request_stop(reasons, days, cycle, env)
                        stop_requested_at=time.monotonic()
                    if stop_requested_at is not None and time.monotonic()-stop_requested_at>45:
                        raise RuntimeError("trial did not finish within 45s of scoped STOP; no forced shutdown")
                    time.sleep(1)
                if getattr(a, "witness", None) and first_stop_reasons == ["witness_budget"]:
                    # Scoped STOP acknowledged by runner exit; a budget stop is no saved night and no fault.
                    write(run / "witness-budget-stop.json", dict(utc=utc(), runner_rc=child.returncode, reasons=first_stop_reasons))
                    break
                if verifier and stop_requested_at is not None:
                    # Existing scoped STOP and runner cleanup remain authoritative.
                    unsafe = sorted(observed_stop_reasons - {'verify_pass'})
                    result = verifier.poll(final=True, stop_reason=','.join(unsafe) or (
                        'day runner failed' if child.returncode else None))
                    write(run / 'verify-result.json', dict(result, final=False))
                    break
                if bench and stop_requested_at is not None:
                    stopped = read(cycle / "cycle_state.json").get("status") == "stopped"
                    unsafe = sorted(observed_stop_reasons - {"bench_verdict", "bench_budget"})
                    if unsafe or child.returncode or not stopped:
                        raise RuntimeError("bench stop unverified: " + ",".join(unsafe or ["cycle did not acknowledge STOP"]))
                    break
                if bench and stop_requested_at is None and bench.result()["verdict"] != "PENDING":
                    # ADR 242: a drain (hands_not_engaged, menu_held_recover, ADR 241's rung 5) ends the runner by
                    # itself; when the journal has already decided the check, that is the sitting's end, not a fault.
                    write(run / "bench-runner-ended.json", dict(utc=utc(), runner_rc=child.returncode,
                          verdict=bench.result()["verdict"], cycle_status=read(cycle / "cycle_state.json").get("status")))
                    break
                if (getattr(a, "rehearsal_checkout", None) and stop_requested_at is not None
                        and "rehearsal_budget" in (first_stop_reasons or [])):
                    from scripts.livepath.safety import budget_outcome
                    outcome = budget_outcome(run / f"day-{days+1:04d}.log", first_stop_reasons, child.returncode)
                    if observed_stop_reasons != {"rehearsal_budget"}:
                        outcome = dict(status="FAIL", reason="nonbudget_stop_observed")
                    write(run / "rehearsal-budget-stop.json", dict(utc=utc(), runner_rc=child.returncode,
                          first_reasons=first_stop_reasons, observed_reasons=sorted(observed_stop_reasons), **outcome))
                    if outcome["status"] == "FAIL" or observed_stop_reasons != {"rehearsal_budget"}:
                        raise RuntimeError("rehearsal stopped for another reason or day runner failed")
                    break  # scoped STOP acknowledged by runner exit; no saved-night credit
                if getattr(a, "rehearsal_checkout", None) and first_stop_reasons:
                    raise RuntimeError("rehearsal stopped before budget: " + ",".join(first_stop_reasons))
                if child.returncode: raise RuntimeError("day runner stopped: " + str(child.returncode))
            recorder.poll()
            if recorder.stop_reasons():
                if stop_requested_at is None:
                    recorder.request_stop(recorder.stop_reasons(), days, cycle, env)
                if verifier:
                    write(run / 'verify-result.json', verifier.poll(final=True, stop_reason=','.join(recorder.stop_reasons())))
                if bench:
                    raise RuntimeError("bench native safety stop: " + ",".join(recorder.stop_reasons()))
                break  # includes a counter delta saved while the day runner was exiting
            saved = read(cycle / "cycle_state.json")["days"][-1]
            saved_day_outcome(saved)
            storage.compact_day(run, cycle, saved)
            # compact_day verifies the native after-save receipt and file hashes;
            # freeze all memory before the next iteration can start a day runner.
            farm_memory.capture_checkpoint(run, fixture, Path(saved['checkpoint']) / 'save')
            days += 1
            if bench and bench.result()["verdict"] != "PENDING":
                break
            if verifier:
                result = verifier.poll(final=time.monotonic() >= end)
                write(run / 'verify-result.json', dict(result, final=False))
                if result['status'] != 'PENDING':
                    break
            if getattr(a, "rehearsal_checkout", None) and time.monotonic() >= end:
                break
            if getattr(a, "witness", None):
                break  # ADR 132: one game day at most; the night is the other end of a witness
        write(run / "completed.json", dict(started_utc=read(run / "request.json")["started_utc"], ended_utc=utc(), elapsed_s=time.monotonic()-start, days=days))
    finally:
        # Stop trial first, await its normal cleanup. Never kill or close while the day runner is alive.
        if getattr(a, "bench_focus", None) is not None:
            try:
                a.bench_focus.release()  # ADR 242: focus is always handed back to the HOST
            except Exception:  # noqa: BLE001 - a failed hand-back is in focus.jsonl; it never blocks cleanup
                pass
        try:
            active = locals().get("child")
            if active is not None and active.poll() is None:
                stoplog = run / f"cleanup-stop-{time.time_ns()}.log"
                call("live_cycle.py", ["stop", "--yes", "--cycle-dir", cycle], stoplog, env)
                active.wait(timeout=45)
        finally:
            if co_tenant is not None:
                try:
                    co_tenant.restore("harness_finally")
                except Exception as exc:  # noqa: BLE001 - cleanup/restore must still run
                    write(run / "cotenant-restore-error.json", dict(utc=utc(), type=type(exc).__name__, detail=str(exc)[:300]))
        if locals().get("realpath") is not None:
            try:
                realpath.stop()
            except Exception as exc:  # noqa: BLE001 - game cleanup/restore must still run
                write(run / "realpath-stop-error.json", dict(utc=utc(), type=type(exc).__name__, detail=str(exc)[:300]))
        runtime = run / "runtime"
        if "window" in locals():
            recorder.poll(); window["end_at"]=time.time(); write(run / "window.json",window)
        if owned:
            # Independent file evidence only; no bridge readers during trial ownership.
            for role, source in (("host", app / "ErrorLogs/SMAPI-latest.txt"), ("farmhand", app / "ErrorLogs/SMAPI-latest.player-2.txt")):
                if source.exists(): shutil.copy2(source, run / (role + "-SMAPI.txt"))
            if smapi_running():
                barrier=run / "host/barrier.json"
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    try:
                        if time.time()-snapshot_stat(barrier).st_mtime<5 and read(barrier).get("saving") is False:
                            break
                    except (OSError, ValueError):
                        pass  # no safety credit; retry within the existing save-barrier deadline
                    time.sleep(.5)
                else:
                    raise RuntimeError("save barrier unresolved; do not close games or restore; baseline retained")
            # ADR 242 follow-up: every launched game by its launch record, also after a failed launch (benchrun3).
            left_open = close_launched_games(
                run, runtime, lambda args, log: call("ui_op.py", ["close", *args[1:]], log, env))
            deadline = time.monotonic() + 30
            while smapi_running() and time.monotonic() < deadline: time.sleep(1)
        if (run / "host").exists(): ipc.publish(run / "host/STOP", "harness finalization", critical=True)
        if smapi_running():
            write(run / "LEFT_OPEN.json", dict(utc=utc(), left=locals().get("left_open") or [],
                                               why="a Stardew process still runs after the recorded close"))
            raise RuntimeError("SMAPI remains: preserve baseline; restoration deferred, do not kill")
        # Preserve N1's actual final witnesses before restoring the legacy mailbox.
        native_final = run / "native-final"; native_final.mkdir(exist_ok=True)
        for name in ("session.json", "result.json", "halt.json", "mod_exit.json", "rearm.json", "stop.json", "stop_all.json", "control.json"):
            if (inbox / name).is_file(): shutil.copy2(inbox / name, native_final / name)
        write(native_final / "MANIFEST.json", manifest(native_final))
        owner_idle(cfg)
        try:
            if (run / 'memory-runtime.json').is_file():
                farm_memory.preserve(run, fixture)
        finally:
            baseline.restore()
        # Only closed, verified evidence copies. Keep raw native/g1 journals and logs for all readers.
        for folder in (run / "preserved-saves", run / "template",
                       *(run / ("preserved-" + name) for name in baseline.file_sets)):
            storage.compact(folder, run)
        if (run / 'STAGING.json').is_file():
            from scripts.free_play.carryover import retain_candidate
            retain_candidate(run)
        for archive in run.glob("runtime/live7/*/day-*/journal/provider_archive"):
            storage.compact(archive, run)  # partial terminal day is closed too; never discard its inputs
        storage.pack_session_logs(run)  # closed report.json + farmhand-stdout; failure leaves raw + fact
        if runtime.exists():
            report(run)
        if verifier:
            # Exceptions, safety stops, or failed restore must never leave a PASS.
            failure = sys.exc_info()[0] is not None
            result = verifier.poll(final=True, stop_reason='harness exception' if failure else None)
            write(run / 'verify-result.json', result)
    if verifier and read(run / 'verify-result.json')['status'] != 'PASS':
        raise RuntimeError('savestate verification FAIL; see verify-result.json')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report"); r.add_argument("run", type=Path)
    d = sub.add_parser("drill", help="I1 booked schedule with recoverable day-runner continuation")
    d.add_argument("--booking", required=True, help="rig module:factory returning Runner and schedule")
    s = sub.add_parser("soak")
    s.add_argument("hours", type=parse_duration, help="duration: Nh, N.Nh, Nm, or XhYm")
    s.add_argument("farm", choices=("Standard", "Wilderness"))
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--continue-from", type=Path, help="explicit closed run: copy its preserved campaign save byte-for-byte; keep baseline restore and next-candidate manifest")
    s.add_argument('--checkpoint', help='native saved date in --continue-from, e.g. Y1-summer-19; default is end save')
    s.add_argument('--verify-expect', help='savestate id: stop with PASS on observed expectations, FAIL at budget/safety stop')
    s.add_argument('--hp-hard-stop', action='store_true', help='savestate debug only: stop at native HP<=10%% or KO; default observe-only')
    for name in ("dll", "helper", "template", "host-template", "trial-config", "provenance", "output"):
        s.add_argument("--" + name, type=Path, required=True)
    for name in ("code-sha", "dll-sha256", "helper-sha256"):
        s.add_argument("--" + name, required=True)
    s.add_argument("--host-delay", type=int, default=0)
    s.add_argument("--staging-script", type=Path)
    s.add_argument("--staging-target", choices=("host","farmhand"), default="farmhand")
    s.add_argument("--operator-rearm", action="store_true", help="test-operator startup containment re-arm before enrollment")
    s.add_argument("--enroll-only", action="store_true", help="verify enrollment then perform normal cleanup/restore before gameplay")
    s.add_argument("--real-path", action="store_true", help="also run the text-only bridge persona (arti_bridge test mode) on the same mind dir")
    s.add_argument("--realpath-bridge-config", type=Path, help="reviewed non-secret base for the bridge test config (optional)")
    s.add_argument("--q12-drill", action="store_true", help="nine HUD backend edges after enrollment, then harness restore")
    s.add_argument("--q12-keys", choices=("Start,LB,RB", "LB,RB", "RB"), default="Start,LB,RB")
    s.add_argument("--q12-lb-start", type=int, choices=(1, 2), default=1)
    s.add_argument("--q12-rb-start", type=int, choices=(1, 3), default=1)
    s.add_argument("--witness", choices=WITNESS_KINDS, help="isolated witness run (ADR 132): marked, never scored, ends at its budget")
    a = p.parse_args()
    if a.cmd == "drill":
        from scripts.free_play.i1_runner import drill
        return drill(a.booking)
    if a.cmd == "report": report(a.run)
    elif a.hours <= 0 or a.days < 1 or not 0 <= a.host_delay <= 60: p.error("positive duration/days, delay 0..60 required")
    elif a.witness and (a.days != 1 or a.hours > WITNESS_MAX_HOURS): p.error(f"a witness is --days 1 and at most {WITNESS_MAX_HOURS}h")
    else:
        output_existed=a.output.exists()
        try:
            run_soak(a)
        except Exception as exc:
            import traceback
            failure=dict(utc=utc(),reason=str(exc),type=type(exc).__name__,traceback=traceback.format_exc())
            # Even a preflight refusal has a receipt. Never overwrite an existing failure on a replay.
            if not output_existed:
                a.output.mkdir(parents=True,exist_ok=True)
                if not (a.output / "failure.json").exists(): write(a.output / "failure.json",failure)
                report(a.output)
            print(failure["traceback"],file=sys.stderr)
            return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
