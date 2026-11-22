"""Live7 run manifest: one self-describing file per cycle (``<cycle_dir>/run_manifest.json``).

Written by ``live_cycle.py start-cycle`` and refreshed at every ``start-day`` (and before each chained day). It names
the exact combination a cycle runs with, so a receipt can be tied to source, DLLs and configs:

- source: git SHA + dirty flag of the checkout that runs ``live_cycle.py`` (``HERE.parents[1]``, e.g. live-checkout);
- dlls: sha256 of the installed farmhand mod DLL (``m1_paths.MOD``) and of the HOST template's DLL in use
  (``RUN/batch-host/host-mods``, the copy ``witness_run.py prepare-run`` stages and ``launch-host`` runs;
  ``ARTI_LIVE7_HOST_RECOVERY`` overrides the recovery folder);
- configs: sha256 of the farmhand mod ``config.json``, ``trial-config.json`` and the HOST template ``config.json``
  (hashes only, never contents) plus the few sanitized flags that matter: ``live7_help_enabled`` /
  ``live7_moments_enabled`` (trial config, the runtime's effective config), ``RehearsalHostConsoleEnabled`` and
  ``CharacterCreationEnabled`` (both mod configs, SMAPI semantics: case-insensitive keys, last duplicate wins,
  absent = the ModConfig default false), and the provider kind/model/effort names;
- session: save id / resolved save name, cycle id, machine hostname;
- watcher: whether a webhook is configured (boolean + basis only, never the URL or the private file's path).

Nothing secret is written: ``secret_findings`` refuses URLs, token-like strings and secret-named keys before the
atomic write. Every read is independent and records only an error *type* when it fails. The caller
(``Cycle.refresh_manifest``) never lets a manifest failure block the cycle: it journals ``run_manifest_warning``.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

SCHEMA = "live7_run_manifest.v1"
FILE_NAME = "run_manifest.json"
MOD_DLL = "ArtiStardewBridge.dll"
MOD_DIR = "ArtiStardewBridge"
HOST_RECOVERY_ENV = "ARTI_LIVE7_HOST_RECOVERY"
WEBHOOK_ENV = "ARTI_LIVE7_WEBHOOK_URL"           # live_watcher.py
WATCHER_CONFIG_ENV = "ARTI_LIVE7_WATCHER_CONFIG"  # optional; default PREP/live7-watcher-private.json
WATCHER_CONFIG_NAME = "live7-watcher-private.json"
TRIAL_FLAGS = ("live7_help_enabled",)
TRIAL_OPTIONAL_FLAGS = ("live7_moments_enabled",)  # recorded only when the key is present
MOD_FLAGS = ("RehearsalHostConsoleEnabled", "CharacterCreationEnabled")
PROVIDER_KEYS = dict(kind="stardew_decision_provider_kind", model="stardew_decision_model",
                     effort="stardew_decision_effort", service_tier="stardew_decision_service_tier")
HISTORY_MAX = 200
GIT_TIMEOUT_S = 15.0
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")
SECRET_KEY = re.compile(r"(url|token|secret|password|passwd|api_?key|apikey|authorization|bearer|credential)", re.I)
SECRET_VALUE = re.compile(r"(://|\bsk-[A-Za-z0-9]|\bghp_|\bgho_|\bxox[abpr]-|\bAKIA[0-9A-Z]{8}|\bBearer\s|"
                          r"discord(app)?\.com/api/webhooks|hooks\.slack\.com)", re.I)


class ManifestSecretError(ValueError):
    """The manifest would carry something secret-looking; it is not written."""


def _err(exc):
    return type(exc).__name__


def safe_name(value):
    """A provider/model name as recorded: short identifier-like strings only, never a key or URL."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and SAFE_NAME.match(value) and not SECRET_VALUE.search(value):
        return value
    return "<redacted>"


def file_digest(path):
    """dict(path, sha256) or dict(path, sha256=None, error=<type>); never the contents."""
    if path is None:
        return dict(path=None, sha256=None, error="path_unknown"), None
    try:
        raw = ipc.read_bytes(Path(path))
    except OSError as exc:
        return dict(path=str(path), sha256=None, error=_err(exc)), None
    return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)), raw


def git_source(root):
    """SHA + dirty flag of the running checkout (read-only; no optional index lock)."""
    out = dict(root=str(root), sha=None, dirty=None)
    try:
        def git(*args):
            return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True,
                                  text=True, timeout=GIT_TIMEOUT_S, check=True, stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        out["sha"] = git("rev-parse", "HEAD")
        out["dirty"] = bool(git("status", "--porcelain", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        out["error"] = _err(exc)
    return out


def _parse(raw, *, smapi):
    """The JSON object of ``raw``; SMAPI configs fold keys case-insensitively (last duplicate wins)."""
    if raw is None:
        return None
    pairs_hook = (lambda pairs: {k.lower(): v for k, v in pairs}) if smapi else None
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs_hook)
    return value if isinstance(value, dict) else None


def mod_flags(raw):
    try:
        cfg = _parse(raw, smapi=True)
    except (ValueError, UnicodeError) as exc:
        return dict(error=_err(exc))
    if cfg is None:
        return dict(error="unreadable")
    # ModConfig.cs: both default false; only a JSON true turns them on.
    return {k: cfg.get(k.lower()) is True for k in MOD_FLAGS}


def trial_settings(raw):
    """(flags, provider) from trial-config.json bytes, sanitized."""
    try:
        cfg = _parse(raw, smapi=False)
    except (ValueError, UnicodeError) as exc:
        return dict(error=_err(exc)), dict(error=_err(exc))
    if cfg is None:
        return dict(error="unreadable"), dict(error="unreadable")
    flags = {k: cfg.get(k) is True for k in TRIAL_FLAGS}  # the runtime treats only true as on
    flags.update({k: cfg.get(k) is True for k in TRIAL_OPTIONAL_FLAGS if k in cfg})
    provider = {name: safe_name(cfg.get(key)) for name, key in PROVIDER_KEYS.items()}
    provider["enabled"] = cfg.get("stardew_provider_enabled") is True
    return flags, provider


def watcher_state(paths, env):
    """Whether live_watcher.py would have a webhook: env first, then the private config file. Boolean only."""
    if (env.get(WEBHOOK_ENV) or "").strip():
        return dict(webhook_configured=True, basis="env")
    prep = getattr(paths, "PREP", None)
    config = env.get(WATCHER_CONFIG_ENV) or (Path(prep) / WATCHER_CONFIG_NAME if prep else None)
    if not config:
        return dict(webhook_configured=False, basis=None)
    try:
        value = json.loads(Path(config).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return dict(webhook_configured=False, basis=None)
    except (OSError, ValueError) as exc:
        return dict(webhook_configured=None, basis="config_file", error=_err(exc))
    url = value.get("webhook_url") if isinstance(value, dict) else None
    return dict(webhook_configured=isinstance(url, str) and bool(url.strip()), basis="config_file")


def host_recovery(paths, env):
    override = env.get(HOST_RECOVERY_ENV)
    if override:
        return Path(override)
    run = getattr(paths, "RUN", None)
    return Path(run) / "batch-host" if run else None


def _attr(paths, name):
    value = getattr(paths, name, None)
    return Path(value) if value else None


def identity_digest(manifest):
    """sha256 of what identifies the run (no timestamps, paths or history): changes iff the combination changes."""
    core = dict(source={k: manifest["source"].get(k) for k in ("sha", "dirty")},
                dlls={k: v.get("sha256") for k, v in manifest["dlls"].items()},
                configs={k: v.get("sha256") for k, v in manifest["configs"].items()},
                flags=manifest["flags"], provider=manifest["provider"],
                session={k: manifest["session"].get(k) for k in ("save_id", "save_name", "cycle_id")},
                watcher=manifest["watcher"].get("webhook_configured"))
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def build(*, cycle_dir, source_root, paths, state, phase, enrolled=None, previous=None, clock=time.time,
          env=None, hostname=None):
    """The manifest dict. Each section reads independently; a failed read records its error type only."""
    env = os.environ if env is None else env
    cycle_dir = Path(cycle_dir)
    state = state if isinstance(state, dict) else {}
    now = clock()
    mod_dir = _attr(paths, "MOD")
    recovery = host_recovery(paths, env)
    host_mod = recovery / "host-mods" / MOD_DIR if recovery else None
    fh_dll, _ = file_digest(mod_dir / MOD_DLL if mod_dir else None)
    host_dll, _ = file_digest(host_mod / MOD_DLL if host_mod else None)
    host_dll["recovery"] = str(recovery) if recovery else None
    host_dll["launch_record"] = bool(recovery and (recovery / "host-launch.json").is_file())
    fh_cfg, fh_raw = file_digest(_attr(paths, "MOD_CONFIG") or (mod_dir / "config.json" if mod_dir else None))
    trial_cfg, trial_raw = file_digest(_attr(paths, "TRIAL_CONFIG"))
    host_cfg, host_raw = file_digest(host_mod / "config.json" if host_mod else None)
    trial_flags, provider = trial_settings(trial_raw) if trial_raw is not None else (
        dict(error=trial_cfg.get("error")), dict(error=trial_cfg.get("error")))
    try:
        host = hostname if hostname is not None else socket.gethostname()
    except OSError as exc:
        host = "<" + _err(exc) + ">"
    enrolled = enrolled if isinstance(enrolled, dict) else {}
    manifest = dict(
        schema=SCHEMA, phase=phase, written_at=now,
        written_utc=datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        machine=dict(hostname=host),
        source=git_source(source_root),
        enrolled=dict(source=enrolled.get("source"), dll=enrolled.get("dll"), dirty=enrolled.get("dirty")),
        dlls=dict(farmhand=fh_dll, host=host_dll),
        configs=dict(farmhand_mod=fh_cfg, trial=trial_cfg, host_mod=host_cfg),
        flags=dict(trial=trial_flags,
                   farmhand_mod=mod_flags(fh_raw) if fh_raw is not None else dict(error=fh_cfg.get("error")),
                   host_mod=mod_flags(host_raw) if host_raw is not None else dict(error=host_cfg.get("error"))),
        provider=provider,
        session=dict(cycle_id=cycle_dir.name, cycle_dir=str(cycle_dir), save_id=state.get("save_id"),
                     save_name=state.get("save_name"), player_name=state.get("player_name"),
                     days=len(state.get("days") or []), status=state.get("status")),
        watcher=watcher_state(paths, env),
    )
    fh, hd = fh_dll.get("sha256"), host_dll.get("sha256")
    manifest["consistency"] = dict(
        source_matches_enrolled=(manifest["source"]["sha"] == enrolled["source"]
                                 if manifest["source"].get("sha") and enrolled.get("source") else None),
        farmhand_dll_matches_enrolled=(fh == enrolled["dll"] if fh and enrolled.get("dll") else None),
        host_dll_matches_farmhand=(hd == fh if hd and fh else None))
    digest = identity_digest(manifest)
    history = list((previous or {}).get("history") or []) if isinstance(previous, dict) else []
    history.append(dict(phase=phase, at=now, day=manifest["session"]["days"], digest=digest))
    history = history[-HISTORY_MAX:]
    digests = [h.get("digest") if isinstance(h, dict) else None for h in history]
    manifest.update(identity_digest=digest, history=history,
                    identity_changes=sum(a != b for a, b in zip(digests, digests[1:])))
    return manifest


def secret_findings(value, where="$"):
    """Paths of anything secret-looking: a secret-named key with a non-boolean value, or a URL/token-like string."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{where}.{k}"
            if SECRET_KEY.search(str(k)) and v is not None and not isinstance(v, bool):
                found.append(here)
            found += secret_findings(v, here)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found += secret_findings(v, f"{where}[{i}]")
    elif isinstance(value, str) and SECRET_VALUE.search(value):
        found.append(where)
    return found


def read(cycle_dir):
    try:
        value = json.loads(ipc.read_text(Path(cycle_dir) / FILE_NAME))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


def write(cycle_dir, manifest):
    """Atomic: a unique temp file in the same folder, fsync, then os.replace. Refuses secret-looking content."""
    found = secret_findings(manifest)
    if found:
        raise ManifestSecretError("secret-looking manifest fields: " + ", ".join(found[:5]))
    target = Path(cycle_dir) / FILE_NAME
    ipc.write_json(target, manifest, fsync=True)
    return target


def refresh(cycle_dir, **kwargs):
    """build + write, carrying the previous manifest's history forward. Raises; the caller tolerates."""
    manifest = build(cycle_dir=cycle_dir, previous=read(cycle_dir), **kwargs)
    write(cycle_dir, manifest)
    return manifest
