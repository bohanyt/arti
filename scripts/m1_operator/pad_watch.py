"""pad_watch.py: Primitives v2 pad monitor (native reads only: no port, inbox or input), with one exception:
ADR 192: inside a Live7 cycle, input held after a terminal is first repaired through the existing pad-panic
inbox request (force neutral, verify from native readback); only an unverified neutral stops the session.

Live7 may explicitly supply --violation-stop-file to write a cooperative STOP sentinel.

  pad_watch.py watch [--seconds N] [--interval 1.0] [--sample-hz 50] [--jump-px 400] [--strict-jumps]
                     [--out FILE] [--quiet] [--stop-file FILE] [--exit-on-violation]
  pad_watch.py preflight            v2 trial pre-flight (run_attempt.sh / run_manual.sh / soak.py in pad mode)
  pad_watch.py check FILE           exit code of a recorded watch JSONL (the rows carry their violations)

watch prints one JSON line per interval (default 1 s): farmhand foreground (fh_fg) + the foreground app's exe (never a
title: the owner's own apps stay private), gamepad_mode, pad_mode_owned / restore_pending / pin, the pad override
count (diagnostic) and the outstanding pad Press count, cursor_hidden (+ reason), os_warp_calls / blocked / unblocked_inactive, the OS cursor position with the largest
single-sample jump and the path length inside the interval (sampled at --sample-hz), and the new PAD_MODE /
GAMEPAD_PROBE / PAD_PANIC SMAPI lines. Field names come from m1_paths.PAD_KEYS (WS-G0); a key the mod does not
publish is reported as null, never as false.

Violations (the exit code is the lowest code seen; 0 = none):
  5 warp_unblocked          os_warp_unblocked_inactive rose above 0: the mod moved the owner's OS cursor (global stop)
  6 pad_held_after_terminal pad_outstanding_count (held Presses only) stayed > 0 for > 2 s after a SMAPI `TERMINAL`
                            or `PAD_PANIC` line. pad_override_count (the whole ledger, Suppress-only entries persist
                            the entire inactive session) is diagnostic only, never a verdict
  7 owned_while_focused     pad_mode_owned stayed true for > 2 s while the farmhand window was in front (the bounded
                            restore is <= 10 ticks, PLAN §1.5 a). Not a fault when session.json says
                            focus_independent: true (ADR 244: pad mode stays hers in front; older DLLs keep the rule)
  8 cursor_jump             (--strict-jumps only) one OS-cursor sample jumped >= --jump-px with the farmhand inactive.
                            Without --strict-jumps a jump is recorded as `suspect` only: the owner's own flicks can be fast.
  9 prefs_force_on          after a SMAPI `Context: after save` line, startup_preferences clientOptions/gamepadMode is
                            ForceOn: the night save captured ARTI's mode (live D-B1b-1; the next join would load it).
                            Checked on that interval and the next one (the farmhand writes clientOptions at save end).
 10 pad_mode_after_release  an idle retired lease still owns/restores/has ForceOn after the grace interval.
 11 pad_mode_cleanup_fault  unresolved restoration is latched; reported immediately, including during a new lease.
preflight exit: 0 ok | 3 refused (reasons printed) | 2 cannot read the hwnd files. Safe during a trial.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, str(Path(HERE).parents[1]))
from arti_stardew_stop import write_sentinel
import m1_paths as paths  # noqa: E402
import win_facts as wf  # noqa: E402
from scripts.m1_operator.farmhand_focus import focus_independent  # noqa: E402  (ADR 250, repo root is on sys.path)

K = paths.PAD_KEYS
TERM_RE = re.compile(r"\] background TERMINAL run=\d+")
ARM_RE = re.compile(r"\] background ARM request_id=(\S+) run=(\d+).*? key=(\S+)")
TURN_RE = re.compile(r"\] background PATH turn run=(\d+).*? key=(\S+)")
TAG_RE = re.compile(r"\b(" + "|".join(map(re.escape, paths.PAD_LOG_TAGS)) + r")\b")
HOLD_GRACE_S = 2.0
FOCUS_GRACE_S = 2.0
JUMP_PX = 400
EXIT = {"warp_unblocked": 5, "pad_held_after_terminal": 6, "owned_while_focused": 7, "cursor_jump": 8, "prefs_force_on": 9,
        "pad_mode_after_release": 10, "pad_mode_cleanup_fault": 11}
AFTER_SAVE_RE = re.compile(r"Context: after save")


def read_startup_prefs_facts():
    """clientOptions facts of startup_preferences (restart_farmhand's parser), or None when unreadable."""
    try:
        import restart_farmhand as rf
        return rf.parse_startup_prefs(ipc.read_bytes(Path(paths.STARTUP_PREFS)))
    except Exception:
        return None


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def pad_facts(session):
    """{short name: value or None} for every PAD_KEYS entry."""
    s = session or {}
    return {k: s.get(v) for k, v in K.items()}


def cursor_stats(samples):
    """samples [(t, x, y)] -> {pos, delta, max_jump, path}."""
    if not samples:
        return dict(pos=None, delta=None, max_jump=0, path=0)
    jumps = [math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(samples, samples[1:])]
    first, last = samples[0], samples[-1]
    return dict(pos=[last[1], last[2]], delta=[last[1] - first[1], last[2] - first[2]],
                max_jump=round(max(jumps), 1) if jumps else 0, path=round(sum(jumps), 1))


class PadWatch:
    """Pure evaluator: step() once per interval with what was read; it keeps the episode state."""

    def __init__(self, strict_jumps=False, jump_px=JUMP_PX, hold_grace=HOLD_GRACE_S, focus_grace=FOCUS_GRACE_S,
                 prefs_reader=read_startup_prefs_facts):
        self.strict, self.jump_px, self.hold_grace, self.focus_grace = strict_jumps, jump_px, hold_grace, focus_grace
        self.prefs_reader = prefs_reader
        self.prefs_checks_due = 0
        self.prefs_reported = False
        self.prev_unblocked = 0
        self.terminal_at = None
        self.held_reported = False
        self.focus_since = None
        self.focus_reported = False
        self.release_since = None
        self.release_reported = False
        self.cleanup_fault_reported = False
        self.violations = []
        self.suspects = 0
        self.last_tags = {}
        self.armed = None

    def step(self, t, fh_fg, session, smapi_lines=(), cursor_samples=(), fg_app=None):
        f = pad_facts(session)
        cur = cursor_stats(list(cursor_samples))
        lines, input_events = [], []
        for line in smapi_lines or ():
            arm = ARM_RE.search(line)
            if arm:
                self.armed = dict(request_id=arm[1], run=int(arm[2]), key=arm[3])
                # ADR 192: a new admitted primitive is not an old terminal's held input.
                # Neutral R/N can occur entirely between the watcher's samples.
                self.terminal_at, self.held_reported = None, False
            turn = TURN_RE.search(line)
            if turn and self.armed and int(turn[1]) == self.armed["run"]:
                self.armed = dict(self.armed, key=turn[2])
            if arm or turn or TERM_RE.search(line):
                input_events.append(line.strip()[:300])
            m = TAG_RE.search(line)
            if m:
                self.last_tags[m.group(1)] = line.strip()[:300]
                lines.append(line.strip()[:300])
            if TERM_RE.search(line) or "PAD_PANIC" in line:
                self.terminal_at, self.held_reported = t, False
            if AFTER_SAVE_RE.search(line):
                self.prefs_checks_due, self.prefs_reported = 2, False
        out = []
        if f["cleanup_fault"] not in (None, "") and not self.cleanup_fault_reported:
            out.append(dict(kind="pad_mode_cleanup_fault", reason=f["cleanup_fault"]))
            self.cleanup_fault_reported = True
        if self.prefs_checks_due > 0 and self.prefs_reader is not None:
            self.prefs_checks_due -= 1
            facts = self.prefs_reader()
            if (facts or {}).get("client_gamepadMode") == "ForceOn" and not self.prefs_reported:
                out.append(dict(kind="prefs_force_on", client_gamepadMode="ForceOn",
                                client_gamepadControls=(facts or {}).get("client_gamepadControls")))
                self.prefs_reported = True
        unb = num(f["warp_unblocked"])
        if unb is not None and unb > self.prev_unblocked:
            out.append(dict(kind="warp_unblocked", value=unb, previous=self.prev_unblocked))
            self.prev_unblocked = unb
        held = num(f["outstanding"])
        # FIXB46: zero outstanding buttons did not prove W3's terminal mode restore.
        # A live lease may keep ForceOn across requests; an idle retired lease may not.
        s = session or {}
        released = (s.get("owner_id") in (None, "") and s.get("release_acked") is True
                    and s.get("release_requested") is False and s.get("input_busy") is False
                    and s.get("inflight_request_id") is None)
        leaking_mode = f["owned"] is True or f["restore_pending"] is True or f["gamepad_mode"] == "ForceOn"
        if released and leaking_mode:
            if self.release_since is None:
                self.release_since, self.release_reported = t, False
            if t - self.release_since > self.hold_grace and not self.release_reported:
                out.append(dict(kind="pad_mode_after_release", gamepad_mode=f["gamepad_mode"], owned=f["owned"],
                                seconds=round(t - self.release_since, 1)))
                self.release_reported = True
        else:
            self.release_since = None
        if self.terminal_at is not None and held is not None:
            if held == 0:
                self.terminal_at = None
            elif t - self.terminal_at > self.hold_grace and not self.held_reported:
                out.append(dict(kind="pad_held_after_terminal", outstanding=held, overrides=f["overrides"],
                                seconds=round(t - self.terminal_at, 1), held=self.armed))
                self.held_reported = True
        # ADR 244: a DLL whose hands do not depend on focus keeps pad mode owned in front by design.
        if fh_fg and f["owned"] is True and not focus_independent(s):  # ADR 250: the weaker per-path value
            if self.focus_since is None:
                self.focus_since, self.focus_reported = t, False
            if t - self.focus_since > self.focus_grace and not self.focus_reported:
                out.append(dict(kind="owned_while_focused", seconds=round(t - self.focus_since, 1)))
                self.focus_reported = True
        else:
            self.focus_since = None
        jump = None
        if cur["max_jump"] >= self.jump_px and not fh_fg:
            jump = dict(px=cur["max_jump"], warp_calls=f["warp_calls"], warp_blocked=f["warp_blocked"])
            if self.strict:
                out.append(dict(kind="cursor_jump", **jump))
            else:
                self.suspects += 1
        self.violations += out
        return dict(t=round(t, 3), utc=wf.utc(t), fh_fg=bool(fh_fg), fg_exe=(fg_app or {}).get("exe"),
                    session=session is not None, **f, cursor=cur, cursor_jump_suspect=jump if not self.strict else None,
                    pad_lines=lines, violations=out, armed=self.armed, input_events=input_events)

    def exit_code(self):
        return min((EXIT[v["kind"]] for v in self.violations), default=0)

    def summary(self):
        kinds = {}
        for v in self.violations:
            kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1
        return dict(exit=self.exit_code(), violations=kinds, cursor_jump_suspects=self.suspects, last_lines=self.last_tags)


def summarize_rows(rows):
    """Exit code + counts from recorded rows (their `violations` lists)."""
    kinds = {}
    for r in rows:
        for v in r.get("violations") or ():
            if v["kind"] == "pad_held_after_terminal" and (r.get("recovery") or {}).get("status") == "verified":
                continue
            kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1
    return dict(exit=min((EXIT[k] for k in kinds if k in EXIT), default=0), violations=kinds,
                rows=len(rows), suspects=sum(1 for r in rows if r.get("cursor_jump_suspect")))


def preflight(session, fh_fg, config):
    """v2 trial pre-flight (pure). config = installed mod config.json (None = unreadable)."""
    s, reasons = session or {}, []
    if session is None:
        reasons.append("no session.json (farmhand not joined, or the inbox is off)")
    elif s.get("world_ready") is not True:
        reasons.append(f"session.json world_ready={s.get('world_ready')!r}")
    if config is None:
        reasons.append("installed mod config.json unreadable")
    else:
        if config.get(paths.PAD_FLAG) is not True:
            reasons.append(f"mod config has no {paths.PAD_FLAG}=true (deploy with --preset v2-pad)")
        if config.get(paths.DRAW_FLAG) is not True:
            reasons.append(f"mod config has no {paths.DRAW_FLAG}=true: the cursor-hidden gate needs the Draw path")
    if session is not None and K["owned"] not in s:
        reasons.append(f"session.json has no {K['owned']}: the deployed DLL does not publish the WS-G0 pad fields")
    unb = num(s.get(K["warp_unblocked"]))
    if unb:
        reasons.append(f"{K['warp_unblocked']}={unb}: an unblocked OS-cursor warp happened (global stop, PLAN §6.2)")
    if s.get(K["restore_pending"]) is True:
        reasons.append(f"{K['restore_pending']} is true: wait for the restore")
    if s.get(K["cleanup_fault"]) not in (None, ""):
        reasons.append(f"pad mode cleanup fault latched: {s[K['cleanup_fault']]}")
    if fh_fg:
        reasons.append("the farmhand window is in front: v2 trials run with the farmhand inactive (the owner keeps the HOST)")
    if s.get("input_busy") is True or s.get("inflight_request_id"):
        reasons.append(f"input busy (input_busy={s.get('input_busy')!r}, inflight={s.get('inflight_request_id')!r})")
    if (s.get("owner_id") in (None, "") and s.get("release_acked") is True
            and (s.get(K["owned"]) is True or s.get(K["gamepad_mode"]) == "ForceOn")):
        reasons.append("pad mode not restored after lease release (UNCERTAIN CLEANUP: stop)")
    return dict(ok=not reasons, reasons=reasons, facts=pad_facts(session),
                note="cursor_hidden is record-only here: before the first admission the mod gate itself refuses "
                     "CURSOR_NOT_HIDDEN")


# ---------------------------------------------------------------- live sources (never used by the offline tests)
def _read_text(p):
    # PM-1 fix7 #8 (attempt 7 N:1079/11519/17756, session.json write denied x3): this watcher polls session.json
    # during play; a plain open lacks FILE_SHARE_DELETE and refuses the mod's POSIX replace. Share-delete read.
    from scripts.free_play.file_io import _read_text as shared
    return shared(p)


def _read_json(p, retries=5):
    for _ in range(retries):
        try:
            return json.loads(_read_text(p))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.05)
    return None


def _read_int(p):
    try:
        return int(_read_text(p).strip())
    except (OSError, ValueError):
        return None


class LiveSource:
    def __init__(self):
        self.smapi = wf.FileTail(paths.SMAPI_LOG)
        self.smapi.start()
        self.partial = ""

    def now(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, s):
        time.sleep(s)

    def fh_hwnd(self):
        return _read_int(paths.FH_HWND_FILE)

    def session(self):
        return _read_json(paths.COMMAND_DIR / "session.json")

    def smapi_lines(self):
        text = self.partial + self.smapi.read_new()
        done, _, self.partial = text.rpartition("\n")
        return done.splitlines() if done else []

    def cursor(self):
        return wf.cursor_pos()

    def fg_app(self):
        return wf.foreground_app()

    def exists(self, p):
        return Path(p).exists()

    def mod_config(self):
        return paths.read_mod_config()

    def force_neutral(self, session):
        from pad_panic import panic_request
        ipc.write_json(paths.COMMAND_DIR / paths.PAD_PANIC_FILE, panic_request(session, self.now()))

    def neutral_request_consumed(self):
        return not (paths.COMMAND_DIR / paths.PAD_PANIC_FILE).exists()


def recover_held(a, src, session, violation):
    from arti_runtime import pad_recovery as recovery
    return recovery.recover(Path(a.cycle_id) / recovery.FILE, src, session, violation)


def watch(a, src=None):
    src = src or LiveSource()
    w = PadWatch(strict_jumps=a.strict_jumps, jump_px=a.jump_px)
    out = Path(a.out) if a.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
    t_end = src.now() + a.seconds if a.seconds else None
    step_s = 1.0 / max(1.0, a.sample_hz)
    try:
        while True:
            t0, samples = src.now(), []
            while src.now() - t0 < a.interval:
                p = src.cursor()
                if p:
                    samples.append((src.now(), p[0], p[1]))
                src.sleep(step_s)
            fg = src.fg_app() or {}
            fh = src.fh_hwnd()
            session = src.session()
            row = w.step(src.now(), bool(fh) and fg.get("hwnd") == fh, session, src.smapi_lines(), samples, fg)
            recovered = False
            if (getattr(a, "cycle_id", None) and len(row["violations"]) == 1
                    and row["violations"][0]["kind"] == "pad_held_after_terminal"):
                row["recovery"] = recover_held(a, src, session, row["violations"][0])
                recovered = row["recovery"]["status"] == "verified"
                if recovered:
                    w.terminal_at = None
                    w.violations.remove(row["violations"][0])
            line = json.dumps(row, ensure_ascii=False, default=str)
            if out:
                ipc.append_text(out, line + "\n")
            if not a.quiet or row["violations"]:
                print(line, flush=True)
            if row["violations"] and not recovered and getattr(a, "violation_stop_file", None):
                stop = Path(a.violation_stop_file)
                write_sentinel(stop, dict(session or {}, cycle_id=getattr(a, "cycle_id", None)),
                               writer="pad_watch.violation")
            if a.exit_on_violation and row["violations"] and not recovered:
                break
            if a.stop_file and src.exists(a.stop_file):
                break
            if t_end is not None and src.now() >= t_end:
                break
    except KeyboardInterrupt:
        pass
    summary = w.summary()
    print("PAD_WATCH " + json.dumps(summary, ensure_ascii=False, default=str), flush=True)
    return summary["exit"]


def cmd_preflight(_a, src=None):
    src = src or LiveSource()
    fh = src.fh_hwnd()
    if not fh:
        print(f"PAD_PREFLIGHT REFUSED no hwnd in {paths.FH_HWND_FILE.name}")
        return 2
    fg = src.fg_app() or {}
    r = preflight(src.session(), fg.get("hwnd") == fh, src.mod_config())
    print(("PAD_PREFLIGHT OK " if r["ok"] else "PAD_PREFLIGHT REFUSED ") + json.dumps(r, ensure_ascii=False, default=str))
    return 0 if r["ok"] else 3


def cmd_check(a):
    rows = []
    for line in ipc.read_text(Path(a.file), encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    s = summarize_rows(rows)
    print(json.dumps(s))
    return s["exit"]


def build_parser():
    ap = argparse.ArgumentParser(prog="pad_watch.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    w = sub.add_parser("watch", help="monitor (one JSON line per interval)")
    w.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds (0 = until Ctrl+C / --stop-file)")
    w.add_argument("--interval", type=float, default=1.0)
    w.add_argument("--sample-hz", type=float, default=50.0, help="OS cursor sampling rate inside an interval")
    w.add_argument("--jump-px", type=float, default=JUMP_PX, help="single-sample cursor jump treated as a warp suspect")
    w.add_argument("--strict-jumps", action="store_true", help="a cursor jump is a violation (owner idle only)")
    w.add_argument("--out", help="append the JSON lines to FILE")
    w.add_argument("--quiet", action="store_true", help="print only rows with violations and the summary")
    w.add_argument("--stop-file", help="stop when FILE exists (soak.py uses this)")
    w.add_argument("--exit-on-violation", action="store_true")
    w.add_argument("--violation-stop-file", type=Path, help="create FILE on a violation (Live7 cross-process STOP)")
    w.add_argument("--cycle-id", help="unique Live7 cycle folder targeted by a violation STOP")
    w.set_defaults(f=watch)
    p = sub.add_parser("preflight", help="v2 trial pre-flight from session.json + the installed mod config")
    p.set_defaults(f=cmd_preflight)
    c = sub.add_parser("check", help="exit code of a recorded watch JSONL")
    c.add_argument("file")
    c.set_defaults(f=cmd_check)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "watch" and (a.interval <= 0 or a.seconds < 0):
        print("--interval must be > 0 and --seconds >= 0", file=sys.stderr)
        return 2
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
