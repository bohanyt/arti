"""File steps for the tests' fake mods, retried over the Windows sharing race.

A fake mod thread answers the command client through request.json/result.json.
On Windows the client (or a scanner) can hold one of those files at that very
instant: the client's result poll keeps result.json open, so replacing it raises
PermissionError, and a request being published can be read half-written or be
briefly in use. Without a retry the exception ends the fake mod's thread
silently and the test waits for a result that never comes.

Each helper retries only those transient errors for a short time, then
re-raises the last one, so a real fault still fails the test.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_PAUSE_S = 0.005


def _retry(step, transient: tuple[type[BaseException], ...], timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return step()
        except transient:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_PAUSE_S)


def read_json_retry(path: str | os.PathLike[str], timeout_s: float = 2.0):
    """Read a JSON file the fake mod has just seen exist."""
    path = Path(path)
    return _retry(
        lambda: json.loads(path.read_text(encoding="utf-8")),
        (PermissionError, FileNotFoundError, json.JSONDecodeError),
        timeout_s,
    )


def unlink_retry(path: str | os.PathLike[str], timeout_s: float = 2.0) -> None:
    path = Path(path)
    _retry(path.unlink, (PermissionError,), timeout_s)


def replace_retry(src: str | os.PathLike[str], dst: str | os.PathLike[str], timeout_s: float = 2.0) -> None:
    src = Path(src)
    _retry(lambda: src.replace(dst), (PermissionError,), timeout_s)
