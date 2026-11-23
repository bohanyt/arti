"""mabar supervise: bounded auto-recovery for a solo stream (RELIABILITY_ANALYSIS §4b).

Long-running by design. Every 10 s it reads the newest cycle and acts. Tests call tick()
with fakes; they never launch a game, the bridge, or SMAPI.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from scripts.livepath.bringup import HARD_HEADROOM_GB, headroom_check, headroom_gb as snapshot_headroom
from scripts.livepath.halt_authority import HELPER_CONFIG, HandsHeld, session_kind  # the launch gate reads it too
from scripts.m1_operator.farmhand_focus import FOCUS_INDEPENDENT, release_farmhand_focus

POLL_S = 10
REOBSERVE_S = 10
MIN_REJOIN_GAP_S = 120
MAX_REJOINS_PER_HOUR = 6
FAIL_WINDOW_S = 15 * 60
FAIL_LIMIT = 3
MAX_HOST_RELAUNCH_PER_STREAM = 2
DISK_FLOOR_GIB = 15.0
MORNING_CLOCK = 630  # Game1.timeOfDay 6:30
VOICE_RESTART_WINDOW_S = 600   # RELIABILITY_ANALYSIS 4b: 3 voice restarts per 10 min, then ALERT and stop trying
VOICE_RESTART_LIMIT = 3
STOP_MARKERS = ("bridge-stop-request", "bridge-drained")
# ADR 237 C: the cycle drains a menu the runtime could not close (menu_held_recover); the supervisor recovers it this
# many times in this window, and the next one in the window stays hard for a human, with the menu's name.
MENU_RECOVER_LIMIT = 3
MENU_RECOVER_WINDOW_S = 30 * 60
# ADR 241 B: rung 5 (`effect_ladder_recover`, the cycle's drain) is recovered like hands_not_engaged. When a fresh cycle
# reaches rung 5 again within LADDER_WINDOW_S, rung 6 relaunches her game from the last saved morning (the clean
# restart below); a human is called only after rung 6 failed, with one alert naming every rung and its read-back.
EFFECT_LADDER_DRAIN = "effect_ladder_recover"
LADDER_WINDOW_S = 15 * 60
DRAINING_FAULTS = ("hands_not_engaged", "menu_held_recover", EFFECT_LADDER_DRAIN)
# ADR 241 C: a held-menu drain is on the same ladder: a second drain of either kind within LADDER_WINDOW_S is rung 6.
LADDER_DRAINS = ("menu_held_recover", EFFECT_LADDER_DRAIN)
HOUR_CAP_ALERT_S = 600  # ADR 241 C: a drain waiting this long on the rejoin hour cap is said once, as an ALERT
GAME_EXIT_WAIT_S = 120  # ADR 256: after its exiting answer, each game must be gone within this

SELF_HEAL_EXACT = {
    "continuation_not_ready",
    "hands_not_engaged",
    "menu_held",
    "menu_held_recover",
    EFFECT_LADDER_DRAIN,
    "terminal_cleanup_unconfirmed",
    "pad_watch_violation",
    "pad_watch_false_stop",
    "pad_watch",
}
HARD_EXACT = {
    "host_left",
    "operator_stopped",
    "stopped",
    "farmhand_identity_mismatch",
    "native_save_identity_changed",
    "identity_mismatch",
    "disk_floor",
    "headroom",
    "source_or_dll_drift",
}


def _read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")


def log_line(run, msg, *, now=None, clock_fn=None):
    """One action line in run/supervise.log, with the named reason."""
    run = Path(run)
    stamp = time.strftime("%H:%M:%S", time.localtime((now if now is not None else (clock_fn or time.time)())))
    line = "%s %s" % (stamp, msg)
    with (run / "supervise.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print("[supervise] " + line, flush=True)
    return line


def default_state():
    return dict(
        rejoin_at=[],
        failure_at=[],
        pending=None,
        helper_handled=None,
        host_relaunches=0,
        save_mtime=None,
        chat_only=False,
        last_fault=None,
        last_action=None,
        headroom_block_since=None,
        voice_down_since=None,
        voice_restart_at=[],
        voice_gave_up=False,
        voice_stop_logged=False,
        menu_recover_at=[],
        menu_gave_up=None,
        ladder_recover_at=[],
        ladder_relaunched_at=None,
        ladder_gave_up=None,
        ladder_log=[],
        hour_cap_since=None,
        hour_cap_alerted=False,
    )


def load_state(run):
    row = _read_json(Path(run) / "supervise_state.json") or {}
    base = default_state()
    base.update(row)
    return base


def save_state(run, state):
    _write_json(Path(run) / "supervise_state.json", state)


def newest_cycle(run):
    from scripts.livepath import mabar
    return mabar.current_cycle(run)


def cycle_stop(cycle, *, strict=False, state=None):
    """(status, reason) from cycle_state.json and cycle_events.jsonl."""
    cycle = Path(cycle)
    state = (state if strict else _read_json(cycle / "cycle_state.json")) or {}
    if not isinstance(state, dict):
        if strict:
            raise ValueError("cycle state is not an object")
        state = {}
    status = state.get("status")
    if state.get("stop_reason"):
        return status, str(state["stop_reason"])
    reason = None
    events = cycle / "cycle_events.jsonl"
    if strict or events.is_file():
        try:
            if strict:
                from arti_runtime import ipc
                lines = ipc.read_bytes(events).decode("utf-8-sig").splitlines()
            else:
                lines = events.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            lines = []
        except OSError:
            if strict:
                raise
            lines = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                if strict:
                    raise
                continue
            if not isinstance(row, dict):
                if strict:
                    raise ValueError("cycle event is not an object")
                continue
            ev = row.get("ev")
            if ev == "SAFETY_STOP" and row.get("reason"):
                reason = str(row["reason"])
                if strict:
                    status = "safety_stop"
            elif ev == "cycle_stopped":
                reason = "operator_stopped"
                if strict:
                    status = "stopped"
            elif ev == "host_left":
                reason = "host_left"
            elif ev == "source_or_dll_drift":
                reason = "source_or_dll_drift"
            elif ev in ("hands_not_engaged", EFFECT_LADDER_DRAIN):
                reason = ev
    if reason is None and active_menu_hold(state):
        return "menu_held", "menu_held"
    return status, reason or status


def active_menu_hold(state):
    if state.get("status") in ("starting", "active", "menu_held") and not state.get("stop_reason"):
        return state.get("menu_held")
    return None


def classify_cycle_stop(status, reason):
    """self_heal | hard | ok, and a named reason."""
    status = (status or "") or ""
    reason = (reason or "") or ""
    low = reason.strip()
    if status == "host_left" or low == "host_left" or low.startswith("host_left:"):
        return "hard", "host_left"
    if status == "stopped" or low in ("operator_stopped", "stopped"):
        return "hard", "operator_stopped"
    if "identity_mismatch" in low or low.startswith("native_save_identity"):
        return "hard", "identity_mismatch"
    if low.startswith("trial_not_verified:") or low in SELF_HEAL_EXACT or low.endswith("continuation_not_ready"):
        return "self_heal", low or "trial_not_verified"
    if "pad_watch" in low:
        return "self_heal", low
    if low in HARD_EXACT or status == "safety_stop":
        return "hard", low or status or "safety_stop"
    if status in ("ready", "active", None, ""):
        return "ok", status or "none"
    return "ok", status or "none"


HOST_WAIT_STALE_PERIODS = 3  # ADR 255 (review M4d item 3): older than three of the helper's periods = not a wait


def host_waits(run, now=None):
    """ADR 255 (review M4d item 3): what the HOST helper waits on, or None. A wait is a fact only while the helper is
    provably alive: its own host_wait.json (rewritten at least every period_s while it waits) says waiting, is at most
    three periods old by the wall clock, and no HOST_STOP exists. Anything else (no row, a stale row, a dead helper, a
    stop after the wait) is not a wait, and the normal fault path applies."""
    host = Path(run) / "host"
    if (host / "HOST_STOP").exists():
        return None
    row = _read_json(host / "host_wait.json")
    if not isinstance(row, dict) or row.get("state") != "waiting":
        return None
    at, period = row.get("at"), row.get("period_s")
    if not isinstance(at, (int, float)) or not isinstance(period, (int, float)) or period <= 0:
        return None
    now = time.time() if now is None else now
    if abs(now - at) > HOST_WAIT_STALE_PERIODS * period:  # stale, or from a clock that is not ours
        return None
    return str(row.get("what") or "waiting")


def last_helper_stop(run):
    """Named helper Stop reason, or None if the helper has not stopped."""
    run = Path(run)
    marker = run / "host" / "HOST_STOP"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text.splitlines()[0].strip()
        except OSError:
            pass
    log = run / "host" / "host.jsonl"
    if not log.is_file():
        return None
    last = None
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("ev") != "HOST_SAFETY_STOP":
            continue
        facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
        last = facts.get("reason") or row.get("reason")
    return last


def treat_startup_pause_lost(run):
    """startup_pause_lost is a release, not a helper death (11-15 13:11)."""
    path = Path(run) / "host" / "startup.json"
    row = _read_json(path) or {}
    row["state"] = "released"
    row["supervise"] = "startup_pause_lost"
    _write_json(path, row)
    return row


def cycle_between_days(state):
    if not isinstance(state, dict):
        return False
    if state.get("status") == "ready":
        return True
    days = state.get("days") or []
    if not days:
        return False
    return days[-1].get("status") in ("ended", "night_saved")


def is_saved_morning(*, save_mtime, prev_mtime, game_time, between_days):
    mtime_changed = save_mtime is not None and prev_mtime is not None and save_mtime > prev_mtime
    clock_morning = game_time is not None and int(game_time) <= MORNING_CLOCK
    return bool((mtime_changed and between_days) or clock_morning)


def game_clock(run):
    facts = ((_read_json(Path(run) / "host" / "status.json") or {}).get("facts") or {})
    t = facts.get("time")
    try:
        return int(t) if t is not None else None
    except (TypeError, ValueError):
        return None


def save_mtime(run, *, saves_root=None):
    continued = _read_json(Path(run) / "continued.json") or {}
    name = continued.get("save")
    if not name:
        return None
    from scripts.livepath import mabar
    root = Path(saves_root or mabar.APPDATA / "Saves")
    path = root / name / name
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def helper_alive(run):
    reason = last_helper_stop(run)
    if reason:
        return "dead:" + reason
    status = Path(run) / "host" / "status.json"
    if status.is_file():
        return "alive"
    return "dead:unknown"


def recoveries_last_hour(state, now):
    return len([t for t in (state.get("rejoin_at") or []) if now - t < 3600])


def format_status_line(*, now, cycle_name, cycle_status, playing, play_since,
                       voice, chat, helper, headroom_gb, last_fault, auto_recoveries, effect=None):
    """§4e one line. play=yes | play=no since HH:MM:SS. ADR 241 A: `effect=NNs` (and `rung=k`) while a trial runs."""
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    play = "yes" if playing else ("no since %s" % (play_since or "??:??:??"))
    hr = "?" if headroom_gb is None else ("%.1f" % float(headroom_gb))
    return ("%s cycle=%s %s play=%s voice=%s chat=%s helper=%s headroom=%s last_fault=%s auto_recoveries=%d/h%s"
            % (stamp, cycle_name or "c?", cycle_status or "none", play, voice, chat, helper,
               hr, last_fault or "none", int(auto_recoveries or 0), (" " + effect) if effect else ""))


def effect_status(cycle, now):
    """ADR 241 A: the runtime's effect clock as the cycle last noted it, aged by the wall seconds since; None when
    no trial is running or the file is missing."""
    row = _read_json(Path(cycle) / "effect_clock.json") or {}
    if row.get("ended") or type(row.get("age_s")) not in (int, float):
        return None
    from arti_runtime.effect_clock import status_text
    since = row.get("noted_wall")
    extra = max(0.0, now - since) if type(since) in (int, float) else 0.0
    return status_text(dict(row, age_s=row["age_s"] + extra))


def play_since_hms(cycle, now):
    events = Path(cycle) / "cycle_events.jsonl"
    last_at = None
    if events.is_file():
        try:
            for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ev") in ("SAFETY_STOP", "cycle_stopped", "host_left", "hands_not_engaged", EFFECT_LADDER_DRAIN):
                    last_at = row.get("at")
        except OSError:
            pass
    if last_at is None:
        return time.strftime("%H:%M:%S", time.localtime(now))
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(last_at)))
    except (TypeError, ValueError, OSError):
        return time.strftime("%H:%M:%S", time.localtime(now))


def gather_status(run, *, now=None, bridge_fn=None, chat_fn=None, helper_fn=None,
                  headroom_fn=None, clock_fn=None):
    run = Path(run)
    now = now if now is not None else (clock_fn or time.time)()
    cycle = newest_cycle(run)
    state = _read_json(cycle / "cycle_state.json") or {}
    status = state.get("status")
    menu_hold = active_menu_hold(state)
    if menu_hold:
        status = "menu_held"
    playing = status in ("ready", "active", "menu_held")
    from scripts.livepath import mabar
    voice = "up" if (bridge_fn or mabar.bridge_pids)() else "down"
    if chat_fn:
        chat = "up" if chat_fn() else "down"
    else:
        cfg = _read_json(run / "rehearsal-config.json") or {}
        chat = "up" if cfg.get("youtube_chat_enabled") and voice == "up" else "down"
    helper = (helper_fn or (lambda: helper_alive(run)))()
    if headroom_fn:
        hr = headroom_fn()
    else:
        from scripts.livepath.bringup import memory_snapshot
        try:
            hr = snapshot_headroom(memory_snapshot())
        except Exception:
            hr = None
    sup = load_state(run)
    last_fault = sup.get("last_fault")
    if status == "not_recovered":
        last_fault = state.get("stop_reason") or last_fault
        if last_fault == "menu_held_recover" and (state.get("menu_held") or {}).get("menu"):
            last_fault += ":" + state["menu_held"]["menu"]
    if menu_hold:
        last_fault = "menu_held:" + menu_hold["menu"]
    return format_status_line(
        now=now, cycle_name=cycle.name if cycle else "c?", cycle_status=status or "none",
        playing=playing, play_since=None if playing else play_since_hms(cycle, now),
        voice=voice, chat=chat, helper=helper, headroom_gb=hr,
        last_fault=last_fault or ("none" if playing else (state.get("stop_reason") or status)),
        auto_recoveries=recoveries_last_hour(sup, now),
        effect=effect_status(cycle, now) if playing and cycle else None,
    )


def _trim(times, now, window):
    return [t for t in times if now - t < window]


def _rejoin_allowed(state, now):
    history = _trim(state.get("rejoin_at") or [], now, 3600)
    if history and now - history[-1] < MIN_REJOIN_GAP_S:
        return False, "rejoin_cooldown"
    if len(history) >= MAX_REJOINS_PER_HOUR:
        return False, "rejoin_hour_limit"
    return True, None


def tick(run, state, *, now=None, clock_fn=None, games_fn=None, rejoin_fn=None,
         host_relaunch_fn=None, headroom_snapshot_fn=None, disk_fn=None,
         helper_stop_fn=None, save_mtime_fn=None, game_clock_fn=None,
         between_days_fn=None, log_fn=None, close_fn=None, hold_fn=None):
    """One 10 s observation. Returns an action dict. Never launches unless fns do.

    ADR 254 (fix148A): `hold_fn(held)` is the HALT table on this moment's evidence (a `halt_authority.Decision`), read
    first on every tick. While it holds, nothing is closed, relaunched or rejoined, one ALERT names it per episode,
    and the tick resumes on its own once the mod shows the operator's re-arm of the held episode. Each close,
    relaunch and rejoin reads the table again right before it acts (`HandsHeld`)."""
    run = Path(run)
    now = now if now is not None else (clock_fn or time.time)()
    log = log_fn or (lambda msg: log_line(run, msg, now=now))

    if (run / "SUPERVISE_STOP").exists():
        state["last_action"] = "off"
        log("off SUPERVISE_STOP")
        return dict(action="off", reason="SUPERVISE_STOP")

    cycle = newest_cycle(run)
    status, reason = cycle_stop(cycle)
    kind, named = classify_cycle_stop(status, reason)
    if (held := _hold(state, log, hold_fn, now, named, run=run)) is not None:
        return held  # ADR 254: her hands are read before anything else; a relaunch is never a way around a HALT
    # ADR 255 (review M4c rule 5): a HOST that waits for the owner, or is not clean, is a NAMED WAIT, never a stall: no
    # recovery fires against it (no rejoin, no rung 6, no close); her play goes on. Said once per wait.
    if kind == "self_heal" and (busy := host_waits(run, now)):
        if state.get("last_action") != "host_busy" or state.get("last_fault") != busy:
            log("named_wait: the HOST is busy (%s); no recovery while it waits" % busy)
        state.update(last_action="host_busy", last_fault=busy)
        return dict(action="wait", reason="host_busy", blocks=busy)
    if kind == "self_heal" and named == "menu_held":
        held = active_menu_hold(_read_json(cycle / "cycle_state.json") or {}) or {}
        menu = held.get("menu")
        fault = "menu_held:" + (menu or "unknown")
        if state.get("last_action") != "menu_held" or state.get("last_fault") != fault:
            log("%s runtime_recovery" % fault)
        state.update(pending=None, last_action="menu_held", last_fault=fault)
        return dict(action="menu_held", status=status, reason=named, menu=menu, recoverable=True)
    games = str((games_fn or (lambda: "0"))())
    snap = None
    if headroom_snapshot_fn:
        try:
            snap = headroom_snapshot_fn()
        except Exception:
            snap = None
    hr = snapshot_headroom(snap) if snap else None
    disk = None
    if disk_fn:
        try:
            disk = float(disk_fn())
        except (TypeError, ValueError):
            disk = None

    helper_reason = (helper_stop_fn or (lambda: last_helper_stop(run)))()
    helper_action = _helper_tick(run, state, helper_reason, now=now, log=log,
                                 save_mtime_fn=save_mtime_fn, game_clock_fn=game_clock_fn,
                                 between_days_fn=between_days_fn, cycle=cycle,
                                 host_relaunch_fn=host_relaunch_fn, rejoin_fn=rejoin_fn, close_fn=close_fn)
    if helper_action and helper_action.get("action") in ("host_stopped", "helper_released"):
        if helper_action["action"] == "host_stopped":
            return helper_action  # review M4b rule 5: the end state, said once

    if disk is not None and disk < DISK_FLOOR_GIB:
        return _stay_hard(state, log, "disk_floor", now)
    if hr is not None and hr < HARD_HEADROOM_GB:
        return _stay_hard(state, log, "headroom", now)

    if kind == "ok":
        state["pending"] = None
        state["last_action"] = "ok"
        return dict(action="ok", reason=named, status=status)

    if kind == "hard":
        return _stay_hard(state, log, named, now)

    if state.get("chat_only") or (run / "chat_only.json").is_file():
        state["chat_only"] = True
        state["last_action"] = "chat_only"
        log("stay_hard chat_only: %s" % named)
        return dict(action="chat_only", reason=named)

    if named == "menu_held_recover" and state.get("menu_gave_up") == cycle.name:
        state["last_action"] = "stay_hard"  # said once (ALERT); a human closes the menu
        return dict(action="stay_hard", reason=state.get("last_fault") or "menu_held_limit")
    if named in LADDER_DRAINS and state.get("ladder_gave_up") == cycle.name:
        state["last_action"] = "stay_hard"  # said once (ALERT); a human looks
        return dict(action="stay_hard", reason="effect_ladder_limit")

    pending = state.get("pending") or {}
    same = pending.get("cycle") == cycle.name and pending.get("reason") == named
    if not same:
        state["pending"] = dict(cycle=cycle.name, reason=named, seen_at=now)
        state["last_action"] = "reobserve"
        log("reobserve %s" % named)
        return dict(action="reobserve", reason=named)

    seen_at = pending.get("seen_at")
    if seen_at is None:
        seen_at = now
    if now - float(seen_at) < REOBSERVE_S:
        state["last_action"] = "reobserve"
        return dict(action="reobserve", reason=named)

    allowed, bound = _rejoin_allowed(state, now)
    if not allowed:
        log("wait %s (%s)" % (bound, named))
        if bound == "rejoin_hour_limit":  # ADR 241 C: never the only trace of a recovery that cannot run
            since = state.get("hour_cap_since") or now
            state["hour_cap_since"] = since
            if now - since >= HOUR_CAP_ALERT_S and not state.get("hour_cap_alerted"):
                state["hour_cap_alerted"] = True
                log("ALERT rejoin_hour_limit: %s has waited %d min on %d rejoins an hour; a human looks" % (
                    named, int((now - since) // 60), MAX_REJOINS_PER_HOUR))
        state["last_action"] = bound
        return dict(action="wait", reason=bound, stop=named)
    state.update(hour_cap_since=None, hour_cap_alerted=False)

    # Both games up: rejoin launches nothing, never memory-gated. One game: farmhand budget.
    if games != "2":
        check = headroom_check(snap, "farmhand")
        if snap is None or check.status == "BLOCKER":
            since = state.get("headroom_block_since") or now
            state["headroom_block_since"] = since
            log("wait headroom (%s) games=%s %s" % (named, games, check.detail if snap else "UNKNOWN"))
            if now - since >= 600:
                log("ALERT headroom wait 10 min: %s" % named)
            state["last_action"] = "wait_headroom"
            return dict(action="wait", reason="headroom", stop=named)
    state["headroom_block_since"] = None

    hands_fault = named in DRAINING_FAULTS
    if hands_fault and (cycle / "supervisor.lock").exists():
        return dict(action="wait", reason="trial_draining", stop=named)
    menu = recent = None
    if named == "menu_held_recover":
        menu = ((_read_json(cycle / "cycle_state.json") or {}).get("menu_held") or {}).get("menu") or "unknown"
        recent = _trim(state.get("menu_recover_at") or [], now, MENU_RECOVER_WINDOW_S)
        if len(recent) >= MENU_RECOVER_LIMIT:  # the family's ceiling (ADR 237 C)
            state["menu_gave_up"] = cycle.name
            state["menu_recover_at"] = recent
            log("ALERT menu_held_limit:%s, %d recoveries in 30 min did not free her; a human closes it" % (
                menu, len(recent)))
            return _stay_hard(state, log, "menu_held_limit:" + menu, now)
    prior_ladder = {key: state.get(key) for key in ("ladder_recover_at", "ladder_log")}
    if named in LADDER_DRAINS:
        rung6 = _ladder_rung6(run, cycle, state, now=now, log=log, games=games, host_relaunch_fn=host_relaunch_fn,
                              rejoin_fn=rejoin_fn, close_fn=close_fn, reason=named, menu=menu)
        if rung6 is not None:
            return rung6
    reserved = {key: state.get(key) for key in ("menu_recover_at", "rejoin_at", "pending")}
    if named == "menu_held_recover":
        state["menu_recover_at"] = recent + [now]
    state["rejoin_at"] = _trim((state.get("rejoin_at") or []) + [now], now, 3600)
    state["pending"] = None
    state["last_fault"] = named
    if hands_fault:
        # ADR 250: a no-op report when every hand path is focus-independent; else the release names its failure.
        cleared = release_farmhand_focus(run)
        cause = getattr(cleared, "cause", None)
        if not cleared:
            log("%s: focus=farmhand_window_active rejoin_deferred cause=%s games=%s" % (named, cause, games))
            state["last_action"] = "farmhand_window_active"
            return dict(action="wait", reason="farmhand_window_active", stop=named, cause=cause)
        log("%s: focus=%s rejoining games=%s" % (
            "menu_held_recovered:%s (#%d in 30 min)" % (menu, len(state["menu_recover_at"])) if menu else named,
            "kept_in_front_focus_independent" if cause == FOCUS_INDEPENDENT else "cleared", games))
    rc = 0
    if rejoin_fn:
        try:
            rc = rejoin_fn() or 0
        except HandsHeld as exc:  # ADR 254: the rejoin's own read held; no failure, no chat_only
            for key, value in (reserved | prior_ladder).items():
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
            return _hold(state, log, None, now, named, decision=exc.decision, run=run)
        except Exception as exc:  # noqa: BLE001 - ADR 241 C: soak.call raises on any non-zero exit; counted, never a loop
            rc = "error"
            log("rejoin_failed: %s %s: %s" % (named, type(exc).__name__, str(exc)[:200]))
    after_status, _ = cycle_stop(newest_cycle(run))
    failed = rc != 0 or after_status in ("safety_stop", "stopped", "host_left", "not_recovered")
    if failed:
        state.setdefault("failure_at", []).append(now)
        state["failure_at"] = _trim(state["failure_at"], now, FAIL_WINDOW_S)
    if not hands_fault:
        log("rejoin: %s games=%s rc=%s" % (named, games, rc))
    recent_fail = _trim(state.get("failure_at") or [], now, FAIL_WINDOW_S)
    if len(recent_fail) >= FAIL_LIMIT:
        _write_json(run / "chat_only.json", dict(reason=named, time=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), failures=len(recent_fail)))
        state["chat_only"] = True
        log("ALERT chat_only after 3 failures in 15 min: %s" % named)
        state["last_action"] = "chat_only"
        return dict(action="chat_only", reason=named, rejoin_rc=rc)
    state["last_action"] = "rejoin"
    return dict(action="rejoin", reason=named, rc=rc, failed=failed)


def _ladder_words(log_rows):
    parts = []
    for row in log_rows:
        rungs = "; ".join("%s %s %s" % (r.get("rung"), r.get("name"), r.get("read_back") or r.get("result"))
                          for r in row.get("rungs") or ())
        if not rungs and row.get("reason") == "menu_held_recover":
            rungs = "menu_held_recover %s" % (row.get("menu") or "unknown")  # ADR 241 C: a held-menu drain
        parts.append("%s: %s" % (row.get("cycle"), rungs or "rows unread"))
    return " | ".join(parts)


def session_games(run, kind):
    """ADR 241 C: what this session started and rung 6 may close, as (name, hwnd, pid) from the operator records in
    run/runtime: her farmhand always; the helper HOST only in a solo session. The owner's game, OBS, VTube Studio and
    the bridge are never in this list."""
    run = Path(run)

    def pid_of(rel):
        pid = (_read_json(run / rel) or {}).get("pid")
        return pid if type(pid) is int else None

    def hwnd_of(rel):
        try:
            return int((run / rel).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
    games = [("farmhand", hwnd_of("runtime/fh-hwnd.txt"), pid_of("runtime/farmhand-launch.json"))]
    if kind == "solo":
        games.append(("helper_host", hwnd_of("runtime/host-hwnd.txt"), pid_of("runtime/batch-host/host-launch.json")))
    pids = [pid for _, _, pid in games if pid is not None]
    if len(pids) != len(set(pids)):
        raise RuntimeError("the farmhand and HOST records name the same pid")
    return games


def keep_smapi_logs(run, label):
    """`mabar.keep_smapi_logs` (ADR 241 C3): both live SMAPI logs into the run before a close or a relaunch."""
    from scripts.livepath import mabar
    return mabar.keep_smapi_logs(run, label)


def controller_session(state):
    """Review M4b rule 4: this supervisor's controller session, one id per supervisor process (run_supervise sets it). Every
    exit request names it; the HOST honours only the id the supervisor wrote once into its folder (controller.json)."""
    import uuid
    if not isinstance(state.get("controller_session"), str) or not state["controller_session"]:
        state["controller_session"] = uuid.uuid4().hex
    return state["controller_session"]


def write_controller_session(run, session):
    """Written ONCE per supervisor process into the HOST helper's folder: a new supervisor (a handoff) replaces it, and
    every request naming the old id is refused. Nothing reads it as a fence. Returns the path, or None (no helper HOST)."""
    cfg = _read_json(Path(run) / HELPER_CONFIG) or {}
    out, run_id = cfg.get("OutputDirectory"), cfg.get("RunId")
    if not out or not run_id:
        return None
    path = Path(out) / "controller.json"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(dict(v=1, run_id=run_id, session=session, writer="mabar_supervise",
                                       utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return None
    return path


def run_stopped(run):
    """Review M4b / M4c rule 7: any stop of this run, as marker files only: the outer markers, the runtime and every
    cycle's own LIVE7_STOP (live_cycle's stop_files). A recorded stop_reason is NOT a stop: a drain record
    (not_recovered with effect_ladder_recover or menu_held_recover) carries one deliberately without a stop file, and
    rung six is its recovery (origin/main behaviour), so it must not veto its own exit request."""
    run = Path(run)
    if any((run / m).exists() for m in STOP_MARKERS + ("SUPERVISE_STOP", "LIVE7_STOP")):
        return True
    if (run / "runtime" / "LIVE7_STOP").exists():
        return True
    cycles = run / "runtime" / "live7"
    if cycles.is_dir() and (any(cycles.glob("*/LIVE7_STOP")) or any(cycles.glob("*/OPERATOR_STOP"))):
        return True
    state = _read_json(newest_cycle(run) / "cycle_state.json") or {}
    own = state.get("live7_stop_file")
    return isinstance(own, str) and bool(own) and Path(own).exists()


def close_session_games(run, kind, *, alive_fn=None, wait_exit_fn=None, keep_fn=None, ask_fn=None, session=None,
                        timeout_s=GAME_EXIT_WAIT_S, window_fn=None, gate_fn=None):
    """ADR 241 C, rung 6's close, now through ADR 256 (review M4 E1): each running session game is asked to exit itself
    when safe (`ask_game_exit`), never sent a window message, so a save can never be cut. A game already gone is skipped.
    A game that does not answer, refuses, or is still up after its exiting answer raises GameStaysUp: the game stays up and
    the caller reports it once (rung 6 spends itself, the HOST relaunch alerts). Before the first ask, both SMAPI logs are
    kept with the run (the relaunch would overwrite the stuck window's log).
    Review M4d item 2: every refusal of origin/main's recorded window close is kept before any request is written: a
    running game whose window is not recorded, a recorded window that no longer exists, or one that belongs to another
    process raises GameStaysUp, and nothing is asked."""
    from scripts.m1_operator import win_facts
    alive_fn = alive_fn or win_facts.pid_alive
    wait_exit_fn = wait_exit_fn or win_facts.wait_pid_exit
    window_fn = window_fn or win_facts.window
    closed, kept = [], False
    for name, hwnd, pid in session_games(run, kind):
        if pid is None or not alive_fn(pid):
            continue
        if hwnd is None:
            raise GameStaysUp("%s pid %s runs but its window is not recorded; nothing was asked" % (name, pid))
        facts = window_fn(hwnd) or {}
        if not facts.get("exists") or facts.get("pid") != pid:
            raise GameStaysUp("refused: %s hwnd %s exists=%s pid=%s, expected pid %s; nothing was asked" % (
                name, hwnd, bool(facts.get("exists")), facts.get("pid"), pid))
        act = "close" if name == "farmhand" else "launch"
        if not kept:
            if gate_fn is not None:
                gate_fn(act)
            (keep_fn or keep_smapi_logs)(run, "before-close")
            kept = True
        if gate_fn is not None:  # ADR 254: recheck after log preservation, before the in-game request.
            gate_fn(act)
        options = dict(kind=kind, session=session, alive_fn=alive_fn, wait_exit_fn=wait_exit_fn, exit_wait_s=timeout_s)
        if gate_fn is not None:
            options.update(gate_fn=gate_fn, gate_act=act)
        (ask_fn or ask_game_exit)(run, name, pid, **options)
        closed.append(name)
    return closed


EXIT_REQUEST_FILE, EXIT_ANSWER_FILE = "exit_request.json", "exit_answer.json"
EXIT_HORIZON_S = 170  # ADR 256: a request lives at most 180 s from issue; 170 leaves the mods' margin
EXIT_POLL_S = 1.0


class GameStaysUp(RuntimeError):
    """ADR 256 (review M4 E1): the game was asked to exit when safe and did not; it stays up (no forced close)."""


def game_exit_target(run, name, kind, *, inbox_fn=None):
    """(folder, identity) of one session game's own mod for an exit request, from what the run recorded. The HOST helper:
    its OutputDirectory, run, runtime (barrier.json), save and HOST farmer (helper config). Her farmhand: her command
    inbox, runtime and farmer (session.json), and the save (helper config in a solo session, else her own occurrence
    ring's save id). Anything unknown raises GameStaysUp: an identity the program cannot state cannot be asked."""
    run = Path(run)
    cfg = _read_json(run / HELPER_CONFIG) or {}
    if name == "helper_host":
        out = cfg.get("OutputDirectory")
        barrier = _read_json(Path(out) / "barrier.json") if out else None
        if not (out and cfg.get("RunId") and isinstance(barrier, dict) and barrier.get("run_id") == cfg.get("RunId")
                and isinstance(barrier.get("runtime_id"), str) and type(cfg.get("SaveId")) is int
                and type(cfg.get("HostId")) is int):
            raise GameStaysUp("helper_host identity unknown (helper config or barrier.json)")
        return Path(out), dict(run_id=cfg["RunId"], runtime_id=barrier["runtime_id"], save_id=str(cfg["SaveId"]),
                               player_id=cfg["HostId"])
    if inbox_fn is None:
        from scripts.livepath import mabar
        inbox_fn = mabar.inbox
    inbox = Path(inbox_fn())
    sess = _read_json(inbox / "session.json") or {}
    enrolled = _read_json(newest_cycle(run) / "cycle_state.json") or {}
    save = enrolled.get("save_id")
    if save is None and kind == "solo" and type(cfg.get("SaveId")) is int:
        save = str(cfg["SaveId"])
    if save is None:
        rings = sorted(inbox.glob("occurrences.*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        parts = [p.name.split(".") for p in rings]
        save = next((pr[1] for pr in parts if len(pr) >= 3 and pr[1].isdigit()), None)
    if not (isinstance(sess.get("runtime_instance_id"), str) and sess["runtime_instance_id"]
            and type(sess.get("player_id")) is int and save):
        raise GameStaysUp("farmhand identity unknown (session.json or save id)")
    return inbox, dict(run_id=cfg.get("RunId") or run.name, runtime_id=sess["runtime_instance_id"], save_id=save,
                       player_id=sess["player_id"])


def ask_game_exit(run, name, pid, *, kind=None, session=None, alive_fn=None, wait_exit_fn=None, now_fn=None,
                  sleep_fn=None, target_fn=None, stop_fn=None, horizon_s=EXIT_HORIZON_S, exit_wait_s=GAME_EXIT_WAIT_S,
                  gate_fn=None, gate_act="close"):
    """ADR 256, review M4 E1: the program's only way to close a session game. It writes `exit_request.json` in the game's
    own mod folder (atomic; bound to the game's identity, this supervisor's controller session and a request id; it
    expires on its own) and waits for that request's `exit_answer.json`:
      exiting -> the game quits itself at a safe tick; success only once its pid is gone;
      waiting -> keep waiting (a save in progress, a committing menu, ...);
      refused:* / expired / aborted, no answer by the deadline, or a stop marker -> GameStaysUp (no window message,
      no forced close; the caller reports it).
    The request is removed when the wait ends, so it can never exit a game later.
    Review M4d item 4: the request names the process it is for (`target_pid`, the recorded launch); any other process
    refuses it. For the helper HOST the controller.json this supervisor published must be in the HOST's folder and name
    this run and session before any request is written (after a relaunch, the new folder must have it)."""
    import os
    import sys
    import uuid
    run = Path(run)
    now_fn = now_fn or time.time
    sleep_fn = sleep_fn or time.sleep
    if alive_fn is None or wait_exit_fn is None:
        from scripts.m1_operator import win_facts
        alive_fn = alive_fn or win_facts.pid_alive
        wait_exit_fn = wait_exit_fn or win_facts.wait_pid_exit
    stop_fn = stop_fn or (lambda: run_stopped(run))  # review M4b: any stop, the cycle's own included
    folder, ident = (target_fn or game_exit_target)(run, name, kind)
    if name == "helper_host":
        ctl = _read_json(Path(folder) / "controller.json") or {}
        if not session or ctl.get("run_id") != ident.get("run_id") or ctl.get("session") != session:
            raise GameStaysUp("helper_host: this supervisor's controller.json is not published in its folder; nothing asked")
    if type(pid) is not int or pid <= 0:
        raise GameStaysUp("%s: no recorded launch pid to name; nothing asked" % name)
    request_id = uuid.uuid4().hex
    issued = now_fn()
    stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    row = dict(v=1, request_id=request_id, reason="supervisor_close:" + name, session=session or "",
               requester_pid=os.getpid(), requester_image=os.path.basename(sys.executable), target_pid=pid,
               issued_utc=stamp(issued), deadline_utc=stamp(issued + horizon_s), **ident)
    path, answer_path = Path(folder) / EXIT_REQUEST_FILE, Path(folder) / EXIT_ANSWER_FILE
    tmp = path.with_name(path.name + "." + request_id + ".tmp")

    def check_request():
        if gate_fn is not None:
            decision = gate_fn(gate_act)
            if (name == "farmhand" and decision is not None
                    and (decision.session or {}).get("runtime_instance_id") != ident.get("runtime_id")):
                raise GameStaysUp("farmhand identity changed before exit")
        if stop_fn():
            raise GameStaysUp("%s: a stop outranks the close (request %s withdrawn)" % (name, request_id))

    try:
        tmp.write_text(json.dumps(row) + "\n", encoding="utf-8")
        check_request()
        tmp.replace(path)
        while True:
            check_request()
            answer = _read_json(answer_path) or {}
            state = answer.get("state") if all(answer.get(k) == row[k] for k in ("request_id", "run_id", "runtime_id")) else None
            if state == "exiting":
                if wait_exit_fn(pid, exit_wait_s) is True:
                    check_request()
                    return dict(name=name, request_id=request_id, state="exited")
                raise GameStaysUp("%s answered exiting but pid %s is still up after %ds" % (name, pid, exit_wait_s))
            if name == "farmhand" and gate_fn is not None and state == "refused:not_owned":
                from scripts.livepath.halt_authority import Decision
                raise HandsHeld(Decision("hold", "native_exit_refused_not_owned"))
            if isinstance(state, str) and state != "waiting":
                raise GameStaysUp("%s answered %s%s; it stays up" % (name, state,
                                  (" (" + str(answer.get("why")) + ")") if answer.get("why") else ""))
            if not alive_fn(pid):
                raise GameStaysUp("%s went away without a matching exit answer; exit unconfirmed" % name)
            if now_fn() >= issued + horizon_s:
                why = (" (last: waiting %s)" % answer.get("why")) if state == "waiting" else " (no answer from its mod)"
                raise GameStaysUp("%s did not exit within %ds%s; it stays up" % (name, horizon_s, why))
            sleep_fn(EXIT_POLL_S)
    finally:
        try:
            tmp.unlink(missing_ok=True)
            current = _read_json(path) or {}
            if current.get("request_id") == request_id:
                path.unlink()
        except OSError:
            pass


def _ladder_rung6(run, cycle, state, *, now, log, games, host_relaunch_fn, rejoin_fn, close_fn=None,
                  reason=EFFECT_LADDER_DRAIN, menu=None):
    """ADR 241 B rungs 5 and 6 on the supervisor (ADR 241 C: held-menu drains are on the same ladder). None: go on
    with the ordinary drained rejoin (rung 5)."""
    previous_log = state.get("ladder_log")
    previous_history = state.get("ladder_recover_at")
    resume = state.get("ladder_resume") or {}
    if resume.get("cycle") != Path(cycle).name or resume.get("reason") != reason:
        resume = {}
        state.pop("ladder_resume", None)
    rows = (_read_json(Path(cycle) / "cycle_state.json") or {}).get("effect_ladder") or {}
    history = _trim(state.get("ladder_recover_at") or [], now, LADDER_WINDOW_S)
    entry = dict(at=now, cycle=Path(cycle).name, rungs=rows.get("rungs") or [], silent_age_s=rows.get("silent_age_s"),
                 reason=reason, menu=menu)
    state["ladder_log"] = ([r for r in state.get("ladder_log") or [] if now - r.get("at", now) < LADDER_WINDOW_S]
                           + [entry])[-6:]
    relaunched = state.get("ladder_relaunched_at")
    if type(relaunched) in (int, float) and now - relaunched < LADDER_WINDOW_S:
        state["ladder_gave_up"] = Path(cycle).name
        state["ladder_recover_at"] = history + [now]
        log("ALERT effect_ladder_limit: rungs 1-6 ran and she still changes nothing; a human looks. Rungs: %s; "
            "rung 6 relaunched %s at %s" % (
                _ladder_words(state["ladder_log"]), state.get("ladder_relaunched_what") or "her game",
                time.strftime("%H:%M:%S", time.localtime(relaunched))))
        return _stay_hard(state, log, "effect_ladder_limit", now)
    state["ladder_recover_at"] = history + [now]
    if not history and not resume:
        return None  # rung 5: the drained attempt is rejoined as a fresh cycle below

    def spent(why):
        state.pop("ladder_resume", None)
        state["ladder_gave_up"] = Path(cycle).name
        log("ALERT effect_ladder_limit: %s; a human looks. Rungs: %s" % (why, _ladder_words(state["ladder_log"])))
        return _stay_hard(state, log, "effect_ladder_limit", now)

    kind = session_kind(run)
    if kind is None:
        return spent("rung 6 refused: who hosts this session is not recorded (trial-config stardew_host_controller=%r, "
                     "helper installed=%s); nothing was closed" % (
                         (_read_json(Path(run) / "trial-config.json") or {}).get("stardew_host_controller"),
                         (Path(run) / HELPER_CONFIG).is_file()))
    if (not resume and int(state.get("host_relaunches") or 0) >= MAX_HOST_RELAUNCH_PER_STREAM) or (
            kind == "solo" and host_relaunch_fn is None):
        return spent("a fresh cycle reached rung 5 again and rung 6 is spent (%d relaunches)" % int(
            state.get("host_relaunches") or 0))
    # Rung 6 closes only what this session started: solo = her farmhand and the helper HOST, then the helper HOST
    # reloads the last saved morning; owner-hosted = her farmhand only, then she rejoins the owner's running farm.
    if not resume:
        state["host_relaunches"] = int(state.get("host_relaunches") or 0) + 1
    what = "her game from the last saved morning" if kind == "solo" else "her farmhand game into the owner's running farm"
    try:
        if not resume:
            closed = close_fn(run, kind) if close_fn else close_session_games(run, kind, session=controller_session(state))
            resume = dict(cycle=Path(cycle).name, reason=reason, closed=closed or [], host_started=False)
            state["ladder_resume"] = resume
        if kind == "solo" and not resume["host_started"]:
            host_relaunch_fn()
            resume["host_started"] = True
            # the relaunch moved run/host aside: publish again; review M4d item 4: say it once if that failed (no request
            # can then be sent to the new HOST: ask_game_exit verifies controller.json before writing one)
            if write_controller_session(run, controller_session(state)) is None:
                log("ALERT controller_not_published: the relaunched HOST has no controller.json; it can not be asked to exit")
        rc = (rejoin_fn() or 0) if rejoin_fn else 0
    except HandsHeld as exc:  # ADR 254: a hold read at a boundary of rung 6 never spends it
        if not resume:
            state["host_relaunches"] = int(state["host_relaunches"]) - 1
            for key, value in (("ladder_log", previous_log), ("ladder_recover_at", previous_history)):
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
        return _hold(state, log, None, now, reason, decision=exc.decision, run=run)
    except Exception as exc:  # noqa: BLE001 - ADR 241 C: any failure inside rung 6 spends it, with one ALERT
        return spent("rung 6 failed (%s session: %s: %s)" % (kind, type(exc).__name__, str(exc)[:200]))
    closed = resume["closed"]
    state.pop("ladder_resume", None)
    state["ladder_relaunched_at"] = now
    state["ladder_relaunched_what"] = what
    state.setdefault("rejoin_at", []).append(now)
    state.update(pending=None, last_fault=reason, last_action="ladder_relaunch")
    done = ("her game relaunched from the last saved morning" if kind == "solo"
            else "her farmhand game relaunched into the owner's running farm")
    log("rung 6: %s (#%d, %s session, closed: %s, rejoin rc=%s); rungs so far: %s" % (
        done, state["host_relaunches"], kind, ", ".join(closed) or "nothing running", rc,
        _ladder_words(state["ladder_log"])))
    return dict(action="ladder_relaunch", reason=reason, games=games, session=kind, closed=closed)


def voice_restart_argv(run, recorded):
    """The recorded `mabar voice` argv without step/--run/--spec, with the run's chat id re-attached."""
    out, skip = [], 0
    for i, item in enumerate(recorded or []):
        if skip:
            skip -= 1
            continue
        if i == 0 and item == "voice":
            continue
        if item in ("--run", "--spec", "--youtube-video-id"):
            skip = 1
            continue
        if item.startswith(("--run=", "--spec=", "--youtube-video-id=")):
            continue
        out.append(item)
    ssn = any(x.startswith(("--ssn-session-file", "--ssn-from-app")) for x in out)
    from scripts.livepath.mabar_solo import chat_id_of
    vid = "" if ssn else chat_id_of(run)
    if vid and vid != "YOUR_VIDEO_ID":
        out += ["--youtube-video-id", vid]
    return out


def _deliberate_voice_stop(run):
    """Presence is authority, independent of the previous voice process's start time."""
    from scripts.livepath import halt_authority as HA, mabar

    try:
        folder = mabar.inbox()
    except FileNotFoundError:
        folder = Path(run) / "runtime" / "command"
    except (OSError, TypeError, ValueError, KeyError):
        return True
    evidence = HA.read_evidence(folder, run, recovery=True)
    return bool(evidence.outranking or evidence.sentinels or evidence.stops or evidence.host_inert)


def voice_tick(run, state, *, now, log, bridge_fn, start_voice_fn):
    """Restart a voice bridge that died by itself. Never a second bridge; never touches the game cycle."""
    run = Path(run)
    if not (run / "voice.json").is_file():
        return None  # mabar never started a voice in this run: nothing to restart
    if bridge_fn():
        state["voice_down_since"] = None
        state["voice_stop_logged"] = False
        return dict(action="voice_ok")
    def stopped():
        if not state.get("voice_stop_logged"):
            log("voice stopped on request: no restart")
            state["voice_stop_logged"] = True
        return dict(action="voice_stopped")

    if _deliberate_voice_stop(run):
        return stopped()
    if state.get("voice_gave_up"):
        return dict(action="voice_gave_up")
    since = state.get("voice_down_since")
    if since is None:
        state["voice_down_since"] = now
        log("voice down: re-observe")
        return dict(action="voice_reobserve")
    if now - float(since) < POLL_S:
        return dict(action="voice_reobserve")
    recent = _trim(state.get("voice_restart_at") or [], now, VOICE_RESTART_WINDOW_S)
    if len(recent) >= VOICE_RESTART_LIMIT:
        state["voice_gave_up"] = True
        state["voice_restart_at"] = recent
        log("ALERT voice died %d times in 10 min: no more restarts" % (len(recent) + 1))
        return dict(action="voice_gave_up")
    if bridge_fn():  # re-check right before starting: two voices would collide
        state["voice_down_since"] = None
        return dict(action="voice_ok")
    recorded = (_read_json(run / "voice_args.json") or {}).get("argv") or []
    argv = voice_restart_argv(run, recorded)
    if _deliberate_voice_stop(run):
        return stopped()
    try:
        start_voice_fn(argv)
    finally:
        recent.append(now)
        state["voice_restart_at"] = recent
        state["voice_down_since"] = None
        state["last_fault"] = "voice_exited"
    chat = argv[argv.index("--youtube-video-id") + 1] if "--youtube-video-id" in argv else "none"
    log("voice_restart %d/%d chat=%s" % (len(recent), VOICE_RESTART_LIMIT, chat))
    return dict(action="voice_restart", argv=argv)


def service_voice(run, state, *, clock_fn, log, bridge_fn, start_voice_fn):
    """A voice probe or restart failure cannot end recovery polling."""
    try:
        return voice_tick(run, state, now=clock_fn(), log=log, bridge_fn=bridge_fn,
                          start_voice_fn=start_voice_fn)
    except Exception as exc:  # noqa: BLE001 - the next voice poll can retry
        message = "voice_supervision_error: %s: %s" % (type(exc).__name__, str(exc)[:200])
        try:
            log(message)
        except OSError:
            import sys
            print(message, file=sys.stderr)
        return None


def wait_for_recovery(operation, *, deadline, voice, poll_s=POLL_S, clock_fn=None):
    """Poll one recovery up to its overall deadline without joining a stuck worker."""
    from concurrent.futures import Future, TimeoutError as PollTimeout
    from threading import Thread
    from scripts.livepath.halt_authority import RecoveryTimedOut

    clock_fn = clock_fn or time.monotonic
    pending = Future()

    def work():
        if not pending.set_running_or_notify_cancel():
            return
        try:
            if clock_fn() >= deadline:
                raise RecoveryTimedOut("recovery_deadline")
            result = operation()
        except BaseException as exc:
            pending.set_exception(exc)
        else:
            pending.set_result(result)

    Thread(target=work, name="mabar-recovery", daemon=True).start()
    while True:
        remaining = deadline - clock_fn()
        if remaining <= 0:
            pending.cancel()
            raise RecoveryTimedOut("recovery_deadline")
        try:
            result = pending.result(timeout=min(poll_s, remaining))
        except PollTimeout:
            if pending.done():
                return pending.result()
            voice()
        else:
            if clock_fn() >= deadline:
                raise RecoveryTimedOut("recovery_deadline")
            return result


def hands_read(run, held=None, *, act="observe"):
    """ADR 254: the HALT table on this moment's evidence, as a recovery reads it (the stops that outrank it too)."""
    from scripts.livepath import halt_authority, mabar
    folder = mabar.inbox()
    if act == "observe":
        mabar.retry_marker_proof(run, folder, held=held)
    return halt_authority.check(folder, run, act=act, recovery=True, held=held)


def hands_gate(run, state):
    """ADR 254: the boundary read right before a close of this session's games (`HandsHeld` when it holds)."""
    def gate(act):
        decision = hands_read(run, state.get("hands_held"), act=act)
        if decision.action == "hold":
            raise HandsHeld(decision)
        return decision
    return gate


def _hold(state, log, hold_fn, now, named, decision=None, *, run=None):
    """ADR 254 (review R6 B4): one ALERT per held episode (or reason); no failure count, no chat_only, no close. A hold
    on a native latch is bound to its episode (`state["hands_held"]`) and ends only on the mod's receipt that the
    operator re-armed it; a clear replacement game or an old snapshot never ends it."""
    if decision is None:
        if hold_fn is None:
            return None
        try:
            decision = hold_fn(state.get("hands_held"))
        except Exception as exc:  # noqa: BLE001 - an unreadable truth is a reason to hold, not to act
            decision = SimpleNamespace(action="hold", reason="hold_read_failed:%s" % type(exc).__name__,
                                       held=state.get("hands_held"), paths=[])
    if getattr(decision, "action", None) != "hold":
        if state.pop("hold_alerted", None) is not None:
            log("hands_resumed (%s): %s" % (named, getattr(decision, "reason", "clear")))
        state.pop("hands_held", None)
        return None
    if getattr(decision, "held", None):
        state["hands_held"] = decision.held
    said = (state.get("hands_held") or {}).get("episode_id") or decision.reason
    if state.get("hold_alerted") != said:
        state["hold_alerted"] = said
        from scripts.livepath.halt_authority import claim_hold_alert
        if claim_hold_alert(run, decision):
            markers = "".join(" --live7-stop %s" % p for p in getattr(decision, "paths", None) or ())
            log("ALERT hands_held %s (%s): waiting for the operator to clear the hold with "
                "tools/arti_rearm.py --folder <inbox> --actor owner%s" % (decision.reason, named, markers))
    state["last_action"] = "hold"
    return dict(action="hold", reason=decision.reason, stop=named)


def _stay_hard(state, log, named, now):
    state["pending"] = None
    state["last_fault"] = named
    state["last_action"] = "stay_hard"
    log("stay_hard: %s" % named)
    return dict(action="stay_hard", reason=named)


def _helper_tick(run, state, helper_reason, *, now, log, save_mtime_fn, game_clock_fn,
                 between_days_fn, cycle, host_relaunch_fn, rejoin_fn, close_fn=None):
    if helper_reason == "startup_pause_lost" and state.get("helper_handled") != helper_reason:
        treat_startup_pause_lost(run)
        state["helper_handled"] = helper_reason
        log("helper_released: startup_pause_lost")
        return dict(action="helper_released", reason="startup_pause_lost")
    if helper_reason and helper_reason != "startup_pause_lost":
        # Review M4b rule 5: a stopped HOST stays up and is reported once (a stop outranks recovery). A stopped HOST is
        # required to refuse an exit request, so no close and no relaunch is attempted: this is the end state, not a loop.
        if state.get("helper_handled") != helper_reason:
            state["helper_handled"] = helper_reason
            log("ALERT host_stopped: %s; the HOST stays up (a stop outranks recovery); a person looks" % helper_reason)
        state.update(last_action="stay_hard", last_fault="host_stopped:" + helper_reason)
        return dict(action="host_stopped", reason=helper_reason)
    if save_mtime_fn:
        mtime = save_mtime_fn()
        if mtime is not None and state.get("save_mtime") is None:
            state["save_mtime"] = mtime
    return None


def relaunch_host(a):
    """The clean restart of rung 6 and the helper path: a fresh helper HOST from the last saved morning
    (`mabar host --relaunch --helper`). mabar.host refuses while any game runs; callers close the session's games first."""
    from scripts.livepath import mabar
    a.relaunch = True
    a.helper = True
    return mabar.host(a)


def run_supervise(a, *, sleep_fn=None, clock_fn=None, once=False, **hooks):
    """Long-running recovery. `once=True` for tests (one tick)."""
    run = Path(a.run)
    state = load_state(run)
    state.pop("controller_session", None)  # review M4b rule 4: one controller session per supervisor process
    write_controller_session(run, controller_session(state))  # written once; a new supervisor ends older requests
    log_line(run, "armed every %ds; SUPERVISE_STOP turns it off" % POLL_S,
             clock_fn=clock_fn)

    from argparse import Namespace
    from scripts.m1_operator.witness_run import RESTART_TIMEOUT_S

    def recovery_args(deadline):
        return Namespace(**dict(vars(a), recovery=True, recovery_deadline=deadline, recovery_expected=None,
                                recovery_clock=None))

    def rejoin(deadline):
        from scripts.livepath import mabar
        attempt = recovery_args(deadline)
        attempt.focus_host = False
        attempt.hands_held = state.get("hands_held")
        attempt.background_cycle = True
        return mabar.rejoin(attempt)

    def relaunch(deadline):
        return relaunch_host(recovery_args(deadline))

    gate = hands_gate(run, state)

    def close(deadline, run, kind):
        from scripts.livepath.halt_authority import RecoveryTimedOut

        def bounded_gate(act):
            if time.time() >= deadline:
                raise RecoveryTimedOut("recovery_deadline")
            decision = gate(act)
            if time.time() >= deadline:
                raise RecoveryTimedOut("recovery_deadline")
            return decision

        return close_session_games(run, kind, session=controller_session(state), gate_fn=bounded_gate)

    def start_voice(argv):
        from scripts.livepath.mabar_solo import spawn_mabar
        return spawn_mabar("voice", a, argv)

    bridge_fn = hooks.pop("bridge_fn", None)
    start_voice_fn = hooks.pop("start_voice_fn", None) or start_voice
    if bridge_fn is None:
        from scripts.livepath import mabar
        bridge_fn = lambda: mabar.bridge_pids(timeout=10)
    voice_interval = hooks.pop("voice_recovery_poll_s", POLL_S)

    def voice():
        now = (clock_fn or time.time)()
        service_voice(run, state, clock_fn=lambda: now,
                      log=lambda msg: log_line(run, msg, now=now),
                      bridge_fn=bridge_fn, start_voice_fn=start_voice_fn)

    def watch_recovery(operation, *args, **kwargs):
        nonlocal recovery_end
        if recovery_end is None:
            recovery_end = time.monotonic() + RESTART_TIMEOUT_S
        wall_end = time.time() + recovery_end - time.monotonic()
        return wait_for_recovery(lambda: operation(wall_end, *args, **kwargs),
                                 deadline=recovery_end, voice=voice, poll_s=voice_interval)

    hooks.setdefault("rejoin_fn", lambda: watch_recovery(rejoin))
    if "hold_fn" not in hooks:
        hooks["hold_fn"] = lambda held: hands_read(run, held)  # ADR 254: read first on every tick
    hooks.setdefault("close_fn", lambda r, kind: watch_recovery(close, r, kind))
    hooks.setdefault("host_relaunch_fn", lambda: watch_recovery(relaunch))
    if "games_fn" not in hooks:
        from scripts.livepath import mabar
        hooks["games_fn"] = lambda: mabar.smapi_count(deadline=time.time() + min(POLL_S, RESTART_TIMEOUT_S))
    while True:
        recovery_end = None
        if (run / "SUPERVISE_STOP").exists():
            log_line(run, "off SUPERVISE_STOP", clock_fn=clock_fn)
            save_state(run, state)
            return 0
        try:
            voice()
            tick(run, state, clock_fn=clock_fn, **hooks)
        except Exception as exc:  # noqa: BLE001 — a failed recovery is journalled; supervision goes on
            state["last_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            log_line(run, "error %s; supervision goes on" % state["last_error"], clock_fn=clock_fn)
        save_state(run, state)
        if once:
            return 0
        (sleep_fn or time.sleep)(POLL_S)


def print_status(a, **hooks):
    print(gather_status(Path(a.run), **hooks))
