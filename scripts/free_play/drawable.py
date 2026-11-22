"""Keep only this harness's PID-bound game windows drawable, without foreground input.

SaveGameMenu.update is gated on hasDrawn. A live desktop session and visible,
non-minimized game HWNDs are preconditions, even though multiplayer updates unfocused.
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import json
import sys
import time


class Windows:
    def __init__(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "m1_operator"))
        import win_facts
        self.facts = win_facts.window
        self.exe = win_facts.exe_name
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        from ctypes import wintypes as W
        self.user32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = W.BOOL

    def show(self, hwnd):
        # SW_SHOWNOACTIVATE: restore/show at the original bounds without foregrounding.
        self.user32.ShowWindow(hwnd, 4)


class DrawableWindows:
    # Recover, not stop: a live owned window that is briefly not drawable (hung pump, minimized,
    # hidden) is waited on; only the 10-minute no-progress bound turns it into a stop.
    GRACE_S = 600

    def __init__(self, bindings, *, api=None, record=lambda row: None, clock=time.monotonic, grace_s=GRACE_S):
        self.bindings = tuple(bindings)
        if len(self.bindings) != 2 or len({b["pid"] for b in self.bindings}) != 2:
            raise ValueError("two distinct owned game processes required")
        self.api = api or Windows()
        self.record = record
        self.clock, self.grace_s = clock, grace_s
        self.waiting = {}
        self.started = False

    @classmethod
    def from_runtime(cls, runtime, **kwargs):
        runtime = Path(runtime)
        rows = []
        for role, launch, hwnd in (("host", "batch-host/host-launch.json", "host-hwnd.txt"),
                                   ("farmhand", "farmhand-launch.json", "fh-hwnd.txt")):
            pid = json.loads((runtime / launch).read_text(encoding="utf-8-sig"))["pid"]
            rows.append(dict(role=role, pid=pid, hwnd=int((runtime / hwnd).read_text().strip())))
        return cls(rows, **kwargs)

    def check(self):
        samples = []
        # Validate BOTH identities before making any window change.
        for binding in self.bindings:
            f = self.api.facts(binding["hwnd"])
            if (not f.get("exists") or f.get("pid") != binding["pid"]
                    or self.api.exe(binding["pid"]) != "StardewModdingAPI.exe"):
                raise RuntimeError("owned window identity unavailable: " + binding["role"])
            samples.append((binding, f))
        for binding, f in samples:
            f = self.api.facts(binding["hwnd"])
            if not f.get("exists") or f.get("pid") != binding["pid"]:
                raise RuntimeError("owned window identity changed: " + binding["role"])
            restored = not f.get("visible") or f.get("iconic")
            if restored:
                self.api.show(binding["hwnd"])
                f = self.api.facts(binding["hwnd"])
            if f.get("pid") != binding["pid"]:
                raise RuntimeError("owned window identity changed: " + binding["role"])
            rect = f.get("rect") or ()
            why = [k for k, bad in (("not_visible", not f.get("visible")), ("iconic", f.get("iconic")),
                                    ("hung", f.get("hung")), ("rect", len(rect) != 4 or rect[2] <= rect[0]
                                                            or rect[3] <= rect[1])) if bad]
            role, now = binding["role"], self.clock()
            if why:
                if role not in self.waiting:
                    self.waiting[role] = now
                    self.record(dict(ev="drawable_wait", **binding, reasons=why, visible=bool(f.get("visible")),
                                     iconic=bool(f.get("iconic")), hung=bool(f.get("hung")), rect=f.get("rect"),
                                     restored=restored))
                if now - self.waiting[role] > self.grace_s:
                    raise RuntimeError(f"owned window not drawable: {role} ({','.join(why)}) for "
                                       f"{now - self.waiting[role]:.0f}s")
                continue
            if role in self.waiting:
                self.record(dict(ev="drawable_recovered", **binding, waited_s=round(now - self.waiting.pop(role), 3)))
            if restored or not self.started:
                self.record(dict(ev="drawable_window", **binding, visible=True, minimized=False,
                                 restored=restored, activation_requested=False))
        self.started = True
