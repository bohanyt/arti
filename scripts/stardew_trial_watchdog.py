"""Run-scoped emergency process cleanup, independent of bridge/agent liveness.

No import effects. The operator supplies exact process creation times and images
for this trial only. Retained Windows handles avoid killing a reused PID. This
does not drive gameplay, save the world, or certify graceful/native completion.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[1]))
from arti_runtime import ipc
import argparse
import ctypes
from ctypes import wintypes
import json
import hashlib
import math
import os
from pathlib import Path
import time


def process_identity(pid=None):
    """Read exact live Windows process identity; never broaden ownership by name."""
    if os.name != "nt":
        raise ValueError("windows_required")
    pid = os.getpid() if pid is None else pid
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        raise ValueError("identity_unavailable")
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if (not api.GetProcessTimes(handle, *[ctypes.byref(t) for t in times])
                or not api.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
                or api.WaitForSingleObject(handle, 0) != 258):
            raise ValueError("identity_unavailable")
        return dict(pid=pid, image=buf.value,
            created_filetime=(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        api.CloseHandle(handle)


class OperationLease:
    """Immutable controller-owned operation; silence alone cannot renew it.

    UI cap: V8's 76.19s observed startup span + 43.81s bounded margin.
    Bridge startup retains the existing 45s limit. Autonomous supervision is
    separately limited to 299s; a lease never overrides that supervisor or STOP.
    """
    CAPS = {"ui_setup": 120, "bridge_start": 45, "autonomous": 300}

    def __init__(self, *, run_id, controller, watchdog, containment_id, started, deadline):
        self.binding = dict(run_id=run_id, controller=controller,
            watchdog=watchdog, containment_id=containment_id)
        self.started, self.deadline = started, deadline
        self.last_now, self.last_pulse = started, 0
        self.active, self.used = None, set()

    def complete(self, operation_id, outcome, *, now):
        self.check(self.active, now=now)
        if (not self.active or self.active["operation_id"] != operation_id
                or outcome not in {"success", "failure"}):
            raise ValueError("operation_completion_mismatch")
        self.active = None

    def check(self, value, *, now, pulse=None):
        if (type(now) not in (int, float) or not math.isfinite(now)
                or now < self.last_now or now >= self.deadline):
            raise ValueError("operation_clock_invalid")
        self.last_now = now
        # Completion/clearing is itself a transition inside the old deadline.
        # A late caller cannot erase an expired lease before the next poll.
        if self.active is not None and now >= self.active["deadline_monotonic"]:
            raise ValueError("operation_expired")
        if pulse is not None:
            if (type(pulse) not in (int, float) or not math.isfinite(pulse)
                    or not self.last_pulse <= pulse <= now):
                raise ValueError("operator_clock_invalid")
            self.last_pulse = pulse
        if value is None:
            self.active = None
            if pulse is not None and now - pulse >= 25:
                raise ValueError("operator_lost")
            return
        if not isinstance(value, dict) or any(value.get(k) != v for k,v in self.binding.items()):
            raise ValueError("operation_identity_mismatch")
        op, kind = value.get("operation_id"), value.get("kind")
        start, end = value.get("started_monotonic"), value.get("deadline_monotonic")
        if (not isinstance(op,str) or not op or len(op)>128 or kind not in self.CAPS
                or any(type(t) not in (int,float) or not math.isfinite(t) for t in (start,end))
                or not self.started <= start <= now
                or not start < end <= min(self.deadline,start+self.CAPS[kind])):
            raise ValueError("operation_invalid")
        if now >= end:
            raise ValueError("operation_expired")
        if self.active is not None:
            if value != self.active:
                raise ValueError("operation_changed")
        else:
            if op in self.used or now-start >= 5 or (pulse is not None and now-pulse >= 25):
                raise ValueError("operation_admission_stale")
            self.used.add(op)
            self.active = dict(value)


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("TotalUserTime", "TotalKernelTime",
        "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
        (name, wintypes.DWORD) for name in ("TotalPageFaultCount", "TotalProcesses",
        "ActiveProcesses", "TotalTerminatedProcesses")]


class OwnedProcess:
    """Pin an exact process and contain its future children in an anonymous job.

    Registration must finish before the owned bridge starts any SDK child.
    Existing children are not retroactively captured and need their own slots.
    """

    def __init__(self, spec):
        if os.name != "nt":
            raise ValueError("windows_required")
        if type(spec.get("pid")) is not int or spec["pid"] <= 4 or spec["pid"] == os.getpid():
            raise ValueError("invalid_owned_pid")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
            ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL)]
        self.job = None
        self.handle = self.api.OpenProcess(0x1000 | 0x100000 | 0x1 | 0x100, False, spec["pid"])
        if not self.handle:
            raise ValueError("owned_process_unavailable")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.api.GetProcessTimes(self.handle, *[ctypes.byref(t) for t in times]):
                raise ValueError("owned_identity_unavailable")
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if not self.api.QueryFullProcessImageNameW(self.handle, 0, buf, ctypes.byref(size)):
                raise ValueError("owned_image_unavailable")
            if (created != spec.get("created_filetime")
                    or os.path.normcase(buf.value) != os.path.normcase(spec.get("image", ""))):
                raise ValueError("owned_identity_mismatch")
            if not self.alive():
                raise ValueError("owned_process_exited")
            self.job = self.api.CreateJobObjectW(None, None)
            if not self.job:
                raise ValueError("owned_job_unavailable")
            limits = _ExtendedLimits()
            # No BREAKAWAY flags; future ordinary subprocesses remain contained.
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.job, 9,
                    ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ValueError("owned_job_limits_failed")
            if not self.api.AssignProcessToJobObject(self.job, self.handle):
                raise ValueError("owned_job_assignment_failed")
        except BaseException:
            self.close()
            raise

    def alive(self):
        status = self.api.WaitForSingleObject(self.handle, 0)
        if status not in (0, 258):
            raise ValueError("owned_wait_failed")
        return status == 258

    def terminate(self):
        if not self.api.TerminateJobObject(self.job, 1):
            raise ValueError("owned_termination_failed")

    def contained(self):
        contained = wintypes.BOOL()
        if not self.api.IsProcessInJob(self.handle, self.job, ctypes.byref(contained)):
            raise ValueError("owned_containment_unavailable")
        return bool(contained.value)

    def descendants_alive(self):
        accounting = _Accounting()
        if not self.api.QueryInformationJobObject(self.job, 1,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ValueError("owned_job_readback_failed")
        return accounting.ActiveProcesses != 0

    def wait(self, milliseconds=5000):
        deadline = time.monotonic() + milliseconds / 1000
        while self.descendants_alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(.02)
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        return self.api.WaitForSingleObject(self.handle, remaining) == 0

    def close(self):
        if self.job:
            self.api.CloseHandle(self.job)
            self.job = None
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _heartbeat_subcondition(heartbeat, *, now, last_heartbeat):
    # Preserve the original short-circuit order and exact threshold.
    if type(heartbeat) not in (int, float):
        return "non_numeric"
    if not math.isfinite(heartbeat):
        return "nonfinite"
    if heartbeat < last_heartbeat:
        return "regressed"
    if heartbeat > now:
        return "future"
    if now - heartbeat >= 30:
        return "stale"
    return None


def stop_reason(*, now, started, deadline, heartbeat, last_heartbeat, stopped):
    if stopped:
        return "REQUESTED_STOP"
    if now < started or now >= deadline:
        return "WATCHDOG_DEADLINE"
    if _heartbeat_subcondition(heartbeat, now=now, last_heartbeat=last_heartbeat):
        return "CONTROLLER_LOST"
    return None


def cleanup_owned(processes):
    """Attempt every pinned cleanup even when one fails; truthful per-slot result."""
    results = []
    for process in processes:
        try:
            was_alive = process.alive() or getattr(process, "descendants_alive", lambda: False)()
            if was_alive:
                process.terminate()
            results.append({"termination_attempted": was_alive, "exited": process.wait()})
        except Exception:
            results.append({"termination_attempted": True, "exited": False})
    return results


def strict_json(data):
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result:
                raise ValueError("duplicate_config_key")
            result[key] = value
        return result
    def reject(value):
        raise ValueError("nonfinite_config_number")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            reject(value)
        return number
    return json.loads(data, object_pairs_hook=pairs, parse_constant=reject,
        parse_float=finite_float)


def semantic_identity(value):
    # Type tags prevent Python's True == 1 from hiding an actual config change.
    if isinstance(value,dict):
        return (dict, sorted((k,semantic_identity(v)) for k,v in value.items()))
    if isinstance(value,list):
        return (list, [semantic_identity(v) for v in value])
    return (type(value),value)


def restore_configs(specs):
    """After owned cleanup, restore exact bytes; allow only pinned JSON semantics."""
    results = []
    for spec in specs:
        try:
            target = Path(spec["path"])
            if target.name not in {"config.json", "config_local.json"}:
                raise ValueError("not_trial_config")
            original = ipc.read_bytes(Path(spec["original_path"]))
            if hashlib.sha256(original).hexdigest() != spec["original_sha256"]:
                raise ValueError("original_identity_changed")
            current_bytes = ipc.read_bytes(target)
            current = hashlib.sha256(current_bytes).hexdigest()
            if current == spec["original_sha256"]:
                results.append(True)
                continue
            if current != spec["trial_sha256"]:
                trial = ipc.read_bytes(Path(spec["trial_path"]))
                if hashlib.sha256(trial).hexdigest() != spec["trial_sha256"]:
                    raise ValueError("temporary_identity_changed")
                expected = semantic_identity(strict_json(trial))
                if (expected != semantic_identity(spec["trial_semantics"])
                        or expected != semantic_identity(strict_json(current_bytes))):
                    raise ValueError("temporary_config_drift")
            temporary = target.with_name(target.name + ".trial-restore.tmp")
            with temporary.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            results.append(hashlib.sha256(ipc.read_bytes(target)).hexdigest() == spec["original_sha256"])
        except Exception:
            results.append(False)
    return results


def run_watchdog(manifest, *, factory=OwnedProcess, clock=time.monotonic, sleep=time.sleep,
                 identity_factory=process_identity):
    """Hard ceiling plus explicit heartbeat; no human/log polling dependency."""
    started = clock()
    deadline = manifest["deadline_monotonic"]
    if type(deadline) not in (int, float) or not started < deadline <= started + 600:
        raise ValueError("invalid_watchdog_deadline")
    specs = manifest["processes"]
    if not 1 <= len(specs) <= 6 or len({s["pid"] for s in specs}) != len(specs):
        raise ValueError("invalid_owned_processes")
    folder = Path(manifest["control_dir"])
    processes = []
    pinned = {}
    expected_count = len(specs)
    registration_complete = False
    reason = "WATCHDOG_ERROR"
    controller_lost = None
    heartbeat_access = dict(failures=0, recovered_episodes=0)
    access_started, access_attempts = None, 0
    lease = None
    ready_extra = {}
    try:
        for spec in specs:
            processes.append(factory(spec))
            pinned[spec["pid"]] = dict(spec)
        registration_complete = True
        if "containment_id" in manifest:
            if len(specs) != 1 or not manifest["containment_id"]:
                raise ValueError("invalid_operation_containment")
            ready_extra = dict(watchdog=identity_factory(), controller=specs[0],
                containment_id=manifest["containment_id"], started_monotonic=started)
            lease = OperationLease(run_id=manifest["run_id"], controller=specs[0],
                watchdog=ready_extra["watchdog"], containment_id=manifest["containment_id"],
                started=started, deadline=deadline)
        ipc.publish(folder / "watchdog-ready.json", json.dumps({
            "run_id": manifest["run_id"], "pinned_processes": len(processes),
            "deadline_monotonic": deadline, **ready_extra}), critical=True)
        last = started - 30
        accepted_heartbeat = None
        last_now = started
        acknowledged = object()
        while True:
            registry = folder / "registrations.json"
            if registry.exists():
                registration_complete = False
                value = json.loads(ipc.read_text(registry, encoding="utf8"))
                additions = value["processes"]
                if value["run_id"] != manifest["run_id"] or len(additions) > 6:
                    raise ValueError("invalid_process_registration")
                for spec in additions:
                    if spec["pid"] in pinned:
                        if spec != pinned[spec["pid"]]:
                            raise ValueError("registered_identity_changed")
                    else:
                        if len(pinned) >= 6:
                            raise ValueError("owned_process_limit")
                        expected_count += 1
                        processes.append(factory(spec))
                        pinned[spec["pid"]] = dict(spec)
                registration_complete = True
                ipc.publish(folder / "watchdog-ready.json", json.dumps({
                    "run_id": manifest["run_id"], "pinned_processes": len(processes),
                    "deadline_monotonic": deadline, **ready_extra}), critical=True)
            raw = None
            read_failure = None
            access_denied = False
            try:
                # One read only: identity belongs to the bytes consumed by this poll.
                raw = ipc.read_bytes(folder / "heartbeat.json")
                value = json.loads(raw.decode("utf8"))
                heartbeat = value["monotonic"] if value.get("run_id") == manifest["run_id"] else None
                if value.get("run_id") != manifest["run_id"]:
                    read_failure = dict(subcondition="wrong_run_id")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                heartbeat = None
                access_denied = isinstance(exc, PermissionError)
                if isinstance(exc, OSError):
                    read_failure = dict(subcondition="missing" if isinstance(exc, FileNotFoundError)
                        else "read_failure", errno=exc.errno, winerror=getattr(exc, "winerror", None))
                elif isinstance(exc, KeyError):
                    read_failure = dict(subcondition="missing_key", key="monotonic")
                elif isinstance(exc, TypeError):
                    read_failure = dict(subcondition="invalid_type")
                else:
                    read_failure = dict(subcondition="parse_failure")
                    if isinstance(exc, json.JSONDecodeError):
                        read_failure["parse_position"] = exc.pos
                read_failure["exception_class"] = type(exc).__name__
            now = clock()
            retry_access = False
            if access_denied:
                heartbeat_access['failures'] += 1
                heartbeat_access['last_error'] = dict(now_monotonic=now, **read_failure)
                if access_started is None:
                    access_started = now
                access_attempts += 1
                # A brief Windows access conflict is not a new heartbeat. Only
                # an already validated sample can cover a bounded retry, and
                # every original deadline/STOP/process check still runs below.
                retry_access = (accepted_heartbeat is not None
                    and now - access_started < .5 and access_attempts <= 25)
                if retry_access:
                    heartbeat = accepted_heartbeat
            elif access_started is not None and now >= access_started + .5:
                # A delayed poll cannot resurrect the controller after the
                # access-recovery window, even if bytes became readable again.
                heartbeat = None
                read_failure = dict(subcondition='access_retry_expired')
            # Startup heartbeat may precede opening handles by a fraction.
            reason = stop_reason(now=now, started=started, deadline=deadline,
                heartbeat=heartbeat, last_heartbeat=last,
                stopped=(folder / "STOP").exists())
            if reason:
                if reason == "CONTROLLER_LOST":
                    controller_lost = dict(now_monotonic=now,
                        previous_accepted_heartbeat=accepted_heartbeat, comparison_heartbeat=last,
                        **(read_failure or dict(subcondition=_heartbeat_subcondition(
                            heartbeat, now=now, last_heartbeat=last))))
                    if raw is not None:
                        controller_lost["snapshot"] = dict(byte_length=len(raw),
                            sha256=hashlib.sha256(raw).hexdigest())
                    if read_failure is None:
                        numeric = type(heartbeat) in (int, float)
                        finite = math.isfinite(heartbeat) if numeric else None
                        candidate = dict(type=type(heartbeat).__name__, finite=finite)
                        if numeric:
                            candidate["value"] = heartbeat if finite else str(heartbeat)
                        controller_lost["candidate"] = candidate
                break
            if now < last_now:
                reason = "WATCHDOG_CLOCK_REGRESSED"
                break
            last_now = now
            if not retry_access:
                last = heartbeat
                accepted_heartbeat = heartbeat
            if not all(p.alive() for p in processes):
                reason = "OWNED_PROCESS_EXITED"
                break
            if lease is not None:
                if not all(p.contained() for p in processes):
                    reason = "OWNED_CONTAINMENT_LOST"
                    break
                try:
                    lease.check(lease.active if retry_access else value.get("operation"), now=now)
                except ValueError as exc:
                    reason = str(exc)
                    break
                operation_id = lease.active["operation_id"] if lease.active else None
                if not retry_access and acknowledged != operation_id:
                    ipc.publish(folder / "watchdog-operation.json", json.dumps(dict(
                        run_id=manifest["run_id"], operation_id=operation_id)), critical=True)
                    acknowledged = operation_id
            if retry_access:
                # Do not clear/admit/acknowledge an operation from unreadable
                # bytes, nor let a retry sleep cross any known safety boundary.
                remaining = min(access_started + .5, deadline, last + 30) - now
                if lease is not None and lease.active is not None:
                    remaining = min(remaining, lease.active['deadline_monotonic'] - now)
                sleep(min(.02, remaining))
            else:
                if access_started is not None:
                    heartbeat_access['recovered_episodes'] += 1
                access_started, access_attempts = None, 0
                sleep(0.2)
    finally:
        # Emergency close can discard unsaved ordinary progression. Never claim
        # a save/restore or a native action's completion from process exit.
        results = cleanup_owned(processes)
        confirmed = (registration_complete and all(r["exited"] for r in results)
                     and len(results) == expected_count)
        configs = restore_configs(manifest.get("config_restores", [])) if confirmed else []
        ipc.publish(folder / "watchdog-result.json", json.dumps({
            "run_id": manifest["run_id"], "reason": reason,
            "processes": results, "cleanup_confirmed": confirmed,
            "config_restored": configs,
            **({"heartbeat_access": heartbeat_access} if heartbeat_access['failures'] else {}),
            **({"controller_lost": controller_lost} if controller_lost is not None else {})}), critical=True)
        for process in processes:
            process.close()
    return reason


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    run_watchdog(json.loads(ipc.read_text(Path(args.manifest), encoding="utf8")))
