"""PM-1 fix10d (a13 d4:J2312-2319): locked backpack slots are no take_out room.

The chest's actor grid presents 36 buttons while the backpack had 12 (all full). The mod now publishes
``actor_usable`` (Farmer.MaxItems); Python's take_room mirrors the mod's capacity term and must skip slots past it.
"""
from arti_stardew_economy import take_room

PARSNIP = {"qualified_item_id": "(O)24", "quality": 0, "stack": 1, "max_stack": 999}


def _block(usable, full=12):
    slots = [{"slot": i, "item": {"qualified_item_id": "(O)390", "quality": 0, "stack": 18, "max_stack": 999}
              if i < full else None} for i in range(36)]
    block = {"available": True, "actor_slots": slots}
    if usable is not None:
        block["actor_usable"] = usable
    return block


def test_full_backpack_has_no_room_past_actor_usable():
    assert take_room(_block(12), PARSNIP) == 0


def test_free_backpack_slot_still_counts():
    assert take_room(_block(12, full=11), PARSNIP) == 999


def test_matching_stack_room_counts_inside_usable():
    block = _block(12)
    block["actor_slots"][3]["item"] = dict(PARSNIP, stack=5)
    assert take_room(block, PARSNIP) == 994


def test_without_the_fact_the_old_reading_is_kept():
    assert take_room(_block(None), PARSNIP) == 24 * 999
