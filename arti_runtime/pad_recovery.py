"""ADR 192: a pad-watch incident fences publications until native neutral + re-observation.

The watcher owns force-release/readback. The game runtime owns cancellation and
the action outcome. Neither side may clear the other's half of the fence.
"""
from collections.abc import Mapping
from pathlib import Path
import time

from arti_runtime import ipc

FILE = "pad_recovery.json"
BOUND_S = 3.0  # 4 Hz panic poll + R/N frames + mode restore, with >2 s scheduling margin
CAUSE = "pad_hiccup_recovered"


def path(config):
    cycle = config.get("stardew_live7_cycle_id")
    return Path(cycle) / FILE if cycle else None


def read(file):
    if file is None:
        return None
    try:
        row = ipc.read_json(file)
        return row if isinstance(row, Mapping) else {"status": "unknown"}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"status": "unknown"}  # an unreadable fence never admits input


def blocked(config):
    row = read(path(config))
    return bool(row and row.get("status") != "consumed")


def neutral(raw, binding, request_id):
    """Native current pad facts AND correlated input-client release; never command success alone."""
    from arti_stardew_command import input_release_state
    return (isinstance(raw, Mapping)
            and all(binding.get(k) is not None and raw.get(k) == binding[k]
                    for k in ("runtime_instance_id", "session_id", "player_id", "screen_id"))
            and type(raw.get("pad_outstanding_count")) is int and raw["pad_outstanding_count"] == 0
            and raw.get("input_busy") is False and raw.get("inflight_request_id") is None
            and raw.get("pad_mode_owned") is False and raw.get("pad_mode_restore_pending") is False
            and raw.get("pad_mode_cleanup_fault") == ""
            and input_release_state(_plain(raw), request_id=request_id) is True)


def _plain(value):
    # Observation stores freeze mappings recursively; the command receipt parser consumes JSON shapes.
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def boundary(runtime):
    """Called by the existing 20 ms STOP hook and every skill boundary; nonblocking."""
    file = path(runtime._config)
    row = read(file)
    if not row or row.get("status") == "consumed":
        return False
    session = runtime._skill_session
    if getattr(session, "_pad_recovery_id", None) != row.get("id", "unknown"):
        session._pad_recovery_id = row.get("id", "unknown")
        session._pad_interrupted = session.current
        runtime._cancel_action("pad_watch_recovery")
    if row.get("status") == "failed":
        runtime._skill_end_locked(session, "held", "SAFETY_STOP",
            "pad_watch_neutral_unverified:" + str(row.get("held")))
        return True
    if row.get("status") != "verified" or runtime._action_busy or runtime._inflight_generation is not None:
        return True
    # Verification by the watcher alone cannot bypass her fresh scene/bag fence.
    p = session.campaign.live
    if getattr(session, "_pad_release_id", None) != row["id"]:
        p.release_after = time.monotonic()
        session._pad_release_id = row["id"]
    facts, motor, context, held = runtime._live7_read(session)
    if held or facts is None or runtime.skill_post_row_state().get("input_released") is not True:
        return True
    interrupted = session._pad_interrupted
    cut_short = (interrupted is not None and interrupted is session.current
                 and not (interrupted.pending and interrupted.pending[0] == "done"))
    if cut_short:
        interrupted.pending = ("fail", CAUSE)
        interrupted.state["live7_recovery_requested"] = True
        session._pad_recovery_fact = "Tindakan terputus karena tombol masih tertekan. Tombol sudah dilepas; keadaan dibaca ulang."
    if getattr(session, "_pad_recovery_recorded", None) != row["id"]:
        runtime._journal(dict(ev="FAULT", at=time.time(), type=CAUSE, cause="pad_held_after_terminal",
            disposition="RECOVER", outcome_class="UNCERTAIN", verified=False, replay=False,
            observed=dict(held=row.get("held"), held_seconds=row.get("held_seconds"),
                          seconds_lost=round(max(row.get("seconds_lost", 0), time.time() - row.get("at", time.time())), 3),
                          released_by="pad_panic", action_cut_short=cut_short),
            refs=dict(incident_id=row["id"], request_id=row.get("request_id"))))
        session._pad_recovery_recorded = row["id"]
    # Only after re-observation may any acquire, renewal or input be published.
    ipc.write_json(file, dict(row, status="consumed"))
    return False


def recover(file, src, session, violation):
    """Fence first, existing native pad-panic path, current release proof, bounded STOP on failure."""
    import uuid
    file = Path(file)
    clock = getattr(src, "monotonic", src.now)
    started = clock()
    armed = violation.get("held") or {}
    request_id = (session or {}).get("inflight_request_id") or armed.get("request_id")
    incident = dict(id=uuid.uuid4().hex, status="pending", at=src.now(),
                    held=armed or dict(outstanding=violation["outstanding"], key="unknown"),
                    held_seconds=violation["seconds"], request_id=request_id)
    ipc.write_json(file, incident)  # publication fence is visible BEFORE force-release/detection log
    last, requested = None, False
    while clock() - started < BOUND_S:
        try:
            if not requested:
                src.force_neutral(session)
                requested = True
            last = src.session()
            if src.neutral_request_consumed() and neutral(last, session or {}, request_id):
                incident["status"] = "verified"
                break
        except (OSError, ValueError, TypeError, AttributeError):
            # A transient write/read failure, or no readable native session to address the release to,
            # spends the same small bound and ends as a named failure, never as a crashed watcher.
            pass
        src.sleep(.02)
    if incident["status"] != "verified":
        incident.update(status="failed", remaining=dict(outstanding=(last or {}).get("pad_outstanding_count"),
                        input_busy=(last or {}).get("input_busy"), held=incident["held"]))
    incident.update(seconds_lost=round(clock() - started, 3), released_by="pad_panic")
    ipc.write_json(file, incident)
    return incident


class RuntimeSource:
    """The explicit monitor hook uses the same inbox panic and native receipt verifier as the watcher."""
    now = staticmethod(time.time)
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def __init__(self, runtime):
        self.commands = runtime._commands
        self.folder = self.commands.folder_perintah(runtime._config.get("stardew_command_dir"))

    def session(self):
        try:
            return self.commands.baca_sesi(self.folder).mentah
        except self.commands.KesalahanPerintah:
            return None

    def force_neutral(self, session):
        from datetime import datetime, timezone
        import uuid
        ipc.write_json(self.folder / "pad_panic.json", dict(protocol=1, op="pad_panic", request_id=uuid.uuid4().hex,
            runtime_instance_id=session.get("runtime_instance_id"), session_id=session.get("session_id"),
            utc=datetime.now(timezone.utc).isoformat(), source="pad_watch_recovery"))

    def neutral_request_consumed(self):
        return not (self.folder / "pad_panic.json").exists()


