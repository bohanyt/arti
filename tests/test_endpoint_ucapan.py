"""Penjaga penilaian "sudah selesai bicara atau belum".

Taruhannya tidak simetris, dan tes ini menegakkan itu:

- Salah bilang BELUM selesai  -> Arti menunggu seperti sekarang. Nol rusak.
- Salah bilang SUDAH selesai  -> streamer dipotong di tengah kalimat, persis
  masalah yang melahirkan ekor sunyi 5 detik.

Jadi kalau ada kasus ambigu, yang benar adalah menahan diri.
"""

import pytest

from arti_endpoint import alasan_belum_selesai, ucapan_terdengar_selesai


# --- HARUS dianggap SELESAI (ini yang bikin Arti terasa cepat) -------------
@pytest.mark.parametrize("teks", [
    "iya",
    "gas",
    "hah?",
    "bener juga",
    "coba lagi deh",
    "menurut kamu gimana",
    "kamu lagi apa",
    "terus?",                       # giliran utuh dalam obrolan
    "coba yang ini",
    "ya udah gitu aja",
    "arti, buka minecraftnya",
    "aku mau tanya soal setting grafisnya",
    "wah gila sih",
    "oke deh makasih ya",
])
def test_kalimat_utuh_boleh_jalan_duluan(teks):
    assert ucapan_terdengar_selesai(teks) is True, alasan_belum_selesai(teks)


# --- HARUS dianggap BELUM selesai (ini yang melindungi Bohan) -------------
@pytest.mark.parametrize("teks", [
    "jadi tadi aku mikir kalau",
    "aku mau coba yang",
    "ini tuh kayak",
    "hmm",
    "eee",
    "anu",
    "apa ya",
    "gimana ya",
    "aku pengen bikin fitur yang",
    "soalnya kemarin itu karena",
    "kita mulai dari",
    "aku sama",
    "bikin yang mirip sama",
    "coba deh, kalau",           # koma di ujung
    "aku mau bilang...",          # elipsis
    "menurut aku sih -",          # tanda hubung menggantung
    "",
    None,
])
def test_kalimat_menggantung_harus_ditunggu(teks):
    assert ucapan_terdengar_selesai(teks) is False


def test_ragu_ragu_di_depan_tidak_menghalangi():
    # "hmm" di AWAL tidak berarti menggantung — isinya sudah ada.
    assert ucapan_terdengar_selesai("hmm iya bener") is True
    assert ucapan_terdengar_selesai("eee oke gas") is True


def test_alasan_bisa_dibaca_manusia():
    assert "kalau" in alasan_belum_selesai("aku mikir kalau")
    assert alasan_belum_selesai("iya") == "selesai"
    assert alasan_belum_selesai("") == "kosong"
    assert "ragu" in alasan_belum_selesai("hmm eee")


def test_kata_yang_SENGAJA_tidak_dianggap_menggantung():
    """Regresi: pernah tergoda memasukkan kata-kata ini, jangan diulang.

    Semuanya lazim menutup kalimat dalam bahasa Indonesia lisan, dan
    memasukkannya berarti Arti menunggu 5 detik untuk giliran yang sudah
    jelas selesai.
    """
    for teks in ("terus?", "gimana", "kamu lagi apa", "coba yang ini",
                 "ya gitu", "yang itu"):
        assert ucapan_terdengar_selesai(teks) is True, teks


def test_TITIK_BUTA_yang_diketahui():
    """Kalimat menggantung yang berakhir kata ganti TIDAK terdeteksi.

    "terus habis itu kita ..." dinilai SELESAI karena berakhir "kita" —
    dan itu tidak bisa dibedakan dari "ini punya kita" tanpa model tata
    bahasa. Memasukkan semua kata ganti ke daftar menggantung akan membuat
    banyak kalimat yang memang selesai ikut menunggu 5 detik.

    Titik buta ini SENGAJA dibiarkan, dan ditutup oleh pengaman kedua yang
    tidak bergantung tata bahasa: commit awal baru boleh sesudah hening
    melewati `asr_ptt_ekor_aman_sec` (jeda berpikir di tengah kalimat
    biasanya lebih pendek daripada hening sesudah kalimat benar-benar
    selesai). Tes ini ada supaya batas itu terlihat, bukan terlupa.
    """
    assert ucapan_terdengar_selesai("terus habis itu kita") is True
