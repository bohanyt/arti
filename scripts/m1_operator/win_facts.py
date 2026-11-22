"""win_facts.py: read-only Win32 facts for the WS5 operator tools (stdlib ctypes; no pywin32, no psutil).

Windows (IsWindow, title, pid, IsIconic, IsHungAppWindow, foreground, OS cursor position), processes (alive, exe name, wait for exit,
working set), keys (GetAsyncKeyState) and python command lines (PowerShell CIM, to see whether a trial runner is
running). Nothing here sends input, closes a window, binds a port or writes a file.

ctypes DLLs load lazily on first use, so importing this module is free on any OS (the offline tests use fakes).
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc

import ctypes
import os
import re
import subprocess
import sys
import time

_DLL = {}

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
WAIT_OBJECT_0, WAIT_TIMEOUT = 0x0, 0x102
STILL_ACTIVE = 259

VK = {"w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44, "c": 0x43, "x": 0x58, "e": 0x45, "lbutton": 0x01, "rbutton": 0x02,
      **{str(n): 0x30 + n for n in range(10)}}


def _w():
    """(user32, kernel32) with argtypes set. Windows only."""
    if "u32" not in _DLL:
        from ctypes import wintypes as W
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for n in ("IsWindow", "IsWindowVisible", "IsIconic", "IsHungAppWindow"):
            getattr(u32, n).argtypes = [W.HWND]; getattr(u32, n).restype = W.BOOL
        u32.GetForegroundWindow.restype = W.HWND
        u32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        u32.GetWindowThreadProcessId.restype = W.DWORD
        u32.GetWindowTextLengthW.argtypes = [W.HWND]
        u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM), W.LPARAM]
        u32.GetAsyncKeyState.argtypes = [ctypes.c_int]; u32.GetAsyncKeyState.restype = ctypes.c_short
        k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k32.OpenProcess.restype = W.HANDLE
        k32.CloseHandle.argtypes = [W.HANDLE]; k32.CloseHandle.restype = W.BOOL
        k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; k32.WaitForSingleObject.restype = W.DWORD
        k32.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]; k32.GetExitCodeProcess.restype = W.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
        k32.QueryFullProcessImageNameW.restype = W.BOOL
        _DLL["u32"], _DLL["k32"] = u32, k32
    return _DLL["u32"], _DLL["k32"]


# ---------------------------------------------------------------- windows
def title(hwnd: int) -> str:
    u32, _ = _w()
    n = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_pid(hwnd: int) -> int | None:
    from ctypes import wintypes as W
    u32, _ = _w()
    pid = W.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def window(hwnd) -> dict:
    """Read-only facts for one hwnd: exists, pid, title, visible, iconic, hung."""
    try:
        h = int(hwnd)
    except (TypeError, ValueError):
        return dict(hwnd=hwnd, exists=False, pid=None, title=None, visible=False, iconic=False, hung=False)
    u32, _ = _w()
    if not u32.IsWindow(h):
        return dict(hwnd=h, exists=False, pid=None, title=None, visible=False, iconic=False, hung=False)
    return dict(hwnd=h, exists=True, pid=window_pid(h), title=title(h), visible=bool(u32.IsWindowVisible(h)),
                rect=window_rect(h),
                iconic=bool(u32.IsIconic(h)), hung=bool(u32.IsHungAppWindow(h)))


def window_rect(hwnd):
    """[left, top, right, bottom] (GetWindowRect, physical pixels when the caller is DPI aware) or None."""
    from ctypes import wintypes as W
    u32, _ = _w()
    r = W.RECT()
    u32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
    u32.GetWindowRect.restype = W.BOOL
    return [r.left, r.top, r.right, r.bottom] if u32.GetWindowRect(int(hwnd), ctypes.byref(r)) else None


def foreground() -> int:
    u32, _ = _w()
    return int(u32.GetForegroundWindow() or 0)


def cursor_pos():
    """(x, y) of the OS cursor (GetCursorPos, read-only; pad_watch's leak check), None when it cannot be read."""
    from ctypes import wintypes as W
    u32, _ = _w()
    pt = W.POINT()
    u32.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
    u32.GetCursorPos.restype = W.BOOL
    return (pt.x, pt.y) if u32.GetCursorPos(ctypes.byref(pt)) else None


def foreground_app() -> dict:
    """{hwnd, pid, exe} of the foreground window (no title: the owner's own apps stay private)."""
    h = foreground()
    pid = window_pid(h) if h else None
    return dict(hwnd=h, pid=pid, exe=exe_name(pid) if pid else None)


def enum_windows(contains: str | None = None) -> list:
    """Visible top-level windows [{hwnd, pid, title}], optionally filtered by a title substring (case-insensitive)."""
    from ctypes import wintypes as W
    u32, _ = _w()
    rows = []

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(h, _):
        if u32.IsWindowVisible(h):
            t = title(h)
            if contains is None or contains.lower() in t.lower():
                rows.append(dict(hwnd=int(h), pid=window_pid(h), title=t))
        return True
    u32.EnumWindows(cb, 0)
    return rows


# ---------------------------------------------------------------- processes
def _open(pid: int, access: int):
    _, k32 = _w()
    try:
        return k32.OpenProcess(access, False, int(pid)) or None
    except (TypeError, ValueError, OverflowError):
        return None


def pid_alive(pid) -> bool:
    """True while the process exists and has not exited (OpenProcess(SYNCHRONIZE) + WaitForSingleObject(0))."""
    if not pid:
        return False
    h = _open(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    _, k32 = _w()
    try:
        return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def wait_pid_exit(pid, timeout_s: float):
    """Block up to timeout_s for the process to exit. True = exited (or no such pid), False = still running,
    None = cannot tell (OpenProcess failed for another reason, e.g. access denied): callers refuse on None."""
    h = _open(pid, SYNCHRONIZE)
    if not h:
        return True if ctypes.get_last_error() == 87 else None    # ERROR_INVALID_PARAMETER = no such process
    _, k32 = _w()
    try:
        return k32.WaitForSingleObject(h, int(max(0.0, timeout_s) * 1000)) == WAIT_OBJECT_0
    finally:
        k32.CloseHandle(h)


def process_image(pid) -> str | None:
    """Full exe path of a process (QueryFullProcessImageNameW), None if not accessible."""
    from ctypes import wintypes as W
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return None
    _, k32 = _w()
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = W.DWORD(len(buf))
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k32.CloseHandle(h)


def exe_name(pid) -> str | None:
    p = process_image(pid)
    return os.path.basename(p.replace("/", "\\").split("\\")[-1]) if p else None


def working_set_bytes(pid) -> int | None:
    """WorkingSetSize via K32GetProcessMemoryInfo; falls back to tasklist (locale-agnostic digits) on failure."""
    from ctypes import wintypes as W

    class PMC(ctypes.Structure):
        _fields_ = [("cb", W.DWORD), ("PageFaultCount", W.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if h:
        _, k32 = _w()
        try:
            pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
            fn = k32.K32GetProcessMemoryInfo
            fn.argtypes = [W.HANDLE, ctypes.POINTER(PMC), W.DWORD]; fn.restype = W.BOOL
            if fn(h, ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize)
        except (AttributeError, OSError):
            pass
        finally:
            k32.CloseHandle(h)
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=20).stdout.strip()
        if out.startswith('"'):
            mem = out.split('","')[-1].strip('"')
            digits = "".join(c for c in mem if c.isdigit())
            return int(digits) * 1024 if digits else None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def image_pids(image: str):
    """PIDs of every running process with this exe name (tasklist; read-only). None = tasklist failed (unknown):
    callers refuse on None."""
    try:
        p = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out = p.stdout
    pids = []
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) > 1 and cells[0].strip('"').lower() == image.lower() and cells[1].isdigit():
            pids.append(int(cells[1]))
    return pids


def python_cmdlines() -> list | None:
    """[(pid, command line)] of python processes via PowerShell CIM (no admin for own processes). None on failure."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "ForEach-Object { '' + $_.ProcessId + [char]9 + $_.CommandLine }")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    rows = []
    for line in p.stdout.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit():
            rows.append((int(pid), cmd.strip()))
    return rows


_TRIAL_RE = re.compile(r"""(^|[\\/ "'])(prep_trial\.py["']?\s+run|stardew_m1_trial\.py["']?)(\s|$)""", re.I)
_TEST_RE = re.compile(r"pytest|--ignore|--deselect", re.I)


def is_trial_cmdline(cmd: str) -> bool:
    """True only when the command line runs the trial runner as the script (`prep_trial.py run` or
    `stardew_m1_trial.py ...`), never for a test run that merely names it (pytest, --ignore=..., --deselect ...)."""
    c = cmd or ""
    return bool(_TRIAL_RE.search(c)) and not _TEST_RE.search(c)


def trial_running(cmdlines=None) -> bool | None:
    """True if a `prep_trial.py run` or `stardew_m1_trial.py` process exists (None = unknown)."""
    rows = python_cmdlines() if cmdlines is None else cmdlines
    if rows is None:
        return None
    me = os.getpid()
    return any(is_trial_cmdline(cmd) for pid, cmd in rows if pid != me)


# ---------------------------------------------------------------- files
class FileTail:
    """Read only the bytes appended to a log after start(): open, seek, read, close each time (never `tail -f`, which
    locks files on Windows). A shrunk file (new SMAPI session) is read again from the start."""

    def __init__(self, path):
        from pathlib import Path
        self.path, self.offset = Path(path), 0

    def start(self):
        self.offset = self.path.stat().st_size if self.path.exists() else 0
        return self.offset

    def read_new(self):
        if not self.path.exists():
            return ""
        if self.path.stat().st_size < self.offset:
            self.offset = 0
        with ipc.open_shared(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        self.offset += len(data)
        return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- keys (record only)
def keys_down(names=tuple(VK)) -> list:
    u32, _ = _w()
    return [n for n in names if u32.GetAsyncKeyState(VK[n]) & 0x8000]


def utc(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def stamp(ts: float | None = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if ts is None else ts))


if __name__ == "__main__" and sys.platform == "win32":  # tiny read-only smoke: python win_facts.py
    import json
    print(json.dumps(dict(foreground=foreground(), stardew=enum_windows("Stardew")), indent=1))
