"""ADR 231 part C: her record is read once per file revision, a new receipt is one appended line, and a page looks up
the game texts it shows from one ledger snapshot. Synthetic rows only; the rows every reader sees are unchanged."""
import hashlib
import json
import os

import pytest

import arti_mind as M
import arti_state
from arti_runtime import game_text_delivery as T
from arti_runtime import jsonl_snapshot as J
from arti_runtime.seen_receipts import SeenReceipts

SCOPE = "synthetic-save:c"


@pytest.fixture(autouse=True)
def fresh():
    J.clear()
    yield
    J.clear()


def write(path, rows, *, mode="w", newline=True):
    with open(path, mode, encoding="utf-8") as f:
        for i, row in enumerate(rows):
            f.write(json.dumps(row) + ("\n" if newline or i < len(rows) - 1 else ""))


def same(path):
    assert J.rows(path) == arti_state.read_jsonl(path)


def test_the_snapshot_reads_what_read_jsonl_reads_through_every_change(tmp_path):
    path = tmp_path / "discoveries.jsonl"
    assert J.rows(path) == [] == arti_state.read_jsonl(path)
    write(path, [dict(id="a", v=1), dict(id="b", v=1)])
    same(path)
    write(path, [dict(id="c", v=1), dict(id="a", v=2)], mode="a")  # an append, a repeated id keeps its position
    same(path)
    assert [r["id"] for r in J.rows(path)] == ["a", "b", "c"] and J.rows(path)[0]["v"] == 2
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"id": "d", "v"')  # a line still being written
    same(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(': 1}\n')
    same(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write("not json\n\n" + json.dumps(dict(id="e")) + "\n" + json.dumps(dict(nope=1)) + "\n")
    same(path)
    arti_state.write_jsonl_atomic(path, [dict(id="z", v=9)])  # replaced: read whole again
    same(path)
    write(path, [dict(id="y")])  # rewritten in place, shorter
    same(path)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(dict(id="bom")).encode() + b"\n")
    same(path)
    write(path, [dict(id="tail")], newline=False)  # a complete last line without its newline
    same(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + json.dumps(dict(id="after")) + "\n")
    same(path)


def test_an_unchanged_file_is_not_read_again_and_an_append_parses_only_new_lines(tmp_path, monkeypatch):
    path = tmp_path / "discoveries.jsonl"
    write(path, [dict(id=str(i)) for i in range(50)])
    J.rows(path)
    calls = []
    original = J._parse
    monkeypatch.setattr(J, "_parse", lambda data, by_id, *, first: (calls.append(data.count(b"\n")), original(data, by_id, first=first))[1])
    assert len(J.rows(path)) == 50 and calls == []
    write(path, [dict(id="new")], mode="a")
    assert len(J.rows(path)) == 51 and calls == [1]


def test_a_receipt_is_one_appended_line_and_reads_as_the_rewrite_did(tmp_path):
    store = SeenReceipts(tmp_path, SCOPE)
    store.note("place_entered", "Farm", game_day=92, clock=600, place="Farm", source="native_visit")
    before = store.path.read_bytes()
    second = store.note("place_entered", "Forest", game_day=92, clock=700, place="Forest", source="native_visit")
    after = store.path.read_bytes()
    assert after.startswith(before) and after.count(b"\n") == before.count(b"\n") + 1
    assert second["first"] is True
    assert store.note("place_entered", "Forest", game_day=92, clock=700, place="Forest",
                      source="native_visit")["first"] is False  # an exact repeat writes nothing
    assert store.path.read_bytes() == after
    rewritten = tmp_path / "rewritten"
    rows = arti_state.read_jsonl(store.path)
    other = SeenReceipts(rewritten, SCOPE)
    other.path.parent.mkdir(parents=True, exist_ok=True)
    arti_state.write_jsonl_atomic(other.path, rows)
    J.clear()
    assert store.receipts() == other.receipts() and store.index() == other.index()


def game_text(base, i):
    text = "Pesan %d dari game." % i
    facts = dict(v=1, kind="hud_notice", id="content:%d" % i, page="0", key="hud_notice:%d" % i,
                 hash=hashlib.sha256(text.encode()).hexdigest(), part=0, parts=1, presentation="p:%d" % i,
                 date="1:winter:8", clock=900 + i, place="Farm", save_id="synthetic", day=92,
                 source="Game1.hudMessages", speaker=None, chunks={"0": text}, capture_complete=True,
                 native_seq=i, capture_ticks=i, native_ref="ui.text:%d" % i, delivered=len(text), answered=True)
    T._save(base, SCOPE, "item-%d" % i, facts)
    return "game_text:item-%d" % i


def test_one_ledger_snapshot_gives_every_text_as_one_read_per_pointer_did(tmp_path):
    refs = [game_text(tmp_path, i) for i in range(30)] + ["game_text:missing"]
    index = T.texts(tmp_path, SCOPE)
    assert [T.read_text(tmp_path, SCOPE, r, index=index) for r in refs] == [T.read_text(tmp_path, SCOPE, r) for r in refs]
    assert T.read_text(tmp_path, SCOPE, refs[3], index=index)["complete"] is True
    assert T.read_text(tmp_path, SCOPE, "game_text:missing", index=index) is None


def test_settling_a_text_never_changes_the_shared_snapshot(tmp_path):
    ref = game_text(tmp_path, 1)
    key = ref.removeprefix("game_text:")
    row = next(r for r in T._rows(tmp_path, SCOPE) if r["item_id"] == key)
    row_facts = dict(row["facts"])
    T._settle(tmp_path, SCOPE, key, 3)
    assert row["facts"] == row_facts  # the snapshot row is untouched; the saved copy is the new row
    assert next(r for r in T._rows(tmp_path, SCOPE) if r["item_id"] == key)["facts"]["answered"] is True
