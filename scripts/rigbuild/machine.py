"""Machine admission and the shared pytest slot, as the hand procedures ran them (ADR 245).

`snapshot()` is the Python form of the evidence folders' `gates.ps1`: memory commit, C: free, a running game or OBS,
another pytest. `admit()` is `run.py`'s `gate()`; `Slot` is its `pytest_slot()`. The two commands keep their own
receipt's numbers (`TEST_BUILD` = testbuild140, `PACKAGE` = pkg112); ADR 245 lists where they differ.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import time


class Refused(RuntimeError):
    """A procedure precondition failed; nothing after it ran."""


@dataclass(frozen=True)
class Policy:
    name: str
    disk_floor_gib: float
    # testbuild136-140: below the floor, re-read every 5 minutes up to six times, then refuse.
    # pkg112: the floor is one more admission condition inside the ordinary wait.
    disk_rechecks: int | None
    disk_recheck_s: int
    wait_s: int
    pytest_wait_s: int
    slot_wait_s: int
    # pkg112 asserts before every dotnet/pytest step that the HOLD is still its own; testbuild140 does not.
    assert_slot_owner: bool
    commit_ceiling_pct: float = 75.0
    timeout_s: int = 10800
    pytest_timeout_s: int = 5400
    slot_timeout_s: int = 5400


TEST_BUILD = Policy("test-build (testbuild140)", 15.0, 6, 300, 7 * 45, 4 * 45, 4 * 45, False)
PACKAGE = Policy("package (pkg112)", 15.5, None, 0, 300, 120, 120, True)

GAME_OR_OBS = {"stardewmoddingapi.exe": "StardewModdingAPI", "stardew valley.exe": "Stardew Valley",
               "obs64.exe": "obs64"}
PYTHONS = {"python.exe", "pythonw.exe", "pytest.exe"}
OTHER_PYTEST = re.compile(r"pytest|exec\(eval\(sys\.stdin\.readline\(\)\)\)")


def census():
    """[(pid, name, command line)] of every process (one CIM read)."""
    from scripts.free_play import soak
    rows = json.loads(soak.ps("Get-CimInstance Win32_Process -ErrorAction Stop | "
                              "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress") or "[]")
    rows = [rows] if isinstance(rows, dict) else rows
    if not rows:
        raise RuntimeError("process census unavailable")
    return [(int(r.get("ProcessId") or 0), (r.get("Name") or "").casefold(), r.get("CommandLine") or "") for r in rows]


def commit_percent():
    from scripts.free_play import bench_gates
    return bench_gates.commit_percent()


def clock():
    """The clock.ps1 pair: UTC and local time with offset, ISO 8601."""
    return dict(utc=datetime.now(timezone.utc).isoformat(), local=datetime.now().astimezone().isoformat())


def snapshot(cfg, *, processes=census, commit=commit_percent, usage=shutil.disk_usage, own_pid=None, now=clock):
    """One admission row, the fields of gates.ps1. Our own process never counts as another pytest."""
    own = os.getpid() if own_pid is None else own_pid
    procs = processes()
    drive = Path(cfg.rig_root).anchor or "C:\\"
    row = dict(now())
    row.update(commit_pct=commit(), c_free_gib=usage(drive).free / 1024**3,
               game_or_obs=sorted({GAME_OR_OBS[n] for _, n, _ in procs if n in GAME_OR_OBS}),
               other_pytest=sorted(f"{pid} {n}" for pid, n, c in procs
                                   if pid != own and n in PYTHONS and OTHER_PYTEST.search(c)))
    if cfg.housekeeping_hold is not None:
        row["housekeeping_hold"] = Path(cfg.housekeeping_hold).exists()
    return row


def admit(policy, label, *, read, pytest=False, identity=lambda: None, record=lambda row: None,
          sleep=time.sleep, mono=time.monotonic, say=print):
    """Wait until the machine admits a step; refuse after the receipt's bound. Returns the admitting row."""
    start, rechecks = mono(), 0
    while True:
        row = dict(read(), label=label)
        record(row)
        identity()
        if policy.disk_rechecks is not None and row["c_free_gib"] < policy.disk_floor_gib:
            if rechecks == policy.disk_rechecks:
                raise Refused(f"C: below {policy.disk_floor_gib} GiB after {rechecks} rechecks: " + json.dumps(row))
            rechecks += 1
            say(f"WAIT C: free below {policy.disk_floor_gib} GiB " + json.dumps(row))
            sleep(policy.disk_recheck_s)
            continue
        disk_ok = policy.disk_rechecks is not None or row["c_free_gib"] >= policy.disk_floor_gib
        foreign = pytest and row["other_pytest"]
        if row["commit_pct"] <= policy.commit_ceiling_pct and disk_ok and not row["game_or_obs"] and not foreign:
            return row
        say("WAIT " + json.dumps(row))
        if mono() - start >= (policy.pytest_timeout_s if pytest else policy.timeout_s):
            raise Refused("Persistent machine admission gate: " + json.dumps(row))
        sleep(policy.pytest_wait_s if pytest else policy.wait_s)


class Slot:
    """The shared pytest slot: create-new HOLD.txt, released only when it still holds our token."""

    def __init__(self, hold, task, *, policy, admit_fn, events, timeout_s=None, sleep=time.sleep,
                 mono=time.monotonic, now=clock, pid=None, say=print):
        self.hold, self.task, self.policy, self.admit_fn = Path(hold), task, policy, admit_fn
        self.events, self.sleep, self.mono, self.now, self.say = Path(events), sleep, mono, now, say
        self.timeout_s = policy.slot_timeout_s if timeout_s is None else timeout_s
        self.pid = os.getpid() if pid is None else pid
        self.token = None

    def _event(self, row):
        with self.events.open("a", encoding="utf8") as f:
            f.write(json.dumps(row) + "\n")

    def owned(self):
        try:
            return json.loads(self.hold.read_text(encoding="utf8")).get("pid") == self.pid
        except (OSError, ValueError, AttributeError):
            return False

    @contextmanager
    def held(self, name, n):
        start, token = self.mono(), None
        self.hold.parent.mkdir(parents=True, exist_ok=True)
        while True:
            self.admit_fn(name + "-slot-admission", True)
            if not self.hold.exists():
                token = json.dumps(dict(task=self.task, name=name, pid=self.pid, start=self.now()["local"],
                                        expected_minutes=40 if n else 3))
                try:
                    with self.hold.open("x", encoding="utf8") as f:
                        f.write(token)
                    break
                except FileExistsError:
                    pass
            self.say("WAIT pytest slot: " + name)
            if self.mono() - start >= self.timeout_s:
                raise Refused(f"pytest slot held by another worker for {self.timeout_s // 60} minutes: {self.hold}")
            self.sleep(self.policy.slot_wait_s)
        self.token = token
        self._event(dict(action="acquired", token=token))
        try:
            yield token
        finally:
            self.token = None
            if self.hold.exists() and self.hold.read_text(encoding="utf8") == token:
                self.hold.unlink()
                self._event(dict(action="released", token=token, utc=self.now()["utc"]))
