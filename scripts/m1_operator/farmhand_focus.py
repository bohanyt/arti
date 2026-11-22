"""Move focus off the farmhand using only an operator-owned neutral target, and only when her hands need it.

ADR 250 (fix145A): with a DLL whose session.json says every hand path is focus-independent
(``focus_independent`` and each ``focus_independent_paths`` value true), nothing moves focus away from her window: the
release reports ``in_front_focus_independent`` and the caller goes on. Otherwise (an older DLL, or a path that still
depends on focus) the release moves focus to the operator console or the shell desktop. When this long-lived process is
denied (Windows' foreground lock; 2026-11-19: a fresh process succeeded at once where the supervisor was refused), a
short helper process makes the same call. Every result names its cause; a failure is never a bare False.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

from arti_runtime import ipc
from scripts.m1_operator import win_facts

NOT_IN_FRONT = "not_in_front"
FOCUS_INDEPENDENT = "in_front_focus_independent"
RELEASED = "released"
DENIED = "foreground_lock_denied"

# The helper is self-contained (-I: no repo imports): one SetForegroundWindow, exit 0 when Windows accepted it.
_HELPER = ("import ctypes, sys\n"
           "from ctypes import wintypes as W\n"
           "u = ctypes.WinDLL('user32')\n"
           "u.SetForegroundWindow.argtypes = [W.HWND]\n"
           "u.SetForegroundWindow.restype = W.BOOL\n"
           "sys.exit(0 if u.SetForegroundWindow(int(sys.argv[1])) else 3)\n")


class FocusRelease:
    """Truthy when her play may go on; ``cause`` names what happened (ADR 250)."""

    __slots__ = ("ok", "cause")

    def __init__(self, ok, cause):
        self.ok = bool(ok)
        self.cause = cause

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return "FocusRelease(ok=%r, cause=%r)" % (self.ok, self.cause)


PATHS = ("world", "menu")


def focus_independent(session):
    """Whether her hands work with her window active, read at the weaker value (ADR 250, review S2).

    The ADR 250 record shape (``focus_independent_paths`` present) counts only with ``focus_independent`` true AND both
    ``world`` and ``menu`` explicitly true. A partial or malformed record is not independent. The legacy single value
    (an ADR 244 DLL, no paths) keeps its own meaning for the guards. A missing or unreadable session is never independent.
    """
    if not isinstance(session, dict) or session.get("focus_independent") is not True:
        return False
    if "focus_independent_paths" not in session:
        return True  # legacy: one value, no paths
    paths = session.get("focus_independent_paths")
    return isinstance(paths, dict) and all(paths.get(name) is True for name in PATHS)


def hands_need_background(session):
    """True unless the DLL states, per path, that every hand works with her window active (ADR 250).

    An ADR 244 DLL publishes one value and no paths: its menu path still depended on focus (2026-11-19), so it keeps
    the release. So does a partial record.
    """
    return not (isinstance(session, dict) and isinstance(session.get("focus_independent_paths"), dict)
                and focus_independent(session))


def _session(launch=None):
    """session.json, or None when unreadable or written before ``launch`` (another launch's record, review S2)."""
    from scripts.m1_operator import m1_paths

    path = Path(m1_paths.COMMAND_DIR) / "session.json"
    try:
        if launch is not None and path.stat().st_mtime < Path(launch).stat().st_mtime:
            return None
        return json.loads(ipc.read_text(path, encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def farmhand_in_front(launch_file, foreground_app=None):
    """Compare the foreground process with the recorded farmhand PID."""
    try:
        launch = json.loads(ipc.read_text(launch_file, encoding="utf-8-sig"))
        pid = launch.get("pid")
        fg = (foreground_app or win_facts.foreground_app)()
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(fg, dict) or type(fg.get("hwnd")) is not int:
        return None
    if fg["hwnd"] == 0:
        return False
    if type(pid) is not int or type(fg.get("pid")) is not int:
        return None
    return fg["pid"] == pid


def _neutral_window():
    # GetConsoleWindow is scoped to this process. Detached/pseudoconsole
    # children often have no visible console, so use the shell desktop.
    from ctypes import wintypes

    user32, kernel32 = win_facts._w()
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    user32.GetShellWindow.restype = wintypes.HWND
    console = kernel32.GetConsoleWindow()
    if console and user32.IsWindowVisible(console):
        return console
    return user32.GetShellWindow()


def _set_foreground(hwnd):
    from ctypes import wintypes

    user32, _ = win_facts._w()
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow(hwnd)


def _helper_set_foreground(hwnd):
    """The same call from a short separate process, without a window. Its exit code, or None when it could not run."""
    try:
        out = subprocess.run([sys.executable, "-I", "-B", "-c", _HELPER, str(int(hwnd))], capture_output=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    return out.returncode


def release_farmhand_focus(run, session=None):
    """Leave existing focus alone unless the farmhand is in front and her hands still need the background.

    Windows can deny a focus change. The readback, not the API return code, decides whether it is safe to continue.
    """
    launch = Path(run) / "runtime" / "farmhand-launch.json"
    if farmhand_in_front(launch) is not True:
        return FocusRelease(True, NOT_IN_FRONT)
    if not hands_need_background(_session(launch) if session is None else session):
        return FocusRelease(True, FOCUS_INDEPENDENT)
    target = _neutral_window()
    if not target:
        return FocusRelease(False, DENIED + ":no_neutral_window")
    tried = []
    if farmhand_in_front(launch) is True:
        _set_foreground(target)
        tried.append("in_process")
        time.sleep(0.25)  # activation on another Win32 input queue can settle asynchronously
    if farmhand_in_front(launch) is False:
        return FocusRelease(True, RELEASED + ":" + (tried[-1] if tried else "settled"))
    rc = _helper_set_foreground(target)
    tried.append("helper_rc=%s" % rc)
    time.sleep(0.25)
    after = farmhand_in_front(launch)
    if after is False:
        return FocusRelease(True, RELEASED + ":helper")
    return FocusRelease(False, "%s:%s%s" % (DENIED, "+".join(tried), "" if after is True else ":readback_unknown"))
