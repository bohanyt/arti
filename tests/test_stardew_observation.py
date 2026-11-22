"""OBS-0: kontrak BRAIN_OBSERVATION, terpisah dari MOTOR_STATE."""

from __future__ import annotations

import json
from pathlib import Path

from arti_stardew import StardewStateStore, decode_event, format_cli_ringkasan
from arti_stardew_observation import (
    KNOWN_DOMAINS,
    KNOWN_OBSERVATION_PAIRS,
    KNOWN_PRODUCTS,
    LIVE_AUTHORITATIVE,
    PROVENANCE_VALUES,
    RichObservationStore,
    observation_payload_sah,
)

ROOT = Path(__file__).resolve().parents[1]
P2 = -100000000000000001
SESSION = "save:demo:epoch:1"
DOMAIN = "scene.current_location"


def _payload_state(**timpa: object) -> dict:
    payload = {
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
    }
    payload.update(timpa)
    return payload


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


def _state(*, seq: int = 2, **timpa: object) -> dict:
    return {
        "protocol": 1,
        "ev": "state",
        "seq": seq,
        "player_id": P2,
        "screen_id": 1,
        "payload": _payload_state(**timpa),
    }


def _obs_payload(**timpa: object) -> dict:
    payload = {
        "session_id": SESSION,
        "product": "scene",
        "domain": DOMAIN,
        "kind": "snapshot",
        "domain_revision": 1,
        "provenance": "ACTIVE_SYNCED",
        "authoritative": True,
        "available": True,
        "error": None,
        "truncation": {
            "truncated": False,
            "observed_count": 1,
            "sent_count": 1,
        },
        "body": {"facts": {"obj:Farm:12,8": {"n": 1}}},
    }
    payload.update(timpa)
    return payload


def _observation(*, seq: int = 3, **payload_timpa: object) -> dict:
    return {
        "protocol": 1,
        "ev": "observation",
        "seq": seq,
        "player_id": P2,
        "screen_id": 1,
        "payload": _obs_payload(**payload_timpa),
    }


def test_motor_lama_tetap_parse():
    assert decode_event(json.dumps(_hello())) is not None
    assert decode_event(json.dumps(_state())) is not None
    # Payload Gate 5 tanpa field observation tetap sah.
    ev = decode_event(json.dumps(_state(seq=2)))
    assert ev is not None
    assert "session_id" not in ev["payload"]
    assert "provenance" not in ev["payload"]


def test_field_wajib_motor_tidak_ikut_longgar():
    rusak = _state()
    del rusak["payload"]["location"]
    assert decode_event(json.dumps(rusak)) is None


def test_unknown_rich_domain_diabaikan_aman():
    motor = StardewStateStore()
    brain = RichObservationStore()
    motor.catat(decode_event(json.dumps(_hello())))
    motor.catat(decode_event(json.dumps(_state(seq=2, tile={"x": 5, "y": 6}))))
    assert brain.apply(decode_event(json.dumps(_observation(seq=3)))).accepted
    assert brain.get(P2, DOMAIN) is not None

    asing = decode_event(json.dumps(_observation(
        seq=4,
        domain="scene.quantum_2027",
        body={"facts": {"x": {"ok": True}}},
    )))
    assert asing is not None
    motor.catat(asing)
    hasil = brain.apply(asing)
    assert hasil.ignored
    assert hasil.reason == "unknown_domain"
    assert motor.latest(P2) is not None
    assert motor.latest(P2).tile == (5, 6)
    assert brain.get(P2, DOMAIN) is not None
    assert not brain.get(P2, DOMAIN).stale
    assert brain.get(P2, "scene.quantum_2027") is None

    produk_asing = decode_event(json.dumps(_observation(
        seq=5,
        product="weather",
        domain=DOMAIN,
        body={"facts": {"x": 1}},
    )))
    assert produk_asing is not None
    assert brain.apply(produk_asing).ignored
    assert brain.get(P2, DOMAIN).domain_revision == 1

    masa_depan = decode_event(json.dumps({
        "protocol": 1, "ev": "future_obs_2027", "seq": 6,
        "player_id": P2, "screen_id": 1,
        "payload": {"apa_saja": True},
    }))
    assert masa_depan is not None
    motor.catat(masa_depan)
    hasil2 = brain.apply(masa_depan)
    assert hasil2.ignored
    assert motor.latest(P2).tile == (5, 6)


def test_unknown_kind_diabaikan_tanpa_menandai_stale():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(seq=3)))).accepted
    aneh = decode_event(json.dumps(_observation(
        seq=4, kind="quantum_patch", domain_revision=2,
    )))
    assert aneh is not None
    hasil = brain.apply(aneh)
    assert hasil.ignored
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.domain_revision == 1
    assert not rec.stale


def test_snapshot_lalu_delta_konvergen():
    brain = RichObservationStore()
    snap = decode_event(json.dumps(_observation(
        seq=3,
        domain_revision=4,
        body={"facts": {"a": {"n": 1}, "b": {"n": 2}}},
        truncation={"truncated": False, "observed_count": 2, "sent_count": 2},
    )))
    delta = decode_event(json.dumps(_observation(
        seq=4,
        kind="delta",
        domain_revision=5,
        body={"upserts": {"b": {"n": 3}, "c": {"n": 4}}, "removes": ["a"]},
        truncation={"truncated": False, "observed_count": 2, "sent_count": 2},
    )))
    assert brain.apply(snap).accepted
    assert brain.apply(delta).accepted
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.domain_revision == 5
    assert dict(rec.facts) == {"b": {"n": 3}, "c": {"n": 4}}
    assert rec.is_complete
    assert not rec.stale
    assert not brain.requires_resync(P2, DOMAIN)


def test_celah_revisi_menandai_stale_dan_resync():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(
        seq=3, domain_revision=1,
    )))).accepted
    gap = brain.apply(decode_event(json.dumps(_observation(
        seq=4, kind="delta", domain_revision=3,
        body={"upserts": {"z": 1}},
    ))))
    assert gap.rejected
    assert gap.reason == "revision_gap"
    assert gap.requires_resync
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.stale
    assert rec.requires_resync
    assert rec.domain_revision == 1
    assert "z" not in rec.facts
    assert brain.query(P2, DOMAIN, authoritative=True) is None
    assert brain.query(P2, DOMAIN, complete=True) is None


def test_non_authoritative_tidak_memenuhi_query_authoritative():
    brain = RichObservationStore()
    assert observation_payload_sah(_obs_payload(
        provenance="SHADOW_STALE_POSSIBLE",
        authoritative=True,
    )) is False

    shadow = decode_event(json.dumps(_observation(
        seq=3,
        provenance="SHADOW_STALE_POSSIBLE",
        authoritative=False,
    )))
    assert shadow is not None
    assert brain.apply(shadow).accepted
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.provenance == "SHADOW_STALE_POSSIBLE"
    assert rec.authoritative is False
    assert brain.query(P2, DOMAIN) is rec
    assert brain.query(P2, DOMAIN, authoritative=True) is None

    derived = decode_event(json.dumps(_observation(
        seq=4,
        domain="progress.player",
        product="progress",
        provenance="DERIVED",
        authoritative=False,
        body={"facts": {"xp": 10}},
    )))
    assert brain.apply(derived).accepted
    assert brain.query(P2, "progress.player", authoritative=True) is None


def test_truncation_eksplisit_bukan_snapshot_lengkap():
    brain = RichObservationStore()
    potong = decode_event(json.dumps(_observation(
        seq=3,
        truncation={"truncated": True, "observed_count": 10, "sent_count": 2},
        body={"facts": {"a": 1, "b": 2}},
    )))
    assert potong is not None
    assert brain.apply(potong).accepted
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.truncated is True
    assert rec.observed_count == 10
    assert rec.sent_count == 2
    assert rec.is_complete is False
    assert brain.query(P2, DOMAIN, complete=True) is None
    # Data potongan masih bisa dibaca, tapi tidak menyamar sebagai lengkap.
    assert brain.query(P2, DOMAIN) is rec

    assert decode_event(json.dumps(_observation(
        seq=4,
        truncation={"truncated": False, "observed_count": 10, "sent_count": 2},
    ))) is None
    assert decode_event(json.dumps(_observation(
        seq=5,
        truncation={"truncated": True, "observed_count": 2, "sent_count": 2},
    ))) is None


def test_session_mismatch_ditolak():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(seq=3)))).accepted
    salah = brain.apply(decode_event(json.dumps(_observation(
        seq=4,
        session_id="save:demo:epoch:LAIN",
        kind="delta",
        domain_revision=2,
        body={"upserts": {"x": 1}},
    ))))
    assert salah.rejected
    assert salah.reason == "session_mismatch"
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.session_id == SESSION
    assert rec.domain_revision == 1
    assert "x" not in rec.facts


def test_sequence_basi_ditolak():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(seq=5)))).accepted
    duplikat = brain.apply(decode_event(json.dumps(_observation(seq=5, domain_revision=2))))
    assert duplikat.rejected
    assert duplikat.reason == "stale_sequence"
    lama = brain.apply(decode_event(json.dumps(_observation(seq=4, domain_revision=2))))
    assert lama.rejected
    assert lama.reason == "stale_sequence"
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.domain_revision == 1
    assert rec.seq == 5


def test_snapshot_baru_membersihkan_stale_setelah_resync():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(
        seq=3, domain_revision=1,
        body={"facts": {"lama": 1}},
    )))).accepted
    gap = brain.apply(decode_event(json.dumps(_observation(
        seq=4, kind="delta", domain_revision=9,
        body={"upserts": {"jangan": 1}},
    ))))
    assert gap.requires_resync
    assert brain.get(P2, DOMAIN).stale

    resync = brain.apply(decode_event(json.dumps(_observation(
        seq=5,
        domain_revision=9,
        body={"facts": {"baru": 2}},
        truncation={"truncated": False, "observed_count": 1, "sent_count": 1},
    ))))
    assert resync.accepted
    rec = brain.get(P2, DOMAIN)
    assert rec is not None
    assert rec.stale is False
    assert rec.requires_resync is False
    assert rec.domain_revision == 9
    assert dict(rec.facts) == {"baru": 2}
    assert rec.is_complete
    assert brain.query(P2, DOMAIN, authoritative=True, complete=True) is rec


def test_snapshot_session_baru_adalah_resync_bukan_mismatch():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(seq=3)))).accepted
    baru = brain.apply(decode_event(json.dumps(_observation(
        seq=1,
        session_id="save:demo:epoch:2",
        domain_revision=1,
        body={"facts": {"epoch2": True}},
    ))))
    assert baru.accepted
    rec = brain.get(P2, DOMAIN)
    assert rec.session_id == "save:demo:epoch:2"
    assert rec.facts["epoch2"] is True


def test_domain_unavailable_eksplisit():
    brain = RichObservationStore()
    ev = decode_event(json.dumps(_observation(
        seq=3,
        kind="error",
        available=False,
        error={"reason": "unsupported_menu_type", "detail": "CustomMenu"},
        body=None,
        truncation=None,
        authoritative=False,
        provenance="UNKNOWN",
    )))
    assert ev is not None
    assert brain.apply(ev).accepted
    rec = brain.get(P2, DOMAIN)
    assert rec.available is False
    assert rec.error_reason == "unsupported_menu_type"
    assert rec.is_complete is False
    assert brain.query(P2, DOMAIN, complete=True) is None


def test_kontrak_skema_observation_dari_csharp():
    berkas = ROOT / "tests" / "data" / "stardew_observation_contract.json"
    payload = json.loads(berkas.read_text(encoding="utf-8"))
    assert observation_payload_sah(payload)
    ev = decode_event(json.dumps({
        "protocol": 1, "ev": "observation", "seq": 3,
        "player_id": P2, "screen_id": 1, "payload": payload,
    }))
    assert ev is not None
    store = RichObservationStore()
    assert store.apply(ev).accepted
    rec = store.get(P2, "scene.current_location")
    assert rec is not None
    assert rec.session_id == "save:demo:epoch:1"
    assert rec.provenance == "ACTIVE_SYNCED"
    assert rec.authoritative
    assert rec.is_complete
    assert "obj:Farm:12,8" in rec.facts


def test_provenance_minimum_set_beku():
    assert PROVENANCE_VALUES == LIVE_AUTHORITATIVE | {
        "STATIC_DATA", "SHADOW_STALE_POSSIBLE", "DERIVED", "UNKNOWN",
    }
    assert KNOWN_PRODUCTS == {"scene", "ui", "progress", "knowledge"}
    assert "state.fast" not in KNOWN_PRODUCTS
    assert KNOWN_DOMAINS == {
        "scene.current_location",
        "ui.active_menu",
        "ui.context",
        "ui.text",
        "progress.player",
        "progress.inventory",
        "knowledge.catalog",
        "knowledge.world",  # 0cb472851: native destinations, separate from motor state.
    }
    assert ("ui", "ui.context") in KNOWN_OBSERVATION_PAIRS
    assert ("ui", "ui.active_menu") in KNOWN_OBSERVATION_PAIRS
    assert ("scene", "scene.current_location") in KNOWN_OBSERVATION_PAIRS
    assert ("knowledge", "knowledge.world") in KNOWN_OBSERVATION_PAIRS
    assert all(product != "state.fast" for product, _domain in KNOWN_OBSERVATION_PAIRS)


def test_motor_store_tidak_menelan_observation():
    motor = StardewStateStore()
    motor.catat(decode_event(json.dumps(_hello())))
    motor.catat(decode_event(json.dumps(_state(seq=2))))
    motor.catat(decode_event(json.dumps(_observation(seq=3))))
    snap = motor.latest(P2)
    assert snap is not None
    assert snap.location == "Farm"
    assert snap.seq == 2


def test_kind_body_salah_ditolak_fail_closed():
    brain = RichObservationStore()
    # snapshot tidak boleh menelan upserts/removes sebagai facts.
    snap_delta = _observation(
        seq=3,
        body={"facts": {"a": 1}, "upserts": {"b": 2}, "removes": ["a"]},
    )
    assert observation_payload_sah(snap_delta["payload"]) is False
    assert decode_event(json.dumps(snap_delta)) is None
    assert brain.apply(snap_delta).rejected
    assert brain.get(P2, DOMAIN) is None
    assert brain.query(P2, DOMAIN, authoritative=True, complete=True) is None

    # delta tidak boleh menelan facts sebagai patch.
    delta_facts = _observation(
        seq=3,
        kind="delta",
        domain_revision=1,
        body={"facts": {"a": 1}, "upserts": {"b": 2}},
    )
    assert observation_payload_sah(delta_facts["payload"]) is False
    assert decode_event(json.dumps(delta_facts)) is None
    assert brain.apply(delta_facts).rejected
    assert brain.query(P2, DOMAIN, authoritative=True, complete=True) is None

    # error tidak boleh menyamar snapshot lengkap kosong.
    error_palsu = _observation(
        seq=3,
        kind="error",
        available=True,
        error=None,
        truncation={"truncated": False, "observed_count": 0, "sent_count": 0},
        body={"facts": {}},
    )
    assert observation_payload_sah(error_palsu["payload"]) is False
    assert decode_event(json.dumps(error_palsu)) is None
    assert brain.apply(error_palsu).rejected
    rec = brain.get(P2, DOMAIN)
    assert rec is None or rec.is_complete is False
    assert brain.query(P2, DOMAIN, authoritative=True, complete=True) is None

    # snapshot yang klaim complete tapi facts lebih sedikit dari sent_count.
    kurang = _observation(
        seq=3,
        truncation={"truncated": False, "observed_count": 2, "sent_count": 2},
        body={"facts": {"satu": 1}},
    )
    assert observation_payload_sah(kurang["payload"]) is False
    assert decode_event(json.dumps(kurang)) is None


def test_delta_kosong_eksplisit_maju_revisi():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(
        seq=3,
        body={"facts": {"a": 1}},
        truncation={"truncated": False, "observed_count": 1, "sent_count": 1},
    )))).accepted
    kosong = decode_event(json.dumps(_observation(
        seq=4, kind="delta", domain_revision=2, body={},
        truncation=None,
    )))
    assert kosong is not None
    assert brain.apply(kosong).accepted
    rec = brain.get(P2, DOMAIN)
    assert rec.domain_revision == 2
    assert dict(rec.facts) == {"a": 1}
    assert rec.truncated is False
    assert rec.observed_count == 1
    assert rec.sent_count == 1
    assert rec.stale is False
    assert rec.is_complete
    assert brain.query(P2, DOMAIN, complete=True) is rec


def test_manifest_grammar_dan_bukan_snapshot_lengkap():
    brain = RichObservationStore()
    sah = decode_event(json.dumps(_observation(
        seq=3,
        product="knowledge",
        domain="knowledge.catalog",
        kind="manifest",
        provenance="STATIC_DATA",
        authoritative=False,
        truncation=None,
        body={
            "catalog_id": "stardew-1.6.15",
            "game_version": "1.6.15.24356",
            "entry_count": 0,
        },
    )))
    assert sah is not None
    assert brain.apply(sah).accepted
    rec = brain.get(P2, "knowledge.catalog")
    assert rec.catalog_id == "stardew-1.6.15"
    assert rec.is_complete is False
    assert brain.query(P2, "knowledge.catalog", complete=True) is None

    palsu = _observation(
        seq=4,
        product="knowledge",
        domain="knowledge.catalog",
        kind="manifest",
        provenance="STATIC_DATA",
        authoritative=False,
        truncation=None,
        body={"facts": {"item": 1}, "catalog_id": "x"},
    )
    assert observation_payload_sah(palsu["payload"]) is False
    assert decode_event(json.dumps(palsu)) is None


def test_session_retired_tidak_bangkit():
    """A -> B -> A harus meninggalkan B. ID opaq, bukan urutan string."""
    brain = RichObservationStore()
    sess_a = "sess-zzz"
    sess_b = "sess-aaa"
    assert brain.apply(decode_event(json.dumps(_observation(
        seq=3, session_id=sess_a, body={"facts": {"a": 1}},
    )))).accepted
    assert brain.apply(decode_event(json.dumps(_observation(
        seq=1, session_id=sess_b, body={"facts": {"b": 1}},
    )))).accepted
    assert brain.get(P2, DOMAIN).session_id == sess_b

    bangkit = brain.apply(decode_event(json.dumps(_observation(
        seq=4, session_id=sess_a, domain_revision=9,
        body={"facts": {"a": 99}},
    ))))
    assert bangkit.rejected
    assert bangkit.reason == "session_superseded"
    rec = brain.get(P2, DOMAIN)
    assert rec.session_id == sess_b
    assert dict(rec.facts) == {"b": 1}

    delta_lama = brain.apply(decode_event(json.dumps(_observation(
        seq=5, session_id=sess_a, kind="delta", domain_revision=2,
        body={"upserts": {"x": 1}},
    ))))
    assert delta_lama.rejected
    assert brain.get(P2, DOMAIN).session_id == sess_b

    sess_c = "sess-mmm"
    assert brain.apply(decode_event(json.dumps(_observation(
        seq=1, session_id=sess_c, body={"facts": {"c": 1}},
    )))).accepted
    assert brain.get(P2, DOMAIN).session_id == sess_c


def test_session_baru_error_menimpa_session_lama():
    """Error session B yang sah harus men-retire snapshot lengkap session A."""
    brain = RichObservationStore()
    sess_a = "save:demo:epoch:A"
    sess_b = "save:demo:epoch:B"
    snap_a = decode_event(json.dumps(_observation(
        seq=3,
        session_id=sess_a,
        body={"facts": {"a": 1}},
        truncation={"truncated": False, "observed_count": 1, "sent_count": 1},
    )))
    assert brain.apply(snap_a).accepted
    rec_a = brain.get(P2, DOMAIN)
    assert rec_a.is_complete
    assert rec_a.authoritative
    assert brain.query(P2, DOMAIN, authoritative=True, complete=True) is rec_a

    err_b = decode_event(json.dumps(_observation(
        seq=1,
        session_id=sess_b,
        kind="error",
        domain_revision=1,
        available=False,
        error={"reason": "unavailable"},
        body=None,
        truncation=None,
        authoritative=False,
        provenance="UNKNOWN",
    )))
    assert err_b is not None
    assert brain.apply(err_b).accepted
    rec = brain.get(P2, DOMAIN)
    assert rec.session_id == sess_b
    assert rec.kind == "error"
    assert rec.available is False
    assert rec.error_reason == "unavailable"
    assert brain.query(P2, DOMAIN, authoritative=True) is None
    assert brain.query(P2, DOMAIN, complete=True) is None

    snap_b = decode_event(json.dumps(_observation(
        seq=2,
        session_id=sess_b,
        domain_revision=2,
        body={"facts": {"b": 1}},
        truncation={"truncated": False, "observed_count": 1, "sent_count": 1},
    )))
    assert brain.apply(snap_b).accepted
    rec = brain.get(P2, DOMAIN)
    assert rec.session_id == sess_b
    assert rec.available is True
    assert rec.is_complete
    assert dict(rec.facts) == {"b": 1}
    assert brain.query(P2, DOMAIN, authoritative=True, complete=True) is rec

    later_a = brain.apply(decode_event(json.dumps(_observation(
        seq=4,
        session_id=sess_a,
        domain_revision=9,
        body={"facts": {"a": 99}},
    ))))
    assert later_a.rejected
    assert later_a.reason == "session_superseded"
    assert brain.get(P2, DOMAIN).session_id == sess_b
    assert dict(brain.get(P2, DOMAIN).facts) == {"b": 1}


def test_available_error_kontradiksi_ditolak():
    kasus = [
        _observation(seq=3, available=False),
        _observation(seq=3, error={"reason": "unavailable"}),
        _observation(
            seq=3, kind="delta", domain_revision=1, available=False,
            body={"upserts": {"x": 1}},
        ),
        _observation(
            seq=3,
            product="knowledge",
            domain="knowledge.catalog",
            kind="manifest",
            provenance="STATIC_DATA",
            authoritative=False,
            truncation=None,
            error={"reason": "unavailable"},
            body={"catalog_id": "stardew-1.6.15"},
        ),
        _observation(
            seq=3, kind="error", available=True,
            error={"reason": "unavailable"}, body=None, truncation=None,
        ),
        _observation(
            seq=3, kind="error", available=False, error=None,
            body=None, truncation=None,
        ),
        _observation(
            seq=3, kind="error", available=False,
            error={"reason": "unavailable"},
            body={"facts": {}}, truncation=None,
        ),
        _observation(
            seq=3, kind="error", available=False,
            error={"reason": "unavailable"}, body=None,
            truncation={"truncated": False, "observed_count": 0, "sent_count": 0},
        ),
        _observation(
            seq=3, kind="delta", domain_revision=1,
            error={"reason": "unavailable"},
            body={"upserts": {"x": 1}},
        ),
    ]
    brain = RichObservationStore()
    for palsu in kasus:
        assert observation_payload_sah(palsu["payload"]) is False, palsu["payload"]
        assert decode_event(json.dumps(palsu)) is None
        assert brain.apply(palsu).rejected
    assert brain.get(P2, DOMAIN) is None


def test_delta_terpotong_stale_bukan_hitungan_domain():
    brain = RichObservationStore()
    assert brain.apply(decode_event(json.dumps(_observation(
        seq=3,
        body={"facts": {"a": 1, "b": 2}},
        truncation={"truncated": False, "observed_count": 2, "sent_count": 2},
    )))).accepted
    rec0 = brain.get(P2, DOMAIN)
    assert rec0.is_complete
    assert rec0.observed_count == 2
    assert rec0.sent_count == 2

    potong = decode_event(json.dumps(_observation(
        seq=4,
        kind="delta",
        domain_revision=2,
        body={"upserts": {"c": 3}},
        truncation={"truncated": True, "observed_count": 10, "sent_count": 1},
    )))
    assert potong is not None
    hasil = brain.apply(potong)
    assert hasil.accepted
    assert hasil.requires_resync
    assert hasil.reason == "truncated"
    rec = brain.get(P2, DOMAIN)
    assert rec.stale
    assert rec.requires_resync
    assert rec.domain_revision == 2
    # Hitungan patch delta jangan disimpan seolah total domain.
    assert rec.observed_count == 2
    assert rec.sent_count == 2
    assert rec.truncated is False
    assert rec.is_complete is False
    assert brain.query(P2, DOMAIN, complete=True) is None
    assert rec.facts["c"] == 3


def test_cli_observation_tidak_dihitung_sebagai_state():
    motor = StardewStateStore()
    hitung: dict[int, int] = {}
    hello = decode_event(json.dumps(_hello()))
    motor.catat(hello)
    hello_lines = format_cli_ringkasan(
        hello, motor, hitung, tiap=1, target="Arti"
    )
    assert hello_lines[0].startswith("[hello]")
    assert hitung == {}

    state = decode_event(json.dumps(_state(seq=2)))
    motor.catat(state)
    state_lines = format_cli_ringkasan(
        state, motor, hitung, tiap=1, target="Arti"
    )
    assert hitung[P2] == 1
    assert state_lines[0].startswith("[state]")
    assert "Farm" in state_lines[0]

    obs = decode_event(json.dumps(_observation(seq=3)))
    motor.catat(obs)
    assert format_cli_ringkasan(obs, motor, hitung, tiap=1, target="Arti") == []
    assert hitung[P2] == 1
    assert motor.latest(P2).seq == 2

    lain = decode_event(json.dumps({
        "protocol": 1, "ev": "future_obs_2027", "seq": 4,
        "player_id": P2, "screen_id": 1, "payload": {},
    }))
    motor.catat(lain)
    assert format_cli_ringkasan(lain, motor, hitung, tiap=1, target="Arti") == []
    assert hitung[P2] == 1
