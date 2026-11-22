"""tools/arti_rearm: the only re-armer, operator-run (R0-03 §9.1 R0-R6, HB-H8; R0-07 §7.5). WP P2.

Never deletes a stop file: it archives only files whose exact bytes match an entry the operator confirmed, via
`archive_stop` with `reason: rearm:<episode_id>:<halt_seq>` (the core's `archived` discharge evidence, R0-03 §2).
ADR-19 item 1 (WP P1): the legacy supervisor's `RUN/LIVE7_STOP` and `<cycle>/LIVE7_STOP` sentinels (`--live7-stop`)
go through the same flow: shown at R2, bytes re-checked at R3, archived at R5 with the latch's reason, or with
`rearm:legacy-live7:<file>` when there is no HALT record (the native mod never latches a sentinel).
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[1]))
from arti_runtime import ipc
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for `python tools/arti_rearm.py`
from arti_stardew_stop import archive_stop, StopRefused  # noqa: E402
from arti_stardew_command import lease_state  # noqa: E402

STOP_FILES = ("stop.json", "stop_all.json")
REARM_WAIT_S = 5.0  # R0-03 §9.1 R6, §13 item 11
POLL_S = 0.1


def _read(path):
    try:
        return ipc.read_bytes(Path(path))
    except FileNotFoundError:
        return None


def _key(halt):
    return None if halt is None else (halt["episode_id"], halt["halt_seq"], halt["entries_digest"])


def read_session(folder):
    """Fail closed (R0-03 §1 (d)): an unreadable session or a malformed `halt` refuses the run."""
    raw = _read(Path(folder) / "session.json")
    try:
        session = json.loads(raw.decode("utf-8-sig"))
    except (AttributeError, ValueError, UnicodeError) as exc:
        raise StopRefused(f"session.json missing or unreadable: {exc}") from exc
    if not isinstance(session, dict) or not lease_state(session).halt_valid:
        raise StopRefused("session.json or session.halt malformed")
    return session


def archive_stale_token(folder, halt):
    """R0: archive (audited) any `rearm.json` that does not match the current latch."""
    path = Path(folder) / "rearm.json"
    raw = _read(path)
    if raw is None:
        return None
    try:
        token = json.loads(raw.decode("utf-8-sig"))
        key = (token["episode_id"], token["halt_seq"], token["entries_digest"])
    except (ValueError, UnicodeError, TypeError, KeyError):
        key = None
    if halt is not None and key == _key(halt):
        return None
    try:
        return archive_stop(path, raw=raw, reason="rearm_token_stale", current={}, evidence={"latch": _key(halt)})
    except FileNotFoundError:
        return None  # the mod deleted it meanwhile (R0-03 §9.2 step 4)


def write_token(folder, token):
    """R4: atomic + fsync."""
    path = Path(folder) / "rearm.json"
    ipc.write_json(path, token, fsync=True)


def show(halt, notes, out):
    out(f"HALT episode {halt['episode_id']} halt_seq {halt['halt_seq']} scope {halt.get('scope')} "
        f"since_ms {halt.get('since_ms')}")
    for e in halt["entries"]:
        out(f"  #{e.get('n')} {e.get('source')} stop_id={e.get('stop_id')} cause={e.get('cause')} scope={e.get('scope')} "
            f"writer={e.get('writer')} file_present={e.get('file_present')} sha256={e.get('stop_sha256')}")
    out(f"  halt_notes: {notes}")


def show_sentinels(shown, out):
    for path, raw in shown.items():
        try:
            rec = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            rec = None
        fields = {k: rec.get(k) for k in ("session_id", "runtime_instance_id", "cycle_id", "writer", "created_at")} \
            if isinstance(rec, dict) else "unparseable"
        out(f"  LIVE7_STOP {path} sha256={hashlib.sha256(raw).hexdigest()} {fields}")


def _archive_sentinels(shown, reason, evidence):
    for path, raw in shown.items():
        archive_stop(path, raw=raw, reason=reason(path), current={}, evidence=dict(evidence, sentinel=str(path)))


def archive_retry_marker(path, raw, *, reason, current):
    """Move a retry marker whose authority and exact bytes the caller just verified."""
    return archive_stop(path, raw=raw, reason=reason, current=current,
                        evidence={"actor": "rig_operator", "sentinel": str(path)})


def archive_owned_record(path, value, *, reason):
    """Withdraw only the exact record this caller published, keeping the normal audit."""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if _read(path) != raw:
        return False
    try:
        archive_stop(path, raw=raw, reason=reason, current={}, evidence={"actor": "rig_operator"})
    except FileNotFoundError:
        return False
    return True


def _watchdog_rearm(folder, token, check_stops, sleep, wait_s):
    path = folder / "rearm.json"
    check_stops()
    if not ipc.create_json_once(path, token, fsync=True):
        return "declined"
    proved = False
    try:
        deadline = time.monotonic() + wait_s
        while True:
            check_stops()
            if time.monotonic() >= deadline:
                return "timeout"
            now = lease_state(read_session(folder))
            check_stops()
            receipt = now.rearmed or {}
            if (not now.halted and (receipt.get("episode_id"), receipt.get("halt_seq"))
                    == (token["episode_id"], token["halt_seq"])):
                proved = True
                return "rearmed"
            if "rearm_token_stale" in now.halt_notes:
                return "declined"
            sleep(POLL_S)
    finally:
        if not proved:
            archive_owned_record(path, token, reason="watchdog_rearm_withdrawn")


def rearm(folder, actor, confirm, *, sentinels=(), out=print, sleep=time.sleep, wait_s=REARM_WAIT_S, automatic=False,
          watchdog_stop_check=None):
    """Return rearmed | sentinels_archived | nothing_to_rearm | unconfirmed_stop_file | declined | timeout.

    `confirm(halt, notes) -> bool`; `halt` is None when only LIVE7_STOP sentinels are shown (ADR-19 item 1)."""
    if actor not in ("owner", "rig_operator"):
        raise ValueError(actor)
    folder, sentinels = Path(folder), [Path(p) for p in sentinels]
    while True:
        session = read_session(folder)  # R1
        state = lease_state(session)
        halt, notes = state.halt, list(state.halt_notes)
        if not automatic:
            archive_stale_token(folder, halt)  # R0
        raws = {n: raw for n in STOP_FILES if (raw := _read(folder / n)) is not None}
        shown = {p: raw for p in sentinels if (raw := _read(p)) is not None}
        if halt is None:
            if raws:
                out(f"unconfirmed_stop_file: {sorted(raws)} not latched yet; nothing written")
                return "unconfirmed_stop_file"
            if not shown:
                out("nothing to re-arm")
                return "nothing_to_rearm"
            out("no HALT record; legacy LIVE7_STOP sentinels:")
            show_sentinels(shown, out)  # R2
            if not confirm(None, notes):
                return "declined"
            changed = sorted(str(p) for p, raw in shown.items() if _read(p) != raw)  # R3
            if changed:
                out(f"unconfirmed_stop_file: {changed} changed after confirmation; nothing written, back to R1")
                sleep(POLL_S)
                continue
            _archive_sentinels(shown, lambda p: f"rearm:legacy-live7:{p}", {"actor": actor})  # R5
            out(f"archived {len(shown)} LIVE7_STOP sentinel(s)")
            return "sentinels_archived"
        show(halt, notes, out)  # R2
        show_sentinels(shown, out)
        if not confirm(halt, notes):
            return "declined"
        confirmed = {e.get("stop_sha256") for e in halt["entries"]}  # R3: bytes read after the confirmation
        raws = {n: raw for n in STOP_FILES if (raw := _read(folder / n)) is not None}
        bad = sorted(n for n, raw in raws.items() if hashlib.sha256(raw).hexdigest() not in confirmed)
        bad += sorted(str(p) for p, raw in shown.items() if _read(p) != raw)
        if bad:
            out(f"unconfirmed_stop_file: {bad}; nothing written, back to R1")
            sleep(POLL_S)
            continue
        episode_id, halt_seq, digest = _key(halt)
        token = {"v": "arti.rearm/1", "episode_id": episode_id, "halt_seq": halt_seq,
                 "entries_digest": digest, "actor": actor, "ts": int(time.time() * 1000)}
        if watchdog_stop_check is not None:
            if not automatic or raws or shown or not confirm(halt, notes):
                return "declined"
            return _watchdog_rearm(folder, token, watchdog_stop_check, sleep, wait_s)
        if automatic:
            if not confirm(halt, notes) or not ipc.create_json_once(folder / "rearm.json", token, fsync=True):
                return "declined"
        else:
            write_token(folder, token)  # R4
        reason = f"rearm:{episode_id}:{halt_seq}"
        for name, raw in raws.items():  # R5
            archive_stop(folder / name, raw=raw, reason=reason, current={},
                         evidence={"episode_id": episode_id, "halt_seq": halt_seq, "entries_digest": digest, "actor": actor})
        _archive_sentinels(shown, lambda _p: reason, {"episode_id": episode_id, "halt_seq": halt_seq, "actor": actor})
        deadline = time.monotonic() + wait_s  # R6
        while time.monotonic() < deadline:
            sleep(POLL_S)
            now = read_session(folder)
            state = lease_state(now)
            rearmed = state.rearmed or {}
            if (not state.halted and not any((folder / n).exists() for n in STOP_FILES)
                    and (rearmed.get("episode_id"), rearmed.get("halt_seq")) == (episode_id, halt_seq)):
                out(f"re-armed {reason}")
                return "rearmed"
            if "rearm_token_stale" in state.halt_notes:
                out("rearm_token_stale: the latch grew; back to R1")
                break
        else:
            out(f"timeout: rearm.json stays valid for exactly {reason}")
            return "timeout"


def main(argv=None):
    p = argparse.ArgumentParser(description="Re-arm a latched HALT (operator-run only; R0-03 §9.1).")
    p.add_argument("--folder", required=True, help="mod command folder (next to session.json)")
    p.add_argument("--actor", required=True, choices=("owner", "rig_operator"))
    p.add_argument("--live7-stop", action="append", default=[], type=Path,
                   help="legacy supervisor sentinel (RUN/LIVE7_STOP, <cycle>/LIVE7_STOP); repeatable (ADR-19 item 1)")
    a = p.parse_args(argv)

    def ask(halt, _notes):
        what = (f"Re-arm episode {halt['episode_id']} halt_seq {halt['halt_seq']}" if halt is not None
                else "Archive the LIVE7_STOP sentinel(s) shown above")
        return input(f"{what}? type 'rearm': ").strip() == "rearm"

    try:
        outcome = rearm(a.folder, a.actor, ask, sentinels=a.live7_stop)
    except StopRefused as exc:
        print(f"refused: {exc}")
        return 2
    return 0 if outcome in ("rearmed", "sentinels_archived", "nothing_to_rearm") else 1


if __name__ == "__main__":
    sys.exit(main())
