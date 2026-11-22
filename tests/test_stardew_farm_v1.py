"""Farm Loop v1: temukan satu HoeDirt kering bersebelahan lewat inspect_tiles.

Yang dijaga:

1. satu tetangga kardinal kering ditanam dipilih lalu di-water_once;
2. basah / diagonal / non-HoeDirt / tidak ditanam tidak dipilih;
3. seleksi deterministik (y, x, fact_id);
4. tanpa kandidat atau inspect gagal/identitas mismatch = nol water;
5. sesi/pemain/lokasi pindah sesudah inspect = gagal tertutup;
6. farmer pindah ubin sesudah seleksi = nol water;
7. tanpa state strictly lebih baru sesudah inspect = nol water;
8. sukses driver hanya ObservedWatered; tepat satu inspect, paling banyak satu water;
8. Farm Loop v0 known-target tidak berubah; tidak ada pathfinding.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    INBOX_BUSY,
    MENU_OPEN,
    MISSING_STATE,
    PLAYER_MISMATCH,
    PermintaanFarmV0,
    SESSION_CHANGED,
    SESSION_UNREADABLE,
    STATE_TIMEOUT,
    STALE_STATE,
    SUCCESS,
    WATER_FAILED,
    WRONG_LOCATION,
    jalankan_farm_v0,
)
from arti_stardew_farm_v1 import (  # noqa: E402
    FARMER_MOVED,
    INSPECT_FAILED,
    INSPECT_IDENTITY_MISMATCH,
    INSPECT_REJECTED,
    INSPECT_UNCERTAIN,
    NO_TARGET,
    PermintaanFarmV1,
    RADIUS_INSPEKSI,
    jalankan_farm_v1,
    pilih_target_kering_bersebelahan,
)
from test_stardew_farm_v0 import (  # noqa: E402
    P2,
    P_LAIN,
    TARGET,
    DuniaFarm,
    JamPalsu,
    _event_state,
    buat_sesi as buat_sesi_v0,
    hasil_water,
)

FARMER = (10, 11)
LOKASI = "Farm"


def buat_sesi(**ubah) -> SesiPerintah:
    data = dict(
        session_id="sesi-abc",
        player_id=P2,
        player_name="artihand",
        location_id=LOKASI,
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


def fact_id(x: int, y: int, terrain: str = "HoeDirt", lokasi: str = LOKASI) -> str:
    return f"tf:{lokasi}:{x},{y}:{terrain}"


def fact_terrain(
    x: int,
    y: int,
    *,
    terrain: str = "HoeDirt",
    watered=False,
    phase=1,
    dead=False,
    qualified_item_id="(O)24",
    lokasi: str = LOKASI,
    **lebih,
):
    crop = lebih.pop("crop", None)
    if crop is None and terrain == "HoeDirt":
        crop = {
            "qualified_item_id": qualified_item_id,
            "phase": phase,
            "dead": dead,
            "watered": watered,
            "ready_for_harvest": False,
        }
    isi = {
        "kind": "terrain",
        "terrain_type": terrain,
        "tile": {"x": x, "y": y},
        "passable": True,
        "crop": crop,
    }
    isi.update(lebih)
    return fact_id(x, y, terrain, lokasi), isi


def hasil_inspect(facts: dict | None = None, request_id="inspect-1", **ubah) -> HasilPerintah:
    if facts is None:
        fid, dto = fact_terrain(TARGET[0], TARGET[1], watered=False)
        facts = {fid: dto}
    mentah = {
        "op": "inspect_tiles",
        "status": "completed",
        "success": True,
        "outcome": "Inspected",
        "session_id": "sesi-abc",
        "player_id": P2,
        "location_id": LOKASI,
        "center_x": FARMER[0],
        "center_y": FARMER[1],
        "radius": RADIUS_INSPEKSI,
        "facts": facts,
    }
    mentah.update(ubah.pop("mentah_timpa", {}))
    return HasilPerintah(
        status=ubah.pop("status", SELESAI),
        sukses=ubah.pop("sukses", True),
        request_id=request_id,
        outcome=ubah.pop("outcome", "Inspected"),
        alasan=ubah.pop("alasan", None),
        mentah=mentah,
        **ubah,
    )


class DuniaFarmV1:
    """Store sungguhan + inspect/water palsu. Tidak ada world_press."""

    def __init__(self) -> None:
        self.jam = JamPalsu()
        self.store = StardewStateStore(jam=self.jam)
        self.seq = 1
        self.sesi = buat_sesi()
        self.inspect_calls: list[tuple[int, int, int]] = []
        self.water_calls: list[tuple[int, int]] = []
        self._inspect_antrian: list[tuple[HasilPerintah, dict | None]] = []
        self._water_antrian: list[tuple[HasilPerintah, dict | None]] = []
        self.baca_count = 0
        self.sesi_pada_baca: dict[int, SesiPerintah] = {}

    def isi_state(self, player_id=P2, **payload):
        self.seq += 1
        self.store.catat(_event_state(player_id, seq=self.seq, **payload))
        return self.store.latest(player_id)

    def antri_inspect(self, hasil: HasilPerintah, state_after: dict | None = None):
        self._inspect_antrian.append((hasil, state_after))

    def antri_water(self, hasil: HasilPerintah, state_after: dict | None = None):
        self._water_antrian.append((hasil, state_after))

    def baca_sesi(self, folder=None, *, retry_detik=0.0) -> SesiPerintah:
        self.baca_count += 1
        if self.baca_count in self.sesi_pada_baca:
            self.sesi = self.sesi_pada_baca[self.baca_count]
        return self.sesi

    def minta_inspect_tiles(self, center_x, center_y, radius, **kw) -> HasilPerintah:
        self.inspect_calls.append((center_x, center_y, radius))
        hasil, state_after = self._inspect_antrian.pop(0)
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
        permintaan = PermintaanFarmV1(
            player_id=kw.pop("player_id", P2),
            **kw,
        )
        return jalankan_farm_v1(
            permintaan,
            store=self.store,
            baca_sesi=self.baca_sesi,
            minta_inspect_tiles=self.minta_inspect_tiles,
            minta_water_once=self.minta_water_once,
        )


def _dunia(facts: dict | None = None, *, segar: bool = True) -> DuniaFarmV1:
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(facts), {} if segar else None)
    return dunia


# --- seleksi murni ---------------------------------------------------------

def test_pilih_satu_tetangga_kardinal_kering_ditanam():
    fid, dto = fact_terrain(10, 12, watered=False)
    assert pilih_target_kering_bersebelahan({fid: dto}, FARMER) == (10, 12)


def test_basah_tidak_dipilih():
    fid, dto = fact_terrain(10, 12, watered=True)
    assert pilih_target_kering_bersebelahan({fid: dto}, FARMER) is None


def test_diagonal_tidak_dipilih():
    fid, dto = fact_terrain(11, 12, watered=False)
    assert pilih_target_kering_bersebelahan({fid: dto}, FARMER) is None


def test_non_hoedirt_tidak_dipilih():
    fid, dto = fact_terrain(10, 12, terrain="Flooring", watered=False, crop=None)
    assert pilih_target_kering_bersebelahan({fid: dto}, FARMER) is None


def test_hoedirt_kosong_tidak_dipilih():
    fid, dto = fact_terrain(
        10, 12, watered=False, phase=None, dead=None, qualified_item_id=None
    )
    assert pilih_target_kering_bersebelahan({fid: dto}, FARMER) is None


def test_seleksi_deterministik_y_lalu_x_lalu_fact_id():
    a_id, a = fact_terrain(11, 11, watered=False)
    b_id, b = fact_terrain(9, 11, watered=False)
    c_id, c = fact_terrain(10, 12, watered=False)
    facts = {a_id: a, c_id: c, b_id: b}
    # y=11 x=9 lebih dulu daripada y=11 x=11 dan y=12
    assert pilih_target_kering_bersebelahan(facts, FARMER) == (9, 11)
    assert pilih_target_kering_bersebelahan(facts, FARMER) == (9, 11)


# --- 1. happy path ---------------------------------------------------------

def test_satu_tetangga_kering_ditemukan_lalu_observed_watered():
    dunia = _dunia()
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.success and hasil.status == SUCCESS
    assert hasil.selected_target == TARGET
    assert hasil.inspect_attempted is True
    assert hasil.inspect_outcome == "Inspected"
    assert dunia.inspect_calls == [(FARMER[0], FARMER[1], RADIUS_INSPEKSI)]
    assert dunia.water_calls == [TARGET]
    assert hasil.water_outcome == "ObservedWatered"


def test_already_watered_bukan_sukses_driver():
    dunia = _dunia()
    dunia.antri_water(
        hasil_water(sukses=False, outcome="AlreadyWatered", alasan="outcome=AlreadyWatered")
    )
    hasil = dunia.jalankan()
    assert not hasil.success
    assert hasil.status == WATER_FAILED
    assert hasil.water_outcome == "AlreadyWatered"
    assert dunia.water_calls == [TARGET]
    assert len(dunia.inspect_calls) == 1


# --- 2–5. filter + determinisme end-to-end --------------------------------

def test_hoedirt_basah_nol_water():
    fid, dto = fact_terrain(10, 12, watered=True)
    dunia = _dunia({fid: dto})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == NO_TARGET
    assert not hasil.success
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_diagonal_kering_nol_water():
    fid, dto = fact_terrain(11, 12, watered=False)
    dunia = _dunia({fid: dto})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == NO_TARGET
    assert dunia.water_calls == []


def test_flooring_bukan_hoedirt_nol_water():
    fid, dto = fact_terrain(10, 12, terrain="Flooring", crop=None)
    dunia = _dunia({fid: dto})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == NO_TARGET
    assert dunia.water_calls == []


def test_banyak_kandidat_kardinal_pilih_y_x_fact_id():
    kiri, kiri_dto = fact_terrain(9, 11, watered=False)
    kanan, kanan_dto = fact_terrain(11, 11, watered=False)
    bawah, bawah_dto = fact_terrain(10, 12, watered=False)
    dunia = _dunia({kanan: kanan_dto, bawah: bawah_dto, kiri: kiri_dto})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.success
    assert hasil.selected_target == (9, 11)
    assert dunia.water_calls == [(9, 11)]
    assert len(dunia.inspect_calls) == 1
    assert len(dunia.water_calls) == 1


def test_tanpa_kandidat_nol_water():
    dunia = _dunia({})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == NO_TARGET
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


# --- 7–8. inspect gagal / identitas ----------------------------------------

def test_inspect_ditolak_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(
            status=DITOLAK,
            sukses=False,
            outcome=None,
            alasan="LocationMismatch",
            mentah_timpa={"status": "rejected", "success": False, "outcome": None},
        )
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_REJECTED
    assert not hasil.success
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_inspect_tidak_pasti_nol_retry_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(
            status=TIDAK_PASTI,
            sukses=False,
            outcome=None,
            alasan="hasil tidak datang",
        )
    )
    dunia.antri_inspect(hasil_inspect())
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_UNCERTAIN
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_inspect_hasil_bukan_inspected_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(sukses=False, outcome="Interrupted", alasan="inspect_capture_failed")
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_FAILED
    assert dunia.water_calls == []


def test_inspect_session_mismatch_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(
            sukses=False,
            alasan="identitas hasil tidak cocok dengan permintaan",
            mentah_timpa={"session_id": "sesi-lain"},
        )
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_IDENTITY_MISMATCH
    assert dunia.water_calls == []


def test_inspect_player_id_hasil_berbeda_nol_water():
    """Wrapper inspect tidak mem-pin player_id hasil; v1 wajib gagal tertutup."""
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(mentah_timpa={"player_id": P_LAIN}))
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_IDENTITY_MISMATCH
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_inspect_location_hasil_berbeda_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(
            sukses=False,
            alasan="identitas hasil tidak cocok dengan permintaan",
            mentah_timpa={"location_id": "FarmHouse"},
        )
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_IDENTITY_MISMATCH
    assert dunia.water_calls == []


def test_inspect_facts_bukan_dict_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(mentah_timpa={"facts": [{"kind": "terrain"}]}))
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_FAILED
    assert dunia.water_calls == []


# --- 9–10. pin berubah / farmer pindah ------------------------------------

def test_session_id_berubah_sesudah_inspect_nol_water():
    dunia = _dunia()
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[2] = buat_sesi(session_id="sesi-baru")
    hasil = dunia.jalankan()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1
    assert hasil.selected_target == TARGET


def test_player_id_berubah_sesudah_inspect_nol_water():
    dunia = _dunia()
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[2] = buat_sesi(player_id=P_LAIN)
    hasil = dunia.jalankan()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == []


def test_location_id_berubah_sesudah_inspect_nol_water():
    dunia = _dunia()
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[2] = buat_sesi(location_id="FarmHouse")
    hasil = dunia.jalankan()
    assert hasil.status == SESSION_CHANGED
    assert dunia.water_calls == []


def test_farmer_pindah_ubin_sesudah_inspect_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(),
        {
            "tile": {"x": 10, "y": 12},
            "pixel": {"x": 640.0, "y": 768.0},
        },
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == FARMER_MOVED
    assert not hasil.success
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_tanpa_state_lebih_baru_nol_water():
    dunia = _dunia(segar=False)
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan(state_timeout=0.0)
    assert hasil.status == STATE_TIMEOUT
    assert not hasil.success
    assert hasil.selected_target == TARGET
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_state_baru_ubin_sama_boleh_water():
    dunia = DuniaFarmV1()
    sebelum = dunia.isi_state()
    dunia.antri_inspect(hasil_inspect(), {})
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.success and hasil.status == SUCCESS
    assert dunia.water_calls == [TARGET]
    assert hasil.tile == FARMER
    assert hasil.revision is not None and hasil.revision > sebelum.revisi


def test_state_baru_ubin_pindah_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(),
        {"tile": {"x": 11, "y": 11}, "pixel": {"x": 704.0, "y": 704.0}},
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == FARMER_MOVED
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


def test_sesi_berubah_sesudah_state_segar_nol_water():
    dunia = _dunia()
    dunia.antri_water(hasil_water())
    dunia.sesi_pada_baca[2] = buat_sesi(session_id="sesi-baru", location_id="Town")
    hasil = dunia.jalankan()
    assert hasil.status == SESSION_CHANGED
    assert hasil.selected_target == TARGET
    assert dunia.water_calls == []
    assert len(dunia.inspect_calls) == 1


# --- gerbang awal, nol inspect --------------------------------------------

def test_state_hilang_nol_inspect_nol_water():
    dunia = DuniaFarmV1()
    hasil = dunia.jalankan()
    assert hasil.status == MISSING_STATE
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []


def test_state_basi_nol_inspect():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.jam.maju(0.6)
    hasil = dunia.jalankan(max_age=0.5)
    assert hasil.status == STALE_STATE
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []


def test_player_mismatch_nol_inspect():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.sesi = buat_sesi(player_id=P_LAIN)
    hasil = dunia.jalankan(player_id=P2)
    assert hasil.status == PLAYER_MISMATCH
    assert dunia.inspect_calls == []


def test_session_tanpa_location_id_nol_inspect():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.sesi = buat_sesi(location_id="")
    hasil = dunia.jalankan()
    assert hasil.status == SESSION_UNREADABLE
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []


def test_lokasi_bukan_farm_nol_inspect():
    dunia = DuniaFarmV1()
    dunia.isi_state(location="FarmHouse")
    hasil = dunia.jalankan()
    assert hasil.status == WRONG_LOCATION
    assert dunia.inspect_calls == []


def test_menu_terbuka_nol_inspect():
    dunia = DuniaFarmV1()
    dunia.isi_state(active_menu="GameMenu")
    hasil = dunia.jalankan()
    assert hasil.status == MENU_OPEN
    assert dunia.inspect_calls == []


def test_inbox_inflight_nol_inspect():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.sesi = buat_sesi(inflight_request_id="masih-jalan")
    hasil = dunia.jalankan()
    assert hasil.status == INBOX_BUSY
    assert dunia.inspect_calls == []
    assert dunia.water_calls == []


# --- 11–12. satu inspect, paling banyak satu water, tidak retry -----------

def test_tepat_satu_inspect_paling_banyak_satu_water():
    dunia = _dunia()
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.success
    assert len(dunia.inspect_calls) == 1
    assert len(dunia.water_calls) == 1
    assert dunia.inspect_calls[0][2] == 1


def test_inspect_gagal_lokal_sebelum_tulis_nol_water():
    dunia = DuniaFarmV1()
    dunia.isi_state()
    dunia.antri_inspect(
        hasil_inspect(
            status=GAGAL_LOKAL,
            sukses=False,
            outcome=None,
            alasan="expected_location_id harus string non-kosong",
        )
    )
    dunia.antri_water(hasil_water())
    hasil = dunia.jalankan()
    assert hasil.status == INSPECT_REJECTED
    assert dunia.water_calls == []


# --- 13. v0 known-target tetap --------------------------------------------

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


def test_modul_v0_tetap_tanpa_inspect():
    src = (ROOT / "arti_stardew_farm_v0.py").read_text(encoding="utf-8")
    assert "inspect_tiles" not in src.lower()
    assert "minta_water_once" in src
    assert "minta_world_press" in src


# --- 14. tidak ada semantik pathfinding -----------------------------------

def test_modul_v1_tanpa_rute_atau_semantik_jalan():
    src = (ROOT / "arti_stardew_farm_v1.py").read_text(encoding="utf-8")
    rendah = src.lower()
    for terlarang in (
        "pathfind",
        "astar",
        "a_star",
        "breadth-first",
        "world_press",
        "minta_world_press",
        "hotbar",
        "websocket",
        "walkable",
    ):
        assert terlarang not in rendah, terlarang
    assert "inspect_tiles" in rendah
    assert "minta_water_once" in src
    assert "passable" not in rendah


def test_tidak_memakai_objek_sebagai_target():
    objek = {
        "obj:Farm:10,12:Chest": {
            "kind": "object",
            "qualified_item_id": "(BC)130",
            "tile": {"x": 10, "y": 12},
        }
    }
    fid, dto = fact_terrain(10, 12, watered=False)
    assert pilih_target_kering_bersebelahan(objek, FARMER) is None
    assert pilih_target_kering_bersebelahan({**objek, fid: dto}, FARMER) == (10, 12)
