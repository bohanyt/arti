"""Verified gift receipts are quiet historical facts, never inventory claims."""

import json

from arti_live7_status_facts import StatusFactsRelay


def append(path, **row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_verified_receipt_survives_day_change_without_speaking(tmp_path):
    cycle = tmp_path / "c1"
    trial = "day-0001-attempt-000"
    append(cycle / "cycle_events.jsonl", ev="day_trial_started", day_trial=trial)
    journal = cycle / trial / "journal/g1_journal.jsonl"
    append(journal, ev="live7_gift_answer", answer="Yes", outcome="unverified", item_qid="(O)24")
    relay = StatusFactsRelay({}, None, clock=lambda: 10000)
    spoken = []
    tick = lambda: relay.tick(cycle, lambda *a, **kw: spoken.append(a), now=10000)
    tick()
    assert not relay.status_block()
    append(journal, ev="live7_gift_received", item_qid="(O)24", item_name="Parsnip", stack=2, sender="Bohan")
    tick()
    block = relay.status_block()
    assert "Barang yang kamu terima dari Bohan: Parsnip (x2)." in block
    assert "masih" not in block and not spoken
    append(cycle / "cycle_events.jsonl", ev="day_trial_started", day_trial="day-0002-attempt-000")
    tick()
    assert relay.status_block() == block and not spoken
    relay.tick(tmp_path / "c2", lambda *a, **kw: spoken.append(a), now=10000)
    assert not relay.status_block()


def test_attach_restores_receipt_and_sanitizes_native_strings(tmp_path):
    cycle = tmp_path / "c1"
    trial = "day-0001-attempt-000"
    append(cycle / "cycle_events.jsonl", ev="day_trial_started", day_trial=trial)
    journal = cycle / trial / "journal/g1_journal.jsonl"
    append(journal, ev="live7_gift_received", item_qid="(O)24", item_name="[TAG]Parsnip", stack=1, sender="<b>Bohan</b>")
    relay = StatusFactsRelay({}, None, clock=lambda: 10000)
    relay.tick(cycle, lambda *_a, **_kw: None, now=10000)
    block = relay.status_block()
    assert "dari Bohan: Parsnip (x1)." in block
    assert "[TAG]" not in block and "<b>" not in block


def test_receipt_requires_sender_item_and_positive_stack(tmp_path):
    cycle = tmp_path / "c1"
    trial = "day-0001-attempt-000"
    append(cycle / "cycle_events.jsonl", ev="day_trial_started", day_trial=trial)
    journal = cycle / trial / "journal/g1_journal.jsonl"
    for fields in ({"sender": ""}, {"item_qid": ""}, {"stack": 0}, {"stack": True}):
        row = dict(ev="live7_gift_received", item_qid="(O)24", stack=1, sender="Bohan")
        row.update(fields)
        append(journal, **row)
    relay = StatusFactsRelay({}, None, clock=lambda: 10000)
    relay.tick(cycle, lambda *_a, **_kw: None, now=10000)
    assert not relay.status_block()


def test_legacy_receipt_falls_back_to_qid_and_history_is_bounded(tmp_path):
    cycle = tmp_path / "c1"
    trial = "day-0001-attempt-000"
    append(cycle / "cycle_events.jsonl", ev="day_trial_started", day_trial=trial)
    journal = cycle / trial / "journal/g1_journal.jsonl"
    for index in range(10):
        append(journal, ev="live7_gift_received", item_qid=f"(O){index}", stack=1, sender="Bohan")
    relay = StatusFactsRelay({}, None, clock=lambda: 10000)
    relay.tick(cycle, lambda *_a, **_kw: None, now=10000)
    block = relay.status_block()
    assert block.count("Barang yang kamu terima") == 8
    assert "dari Bohan: barang (nama belum diketahui) (x1)." in block
    assert "(O)0" not in block
