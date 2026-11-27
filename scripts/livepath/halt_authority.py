"""fix148A (ADR 254): who may clear a native HALT, in one table, read right before every action on her hands.

Rules (CT, 2026-11-19/20): the program gains NO authority to clear a HALT beyond what main already clears; any automatic
step is tied to the very episode id, halt_seq and entries digest (or the exact bytes of the session's own sentinel) it
was decided for; unreadable, incomplete or foreign evidence is never "clear": it is "unknown" = report and hold; every
entry point that can start or resume her hands, close her game or launch it asks `decide` first (through `check`); a stop
the recovery did not issue itself outranks every recovery; a hold on a native latch ends by itself only on the mod's
receipt that the operator re-armed that very episode; a proof is a fresh, identity-matched clear that arrives before its
deadline; the owner's switch stays one switch, default OFF.

Evidence is read from the mod's own files at the moment of the decision: session.json, the persisted `halt.json`, the
raw native stop files, `mod_exit.json`. There is no age bound on a clear session.json: the mod republishes it only on a
change (ModEntry.PublishCommandSession), so a quiet game is not a stale one; a stale clear session.json cannot hide a
persisted latch or a raw stop because those are read from their own files.

Reads only. The one writer of a re-arm stays `tools/arti_rearm.py`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from arti_runtime import ipc

# The owner's open question (ADR 254 row 2b). OFF: a native_unclean_exit is the operator's, also after the session's
# own relaunch. Production never supplies an own-relaunch authorization.
OWN_RELAUNCH_UNCLEAN_EXIT_CLEARABLE = False
KEY_FIELDS = ("episode_id", "halt_seq", "entries_digest")
IDENTITY_FIELDS = ("session_id", "runtime_instance_id")
STOP_FILES = ("stop.json", "stop_all.json")
STOP_SOURCES = {"stop.json": "stop", "stop_all.json": "stop_all"}
# The cycle's own end markers, by the ending main's recovery takes up on its own (`own_ending`): the host-left marker
# (live_cycle.end_host_left -> request_stop(source="host_left")); a safety stop the supervisor self-heals on main
# writes `cycle_safety_stop` (live_cycle.drive), or `pad_watch` first when the pad watch stopped it (live_cycle.check).
OWN_SENTINEL_WRITERS = {"host_left": ("supervisor.host_left",),
                        "self_heal": ("supervisor.cycle_safety_stop", "supervisor.pad_watch")}
REARM_PROOF_S = 15  # the whole automatic archive or re-arm, the tool included, must prove itself within this
# What an action is: start or resume her hands; close (end) her running game; launch her game when none runs; observe
# (a supervisor or a running cycle asking whether her hands are held now).
ACTS = ("start", "close", "launch", "observe", "join")
UNREADABLE = type("Unreadable", (), {"__repr__": lambda self: "UNREADABLE", "__bool__": lambda self: True})()


class HandsHeld(RuntimeError):
    """A hold decided by the table: the caller does nothing more, reports it once and waits for the operator."""

    def __init__(self, decision):
        super().__init__("hands_held:" + decision.reason)
        self.decision = decision


@dataclass
class Decision:
    action: str  # "nothing" | "archive_sentinels" | "rearm" | "hold"
    reason: str
    halt: dict | None = None
    expect: tuple | None = None  # (episode_id, halt_seq, entries_digest) a re-arm is bound to
    sentinels: dict = field(default_factory=dict)  # path -> exact bytes the archive is bound to
    session: dict | None = None
    held: dict | None = None  # the episode a hold on a native latch is bound to (ends on its receipt)
    paths: list = field(default_factory=list)  # stop markers the operator's command must name
    watchdog: dict | None = None


@dataclass
class Evidence:
    session: tuple = (None, None)
    halt_file: tuple = (None, None)
    mod_exit: tuple = (None, None)
    stops: dict = field(default_factory=dict)  # present native stop files: name -> (raw, value)
    sentinels: dict = field(default_factory=dict)  # present LIVE7_STOP / OPERATOR_STOP markers: path -> (raw, value)
    run: Path | None = None
    newest: Path | None = None  # this run's newest cycle folder
    newest_state: tuple = (None, None)
    cycle_end: tuple = (None, None)
    expected: dict = field(default_factory=dict)
    startup: dict | None = None
    token_present: bool = False
    prior_launch: bool = False
    host_inert: str | None = None
    outranking: list = field(default_factory=list)  # read only for a recovery caller
    launch: tuple = (None, None)
    launch_end: dict | None = None  # what was recorded as that launch's game ended, if anybody saw it
    archive_proof: tuple = (None, None)


RECOVERY_GUARD_ENV = "ARTI_FARMHAND_RECOVERY_GUARD"


class RecoveryTimedOut(RuntimeError):
    """A recovery attempt exhausted main's restart budget."""


def _unique_fields(pairs):
    value = {}
    for key, field_value in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = field_value
    return value


def read_record(path):
    """(raw, value). Absent: (None, None). Present but unreadable (a sharing violation, a permission): (UNREADABLE,
    None). Readable but not JSON: (raw, None)."""
    try:
        raw = ipc.read_bytes(Path(path))
    except FileNotFoundError:
        return None, None
    except OSError:
        return UNREADABLE, None
    try:
        return raw, json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError):
        return raw, None


def present(path):
    """A file whose existence cannot be read counts as present (unknown is never clear)."""
    try:
        Path(path).stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def identity(record):
    return tuple(record.get(k) for k in IDENTITY_FIELDS) if isinstance(record, Mapping) else (None, None)


def halt_of(session):
    """The latched HALT in session.json, or None; a latch with a malformed or partial record is 'malformed'."""
    if not isinstance(session, Mapping):
        return None
    halt = session.get("halt")
    if session.get("stop_latched") is not True and halt is None:
        return None
    entries = halt.get("entries") if isinstance(halt, Mapping) else None
    if (session.get("stop_latched") is not True or not isinstance(halt, Mapping)
            or not isinstance(halt.get("episode_id"), str) or not halt["episode_id"]
            or type(halt.get("halt_seq")) is not int or halt["halt_seq"] < 1
            or not isinstance(halt.get("entries_digest"), str) or not halt["entries_digest"]
            or not isinstance(entries, list) or not entries
            or not all(isinstance(e, Mapping) and isinstance(e.get("cause"), str) and isinstance(e.get("source"), str)
                       for e in entries)):
        return dict(malformed=True)
    return dict(key=tuple(halt[k] for k in KEY_FIELDS), entries=[dict(e) for e in entries],
                causes=sorted({e["cause"] for e in entries}), persisted=halt.get("persisted") is True)


def _complete(session):
    """Every field the table reads is there with its type (the mod always writes them: CommandInboxSession)."""
    player = session.get("player_id")
    return (all(isinstance(session.get(k), str) and session[k] for k in IDENTITY_FIELDS)
            and type(session.get("stop_latched")) is bool and type(session.get("record_faulted")) is bool
            and type(session.get("world_ready")) is bool and isinstance(session.get("player_name"), str)
            and (type(player) is int or isinstance(player, str) and re.fullmatch(r"-?[0-9]+", player) is not None)
            and "halt" in session)


def expected_identity(state):
    """The enrolled actor and the last accepted native scope for this cycle."""
    if not isinstance(state, Mapping):
        return {}
    scope = state.get("stop_scope")
    if not isinstance(scope, Mapping):
        scope = {}
    return {k: v for k, v in {
        "player_id": state.get("player_id"), "player_name": state.get("player_name"),
        "runtime_instance_id": scope.get("runtime_instance_id"), "session_id": scope.get("session_id"),
        "save_id": state.get("save_id"),
    }.items() if v is not None}


def recovery_identity(run, cycle, expected):
    """Use a verified join only while its origin cycle and actual launch still match."""
    _, handoff = read_record(Path(run) / "runtime/farmhand-recovery.json")
    if not isinstance(handoff, dict) or handoff.get("cycle") != cycle.name or handoff.get("previous") != expected:
        return expected
    _, launch = read_record(Path(run) / "runtime/farmhand-launch.json")
    if not isinstance(launch, dict) or any(launch.get(k) != handoff.get(k) for k in ("launch_id", "pid")):
        return expected
    successor = handoff.get("successor")
    if not isinstance(successor, dict) or not all(identity(successor)):
        return expected
    if any(str(successor.get(k)) != str(expected[k]) for k in ("player_id", "player_name", "save_id") if k in expected):
        return expected
    return successor


def _matches_expected(session, expected, startup, act):
    if startup is not None and act == "join":
        # The worker pins the fresh runtime only after verifying the launch record and its live window.
        if session.get("runtime_instance_id") != startup.get("runtime_instance_id"):
            return False
        if session.get("world_ready") is False:
            return (type(session.get("is_main_player")) is bool
                    and session.get("player_id") in (0, "0") and session.get("player_name") == "")
        expected = {k: v for k, v in expected.items() if k not in IDENTITY_FIELDS}
    if session.get("is_main_player") is not False:
        return False
    return all(str(session.get(k)) == str(expected[k]) for k in (*IDENTITY_FIELDS, "player_id", "player_name")
               if k in expected)


def _exit_record_malformed(raw, record):
    """A present `mod_exit.json` that is not the mod's exact `{runtime_id, ms}` record (unreadable is malformed)."""
    return raw is not None and (
        not isinstance(record, Mapping) or record.keys() != {"runtime_id", "ms"}
        or not isinstance(record.get("runtime_id"), str)
        or not record["runtime_id"] or type(record.get("ms")) is not int or record["ms"] <= 0
    )


def _clean_exit(session, record):
    """The mod's exit record proves this very runtime exited after its last session.json publication."""
    return (session is not None and isinstance(record, Mapping)
            and record.get("runtime_id") == session["runtime_instance_id"]
            and type(session.get("native_ms")) is int and type(record.get("ms")) is int
            and record["ms"] >= session["native_ms"])


def _closed_title_screen(session, exit_raw, exit_record):
    """A game that never left the title screen (no world, player 0, no name) and whose own well-formed exit
    record proves this very runtime has exited. At the title screen every game is its own main player, so
    `is_main_player` names no one there; such a session is neither a live player nor a foreign one. Read only by
    `decide` for a launch in a solo run (`_solo_run`): what follows (the latch, the lock that would be lost) is decided
    as for any launch."""
    return (session.get("world_ready") is False and type(session.get("is_main_player")) is bool
            and session.get("player_id") in (0, "0") and session.get("player_name") == ""
            and not _exit_record_malformed(exit_raw, exit_record) and _clean_exit(session, exit_record))


def _disk_key(record):
    """The persisted record's key when it is a valid `arti.halt/1` record, else None."""
    if (not isinstance(record, Mapping) or record.get("v") != "arti.halt/1"
            or record.get("state") not in ("latched", "rearmed")
            or not isinstance(record.get("episode_id"), str) or not record["episode_id"]
            or type(record.get("halt_seq")) is not int or record["halt_seq"] < 1
            or not isinstance(record.get("entries_digest"), str) or not isinstance(record.get("entries"), list)
            or not record["entries"]):
        return None
    if record["state"] == "rearmed":
        receipt = record.get("rearmed")
        if (not isinstance(receipt, dict) or receipt.keys() != {"halt_seq", "token_sha256", "ms"}
                or type(receipt.get("halt_seq")) is not int or receipt["halt_seq"] != record["halt_seq"]
                or type(receipt.get("ms")) is not int or receipt["ms"] < 0
                or not isinstance(receipt.get("token_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt["token_sha256"])
                or not _valid_halt_body(record)):
            return None
    return tuple(record[k] for k in KEY_FIELDS)


def _valid_halt_body(record):
    def native_id(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/|-]{1,128}", value)

    def wall_ms(value):
        return type(value) is int and 0 <= value <= 2**63 - 1

    required = {"v", "state", "episode_id", "halt_seq", "scope", "since_ms", "entries", "entries_digest"}
    if (record.keys() - required - {"last_rearmed", "rearmed"} or not native_id(record["episode_id"])
            or record["halt_seq"] > 2**63 - 1 or record.get("scope") not in ("core", "all")
            or not wall_ms(record.get("since_ms"))):
        return False
    if "last_rearmed" in record:
        history = record["last_rearmed"]
        if (not isinstance(history, dict) or history.keys() != {"episode_id", "halt_seq", "stop_ids", "ms"}
                or not native_id(history["episode_id"]) or type(history["halt_seq"]) is not int
                or not 1 <= history["halt_seq"] <= 2**63 - 1 or not wall_ms(history["ms"])
                or not isinstance(history["stop_ids"], list) or len(history["stop_ids"]) > 16
                or not all(native_id(stop) for stop in history["stop_ids"])):
            return False
    fields = {"n", "source", "stop_id", "stop_sha256", "cause", "scope", "writer", "latched_ms", "file_present"}
    causes = ("H1", "H2", "H3", "H4", "H5", "identity", "owner", "operator", "legacy", "malformed",
              "halt_record_corrupt", "native_containment", "native_unclean_exit", "entries_overflow")
    entries = record["entries"]
    if not 1 <= len(entries) <= 16 or any(
        not isinstance(e, dict) or e.keys() != fields or type(e["n"]) is not int or not 1 <= e["n"] <= 16
        or not wall_ms(e["latched_ms"]) or type(e["file_present"]) is not bool
        or e["source"] not in ("stop", "stop_all", "native") or e["scope"] not in ("core", "all")
        or e["cause"] not in causes or e["writer"] not in ("safety", "arti_stop", "launcher", "legacy", "native")
        or not native_id(e["stop_id"]) or not isinstance(e["stop_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", e["stop_sha256"])
        for e in entries
    ):
        return False
    native = [{k: v for k, v in e.items() if k != "file_present"} for e in entries]
    encoded = json.dumps(native, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded) == record["entries_digest"]


def _restores_latch(record, session, halt):
    """Match the native startup record, including its C8 digest."""
    if _disk_key(record) != halt["key"] or "rearmed" in record or not halt["persisted"] or not _valid_halt_body(record):
        return False
    native = [{k: v for k, v in e.items() if k != "file_present"} for e in record["entries"]]
    live = [{k: v for k, v in e.items() if k != "file_present"} for e in halt["entries"]]
    return (native == live and record["scope"] == session["halt"].get("scope")
            and record["since_ms"] == session["halt"].get("since_ms"))


def launch_published(launch, session):
    """The latest launch can bind only a new native publication produced after its creation boundary."""
    return (isinstance(launch, dict) and isinstance(session, dict) and _complete(session) and type(launch.get("pid")) is int
            and launch["pid"] > 0 and isinstance(launch.get("launch_id"), str) and bool(launch["launch_id"])
            and "previous_runtime" in launch and type(launch.get("issued_ms")) is int
            and type(session.get("native_ms")) is int and session["native_ms"] >= launch["issued_ms"]
            and session["runtime_instance_id"] != launch["previous_runtime"]
            and launch.get("runtime_instance_id", session["runtime_instance_id"]) == session["runtime_instance_id"])


def launch_end(run, launch):
    """The row `owner_join.record_ended` wrote as this launch's game ended (a pending record stands for the game
    it replaces), or None."""
    if isinstance(launch, Mapping) and launch.get("pid") is None:
        launch = launch.get("replaces")
    folder = Path(run) / "runtime/owner-game-ended"  # only a run the owner hosts has it; every other run reads as before
    if not isinstance(launch, Mapping) or not launch.get("launch_id") or not folder.is_dir():
        return None
    _, row = read_record(folder / (str(launch["launch_id"]) + ".json"))
    return row if isinstance(row, dict) and row.get("launch_id") == launch["launch_id"] else None


def ended_lock(ev):
    """What became of the lock of the game whose last publication `ev` holds, whatever else holds a launch now: "none",
    "kept" (its halt file restores it at the next start), "lost" (no start can restore it) or "unknown"."""
    session = ev.session[1]
    disk_raw, disk = ev.halt_file
    if (not isinstance(session, Mapping) or not _complete(session) or session["record_faulted"] or disk_raw is UNREADABLE
            or _exit_record_malformed(*ev.mod_exit)):
        return "unknown"
    key = _disk_key(disk) if disk_raw is not None else None
    halt = halt_of(session)
    if (disk_raw is not None and key is None) or (halt is not None and halt.get("malformed")):
        return "unknown"
    latched = key if key is not None and disk["state"] == "latched" else None
    if halt is None:
        return "none" if latched is None else "unknown"
    kept = (_clean_exit(session, ev.mod_exit[1]) and isinstance(disk_raw, bytes) and len(disk_raw) <= 32768
            and not ev.token_present and latched == halt["key"] and _restores_latch(disk, session, halt))
    return "kept" if kept else "lost"


def _end_accounted(ev, session):
    """Before a relaunch on the owner's word, how the recorded launch's game ended is known (it was recorded as it
    died, and no lock was lost) or its last publication is still the one read here. A session somebody else published
    since would hide a lock that nobody saw."""
    end, launch = ev.launch_end, ev.launch[1]
    runtime = session["runtime_instance_id"]
    if isinstance(end, Mapping):
        return (end.get("known") is True and end.get("lost_lock") is False) or end.get("runtime_instance_id") == runtime
    return runtime == ev.expected.get("runtime_instance_id") or (
        isinstance(launch, Mapping) and runtime == launch.get("previous_runtime"))


def read_launch_record(path):
    raw, launch = read_record(path)
    if isinstance(launch, dict) and launch.get("not_started") is True and launch.get("pid") is None:
        return None, None
    return raw, launch


def _run_has_launched(ev):
    """Read by `decide` for a launch in a solo run only (`_solo_run`): this run has launched her game, or
    has already played. A launch record exists (any bytes, also unreadable ones; a `not_started` record, read as none by
    `read_launch_record`, is none), or the run has a cycle folder (a run that has played must have launched, so a
    missing record there still holds). Only then must her inbox's session.json be the latest launch's own publication.
    A solo run fresh from `mabar prepare` has neither, and its launch skips only that publication check: the run's stops
    and archive proof still apply, and the launch relies on the clean-exit witness of the earlier runtime (a missing
    launch record is not proof that no process started)."""
    launch_raw, _ = ev.launch
    return launch_raw is not None or (ev.run is not None and (ev.newest is not None or bool(cycle_dirs(ev.run))))


HELPER_CONFIG = "runtime/batch-host/host-mods/ArtiHostHelper/config.json"  # the helper, installed in this run's HOST


def _read_json(path, default=None):
    """The reader `session_kind` always used (mabar_supervise._read_json): absent or unreadable is `default`."""
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def session_kind(run):
    """Who hosts this session, from what mabar itself recorded when it launched the HOST. 'solo': the run's
    trial config names the helper program as the HOST farmer's controller (`mabar host --helper` writes
    stardew_host_controller=harness) and the helper is installed in this run's HOST. 'owner': the owner hosts
    (controller=owner, no helper). None when the two are missing or disagree: rung 6 is refused, never guessed.
    Lives here so that the launch gate (`_solo_run`) reads the same rule; mabar_supervise imports it. A trial config
    that is JSON but not an object names no controller."""
    run = Path(run)
    trial = _read_json(run / "trial-config.json")
    controller = trial.get("stardew_host_controller") if isinstance(trial, Mapping) else None
    helper = (run / HELPER_CONFIG).is_file()
    if controller == "harness" and helper:
        return "solo"
    if controller == "owner" and not helper:
        return "owner"
    return None


def prepared_owner_run(run):
    """The owner hosts this run and `mabar prepare` recorded the farm it continues. Only such a run can check
    her world against the prepared save; an owner-hosted run on a new farm has nothing to check it against."""
    return session_kind(run) == "owner" and present(Path(run) / "continued.json")


def _owner_admits(ev, owner_launch):
    """Whether the owner's live word speaks for this launch. `owner_launch` is what the caller that holds the
    word verified at this boundary: "first" (this run never started her game) or "again" (the recorded process of its
    previous launch is verifiably gone). No record read by the table carries it. A pending record without a PID, in a
    run that has not played, is a launcher that died before a start."""
    if not owner_launch or ev.run is None or session_kind(ev.run) != "owner":
        return False
    if owner_launch == "again":
        return True
    launch = ev.launch[1]
    return not _run_has_launched(ev) or (isinstance(launch, dict) and "pid" in launch and launch["pid"] is None
                                         and not cycle_dirs(ev.run))


def _solo_run(ev):
    """The program's helper hosts this run, `session_kind(run) == "solo"`,
    read now. Only then may a launch pass the two admissions in `decide` (a closed title screen at the
    `foreign_session` row, a fresh run at the `latest_launch_session_unconfirmed` row). In an owner-hosted run her
    title-screen join can send input to the owner's foreground game, so there both rows decide as on main. No run
    folder, an owner run, a kind that is not recorded, or a trial config that cannot be read is not solo."""
    if ev.run is None:
        return False
    try:
        return session_kind(ev.run) == "solo"
    except OSError:
        return False


def _part_of_latch(halt, name, raw):
    sha = sha256(raw)
    return any(e.get("source") == STOP_SOURCES[name] and e.get("stop_sha256") == sha for e in halt["entries"])


def bind(session, halt, held=None):
    """The episode a hold is bound to: the latest episode seen, its highest seq (an episode grows while it waits)."""
    episode, seq, digest = halt["key"]
    if isinstance(held, Mapping) and held.get("episode_id") == episode and type(held.get("halt_seq")) is int:
        if held["halt_seq"] > seq:
            seq, digest = held["halt_seq"], held.get("entries_digest")
    return dict(episode_id=episode, halt_seq=seq, entries_digest=digest, session_id=session.get("session_id"),
                runtime_instance_id=session.get("runtime_instance_id"))


def receipt_for(session, held):
    """The mod's own receipt that the operator re-armed the held episode (any later seq of it covers it), with the
    latch gone. A clear without it (a replacement game, an old snapshot) never ends a hold."""
    rearmed = session.get("rearmed") if isinstance(session, Mapping) else None
    return (isinstance(rearmed, Mapping) and session.get("stop_latched") is False and session.get("halt") is None
            and rearmed.get("episode_id") == held.get("episode_id") and type(rearmed.get("halt_seq")) is int
            and type(held.get("halt_seq")) is int and rearmed["halt_seq"] >= held["halt_seq"]
            and (held.get("runtime_instance_id") is None
                 or session.get("runtime_instance_id") == held["runtime_instance_id"]))


def restored_receipt(ev, key):
    """Native persisted proof covers the original key, including a same-episode escalation."""
    session = ev.session[1]
    raw, record = ev.halt_file
    found = _disk_key(record) if isinstance(raw, bytes) and len(raw) <= 32768 else None
    if (not found or record["state"] != "rearmed" or found[0] != key[0] or found[1] < key[1]
            or (found[1] == key[1] and found[2] != key[2]) or ev.stops):
        return False
    return receipt_for(session, dict(episode_id=found[0], halt_seq=found[1]))


def own_startup_receipt(ev, session):
    """The mod's persisted proof that a person re-armed this very runtime's single startup lock for an unclean exit."""
    record = ev.halt_file[1]
    key = _disk_key(record)
    return bool(isinstance(session, dict) and key and record["state"] == "rearmed" and len(record["entries"]) == 1
                and record["entries"][0].get("cause") == "native_unclean_exit"
                and record["entries"][0].get("source") == "native"
                and key[0] == "native:%s:%s" % (session.get("runtime_instance_id"), key[1]) and restored_receipt(ev, key))


def manual_crash_successor(ev, previous):
    """A native re-arm on the manually launched successor can replace the old enrolled runtime."""
    session, launch = ev.session[1], ev.launch[1]
    old_runtime = previous.get("runtime_instance_id")
    if (not old_runtime or not isinstance(session, dict) or session.get("world_ready") is not True
            or not launch_published(launch, session) or launch.get("previous_runtime") != old_runtime):
        return None
    runtime = session["runtime_instance_id"]
    next_identity = dict(previous, runtime_instance_id=runtime, session_id=session["session_id"])
    if not _matches_expected(session, next_identity, startup=None, act="observe"):
        return None
    return next_identity if own_startup_receipt(ev, session) else None


def _resolved(path):
    try:
        return str(Path(str(path)).resolve())
    except (OSError, ValueError):
        return str(path)


def own_ending(state, cycle_end=(None, None)):
    """How the newest cycle ended, when main's recovery takes that ending up on its own (CT round three: what main
    self-heals stays self-healed, nothing is added): "host_left" (the rejoin after the host left) or "self_heal" (the
    supervisor's own retry, by main's own `mabar_supervise.classify_cycle_stop`, called as it is). Else None."""
    if not isinstance(state, Mapping):
        return None
    status, reason = cycle_end
    status = status or state.get("status")
    reason = reason or state.get("stop_reason")
    if status == "host_left" or reason == "host_left":
        return "host_left"
    from scripts.livepath.mabar_supervise import classify_cycle_stop
    kind, _ = classify_cycle_stop(status, reason if isinstance(reason, str) else None)
    return "self_heal" if kind == "self_heal" else None


def own_host_left(path, record, ev):
    """Review R6 B5: the cycle's OWN end marker of the cycle that just ended, nothing else. Its writer (the cycle's own
    for that ending: `OWN_SENTINEL_WRITERS`), its place (that cycle's folder or that cycle's recorded run-level stop
    file), its cycle (this run's newest, ended in a way main's recovery takes up: `own_ending`) and its scope (the
    session and game that cycle recorded) must all agree. An operator's or an observed stop is never the cycle's own."""
    from arti_stardew_stop import scoped
    if (not scoped(record) or not isinstance(record.get("cycle_id"), str)
            or ev.newest is None or _resolved(record["cycle_id"]) != _resolved(ev.newest)):
        return False
    _, state = ev.newest_state
    if not isinstance(state, Mapping):
        return False
    scope = state.get("stop_scope")
    if not isinstance(scope, Mapping) or any(not scope.get(k) or record.get(k) != scope.get(k)
                                             for k in ("runtime_instance_id", "session_id")):
        return False
    places = {_resolved(Path(ev.newest) / "LIVE7_STOP")}
    run_stop = state.get("live7_stop_file")
    if isinstance(run_stop, str) and run_stop:
        places.add(_resolved(run_stop))
    elif ev.run is not None:
        places.add(_resolved(Path(ev.run) / "runtime/LIVE7_STOP"))
    if record.get("writer") == "pad_watch.violation":
        status, reason = ev.cycle_end
        return (reason == "pad_watch_violation" and status in ("active", "starting", "rejoining", "safety_stop")
                and _resolved(path) == _resolved(ev.run / "runtime/LIVE7_STOP")
                and isinstance(run_stop, str) and _resolved(run_stop) == _resolved(path))
    if record.get("writer") not in OWN_SENTINEL_WRITERS.get(own_ending(state, ev.cycle_end), ()):
        return False
    return _resolved(path) in places


def _water_row(ev, session, halt):
    """Row 5: main's `recover_nonowner_stop` exception, unchanged in what it may clear: only a `runtime.water_stop`
    stop.json that native shows latched, alone, with idle hands. Here it is also bound to the episode first seen."""
    from arti_stardew_stop import recoverable_native_state, stop_class
    raw, value = ev.stops.get("stop.json", (None, None))
    return (not ev.token_present and set(ev.stops) == {"stop.json"}
            and isinstance(raw, bytes) and stop_class(value) == "recoverable"
            and recoverable_native_state(session, raw) is None)


def _own_relaunch_row(session, halt, own):
    """Row 2b with the switch ON (review R6 SHOULD FIX): one native `native_unclean_exit` entry the CURRENT runtime
    latched at its start, after the session's own recorded close request of the game before it, with no other launch
    between (the recorded successor is the game running now). A time alone, another runtime or request never does."""
    if not isinstance(own, Mapping) or len(halt["entries"]) != 1:
        return False
    entry, runtime = halt["entries"][0], session["runtime_instance_id"]
    return (entry.get("source") == "native" and entry.get("cause") == "native_unclean_exit"
            and entry.get("stop_id") == "native:%s:%s" % (runtime, halt["key"][1])
            and isinstance(own.get("request_id"), str) and bool(own["request_id"])
            and own.get("successor_runtime_id") == runtime and own.get("closed_runtime_id") not in (None, "", runtime)
            and type(own.get("close_ms")) is int and type(entry.get("latched_ms")) is int
            and entry["latched_ms"] >= own["close_ms"])


WATCHDOG_EXIT = 0x80131623
WATCHDOG_LIMIT = 2


def watchdog_launch_proof(ev, *, reservation=None):
    """Bind the launcher exit, the exact raw stderr, and the recorded attempt's containment finding."""
    launch, session = ev.launch[1], ev.session[1]
    if (ev.run is None or ev.newest is None or ev.outranking or ev.sentinels or ev.host_inert
            or ev.stops or ev.token_present or not launch_published(launch, session)
            or not _matches_expected(session, ev.expected, startup=None, act="observe")):
        return None
    runtime = ev.run / "runtime"
    _, exit_record = read_record(runtime / "farmhand-exit.json")
    if (not isinstance(exit_record, dict) or any(exit_record.get(k) != launch[k] for k in ("pid", "launch_id"))
            or type(exit_record.get("returncode")) is not int
            or exit_record["returncode"] not in (WATCHDOG_EXIT, WATCHDOG_EXIT - (1 << 32))):
        return None
    try:
        exit_ms = int(datetime.fromisoformat(exit_record["utc"]).timestamp() * 1000)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return None
    if exit_ms + 999 < max(launch["issued_ms"], session["native_ms"]):
        return None
    stderr = launch.get("stderr")
    if not isinstance(stderr, str) or not re.fullmatch(r"farmhand-stderr-[A-Za-z0-9-]+\.raw", stderr):
        return None
    state = ev.newest_state[1]
    try:
        attempt = state["days"][-1]["attempts"][-1]
        attempt_id = attempt["id"]
    except (TypeError, KeyError, IndexError):
        return None
    if (not isinstance(attempt_id, str) or not re.fullmatch(r"day-[0-9]+-attempt-[0-9]+", attempt_id)
            or attempt.get("session") != session["session_id"]):
        return None
    try:
        raw = ipc.read_bytes(runtime / stderr)
    except OSError:
        return None
    prefix = "ARTI_BG_CONTAINMENT:"
    text = raw.decode("utf-16-le" if prefix.encode("utf-16-le") in raw else "utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if prefix in line]
    if len(lines) != 1:
        return None
    match = re.fullmatch(r"(?:Process terminated\. )?ARTI_BG_CONTAINMENT:"
                         r"(T1UncontrolledMotion|T2DispatchImpossible|T3HeartbeatLoss|T4GateClosedUnresolved|"
                         r"PreHCleanupTimeout|T5PressReleaseUnresolved|WATCHDOG_FAULT:[A-Za-z_][A-Za-z0-9_]*"
                         r"(?:`[0-9]+)?)"
                         r"\|request=([A-Za-z0-9._:/-]+)\|runtime=([A-Za-z0-9._:/-]+)", lines[0])
    if not match or match[3] != session["runtime_instance_id"]:
        return None
    if session.get("inflight_request_id") not in (None, match[2]):
        return None
    verified = False
    for snapshot in (ev.newest / attempt_id / "process").glob("*/containment.json"):
        _, record = read_record(snapshot)
        _, launch_copy = read_record(snapshot.with_name("farmhand-launch.json"))
        if (launch_copy != launch or not isinstance(record, dict) or record.get("token") != prefix[:-1]
                or record.get("unreadable") != [] or not isinstance(record.get("run_dir"), str)
                or _resolved(record["run_dir"]) != _resolved(runtime) or not isinstance(record.get("hits"), list)):
            continue
        if any(isinstance(hit, dict) and hit.get("file") == stderr and hit.get("sha256") == sha256(raw)
               and hit.get("lines") == lines for hit in record["hits"]):
            verified = True
            break
    if not verified:
        return None
    proof = dict(launch_id=launch["launch_id"], pid=launch["pid"], runtime_instance_id=match[3],
                 request_id=match[2], trigger=match[1])
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", proof["launch_id"]):
        return None
    ledger = ev.run / "runtime/watchdog-recoveries"
    if reservation is not None:
        return proof if watchdog_reservation(ev.run, reservation) and reservation.get("proof") == proof else None
    if present(ledger / ("launch-" + proof["launch_id"] + ".json")):
        return None
    if all(present(ledger / f"{n}.json") for n in range(1, WATCHDOG_LIMIT + 1)):
        return None
    return proof


def reserve_watchdog(run, proof):
    """Exclusive, durable per-crash claim plus one of the run's two launch slots."""
    ledger = Path(run) / "runtime/watchdog-recoveries"
    ledger.mkdir(parents=True, exist_ok=True)
    claim = ledger / ("launch-" + proof["launch_id"] + ".json")
    if not ipc.create_json_once(claim, proof, fsync=True):
        return None
    from tools.arti_rearm import archive_owned_record
    try:
        for number in range(1, WATCHDOG_LIMIT + 1):
            if ipc.create_json_once(ledger / f"{number}.json", proof, fsync=True):
                return dict(slot=number, proof=proof)
    except OSError:
        archive_owned_record(claim, proof, reason="watchdog_slot_write_failed")
        raise
    archive_owned_record(claim, proof, reason="watchdog_slots_unavailable")
    return None


def release_watchdog(run, reservation):
    """Refund an unstarted reservation and let that exact crash be considered again."""
    from tools.arti_rearm import archive_owned_record
    ledger = Path(run) / "runtime/watchdog-recoveries"
    proof = reservation["proof"]
    reason = "watchdog_launch_not_created"
    if archive_owned_record(ledger / f"{reservation['slot']}.json", proof, reason=reason):
        archive_owned_record(ledger / ("launch-" + proof["launch_id"] + ".json"), proof, reason=reason)


def watchdog_reservation(run, reservation):
    if not isinstance(reservation, dict) or reservation.get("slot") not in range(1, WATCHDOG_LIMIT + 1):
        return False
    _, proof = read_record(Path(run) / "runtime/watchdog-recoveries" / f"{reservation['slot']}.json")
    return isinstance(proof, dict) and proof == reservation.get("proof")


def watchdog_successor(ev, session, halt):
    launch, startup = ev.launch[1], ev.startup
    if (ev.run is None or not isinstance(startup, dict) or not launch_published(launch, session)
            or startup.get("launch_id") != launch["launch_id"] or startup.get("pid") != launch["pid"]
            or not watchdog_reservation(ev.run, launch.get("watchdog")) or len(halt["entries"]) != 1):
        return False
    prior = launch["watchdog"]["proof"]
    entry = halt["entries"][0]
    runtime = session["runtime_instance_id"]
    return (prior.get("runtime_instance_id") == launch.get("previous_runtime") != runtime
            and entry.get("cause") == "native_unclean_exit" and entry.get("source") == "native"
            and entry.get("stop_id") == "native:%s:%s" % (runtime, halt["key"][1])
            and halt["key"][0] == entry["stop_id"] and type(entry.get("latched_ms")) is int
            and entry["latched_ms"] >= launch["issued_ms"])


def decide(ev, *, act, held=None, water=False, own_relaunch=None, switch=OWN_RELAUNCH_UNCLEAN_EXIT_CLEARABLE,
           proof_id=None, watchdog=None, clear_watchdog=False, owner_launch=False):
    """The table (ADR 254) as one decision for `act` on the evidence `ev` (see `read_evidence`)."""
    if act not in ACTS:
        raise ValueError(act)
    raw, session = ev.session
    session = session if isinstance(session, dict) else None
    paths = [str(p) for p in ev.sentinels]

    def hold(reason, halt=None, binding=None):
        return Decision("hold", reason, halt=halt, session=session, held=binding if binding is not None else held,
                        paths=paths)

    if ev.outranking:  # rows 8 and 11: a stop the recovery did not issue itself
        return hold("stop_outranks_recovery:" + ",".join(ev.outranking))
    proof_raw, proof = ev.archive_proof
    if proof_raw is not None and (not isinstance(proof, dict)
                                  or (proof.get("state") != "proven" and not (
                                      proof_id and proof.get("id") == proof_id))):
        return hold("archive_proof_pending")
    if ev.host_inert and (act in ("close", "launch", "join")
                          or ev.host_inert not in ("LIVE7_STOP", "runtime/LIVE7_STOP", "cycle/LIVE7_STOP")):
        return hold("stop_outranks_recovery:host_inert")
    bound = {}
    for path, (marker_raw, record) in ev.sentinels.items():
        if marker_raw is UNREADABLE:
            return hold("stop_marker_unreadable:" + Path(path).name)
        if not own_host_left(path, record, ev):
            writer = record.get("writer") if isinstance(record, Mapping) else None
            return hold("stop_outranks_recovery:%s:%s" % (Path(path).name, writer or "unknown"))
        bound[str(path)] = marker_raw
    if raw is UNREADABLE or (raw is not None and session is None):
        return hold("session_unreadable")  # row 6
    if raw is None and (act != "launch" or ev.expected or held or ev.prior_launch or ev.mod_exit[0] is not None):
        return hold("session_absent")  # row 6: her hands are never started or her game closed on no evidence
    # The caller can pass a live grant only at a launch boundary. No record read by this table carries it.
    solo = act == "launch" and session is not None and _solo_run(ev)
    owner = act == "launch" and session is not None and not solo and _owner_admits(ev, owner_launch)
    again = owner and owner_launch == "again"
    if session is not None:
        if not _complete(session):
            return hold("halt_evidence_unknown")
        # On a word that relaunches her, the publication recorded as her game ended is hers whatever screen it
        # ended on (its process is verifiably gone); any other session passes only main's identity or the clean title exit.
        if (not _matches_expected(session, ev.expected, ev.startup, act)
                and not ((solo or owner) and _closed_title_screen(session, *ev.mod_exit))
                and not (again and isinstance(ev.launch_end, Mapping)
                         and ev.launch_end.get("runtime_instance_id") == session["runtime_instance_id"])):
            return hold("foreign_session")
        if (act != "join" and isinstance(held, Mapping) and held.get("runtime_instance_id")
                and session["runtime_instance_id"] != held["runtime_instance_id"]):
            return hold("operator_rearm_not_seen:%s" % held.get("episode_id"))
        if session["record_faulted"]:
            return hold("halt_record_faulted")  # the mod could not read a stop record: it may latch next poll
    disk_raw, disk = ev.halt_file
    if disk_raw is UNREADABLE:
        return hold("halt_record_unreadable")
    disk_latched = None
    if disk_raw is not None:
        key = _disk_key(disk)
        if key is None:
            return hold("halt_record_unknown")
        disk_latched = key if disk["state"] == "latched" else None
    halt = halt_of(session) if session is not None else None
    if halt is not None and halt.get("malformed"):
        return hold("halt_malformed", halt)
    binding = bind(session, halt, held) if halt is not None else None
    launch_raw, launch = ev.launch
    if (act == "launch" and session is not None and not owner
            and (_run_has_launched(ev) if solo else (ev.run is not None or launch_raw is not None))
            and not launch_published(launch, session)):
        return hold("latest_launch_session_unconfirmed", halt, binding)
    if again and not _end_accounted(ev, session):
        return hold("latest_launch_session_unconfirmed", halt, binding)
    exit_raw, exit_record = ev.mod_exit
    if act == "launch" and _exit_record_malformed(exit_raw, exit_record):
        return hold("exit_record_unreadable", halt, binding)
    clean_exit = _clean_exit(session, exit_record)
    for name in sorted(ev.stops):  # a raw native STOP the mod has not latched (yet) is a stop
        stop_raw, _ = ev.stops[name]
        if stop_raw is UNREADABLE:
            return hold("native_stop_unreadable:" + name, halt, binding)
        if halt is None or not _part_of_latch(halt, name, stop_raw):
            return hold("native_stop_pending:" + name, halt, binding)
    if halt is not None:
        if act == "start" and water and not bound and _water_row(ev, session, halt):
            return Decision("rearm", "runtime.water_stop", halt=halt, expect=halt["key"], session=session)
        if act == "start" and switch and _own_relaunch_row(session, halt, own_relaunch):
            return Decision("rearm", "own_relaunch_unclean_exit", halt=halt, expect=halt["key"], session=session)
        if (act == "join" and clear_watchdog and not bound and not ev.stops and not ev.token_present
                and disk_latched == halt["key"] and watchdog_successor(ev, session, halt)
                and _restores_latch(disk, session, halt)):
            return Decision("rearm", "watchdog_successor_unclean_exit", halt=halt, expect=halt["key"], session=session)
        if act != "launch":
            return hold("halt_latched:" + ",".join(halt["causes"]), halt, binding)
        if ev.stops:
            return hold("native_stop_blocks_launch", halt, binding)
        if isinstance(held, Mapping):
            episode, sequence, digest = halt["key"]
            observed_seq = held.get("halt_seq")
            if (held.get("episode_id") != episode or type(observed_seq) is not int or sequence < observed_seq
                    or (sequence == observed_seq and held.get("entries_digest") not in (None, digest))):
                return hold("launch_halt_key_changed", halt, held)
        if (not clean_exit or not isinstance(disk_raw, bytes) or len(disk_raw) > 32768 or ev.token_present
                or disk_latched != halt["key"] or not _restores_latch(disk, session, halt)):
            return hold("lock_would_be_lost:" + ",".join(halt["causes"]), halt, binding)
    elif disk_latched is not None:
        return hold("persisted_halt_disagrees")  # the game's own file says latched while session.json says clear
    elif isinstance(held, Mapping) and not receipt_for(session, held):
        return hold("operator_rearm_not_seen:%s" % held.get("episode_id"))
    elif act == "launch" and session is not None and not clean_exit:
        if watchdog is not None and not bound:
            return Decision("nothing", "watchdog_launch", session=session, watchdog=watchdog)
        if again:  # the mod latches this at its startup and a person clears it; the word clears nothing
            return Decision("nothing", "owner_relaunch_unclean_exit", session=session, sentinels=bound, paths=paths)
        # The mod would latch this on its next startup. Hold before the program starts a replacement.
        return hold("native_unclean_exit_before_launch")
    if bound and act == "start":
        return Decision("archive_sentinels", "own_sentinel:%s" % own_ending(ev.newest_state[1], ev.cycle_end), sentinels=bound,
                        session=session, paths=paths)
    if bound and act == "launch" and halt is not None:
        return hold("launch_waits_for_clear_end_markers", halt, binding)
    return Decision("nothing", "clear" if halt is None else "launch_relatches:" + ",".join(halt["causes"]),
                    halt=halt, session=session, held=binding, sentinels=bound, paths=paths)


def cycle_dirs(run):
    root = Path(run) / "runtime/live7"
    return sorted((p for p in root.glob("c*") if p.is_dir() and p.name[1:].isdigit()), key=lambda p: int(p.name[1:]))


def read_evidence(inbox, run=None, *, recovery=False, sentinels=True, expected=None, startup=None):
    """Everything `decide` reads, read now. `run` adds this run's stop markers (and, for a recovery caller, the stops
    that outrank recovery); `sentinels=False` leaves the markers to the caller (the cycle keeps main's reading)."""
    ev = Evidence()
    if inbox is None:
        ev.session = (UNREADABLE, None)
        return ev
    folder = Path(inbox)
    ev.session = read_record(folder / "session.json")
    ev.halt_file = read_record(folder / "halt.json")
    ev.mod_exit = read_record(folder / "mod_exit.json")
    ev.token_present = present(folder / "rearm.json")
    if ev.session[0] is None:
        ev.prior_launch = any(folder.glob("occurrences.*.json"))
    ev.stops = {name: row for name in STOP_FILES if (row := read_record(folder / name))[0] is not None}
    ev.expected, ev.startup = dict(expected or {}), startup
    if run is None:
        return ev
    ev.run = Path(run)
    ev.launch = read_launch_record(ev.run / "runtime/farmhand-launch.json")
    ev.launch_end = launch_end(ev.run, ev.launch[1])
    ev.archive_proof = read_record(ev.run / "runtime/marker-archive-proof.json")
    if ev.session[0] is None:
        ev.prior_launch |= ev.launch[0] is not None
    cycles = cycle_dirs(run)
    ev.newest = cycles[-1] if cycles else None
    if ev.newest is not None:
        ev.newest_state = read_record(ev.newest / "cycle_state.json")
        from scripts.livepath.mabar_supervise import cycle_stop
        try:
            ev.cycle_end = cycle_stop(ev.newest, strict=True, state=ev.newest_state[1])
        except (OSError, ValueError):
            ev.outranking.append("cycle_events:unreadable")
        if expected is None:
            ev.expected = recovery_identity(ev.run, ev.newest, expected_identity(ev.newest_state[1]))
    operator_paths = [c / "OPERATOR_STOP" for c in cycles]
    markers = operator_paths
    if sentinels:
        markers = [ev.run / "LIVE7_STOP", ev.run / "runtime/LIVE7_STOP"] + [c / "LIVE7_STOP" for c in cycles] + markers
        state = ev.newest_state[1]
        if isinstance(state, Mapping) and isinstance(state.get("live7_stop_file"), str):
            markers.append(Path(state["live7_stop_file"]))
    ev.sentinels = {p: row for p in dict.fromkeys(markers) if (row := read_record(p))[0] is not None}
    if recovery:
        helper_reason, ev.host_inert = helper_status(run)
        ev.outranking.extend(stops_outranking(run, ev.newest_state, ev.cycle_end, helper_reason))
    return ev


def check(inbox, run=None, *, act, recovery=False, held=None, sentinels=True, expected=None, startup=None,
          allow_watchdog=False, **options):
    """`decide` on the evidence of this moment: the one call every entry point makes right before it acts."""
    ev = read_evidence(inbox, run, recovery=recovery, sentinels=sentinels, expected=expected, startup=startup)
    proof = watchdog_launch_proof(ev) if allow_watchdog and act == "launch" else None
    return decide(ev, act=act, held=held, watchdog=proof, **options)


def recovery_guard_config():
    raw = os.environ.get(RECOVERY_GUARD_ENV)
    if raw is None:
        return None
    try:
        config = json.loads(raw)
        if not isinstance(config, dict) or config.get("act") not in ("launch", "join"):
            raise ValueError("guard action")
        return config
    except ValueError as exc:
        raise HandsHeld(Decision("hold", "recovery_guard_unreadable")) from exc


def child_recovery_gate(config, *, saved_launch=None, window_fn=None, reservation=None, owner_launch=False):
    """Re-read launch and input authority inside the child that performs the effect."""
    if config is None:
        return None
    run = Path(config["run"])
    if time.time() >= config["deadline"]:
        raise RecoveryTimedOut("recovery_deadline")
    ev = read_evidence(config["inbox"], run, recovery=True, expected=config.get("expected"),
                       startup=config.get("startup"))
    if saved_launch is not None:
        pending_id, predecessor = saved_launch
        if not isinstance(ev.launch[1], dict) or ev.launch[1].get("launch_id") != pending_id:
            raise HandsHeld(Decision("hold", "farmhand_launch_changed"))
        ev.launch = predecessor
        if ev.session[0] is None:
            ev.prior_launch = predecessor[0] is not None or any(Path(config["inbox"]).glob("occurrences.*.json"))
    if (ev.newest.name if ev.newest is not None else None) != config.get("origin_cycle"):
        raise HandsHeld(Decision("hold", "stop_outranks_recovery:origin_cycle_changed"))
    if config.get("stop_file") and present(config["stop_file"]):
        raise HandsHeld(Decision("hold", "stop_outranks_recovery:cycle_marker"))
    proof = watchdog_launch_proof(ev, reservation=reservation) if config.get("allow_watchdog") else None
    decision = decide(ev, act=config["act"], held=config.get("held"), watchdog=proof, owner_launch=owner_launch)
    if decision.action == "hold":
        raise HandsHeld(decision)
    if decision.sentinels and not owner_launch:  # on the word, the run's own markers are archived after the join
        raise HandsHeld(Decision("hold", "recovery_waits_for_clear_end_markers", paths=decision.paths))
    if not owner_launch and session_kind(run) == "owner":
        raise HandsHeld(Decision("hold", "owner_join_waits_for_operator"))
    startup = config.get("startup")
    if startup is not None:
        _, launch = ev.launch
        if not isinstance(launch, dict) or any(launch.get(k) != startup.get(k) for k in ("launch_id", "pid")):
            raise HandsHeld(Decision("hold", "farmhand_launch_changed"))
        if window_fn is None:
            from scripts.m1_operator.win_facts import window as window_fn
        facts = window_fn(startup["hwnd"]) or {}
        if not facts.get("exists") or facts.get("pid") != startup.get("pid"):
            raise HandsHeld(Decision("hold", "farmhand_window_changed"))
    if (config.get("cabin") or decision.watchdog) and not host_save_matches(
            run, config.get("save_id"), config.get("host_hwnd"), window_fn=window_fn):
        raise HandsHeld(Decision("hold", "host_save_identity_unconfirmed"))
    if decision.watchdog and not host_save_matches(
            run, config.get("save_id"), config.get("host_hwnd"), window_fn=window_fn, accepting=True):
        raise HandsHeld(Decision("hold", "host_not_accepting"))  # never at cabin selection
    return decision


def stop_guards(run, *, stop_file=None):
    """Every stop marker the guarded watchdog path reads by its existence, as absolute paths: the run's own
    markers (`stops_outranking`, `helper_status`), each cycle folder's markers and the newest state's `live7_stop_file`
    (`read_evidence`), and the caller's cycle marker (`stop_check`). A watchdog re-arm token carries this list and the
    native stats each path when it consumes the token; the inbox stop files stay the native's own check."""
    run = Path(os.path.abspath(run))
    paths = [run / "host/HOST_STOP", run / "host/STOP", run / "SUPERVISE_STOP", run / "bridge-stop-request",
             run / "bridge-drained", run / "LIVE7_STOP", run / "runtime/LIVE7_STOP"]
    cycles = cycle_dirs(run)
    for cycle in cycles:
        paths += [cycle / "LIVE7_STOP", cycle / "OPERATOR_STOP"]
    if cycles:
        _, state = read_record(cycles[-1] / "cycle_state.json")
        if isinstance(state, Mapping) and isinstance(state.get("live7_stop_file"), str):
            paths.append(Path(state["live7_stop_file"]))
    if stop_file:
        paths.append(Path(stop_file))
    return list(dict.fromkeys(os.path.abspath(p) for p in paths))


def host_save_matches(run, save_id, hwnd, *, window_fn=None, now=None, accepting=False):
    """Use the enrolled helper's live, PID-bound Own() heartbeat before selecting a cabin. `accepting` (the
    watchdog token and the watchdog launch only) also needs the helper's fresh `accepting.ok` in the same barrier.json:
    a missing field holds, so an older helper build fails closed."""
    if not save_id or not hwnd:
        return False
    if window_fn is None:
        from scripts.m1_operator.win_facts import window as window_fn
    run = Path(run)
    prefix = run / "runtime/batch-host"
    _, launch = read_record(prefix / "host-launch.json")
    _, cfg = read_record(prefix / "host-mods/ArtiHostHelper/config.json")
    _, barrier = read_record(run / "host/barrier.json")
    _, status = read_record(run / "host/status.json")
    if not all(isinstance(row, dict) for row in (launch, cfg, barrier, status)):
        return False
    facts = window_fn(hwnd) or {}
    host = status.get("facts")
    if (not facts.get("exists") or type(launch.get("pid")) is not int or facts.get("pid") != launch["pid"]
            or str(cfg.get("SaveId")) != str(save_id) or not cfg.get("RunId")
            or not isinstance(cfg.get("OutputDirectory"), str)
            or _resolved(cfg["OutputDirectory"]) != _resolved(run / "host")
            or barrier.get("run_id") != cfg["RunId"] or status.get("run_id") != cfg["RunId"]
            or not barrier.get("runtime_id") or barrier.get("world_ready") is not True
            or barrier.get("stopped") is not False or barrier.get("saving") is not False or not isinstance(host, dict)
            or type(cfg.get("HostId")) is not int or host.get("host_id") != cfg["HostId"]):
        return False
    if accepting and (not isinstance(barrier.get("accepting"), dict) or barrier["accepting"].get("ok") is not True):
        return False
    try:
        observed = [datetime.fromisoformat(row["utc"]).timestamp() for row in (barrier, status)]
        launched = datetime.fromisoformat(launch["utc"]).timestamp()
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return False
    now = time.time() if now is None else now
    return all(launched <= sample and 0 <= now - sample <= 6 for sample in observed)


def helper_status(run):
    """The helper's latched stop and the provenance of its inert state, when published."""
    run = Path(run)
    raw, _ = read_record(run / "host/HOST_STOP")
    if raw is UNREADABLE:
        return UNREADABLE, None
    last = None
    if raw is not None:
        lines = raw.decode("utf-8-sig", errors="replace").strip().splitlines()
        reason = lines[0].strip() if lines else "HOST_STOP"
        return ("HOST_STOP:startup_pause_lost" if reason == "startup_pause_lost" else reason), None
    wait_raw, wait = read_record(run / "host/host_wait.json")
    if wait_raw is not None and not isinstance(wait, dict):
        return UNREADABLE, None
    inert = "unknown" if isinstance(wait, dict) and wait.get("state") == "inert" else None
    log_raw, _ = read_record(run / "host/host.jsonl")
    if log_raw is UNREADABLE:
        return UNREADABLE, inert
    for line in (log_raw or b"").decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("ev") == "host_inert":
            facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
            why = facts.get("why") or row.get("why")
            inert = why if isinstance(why, str) and why else "unknown"
        elif isinstance(row, dict) and row.get("ev") == "HOST_SAFETY_STOP":
            facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
            last = facts.get("reason") or row.get("reason")
    return last, inert


def stops_outranking(run, newest_state, cycle_end, helper):
    """STOP OUTRANKS RECOVERY: the stops a recovery must not pass, by name (review R6 B3). The file stops, the helper's
    `harness_stop` (also after host/STOP is gone), and the newest cycle ended by an operator or a safety stop."""
    run = Path(run)
    found = [name for name, path in (("SUPERVISE_STOP", run / "SUPERVISE_STOP"),
                                     ("bridge-stop-request", run / "bridge-stop-request"),
                                     ("bridge-drained", run / "bridge-drained"),
                                     ("host_STOP", run / "host" / "STOP")) if present(path)]
    if helper is UNREADABLE:
        found.append("HOST_STOP:unreadable")
    elif helper and helper != "startup_pause_lost":
        found.append(helper)
    raw, state = newest_state
    if raw is UNREADABLE or (raw is not None and not isinstance(state, Mapping)):
        found.append("cycle_state:unreadable")
    elif cycle_end[1] == "operator_stopped" or (isinstance(state, Mapping) and state.get("status") == "stopped"):
        found.append("cycle_stopped")
    elif (isinstance(state, Mapping) and (state.get("status") == "safety_stop" or cycle_end[0] == "safety_stop")
          and own_ending(state, cycle_end) != "self_heal"):
        found.append("cycle_safety_stop")  # a safety stop main's supervisor does not retry either
    return found


def claim_hold_alert(run, decision, *, fallback=False):
    """The first caller to see a native episode claims its operator alert."""
    episode = (decision.held or {}).get("episode_id")
    if run is None or not episode:
        return True
    key = sha256(episode.encode("utf-8"))
    path = Path(run) / "runtime" / "halt-alerts" / (key + ".json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return ipc.create_json_once(path, {"episode_id": episode}, fsync=True)
    except OSError:
        return fallback  # only the cycle emits without a shared claim


def halt_hold(decision):
    """A hold on a native lock, or on lock evidence that cannot be read: it ends on a person's re-arm or on a fresh read."""
    return bool(decision.halt or decision.held) or decision.reason.startswith(
        ("halt_", "native_stop_", "operator_rearm_not_seen", "persisted_halt", "lock_would_be_lost", "restoration_",
         "session_unreadable"))


def operator_stop(decision):
    """A stop outside the native HALT wait that ends an automatic recovery."""
    prefix = "stop_outranks_recovery:"
    return (decision.reason.startswith(prefix)
            and any(not reason.endswith(":unreadable") for reason in decision.reason[len(prefix):].split(",")))


def operator_command(inbox, paths=()):
    """What the operator runs: the recorded tool with its typed confirmation, naming every stop marker it shows."""
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-B", str(root / "tools/arti_rearm.py"), "--folder", str(inbox), "--actor", "owner"]
    for path in paths:
        command += ["--live7-stop", str(path)]
    return command


def proof_gap(decision, before, after, sentinels_gone, outcome):
    """Why the tool's run does not prove `decision`, or None. A re-arm: the tool's own `rearmed`, a complete clear in
    session.json for the same session and runtime, the mod's receipt naming the bound episode and seq (absent ids
    never match absent ids). A sentinel archive: the tool's own `sentinels_archived`, every bound sentinel gone and
    session.json still clear for the same session and runtime (the mod writes nothing for a sentinel)."""
    if not isinstance(before, Mapping) or not all(identity(before)):
        return "no_session_before_rearm"
    if not isinstance(after, Mapping) or not _complete(after):
        return "session_unreadable"
    if identity(after) != identity(before):
        return "foreign_session"
    if after["halt"] is not None or after["stop_latched"] is not False or after["record_faulted"] is not False:
        return "halt_not_cleared"
    if decision.action == "archive_sentinels":
        if outcome != "sentinels_archived":
            return "tool:" + str(outcome)
        return None if sentinels_gone else "sentinel_not_archived"
    if outcome != "rearmed":
        return "tool:" + str(outcome)
    if not decision.expect or not receipt_for(after, dict(episode_id=decision.expect[0], halt_seq=decision.expect[1])):
        return "rearm_not_correlated"
    return None


def sha256(raw):
    return hashlib.sha256(raw).hexdigest() if isinstance(raw, bytes) else None
