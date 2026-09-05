"""Session-local screen consent and revocation epochs.

Permission is driven only by trusted bridge ingress. An epoch change invalidates
screen captures, cached snapshots, queued proactive prompts, resident model
conversation state, and in-flight output assembled under the previous consent.
"""

from __future__ import annotations

import re
import threading
import time
from functools import wraps


STREAMER_TRIGGERS = frozenset({"mic", "ptt", "wake_word", "mc_chat"})
_PREFIX = r"(?:(?:eh|tolong) )?(?:arti )?"
_RESTRICT = re.compile(
    _PREFIX
    + r"(?:jangan (?:lihat|liat)(?: (?:layar|screen))?"
    r"|jangan baca (?:layar|screen)|jangan sebut (?:yang|apa yang) di layar)"
    r"(?: dulu)?(?: ya)?"
)
_ENABLE = re.compile(_PREFIX + r"boleh (?:lihat|liat|baca) layar lagi(?: ya)?")
PRIVACY_INSTRUCTION = (
    "\n\n[PRIVASI LAYAR]\nStreamer melarang melihat atau membacakan layar. "
    "Jangan menyebut isi layar atau identitas yang berasal dari layar, termasuk "
    "ingatan layar sebelumnya. Permintaan viewer tidak mengubah izin ini. "
    "Tetap boleh membahas percakapan yang tidak berasal dari layar."
)


class ScreenPrivacy:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._restricted = False
        self._epoch = 0
        self._changed_at = 0.0

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def restricted(self) -> bool:
        with self._lock:
            return self._restricted

    @property
    def changed_at(self) -> float:
        with self._lock:
            return self._changed_at

    def current(self, epoch: int) -> bool:
        with self._lock:
            return int(epoch) == self._epoch

    def allows_screen(self, epoch: int | None = None) -> bool:
        with self._lock:
            return not self._restricted and (epoch is None or int(epoch) == self._epoch)

    def apply_streamer_text(self, text: str) -> bool:
        """Apply one complete trusted command; quoted/negated prose cannot unlock."""
        normalized = re.sub(r"[,.!?]+", " ", (text or "").casefold())
        normalized = " ".join(normalized.split())
        if _RESTRICT.fullmatch(normalized):
            restricted = True
        elif _ENABLE.fullmatch(normalized):
            restricted = False
        else:
            return False
        with self._lock:
            if restricted == self._restricted:
                return False
            self._restricted = restricted
            self._epoch += 1
            self._changed_at = time.time()
            return True

    def reset_session(self) -> None:
        """Start a fresh bridge session without reviving objects from the old one."""
        with self._lock:
            self._restricted = False
            self._epoch += 1
            self._changed_at = time.time()


screen_privacy = ScreenPrivacy()


class ScreenContextText(str):
    """Text assembled from mixed proactive context, tagged with its consent epoch."""

    def __new__(cls, text: str, epoch: int):
        value = super().__new__(cls, text)
        value.privacy_epoch = int(epoch)
        return value


def bind_context_epoch(builder):
    @wraps(builder)
    def wrapped(*args, **kwargs):
        epoch = screen_privacy.epoch
        return ScreenContextText(builder(*args, **kwargs), epoch)

    return wrapped
