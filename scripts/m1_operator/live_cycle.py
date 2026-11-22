"""Live7 cycle actions. JSON output; 0 success, 2 refusal, 3 incomplete.

start-cycle enrolls the current clean SHA/DLL and existing named farmhand.
start-day supervises and chains days until STOP (--once completes one day).
wait-night, checkpoint, end-day, status and stop are individual actions.
STOP-write-only (WP P1): a retained STOP refuses every start; only tools/arti_rearm archives one. fix10f: a
recoverable (non-owner) runtime STOP with native input idle is re-armed through that same audited tool at the boundary.
No HOST input, deployment, save edits, restore, upload or approval-file edits.

Live-day terminals (arti_stardew_live7.day_terminal, shared with the trial report):
- LIVE7_DAY_VERIFIED: verified bed + fresh next morning = a successful day, even if rows failed.
- LIVE7_DAY_LOST: an honest lost day; the supervisor waits (bounded) for the native next morning; an
  end-of-night LevelUp/Shipping menu gets only ARTI's existing day-start rule (wait_lost_night).
- host_left: only when the cycle was enrolled with host_owner=human (mabar co-op). HOST process gone or an unbound actor ends the leg with that named reason (not an identity SAFETY_STOP). Harness-owned HOST (the default) still rejoins on HOST loss; lost-night HOST loss is still host_lost_during_lost_day. A different farmer, save, or bound main player stays SAFETY_STOP.
- hands_not_engaged (ADR 241 C5): a stated cause only: her farmhand window in front for more than 60 s, or two
  foreground refusals in a row from the mod. The cycle becomes not_recovered and drains the attempt locally so mabar
  can rejoin after a focus hand-off. Missing pad evidence 45 s after the first admission is one observed row
  (`hands_evidence_missing`), never a drain.
- menu_held_recover (ADR 237 C): a menu the runtime could not close is still held MENU_HELD_RECOVER_S after its first
  report. Same drain, same rejoin; the supervisor bounds the count.
- effect_ladder_recover (ADR 241 B rung 5): the runtime's effect clock passed rung 5 (nothing she did changed the game
  through rungs 1-4), or the runtime went silent past it. Same drain; the supervisor starts a fresh cycle (rung 6 after).
- anything else: refusal -> SAFETY_STOP. A stopped cycle refuses every later action cleanly.
Night checkpoint: copied only after the HOST SMAPI log shows the game's own "Context: after save"
for the new date; if that cannot be confirmed within NIGHT_SAVE_WAIT_S, the runner refuses.
Manual stage_save snapshots can still be labelled taken_while_running; they cannot complete a day.
Run manifest: start-cycle writes <cycle>/run_manifest.json (run_manifest.py: source SHA/dirty, farmhand + HOST DLL and
config hashes, sanitized flags, save, hostname, watcher webhook boolean); each start-day and chained day refreshes it.
A manifest failure never blocks the cycle (run_manifest_warning event).
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import argparse
import contextlib
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from live_watcher import Tail, read_json, safety, write_json, update_host_wait, host_wait_alert, ObservedPauseClock
from arti_stardew_day_start import DAY_START_HOST_WAIT_S
from live_save import scene_save_identity, resolve_save
sys.path.insert(0, str(HERE.parents[1]))
import arti_stardew_stop as stops
from scripts.live7.live7_common import CropMemory
from scripts.m1_operator.farmhand_focus import farmhand_in_front, focus_independent
from arti_stardew_live7_recovery import ProgressWatchdog


FARMHAND_FOREGROUND_LIMIT_S = 60
# c1 took about 10–30 s to reach its first skill. Give the admitted skill
# another 30 s plus 15 s for a fresh decision and pad watcher readback.
HANDS_ENGAGEMENT_GRACE_S = 45
# ADR 241 C5: the mod's explicit foreground refusals (ArmRejected detail); this many in a row drain the attempt.
FOREGROUND_REFUSAL_DETAILS = ("SEGMENT_INVALID:FOREGROUND", "background-only",
                              "FOREIGN_INPUT")  # ADR 244: an unsuppressible owner input in her window, named by the mod
FOREGROUND_REFUSALS_DRAIN = 2
# ADR 237 C: a menu the runtime could not close gets this long after its first report, then one bounded recovery.
MENU_HELD_RECOVER_S = 180
EFFECT_FILE_S = 5.0  # ADR 241 A: the status file of the effect clock is rewritten at most this often
# ADR 241 B rung 5: the runtime's own rung-5 row drains the attempt (like hands_not_engaged); a runtime silent this
# long past rung 5's bound on its last clock row drains it too. The supervisor starts a fresh cycle in the same game.
EFFECT_LADDER_DRAIN = "effect_ladder_recover"
EFFECT_LADDER_SILENT_S = 60.0
EFFECT_LADDER_TOLD = "ladder_told.json"  # beside the cycles: the next cycle's first page says what rung 5 did
TRIAL_IN_PROGRESS = {"starting", "active"}


class Refused(Exception):
    pass


class HandsNotEngaged(Exception):
    """The trial should drain cooperatively without a cycle-wide safety stop."""


class HostLeft(Exception):
    """The HOST process or connection went away. Not an identity safety fault."""

    def __init__(self, observed, day_saved=False, **info):
        self.observed = observed
        self.day_saved = bool(day_saved)
        self.info = info
        super().__init__("host_left:%s" % observed)


def unbound_actor(session):
    """Title/disconnect: empty name or actor 0. Not a different farmer."""
    if not isinstance(session, dict):
        return True
    name = session.get("player_name")
    pid = session.get("player_id")
    return (not isinstance(name, str) or not name.strip()
            or pid in (None, 0, "0", ""))


def foreign_farmhand(session, state):
    """A different named farmer, id, or bound main player while the world is ready.

    Unbound (empty name / id 0 at title or disconnect) is not a different farmer.
    A bound actor with is_main_player is not False stays farmhand_identity_mismatch.
    """
    if not isinstance(session, dict) or session.get("world_ready") is not True:
        return False
    if unbound_actor(session):
        return False
    return (session.get("player_name") != state.get("player_name")
            or session.get("player_id") != state.get("player_id")
            or session.get("is_main_player") is not False)


_MANIFEST = []


def manifest_module():
    """run_manifest.py of THIS checkout, by file path (other operator helpers prepend their own WORKTREE)."""
    if not _MANIFEST:
        import importlib.util
        spec = importlib.util.spec_from_file_location("live7_run_manifest", HERE / "run_manifest.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MANIFEST.append(module)
    return _MANIFEST[0]


class Stopped(Exception):
    pass


def matching_farmhand_exit(launch, exited):
    """A PID and wall clock cannot identify a process instance; legacy is ambiguous."""
    if not isinstance(launch, dict) or not isinstance(exited, dict):
        return False
    nonce = launch.get("launch_id")
    if (not isinstance(nonce, str) or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce)
            or exited.get("launch_id") != nonce
            or type(launch.get("pid")) is not int or launch["pid"] <= 0
            or type(exited.get("pid")) is not int or exited["pid"] != launch["pid"]
            or type(exited.get("returncode")) is not int):
        return False
    try:
        started = datetime.fromisoformat(launch["utc"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat(exited["utc"].replace("Z", "+00:00"))
        return started.tzinfo is not None and ended.tzinfo is not None and ended >= started
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def refuse_retained_stop(path, backend, session, *, current=None):
    """STOP-write-only (WP P1, R0-03 §9): the supervisor never archives a STOP. Any retained STOP refuses with its
    diagnosis; the operator archives only through tools/arti_rearm."""
    # ADR-19 item 1: the operator archives LIVE7_STOP sentinels with `tools/arti_rearm --live7-stop <path>`.
    try:
        raw, value = stops.read_stop(path)
    except OSError as exc:
        raise Refused(f"STOP unreadable at {path}; operator must inspect and retain it: {exc}") from exc
    if raw is None:
        return
    current = current if current is not None else session
    if not stops.scoped(value) or stops.pending(path, current):
        raise Refused(f"STOP current or unscoped at {path} ({stops.stop_class(value)}, writer "
                      f"{(value or {}).get('writer') if isinstance(value, dict) else None}); operator must inspect the stop, "
                      "confirm the target session has ended; never delete it")
    if not backend.ended_stop_scope(value, session):
        raise Refused(f"STOP targets an unknown live session at {path}; operator must establish "
                      "that it ended; never delete it")
    raise Refused(f"STOP of an ended session retained at {path}; the supervisor never archives it: "
                  "operator archives only via tools/arti_rearm; never delete it")


NIGHT_SAVE_WAIT_S = 600.0  # recovery/readback deadline; no next-day input before native save proof
SESSION_FAILURES = 4
SESSION_FAILURE_SECONDS = 5.0
SESSION_UNKNOWN_SECONDS = 15.0
# Risk #3 (lost-night menus). The night bound runs from the observed native night (a verified 02:00
# pass-out, a night-only screen, or the next date waiting for its save), never from an early day loss: it
# covers the night save and the day-start passes. Exceeding it is a named refusal, never a hang.
LOST_NIGHT_WAIT_S = 24 * 60.0
# Before the night (review F1): the rest of a lost day runs natively and its clock may pause (festival,
# event, HOST pause). That phase is bounded by NO native clock progress for this long (each advance of the
# native time resets it; the clock is capped at 02:00, so resets are finite). Operator notice at
# LOST_NIGHT_WAIT_S of stall.
LOST_DAY_STALL_S = 60 * 60.0
# Review R2-A: a slowly advancing clock resets the stall bound at every step (worst case ~120 h). This wall
# cap covers the whole lost-day + night wait from day_lost; exceeding it is lost_night_timeout (phase overall).
LOST_WAIT_TOTAL_S = 4 * 60 * 60.0
DAY_START_MENUS = ("LevelUpMenu", "ShippingMenu")  # the end-of-night screens the day-start rule clears
NIGHT_ONLY_MENUS = DAY_START_MENUS + ("SaveGameMenu",)  # seen only after the day ended (night signal)
DAY_START_PASSES = 2  # per lost night (soak.py DAY_START_TRIALS_MAX)
DAY_START_PASS_S = 600.0  # no verified progress; native save barriers are never forcibly closed
DAY_START_PASS_RESULTS = ("LIVE7_DAY_START_CLEARED", "LIVE7_DAY_START_IDLE")
# Save-name review MEDIUM #2: one 2.5 s scene sample after a rejoin can miss (mod reconnect lag, a
# capture-error frame). Bind the world ID in short bounded attempts: 4 x 2.5 s + 3 x 1 s ~= 13 s <= 15 s.
SAVE_IDENTITY_SAMPLES = 4
SAVE_IDENTITY_RETRY_S = 1.0
SAVE_IDENTITY_WAIT_S = 15.0


def day_terminal(report):
    """'verified' | 'lost' | None: the shared live-day contract (this checkout's runtime module)."""
    root = str(HERE.parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from arti_stardew_live7 import day_terminal as contract
    return contract(report)


def no_input_pass(report):
    """Review F3: a day-start pass report that proves zero published input and a released pad. A STOP
    (OWNER_STOPPED), an unconfirmed cleanup or any missing evidence is never excused."""
    if not isinstance(report, dict):
        return False
    post = report.get("post_row_state")
    return (report.get("status") not in ("OWNER_STOPPED", None)
            and type(report.get("physical_inputs")) is int and report["physical_inputs"] == 0
            and isinstance(post, dict) and post.get("input_released") is True
            and report.get("cleanup") != "UNCONFIRMED")


def date_key(value):
    return (int(value["year"]), str(value["season"]).lower(), int(value["day"]))


def tomorrow(value):
    year, season, day = date_key(value)
    seasons = ("spring", "summer", "fall", "winter")
    if day == 28:
        index = seasons.index(season)
        return year + (index == 3), seasons[(index + 1) % 4], 1
    return year, season, day + 1


CONTAINMENT_TOKEN = "ARTI_BG_CONTAINMENT"


SMAPI_SEGMENT_CHUNK = 1 << 20


def _segment_total(row):
    """Bytes of the live log covered by a saved row (legacy full copies carry only `bytes`)."""
    total = row.get("total_bytes", row.get("bytes"))
    return total if isinstance(total, int) and total >= 0 else 0


def _segment_ref(cycle_root, process_dir, row):
    return Path(process_dir).relative_to(cycle_root).as_posix() + "/" + row["saved"]


def smapi_log_segments(cycle_root, role):
    """Saved SMAPI log pieces of one role in this cycle, oldest first: [(process_dir, manifest_row)].

    Day folders, attempts and process ids are zero-padded/nanosecond names, so path order is time order."""
    cycle_root = Path(cycle_root)
    rows = []
    for logs_path in sorted(cycle_root.glob("day-*/process/*/smapi_logs.json")):
        doc = read_json(logs_path, {})
        for row in (doc.get("logs") or []) if isinstance(doc, dict) else []:
            if isinstance(row, dict) and row.get("role") == role and row.get("saved"):
                rows.append((logs_path.parent, row))
    return rows


def save_smapi_log_segment(source, target, cycle_root, previous):
    """DISK-A (a51): write only the bytes of `source` that the newest saved segment does not already hold.

    SMAPI appends to one log for the life of a game process, so every snapshot of a long session used to
    re-copy the whole cumulative log (26.6x redundancy over 26 days, growing with the square of days). The
    newest segment's full-file sha256 must equal the hash of the same-length prefix of the live file;
    then the new segment starts at that offset and names the segment it continues. A rotated or truncated
    log (farmhand relaunch) fails the prefix check and is saved whole as a new offset-0 segment, which is
    the original purpose of the snapshot. Returns the manifest fields of the saved segment."""
    prev_dir, prev = previous[-1] if previous else (None, None)
    prev_total = _segment_total(prev) if prev else 0
    full = hashlib.sha256()
    with ipc.open_shared(Path(source), "rb") as stream:
        offset = 0
        if prev is not None and prev_total:
            prefix, remaining = hashlib.sha256(), prev_total
            while remaining:
                chunk = stream.read(min(SMAPI_SEGMENT_CHUNK, remaining))
                if not chunk:
                    break
                prefix.update(chunk)
                remaining -= len(chunk)
            if not remaining and prefix.hexdigest() == prev.get("sha256"):
                offset, full = prev_total, prefix
            else:
                stream.seek(0)
        segment, written = hashlib.sha256(), 0
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        with Path(target).open("wb") as out:
            while True:
                chunk = stream.read(SMAPI_SEGMENT_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                full.update(chunk)
                segment.update(chunk)
                written += len(chunk)
    return dict(saved=Path(target).name, bytes=written, offset=offset, total_bytes=offset + written,
                sha256=full.hexdigest(), segment_sha256=segment.hexdigest(),
                continues=_segment_ref(cycle_root, prev_dir, prev) if offset else None)


def reconstruct_smapi_log(cycle_root, role, destination, *, process=None):
    """Rebuild one role's full SMAPI log as it stood at a saved snapshot (default: the newest) by following
    the `continues` chain back to an offset-0 segment. Legacy full copies are a chain of one. The result is
    verified against the snapshot's recorded length and sha256 before it is reported."""
    cycle_root, destination = Path(cycle_root), Path(destination)
    segments = smapi_log_segments(cycle_root, role)
    by_ref = {_segment_ref(cycle_root, d, row): (d, row) for d, row in segments}
    if process is not None:
        segments = [s for s in segments if s[0].name == process]
    if not segments:
        raise Refused("no saved SMAPI log segment for role " + role)
    chain, current = [], segments[-1]
    while True:
        chain.append(current)
        row = current[1]
        if not row.get("offset"):
            break
        if row.get("continues") not in by_ref or len(chain) > len(by_ref):
            raise Refused("broken SMAPI segment chain at " + str(row.get("continues")))
        current = by_ref[row["continues"]]
    chain.reverse()
    digest, total = hashlib.sha256(), 0
    with destination.open("wb") as out:
        for process_dir, row in chain:
            if (row.get("offset") or 0) != total:
                raise Refused("SMAPI segment offset mismatch at " + str(process_dir))
            data = (process_dir / row["saved"]).read_bytes()
            out.write(data)
            digest.update(data)
            total += len(data)
    final = chain[-1][1]
    if total != _segment_total(final) or digest.hexdigest() != final.get("sha256"):
        raise Refused("reconstructed SMAPI log does not match the recorded snapshot")
    return dict(role=role, bytes=total, sha256=digest.hexdigest(), segments=len(chain),
                snapshot=chain[-1][0].name, destination=str(destination))


def containment_hits(run_dir, since=None):
    """Read-only scan of the farmhand's raw stderr files for our own containment FailFast.

    Live7 rehearsal 9: the mod's watchdog ends the farmhand with Environment.FailFast
    ("ARTI_BG_CONTAINMENT:<trigger>|request=..|runtime=..") and that text reaches only the
    farmhand-stderr-*.raw files prep_trial writes into the run folder, never the SMAPI log.

    ``since`` (wall epoch seconds, the cycle start): a marker file last written before it belongs
    to an earlier cycle and is not counted (``older`` counts the skipped files). A farmhand
    launched before the cycle that fails during it rewrites its file, so mtime is the test.
    """
    hits, unreadable, older = [], [], 0
    for raw in sorted(Path(run_dir).glob("farmhand-stderr-*.raw")):
        try:
            stat = raw.stat()
            if since is not None and stat.st_mtime < since:
                older += 1
                continue
            data = ipc.read_bytes(raw)
        except OSError as exc:
            unreadable.append(dict(file=raw.name, error=type(exc).__name__))
            continue
        utf16 = CONTAINMENT_TOKEN.encode("utf-16-le") in data
        text = data.decode("utf-16-le" if utf16 else "utf-8", errors="replace")
        lines = [line.strip()[:500] for line in text.splitlines() if CONTAINMENT_TOKEN in line]
        if lines:
            hits.append(dict(file=raw.name, source=str(raw), bytes=len(data), mtime=stat.st_mtime,
                             sha256=hashlib.sha256(data).hexdigest(), lines=lines))
    out = dict(hits=hits, unreadable=unreadable)
    if since is not None:
        out.update(since=since, older=older)
    return out


class Backend:
    """Production effects are confined here. Nothing happens on import."""
    def __init__(self):
        import m1_paths as paths
        import witness_run as witness
        import stage_save
        import stardew_m1_trial as trial
        self.paths, self.witness, self.saves, self.trial = paths, witness, stage_save, trial
        self.live = witness.Live()

    def identity(self):
        if self.paths.WORKTREE.resolve() != HERE.parents[1]:
            raise Refused("ARTI_M1_WORKTREE must be the checkout containing this supervisor")
        return self.trial.identity(root=self.paths.WORKTREE, artifact=self.paths.MOD / "ArtiStardewBridge.dll")

    def porcelain(self):
        from scripts.livepath.source_drift import porcelain_of
        try:
            return porcelain_of(self.paths.WORKTREE)
        except Exception:
            return ""

    def session(self):
        value = self.live.session()
        return value if isinstance(value, dict) else {}

    def save_identity(self, session):
        if self.live.trial_running():
            raise Refused("native_save_identity_port_in_use")
        diagnostics = {}
        self.witness.PS.sample(diagnostics=diagnostics)
        current = self.session()
        if any(current.get(k) != session.get(k) for k in
               ("session_id", "runtime_instance_id", "player_id", "screen_id", "world_ready")):
            raise Refused("native_save_session_changed")
        try:
            return scene_save_identity(diagnostics.get("scene_identity"), session)
        except ValueError as exc:
            raise Refused(str(exc)) from None

    def resolve_save(self, state):
        try:
            return resolve_save(state["saves_root"], state["save_id"])
        except ValueError as exc:
            raise Refused(str(exc)) from None

    def ended_stop_scope(self, stop, current):
        """A different ID alone is not proof that a foreign runtime is dead.

        Same-runtime rotation is native evidence. Across runtimes require the
        matching historical attempt and its captured launch/exit pair, bound by the
        supervisor's unique launch_id. Legacy pairs without that identity block. Reconnect's
        exit is written at that attempt's RUN root, after its process snapshot. This
        never upgrades a reused PID or clock skew into exit evidence.
        """
        if current.get("world_ready") is not True or not stops.scoped({"protocol": 1, **current}):
            return None
        if stop["session_id"] == current["session_id"]:
            return None
        if stop["runtime_instance_id"] == current["runtime_instance_id"]:
            return dict(basis="native_session_rotated", current_scope=stops.scope(current))
        roots = {self.paths.COMMAND_DIR.parent, self.paths.RUN, self.paths.RUN.parent}
        states = {path for root in roots for pattern in ("*/live7/*/cycle_state.json", "live7/*/cycle_state.json")
                  for path in root.glob(pattern)}
        for state_path in sorted(states):
            state = read_json(state_path, {})
            if not isinstance(state, dict):
                continue
            for day in state.get("days", []):
                if not isinstance(day, dict):
                    continue
                for attempt in day.get("attempts", []):
                    if not isinstance(attempt, dict) or attempt.get("session") != stop["session_id"]:
                        continue
                    recorded_scope = attempt.get("stop_scope") or {}
                    if not isinstance(recorded_scope, dict):
                        continue
                    if recorded_scope.get("runtime_instance_id", stop["runtime_instance_id"]) != stop["runtime_instance_id"]:
                        continue
                    # Resolve from the actual evidence tree, never an embedded absolute path.
                    ident = attempt.get("id", "")
                    if not ident or ident in {".", ".."} or Path(ident).name != ident:
                        continue
                    for process in (state_path.parent / ident / "process").glob("*"):
                        launch = read_json(process / "farmhand-launch.json", {})
                        # Resolve from the discovered historical RUN, never from a stored
                        # absolute path or an unrelated new run's exit record.
                        for exit_path in (process / "farmhand-exit.json", state_path.parents[2] / "farmhand-exit.json"):
                            exit = read_json(exit_path, {})
                            if matching_farmhand_exit(launch, exit):
                                return dict(basis="recorded_farmhand_exit", pid=launch["pid"],
                                            launch_id=launch["launch_id"],
                                            launch_utc=launch["utc"], utc=exit["utc"], evidence=str(process),
                                            exit_evidence=str(exit_path), session_id=stop["session_id"])
        return None

    def host_log_cursor(self):
        try:
            return self.paths.HOST_SMAPI_LOG.stat().st_size
        except OSError:
            return None

    def host_disconnect(self, attempt, player_id):
        """Only a quit for this actor AFTER attempt start corroborates an unreadable session."""
        offset = attempt.get("host_log_cursor")
        if offset is None:
            return dict(confirmed=False, reason="host_log_baseline_unavailable")
        try:
            with ipc.open_shared(self.paths.HOST_SMAPI_LOG, "rb") as log:
                if os.fstat(log.fileno()).st_size < offset:
                    return dict(confirmed=False, reason="host_log_rotated")
                log.seek(offset)
                lines = log.read().decode("utf-8-sig", errors="replace").splitlines()
        except OSError:
            return dict(confirmed=False, reason="host_log_unreadable")
        token = "Player quit: " + str(player_id)
        return dict(confirmed=any(line.endswith(token) for line in lines), basis="host_player_quit")

    def farmhand_alive(self):
        rec = read_json(self.paths.FARMHAND_LAUNCH, {})
        if not rec.get("pid"):
            raise Refused("farmhand launch identity missing")
        exited = self.witness.wf.wait_pid_exit(rec["pid"], 0)
        if exited is None:
            raise Refused("farmhand process access denied or unknown")
        return not exited

    def farmhand_in_front(self):
        """True when the foreground window belongs to the farmhand process (the owner is using it, as in
        soak.py day_start), False when not, None when unknown. Read-only; nothing is focused or closed.
        Review R2-C: no foreground window at all (hwnd 0: locked screen, secure desktop, idle desktop) is
        provably not the farmhand, so False; a failed or unreadable read stays None (unknown, skip)."""
        try:
            return farmhand_in_front(self.paths.FARMHAND_LAUNCH, self.witness.wf.foreground_app)
        except Exception:
            return None  # unknown: the read itself failed

    def trial_alive(self, attempt):
        return self.witness.wf.pid_alive(attempt["pid"])

    def host_alive(self):
        wf = self.witness.wf
        fh = read_json(self.paths.FARMHAND_LAUNCH, {}).get("pid")
        hwnd = self.live.host_hwnd()
        window = wf.window(hwnd)
        if (window.get("exists") and window.get("pid") != fh
                and wf.exe_name(window["pid"]) in self.witness.GAME_IMAGES):
            return True
        # Owner reload may create a new HWND; identify the only separate game.
        windows = [w for w in wf.enum_windows() if w.get("pid") != fh
                   and "[farmhand]" not in w.get("title", "")
                   and (w.get("title", "").startswith("Stardew Valley") or "running SMAPI" in w.get("title", ""))
                   and wf.exe_name(w["pid"]) in self.witness.GAME_IMAGES]
        if len(windows) == 1:
            ipc.atomic_write(self.paths.HOST_HWND_FILE, str(windows[0]["hwnd"]))
            return True
        return False

    def ready(self, state, *, any_screen=False):
        """The game answers, she is the enrolled farmhand, the world and the clock are in range. ``any_screen`` (ADR 241
        C6, the attempt start and a rejoin): whatever is on screen is her first page, never a reason to refuse. The
        night readback keeps the strict form: the lost-night pass reads the end-of-night menu from its refusal."""
        session = self.session()
        if (session.get("player_name") != state["player_name"]
                or session.get("player_id") != state["player_id"]
                or session.get("is_main_player") is not False):
            raise Refused("farmhand_identity_mismatch")
        try:
            out, rc = self.witness.cmd_wait_ready(argparse.Namespace(who="farmhand", max_hour=26, timeout=30,
                                                                     any_screen=any_screen), self.live)
        except self.witness.Refused as exc:
            raise Refused(str(exc)) from None
        if rc:
            raise Refused("farmhand_not_ready:" + json.dumps(out.get("reasons")))
        return self.live.pose()

    def launch(self, slot, stop_file=None, *, expected=None, held=None):
        from scripts.livepath import halt_authority as HA, mabar
        runtime = Path(self.paths.RUN)
        run = runtime.parent if runtime.name == "runtime" else runtime
        try:
            return mabar.recover_farmhand(run, slot, stop_file=stop_file, expected=expected, held=held,
                                          env=dict(os.environ, ARTI_M1_COMMAND_DIR=str(self.paths.COMMAND_DIR)))
        except HA.RecoveryTimedOut:
            return False  # consumes one of main's bounded recovery tries
        except HA.HandsHeld:
            raise
        except RuntimeError as exc:
            raise Refused(str(exc)) from None

    def approve(self, cycle, state, attempt):
        import prep_trial as prep
        with contextlib.redirect_stdout(sys.stderr):
            prep.cmd_approval(argparse.Namespace(live7=True, identity_file=str(cycle / "identity.json"),
                output=attempt["approval"], journal=attempt["journal"], cycle=cycle.name,
                day_trial=attempt["id"], resume_file=attempt.get("resume_file"), inputs=None))

    def start_trial(self, attempt):
        argv = [str(self.paths.PY), "-B", str(self.paths.WORKTREE / "scripts/stardew_m1_trial.py"),
                "--mode", "combined", "--skills", "--approval", attempt["approval"],
                "--artifact", str(self.paths.MOD / "ArtiStardewBridge.dll"), "--config", str(self.paths.TRIAL_CONFIG),
                "--report", attempt["report"], "--stop-file", attempt["stop_file"],
                "--stop-file", str(Path(attempt["approval"]).parent.parent / "LIVE7_STOP"),
                "--stop-file", attempt["live7_stop_file"],
                "--live7-stop-file", attempt["live7_stop_file"],
                "--live7-help-dir", str(self.paths.RUN.resolve())]
        if attempt.get("day_start_only"):
            argv.append("--live7-day-start-only")  # risk #3: the existing day-start rule only, no owner turn
        with Path(attempt["stdout"]).open("xb") as log:
            return subprocess.Popen(argv, cwd=self.paths.WORKTREE, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def preserve_process_evidence(self, attempt):
        """Snapshot rotating records/logs before another farmhand launch overwrites them.

        Rehearsal 1 kept only SMAPI-latest.txt, which is the HOST's log (the HOST starts first; the
        farmhand's second SMAPI instance writes SMAPI-latest.player-2.txt). Both are kept, labelled.
        """
        import hashlib
        root = Path(attempt["approval"]).parent / "process" / str(time.time_ns())
        for source in (self.paths.FARMHAND_LAUNCH, self.paths.FARMHAND_EXIT):
            try:
                raw = ipc.read_bytes(source)
            except FileNotFoundError:
                continue
            root.mkdir(parents=True, exist_ok=True)
            (root / source.name).write_bytes(raw)
        logs = []
        cycle_root = root.parents[2]
        for role, attr in (("farmhand", "FARMHAND_SMAPI_LOG"), ("host", "HOST_SMAPI_LOG")):
            source = getattr(self.paths, attr, None)
            if source is None:
                continue
            row = dict(role=role, source=str(source))
            name = "smapi-%s-%s" % (role, Path(source).name)
            try:
                # DISK-A: only the bytes since the newest saved segment of this log; see save_smapi_log_segment.
                saved = save_smapi_log_segment(Path(source), root / name, cycle_root,
                                               smapi_log_segments(cycle_root, role))
            except OSError as exc:
                logs.append(dict(row, saved=None, error=type(exc).__name__))
                continue
            logs.append(dict(row, **saved))
        if logs:
            root.mkdir(parents=True, exist_ok=True)
            write_json(root / "smapi_logs.json", dict(logs=logs, segments=(
                "offset/total_bytes/continues: a segment holds live-log bytes [offset, total_bytes); sha256 is "
                "the whole log up to total_bytes; rebuild with `live_cycle.py smapi-log`"), role_basis=(
                "SMAPI instance log names: HOST starts first (SMAPI-latest.txt), the farmhand second "
                "(SMAPI-latest.player-2.txt); overridable by ARTI_HOST_SMAPI_LOG / ARTI_FARMHAND_SMAPI_LOG")))
        # Rehearsal 9: a containment FailFast is only visible in the farmhand's raw stderr.
        run_dir = getattr(self.paths, "RUN", None) or Path(self.paths.FARMHAND_LAUNCH).parent
        containment = containment_hits(run_dir, since=attempt.get("cycle_started_at"))
        if containment["hits"] or containment["unreadable"]:
            root.mkdir(parents=True, exist_ok=True)
            write_json(root / "containment.json", dict(containment, run_dir=str(run_dir), token=CONTAINMENT_TOKEN))
        return containment

    def launch_record(self):
        """Raw bytes of the current farmhand-launch.json (the session about to be replaced), or None."""
        try:
            return ipc.read_bytes(self.paths.FARMHAND_LAUNCH)
        except OSError:
            return None

    def preserve_exit_proof(self, attempt, launch_raw):
        """Live7 risk #2: a non-crash drop's farmhand is still alive when the rejoin snapshot runs; restart
        asks it to exit safely and only then records the exit, while the new launch overwrites farmhand-launch.json.
        Keep the OLD launch with its own exit as one pair in the interrupted attempt, so start-day's
        ended_stop_scope can prove that session ended. PID, launch_id and clock order must all match."""
        try:
            launch = json.loads(launch_raw.decode("utf-8-sig")) if launch_raw else None
            exit_raw = ipc.read_bytes(self.paths.FARMHAND_EXIT)
            exit = json.loads(exit_raw.decode("utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            return None
        if not matching_farmhand_exit(launch, exit):
            return None
        root = Path(attempt["approval"]).parent / "process" / (str(time.time_ns()) + "-rejoin-exit")
        try:
            root.mkdir(parents=True, exist_ok=False)
            (root / "farmhand-launch.json").write_bytes(launch_raw)
            (root / "farmhand-exit.json").write_bytes(exit_raw)
        except OSError:
            # Review F6: runs in rejoin's finally; never mask the launch result. No proof = an old STOP
            # stays unproven and start-day refuses it by name.
            return None
        return dict(pid=launch["pid"], launch_id=launch["launch_id"],
                    launch_utc=launch["utc"], exit_utc=exit["utc"], evidence=str(root))

    def start_guard(self, cycle):
        stop = cycle / "WATCH_STOP"
        if stop.exists():
            stop.unlink()
        # pad_watch follows m1_paths.SMAPI_LOG; in rehearsal 1 that was the HOST's log (0 PAD lines in
        # 256 rows). Point it at the farmhand's own SMAPI log, where ARTI's PAD_MODE lines are written.
        env = dict(os.environ, ARTI_SMAPI_LOG=str(getattr(self.paths, "FARMHAND_SMAPI_LOG", self.paths.SMAPI_LOG)))
        with ipc.open_shared(cycle / "pad_watch_stdout.txt", "ab") as log:
            return subprocess.Popen([str(self.paths.PY), "-B", str(HERE / "pad_watch.py"), "watch",
                "--interval", "0.5", "--quiet", "--exit-on-violation", "--stop-file", str(stop),
                "--violation-stop-file", str((self.paths.RUN / "LIVE7_STOP").resolve()),
                "--cycle-id", str(cycle.resolve()),
                "--out", str(cycle / "pad_watch.jsonl")], stdout=log, stderr=subprocess.STDOUT, env=env,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def save_date(self, state):
        import xml.etree.ElementTree as ET
        if not state.get("save_name") or not state.get("save_folder"):
            return None
        try:
            return self.saves.facts(ipc.read_bytes(Path(state["save_folder"]) / state["save_name"]))["date"]
        except (OSError, ValueError, ET.ParseError):
            return None  # the nightly rename/write may briefly hide or lock the save

    def night_save(self, state, date):
        """The game's own night save for ``date``, read from the HOST SMAPI log (read-only)."""
        return self.saves.night_save_evidence(self.paths.HOST_SMAPI_LOG, date)

    def checkpoint(self, state, target, date, night_save=None):
        try:
            return self.saves.checkpoint_save(state["save_folder"], target, date, night_save=night_save)
        except self.saves.Refused as exc:
            raise Refused(str(exc)) from None

    def sleep(self, seconds):
        time.sleep(seconds)


class Cycle:
    def __init__(self, folder, backend, *, clock=time.time):
        self.folder, self.backend, self.clock = Path(folder).resolve(), backend, clock
        self.state_path = self.folder / "cycle_state.json"
        self.state = read_json(self.state_path, {})
        self.game_clock = ObservedPauseClock(clock, self.state.get("game_pause_clock", {}))
        self.tail = Tail()
        self.guard = None
        self._first_skill_at = self._last_pad_engaged_at = self._pad_focus_since = self._last_pad_at = None
        self._hands_engaged = False
        # ADR 241 C5: the stated causes the hands guard drains on, and what it only reports.
        self._first_admission = self._last_pad_row = self._last_foreground_refusal = None
        self._foreground_refusals = 0
        self._hands_evidence_said = False
        # ADR 241 C5 + ADR 244: a focus-independent DLL (session.json focus_independent: true) serves her whether her
        # window is active or not, so "her window in front" is no cause there. Absent or false (an older DLL): as before.
        self._focus_independent = False
        self.child = self.attempt = None
        self.night_wait_reason = self.night_clock = None

    def save(self):
        self.state["game_pause_clock"] = self.game_clock.state
        write_json(self.state_path, self.state)

    def refresh_manifest(self, phase):
        """Write/refresh <cycle>/run_manifest.json (run_manifest.py). Never blocks the cycle: any failure is
        journalled as run_manifest_warning (error type only) and the cycle continues."""
        try:
            run_manifest = manifest_module()
            run_manifest.refresh(self.folder, source_root=HERE.parents[1],
                                 paths=getattr(self.backend, "paths", None), state=self.state, phase=phase,
                                 enrolled=read_json(self.folder / "identity.json", {}), clock=self.clock)
        except Exception as exc:  # noqa: BLE001 - tolerance is the contract here
            try:
                self.event("run_manifest_warning", phase=phase, error=type(exc).__name__,
                           message="run_manifest.json not written; the cycle continues.")
            except Exception:  # noqa: BLE001
                pass

    def cycle_started_at(self):
        """Wall epoch seconds of the cycle start: the state field, else the first cycle_started event
        (cycles enrolled before the field existed); None when neither is readable (no filter)."""
        value = self.state.get("started_at")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        try:
            with ipc.open_shared(self.folder / "cycle_events.jsonl", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("ev") == "cycle_started":
                        at = row.get("at")
                        return at if isinstance(at, (int, float)) and not isinstance(at, bool) else None
        except OSError:
            return None
        return None

    def status(self):
        current = self.state.get("days", [])[-1:]
        status = self.state.get("status")
        held = self.state.get("menu_held") if status in TRIAL_IN_PROGRESS else None
        return dict(cycle=self.folder.name, status="menu_held" if held else status,
            **({"menu": held["menu"], "recoverable": True} if held else {}),
            completed_days=sum(d.get("status") == "ended" for d in self.state.get("days", [])),
            current_day=({k: current[0].get(k) for k in ("index", "date", "status", "next_date", "checkpoint")}
                         if current else None),
            attempts=len(current[0].get("attempts", [])) if current else 0,
            identity=read_json(self.folder / "identity.json"), state_file=str(self.state_path),
            stop_requested=any(stops.pending(p, self.stop_scope()) for p in self.stop_files()))

    def stop_scope(self):
        return dict(self.state.get("stop_scope") or {}, cycle_id=str(self.folder))

    def hands(self, act, *, water=False):
        """ADR 254: the HALT table (`halt_authority.decide`) on this moment's evidence: the backend's session (the
        same read this cycle acts on) and the mod's halt.json, stop files and exit record. A close or a launch is a
        recovery: the stops that outrank it are read too. None without a native inbox (test backends)."""
        folder = getattr(getattr(self.backend, "paths", None), "COMMAND_DIR", None)
        if folder is None:
            return None
        from scripts.livepath import halt_authority as HA
        run_dir = Path(self.backend.paths.RUN)
        run = run_dir.parent if run_dir.name == "runtime" else None
        expected = getattr(self, "_enroll_expected", None) or HA.expected_identity(self.state) or None
        evidence = HA.read_evidence(folder, run, recovery=run is not None, sentinels=run is not None, expected=expected)
        if run is None:
            operator_markers = [self.folder / "OPERATOR_STOP", *(run_dir / "live7").glob("c*/OPERATOR_STOP")]
            for path in dict.fromkeys(operator_markers):
                record = HA.read_record(path)
                if record[0] is not None:
                    evidence.sentinels[path] = record
            launch_file = getattr(self.backend.paths, "FARMHAND_LAUNCH", None)
            if launch_file is not None:
                evidence.launch = HA.read_launch_record(launch_file)
                evidence.prior_launch |= evidence.launch[0] is not None
        if not isinstance(self.backend, Backend):
            evidence.session = (b"backend", self.backend.session())
        held = getattr(self, "_hands_held", None)
        if getattr(self, "_crash_wait", False) and act == "launch":
            successor = HA.manual_crash_successor(evidence, expected or {})
            if successor is not None and self.backend.farmhand_alive():
                evidence.expected = successor
                decision = HA.decide(evidence, act="observe")
                if decision.action == "nothing" and not decision.sentinels:
                    try:
                        save = self.backend.save_identity(decision.session)
                    except (Refused, OSError, ValueError):
                        pass
                    else:
                        if str(save.get("save_id")) == str(successor.get("save_id")):
                            self.state.update(stop_scope=stops.scope(decision.session), save_identity=save)
                            self._manual_crash_rejoined = True
                            self.save()
                            return decision
                evidence.expected = expected or {}
        watchdog = HA.watchdog_launch_proof(evidence) if act == "launch" and self.backend.host_alive() else None
        return HA.decide(evidence, act=act, held=held, water=water, watchdog=watchdog)

    def hands_held(self, decision, boundary):
        """One `hands_held` event per held episode (or reason); the binding ends only on the operator's receipt."""
        def note(event, **fields):
            if self.state:
                self.event(event, boundary=boundary, **fields)
            else:
                # An enrollment that waits or refuses must leave the proposed cycle directory empty.
                from scripts.livepath import mabar
                runtime = Path(self.backend.paths.RUN)
                mabar.log(runtime.parent if runtime.name == "runtime" else runtime, event,
                          cycle=self.folder.name, boundary=boundary, **fields)

        if decision is None or decision.action != "hold":
            if getattr(self, "_hands_alerted", None) is not None:
                note("hands_resumed", held=getattr(self, "_hands_held", None))
            self._hands_held = self._hands_alerted = None
            return False
        if decision.held:
            self._hands_held = decision.held
        marker = (decision.held or {}).get("episode_id") or decision.reason
        if marker != getattr(self, "_hands_alerted", None):
            self._hands_alerted = marker
            from scripts.livepath.halt_authority import claim_hold_alert
            runtime = Path(self.backend.paths.RUN)
            run = runtime.parent if runtime.name == "runtime" else None
            note("hands_held", reason=decision.reason, held=decision.held,
                 alert=claim_hold_alert(run, decision, fallback=True))
        return True

    def wait_for_hands(self, act, boundary):
        """ADR 254 (review R6 B1/B4): before a trial starts or her game is closed or relaunched, the table decides; while
        it holds, nothing is started, closed or launched, every stop still ends the wait (`check`), and the wait ends
        by itself on the operator's re-arm of the held episode."""
        from scripts.livepath.halt_authority import operator_stop
        while True:
            decision = self.hands(act)
            if not self.hands_held(decision, boundary):
                return
            if operator_stop(decision):
                self.check()  # preserve the pad-watch classification if its marker landed during this read
                raise Stopped(decision.reason)
            self.check()
            self.backend.sleep(1)

    def reconcile_stops(self, session, boundary):
        """Wait at this boundary while held and return the sample that cleared it."""
        from scripts.livepath import halt_authority as HA
        command_dir = getattr(self.backend.paths, "COMMAND_DIR", None)
        attempted_water = False
        while True:
            decision = self.hands("start", water=not attempted_water)
            if HA.present(self.folder / "OPERATOR_STOP") or (decision is not None and HA.operator_stop(decision)):
                if decision is not None:
                    self.hands_held(decision, boundary)
                raise Stopped("operator_stop" if decision is None else decision.reason)
            if decision is not None and decision.action == "rearm":
                attempted_water = True
                self.recover_nonowner_stop(Path(command_dir), decision.session, boundary, expect=decision.expect)
                continue
            paths = self.stop_files()
            if command_dir is not None and (decision is None or decision.action != "hold"):
                paths = [Path(command_dir) / "stop.json"] + paths
            current = decision.session if decision is not None and decision.session is not None else session
            scoped = dict(current or {}, cycle_id=str(self.folder))
            for path in dict.fromkeys(paths):
                try:
                    refuse_retained_stop(path, self.backend, current, current=scoped)
                except stops.StopRefused as exc:
                    raise Refused(f"{exc} ({boundary})") from exc
            if not self.hands_held(decision, boundary):
                return current
            if self.state:
                self.check()
            self.backend.sleep(1)
            session = self.backend.session()

    def recover_nonowner_stop(self, folder, session, boundary, *, expect=None):
        """fix10f (a15 day 13): a stop.json from a recoverable (non-owner) writer never blocks a start.

        Only when native shows that exact STOP latched and input idle: the audited tools/arti_rearm flow archives
        it (never deletes) and re-arms, recorded as a RECOVER fault with its evidence. Owner hard stops, unknown
        provenance and anything with native held evidence are left untouched and refuse below as before."""
        path = folder / "stop.json"
        try:
            raw, value = stops.read_stop(path)
        except (OSError, stops.StopRefused):
            return
        if raw is None or stops.stop_class(value) != "recoverable":
            return
        blocker = stops.recoverable_native_state(session, raw)
        evidence = dict(writer=value.get("writer"), created_at=value.get("created_at"),
                        stop_sha256=hashlib.sha256(raw).hexdigest(), boundary=boundary,
                        input_busy=session.get("input_busy"), release_acked=session.get("release_acked"),
                        halt=(session.get("halt") or {}).get("episode_id"))
        if blocker is not None:
            self.event("nonowner_stop_retained", blocker=blocker, **evidence)
            return
        from tools import arti_rearm

        def confirm(halt, _notes):
            fresh = arti_rearm.read_session(folder)  # R1 again: native evidence at the moment of confirmation
            authorization = self.hands("start", water=True)
            return (halt is not None and fresh.get("halt") == halt
                    and authorization is not None and authorization.action == "rearm"
                    and authorization.expect == expect == arti_rearm._key(halt)
                    and stops.recoverable_native_state(fresh, raw) is None
                    and path.exists() and ipc.read_bytes(path) == raw)
        try:
            outcome = arti_rearm.rearm(folder, "rig_operator", confirm, out=lambda _s: None, sleep=self.backend.sleep,
                                      automatic=True)
        except stops.StopRefused as exc:
            outcome = "refused:" + str(exc)[:80]
        self.event("FAULT", type="nonowner_stop_recovered" if outcome == "rearmed" else "nonowner_stop_retained",
                   cause=value.get("writer"), disposition="RECOVER", outcome=outcome, refs={}, observed=evidence)

    def stop_files(self):
        paths = [self.folder / "LIVE7_STOP"]
        run_stop = self.state.get("live7_stop_file")
        if run_stop:
            paths.insert(0, Path(run_stop))
        elif getattr(self.backend, "paths", None):
            paths.insert(0, self.backend.paths.RUN / "LIVE7_STOP")
        return paths

    def request_stop(self, *, source):
        created = False
        if source == "operator_stop":
            created = stops.write_sentinel(self.folder / "OPERATOR_STOP", self.stop_scope(), writer="supervisor." + source)
        # The separate trial consumes the shared RUN marker at each boundary and in its 20 ms hook.
        for path in self.stop_files():
            created |= stops.write_sentinel(path, self.stop_scope(), writer="supervisor." + source)
        if created:
            self.event("live7_stop_file_created", source=source)

    def event(self, ev, **details):
        row = dict(ev=ev, at=self.clock(), domain="stardew", cycle=self.folder.name, **details)
        if ev == "FAULT" and details.get("disposition") == "RECOVER":
            history = self.state.setdefault("hints", {}).setdefault("recovery_history", [])
            history.append({k: details[k] for k in ("type", "cause", "refs", "observed", "disposition") if k in details})
            del history[:-8]
            self.save()
        ipc.append_text(self.folder / "cycle_events.jsonl", json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False), file=sys.stderr, flush=True)

    def source_porcelain(self):
        """Git porcelain of the running checkout. Fakes set porcelain_text / porcelain()."""
        fn = getattr(self.backend, "porcelain", None)
        if callable(fn):
            try:
                return fn() or ""
            except Exception:
                return ""
        text = getattr(self.backend, "porcelain_text", None)
        if text is not None:
            return text
        return ""

    def _note_pad(self, row):
        at = row.get("t")
        if type(at) not in (int, float) or not math.isfinite(at):
            return
        self._last_pad_at = at
        self._last_pad_row = {k: row.get(k) for k in ("t", "fh_fg", "pin", "owned", "armed", "gamepad_mode")}
        if row.get("fh_fg") is not True:
            self._pad_focus_since = None
        elif self._pad_focus_since is None or at < self._pad_focus_since:
            self._pad_focus_since = at
        if row.get("pin") is True or row.get("owned") is True or row.get("armed"):
            self._last_pad_engaged_at = at
            self._note_pad_engagement()

    def _note_pad_engagement(self):
        # ADR 241 C5: pad evidence after the first admission proves her hands whenever it comes (late evidence is no
        # longer a fault, so it must still end "starting"); evidence from before the admission does not count.
        if self._first_skill_at is not None and self._last_pad_engaged_at is not None:
            self._hands_engaged |= self._last_pad_engaged_at - self._first_skill_at >= 0

    def _note_action_result(self, row):
        """ADR 241 C5: the mod's own foreground refusals, read from the action journal (c75873617 keeps ArmRejected's
        detail). Only an explicit foreground detail counts; a bare ArmRejected is not a stated cause. Any other action
        result ends the run of refusals."""
        action = row.get("action") if isinstance(row.get("action"), dict) else {}
        detail = str(action.get("refusal_detail") or "")
        if action.get("native_reason") == "ArmRejected" and any(d in detail for d in FOREGROUND_REFUSAL_DETAILS):
            self._foreground_refusals += 1
            self._last_foreground_refusal = detail
        else:
            self._foreground_refusals = 0

    def _note_focus_independence(self, session):
        self._focus_independent = focus_independent(session)  # ADR 250: the weaker of the per-path values

    def _check_hands(self):
        """ADR 241 C5 (CT 2026-11-19, bench check 19): drain only on a STATED cause: the mod's own refusals,
        FOREGROUND_REFUSALS_DRAIN in a row with a stated detail; and, only for a DLL that is not focus-independent, her
        window in front for more than FARMHAND_FOREGROUND_LIMIT_S (pad watch fh_fg). With ADR 244's focus_independent
        her window in front is normal. Missing pad evidence after the grace is an absence, not a cause: one observed
        row, nothing else (her first action may wait on a route, e.g. a festival talk choice, ADR 240)."""
        focused = (self._last_pad_at - self._pad_focus_since if self._pad_focus_since is not None else 0)
        elapsed = (self.clock() - self._first_skill_at if self._first_skill_at is not None else None)
        due = elapsed is not None and elapsed >= HANDS_ENGAGEMENT_GRACE_S
        cause = ("farmhand_window_active" if not self._focus_independent and focused > FARMHAND_FOREGROUND_LIMIT_S
                 else "mod_refused" if self._foreground_refusals >= FOREGROUND_REFUSALS_DRAIN else None)
        if cause is not None:
            self.state.update(status="not_recovered", stop_reason="hands_not_engaged")
            if self.state.get("days") and self.day.get("status") == "active":
                self.day["status"] = "not_recovered"
            if self.attempt is not None and self.attempt["status"] == "running":
                self.attempt["status"] = "not_recovered"
            self.save()
            self.event("hands_not_engaged", reason="hands_not_engaged", cause=cause,
                       focus_independent=self._focus_independent,
                       fh_fg=self._pad_focus_since is not None, fh_fg_seconds=focused,
                       foreground_refusals=self._foreground_refusals, refusal_detail=self._last_foreground_refusal,
                       first_admission_at=self._first_skill_at, seconds_since_admission=elapsed,
                       last_pad_engaged_at=self._last_pad_engaged_at, engaged=self._hands_engaged,
                       grace_seconds=HANDS_ENGAGEMENT_GRACE_S)
            raise HandsNotEngaged("hands_not_engaged")
        if due and not self._hands_engaged and not self._hands_evidence_said:
            self._hands_evidence_said = True  # observed once; the attempt goes on
            self.event("hands_evidence_missing", observed_only=True, focus_independent=self._focus_independent,
                       first_admission=self._first_admission,
                       first_admission_at=self._first_skill_at, seconds_since_admission=elapsed,
                       last_pad=self._last_pad_row, last_pad_engaged_at=self._last_pad_engaged_at,
                       grace_seconds=HANDS_ENGAGEMENT_GRACE_S)
        if (self.state["status"] == "starting" and self._hands_engaged
                and (self._pad_focus_since is None or self._focus_independent)):
            self.state["status"] = "active"
            self.save()

    def check(self):
        # The watcher publishes its journal row before its create-once marker. Sample markers first
        # so a watcher that wins the write after the journal read is classified on the next poll.
        present = [str(p) for p in [self.folder / "OPERATOR_STOP", *self.stop_files()]
                   if stops.pending(p, self.stop_scope())]
        # Read the guard before STOP/exit: its own process may already have
        # written RUN/LIVE7_STOP. Preserve the violation as a safety stop.
        if self.guard is not None:
            for line, row in self.tail.read(self.folder / "pad_watch.jsonl"):
                self._note_pad(row)
                if row.get("violations"):
                    held_only = all(isinstance(v, dict) and v.get("kind") == "pad_held_after_terminal" for v in row["violations"])  # a malformed row stops
                    if held_only and (row.get("recovery") or {}).get("status") == "verified":
                        continue  # runtime retains its publication fence through fresh observation
                    self.request_stop(source="pad_watch")
                    recovery = row.get("recovery") or {}
                    self.event("SAFETY_STOP", reason="pad_watch_violation", line=line,
                               protection="pad_held_after_terminal" if held_only else row["violations"],
                               held=recovery.get("remaining", recovery.get("held")))
                    raise Refused("pad_watch_violation")
            if self.guard.poll() is not None:
                raise Refused("pad_watch_exited:" + str(self.guard.returncode))
        if present:
            raise Stopped("stop_file_present:" + present[0])
        if self.state.get("status") in {"stopped", "safety_stop", "host_left"}:
            raise Stopped("cycle_status:" + self.state["status"])
        if self.state.get("status") == "not_recovered":
            raise HandsNotEngaged(self.state.get("stop_reason") or "hands_not_engaged")
        # ADR 254 (review R6 B4): while a native latch, or evidence the table cannot read, holds her hands, nothing
        # drains her running trial (no focus, effect-ladder or held-menu drain): it runs on, and every stop above still
        # ends it. What the runtime counted under the latch is dropped, so the clocks start fresh at the re-arm.
        held = self.state.get("status") in TRIAL_IN_PROGRESS and (
            getattr(self, "_crash_wait", False) or self.hands_held(self.hands("observe"), "trial"))
        if held:
            self._effect_rung5 = self._effect_clock = None
            self._foreground_refusals = 0
            if isinstance(self.state.get("menu_held"), dict):
                self.state["menu_held"]["since"] = self.clock()
        else:
            if self.guard is not None:
                self._check_hands()
            self._check_effect_ladder()
            self._check_menu_hold()
        actual = self.backend.identity()
        enrolled = read_json(self.folder / "identity.json")
        from scripts.livepath.source_drift import drift_verdict
        verdict = drift_verdict(actual, enrolled, self.source_porcelain())
        if verdict["action"] == "warn":
            seen = tuple(sorted(verdict["paths"]))
            if seen != getattr(self, "_drift_warned", None):  # once per distinct set, not once per poll
                self._drift_warned = seen
                self.event("SOURCE_DRIFT_WARN", reason="source_or_dll_drift", paths=verdict["paths"])
            return
        if verdict["action"] == "stop":
            self.event("source_or_dll_drift", paths=verdict["paths"],
                       hash_diff=verdict.get("hash_diff") or [], warn_paths=verdict.get("warn_paths") or [])
            raise Refused("source_or_dll_drift")

    def human_host(self):
        """True only when mabar wrote host_owner=human. Default (missing or harness) is harness-owned."""
        return self.state.get("host_owner") == "human"

    def enroll(self, *, rejoin_attempts=3, host_owner="harness", expected_native=None):
        if self.state or self.folder.exists() and any(self.folder.iterdir()):
            raise Refused("cycle folder must be new or empty")
        actual = self.backend.identity()
        if host_owner not in ("harness", "human"):
            raise Refused("host_owner must be harness or human")
        if actual.get("dirty") or not actual.get("dll"):
            raise Refused("clean source and installed DLL required")
        self._enroll_expected = expected_native
        session = self.reconcile_stops(self.backend.session(), "start-cycle")
        name = session.get("player_name")
        if (not isinstance(name, str) or not 1 <= len(name) <= 12 or not session.get("player_id")
                or session.get("is_main_player") is not False or not session.get("world_ready")):
            raise Refused("an existing named farmhand must be world-ready; owner is HOST")
        p = self.backend.paths
        save_identity = self.backend.save_identity(session)
        if (expected_native and expected_native.get("save_id") is not None
                and str(save_identity["save_id"]) != str(expected_native["save_id"])):
            raise Refused("native_save_identity_changed")
        save_state = dict(save_id=save_identity["save_id"], save_identity=save_identity,
                          saves_root=str(p.SAVES), save_name=None, save_folder=None)
        save_state.update(self.backend.resolve_save(save_state) or {})
        self.folder.mkdir(parents=True, exist_ok=True)
        write_json(self.folder / "identity.json", actual)
        self.state = dict(schema="live7_cycle.v1", status="ready", cycle=self.folder.name,
            stop_scope=stops.scope(session), command_dir=str(p.COMMAND_DIR) if hasattr(p, "COMMAND_DIR") else None,
            live7_stop_file=str((p.RUN / "LIVE7_STOP").resolve()),
            player_name=name, player_id=session["player_id"], join_slot="name:" + name,
            **save_state,
            rejoin_attempts=rejoin_attempts, host_owner=host_owner, days=[],
            hints=dict(enrolled_crops=[], chest_ref=None, baseline={}, rows=[], credit=False),
            archive="retain_all;upload_after_stream", started_at=self.clock())
        self.save()
        self._enroll_expected = None
        self.event("cycle_started", identity=actual)
        self.refresh_manifest("start-cycle")
        return self.state

    @property
    def day(self):
        if not self.state.get("days"):
            raise Refused("no current day")
        return self.state["days"][-1]

    def bind_save_identity(self, session):
        """Bind the enrolled native world ID to this session, in short bounded samples.

        A matching ID binds at once. An unreadable sample (any named sampling refusal) retries.
        Only a positive, consistent foreign ID refuses native_save_identity_changed: two agreeing
        consecutive readings, or foreign readings and no match by the bound. If every sample is
        unreadable, the last sample's named reason refuses, exactly as a single sample did.
        """
        if not self.state.get("save_id"):  # cycles enrolled before native save discovery
            return
        if (self.state.get("save_identity") or {}).get("session_id") == session.get("session_id"):
            return
        deadline = self.clock() + SAVE_IDENTITY_WAIT_S
        foreign, reason = [], None
        for sample in range(1, SAVE_IDENTITY_SAMPLES + 1):
            try:
                identity = self.backend.save_identity(session)
            except Refused as exc:
                reason = str(exc)
                self.event("save_identity_sample", sample=sample, outcome="unreadable", reason=reason)
            else:
                if identity["save_id"] == self.state["save_id"]:
                    self.state["save_identity"] = identity
                    self.save()
                    return
                foreign.append(identity["save_id"])
                self.event("save_identity_sample", sample=sample, outcome="foreign", save_id=identity["save_id"])
                if len(foreign) >= 2 and foreign[-1] == foreign[-2]:
                    break
            if sample == SAVE_IDENTITY_SAMPLES or self.clock() + SAVE_IDENTITY_RETRY_S > deadline:
                break
            self.check()
            self.backend.sleep(SAVE_IDENTITY_RETRY_S)
        raise Refused("native_save_identity_changed" if foreign else reason)

    def start_attempt(self, *, resume=None):
        self.check()
        session = self.reconcile_stops(self.backend.session(), "start-day")
        self._note_focus_independence(session)  # ADR 244: the DLL says whether her window in front matters
        pose = self.backend.ready(self.state, any_screen=True)  # ADR 241 C6: an open menu is her first page
        if isinstance(pose, dict) and (pose.get("active_menu") is not None or pose.get("is_player_free") is False):
            self.event("attempt_start_screen", menu=pose.get("active_menu"), free=pose.get("is_player_free"),
                       location=pose.get("location"), time_of_day=pose.get("time_of_day"))
        self.bind_save_identity(session)
        date = pose.get("date")
        if not isinstance(date, dict):
            raise Refused("fresh native game date required")
        # ADR 93: a verified night carries its wallet facts too (night_facts); a lost night keeps its own record.
        previous_night = (self.day.get("lost_night_facts") or self.day.get("night_facts")) if self.state["days"] else None
        if not self.state["days"] or self.day.get("status") == "ended":
            if self.state["days"] and date_key(date) != date_key(self.day["next_date"]):
                raise Refused("cycle_date_gap; fresh native date differs from the last night checkpoint")
            self.state["days"].append(dict(index=len(self.state["days"]) + 1, date=date,
                                             status="active", attempts=[], rows=[]))
        day = self.day
        if day["attempts"] and date_key(date) != date_key(day["date"]):
            raise Refused("rejoin_game_date_changed; reconcile the interrupted night first")
        number = len(day["attempts"])
        ident = f"day-{day['index']:04d}-attempt-{number:03d}"
        root = self.folder / ident
        root.mkdir()
        attempt = dict(id=ident, journal=str(root / "journal"), approval=str(root / "approval.json"),
            report=str(root / "report.json"), stdout=str(root / "stdout.txt"), stop_file=str(root / "STOP"),
            live7_stop_file=str(self.stop_files()[0]),
            session=session.get("session_id"), stop_scope=stops.scope(session), status="starting",
            cycle_started_at=self.cycle_started_at())
        if not attempt["session"]:
            raise Refused("fresh_session_id_required")
        attempt["host_log_cursor"] = self.backend.host_log_cursor()
        structural = list(self.state.get("hints", {}).get("structural_quarantine", []))
        crops = self.state.get("hints", {}).get("crop_memory")
        if crops:
            resume = dict(resume or {}, crop_memory=crops)
        if structural:
            resume = dict(resume or {}, structural_quarantine=structural)
        trips = self.state.get("hints", {}).get("travel_trips")
        if trips:
            resume = dict(resume or {}, travel_trips=trips)
        shop = self.state.get("hints", {}).get("shop_memory")
        if shop:
            resume = dict(resume or {}, shop_memory=shop)  # closures ARTI saw herself (smoke 2026-10-23 item 11)
        ground = self.state.get("hints", {}).get("ground_items")
        if ground:
            resume = dict(resume or {}, ground_items=ground)  # fix59A (ADR 86): items she left on the ground
        totals = self.state.get("hints", {}).get("catch_totals")
        if totals:
            resume = dict(resume or {}, catch_totals=totals)  # FIX71C: landed catches by place, across days
        kinds = self.state.get("hints", {}).get("catch_kinds")
        if kinds:
            resume = dict(resume or {}, catch_kinds=kinds)  # FIX73D: landed catches by kind, across days
        chests = self.state.get("hints", {}).get("chest_memory")
        if chests:
            resume = dict(resume or {}, chest_memory=chests)  # FIX72D: what her chests held, past the day and a restart
        forage = self.state.get("hints", {}).get("forage_memory")
        if forage:
            resume = dict(resume or {}, forage_memory=forage)  # FIX73E: where she has seen forage, past the day and a restart
        seen = self.state.get("hints", {}).get("seen_objects")
        if seen:
            resume = dict(resume or {}, seen_objects=seen)  # FIX77C (ADR 136): what she has seen on each map, past the day and a restart
        cc_seen = self.state.get("hints", {}).get("cc_seen")
        if isinstance(cc_seen, str):
            resume = dict(resume or {}, cc_seen=cc_seen)  # FIX75B (ADR 129): the Community Center state she last saw in full
        names = self.state.get("hints", {}).get("item_names")
        if names:
            resume = dict(resume or {}, item_names=names)  # FIX72D: names she has read, for items no longer in her hands
        quests = self.state.get("hints", {}).get("quest_refused_items")
        if quests:
            resume = dict(resume or {}, quest_refused_items=quests)
        if knockouts := self.state.get("hints", {}).get("knockouts"):
            resume = dict(resume or {}, knockouts=knockouts[-8:])
        moments = [m for recent_day in self.state["days"][-2:] for m in recent_day.get("moments", [])]
        if moments:
            resume = dict(resume or {}, moments=moments)  # keep cooldown across a new day's process too
        if previous_night:
            resume = dict(resume or {}, previous_night=previous_night)
        if number == 0 and day["index"] > 1:
            # fix54H: this runner opens a native day after a night; its progress watch starts at the wake.
            resume = dict(resume or {}, after_night=True)
        recovery_history = self.state.get("hints", {}).get("recovery_history")
        if recovery_history:
            resume = dict(resume or {}, recovery_history=recovery_history[-8:])
        told_file = self.folder.parent / EFFECT_LADDER_TOLD
        told = read_json(told_file, {}) if told_file.is_file() else {}
        if isinstance(told.get("told"), str) and told.get("cycle") != self.folder.name:
            resume = dict(resume or {}, ladder_told=told["told"])  # ADR 241 B: said once on the fresh cycle's first page
            try:
                told_file.unlink()
            except OSError:
                pass
        if resume:
            attempt["resume_file"] = str(root / "resume.json")
            attempt["quarantine"] = list(resume.get("quarantine", []))
            write_json(attempt["resume_file"], resume)
        day["attempts"].append(attempt)
        self.state.pop("menu_held", None)
        self.state["status"] = ("starting" if self.human_host() and self.guard is not None
                                and not self._hands_engaged else "active")
        self.state["stop_scope"] = stops.scope(session)
        self.save()  # reserve the attempt before approval/process effects
        self.backend.approve(self.folder, self.state, attempt)
        self.check()
        self.backend.preserve_process_evidence(attempt)
        self.wait_for_hands("start", "day_trial")  # ADR 254 (R6 B1): no trial starts into a latch
        child = self.backend.start_trial(attempt)
        self.child, self.attempt = child, attempt
        attempt.update(pid=child.pid, status="running")
        self.save()
        self.event("day_trial_started", day_trial=ident)
        return child, attempt

    def note_menu_recovery(self, attempt, row):
        """ADR 237 B/C: the runtime's held-menu rows. A hold starts at the runtime's first report (the game refused its
        own close or the menu must be answered: `native_menu_deadline`; else the 600 s watchdog) and keeps that time
        while the same menu holds; the runtime's close readback or its `menu_held_cleared` row ends it."""
        held = self.state.get("menu_held")
        menu = row.get("menu")
        reported = isinstance(menu, str) and bool(menu.strip()) and (
            (row.get("ev") == "live7_menu_held" and row.get("state") == "menu_held" and row.get("recoverable") is True)
            or (row.get("ev") == "menu_recovery_outcome" and row.get("source") == "native_menu_deadline"
                and row.get("status") == "menu_held"))
        same = bool(held) and held.get("day_trial") == attempt["id"] and held.get("menu") == menu
        if reported:
            if not same:
                at = row.get("at")
                since = at if type(at) in (int, float) and math.isfinite(at) else self.clock()
                current = dict(menu=menu, day_trial=attempt["id"], recoverable=True, since=since)
                self.state["menu_held"] = current
                self.save()
                self.event("live7_menu_held", state="menu_held", **current)
        elif same and ((row.get("ev") == "menu_recovery_outcome" and row.get("request_id")
                        and row.get("source") == "native_post_recovery_readback"
                        and row.get("status") in {"closed", "interrupted"}
                        and "menu_after" in row and row["menu_after"] is None)
                       or (row.get("ev") == "menu_held_cleared" and row.get("source") == "native_menu_gone")):
            self.state.pop("menu_held")
            self.save()

    def note_effect_clock(self, attempt, row):
        """ADR 241 A: the runtime's effect clock on the operator's and the supervisor's status line
        (<cycle>/effect_clock.json, small, rewritten at most every EFFECT_FILE_S or when the rung changes)."""
        if row.get("ev") == "live7_effect_ladder":
            if row.get("rung") == 5 and row.get("result") == "fresh_cycle_due":
                self._effect_rung5 = dict(day_trial=attempt["id"], at=row.get("at"), rungs=row.get("rungs") or [],
                                          told=row.get("told"))
            row = dict(row, kind="ladder", age_s=row.get("age_s"))
            if type(row.get("age_s")) not in (int, float):
                return
        if row.get("ev") == "skill_session_finished":
            self._effect_clock = None
            self._effect_rung5 = None
            write_json(self.folder / "effect_clock.json", dict(day_trial=attempt["id"], ended=True,
                                                               at=row.get("at"), reason=row.get("reason")))
            return
        keep = {k: row.get(k) for k in ("at", "kind", "age_s", "ladder_age_s", "reset_by", "since_at", "rung",
                                        "honoured_s", "held_s", "rungs", "stand_aside") if k in row}
        previous = getattr(self, "_effect_clock", None) or {}
        current = dict(keep, day_trial=attempt["id"], noted=self.game_clock.now(), noted_wall=self.clock())
        self._effect_clock = current
        if (previous.get("day_trial") != attempt["id"] or previous.get("rung") != current.get("rung")
                or self.clock() - previous.get("written", 0) >= EFFECT_FILE_S):
            current["written"] = self.clock()
            write_json(self.folder / "effect_clock.json", current)
        else:
            current["written"] = previous.get("written", 0)

    def _check_effect_ladder(self):
        """ADR 241 B rung 5: end this attempt cleanly so the supervisor starts a fresh cycle in the same game (the
        hands_not_engaged drain, with its hands proof on the new cycle). Never a stop. The runtime's rung-5 row is the
        trigger; a runtime that went silent past rung 5's bound on its last clock row is drained too."""
        if self.state.get("status") not in TRIAL_IN_PROGRESS or self.attempt is None or self.attempt.get("status") != "running":
            return
        due = getattr(self, "_effect_rung5", None)
        clock = getattr(self, "_effect_clock", None) or {}
        silent = None
        if due is not None and due.get("day_trial") != self.attempt["id"]:
            due = None
        if due is None and clock.get("day_trial") == self.attempt["id"] and type(clock.get("ladder_age_s")) in (int, float):
            from arti_runtime.recovery_ladder import BOUND_S
            quiet = max(0.0, self.game_clock.now() - clock.get("noted", self.game_clock.now()))
            age = clock["ladder_age_s"] + quiet
            if quiet >= EFFECT_LADDER_SILENT_S and age >= BOUND_S[5] + EFFECT_LADDER_SILENT_S:
                silent = round(age, 3)
        if due is None and silent is None:
            return
        rungs = (due or {}).get("rungs") or []
        told = (due or {}).get("told")
        self.state.update(status="not_recovered", stop_reason=EFFECT_LADDER_DRAIN)
        self.state["effect_ladder"] = dict(day_trial=self.attempt["id"], rungs=rungs, silent_age_s=silent)
        if self.state.get("days") and self.day.get("status") == "active":
            self.day["status"] = "not_recovered"
        if self.attempt["status"] == "running":
            self.attempt["status"] = "not_recovered"
        if isinstance(told, str) and told:
            write_json(self.folder.parent / EFFECT_LADDER_TOLD, dict(told=told, cycle=self.folder.name, at=self.clock()))
        self.save()
        self.event(EFFECT_LADDER_DRAIN, reason=EFFECT_LADDER_DRAIN, rungs=rungs, runtime_silent_age_s=silent,
                   day_trial=self.attempt["id"])
        raise HandsNotEngaged(EFFECT_LADDER_DRAIN)

    def _check_menu_hold(self):
        """ADR 237 C: a menu the runtime could not close, still held MENU_HELD_RECOVER_S after its first report, ends
        this attempt as a recoverable drain (like hands_not_engaged) so the supervisor recovers it; never a stop."""
        held = self.state.get("menu_held") or {}
        since = held.get("since")
        if (self.state.get("status") not in TRIAL_IN_PROGRESS or type(since) not in (int, float)
                or self.clock() - since < MENU_HELD_RECOVER_S):
            return
        self.state.update(status="not_recovered", stop_reason="menu_held_recover")
        if self.state.get("days") and self.day.get("status") == "active":
            self.day["status"] = "not_recovered"
        if self.attempt is not None and self.attempt["status"] == "running":
            self.attempt["status"] = "not_recovered"
        self.save()
        self.event("menu_held_recover", reason="menu_held_recover", menu=held.get("menu"), since=since,
                   held_s=round(self.clock() - since, 3), bound_s=MENU_HELD_RECOVER_S)
        raise HandsNotEngaged("menu_held_recover")

    def scan(self, attempt):
        journal = Path(attempt["journal"]) / "g1_journal.jsonl"
        wait = attempt.setdefault("bed_wait", {})
        rows = self.tail.read(journal)
        for _, row in rows:
            self.game_clock.note(row)
        self.trial_safety(rows)
        for line, row in rows:
            self.note_menu_recovery(attempt, row)
            if row.get("ev") in ("live7_effect_clock", "live7_effect_ladder", "skill_session_finished"):
                self.note_effect_clock(attempt, row)
            hints = self.state["hints"]
            if row.get("ev") == "live7_day_lost":
                attempt["lost_night_facts"] = {k: row.get(k) for k in
                    ("reason", "time_of_day", "money_before", "money_before_observed", "money_after")}
            if row.get("ev") == "live7_wallet" and type(row.get("money")) is int:
                attempt["last_wallet"] = {k: row.get(k) for k in ("money", "date", "time_of_day")}  # ADR 93
            if row.get("ev") == "live7_crop_memory":
                hints["crop_memory"] = CropMemory(row.get("crops")).crops
            if row.get("ev") == "live7_ground_items" and isinstance(row.get("items"), list):
                hints["ground_items"] = row["items"]
            if row.get("ev") == "live7_catch_totals" and isinstance(row.get("totals"), dict):
                hints["catch_totals"] = row["totals"]
                if isinstance(row.get("kinds"), dict):
                    hints["catch_kinds"] = row["kinds"]  # FIX73D
            if row.get("ev") == "live7_chest_memory" and isinstance(row.get("memory"), dict):
                hints["chest_memory"] = row["memory"]
            if row.get("ev") == "live7_forage_memory" and isinstance(row.get("memory"), dict):
                hints["forage_memory"] = row["memory"]
            if row.get("ev") == "live7_seen_objects" and isinstance(row.get("memory"), dict):
                hints["seen_objects"] = row["memory"]
            if row.get("ev") == "live7_cc_seen" and isinstance(row.get("signature"), str):
                hints["cc_seen"] = row["signature"]  # FIX75B
            if row.get("ev") == "live7_item_names" and isinstance(row.get("names"), dict):
                hints.setdefault("item_names", {}).update(row["names"])
            if row.get("ev") == "live7_travel_memory" and isinstance(row.get("trips"), list):
                hints["travel_trips"] = row["trips"][-128:]
            if row.get("ev") == "live7_shop_memory" and isinstance(row.get("memory"), dict):
                hints["shop_memory"] = row["memory"]
            if row.get("ev") == "knocked_out" and row.get("id"):
                known = {k["id"]: k for k in hints.get("knockouts", [])}
                known[row["id"]] = {k: v for k, v in row.items() if k != "ev"}
                hints["knockouts"] = list(known.values())[-8:]
            if row.get("ev") == "live7_quest_refusals" and isinstance(row.get("items"), list):
                hints["quest_refused_items"] = sorted(set(hints.get("quest_refused_items", ())) | {
                    q for q in row["items"] if isinstance(q, str) and 0 < len(q) <= 128})
            if row.get("ev") == "part_live_row":
                memory = CropMemory(hints.get("crop_memory"))
                memory.record(row, self.day["date"])
                hints["crop_memory"] = memory.crops
            was_waiting = bool(wait)
            update_host_wait(wait, row, source=journal, line=line)
            if was_waiting and not wait:
                self.event("host_wait_ended", day_trial=attempt["id"])
            if row.get("ev") == "live7_baseline":
                attempt["before"] = row.get("before", {})
                self.state["hints"].update(baseline=attempt["before"], credit=False)
                attempt["inflight"] = {k: row.get(k) for k in ("skill", "arg", "target")}
            if row.get("ev") == "live7_moment_emitted":
                moments = self.day.setdefault("moments", [])
                if not any(m.get("kind") == row.get("kind") and m.get("date") == row.get("date") for m in moments):
                    moments.append({k: row[k] for k in ("kind", "date", "ts")})
            if row.get("ev") in {"skill_started", "skill_leg_started"}:
                attempt["inflight"] = {k: row.get(k) for k in ("skill", "arg", "target")}
                if self._first_skill_at is None and row.get("admitted") is True:
                    at = row.get("at")
                    self._first_skill_at = at if type(at) in (int, float) and math.isfinite(at) else self.clock()
                    self._first_admission = {k: row.get(k) for k in ("skill", "arg")}
                    self._note_pad_engagement()
            if row.get("ev") == "action_result":
                self._note_action_result(row)
            if row.get("ev") == "part_live_row":
                self.day["rows"].append(row)
                from arti_stardew_live7 import structural_refusal
                structural = dict(row, location=(row.get("game") or {}).get("location") or
                                  ((row.get("evidence") or {}).get("cleanup") or {}).get("location"))
                if structural_refusal(structural):
                    hint = {k: structural[k] for k in ("skill", "arg", "location", "reason", "outcome_class")}
                    hints = self.state["hints"].setdefault("structural_quarantine", [])
                    if hint not in hints:
                        hints.append(hint)
                if row.get("verified") is True:
                    self.day["rejoin_used"] = 0
                if row.get("outcome_class") == "UNCERTAIN" and attempt.get("inflight"):
                    attempt.setdefault("quarantine", []).append(attempt["inflight"])
                attempt.pop("inflight", None)
        self._host_wait_reminder(attempt, wait)

    def _host_wait_reminder(self, attempt, wait):
        if not self.game_clock.paused() and (alert := host_wait_alert(wait, self.clock())):
            attempt["host_wait"] = alert["at"]
            self.event("host_wait", day_trial=attempt["id"], goal_id=wait.get("goal_id"),
                       source=alert["source"], line=alert["line"], phase=wait.get("phase"),
                       message=wait.get("message") or "ARTI is in bed; owner, sleep on HOST when ready.")

    def stop_child(self, child, attempt):
        stops.write_sentinel(attempt["stop_file"], dict(attempt.get("stop_scope") or {},
                             cycle_id=str(self.folder)), writer="supervisor.stop_child")
        # Cooperative runtime stop releases input. Never kill a process on timeout.
        deadline = self.clock() + 20
        while child.poll() is None and self.clock() < deadline:
            self.backend.sleep(0.1)
        if child.poll() is None:
            raise Refused("trial_cleanup_timeout; no relaunch")

    def rejoin(self, child, attempt, *, host_lost=False):
        from scripts.livepath import halt_authority as HA
        self._manual_crash_rejoined = False
        crashed = not self.backend.farmhand_alive()
        if crashed:
            self.event("crash", day_trial=attempt["id"])
            try:
                self.backend.preserve_process_evidence(attempt)
            except OSError:
                pass  # the table will hold if it cannot correlate the watchdog's two witnesses
            self._crash_wait = True
            try:
                self.wait_for_hands("launch", "rejoin")
            finally:
                self._crash_wait = False
        self.wait_for_hands("observe", "rejoin")
        self.stop_child(child, attempt)
        self.scan(attempt)
        self.backend.preserve_process_evidence(attempt)
        attempt["status"] = "interrupted"
        resume = dict(before=attempt.get("before", {}), interrupted_session=attempt["session"],
                      quarantine=attempt.get("quarantine", []) +
                      ([attempt["inflight"]] if attempt.get("inflight") else []))
        if not crashed:
            self.event("session_dropped", day_trial=attempt["id"])
        self.save()
        if host_lost:
            attempt["day_replay"] = True
            self.state["status"] = "host_wait"
            self.event("host_crash", message="Waiting for the owner to reload HOST; automatic rejoin follows.")
            self.save()
        while not self.backend.host_alive():
            self.check()
            self.backend.sleep(1)
        self.state["status"] = "rejoining"
        self.save()
        # Risk #2: keep the exit proof for the session the recovery worker replaces.
        old_launch = self.backend.launch_record()
        while self.day.get("rejoin_used", 0) < self.state["rejoin_attempts"]:
            self.check()
            if not self._manual_crash_rejoined:
                self.wait_for_hands("close" if self.backend.farmhand_alive() else "launch", "rejoin")
            retry = self.day.get("rejoin_used", 0) + 1
            self.day["rejoin_used"] = retry
            self.save()  # reserve before launch; successful joins alone do not reset the retry bound
            self.event("rejoin_attempt", attempt=retry, day_trial=attempt["id"], slot=self.state["join_slot"])
            try:
                launched = self._manual_crash_rejoined or self.backend.launch(
                    self.state["join_slot"], self.folder / "LIVE7_STOP", expected=HA.expected_identity(self.state),
                    held=getattr(self, "_hands_held", None))
            except HA.HandsHeld as exc:
                self.hands_held(exc.decision, "rejoin")
                if HA.operator_stop(exc.decision):
                    raise Stopped(exc.decision.reason) from None
                self.day["rejoin_used"] -= 1
                self.save()
                self.backend.sleep(1)
                continue
            finally:
                if old_launch is not None and not attempt.get("exit_proof"):
                    proof = self.backend.preserve_exit_proof(attempt, old_launch)
                    if proof:
                        attempt["exit_proof"] = proof
                        self.save()
                        self.event("rejoin_exit_recorded", day_trial=attempt["id"], **proof)
            if launched:
                pose = self.backend.ready(self.state, any_screen=True)  # ADR 241 C6: the same after a rejoin
                rejoined = self.backend.session()
                self._note_focus_independence(rejoined)  # the relaunched DLL says it again
                self.bind_save_identity(rejoined)
                self.state["stop_scope"] = stops.scope(rejoined)
                self.event("rejoin_complete", attempt=retry, day_replay=host_lost)
                date = pose.get("date")
                if isinstance(date, dict) and date_key(date) == tomorrow(self.day["date"]):
                    # The farmhand vanished during the save transition. Fresh native
                    # morning + on-disk date prove a night, never credit its lost row.
                    while not self.saved_date_matches(date):
                        self.check()
                        self.backend.sleep(1)
                    self.day.update(status="night_saved", next_date=date, interrupted_night=True, outcome="lost")
                    self.save()
                    self.event("night_saved", date=date, verified_trial=False, reason="rejoined_after_night_save")
                    return None, None
                return self.start_attempt(resume=resume)
            self.event("rejoin_failed", attempt=retry)
            self.backend.sleep(2)
        raise Refused("rejoin_retries_exhausted")

    def day_was_saved(self):
        """Whether this leg already passed a night save (on disk or native next morning)."""
        if self.day.get("status") == "night_saved" or self.day.get("checkpoint"):
            return True
        native = None
        try:
            native = self.backend.save_date(self.state)
        except Exception:
            native = None
        session = self.backend.session()
        if isinstance(session, dict) and isinstance(session.get("date"), dict):
            native = session["date"]
        if isinstance(native, dict):
            try:
                return date_key(native) == tomorrow(self.day["date"])
            except (KeyError, TypeError, ValueError):
                return False
        return False

    def host_away(self, attempt):
        """Human-owned HOST: process gone or unbound actor. Does not read the HOST SMAPI log.

        host_disconnect (open HOST SMAPI, seek attempt baseline, read to EOF, scan Player quit)
        stays in session_dropped after debounce, same site as before this branch.
        """
        if not self.backend.host_alive():
            return dict(observed="process_gone")
        session = self.backend.session()
        if session.get("world_ready") is True and unbound_actor(session):
            return dict(observed="connection_closed", basis="unbound_actor")
        return None

    def end_host_left(self, exc):
        """End this cycle leg with host_left. Not a SAFETY_STOP. Does not touch the HOST save."""
        info = dict(exc.info or {})
        payload = dict(observed=exc.observed, day_saved=exc.day_saved, **info)
        self.state["status"] = "host_left"
        self.state["host_left"] = payload
        self.request_stop(source="host_left")
        self.event("host_left", **payload)
        self.save()
        return self.state

    def session_dropped(self, attempt):
        """Debounce file races without ever restarting a healthy, unchanged session.

        Process exit is conclusive. A live process needs repeated native session evidence or a
        new HOST quit. Persistently unreadable evidence alone stops safely; it cannot justify WM_CLOSE.
        """
        if not self.backend.farmhand_alive():
            return True
        session = self.backend.session()
        # At the title/disconnect screen the native bridge deliberately publishes actor 0 / "".
        # A bound actor mismatch is a refusal; an unbound world is debounced, then rejoined by name.
        # A missing/empty actor is not a different farmer (11-12: that misfire was SAFETY_STOP).
        if foreign_farmhand(session, self.state):
            raise Refused("farmhand_identity_mismatch")
        if session.get("world_ready") is True and session.get("session_id") == attempt["session"]:
            if attempt.pop("session_failure", None):
                self.event("session_read_recovered", day_trial=attempt["id"])
            return False
        failure = attempt.setdefault("session_failure", dict(since=self.clock(), count=0))
        failure["count"] += 1
        elapsed = self.clock() - failure["since"]
        if failure["count"] < SESSION_FAILURES or elapsed < SESSION_FAILURE_SECONDS:
            return False
        host = self.backend.host_disconnect(attempt, self.state["player_id"])
        native_drop = bool(session.get("session_id") and (
            session["session_id"] != attempt["session"] or session.get("world_ready") is False))
        if native_drop or host.get("confirmed"):
            self.event("session_drop_confirmed", day_trial=attempt["id"], failures=failure["count"],
                       elapsed=elapsed, session_id=session.get("session_id"), host=host)
            return True
        if elapsed >= SESSION_UNKNOWN_SECONDS:
            if not failure.get("recovery_logged"):
                failure["recovery_logged"] = True
                self.event("FAULT", type="live7_recovery", cause="session_read_unconfirmed",
                    refs=dict(day_trial=attempt["id"]), observed=dict(failures=failure["count"], elapsed_s=elapsed),
                    disposition="RECOVER", outcome_class="UNCERTAIN", verified=False)
            if elapsed >= 600:
                raise Refused("no_progress_600s:session_read_unconfirmed; no relaunch")
        return False

    def wait_night(self, child, attempt):
        from scripts.livepath.halt_authority import operator_stop
        while True:
            self.check()
            self.scan(attempt)
            decision = self.hands("observe")
            if self.hands_held(decision, "trial"):
                if operator_stop(decision):
                    # A STOP racing the scan must not mask a terminal safety failure already on disk.
                    if child.poll() is not None:
                        self.scan(attempt)
                        report = read_json(attempt["report"], {})
                        reason = report.get("reason", report.get("status"))
                        independently_failed = (report and day_terminal(report) is None
                                                and reason not in ("stop:live7_stop_file", "owner_stop")
                                                and not (reason == "terminal_cleanup_unconfirmed"
                                                         and day_terminal(report.get("terminal_outcome"))))
                        if independently_failed:
                            raise Refused("trial_not_verified:" + str(reason))
                    self.check()
                    raise Stopped(decision.reason)
                self.backend.sleep(0.25)
                continue
            if self.human_host():
                away = self.host_away(attempt)
                if away:
                    raise HostLeft(day_saved=self.day_was_saved(), **away)
                dropped = self.session_dropped(attempt)
                if dropped:
                    child, attempt = self.rejoin(child, attempt, host_lost=False)
                    if child is None:
                        return
                    continue
            else:
                host_lost = not self.backend.host_alive()
                dropped = self.session_dropped(attempt)
                if host_lost or dropped:
                    child, attempt = self.rejoin(child, attempt, host_lost=host_lost)
                    if child is None:
                        return
                    continue
            if attempt.get("session_failure"):
                self.backend.sleep(0.25)
                continue  # retry read-only before interpreting child/night readiness
            if child.poll() is not None:
                self.scan(attempt)
                if attempt.get("status") != "finished":
                    self.backend.preserve_process_evidence(attempt)
                report = read_json(attempt["report"], {})
                attempt.update(exit=child.returncode, status="finished")
                self.trial_stop(report.get("reason"))
                verdict = day_terminal(report)
                if (verdict is None and report.get("reason") == "terminal_cleanup_unconfirmed"
                        and day_terminal(report.get("terminal_outcome")) is not None):
                    self.recover_trial_cleanup(attempt)
                    verdict = day_terminal(report["terminal_outcome"])
                    attempt["recovered_day_terminal"] = verdict
                if verdict is None:
                    if not report:
                        self.event("trial_crash", day_trial=attempt["id"])
                    raise Refused("trial_not_verified:" + str(report.get("reason", report.get("status"))))
                if report.get("cleanup") == "UNCONFIRMED":
                    self.recover_trial_cleanup(attempt)
                attempt["day_terminal"] = verdict
                if verdict == "lost":
                    return self.wait_lost_night(attempt)
                if not self.night_readback(attempt):
                    self.backend.sleep(1)  # bounded save sync after the native next morning
                    continue
                return
            self.backend.sleep(0.25)

    def recover_trial_cleanup(self, attempt):
        """Exited trial already released its lease; reobserve before any next-day input."""
        from arti_stardew_command import input_release_state, lease_state
        report = read_json(attempt["report"], {})
        request_id = (report.get("post_row_state") or {}).get("request_id") or ((report.get("final_goal") or {}).get("last_action") or {}).get("request_id")
        self.event("FAULT", type="live7_recovery", cause="trial_cleanup_unconfirmed",
                   refs=dict(day_trial=attempt["id"], report=attempt["report"], request_id=request_id),
                   observed=dict(input_released="UNKNOWN"), disposition="RECOVER", outcome_class="UNCERTAIN", verified=False)
        deadline = self.game_clock.now() + 600
        while True:
            self.check()
            before = self.game_clock.now()
            self.wait_for_hands("observe", "trial_cleanup")
            deadline += self.game_clock.now() - before
            if not self.backend.farmhand_alive() or not self.backend.host_alive():
                raise Refused("cleanup_recovery_process_exited")
            raw = self.backend.session()
            if raw.get("session_id") and raw["session_id"] != attempt["session"]:
                raise Refused("cleanup_recovery_session_changed")
            if raw.get("world_ready") is True and (raw.get("player_id") != self.state["player_id"]
                    or raw.get("player_name") != self.state["player_name"] or raw.get("is_main_player") is not False):
                raise Refused("cleanup_recovery_identity_changed")
            command_dir = getattr(self.backend.paths, "COMMAND_DIR", None)
            released = input_release_state(raw, request_id=request_id,
                result=read_json(Path(command_dir) / "result.json", {}) if command_dir else None)
            if released is False:
                raise Refused("uncontrolled_input:native_held_cleanup_recovery")
            if (raw.get("session_id") == attempt["session"] and released is True
                    and lease_state(raw).owner is None):
                self.state["hints"]["recovery_history"][-1]["observed"]["input_released"] = True
                self.save()
                self.event("trial_cleanup_reobserved", day_trial=attempt["id"], input_released=True,
                           credit=False, replay=False)
                return
            if self.game_clock.now() >= deadline:
                raise Refused("no_progress_600s:cleanup_recovery")
            self.backend.sleep(.25)

    def wait_lost_night(self, attempt):
        """An honest lost day (PLAN 2.1): no relaunch; the night ends natively (pass-out or the owner's
        HOST sleep). A HOST loss or farmhand exit here is a refusal, never a rejoin.

        Risk #3: the trial has exited, so a farmhand LevelUpMenu / ShippingMenu after the night would block
        the HOST save forever. Such a menu gets at most DAY_START_PASSES bounded passes of ARTI's existing
        day-start rule (a --live7-day-start-only trial: no model, no owner turn, never a window close).
        Bounds (review F1): the night is bounded by LOST_NIGHT_WAIT_S from the first observed night signal
        (a verified 02:00 pass-out, a night-only screen, the next date awaiting its save). Before that, the
        rest of the lost day is bounded by LOST_DAY_STALL_S without native clock progress. The whole wait
        is capped at LOST_WAIT_TOTAL_S wall (review R2-A). All end in a named refusal, never a silent hang."""
        from arti_stardew_live7 import PASSED_OUT_REASON
        self.event("day_lost", day_trial=attempt["id"],
                   message="ARTI's day was lost honestly; waiting for the native next morning.")
        started = now = self.game_clock.now()  # the overall wall cap (LOST_WAIT_TOTAL_S) runs from here
        report = read_json(attempt["report"], {})
        night_since = now if report.get("reason") == PASSED_OUT_REASON else None  # 02:00 already verified
        progress_at, clock_seen, stall_noticed, owner_noticed = now, None, False, False
        passes = attempt.setdefault("night_passes", [])
        while True:
            self.check()
            before = self.game_clock.now()
            self.wait_for_hands("observe", "night_wait")
            paused = self.game_clock.now() - before
            started += paused
            progress_at += paused
            if night_since is not None:
                night_since += paused
            if not self.backend.host_alive():
                if self.human_host():
                    raise HostLeft("process_gone", day_saved=self.day_was_saved(),
                                   phase="lost_night")
                raise Refused("host_lost_during_lost_day")
            if not self.backend.farmhand_alive():
                raise Refused("farmhand_exited_during_lost_day")
            self.night_wait_reason = self.night_clock = None
            if self.night_readback(attempt):
                return
            reason = self.night_wait_reason or "night_not_saved"
            now = self.game_clock.now()
            tod = self.night_clock
            if type(tod) is int and (clock_seen is None or tod > clock_seen):
                clock_seen, progress_at = tod, now  # the native lost day is still progressing
            if night_since is None and (reason == "night_save_not_on_disk"
                                        or any(m in reason for m in NIGHT_ONLY_MENUS)):
                night_since = now
                self.event("lost_night_started", day_trial=attempt["id"], reason=reason[:200])
            menu = next((m for m in DAY_START_MENUS if m in reason), None)
            if (menu is not None and len(passes) < DAY_START_PASSES
                    and not any(p.get("uncertain") and p.get("menu") == menu for p in passes)):
                # Review F4 (soak.py day_start): the owner clearing it by hand must not race ARTI's rule.
                # True defers (owner in front); None (read failed / unreadable) defers too; a locked or idle
                # desktop with no foreground window reads False (review R2-C), so the AFK night still passes.
                front = self.backend.farmhand_in_front()
                if front is False:
                    self.day_start_pass(attempt, menu)
                    continue
                if not owner_noticed:
                    owner_noticed = True
                    self.event("day_start_pass_deferred", day_trial=attempt["id"], menu=menu,
                               front="farmhand" if front else "unknown",
                               message="The farmhand window is in front (the owner may be clearing it) or the "
                                       "foreground is unreadable; no pass while it is, still bounded by the "
                                       "night wait.")
            if night_since is not None:
                phase, waited, limit = "night", now - night_since, LOST_NIGHT_WAIT_S
            else:
                phase, waited, limit = "day", now - progress_at, LOST_DAY_STALL_S
                if waited >= LOST_NIGHT_WAIT_S and not stall_noticed:
                    stall_noticed = True
                    self.event("lost_day_clock_stalled", day_trial=attempt["id"], stalled_s=round(waited, 1),
                               last_reason=reason, time_of_day=clock_seen, limit_s=LOST_DAY_STALL_S,
                               message="The native clock of the lost day has not advanced (festival, event or "
                                       "HOST pause?); the cycle stops safely if it stays paused.")
            if now - started >= LOST_WAIT_TOTAL_S:  # review R2-A: a slow clock cannot stretch the wait forever
                phase, waited, limit = "overall", now - started, LOST_WAIT_TOTAL_S
            if waited >= limit:
                self.event("live7_long_wait", day_trial=attempt["id"], waited_s=round(waited, 1), phase=phase,
                           limit_s=limit, last_reason=reason, menu=menu, day_start_passes=len(passes),
                           time_of_day=clock_seen,
                           message="Long wait with verified progress; the 600s no-progress watchdog remains authority.")
                # A duration quota cannot turn real native progress into danger.
                # The generic watchdog above still stops a genuinely stalled wait.
                if phase == "overall":
                    started = now
                elif phase == "night":
                    night_since = now
                else:
                    progress_at = now
            self.backend.sleep(1)

    def day_start_pass(self, attempt, menu):
        """One bounded day-start-only trial on the farmhand's end-of-night menu (risk #3)."""
        passes = attempt["night_passes"]
        ident = f"{attempt['id']}-night-{len(passes):03d}"
        root = self.folder / ident
        root.mkdir()
        session = self.backend.session()
        run = dict(id=ident, journal=str(root / "journal"), approval=str(root / "approval.json"),
                   report=str(root / "report.json"), stdout=str(root / "stdout.txt"), stop_file=str(root / "STOP"),
                   live7_stop_file=str(self.stop_files()[0]), session=session.get("session_id"),
                   stop_scope=stops.scope(session), status="starting", day_start_only=True, menu=menu)
        if not run["session"]:
            raise Refused("fresh_session_id_required")
        passes.append(run)
        self.save()  # reserve before approval/process effects
        self.backend.approve(self.folder, self.state, run)
        self.check()
        self.wait_for_hands("start", "night_only")  # ADR 254 (R6 B1): the night-only worker too
        child = self.backend.start_trial(run)
        self.child, self.attempt = child, run  # drive's cleanup stops it cooperatively on STOP/refusal
        run.update(pid=child.pid, status="running")
        self.save()
        self.event("day_start_pass_started", day_trial=attempt["id"], night_pass=ident, menu=menu,
                   message="ARTI's own day-start rule clears the end-of-night menu (no model, no window close).")
        run["progress_since"] = self.game_clock.now()
        run["parent_day_trial"] = attempt["id"]
        while child.poll() is None:
            self.check()
            from scripts.livepath.halt_authority import operator_stop
            decision = self.hands("observe")
            if self.hands_held(decision, "night_only"):
                if operator_stop(decision):
                    raise Stopped(decision.reason)
                run["progress_since"] = self.game_clock.now()
                self.backend.sleep(0.25)
                continue
            self.pass_safety(run)
            if self.game_clock.now() - run["progress_since"] >= DAY_START_PASS_S:
                self.event("day_start_pass_timeout", night_pass=ident, seconds=DAY_START_PASS_S)
                run["timeout"] = True
                self.stop_child(child, run)  # cooperative STOP only; refuses if it does not exit
                break
            self.backend.sleep(0.25)
        self.pass_safety(run)
        report = read_json(run["report"], {})
        if not report:
            raise Refused("day_start_pass_crash:missing_report")
        run.update(exit=child.returncode, status="finished", result=report.get("status"), reason=report.get("reason"))
        self.trial_stop(run["reason"])
        if run["result"] == "OWNER_STOPPED" and not run.get("timeout"):
            raise Refused("day_start_pass_not_clean:owner_stop")
        if safety(report):
            raise Refused("day_start_pass_safety_stop")
        self.save()
        self.event("day_start_pass_finished", night_pass=ident, result=run["result"], reason=run["reason"])
        if run["result"] in DAY_START_PASS_RESULTS:
            return run
        if run["result"] == "HELD" and str(run["reason"]).startswith("day_start_blocked"):
            return run  # the rule refused the menu; the bounded night wait decides (named timeout)
        if no_input_pass(report):
            # Review F3: a pass that provably sent nothing and left input released (e.g. native binding not
            # ready, a FAILED validate) changed nothing; the bounded night wait decides, as for a blocked rule.
            run["no_input"] = True
            self.save()
            return run
        # A possibly-applied pass is never replayed on this menu. Keep waiting
        # for native readback; the bounded night wait retains its safety deadline.
        run["uncertain"] = True
        self.save()
        self.event("FAULT", type="live7_recovery", cause="day_start_pass_not_clean",
                   refs=dict(night_pass=ident, report=run["report"]), observed=dict(reason=run["reason"], result=run["result"]),
                   disposition="RECOVER", outcome_class="UNCERTAIN", verified=False)
        return run

    def trial_stop(self, reason):
        """Recheck a stop-induced terminal against the operator's scoped request.

        STOP can arrive after check() but before a journal read or child.poll().
        The skill uses SAFETY_STOP for cooperative cancellation too; its label
        alone, or the mere presence of a STOP file, does not establish the cause.
        """
        if reason not in ("stop:live7_stop_file", "owner_stop"):
            return
        owner = False
        for path in self.stop_files():
            if not stops.pending(path, self.stop_scope()):
                continue
            try:
                _, value = stops.read_stop(path)
            except (OSError, ValueError):
                return  # unknown provenance retains the original refusal
            if (not stops.scoped(value) or value.get("cycle_id") != str(self.folder)
                    or value.get("writer") != "supervisor.operator_stop"):
                return
            owner = True
        if owner:
            self.check()  # guard violations/exits still take precedence over STOP

    def trial_safety(self, rows):
        stopped = False
        for _, row in rows:
            if not safety(row):
                continue
            if (row.get("ev") == "skill_session_finished"
                    and row.get("reason") == "stop:live7_stop_file"):
                stopped = True
            else:
                raise Refused("journal_SAFETY_STOP")
        if stopped:
            self.trial_stop("stop:live7_stop_file")
            raise Refused("journal_SAFETY_STOP")

    def pass_safety(self, run):
        journal = Path(run["journal"]) / "g1_journal.jsonl"
        rows = self.tail.read(journal)
        for _, row in rows:
            self.game_clock.note(row)
            if (row.get("ev") == "ui_key_result" and row.get("verified") is True
                    and row.get("cleanup") is True and row.get("effect") not in (None, "no_effect", "unchanged")):
                run["progress_since"] = self.game_clock.now()
                watch = self.__dict__.get("_night_progress", {}).get(run.get("parent_day_trial"))
                if watch is not None:
                    watch.since = self.game_clock.now()
        self.trial_safety(rows)
        wait = run.setdefault("bed_wait", {})
        for line, row in rows:
            was_waiting = bool(wait)
            update_host_wait(wait, row, source=journal, line=line)
            if was_waiting and not wait:
                self.event("host_wait_ended", day_trial=run["id"])
        self._host_wait_reminder(run, wait)

    def night_readback(self, attempt):
        report = read_json(attempt["report"], {})
        verdict = day_terminal(report) or attempt.get("recovered_day_terminal")
        if verdict is None:
            raise Refused("trial_not_verified:" + str(report.get("reason", report.get("status"))))
        try:
            pose = self.backend.ready(self.state)
        except Refused as exc:
            if str(exc).startswith("farmhand_not_ready"):
                self.night_wait_reason = str(exc)
                self.night_progress_check(attempt)
                key = (attempt["id"], "night_readback_unconfirmed")
                seen = self.__dict__.setdefault("_recovery_faults", set())
                if key not in seen:
                    seen.add(key)
                    self.event("FAULT", type="live7_recovery", cause=key[1],
                        refs=dict(day_trial=attempt["id"]), observed=dict(reason=str(exc)),
                        disposition="RECOVER", outcome_class="UNCERTAIN", verified=False)
                return False  # night / save screens of a lost day: keep waiting (bounded by the caller)
            raise
        control = pose.get("player_control")
        # A date edge is not a wake receipt: native farm/story events can still own controls and save sync.
        if isinstance(control, dict) and (control.get("event_up") is True or control.get("farm_event_type")
                or control.get("freeze_controls") is True or control.get("dialogue_up") is True):
            since = attempt.setdefault("native_wake_since", self.clock())
            self.night_wait_reason = "native_wake_pending"
            if self.clock() - since >= NIGHT_SAVE_WAIT_S:
                raise Refused("native_wake_timeout")
            self.night_progress_check(attempt, pose)
            return False
        if isinstance(control, dict) and all(type(control.get(k)) is bool for k in ("paused", "host_paused")):
            self.game_clock.note(dict(ev="game_paused" if control["paused"] or control["host_paused"]
                                      else "game_resumed", at=self.clock()))
            if self.game_clock.paused():
                self.night_wait_reason = "host_paused"
                return False
        date = pose.get("date")
        if verdict == "lost" and isinstance(date, dict) and date_key(date) == date_key(self.day["date"]):
            self.night_wait_reason = "lost_day_still_running"
            self.night_clock = pose.get("time_of_day")  # native progress of the lost day (F1 bound)
            self.night_progress_check(attempt, pose)
            return False  # the lost day is still running natively
        if not isinstance(date, dict) or date_key(date) == date_key(self.day["date"]):
            key = (attempt["id"], "next_morning_unconfirmed")
            seen = self.__dict__.setdefault("_recovery_faults", set())
            if key not in seen:
                seen.add(key)
                self.event("FAULT", type="live7_recovery", cause=key[1], refs=dict(day_trial=attempt["id"]),
                    observed=dict(date=date), disposition="RECOVER", outcome_class="UNCERTAIN", verified=False)
            self.night_wait_reason = key[1]
            self.night_progress_check(attempt, pose)
            return False
        if date_key(date) != tomorrow(self.day["date"]):
            raise Refused("next_morning_not_observed")
        if not self.saved_date_matches(date):
            self.night_wait_reason = "night_save_not_on_disk"
            self.night_progress_check(attempt, pose)
            return False
        self.day.update(status="night_saved", next_date=date, outcome=verdict)
        if verdict == "lost":
            self.day["lost_night_facts"] = dict(attempt.get("lost_night_facts", {}),
                outcome="lost", native_save_confirmed=True, woke_location=pose.get("location"),
                next_date=list(date_key(date)))
        elif isinstance(attempt.get("last_wallet"), dict):
            # ADR 93: the wallet she slept on, so the morning can state the night's net change (shipping pays overnight).
            wallet = attempt["last_wallet"]
            self.day["night_facts"] = dict(outcome="verified", money_before=wallet.get("money"),
                money_before_observed=dict(wallet), time_of_day=wallet.get("time_of_day"),
                native_save_confirmed=True, woke_location=pose.get("location"), next_date=list(date_key(date)))
        self.save()
        self.event("night_saved", day_trial=attempt["id"], date=date, day_terminal=verdict)
        return True

    def host_waits(self, now=None):
        """ADR 255 (review M4d item 3): what the HOST helper waits on, or None. A wait is a fact only while the helper is
        provably alive: RUN/../host/host_wait.json (rewritten at least every period_s while it waits) says waiting, is at
        most three periods old by the wall clock, and no HOST_STOP exists. A stale row or a dead helper is not a wait."""
        paths = getattr(self.backend, "paths", None)
        if paths is None:
            return None
        host = Path(paths.RUN).parent / "host"
        if (host / "HOST_STOP").exists():
            return None
        row = read_json(host / "host_wait.json", None)
        if not isinstance(row, dict) or row.get("state") != "waiting":
            return None
        at, period = row.get("at"), row.get("period_s")
        if not isinstance(at, (int, float)) or not isinstance(period, (int, float)) or period <= 0:
            return None
        if abs((time.time() if now is None else now) - at) > 3 * period:
            return None
        return str(row.get("what") or "waiting")

    def fresh_time_of_day(self):
        """Review M4e item 2: the game clock read now, from the same live pose that Backend.ready returns (the source of
        the cycle's other clock checks), independent of the readiness gate. None when it cannot be read."""
        live = getattr(self.backend, "live", None)
        try:
            pose = live.pose() if live is not None else None
        except Exception:  # noqa: BLE001 - an unread clock is no clock
            return None
        tod = pose.get("time_of_day") if isinstance(pose, dict) else None
        return tod if type(tod) is int else None

    def night_progress_check(self, attempt, pose=None):
        """Exited-trial night waits also obey the generic ten-minute progress rule."""
        watches = self.__dict__.setdefault("_night_progress", {})
        # ADR 255 (review M4c rule 5): a night held by the owner's HOST (the helper waits for him or for a clean HOST) is a
        # named wait, not a stall: the ten-minute bound restarts and no stop is written. Said once per wait.
        held = self.host_waits()
        # Review M4d item 3 / M4e item 2: the restart is itself bounded: never past the game's own end of day (2:00 AM,
        # 2600). The clock is read FRESH at this moment, from the live pose the cycle's other clock checks use (never a
        # carried value, never the readiness gate's refusal); an unread clock, or 2600 or later, = the ordinary bound.
        clock = self.fresh_time_of_day() if held else None
        if held and not (type(clock) is int and clock < 2600):
            if self.__dict__.get("_night_held_bounded") != held:
                self.__dict__["_night_held_bounded"] = held
                self.event("night_wait_bound_reached", day_trial=attempt["id"], blocks=held, time_of_day=clock)
            held = None
        if held:
            watches[attempt["id"]] = ProgressWatchdog(self.game_clock.now())
            if self.__dict__.get("_night_held") != held:
                self.__dict__["_night_held"] = held
                self.event("night_waits_for_host", day_trial=attempt["id"], blocks=held)
            return
        self.__dict__["_night_held"] = None
        watch = watches.setdefault(attempt["id"], ProgressWatchdog(self.game_clock.now()))
        signature = None
        if pose is not None:
            signature = json.dumps({k: pose.get(k) for k in
                ("date", "location", "tile", "pixel", "inventory", "money", "active_menu", "is_player_free")},
                sort_keys=True, default=str)
        if watch.observe(self.game_clock.now(), signature):
            self.event("live7_no_progress", day_trial=attempt["id"], bound_s=600,
                       outcome_class="SAFETY_STOP")
            raise Refused("no_progress_600s:night_readback")

    def saved_date_matches(self, date):
        since = self.day.setdefault("save_readback_since", self.game_clock.now())
        if not self.state.get("save_name") and self.state.get("save_id"):
            resolved = self.backend.resolve_save(self.state)
            if resolved:
                self.state.update(resolved)
                self.event("save_name_resolved", save_id=self.state["save_id"], **resolved)
        saved = self.backend.save_date(self.state)
        if isinstance(saved, dict) and date_key(saved) == date_key(date):
            self.day.pop("save_readback_since", None)
            self.save()
            return True
        self.save()
        if self.game_clock.now() - since >= NIGHT_SAVE_WAIT_S:
            reason = "night_save_name_unresolved" if not self.state.get("save_name") else "night_save_date_timeout"
            self.event("night_save_timeout", reason=reason, save_id=self.state.get("save_id"),
                       save_name=self.state.get("save_name"), limit_s=NIGHT_SAVE_WAIT_S)
            raise Refused(reason)
        return False

    def checkpoint(self):
        self.check()
        if self.day.get("status") == "ended":
            raise Refused(f"already done (day {self.day['index']} completed)")
        if self.day.get("status") != "night_saved":
            raise Refused("checkpoint requires verified next-morning night save")
        if self.day.get("checkpoint"):
            return dict(status="copied", target=self.day["checkpoint"], existing=True)
        # D6: copy only after the game's own night save completed (HOST "Context: after save").
        evidence = self.backend.night_save(self.state, self.day["next_date"])
        if not evidence.get("confirmed"):
            since = self.day.setdefault("night_save_wait_since", self.clock())
            if self.clock() - since < NIGHT_SAVE_WAIT_S:
                self.save()
                return dict(status="waiting_night_save", reason=evidence.get("reason"))
            self.event("checkpoint_night_save_unconfirmed", **evidence)
            raise Refused("checkpoint_night_save_unconfirmed:" + str(evidence.get("reason")))
        since = self.day.setdefault("checkpoint_copy_since", self.clock())
        if self.clock() - since >= NIGHT_SAVE_WAIT_S:
            raise Refused("checkpoint_copy_timeout")
        target = self.folder / "checkpoints" / f"day-{self.day['index']:04d}-{time.time_ns()}"
        report = self.backend.checkpoint(self.state, target, self.day["next_date"], evidence)
        self.event("checkpoint", **{k: v for k, v in report.items() if k != "files"})
        if report.get("status") == "copied":
            self.day["checkpoint"] = str(target)
            self.day.pop("checkpoint_copy_since", None)
        self.save()
        return report

    def end_day(self):
        self.check()
        if self.day.get("status") == "ended":
            raise Refused(f"already done (day {self.day['index']} completed)")
        if self.day.get("status") != "night_saved" or not self.day.get("checkpoint"):
            raise Refused("end-day requires a completed night checkpoint")
        self.day["status"] = "ended"
        self.state["status"] = "ready"
        self.state.pop("menu_held", None)
        self.day.setdefault("outcome", "verified")
        self.state["hints"].update(rows=self.day["rows"], credit=False)
        self.save()
        self.event("day_ended", index=self.day["index"], date=self.day["date"])

    def drive(self, *, once=False):
        # Reconcile before starting a guard or granting any action authority.
        if self.state.get("status") not in {"stopped", "safety_stop", "not_recovered"}:
            if self.state.get("status") in TRIAL_IN_PROGRESS:
                raise Refused("an interrupted supervisor needs reconciliation; refusing duplicate trial")
            self.reconcile_stops(self.backend.session(), "start-day")
        self.check()
        lock = self.folder / "supervisor.lock"
        with lock.open("x", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        child = attempt = None
        try:
            if self.state.get("status") in TRIAL_IN_PROGRESS:
                raise Refused("an interrupted supervisor needs reconciliation; refusing duplicate trial")
            self.refresh_manifest("start-day")
            self.guard = self.backend.start_guard(self.folder)
            while True:
                if not self.state["days"] or self.day.get("status") != "night_saved":
                    child, attempt = self.start_attempt()
                    self.wait_night(child, attempt)
                while self.checkpoint().get("status") != "copied":
                    self.check()
                    self.backend.sleep(1)
                self.end_day()
                if once:
                    return self.state
                self.refresh_manifest("next-day")  # chained days: re-describe before the next day starts
        except Stopped:
            self.request_stop(source="observed_stop")
            self.state["status"] = "stopped"
            self.event("cycle_stopped")
            return self.state
        except HostLeft as exc:
            return self.end_host_left(exc)
        except HandsNotEngaged:
            # The attempt-local STOP in finally drains the trial. No RUN/cycle
            # STOP, pad panic, voice notification or page event for this fault.
            return self.state
        except (Refused, SystemExit, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            self.state["status"] = "safety_stop"
            self.state["stop_reason"] = str(exc)[:300]
            self.request_stop(source="cycle_safety_stop")
            self.event("SAFETY_STOP", reason=str(exc)[:300])
            raise
        finally:
            try:
                if self.child is not None and self.child.poll() is None:
                    self.stop_child(self.child, self.attempt)
            finally:
                (self.folder / "WATCH_STOP").touch()
                if self.guard is not None:
                    try:
                        self.guard.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.event("guard_cleanup_timeout")
                self.save()
                lock.unlink()


def main(argv=None, backend=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("start-cycle", "start-day", "wait-night", "checkpoint", "end-day", "status", "stop"):
        p = sub.add_parser(cmd)
        p.add_argument("--cycle-dir", type=Path, required=True)
        if cmd == "stop":
            p.add_argument("--yes", action="store_true", help="confirm STOP explicitly for CT/noninteractive callers")
        if cmd == "start-cycle":
            p.add_argument("--rejoin-attempts", type=int, default=3)
            p.add_argument("--host-owner", choices=("harness", "human"), default="harness",
                           help="human: mabar co-op (HOST quit is host_left); harness: solo (HOST loss rejoins)")
            p.add_argument("--expected-native", type=json.loads, help=argparse.SUPPRESS)
        if cmd == "start-day":
            p.add_argument("--once", action="store_true", help="complete one day; default chains until STOP")
    p = sub.add_parser("smapi-log", help="rebuild a role's full SMAPI log from the saved per-snapshot segments")
    p.add_argument("--cycle-dir", type=Path, required=True)
    p.add_argument("--role", choices=("farmhand", "host"), default="farmhand")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--process", help="process snapshot id (default: newest)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "smapi-log":
            result = reconstruct_smapi_log(a.cycle_dir, a.role, a.out, process=a.process)
        elif a.cmd == "status":
            result = Cycle(a.cycle_dir, None).status()
        elif a.cmd == "stop":
            if not (a.cycle_dir / "cycle_state.json").is_file():
                raise Refused("unknown cycle")
            if not a.yes:
                if not sys.stdin.isatty():
                    raise Refused("stop confirmation required: use --yes for noninteractive STOP")
                print("Hentikan siklus ARTI? [y/N] ", end="", file=sys.stderr, flush=True)
                try:
                    confirmed = sys.stdin.readline().strip().lower() in {"y", "yes"}
                except KeyboardInterrupt:
                    confirmed = False
                if not confirmed:
                    raise Refused("stop cancelled; no STOP written")
            Cycle(a.cycle_dir, None).request_stop(source="operator_stop")
            result = dict(stop_requested=True)
        else:
            sys.path.insert(0, str(HERE.parents[1]))
            sys.path.insert(0, str(HERE.parents[1] / "scripts"))
            c = Cycle(a.cycle_dir, backend or Backend())
            if a.cmd == "start-cycle":
                if a.rejoin_attempts < 1:
                    raise Refused("positive bounded rejoin attempts required")
                result = c.enroll(rejoin_attempts=a.rejoin_attempts, host_owner=a.host_owner,
                                  expected_native=a.expected_native)
            else:
                if not c.state:
                    raise Refused("start-cycle required")
                if a.cmd == "start-day":
                    result = c.drive(once=a.once)
                elif a.cmd == "wait-night":
                    c.check()
                    if (a.cycle_dir / "supervisor.lock").exists():
                        raise Refused("supervisor owns wait-night; use status")
                    attempt = c.day["attempts"][-1]
                    while c.backend.trial_alive(attempt):
                        c.check()
                        c.scan(attempt)
                        c.backend.sleep(0.25)
                    c.scan(attempt)
                    while not c.night_readback(attempt):
                        c.check()
                        c.backend.sleep(1)
                    result = dict(status=c.day["status"], date=c.day["next_date"])
                else:
                    if (a.cycle_dir / "supervisor.lock").exists():
                        raise Refused("supervisor owns this cycle; use status or stop")
                    result = c.checkpoint() if a.cmd == "checkpoint" else c.end_day()
        print(json.dumps(dict(ok=True, result=result), ensure_ascii=False))
        return 0
    except Stopped as exc:
        # Rehearsal 1: end-day on a SAFETY_STOP cycle crashed with a traceback. A stopped cycle refuses.
        state = read_json(a.cycle_dir / "cycle_state.json", {})
        print(json.dumps(dict(ok=False, refused="cycle_stopped:" + (str(exc) or "stop"),
                              cycle_status=state.get("status"),
                              message="This cycle is stopped; no checkpoint/end-day/night action runs. "
                                      "Read status and the evidence; a new cycle needs a new folder.")))
        return 2
    except HostLeft as exc:
        print(json.dumps(dict(ok=True, result=dict(status="host_left", observed=exc.observed,
                                                   day_saved=exc.day_saved, **(exc.info or {})))))
        return 0
    except HandsNotEngaged as exc:
        print(json.dumps(dict(ok=False, refused=str(exc), cycle_status="not_recovered")))
        return 2
    except (Refused, OSError, ValueError, SystemExit) as exc:
        print(json.dumps(dict(ok=False, refused=str(exc))))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
