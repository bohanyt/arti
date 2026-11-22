"""ADR 235: opt-in journal receipt of the native decision fence's own inputs."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import json
import math
from threading import Event
from types import SimpleNamespace as NS
from collections.abc import Mapping

SCHEMA = "arti.stardew.menu_fence/1"
SETTING = "stardew_menu_fence_replay"
MAX_ROW = 48 * 1024
MAX_SITTING_ROWS = 8
MAX_RUNTIME_BYTES = 512 * 1024
TUPLE = "$tuple"
OBJECT = "$object"


def wire(value):
    """Detach data while keeping the tuple/list distinction the real pins compare."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if is_dataclass(value):
        return wire({f.name: getattr(value, f.name) for f in fields(value)})
    if isinstance(value, Mapping):
        if any(type(key) is not str or key in (TUPLE, OBJECT) for key in value):
            return {OBJECT: [[wire(key), wire(item)] for key, item in value.items()]}
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return {TUPLE: [wire(item) for item in value]}
    if isinstance(value, list):
        return [wire(item) for item in value]
    raise ValueError("unsupported fence snapshot value")


def unwire(value):
    if isinstance(value, dict):
        if set(value) == {TUPLE}:
            return tuple(unwire(item) for item in value[TUPLE])
        if set(value) == {OBJECT}:
            return {unwire(key): unwire(item) for key, item in value[OBJECT]}
        return {key: unwire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unwire(item) for item in value]
    return value


def frame(runtime, request, trace):
    offer = getattr(runtime, "_live7_ui_offer", None)
    goal = runtime._active
    session = getattr(runtime, "_skill_session", None)
    published = None
    if offer:
        published = dict(
            pin=trace.get("current_pin"),
            native=offer.get("replay_native"),
            world_delta=offer["facts"].get("prior_world_delta"),
            native_motor=offer["facts"].get("native_motor"),
            decision_until=offer.get("decision_until"),
        )
    return wire(dict(
        request=dict(generation_id=request.generation_id, goal_id=request.goal_id,
                     goal_revision=request.goal_revision, ui_offer_pin=request.ui_offer_pin,
                     superseded=trace.get("superseded") is True),
        runtime=dict(status=runtime._status, busy=runtime._action_busy,
                     inflight=runtime._inflight_generation, session_present=session is not None,
                     goal=dict(goal_id=goal.goal_id, revision=goal.revision) if goal else None,
                     ui_view_miss=getattr(runtime, "_live7_ui_view_miss", None)),
        published=published, sampled=trace,
    ))


def record(runtime, request, site, miss, trace):
    """Capture only the existing fence sample, even if it was refused before a model call."""
    pin = request.ui_offer_pin or {}
    receiver = (pin.get("pins") or {}).get("receiver_token")
    previous, count, total = getattr(runtime, "_menu_fence_replay_budget", (None, 0, 0))
    if receiver != previous:
        count = 0
    if count > MAX_SITTING_ROWS or total >= MAX_RUNTIME_BYTES:
        return
    row = dict(ev="menu_fence_sample", schema=SCHEMA, receiver=receiver, generation=request.generation_id,
               site=site, reply_received=site == "after_reply", result=miss)
    if count == MAX_SITTING_ROWS:
        row["unavailable"] = "sitting_limit"
    else:
        try:
            row["frame"] = frame(runtime, request, trace)
            if len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_ROW:
                row.pop("frame")
                row["unavailable"] = "row_limit"
        except (ValueError, TypeError, OverflowError, RecursionError, AttributeError, KeyError):
            row.pop("frame", None)
            row["unavailable"] = "snapshot_unrepresentable"
    size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
    if total + size > MAX_RUNTIME_BYTES:
        runtime._menu_fence_replay_budget = (receiver, count, MAX_RUNTIME_BYTES)
        return
    runtime._menu_fence_replay_budget = (receiver, count + 1, total + size)
    runtime._journal(row)


def replay(row):
    """Run the production predicate against a frozen, already read-back native view."""
    from arti_stardew_generic_ui import GenericUiMixin
    from arti_stardew_ui_pad import UiPadView

    if row.get("schema") != SCHEMA or row.get("ev") != "menu_fence_sample" or "frame" not in row:
        raise ValueError("no complete menu fence sample")
    data = unwire(row["frame"])
    recorded = data["runtime"]
    sampled = data["sampled"]
    published = data["published"]
    pin = published["pin"] if published else None
    request = dict(data["request"])
    superseded = Event()
    if request["superseded"]:
        superseded.set()
    request["superseded"] = superseded
    offer = None
    if published:
        # Early refusal can have an available offer with no pin comparison.
        pin = pin or dict(pins={}, menu=None, snapped_id=None, actions={})
        offer = dict(stable=(pin["pins"], pin["menu"]), snapped_id=pin["snapped_id"],
                     actions=pin["actions"], facts=dict(prior_world_delta=published["world_delta"],
                                                        native_motor=published["native_motor"]))
        if published["decision_until"] is not None:
            offer["decision_until"] = published["decision_until"]
    session = None
    if recorded["session_present"]:
        delta = sampled.get("world_delta")
        session = NS(current=NS(state=dict(generic_receipt_observed=delta is not None, generic_readback=delta)))
    view = UiPadView(**sampled["native_view"]) if sampled.get("native_view") is not None else None
    shim = NS(_live7_ui_offer=offer, _skill_session=session, _status=recorded["status"],
              _action_busy=recorded["busy"], _inflight_generation=recorded["inflight"],
              _active=NS(**recorded["goal"]) if recorded["goal"] is not None else None,
              _live7_active=lambda: sampled["live7_active"],
              _live7_unknown_ui_view=lambda session: view,
              _live7_ui_view_miss=recorded["ui_view_miss"],
              _live7_motor_facts=lambda: sampled["native_motor"])
    return GenericUiMixin._live7_ui_fence_miss(shim, NS(**request), clock=lambda: sampled["checked_at"])
