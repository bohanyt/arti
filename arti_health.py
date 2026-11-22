"""Periodic process-health line + telemetry row for long streams (audit L06).

Content-free by design: numbers only (memory, threads, tasks, pool slots, disk,
uptime); never chat text, viewer names or prompts. Runs on its own daemon
thread and swallows every error, so it can never raise into the bridge loop.
No new dependency: RSS comes from ctypes (Windows) or /proc (Linux).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

_DEFAULT_INTERVAL_SEC = 600.0
_stop = threading.Event()
_thread: threading.Thread | None = None


def rss_bytes() -> int | None:
    """Resident set size (Windows working set) of this process, or None."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            # Private library objects: setting argtypes must not leak into
            # other users of ctypes.windll.
            k32 = ctypes.WinDLL("kernel32")
            psapi = ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.GetCurrentProcess.argtypes = []
            fn = psapi.GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            if fn(k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        statm = Path("/proc/self/statm")
        if statm.is_file():
            pages = int(statm.read_text().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:  # noqa: BLE001
        return None
    return None


def _asyncio_task_count(loop) -> int | None:
    if loop is None:
        return None
    try:
        if loop.is_closed():
            return None
        return len(asyncio.all_tasks(loop))
    except Exception:  # noqa: BLE001 — cross-thread read; best effort
        return None


def _pool_stats() -> dict[str, dict[str, int]] | None:
    try:
        import arti_executors

        return arti_executors.pool_stats()
    except Exception:  # noqa: BLE001
        return None


def _disk_free_bytes(path: Path | str | None) -> int | None:
    if not path:
        return None
    try:
        p = Path(path)
        while not p.exists() and p.parent != p:
            p = p.parent
        return int(shutil.disk_usage(str(p)).free)
    except Exception:  # noqa: BLE001
        return None


def collect(*, loop=None, disk_path: Path | str | None = None,
            started_monotonic: float | None = None) -> dict[str, Any]:
    """One snapshot; every field is independently best-effort (None on failure)."""
    try:
        threads = threading.active_count()
    except Exception:  # noqa: BLE001
        threads = None
    uptime = None
    if started_monotonic is not None:
        uptime = max(0, int(time.monotonic() - started_monotonic))
    return {
        "rss_bytes": rss_bytes(),
        "threads": threads,
        "asyncio_tasks": _asyncio_task_count(loop),
        "pools": _pool_stats(),
        "disk_free_bytes": _disk_free_bytes(disk_path),
        "uptime_sec": uptime,
    }


def _mb(n) -> str:
    return "?" if n is None else f"{n / (1024 * 1024):.0f}MB"


def _gb(n) -> str:
    return "?" if n is None else f"{n / (1024 ** 3):.1f}GB"


def format_line(stats: dict[str, Any]) -> str:
    pools = stats.get("pools") or {}
    pool_txt = " ".join(
        f"{name}:{p.get('busy', '?')}/{p.get('queued', '?')}" for name, p in pools.items()
    ) or "?"
    up = stats.get("uptime_sec")
    up_txt = "?" if up is None else f"{up // 3600}h{(up % 3600) // 60:02d}m"

    def _v(key):
        val = stats.get(key)
        return "?" if val is None else val

    return (
        f"[Health] rss={_mb(stats.get('rss_bytes'))} threads={_v('threads')} "
        f"tasks={_v('asyncio_tasks')} pools(busy/queued)={pool_txt} "
        f"disk_free={_gb(stats.get('disk_free_bytes'))} uptime={up_txt}"
    )


def _record_telemetry(stats: dict[str, Any], config: dict) -> None:
    import arti_api_telemetry as tel

    tel.record_call(subsystem="health", provider="bridge", model="health",
                    latency_ms=0, ok=True, extra=stats, config=config)


def emit_health(config: dict, *, loop=None, disk_path=None, started_monotonic=None,
                record: Callable[[dict[str, Any], dict], None] | None = None,
                log: Callable[[str], None] = print) -> dict[str, Any] | None:
    """Log one line + one telemetry row. Never raises."""
    try:
        stats = collect(loop=loop, disk_path=disk_path, started_monotonic=started_monotonic)
    except Exception:  # noqa: BLE001
        return None
    try:
        log(format_line(stats))
    except Exception:  # noqa: BLE001
        pass
    try:
        # Satu baris ringkasan provider (arti_provider_health) — hanya saat aktif.
        import arti_provider_health as ph  # noqa: PLC0415

        if ph.enabled():
            stats["providers"] = ph.summary_line()
            log(f"[Health] {stats['providers']}")
    except Exception:  # noqa: BLE001
        pass
    try:
        (record or _record_telemetry)(stats, config)
    except Exception:  # noqa: BLE001
        pass
    return stats


def start_health_monitor(config: dict, *, loop=None, disk_path=None,
                         started_monotonic: float | None = None,
                         record: Callable[[dict[str, Any], dict], None] | None = None,
                         log: Callable[[str], None] = print) -> threading.Thread | None:
    """Start the daemon thread (one per process). ``health_log_interval_sec`` <= 0 disables."""
    global _thread
    try:
        interval = float(config.get("health_log_interval_sec", _DEFAULT_INTERVAL_SEC))
    except (TypeError, ValueError):
        interval = _DEFAULT_INTERVAL_SEC
    if interval <= 0:
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    started = time.monotonic() if started_monotonic is None else started_monotonic
    _stop.clear()

    def _run() -> None:
        while not _stop.wait(interval):
            emit_health(config, loop=loop, disk_path=disk_path,
                        started_monotonic=started, record=record, log=log)

    _thread = threading.Thread(target=_run, name="arti-health", daemon=True)
    _thread.start()
    return _thread


def stop_health_monitor(timeout: float = 1.0) -> None:
    global _thread
    _stop.set()
    t, _thread = _thread, None
    if t is not None:
        t.join(timeout)
