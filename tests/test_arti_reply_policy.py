"""YouTube adaptive reply length policy."""
from __future__ import annotations

import arti_reply_policy as policy

CFG = {
    "arti_reply_yt_adaptive": True,
    "arti_reply_yt_gacha_min_sentences": 1,
    "arti_reply_yt_gacha_max_sentences": 5,
}


def _plan(msg: str) -> policy.YtReplyPlan:
    wrap = f'[Pesan Live Chat dari Viewer @x (YouTube)]: {msg}'
    return policy.resolve_yt_reply_plan(wrap, CFG)


def test_yt_halo_brief():
    p = _plan("halo")
    assert policy.classify_yt_message("halo") == "brief"
    assert 1 <= p.sentences <= 2


def test_yt_deep_question():
    msg = (
        "arti menurut kamu kenapa vault RAG pakai embedding lokal "
        "dan gimana bedanya sama keyword search biasa?"
    )
    assert policy.classify_yt_message(msg) in ("normal", "deep")
    p = _plan(msg)
    assert p.sentences >= 3


def test_yt_gacha_deterministic():
    p1 = _plan("wkwk aneh")
    p2 = _plan("wkwk aneh")
    assert p1.sentences == p2.sentences
    assert 1 <= p1.sentences <= 5


def test_yt_gacha_range_varies():
    plans = {_plan(f"pesan ambigu nomor {i}").sentences for i in range(30)}
    assert len(plans) > 1


# --- rant mode: sesekali panjang banget, HANYA saat sepi ----------------------
# Permintaan Bohan 2026-08-01: "pertanyaan kurang bermutu jawabnya panjang
# banget mungkin, tapi harus jarang, mungkin kalo sepi".


def _plan_q(msg, quiet=False, **cfg_over):
    wrap = f'[Pesan Live Chat dari Viewer @x (YouTube)]: {msg}'
    return policy.resolve_yt_reply_plan(wrap, {**CFG, **cfg_over}, quiet=quiet)


def test_rant_never_when_chat_busy():
    for i in range(120):
        p = _plan_q(f"halo arti nomor {i}", quiet=False)
        assert not p.mode.startswith("rant")
        assert p.sentences <= 5


def test_rant_rare_but_present_when_quiet():
    modes = [_plan_q(f"halo arti nomor {i}", quiet=True).mode for i in range(200)]
    rants = sum(1 for m in modes if m.startswith("rant"))
    assert 0 < rants < 50, (
        f"rant harus JARANG tapi ada (~10%); dapat {rants}/200"
    )


def test_rant_plan_is_deterministic_and_generous():
    found = None
    for i in range(300):
        p = _plan_q(f"pesan iseng {i}", quiet=True)
        if p.mode.startswith("rant"):
            found = (f"pesan iseng {i}", p)
            break
    assert found, "300 pesan tanpa satu pun rant — dadu mati"
    msg, p = found
    p2 = _plan_q(msg, quiet=True)
    assert (p.sentences, p.max_tokens) == (p2.sentences, p2.max_tokens), (
        "dadu wajib deterministik per pesan — dua panggilan plan per turn "
        "(token & filter) harus sepakat"
    )
    assert 6 <= p.sentences <= 8
    assert p.max_tokens >= 380, "rant butuh token longgar biar tidak kepotong"
    assert p.max_chars > 500, "rant boleh melewati cap karakter YT biasa"
    assert "sepi" in policy.format_yt_reply_instruction(p).lower()


def test_rant_never_hijacks_deep_questions():
    for i in range(40):
        msg = (
            f"arti menurut kamu kenapa sistem memori nomor {i} penting "
            "dan gimana cara kerjanya dibanding catatan biasa?"
        )
        p = _plan_q(msg, quiet=True)
        assert not p.mode.startswith("rant"), (
            "deep sudah punya jatah panjang sendiri — rant khusus pertanyaan garing"
        )


def test_rant_chance_zero_disables():
    for i in range(100):
        p = _plan_q(f"halo arti nomor {i}", quiet=True, arti_reply_rant_chance=0.0)
        assert not p.mode.startswith("rant")


def test_bridge_wires_quiet_signal():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "def yt_chat_is_quiet" in src
    assert '"yt_quiet_after_sec"' in src
    assert src.count("quiet=yt_chat_is_quiet(") >= 2, (
        "plan (_yt_reply_plan) DAN prepare_turn_context harus menerima sinyal sepi"
    )


# --- nickname viewer: 2-3 suku kata, tanpa angka ekor (Bohan 2026-08-02) -------


def test_viewer_nickname_strips_trailing_digits_and_suffix():
    cases = {
        "@penontonsetia241": "penonton",
        "@Dewi-radio108": "Dewi",
        "@RiskyTuan": "Risky",
        "@bohanyt": "bohan",
        "@namayangpanjangnya7169": "namayang",
        "@Warga_H1889": "Warga",
        "@budi3Dmodeller": "budi",
        "@kelap-z": "kelap",
    }
    for handle, want in cases.items():
        assert policy.viewer_nickname(handle) == want, handle


def test_viewer_nickname_keeps_short_handles():
    assert policy.viewer_nickname("@tamubaru") == "tamubaru"
    assert policy.viewer_nickname("") == ""


def test_extract_viewer_handle_from_wrapper():
    w = "[Pesan Live Chat dari Viewer @penontonsetia241 (YouTube)]: halo"
    assert policy.extract_viewer_handle(w) == "@penontonsetia241"
    assert policy.extract_viewer_handle("halo biasa") == ""
