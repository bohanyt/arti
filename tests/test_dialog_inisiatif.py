"""Dua akar keluhan: inisiatif terlalu sering, dan sapaan ke penonton saat offline.

Gejala: kebanyakan giliran jadi inisiatif walau streamer bicara, dan balasan
menyapa "Penonton" padahal siaran OFFLINE.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as ac
import arti_bridge as bridge


# --------------------------------------------------------------------------
# 1. Pemilik bukan penonton
# --------------------------------------------------------------------------
def _reset_penonton(monkeypatch, count=-1):
    monkeypatch.setattr(bridge, "_yt_viewer_count", count)
    bridge._yt_viewers_seen.clear()


def test_chat_dari_pemilik_bukan_penonton(monkeypatch):
    """Streamer yang menguji streamnya sendiri bukan audiens.

    Pesan tes dari akun streamer tidak boleh membuat register
    SENDIRIAN tidak pernah menyala.
    """
    _reset_penonton(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "owner_yt_handles", ["bohanyt"])
    bridge._yt_viewers_seen["@bohanyt"] = time.time()
    assert bridge._ada_penonton() is False, "pemilik terhitung sebagai penonton"


def test_handle_pemilik_dinormalisasi(monkeypatch):
    """'@bohanyt', 'bohanyt', 'BohanYT' harus sama-sama dikenali."""
    _reset_penonton(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "owner_yt_handles", ["@BohanYT"])
    for varian in ("bohanyt", "@bohanyt", "BohanYT"):
        bridge._yt_viewers_seen.clear()
        bridge._yt_viewers_seen[varian] = time.time()
        assert bridge._ada_penonton() is False, f"varian {varian!r} lolos"


def test_penonton_asli_tetap_terhitung(monkeypatch):
    """KONTROL: perbaikan tidak boleh membuat semua penonton hilang."""
    _reset_penonton(monkeypatch)
    monkeypatch.setitem(bridge.CONFIG, "owner_yt_handles", ["bohanyt"])
    bridge._yt_viewers_seen["@bohanyt"] = time.time()
    bridge._yt_viewers_seen["@viewer_a"] = time.time()
    assert bridge._ada_penonton() is True, "penonton asli ikut terbuang"


def test_jumlah_viewer_live_menang(monkeypatch):
    _reset_penonton(monkeypatch, count=5)
    monkeypatch.setitem(bridge.CONFIG, "owner_yt_handles", ["bohanyt"])
    assert bridge._ada_penonton() is True


# --------------------------------------------------------------------------
# 2. Prompt inisiatif jujur terhadap keadaan
# --------------------------------------------------------------------------
def test_prompt_tidak_bohong_saat_streamer_baru_bicara():
    """Pagar anti-motong cuma 5 dtk, jadi Bohan yang baru selesai bicara tetap
    dapat giliran inisiatif. Dulu promptnya bilang "[recorded remark omitted]" — Arti melihat omongannya tapi dilarang
    menjawabnya."""
    p = ac.build_initiative_prompt(
        bridge.CONFIG, streamer_baru_bicara=True, ada_penonton=True, vault_topic="tes"
    )
    assert "nyambung obrolan" in p
    assert "BARU SAJA bicara" in p
    assert "bukan menjawab siapa pun" not in p, "masih melarang Arti menjawab"


def test_prompt_buka_topik_saat_benar_benar_hening():
    p = ac.build_initiative_prompt(
        bridge.CONFIG, streamer_baru_bicara=False, ada_penonton=True, vault_topic="tes"
    )
    assert "buka topik sendiri" in p
    assert "bukan menjawab siapa pun" in p


def test_tanpa_penonton_dilarang_menyapa_penonton():
    p = ac.build_initiative_prompt(
        bridge.CONFIG, streamer_baru_bicara=False, ada_penonton=False, vault_topic="tes"
    )
    assert "TIDAK ADA penonton" in p
    assert "lempar ke penonton" not in p, "masih menyuruh melempar ke ruangan kosong"


def test_ada_penonton_boleh_menyapa_penonton():
    p = ac.build_initiative_prompt(
        bridge.CONFIG, streamer_baru_bicara=False, ada_penonton=True, vault_topic="tes"
    )
    assert "lempar ke penonton" in p


def test_default_tidak_mengubah_perilaku_lama():
    """Tanpa argumen baru, promptnya harus persis mode lama (buka topik)."""
    p = ac.build_initiative_prompt(bridge.CONFIG, vault_topic="tes")
    assert "buka topik sendiri" in p
    assert "lempar ke penonton" in p
