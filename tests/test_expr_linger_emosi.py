"""Linger emosi (A3): mood tidak mati seketika di akhir giliran.

Akar masalahnya (25 Agu, keluhan Bohan): Arti selesai mengucapkan kalimat
sedih lalu wajahnya SEKETIKA netral — seperti saklar, bukan orang.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_expression_runtime as E  # noqa: E402


class FakeVTS:
    def __init__(self):
        self.state = []
        self.expr = []       # (berkas, aktif)
        self.fade = []

    async def trigger_expression_state(self, nama):
        self.state.append(nama)

    async def send_expression(self, berkas, aktif, confirm=False, fade=0.0):
        self.expr.append((berkas, aktif))
        self.fade.append(fade)


def _mati(vts):
    return [f for f, aktif in vts.expr if not aktif]


def _cfg(**over):
    cfg = {"expression_emotion_enabled": True, "expression_emosi_linger_sec": 0.0}
    cfg.update(over)
    return cfg


def test_bawaan_source_mati():
    """0.0 = perilaku lama. Bawaan MATI supaya tidak menumpuk ke sesi uji
    yang sudah memuat enam perubahan lain."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert '"expression_emosi_linger_sec": 0.0,' in src


def test_linger_nol_mematikan_mood_seketika():
    async def jalan():
        vts = FakeVTS()
        await E.apply_turn_end(vts, _cfg())
        return vts
    vts = asyncio.run(jalan())
    assert vts.state == ["default"]
    assert set(_mati(vts)) == set(E._ALL_MOOD_FILES)


def test_linger_menunda_mood_tapi_TIDAK_menunda_lampu():
    """Lampu/mulut wajib kembali seketika — kalau ikut ditunda, mulut Arti
    tampak masih bicara padahal sudah diam."""
    async def jalan():
        vts = FakeVTS()
        await E.apply_turn_end(vts, _cfg(expression_emosi_linger_sec=0.25))
        segera = list(vts.expr)
        await asyncio.sleep(0.4)
        return vts, segera
    vts, segera = asyncio.run(jalan())
    assert vts.state == ["default"], "lampu harus kembali seketika"
    assert segera == [], "mood TIDAK boleh dimatikan di detik yang sama"
    assert set(_mati(vts)) == set(E._ALL_MOOD_FILES), "mood mati sesudah jeda"


def test_giliran_baru_membatalkan_linger_lama():
    """Tanpa pembatalan, pembersih giliran LAMA menghapus mood giliran BARU
    beberapa detik sesudah dipasang — lebih buruk dari penyakit aslinya."""
    async def jalan():
        vts = FakeVTS()
        await E.apply_turn_end(vts, _cfg(expression_emosi_linger_sec=0.25))
        await asyncio.sleep(0.05)
        vts.expr.clear()
        await E.apply_speaking(vts, "sedih", _cfg(expression_emosi_linger_sec=0.25))
        await asyncio.sleep(0.4)
        return vts
    vts = asyncio.run(jalan())
    assert not _mati(vts), f"mood giliran baru terhapus: {vts.expr}"
    assert (E.EMOTION_MAP["sedih"], True) in vts.expr


def test_emotion_disabled_tidak_menjadwalkan_apa_pun():
    async def jalan():
        vts = FakeVTS()
        await E.apply_turn_end(
            vts, _cfg(expression_emotion_enabled=False,
                      expression_emosi_linger_sec=5.0)
        )
        return vts
    vts = asyncio.run(jalan())
    assert vts.expr == []
    E.batalkan_linger()


def test_nilai_rusak_jatuh_ke_perilaku_lama():
    for rusak in ("", None, "dua koma lima", -3):
        async def jalan(v=rusak):
            vts = FakeVTS()
            await E.apply_turn_end(vts, _cfg(expression_emosi_linger_sec=v))
            return vts
        vts = asyncio.run(jalan())
        assert set(_mati(vts)) == set(E._ALL_MOOD_FILES), f"gagal untuk {rusak!r}"


def test_mood_MELELEH_bukan_dipotong():
    """Bohan 27 Agu: "[recorded remark omitted]". Memang — versi
    pertama cuma MENUNDA lalu memotong. VTS punya fadeTime dan kita tidak
    pernah memakainya."""
    async def jalan():
        vts = FakeVTS()
        await E.apply_turn_end(vts, _cfg(expression_emosi_linger_sec=0.2,
                                         expression_emosi_fade_sec=2.0))
        await asyncio.sleep(0.35)
        return vts
    vts = asyncio.run(jalan())
    assert set(_mati(vts)) == set(E._ALL_MOOD_FILES)
    assert all(f == 2.0 for f in vts.fade), f"fade tidak dikirim: {vts.fade}"


def test_fade_dijepit_di_2_detik():
    """VTS mendokumentasikan rentang 0-2 dtk."""
    async def jalan():
        vts = FakeVTS()
        await E.apply_turn_end(vts, _cfg(expression_emosi_fade_sec=99))
        return vts
    vts = asyncio.run(jalan())
    assert all(f == 2.0 for f in vts.fade)


def test_fade_nol_tetap_potong_seketika():
    async def jalan():
        vts = FakeVTS()
        await E.apply_turn_end(vts, _cfg(expression_emosi_fade_sec=0))
        return vts
    vts = asyncio.run(jalan())
    assert vts.fade == [0.0] * len(E._ALL_MOOD_FILES)


def test_fade_rusak_jatuh_ke_nol():
    for rusak in ("", None, "dua"):
        async def jalan(v=rusak):
            vts = FakeVTS()
            await E.apply_turn_end(vts, _cfg(expression_emosi_fade_sec=v))
            return vts
        vts = asyncio.run(jalan())
        assert all(f == 0.0 for f in vts.fade), f"gagal untuk {rusak!r}"


def test_default_source_fade_2_detik():
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert '"expression_emosi_fade_sec": 2.0,' in src
    assert '"fadeTime"' in src, "payload VTS tidak pernah mengirim fadeTime"
