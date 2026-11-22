"""Codex weekly usage from the newest session log (names/limits only). Copied into the repo
from tools/usage_pace.py so livepath never imports outside this checkout. Unreadable → error, never a guess.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path


def read_codex_weekly(sessions_root=None):
    """Return {used_percent, left_percent, resets_h} or {error: reason}. Never guesses a share."""
    root = Path(sessions_root or os.path.expanduser("~/.codex/sessions"))
    try:
        files = sorted(glob.glob(os.path.join(str(root), "*", "*", "*", "*.jsonl")),
                       key=os.path.getmtime)[-3:]
    except OSError as exc:
        return dict(error=type(exc).__name__)
    if not files:
        return dict(error="no_session_log")
    last = None
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = re.search(r'"rate_limits":(\{.*?"plan_type":"[^"]*"[^}]*\})', line)
                    if m:
                        last = m.group(1)
        except OSError:
            continue
    if not last:
        return dict(error="no_rate_limits")
    try:
        rl = json.loads(last)
        primary = rl["primary"]
        used = float(primary["used_percent"])
        if used < 0 or used > 100:
            return dict(error="used_percent_out_of_range")
        left_h = (primary["resets_at"] - time.time()) / 3600
        return dict(used_percent=used, left_percent=100.0 - used, resets_h=left_h)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return dict(error=type(exc).__name__)
