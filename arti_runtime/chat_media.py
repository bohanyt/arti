"""Opt-in quiet-chat retention and session-local media hold accounting.

No I/O or game policy. The bridge records holds once; both speech TTLs and
optional relay admission use that same clock.
"""
from __future__ import annotations

from collections import deque
import threading

MEDIA_REACTION_INSTRUCTION = (
    "Sebut nama donaturnya persis seperti tertulis dan jumlahnya, bilang makasih, "
    "lalu reaksi ke satu hal dari isi videonya (pakai ringkasan di atas)."
)


class PendingChat(str):
    """Queue metadata travels with the existing text through both queue paths."""
    retain_pending = True

    def __new__(cls, text, *, answer_required=False):
        obj = super().__new__(cls, text)
        obj.answer_required = answer_required
        return obj


def append_chat(text, extra, *, answer_required=None):
    joined = f"{text}\n— {extra}"
    if answer_required is not None or getattr(text, "retain_pending", False):
        return PendingChat(joined, answer_required=bool(answer_required or getattr(text, "answer_required", False)))
    return joined


class QuietChat:
    def __init__(self):
        self._messages = deque()
        self._lock = threading.RLock()

    def observe(self, config, now):
        """Count the current eligible viewer message too; exactly eight is busy."""
        with self._lock:
            window = float(config.get("chat_busy_window_sec", 180.0))
            while self._messages and self._messages[0] <= now - window:
                self._messages.popleft()
            self._messages.append(now)
            return len(self._messages) < int(config.get("chat_busy_messages", 8))


class HoldClock:
    def __init__(self):
        self._intervals = []
        self._reactions = set()
        self._lock = threading.RLock()

    def record(self, start, until):
        with self._lock:
            if until <= start:
                return
            if self._intervals and start <= self._intervals[-1][1]:
                old_start, old_end = self._intervals[-1]
                self._intervals[-1] = (old_start, max(old_end, until))
            else:
                self._intervals.append((start, until))

    def end(self, now):
        """Clip skip/off truncates only the active interval, never past holds."""
        with self._lock:
            if self._intervals and self._intervals[-1][1] > now:
                start, _ = self._intervals[-1]
                self._intervals[-1] = (start, max(start, now))
            self._reactions.clear()

    def held(self, now):
        with self._lock:
            return bool(self._intervals and self._intervals[-1][0] <= now < self._intervals[-1][1])

    def reserve_reaction(self, key):
        with self._lock:
            self._reactions.add(key)

    def release_reaction(self, key):
        with self._lock:
            self._reactions.discard(key)

    def reaction_waiting(self):
        with self._lock:
            return bool(self._reactions)

    def age(self, created, now):
        with self._lock:
            overlap = sum(max(0.0, min(now, end) - max(created, start))
                          for start, end in self._intervals)
        return max(0.0, now - created - overlap)


holds = HoldClock()


def watch_held(config, now):
    return bool(config.get("media_watch_mode") and (holds.held(now) or holds.reaction_waiting()))
