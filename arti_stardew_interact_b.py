"""WS3b (Primitives v1, 2026-10-07): talk to an NPC, eat one item, go to bed — pure helpers.

No IO, no provider, no command inbox. The runtime mixin (``arti_stardew_interact_b_runtime.py``)
calls these at small hook sites. The mod stays authoritative for every target
(``world_interaction_target`` kinds ``npc`` / ``eat``, ``move_through_target`` kind ``bed``); these
helpers only decide presentation (what to offer), objective bookkeeping from authenticated owner
text, the deterministic follow-through over the DialogueBox the press opened (WS2 ``ui_key`` with
the runtime's own allowlisted answers) and the finish verdicts.

Follow-through policy (owner rules, Primitives v1):
- talk: page the NPC's own dialogue closed with ``dialogue:advance``; an ``npc_question`` is NEVER
  auto-answered (no allowlisted/known-safe choice exists) — the chain stops and the menu is left to
  the model's WS2 ``ui_key`` offer.
- eat: answer the ``Eat`` question ``Yes`` (response key ``Yes``) once.
- bed: answer the ``Sleep`` question ``Yes`` once; a farmhand that sleeps first waits in the
  ``ReadyCheckDialog`` (wait-only) for every player incl. the HOST, who passes out at 02:00 per the
  owner decision. The runtime holds model continuations (``bed_waiting_for_host`` heartbeat every
  30 s) until the day changes or the owner stops play — never released while ARTI sits in the
  ReadyCheckDialog; WS2's day-start rule then owns the next morning.

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the CT live witness.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import unquote

KIND_NPC = "npc"
KIND_EAT = "eat"
KIND_BED = "bed"
RECEIVER_KINDS = (KIND_NPC, KIND_EAT)

OBJ_TALK = "talk_npc"
OBJ_EAT = "eat"
OBJ_SLEEP = "sleep"

EFFECT_DIALOGUE_OPEN = "observed_dialogue_open"
EFFECT_EAT_QUESTION = "observed_eat_question"
EFFECT_SLEEP_QUESTION = "observed_sleep_question"
OPEN_EFFECTS = frozenset({EFFECT_DIALOGUE_OPEN, EFFECT_EAT_QUESTION, EFFECT_SLEEP_QUESTION})
EFFECT_FOR_KIND = {KIND_NPC: EFFECT_DIALOGUE_OPEN, KIND_EAT: EFFECT_EAT_QUESTION, KIND_BED: EFFECT_SLEEP_QUESTION}
OUTCOME_FOR_KIND = {KIND_NPC: "DialogueOpened", KIND_EAT: "QuestionOpened", KIND_BED: "TransitionObserved"}
QUESTION_FOR_KIND = {KIND_EAT: "Eat", KIND_BED: "Sleep"}

FINISH_DIALOGUE_CLOSED = "observed_dialogue_closed"
FINISH_EAT = "observed_eat_effect"
FINISH_DAY = "observed_day_change"
PARTIAL_BED_WAIT = "bed_ready_waiting"

RESULT_SCOPE = {
    KIND_NPC: "the NPC's dialogue opens; the runtime then pages it closed with ui_key (no gift: plain tool in hand)",
    KIND_EAT: "the Eat question opens for the held item; the runtime answers Yes with ui_key",
    KIND_BED: "walk onto the bed's sleep tile; the Sleep question opens and the runtime answers Yes with ui_key",
}

# Bounds (follow-through is deterministic and bounded; never a retry loop).
TALK_MAX_UI_STEPS = 12
ANSWER_MAX_UI_STEPS = 1
SLEEP_NOT_STARTED = "sleep_not_started"  # fix54H: a verified Yes that opened no native ReadyCheck
# fix55C / ADR 78 (REVIEW2 #1): native night-flow menus after her bed Yes. The HOST readies the sleep check on its next tick
# (870e62a), so the ReadyCheckDialog may live only a few ticks and the night goes straight to these.
NIGHT_FLOW_MENUS = ("ReadyCheckDialog", "SaveGameMenu", "ShippingMenu", "LevelUpMenu")
FOLLOW_STEP_WAIT_S = 10.0     # one dialogue page may still be typing (DialogueBox typewriter)
FOLLOW_TOTAL_S = 90.0
OPEN_READBACK_S = 2.5
EAT_READBACK_S = 4.0
# FIXB24 D-B21-1 (live V2B21EAT-a: energy 246 -> 270 but the journal said `energy_gain 0.0`): eatHeldObject takes the item
# at once and applies stamina / health at the end of the eating animation; after the count drops, the vitals are re-read
# until they rise or this settle window ends (inside EAT_READBACK_S).
EAT_VITALS_SETTLE_S = 2.5
BED_DEFAULT_TICKS = 24
MAX_EAT_PRICE = 100           # mirror of the mod BgEatPrompt.MaxPrice
SURVIVAL_EAT_PRICE = 300      # mirror of BgEatPrompt.SurvivalMaxPrice (FINAL B7: hotbar food below the survival HP line)
NPC_REACH = 12
BED_REACH = 16
# D28 (live B4W3b): an idle actor gets no scene record for minutes (scene = snapshot-on-demand), so
# the 2 s window gate dropped every talk / bed entry. NPC / bed / eat-site entries need no sub-2 s
# freshness (the mod re-checks the exact target at press time): accept a record this old for them.
ROUTINE_SCENE_MAX_AGE_S = 60.0
# D25: one short press toward the faced empty floor tile moves the grab tile off ARTI's own tile
# (Character.GetGrabTile needs the box within 3-11 px of the edge) without crossing into that tile.
EAT_SITE_FLUSH_TICKS = 3
# Mirror of the mod BgNpcTalk.ExcludedNames (never a talk target).
EXCLUDED_NPCS = frozenset(n.casefold() for n in ("Krobus", "Dwarf", "Henchman", "Bouncer", "Leo", "Fizz", "Mister Qi",
                                                 "Gunther", "Marlon", "Morris", "Raccoon", "MrsRaccoon"))
# FIXB7 curfew guard (live V2B5b end state: ARTI passed out on the Farm at 02:00, shared wallet -1000g). timeOfDay
# counts past midnight: 2400 = 24:00, 2500 = 01:00 (mod WorldExtras.HardStopTime), 2600 = 02:00 pass-out.
CURFEW_SOFT_TIME = 2400
CURFEW_HARD_TIME = 2500
CURFEW_REASON = "curfew"


def curfew_phase(time_of_day) -> str | None:
    """"hard" from 01:00 (abort open objectives, pin sleep), "soft" from 24:00 (rules start no new stage), else None."""
    if type(time_of_day) is not int:
        return None
    return "hard" if time_of_day >= CURFEW_HARD_TIME else "soft" if time_of_day >= CURFEW_SOFT_TIME else None


_TALK_TOOLS = re.compile(r"^\(T\)\w*(?:Axe|Pickaxe|Hoe|WateringCan)$")


@dataclass
class RoutineObjective:
    """One owner-authored WS3b objective inside the existing goal (never a model grant)."""
    kind: str                       # talk_npc | eat | sleep
    npc_name: str | None = None     # talk: owner-named villager (casefolded), None = any talkable villager
    start_date: tuple | None = None
    target_ref: str | None = None   # npc / bed fact id, eat source id of the opened receiver
    speaker: str | None = None
    opened: bool = False            # verified DialogueOpened / Eat / Sleep question
    closed: bool = False            # talk: verified dialogue closed afterwards
    answered: bool = False          # eat / sleep: verified Yes answer
    eat_effect: dict | None = None
    waiting_since: float | None = None
    wait_menu: str | None = None
    day_changed: bool = False
    finish_recorded: bool = False
    eat_site_ref: str | None = None      # D25: the eat-site approach entry last offered
    eat_site_adjusted: bool = False
    eat_force: bool = False              # FIXB18 D-B15a-2: the owner asked to eat even when already full
    eat_rule_attempts: int = 0           # D30: runtime-published eat presses (bounded)   # D17: the runtime recorded the sleep goal's own verified finish
    ui_steps: int = 0
    blocker: str | None = None
    stop: str | None = None


_ANY_NPC = r"(?:(?:an?|one|satu|seorang) )?(?:npc|villager|warga|warga yang terlihat|visible villager|visible npc|orang|penduduk|someone|seseorang|somebody)"
_CLOSE = r"(?: (?:and|lalu|terus|dan|then) (?:close|tutup|selesaikan) (?:the |its )?(?:dialogue|dialog|dialognya|percakapannya|chat))?"


def parse_routine_objective(text: str) -> RoutineObjective | None:
    """Anchored owner grammar (authenticated ingress only; never model/scene text). Count is one."""
    t = re.sub(r"\s+", " ", text.strip().rstrip(".!")).casefold()
    m = re.fullmatch(r"(?:talk (?:to|with)|chat with|speak (?:to|with)|greet|ngobrol (?:dengan|sama|ama|ke)"
                     r"|bicara (?:dengan|sama|ke)|sapa|ajak ngobrol) (.{2,32}?)" + _CLOSE, t)
    if m:
        who = m[1].strip()
        if re.fullmatch(_ANY_NPC, who):
            return RoutineObjective(OBJ_TALK)
        if re.fullmatch(r"[a-z][a-z' .\-]{1,23}", who) and who not in {"the house", "home", "rumah"}:
            return RoutineObjective(OBJ_TALK, npc_name=who)
        return None
    m = re.fullmatch(r"(?P<pre>tetap )?(?:eat|makan)(?: (?:one|an|a|satu|1|sebuah))?(?: (?:item|food|thing|something|snack"
                     r"|makanan|barang|sesuatu|camilan))?(?P<post> (?:anyway|even if full|tetap|walaupun kenyang|meski kenyang"
                     r"|biarpun kenyang))?", t)
    if m:
        # FIXB18 D-B15a-2: "tetap makan" / "makan walaupun kenyang" / "eat anyway" eats even with energy + HP full
        return RoutineObjective(OBJ_EAT, eat_force=bool(m["pre"] or m["post"]))
    if re.fullmatch(r"(?:go to (?:bed|sleep)|sleep|tidur|pergi tidur|ayo tidur|ke kasur|ke ranjang|go sleep)", t):
        return RoutineObjective(OBJ_SLEEP)
    return None


def receiver_kind(args: object) -> str | None:
    kind = args.get("kind") if isinstance(args, Mapping) else None
    return kind if kind in RECEIVER_KINDS else None


def through_kind(args: object) -> str | None:
    return KIND_BED if isinstance(args, Mapping) and args.get("kind") == KIND_BED else None


def npc_name_from_fact_id(fact_id: object) -> str | None:
    """npc:<loc>:<internal name token>[:discriminator] -> casefolded internal name."""
    parts = fact_id.split(":") if isinstance(fact_id, str) else []
    if len(parts) < 3 or parts[0] != "npc":
        return None
    return unquote(parts[2]).casefold()


def _name_matches(objective: RoutineObjective, fact_id: str, fact: Mapping | None) -> bool:
    if objective.npc_name is None:
        return True
    names = {npc_name_from_fact_id(fact_id)}
    if isinstance(fact, Mapping):
        for key in ("internal_name", "display_name"):
            if isinstance(fact.get(key), str):
                names.add(unquote(fact[key]).casefold())
    return objective.npc_name in names


def objective_open(objective: RoutineObjective | None) -> bool:
    return objective is not None and objective.blocker is None and objective.stop is None


def eat_candidates(ui_facts: Mapping | None) -> list[dict]:
    """The mod's published eat_candidates (already filtered: cheap, non-artisan, non-quest, non-usable)."""
    raw = (ui_facts or {}).get("eat_candidates")
    out = []
    for c in raw if isinstance(raw, (list, tuple)) else ():
        # FINAL B7: the mod flags a hotbar food it admitted only under its survival exception (HP below the line)
        cap = SURVIVAL_EAT_PRICE if isinstance(c, Mapping) and c.get("survival") is True else MAX_EAT_PRICE
        if (isinstance(c, Mapping) and all(type(c.get(k)) is int for k in ("slot", "price", "stack"))
                and isinstance(c.get("qualified_item_id"), str) and 0 <= c["price"] <= cap):
            out.append(dict(c))
    return out


def choose_backpack_eat_candidate(ui_facts: Mapping | None) -> dict | None:
    """FIXB26: the cheapest backpack food (the mod's ``eat_backpack_candidates``, same filter and policy), or None."""
    raw = (ui_facts or {}).get("eat_backpack_candidates")
    # FINAL B7: the survival exception is hotbar-only; the backpack keeps the cheap-food cap
    raw = [{k: v for k, v in c.items() if k != "survival"} if isinstance(c, Mapping) else c
           for c in (raw if isinstance(raw, (list, tuple)) else ())]
    found = choose_eat_candidate({"eat_candidates": raw})
    return found if found is not None and found["slot"] >= 12 else None


def choose_eat_candidate(ui_facts: Mapping | None) -> dict | None:
    """Deterministic: the cheapest candidate (price, then smallest stack, then slot). The model never picks.

    FINAL B7 (J3156-J3176): below the survival HP line (the mod's ``eat_survival``) the hotbar food that restores
    the most (edibility) wins instead, then the cheapest; the staged Salad beats a Parsnip."""
    candidates = eat_candidates(ui_facts)
    if not candidates:
        return None
    if (ui_facts or {}).get("eat_survival") is True:
        return min(candidates, key=lambda c: (-(c["edibility"] if type(c.get("edibility")) is int else 0),
                                              c["price"], c["slot"]))
    return min(candidates, key=lambda c: (c["price"], c["stack"], c["slot"]))


def eat_next_step(food: Mapping | None, active_slot: object, eat_target_offered: bool,
                  eat_site_offered: bool = False) -> str:
    """The next act of an eat goal (presentation only; the mod and the gates stay authoritative)."""
    if food is None:
        return "no_food_candidate"          # nothing cheap and edible in the hotbar: ask the owner / finish partial
    if eat_target_offered:
        return "world_interact_eat"
    if active_slot != food["slot"]:
        return "select_item_food"
    if eat_site_offered:
        return "approach_target_eat_site"   # D25: the runtime turns / steps to face an empty floor tile
    return "face_empty_floor_tile"          # the eat prompt needs nothing at the tile ahead, below / above it, or here


def eat_source_slot(source_fact_id: object) -> int | None:
    parts = source_fact_id.split(":") if isinstance(source_fact_id, str) else []
    return int(parts[1]) if len(parts) >= 3 and parts[0] == "inv" and parts[1].isdigit() else None


def interaction_offered(target: Mapping | None, *, gathering: bool, objective: RoutineObjective | None,
                        facts: Mapping | None = None, ui_facts: Mapping | None = None) -> bool:
    """Presentation filter for the mod-observed npc / eat target (the mod stays authoritative)."""
    kind = receiver_kind(target)
    if kind is None or gathering or not objective_open(objective):
        return False
    if kind == KIND_NPC:
        source = target.get("source_fact_id")
        return (objective.kind == OBJ_TALK and not objective.opened and isinstance(source, str)
                and _name_matches(objective, source, (facts or {}).get(source)))
    chosen = choose_eat_candidate(ui_facts)
    return (objective.kind == OBJ_EAT and not objective.opened and chosen is not None
            and eat_source_slot(target.get("source_fact_id")) == chosen["slot"])


def move_through_offered(target: Mapping | None, *, gathering: bool, objective: RoutineObjective | None) -> bool:
    return (through_kind(target) is not None and not gathering and objective_open(objective)
            and objective.kind == OBJ_SLEEP and not objective.opened)


def filter_selectable(items: tuple, objective: RoutineObjective | None, ui_facts: Mapping | None) -> tuple:
    """Eat: only the runtime's chosen (cheapest) candidate; talk: only plain tools (never a giftable item)."""
    if not objective_open(objective):
        return items
    if objective.kind == OBJ_EAT:
        chosen = choose_eat_candidate(ui_facts)
        return tuple(t for t in items if chosen is not None
                     and (t.get("slot"), t.get("qualified_item_id")) == (chosen["slot"], chosen["qualified_item_id"]))
    if objective.kind == OBJ_TALK:
        return tuple(t for t in items if _TALK_TOOLS.fullmatch(str(t.get("qualified_item_id") or "")))
    return items


def _tile(fact: Mapping, key: str = "tile") -> tuple[int, int] | None:
    tile = fact.get(key)
    if not isinstance(tile, Mapping) or any(type(tile.get(k)) is not int for k in ("x", "y")):
        return None
    return tile["x"], tile["y"]


def _revision(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def approach_entries(facts: Mapping, motor_tile: tuple[int, int], objective: RoutineObjective | None,
                     *, npc_reach=NPC_REACH) -> list[dict]:
    """Approach catalog entries for the objective's own targets (talkable NPCs, the owned bed's sides)."""
    if not objective_open(objective) or objective.opened:
        return []
    out: list[dict] = []
    for ref, fact in facts.items():
        if not isinstance(fact, Mapping) or not isinstance(ref, str) or len(ref) > 100:
            continue
        if objective.kind == OBJ_TALK and fact.get("kind") == "npc":
            tile = _tile(fact)
            if (tile is None or fact.get("villager") is not True or fact.get("talked_today") is True
                    or not _name_matches(objective, ref, fact)
                    or npc_reach is not None and max(abs(tile[0] - motor_tile[0]), abs(tile[1] - motor_tile[1])) > npc_reach):
                continue
            # Revision over identity + tile: a moved NPC is a changed target (rebuilt from fresh facts).
            out.append(dict(ref=ref, tile=[tile[0], tile[1]], kind=KIND_NPC, revision=_revision(ref, tile)))
        elif objective.kind == OBJ_SLEEP and fact.get("kind") == "bed" and fact.get("owned_by_actor") is True:
            entries = fact.get("entry_tiles")
            if not isinstance(entries, (list, tuple)):
                continue
            for entry in entries[:2]:
                tile = _tile({"tile": entry})
                if (tile is None or min(tile) < 0
                        or max(abs(tile[0] - motor_tile[0]), abs(tile[1] - motor_tile[1])) > BED_REACH):
                    continue
                # Stand beside the bed on its sleep row; move_through then walks in sideways.
                out.append(dict(ref=f"{ref}@{tile[0]},{tile[1]}", tile=[tile[0], tile[1]], kind=KIND_BED,
                                reach="DIRECT_TARGET", revision=_revision(ref, tile)))
    return out


def npc_entry_skips(facts: Mapping, motor_tile: tuple[int, int], objective: RoutineObjective | None,
                    *, npc_reach=NPC_REACH) -> list[dict]:
    """D28: why each NPC fact is NOT a talk approach entry (diagnostic; same rules as approach_entries)."""
    if objective is None or objective.kind != OBJ_TALK:
        return []
    out = []
    for ref, fact in facts.items():
        if not isinstance(fact, Mapping) or fact.get("kind") != "npc" or not isinstance(ref, str):
            continue
        name = npc_name_from_fact_id(ref) or str(fact.get("internal_name") or "")
        tile = _tile(fact)
        reason = ("ref_too_long" if len(ref) > 100 else "no_tile" if tile is None
                  else "not_villager" if fact.get("villager") is not True
                  else "excluded_name" if name.casefold() in EXCLUDED_NPCS
                  else "talked_today" if fact.get("talked_today") is True
                  else "other_npc" if not _name_matches(objective, ref, fact)
                  else "too_far" if npc_reach is not None and max(abs(tile[0] - motor_tile[0]), abs(tile[1] - motor_tile[1])) > npc_reach
                  else None)
        if reason is not None:
            out.append({"npc": name, "reason": reason})
    return out


def nearest_eat_stand(grid, start, scene_facts, *, excluded=()):
    """A7: nearest reachable candidate, never eat permission. Native EatSiteMiss stays authoritative.

    Walk only explicit open cells. Walkable HoeDirt/crops still block the own/grab/above/below
    footprint. Unknown/out-of-window cells, water, exits and sleep rows cannot be candidates.
    """
    from arti_stardew_goal_routes import open_paths, open_path
    parents = open_paths(grid, start)
    blocked = set()
    for fact in (scene_facts or {}).values():
        if not isinstance(fact, Mapping):
            continue
        tile = _tile(fact)
        if tile is not None and not (fact.get("kind") == "terrain"
                                     and fact.get("terrain_type") in ("Grass", "Flooring")):
            blocked.add(tile)
    excluded = set(excluded) | {tuple(start)}
    # BFS insertion order ranks by actual path length, not Manhattan distance through obstacles.
    for stand in parents:
        if stand in excluded:
            continue
        for facing, (dx, dy) in enumerate(((0, -1), (1, 0), (0, 1), (-1, 0))):
            grab = stand[0] + dx, stand[1] + dy
            footprint = (stand, grab, (grab[0], grab[1]-1), (grab[0], grab[1]+1))
            if all(grid.get(t) == "." and t not in blocked for t in footprint):
                return open_path(parents, stand), facing
    return None


def eat_site_entry(ui_facts: Mapping | None, objective: RoutineObjective | None, *, eat_target_offered: bool) -> dict | None:
    """D25: approach entry 'stand here, face that empty floor tile' from the mod's eat_site hint.

    Only for an open eat objective with the runtime-chosen food in hand and no eat target yet."""
    if not objective_open(objective) or objective.kind != OBJ_EAT or objective.opened or eat_target_offered:
        return None
    facts = ui_facts or {}
    food = choose_eat_candidate(facts)
    toolbar = facts.get("toolbar") if isinstance(facts.get("toolbar"), Mapping) else {}
    site = facts.get("eat_site")
    if food is None or toolbar.get("active_slot") != food["slot"] or not isinstance(site, Mapping):
        return None
    keys = ("stand_x", "stand_y", "face_x", "face_y", "facing")
    if any(type(site.get(k)) is not int for k in keys) or not 0 <= site["facing"] <= 3:
        return None
    stand, face = (site["stand_x"], site["stand_y"]), (site["face_x"], site["face_y"])
    if abs(stand[0] - face[0]) + abs(stand[1] - face[1]) != 1 or min(stand + face) < 0:
        return None
    ref = f"eat_site:{face[0]},{face[1]}@{stand[0]},{stand[1]}"
    return dict(ref=ref, tile=[face[0], face[1]], kind="eat_site", reach_tiles=[[stand[0], stand[1]]],
                revision=_revision(ref))


def rank_override(entry: Mapping, objective: RoutineObjective | None) -> int | None:
    if objective_open(objective) and entry.get("kind") in (KIND_NPC, KIND_BED, "eat_site"):
        return 0
    return None


def bed_row_tiles(facts: Mapping) -> set[tuple[int, int]]:
    """Every tile of a bed's sleep row: approach routing never enters it (the touch would fire mid-step)."""
    out: set[tuple[int, int]] = set()
    for fact in facts.values():
        if isinstance(fact, Mapping) and fact.get("kind") == "bed":
            tile, width = _tile(fact), fact.get("width")
            if tile is not None and type(width) is int and 1 <= width <= 4:
                out |= {(tile[0] + i, tile[1] + 1) for i in range(width)}
    return out


@dataclass(frozen=True)
class FollowStep:
    kind: str                       # act | wait | done | stop
    reason: str
    args: dict | None = None


def _menu_type(ui_facts: Mapping | None) -> str | None:
    interactive = (ui_facts or {}).get("interactive")
    menu = interactive.get("menu_type") if isinstance(interactive, Mapping) else None
    return menu if isinstance(menu, str) and menu else None


def _receiver_token(ui_facts: Mapping | None) -> str | None:
    interactive = (ui_facts or {}).get("interactive")
    token = interactive.get("receiver_token") if isinstance(interactive, Mapping) else None
    return token if isinstance(token, str) and token else None


def plan_follow_step(kind: str, view, ui_facts: Mapping | None, objective: RoutineObjective, *,
                     closed_receiver: str | None = None) -> FollowStep:
    """Next deterministic menu step after the press opened its DialogueBox (pure; view = UiKeyView).

    ``closed_receiver``: the receiver our last verified ui_key proved closed (effect "closed"). FINAL B4 (J L2681-L2683;
    farmhand SMAPI 12:55:23.44 DialogueBox -> 23.597 null, no second box): the answered box still shows the same
    receiver while it closes (UiKeyInput: ready false, "transitioning"). That record is not a new menu, so the step
    waits for the republish (bounded by FOLLOW_STEP_WAIT_S). Any other receiver is judged as before."""
    if closed_receiver is not None and _receiver_token(ui_facts) == closed_receiver:
        return FollowStep("wait", "closed_receiver_not_republished")
    menu = _menu_type(ui_facts)
    if kind == KIND_NPC:
        if menu is None:
            return FollowStep("done", "dialogue_closed")
        if menu != "DialogueBox":
            return FollowStep("stop", "unexpected_menu:" + menu[:48])
        if view is None or view.menu_type != "DialogueBox":
            return FollowStep("wait", "ui_key_unavailable")
        if view.kind == "npc_question":
            return FollowStep("stop", "npc_question_requires_choice")
        if view.kind != "dialogue":
            return FollowStep("stop", "unexpected_dialogue_kind:" + str(view.kind))
        if not view.available:
            return FollowStep("wait", view.not_ready_reason or "not_ready")
        return FollowStep("act", "advance", view.args_for("advance"))
    question = QUESTION_FOR_KIND.get(kind)
    if question is None:
        return FollowStep("stop", "unknown_kind")
    if objective.answered:
        if menu is None and kind == KIND_BED:
            # ADR 72 (a39 Sp13 FH:L29150-L29154): the Yes closed the box, no ReadyCheckDialog opened and she was
            # free again. A closed question is not sleep: the runtime judges the native date / player-free signals.
            return FollowStep("wait", SLEEP_NOT_STARTED)
        if menu is None:
            return FollowStep("done", "answered")
        if kind == KIND_BED and menu == "ReadyCheckDialog":
            return FollowStep("done", "waiting_ready_check")
        if kind == KIND_BED and menu in NIGHT_FLOW_MENUS:
            return FollowStep("done", "night_menu:" + menu)  # fix55C N1: the brief ReadyCheck was never polled
        return FollowStep("stop", "unexpected_menu:" + menu[:48])
    if menu is None:
        return FollowStep("stop", "question_closed_unanswered")
    if menu != "DialogueBox":
        return FollowStep("stop", "unexpected_menu:" + menu[:48])
    if view is None or view.kind != "question":
        return FollowStep("wait", "ui_key_unavailable")
    if view.question_key != question:
        return FollowStep("stop", "unexpected_question:" + str(view.question_key))
    if not view.available:
        return FollowStep("wait", view.not_ready_reason or "not_ready")
    yes = [o["index"] for o in view.options if o.get("response_key") == "Yes"]
    if len(yes) != 1:
        return FollowStep("stop", "yes_option_missing")
    return FollowStep("act", "answer_yes", view.args_for("answer", option_index=yes[0]))


def vitals_full(ui_facts: Mapping | None) -> dict | None:
    """FIXB18 D-B15a-2: {"energy_health_full": True} when the mod's stable ``eat_full`` bool says stamina AND health are
    at their maximum (eating gains nothing), else None (not full, or not published)."""
    full = (ui_facts or {}).get("eat_full") if isinstance(ui_facts, Mapping) else None
    return {"energy_health_full": True} if full is True else None


def eat_effect(before: Mapping, after: Mapping) -> dict | None:
    """The eaten item's count must drop by exactly one; vitals gains alone cannot prove eating."""
    try:
        consumed = before["count"] - after["count"] == 1
        energy_gain = round(after["energy"] - before["energy"], 2)
        health_gain = after["health"] - before["health"]
    except (KeyError, TypeError):
        return None
    if not consumed or after.get("qid", before.get("qid")) != before.get("qid"):
        return None
    return {"qid": before.get("qid"), "consumed": consumed, "energy_gain": energy_gain,
            "health_gain": health_gain}


def eat_consumption_observed(observed: Mapping | None) -> bool:
    """Both existing finish schemas carry the same authenticated count-decrease effect.

    The routine rule flattens it; the ordinary finish nests it under eat_effect. Never infer
    consumption from gains, truthy values or conflicting projections (FIXB42 A9 t7/t18).
    """
    if not isinstance(observed, Mapping):
        return False
    effect = observed.get("eat_effect")
    if "eat_effect" in observed:
        return (isinstance(effect, Mapping) and effect.get("consumed") is True
                and ("consumed" not in observed or observed["consumed"] is True))
    return observed.get("consumed") is True


def objective_verdict(objective: RoutineObjective) -> tuple[str, str]:
    """(status, reason) for finish: achieved only from verified effects recorded by the runtime."""
    if objective.kind == OBJ_TALK:
        if objective.opened and objective.closed:
            return "achieved", FINISH_DIALOGUE_CLOSED
        if objective.blocker or objective.stop:
            return "partial", objective.blocker or objective.stop
        return "partial", "dialogue_not_closed" if objective.opened else "dialogue_not_opened"
    if objective.kind == OBJ_EAT:
        if objective.eat_effect and objective.eat_effect.get("consumed") is True:
            return "achieved", FINISH_EAT
        if objective.blocker or objective.stop:
            return "partial", objective.blocker or objective.stop
        return "partial", ("eat_effect_not_observed" if objective.answered else "eat_question_not_answered"
                           if objective.opened else "eat_not_started")
    if objective.kind == OBJ_SLEEP:
        if objective.day_changed and objective.answered:
            return "achieved", FINISH_DAY
        if objective.answered:
            return "partial", PARTIAL_BED_WAIT
        if objective.blocker or objective.stop:
            return "partial", objective.blocker or objective.stop
        return "partial", "sleep_question_not_answered" if objective.opened else "bed_not_reached"
    return "partial", "objective_unknown"


def sleep_signal(start_date: tuple | None, date: tuple | None, menu: str | None, is_player_free: bool | None, *,
                 after_yes: bool) -> str | None:
    """fix55C / ADR 78 (REVIEW2 #1): the native sign that the night began after an answered bed Yes, else None.

    A new date, a night-flow menu, or (on a motor frame newer than the Yes) a player held by the night fade.
    ``after_yes`` False never reads player-free: the closing DialogueBox can hold her for a tick."""
    if date is not None and start_date is not None and date != start_date:
        return "date_changed"
    if menu in NIGHT_FLOW_MENUS:
        return "night_menu:" + menu
    if after_yes and is_player_free is False and menu is None:
        return "player_not_free"
    return None


def bed_wait_state(objective: RoutineObjective | None, *, date: tuple | None) -> str | None:
    """None when no hold applies; 'waiting' while the continuation hold stays; 'day_changed' ends it.

    No time bound (review 2026-10-07): ARTI in the ReadyCheckDialog has nothing else to do until the
    host sleeps; the owner's STOP (no active goal) or the trial's own clock end the wait."""
    if objective is None or objective.kind != OBJ_SLEEP or not objective.answered or objective.waiting_since is None:
        return None
    if objective.day_changed:
        return None
    if date is not None and objective.start_date is not None and date != objective.start_date:
        return "day_changed"
    return "waiting"
