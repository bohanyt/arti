"""Satu pemanggilan Python meminta satu aksi lewat existing command inbox.

`request.json` tetap satu-satunya ingress dan `result.json` tetap mailbox hasil
terbaru. Tidak ada retry otomatis. Issue #184 menambahkan mode journal hanya
untuk leased mutations ketika session native secara eksplisit mengiklankan
`journal_supported=true` + schema 1; caller protocol-v1 lama tetap memakai
arbitrary UUID seperti sebelumnya.
"""

from __future__ import annotations

from arti_runtime import wait_ledger as W

from arti_runtime import ipc
import argparse
import base64
import contextlib
import functools
import hashlib
import json
import math
import os
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Per calling action thread; never changes wall-clock leases or safety polls.
result_wait_clock = ContextVar("stardew_result_wait_clock", default=None)
# fix55C / ADR 78 (REVIEW2 #4): a cancel/STOP seen during a result wait shortens it to this grace (a leased move's wait
# grew to 4.5 s under load). The result is still read; a missing one stays uncertain, never replayed.
result_wait_cancel = ContextVar("stardew_result_wait_cancel", default=None)
CANCEL_WAIT_GRACE_S = 0.5


@contextlib.contextmanager
def result_wait_cancelled(cancelled):
    token = result_wait_cancel.set(cancelled)
    try:
        yield
    finally:
        result_wait_cancel.reset(token)

PROTOCOL = 1
OP_MENU_STEP = "menu_step"
OP_WATER_ONCE = "water_once"
OP_WORLD_PRESS = "world_press"
OP_INPUT_ONCE = "input_once"
OP_INSPECT_TILES = "inspect_tiles"
OP_INSPECT_CONTEXT = "inspect_context"

FAMILY_WORLD_KEY = "world_key"
PRIMITIVE_MOVE_CARDINAL = "move_cardinal"
PRIMITIVE_USE_TOOL = "use_tool"
# WS3a (2026-10-07): walk-onto warp through the background move run (family world_key).
PRIMITIVE_MOVE_THROUGH = "move_through"
# world_interact kinds; the mod's success outcome differs per kind (door/forage: no receiver).
# WS3b: npc talk / eat prompt open a DialogueBox receiver (ui_key closes / answers it).
# Primitives v2 WS-G6 (mod Protocol/WorldExtras.cs, WorldExtrasEnabled): world extras kinds on the pad path; the mod
# captures them only with its flag on, so the v1 kinds / digests are unchanged with the flag off.
EVENT_ACTION_OUTCOMES = {"event_target": "EventTargetActivated", "festival_npc": "DialogueOpened",
    "festival_shop": "MenuOpened", "festival_seat": "FestivalSeated", "festival_egg": "FestivalEggCollected",
    "festival_egg_route": "FestivalEggCollected", "event_interact": "EventInteractionObserved",
    "festival_stand": "FestivalStood"}  # the game's own A while she sits on a festival seat
APPROACH_ROUTE_KINDS = ("event_target_route", "festival_npc_route", "festival_shop_route", "festival_seat_route", "event_interact_route")
EVENT_ACTION_OUTCOMES.update({kind: EVENT_ACTION_OUTCOMES[kind[:-6]] for kind in APPROACH_ROUTE_KINDS})
EVENT_ROUTE_KINDS = APPROACH_ROUTE_KINDS + ("festival_egg_route",)
WORLD_EXTRAS_OUTCOMES = {
    "tv": "QuestionOpened", "mailbox": "MenuOpened",
    "npc_gift": "GiftGiven", "pet": "Petted", "animal_pet": "AnimalPetted", "animal_milk": "ProductCollected", "animal_shear": "ProductCollected",
    "horse_mount": "Mounted", "horse_dismount": "Dismounted", "shake_tree": "TreeShaken", "harvest_crop": "CropHarvested",
    "machine_collect": "MachineCollected", "machine_insert": "MachineLoaded", "hay_hopper": "HayTaken", "hay_place": "ItemPlaced",
    "ladder_down": "LocationChanged", "shaft": "QuestionOpened", "mine_elevator": "MenuOpened", "bus": "QuestionOpened",
    "minecart": "QuestionOpened", "place_item": "ItemPlaced",
    # Fix D-B2-1: the shop counter opens the ShopMenu (mod WorldExtras.ShopCounter; also with EconomyMenusEnabled).
    "shop_counter": "MenuOpened",
    "animal_shop": "QuestionOpened", "blacksmith": "QuestionOpened", "community_note": "MenuOpened",
    # FIXB7 D-B5b-3: the shipping bin opens ItemGrabMenu(shippingBin) (mod WorldExtras.ShippingBin; economy flag).
    "shipping_bin": "MenuOpened",
    # FIXB17 D-B5-0: the Mines up-ladder opens the ExitMine question (mod WorldExtras.LadderUp).
    "ladder_up": "QuestionOpened",
    # W1 gap B3 S30: the faced Kitchen A opens the standalone cooking CraftingPage (mod WorldExtras.Kitchen).
    "kitchen": "MenuOpened",
    # W1 gap B4 S28: the faced villager A completes ONE active native item-delivery quest (mod WorldExtras.QuestDelivery).
    "quest_delivery": "QuestDelivered",
}
WORLD_EXTRAS_KINDS = tuple(WORLD_EXTRAS_OUTCOMES)
# Kinds whose press warps (door machinery: target_location_id required).
HOUSE_ROUTE_KIND = "animal_house_route"
HOUSE_ROUTE_INPUTS_MAX = 96
WORLD_INTERACT_LOCATION_KINDS = ("door", "ladder_down", HOUSE_ROUTE_KIND)
GENERIC_WORLD_KINDS = ("generic_action", "generic_use")
WORLD_INTERACT_KINDS = ("chest", "door", "forage", "npc", "eat", HOUSE_ROUTE_KIND) + WORLD_EXTRAS_KINDS + tuple(EVENT_ACTION_OUTCOMES) + GENERIC_WORLD_KINDS
OUTCOME_SUKSES_INTERACT = {"chest": "ContainerOpened", "door": "LocationChanged", "forage": "ItemPickedUp",
                           "npc": "DialogueOpened", "eat": "QuestionOpened", HOUSE_ROUTE_KIND: "LocationChanged", **WORLD_EXTRAS_OUTCOMES, **EVENT_ACTION_OUTCOMES}
OUTCOME_SUKSES_INTERACT.update({k: "WorldDeltaObserved" for k in GENERIC_WORLD_KINDS})
# WS3b: bed = walk sideways onto the owned bed's sleep tile (TouchAction Sleep -> Sleep question).
MOVE_THROUGH_KINDS = ("warp", "bed")
MOVE_THROUGH_TICKS_MAX = 48  # D29 (live B3W6c/B4W1a): distance-2 warp needs ~47; mod MoveThroughTarget.MaxTicks
# Review 2026-10-07: the expected location reached while an arrival event / menu holds the player is
# still the success (never a warp timeout); the readback then does not require a free player.
OUTCOME_EVENT_UP = {"LocationChanged": "LocationChangedEventUp", "TransitionObserved": "TransitionObservedEventUp"}


def _legacy_compat_pad_released(raw: dict, *, require_pad: bool = False) -> bool:
    """legacy_compat: restoration can prove legacy idle; failure is only unproven."""
    if raw.get("pad_mode_cleanup_fault", "") != "":
        return False
    if not require_pad and "gamepad_input_enabled" not in raw and "pad_mode_owned" not in raw:
        return True
    return (raw.get("gamepad_input_enabled") is True
            and raw.get("pad_mode_owned") is False
            and raw.get("pad_mode_restore_pending") is False
            and type(raw.get("pad_outstanding_count")) is int
            and raw["pad_outstanding_count"] == 0
            and raw.get("gamepad_mode") in ("Auto", "ForceOff"))


def release_results(raw: dict, request_id, result=None):
    """Correlated direct/Settled evidence only; absence never proves nonexecution."""
    answers = raw.get("status_answers")
    for candidate in [result] + [a.get("result") for a in (answers if isinstance(answers, list) else [])
            if isinstance(a, dict) and a.get("status") == "Settled" and a.get("request_id") == request_id]:
        if (isinstance(candidate, dict) and request_id and candidate.get("request_id") == request_id
                and candidate.get("session_id", raw.get("session_id") if candidate.get("v") == "arti.verb_result/3" else None) == raw.get("session_id")
                and candidate.get("runtime_instance_id", raw.get("runtime_instance_id")) == raw.get("runtime_instance_id")):
            yield candidate


# Process-local publication provenance, scoped to the native runtime/session/player.
# Retain outstanding writes: a refused request need not replace the previous core receipt.
_written_requests: dict[tuple, set[str]] = {}


def _request_binding(raw):
    session, runtime, player, screen = (raw.get(k) for k in
        ("session_id", "runtime_instance_id", "expected_player_id", "expected_screen_id"))
    if (type(session) is str and session and type(runtime) is str and runtime
            and type(player) is int and player != 0 and type(screen) is int and screen >= 0):
        return session, runtime, player, screen
    return None


def _retained_idle(receipt, client):
    """A native client's retained release is occupancy evidence, never effect credit (ADR-51)."""
    if not isinstance(receipt, dict):
        return receipt is None
    if (receipt.get("client") != client or receipt.get("idle") is not True
            or any(type(receipt.get(k)) is not str or not 0 < len(receipt[k]) <= 128
                   for k in ("request_id", "attempt_id"))
            or any(type(receipt.get(k)) is not int or receipt[k] < 0 for k in ("tick", "frame", "ms"))
            or receipt.get("via") not in ("release", "neutralize", "tick_lapse")):
        return False
    lapse = receipt.get("lapse")
    return receipt["via"] != "tick_lapse" or (isinstance(lapse, dict)
        and all(type(lapse.get(k)) is int and lapse[k] >= 0 for k in ("from_tick", "from_ms", "to_ms")))


@W.release_verdict
def input_release_state(raw: dict, *, request_id=None, result=None) -> bool | None:
    """R0-05 W1: current native held evidence precedes historical release proof.

    True = correlated release, or native empty clients before any publication.
    False = native held evidence; None = unproven, never stuck.
    Legacy idle compatibility remains; occupancy/owned ForceOn alone is not held.
    Closure/op_closed proves effect closure separately and cannot replace W1.
    """
    if type(raw.get("pad_outstanding_count")) is int and raw["pad_outstanding_count"] > 0:
        return False
    if (request_id or "input_idle" in raw) and any(not isinstance(raw.get(k), str) or not raw[k]
                          for k in ("session_id", "runtime_instance_id")):
        return None
    slots = raw.get("input_idle")
    core = slots.get("core") if isinstance(slots, dict) else None
    receipts = [(core, request_id, request_id)]
    binding = _request_binding(dict(raw, expected_player_id=raw.get("player_id"), expected_screen_id=raw.get("screen_id")))
    ours = _written_requests.get(binding, set())
    # A restarted/day runner has no request to correlate. Native clients retain
    # their last releases across wakes; they do not become empty again. Prove
    # current quiescence from BOTH clients and the retired lease. Outstanding
    # pad Presses matter; suppress-only overrides and historical journal debt
    # do not mean held input. Journal admission keeps its independent fence.
    if (request_id is None and result is None and binding is not None and not ours
            and isinstance(slots, dict) and set(slots) == {"core", "progression"}
            and any(isinstance(v, dict) for v in slots.values())
            and _retained_idle(slots["core"], "inbox") and _retained_idle(slots["progression"], "progression")
            and all(k in raw for k in ("owner_id", "generation", "control_seq", "lease_until_unix_ms", "halt"))
            and raw["owner_id"] is None and lease_state(raw).vacant
            and raw.get("input_busy") is False and "inflight_request_id" in raw and raw["inflight_request_id"] is None
            and raw.get("release_acked") is True and raw.get("release_requested") is False
            and raw.get("stop_latched") is False and raw.get("record_faulted") is False and raw["halt"] is None
            and type(raw.get("pad_outstanding_count")) is int and raw["pad_outstanding_count"] == 0
            and raw.get("pad_mode_owned") is False and raw.get("pad_mode_restore_pending") is False
            and raw.get("pad_mode_cleanup_fault") == ""):
        return True
    # No request exists to correlate before the first publication. Native must
    # positively prove both input clients empty and the lease retired; idle or
    # a missing request ID alone is insufficient (ADR-31).
    if (request_id is None and result is None and binding is not None and not ours
            and isinstance(slots, dict) and set(slots) == {"core", "progression"}
            and all(value is None for value in slots.values())
            and all(k in raw for k in ("owner_id", "generation", "control_seq", "lease_until_unix_ms", "halt"))
            and raw["owner_id"] is None
            and raw.get("input_busy") is False and "inflight_request_id" in raw
            and raw["inflight_request_id"] is None and lease_state(raw).vacant
            and raw.get("release_acked") is True and raw.get("release_requested") is False
            and raw.get("stop_latched") is False and raw.get("record_faulted") is False
            and raw.get("halt") is None
            and all(type(raw.get(k)) is int and raw[k] == 0 for k in
                    ("pad_outstanding_count", "pad_override_count", "journal_unresolved_count"))
            and raw.get("pad_mode_owned") is False and raw.get("pad_mode_restore_pending") is False
            and raw.get("pad_mode_cleanup_fault") == ""):
        return True
    # PM-1 fix10c (a12 J827): an ADMITTED request is released only by its own receipt. Borrowing the previous
    # move's idle receipt reported cleanup_confirmed while native still held the slot for 18.9 min.
    admitted = any(c.get("accepted") is True for c in release_results(raw, request_id, result))
    if (not admitted and isinstance(core, dict) and type(core.get("request_id")) is str and core["request_id"] in ours
            and (core.get("idle") is False or "inflight_request_id" in raw and raw["inflight_request_id"] is None
                and type(raw.get("pad_outstanding_count")) is int and raw["pad_outstanding_count"] == 0)):
        receipts.append((core, core.get("attempt_id"), core["request_id"]))
    receipt_protocol = raw.get("v") == "arti.session/3" or any(k in raw for k in ("input_idle", "halt", "status_answers", "fenced_generation")) or isinstance(result, dict) and "release_receipt" in result
    for candidate in release_results(raw, request_id, result):
        attempt_id = candidate.get("attempt_id", request_id)
        receipts.extend(((candidate.get("release_receipt"), attempt_id, request_id), (core, attempt_id, request_id)))
        receipt_protocol |= "release_receipt" in candidate
    matched = []
    for receipt, attempt_id, receipt_request in receipts:
        if (not isinstance(receipt, dict) or type(receipt_request) is not str or not receipt_request
                or type(attempt_id) is not str or not 0 < len(attempt_id) <= 128
                or receipt.get("request_id") != receipt_request or receipt.get("attempt_id") != attempt_id
                or receipt.get("client") != "inbox" or type(receipt.get("idle")) is not bool
                or receipt.get("via") not in ("release", "neutralize", "tick_lapse")
                or any(type(receipt.get(k)) is not int or receipt[k] < 0 for k in ("tick", "frame", "ms"))):
            continue
        if receipt["via"] == "tick_lapse":
            lapse = receipt.get("lapse")
            if (not isinstance(lapse, dict) or any(type(lapse.get(k)) is not int or lapse[k] < 0
                    for k in ("from_tick", "from_ms", "to_ms"))):
                continue
        matched.append(receipt["idle"])
    if matched:
        return all(matched)  # native held evidence is never masked by another route
    if receipt_protocol:
        return None  # N1 advertises receipts; a legacy completion is not release proof
    if raw.get("input_busy") is False and _legacy_compat_pad_released(raw, require_pad=raw.get("gamepad_input_enabled") is True):
        return True
    return None


@dataclass(frozen=True)
class LeaseState:
    binding: tuple
    owner: str | None
    generation: int | None
    seq: int | None
    expiry: int | None
    fenced_generation: int | None
    acquired_ms: int | None
    halted: bool
    halt: dict | None
    halt_valid: bool
    rearmed: dict | None
    halt_notes: tuple
    faulted: bool
    release_requested: bool
    release_acked: bool

    def bound(self, other, *, location=False):
        return self.matches(*other.binding[:4]) and (not location or self.binding[4] == other.binding[4])

    def matches(self, session, runtime, player, screen, location=None, name=None):
        return (type(player) is int and player != 0 and type(screen) is int and screen >= 0
                and all(v is not None for v in self.binding[:4]) and self.binding[:4] == (session, runtime, player, screen)
                and (location is None or self.binding[4] == location) and (name is None or self.binding[5] == name))

    def owns(self, owner, generation, seq=None, *, live=False):
        return (type(owner) is str and bool(owner) and type(generation) is int and generation > 0
                and self.owner == owner and self.generation == generation and self.seq is not None and self.seq > 0
                and (seq is None or type(seq) is int and seq > 0 and self.seq == seq) and (not live or self.expiry is not None and self.expiry > _sekarang_ms()))

    @property
    def blocked(self):
        return self.halted or self.faulted or self.release_requested

    @property
    def vacant(self):
        return self.owner is None and all(v in (None, 0) for v in (self.generation, self.seq, self.expiry))

    def can_acquire(self, generation):
        return type(generation) is int and generation > 0 and all(v is not None for v in self.binding[:4]) and self.vacant and not self.blocked and self.fenced_generation is not None and generation > self.fenced_generation

    def retired(self, owner, generation, sequences):
        return (self.owner is None or self.halted and self.owns(owner, generation) and self.seq in sequences) and self.release_acked and not self.release_requested


def lease_state(raw: dict) -> LeaseState:
    """Typed binding/lease/HALT/fence facts. Generation never authorizes re-arm."""
    def number(k, missing=None):
        v = raw.get(k)
        return missing if k not in raw else None if v is None else v if type(v) is int and v >= 0 else -1
    def text(k):
        v = raw.get(k)
        return v if type(v) is str and 0 < len(v) <= 128 else None
    halt = raw.get("halt")
    valid = ("halt" in raw or raw.get("v") != "arti.session/3") and (halt is None or
        isinstance(halt, dict) and type(halt.get("episode_id")) is str and bool(halt["episode_id"])
        and type(halt.get("halt_seq")) is int and halt["halt_seq"] > 0
        and type(halt.get("entries_digest")) is str and len(halt["entries_digest"]) == 64
        and halt.get("scope") in ("core", "all") and type(halt.get("persisted")) is bool
        and type(halt.get("since_ms")) is int and halt["since_ms"] >= 0
        and isinstance(halt.get("entries"), list) and 0 < len(halt["entries"]) <= 16
        and all(isinstance(e, dict) and type(e.get("stop_id")) is str for e in halt["entries"]))
    # legacy_compat: old latch levels may deny; they can never clear a persisted HALT.
    halted = not valid or halt is not None or raw.get("stop_latched", False if "halt" in raw else None) is not False
    player, screen = raw.get("player_id"), number("screen_id")
    binding = (text("session_id"), text("runtime_instance_id"), player if type(player) is int and player != 0 else None,
        screen if screen is not None and screen >= 0 else None, raw.get("location_id"), raw.get("player_name"))
    # legacy_compat: pre-N1 has no native fence; malformed N1 never grants acquire.
    n1 = raw.get("v") == "arti.session/3" or "input_idle" in raw or "halt" in raw
    missing = -1 if n1 else None
    fence = number("fenced_generation", -1 if n1 else 0)
    rearmed = raw.get("rearmed")
    if not (isinstance(rearmed, dict) and type(rearmed.get("episode_id")) is str
            and type(rearmed.get("halt_seq")) is int and rearmed["halt_seq"] > 0
            and type(rearmed.get("ms")) is int and rearmed["ms"] >= 0
            and isinstance(rearmed.get("stop_ids"), list) and all(type(s) is str for s in rearmed["stop_ids"])):
        rearmed = None
    notes = raw.get("halt_notes")
    return LeaseState(binding,
        "<invalid>" if n1 and "owner_id" not in raw else text("owner_id") if raw.get("owner_id") in (None, "") or text("owner_id") else "<invalid>",
        number("generation", missing), number("control_seq", missing), number("lease_until_unix_ms", missing), fence if fence is not None and fence >= 0 else None, number("acquired_ms"),
        halted, halt if valid else None, bool(valid), rearmed, tuple(notes) if isinstance(notes, list) else (), raw.get("record_faulted") is not False,
        raw.get("release_requested") is not False, raw.get("release_acked") is True)


def admission_state(raw, required=(), *, idle=True, op=None, primitive=None) -> str | None:
    """Availability projection; slot occupancy is never held-input evidence."""
    if isinstance(raw, SesiPerintah):
        return raw._admission_reason(op or OP_MENU_STEP, primitive=primitive)
    lease = lease_state(raw)
    if lease.blocked:
        return "stop_latched" if lease.halted else "record_faulted" if lease.faulted else "release_requested"
    for key, wanted in (("is_main_player", False), ("is_split_screen", False), ("world_ready", True), ("inbox_enabled", True), *required):
        if raw.get(key) is not wanted:
            return key
    if idle and (raw.get("input_busy") is not False or "inflight_request_id" not in raw or raw.get("inflight_request_id") is not None):
        return "slot_busy"
    return None


def outcome_matches(outcome, expected) -> bool:
    """The expected success outcome, or its event-up variant (never a None / unknown outcome)."""
    return outcome is not None and expected is not None and outcome in (expected, OUTCOME_EVENT_UP.get(expected))


# The native commit/progress ladder is bounded at 60 s (TravelProgress.TotalWaitMs).
# Result polling must outlive it plus cleanup. This does not extend input/admission deadlines.
WARP_RESULT_EXTRA_SECONDS = 62.0

NAMA_REQUEST = "request.json"
NAMA_RESULT = "result.json"
NAMA_SESSION = "session.json"
NAMA_CONTROL = "control.json"
NAMA_STOP = "stop.json"

KIND_ACQUIRE = "acquire"
KIND_RENEW = "renew"
KIND_RELEASE = "release"

LEASE_DURATION_MS = 1500
LEASE_MAX_MS = 4000  # NativeActionOwner.MaxLeaseDurationMs (fix54I)
RENEW_HINT_MS = 500

ARAH_SAH = ("down", "right", "up", "left")
TIMEOUT_TICKS_MIN = 1
TIMEOUT_TICKS_MAX = 60
TIMEOUT_TICKS_DEFAULT = 60
DELAY_TICKS_MIN = 0
DELAY_TICKS_MAX = 1800
TILE_MIN = 0
TILE_MAX = 10000
WATER_ONCE_MAX_TICKS = 180
WORLD_PRESS_TICKS_MIN = 1
WORLD_PRESS_TICKS_MAX = 60
# fix89D / ADR 158: a move_cardinal that names its straight segment's stop tile may hold up to
# MoveSegmentGuard.MaxTicks (mod); the native guard ends the hold at the stop tile, on no progress
# or on leaving the line. Without a stop tile the 1..60 bound is unchanged.
MOVE_SEGMENT_TICKS_MAX = 120
MOVE_SEGMENT_TILES_MAX = 10
# fix92A / ADR 162: a guarded PATH (segment_stop + path_next: the following stop tiles, each a 90-degree turn)
# runs in one request on the pad path; the mod turns at each reached stop after proving the old pin released.
MOVE_PATH_TICKS_MAX = 180  # MovePath.MaxTicks: 4 s lease - 0.5 s admission margin - 0.5 s deadline margin
MOVE_PATH_TILES_MAX = 30
MOVE_PATH_SEGMENTS_MAX = 8
INSPECT_RADIUS_MIN = 0
INSPECT_RADIUS_MAX = 4

OUTCOME_SUKSES = {
    OP_MENU_STEP: "Stepped",
    OP_WATER_ONCE: "ObservedWatered",
    OP_WORLD_PRESS: "PressCompleted",
    OP_INPUT_ONCE: "PressCompleted",
    OP_INSPECT_TILES: "Inspected",
    OP_INSPECT_CONTEXT: "ContextInspected",
}
# input_once carries one primitive; each has its own completion outcome (the mod's
# CompleteUseTool/CompleteSelection/CompleteWorldInteraction/... writers). Live 2026-10-06
# (M1 attempt 9w): a completed background use_tool (outcome InputCompleted, success true)
# was read back as not successful because only the op table was consulted.
OUTCOME_SUKSES_PRIMITIVE = {
    "move_cardinal": "PressCompleted",
    "use_tool": "InputCompleted",
    "select_item": "SelectionObserved",
    "world_interact": "ContainerOpened",  # chest; other kinds: OUTCOME_SUKSES_INTERACT
    "move_through": "TransitionObserved",
    "container_transfer": "TransferObserved",
    "container_exit": "MenuExited",
    "ui_key": "UiKeyApplied",
    "ui_nav": "UiNavApplied",      # Primitives v2 WS-G1
    "ui_press": "UiPressApplied",  # Primitives v2 WS-G1 (effect in ui_key_effect)
    "chat_send": "ChatSent",       # Primitives v2 WS-G3 (owner D5, default OFF)
    "reflex_fish": "FishCaught",  # Primitives v2 WS-G4: outcome = verdict kind; other kinds are settled non-success
    "reflex_combat": "MonsterKilled",  # Primitives v2 WS-G5: outcome = verdict kind (wire MonsterKilled(n))
    "reflex_charge": "ChargeReached",  # Primitives v2 WS-G5: success also needs an observed area change
}

# Primitives v2 WS-G0 slice 2: input_once family world_key primitive "pad_button" (mod
# Protocol/PadButtonInput.cs): one world pad button as a direct call. LB/RB shift the toolbar row,
# Start/Y open the GameMenu (Y on the crafting tab), Select the QuestLog (sub-flag). Success outcome
# per button; refusals (PadContextTable "—" cells / denylist) are mod rejections before any call.
PRIMITIVE_PAD_BUTTON = "pad_button"
PAD_BUTTONS = ("LB", "RB", "Start", "Y", "Select")
OUTCOME_EVENT_SKIPPED = "EventSkipped"  # FIXB31: pad_button Select skipped a plain cutscene (mod PadButtonVerdict)
OUTCOME_SUKSES_PAD_BUTTON = {"LB": "ToolbarShifted", "RB": "ToolbarShifted",
                             "Start": "MenuOpened", "Y": "MenuOpened", "Select": "MenuOpened"}

# Primitives v2 WS-G1: input_once families ui_nav / ui_press (mod Protocol/UiPadInput.cs). Args are copied from
# the mod's published ui.context "ui_pad" block (receiver / menu / context / up: target token); the per-context
# button table lives in the mod. Reserved payloads (qty/half/stack/quick/text/value) belong to G2/G3: refused here.
PRIMITIVE_UI_NAV = "ui_nav"
PRIMITIVE_UI_PRESS = "ui_press"
UI_PAD_CONTEXTS = ("game_menu", "letter", "billboard", "special_orders", "quest_log", "dialogue", "question",
                   "npc_question", "level_up", "level_up_profession", "shipping", "item_grab", "pond_query", "event",
                   "text_entry", "other", "naming", "number", "ready_check", "shop", "crafting",
                   "mine_elevator", "junimo_note", "museum", "purchase_animals",
                   # Live7 character creation (mod Protocol/CharCustomInput.cs, CharacterCreationEnabled only).
                   "char_custom")
UI_NAV_DIRS = ("up", "right", "down", "left")
UI_PRESS_BUTTONS = ("A", "B", "X", "Y", "LB", "RB", "LT", "RT", "Start", "Select", "tab")
UI_PAD_TAB_NAMES = ("inventory", "skills", "social", "map", "crafting", "animals", "powers", "collections", "options",
                    "exit")
UI_PAD_RESERVED_ARGS = ("qty", "half", "stack", "quick", "text", "value", "choice", "festival", "slot", "item", "building", "shift", "settle")
# Primitives v2 WS-G3 (mod Protocol/UiTextEventInput.cs UiG3Table.ArgsFor): the reserved args a G3 context consumes.
# Values travel as strings (the mod echoes every reserved arg back as a string in input_args).
UI_PRESS_CONTEXT_ARGS = {"naming": ("text",), "number": ("value",), "event": ("choice", "festival", "slot", "item"),
                         "question": ("choice",), "purchase_animals": ("building",),  # offered response only; receiver/native policy still gates it
                         # Live7: one character control = choice (+ value for arrows / gender / colour bars, + text for name / favourite).
                         "char_custom": ("choice", "value", "text")}
UI_PAD_CONTEXT_CHAR_CUSTOM = "char_custom"
UI_G3_FESTIVAL_KINDS = ("fortune", "secret_santa_gift", "luau_soup", "grange_place", "star_tokens_bet", "star_tokens_buy")
UI_G3_NAME_MAX = 12
CHAT_MAX = 100

# Primitives v2 WS-G3 owner D5: input_once family world_key primitive "chat_send" (mod ChatSendTarget), args {text}.
PRIMITIVE_CHAT_SEND = "chat_send"
# Primitives v2 WS-G2: the economy payloads the mod implements on the shop / crafting rows (Protocol/EconomyInput.cs):
# qty = int 1..999 (A on a stock row / recipe), half = true (X on a sell slot), stack = true (A on a sell slot).
# quick (chests use container_transfer mode quick) and text / value (G3) stay refused.
UI_PRESS_ECONOMY_ARGS = ("qty", "half", "stack")
# The contexts whose rows consume them (GameMenu: the crafting tab; the mod refuses them on every other page).
UI_PRESS_ECONOMY_CONTEXTS = ("shop", "crafting", "game_menu", "other")
# Primitives v2 WS-G2 container_transfer modes (the mod's ContainerTransferTarget.Modes); no mode = v1 unchanged.
CONTAINER_TRANSFER_MODES = ("quick", "take_out", "ship_one", "ship_stack")
UI_PAD_BASE_ARGS = ("receiver_token", "menu_type", "context", "target_token")

# Primitives v2 WS-G4: input_once family world_key primitive "reflex_fish" (mod Protocol/FishingReflexWiring.cs +
# FishingReflexRunner.cs): one model call -> one bounded mod loop (cast, hook, BobberBar, dismiss) -> one verdict.
# Args exactly {max_s, cast_power, treasure, hook_retries} (FishReflexArgs bounds below).
PRIMITIVE_REFLEX_FISH = "reflex_fish"
REFLEX_FISH_ARG_KEYS = ("max_s", "cast_power", "treasure", "hook_retries")
# Primitives v2 WS-G5: reflex_combat (mod CombatReflexRunner: face / swing / retreat, one verdict) and reflex_charge
# (mod ChargeReflexRunner: pad X held until toolPower reaches the level, one verdict). Args exactly these keys.
PRIMITIVE_REFLEX_COMBAT = "reflex_combat"
REFLEX_COMBAT_ARG_KEYS = ("max_s", "hp_floor_pct", "max_kills", "retreat")
REFLEX_COMBAT_OPTIONAL_KEYS = ("target_kind", "escape", "disengage")  # ADR 169 escape batch; ADR 177 body leg
REFLEX_COMBAT_DISENGAGE_MAX_S = 30  # ADR 177: mirror of ReflexContracts.CombatDisengageMaxSeconds
REFLEX_COMBAT_TARGET_KINDS = ("slime", "monster")
PRIMITIVE_REFLEX_CHARGE = "reflex_charge"
REFLEX_CHARGE_ARG_KEYS = ("target_level", "max_ticks")

# WS2 ui_key: copy of the mod's UiKeyTable (Protocol/UiKeyInput.cs). kind -> (menu_type, keys).
PRIMITIVE_UI_KEY = "ui_key"
UI_KEY_TABLE = {
    "dialogue": ("DialogueBox", ("advance",)),
    "question": ("DialogueBox", ("answer", "cancel")),
    "npc_question": ("DialogueBox", ("answer",)),
    "level_up": ("LevelUpMenu", ("confirm",)),
    "level_up_profession": ("LevelUpMenu", ("choose",)),
    "shipping_summary": ("ShippingMenu", ("confirm",)),
    "letter": ("LetterViewerMenu", ("close",)),
}
UI_KEY_WAIT_ONLY = ("SaveGameMenu", "ReadyCheckDialog", "NamingMenu", "ShopMenu", "GameMenu", "ItemGrabMenu")
UI_KEY_BASE_ARGS = ("receiver_token", "menu_type", "kind", "key", "target_token")
UI_KEY_MAX_OPTIONS = 8

# Primitives v2 WS-G4/G5 reflex vocabulary: copy of the mod's Protocol/ReflexContracts.cs (constants only, no
# runtime wiring yet; tests/test_stardew_reflex_contracts.py keeps both sides equal). Verdict wire form:
# "Kind" or "Kind(arg)", e.g. "FishCaught", "Aborted(stamina_low)", "Retreated(hp_floor)", "ChargeReached(1)".
REFLEX_REQUEST_FISH = "reflex_fish"
REFLEX_REQUEST_COMBAT = "reflex_combat"
REFLEX_REQUEST_CHARGE = "reflex_charge"
REFLEX_REQUESTS = (REFLEX_REQUEST_FISH, REFLEX_REQUEST_COMBAT, REFLEX_REQUEST_CHARGE)
REFLEX_VERDICTS_FISH = ("FishCaught", "FishEscaped", "NoBite", "Aborted")
REFLEX_VERDICTS_COMBAT = ("MonsterKilled", "Retreated", "Downed", "NoTarget", "Aborted")
REFLEX_VERDICTS_CHARGE = ("ChargeReached", "ChargeAborted")
REFLEX_VERDICT_KINDS_WITH_ARG = ("Aborted", "Retreated", "ChargeReached", "ChargeAborted", "MonsterKilled")
REFLEX_REASONS = ("stop", "lease_lost", "focus_regain", "event", "foreign_menu", "location_changed", "timeout",
                  "exception", "interrupted", "tool_changed", "inventory_full", "stamina_low", "cast_no_effect",
                  "hook_missed", "hp_floor", "time_rule", "stuck", "no_exit", "above_cap", "not_chargeable",
                  "pad_refused", "heal_needed", "location_refused", "route_blocked", "contact")
REFLEX_RETREAT_REASONS = ("hp_floor", "time_rule", "timeout")
REFLEX_VERDICT_PATTERN = r"^(?P<kind>[A-Za-z]+)(?:\((?P<arg>[a-z0-9_]+)\))?$"
REFLEX_FISH_MAX_S_MAX = 90
REFLEX_FISH_MAX_S_DEFAULT = 60
REFLEX_FISH_CAST_POWER_MIN = 0.30
REFLEX_FISH_CAST_POWER_MAX = 0.95
REFLEX_FISH_CAST_POWER_DEFAULT = 0.85
REFLEX_FISH_HOOK_RETRIES_MAX = 2
REFLEX_FISH_STAMINA_FLOOR = 8.0
REFLEX_COMBAT_UNTIL = ("target_dead", "area_clear")
REFLEX_COMBAT_MAX_S_MAX = 60
REFLEX_COMBAT_HP_FLOOR_PCT_MIN = 30
REFLEX_COMBAT_HP_HEAL_GAP_PCT = 20
REFLEX_COMBAT_MAX_HEALS = 2
REFLEX_COMBAT_RADIUS_TILES_MAX = 8
REFLEX_COMBAT_LATEST_START_TIME = 2000
REFLEX_COMBAT_RETREAT_TIME = 2400
REFLEX_COMBAT_HARD_STOP_TIME = 2500
REFLEX_COMBAT_MAX_S_DEFAULT = 30
REFLEX_COMBAT_HP_FLOOR_PCT_DEFAULT = 40
REFLEX_COMBAT_MAX_KILLS_MAX = 5
REFLEX_COMBAT_MAX_KILLS_DEFAULT = 1
REFLEX_COMBAT_EARLIEST_START_TIME = 900
REFLEX_COMBAT_HARD_FLOOR_PCT = 20
REFLEX_CHARGE_LEVEL_MAX = 5
REFLEX_CHARGE_MS_PER_LEVEL = 600
REFLEX_CHARGE_TARGET_LEVEL_MAX = 4
REFLEX_CHARGE_MAX_TICKS_MIN = 45
REFLEX_CHARGE_MAX_TICKS_MAX = 200
REFLEX_CHARGE_MAX_TICKS_DEFAULT = 200

ADMISI_DETIK_DEFAULT = 3.0
JEDA_POLL_DETIK = 0.05

SELESAI = "selesai"
DITOLAK = "ditolak"
TERPUTUS = "terputus"
TIDAK_DIAMBIL = "tidak_diambil"
TIDAK_PASTI = "tidak_pasti"
GAGAL_LOKAL = "gagal_lokal"

JOURNAL_SCHEMA = 1
JOURNAL_REQUEST_PREFIX = "j1-"
_request_seq_last: dict[tuple[str, str, str, int], int] = {}


class KesalahanPerintah(Exception):
    """Permintaan tidak pernah ditulis; tidak ada input yang terjadi."""


@dataclass(frozen=True)
class SesiPerintah:
    session_id: str
    player_id: int
    player_name: str
    location_id: str
    screen_id: int
    is_main_player: bool
    is_split_screen: bool
    inbox_enabled: bool
    menu_step_enabled: bool
    water_once_enabled: bool
    world_press_enabled: bool
    world_ready: bool
    inflight_request_id: str | None
    input_busy: bool
    mentah: dict = field(default_factory=dict, repr=False)

    @property
    def siap(self) -> bool:
        return self.alasan_belum_siap(OP_MENU_STEP) is None

    def alasan_belum_siap(self, op: str = OP_MENU_STEP, *, primitive: str | None = None) -> str | None:
        return admission_state(self, op=op, primitive=primitive)

    def _admission_reason(self, op, *, primitive=None):
        if "halt" in self.mentah or self.mentah.get("v") == "arti.session/3":
            reason = admission_state(self.mentah, idle=op not in (OP_INSPECT_TILES, OP_INSPECT_CONTEXT))
            if reason:
                return reason
        if op not in (OP_INSPECT_TILES, OP_INSPECT_CONTEXT) and self.mentah.get("journal_blocked_reason"):
            return (f"{self.mentah['journal_blocked_reason']}:"
                    f"{self.mentah.get('journal_unresolved_count', '?')}/{self.mentah.get('journal_unresolved_limit', '?')}")
        if not self.session_id:
            return "session.json tanpa session_id"
        if not self.inbox_enabled:
            return "MenuStepCommandInboxEnabled mati di config mod"
        if op == OP_WATER_ONCE:
            if not self.water_once_enabled:
                return "WaterOnceEnabled mati di config mod"
        elif op == OP_INPUT_ONCE and primitive == "select_item":
            if self.mentah.get("select_item_enabled") is not True:
                return "SelectItemInputEnabled/binding belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == PRIMITIVE_UI_KEY:
            if self.mentah.get("ui_key_enabled") is not True:
                return "UiKeyInputEnabled/binding belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive in (PRIMITIVE_UI_NAV, PRIMITIVE_UI_PRESS):
            if self.mentah.get("ui_pad_enabled") is not True:
                return "GamepadInputEnabled/UiPadMenusEnabled belum siap di mod (farmhand harus inactive)"
        elif op == OP_INPUT_ONCE and primitive == PRIMITIVE_PAD_BUTTON:
            if self.mentah.get("pad_button_enabled") is not True:
                return "GamepadInputEnabled/PadButtonInputEnabled belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == PRIMITIVE_CHAT_SEND:
            if self.mentah.get("chat_send_enabled") is not True:
                return "ChatSendEnabled mati di config mod (owner D5)"
        elif op == OP_INPUT_ONCE and primitive == PRIMITIVE_REFLEX_FISH:
            if self.mentah.get("reflex_fish_enabled") is not True:
                return "ReflexFishingEnabled/pad path belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == PRIMITIVE_REFLEX_COMBAT:
            if self.mentah.get("reflex_combat_enabled") is not True:
                return "ReflexCombatEnabled/pad path belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == PRIMITIVE_REFLEX_CHARGE:
            if self.mentah.get("reflex_charge_enabled") is not True:
                return "ReflexChargeEnabled/pad path belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == "container_exit":
            if self.mentah.get("container_exit_enabled") is not True:
                return "ContainerExitInputEnabled/binding belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == "container_transfer":
            if self.mentah.get("container_transfer_enabled") is not True:
                return "ContainerTransferInputEnabled/binding belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == "world_interact":
            if self.mentah.get("world_interaction_enabled") is not True:
                return "WorldInteractionInputEnabled/binding belum siap di mod"
        elif op == OP_INPUT_ONCE and primitive == PRIMITIVE_USE_TOOL:
            if self.mentah.get("use_tool_enabled") is not True:
                return "UseToolInputEnabled/binding belum siap di mod"
        elif op in (OP_WORLD_PRESS, OP_INPUT_ONCE):
            if not self.world_press_enabled:
                return "WorldPressEnabled mati di config mod"
        elif op in (OP_INSPECT_TILES, OP_INSPECT_CONTEXT):
            pass
        elif not self.menu_step_enabled:
            return "ControllerMenuStepEnabled mati di config mod"
        if not self.world_ready:
            return "dunia belum siap (belum masuk save)"
        if self.is_main_player:
            return "instance ini HOST, bukan farmhand ARTI"
        if self.is_split_screen:
            return "instance ini split-screen, bukan remote farmhand"
        if self.inflight_request_id is not None:
            return f"masih ada perintah berjalan: {self.inflight_request_id}"
        if op not in (OP_INSPECT_TILES, OP_INSPECT_CONTEXT) and self.input_busy:
            return "input menu/dunia sedang dipakai probe lain"
        return None


@dataclass(frozen=True)
class HasilPerintah:
    status: str
    sukses: bool
    request_id: str
    outcome: str | None = None
    alasan: str | None = None
    mentah: dict | None = field(default=None, repr=False)
    refusal: dict | None = None  # WP P1: typed executed:false refusal (typed_refusal), never a success

    @property
    def pasti(self) -> bool:
        return self.status != TIDAK_PASTI

    def ringkas(self) -> str:
        bagian = [f"status={self.status}", f"sukses={self.sukses}"]
        if self.outcome:
            bagian.append(f"outcome={self.outcome}")
        if self.alasan:
            bagian.append(f"alasan={self.alasan}")
        bagian.append(f"request_id={self.request_id}")
        return " ".join(bagian)


def _sekarang_ms() -> int:
    return int(time.time() * 1000)


def _buka_bersama(path: Path):
    return ipc.open_shared(path)


def _baca_json(path: Path) -> dict | None:
    try:
        with _buka_bersama(path) as berkas:
            teks = berkas.read()
    except (FileNotFoundError, OSError):
        return None
    if not teks.strip():
        return None
    try:
        isi = json.loads(teks)
    except json.JSONDecodeError:
        return None
    return isi if isinstance(isi, dict) else None


def folder_perintah(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    dari_env = os.environ.get("ARTI_STARDEW_COMMAND_DIR", "").strip()
    if not dari_env:
        raise KesalahanPerintah(
            "folder kotak masuk tidak diketahui; beri argumen atau set "
            "ARTI_STARDEW_COMMAND_DIR ke folder command/ milik mod ARTI"
        )
    return Path(dari_env)


@W.span('settle_readback', 82, runtime=False)
def baca_sesi(folder: str | os.PathLike[str] | None = None, *, retry_detik: float = 0.0) -> SesiPerintah:
    """Baca session.json. retry_detik > 0: pembacaan yang gagal sesaat (berkas sedang
    diganti atomik oleh mod lewat temp+rename, atau belum ada) dicoba ulang setiap 10 ms
    sampai batas itu; setelah batas tetap gagal seperti biasa (fail-closed, read-only)."""
    dirpath = folder_perintah(folder)
    isi = _baca_json(dirpath / NAMA_SESSION)
    if isi is None and retry_detik > 0:
        from arti_runtime.file_arrival import FileArrival
        batas = time.monotonic() + retry_detik
        with FileArrival(dirpath) as arrival:
            isi = _baca_json(dirpath / NAMA_SESSION)  # close the read/arm race
            while isi is None and time.monotonic() < batas:
                W.call('poll', 85, arrival.wait, min(0.01, max(0., batas - time.monotonic())))
                isi = _baca_json(dirpath / NAMA_SESSION)
    if isi is None:
        raise KesalahanPerintah(
            f"{dirpath / NAMA_SESSION} tidak terbaca; mod belum jalan atau "
            "kotak masuknya mati"
        )
    if isi.get("protocol") != PROTOCOL:
        raise KesalahanPerintah(f"protocol sesi tidak dikenal: {isi.get('protocol')!r}")

    inflight = isi.get("inflight_request_id")
    return SesiPerintah(
        session_id=str(isi.get("session_id") or ""),
        player_id=int(isi.get("player_id") or 0),
        player_name=str(isi.get("player_name") or ""),
        location_id=str(isi.get("location_id") or ""),
        screen_id=int(isi.get("screen_id") or 0),
        is_main_player=bool(isi.get("is_main_player")),
        is_split_screen=bool(isi.get("is_split_screen")),
        inbox_enabled=bool(isi.get("inbox_enabled")),
        menu_step_enabled=bool(isi.get("menu_step_enabled")),
        water_once_enabled=bool(isi.get("water_once_enabled")),
        world_press_enabled=bool(isi.get("world_press_enabled")),
        world_ready=bool(isi.get("world_ready")),
        inflight_request_id=str(inflight) if inflight else None,
        input_busy=bool(isi.get("input_busy")),
        mentah=isi,
    )


def bersihkan_slot(folder: str | os.PathLike[str] | None = None) -> list[str]:
    dirpath = folder_perintah(folder)
    dibuang: list[str] = []
    for nama in (NAMA_REQUEST, NAMA_RESULT):
        target = dirpath / nama
        try:
            target.unlink()
            dibuang.append(nama)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise KesalahanPerintah(f"gagal menghapus {target}: {exc}") from exc
    return dibuang


def _validasi_argumen(arah: str, timeout_ticks: int, delay_ticks: int) -> str:
    arah_bersih = (arah or "").strip().lower()
    if arah_bersih not in ARAH_SAH:
        raise KesalahanPerintah(f"arah harus salah satu dari {ARAH_SAH}, dapat {arah!r}")
    if not TIMEOUT_TICKS_MIN <= timeout_ticks <= TIMEOUT_TICKS_MAX:
        raise KesalahanPerintah(
            f"timeout_ticks harus {TIMEOUT_TICKS_MIN}..{TIMEOUT_TICKS_MAX}, dapat {timeout_ticks}"
        )
    if not DELAY_TICKS_MIN <= delay_ticks <= DELAY_TICKS_MAX:
        raise KesalahanPerintah(
            f"delay_ticks harus {DELAY_TICKS_MIN}..{DELAY_TICKS_MAX}, dapat {delay_ticks}"
        )
    return arah_bersih


def _validasi_ubin(nilai: object, nama: str) -> int:
    if isinstance(nilai, bool) or not isinstance(nilai, int):
        raise KesalahanPerintah(
            f"{nama} harus integer {TILE_MIN}..{TILE_MAX}, dapat {nilai!r}"
        )
    if not TILE_MIN <= nilai <= TILE_MAX:
        raise KesalahanPerintah(f"{nama} harus {TILE_MIN}..{TILE_MAX}, dapat {nilai}")
    return nilai


def _validasi_radius(nilai: object) -> int:
    if isinstance(nilai, bool) or not isinstance(nilai, int):
        raise KesalahanPerintah(
            f"radius harus integer {INSPECT_RADIUS_MIN}..{INSPECT_RADIUS_MAX}, dapat {nilai!r}"
        )
    if not INSPECT_RADIUS_MIN <= nilai <= INSPECT_RADIUS_MAX:
        raise KesalahanPerintah(
            f"radius harus {INSPECT_RADIUS_MIN}..{INSPECT_RADIUS_MAX}, dapat {nilai}"
        )
    return nilai


def _validasi_location_id(nilai: object) -> str:
    if not isinstance(nilai, str) or not nilai.strip():
        raise KesalahanPerintah(
            f"expected_location_id harus string non-kosong, dapat {nilai!r}"
        )
    lokasi = nilai.strip()
    if len(lokasi) > 128:
        raise KesalahanPerintah("expected_location_id terlalu panjang")
    return lokasi


def _validasi_world_press(arah: str, ticks: object) -> tuple[str, int]:
    arah_bersih = (arah or "").strip().lower()
    if arah_bersih not in ARAH_SAH:
        raise KesalahanPerintah(f"arah harus salah satu dari {ARAH_SAH}, dapat {arah!r}")
    if isinstance(ticks, bool) or not isinstance(ticks, int):
        raise KesalahanPerintah(
            f"ticks harus integer {WORLD_PRESS_TICKS_MIN}..{WORLD_PRESS_TICKS_MAX}, dapat {ticks!r}"
        )
    if not WORLD_PRESS_TICKS_MIN <= ticks <= WORLD_PRESS_TICKS_MAX:
        raise KesalahanPerintah(
            f"ticks harus {WORLD_PRESS_TICKS_MIN}..{WORLD_PRESS_TICKS_MAX}, dapat {ticks}"
        )
    return arah_bersih, ticks


def _validasi_segment_press(arah: str, ticks: object, stop: object, event_token: object) -> tuple[str, int]:
    """fix89D / ADR 158: a segment-guarded move_cardinal (stop tile on the line ahead, checked natively)."""
    arah_bersih = (arah or "").strip().lower()
    if arah_bersih not in ARAH_SAH:
        raise KesalahanPerintah(f"arah harus salah satu dari {ARAH_SAH}, dapat {arah!r}")
    if event_token is not None:
        raise KesalahanPerintah("segment_stop_only_for_move_cardinal")
    if isinstance(ticks, bool) or not isinstance(ticks, int) or not WORLD_PRESS_TICKS_MIN <= ticks <= MOVE_SEGMENT_TICKS_MAX:
        raise KesalahanPerintah(f"ticks segmen harus {WORLD_PRESS_TICKS_MIN}..{MOVE_SEGMENT_TICKS_MAX}, dapat {ticks!r}")
    if (not isinstance(stop, (tuple, list)) or len(stop) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) or not TILE_MIN <= v <= TILE_MAX for v in stop)):
        raise KesalahanPerintah(f"segment_stop harus [x, y] integer {TILE_MIN}..{TILE_MAX}, dapat {stop!r}")
    return arah_bersih, ticks


def _validasi_path_next(arah: str, stop: object, path_next: object) -> tuple[tuple[int, int], ...]:
    """fix92A / ADR 162: the following stops of a guarded path; the mod re-checks the whole polyline from the real pose."""
    if (not isinstance(path_next, (tuple, list)) or not 1 <= len(path_next) <= MOVE_PATH_SEGMENTS_MAX - 1
            or any(not isinstance(s, (tuple, list)) or len(s) != 2
                   or any(isinstance(v, bool) or not isinstance(v, int) or not TILE_MIN <= v <= TILE_MAX for v in s)
                   for s in path_next)):
        raise KesalahanPerintah(f"path_next harus 1..{MOVE_PATH_SEGMENTS_MAX - 1} tile [x, y], dapat {path_next!r}")
    horizontal = arah in ("left", "right")
    previous, total = (int(stop[0]), int(stop[1])), 0
    for raw in path_next:
        tile = (int(raw[0]), int(raw[1]))
        dx, dy = tile[0] - previous[0], tile[1] - previous[1]
        turn_ok = (dx == 0 and dy != 0) if horizontal else (dy == 0 and dx != 0)
        length = abs(dx) + abs(dy)
        total += length
        if not turn_ok or length > MOVE_SEGMENT_TILES_MAX or total > MOVE_PATH_TILES_MAX:
            raise KesalahanPerintah(f"path_next bukan jalur belok 90 derajat yang terbatas: {path_next!r}")
        horizontal, previous = not horizontal, tile
    return tuple((int(s[0]), int(s[1])) for s in path_next)


def _tulis_json_atomik(dirpath: Path, nama: str, isi: dict) -> None:
    target = dirpath / nama
    try:
        ipc.write_json(target, isi, create_only=nama in (NAMA_REQUEST, NAMA_STOP), fsync=False)
    except OSError as exc:
        raise KesalahanPerintah(f"gagal menulis {target}: {exc}") from exc


def _tulis_permintaan(dirpath: Path, isi: dict) -> None:
    native = _baca_json(dirpath / NAMA_SESSION) or {}
    binding = dict(isi, runtime_instance_id=isi.get("runtime_instance_id", native.get("runtime_instance_id")),
                   expected_screen_id=isi.get("expected_screen_id", native.get("screen_id")))
    # A failed publication can be ambiguous: record the attempt before writing,
    # so it can never regain the before-first-publication idle shortcut.
    if (key := _request_binding(binding)) is not None:
        _written_requests.setdefault(key, set()).add(isi["request_id"])
    W.note('sent', isi['request_id'], isi.get('op') not in (OP_INSPECT_TILES, OP_INSPECT_CONTEXT))
    _tulis_json_atomik(dirpath, NAMA_REQUEST, isi)


def tulis_kontrol(
    kind: str,
    *,
    owner_id: str,
    generation: int,
    control_seq: int,
    folder: str | os.PathLike[str] | None = None,
    sesi: SesiPerintah | None = None,
    runtime_instance_id: str | None = None,
    session_id: str | None = None,
    expected_player_id: int | None = None,
    expected_player_name: str | None = None,
    expected_screen_id: int | None = None,
    lease_ms: int | None = None,
) -> None:
    if kind not in (KIND_ACQUIRE, KIND_RENEW, KIND_RELEASE):
        raise KesalahanPerintah(f"kind control harus acquire|renew|release, dapat {kind!r}")
    # fix54I: an ACQUIRE/RENEW may carry a measured lease length inside the native bounds (control.schema.json).
    if lease_ms is not None and (kind == KIND_RELEASE or isinstance(lease_ms, bool) or not isinstance(lease_ms, int)
                                 or not LEASE_DURATION_MS <= lease_ms <= LEASE_MAX_MS):
        raise KesalahanPerintah(f"lease_ms harus integer {LEASE_DURATION_MS}..{LEASE_MAX_MS} untuk acquire|renew")
    dirpath = folder_perintah(folder)
    if sesi is None:
        sesi = baca_sesi(dirpath)
    runtime = runtime_instance_id or str(sesi.mentah.get("runtime_instance_id") or "")
    if not runtime:
        raise KesalahanPerintah("runtime_instance_id wajib untuk control.json")
    isi: dict = {
        "protocol": PROTOCOL,
        "kind": kind,
        "runtime_instance_id": runtime,
        "session_id": session_id or sesi.session_id,
        "owner_id": owner_id,
        "generation": generation,
        "control_seq": control_seq,
    }
    if expected_player_id is not None:
        isi["expected_player_id"] = expected_player_id
    elif sesi.player_id:
        isi["expected_player_id"] = sesi.player_id
    if expected_player_name is not None:
        isi["expected_player_name"] = expected_player_name
    elif sesi.player_name:
        isi["expected_player_name"] = sesi.player_name
    if expected_screen_id is not None:
        isi["expected_screen_id"] = expected_screen_id
    if lease_ms is not None and lease_ms != LEASE_DURATION_MS:
        isi["lease_ms"] = lease_ms
    _tulis_json_atomik(dirpath, NAMA_CONTROL, isi)


def tulis_stop(
    *,
    generation: int,
    folder: str | os.PathLike[str] | None = None,
    sesi: SesiPerintah | None = None,
    owner_id: str = "",
    control_seq: int = 0,
    runtime_instance_id: str | None = None,
    session_id: str | None = None,
    cycle_id: str | None = None,
    writer: str = "command.tulis_stop",
    interrupted_session: str | None = None,
) -> None:
    """STOP-write-only (WP P1, R0-03 §9): an existing STOP is retained, never archived here; only the operator archives
    it through tools/arti_rearm. ``interrupted_session`` is accepted for callers and authorises nothing."""
    from arti_stardew_stop import provenance, read_stop, scoped, StopRefused
    dirpath = folder_perintah(folder)
    if sesi is None:
        sesi = baca_sesi(dirpath)
    runtime = runtime_instance_id or str(sesi.mentah.get("runtime_instance_id") or "")
    if not runtime:
        raise KesalahanPerintah("runtime_instance_id wajib untuk stop.json")
    if not (session_id or sesi.session_id):
        raise KesalahanPerintah("session_id wajib untuk stop.json")
    isi: dict = {
        "protocol": PROTOCOL,
        "runtime_instance_id": runtime,
        "session_id": session_id or sesi.session_id,
        "owner_id": owner_id,
        "generation": generation,
        "cycle_id": cycle_id,
        **provenance(writer),
    }
    if control_seq:
        isi["control_seq"] = control_seq
    # Unknown or foreign records are never overwritten, archived or implicitly acknowledged.
    try:
        raw, previous = read_stop(dirpath / NAMA_STOP)
        if raw is not None:
            if not scoped(previous, native=True):
                raise StopRefused("STOP scope unknown; operator must inspect it (archive only via tools/arti_rearm)")
            if (previous["runtime_instance_id"], previous["session_id"]) == (runtime, isi["session_id"]):
                return  # the existing STOP already halts this session; retain it
            raise StopRefused("foreign STOP retained; operator must inspect it (archive only via tools/arti_rearm)")
        # Atomic exclusive publication: a racing STOP wins, without partial JSON.
        _tulis_json_atomik(dirpath, NAMA_STOP, isi)
    except StopRefused as exc:
        raise KesalahanPerintah(str(exc)) from exc


def interaction_kind(envelope: dict) -> str | None:
    """world_interact kind of a result envelope (echoed input_args, else the result field).

    No kind anywhere is the pre-WS3a chest receipt; a present but unknown kind is never a success."""
    args = envelope.get("input_args")
    kind = args.get("kind") if isinstance(args, dict) else None
    if kind is None:
        kind = envelope.get("kind")
    if kind is None:
        return "chest"
    return kind if kind in WORLD_INTERACT_KINDS else None


# WP P1 typed-result reader (R0-07 C2, §5, §13.2; SCH-11). A `v: arti.verb_result/3` result is read by `executed` /
# `not_executed`; any other result.json is the legacy MenuStepCommandResult (decoder path "legacy"), whose
# `reject_reason` maps through refusals.json native_map. Unknown code = None (defect-unclassified), never a success.
# ADR-19 item 2 closed by ADR-17/A3: mapped legacy pre-edge refusals are complete v3 envelopes.
# Malformed identities and unmapped forms retain the legacy native-map fallback; executed legacy results are unchanged.
VERB_RESULT_V3 = "arti.verb_result/3"
REFUSALS_PATH = Path(__file__).resolve().parent / "contracts" / "refusals.json"


@functools.lru_cache(maxsize=1)
def _refusals() -> tuple[frozenset, dict]:
    try:
        doc = json.loads(REFUSALS_PATH.read_text(encoding="utf-8"))
        return frozenset(doc["codes"]), dict(doc["native_map"])
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset(), {}


def decoder_path(isi: dict) -> str:
    return "v3" if isi.get("v") == VERB_RESULT_V3 else "legacy"


def typed_refusal(isi: dict) -> dict | None:
    """The typed executed:false refusal of a result, or None (executed, a response-only replay, or not rejected)."""
    codes, native_map = _refusals()
    if decoder_path(isi) == "v3":
        if isi.get("executed") is not False:
            return None
        detail = isi.get("not_executed") if isinstance(isi.get("not_executed"), dict) else {}
        code, native = detail.get("reason"), detail.get("native_code")
        return dict(decoder_path="v3", code=code if code in codes else None, native_code=native,
                    detail=detail.get("detail"), cancel_reason=detail.get("cancel_reason"))
    if isi.get("status") != "rejected":
        return None
    native = isi.get("reject_reason")
    if native == "ArmRejected" and isi.get("arm_status") in native_map:
        native = isi["arm_status"]  # the pre-input background reason behind the arm refusal (N0_NOT_CONSUMED, ...)
    code = native_map.get(native) if isinstance(native, str) else None
    if code == "not_a_refusal":
        return None
    return dict(decoder_path="legacy", code=code if code in codes else None, native_code=native,
                detail=isi.get("arm_status"), cancel_reason=None)


def authenticated_no_input(isi, request_id, generation) -> bool:
    """A correlated native admission refusal; release/current-world reconciliation is separate."""
    refusal = typed_refusal(isi) if isinstance(isi, dict) else None
    return bool(refusal and refusal["decoder_path"] == "v3" and refusal["code"]
        and isi.get("request_id") == request_id and request_id
        and isi.get("generation") == generation and type(generation) is int
        # PM-1 fix4 #1: a post-admission debt refusal is admitted, and debts_after names exactly the refusing debt.
        and (isi.get("native_lifecycle") == "not_admitted" and isi.get("debts_after") == []
             or isi.get("native_lifecycle") == "admitted" and refusal["code"] == "debt_active"
             and [d.get("debt_id") if isinstance(d, dict) else None for d in isi.get("debts_after") or [None]]
             == [isi["not_executed"].get("debt_id")])
        and isi.get("executed") is False and isi.get("delivered") is False
        and type(isi.get("edges")) is int and isi["edges"] == 0
        and isinstance(isi.get("validity"), dict)
        and isi["validity"].get("phase_at_end") == "admission"
        and isi["validity"].get("ended_by") == "no_input"
        and type(isi.get("receiver_epoch_before")) is int
        and isi["receiver_epoch_before"] == isi.get("receiver_epoch_after") and isi.get("faults") == [])


def _hasil_dari_envelope(isi: dict, request_id: str) -> HasilPerintah:
    W.note('received', request_id)
    refusal = typed_refusal(isi)
    if decoder_path(isi) == "v3":
        # B1 sends legacy forms only; an executed v3 result has no B1 effect decoder, so it is never a success.
        alasan = (f"{refusal['code'] or 'defect-unclassified'}:{refusal['native_code']}" if refusal is not None
                  else "v3 executed result: no B1 effect decoder")
        return HasilPerintah(status=DITOLAK if refusal is not None else TIDAK_PASTI, sukses=False,
                             request_id=request_id, alasan=alasan, mentah=isi, refusal=refusal)
    status_mod = str(isi.get("status") or "")
    outcome = isi.get("outcome")
    outcome = str(outcome) if outcome else None
    op = str(isi.get("op") or OP_MENU_STEP)
    outcome_sukses = OUTCOME_SUKSES.get(op)
    primitive = isi.get("primitive")
    if op == OP_INPUT_ONCE and isinstance(primitive, str) and primitive:
        outcome_sukses = OUTCOME_SUKSES_PRIMITIVE.get(primitive)
        if primitive == "world_interact":
            outcome_sukses = OUTCOME_SUKSES_INTERACT.get(interaction_kind(isi))
        elif primitive == PRIMITIVE_PAD_BUTTON:
            args_echo = isi.get("input_args")
            outcome_sukses = OUTCOME_SUKSES_PAD_BUTTON.get(args_echo.get("button") if isinstance(args_echo, dict) else None)
            if outcome == OUTCOME_EVENT_SKIPPED and isinstance(args_echo, dict) and args_echo.get("button") == "Select":
                outcome_sukses = OUTCOME_EVENT_SKIPPED  # FIXB31: Select in a plain cutscene (mod EventSkipEnabled)
    sukses = (
        status_mod == "completed"
        and bool(isi.get("success"))
        and outcome_sukses is not None
        and outcome_matches(outcome, outcome_sukses)
    )

    if status_mod == "completed":
        status = SELESAI
        alasan = None if sukses else f"outcome={outcome}"
    elif status_mod == "rejected":
        status = DITOLAK
        alasan = str(isi.get("reject_reason") or "?")
        if isi.get("arm_status"):
            alasan += f" ({isi['arm_status']})"
    elif status_mod == "interrupted":
        status = TERPUTUS
        alasan = str(isi.get("reject_reason") or "sesi berakhir")
    elif status_mod == "uncertain":
        status = TIDAK_PASTI
        alasan = str(isi.get("reject_reason") or outcome or "journal menyimpan outcome tidak pasti")
        sukses = False
    else:
        status = TIDAK_PASTI
        alasan = f"status mod tidak dikenal: {status_mod!r}"
        sukses = False

    return HasilPerintah(
        status=status,
        sukses=sukses,
        request_id=request_id,
        outcome=outcome,
        alasan=alasan,
        mentah=isi,
        refusal=refusal,
    )


def _gagal_lokal_sebelum_tulis(
    dirpath: Path, sesi: SesiPerintah, request_id: str, op: str, *, primitive: str | None = None
) -> HasilPerintah | None:
    belum = sesi.alasan_belum_siap(op, primitive=primitive)
    if belum is not None:
        return HasilPerintah(
            status=GAGAL_LOKAL, sukses=False, request_id=request_id, alasan=belum
        )
    sisa = dirpath / NAMA_REQUEST
    if sisa.exists():
        return HasilPerintah(
            status=GAGAL_LOKAL,
            sukses=False,
            request_id=request_id,
            alasan=f"{sisa} masih ada; pakai bersihkan_slot() secara sadar dulu",
        )
    # The result mailbox is correlated by request ID. An unrelated poller must
    # never remove another caller's receipt while preparing its own request.
    return None


@W.span('native_roundtrip', 83, runtime=False)
def _tunggu_hasil(dirpath: Path, request_id: str, batas_detik: float) -> HasilPerintah:
    clock = result_wait_clock.get() or time.monotonic
    cancelled = result_wait_cancel.get()
    batas = clock() + batas_detik
    hasil_path = dirpath / NAMA_RESULT
    cut = False
    from arti_runtime.file_arrival import FileArrival
    with FileArrival(dirpath) as arrival:
        while clock() < batas:
            isi = _baca_json(hasil_path)
            if isi is not None and isi.get("request_id") == request_id:
                return _hasil_dari_envelope(isi, request_id)
            if cancelled is not None and not cut and cancelled():
                cut, batas = True, min(batas, clock() + CANCEL_WAIT_GRACE_S)
            W.call('poll', 84, arrival.wait, min(JEDA_POLL_DETIK, max(0., batas - clock())))
    if cut:
        return HasilPerintah(
            status=TIDAK_PASTI,
            sukses=False,
            request_id=request_id,
            alasan="cancelled_before_result; aksi mungkin terjadi. JANGAN ulangi otomatis",
        )

    # Neither an absent pathname nor a different pending publication proves
    # non-execution: the native consumer may own a private claim. Leave all
    # publications/receipts intact; timeout never authorizes replay or cleanup.
    return HasilPerintah(
        status=TIDAK_PASTI,
        sukses=False,
        request_id=request_id,
        alasan="hasil belum datang; aksi mungkin terjadi. JANGAN ulangi otomatis; periksa log SMAPI",
    )


def _journal_capable(sesi: SesiPerintah) -> bool:
    return (
        sesi.mentah.get("journal_supported") is True
        and sesi.mentah.get("journal_schema") == JOURNAL_SCHEMA
    )


def _b64_teks(value: object) -> str:
    teks = "" if value is None else str(value)
    return base64.b64encode(teks.encode("utf-8")).decode("ascii")


def _journal_namespace_text(payload: dict) -> str:
    return "\n".join(
        (
            "journal-namespace-v1",
            f"runtime={_b64_teks(payload.get('runtime_instance_id', ''))}",
            f"session={_b64_teks(payload.get('session_id', ''))}",
            f"owner={_b64_teks(payload.get('owner_id', ''))}",
            f"generation={int(payload.get('generation') or 0)}",
        )
    )


def _journal_payload_digest(payload: dict) -> str:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    canonical = "\n".join(
        (
            "journal-payload-v1",
            "protocol=1",
            f"op={_b64_teks(payload.get('op', ''))}",
            f"runtime={_b64_teks(payload.get('runtime_instance_id', ''))}",
            f"session={_b64_teks(payload.get('session_id', ''))}",
            f"owner={_b64_teks(payload.get('owner_id', ''))}",
            f"generation={int(payload.get('generation') or 0)}",
            f"control_seq={int(payload.get('control_seq') or 0)}",
            f"context_generation={int(payload.get('context_generation') or 0)}",
            f"expected_player_id={int(payload.get('expected_player_id') or 0)}",
            f"expected_player_name={_b64_teks(payload.get('expected_player_name', ''))}",
            f"expected_screen_id={int(payload.get('expected_screen_id') or 0)}",
            f"expected_location_id={_b64_teks(payload.get('expected_location_id', ''))}",
            f"deadline_unix_ms={int(payload.get('deadline_unix_ms') or 0)}",
            f"family={_b64_teks(payload.get('family', ''))}",
            f"primitive={_b64_teks(payload.get('primitive', ''))}",
            f"direction={_b64_teks(args.get('direction', payload.get('direction', '')))}",
            f"ticks={1 if payload.get('primitive') in ('select_item', 'world_interact', 'container_transfer', 'container_exit', 'ui_key', PRIMITIVE_PAD_BUTTON, PRIMITIVE_UI_NAV, PRIMITIVE_UI_PRESS, PRIMITIVE_CHAT_SEND, PRIMITIVE_REFLEX_FISH, PRIMITIVE_REFLEX_COMBAT, PRIMITIVE_REFLEX_CHARGE) else int(args.get('ticks', payload.get('ticks', 0)) or 0)}",
            f"tile_x={int(payload.get('tile_x') or 0)}",
            f"tile_y={int(payload.get('tile_y') or 0)}",
        )
    )
    if "execution_deadline_unix_ms" in payload:
        canonical += "\nexecution_deadline_unix_ms=" + str(payload["execution_deadline_unix_ms"])
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_MOVE_CARDINAL and "event_token" in args:
        canonical += "\nevent_token=" + _b64_teks(args["event_token"])
    if (payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_MOVE_CARDINAL
            and isinstance(args.get("segment_stop"), list)):
        # fix89D / ADR 158: mirrors CommandSettledJournal.CanonicalPayloadDigest.
        canonical += f"\nsegment_stop_x={int(args['segment_stop'][0])}\nsegment_stop_y={int(args['segment_stop'][1])}"
        if isinstance(args.get("path_next"), list):
            # fix92A / ADR 162: mirrors MovePathStops.Canonical in CommandSettledJournal.CanonicalPayloadDigest.
            canonical += "\npath_next=" + ";".join(f"{int(s[0])},{int(s[1])}" for s in args["path_next"])
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_USE_TOOL:
        canonical += "\n" + "\n".join((
            f"tool_slot={args['slot']}",
            f"tool_item={_b64_teks(args['qualified_item_id'])}",
            f"tool_x={args['tile_x']}", f"tool_y={args['tile_y']}",
            f"tool_facing={args['facing']}",
        ))
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == "select_item":
        canonical += "\n" + "\n".join((
            f"select_receiver={_b64_teks(args['receiver_token'])}",
            f"select_target={_b64_teks(args['target_token'])}", f"select_slot={args['slot']}",
            f"select_item={_b64_teks(args['qualified_item_id'])}",
            f"select_stack={args['stack']}", f"select_quality={args['quality']}",
        ))
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == "container_transfer":
        fields=(("receiver","receiver_token"),("container","container_token"),("readback","readback_token"),
                ("source","source_fact_id"),("target","target_token"),("slot","slot"),
                ("item","qualified_item_id"),("stack","stack"),("quality","quality"),("ceiling","owner_ceiling"))
        if "quantity" in args: fields+=(("quantity","quantity"),)
        # WS-G2: a mode request (quick / take_out / ship_one / ship_stack) adds transfer_mode; v1 unchanged.
        if "mode" in args: fields+=(("mode","mode"),)
        canonical +="\n" + "\n".join("transfer_"+label+"="+(_b64_teks(args[key]) if type(args[key]) is str else str(args[key]))
                                      for label,key in fields)
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == "world_interact":
        canonical += "\n" + "\n".join((
            f"interact_target={_b64_teks(args['target_token'])}",
            f"interact_source={_b64_teks(args['source_fact_id'])}",
            f"interact_x={args['tile_x']}", f"interact_y={args['tile_y']}",
            f"interact_actor_x={args['actor_tile_x']}", f"interact_actor_y={args['actor_tile_y']}",
            f"interact_facing={args['facing']}",
            f"interact_kind={_b64_teks(args['kind'])}",
        ))
        if "target_location_id" in args:
            canonical += f"\ninteract_expected={_b64_teks(args['target_location_id'])}"
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_MOVE_THROUGH:
        canonical += "\n" + "\n".join((
            f"through_kind={_b64_teks(args['kind'])}",
            f"through_source={_b64_teks(args['source_fact_id'])}",
            f"through_target={_b64_teks(args['target_token'])}",
            f"through_x={args['tile_x']}", f"through_y={args['tile_y']}",
            f"through_actor_x={args['actor_tile_x']}", f"through_actor_y={args['actor_tile_y']}",
        ))
        if "target_location_id" in args:
            canonical += f"\nthrough_expected={_b64_teks(args['target_location_id'])}"
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == "container_exit":
        fields=(("receiver","receiver_token"),("container","container_token"),("source","source_fact_id"),
                ("readback","readback_token"),("target","target_token"))
        canonical += "\n" + "\n".join("exit_"+label+"="+_b64_teks(args[key]) for label,key in fields)
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_PAD_BUTTON:
        # WS-G0: same canonical line as the mod CommandSettledJournal (shared vector in the tests).
        canonical += "\n" + f"padbutton_button={_b64_teks(args['button'])}"
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_CHAT_SEND:
        # WS-G3: same canonical line as the mod CommandSettledJournal (shared vector in both suites).
        canonical += "\n" + f"chatsend_text={_b64_teks(args['text'])}"
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_UI_NAV:
        # WS-G1: same canonical lines as the mod CommandSettledJournal (shared vectors in both suites).
        canonical += "\n" + "\n".join((
            f"uinav_receiver={_b64_teks(args['receiver_token'])}",
            f"uinav_menu={_b64_teks(args['menu_type'])}",
            f"uinav_context={_b64_teks(args['context'])}",
            f"uinav_target={_b64_teks(args['target_token'])}",
            f"uinav_dir={_b64_teks(args['dir'])}",
        ))
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_UI_PRESS:
        reserved = ";".join(f"{k}={_reserved_text(args[k])}" for k in sorted(args) if k in UI_PAD_RESERVED_ARGS)
        canonical += "\n" + "\n".join((
            f"uipress_receiver={_b64_teks(args['receiver_token'])}",
            f"uipress_menu={_b64_teks(args['menu_type'])}",
            f"uipress_context={_b64_teks(args['context'])}",
            f"uipress_target={_b64_teks(args['target_token'])}",
            f"uipress_btn={_b64_teks(args['btn'])}",
            f"uipress_snapped={args['snapped_id']}",
            f"uipress_tab={_b64_teks(args.get('tab', ''))}",
            f"uipress_reserved={_b64_teks(reserved)}",
        ))
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_REFLEX_FISH:
        # WS-G4: same canonical lines as the mod CommandSettledJournal (shared vector in the tests).
        canonical += "\n" + "\n".join((
            f"reflex_max_s={int(args['max_s'])}",
            f"reflex_cast_permille={_permille(args['cast_power'])}",
            f"reflex_treasure={1 if args['treasure'] is True else 0}",
            f"reflex_hook_retries={int(args['hook_retries'])}",
        ))
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_REFLEX_COMBAT:
        # WS-G5: same canonical lines as the mod CommandSettledJournal (shared vector in the tests).
        canonical += "\n" + "\n".join((
            f"reflex_max_s={int(args['max_s'])}",
            f"reflex_hp_floor_pct={int(args['hp_floor_pct'])}",
            f"reflex_max_kills={int(args['max_kills'])}",
            f"reflex_retreat={1 if args['retreat'] is True else 0}",
        ))
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_REFLEX_CHARGE:
        canonical += "\n" + "\n".join((
            f"reflex_target_level={int(args['target_level'])}",
            f"reflex_max_ticks={int(args['max_ticks'])}",
        ))
    if payload.get("op") == OP_INPUT_ONCE and payload.get("primitive") == PRIMITIVE_UI_KEY:
        canonical += "\n" + "\n".join((
            f"uikey_receiver={_b64_teks(args['receiver_token'])}",
            f"uikey_menu={_b64_teks(args['menu_type'])}",
            f"uikey_kind={_b64_teks(args['kind'])}",
            f"uikey_key={_b64_teks(args['key'])}",
            f"uikey_target={_b64_teks(args['target_token'])}",
            f"uikey_option={args.get('option_index', -1)}",
            f"uikey_profession={args.get('profession_id', -1)}",
        ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _next_request_seq(payload: dict) -> int:
    key = (
        str(payload.get("runtime_instance_id") or ""),
        str(payload.get("session_id") or ""),
        str(payload.get("owner_id") or ""),
        int(payload.get("generation") or 0),
    )
    candidate = time.time_ns()
    terakhir = _request_seq_last.get(key, 0)
    seq = max(candidate, terakhir + 1)
    if seq > 9_223_372_036_854_775_807:
        raise KesalahanPerintah("request_seq melewati int64")
    _request_seq_last[key] = seq
    return seq


def _journalize_payload(payload: dict) -> dict:
    isi = dict(payload)
    seq = _next_request_seq(isi)
    isi["request_seq"] = seq
    digest = _journal_payload_digest(isi)
    identity = (
        _journal_namespace_text(isi)
        + f"\nrequest_seq={seq}\npayload_sha256={digest}"
    )
    isi["request_id"] = JOURNAL_REQUEST_PREFIX + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()
    return isi


def minta_menu_step(
    arah: str,
    *,
    folder: str | os.PathLike[str] | None = None,
    timeout_ticks: int = TIMEOUT_TICKS_DEFAULT,
    delay_ticks: int = 0,
    admisi_detik: float = ADMISI_DETIK_DEFAULT,
    batas_detik: float | None = None,
    sesi: SesiPerintah | None = None,
) -> HasilPerintah:
    dirpath = folder_perintah(folder)
    arah_bersih = _validasi_argumen(arah, timeout_ticks, delay_ticks)
    if sesi is None:
        sesi = baca_sesi(dirpath)
    request_id = uuid.uuid4().hex
    gagal = _gagal_lokal_sebelum_tulis(dirpath, sesi, request_id, OP_MENU_STEP)
    if gagal is not None:
        return gagal
    deadline_ms = _sekarang_ms() + int(admisi_detik * 1000)
    _tulis_permintaan(
        dirpath,
        {
            "protocol": PROTOCOL,
            "op": OP_MENU_STEP,
            "request_id": request_id,
            "session_id": sesi.session_id,
            "expected_player_id": sesi.player_id,
            "expected_player_name": sesi.player_name,
            "direction": arah_bersih,
            "timeout_ticks": timeout_ticks,
            "delay_ticks": delay_ticks,
            "deadline_unix_ms": deadline_ms,
        },
    )
    if batas_detik is None:
        batas_detik = admisi_detik + (timeout_ticks / 60.0) + 3.0
    return _tunggu_hasil(dirpath, request_id, batas_detik)


def minta_water_once(
    tile_x: int,
    tile_y: int,
    *,
    folder: str | os.PathLike[str] | None = None,
    admisi_detik: float = ADMISI_DETIK_DEFAULT,
    batas_detik: float | None = None,
    sesi: SesiPerintah | None = None,
) -> HasilPerintah:
    dirpath = folder_perintah(folder)
    x = _validasi_ubin(tile_x, "tile_x")
    y = _validasi_ubin(tile_y, "tile_y")
    if sesi is None:
        sesi = baca_sesi(dirpath)
    request_id = uuid.uuid4().hex
    gagal = _gagal_lokal_sebelum_tulis(dirpath, sesi, request_id, OP_WATER_ONCE)
    if gagal is not None:
        return gagal
    deadline_ms = _sekarang_ms() + int(admisi_detik * 1000)
    _tulis_permintaan(
        dirpath,
        {
            "protocol": PROTOCOL,
            "op": OP_WATER_ONCE,
            "request_id": request_id,
            "session_id": sesi.session_id,
            "expected_player_id": sesi.player_id,
            "expected_player_name": sesi.player_name,
            "tile_x": x,
            "tile_y": y,
            "deadline_unix_ms": deadline_ms,
        },
    )
    if batas_detik is None:
        batas_detik = admisi_detik + (WATER_ONCE_MAX_TICKS / 60.0) + 3.0
    return _tunggu_hasil(dirpath, request_id, batas_detik)


def _validasi_leased_common(
    *,
    sesi: SesiPerintah,
    owner_id: object,
    generation: object,
    control_seq: object,
    context_generation: object,
    runtime_instance_id: object | None,
    expected_location_id: object | None,
    expected_screen_id: object | None,
    admisi_detik: object,
    request_id: str,
    allow_unnamed_player: bool = False,
) -> tuple[HasilPerintah | None, str, str, int, int]:
    def gagal(alasan: str) -> tuple[HasilPerintah, str, str, int, int]:
        return (
            HasilPerintah(GAGAL_LOKAL, False, request_id, alasan=alasan),
            "",
            "",
            0,
            0,
        )

    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 128:
        return gagal(f"owner_id wajib string non-kosong <= 128, dapat {owner_id!r}")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        return gagal(f"generation lease wajib integer >= 1, dapat {generation!r}")
    if isinstance(control_seq, bool) or not isinstance(control_seq, int) or control_seq < 1:
        return gagal(f"control_seq wajib integer >= 1, dapat {control_seq!r}")
    if (
        isinstance(context_generation, bool)
        or not isinstance(context_generation, int)
        or context_generation < 1
    ):
        return gagal(
            f"context_generation wajib integer >= 1, dapat {context_generation!r}"
        )
    runtime = (
        runtime_instance_id
        if runtime_instance_id is not None
        else sesi.mentah.get("runtime_instance_id")
    )
    if not isinstance(runtime, str) or not runtime.strip() or len(runtime) > 128:
        return gagal("runtime_instance_id wajib string non-kosong <= 128")
    if not sesi.session_id or len(sesi.session_id) > 128:
        return gagal("session_id current wajib string non-kosong <= 128")
    if isinstance(sesi.player_id, bool) or not isinstance(sesi.player_id, int) or sesi.player_id == 0:
        return gagal("player_id current wajib integer non-zero")
    # Live7: a brand-new farmhand has no name until ARTI types one in the character menu (Cabin.CreateFarmhand builds it
    # with name "", decomp Cabin.cs:46-66); only that menu's char_custom ui_press may run with an empty name. The mod skips
    # an empty expected_player_name (MenuStepCommandInbox.TryValidateInputOnceCurrentBinding); every other pin stays.
    if not isinstance(sesi.player_name, str) or (not sesi.player_name.strip()
                                                 and not (allow_unnamed_player and sesi.player_name == "")):
        return gagal("player_name current wajib string non-kosong")
    try:
        lokasi = _validasi_location_id(
            expected_location_id if expected_location_id is not None else sesi.location_id
        )
    except KesalahanPerintah as exc:
        return gagal(str(exc))
    layar = sesi.screen_id if expected_screen_id is None else expected_screen_id
    if isinstance(layar, bool) or not isinstance(layar, int) or layar < 0:
        return gagal(f"expected_screen_id harus integer >= 0, dapat {layar!r}")
    if (
        isinstance(admisi_detik, bool)
        or not isinstance(admisi_detik, (int, float))
        or not math.isfinite(admisi_detik)
        or admisi_detik <= 0
    ):
        return gagal(f"admisi_detik harus angka finite > 0, dapat {admisi_detik!r}")
    return None, runtime.strip(), lokasi, layar, int(admisi_detik * 1000)


def minta_water_once_leased(
    tile_x: int,
    tile_y: int,
    *,
    owner_id: str,
    generation: int,
    control_seq: int,
    context_generation: int,
    folder: str | os.PathLike[str] | None = None,
    admisi_detik: float = ADMISI_DETIK_DEFAULT,
    batas_detik: float | None = None,
    sesi: SesiPerintah | None = None,
    runtime_instance_id: str | None = None,
    expected_location_id: str | None = None,
    expected_screen_id: int | None = None,
    guarded_publish: Callable[[Callable[[], None]], None] | None = None,
) -> HasilPerintah:
    """Optional guard must synchronously publish once or raise; waiting is unguarded."""
    dirpath = folder_perintah(folder)
    x = _validasi_ubin(tile_x, "tile_x")
    y = _validasi_ubin(tile_y, "tile_y")
    if sesi is None:
        sesi = baca_sesi(dirpath)
    provisional_id = uuid.uuid4().hex
    gagal_common, runtime, lokasi, layar, delta_ms = _validasi_leased_common(
        sesi=sesi,
        owner_id=owner_id,
        generation=generation,
        control_seq=control_seq,
        context_generation=context_generation,
        runtime_instance_id=runtime_instance_id,
        expected_location_id=expected_location_id,
        expected_screen_id=expected_screen_id,
        admisi_detik=admisi_detik,
        request_id=provisional_id,
    )
    if gagal_common is not None:
        return gagal_common
    now_ms = _sekarang_ms()
    deadline_ms = now_ms + delta_ms
    if deadline_ms > 9_223_372_036_854_775_807:
        return HasilPerintah(
            GAGAL_LOKAL,
            False,
            provisional_id,
            alasan=f"admisi_detik menghasilkan deadline tidak sah: {admisi_detik!r}",
        )
    payload: dict = {
        "protocol": PROTOCOL,
        "op": OP_WATER_ONCE,
        "request_id": provisional_id,
        "runtime_instance_id": runtime,
        "session_id": sesi.session_id,
        "owner_id": owner_id.strip(),
        "generation": generation,
        "control_seq": control_seq,
        "context_generation": context_generation,
        "expected_player_id": sesi.player_id,
        "expected_player_name": sesi.player_name,
        "expected_screen_id": layar,
        "expected_location_id": lokasi,
        "tile_x": x,
        "tile_y": y,
        "deadline_unix_ms": deadline_ms,
    }
    if _journal_capable(sesi):
        payload = _journalize_payload(payload)
    request_id = str(payload["request_id"])
    gagal = _gagal_lokal_sebelum_tulis(dirpath, sesi, request_id, OP_WATER_ONCE)
    if gagal is not None:
        return gagal
    if guarded_publish is None:
        _tulis_permintaan(dirpath, payload)
    else:
        guarded_publish(lambda: _tulis_permintaan(dirpath, payload))
    if batas_detik is None:
        batas_detik = admisi_detik + (WATER_ONCE_MAX_TICKS / 60.0) + 3.0
    hasil = _tunggu_hasil(dirpath, request_id, batas_detik)
    post_admission_ambigu = hasil.status == TERPUTUS or (
        hasil.status == SELESAI
        and not hasil.sukses
        and hasil.mentah is not None
        and bool(hasil.mentah.get("accepted"))
    )
    if not post_admission_ambigu:
        return hasil
    return HasilPerintah(
        status=TIDAK_PASTI,
        sukses=False,
        request_id=request_id,
        outcome=hasil.outcome,
        alasan=(
            f"{hasil.alasan or 'water_once leased terputus'}; request leased sudah "
            "diambil/admitted dan native tidak membuktikan nol world effect. "
            "JANGAN ulangi otomatis — rekonsiliasi observasi dulu"
        ),
        mentah=hasil.mentah,
    )


def minta_world_press(
    arah: str,
    ticks: int,
    *,
    folder: str | os.PathLike[str] | None = None,
    admisi_detik: float = ADMISI_DETIK_DEFAULT,
    batas_detik: float | None = None,
    sesi: SesiPerintah | None = None,
) -> HasilPerintah:
    dirpath = folder_perintah(folder)
    arah_bersih, n = _validasi_world_press(arah, ticks)
    if sesi is None:
        sesi = baca_sesi(dirpath)
    request_id = uuid.uuid4().hex
    gagal = _gagal_lokal_sebelum_tulis(dirpath, sesi, request_id, OP_WORLD_PRESS)
    if gagal is not None:
        return gagal
    deadline_ms = _sekarang_ms() + int(admisi_detik * 1000)
    _tulis_permintaan(
        dirpath,
        {
            "protocol": PROTOCOL,
            "op": OP_WORLD_PRESS,
            "request_id": request_id,
            "session_id": sesi.session_id,
            "expected_player_id": sesi.player_id,
            "expected_player_name": sesi.player_name,
            "direction": arah_bersih,
            "ticks": n,
            "deadline_unix_ms": deadline_ms,
        },
    )
    if batas_detik is None:
        batas_detik = admisi_detik + (n / 60.0) + 3.0
    return _tunggu_hasil(dirpath, request_id, batas_detik)


def minta_input_once(
    arah: str, ticks: int, **kwargs,
) -> HasilPerintah:
    return _minta_input_once(arah, ticks, **kwargs)


def minta_use_tool_once(
    ticks: int, *, slot: int, qualified_item_id: str, tile_x: int, tile_y: int,
    facing: int, **kwargs,
) -> HasilPerintah:
    """One configured equipped-tool input; result is input lifecycle, not a world oracle."""
    target = dict(slot=slot, qualified_item_id=qualified_item_id,
                  tile_x=tile_x, tile_y=tile_y, facing=facing)
    validate_use_tool_args(dict(target, ticks=ticks))
    return _minta_input_once("", ticks, _tool_target=target, **kwargs)


def minta_select_item(*, receiver_token: str, target_token: str, slot: int,
                      qualified_item_id: str, stack: int, quality: int, **kwargs) -> HasilPerintah:
    target = dict(receiver_token=receiver_token, target_token=target_token, slot=slot,
                  qualified_item_id=qualified_item_id, stack=stack, quality=quality)
    validate_select_item_args(target)
    return _minta_input_once("", 1, _selection_target=target, **kwargs)


def validate_select_item_args(args: dict) -> None:
    if set(args) != {"receiver_token", "target_token", "slot", "qualified_item_id", "stack", "quality"}:
        raise KesalahanPerintah("invalid_select_item_args")
    for key in ("receiver_token", "target_token"):
        if not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > 128:
            raise KesalahanPerintah("invalid_select_item_" + key)
    for key, low, high in (("slot", 0, 47), ("stack", 0, 999999), ("quality", 0, 4)):
        if type(args[key]) is not int or not low <= args[key] <= high:
            raise KesalahanPerintah("invalid_select_item_" + key)
    if args["quality"] not in (0, 1, 2, 4):
        raise KesalahanPerintah("invalid_select_item_quality")
    qid = args["qualified_item_id"]
    if (not isinstance(qid, str) or len(qid) > 128
            or (args["stack"] == 0 and (qid != "" or args["quality"] != 0))
            or (args["stack"] > 0 and not qid.strip())):
        raise KesalahanPerintah("invalid_select_item_qualified_item_id")


def validate_container_exit_args(args: dict) -> None:
    fields={"receiver_token":128,"container_token":128,"source_fact_id":256,"readback_token":128,"target_token":128}
    if not isinstance(args,dict) or set(args)!=set(fields):
        raise KesalahanPerintah("invalid_container_exit_args")
    if any(not isinstance(args[k],str) or not args[k].strip() or len(args[k])>n for k,n in fields.items()):
        raise KesalahanPerintah("invalid_container_exit_pin")


def minta_container_exit(*,receiver_token,container_token,source_fact_id,readback_token,target_token,**kwargs):
    target=dict(receiver_token=receiver_token,container_token=container_token,source_fact_id=source_fact_id,
                readback_token=readback_token,target_token=target_token)
    validate_container_exit_args(target)
    return _minta_input_once("",1,_exit_target=target,**kwargs)


def ui_key_arg_keys(key: str) -> tuple[str, ...]:
    """Exact arg key set per ui_key key (mod UiKeyTable.ArgKeys)."""
    if key == "answer":
        return UI_KEY_BASE_ARGS + ("option_index",)
    if key == "choose":
        return UI_KEY_BASE_ARGS + ("option_index", "profession_id")
    return UI_KEY_BASE_ARGS


def ui_key_token_shape(token: object) -> bool:
    return (isinstance(token, str) and len(token) == 35 and token.startswith("uk:")
            and all(c in "0123456789abcdef" for c in token[3:]))


def validate_ui_key_args(args: dict) -> None:
    """Exact key sets per key, bounded strings, rcv:/uk: shapes and the mod's kind/key table."""
    if not isinstance(args, dict) or not isinstance(args.get("key"), str):
        raise KesalahanPerintah("invalid_ui_key_args")
    if set(args) != set(ui_key_arg_keys(args["key"])):
        raise KesalahanPerintah("invalid_ui_key_args")
    for key in UI_KEY_BASE_ARGS:
        value = args[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise KesalahanPerintah("invalid_ui_key_" + key)
    if not args["receiver_token"].startswith("rcv:"):
        raise KesalahanPerintah("invalid_ui_key_receiver_token")
    if not ui_key_token_shape(args["target_token"]):
        raise KesalahanPerintah("invalid_ui_key_target_token")
    menu, keys = UI_KEY_TABLE.get(args["kind"], (None, ()))
    if menu != args["menu_type"] or args["key"] not in keys:
        raise KesalahanPerintah("ui_key_outside_table")
    if "option_index" in args:
        option = args["option_index"]
        high = 1 if args["key"] == "choose" else UI_KEY_MAX_OPTIONS - 1
        if type(option) is not int or not 0 <= option <= high:
            raise KesalahanPerintah("invalid_ui_key_option_index")
    if "profession_id" in args:
        profession = args["profession_id"]
        if type(profession) is not int or not 0 <= profession <= 29:
            raise KesalahanPerintah("invalid_ui_key_profession_id")


def minta_ui_key(*, receiver_token, menu_type, kind, key, target_token, option_index=None,
                 profession_id=None, **kwargs) -> HasilPerintah:
    """One direct menu-method call in the mod (WS2). Result is the mod's ui_key verdict."""
    target = dict(receiver_token=receiver_token, menu_type=menu_type, kind=kind, key=key, target_token=target_token)
    if option_index is not None:
        target["option_index"] = option_index
    if profession_id is not None:
        target["profession_id"] = profession_id
    validate_ui_key_args(target)
    return _minta_input_once("", 1, _ui_key_target=target, **kwargs)


def _reserved_text(value: object) -> str:
    """Mirror of the mod's reserved-arg canonical scalar (bool -> true/false, int -> decimal, str as is)."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def ui_pad_token_shape(token: object) -> bool:
    return (isinstance(token, str) and len(token) == 35 and token.startswith("up:")
            and all(c in "0123456789abcdef" for c in token[3:]))


def _validate_ui_pad_base(args: dict, prefix: str) -> None:
    for key in UI_PAD_BASE_ARGS:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise KesalahanPerintah(f"invalid_{prefix}_{key}")
    if not args["receiver_token"].startswith("rcv:"):
        raise KesalahanPerintah(f"invalid_{prefix}_receiver_token")
    if not ui_pad_token_shape(args["target_token"]):
        raise KesalahanPerintah(f"invalid_{prefix}_target_token")
    if args["context"] not in UI_PAD_CONTEXTS:
        raise KesalahanPerintah(f"invalid_{prefix}_context")


def validate_ui_nav_args(args: dict) -> None:
    """Mirror of the mod UiNavTarget.TryParse: exactly the binding pins + dir (up/right/down/left)."""
    if not isinstance(args, dict) or set(args) != set(UI_PAD_BASE_ARGS) | {"dir"}:
        raise KesalahanPerintah("invalid_ui_nav_args")
    _validate_ui_pad_base(args, "ui_nav")
    if args["dir"] not in UI_NAV_DIRS:
        raise KesalahanPerintah("invalid_ui_nav_dir")


def name_refusal(text: object, *, min_length: int = 1, text_limit: int = -1) -> str | None:
    """Mirror of the mod UiG3Policy.NameRefusal (owner-supplied names only, never model free text)."""
    if not isinstance(text, str) or not text:
        return "text_required"
    if len(text) > UI_G3_NAME_MAX or (text_limit > 0 and len(text) > text_limit):
        return "text_too_long"
    if len(text) < max(1, min_length):
        return "text_too_short"
    if text.strip() != text or "  " in text:
        return "text_whitespace"
    if not all(c.isalnum() or c in " '-." for c in text):
        return "text_charset"
    return None


def chat_refusal(text: object) -> str | None:
    """Mirror of the mod UiG3Policy.ChatRefusal: plain text, never a "/" command (owner D5)."""
    if not isinstance(text, str) or not text:
        return "text_required"
    if len(text) > CHAT_MAX:
        return "text_too_long"
    if text.strip() != text:
        return "text_whitespace"
    if text[0] == "/":
        return "chat_command_denied"
    if any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        return "text_charset"
    return None


def _validate_ui_press_reserved(args: dict) -> None:
    reserved = [k for k in args if k in UI_PAD_RESERVED_ARGS]
    if not reserved:
        return
    if args.get("context") == "other":
        if any(k not in ("choice", "text", "shift", "value", "settle") for k in reserved):
            raise KesalahanPerintah("invalid_generic_ui_payload")
        if args.get("btn") not in ("A", "X", "Y", "LT", "RT", "RB"):
            raise KesalahanPerintah("invalid_generic_ui_button")
        if "choice" in args and not (isinstance(args["choice"], str) and args["choice"].isascii()
                and args["choice"].isdigit() and 0 <= int(args["choice"]) < 512):
            raise KesalahanPerintah("invalid_generic_ui_component")
        if "text" in args and (args.get("btn") != "Y" or not isinstance(args["text"], str)
                or len(args["text"]) > 128 or any(ord(c) < 32 for c in args["text"])):
            raise KesalahanPerintah("invalid_generic_ui_text")
        if "shift" in args and (type(args["shift"]) is not bool or args.get("btn") not in ("A", "X")):
            raise KesalahanPerintah("invalid_generic_ui_shift")
        if "value" in args and (args.get("btn") not in ("A", "X", "RB")
                or not isinstance(args["value"], str) or not args["value"].isascii()
                or not args["value"].isdigit() or not 1 <= int(args["value"]) <= 120):
            raise KesalahanPerintah("invalid_generic_ui_hold")
        if "settle" in args and (args["settle"] is not True or args.get("btn") != "A"
                                  or "choice" not in args or "shift" in args):
            raise KesalahanPerintah("invalid_cursor_settle_payload")
        return
    allowed = UI_PRESS_CONTEXT_ARGS.get(args.get("context"), ())
    if any(k not in allowed for k in reserved) or args.get("btn") != "A":
        raise KesalahanPerintah("ui_press_reserved_arg_g2_g3")
    for key in reserved:
        value = args[key]
        if not isinstance(value, str) or not value or len(value) > 128:
            raise KesalahanPerintah("invalid_ui_press_" + key)
    if "text" in args and name_refusal(args["text"]) is not None:
        raise KesalahanPerintah("invalid_ui_press_text:" + str(name_refusal(args["text"])))
    for key in ("value", "choice", "slot"):
        if key in args and not (args[key].isascii() and args[key].lstrip("-").isdigit() and len(args[key]) <= 9):
            raise KesalahanPerintah("invalid_ui_press_" + key)
    if "festival" in args and args["festival"] not in UI_G3_FESTIVAL_KINDS:
        raise KesalahanPerintah("invalid_ui_press_festival")
    if ("slot" in args or "item" in args) and "festival" not in args:
        raise KesalahanPerintah("invalid_ui_press_slot_item")


def validate_ui_press_args(args: dict) -> None:
    """Mirror of the mod UiPressTarget.TryParse. WS-G3: reserved payloads only where a G3 context consumes them.
    WS-G2: at most one typed economy payload (qty int 1..999 / half true / stack true) on A / X, never together with a
    G3 payload; quick stays refused."""
    if not isinstance(args, dict):
        raise KesalahanPerintah("invalid_ui_press_args")
    extras = [k for k in UI_PRESS_ECONOMY_ARGS if k in args]
    if extras and (args.get("context") not in UI_PRESS_ECONOMY_CONTEXTS
                   or any(k in UI_PAD_RESERVED_ARGS and k not in UI_PRESS_ECONOMY_ARGS
                          and not (args.get("context") == "other" and k == "choice") for k in args)):
        raise KesalahanPerintah("ui_press_reserved_arg_g2_g3")
    _validate_ui_press_reserved({k: v for k, v in args.items() if k not in UI_PRESS_ECONOMY_ARGS})
    if len(extras) > 1 or extras and args.get("context") == "other" and (
            extras != ["qty"] or "choice" not in args or args.get("btn") != "A"):
        raise KesalahanPerintah("invalid_ui_press_economy_combination")
    if extras:
        key = extras[0]
        value = args[key]
        if key == "qty" and (type(value) is not int or not 1 <= value <= 999):
            raise KesalahanPerintah("invalid_ui_press_qty")
        if key in ("half", "stack") and value is not True:
            raise KesalahanPerintah("invalid_ui_press_" + key)
        if args.get("btn") not in ("A", "X"):
            raise KesalahanPerintah("invalid_ui_press_economy_button")
    base = set(UI_PAD_BASE_ARGS) | {"btn", "snapped_id"}
    plain = {k: v for k, v in args.items() if k not in UI_PAD_RESERVED_ARGS}
    if set(plain) not in (base, base | {"tab"}):
        raise KesalahanPerintah("invalid_ui_press_args")
    _validate_ui_pad_base(args, "ui_press")
    if args["btn"] not in UI_PRESS_BUTTONS:
        raise KesalahanPerintah("invalid_ui_press_btn")
    if type(args["snapped_id"]) is not int or not -1 <= args["snapped_id"] <= 1_000_000:
        raise KesalahanPerintah("invalid_ui_press_snapped_id")
    if (args["btn"] == "tab") != ("tab" in args) or ("tab" in args and args["tab"] not in UI_PAD_TAB_NAMES):
        raise KesalahanPerintah("invalid_ui_press_tab")


def minta_ui_nav(*, receiver_token, menu_type, context, target_token, dir, **kwargs) -> HasilPerintah:
    """One snap step on the active menu (WS-G1): a table-gated direct call in the mod, never a Press."""
    target = dict(receiver_token=receiver_token, menu_type=menu_type, context=context, target_token=target_token, dir=dir)
    validate_ui_nav_args(target)
    return _minta_input_once("", 1, _ui_nav_target=target, **kwargs)


def minta_ui_press(*, receiver_token, menu_type, context, target_token, btn, snapped_id, tab=None,
                   **kwargs) -> HasilPerintah:
    """One mirrored menu button on the snapped component (WS-G1); the mod's table refuses "-" cells first.
    WS-G3: the reserved payloads a G3 context consumes ride in ``kwargs`` (text / value / choice / festival / slot / item);
    WS-G2: qty / half / stack ride there too, with their JSON type."""
    target = dict(receiver_token=receiver_token, menu_type=menu_type, context=context, target_token=target_token,
                  btn=btn, snapped_id=snapped_id)
    if tab is not None:
        target["tab"] = tab
    for key in UI_PAD_RESERVED_ARGS:
        if key in kwargs:
            target[key] = kwargs.pop(key)
    validate_ui_press_args(target)
    return _minta_input_once("", 1, _ui_press_target=target, **kwargs)


def validate_chat_send_args(args: dict) -> None:
    """Mirror of the mod ChatSendTarget.TryParse: exactly {"text"} under the chat policy."""
    if not isinstance(args, dict) or set(args) != {"text"}:
        raise KesalahanPerintah("invalid_chat_send_args")
    why = chat_refusal(args["text"])
    if why is not None:
        raise KesalahanPerintah("invalid_chat_send_text:" + why)


def minta_chat_send(*, text, **kwargs) -> HasilPerintah:
    """One chat line through ChatBox.textBoxEnter in the mod (WS-G3, owner D5; ChatSendEnabled default OFF)."""
    target = dict(text=text)
    validate_chat_send_args(target)
    return _minta_input_once("", 1, _chat_send_target=target, **kwargs)


def _permille(value) -> int:
    """cast_power in per-mille, rounded half away from zero (mod: Math.Round(x * 1000, AwayFromZero))."""
    scaled = float(value) * 1000.0
    return int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)


def validate_reflex_fish_args(args: dict) -> None:
    """Mirror of the mod ReflexFishTarget.TryParse + FishReflexArgs.Validate (exact keys, types, bounds)."""
    if not isinstance(args, dict) or set(args) != set(REFLEX_FISH_ARG_KEYS):
        raise KesalahanPerintah("invalid_reflex_fish_args")
    max_s, power, treasure, retries = (args[k] for k in REFLEX_FISH_ARG_KEYS)
    if type(max_s) is not int or not 1 <= max_s <= REFLEX_FISH_MAX_S_MAX:
        raise KesalahanPerintah("invalid_reflex_fish_max_s")
    if (type(power) not in (int, float) or isinstance(power, bool) or power != power
            or not REFLEX_FISH_CAST_POWER_MIN <= power <= REFLEX_FISH_CAST_POWER_MAX):
        raise KesalahanPerintah("invalid_reflex_fish_cast_power")
    if type(treasure) is not bool:
        raise KesalahanPerintah("invalid_reflex_fish_treasure")
    if type(retries) is not int or not 0 <= retries <= REFLEX_FISH_HOOK_RETRIES_MAX:
        raise KesalahanPerintah("invalid_reflex_fish_hook_retries")


def minta_reflex_fish(*, max_s, cast_power, treasure, hook_retries, **kwargs) -> HasilPerintah:
    """One fishing reflex in the mod (WS-G4): the call returns after the reflex verdict (bounded by max_s)."""
    target = dict(max_s=max_s, cast_power=cast_power, treasure=treasure, hook_retries=hook_retries)
    validate_reflex_fish_args(target)
    return _minta_input_once("", 1, _reflex_fish_target=target, **kwargs)


def validate_reflex_combat_args(args: dict) -> None:
    """Mirror of the mod ReflexCombatTarget.TryParse (exact keys, types, bounds)."""
    if (not isinstance(args, dict) or not set(REFLEX_COMBAT_ARG_KEYS) <= set(args)
            or set(args) - set(REFLEX_COMBAT_ARG_KEYS) - set(REFLEX_COMBAT_OPTIONAL_KEYS)):
        raise KesalahanPerintah("invalid_reflex_combat_args")
    if "escape" in args and args["escape"] is not True:
        raise KesalahanPerintah("invalid_reflex_combat_escape")
    if args.get("escape") and (args.get("retreat") is not False or args.get("max_kills") != 1):
        raise KesalahanPerintah("invalid_reflex_combat_escape")
    if "disengage" in args and (args["disengage"] is not True or args.get("escape") or args.get("retreat") is not True
                                or args.get("max_kills") != 1
                                or type(args.get("max_s")) is not int or args["max_s"] > REFLEX_COMBAT_DISENGAGE_MAX_S):
        raise KesalahanPerintah("invalid_reflex_combat_disengage")
    if "target_kind" in args and args["target_kind"] not in REFLEX_COMBAT_TARGET_KINDS:
        raise KesalahanPerintah("invalid_reflex_combat_target_kind")
    max_s, floor, kills, retreat = (args[k] for k in REFLEX_COMBAT_ARG_KEYS)
    if type(max_s) is not int or not 1 <= max_s <= REFLEX_COMBAT_MAX_S_MAX:
        raise KesalahanPerintah("invalid_reflex_combat_max_s")
    if args.get("escape") and max_s > 4:
        raise KesalahanPerintah("invalid_reflex_combat_escape")
    if type(floor) is not int or not REFLEX_COMBAT_HP_FLOOR_PCT_MIN <= floor <= 90:
        raise KesalahanPerintah("invalid_reflex_combat_hp_floor_pct")
    if type(kills) is not int or not 1 <= kills <= REFLEX_COMBAT_MAX_KILLS_MAX:
        raise KesalahanPerintah("invalid_reflex_combat_max_kills")
    if type(retreat) is not bool:
        raise KesalahanPerintah("invalid_reflex_combat_retreat")


def minta_reflex_combat(*, max_s, hp_floor_pct, max_kills, retreat, target_kind=None, escape=None, disengage=None,
                        **kwargs) -> HasilPerintah:
    """One combat reflex in the mod (WS-G5): the call returns after the reflex verdict (bounded by max_s)."""
    target = dict(max_s=max_s, hp_floor_pct=hp_floor_pct, max_kills=max_kills, retreat=retreat)
    if target_kind is not None:
        target["target_kind"] = target_kind  # FIXB15 D-B13-3
    if escape is not None:
        target["escape"] = escape
    if disengage is not None:
        target["disengage"] = disengage
    validate_reflex_combat_args(target)
    return _minta_input_once("", 1, _reflex_combat_target=target, **kwargs)


def validate_reflex_charge_args(args: dict) -> None:
    """Mirror of the mod ReflexChargeTarget.TryParse (exact keys, types, bounds)."""
    if not isinstance(args, dict) or set(args) != set(REFLEX_CHARGE_ARG_KEYS):
        raise KesalahanPerintah("invalid_reflex_charge_args")
    level, ticks = (args[k] for k in REFLEX_CHARGE_ARG_KEYS)
    if type(level) is not int or not 1 <= level <= REFLEX_CHARGE_TARGET_LEVEL_MAX:
        raise KesalahanPerintah("invalid_reflex_charge_target_level")
    if type(ticks) is not int or not REFLEX_CHARGE_MAX_TICKS_MIN <= ticks <= REFLEX_CHARGE_MAX_TICKS_MAX:
        raise KesalahanPerintah("invalid_reflex_charge_max_ticks")


def minta_reflex_charge(*, target_level, max_ticks, **kwargs) -> HasilPerintah:
    """One tool-charge reflex in the mod (WS-G5): the call returns after the verdict (bounded by max_ticks + settle)."""
    target = dict(target_level=target_level, max_ticks=max_ticks)
    validate_reflex_charge_args(target)
    return _minta_input_once("", 1, _reflex_charge_target=target, **kwargs)


def validate_pad_button_args(args: dict) -> None:
    """Mirror of the mod PadButtonTarget.TryParse: exactly {"button"} with one of PAD_BUTTONS."""
    if not isinstance(args, dict) or set(args) != {"button"}:
        raise KesalahanPerintah("invalid_pad_button_args")
    if args["button"] not in PAD_BUTTONS:
        raise KesalahanPerintah("invalid_pad_button_button")


def minta_pad_button(*, button, **kwargs) -> HasilPerintah:
    """One world pad button in the mod (WS-G0 slice 2): a table-gated direct call, never a Press."""
    target = dict(button=button)
    validate_pad_button_args(target)
    return _minta_input_once("", 1, _pad_button_target=target, **kwargs)


def validate_container_transfer_args(args: dict) -> None:
    texts={"receiver_token":128,"container_token":128,"readback_token":128,
           "source_fact_id":512,"target_token":128,"qualified_item_id":128}
    required=set(texts)|{"slot","stack","quality","owner_ceiling"}
    if isinstance(args,dict) and "mode" in args:
        _validate_container_transfer_mode(args,texts,required)
        return
    if not isinstance(args,dict) or set(args) not in (required, required|{"quantity"}):
        raise KesalahanPerintah("invalid_container_transfer_args")
    for key,limit in texts.items():
        if not isinstance(args[key],str) or not args[key].strip() or len(args[key])>limit:
            raise KesalahanPerintah("invalid_container_transfer_"+key)
    if (any(type(args[k]) is not int for k in ("slot","stack","quality","owner_ceiling"))
            or not 0<=args["slot"]<48 or not 0<args["stack"]<=999
            or not 0<args["owner_ceiling"]<=999
            or ("quantity" in args and (type(args["quantity"]) is not int or args["quantity"]!=1 or args["stack"]<=1))
            or args.get("quantity",args["stack"])>args["owner_ceiling"]
            or args["quality"]!=0 or args["qualified_item_id"] not in {"(O)388","(O)390"}):
        raise KesalahanPerintah("invalid_container_transfer_quantity_or_item")


def _validate_container_transfer_mode(args: dict, texts: dict, required: set) -> None:
    """Mirror of the mod ContainerTransferTarget mode validity (WS-G2): any item, quality 0/1/2/4, no quantity,
    take_out slots 0..71 (container side), the rest 0..47 (player side), amount (1 for ship_one) <= owner_ceiling."""
    if set(args) != required | {"mode"} or args["mode"] not in CONTAINER_TRANSFER_MODES:
        raise KesalahanPerintah("invalid_container_transfer_mode")
    for key,limit in texts.items():
        if not isinstance(args[key],str) or not args[key].strip() or len(args[key])>limit:
            raise KesalahanPerintah("invalid_container_transfer_"+key)
    if any(type(args[k]) is not int for k in ("slot","stack","quality","owner_ceiling")):
        raise KesalahanPerintah("invalid_container_transfer_quantity_or_item")
    amount = 1 if args["mode"] == "ship_one" else args["stack"]
    if (not 0<=args["slot"]<(72 if args["mode"]=="take_out" else 48) or not 0<args["stack"]<=999
            or not 0<args["owner_ceiling"]<=999 or amount>args["owner_ceiling"] or args["quality"] not in (0,1,2,4)):
        raise KesalahanPerintah("invalid_container_transfer_quantity_or_item")


def minta_container_transfer(*,receiver_token,container_token,readback_token,source_fact_id,target_token,
                            slot,qualified_item_id,stack,quality,owner_ceiling,quantity=None,mode=None,**kwargs):
    target=dict(receiver_token=receiver_token,container_token=container_token,readback_token=readback_token,
                source_fact_id=source_fact_id,target_token=target_token,slot=slot,qualified_item_id=qualified_item_id,
                stack=stack,quality=quality,owner_ceiling=owner_ceiling)
    if quantity is not None: target["quantity"]=quantity
    if mode is not None: target["mode"]=mode
    validate_container_transfer_args(target)
    return _minta_input_once("",1,_transfer_target=target,**kwargs)


_WORLD_INTERACT_BASE_KEYS = frozenset({"target_token", "source_fact_id", "tile_x", "tile_y",
                                       "actor_tile_x", "actor_tile_y", "facing", "kind"})


def location_token_valid(value) -> bool:
    """Mod SceneIdentity.Token form: non-empty printable ASCII without spaces, at most 128."""
    return (isinstance(value, str) and 0 < len(value) <= 128
            and all(" " < ch < "\x7f" for ch in value))


def validate_world_interaction_args(args: dict) -> None:
    """Exact key set per kind (WS3a): chest/forage 8 keys, door 9 with target_location_id (WS-G6: ladder_down too)."""
    if not isinstance(args, dict) or args.get("kind") not in WORLD_INTERACT_KINDS:
        raise KesalahanPerintah("invalid_world_interaction_kind")
    door = args["kind"] in WORLD_INTERACT_LOCATION_KINDS
    if set(args) != (_WORLD_INTERACT_BASE_KEYS | {"target_location_id"} if door else _WORLD_INTERACT_BASE_KEYS):
        raise KesalahanPerintah("invalid_world_interaction_args")
    if door and not location_token_valid(args["target_location_id"]):
        raise KesalahanPerintah("invalid_world_interaction_target_location_id")
    for key, maximum in (("target_token",128), ("source_fact_id",256)):
        if not isinstance(args[key],str) or not args[key].strip() or len(args[key])>maximum:
            raise KesalahanPerintah("invalid_world_interaction_"+key)
    for key in ("tile_x","tile_y","actor_tile_x","actor_tile_y","facing"):
        if type(args[key]) is not int or not 0<=args[key]<=(3 if key=="facing" else 4096):
            raise KesalahanPerintah("invalid_world_interaction_"+key)
    distance = abs(args["tile_x"]-args["actor_tile_x"])+abs(args["tile_y"]-args["actor_tile_y"])
    # Native A can harvest a walkable crop at the actual grab tile underfoot (and stand her up from a seat
    # whose grab tile is her own). Capture/re-capture still owns admission; the wire only bounds its reach.
    if args["kind"] in (*GENERIC_WORLD_KINDS, "harvest_crop", "festival_stand"):
        if distance > 1:
            raise KesalahanPerintah("world_interaction_not_adjacent")
    elif args["kind"] != HOUSE_ROUTE_KIND and args["kind"] not in APPROACH_ROUTE_KINDS and (not 0 <= distance <= 9 if args["kind"] == "festival_egg_route" else distance != 1):
        raise KesalahanPerintah("world_interaction_not_adjacent")


def minta_world_interaction(*, target_token, source_fact_id, tile_x, tile_y, actor_tile_x, actor_tile_y, facing,
                            kind="chest", target_location_id=None, **kwargs):
    target=dict(target_token=target_token,source_fact_id=source_fact_id,tile_x=tile_x,tile_y=tile_y,
                actor_tile_x=actor_tile_x,actor_tile_y=actor_tile_y,facing=facing,kind=kind)
    if target_location_id is not None:
        target["target_location_id"]=target_location_id
    validate_world_interaction_args(target)
    if kind == HOUSE_ROUTE_KIND:
        kwargs["batas_detik"] = max(kwargs.get("batas_detik") or 0, 125.0)
    if kind in EVENT_ROUTE_KINDS:
        kwargs["batas_detik"] = max(kwargs.get("batas_detik") or 0, 125.0 if kind in APPROACH_ROUTE_KINDS else 25.0)
    if kind in WORLD_INTERACT_LOCATION_KINDS:
        # A door / ladder warp settles only after the farmhand location round trip.
        kwargs["batas_detik"]=max(kwargs.get("batas_detik") or 0, kwargs.get("admisi_detik", ADMISI_DETIK_DEFAULT)
                                  + 3.0 + WARP_RESULT_EXTRA_SECONDS)
    elif kind in WORLD_EXTRAS_KINDS:
        # WS-G6: mount / dismount / milk / shear settle up to 3 s (mod BgExtrasEffect.SlowWaitMs) + the cleanup bound.
        kwargs["batas_detik"]=max(kwargs.get("batas_detik") or 0, kwargs.get("admisi_detik", ADMISI_DETIK_DEFAULT) + 3.0 + 6.0)
    return _minta_input_once("",1,_interaction_target=target,**kwargs)


_MOVE_THROUGH_BASE_KEYS = frozenset({"direction", "ticks", "kind", "source_fact_id", "target_token",
                                     "tile_x", "tile_y", "actor_tile_x", "actor_tile_y"})
_DIRECTION_VECTORS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def validate_move_through_args(args: dict) -> None:
    """Mirror of the mod MoveThroughTarget: the warp tile is the faced cardinal neighbour."""
    if not isinstance(args, dict) or args.get("kind") not in MOVE_THROUGH_KINDS:
        raise KesalahanPerintah("invalid_move_through_kind")
    keys = _MOVE_THROUGH_BASE_KEYS | ({"target_location_id"} if args["kind"] == "warp" else set())
    if set(args) != keys:
        raise KesalahanPerintah("invalid_move_through_args")
    if args["direction"] not in _DIRECTION_VECTORS:
        raise KesalahanPerintah("invalid_move_through_direction")
    if type(args["ticks"]) is not int or not 1 <= args["ticks"] <= MOVE_THROUGH_TICKS_MAX:
        raise KesalahanPerintah("invalid_move_through_ticks")
    for key, maximum in (("target_token", 128), ("source_fact_id", 256)):
        if not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > maximum:
            raise KesalahanPerintah("invalid_move_through_" + key)
    for key, low, high in (("tile_x", -1, 4097), ("tile_y", -1, 4097), ("actor_tile_x", 0, 4096), ("actor_tile_y", 0, 4096)):
        if type(args[key]) is not int or not low <= args[key] <= high:
            raise KesalahanPerintah("invalid_move_through_" + key)
    dx, dy = _DIRECTION_VECTORS[args["direction"]]
    step = (args["tile_x"] - args["actor_tile_x"], args["tile_y"] - args["actor_tile_y"])
    if args["kind"] == "bed":
        # Mirror of the mod BgBedThrough.ReachValid: sideways along the sleep row, one or two tiles.
        if dy != 0 or step not in ((dx, 0), (2 * dx, 0)):
            raise KesalahanPerintah("move_through_bed_not_on_sleep_row")
    # One or two tiles straight ahead (live B2W6a D6: the approach stops one tile short of the warp).
    elif step not in ((dx, dy), (2 * dx, 2 * dy)):
        raise KesalahanPerintah("move_through_not_faced_neighbour")
    if args["kind"] == "warp" and not location_token_valid(args["target_location_id"]):
        raise KesalahanPerintah("invalid_move_through_target_location_id")


def minta_move_through(*, direction, ticks, kind, source_fact_id, target_token, tile_x, tile_y,
                       actor_tile_x, actor_tile_y, target_location_id=None, **kwargs):
    target = dict(direction=direction, ticks=ticks, kind=kind, source_fact_id=source_fact_id,
                  target_token=target_token, tile_x=tile_x, tile_y=tile_y,
                  actor_tile_x=actor_tile_x, actor_tile_y=actor_tile_y)
    if target_location_id is not None:
        target["target_location_id"] = target_location_id
    validate_move_through_args(target)
    kwargs["batas_detik"] = max(kwargs.get("batas_detik") or 0, kwargs.get("admisi_detik", ADMISI_DETIK_DEFAULT)
                                + ticks / 60.0 + 3.0 + WARP_RESULT_EXTRA_SECONDS)
    return _minta_input_once(direction, ticks, _through_target=target, **kwargs)


def validate_use_tool_args(args: dict) -> None:
    if set(args) != {"ticks", "slot", "qualified_item_id", "tile_x", "tile_y", "facing"}:
        raise KesalahanPerintah("invalid_use_tool_args")
    for key, maximum in (("ticks", 60), ("slot", 255), ("tile_x", 10000),
                         ("tile_y", 10000), ("facing", 3)):
        value = args[key]
        if type(value) is not int or not (1 if key == "ticks" else 0) <= value <= maximum:
            raise KesalahanPerintah("invalid_use_tool_" + key)
    item = args["qualified_item_id"]
    if not isinstance(item, str) or not item.strip() or len(item) > 128:
        raise KesalahanPerintah("invalid_use_tool_item")


def _minta_input_once(
    arah: str,
    ticks: int,
    *,
    owner_id: str,
    generation: int,
    control_seq: int,
    context_generation: int,
    folder: str | os.PathLike[str] | None = None,
    admisi_detik: float = ADMISI_DETIK_DEFAULT,
    batas_detik: float | None = None,
    sesi: SesiPerintah | None = None,
    runtime_instance_id: str | None = None,
    expected_location_id: str | None = None,
    expected_screen_id: int | None = None,
    guarded_publish: Callable[[Callable[[], None]], None] | None = None,
    deadline_cap_unix_ms: int | None = None,
    execution_deadline_unix_ms: int | None = None,
    _tool_target: dict | None = None,
    _selection_target: dict | None = None,
    _interaction_target: dict | None = None,
    _transfer_target: dict | None = None,
    _exit_target: dict | None = None,
    _ui_key_target: dict | None = None,
    _through_target: dict | None = None,
    _pad_button_target: dict | None = None,
    _ui_nav_target: dict | None = None,
    _ui_press_target: dict | None = None,
    _chat_send_target: dict | None = None,
    _reflex_fish_target: dict | None = None,
    _reflex_combat_target: dict | None = None,
    _reflex_charge_target: dict | None = None,
    event_token: str | None = None,
    segment_stop: tuple[int, int] | None = None,
    path_next: tuple[tuple[int, int], ...] | None = None,
) -> HasilPerintah:
    """Optional guard must synchronously publish once or raise; waiting is unguarded.

    An absolute deadline cap bounds admission despite setup latency; omitted caps
    preserve the relative admisi_detik contract. Native expiry fencing still applies.
    Only reviewed bounded event routes may carry a separate absolute execution deadline;
    the same explicit cap bounds it, and omission retains the original deadline.
    """
    if deadline_cap_unix_ms is not None and (
        type(deadline_cap_unix_ms) is not int or not 0 < deadline_cap_unix_ms <= 9_223_372_036_854_775_807
    ):
        raise KesalahanPerintah("invalid_deadline_cap_unix_ms")
    if execution_deadline_unix_ms is not None and (
        type(execution_deadline_unix_ms) is not int
        or not 0 < execution_deadline_unix_ms <= 9_223_372_036_854_775_807
        or _interaction_target is None or _interaction_target.get("kind") not in (*EVENT_ROUTE_KINDS, HOUSE_ROUTE_KIND)
    ):
        raise KesalahanPerintah("invalid_execution_deadline_unix_ms")
    if segment_stop is not None and any(t is not None for t in (
            _tool_target, _selection_target, _interaction_target, _transfer_target, _exit_target, _ui_key_target,
            _through_target, _pad_button_target, _ui_nav_target, _ui_press_target, _chat_send_target,
            _reflex_fish_target, _reflex_combat_target, _reflex_charge_target, event_token)):
        raise KesalahanPerintah("segment_stop_only_for_move_cardinal")  # fix89D / ADR 158
    if path_next is not None and segment_stop is None:
        raise KesalahanPerintah("path_next_only_with_segment_stop")  # fix92A / ADR 162
    if event_token is not None:
        from arti_stardew_event_control import token_shape
        if (not token_shape(event_token) or type(ticks) is not int or not 1 <= ticks <= 4
                or any(t is not None for t in (_tool_target, _selection_target, _interaction_target,
                    _transfer_target, _exit_target, _ui_key_target, _through_target, _pad_button_target,
                    _ui_nav_target, _ui_press_target, _chat_send_target, _reflex_fish_target,
                    _reflex_combat_target, _reflex_charge_target))):
            raise KesalahanPerintah("invalid_event_movement")
    dirpath = folder_perintah(folder)
    ui_pad_target = _ui_nav_target if _ui_nav_target is not None else _ui_press_target
    g5_target = _reflex_combat_target if _reflex_combat_target is not None else _reflex_charge_target
    if g5_target is not None:
        # Primitives v2 WS-G5: one reflex primitive per request, nothing else.
        if any(t is not None for t in (_tool_target, _selection_target, _interaction_target, _transfer_target, _exit_target,
                                       _ui_key_target, _through_target, _pad_button_target, ui_pad_target, _chat_send_target,
                                       _reflex_fish_target)) or (_reflex_combat_target is not None and _reflex_charge_target is not None):
            raise KesalahanPerintah("conflicting_input_primitive")
        if _reflex_combat_target is not None:
            validate_reflex_combat_args(_reflex_combat_target)
        else:
            validate_reflex_charge_args(_reflex_charge_target)
        arah_bersih, n = "", 1
    elif _reflex_fish_target is not None:
        if any(t is not None for t in (_tool_target, _selection_target, _interaction_target, _transfer_target,
                                       _exit_target, _ui_key_target, _through_target, _pad_button_target, ui_pad_target)):
            raise KesalahanPerintah("conflicting_input_primitive")
        validate_reflex_fish_args(_reflex_fish_target)
        arah_bersih, n = "", 1
    elif ui_pad_target is not None:
        if (any(t is not None for t in (_tool_target, _selection_target, _interaction_target, _transfer_target,
                                        _exit_target, _ui_key_target, _through_target, _pad_button_target,
                                        _chat_send_target))
                or (_ui_nav_target is not None and _ui_press_target is not None)):
            raise KesalahanPerintah("conflicting_input_primitive")
        if _ui_nav_target is not None:
            validate_ui_nav_args(_ui_nav_target)
        else:
            validate_ui_press_args(_ui_press_target)
        arah_bersih, n = "", 1
    elif _pad_button_target is not None or _chat_send_target is not None:
        if any(t is not None for t in (_tool_target, _selection_target, _interaction_target, _transfer_target,
                                       _exit_target, _ui_key_target, _through_target)) or (
                _pad_button_target is not None and _chat_send_target is not None):
            raise KesalahanPerintah("conflicting_input_primitive")
        if _pad_button_target is not None:
            validate_pad_button_args(_pad_button_target)
        else:
            validate_chat_send_args(_chat_send_target)
        arah_bersih, n = "", 1
    elif _ui_key_target is not None:
        if any(t is not None for t in (_tool_target, _selection_target, _interaction_target, _transfer_target, _exit_target, _through_target)):
            raise KesalahanPerintah("conflicting_input_primitive")
        validate_ui_key_args(_ui_key_target)
        arah_bersih, n = "", 1
    elif _through_target is not None:
        if any(t is not None for t in (_tool_target,_selection_target,_interaction_target,_transfer_target,_exit_target)):
            raise KesalahanPerintah("conflicting_input_primitive")
        validate_move_through_args(_through_target)
        if arah != _through_target["direction"] or ticks != _through_target["ticks"]:
            raise KesalahanPerintah("move_through_direction_or_ticks_mismatch")
        arah_bersih, n = _validasi_world_press(arah, ticks)
    elif _exit_target is not None:
        if any(t is not None for t in (_tool_target,_selection_target,_interaction_target,_transfer_target)):
            raise KesalahanPerintah("conflicting_input_primitive")
        validate_container_exit_args(_exit_target)
        arah_bersih,n="",1
    elif _transfer_target is not None:
        if any(t is not None for t in (_tool_target,_selection_target,_interaction_target)):
            raise KesalahanPerintah("conflicting_input_primitive")
        validate_container_transfer_args(_transfer_target)
        arah_bersih,n="",1
    elif _interaction_target is not None:
        if _tool_target is not None or _selection_target is not None:
            raise KesalahanPerintah("conflicting_input_primitive")
        validate_world_interaction_args(_interaction_target)
        arah_bersih, n = "", 1
    elif _selection_target is not None:
        if _tool_target is not None:
            raise KesalahanPerintah("conflicting_input_primitive")
        validate_select_item_args(_selection_target)
        arah_bersih, n = "", 1
    elif _tool_target is None and segment_stop is not None and path_next is not None:
        # fix92A / ADR 162: a guarded path may hold up to MOVE_PATH_TICKS_MAX frames over all its segments.
        if isinstance(ticks, bool) or not isinstance(ticks, int) or not WORLD_PRESS_TICKS_MIN <= ticks <= MOVE_PATH_TICKS_MAX:
            raise KesalahanPerintah(f"ticks jalur harus {WORLD_PRESS_TICKS_MIN}..{MOVE_PATH_TICKS_MAX}, dapat {ticks!r}")
        arah_bersih, _ = _validasi_segment_press(arah, WORLD_PRESS_TICKS_MIN, segment_stop, event_token)  # direction, stop
        n = ticks
        path_next = _validasi_path_next(arah_bersih, segment_stop, path_next)
    elif _tool_target is None and segment_stop is not None:
        arah_bersih, n = _validasi_segment_press(arah, ticks, segment_stop, event_token)
    elif _tool_target is None:
        arah_bersih, n = _validasi_world_press(arah, ticks)
    else:
        validate_use_tool_args(dict(_tool_target, ticks=ticks))
        arah_bersih, n = "", ticks
    if sesi is None:
        sesi = baca_sesi(dirpath)

    # Preserve the established input_once caller contract: malformed lease
    # arguments are programmer/input errors and raise before any file write.
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise KesalahanPerintah(f"owner_id wajib, dapat {owner_id!r}")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise KesalahanPerintah(f"generation lease wajib integer >= 1, dapat {generation!r}")
    if isinstance(control_seq, bool) or not isinstance(control_seq, int) or control_seq < 1:
        raise KesalahanPerintah(f"control_seq wajib integer >= 1, dapat {control_seq!r}")
    if (
        isinstance(context_generation, bool)
        or not isinstance(context_generation, int)
        or context_generation < 1
    ):
        raise KesalahanPerintah(
            f"context_generation wajib integer >= 1, dapat {context_generation!r}"
        )
    runtime_for_contract = runtime_instance_id or str(
        sesi.mentah.get("runtime_instance_id") or ""
    )
    if not runtime_for_contract:
        raise KesalahanPerintah("runtime_instance_id wajib untuk input_once")

    provisional_id = uuid.uuid4().hex
    gagal_common, runtime, lokasi, layar, delta_ms = _validasi_leased_common(
        sesi=sesi,
        owner_id=owner_id,
        generation=generation,
        control_seq=control_seq,
        context_generation=context_generation,
        runtime_instance_id=runtime_for_contract,
        expected_location_id=expected_location_id,
        expected_screen_id=expected_screen_id,
        admisi_detik=admisi_detik,
        request_id=provisional_id,
        allow_unnamed_player=(_ui_press_target is not None
                              and _ui_press_target.get("context") == UI_PAD_CONTEXT_CHAR_CUSTOM),
    )
    if gagal_common is not None:
        return gagal_common
    now_ms = _sekarang_ms()
    deadline_ms = now_ms + delta_ms
    if deadline_ms > 9_223_372_036_854_775_807:
        return HasilPerintah(
            GAGAL_LOKAL,
            False,
            provisional_id,
            alasan=f"admisi_detik menghasilkan deadline tidak sah: {admisi_detik!r}",
        )
    if deadline_cap_unix_ms is not None:
        deadline_ms = min(deadline_ms, deadline_cap_unix_ms)
    payload: dict = {
        "protocol": PROTOCOL,
        "op": OP_INPUT_ONCE,
        "request_id": provisional_id,
        "runtime_instance_id": runtime,
        "session_id": sesi.session_id,
        "owner_id": owner_id.strip(),
        "generation": generation,
        "control_seq": control_seq,
        "context_generation": context_generation,
        "expected_player_id": sesi.player_id,
        "expected_player_name": sesi.player_name,
        "expected_screen_id": layar,
        "expected_location_id": lokasi,
        "family": PRIMITIVE_UI_NAV if _ui_nav_target is not None else PRIMITIVE_UI_PRESS if _ui_press_target is not None else PRIMITIVE_UI_KEY if _ui_key_target is not None else "container_exit" if _exit_target is not None else "container_transfer" if _transfer_target is not None else "select_item" if _selection_target is not None else FAMILY_WORLD_KEY,
        "primitive": PRIMITIVE_REFLEX_COMBAT if _reflex_combat_target is not None else PRIMITIVE_REFLEX_CHARGE if _reflex_charge_target is not None else PRIMITIVE_REFLEX_FISH if _reflex_fish_target is not None else PRIMITIVE_UI_NAV if _ui_nav_target is not None else PRIMITIVE_UI_PRESS if _ui_press_target is not None else PRIMITIVE_PAD_BUTTON if _pad_button_target is not None else PRIMITIVE_CHAT_SEND if _chat_send_target is not None else PRIMITIVE_UI_KEY if _ui_key_target is not None else PRIMITIVE_MOVE_THROUGH if _through_target is not None else "container_exit" if _exit_target is not None else "container_transfer" if _transfer_target is not None else "world_interact" if _interaction_target is not None else "select_item" if _selection_target is not None else (
            PRIMITIVE_MOVE_CARDINAL if _tool_target is None else PRIMITIVE_USE_TOOL),
        "args": dict(g5_target) if g5_target is not None else dict(_reflex_fish_target) if _reflex_fish_target is not None else dict(ui_pad_target) if ui_pad_target is not None else dict(_pad_button_target) if _pad_button_target is not None else dict(_chat_send_target) if _chat_send_target is not None else dict(_ui_key_target) if _ui_key_target is not None else dict(_through_target) if _through_target is not None else dict(_exit_target) if _exit_target is not None else dict(_transfer_target) if _transfer_target is not None else dict(_interaction_target) if _interaction_target is not None else dict(_selection_target) if _selection_target is not None else (
            {"direction": arah_bersih, "ticks": n} if _tool_target is None else dict(_tool_target, ticks=n)),
        "deadline_unix_ms": deadline_ms,
    }
    if event_token is not None:
        payload["args"]["event_token"] = event_token
    if segment_stop is not None:
        payload["args"]["segment_stop"] = [int(segment_stop[0]), int(segment_stop[1])]
    if path_next is not None:
        payload["args"]["path_next"] = [[int(s[0]), int(s[1])] for s in path_next]  # fix92A / ADR 162
    if execution_deadline_unix_ms is not None:
        # Fresh admission is independent of bounded route execution and its trial cap.
        execution_ms = min(execution_deadline_unix_ms, now_ms + (120_000 if _interaction_target["kind"] in (*APPROACH_ROUTE_KINDS, HOUSE_ROUTE_KIND) else 20_000))
        if deadline_cap_unix_ms is not None:
            execution_ms = min(execution_ms, deadline_cap_unix_ms)
        payload["execution_deadline_unix_ms"] = execution_ms
    if _journal_capable(sesi):
        payload = _journalize_payload(payload)
    request_id = str(payload["request_id"])
    gagal = _gagal_lokal_sebelum_tulis(dirpath, sesi, request_id, OP_INPUT_ONCE,
                                     primitive=payload["primitive"])
    if gagal is not None:
        return gagal
    if guarded_publish is None:
        _tulis_permintaan(dirpath, payload)
    else:
        guarded_publish(lambda: _tulis_permintaan(dirpath, payload))
    if batas_detik is None:
        batas_detik = admisi_detik + (n / 60.0) + 3.0
    return _tunggu_hasil(dirpath, request_id, batas_detik)


def minta_inspect_tiles(
    center_x: int,
    center_y: int,
    radius: int,
    *,
    expected_location_id: str | None = None,
    folder: str | os.PathLike[str] | None = None,
    admisi_detik: float = ADMISI_DETIK_DEFAULT,
    batas_detik: float | None = None,
    sesi: SesiPerintah | None = None,
) -> HasilPerintah:
    dirpath = folder_perintah(folder)
    x = _validasi_ubin(center_x, "center_x")
    y = _validasi_ubin(center_y, "center_y")
    r = _validasi_radius(radius)
    if sesi is None:
        sesi = baca_sesi(dirpath)
    request_id = uuid.uuid4().hex
    gagal = _gagal_lokal_sebelum_tulis(dirpath, sesi, request_id, OP_INSPECT_TILES)
    if gagal is not None:
        return gagal
    try:
        lokasi = _validasi_location_id(
            expected_location_id if expected_location_id is not None else sesi.location_id
        )
    except KesalahanPerintah as exc:
        return HasilPerintah(GAGAL_LOKAL, False, request_id, alasan=str(exc))
    deadline_ms = _sekarang_ms() + int(admisi_detik * 1000)
    _tulis_permintaan(
        dirpath,
        {
            "protocol": PROTOCOL,
            "op": OP_INSPECT_TILES,
            "request_id": request_id,
            "session_id": sesi.session_id,
            "expected_player_id": sesi.player_id,
            "expected_player_name": sesi.player_name,
            "expected_location_id": lokasi,
            "center_x": x,
            "center_y": y,
            "radius": r,
            "deadline_unix_ms": deadline_ms,
        },
    )
    if batas_detik is None:
        batas_detik = admisi_detik + 3.0
    hasil = _tunggu_hasil(dirpath, request_id, batas_detik)
    if (
        hasil.status == SELESAI
        and hasil.mentah is not None
        and (
            hasil.mentah.get("session_id") != sesi.session_id
            or hasil.mentah.get("location_id") != lokasi
            or hasil.mentah.get("center_x") != x
            or hasil.mentah.get("center_y") != y
            or hasil.mentah.get("radius") != r
        )
    ):
        return HasilPerintah(
            status=SELESAI,
            sukses=False,
            request_id=request_id,
            outcome=hasil.outcome,
            alasan="identitas hasil tidak cocok dengan permintaan",
            mentah=hasil.mentah,
        )
    return hasil


def minta_inspect_context(
    *,
    expected_location_id: str | None = None,
    expected_screen_id: int | None = None,
    folder: str | os.PathLike[str] | None = None,
    admisi_detik: float = ADMISI_DETIK_DEFAULT,
    batas_detik: float | None = None,
    sesi: SesiPerintah | None = None,
    ui_decision_receiver: str | None = None,
    ui_decision_timeout_ms: int = 0,
) -> HasilPerintah:
    dirpath = folder_perintah(folder)
    if sesi is None:
        sesi = baca_sesi(dirpath)
    request_id = uuid.uuid4().hex
    gagal = _gagal_lokal_sebelum_tulis(dirpath, sesi, request_id, OP_INSPECT_CONTEXT)
    if gagal is not None:
        return gagal
    try:
        lokasi = _validasi_location_id(
            expected_location_id if expected_location_id is not None else sesi.location_id
        )
    except KesalahanPerintah as exc:
        return HasilPerintah(GAGAL_LOKAL, False, request_id, alasan=str(exc))
    layar = sesi.screen_id if expected_screen_id is None else expected_screen_id
    if isinstance(layar, bool) or not isinstance(layar, int) or layar < 0:
        return HasilPerintah(
            GAGAL_LOKAL,
            False,
            request_id,
            alasan=f"expected_screen_id harus integer >= 0, dapat {layar!r}",
        )
    deadline_ms = _sekarang_ms() + int(admisi_detik * 1000)
    decision_lease = {}
    if ui_decision_receiver is not None:
        if (not isinstance(ui_decision_receiver, str) or not ui_decision_receiver.startswith("rcv:")
                or len(ui_decision_receiver) > 128 or type(ui_decision_timeout_ms) is not int
                or not 0 <= ui_decision_timeout_ms <= 120000):
            raise KesalahanPerintah("ui_decision_lease")
        decision_lease = dict(ui_decision_receiver=ui_decision_receiver,
                              ui_decision_timeout_ms=ui_decision_timeout_ms)
    _tulis_permintaan(
        dirpath,
        {
            "protocol": PROTOCOL,
            "op": OP_INSPECT_CONTEXT,
            "request_id": request_id,
            "session_id": sesi.session_id,
            "expected_player_id": sesi.player_id,
            "expected_player_name": sesi.player_name,
            "expected_location_id": lokasi,
            "expected_screen_id": layar,
            "deadline_unix_ms": deadline_ms,
            **decision_lease,
        },
    )
    if batas_detik is None:
        batas_detik = admisi_detik + 3.0
    hasil = _tunggu_hasil(dirpath, request_id, batas_detik)
    if hasil.status != SELESAI or hasil.mentah is None:
        return hasil
    if _inspect_context_pin_tidak_cocok(hasil.mentah, sesi, lokasi, layar):
        return HasilPerintah(
            status=SELESAI,
            sukses=False,
            request_id=request_id,
            outcome=hasil.outcome,
            alasan="identitas hasil tidak cocok dengan permintaan",
            mentah=hasil.mentah,
        )
    return hasil


def _inspect_context_pin_tidak_cocok(
    mentah: dict,
    sesi: SesiPerintah,
    lokasi: str,
    layar: int,
) -> bool:
    ctx = mentah.get("context")
    if not isinstance(ctx, dict):
        return True
    return (
        mentah.get("session_id") != sesi.session_id
        or mentah.get("player_id") != sesi.player_id
        or mentah.get("location_id") != lokasi
        or mentah.get("screen_id") != layar
        or ctx.get("session_id") != sesi.session_id
        or ctx.get("player_id") != sesi.player_id
        or ctx.get("location_id") != lokasi
        or ctx.get("screen_id") != layar
        or ctx.get("runtime_instance_id") != mentah.get("runtime_instance_id")
        or ctx.get("context_generation") != mentah.get("context_generation")
        or ctx.get("ui_revision") != mentah.get("ui_revision")
        or ctx.get("viewport_transform_revision")
        != mentah.get("viewport_transform_revision")
    )


def _ui_key_block(context: dict) -> dict | None:
    block = context.get("ui_key") if isinstance(context, dict) else None
    return block if isinstance(block, dict) else None


def _cli_ui_key(args) -> int:
    """Operator witness (WS2 §5): print the current ui_key block; send ONE key only with --yes.

    One lease (acquire -> one input_once -> release), bounded waits, never a loop over keys.
    """
    sesi = baca_sesi(args.folder, retry_detik=0.25)
    belum = sesi.alasan_belum_siap(OP_INPUT_ONCE, primitive=PRIMITIVE_UI_KEY)
    inspect = minta_inspect_context(folder=args.folder, sesi=sesi)
    context = (inspect.mentah or {}).get("context") if inspect.sukses else None
    block = _ui_key_block(context) if isinstance(context, dict) else None
    capability = ((context or {}).get("capabilities") or {}).get(PRIMITIVE_UI_KEY) if isinstance(context, dict) else None
    print(json.dumps({"ui_key": block, "capability": capability,
                      "day_transition": (context or {}).get("day_transition") if isinstance(context, dict) else None,
                      "session_not_ready": belum}, ensure_ascii=False, indent=2))
    if args.key is None:
        return 0 if block is not None else 2
    if block is None or not isinstance(capability, dict) or capability.get("available") is not True:
        print("GAGAL: ui_key tidak tersedia untuk receiver sekarang", file=sys.stderr)
        return 2
    token = next((k.get("target_token") for k in block.get("keys", []) if isinstance(k, dict) and k.get("key") == args.key), None)
    target = dict(receiver_token=block.get("receiver_token"), menu_type=block.get("menu_type"),
                  kind=block.get("kind"), key=args.key, target_token=token)
    if args.option is not None:
        target["option_index"] = args.option
    if args.profession is not None:
        target["profession_id"] = args.profession
    validate_ui_key_args(target)
    if args.key == "choose":
        # A profession is a save mutation: the witness CLI may only send the owner-approved default choice.
        from arti_stardew_day_start import DayStartPolicy
        options = tuple(p.get("profession_id") for p in block.get("professions") or [] if isinstance(p, dict))
        approved = DayStartPolicy(enabled=True).profession_for(block.get("skill", -1), block.get("level", -1), options)
        if approved is None or approved != args.profession or options.index(approved) != args.option:
            print("GAGAL: choose hanya untuk profesi default yang disetujui owner", file=sys.stderr)
            return 2
    if not args.yes:
        print("DRY: tidak mengirim tanpa --yes; target=" + json.dumps(target, ensure_ascii=False))
        return 2
    if belum:
        print(f"GAGAL: {belum}", file=sys.stderr)
        return 2
    owner = "ui-key-cli-" + uuid.uuid4().hex[:12]
    generation = int(time.time() * 1000)
    tulis_kontrol(KIND_ACQUIRE, owner_id=owner, generation=generation, control_seq=1, folder=args.folder, sesi=sesi,
                  expected_screen_id=sesi.screen_id)
    try:
        batas = time.monotonic() + 1.0
        current = sesi
        while time.monotonic() < batas:
            current = baca_sesi(args.folder, retry_detik=0.25)
            if lease_state(current.mentah).owns(owner, generation, 1, live=True):
                break
            W.call('poll', 86, time.sleep, 0.02)
        else:
            print("GAGAL: acquire tidak dikonfirmasi", file=sys.stderr)
            return 3
        hasil = minta_ui_key(**target, folder=args.folder, sesi=current, owner_id=owner, generation=generation,
                             control_seq=1, context_generation=int(context["context_generation"]),
                             runtime_instance_id=str(context.get("runtime_instance_id") or ""),
                             expected_location_id=str(context.get("location_id") or sesi.location_id),
                             expected_screen_id=sesi.screen_id, admisi_detik=1.0, batas_detik=4.5)
    finally:
        try:
            current = baca_sesi(args.folder, retry_detik=0.25)
            lease = lease_state(current.mentah)
            seq = lease.seq if lease.owner == owner else 1
            tulis_kontrol(KIND_RELEASE, owner_id=owner, generation=generation,
                          control_seq=(seq if type(seq) is int else 1) + 1, folder=args.folder, sesi=current,
                          expected_screen_id=sesi.screen_id)
        except KesalahanPerintah as exc:
            print(f"PERINGATAN: release gagal: {exc}", file=sys.stderr)
    print(hasil.ringkas())
    if hasil.mentah is not None:
        print(json.dumps({k: hasil.mentah.get(k) for k in ("outcome", "ui_key_effect", "result_receiver_token",
                          "result_menu_type", "receiver_transition_status", "input_settled", "profession_id")},
                         ensure_ascii=False))
    return 0 if hasil.sukses else (3 if hasil.status == TIDAK_PASTI else 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P5-R3D: minta SATU menu_step ke mod ARTI (kandidat, bawaan mati).",
    )
    parser.add_argument("arah", nargs="?", choices=ARAH_SAH, help="arah satu langkah")
    parser.add_argument("--ui-key", action="store_true",
                        help="WS2 witness: cetak blok ui_key; dengan --key dan --yes kirim SATU ui_key di bawah lease")
    parser.add_argument("--key", default=None, choices=("advance", "answer", "cancel", "confirm", "choose", "close"))
    parser.add_argument("--option", type=int, default=None, help="option_index untuk answer/choose")
    parser.add_argument("--profession", type=int, default=None, help="profession_id untuk choose")
    parser.add_argument("--yes", action="store_true", help="wajib untuk benar-benar mengirim ui_key")
    parser.add_argument("--folder", default=None, help="folder command/ milik mod")
    parser.add_argument("--timeout-ticks", type=int, default=TIMEOUT_TICKS_DEFAULT)
    parser.add_argument("--delay-ticks", type=int, default=0)
    parser.add_argument("--admisi-detik", type=float, default=ADMISI_DETIK_DEFAULT)
    parser.add_argument("--batas-detik", type=float, default=None)
    parser.add_argument("--sesi", action="store_true", help="cetak status sesi lalu keluar")
    parser.add_argument(
        "--bersihkan",
        action="store_true",
        help="buang request/result yang tertinggal (sesudah hasil tidak_pasti)",
    )
    parser.add_argument("--json", action="store_true", help="cetak envelope mentah")
    args = parser.parse_args(argv)

    try:
        if args.ui_key:
            return _cli_ui_key(args)
        if args.bersihkan:
            dibuang = bersihkan_slot(args.folder)
            print("dibuang: " + (", ".join(dibuang) if dibuang else "(tidak ada)"))
            return 0
        sesi = baca_sesi(args.folder)
        if args.sesi or args.arah is None:
            belum = sesi.alasan_belum_siap()
            print(
                f"session_id={sesi.session_id} player={sesi.player_name}"
                f"({sesi.player_id}) screen={sesi.screen_id} "
                f"siap={sesi.siap}" + (f" belum_siap={belum}" if belum else "")
            )
            if args.json:
                print(json.dumps(sesi.mentah, ensure_ascii=False, indent=2))
            return 0 if (args.sesi or sesi.siap) else 2
        hasil = minta_menu_step(
            args.arah,
            folder=args.folder,
            timeout_ticks=args.timeout_ticks,
            delay_ticks=args.delay_ticks,
            admisi_detik=args.admisi_detik,
            batas_detik=args.batas_detik,
            sesi=sesi,
        )
    except KesalahanPerintah as exc:
        print(f"GAGAL: {exc}", file=sys.stderr)
        return 2

    print(hasil.ringkas())
    if args.json and hasil.mentah is not None:
        print(json.dumps(hasil.mentah, ensure_ascii=False, indent=2))
    if hasil.sukses:
        return 0
    return 3 if hasil.status == TIDAK_PASTI else 1


if __name__ == "__main__":
    raise SystemExit(main())
