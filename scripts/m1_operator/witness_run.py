"""witness_run.py: scripted live-witness runner for the Primitives v2 operator (one action per subcommand).

It exists so the live operator makes few manual steps and cannot fall into the recorded procedure traps: a typed or
truncated /debug warp name (use `stage_save.py locations`), a grouped command the approval reviewer rejects (every
subcommand here does ONE thing), a literal "06:00" precondition (wait-ready uses a window), and a reused journal
(trial refuses an existing one). Every subcommand prints ONE short JSON line and exits 0 ok, 2 refused, 3 failed/timeout.

  preflight --row NAME --dll PATH [--sha SHA]    read-only: games, trial running, staging flag on disk, DLL hashes,
                                                 live-checkout HEAD, save date, farmhand inventory slots, fresh journal
  prepare-run --run-dir DIR --template DIR --sha256 SHA --row ROW --snapshot NAME
                                                 offline: validate DLL/config/inbox and stage a fresh HOST recovery folder
  restore NAME                                   stage_save.py restore (refuses while a game runs)
  launch-host --recovery DIR                     FIXB62 step 4: one SMAPI HOST supervisor (DIR/host_supervisor.py,
                                                 the operator's copy), waits for its window, writes run/host-hwnd.txt
  launch-farmhand [--join-slot new|name:NAME]    FIXB62 step 6: restart_farmhand.py --no-close (one process; joins);
                                                 Live7 --join-slot: the slot from the mod's join_screen.json (new = an
                                                 unclaimed cabin, ends in the game's character creation)
  wait-charcreate                                Live7: bounded read-only wait until session.json is world_ready and one
                                                 inspect_context shows CharacterCustomization in context char_custom
  wait-ready --who farmhand --max-hour H         bounded wait until the farmhand is in-world, free, no menu, and
                                                 game time hour <= H (a window, not a literal 06:00)
  trial --goal-file F --owner-url URL --inputs N --seconds S --journal PATH [--minutes M]
                                                 approval sized as given, then one combined trial; the journal must
                                                 be the canonical PATH that does not exist yet (preflight prints it)
  readback --row p13|jellies|haley               independent read of pose, session, newest report, save facts

Nothing here edits a save, deploys a DLL or changes a flag. The game logic stays in prep_trial / stage_save /
restart_farmhand / poll_state; this module only sequences and refuses.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m1_paths as paths  # noqa: E402
import poll_state as PS  # noqa: E402
import prep_trial as P  # noqa: E402
import stage_save as S  # noqa: E402
import win_facts as wf  # noqa: E402

GAME_EXE = "Stardew Valley.exe"
SMAPI_EXE = "StardewModdingAPI.exe"
GAME_IMAGES = (GAME_EXE, SMAPI_EXE)
LIVE_ROOT = paths._ARTI / "live-checkout"            # read-only HEAD check; never edited here
HOST_RESTART = Path(__file__).resolve().parent / "restart_farmhand.py"
STAGING_KEY = "OperatorEventStagingEnabled"       # ModConfig.cs: default false
HOST_CONSOLE_KEY = "RehearsalHostConsoleEnabled"  # ModConfig.cs: default false; HOST template only (arti_host)
READBACK_ROWS = ("p13", "jellies", "haley")
ROW_LOCATION = {"haley": "Beach", "jellies": "Temp", "p13": None}   # expected pose.location per row (None = any)
ROW_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
SNAP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_INPUTS = 280                                  # animal routine ceiling; prep_trial checks the exact per-goal max
SECONDS_RANGE = (60, 900)
LAUNCH_WAIT_S = 90.0
RESTART_TIMEOUT_S = 420.0
READY_TIMEOUT_S = 600.0
READY_STEP_S = 8.0
NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
CREATE_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Refused(Exception):
    """A precondition failed: nothing was started or changed."""


# ---------------------------------------------------------------- argument validation (pure)
def row_name(text: str) -> str:
    if not ROW_RE.fullmatch(text or ""):
        raise argparse.ArgumentTypeError("row must be 1..32 of [a-z0-9_-], starting with a letter or digit")
    return text


def sha_arg(text: str) -> str:
    if not SHA_RE.fullmatch(text or ""):
        raise argparse.ArgumentTypeError("sha must be 7..64 hex characters")
    return text.lower()


def snapshot_name(text: str) -> str:
    if not SNAP_RE.fullmatch(text or ""):
        raise argparse.ArgumentTypeError("snapshot NAME must be 1..64 of [A-Za-z0-9._-], starting with a letter or digit")
    return text


def sha256_arg(text: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text or ""):
        raise argparse.ArgumentTypeError("sha256 must be exactly 64 hex characters")
    return text.lower()


def hour_arg(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("max-hour must be an integer 0..26") from None
    if not 0 <= v <= 26:
        raise argparse.ArgumentTypeError("max-hour must be 0..26 (game clock hour; 26 = 02:00 next morning)")
    return v


def validate_trial(goal_text: str, owner_url: str, inputs: int, seconds: int) -> list:
    """Problems with the trial sizing (pure). prep_trial.cmd_approval still applies the exact per-goal maxima."""
    problems = []
    goal = (goal_text or "").strip()
    if not goal:
        problems.append("goal file is empty")
    elif "\n" in goal or len(goal) > 500:
        problems.append("goal must be one line of at most 500 characters")
    if not isinstance(owner_url, str) or not owner_url.startswith("https://"):
        problems.append("--owner-url must be an https:// URL (the owner's resolution comment)")
    if not isinstance(inputs, int) or not 1 <= inputs <= MAX_INPUTS:
        problems.append(f"--inputs must be 1..{MAX_INPUTS}")
    if not isinstance(seconds, int) or not SECONDS_RANGE[0] <= seconds <= SECONDS_RANGE[1]:
        problems.append(f"--seconds must be {SECONDS_RANGE[0]}..{SECONDS_RANGE[1]}")
    return problems


# ---------------------------------------------------------------- pure helpers
def games_state(pids: dict) -> dict:
    """{image: [pids] | None} -> {"pids": ..., "running": True | False | None (None = some listing failed)}."""
    if any(v is None for v in pids.values()):
        return {"pids": pids, "running": None}
    return {"pids": pids, "running": any(bool(v) for v in pids.values())}


def journal_state(journal_dir: Path) -> dict:
    """The trial journal folder. A used journal has g1_journal.jsonl; any existing folder is not fresh (the trial
    refuses it, and prep_trial would append to it). A fresh path is the one that does not exist."""
    exists = journal_dir.exists()
    used = (journal_dir / "g1_journal.jsonl").exists()
    return {"dir": str(journal_dir), "exists": exists, "used": used, "fresh": not exists,
            "fresh_path": None if exists else str(journal_dir)}


class _ConfigObject(dict):
    """JSON object that also keeps every (key, value) pair in file order, duplicates included."""

    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = list(pairs)


def cmd_prepare_run(a, live=None):
    """Validate and stage a new run folder using file operations only; never launches a game."""
    run_dir, template = Path(a.run_dir), Path(a.template)
    batch_host = run_dir / "batch-host"
    if run_dir.exists() and any(run_dir.iterdir()):
        raise Refused(f"run folder already exists and is non-empty: {run_dir}")
    if not template.is_dir():
        raise Refused(f"HOST template folder is missing: {template}")
    supervisor = template / "host_supervisor.py"
    host_mods = template / "host-mods"
    host_dll = host_mods / "ArtiStardewBridge" / "ArtiStardewBridge.dll"
    host_config = host_mods / "ArtiStardewBridge" / "config.json"
    farmhand_dll = paths.MOD / "ArtiStardewBridge.dll"
    farmhand_config = paths.MOD_CONFIG
    missing = [str(p) for p in (supervisor, host_mods, host_dll, host_config, farmhand_dll, farmhand_config)
               if not p.exists()]
    if missing:
        raise Refused("incomplete HOST template or farmhand installation; missing: " + ", ".join(missing))
    if batch_host.exists():
        raise Refused(f"batch-host already exists in run folder: {batch_host}")

    def config(path):
        try:
            value = json.loads(ipc.read_text(path, encoding="utf-8-sig"), object_pairs_hook=_ConfigObject)
        except (OSError, ValueError) as exc:
            raise Refused(f"cannot read JSON config {path}: {exc}") from None
        if not isinstance(value, dict):
            raise Refused(f"config must be a JSON object: {path}")
        return value

    host_cfg, farmhand_cfg = config(host_config), config(farmhand_config)
    if host_cfg.get(STAGING_KEY) is not False:
        raise Refused(f"HOST config must have {STAGING_KEY}=false")
    if host_cfg.get("MenuStepCommandInboxEnabled") is not False:
        raise Refused("HOST config must have MenuStepCommandInboxEnabled=false")
    if any(key.lower() == HOST_CONSOLE_KEY.lower() and value not in (None, False)
           for key, value in farmhand_cfg.pairs):
        # The rehearsal-only arti_host command is HOST-only: at the title screen a farmhand process is also the
        # "main player", so the flag itself must never be on in the farmhand's config. SMAPI (Newtonsoft) matches
        # config keys case-insensitively and the last duplicate wins, so every top-level pair is checked, any case.
        raise Refused(f"farmhand config must not set {HOST_CONSOLE_KEY} (HOST-only rehearsal console)")

    host_hash = hashlib.sha256(ipc.read_bytes(host_dll)).hexdigest()
    farmhand_hash = hashlib.sha256(ipc.read_bytes(farmhand_dll)).hexdigest()
    expected = a.sha256.lower()
    if host_hash != expected or farmhand_hash != expected:
        raise Refused(f"DLL sha256 mismatch: expected {expected}; HOST={host_hash}; farmhand={farmhand_hash}")
    mismatch = paths.command_dir_mismatch()
    if mismatch:
        raise Refused(mismatch)
    if paths.mod_command_inbox() is None:
        raise Refused(f"cannot verify installed mod CommandInboxDirectory in {farmhand_config}")

    # All preconditions are checked before the first write. Never copy launch/exit records from a template.
    run_dir.mkdir(parents=True, exist_ok=True)
    batch_host.mkdir()
    shutil.copy2(supervisor, batch_host / supervisor.name)
    shutil.copytree(host_mods, batch_host / "host-mods")
    (batch_host / "evidence").mkdir()
    (batch_host / "host-console").mkdir()
    worktree = Path(__file__).resolve().parents[2]
    context = (f'$env:ARTI_M1_RUN = "{run_dir.resolve()}"\n'
               f'$env:ARTI_M1_WORKTREE = "{worktree}"\n')
    (run_dir / "context.ps1").write_text(context, encoding="utf-8")
    py = f'"{paths.PY}"'
    script = '"scripts/m1_operator/witness_run.py"'
    commands = [
        f'& {py} -B {script} preflight --row {a.row} --dll "{farmhand_dll}"',
        f'& {py} -B "scripts/m1_operator/stage_save.py" backup',
        f'& {py} -B {script} restore {a.snapshot}',
        f'& {py} -B {script} launch-host --recovery "{batch_host.resolve()}"',
        f'& {py} -B {script} launch-farmhand',
        f'& {py} -B {script} wait-ready --who farmhand --max-hour 12',
    ]
    return _out({"ok": True, "run_dir": str(run_dir.resolve()), "batch_host": str(batch_host.resolve()),
                 "context": str((run_dir / "context.ps1").resolve()), "host_sha256": host_hash,
                 "farmhand_sha256": farmhand_hash, "commands": commands})


def journal_refusal(given: Path, canonical: Path, exists: bool) -> str | None:
    """Why this journal cannot be used for a trial, or None. The approval pins the canonical journal path, so the
    only fresh journal is the canonical one while it does not exist (`prep_trial.py preserve-journal LABEL` moves
    a used one aside first)."""
    if os.path.normcase(os.path.abspath(str(given))) != os.path.normcase(os.path.abspath(str(canonical))):
        return f"--journal must be the canonical journal {canonical} (the approval pins it); got {given}"
    if exists:
        return (f"journal {canonical} already exists (used or not): run `prep_trial.py preserve-journal LABEL` first; "
                "a trial never reuses a journal")
    return None


def ready_verdict(pose, session, max_hour: int, *, any_screen: bool = False) -> tuple:
    """(ready, reasons) for wait-ready. Game time is HHMM; the window is 'hour <= max_hour', not a literal time.

    ``any_screen`` (ADR 241 C6, raw hands: the cycle start and a rejoin): whatever is on screen is her first page, so
    "not free" and "menu open" are not reasons there. A missing pose, no location, the clock and the world stay
    reasons. Without it (the night readback, the operator tools) the verdict is unchanged."""
    reasons = []
    if not isinstance(pose, dict):
        reasons.append("no pose read on the mod port")
    else:
        if pose.get("location") is None:
            reasons.append("not in a location")
        if pose.get("is_player_free") is not True and not any_screen:
            reasons.append("not free")
        if pose.get("active_menu") is not None and not any_screen:
            reasons.append(f"menu open: {pose.get('active_menu')}")
        t = pose.get("time_of_day")
        if not isinstance(t, int) or t > max_hour * 100 + 59:
            reasons.append(f"game time {t} is after hour {max_hour:02d}")
    if not isinstance(session, dict):
        reasons.append("no session.json")
    elif session.get("world_ready") is not True:
        reasons.append("world not ready")
    return (not reasons), reasons


def parse_run_stdout(text: str) -> dict | None:
    """The exit/report line prep_trial.cmd_run prints (one JSON line starting {"exit")."""
    for line in (text or "").splitlines():
        if line.startswith('{"exit"'):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def inventory_slots(farmer) -> dict | None:
    """Slots and free slots of one <Farmer> element (an empty slot is <Item xsi:nil="true">). None when unreadable."""
    items = farmer.find("items") if farmer is not None else None
    if items is None:
        return None
    slots = list(items)
    return {"slots": len(slots), "free": sum(1 for it in slots if it.get(NIL) == "true")}


def coop_barn_summary(raw: bytes) -> dict:
    """Save-side facts for p13: FarmAnimal wasPet / currentProduce in every Coop and Barn. A save read, not native."""
    root = S._root(raw)
    rows = []
    for b in root.iter("Building"):
        kind = (b.findtext("buildingType") or "")
        if "coop" not in kind.lower() and "barn" not in kind.lower():
            continue
        for a in b.iter("FarmAnimal"):
            rows.append({"building": kind, "wasPet": a.findtext("wasPet"), "currentProduce": a.findtext("currentProduce")})
    return {"animals": len(rows), "unpetted": sum(1 for r in rows if r["wasPet"] == "false"),
            "with_produce": sum(1 for r in rows if r["currentProduce"] not in (None, "", "-1")), "rows": rows}


# ---------------------------------------------------------------- live readers (the only place that touches the machine)
class Live:
    def games(self) -> dict:
        return {img: wf.image_pids(img) for img in GAME_IMAGES}

    def trial_running(self):
        return wf.trial_running()

    def file_sha256(self, path):
        p = Path(path)
        return hashlib.sha256(ipc.read_bytes(p)).hexdigest() if p.is_file() else None

    def read_bytes(self, path):
        p = Path(path)
        return ipc.read_bytes(p) if p.is_file() else None

    def git_head(self, root):
        try:
            return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
                                           stderr=subprocess.DEVNULL, timeout=20).strip()
        except (OSError, subprocess.SubprocessError):
            return None

    def disk_staging(self):
        cfg = paths.read_mod_config()
        if not isinstance(cfg, dict):
            return None
        return cfg.get(STAGING_KEY) is True

    def installed_dll(self):
        return paths.MOD / "ArtiStardewBridge.dll"

    def save_file(self):
        return S.save_file()

    def journal_dir(self):
        return paths.JOURNAL_DIR

    def run_dir(self):
        return paths.RUN

    def command_dir(self):
        return paths.COMMAND_DIR

    def session(self):
        p = paths.COMMAND_DIR / "session.json"
        try:
            return json.loads(ipc.read_text(p, encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    def pose(self):
        return PS.sample()          # binds the mod port for 2.5 s: never while a trial runs

    def inspect(self):
        """Live7: one inspect_context through the command inbox (no port, no input), or None."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import arti_stardew_command as command
        try:
            sesi = command.baca_sesi(paths.COMMAND_DIR, retry_detik=0.25)
            res = command.minta_inspect_context(folder=paths.COMMAND_DIR, sesi=sesi)
        except (OSError, ValueError, command.KesalahanPerintah):
            return None
        ctx = (res.mentah or {}).get("context") if res.sukses else None
        return ctx if isinstance(ctx, dict) else None

    def host_hwnd(self):
        try:
            return int(ipc.read_text(paths.HOST_HWND_FILE, encoding="utf-8").strip())
        except (OSError, ValueError):
            return None


def _out(obj: dict, rc: int = 0):
    return obj, rc


# ---------------------------------------------------------------- subcommands
def cmd_preflight(a, live):
    problems = []
    g = games_state(live.games())
    if g["running"] is None:
        problems.append("cannot list the game processes (tasklist failed)")
    elif g["running"]:
        problems.append("a game or SMAPI process is running; preflight expects both closed before a launch")
    tr = live.trial_running()
    if tr is None:
        problems.append("trial state unknown (process list failed)")
    elif tr:
        problems.append("a trial runner is running")
    inbox = paths.command_dir_mismatch()
    if inbox:
        problems.append(inbox)

    supplied = Path(a.dll)
    sup_sha = live.file_sha256(supplied)
    if sup_sha is None:
        problems.append(f"--dll {supplied} is missing")
    inst_sha = live.file_sha256(live.installed_dll())
    dll_match = sup_sha is not None and inst_sha == sup_sha
    if not dll_match:
        problems.append("installed DLL differs from --dll (wrong DLL or deploy not done)")

    head = live.git_head(LIVE_ROOT)
    live_block = {"root": str(LIVE_ROOT), "head": head, "expected": a.sha, "match": None}
    if a.sha is not None:
        live_block["match"] = bool(head) and head.lower().startswith(a.sha.lower())
        if not live_block["match"]:
            problems.append(f"live-checkout HEAD {head} is not --sha {a.sha}")

    save = {"path": str(live.save_file()), "date": None, "readable": False}
    inventory = {"readable": False, "farmhands": []}
    raw = live.read_bytes(live.save_file())
    if raw is None:
        problems.append("save file missing")
    else:
        try:
            facts = S.facts(raw)
            save.update(readable=True, date=facts["date"], farmhands_named=[f.get("name") for f in facts["farmhands"]])
            root = S._root(raw)
            inventory = {"readable": True, "farmhands": [dict(name=f.findtext("name"), **(inventory_slots(f) or {}))
                                                         for f in root.findall("farmhands/Farmer")]}
        except Exception as e:  # noqa: BLE001 - report, never crash the preflight
            problems.append(f"save unreadable: {type(e).__name__}")

    journal = journal_state(live.journal_dir())
    if journal["exists"]:
        problems.append(f"journal exists ({'used' if journal['used'] else 'empty folder'}): preserve it first with "
                        "`prep_trial.py preserve-journal LABEL`")
    run = live.run_dir()
    report_fresh = run / ("report-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
    out = {
        "row": a.row, "ok": not problems, "problems": problems,
        "games": {"pids": g["pids"], "running": g["running"]},
        "trial_running": tr,
        "staging": {"disk_flag": live.disk_staging(), "loaded": "unknown from disk: confirm with arti_stage_event fixb62-off-check"},
        "dll": {"supplied": str(supplied), "supplied_sha256": sup_sha, "installed_sha256": inst_sha, "match": dll_match},
        "live": live_block, "save": save, "inventory": inventory,
        "journal": journal,
        "report": {"dir": str(run), "fresh_path": str(report_fresh) if not report_fresh.exists() else None},
    }
    return _out(out)


def cmd_restore(a, live):
    g = games_state(live.games())
    if g["running"] is None:
        raise Refused("cannot list game processes: refusing a restore")
    if g["running"]:
        raise Refused("a game or SMAPI process is running: close them before a restore")
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        rc = S.main(["restore", a.name])
    try:
        report = json.loads(buf_out.getvalue())
    except ValueError:
        report = None
    out = {"restore": a.name, "rc": rc, "verified": report.get("verified") if report else None,
           "date": report.get("date") if report else None, "stderr": buf_err.getvalue().strip()[-400:] or None}
    return _out(out, 0 if rc == 0 else 3)


def _wait_for(pred, timeout: float, step: float = 0.5, sleep=time.sleep, clock=time.time):
    end = clock() + timeout
    while True:
        v = pred()
        if v:
            return v
        if clock() >= end:
            return None
        sleep(step)


def cmd_launch_host(a, live):
    rec = Path(a.recovery)
    sup = rec / "host_supervisor.py"
    if not sup.is_file():
        raise Refused(f"{sup} missing: copy the archived HOST supervisor into a fresh recovery folder first (FIXB62 step 4)")
    if (rec / "host-launch.json").exists() or (rec / "host-exit.json").exists():
        raise Refused(f"{rec} already holds a HOST launch record: use a fresh recovery folder")
    g = games_state(live.games())
    if g["running"] is not False:
        raise Refused("a game or SMAPI process is running (or unknown): HOST launch needs both closed")
    if live.trial_running():
        raise Refused("a trial runner is running")
    proc = subprocess.Popen([str(P.PY), "-B", str(sup)], cwd=str(P.GAME), creationflags=CREATE_FLAGS, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def smapi_pid():
        try:
            return json.loads(ipc.read_text(rec / "host-launch.json", encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            return None
    pid = _wait_for(smapi_pid, LAUNCH_WAIT_S)
    if pid is None:
        return _out({"ok": False, "supervisor_pid": proc.pid, "error": "no host-launch.json within the bounded wait"}, 3)

    def window():
        for w in wf.enum_windows("running SMAPI"):
            if w["pid"] == pid:
                return w
        return None
    win = _wait_for(window, LAUNCH_WAIT_S)
    if win is None:
        return _out({"ok": False, "supervisor_pid": proc.pid, "smapi_pid": pid, "error": "no SMAPI window for the launch pid"}, 3)
    paths.HOST_HWND_FILE.parent.mkdir(parents=True, exist_ok=True)
    ipc.atomic_write(paths.HOST_HWND_FILE, str(win["hwnd"]))
    return _out({"ok": True, "supervisor_pid": proc.pid, "smapi_pid": pid, "hwnd": win["hwnd"], "title": win["title"],
                 "hwnd_file": str(paths.HOST_HWND_FILE)})


def join_slot_arg(text: str) -> str:
    t = (text or "").strip()
    if t == "new" or (t.startswith("name:") and 1 <= len(t) - 5 <= 12):
        return t
    raise argparse.ArgumentTypeError("--join-slot is 'new' or 'name:NAME' (1-12 characters)")


def charcreate_verdict(session, ctx) -> tuple:
    """(ready, reasons) for wait-charcreate: the game's farmhand character creation is open and published."""
    reasons = []
    if not isinstance(session, dict) or session.get("world_ready") is not True:
        reasons.append("world not ready")
    if not isinstance(ctx, dict):
        reasons.append("no inspect_context")
    else:
        menu = (ctx.get("interactive") or {}).get("menu_type")
        block = ctx.get("ui_pad") or {}
        if menu != "CharacterCustomization":
            reasons.append(f"menu is {menu!r}, not CharacterCustomization")
        elif block.get("context") != "char_custom" or not block.get("char_custom"):
            reasons.append(f"ui_pad context {block.get('context')!r} (CharacterCreationEnabled / UiPadMenusEnabled off?)")
    return (not reasons), reasons


def cmd_wait_charcreate(a, live, sleep=time.sleep, clock=time.time):
    if live.trial_running():
        raise Refused("a trial runner is running: the command inbox is in use")
    end = clock() + a.timeout
    while True:
        ctx = live.inspect()
        ok, reasons = charcreate_verdict(live.session(), ctx)
        if ok:
            block = ctx["ui_pad"]
            return _out({"ok": True, "receiver": block.get("receiver_token"), "ready": block.get("ready"),
                         "wait_reason": block.get("wait_reason"),
                         "waited_s": round(a.timeout - (end - clock()), 1)})
        if clock() >= end:
            return _out({"ok": False, "reasons": reasons}, 3)
        sleep(READY_STEP_S)


def cmd_launch_farmhand(a, live):
    if live.trial_running():
        raise Refused("a trial runner is running")
    if live.host_hwnd() is None or not wf.window(live.host_hwnd())["exists"]:
        raise Refused("HOST window not known (run/host-hwnd.txt): launch and load the HOST first")
    old = None
    if paths.FARMHAND_LAUNCH.exists():
        try:
            old = json.loads(ipc.read_text(paths.FARMHAND_LAUNCH, encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            old = None
        if old is not None and wf.pid_alive(old) and not getattr(a, "reconnect", False):
            raise Refused(f"farmhand pid {old} is alive: this rejoin needs a closed farmhand")
    log = live.run_dir() / f"launch-farmhand-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{time.time_ns()}.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as f:
        try:
            join = getattr(a, "join_slot", None)
            close = getattr(a, "reconnect", False) and old is not None and wf.pid_alive(old)
            stop_file = getattr(a, "stop_file", None)
            rc = subprocess.run([str(P.PY), "-B", str(HOST_RESTART)] + ([] if close else ["--no-close"]) + (["--join-slot", join] if join else [])
                                + (["--stop-file", str(stop_file)] if stop_file else []),
                                cwd=str(HOST_RESTART.parent),
                                stdout=f, stderr=subprocess.STDOUT, timeout=RESTART_TIMEOUT_S).returncode
        except subprocess.TimeoutExpired:
            rc = None
    pid = hwnd = None
    try:
        pid = json.loads(ipc.read_text(paths.FARMHAND_LAUNCH, encoding="utf-8")).get("pid")
    except (OSError, ValueError):
        pass
    try:
        hwnd = int(ipc.read_text(paths.FH_HWND_FILE, encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return _out({"ok": rc == 0, "rc": rc, "pid": pid, "hwnd": hwnd, "log": str(log)}, 0 if rc == 0 else 3)


def cmd_wait_ready(a, live, sleep=time.sleep, clock=time.time):
    if live.trial_running():
        raise Refused("a trial runner is running: the pose port is in use")
    end = clock() + a.timeout
    last = (False, ["never sampled"])
    while True:
        expected_save = getattr(a, "expected_save_id", None)
        diagnostics = {}
        pose = PS.sample(diagnostics=diagnostics) if expected_save is not None else live.pose()
        session = live.session()
        last = ready_verdict(pose, session, a.max_hour, any_screen=getattr(a, "any_screen", False))
        if last[0]:
            if expected_save is not None:
                from scripts.m1_operator.live_save import scene_save_identity
                try:
                    identity = scene_save_identity(diagnostics.get("scene_identity"), session)
                except ValueError as exc:
                    raise Refused(str(exc)) from None
                if identity["save_id"] != expected_save or int(identity["save_id"]) == 100000001:
                    raise Refused("native save identity disagrees with the disposable bench start")
            if getattr(a, "expected_pose", None):
                from scripts.free_play.expectations import matches
                with a.expected_pose.open(encoding="utf-8") as file:
                    expected = json.load(file)
                if not matches(pose, expected):
                    raise Refused("staged pose failed native readback")
            out = {"ok": True, "who": a.who, "max_hour": a.max_hour, "waited_s": round(a.timeout - (end - clock()), 1)}
            if getattr(a, "any_screen", False):  # ADR 241 C6: say what is on screen; it is her first page
                out.update(screen=pose.get("active_menu"), free=pose.get("is_player_free"))
            return _out(out)
        # A startup gate never waits for another native day to hide a missed window.
        t = pose.get("time_of_day") if isinstance(pose, dict) else None
        expired = (isinstance(session, dict) and session.get("world_ready") is True
                   and type(t) is int and t > a.max_hour * 100 + 59)
        if expired or clock() >= end:
            return _out({"ok": False, "who": a.who, "max_hour": a.max_hour, "reasons": last[1],
                         "cause": "startup_morning_window_expired" if expired else "readiness_timeout",
                         "time_of_day": t, "waited_s": round(a.timeout - (end - clock()), 1)}, 3)
        sleep(READY_STEP_S)


def cmd_trial(a, live):
    skills_mode = getattr(a, "skills", False)
    skills_envelope_ok = getattr(a, "skills_envelope_ok", False)
    goal = ipc.read_text(Path(a.goal_file), encoding="utf-8-sig").strip() if Path(a.goal_file).is_file() else ""
    problems = validate_trial(goal, a.owner_url, a.inputs, a.seconds)
    if skills_envelope_ok and not skills_mode:
        problems.append("--skills-envelope-ok requires --skills")
    canonical = live.journal_dir()
    refusal = journal_refusal(Path(a.journal), canonical, exists=canonical.exists())
    if refusal:
        problems.append(refusal)
    if live.trial_running():
        problems.append("a trial runner is running")
    if problems:
        raise Refused("; ".join(problems))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            P.cmd_approval(argparse.Namespace(owner_text=goal, owner_url=a.owner_url, goal=goal, minutes=a.minutes,
                                              seconds=a.seconds, inputs=a.inputs))
    except SystemExit as e:
        raise Refused(f"approval refused: {e}") from None
    except AssertionError as e:
        raise Refused(f"approval refused: {e}") from None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            P.cmd_run(argparse.Namespace(skills=skills_mode, skills_envelope_ok=skills_envelope_ok, overnight=False, inputs_ok=False,
                                         inputs=a.inputs, day_start=False, no_pose_gate=False))
    except SystemExit as e:
        raise Refused(f"trial did not start: {e}") from None
    head = parse_run_stdout(buf.getvalue())
    if head is None or not head.get("report"):
        return _out({"ok": False, "error": "no exit/report line from prep_trial run", "stdout_tail": buf.getvalue()[-400:]}, 3)
    rep_path = Path(head["report"])
    report = {}
    if rep_path.is_file():
        try:
            report = json.loads(ipc.read_text(rep_path, encoding="utf-8"))
        except ValueError:
            report = {}
    verdict = report.get("status")
    return _out({"ok": verdict is not None, "exit": head.get("exit"), "seconds": head.get("seconds"),
                 "report": str(rep_path), "verdict": verdict, "reason": report.get("reason"),
                 "inputs": a.inputs, "limit_seconds": a.seconds, "journal": str(canonical),
                 **({k: report.get(k) for k in ("skills", "skills_input_envelope", "skills_envelope_needed",
                                               "trial_input_envelope")} if skills_mode else {})},
                0 if verdict is not None else 3)


def cmd_readback(a, live):
    if live.trial_running():
        raise Refused("a trial runner is running: the pose port is in use; read back after the trial")
    pose = live.pose()
    session = live.session()
    checks = {}
    expect = ROW_LOCATION[a.row]
    if expect is not None:
        checks["location_is_expected"] = (pose or {}).get("location") == expect
    newest = sorted(live.run_dir().glob("report-*.json"))
    report = None
    if newest:
        try:
            r = json.loads(ipc.read_text(newest[-1], encoding="utf-8"))
            report = {"file": newest[-1].name, **{k: r.get(k) for k in ("status", "reason", "physical_inputs", "final_goal")}}
        except ValueError:
            report = {"file": newest[-1].name, "unreadable": True}
    save = None
    raw = live.read_bytes(live.save_file())
    if raw is not None:
        try:
            save = {"date": S.facts(raw)["date"]}
            if a.row == "p13":
                save["coop_barn"] = {k: v for k, v in coop_barn_summary(raw).items() if k != "rows"}
        except Exception as e:  # noqa: BLE001
            save = {"unreadable": type(e).__name__}
    out = {"row": a.row, "pose": pose, "session": {k: (session or {}).get(k) for k in
                                                   ("session_id", "player_name", "location_id", "world_ready")},
           "checks": checks, "last_report": report, "save": save}
    return _out(out)


# ---------------------------------------------------------------- CLI
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    prep = sub.add_parser("prepare-run", help="offline setup and validation for a fresh witness run")
    prep.add_argument("--run-dir", required=True, help="new run folder to create or an existing empty folder")
    prep.add_argument("--template", required=True, help="previous working batch-host folder")
    prep.add_argument("--sha256", required=True, type=sha256_arg, help="expected HOST and installed farmhand DLL SHA-256")
    prep.add_argument("--row", required=True, type=row_name)
    prep.add_argument("--snapshot", required=True, type=snapshot_name)
    prep.set_defaults(f=cmd_prepare_run)
    p = sub.add_parser("preflight", help="read-only pre-launch facts")
    p.add_argument("--row", required=True, type=row_name)
    p.add_argument("--dll", required=True, help="the supplied DLL the trial must run")
    p.add_argument("--sha", type=sha_arg, default=None, help="expected live-checkout HEAD (prefix ok)")
    p.set_defaults(f=cmd_preflight)
    r = sub.add_parser("restore", help="stage_save.py restore NAME (games closed)")
    r.add_argument("name", type=snapshot_name)
    r.set_defaults(f=cmd_restore)
    h = sub.add_parser("launch-host", help="FIXB62 step 4: one SMAPI HOST supervisor, wait for its window")
    h.add_argument("--recovery", required=True, help="a fresh folder holding host_supervisor.py")
    h.set_defaults(f=cmd_launch_host)
    lf = sub.add_parser("launch-farmhand", help="FIXB62 step 6: restart_farmhand.py --no-close (rejoin)")
    lf.add_argument("--reconnect", action="store_true", help="disconnected farmhand: verified graceful close via restart_farmhand, then rejoin")
    lf.add_argument("--stop-file", type=Path, help="cooperative STOP during relaunch/join")
    lf.add_argument("--join-slot", type=join_slot_arg, default=None,
                    help="Live7: 'new' (unclaimed cabin -> character creation) or 'name:NAME', from the mod's join facts")
    lf.set_defaults(f=cmd_launch_farmhand)
    wc = sub.add_parser("wait-charcreate", help="Live7: bounded wait until the farmhand character creation is published")
    wc.add_argument("--timeout", type=float, default=READY_TIMEOUT_S)
    wc.set_defaults(f=cmd_wait_charcreate)
    w = sub.add_parser("wait-ready", help="bounded wait until in-world, free, no menu, hour <= max")
    w.add_argument("--who", required=True, choices=["farmhand"])
    w.add_argument("--max-hour", required=True, type=hour_arg)
    w.add_argument("--expected-save-id", help="require this identity from the native scene token")
    w.add_argument("--expected-pose", type=Path, help="checked dotted-field predicates over the native pose")
    w.add_argument("--timeout", type=float, default=READY_TIMEOUT_S)
    w.add_argument("--any-screen", action="store_true",
                   help="ADR 241 C6: an open menu or a not-free player is not a reason (the cycle start, a rejoin)")
    w.set_defaults(f=cmd_wait_ready)
    t = sub.add_parser("trial", help="approval + one combined trial with a guaranteed-fresh journal")
    t.add_argument("--goal-file", required=True)
    t.add_argument("--owner-url", required=True)
    t.add_argument("--inputs", required=True, type=int)
    t.add_argument("--seconds", required=True, type=int)
    t.add_argument("--journal", required=True, help="the canonical journal path that does not exist yet (preflight prints it)")
    t.add_argument("--minutes", type=int, default=45)
    t.add_argument("--skills", action="store_true", help="use the existing skill-session driver and report")
    t.add_argument("--skills-envelope-ok", action="store_true", help="explicit existing skill input-envelope opt-in")
    t.set_defaults(f=cmd_trial)
    rb = sub.add_parser("readback", help="independent read of pose, session, newest report, save facts")
    rb.add_argument("--row", required=True, choices=READBACK_ROWS)
    rb.set_defaults(f=cmd_readback)
    return ap


def main(argv=None, live=None):
    a = build_parser().parse_args(argv)
    try:
        out, rc = a.f(a, live or Live())
    except Refused as e:
        out, rc = {"ok": False, "refused": str(e)}, 2
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
