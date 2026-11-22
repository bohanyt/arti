"""Small, independent worker pools for the bridge's blocking call sites.

Cancellation cannot stop a running Python thread. Keep its slot occupied until
the real work finishes; callers waiting for a slot remain cancellable on the
event loop instead of building an unbounded executor queue.
"""

import asyncio
import contextvars
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# Nasib per panggilan (arti_provider_health), dibaca dari DALAM thread pekerja
# lewat current_call_outcome() — konteks disalin ke thread saat submit:
#   None        = normal (hasil dihitung apa adanya)
#   "cancelled" = dibatalkan pengguna (barge-in / PTT / toggle) -> bukan hasil
#   "abandoned" = penunggu menyerah tanpa aksi pengguna, mis. asyncio.wait_for
#                 habis waktu -> dihitung TIMEOUT (provider lambat)
# Cara membedakan (paling sederhana yang benar): pembatalan dianggap milik
# pengguna kalau CancelledError membawa pesan BARGE_IN, atau terjadi <=
# USER_CANCEL_WINDOW_SEC setelah note_user_cancel() (dipanggil handler toggle
# PTT mouse/keyboard; jalur pasif membatalkan <=0,1 dtk setelah toggle).
# wait_for tidak pernah memberi pesan dan tidak memanggil note_user_cancel.
BARGE_IN = "arti_barge_in"
USER_CANCEL_WINDOW_SEC = 1.5
_last_user_cancel = float("-inf")
_call_state: contextvars.ContextVar = contextvars.ContextVar(
    "arti_executor_call_state", default=None)


def note_user_cancel() -> None:
    """Handler PTT/toggle memanggil ini SEBELUM membatalkan giliran."""
    global _last_user_cancel
    _last_user_cancel = time.monotonic()


def _user_cancelled(exc: BaseException) -> bool:
    if BARGE_IN in getattr(exc, "args", ()):
        return True
    return time.monotonic() - _last_user_cancel <= USER_CANCEL_WINDOW_SEC


def current_call_outcome() -> str | None:
    state = _call_state.get()
    return state.get("outcome") if state is not None else None


def current_cancellation_probe():
    """Capture this call's shared cancellation state for nested SDK worker threads."""
    state = _call_state.get()
    return lambda: state is not None and state.get("outcome") in ("cancelled", "abandoned")


def current_call_cancelled() -> bool:
    """True bila panggilan executor ini dibatalkan PENGGUNA (bukan timeout)."""
    return current_call_outcome() == "cancelled"


class WorkPool:
    def __init__(self, name: str, workers: int):
        self._executor = ThreadPoolExecutor(workers, thread_name_prefix=f"arti-{name}")
        self._slots = threading.BoundedSemaphore(workers)
        self._workers = workers
        self._waiting = 0
        self._waiting_lock = threading.Lock()

    def stats(self) -> dict:
        """Slots held (incl. abandoned calls still running) and callers waiting."""
        free = getattr(self._slots, "_value", None)
        busy = self._workers - free if isinstance(free, int) else -1
        return {"busy": busy, "queued": self._waiting, "workers": self._workers}

    def _note_waiting(self, delta: int) -> None:
        with self._waiting_lock:
            self._waiting += delta

    async def run(self, function, *args, **kwargs):
        if not self._slots.acquire(blocking=False):
            self._note_waiting(1)
            try:
                while not self._slots.acquire(blocking=False):
                    await asyncio.sleep(0.01)
            finally:
                self._note_waiting(-1)
        try:
            call = functools.partial(function, *args, **kwargs)
            ctx = contextvars.copy_context()
            nasib: dict = {"outcome": None}
            ctx.run(_call_state.set, nasib)
            future = self._executor.submit(ctx.run, call)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError as exc:
            # Thread tetap jalan sampai provider menjawab; tandai nasibnya
            # supaya arti_provider_health tahu cara menghitung hasilnya.
            nasib["outcome"] = "cancelled" if _user_cancelled(exc) else "abandoned"
            raise

    def shutdown(self):
        self._executor.shutdown(wait=True, cancel_futures=True)


_POOLS = {name: WorkPool(name, workers) for name, workers in (
    # LLM calls get their own pool: a reply abandoned by the turn deadline keeps
    # its thread until the provider returns, and must not starve TTS/playback.
    ("voice", 4), ("llm", 6), ("rag", 2), ("vision", 2), ("background", 2),
)}


def pool_stats() -> dict:
    """{pool: {busy, queued, workers}} for the health line (audit L06)."""
    return {name: pool.stats() for name, pool in list(_POOLS.items())}


async def run(pool: str, function, *args, **kwargs):
    return await _POOLS[pool].run(function, *args, **kwargs)
