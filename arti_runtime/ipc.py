"""File IPC contract: complete snapshots, delete-sharing readers, bounded retries.

One publisher owns each snapshot/append log. Create-once commands retain their
no-overwrite semantics; this module never retries an executed command.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

import contextlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time

RETRY_DELAYS = (0.005, 0.01, 0.02, 0.04, 0.08)
LOG = logging.getLogger(__name__)
_APPEND_LOCKS = {}
_APPEND_LOCKS_GUARD = threading.Lock()


def _append_lock(path):
    key = os.path.normcase(os.path.realpath(path))
    with _APPEND_LOCKS_GUARD:
        return _APPEND_LOCKS.setdefault(key, threading.Lock())


def _append_fd(path):
    """Append-only kernel handle; Windows must not emulate append by seek/write."""
    if os.name != 'nt':
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    import _winapi
    import msvcrt
    # FILE_APPEND_DATA without FILE_WRITE_DATA makes EOF placement atomic even
    # between processes. Retain READ|WRITE|DELETE sharing for live tail/archives.
    handle = _winapi.CreateFile(os.path.abspath(path), 0x4, 7, 0, 4, 0, 0)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise


def retry(operation):
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return operation()
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33)
            if not transient or attempt == len(RETRY_DELAYS):
                raise
            W.call('ipc_retry', 89, time.sleep, RETRY_DELAYS[attempt])


def retry_read(operation):
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return operation()
        except (OSError, ValueError) as exc:
            transient = (isinstance(exc, (FileNotFoundError, json.JSONDecodeError, PermissionError))
                         or getattr(exc, "winerror", None) in (5, 32, 33))
            if not transient or attempt == len(RETRY_DELAYS):
                raise
            W.call('ipc_retry', 90, time.sleep, RETRY_DELAYS[attempt])


def _open(path, mode="r", encoding="utf-8-sig", errors="strict"):
    if mode == 'ab':
        fd = _append_fd(path)
        try:
            # The kernel handle owns append semantics; the unbuffered wrapper
            # owns lifetime only (no CRT seek-to-EOF emulation).
            return os.fdopen(fd, 'wb', buffering=0)
        except BaseException:
            os.close(fd)
            raise
    if os.name != "nt":
        return open(path, mode, **({} if "b" in mode else dict(encoding=encoding, errors=errors)))
    import _winapi
    import msvcrt
    append = "a" in mode
    handle = _winapi.CreateFile(os.path.abspath(path), 0x40000000 if append else 0x80000000,
                               7, 0, 4 if append else 3, 0, 0)
    try:
        fd = msvcrt.open_osfhandle(handle, (os.O_WRONLY | os.O_APPEND if append else os.O_RDONLY) | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    return os.fdopen(fd, mode, **({} if "b" in mode else dict(encoding=encoding, errors=errors)))


def open_shared(path, mode="r", *, encoding="utf-8-sig", errors="strict", retry_missing=False):
    """Short-lived read/append handles share READ | WRITE | DELETE on Windows."""
    if mode not in ("r", "rb", "a", "ab"):
        raise ValueError("IPC open supports read/append only")
    # Missing optional inbox/control/result files mean "no message". Waiting for
    # them here would consume an action lease; required snapshots opt in.
    return (retry_read if retry_missing else retry)(lambda: _open(path, mode, encoding, errors))


def read_bytes(path, limit=-1, *, retry_missing=False):
    def read():
        with _open(path, "rb") as stream:
            return stream.read(limit)
    return (retry_read if retry_missing else retry)(read)


def read_text(path, *, encoding="utf-8-sig", errors="strict"):
    return read_bytes(path).decode(encoding, errors)


def read_json(path):
    return json.loads(read_text(path))


def _posix_replace(source, target):
    if os.name != "nt":
        os.replace(source, target)
        return
    import ctypes
    from ctypes import wintypes
    import _winapi
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    rename.restype = wintypes.BOOL
    name = os.path.abspath(target).encode("utf-16-le")
    name_at = 2 * ctypes.sizeof(ctypes.c_void_p) + 4
    buffer = ctypes.create_string_buffer(name_at + len(name) + 2)
    ctypes.c_uint32.from_buffer(buffer).value = 3  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
    ctypes.c_uint32.from_buffer(buffer, 2 * ctypes.sizeof(ctypes.c_void_p)).value = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + name_at, name, len(name))
    handle = _winapi.CreateFile(os.path.abspath(source), 0x10000, 7, 0, 3, 0, 0)
    try:
        if not rename(handle, 22, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _winapi.CloseHandle(handle)


def replace(source, target):
    retry(lambda: _posix_replace(source, target))


@W.span('journal_archive', 87, runtime=False)
def atomic_write(path, content, *, create_only=False, fsync=False):
    path = Path(path)
    temp = None
    phase = "write_temp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            stream = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            if fsync:
                os.fsync(stream.fileno())
        phase = "publish"
        if create_only:
            retry(lambda: os.link(temp, path))
        else:
            replace(temp, path)
    except OSError as exc:
        exc.ipc_phase = phase
        raise
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)


def write_json(path, value, *, critical=True, create_only=False, fsync=False):
    return publish(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                   critical=critical, create_only=create_only, fsync=fsync)


def create_json_once(path, value, *, fsync=False):
    """An existing command/sentinel wins; expected contention is not an IO fault."""
    try:
        atomic_write(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                     create_only=True, fsync=fsync)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        LOG.error("FAULT ipc_write file=%s error=%s", Path(path).name, type(exc).__name__)
        raise


def publish(path, content, *, critical=True, create_only=False, fsync=False):
    try:
        atomic_write(path, content, create_only=create_only, fsync=fsync)
        return True
    except OSError as exc:
        LOG.error("FAULT ipc_write file=%s error=%s", Path(path).name, type(exc).__name__)
        if critical:
            raise
        return False


@W.span('journal_archive', 88, runtime=False)
def append_text(path, content, *, critical=True):
    """ADR 226: one complete UTF-8 append in one OS write, serialized per file.

    Synchronous completion retains flush-before-arm ordering. No fsync is added.
    Never retry a short/failed write: a partial row remains visible to faulting
    readers instead of duplicating its prefix or hiding lost evidence.
    """
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = content.encode('utf-8')
        with _append_lock(path):
            with open_shared(path, 'ab') as stream:
                if os.write(stream.fileno(), data) != len(data):
                    raise OSError('short append write')
        return True
    except OSError as exc:
        LOG.error("FAULT ipc_append file=%s error=%s", Path(path).name, type(exc).__name__)
        if critical:
            raise
        return False
