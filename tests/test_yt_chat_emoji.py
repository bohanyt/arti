"""Emoji chat YouTube harus SAMPAI ke Arti.

Sampai 27 Agu parser memakai `r.get("text", "")`, padahal emoji datang sebagai `{"emoji": {...}}` tanpa kunci `text`. Dua akibatnya diam-diam:

    "halo <emoji>"  -> Arti cuma melihat "halo"
    "<emoji>" saja  -> pesan jadi kosong lalu DI-DROP oleh
                       `if not msg and not paid: return None`

Yang kedua paling buruk: penonton menulis sesuatu, dan Arti tidak pernah
tahu ada yang menulis.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_bridge as H  # noqa: E402

JOY = "\U0001F602"
HATI = "\u2764"


def test_teks_biasa_tidak_berubah():
    assert H.teks_pesan_chat([{"text": "halo bang"}]) == "halo bang"


def test_emoji_standar_ikut_terbaca():
    runs = [{"text": "halo "},
            {"emoji": {"emojiId": JOY, "isCustomEmoji": False,
                       "shortcuts": [":joy:"]}}]
    assert H.teks_pesan_chat(runs) == f"halo {JOY}"


def test_pesan_HANYA_emoji_tidak_lagi_kosong():
    """Ini yang paling penting: pesan emoji-saja dulu di-drop total."""
    runs = [{"emoji": {"emojiId": HATI, "isCustomEmoji": False}}]
    assert H.teks_pesan_chat(runs) == HATI


def test_emote_kustom_jadi_NAMA_bukan_kosong():
    """Gambar tidak berarti apa-apa buat model; namanya yang membawa makna."""
    runs = [{"emoji": {"emojiId": "UCabc/xyz123456", "isCustomEmoji": True,
                       "shortcuts": [":_bohanLove:"]}}]
    assert H.teks_pesan_chat(runs) == ":_bohanLove:"


def test_emote_kustom_tanpa_shortcut_pakai_label_aksesibilitas():
    runs = [{"emoji": {"emojiId": "UCabc/zzz999999",
                       "image": {"accessibility": {
                           "accessibilityData": {"label": "bohanSad"}}}}}]
    assert H.teks_pesan_chat(runs) == ":bohanSad:"


def test_tanpa_bendera_isCustomEmoji_masih_bisa_dibedakan():
    """Tidak semua payload membawa benderanya."""
    pendek = [{"emoji": {"emojiId": HATI}}]
    panjang = [{"emoji": {"emojiId": "UCabc/panjangsekali", "shortcuts": [":x:"]}}]
    assert H.teks_pesan_chat(pendek) == HATI
    assert H.teks_pesan_chat(panjang) == ":x:"


def test_run_sampah_fail_closed():
    for sampah in ([{"foo": 1}], [None], ["bukan dict"], "bukan list", None, []):
        assert H.teks_pesan_chat(sampah) == ""


def test_emoji_tanpa_nama_apa_pun_tidak_bikin_teks_aneh():
    runs = [{"text": "halo"}, {"emoji": {"emojiId": "UCabc/tanpa_nama_sama_sekali"}}]
    assert H.teks_pesan_chat(runs) == "halo"


def test_tee_tidak_meledak_saat_terminal_cp1252():
    """Satu emoji dari penonton tidak boleh menjatuhkan chat worker."""
    class StreamCp1252:
        encoding = "cp1252"
        def __init__(self): self.keluar = []
        def write(self, d):
            d.encode("cp1252")      # meniru terminal Windows
            self.keluar.append(d)
        def flush(self): pass

    class BerkasLog:
        def __init__(self): self.keluar = []
        def write(self, d): self.keluar.append(d)
        def flush(self): pass

    term, berkas = StreamCp1252(), BerkasLog()
    tee = H._TeeOutput(term, berkas)
    tee.write(f"[YT Chat] @orang: {JOY}\n")          # tidak boleh raise
    assert berkas.keluar and JOY in berkas.keluar[0], "log HARUS simpan aslinya"
    assert term.keluar, "terminal tetap dapat sesuatu"
    assert JOY not in term.keluar[0], "terminal dapat versi pengganti"
