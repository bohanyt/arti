"""a58 T49/T100: the newest native move, remaining bag and chest memory agree."""
from types import SimpleNamespace as NS

from arti_runtime.chest_memory import ChestMemory
from arti_runtime.transfer_outcomes import publish, publish_changes, lines
from arti_stardew_live7_recovery import Live7RecoveryMixin
from scripts.live7.native_facts import completed_receipt

CHEST = "obj:Farm:74,27:(BC)130"
WOOD, SEEDS = "(O)388", "(O)472"
DATE = (1, "spring", 8)


def view(bag, chest, *, item=SEEDS, quality=0, cursor=None):
    def slots(n):
        return [dict(slot=0, item=dict(qualified_item_id=item, stack=n, quality=quality, name="Parsnip Seeds") if n else None)]
    return dict(actor_slots=slots(bag), container_slots=slots(chest), source_fact_id=CHEST,
                container_token="native-chest", receiver_token="r", readback_token="b", cursor=cursor)


def rig():
    memory = ChestMemory()
    memory.observe(CHEST, view(5, 0)["container_slots"], date=DATE)
    s = NS(session_id="s", mode="part_live", campaign=NS(date=DATE,
        live=NS(item_names={WOOD: "Wood", SEEDS: "Parsnip Seeds"}, chest_memory=memory)))
    rt = NS(_skill_session=s, events=[], _state=NS(latest=lambda p: NS(time_of_day=1200)))
    rt._binding_view = lambda: (None, 1)
    rt._live7_mind_heartbeat = lambda *a: None
    rt._live7_semantic_receipts = lambda: 0
    def journal(row):
        rt.events.append(row)
        Live7RecoveryMixin._live7_note_journal(rt, row)
    rt._journal = journal
    return rt, s


def move(rt, request, before, after, *, item=SEEDS, mode="put"):
    return publish(rt, dict(qualified_item_id=item, quality=0, mode=mode,
        container_token="native-chest", source_fact_id=CHEST), request, before, after)


def test_a58_t49_store_reports_zero_wood_left_in_bag_on_next_page():
    rt, s = rig()
    row = move(rt, "wood", view(110, 0, item=WOOD), view(0, 110, item=WOOD), item=WOOD)
    assert row["moved"] == 110 and row["bag_left"] == 0
    assert s.campaign.live.chest_memory.counts()[WOOD] == 110
    assert "Wood: pindah 110" in lines(s)[0] and "tersisa 0 di tas" in lines(s)[0]
    assert s._persona_completed_action["bag_left"] == 0


def test_a58_t100_in_out_in_out_keeps_final_seeds_in_bag_not_stored():
    rt, s = rig()
    for request, before, after, mode in [
        ("in1", view(5, 0), view(0, 5), "put"),
        ("out1", view(0, 5), view(5, 0), "take_out"),
        ("in2", view(5, 0), view(0, 5), "put"),
        ("out2", view(0, 5), view(5, 0), "take_out")]:
        assert move(rt, request, before, after, mode=mode)
    assert SEEDS not in s.campaign.live.chest_memory.counts()
    assert len(lines(s)) == 1 and "tas dari peti Farm 74,27" in lines(s)[0]
    assert "tersisa 5 di tas" in lines(s)[0]
    assert s._persona_completed_action["effect"] == "withdrawn"
    # The older store objective's close arrives after the latest withdrawal.
    rt._journal(dict(ev="finish_verified", verdict=dict(status="achieved", reason="observed_paired_transfer_objective",
        observed=dict(chest_gain=5, bag_decrease=5))))
    assert s._persona_completed_action["effect"] == "withdrawn"
    assert s._persona_completed_action["request_id"] == "out2"
    assert ChestMemory(s.campaign.live.chest_memory.dump()).counts() == {}


def test_partial_native_move_reports_actual_amount_and_remaining_stack():
    rt, s = rig()
    row = move(rt, "partial", view(16, 0), view(11, 5))
    assert row["moved"] == 5 and row["bag_left"] == 11
    assert s.campaign.live.chest_memory.counts()[SEEDS] == 5
    assert completed_receipt(row, 0)["quantity"] == 5


def test_unpaired_delta_unread_after_or_foreign_chest_cannot_claim_completion():
    rt, s = rig()
    assert move(rt, "unpaired", view(16, 0), view(11, 0)) is None
    assert move(rt, "unread", view(16, 0), None) is None
    foreign = dict(view(11, 5), source_fact_id="obj:Farm:4,4:(BC)130")
    assert move(rt, "foreign", view(16, 0), foreign) is None
    assert not hasattr(s, "_persona_completed_action")


def test_repeated_publication_is_idempotent():
    rt, s = rig()
    assert move(rt, "same", view(5, 0), view(0, 5))
    assert move(rt, "same", view(5, 0), view(0, 5)) is None
    assert len([r for r in rt.events if r["ev"] == "item_move_outcome"]) == 1


def test_cursor_move_is_only_finished_when_the_item_reaches_the_bag():
    rt, s = rig()
    initial = view(0, 5)
    held = view(0, 0, cursor=dict(qualified_item_id=SEEDS, stack=5, quality=0))
    publish_changes(rt, "lift", initial, held)
    assert not lines(s)
    publish_changes(rt, "settle", held, view(5, 0))
    assert s._persona_completed_action["effect"] == "withdrawn"
    assert s._persona_completed_action["bag_left"] == 5
    assert len([r for r in rt.events if r["ev"] == "item_move_outcome"]) == 1
