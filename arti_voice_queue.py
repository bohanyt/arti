"""Priority voice queue: current owner mic, paid events, chat, initiative."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from arti_runtime.chat_media import append_chat

# Lower number = higher priority. Donasi paling atas — orang sudah bayar.
# "game" (event Minecraft) SENGAJA type sendiri, bukan reuse "curious":
# curious kena cull khusus (drop saat yt pending / yt_chat masuk) — reaksi
# kematian Arti tidak boleh ikut kebuang. Di bawah mic: manusia selalu menang.
# "game" dipisah dari "curious" (audit 2026-08-05): dulu keduanya 4 = SERI,
# dan seri dipecah urutan masuk — jadi celetukan proaktif yang antre 1 detik
# lebih dulu mengalahkan reaksi KEMATIAN, yang baru keluar 10-20 detik kemudian
# dengan teks "Kamu BARU AJA MATI".
# Tetap DI BAWAH mic: aturan lama "omongan manusia selalu menang" tidak dicabut
# — reaksi game tidak lagi hilang (sekarang kebal drain-newest), cuma antre.
_TRIGGER_PRIORITY = {
    "mic": -1, "ptt": -1, "wake_word": -1, "mic_pasif": -1,
    "donation": 0, "yt_chat": 1, "video": 2, "game": 4, "curious": 5,
    "own_play": 6,
}
_DEFAULT_PRIORITY = 5
# Giliran yang boleh dibuang demi chat viewer: curious dan mic_pasif (ucapan
# PTT-mati Bohan, owner 2026-10-23) — keduanya bukan panggilan langsung.
_CULLABLE = ("curious",)
HUMAN_KINDS = ("mic", "ptt", "wake_word", "mic_pasif", "mc_chat", "yt_chat")


@dataclass
class QueuedVoiceTrigger:
    """Wrapper with enqueue metadata."""

    text: str
    trigger_type: str = "mic"
    viewer_name: str | None = None
    enqueued_at: float = 0.0
    turn_id: str | None = None
    stardew_voice_epoch: int | None = None
    name_addressed: bool = False
    owner_marked: bool = True
    rotation_rank: int = 0

    def priority(self) -> int:
        if self.trigger_type == "mic_pasif" and not self.owner_marked:
            return 3
        return _TRIGGER_PRIORITY.get(self.trigger_type, _DEFAULT_PRIORITY)


class VoiceTriggerQueue:
    """In-memory FIFO with priority dequeue, TTL, per-viewer dedup, max depth."""

    def __init__(
        self,
        *,
        max_yt: int = 2,
        ttl_sec: float = 60.0,
        priority_overrides: dict | None = None,
        preserve_humans: bool = False,
        human_age: Callable[[float, float], float] | None = None,
    ) -> None:
        self.max_yt = max(1, int(max_yt))
        self.preserve_humans = preserve_humans
        self._human_age = human_age
        # Mabar (owner 2026-11-10): game/story lines go ahead of viewer chat. Live passes none.
        self._priority = {**_TRIGGER_PRIORITY, **(priority_overrides or {})}
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

    def has_name_pending(self) -> bool:
        with self._lock:
            return any(it.name_addressed for it in self._items)

    def _purge_expired(self) -> None:
        # donation/video KEBAL TTL — "tidak pernah di-drop" (orang sudah bayar/
        # nunggu playback; hold media share bisa > 60 dtk). Temuan audit.
        now = time.time()
        before = len(self._items)
        self._items = [
            it for it in self._items
            if it.trigger_type in ("donation", "video", "game")
            or (self.preserve_humans and it.trigger_type in HUMAN_KINDS)
            # The bridge owns this trusted epoch until turn-finally/explicit
            # discard. Silent TTL eviction would strand the runtime voice hold.
            or it.stardew_voice_epoch is not None
            or getattr(it.text, "answer_required", False)
            or (self._human_age(it.enqueued_at, now) if self._human_age is not None
                and it.trigger_type in HUMAN_KINDS else now - it.enqueued_at) <= self.ttl_sec
        ]
        if len(self._items) != before:  # A count only, never a viewer's text or name.
            print(f"[Queue] Kedaluwarsa {self.ttl_sec:.0f} dtk: {before - len(self._items)} dibuang")

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

            if item.trigger_type in _CULLABLE and self.has_yt_pending():
                return False

            if prepare is not None:
                prepare(item)

            retain_chat = bool(getattr(item.text, "retain_pending", False))
            if not self.preserve_humans and not retain_chat and item.trigger_type == "yt_chat" and item.viewer_name:
                self._items = [
                    it
                    for it in self._items
                    if not (
                        it.trigger_type == "yt_chat"
                        and it.viewer_name == item.viewer_name
                        and not getattr(it.text, "retain_pending", False)
                    )
                ]

            if item.trigger_type == "yt_chat":
                yt_count = self.depth_for("yt_chat")
                if not self.preserve_humans and not retain_chat and yt_count >= self.max_yt:
                    for i, old in enumerate(self._items):
                        if old.trigger_type == "yt_chat" and not getattr(old.text, "retain_pending", False):
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

    def dequeue(self, *, skip_types=()) -> QueuedVoiceTrigger | None:
        with self._lock:
            self._purge_expired()
            eligible = [i for i, it in enumerate(self._items) if it.trigger_type not in skip_types]
            if not eligible:
                return None
            best_idx = min(eligible, key=lambda i: self._dequeue_key(self._items[i]))
            return self._items.pop(best_idx)

    def dequeue_best(self, allowed: Callable[[QueuedVoiceTrigger], bool]) -> QueuedVoiceTrigger | None:
        """Best item by the normal order among those `allowed` accepts; others stay queued untouched."""
        with self._lock:
            self._purge_expired()
            eligible = [i for i, it in enumerate(self._items) if allowed(it)]
            if not eligible:
                return None
            best_idx = min(eligible, key=lambda i: self._dequeue_key(self._items[i]))
            return self._items.pop(best_idx)

    def _dequeue_key(self, it: QueuedVoiceTrigger) -> tuple:
        if it.name_addressed:
            type_rank = -2
        elif it.trigger_type == "mic_pasif" and not it.owner_marked:
            type_rank = 3
        else:
            type_rank = self._priority.get(it.trigger_type, _DEFAULT_PRIORITY)
        return (type_rank, it.rotation_rank, it.enqueued_at)

    def merge_viewer_chat(self, viewer_name: str, extra_line: str, *, cap: int | None = 8,
                          max_age: float | None = None, answer_required: bool | None = None,
                          skip_turn_id: str | None = None) -> bool:
        """Append a consecutive line onto one waiting yt_chat for this viewer (never onto ``skip_turn_id``)."""
        with self._lock:
            self._purge_expired()
            for it in self._items:
                if it.trigger_type != "yt_chat" or it.viewer_name != viewer_name:
                    continue
                if skip_turn_id is not None and it.turn_id == skip_turn_id:
                    continue
                if max_age is not None and time.time() - it.enqueued_at > max_age:
                    continue
                n = int(getattr(it, "_merged_n", 1) or 1)
                if cap is not None and n >= cap:
                    return True
                extra = str(extra_line or "").strip()
                if extra:
                    it.text = append_chat(it.text, extra, answer_required=answer_required)
                it._merged_n = n + 1
                if it._merged_n >= 3:
                    it.rotation_rank = max(int(it.rotation_rank or 0), 1)
                return True
            return False

    def snapshot(self) -> tuple[QueuedVoiceTrigger, ...]:
        """ADR 172: non-destructive view; selection never drains unchosen humans."""
        with self._lock:
            self._purge_expired()
            return tuple(self._items)

    def take(self, item: QueuedVoiceTrigger) -> bool:
        """Remove exactly the selected identity, without equality/dedup of human text."""
        with self._lock:
            for index, pending in enumerate(self._items):
                if pending is item:
                    self._items.pop(index)
                    return True
            return False

    def drop_if(self, predicate: Callable[[QueuedVoiceTrigger], bool]) -> int:
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if not predicate(it)]
            return before - len(self._items)

    def drop_curious(self) -> int:
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.trigger_type not in _CULLABLE]
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

    def clear(self, *, keep_types: tuple[str, ...] = ()) -> list[QueuedVoiceTrigger]:
        with self._lock:
            discarded = [it for it in self._items if it.trigger_type not in keep_types
                         and not getattr(it.text, "retain_pending", False)]
            self._items = [it for it in self._items if it.trigger_type in keep_types
                           or getattr(it.text, "retain_pending", False)]
            return discarded


def wrap_trigger(raw: Any) -> QueuedVoiceTrigger:
    """Normalize bridge VoiceTrigger or legacy tuple."""
    if isinstance(raw, QueuedVoiceTrigger):
        return raw
    if hasattr(raw, "text"):
        return QueuedVoiceTrigger(
            text=str(raw.text),
            trigger_type=str(getattr(raw, "trigger_type", "mic") or "mic"),
            viewer_name=getattr(raw, "viewer_name", None),
            stardew_voice_epoch=getattr(raw, "stardew_voice_epoch", None),
            owner_marked=bool(getattr(raw, "owner_marked", True)),
            rotation_rank=int(getattr(raw, "rotation_rank", 0) or 0),
        )
    if isinstance(raw, tuple) and raw:
        return QueuedVoiceTrigger(
            text=str(raw[0]),
            trigger_type=str(raw[1]) if len(raw) > 1 else "mic",
            viewer_name=raw[2] if len(raw) > 2 else None,
        )
    return QueuedVoiceTrigger(text=str(raw), trigger_type="mic")
