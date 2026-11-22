"""G3-1: ui.text current + bounded history on the existing OBS-0 store.

UNIT_TESTED / CLOUD_VERIFIED. Not LOCAL_VERIFIED. Not live Stardew.
X04 chat-focus is DEFERRED and is not claimed here.
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
OBS_SESSION = "obs:demo:1:1"
CMD_SESSION = "cmdinbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
UI_TEXT = "ui.text"
UI_CTX = "ui.context"


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


def _occ(seq: int, text: str, *, status: str = "active", revision: int = 1, replayed: bool = False) -> dict:
    return {
        "occurrence_seq": seq,
        "family": "dialogue",
        "instance_token": "mi1" if status == "active" else "mi0",
        "page_type": "DialogueBox",
        "source": "DialogueBox.getCurrentString",
        "text": text,
        "text_revision": revision,
        "reveal_status": "full",
        "status": status,
        "complete": True,
        "replayed": replayed,
        "actionable": False,
        "first_capture_ticks": 10,
        "last_capture_ticks": 12,
        "origin_occurrence_seq": seq,
        "speaker": "Robin",
        "options": [{"index": 0, "label": "Ya", "selected": True}],
    }


def _ui_text(
    *,
    seq: int = 3,
    revision: int = 1,
    session: str = OBS_SESSION,
    kind: str = "snapshot",
    facts: dict | None = None,
    upserts: dict | None = None,
    removes: list | None = None,
    truncated: bool = False,
    observed: int | None = None,
    sent: int | None = None,
) -> dict:
    if kind == "snapshot":
        body_facts = facts if facts is not None else {
            "binding": {
                "runtime_instance_id": "runtime-g3-001",
                "session_id": CMD_SESSION,
                "player_id": P2,
                "screen_id": 0,
                "location_id": "Farm",
                "context_generation": 1,
                "ui_revision": 1,
                "menu_instance_token": "mi1",
                "capture_ticks": 10,
            },
            "meta": {
                "cursor_occurrence_seq": 1,
                "floor_lost_before_seq": 1,
                "dropped_count": 0,
                "replayed_history": False,
                "active_seqs": [1],
            },
            "occ:1": _occ(1, "Halo farmhand."),
        }
        sent_count = sent if sent is not None else len(body_facts)
        observed_count = observed if observed is not None else sent_count
        body: dict = {"facts": body_facts}
    else:
        body = {}
        if upserts is not None:
            body["upserts"] = upserts
        if removes is not None:
            body["removes"] = removes
        sent_count = sent if sent is not None else len(upserts or {}) + len(removes or [])
        observed_count = observed if observed is not None else sent_count
    return {
        "protocol": 1,
        "ev": "observation",
        "seq": seq,
        "player_id": P2,
        "screen_id": 0,
        "payload": {
            "session_id": session,
            "product": "ui",
            "domain": UI_TEXT,
            "kind": kind,
            "domain_revision": revision,
            "provenance": "LOCAL_AUTHORITATIVE",
            "authoritative": True,
            "available": True,
            "error": None,
            "truncation": {
                "truncated": truncated,
                "observed_count": observed_count,
                "sent_count": sent_count,
            },
            "body": body,
        },
    }


def test_known_pair_ui_text_additive():
    assert UI_TEXT in KNOWN_DOMAINS
    assert ("ui", UI_TEXT) in KNOWN_OBSERVATION_PAIRS
    assert ("ui", UI_CTX) in KNOWN_OBSERVATION_PAIRS
    assert ("ui", "ui.active_menu") in KNOWN_OBSERVATION_PAIRS


def test_store_accepts_ui_text_snapshot_and_session_split():
    motor = StardewStateStore()
    brain = RichObservationStore()
    motor.catat(decode_event(json.dumps(_hello())))
    motor.catat(decode_event(json.dumps(_state())))
    ev = decode_event(json.dumps(_ui_text(), ensure_ascii=False))
    assert ev is not None
    assert observation_payload_sah(ev["payload"])
    hasil = brain.apply(ev)
    assert hasil.accepted, hasil
    rec = brain.get(P2, UI_TEXT)
    assert rec is not None
    assert rec.session_id == OBS_SESSION
    assert rec.facts["binding"]["session_id"] == CMD_SESSION
    assert rec.facts["binding"]["capture_ticks"] == 10
    assert rec.facts["occ:1"]["actionable"] is False
    assert rec.seq == 3
    assert rec.facts["binding"]["capture_ticks"] != rec.seq
    assert motor.latest(P2) is not None


def test_delta_requires_valid_base_gap_resync():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_ui_text()))).accepted
    delta = decode_event(json.dumps(_ui_text(
        seq=4,
        revision=2,
        kind="delta",
        upserts={"occ:1": _occ(1, "Halo farmhand.", revision=2)},
    )))
    assert brain.apply(delta).accepted
    rec = brain.get(P2, UI_TEXT)
    assert rec.domain_revision == 2
    assert rec.facts["occ:1"]["text_revision"] == 2

    gap = decode_event(json.dumps(_ui_text(
        seq=5,
        revision=9,
        kind="delta",
        upserts={"occ:2": _occ(2, "lain")},
    )))
    hasil = brain.apply(gap)
    assert hasil.rejected
    assert hasil.requires_resync
    stale = brain.get(P2, UI_TEXT)
    assert stale.requires_resync


def test_unknown_pair_still_ignored():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_ui_text()))).accepted
    asing = decode_event(json.dumps(_ui_text(seq=4)))
    asing["payload"] = dict(asing["payload"])
    asing["payload"]["domain"] = "ui.quantum_2027"
    hasil = brain.apply(asing)
    assert hasil.ignored
    assert hasil.reason == "unknown_domain"
    assert brain.get(P2, UI_TEXT) is not None
    assert brain.get(P2, "ui.quantum_2027") is None


def test_unicode_backslash_oversize_actual_encoded_bytes():
    facts = {
        "binding": {
            "runtime_instance_id": "runtime-g3-001",
            "session_id": CMD_SESSION,
            "player_id": P2,
            "screen_id": 0,
            "location_id": "Farm",
            "context_generation": 1,
            "ui_revision": 1,
            "menu_instance_token": "mi1",
            "capture_ticks": 10,
        },
        "meta": {
            "cursor_occurrence_seq": 1,
            "floor_lost_before_seq": 1,
            "dropped_count": 0,
            "replayed_history": False,
            "active_seqs": [1],
        },
        "occ:1": _occ(1, 'Halo 👋 "farmhand" \\path'),
    }
    ev = decode_event(json.dumps(_ui_text(facts=facts), ensure_ascii=False))
    assert ev is not None
    brain = RichObservationStore()
    assert brain.apply(ev).accepted
    rec = brain.get(P2, UI_TEXT)
    assert rec.facts["occ:1"]["text"] == 'Halo 👋 "farmhand" \\path'
    line = json.dumps(_ui_text(facts=facts), ensure_ascii=False) + "\n"
    encoded = line.encode("utf-8")
    assert len(encoded) <= MAX_LINE_BYTES
    assert MAX_LINE_BYTES == 64 * 1024
    payload = json.dumps(_ui_text(facts=facts)["payload"], ensure_ascii=False).encode("utf-8")
    assert len(payload) <= 32 * 1024


def test_expired_history_is_not_actionable_and_replay_does_not_retrigger():
    brain = RichObservationStore()
    snap = _ui_text()
    assert brain.apply(decode_event(json.dumps(snap))).accepted
    replay = _ui_text(seq=4, revision=1, session="obs:demo:2:1", facts={
        "binding": {
            "runtime_instance_id": "runtime-g3-001",
            "session_id": "cmdinbox-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "player_id": P2,
            "screen_id": 0,
            "location_id": "Farm",
            "context_generation": 2,
            "ui_revision": 1,
            "menu_instance_token": "mi2",
            "capture_ticks": 90,
        },
        "meta": {
            "cursor_occurrence_seq": 1,
            "floor_lost_before_seq": 1,
            "dropped_count": 0,
            "replayed_history": True,
            "active_seqs": [],
        },
        "occ:1": _occ(1, "Halo farmhand.", status="expired", replayed=True),
    })
    assert brain.apply(decode_event(json.dumps(replay))).accepted
    rec = brain.get(P2, UI_TEXT)
    assert rec.session_id == "obs:demo:2:1"
    assert rec.facts["meta"]["replayed_history"] is True
    assert rec.facts["occ:1"]["actionable"] is False
    assert rec.facts["occ:1"]["replayed"] is True


def test_x04_chat_not_claimed():
    blob = json.dumps(_ui_text(), ensure_ascii=False)
    assert '"family": "chat"' not in blob
    assert "hud_messages" not in blob
    assert "chat_box" not in blob
    assert "letter_viewer" not in blob
    assert "mailMessage" not in blob


def test_complete_false_truncated_payload_accepted():
    facts = {
        "binding": {
            "runtime_instance_id": "runtime-g3-001",
            "session_id": CMD_SESSION,
            "player_id": P2,
            "screen_id": 0,
            "location_id": "Farm",
            "context_generation": 1,
            "ui_revision": 1,
            "menu_instance_token": "mi1",
            "capture_ticks": 10,
        },
        "meta": {
            "cursor_occurrence_seq": 1,
            "floor_lost_before_seq": 1,
            "dropped_count": 0,
            "replayed_history": False,
            "active_seqs": [1],
        },
        "occ:1": {
            **_occ(1, "Halo " + ("文" * 20)),
            "complete": False,
            "options": [{"index": i, "label": f"{i:03d}-x", "selected": False} for i in range(8)],
        },
    }
    ev = decode_event(json.dumps(_ui_text(facts=facts), ensure_ascii=False))
    assert ev is not None
    brain = RichObservationStore()
    hasil = brain.apply(ev)
    assert hasil.accepted, hasil
    rec = brain.get(P2, UI_TEXT)
    assert rec.facts["occ:1"]["complete"] is False
    assert rec.facts["occ:1"]["actionable"] is False
    line = json.dumps(_ui_text(facts=facts), ensure_ascii=False) + "\n"
    assert len(line.encode("utf-8")) <= MAX_LINE_BYTES


def test_multibyte_store_roundtrip_no_replacement():
    text = "Halo 👋 " + ("文" * 40)
    facts = {
        "binding": {
            "runtime_instance_id": "runtime-g3-001",
            "session_id": CMD_SESSION,
            "player_id": P2,
            "screen_id": 0,
            "location_id": "Farm",
            "context_generation": 1,
            "ui_revision": 1,
            "menu_instance_token": "mi1",
            "capture_ticks": 10,
        },
        "meta": {
            "cursor_occurrence_seq": 1,
            "floor_lost_before_seq": 1,
            "dropped_count": 0,
            "replayed_history": False,
            "active_seqs": [1],
        },
        "occ:1": _occ(1, text),
    }
    ev = decode_event(json.dumps(_ui_text(facts=facts), ensure_ascii=False))
    brain = RichObservationStore()
    assert brain.apply(ev).accepted
    rec = brain.get(P2, UI_TEXT)
    got = rec.facts["occ:1"]["text"]
    assert got == text
    assert "\ufffd" not in got
    assert text.startswith(got)
