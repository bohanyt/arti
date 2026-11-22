"""G1 GameDecision.v1 — codec JSON ketat untuk seluruh respons model.

Modul murni: tidak ada HTTP, provider, subprocess, command inbox, atau wiring
bridge. WEB-1 (`arti_brain_contract`) tetap kontrak terpisah dan tidak
digeneralisasi di sini.

Sumber desain: Issue #133 G1 + architecture lock CT-20260908-007/009.
"""

from __future__ import annotations

import json
from dataclasses import InitVar, asdict, dataclass
from typing import Literal
from arti_stardew_observation import KNOWN_DOMAINS

# Primitives v2 WS-G6 world_interact kinds (copy of the command contract table; parity-tested).
WORLD_EXTRAS_KINDS = frozenset({
    "animal_shop", "blacksmith", "community_note", "tv", "mailbox", "npc_gift", "pet", "animal_pet", "animal_milk", "animal_shear", "horse_mount", "horse_dismount", "shake_tree", "harvest_crop",
    "machine_collect", "machine_insert", "hay_hopper", "hay_place", "ladder_down", "shaft", "mine_elevator", "bus", "minecart",
    "place_item", "shop_counter", "shipping_bin", "ladder_up",  # FIXB17 D-B5-0: the Mines up-ladder (exit leg)
    "kitchen",  # W1 gap B3 S30: the actor's own kitchen (standalone cooking menu)
    "quest_delivery",  # W1 gap B4 S28: the native quest hand-in (never a gift)
})

SCHEMA_VERSION = 1
ENVELOPE_MAX_BYTES = 16 * 1024
SPEECH_MAX_CHARS = 800
WAIT_MS_MAX = 5000
EVIDENCE_REFS_MAX = 16
REASON_MAX_CHARS = 200
QUESTION_MAX_CHARS = 400
OPERATION_MAX_CHARS = 80
REF_MAX_CHARS = 120
GOAL_REF_MAX_CHARS = 40  # mind slice 1: optional id of her own open goal (arti_mind), never authority
MAX_OBJECT_DEPTH = 3  # root + choice + args
CHOICE_KINDS = frozenset(
    {"speak", "act", "inspect", "lookup", "wait", "ask_owner", "finish"}
)
G10_ADMITTED_KINDS = frozenset({"speak", "wait", "ask_owner", "finish"})
G10_UNAVAILABLE_KINDS = frozenset({"act", "inspect", "lookup"})
INSPECT_SOURCES = frozenset({"state", "text", "visual"})
G12_INSPECT_ADMITTED_SOURCES = frozenset({"state", "text"})
_ROOT_KEYS = frozenset(
    {
        "v",
        "request_nonce",
        "goal_id",
        "goal_revision",
        "context_ref",
        "speech",
        "choice",
    }
)
_FORBIDDEN_ARG_KEYS = frozenset({"method", "path", "keycode"})


class DecisionCodecError(ValueError):
    """Keputusan model tidak sah; runtime harus zero-action."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SpeakChoice:
    kind: Literal["speak"] = "speak"


@dataclass(frozen=True)
class WaitChoice:
    reason: str
    ms: int
    kind: Literal["wait"] = "wait"


@dataclass(frozen=True)
class AskOwnerChoice:
    kind: Literal["ask_owner"] = "ask_owner"
    basis: Literal["optional", "permission", "prerequisite"] = "prerequisite"
    wait_ms: int | None = None


@dataclass(frozen=True)
class FinishChoice:
    kind: Literal["finish"] = "finish"
    scope: Literal["goal", "activity"] = "goal"


@dataclass(frozen=True)
class ActChoice:
    operation: str
    target_ref: str | None
    args: dict[str, object]
    evidence_refs: tuple[str, ...]
    kind: Literal["act"] = "act"


@dataclass(frozen=True)
class InspectChoice:
    source: str
    question: str
    scope: str | None
    kind: Literal["inspect"] = "inspect"


@dataclass(frozen=True)
class LookupChoice:
    question: str
    kind: Literal["lookup"] = "lookup"


Choice = (
    SpeakChoice
    | WaitChoice
    | AskOwnerChoice
    | FinishChoice
    | ActChoice
    | InspectChoice
    | LookupChoice
)


@dataclass(frozen=True)
class ActivityIntention:
    """Model proposal only; no permissions, progress or completion fields."""

    activity: str
    purpose: str
    target: str | None
    next_step: str


@dataclass(frozen=True)
class GameDecision:
    v: int
    request_nonce: str
    goal_id: str
    goal_revision: int
    context_ref: str | None
    speech: str
    choice: Choice
    activity: ActivityIntention | None = None
    goal_ref: str | None = None
    plan: tuple[dict, ...] = ()  # ADR 140: optional offered calls, including choice as the first step
    # ADR 155: local telemetry, never a dataclass/wire field (including asdict).
    dropped: InitVar[tuple[str, ...]] = ()
    catatan: InitVar[dict[str, str] | None] = None  # ADR 221: optional authored note, independent of choice

    def __post_init__(self, dropped: tuple[str, ...], catatan: dict[str, str] | None) -> None:
        object.__setattr__(self, "dropped", dropped)
        if catatan is not None:
            object.__setattr__(self, "catatan", catatan)


def g10_choice_admitted(decision: GameDecision) -> bool:
    return decision.choice.kind in G10_ADMITTED_KINDS


def inspect_source_admitted(source: str) -> bool:
    """G1-2: inspect hanya state|text. visual tetap unavailable."""
    return source in G12_INSPECT_ADMITTED_SOURCES


_OPERATION_ALIASES = {"use_tool": "world_key.use_tool", "move_cardinal": "world_key.move_cardinal"}


def runtime_choice_admitted(decision: GameDecision) -> bool:
    """G1 choices plus the explicitly bound physical operations."""
    if decision.choice.kind in G10_ADMITTED_KINDS:
        return True
    if decision.choice.kind == "inspect":
        return inspect_source_admitted(decision.choice.source)
    if isinstance(decision.choice, ActChoice):
        if decision.choice.operation == "animal_routine":
            return decision.choice.target_ref is None and decision.choice.args == {}
        if decision.choice.operation in {"world_key.move_cardinal", "approach_target"}:
            return True
        if decision.choice.operation == "ui_key":
            # WS2: model keys only (never choose/confirm; level-up/profession/shipping are runtime-owned).
            args = decision.choice.args
            base = {"receiver_token", "menu_type", "kind", "key", "target_token"}
            key = args.get("key") if isinstance(args, dict) else None
            expected = base | {"option_index"} if key == "answer" else base
            return (decision.choice.target_ref is None and isinstance(args, dict)
                    and key in {"advance", "answer", "cancel", "close"}
                    and set(args) == expected
                    and all(isinstance(args.get(k), str) for k in base)
                    and ("option_index" not in args or _plain_int(args["option_index"])))
        if decision.choice.operation in {"ui_inspect", "ui_nav", "ui_press", "pad_button"}:
            # Primitives v2 WS-G1: menu layer. Pins are copied from the offered ui_pad block (the runtime
            # re-checks them against the offer); reserved G2/G3 payloads are never admitted here.
            args = decision.choice.args
            if decision.choice.target_ref is not None or not isinstance(args, dict):
                return False
            op = decision.choice.operation
            base = {"receiver_token", "menu_type", "context", "target_token"}
            if op == "ui_inspect":
                return args == {}
            if op == "pad_button":
                return set(args) == {"button"} and isinstance(args["button"], str)
            if "handle" in args:
                if not isinstance(args["handle"], str):
                    return False
                if op == "ui_nav":
                    return ((set(args) == {"handle", "dir"} and isinstance(args["dir"], str))
                            or (set(args) == {"handle", "destination_id"} and _plain_int(args["destination_id"])))
                extras = {k for k in ("qty", "half", "stack") if k in args}
                return (len(extras) <= 1 and set(args) in ({"handle", "btn"} | extras,
                        {"handle", "btn", "tab"} | extras, {"handle", "btn", "choice"})
                        and isinstance(args.get("btn"), str)
                        and ("tab" not in args or isinstance(args["tab"], str))
                        and ("qty" not in args or _plain_int(args["qty"]))
                        and all(args[k] is True for k in extras - {"qty"})
                        and ("choice" not in args or (isinstance(args["choice"], str)
                            and args["choice"].isascii() and args["choice"].isdigit() and len(args["choice"]) <= 2)))
            if op == "ui_nav":
                return (all(isinstance(args.get(k), str) for k in base)
                        and (set(args) == base | {"dir"} and isinstance(args["dir"], str)
                             or set(args) == base | {"destination_id"} and _plain_int(args["destination_id"])))
            press = base | {"btn", "snapped_id"}
            # S26 gap8c L350: one native building payload or the same receiver's G3 name. Owner/request,
            # live offer and native pin checks still gate the admitted shape.
            animal_payload = ("building" if args.get("context") == "purchase_animals" else
                              "text" if args.get("context") == "naming" else None)
            if args.get("menu_type") == "PurchaseAnimalsMenu" and animal_payload is not None and animal_payload in args:
                return (set(args) == press | {animal_payload} and args.get("btn") == "A"
                        and _plain_int(args.get("snapped_id"))
                        and all(isinstance(args.get(k), str) and args[k] for k in base | {animal_payload}))
            # Primitives v2 WS-G3: the event-question "choice" (a digit string). WS-G2: at most one economy payload
            # (qty plain int / half true / stack true), never with a choice; the runtime then requires the offered one.
            extras = {k for k in ("qty", "half", "stack") if k in args}
            return (len(extras) <= 1 and set(args) in (press | extras, press | extras | {"tab"}, press | {"choice"})
                    and _plain_int(args.get("snapped_id"))
                    and ("qty" not in args or _plain_int(args["qty"]))
                    and all(args[k] is True for k in extras - {"qty"})
                    and all(isinstance(args[k], str) for k in set(args) - {"snapped_id"} - extras)
                    and ("choice" not in args or (args["choice"].isascii() and args["choice"].isdigit()
                                                  and len(args["choice"]) <= 2)))
        if decision.choice.operation == "economy_transfer":
            # Primitives v2 WS-G2: one offered bin ship / chest take-out (container_transfer + mode), copied exactly.
            args = decision.choice.args
            required = {"receiver_token", "container_token", "readback_token", "source_fact_id", "target_token",
                        "slot", "qualified_item_id", "stack", "quality", "owner_ceiling", "mode"}
            return (decision.choice.target_ref is None and isinstance(args, dict) and set(args) == required
                    and args.get("mode") in ("ship_one", "ship_stack", "take_out")
                    and all(_plain_int(args.get(k)) for k in ("slot", "stack", "quality", "owner_ceiling"))
                    and all(isinstance(args.get(k), str) for k in required - {"slot", "stack", "quality", "owner_ceiling"}))
        if decision.choice.operation == "container_exit":
            args=decision.choice.args
            return (decision.choice.target_ref is None
                and set(args)=={"receiver_token","container_token","source_fact_id","readback_token","target_token"}
                and all(isinstance(v,str) for v in args.values()))
        if decision.choice.operation == "container_transfer":
            args=decision.choice.args
            required={"receiver_token","container_token","readback_token","source_fact_id","target_token",
                      "slot","qualified_item_id","stack","quality","owner_ceiling"}
            return (decision.choice.target_ref is None
                    and (set(args) in (required,required|{"quantity"})
                         or set(args) == required|{"mode"} and args.get("mode") == "quick")
                    and ("quantity" not in args or _plain_int(args["quantity"]) and args["quantity"]==1)
                    and all(_plain_int(args.get(k)) for k in ("slot","stack","quality","owner_ceiling"))
                    and all(isinstance(args.get(k),str) for k in
                            ("receiver_token","container_token","readback_token","source_fact_id","target_token","qualified_item_id")))
        if decision.choice.operation == "event_action":
            args = decision.choice.args
            return (decision.choice.target_ref is None and set(args) == {"handle", "btn"}
                    and isinstance(args.get("handle"), str) and args.get("btn") == "A")
        if decision.choice.operation == "world_interact":
            # WS3a: kind is mandatory; a door also names its destination (target_location_id).
            args = decision.choice.args
            base = {"target_token","source_fact_id","tile_x","tile_y","actor_tile_x","actor_tile_y","facing","kind"}
            # Primitives v2 WS-G6: the world extras kinds (ladder_down names its destination like a door). The codec
            # never imports the command module: the copy is pinned by the WS-G6 parity test.
            door = args.get("kind") in ("door", "ladder_down", "animal_house_route")
            return (decision.choice.target_ref is None
                    and args.get("kind") in {"chest", "door", "forage", "npc", "eat", "animal_house_route", "generic_action", "generic_use"} | WORLD_EXTRAS_KINDS
                    and set(args) == (base | {"target_location_id"} if door else base)
                    and all(_plain_int(args.get(k)) for k in ("tile_x","tile_y","actor_tile_x","actor_tile_y","facing"))
                    and all(isinstance(args.get(k),str) for k in ("target_token","source_fact_id"))
                    and (not door or isinstance(args.get("target_location_id"), str)))
        if decision.choice.operation == "move_through":
            # WS3a walk-onto warp; the runtime fills the observed target (only ticks is the model's).
            args = decision.choice.args
            base = {"direction","ticks","kind","source_fact_id","target_token","tile_x","tile_y","actor_tile_x","actor_tile_y"}
            warp = args.get("kind") == "warp"  # WS3b: kind "bed" carries no destination
            return (decision.choice.target_ref is None and args.get("kind") in {"warp", "bed"}
                    and set(args) == (base | {"target_location_id"} if warp else base)
                    and all(_plain_int(args.get(k)) for k in ("ticks","tile_x","tile_y","actor_tile_x","actor_tile_y"))
                    and all(isinstance(args.get(k),str) for k in ("direction","source_fact_id","target_token"))
                    and (not warp or isinstance(args.get("target_location_id"), str)))
        if decision.choice.operation == "select_item":
            args = decision.choice.args
            return (decision.choice.target_ref is None
                    and set(args) == {"receiver_token", "target_token", "slot", "qualified_item_id", "stack", "quality"}
                    and all(_plain_int(args.get(k)) for k in ("slot", "stack", "quality"))
                    and all(isinstance(args.get(k), str) for k in ("receiver_token", "target_token", "qualified_item_id")))
        if decision.choice.operation == "world_key.use_tool":
            args = decision.choice.args
            return (decision.choice.target_ref is None
                    and set(args) == {"ticks", "slot", "qualified_item_id", "tile_x", "tile_y", "facing"}
                    and all(_plain_int(args.get(k)) for k in ("ticks", "slot", "tile_x", "tile_y", "facing"))
                    and isinstance(args.get("qualified_item_id"), str))
        if decision.choice.operation == "reflex_fish":
            # Primitives v2 WS-G4: the runtime filled the four contract keys from the offered target (defaults).
            args = decision.choice.args
            return (decision.choice.target_ref is None and isinstance(args, dict)
                    and set(args) == {"max_s", "cast_power", "treasure", "hook_retries"}
                    and _plain_int(args.get("max_s")) and _plain_int(args.get("hook_retries"))
                    and isinstance(args.get("treasure"), bool)
                    and isinstance(args.get("cast_power"), (int, float)) and not isinstance(args.get("cast_power"), bool))
        if decision.choice.operation == "reflex_combat":
            # Primitives v2 WS-G5: the runtime filled the four contract keys from the offered target (defaults).
            args = decision.choice.args
            return (decision.choice.target_ref is None and isinstance(args, dict)
                    and {"max_s", "hp_floor_pct", "max_kills", "retreat"} <= set(args)
                    and not set(args) - {"max_s", "hp_floor_pct", "max_kills", "retreat", "target_kind", "escape",
                                         "disengage"}
                    and ("disengage" not in args or args["disengage"] is True and "escape" not in args
                         and args.get("retreat") is True and args.get("max_kills") == 1
                         and _plain_int(args.get("max_s")) and 1 <= args["max_s"] <= 30)
                    and ("escape" not in args or args["escape"] is True
                         and args.get("retreat") is False and args.get("max_kills") == 1
                         and _plain_int(args.get("max_s")) and 1 <= args["max_s"] <= 4)
                    and all(_plain_int(args.get(k)) for k in ("max_s", "hp_floor_pct", "max_kills"))
                    and isinstance(args.get("retreat"), bool)
                    and args.get("target_kind", "slime") in ("slime", "monster"))
        if decision.choice.operation == "reflex_charge":
            args = decision.choice.args
            return (decision.choice.target_ref is None and isinstance(args, dict)
                    and set(args) == {"target_level", "max_ticks"}
                    and all(_plain_int(args.get(k)) for k in ("target_level", "max_ticks")))
        if decision.choice.operation == "water_once":
            args = decision.choice.args
            return (
                decision.choice.target_ref is None
                and set(args) == {"tile_x", "tile_y"}
                and _plain_int(args.get("tile_x"))
                and _plain_int(args.get("tile_y"))
            )
    return False


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise DecisionCodecError("duplicate_keys")
        seen.add(key)
        out[key] = value
    return out


def _reject_constant(name: str) -> None:
    raise DecisionCodecError("non_json_constant")


def _has_disallowed_controls(text: str, *, allow_nl_tab: bool) -> bool:
    for ch in text:
        code = ord(ch)
        if code < 32:
            if allow_nl_tab and ch in "\n\t":
                continue
            return True
        if code == 127 or (0x80 <= code <= 0x9F):
            return True
    return False


def _check_structure(obj: object, depth: int, *, allow_array: bool = False, path: tuple = ()) -> None:
    if isinstance(obj, list):
        if not allow_array:
            raise DecisionCodecError("array_not_allowed")
        if depth > MAX_OBJECT_DEPTH and path != ("plan", "presses"):
            raise DecisionCodecError("too_deep")
        for item in obj:
            _check_structure(item, depth + 1, allow_array=False, path=path)
        return
    if isinstance(obj, dict):
        handle_object = (path == ("choice", "args", "observed_target")
                         and set(obj) == {"handle"} and isinstance(obj["handle"], str))
        if depth > MAX_OBJECT_DEPTH and not handle_object:
            raise DecisionCodecError("too_deep")
        for key, value in obj.items():
            nested_array = key == "evidence_refs" or path == () and key == "plan" or path == ("plan",) and key == "presses"
            _check_structure(value, depth + 1, allow_array=nested_array, path=path + (key,))


def _to_text(raw: str | bytes) -> str:
    if isinstance(raw, bytes):
        if len(raw) > ENVELOPE_MAX_BYTES:
            raise DecisionCodecError("too_large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecisionCodecError("malformed_json") from exc
        return text
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > ENVELOPE_MAX_BYTES:
            raise DecisionCodecError("too_large")
        return raw
    raise DecisionCodecError("wrong_type")


def _req_str(value: object, *, code: str = "wrong_type") -> str:
    if not isinstance(value, str):
        raise DecisionCodecError(code)
    return value


def _bounded_str(
    value: object, *, max_chars: int, empty_ok: bool = False, allow_nl_tab: bool = True
) -> str:
    text = _req_str(value)
    if _has_disallowed_controls(text, allow_nl_tab=allow_nl_tab):
        raise DecisionCodecError("control_character")
    if not empty_ok and not text.strip():
        raise DecisionCodecError("empty_text")
    if len(text) > max_chars:
        raise DecisionCodecError("speech_too_long" if max_chars == SPEECH_MAX_CHARS else "too_long")
    return text


def _validate_choice(obj: object) -> Choice:
    if not isinstance(obj, dict):
        raise DecisionCodecError("wrong_type")
    kind = obj.get("kind")
    if not isinstance(kind, str):
        raise DecisionCodecError("wrong_type")
    if kind not in CHOICE_KINDS:
        raise DecisionCodecError("unknown_choice")
    keys = set(obj)
    if kind == "speak":
        if keys != {"kind"}:
            raise DecisionCodecError(
                "unknown_field" if keys - {"kind"} else "missing_field"
            )
        return SpeakChoice()
    if kind == "ask_owner":
        if keys == {"kind"}: return AskOwnerChoice()
        if keys != {"kind", "basis", "wait_ms"}: raise DecisionCodecError("unknown_field")
        basis, delay = obj["basis"], obj["wait_ms"]
        if not isinstance(basis,str) or basis not in {"optional","permission","prerequisite"}:
            raise DecisionCodecError("invalid_question_basis")
        if delay is not None and (type(delay) is not int or not 1 <= delay <= 30000 or basis != "optional"):
            raise DecisionCodecError("invalid_optional_wait")
        return AskOwnerChoice(basis=basis,wait_ms=delay)
    if kind == "finish":
        if keys - {"kind", "scope"}:
            raise DecisionCodecError("unknown_field")
        scope = obj.get("scope", "goal")
        if not isinstance(scope, str) or scope not in {"goal", "activity"}:
            raise DecisionCodecError("invalid_finish_scope")
        return FinishChoice(scope=scope)
    if kind == "wait":
        expected = {"kind", "reason", "ms"}
        if keys != expected:
            raise DecisionCodecError(
                "unknown_field" if keys - expected else "missing_field"
            )
        reason = _bounded_str(
            obj["reason"], max_chars=REASON_MAX_CHARS, allow_nl_tab=False
        )
        ms = obj["ms"]
        if not _plain_int(ms) or ms < 0 or ms > WAIT_MS_MAX:
            raise DecisionCodecError("wait_ms")
        return WaitChoice(reason=reason, ms=ms)
    if kind == "lookup":
        expected = {"kind", "question"}
        if keys != expected:
            raise DecisionCodecError(
                "unknown_field" if keys - expected else "missing_field"
            )
        question = _bounded_str(obj["question"], max_chars=QUESTION_MAX_CHARS)
        return LookupChoice(question=question)
    if kind == "inspect":
        expected = {"kind", "source", "question", "scope"}
        if keys != expected:
            raise DecisionCodecError(
                "unknown_field" if keys - expected else "missing_field"
            )
        source = obj["source"]
        if not isinstance(source, str) or source not in INSPECT_SOURCES:
            raise DecisionCodecError("inspect_source")
        question = _bounded_str(obj["question"], max_chars=QUESTION_MAX_CHARS)
        scope = obj["scope"]
        if scope is not None:
            scope = _bounded_str(scope, max_chars=REF_MAX_CHARS, empty_ok=True)
            if source == 'state' and scope.strip() == 'play':
                scope = 'state'  # exact read-only alias; no domain or mutation authority added
            if source == 'state' and scope.strip() not in {'', 'state', *KNOWN_DOMAINS}:
                raise DecisionCodecError('inspect_scope')
        return InspectChoice(source=source, question=question, scope=scope)
    expected = {"kind", "operation", "target_ref", "args", "evidence_refs"}
    if keys == expected - {"evidence_refs"}:
        obj = dict(obj, evidence_refs=[])
        keys = set(obj)
    if keys != expected:
        raise DecisionCodecError(
            "unknown_field" if keys - expected else "missing_field"
        )
    operation = _bounded_str(
        obj["operation"], max_chars=OPERATION_MAX_CHARS, allow_nl_tab=False
    )
    # Unambiguous short names the model writes for catalog keys (live 2026-10-06 attempt 9x: "use_tool").
    operation = _OPERATION_ALIASES.get(operation, operation)
    target_ref = obj["target_ref"]
    if target_ref is not None:
        target_ref = _bounded_str(target_ref, max_chars=REF_MAX_CHARS, empty_ok=False)
    args = obj["args"]
    if not isinstance(args, dict):
        raise DecisionCodecError("wrong_type")
    copied_args: dict[str, object] = {}
    for key, value in args.items():
        if not isinstance(key, str) or not key:
            raise DecisionCodecError("wrong_type")
        if key.casefold() in _FORBIDDEN_ARG_KEYS:
            raise DecisionCodecError("forbidden_arg")
        if isinstance(value, (list, dict)):
            # Compatibility input only; canonical prompts use flat scalar args.
            # No other nested model arg/object or native-pin object is accepted.
            if (operation in {"ui_nav", "ui_press"} and key == "observed_target"
                    and isinstance(value, dict) and set(value) == {"handle"}
                    and isinstance(value["handle"], str) and 0 < len(value["handle"]) <= REF_MAX_CHARS):
                copied_args[key] = dict(value)
                continue
            raise DecisionCodecError("array_not_allowed")
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            raise DecisionCodecError("non_json_constant")
        if isinstance(value, int) and not isinstance(value, bool):
            copied_args[key] = value
        elif isinstance(value, (str, bool)) or value is None:
            copied_args[key] = value
        elif isinstance(value, float):
            copied_args[key] = value
        else:
            raise DecisionCodecError("wrong_type")
    refs = obj["evidence_refs"]
    if not isinstance(refs, list):
        raise DecisionCodecError("wrong_type")
    if len(refs) > EVIDENCE_REFS_MAX:
        raise DecisionCodecError("too_many_refs")
    copied_refs: list[str] = []
    for item in refs:
        if not isinstance(item, str) or not item.strip() or len(item) > REF_MAX_CHARS:
            raise DecisionCodecError("wrong_type")
        copied_refs.append(item)
    if operation == "inspect" and target_ref is None and copied_args == {} and obj["evidence_refs"] == []:
        return InspectChoice(source="state", question="Refresh current state and event offers.", scope="state")
    return ActChoice(
        operation=operation,
        target_ref=target_ref,
        args=copied_args,
        evidence_refs=tuple(copied_refs),
    )


def _validate_decision_object(obj: dict[str, object]) -> GameDecision:
    extra = set(obj) - _ROOT_KEYS - {"activity", "goal_ref", "plan", "catatan"}
    missing = _ROOT_KEYS - set(obj)
    if extra:
        raise DecisionCodecError("unknown_field")
    if missing:
        raise DecisionCodecError("missing_field")
    version = obj["v"]
    if not _plain_int(version) or version != SCHEMA_VERSION:
        raise DecisionCodecError("schema_version")
    nonce = _bounded_str(obj["request_nonce"], max_chars=REF_MAX_CHARS, allow_nl_tab=False)
    goal_id = _bounded_str(obj["goal_id"], max_chars=REF_MAX_CHARS, allow_nl_tab=False)
    revision = obj["goal_revision"]
    if not _plain_int(revision) or revision < 0:
        raise DecisionCodecError("goal_revision")
    context_ref = obj["context_ref"]
    if context_ref is not None:
        context_ref = _bounded_str(
            context_ref, max_chars=REF_MAX_CHARS, allow_nl_tab=False
        )
    speech = _bounded_str(
        obj["speech"], max_chars=SPEECH_MAX_CHARS, empty_ok=True, allow_nl_tab=True
    )
    choice = _validate_choice(obj["choice"])
    plan = obj.get("plan")
    plan = [] if plan is None else plan
    if not isinstance(plan, list) or len(plan) > 3:
        raise DecisionCodecError("plan_length")
    dropped: list[str] = []
    kept_plan: list[dict] = []
    for index, step in enumerate(plan):
        if (not isinstance(step, dict) or not {"name", "arg"} <= set(step)
                or not set(step) <= {"name", "arg", "n", "repeat", "presses"}):
            raise DecisionCodecError("plan_fields")
        _bounded_str(step["name"], max_chars=OPERATION_MAX_CHARS)
        if "presses" in step and step["name"] != "ui_action":
            # ADR 163: strict envelopes require this nullable key on every step.
            # An absent menu sequence is a no-op; a populated stray field still owns ADR 155's FAULT.
            stray = step["presses"] is not None and step["presses"] != []
            step = {k: v for k, v in step.items() if k != "presses"}
            if stray:
                dropped.append(f"plan[{index}].presses")
        kept_plan.append(step)
        if step["arg"] is not None:
            _bounded_str(step["arg"], max_chars=800)
        if step.get("n") is not None and (not _plain_int(step["n"]) or step["n"] <= 0):
            raise DecisionCodecError("plan_quantity")
        repeat = step.get("repeat", 1)
        if not _plain_int(repeat) or not 1 <= repeat <= 4 or repeat > 1 and step["name"] != "ui_action":
            raise DecisionCodecError("plan_repeat")
        presses = step.get("presses")
        if presses == [] and step["name"] == "ui_action":
            step = dict(step, presses=None)
            kept_plan[-1] = step
            presses = None
        if presses is not None:
            if (step["name"] != "ui_action" or repeat != 1 or not isinstance(presses, list)
                    or not 1 <= len(presses) <= 4 or presses[0] != step["arg"]):
                raise DecisionCodecError("plan_presses")
            for press in presses:
                _bounded_str(press, max_chars=800)
    plan = kept_plan
    activity = obj.get("activity")
    if activity is not None:
        if not isinstance(activity, dict):
            raise DecisionCodecError("wrong_type")
        if set(activity) != {"activity", "purpose", "target", "next_step"}:
            raise DecisionCodecError("activity_fields")
        activity = ActivityIntention(
            activity=_bounded_str(activity["activity"], max_chars=80),
            purpose=_bounded_str(activity["purpose"], max_chars=160),
            target=(None if activity["target"] is None else
                    _bounded_str(activity["target"], max_chars=120)),
            next_step=_bounded_str(activity["next_step"], max_chars=160),
        )
    goal_ref = obj.get("goal_ref")
    if goal_ref is not None:
        goal_ref = _bounded_str(goal_ref, max_chars=GOAL_REF_MAX_CHARS, allow_nl_tab=False)
    # An invalid optional note is discarded; the existing choice validator above
    # and its result are unchanged (fix117B owns choice-contract changes).
    catatan = obj.get('catatan')
    if catatan is not None:
        try:
            if (not isinstance(catatan, dict) or set(catatan) != {'kind', 'text'}
                    or catatan.get('kind') not in ('question', 'guess')):
                raise DecisionCodecError('note_fields')
            catatan = dict(kind=catatan['kind'], text=_bounded_str(catatan['text'], max_chars=100, allow_nl_tab=False))
        except DecisionCodecError:
            catatan = None
            dropped.append('catatan')
    return GameDecision(
        v=version,
        request_nonce=nonce,
        goal_id=goal_id,
        goal_revision=revision,
        context_ref=context_ref,
        speech=speech,
        choice=choice,
        activity=activity,
        goal_ref=goal_ref,
        plan=tuple(dict(s) for s in plan),
        dropped=tuple(dropped),
        catatan=catatan,
    )


def decode_game_decision(raw: str | bytes) -> GameDecision:
    """Parse seluruh respons asisten menjadi GameDecision.v1.

    Tidak mengekstrak JSON yang terselip di prosa, tidak eval, tidak memperbaiki.
    """
    text = _to_text(raw)
    if text.startswith("\ufeff"):
        text = text[1:]
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
        strict=True,
    )
    try:
        obj, end = decoder.raw_decode(text)
    except DecisionCodecError:
        raise
    except json.JSONDecodeError as exc:
        raise DecisionCodecError("malformed_json") from exc
    except ValueError as exc:
        raise DecisionCodecError("malformed_json") from exc
    if end < len(text) and text[end:].strip():
        raise DecisionCodecError("trailing_content")
    if isinstance(obj, list):
        raise DecisionCodecError("array_not_allowed")
    if not isinstance(obj, dict):
        raise DecisionCodecError("not_object")
    _check_structure(obj, 1)
    decision = _validate_decision_object(obj)
    # Local numeric telemetry only, deliberately excluded from the wire schema.
    choice = obj.get("choice") or {}
    normalized = int(choice.get("kind") == "act" and "evidence_refs" not in choice)
    normalized += sum(s.get("name") == "ui_action" and s.get("presses") == [] for s in obj.get("plan") or ())
    object.__setattr__(decision, "normalized", normalized)
    return decision


def encode_game_decision(decision: GameDecision) -> str:
    choice = decision.choice
    if isinstance(choice, SpeakChoice):
        choice_obj: dict[str, object] = {"kind": "speak"}
    elif isinstance(choice, AskOwnerChoice):
        choice_obj = {"kind": "ask_owner"}
        if choice.basis != "prerequisite" or choice.wait_ms is not None:
            choice_obj.update(basis=choice.basis,wait_ms=choice.wait_ms)
    elif isinstance(choice, FinishChoice):
        choice_obj = {"kind": "finish"}
        if choice.scope != "goal":
            choice_obj["scope"] = choice.scope
    elif isinstance(choice, WaitChoice):
        choice_obj = {"kind": "wait", "reason": choice.reason, "ms": choice.ms}
    elif isinstance(choice, LookupChoice):
        choice_obj = {"kind": "lookup", "question": choice.question}
    elif isinstance(choice, InspectChoice):
        choice_obj = {
            "kind": "inspect",
            "source": choice.source,
            "question": choice.question,
            "scope": choice.scope,
        }
    elif isinstance(choice, ActChoice):
        choice_obj = {
            "kind": "act",
            "operation": choice.operation,
            "target_ref": choice.target_ref,
            "args": dict(choice.args),
            "evidence_refs": list(choice.evidence_refs),
        }
    else:
        raise DecisionCodecError("unknown_choice")
    payload = {
        "v": SCHEMA_VERSION,
        "request_nonce": decision.request_nonce,
        "goal_id": decision.goal_id,
        "goal_revision": decision.goal_revision,
        "context_ref": decision.context_ref,
        "speech": decision.speech,
        "choice": choice_obj,
    }
    if decision.activity is not None:
        payload["activity"] = asdict(decision.activity)
    if decision.goal_ref is not None:
        payload["goal_ref"] = decision.goal_ref
    if decision.plan:
        payload["plan"] = [dict(s) for s in decision.plan]
    if getattr(decision, 'catatan', None) is not None:
        payload['catatan'] = dict(decision.catatan)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
