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
