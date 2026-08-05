"""Profil penonton per-turn — bukan dump statis semua penonton.

Keputusan Bohan 2026-08-01: "ambil soal mereka kalau mereka nanya aja, gausah
penuhin context kalau mereka belum terbukti ada". Dump lama (~230 token, 23 baris)
ikut TIAP turn walau tidak ada penonton — dan bikin system prompt jebol cap sampai
blok instruksi memori terbuang.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_vtuber_bridge as b

import pytest

# ARTI_VIEWERS.md berisi data penonton NYATA dan tidak ikut repo (gitignored).
# Tes membuat berkasnya sendiri di direktori sementara supaya lulus di clone
# yang bersih — dan supaya tidak pernah bergantung pada data pribadi siapa pun.
@pytest.fixture(autouse=True)
def _viewers_file(tmp_path, monkeypatch):
    (tmp_path / "ARTI_VIEWERS.md").write_text(
        "# VIEWER TRACKER\n"
        "\n"
        "### ExampleViewer | Pertemuan: 2026-01-01 | suka ngobrol soal musik\n"
        "\n"
        "### penontonpertama\n"
        "- **Channel:** YouTube\n"
        "- **Pertemuan pertama:** 2026-01-02\n"
        "- **Interaksi:** sering nanya soal teknis\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(b, "_SCRIPT_DIR", str(tmp_path))



def test_viewer_block_empty_when_no_viewer():
    """mic/curious tidak bawa viewer_name -> blok kosong, nol biaya token."""
    assert b.viewer_block_for(None) == ""
    assert b.viewer_block_for("") == ""
    assert b.viewer_block_for("OrangYangTidakDikenal999") == ""


def test_viewer_block_matches_compact_pipe_format():
    """Format `### nama | Pertemuan: ...` — loader lama justru MEN-SKIP baris ini
    karena mengandung '|', jadi 9 penonton hasil pemulihan arsip tidak pernah
    terbawa ke prompt sama sekali."""
    out = b.viewer_block_for("ExampleViewer")
    assert "[VIEWER SAAT INI" in out
    assert "ExampleViewer" in out


def test_viewer_block_matches_full_format_case_insensitive():
    out = b.viewer_block_for("@PENONTONPERTAMA")
    assert "[VIEWER SAAT INI" in out
    assert "penontonpertama" in out.lower()
    # hanya SATU entri — bukan seluruh berkas
    assert out.count("Pertemuan") <= 2, "entri lain ikut terbawa"


def test_startup_prompt_no_longer_dumps_all_viewers():
    """Dump statis [VIEWER YANG DIKETAHUI:] harus hilang dari rakitan startup."""
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    # String literal blok lama tidak boleh lagi dirakit ke dynamic_system_prompt
    assert 'viewer_block = f"\\n\\n[VIEWER YANG DIKETAHUI:]' not in src
    # dan injeksi per-turn harus ada di handler
    assert "viewer_block_for(trigger.viewer_name)" in src


def test_trim_sacrifices_current_viewer_last():
    """Blok [VIEWER SAAT INI muncul hanya saat penontonnya chat — paling relevan
    untuk turn itu, jadi harus jadi korban TERAKHIR saat prompt over cap."""
    assert b._SYSTEM_PROMPT_BLOCK_MARKERS[-1].startswith("\n\n[VIEWER SAAT INI")
