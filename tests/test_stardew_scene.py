"""OBS-1: snapshot scene.current_location di atas kontrak OBS-0."""

from __future__ import annotations

import json
from pathlib import Path

from arti_stardew import StardewStateStore, decode_event
from arti_stardew_observation import RichObservationStore, observation_payload_sah
from arti_stardew_scene import (
    FACT_CLUMP,
    FACT_DOOR,
    FACT_LOCATION,
    FACT_MONSTER,
    FACT_NPC,
    FACT_OBJECT,
    FACT_TERRAIN,
    FACT_WARP,
    SCENE_DOMAIN,
    facts_of_kind,
    is_ephemeral_monster_id,
)

ROOT = Path(__file__).resolve().parents[1]
P2 = -100000000000000001
KONTRAK = ROOT / "tests" / "data" / "stardew_scene_snapshot_contract.json"


def test_kontrak_scene_obs1_masuk_store_authoritative_complete():
    raw = json.loads(KONTRAK.read_text(encoding="utf-8"))
    assert raw["ev"] == "observation"
    payload = raw["payload"]
    assert observation_payload_sah(payload)
    assert payload["product"] == "scene"
    assert payload["domain"] == SCENE_DOMAIN
    assert payload["kind"] == "snapshot"
    assert payload["available"] is True
    assert payload["error"] is None
    assert payload["truncation"]["truncated"] is False
    facts = payload["body"]["facts"]
    assert payload["truncation"]["sent_count"] == len(facts)
    assert FACT_LOCATION in {facts[k]["kind"] for k in facts}

    loc = facts_of_kind(facts, FACT_LOCATION)
    assert "loc:Farm" in loc
    assert loc["loc:Farm"]["location_id"] == "Farm"
    assert loc["loc:Farm"]["map_width"] == 80
    assert "tiles" not in loc["loc:Farm"]

    objek = facts_of_kind(facts, FACT_OBJECT)
    keg = objek["obj:Farm:20,10:(BC)12"]
    assert keg["qualified_item_id"] == "(BC)12"
    assert keg["held_item"]["qualified_item_id"] == "(O)346"
    assert "modData" not in keg

    terrain = facts_of_kind(facts, FACT_TERRAIN)
    assert terrain["tf:Farm:12,9:HoeDirt"]["crop"]["watered"] is True

    npc = facts_of_kind(facts, FACT_NPC)
    assert npc["npc:Farm:Leah"]["internal_name"] == "Leah"

    monster = facts_of_kind(facts, FACT_MONSTER)
    assert is_ephemeral_monster_id("mon:Farm:eph-1")
    assert monster["mon:Farm:eph-1"]["monster"]["ephemeral"] is True

    assert facts_of_kind(facts, FACT_WARP)
    assert facts_of_kind(facts, FACT_DOOR)
    assert facts_of_kind(facts, FACT_CLUMP)

    ev = decode_event(json.dumps(raw))
    assert ev is not None
    brain = RichObservationStore()
    assert brain.apply(ev).accepted
    rec = brain.query(P2, SCENE_DOMAIN, authoritative=True, complete=True)
    assert rec is not None
    assert rec.authoritative
    assert rec.is_complete
    assert rec.provenance == "ACTIVE_SYNCED"


def test_duplicate_semantic_id_truncated_bukan_complete():
    """Duplikat ID tidak boleh klaim truncated=false saat observed != sent."""
    facts = {
        "loc:Farm": {
            "kind": FACT_LOCATION,
            "location_id": "Farm",
            "location_kind": "Farm",
            "is_outdoors": True,
            "map_width": 80,
            "map_height": 65,
        },
        "npc:Farm:Leah": {
            "kind": FACT_NPC,
            "internal_name": "Leah",
            "display_name": "first",
        },
    }
    palsu = {
        "session_id": "obs:demo:1:1",
        "product": "scene",
        "domain": SCENE_DOMAIN,
        "kind": "snapshot",
        "domain_revision": 1,
        "provenance": "ACTIVE_SYNCED",
        "authoritative": True,
        "available": True,
        "error": None,
        "truncation": {
            "truncated": False,
            "observed_count": 3,
            "sent_count": 2,
        },
        "body": {"facts": facts},
    }
    assert observation_payload_sah(palsu) is False

    sah = dict(palsu)
    sah["truncation"] = {
        "truncated": True,
        "observed_count": 3,
        "sent_count": 2,
    }
    assert observation_payload_sah(sah)
    assert sah["truncation"]["sent_count"] == len(facts)
    envelope = {
        "protocol": 1,
        "ev": "observation",
        "seq": 8,
        "player_id": P2,
        "screen_id": 1,
        "payload": sah,
    }
    ev = decode_event(json.dumps(envelope))
    assert ev is not None
    brain = RichObservationStore()
    assert brain.apply(ev).accepted
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec is not None
    assert rec.is_complete is False
    assert brain.query(P2, SCENE_DOMAIN, complete=True) is None


def test_scene_truncated_tidak_complete_di_store():
    raw = json.loads(KONTRAK.read_text(encoding="utf-8"))
    payload = dict(raw["payload"])
    payload["truncation"] = {
        "truncated": True,
        "observed_count": 40,
        "sent_count": len(payload["body"]["facts"]),
    }
    payload["domain_revision"] = 9
    envelope = {
        "protocol": 1,
        "ev": "observation",
        "seq": 8,
        "player_id": P2,
        "screen_id": 1,
        "payload": payload,
    }
    ev = decode_event(json.dumps(envelope))
    assert ev is not None
    brain = RichObservationStore()
    assert brain.apply(ev).accepted
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec is not None
    assert rec.is_complete is False
    assert brain.query(P2, SCENE_DOMAIN, complete=True) is None
    assert brain.query(P2, SCENE_DOMAIN, authoritative=True, complete=True) is None


def test_scene_shadow_tidak_authoritative():
    raw = json.loads(KONTRAK.read_text(encoding="utf-8"))
    payload = dict(raw["payload"])
    payload["provenance"] = "SHADOW_STALE_POSSIBLE"
    payload["authoritative"] = False
    envelope = {
        "protocol": 1,
        "ev": "observation",
        "seq": 8,
        "player_id": P2,
        "screen_id": 1,
        "payload": payload,
    }
    ev = decode_event(json.dumps(envelope))
    assert ev is not None
    brain = RichObservationStore()
    assert brain.apply(ev).accepted
    rec = brain.get(P2, SCENE_DOMAIN)
    assert rec.authoritative is False
    assert rec.provenance == "SHADOW_STALE_POSSIBLE"
    assert brain.query(P2, SCENE_DOMAIN, authoritative=True) is None


def test_motor_state_tetap_terpisah_dari_scene():
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
    obs = decode_event(KONTRAK.read_text(encoding="utf-8"))
    motor.catat(hello)
    motor.catat(state)
    motor.catat(obs)
    snap = motor.latest(P2)
    assert snap is not None
    assert snap.seq == 2
    assert snap.location == "Farm"
    assert not hasattr(snap, "facts")


# ------------------------------------------------------------------ WS1 gather window tool keys

def _gather_record(**window_changes):
    import time
    from types import SimpleNamespace
    window = dict(kind="gather_window", available=True, reason=None, location_id="Farm", captured_ticks=5,
                  center=dict(x=3, y=8), radius=1, targets={}, drops=[], drops_complete=True,
                  using_tool=False, last_use=None)
    window.update(window_changes)
    return SimpleNamespace(available=True, authoritative=True, stale=False, requires_resync=False,
                           session_id="s", screen_id=0, received_monotonic=time.monotonic(),
                           facts={"gather:nearby": window})


def _tiles(**override):
    tiles = [dict(x=x, y=y, tillable=False, water_refill=False, villager=False) for y in (7, 8, 9) for x in (2, 3, 4)]
    tiles[0].update(override)
    return tiles


def test_gather_window_ws1_tool_keys_valid_absent_and_malformed():
    from arti_stardew_scene import gather_window
    check = lambda **kw: gather_window(_gather_record(**kw), session_id="s", screen_id=0, location_id="Farm")
    assert check() is not None  # older mod: keys absent is fine
    assert check(tool_tile=dict(x=3, y=9), tiles=_tiles(tillable=True)) is not None
    assert check(tool_tile=dict(x=5, y=8)) is not None  # outer 8 px band: two tiles away keeps the window
    for bad in (dict(tool_tile=dict(x=6, y=8)), dict(tool_tile=dict(x="3", y=9)), dict(tool_tile=[3, 9]),
                dict(tiles=_tiles() + [dict(x=3, y=8, tillable=False, water_refill=False, villager=False)]),
                dict(tiles=[dict(x=3, y=8, tillable=False, water_refill=False, villager=False)] * 2),
                dict(tiles=[dict(x=5, y=8, tillable=False, water_refill=False, villager=False)]),
                dict(tiles=_tiles(tillable=1)), dict(tiles=_tiles(villager=None)), dict(tiles={"x": 1})):
        assert check(**bad) is None, bad  # malformed fails the whole window closed
