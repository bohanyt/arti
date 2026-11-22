"""Tugas yang disela harus dikembalikan, bukan dilupakan.

Sampai 12 Agu 2026 aturannya "[recorded remark omitted]": Arti menambang di
titik X, viewer minta sesuatu, titik X hilang selamanya. Yang menariknya
kembali cuma nudge/takdir — arah umum, bukan pekerjaan konkret.

Keputusan Bohan (12 Agu):
- `stop`/`leave`/`pulang`/`tidur`/`come`/`follow`/`goal` = BATALKAN ingatan
- `kabur`/`serang`/refleks = cuma menyela; sesudahnya balik ke pekerjaan tadi
- waktu balik, Arti NGOMONG singkat
"""

from pathlib import Path

from arti_minecraft import ReactionLimiter, map_event_to_reaction


def _reaksi(ev):
    """Bungkus tipis — limiter/now/config tidak relevan buat event lanjut-tugas."""
    return map_event_to_reaction(ev, ReactionLimiter(), 100.0, {})


_BOT_JS = (
    Path(__file__).resolve().parent.parent / "mc-bot" / "bot.js"
).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Sisi bot: ingatan, pembatal, penanda generasi
# --------------------------------------------------------------------------- #

def test_verb_panjang_masuk_daftar_bisa_dilanjut():
    blok = _BOT_JS.split("const TUGAS_BISA_LANJUT")[1].split(")")[0]
    for verb in ("mine", "turun", "jembatan", "bangun"):
        assert f"'{verb}'" in blok


def test_perintah_sudahi_membatalkan_ingatan():
    blok = _BOT_JS.split("const PEMBATAL_LANJUT")[1].split(")")[0]
    for verb in ("stop", "leave", "pulang", "tidur", "come", "follow", "goal"):
        assert f"'{verb}'" in blok


def test_kabur_dan_serang_bukan_pembatal():
    """Justru sesudah lari dari zombie, balik ke tambang paling masuk akal."""
    blok = _BOT_JS.split("const PEMBATAL_LANJUT")[1].split(")")[0]
    assert "'kabur'" not in blok
    assert "'serang'" not in blok


def test_nambang_berhenti_kalau_diambil_alih():
    """Tanpa penanda generasi, loop mine terus jalan di belakang layar sementara
    aksi baru juga berjalan — dua tugas berebut badan bot yang sama."""
    assert "const epok = epokTugas" in _BOT_JS
    assert "if (epok !== epokTugas)" in _BOT_JS


def test_penyambungan_menunggu_promise_handler():
    """Handler-nya async: `try/catch` sinkron tidak tahu kapan tugasnya selesai."""
    assert "Promise.resolve(hasil)" in _BOT_JS
    assert "sesudahPerintah" in _BOT_JS


def test_ada_batas_kedaluwarsa():
    assert "LANJUT_KEDALUWARSA_MS = 180000" in _BOT_JS


# --------------------------------------------------------------------------- #
# Sisi narasi
# --------------------------------------------------------------------------- #

def test_balik_kerja_diceritakan():
    baris = _reaksi({"ev": "lanjut_tugas", "task": "mine", "jeda_dtk": 40})
    assert baris is not None
    assert "menambang" in baris
    assert "jangan sebut angka" in baris


def test_disela_tidak_bicara():
    """Saat itu Arti sedang menjawab permintaan yang menyelanya — dia tidak boleh
    menyela dirinya sendiri cuma untuk mengumumkan penundaan."""
    assert _reaksi({"ev": "tugas_disela", "task": "mine"}) is None


def test_batal_lanjut_tidak_bicara():
    assert _reaksi(
        {"ev": "lanjut_batal", "task": "mine", "sebab": "stop"}
    ) is None


def test_task_tak_dikenal_tetap_punya_kalimat():
    baris = _reaksi({"ev": "lanjut_tugas", "task": "entah"})
    assert baris is not None
    assert "pekerjaan tadi" in baris
