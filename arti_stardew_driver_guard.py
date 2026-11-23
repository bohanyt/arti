"""Refuse a second Stardew driver while an operator-hosted Live7 cycle runs.

The Live7 supervisor (scripts/m1_operator/live_cycle.py) hosts its own Stardew
runtime. A bridge with ``stardew_runtime_enabled`` on the same command dir would
be a second driver. ``active_live7_cycles`` is the one shared resolver (driver
guard + Live7 help file channel). It is cheap (a few globs, small reads) and
never raises: any unreadable file counts as "not active".

stardew_live() reuses the same check as the bridge's single answer to "is ARTI
playing Stardew now" for session mode and proactive gating. It only READS a
cached bool: a daemon thread (started lazily on first call) refreshes it every
`stardew_live_cache_sec` (default 5 s), so the asyncio main loop never scans the
filesystem (REVIEW_BRAIN F1).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import NamedTuple

# Statuses (live_cycle.py) that mean the cycle is driving or about to drive the
# game. "ready" is NOT here: finished rehearsals rest at "ready" indefinitely.
_ACTIVE = frozenset({"starting", "running", "active", "host_wait", "rejoining",
                     "waiting_night_save", "checkpoint", "night_saved"})
# A stale file with an active status (crashed supervisor) stops counting. The
# same window applies to a leftover supervisor.lock (by lock mtime, F2).
_MAX_STATE_AGE_SEC = 12 * 3600


class ActiveCycle(NamedTuple):
    cycle_dir: Path  # <run>/live7/<cycle>
    run_dir: Path    # the run folder = Live7 help dir of that cycle
    mtime: float = 0.0  # cycle_state.json mtime (else supervisor.lock mtime)
    status: str = ""


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _candidate_dirs(config: dict) -> list[Path]:
    """live7_help_dir, stardew_live7_help_dir and the parent of stardew_command_dir."""
    dirs: list[Path] = []
    seen: set[str] = set()
    raws = [str(config.get(key) or "").strip() for key in ("live7_help_dir", "stardew_live7_help_dir")]
    cmd = str(config.get("stardew_command_dir") or "").strip()
    if cmd:
        raws.append(str(Path(cmd).parent))
    for raw in raws:
        if raw and _key(Path(raw)) not in seen:
            seen.add(_key(Path(raw)))
            dirs.append(Path(raw))
    return dirs


def _read_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unreadable = no state
        return None
    return data if isinstance(data, dict) else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return None


def _state_active(path: Path, now: float) -> bool:
    mtime = _mtime(path)
    if mtime is None or now - mtime >= _MAX_STATE_AGE_SEC:
        return False
    data = _read_state(path)
    return data is not None and str(data.get("status") or "").strip().lower() in _ACTIVE


def _cycle_dirs(base: Path) -> list[Path]:
    """Cycle folders with ``base`` as a run folder AND as a runs root."""
    found: list[Path] = []
    try:
        if not base.is_dir():
            return found
        for pattern in ("live7/*", "*/live7/*"):
            try:
                found.extend(p for p in base.glob(pattern) if p.is_dir())
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return found


def _pid_is_live_python(pid: int, lock_mtime: float | None = None) -> bool | None:
    """True/False on Windows when the answer is known, None when it is not.

    False only when the PID provably is not the lock's supervisor: no such
    process (ERROR_INVALID_PARAMETER), exited, not python, or started AFTER the
    lock was written (a reused PID). Access denied and other failures -> None,
    so the caller falls back to the 12 h window (REVIEW_BRAIN_FINAL R1/R2).
    """
    if os.name != "nt" or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if code.value != 259:  # 259 = STILL_ACTIVE
                return False
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            if "python" not in os.path.basename(buf.value).lower():
                return False
            if lock_mtime is not None:
                times = [wintypes.FILETIME() for _ in range(4)]
                if k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                    ft = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                    created = ft / 1e7 - 11644473600.0  # FILETIME -> unix seconds
                    if created > lock_mtime + 5.0:
                        return False  # a newer python reused the dead supervisor's PID
            return True
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _lock_held(lock: Path, now: float) -> bool:
    mtime = _mtime(lock)
    if mtime is None:
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").strip() or "0")
    except Exception:  # noqa: BLE001
        pid = 0
    alive = _pid_is_live_python(pid, mtime)
    if alive is not None:
        return alive
    return now - mtime < _MAX_STATE_AGE_SEC


def _active_cycle(cycle_dir: Path, now: float) -> ActiveCycle | None:
    try:
        lock = cycle_dir / "supervisor.lock"
        state_path = cycle_dir / "cycle_state.json"
        # live_cycle writes the lock ONCE (its PID) and holds it for the whole
        # multi-day cycle, so the lock's age says nothing. A hard-killed
        # supervisor leaves the lock behind: it counts only while that PID is a
        # live python process; an unreadable PID falls back to the 12 h window
        # (REVIEW_BRAIN F2 / REVIEW_BRAIN_FIXES N1).
        locked = _lock_held(lock, now)
        if not locked and not _state_active(state_path, now):
            return None
        run_dir = cycle_dir.parent.parent
        state = _read_state(state_path)
        stop = str((state or {}).get("live7_stop_file") or "").strip()
        if stop and Path(stop).is_absolute():  # live_cycle writes absolute paths
            parent = Path(stop).parent
            if parent.is_dir():
                run_dir = parent
        mtime = _mtime(state_path)
        if mtime is None:
            mtime = _mtime(lock) or 0.0
        return ActiveCycle(cycle_dir, run_dir, mtime, str((state or {}).get("status") or "").strip().lower())
    except Exception:  # noqa: BLE001
        return None


def active_live7_cycles(config: dict, now: float | None = None) -> list[ActiveCycle]:
    """Active Live7 cycles under any candidate dir, newest state first; [] on any error.

    Active = a ``supervisor.lock`` whose PID is a live python (else younger
    than 12 h when the PID is unknown), or a
    ``cycle_state.json`` younger than 12 h whose status is in ``_ACTIVE``.
    """
    try:
        now = time.time() if now is None else now
        cycles: list[ActiveCycle] = []
        seen: set[str] = set()
        for base in _candidate_dirs(config or {}):
            for cycle_dir in _cycle_dirs(base):
                key = _key(cycle_dir)
                if key in seen:
                    continue
                seen.add(key)
                cycle = _active_cycle(cycle_dir, now)
                if cycle is not None:
                    cycles.append(cycle)
        cycles.sort(key=lambda c: c.mtime, reverse=True)
        return cycles
    except Exception:  # noqa: BLE001
        return []


def active_live7_run_dir(config: dict, now: float | None = None) -> str | None:
    """The run dir of the newest active Live7 cycle, or None (also on any error)."""
    try:
        cycles = active_live7_cycles(config, now)
        return str(cycles[0].run_dir) if cycles else None
    except Exception:  # noqa: BLE001
        return None


# stardew_live() is polled from the bridge main loop (10 Hz). The Live7 check
# globs the run root (thousands of entries live), so it runs ONLY on a daemon
# refresher thread; callers read the cached bool (REVIEW_BRAIN F1).
_DEFAULT_LIVE_CACHE_SEC = 5.0
_MIN_REFRESH_SEC = 0.5
_live_cache_lock = threading.Lock()
_live_cache: dict = {"key": None, "at": 0.0, "value": False, "playing": False, "config": None}
_refresher: threading.Thread | None = None
_refresher_stop: threading.Event | None = None  # per-thread stop flag
_refresher_shutdown = False  # stop_live_refresher() ran: never restart
_refresher_lock = threading.Lock()
_refresher_wake = threading.Event()


def _live_key(cfg: dict) -> tuple:
    # Pure path strings (no filesystem access): safe on the caller's thread.
    return tuple(str(d) for d in _candidate_dirs(cfg))


def _refresh_interval(cfg: dict) -> float:
    try:
        ttl = float(cfg.get("stardew_live_cache_sec", _DEFAULT_LIVE_CACHE_SEC))
    except Exception:  # noqa: BLE001
        ttl = _DEFAULT_LIVE_CACHE_SEC
    return max(_MIN_REFRESH_SEC, ttl)


def _refresh_live_cache(config: dict | None = None, now: float | None = None) -> bool:
    """One scan + cache store. Runs on the refresher thread (tests may call it
    directly). Never raises."""
    try:
        with _live_cache_lock:
            cfg = config if config is not None else (_live_cache["config"] or {})
        key = _live_key(cfg)
        cycles = active_live7_cycles(cfg, now=now)
        value = bool(cycles)
        playing = value and cycles[0].status in {"active", "running"}
        with _live_cache_lock:
            _live_cache.update(key=key, at=time.time() if now is None else now,
                               value=value, playing=playing)
        return value
    except Exception:  # noqa: BLE001 — advisory; unreadable = not active
        return False


def _refresher_loop(stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            _refresh_live_cache()
            with _live_cache_lock:
                cfg = _live_cache["config"] or {}
            _refresher_wake.wait(_refresh_interval(cfg))
            _refresher_wake.clear()
    except Exception:  # noqa: BLE001 — a dead refresher is restarted lazily
        pass


def _ensure_refresher() -> None:
    global _refresher, _refresher_stop
    with _refresher_lock:
        if _refresher_shutdown:
            return  # shut down: keep serving the last cached answer
        if _refresher is not None and _refresher.is_alive():
            return
        stop = threading.Event()
        t = threading.Thread(target=_refresher_loop, args=(stop,),
                             name="stardew-live-refresh", daemon=True)
        _refresher, _refresher_stop = t, stop
        t.start()


def stop_live_refresher(timeout: float = 2.0) -> None:
    """Stop the refresher thread for good (bridge shutdown). Never raises."""
    global _refresher, _refresher_stop, _refresher_shutdown
    try:
        with _refresher_lock:
            _refresher_shutdown = True
            t, stop = _refresher, _refresher_stop
            _refresher, _refresher_stop = None, None
        if stop is not None:
            stop.set()
        _refresher_wake.set()
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
    except Exception:  # noqa: BLE001
        pass


def _reset_live_cache() -> None:
    """Tests: stop the refresher, forget the cached answer, allow a restart."""
    global _refresher_shutdown
    stop_live_refresher()
    with _live_cache_lock:
        _live_cache.update(key=None, at=0.0, value=False, playing=False, config=None)
    with _refresher_lock:
        _refresher_shutdown = False
    _refresher_wake.clear()


def live7_cycle_active(config: dict, now: float | None = None) -> bool:
    """active_live7_run_dir() is not None, as last computed by the refresher
    thread (every `stardew_live_cache_sec`, default 5 s). Never scans on the
    caller's thread and never raises. Until the first refresh for this run-dir
    set lands, the answer is False (`now` is accepted for compatibility)."""
    return _cached_cycle_fact(config, "value")


def live7_cycle_playing(config: dict) -> bool:
    """Use the same off-thread scan, with only active and running as play."""
    return _cached_cycle_fact(config, "playing")


def _cached_cycle_fact(config: dict, field: str) -> bool:
    try:
        cfg = config or {}
        key = _live_key(cfg)
        with _live_cache_lock:
            _live_cache["config"] = cfg
            hit = _live_cache["key"] == key
            value = bool(_live_cache[field])
        _ensure_refresher()
        if hit:
            return value
        _refresher_wake.set()  # run-dir set changed: refresh now, off-thread
        return False
    except Exception:  # noqa: BLE001 — advisory; unreadable = not active
        return False


def stardew_live(config: dict, *, runtime_running: bool = False,
                 now: float | None = None) -> bool:
    """Is ARTI playing Stardew right now?

    True when the bridge-hosted Stardew runtime is running (`runtime_running`,
    supplied by the bridge) or an operator-hosted Live7 cycle is active — the
    exact definition the two-driver guard uses (active_live7_run_dir), read
    from the background-refreshed cache.
    """
    if runtime_running:
        return True
    return live7_cycle_active(config, now=now)
