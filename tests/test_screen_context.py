"""Tests for arti_screen_context."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_screen_context as sc


def test_parse_vision_response_json():
    raw = json.dumps(
        {
            "scene": "Pomni berdiri di depan tenda.",
            "playback_mmss": "12:34",
            "ocr_text": "PAUSE",
        }
    )
    snap = sc.parse_vision_response(raw)
    assert snap.scene.startswith("Pomni")
    assert snap.playback_mmss == "12:34"
    assert snap.ocr_text == "PAUSE"


def test_parse_playback_null_string():
    raw = json.dumps({"scene": "Desktop.", "playback_mmss": "null", "ocr_text": ""})
    snap = sc.parse_vision_response(raw)
    assert snap.playback_mmss is None


def test_build_vision_prompt_contract():
    p = sc.build_vision_prompt()
    assert "MAKS 200" in p
    assert "null JSON" in p


def test_screen_ring_max_size():
    ring = sc.ScreenRing(max_size=2)
    for i in range(4):
        ring.push(sc.ScreenSnapshot(wall_ts=float(i), scene=f"s{i}"))
    assert len(ring.snapshot()) == 2
    assert ring.latest().scene == "s3"


def test_build_vision_prompt_mentions_timecode():
    p = sc.build_vision_prompt()
    assert "playback_mmss" in p
    assert "play" in p.lower()


def test_format_screen_context_empty():
    ring = sc.ScreenRing()
    assert sc.format_screen_context(ring) == ""


# --- salvage JSON terpotong (regresi sesi live 2026-08-01) -------------------


def test_parse_vision_salvages_truncated_json():
    """JSON kepotong max_tokens -> field scene tetap terselamatkan.

    Terpantau live: fallback lama menyimpan JSON mentah sebagai scene, yang lalu
    tersuntik ke prompt Arti sebagai [LAYAR: { "scene": ...].
    """
    truncated = '{\n  "scene": "Sedang memantau log error sistem yang menampilkan pesan Cursor', 
    snap = sc.parse_vision_response(truncated[0])
    assert not snap.scene.startswith("{"), "JSON mentah tidak boleh jadi scene"
    assert "memantau log error" in snap.scene


def test_parse_vision_salvages_partial_fields():
    txt = ('{"scene": "Main game di area hutan", "hook": "HP tinggal sedikit", '
           '"playback_mmss": "03:45", "ocr_text": "Boss Fight"')  # tanpa } penutup
    snap = sc.parse_vision_response(txt)
    assert snap.scene == "Main game di area hutan"
    assert snap.hook == "HP tinggal sedikit"
    assert snap.playback_mmss == "03:45"
    assert snap.ocr_text == "Boss Fight"


def test_parse_vision_plain_text_fallback_unchanged():
    """Teks non-JSON biasa tetap jatuh ke fallback lama."""
    snap = sc.parse_vision_response("Layar menampilkan desktop kosong")
    assert snap.scene == "Layar menampilkan desktop kosong"


# --- mata melek saat ditanya soal layar (regresi sesi live 2026-08-01) -------
#
# Bohan tanya "yang lagi ada di layar aku apa" SEBELUM scouter membuka jendela
# vision -> Arti menjawab tanpa data dan mengarang. Sekarang keyword layar di
# turn itu sendiri membuka jendelanya, tanpa menunggu timer scouter.


def _bridge():
    import hermes_vtuber_bridge as b

    return b


def test_screen_question_opens_vision_window(monkeypatch):
    b = _bridge()
    import time as _t

    monkeypatch.setitem(b.CONFIG, "vision_enabled", True)
    monkeypatch.setitem(b.CONFIG, "vision_runtime_on", False)
    monkeypatch.setitem(b.CONFIG, "vision_auto_until", 0.0)
    monkeypatch.setattr(b, "vision_auto_until", 0.0)
    called = []
    monkeypatch.setattr(
        b.arti_vision_client, "refresh_if_stale", lambda cfg: (called.append(1), (None, "x"))[1]
    )
    b.refresh_vision_for_turn("eh arti yang lagi ada di layar aku apa")
    assert b.vision_auto_until > _t.time(), "jendela vision harus terbuka"
    assert called, "screenshot harus langsung diambil untuk turn ini"


def test_non_screen_question_keeps_vision_closed(monkeypatch):
    b = _bridge()

    monkeypatch.setitem(b.CONFIG, "vision_enabled", True)
    monkeypatch.setitem(b.CONFIG, "vision_runtime_on", False)
    monkeypatch.setitem(b.CONFIG, "vision_auto_until", 0.0)
    monkeypatch.setattr(b, "vision_auto_until", 0.0)
    called = []
    monkeypatch.setattr(
        b.arti_vision_client, "refresh_if_stale", lambda cfg: (called.append(1), (None, "x"))[1]
    )
    b.refresh_vision_for_turn("pagi arti kamu tau jam berapa")
    assert b.vision_auto_until == 0.0, "pertanyaan biasa tidak boleh membuka mata"
    assert not called


def test_master_switch_off_never_opens(monkeypatch):
    b = _bridge()

    monkeypatch.setitem(b.CONFIG, "vision_enabled", False)
    monkeypatch.setitem(b.CONFIG, "screen_context_enabled", False)
    monkeypatch.setitem(b.CONFIG, "vision_auto_until", 0.0)
    monkeypatch.setattr(b, "vision_auto_until", 0.0)
    b.refresh_vision_for_turn("eh arti liat layar dong")
    assert b.vision_auto_until == 0.0, "master switch OFF harus menang atas keyword"


# --- backstage: log bridge sendiri bukan bahan omongan (live 2026-08-02 sore) ----
# Vision membaca terminal bridge di layar -> Arti narasi dapurnya sendiri
# ("cursor screen relevant False", "aku membaca 50 pesan sejarah stream").


def test_looks_like_bridge_log_positives():
    for t in (
        "[Scouter] OK cursor (4859ms) screen_relevant=True",
        "Terminal menampilkan cursor screen relevant False saat Scouter jalan",
        "Mengirim ke Groq dengan 50 pesan sejarah stream",
        "log: Arti menjawab: 'halo semua'",
        "History Recorded [15:22:28] Viewer @bohanyt",
    ):
        assert sc.looks_like_bridge_log(t), t


def test_looks_like_bridge_log_negatives_konten_sah():
    # Bohan ngoding ARTI di layar = konten sah — jangan kena blokir.
    for t in (
        "",
        "Bohan lagi ngedit arti_curious.py di VSCode, fungsi should_fire",
        "Tutorial Unsloth fine-tune LLM hemat memori 70 persen",
        "Zelda: The Great Sea, klaim hak cipta di YouTube Studio",
        "pytest 3 test gagal di test_video_watcher.py",
    ):
        assert not sc.looks_like_bridge_log(t), t


def test_parse_vision_response_scrubs_bridge_log_snapshot():
    raw = json.dumps(
        {
            "scene": "Terminal PowerShell menampilkan [Scouter] OK cursor screen_relevant=True",
            "hook": "kenapa screen relevant False terus ya?",
            "playback_mmss": None,
            "ocr_text": "[Vault RAG] Lookup (8s max)",
        }
    )
    snap = sc.parse_vision_response(raw)
    assert snap.scene == "" and snap.hook == "" and snap.ocr_text == ""


def test_parse_vision_response_scrub_partial_field_blanks_all():
    # scene sah tapi ocr mengutip log bridge -> seluruh snapshot teks kosong
    # (vision lagi menatap dapur; lebih baik diam daripada bocor).
    raw = json.dumps(
        {
            "scene": "OBS scene game dengan overlay chat",
            "hook": None,
            "playback_mmss": None,
            "ocr_text": "Arti menjawab: iya bohan sabar",
        }
    )
    snap = sc.parse_vision_response(raw)
    assert snap.scene == "" and snap.ocr_text == ""


def test_vision_prompt_declares_backstage_rule():
    p = sc.build_vision_prompt()
    assert "BACKSTAGE" in p


def test_parse_vision_response_scrubs_own_prompt_echo():
    """Live pagi 2026-08-03: google_gemma mengembalikan prompt-nya sendiri dan
    fallback parse menyimpannya sebagai scene ("VTuber AI companion. Analyze
    a screenshot of a str...") — junk begitu tidak boleh masuk konteks."""
    snap = sc.parse_vision_response(
        "VTuber AI companion.\nAnalyze a screenshot of a streamer's screen..."
    )
    assert snap.scene == "" and snap.hook == ""
