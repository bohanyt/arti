"""OBS-3A: snapshot inventaris/peralatan farmer, terpisah dari MOTOR_STATE."""

from __future__ import annotations

import json
from pathlib import Path

from arti_stardew import StardewStateStore, decode_event
from arti_stardew_observation import (
    KNOWN_DOMAINS,
    KNOWN_OBSERVATION_PAIRS,
    RichObservationStore,
    observation_payload_sah,
)

ROOT = Path(__file__).resolve().parents[1]
P2 = -100000000000000001
SESSION = "obs:demo:1:1"
INV = "progress.inventory"
SCENE = "scene.current_location"
KONTRAK = ROOT / "tests" / "data" / "stardew_inventory_snapshot_contract.json"


def _hello(*, seq: int = 1) -> dict:
    return {
        "protocol": 1,
        "ev": "hello",
        "seq": seq,
        "player_id": P2,
        "screen_id": 1,
        "player_name": "Arti",
        "is_main_player": False,
        "is_split_screen": False,
    }


def _state(*, seq: int = 2) -> dict:
    return {
        "protocol": 1,
        "ev": "state",
        "seq": seq,
        "player_id": P2,
        "screen_id": 1,
        "payload": {
            "location": "FarmHouse",
            "tile": {"x": 3, "y": 8},
            "pixel": {"x": 192.0, "y": 512.0},
            "facing": 2,
            "time_of_day": 610,
            "date": {"season": "spring", "day": 1, "year": 1},
            "energy": 270.0,
            "health": 100,
            "is_player_free": True,
            "can_player_move": True,
            "active_menu": None,
            "inventory": {
                "active_slot": 0,
                "hotbar": [{"slot": 0, "name": "Hoe", "stack": 1, "is_tool": True}],
                "slots_used": 1,
                "items_total": 1,
            },
        },
    }


def _scene_facts() -> dict:
    return {"loc:Farm": {"kind": "location", "location_id": "Farm"}}


def _inv_facts() -> dict:
    return {
        "meta:inventory": {"kind": "meta", "active_slot": 0, "slot_count": 4},
        "slot:0": {
            "kind": "slot",
            "slot": 0,
            "occupied": True,
            "is_active": True,
            "qualified_item_id": "(T)IridiumHoe",
            "name": "Iridium Hoe",
            "display_name": "Iridium Hoe",
            "stack": 1,
            "quality": 0,
            "category": -99,
            "type_definition": "(T)",
            "type_token": "tool",
            "tool_subtype": "Hoe",
            "tool_upgrade_level": 2,
        },
        "slot:1": {"kind": "slot", "slot": 1, "occupied": False, "is_active": False},
        "slot:2": {
            "kind": "slot",
            "slot": 2,
            "occupied": True,
            "is_active": False,
            "qualified_item_id": "(O)276",
            "display_name": "Parsnip",
            "stack": 8,
            "quality": 2,
            "type_token": "object",
        },
        "slot:3": {
            "kind": "slot",
            "slot": 3,
            "occupied": True,
            "is_active": False,
            "qualified_item_id": "(W)4",
            "type_token": "weapon",
            "tool_subtype": "MeleeWeapon",
            "weapon_subtype": "sword",
            "tool_upgrade_level": 0,
        },
        "eq:hat": {
            "kind": "equipment",
            "location": "hat",
            "occupied": True,
            "qualified_item_id": "(H)15",
            "type_token": "hat",
        },
        "eq:boots": {"kind": "equipment", "location": "boots", "occupied": False},
    }


def _obs(*, seq: int, domain: str = INV, product: str = "progress", **payload) -> dict:
    body = {
        "session_id": SESSION,
        "product": product,
        "domain": domain,
        "kind": "snapshot",
        "domain_revision": 1,
        "provenance": "LOCAL_AUTHORITATIVE",
        "authoritative": True,
        "available": True,
        "error": None,
        "truncation": None,
        "body": {"facts": _inv_facts()},
    }
    body.update(payload)
    facts = (body.get("body") or {}).get("facts") if isinstance(body.get("body"), dict) else None
    if (
        body.get("kind") == "snapshot"
        and isinstance(facts, dict)
        and body.get("truncation") is None
    ):
        body["truncation"] = {
            "truncated": False,
            "observed_count": len(facts),
            "sent_count": len(facts),
        }
    return {
        "protocol": 1,
        "ev": "observation",
        "seq": seq,
        "player_id": P2,
        "screen_id": 1,
        "payload": body,
    }


def test_known_pair_progress_inventory_ada():
    assert INV in KNOWN_DOMAINS
    assert ("progress", INV) in KNOWN_OBSERVATION_PAIRS
    assert ("progress", "progress.player") in KNOWN_OBSERVATION_PAIRS


def test_empty_and_occupied_slots_round_trip():
    brain = RichObservationStore()
    ev = decode_event(json.dumps(_obs(seq=3)))
    assert ev is not None
    assert observation_payload_sah(ev["payload"])
    assert brain.apply(ev).accepted
    rec = brain.get(P2, INV)
    assert rec is not None
    facts = dict(rec.facts)
    assert facts["slot:1"]["occupied"] is False
    assert facts["slot:0"]["occupied"] is True
    assert facts["slot:0"]["is_active"] is True
    assert facts["slot:0"]["tool_subtype"] == "Hoe"
    assert facts["slot:0"]["tool_upgrade_level"] == 2
    assert facts["slot:2"]["stack"] == 8
    assert facts["slot:2"]["quality"] == 2
    assert facts["slot:3"]["weapon_subtype"] == "sword"
    assert facts["slot:3"]["qualified_item_id"] == "(W)4"
    assert facts["meta:inventory"]["active_slot"] == 0
    assert facts["eq:hat"]["occupied"] is True
    assert facts["eq:boots"]["occupied"] is False


def test_python_decode_store_round_trip_contract():
    raw = json.loads(KONTRAK.read_text(encoding="utf-8"))
    line = json.dumps(raw, separators=(",", ":"))
    ev = decode_event(line)
    assert ev is not None
    assert ev["ev"] == "observation"
    assert ev["payload"]["domain"] == INV
    assert ev["payload"]["product"] == "progress"
    assert observation_payload_sah(ev["payload"])
    store = RichObservationStore()
    assert store.apply(ev).accepted
    rec = store.query(P2, INV, authoritative=True, complete=True)
    assert rec is not None
    assert rec.session_id == "obs:demo:1:1"
    assert rec.domain_revision == 1
    assert rec.provenance == "LOCAL_AUTHORITATIVE"
    assert rec.authoritative
    assert rec.is_complete
    assert rec.facts["slot:1"]["occupied"] is False
    assert rec.facts["slot:0"]["tool_upgrade_level"] == 2
    assert rec.facts["slot:2"]["quality"] == 2
    assert rec.facts["slot:3"]["weapon_subtype"] == "sword"
    assert rec.facts["meta:inventory"]["active_slot"] == 0
    assert rec.facts["eq:hat"]["qualified_item_id"] == "(H)15"


def test_session_and_revision_independent_from_scene():
    brain = RichObservationStore()
    scene = _obs(
        seq=3,
        domain=SCENE,
        product="scene",
        provenance="ACTIVE_SYNCED",
        body={"facts": _scene_facts()},
    )
    inv = _obs(seq=4, domain_revision=1)
    assert brain.apply(decode_event(json.dumps(scene))).accepted
    assert brain.apply(decode_event(json.dumps(inv))).accepted
    assert brain.current_revision(P2, SCENE) == 1
    assert brain.current_revision(P2, INV) == 1

    inv2_facts = dict(_inv_facts())
    inv2_facts["slot:2"] = {**inv2_facts["slot:2"], "stack": 9, "quality": 4}
    later = _obs(seq=5, domain_revision=2, body={"facts": inv2_facts})
    assert brain.apply(decode_event(json.dumps(later))).accepted
    assert brain.current_revision(P2, INV) == 2
    assert brain.current_revision(P2, SCENE) == 1
    rec = brain.get(P2, INV)
    assert rec.facts["slot:2"]["stack"] == 9
    assert rec.facts["slot:2"]["quality"] == 4
    assert brain.get(P2, SCENE).facts == _scene_facts()


def test_normalized_receiver_converges_on_newer_snapshot():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_obs(seq=3, domain_revision=1)))).accepted
    newer_facts = dict(_inv_facts())
    newer_facts["slot:1"] = {
        "kind": "slot",
        "slot": 1,
        "occupied": True,
        "is_active": False,
        "qualified_item_id": "(O)388",
        "stack": 12,
        "quality": 0,
        "type_token": "object",
    }
    assert brain.apply(decode_event(json.dumps(
        _obs(seq=4, domain_revision=2, body={"facts": newer_facts})
    ))).accepted
    rec = brain.query(P2, INV, authoritative=True, complete=True)
    assert rec is not None
    assert rec.domain_revision == 2
    assert rec.facts["slot:1"]["occupied"] is True
    assert rec.facts["slot:1"]["qualified_item_id"] == "(O)388"
    assert rec.facts["slot:0"]["tool_subtype"] == "Hoe"
    assert rec.stale is False
    assert rec.requires_resync is False


def test_unknown_progress_domain_fail_safe_leaves_inventory_and_motor():
    motor = StardewStateStore()
    brain = RichObservationStore()
    motor.catat(decode_event(json.dumps(_hello())))
    motor.catat(decode_event(json.dumps(_state(seq=2))))
    assert brain.apply(decode_event(json.dumps(_obs(seq=3)))).accepted

    asing = decode_event(json.dumps(_obs(
        seq=4,
        domain="progress.skills",
        body={"facts": {"level": 10}},
    )))
    assert asing is not None
    motor.catat(asing)
    hasil = brain.apply(asing)
    assert hasil.ignored
    assert hasil.reason == "unknown_domain"
    assert brain.get(P2, INV) is not None
    assert brain.get(P2, INV).domain_revision == 1
    assert brain.get(P2, "progress.skills") is None
    snap = motor.latest(P2)
    assert snap is not None
    assert snap.location == "FarmHouse"
    assert snap.active_slot == 0
    assert snap.hotbar[0].nama == "Hoe"


def test_malformed_inventory_payload_rejected():
    brain = RichObservationStore()
    rusak = _obs(seq=3)
    rusak["payload"]["kind"] = "snapshot"
    rusak["payload"]["body"] = {"upserts": {"slot:0": {"n": 1}}}
    assert observation_payload_sah(rusak["payload"]) is False
    assert decode_event(json.dumps(rusak)) is None
    assert brain.apply(rusak).rejected
    assert brain.get(P2, INV) is None


def test_scene_observation_tetap_berjalan_bersamaan():
    brain = RichObservationStore()
    scene = decode_event(json.dumps(_obs(
        seq=3,
        domain=SCENE,
        product="scene",
        provenance="ACTIVE_SYNCED",
        body={"facts": _scene_facts()},
    )))
    inv = decode_event(json.dumps(_obs(seq=4)))
    assert brain.apply(scene).accepted
    assert brain.apply(inv).accepted
    scene_rec = brain.query(P2, SCENE, authoritative=True, complete=True)
    inv_rec = brain.query(P2, INV, authoritative=True, complete=True)
    assert scene_rec is not None
    assert inv_rec is not None
    assert scene_rec.product == "scene"
    assert inv_rec.product == "progress"
    assert "slot:0" not in scene_rec.facts
    assert "loc:Farm" not in inv_rec.facts


def test_motor_state_tidak_membesar():
    motor = StardewStateStore()
    motor.catat(decode_event(json.dumps(_hello())))
    motor.catat(decode_event(json.dumps(_state(seq=2))))
    motor.catat(decode_event(json.dumps(_obs(seq=3))))
    snap = motor.latest(P2)
    assert snap is not None
    assert snap.seq == 2
    assert snap.active_slot == 0
    assert len(snap.hotbar) == 1
    assert not hasattr(snap, "facts")
    assert snap.slots_used == 1
    payload = _state(seq=2)["payload"]["inventory"]
    assert "qualified_item_id" not in payload
    assert "quality" not in payload
    assert "facts" not in payload


def test_error_inventory_unavailable_explicit():
    brain = RichObservationStore()
    ev = decode_event(json.dumps(_obs(
        seq=3,
        kind="error",
        available=False,
        error={"reason": "unavailable", "detail": "farmer"},
        body=None,
        truncation=None,
        authoritative=False,
        provenance="UNKNOWN",
    )))
    assert ev is not None
    assert brain.apply(ev).accepted
    rec = brain.get(P2, INV)
    assert rec.available is False
    assert rec.error_reason == "unavailable"
    assert rec.is_complete is False
    assert brain.query(P2, INV, authoritative=True) is None


def test_ws1_watering_can_water_level_passes_through_the_store():
    payload = _obs(seq=3)
    payload["payload"]["body"]["facts"]["slot:1"] = {
        "kind": "slot", "slot": 1, "occupied": True, "is_active": False, "qualified_item_id": "(T)WateringCan",
        "stack": 1, "quality": 0, "type_token": "tool", "tool_subtype": "WateringCan", "tool_upgrade_level": 0,
        "tool_water_left": 39, "tool_water_max": 40}
    brain = RichObservationStore()
    ev = decode_event(json.dumps(payload))
    assert ev is not None and observation_payload_sah(ev["payload"])
    assert brain.apply(ev).accepted
    facts = dict(brain.get(P2, INV).facts)
    assert facts["slot:1"]["tool_water_left"] == 39 and facts["slot:1"]["tool_water_max"] == 40
    assert "tool_water_left" not in facts["slot:0"]


def test_s31_rod_attachment_facts_pass_through_the_store_and_parse():
    import arti_stardew_skills_attempts as A
    payload = _obs(seq=3)
    payload["payload"]["body"]["facts"]["slot:1"] = {
        "kind": "slot", "slot": 1, "occupied": True, "is_active": False, "qualified_item_id": "(T)FiberglassRod",
        "stack": 1, "quality": 0, "type_token": "tool", "tool_subtype": "FishingRod", "tool_upgrade_level": 2,
        "tool_instance": "ti12", "attachment_slots": 1,
        "attachments": [{"slot": 0, "occupied": True, "qualified_item_id": "(O)685", "stack": 1, "quality": 0,
                         "category": -21}]}
    brain = RichObservationStore()
    ev = decode_event(json.dumps(payload))
    assert ev is not None and observation_payload_sah(ev["payload"])
    assert brain.apply(ev).accepted
    facts = dict(brain.get(P2, INV).facts)
    token, entries = A.rod_attachments(facts["slot:1"])
    assert token == "ti12" and [(e["slot"], e["item"], e["stack"]) for e in entries] == [(0, "(O)685", 1)]
    assert "attachments" not in facts["slot:0"] and A.rod_attachments(facts["slot:0"]) is None
