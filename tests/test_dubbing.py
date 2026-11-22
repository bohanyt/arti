"""Arti dubbing v1 (19 Agu 2026) — kurasi, pelacak efek-ketik, preset suara.

Bagian OCR/window/TTS diuji lewat spike & demo (butuh GPU/engine); di sini
semua logika murninya: klasifikasi kotak, penggabung subtitle multi-baris
(PR #1 spike), gerbang stabilitas + dedup efek-ketik, preset gender, dan
kebijakan telat-lebih-buruk-daripada-skip.
"""

from __future__ import annotations

from pathlib import Path

import arti_dubbing as ad

_SRC = (Path(__file__).resolve().parent.parent / "arti_dubbing.py"
        ).read_text(encoding="utf-8")

W, H = 848, 477


def _box(cx: float, cy: float, lebar: float = 0.3):
    x0, x1 = (cx - lebar / 2) * W, (cx + lebar / 2) * W
    y0, y1 = (cy - 0.02) * H, (cy + 0.02) * H
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# --------------------------------------------------------------------------
# Klasifikasi + gabung multi-baris
# --------------------------------------------------------------------------

def test_subtitle_bawah_tengah_kalimat():
    assert ad.klasifikasi_kotak(_box(0.5, 0.88), "Aku harus menemukan kunci", W, H) == "SUBTITLE"


def test_chat_pojok_dan_hp_bar_terbuang():
    assert ad.klasifikasi_kotak(_box(0.06, 0.05), "WRONG TIME FOR THIS", W, H) == "UI"
    assert ad.klasifikasi_kotak(_box(0.9, 0.8), "175 / 175", W, H) == "UI"


def test_gabung_subtitle_dua_baris_urut():
    """PR #1 spike: 'Lay Traps...' kehilangan baris kedua."""
    baris2 = (_box(0.5, 0.92), "get a Weapon from the Storage Room.")
    baris1 = (_box(0.5, 0.87), "Lay Traps, Barricade Doors and")
    assert ad.gabung_subtitle([baris2, baris1]) == (
        "Lay Traps, Barricade Doors and get a Weapon from the Storage Room."
    )


def test_zona_dinilai_sebelum_jumlah_kata():
    """Temuan demo: pecahan baris 'Room.' (1 kata) terbuang sebelum
    digabung. Zona subtitle memungut SEMUA kotak, penilaian belakangan."""
    assert ad.di_zona_subtitle(_box(0.5, 0.92), W, H)
    assert not ad.di_zona_subtitle(_box(0.06, 0.05), W, H)  # chat pojok


def test_rasio_huruf_menolak_sampah_angka():
    """Temuan demo ronde 2: stempel '7/27/26' dan ukur tinggi dinding
    '2016-3'11\"' sempat ikut di-dub."""
    assert ad.rasio_huruf("7/27/26") < 0.35
    assert ad.rasio_huruf('2016-3\'11" 2015-3\'8"') < 0.35
    assert ad.rasio_huruf("Terkunci, aku harus menemukan kunci pintunya.") > 0.5


def test_rapikan_teks_spasi_tanda_baca():
    assert ad.rapikan_teks("Terkunci,aku harus pergi.Ini gawat") == (
        "Terkunci, aku harus pergi. Ini gawat"
    )
    assert ad.rapikan_teks("Harga $349,900 aman") == "Harga $349,900 aman"  # angka utuh


# --------------------------------------------------------------------------
# Pelacak: stabilitas + efek ketik + dedup
# --------------------------------------------------------------------------

def test_efek_ketik_diucap_sekali_versi_penuh():
    p = ad.PelacakDialog(stabil_frame=2)
    assert p.masuk("Aku tidak sabar menyantap ini! In") is None       # masih tumbuh
    penuh = "Aku tidak sabar menyantap ini! Ini jadi makan malam terbaik yg pernah ada."
    assert p.masuk(penuh) is None                                     # baru 1 frame stabil
    assert p.masuk(penuh) == penuh                                    # stabil 2 frame -> ucap
    assert p.masuk(penuh) is None                                     # jangan diulang


def test_dialog_baru_setelah_dialog_lama():
    p = ad.PelacakDialog(stabil_frame=2)
    a, b = "Kalimat pertama yang muncul di layar.", "Kalimat kedua yang berbeda total isinya."
    p.masuk(a)
    assert p.masuk(a) == a
    p.masuk(b)
    assert p.masuk(b) == b


def test_layar_kosong_reset_kandidat():
    p = ad.PelacakDialog(stabil_frame=2)
    p.masuk("Sesuatu yang belum stabil di layar")
    assert p.masuk("") is None
    assert p.masuk("Sesuatu yang belum stabil di layar") is None  # hitung ulang dari 1


def test_versi_pendek_dari_yang_sudah_diucap_tidak_diulang():
    p = ad.PelacakDialog(stabil_frame=1)
    penuh = "Aku tidak sabar menyantap ini! Ini jadi makan malam terbaik."
    assert p.masuk(penuh) == penuh
    assert p.masuk("Aku tidak sabar menyantap ini!") is None


def test_kalimat_lanjutan_cuma_diucap_ekornya():
    """Efek ketik yang sempat 'stabil' lalu lanjut: jangan ulang dari awal."""
    p = ad.PelacakDialog(stabil_frame=1)
    awal = "Aku tidak sabar menyantap ini!"
    penuh = "Aku tidak sabar menyantap ini! Ini jadi makan malam terbaik."
    assert p.masuk(awal) == awal
    assert p.masuk(penuh) == "Ini jadi makan malam terbaik."


# --------------------------------------------------------------------------
# Nama tokoh -> preset gender (keputusan Bohan: cowok turun, cewek naik)
# --------------------------------------------------------------------------

def test_deteksi_nama_dan_preset():
    cfg = dict(ad.DEFAULTS)
    cfg["dub_nama_gender"] = {"seka": "cewek", "budi": "cowok"}
    nama, isi = ad.deteksi_nama("Seka: kamu mau ke mana?")
    assert (nama, isi) == ("seka", "kamu mau ke mana?")
    assert ad.pilih_preset(nama, cfg) == "cewek"
    assert ad.pilih_preset("budi", cfg) == "cowok"
    assert ad.pilih_preset("orang asing", cfg) == "netral"
    assert ad.pilih_preset(None, cfg) == "netral"


def test_teks_tanpa_label_tidak_dipotong():
    nama, isi = ad.deteksi_nama("Terkunci, aku harus menemukan kunci pintunya.")
    assert nama is None and isi.startswith("Terkunci")


def test_config_preset_tidak_menyentuh_kunci_produksi():
    """Suara dasar Arti TERKUNCI — preset dub = dict overlay baru."""
    cfg = dict(ad.DEFAULTS)
    hasil = ad.config_preset("cowok", cfg)
    assert hasil["supertonic_pitch_semitone"] == -3.0
    assert ad.config_preset("cewek", cfg)["supertonic_pitch_semitone"] == 5.0
    assert "supertonic_voice" not in hasil  # cuma kunci polesan, bukan engine
    assert "dub_preset_semitone" in ad.DEFAULTS  # sumber tunggal


# --------------------------------------------------------------------------
# Kebijakan antrian & pagar proyek
# --------------------------------------------------------------------------

def test_antrian_membuang_yang_tua(capsys):
    suara = ad.SuaraDub({**ad.DEFAULTS, "dub_antrian_maks": 1}, tanpa_suara=True)
    suara.antrian.append(("baris lama yang telat", "netral"))
    suara.ucap("baris baru yang sekarang", "netral")
    out = capsys.readouterr().out
    assert "SKIP" in out and "baris lama" in out
    assert "baris baru yang sekarang" in out


def test_dub_tercatat_ke_log(tmp_path):
    cfg = dict(ad.DEFAULTS)
    cfg["dub_log_path"] = str(tmp_path / "dub.jsonl")
    ad.catat_log(cfg, "seka", "halo dunia", "cewek")
    import json
    baris = json.loads((tmp_path / "dub.jsonl").read_text(encoding="utf-8"))
    assert baris["nama"] == "seka" and baris["preset"] == "cewek"


def test_tanpa_llm_dan_tanpa_composer():
    """Jalur dub = nol token (desain): tidak ada klien LLM yang diimpor."""
    for larangan in ("arti_openrouter", "arti_gemini_vision", "groq",
                     "arti_cursor_agent"):
        assert larangan not in _SRC, f"jalur dub harus bebas LLM: {larangan}"


def test_privasi_mata_meram_saat_game_tidak_fokus():
    """Aturan privasi v1 tertanam di loop: capture hanya saat di_depan()."""
    blok = _SRC.split("while True:")[1][:400]
    assert "di_depan()" in blok
