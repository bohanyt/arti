"""OBS-2A: model delta scene + rekonsiliasi store. Tanpa runtime SMAPI."""

from __future__ import annotations

import json
from pathlib import Path

from arti_stardew import MAX_LINE_BYTES, StardewStateStore, decode_event
from arti_stardew_observation import RichObservationStore, observation_payload_sah
from arti_stardew_scene import SCENE_DOMAIN
from arti_stardew_scene_delta import (
    SCENE_DELTA_MAX_PATCH_OPS,
    apply_scene_delta,
    apply_scene_patch,
    build_delta_payload,
    canonical_json,
    diff_scene_facts,
    facts_equal,
)

ROOT = Path(__file__).resolve().parents[1]
P2 = -100000000000000001
SNAP = ROOT / "tests" / "data" / "stardew_scene_snapshot_contract.json"
DELTA = ROOT / "tests" / "data" / "stardew_scene_delta_contract.json"
CS_DELTA = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Protocol" / "SceneDeltaLogic.cs"
MOD_ENTRY = ROOT / "stardew-mod" / "ArtiStardewBridge" / "ModEntry.cs"
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


def _after_farm(before: dict[str, object]) -> dict[str, object]:
    after = dict(before)
    after.pop("obj:Farm:12,8:(O)472", None)
    keg = dict(after["obj:Farm:20,10:(BC)12"])  # type: ignore[arg-type]
    keg["minutes_until_ready"] = 10
    keg["ready_for_harvest"] = False
    after["obj:Farm:20,10:(BC)12"] = keg
    leah = dict(after["npc:Farm:Leah"])  # type: ignore[arg-type]
    tile = dict(leah["tile"])  # type: ignore[arg-type]
    tile["x"] = 19
    leah["tile"] = tile
    after["npc:Farm:Leah"] = leah
    after["obj:Farm:3,3:(O)388"] = {
        "kind": "object",
        "qualified_item_id": "(O)388",
        "name": "Wood",
        "display_name": "Wood",
        "tile": {"x": 3, "y": 3},
        "passable": False,
        "grabbable": True,
        "machine": False,
    }
    return after


def test_canonical_equality_abaikan_urutan_kunci():
    left = {"obj:x": {"n": 1, "m": 2}}
    right = {"obj:x": {"m": 2, "n": 1}}
    assert facts_equal(left["obj:x"], right["obj:x"])
    patch = diff_scene_facts(left, right)
    assert patch.is_empty
    assert not patch.truncated


def test_diff_deterministik_upserts_removes():
    before = {"c": {"n": 1}, "a": {"n": 1}, "b": {"n": 1}}
    after = {"b": {"n": 2}, "d": {"n": 3}, "a": {"n": 1}}
    first = diff_scene_facts(before, after)
    second = diff_scene_facts(
        {"b": before["b"], "c": before["c"], "a": before["a"]},
        {"d": after["d"], "a": after["a"], "b": after["b"]},
    )
    assert list(first.upserts) == list(second.upserts) == ["b", "d"]
    assert first.removes == second.removes == ("c",)
    assert not first.truncated
    assert first.observed_count == first.sent_count == 3


def test_diff_farm_fixture_upserts_dan_removes():
    before = json.loads(SNAP.read_text(encoding="utf-8"))["payload"]["body"]["facts"]
    after = _after_farm(before)
    patch = diff_scene_facts(before, after)
    assert patch.removes == ("obj:Farm:12,8:(O)472",)
    assert list(patch.upserts) == [
        "npc:Farm:Leah",
        "obj:Farm:20,10:(BC)12",
        "obj:Farm:3,3:(O)388",
    ]
    assert "loc:Farm" not in patch.upserts
    assert "loc:Farm" not in patch.removes
    assert patch.observed_count == 4
    assert not patch.truncated


def test_apply_konvergen_ke_snapshot_segar():
    before = json.loads(SNAP.read_text(encoding="utf-8"))["payload"]["body"]["facts"]
    after = _after_farm(before)
    patch = diff_scene_facts(before, after)
    applied = apply_scene_delta(before, patch)
    assert canonical_json(applied) == canonical_json(after)
    assert diff_scene_facts(applied, after).is_empty


def test_empty_delta_maju_revisi_di_store():
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(snap))).accepted
    rec0 = brain.get(P2, SCENE_DOMAIN)
    assert rec0 is not None
    payload = build_delta_payload(
        session_id=rec0.session_id,
        previous_revision=rec0.domain_revision,
        provenance="ACTIVE_SYNCED",
        authoritative=True,
        patch=diff_scene_facts(rec0.facts, rec0.facts),
    )
    assert payload["domain_revision"] == rec0.domain_revision + 1
    assert payload["body"]["upserts"] == {}
    assert payload["body"]["removes"] == []
    assert payload["truncation"] is None
    assert observation_payload_sah(payload)
    ev = decode_event(json.dumps(_observation(seq=snap["seq"] + 1, payload=payload)))
    assert ev is not None
    assert brain.apply(ev).accepted
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec.domain_revision == rec0.domain_revision + 1
    assert facts_equal(dict(rec.facts), dict(rec0.facts))
    assert rec.is_complete
    assert not rec.stale


def test_snapshot_plus_delta_store_konvergen():
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    delta = json.loads(DELTA.read_text(encoding="utf-8"))
    before = snap["payload"]["body"]["facts"]
    after = _after_farm(before)
    assert observation_payload_sah(delta["payload"])
    assert delta["payload"]["kind"] == "delta"
    assert delta["payload"]["domain_revision"] == snap["payload"]["domain_revision"] + 1
    assert "facts" not in delta["payload"]["body"]

    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(snap))).accepted
    hasil = brain.apply(decode_event(json.dumps(delta)))
    assert hasil.accepted
    assert not hasil.requires_resync
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec is not None
    assert rec.domain_revision == 2
    assert not rec.stale
    assert rec.is_complete
    applied = apply_scene_patch(
        before,
        upserts=delta["payload"]["body"]["upserts"],
        removes=delta["payload"]["body"]["removes"],
    )
    assert facts_equal(dict(rec.facts), applied)
    assert "obj:Farm:12,8:(O)472" not in rec.facts
    assert rec.facts["obj:Farm:20,10:(BC)12"]["minutes_until_ready"] == 10
    assert rec.facts["npc:Farm:Leah"]["tile"]["x"] == 19
    assert "obj:Farm:3,3:(O)388" in rec.facts
    # Kunci patch Python (from snapshot JSON) wajib sama dengan kontrak C#.
    py_patch = diff_scene_facts(before, after)
    assert py_patch.removes == tuple(delta["payload"]["body"]["removes"])
    assert list(py_patch.upserts) == list(delta["payload"]["body"]["upserts"])
    assert brain.query(P2, SCENE_DOMAIN, authoritative=True, complete=True) is rec


def test_celah_revisi_stale_resync():
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(snap))).accepted
    rec0 = brain.get(P2, SCENE_DOMAIN)
    payload = build_delta_payload(
        session_id=rec0.session_id,
        previous_revision=rec0.domain_revision + 1,
        provenance="ACTIVE_SYNCED",
        authoritative=True,
        patch=diff_scene_facts(rec0.facts, _after_farm(dict(rec0.facts))),
    )
    assert payload["domain_revision"] == rec0.domain_revision + 2
    ev = decode_event(json.dumps(_observation(seq=snap["seq"] + 1, payload=payload)))
    hasil = brain.apply(ev)
    assert hasil.rejected
    assert hasil.reason == "revision_gap"
    assert hasil.requires_resync
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec.stale
    assert rec.requires_resync
    assert rec.domain_revision == rec0.domain_revision
    assert "obj:Farm:3,3:(O)388" not in rec.facts
    assert brain.query(P2, SCENE_DOMAIN, complete=True) is None


def test_delta_terpotong_stale_resync():
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(snap))).accepted
    rec0 = brain.get(P2, SCENE_DOMAIN)
    before = dict(rec0.facts)
    after = {f"baru:{i}": {"n": i} for i in range(SCENE_DELTA_MAX_PATCH_OPS + 5)}
    patch = diff_scene_facts(before, after)
    assert patch.truncated
    assert patch.observed_count > patch.sent_count
    payload = build_delta_payload(
        session_id=rec0.session_id,
        previous_revision=rec0.domain_revision,
        provenance="ACTIVE_SYNCED",
        authoritative=True,
        patch=patch,
    )
    assert payload["truncation"]["truncated"] is True
    assert observation_payload_sah(payload)
    ev = decode_event(json.dumps(_observation(seq=snap["seq"] + 1, payload=payload)))
    hasil = brain.apply(ev)
    assert hasil.accepted
    assert hasil.requires_resync
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec.stale
    assert rec.requires_resync
    assert rec.domain_revision == rec0.domain_revision + 1
    # Hitungan patch tidak menimpa kelengkapan snapshot.
    assert rec.observed_count == rec0.observed_count
    assert rec.sent_count == rec0.sent_count
    assert rec.is_complete is False
    leftover = diff_scene_facts(rec.facts, after)
    assert not leftover.is_empty


def test_session_mismatch_delta_ditolak():
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(snap))).accepted
    payload = build_delta_payload(
        session_id="obs:session:LAIN:1",
        previous_revision=1,
        provenance="ACTIVE_SYNCED",
        authoritative=True,
        patch=diff_scene_facts({"a": 1}, {"a": 2}),
    )
    ev = decode_event(json.dumps(_observation(seq=snap["seq"] + 1, payload=payload)))
    hasil = brain.apply(ev)
    assert hasil.rejected
    assert hasil.reason == "session_mismatch"
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec.session_id == snap["payload"]["session_id"]
    assert rec.domain_revision == 1


def test_delta_tidak_membuka_session():
    brain = RichObservationStore()
    payload = build_delta_payload(
        session_id="obs:baru:1:1",
        previous_revision=0,
        provenance="ACTIVE_SYNCED",
        authoritative=True,
        patch=diff_scene_facts({}, {"a": {"n": 1}}),
    )
    ev = decode_event(json.dumps(_observation(seq=1, payload=payload)))
    hasil = brain.apply(ev)
    assert hasil.rejected
    assert hasil.reason == "revision_gap"
    assert hasil.requires_resync
    assert brain.get(P2, SCENE_DOMAIN) is None


def test_motor_state_tidak_tercerna_delta():
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    delta = json.loads(DELTA.read_text(encoding="utf-8"))
    motor = StardewStateStore()
    hello = decode_event(json.dumps({
        "protocol": 1, "ev": "hello", "seq": 1,
        "player_id": P2, "screen_id": 1,
        "player_name": "Arti", "is_main_player": False, "is_split_screen": False,
    }))
    state = decode_event(json.dumps({
        "protocol": 1, "ev": "state", "seq": 2,
        "player_id": P2, "screen_id": 1,
        "payload": {
            "location": "Farm",
            "tile": {"x": 42, "y": 17},
            "pixel": {"x": 2688.0, "y": 1088.0},
            "facing": 2,
            "time_of_day": 610,
            "date": {"season": "spring", "day": 1, "year": 1},
            "energy": 270.0,
            "health": 100,
            "is_player_free": True,
            "can_player_move": True,
            "active_menu": None,
        },
    }))
    motor.catat(hello)
    motor.catat(state)
    motor.catat(decode_event(json.dumps(snap)))
    motor.catat(decode_event(json.dumps(delta)))
    latest = motor.latest(P2)
    assert latest is not None
    assert latest.seq == 2
    assert latest.location == "Farm"
    assert not hasattr(latest, "facts")
    assert not hasattr(latest, "upserts")


def test_max_line_bytes_dan_cap_tidak_naik():
    assert MAX_LINE_BYTES == 64 * 1024
    assert SCENE_DELTA_MAX_PATCH_OPS == 48
    src = CS_DELTA.read_text(encoding="utf-8")
    assert "MaxPatchOps = 48" in src
    assert "ReceiverMaxLineBytes = SceneObservationCaps.ReceiverMaxLineBytes" in src
    receiver = RECEIVER.read_text(encoding="utf-8")
    assert "MAX_LINE_BYTES = 64 * 1024" in receiver


def test_runtime_hanya_wiring_objectlistchanged_current_location():
    entry = MOD_ENTRY.read_text(encoding="utf-8")
    assert (
        "helper.Events.World.ObjectListChanged += this.OnObjectListChanged;" in entry
    )
    assert "ObjectListChangedEventArgs e" in entry
    assert "e.IsCurrentLocation" in entry
    assert "e.Added" in entry
    assert "e.Removed" in entry
    assert "SceneSnapshotCapture.CaptureObject" in entry
    assert "TryBuildDelta" in entry
    assert "ObservationKind.Delta" in entry
    assert "RequestOverflowSnapshot" in entry
    assert entry.count("Deltas.Invalidate()") >= 3
    assert "ReserveDeltaRevision" in entry
    assert "screen.Deltas.RequiresSnapshot" in entry
    assert "if (!ok && sent.Kind == ObservationKind.Delta)" in entry
    assert "screen.AbandonClient()" in entry
    assert "SnapshotNeeded" in entry
