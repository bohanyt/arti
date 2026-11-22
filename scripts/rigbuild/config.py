"""The one config file of the build procedures (ADR 245). No machine path is a constant in code.

`load(path, overrides)` reads a JSON object whose keys are named below; an argument (`--rig-root`, ...) overrides
the file. `rigbuild.example.json` next to this file holds placeholder paths as the documented example; replace
every one. Paths relative to the rig root (`evidence/`, `evidence/pytest-slot/HOLD.txt`) are the procedure's own
layout, shared with the bench (ADR 242), not machine constants.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
from pathlib import Path

REQUIRED = {
    "rig_root": "the rig folder: evidence/, the shared pytest slot evidence/pytest-slot/HOLD.txt, builds, bookings",
    "checkout": "the dedicated build checkout: detached at the source commit, clean, no private state",
    "python312": "the rig interpreter, Python 3.12 (requirements.txt + requirements-test.txt)",
    "python311": "the voice-lane interpreter, Python 3.11 (the bridge venv)",
    "dotnet": "the .NET 8 SDK launcher, dotnet.exe",
    "game": "the Stardew Valley folder (Stardew Valley.dll, StardewModdingAPI.exe, Content/)",
    "nuget_offline": "the offline NuGet source folder of the C# builds",
}
OPTIONAL = {
    "receipts": "folder for RECEIPT_*.md notes (default: the run's evidence folder)",
    "saves": "the Stardew Saves folder the privacy guard refuses (default %APPDATA%/StardewValley/Saves)",
    "private_root": "the folder whose first-level vault* names the privacy guard refuses (default: rig_root's parent)",
    "booking_like": "an earlier test-build booking whose rig fields a new test-build booking copies",
    "savestates": "the savestate library (default rig_root/savestates)",
    "housekeeping_hold": "a marker file recorded (never obeyed) in every admission row",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    rig_root: Path
    checkout: Path
    python312: Path
    python311: Path
    dotnet: Path
    game: Path
    nuget_offline: Path
    receipts: Path | None = None
    saves: Path | None = None
    private_root: Path | None = None
    booking_like: Path | None = None
    savestates: Path | None = None
    housekeeping_hold: Path | None = None

    @property
    def evidence_root(self):
        return self.rig_root / "evidence"

    @property
    def slot(self):
        """The one pytest slot every worker and the coordinator share (create-new HOLD.txt)."""
        return self.rig_root / "evidence" / "pytest-slot" / "HOLD.txt"


def _path(value, environ):
    return Path(os.path.expandvars(str(value))) if value not in (None, "") else None


def load(path=None, overrides=None, environ=None):
    environ = os.environ if environ is None else environ
    data = {}
    if path is not None:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: not a JSON object")
    unknown = sorted(set(data) - set(REQUIRED) - set(OPTIONAL) - {"_comment"})
    if unknown:
        raise ConfigError("unknown config keys: " + ", ".join(unknown))
    data.update({k: v for k, v in (overrides or {}).items() if v not in (None, "")})
    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        raise ConfigError("missing config: " + "; ".join(f"{k} ({REQUIRED[k]})" for k in missing)
                          + ". Pass --config <file> (example: scripts/rigbuild/rigbuild.example.json) or the arguments.")
    values = {f.name: _path(data.get(f.name), environ) for f in fields(Config)}
    if values["saves"] is None:
        values["saves"] = Path(environ.get("APPDATA", "")) / "StardewValley" / "Saves"
    if values["private_root"] is None:
        values["private_root"] = values["rig_root"].parent
    if values["savestates"] is None:
        values["savestates"] = values["rig_root"] / "savestates"
    return Config(**values)


def describe(cfg):
    """One line per key: the path and whether it exists. Reads nothing else."""
    rows = []
    for f in fields(Config):
        value = getattr(cfg, f.name)
        what = REQUIRED.get(f.name) or OPTIONAL.get(f.name)
        state = "unset" if value is None else ("present" if value.exists() else "MISSING")
        rows.append(f"{f.name:17} {state:8} {value}  ({what})")
    return rows
