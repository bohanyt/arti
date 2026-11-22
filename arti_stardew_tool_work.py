"""WS1 (primitives v1) tool work: one owner-named farm-tool effect, pure logic only (no IO).

Five bounded objectives: break one rock (pickaxe), till one tile (hoe), water one tile (watering
can), refill the can, cut one weed (scythe). The runtime offers ``world_key.use_tool`` only while
the EXACT tool hit tile (mod gather-window ``tool_tile``) is the adjacent faced tile and holds a
target this tool may hit; success is an effect observed on a fresh, settled, newer capture of
the pinned target. Request ids, stamina, animation or input completion are never success.

Grammar is parsed only at authenticated owner ingress (never model/scene text). Evidence
ceiling: UNIT_TESTED; every game-behaviour assumption is HYPOTHESIS until live.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

# kind -> (required equipped qualified item id, max physical work steps)
KINDS: dict[str, tuple[str, int]] = {
    "break_rock": ("(T)Pickaxe", 6),
    "till_tile": ("(T)Hoe", 2),
    "water_tile": ("(T)WateringCan", 2),
    "refill_can": ("(T)WateringCan", 2),
    "cut_weed": ("(W)47", 3),
    "cut_grass": ("(W)47", 6),  # FIXB18: a Grass tuft takes up to 4 scythe hits (Grass.numberOfWeeds - 1 per hit)
    "cut_twig": ("(T)Axe", 1),
    # W1 gap S15b (S15-route HELD route_blocked_unsupported:tree): an immature wild tree (growth stage 3-4, the
    # native "bush" branch of Tree.performToolAction) loses 2..10 health per axe hit from 10 and is then removed
    # (performBushDestroy). No per-hit health is observed, so hits before removal settle as whiffs.
    "cut_small_tree": ("(T)Axe", 7),
}
SCYTHE_QIDS = frozenset({"(W)47"})
# Review fix (WS1 minor 9): a pickaxe never charges (Tool.beginUsing ends its use on the press), so
# every upgrade breaks a rock with one press; the mod admits any Pickaxe upgrade (BgToolClass).
PICKAXE_QIDS = frozenset({"(T)Pickaxe", "(T)CopperPickaxe", "(T)SteelPickaxe", "(T)GoldPickaxe", "(T)IridiumPickaxe"})
AXE_QIDS = frozenset({"(T)Axe", "(T)CopperAxe", "(T)SteelAxe", "(T)GoldAxe", "(T)IridiumAxe"})
KIND_TOOL_QIDS: dict[str, frozenset] = {"break_rock": PICKAXE_QIDS, "cut_twig": AXE_QIDS, "cut_small_tree": AXE_QIDS}
# Tools that are offered ONLY under a matching tool-work objective (never blindly).
TOOL_WORK_ONLY_QIDS = frozenset({"(T)Hoe", "(T)WateringCan", "(W)47"})
SCYTHE_KINDS = ("cut_weed", "cut_grass")
MAX_TICKS = 4
MAX_WHIFFS = 2
MAX_WHIFFS_GRASS = 5  # FIXB18 D-B15a-1: a Grass tuft survives until its last weed is cut (no per-hit count is observed)
MAX_WHIFFS_SMALL_TREE = 6  # basic axe: 10 health / 2 per hit = 5 hits, the last one removes it (+1 margin)
SMALL_TREE_STAGE_MIN = 3  # Tree.performToolAction: stage >= 3 takes axe hits, >= 5 is a mature tree (S6)
MATURE_TREE_STAGE = 5
# Game ticks after the step's publication before a refill without an observed water change counts
# as a whiff: the water level rides the separate 10 Hz inventory domain, the effect rides the scene.
REFILL_SETTLE_TICKS = 60

ACTIVE = frozenset({"WORK_REMAINING", "NEEDS_REFILL"})
TERMINAL = frozenset({"EFFECT_OBSERVED", "UNAVAILABLE", "UNCERTAIN", "BUDGET_EXHAUSTED"})
_FACING = ((0, -1), (1, 0), (0, 1), (-1, 0))

# FIXB17 (batch 14c: `pecah 3 batu` did not parse): break_rock takes a count 1..TOOL_WORK_COUNT_MAX; every other kind
# stays one effect.
TOOL_WORK_COUNT_MAX = 9
_COUNT_WORDS = {"satu": 1, "one": 1, "dua": 2, "two": 2, "tiga": 3, "three": 3, "empat": 4, "four": 4, "lima": 5,
                "five": 5, "enam": 6, "six": 6, "tujuh": 7, "seven": 7, "delapan": 8, "eight": 8, "sembilan": 9, "nine": 9}
_N = r"(?:[1-9]|" + "|".join(_COUNT_WORDS) + r")"
_GRAMMAR = (
    (r"(?:pecah|break) " + _N + r" (?:buah )?(?:batu|rocks?|stones?)", "break_rock"),
    (r"(?:cangkul|till) (?:1|satu|one) (?:petak|tile)", "till_tile"),
    (r"(?:siram|water) (?:1|satu|one) (?:petak|tile)", "water_tile"),
    (r"(?:isi(?: ulang)? (?:penyiram|gembor)|refill (?:the )?watering can)", "refill_can"),
    (r"(?:potong|cut) (?:1|satu|one) (?:rumput liar|weed)", "cut_weed"),
    (r"(?:potong|cut) (?:1|satu|one )?twigs?", "cut_twig"),
    (r"(?:potong|cut) (?:1|satu|one) (?:pohon kecil|small tree)", "cut_small_tree"),
    # FIXB18 review: a Grass tuft only on an explicit grass word (`potong rumput` / `cut grass`); `rumput liar` = Weeds
    (r"(?:potong|cut|babat) (?:(?:1|satu|one) )?(?:rumput|grass)", "cut_grass"),
)


@dataclass(frozen=True)
class ToolWorkProgress:
    """Immutable view of one tool-work objective (like GatherProgress)."""

    goal_id: str
    session_id: str
    kind: str
    tool_qid: str
    target_ref: str | None = None      # fact ref, or "tile:<location>:<x>,<y>" for till/refill
    target_tile: tuple | None = None
    baseline: dict | None = None       # e.g. {"minutes_until_ready": 2} / {"phase": "refill", "water_left": 0}
    status: str = "WORK_REMAINING"
    reason: str = "tool_effect_required"
    settled_steps: int = 0
    remaining_steps: int = 0
    whiffs: int = 0
    last_step: str | None = None       # effect | progress | refilled | whiff | gone | uncertain
    effect: dict | None = None         # set once: {"kind", "reason", "request_id", "captured_ticks", ...}
    causal_credit: str = "unattributed"
    crop_required: bool = False  # Live7 crop chores; explicit primitive/Part A/B/C contracts stay unchanged


def parse_tool_work_goal(text: object) -> str | None:
    """Anchored owner grammar (ID/EN); break_rock with a count 1..9 (parse_tool_work_count), the others one; anything
    else is None."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    for pattern, kind in _GRAMMAR:
        if re.fullmatch(pattern.replace(" ", r"\s+") + r"[.!]?", stripped, re.IGNORECASE):
            return kind
    return None


def parse_tool_work_count(text: object) -> int:
    """FIXB17: the owner's count of a parsed tool-work goal (1 unless `pecah N batu` with N in 1..9)."""
    if parse_tool_work_goal(text) != "break_rock":
        return 1
    word = str(text).strip().split()[1].casefold()
    count = int(word) if word.isdigit() else _COUNT_WORDS.get(word, 1)
    return count if 1 <= count <= TOOL_WORK_COUNT_MAX else 1


def faced_tile(args: Mapping) -> tuple[int, int] | None:
    """tile + facing delta from validated use_tool args."""
    try:
        facing = args["facing"]
        x, y = args["tile_x"], args["tile_y"]
        if type(facing) is not int or type(x) is not int or type(y) is not int or not 0 <= facing <= 3:
            return None
        dx, dy = _FACING[facing]
        return x + dx, y + dy
    except (KeyError, TypeError):
        return None


def _xy(value: object) -> tuple[int, int] | None:
    if (isinstance(value, Mapping) and type(value.get("x")) is int and type(value.get("y")) is int):
        return value["x"], value["y"]
    return None


def tool_tile_reason(args: Mapping, window: Mapping | None) -> str | None:
    """None when the mod's exact hit tile equals the adjacent faced tile."""
    if not isinstance(window, Mapping):
        return "tool_tile_unobserved"
    hit = _xy(window.get("tool_tile"))
    if hit is None:
        return "tool_tile_unobserved"   # old mod without the key: no target for tool work
    return None if hit == faced_tile(args) else "tool_tile_not_adjacent"


def tool_tile(args: Mapping, window: Mapping | None) -> tuple[int, int] | None:
    return faced_tile(args) if tool_tile_reason(args, window) is None else None


def tile_ref(location_id: str, x: int, y: int) -> str:
    return f"tile:{location_id}:{x},{y}"


def _tile_entry(window: Mapping, x: int, y: int) -> Mapping | None:
    for entry in window.get("tiles") or ():
        if isinstance(entry, Mapping) and entry.get("x") == x and entry.get("y") == y:
            return entry
    return None


def _facts_on(window: Mapping, x: int, y: int) -> list:
    return [(ref, fact) for ref, fact in (window.get("targets") or {}).items()
            if isinstance(fact, Mapping) and _xy(fact.get("tile")) == (x, y)]


def _crop_watered(fact: Mapping) -> object:
    crop = fact.get("crop")
    return crop.get("watered") if isinstance(crop, Mapping) else None


def tool_matches(progress: ToolWorkProgress, qid: object) -> bool:
    """The held qid may do this kind's work (break_rock: any pickaxe upgrade; else the exact basic tool)."""
    return qid in KIND_TOOL_QIDS.get(progress.kind, frozenset({progress.tool_qid}))


def scythe_area_tiles(x: int, y: int, facing: int) -> list[tuple[int, int]]:
    """Mirror of BgToolTileGuard.ScytheAreaTiles: the player's tile and its two side tiles, plus the faced
    tile and its two perpendicular neighbours (the scythe sweeps every border tile of its area)."""
    if type(facing) is not int or not 0 <= facing <= 3:
        return []
    dx, dy = _FACING[facing]
    px, py = abs(dy), abs(dx)
    tiles = []
    for k in (0, 1):
        bx, by = x + k * dx, y + k * dy
        tiles += [(bx, by), (bx - px, by - py), (bx + px, by + py)]
    return tiles


def is_small_wild_tree(fact: object) -> bool:
    """An untapped, non-stump WILD tree (never a FruitTree: terrain_type "FruitTree", no wild_tree block) at growth
    stage 3-4, which the axe removes natively; saplings below 3 and mature trees (S6 tebang) are other paths."""
    if not isinstance(fact, Mapping) or fact.get("kind") != "terrain" or fact.get("terrain_type") != "Tree":
        return False
    tree = fact.get("wild_tree")
    return (isinstance(tree, Mapping) and tree.get("tapped") is False and tree.get("stump") is not True
            and type(tree.get("growth_stage")) is int
            and SMALL_TREE_STAGE_MIN <= tree["growth_stage"] < MATURE_TREE_STAGE)


def _has_crop(fact: Mapping) -> bool:
    crop = fact.get("crop")
    return isinstance(crop, Mapping) and (crop.get("phase") is not None or crop.get("qualified_item_id") is not None)


def live_crop(fact: Mapping) -> bool:
    """Native HoeDirt also emits a crop block for bare soil (null phase/id/dead)."""
    return (fact.get("kind") == "terrain" and fact.get("terrain_type") == "HoeDirt"
            and _has_crop(fact) and fact["crop"].get("dead") is False)


def crop_needs_water(fact: Mapping) -> bool:
    return (live_crop(fact) and fact["crop"].get("watered") is False
            and fact["crop"].get("ready_for_harvest") is False)


WET_SOIL_MAX = 1024  # remembered wet tiles (one game date); bounded, never a crop list


def wet_soil_view(memory: dict, location: object, date: object, scene: Mapping | None,
                  window: Mapping | None) -> dict:
    """Live7 M2 (live7-game-m2 day 2, 3 x siram no_effect_progress): the global scene facts are a
    location snapshot plus TerrainFeatureListChanged add/remove deltas (ModEntry.OnTerrainFeatureListChanged).
    Watering flips HoeDirt.state in place, which is no list change, so outside the fresh local tool window
    a watered tile kept ``crop.watered false`` until the next location snapshot; the offer and approach
    catalog then sent ARTI back to wet tiles. Native state only: a tile the fresh window showed wet today
    stays wet in the merged view for this location and game date (vanilla soil only dries overnight).
    ``memory`` maps (location, x, y) -> date and is pruned to ``date``. Returns ``{**scene, **window}``."""
    merged = dict(scene or {})
    window = window or {}
    if date is None or not location:
        merged.update(window)
        return merged
    for k in [k for k, d in list(memory.items()) if d != date]:
        memory.pop(k, None)
    def soil(fact):
        if not (isinstance(fact, Mapping) and fact.get("kind") == "terrain" and fact.get("terrain_type") == "HoeDirt"
                and isinstance(fact.get("crop"), Mapping)):
            return None
        xy = _xy(fact.get("tile"))
        return None if xy is None else (location, *xy)
    for fact in window.values():
        k = soil(fact)
        if k is None:
            continue
        if fact["crop"].get("watered") is True:
            memory.pop(k, None)
            memory[k] = date
        elif fact["crop"].get("watered") is False:
            memory.pop(k, None)
    while len(memory) > WET_SOIL_MAX:
        memory.pop(next(iter(memory)), None)
    for ref, fact in list(merged.items()):
        k = soil(fact)
        if k is not None and fact["crop"].get("watered") is False and memory.get(k) == date:
            merged[ref] = dict(fact, crop=dict(fact["crop"], watered=True))
    merged.update(window)
    return merged


def scythe_area_safe(args: Mapping, window: Mapping) -> bool:
    """No sapling (wild tree growth stage < 5: Tree.performSproutDestroy) and no crop (scythe harvest /
    dead-crop removal) anywhere in the sweep; every sweep tile must be inside the observed window."""
    center = _xy(window.get("center"))
    tiles = scythe_area_tiles(args.get("tile_x"), args.get("tile_y"), args.get("facing"))
    if not tiles or center is None or any(abs(x - center[0]) > 1 or abs(y - center[1]) > 1 for x, y in tiles):
        return False
    for x, y in tiles:
        for _, fact in _facts_on(window, x, y):
            if fact.get("kind") != "terrain":
                continue
            tree = fact.get("wild_tree")
            if isinstance(tree, Mapping) and not (type(tree.get("growth_stage")) is int and tree["growth_stage"] >= 5):
                return False
            if fact.get("terrain_type") == "HoeDirt" and _has_crop(fact):
                return False
    return True


def refill_phase(progress: ToolWorkProgress) -> bool:
    return progress.kind == "refill_can" or progress.status == "NEEDS_REFILL" or (
        isinstance(progress.baseline, Mapping) and progress.baseline.get("phase") == "refill")


def is_artifact_spot(fact: object) -> bool:
    return isinstance(fact, Mapping) and (
        fact.get("kind") == "object" and fact.get("qualified_item_id") == "(O)590"
        or fact.get("kind") == "terrain" and fact.get("terrain_type") == "ArtifactSpot")


def eligible_target(progress: ToolWorkProgress, args: Mapping, window: Mapping | None,
                    water: Mapping | None, location_id: str) -> tuple[str, dict] | None:
    """The single permitted target on the exact adjacent tool tile, or None.

    Returns (ref, fact) where fact is the observed scene fact, or a synthetic
    {"kind": "tile", "tile": {...}} for till/refill (empty tiles and water are not scene facts).
    """
    if progress.kind not in KINDS or not tool_matches(progress, args.get("qualified_item_id")):
        return None
    hit = tool_tile(args, window)
    if hit is None:
        return None
    x, y = hit
    facts = _facts_on(window, x, y)
    entry = _tile_entry(window, x, y)
    tile = {"x": x, "y": y}
    if refill_phase(progress):
        if (water is None or entry is None or entry.get("water_refill") is not True
                or entry.get("villager") is not False):
            return None
        if progress.kind == "refill_can" and not water["water_left"] < water["water_max"]:
            return None
        if progress.kind == "water_tile" and water["water_left"] != 0:
            return None
        return tile_ref(location_id, x, y), {"kind": "tile", "tile": tile, "phase": "refill"}
    if progress.kind == "break_rock":
        found = [(r, f) for r, f in facts if f.get("kind") == "object" and f.get("name") == "Stone"]
    elif progress.kind == "cut_twig":
        found = [(r, f) for r, f in facts if f.get("kind") == "object" and f.get("name") in ("Twig", "Twigs")]
    elif progress.kind == "cut_small_tree":
        found = [(r, f) for r, f in facts if is_small_wild_tree(f)]
    elif progress.kind == "cut_weed":
        if not scythe_area_safe(args, window):  # review fix (WS1 major 1): the sweep reaches past the faced tile
            return None
        found = [(r, f) for r, f in facts if f.get("kind") == "object" and f.get("name") == "Weeds"]
    elif progress.kind == "cut_grass":
        # FIXB18 D-B15a-1 (live V2B15aT6-a: only Grass terrain features in view): an explicit grass goal cuts a Grass
        # tuft (the mod's BgToolTileGuard admits it); `rumput liar` stays Weeds only (owner semantics).
        if not scythe_area_safe(args, window):
            return None
        found = [(r, f) for r, f in facts if f.get("kind") == "terrain" and f.get("terrain_type") == "Grass"]
    elif progress.kind == "till_tile":
        artifact_spots = [(r, f) for r, f in facts if is_artifact_spot(f)]
        if artifact_spots:
            if (len(facts) != 1 or len(artifact_spots) != 1
                    or entry is not None and entry.get("villager") is not False):
                return None
            return artifact_spots[0][0], dict(artifact_spots[0][1])
        if (facts or entry is None or entry.get("tillable") is not True or entry.get("villager") is not False):
            return None
        return tile_ref(location_id, x, y), {"kind": "tile", "tile": tile, "phase": "work"}
    elif progress.kind == "water_tile":
        if water is None or not water["water_left"] > 0:
            return None
        found = [(r, f) for r, f in facts if f.get("kind") == "terrain"
                 and f.get("terrain_type") == "HoeDirt" and _crop_watered(f) is False
                 and (not progress.crop_required or crop_needs_water(f))]
    else:
        return None
    if len(found) != 1 or (entry is not None and entry.get("villager") is True):
        return None
    return found[0][0], dict(found[0][1])


def eligibility_miss(progress: ToolWorkProgress, args: Mapping, window: Mapping | None) -> str | None:
    """FIXB20 D-B17-2: why eligible_target() refused the faced tile (diagnostic, journaled by the tool-work rule)."""
    if not tool_matches(progress, args.get("qualified_item_id")):
        return "wrong_tool"
    hit = tool_tile(args, window)
    if hit is None:
        return tool_tile_reason(args, window) or "tool_tile_unobserved"
    if progress.kind in SCYTHE_KINDS:
        center = _xy((window or {}).get("center"))
        tiles = scythe_area_tiles(args.get("tile_x"), args.get("tile_y"), args.get("facing"))
        if not tiles or center is None or any(abs(x - center[0]) > 1 or abs(y - center[1]) > 1 for x, y in tiles):
            return "scythe_area_outside_window"
        for x, y in tiles:
            for _, fact in _facts_on(window, x, y):
                tree = fact.get("wild_tree") if fact.get("kind") == "terrain" else None
                if isinstance(tree, Mapping) and not (type(tree.get("growth_stage")) is int and tree["growth_stage"] >= 5):
                    return "scythe_area_unsafe:sapling@%d,%d" % (x, y)
                if fact.get("kind") == "terrain" and fact.get("terrain_type") == "HoeDirt" and _has_crop(fact):
                    return "scythe_area_unsafe:crop@%d,%d" % (x, y)
    entry = _tile_entry(window, *hit)
    if entry is not None and entry.get("villager") is True:
        return "villager_at_tool_tile"
    return "no_target_fact_on_tool_tile"


def baseline_for(progress: ToolWorkProgress, fact: Mapping, water: Mapping | None) -> dict:
    if fact.get("phase") == "refill":
        return {"phase": "refill", "water_left": water["water_left"] if water else None}
    if progress.kind == "break_rock":
        return {"phase": "work", "minutes_until_ready": fact.get("minutes_until_ready")}
    if progress.kind == "water_tile":
        return {"phase": "work", "watered": False, "water_left": water["water_left"] if water else None,
                **({"crop": dict(fact["crop"])} if progress.crop_required and live_crop(fact) else {})}
    if progress.kind == "till_tile":
        return {"phase": "work", "artifact_spot": is_artifact_spot(fact)}
    if progress.kind == "cut_grass":
        return {"phase": "work", "target": "grass"}
    return {"phase": "work"}


def pin_target(progress: ToolWorkProgress, ref: str, fact: Mapping, water: Mapping | None) -> ToolWorkProgress:
    tile = _xy(fact.get("tile"))
    return replace(progress, target_ref=ref, target_tile=tile, baseline=baseline_for(progress, fact, water))


def _classify(progress: ToolWorkProgress, window: Mapping, water: Mapping | None) -> tuple[str, str, dict | None]:
    """(outcome, reason, new_baseline) of the latest settled step against the pinned baseline."""
    base = progress.baseline or {}
    tile = progress.target_tile
    if refill_phase(progress):
        if water is None:
            return "uncertain", "water_level_unavailable", None
        before = base.get("water_left")
        if (type(before) is int and water["water_left"] == water["water_max"] and water["water_left"] > before):
            return "refilled", "can_refilled", None
        return "whiff", "no_observed_water_change", None
    center = _xy(window.get("center"))
    if tile is None or center is None or abs(tile[0] - center[0]) > 1 or abs(tile[1] - center[1]) > 1:
        return "uncertain", "target_outside_current_window", None
    targets = window.get("targets") or {}
    if progress.kind == "break_rock":
        fact = targets.get(progress.target_ref)
        if fact is None:
            return "effect", "rock_removed", None
        now, before = fact.get("minutes_until_ready"), base.get("minutes_until_ready")
        if type(now) is int and type(before) is int and now < before:
            return "progress", "rock_damaged", dict(base, minutes_until_ready=now)
        return "whiff", "no_observed_change", None
    if progress.kind in SCYTHE_KINDS:
        if targets.get(progress.target_ref) is None:
            return "effect", "grass_cut" if progress.kind == "cut_grass" else "weed_cut", None
        return "whiff", "no_observed_change", None
    if progress.kind == "cut_twig":
        fact = targets.get(progress.target_ref)
        if fact is None or fact.get("name") not in ("Twig", "Twigs"):
            return "effect", "twig_cut", None
        return "whiff", "no_observed_change", None
    if progress.kind == "cut_small_tree":
        fact = targets.get(progress.target_ref)
        if fact is None:
            return "effect", "small_tree_removed", None
        if not is_small_wild_tree(fact):
            return "uncertain", "small_tree_changed", None
        return "whiff", "no_observed_change", None
    if progress.kind == "till_tile":
        if base.get("artifact_spot"):
            fact = targets.get(progress.target_ref)
            if fact is None or not is_artifact_spot(fact):
                return "effect", "artifact_spot_dug", None
            return "whiff", "no_observed_change", None
        if any(f.get("kind") == "terrain" and f.get("terrain_type") == "HoeDirt"
               for _, f in _facts_on(window, *tile)):
            return "effect", "tile_tilled", None
        return "whiff", "no_observed_change", None
    if progress.kind == "water_tile":
        fact = targets.get(progress.target_ref)
        if fact is None:
            return "gone", "target_gone", None
        if progress.crop_required and (not live_crop(fact) or not base.get("crop")
                or any(fact["crop"].get(k) != base["crop"].get(k) for k in ("qualified_item_id", "phase"))):
            return "uncertain", "planted_crop_changed", None
        if _crop_watered(fact) is True:
            return "effect", "tile_watered", None
        return "whiff", "no_observed_change", None
    return "uncertain", "unknown_kind", None


def settle_pending(progress: ToolWorkProgress, water: Mapping | None, captured_ticks: int, step_tick: int) -> bool:
    """A refill whose water change is not yet visible waits (bounded) instead of counting as a whiff."""
    if not refill_phase(progress) or water is None:
        return False
    before = (progress.baseline or {}).get("water_left")
    changed = type(before) is int and water["water_left"] > before
    return not changed and captured_ticks - step_tick < REFILL_SETTLE_TICKS


def settle_step(progress: ToolWorkProgress, window: Mapping, water: Mapping | None, *,
                request_id: str | None, captured_ticks: int) -> ToolWorkProgress:
    """Apply exactly one settled use. The effect is set once and never from input completion alone."""
    outcome, reason, new_base = _classify(progress, window, water)
    settled = progress.settled_steps + 1
    if outcome == "effect" or (outcome == "refilled" and progress.kind == "refill_can"):
        effect = {"kind": progress.kind, "reason": reason, "request_id": request_id,
                  "captured_ticks": captured_ticks, "target_ref": progress.target_ref}
        if progress.kind in SCYTHE_KINDS:
            effect["side_effects"] = "unattributed"  # the scythe area reaches past the faced tile
        if progress.kind == "water_tile" and water is not None:
            effect["water_left_after"] = water["water_left"]  # supporting evidence only
        if progress.kind == "refill_can" and water is not None:
            effect["water_left_after"] = water["water_left"]
        return replace(progress, status="EFFECT_OBSERVED", reason=reason, settled_steps=settled,
                       last_step="effect", effect=effect)
    if outcome == "refilled":  # water_tile: back to the work phase; the next target is a dry tile
        return replace(progress, status="WORK_REMAINING", reason="can_refilled_continue", settled_steps=settled,
                       last_step="refilled", target_ref=None, target_tile=None, baseline=None, whiffs=0)
    if outcome == "progress":
        return replace(progress, status="WORK_REMAINING", reason=reason, settled_steps=settled,
                       last_step="progress", baseline=new_base)
    if outcome == "whiff":
        whiffs = progress.whiffs + 1
        grass = (progress.baseline or {}).get("target") == "grass"
        limit = (MAX_WHIFFS_GRASS if grass else MAX_WHIFFS_SMALL_TREE if progress.kind == "cut_small_tree"
                 else MAX_WHIFFS)
        if whiffs >= limit:
            return replace(progress, status="UNAVAILABLE", reason="no_effect_after_settled_steps",
                           settled_steps=settled, whiffs=whiffs, last_step="whiff")
        return replace(progress, status=progress.status, reason="whiff_" + reason, settled_steps=settled,
                       whiffs=whiffs, last_step="whiff")
    if outcome == "gone":
        return replace(progress, status="UNAVAILABLE", reason=reason, settled_steps=settled, last_step="gone")
    return replace(progress, status="UNCERTAIN", reason=reason, settled_steps=settled, last_step="uncertain")


def refresh_phase(progress: ToolWorkProgress, water: Mapping | None) -> ToolWorkProgress:
    """water_tile only, with no step outstanding: NEEDS_REFILL while the can is empty."""
    if progress.kind != "water_tile" or water is None:
        return progress
    if progress.status == "WORK_REMAINING" and water["water_left"] == 0:
        return replace(progress, status="NEEDS_REFILL", reason="can_empty_refill_required",
                       target_ref=None, target_tile=None, baseline=None)
    if progress.status == "NEEDS_REFILL" and water["water_left"] > 0:
        # Water appeared without a settled refill step of ours (e.g. a co-op player): no credit.
        return replace(progress, status="WORK_REMAINING", reason="water_observed_without_refill_step",
                       target_ref=None, target_tile=None, baseline=None)
    return progress


def budget(progress: ToolWorkProgress, steps: int) -> ToolWorkProgress:
    maximum = KINDS[progress.kind][1]
    progress = replace(progress, remaining_steps=max(0, maximum - steps))
    if progress.status in ACTIVE and steps >= maximum and steps == progress.settled_steps:
        return replace(progress, status="BUDGET_EXHAUSTED", reason="finite_tool_work_budget")
    return progress


def offer_reason(progress: ToolWorkProgress | None, equipped_qid: object, target: str | None) -> str | None:
    """Why world_key.use_tool is NOT offered (None = offer it)."""
    if progress is None:
        return "tool_requires_tool_work_objective" if equipped_qid in TOOL_WORK_ONLY_QIDS else None
    if not tool_matches(progress, equipped_qid):
        return "equip_" + progress.tool_qid
    if progress.status not in ACTIVE:
        return "tool_work_" + progress.status.lower()
    if target is None:
        return "no_permitted_target_on_tool_tile"
    return None


def needs_energy(progress: ToolWorkProgress) -> bool:
    """Every kind but the scythe and a refill costs stamina (runtime floor 20, mod floor 4)."""
    return progress.kind not in SCYTHE_KINDS and not refill_phase(progress)


def approach_rank(progress: ToolWorkProgress, fact: object) -> int:
    """1 = this objective's own observable target kind, 3 = everything else (till/refill: none)."""
    if not isinstance(fact, Mapping):
        return 3
    if progress.kind == "break_rock" and fact.get("kind") == "object" and fact.get("name") == "Stone":
        return 1
    if progress.kind == "cut_twig" and fact.get("kind") == "object" and fact.get("name") in ("Twig", "Twigs"):
        return 1
    if progress.kind == "cut_small_tree" and is_small_wild_tree(fact):
        return 1
    if progress.kind == "cut_weed" and fact.get("kind") == "object" and fact.get("name") == "Weeds":
        return 1
    if progress.kind == "cut_grass" and fact.get("kind") == "terrain" and fact.get("terrain_type") == "Grass":
        return 1  # FIXB18 D-B15a-1 (explicit grass goal only)
    if (progress.kind == "water_tile" and progress.status == "WORK_REMAINING" and fact.get("kind") == "terrain"
            and fact.get("terrain_type") == "HoeDirt" and _crop_watered(fact) is False
            and (not progress.crop_required or crop_needs_water(fact))):
        return 1
    return 3


def wire(progress: ToolWorkProgress, water: Mapping | None) -> dict:
    """Bounded provider projection: no ids, baselines or request receipts."""
    out = {"kind": progress.kind, "tool_qid": progress.tool_qid, "status": progress.status,
           "reason": progress.reason, "target_tile": list(progress.target_tile) if progress.target_tile else None,
           "settled_steps": progress.settled_steps, "remaining_steps": progress.remaining_steps,
           "causal_credit": progress.causal_credit}
    if progress.last_step:
        out["last_step"] = progress.last_step
    if progress.effect:
        out["effect_observed"] = progress.effect.get("reason")
    if progress.tool_qid == "(T)WateringCan" and water is not None:
        out["water_left"], out["water_max"] = water["water_left"], water["water_max"]
    return {k: v for k, v in out.items() if v is not None}


_TARGET_WORDS = {"break_rock": "the stone to break", "till_tile": "the empty tile to till",
                 "water_tile": "the dry tilled tile to water", "refill_can": "the water to refill from",
                 "cut_weed": "the weeds to cut", "cut_grass": "the grass tuft to cut"}


def act_now_guidance(progress: ToolWorkProgress | None) -> str:
    """Imperative line when use_tool is offered on the faced target (mirrors the axe path's line)."""
    if progress is None:
        return ""
    target = (_TARGET_WORDS["refill_can"] if progress.status == "NEEDS_REFILL"
              else _TARGET_WORDS.get(progress.kind, "the target"))
    return (f" use_tool is offered only while tool_work.tool_qid is equipped and its exact hit tile is"
            f" faces_target ({target}), verified now: use_tool now; don't inspect it.")


GUIDANCE = (" Tool work: equip tool_work.tool_qid, face the target (a 1-tick move_cardinal turns);"
            " one use_tool at faces_target; the effect appears in tool_work.")
