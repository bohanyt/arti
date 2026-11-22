"""Content-free C: free-space witnesses; never deletes or diagnoses other files."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading
import time

GIB = 1024 ** 3


class DiskRecorder:
    def __init__(self, path, floor_gib=15, *, usage=shutil.disk_usage, clock=time.time):
        self.path, self.floor_gib = Path(path), floor_gib
        self.usage, self.clock = usage, clock
        self.lock, self.halt = threading.Lock(), threading.Event()
        self.thread = None
        self.floor_crossed = False

    def sample(self, phase="sample", *, free_bytes=None):
        with self.lock:
            row = dict(phase=phase, at=self.clock(), free_bytes=self.usage("C:\\").free if free_bytes is None else free_bytes,
                       floor_gib=self.floor_gib)
            self.floor_crossed |= row["free_bytes"] < self.floor_gib * GIB
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            return row

    def start(self):
        self.sample("start")
        def monitor():
            while not self.halt.wait(60):
                try:
                    self.sample()
                except OSError:
                    # Missing samples remain UNKNOWN in the report; never guess disk space.
                    pass
        self.thread = threading.Thread(target=monitor, name="livepath-disk", daemon=True)
        self.thread.start()

    def stop(self):
        self.halt.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.sample("end")


def summarize(rows, floor_gib=15):
    valid = [r for r in rows if isinstance(r.get("free_bytes"), int) and r["free_bytes"] >= 0]
    starts = [r for r in valid if r.get("phase") == "start"]
    ends = [r for r in valid if r.get("phase") == "end"]
    if not valid:
        return dict(status="UNKNOWN", samples=0, min_free_gib=floor_gib,
                    note="OBS is closed in a rehearsal; a disk fall cannot be OBS recording.")
    values = [r["free_bytes"] for r in valid]
    peak = largest_fall = 0
    for value in values:
        peak = max(peak, value)
        largest_fall = max(largest_fall, peak - value)
    crossed = min(values) < floor_gib * GIB
    return dict(status="FLOOR_CROSSED" if crossed else "RECORDED" if starts and ends else "UNKNOWN",
                samples=len(valid), start_free_bytes=starts[0]["free_bytes"] if starts else None,
                end_free_bytes=ends[-1]["free_bytes"] if ends else None,
                lowest_free_bytes=min(values), largest_fall_bytes=largest_fall,
                start_free_gib=round(starts[0]["free_bytes"] / GIB, 3) if starts else None,
                end_free_gib=round(ends[-1]["free_bytes"] / GIB, 3) if ends else None,
                lowest_free_gib=round(min(values) / GIB, 3), largest_fall_gib=round(largest_fall / GIB, 3),
                min_free_gib=floor_gib, floor_crossed=crossed,
                note="OBS is closed in a rehearsal; a disk fall cannot be OBS recording.")
