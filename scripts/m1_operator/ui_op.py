"""ui_op.py: native UI operator for the M1 background trial (the operator's hands; every input is logged).

  windows [FILTER]              list top-level visible windows (hwnd, pid, title, rect); default filter "Stardew", "*" = all
  info HWND                     read-only facts for one window (exists, pid, title, visible)
  shot HWND [OUT.png]           PrintWindow capture; prints geometry + lumaStd (does not need the window in front)
  fg HWND                       bring HWND to the foreground (Alt-tap trick + SetForegroundWindow), verify
  click HWND CX CY              foreground HWND, move the cursor to CLIENT coords (CX,CY), left click (SendInput)
  move HWND CX CY               cursor move only (parks the in-game cursor; foreground the window first, see README)
  type TEXT... [--hwnd H]       type unicode text (SendInput); with H: foreground H, re-check before every character
  key NAME [HWND]               press a key (enter esc tab backspace space alt w a s d c x e t 0-9); HWND = foreground first
  hold HWND KEY MS              foreground HWND, hold a key for MS milliseconds (operator pre-positioning, never mid-trial)
  pos HWND X Y W H              SetWindowPos (cosmetic; separates overlapping game windows)
  close HWND --expect-pid PID   post WM_CLOSE to HWND only if its process id is PID (operator-launched farmhand only)

Every action is appended to RUN/ui-op-log.jsonl (see m1_paths.py) with a UTC stamp and echoed as one JSON line.
No game logic; coordinates come from screenshots. Clicks and keys need the window in front (the game ignores input
while inactive); `fg` exits 0 even when it fails, so callers check `"ok": true` in its output.
"""
import argparse, ctypes, ctypes.wintypes as W, json, os, sys, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m1_paths as paths  # noqa: E402
import printwindow as pw  # noqa: E402

LOG = paths.UI_LOG
u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.SetForegroundWindow.argtypes = [W.HWND]; u32.SetForegroundWindow.restype = W.BOOL
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]; u32.SetCursorPos.restype = W.BOOL
u32.GetForegroundWindow.restype = W.HWND
u32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
u32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT]
u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM), W.LPARAM]
u32.GetWindowTextLengthW.argtypes = [W.HWND]
u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
u32.IsWindowVisible.argtypes = [W.HWND]
u32.IsWindow.argtypes = [W.HWND]; u32.IsWindow.restype = W.BOOL
u32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
u32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
u32.ClientToScreen.argtypes = [W.HWND, ctypes.POINTER(W.POINT)]
u32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]; u32.PostMessageW.restype = W.BOOL
pw.dpi_aware()  # physical pixels for ClientToScreen/SetCursorPos (same call the live tool made at import)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x0002, 0x0004, 0x8000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
WM_CLOSE = 0x0010
VK = {"enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "space": 0x20, "alt": 0x12,
      "w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44, "c": 0x43, "x": 0x58, "e": 0x45,
      "t": 0x54,  # chat open (Options.chatButton T, Game1.cs:12484): stage_save.py types /debug lines after it
      "f": 0x46,  # journal (Options.journalButton F): the bench stages an open QuestLog with it (ADR 242)
      **{str(n): 0x30 + n for n in range(10)}}  # digits: toolbar slot selection (operator chest placement)
u32.MapVirtualKeyW.argtypes = [W.UINT, W.UINT]; u32.MapVirtualKeyW.restype = W.UINT


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", W.DWORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", _U)]


u32.SendInput.argtypes = [W.UINT, ctypes.POINTER(INPUT), ctypes.c_int]; u32.SendInput.restype = W.UINT


HOST_DRIVER_ENV = "ARTI_UI_OP_HOST_DRIVER"


def host_driver_tag() -> dict:
    """FIXB17 (owner decision: an Opus agent drives the HOST with the mouse during the soak): the HOST-driver tooling runs
    ui_op with ARTI_UI_OP_HOST_DRIVER=<HOST hwnd>; its rows carry source=host_driver, target=host and that hwnd, so the
    soak's farmhand leak check can exclude them (still counted and listed in the receipt). Any untagged row still fails."""
    raw = os.environ.get(HOST_DRIVER_ENV, "").strip()
    return dict(source="host_driver", target="host", host_hwnd=int(raw)) if raw.isdigit() else {}


def fg_now() -> int:
    """FIXB17 review: the real foreground hwnd at the moment an input is sent (logged on every input row)."""
    return int(u32.GetForegroundWindow() or 0)


def log(ev):
    ev = dict(utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **ev, **host_driver_tag())
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    print(json.dumps(ev, ensure_ascii=False))


def send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    n = u32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if n != len(inputs):
        raise OSError(ctypes.get_last_error(), f"SendInput sent {n}/{len(inputs)}")


def mouse(flags, dx=0, dy=0):
    i = INPUT(type=INPUT_MOUSE); i.mi = MOUSEINPUT(dx, dy, 0, flags, 0, None); return i


def kbd(vk=0, scan=0, flags=0):
    i = INPUT(type=INPUT_KEYBOARD); i.ki = KEYBDINPUT(vk, scan, flags, 0, None); return i


def title(h):
    n = u32.GetWindowTextLengthW(h); buf = ctypes.create_unicode_buffer(n + 1); u32.GetWindowTextW(h, buf, n + 1); return buf.value


def window_pid(h):
    pid = W.DWORD(); u32.GetWindowThreadProcessId(h, ctypes.byref(pid)); return pid.value


def cmd_windows(a):
    flt = (a[0] if a else "Stardew").lower()
    rows = []
    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(h, _):
        if u32.IsWindowVisible(h):
            t = title(h)
            if flt in t.lower() or flt == "*":
                pid = W.DWORD(); u32.GetWindowThreadProcessId(h, ctypes.byref(pid)); r = W.RECT(); u32.GetWindowRect(h, ctypes.byref(r))
                rows.append(dict(hwnd=int(h), pid=pid.value, title=t, rect=[r.left, r.top, r.right, r.bottom], foreground=int(u32.GetForegroundWindow() or 0) == int(h)))
        return True
    u32.EnumWindows(cb, 0)
    print(json.dumps(rows, indent=1, ensure_ascii=False))


def cmd_info(a):
    h = int(a[0]); exists = bool(u32.IsWindow(h))
    print(json.dumps(dict(hwnd=h, exists=exists, pid=window_pid(h) if exists else None, title=title(h) if exists else None,
                          visible=exists and bool(u32.IsWindowVisible(h))), ensure_ascii=False))


def cmd_shot(a):
    h = int(a[0]); out = Path(a[1]) if len(a) > 1 else paths.RUN / f"shot-{int(time.time())}-{h}.png"
    r = pw._printwindow(h)
    ok, s, u = pw.client_gate(r["bgra"], r["w"], r["h"], r["client"])
    png, enc = pw.encode_png(r["bgra"], r["w"], r["h"]); out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(png)
    log(dict(op="shot", hwnd=h, out=str(out), w=r["w"], h=r["h"], client=r["client"], rect=r.get("rect"), lumaStd=round(s, 2), uniform=u,
             foreground=int(u32.GetForegroundWindow() or 0) == h))


def cmd_fg(a):
    h = int(a[0])
    for attempt in range(3):
        send(kbd(VK["alt"]), kbd(VK["alt"], flags=KEYEVENTF_KEYUP))  # Alt tap: lets a background process set the foreground
        u32.ShowWindow(h, 9 if u32.IsIconic(h) else 5)  # SW_RESTORE / SW_SHOW
        u32.SetForegroundWindow(h); time.sleep(0.25)
        if int(u32.GetForegroundWindow() or 0) == h:
            log(dict(op="fg", hwnd=h, ok=True, attempt=attempt + 1)); return True
    log(dict(op="fg", hwnd=h, ok=False, foreground=int(u32.GetForegroundWindow() or 0))); return False


def screen_point(h, cx, cy):
    pt = W.POINT(cx, cy); u32.ClientToScreen(h, ctypes.byref(pt)); return pt.x, pt.y


def recovery_input_check():
    """Enforce the worker's gate in the input process, after its window delays."""
    from dataclasses import asdict
    from scripts.livepath import halt_authority as HA
    try:
        return HA.child_recovery_gate(HA.recovery_guard_config())
    except HA.HandsHeld as exc:
        print(json.dumps({"recovery_hold": asdict(exc.decision)}), flush=True)
        raise SystemExit(2) from None
    except HA.RecoveryTimedOut:
        raise SystemExit("recovery_deadline") from None


def cmd_move(a, click=False):
    h, cx, cy = int(a[0]), int(a[1]), int(a[2])
    if click and not cmd_fg([h]):
        raise SystemExit("foreground failed; no click sent")
    sx, sy = screen_point(h, cx, cy)
    fg_start = fg_now()
    u32.SetCursorPos(sx, sy); time.sleep(0.08)
    send(mouse(MOUSEEVENTF_MOVE, 1, 0)); send(mouse(MOUSEEVENTF_MOVE, -1, 0)); time.sleep(0.08)  # relative nudge so the game sees motion
    if click:
        guarded = recovery_input_check()
        fg_start = fg_now()
        if guarded is not None and fg_start != h:
            raise SystemExit("guarded input lost its foreground window")
        send(mouse(MOUSEEVENTF_LEFTDOWN)); time.sleep(0.07); send(mouse(MOUSEEVENTF_LEFTUP))
    fg_end = fg_now()
    log(dict(op="click" if click else "move", hwnd=h, client=[cx, cy], screen=[sx, sy], foreground=fg_start == h and fg_end == h,
             fg_hwnd=fg_start, fg_hwnd_end=fg_end))


def cmd_type(a, hwnd=None):
    """type TEXT [--hwnd H]: with H, foreground H first and confirm GetForegroundWindow() == H before EVERY character;
    abort (exit 3, nothing more typed) the moment it is not (digits typed into the HOST change its toolbar slot)."""
    text = " ".join(a)
    if hwnd is not None and not cmd_fg([hwnd]):
        log(dict(op="type", hwnd=hwnd, chars=0, aborted="foreground failed"))
        raise SystemExit(3)
    fg_first = fg_last = None
    for i, ch in enumerate(text):
        recovery_input_check()
        fg_last = fg_now()  # the one foreground sample per character (also the abort check)
        fg_first = fg_last if fg_first is None else fg_first
        if hwnd is not None and fg_last != hwnd:
            log(dict(op="type", hwnd=hwnd, chars=i, aborted="target lost the foreground", foreground=False))
            raise SystemExit(3)
        code = ord(ch)
        send(kbd(0, code, KEYEVENTF_UNICODE)); send(kbd(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)); time.sleep(0.03)
    log(dict(op="type", chars=len(text), text=text, hwnd=hwnd, foreground=hwnd is not None and fg_first == fg_last == hwnd,
             fg_hwnd=fg_first, fg_hwnd_end=fg_last))


def cmd_key(a):
    """key NAME [HWND]: with HWND, foreground it first; scan code included so XNA keyboard state sees the key."""
    if len(a) > 1 and not cmd_fg([int(a[1])]):
        raise SystemExit("foreground failed; no key sent")
    vk = VK[a[0].lower()]; scan = u32.MapVirtualKeyW(vk, 0)
    fg_start = fg_now()
    send(kbd(vk, scan)); time.sleep(0.08); send(kbd(vk, scan, KEYEVENTF_KEYUP))
    fg_end = fg_now()
    h = int(a[1]) if len(a) > 1 else None
    log(dict(op="key", key=a[0], hwnd=h, foreground=h is not None and fg_start == fg_end == h, fg_hwnd=fg_start,
             fg_hwnd_end=fg_end))


def cmd_hold(a):
    """hold HWND KEY MS: foreground HWND, hold a movement key (w/a/s/d) for MS milliseconds, release. Operator
    pre-positioning only (never during a trial); logged like every other input."""
    h, key, ms = int(a[0]), a[1].lower(), int(a[2])
    if not cmd_fg([h]):
        raise SystemExit("foreground failed; no key sent")
    vk = VK[key]; scan = u32.MapVirtualKeyW(vk, 0)
    fg_start = fg_now()
    send(kbd(vk, scan)); time.sleep(ms / 1000.0); send(kbd(vk, scan, KEYEVENTF_KEYUP))
    fg_end = fg_now()
    log(dict(op="hold", hwnd=h, key=key, ms=ms, foreground=fg_start == fg_end == h, fg_hwnd=fg_start, fg_hwnd_end=fg_end))


def cmd_pos(a):
    h, x, y, w, hh = map(int, a[:5])
    ok = bool(u32.SetWindowPos(h, None, x, y, w, hh, 0x0004 | 0x0010))  # SWP_NOZORDER | SWP_NOACTIVATE
    r = W.RECT(); u32.GetWindowRect(h, ctypes.byref(r))
    log(dict(op="pos", hwnd=h, ok=ok, rect=[r.left, r.top, r.right, r.bottom]))


def cmd_close(a):
    """close HWND PID: WM_CLOSE only when the window belongs to PID (the farmhand this operator launched)."""
    h, want = int(a[0]), int(a[1])
    exists = bool(u32.IsWindow(h)); pid = window_pid(h) if exists else None
    if not exists or pid != want:
        log(dict(op="close", hwnd=h, ok=False, refused=True, exists=exists, pid=pid, expect_pid=want))
        raise SystemExit(f"refused: hwnd {h} exists={exists} pid={pid}, expected pid {want}; no WM_CLOSE sent")
    ok = bool(u32.PostMessageW(h, WM_CLOSE, 0, 0))
    log(dict(op="close", hwnd=h, ok=ok, pid=pid, title=title(h)))
    if not ok:
        raise SystemExit("PostMessageW(WM_CLOSE) failed")


def build_parser():
    ap = argparse.ArgumentParser(prog="ui_op.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    s = sub.add_parser("windows", help="list visible windows"); s.add_argument("filter", nargs="?", default="Stardew")
    s.set_defaults(f=lambda a: cmd_windows([a.filter]))
    s = sub.add_parser("info", help="read-only facts for one window"); s.add_argument("hwnd", type=int)
    s.set_defaults(f=lambda a: cmd_info([a.hwnd]))
    s = sub.add_parser("shot", help="PrintWindow screenshot"); s.add_argument("hwnd", type=int); s.add_argument("out", nargs="?")
    s.set_defaults(f=lambda a: cmd_shot([a.hwnd] + ([a.out] if a.out else [])))
    s = sub.add_parser("fg", help="foreground a window"); s.add_argument("hwnd", type=int)
    s.set_defaults(f=lambda a: cmd_fg([a.hwnd]))
    for name, click in (("click", True), ("move", False)):
        s = sub.add_parser(name, help=("foreground + move + left click" if click else "cursor move only") + " at client coords")
        s.add_argument("hwnd", type=int); s.add_argument("cx", type=int); s.add_argument("cy", type=int)
        s.set_defaults(f=(lambda c: lambda a: cmd_move([a.hwnd, a.cx, a.cy], c))(click))
    s = sub.add_parser("type", help="type unicode text (--hwnd: only while that window is foreground)")
    s.add_argument("text", nargs="+"); s.add_argument("--hwnd", type=int)
    s.set_defaults(f=lambda a: cmd_type(a.text, a.hwnd))
    s = sub.add_parser("key", help="press one key"); s.add_argument("name", type=str.lower, choices=sorted(VK))
    s.add_argument("hwnd", nargs="?", type=int)
    s.set_defaults(f=lambda a: cmd_key([a.name] + ([a.hwnd] if a.hwnd is not None else [])))
    s = sub.add_parser("hold", help="hold a key for MS ms"); s.add_argument("hwnd", type=int)
    s.add_argument("key", type=str.lower, choices=sorted(VK)); s.add_argument("ms", type=int)
    s.set_defaults(f=lambda a: cmd_hold([a.hwnd, a.key, a.ms]))
    s = sub.add_parser("pos", help="SetWindowPos")
    for n in ("hwnd", "x", "y", "w", "h"):
        s.add_argument(n, type=int)
    s.set_defaults(f=lambda a: cmd_pos([a.hwnd, a.x, a.y, a.w, a.h]))
    s = sub.add_parser("close", help="WM_CLOSE a window owned by --expect-pid"); s.add_argument("hwnd", type=int)
    s.add_argument("--expect-pid", type=int, required=True); s.set_defaults(f=lambda a: cmd_close([a.hwnd, a.expect_pid]))
    return ap


def main(argv):
    a = build_parser().parse_args(argv)
    a.f(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
