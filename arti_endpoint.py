"""Menebak apakah Bohan SUDAH selesai bicara, dari teks transkripnya.

Kenapa ada: ekor sunyi PTT dipatok 5,0 detik atas permintaan Bohan sendiri
(12 Agu 2026, "aku lemot mikir") supaya dia tidak kepotong saat menimbang
kata. Itu keputusan yang benar — TAPI harganya dibayar oleh SETIAP ucapan,
termasuk yang jelas-jelas sudah selesai seperti "iya", "gas", atau "hah?".
Padahal transkripsi Groq sendiri cuma ~630 ms; sisanya murni menunggu.

Jadi jangan memendekkan ekornya. Buat ADAPTIF: transkrip lebih awal, lalu
lihat teksnya — kalau kalimatnya menggantung, tunggu sabar seperti biasa;
kalau sudah utuh, jalan sekarang.

Modul ini sengaja MURNI (tanpa audio, tanpa jaringan) supaya penilaiannya
bisa diuji habis-habisan. Bagian yang berbahaya di sini bukan pipa-nya,
melainkan tebakannya.

TITIK BUTA YANG DIKETAHUI: kalimat menggantung yang berakhir kata ganti
("terus habis itu kita ...") dinilai SELESAI, karena tidak bisa dibedakan
dari "ini punya kita" tanpa model tata bahasa. Sengaja dibiarkan — daftar
yang memuat semua kata ganti akan membuat banyak kalimat utuh ikut
menunggu. Penutupnya ada di sisi pemanggil: commit awal baru boleh sesudah
hening melewati ambang aman, karena jeda berpikir di tengah kalimat
biasanya lebih pendek daripada hening sesudah kalimat benar-benar habis.

ATURAN TARUHAN: kalau ragu, bilang BELUM selesai. Salah tebak "belum" cuma
membuat Arti menunggu seperti sekarang (tidak ada yang rusak). Salah tebak
"sudah" berarti Bohan dipotong di tengah kalimat — persis keluhan yang dulu
melahirkan ekor 5 detik ini.
"""

from __future__ import annotations

import re

# Kata yang di UJUNG kalimat hampir selalu berarti masih ada lanjutannya.
# Sengaja KONSERVATIF. Yang TIDAK dimasukkan dan alasannya:
#   "terus"/"trus" -> "terus?" itu giliran utuh dalam obrolan
#   "gimana"       -> "menurut kamu gimana" sudah selesai
#   "apa"          -> "kamu lagi apa" sudah selesai
#   "ini"/"itu"    -> "coba yang ini" sudah selesai
#   "gitu"         -> justru sering menutup kalimat
_GANTUNG = frozenset({
    # konjungsi / preposisi
    "dan", "atau", "tapi", "tetapi", "karena", "soalnya", "sebab",
    "kalau", "kalo", "jika", "yang", "dengan", "sama", "buat", "untuk",
    "sambil", "biar", "supaya", "agar", "walaupun", "meskipun", "walau",
    "sedangkan", "sementara", "kecuali", "hingga", "sampai", "lalu",
    "kemudian", "makanya", "sehingga", "seperti", "kayak", "kaya",
    "dari", "ke", "di", "pada", "oleh", "tentang", "soal", "menurut",
    # ragu-ragu
    "hmm", "hmmm", "hm", "mmm", "mm", "eee", "ee", "eh", "anu",
    "umm", "um", "ehm", "emm", "em", "aaa", "aa",
})

# Dua kata terakhir yang menggantung ("apa ya", "gimana ya", ...).
_GANTUNG_2 = frozenset({
    "apa ya", "gimana ya", "apa namanya", "apa sih", "gimana sih",
    "itu loh", "anu itu", "apa tuh", "gimana tuh", "yang mana",
})

_BUKAN_HURUF = re.compile(r"[^\w\s']", re.UNICODE)


def _kata(teks: str) -> list[str]:
    bersih = _BUKAN_HURUF.sub(" ", teks.lower())
    return [w for w in bersih.split() if w]


def ucapan_terdengar_selesai(teks: str | None) -> bool:
    """True kalau transkrip ini terdengar sudah utuh.

    Dipakai untuk memutuskan boleh commit lebih awal atau harus menunggu
    ekor sunyi penuh. Ragu = False.
    """
    if not teks:
        return False
    mentah = teks.strip()
    if not mentah:
        return False

    # Tanda baca menggantung: koma / elipsis / tanda hubung di ujung.
    if re.search(r"[,\-–—]$", mentah) or mentah.endswith("..") or mentah.endswith("…"):
        return False

    kata = _kata(mentah)
    if not kata:
        return False

    # Cuma bunyi ragu-ragu, belum ada isi.
    if all(k in _GANTUNG for k in kata):
        return False

    if kata[-1] in _GANTUNG:
        return False

    if len(kata) >= 2 and " ".join(kata[-2:]) in _GANTUNG_2:
        return False

    return True


def alasan_belum_selesai(teks: str | None) -> str:
    """Penjelasan singkat untuk log — kenapa dianggap belum selesai."""
    if not teks or not teks.strip():
        return "kosong"
    mentah = teks.strip()
    if re.search(r"[,\-–—]$", mentah) or mentah.endswith("..") or mentah.endswith("…"):
        return "tanda baca menggantung"
    kata = _kata(mentah)
    if not kata:
        return "kosong"
    if all(k in _GANTUNG for k in kata):
        return "cuma bunyi ragu-ragu"
    if kata[-1] in _GANTUNG:
        return f"berakhir dengan '{kata[-1]}'"
    if len(kata) >= 2 and " ".join(kata[-2:]) in _GANTUNG_2:
        return f"berakhir dengan '{' '.join(kata[-2:])}'"
    return "selesai"
