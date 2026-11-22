"""fix54J co-tenant priority: the owner's clip-ops run at low priority while ARTI plays.

Owner decision 2026-11-03 ("[recorded remark omitted]"): from ARM to stop, the
harness lowers the CPU, I/O and memory priority of exactly the heavy-work-gate exemption
(python running run_queue.py / momen_bank.py / -m yt_dlp, plus their ffmpeg/deno
descendants) and restores the recorded originals afterwards. It never stops, kills,
suspends or zero-throttles them: CPU is BelowNormal (not Idle), I/O and memory use the
lowest hint Windows grants a same-user process without admin. Every error is logged and
the session continues. Stdlib only, so `python cotenant.py restore <run>` works after a
runner that died.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOG_NAME = "cotenant-priority.jsonl"
STATE_NAME = "cotenant-priority-state.json"
INTERVAL_S = 20          # new ffmpeg children spawn per clip
MAX_PER_TICK = 64
MAX_RECORDS = 2000
MAX_ATTEMPTS = 3
BELOW_NORMAL = 0x4000
CPU_NAMES = {0x40: "Idle", 0x4000: "BelowNormal", 0x20: "Normal", 0x8000: "AboveNormal", 0x80: "High", 0x100: "Realtime"}
IO_VERY_LOW, MEM_VERY_LOW = 0, 1
TARGET = {"cpu": BELOW_NORMAL, "io": IO_VERY_LOW, "mem": MEM_VERY_LOW}
FIELDS = ("cpu", "io", "mem")

# Exact port of the operator exemption matcher (a rig-side PowerShell script).
PYTHON = re.compile(r"^python.*\.exe$", re.I)
CHILD = re.compile(r"^(ffmpeg|deno)\.exe$", re.I)
ROOT_RULES = (
    ("run_queue.py", re.compile(r"(?:^|[\\/\s\"'])run_queue\.py(?:[\s\"']|$)", re.I)),
    ("momen_bank.py", re.compile(r"(?:^|[\\/\s\"'])momen_bank\.py(?:[\s\"']|$)", re.I)),
    ("yt_dlp", re.compile(r"\s-m\s+yt_dlp(?:\s|$)", re.I)),
)


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    name: str
    created: int          # FILETIME ticks; (pid, created) is the identity
    cmdline: str | None = None


class Gone(Exception):
    """The (pid, created) identity no longer exists."""


class PriorityError(Exception):
    def __init__(self, detail, *, privileged=False):
        super().__init__(detail)
        self.privileged = privileged


def utc():
    return datetime.now(timezone.utc).isoformat()


def root_rule(proc):
    if not PYTHON.match(proc.name or ""):
        return None
    for rule, pattern in ROOT_RULES:
        if pattern.search(proc.cmdline or ""):
            return rule
    return None


def select(procs, self_pid=None):
    """[(proc, rule, ancestor identities nearest-first)] for exactly the exemption list."""
    by_pid = {p.pid: p for p in procs}
    roots = {p.pid: rule for p in procs if (rule := root_rule(p))}
    out = []
    for proc in procs:
        if proc.pid in (0, self_pid):
            continue
        if proc.pid in roots:
            out.append((proc, roots[proc.pid], ()))
            continue
        if not CHILD.match(proc.name or ""):
            continue
        chain, child, seen = [], proc, {proc.pid}
        while child.ppid and child.ppid not in seen:
            parent = by_pid.get(child.ppid)
            # A reused parent PID is younger than its "child": never follow it.
            if parent is None or parent.created > child.created:
                break
            seen.add(parent.pid)
            chain.append((parent.pid, parent.created))
            if parent.pid in roots:
                out.append((proc, proc.name.lower(), tuple(chain)))
                break
            child = parent
    return out


class CoTenantPriority:
    def __init__(self, run, backend=None, *, clock=time.monotonic, interval_s=INTERVAL_S, self_pid=None):
        self.run = Path(run)
        self.backend = backend if backend is not None else WindowsBackend()
        self.clock, self.interval_s = clock, interval_s
        self.self_pid = os.getpid() if self_pid is None else self_pid
        self.fields = list(FIELDS)
        self.records = {}
        self.active = True
        self._last = None
        self._errors = set()

    # ---- bookkeeping -------------------------------------------------
    def log(self, event, **row):
        try:
            with (self.run / LOG_NAME).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(utc=utc(), event=event, **row), sort_keys=True) + "\n")
        except Exception:  # noqa: BLE001 - telemetry must never stop the session
            pass

    def log_once(self, key, event, **row):
        if key not in self._errors:
            self._errors.add(key)
            self.log(event, **row)

    def save(self):
        try:
            tmp = self.run / (STATE_NAME + ".tmp")
            tmp.write_text(json.dumps(dict(records=list(self.records.values()), fields=self.fields, active=self.active), indent=1), encoding="utf-8")
            os.replace(tmp, self.run / STATE_NAME)
        except Exception as exc:  # noqa: BLE001
            self.log_once(("save", type(exc).__name__), "error", op="save_state", detail=str(exc)[:300])

    @classmethod
    def from_state(cls, run, backend=None):
        state = json.loads((Path(run) / STATE_NAME).read_text(encoding="utf-8"))
        tenant = cls(run, backend)
        tenant.fields = list(state.get("fields") or FIELDS)
        tenant.records = {(r["pid"], r["created"]): r for r in state.get("records") or ()}
        return tenant

    # ---- session -----------------------------------------------------
    def start(self):
        self.log("start", target={f: TARGET[f] for f in FIELDS}, cpu_target=CPU_NAMES[BELOW_NORMAL], interval_s=self.interval_s,
                 rules=[r for r, _ in ROOT_RULES] + ["ffmpeg.exe/deno.exe descendants"])
        self.tick(force=True)

    def tick(self, force=False):
        if not self.active:
            return
        now = self.clock()
        if not force and self._last is not None and now - self._last < self.interval_s:
            return
        self._last = now
        try:
            procs = self.backend.processes()
            matched = select(procs, self.self_pid)
        except Exception as exc:  # noqa: BLE001 - fail open: game session continues
            self.log_once(("list", type(exc).__name__), "error", op="list", detail=str(exc)[:300])
            return
        changed = False
        for proc, rule, chain in matched[:MAX_PER_TICK]:
            try:
                changed |= self._lower(proc, rule, chain)
            except Exception as exc:  # noqa: BLE001
                self.log_once(("lower", proc.pid, proc.created), "error", op="lower", pid=proc.pid, name=proc.name, detail=str(exc)[:300])
        if changed:
            self.save()

    def _lower(self, proc, rule, chain):
        key = (proc.pid, proc.created)
        rec = self.records.get(key)
        if rec is None:
            if len(self.records) >= MAX_RECORDS:
                self.log_once("cap", "cap", records=len(self.records))
                return False
            rec = dict(pid=proc.pid, created=proc.created, name=proc.name, rule=rule, root_chain=[list(c) for c in chain],
                       before={}, original={}, after={}, attempts={}, inherited_from={}, restored=False)
            self.records[key] = rec
        changed = False
        for field in list(self.fields):
            if field in rec["after"] or rec["attempts"].get(field, 0) >= MAX_ATTEMPTS:
                continue
            rec["attempts"][field] = rec["attempts"].get(field, 0) + 1
            try:
                if field not in rec["before"]:
                    rec["before"][field] = self.backend.read(proc.pid, proc.created, field)
                    rec["original"][field] = rec["before"][field]
                    parent = self._lowered_ancestor(chain, field)
                    if rec["before"][field] == TARGET[field] and parent is not None:
                        # Children of a lowered root inherit the low value; their normal is the root's.
                        rec["original"][field] = parent["original"][field]
                        rec["inherited_from"][field] = parent["pid"]
                    changed = True
                if rec["before"][field] != TARGET[field]:
                    self.backend.write(proc.pid, proc.created, field, TARGET[field])
                rec["after"][field] = TARGET[field]
                changed = True
                self.log("lowered", pid=proc.pid, name=proc.name, rule=rule, field=field, before=rec["before"][field],
                         after=TARGET[field], original=rec["original"][field], inherited_from=rec["inherited_from"].get(field))
            except Gone:
                self.log("gone", pid=proc.pid, name=proc.name, field=field, phase="lower")
                rec["attempts"][field] = MAX_ATTEMPTS
                return True
            except PriorityError as exc:
                if exc.privileged and field != "cpu":
                    self.fields.remove(field)
                    self.log("fallback", field=field, mode="without_" + field, detail=str(exc)[:300],
                             note="needs admin; continuing with the remaining priorities")
                else:
                    self.log_once(("set", key, field), "error", op="lower", pid=proc.pid, name=proc.name, field=field, detail=str(exc)[:300])
                changed = True
        return changed

    def _lowered_ancestor(self, chain, field):
        for pid, created in chain:
            rec = self.records.get((pid, created))
            if rec is not None and rec["after"].get(field) == TARGET[field]:
                return rec
        return None

    def restore(self, reason="session_end"):
        """Put back every recorded original for processes still alive. Idempotent."""
        self.active = False
        counts = dict(restored=0, unchanged=0, gone=0, left_external=0, error=0)
        for rec in self.records.values():
            if rec.get("restored"):
                continue
            for field, set_value in rec["after"].items():
                original = rec["original"][field]
                try:
                    current = self.backend.read(rec["pid"], rec["created"], field)
                    if current != set_value:
                        counts["left_external"] += 1
                        self.log("left_external", pid=rec["pid"], name=rec["name"], field=field, current=current, set=set_value)
                        continue
                    if original == set_value:
                        counts["unchanged"] += 1
                        continue
                    self.backend.write(rec["pid"], rec["created"], field, original)
                    counts["restored"] += 1
                    self.log("restored", pid=rec["pid"], name=rec["name"], field=field, before=set_value, after=original)
                except Gone:
                    counts["gone"] += 1
                    break
                except Exception as exc:  # noqa: BLE001 - restore what we can; never raise into cleanup
                    counts["error"] += 1
                    self.log("error", op="restore", pid=rec["pid"], name=rec["name"], field=field, detail=str(exc)[:300])
            rec["restored"] = True
        self.save()
        self.log("stop", reason=reason, records=len(self.records), fields=self.fields, **counts)
        return counts


# ---- Windows backend (ctypes, no admin) -------------------------------
class WindowsBackend:
    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_IO_PRIORITY, PROCESS_COMMAND_LINE = 33, 60
    PROCESS_MEMORY_PRIORITY = 0          # PROCESS_INFORMATION_CLASS for Get/SetProcessInformation
    PRIVILEGE_STATUS = {0xC0000061, 0xC0000022}  # STATUS_PRIVILEGE_NOT_HELD, STATUS_ACCESS_DENIED

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wt = ctypes, wintypes
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        n = self.n = ctypes.WinDLL("ntdll")
        H, D, B = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL

        class ENTRY(ctypes.Structure):
            _fields_ = [("dwSize", D), ("cntUsage", D), ("th32ProcessID", D), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", D), ("cntThreads", D), ("th32ParentProcessID", D), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", D), ("szExeFile", wintypes.WCHAR * 260)]

        class USTR(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]
        self.ENTRY, self.USTR = ENTRY, USTR
        FT = ctypes.POINTER(wintypes.FILETIME)
        for name, args, res in (("CreateToolhelp32Snapshot", (D, D), H), ("Process32FirstW", (H, ctypes.POINTER(ENTRY)), B),
                                ("Process32NextW", (H, ctypes.POINTER(ENTRY)), B), ("OpenProcess", (D, B, D), H),
                                ("CloseHandle", (H,), B), ("GetProcessTimes", (H, FT, FT, FT, FT), B),
                                ("GetPriorityClass", (H,), D), ("SetPriorityClass", (H, D), B),
                                ("GetProcessInformation", (H, ctypes.c_int, ctypes.c_void_p, D), B),
                                ("SetProcessInformation", (H, ctypes.c_int, ctypes.c_void_p, D), B)):
            fn = getattr(k, name); fn.argtypes, fn.restype = args, res
        n.NtQueryInformationProcess.argtypes = (H, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))
        n.NtQueryInformationProcess.restype = wintypes.LONG
        n.NtSetInformationProcess.argtypes = (H, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG)
        n.NtSetInformationProcess.restype = wintypes.LONG

    def _open(self, pid, access):
        handle = self.k.OpenProcess(access, False, pid)
        if not handle:
            raise OSError(self.ctypes.get_last_error(), "OpenProcess failed")
        return handle

    def _created(self, handle):
        times = [self.wt.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *(self.ctypes.byref(t) for t in times)):
            raise OSError(self.ctypes.get_last_error(), "GetProcessTimes failed")
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def _cmdline(self, handle):
        size = self.wt.ULONG(0)
        status = self.n.NtQueryInformationProcess(handle, self.PROCESS_COMMAND_LINE, None, 0, self.ctypes.byref(size))
        if not size.value:
            return None
        buf = self.ctypes.create_string_buffer(size.value)
        status = self.n.NtQueryInformationProcess(handle, self.PROCESS_COMMAND_LINE, buf, size, self.ctypes.byref(size))
        if status:
            return None
        text = self.USTR.from_buffer(buf)
        return self.ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""

    def processes(self):
        snap = self.k.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (None, self.ctypes.c_void_p(-1).value):
            raise OSError(self.ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
        rows = []
        try:
            entry = self.ENTRY(); entry.dwSize = self.ctypes.sizeof(self.ENTRY)
            ok = self.k.Process32FirstW(snap, self.ctypes.byref(entry))
            while ok:
                pid, name = entry.th32ProcessID, entry.szExeFile
                created, cmdline = 0, None
                try:
                    handle = self._open(pid, self.PROCESS_QUERY_LIMITED_INFORMATION)
                    try:
                        created = self._created(handle)
                        if PYTHON.match(name):
                            cmdline = self._cmdline(handle)
                    finally:
                        self.k.CloseHandle(handle)
                except OSError:
                    pass  # protected/other-user process: identity unknown, never matched as an ancestor root
                rows.append(Proc(pid, entry.th32ParentProcessID, name, created, cmdline))
                ok = self.k.Process32NextW(snap, self.ctypes.byref(entry))
        finally:
            self.k.CloseHandle(snap)
        return rows

    def _handle(self, pid, created, access):
        try:
            handle = self._open(pid, access)
        except OSError as exc:
            if exc.errno == 87:  # ERROR_INVALID_PARAMETER: no such pid
                raise Gone(pid) from exc
            raise PriorityError(f"OpenProcess({pid}) error {exc.errno}") from exc
        if self._created(handle) != created:
            self.k.CloseHandle(handle)
            raise Gone(pid)
        return handle

    def read(self, pid, created, field):
        handle = self._handle(pid, created, self.PROCESS_QUERY_INFORMATION)
        try:
            if field == "cpu":
                value = self.k.GetPriorityClass(handle)
                if not value:
                    raise PriorityError(f"GetPriorityClass error {self.ctypes.get_last_error()}")
                return int(value)
            value = self.wt.ULONG(0)
            if field == "io":
                status = self.n.NtQueryInformationProcess(handle, self.PROCESS_IO_PRIORITY, self.ctypes.byref(value), 4, None)
                if status:
                    raise PriorityError(f"query io status 0x{status & 0xFFFFFFFF:08X}")
            elif not self.k.GetProcessInformation(handle, self.PROCESS_MEMORY_PRIORITY, self.ctypes.byref(value), 4):
                raise PriorityError(f"GetProcessInformation(memory) error {self.ctypes.get_last_error()}")
            return int(value.value)
        finally:
            self.k.CloseHandle(handle)

    def write(self, pid, created, field, value):
        handle = self._handle(pid, created, self.PROCESS_QUERY_INFORMATION | self.PROCESS_SET_INFORMATION)
        try:
            if field == "cpu":
                if not self.k.SetPriorityClass(handle, value):
                    raise PriorityError(f"SetPriorityClass error {self.ctypes.get_last_error()}")
                return
            box = self.wt.ULONG(value)
            if field == "io":
                status = self.n.NtSetInformationProcess(handle, self.PROCESS_IO_PRIORITY, self.ctypes.byref(box), 4) & 0xFFFFFFFF
                if status:
                    raise PriorityError(f"set io status 0x{status:08X}", privileged=status in self.PRIVILEGE_STATUS)
            elif not self.k.SetProcessInformation(handle, self.PROCESS_MEMORY_PRIORITY, self.ctypes.byref(box), 4):
                error = self.ctypes.get_last_error()
                raise PriorityError(f"SetProcessInformation(memory) error {error}", privileged=error in (5, 1314))
        finally:
            self.k.CloseHandle(handle)


def main(argv=None):
    """`cotenant.py restore <run>`: idempotent restore from the persisted state after any stop."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "restore":
        print("usage: cotenant.py restore <run-dir>", file=sys.stderr)
        return 2
    if not (Path(argv[1]) / STATE_NAME).is_file():
        print("no co-tenant state; nothing to restore")
        return 0
    counts = CoTenantPriority.from_state(argv[1]).restore("post_runner")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
