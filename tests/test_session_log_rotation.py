"""Rotasi session log yang sadar-ukuran + pytest tidak meninggalkan sampah.

Diagnosa 12 Agu 2026: rotasi lama menghitung jatah dari SEMUA berkas, padahal
tiap bridge gagal-start / harness / impor pytest melahirkan log ~293 byte.
Log siaran 194 KB tersapu ke arsip dalam hitungan menit, Bohan membuka
session_logs/ dan cuma menemukan cangkang kosong — lalu berhenti percaya pada
log otomatis dan menyalin terminal ke notepad manual berbulan-bulan.
"""

import os

import pytest

import session_transcript


def _tulis(path, nama, ukuran, mtime):
    p = path / nama
    p.write_bytes(b"x" * ukuran)
    os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def akar(tmp_path, monkeypatch):
    (tmp_path / "session_logs").mkdir()
    monkeypatch.setattr(session_transcript, "_ROOT", tmp_path, raising=True)
    return tmp_path


def _sisa(akar):
    return sorted(p.name for p in (akar / "session_logs").glob("*_bridge.log"))


def _arsip(akar):
    d = akar / "archive" / "v0.4" / "session_logs"
    return sorted(p.name for p in d.glob("*_bridge.log")) if d.is_dir() else []


def test_log_remeh_tidak_memakan_jatah_log_siaran(akar):
    logs = akar / "session_logs"
    # Log siaran sungguhan, PALING TUA.
    _tulis(logs, "2026-08-11_221019_bridge.log", 194_301, 1_000)
    # Lima cangkang bridge gagal-start, semuanya lebih baru.
    for i in range(5):
        _tulis(logs, f"2026-08-12_20340{i}_bridge.log", 293, 2_000 + i)

    session_transcript.rotate_session_logs({"session_log_keep_n": 5})

    # Dengan rotasi lama, log 194 KB itu yang tersingkir (paling tua, jatah 5
    # habis dipakai cangkang). Sekarang justru cangkangnya yang pergi.
    assert _sisa(akar) == ["2026-08-11_221019_bridge.log"]
    assert len(_arsip(akar)) == 5


def test_log_substansial_berlebih_tetap_diarsipkan(akar):
    logs = akar / "session_logs"
    for i in range(4):
        _tulis(logs, f"2026-08-1{i}_120000_bridge.log", 100_000, 1_000 + i)

    session_transcript.rotate_session_logs({"session_log_keep_n": 2})

    assert _sisa(akar) == [
        "2026-08-12_120000_bridge.log",
        "2026-08-13_120000_bridge.log",
    ]
    assert len(_arsip(akar)) == 2


def test_tidak_ada_yang_dihapus_hanya_dipindah(akar):
    logs = akar / "session_logs"
    _tulis(logs, "2026-08-12_000000_bridge.log", 293, 1_000)

    session_transcript.rotate_session_logs({"session_log_keep_n": 5})

    assert _sisa(akar) == []
    assert _arsip(akar) == ["2026-08-12_000000_bridge.log"]


def test_bridge_di_bawah_pytest_tidak_menulis_berkas_log():
    """Impor bridge saat pytest TIDAK boleh melahirkan berkas di session_logs/."""
    import arti_bridge as hb

    assert os.path.basename(hb._log_fh.name).lower() in ("nul", "null")
