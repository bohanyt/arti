"""Catch-up reindex saat startup — jaring pengaman reindex shutdown yang terpotong.

Insiden 2026-08-01: reindex shutdown jalan di thread DAEMON; user menutup
terminal ("Terminate batch job") → thread dibunuh di tengah embedding →
DB berhenti di 624 dari 747 chunk, dan baru ketahuan saat crosscheck manual.
Pesan lama "Reindex lanjut di background — Ctrl+C tidak perlu tunggu" BOHONG:
daemon thread mati bersama proses.

Dua janji yang dikunci di sini:
1. Bridge start menjalankan catch-up reindex (incremental, murah saat sinkron)
   sehingga sisa reindex yang terpotong sembuh sendiri.
2. Pesan shutdown jujur soal nasib thread daemon.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_vault_rag as vr


def _stats(**over):
    base = {"files": 0, "chunks_new": 0, "chunks_skipped": 0,
            "chunks_removed": 0, "chunks_junk": 0, "errors": 0}
    return {**base, **over}


def test_catchup_reports_leftover_work(monkeypatch, capsys):
    monkeypatch.setattr(vr, "reindex_all", lambda cfg, **k: _stats(chunks_new=123))
    vr.reindex_startup_catchup({})
    out = capsys.readouterr().out
    assert "Catch-up" in out and "123" in out, "sisa reindex harus dilaporkan eksplisit"


def test_catchup_quiet_single_line_when_synced(monkeypatch, capsys):
    monkeypatch.setattr(vr, "reindex_all", lambda cfg, **k: _stats())
    vr.reindex_startup_catchup({})
    out = capsys.readouterr().out
    assert "sinkron" in out.lower()
    assert len(out.strip().splitlines()) == 1, "saat sinkron: satu baris, jangan berisik"


def test_catchup_never_raises(monkeypatch, capsys):
    """LM Studio mati saat startup = normal — catch-up tidak boleh merobohkan bridge."""

    def _boom(cfg, **k):
        raise RuntimeError("LM Studio tidak menjawab")

    monkeypatch.setattr(vr, "reindex_all", _boom)
    vr.reindex_startup_catchup({})  # tidak boleh raise
    out = capsys.readouterr().out
    assert "manual" in out.lower(), "kalau gagal, kasih tahu cara manualnya"


def test_bridge_wires_catchup_and_honest_shutdown_message():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"vault_rag_reindex_on_startup": True,' in src, "kill switch harus ada dan default ON"
    assert "reindex_startup_catchup" in src, "main_loop harus memanggil catch-up"
    assert "Ctrl+C tidak perlu tunggu" not in src, (
        "pesan lama menyesatkan — daemon thread MATI saat proses exit"
    )
    assert "start berikutnya" in src, (
        "pesan shutdown harus menjelaskan bahwa sisa reindex dilanjutkan saat start berikutnya"
    )


# --- shutdown menunggu tuntas + progress embedding (Bohan 2026-08-02) ----------


def test_iter_embed_batches_covers_all_items_in_order():
    items = list(range(75))
    batches = list(vr._iter_embed_batches(items, 32))
    assert [len(b) for b in batches] == [32, 32, 11]
    assert [x for b in batches for x in b] == items
    assert list(vr._iter_embed_batches([], 32)) == []
    assert [len(b) for b in vr._iter_embed_batches([1, 2], 0)] == [1, 1], (
        "batch_size tidak valid tidak boleh membuat loop tak berhingga"
    )


def test_reindex_all_accepts_progress_callback():
    import inspect

    assert "progress" in inspect.signature(vr.reindex_all).parameters


def test_shutdown_waits_until_done_by_default():
    """'Terminate batch job (Y/N)?' tidak boleh muncul saat masih ada kerja bisu.

    Default timeout = 0 (tunggu tuntas), ada heartbeat saat masih jalan, dan
    banner 'aman menutup terminal' HANYA setelah semuanya selesai.
    """
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"vault_rag_reindex_shutdown_timeout_sec": 0,' in src, (
        "default harus 0 = tunggu sampai tuntas"
    )
    assert "while t.is_alive():" in src, "harus ada loop tunggu-tuntas"
    assert "masih embedding" in src, "heartbeat supaya user tahu masih ada kerja"
    assert "aman menutup terminal" in src, "banner all-clear di akhir shutdown"
    assert "JANGAN tutup terminal dulu" in src
