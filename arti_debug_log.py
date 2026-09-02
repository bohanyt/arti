"""Tee terminal->disk untuk proses MANDIRI di luar bridge (19 Agu 2026).

Audit streamer ("loggernya aman ga? otomatis ke disk?"): bridge sudah punya
Tee stdout+stderr sejak import — tapi arti_dubbing dan arti_discord hidup
sebagai proses terpisah TANPA logger: status/error mereka menguap begitu
terminal ditutup (fatal untuk Discord yang kelak 24/7 di VM).

Pola sama dengan Tee bridge (duplikat stdout+stderr, pytest -> no-op
supaya suite tidak menabur berkas sampah), plus rotasi mini per-nama
karena rotasi bridge hanya menyapu *_bridge.log.

Pakai:  arti_debug_log.pasang("dubbing")   # -> session_logs/<ts>_dubbing.log
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "session_logs"


class _Tee:
    def __init__(self, stream, fh):
        self.stream = stream
        self.fh = fh

    def write(self, data):
        self.stream.write(data)
        self.stream.flush()
        try:
            self.fh.write(data)
            self.fh.flush()
        except Exception:  # noqa: BLE001 — log mati bukan alasan proses mati
            pass

    def flush(self):
        self.stream.flush()
        try:
            self.fh.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self):
        return False


def _rotasi(nama: str, keep_n: int) -> None:
    logs = sorted(
        _DIR.glob(f"*_{nama}.log"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for tua in logs[max(1, keep_n):]:
        try:
            tua.unlink()
        except OSError:
            pass


def pasang(nama: str, keep_n: int = 10, force: bool = False) -> str | None:
    """Duplikasi stdout+stderr proses ini ke session_logs/<ts>_<nama>.log.

    Di bawah pytest = no-op (return None) kecuali force=True (untuk tes
    modul ini sendiri). Gagal apa pun -> None, proses jalan tanpa log.
    """
    if not force and ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules):
        return None
    try:
        _DIR.mkdir(exist_ok=True)
        _rotasi(nama, keep_n)
        path = _DIR / f"{time.strftime('%Y-%m-%d_%H%M%S')}_{nama}.log"
        fh = open(path, "w", encoding="utf-8", buffering=1)
        fh.write(f"[{nama} started {time.strftime('%Y-%m-%d %H:%M:%S')}] [PID {os.getpid()}]\n")
        sys.stdout = _Tee(sys.stdout, fh)
        sys.stderr = _Tee(sys.stderr, fh)
        print(f"[DebugLogger] Log {nama} aktif: {path}")
        return str(path)
    except Exception as e:  # noqa: BLE001
        print(f"[DebugLogger] gagal pasang log {nama}: {type(e).__name__}")
        return None
