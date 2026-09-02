"""Priority voice trigger queue for live bridge (donation > yt_chat > mic > curious)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

# Lower number = higher priority. Donasi paling atas — orang sudah bayar.
# "game" (event Minecraft) SENGAJA type sendiri, bukan reuse "curious":
# curious kena cull khusus (drop saat yt pending / yt_chat masuk) — reaksi
# kematian Arti tidak boleh ikut kebuang. Di bawah mic: manusia selalu menang.
# "game" dipisah dari "curious" (audit [date removed]): dulu keduanya 4 = SERI,
# dan seri dipecah urutan masuk — jadi celetukan proaktif yang antre 1 detik
# lebih dulu mengalahkan reaksi KEMATIAN, yang baru keluar 10-20 detik kemudian
# dengan teks "Kamu BARU AJA MATI".
# Tetap DI BAWAH mic: aturan lama "omongan manusia selalu menang" tidak dicabut
# — reaksi game tidak lagi hilang (sekarang kebal drain-newest), cuma antre.
_TRIGGER_PRIORITY = {
    "donation": 0, "yt_chat": 1, "video": 2, "mic": 3, "game": 4, "curious": 5,
}
_DEFAULT_PRIORITY = 5


@dataclass
class QueuedVoiceTrigger:
    """Wrapper with enqueue metadata."""

    text: str
    trigger_type: str = "mic"
    viewer_name: str | None = None
    enqueued_at: float = 0.0
    turn_id: str | None = None

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
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._items)

    def depth_for(self, trigger_type: str) -> int:
        with self._lock:
            self._purge_expired()
            return sum(1 for it in self._items if it.trigger_type == trigger_type)

    def has_yt_pending(self) -> bool:
        with self._lock:
            return self.depth_for("yt_chat") > 0

    def _purge_expired(self) -> None:
        # donation/video KEBAL TTL — "tidak pernah di-drop" (orang sudah bayar/
        # nunggu playback; hold media share bisa > 60 dtk). Temuan audit.
        now = time.time()
        self._items = [
            it for it in self._items
            if it.trigger_type in ("donation", "video", "game")
            or (now - it.enqueued_at) <= self.ttl_sec
        ]

    def enqueue(
        self,
        item: QueuedVoiceTrigger,
        *,
        prepare: Callable[[QueuedVoiceTrigger], None] | None = None,
    ) -> bool:
        """Add trigger; returns False if dropped (overflow)."""
        with self._lock:
            self._purge_expired()
            item.enqueued_at = item.enqueued_at or time.time()

            if item.trigger_type == "curious" and self.has_yt_pending():
                return False

            if prepare is not None:
                prepare(item)

            if item.trigger_type == "yt_chat" and item.viewer_name:
                self._items = [
                    it
                    for it in self._items
                    if not (
                        it.trigger_type == "yt_chat"
                        and it.viewer_name == item.viewer_name
                    )
                ]

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
        with self._lock:
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
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.trigger_type != "curious"]
            return before - len(self._items)

    def enqueue_replacing(
        self,
        item: QueuedVoiceTrigger,
        predicate: Callable[[QueuedVoiceTrigger], bool],
        *,
        prepare: Callable[[QueuedVoiceTrigger], None] | None = None,
    ) -> int:
        """Siapkan metadata, lalu drop+append sebagai satu transaksi."""
        with self._lock:
            self._purge_expired()
            item.enqueued_at = item.enqueued_at or time.time()
            if prepare is not None:
                prepare(item)
            before = len(self._items)
            self._items = [old for old in self._items if not predicate(old)]
            dropped = before - len(self._items)
            self._items.append(item)
            return dropped

    def clear(self) -> None:
        with self._lock:
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
