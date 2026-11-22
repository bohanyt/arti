"""Mabar bring-up preflight: catch blockers before the owner is told she is ready.

Each check prints one `OK` / `BLOCKER` / `WARN` line. A BLOCKER is a non-zero exit.
No launcher path is invented: her VTS is the second instance on port 8002.
"""
from __future__ import annotations

from collections import namedtuple
from pathlib import Path
import os
import shutil
import subprocess

Check = namedtuple("Check", "status name detail")

# live_cycle enroll refuses a dirty checkout ("clean source and installed DLL required").
REJOIN_REFUSAL = "clean source required"

# Commit headroom in GB (solo stream 2026-11-15). The 85% commit check stays; this sits beside it.
# Farmhand 3.0 = 1.5 steady + 1.5 launch burst. Reserve 3 GB on top of the next launch.
# solo = HOST + voice + farmhand before OBS/Window 1 (RELIABILITY_ANALYSIS §4a/4c).
LAUNCH_BUDGET_GB = dict(farmhand=3.0, host=1.5, voice=3.5, solo=8.0)
HEADROOM_RESERVE_GB = 3.0
PHYSICAL_MIN_GB = 6.0
HARD_HEADROOM_GB = 1.5  # stay-hard: stop launching anything, including our own tools
_HEADROOM_OMIT = object()
VTS_HOW = (
    "start the second VTS instance (Window 2) so its API listens on port {port}; "
    "this repo has no VTS launcher"
)
# After a direct start VTube Studio can be up with its API off. The one thing the operator must do by hand,
# said exactly.
VTS_OWNER_ACTION = (
    "OWNER: in VTube Studio Window 2 open Settings and switch the API (\"Start API\") ON at port {port}, then "
    "press Allow when ARTI asks for the plugin"
)
# A release checkout must carry what the bridge resolves next to its own code (livestream test 2026-11-17: no
# venv312 = Supertone fell back to edge-tts and her voice was silent; no soul file = "[recorded remark omitted]").
RUNTIME_PARTS = (
    ("venv", "venv/Scripts/python.exe", "the bridge interpreter"),
    ("venv312", "venv312/Scripts/python.exe", "Supertone, her voice"),
    ("ARTI_SOUL.md", "ARTI_SOUL.md", "her soul"),
)


# V9b (2026-11-18): private state a bridge run writes next to its own code. pkg110 stopped NOT READY on its privacy
# guard because the 11-17 livestream test had run the bridge from the package checkout package-checkout and left these
# there (moved out by hand to a private folder).
PRIVATE_STATE = ("ARTI_VIEWERS.md", "ARTI_MOOD_STATE.json", "vault", "arti-state/mind", "data/telemetry",
                 "data/streamlabs_events_sample.jsonl")
PRIVATE_GLOBS = ("vts_token_*.txt",)


def private_files(root):
    """{relative posix path: sha256} of every private-state file in this checkout. Names and hashes only."""
    import hashlib
    root, out, paths = Path(root), {}, []
    for rel in PRIVATE_STATE:
        p = root / rel
        if p.is_file():
            paths.append(p)
        elif p.is_dir():
            paths.extend(sorted(f for f in p.rglob("*") if f.is_file()))
    for pattern in PRIVATE_GLOBS:
        paths.extend(sorted(f for f in root.glob(pattern) if f.is_file()))
    for f in paths:
        out[f.relative_to(root).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def private_state_in(root):
    """The private-state paths present in this checkout (PRIVATE_STATE entries and token file names)."""
    root, found = Path(root), []
    for rel in PRIVATE_STATE:
        p = root / rel
        if p.is_file() or p.is_dir() and any(f.is_file() for f in p.rglob("*")):
            found.append(rel)
    for pattern in PRIVATE_GLOBS:
        found.extend(sorted(f.name for f in root.glob(pattern) if f.is_file()))
    return found


def runtime_missing(root):
    """The runtime parts the bridge needs next to its code that this checkout lacks, as (name, what) pairs."""
    root = Path(root)
    return [(name, what) for name, rel, what in RUNTIME_PARTS if not (root / rel).is_file()]


def parse_porcelain(text):
    """Git status --porcelain paths (modified, untracked, renamed targets)."""
    names = []
    for line in (text or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            names.append(path)
    return names


def git_porcelain(root):
    return subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=str(root), text=True, timeout=20,
    )


def git_ignored(root, rel):
    r = subprocess.run(
        ["git", "check-ignore", "-q", rel], cwd=str(root), timeout=20,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def vts_launcher(root):
    """A VTS starter checked in under this repo, if any. None: do not invent a path."""
    for rel in (
        "scripts/livepath/start_vts.ps1",
        "scripts/livepath/start_vts.bat",
        "scripts/start_vts.ps1",
        "scripts/start_vts.bat",
        "start_vts.ps1",
        "start_vts.bat",
    ):
        path = Path(root) / rel
        if path.is_file():
            return path
    return None


def evaluate_checks(
    *,
    dirty_names,
    scratch_outside,
    scratch_path,
    tmp_ignored,
    vts_ok,
    vts_port,
    no_vts,
    launcher,
    embed,
    watcher_present,
    alert_keys,
    alerts_enabled,
    dll_ok,
    dll_detail,
    memory_pct=None,
    disk_free_gib=None,
    voice_supply=None,
    api_voice=False,
    skip_memory=False,
    memory_snapshot=_HEADROOM_OMIT,
    launch="farmhand",
    runtime_gaps=None,
    vts_window1_ok=None,
    private_state=None,
):
    """Pure verdicts. `alert_keys` is [(ENV_NAME, length_or_0), ...]. `embed` from probe_embedding_endpoints.

    memory_pct: committed/limit percent or None (UNKNOWN). WARN >65, BLOCKER >85 (the same check gates a mid-stream rejoin; the machine hung at 86 on 2026-11-10).
    disk_free_gib: C: free GiB or None (UNKNOWN). WARN under 15.5.
    voice_supply: Codex weekly parse dict, None if unused, or {"error": ...} / unreadable → UNKNOWN.
    skip_memory: a rejoin with both games up skips every memory gate (percent and GB).
    memory_snapshot: injected GB snapshot (tests never read the machine). Omitted: no headroom line
    (old callers). None: WARN UNKNOWN. A dict: the GB rule beside the 85% check.
    """
    checks = []
    if runtime_gaps is not None:
        if runtime_gaps:
            checks.append(Check(
                "BLOCKER", "runtime",
                "this checkout lacks %s; run `mabar link-runtime` (links them from the runtime checkout; `restore` "
                "removes them)" % ", ".join("%s (%s)" % (name, what) for name, what in runtime_gaps)))
        else:
            checks.append(Check("OK", "runtime", "venv, venv312 and ARTI_SOUL.md present"))
    if private_state:
        checks.append(Check(
            "WARN", "private_state",
            "this checkout holds private state: %s; `mabar restore` moves what the run wrote into the run folder, "
            "and a package build refuses while any of it is here" % ", ".join(private_state)))
    dirty = [str(n) for n in (dirty_names or ()) if str(n).strip()]
    if dirty:
        checks.append(Check(
            "BLOCKER", "source",
            "untracked/modified %s (%s); commit, ignore, or move them" % (
                ", ".join(dirty[:8]), REJOIN_REFUSAL),
        ))
    else:
        checks.append(Check("OK", "source", "clean (rejoin will not refuse the tree)"))

    if scratch_outside:
        scratch_line = "%s is outside the tree" % scratch_path
    else:
        scratch_line = "%s is inside the tree; tools must write outside or gitignore it" % scratch_path
    if tmp_ignored:
        scratch_line += "; .tmp/ is gitignored"
        scratch_status = "OK" if scratch_outside else "BLOCKER"
    else:
        scratch_line += "; .tmp/ is not gitignored (an untracked .tmp/ blocks rejoin)"
        scratch_status = "BLOCKER" if not scratch_outside else "WARN"
    if not scratch_outside:
        scratch_status = "BLOCKER"
    checks.append(Check(scratch_status, "scratch", scratch_line))

    if no_vts:
        checks.append(Check("WARN", "vts", "skipped (--no-vts)"))
    elif vts_ok:
        checks.append(Check("OK", "vts", "her VTS reachable on port %s (Window 2)" % vts_port))
    else:
        how = VTS_HOW.format(port=vts_port)
        if launcher:
            how += "; launcher: %s" % launcher
        if vts_window1_ok is not None:
            how += "; Window 1 API (8001) %s" % ("answers" if vts_window1_ok else "is off too")
            how += ". " + VTS_OWNER_ACTION.format(port=vts_port)
        checks.append(Check("BLOCKER", "vts", "her VTS (Window 2, port %s) is not reachable; %s" % (vts_port, how)))

    using = (embed or {}).get("using")
    fallback = (embed or {}).get("fallback") or ""
    if (embed or {}).get("ok") and using == "fallback":
        checks.append(Check(
            "WARN", "embed",
            "primary unreachable, using fallback %s" % ((embed or {}).get("base") or fallback),
        ))
    elif (embed or {}).get("ok"):
        detail = "%s answers" % (using or "primary")
        if not fallback:
            detail += "; no fallback configured (preflight WARN if the primary is remote)"
            checks.append(Check("WARN" if "127.0.0.1" not in str((embed or {}).get("base") or "") else "OK",
                                "embed", detail))
        else:
            checks.append(Check("OK", "embed", detail))
    else:
        checks.append(Check(
            "BLOCKER", "embed",
            "primary and fallback unreachable; start local LM Studio on 127.0.0.1:1234 or the configured host",
        ))

    if watcher_present:
        checks.append(Check("OK", "watcher", "ARTI_VIDEO_GEMINI_API_KEY present"))
    elif alerts_enabled:
        checks.append(Check(
            "BLOCKER", "watcher",
            "ARTI_VIDEO_GEMINI_API_KEY missing; set GEMINI_API_KEY (name only)",
        ))
    else:
        checks.append(Check("WARN", "watcher", "ARTI_VIDEO_GEMINI_API_KEY missing (alerts off)"))

    parts, any_present = [], False
    for name, length in alert_keys or ():
        if length:
            parts.append("%s len=%d" % (name, length))
            any_present = True
        else:
            parts.append("%s missing" % name)
    alert_detail = ", ".join(parts) if parts else "no provider keys configured"
    if alerts_enabled and not any_present:
        checks.append(Check("BLOCKER", "alerts", alert_detail + "; alerts route has no provider"))
    elif not any_present:
        checks.append(Check("WARN", "alerts", alert_detail))
    elif any(length == 0 for _, length in (alert_keys or ())):
        checks.append(Check("WARN", "alerts", alert_detail))
    else:
        checks.append(Check("OK", "alerts", alert_detail))

    if dll_ok:
        checks.append(Check("OK", "dll", dll_detail or "installed hash matches booking"))
    else:
        checks.append(Check("BLOCKER", "dll", dll_detail or "installed DLL does not match booking dllSha256"))

    if skip_memory:
        checks.append(Check("OK", "memory", "skipped (both games up; rejoin launches nothing)"))
    elif memory_pct is None:
        checks.append(Check("WARN", "memory", "UNKNOWN commit percent"))
    else:
        pct = float(memory_pct)
        override = os.environ.get("ARTI_PREFLIGHT_MEMORY_OVERRIDE", "").strip()
        if pct > 85 and override:
            # Per-run, explicit and logged: commit % counts the pagefile limit, not physical RAM (CT 2026-11-15).
            checks.append(Check("WARN", "memory", "commit %.0f%% above 85, BLOCKER overridden: %s" % (pct, override[:160])))
        elif pct > 85:
            checks.append(Check("BLOCKER", "memory", "commit %.0f%% (blocker above 85)" % pct))
        elif pct > 65:
            checks.append(Check("WARN", "memory", "commit %.0f%% (warn above 65)" % pct))
        else:
            checks.append(Check("OK", "memory", "commit %.0f%%" % pct))

    if skip_memory:
        pass
    elif memory_snapshot is _HEADROOM_OMIT:
        pass
    else:
        checks.append(headroom_check(memory_snapshot, launch or "farmhand"))

    if disk_free_gib is None:
        checks.append(Check("WARN", "disk", "UNKNOWN C: free GiB"))
    else:
        free = float(disk_free_gib)
        detail = "C: %.1f GiB free" % free
        if free < 15.5:
            checks.append(Check("WARN", "disk", detail + " (warn under 15.5)"))
        else:
            checks.append(Check("OK", "disk", detail))

    if not api_voice:
        if not isinstance(voice_supply, dict) or voice_supply.get("error") or "left_percent" not in voice_supply:
            why = (voice_supply or {}).get("error") if isinstance(voice_supply, dict) else "unreadable"
            checks.append(Check("WARN", "voice_supply", "UNKNOWN Codex weekly share (%s)" % why))
        else:
            checks.append(Check(
                "WARN", "voice_supply",
                "Codex weekly share left %.0f%% (used %.0f%%)" % (
                    float(voice_supply["left_percent"]), float(voice_supply.get("used_percent") or 0)),
            ))
    return checks


def format_check(check):
    return "%s %s: %s" % (check.status, check.name, check.detail)


def blocked(checks):
    return any(c.status == "BLOCKER" for c in checks)


def projected_limit_gb(snapshot):
    """RAM + pagefile max, so a growing pagefile is not read as relief."""
    if not snapshot:
        return None
    ram = snapshot.get("ram_gb")
    page = snapshot.get("pagefile_max_gb")
    if ram is None or page is None:
        return None
    return float(ram) + float(page)


def headroom_gb(snapshot):
    if not snapshot:
        return None
    limit = snapshot.get("limit_now_gb")
    committed = snapshot.get("committed_gb")
    if limit is None or committed is None:
        return None
    return float(limit) - float(committed)


def headroom_check(snapshot, launch="farmhand"):
    """(limit_now − committed) ≥ budget(next launch) + 3 GB, and available_physical ≥ 6 GB."""
    launch = launch if launch in LAUNCH_BUDGET_GB else "farmhand"
    budget = LAUNCH_BUDGET_GB[launch]
    need = budget + HEADROOM_RESERVE_GB
    if not snapshot:
        return Check("WARN", "headroom", "UNKNOWN commit headroom")
    have = headroom_gb(snapshot)
    phys = snapshot.get("available_physical_gb")
    projected = projected_limit_gb(snapshot)
    parts = []
    if have is None:
        return Check("WARN", "headroom", "UNKNOWN commit headroom")
    parts.append("headroom %.1f GB (need %.1f = %s %.1f + %.1f)" % (
        have, need, launch, budget, HEADROOM_RESERVE_GB))
    if projected is not None:
        parts.append("projected limit %.1f GB (RAM %.1f + pagefile_max %.1f)" % (
            projected, float(snapshot.get("ram_gb") or 0), float(snapshot.get("pagefile_max_gb") or 0)))
    else:
        parts.append("projected limit UNKNOWN")
    if phys is None:
        parts.append("physical UNKNOWN")
        return Check("WARN", "headroom", "; ".join(parts))
    parts.append("physical %.1f GB" % float(phys))
    short = have < need or float(phys) < PHYSICAL_MIN_GB
    detail = "; ".join(parts)
    if short:
        return Check("BLOCKER", "headroom", "headroom_short:%.1f/%.1fGB; %s" % (need, have, detail))
    return Check("OK", "headroom", detail)


def run_preflight(a, *, root, print_fn=print, porcelain_fn=None, ignored_fn=None,
                  scratch_fn=None, vts_fn=None, launcher_fn=None, embed_fn=None,
                  watcher_fn=None, alert_fn=None, dll_fn=None, env=None,
                  memory_fn=None, disk_fn=None, voice_supply_fn=None,
                  memory_snapshot_fn=None, skip_memory=False, launch=None, runtime_fn=None,
                  vts_window1_fn=None, private_fn=None):
    """Gather + print + return 1 on BLOCKER. Every gather fn is injectable (tests: fakes only)."""
    env = env if env is not None else os.environ
    root = Path(root)
    try:
        dirty = parse_porcelain((porcelain_fn or git_porcelain)(root))
    except (OSError, subprocess.SubprocessError) as exc:
        dirty = ["git-status-failed:%s" % type(exc).__name__]

    scratch_path, scratch_outside = "unknown", False
    try:
        path = (scratch_fn or _default_scratch)(root, env)
        scratch_path = str(path)
        resolved, base = Path(path).resolve(), root.resolve()
        scratch_outside = resolved != base and base not in resolved.parents
    except (RuntimeError, KeyError, OSError, TypeError) as exc:
        scratch_path, scratch_outside = type(exc).__name__, False

    tmp_ignored = bool((ignored_fn or git_ignored)(root, ".tmp"))
    no_vts = bool(getattr(a, "no_vts", False))
    vts_port = int(getattr(a, "vts_port", 8002) or 8002)
    vts_ok = True if no_vts else bool((vts_fn or (lambda: False))())
    launcher = (launcher_fn or vts_launcher)(root)
    embed = (embed_fn or (lambda: dict(ok=False)))()
    watcher_present = bool((watcher_fn or (lambda: None))())
    alert_keys = list((alert_fn or (lambda: ()))())
    alerts_enabled = not bool(getattr(a, "no_alerts", False))
    dll_ok, dll_detail = (dll_fn or (lambda: (False, "booking/DLL unread")))()
    api_voice = bool(getattr(a, "api_voice", None))
    skip_memory = bool(skip_memory or getattr(a, "skip_memory", False))
    launch = launch or getattr(a, "launch", None) or "farmhand"
    snapshot = _HEADROOM_OMIT
    memory_pct = None
    if not skip_memory:
        # Injected memory_fn: tests must never read the machine. Production (both None)
        # takes one snapshot and derives the percent from it.
        if memory_snapshot_fn is not None:
            try:
                snapshot = memory_snapshot_fn()
            except Exception:
                snapshot = None
        elif memory_fn is None:
            try:
                snapshot = memory_snapshot()
            except Exception:
                snapshot = None
        else:
            snapshot = None
        if memory_fn is not None:
            try:
                memory_pct = memory_fn()
            except Exception:
                memory_pct = None
        elif snapshot:
            limit = snapshot.get("limit_now_gb") or 0
            memory_pct = (100.0 * float(snapshot["committed_gb"]) / float(limit)) if limit else None
        else:
            try:
                memory_pct = memory_commit_percent()
            except Exception:
                memory_pct = None
    try:
        disk_free_gib = (disk_fn or _default_disk_free)()
    except Exception:
        disk_free_gib = None
    voice_supply = None
    if not api_voice:
        try:
            voice_supply = (voice_supply_fn or default_voice_supply)()
        except Exception:
            voice_supply = dict(error="unreadable")

    checks = evaluate_checks(
        dirty_names=dirty, scratch_outside=scratch_outside, scratch_path=scratch_path,
        tmp_ignored=tmp_ignored, vts_ok=vts_ok, vts_port=vts_port, no_vts=no_vts,
        launcher=launcher, embed=embed, watcher_present=watcher_present,
        alert_keys=alert_keys, alerts_enabled=alerts_enabled, dll_ok=dll_ok,
        dll_detail=dll_detail, memory_pct=memory_pct, disk_free_gib=disk_free_gib,
        voice_supply=voice_supply, api_voice=api_voice,
        skip_memory=skip_memory, memory_snapshot=snapshot, launch=launch,
        runtime_gaps=None if runtime_fn is None else list(runtime_fn()),
        vts_window1_ok=None if (vts_window1_fn is None or no_vts or vts_ok) else bool(vts_window1_fn()),
        private_state=None if private_fn is None else list(private_fn()),
    )
    for check in checks:
        print_fn(format_check(check))
    return 1 if blocked(checks) else 0


def _default_scratch(root, env):
    from scripts.livepath import rehearse
    return rehearse.scratch_path("preflight", env, root=root)


def _pagefile_sizes_gb():
    """(initial_gb, max_gb) from the paging-files registry value, or (None, None)."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
        ) as key:
            vals, _ = winreg.QueryValueEx(key, "PagingFiles")
        if isinstance(vals, str):
            vals = [vals]
        initial_mb = max_mb = 0
        for line in vals or ():
            nums = [int(p) for p in str(line).split()[1:] if p.isdigit()]
            if len(nums) >= 2:
                initial_mb += nums[0]
                max_mb += nums[1]
            elif len(nums) == 1:
                initial_mb += nums[0]
                max_mb += nums[0]
        return (
            (initial_mb / 1024.0) if initial_mb else None,
            (max_mb / 1024.0) if max_mb else None,
        )
    except Exception:
        return None, None


def _pagefile_max_gb():
    """Sum of pagefile maximum sizes (GB) from the paging-files registry value, or None."""
    _initial, maximum = _pagefile_sizes_gb()
    return maximum


def memory_snapshot():
    """Committed / limit / physical / RAM / pagefile max in GB. None if unreadable.

    Tests inject a dict of the same shape and never call this.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class Performance(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD)] + [(k, ctypes.c_size_t) for k in
                ("CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
                 "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
                (k, wintypes.DWORD) for k in ("HandleCount", "ProcessCount", "ThreadCount")]

        info = Performance()
        info.cb = ctypes.sizeof(info)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
        page = float(info.PageSize or 0)
        if page <= 0 or info.CommitLimit <= 0:
            return None
        gb = 1024.0 ** 3
        initial_gb, max_gb = _pagefile_sizes_gb()
        return dict(
            committed_gb=info.CommitTotal * page / gb,
            limit_now_gb=info.CommitLimit * page / gb,
            available_physical_gb=info.PhysicalAvailable * page / gb,
            ram_gb=info.PhysicalTotal * page / gb,
            pagefile_max_gb=max_gb,
            pagefile_initial_gb=initial_gb,
        )
    except Exception:
        return None


def memory_commit_percent():
    """Windows committed bytes / commit limit as a percent, or None if unreadable."""
    snap = memory_snapshot()
    if not snap or not snap.get("limit_now_gb"):
        return None
    return 100.0 * float(snap["committed_gb"]) / float(snap["limit_now_gb"])


def _default_disk_free():
    return shutil.disk_usage("C:\\").free / 1024**3


def default_voice_supply():
    from scripts.livepath.codex_usage import read_codex_weekly
    return read_codex_weekly()
