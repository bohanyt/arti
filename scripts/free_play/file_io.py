"""Compatibility entrypoints for the shared file IPC contract."""
import json
import os
import time
from pathlib import Path
from arti_runtime import ipc

RETRY_DELAYS = ipc.RETRY_DELAYS
_read_text = ipc.read_text
_retry_read = ipc.retry_read

def read_json(path):
    return _retry_read(lambda: json.loads(_read_text(path)))

def snapshot_stat(path):
    return _retry_read(lambda: Path(path).stat())

def snapshot_exists(path):
    try:
        snapshot_stat(path)
        return True
    except FileNotFoundError:
        return False
