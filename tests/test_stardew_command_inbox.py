"""P5-R3D: satu pemanggilan Python -> satu menu_step -> satu putusan.

Yang dijaga di sini bukan "apakah perintahnya sampai" — itu hanya bisa
dibuktikan dengan game hidup. Yang dijaga adalah hal-hal yang justru TIDAK
terlihat saat live test berjalan mulus:

1. permintaan yang tidak boleh dikirim benar-benar tidak pernah ditulis;
2. terkirim/diantre tidak pernah terbaca sebagai sukses;
3. "tidak terjadi" tidak pernah tertukar dengan "tidak tahu";
4. tidak ada retry otomatis sesudah hasil yang tidak pasti.

Tidak ada satu pun tes di berkas ini yang menyentuh Stardew, SMAPI, atau
socket. Mod-nya dipalsukan dengan menulis berkas hasil.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_stardew_command as C  # noqa: E402


SESI_SIAP = {
    "protocol": 1,
    "ev": "command_session",
    "session_id": "sesi-abc",
    "player_id": 100000000012,
    "player_name": "artihand",
    "screen_id": 0,
    "is_main_player": False,
    "is_split_screen": False,
    "inbox_enabled": True,
    "menu_step_enabled": True,
    "world_ready": True,
    "inflight_request_id": None,
    "input_busy": False,
}


# Berapa lama tes menunggu ModPalsu mengambil berkas permintaan. Margin ~15x
# terhadap jeda pollingnya (0,01 dtk): runner CI yang sibuk tidak boleh membuat
# tes ini merah karena penjadwalan thread, bukan karena logikanya salah.
TUNGGU_MOD = 1.5


def tulis_sesi(folder: Path, **ubah) -> None:
    isi = dict(SESI_SIAP)
    isi.update(ubah)
    (folder / C.NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")


@pytest.fixture
def kotak(tmp_path: Path) -> Path:
    tulis_sesi(tmp_path)
    return tmp_path


def baca_permintaan(folder: Path) -> dict:
    return json.loads((folder / C.NAMA_REQUEST).read_text(encoding="utf-8"))


class ModPalsu:
    """Mod tiruan: ambil permintaan, lalu tulis satu envelope hasil.

    Meniru urutan mod sungguhan — baca, HAPUS berkas permintaan, baru tulis
    hasil — supaya perbedaan antara "[recorded remark omitted]" dan "[recorded remark omitted]" benar-benar teruji.
    """

    def __init__(self, folder: Path, envelope: dict | None, jeda: float = 0.0):
        self.folder = folder
        self.envelope = envelope
        self.jeda = jeda
        self.permintaan: dict | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "ModPalsu":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        req = self.folder / C.NAMA_REQUEST
        while not self._stop.is_set():
            if req.exists():
                self.permintaan = json.loads(req.read_text(encoding="utf-8"))
                req.unlink()
                if self.jeda:
                    time.sleep(self.jeda)
                if self.envelope is not None:
                    isi = dict(self.envelope)
                    isi.setdefault("request_id", self.permintaan["request_id"])
                    tmp = self.folder / (C.NAMA_RESULT + ".tmp")
                    tmp.write_text(json.dumps(isi), encoding="utf-8")
                    tmp.replace(self.folder / C.NAMA_RESULT)
                return
            time.sleep(0.01)


def envelope(**ubah) -> dict:
    isi = {
        "protocol": 1,
        "ev": "command_result",
        "op": "menu_step",
        "session_id": "sesi-abc",
        "accepted": True,
        "status": "completed",
        "reject_reason": None,
        "arm_status": "Started",
        "run_id": 7,
        "outcome": "Stepped",
        "success": True,
        "start_snap_id": 102,
        "end_snap_id": 103,
        "expected_neighbor_id": 103,
        "press_count": 16,
        "completed_unix_ms": 1,
    }
    isi.update(ubah)
    return isi


# --------------------------------------------------------------------------
# Kontrak permintaan
# --------------------------------------------------------------------------


def test_permintaan_membawa_identitas_dan_deadline(kotak: Path):
    with ModPalsu(kotak, envelope()) as mod:
        hasil = C.minta_menu_step("down", folder=kotak, timeout_ticks=42, delay_ticks=5)

    assert hasil.sukses
    req = mod.permintaan
    assert req is not None
    # Nama field ini dibaca MenuStepCommandInbox.TryParse di sisi C#.
    # Mengubah salah satunya di satu sisi wajib menggagalkan sisi lain.
    assert set(req) == {
        "protocol",
        "op",
        "request_id",
        "session_id",
        "expected_player_id",
        "expected_player_name",
        "direction",
        "timeout_ticks",
        "delay_ticks",
        "deadline_unix_ms",
    }
    assert req["protocol"] == 1
    assert req["op"] == "menu_step"
    assert req["session_id"] == SESI_SIAP["session_id"]
    assert req["expected_player_id"] == SESI_SIAP["player_id"]
    assert req["expected_player_name"] == SESI_SIAP["player_name"]
    assert req["direction"] == "down"
    assert req["timeout_ticks"] == 42 and req["delay_ticks"] == 5
    assert req["request_id"] == hasil.request_id
    assert req["deadline_unix_ms"] > C._sekarang_ms() - 1000


def test_setiap_panggilan_memakai_request_id_baru(kotak: Path):
    ids = []
    for _ in range(2):
        with ModPalsu(kotak, envelope()) as mod:
            C.minta_menu_step("down", folder=kotak)
        ids.append(mod.permintaan["request_id"])
    assert ids[0] != ids[1], "satu request_id = satu percobaan"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"arah": "diagonal"},
        {"arah": "down", "timeout_ticks": 0},
        {"arah": "down", "timeout_ticks": C.TIMEOUT_TICKS_MAX + 1},
        {"arah": "down", "delay_ticks": -1},
        {"arah": "down", "delay_ticks": C.DELAY_TICKS_MAX + 1},
    ],
)
def test_argumen_di_luar_batas_probe_tidak_pernah_menulis_berkas(kotak: Path, kwargs):
    arah = kwargs.pop("arah")
    with pytest.raises(C.KesalahanPerintah):
        C.minta_menu_step(arah, folder=kotak, **kwargs)
    assert not (kotak / C.NAMA_REQUEST).exists()


# --------------------------------------------------------------------------
# Gerbang: berhenti SEBELUM menulis apa pun
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ubah,potongan",
    [
        ({"inbox_enabled": False}, "MenuStepCommandInboxEnabled"),
        ({"menu_step_enabled": False}, "ControllerMenuStepEnabled"),
        ({"world_ready": False}, "dunia belum siap"),
        ({"is_main_player": True}, "HOST"),
        ({"is_split_screen": True}, "split-screen"),
        ({"inflight_request_id": "lain"}, "masih ada perintah berjalan"),
        ({"input_busy": True}, "input menu/dunia"),
        ({"session_id": ""}, "session_id"),
    ],
)
def test_sesi_belum_siap_menolak_tanpa_menulis(kotak: Path, ubah, potongan):
    tulis_sesi(kotak, **ubah)
    hasil = C.minta_menu_step("down", folder=kotak, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert not hasil.sukses
    assert potongan in (hasil.alasan or "")
    assert not (kotak / C.NAMA_REQUEST).exists(), "tidak ada berkas = pasti nol input"


def test_slot_terpakai_tidak_ditimpa(kotak: Path):
    (kotak / C.NAMA_REQUEST).write_text('{"request_id":"punya-orang-lain"}', encoding="utf-8")
    hasil = C.minta_menu_step("down", folder=kotak, batas_detik=0.2)
    assert hasil.status == C.GAGAL_LOKAL
    assert not hasil.sukses
    assert "bersihkan_slot" in (hasil.alasan or "")
    assert baca_permintaan(kotak)["request_id"] == "punya-orang-lain"


def test_sesi_hilang_atau_protocol_asing_ditolak(tmp_path: Path):
    with pytest.raises(C.KesalahanPerintah):
        C.baca_sesi(tmp_path)
    (tmp_path / C.NAMA_SESSION).write_text('{"protocol":99}', encoding="utf-8")
    with pytest.raises(C.KesalahanPerintah):
        C.baca_sesi(tmp_path)


def test_folder_tidak_diketahui_ditolak(monkeypatch):
    monkeypatch.delenv("ARTI_STARDEW_COMMAND_DIR", raising=False)
    with pytest.raises(C.KesalahanPerintah):
        C.folder_perintah(None)


# --------------------------------------------------------------------------
# Terkirim bukan sukses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ubah",
    [
        {"outcome": "Unmoved", "success": False},
        {"outcome": "Overshoot", "success": False},
        {"outcome": "Timeout", "success": False},
        {"outcome": "Moved", "success": False},
        {"outcome": "Cleanup", "success": False},
        # Pertahanan: kalau salah satu sinyal bergeser, hasilnya TURUN jadi
        # tidak sukses, bukan naik jadi sukses.
        {"outcome": "Unmoved", "success": True},
        {"outcome": "Stepped", "success": False},
    ],
)
def test_hanya_stepped_yang_boleh_sukses(kotak: Path, ubah):
    with ModPalsu(kotak, envelope(**ubah)):
        hasil = C.minta_menu_step("down", folder=kotak)
    assert hasil.status == C.SELESAI
    assert not hasil.sukses
    assert hasil.pasti


def test_stepped_penuh_adalah_sukses(kotak: Path):
    with ModPalsu(kotak, envelope()):
        hasil = C.minta_menu_step("down", folder=kotak)
    assert hasil.status == C.SELESAI and hasil.sukses and hasil.outcome == "Stepped"
    assert hasil.mentah is not None and hasil.mentah["end_snap_id"] == 103


def test_ditolak_membawa_alasan_dan_arm_status(kotak: Path):
    with ModPalsu(
        kotak,
        envelope(
            accepted=False,
            status="rejected",
            reject_reason="ArmRejected",
            arm_status="RejectedNoMenu",
            outcome=None,
            success=False,
        ),
    ):
        hasil = C.minta_menu_step("down", folder=kotak)
    assert hasil.status == C.DITOLAK and not hasil.sukses
    assert "ArmRejected" in hasil.alasan and "RejectedNoMenu" in hasil.alasan


def test_sesi_runtuh_dilaporkan_terputus(kotak: Path):
    with ModPalsu(
        kotak,
        envelope(status="interrupted", reject_reason="ReturnedToTitle", outcome=None, success=False),
    ):
        hasil = C.minta_menu_step("down", folder=kotak)
    assert hasil.status == C.TERPUTUS and not hasil.sukses
    assert "ReturnedToTitle" in (hasil.alasan or "")


def test_status_mod_tak_dikenal_tidak_pernah_sukses(kotak: Path):
    with ModPalsu(kotak, envelope(status="queued", success=True, outcome="Stepped")):
        hasil = C.minta_menu_step("down", folder=kotak)
    assert not hasil.sukses
    assert hasil.status == C.TIDAK_PASTI


# --------------------------------------------------------------------------
# "Tidak terjadi" tidak boleh tertukar dengan "tidak tahu"
# --------------------------------------------------------------------------


def test_timeout_dengan_permintaan_tertahan_tetap_tidak_pasti(kotak: Path):
    hasil = C.minta_menu_step("down", folder=kotak, batas_detik=0.3)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.sukses and not hasil.pasti
    assert json.loads((kotak / C.NAMA_REQUEST).read_text())["request_id"] == hasil.request_id
    assert "JANGAN ulangi otomatis" in hasil.alasan


def test_sudah_diambil_tanpa_hasil_adalah_tidak_pasti(kotak: Path):
    with ModPalsu(kotak, envelope=None):  # ambil, lalu diam
        hasil = C.minta_menu_step("down", folder=kotak, batas_detik=TUNGGU_MOD)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.sukses
    assert not hasil.pasti
    assert "JANGAN ulangi otomatis" in (hasil.alasan or "")


def test_tidak_pasti_tidak_menulis_permintaan_kedua(kotak: Path):
    with ModPalsu(kotak, envelope=None) as mod:
        C.minta_menu_step("down", folder=kotak, batas_detik=TUNGGU_MOD)
    # Satu permintaan diambil, dan tidak ada berkas baru sesudahnya.
    assert mod.permintaan is not None
    assert not (kotak / C.NAMA_REQUEST).exists()
    assert not (kotak / (C.NAMA_REQUEST + ".tmp")).exists()


def test_hasil_milik_permintaan_lain_tidak_pernah_diklaim(kotak: Path):
    (kotak / C.NAMA_RESULT).write_text(
        json.dumps(envelope(request_id="punya-orang-lain")), encoding="utf-8"
    )
    with ModPalsu(kotak, envelope=None):
        hasil = C.minta_menu_step("down", folder=kotak, batas_detik=TUNGGU_MOD)
    assert hasil.status == C.TIDAK_PASTI
    assert not hasil.sukses


def test_hasil_datang_terlambat_masih_ditunggu(kotak: Path):
    with ModPalsu(kotak, envelope(), jeda=0.3):
        hasil = C.minta_menu_step("down", folder=kotak, batas_detik=2.0)
    assert hasil.sukses


def test_hasil_dipertahankan_dan_request_id_mencegah_pakai_ulang(kotak: Path):
    with ModPalsu(kotak, envelope()):
        result = C.minta_menu_step("down", folder=kotak)
    original = (kotak / C.NAMA_RESULT).read_bytes()
    assert json.loads(original)["request_id"] == result.request_id
    assert C._tunggu_hasil(kotak,"foreign-request",0).status == C.TIDAK_PASTI
    assert (kotak / C.NAMA_RESULT).read_bytes() == original


# --------------------------------------------------------------------------
# Pembersihan slot memang manual
# --------------------------------------------------------------------------


def test_bersihkan_slot_eksplisit(kotak: Path):
    (kotak / C.NAMA_REQUEST).write_text("{}", encoding="utf-8")
    (kotak / C.NAMA_RESULT).write_text("{}", encoding="utf-8")
    assert sorted(C.bersihkan_slot(kotak)) == sorted([C.NAMA_REQUEST, C.NAMA_RESULT])
    assert C.bersihkan_slot(kotak) == []
    assert not (kotak / C.NAMA_REQUEST).exists()
    assert not (kotak / C.NAMA_RESULT).exists()


def test_berkas_hasil_separuh_jadi_tidak_ditebak(kotak: Path):
    (kotak / C.NAMA_RESULT).write_text('{"request_id": "r1", "sta', encoding="utf-8")
    assert C._baca_json(kotak / C.NAMA_RESULT) is None
