"""mabar solo: one background bring-up whose READY line needs real play (§4a).

The CLI detaches a worker so the caller is never blocked for minutes. Tests call
run_solo() / hand_chat_id() / poll helpers with fakes; they never launch a game,
OBS, VTS, or the voice bridge.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts.livepath import bringup, mabar
from scripts.livepath.bringup import headroom_check, headroom_gb

CHAT_ID_FILE = "youtube-video-id.txt"
# The solo stream voice (passive mic + own-play lines). The voice step itself turns passive off
# when her voice is not on headphones (audio_route). API voice stays off.
# Alert yield stays a switch: it is passed through only when the operator gives --alert-yield
# (the rehearsal does; a stream does after it passed).
SOLO_VOICE_FLAGS = ("--passive", "--own-play")


def decision_cap_args(a):
    """ADR 164: her game API budget reaches every process that writes trial-config.json. That is the solo worker, its
    `play` step, and the supervisor, whose rejoin runs play again. Before, none of them was given
    `--decision-cap-usd`, so each wrote the default whatever the operator typed. Only a cap other than the default is
    passed on."""
    cap = getattr(a, "decision_cap_usd", None)
    return [] if cap is None or cap == mabar.DECISION_CAP_USD else ["--decision-cap-usd", repr(float(cap))]


def supervise_args(a):
    """The supervisor needs her name and the farm: its rejoin joins her farmhand by name, its HOST reopen continues
    the farm (2026-11-18: without them its first game recovery raised and ended the supervisor). It also needs her
    decision cap, because its rejoin writes trial-config.json again (decision_cap_args)."""
    return [*(["--name", a.name] if getattr(a, "name", None) else []),
            *(["--farm", a.continue_save] if getattr(a, "continue_save", None) else []),
            *decision_cap_args(a)]


def solo_voice_args(video_id=None, alert_yield=False):
    """Arguments for the `voice` step that mabar solo starts."""
    return [*SOLO_VOICE_FLAGS, *(["--alert-yield"] if alert_yield else []),
            *(["--youtube-video-id", video_id] if video_id else [])]
SOLO_LOG = "solo.log"
LOCK_NAME = "solo.pid"
PAGEFILE_MIN_GB = 12.0
DISK_FLOOR_GIB = 15.5
HOST_WAIT_S = 180
JOIN_WAIT_S = 120
PLAY_WAIT_S = 90
HELPER_WATCH_S = 30
VOICE_WAIT_S = 90
PLAYING_STATUSES = ("ready", "active", "running")


class SoloRefused(Exception):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def video_id_of(a):
    return (getattr(a, "video_id", None) or getattr(a, "youtube_video_id", None) or "") or ""


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def solo_log(run, msg, *, now=None, clock_fn=None):
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H:%M:%S", time.localtime((now if now is not None else (clock_fn or time.time)())))
    line = "%s %s" % (stamp, msg)
    with (run / SOLO_LOG).open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print("[solo] " + line, flush=True)
    return line


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() in (5, 6)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def lock_path(runs_root=None):
    return Path(runs_root or (mabar.RIG / "mabar-runs")) / LOCK_NAME


def read_lock(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(pid=None, raw=True)


def acquire_lock(path, pid, run, *, alive_fn=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    alive_fn = alive_fn or pid_alive
    row = dict(pid=int(pid), run=str(run), utc=utc())
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = read_lock(path) or {}
        other = existing.get("pid")
        if other and alive_fn(other):
            raise SoloRefused("another_run_active")
        try:
            path.unlink()
        except OSError:
            raise SoloRefused("another_run_active")
        return acquire_lock(path, pid, run, alive_fn=alive_fn)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return row


def release_lock(path, pid):
    path = Path(path)
    existing = read_lock(path) or {}
    if existing.get("pid") in (None, int(pid)):
        try:
            path.unlink()
        except OSError:
            pass


def new_run_folder(runs_root, *, when=None):
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(when if when is not None else time.time()))
    base = Path(runs_root) / ("mabar-" + stamp)
    if not base.exists():
        return base
    for suffix in "bcdefghijklmnopqrstuvwxyz":
        cand = Path(str(base) + suffix)
        if not cand.exists():
            return cand
    return Path(str(base) + "-" + str(os.getpid()))


def booking_source_sha(booking, *, read_fn=None):
    if booking.get("sourceSha") or booking.get("source_sha"):
        return str(booking.get("sourceSha") or booking.get("source_sha"))
    path = booking.get("provenance")
    if not path:
        return ""
    if isinstance(path, dict):
        return str(path.get("source_sha") or "")
    try:
        data = (read_fn or mabar.soak.read)(path)
    except (OSError, ValueError, TypeError, KeyError):
        return ""
    return str((data or {}).get("source_sha") or "")


def checkout_refusal(root, booking, *, head="", branch="", dirty=(), release_path=None):
    dirty = [str(n) for n in (dirty or ()) if str(n).strip()]
    if dirty:
        return "checkout_dirty:" + ",".join(dirty[:8])
    want = (booking_source_sha(booking) or "").strip().lower()[:40]
    got = (head or "").strip().lower()[:40]
    if want and got != want:
        return "checkout_not_release"
    if (branch or "").startswith("agent/"):
        return "checkout_not_release"
    rel = release_path or booking.get("releaseWorktree") or booking.get("release_worktree")
    if rel and Path(rel).resolve() != Path(root).resolve():
        return "checkout_not_release"
    return None


def git_head(root):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=20).strip()


def git_branch(root):
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root), text=True, timeout=20
    ).strip()


def reboot_pending():
    try:
        import winreg
    except ImportError:
        return False
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"),
    )
    for hive, path in keys:
        try:
            key = winreg.OpenKey(hive, path)
            winreg.CloseKey(key)
            return True
        except OSError:
            continue
    return False


def machine_refusal(snapshot, *, disk=None, reboot=False, launch="solo", pagefile_gb=None):
    if reboot:
        return "reboot_pending"
    initial = pagefile_gb
    if initial is None and snapshot:
        initial = snapshot.get("pagefile_initial_gb")
    try:
        initial = float(initial)
    except (TypeError, ValueError):
        initial = None
    if initial is None or initial < PAGEFILE_MIN_GB:
        return "pagefile_small"
    try:
        disk = None if disk is None else float(disk)
    except (TypeError, ValueError):
        disk = None
    if disk is None or disk < DISK_FLOOR_GIB:
        return "disk_floor"
    check = headroom_check(snapshot, launch)
    if check.status == "BLOCKER":
        return check.detail.split(";")[0].strip()
    return None


def owner_vts_count(*, processes_fn=None):
    names = (processes_fn or mabar.processes)()
    return sum(1 for n in names if "vtube" in n.lower() and "studio" in n.lower())


def vts2_probe(*, port=None, reachable_fn=None, token_fn=None, processes_fn=None):
    port = int(port or getattr(mabar, "VTS_PORT", 8002))
    reachable = bool((reachable_fn or (lambda: mabar.vts_reachable(port)))())
    token = None
    if token_fn:
        token = token_fn()
    else:
        try:
            import arti_vts_auth
            token = arti_vts_auth.read_token(str(mabar.ROOT / "arti_bridge.py"), port, main_port=8001)
        except Exception:
            token = None
    token_ok = bool(token)
    return dict(
        ok=reachable, token_ok=token_ok, model=reachable and token_ok,
        owner_windows=owner_vts_count(processes_fn=processes_fn),
    )


def host_snapshot(run):
    run = Path(run)
    startup = {}
    if (run / "host" / "startup.json").is_file():
        try:
            startup = json.loads((run / "host" / "startup.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            startup = {}
    status = {}
    if (run / "host" / "status.json").is_file():
        try:
            status = json.loads((run / "host" / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
    facts = status.get("facts") if isinstance(status.get("facts"), dict) else {}
    location = str(facts.get("location") or "")
    from scripts.livepath.mabar_supervise import last_helper_stop
    return startup.get("state"), location, last_helper_stop(run)


def last_world_effect(run, *, now=None):
    """(age_seconds, cycle_name) of the newest observed world-effect action, or None."""
    run = Path(run)
    now = time.time() if now is None else now
    cycle = mabar.current_cycle(run)
    newest = None
    paths = [cycle / "cycle_events.jsonl"]
    paths.extend(cycle.glob("**/g1_journal.jsonl"))
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            action = row.get("action") if isinstance(row.get("action"), dict) else {}
            status = action.get("world_effect_status") or row.get("world_effect_status")
            if not (isinstance(status, str) and status.startswith("observed_")):
                continue
            ts = row.get("ts", row.get("at"))
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue  # no timestamp: never fresh (fail closed)
            if newest is None or ts >= newest[0]:
                newest = (ts, cycle.name)
    if newest is None:
        return None
    return max(0.0, now - newest[0]), newest[1]


def observed_world_effects(run):
    """(count, cycle_name) of observed world-effect rows in the current cycle. Journal rows carry no timestamp."""
    run = Path(run)
    cycle = mabar.current_cycle(run)
    count = 0
    paths = [cycle / "cycle_events.jsonl"]
    paths.extend(cycle.glob("**/g1_journal.jsonl"))
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            action = row.get("action") if isinstance(row.get("action"), dict) else {}
            status = action.get("world_effect_status") or row.get("world_effect_status")
            if isinstance(status, str) and status.startswith("observed_"):
                count += 1
    return count, cycle.name


class WorldEffectWatch:
    """Fresh play = the observed count grew AFTER the watch started. Old rows never count (fail closed)."""

    def __init__(self, run, *, count_fn=None):
        self.run = run
        self.count_fn = count_fn or observed_world_effects
        self.baseline = None
        self.last_count = None
        self.grew_at = None
        self.cycle = None

    def poll(self, now):
        try:
            count, cycle = self.count_fn(self.run)
        except Exception:
            return None
        if self.baseline is None or cycle != self.cycle:
            self.baseline, self.last_count, self.cycle, self.grew_at = count, count, cycle, None
            return None
        if count > self.last_count:
            self.grew_at = now
        self.last_count = count
        if self.grew_at is None:
            return None
        return max(0.0, now - self.grew_at), cycle


def chat_id_path(run):
    return Path(run) / CHAT_ID_FILE


def chat_id_of(run):
    run = Path(run)
    path = chat_id_path(run)
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text.splitlines()[0].strip()
    cfg = {}
    if (run / "rehearsal-config.json").is_file():
        try:
            cfg = json.loads((run / "rehearsal-config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    return str(cfg.get("youtube_video_id") or "") or ""


def status_extras(run):
    run = Path(run)
    parts = []
    log = run / SOLO_LOG
    if log.is_file():
        try:
            lines = [ln.strip() for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except OSError:
            lines = []
        if lines:
            parts.append("solo_last=" + lines[-1])
            parts.append("solo_steps=%d" % sum(1 for ln in lines if " step=" in ln or "READY_TO_GO_LIVE" in ln))
    vid = chat_id_of(run)
    if vid:
        parts.append("chat_id=" + vid)
    return " ".join(parts)


def write_chat_id(run, video_id):
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    vid = str(video_id or "").strip()
    chat_id_path(run).write_text(vid + "\n", encoding="utf-8")
    for name in ("rehearsal-config.json", "live_session.json"):
        path = run / name
        row = {}
        if path.is_file():
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                row = {}
        if not isinstance(row, dict):
            row = {}
        row["youtube_video_id"] = vid
        row["youtube_chat_enabled"] = bool(vid)
        path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return vid


def hand_chat_id(a):
    vid = video_id_of(a)
    if not vid:
        raise RuntimeError("chat needs --video-id")
    run = Path(a.run)
    write_chat_id(run, vid)
    mabar.log(run, "chat_id_sent", video_id=vid)
    print("chat_id=%s file=%s" % (vid, chat_id_path(run)), flush=True)
    return 0


def _spawn_flags():
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def spawn_mabar(step, a, extra=(), *, popen=None):
    cmd = [sys.executable, "-B", str(mabar.ROOT / "scripts/livepath/mabar.py"), step,
           "--run", str(a.run), "--spec", str(a.spec), *extra]
    log_path = Path(a.run) / ("%s.bg.log" % step)
    popen = popen or subprocess.Popen
    with log_path.open("ab") as console:
        return popen(cmd, cwd=str(mabar.ROOT), stdout=console, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, creationflags=_spawn_flags(), close_fds=True)


def spawn_worker(a, run, *, popen=None):
    extra = ["--solo-worker", "--run", str(run), "--spec", str(a.spec),
             "--owner-vts", getattr(a, "owner_vts", "keep") or "keep"]
    if getattr(a, "continue_save", None):
        extra += ["--farm", a.continue_save]
    if getattr(a, "alert_yield", False):
        extra += ["--alert-yield"]
    vid = video_id_of(a)
    if vid:
        extra += ["--video-id", vid]
    if getattr(a, "name", None):
        extra += ["--name", a.name]
    extra += decision_cap_args(a)
    cmd = [sys.executable, "-B", str(mabar.ROOT / "scripts/livepath/mabar.py"), "solo", *extra]
    popen = popen or subprocess.Popen
    log_path = Path(run) / "solo.worker.log"
    with log_path.open("ab") as console:
        return popen(cmd, cwd=str(mabar.ROOT), stdout=console, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, creationflags=_spawn_flags(), close_fds=True)


def start_solo(a, **hooks):
    """Caller-facing: create a run folder, detach the worker, return immediately."""
    farm = getattr(a, "continue_save", None)
    if not farm:
        print("solo needs --farm SAVE_FOLDER", flush=True)
        return 1
    if not getattr(a, "name", None):
        # Her farmhand name from the farm itself (a continued farm has no creation facts in a new run folder).
        hands = (hooks.get("farmhands_fn") or (lambda save: mabar.saved_farmhands(mabar.APPDATA / "Saves" / save)))(farm)
        if len(hands) != 1:
            print("solo needs --name: the farm has %d farmhands" % len(hands), flush=True)
            return 1
        a.name = hands[0]
    runs_root = Path(hooks.get("runs_root") or (mabar.RIG / "mabar-runs"))
    runs_root.mkdir(parents=True, exist_ok=True)
    lock = Path(hooks.get("lock_path") or (runs_root / LOCK_NAME))
    run = Path(hooks["run"]) if hooks.get("run") else new_run_folder(runs_root, when=hooks.get("when"))
    run.mkdir(parents=True, exist_ok=True)
    a.run = run
    existing = read_lock(lock) or {}
    alive_fn = hooks.get("alive_fn") or pid_alive
    if existing.get("pid") and alive_fn(existing["pid"]):
        solo_log(run, "REFUSED another_run_active", clock_fn=hooks.get("clock_fn"))
        print("REFUSED another_run_active", flush=True)
        return 1
    spawn = hooks.get("spawn_worker") or spawn_worker
    proc = spawn(a, run)
    pid = getattr(proc, "pid", None)
    solo_log(run, "step=0 lock detached pid=%s" % pid, clock_fn=hooks.get("clock_fn"))
    print("solo run=%s" % run, flush=True)
    return 0


def _wait_until(ok_fn, *, timeout, clock_fn, sleep_fn, interval=1.0):
    clock_fn = clock_fn or time.time
    sleep_fn = sleep_fn or time.sleep
    deadline = clock_fn() + float(timeout)
    while clock_fn() < deadline:
        if ok_fn():
            return True
        sleep_fn(interval)
    return bool(ok_fn())


def run_solo(a, **hooks):
    """Worker: steps 0-9. Every external effect is injectable."""
    run = Path(a.run)
    run.mkdir(parents=True, exist_ok=True)
    lock = Path(hooks.get("lock_path") or lock_path(hooks.get("runs_root")))
    clock_fn = hooks.pop("clock_fn", None) or time.time
    sleep_fn = hooks.pop("sleep_fn", None) or time.sleep
    launched = hooks.pop("launched", None)
    if launched is None:
        launched = []
    owned_lock = False
    try:
        acquire_lock(lock, os.getpid(), run, alive_fn=hooks.get("alive_fn"))
        owned_lock = True
        _run_steps(a, run, launched=launched, clock_fn=clock_fn, sleep_fn=sleep_fn, **hooks)
        return 0
    except SoloRefused as exc:
        solo_log(run, "REFUSED %s" % exc.code, clock_fn=clock_fn)
        print("REFUSED %s" % exc.code, flush=True)
        return 1
    finally:
        if owned_lock:
            release_lock(lock, os.getpid())


def _run_steps(a, run, *, launched, clock_fn, sleep_fn, **hooks):
    # The live profile and the run sheet, where the operator reads (solo.log), not only in voice.bg.log.
    for line in mabar.live_profile_lines():
        solo_log(run, "live " + line, clock_fn=clock_fn)
    for line in mabar.live_run_sheet_lines():
        solo_log(run, "live " + line, clock_fn=clock_fn)
    booking = hooks.get("booking")
    if booking is None:
        booking = mabar.rehearse.read_booking(a.spec)
    root = Path(hooks.get("root") or mabar.ROOT)

    dirty = hooks.get("dirty")
    if dirty is None:
        try:
            dirty = bringup.parse_porcelain((hooks.get("porcelain_fn") or bringup.git_porcelain)(root))
        except (OSError, subprocess.SubprocessError):
            dirty = ["git-status-failed"]
    head = hooks.get("head")
    if head is None:
        try:
            head = (hooks.get("head_fn") or git_head)(root)
        except (OSError, subprocess.SubprocessError):
            head = ""
    branch = hooks.get("branch")
    if branch is None:
        try:
            branch = (hooks.get("branch_fn") or git_branch)(root)
        except (OSError, subprocess.SubprocessError):
            branch = ""
    why = checkout_refusal(root, booking, head=head, branch=branch, dirty=dirty,
                           release_path=hooks.get("release_path"))
    solo_log(run, "step=0 lock checkout branch=%s" % (branch or "?"), clock_fn=clock_fn)
    if why:
        raise SoloRefused(why)

    snapshot = hooks.get("snapshot")
    if snapshot is None and hooks.get("snapshot_fn"):
        snapshot = hooks["snapshot_fn"]()
    elif snapshot is None:
        snapshot = bringup.memory_snapshot()
    disk = hooks.get("disk")
    if disk is None and hooks.get("disk_fn"):
        disk = hooks["disk_fn"]()
    elif disk is None:
        disk = bringup._default_disk_free()
    reboot = hooks.get("reboot")
    if reboot is None and hooks.get("reboot_fn"):
        reboot = hooks["reboot_fn"]()
    elif reboot is None:
        reboot = reboot_pending()
    why = machine_refusal(
        snapshot, disk=disk, reboot=reboot, launch="solo",
        pagefile_gb=hooks.get("pagefile_gb"),
    )
    have = headroom_gb(snapshot)
    commit = None
    if snapshot and snapshot.get("limit_now_gb"):
        commit = 100.0 * float(snapshot["committed_gb"]) / float(snapshot["limit_now_gb"])
    solo_log(run, "step=1 machine headroom=%.1fGB commit=%s" % (
        have if have is not None else -1,
        ("%.0f%%" % commit) if commit is not None else "?"), clock_fn=clock_fn)
    if why:
        raise SoloRefused(why)

    solo_log(run, "step=2 run %s" % run.name, clock_fn=clock_fn)

    def mark(name, fn):
        launched.append(name)
        return fn()

    backup_fn = hooks.get("backup_fn") or (lambda: mabar.backup(a) or True)
    try:
        mark("backup", backup_fn)
    except Exception as exc:
        if isinstance(exc, SoloRefused):
            raise
        raise SoloRefused("backup_unverified") from exc
    if not (run / "backup.json").is_file() and hooks.get("backup_fn") is None:
        raise SoloRefused("backup_unverified")
    if hooks.get("backup_ok") is False:
        raise SoloRefused("backup_unverified")
    a.continue_save = getattr(a, "continue_save", None)
    prepare_fn = hooks.get("prepare_fn") or (lambda: mabar.prepare(a))
    mark("prepare", prepare_fn)
    solo_log(run, "step=3 save backup+prepare --continue %s" % (a.continue_save or ""), clock_fn=clock_fn)

    vts = hooks.get("vts")
    if vts is None and hooks.get("vts_fn"):
        vts = hooks["vts_fn"]()
    elif vts is None:
        vts = vts2_probe(port=getattr(a, "vts_port", mabar.VTS_PORT),
                         reachable_fn=hooks.get("vts_reachable_fn"),
                         token_fn=hooks.get("vts_token_fn"),
                         processes_fn=hooks.get("processes_fn"))
    owner_mode = getattr(a, "owner_vts", "keep") or "keep"
    solo_log(run, "step=4 vts2 ok=%s token=%s owner_vts=%s n=%s" % (
        vts.get("ok"), vts.get("token_ok"), owner_mode, vts.get("owner_windows")), clock_fn=clock_fn)
    if not vts.get("ok"):
        raise SoloRefused("vts2_not_ready")
    if not vts.get("token_ok"):
        raise SoloRefused("vts_token_needs_allow")

    vid = video_id_of(a)
    voice_fn = hooks.get("voice_fn") or (lambda: spawn_mabar("voice", a, solo_voice_args(vid, bool(getattr(a, "alert_yield", False)))))
    mark("voice", voice_fn)

    def default_voice_ready():
        if not (run / "voice.json").is_file():
            return False, "no_pid"
        if not (hooks.get("pids_fn") or mabar.bridge_pids)():
            return False, "exited"
        return True, ""

    ready_fn = hooks.get("voice_ready_fn") or default_voice_ready
    voice_wait = hooks.get("voice_wait_s", VOICE_WAIT_S)

    def voice_ok():
        ok, _why = ready_fn()
        return bool(ok)

    if not _wait_until(voice_ok, timeout=voice_wait, clock_fn=clock_fn, sleep_fn=sleep_fn):
        _ok, why = ready_fn()
        raise SoloRefused("voice_not_ready:%s" % (why or "timeout"))
    solo_log(run, "step=5 voice ready chat=%s" % (vid or "off"), clock_fn=clock_fn)

    def default_host():
        a.helper = True
        return mabar.host(a)

    host_fn = hooks.get("host_fn") or default_host
    mark("host", host_fn)
    host_wait = hooks.get("host_wait_s", HOST_WAIT_S)
    host_state_fn = hooks.get("host_state_fn") or (lambda: host_snapshot(run))

    def host_held():
        state, location, stopped = host_state_fn()
        if stopped:
            raise SoloRefused("host_stopped:%s" % stopped)
        return state == "held" and "FarmHouse" in (location or "")

    try:
        held = _wait_until(host_held, timeout=host_wait, clock_fn=clock_fn, sleep_fn=sleep_fn)
    except SoloRefused:
        raise
    if not held:
        state, location, stopped = host_state_fn()
        if stopped:
            raise SoloRefused("host_stopped:%s" % stopped)
        raise SoloRefused("host_not_held")
    solo_log(run, "step=6 host held FarmHouse", clock_fn=clock_fn)

    farm_check = headroom_check(snapshot if hooks.get("farmhand_snapshot") is None else hooks["farmhand_snapshot"],
                                "farmhand")
    if hooks.get("farmhand_snapshot") is not None or hooks.get("refuse_farmhand_headroom"):
        snap7 = hooks.get("farmhand_snapshot") or snapshot
        why = machine_refusal(snap7, disk=disk, reboot=False, launch="farmhand",
                              pagefile_gb=hooks.get("pagefile_gb") or PAGEFILE_MIN_GB)
        if why and why.startswith("headroom_short"):
            raise SoloRefused(why)
    elif farm_check.status == "BLOCKER":
        raise SoloRefused(farm_check.detail.split(";")[0].strip())

    def default_join():
        a.focus_host = False
        return mabar.rejoin_farmhand(a, focus_host=False)

    join_fn = hooks.get("join_fn") or default_join
    from scripts.livepath.halt_authority import HandsHeld, RecoveryTimedOut, operator_stop
    from scripts.livepath import mabar_supervise as supervision
    from scripts.m1_operator.witness_run import RESTART_TIMEOUT_S

    startup_end = time.monotonic() + RESTART_TIMEOUT_S
    a.recovery_deadline = time.time() + RESTART_TIMEOUT_S
    voice_state = supervision.load_state(run)
    pids_fn = hooks.get("pids_fn") or (lambda: mabar.bridge_pids(timeout=10))
    restart_voice = hooks.get("start_voice_fn") or (lambda argv: spawn_mabar("voice", a, argv))

    def voice():
        now = clock_fn()
        supervision.service_voice(run, voice_state, clock_fn=lambda: now,
                                  log=lambda msg: supervision.log_line(run, msg, now=now),
                                  bridge_fn=pids_fn, start_voice_fn=restart_voice)

    def startup_wait(operation):
        if time.monotonic() >= startup_end:
            raise RecoveryTimedOut("solo_startup_deadline")
        return supervision.wait_for_recovery(operation, deadline=startup_end, voice=voice)

    launched.append("join")
    while True:
        try:
            voice()
            startup_wait(join_fn)
            break
        except HandsHeld as exc:
            if operator_stop(exc.decision):
                raise SoloRefused(exc.decision.reason) from None
            a.hands_held = exc.decision.held or getattr(a, "hands_held", None)
            sleep_fn(1)
        except SoloRefused:
            raise
        except Exception as exc:
            raise SoloRefused("join_timeout") from exc
    wait_ready = hooks.get("wait_ready_fn") or (lambda: mabar.tool(
        run, "witness_run.py",
        ["wait-ready", "--who", "farmhand", "--max-hour", "23", "--timeout", "120", "--any-screen"],  # ADR 241 C6
        "wait-ready", deadline=a.recovery_deadline))
    try:
        startup_wait(wait_ready)
    except Exception as exc:
        if isinstance(exc, SoloRefused):
            raise
        raise SoloRefused("join_timeout") from exc

    state, location, stopped = host_state_fn()
    if stopped:
        raise SoloRefused("helper_died:%s" % stopped)
    released = False
    if state == "held":
        release_fn = hooks.get("release_fn") or (lambda: mabar.release_helper_pause(run))
        try:
            startup_wait(release_fn)
        except RecoveryTimedOut as exc:
            raise SoloRefused("join_timeout") from exc
        released = True
        launched.append("release")
    else:
        solo_log(run, "step=7 release skipped state=%s" % state, clock_fn=clock_fn)
    helper_fn = hooks.get("helper_fn") or (lambda: host_state_fn()[2])
    watch = hooks.get("helper_watch_s", HELPER_WATCH_S)

    def helper_dead():
        voice()
        reason = helper_fn()
        if reason:
            raise SoloRefused("helper_died:%s" % reason)
        return False

    try:
        _wait_until(helper_dead, timeout=watch, clock_fn=clock_fn, sleep_fn=sleep_fn)
    except SoloRefused:
        raise
    solo_log(run, "step=7 join focus_host=0 released=%s" % int(released), clock_fn=clock_fn)

    def default_play():
        argv = decision_cap_args(a)
        if getattr(a, "recovery_expected", None):
            argv += ["--expected-native", json.dumps(a.recovery_expected)]
        return spawn_mabar("play", a, argv)

    play_fn = hooks.get("play_fn") or default_play
    mark("play", play_fn)
    play_wait = hooks.get("play_wait_s", PLAY_WAIT_S)
    cycle_fn = hooks.get("cycle_fn") or (lambda: (
        mabar.current_cycle(run).name,
        (mabar.cycle_status(mabar.current_cycle(run)) or {}).get("status"),
    ))
    watch = WorldEffectWatch(run)
    world_fn = hooks.get("world_fn") or (lambda: watch.poll(clock_fn()))
    playing = {"cycle": None, "status": None, "age": None}

    def play_ok():
        voice()
        name, status = cycle_fn()
        playing["cycle"], playing["status"] = name, status
        helper_reason = helper_fn()
        if helper_reason:
            raise SoloRefused("helper_died:%s" % helper_reason)
        if not pids_fn():
            return False  # give active voice supervision its re-observation window
        world = world_fn()
        if world:
            playing["age"] = world[0]
        return status in PLAYING_STATUSES and world is not None and world[0] <= 60

    while True:
        remaining = startup_end - time.monotonic()
        if remaining <= 0:
            raise SoloRefused("not_playing:startup_deadline")
        ok = _wait_until(play_ok, timeout=min(play_wait, remaining), clock_fn=clock_fn, sleep_fn=sleep_fn)
        if ok or not getattr(a, "recovery_expected", None):
            break
        from scripts.livepath import halt_authority as HA
        expected = None if HA.cycle_dirs(run) else a.recovery_expected
        decision = HA.check(mabar.inbox(), run, act="observe", recovery=True, expected=expected,
                            held=getattr(a, "hands_held", None))
        if decision.action != "hold":
            break
        if operator_stop(decision):
            raise SoloRefused(decision.reason)
        try:
            mabar.report_hold(run, decision, a)
        except HandsHeld:
            sleep_fn(1)
    if not ok:
        status = playing["status"] or "none"
        raise SoloRefused("not_playing:%s" % status)
    solo_log(run, "step=8 play cycle=%s last_action=%.0fs" % (
        playing["cycle"] or "c?", playing["age"] if playing["age"] is not None else -1), clock_fn=clock_fn)

    ready = "READY_TO_GO_LIVE headroom=%.1fGB commit=%s cycle=%s last_action=%.0fs" % (
        have if have is not None else -1,
        ("%.0f%%" % commit) if commit is not None else "?",
        playing["cycle"] or "c?",
        playing["age"] if playing["age"] is not None else -1,
    )
    solo_log(run, "step=9 %s" % ready, clock_fn=clock_fn)
    print(ready, flush=True)
    supervision.save_state(run, voice_state)
    supervise_fn = hooks.get("supervise_fn") or (lambda: spawn_mabar("supervise", a, supervise_args(a)))
    mark("supervise", supervise_fn)
    return 0
