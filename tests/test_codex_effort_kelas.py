"""Effort Luna mengikuti KELAS jawaban (usul Bohan 27 Agu).

Angka yang membenarkan pemetaan ini ada di docstring
`_codex_cfg_untuk_kelas`; yang dijaga di sini cuma perilakunya.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_reply_policy as P  # noqa: E402
import arti_bridge as b  # noqa: E402


def _cfg(**over):
    cfg = {
        "codex_effort": "low",
        "codex_effort_berat": "high",
        "codex_effort_kelas_berat": ["deep", "rant"],
        "arti_reply_streamer_adaptive": True,
    }
    cfg.update(over)
    return cfg


def test_kelas_dari_mode_semua_bentuk():
    assert P.kelas_dari_mode("deep\u21923kal") == "deep"
    assert P.kelas_dari_mode("streamer-deep\u21923kal") == "deep"
    assert P.kelas_dari_mode("rant\u21927kal") == "rant"
    assert P.kelas_dari_mode("gacha\u21922kal") == "gacha"
    assert P.kelas_dari_mode("inisiatif\u21921kal") == "inisiatif"
    assert P.kelas_dari_mode("streamer-fixed") == "fixed"
    assert P.kelas_dari_mode("") == ""


def test_default_source_low_dan_high_bukan_xhigh():
    """xhigh dilarang jadi bawaan: ekornya 26 dtk lawan pagar 12 dtk."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert '"codex_effort": "low",' in src
    assert '"codex_effort_berat": "high",' in src
    assert '"codex_effort_kelas_berat": ["deep", "rant"],' in src


def test_kelas_ringan_tidak_menaikkan_effort():
    cfg = _cfg()
    ucap = '[YouTube] Seka: wkwk'
    assert b._codex_cfg_untuk_kelas(ucap, cfg) is cfg


def test_kelas_berat_menaikkan_effort():
    cfg = _cfg()
    panjang = (
        "arti menurut kamu kenapa sih orang betah nonton streamer padahal "
        "bisa main sendiri? aku beneran penasaran soalnya"
    )
    ucap = f"[YouTube] ViewerG: {panjang}"
    kelas = P.kelas_dari_mode(P.resolve_reply_plan(ucap, cfg, quiet=True).mode)
    hasil = b._codex_cfg_untuk_kelas(ucap, cfg)
    if kelas in ("deep", "rant"):
        assert hasil["codex_effort"] == "high"
        assert cfg["codex_effort"] == "low", "cfg asal tidak boleh ikut berubah"
    else:  # klasifikasi berubah -> tes ini tidak boleh diam-diam hijau
        assert hasil is cfg, f"kelas {kelas} seharusnya ringan"


def test_daftar_kelas_berat_kosong_mematikan_fitur():
    cfg = _cfg(codex_effort_kelas_berat=[])
    ucap = "[YouTube] ViewerG: " + ("kenapa sih orang betah nonton streamer " * 3)
    assert b._codex_cfg_untuk_kelas(ucap, cfg) is cfg


def test_effort_sama_tidak_menyalin_cfg():
    cfg = _cfg(codex_effort="high")
    ucap = "[YouTube] ViewerG: " + ("kenapa sih orang betah nonton streamer " * 3)
    assert b._codex_cfg_untuk_kelas(ucap, cfg) is cfg


def test_penilai_rusak_tidak_menjatuhkan_giliran(monkeypatch):
    """Menilai kelas tidak pernah boleh membuat giliran suara gagal."""
    monkeypatch.setattr(
        P, "resolve_reply_plan",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("meledak")),
    )
    cfg = _cfg()
    assert b._codex_cfg_untuk_kelas("apa pun", cfg) is cfg


def test_tidak_mencetak_apa_pun_saat_menaikkan_effort(capsys):
    """Sesi live 27 Agu: 6 baris "[recorded remark omitted]" tercetak dan
    NOL di antaranya benar-benar memakai Luna (agy yang menjawab semuanya).
    Log yang menipu pembaca berikutnya lebih buruk daripada log yang sepi —
    effort aslinya sudah tercetak oleh Luna sendiri saat dia dipakai."""
    cfg = _cfg()
    ucap = "[YouTube] ViewerG: " + ("kenapa sih orang betah nonton streamer " * 3)
    capsys.readouterr()
    b._codex_cfg_untuk_kelas(ucap, cfg)
    assert capsys.readouterr().out == ""
