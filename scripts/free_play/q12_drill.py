"""Q12 controlled-enrollment drill; no OS input, D8/D9 call, or autonomous day runner."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

import arti_stardew_command as command
import arti_stardew_ui_pad as ui_pad
from scripts.m1_operator import uipad_op, win_facts


def inspect(folder):
    session = command.baca_sesi(folder, retry_detik=.25)
    return session, uipad_op._inspect(command, folder, session)


def edge(folder, button):
    """One leased ui_press, after fresh pins, with independently confirmed release."""
    session, initial = inspect(folder)
    identity = uipad_op._identity(session, initial)
    owner, generation = "q12-" + uuid.uuid4().hex[:12], time.time_ns() // 1_000_000
    command.tulis_kontrol(command.KIND_ACQUIRE, folder=folder, sesi=session, owner_id=owner,
        generation=generation, control_seq=1, expected_screen_id=session.screen_id)
    result = None
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = command.baca_sesi(folder, retry_detik=.25)
            if current.mentah.get("owner_id") == owner and current.mentah.get("generation") == generation:
                break
            time.sleep(.02)
        else:
            raise RuntimeError("Q12 acquire not confirmed")
        fresh = uipad_op._inspect(command, folder, current)
        if uipad_op._identity(current, fresh) != identity:
            raise RuntimeError("Q12 identity changed")
        view = ui_pad.baca_ui_pad(fresh, age_s=0)
        if view is None or not view.available or not view.press_allowed(button):
            raise RuntimeError("Q12 backend edge not offered: " + button)
        current = command.baca_sesi(folder, retry_detik=.25)
        admission = uipad_op._lease_admission(current, fresh, identity, owner, generation, time)
        result = command.minta_ui_press(**view.press_args(button), folder=folder, sesi=current,
            owner_id=owner, generation=generation, control_seq=1,
            context_generation=fresh["context_generation"], runtime_instance_id=fresh["runtime_instance_id"],
            expected_location_id=current.location_id, expected_screen_id=current.screen_id,
            admisi_detik=admission, deadline_cap_unix_ms=current.mentah["lease_until_unix_ms"], batas_detik=5)
    finally:
        current = command.baca_sesi(folder, retry_detik=.25)
        seq = current.mentah.get("control_seq", 1) if current.mentah.get("owner_id") == owner else 1
        command.tulis_kontrol(command.KIND_RELEASE, folder=folder, sesi=session, owner_id=owner,
            generation=generation, control_seq=seq + 1, expected_screen_id=session.screen_id)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            released = command.baca_sesi(folder, retry_detik=.25)
            if (command.lease_state(released.mentah).bound(command.lease_state(session.mentah))
                    and command.lease_state(released.mentah).retired(owner, generation, (seq + 1,))
                    and released.inflight_request_id is None
                    and command.input_release_state(released.mentah,
                        request_id=getattr(result, "request_id", None), result=getattr(result, "mentah", None)) is True):
                break
            time.sleep(.02)
        else:
            raise RuntimeError("Q12 release not confirmed")
    return result.mentah, fresh, inspect(folder)[1]


def verdict(button, raw, after):
    ledger = raw.get("ui_pad_readback")
    if isinstance(ledger, str):
        ledger = json.loads(ledger)
    if not ledger or ledger.get("delivered") is not True or ledger.get("released") is not True:
        raise RuntimeError("Q12 no confirmed backend delivery/release; not capability evidence")
    opened = after["ui_motor"]["active_menu"] == "GameMenu"
    old, new = ledger["before"]["inventory"], ledger["after"]["inventory"]
    # Readback must show the complete native row rotation, including empty slots.
    old = [(s["item"], s["stack"], s["quality"]) for s in old]
    new = [(s["item"], s["stack"], s["quality"]) for s in new]
    rotations = (old[12:] + old[:12], old[-12:] + old[:-12])
    return dict(opened=opened, slot_shifted=new != old and new in rotations,
                delivered=True, released=True)


def run_drill(folder, output, drawable, keys=("Start", "LB", "RB"), lb_start=1, rb_start=1):
    output = Path(output)
    rows = []
    for button in keys:
        for attempt in range(lb_start if button == "LB" else rb_start if button == "RB" else 1, 4):
            drawable.check()
            farmhand = next(b for b in drawable.bindings if b["role"] == "farmhand")
            if win_facts.foreground() == farmhand["hwnd"]:
                raise RuntimeError("Q12 farmhand must be visible-inactive")
            _, context = inspect(folder)
            if context["ui_motor"]["surface"] != "HUD" or context["ui_motor"]["active_menu"]:
                raise RuntimeError("Q12 requires HUD before every edge")
            raw, before, after = edge(folder, button)
            (output / f"q12-raw-{button}-{attempt}.json").write_text(
                json.dumps(dict(result=raw, before=before, after=after), indent=2), encoding="utf-8")
            row = dict(key=button, attempt=attempt, visible_inactive=True, **verdict(button, raw, after))
            row["result"] = raw
            rows.append(row)
            (output / f"q12-{button}-{attempt}.json").write_text(
                json.dumps(dict(row=row, before=before, after=after), indent=2), encoding="utf-8")
            (output / "Q12.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            if row["opened"]:
                motor = after["ui_motor"]
                cleanup = uipad_op.close_once(folder=folder, menu=motor["active_menu"],
                    receiver=after["ui_pad"]["receiver_token"], yes=True)
                _, restored = inspect(folder)
                if (not cleanup["release_verified"] or restored["ui_motor"]["active_menu"]
                        or restored["ui_motor"]["surface"] != "HUD"):
                    raise RuntimeError("Q12 menu cleanup not confirmed")
