"""Invocation-scoped launch permission and read-only co-op identity checks."""
from contextlib import contextmanager
from ctypes import wintypes
import ctypes
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import time
from uuid import uuid4

from arti_runtime import ipc
from scripts.livepath import halt_authority as HA
from scripts.m1_operator import win_facts as wf


LIMIT_S = 600
SCENE_S, SCENE_TRIES = 5.0, 3  # one live scene sample, and how many before the save counts as unknown
UNREADABLE_S = 2.0  # a session or lock file that cannot be read this long is unknown; a shorter gap is a replace
OUTER = "ARTI_OWNER_REJOIN_OUTER"  # the process that restarted this CLI without a window, as pid:start
HOST_LAUNCH = "runtime/batch-host/host-launch.json"
FARMHAND_LAUNCH = "runtime/farmhand-launch.json"
SMAPI = "StardewModdingAPI.exe"


def refuse(reason):
    raise HA.HandsHeld(HA.Decision("hold", reason))


def prepared_cabins(save_dir):
    """Read the selected save once at prepare and pin its native world and established cabin IDs."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(save_dir / save_dir.name).getroot()
    except (OSError, ET.ParseError):
        return {"save_id": None}
    peers = root.findall("./farmhands/Farmer")
    peers += root.findall(".//farmhand")
    candidates = {}
    for peer in peers:
        name, pid = peer.findtext("name"), peer.findtext("UniqueMultiplayerID")
        if name and pid and re.fullmatch(r"-?[1-9][0-9]*", pid):
            candidates.setdefault(name, set()).add(pid)
    return dict(save_id=root.findtext("uniqueIDForThisGame"),
                farmhand_ids={name: next(iter(ids)) if len(ids) == 1 else None for name, ids in candidates.items()})


def selected(run, name):
    if not isinstance(name, str) or not 1 <= len(name) <= 12 or not name.isprintable():
        refuse("owner_join_needs_farmhand_name")
    _, row = HA.read_record(Path(run) / "continued.json")
    row = row if isinstance(row, dict) else {}
    folder = row.get("save")
    if (not isinstance(folder, str) or folder != PureWindowsPath(folder).name
            or not re.fullmatch(r'[^<>:"/\\|?*\x00-\x1f]+_[1-9][0-9]*', folder)
            or PureWindowsPath(folder).is_reserved()):
        refuse("owner_join_selected_save_unknown")
    save_id = folder.rsplit("_", 1)[1]
    if str(row.get("save_id", save_id)) != save_id:
        refuse("owner_join_selected_save_disagrees")
    ids = row.get("farmhand_ids", {})
    if not isinstance(ids, dict):
        refuse("owner_join_prepared_cabin_unknown")
    binding = dict(save_folder=folder, save_id=save_id, player_name=name)
    if name in ids:
        if not re.fullmatch(r"-?[1-9][0-9]*", str(ids[name])):
            refuse("owner_join_prepared_cabin_unknown")
        binding["player_id"] = str(ids[name])
    elif ids:
        refuse("owner_join_prepared_cabin_unknown")
    return binding


def _hwnd(path):
    try:
        value = int(ipc.read_text(path, encoding="utf-8-sig").strip())
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def host_binding(run):
    run = Path(run)
    if HA.session_kind(run) != "owner" or HA.present(run / HA.HELPER_CONFIG):
        refuse("owner_join_host_role_unknown")
    raw, row = HA.read_record(run / HOST_LAUNCH)
    pid = row.get("pid") if isinstance(row, dict) else None
    hwnd = _hwnd(run / "runtime/host-hwnd.txt")
    facts = wf.window(hwnd) if hwnd else {}
    started = wf.process_start(pid) if type(pid) is int and pid > 0 and facts.get("pid") == pid else None
    if not (started and facts.get("exists") and (wf.exe_name(pid) or "").casefold() == SMAPI.casefold()):
        refuse("owner_join_host_unconfirmed")
    # The original HOST supervisor's record is immutable for one launch.
    return dict(pid=pid, started=started, hwnd=hwnd, generation=HA.sha256(raw))


def require_host(run, binding):
    if host_binding(run) != binding:
        refuse("owner_join_host_changed")


def _object_name(kind, key):
    return "Local\\ARTI-" + kind + "-" + key


@contextmanager
def live_attempt(lease):
    """The initiating CLI alone owns this unsignalled event for the life of its invocation."""
    _, k32 = wf._w()
    create = k32.CreateEventW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    handle = create(None, True, False, _object_name("owner-word", lease))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.SetEvent(handle)
        k32.CloseHandle(handle)


def launch_mode(run):
    """("first" or "again", the digest of the launch record it was read from). "again" only when the exact process of the
    previous launch (PID and creation time) has verifiably ended; anything unknown refuses."""
    raw, launch = HA.read_launch_record(Path(run) / FARMHAND_LAUNCH)
    played = bool(HA.cycle_dirs(run))
    if raw is None:
        if played:
            refuse("owner_join_previous_launch_unknown")
        return "first", None
    if not isinstance(launch, dict) or "pid" not in launch:
        refuse("owner_join_previous_launch_unknown")
    if launch["pid"] is None:  # its launcher never published a PID; `replaces` is the game that record stood for
        if launch.get("replaces") is None and not played:
            return "first", HA.sha256(raw)
        launch = launch.get("replaces")
        if not isinstance(launch, dict):
            refuse("owner_join_previous_launch_unknown")
    require_gone(launch)
    if (ended(run, launch) or {}).get("lost_lock"):
        refuse("owner_join_lock_would_be_lost")
    return "again", HA.sha256(raw)


def ended(run, launch):
    """What was recorded the moment this launch's game ended (`record_ended`), or None."""
    return HA.launch_end(run, launch)


def record_ended(run, folder):
    """Called as her game ends, before another game can publish over its last session: whether a lock that no start can
    restore ended with it. Main keeps such a lock by refusing every other session; a game a person starts by hand would
    otherwise hide it from the next word and from `rejoin`."""
    run = Path(run)
    _, launch = HA.read_launch_record(run / FARMHAND_LAUNCH)
    if (not isinstance(launch, dict) or not launch.get("launch_id") or type(launch.get("pid")) is not int
            or not wf.process_gone(launch["pid"], launch.get("process_start"))):
        return
    ev = HA.read_evidence(folder, run, sentinels=False, expected={})
    session = ev.session[1]
    _, host = HA.read_record(run / HOST_LAUNCH)
    games = wf.image_pids(SMAPI)
    # Only her own last publication (or the one she was started over and never replaced) says how she ended, and only
    # while no other game than the HOST runs that could have published since.
    hers = (games is not None and isinstance(host, dict) and set(games) <= {host.get("pid")} and isinstance(session, dict)
            and (HA.launch_published(launch, session)
                 or (session.get("runtime_instance_id") is not None
                     and session["runtime_instance_id"] == launch.get("previous_runtime"))))
    lock = HA.ended_lock(ev) if hers else "unknown"
    path = run / "runtime/owner-game-ended" / (launch["launch_id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    ipc.create_json_once(path, dict(launch_id=launch["launch_id"], pid=launch["pid"], lock=lock,
                                    runtime_instance_id=session["runtime_instance_id"] if hers else None,
                                    known=lock in ("none", "kept"), lost_lock=lock == "lost"), fsync=True)


def require_gone(launch):
    pid = launch.get("pid")
    if type(pid) is not int or pid <= 0:
        refuse("owner_join_previous_launch_unknown")
    if not wf.process_gone(pid, launch.get("process_start")):
        refuse("owner_join_previous_game_still_up")


def replaced(launch):
    """What a pending record keeps of the launch it stands in for."""
    if not isinstance(launch, dict):
        return None
    return (launch.get("replaces") if launch.get("pid") is None
            else {k: launch.get(k) for k in ("pid", "process_start", "launch_id")})


def new_attempt(run, name, *, issued=None):
    issued = time.monotonic() if issued is None else issued
    if time.monotonic() >= issued + LIMIT_S:
        refuse("owner_join_attempt_expired")
    pid = os.getpid()
    mode, prior = launch_mode(run)
    outer = os.environ.get(OUTER)
    return dict(id=uuid4().hex, lease=uuid4().hex, run=str(Path(run).resolve()), selected=selected(run, name),
                host=host_binding(run), caller_pid=pid, caller_start=wf.process_start(pid), outer=outer,
                issued=issued, expires=issued + LIMIT_S, mode=mode, prior=prior)


def require_mode(word, run):
    """The word speaks for the launch record it was given on; another start since then needs another word."""
    mode, prior = launch_mode(run)
    if (mode, prior) != (word["mode"], word["prior"]):
        refuse("owner_join_launch_record_changed")
    return mode


def _outer_alive(outer):
    if outer is None:
        return True
    pid, _, started = str(outer).partition(":")
    return pid.isdigit() and started.isdigit() and wf.process_start(int(pid)) == int(started)


def _is_child(pid):
    parent = os.getppid()
    return parent == pid or (wf.exe_name(parent) == "python.exe" and wf.process_parent(parent) == pid)


def require_attempt(word, run, *, slot=None, parent=None):
    if word is None:
        refuse("owner_join_needs_owner_word")
    if (not isinstance(word, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(word.get("id")))
            or not re.fullmatch(r"[0-9a-f]{32}", str(word.get("lease"))) or word.get("run") != str(Path(run).resolve())):
        refuse("owner_join_attempt_mismatch")
    if not (word["issued"] <= time.monotonic() < word["expires"] <= word["issued"] + LIMIT_S):
        refuse("owner_join_attempt_expired")
    if (not word["caller_start"] or wf.process_start(word["caller_pid"]) != word["caller_start"]
            or not _outer_alive(word.get("outer"))
            or not wf.event_exists(_object_name("owner-word", word["lease"]))):
        refuse("owner_join_attempt_cancelled")
    if parent == "worker" and not _is_child(word["caller_pid"]):
        refuse("owner_join_attempt_wrong_parent")
    if parent == "launcher" and (not _is_child(word.get("worker_pid")) or not word.get("worker_start")
                                  or wf.process_start(word["worker_pid"]) != word["worker_start"]):
        refuse("owner_join_attempt_wrong_parent")
    name = word["selected"]["player_name"]
    if word["selected"] != selected(run, name) or (slot is not None and slot != "name:" + name):
        refuse("owner_join_attempt_mismatch")
    require_host(run, word["host"])


@contextmanager
def spawn_lock(folder):
    """Serialize owner launches for one canonical shared inbox through PID publication."""
    key = hashlib.sha256(os.path.normcase(str(Path(folder).resolve())).encode("utf-8")).hexdigest()
    _, k32 = wf._w()
    create = k32.CreateMutexW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    handle = create(None, False, _object_name("farmhand-launch", key))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if k32.WaitForSingleObject(handle, 0) not in (0, 0x80):  # acquired or previous owner died
            refuse("owner_join_spawn_busy")
        try:
            yield
        finally:
            k32.ReleaseMutex(handle)
    finally:
        k32.CloseHandle(handle)


def reserve_spawn(run, word):
    ledger = Path(run) / "runtime/owner-join-attempts"
    ledger.mkdir(parents=True, exist_ok=True)
    if not ipc.create_json_once(ledger / (word["id"] + ".json"), {"attempt_id": word["id"]}, fsync=True):
        refuse("owner_join_attempt_spent")


def farmhand_binding(run, host):
    run = Path(run)
    _, launch = HA.read_launch_record(run / FARMHAND_LAUNCH)
    pid = launch.get("pid") if isinstance(launch, dict) else None
    hwnd = _hwnd(run / "runtime/fh-hwnd.txt")
    facts = wf.window(hwnd) if hwnd else {}
    if (not pid or not facts.get("exists") or facts.get("pid") != pid or pid == host["pid"]
            or launch.get("owner_host") != host or not launch.get("process_start")
            or wf.process_start(pid) != launch["process_start"]):
        refuse("owner_join_farmhand_changed")
    return launch, hwnd, facts


def hand_started(run, host, raw, old):
    """(launch, hwnd, window): the one game a person started by hand for this run after its recorded launch ended, in the
    shape of a launch record. Read only; `adopt` records it once the same verification has passed."""
    if raw is not None:
        if not isinstance(old, dict) or "pid" not in old:
            refuse("owner_join_previous_launch_unknown")
        if (previous := replaced(old)) is not None:
            require_gone(previous)
    others = [pid for pid in wf.image_pids(SMAPI) or [] if pid != host["pid"]]
    started = wf.process_start(others[0]) if len(others) == 1 else None
    windows = [w for w in wf.enum_windows("running SMAPI") if started and w.get("pid") == others[0]]
    if len(windows) != 1:
        refuse("owner_join_farmhand_changed")
    cycles = HA.cycle_dirs(run)
    _, state = HA.read_record(cycles[-1] / "cycle_state.json") if cycles else (None, None)
    issued = wf.filetime_ms(started)  # only a publication made after this process began can be its own
    launch = dict(pid=others[0], launch_id="hand-%d-%d" % (others[0], started), exe=wf.process_image(others[0]),
                  stderr=None, previous_runtime=HA.expected_identity(state).get("runtime_instance_id"),
                  issued_ms=issued, utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(0, issued) / 1000)),
                  process_start=started, owner_host=host, hand_started=True)
    return launch, windows[0]["hwnd"], windows[0]


def bound_game(run, host):
    """(launch, hwnd, window, adoption): her recorded launch while that process lives, else the game a person started.
    `adoption` is None for a recorded launch, else the digest of the record it would replace."""
    raw, launch = HA.read_launch_record(Path(run) / FARMHAND_LAUNCH)
    if (isinstance(launch, dict) and type(launch.get("pid")) is int
            and not wf.process_gone(launch["pid"], launch.get("process_start"))):
        return (*farmhand_binding(run, host), None)
    return (*hand_started(run, host, raw, launch), dict(replaces=HA.sha256(raw)))


def require_accounted(run, folder):
    """A hand-started game hides the last session of the game before it. It binds only when that end is known to have lost
    no lock, or when a person re-armed this very game's own startup lock, as main's manual successor needs."""
    raw, launch = HA.read_launch_record(Path(run) / FARMHAND_LAUNCH)
    previous = replaced(launch) if raw is not None else None
    if previous is None and not HA.cycle_dirs(run):
        return  # no game of this run before it: nothing to lose
    row = ended(run, previous) if previous else None
    if row is not None and row.get("lost_lock"):
        refuse("owner_join_lock_would_be_lost")
    ev = HA.read_evidence(folder, run)
    if not (row is not None and row.get("known")) and not HA.own_startup_receipt(ev, ev.session[1]):
        refuse("owner_join_previous_end_unknown")


def adopt(run, folder, game):
    """Record a hand-started game as this run's launch, after its verification and under the spawn lock."""
    launch, hwnd, _, adoption = game
    run = Path(run)
    with spawn_lock(folder):
        raw, _ = HA.read_launch_record(run / FARMHAND_LAUNCH)
        if HA.sha256(raw) != adoption["replaces"] or wf.process_start(launch["pid"]) != launch["process_start"]:
            refuse("owner_join_launch_record_changed")
        ipc.publish(run / "runtime/fh-hwnd.txt", str(hwnd), critical=True)
        ipc.write_json(run / FARMHAND_LAUNCH, launch, fsync=True)


def world_actor(session, selected_farmhand):
    if session.get("world_ready") is not True:
        refuse("owner_join_world_not_ready")
    if (session.get("is_main_player") is not False or session.get("is_split_screen") is not False
            or type(session.get("screen_id")) is not int or session["screen_id"] != 0):
        refuse("owner_join_wrong_role")
    player_id = str(session.get("player_id"))
    if (session.get("player_name") != selected_farmhand["player_name"]
            or not re.fullmatch(r"-?[1-9][0-9]*", player_id)
            or player_id != selected_farmhand.get("player_id", player_id)):
        refuse("owner_join_wrong_cabin")


def _title(window):
    return " ".join((window.get("title") or "").split())


def _tell(tell):
    if tell is None:
        from scripts.livepath.mabar import log as tell
    return tell


def opened_as_main(run, session, launch, hwnd, window, tell=None):
    """A person opened a farm in her window as its own host. Say it at once; nothing is closed and nothing is cleared."""
    if (not HA.launch_published(launch, session) or session.get("world_ready") is not True
            or session.get("is_main_player") is not True):
        return
    from scripts.livepath.mabar_supervise import log_line
    message = (f'Her window "{_title(window)}" (PID {launch["pid"]}, HWND {hwnd}) opened a farm as its own host instead '
               'of joining the hosted one. Close that window before anyone sleeps in it: its night would be saved over '
               'that farm. Then start again with a new word.')
    _tell(tell)(run, "owner_join_opened_as_main_player", pid=launch["pid"], hwnd=hwnd, title=_title(window), alert=True,
                message=message, stream=sys.stderr)
    log_line(run, "ALERT " + message)
    refuse("owner_join_opened_as_main_player")


UNREADABLE_REASONS = ("session_unreadable", "halt_evidence_unknown", "halt_record_unreadable", "exit_record_unreadable")


def check_wait(ev, held=None, *, restored=None, window=True, unread=None):
    """A stopped or locked boundary cannot produce a human handoff. Until her new game publishes, the old game's lock that
    the launch carries over (`restored`) is not news; her own startup lock ends the wait once her window is known."""
    session = ev.session[1] if isinstance(ev.session[1], dict) else {}
    ours = HA.launch_published(ev.launch[1], session)
    ev.startup = dict(runtime_instance_id=session.get("runtime_instance_id"))
    decision = HA.decide(ev, act="join", held=held)
    if decision.action == "hold":
        outranked = decision.reason.startswith("stop_outranks_recovery:") and not HA.operator_stop(decision)
        if unread is not None and (outranked or decision.reason in UNREADABLE_REASONS):
            if time.monotonic() - unread.setdefault("since", time.monotonic()) < UNREADABLE_S:
                return
        lock = decision.halt if decision.halt and not ev.stops else None
        carried = lock and not ours and restored is not None and lock.get("key") == tuple(restored)
        if not (decision.reason in ("foreign_session", "session_absent") or carried or (lock and ours and not window)):
            raise HA.HandsHeld(decision)
    elif unread is not None:
        unread.clear()
    if ev.token_present:
        refuse("owner_join_stop_or_rearm_pending")


def handoff_held(run, folder, word, decision, tell=None):
    """Her game is up with a lock: which window, what is latched, what clears it, and what comes next."""
    if decision.reason.startswith("owner_join_") or HA.operator_stop(decision):
        return
    try:
        launch, hwnd, window = farmhand_binding(run, word["host"])
    except HA.HandsHeld:
        return
    if launch.get("owner_attempt") != word["id"]:
        return
    causes = (decision.halt or {}).get("causes") or []
    what = "a HALT latched: " + ", ".join(causes) if causes else "her hands held: " + decision.reason
    command = subprocess.list2cmdline(HA.operator_command(folder, decision.paths))
    _tell(tell)(run, "owner_join_handoff_held", pid=launch["pid"], hwnd=hwnd, title=_title(window),
                reason=decision.reason, causes=causes, stream=sys.stderr,
                message=(f'Her window "{_title(window)}" (PID {launch["pid"]}, HWND {hwnd}) is up with {what}. It stays '
                         f'up. A person clears it with: [{command}]. A person may click her in from that window '
                         f'meanwhile (Co-op, Join LAN game, 127.0.0.1, the cabin named {word["selected"]["player_name"]}). '
                         'Then run rejoin again, without --owner-ready.'))


def wait_for_person(run, folder, word, adapter, pid, hwnd, *, held, save_reader, tell):
    host, binding = word["host"], word["selected"]
    launch, pinned_hwnd, facts = farmhand_binding(run, host)
    if (pid, hwnd, word["id"]) != (launch["pid"], pinned_hwnd, launch.get("owner_attempt")):
        refuse("owner_join_farmhand_changed")
    runtime = unread = None
    while True:
        adapter.check_stop()
        current, current_hwnd, _ = farmhand_binding(run, host)
        if current != launch or current_hwnd != hwnd:
            refuse("owner_join_farmhand_changed")
        _, session = HA.read_record(Path(folder) / "session.json")
        if not HA.launch_published(launch, session):
            if runtime is not None:
                other = session.get("runtime_instance_id") if isinstance(session, dict) else None
                unread = time.monotonic() if unread is None else unread
                if other not in (None, runtime) or time.monotonic() - unread >= UNREADABLE_S:
                    refuse("owner_join_runtime_changed")
        elif runtime is None:
            if (session.get("world_ready") is not False or session.get("player_id") not in (0, "0")
                    or session.get("player_name") != "" or session.get("is_split_screen") is not False):
                refuse("owner_join_title_unconfirmed")
            runtime = session["runtime_instance_id"]
            title = _title(facts)
            if not title or not wf.lan_host(host["pid"]):
                refuse("owner_join_local_host_unconfirmed")
            adapter.check_stop()
            tell(run, "owner_join_handoff", pid=pid, hwnd=hwnd, title=title, attempt_id=word["id"],
                 message=(f'In her window "{title}" (PID {pid}, HWND {hwnd}), open Co-op, choose Join LAN game, '
                          f'enter 127.0.0.1 and select the cabin named {binding["player_name"]}.'), stream=sys.stderr)
        elif session["runtime_instance_id"] != runtime:
            refuse("owner_join_runtime_changed")
        else:
            unread = None
            opened_as_main(run, session, launch, hwnd, facts, tell)
            if session.get("world_ready") is True:
                proof = verify(run, folder, host, binding, runtime=runtime, save_reader=save_reader, held=held,
                               check=adapter.check_stop, tell=tell)
                adapter.check_stop()
                return proof
        adapter.sleep(0.25)


def read_scene(session, pid, check=None):
    """The native save of her live scene, over a stream that belongs to her process; None when no sample shows it."""
    from scripts.m1_operator import poll_state
    from scripts.m1_operator.live_save import scene_save_identity

    for _ in range(SCENE_TRIES):
        if check is not None:
            check()
        diagnostics = {}
        try:
            poll_state.sample(SCENE_S, diagnostics=diagnostics, peer_pid=pid)
            return scene_save_identity(diagnostics.get("scene_identity"), session)
        except (OSError, ValueError):
            continue
    return None


def verify(run, folder, host, binding, *, runtime=None, save_reader=None, held=None, game=None, check=None, tell=None):
    """Pin her game and read a live scene without sending any game input. `game` is what `bound_game` pinned."""
    require_host(run, host)
    launch, hwnd, window, adoption = game or (*farmhand_binding(run, host), None)
    before = HA.read_evidence(folder, run, recovery=True, expected={})
    opened_as_main(run, before.session[1], launch, hwnd, window, tell)
    decision = HA.decide(before, act="observe", held=held)
    if decision.action == "hold":
        raise HA.HandsHeld(decision)
    session = decision.session
    if (not HA.launch_published(launch, session)
            or (runtime is not None and session["runtime_instance_id"] != runtime)):
        refuse("owner_join_runtime_changed")
    world_actor(session, binding)
    if sorted(wf.image_pids(SMAPI) or []) != sorted([host["pid"], launch["pid"]]) or wf.trial_running() is not False:
        refuse("owner_join_runtime_unconfirmed")
    try:
        save = read_scene(session, launch["pid"], check) if save_reader is None else save_reader(session)
        save_id = str(save["save_id"])
    except (OSError, ValueError, KeyError, TypeError):
        save_id = None
    fresh = HA.check(folder, run, act="observe", recovery=True, expected={}, held=held)
    if fresh.action == "hold":
        raise HA.HandsHeld(fresh)
    require_host(run, host)
    if adoption is None:
        after, after_hwnd, _ = farmhand_binding(run, host)
    else:
        after, after_hwnd, _, again = bound_game(run, host)
        if again != adoption:
            refuse("owner_join_launch_record_changed")
    if (after != launch or after_hwnd != hwnd or HA.identity(fresh.session) != HA.identity(session)):
        refuse("owner_join_runtime_changed")
    world_actor(fresh.session, binding)
    if save_id is None:
        refuse("owner_join_native_save_unknown")
    if save_id != binding["save_id"]:
        refuse("owner_join_wrong_save")
    if not wf.lan_host(host["pid"]):
        refuse("owner_join_local_host_unconfirmed")
    return ({k: session[k] for k in (*HA.IDENTITY_FIELDS, "player_id", "player_name")}
            | dict(save_id=save_id, owner_host=host, owner_farmhand=_farmhand_scope(launch, hwnd)))


def verify_joined(run, folder, host, binding, **options):
    """The two-game route: verify her recorded game, or one a person started by hand, which is recorded only then."""
    game = bound_game(run, host)
    if game[3] is not None:
        require_accounted(run, folder)
    proof = verify(run, folder, host, binding, game=game, **options)
    if game[3] is not None:
        adopt(run, folder, game)
    return proof


def _farmhand_scope(launch, hwnd):
    return dict(pid=launch["pid"], started=launch["process_start"], launch_id=launch["launch_id"], hwnd=hwnd)


def enrollment_session(run, folder, expected):
    """Final stand-alone cycle guard, also repeated after the child's live save sample."""
    ev = HA.read_evidence(folder, run, recovery=True, expected=expected)
    decision = HA.decide(ev, act="start")
    if decision.action == "hold":
        raise HA.HandsHeld(decision)
    if decision.action != "nothing" or decision.sentinels or ev.token_present:
        refuse("owner_join_stop_or_rearm_pending")
    host = expected["owner_host"]
    require_host(run, host)
    launch, hwnd, _ = farmhand_binding(run, host)
    if _farmhand_scope(launch, hwnd) != expected.get("owner_farmhand") or not HA.launch_published(launch, decision.session):
        refuse("owner_join_runtime_changed")
    binding = selected(run, expected["player_name"])
    if binding["save_id"] != expected["save_id"]:
        refuse("owner_join_selected_save_disagrees")
    world_actor(decision.session, binding)
    if not wf.lan_host(host["pid"]):
        refuse("owner_join_local_host_unconfirmed")
    return decision.session


def running_cycle(run):
    """The newest cycle while its own supervisor process lives, else None. A lock that cannot be read refuses: a second
    cycle beside a live one would put two hands on one inbox."""
    cycles = HA.cycle_dirs(run)
    if not cycles:
        return None
    lock = cycles[-1] / "supervisor.lock"
    try:
        pid, made = int(ipc.read_text(lock).strip()), lock.stat().st_mtime
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        refuse("owner_join_cycle_unknown")
    started = wf.process_start(pid)
    return cycles[-1] if started and wf.filetime_ms(started) <= made * 1000 + 2000 else None


def hand_over(run, cycle, proof):
    """Give a verified join to the cycle that waits for her, in the handoff record main's recovery writes."""
    _, state = HA.read_record(cycle / "cycle_state.json")
    previous = HA.expected_identity(state)
    successor = {k: proof[k] for k in (*HA.IDENTITY_FIELDS, "player_id", "player_name", "save_id")}
    if not previous or any(str(successor[k]) != str(previous[k]) for k in ("player_id", "player_name", "save_id")
                           if k in previous):
        refuse("owner_join_cycle_changed")
    ipc.write_json(Path(run) / "runtime/farmhand-recovery.json",
                   dict(cycle=cycle.name, previous=previous, successor=successor, pid=proof["owner_farmhand"]["pid"],
                        launch_id=proof["owner_farmhand"]["launch_id"]), fsync=True)


def handed_over(run, folder, cycle, state, held=None, *, host=None):
    """The waiting cycle's read: None, or the join `rejoin` handed to it, verified again here before her hands start.
    `host` is the HOST this wait began with: a join to another one is never taken up."""
    expected = HA.expected_identity(state)
    successor = HA.recovery_identity(run, cycle, expected)
    if successor == expected:
        return None
    # The lock her old game died with, or her new game's startup lock: only the mod's receipt for it ends the hold.
    held = dict(held, runtime_instance_id=None) if held else None
    if host is not None:
        require_host(run, host)
    proof = verify(run, folder, host or host_binding(run), selected(run, state["player_name"]), held=held)
    if any(str(proof[k]) != str(successor[k]) for k in successor):
        refuse("owner_join_runtime_changed")
    return proof


def _connected(run, folder):
    """(launch, window, session) of a farmhand in the hosted world, or None. Read only, for the notice."""
    host = host_binding(run)
    _, launch = HA.read_launch_record(Path(run) / FARMHAND_LAUNCH)
    hwnd = _hwnd(Path(run) / "runtime/fh-hwnd.txt")
    window = wf.window(hwnd) if hwnd else {}
    if not (isinstance(launch, dict) and launch.get("pid") and window.get("exists")
            and window.get("pid") == launch["pid"] != host["pid"]):
        launch, _, window, _ = bound_game(run, host)
    _, session = HA.read_record(Path(folder) / "session.json")
    if (not HA.launch_published(launch, session) or session.get("world_ready") is not True
            or session.get("is_main_player") is not False or session.get("is_split_screen") is True
            or not _title(window)):
        return None
    return launch, window, session


def stop_notice(run, folder, log, *, episode="stop"):
    """Say once per stop and client session that she is still connected, on the console and in the supervisor log."""
    if HA.session_kind(run) != "owner":
        return
    try:
        found = _connected(run, folder() if callable(folder) else folder)
        if found is None:
            return
        launch, window, session = found
        pid, title = launch["pid"], _title(window)
        key = HA.sha256(repr((pid, launch.get("process_start"), session["session_id"], episode)).encode("utf-8"))
        path = Path(run) / "runtime/owner-stop-notices" / (key + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        if ipc.create_json_once(path, {"pid": pid, "episode": episode}):
            from scripts.livepath.mabar_supervise import log_line
            message = (f'Her window "{title}" (PID {pid}) is still connected. The owner’s night or festival '
                       'can wait for her until that window is closed or she leaves through its own menu.')
            log(run, "owner_join_client_still_connected", pid=pid, title=title, episode=episode, message=message)
            log_line(run, message)
    except (HA.HandsHeld, OSError):
        return


def cycle_notice(run, folder, log, cycle):
    """One notice for one way a cycle ended, whichever process sees it first."""
    _, state = HA.read_record(Path(cycle) / "cycle_state.json")
    status = state.get("status") if isinstance(state, dict) else None
    stop_notice(run, folder, log, episode="stop" if status == "stopped" else "cycle:%s:%s" % (Path(cycle).name, status))


def worker_notice(run, folder, log):
    """The cycle's process died without releasing its lock while she is still connected."""
    try:
        cycles = HA.cycle_dirs(run)
        if cycles and HA.present(cycles[-1] / "supervisor.lock") and running_cycle(run) is None:
            cycle_notice(run, folder, log, cycles[-1])
    except HA.HandsHeld:
        return


def quiet_console(command, *, call=subprocess.call):
    """No child of `rejoin` may open a window in front of the owner's game. A process without a console restarts
    itself once with a console that has no window (the return code); one that was given a console window nobody reads
    hides it. None: go on in this process."""
    if os.name != "nt" or os.environ.get(OUTER):
        return None
    _, k32 = wf._w()
    if not k32.GetConsoleCP():
        exe = Path(sys.executable)
        if exe.name.casefold() == "pythonw.exe":
            exe = exe.with_name("python.exe")
        pid = os.getpid()
        return call([str(exe), *command], env=dict(os.environ, **{OUTER: "%d:%d" % (pid, wf.process_start(pid) or 0)}),
                    creationflags=subprocess.CREATE_NO_WINDOW)
    _hide_own_console()
    return None


def _hide_own_console():
    """A detached start under the venv's Python gets a console window of its own that nobody reads: hide that window."""
    if sys.stdout is not None and sys.stdout.isatty():
        return
    u32, k32 = wf._w()
    k32.GetConsoleWindow.restype = wintypes.HWND
    window = k32.GetConsoleWindow()
    if window and k32.GetConsoleProcessList((wintypes.DWORD * 4)(), 4) == 1:
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow(window, 0)
