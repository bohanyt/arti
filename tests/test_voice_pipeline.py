"""Tests for arti_voice_pipeline."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_voice_pipeline as vp


def test_prepare_turn_context_builds_prompt():
    ctx = asyncio.run(
        vp.prepare_turn_context(
            "eh arti halo",
            [],
            "system base",
            {"vault_rag_live_enabled": False},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "[history]",
            extract_trigger_message=lambda s: s,
        )
    )
    assert "[history]" in ctx.prompt_content
    assert "eh arti halo" in ctx.prompt_content
    assert ctx.llm_system == "system base"


def _viewer_ctx(msg, quiet=False):
    speech = f'[Pesan Live Chat dari Viewer @x (YouTube)]: {msg}'
    return asyncio.run(
        vp.prepare_turn_context(
            speech,
            [],
            "system base",
            {"vault_rag_live_enabled": False},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "[history]",
            extract_trigger_message=lambda s: s,
            quiet=quiet,
        )
    )


def test_viewer_prompt_length_follows_plan_not_hardcoded():
    """Dulu hardcoded "2-3 kalimat" MENABRAK rencana adaptif: model nulis 2-3,
    filter memotong sisanya (58% jawaban kena potong di live 11,5 jam)."""
    import arti_reply_policy as policy

    deep_msg = (
        "arti menurut kamu kenapa vault RAG pakai embedding lokal "
        "dan gimana bedanya sama keyword search biasa?"
    )
    ctx = _viewer_ctx(deep_msg)
    assert "2-3 kalimat penuh" not in ctx.prompt_content, (
        "instruksi panjang viewer harus dari plan, bukan hardcoded"
    )
    plan = policy.resolve_yt_reply_plan(
        f'[Pesan Live Chat dari Viewer @x (YouTube)]: {deep_msg}',
        {},
    )
    assert f"{plan.sentences} kalimat" in ctx.prompt_content


def test_streamer_prompt_keeps_default_length_line():
    ctx = asyncio.run(
        vp.prepare_turn_context(
            "eh arti halo",
            [],
            "system base",
            {"vault_rag_live_enabled": False},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "[history]",
            extract_trigger_message=lambda s: s,
        )
    )
    assert "2-3 kalimat penuh" in ctx.prompt_content


def test_viewer_rant_prompt_mentions_quiet_vibes():
    import arti_reply_policy as policy

    found = None
    for i in range(300):
        msg = f"pesan iseng {i}"
        plan = policy.resolve_yt_reply_plan(
            f'[Pesan Live Chat dari Viewer @x (YouTube)]: {msg}', {}, quiet=True
        )
        if plan.mode.startswith("rant"):
            found = msg
            break
    assert found
    ctx = _viewer_ctx(found, quiet=True)
    assert "sepi" in ctx.prompt_content.lower()
