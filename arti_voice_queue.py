"""Priority voice trigger queue for live bridge (yt_chat > mic > curious)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# Lower number = higher priority.
_TRIGGER_PRIORITY = {"yt_chat": 0, "mic": 1, "curious": 2}
_DEFAULT_PRIORITY = 3


@dataclass
class QueuedVoiceTrigger:
    """Wrapper with enqueue metadata."""

    text: str
    trigger_type: str = "mic"
    viewer_name: str | None = None
    enqueued_at: float = 0.0

    def priority(self) -> int:
        return _TRIGGER_PRIORITY.get(self.trigger_type, _DEFAULT_PRIORITY)


class VoiceTriggerQueue:
    """In-memory FIFO with priority dequeue, TTL, per-viewer dedup, max depth."""

    def __init__(
        self,
        *,
        max_yt: int = 2,
        ttl_sec: float = 60.0,
    ) -> None:
        self.max_yt = max(1, int(max_yt))
        self.ttl_sec = float(ttl_sec)
        self._items: list[QueuedVoiceTrigger] = []

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._items)

    def depth_for(self, trigger_type: str) -> int:
        self._purge_expired()
        return sum(1 for it in self._items if it.trigger_type == trigger_type)

    def has_yt_pending(self) -> bool:
        return self.depth_for("yt_chat") > 0

    def _purge_expired(self) -> None:
        now = time.time()
        self._items = [
            it for it in self._items if (now - it.enqueued_at) <= self.ttl_sec
        ]

    def enqueue(self, item: QueuedVoiceTrigger) -> bool:
        """Add trigger; returns False if dropped (overflow)."""
        self._purge_expired()
        item.enqueued_at = item.enqueued_at or time.time()

        if item.trigger_type == "yt_chat" and item.viewer_name:
            self._items = [
                it
                for it in self._items
                if not (
                    it.trigger_type == "yt_chat"
                    and it.viewer_name == item.viewer_name
                )
            ]

        if item.trigger_type == "curious" and self.has_yt_pending():
            return False

        if item.trigger_type == "yt_chat":
            yt_count = self.depth_for("yt_chat")
            if yt_count >= self.max_yt:
                for i, old in enumerate(self._items):
                    if old.trigger_type == "yt_chat":
                        print(
                            f"[Queue] Penuh (max {self.max_yt}) — buang chat tertua "
                            f"({old.viewer_name or 'viewer'})"
                        )
                        self._items.pop(i)
                        break
            dropped = self.drop_curious()
            if dropped:
                print(f"[Queue] Curious dibuang ({dropped}) — prioritas yt_chat")

        self._items.append(item)
        return True

    def dequeue(self) -> QueuedVoiceTrigger | None:
        self._purge_expired()
        if not self._items:
            return None
        best_idx = min(
            range(len(self._items)),
            key=lambda i: (
                self._items[i].priority(),
                self._items[i].enqueued_at,
            ),
        )
        return self._items.pop(best_idx)

    def drop_curious(self) -> int:
        before = len(self._items)
        self._items = [it for it in self._items if it.trigger_type != "curious"]
        return before - len(self._items)

    def clear(self) -> None:
        self._items.clear()


def wrap_trigger(raw: Any) -> QueuedVoiceTrigger:
    """Normalize bridge VoiceTrigger or legacy tuple."""
    if isinstance(raw, QueuedVoiceTrigger):
        return raw
    if hasattr(raw, "text"):
        return QueuedVoiceTrigger(
            text=str(raw.text),
            trigger_type=str(getattr(raw, "trigger_type", "mic") or "mic"),
            viewer_name=getattr(raw, "viewer_name", None),
        )
    if isinstance(raw, tuple) and raw:
        return QueuedVoiceTrigger(
            text=str(raw[0]),
            trigger_type=str(raw[1]) if len(raw) > 1 else "mic",
            viewer_name=raw[2] if len(raw) > 2 else None,
        )
    return QueuedVoiceTrigger(text=str(raw), trigger_type="mic")
