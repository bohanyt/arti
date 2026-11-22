"""Epidemi nyangkut, lapis kedua (Bohan 2026-08-10: "[recorded remark omitted]").

Tiga jurus baru di bot.js:
1. Titik nyangkut (posisi mematung DAN tujuan roam yang gagal) di-blacklist
   5 menit — kandidat roam di dekatnya ditolak.
2. Semua kandidat ditolak -> ronde dekat di sekitar POSISI DIA; kalau tetap
   buntu, JUJUR bilang buntu (return null) — bukan "jalan ke yang terakhir"
   yang sudah terbukti mustahil (resep pasti mematung 20 detik).
3. Nyangkut 2x beruntun di titik sama / terkurung total -> galiKeluar():
   bongkar 1-2 blok penghalang pakai alat, dengan rem 3x per menit.

Perilaku bot.js tidak bisa diuji unit dari Python — yang dikunci di sini
adalah SUMBERNYA, supaya refactor yang menghapus jurusnya ketahuan.
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "mc-bot" / "bot.js").read_text(
    encoding="utf-8")


def _potong(mulai: str, akhir: str) -> str:
    i = SRC.index(mulai)
    return SRC[i:SRC.index(akhir, i)]


# --- galiKeluar -----------------------------------------------------------

def test_gali_keluar_ada_dan_pakai_alat():
    badan = _potong("async function galiKeluar", "\nfunction ")
    assert "equipForBlock" in badan, "gali tanpa alat = lambat & tidak jujur"
    assert "bot.dig(" in badan


def test_gali_keluar_menolak_cairan():
    badan = _potong("async function galiKeluar", "\nfunction ")
    assert "'lava'" in badan and "'water'" in badan
    assert "cair(mata) || cair(kaki)" in badan, (
        "menembus dinding berisi air/lava lebih buruk daripada nyangkut")


def test_gali_keluar_punya_rem():
    badan = _potong("async function galiKeluar", "\nfunction ")
    assert "galiKeluarRiwayat.length >= 3" in badan, (
        "tanpa rem, kurungan yang tak tergali jadi loop gali tanpa akhir")
    assert "GALI_KELUAR_JEDA_MS" in SRC


def test_detektor_nyangkut_diam_saat_menggali():
    badan = _potong("const STUCK_MS", "startRoam('unstuck')")
    assert "if (sedangGaliKeluar) return" in badan, (
        "menggali = diam disengaja; jangan dicap nyangkut di tengah usahanya")


# --- daftar hitam titik nyangkut -----------------------------------------

def test_titik_nyangkut_diingat_dan_dipakai_menolak_kandidat():
    assert "NYANGKUT_INGAT_MS = 5 * 60 * 1000" in SRC
    pemilih = _potong("function pilihTujuanTerjangkau", "\nfunction startRoam")
    assert pemilih.count("dekatTitikNyangkut(t)") >= 2, (
        "blacklist harus dicek di ronde jauh DAN ronde dekat")


def test_nyangkut_mengingat_posisi_dan_tujuan_yang_gagal():
    badan = _potong("reason: 'stuck_timeout'", "startRoam('unstuck')")
    assert "titikNyangkut.push({ x: p.x" in badan
    assert "roamTarget.x" in badan, (
        "tujuan roam yang gagal dicapai juga harus di-blacklist — kalau tidak, "
        "dia memilih ulang tujuan yang sama dan mematung lagi di tempat sama")


def test_streak_dua_kali_di_titik_sama_memicu_gali():
    badan = _potong("reason: 'stuck_timeout'", "startRoam('unstuck')")
    assert "nyangkutStreak >= 2" in badan
    assert "galiKeluar('nyangkut berulang')" in badan


# --- pemilih tujuan: jujur saat buntu ------------------------------------

def test_pemilih_tidak_lagi_jalan_ke_tujuan_mustahil():
    pemilih = _potong("function pilihTujuanTerjangkau", "\nfunction startRoam")
    assert "return terakhir" not in pemilih, (
        "jalan ke kandidat yang SUDAH terbukti tak terjangkau = resep mematung")
    assert "return null" in pemilih


def test_pemilih_punya_ronde_dekat_berpusat_posisi_dia():
    pemilih = _potong("function pilihTujuanTerjangkau", "\nfunction startRoam")
    assert "bot.entity.position" in pemilih, (
        "ronde cadangan harus di sekitar POSISI DIA — pickRoamTarget berpusat "
        "di rumah, dan rumah bisa jauh dari tempat dia terjebak")


def test_start_roam_menggali_saat_terkurung_dan_tidak_spin_loop():
    badan = _potong("function startRoam", "\n}")
    assert "galiKeluar('terkurung')" in badan
    assert "if (tembus) { startRoam('lolos_gali'); return }" in badan, (
        "startRoam ulang tanpa syarat tembus = spin-loop gali-gagal-gali")
    assert "roamTarget = pickRoamTarget()" in badan, (
        "gali gagal -> jalan ke kandidat mentah (A/B 2026-08-08: mematung "
        "dengan task 'roam' lebih buruk daripada tujuan mustahil)")


def test_blacklist_radius_kecil_dan_self_healing():
    """Harness 2026-08-10: radius 8 x 20 titik meracuni seluruh cincin
    kandidat — dia divonis "terkurung" di TEMPAT TERBUKA berulang-ulang
    (buktinya: gali "tidak ada arah yang bisa dibongkar"). Radius kini 5,
    dan bukti-terbuka itu dipakai untuk MEMBERSIHKAN daftar hitam basi."""
    assert "const NYANGKUT_RADIUS = 5" in SRC
    i = SRC.index("tidak ada arah yang bisa dibongkar`)")
    blok = SRC[i:i + 900]
    assert "sebab === 'terkurung'" in blok
    assert "titikNyangkut = titikNyangkut.filter" in blok
    assert "dibersihkan" in blok


def test_dunia_belum_termuat_tidak_divonis_terkurung():
    """Pasca-death/respawn chunk belum ada -> semua cek jalur gagal instan ->
    "terkurung" di udara kosong (terlihat berulang di harness). Dunia belum
    termuat = jalan ke kandidat mentah dulu."""
    pemilih = _potong("function pilihTujuanTerjangkau", "\nfunction startRoam")
    i = pemilih.index("if (!kaki) return pickRoamTarget()")
    assert "let ditolak" not in pemilih[:i], (
        "pagar chunk harus PALING ATAS — sebelum satu pun pemeriksaan jalur")
