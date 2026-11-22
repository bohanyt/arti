"""Rule layer: in the store phase with a chest act offered, a second inspect in a row is refused."""
from types import SimpleNamespace as NS

from arti_stardew_runtime import StardewGoalRuntime, _TRANSIENT_PLAY_REFUSALS


def _req(world_target=None, approach=()):
    return NS(snapshot=NS(world_interaction_target=world_target, approach_targets=tuple(approach)))


def test_store_act_offered_requires_satisfied_wood_and_a_chest_act():
    satisfied = NS(gather_progress=NS(status="INVENTORY_SATISFIED"))
    pending = NS(gather_progress=NS(status="PENDING_PICKUP"))
    chest = {"ref": "obj:Farm:45,12:(BC)130"}
    assert StardewGoalRuntime._store_act_offered(satisfied, _req(approach=[chest]))
    assert StardewGoalRuntime._store_act_offered(satisfied, _req(world_target={"tile_x": 45}))
    assert not StardewGoalRuntime._store_act_offered(satisfied, _req(approach=[{"ref": "tf:Farm:44,10:Tree"}]))
    assert not StardewGoalRuntime._store_act_offered(pending, _req(approach=[chest]))
    assert not StardewGoalRuntime._store_act_offered(NS(gather_progress=None), _req(approach=[chest]))


def test_refusal_is_transient_so_the_runtime_refreshes():
    assert "store_act_offered_inspect_redundant" in _TRANSIENT_PLAY_REFUSALS


def test_offered_store_transfer_also_makes_a_second_inspect_redundant():
    """Live B2W1a: after a store chain stopped at its cap the model inspected in a loop."""
    satisfied = NS(gather_progress=NS(status="INVENTORY_SATISFIED"))
    req = NS(snapshot=NS(world_interaction_target=None, approach_targets=(), transfer_targets=({"slot": 0},)))
    assert StardewGoalRuntime._store_act_offered(satisfied, req)
    req = NS(snapshot=NS(world_interaction_target=None, approach_targets=(), transfer_targets=()))
    assert not StardewGoalRuntime._store_act_offered(satisfied, req)
