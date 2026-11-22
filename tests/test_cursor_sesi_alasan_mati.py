"""Kenapa sesi Cursor mati harus KELIHATAN di log.

Tanpa log alasan, sebuah sesi bisa sering jatuh ke Groq dan memanaskan ulang
sesi berkali-kali, sehingga suara yang didengar penonton BUKAN composer-2.5
seperti yang dipilih — dan tidak ada satu baris pun
di log yang menyebut sebabnya, karena `should_recycle()` menghitung alasannya
lalu membuangnya dan `mark_dirty()` menyimpan alasannya tanpa mencetak.
"""

from pathlib import Path

import arti_cursor_agent

_SRC = (
    Path(__file__).resolve().parent.parent / "arti_cursor_agent.py"
).read_text(encoding="utf-8")


def test_alasan_daur_ulang_dicetak_bukan_dibuang():
    assert 'print(f"[Cursor] daur ulang sesi {self.role}: {why}")' in _SRC


def test_mark_dirty_mencetak_alasan(capsys):
    sesi = object.__new__(arti_cursor_agent.CursorSession)
    sesi.role = "voice"

    sesi.mark_dirty("timeout 12s")

    keluaran = capsys.readouterr().out
    assert "voice" in keluaran
    assert "timeout 12s" in keluaran
    assert sesi.dirty is True
    assert sesi.dirty_reason == "timeout 12s"


def test_mark_dirty_tidak_meledak_tanpa_role():
    """Dipanggil dari jalur error; dia sendiri tidak boleh jadi sumber error."""
    sesi = object.__new__(arti_cursor_agent.CursorSession)

    sesi.mark_dirty("ensure gagal: RuntimeError")

    assert sesi.dirty is True
