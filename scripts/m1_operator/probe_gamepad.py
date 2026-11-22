"""probe_gamepad.py: TEST-ONLY operator side of the virtual-gamepad probes (a probe branch, #278; PLAN §4 PRB-n).

  probe_gamepad.py send ID OPS [--timeout S] [--allow-new-ops]   OPS = inline JSON list or a .json file with a list
  probe_gamepad.py tail [N]                                       print the last N probe-log lines (compact)
  probe_gamepad.py validate FILE                                  check a step script / ops list against the op schema
  probe_gamepad.py steps FILE                                     print a step script (operator steps + runs)
  probe_gamepad.py run-step FILE RUN_ID [--timeout S] [--allow-new-ops]   send one run of a step script
  probe_gamepad.py collect ID [--since HH:MM] [--out DIR]         copy the run's probe-log lines + SMAPI lines
  probe_gamepad.py template N [--out DIR]                         write a PROBE_V2_PRB<N>.md receipt template

The mod side is GamepadProbe.cs (ModConfig.GamepadProbeEnabled, default OFF; `restart_farmhand.py --deploy <probe DLL>
--preset v2-probe`). This tool writes RUN/probe-gamepad/trigger.json and reads the probe log + the SMAPI log: no port,
no inbox, no input. Step scripts live in probe_ops/ (PRB-1/2/3/4/8/10). Ops the current probe DLL does not implement
(`needs_ops`, e.g. PRB-1's oldPadState write, a G4 probe op) are refused unless --allow-new-ops (CT deployed a probe
build that has them). Standing constraint (PLAN §6.2): never press the right stick or the triggers; validate refuses
them. send / run-step refuse while a trial runner runs (exit 2).
Exit: 0 run_end | 1 fault | 2 refused / invalid | 3 timeout (trigger not consumed or no run_end).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m1_paths as paths  # noqa: E402

DIR = paths.RUN / "probe-gamepad"
LOG = DIR / "probe-log.jsonl"
OPS_DIR = Path(__file__).resolve().parent / "probe_ops"

# GamepadProbe.cs (StepOp / Menu / Setup / SetOptions), integration head 5a95178
PRESS_BUTTONS = {"ControllerA", "ControllerB", "ControllerX", "ControllerY", "ControllerStart", "ControllerBack",
                 "LeftThumbstickUp", "LeftThumbstickDown", "LeftThumbstickLeft", "LeftThumbstickRight",
                 "DPadUp", "DPadDown", "DPadLeft", "DPadRight", "LeftShoulder", "RightShoulder"}
FORBIDDEN_BUTTONS = {"RightThumbstickUp", "RightThumbstickDown", "RightThumbstickLeft", "RightThumbstickRight",
                     "RightStick", "LeftStick", "LeftTrigger", "RightTrigger"}           # PLAN §6.2
MENU_BUTTONS = {"A", "B", "X", "Y", "Start", "Back", "DPadUp", "DPadDown", "DPadLeft", "DPadRight", "LeftShoulder",
                "RightShoulder"}
MENU_ACTIONS = {"populate_snap_default", "snap_default", "move", "pad", "click_snapped", "close"}
KNOWN_OPS = {"setup", "restore", "snapshot", "warp_block", "options", "menu", "wait", "press"}
GAMEPAD_MODES = {"Auto", "ForceOn", "ForceOff"}


def lines():
    if not LOG.exists():
        return []
    return LOG.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------- schema (pure)
def _is_int(v, lo=None):
    return isinstance(v, int) and not isinstance(v, bool) and (lo is None or v >= lo)


def check_op(op, allow_new=frozenset()):
    """Problems with one op ([] = valid for the current probe DLL)."""
    if not isinstance(op, dict) or not isinstance(op.get("op"), str):
        return [f"not an op object: {op!r}"]
    kind, bad = op["op"], []
    if kind not in KNOWN_OPS:
        return [] if kind in allow_new else [f"op {kind!r} is not in the current probe DLL (needs a newer probe build)"]
    if kind == "setup":
        bad += [f"setup.{k} must be a bool" for k in ("force_on", "snappy") if k in op and not isinstance(op[k], bool)]
    elif kind == "warp_block" and "on" in op and not isinstance(op["on"], bool):
        bad.append("warp_block.on must be a bool")
    elif kind == "snapshot" and "label" in op and not isinstance(op["label"], str):
        bad.append("snapshot.label must be a string")
    elif kind == "wait" and not _is_int(op.get("ticks"), 1):
        bad.append("wait.ticks must be an int >= 1")
    elif kind == "press":
        btns = op.get("buttons")
        if not isinstance(btns, list) or not btns:
            bad.append("press.buttons must be a non-empty list")
        else:
            for b in btns:
                if b in FORBIDDEN_BUTTONS:
                    bad.append(f"press {b}: never press the right stick or the triggers (PLAN §6.2)")
                elif b not in PRESS_BUTTONS:
                    bad.append(f"press {b!r} is not a pad SButton the probe may press")
        if not _is_int(op.get("ticks", 1), 1) or op.get("ticks", 1) > 3600:
            bad.append("press.ticks must be an int 1..3600")
        if "sample_every" in op and not _is_int(op["sample_every"], 0):
            bad.append("press.sample_every must be an int >= 0")
    elif kind == "options":
        for k in ("gamepadControls", "snappyMenus"):
            if k in op and not isinstance(op[k], bool):
                bad.append(f"options.{k} must be a bool")
        if "gamepadMode" in op and op["gamepadMode"] not in GAMEPAD_MODES:
            bad.append(f"options.gamepadMode must be one of {sorted(GAMEPAD_MODES)}")
        if "timerUntilMouseFade" in op and not _is_int(op["timerUntilMouseFade"], 0):
            bad.append("options.timerUntilMouseFade must be an int >= 0")
    elif kind == "menu":
        act = op.get("action")
        if act not in MENU_ACTIONS:
            bad.append(f"menu.action must be one of {sorted(MENU_ACTIONS)}")
        if act == "move" and op.get("dir") not in (0, 1, 2, 3):
            bad.append("menu move needs dir 0..3 (up, right, down, left)")
        if act == "pad" and op.get("button") not in MENU_BUTTONS:
            bad.append(f"menu pad needs button in {sorted(MENU_BUTTONS)}")
    return bad


def check_ops(ops, allow_new=frozenset()):
    if not isinstance(ops, list) or not ops:
        return ["ops must be a non-empty list"]
    out = []
    for i, op in enumerate(ops):
        out += [f"op {i}: {p}" for p in check_op(op, allow_new)]
    return out


def load_script(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return dict(probe=Path(path).stem, runs=[dict(id=Path(path).stem, ops=data)], needs_ops=[])
    return data


def validate_script(script, allow_new_ops=False):
    """{runnable, problems, needs_ops} for a step script."""
    problems = []
    for k in ("probe", "runs"):
        if k not in script:
            problems.append(f"missing {k!r}")
    needs = set(script.get("needs_ops") or [])
    ids = [r.get("id") for r in script.get("runs") or []]
    if len(ids) != len(set(ids)):
        problems.append("duplicate run ids")
    for r in script.get("runs") or []:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(r.get("id"))):
            problems.append(f"run id {r.get('id')!r} must be [A-Za-z0-9_.-]{{1,40}}")
        # declared new ops are not schema problems; they only make the script not runnable on the current DLL
        problems += [f"run {r.get('id')}: {p}" for p in check_ops(r.get("ops"), frozenset(needs))]
    runs = script.get("runs") or []
    if any(isinstance(op, dict) and op.get("op") == "setup" for r in runs for op in r.get("ops") or []):
        last = (runs[-1].get("ops") or [{}])[-1] if runs[-1].get("ops") else {}
        if last.get("op") != "restore":
            problems.append("the script sets the probe up but its last run does not end with restore")
    used_new = sorted({op.get("op") for r in script.get("runs") or [] for op in r.get("ops") or []
                       if isinstance(op, dict) and op.get("op") not in KNOWN_OPS})
    undeclared = [o for o in used_new if o not in needs]
    if undeclared:
        problems.append(f"ops {undeclared} are neither in the current probe DLL nor declared in needs_ops")
    runnable = not problems and (allow_new_ops or not used_new)
    return dict(probe=script.get("probe"), runnable=runnable, problems=problems, needs_ops=sorted(needs),
                needs_readback=script.get("needs_readback") or [], runs=ids,
                note=None if runnable or problems else f"needs a probe build with {used_new} (--allow-new-ops once deployed)")


# ---------------------------------------------------------------- live
def _trial_running():
    import win_facts as wf
    return wf.trial_running()


def _send_ops(run_id, ops, timeout, trial_check=None):
    running = (trial_check or _trial_running)()
    if running is not False:
        print("REFUSED: a trial runner is running (or the process list is unknown): the probe shares the farmhand",
              file=sys.stderr)
        return 2
    DIR.mkdir(parents=True, exist_ok=True)
    start = len(lines())
    tmp = DIR / "trigger.tmp"
    tmp.write_text(json.dumps({"id": run_id, "ops": ops}), encoding="utf-8")
    os.replace(tmp, DIR / "trigger.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = lines()[start:]
        for ln in new:
            ev = json.loads(ln)
            if ev["ev"] in ("run_end", "fault") and ev["data"].get("run") == run_id:
                for x in new:
                    print(x)
                return 0 if ev["ev"] == "run_end" else 1
        time.sleep(0.25)
    print(json.dumps({"timeout": timeout, "trigger_consumed": not (DIR / "trigger.json").exists()}))
    return 3


def send(a):
    src = Path(a.ops)
    ops = json.loads(src.read_text(encoding="utf-8")) if src.suffix == ".json" and src.is_file() else json.loads(a.ops)
    if isinstance(ops, dict):
        print("REFUSED: that is a step script; use `run-step FILE RUN_ID`", file=sys.stderr)
        return 2
    used_new = {op.get("op") for op in ops if isinstance(op, dict)} - KNOWN_OPS
    problems = check_ops(ops, frozenset(used_new) if a.allow_new_ops else frozenset())
    if problems:
        print("INVALID " + "; ".join(problems), file=sys.stderr)
        return 2
    return _send_ops(a.id, ops, a.timeout)


def run_step(a):
    script = load_script(a.file)
    v = validate_script(script, a.allow_new_ops)
    if not v["runnable"]:
        print("REFUSED " + json.dumps(v), file=sys.stderr)
        return 2
    run = next((r for r in script["runs"] if r["id"] == a.run_id), None)
    if run is None:
        print(f"REFUSED: no run {a.run_id!r} in {a.file} (runs: {v['runs']})", file=sys.stderr)
        return 2
    return _send_ops(run["id"], run["ops"], a.timeout or float(run.get("timeout", 60)))


def tail(a):
    for ln in lines()[-a.n:]:
        print(ln)
    return 0


def cmd_validate(a):
    v = validate_script(load_script(a.file), a.allow_new_ops)
    print(json.dumps(v, indent=1))
    return 0 if not v["problems"] else 2


def cmd_steps(a):
    s = load_script(a.file)
    v = validate_script(s)
    print(f"{s.get('probe')}: {s.get('title', '')}")
    for k in ("decides", "dll", "pass"):
        if s.get(k):
            print(f"  {k}: {s[k]}")
    for i, st in enumerate(s.get("preconditions") or [], 1):
        print(f"  pre {i}. {st}")
    for i, st in enumerate(s.get("operator_steps") or [], 1):
        print(f"  step {i}. {st}")
    for r in s.get("runs") or []:
        print(f"  run {r['id']}: {r.get('purpose', '')} ({len(r.get('ops') or [])} ops)")
    print("  runnable with the current probe DLL: " + ("yes" if v["runnable"] else f"no {v['note'] or v['problems']}"))
    return 0


SMAPI_TS = re.compile(r"^\[(\d\d:\d\d):\d\d ")


def collect_lines(probe_lines, smapi_text, run_id, since=None):
    """(probe-log rows of run_id, SMAPI lines: GAMEPAD_PROBE of that run + PAD_MODE/PAD_PANIC since HH:MM)."""
    rows = []
    for ln in probe_lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if (ev.get("data") or {}).get("run") == run_id:
            rows.append(ev)
    sm = []
    for ln in (smapi_text or "").splitlines():
        if "GAMEPAD_PROBE" in ln and f'"run":"{run_id}"' in ln.replace(" ", ""):
            sm.append(ln)
        elif since and any(t in ln for t in paths.PAD_LOG_TAGS if t != "GAMEPAD_PROBE"):
            m = SMAPI_TS.match(ln)
            if m and m.group(1) >= since:
                sm.append(ln)
    return rows, sm


def cmd_collect(a):
    smapi = paths.SMAPI_LOG.read_text(encoding="utf-8", errors="replace") if paths.SMAPI_LOG.exists() else ""
    rows, sm = collect_lines(lines(), smapi, a.id, a.since)
    out = Path(a.out) if a.out else DIR / f"collect-{a.id}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "probe-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (out / "smapi-lines.txt").write_text("".join(x + "\n" for x in sm), encoding="utf-8")
    consumed = DIR / f"consumed-{a.id}.json"
    if consumed.exists():
        (out / consumed.name).write_text(consumed.read_text(encoding="utf-8"), encoding="utf-8")
    evs = [r["ev"] for r in rows]
    summary = dict(id=a.id, probe_rows=len(rows), smapi_lines=len(sm), run_begin="run_begin" in evs,
                   run_end="run_end" in evs, faults=evs.count("fault"), out=str(out))
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary))
    return 0 if rows else 3


TEMPLATE = """# PROBE_V2_PRB{n} — <title> (<date>)

| Item | Value |
|---|---|
| Probe | PRB-{n} (PLAN §4): <decides> |
| Step script | `scripts/m1_operator/probe_ops/prb{n}.json` (`probe_gamepad.py steps` / `validate`) |
| DLL | <probe DLL dll8 + commit; deploy record> ; restored to <production dll8> afterwards |
| Preconditions | trial_running False; HOST pid <pid> in front; farmhand inactive; <staging, STAGING_<n>.md if any> |
| pad_watch | `pad_watch.py watch --out RUN/probe-gamepad/prb{n}-pad-watch.jsonl` exit <code> |

Evidence labels per row: VERIFIED LIVE / INCONCLUSIVE / NOT RUN (never upgrade from the decomp alone).

## Runs

| run id | what | result | numbers (probe-log / collect-<id>/summary.json) |
|---|---|---|---|
| <id> | <purpose> | <label> | <before -> after> |

## Operator interventions

- <every fg / cursor move / click, from ui-op-log.jsonl>

## Verdict

<PASS / FAIL against the PLAN §4 pass column; what the owning WS does next>
"""


def cmd_template(a):
    out = Path(a.out) if a.out else DIR
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"PROBE_V2_PRB{a.n}.md"
    if dest.exists():
        print(f"REFUSED: {dest} exists (never overwritten)", file=sys.stderr)
        return 2
    dest.write_text(TEMPLATE.format(n=a.n), encoding="utf-8")
    print(str(dest))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="probe_gamepad.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send"); s.add_argument("id"); s.add_argument("ops"); s.add_argument("--timeout", type=float, default=30)
    s.add_argument("--allow-new-ops", action="store_true"); s.set_defaults(fn=send)
    t = sub.add_parser("tail"); t.add_argument("n", nargs="?", type=int, default=20); t.set_defaults(fn=tail)
    v = sub.add_parser("validate"); v.add_argument("file"); v.add_argument("--allow-new-ops", action="store_true")
    v.set_defaults(fn=cmd_validate)
    st = sub.add_parser("steps"); st.add_argument("file"); st.set_defaults(fn=cmd_steps)
    r = sub.add_parser("run-step"); r.add_argument("file"); r.add_argument("run_id")
    r.add_argument("--timeout", type=float); r.add_argument("--allow-new-ops", action="store_true"); r.set_defaults(fn=run_step)
    c = sub.add_parser("collect"); c.add_argument("id"); c.add_argument("--since", help="HH:MM: also PAD_MODE / PAD_PANIC lines")
    c.add_argument("--out"); c.set_defaults(fn=cmd_collect)
    tp = sub.add_parser("template"); tp.add_argument("n", type=int); tp.add_argument("--out"); tp.set_defaults(fn=cmd_template)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
