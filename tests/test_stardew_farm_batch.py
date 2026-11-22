"""Farm Loop batch: sampai empat tetangga kardinal, reuse v1, tanpa gerak.

Yang dijaga:

1. 2..4 HoeDirt kering ditanam bersebelahan disiram berurutan dengan
   inspect segar tiap iterasi;
2. basah / kosong / diagonal tetap memakai aturan kelayakan v1;
3. NO_TARGET di iterasi pertama atau berikutnya menghentikan tanpa water
   tambahan;
4. session/player/location pecah antar-iterasi DAN di admisi perintah
   terakhir = STOP sebelum motor berikutnya;
5. farmer pindah antar-iterasi atau saat seleksi = tanpa water berikutnya;
6. state hilang/basi/timeout = tanpa water berikutnya;
7. target yang sudah dicoba tidak di-water lagi meski inspect masih kering;
8. water tidak pasti/ditolak = STOP tanpa retry;
9. cap aksi, deadline, dan batal mencegah permintaan tambahan;
10. gagal belakangan menyimpan sukses sebelumnya; success batch bukan
    klaim seluruh Farm;
11. v0/v1 tidak berubah; tidak ada world_press/pathfinding.
"""
from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from arti_stardew_command import (  # noqa: E402
    DITOLAK,
    TIDAK_PASTI,
)
from arti_stardew_farm_v0 import (  # noqa: E402
    MISSING_STATE,
    PLAYER_MISMATCH,
    PermintaanFarmV0,
    SESSION_CHANGED,
    STATE_TIMEOUT,
    STALE_STATE,
    SUCCESS,
    WATER_FAILED,
    WATER_REJECTED,
    WATER_UNCERTAIN,
    jalankan_farm_v0,
)
from arti_stardew_farm_v1 import (  # noqa: E402
    FARMER_MOVED,
    NO_TARGET,
    PermintaanFarmV1,
    RADIUS_INSPEKSI,
    jalankan_farm_v1,
    pilih_target_kering_bersebelahan,
)
from arti_stardew_farm_batch import (  # noqa: E402
    AKSI_MAKS,
    CANCELLED,
    DEADLINE_EXCEEDED,
    DEADLINE_MAKS,
    DUPLICATE_TARGET,
    INVALID_REQUEST,
    LIMIT_REACHED,
    PermintaanFarmBatch,
    jalankan_farm_batch,
)
from test_stardew_farm_v0 import (  # noqa: E402
    P2,
    P_LAIN,
    TARGET,
    DuniaFarm,
    JamPalsu,
    buat_sesi as buat_sesi_v0,
    hasil_water,
)
from test_stardew_farm_v1 import (  # noqa: E402
    FARMER,
    LOKASI,
    DuniaFarmV1,
    buat_sesi,
    fact_terrain,
    hasil_inspect,
)

# Urutan seleksi v1: (y, x, fact_id). Farmer (10,11).
ATAS = (10, 10)
KIRI = (9, 11)
KANAN = (11, 11)
BAWAH = (10, 12)
URUTAN = (ATAS, KIRI, KANAN, BAWAH)


def facts_kering(*ubin: tuple[int, int]) -> dict:
    isi = {}
    for x, y in ubin:
        fid, dto = fact_terrain(x, y, watered=False)
        isi[fid] = dto
    return isi


class DuniaBatch(DuniaFarmV1):
    """Store sungguhan + inspect/water palsu. Satu jam admisi terpisah."""

    def __init__(self) -> None:
        super().__init__()
        self.jam_admisi = JamPalsu()
        self.ganti_sesi_setelah_water = None
        self.ganti_sesi_setelah_inspect = None
        self.pindah_ubin_setelah_water = None
        # Transisi di dalam baca_sesi milik wrapper admisi batch (bukan v1).
        # inspect: pin + loop + v1-start + inspect_batch = panggilan ke-4
        #          (iterasi berikutnya tanpa pin: loop + v1-start + inspect = 3).
        # water: v1 post-inspect dulu, baru water_batch = panggilan ke-2
        #        sesudah inspect_calls == water_calls + 1.
        self.saat_baca_admisi_inspect = None
        self.saat_baca_admisi_water = None
        self._n_baca_pre_inspect = 0
        self._n_baca_pre_water = 0

    def baca_sesi(self, folder=None, *, retry_detik=0.0):
        n_ins = len(self.inspect_calls)
        n_water = len(self.water_calls)
        if n_ins == n_water:
            self._n_baca_pre_inspect += 1
            self._n_baca_pre_water = 0
            ambang = 4 if n_ins == 0 else 3
            if (
                self._n_baca_pre_inspect == ambang
                and self.saat_baca_admisi_inspect is not None
            ):
                self.saat_baca_admisi_inspect(self)
        elif n_ins == n_water + 1:
            self._n_baca_pre_water += 1
            self._n_baca_pre_inspect = 0
            if (
                self._n_baca_pre_water == 2
                and self.saat_baca_admisi_water is not None
            ):
                self.saat_baca_admisi_water(self)
        return super().baca_sesi(folder)

    def minta_inspect_tiles(self, center_x, center_y, radius, **kw):
        hasil = super().minta_inspect_tiles(center_x, center_y, radius, **kw)
        if self.ganti_sesi_setelah_inspect is not None:
            self.sesi = self.ganti_sesi_setelah_inspect
            self.ganti_sesi_setelah_inspect = None
        return hasil

    def minta_water_once(self, tile_x, tile_y, **kw):
        hasil = super().minta_water_once(tile_x, tile_y, **kw)
        if self.ganti_sesi_setelah_water is not None:
            self.sesi = self.ganti_sesi_setelah_water
            self.ganti_sesi_setelah_water = None
        if self.pindah_ubin_setelah_water is not None:
            self.isi_state(**self.pindah_ubin_setelah_water)
            self.pindah_ubin_setelah_water = None
        return hasil

    def jalankan_batch(self, **kw):
        jam = kw.pop("jam", self.jam_admisi)
        permintaan = PermintaanFarmBatch(
            player_id=kw.pop("player_id", P2),
            **kw,
        )
        return jalankan_farm_batch(
            permintaan,
            store=self.store,
            baca_sesi=self.baca_sesi,
            minta_inspect_tiles=self.minta_inspect_tiles,
            minta_water_once=self.minta_water_once,
            jam=jam,
        )


def _antri_siram(dunia: DuniaBatch, ubin: tuple[tuple[int, int], ...], *, segar=True):
    """Antri inspect facts menyusut + ObservedWatered per target."""
    sisa = list(ubin)
    for i, target in enumerate(ubin):
        dunia.antri_inspect(
            hasil_inspect(facts_kering(*sisa), request_id=f"inspect-{i + 1}"),
            {} if segar else None,
        )
        dunia.antri_water(hasil_water(request_id=f"water-{i + 1}"), {})
        sisa = sisa[1:]
        assert sisa or target == ubin[-1]


# --- import / validasi -----------------------------------------------------

def test_import_modul_tanpa_io(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    importlib.reload(importlib.import_module("arti_stardew_farm_batch"))
    assert list(tmp_path.iterdir()) == []


def test_max_actions_bool_ditolak_tanpa_inspect():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect())
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan_batch(max_actions=True)
    assert hasil.status == INVALID_REQUEST
    assert not hasil.success
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []


def test_max_actions_nol_dan_lima_ditolak():
    dunia = DuniaBatch()
    dunia.isi_state()
    for n in (0, 5, -1):
        dunia.inspect_calls.clear()
        hasil = dunia.jalankan_batch(max_actions=n)
        assert hasil.status == INVALID_REQUEST
        assert dunia.inspect_calls == []


def test_deadline_di_atas_maks_atau_tidak_hingga_ditolak():
    dunia = DuniaBatch()
    dunia.isi_state()
    for nilai in (DEADLINE_MAKS + 1, 0, -1, math.inf, math.nan, True):
        hasil = dunia.jalankan_batch(deadline_seconds=nilai)
        assert hasil.status == INVALID_REQUEST
        assert dunia.inspect_calls == []


# --- 2..4 target segar -----------------------------------------------------

def test_dua_tetangga_kering_berurutan_inspect_segar():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, (ATAS, KIRI))
    dunia.antri_inspect(hasil_inspect({}, request_id="inspect-3"), {})
    hasil = dunia.jalankan_batch(max_actions=4)
    assert hasil.status == NO_TARGET
    assert hasil.success
    assert hasil.completed_targets == (ATAS, KIRI)
    assert dunia.inspect_calls == [
        (FARMER[0], FARMER[1], RADIUS_INSPEKSI),
        (FARMER[0], FARMER[1], RADIUS_INSPEKSI),
        (FARMER[0], FARMER[1], RADIUS_INSPEKSI),
    ]
    assert dunia.water_calls == [ATAS, KIRI]
    assert hasil.inspect_count == 3
    assert hasil.water_count == 2
    assert hasil.pin_session_id == "sesi-abc"
    assert hasil.pin_player_id == P2
    assert hasil.pin_location_id == LOKASI
    assert hasil.pin_tile == FARMER


def test_empat_tetangga_limit_reached_bukan_seluruh_farm():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, URUTAN)
    # inspect ke-5 tidak boleh terpanggil meski antrian masih ada
    dunia.antri_inspect(hasil_inspect(facts_kering((10, 9)), request_id="inspect-5"))
    dunia.antri_water(hasil_water(request_id="water-5"))
    hasil = dunia.jalankan_batch(max_actions=4)
    assert hasil.status == LIMIT_REACHED
    assert hasil.success
    assert hasil.completed_targets == URUTAN
    assert dunia.water_calls == list(URUTAN)
    assert len(dunia.inspect_calls) == 4
    assert len(dunia.water_calls) == 4
    assert hasil.inspect_count == 4
    assert hasil.water_count == 4


def test_cap_dua_mencegah_water_ketiga():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, URUTAN)
    hasil = dunia.jalankan_batch(max_actions=2)
    assert hasil.status == LIMIT_REACHED
    assert hasil.completed_targets == (ATAS, KIRI)
    assert dunia.water_calls == [ATAS, KIRI]
    assert len(dunia.inspect_calls) == 2


# --- kelayakan v1 ----------------------------------------------------------

def test_basah_kosong_diagonal_tidak_dipilih():
    farmer = FARMER
    basah_id, basah = fact_terrain(10, 12, watered=True)
    kosong_id, kosong = fact_terrain(
        9, 11, watered=False, phase=None, dead=None, qualified_item_id=None
    )
    diagonal_id, diagonal = fact_terrain(11, 12, watered=False)
    facts = {basah_id: basah, kosong_id: kosong, diagonal_id: diagonal}
    assert pilih_target_kering_bersebelahan(facts, farmer) is None

    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts), {})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan_batch()
    assert hasil.status == NO_TARGET
    assert not hasil.success
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_no_target_iterasi_pertama_tanpa_water():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect({}), {})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan_batch()
    assert hasil.status == NO_TARGET
    assert not hasil.success
    assert hasil.completed_targets == ()
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_no_target_iterasi_berikutnya_menyimpan_sukses():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="i1"), {})
    dunia.antri_water(hasil_water(request_id="w1"), {})
    dunia.antri_inspect(hasil_inspect({}, request_id="i2"), {})
    dunia.antri_water(hasil_water(request_id="w2"))
    hasil = dunia.jalankan_batch()
    assert hasil.status == NO_TARGET
    assert hasil.success
    assert hasil.completed_targets == (ATAS,)
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 2


# --- pin identitas / ubin --------------------------------------------------

def test_session_berubah_antar_iterasi_tanpa_inspect_kedua():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, (ATAS, KIRI))
    dunia.ganti_sesi_setelah_water = buat_sesi(session_id="sesi-baru")
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert hasil.completed_targets == (ATAS,)
    assert not hasil.success
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1
    assert hasil.pin_session_id == "sesi-abc"


def test_player_berubah_antar_iterasi_tanpa_inspect_kedua():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, (ATAS, KIRI))
    dunia.ganti_sesi_setelah_water = buat_sesi(player_id=P_LAIN)
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_location_berubah_antar_iterasi_tanpa_inspect_kedua():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, (ATAS, KIRI))
    dunia.ganti_sesi_setelah_water = buat_sesi(location_id="FarmHouse")
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_session_berubah_di_admisi_water_tanpa_water():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS)), {})
    dunia.antri_water(hasil_water())
    dunia.ganti_sesi_setelah_inspect = buat_sesi(session_id="sesi-baru")
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1
    assert hasil.iterations[0].selected_target == ATAS


def test_farmer_pindah_antar_iterasi_tanpa_water_kedua():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, (ATAS, KIRI))
    dunia.pindah_ubin_setelah_water = {
        "tile": {"x": 12, "y": 11},
        "pixel": {"x": 768.0, "y": 704.0},
    }
    hasil = dunia.jalankan_batch()
    assert hasil.status == FARMER_MOVED
    assert hasil.completed_targets == (ATAS,)
    assert not hasil.success
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_farmer_pindah_saat_seleksi_tanpa_water():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(facts_kering(ATAS)),
        {"tile": {"x": 10, "y": 12}, "pixel": {"x": 640.0, "y": 768.0}},
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan_batch()
    assert hasil.status == FARMER_MOVED
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_state_hilang_tanpa_inspect():
    dunia = DuniaBatch()
    hasil = dunia.jalankan_batch()
    assert hasil.status == MISSING_STATE
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []


def test_state_basi_antar_iterasi_tanpa_inspect_kedua():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS)), {})
    dunia.antri_water(hasil_water(), {})
    dunia.antri_inspect(hasil_inspect(facts_kering(KIRI)), {})
    dunia.antri_water(hasil_water())
    asal = dunia.minta_water_once

    def water_lalu_basi(tile_x, tile_y, **kw):
        hasil = asal(tile_x, tile_y, **kw)
        dunia.jam.maju(0.6)
        return hasil

    dunia.minta_water_once = water_lalu_basi
    hasil = dunia.jalankan_batch(max_age=0.5)
    assert hasil.status == STALE_STATE
    assert hasil.completed_targets == (ATAS,)
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_timeout_state_baru_tanpa_water():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS)), None)
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan_batch(state_timeout=0.0)
    assert hasil.status == STATE_TIMEOUT
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


# --- at-most-once ----------------------------------------------------------

def test_target_yang_sudah_dicoba_tidak_di_water_ulang():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="i1"), {})
    dunia.antri_water(hasil_water(request_id="w1"), {})
    # inspect kedua masih mengembalikan ATAS kering — facts tidak ditulis ulang
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="i2"), {})
    dunia.antri_water(hasil_water(request_id="w2"))
    hasil = dunia.jalankan_batch()
    assert hasil.status == DUPLICATE_TARGET
    assert hasil.completed_targets == (ATAS,)
    assert not hasil.success
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 2
    assert hasil.iterations[-1].selected_target == ATAS


# --- water gagal / uncertainty --------------------------------------------

def test_water_tidak_pasti_tanpa_retry():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS)), {})
    dunia.antri_water(
        hasil_water(status=TIDAK_PASTI, sukses=False, outcome=None, alasan="hasil tidak datang")
    )
    dunia.antri_inspect(hasil_inspect(facts_kering(KIRI)), {})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan_batch()
    assert hasil.status == WATER_UNCERTAIN
    assert not hasil.success
    assert hasil.completed_targets == ()
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_water_ditolak_tanpa_retry():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS)), {})
    dunia.antri_water(
        hasil_water(
            status=DITOLAK,
            sukses=False,
            outcome=None,
            alasan="LocationMismatch",
        )
    )
    dunia.antri_inspect(hasil_inspect(facts_kering(KIRI)), {})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan_batch()
    assert hasil.status == WATER_REJECTED
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_gagal_belakangan_menyimpan_sukses_awal():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS, KIRI), request_id="i1"), {})
    dunia.antri_water(hasil_water(request_id="w1"), {})
    dunia.antri_inspect(hasil_inspect(facts_kering(KIRI), request_id="i2"), {})
    dunia.antri_water(
        hasil_water(
            request_id="w2",
            sukses=False,
            outcome="AlreadyWatered",
            alasan="outcome=AlreadyWatered",
        )
    )
    dunia.antri_inspect(hasil_inspect(facts_kering(KANAN), request_id="i3"), {})
    dunia.antri_water(hasil_water(request_id="w3"))
    hasil = dunia.jalankan_batch()
    assert hasil.status == WATER_FAILED
    assert not hasil.success
    assert hasil.completed_targets == (ATAS,)
    assert dunia.water_calls == [ATAS, KIRI]
    assert len(dunia.inspect_calls) == 2
    assert hasil.iterations[0].success
    assert not hasil.iterations[1].success


# --- deadline / batal ------------------------------------------------------

def test_deadline_mencegah_inspect_berikutnya():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, (ATAS, KIRI))
    asal = dunia.minta_water_once

    def water_lalu_habis(tile_x, tile_y, **kw):
        hasil = asal(tile_x, tile_y, **kw)
        dunia.jam_admisi.maju(16.0)
        return hasil

    dunia.minta_water_once = water_lalu_habis
    hasil = dunia.jalankan_batch(deadline_seconds=15.0)
    assert hasil.status == DEADLINE_EXCEEDED
    assert hasil.completed_targets == (ATAS,)
    assert not hasil.success
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_batal_mencegah_inspect_berikutnya():
    dunia = DuniaBatch()
    dunia.isi_state()
    _antri_siram(dunia, (ATAS, KIRI))
    n = {"water": 0}

    def cek_batal():
        return n["water"] >= 1

    asal = dunia.minta_water_once

    def water_hitung(tile_x, tile_y, **kw):
        hasil = asal(tile_x, tile_y, **kw)
        n["water"] += 1
        return hasil

    dunia.minta_water_once = water_hitung
    hasil = dunia.jalankan_batch(cek_batal=cek_batal)
    assert hasil.status == CANCELLED
    assert hasil.completed_targets == (ATAS,)
    assert dunia.water_calls == [ATAS]
    assert len(dunia.inspect_calls) == 1


def test_deadline_saat_baca_sesi_inspect_final_nol_perintah():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS)), {})
    dunia.antri_water(hasil_water())

    def habiskan(d):
        d.jam_admisi.t = 1000.0 + 15.1

    dunia.saat_baca_admisi_inspect = habiskan
    hasil = dunia.jalankan_batch(deadline_seconds=15.0)
    assert hasil.status == DEADLINE_EXCEEDED
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []
    assert hasil.inspect_count == 0
    assert hasil.water_count == 0
    assert not hasil.success


def test_batal_saat_baca_sesi_inspect_final_nol_perintah():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS)), {})
    dunia.antri_water(hasil_water())
    dunia.batal_aktif = False

    def cek_batal():
        return dunia.batal_aktif

    def nyalakan(d):
        d.batal_aktif = True

    dunia.saat_baca_admisi_inspect = nyalakan
    hasil = dunia.jalankan_batch(cek_batal=cek_batal)
    assert hasil.status == CANCELLED
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []
    assert hasil.inspect_count == 0
    assert hasil.water_count == 0


def test_deadline_saat_baca_sesi_water_final_nol_water():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="inspect-1"), {})
    dunia.antri_water(hasil_water())

    def habiskan(d):
        d.jam_admisi.t = 1000.0 + 15.1

    dunia.saat_baca_admisi_water = habiskan
    hasil = dunia.jalankan_batch(deadline_seconds=15.0)
    assert hasil.status == DEADLINE_EXCEEDED
    assert len(dunia.inspect_calls) == 1
    assert dunia.water_calls == []
    assert hasil.inspect_count == 1
    assert hasil.water_count == 0
    assert hasil.iterations[-1].selected_target == ATAS
    assert hasil.iterations[-1].inspect_request_id == "inspect-1"
    assert hasil.iterations[-1].inspect_outcome == "Inspected"
    assert hasil.iterations[-1].water_outcome is None
    assert not hasil.success


def test_batal_saat_baca_sesi_water_final_nol_water():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="inspect-1"), {})
    dunia.antri_water(hasil_water())
    dunia.batal_aktif = False

    def cek_batal():
        return dunia.batal_aktif

    def nyalakan(d):
        d.batal_aktif = True

    dunia.saat_baca_admisi_water = nyalakan
    hasil = dunia.jalankan_batch(cek_batal=cek_batal)
    assert hasil.status == CANCELLED
    assert len(dunia.inspect_calls) == 1
    assert dunia.water_calls == []
    assert hasil.water_count == 0
    assert hasil.iterations[-1].selected_target == ATAS
    assert hasil.iterations[-1].inspect_request_id == "inspect-1"
    assert hasil.iterations[-1].inspect_outcome == "Inspected"
    assert hasil.iterations[-1].water_outcome is None


def test_session_hanya_pecah_di_admisi_water_batch_menyimpan_inspect():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="inspect-1"), {})
    dunia.antri_water(hasil_water())

    def ganti(d):
        d.sesi = buat_sesi(session_id="sesi-baru")

    dunia.saat_baca_admisi_water = ganti
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == []
    assert hasil.water_count == 0
    assert len(dunia.inspect_calls) == 1
    terakhir = hasil.iterations[-1]
    assert terakhir.selected_target == ATAS
    assert terakhir.inspect_request_id == "inspect-1"
    assert terakhir.inspect_outcome == "Inspected"
    assert terakhir.water_outcome is None
    assert terakhir.terminal_request_id == "inspect-1"
    assert terakhir.terminal_outcome == "Inspected"


def test_player_hanya_pecah_di_admisi_water_batch_menyimpan_inspect():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="inspect-1"), {})
    dunia.antri_water(hasil_water())

    def ganti(d):
        d.sesi = buat_sesi(player_id=P_LAIN)

    dunia.saat_baca_admisi_water = ganti
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == []
    assert hasil.iterations[-1].selected_target == ATAS
    assert hasil.iterations[-1].inspect_outcome == "Inspected"
    assert hasil.iterations[-1].water_outcome is None


def test_location_hanya_pecah_di_admisi_water_batch_menyimpan_inspect():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="inspect-1"), {})
    dunia.antri_water(hasil_water())

    def ganti(d):
        d.sesi = buat_sesi(location_id="FarmHouse")

    dunia.saat_baca_admisi_water = ganti
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == []
    assert hasil.iterations[-1].selected_target == ATAS
    assert hasil.iterations[-1].inspect_outcome == "Inspected"
    assert hasil.iterations[-1].water_outcome is None


def test_ubin_hanya_berubah_di_admisi_water_batch_menyimpan_inspect():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS), request_id="inspect-1"), {})
    dunia.antri_water(hasil_water())

    def pindah(d):
        d.isi_state(tile={"x": 12, "y": 11}, pixel={"x": 768.0, "y": 704.0})

    dunia.saat_baca_admisi_water = pindah
    hasil = dunia.jalankan_batch()
    assert hasil.status == FARMER_MOVED
    assert dunia.water_calls == []
    assert hasil.water_count == 0
    assert hasil.iterations[-1].selected_target == ATAS
    assert hasil.iterations[-1].inspect_request_id == "inspect-1"
    assert hasil.iterations[-1].inspect_outcome == "Inspected"
    assert hasil.iterations[-1].water_outcome is None


def test_admisi_water_batch_menyimpan_sukses_sebelumnya():
    dunia = DuniaBatch()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts_kering(ATAS, KIRI), request_id="inspect-1"), {})
    dunia.antri_water(hasil_water(request_id="water-1"), {})
    dunia.antri_inspect(hasil_inspect(facts_kering(KIRI), request_id="inspect-2"), {})
    dunia.antri_water(hasil_water(request_id="water-2"))

    def ganti(d):
        if len(d.water_calls) == 1:
            d.sesi = buat_sesi(session_id="sesi-baru")

    dunia.saat_baca_admisi_water = ganti
    hasil = dunia.jalankan_batch()
    assert hasil.status == SESSION_CHANGED
    assert hasil.completed_targets == (ATAS,)
    assert not hasil.success
    assert dunia.water_calls == [ATAS]
    assert hasil.water_count == 1
    assert len(dunia.inspect_calls) == 2
    assert hasil.iterations[0].success
    terakhir = hasil.iterations[-1]
    assert terakhir.selected_target == KIRI
    assert terakhir.inspect_request_id == "inspect-2"
    assert terakhir.inspect_outcome == "Inspected"
    assert terakhir.water_outcome is None
    assert terakhir.terminal_outcome == "Inspected"


# --- v0/v1 regression ------------------------------------------------------

def test_farm_v1_satu_target_tetap():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(), {})
    dunia.antri_water(hasil_water())
    hasil = jalankan_farm_v1(
        PermintaanFarmV1(player_id=P2),
        store=dunia.store,
        baca_sesi=dunia.baca_sesi,
        minta_inspect_tiles=dunia.minta_inspect_tiles,
        minta_water_once=dunia.minta_water_once,
    )
    assert hasil.success and hasil.status == SUCCESS
    assert dunia.water_calls == [TARGET]
    assert len(dunia.inspect_calls) == 1


def test_farm_v0_known_target_tetap_tanpa_inspect():
    dunia = DuniaFarm()
    dunia.isi_state()
    dunia.sesi = buat_sesi_v0()
    dunia.antri_water(hasil_water())
    hasil = jalankan_farm_v0(
        PermintaanFarmV0(target=TARGET, route=(), player_id=P2, state_timeout=0.0),
        store=dunia.store,
        baca_sesi=dunia.baca_sesi,
        minta_world_press=dunia.minta_world_press,
        minta_water_once=dunia.minta_water_once,
    )
    assert hasil.success and hasil.status == SUCCESS
    assert dunia.press_calls == []
    assert dunia.water_calls == [TARGET]


def test_modul_batch_tanpa_gerak_atau_pathfinding():
    src = (ROOT / "arti_stardew_farm_batch.py").read_text(encoding="utf-8")
    rendah = src.lower()
    for terlarang in (
        "pathfind",
        "astar",
        "a_star",
        "world_press",
        "minta_world_press",
        "menu_step",
        "hotbar",
        "refill",
        "websocket",
    ):
        assert terlarang not in rendah, terlarang
    assert "jalankan_farm_v1" in src
    assert "pilih_target_kering_bersebelahan" not in src


def test_sumber_v0_v1_tidak_memuat_batch():
    v0 = (ROOT / "arti_stardew_farm_v0.py").read_text(encoding="utf-8")
    v1 = (ROOT / "arti_stardew_farm_v1.py").read_text(encoding="utf-8")
    assert "farm_batch" not in v0
    assert "farm_batch" not in v1
    assert "jalankan_farm_batch" not in v0
    assert "jalankan_farm_batch" not in v1


def test_aksi_maks_empat():
    assert AKSI_MAKS == 4
