"""m1_paths.py: the one place that knows where the M1 operator's folders live. Every tool imports it.

Each value comes from an environment variable; the default is an example folder layout.

  ARTI_M1_PREP          trial prep folder (approval.json, trial-config.json, mod-backup-*, deploy-record-*, save-backup-*)
  ARTI_M1_RUN           operator run folder (journal/, reports, frames, screenshots, hwnd files)   default PREP/run
  ARTI_M1_COMMAND_DIR   mod command inbox (request.json / result.json / session.json)             default RUN/command
  ARTI_M1_WORKTREE      Stage 2 runtime worktree (stardew_m1_trial.py, arti_stardew_command.py)
  ARTI_CHECKOUT              the checkout (default: this one) (config_local.json, venv)
  ARTI_VENV_PYTHON      python used for the trial runner and the SMAPI supervisor      default DEV/venv/Scripts/python.exe
  ARTI_STARDEW_GAME     Stardew Valley install (Stardew Valley.exe, StardewModdingAPI.exe, Mods/)
  ARTI_M1_DLL           mod DLL that `prep_trial.py deploy` / `restart_farmhand.sh --deploy` installs by default
  ARTI_SMAPI_LOG        SMAPI log read by run_attempt.sh       default %APPDATA%/StardewValley/ErrorLogs/SMAPI-latest.txt
  ARTI_HOST_SMAPI_LOG / ARTI_FARMHAND_SMAPI_LOG   Live7 labelled logs (HOST first instance = SMAPI-latest.txt,
                        farmhand second instance = SMAPI-latest.player-2.txt)
  ARTI_M1_PORT          mod NDJSON port (the trial runtime listens here too; see README)            default 3017
  ARTI_M1_SAVE_NAME     save folder name recorded in approval.json / staged by stage_save.py       default host1_100000003
  ARTI_STARDEW_SAVES    the game's Saves folder                            default %APPDATA%/StardewValley/Saves
  ARTI_STARDEW_STARTUP_PREFS  startup_preferences (gamepadMode survives here, PLAN §1.5 a)
                        default %APPDATA%/StardewValley/startup_preferences

  python m1_paths.py            print the resolved values as JSON
  python m1_paths.py --shell    print `export NAME='value'` lines (forward slashes) for the bash tools to eval
  python m1_paths.py --mode [auto|pad|v1]   print the operator mode: pad (Primitives v2 controller model, no cursor
                        park) when asked, or for auto when the installed mod config.json has GamepadInputEnabled true;
                        auto with an absent / unreadable config exits 2 (never falls open to v1)

v2 (controller model) names live here too: PAD_KEYS = the session.json keys WS-G0 publishes (pad_watch.py, the
pre-flights and soak.py read only these constants), PAD_LOG_TAGS = the SMAPI line prefixes, PAD_PANIC_FILE.

Reading this module has no side effects: nothing is created, nothing binds a port.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

# Operator scripts also run under python -I, outside the repository cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from arti_runtime import ipc


def read_session():
    """Read one live snapshot without denying the mod rename access (ADR 152)."""
    return ipc.read_json(COMMAND_DIR / 'session.json')

_ARTI = Path(__file__).resolve().parents[3]  # the folder that holds this checkout


def _p(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


PREP = _p("ARTI_M1_PREP", _ARTI / "operator-staging")
RUN = _p("ARTI_M1_RUN", PREP / "run")
WORKTREE = _p("ARTI_M1_WORKTREE", Path(__file__).resolve().parents[2])
DEV = _p("ARTI_CHECKOUT", Path(__file__).resolve().parents[2])
PY = _p("ARTI_VENV_PYTHON", DEV / "venv" / "Scripts" / "python.exe")
GAME = _p("ARTI_STARDEW_GAME", Path(r"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley"))
DLL = _p("ARTI_M1_DLL", WORKTREE / "stardew-mod/ArtiStardewBridge/bin/Release/net6.0/ArtiStardewBridge.dll")
SMAPI_LOG = _p("ARTI_SMAPI_LOG", Path(os.environ.get("APPDATA", "")) / "StardewValley/ErrorLogs/SMAPI-latest.txt")
# Two SMAPI instances (HOST + farmhand): the first writes SMAPI-latest.txt, the second SMAPI-latest.player-2.txt.
# Live7 starts the HOST first (live7 rehearsal 1: HOST = SMAPI-latest.txt, farmhand = .player-2).
_ERROR_LOGS = Path(os.environ.get("APPDATA", "")) / "StardewValley/ErrorLogs"
HOST_SMAPI_LOG = _p("ARTI_HOST_SMAPI_LOG", _ERROR_LOGS / "SMAPI-latest.txt")
FARMHAND_SMAPI_LOG = _p("ARTI_FARMHAND_SMAPI_LOG", _ERROR_LOGS / "SMAPI-latest.player-2.txt")
PORT = int(os.environ.get("ARTI_M1_PORT") or 3017)
SAVE_NAME = os.environ.get("ARTI_M1_SAVE_NAME") or "host1_100000003"
_APPDATA = Path(os.environ.get("APPDATA", ""))
SAVES = _p("ARTI_STARDEW_SAVES", _APPDATA / "StardewValley/Saves")
STARTUP_PREFS = _p("ARTI_STARDEW_STARTUP_PREFS", _APPDATA / "StardewValley/startup_preferences")

# derived (not separately configurable)
MOD = GAME / "Mods/ArtiStardewBridge"
MOD_CONFIG = MOD / "config.json"


def mod_command_inbox() -> Path | None:
    """The command inbox the installed mod actually uses (config.json CommandInboxDirectory), or None if unreadable."""
    try:
        v = json.loads(MOD_CONFIG.read_text(encoding="utf-8-sig")).get("CommandInboxDirectory")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(v) if v else None


# Skills W1 Block B run 2 (2026-10-16): a new ARTI_M1_RUN silently moved the default inbox away from the mod's.
# Default to the installed mod's inbox; an explicit ARTI_M1_COMMAND_DIR still wins (witness_run preflight refuses a mismatch).
COMMAND_DIR = _p("ARTI_M1_COMMAND_DIR", mod_command_inbox() or RUN / "command")


def command_dir_mismatch() -> str | None:
    """A refusal reason when COMMAND_DIR differs from the installed mod's inbox; None when they agree or it is unknown."""
    mod = mod_command_inbox()
    if mod is None:
        return None
    if os.path.normcase(os.path.abspath(COMMAND_DIR)) != os.path.normcase(os.path.abspath(mod)):
        return f"command inbox {COMMAND_DIR} != installed mod CommandInboxDirectory {mod}: unset ARTI_M1_COMMAND_DIR"
    return None
JOURNAL_DIR = RUN / "journal"
TRIAL_CONFIG = PREP / "trial-config.json"
APPROVAL = PREP / "approval.json"
FH_HWND_FILE = RUN / "fh-hwnd.txt"
HOST_HWND_FILE = RUN / "host-hwnd.txt"
UI_LOG = RUN / "ui-op-log.jsonl"
FARMHAND_LAUNCH = RUN / "farmhand-launch.json"
FARMHAND_EXIT = RUN / "farmhand-exit.json"
LEARNED_BLOCKED = RUN / "learned-blocked.json"   # walk_to's learned blocked tiles + failed goals per location (D37)

TOOLS = Path(__file__).resolve().parent

# ---------------------------------------------------------------- v2 controller model (WS-G0 / WS-G7)
# The ONE place the operator tools name the pad fields. Keys marked G0 are in WS-G0_BRIEF §2.2-2.3 / §3.1 (published in
# session.json, written on change); keys marked G7 are requested from G0 and simply reported as absent (None) until
# the mod publishes them. Every consumer must treat a missing key as "unknown", never as false.
PAD_FLAG = "GamepadInputEnabled"                     # ModConfig flag (default OFF); the v2-pad preset turns it on
DRAW_FLAG = "FarmhandInactiveRenderingEnabled"       # the cursor-hidden gate needs the Draw path (PLAN §1.5 b)
PAD_KEYS = {
    "owned": "pad_mode_owned",                       # G0: ARTI holds gamepadMode=ForceOn for this inactive session
    "restore_pending": "pad_mode_restore_pending",   # G0: owner exit or FIXB46 restore-write retry not finished
    "cleanup_fault": "pad_mode_cleanup_fault",       # FIXB46 R2: latched unresolved cleanup, new takes refused
    "pin": "pad_connected_pin",                      # G0: _oldGamepadConnectedState pinned (pinned restore path)
    "cursor_hidden": "cursor_hidden",                # G0 slice 2: cursor-hidden gate verdict
    "cursor_hidden_reason": "cursor_hidden_reason",  # G0 slice 2: CURSOR_NOT_HIDDEN:<why>
    "warp_calls": "os_warp_calls",                   # G0: InputState.SetMousePosition calls (all)
    "warp_blocked": "os_warp_blocked",               # G0: calls skipped while inactive
    "warp_unblocked": "os_warp_unblocked_inactive",  # G0: must stay 0 (an unblocked warp = global stop, PLAN §6.2)
    "auto_run": "auto_run",                          # G0 slice 2
    "gamepad_mode": "gamepad_mode",                  # G7 request: Game1.options.gamepadMode as a string
    "overrides": "pad_override_count",               # diagnostic only: the WHOLE override ledger, Suppress-only
                                                     # entries persist the entire inactive session (never a verdict)
    "outstanding": "pad_outstanding_count",          # G0: held pad Presses only (pad_watch exit 6 keys on this)
}
PAD_LOG_TAGS = ("PAD_MODE", "GAMEPAD_PROBE", "PAD_PANIC")   # SMAPI line prefixes pad_watch / pad_panic follow
PAD_PANIC_FILE = "pad_panic.json"                    # G7 request: inbox control file for the kill switch (PLAN §1.5 e)
OPERATOR_MODES = ("auto", "pad", "v1")
_READ = object()


class ModeUnknown(ValueError):
    """auto mode with an unreadable / absent installed mod config: never fall open to v1."""


def read_mod_config(path=None):
    """The installed mod config.json as a dict, None when absent or unreadable (read-only)."""
    try:
        return json.loads(Path(path or MOD_CONFIG).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def operator_mode(requested="auto", config=_READ, config_path=None):
    """'pad' or 'v1'. auto = pad iff the installed mod config has GamepadInputEnabled true. auto with an unreadable or
    absent config (config None, or the file cannot be read) raises ModeUnknown: pass --mode pad|v1 explicitly."""
    if requested not in OPERATOR_MODES:
        raise ValueError(f"mode must be one of {OPERATOR_MODES}, got {requested!r}")
    if requested != "auto":
        return requested
    cfg = read_mod_config(config_path) if config is _READ else config
    if not isinstance(cfg, dict):
        raise ModeUnknown(f"mode auto: the installed mod config ({config_path or MOD_CONFIG}) is absent or unreadable; "
                          "pass --mode pad or --mode v1")
    return "pad" if cfg.get(PAD_FLAG) is True else "v1"

_EXPORTS = {
    "ARTI_M1_PREP": PREP, "ARTI_M1_RUN": RUN, "ARTI_M1_COMMAND_DIR": COMMAND_DIR, "ARTI_M1_WORKTREE": WORKTREE,
    "ARTI_CHECKOUT": DEV, "ARTI_VENV_PYTHON": PY, "ARTI_STARDEW_GAME": GAME, "ARTI_M1_DLL": DLL, "ARTI_SMAPI_LOG": SMAPI_LOG,
    "ARTI_M1_PORT": PORT, "ARTI_M1_TOOLS": TOOLS, "ARTI_M1_JOURNAL_DIR": JOURNAL_DIR,
    "ARTI_M1_FH_HWND_FILE": FH_HWND_FILE, "ARTI_M1_HOST_HWND_FILE": HOST_HWND_FILE,
    "ARTI_M1_FARMHAND_LAUNCH": FARMHAND_LAUNCH, "ARTI_M1_SAVE_NAME": SAVE_NAME, "ARTI_STARDEW_SAVES": SAVES,
    "ARTI_STARDEW_STARTUP_PREFS": STARTUP_PREFS,
}


def as_dict() -> dict:
    return {k: (v.as_posix() if isinstance(v, Path) else v) for k, v in _EXPORTS.items()}


def shell_lines() -> str:
    return "\n".join(f"export {k}={shlex.quote(str(v))}" for k, v in as_dict().items())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shell", action="store_true", help="print bash `export` lines instead of JSON")
    ap.add_argument("--mode", nargs="?", const="auto", choices=OPERATOR_MODES,
                    help="print the operator mode (pad|v1) for the requested mode (default auto: from the mod config)")
    a = ap.parse_args(argv)
    if a.mode:
        try:
            print(operator_mode(a.mode))
        except ModeUnknown as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 2
        return 0
    print(shell_lines() if a.shell else json.dumps(as_dict(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
