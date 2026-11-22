"""ADR 200: authorize turn/start atomically, without holding privacy locks during pipe I/O.

The installed synchronous SDK combines dispatch and wait in Thread.run (and even turn waits for an ack).
Its client writes JSON-RPC through _write_message. Wrap that instance seam once; thread-local gates apply
only to the opted-in call, including every retry, and leave other roles and control messages unchanged.
An unsupported transport fails closed instead of silently losing the last-byte check.
"""
import threading
import time
import weakref
from contextlib import contextmanager

_install_lock = threading.Lock()
DEFAULT_WRITE_TIMEOUT_SEC = 1.0
_barriers_lock = threading.Lock()
_barriers = weakref.WeakSet()


class SendSeamMissing(RuntimeError):
    """The SDK cannot enforce append dispatch privacy at its pipe seam."""


def _kill(proc):
    if proc is not None:
        try:
            proc.kill()  # bypass buffered stdin.close(), which can wait behind a blocked writer
        except Exception:
            pass


class _WriteBarrier:
    """One transport's message bytes; a boundary takes this LAST, never the SDK lock."""
    def __init__(self):
        self.lock = threading.Lock()
        self.token = None
        with _barriers_lock:
            _barriers.add(self)

    def begin(self, token):
        if not self.lock.acquire(timeout=max(0.0, token.deadline - time.monotonic())):
            raise TimeoutError("Codex turn/start write deadline")
        self.token = token

    def drain(self):
        token = self.token
        deadline = token.deadline if token is not None else time.monotonic()
        if self.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return True
        # Deadline expiry fails closed even if the writer is suspended before stdin.write.
        token = self.token
        if token is not None:
            token.cancelled.set()
            _kill(token.proc)
        return self.lock.acquire(blocking=False)


@contextmanager
def write_boundary(*, receipt=None):
    """Order mutations against THIS message's write/flush, never its ack/model wait.

    Gate locks are already held. Registry locking ends before waiting; all barriers use one order.
    At the write deadline, kill the captured pipe process before allowing the mutation to finish.
    A suspended writer also checks cancellation before entering the original SDK write.
    """
    with _barriers_lock:
        barriers = sorted(_barriers, key=id)
    acquired = []
    try:
        for barrier in barriers:
            token = barrier.token
            if receipt is not None and (token is None or not any(item is receipt for item in token.receipts)):
                continue
            if barrier.drain():
                acquired.append(barrier)
        yield
    finally:
        for barrier in reversed(acquired):
            barrier.lock.release()


class _DispatchToken:
    """Authorization transfers to a per-client byte-write barrier, not a 'started' assertion."""
    def __init__(self, gate, proc, barrier, timeout, receipts):
        self.gate, self.proc, self.barrier = gate, proc, barrier
        self.receipts = tuple(receipts)
        self.deadline = time.monotonic() + max(0.001, float(timeout))
        self.cancelled = threading.Event()
        self._owns_write = False

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("Codex write cancelled before dispatch")

    def start(self):
        with self.gate():
            self.check()
            self.barrier.begin(self)
            self._owns_write = True
        # Only the small byte-write barrier survives into pipe I/O, not privacy/session/receipt locks.

    def finish(self):
        if self._owns_write:
            self._owns_write = False
            self.barrier.token = None
            self.barrier.lock.release()


class _WriteLock:
    """Keep the SDK's original writer; authorize only after its serialization lock is acquired."""
    def __init__(self, lock, local):
        self.lock, self.local = lock, local

    def __enter__(self):
        self.lock.acquire()
        token = getattr(self.local, "token", None)
        try:
            if token is not None:
                token.start()
                token.check()
        except BaseException:
            if token is not None:
                token.finish()
            self.lock.release()
            raise
        return self

    def __exit__(self, *exc):
        token = getattr(self.local, "token", None)
        if token is not None:
            token.finish()
        self.lock.release()


def _bounded_write(transport, write, payload, gate, timeout, local, sdk_lock, receipts):
    with gate():
        token = _DispatchToken(gate, getattr(transport, "_proc", None), transport._arti_write_barrier, timeout, receipts)
    box = {}
    with transport._arti_pending_lock:
        transport._arti_pending_writes.add(token)

    def work():
        local.token = token
        try:
            if not sdk_lock:
                token.start()  # minimal fake/custom transports have no SDK serialization lock
                token.check()
            box["result"] = write(payload)
        except BaseException as exc:
            box["error"] = exc
        finally:
            token.finish()
            local.token = None
            with transport._arti_pending_lock:
                transport._arti_pending_writes.discard(token)

    worker = threading.Thread(target=work, daemon=True, name="codex-context-write")
    worker.start()
    worker.join(timeout=max(0.0, token.deadline - time.monotonic()))
    if worker.is_alive():
        token.cancelled.set()
        _kill(token.proc)
        raise TimeoutError("Codex turn/start write deadline")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def close_with_deadline(client, timeout=0.25):
    """Kill outstanding writers before SDK close, and bound close even if its own cleanup stalls."""
    transport = getattr(client, "_client", None)
    if transport is None:
        owner = getattr(getattr(client, "close", None), "__self__", None)
        transport = getattr(owner, "_client", None)
    proc = getattr(transport, "_proc", None)
    pending_lock = getattr(transport, "_arti_pending_lock", None)
    if pending_lock is not None:
        with pending_lock:
            pending = tuple(transport._arti_pending_writes)
        for token in pending:
            token.cancelled.set()
            _kill(token.proc)

    def close():
        try:
            client.close()
        except Exception:
            pass

    worker = threading.Thread(target=close, daemon=True, name="codex-context-close")
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        _kill(proc)


def run_with_send_gate(thread, text, gate, *, write_timeout_sec=DEFAULT_WRITE_TIMEOUT_SEC, write_receipts=(),
                       on_turn=None, **kwargs):
    """`on_turn(handle)` (V13) receives the started turn's handle, so a slow or cancelled turn can be interrupted
    and the thread kept; `Thread.run` is `turn()` plus `handle.run()`. Without it, or on a thread without `turn`,
    the call is exactly `thread.run`."""
    transport = getattr(thread, "_client", None)
    if transport is None or not callable(getattr(transport, "_write_message", None)):
        raise SendSeamMissing("append context requires the Codex turn/start send seam")
    with _install_lock:
        local = getattr(transport, "_arti_dispatch_local", None)
        if local is None:
            local = threading.local()
            transport._arti_write_barrier = _WriteBarrier()
            write = transport._write_message
            sdk_lock = getattr(transport, "_lock", None)
            if sdk_lock is not None:
                transport._lock = _WriteLock(sdk_lock, local)
            transport._arti_pending_lock = threading.Lock()
            transport._arti_pending_writes = set()

            def guarded_write(payload):
                active = getattr(local, "gate", None)
                if active is not None and payload.get("method") == "turn/start":
                    return _bounded_write(transport, write, payload, active, local.timeout, local, sdk_lock, local.receipts)
                return write(payload)

            transport._write_message = guarded_write
            transport._arti_dispatch_local = local
    previous = getattr(local, "gate", None)
    previous_timeout = getattr(local, "timeout", None)
    previous_receipts = getattr(local, "receipts", ())
    local.gate = gate
    local.timeout = write_timeout_sec
    local.receipts = tuple(write_receipts)
    try:
        if on_turn is not None and callable(getattr(thread, "turn", None)):
            handle = thread.turn(text, **kwargs)
            on_turn(handle)
            return handle.run()
        return thread.run(text, **kwargs)
    finally:
        local.gate = previous
        local.timeout = previous_timeout
        local.receipts = previous_receipts
