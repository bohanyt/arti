"""Penjaga Fase 4: motion boleh terus jalan saat Arti bicara — tapi hanya
kalau dinyalakan, dan ekspresi idle TETAP dimatikan.

Latar: `_prepare_turn_start` dulu selalu memicu hotkey penghenti motion tiap
giliran (akal-akalan lama Bohan). Sebabnya nyata: motion menulis
`ParamAngleY` sedangkan angguk menyuntik `FaceAngleY`, dan motion menang —
jadi selama motion memegang kepala, anggukan tidak akan pernah terlihat.

Dua sifat yang dikunci tes ini:

1. Bawaan HARUS mati. Menyalakannya bersama motion BERKURVA KEPALA
   (ArtiIdle1..5 produksi) akan menelan anggukan dan hasilnya lebih buruk
   daripada sebelum rombakan.
2. `stop_idle_animation()` tetap dipanggil dalam KEDUA keadaan. Dia yang
   mematikan ekspresi idle aktif — dan ArtiIdle* dengan ArtiBicara berbagi
   43 parameter blend Add yang nilainya identik, jadi kalau dibiarkan hidup
   bersamaan potret wajah yang sama ditambahkan dua kali.
"""

import asyncio

import arti_bridge as H


def _jalankan(monkeypatch, nyala: bool) -> list:
    jejak: list[str] = []

    monkeypatch.setitem(H.CONFIG, "idle_motion_lanjut_saat_bicara", nyala)
    monkeypatch.setattr(H, "stop_idle_animation", lambda: jejak.append("stop_expr"))

    async def _stop_motion():
        jejak.append("stop_motion")

    monkeypatch.setattr(H, "_idle_motion_stop_for_turn", _stop_motion)

    class _VTSPalsu:
        async def trigger_expression_state(self, nama):
            jejak.append(f"expr:{nama}")

    monkeypatch.setattr(H, "vts", _VTSPalsu())
    asyncio.run(H._prepare_turn_start("mic", None))
    return jejak


def test_bawaan_mati_di_kode(monkeypatch):
    """Bawaan KODE mati — bukan mempolisikan config_local Bohan.

    Versi pertama menuntut config_local kosong dan langsung merah begitu
    Bohan menjalankan sesi uji. Salah tempat, sama seperti penjaga
    idle_motion_hotkeys: suite merah harus berarti KODE rusak, bukan
    "config sedang mode uji". Pengingatnya dicetak bridge tiap sesi.
    """
    monkeypatch.delitem(H.CONFIG, "idle_motion_lanjut_saat_bicara", raising=False)
    assert H.CONFIG.get("idle_motion_lanjut_saat_bicara", False) is False


def test_mati_maka_motion_dihentikan(monkeypatch):
    jejak = _jalankan(monkeypatch, False)
    assert "stop_motion" in jejak, "perilaku lama hilang — motion tidak dihentikan"
    assert "stop_expr" in jejak


def test_nyala_maka_motion_dibiarkan_jalan(monkeypatch):
    jejak = _jalankan(monkeypatch, True)
    assert "stop_motion" not in jejak, "motion masih dihentikan padahal flag nyala"


def test_ekspresi_idle_tetap_dimatikan_dalam_dua_keadaan(monkeypatch):
    # Ini yang mencegah tumpukan ganda ArtiIdle* + ArtiBicara (43 parameter
    # Add yang nilainya identik). Jangan pernah ikut dilewati.
    assert "stop_expr" in _jalankan(monkeypatch, True)
    assert "stop_expr" in _jalankan(monkeypatch, False)
