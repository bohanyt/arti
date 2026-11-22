"""Gerakan dialog: giliran curious tidak boleh jatuh ke satu template reporter.

Kalau prompt cuma mengenal dua gerakan, giliran curious jatuh ke satu template
reporter: pembuka kaleng yang sama dan laporan keadaan, opini eksplisit jarang.
Tujuannya Arti BERDIALOG.

Obatnya dua lapis: (1) satu "[recorded remark omitted]" dirotasi per giliran (ledek,
bantah, callback, cerita, timpali, spekulasi, tagih) supaya bentuk omongannya
berganti-ganti; (2) instruksi anti-reporter eksplisit di prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as ac
import arti_bridge as bridge


def _reset():
    ac._gerakan_sisa.clear()


# --------------------------------------------------------------------------
# Rotasi
# --------------------------------------------------------------------------

def test_satu_siklus_memakai_semua_gerakan_tanpa_ulang():
    _reset()
    n = len(ac.DEFAULT_GERAKAN_DIALOG)
    hasil = [ac.gerakan_dialog(rng=lambda: 0.5) for _ in range(n)]
    assert len(set(hasil)) == n, "ada gerakan terulang sebelum siklus habis"


def test_siklus_berikutnya_mulai_lagi():
    _reset()
    n = len(ac.DEFAULT_GERAKAN_DIALOG)
    for _ in range(n):
        ac.gerakan_dialog(rng=lambda: 0.5)
    assert ac.gerakan_dialog(rng=lambda: 0.5)  # tidak meledak, siklus baru


def test_config_produksi_dengan_kunci_none_tetap_pakai_default():
    """CONFIG produksi menaruh curious_gerakan_dialog=None ('pakai default').
    .get(key, default) TIDAK menolong karena kuncinya ADA — aturan proyek no. 7."""
    _reset()
    assert ac.gerakan_dialog(rng=lambda: 0.5, config={"curious_gerakan_dialog": None})


def test_daftar_kosong_mematikan_fitur():
    _reset()
    assert ac.gerakan_dialog(config={"curious_gerakan_dialog": []}) == ""


def test_gerakan_bisa_ditimpa_dari_config():
    _reset()
    hasil = ac.gerakan_dialog(config={"curious_gerakan_dialog": ["Nyanyi dangdut."]})
    assert hasil == "Nyanyi dangdut."


def test_larangan_bisa_ditimpa_dan_dikosongkan():
    kustom = ac.build_curious_system_addon({"curious_larangan": "Jangan bahas politik."})
    assert "Jangan bahas politik." in kustom
    assert "merangkum aktivitas streamer" not in kustom
    kosong = ac.build_curious_system_addon({"curious_larangan": ""})
    assert "merangkum aktivitas streamer" not in kosong
    assert "None" not in kosong


def test_suntikan_gerakan_membawa_pintu_keluar_momen():
    """Jangan sampai gerakan/bits merebut giliran
    saat ada kejadian di stream. Tiap suntikan wajib bilang momen menang."""
    _reset()
    p = ac.build_initiative_prompt(
        bridge.CONFIG, streamer_baru_bicara=False, ada_penonton=True, vault_topic="tes"
    )
    assert "momen selalu menang" in p


def test_tanpa_penonton_tidak_pernah_menyuruh_timpali_chat():
    """'Timpali chat' ke ruangan kosong = mengoceh ke hantu — bug yang sudah
    sekali dibayar. Jangan lewat pintu lain."""
    _reset()
    for _ in range(len(ac.DEFAULT_GERAKAN_DIALOG) * 3):
        instruksi = ac.gerakan_dialog(rng=lambda: 0.5, ada_penonton=False)
        assert "chat" not in instruksi.lower()
        assert "penonton" not in instruksi.lower()


def test_reset_session_mengosongkan_rotasi():
    _reset()
    ac.gerakan_dialog()
    ac.reset_session()
    assert ac._gerakan_sisa == []


# --------------------------------------------------------------------------
# Injeksi ke prompt
# --------------------------------------------------------------------------

def test_prompt_inisiatif_membawa_gerakan():
    _reset()
    p = ac.build_initiative_prompt(
        bridge.CONFIG, streamer_baru_bicara=False, ada_penonton=True, vault_topic="tes"
    )
    assert "Gerakan giliran ini" in p


def test_prompt_reaksi_membawa_gerakan_dan_anti_reporter(monkeypatch):
    _reset()

    class _Snap:
        scene = "Bohan lagi menata kebun"
        playback_mmss = None

    monkeypatch.setattr(ac.sc.screen_ring, "latest", lambda: _Snap())
    p = ac.build_prompt(dict(bridge.CONFIG, scouter_last_result={}))
    assert "Gerakan giliran ini" in p
    assert "momen selalu menang" in p


def test_system_addon_melarang_pola_reporter():
    addon = ac.build_curious_system_addon(bridge.CONFIG)
    assert "merangkum aktivitas streamer" in addon
    assert "nama streamer" in addon
    assert "deg-degan" in addon  # larangan kata emosi kaleng disebut eksplisit


# --------------------------------------------------------------------------
# Plafon prompt sistem ikut naik (jiwa gemuk tidak boleh bikin memori terpangkas)
# --------------------------------------------------------------------------

def test_plafon_prompt_muat_jiwa_baru():
    """ARTI_SOUL.md digemukkan ~3 KB (16 Agu). Plafon lama 6500 membuat trim
    membuang blok memori jangka panjang — penalti yang tidak proporsional."""
    assert int(bridge.CONFIG["llm_system_prompt_max_chars"]) >= 9500
