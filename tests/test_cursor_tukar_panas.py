"""Tukar panas: pengganti dipanaskan SEBELUM sesi lama dibuang.

Tanpa tukar panas, tiap pemanasan ulang memakan belasan detik, dan selama
jendela itu SEMUA giliran jatuh ke Groq. Padahal daur ulangnya sendiri benar
(spike: sesi hangat menjawab pertanyaan mirip nyaris verbatim; pembengkakan
konteks cuma 1,05x/20 turn). Yang salah urutannya: bunuh dulu, panaskan
belakangan.
"""

import arti_cursor_agent as ca


class SesiPalsu:
    def __init__(self, *, warmed=True, dirty=False, punya_agen=True):
        self._agent = object() if punya_agen else None
        self.warmed = warmed
        self.dirty = dirty
        self.ditutup_dengan_detach = None

    def close(self, detach=False):
        self.ditutup_dengan_detach = detach


# --------------------------------------------------------------------------- #
# KEBIJAKAN KEMBAR (18 Agu 2026, log 22.18: 24 timeout / breaker 7x / tukar
# panas 0x): cadangan dipanaskan SELALU, dan sesi aktif yang mati muda langsung
# digantikan kembarannya di prewarm — bukan pemanasan penuh 18 dtk via Groq.
# --------------------------------------------------------------------------- #

def _siapkan_prewarm(monkeypatch):
    monkeypatch.setattr(ca, "is_available", lambda cfg: (True, ""))
    monkeypatch.setattr(ca, "_breaker_allows_attempt", lambda cfg: True)
    monkeypatch.setattr(ca, "_warming", False)
    monkeypatch.setattr(ca, "_standby_warming", False)


def test_prewarm_selalu_menyiapkan_kembaran(monkeypatch):
    """Sesi aktif sehat -> cadangan tetap dipanaskan (bukan cuma dekat batas)."""
    _siapkan_prewarm(monkeypatch)
    monkeypatch.setattr(ca, "_session", SesiPalsu())
    monkeypatch.setattr(ca, "_standby", None)
    dipanggil = []
    monkeypatch.setattr(ca, "panaskan_cadangan", lambda cfg: dipanggil.append(1))

    assert ca.prewarm({}) is True
    assert dipanggil, "kembaran tidak disiapkan padahal sesi aktif sehat"


def test_sesi_mati_muda_langsung_diganti_kembaran(monkeypatch, capsys):
    """Inti keluhan 22.18: aktif kotor (timeout) + kembaran hangat ->
    tukar takhta SEKARANG, prewarm lapor hangat, kembaran baru disiapkan."""
    _siapkan_prewarm(monkeypatch)
    mati = SesiPalsu(dirty=True)
    kembaran = SesiPalsu()
    monkeypatch.setattr(ca, "_session", mati)
    monkeypatch.setattr(ca, "_standby", kembaran)
    dipanggil = []
    monkeypatch.setattr(ca, "panaskan_cadangan", lambda cfg: dipanggil.append(1))

    assert ca.prewarm({}) is True, "harusnya hangat via tukar panas, bukan False"
    assert ca._session is kembaran
    assert mati.ditutup_dengan_detach is True
    assert dipanggil, "kembaran pengganti tidak disiapkan untuk kematian berikutnya"
    assert "tukar panas" in capsys.readouterr().out


def test_tanpa_kembaran_prewarm_tetap_tidak_memblokir(monkeypatch):
    """Aktif mati + cadangan belum hangat -> jalur lama: False (Groq dulu),
    pemanasan jalan di latar."""
    _siapkan_prewarm(monkeypatch)
    monkeypatch.setattr(ca, "_session", SesiPalsu(dirty=True))
    monkeypatch.setattr(ca, "_standby", SesiPalsu(warmed=False))

    class _ThreadPalsu:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(ca.threading, "Thread", _ThreadPalsu)
    assert ca.prewarm({}) is False


# --------------------------------------------------------------------------- #
# Pertukaran itu sendiri
# --------------------------------------------------------------------------- #

def test_cadangan_hangat_naik_takhta_dan_yang_lama_ditutup(monkeypatch, capsys):
    lama, cadangan = SesiPalsu(), SesiPalsu()
    monkeypatch.setattr(ca, "_session", lama)
    monkeypatch.setattr(ca, "_standby", cadangan)

    assert ca.tukar_ke_cadangan("mencapai 20 turn") is True

    assert ca._session is cadangan
    assert ca._standby is None
    # detach=True: teardown sesi lama tidak boleh menambah latensi giliran ini.
    assert lama.ditutup_dengan_detach is True
    assert "tukar panas" in capsys.readouterr().out


def test_tanpa_cadangan_tidak_menyentuh_sesi_aktif(monkeypatch):
    lama = SesiPalsu()
    monkeypatch.setattr(ca, "_session", lama)
    monkeypatch.setattr(ca, "_standby", None)

    assert ca.tukar_ke_cadangan("umur habis") is False

    assert ca._session is lama
    assert lama.ditutup_dengan_detach is None


def test_cadangan_belum_hangat_tidak_dipakai(monkeypatch):
    lama = SesiPalsu()
    monkeypatch.setattr(ca, "_session", lama)
    monkeypatch.setattr(ca, "_standby", SesiPalsu(warmed=False))

    assert ca.tukar_ke_cadangan("mencapai 20 turn") is False
    assert ca._session is lama


def test_cadangan_kotor_tidak_dipakai(monkeypatch):
    """Menukar sesi sehat dengan sesi rusak = memperburuk keadaan."""
    lama = SesiPalsu()
    monkeypatch.setattr(ca, "_session", lama)
    monkeypatch.setattr(ca, "_standby", SesiPalsu(dirty=True))

    assert ca.tukar_ke_cadangan("mencapai 20 turn") is False
    assert ca._session is lama
