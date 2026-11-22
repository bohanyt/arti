"""Shared receipt contract: deterministic, scoped, additive, no lore inference."""
import json

from arti_runtime.seen_receipts import SeenReceipts, backfill_from_save


def test_first_receipt_survives_repeat_restart_and_other_actor(tmp_path):
    store = SeenReceipts(tmp_path, "save:actor")
    args = dict(game_day=91, clock=900, place="Town", source="journal:J12", text_ref="native:calendar:7")
    first = store.note("calendar_day_read", "winter7", **args)
    assert first == dict(v=1, kind="calendar_day_read", id="winter7",
                         game_day=dict(absolute=91, text="Y1 winter 7"), clock=900,
                         place="Town", source="journal:J12", text_ref="native:calendar:7", first=True)
    repeated = SeenReceipts(tmp_path, "save:actor").note("calendar_day_read", "winter7", **args)
    assert repeated == dict(first, first=False)
    assert store.seen("calendar_day_read", "winter7") == first
    assert SeenReceipts(tmp_path, "save:other").seen("calendar_day_read", "winter7") is None
    assert store.receipts(since_day=92) == []
    first["game_day"]["absolute"] = 1
    assert store.seen("calendar_day_read", "winter7")["game_day"]["absolute"] == 91


def test_later_native_text_read_can_follow_identity_only_save_backfill(tmp_path):
    store = SeenReceipts(tmp_path, "save:actor")
    args = dict(game_day=91, clock=600, place="Cabin", source="native_save_flags:recipes_known")
    first = store.note("recipe_seen", "Furnace", **args)
    read = store.note("recipe_seen", "Furnace", **dict(args, game_day=92, clock=900,
                      source="native_recipe:J14", text_ref="native:recipe:Furnace"))
    assert first["first"] and "text_ref" not in first
    assert not read["first"] and read["text_ref"] == "native:recipe:Furnace"
    assert store.seen("recipe_seen", "Furnace") == read
    assert store.receipts() == [first, read]
    assert store.note("recipe_seen", "Furnace", **dict(args, game_day=92, clock=900,
                      source="native_recipe:J14", text_ref="native:recipe:Furnace")) == read
    assert store.receipts() == [first, read]


def test_unknown_kinds_and_existing_first_seen_rows_are_kept(tmp_path):
    import arti_mind
    import arti_state
    store = SeenReceipts(tmp_path, "save:actor")
    arti_mind.record_discoveries(tmp_path, "stardew", scope="save:actor", day=1,
                                day_label="Y1 spring 1", discoveries=[dict(id="old", kind="item_seen", text="Native")])
    before = arti_state.read_jsonl(store.path)
    store.note("future_native_kind", "native-id", game_day=2, clock=610, place="Farm", source="native_reader")
    assert arti_state.read_jsonl(store.path)[:len(before)] == before
    assert len(store.receipts("future_native_kind")) == 1
    assert "wall" not in json.dumps(store.receipts())


def test_save_backfill_is_pure_explicit_and_identity_only():
    native = dict(game_day=112, clock=600, place="Cabin", source="recorded-save:sha",
                  places_visited=["Town", "Farm", "Town"], recipes_known={"Furnace": 0, "Chest": True},
                  mail_read=["winter_7"], mail_received=["winter_24"],
                  shop_menus_opened={"SeedShop": True, "FishShop": False})
    rows = backfill_from_save(native)
    assert rows == backfill_from_save(native)
    assert {(r["kind"], r["id"]) for r in rows} == {
        ("place_entered", "Farm"), ("place_entered", "Town"), ("recipe_seen", "Chest"), ("recipe_seen", "Furnace"),
        ("letter_opened", "winter_7"), ("shop_menu_seen", "SeedShop")}
    assert all(r["identity_only"] and r["backfilled"] and "text_ref" not in r for r in rows)
    assert backfill_from_save(dict(native, places_visited=[], recipes_known=[], mail_read=[], shop_menus_opened=[])) == []
