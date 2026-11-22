"""fix144A (ADR 249): her hands held by a latched native HALT, read from the mod's own session.json.

Bench check 19 (2026-11-19, festival inside): the farmhand started with a persisted HALT (``halt_seq`` 14, cause
``native_unclean_exit``, ``halt_notes: ["halt_record_missing"]``, ``stop_latched: true``) latched 107 s before the
cycle began. Every input was refused at its precheck (``session_precondition:stop_latched``), the release of her
input could never be proven (``arti_stardew_command.input_release_state`` needs ``halt is None``), and the recovery
wait held a failed run for 170 s with no page. The mod's session.json is the truth of whether her hands are held;
``inbox/halt.json`` can be missing while the HALT is latched (``halt_record_missing``).

REPORTING ONLY. Who may clear a HALT is a safety decision that stays with the operator and the recorded tool
(``tools/arti_rearm.py`` with its typed confirmation); its policy is ADR 254 (fix148A), not this module. Here the
program says that her hands are held, why, and what would clear them (her page, the ladder, one alert per native
episode); her voice goes on. Nothing here clears, re-arms or relaunches anything.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import os

KEY = "tangan_ditahan"  # her page's fact line while a HALT holds her hands
CLEARS = "operator_rearm"  # what clears it: the operator's recorded re-arm tool (tools/arti_rearm.py, ADR 254)
# Precheck refusals no retry of the same step can cure (arti_stardew_command.admission_state): a latched HALT, a
# faulted native record, a session that is not a farmhand's single screen or whose inbox is off. A busy slot, a
# requested release or a world that is not ready (a warp) pass by themselves and keep the bounded refresh.
SESSION_LEVEL = frozenset({"stop_latched", "record_faulted", "is_main_player", "is_split_screen", "inbox_enabled"})
# C7: a recovery wait for a release proof, with nothing of hers held by native evidence, no menu and her player free,
# ends its run after this long (seconds of the wait). ADR 237/241's 60 s: rung 1's bound and the longest healthy
# stretch without an effect (62.9 s, 21:38 run, 10 days); a proof that has not come in that time is not coming by
# waiting. A latched HALT ends it at once: no proof can come while it holds.
UNPROVEN_END_S = 60.0
QUIET_POLL_GAP_S = 5.0  # allow slow reads on the worker's 0.2 s poll without counting a missing stretch


def session_level(code) -> str | None:
    """The admission reason of a ``session_precondition:<reason>`` precheck code when no retry can cure it."""
    text = str(code or "")
    if not text.startswith("session_precondition:"):
        return None
    reason = text.split(":", 1)[1]
    return reason if reason in SESSION_LEVEL else None


def of(raw) -> dict | None:
    """The latched HALT in a session.json mapping, or None. Only explicit evidence counts (``stop_latched: true`` or
    a ``halt`` object); a reading without either field is not a HALT here (legacy shapes stay with their own fences)."""
    if not isinstance(raw, Mapping):
        return None
    halt = raw.get("halt")
    if raw.get("stop_latched") is not True and not isinstance(halt, Mapping):
        return None
    halt = halt if isinstance(halt, Mapping) else {}
    entries = [e for e in (halt.get("entries") or ()) if isinstance(e, Mapping)]
    cause = next((str(e["cause"]) for e in entries if e.get("cause")), None)
    notes = raw.get("halt_notes")
    rearmed = raw.get("rearmed") if isinstance(raw.get("rearmed"), Mapping) else {}
    return dict(seq=halt.get("halt_seq"), cause=cause or "stop_latched", scope=halt.get("scope"),
                episode=halt.get("episode_id"), since_ms=halt.get("since_ms"), persisted=halt.get("persisted"),
                notes=[str(n) for n in notes] if isinstance(notes, list) else [],
                stop_latched=raw.get("stop_latched") is True, rearmed_seq=rearmed.get("halt_seq"))


def episode_key(halt) -> str:
    """One native HALT episode: the mod's episode id, whatever skill session, runner or entry sees it (an entry added
    to the same episode is the same incident, review R1b SF3); the seq only when no episode id was published."""
    halt = halt or {}
    return str(halt.get("episode") or "seq:%s" % halt.get("seq"))


@contextmanager
def episode_lock(path):
    """The OS releases this lock even if the runner exits during delivery."""
    with path.with_suffix(".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def session_raw(runtime) -> dict | None:
    """The runtime's bound command session.json as read now, or None (no configured inbox, an unreadable session)."""
    config = getattr(runtime, "_config", None) or {}
    where = config.get("stardew_command_dir")
    commands = getattr(runtime, "_commands", None)
    if not where or commands is None:
        return None
    try:
        raw = commands.baca_sesi(commands.folder_perintah(where), retry_detik=0.25).mentah
    except Exception:  # noqa: BLE001 - an unread session is no claim; the precheck still refuses
        return None
    return raw if isinstance(raw, Mapping) else None


def read(runtime) -> dict | None:
    """The HALT of the runtime's bound command session, or None."""
    return of(session_raw(runtime))


def quiet(raw) -> bool:
    """The mod's own evidence that no input of hers is held or in flight: not busy, no request in flight, no lease
    owner, no outstanding pad press. Missing evidence is not quiet."""
    return (isinstance(raw, Mapping) and raw.get("input_busy") is False and "inflight_request_id" in raw
            and raw["inflight_request_id"] is None and "owner_id" in raw and raw["owner_id"] is None
            and type(raw.get("pad_outstanding_count")) is int and raw["pad_outstanding_count"] == 0)


def why(halt) -> str:
    """The ladder's and the run's cause: ``halt_latched:<cause>``."""
    return "halt_latched:" + str((halt or {}).get("cause") or "stop_latched")


def label(halt) -> str:
    """seq, cause and notes in one short operator phrase."""
    halt = halt or {}
    notes = ", ".join(halt.get("notes") or ())
    return "HALT native %s (%s%s)" % (halt.get("seq"), halt.get("cause") or "stop_latched",
                                      "; " + notes if notes else "")


def page_line(halt) -> str | None:
    """Her page's one line while the HALT holds: what holds her, why, what clears it; nothing to choose."""
    if not halt:
        return None
    return ("Tanganku sedang ditahan game: %s. Selama ini setiap tombol ditolak sebelum sampai ke game, jadi pilihan "
            "yang butuh tangan akan langsung gagal dengan alasan itu. Yang bisa membukanya hanya operator lewat alat "
            "rearm yang tercatat; suaraku tetap jalan." % label(halt))


def ended_line(skill, arg, seconds, waited, halt=None) -> str:
    """C7: her next page says, once, which skill ended, after how long with no change, and what it waited for."""
    what = ("tanganku ditahan game (%s)" % label(halt)) if halt else ("bukti dari game bahwa tombolnya sudah "
                                                                      "dilepas")
    name = skill + (" " + str(arg) if arg not in (None, "") else "")
    return ("%s kuhentikan: %d detik tidak ada yang berubah di game, dan yang ditunggunya (%s) tidak datang. Tanganku "
            "sendiri tetap ada di halaman ini." % (name, int(seconds), what))
