"""Penjaga: track MOTION dan track EKSPRESI diurus terpisah.

Permintaan Bohan 26 Agu 2026: "[recorded remark omitted]".

Sebelumnya keduanya digerbangi satu bendera `idle_timer_running`, jadi begitu
giliran mulai KEDUANYA mati. `idle_motion_lanjut_saat_bicara` cuma melewati
hotkey penghenti — motion yang SEDANG main boleh habis (10 dtk), tapi tidak
ada motion baru sepanjang sisa giliran. Giliran p50 sesi 26 Agu 29,7 detik,
jadi Arti tetap membeku dua pertiga waktu bicaranya.

Yang TIDAK boleh ikut berubah: track EKSPRESI harus tetap mati saat bicara.
ArtiIdle* dan ArtiBicara sama-sama blend Add dan berbagi 43 parameter
bernilai identik — hidup bersamaan berarti potret wajah yang sama
ditambahkan dua kali.
"""

import asyncio
import inspect

import arti_bridge as H


def _sup() -> str:
    return inspect.getsource(H._idle_worker_main)


def test_dua_track_punya_task_sendiri():
    src = _sup()
    assert "motion_task" in src and "expr_task" in src, (
        "supervisor masih mengurus kedua track sebagai satu daftar"
    )


def test_motion_boleh_hidup_saat_giliran():
    src = _sup()
    assert "_motion_run_state()" in src, (
        "supervisor tidak memakai gerbang lifecycle motion bersama"
    )


def test_ekspresi_TETAP_digerbangi_bendera_lama():
    """Ini pagar anti tumpuk-ganda; jangan pernah ikut dilonggarkan."""
    src = _sup()
    potong = src.split("track EKSPRESI", 1)
    assert len(potong) == 2, "blok track ekspresi tidak ketemu"
    blok = potong[1][:400]
    assert "idle_timer_running" in blok
    assert "idle_motion_lanjut_saat_bicara" not in blok, (
        "track ekspresi ikut dibiarkan hidup saat bicara — itu membuat "
        "ArtiIdle* dan ArtiBicara menumpuk 43 parameter yang sama"
    )


def test_izin_motion_membedakan_idle_turn_dan_stop(monkeypatch):
    monkeypatch.setattr(H, "idle_timer_running", True)
    monkeypatch.setattr(H, "_brain_busy", False)
    monkeypatch.setitem(H.CONFIG, "idle_motion_lanjut_saat_bicara", False)
    assert H._motion_run_state() == "idle"

    monkeypatch.setattr(H, "idle_timer_running", False)
    monkeypatch.setattr(H, "_brain_busy", True)
    monkeypatch.setitem(H.CONFIG, "idle_motion_lanjut_saat_bicara", True)
    assert H._motion_run_state() == "turn"

    # PTT-menunggu, turn selesai, dan shutdown semuanya terlihat sebagai
    # idle mati + brain tidak busy: tidak boleh ada hotkey baru.
    monkeypatch.setattr(H, "_brain_busy", False)
    assert H._motion_run_state() is None

    monkeypatch.setattr(H, "_brain_busy", True)
    monkeypatch.setitem(H.CONFIG, "idle_motion_lanjut_saat_bicara", False)
    assert H._motion_run_state() is None


def test_motion_benar_benar_terkirim_saat_brain_busy(monkeypatch, capsys):
    async def _run():
        terkirim = asyncio.Event()
        payloads = []

        monkeypatch.setattr(H, "idle_timer_running", False)
        monkeypatch.setattr(H, "_brain_busy", True)
        monkeypatch.setitem(H.CONFIG, "idle_motion_lanjut_saat_bicara", True)
        monkeypatch.setitem(H.CONFIG, "idle_motion_sambung", True)

        async def _send(_ws, payload):
            payloads.append(payload)
            terkirim.set()
            return {"messageType": "HotkeyTriggerResponse"}

        monkeypatch.setattr(H, "_idle_send", _send)
        task = asyncio.create_task(H._motion_track({"UjiTanpaAngguk": "hk-1"}))
        await asyncio.wait_for(terkirim.wait(), timeout=0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return payloads

    payloads = asyncio.run(_run())
    assert payloads[0]["data"]["hotkeyID"] == "hk-1"
    assert "state=turn" in capsys.readouterr().out


def test_motion_dibatalkan_bila_turn_berakhir_tepat_sebelum_kirim(monkeypatch):
    async def _run():
        terkirim = []
        monkeypatch.setattr(H, "idle_timer_running", False)
        monkeypatch.setattr(H, "_brain_busy", True)
        monkeypatch.setitem(H.CONFIG, "idle_motion_lanjut_saat_bicara", True)
        monkeypatch.setitem(H.CONFIG, "idle_motion_sambung", True)

        def _choice(names):
            # Meniru turn selesai di celah sesudah interval, sebelum send.
            monkeypatch.setattr(H, "_brain_busy", False)
            return names[0]

        async def _send(_ws, payload):
            terkirim.append(payload)
            return {"messageType": "HotkeyTriggerResponse"}

        monkeypatch.setattr(H.random, "choice", _choice)
        monkeypatch.setattr(H, "_idle_send", _send)
        await asyncio.wait_for(
            H._motion_track({"UjiTanpaAngguk": "hk-1"}), timeout=0.1
        )
        return terkirim

    assert asyncio.run(_run()) == []


def test_reset_sudut_wajah_tidak_melawan_motion():
    """Reset FaceAngle hanya kalau motion memang ikut berhenti.

    Kalau motion masih jalan, reset itu menarik kepala kembali ke nol sambil
    kurva motion menggerakkannya — dua penulis berkelahi di sendi yang sama.
    """
    src = _sup()
    i = src.find("_idle_reset_face_angles")
    assert i > 0, "reset sudut wajah hilang dari supervisor"
    sekitar = src[max(0, i - 300):i]
    assert "_motion_run_state" in sekitar, (
        "reset sudut wajah tidak lagi dipagari keadaan motion"
    )
