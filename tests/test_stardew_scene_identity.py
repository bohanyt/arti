"""OBS-2B1 issue #14: identitas semantik sebagai kunci opaq di store.

Python tidak menghitung SceneIdentity.Token — itu milik C#. Tes ini
mengunci: (1) store/diff memperlakukan ID encoded sebagai kunci opaq,
(2) penggantian tile = remove ID lama + upsert ID baru, (3) snapshot
dan delta runtime menyerahkan raw ke ObjectFactId yang sama.
Tidak mengubah arti receiver/store.
"""

from __future__ import annotations

import json
from pathlib import Path

from arti_stardew import decode_event
from arti_stardew_observation import RichObservationStore, observation_payload_sah
from arti_stardew_scene import SCENE_DOMAIN
from arti_stardew_scene_delta import (
    apply_scene_patch,
    canonical_json,
    diff_scene_facts,
    facts_equal,
)

ROOT = Path(__file__).resolve().parents[1]
P2 = -100000000000000001
CAPTURE = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Observation" / "SceneSnapshotCapture.cs"
MOD_ENTRY = ROOT / "stardew-mod" / "ArtiStardewBridge" / "ModEntry.cs"
IDENTITY = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Protocol" / "SceneSnapshotLogic.cs"
BRIDGE = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Protocol" / "BridgeClient.cs"
RECEIVER = ROOT / "arti_stardew.py"


def _observation(*, seq: int, payload: dict) -> dict:
    return {
        "protocol": 1,
        "ev": "observation",
        "seq": seq,
        "player_id": P2,
        "screen_id": 1,
        "payload": payload,
    }


def _object_fact(qid: str, x: int, y: int) -> dict:
    return {
        "kind": "object",
        "qualified_item_id": qid,
        "name": "n",
        "display_name": "n",
        "tile": {"x": x, "y": y},
        "passable": False,
        "grabbable": True,
        "machine": False,
    }


def _snapshot(facts: dict[str, object], *, revision: int = 1) -> dict:
    return {
        "session_id": "obs:id-hard:1:1",
        "product": "scene",
        "domain": SCENE_DOMAIN,
        "kind": "snapshot",
        "domain_revision": revision,
        "provenance": "ACTIVE_SYNCED",
        "authoritative": True,
        "available": True,
        "error": None,
        "truncation": {
            "truncated": False,
            "observed_count": len(facts),
            "sent_count": len(facts),
        },
        "body": {"facts": facts},
    }


def _delta(*, revision: int, upserts: dict, removes: list[str]) -> dict:
    observed = len(upserts) + len(removes)
    return {
        "session_id": "obs:id-hard:1:1",
        "product": "scene",
        "domain": SCENE_DOMAIN,
        "kind": "delta",
        "domain_revision": revision,
        "provenance": "ACTIVE_SYNCED",
        "authoritative": True,
        "available": True,
        "error": None,
        "truncation": {
            "truncated": False,
            "observed_count": observed,
            "sent_count": observed,
        },
        "body": {"upserts": upserts, "removes": removes},
    }


def test_store_mengganti_objek_di_tile_sama_lewat_remove_plus_upsert():
    """qid berbeda di tile sama = ID berbeda; patch wajib remove lama + upsert baru."""
    old_id = "obj:Farm:12,8:(O)472"
    new_id = "obj:Farm:12,8:(O)390"
    assert old_id != new_id
    facts = {
        "loc:Farm": {
            "kind": "location",
            "location_id": "Farm",
            "location_kind": "Farm",
            "is_outdoors": True,
            "map_width": 80,
            "map_height": 65,
        },
        old_id: _object_fact("(O)472", 12, 8),
    }
    snap = _snapshot(facts)
    assert observation_payload_sah(snap)
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(seq=1, payload=snap)))).accepted

    after = dict(facts)
    after.pop(old_id)
    after[new_id] = _object_fact("(O)390", 12, 8)
    patch = diff_scene_facts(facts, after)
    assert patch.removes == (old_id,)
    assert list(patch.upserts) == [new_id]
    delta = _delta(revision=2, upserts=dict(patch.upserts), removes=list(patch.removes))
    assert observation_payload_sah(delta)
    hasil = brain.apply(decode_event(json.dumps(_observation(seq=2, payload=delta))))
    assert hasil.accepted
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec is not None
    assert old_id not in rec.facts
    assert new_id in rec.facts
    assert rec.facts[new_id]["qualified_item_id"] == "(O)390"
    assert facts_equal(dict(rec.facts), apply_scene_patch(
        facts, upserts=patch.upserts, removes=patch.removes
    ))


def test_remove_lalu_readd_id_yang_sama_konvergen():
    fact_id = "obj:Farm%3AHouse:1,2:(O)388"
    body = _object_fact("(O)388", 1, 2)
    facts = {
        "loc:Farm%3AHouse": {
            "kind": "location",
            "location_id": "Farm%3AHouse",
            "location_kind": "Farm",
            "is_outdoors": False,
            "map_width": 10,
            "map_height": 10,
        },
        fact_id: body,
    }
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(
        _observation(seq=1, payload=_snapshot(facts))
    ))).accepted

    gone = _delta(revision=2, upserts={}, removes=[fact_id])
    assert brain.apply(decode_event(json.dumps(_observation(seq=2, payload=gone)))).accepted
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec is not None
    assert fact_id not in rec.facts

    back = _delta(revision=3, upserts={fact_id: body}, removes=[])
    assert brain.apply(decode_event(json.dumps(_observation(seq=3, payload=back)))).accepted
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec is not None
    assert fact_id in rec.facts
    assert facts_equal(rec.facts[fact_id], body)


def test_kunci_percent_unicode_survives_json_python_receiver():
    """ID encoded C# (ASCII %XX) wajib lolos json.dumps/loads + decode_event."""
    weird = "obj:Farm%3AHouse:1,2:%F0%9F%98%80"
    facts = {
        "loc:Farm%3AHouse": {
            "kind": "location",
            "location_id": "Farm%3AHouse",
            "location_kind": "Farm",
            "is_outdoors": True,
            "map_width": 8,
            "map_height": 8,
        },
        weird: _object_fact("%F0%9F%98%80", 1, 2),
    }
    line = json.dumps(_observation(seq=4, payload=_snapshot(facts)), ensure_ascii=False)
    ev = decode_event(line)
    assert ev is not None
    assert weird in ev["payload"]["body"]["facts"]
    roundtrip = json.loads(line)
    assert list(roundtrip["payload"]["body"]["facts"]) == list(facts)
    assert weird in json.dumps(roundtrip, ensure_ascii=False)


def test_urutan_patch_ordinal_termasuk_id_encoded():
    before = {
        "obj:Farm%3AZ:1,1:(O)1": {"n": 1},
        "obj:Farm:1,1:(O)1": {"n": 1},
        "obj:Farm:1,1:(O)2": {"n": 1},
    }
    after = {
        "obj:Cabin%3A1:0,0:(O)388": {"n": 2},
        "obj:Farm:2,2:(O)390": {"n": 2},
    }
    first = diff_scene_facts(before, after)
    second = diff_scene_facts(
        dict(reversed(list(before.items()))),
        dict(reversed(list(after.items()))),
    )
    assert first.removes == second.removes == tuple(sorted(before))
    assert list(first.upserts) == list(second.upserts) == sorted(after)
    capped = diff_scene_facts(before, after, max_patch_ops=2)
    assert capped.truncated
    assert capped.removes == tuple(sorted(before)[:2])
    assert dict(capped.upserts) == {}


def test_canonical_json_tidak_mengubah_kunci_identity():
    facts = {"obj:A%2FB:0,0:(O)1": {"qualified_item_id": "A%2FB"}}
    assert "obj:A%2FB:0,0:(O)1" in canonical_json(facts)
    assert facts_equal(facts, json.loads(canonical_json(facts)))


def test_snapshot_dan_delta_runtime_menyerahkan_raw_ke_objectfactid():
    capture = CAPTURE.read_text(encoding="utf-8")
    entry = MOD_ENTRY.read_text(encoding="utf-8")
    identity = IDENTITY.read_text(encoding="utf-8")
    compact_entry = " ".join(entry.split())

    assert "CaptureObject(locationRaw," in capture
    assert "CaptureObject(locationId," not in capture
    assert 'string qid = obj.QualifiedItemId ?? "";' in capture
    assert "SceneIdentity.ObjectFactId(locationId, x, y, qid)" in capture
    assert "ObjectFactId(locationId, x, y, SceneIdentity.Token" not in capture

    assert "string locationRaw = e.Location.NameOrUniqueName" in entry
    assert "SceneIdentity.ObjectFactId( locationRaw," in compact_entry
    assert 'pair.Value.QualifiedItemId ?? ""' in entry
    assert "SceneSnapshotCapture.CaptureObject( locationRaw," in compact_entry

    assert "obj:{Token(locationId)}:{x},{y}:{Token(qualifiedItemId)}" in identity
    assert "s[start] == ' ' || s[start] == '\\t'" in identity
    assert "s[end] == ' ' || s[end] == '\\t'" in identity


def test_issue14_tidak_menyentuh_bridgeclient_atau_receiver():
    """Penjaga scope: identitas di-stress tanpa mengubah transport/receiver."""
    bridge = BRIDGE.read_text(encoding="utf-8")
    receiver = RECEIVER.read_text(encoding="utf-8")
    assert "SendAfterHelloAsync" in bridge
    assert "MAX_LINE_BYTES = 64 * 1024" in receiver
    assert "class BridgeClient" in bridge
