"""G3-0: current-only ui.context on the existing OBS-0 store.

UNIT_TESTED / CLOUD_VERIFIED. Not LOCAL_VERIFIED. Not live Stardew.
"""

from __future__ import annotations

import json

from arti_stardew import MAX_LINE_BYTES, StardewStateStore, decode_event
from arti_stardew_observation import (
    KNOWN_DOMAINS,
    KNOWN_OBSERVATION_PAIRS,
    RichObservationStore,
    observation_payload_sah,
)

P2 = -100000000000000001
SESSION = "obs:demo:1:1"
UI_CTX = "ui.context"
SCENE = "scene.current_location"


def _hello(*, seq: int = 1) -> dict:
    return {
        "protocol": 1,
        "ev": "hello",
        "seq": seq,
        "player_id": P2,
        "screen_id": 0,
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
        "screen_id": 0,
        "payload": {
            "location": "Farm",
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
        },
    }


def _g2_facts(*, visible: str = "Halo farmhand.") -> dict:
    return {
        "runtime_instance_id": "runtime-g3-001",
        "context_generation": 1,
        "session_id": SESSION,
        "player_id": P2,
        "screen_id": 0,
        "ui_revision": 1,
        "viewport_transform_revision": 1,
        "location_id": "Farm",
        "mode": "menu",
        "is_player_free": True,
        "can_player_move": True,
        "capabilities": {
            "inspect_tiles": {"available": True},
            "inspect_context": {"available": True},
            "text_edit": {"available": False, "reason": "subscriber_present_backend_not_implemented"},
        },
        "interactive": {
            "receiver_token": "rcv:mi1",
            "page_token": "pg:mi1:DialogueBox",
            "menu_type": "DialogueBox",
            "page_type": "DialogueBox",
            "snap_id": 42,
            "focus_key": "dlg:42",
            "focus_target_token": "tgt:mi1:dlg:42",
            "visible_text": visible,
            "speaker": "Robin",
            "text_input_token": "txt:ti1",
        },
    }


def _ui_context(*, seq: int = 3, revision: int = 1, session: str = SESSION, facts: dict | None = None) -> dict:
    body_facts = facts if facts is not None else _g2_facts()
    return {
        "protocol": 1,
        "ev": "observation",
        "seq": seq,
        "player_id": P2,
        "screen_id": 0,
        "payload": {
            "session_id": session,
            "product": "ui",
            "domain": UI_CTX,
            "kind": "snapshot",
            "domain_revision": revision,
            "provenance": "LOCAL_AUTHORITATIVE",
            "authoritative": True,
            "available": True,
            "error": None,
            "truncation": {
                "truncated": False,
                "observed_count": len(body_facts),
                "sent_count": len(body_facts),
            },
            "body": {"facts": body_facts},
        },
    }


def test_known_pair_ui_context_only_for_g3():
    assert UI_CTX in KNOWN_DOMAINS
    assert ("ui", UI_CTX) in KNOWN_OBSERVATION_PAIRS
    assert ("ui", "ui.active_menu") in KNOWN_OBSERVATION_PAIRS
    assert ("ui", "ui.text") in KNOWN_OBSERVATION_PAIRS
    assert "ui.text" in KNOWN_DOMAINS


def test_store_accepts_ui_context_snapshot_and_session_rules():
    motor = StardewStateStore()
    brain = RichObservationStore()
    motor.catat(decode_event(json.dumps(_hello())))
    motor.catat(decode_event(json.dumps(_state())))
    ev = decode_event(json.dumps(_ui_context()))
    assert ev is not None
    assert observation_payload_sah(ev["payload"])
    hasil = brain.apply(ev)
    assert hasil.accepted, hasil
    rec = brain.get(P2, UI_CTX)
    assert rec is not None
    assert rec.session_id == SESSION
    assert rec.domain_revision == 1
    assert rec.facts["runtime_instance_id"] == "runtime-g3-001"
    assert rec.facts["interactive"]["text_input_token"] == "txt:ti1"
    assert rec.facts["viewport_transform_revision"] == 1
    assert motor.latest(P2) is not None
    assert motor.latest(P2).location == "Farm"

    lagi = decode_event(json.dumps(_ui_context(seq=4, revision=2)))
    assert brain.apply(lagi).accepted
    assert brain.get(P2, UI_CTX).domain_revision == 2

    sesi_baru = decode_event(json.dumps(_ui_context(seq=5, revision=1, session="obs:demo:2:1")))
    assert brain.apply(sesi_baru).accepted
    assert brain.get(P2, UI_CTX).session_id == "obs:demo:2:1"


def test_unknown_pair_still_ignored():
    brain = RichObservationStore()
    ok = decode_event(json.dumps(_ui_context()))
    assert brain.apply(ok).accepted
    asing = decode_event(json.dumps(_ui_context(seq=4)))
    asing["payload"] = dict(asing["payload"])
    asing["payload"]["domain"] = "ui.quantum_2027"
    hasil = brain.apply(asing)
    assert hasil.ignored
    assert hasil.reason == "unknown_domain"
    assert brain.get(P2, UI_CTX) is not None
    assert brain.get(P2, "ui.quantum_2027") is None

    campuran = decode_event(json.dumps(_ui_context(seq=5)))
    campuran["payload"] = dict(campuran["payload"])
    campuran["payload"]["product"] = "scene"
    hasil2 = brain.apply(campuran)
    assert hasil2.ignored
    assert brain.get(P2, SCENE) is None

    teks = decode_event(json.dumps(_ui_context(seq=6)))
    teks["payload"] = dict(teks["payload"])
    teks["payload"]["domain"] = "ui.recent_history"
    assert brain.apply(teks).ignored


def test_unicode_visible_text_accepted():
    facts = _g2_facts(visible='Halo 👋 "farmhand" \nRobin')
    ev = decode_event(json.dumps(_ui_context(facts=facts)))
    assert ev is not None
    brain = RichObservationStore()
    assert brain.apply(ev).accepted
    rec = brain.get(P2, UI_CTX)
    assert rec.facts["interactive"]["visible_text"] == 'Halo 👋 "farmhand" \nRobin'


def test_encoded_envelope_under_64kib():
    line = json.dumps(_ui_context(), ensure_ascii=False) + "\n"
    assert len(line.encode("utf-8")) <= MAX_LINE_BYTES
    assert MAX_LINE_BYTES == 64 * 1024


def test_no_hud_chat_letter_fields():
    blob = json.dumps(_ui_context())
    assert "hud_messages" not in blob
    assert "chat_box" not in blob
    assert "letter_viewer" not in blob
    assert "occurrence_ring" not in blob
