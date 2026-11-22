"""Periodic off-loop file writes, with a blocking final flush on clean close.

A failed batch never poisons the writer (long-session audit L01): the batch is
dropped and counted, the error is kept only until the next successful batch,
and ``write()`` never re-raises it. One ENOSPC during an 8-10 h stream used to
make every later transcript write raise, which stopped triggers from queuing.
The writer thread never prints (it may be the debug log's own sink); callers
read ``write_health()`` and log at their own pace.
"""

import atexit
import os
import threading
import time


class BatchedWriter:
    def __init__(self, stream, *, fsync=False, interval=1.0, lines=32):
        self._stream = stream
        self._fsync = fsync
        self._interval = min(1.0, max(0.01, float(interval)))
        self._lines = max(1, int(lines))
        self._condition = threading.Condition()
        self._pending = []
        self._pending_lines = 0
        self._deadline = 0.0
        self._force = False
        self._closing = False
        self._flushing = False
        self._error = None  # error of the LAST batch only; cleared by the next successful batch
        self._failed_batches = 0  # total failed batches (monotonic)
        self._dropped_lines = 0  # total lines dropped with failed batches (monotonic)
        self._thread = None
        atexit.register(self.close)

    @property
    def name(self):
        """Underlying file name (tests and log rotation inspect it)."""
        return getattr(self._stream, "name", "")

    def write_health(self):
        """(last_error_or_None, failed_batches, dropped_lines); counters are monotonic."""
        with self._condition:
            return self._error, self._failed_batches, self._dropped_lines

    def write(self, data):
        with self._condition:
            if self._closing:
                raise ValueError("write to closed batched writer")
            if not self._pending:
                self._deadline = time.monotonic() + self._interval
            self._pending.append(data)
            self._pending_lines += data.count("\n")
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="arti-file-writer", daemon=True)
                self._thread.start()
            self._condition.notify_all()
        return len(data)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closing or self._force)
                deadline = self._deadline
                while not (self._closing or self._force or self._pending_lines >= self._lines):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                batch, self._pending = self._pending, []
                batch_lines = self._pending_lines
                self._pending_lines = 0
                self._force = False
                self._flushing = True
            try:
                if batch:
                    self._stream.write("".join(batch))
                self._stream.flush()
                if self._fsync:
                    os.fsync(self._stream.fileno())
            except Exception as exc:
                # Drop the batch (no re-queue: a partially buffered write would
                # duplicate on retry) and try again with the next batch.
                with self._condition:
                    self._error = exc
                    self._failed_batches += 1
                    self._dropped_lines += batch_lines
            else:
                with self._condition:
                    self._error = None
            finally:
                with self._condition:
                    self._flushing = False
                    self._condition.notify_all()
                    if self._closing and not self._pending:
                        return

    def flush(self):
        with self._condition:
            if self._thread is None or self._closing:
                return
            self._force = True
            self._condition.notify_all()
            self._condition.wait_for(lambda: not (self._pending or self._force or self._flushing))
            if self._error:
                raise self._error

    def close(self):
        with self._condition:
            if self._closing:
                return
            self._closing = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join()
        self._stream.close()
        atexit.unregister(self.close)
        if self._error:
            raise self._error
