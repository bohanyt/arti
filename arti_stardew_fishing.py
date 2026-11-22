"""Primitives v2 WS-G4: the runtime side of the fishing reflex (pure; no IO, no store, no input path).

The reflex itself lives in the MOD (``FishingReflexRunner``): one ``reflex_fish`` call casts, hooks, drives the
BobberBar and dismisses the catch, then returns ONE verdict ``FishCaught`` / ``FishEscaped`` / ``NoBite`` /
``Aborted(reason)``. This module only:

* parses the owner objective (authenticated owner grant only): ``tangkap satu ikan`` / ``catch one fish``;
* reads the mod's ``ui.context`` facts (capability ``reflex_fish`` + ``fishing``) into the offered target and the
  ``water_edge`` approach entries (stand beside water, face it);
* fills the model's reflex args from the defaults (only the four contract keys, each bounded);
* keeps the objective accounting from settled reflex results and decides the deterministic finish
  (``fishing_rule`` -> ``observed_fish_caught``).

Evidence ceiling: UNIT_TESTED.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

import arti_stardew_command as command

OBJ_CATCH_FISH = "catch_fish"
KIND_WATER_EDGE = "water_edge"
OPERATION = command.PRIMITIVE_REFLEX_FISH
MAX_REFLEX_CALLS = 4          # one goal: at most this many reflex calls (each one cast), then BLOCKED
WATER_EDGE_REACH = 8          # approach entries only for stand tiles this close (Chebyshev)
FINISH_REASON = "observed_fish_caught"
RULE_ORIGIN = "fishing_rule"
RESULT_SCOPE = "one cast: hook, BobberBar and dismiss run in the mod; verdict FishCaught/FishEscaped/NoBite/Aborted"
GUIDANCE = (" Fish: approach_target a water_edge entry (stand beside water, facing it) with the rod equipped"
            " (select_item), then reflex_fish (args optional: the mod casts, hooks, reels and returns one verdict);"
            " after FishEscaped / NoBite / junk choose reflex_fish again. fishing_objective.rod_held says whether the"
            " rod is in hand (rod_slot = its slot for select_item); don't inspect for it.")
FISH_CATEGORY = -4            # Object.FishCategory: only a fish is the objective's catch (junk / trash pulls skip the bar)
LABEL_JUNK = "junk"           # a FishCaught whose item is not a fish (category != -4): not the goal, another cast allowed

_GRAMMAR = (
    r"(?:tangkap|pancing|mancing|catch) (?:1|satu|one|an?|seekor) (?:ekor )?(?:ikan|fish)",
    # FIXB22 D-B19-4 (live V2B19FISH-a: `mancing` did not parse): the bare activity words, one fish (count 1)
    r"(?:ayo |pergi )?(?:mancing|memancing)(?: ikan)?",
    r"(?:pancing|tangkap) ikan",
    r"(?:go )?fishing",
    r"(?:memancing|pergi memancing|go fishing)(?: dan| and)? (?:tangkap|catch) (?:1|satu|one|an?) (?:ikan|fish)",
)

_TERMINAL_STATUS = frozenset({"CAUGHT", "BLOCKED", "UNCERTAIN"})
_ABORT_RETRYABLE = frozenset({"hook_missed", "cast_no_effect", "timeout", "pad_refused", "interrupted"})


def parse_fishing_goal(text: object) -> bool:
    """Anchored owner grammar (ID/EN): one fish only; anything else is False."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return any(re.fullmatch(pattern.replace(" ", r"\s+") + r"[.!]?", stripped, re.IGNORECASE) for pattern in _GRAMMAR)


@dataclass(frozen=True)
class FishingObjective:
    """Immutable view of one 'catch one fish' objective (like ToolWorkProgress)."""

    goal_id: str
    status: str = "OPEN"               # OPEN | CAUGHT | BLOCKED | UNCERTAIN
    reason: str = "fish_not_caught_yet"
    calls: int = 0                     # settled reflex_fish calls (each one cast)
    last_verdict: str | None = None
    last_detail: str | None = None
    fish: dict | None = None           # {"item", "quality", "size"} from the FishCaught envelope
    request_ids: tuple = ()
    caught_request_id: str | None = None
    catch_any_item: bool = False       # S14 activity: any native fishing catch, including seaweed/trash


def objective_open(objective: FishingObjective | None) -> bool:
    return objective is not None and objective.status == "OPEN"


def default_args() -> dict:
    return dict(max_s=command.REFLEX_FISH_MAX_S_DEFAULT, cast_power=command.REFLEX_FISH_CAST_POWER_DEFAULT,
                treasure=False, hook_retries=1)


def fill_args(args: object, target: Mapping | None) -> dict | None:
    """Model args -> the exact four contract keys. Missing keys take the defaults; unknown keys or a value outside
    the contract refuse (None). Only an offered target can be filled (grants nothing new)."""
    if target is None:
        return None
    given = dict(args) if isinstance(args, Mapping) else {}
    if any(k not in command.REFLEX_FISH_ARG_KEYS for k in given):
        return None
    filled = {**default_args(), **given}
    try:
        command.validate_reflex_fish_args(filled)
    except command.KesalahanPerintah:
        return None
    return filled


def _fresh_facts(ui_facts: Mapping | None) -> tuple[Mapping, Mapping] | None:
    if not isinstance(ui_facts, Mapping):
        return None
    caps = ui_facts.get("capabilities")
    cap = caps.get(OPERATION) if isinstance(caps, Mapping) else None
    fishing = ui_facts.get("fishing")
    if not isinstance(cap, Mapping) or not isinstance(fishing, Mapping):
        return None
    return cap, fishing


def reflex_target(ui_facts: Mapping | None, objective: FishingObjective | None) -> dict | None:
    """The offered reflex target: the mod says reflex_fish is available (rod held, facing water whose landing tile is
    fishable, free slot, no menu) and the objective is open. None = not offered."""
    if not objective_open(objective) or objective.calls >= MAX_REFLEX_CALLS:
        return None
    pair = _fresh_facts(ui_facts)
    if pair is None:
        return None
    cap, fishing = pair
    landing = fishing.get("landing")
    if (cap.get("available") is not True or fishing.get("rod_held") is not True or fishing.get("faces_water") is not True
            or fishing.get("landing_fishable") is not True or not isinstance(landing, (list, tuple)) or len(landing) != 2
            or any(type(v) is not int for v in landing)):
        return None
    return {"landing": [landing[0], landing[1]], "fishing_level": fishing.get("fishing_level")
            if type(fishing.get("fishing_level")) is int else None}


def _revision(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def water_edge_entries(ui_facts: Mapping | None, motor_tile: tuple[int, int] | None,
                       objective: FishingObjective | None) -> list[dict]:
    """Approach entries from the mod's 1-2 water-edge stand tiles: tile = the water tile faced, reach = the stand
    tile (the approach turns to face it on arrival). Only while the objective is open and the reflex is not
    already offered here."""
    if not objective_open(objective) or motor_tile is None or not isinstance(ui_facts, Mapping):
        return []
    fishing = ui_facts.get("fishing")
    edges = fishing.get("water_edges") if isinstance(fishing, Mapping) else None
    if not isinstance(edges, (list, tuple)):
        return []
    out = []
    for edge in edges[:2]:
        if not isinstance(edge, Mapping):
            continue
        keys = ("stand_x", "stand_y", "face_x", "face_y", "facing")
        if any(type(edge.get(k)) is not int for k in keys) or not 0 <= edge["facing"] <= 3:
            continue
        stand, face = (edge["stand_x"], edge["stand_y"]), (edge["face_x"], edge["face_y"])
        if (abs(stand[0] - face[0]) + abs(stand[1] - face[1]) != 1 or min(stand + face) < 0
                or max(abs(stand[0] - motor_tile[0]), abs(stand[1] - motor_tile[1])) > WATER_EDGE_REACH):
            continue
        ref = f"water_edge:{face[0]},{face[1]}@{stand[0]},{stand[1]}"
        out.append(dict(ref=ref, tile=[face[0], face[1]], kind=KIND_WATER_EDGE, reach_tiles=[[stand[0], stand[1]]],
                        revision=_revision(ref)))
    return out


def rank_override(entry: Mapping, objective: FishingObjective | None) -> int | None:
    return 0 if objective_open(objective) and entry.get("kind") == KIND_WATER_EDGE else None


def parse_verdict(wire: object) -> tuple[str, str | None] | None:
    m = re.fullmatch(command.REFLEX_VERDICT_PATTERN, wire) if isinstance(wire, str) else None
    if m is None or m.group("kind") not in command.REFLEX_VERDICTS_FISH:
        return None
    arg = m.group("arg")
    if (m.group("kind") == "Aborted") != (arg is not None) or (arg is not None and arg not in command.REFLEX_REASONS):
        return None
    return m.group("kind"), arg


def settle(objective: FishingObjective, *, request_id: str | None, lifecycle: str, cleanup: bool,
           envelope: Mapping | None) -> FishingObjective:
    """Account one settled reflex call (never replayed: a request id counts once)."""
    if objective.status in _TERMINAL_STATUS or (request_id and request_id in objective.request_ids):
        return objective
    ids = objective.request_ids + ((request_id,) if request_id else ())
    env = envelope or {}
    verdict = env.get("reflex_verdict")
    parsed = parse_verdict(verdict)
    reflex = env.get("reflex") if isinstance(env.get("reflex"), Mapping) else {}
    detail = str(reflex.get("detail") or "")[:80] or None
    if lifecycle in {"rejected", "not_taken"}:
        # Refused before any press (mod admission / binding): no call spent, no effect.
        return replace(objective, request_ids=ids, last_verdict="refused", last_detail=str(env.get("arm_status") or
                       env.get("reject_reason") or "")[:80] or None, reason="reflex_refused")
    if lifecycle != "completed" or not cleanup or parsed is None:
        return replace(objective, request_ids=ids, calls=objective.calls + 1, status="UNCERTAIN",
                       reason="reflex_result_unverified", last_verdict=verdict if isinstance(verdict, str) else None,
                       last_detail=detail)
    kind, arg = parsed
    calls = objective.calls + 1
    fish = reflex.get("fish") if isinstance(reflex.get("fish"), Mapping) else {}
    # ADR 107: the old native verdict called a landed catch an abort merely because
    # it went to the ground. Its exact, cleaned-up drop receipt still proves a catch.
    dropped = detail == "catch_menu_closed:fish_dropped_at_feet"
    legacy_drop = kind == "Aborted" and arg == "inventory_full" and dropped
    if ((kind == "FishCaught" and env.get("success") is True
            and env.get("world_effect_status") == "observed_fish_caught") or legacy_drop):
        category = fish.get("category")
        if (type(category) is int and category == FISH_CATEGORY
                or objective.catch_any_item and isinstance(fish.get("item"), str) and fish["item"]
                and (detail in ("item_added", "catch_menu_pending_choice") or (detail or "").startswith("catch_menu_closed:"))):
            return replace(objective, request_ids=ids, calls=calls, status="CAUGHT",
                           reason=(FINISH_REASON + "_dropped_at_feet" if dropped else
                                   FINISH_REASON + "_menu_pending_choice" if detail == "catch_menu_pending_choice" else FINISH_REASON),
                           last_verdict=verdict, last_detail=detail, caught_request_id=request_id,
                           fish={k: fish.get(k) for k in ("item", "name", "quantity", "quality", "size", "category") if k in fish} or None)
        # Review minor 4: junk / trash (FR:640-644 skips the bar) or an unclassified item is a pull, not the fish the
        # owner asked for: labelled junk, the goal stays open and another cast is allowed within the call budget.
        status, reason = ("BLOCKED", "reflex_call_budget_exhausted") if calls >= MAX_REFLEX_CALLS else ("OPEN", "caught_not_fish")
        return replace(objective, request_ids=ids, calls=calls, status=status, reason=reason, last_verdict=LABEL_JUNK,
                       last_detail=(f"{fish.get('item', '')} category={category}")[:80])
    if kind == "Aborted" and arg not in _ABORT_RETRYABLE:
        # inventory_full, stamina_low, foreign_menu, focus_regain, stop, ...: the objective cannot continue by itself.
        # ADR 105: the mod's own drop of a catch that did not fit is a fact about where the fish went.
        dropped = ":fish_dropped_at_feet" if str(detail or "").endswith("fish_dropped_at_feet") else ""
        return replace(objective, request_ids=ids, calls=calls, status="BLOCKED", reason="reflex_aborted:" + str(arg) + dropped,
                       last_verdict=verdict, last_detail=detail)
    if calls >= MAX_REFLEX_CALLS:
        return replace(objective, request_ids=ids, calls=calls, status="BLOCKED", reason="reflex_call_budget_exhausted",
                       last_verdict=verdict, last_detail=detail)
    return replace(objective, request_ids=ids, calls=calls, reason="fish_not_caught_yet", last_verdict=verdict,
                   last_detail=detail)


def finish_verdict(objective: FishingObjective | None) -> tuple[str, str] | None:
    """(status, reason) of the goal finish, or None when no fishing objective is active."""
    if objective is None:
        return None
    if objective.status == "CAUGHT":
        return "achieved", objective.reason
    if objective.status == "UNCERTAIN":
        return "uncertain", objective.reason
    return "partial", objective.reason


# FIXB20 D-B17-3: mod refusals (REFLEX_FISH:<reason>) that another water edge may cure; every other refusal is either
# transient (busy, using tool, menu) or durable (stamina / a full bag / flags) and never a reason to walk elsewhere.
POSITIONAL_REFUSALS = ("not_facing_water", "landing_not_fishable")
DURABLE_REFUSALS = ("stamina_low", "inventory_full", "flag_off", "pad_path_off")
EDGE_HOPS_MAX = 2


def reflex_refusal(ui_facts: Mapping | None) -> str | None:
    """FIXB20: the mod's reflex_fish refusal reason (``REFLEX_FISH:<reason>`` -> ``<reason>``) when not available."""
    caps = ui_facts.get("capabilities") if isinstance(ui_facts, Mapping) else None
    cap = caps.get(OPERATION) if isinstance(caps, Mapping) else None
    if not isinstance(cap, Mapping) or cap.get("available") is True:
        return None
    reason = cap.get("reason")
    if not isinstance(reason, str) or not reason.startswith("REFLEX_FISH:"):
        return None
    return reason.split(":", 1)[1][:40] or None


def rod_facts(ui_facts: Mapping | None) -> dict | None:
    """D-B2-4/5: {"rod_held": bool, "rod_slot"?: int} from the mod's ``fishing`` fact (published whenever a rod is in the
    inventory), or None when the mod published none (no rod, flag off, or an older mod)."""
    fishing = ui_facts.get("fishing") if isinstance(ui_facts, Mapping) else None
    if not isinstance(fishing, Mapping) or type(fishing.get("rod_held")) is not bool:
        return None
    out = {"rod_held": fishing["rod_held"]}
    if type(fishing.get("rod_slot")) is int and 0 <= fishing["rod_slot"] < 48:
        out["rod_slot"] = fishing["rod_slot"]
    refusal = reflex_refusal(ui_facts)
    if refusal is not None:
        out["reflex_refusal"] = refusal  # FIXB20 D-B17-3: why the cast is not offered here
    return out


def wire(objective: FishingObjective | None, rod: Mapping | None = None) -> dict | None:
    """Prompt / snapshot summary (verified progress only). D-B2-5: with an open objective it also names whether the rod
    is held (the wire drops equipped_tool when use_tool is not offered, so the model kept asking for the rod)."""
    if objective is None:
        return None
    out = {"kind": OBJ_CATCH_FISH, "status": objective.status, "reflex_calls": objective.calls,
           "calls_left": max(0, MAX_REFLEX_CALLS - objective.calls)}
    if objective.status == "OPEN" and isinstance(rod, Mapping):
        out.update({k: rod[k] for k in ("rod_held", "rod_slot", "reflex_refusal") if k in rod})
    if objective.last_verdict:
        out["last_verdict"] = objective.last_verdict
    if objective.fish:
        out["fish"] = dict(objective.fish)
    if objective.status != "OPEN":
        out["reason"] = objective.reason
    return out


def journal_fields(envelope: Mapping | None) -> dict:
    """Bounded journal projection of one reflex result (phase ticks + bar summary, never per tick)."""
    env = envelope or {}
    reflex = env.get("reflex") if isinstance(env.get("reflex"), Mapping) else {}
    out = {"verdict": env.get("reflex_verdict"), "outcome": env.get("outcome"), "status": env.get("status"),
           "cleanup_confirmed": env.get("cleanup_confirmed"), "press_count": env.get("press_count")}
    for key in ("detail", "ticks", "presses", "presses_refused", "taps", "hook_misses", "cast_power",
                "bar_write_refusals", "releases_proved", "catch_menu"):
        if key in reflex:
            out[key] = reflex[key]
    if isinstance(reflex.get("phase_ticks"), Mapping):
        out["phase_ticks"] = dict(reflex["phase_ticks"])
    if isinstance(reflex.get("bar"), Mapping):
        out["bar"] = dict(reflex["bar"])
    if isinstance(reflex.get("fish"), Mapping):
        out["fish"] = dict(reflex["fish"])
    return out


def ground_receipts(envelope, evidence):
    """ADR 107: native catch-menu drops use the ordinary ADR 86 ground memory.

    IDs alone do not establish a quantity. Old DLLs still report a catch truthfully,
    but only the native stack receipt supplies the dropped-item memory.
    """
    # A physical drop receipt remains a fact even when the caller's lease cleanup
    # is uncertain. Publishing it grants no action, completion or safety clearance.
    if evidence.lifecycle != "completed" or not evidence.request_id:
        return []
    reflex = (envelope or {}).get("reflex") or {}
    menu = reflex.get("catch_menu") or {}
    if menu.get("closed") is not True or menu.get("refusal") not in (None, ""):
        return []
    out = []
    for row in menu.get("dropped_stacks") or ():
        if (not isinstance(row, Mapping) or not isinstance(row.get("item"), str) or not row["item"]
                or type(row.get("quantity")) is not int or row["quantity"] < 1):
            continue
        record = dict(ev="native_ground_item", effect="dropped", destination="ground", via="landed",
                      source="native_fishing_catch_menu", request_id=evidence.request_id,
                      caught=row["item"] == (reflex.get("fish") or {}).get("item"),
                      location=(evidence.before or {}).get("location"))
        record.update({k: row[k] for k in ("item", "name", "quantity", "quality", "bag_after", "location", "tile", "date", "time") if k in row})
        out.append(record)
    return out


def landed_catch(envelope, evidence):
    """ADR 115: the native item a cast landed, for today's count. Like a drop receipt it is physical: it stands even if
    the caller's lease cleanup is uncertain. A festival score is not an item she landed."""
    if evidence.lifecycle != "completed" or not evidence.request_id:
        return None
    env = envelope or {}
    parsed = parse_verdict(env.get("reflex_verdict"))
    reflex = env.get("reflex") if isinstance(env.get("reflex"), Mapping) else {}
    fish = reflex.get("fish") if isinstance(reflex.get("fish"), Mapping) else {}
    detail = reflex.get("detail")
    landed = parsed is not None and (parsed[0] == "FishCaught" or parsed == ("Aborted", "inventory_full")
                                     and detail == "catch_menu_closed:fish_dropped_at_feet")
    if not landed or detail == "festival_score+1" or not isinstance(fish.get("item"), str) or not fish["item"]:
        return None
    return dict(ev="native_fishing_catch", request_id=evidence.request_id, location=(evidence.before or {}).get("location"),
                item=fish["item"], name=fish.get("name"), category=fish.get("category"), quantity=fish.get("quantity"),
                destination="ground" if str(detail or "").endswith("fish_dropped_at_feet") else
                            "menu" if detail == "catch_menu_pending_choice" else
                            "inventory" if str(detail or "").endswith("item_added") else "unknown")


def catch_observed(objective):
    if objective.status != "CAUGHT":
        return {}
    destination = ("ground" if objective.reason.endswith("_dropped_at_feet") else
                   "menu" if objective.last_detail == "catch_menu_pending_choice" else
                   "event" if objective.last_detail == "festival_score+1" else "inventory")
    return dict(effect="caught", item=(objective.fish or {}).get("item"), destination=destination)


def catch_speech(objective):
    fish = objective.fish or {}
    if objective.last_detail == "festival_score+1":
        return "Ikan tertangkap; skor festival bertambah satu."
    if objective.last_detail == "catch_menu_pending_choice":
        return "Hasil pancing tertangkap: %s; masih di menu, pilihan simpan atau buang belum dibuat." % str(
            fish.get("name") or fish.get("item", "?"))
    return ("Hasil pancing tertangkap: " + str(fish.get("name") or fish.get("item", "?"))
            + ("; tas tidak muat, jatuh di kakiku." if objective.reason.endswith("_dropped_at_feet") else "; masuk tas."))


# ---------------------------------------------------------------------------- fishing_rule auto act (D-B2-5)

RULE_FISH = "fishing_rule"


def edge_facing(ref: object) -> int | None:
    """FIXB20 review: the facing from a water edge's stand to its water tile (``water_edge:wx,wy@sx,sy``)."""
    try:
        water, stand = str(ref).split(":", 1)[1].split("@")
        wx, wy = (int(v) for v in water.split(","))
        sx, sy = (int(v) for v in stand.split(","))
    except (IndexError, ValueError):
        return None
    return {(0, -1): 0, (1, 0): 1, (0, 1): 2, (-1, 0): 3}.get((wx - sx, wy - sy))


def auto_choice(objective: FishingObjective | None, snap, *, approach_progress: Mapping | None,
                edge: Mapping | None = None, allow_hop: bool = True) -> tuple | None:
    """(operation, target_ref, args, label) for the offered fishing step the model ignored, or None.

    Order: reflex_fish when offered (rod held, facing fishable water); else select_item of the rod when it is not held
    (only the rod's own offered target, never another item); else approach_target of an offered water_edge entry not
    already ARRIVED at. Only steps the snapshot already offers; the same admission gates run on the result."""
    if not objective_open(objective) or objective.calls >= MAX_REFLEX_CALLS:
        return None
    target = getattr(snap, "reflex_fish_target", None)
    if isinstance(target, Mapping):
        args = fill_args({}, target)
        if args is not None:
            return OPERATION, None, args, "reflex_fish"
    summary = getattr(snap, "fishing_objective", None) or {}
    if summary.get("rod_held") is False and type(summary.get("rod_slot")) is int and getattr(snap, "select_item_available", False):
        for item in getattr(snap, "selectable_items", ()) or ():
            if isinstance(item, Mapping) and item.get("slot") == summary["rod_slot"]:
                return "select_item", None, dict(item), "rod_slot:%d" % item["slot"]
    progress = approach_progress or {}
    edges = [e for e in getattr(snap, "approach_targets", ()) or ()
             if isinstance(e, Mapping) and e.get("kind") == KIND_WATER_EDGE]
    # FIXB20 D-B17-3 (live V2B17FISH-a/b: the mod publishes the 1-2 edges nearest ARTI, so after every arrival the "next"
    # edge north was approached and ARTI walked the shore (43,16) -> (43,11) without a cast): the first chosen edge is
    # pinned; once ARRIVED on an edge the rule stays (the cast is offered there, or the refusal decides): only a
    # positional refusal (not facing water / landing not fishable) moves on, at most EDGE_HOPS_MAX times.
    edge = edge if isinstance(edge, Mapping) else {}
    arrived = progress.get("status") == "ARRIVED" and str(progress.get("target_ref") or "").startswith(KIND_WATER_EDGE + ":")
    if arrived:
        if (summary.get("reflex_refusal") not in POSITIONAL_REFUSALS or edge.get("hops", 0) >= EDGE_HOPS_MAX
                or not allow_hop):
            return None
        tried = set(edge.get("tried") or ()) | {progress.get("target_ref")}
        for entry in edges:
            if entry.get("ref") not in tried:
                return "approach_target", str(entry["ref"]), {}, "hop:" + str(entry["ref"])
        return None
    pinned = edge.get("ref")
    for entry in edges:
        if pinned is not None and entry.get("ref") != pinned:
            continue
        return "approach_target", str(entry["ref"]), {}, str(entry["ref"])
    if pinned is not None and edges and edge.get("hops", 0) < EDGE_HOPS_MAX:
        # the pinned edge left the 1-2 nearest publication: the new nearest becomes the pin, counted as a hop (review)
        return "approach_target", str(edges[0]["ref"]), {}, "repin:" + str(edges[0]["ref"])
    return None
