"""A recorded run: the model's copied chest tokens did not match, so every
world_interact facing the chest was refused. The runtime fills the exact observed target."""
from types import SimpleNamespace
from arti_stardew_decision import ActChoice, GameDecision, runtime_choice_admitted
from arti_stardew_runtime import _fill_observed_chest_args

TARGET = dict(target_token="a" * 64, source_fact_id="obj:Farm:45,15:(BC)130", tile_x=45, tile_y=15,
              actor_tile_x=45, actor_tile_y=16, facing=0, kind="chest")
T1 = dict(receiver_token="r", container_token="c", readback_token="b", source_fact_id="s", target_token="t1",
          slot=5, qualified_item_id="(O)388", stack=30, quality=0, owner_ceiling=30)
T2 = dict(T1, target_token="t2", slot=6, qualified_item_id="(O)92")


def decision(op, args):
    return GameDecision(1, "n", "g", 1, "ctx", "", ActChoice(op, None, args, ()))


def snap(**kw):
    return SimpleNamespace(**dict(dict(world_interaction_target=None, exit_target=None, transfer_targets=()), **kw))


def test_world_interact_args_are_filled_from_the_observed_target():
    for args in ({}, dict(TARGET, target_token="a" * 63 + "b"), {k: v for k, v in TARGET.items() if k != "source_fact_id"}):
        filled = _fill_observed_chest_args(decision("world_interact", args), snap(world_interaction_target=TARGET))
        assert filled.choice.args == TARGET and runtime_choice_admitted(filled)


def test_nothing_is_filled_without_an_offered_target():
    d = decision("world_interact", {})
    assert _fill_observed_chest_args(d, snap()) is d
    assert not runtime_choice_admitted(d)


def test_transfer_fills_only_missing_keys_and_keeps_single_quantity():
    filled = _fill_observed_chest_args(decision("container_transfer", dict(slot=5, quantity=1)),
                                       snap(transfer_targets=(T1, T2)))
    assert filled.choice.args == dict(T1, quantity=1) and runtime_choice_admitted(filled)
    ambiguous = decision("container_transfer", {})
    assert _fill_observed_chest_args(ambiguous, snap(transfer_targets=(T1, T2))) is ambiguous
    forged = decision("container_transfer", dict(slot=5, target_token="typo"))
    assert _fill_observed_chest_args(forged, snap(transfer_targets=(T1, T2))) is forged


def test_world_interact_with_a_different_tile_is_not_repaired():
    d = decision("world_interact", dict(TARGET, actor_tile_x=44))
    assert _fill_observed_chest_args(d, snap(world_interaction_target=TARGET)) is d
