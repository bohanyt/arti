"""OBS-2B1 Tier-3: kolektor bukti — sintetis deterministik, tanpa game.

Semua tes memakai jam palsu + event yang lolos decode_event, jadi hasilnya
byte-per-byte sama antar-run. Tidak menyentuh Stardew/SMAPI/motor.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from arti_stardew import decode_event
from arti_stardew import StardewReceiver
from arti_stardew_scene import SCENE_DOMAIN
from arti_stardew_scene_delta import (
    SCENE_DELTA_MAX_PATCH_OPS,
    build_delta_payload,
    diff_scene_facts,
)
from arti_stardew_tier3 import KolektorBuktiTier3

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "tests" / "data" / "stardew_scene_snapshot_contract.json"
P2 = -100000000000000001
OBJ_BARU = "obj:Farm:3,3:(O)388"
OBJ_LAMA = "obj:Farm:12,8:(O)472"


class JamPalsu:
    def __init__(self, awal: float = 100.0) -> None:
        self.t = awal

    def __call__(self) -> float:
        return self.t

    def maju(self, detik: float) -> None:
        self.t += detik


def _ev(raw: dict) -> dict:
    ev = decode_event(json.dumps(raw))
    assert ev is not None, "event sintetis wajib lolos decode_event"
    return ev


def _observation(*, seq: int, payload: dict) -> dict:
    return _ev({
        "protocol": 1,
        "ev": "observation",
        "seq": seq,
        "player_id": P2,
        "screen_id": 1,
        "payload": payload,
    })


def _snapshot_event() -> dict:
    return _ev(json.loads(SNAP.read_text(encoding="utf-8")))


def _hello(seq: int = 1) -> dict:
    return _ev({
        "protocol": 1, "ev": "hello", "seq": seq,
        "player_id": P2, "screen_id": 1,
        "player_name": "artihand", "is_main_player": False,
        "is_split_screen": False,
    })


def _state(seq: int, x: int = 42) -> dict:
    return _ev({
        "protocol": 1, "ev": "state", "seq": seq,
        "player_id": P2, "screen_id": 1,
        "payload": {
            "location": "Farm",
            "tile": {"x": x, "y": 17},
            "pixel": {"x": x * 64.0, "y": 1088.0},
            "facing": 2,
            "time_of_day": 610,
            "date": {"season": "spring", "day": 1, "year": 1},
            "energy": 270.0,
            "health": 100,
            "is_player_free": True,
            "can_player_move": True,
            "active_menu": None,
        },
    })


def _delta_event(
    kolektor: KolektorBuktiTier3, *, seq: int, after: dict,
    session_id: str | None = None, previous_revision: int | None = None,
) -> dict:
    rec = kolektor.brain.get(P2, SCENE_DOMAIN)
    assert rec is not None
    payload = build_delta_payload(
        session_id=session_id or rec.session_id,
        previous_revision=(
            rec.domain_revision if previous_revision is None
            else previous_revision
        ),
        provenance="ACTIVE_SYNCED",
        authoritative=True,
        patch=diff_scene_facts(dict(rec.facts), after),
    )
    return _observation(seq=seq, payload=payload)


def test_snapshot_lalu_delta_tambah_tercatat():
    kolektor = KolektorBuktiTier3(jam=JamPalsu())
    kolektor.on_event(_snapshot_event())
    rec0 = kolektor.brain.get(P2, SCENE_DOMAIN)
    after = dict(rec0.facts)
    after[OBJ_BARU] = {
        "kind": "object", "qualified_item_id": "(O)388", "name": "Wood",
        "display_name": "Wood", "tile": {"x": 3, "y": 3},
        "passable": False, "grabbable": True, "machine": False,
    }
    kolektor.on_event(_delta_event(kolektor, seq=4, after=after))

    r = kolektor.ringkasan()
    assert r["apply"] == {
        "accepted": 2, "ignored": 0, "rejected": 0, "alasan": {},
    }
    assert r["revisi_berurutan"] is True
    proof = r["bukti_delta_objek"]
    assert proof is not None
    assert proof["seq"] == 4
    assert proof["domain_revision"] == 2
    assert proof["upsert_ids"] == [OBJ_BARU]
    assert proof["remove_ids"] == []
    sc = r["scene_akhir"][str(P2)]
    assert sc["kind"] == "delta"
    assert sc["domain_revision"] == 2
    assert sc["stale"] is False
    assert sc["requires_resync"] is False
    assert sc["complete"] is True
    assert OBJ_BARU in kolektor.brain.get(P2, SCENE_DOMAIN).facts
    assert any(
        "delta rev=2 -> TERIMA" in b and OBJ_BARU in b for b in kolektor.bukti
    )


def test_snapshot_lalu_delta_hapus_tercatat():
    kolektor = KolektorBuktiTier3(jam=JamPalsu())
    kolektor.on_event(_snapshot_event())
    rec0 = kolektor.brain.get(P2, SCENE_DOMAIN)
    assert OBJ_LAMA in rec0.facts
    after = dict(rec0.facts)
    after.pop(OBJ_LAMA)
    kolektor.on_event(_delta_event(kolektor, seq=4, after=after))

    r = kolektor.ringkasan()
    proof = r["bukti_delta_objek"]
    assert proof is not None
    assert proof["upsert_ids"] == []
    assert proof["remove_ids"] == [OBJ_LAMA]
    rec = kolektor.brain.get(P2, SCENE_DOMAIN)
    assert OBJ_LAMA not in rec.facts
    assert r["scene_akhir"][str(P2)]["complete"] is True
    assert r["scene_akhir"][str(P2)]["jumlah_fakta"] == len(rec0.facts) - 1


def test_celah_revisi_tercatat_stale_resync():
    kolektor = KolektorBuktiTier3(jam=JamPalsu())
    kolektor.on_event(_snapshot_event())
    rec0 = kolektor.brain.get(P2, SCENE_DOMAIN)
    after = dict(rec0.facts)
    after[OBJ_BARU] = {"kind": "object"}
    # previous_revision = rev+1 -> delta datang sebagai R+2: celah.
    kolektor.on_event(_delta_event(
        kolektor, seq=4, after=after,
        previous_revision=rec0.domain_revision + 1,
    ))

    r = kolektor.ringkasan()
    assert r["apply"]["rejected"] == 1
    assert r["apply"]["alasan"] == {"revision_gap": 1}
    assert r["bukti_delta_objek"] is None
    sc = r["scene_akhir"][str(P2)]
    assert sc["stale"] is True
    assert sc["requires_resync"] is True
    assert sc["complete"] is False
    assert sc["domain_revision"] == rec0.domain_revision
    # Revisi DITERIMA tetap berurutan: yang bercelah justru DITOLAK store.
    assert r["revisi_berurutan"] is True
    assert any("TOLAK (revision_gap, resync)" in b for b in kolektor.bukti)


def test_delta_terpotong_resync_tidak_complete():
    kolektor = KolektorBuktiTier3(jam=JamPalsu())
    kolektor.on_event(_snapshot_event())
    rec0 = kolektor.brain.get(P2, SCENE_DOMAIN)
    after = {
        f"obj:Farm:{i},{i}:(O)0": {"n": i}
        for i in range(SCENE_DELTA_MAX_PATCH_OPS + 5)
    }
    kolektor.on_event(_delta_event(kolektor, seq=4, after=after))

    r = kolektor.ringkasan()
    assert r["apply"]["accepted"] == 2
    assert r["apply"]["alasan"] == {"truncated": 1}
    sc = r["scene_akhir"][str(P2)]
    assert sc["stale"] is True
    assert sc["requires_resync"] is True
    assert sc["complete"] is False
    assert sc["domain_revision"] == rec0.domain_revision + 1
    assert any("TERIMA resync (truncated)" in b for b in kolektor.bukti)


def test_snapshot_session_baru_transisi():
    kolektor = KolektorBuktiTier3(jam=JamPalsu())
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    sesi_lama = snap["payload"]["session_id"]
    kolektor.on_event(_ev(snap))

    snap_baru = json.loads(SNAP.read_text(encoding="utf-8"))
    sesi_baru = f"{sesi_lama}:epoch2"
    snap_baru["payload"]["session_id"] = sesi_baru
    snap_baru["seq"] = snap["seq"] + 1
    kolektor.on_event(_ev(snap_baru))

    # Delta yang masih memakai session lama wajib ditolak (superseded).
    rec = kolektor.brain.get(P2, SCENE_DOMAIN)
    after = dict(rec.facts)
    after[OBJ_BARU] = {"kind": "object"}
    kolektor.on_event(_delta_event(
        kolektor, seq=snap["seq"] + 2, after=after, session_id=sesi_lama,
        previous_revision=1,
    ))

    r = kolektor.ringkasan()
    assert [s["session_id"] for s in r["sesi"]] == [sesi_lama, sesi_baru]
    assert all(s["berurutan"] for s in r["sesi"])
    assert r["apply"]["accepted"] == 2
    assert r["apply"]["rejected"] == 1
    assert r["apply"]["alasan"] == {"session_superseded": 1}
    sc = r["scene_akhir"][str(P2)]
    assert sc["session_id"] == sesi_baru
    assert sc["domain_revision"] == 1
    assert sc["complete"] is True


def test_motor_state_terpisah_dan_kesehatan():
    jam = JamPalsu()
    kolektor = KolektorBuktiTier3(jam=jam)
    kolektor.on_event(_hello(seq=1))
    for i, seq in enumerate((2, 3, 4)):
        jam.maju(0.1)
        kolektor.on_event(_state(seq, x=42 + i))
    jam.maju(0.5)
    kolektor.on_event(_snapshot_event())
    rec0 = kolektor.brain.get(P2, SCENE_DOMAIN)
    after = dict(rec0.facts)
    after[OBJ_BARU] = {"kind": "object"}
    kolektor.on_event(_delta_event(kolektor, seq=6, after=after))
    jam.maju(0.2)

    r = kolektor.ringkasan()
    # MOTOR_STATE hanya dari ev=state; observation tidak menggesernya.
    motor = kolektor.motor.latest(P2)
    assert motor is not None
    assert motor.seq == 4
    assert motor.tile == (44, 17)
    assert motor.revisi == 3
    assert not hasattr(motor, "facts")
    k = r["motor_state"][str(P2)]
    assert k["events"] == 3
    assert k["rate_hz"] == 10.0
    assert k["celah_maks_s"] == 0.1
    assert k["umur_terakhir_s"] == 0.7
    pem = r["pemisahan"]
    assert pem["nonobservasi_total"] == 4  # 1 hello + 3 state
    assert pem["brain_abaikan_nonobservasi"] == 4
    assert pem["motor_berubah_saat_observation"] == 0
    assert pem["bersih"] is True
    # Sisi brain hanya tersentuh observation.
    assert r["apply"]["accepted"] == 2
    assert r["protokol"]["state"] == 3
    assert r["protokol"]["observation"] == 2


def _urutan_campuran(kolektor: KolektorBuktiTier3, jam: JamPalsu) -> None:
    kolektor.on_event(_hello(seq=1))
    for i, seq in enumerate((2, 3, 4, 5)):
        jam.maju(0.1)
        kolektor.on_event(_state(seq, x=40 + i))
    kolektor.on_event(_snapshot_event())
    rec0 = kolektor.brain.get(P2, SCENE_DOMAIN)
    after = dict(rec0.facts)
    after.pop(OBJ_LAMA)
    after[OBJ_BARU] = {"kind": "object"}
    kolektor.on_event(_delta_event(kolektor, seq=7, after=after))
    jam.maju(0.3)


def test_ringkasan_deterministik_dan_ringkas():
    jam_a, jam_b = JamPalsu(), JamPalsu()
    a = KolektorBuktiTier3(jam=jam_a)
    b = KolektorBuktiTier3(jam=jam_b)
    _urutan_campuran(a, jam_a)
    _urutan_campuran(b, jam_b)

    assert a.ringkasan() == b.ringkasan()
    assert a.baris_ringkas() == b.baris_ringkas()
    assert a.bukti == b.bukti
    assert json.dumps(a.ringkasan(), sort_keys=True) == json.dumps(
        b.ringkasan(), sort_keys=True
    )
    # Ringkas: bukan banjir — bukti per-event hanya hello + observation.
    assert len(a.bukti) == 3  # 1 hello + snapshot + delta; 4 state TANPA baris
    assert len(a.baris_ringkas()) <= 15

    # Banjir 200 state tidak menambah baris bukti sama sekali.
    sebelum = len(a.bukti)
    for i in range(200):
        jam_a.maju(0.1)
        a.on_event(_state(100 + i))
    assert len(a.bukti) == sebelum
    assert a.ringkasan()["motor_state"][str(P2)]["events"] == 204


def test_callback_error_tercatat_tidak_melempar():
    kolektor = KolektorBuktiTier3(jam=JamPalsu())

    def meledak(event):  # noqa: ANN001
        raise RuntimeError("bukti error sintetis")

    kolektor.brain.apply = meledak  # type: ignore[method-assign]
    kolektor.on_event(_snapshot_event())  # tidak boleh melempar
    r = kolektor.ringkasan()
    assert r["callback_error"]["jumlah"] == 1
    assert r["callback_error"]["contoh"] == [
        "RuntimeError: bukti error sintetis"
    ]
    baris = kolektor.baris_ringkas()
    assert any("callback_error: 1" in b for b in baris)


def test_wiring_receiver_loopback():
    """Smoke satu arah lewat TCP sungguhan: receiver -> kolektor."""
    kolektor = KolektorBuktiTier3()
    receiver = StardewReceiver(on_event=kolektor.on_event, port=0)
    receiver.start()
    try:
        snap = json.loads(SNAP.read_text(encoding="utf-8"))
        rec_payload = snap["payload"]
        delta = build_delta_payload(
            session_id=rec_payload["session_id"],
            previous_revision=rec_payload["domain_revision"],
            provenance="ACTIVE_SYNCED",
            authoritative=True,
            patch=diff_scene_facts(
                rec_payload["body"]["facts"],
                {
                    k: v for k, v in rec_payload["body"]["facts"].items()
                    if k != OBJ_LAMA
                },
            ),
        )
        with socket.create_connection(receiver.address, timeout=5.0) as sock:
            baris_hello = {
                "protocol": 1, "ev": "hello", "seq": 1,
                "player_id": P2, "screen_id": 1,
                "player_name": "artihand", "is_main_player": False,
                "is_split_screen": False,
            }
            for raw in (
                baris_hello,
                snap,
                {
                    "protocol": 1, "ev": "observation",
                    "seq": snap["seq"] + 1,
                    "player_id": P2, "screen_id": 1, "payload": delta,
                },
            ):
                sock.sendall(
                    (json.dumps(raw, ensure_ascii=False) + "\n").encode()
                )
        batas = time.monotonic() + 5.0
        while time.monotonic() < batas:
            if kolektor.apply_accepted >= 2:
                break
            time.sleep(0.02)
    finally:
        receiver.stop()

    r = kolektor.ringkasan()
    assert r["apply"]["accepted"] == 2
    assert r["bukti_delta_objek"] is not None
    assert r["bukti_delta_objek"]["remove_ids"] == [OBJ_LAMA]
    assert r["pemisahan"]["bersih"] is True
    assert r["callback_error"]["jumlah"] == 0
