"""Telemetri jumlah penonton YouTube (spek Bohan 2026-08-03):
naik = trigger/bangunkan Arti; turun + 5 menit sunyi = proaktif off."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_yt_viewers as yv


def _resp(view_count_obj, *, is_live=True, **extra):
    renderer = {"viewCount": view_count_obj, **extra}
    if is_live:
        renderer["isLive"] = True
    return {"actions": [{"updateViewershipAction": {"viewCount": {
        "videoViewCountRenderer": renderer
    }}}]}


def test_parse_viewer_count_runs_format():
    data = _resp({"runs": [{"text": "1,234"}, {"text": " watching now"}]})
    assert yv.parse_viewer_count(data) == 1234


def test_parse_viewer_count_simpletext_locale():
    assert yv.parse_viewer_count(_resp({"simpleText": "2.345 orang menonton"})) == 2345
    assert yv.parse_viewer_count(_resp({"simpleText": "3 watching now"})) == 3


def test_parse_viewer_count_no_digits_means_zero():
    data = _resp({"runs": [{"text": "No one is watching"}]})
    assert yv.parse_viewer_count(data) == 0


def test_parse_viewer_count_absent_is_none():
    assert yv.parse_viewer_count({}) is None
    assert yv.parse_viewer_count(None) is None
    assert yv.parse_viewer_count({"actions": [{"other": {}}]}) is None


def test_parse_viewer_count_not_live_is_none():
    """Video sudah TIDAK live tetap dijawab endpoint, tapi angkanya = total
    views KUMULATIF (verifikasi riset 2026-08-03). Tanpa gerbang isLive,
    stream berakhir terbaca 'penonton melonjak ribuan' -> sapaan hantu."""
    data = _resp({"simpleText": "152,340 views"}, is_live=False)
    assert yv.parse_viewer_count(data) is None


def test_parse_viewer_count_mixed_actions_finds_live_one():
    """Audit 3/8: action non-live pertama tidak boleh membutakan action live
    yang valid di belakangnya (dulu return None, kini continue)."""
    dead = _resp({"simpleText": "152,340 views"}, is_live=False)["actions"][0]
    live = _resp({"simpleText": "7 watching now"})["actions"][0]
    assert yv.parse_viewer_count({"actions": [dead, live]}) == 7


def test_parse_viewer_count_abbreviated_numbers():
    """Audit 3/8: "1.6K" pernah terbaca 16 -> false decrease lalu lonjakan
    palsu -> Arti nyapa hantu."""
    assert yv.parse_viewer_count(_resp({"simpleText": "1.6K watching now"})) == 1600
    assert yv.parse_viewer_count(_resp({"simpleText": "1,2 rb menonton"})) == 1200
    assert yv.parse_viewer_count(_resp({"simpleText": "2.345 orang menonton"})) == 2345


def test_parse_viewer_count_original_zero_is_valid():
    data = _resp({"simpleText": "1 watching now"}, originalViewCount=0)
    assert yv.parse_viewer_count(data) == 0, "0 penonton = angka valid, bukan falsy"


def test_parse_viewer_count_prefers_clean_integer_fields():
    data = _resp(
        {"simpleText": "1,625 watching now"},
        originalViewCount="1625",
        unlabeledViewCountValue={"simpleText": "1626"},
    )
    assert yv.parse_viewer_count(data) == 1625, "originalViewCount menang"
    data2 = _resp(
        {"simpleText": "1,625 watching now"},
        unlabeledViewCountValue={"simpleText": "1626"},
    )
    assert yv.parse_viewer_count(data2) == 1626, "unlabeled sebelum teks berlabel"


def test_worker_fires_on_increase_and_decrease_only():
    """[3, 3, 4, 2, None, 2]: naik sekali (3->4), turun sekali (4->2);
    sampel pertama = baseline; None (fetch gagal) tidak mengubah arah."""
    cfg = {"youtube_chat_enabled": True, "yt_viewer_poll_sec": 0.0}
    samples = [3, 3, 4, 2, None, 2]
    ups, downs = [], []

    def fetch():
        if not samples:
            cfg["youtube_chat_enabled"] = False
            return None
        return samples.pop(0)

    yv.viewer_count_worker(
        cfg,
        fetch_count=fetch,
        on_increase=lambda a, b: ups.append((a, b)),
        on_decrease=lambda a, b: downs.append((a, b)),
        sleep=lambda s: None,
    )
    assert ups == [(3, 4)]
    assert downs == [(4, 2)]


def test_worker_callback_crash_does_not_kill_loop():
    cfg = {"youtube_chat_enabled": True, "yt_viewer_poll_sec": 0.0}
    samples = [1, 2, 3]
    seen = []

    def fetch():
        if not samples:
            cfg["youtube_chat_enabled"] = False
            return None
        return samples.pop(0)

    def boom(a, b):
        seen.append((a, b))
        raise RuntimeError("callback rusak")

    yv.viewer_count_worker(
        cfg, fetch_count=fetch, on_increase=boom, sleep=lambda s: None
    )
    assert seen == [(1, 2), (2, 3)], "loop wajib selamat dari callback error"


# --- wiring bridge ---------------------------------------------------------------


def test_bridge_increase_bumps_life_and_sets_note(monkeypatch):
    import hermes_vtuber_bridge as b

    monkeypatch.setattr(b, "_last_human_activity_ts", 0.0)
    monkeypatch.setattr(b, "_viewer_join_note", "")
    monkeypatch.setattr(b, "_viewer_join_note_ts", 0.0)
    b._on_viewer_count_increase(3, 4)
    assert b._last_human_activity_ts > time.time() - 5, (
        "penonton naik = tanda kehidupan (bangunkan proaktif)"
    )
    assert "TANPA menyebut angka" in b._viewer_join_note
    assert b._yt_viewer_count == 4


def test_bridge_decrease_is_not_a_life_sign(monkeypatch):
    import hermes_vtuber_bridge as b

    monkeypatch.setattr(b, "_last_human_activity_ts", 111.0)
    b._on_viewer_count_decrease(4, 2)
    assert b._last_human_activity_ts == 111.0, (
        "turun BUKAN tanda kehidupan — 5 menit sunyi berikutnya = off"
    )
    assert b._yt_viewer_count == 2


def test_materials_include_fresh_join_note_only(monkeypatch):
    import hermes_vtuber_bridge as b

    monkeypatch.setattr(b, "_viewer_join_note", "Barusan ada yang masuk nonton")
    monkeypatch.setattr(b, "_viewer_join_note_ts", time.time() - 10)
    assert b._initiative_materials()["viewer_join_note"] != ""
    monkeypatch.setattr(b, "_viewer_join_note_ts", time.time() - 500)
    assert b._initiative_materials()["viewer_join_note"] == "", (
        "note basi (>120 dtk) jangan bikin Arti nyapa hantu"
    )


def test_worker_started_alongside_chat_listener():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    i = src.index("threading.Thread(target=youtube_chat_worker")
    assert "start_yt_viewer_count_worker()" in src[i:i + 300], (
        "telemetri penonton hidup bersama listener chat"
    )
    assert '"initiative_dormant_after_idle_sec": 300.0,' in src, (
        "spek Bohan: 5 menit sunyi = off"
    )
