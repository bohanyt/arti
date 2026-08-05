"""Reconnect koneksi VTS utama — pelajaran live 11,5 jam 2026-08-01.

Koneksi utama putus ~1 jam masuk stream: send_expression menelan semua error,
[Expr] tetap dicetak seolah sukses, dan model nyangkut di overlay 'mikir'
(titik tiga) selama 10 jam. Idle selamat karena punya reconnect sendiri.

Test ini mengunci tiga janji:
1. Kegagalan KIRIM menandai koneksi putus + meninggalkan jejak log (sekali,
   bukan spam) — ACK lambat TIDAK dihitung putus.
2. Reader yang mati tidak lagi diam-diam.
3. Transisi ekspresi mencoba reconnect (throttle), dan saat putus log bilang
   SKIP — bukan berbohong "ketrigger".

Semua pakai websocket palsu — tanpa VTube Studio, tanpa jaringan.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_vtuber_bridge as b


class _DeadWS:
    """Socket yang selalu gagal kirim (koneksi sudah putus)."""

    async def send(self, _msg):
        raise RuntimeError("socket mati")

    async def close(self):
        pass


class _MuteWS:
    """Socket yang menerima kirim tapi tidak pernah membalas (ACK menggantung)."""

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def close(self):
        pass


def _ctl(ws=None):
    """VTSController tanpa __init__ — hindari baca vts_token.txt mesin ini."""
    c = b.VTSController.__new__(b.VTSController)
    c.websocket = ws
    c.auth_token = None
    c._ws_send_lock = asyncio.Lock()
    c._pending = {}
    c._reader_task = None
    c._reader_stop = False
    c._conn_lost = False
    c._last_reconnect_attempt = 0.0
    return c


def _run(coro):
    return asyncio.run(coro)


# --- kegagalan kirim ---------------------------------------------------------


def test_send_failure_marks_conn_lost_and_logs_once(capsys):
    async def scenario():
        c = _ctl(_DeadWS())
        await c.send_expression("ArtiMikir.exp3.json", True)
        await c.send_expression("ArtiBicara.exp3.json", True)

    _run(scenario())
    out = capsys.readouterr().out
    assert out.count("koneksi utama ditandai putus") == 1, (
        "log harus sekali saat transisi sehat→putus, bukan spam per kirim"
    )


def test_send_failure_sets_flag_without_raising():
    async def scenario():
        c = _ctl(_DeadWS())
        await c.send_expression("ArtiMikir.exp3.json", True, confirm=True)
        assert c._conn_lost is True
        assert not c._pending, "future confirm harus dibersihkan walau kirim gagal"

    _run(scenario())


def test_ack_timeout_is_not_conn_lost():
    """VTS lambat balas ≠ koneksi putus — dulu pun cuma di-skip, jangan berubah."""

    async def scenario():
        c = _ctl(_MuteWS())
        await c.send_expression("ArtiBicara.exp3.json", True, confirm=True)
        assert c._conn_lost is False
        assert len(c.websocket.sent) == 1, "payload tetap terkirim"
        assert not c._pending

    _run(scenario())


def test_inject_failure_marks_conn_lost():
    async def scenario():
        c = _ctl(_DeadWS())
        await c.inject_parameter_data([{"id": "FaceAngleY", "value": 1.0}])
        assert c._conn_lost is True

    _run(scenario())


# --- reader mati -------------------------------------------------------------


def test_reader_death_marks_and_logs(capsys):
    class _BrokenRecvWS:
        async def recv(self):
            raise RuntimeError("connection reset")

    async def scenario():
        c = _ctl(_BrokenRecvWS())
        await c._reader_loop()
        assert c._conn_lost is True

    _run(scenario())
    assert "Koneksi utama putus" in capsys.readouterr().out, (
        "reader mati harus meninggalkan jejak — dulu diam 10 jam"
    )


def test_reader_cancelled_is_silent_and_not_lost(capsys):
    class _CancelledRecvWS:
        async def recv(self):
            raise asyncio.CancelledError()

    async def scenario():
        c = _ctl(_CancelledRecvWS())
        await c._reader_loop()
        assert c._conn_lost is False

    _run(scenario())
    assert "Koneksi utama putus" not in capsys.readouterr().out


# --- ensure_connected: throttle + pemulihan ----------------------------------


def test_ensure_connected_healthy_is_noop():
    async def scenario():
        c = _ctl(_MuteWS())
        calls = []

        async def _connect():
            calls.append(1)

        c.connect = _connect
        assert await c.ensure_connected() is True
        assert not calls, "koneksi sehat tidak boleh memicu reconnect"

    _run(scenario())


def test_ensure_connected_reconnects_then_throttles():
    async def scenario():
        c = _ctl(_MuteWS())
        c._conn_lost = True
        calls = []

        async def _connect():
            calls.append(1)
            c._conn_lost = False

        async def _close():
            pass

        c.connect = _connect
        c.close = _close

        assert await c.ensure_connected() is True, "percobaan pertama harus jalan"
        c._conn_lost = True
        assert await c.ensure_connected() is False, "dalam jendela throttle: jangan coba lagi"
        assert len(calls) == 1
        c._last_reconnect_attempt = time.monotonic() - 999
        assert await c.ensure_connected() is True
        assert len(calls) == 2

    _run(scenario())


def test_ensure_connected_failed_reconnect_reports_false():
    async def scenario():
        c = _ctl(_MuteWS())
        c._conn_lost = True

        async def _connect():
            pass  # gagal: flag tetap True (connect asli menelan exception)

        async def _close():
            pass

        c.connect = _connect
        c.close = _close
        assert await c.ensure_connected() is False

    _run(scenario())


# --- trigger_expression_state: jujur saat putus -------------------------------


def test_trigger_state_skips_honestly_when_down(capsys):
    async def scenario():
        c = _ctl(_MuteWS())
        c._conn_lost = True
        c._last_reconnect_attempt = time.monotonic()  # throttle aktif → reconnect skip
        activated = []

        async def _activate(on, *off):
            activated.append(on)

        c._activate_expression = _activate
        await c.trigger_expression_state("mikir")
        assert not activated, "saat putus: jangan pura-pura mengirim"

    _run(scenario())
    out = capsys.readouterr().out
    assert "SKIP" in out, "log harus jujur bilang SKIP — dulu mencetak [Expr] → seolah sukses"
    assert "[Expr] → mikir" not in out


def test_trigger_state_normal_path_unchanged(capsys):
    async def scenario():
        c = _ctl(_MuteWS())
        activated = []

        async def _activate(on, *off):
            activated.append(on)

        c._activate_expression = _activate
        await c.trigger_expression_state("bicara")
        assert activated == [c._EXPR_BICARA]

    _run(scenario())
    assert "[Expr] → bicara" in capsys.readouterr().out
