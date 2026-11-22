"""Primitives v2 WS-G6: world extras — pure helpers (objectives, offers, approach entries, readback, finish rule).

No IO, no provider, no command inbox. The mod is authoritative for every target (``world_interaction_target`` with a
WS-G6 kind, captured only with its default-OFF ``WorldExtrasEnabled`` flag on the pad path; the ``extras_targets``
list in ``ui.context``; the G6 menu contexts of the ``ui_pad`` block). This module only

* parses the owner's anchored ID / EN grammar into one :class:`ExtrasObjective` (authenticated owner text only),
* decides presentation (which mod-observed target / approach entry to offer for that objective),
* parses the mod's verdict detail (``effect_detail`` = ``kind|item|delta|value``) and records verified progress,
* plans the bounded deterministic follow-through (the shaft / bus / minecart question answer through WS2 ``ui_key``,
  the elevator floor through WS-G1 ``ui_nav`` / ``ui_press``), and
* computes the deterministic finish verdict (``extras_rule``) from verified effects only.

Policies (mirrored from the mod, documented here): the profession-menu rule (an XP gain crossing level 5 / 10 is
refused by the mod unless the owner policy ``ExtrasAllowProfessionMenu`` / ``allow_profession_menu`` is on; the live
save is five Farming XP short of 5, so harvest / animal pet / milk / shear are refused there), and the WS-G5 mines time
rule (no descent at or after 20:00, nothing new at or after 01:00).

Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G6 live witnesses.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import unquote
from collections.abc import Mapping
from dataclasses import dataclass, field

import arti_stardew_command as command

# ------------------------------------------------------------------ kinds and outcomes (mod Protocol/WorldExtras.cs)

KINDS = command.WORLD_EXTRAS_KINDS
OUTCOMES = command.WORLD_EXTRAS_OUTCOMES
RECEIVER_KINDS = ("tv", "mailbox", "shaft", "bus", "minecart", "mine_elevator", "shop_counter", "shipping_bin", "ladder_up", "animal_shop", "blacksmith", "community_note", "kitchen")
# Fix D-B2-1: the receiver menu type of each receiver kind (the runtime's readback after the press).
RECEIVER_MENUS = {"animal_shop": "DialogueBox", "blacksmith": "DialogueBox", "community_note": "JunimoNoteMenu", "tv": "DialogueBox", "mailbox": "LetterViewerMenu", "mine_elevator": "MineElevatorMenu", "shop_counter": "ShopMenu", "shipping_bin": "ItemGrabMenu", "kitchen": "CraftingPage"}
QUESTION_KEYS = {"animal_shop": "Marnie", "blacksmith": "Blacksmith", "shaft": "Shaft", "bus": "Bus", "minecart": "pagedResponse", "ladder_up": "ExitMine"}
X_KINDS = ("animal_milk", "animal_shear", "machine_insert", "place_item")

# WS-G5 mirror (ReflexContracts / arti_stardew_command REFLEX_COMBAT_*).
MINES_LATEST_DESCEND_TIME = command.REFLEX_COMBAT_LATEST_START_TIME
HARD_STOP_TIME = command.REFLEX_COMBAT_HARD_STOP_TIME
ANIMAL_SLEEP_TIME = 1900
DESCEND_KINDS = ("ladder_down", "shaft")
FARMING_XP_KINDS = ("harvest_crop", "animal_pet", "animal_milk", "animal_shear")

# Objective kind -> the world_interact kinds it may press (menus: none; the model drives the G1 menu offer).
OBJ_KINDS = {
    "morning": ("tv", "mailbox"),
    "gift": ("npc_gift",), "pet": ("pet",), "animal_pet": ("animal_pet",), "milk": ("animal_milk",), "shear": ("animal_shear",),
    "mount": ("horse_mount",), "dismount": ("horse_dismount",), "shake": ("shake_tree",), "harvest": ("harvest_crop",),
    "collect": ("machine_collect",), "insert": ("machine_insert",), "hay": ("hay_hopper",), "ladder": ("ladder_down",),
    "shaft": ("shaft",), "elevator": ("mine_elevator",), "bus": ("bus",), "minecart": ("minecart",),
    "place": ("place_item",), "bundle": (), "museum": (),
    # FIXB17 D-B5-0: the combat goal's Mines exit leg (never owner grammar: pinned by the combat finish in the Mines).
    "exit_mines": ("ladder_up",),
    "animals": ("animal_pet", "animal_milk", "animal_shear", "hay_hopper", "hay_place"),
}
MENU_OBJECTIVES = {"elevator": "mine_elevator", "bundle": "junimo_note", "museum": "museum"}

FINISH = "observed_extras_effect"
TRAVEL_OBJECTIVES = ("ladder", "shaft", "elevator", "bus", "minecart", "exit_mines")
QUESTION_OBJECTIVES = ("shaft", "bus", "minecart", "exit_mines")  # the press opens a question the runtime answers
FOLLOW_MAX_UI_STEPS = 14
FOLLOW_STEP_WAIT_S = 6.0
FOLLOW_TOTAL_S = 60.0
EXTRAS_REACH = 12
EXTRAS_CHARACTER_REACH = 24  # FIXB15 review: pets / horses / animals move; the mod publishes them over the nav window
_CHARACTER_KINDS = ("npc_gift", "pet", "horse_mount", "animal_pet")
EXTRAS_LADDER_UP_REACH = 40  # shared native offer reach for up/down ladders and shafts; press stays faced-only
# Owner destination words -> minecart response keys (Data/Minecarts Default network ids).
MINECART_DESTINATIONS = {"bus stop": "Bus", "halte": "Bus", "halte bus": "Bus", "mines": "Mines", "tambang": "Mines",
                         "town": "Town", "kota": "Town", "quarry": "Quarry", "tambang batu": "Quarry"}

RESULT_SCOPE = {
    "npc_gift": "give one gift (native counters and inventory; close dialogue)", "pet": "pet the farm pet (petted today)", "animal_pet": "pet the farm animal (wasPet)",
    "animal_milk": "milk the animal with the pail (product in inventory)", "animal_shear": "shear the animal (wool in inventory)",
    "horse_mount": "mount the horse", "horse_dismount": "dismount the horse", "shake_tree": "shake the tree (seed / fruit drops)",
    "harvest_crop": "harvest the ready crop (item in inventory)", "machine_collect": "collect the machine output",
    "machine_insert": "drop the held item into the machine", "hay_hopper": "take hay from the hopper",
    "ladder_down": "go down the ladder (next mine level)", "shaft": "open the shaft question (runtime answers Jump)",
    "mine_elevator": "open the elevator (runtime presses the floor)", "bus": "open the bus ticket question",
    "minecart": "open the minecart destinations", "place_item": "place the held item on the faced tile",
    "shop_counter": "open the shop (ShopMenu) at the counter",
    "kitchen": "open the cooking menu at your own kitchen",
    "quest_delivery": "hand the requested item to the quest's NPC (the native quest completes; never a gift)",
}


@dataclass
class ExtrasObjective:
    """One owner-authored WS-G6 objective inside the existing goal (never a model grant)."""
    kind: str
    target: str | None = None          # pet / animal word, destination word, item word (presentation only)
    floor: int | None = None           # elevator
    destination: str | None = None     # minecart response key / "Desert" for the bus
    count: int = 1
    done: int = 0
    effects: list = field(default_factory=list)
    opened: bool = False               # receiver / menu observed open (verified)
    answered: bool = False             # question answered by the runtime (verified ui_key)
    pressed: bool = False              # elevator floor pressed (verified ui_press)
    start_location: str | None = None
    arrived: str | None = None         # travel objectives: the verified new location
    blocker: str | None = None
    stop: str | None = None
    finish_recorded: bool = False
    item: str | None = None            # D-B4-8: the owner's named seed (presentation + seed choice only)
    routine: dict | None = None        # FIXB60 native completion summary (never an effect count)
    name: str | None = None            # FIXB15 D-B13-8: the owner's named pet (presentation only)


_NUM = {"one": 1, "a": 1, "an": 1, "satu": 1, "sebuah": 1, "seekor": 1}

_PATTERNS: tuple[tuple[str, str], ...] = (
    ("morning", r"skill cek_pagi"),
    ("animals", r"take care of all animals in the coop and barn"),
    ("gift", r"kasih hadiah (?P<item>[a-z][a-z0-9 '-]{0,39}) ke (?P<name>[a-z][a-z'-]{0,19}(?: [a-z][a-z'-]{0,19})?)"),
    # FIXB15 D-B13-8 (live V2B13G6pet-a: `elus kucingnya, Dudley` did not parse): an optional pet name after the kind.
    ("pet", r"(?:elus|usap|pet) (?:the |my )?(?P<t>kucing(?:nya)?|anjing(?:nya)?|cat|dog|pet|peliharaan(?:nya)?|hewan peliharaan)"
            r"(?:,? (?:si |named |bernama )?(?P<name>[a-z][a-z'-]{0,19}))?"),
    ("milk", r"(?:perah|milk) (?:the |a |one |seekor )?(?P<t>sapi|kambing|cow|goat)"),
    ("shear", r"(?:cukur|gunting bulu|shear) (?:the |a |one |seekor )?(?P<t>domba|sheep)"),
    ("animal_pet", r"(?:elus|usap|pet) (?:the |a |one |seekor )?(?P<t>sapi|ayam|kambing|domba|babi|bebek|kelinci|hewan(?: ternak)?|"
                   r"cow|chicken|goat|sheep|pig|duck|rabbit|animal|farm animal)"),
    ("mount", r"(?:naik|tunggangi|ride|mount|ride on) (?:the |my )?(?:kuda|horse)(?:nya)?"),
    ("dismount", r"(?:turun dari kuda|dismount(?: the horse)?|get off the horse)"),
    ("shake", r"(?:goyang|goyangkan|shake) (?:a |the |one |satu )?(?:pohon|tree|fruit tree|pohon buah)"),
    ("harvest", r"(?:panen|harvest)(?: (?:a |the |one |satu )?(?:tanaman|crop|hasil panen))?"),
    ("collect", r"(?:ambil hasil mesin|ambil hasil dari mesin|collect (?:from )?(?:the )?machine(?: output)?|collect the output)"),
    ("insert", r"(?:isi mesin|masukkan ke mesin|masukkan (?:bahan )?ke (?:dalam )?mesin|load the machine|put (?:it |one )?into the machine)"),
    ("hay", r"(?:ambil jerami|take hay|get hay)(?: (?:dari|from) (?:the )?(?:hopper|tempat jerami))?"),
    ("ladder", r"(?:turun tangga|turuni tangga|go down the ladder|take the ladder down|go down a level)"),
    ("shaft", r"(?:lompat ke lubang|turun lewat lubang|jump down the shaft|take the shaft)"),
    ("elevator", r"(?:naik lift|naik elevator|take the elevator|ride the elevator)(?: (?:ke|to) (?:lantai|floor|level) (?P<n>\d{1,3}))?"),
    ("bus", r"(?:naik bus|take the bus)(?: (?:ke|to) (?:the )?(?:gurun|desert))?"),
    ("minecart", r"(?:naik minecart|naik kereta tambang|take the minecart)(?: (?:ke|to) (?:the )?(?P<d>[a-z ]{3,16}))?"),
    ("place", r"(?:tanam|plant) (?:(?:a |one |satu |sebuah )?(?:(?:benih|bibit) (?P<seed>[a-z][a-z ]{2,23})|"
              r"(?P<seed2>[a-z][a-z ]{2,23}) seeds?|benih|bibit|seed|seeds))"),
    ("place", r"(?:pasang|taruh|place|put down) (?:(?:a |one |satu |sebuah )?(?:sprinkler|penyiram|chest|peti))"),
    # W1 gap B3 S24: the crab-pot leg; the mod admits it only on native-valid crab-pot water.
    ("place", r"(?:pasang|taruh|place|put down) (?:(?:a |one |satu |sebuah )?(?:crab pot|bubu))"),
    ("bundle", r"(?:isi|lengkapi|fill|donate to) (?:a |one |the |satu )?(?:bundle|bundel)(?: (?P<t>[a-z ]{3,24}))?"),
    ("museum", r"(?:donasi(?:kan)? ke museum|sumbang ke museum|donate (?:an item |something |one item )?to the museum)"),
)


def parse_extras_objective(text: str) -> ExtrasObjective | None:
    """Anchored owner grammar (authenticated ingress only; never model / scene text). Count is one."""
    t = re.sub(r"\s+", " ", (text or "").strip().rstrip(".!")).casefold()
    for kind, pattern in _PATTERNS:
        m = re.fullmatch(pattern, t)
        if not m:
            continue
        groups = m.groupdict()
        obj = ExtrasObjective(kind, target=(groups.get("t") or None))
        if kind == "morning":
            obj.count = 999  # S29's text + closed-menu verifier alone finishes the composition
        obj.name = groups.get("name") or None
        if kind == "gift":
            obj.item = groups["item"]
        if kind == "elevator":
            n = groups.get("n")
            if n is None:
                return None  # the floor is the owner's choice: never guessed
            floor = int(n)
            if floor % 5 != 0 or not 0 <= floor <= 120:
                return None
            obj.floor = floor
        if kind == "minecart":
            d = (groups.get("d") or "").strip()
            if d not in MINECART_DESTINATIONS:
                return None
            obj.destination = MINECART_DESTINATIONS[d]
        if kind == "bus":
            obj.destination = "Desert"
        if kind == "place" and re.search(r"benih|bibit|seed", t):
            obj.target = "seed"  # D-B3-5: extras_rule selects a seed (inventory category -74) before the X press
            named = (groups.get("seed") or groups.get("seed2") or "").strip()
            obj.item = named or None  # D-B4-8: the owner's named seed ("[recorded remark omitted]" / "[recorded remark omitted]")
        return obj
    return None


def objective_open(objective: ExtrasObjective | None) -> bool:
    return objective is not None and objective.blocker is None and objective.stop is None


def time_refusal(objective: ExtrasObjective | None, time_of_day: object) -> str | None:
    """Mirror of WorldExtras.TimeRefusal (mod refuses too): checked at pin and before every offer."""
    if objective is None or type(time_of_day) is not int:
        return None
    if time_of_day >= HARD_STOP_TIME:
        return "time_rule:hard_stop"
    if objective.kind in ("ladder", "shaft") and time_of_day >= MINES_LATEST_DESCEND_TIME:
        return "time_rule:mines_after_2000"
    if objective.kind == "elevator" and (objective.floor or 0) > 0 and time_of_day >= MINES_LATEST_DESCEND_TIME:
        return "time_rule:mines_after_2000"
    # Native WorldExtras.TimeRefusal gates petting, not milk/shear/hay or the Farm return.
    if objective.kind == "animal_pet" and time_of_day >= ANIMAL_SLEEP_TIME:
        return "time_rule:animal_sleeping"
    return None


def target_offered(target: Mapping | None, objective: ExtrasObjective | None) -> bool:
    """Presentation filter for the mod-observed extras target (the mod stays authoritative)."""
    kind = target.get("kind") if isinstance(target, Mapping) else None
    return (kind in KINDS and objective_open(objective) and kind in OBJ_KINDS.get(objective.kind, ())
            and (objective.kind != "gift" or unquote(str(target.get("source_fact_id", "")).rsplit(":", 1)[-1]).casefold() == objective.name)
            and objective.done < objective.count and not (objective.kind in RECEIVER_OBJECTIVES and objective.opened))


RECEIVER_OBJECTIVES = ("shaft", "bus", "minecart", "elevator", "exit_mines")


def extras_targets(ui_facts: Mapping | None) -> list[dict]:
    """The mod's published extras_targets list (validated; malformed entries dropped)."""
    raw = (ui_facts or {}).get("extras_targets")
    out = []
    for e in raw if isinstance(raw, (list, tuple)) else ():
        if (isinstance(e, Mapping) and e.get("kind") in KINDS and isinstance(e.get("ref"), str) and 0 < len(e["ref"]) <= 100
                and all(type(e.get(k)) is int and 0 <= e[k] <= command.TILE_MAX for k in ("x", "y"))):
            out.append(dict(e))
    return out


def _revision(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def approach_entries(ui_facts: Mapping | None, motor_tile: tuple[int, int], objective: ExtrasObjective | None,
                     unreachable: list | None = None, invalid_stands: set | frozenset = frozenset()) -> list[dict]:
    """Approach catalog entries for the objective's own targets (animals / machines / ladders / trees / crops / dirt).

    Stand on a cardinal neighbour and face the target (the mod's capture at the grab tile decides). Moving targets
    (pets, animals, horse) carry their current tile in the revision, so a moved target is rebuilt from fresh facts."""
    if not objective_open(objective) or objective.done >= objective.count:
        return []
    kinds = OBJ_KINDS.get(objective.kind, ())
    out = []
    for e in extras_targets(ui_facts):
        if e["kind"] not in kinds or e.get("ready") is False:
            continue
        if objective.kind == "gift" and str(e.get("label", "")).casefold() != objective.name:
            continue
        reach = (EXTRAS_CHARACTER_REACH if e["kind"] in _CHARACTER_KINDS
                 else EXTRAS_LADDER_UP_REACH if e["kind"] in ("ladder_up", "ladder_down", "shaft") else EXTRAS_REACH)
        if max(abs(e["x"] - motor_tile[0]), abs(e["y"] - motor_tile[1])) > reach:
            continue
        entry = dict(ref=e["ref"], tile=[e["x"], e["y"]], kind=e["kind"], label=str(e.get("label") or "")[:40],
                     revision=_revision(e["ref"], e["x"], e["y"], e["kind"]))
        stands = stand_tiles(e)
        if stands is not None and invalid_stands:
            # FIXB19 D-B16-1: a stand ARTI reached without the target being offered there is not a stand for this tile
            stands = [s for s in stands if (e["ref"], e["x"], e["y"], s[0], s[1]) not in invalid_stands]
        if stands is not None:
            if not stands:
                if unreachable is not None:
                    unreachable.append(dict(ref=e["ref"], tile=[e["x"], e["y"]], misses=stand_misses(e)))
                continue  # D-B7b-1: no walkable side: never approached (a moving target is re-read next publication)
            entry["reach_tiles"] = stands
            entry["revision"] = _revision(e["ref"], e["x"], e["y"], e["kind"], stands)
        out.append(entry)
    return out


def stand_tiles(entry: Mapping) -> list | None:
    """D-B7b-1: the mod's walkable cardinal stands for an A-kind target (``stands``), validated (cardinal neighbours of
    the target only); None = not published (older mod / X kinds: the default four neighbours stay)."""
    raw = entry.get("stands")
    if not isinstance(raw, (list, tuple)):
        return None
    out = []
    for s in raw:
        if (isinstance(s, (list, tuple)) and len(s) == 2 and all(type(v) is int for v in s)
                and abs(s[0] - entry["x"]) + abs(s[1] - entry["y"]) == 1 and [s[0], s[1]] not in out):
            out.append([s[0], s[1]])
    return out


NAV_GRID_MAX = 48
NAV_CLASSES = {".": "open", "s": "stone", "b": "clump", "w": "water", "#": "wall", "x": "warp", "z": "sleep_row"}


def nav_grid(ui_facts: Mapping | None, location: str | None = None) -> dict | None:
    """FIXB14: the mod's ui.context ``nav_grid`` (walkability window around ARTI) as {(x, y): cell}, cell in
    NAV_CLASSES ('.' open, 's' breakable stone, 'b' boulder / stump / log, 'w' water, '#' wall / building / tree / bush /
    other object, 'x' warp source, 'z' a bed's sleep row); None when absent, malformed or for another location."""
    raw = (ui_facts or {}).get("nav_grid") if isinstance(ui_facts, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    x0, y0, w, h, rows = (raw.get(k) for k in ("x0", "y0", "w", "h", "rows"))
    if (any(type(v) is not int for v in (x0, y0, w, h)) or x0 < 0 or y0 < 0 or not 0 < w <= NAV_GRID_MAX
            or not 0 < h <= NAV_GRID_MAX or not isinstance(rows, (list, tuple)) or len(rows) != h
            or any(not isinstance(r, str) or len(r) != w or set(r) - set(NAV_CLASSES) for r in rows)):
        return None
    if location is not None and raw.get("location") not in (None, location):
        return None
    return {(x0 + dx, y0 + dy): c for dy, r in enumerate(rows) for dx, c in enumerate(r)}


def sleep_exit_tiles(grid: Mapping | None, center) -> set:
    """FIXB16 review: the 4-connected 'z' (bed sleep row) cells around ``center`` when ARTI itself stands on one (it
    leaves the bed sideways along its row); empty otherwise. Every other sleep-row cell is never routed onto (a step
    there starts the Sleep question)."""
    grid = grid or {}
    start = tuple(center)
    if grid.get(start) != "z":
        return set()
    out, todo = {start}, [start]
    while todo:
        x, y = todo.pop()
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if n not in out and grid.get(n) == "z" and len(out) < 8:
                out.add(n)
                todo.append(n)
    return out


def nav_routing_sets(grid: Mapping | None, center, limit: int, *, stones_open: bool = False) -> tuple[set, set]:
    """FIXB14: (blocked, passable) tiles of a nav grid within ``limit`` (Chebyshev) of ``center``: open cells passable,
    breakable stones passable only with ``stones_open`` (a combat goal that can break them), everything else blocked;
    ``center`` (ARTI's own tile) is never blocked. FIXB16 review: a bed's sleep row ('z') is blocked, except the row
    ARTI starts on (sleep_exit_tiles: the way out of the bed)."""
    blocked, passable = set(), set()
    exit_row = sleep_exit_tiles(grid, center)
    for t, cell in (grid or {}).items():
        if max(abs(t[0] - center[0]), abs(t[1] - center[1])) > limit:
            continue
        (passable if cell == "." or (cell == "s" and stones_open) or t in exit_row else blocked).add(t)
    blocked.discard(tuple(center))
    return blocked, passable


# FIXB15 D-B12-6: objectives whose targets live outdoors on the Farm (pets, the horse, trees, crops, planting).
FARM_OUTDOOR_OBJECTIVES = ("pet", "mount", "shake", "harvest", "place")


def farm_interior(location: str | None) -> bool:
    """The farmhouse / a cabin (the locations a farm-outdoor objective is given from indoors)."""
    # live ids: "Cabin" (motor) / "FarmHouse1a2b3c~0123456789abcdef" (the cabin's scene token)
    return isinstance(location, str) and location.startswith(("FarmHouse", "Cabin"))


def offscene_destination(objective: ExtrasObjective | None, location: str | None) -> str | None:
    """FIXB15 D-B12-6 (live V2B12G6pet-b: in the Cabin (9,8), Dudley on the Farm (53,8); no exit was offered, the model
    inspected 12 times): the location an open farm-outdoor objective's target is in when ARTI is inside the farmhouse /
    a cabin -> "Farm"; else None (the target is here, or unknown)."""
    if objective_open(objective) and objective.kind in FARM_OUTDOOR_OBJECTIVES and farm_interior(location):
        return "Farm"
    return None


def extras_blockers(ui_facts: Mapping | None) -> list[dict]:
    """FIXB15 D-B12-5: the mod's ``extras_blockers`` (e.g. an unnamed horse: [{kind, blocker, x, y, stable_owner}])."""
    raw = (ui_facts or {}).get("extras_blockers") if isinstance(ui_facts, Mapping) else None
    out = []
    for b in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(b, Mapping) and b.get("kind") in KINDS and isinstance(b.get("blocker"), str):
            out.append({"kind": b["kind"], "blocker": b["blocker"][:40],
                        "stable_owner": str(b.get("stable_owner") or "")[:40] or None})
    return out[:4]


def stand_misses(entry: Mapping) -> list:
    """FIXB13 D-B10-3: the mod's per-side refusal reasons (``stand_misses`` [[x, y, reason]]), bounded, for the journal."""
    raw = entry.get("stand_misses")
    out = []
    for m in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(m, (list, tuple)) and len(m) == 3 and type(m[0]) is int and type(m[1]) is int:
            out.append([m[0], m[1], str(m[2])[:24]])
    return out[:4]


SEED_CATEGORY = -74
# D-B4-8 (live V2B4G6plant-a picked slot 7 = Pine Cone): tree seeds share category -74 with crop seeds; saplings too.
TREE_SEEDS = frozenset({"(O)309", "(O)310", "(O)311", "(O)292", "(O)891", "(O)MossySeed", "(O)MysticTreeSeed"})


def rank_seed_slots(objective: ExtrasObjective | None, seeds: list, *, season=None) -> tuple:
    """Slots to select for "plant a seed", best tier only: the owner's named seed when given (display name match); else
    crop seeds (not a tree seed, not a sapling); else (no crop seed owned) tree seeds. ``seeds`` = [{slot, qid, name}]."""
    def norm(text):
        return re.sub(r"[^a-z ]", "", str(text or "").casefold()).strip()
    entries = [s for s in seeds if isinstance(s, Mapping) and type(s.get("slot")) is int]
    from arti_stardew_seeds import seed_in_season
    entries = [s for s in entries if seed_in_season(s.get("qid"), season) is not False]
    named = norm(getattr(objective, "item", None))
    if named:
        hits = [s["slot"] for s in entries if named in norm(s.get("name"))]
        return tuple(hits)  # a named seed that is not owned selects nothing (never another seed)
    crop = [s["slot"] for s in entries
            if s.get("qid") not in TREE_SEEDS and not norm(s.get("name")).endswith("sapling")]
    # Do not replace owned out-of-season crops with a tree seed for an unnamed crop goal.
    owned_crop = any(s.get("qid") not in TREE_SEEDS and not norm(s.get("name")).endswith("sapling")
                     for s in seeds if isinstance(s, Mapping))
    return tuple(crop) if crop or owned_crop else tuple(s["slot"] for s in entries)
RULE_EXTRAS = "extras_rule"


def auto_choice(objective: ExtrasObjective | None, snap, *, approach_progress: Mapping | None,
                seed_slots: tuple = ()) -> tuple | None:
    """D-B3-5 extras_rule (live V2B3G6shake-a / plant-a: approach entries offered, the model swung the axe or inspected):
    (operation, target_ref, args, label) for the objective's own offered step, or None. Order: world_interact of the
    faced target of the objective's kind; for "plant a seed" with no seed in hand, select_item of an owned seed slot
    (inventory category -74); else approach_target of an offered entry of the objective's kind not already ARRIVED at.
    Only steps the snapshot already offers; the same admission gates run on the result."""
    if not objective_open(objective) or objective.done >= objective.count:
        return None
    target = getattr(snap, "world_interaction_target", None)
    if objective.kind == "morning":
        return None  # S29 alone orders TV channels/mail; generic auto-acts must never reopen the TV
    if objective.kind == "gift":
        held = getattr(snap, "held_item", None) or {}
        if str(held.get("display_name") or held.get("name") or "").casefold() != objective.item:
            for selected in getattr(snap, "selectable_items", ()) or ():
                if str(selected.get("display_name") or selected.get("name") or "").casefold() == objective.item:
                    return "select_item", None, {k: v for k, v in selected.items() if k not in ("name", "display_name")}, "gift_item"
            return None
    if isinstance(target, Mapping) and target_offered(target, objective):
        return "world_interact", None, {}, str(target.get("kind"))
    held = getattr(snap, "held_item", None) or {}
    # FIXB12 D-B9-1 (live V2B9PLANT-b: `select seed_slot:0` twice on the already-active seeds, never placed): a seed in
    # hand (its slot is a seed slot, or the held item is a seed by category) needs no select: approach / place next.
    from arti_stardew_seeds import SEASONS
    season_known = getattr(snap, "season", None) in SEASONS
    seed_in_hand = held.get("slot") in seed_slots or (
        not season_known and held.get("item_category") == SEED_CATEGORY)
    if objective.kind == "place" and objective.target == "seed" and season_known and not seed_slots:
        return None
    if (objective.kind == "place" and objective.target == "seed" and not seed_in_hand
            and getattr(snap, "select_item_available", False)):
        offered = {item.get("slot"): item for item in getattr(snap, "selectable_items", ()) or () if isinstance(item, Mapping)}
        for slot in seed_slots:  # preference order (rank_seed_slots)
            if slot in offered:
                return "select_item", None, dict(offered[slot]), "seed_slot:%d" % slot
    kinds = OBJ_KINDS.get(objective.kind, ())
    progress = approach_progress or {}
    for entry in getattr(snap, "approach_targets", ()) or ():
        if not isinstance(entry, Mapping) or entry.get("kind") not in kinds:
            continue
        if progress.get("status") == "ARRIVED" and progress.get("target_ref") == entry.get("ref"):
            # FIXB41: an arrival belongs to a pose at one observed pet tile, not to its permanent identity.
            # A moved pet needs a new short leg; an unchanged arrival waits for the native grab-tile offer.
            if entry.get("kind") not in ("pet", "npc_gift") or progress.get("target_tile") == entry.get("tile"):
                continue
        return "approach_target", str(entry["ref"]), {}, str(entry["ref"])
    return None


def objective_target_tiles(objective: ExtrasObjective | None, approach_targets, world_target, ui_facts) -> set:
    """FIXB17 D-B14c-1: the tiles of the open objective's own targets (offered approach entries, the faced world target,
    the mod's published extras_targets) of the objective's kinds."""
    if not objective_open(objective):
        return set()
    kinds = OBJ_KINDS.get(objective.kind, ())
    out: set = set()
    for e in approach_targets or ():
        tile = e.get("tile") if isinstance(e, Mapping) and e.get("kind") in kinds else None
        if isinstance(tile, (list, tuple)) and len(tile) == 2 and all(type(v) is int for v in tile):
            out.add((tile[0], tile[1]))
    if (isinstance(world_target, Mapping) and world_target.get("kind") in kinds
            and type(world_target.get("tile_x")) is int and type(world_target.get("tile_y")) is int):
        out.add((world_target["tile_x"], world_target["tile_y"]))
    raw = ui_facts.get("extras_targets") if isinstance(ui_facts, Mapping) else None
    for e in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(e, Mapping) and e.get("kind") in kinds and type(e.get("x")) is int and type(e.get("y")) is int:
            out.add((e["x"], e["y"]))
    return out


def rank_override(entry: Mapping, objective: ExtrasObjective | None) -> int | None:
    if objective_open(objective) and objective.kind == "animals" and entry.get("kind") == "forage":
        return 0  # FIXB67: floor products are this routine's own tasks, like pet/hay.
    if objective_open(objective) and entry.get("kind") in OBJ_KINDS.get(objective.kind, ()):
        return 0
    return None


def parse_effect_detail(detail: object) -> dict | None:
    """Mod BgExtrasEffect.Detail: "kind|item|delta|value" (None on any doubt)."""
    if not isinstance(detail, str) or detail.count("|") != 3 or len(detail) > 200:
        return None
    kind, item, delta, value = detail.split("|")
    if kind not in KINDS or not re.fullmatch(r"-?\d{1,6}", delta) or not re.fullmatch(r"-?\d{1,9}", value):
        return None
    return {"kind": kind, "item": item, "delta": int(delta), "value": int(value)}


def verified_effect(kind: str, envelope: Mapping | None) -> dict | None:
    """The mod's verified success for one extras press (status completed, success, the kind's outcome, cleanup)."""
    if not isinstance(envelope, Mapping) or kind not in KINDS:
        return None
    args = envelope.get("input_args")
    if (envelope.get("status") != "completed" or envelope.get("success") is not True
            or envelope.get("outcome") not in (OUTCOMES[kind], command.OUTCOME_EVENT_UP.get(OUTCOMES[kind]))
            or envelope.get("cleanup_confirmed") is not True or not isinstance(args, Mapping) or args.get("kind") != kind):
        return None
    if kind in ("npc_gift", "quest_delivery") and (envelope.get("outcome") != OUTCOMES[kind]
                               or type(envelope.get("press_count")) is not int or envelope["press_count"] != 1
                               or not str(envelope.get("result_receiver_token") or "").startswith("rcv:")
                               or type(envelope.get("result_context_generation")) is not int
                               or envelope["result_context_generation"] < 1):
        return None
    if kind in RECEIVER_KINDS:
        receiver = envelope.get("result_receiver_token")
        if not isinstance(receiver, str) or not receiver.startswith("rcv:") or envelope.get("effect_detail") != kind:
            return None
        return {"kind": kind, "receiver_token": receiver, "generation": envelope.get("result_context_generation")}
    if kind == "ladder_down":
        location = envelope.get("result_location_id")
        return {"kind": kind, "location": location} if isinstance(location, str) and location else None
    detail = parse_effect_detail(envelope.get("effect_detail"))
    if detail is None or detail["kind"] != kind:
        return None
    if kind == "hay_place" and (detail["item"] != "(O)178" or detail["delta"] != -1):
        return None
    if kind == "npc_gift":
        return dict(detail, receiver_token=envelope["result_receiver_token"], generation=envelope["result_context_generation"]) if detail["delta"] == -1 and detail["value"] == 1 and detail["item"].startswith("(O)") else None
    if kind == "quest_delivery":
        # W1 gap B4: value = the pinned quest's native state (1 completed in the log, 2 terminally removed); delta = -number.
        return (dict(detail, receiver_token=envelope["result_receiver_token"], generation=envelope["result_context_generation"])
                if detail["delta"] <= -1 and (detail["value"] == 1 or detail["value"] == 2) and detail["item"].startswith("(") else None)
    sign = {"animal_milk": 1, "animal_shear": 1, "harvest_crop": 1, "machine_collect": 1, "hay_hopper": 1,
            "machine_insert": -1, "place_item": -1, "hay_place": -1}.get(kind, 0)
    if (sign == 1 and detail["delta"] < 1) or (sign == -1 and detail["delta"] > -1) or (sign == 0 and kind != "shake_tree"
                                                                                       and detail["delta"] != 0):
        return None
    return detail


def record_effect(objective: ExtrasObjective | None, effect: Mapping | None) -> dict | None:
    """Records one verified effect. Returns the objective_progress journal body (None = nothing recorded)."""
    if objective is None or effect is None or effect.get("kind") not in OBJ_KINDS.get(objective.kind, ()):
        return None
    if objective.kind == "animals":
        objective.effects.append(dict(effect))
        del objective.effects[:-64]
        return {"kind": "animals", "effect": dict(effect), "completion": "requires_fresh_native_state"}
    if effect["kind"] in RECEIVER_KINDS:
        objective.opened = True
    elif effect["kind"] == "ladder_down":
        objective.arrived = effect.get("location")
        objective.done += 1
    else:
        objective.done += 1
    objective.effects.append(dict(effect))
    del objective.effects[:-4]
    return {"kind": objective.kind, "effect": dict(effect), "done": objective.done, "count": objective.count,
            "opened": objective.opened}


def record_arrival(objective: ExtrasObjective | None, before: str | None, after: str | None) -> dict | None:
    """Travel objectives (shaft / elevator / bus / minecart): the verified location change after the menu step."""
    if objective is None or objective.kind not in ("shaft", "elevator", "bus", "minecart", "exit_mines") or not after \
            or after == before:
        return None
    if objective.kind != "elevator" and not objective.answered:
        return None
    if objective.kind == "elevator" and not objective.pressed:
        return None
    objective.arrived = after
    objective.done = max(objective.done, 1)
    return {"kind": objective.kind, "from": before, "to": after}


def note_ui_press(objective: ExtrasObjective | None, *, context: str | None, snapped_kind: str | None, held: bool,
                  btn: str | None, effect: str | None, verified: bool) -> dict | None:
    """A VERIFIED G1 ui_press in a G6 menu: bundle deposit / museum place / elevator floor."""
    if objective is None or not verified or btn != "A":
        return None
    if objective.kind == "bundle" and context == "junimo_note":
        if snapped_kind == "ingredient" and held and effect == "changed":
            objective.done += 1
            return {"kind": "bundle", "deposit": True, "done": objective.done}
        if context == "junimo_note":
            objective.opened = True
    if objective.kind == "museum" and context == "museum" and held and effect == "changed":
        objective.done += 1
        return {"kind": "museum", "donated": True, "done": objective.done}
    if objective.kind == "elevator" and context == "mine_elevator" and snapped_kind == "floor" and effect == "closed":
        objective.pressed = True
        return {"kind": "elevator", "pressed": True}
    return None


def note_menu_view(objective: ExtrasObjective | None, context: str | None) -> bool:
    """The objective's own menu observed open (presentation progress only, never a finish by itself)."""
    if objective is None or objective.kind not in MENU_OBJECTIVES or objective.opened:
        return False
    if context == MENU_OBJECTIVES[objective.kind]:
        objective.opened = True
        return True
    return False


@dataclass(frozen=True)
class FollowStep:
    kind: str           # act | wait | done | stop
    reason: str
    args: dict | None = None


def plan_question_answer(objective: ExtrasObjective, view) -> FollowStep:
    """Deterministic answer of the question the shaft / bus / minecart press opened (view = UiKeyView)."""
    key = {"shaft": "Shaft", "bus": "Bus", "minecart": "pagedResponse", "exit_mines": "ExitMine"}.get(objective.kind)
    if key is None:
        return FollowStep("stop", "not_a_question_objective")
    if objective.answered:
        return FollowStep("done", "answered")
    if view is None or view.kind != "question":
        return FollowStep("wait", "ui_key_unavailable")
    if view.question_key != key:
        return FollowStep("stop", "unexpected_question:" + str(view.question_key))
    if not view.available:
        return FollowStep("wait", view.not_ready_reason or "not_ready")
    want = {"shaft": "Jump", "bus": "Yes", "exit_mines": "Leave"}.get(objective.kind, objective.destination)
    matches = [o["index"] for o in view.options if o.get("response_key") == want]
    if len(matches) != 1:
        return FollowStep("stop", "response_missing:" + str(want))
    return FollowStep("act", "answer:" + str(want), view.args_for("answer", option_index=matches[0]))


def plan_elevator_step(objective: ExtrasObjective, view) -> FollowStep:
    """Next ui_nav / ui_press toward the owner's floor in MineElevatorMenu (view = arti_stardew_ui_pad.UiPadView)."""
    if objective.pressed:
        return FollowStep("done", "floor_pressed")
    if view is None or view.context != "mine_elevator":
        return FollowStep("wait", "elevator_menu_unavailable")
    if not view.available:
        return FollowStep("wait", view.wait_reason or "not_ready")
    goal = next((c for c in view.components if c.get("kind") == "floor" and c.get("label") == str(objective.floor)), None)
    if goal is None:
        return FollowStep("stop", "floor_not_offered:" + str(objective.floor))
    if view.snapped_id == goal["id"]:
        if not view.press_allowed("A"):
            return FollowStep("stop", "floor_press_refused")
        return FollowStep("act", "press_floor", view.press_args("A"))
    if view.snapped_id < 0:
        return FollowStep("act", "nav_first_snap", view.nav_args("right"))
    direction = nav_direction(view.components, view.snapped_id, goal["id"])
    if direction is None or not view.nav_allowed:
        return FollowStep("stop", "no_route_to_floor")
    return FollowStep("act", "nav_" + direction, view.nav_args(direction))


def nav_direction(components: tuple | list, start: int, goal: int) -> str | None:
    """First step of the shortest neighbour path start -> goal over the published nbr ids (up, right, down, left)."""
    by_id = {c.get("id"): c for c in components if isinstance(c, Mapping)}
    names = ("up", "right", "down", "left")
    frontier = [(start, None)]
    seen = {start}
    while frontier:
        nxt = []
        for node, first in frontier:
            comp = by_id.get(node)
            nbr = comp.get("nbr") if comp else None
            for i, n in enumerate(nbr if isinstance(nbr, (list, tuple)) and len(nbr) == 4 else ()):
                if n in seen or n not in by_id:
                    continue
                step = first or names[i]
                if n == goal:
                    return step
                seen.add(n)
                nxt.append((n, step))
        frontier = nxt
        if len(seen) > 200:
            break
    return None


def objective_verdict(objective: ExtrasObjective) -> tuple[str, str]:
    """(status, reason) for finish: achieved only from verified effects recorded by the runtime."""
    if objective.kind == "animals":
        from arti_stardew_animal_routine import verdict
        return verdict(objective.routine, objective.blocker or objective.stop)
    if objective.kind == "gift" and objective.stop and objective.done < objective.count:
        # PART_B3: a refusal read after the recorded gift effect cannot undo it.
        return "partial", objective.stop
    if objective.kind in ("shaft", "elevator", "bus", "minecart", "exit_mines"):
        if objective.arrived:
            return "achieved", FINISH
    elif objective.done >= objective.count:
        return "achieved", FINISH
    if objective.blocker or objective.stop:
        return "partial", objective.blocker or objective.stop
    if objective.kind in QUESTION_OBJECTIVES:
        return "partial", ("arrival_not_observed" if objective.answered else "question_not_answered"
                           if objective.opened else "question_not_opened")
    if objective.kind == "elevator":
        return "partial", ("arrival_not_observed" if objective.pressed else "floor_not_pressed"
                           if objective.opened else "elevator_not_opened")
    if objective.kind in ("bundle", "museum"):
        return "partial", "menu_effect_not_observed" if objective.opened else "menu_not_opened"
    return "partial", "effect_not_observed"


def objective_summary(objective: ExtrasObjective | None) -> dict | None:
    """Bounded model-visible view (same shape as the other routine summaries: kind / status / reason)."""
    if objective is None:
        return None
    status, reason = objective_verdict(objective)
    if objective.kind == "animals":
        return dict(objective.routine or {}, kind="extras_animals", status=status, reason=reason)
    summary = {"kind": "extras_" + objective.kind, "status": status, "reason": reason, "done": objective.done,
               "count": objective.count}
    if objective.floor is not None:
        summary["floor"] = objective.floor
    if objective.kind in MENU_OBJECTIVES:
        summary["menu"] = MENU_OBJECTIVES[objective.kind]
        summary["next"] = ("open the Junimo note (GameMenu inventory icon) then pick an item and put it in a bundle slot"
                           if objective.kind == "bundle" else "pick one donatable item, then press A to place it"
                           if objective.kind == "museum" else "the runtime presses the floor")
    return summary


MODEL_GUIDANCE = (" Extras: world_interaction_target kinds pet / animal_* / horse_* / shake_tree / harvest_crop / machine_* /"
                  " hay_hopper / ladder_down / shaft / mine_elevator / bus / minecart / place_item are offered only for the"
                  " owner's extras objective; approach_targets name where to stand. X kinds (milk, shear, machine drop-in,"
                  " place) need the right item selected first.")
