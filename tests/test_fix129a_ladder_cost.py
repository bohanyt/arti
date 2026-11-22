"""fix129A Part T: the goal ladder's per-page receipt work reads its ledger once.

The recorded S1 replay (ADR 227) found ``remember`` re-reading the whole
discoveries ledger once per evidence row (674 reads per page). These tests pin
the cause and the identity of the cheaper path: same rows written, same
receipts returned, same place names, same evidence rows.
"""
import re

import arti_state
from arti_runtime import goal_ladder as G
from arti_runtime.fact_line import Witnesses
from arti_runtime.seen_receipts import SeenReceipts

SCOPE = "save:actor"


def evidence(n, day=10):
    rows = []
    for i in range(n):
        kind = ("item_seen", "location_entered", "text_read", "dialogue_read", "npc_met")[i % 5]
        rows.append(dict(id="ev-%d" % i, kind=kind, subject="Thing%d" % (i % 17), text="Native words %d" % i,
                         source="native:%d" % i, day=day - (i % 3), event_place="Town", clock=600 + i))
    rows.append(dict(kind="menu_text", subject="SkillsPage", surface="SkillsPage", root_menu="GameMenu",
                     text="Farming 1", source="native_menu:GameMenu", event_place="Farm", clock=630))
    rows.append(dict(kind="text_read", subject="sign", text="Town square", source="native:sign"))
    return rows


def counting(monkeypatch):
    calls = []
    original = arti_state.read_jsonl

    def read(path, *a, **k):
        calls.append(str(path))
        return original(path, *a, **k)
    monkeypatch.setattr(arti_state, "read_jsonl", read)
    return calls


def reference_remember(base, scope, rows, day):
    """remember() with the repeat shortcut disabled: note() (and its re-read) for every row."""
    store = SeenReceipts(base, scope)
    original = store.repeat
    try:
        SeenReceipts.repeat = staticmethod(lambda *a, **k: None)
        return G.remember(base, scope, rows, day)
    finally:
        SeenReceipts.repeat = staticmethod(original)


def test_repeat_page_reads_the_ledger_once_and_writes_nothing(tmp_path, monkeypatch):
    rows = evidence(120)
    assert G.remember(tmp_path, SCOPE, rows, 10) > 0
    path = SeenReceipts(tmp_path, SCOPE).path
    before = path.read_bytes()
    calls = counting(monkeypatch)
    assert G.remember(tmp_path, SCOPE, rows, 10) == 0
    assert path.read_bytes() == before
    # ADR 231 part C: the unchanged file is parsed once per revision (jsonl_snapshot), so a repeat page reads it 0 times.
    assert sum(p.endswith("discoveries.jsonl") for p in calls) <= 1


def test_cheaper_remember_writes_the_same_ledger_and_returns_the_same_count(tmp_path):
    rows = evidence(60)
    fast, slow = tmp_path / "fast", tmp_path / "slow"
    for day in (10, 11):
        assert G.remember(fast, SCOPE, rows, day) == reference_remember(slow, SCOPE, rows, day)
    # Two identical rows inside one call: the second is an exact repeat of the first write.
    twice = [rows[0], dict(rows[0]), dict(rows[1], clock=999)]
    assert G.remember(fast, SCOPE, twice, 12) == reference_remember(slow, SCOPE, twice, 12)
    for name in ("discoveries.jsonl", "ledger.jsonl", "journey.jsonl"):
        a, b = fast / "stardew" / name, slow / "stardew" / name
        assert a.exists() == b.exists()
        if a.exists():
            assert a.read_bytes() == b.read_bytes(), name


def test_repeat_is_exactly_what_note_returns_without_writing(tmp_path):
    store = SeenReceipts(tmp_path, SCOPE)
    args = dict(game_day=91, clock=900, place="Town", source="native:J1", text_ref="t1")
    first = store.note("door_message", "SeedShop", **args)
    later = store.note("door_message", "SeedShop", **dict(args, clock=1000))
    found = store.index()
    assert store.repeat(found, "door_message", "SeedShop", **args) == dict(first, first=False)
    assert store.repeat(found, "door_message", "SeedShop", **dict(args, clock=1000)) == dict(later, first=False)
    assert store.repeat(found, "door_message", "SeedShop", **dict(args, clock=1100)) is None
    assert store.repeat(found, "door_message", "Other", **args) is None
    assert SeenReceipts(tmp_path, "save:other").index() == {}
    assert store.receipts(id="SeenReceipts") == [] and store.receipts("door_message", id="SeedShop") == [first, later]


def test_single_receipt_projection_equals_the_filtered_full_list(tmp_path):
    G.remember(tmp_path, SCOPE, evidence(40), 10)
    full = G.facts(tmp_path, SCOPE)
    for key in {(f["receipt_kind"], f["receipt_id"]) for f in full}:
        assert G.facts(tmp_path, SCOPE, only=key) == [f for f in full if (f["receipt_kind"], f["receipt_id"]) == key]


def old_names(w):
    """The pre-fix129A Witnesses.names body, kept here as the reference."""
    from arti_runtime.native_rows import native_rows
    sources = dict(w.entered)
    for place in native_rows((w.catalog.get("world_graph") or {}).get("places", ())):
        identity = place.get("id")
        if identity == w.current:
            sources.setdefault(identity, None)
        for row in w.matching(("menu_tab_opened", "door_message", "letter_opened", "calendar_day_read",
                               "event_watched", "quest_entry_read", "dialogue_read", "text_read")):
            text = w.text(row)
            if any(isinstance(n, str) and n and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.I)
                   for n in (identity, place.get("label"))):
                sources.setdefault(identity, row)
    return sources


def test_place_names_are_the_same_first_matching_receipts():
    texts = ["Pierre's General Store opens", "the Saloon is closed", "See you at the Beach!",
             "nothing about places", "Town Square and the SEEDSHOP", "Beach again"]
    events = [dict(id="t%d" % i, kind="text_read", text=t,
                   receipt=dict(kind=("door_message", "text_read", "letter_opened")[i % 3], id="r%d" % i,
                                text_ref="t%d" % i, game_day=dict(absolute=3, text="Y1 spring 3"), source="native"))
              for i, t in enumerate(texts)]
    events.append(dict(kind="location_entered", subject="Mine", date="1:spring:2", source="native_visit"))
    places = [dict(id=i, label=l) for i, l in (("Town", "Town Square"), ("SeedShop", "Pierre's General Store"),
                                               ("Saloon", "Stardrop Saloon"), ("Beach", "Beach"), ("Mine", "Mines"),
                                               ("Farm", "Farm"), ("Desert", None), (None, "Beach"))]
    catalog = dict(scope=SCOPE, discoveries=events, world_graph=dict(current="Farm", places=places))
    w = Witnesses(catalog)
    assert w.names() == old_names(w)
    assert set(w.names()) == {"Town", "SeedShop", "Saloon", "Beach", "Mine", "Farm", None}
