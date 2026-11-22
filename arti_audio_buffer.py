"""Bounded mic buffers; no device or ASR ownership."""

import queue

import numpy as np


class UtteranceBuffer:
    def __init__(self, samplerate, max_seconds):
        self.max_samples = max(1, int(samplerate * max_seconds))
        self.chunks = []
        self.samples = 0

    @property
    def full(self):
        return self.samples >= self.max_samples

    def append(self, chunk):
        take = min(len(chunk), self.max_samples - self.samples)
        if take > 0:
            self.chunks.append(np.asarray(chunk[:take], dtype=np.float32).copy())
            self.samples += take

    def as_array(self):
        return np.concatenate(self.chunks) if self.chunks else np.empty(0, dtype=np.float32)

    def clear(self):
        self.chunks.clear()
        self.samples = 0


class AudioChunkQueue(queue.Queue):
    def __init__(self, max_chunks):
        super().__init__(maxsize=max(1, int(max_chunks)))
        self.dropped = 0
        self._reported_drops = 0

    def put_latest(self, chunk):
        # The callback never waits for space; the ASR consumer may be busy.
        with self.not_empty:
            if self._qsize() >= self.maxsize:
                self._get()
                self.unfinished_tasks -= 1
                self.dropped += 1
            self._put(chunk)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def report_drops(self):
        # Called by the consumer, never by PortAudio's callback thread.
        with self.mutex:
            dropped = self.dropped
        if dropped != self._reported_drops:
            self._reported_drops = dropped
            print(f"[ASR WARNING] Audio queue penuh: drop_oldest={dropped} chunks")
