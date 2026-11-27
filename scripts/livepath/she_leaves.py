"""`mabar leave` — her play ends and she leaves the game; voice stays up."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import subprocess, sys, time

from arti_runtime import ipc
import arti_stardew_stop as stops
from scripts.livepath import halt_authority as HA, mabar, mabar_supervise, owner_join

PLAY_LEFT, CYCLE_WAIT_S, RETRY_AFTER_S, EXIT_WAIT_S, POLL_S = "play_left.json", 30.0, 10.0, 25.0, 0.25
TERMINAL_STATUSES = frozenset({"stopped", "safety_stop", "host_left", "not_recovered"})
_run_cmd, _clock, _sleep = subprocess.run, time.monotonic, time.sleep


def _run_allowed(run: Path):
    kind = HA.session_kind(run)
    if kind == "owner" and not HA.present(run / HA.HELPER_CONFIG):
        return "owner", None
    if kind == "solo":
        try:
            ok = (run / "SUPERVISE_STOP").is_file()
        except OSError:
            ok = False
        return ("solo", None) if ok else (None, "solo_requires_supervise_stop")
    return None, "run_host_kind_unknown"


def _read_play_left(run: Path):
    raw, r = HA.read_record(run / PLAY_LEFT)
    if raw is None: return "absent", None
    ok = (raw is not HA.UNREADABLE and isinstance(r, dict) and r.get("v") == 1
          and r.get("outcome") in ("left", "still_connected")
          and all(isinstance(r.get(k), str) and r[k] for k in ("run", "launch_id"))
          and all(type(r.get(k)) is int and r[k] > 0 for k in ("pid", "process_start"))
          and (r.get("cycle") is None or all(isinstance(r.get(k), str) for k in ("cycle", "cycle_dir"))))
    return ("unreadable", None) if not ok else (("ok", r) if HA._resolved(r["run"]) == HA._resolved(run) else ("foreign", r))


def _read_inbox():
    try: folder = Path(mabar.inbox())
    except (OSError, ValueError, TypeError, KeyError): return None, "inbox_unreadable"
    sr, sess = HA.read_record(folder / "session.json")
    if sr is HA.UNREADABLE or (sr is not None and not isinstance(sess, dict)): return None, "inbox_session_unreadable"
    return (None, "inbox_exit_unreadable") if HA._exit_record_malformed(*HA.read_record(folder / "mod_exit.json")) else (folder, None)


def _bind(run: Path, *, live=True, expected=None):
    raw, launch = HA.read_launch_record(run / owner_join.FARMHAND_LAUNCH)
    if raw is HA.UNREADABLE or raw is None or not isinstance(launch, dict):
        return None, "launch_record_unreadable" if raw is HA.UNREADABLE else "launch_record_missing"
    pid, started, lid = launch.get("pid"), launch.get("process_start"), launch.get("launch_id")
    if type(pid) is not int or pid <= 0 or type(started) is not int or started <= 0 or not isinstance(lid, str) or not lid:
        return None, "launch_record_incomplete"
    hr, hrow = HA.read_record(run / owner_join.HOST_LAUNCH)
    host_pid = hrow.get("pid") if isinstance(hrow, dict) else None
    if hr is HA.UNREADABLE or hr is None or type(host_pid) is not int or host_pid <= 0: return None, "host_record_unreadable"
    if pid == host_pid or (isinstance(launch.get("owner_host"), dict) and launch["owner_host"].get("pid") != host_pid):
        return None, "target_is_host" if pid == host_pid else "host_record_mismatch"
    host_hwnd = owner_join._hwnd(run / "runtime/host-hwnd.txt") if HA.present(run / "runtime/host-hwnd.txt") else 0
    hwnd = owner_join._hwnd(run / "runtime/fh-hwnd.txt")
    if host_hwnd is None or hwnd is None or (host_hwnd and hwnd == host_hwnd):
        return None, "host_hwnd_unreadable" if host_hwnd is None else ("target_hwnd_is_host" if hwnd is not None else "farmhand_hwnd_unreadable")
    b = dict(launch=launch, pid=pid, started=started, launch_id=lid, hwnd=hwnd, host_hwnd=host_hwnd or None)
    if not live: return b, None
    if expected and any(b[k] != expected[k] for k in ("launch_id", "pid", "started", "hwnd")): return None, "binding_changed"
    wf = owner_join.wf
    if wf.process_start(pid) != started: return None, "process_start_mismatch"
    if (wf.exe_name(pid) or "").casefold() != owner_join.SMAPI.casefold(): return None, "image_not_smapi"
    win = wf.window(hwnd) or {}
    if not win.get("exists") or win.get("pid") != pid: return None, "window_missing" if not win.get("exists") else "window_pid_mismatch"
    if host_hwnd and (hwin := wf.window(host_hwnd) or {}).get("exists") and hwin.get("pid") == pid: return None, "target_is_host"
    return dict(b, window=win), None


def _cycle_ended(cycle: Path):
    lock = cycle / "supervisor.lock"
    try:
        lpid, made = int(ipc.read_text(lock).strip()), lock.stat().st_mtime
        st = owner_join.wf.process_start(lpid)
        live = bool(st and owner_join.wf.filetime_ms(st) <= made * 1000 + 2000)
    except FileNotFoundError: live = False
    except (OSError, ValueError): live = True
    if not live: return "lock_cleared"
    _, state = HA.read_record(cycle / "cycle_state.json")
    status = state.get("status") if isinstance(state, dict) else None
    return ("terminal:" + status) if status in TERMINAL_STATUSES else None


def _prove_mod_exit(folder: Path, launch: dict):
    sr, sess = HA.read_record(folder / "session.json")
    er, ex = HA.read_record(folder / "mod_exit.json")
    if sr is HA.UNREADABLE or (sr is not None and not isinstance(sess, dict)) or HA._exit_record_malformed(er, ex): return "unreadable", None
    pub = HA.launch_published(launch, sess)
    if er is None: return "absent", (sess.get("runtime_instance_id") if pub else None)
    return (("confirmed", ex["runtime_id"]) if HA._clean_exit(sess, ex) else ("stale", sess.get("runtime_instance_id"))) if pub else ("unmatched", ex.get("runtime_id"))


def _wait_until(clock, sleep, deadline, pred):
    while not ((res := pred()) or clock() >= deadline):
        sleep(min(POLL_S, max(0.0, deadline - clock())))
    return res


def leave(a, *, run_fn=None, clock_fn=None, sleep_fn=None):
    """End her play and close her window cleanly; keep voice, chat, and voice supervision running."""
    run, wf = Path(a.run), owner_join.wf
    clock, sleep, run_sub = clock_fn or _clock, sleep_fn or _sleep, run_fn or _run_cmd
    t0 = clock()
    refuse = lambda r, d: (mabar.log(run, "leave_refused", reason=r, message=f"refused ({r}): {d}"), 2)[1]
    write_left = lambda **kw: ipc.write_json(run / PLAY_LEFT, dict(v=1, run=str(run.resolve()), utc=mabar.utc(), **kw), fsync=True)

    kind, err = _run_allowed(run)
    if err: return refuse(err, "owner-hosted runs only, or solo runs with SUPERVISE_STOP present.")
    pl_state, pl_rec = _read_play_left(run)
    if pl_state == "unreadable": return refuse("play_left_unreadable", "play_left.json cannot be read; nothing changed.")
    folder, inbox_err = _read_inbox()
    if inbox_err: return refuse(inbox_err, "Her mod inbox cannot be read; nothing changed.")
    base, base_err = _bind(run, live=False)
    if base_err: return refuse(base_err, "Her launch or HOST binding is unclear; nothing changed.")
    same = pl_state == "ok" and (pl_rec["launch_id"], pl_rec["pid"], pl_rec["process_start"]) == (base["launch_id"], base["pid"], base["started"])
    if same and wf.process_gone(base["pid"], base["started"]):
        mod_state, rid = _prove_mod_exit(folder, base["launch"])
        if mod_state == "unreadable": return refuse("inbox_unreadable", "Her inbox exit record cannot be read; nothing changed.")
        secs = round(max(0.0, clock() - t0), 2)
        if pl_rec["outcome"] != "left":
            write_left(cycle=pl_rec["cycle"], cycle_dir=pl_rec["cycle_dir"], launch_id=base["launch_id"], pid=base["pid"],
                       process_start=base["started"], hwnd=base["hwnd"], outcome="left", reason="gone_since_last_leave",
                       mod_exit=mod_state, runtime_id=rid, elapsed_s=secs)
        mabar.log(run, "leave_already_left", status="already_left", pid=base["pid"], hwnd=base["hwnd"], launch_id=base["launch_id"], cycle=pl_rec["cycle"],
                  seconds=secs, mod_exit=mod_state, runtime_id=rid, message=f"already_left (PID {base['pid']}): her play already ended and her game window is gone; voice stays up.")
        return 0
    bound, bind_err = _bind(run, live=True)
    if bind_err: return refuse(bind_err, "Her live game window does not match the recorded launch; nothing changed.")
    pid, started, hwnd, launch = bound["pid"], bound["started"], bound["hwnd"], bound["launch"]
    title = owner_join._title(bound["window"]) or "Stardew Valley"
    mabar.log(run, "leave_bound", kind=kind, pid=pid, hwnd=hwnd, launch_id=bound["launch_id"], title=title, repeat=same)
    cycle = (HA.cycle_dirs(run) or [None])[-1]
    has_cycle, leave_reason, wait_deadline = (cycle is not None and (cycle / "cycle_state.json").is_file()), "closed", clock()

    def run_tool(script, args, timeout):
        return run_sub([sys.executable, "-B", str(mabar.ROOT / "scripts/m1_operator" / script), *args], cwd=mabar.ROOT, env=mabar.env_for_tools(run), timeout=timeout)

    def alert_still(ev_name, lead, **kw):
        msg = f"still_connected: {lead} Close that window with its X; the owner's night can wait for her until then."
        mabar.log(run, ev_name, status="still_connected", pid=pid, hwnd=hwnd, title=title, message=msg, **kw)
        mabar_supervise.log_line(run, "ALERT " + msg)

    def send_close(attempt):
        nonlocal title
        if wf.process_gone(pid, started): return "gone"
        fresh, re_err = _bind(run, live=True, expected=bound)
        if re_err:
            if wf.process_gone(pid, started): return "gone"
            mabar.log(run, "leave_rebind_failed", attempt=attempt, reason=re_err, pid=pid, hwnd=hwnd)
            return "rebind_failed:" + re_err
        title = owner_join._title(fresh["window"]) or title
        try:
            res = run_tool("ui_op.py", ["close", str(hwnd), "--expect-pid", str(pid)], 15)
            mabar.log(run, "leave_close_sent", attempt=attempt, hwnd=hwnd, pid=pid, returncode=res.returncode)
            return "sent"
        except (OSError, subprocess.SubprocessError) as exc:
            mabar.log(run, "leave_close_failed", attempt=attempt, hwnd=hwnd, pid=pid, error=type(exc).__name__)
            return "close_failed"

    try:
        if not same:
            mabar.keep_smapi_logs(run, "leave")
            if has_cycle:
                try:
                    res = run_tool("live_cycle.py", ["stop", "--cycle-dir", str(cycle), "--yes"], 120)
                    mabar.log(run, "leave_play_stop_requested", cycle=cycle.name, returncode=res.returncode)
                except (OSError, subprocess.SubprocessError) as exc:
                    mabar.log(run, "leave_play_stop_failed", cycle=cycle.name, error=type(exc).__name__)
                why = _wait_until(clock, sleep, clock() + CYCLE_WAIT_S, lambda: _cycle_ended(cycle))
                mabar.log(run, "leave_cycle_ended" if why else "leave_cycle_wait_timeout", cycle=cycle.name, seconds=round(max(0.0, clock() - t0), 2),
                          **({"reason": why} if why else {"message": f"Cycle {cycle.name} did not end within {int(CYCLE_WAIT_S)}s; closing her window."}))
            else:
                mabar.log(run, "leave_no_cycle", reason="no_cycle")
        if wf.process_gone(pid, started):
            leave_reason = "process_exited_before_close"
            mabar.log(run, "leave_process_already_gone", phase="before_close", pid=pid)
        else:
            c0 = clock(); wait_deadline = c0 + EXIT_WAIT_S; s1 = send_close(1)
            if s1 == "gone": leave_reason, wait_deadline = "process_exited_before_close", clock()
            elif s1 == "sent":
                if not _wait_until(clock, sleep, c0 + RETRY_AFTER_S, lambda: wf.process_gone(pid, started)):
                    s2 = send_close(2)
                    leave_reason = {"gone": "closed_before_retry", "sent": "closed_on_retry", "rebind_failed:window_missing": "window_closed_before_retry"}.get(s2, s2)
            else:
                leave_reason, wait_deadline = s1, (wait_deadline if s1 == "rebind_failed:window_missing" else clock())
    except KeyboardInterrupt:
        alert_still("leave_cancelled", f'leave was cancelled; her window "{title}" (PID {pid}) is still connected.')
        return 3

    gone, secs = bool(_wait_until(clock, sleep, wait_deadline, lambda: wf.process_gone(pid, started))), round(max(0.0, clock() - t0), 2)
    rec_cycle, rec_cdir = (pl_rec["cycle"], pl_rec["cycle_dir"]) if same else ((cycle.name, str(cycle.resolve())) if has_cycle else (None, None))
    if gone:
        mod_state, rid = _prove_mod_exit(folder, launch)
        outcome, why = ("still_connected", "inbox_unreadable") if mod_state == "unreadable" else ("left", leave_reason)
    else:
        outcome, mod_state, rid, why = "still_connected", None, None, (leave_reason if leave_reason.startswith("rebind_failed:") else "exit_timeout")
        if (cur := wf.window(hwnd) or {}).get("exists") and cur.get("pid") == pid:
            title = owner_join._title(cur) or title
    if outcome == "left":
        mabar.log(run, "leave_left", status="left", pid=pid, hwnd=hwnd, launch_id=bound["launch_id"], cycle=rec_cycle, seconds=secs, reason=why,
                  mod_exit=mod_state, runtime_id=rid, message=f"left (PID {pid}, {secs:.1f}s, {why}): her play ended and her game window is gone; voice stays up.")
    else:
        alert_still("leave_still_connected", f'her window "{title}" (PID {pid}) is still up after {secs:.1f}s ({why}).',
                    launch_id=bound["launch_id"], cycle=rec_cycle, seconds=secs, reason=why, alert=True)
    write_left(cycle=rec_cycle, cycle_dir=rec_cdir, launch_id=bound["launch_id"], pid=pid, process_start=started,
               hwnd=hwnd, outcome=outcome, reason=why, mod_exit=mod_state, runtime_id=rid, elapsed_s=secs)
    return 0 if outcome == "left" else 3


def deliberate_voice_stop_after_leave(run, ev):
    """Point 7: only this run's own step-2 OPERATOR_STOP / scoped LIVE7_STOP do not veto voice restart."""
    run, (sr, sess), (hr, hdisk), (lr, launch) = Path(run), ev.session, ev.halt_file, ev.launch
    if (ev.stops or ev.host_inert or ev.token_present or HA._exit_record_malformed(*ev.mod_exit)
            or sr is HA.UNREADABLE or hr is HA.UNREADABLE or lr is HA.UNREADABLE or ev.archive_proof[0] is HA.UNREADABLE
            or not isinstance(launch, Mapping)
            or (sr is not None and (not isinstance(sess, Mapping) or not HA._complete(sess) or sess["record_faulted"] or HA.halt_of(sess) is not None))
            or (hr is not None and (HA._disk_key(hdisk) is None or hdisk.get("state") == "latched"))): return True
    pl_state, rec = _read_play_left(run)
    if pl_state != "ok" or any(rec[k] != launch.get(k) for k in ("launch_id", "pid", "process_start")): return True
    newest = ev.newest
    if rec["cycle"] is None: return newest is not None or bool(ev.outranking or ev.sentinels)
    if newest is None or rec["cycle"] != newest.name or HA._resolved(rec["cycle_dir"]) != HA._resolved(newest): return True
    _, state = ev.newest_state
    scope = state.get("stop_scope") if isinstance(state, Mapping) else None
    if (not isinstance(state, Mapping) or any(x != "cycle_stopped" for x in ev.outranking)
            or (state.get("status") == "stopped" and state.get("stop_reason"))
            or not isinstance(scope, Mapping) or not scope.get("runtime_instance_id") or not scope.get("session_id")): return True
    allowed = {HA._resolved(newest / "OPERATOR_STOP"), HA._resolved(newest / "LIVE7_STOP"), HA._resolved(run / "runtime/LIVE7_STOP")}
    if isinstance(state.get("live7_stop_file"), str) and state["live7_stop_file"]: allowed.add(HA._resolved(state["live7_stop_file"]))
    return any(mr is HA.UNREADABLE or not isinstance(rm, Mapping) or HA._resolved(p) not in allowed
               or rm.get("writer") != "supervisor.operator_stop" or not stops.scoped(rm)
               or not isinstance(rm.get("cycle_id"), str) or HA._resolved(rm["cycle_id"]) != HA._resolved(newest)
               or rm["runtime_instance_id"] != scope["runtime_instance_id"] or rm["session_id"] != scope["session_id"]
               for p, (mr, rm) in ev.sentinels.items())
