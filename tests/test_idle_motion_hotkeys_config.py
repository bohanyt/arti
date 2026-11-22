"""Penjaga: daftar hotkey motion idle bisa diganti lewat config_local.

Dibuat 25 Agu 2026 supaya motion percobaan (salinan tanpa kurva kepala) bisa
diuji LIVE sambil Arti benar-benar bicara — tanpa mengedit kode dan tanpa
menyentuh hotkey produksi.

Aturan #7: nilai default DIUJI terhadap CONFIG produksi, bukan dict kosong.
Pengingat kalau `idle_motion_hotkeys` tertinggal terisi sesudah sesi uji
TIDAK ditaruh di sini: bridge mencetaknya tiap startup. Suite merah harus
berarti kode rusak, bukan config sedang mode uji.
"""

import arti_bridge as H


def test_default_memakai_daftar_bawaan(monkeypatch):
    # Versi pertama tes ini menuntut config_local Bohan kosong, dan langsung
    # merah begitu dia menjalankan sesi uji. Salah tempat: suite merah berarti
    # KODE rusak, bukan "config sedang mode uji". Pengingat sesi uji sekarang
    # dicetak bridge tiap startup (lihat _idle_motion_names).
    monkeypatch.setitem(H.CONFIG, "idle_motion_hotkeys", [])
    assert H._idle_motion_names() == list(H.IDLE_MOTION_HOTKEYS)


def test_config_menang_kalau_diisi(monkeypatch):
    monkeypatch.setitem(H.CONFIG, "idle_motion_hotkeys", ["UjiTanpaKepala"])
    assert H._idle_motion_names() == ["UjiTanpaKepala"]


def test_entri_kosong_dan_spasi_dibuang(monkeypatch):
    monkeypatch.setitem(H.CONFIG, "idle_motion_hotkeys", ["  A  ", "", "   ", "B"])
    assert H._idle_motion_names() == ["A", "B"]


def test_daftar_isi_kosong_jatuh_ke_bawaan(monkeypatch):
    # Semua entri kosong = seperti tidak diisi; JANGAN sampai nol motion,
    # karena itu mematikan seluruh gerakan idle tanpa pesan apa pun.
    monkeypatch.setitem(H.CONFIG, "idle_motion_hotkeys", ["", "  "])
    assert H._idle_motion_names() == list(H.IDLE_MOTION_HOTKEYS)


def test_bawaan_produksi_adalah_motion_BERSIH():
    """27 Agu, dibuktikan live: motion mentah (ArtiIdle*) menulis 62 parameter
    yang sama dengan berkas ekspresi — termasuk ParamMouthOpenY — sehingga
    lipsync, lampu, dan titik tiga ditimpa selama motion berputar.

    Selama bawaannya masih IdleMotion1..5, saran "kosongkan
    idle_motion_hotkeys" MENGEMBALIKAN bug itu.
    """
    assert H.IDLE_MOTION_HOTKEYS == [
        "ArtiMotionBersih1", "ArtiMotionBersih2", "ArtiMotionBersih3",
        "ArtiMotionBersih4", "ArtiMotionBersih5",
    ]


def test_daftar_produksi_di_config_TIDAK_disebut_mode_uji(monkeypatch, capsys):
    """Versi lama berteriak "MODE UJI" apa pun isi daftarnya — termasuk saat
    isinya persis rotasi produksi."""
    monkeypatch.setattr(H, "_idle_motion_uji_diwartakan", False)
    monkeypatch.setitem(H.CONFIG, "idle_motion_hotkeys", list(H.IDLE_MOTION_HOTKEYS))
    capsys.readouterr()
    hasil = H._idle_motion_names()
    keluar = capsys.readouterr().out
    assert hasil == list(H.IDLE_MOTION_HOTKEYS)
    assert "MODE UJI" not in keluar
    assert "Motion rotasi" in keluar


def test_nama_ASING_tetap_diperingatkan(monkeypatch, capsys):
    monkeypatch.setattr(H, "_idle_motion_uji_diwartakan", False)
    monkeypatch.setitem(H.CONFIG, "idle_motion_hotkeys",
                        ["ArtiMotionBersih1", "UjiTanpaAngguk"])
    capsys.readouterr()
    H._idle_motion_names()
    keluar = capsys.readouterr().out
    assert "MODE UJI" in keluar and "UjiTanpaAngguk" in keluar
