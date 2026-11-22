"""A clip's first reaction and optional late-summary follow-up, serialized once."""
from __future__ import annotations

import threading
import time

SUMMARY_UNAVAILABLE_INSTRUCTION = (
    "Ringkasan video belum tersedia. Jangan mengarang isi videonya. "
    "Sebut nama donatur persis seperti tertulis dan jumlahnya, bilang makasih, "
    "lalu tanggapi hanya judul, pesan donatur, atau transkrip yang tersedia."
)
FOLLOWUP_INSTRUCTION = "Ringkasan baru tersedia: beri satu kalimat reaksi pada satu hal dari ringkasan ini."


class ClipReaction:
    """Callbacks only enqueue/emit; the slow ingest never owns the first-reaction clock."""
    def __init__(self, *, ready_prompt, fallback_prompt, queue, emit, active):
        self.done = threading.Event()
        self._lock = threading.RLock()
        self._ready_prompt, self._fallback_prompt = ready_prompt, fallback_prompt
        self._queue, self._emit, self._active = queue, emit, active
        self.title = ""
        self.digest = ""
        self.failed = False
        self._first_sent = False
        self._event_sent = False

    def set_title(self, title):
        with self._lock:
            self.title = title

    def complete(self, digest, *, failed=False):
        with self._lock:
            if self.done.is_set():
                return
            self.digest = str(digest or "").strip()
            self.failed = bool(failed or not self.digest)
            self.done.set()
            if self._active():
                self._event("summary_failed" if self.failed else
                            "summary_late" if self._first_sent else "summary_ready")
                if self._first_sent and not self.failed:
                    self._queue(self._ready_prompt(self.digest, self.title) + "\n" + FOLLOWUP_INSTRUCTION)

    def first(self):
        with self._lock:
            if self._first_sent or not self._active():
                return
            prompt = (self._ready_prompt(self.digest, self.title) if self.digest and not self.failed
                      else self._fallback_prompt(self.title, self.digest if self.failed else ""))
            # A late completion cannot enqueue its follow-up ahead of this first turn.
            self._queue(prompt)
            self._first_sent = True

    def _event(self, status):
        if not self._event_sent:
            self._event_sent = True
            self._emit(status)


class SummaryBudget:
    """Hard result deadline even if a transport ignores its own socket timeout.

    Transport timeouts also receive the remaining budget. Late completion is
    ignored by ClipReaction, so it cannot produce a follow-up or another event.
    """
    def __init__(self, reaction, seconds, *, clock=time.monotonic):
        self.reaction, self.clock = reaction, clock
        self.seconds = max(0.0, float(seconds))
        self.deadline = clock() + self.seconds
        self.timer = threading.Timer(self.seconds, self._timeout)
        self.timer.daemon = True

    def start(self):
        self.timer.start()

    def _timeout(self):
        self.reaction.complete("", failed=True)

    def remaining(self):
        return 0.0 if self.reaction.done.is_set() else min(self.seconds, max(0.0, self.deadline - self.clock()))

    def complete(self, digest, *, failed=False):
        if self.clock() >= self.deadline:
            self._timeout()
        else:
            self.reaction.complete(digest, failed=failed)
        self.timer.cancel()

    def cancel(self):
        self.timer.cancel()
