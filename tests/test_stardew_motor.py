"""Fase D: motor + tangan Stardew.

Yang dijaga di sini bukan "apakah Arti bisa jalan", melainkan **apakah Arti
bisa berhenti**. Sebuah motor yang jalannya bagus tapi remnya bolong lebih
berbahaya daripada motor yang tidak ada, karena kerusakannya terjadi di
depan penonton dan di save Bohan yang sungguhan.

Empat cara tangan bisa lepas kendali, dan masing-masing punya tes:

1. tombol terkirim saat window lain yang fokus -> Arti mengetik ke Discord;
2. rem darurat ditekan tapi pulih sendiri -> Bohan menekan rem, sepuluh
   detik kemudian tangannya hidup lagi tanpa dia minta;
3. utas motor menggantung sambil menahan tombol -> Arti lari ke tembok
   sampai ada yang sadar;
4. motor mengaku berhasil padahal tidak -> melanggar aturan #3, dan Arti
   mengumumkan ke penonton sesuatu yang tidak terjadi.

Tidak ada satu pun tes di berkas ini yang menyentuh vgamepad, window
sungguhan, atau Stardew. Kalau ada yang perlu, itu bukan unit test.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_gamepad as G  # noqa: E402
from arti_gamepad import (  # noqa: E402
    ATAS,
    BAWAH,
    KANAN,
    KIRI,
    GerbangTangan,
    MotorStardew,
    PadPalsu,
    SetelanMotor,
    WindowStardew,
    benturan_vts,
    buat_motor,
)
from arti_stardew import StardewStateStore  # noqa: E402

P1 = 1100000000000000002
P2 = -100000000000000001


def _payload(**timpa):
    p = {
        "location": "Farm",
        "tile": {"x": 10, "y": 10},
        "pixel": {"x": 640.0, "y": 640.0},
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


def _hello(player_id=P2, *, screen_id=1, name="artihand", main=False):
    return {
        "protocol": 1, "ev": "hello", "seq": 1,
        "player_id": player_id, "screen_id": screen_id,
        "player_name": name, "is_main_player": main, "is_split_screen": True,
    }


class Dunia:
    """Stardew palsu: satu farmer, peta halangan, jam palsu.

    Waktu hanya maju lewat `tidur()` yang disuntik ke motor, jadi tesnya
    deterministik dan tidak pernah menunggu jam dinding.
    """

    def __init__(
        self,
        *,
        tile=(10, 10),
        facing=2,
        energy=270.0,
        halangan=(),
        menu=None,
        bebas=True,
        bisa_gerak=True,
        main=False,
        fokus=True,
        nama="artihand",
        setelan=None,
        inventaris=None,      # None = DLL lama, belum mengirim inventaris
        # Kursor UI controller. `menu_grid=None` = mod TIDAK melaporkan kursor
        # (DLL lama) — bukan "tidak ada kursor". Bedanya menentukan apakah
        # navigasi menu boleh dicoba sama sekali.
        menu_grid=None,
        menu_keys=None,
        menu_roles=None,
        menu_texts=None,
        menu_neighbor_hints=True,
        menu_kursor=(0, 0),
        menu_close=None,
        menu_kursor_beku=False,
        # True = B menutup menu (GameMenu sungguhan). False = B diabaikan,
        # supaya tes jalur stik-ke-silang tidak kejebak pintasan B.
        b_menutup=False,
        a_menutup=False,
        start_membuka=False,
        start_delay_ticks=0,
        ui_revision=1,
        a_changes_ui=False,
    ):
        self.t = 1000.0
        self.tile = tile
        self.facing = facing
        self.energy = energy
        self.halangan = set(halangan)
        self.menu = menu
        self.bebas = bebas
        self.bisa_gerak = bisa_gerak
        self.fokus = fokus
        self.seq = 1
        self.beku = False          # berhenti mengirim state (simulasi mod mati)
        self.energi_per_tekan = 2.0
        self.inv = inventaris
        self.menu_grid = menu_grid
        self.menu_keys = menu_keys
        self.menu_roles = menu_roles
        self.menu_texts = menu_texts
        self.menu_neighbor_hints = menu_neighbor_hints
        self.menu_kursor = menu_kursor
        self.menu_close = menu_close
        self.menu_kursor_beku = menu_kursor_beku
        self.b_menutup = b_menutup
        self.a_menutup = a_menutup
        self.start_membuka = start_membuka
        self.start_delay_ticks = start_delay_ticks
        self._start_pending = None
        self.ui_revision = ui_revision
        self.a_changes_ui = a_changes_ui

        self.store = StardewStateStore(jam=self.jam)
        self.pad = PadPalsu(jam=self.jam)
        self.store.catat(_hello(name=nama, main=main))
        self.store.pilih_target(nama)
        self.kirim()

        self.window = WindowStardew(
            "stardew",
            cari_depan=lambda: (
                ("Stardew Valley", "Stardew Valley.exe") if self.fokus
                else ("Discord", "Discord.exe")
            ),
        )
        self.gerbang = GerbangTangan(self.store, window=self.window)
        self.motor = MotorStardew(
            self.store,
            self.pad,
            self.gerbang,
            setelan=setelan or SetelanMotor(batas_jalan_detik=5.0),
            jam=self.jam,
            tidur=self.tidur,
        )
        self.gerbang.aktifkan()   # lewat gerbang, bukan motor.aktifkan():
                                  # tes tidak boleh bergantung berkas VTS nyata

    # --- waktu ---
    def jam(self) -> float:
        return self.t

    def tidur(self, d: float) -> None:
        self.t += max(float(d), 0.001)
        self.gerak()
        self.kirim()

    # --- fisika ---
    def gerak(self) -> None:
        if self._start_pending is not None:
            self._start_pending -= 1
            if self._start_pending <= 0:
                self._start_pending = None
                self.menu = "GameMenu"
                self.bebas = False
                self.bisa_gerak = False
                self.ui_revision += 1
                return
        if (
            self.menu is None
            and G.TOMBOL_START in self.pad.ditahan
            and self.start_membuka
        ):
            if self.start_delay_ticks > 0:
                self._start_pending = self.start_delay_ticks
                return
            self.menu = "GameMenu"
            self.bebas = False
            self.bisa_gerak = False
            self.ui_revision += 1
            return
        # Menu terbuka: stik menggerakkan KURSOR, bukan kaki. Ini bukan
        # penyederhanaan tes — begitulah Stardew memperlakukan controller.
        if self.menu is not None:
            self._gerak_kursor()
            return
        stik = self.pad.stik_sekarang
        arah = next((a for a, s in G._STIK.items() if s == stik), None)
        if arah is None:
            return
        self.facing = G.FACING[arah]
        dx, dy = G._DELTA[arah]
        depan = (self.tile[0] + dx, self.tile[1] + dy)
        if depan not in self.halangan:
            self.tile = depan

    def _gerak_kursor(self) -> None:
        if G.TOMBOL_B in self.pad.ditahan and self.b_menutup:
            self.menu = None
            self.bebas = True
            self.bisa_gerak = True
            self.ui_revision += 1
            return
        if self.menu_grid is None:
            return
        r, c = self.menu_kursor
        if G.TOMBOL_A in self.pad.ditahan:
            if self.a_menutup:
                self.menu = None
                self.bebas = True
                self.bisa_gerak = True
                self.ui_revision += 1
                return
            if self.menu_close is not None and self.menu_grid[r][c] == self.menu_close:
                self.menu = None
                self.bebas = True
                self.bisa_gerak = True
                self.ui_revision += 1
            elif self.a_changes_ui:
                self.ui_revision += 1
            return
        if self.menu_kursor_beku:
            return
        stik = self.pad.stik_sekarang
        arah = next((a for a, sk in G._STIK.items() if sk == stik), None)
        if arah is None:
            return
        dx, dy = G._DELTA[arah]
        nr, nc = r + dy, c + dx
        if 0 <= nr < len(self.menu_grid) and 0 <= nc < len(self.menu_grid[nr]):
            if (nr, nc) != self.menu_kursor:
                self.menu_kursor = (nr, nc)
                self.ui_revision += 1

    def kirim(self) -> None:
        if self.beku:
            return
        self.seq += 1
        muatan = _payload(
            tile={"x": self.tile[0], "y": self.tile[1]},
            facing=self.facing,
            energy=self.energy,
            active_menu=self.menu,
            is_player_free=self.bebas,
            can_player_move=self.bisa_gerak,
            ui_revision=self.ui_revision,
        )
        # DLL lama tidak mengirim kunci ini sama sekali — bukan mengirim null.
        if self.inv is not None:
            muatan["inventory"] = self.inv
        if self.menu is not None and self.menu_grid is not None:
            r, c = self.menu_kursor
            komponen = self.menu_grid[r][c]
            def nilai(grid, rr, cc, bawaan):
                return grid[rr][cc] if grid is not None else bawaan

            components = []
            for rr, row in enumerate(self.menu_grid):
                for cc, native_id in enumerate(row):
                    def tetangga(dr, dc):
                        nr, nc = rr + dr, cc + dc
                        if (
                            not self.menu_neighbor_hints
                            or nr < 0 or nr >= len(self.menu_grid)
                            or nc < 0 or nc >= len(self.menu_grid[nr])
                        ):
                            return None
                        return self.menu_grid[nr][nc]

                    text = nilai(
                        self.menu_texts, rr, cc, f"komponen-{native_id}"
                    )
                    components.append({
                        "key": nilai(
                            self.menu_keys, rr, cc, f"ui.component.{native_id}"
                        ),
                        "native_id": native_id,
                        "source": "page",
                        "role": nilai(self.menu_roles, rr, cc, "component"),
                        "name": str(native_id),
                        "label": text,
                        "text": text,
                        "count": None,
                        "selected": (rr, cc) == self.menu_kursor,
                        "bounds": {"x": cc * 64, "y": rr * 64, "w": 64, "h": 64},
                        "neighbors": {
                            "up": tetangga(-1, 0),
                            "right": tetangga(0, 1),
                            "down": tetangga(1, 0),
                            "left": tetangga(0, -1),
                        },
                    })
            focus_key = nilai(
                self.menu_keys, r, c, f"ui.component.{komponen}"
            )
            muatan["menu"] = {
                "name": self.menu,
                "menu_type": self.menu,
                "page_type": (
                    "InventoryPage" if self.menu == "GameMenu" else self.menu
                ),
                "snap_id": komponen,
                "snap_label": nilai(
                    self.menu_texts, r, c, f"komponen-{komponen}"
                ),
                "snap_bounds": {"x": c * 64, "y": r * 64, "w": 64, "h": 64},
                "close_id": self.menu_close,
                "focus_key": focus_key,
                "visible_text": None,
                "speaker": None,
                "components": components,
            }
        self.store.catat({
            "protocol": 1, "ev": "state", "seq": self.seq,
            "player_id": P2, "screen_id": 1, "payload": muatan,
        })


def _inv(*, active_slot=0, hotbar=None, slots_used=1, items_total=10):
    return {
        "active_slot": active_slot,
        "hotbar": hotbar if hotbar is not None else [
            {"slot": 0, "name": "Hoe", "stack": 1, "is_tool": True},
        ],
        "slots_used": slots_used,
        "items_total": items_total,
    }


# ===========================================================================
# 1. Gerbang keselamatan
# ===========================================================================

def test_tanpa_fokus_window_nol_tombol_terkirim():
    """Window lain di depan = Arti sedang mengetik ke aplikasi orang."""
    d = Dunia(fokus=False)
    hasil = d.motor.jalan_ke(15, 10)
    assert not hasil.berhasil
    assert "fokus" in hasil.alasan
    assert d.pad.arah_terkirim() == []
    assert d.pad.tombol_ditekan() == []


def test_fokus_hilang_di_tengah_jalan_langsung_berhenti():
    d = Dunia()
    asli = d.motor._periksa
    langkah = {"n": 0}

    def periksa_lalu_alt_tab(pid):
        langkah["n"] += 1
        if langkah["n"] == 3:
            d.fokus = False
        return asli(pid)

    d.motor._periksa = periksa_lalu_alt_tab  # type: ignore[method-assign]
    hasil = d.motor.jalan_ke(30, 10)
    assert not hasil.berhasil
    assert "fokus" in hasil.alasan
    assert not d.pad.ada_input(), "tombol wajib dilepas saat gerbang menolak"


def test_rem_darurat_mengunci_dan_tidak_pulih_sendiri():
    d = Dunia()
    d.motor.rem_darurat("uji")
    assert d.gerbang.darurat
    assert not d.gerbang.aktif

    # waktu berlalu banyak — rem TETAP terkunci
    d.t += 3600.0
    d.kirim()
    hasil = d.motor.jalan_ke(11, 10)
    assert not hasil.berhasil
    assert "rem darurat" in hasil.alasan

    # bahkan aktifkan() eksplisit ditolak selama belum dibangunkan
    try:
        d.gerbang.aktifkan()
        assert False, "aktifkan() harus melempar selagi rem terkunci"
    except RuntimeError as e:
        assert "rem darurat" in str(e)

    d.gerbang.bangunkan()
    d.gerbang.aktifkan()
    assert d.gerbang.aktif


def test_rem_darurat_melepas_tombol_seketika():
    d = Dunia()
    d.pad.tekan(G.TOMBOL_A)
    d.pad.stik(1.0, 0.0)
    assert d.pad.ada_input()
    d.motor.rem_darurat()
    assert not d.pad.ada_input()


def test_gerbang_menolak_pemain_utama():
    """P1 adalah Bohan. Motor menggerakkan Bohan = mimpi buruk."""
    store = StardewStateStore()
    store.catat(_hello(P1, screen_id=0, name="host1", main=True))
    gerbang = GerbangTangan(store, window=WindowStardew(
        cari_depan=lambda: ("Stardew Valley", "Stardew Valley.exe")))
    gerbang.aktifkan()
    snap, alasan = gerbang.izin(P1)
    assert snap is None
    assert "pemain utama" in alasan or "bukan farmer target" in alasan


def test_gerbang_menolak_state_basi():
    d = Dunia()
    d.beku = True          # mod berhenti mengirim
    d.t += 5.0             # state terakhir jadi tua
    hasil = d.motor.jalan_ke(11, 10)
    assert not hasil.berhasil
    assert "basi" in hasil.alasan


def test_gerbang_menolak_saat_menu_terbuka():
    d = Dunia(menu="ShopMenu", bebas=False)
    hasil = d.motor.jalan_ke(11, 10)
    assert not hasil.berhasil
    assert "is_player_free" in hasil.alasan
    assert d.pad.arah_terkirim() == []


def test_motor_tanpa_target_menolak_bergerak():
    """Target ambigu = fail closed. Menebak berarti menggerakkan farmer salah."""
    store = StardewStateStore()
    gerbang = GerbangTangan(store, window=WindowStardew(
        cari_depan=lambda: ("Stardew Valley", "Stardew Valley.exe")))
    gerbang.aktifkan()
    pad = PadPalsu()
    motor = MotorStardew(store, pad, gerbang)
    for hasil in (motor.jalan_ke(1, 1), motor.hadap(ATAS),
                  motor.pakai_alat(), motor.ambil(1, 1), motor.tutup_menu()):
        assert not hasil.berhasil
        assert "target farmer belum dikunci" in hasil.alasan
    assert pad.jejak == []


# ===========================================================================
# 2. Deadman
# ===========================================================================

def test_deadman_melepas_tombol_saat_motor_menggantung():
    """Gerbang di dalam loop tidak menolong kalau utasnya sendiri macet."""
    store = StardewStateStore()
    pad = PadPalsu()
    gerbang = GerbangTangan(store, window=WindowStardew(
        cari_depan=lambda: ("Stardew Valley", "Stardew Valley.exe")))
    motor = MotorStardew(
        store, pad, gerbang,
        setelan=SetelanMotor(pulsa_detik=0.01, deadman_detik=0.05),
    )
    gerbang.aktifkan()
    motor._mulai_deadman()
    try:
        pad.stik(1.0, 0.0)          # tombol tertahan, tanpa detak menyusul
        batas = time.monotonic() + 2.0
        while pad.ada_input() and time.monotonic() < batas:
            time.sleep(0.01)
        assert not pad.ada_input(), "deadman gagal melepas tombol"
        assert motor.deadman_menyala >= 1
    finally:
        motor.matikan()


def test_deadman_tidak_mengganggu_motor_yang_sehat():
    """Detak diperbarui tiap iris yang lolos gerbang, jadi jalan normal
    tidak boleh memicu deadman."""
    d = Dunia(setelan=SetelanMotor(batas_jalan_detik=5.0, deadman_detik=1.0))
    d.motor._mulai_deadman()
    try:
        hasil = d.motor.jalan_ke(14, 10)
        assert hasil.berhasil
        assert d.motor.deadman_menyala == 0
    finally:
        d.motor.matikan()


# ===========================================================================
# 3. Benturan hotkey VTS (HANYA-BACA)
# ===========================================================================

def _tulis_model(tmp_path, pemicu):
    p = tmp_path / "MyModel.vtube.json"
    p.write_text(json.dumps({
        "Hotkeys": [
            {"Name": "IdleMotion1", "Triggers": pemicu},
            {"Name": "ArtiDefault1",
             "Triggers": {"Trigger1": "LeftControl", "Trigger2": "", "Trigger3": ""}},
        ]
    }), encoding="utf-8")
    return str(p)


def test_benturan_vts_menemukan_tombol_controller(tmp_path):
    """6 dari 8 hotkey Arti pernah terikat ke tombol yang dipakai Stardew.
    Kalau kembali, tiap ayunan cangkul memicu IdleMotion sepanjang siaran."""
    p = _tulis_model(tmp_path, {
        "Trigger1": "ControllerCross", "Trigger2": "", "Trigger3": ""
    })
    assert benturan_vts(p) == [("IdleMotion1", "ControllerCross")]


def test_benturan_vts_bersih_saat_trigger_kosong(tmp_path):
    p = _tulis_model(tmp_path, {"Trigger1": "", "Trigger2": "", "Trigger3": ""})
    assert benturan_vts(p) == []


def test_benturan_vts_tidak_pernah_menulis_berkas(tmp_path):
    """VTS memegang konfigurasi model di memori dan menimpanya saat keluar,
    jadi menulis berkas ini dari kode bisa merusak model Arti diam-diam."""
    p = _tulis_model(tmp_path, {
        "Trigger1": "ControllerSquare", "Trigger2": "", "Trigger3": ""
    })
    sebelum = Path(p).read_bytes()
    benturan_vts(p)
    benturan_vts(p)
    assert Path(p).read_bytes() == sebelum


def test_benturan_vts_berkas_hilang_tidak_meledak():
    assert benturan_vts(str(Path("tidak") / "ada.json")) == []


def test_aktifkan_menolak_saat_ada_benturan(monkeypatch, tmp_path):
    d = Dunia()
    d.gerbang.matikan()
    p = _tulis_model(tmp_path, {
        "Trigger1": "ControllerCross", "Trigger2": "", "Trigger3": ""
    })
    monkeypatch.setattr(G, "_MODEL_VTS_BAWAAN", p)
    try:
        d.motor.aktifkan()
        assert False, "motor tidak boleh menyala saat hotkey VTS bentrok"
    except RuntimeError as e:
        assert "IdleMotion1" in str(e)
    assert not d.gerbang.aktif
    # Ada jalan keluar sadar, tapi harus diminta eksplisit.
    d.motor.aktifkan(abaikan_benturan_vts=True)
    assert d.gerbang.aktif
    d.motor.matikan()


# ===========================================================================
# 4. Motor — dan kejujurannya (aturan #3)
# ===========================================================================

def test_jalan_ke_sampai_dan_terverifikasi():
    d = Dunia(tile=(10, 10))
    hasil = d.motor.jalan_ke(14, 13)
    assert hasil.berhasil
    assert hasil.terverifikasi
    assert hasil.tile_akhir == (14, 13)
    assert d.tile == (14, 13)
    assert not d.pad.ada_input(), "stik wajib netral sesudah sampai"


def test_jalan_ke_sudah_di_tujuan_langsung_selesai():
    d = Dunia(tile=(10, 10))
    hasil = d.motor.jalan_ke(10, 10)
    assert hasil.berhasil
    assert d.pad.arah_terkirim() == []


def test_jalan_ke_terhalang_gagal_jujur_bukan_diam():
    """Tidak ada peta tabrakan di state, jadi motor baru tahu ada pagar
    ketika dia berhenti bergerak. Yang penting: dia BILANG."""
    tembok = {(x, 10) for x in range(11, 20)} | {(10, y) for y in range(5, 16)}
    d = Dunia(tile=(10, 10), halangan=tembok,
              setelan=SetelanMotor(batas_jalan_detik=5.0, macet_detik=0.2))
    hasil = d.motor.jalan_ke(18, 10)
    assert not hasil.berhasil
    assert hasil.terverifikasi
    assert "terhalang" in hasil.alasan
    assert hasil.tile_akhir != (18, 10)
    assert not d.pad.ada_input()


def test_jalan_ke_berhenti_di_batas_waktu():
    """Pagar keras: satu perintah jalan tidak boleh jadi maraton."""
    d = Dunia(tile=(10, 10),
              setelan=SetelanMotor(batas_jalan_detik=0.3, macet_detik=99.0))
    hasil = d.motor.jalan_ke(9999, 10)
    assert not hasil.berhasil
    assert "batas" in hasil.alasan
    assert not d.pad.ada_input()


def test_hadap_terverifikasi_dari_state():
    d = Dunia(tile=(10, 10), facing=G.FACING[BAWAH])
    hasil = d.motor.hadap(KIRI)
    assert hasil.berhasil
    assert hasil.terverifikasi
    assert d.facing == G.FACING[KIRI]


def test_hadap_arah_ngawur_ditolak():
    d = Dunia()
    hasil = d.motor.hadap("serong")
    assert not hasil.berhasil
    assert d.pad.jejak == []


def test_pakai_alat_tidak_pernah_mengaku_terverifikasi():
    """Delta energi itu PETUNJUK, bukan bukti tanahnya tercangkul.
    Arti tidak boleh menyuarakan hasil ini sebagai kejadian (aturan #3)."""
    d = Dunia()
    hasil = d.motor.pakai_alat()
    assert hasil.berhasil          # tombolnya memang terkirim
    assert not hasil.terverifikasi
    assert G.TOMBOL_A in d.pad.tombol_ditekan()
    assert not d.pad.ada_input()


def test_ambil_tak_terverifikasi_kalau_mod_belum_kirim_inventaris():
    """DLL lama: tanpa isi tas, motor TIDAK BISA tahu barangnya masuk.
    Harus tetap tak terverifikasi, dan alasannya harus mengatakannya."""
    d = Dunia(tile=(10, 10))
    hasil = d.motor.ambil(12, 10)
    assert not hasil.terverifikasi
    assert "inventaris" in hasil.alasan


def test_ambil_terverifikasi_saat_isi_tas_bertambah():
    """Dengan inventaris, ambil() akhirnya bisa dibuktikan dari dunia
    (aturan #3) — bukan dari 'tombolnya sudah kukirim'."""
    d = Dunia(tile=(10, 10), inventaris=_inv(items_total=10))

    # Hook di `tekan`, bukan `lepas`: pakai_alat melepas lewat reset() pad,
    # jadi hook di `lepas` tidak akan pernah kepanggil.
    asli = d.pad.tekan

    def tekan_lalu_barang_masuk(tombol):
        asli(tombol)
        if tombol == G.TOMBOL_A:
            d.inv = _inv(items_total=11)
            d.kirim()

    d.pad.tekan = tekan_lalu_barang_masuk  # type: ignore[method-assign]
    hasil = d.motor.ambil(12, 10)
    assert hasil.berhasil
    assert hasil.terverifikasi


def test_ambil_gagal_jujur_saat_isi_tas_tidak_bertambah():
    """Tombol terkirim tapi tidak ada yang masuk = GAGAL, bukan 'berhasil
    tapi tak terverifikasi'. Sekarang datanya ada, jadi tidak ada alasan
    mengaku."""
    d = Dunia(tile=(10, 10), inventaris=_inv(items_total=10))
    hasil = d.motor.ambil(12, 10)
    assert not hasil.berhasil
    assert hasil.terverifikasi
    assert "tidak bertambah" in hasil.alasan


def test_pakai_alat_menolak_saat_memegang_bukan_alat():
    """Tombol pakai-alat di Stardew kontekstual: sambil memegang barang
    bukan-alat, tombol yang sama bisa memberi hadiah ke NPC atau memasukkan
    barang ke shipping bin — langsung terjual, tidak bisa dibatalkan. Motor
    tidak melihat NPC maupun bin, jadi bawaannya menolak."""
    d = Dunia(inventaris=_inv(hotbar=[
        {"slot": 0, "name": "Diamond", "stack": 1, "is_tool": False},
    ]))
    hasil = d.motor.pakai_alat()
    assert not hasil.berhasil
    assert "bukan-alat" in hasil.alasan
    assert d.pad.tombol_ditekan() == [], "nol tombol saat ditolak"


def test_pakai_alat_jalan_saat_memegang_alat():
    d = Dunia(inventaris=_inv(hotbar=[
        {"slot": 0, "name": "Hoe", "stack": 1, "is_tool": True},
    ]))
    hasil = d.motor.pakai_alat()
    assert hasil.berhasil
    assert G.TOMBOL_A in d.pad.tombol_ditekan()


def test_pakai_alat_boleh_bukan_alat_kalau_diminta_eksplisit():
    d = Dunia(inventaris=_inv(hotbar=[
        {"slot": 0, "name": "Diamond", "stack": 1, "is_tool": False},
    ]))
    hasil = d.motor.pakai_alat(izinkan_bukan_alat=True)
    assert hasil.berhasil


def test_pagar_bukan_alat_tidak_melumpuhkan_dll_lama():
    """Tanpa inventaris kita tidak tahu apa yang dipegang. Menolak semuanya
    bukan keamanan — cuma bikin motor tidak berguna."""
    d = Dunia()          # DLL lama
    hasil = d.motor.pakai_alat()
    assert hasil.berhasil


def test_slot_aktif_kosong_tidak_dianggap_bukan_alat():
    """Tangan kosong: tombol itu 'periksa/interaksi', bukan aksi merusak."""
    d = Dunia(inventaris=_inv(active_slot=5, hotbar=[
        {"slot": 0, "name": "Hoe", "stack": 1, "is_tool": True},
    ]))
    hasil = d.motor.pakai_alat()
    assert hasil.berhasil


def test_ambil_berdiri_di_tetangga_bukan_di_atas_target():
    d = Dunia(tile=(10, 10))
    d.motor.ambil(14, 10)
    assert d.tile != (14, 10), "motor tidak boleh menginjak tile targetnya"
    assert abs(d.tile[0] - 14) + abs(d.tile[1] - 10) == 1


def test_ambil_gagal_jujur_saat_tidak_bisa_mendekat():
    d = Dunia(tile=(10, 10),
              halangan={(x, y) for x in range(11, 20) for y in range(5, 16)},
              setelan=SetelanMotor(batas_jalan_detik=3.0, macet_detik=0.2))
    hasil = d.motor.ambil(15, 10)
    assert not hasil.berhasil
    assert "tidak sampai" in hasil.alasan


def test_tutup_menu_tekan_b_kalau_silang_di_luar_grid_halaman():
    """GameMenu: silang di BINGKAI, kursor di HALAMAN.

    Terbukti live 29 Agu 20:18: snap_id 0 lalu 12340 (Inventory),
    close_id 9175502, 24 langkah stik gagal. Keduanya tidak pernah
    ketemu. B hanya boleh dicoba sesudah kursor terlihat — itu bukti
    pad tertaut, bukan tap buta yang sudah dibayar sesi yang sama.
    """
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        menu_grid=[[1, 2], [3, 4]],
        menu_kursor=(0, 0),
        menu_close=9175502,
        b_menutup=True,
    )
    hasil = d.motor.tutup_menu()
    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert d.menu is None
    assert G.TOMBOL_B in d.pad.tombol_ditekan()
    assert G.TOMBOL_A not in d.pad.tombol_ditekan()
    assert not d.pad.ada_input()


def test_tutup_menu_terverifikasi():
    """Kursor dinavigasi dengan STIK ke tombol silang, lalu ditekan.

    Versi lama tes ini menyuntik "B ditekan -> menu tertutup", sehingga
    hijau padahal cara itu tidak pernah bekerja di game sungguhan. Sekarang
    dunia palsunya meniru lock-on: stik memindahkan kursor antar komponen,
    dan HANYA komponen tombol tutup yang bereaksi terhadap A.
    """
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        menu_grid=[[1, 2, 3], [4, 5, 6]],
        menu_kursor=(1, 0),   # pojok kiri bawah
        menu_close=3,         # silang di kanan atas
    )
    hasil = d.motor.tutup_menu()
    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert d.menu is None
    assert d.menu_kursor == (0, 2), "kursor tidak berhenti di tombol tutup"
    assert not d.pad.ada_input()


def test_tutup_menu_menolak_menavigasi_buta_kalau_kursor_tak_dilaporkan():
    """DLL lama tidak mengirim blok `menu`.

    Ini kasus yang membakar sesi 29 Agu 2026: tanpa laporan kursor, motor
    menekan tombol berdasarkan tebakan dan menu tidak pernah tertutup. Lebih
    jujur berhenti daripada menekan sesuatu yang tidak bisa diverifikasi.
    """
    d = Dunia(menu="GameMenu", bebas=False)      # menu_grid=None
    hasil = d.motor.tutup_menu()
    assert not hasil.berhasil
    assert "buta" in hasil.alasan.lower()
    assert d.pad.tombol_ditekan() == [], "menekan tombol tanpa tahu kursor di mana"


def test_tutup_menu_menyerah_jujur_kalau_menu_tanpa_tombol_tutup():
    d = Dunia(
        menu="ShopMenu",
        bebas=False,
        menu_grid=[[1, 2]],
        menu_kursor=(0, 0),
        menu_close=None,
    )
    hasil = d.motor.tutup_menu()
    assert not hasil.berhasil
    assert "ShopMenu" in hasil.alasan
    assert not d.pad.ada_input()


def test_tutup_menu_menyerah_kalau_kursor_mandek():
    """Kursor yang tidak pernah bergerak = berhenti, bukan menekan membabi buta."""
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        menu_grid=[[1, 2, 3], [4, 5, 6]],
        menu_kursor=(1, 0),
        menu_close=3,
        menu_kursor_beku=True,
    )
    hasil = d.motor.tutup_menu()
    assert not hasil.berhasil
    assert "mandek" in hasil.alasan.lower()
    assert d.menu == "GameMenu"
    assert not d.pad.ada_input()


def test_tutup_menu_tetap_tunduk_pada_rem_darurat():
    """tutup_menu sengaja melewati gerbang STATE (karena menu terbuka membuat
    is_player_free False), tapi tidak boleh melewati rem darurat."""
    d = Dunia(menu="ShopMenu", bebas=False)
    d.motor.rem_darurat("uji")
    hasil = d.motor.tutup_menu()
    assert not hasil.berhasil
    assert d.pad.tombol_ditekan() == []


def test_konfirmasi_menu_menekan_a_kalau_kursor_terlihat_dan_memverifikasi():
    """Dialog controller dipilih dengan A, tetapi hanya sesudah snap terbaca."""
    d = Dunia(
        menu="DialogueBox",
        bebas=False,
        menu_grid=[[101, 102]],
        menu_kursor=(0, 1),
        menu_close=None,
        a_menutup=True,
    )

    hasil = d.motor.konfirmasi_menu()

    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert d.menu is None
    assert d.pad.tombol_ditekan() == [G.TOMBOL_A]
    assert not d.pad.ada_input()


def test_konfirmasi_menu_expected_revision_basi_nol_input():
    d = Dunia(
        menu="ReadyCheckDialog",
        bebas=False,
        menu_grid=[[101]],
        ui_revision=7,
    )

    hasil = d.motor.konfirmasi_menu(expected_ui_revision=6)

    assert not hasil.berhasil
    assert hasil.kode == "UI_CHANGED"
    assert d.pad.tombol_ditekan() == []


def test_konfirmasi_menu_sukses_di_menu_sama_bila_ui_revision_berubah():
    d = Dunia(
        menu="ReadyCheckDialog",
        bebas=False,
        menu_grid=[[101]],
        ui_revision=7,
        a_changes_ui=True,
    )

    hasil = d.motor.konfirmasi_menu(expected_ui_revision=7)

    assert hasil.berhasil
    assert hasil.terverifikasi
    assert d.menu == "ReadyCheckDialog"
    assert d.ui_revision == 8


def test_konfirmasi_menu_tanpa_perubahan_observable_gagal_terverifikasi():
    d = Dunia(
        menu="ReadyCheckDialog",
        bebas=False,
        menu_grid=[[101]],
        ui_revision=7,
    )

    hasil = d.motor.konfirmasi_menu(expected_ui_revision=7)

    assert not hasil.berhasil
    assert hasil.terverifikasi
    assert hasil.kode == "NO_OBSERVABLE_CHANGE"


def test_konfirmasi_menu_menolak_a_buta_dan_window_yang_salah():
    buta = Dunia(menu="DialogueBox", bebas=False)
    hasil_buta = buta.motor.konfirmasi_menu()
    assert not hasil_buta.berhasil
    assert buta.pad.tombol_ditekan() == []

    salah_window = Dunia(
        menu="DialogueBox",
        bebas=False,
        fokus=False,
        menu_grid=[[101]],
        a_menutup=True,
    )
    hasil_fokus = salah_window.motor.konfirmasi_menu()
    assert not hasil_fokus.berhasil
    assert "fokus" in hasil_fokus.alasan
    assert salah_window.pad.tombol_ditekan() == []


def test_tutup_menu_tetap_menolak_window_yang_salah():
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        fokus=False,
        menu_grid=[[1]],
        menu_close=1,
        b_menutup=True,
    )
    hasil = d.motor.tutup_menu()
    assert not hasil.berhasil
    assert "fokus" in hasil.alasan
    assert d.pad.tombol_ditekan() == []


def test_status_mencetak_menu_dan_kursor_tanpa_input():
    d = Dunia(
        tile=(10, 9),
        menu="ReadyCheckDialog",
        bebas=False,
        bisa_gerak=False,
        menu_grid=[[101, 102]],
        menu_kursor=(0, 1),
    )

    hasil = d.motor.status()

    assert hasil.berhasil
    assert "ReadyCheckDialog" in hasil.alasan
    assert "snap=102" in hasil.alasan
    assert "komponen-102" in hasil.alasan
    assert "tile=(10, 9)" in hasil.alasan
    assert "free=False" in hasil.alasan
    assert d.pad.jejak == []


def test_navigasi_menu_menggeser_satu_langkah_dan_membaca_snap_baru():
    d = Dunia(
        menu="ReadyCheckDialog",
        bebas=False,
        menu_grid=[[101, 102]],
        menu_kursor=(0, 0),
    )

    hasil = d.motor.navigasi_menu(G.KANAN)

    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert d.menu_kursor == (0, 1)
    assert "101 -> 102" in hasil.alasan
    assert "komponen-102" in hasil.alasan
    assert not d.pad.ada_input()


def test_navigasi_menu_menolak_tanpa_kursor_atau_fokus():
    buta = Dunia(menu="ReadyCheckDialog", bebas=False)
    assert not buta.motor.navigasi_menu(G.KANAN).berhasil
    assert buta.pad.arah_terkirim() == []

    salah_window = Dunia(
        menu="ReadyCheckDialog",
        bebas=False,
        fokus=False,
        menu_grid=[[101, 102]],
    )
    hasil = salah_window.motor.navigasi_menu(G.KANAN)
    assert not hasil.berhasil
    assert "fokus" in hasil.alasan
    assert salah_window.pad.arah_terkirim() == []


def test_semua_input_menu_menolak_state_basi_tanpa_menekan_tombol():
    """Menu boleh membuat free=false, tetapi identitas dan freshness tetap wajib."""
    for aksi in (
        lambda motor: motor.konfirmasi_menu(),
        lambda motor: motor.navigasi_menu(G.KANAN),
        lambda motor: motor.tutup_menu(),
    ):
        d = Dunia(
            menu="ReadyCheckDialog",
            bebas=False,
            bisa_gerak=False,
            menu_grid=[[101, 102]],
            menu_kursor=(0, 0),
            menu_close=102,
        )
        d.beku = True
        d.t += 5.0

        hasil = aksi(d.motor)

        assert not hasil.berhasil
        assert "basi" in hasil.alasan
        assert d.pad.tombol_ditekan() == []
        assert d.pad.arah_terkirim() == []
        assert not d.pad.ada_input()


def test_ui_focus_mencapai_semantic_target_tanpa_caller_menghitung_arah():
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        menu_grid=[[0, 1, 2]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1", "inventory.slot.2"]],
        menu_roles=[["inventory_slot", "inventory_slot", "inventory_slot"]],
        menu_kursor=(0, 0),
        menu_close=9175502,
        ui_revision=10,
    )

    hasil = d.motor.ui_focus("inventory.slot.2", expected_ui_revision=10)

    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert d.menu_kursor == (0, 2)
    assert d.pad.tombol_ditekan() == []
    assert d.pad.arah_terkirim(), "navigator tidak pernah memakai backend arah"


def test_ui_focus_memakai_neighbor_hint_sebagai_rute_terpendek():
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        menu_grid=[[0, 1], [2, 3]],
        menu_keys=[
            ["inventory.slot.0", "inventory.slot.1"],
            ["inventory.slot.2", "inventory.slot.3"],
        ],
        menu_roles=[
            ["inventory_slot", "inventory_slot"],
            ["inventory_slot", "inventory_slot"],
        ],
        menu_kursor=(1, 0),
        ui_revision=10,
    )

    hasil = d.motor.ui_focus("inventory.slot.3", expected_ui_revision=10)

    assert hasil.berhasil, hasil.alasan
    assert hasil.langkah == 1
    assert d.pad.arah_terkirim() == [G._STIK[G.KANAN]]


def test_ui_focus_sukses_bila_langkah_terakhir_tepat_di_batas():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
        ui_revision=10,
    )

    hasil = d.motor.ui_focus(
        "inventory.slot.1", expected_ui_revision=10, max_transitions=1
    )

    assert hasil.berhasil, hasil.alasan
    assert hasil.langkah == 1


def test_ui_activate_target_ambigu_nol_confirm():
    d = Dunia(
        menu="DialogueBox",
        bebas=False,
        menu_grid=[[0, 1]],
        menu_keys=[["dialogue.choice.yes", "dialogue.choice.yes"]],
        menu_roles=[["choice", "choice"]],
        ui_revision=10,
    )

    hasil = d.motor.ui_activate("dialogue.choice.yes", expected_ui_revision=10)

    assert not hasil.berhasil
    assert hasil.kode == "TARGET_AMBIGUOUS"
    assert d.pad.tombol_ditekan() == []
    assert d.pad.arah_terkirim() == []


def test_ui_focus_menolak_focus_key_dan_native_snap_yang_tidak_konsisten():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
    )
    asli = d.motor._store.latest

    def latest_desync(pid):
        state = asli(pid)
        assert state is not None
        return replace(state, menu_focus_key="inventory.slot.1")

    d.motor._store.latest = latest_desync
    hasil = d.motor.ui_focus("inventory.slot.1")

    assert not hasil.berhasil
    assert hasil.kode == "UI_CHANGED"
    assert d.pad.jejak == []


def test_ui_focus_stale_revision_nol_input():
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
        ui_revision=10,
    )

    hasil = d.motor.ui_focus("inventory.slot.1", expected_ui_revision=9)

    assert not hasil.berhasil
    assert hasil.kode == "UI_CHANGED"
    assert d.pad.arah_terkirim() == []


def test_ui_focus_bounded_exploration_hanya_memakai_navigation():
    d = Dunia(
        menu="GameMenu",
        bebas=False,
        menu_grid=[[0, 1, 2]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1", "inventory.slot.2"]],
        menu_roles=[["inventory_slot", "inventory_slot", "inventory_slot"]],
        menu_neighbor_hints=False,
        ui_revision=10,
    )

    hasil = d.motor.ui_focus("inventory.slot.2", expected_ui_revision=10)

    assert hasil.berhasil, hasil.alasan
    assert d.menu_kursor == (0, 2)
    assert d.pad.tombol_ditekan() == [], "eksplorasi menekan tombol aksi"
    assert len(d.pad.arah_terkirim()) <= 32


def test_dialogue_choose_fokus_lalu_a_dan_verifikasi_revision():
    d = Dunia(
        menu="DialogueBox",
        bebas=False,
        menu_grid=[[0, 1]],
        menu_keys=[["dialogue.choice.0", "dialogue.choice.1"]],
        menu_roles=[["choice", "choice"]],
        menu_texts=[["Yes", "No"]],
        a_changes_ui=True,
        ui_revision=10,
    )

    hasil = d.motor.dialogue_choose(1, expected_ui_revision=10)

    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert d.menu_kursor == (0, 1)
    assert d.pad.tombol_ditekan() == [G.TOMBOL_A]


def test_dialogue_choose_menolak_bool_sebagai_index_tanpa_input():
    d = Dunia(
        menu="DialogueBox", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["dialogue.choice.0", "dialogue.choice.1"]],
        menu_roles=[["choice", "choice"]],
    )

    hasil = d.motor.dialogue_choose(True)

    assert not hasil.berhasil
    assert hasil.kode == "TARGET_NOT_FOUND"
    assert d.pad.jejak == []


def test_semantic_action_tetap_tunduk_rem_fokus_dan_freshness():
    scenarios = []
    rem = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
    )
    rem.motor.rem_darurat("uji")
    scenarios.append((rem, "EMERGENCY_STOP"))
    scenarios.append((Dunia(
        menu="GameMenu", bebas=False, fokus=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
    ), "FOCUS_REQUIRED"))
    stale = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
    )
    stale.beku = True
    stale.t += 5.0
    scenarios.append((stale, "STATE_STALE"))

    for dunia, kode in scenarios:
        hasil = dunia.motor.ui_focus("inventory.slot.1")
        assert not hasil.berhasil
        assert hasil.kode == kode
        assert dunia.pad.tombol_ditekan() == []
        assert dunia.pad.arah_terkirim() == []


def test_kode_target_ui_tidak_dipakai_untuk_kegagalan_identitas_farmer():
    assert G._kode_gerbang("target farmer belum dikunci") == "TARGET_NOT_FOUND"
    assert G._kode_gerbang("pemain utama tidak boleh dikontrol") == "BLOCKED"
    assert G._kode_gerbang("identitas belum pernah diterima (hello)") == "BLOCKED"


def test_ui_snapshot_read_only_tidak_membocorkan_native_id():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[12340, 3]],
        menu_keys=[["game.tab.inventory", "inventory.slot.3"]],
        menu_roles=[["tab", "inventory_slot"]],
        menu_texts=[["Inventory", "Parsnip Seeds"]],
        ui_revision=12,
    )

    snapshot = d.motor.ui_snapshot()

    assert snapshot is not None
    assert snapshot.revision == 12
    assert [e.key for e in snapshot.elements] == [
        "game.tab.inventory", "inventory.slot.3",
    ]
    assert all(not hasattr(e, "native_id") for e in snapshot.elements)
    assert d.pad.jejak == []


def test_cli_ui_mencetak_snapshot_semantic_tanpa_native_id_dan_nol_input():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[12340, 3]],
        menu_keys=[["game.tab.inventory", "inventory.slot.3"]],
        menu_roles=[["tab", "inventory_slot"]],
        menu_texts=[["Inventory", "Parsnip Seeds"]],
        ui_revision=12,
    )

    hasil = G._urai_aksi(d.motor, "ui")
    snapshot = json.loads(hasil.alasan)

    assert hasil.berhasil
    assert snapshot["revision"] == 12
    assert snapshot["focus"] == "game.tab.inventory"
    assert [element["key"] for element in snapshot["elements"]] == [
        "game.tab.inventory", "inventory.slot.3",
    ]
    assert "native_id" not in hasil.alasan
    assert d.pad.jejak == []


def test_cli_ui_focus_wajib_revision_dan_memakai_runner_pad_yang_sama():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
        ui_revision=10,
    )
    baris = iter(("ui", "ui_focus:inventory.slot.1@10", "keluar"))
    keluaran = []

    status = G._runner_interaktif(
        d.motor, baca=lambda _prompt: next(baris), tulis=keluaran.append
    )

    assert status == 0
    assert d.menu_kursor == (0, 1)
    assert d.pad.arah_terkirim()
    assert d.pad.tombol_ditekan() == []
    assert not d.pad.tertutup
    assert any('"revision":12' not in line and '"revision":10' in line for line in keluaran)


def test_cli_semantic_mutation_tanpa_revision_ditolak_dan_runner_tetap_hidup():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
        ui_revision=10,
    )
    baris = iter(("ui_focus:inventory.slot.1", "ui", "keluar"))
    keluaran = []

    status = G._runner_interaktif(
        d.motor, baca=lambda _prompt: next(baris), tulis=keluaran.append
    )

    assert status == 0
    assert d.pad.jejak == []
    assert any("Perintah ditolak" in line for line in keluaran)
    assert any('"revision":10' in line for line in keluaran)


def test_cli_ui_back_revision_basi_nol_input():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0]],
        menu_keys=[["inventory.slot.0"]],
        menu_roles=[["inventory_slot"]],
        menu_close=9175502,
        ui_revision=4,
    )

    hasil = G._urai_aksi(d.motor, "ui_back:3")

    assert not hasil.berhasil
    assert hasil.aksi == "ui_back"
    assert hasil.kode == "UI_CHANGED"
    assert d.pad.tombol_ditekan() == []
    assert d.pad.arah_terkirim() == []


def test_ui_open_start_terverifikasi_dari_game_menu_baru():
    d = Dunia(start_membuka=True, ui_revision=4)

    hasil = d.motor.ui_open(expected_ui_revision=4)

    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert hasil.aksi == "ui_open"
    assert d.menu == "GameMenu"
    assert d.pad.tombol_ditekan() == [G.TOMBOL_START]
    assert not d.pad.ada_input()


def test_ui_open_menunggu_transisi_yang_datang_setelah_state_antara():
    d = Dunia(start_membuka=True, start_delay_ticks=1, ui_revision=4)

    hasil = d.motor.ui_open(expected_ui_revision=4)

    assert hasil.berhasil, hasil.alasan
    assert hasil.terverifikasi
    assert d.menu == "GameMenu"
    assert d.pad.tombol_ditekan() == [G.TOMBOL_START]
    assert not d.pad.ada_input()


def test_ui_open_revision_basi_dan_menu_lain_nol_start():
    basi = Dunia(start_membuka=True, ui_revision=4)
    hasil_basi = basi.motor.ui_open(expected_ui_revision=3)

    assert not hasil_basi.berhasil
    assert hasil_basi.kode == "UI_CHANGED"
    assert basi.pad.tombol_ditekan() == []

    dialog = Dunia(menu="DialogueBox", bebas=False, bisa_gerak=False)
    hasil_dialog = dialog.motor.ui_open(expected_ui_revision=1)

    assert not hasil_dialog.berhasil
    assert hasil_dialog.kode == "BLOCKED"
    assert dialog.pad.tombol_ditekan() == []


def test_ui_open_fokus_rem_dan_tanpa_perubahan_fail_closed():
    salah_fokus = Dunia(fokus=False, start_membuka=True)
    hasil_fokus = salah_fokus.motor.ui_open(expected_ui_revision=1)
    assert hasil_fokus.kode == "FOCUS_REQUIRED"
    assert salah_fokus.pad.tombol_ditekan() == []

    rem = Dunia(start_membuka=True)
    rem.motor.rem_darurat("uji")
    hasil_rem = rem.motor.ui_open(expected_ui_revision=1)
    assert hasil_rem.kode == "EMERGENCY_STOP"
    assert rem.pad.tombol_ditekan() == []

    basi = Dunia(start_membuka=True)
    basi.t += 1.0
    hasil_basi = basi.motor.ui_open(expected_ui_revision=1)
    assert hasil_basi.kode == "STATE_STALE"
    assert basi.pad.tombol_ditekan() == []

    diam = Dunia(start_membuka=False)
    hasil_diam = diam.motor.ui_open(expected_ui_revision=1)
    assert not hasil_diam.berhasil
    assert hasil_diam.terverifikasi
    assert hasil_diam.kode == "NO_OBSERVABLE_CHANGE"
    assert diam.pad.tombol_ditekan() == [G.TOMBOL_START]
    assert not diam.pad.ada_input()


def test_cli_ui_open_wajib_revision_dan_runner_tetap_hidup():
    d = Dunia(start_membuka=True, ui_revision=7)
    baris = iter(("ui_open", "ui", "ui_open:7", "status", "keluar"))
    keluaran = []

    status = G._runner_interaktif(
        d.motor, baca=lambda _prompt: next(baris), tulis=keluaran.append
    )

    assert status == 0
    assert d.menu == "GameMenu"
    assert d.pad.tombol_ditekan() == [G.TOMBOL_START]
    assert any("Perintah ditolak" in line for line in keluaran)
    assert any('"revision":7' in line for line in keluaran)
    assert any("menu='GameMenu'" in line for line in keluaran)


def test_ui_focus_target_tidak_ada_dan_tidak_terjangkau_fail_closed():
    hilang = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0]],
        menu_keys=[["inventory.slot.0"]],
        menu_roles=[["inventory_slot"]],
    )
    hasil_hilang = hilang.motor.ui_focus("inventory.slot.9")
    assert hasil_hilang.kode == "TARGET_NOT_FOUND"
    assert hilang.pad.arah_terkirim() == []

    buntu = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
        menu_neighbor_hints=False,
        menu_kursor_beku=True,
    )
    hasil_buntu = buntu.motor.ui_focus("inventory.slot.1")
    assert not hasil_buntu.berhasil
    assert hasil_buntu.kode == "TARGET_NOT_REACHABLE"
    assert buntu.pad.tombol_ditekan() == []
    assert len(buntu.pad.arah_terkirim()) <= 32


def test_ui_focus_edge_buntu_boleh_dicoba_ulang_command_berikutnya():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
        menu_neighbor_hints=False,
        menu_kursor_beku=True,
    )

    pertama = d.motor.ui_focus("inventory.slot.1")
    assert not pertama.berhasil
    assert pertama.kode == "TARGET_NOT_REACHABLE"

    d.menu_kursor_beku = False
    d.pad.jejak.clear()
    kedua = d.motor.ui_focus("inventory.slot.1")

    assert kedua.berhasil, kedua.alasan
    assert d.pad.arah_terkirim(), "edge timeout diracuni lintas command"


def test_ui_focus_state_hilang_setelah_gerak_tidak_diklaim_terverifikasi():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0, 1]],
        menu_keys=[["inventory.slot.0", "inventory.slot.1"]],
        menu_roles=[["inventory_slot", "inventory_slot"]],
    )
    asli = d.motor._store.latest

    def latest_lalu_hilang(pid):
        if d.pad.arah_terkirim():
            return None
        return asli(pid)

    d.motor._store.latest = latest_lalu_hilang
    hasil = d.motor.ui_focus("inventory.slot.1")

    assert not hasil.berhasil
    assert not hasil.terverifikasi
    assert hasil.kode == "ACTION_UNVERIFIED"


def test_ui_back_revision_basi_nol_b_dan_nol_a():
    d = Dunia(
        menu="GameMenu", bebas=False, menu_grid=[[0]],
        menu_keys=[["inventory.slot.0"]],
        menu_roles=[["inventory_slot"]],
        menu_close=9175502,
        ui_revision=4,
    )

    hasil = d.motor.ui_back(expected_ui_revision=3)

    assert not hasil.berhasil
    assert hasil.aksi == "ui_back"
    assert hasil.kode == "UI_CHANGED"
    assert d.pad.tombol_ditekan() == []


# ===========================================================================
# 5. Perakitan & pagar aturan #2
# ===========================================================================

def test_buat_motor_tidak_menyalakan_tangan_sebagai_efek_samping():
    store = StardewStateStore()
    motor = buat_motor(store, palsu=True)
    assert not motor._gerbang.aktif, "motor hasil rakitan wajib mati sampai diminta"


def test_pad_palsu_reset_benar_benar_mengosongkan():
    pad = PadPalsu()
    pad.tekan(G.TOMBOL_A)
    pad.stik(-1.0, 0.0)
    pad.reset()
    assert not pad.ada_input()
    assert pad.ditahan == frozenset()
    assert pad.stik_sekarang == (0.0, 0.0)


def test_motor_tidak_mengimpor_llm():
    """Aturan wajib #2. Loop game yang memanggil composer/Luna adalah pemakai
    penyedia kedua saat siaran hidup — kegagalan yang sudah dibayar dua
    kali dalam satu malam (11 Agu). Motor deterministik, titik.

    Meniru `test_kerja_latar_tidak_pakai_cursor`.

    Dibaca lewat AST, bukan pencarian teks. Versi teks gagal pada percobaan
    pertama karena docstring modul MENJELASKAN aturan ini dan menyebut nama
    yang dilarang — jebakan #10 di CLAUDE.md, dijatuhi tepat seperti tertulis.
    Impor adalah yang mengikat; komentar tidak memanggil apa pun.
    """
    import ast

    pohon = ast.parse((ROOT / "arti_gamepad.py").read_text(encoding="utf-8"))
    diimpor: set[str] = set()
    for simpul in ast.walk(pohon):
        if isinstance(simpul, ast.Import):
            diimpor.update(a.name.split(".")[0] for a in simpul.names)
        elif isinstance(simpul, ast.ImportFrom) and simpul.module:
            diimpor.add(simpul.module.split(".")[0])

    terlarang = {
        "cursor_sdk", "arti_codex_agent", "arti_agy_agent", "arti_composer",
        "openai", "groq", "anthropic", "openrouter", "requests", "httpx",
        "aiohttp", "urllib", "arti_bridge",
    }
    ada = sorted(diimpor & terlarang)
    assert not ada, f"motor Stardew tidak boleh mengimpor: {ada}"

    # Dan tidak ada impor tertunda di dalam fungsi yang lolos dari mata di atas
    # selain tiga yang memang disengaja.
    assert diimpor <= {
        "__future__", "ctypes", "json", "threading", "time", "dataclasses",
        "typing", "arti_stardew", "arti_stardew_ui", "vgamepad", "keyboard",
        "ast", "argparse",
    }, f"impor tak terduga di motor: {sorted(diimpor)}"


def test_semua_primitif_lewat_satu_corong():
    """Kalau ada jalur kedua yang menekan tombol tanpa memeriksa gerbang,
    seluruh berkas tes ini jadi teater. Jadi: dengan gerbang menolak, TIDAK
    ADA primitif yang boleh meninggalkan jejak tombol."""
    d = Dunia(fokus=False)
    d.motor.jalan_ke(20, 20)
    d.motor.hadap(ATAS)
    d.motor.pakai_alat()
    d.motor.ambil(12, 10)
    assert d.pad.tombol_ditekan() == []
    assert d.pad.arah_terkirim() == []


def test_matikan_melepas_tombol_dan_menghentikan_deadman():
    d = Dunia()
    d.motor._mulai_deadman()
    d.pad.tekan(G.TOMBOL_A)
    d.motor.matikan()
    assert not d.pad.ada_input()
    assert not d.gerbang.aktif
    hidup = [t for t in threading.enumerate() if t.name == "stardew-deadman"]
    assert hidup == [], "utas deadman wajib berhenti"


def test_runner_mempertahankan_pad_lintas_batch_dan_pulih_sesudah_gagal():
    """Satu aksi gagal tidak boleh membongkar pad atau mematikan runner.

    Ini beda penting dari CLI batch lama: operator harus bisa memperbaiki
    fokus/tujuan lalu mengirim batch berikutnya dengan controller yang SAMA.
    """
    d = Dunia(fokus=False, facing=2)
    baris = iter(("hadap:atas", "hadap:atas", "keluar"))
    saat_prompt = []
    keluaran = []

    def baca(_prompt):
        saat_prompt.append((d.pad.tertutup, d.pad.ada_input()))
        if len(saat_prompt) == 2:
            d.fokus = True
        return next(baris)

    status = G._runner_interaktif(d.motor, baca=baca, tulis=keluaran.append)

    assert status == 0
    assert d.facing == G.FACING[G.ATAS], "batch kedua tidak pernah dijalankan"
    assert len(saat_prompt) == 3, "runner mati sesudah batch pertama gagal"
    assert all(not tertutup for tertutup, _ in saat_prompt)
    assert all(not ada_input for _, ada_input in saat_prompt)
    assert not d.pad.tertutup, "hanya pemilik lifecycle yang boleh menutup pad"
    assert any("GAGAL" in teks for teks in keluaran)
    assert any("OK" in teks for teks in keluaran)


# ===========================================================================
# 6. Gerbang fokus mengenali window GAME SMAPI, bukan console SMAPI
# ===========================================================================

def _win(judul, exe):
    return WindowStardew(cari_depan=lambda: (judul, exe))


def test_console_smapi_bukan_game():
    """Dibayar sebelum sempat hidup. Console SMAPI berjudul
    `SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods` — string
    `running Stardew Valley` memang ada di StardewModdingAPI.dll. Pencocokan
    judul dengan "stardew" akan lolos untuk window yang SALAH."""
    w = _win("SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods",
             "StardewModdingAPI.exe")
    assert not w.di_depan()


def test_explorer_di_folder_game_bukan_game():
    """Window Explorer yang membuka folder game berjudul persis
    `Stardew Valley`. Judul saja tidak bisa membedakannya."""
    assert not _win("Stardew Valley", "explorer.exe").di_depan()


def test_game_sungguhan_lolos():
    assert _win("Stardew Valley", "Stardew Valley.exe").di_depan()
    assert _win("Stardew Valley", "STARDEW VALLEY.EXE").di_depan()


def test_game_yang_dihost_smapi_lolos_tapi_consolenya_tidak():
    """Diukur live 29 Agu: game dan console sama-sama dimiliki proses SMAPI.
    Urutan judul membedakan keduanya."""
    exe = "StardewModdingAPI.exe"
    assert _win(
        "Stardew Valley 1.6.15 - running SMAPI 4.5.2 with 3 mods", exe
    ).di_depan()
    assert not _win(
        "SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods", exe
    ).di_depan()


def test_window_stub_kering_memakai_satu_exe_yang_sah():
    """Dry-run harus melewati stub fokus tanpa menyentuh window sungguhan."""
    assert G._buat_window_kering().di_depan()


def test_join_lokal_memakai_pad_yang_sama_dengan_urutan_start_lalu_a():
    """Controller yang join wajib controller yang kemudian dipakai motor."""
    pad = PadPalsu()

    class WindowSelaluFokus:
        def di_depan(self):
            return True

    berhasil, alasan = G._join_lokal(
        pad,
        WindowSelaluFokus(),
        tidur=lambda _detik: None,
        stabil_detik=0.0,
    )

    assert berhasil, alasan
    assert pad.tombol_ditekan() == [G.TOMBOL_START, G.TOMBOL_A]
    assert not pad.ada_input()


def test_join_lokal_batal_sebelum_a_kalau_fokus_hilang():
    """Jangan kirim konfirmasi ke aplikasi lain setelah transisi START."""
    pad = PadPalsu()
    # Panggilan pertama dipakai masa tenang (stabil_detik=0.0 -> lolos sekali),
    # lalu gerbang START, lalu gerbang A yang gagal.
    fokus = iter((True, True, False))

    class WindowFokusHilang:
        def di_depan(self):
            return next(fokus)

    berhasil, alasan = G._join_lokal(
        pad,
        WindowFokusHilang(),
        tidur=lambda _detik: None,
        stabil_detik=0.0,
    )

    assert not berhasil
    assert "fokus" in alasan.lower()
    assert pad.tombol_ditekan() == [G.TOMBOL_START]
    assert not pad.ada_input()


def test_join_lokal_tidak_menekan_apa_pun_sebelum_pad_sempat_terdaftar():
    """Fokus sekejap = nol tombol.

    Akar kegagalan join pertama di game hidup (29 Agu 2026): START dikirim
    ~0 detik sesudah pad virtual dibuat, sebelum Stardew mendaftarkan
    controller barunya. Tombol terkirim, `LocalCoopJoinMenu` diam, P2 tidak
    pernah join. Tes ini mengunci masa tenangnya: kalau fokus tidak pernah
    bertahan `stabil_detik`, pad TIDAK BOLEH mengirim apa pun.
    """
    pad = PadPalsu()
    detak = [0.0]

    def jam():
        return detak[0]

    def tidur(detik):
        detak[0] += detik

    class WindowKedip:
        """Fokus datang-pergi lebih cepat daripada syarat stabil."""

        def __init__(self):
            self.n = 0

        def di_depan(self):
            self.n += 1
            return self.n % 2 == 1

    berhasil, alasan = G._join_lokal(
        pad,
        WindowKedip(),
        tidur=tidur,
        jam=jam,
        stabil_detik=1.0,
        batas_fokus=2.0,
    )

    assert not berhasil
    assert "stabil" in alasan.lower()
    assert pad.tombol_ditekan() == [], "tombol terkirim sebelum pad terdaftar"
    assert not pad.ada_input()


def test_join_lokal_menekan_sesudah_fokus_stabil_terpenuhi():
    """Kebalikannya: fokus yang bertahan cukup lama HARUS lanjut ke START."""
    pad = PadPalsu()
    detak = [0.0]

    class WindowSelaluFokus:
        def di_depan(self):
            return True

    berhasil, alasan = G._join_lokal(
        pad,
        WindowSelaluFokus(),
        tidur=lambda detik: detak.__setitem__(0, detak[0] + detik),
        jam=lambda: detak[0],
        stabil_detik=1.0,
        batas_fokus=20.0,
    )

    assert berhasil, alasan
    assert pad.tombol_ditekan() == [G.TOMBOL_START, G.TOMBOL_A]
    assert detak[0] >= 1.0, "tombol pertama dikirim sebelum masa tenang selesai"


def test_join_lokal_melewati_start_kalau_p2_sudah_join():
    """START pada P2 yang SUDAH join = membuka menu pause, bukan join.

    Dibayar di game hidup 29 Agu 2026. Menutup pad virtual tidak melepas
    farmhand yang sudah masuk, dan pad baru terikat lagi ke slot P2 yang
    sama. Percobaan join ulang membuat `artihand` berakhir di
    `menu='GameMenu' free=False`, sehingga SEMUA aksi sesudahnya ditolak
    gerbang state -- dan tekanan `A` mendarat di dalam menu inventaris.
    """
    pad = PadPalsu()

    class WindowSelaluFokus:
        def di_depan(self):
            return True

    berhasil, alasan = G._join_lokal(
        pad,
        WindowSelaluFokus(),
        tidur=lambda _detik: None,
        stabil_detik=0.0,
        sudah_join=lambda: True,
    )

    assert berhasil, alasan
    assert "dilewati" in alasan.lower()
    assert pad.tombol_ditekan() == [], "join ulang membuka menu pause P2"
    assert not pad.ada_input()


def test_join_lokal_tetap_join_kalau_p2_belum_ada():
    """Kebalikannya: kalau P2 memang belum ada, urutan join tetap dikirim."""
    pad = PadPalsu()
    detak = [0.0]

    class WindowSelaluFokus:
        def di_depan(self):
            return True

    berhasil, alasan = G._join_lokal(
        pad,
        WindowSelaluFokus(),
        tidur=lambda detik: detak.__setitem__(0, detak[0] + detik),
        jam=lambda: detak[0],
        stabil_detik=0.0,
        sudah_join=lambda: False,
        tunggu_cek_join=1.0,
    )

    assert berhasil, alasan
    assert pad.tombol_ditekan() == [G.TOMBOL_START, G.TOMBOL_A]


def test_window_lain_ditolak():
    for judul, exe in [
        ("Discord", "Discord.exe"),
        ("arti_gamepad.py - Cursor", "Cursor.exe"),
        ("", ""),
        ("Stardew Valley Wiki - Chrome", "chrome.exe"),
    ]:
        assert not _win(judul, exe).di_depan(), (judul, exe)


def test_nama_proses_tidak_terbaca_gagal_tertutup():
    """Tidak tahu proses mana yang aktif berarti tidak boleh mengirim input."""
    assert not _win("Stardew Valley", "").di_depan()
    assert not _win("  stardew valley  ", "").di_depan()
    assert not _win("SMAPI 4.5.2 - running Stardew Valley 1.6.15", "").di_depan()
    assert not _win("Stardew Valley Wiki - Chrome", "").di_depan()
    assert not _win("", "").di_depan()


def test_gerbang_fokus_dipakai_motor_bukan_cuma_ada():
    """Pagar yang tidak dipanggil bukan pagar."""
    d = Dunia()
    d.gerbang._window = _win("SMAPI - running Stardew Valley", "StardewModdingAPI.exe")
    hasil = d.motor.jalan_ke(20, 20)
    assert not hasil.berhasil
    assert "fokus" in hasil.alasan
    assert d.pad.arah_terkirim() == []


# --- join terpandu: layar join minta START, bukan A -----------------------

class _WindowFokus:
    def di_depan(self):
        return True


def _join_terpandu(pad, menu_urut, **kw):
    """Panggil join dengan pembaca menu yang memutar `menu_urut` sekali jalan."""
    sisa = list(menu_urut)
    t = [0.0]

    def baca():
        return sisa.pop(0) if sisa else None

    def tidur(detik):
        t[0] += max(float(detik), 0.01)

    return G._join_lokal(
        pad, _WindowFokus(), tidur=tidur, jam=lambda: t[0],
        stabil_detik=0.0, baca_menu=baca, tunggu_cek_join=0.0, **kw,
    )


def test_join_terpandu_menekan_start_di_layar_join_bukan_a():
    """Layar itu berbunyi 'Press START to join game'; A tidak berarti apa-apa.

    Urutan buta lama mengirim START -> A dan gagal live 30 Agu: active_menu
    bertahan `LocalCoopJoinMenu` dan farmer kedua tidak pernah muncul.
    """
    pad = PadPalsu()

    berhasil, alasan = _join_terpandu(
        pad, [None, G.MENU_JOIN_LOKAL],
        sudah_join=lambda: len(pad.tombol_ditekan()) >= 2,
    )

    assert berhasil, alasan
    assert pad.tombol_ditekan() == [G.TOMBOL_START, G.TOMBOL_START]
    assert G.TOMBOL_A not in pad.tombol_ditekan()
    assert not pad.ada_input()


def test_join_terpandu_memanggil_layar_join_dulu_kalau_belum_terbuka():
    """Tanpa menu apa pun, START pertama membuka layar join; START kedua join."""
    pad = PadPalsu()

    berhasil, alasan = _join_terpandu(
        pad, [None, G.MENU_JOIN_LOKAL],
        sudah_join=lambda: len(pad.tombol_ditekan()) >= 2,
    )

    assert berhasil, alasan
    assert pad.tombol_ditekan() == [G.TOMBOL_START, G.TOMBOL_START]
    assert not pad.ada_input()


def test_join_terpandu_melewati_tombol_kalau_p2_sudah_join():
    pad = PadPalsu()

    berhasil, alasan = _join_terpandu(
        pad, [G.MENU_JOIN_LOKAL], sudah_join=lambda: True,
    )

    assert berhasil, alasan
    assert pad.tombol_ditekan() == []
    assert not pad.ada_input()


def test_join_terpandu_berhenti_kalau_menu_lain_yang_terbuka():
    """GameMenu = pad terikat ke P1; START di situ membuka menu pause.

    Fail closed, dan JANGAN menekan B untuk membereskan menu milik orang lain.
    """
    pad = PadPalsu()

    berhasil, alasan = _join_terpandu(
        pad, ["GameMenu"], sudah_join=lambda: False,
    )

    assert not berhasil
    assert "GameMenu" in alasan
    assert pad.tombol_ditekan() == []
    assert not pad.ada_input()


def test_join_terpandu_tidak_menekan_start_tanpa_batas():
    """Layar join yang tuli tidak boleh dibanjiri START selamanya."""
    pad = PadPalsu()

    berhasil, alasan = _join_terpandu(
        pad, [None] + [G.MENU_JOIN_LOKAL] * 20, sudah_join=lambda: False,
        batas_konfirmasi=2,
    )

    assert not berhasil
    assert pad.tombol_ditekan() == [
        G.TOMBOL_START, G.TOMBOL_START, G.TOMBOL_START
    ]
    assert not pad.ada_input()


def test_join_terpandu_tidak_memakai_nama_karakter():
    """World/profil boleh ganti: keputusan join cuma dari jenis menu + hello."""
    import inspect
    sumber = inspect.getsource(G._join_terpandu)
    for nama in ("artihand", "host1", "player_name"):
        assert nama not in sumber, f"nama karakter bocor ke join: {nama}"


def test_join_terpandu_membatalkan_layar_join_milik_pad_lain():
    """Layar join yang bukan kita buka wajib di-Cancel dulu, baru dibuka ulang.

    Live 30 Agu: layar sudah terbuka sebelum pad virtual dibuat, tiga START
    berturut-turut tidak menggerakkannya sama sekali.
    """
    pad = PadPalsu()

    berhasil, alasan = _join_terpandu(
        pad, [G.MENU_JOIN_LOKAL, None, G.MENU_JOIN_LOKAL],
        sudah_join=lambda: pad.tombol_ditekan() == [
            G.TOMBOL_B, G.TOMBOL_START, G.TOMBOL_START
        ],
    )

    assert berhasil, alasan
    assert pad.tombol_ditekan() == [G.TOMBOL_B, G.TOMBOL_START, G.TOMBOL_START]
    assert not pad.ada_input()
