"""G4 bounded ARTI Stardew HWND / PID / process-instance binding.

Owned by the G4 capture-adapter slice. This is NOT a second context
authority: G2 inspect_context identity is copied by the capture owner,
not generated here.

Fail closed. Never retarget desktop or HOST. Cloud/Linux is explicitly
PLATFORM_UNAVAILABLE; native EnumWindows is not claimed LOCAL_VERIFIED.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol, Sequence

from arti_gamepad import WindowStardew

# Reason tokens. Callers must treat unknown as unavailable, never as success.
PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"
MISSING_WINDOW = "MISSING_WINDOW"
AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
WRONG_TARGET = "WRONG_TARGET"
WRONG_PROCESS_INSTANCE = "WRONG_PROCESS_INSTANCE"
HWND_REUSED = "HWND_REUSED"
PID_MISMATCH = "PID_MISMATCH"
WINDOW_BINDING_UNAVAILABLE = "WINDOW_BINDING_UNAVAILABLE"

# SMAPI host exe for the ARTI farmhand game surface. Vanilla HOST
# (`Stardew Valley.exe`) is recognized by WindowStardew but is the wrong
# capture target for G4 — never bind HOST when ARTI/SMAPI is absent.
_ARTI_EXE = "stardewmoddingapi.exe"


@dataclass(frozen=True)
class WindowRecord:
    """One enumerated top-level window. Synthetic in tests; native on Windows."""

    hwnd: int
    title: str
    pid: int
    creation_time: int
    exe_name: str
    visible: bool
    minimized: bool
    client_rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class ArtiStardewWindowBinding:
    """Pinned ARTI game-surface HWND plus the process instance that owns it."""

    hwnd: int
    pid: int
    creation_time: int
    exe_name: str
    title: str
    client_rect: tuple[int, int, int, int]
    minimized: bool


@dataclass(frozen=True)
class BindingOutcome:
    ok: bool
    reason: str | None
    binding: ArtiStardewWindowBinding | None

    def __post_init__(self) -> None:
        if self.ok:
            if self.reason is not None or self.binding is None:
                raise ValueError("sukses binding wajib punya binding dan tanpa reason")
        elif self.binding is not None or not self.reason:
            raise ValueError("gagal binding wajib reason dan tanpa binding")


class WindowEnumerator(Protocol):
    def is_platform_available(self) -> bool: ...

    def list_windows(self) -> Sequence[WindowRecord]: ...


@dataclass
class FakeWindowEnumerator:
    """Test double. Never talks to the OS."""

    windows: list[WindowRecord]
    available: bool = True

    def is_platform_available(self) -> bool:
        return self.available

    def list_windows(self) -> Sequence[WindowRecord]:
        return list(self.windows)


class UnavailableWindowEnumerator:
    def is_platform_available(self) -> bool:
        return False

    def list_windows(self) -> Sequence[WindowRecord]:
        return ()


def _judul_exe_lolos(judul: str, exe: str) -> bool:
    """Reuse the paid WindowStardew title-prefix + exe rule (SMAPI console vs game)."""
    return WindowStardew(cari_depan=lambda: (judul, exe)).di_depan()


def _is_arti_exe(exe: str) -> bool:
    return (exe or "").lower() == _ARTI_EXE


def _kandidat_arti(windows: Sequence[WindowRecord]) -> list[WindowRecord]:
    lolos: list[WindowRecord] = []
    for rec in windows:
        if not rec.visible:
            continue
        if rec.hwnd <= 0 or rec.pid <= 0 or rec.creation_time <= 0:
            continue
        if not _judul_exe_lolos(rec.title, rec.exe_name):
            continue
        if not _is_arti_exe(rec.exe_name):
            continue
        lolos.append(rec)
    return lolos


def _binding_dari(rec: WindowRecord) -> ArtiStardewWindowBinding:
    return ArtiStardewWindowBinding(
        hwnd=rec.hwnd,
        pid=rec.pid,
        creation_time=rec.creation_time,
        exe_name=rec.exe_name,
        title=rec.title,
        client_rect=rec.client_rect,
        minimized=rec.minimized,
    )


def _gagal(reason: str) -> BindingOutcome:
    return BindingOutcome(ok=False, reason=reason, binding=None)


def resolve_arti_stardew_window(
    enumerator: WindowEnumerator | None = None,
    *,
    expected_hwnd: int | None = None,
    expected_pid: int | None = None,
    expected_creation_time: int | None = None,
    pinned: ArtiStardewWindowBinding | None = None,
) -> BindingOutcome:
    """Resolve the intended ARTI Stardew game HWND.

    Wrong, reused, ambiguous, missing, HOST-only, or non-Windows => fail closed.
    Never returns a desktop/monitor target.
    """
    enum = enumerator if enumerator is not None else platform_window_enumerator()
    if not enum.is_platform_available():
        return _gagal(PLATFORM_UNAVAILABLE)

    try:
        records = list(enum.list_windows())
    except Exception:
        return _gagal(WINDOW_BINDING_UNAVAILABLE)

    if pinned is not None:
        if expected_hwnd is not None and expected_hwnd != pinned.hwnd:
            return _gagal(WRONG_TARGET)
        if expected_pid is not None and expected_pid != pinned.pid:
            return _gagal(PID_MISMATCH)
        if (
            expected_creation_time is not None
            and expected_creation_time != pinned.creation_time
        ):
            return _gagal(WRONG_PROCESS_INSTANCE)
        same_hwnd = [r for r in records if r.hwnd == pinned.hwnd]
        if not same_hwnd:
            return _gagal(MISSING_WINDOW)
        rec = same_hwnd[0]
        if rec.pid != pinned.pid or rec.creation_time != pinned.creation_time:
            return _gagal(HWND_REUSED)
        if not _kandidat_arti([rec]):
            return _gagal(HWND_REUSED)
        return BindingOutcome(ok=True, reason=None, binding=_binding_dari(rec))

    kandidat = _kandidat_arti(records)

    if expected_hwnd is not None:
        by_hwnd = [r for r in kandidat if r.hwnd == expected_hwnd]
        if not by_hwnd:
            leftover = [r for r in records if r.hwnd == expected_hwnd]
            if leftover:
                if leftover[0].pid != (expected_pid or leftover[0].pid):
                    return _gagal(PID_MISMATCH)
                if leftover[0].creation_time != (
                    expected_creation_time or leftover[0].creation_time
                ):
                    return _gagal(WRONG_PROCESS_INSTANCE)
                return _gagal(HWND_REUSED)
            return _gagal(MISSING_WINDOW)
        kandidat = by_hwnd

    if expected_pid is not None:
        by_pid = [r for r in kandidat if r.pid == expected_pid]
        if not by_pid:
            return _gagal(PID_MISMATCH)
        kandidat = by_pid

    if expected_creation_time is not None:
        by_ct = [r for r in kandidat if r.creation_time == expected_creation_time]
        if not by_ct:
            return _gagal(WRONG_PROCESS_INSTANCE)
        kandidat = by_ct

    if not kandidat:
        # Visible vanilla/HOST Stardew is the wrong target, not a fallback.
        hostish = [
            r
            for r in records
            if r.visible and _judul_exe_lolos(r.title, r.exe_name)
        ]
        if hostish:
            return _gagal(WRONG_TARGET)
        return _gagal(MISSING_WINDOW)

    if len(kandidat) > 1:
        return _gagal(AMBIGUOUS_TARGET)

    rec = kandidat[0]
    return BindingOutcome(ok=True, reason=None, binding=_binding_dari(rec))


def reverify_binding(
    enumerator: WindowEnumerator,
    pinned: ArtiStardewWindowBinding,
) -> BindingOutcome:
    """HWND -> PID + creation time must still match the pinned instance."""
    return resolve_arti_stardew_window(enumerator, pinned=pinned)


def platform_window_enumerator() -> WindowEnumerator:
    if sys.platform != "win32":
        return UnavailableWindowEnumerator()
    return WindowsWindowEnumerator()


class WindowsWindowEnumerator:
    """EnumWindows + GetProcessTimes. Invoked only on win32; unproved here."""

    def is_platform_available(self) -> bool:
        return sys.platform == "win32"

    def list_windows(self) -> Sequence[WindowRecord]:
        if sys.platform != "win32":
            return ()
        return tuple(_enum_windows_native())


def _enum_windows_native() -> list[WindowRecord]:
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    found: list[WindowRecord] = []

    def _creation_time(pid: int) -> int:
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return 0
        try:
            create = FILETIME()
            exit_t = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            if not k32.GetProcessTimes(
                handle,
                ctypes.byref(create),
                ctypes.byref(exit_t),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return 0
            return (int(create.dwHighDateTime) << 32) | int(create.dwLowDateTime)
        finally:
            k32.CloseHandle(handle)

    def _exe_name(pid: int) -> str:
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            besar = wintypes.DWORD(32768)
            nama = ctypes.create_unicode_buffer(besar.value)
            if k32.QueryFullProcessImageNameW(handle, 0, nama, ctypes.byref(besar)):
                return (nama.value or "").replace("/", "\\").rsplit("\\", 1)[-1]
            return ""
        finally:
            k32.CloseHandle(handle)

    def _judul(hwnd: int) -> str:
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""

    def _client_rect(hwnd: int) -> tuple[int, int, int, int]:
        rect = RECT()
        if not u32.GetClientRect(hwnd, ctypes.byref(rect)):
            return (0, 0, 0, 0)
        tl = POINT(rect.left, rect.top)
        br = POINT(rect.right, rect.bottom)
        u32.ClientToScreen(hwnd, ctypes.byref(tl))
        u32.ClientToScreen(hwnd, ctypes.byref(br))
        return (int(tl.x), int(tl.y), int(br.x), int(br.y))

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        if not u32.IsWindow(hwnd):
            return True
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        found.append(
            WindowRecord(
                hwnd=int(hwnd),
                title=_judul(int(hwnd)),
                pid=int(pid.value),
                creation_time=_creation_time(int(pid.value)),
                exe_name=_exe_name(int(pid.value)),
                visible=bool(u32.IsWindowVisible(hwnd)),
                minimized=bool(u32.IsIconic(hwnd)),
                client_rect=_client_rect(int(hwnd)),
            )
        )
        return True

    u32.EnumWindows(WNDENUMPROC(_callback), 0)
    return found
