"""WS1 tool work, pure module (no IO): grammar, exact tool tile, eligibility, effect, budgets."""
from dataclasses import asdict, replace

import pytest

import arti_stardew_tool_work as tw

LOC = "Farm"
STONE = "obj:Farm:3:9:(O)450"
WEED = "obj:Farm:3:9:(O)674"
DIRT = "ter:Farm:3:9:HoeDirt"


def args(qid, tile=(3, 8), facing=2, ticks=3):
    return dict(slot=0, qualified_item_id=qid, tile_x=tile[0], tile_y=tile[1], facing=facing, ticks=ticks)


def window(targets=None, tool_tile=(3, 9), tiles=None, tick=20, center=(3, 8)):
    w = dict(kind="gather_window", available=True, location_id=LOC, captured_ticks=tick,
             center=dict(x=center[0], y=center[1]), radius=1, targets=dict(targets or {}), drops=[],
             drops_complete=True, using_tool=False, last_use=None)
    if tool_tile is not None:
        w["tool_tile"] = dict(x=tool_tile[0], y=tool_tile[1])
    if tiles is not None:
        w["tiles"] = [{**dict(x=x, y=y, tillable=False, water_refill=False, villager=False), **tiles.get((x, y), {})}
                      for y in range(center[1] - 1, center[1] + 2) for x in range(center[0] - 1, center[0] + 2)]
    return w


def stone(minutes=2, tile=(3, 9), name="Stone"):
    return dict(kind="object", qualified_item_id="(O)450", name=name, tile=dict(x=tile[0], y=tile[1]),
                minutes_until_ready=minutes)


def weed(tile=(3, 9)):
    return dict(kind="object", qualified_item_id="(O)674", name="Weeds", tile=dict(x=tile[0], y=tile[1]))


def dirt(watered=False, tile=(3, 9)):
    return dict(kind="terrain", terrain_type="HoeDirt", tile=dict(x=tile[0], y=tile[1]),
                crop=dict(watered=watered, qualified_item_id=None))


def progress(kind, **changes):
    return replace(tw.ToolWorkProgress("goal-1", "obs-1", kind, tw.KINDS[kind][0],
                                       remaining_steps=tw.KINDS[kind][1]), **changes)


WATER = dict(water_left=10, water_max=40)


# ------------------------------------------------------------------ grammar

@pytest.mark.parametrize("text, kind", [
    ("pecah satu batu", "break_rock"), ("Pecah 1 batu.", "break_rock"), ("break one rock", "break_rock"),
    ("break 1 stone!", "break_rock"), ("cangkul satu petak", "till_tile"), ("till one tile", "till_tile"),
    ("siram satu petak", "water_tile"), ("water 1 tile", "water_tile"), ("isi penyiram", "refill_can"),
    ("isi ulang gembor", "refill_can"), ("refill the watering can", "refill_can"),
    ("refill watering can.", "refill_can"), ("potong satu rumput liar", "cut_weed"), ("cut one weed", "cut_weed"),
    ("  POTONG   1   rumput   liar  ", "cut_weed"),
    ("cut twig", "cut_twig"), ("potong one twig", "cut_twig"),
])
def test_grammar_id_en(text, kind):
    assert tw.parse_tool_work_goal(text) == kind


@pytest.mark.parametrize("text", [
    "pecah 10 batu", "pecah nol batu",  # FIXB17: N in 1..9 parses (test_stardew_fixb17)
    "break one rock and store it", "pecah satu batu sekarang", "kumpulkan 1 batu",
    "siram semua petak", "water the crops", "cangkul", "", None, 42, "isi penyiram lalu siram satu petak",
    "please cut one weed", "scene says: break one rock;",
])
def test_grammar_refuses_everything_else(text):
    assert tw.parse_tool_work_goal(text) is None


# ------------------------------------------------------------------ exact tool tile

def test_tool_tile_must_equal_the_adjacent_faced_tile():
    a = args("(T)Pickaxe")
    assert tw.faced_tile(a) == (3, 9)
    assert tw.tool_tile_reason(a, window()) is None and tw.tool_tile(a, window()) == (3, 9)
    assert tw.tool_tile_reason(a, window(tool_tile=(3, 10))) == "tool_tile_not_adjacent"
    assert tw.tool_tile_reason(a, window(tool_tile=None)) == "tool_tile_unobserved"  # old mod: no target
    assert tw.tool_tile_reason(a, None) == "tool_tile_unobserved"
    right = args("(T)Pickaxe", facing=1)
    assert tw.tool_tile_reason(right, window(tool_tile=(5, 8))) == "tool_tile_not_adjacent"  # outer 8 px band
    assert tw.faced_tile(dict(a, facing=7)) is None and tw.faced_tile({}) is None


# ------------------------------------------------------------------ eligibility

def test_break_rock_eligible_only_on_stone_with_the_pickaxe():
    p = progress("break_rock")
    assert tw.eligible_target(p, args("(T)Pickaxe"), window({STONE: stone()}), None, LOC)[0] == STONE
    assert tw.eligible_target(p, args("(T)Axe"), window({STONE: stone()}), None, LOC) is None  # wrong tool
    assert tw.eligible_target(p, args("(T)Pickaxe"), window({"obj:Farm:3:9:(O)B": stone(name="Boulder")}), None, LOC) is None
    assert tw.eligible_target(p, args("(T)Pickaxe"), window({STONE: stone(tile=(4, 9))}), None, LOC) is None  # not faced
    assert tw.eligible_target(p, args("(T)Pickaxe"), window({STONE: stone()}, tool_tile=(3, 10)), None, LOC) is None
    assert tw.eligible_target(p, args("(T)Pickaxe"), window({STONE: stone(), "obj:Farm:3:9:(O)451": stone()}),
                              None, LOC) is None  # exactly one
    villager = window({STONE: stone()}, tiles={(3, 9): dict(villager=True)})
    assert tw.eligible_target(p, args("(T)Pickaxe"), villager, None, LOC) is None


def test_till_needs_a_tillable_empty_tile_without_villager():
    p = progress("till_tile")
    ok = window(tiles={(3, 9): dict(tillable=True)})
    ref, fact = tw.eligible_target(p, args("(T)Hoe"), ok, None, LOC)
    assert ref == "tile:Farm:3,9" and fact["kind"] == "tile" and fact["phase"] == "work"
    assert tw.eligible_target(p, args("(T)Hoe"), window(tiles={}), None, LOC) is None  # grass/untillable
    assert tw.eligible_target(p, args("(T)Hoe"), window(), None, LOC) is None  # no tiles key (flag OFF)
    assert tw.eligible_target(p, args("(T)Hoe"), window({WEED: weed()}, tiles={(3, 9): dict(tillable=True)}),
                              None, LOC) is None  # a fact on the tile
    assert tw.eligible_target(p, args("(T)Hoe"), window(tiles={(3, 9): dict(tillable=True, villager=True)}),
                              None, LOC) is None


def test_till_artifact_spot_accepts_only_one_typed_exact_tile_fact():
    p = progress("till_tile")
    spot = dict(kind="terrain", terrain_type="ArtifactSpot", tile=dict(x=3, y=9))
    ref, fact = tw.eligible_target(p, args("(T)Hoe"), window({"artifact:3,9": spot}), None, LOC)
    assert ref == "artifact:3,9" and fact["terrain_type"] == "ArtifactSpot"
    ordinary = window({WEED: weed()}, tiles={(3, 9): dict(tillable=True)})
    assert tw.eligible_target(p, args("(T)Hoe"), ordinary, None, LOC) is None
    ambiguous = window({"artifact:3,9": spot, WEED: weed()})
    assert tw.eligible_target(p, args("(T)Hoe"), ambiguous, None, LOC) is None
    wrong_type = window({"artifact:3,9": dict(spot, terrain_type="HoeDirt")})
    assert tw.eligible_target(p, args("(T)Hoe"), wrong_type, None, LOC) is None


def test_cut_twig_requires_exact_twig_fact_and_axe_qid():
    p = progress("cut_twig")
    twig = dict(kind="object", qualified_item_id="(O)294", name="Twig", tile={"x": 3, "y": 9})
    assert tw.eligible_target(p, args("(T)Axe"), window({"twig-ref": twig}), None, LOC)[0] == "twig-ref"
    assert tw.eligible_target(p, args("(T)SteelAxe"), window({"twig-ref": twig}), None, LOC)[0] == "twig-ref"
    assert tw.eligible_target(p, args("(T)Pickaxe"), window({"twig-ref": twig}), None, LOC) is None
    assert tw.eligible_target(p, args("(T)Axe"), window({STONE: stone()}), None, LOC) is None
    pinned = tw.pin_target(p, "twig-ref", twig, None)
    assert settle(pinned, window(), tick=30).reason == "twig_cut"


def test_artifact_spot_effect_requires_the_pinned_typed_fact_to_change():
    p = progress("till_tile")
    spot = dict(kind="terrain", terrain_type="ArtifactSpot", tile={"x": 3, "y": 9})
    pinned = tw.pin_target(p, "artifact:3,9", spot, None)
    assert settle(pinned, window({"artifact:3,9": spot}), tick=30).last_step == "whiff"
    assert settle(pinned, window(), tick=31).reason == "artifact_spot_dug"


def test_water_tile_needs_dry_dirt_and_water_else_refill_target():
    p = progress("water_tile")
    assert tw.eligible_target(p, args("(T)WateringCan"), window({DIRT: dirt()}), WATER, LOC)[0] == DIRT
    assert tw.eligible_target(p, args("(T)WateringCan"), window({DIRT: dirt(watered=True)}), WATER, LOC) is None
    assert tw.eligible_target(p, args("(T)WateringCan"), window({DIRT: dirt()}), None, LOC) is None  # level unknown
    empty = dict(water_left=0, water_max=40)
    assert tw.eligible_target(p, args("(T)WateringCan"), window({DIRT: dirt()}), empty, LOC) is None
    needs = replace(p, status="NEEDS_REFILL")
    pond = window(tiles={(3, 9): dict(water_refill=True)})
    ref, fact = tw.eligible_target(needs, args("(T)WateringCan"), pond, empty, LOC)
    assert ref == "tile:Farm:3,9" and fact["phase"] == "refill"
    assert tw.eligible_target(needs, args("(T)WateringCan"), window({DIRT: dirt()}), empty, LOC) is None


def test_refill_can_needs_water_below_max_on_a_refill_tile():
    p = progress("refill_can")
    pond = window(tiles={(3, 9): dict(water_refill=True)})
    assert tw.eligible_target(p, args("(T)WateringCan"), pond, dict(water_left=39, water_max=40), LOC)[0] == "tile:Farm:3,9"
    assert tw.eligible_target(p, args("(T)WateringCan"), pond, dict(water_left=40, water_max=40), LOC) is None  # full
    assert tw.eligible_target(p, args("(T)WateringCan"), window(tiles={}), dict(water_left=0, water_max=40), LOC) is None
    assert tw.eligible_target(p, args("(T)WateringCan"), pond, None, LOC) is None


def test_cut_weed_only_on_weeds_with_the_basic_scythe():
    p = progress("cut_weed")
    assert tw.eligible_target(p, args("(W)47"), window({WEED: weed()}), None, LOC)[0] == WEED
    assert tw.eligible_target(p, args("(W)53"), window({WEED: weed()}), None, LOC) is None
    assert tw.eligible_target(p, args("(W)47"), window({STONE: stone()}), None, LOC) is None


# ------------------------------------------------------------------ effects

def settle(p, w, water=None, tick=30):
    return tw.settle_step(p, w, water, request_id="req-1", captured_ticks=tick)


def test_rock_damaged_then_removed():
    p = tw.pin_target(progress("break_rock"), STONE, stone(minutes=2), None)
    assert p.baseline == {"phase": "work", "minutes_until_ready": 2} and p.target_tile == (3, 9)
    p = settle(p, window({STONE: stone(minutes=1)}))
    assert (p.status, p.reason, p.last_step, p.baseline["minutes_until_ready"]) == ("WORK_REMAINING", "rock_damaged", "progress", 1)
    p = settle(p, window({}))
    assert (p.status, p.reason) == ("EFFECT_OBSERVED", "rock_removed")
    assert p.effect["request_id"] == "req-1" and p.effect["captured_ticks"] == 30 and p.causal_credit == "unattributed"


def test_till_water_weed_effects():
    till = tw.pin_target(progress("till_tile"), "tile:Farm:3,9", dict(kind="tile", tile=dict(x=3, y=9), phase="work"), None)
    assert settle(till, window({DIRT: dirt()})).reason == "tile_tilled"
    water = tw.pin_target(progress("water_tile"), DIRT, dirt(), WATER)
    done = settle(water, window({DIRT: dirt(watered=True)}), dict(water_left=9, water_max=40))
    assert (done.status, done.reason, done.effect["water_left_after"]) == ("EFFECT_OBSERVED", "tile_watered", 9)
    assert settle(water, window({}), WATER).status == "UNAVAILABLE"  # target gone
    cut = tw.pin_target(progress("cut_weed"), WEED, weed(), None)
    done = settle(cut, window({}))
    assert done.reason == "weed_cut" and done.effect["side_effects"] == "unattributed"


def test_refill_effect_and_water_tile_needs_refill_round_trip():
    pond = window(tiles={(3, 9): dict(water_refill=True)})
    refill = tw.pin_target(progress("refill_can"), "tile:Farm:3,9", dict(kind="tile", tile=dict(x=3, y=9), phase="refill"),
                           dict(water_left=39, water_max=40))
    done = settle(refill, pond, dict(water_left=40, water_max=40))
    assert (done.status, done.reason) == ("EFFECT_OBSERVED", "can_refilled")
    p = tw.refresh_phase(progress("water_tile"), dict(water_left=0, water_max=40))
    assert (p.status, p.reason) == ("NEEDS_REFILL", "can_empty_refill_required")
    p = tw.pin_target(p, "tile:Farm:3,9", dict(kind="tile", tile=dict(x=3, y=9), phase="refill"), dict(water_left=0, water_max=40))
    p = settle(p, pond, dict(water_left=40, water_max=40))
    assert (p.status, p.reason, p.last_step, p.target_ref, p.effect) == (
        "WORK_REMAINING", "can_refilled_continue", "refilled", None, None)
    # Water appearing without our own refill step gives no credit, only the phase change.
    other = tw.refresh_phase(replace(progress("water_tile"), status="NEEDS_REFILL"), WATER)
    assert (other.status, other.reason) == ("WORK_REMAINING", "water_observed_without_refill_step")


def test_refill_waits_bounded_for_the_inventory_water_change():
    p = tw.pin_target(progress("refill_can"), "tile:Farm:3,9", dict(kind="tile", tile=dict(x=3, y=9), phase="refill"),
                      dict(water_left=39, water_max=40))
    assert tw.settle_pending(p, dict(water_left=39, water_max=40), captured_ticks=110, step_tick=100)
    assert not tw.settle_pending(p, dict(water_left=40, water_max=40), captured_ticks=110, step_tick=100)
    assert not tw.settle_pending(p, dict(water_left=39, water_max=40), captured_ticks=100 + tw.REFILL_SETTLE_TICKS, step_tick=100)
    assert not tw.settle_pending(progress("break_rock"), WATER, 101, 100)


def test_two_whiffs_unavailable_budget_and_uncertain_window():
    p = tw.pin_target(progress("break_rock"), STONE, stone(minutes=2), None)
    once = settle(p, window({STONE: stone(minutes=2)}))
    assert (once.status, once.whiffs, once.last_step) == ("WORK_REMAINING", 1, "whiff")
    twice = settle(once, window({STONE: stone(minutes=2)}))
    assert (twice.status, twice.reason) == ("UNAVAILABLE", "no_effect_after_settled_steps")
    assert settle(p, window({STONE: stone()}, center=(3, 11))).status == "UNCERTAIN"  # target outside window
    spent = tw.budget(replace(p, settled_steps=6), steps=6)
    assert (spent.status, spent.remaining_steps) == ("BUDGET_EXHAUSTED", 0)
    assert tw.budget(replace(p, settled_steps=5), steps=6).status == "WORK_REMAINING"  # last step not settled yet
    assert tw.budget(p, steps=2).remaining_steps == 4


def test_effect_is_never_set_without_a_settled_step():
    p = tw.pin_target(progress("cut_weed"), WEED, weed(), None)
    assert p.effect is None and p.status == "WORK_REMAINING"
    assert tw.refresh_phase(p, None) is p and tw.budget(p, 0).effect is None


# ------------------------------------------------------------------ offer / wire / approach

def test_offer_reason_and_energy_rule():
    assert tw.offer_reason(None, "(T)Hoe", None) == "tool_requires_tool_work_objective"
    assert tw.offer_reason(None, "(W)47", None) == "tool_requires_tool_work_objective"
    assert tw.offer_reason(None, "(T)Axe", None) is None  # legacy axe/pickaxe paths unchanged
    p = progress("till_tile")
    assert tw.offer_reason(p, "(T)Axe", "tile:Farm:3,9") == "equip_(T)Hoe"
    assert tw.offer_reason(p, "(T)Hoe", None) == "no_permitted_target_on_tool_tile"
    assert tw.offer_reason(p, "(T)Hoe", "tile:Farm:3,9") is None
    assert tw.offer_reason(replace(p, status="EFFECT_OBSERVED"), "(T)Hoe", "x") == "tool_work_effect_observed"
    assert not tw.needs_energy(progress("cut_weed")) and not tw.needs_energy(progress("refill_can"))
    assert tw.needs_energy(progress("break_rock")) and not tw.needs_energy(replace(progress("water_tile"), status="NEEDS_REFILL"))


def test_wire_projection_has_no_ids_or_baselines():
    p = settle(tw.pin_target(progress("water_tile"), DIRT, dirt(), WATER), window({DIRT: dirt(watered=True)}), WATER)
    out = tw.wire(p, dict(water_left=9, water_max=40))
    assert out["effect_observed"] == "tile_watered" and out["water_left"] == 9 and out["target_tile"] == [3, 9]
    flat = repr(out)
    for secret in ("goal-1", "obs-1", "req-1", "baseline", DIRT):
        assert secret not in flat
    assert "water_left" not in tw.wire(progress("break_rock"), WATER)
    assert asdict(p)["effect"]["request_id"] == "req-1"  # local receipt keeps the id


def test_approach_rank_per_kind():
    assert tw.approach_rank(progress("break_rock"), stone()) == 1
    assert tw.approach_rank(progress("break_rock"), weed()) == 3
    assert tw.approach_rank(progress("cut_weed"), weed()) == 1
    assert tw.approach_rank(progress("water_tile"), dirt()) == 1
    assert tw.approach_rank(progress("water_tile"), dirt(watered=True)) == 3
    assert tw.approach_rank(progress("till_tile"), dirt()) == 3 and tw.approach_rank(progress("till_tile"), None) == 3


def test_upgraded_pickaxes_count_for_break_rock_only():
    p = progress("break_rock")
    for qid in ("(T)Pickaxe", "(T)CopperPickaxe", "(T)IridiumPickaxe"):
        assert tw.tool_matches(p, qid) and tw.eligible_target(p, args(qid), window({STONE: stone()}), None, LOC)[0] == STONE
        assert tw.offer_reason(p, qid, STONE) is None
    assert not tw.tool_matches(p, "(T)Axe") and not tw.tool_matches(progress("till_tile"), "(T)CopperHoe")


def sapling(stage, tile):
    return dict(kind="terrain", terrain_type="Tree", tile=dict(x=tile[0], y=tile[1]),
                wild_tree=dict(tree_type="1", growth_stage=stage, tapped=False, stump=False))


def test_scythe_sweep_tiles_and_unsafe_neighbours():
    assert sorted(tw.scythe_area_tiles(3, 8, 2)) == [(2, 8), (2, 9), (3, 8), (3, 9), (4, 8), (4, 9)]
    assert sorted(tw.scythe_area_tiles(3, 8, 1)) == [(3, 7), (3, 8), (3, 9), (4, 7), (4, 8), (4, 9)]
    assert tw.scythe_area_tiles(3, 8, 7) == []
    p, a = progress("cut_weed"), args("(W)47")
    base = {WEED: weed()}
    assert tw.eligible_target(p, a, window(base), None, LOC) is not None
    for stage in (0, 1, 4):
        unsafe = dict(base, **{"ter:Farm:4:9:Tree": sapling(stage, (4, 9))})
        assert tw.eligible_target(p, a, window(unsafe), None, LOC) is None, stage
    assert tw.eligible_target(p, a, window(dict(base, **{"ter:Farm:4:9:Tree": sapling(5, (4, 9))})), None, LOC) is not None
    crop = dict(kind="terrain", terrain_type="HoeDirt", tile=dict(x=2, y=8), crop=dict(phase=1, watered=False))
    assert tw.eligible_target(p, a, window(dict(base, **{"ter:Farm:2:8:HoeDirt": crop})), None, LOC) is None
    empty = dict(crop, crop=dict(phase=None, qualified_item_id=None, watered=False))
    assert tw.eligible_target(p, a, window(dict(base, **{"ter:Farm:2:8:HoeDirt": empty})), None, LOC) is not None
    behind = dict(base, **{"ter:Farm:3:7:Tree": sapling(1, (3, 7))})  # behind the player: outside the sweep
    assert tw.eligible_target(p, a, window(behind), None, LOC) is not None
    assert tw.eligible_target(p, a, window(base, center=(3, 7)), None, LOC) is None  # sweep not inside the window
