"""Primitives v2 WS-G5: the runtime side of the combat and tool-charge reflexes (pure; no IO, no store, no input path).

The reflexes themselves live in the MOD (``CombatReflexRunner`` / ``ChargeReflexRunner``): one ``reflex_combat`` call faces,
approaches and swings at the nearest monster (1-frame pad X at a 24-tick cadence; pad A never), retreats to the Mines
up-ladder under the hp floor and returns ONE verdict ``MonsterKilled(n)`` / ``Retreated(reason)`` / ``Downed`` /
``NoTarget`` / ``Aborted(reason)``; one ``reflex_charge`` call holds pad X until ``toolPower`` reaches the level and returns
``ChargeReached(n)`` / ``ChargeAborted(reason)`` with the area readback. This module only:

* parses the owner objectives (authenticated owner grant only): ``bunuh satu slime`` / ``kill one slime`` (n <= 5,
  slime or any monster) and ``charge the hoe to level 2`` / ``cangkul 3 petak sekaligus`` (3 / 5 / 9 / 18 tiles);
* reads the mod's ``ui.context`` facts (capabilities ``reflex_combat`` / ``reflex_charge`` + ``combat`` / ``charge``) into
  the offered targets; the combat time rule is mirrored here (never offered at/after 20:00; Mines only from 09:00; the
  Mines must be left by 01:00);
* fills the model's reflex args from the defaults (only the contract keys, each bounded);
* keeps the objective accounting from settled reflex results and decides the deterministic finishes
  (``combat_rule`` -> ``observed_monster_killed``, ``charge_rule`` -> ``observed_charge_area``).

Evidence ceiling: UNIT_TESTED.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

import arti_stardew_command as command

OPERATION_COMBAT = command.PRIMITIVE_REFLEX_COMBAT
OPERATION_CHARGE = command.PRIMITIVE_REFLEX_CHARGE
OBJ_KILL = "kill_monsters"
OBJ_CHARGE = "charge_tool"
KIND_SLIME = "slime"
KIND_MONSTER = "monster"
MAX_COMBAT_CALLS = 6
MAX_CHARGE_CALLS = 3
MAX_HEALS = 2
OFFER_LATEST_TIME = 2000          # never START a Mines combat at/after 20:00 (owner time rule, Mines only)
MINES_EARLIEST_TIME = 900         # Mines combat only from 09:00
MINES_LEAVE_TIME = 2400           # from 24:00 the Mines must be left (pass-out at 02:00; out by 01:00)
# Owner 2026-10-23: outside the Mines combat at night is allowed (farm monsters appear from ~20:00); only the mod's
# 01:00 hard stop (ReflexContracts.CombatHardStopTime) still refuses it.
HARD_STOP_TIME = 2500
FINISH_COMBAT = "observed_monster_killed"
FINISH_CHARGE = "observed_charge_area"
RULE_COMBAT = "combat_rule"
RULE_CHARGE = "charge_rule"
TILES_FOR_LEVEL = {1: 3, 2: 5, 3: 9, 4: 18}   # Tool.tilesAffected (PRB0 §T4)
RESULT_SCOPE_COMBAT = ("one reflex: face / approach / swing at the nearest monster in the mod (never pad A); verdict "
                       "MonsterKilled(n)/Retreated/Downed/NoTarget/Aborted")
RESULT_SCOPE_CHARGE = "one X hold until toolPower = target_level, then the release; verdict ChargeReached(n)/ChargeAborted"
RULE_COMBAT_AUTO = "combat_rule"
GUIDANCE_COMBAT = (" Combat: with the weapon selected (select_item) and a monster near, reflex_combat (args optional: the mod"
                   " faces, swings and retreats under the hp floor, then returns one verdict); approach entries for"
                   " monsters are not needed (the reflex faces them).")
GUIDANCE_CHARGE = (" Charge: face the first tile with the upgraded hoe / can selected, then reflex_charge (args optional:"
                   " the mod holds X until the level and releases).")
GUIDANCE_LEAVE_MINES = " It is past 24:00 in the Mines: leave now (walk to the up-ladder and take it); no more combat."

_COUNT_WORDS = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "satu": 1, "one": 1, "a": 1, "an": 1, "seekor": 1, "se": 1,
                "dua": 2, "two": 2, "tiga": 3, "three": 3, "empat": 4, "four": 4, "lima": 5, "five": 5}
_COMBAT_RE = re.compile(
    r"(?:bunuh|kalahkan|basmi|lawan|kill|defeat|slay)\s+(?P<n>1|2|3|4|5|satu|dua|tiga|empat|lima|one|two|three|four|five|an?|seekor)"
    r"\s+(?:ekor\s+)?(?P<what>slimes?|monsters?)", re.IGNORECASE)
_CHARGE_LEVEL_RE = re.compile(
    r"(?:charge|cas|isi)\s+(?:the\s+)?(?P<tool>hoe|cangkul|watering\s+can|can|penyiram|gembor)"
    r"\s+(?:to\s+|ke\s+)?(?:level|lvl|tingkat)\s+(?P<n>[1-4])", re.IGNORECASE)
_CHARGE_TILES_RE = re.compile(
    r"(?P<verb>cangkul|till|hoe|siram|water)\s+(?P<n>3|5|9|18|tiga|lima|sembilan|three|five|nine)\s+"
    r"(?:petak|tiles?|kotak)\s+(?:sekaligus|at\s+once|in\s+one\s+go)", re.IGNORECASE)
_TILE_WORDS = {"3": 3, "5": 5, "9": 9, "18": 18, "tiga": 3, "lima": 5, "sembilan": 9, "three": 3, "five": 5, "nine": 9}


def _anchored(pattern: re.Pattern, text: str) -> re.Match | None:
    return pattern.fullmatch(re.sub(r"\s+", " ", text.strip()).rstrip(".!"))


# ---------------------------------------------------------------------------------------------- combat objective

@dataclass(frozen=True)
class CombatObjective:
    goal_id: str
    kind: str = KIND_SLIME             # slime | monster
    count: int = 1
    status: str = "OPEN"               # OPEN | KILLED | RETREATED | DOWNED | BLOCKED | UNCERTAIN
    reason: str = "monster_not_killed_yet"
    kills: int = 0                     # verified kills of the objective's kind
    calls: int = 0
    heals: int = 0
    heal_pending: bool = False         # Aborted(heal_needed): the runtime runs the eat chain, then may re-arm
    food_slot: int | None = None
    last_verdict: str | None = None
    last_detail: str | None = None
    killed_types: tuple = ()
    request_ids: tuple = ()
    killed_request_id: str | None = None
    escape: bool = False  # ADR 169: a bounded fight owned by the exit
    disengage: bool = False  # ADR 177: the body's own walk out of contact (mod leg), owned by the exit
    leg: dict | None = None  # ADR 179: the settled leg's native vitals/route (hp_start, hp_end, defends, route, kills)
    weapon_readback: dict | None = None  # ADR 171: held native weapon at this batch's publication fence


def parse_combat_goal(text: object) -> tuple[int, str] | None:
    """Anchored owner grammar (ID/EN): (count 1-5, 'slime' | 'monster'), or None."""
    if not isinstance(text, str):
        return None
    m = _anchored(_COMBAT_RE, text)
    if m is None:
        return None
    n = _COUNT_WORDS.get(m["n"].casefold())
    if n is None or not 1 <= n <= command.REFLEX_COMBAT_MAX_KILLS_MAX:
        return None
    return n, KIND_SLIME if m["what"].casefold().startswith("slime") else KIND_MONSTER


# FIXB23 D-B20-2 (live V2B20ACC11b: kill witnessed, then `mines_exit step` x6 hit the trial's 48-input envelope before
# ExitMine): a combat goal's trial input envelope, sized like a skill plan's (plan_input_envelope). Per kill: the
# moving-target approach cap (runtime _APPROACH_STEPS_MOVING_MAX) + the objective's reflex calls; once per goal: one
# clear_path stage (CombatRuntimeMixin.CLEAR_PATH_STEPS_MAX) and the Mines exit leg (<= 32 approach steps to the up-ladder,
# the A press, the Leave answer). Capped like the skills envelope.
COMBAT_ENVELOPE_APPROACH = 64
COMBAT_ENVELOPE_CLEAR_PATH = 30
COMBAT_ENVELOPE_EXIT = 34
COMBAT_INPUT_ENVELOPE_MAX = 160


def combat_input_envelope(owner_text: object) -> int | None:
    """The trial input envelope a combat owner goal needs (grant prefix "[recorded remark omitted]" tolerated), or None when
    the text is not a combat goal. kill 1 = 64 + 6 + 30 + 34 = 134."""
    if not isinstance(owner_text, str):
        return None
    text = owner_text.split(":", 1)[1] if ":" in owner_text else owner_text
    parsed = parse_combat_goal(text)
    if parsed is None:
        return None
    count, _ = parsed
    needed = count * COMBAT_ENVELOPE_APPROACH + MAX_COMBAT_CALLS + COMBAT_ENVELOPE_CLEAR_PATH + COMBAT_ENVELOPE_EXIT
    return min(COMBAT_INPUT_ENVELOPE_MAX, needed)


def combat_open(objective: CombatObjective | None) -> bool:
    return objective is not None and objective.status == "OPEN"


def kill_matches(kind: str, monster_type: object) -> bool:
    return isinstance(monster_type, str) and (kind == KIND_MONSTER or "slime" in monster_type.casefold())


def time_rule(combat_facts: Mapping | None) -> str | None:
    """The runtime mirror of the mod's time rule: None = combat may be offered now, else the reason."""
    if not isinstance(combat_facts, Mapping) or type(combat_facts.get("time_of_day")) is not int:
        return "time_unknown"
    t = combat_facts["time_of_day"]
    in_mines = combat_facts.get("in_mines") is True
    if not in_mines:
        return "after_01" if t >= HARD_STOP_TIME else None  # owner 2026-10-23: night combat outside the Mines
    if t >= OFFER_LATEST_TIME:
        return "after_20" if t < MINES_LEAVE_TIME else "leave_mines"
    if in_mines and t < MINES_EARLIEST_TIME:
        return "mines_before_09"
    return None


def leave_mines_due(combat_facts: Mapping | None) -> bool:
    return (isinstance(combat_facts, Mapping) and combat_facts.get("in_mines") is True
            and type(combat_facts.get("time_of_day")) is int and combat_facts["time_of_day"] >= MINES_LEAVE_TIME)


def _cap(ui_facts: Mapping | None, operation: str) -> Mapping | None:
    caps = ui_facts.get("capabilities") if isinstance(ui_facts, Mapping) else None
    cap = caps.get(operation) if isinstance(caps, Mapping) else None
    return cap if isinstance(cap, Mapping) else None


REFLEX_RADIUS_MARGIN = 0.5


def combat_target(ui_facts: Mapping | None, objective: CombatObjective | None,
                  inventory_facts: Mapping | None = None) -> dict | None:
    """The offered reflex_combat target (mod says available: weapon held, monster within 8 tiles, hp above the floor,
    time rule) while the objective is open, not healing, within the call budget and inside the runtime time rule."""
    if not combat_open(objective) or objective.heal_pending or objective.calls >= MAX_COMBAT_CALLS:
        return None
    cap = _cap(ui_facts, OPERATION_COMBAT)
    combat = ui_facts.get("combat") if isinstance(ui_facts, Mapping) else None
    escaping = objective.escape and isinstance(combat, Mapping) and combat.get("in_mines") is True
    if getattr(objective, "disengage", False):
        # ADR 177: no weapon, clock or HP-floor term; a live monster within the reflex radius on a Mines floor.
        if (cap is None or cap.get("disengage_available") is not True or not isinstance(combat, Mapping)
                or combat.get("in_mines") is not True):
            return None  # ADR 179: every walk out uses the leg, a monster near or not
        near = [{k: m.get(k) for k in ("id", "type", "distance", "hp")} for m in (combat.get("monsters") or ())[:3]
                if isinstance(m, Mapping)]
        return {"monsters": near, "hp": combat.get("hp") if type(combat.get("hp")) is int else None,
                "max_hp": combat.get("max_hp") if type(combat.get("max_hp")) is int else None, "in_mines": True,
                "time_of_day": combat.get("time_of_day"), "disengage": True}
    if (cap is None or cap.get("escape_available" if escaping else "available") is not True
            or not isinstance(combat, Mapping) or not escaping and time_rule(combat) is not None):
        return None
    monsters = combat.get("monsters")
    if not weapon_ready(ui_facts, inventory_facts) or not isinstance(monsters, (list, tuple)) or not monsters:
        return None
    # FIXB16 D-B14-1 (live V2B14ACC11a: after the approach budget the rule armed the reflex with the slime outside the radius
    # -> 6x NoTarget(no_monster_in_radius) in 3 s -> call budget exhausted): only a monster of the objective's kind whose
    # published distance is inside the reflex radius (minus a margin) offers the reflex; else approach / wait.
    if not any(isinstance(m, Mapping) and kill_matches(objective.kind, m.get("type"))
               and isinstance(m.get("distance"), (int, float))
               and m["distance"] <= command.REFLEX_COMBAT_RADIUS_TILES_MAX - REFLEX_RADIUS_MARGIN for m in monsters):
        return None
    near = [{k: m.get(k) for k in ("id", "type", "distance", "hp")} for m in monsters[:3] if isinstance(m, Mapping)]
    hp, max_hp = combat.get("hp"), combat.get("max_hp")
    return {"monsters": near, "hp": hp if type(hp) is int else None, "max_hp": max_hp if type(max_hp) is int else None,
            "in_mines": combat.get("in_mines") is True, "time_of_day": combat.get("time_of_day")}


def idle_hp_floor(combat_facts: Mapping | None, floor_pct: int = command.REFLEX_COMBAT_HP_FLOOR_PCT_DEFAULT) -> dict | None:
    """D-B6-2: {hp, max_hp, floor_pct} when the published HP is below the reflex HP floor, else None."""
    if not isinstance(combat_facts, Mapping):
        return None
    hp, max_hp = combat_facts.get("hp"), combat_facts.get("max_hp")
    if type(hp) is not int or type(max_hp) is not int or max_hp <= 0 or hp * 100 >= floor_pct * max_hp:
        return None
    return {"hp": hp, "max_hp": max_hp, "floor_pct": floor_pct}


def heal_needed(objective: CombatObjective) -> CombatObjective:
    """D-B6-2: the idle HP-floor hand-off (the same heal_pending the reflex's Aborted(heal_needed) sets)."""
    return replace(objective, heal_pending=True, food_slot=None, reason="heal_needed")


def remaining_kills(objective: CombatObjective | Mapping | None) -> int:
    """Kills still needed (objective or its wire summary), at least 1."""
    if isinstance(objective, CombatObjective):
        return max(1, objective.count - objective.kills)
    if isinstance(objective, Mapping) and type(objective.get("count")) is int and type(objective.get("kills")) is int:
        return max(1, objective["count"] - objective["kills"])
    return 1


def combat_default_args(objective: CombatObjective | Mapping | None) -> dict:
    out = dict(max_s=command.REFLEX_COMBAT_MAX_S_DEFAULT, hp_floor_pct=command.REFLEX_COMBAT_HP_FLOOR_PCT_DEFAULT,
               max_kills=min(command.REFLEX_COMBAT_MAX_KILLS_MAX, remaining_kills(objective)), retreat=True)
    kind = objective_kind(objective)
    if kind in command.REFLEX_COMBAT_TARGET_KINDS:
        # FIXB15 D-B13-3 (live V2B13ACC11b: a Bug killed under "bunuh satu slime"): the reflex targets the objective's kind.
        out["target_kind"] = kind
    if (objective.get("escape", False) if isinstance(objective, Mapping) else getattr(objective, "escape", False)):
        out.update(max_s=4, retreat=False, escape=True)
    if (objective.get("disengage", False) if isinstance(objective, Mapping) else getattr(objective, "disengage", False)):
        out.update(max_s=command.REFLEX_COMBAT_DISENGAGE_MAX_S, retreat=True, max_kills=1, disengage=True,
                   target_kind="monster")
    return out


def objective_kind(objective: CombatObjective | Mapping | None) -> str | None:
    """The combat objective's monster kind: CombatObjective.kind, or the wire summary's "target" (combat_wire)."""
    if isinstance(objective, CombatObjective):
        return objective.kind
    if isinstance(objective, Mapping) and objective.get("target") in command.REFLEX_COMBAT_TARGET_KINDS:
        return objective["target"]
    return None


def fill_combat_args(args: object, target: Mapping | None, objective: CombatObjective | Mapping | None) -> dict | None:
    """Model args -> the exact four contract keys (defaults for missing keys); unknown keys / bad values refuse."""
    if target is None:
        return None
    given = dict(args) if isinstance(args, Mapping) else {}
    escaping = objective.get("escape", False) if isinstance(objective, Mapping) else getattr(objective, "escape", False)
    if given.get("escape", escaping) != escaping:
        return None  # only the exit may own this exemption
    disengaging = (objective.get("disengage", False) if isinstance(objective, Mapping)
                   else getattr(objective, "disengage", False))
    if given.get("disengage", disengaging) != disengaging:
        return None  # ADR 177: only the exit's body leg
    if any(k not in command.REFLEX_COMBAT_ARG_KEYS + command.REFLEX_COMBAT_OPTIONAL_KEYS for k in given):
        return None
    filled = {**combat_default_args(objective), **given}
    kind = objective_kind(objective)
    if kind in command.REFLEX_COMBAT_TARGET_KINDS:
        # FIXB15 review MAJOR: the model path fills from the wire summary; the objective's kind is ALWAYS the target kind
        # (a model "monster" under a slime goal is refused, never passed on).
        if given.get("target_kind", kind) != kind:
            return None
        filled["target_kind"] = kind
    try:
        command.validate_reflex_combat_args(filled)
    except command.KesalahanPerintah:
        return None
    return filled


def parse_verdict(wire: object, family: tuple) -> tuple[str, str | None] | None:
    m = re.fullmatch(command.REFLEX_VERDICT_PATTERN, wire) if isinstance(wire, str) else None
    if m is None or m.group("kind") not in family:
        return None
    kind, arg = m.group("kind"), m.group("arg")
    if (kind in command.REFLEX_VERDICT_KINDS_WITH_ARG) != (arg is not None):
        return None
    if kind in ("ChargeReached", "MonsterKilled"):
        if not (arg or "").isdigit():
            return None
    elif arg is not None and arg not in command.REFLEX_REASONS:
        return None
    return kind, arg


_COMBAT_RETRYABLE = frozenset({"stuck", "timeout", "pad_refused", "interrupted", "route_blocked"})


def route_blocked_rock(detail: object) -> tuple[int, int] | None:
    """FIXB15 D-B13-3: the rock the mod's reflex route needs broken (detail ``route_blocked rock=x,y``), or None."""
    m = re.fullmatch(r"route_blocked rock=(\d{1,4}),(\d{1,4})", str(detail or ""))
    return (int(m[1]), int(m[2])) if m else None


def settle_combat(objective: CombatObjective, *, request_id: str | None, lifecycle: str, cleanup: bool,
                  envelope: Mapping | None) -> CombatObjective:
    """Account one settled reflex_combat call (a request id counts once)."""
    if objective.status != "OPEN" or (request_id and request_id in objective.request_ids):
        return objective
    ids = objective.request_ids + ((request_id,) if request_id else ())
    env = envelope or {}
    verdict = env.get("reflex_verdict")
    parsed = parse_verdict(verdict, command.REFLEX_VERDICTS_COMBAT)
    reflex = env.get("reflex") if isinstance(env.get("reflex"), Mapping) else {}
    detail = str(reflex.get("detail") or "")[:80] or None
    if lifecycle in {"rejected", "not_taken"}:
        return replace(objective, request_ids=ids, last_verdict="refused", reason="reflex_refused",
                       last_detail=str(env.get("arm_status") or env.get("reject_reason") or "")[:80] or None)
    if lifecycle != "completed" or not cleanup or parsed is None:
        return replace(objective, request_ids=ids, calls=objective.calls + 1, status="UNCERTAIN",
                       reason="reflex_result_unverified", last_verdict=verdict if isinstance(verdict, str) else None,
                       last_detail=detail)
    kind, arg = parsed
    calls = objective.calls + 1
    base = dict(request_ids=ids, calls=calls, last_verdict=verdict, last_detail=detail)
    if kind == "MonsterKilled" and env.get("world_effect_status") == FINISH_COMBAT:
        types = tuple(t for t in (reflex.get("killed_types") or ()) if isinstance(t, str))
        matched = sum(1 for t in types if kill_matches(objective.kind, t))
        kills = objective.kills + matched
        if kills >= objective.count:
            return replace(objective, **base, kills=kills, killed_types=objective.killed_types + types, status="KILLED",
                           reason=FINISH_COMBAT, killed_request_id=request_id)
        objective = replace(objective, kills=kills, killed_types=objective.killed_types + types)
        reason = "killed_other_monster" if matched == 0 else "more_kills_needed"
    elif kind == "Downed":
        return replace(objective, **base, status="DOWNED", reason="player_downed")
    elif kind == "Retreated":
        return replace(objective, **base, status="RETREATED", reason="retreated:" + str(arg))
    elif kind == "Aborted" and arg == "heal_needed":
        slot = reflex.get("food_slot")
        if objective.heals >= MAX_HEALS:
            return replace(objective, **base, status="BLOCKED", reason="heal_budget_exhausted")
        return replace(objective, **base, heal_pending=True, food_slot=slot if type(slot) is int else None,
                       reason="heal_needed")
    elif kind == "Aborted" and arg not in _COMBAT_RETRYABLE:
        return replace(objective, **base, status="BLOCKED", reason="reflex_aborted:" + str(arg))
    else:
        reason = "no_target" if kind == "NoTarget" else "monster_not_killed_yet"
    if calls >= MAX_COMBAT_CALLS:
        return replace(objective, **base, status="BLOCKED", reason="reflex_call_budget_exhausted")
    return replace(objective, **base, reason=reason)


def healed(objective: CombatObjective) -> CombatObjective:
    return replace(objective, heal_pending=False, heals=objective.heals + 1, food_slot=None, reason="healed")


def heal_failed(objective: CombatObjective, why: str) -> CombatObjective:
    return replace(objective, heal_pending=False, status="BLOCKED", reason="heal_failed:" + why)


def time_blocked(objective: CombatObjective) -> CombatObjective:
    return replace(objective, status="BLOCKED", reason="time_rule_leave_mines")


def combat_finish_verdict(objective: CombatObjective | None) -> tuple[str, str] | None:
    if objective is None:
        return None
    if objective.status == "KILLED":
        return "achieved", FINISH_COMBAT
    if objective.status == "UNCERTAIN":
        return "uncertain", objective.reason
    return "partial", objective.reason


SCYTHES = frozenset({"(W)47", "(W)53", "(W)66"})   # MeleeWeapon scythes (CombatWeapon.IsScythe)
SLINGSHOTS = frozenset({"(W)32", "(W)33", "(W)34"})  # Slingshot (not a MeleeWeapon): reflex_combat refuses it


def is_weapon(qualified_item_id: object) -> bool:
    """A real melee weapon (no scythe, no slingshot): preferred, and what ``lawan_mine`` requires."""
    return (isinstance(qualified_item_id, str) and qualified_item_id.startswith("(W)")
            and qualified_item_id not in SCYTHES and qualified_item_id not in SLINGSHOTS)


def is_combat_tool(qualified_item_id: object) -> bool:
    """Owner 2026-10-23: the scythe counts as a weapon (the mod's reflex_combat swings any MeleeWeapon, DLL with the
    ``combat.scythe_held`` fact). A real weapon is still preferred (``weapon_facts``); a slingshot never counts."""
    return is_weapon(qualified_item_id) or qualified_item_id in SCYTHES


def scythe_reflex(combat_facts: Mapping | None) -> bool:
    """The deployed mod admits the scythe to reflex_combat: its combat block publishes ``scythe_held`` (older DLLs
    refuse the scythe with ``scythe_not_weapon`` and never publish the key)."""
    return isinstance(combat_facts, Mapping) and type(combat_facts.get("scythe_held")) is bool


TOOLBAR_SLOTS = 12


def weapon_facts(ui_facts: Mapping | None, inventory_facts: Mapping | None = None) -> dict | None:
    """D-B3-6 / D-B5-2: {"weapon_held": bool, "weapon_slot"?: int, "weapon_qid"?: str, "weapon_in_toolbar"?: bool} from
    the mod's combat fact + toolbar (first real (W) slot), else the full inventory (progress.inventory slot facts;
    live V2B5ACC11a: the sword sat in backpack slot 13, outside the 12 toolbar slots), or None without a combat fact.

    Owner 2026-10-23: the scythe counts as a weapon only when no real weapon is owned, the mod admits it
    (``scythe_reflex``) and ARTI is outside the Mines (``lawan_mine`` needs a real weapon). ``weapon_held`` is the
    EFFECTIVE fact: a held scythe with a real weapon owned (or inside the Mines) is not held, so the existing staging
    (select_item / item_to_toolbar) switches to the real weapon first (review r1 finding 1)."""
    combat = ui_facts.get("combat") if isinstance(ui_facts, Mapping) else None
    if not isinstance(combat, Mapping) or type(combat.get("weapon_held")) is not bool:
        return None
    held_scythe = combat["weapon_held"] and (combat.get("scythe_held") is True or combat.get("weapon") in SCYTHES)
    scythe_ok = scythe_reflex(combat) and combat.get("in_mines") is not True
    toolbar = ui_facts.get("toolbar") if isinstance(ui_facts.get("toolbar"), Mapping) else {}
    tools = [t for t in toolbar.get("targets") or () if isinstance(t, Mapping) and type(t.get("slot")) is int]
    slots = sorted((f for k, f in (inventory_facts or {}).items() if str(k).startswith("slot:") and isinstance(f, Mapping)
                    and f.get("occupied") is True and type(f.get("slot")) is int), key=lambda f: f["slot"])

    owned_slots = {f["slot"]: f for f in slots}
    owned_slots.update({t["slot"]: t for t in tools})  # fresh toolbar wins over an older bag projection

    def find(pick, occurrence=0):
        for _, f in sorted(owned_slots.items()):
            if pick(f.get("qualified_item_id")):
                if occurrence:
                    occurrence -= 1
                    continue
                return dict(weapon_slot=f["slot"], weapon_qid=f["qualified_item_id"],
                            weapon_in_toolbar=f["slot"] < TOOLBAR_SLOTS)
        return None

    real = find(is_weapon)
    # ADR 171: rank the owned native damage ranges, then verify that exact
    # weapon in hand. A boolean saying *some* melee tool is held is insufficient.
    native_weapons = combat.get("weapons")
    best_stats = None
    if isinstance(native_weapons, (list, tuple)):
        ranked = sorted((w for w in native_weapons if isinstance(w, Mapping)
                         and is_weapon(w.get("qualified_item_id"))
                         and type(w.get("min_damage")) is int and type(w.get("max_damage")) is int),
                        key=lambda w: (w["min_damage"] + w["max_damage"], w["max_damage"]), reverse=True)
        if ranked:
            best_stats = ranked[0]
            best = best_stats["qualified_item_id"]
            # The native weapon list preserves bag order. Resolve duplicate IDs
            # by their occurrence, including differently forged copies.
            before_best = native_weapons[:next(i for i, w in enumerate(native_weapons) if w is best_stats)]
            occurrence = sum(isinstance(w, Mapping) and w.get("qualified_item_id") == best for w in before_best)
            real = find(lambda q: q == best, occurrence)
            if real is None:
                return {"weapon_held": False}  # inventory/toolbar read has not caught up
    if real is not None:
        held = combat["weapon_held"] and not held_scythe
        if "weapon" in combat or isinstance(native_weapons, (list, tuple)):
            held = held and combat.get("weapon") == real["weapon_qid"]
        if best_stats is not None:
            held_stats = combat.get("weapon_stats") or {}
            held = held and all(held_stats.get(k) == best_stats[k] for k in ("min_damage", "max_damage"))
            if type(toolbar.get("active_slot")) is int:
                held = held and toolbar["active_slot"] == real["weapon_slot"]
        return {"weapon_held": held, **real}
    if scythe_ok:
        return {"weapon_held": combat["weapon_held"], **(find(lambda q: q in SCYTHES) or {})}
    return {"weapon_held": combat["weapon_held"] and not held_scythe}


def weapon_ready(ui_facts: Mapping | None, inventory_facts: Mapping | None = None) -> bool:
    """The effective weapon is in hand (``weapon_facts``): the reflex may be offered / a monster approached."""
    weapon = weapon_facts(ui_facts, inventory_facts)
    return weapon is not None and weapon["weapon_held"] is True


WEAPON_STAGE_STEPS_MAX = 48  # FIXB10: a backpack slot far from a full toolbar takes ~3 nav legs of <= 13 + 5 presses


NOT_PUBLISHED_SETTLE_S = 1.0  # FIXB11 D-B8-2: one settle window before `weapon_not_published` is final
WEAPON_ABORT_STEPS = 12          # FIXB6 review: extra steps for the one bounded place-back after a stop while holding


def weapon_stage_unsafe(ui_facts, objective) -> str | None:
    """FIXB6 review: never open the inventory with a matching monster inside the reflex radius or HP at / below the
    reflex floor (the same terms as monster_entries); the reason, or None."""
    facts = ui_facts.get("combat") if isinstance(ui_facts, Mapping) else None
    if not isinstance(facts, Mapping):
        return "combat_facts_unavailable"
    hp, max_hp = facts.get("hp"), facts.get("max_hp")
    if (type(hp) is not int or type(max_hp) is not int or max_hp <= 0
            or (100 * hp) // max_hp <= command.REFLEX_COMBAT_HP_FLOOR_PCT_DEFAULT):
        return "hp_floor"
    kind = getattr(objective, "kind", KIND_MONSTER)
    for m in facts.get("monsters") or ():
        if (isinstance(m, Mapping) and kill_matches(kind, m.get("type")) and isinstance(m.get("distance"), (int, float))
                and m["distance"] <= command.REFLEX_COMBAT_RADIUS_TILES_MAX):
            return "monster_in_radius"
    return None


def weapon_to_toolbar_step(view, weapon_qid: str, stage: dict) -> tuple[str, str | None, dict | None, str]:
    """D-B5-2 + FIXB6 review: a stop while an item is on the cursor first runs ONE bounded place-back (A on the origin
    slot, then B) before the stop is reported."""
    result = _weapon_to_toolbar_step_guarded(view, weapon_qid, stage)
    if result[3] == "pick_weapon" and stage.get("need") not in (None, "weapon"):
        return result[0], result[1], result[2], "pick_item"  # FIXB12: a rod / seed / tool is no weapon
    return result


def _weapon_to_toolbar_step_guarded(view, weapon_qid: str, stage: dict) -> tuple[str, str | None, dict | None, str]:
    if stage.get("closing") and (view is None or getattr(view, "menu_type", None) != "GameMenu"):
        return "stop", None, None, stage["closing"]  # FIXB11 D-B8-2: closed after a stop: report the original reason
    result = _weapon_to_toolbar_step(view, weapon_qid, stage)
    held = (getattr(view, "held_item", None) or {}).get("item") if view is not None else None
    if result[0] == "stop" and result[3] == "weapon_not_published" and held is None:
        # FIXB11 D-B8-2 (live V2B8ACC11b, full bag: `place_swapped_item_back` -> `weapon_not_published` while the sword
        # was already in toolbar slot 0): the mod republishes the slots after the inventory change settles; re-read once
        # after a short settle, then close.
        import time
        since = stage.setdefault("not_published_since", time.monotonic())
        if time.monotonic() - since < NOT_PUBLISHED_SETTLE_S:
            return "wait", None, None, "weapon_not_published_recheck"
    if result[0] == "stop" and held is not None and not stage.get("abort"):
        stage["abort"] = result[3]
        return _weapon_to_toolbar_step(view, weapon_qid, stage)
    if (result[0] == "stop" and held is None and view is not None and getattr(view, "menu_type", None) == "GameMenu"
            and getattr(view, "available", False)):
        # FIXB11 D-B8-2: the stage never ends with its GameMenu open (22 L2513 discards followed live): close it first.
        btn = next((b for b in ("B", "Start") if view.press_allowed(b)), None)
        if btn is not None:
            stage["closing"] = result[3]
            return "act", "ui_press", view.press_args(btn), "close_after_stop"
    return result


def toolbar_swap_slot(slots: Mapping, edible_slots=()) -> int:
    """FIXB19 (live batch 16: the plant stage swapped the Pickaxe out of the toolbar, a later clear_path could not select
    it): a full toolbar gives up its LAST slot holding no tool / weapon ((T) / (W) qualified ids) and no food (review:
    the heal hand-off eats from the toolbar; ``edible_slots`` = the mod's eat_candidates slots); then the last non-tool
    (food); only a toolbar of tools falls back to slot 0 (the swapped item goes back into the moved item's slot)."""
    def item_at(i):
        slot = slots.get(i)
        return (slot.get("value") or {}).get("item") if isinstance(slot, Mapping) else None
    non_tools = [i for i in range(TOOLBAR_SLOTS - 1, -1, -1)
                 if isinstance(item_at(i), str) and not item_at(i).startswith(("(T)", "(W)"))]
    for i in non_tools:
        if i not in set(edible_slots or ()):
            return i
    return non_tools[0] if non_tools else 0


def _weapon_to_toolbar_step(view, weapon_qid: str, stage: dict) -> tuple[str, str | None, dict | None, str]:
    """D-B5-2: the next GameMenu inventory step moving the weapon from the backpack into the toolbar (G1 InventoryPage:
    A picks up / places the snapped stack): open with Start, nav to the weapon, A, nav to an empty toolbar slot (else
    slot 0: the swapped item goes back into the weapon's old slot), A, then B. ``stage`` keeps the origin slot.
    -> ("act", operation, args, reason) | ("done" | "stop" | "wait", None, None, reason)."""
    from arti_stardew_world_extras import nav_direction
    if view is None:
        if stage.get("abort"):
            return "stop", None, None, "aborted:" + str(stage["abort"])
        return ("act", "pad_button", {"button": "Start"}, "open_inventory") if not stage.get("opened") else \
            ("done", None, None, "menu_closed")
    if view.menu_type != "GameMenu":
        return "stop", None, None, "foreign_menu:" + str(view.menu_type)
    if not view.available:
        return "wait", None, None, view.wait_reason or "not_ready"
    stage["opened"] = True
    if view.page_type != "InventoryPage":
        if "inventory" in view.tabs:
            return "act", "ui_press", view.press_args("tab", "inventory"), "tab_inventory"
        return "stop", None, None, "inventory_tab_unavailable"
    slots = {c["id"]: c for c in view.components if c.get("kind") == "slot" and type(c.get("id")) is int and 0 <= c["id"] < 36}
    held = (view.held_item or {}).get("item")
    weapon_at = next((i for i, c in sorted(slots.items()) if (c.get("value") or {}).get("item") == weapon_qid), None)
    if stage.get("abort"):
        if held is None:
            btn = next((b for b in ("B", "Start") if view.press_allowed(b)), None)
            return ("act", "ui_press", view.press_args(btn), "abort_close") if btn else \
                ("stop", None, None, "aborted_close_not_offered:" + str(stage["abort"]))
        if stage.get("origin") is None:
            return "stop", None, None, "aborted_no_origin:" + str(stage["abort"])
        goal, press_reason = stage["origin"], "abort_place_back"
        if (slots.get(goal) or {}).get("value"):
            # FIXB27 D-B24-3: the origin is taken (or unreadable as empty): the first free slot; FIXB27 review: with no free
            # slot (a full inventory), back onto the slot it was swapped from (vanilla InventoryPage.readyToClose is false
            # while an item is held, so B never closes it). Bounded: never an endless swap.
            free = next((i for i in sorted(slots) if not slots[i].get("value")), None)
            if free is not None:
                goal = free
            elif type(stage.get("swap_slot")) is int and stage.get("abort_swap_backs", 0) < 1:
                goal, press_reason = stage["swap_slot"], "abort_swap_back"
            else:
                return "stop", None, None, "held_item_no_free_slot"
    elif held is None:
        if weapon_at is not None and weapon_at < TOOLBAR_SLOTS:
            btn = next((b for b in ("B", "Start") if view.press_allowed(b)), None)
            return ("act", "ui_press", view.press_args(btn), "close") if btn else ("stop", None, None, "close_not_offered")
        if weapon_at is None:
            return "stop", None, None, "weapon_not_published"
        stage.setdefault("origin", weapon_at)
        goal, press_reason = weapon_at, "pick_weapon"
    elif held == weapon_qid:
        empty = [i for i in range(TOOLBAR_SLOTS) if i in slots and not slots[i].get("value")]
        goal, press_reason = (empty[0] if empty else toolbar_swap_slot(slots, stage.get("edible_slots") or ())), "place_in_toolbar"
        stage["swap_slot"] = goal  # FIXB27 review: where a swapped item came from (the abort's last resort)
    else:
        origin = stage.get("origin")
        if origin is None:
            return "stop", None, None, "unexpected_item_held"
        goal, press_reason = origin, "place_swapped_item_back"
    if view.snapped_id == goal:
        if not view.press_allowed("A"):
            return "stop", None, None, "press_a_refused"
        if press_reason == "abort_swap_back":
            stage["abort_swap_backs"] = stage.get("abort_swap_backs", 0) + 1
        return "act", "ui_press", view.press_args("A"), press_reason
    if not view.nav_allowed:
        return "stop", None, None, "nav_not_offered"
    if view.snapped_id < 0:
        return "act", "ui_nav", view.nav_args("down"), "first_snap"
    direction = slot_grid_direction(view.components, view.snapped_id, goal)
    if direction is None:
        return "stop", None, None, "no_route_to_slot"
    return "act", "ui_nav", view.nav_args(direction), "nav_" + direction


def slot_grid_direction(components, here: int, goal: int) -> str | None:
    """FIXB27 D-B24-3 (live V2B24EAT-a: `place_swapped_item_back` from toolbar slot 11 routed `nav_right` through the
    published side button 898 (off the slot grid); the next nav was refused, `abort_place_back` never finished, the
    GameMenu stayed open with Bait held): inventory moves stay ON the 36-slot grid (rows of 12). On the grid: the
    neighbour path over slot components only, else row / column math; off the grid (a side button): back onto it through
    the neighbour that is a slot. None = no grid route."""
    from arti_stardew_world_extras import nav_direction
    grid = [c for c in components if isinstance(c, Mapping) and type(c.get("id")) is int and 0 <= c["id"] < 36]
    if 0 <= here < 36:
        direction = nav_direction(grid, here, goal)
        if direction is None:
            direction = ("down" if goal // 12 > here // 12 else "up" if goal // 12 < here // 12
                         else "right" if goal > here else "left")
        return direction
    comp = next((c for c in components if isinstance(c, Mapping) and c.get("id") == here), None)
    nbr = comp.get("nbr") if comp else None
    names = ("up", "right", "down", "left")
    return next((names[i] for i, n in enumerate(nbr if isinstance(nbr, (list, tuple)) and len(nbr) == 4 else ())
                 if type(n) is int and 0 <= n < 36), None)


KIND_MONSTER_ENTRY = "monster"
MONSTER_APPROACH_MAX = 30          # tiles: farther monsters are not approached (D-B4-6)


def monster_entries(ui_facts: Mapping | None, motor_tile: tuple[int, int] | None,
                    objective: CombatObjective | None) -> list[dict]:
    """D-B4-6 (live V2B4ACC11b: slimes 13.1 / 23.2 tiles away, REFLEX_COMBAT:no_monster_in_radius, nothing approached):
    approach entries toward the nearest matching monsters OUTSIDE the reflex radius (the mod's 8 tiles) while the weapon
    is held. Tile = the monster's tile (ADJACENT reach: its cardinal neighbours); the revision includes the tile, so a
    moving monster rebuilds the controller from fresh facts. Once inside the radius the reflex is offered instead."""
    import hashlib
    if not combat_open(objective) or objective.heal_pending or motor_tile is None or not isinstance(ui_facts, Mapping):
        return []
    facts = ui_facts.get("combat")
    if not isinstance(facts, Mapping) or not weapon_ready(ui_facts) or time_rule(facts) is not None:
        return []
    # FIXB4 review: the reflex's own HP admission term (CombatReflexAdmission: floor(100*hp/max) > hp_floor_pct); never
    # walk toward a monster the reflex would then refuse to fight.
    hp, max_hp = facts.get("hp"), facts.get("max_hp")
    if (type(hp) is not int or type(max_hp) is not int or max_hp <= 0
            or (100 * hp) // max_hp <= command.REFLEX_COMBAT_HP_FLOOR_PCT_DEFAULT):
        return []
    out = []
    for m in facts.get("monsters") or ():
        if not isinstance(m, Mapping) or not kill_matches(objective.kind, m.get("type")) or m.get("invincible") is True:
            continue
        tile, distance = m.get("tile"), m.get("distance")
        if (not isinstance(tile, (list, tuple)) or len(tile) != 2 or any(type(v) is not int or v < 0 for v in tile)
                # FIXB23 D-B21-2: the reflex is offered only inside radius - margin (FIXB16 D-B14-1); an approach entry
                # covers everything beyond it, so (7.5, 8.0] is no longer neither (live V2B21ACC11b: slime at 8.0).
                or not isinstance(distance, (int, float))
                or distance <= command.REFLEX_COMBAT_RADIUS_TILES_MAX - REFLEX_RADIUS_MARGIN
                or max(abs(tile[0] - motor_tile[0]), abs(tile[1] - motor_tile[1])) > MONSTER_APPROACH_MAX):
            continue
        ref = "monster:%s" % str(m.get("id"))[:40]
        out.append(dict(ref=ref, tile=[tile[0], tile[1]], kind=KIND_MONSTER_ENTRY, label=str(m.get("type") or "")[:40],
                        revision=hashlib.sha256(("%s|%d|%d" % (ref, tile[0], tile[1])).encode()).hexdigest()[:16]))
        if len(out) >= 2:
            break
    return out


def weapon_hand_choice(snap) -> tuple | None:
    """Select the game's own melee weapon by native item category, not by name."""
    held = getattr(snap, "equipped_tool", None)
    if not is_weapon(held):
        held = (getattr(snap, "held_item", None) or {}).get("qualified_item_id")
    if is_weapon(held):
        return None
    if getattr(snap, "select_item_available", False):
        for item in getattr(snap, "selectable_items", ()) or ():
            if isinstance(item, Mapping) and is_weapon(item.get("qualified_item_id")):
                return "select_item", None, dict(item), "weapon"
    for item in getattr(snap, "selectable_items", ()) or ():
        if isinstance(item, Mapping) and is_weapon(item.get("qualified_item_id")):
            slot = item.get("slot")
            if type(slot) is int and slot >= TOOLBAR_SLOTS:
                return "item_to_toolbar", None, dict(qualified_item_id=item["qualified_item_id"]), "weapon"
    return None


def combat_auto_choice(objective: CombatObjective | None, snap, *, approach_progress: Mapping | None = None) -> tuple | None:
    """D-B3-6 combat_rule: (operation, target_ref, args, label) for the offered combat step the model ignored, or None.
    reflex_combat when offered (weapon held, monster near, time rule); else select_item of the weapon's own slot when
    the weapon is not held. Only steps the snapshot already offers; the same admission gates run on the result."""
    if not combat_open(objective) or objective.heal_pending or objective.calls >= MAX_COMBAT_CALLS:
        return None
    summary = getattr(snap, "combat_objective", None) or {}
    target = getattr(snap, "reflex_combat_target", None)
    if summary.get("weapon_held") is False and not objective.disengage:
        target = None  # ADR 177: the body's walk out needs no weapon
    if isinstance(target, Mapping):
        args = fill_combat_args({}, target, objective)
        if args is not None:
            return OPERATION_COMBAT, None, args, "reflex_combat"
    if (summary.get("weapon_held") is False and type(summary.get("weapon_slot")) is int
            and getattr(snap, "select_item_available", False)):
        for item in getattr(snap, "selectable_items", ()) or ():
            if isinstance(item, Mapping) and item.get("slot") == summary["weapon_slot"]:
                return "select_item", None, dict(item), "weapon_slot:%d" % item["slot"]
    hand = weapon_hand_choice(snap)
    if hand is not None and summary.get("weapon_held") is False:
        return hand
    progress = approach_progress or {}
    for entry in getattr(snap, "approach_targets", ()) or ():
        if not isinstance(entry, Mapping) or entry.get("kind") != KIND_MONSTER_ENTRY:
            continue
        if progress.get("status") == "ARRIVED" and progress.get("target_ref") == entry.get("ref"):
            continue
        return "approach_target", str(entry["ref"]), {}, str(entry["ref"])
    return None


def combat_wire(objective: CombatObjective | None, weapon: Mapping | None = None) -> dict | None:
    if objective is None:
        return None
    out = {"kind": OBJ_KILL, "target": objective.kind, "count": objective.count, "kills": objective.kills,
           "status": objective.status, "reflex_calls": objective.calls,
           "calls_left": max(0, MAX_COMBAT_CALLS - objective.calls)}
    if objective.escape:
        out["escape"] = True
    if objective.disengage:
        out["disengage"] = True
    if objective.status == "OPEN" and isinstance(weapon, Mapping):
        # D-B3-6 (live V2B3ACC11a): the model asked 10x for a selectable weapon target with the sword held.
        out.update({k: weapon[k] for k in ("weapon_held", "weapon_slot", "weapon_in_toolbar") if k in weapon})
    if objective.last_verdict:
        out["last_verdict"] = objective.last_verdict
    if objective.heal_pending:
        out["heal_pending"] = True
    if objective.status != "OPEN" or objective.reason in ("heal_needed", "killed_other_monster"):
        out["reason"] = objective.reason
    return out


# ---------------------------------------------------------------------------------------------- charge objective

@dataclass(frozen=True)
class ChargeObjective:
    goal_id: str
    tool: str = "hoe"                  # hoe | can
    level: int = 1
    tiles: int = 1                     # tiles that must change (owner "[recorded remark omitted]"), else 1
    status: str = "OPEN"               # OPEN | CHARGED | BLOCKED | UNCERTAIN
    reason: str = "charge_not_done_yet"
    calls: int = 0
    last_verdict: str | None = None
    last_detail: str | None = None
    tiles_changed: int = 0
    request_ids: tuple = ()
    charged_request_id: str | None = None


def parse_charge_goal(text: object) -> tuple[str, int, int] | None:
    """Anchored owner grammar: (tool 'hoe' | 'can', level 1-4, tiles required), or None."""
    if not isinstance(text, str):
        return None
    m = _anchored(_CHARGE_LEVEL_RE, text)
    if m is not None:
        tool = "hoe" if m["tool"].casefold() in ("hoe", "cangkul") else "can"
        return tool, int(m["n"]), 1
    m = _anchored(_CHARGE_TILES_RE, text)
    if m is not None:
        tiles = _TILE_WORDS[m["n"].casefold()]
        level = next(lvl for lvl, n in TILES_FOR_LEVEL.items() if n == tiles)
        tool = "hoe" if m["verb"].casefold() in ("cangkul", "till", "hoe") else "can"
        return tool, level, tiles
    return None


def charge_open(objective: ChargeObjective | None) -> bool:
    return objective is not None and objective.status == "OPEN"


def charge_target(ui_facts: Mapping | None, objective: ChargeObjective | None) -> dict | None:
    """The offered reflex_charge target: the mod says the objective's level is admitted here (tool, cap, faced tile, area)."""
    if not charge_open(objective) or objective.calls >= MAX_CHARGE_CALLS:
        return None
    cap = _cap(ui_facts, OPERATION_CHARGE)
    charge = ui_facts.get("charge") if isinstance(ui_facts, Mapping) else None
    if cap is None or cap.get("available") is not True or not isinstance(charge, Mapping):
        return None
    levels = charge.get("levels")
    if charge.get("tool") != objective.tool or not isinstance(levels, Mapping) or levels.get(str(objective.level)) != "ok":
        return None
    tile = charge.get("tool_tile")
    return {"tool": objective.tool, "target_level": objective.level, "cap": charge.get("cap"),
            "tool_tile": list(tile) if isinstance(tile, (list, tuple)) else None}


def fill_charge_args(args: object, target: Mapping | None) -> dict | None:
    """Model args -> {target_level, max_ticks}: the level is the objective's (a different one refuses)."""
    if target is None:
        return None
    given = dict(args) if isinstance(args, Mapping) else {}
    if any(k not in command.REFLEX_CHARGE_ARG_KEYS for k in given):
        return None
    filled = {"target_level": target["target_level"], "max_ticks": command.REFLEX_CHARGE_MAX_TICKS_DEFAULT, **given}
    if filled["target_level"] != target["target_level"]:
        return None
    try:
        command.validate_reflex_charge_args(filled)
    except command.KesalahanPerintah:
        return None
    return filled


_CHARGE_RETRYABLE = frozenset({"timeout", "pad_refused", "interrupted"})


def settle_charge(objective: ChargeObjective, *, request_id: str | None, lifecycle: str, cleanup: bool,
                  envelope: Mapping | None) -> ChargeObjective:
    if objective.status != "OPEN" or (request_id and request_id in objective.request_ids):
        return objective
    ids = objective.request_ids + ((request_id,) if request_id else ())
    env = envelope or {}
    verdict = env.get("reflex_verdict")
    parsed = parse_verdict(verdict, command.REFLEX_VERDICTS_CHARGE)
    reflex = env.get("reflex") if isinstance(env.get("reflex"), Mapping) else {}
    detail = str(reflex.get("detail") or "")[:80] or None
    if lifecycle in {"rejected", "not_taken"}:
        return replace(objective, request_ids=ids, last_verdict="refused", reason="reflex_refused",
                       last_detail=str(env.get("arm_status") or env.get("reject_reason") or "")[:80] or None)
    if lifecycle != "completed" or not cleanup or parsed is None:
        return replace(objective, request_ids=ids, calls=objective.calls + 1, status="UNCERTAIN",
                       reason="reflex_result_unverified", last_verdict=verdict if isinstance(verdict, str) else None,
                       last_detail=detail)
    kind, arg = parsed
    calls = objective.calls + 1
    changed = reflex.get("tiles_changed") if type(reflex.get("tiles_changed")) is int else 0
    base = dict(request_ids=ids, calls=calls, last_verdict=verdict, last_detail=detail, tiles_changed=changed)
    if (kind == "ChargeReached" and int(arg) >= objective.level and env.get("world_effect_status") == FINISH_CHARGE
            and changed >= objective.tiles):
        return replace(objective, **base, status="CHARGED", reason=FINISH_CHARGE, charged_request_id=request_id)
    if kind == "ChargeAborted" and arg not in _CHARGE_RETRYABLE:
        return replace(objective, **base, status="BLOCKED", reason="reflex_aborted:" + str(arg))
    if calls >= MAX_CHARGE_CALLS:
        return replace(objective, **base, status="BLOCKED", reason="reflex_call_budget_exhausted")
    reason = "too_few_tiles_changed" if kind == "ChargeReached" else "charge_not_done_yet"
    return replace(objective, **base, reason=reason)


def charge_finish_verdict(objective: ChargeObjective | None) -> tuple[str, str] | None:
    if objective is None:
        return None
    if objective.status == "CHARGED":
        return "achieved", FINISH_CHARGE
    if objective.status == "UNCERTAIN":
        return "uncertain", objective.reason
    return "partial", objective.reason


def charge_wire(objective: ChargeObjective | None) -> dict | None:
    if objective is None:
        return None
    out = {"kind": OBJ_CHARGE, "tool": objective.tool, "level": objective.level, "tiles": objective.tiles,
           "status": objective.status, "reflex_calls": objective.calls,
           "calls_left": max(0, MAX_CHARGE_CALLS - objective.calls)}
    if objective.last_verdict:
        out["last_verdict"] = objective.last_verdict
    if objective.status != "OPEN":
        out["reason"] = objective.reason
    return out


def journal_fields(envelope: Mapping | None) -> dict:
    """Bounded journal projection of one reflex_combat / reflex_charge result (never per tick)."""
    env = envelope or {}
    reflex = env.get("reflex") if isinstance(env.get("reflex"), Mapping) else {}
    out = {"verdict": env.get("reflex_verdict"), "outcome": env.get("outcome"), "status": env.get("status"),
           "cleanup_confirmed": env.get("cleanup_confirmed"), "press_count": env.get("press_count"),
           "world_effect_status": env.get("world_effect_status")}
    for key in ("detail", "ticks", "kills", "swings", "steps", "presses", "presses_refused", "releases_proved", "hp_start",
                "hp_min", "hp_end", "max_hp", "at_exit", "retreat_reason", "food_slot", "tool", "target_level", "held_ticks",
                "power_at_release", "max_power", "area_tiles", "tiles_expected", "tiles_changed", "tool_settled",
                "damage_dealt", "defends", "disengage_route"):
        if key in reflex:
            out[key] = reflex[key]
    if isinstance(reflex.get("killed_types"), (list, tuple)):
        out["killed_types"] = list(reflex["killed_types"])[:5]
    if isinstance(reflex.get("downed"), Mapping):
        out["downed"] = dict(reflex["downed"])
    if isinstance(reflex.get("kill_witness"), Mapping):
        out["kill_witness"] = dict(reflex["kill_witness"])
    if isinstance(reflex.get("target"), Mapping):
        out["target"] = {k: reflex["target"][k] for k in ("id", "type", "hp_start", "hp_end", "max_hp", "dist_start", "dist_end")
                         if k in reflex["target"]}
    if isinstance(reflex.get("hidden_types"), (list, tuple)):
        out["hidden_types"] = [str(t)[:40] for t in reflex["hidden_types"]][:5]
    return out


# ADR 153 (fix87B): a stopped batch is one slice of a fight, not the fight. a63 d2 J1004: a 4 s batch began 7.6 tiles
# from its target and spent 10 of 14 presses walking (~19 ticks per 8-tick step); a Rusty Sword (2-5 damage) needs
# about 7 landed swings for a 24 HP Green Slime. The fight continues while the native readback shows it moving.
APPROACH_PROGRESS_TILES = 1.0


def batch_progress(fields: Mapping | None) -> str | None:
    """'damage' / 'approach' when one settled reflex batch moved its fight forward (native facts only), else None.

    damage: the monsters it picked or swung at lost HP (``damage_dealt``). approach: no swing landed yet, and its
    target is at least APPROACH_PROGRESS_TILES closer than when it was picked. A batch that swung and hurt nothing
    (a Rock Crab in its shell, a miss) is no progress; an older mod without these fields reports none."""
    if not isinstance(fields, Mapping):
        return None
    dealt = fields.get("damage_dealt")
    if type(dealt) is int and dealt > 0:
        return "damage"
    target = fields.get("target") if isinstance(fields.get("target"), Mapping) else {}
    start, end = target.get("dist_start"), target.get("dist_end")
    swings = fields.get("swings")
    if (type(dealt) is int and type(swings) is int and swings == 0
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in (start, end))
            and start - end >= APPROACH_PROGRESS_TILES):
        return "approach"
    return None


def unhittable_fact(fields: Mapping | None) -> str | None:
    """ADR 153: the native reason a batch could not hurt anything now ("Duggy:hidden"), or None."""
    detail = str((fields or {}).get("detail") or "")
    m = re.fullmatch(r"(?:target_hidden|only_hidden) type=([A-Za-z]{1,40})", detail)
    return m[1] + ":hidden" if m else None
