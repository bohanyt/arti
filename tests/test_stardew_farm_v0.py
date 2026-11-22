"""Farm Loop v0: orkestrasi deterministik di atas kontrak yang sudah ada.

Yang dijaga di sini adalah kendali, bukan kecerdasan:

1. rute operator di luar batas tidak pernah mengeluarkan perintah;
2. PressCompleted bukan bukti farmer pindah — hanya state baru + geser piksel;
3. gagal/uncertainty = STOP, tanpa retry, tanpa water;
4. sukses driver hanya ObservedWatered;
5. tidak ada pencarian target dan tidak ada pembuatan rute.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_stardew import StardewStateStore  # noqa: E402
from arti_stardew_command import (  # noqa: E402
    DITOLAK,
    GAGAL_LOKAL,
    HasilPerintah,
    SELESAI,
    SesiPerintah,
    TIDAK_PASTI,
)
from arti_stardew_farm_v0 import (  # noqa: E402
    CANNOT_MOVE,
    INBOX_BUSY,
    MENU_OPEN,
    MISSING_STATE,
    NO_DISPLACEMENT,
    NOT_ADJACENT,
    PLAYER_MISMATCH,
    PermintaanFarmV0,
    ROUTE_INVALID,
    SESSION_CHANGED,
    STATE_TIMEOUT,
    STALE_STATE,
    SUCCESS,
    UNEXPECTED_LOCATION,
    WATER_FAILED,
    WATER_REJECTED,
    WATER_UNCERTAIN,
    WORLD_PRESS_FAILED,
    WORLD_PRESS_REJECTED,
    WORLD_PRESS_UNCERTAIN,
    WRONG_DIRECTION,
    WRONG_LOCATION,
    jalankan_farm_v0,
)

P2 = -100000000000000001
P_LAIN = 100000000012
TARGET = (10, 12)


class JamPalsu:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def maju(self, d: float) -> None:
        self.t += d


def _payload(**timpa):
    p = {
        "location": "Farm",
        "tile": {"x": 10, "y": 11},
        "pixel": {"x": 640.0, "y": 704.0},
        "facing": 2,
        "time_of_day": 610,
        "date": {"season": "spring", "day": 1, "year": 1},
        "energy": 270.0,
        "health": 100,
        "is_player_free": True,
        "can_player_move": True,
        "active_menu": None,
    }
    p.update(timpa)
    return p


def _event_state(player_id=P2, *, seq=2, screen_id=0, **payload_timpa):
    return {
        "protocol": 1,
        "ev": "state",
        "seq": seq,
        "player_id": player_id,
        "screen_id": screen_id,
        "payload": _payload(**payload_timpa),
    }


def buat_sesi(**ubah) -> SesiPerintah:
    data = dict(
        session_id="sesi-abc",
        player_id=P2,
        player_name="artihand",
        location_id="",
        screen_id=0,
        is_main_player=False,
        is_split_screen=False,
        inbox_enabled=True,
        menu_step_enabled=False,
        water_once_enabled=True,
        world_press_enabled=True,
        world_ready=True,
        inflight_request_id=None,
        input_busy=False,
    )
    data.update(ubah)
    return SesiPerintah(**data)


def hasil_press(arah="down", ticks=10, request_id="press-1", **ubah) -> HasilPerintah:
    mentah = {
        "op": "world_press",
        "status": "completed",
        "success": True,
        "outcome": "PressCompleted",
        "direction": arah,
        "press_count": ticks,
        "moved": None,
    }
    if "mentah" in ubah:
        mentah = ubah.pop("mentah")
    else:
        mentah.update(ubah.pop("mentah_timpa", {}))
    return HasilPerintah(
        status=ubah.pop("status", SELESAI),
        sukses=ubah.pop("sukses", True),
        request_id=request_id,
        outcome=ubah.pop("outcome", "PressCompleted"),
        alasan=ubah.pop("alasan", None),
        mentah=mentah,
        **ubah,
    )


def hasil_water(request_id="water-1", **ubah) -> HasilPerintah:
    mentah = {
        "op": "water_once",
        "status": "completed",
        "success": True,
        "outcome": "ObservedWatered",
    }
    mentah.update(ubah.pop("mentah_timpa", {}))
    return HasilPerintah(
        status=ubah.pop("status", SELESAI),
        sukses=ubah.pop("sukses", True),
        request_id=request_id,
        outcome=ubah.pop("outcome", "ObservedWatered"),
        alasan=ubah.pop("alasan", None),
        mentah=mentah,
        **ubah,
    )


class DuniaFarm:
    """Store sungguhan + perintah palsu. State baru disuntik saat perintah."""

    def __init__(self) -> None:
        self.jam = JamPalsu()
        self.store = StardewStateStore(jam=self.jam)
        self.seq = 1
        self.sesi = buat_sesi()
        self.press_calls: list[tuple[str, int]] = []
        self.water_calls: list[tuple[int, int]] = []
        self._press_antrian: list[tuple[HasilPerintah, dict | None]] = []
        self._water_antrian: list[tuple[HasilPerintah, dict | None]] = []
        self.baca_count = 0
        self.sesi_pada_baca: dict[int, SesiPerintah] = {}

    def isi_state(self, player_id=P2, **payload):
        self.seq += 1
        self.store.catat(
            _event_state(player_id, seq=self.seq, **payload)
        )
        return self.store.latest(player_id)

    def antri_press(self, hasil: HasilPerintah, state_after: dict | None = None):
        self._press_antrian.append((hasil, state_after))

    def antri_water(self, hasil: HasilPerintah, state_after: dict | None = None):
        self._water_antrian.append((hasil, state_after))

    def baca_sesi(self, folder=None, *, retry_detik=0.0) -> SesiPerintah:
        self.baca_count += 1
        if self.baca_count in self.sesi_pada_baca:
            self.sesi = self.sesi_pada_baca[self.baca_count]
        return self.sesi

    def minta_world_press(self, arah, ticks, **kw) -> HasilPerintah:
        self.press_calls.append((arah, ticks))
        hasil, state_after = self._press_antrian.pop(0)
        if state_after is not None:
            self.isi_state(**state_after)
        return hasil

    def minta_water_once(self, tile_x, tile_y, **kw) -> HasilPerintah:
        self.water_calls.append((tile_x, tile_y))
        hasil, state_after = self._water_antrian.pop(0)
        if state_after is not None:
            self.isi_state(**state_after)
        return hasil

    def jalankan(self, **kw):
        permintaan = PermintaanFarmV0(
            target=kw.pop("target", TARGET),
            player_id=kw.pop("player_id", P2),
            state_timeout=kw.pop("state_timeout", 0.0),
            **kw,
        )
        return jalankan_farm_v0(
            permintaan,
            store=self.store,
            baca_sesi=self.baca_sesi,
            minta_world_press=self.minta_world_press,
            minta_water_once=self.minta_water_once,
        )


def _dunia_bersebelahan() -> DuniaFarm:
    dunia = DuniaFarm()
    dunia.isi_state()
    return dunia


# --- 1. already adjacent happy path ---------------------------------------

def test_sudah_bersebelahan_satu_water_observed_watered():
    dunia = _dunia_bersebelahan()
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan(route=())
    assert hasil.success and hasil.status == SUCCESS
    assert hasil.water_outcome == "ObservedWatered"
    assert hasil.adjacency_achieved is True
    assert dunia.press_calls == []
    assert dunia.water_calls == [TARGET]
    assert hasil.world_press_attempted is False
    assert hasil.segments_completed == 0


def test_queued_atau_already_watered_bukan_sukses_driver():
    dunia = _dunia_bersebelahan()
    dunia.antri_water(
        hasil_water(sukses=False, outcome="AlreadyWatered", alasan="outcome=AlreadyWatered")
    )
    hasil = dunia.jalankan(route=())
    assert not hasil.success
    assert hasil.status == WATER_FAILED
    assert hasil.water_outcome == "AlreadyWatered"
    assert dunia.water_calls == [TARGET]
    assert dunia.press_calls == []


# --- 2–3. supplied route happy path ---------------------------------------

def test_satu_segmen_press_plus_geser_lalu_water():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10),
        {
            "tile": {"x": 10, "y": 11},
            "pixel": {"x": 640.0, "y": 704.0},
        },
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.success and hasil.status == SUCCESS
    assert dunia.press_calls == [("down", 10)]
    assert dunia.water_calls == [TARGET]
    assert hasil.world_press_attempted is True
    assert hasil.segments_completed == 1
    assert hasil.adjacency_achieved is True
    assert hasil.tile == (10, 11)


def test_multi_segmen_urut_dan_state_segar_tiap_segmen():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10, request_id="p1"),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    dunia.antri_press(
        hasil_press("right", 8, request_id="p2"),
        {"tile": {"x": 11, "y": 11}, "pixel": {"x": 704.0, "y": 704.0}},
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan(target=(12, 11), route=(("down", 10), ("right", 8)))
    assert hasil.success
    assert dunia.press_calls == [("down", 10), ("right", 8)]
    assert hasil.segments_completed == 2
    assert hasil.press_request_ids == ("p1", "p2")
    assert dunia.water_calls == [(12, 11)]


# --- 4. route bounds ------------------------------------------------------

@pytest.mark.parametrize(
    "route,potongan",
    [
        ((("forward", 10),), "arah"),
        ((("down", 0),), "ticks"),
        ((("down", 61),), "ticks"),
        (tuple(("down", 1) for _ in range(9)), "segmen"),
        ((("down", 60), ("right", 60), ("up", 60), ("left", 60), ("down", 1)), "total ticks"),
    ],
)
def test_rute_di_luar_batas_nol_perintah(route, potongan):
    dunia = _dunia_bersebelahan()
    hasil = dunia.jalankan(route=route)
    assert hasil.status == ROUTE_INVALID
    assert not hasil.success
    assert potongan in (hasil.reason or "")
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_total_240_ticks_tidak_ditolak_di_gerbang_rute():
    dunia = _dunia_bersebelahan()
    dunia.antri_water(hasil_water())
    route = (("down", 60), ("right", 60), ("up", 60), ("left", 60))
    # Rute sah, tetapi farmer sudah bersebelahan jadi kita tidak menjalankan
    # rute ini di tes ini — gerbang rute harus lolos dulu. Pakai posisi
    # non-adjacent supaya rute benar-benar dijalankan sebagian? Cukup
    # pastikan ROUTE_INVALID tidak muncul saat validasi murni.
    from arti_stardew_farm_v0 import validasi_rute

    assert validasi_rute(route) is None
    assert validasi_rute(route + (("down", 1),)) is not None


# --- 5–8. preconditions, zero commands ------------------------------------

def test_state_hilang_nol_perintah():
    dunia = DuniaFarm()
    hasil = dunia.jalankan(route=())
    assert hasil.status == MISSING_STATE
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_state_basi_nol_perintah():
    dunia = _dunia_bersebelahan()
    dunia.jam.maju(0.6)
    hasil = dunia.jalankan(route=(), max_age=0.5)
    assert hasil.status == STALE_STATE
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_identitas_petani_vs_sesi_tidak_cocok_nol_perintah():
    dunia = _dunia_bersebelahan()
    dunia.sesi = buat_sesi(player_id=P_LAIN)
    hasil = dunia.jalankan(route=(), player_id=P2)
    assert hasil.status == PLAYER_MISMATCH
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_lokasi_salah_nol_perintah():
    dunia = DuniaFarm()
    dunia.isi_state(location="FarmHouse")
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == WRONG_LOCATION
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_menu_aktif_nol_perintah():
    dunia = DuniaFarm()
    dunia.isi_state(active_menu="GameMenu")
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == MENU_OPEN
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_tidak_bisa_gerak_sebelum_segmen_nol_world_press():
    dunia = DuniaFarm()
    dunia.isi_state(
        tile={"x": 10, "y": 10},
        pixel={"x": 640.0, "y": 640.0},
        can_player_move=False,
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == CANNOT_MOVE
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_inbox_sibuk_nol_perintah_baru():
    dunia = _dunia_bersebelahan()
    dunia.sesi = buat_sesi(inflight_request_id="masih-jalan")
    hasil = dunia.jalankan(route=())
    assert hasil.status == INBOX_BUSY
    assert dunia.press_calls == []
    assert dunia.water_calls == []


def test_input_busy_nol_perintah_baru():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.sesi = buat_sesi(input_busy=True)
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == INBOX_BUSY
    assert dunia.press_calls == []
    assert dunia.water_calls == []


# --- 9–10. world_press reject / uncertain ---------------------------------

def test_world_press_ditolak_stop_tanpa_water():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press(
            status=DITOLAK,
            sukses=False,
            outcome=None,
            alasan="ArmRejected",
            mentah={"op": "world_press", "status": "rejected", "success": False},
        )
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == WORLD_PRESS_REJECTED
    assert not hasil.success
    assert dunia.press_calls == [("down", 10)]
    assert dunia.water_calls == []
    assert hasil.segments_completed == 0


def test_world_press_tidak_pasti_stop_tanpa_retry_tanpa_water():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press(
            status=TIDAK_PASTI,
            sukses=False,
            outcome=None,
            alasan="aksi mungkin terjadi",
            mentah=None,
        )
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == WORLD_PRESS_UNCERTAIN
    assert dunia.water_calls == []
    assert len(dunia.press_calls) == 1


def test_world_press_lokal_gagal_sebelum_eksekusi_tanpa_water():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        HasilPerintah(
            status=GAGAL_LOKAL,
            sukses=False,
            request_id="x",
            alasan="input menu/dunia sedang dipakai probe lain",
        )
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == WORLD_PRESS_REJECTED
    assert dunia.water_calls == []


# --- 11–14. PressCompleted is not movement --------------------------------

def test_press_completed_tanpa_geser_stop_tanpa_retry_tanpa_water():
    """Local 27: PressCompleted + snapshot baru, piksel sama = bukan gerak."""
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10, mentah_timpa={"moved": True}),
        {"tile": {"x": 10, "y": 10}, "pixel": {"x": 640.0, "y": 640.0}},
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == NO_DISPLACEMENT
    assert not hasil.success
    assert dunia.water_calls == []
    assert len(dunia.press_calls) == 1
    assert hasil.segments_completed == 0


def test_geser_arah_salah_stop():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10),
        {"tile": {"x": 10, "y": 10}, "pixel": {"x": 640.0, "y": 600.0}},
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == WRONG_DIRECTION
    assert dunia.water_calls == []
    assert hasil.segments_completed == 0


def test_tidak_ada_state_baru_sesudah_press_completed_adalah_timeout():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(hasil_press("down", 10), state_after=None)
    hasil = dunia.jalankan(route=(("down", 10),), state_timeout=0.0)
    assert hasil.status == STATE_TIMEOUT
    assert dunia.water_calls == []


def test_warp_lokasi_sesudah_press_stop():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10),
        {
            "location": "Town",
            "tile": {"x": 10, "y": 11},
            "pixel": {"x": 640.0, "y": 704.0},
        },
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == UNEXPECTED_LOCATION
    assert dunia.water_calls == []
    assert hasil.segments_completed == 0


# --- 15. non-adjacent, no invented step -----------------------------------

def test_rute_berakhir_tidak_bersebelahan_tanpa_water_tanpa_langkah_baru():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    hasil = dunia.jalankan(target=(20, 20), route=(("down", 10),))
    assert hasil.status == NOT_ADJACENT
    assert dunia.water_calls == []
    assert dunia.press_calls == [("down", 10)]
    assert hasil.adjacency_achieved is False
    assert hasil.segments_completed == 1


def test_rute_kosong_tidak_bersebelahan_nol_perintah():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    hasil = dunia.jalankan(target=(20, 20), route=())
    assert hasil.status == NOT_ADJACENT
    assert dunia.press_calls == []
    assert dunia.water_calls == []


# --- 16–18. WaterOnce exact outcome ---------------------------------------

def test_water_ditolak_deterministik_stop_tanpa_retry():
    dunia = _dunia_bersebelahan()
    dunia.antri_water(
        HasilPerintah(
            status=DITOLAK,
            sukses=False,
            request_id="w1",
            outcome=None,
            alasan="NotAdjacent",
        )
    )
    hasil = dunia.jalankan(route=())
    assert hasil.status == WATER_REJECTED
    assert not hasil.success
    assert len(dunia.water_calls) == 1


def test_water_tidak_pasti_terminal_tanpa_retry():
    dunia = _dunia_bersebelahan()
    dunia.antri_water(
        HasilPerintah(
            status=TIDAK_PASTI,
            sukses=False,
            request_id="w1",
            alasan="aksi mungkin terjadi",
        )
    )
    hasil = dunia.jalankan(route=())
    assert hasil.status == WATER_UNCERTAIN
    assert not hasil.success
    assert len(dunia.water_calls) == 1


@pytest.mark.parametrize("outcome", ["Queued", "AlreadyWatered", "Released"])
def test_bukan_observed_watered_bukan_sukses(outcome):
    dunia = _dunia_bersebelahan()
    dunia.antri_water(
        hasil_water(sukses=False, outcome=outcome, alasan=f"outcome={outcome}")
    )
    hasil = dunia.jalankan(route=())
    assert not hasil.success
    assert hasil.status == WATER_FAILED
    assert hasil.water_outcome == outcome


# --- 19. command budget ---------------------------------------------------

def test_sukses_tidak_pernah_lebih_dari_segmen_plus_satu_water():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.success
    assert len(dunia.press_calls) == 1
    assert len(dunia.water_calls) == 1
    assert hasil.segments_completed == 1


def test_gagal_di_segmen_kedua_tidak_meneruskan_atau_menyiram():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10, request_id="p1"),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    dunia.antri_press(
        hasil_press("right", 8, request_id="p2"),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    hasil = dunia.jalankan(target=(12, 11), route=(("down", 10), ("right", 8)))
    assert hasil.status == NO_DISPLACEMENT
    assert dunia.press_calls == [("down", 10), ("right", 8)]
    assert dunia.water_calls == []
    assert hasil.segments_completed == 1


# --- session identity pin (Cloud 50B) -------------------------------------

def test_session_id_berubah_antara_segmen_stop_tanpa_segmen_berikutnya():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10, request_id="p1"),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    dunia.antri_press(
        hasil_press("right", 8, request_id="p2"),
        {"tile": {"x": 11, "y": 11}, "pixel": {"x": 704.0, "y": 704.0}},
    )
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[2] = buat_sesi(session_id="sesi-baru")
    hasil = dunia.jalankan(target=(12, 11), route=(("down", 10), ("right", 8)))
    assert hasil.status == SESSION_CHANGED
    assert not hasil.success
    assert dunia.press_calls == [("down", 10)]
    assert dunia.water_calls == []
    assert hasil.segments_completed == 1


def test_session_id_berubah_sebelum_water_zero_water_once():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[3] = buat_sesi(session_id="sesi-baru")
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == SESSION_CHANGED
    assert not hasil.success
    assert dunia.press_calls == [("down", 10)]
    assert dunia.water_calls == []
    assert hasil.segments_completed == 1
    assert hasil.adjacency_achieved is True


def test_player_id_berubah_antara_segmen_stop_sebelum_motor_berikutnya():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10, request_id="p1"),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    dunia.antri_press(
        hasil_press("right", 8, request_id="p2"),
        {"tile": {"x": 11, "y": 11}, "pixel": {"x": 704.0, "y": 704.0}},
    )
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[2] = buat_sesi(player_id=P_LAIN)
    hasil = dunia.jalankan(target=(12, 11), route=(("down", 10), ("right", 8)))
    assert hasil.status == SESSION_CHANGED
    assert not hasil.success
    assert dunia.press_calls == [("down", 10)]
    assert dunia.water_calls == []
    assert hasil.segments_completed == 1


def test_player_id_berubah_sebelum_water_zero_water_once():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10),
        {"tile": {"x": 10, "y": 11}, "pixel": {"x": 640.0, "y": 704.0}},
    )
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[3] = buat_sesi(player_id=P_LAIN)
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == SESSION_CHANGED
    assert not hasil.success
    assert dunia.press_calls == [("down", 10)]
    assert dunia.water_calls == []
    assert hasil.adjacency_achieved is True


# --- 20 / contract guards -------------------------------------------------

def test_press_completed_bukan_bukti_pindah_walau_moved_true():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("right", 8, mentah_timpa={"moved": True, "direction": "right", "press_count": 8}),
        {"tile": {"x": 10, "y": 10}, "pixel": {"x": 640.0, "y": 640.0}},
    )
    hasil = dunia.jalankan(route=(("right", 8),))
    assert hasil.status == NO_DISPLACEMENT
    assert dunia.water_calls == []


def test_press_count_tidak_cocok_bukan_sukses_gerak():
    dunia = DuniaFarm()
    dunia.isi_state(tile={"x": 10, "y": 10}, pixel={"x": 640.0, "y": 640.0})
    dunia.antri_press(
        hasil_press("down", 10, mentah_timpa={"direction": "down", "press_count": 3})
    )
    hasil = dunia.jalankan(route=(("down", 10),))
    assert hasil.status == WORLD_PRESS_FAILED
    assert dunia.water_calls == []


def test_sudah_bersebelahan_tetap_siram_meski_can_player_move_false():
    dunia = DuniaFarm()
    dunia.isi_state(can_player_move=False)
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan(route=())
    assert hasil.success
    assert dunia.press_calls == []
    assert dunia.water_calls == [TARGET]


def test_modul_hanya_orkestrasi_kontrak_lama():
    src = (ROOT / "arti_stardew_farm_v0.py").read_text(encoding="utf-8")
    assert "StardewStateStore" in src
    assert "wait_next" in src
    assert "minta_world_press" in src
    assert "minta_water_once" in src
    assert "arti_bridge" not in src
    assert "win32" not in src.lower()
    rendah = src.lower()
    for terlarang in (
        "inspect_tiles",
        "pathfind",
        "astar",
        "a_star",
        "breadth-first",
        "hotbar",
        "websocket",
    ):
        assert terlarang not in rendah, terlarang
