"""host_console.py: rehearsal-only background HOST actions through the HOST supervisor's console pipe.

The HOST supervisor (run/host-template-*/host_supervisor.py, copied to RUN/batch-host by `witness_run.py prepare-run`)
feeds every RECOVERY/host-console/*.pending file to the HOST SMAPI's stdin, renames it to *.sent and logs it to
RECOVERY/evidence/HOST_CONSOLE_LOG.jsonl. No window focus, no SendInput, no cursor: an elevated foreground window
(rehearsal 12: OmApSvcBroker) cannot block it. The farmhand's supervisor (prep_trial `_supervise`) has no such pipe.

  status [--recovery DIR]     read-only: flag, HOST identity, unsent files, last console rows (writes nothing)
  sleep  [--recovery DIR]     HOST `debug sleep`: the native Sleep_Yes route (bed flags + ReadyCheck "sleep"), the
                              same line as the earlier HOST-console runs (V2 witness W11/W11B/W12A/W12B/W13)
  load SAVE [--saves-dir D]   HOST `arti_host load SAVE` at the title screen (mod command, Phase 2): the native
                              host-slot load. SAVE must fully match SAVE_RE and D/SAVE/SAVE must exist.
  ok     [--recovery DIR]     HOST `arti_host ok`: native LevelUpMenu OK / ShippingMenu OK (never the profession
                              chooser). The mod refuses unless the HOST config has RehearsalHostConsoleEnabled=true.

Rules (operator-staging/DESIGN_HOST_BACKGROUND_INPUT_2026-10-22.md):
- OFF by default: every action refuses unless ARTI_REHEARSAL_HOST_BG=1. Rehearsals only; never in the real stream
  (the owner plays HOST by hand).
- HOST-only allowlist (ACTIONS): exact console lines. The only argument is `load`'s save folder name, which must
  fully match SAVE_RE (letters/digits, "_", digits); no other free text reaches the console.
- HOST identity: RUN/host-hwnd.txt is a live window whose pid == RECOVERY/host-launch.json pid, the title has
  "stardew valley" and no "[farmhand]", hwnd != RUN/fh-hwnd.txt, pid != RUN/farmhand-launch.json pid, and RECOVERY
  has no host-exit.json. `sleep`/`ok` need "[main player]" (world loaded); `load` needs the title screen (no
  "[main player]").
- One command in flight (refuse while any *.pending is unsent); each action refuses within COOLDOWN_S[action] of
  the last identical line this HOST process received (from the supervisor's own log).
- Every attempt (sent, undelivered, refused) appends ONE RUN/ui-op-log.jsonl row: op=host_console, source=host_driver,
  target=host, host_hwnd (the FIXB17 HOST-driver tag), background=true, plus the foreground hwnd at that moment.
- Delivered = the supervisor renamed the file to *.sent. Delivery is not a world effect: read it back with a HOST
  `ui_op.py shot` ("Waiting for other players") or the farmhand journal (bed wait ends).
- An undelivered file is never withdrawn (renaming it under the supervisor would crash its loop and close SMAPI's
  stdin); the next call refuses until the operator has looked.
Exit: 0 delivered / status ok, 2 refused (nothing written to host-console), 3 not delivered within DELIVER_WAIT_S.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m1_paths as paths  # noqa: E402
import win_facts as wf  # noqa: E402

FLAG_ENV = "ARTI_REHEARSAL_HOST_BG"
ACTIONS = {"sleep": "debug sleep",           # action -> exact console line; nothing else is ever written
           "ok": "arti_host ok",
           "load": "arti_host load {save}"}   # {save} only after SAVE_RE.fullmatch
SAVE_RE = re.compile(r"[A-Za-z0-9]{1,40}_[0-9]{1,20}", re.ASCII)  # = HostRehearsalConsole.IsSaveFolderName (C#)
SLEEP_COOLDOWN_S = 300.0
COOLDOWN_S = {"sleep": SLEEP_COOLDOWN_S, "load": 120.0, "ok": 3.0}
DELIVER_WAIT_S = 6.0                         # the supervisor polls every 0.25 s
POLL_S = 0.25


class Refused(Exception):
    """A precondition failed: nothing was written to host-console."""


class Live:
    def window(self, h):
        return wf.window(h)

    def foreground(self):
        try:
            return wf.foreground()
        except Exception:  # noqa: BLE001 - diagnostic field only
            return None

    def clock(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)


def _text(p: Path):
    try:
        return p.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def flag_on(env=None) -> bool:
    return (os.environ if env is None else env).get(FLAG_ENV, "").strip() == "1"


def host_identity(recovery: Path, run_dir: Path, ops, world: bool = True) -> dict:
    """The HOST window this recovery folder launched, or Refused. Never accepts the farmhand.
    world=True: a loaded world ("[main player]"); world=False: the title screen (no role marker yet)."""
    launch = _json(recovery / "host-launch.json")
    pid = launch.get("pid") if isinstance(launch, dict) else None
    if not isinstance(pid, int):
        raise Refused(f"{recovery / 'host-launch.json'} missing or has no pid: not a launched HOST recovery folder")
    if (recovery / "host-exit.json").exists():
        raise Refused("host-exit.json exists: this HOST supervisor has exited")
    if not (recovery / "host-console").is_dir():
        raise Refused(f"{recovery / 'host-console'} missing: not a host_supervisor recovery folder")
    raw = _text(run_dir / "host-hwnd.txt")
    if not raw or not raw.isdigit():
        raise Refused(f"{run_dir / 'host-hwnd.txt'} missing or not an hwnd")
    h = int(raw)
    w = ops.window(h) or {}
    title = (w.get("title") or "").casefold()
    fh_hwnd = _text(run_dir / "fh-hwnd.txt")
    fh_launch = _json(run_dir / "farmhand-launch.json")
    fh_pid = fh_launch.get("pid") if isinstance(fh_launch, dict) else None
    if not w.get("exists"):
        raise Refused(f"HOST window {h} does not exist")
    if "[farmhand]" in title or (fh_hwnd and fh_hwnd == raw) or (fh_pid is not None and fh_pid == w.get("pid")):
        raise Refused(f"window {h} is the farmhand: HOST-only tool")
    if w.get("pid") != pid:
        raise Refused(f"HOST window {h} pid {w.get('pid')} != host-launch.json pid {pid}")
    if "stardew valley" not in title:
        raise Refused(f"HOST window {h} is not Stardew Valley ({w.get('title')!r})")
    if world and "[main player]" not in title:
        raise Refused(f"HOST window {h} has no loaded world ([main player] missing from {w.get('title')!r})")
    if not world and "[main player]" in title:
        raise Refused(f"HOST window {h} already has a loaded world ([main player]): load is title-screen only")
    return dict(hwnd=h, pid=pid, title=w.get("title"))


def console_rows(recovery: Path) -> list:
    rows = []
    try:
        lines = (recovery / "evidence" / "HOST_CONSOLE_LOG.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def _epoch(utc):
    try:
        t = datetime.datetime.fromisoformat(str(utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)).timestamp()


def last_command_age(recovery: Path, pid: int, now: float, command: str):
    ages = [now - e for r in console_rows(recovery)
            if r.get("pid") == pid and str(r.get("command") or "").lstrip("﻿").strip() == command
            and (e := _epoch(r.get("utc"))) is not None]
    return min(ages) if ages else None


def last_sleep_age(recovery: Path, pid: int, now: float):
    return last_command_age(recovery, pid, now, ACTIONS["sleep"])


def default_saves_dir(env=None) -> Path:
    appdata = (os.environ if env is None else env).get("APPDATA") or ""
    return Path(appdata) / "StardewValley" / "Saves"


def console_line(action: str, save=None) -> str:
    """The exact console line for an allowlisted action, or Refused. `save` is accepted only for `load`."""
    if action == "load":
        if not isinstance(save, str) or not SAVE_RE.fullmatch(save):
            raise Refused(f"invalid save folder name {save!r}: must fully match {SAVE_RE.pattern}")
        return ACTIONS["load"].format(save=save)
    if save is not None:
        raise Refused(f"{action} takes no save folder")
    return ACTIONS[action]


def log_row(run_dir: Path, ev: dict, host_hwnd):
    row = dict(utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), op="host_console", background=True, **ev,
               source="host_driver", target="host", host_hwnd=host_hwnd)
    log = run_dir / "ui-op-log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def send(action: str, recovery: Path, run_dir: Path, ops, env=None, save=None, saves_dir=None) -> tuple:
    command = None if action == "load" else ACTIONS[action]   # load: only a validated line is ever logged/written
    host = None
    try:
        if not flag_on(env):
            raise Refused(f"{FLAG_ENV}=1 is not set: background HOST actions are rehearsal-only and off by default")
        command = console_line(action, save)
        host = host_identity(recovery, run_dir, ops, world=action != "load")
        console = recovery / "host-console"
        unsent = sorted(p.name for p in console.glob("*.pending"))
        if unsent:
            raise Refused(f"unsent HOST console file(s) {unsent}: supervisor stuck or dead; look before sending more")
        cooldown = COOLDOWN_S[action]
        age = last_command_age(recovery, host["pid"], ops.clock(), command)
        if age is not None and age < cooldown:
            raise Refused(f"`{command}` already sent {age:.0f} s ago to this HOST (cooldown {cooldown:.0f} s)")
        if action == "load":
            saves = Path(saves_dir) if saves_dir is not None else default_saves_dir(env)
            if not (saves / save / save).is_file():
                raise Refused(f"save {save!r} not found: {saves / save / save} is not a file")
        name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ops.clock())) + "-" + action
        pending, sent = console / (name + ".pending"), console / (name + ".sent")
        if pending.exists() or sent.exists():
            raise Refused(f"{name} already exists: one {action} per second at most")
    except Refused as e:
        row = log_row(run_dir, dict(action=action, command=command, save=save, ok=False, refused=str(e),
                                    hwnd=host and host["hwnd"], fg_hwnd=ops.foreground()), host and host["hwnd"])
        return row, 2
    tmp = console / (name + ".tmp")                    # the supervisor only globs *.pending: publish atomically
    tmp.write_text(command, encoding="utf-8")
    os.replace(tmp, pending)
    end = ops.clock() + DELIVER_WAIT_S
    while not sent.exists() and ops.clock() < end:
        ops.sleep(POLL_S)
    delivered = sent.exists()
    row = log_row(run_dir, dict(action=action, command=command, save=save, ok=delivered, delivered=delivered,
                                hwnd=host["hwnd"], pid=host["pid"], file=str(sent if delivered else pending),
                                recovery=str(recovery), fg_hwnd=ops.foreground(),
                                note=None if delivered else "not delivered: file left in place, never withdrawn"),
                  host["hwnd"])
    return row, 0 if delivered else 3


def status(recovery: Path, run_dir: Path, ops, env=None) -> tuple:
    out = dict(ok=True, flag=flag_on(env), flag_env=FLAG_ENV, actions=sorted(ACTIONS), recovery=str(recovery))
    try:
        out["host"], out["host_at_title"] = host_identity(recovery, run_dir, ops), False
    except Refused as e:
        try:                                            # a HOST at the title screen (before `load`)
            out["host"], out["host_at_title"] = host_identity(recovery, run_dir, ops, world=False), True
        except Refused:
            out["host"], out["host_refused"] = None, str(e)
    console = recovery / "host-console"
    out["unsent"] = sorted(p.name for p in console.glob("*.pending")) if console.is_dir() else None
    out["last_console_rows"] = console_rows(recovery)[-3:]
    return out, 0


def build_parser():
    ap = argparse.ArgumentParser(prog="host_console.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", *sorted(ACTIONS)])
    ap.add_argument("save", nargs="?", default=None, help="load only: the HOST save folder name (SAVE_RE)")
    ap.add_argument("--saves-dir", type=Path, default=None, help="load only: saves folder (default %%APPDATA%%)")
    ap.add_argument("--recovery", type=Path, default=None, help="HOST recovery folder (default RUN/batch-host)")
    ap.add_argument("--run-dir", type=Path, default=None, help="run folder (default ARTI_M1_RUN)")
    return ap


def main(argv=None, ops=None, env=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.save is not None and a.cmd != "load":
        ap.error(f"{a.cmd} takes no argument")
    run_dir = a.run_dir or paths.RUN
    recovery = a.recovery or run_dir / "batch-host"
    ops = ops or Live()
    if a.cmd == "status":
        out, rc = status(recovery, run_dir, ops, env)
    else:
        out, rc = send(a.cmd, recovery, run_dir, ops, env, save=a.save, saves_dir=a.saves_dir)
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
