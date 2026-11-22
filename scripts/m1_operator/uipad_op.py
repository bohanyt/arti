"""One known-menu cleanup through native ui_press B; no window input or port reader.

FIXB43 replacement for the evidence-only v2b27 helper. Pins are captured AFTER
lease acquisition. Never retry an input, change receiver, or close with an item held.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import arti_stardew_command as command  # noqa: E402
import arti_stardew_ui_pad as ui_pad  # noqa: E402


def _inspect(io, folder, session):
    result = io.minta_inspect_context(folder=folder, sesi=session)
    context = (result.mentah or {}).get("context") if result.sukses else None
    if not isinstance(context, dict):
        raise ValueError("inspect_failed")
    return context


def _identity(session, context):
    fields = ("session_id", "player_id", "player_name", "screen_id", "location_id")
    if any(context.get(k) != getattr(session, k) for k in fields if k != "player_name"):
        raise ValueError("inspect_identity_changed")
    runtime = context.get("runtime_instance_id")
    generation = context.get("context_generation")
    if not isinstance(runtime, str) or not runtime or type(generation) is not int or generation < 1:
        raise ValueError("inspect_binding_missing")
    return tuple(getattr(session, k) for k in fields) + (runtime, generation)


def _known_menu(context, menu, receiver):
    block = context.get("ui_pad")
    interactive = context.get("interactive")
    if (not isinstance(block, dict) or not isinstance(interactive, dict)
            or any(b.get("menu_type") != menu or b.get("receiver_token") != receiver
                   for b in (block, interactive))):
        raise ValueError("known_menu_changed")
    if block.get("held_item") is not None:
        raise ValueError("held_item_cleanup_refused")
    return block


def _lease_admission(session, context, identity, owner, generation, clock):
    """Validate a newly read native session; never adopt a replacement lease."""
    raw = session.mentah
    # CommandInboxSession publishes runtime/actor/lease, not context_generation.
    # That mandatory pin comes from inspect_context; native input admission
    # checks it again. If a session publishes it, reject an invalid or changed pin.
    # The shared session parser defaults an omitted screen_id to 0. Admission
    # requires the native field itself to be an integer matching the inspected pin.
    if (type(raw.get("screen_id")) is not int or raw["screen_id"] != context["screen_id"]
            or _identity(session, context) != identity
            or raw.get("runtime_instance_id") != context["runtime_instance_id"]
            or ("context_generation" in raw
                and (type(raw["context_generation"]) is not int
                     or raw["context_generation"] != context["context_generation"]))):
        raise ValueError("lease_binding_changed")
    if (raw.get("owner_id") != owner or type(raw.get("generation")) is not int
            or raw["generation"] != generation
            or type(raw.get("control_seq")) is not int or raw["control_seq"] != 1):
        raise ValueError("lease_owner_or_sequence_changed")
    reason = session.alasan_belum_siap(command.OP_INPUT_ONCE, primitive=command.PRIMITIVE_UI_PRESS)
    if reason:
        raise ValueError("session_not_ready:" + reason)
    # UI-pad B is a guarded native menu call, not a retained background Press.
    # Native UiPadRoute.Ready does not require mode ownership, and ApplyAcquire
    # does not take it. Admit a published ready menu under an intact lease with
    # either owned mode or a fully restored Auto/ForceOff mode; no stray ForceOn.
    if (command.lease_state(raw).blocked
            # Native ApplyAcquire leaves the previous release acknowledgement intact.
            # It is not an armed-lease predicate; owner/generation/seq/expiry above are.
            or type(raw.get("release_acked")) is not bool or type(raw.get("pad_mode_owned")) is not bool
            or (not any(k in raw for k in ("input_idle", "halt", "status_answers", "fenced_generation"))
                and raw.get("v") != "arti.session/3" and raw["pad_mode_owned"] is False
                and command.input_release_state(raw) is not True)  # legacy_compat: unproven denies
            or raw.get("pad_mode_restore_pending") is not False
            or type(raw.get("pad_outstanding_count")) is not int or raw["pad_outstanding_count"] != 0):
        raise ValueError("lease_not_ready")
    expiry = raw.get("lease_until_unix_ms")
    if type(expiry) is not int:
        raise ValueError("lease_expired_or_missing")
    remaining_ms = expiry - int(clock.time() * 1000)
    if remaining_ms <= 0:
        raise ValueError("lease_expired_or_missing")
    return min(1.0, remaining_ms / 1000.0)


def close_once(*, folder, menu, receiver, yes=False, io=command, clock=time):
    """Acquire -> inspect same actor/menu -> one B -> release and verify, all bounded.

    A rejected/uncertain result is returned unchanged, with no fallback input.
    Failures before submission raise ValueError after releasing any acquired lease.
    """
    session = io.baca_sesi(folder, retry_detik=0.25)
    context = _inspect(io, folder, session)
    identity = _identity(session, context)
    initial = _known_menu(context, menu, receiver)
    if not yes:
        return {"dry_run": True, "menu_type": menu, "receiver_token": receiver}
    reason = session.alasan_belum_siap(command.OP_INPUT_ONCE, primitive=command.PRIMITIVE_UI_PRESS)
    if reason:
        raise ValueError("session_not_ready:" + reason)
    owner = "uipad-close-" + uuid.uuid4().hex[:12]
    generation = int(clock.time() * 1000)
    io.tulis_kontrol(command.KIND_ACQUIRE, folder=folder, sesi=session, owner_id=owner,
                     generation=generation, control_seq=1, expected_screen_id=session.screen_id)
    result = None
    try:
        deadline = clock.monotonic() + 1.0
        while clock.monotonic() < deadline:
            current = io.baca_sesi(folder, retry_detik=0.25)
            if current.mentah.get("owner_id") == owner and current.mentah.get("generation") == generation:
                break
            clock.sleep(0.02)
        else:
            raise ValueError("acquire_not_confirmed")
        # A menu may snap after acquisition. Use its new token and id,
        # but never adopt a different actor/session/location/runtime or receiver.
        fresh = _inspect(io, folder, current)
        if _identity(current, fresh) != identity:
            raise ValueError("lease_binding_changed")
        block = _known_menu(fresh, menu, receiver)
        if block.get("context") != initial.get("context"):
            raise ValueError("known_menu_context_changed")
        view = ui_pad.baca_ui_pad(fresh, age_s=0.0)
        if view is None or not view.available or not view.press_allowed("B"):
            raise ValueError("close_not_published")
        # Inspection can outlast the native lease. Read again before calling the
        # command API, then again at its guarded write boundary (after setup).
        current = io.baca_sesi(folder, retry_detik=0.25)
        admission = _lease_admission(current, fresh, identity, owner, generation, clock)
        expiry = current.mentah["lease_until_unix_ms"]

        def guarded_publish(publish):
            latest = io.baca_sesi(folder, retry_detik=0.25)
            _lease_admission(latest, fresh, identity, owner, generation, clock)
            if latest.mentah["lease_until_unix_ms"] < expiry:
                raise ValueError("lease_expiry_shortened")
            publish()

        result = io.minta_ui_press(**view.press_args("B"), folder=folder, sesi=current,
                                  owner_id=owner, generation=generation, control_seq=1,
                                  context_generation=fresh["context_generation"],
                                  runtime_instance_id=fresh["runtime_instance_id"],
                                  expected_location_id=current.location_id, expected_screen_id=current.screen_id,
                                  admisi_detik=admission, deadline_cap_unix_ms=expiry,
                                  batas_detik=4.5, guarded_publish=guarded_publish)
    finally:
        current = io.baca_sesi(folder, retry_detik=0.25)
        # Release ONLY our lease using the original session identity, even if a
        # new session appeared during the input. Native stale-session refusal is safe.
        seq = current.mentah.get("control_seq") if current.mentah.get("owner_id") == owner else 1
        io.tulis_kontrol(command.KIND_RELEASE, folder=folder, sesi=session, owner_id=owner,
                         generation=generation, control_seq=(seq if type(seq) is int else 1) + 1,
                         expected_screen_id=session.screen_id)
        deadline = clock.monotonic() + 1.0
        while clock.monotonic() < deadline:
            released = io.baca_sesi(folder, retry_detik=0.25)
            if (command.lease_state(released.mentah).bound(command.lease_state(session.mentah))
                    and command.lease_state(released.mentah).retired(owner, generation, ((seq if type(seq) is int else 1) + 1,))
                    and released.inflight_request_id is None
                    and command.input_release_state(released.mentah, request_id=getattr(result, "request_id", None),
                        result=getattr(result, "mentah", None)) is True):
                break
            clock.sleep(0.02)
        else:
            raise ValueError("release_not_confirmed")
    raw = result.mentah or {}
    closed = (result.status == command.SELESAI and result.sukses
              and result.outcome == command.OUTCOME_SUKSES_PRIMITIVE[command.PRIMITIVE_UI_PRESS]
              and raw.get("ui_key_effect") == "closed" and raw.get("cleanup_confirmed") is True
              and raw.get("input_settled") is True and raw.get("result_receiver_token") is None
              and raw.get("result_menu_type") is None)
    return {"status": result.status, "success": result.sukses, "reason": result.alasan,
            "request_id": result.request_id, "closed": bool(closed), "release_verified": True,
            "result": {k: raw.get(k) for k in ("outcome", "ui_key_effect", "result_receiver_token",
                                               "result_menu_type", "input_settled")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("op", choices=("close",))
    parser.add_argument("--folder", required=True, help="exact native command inbox directory")
    parser.add_argument("--expect-menu", required=True)
    parser.add_argument("--expect-receiver", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = close_once(folder=args.folder, menu=args.expect_menu, receiver=args.expect_receiver, yes=args.yes)
    except (ValueError, OSError, command.KesalahanPerintah) as exc:
        print(json.dumps({"label": args.label, "error": str(exc)}))
        return 1
    print(json.dumps(dict(report, label=args.label), ensure_ascii=False))
    return 0 if report.get("closed") and report.get("release_verified") else 2 if report.get("dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
