"""fix54I (ADR 73): leased move bounds sized from measured hand-off time under machine load (bounded).

a39 (RESEARCH_A39_UNANALYSED_2026-11-03 §4): 17 of 24 ``approach_uncertain`` rows were night walk-home legs; 15 were
lease/deadline expiries (``LeaseExpired`` at admission, ``RELEASED_BEFORE_H:DEADLINE``, ``not_admitted_deadline``) and 8
were ``GAP_EXCEEDED`` after ``PressCompleted``. 92% fell inside clip-ops windows (median CPU 90%). The fixed 1500 ms
lease and 1.0 s admission window had to cover ACQUIRE hand-off, publication, native admission and the whole hold.

This tracker keeps the last few measured samples of two motor facts and sizes the next press from their maximum:

- ``ack``: ACQUIRE written -> owned lease visible to the runtime (both IPC legs plus the native poll);
- ``overhead``: caller-observed admission -> release interval of a completed press, minus its nominal hold.

Unloaded samples reproduce the old 1500 ms / 1.0 s / 2.0 s values. Everything stays bounded by the native
``MaxLeaseDurationMs`` (4000 ms); STOP, release proof and the stuck-input hard stop are untouched.
"""

from __future__ import annotations

import math
from collections import deque

DEFAULT_LEASE_MS = 1500  # NativeActionOwner.LeaseDurationMs
MAX_LEASE_MS = 4000  # NativeActionOwner.MaxLeaseDurationMs
DEFAULT_ADMISSION_S = 1.0
DEFAULT_RESULT_WAIT_S = 2.0
TICK_S = 1.0 / 60.0
MARGIN_S = 0.25
WINDOW = 8
MAX_SAMPLE_S = 10.0
# Evidence: the lease-owned, release-proven command interval may extend the caller's before->after gap by at most this.
MAX_COMMAND_EXTENSION_S = 8.0


class MovePacing:
    def __init__(self) -> None:
        self._ack: deque[float] = deque(maxlen=WINDOW)
        self._overhead: deque[float] = deque(maxlen=WINDOW)

    @staticmethod
    def _sample(seconds: object) -> float | None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            return None
        return min(MAX_SAMPLE_S, max(0.0, float(seconds)))

    def observe_ack(self, seconds: object) -> None:
        value = self._sample(seconds)
        if value is not None:
            self._ack.append(value)

    def observe_press(self, ticks: object, seconds: object) -> None:
        value = self._sample(seconds)
        if value is None or isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1:
            return
        self._overhead.append(max(0.0, value - ticks * TICK_S))

    def lease_ms(self, ticks: int = 60) -> int:
        """Lease long enough for hand-off + admission + the hold, from the worst recent measurement."""
        if not self._ack and not self._overhead:
            return DEFAULT_LEASE_MS
        need = max(self._ack, default=0.0) + max(self._overhead, default=0.0) + max(1, ticks) * TICK_S + MARGIN_S
        return max(DEFAULT_LEASE_MS, min(MAX_LEASE_MS, math.ceil(need * 1000)))

    @staticmethod
    def admission_s(lease_ms: int, remaining_s: float) -> float:
        """Request deadline (admission and hold): the old 1.0 s at the default lease, never past the live lease."""
        cap = max(DEFAULT_ADMISSION_S, min(MAX_LEASE_MS, lease_ms) / 1000.0 - 0.5)
        return max(0.001, min(cap, remaining_s))

    @staticmethod
    def acquire_s(lease_ms: int) -> float:
        """fix55C / ADR 78 (REVIEW2 #4): the ACQUIRE hand-off wait follows the requested lease (1.0 s at the default lease)."""
        return max(DEFAULT_ADMISSION_S, min(MAX_LEASE_MS, lease_ms) / 1000.0 - 0.5)

    @staticmethod
    def result_wait_s(admission_s: float) -> float:
        return max(DEFAULT_RESULT_WAIT_S, admission_s + 1.0)
