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
    assert ctx.llm_system == "system base" + vp.DELIVERY_FACTS


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
    # DIUBAH 26 Agu 2026. Baris panjang di cabang NON-YouTube dulu hardcoded
    # "2-3 kalimat penuh" — kesalahan yang SAMA PERSIS dengan yang sudah
    # diperbaiki untuk YouTube (komentar di arti_voice_pipeline: 58% jawaban
    # kena potong di live 11,5 jam), tapi tertinggal di cabang ini. Tes lama
    # justru MENGUNCI bug itu. Sekarang instruksinya ikut rencana adaptif.
    assert "2-3 kalimat penuh" not in ctx.prompt_content
    assert "kalimat" in ctx.prompt_content.lower()
    assert "Bahasa Indonesia" in ctx.prompt_content


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


def test_spoken_youtube_reply_has_truthful_delivery_facts():
    ctx = _viewer_ctx("apa kamu sudah bisa mengetik di chat YouTube?")
    assert "belum ada jalur kirim pesan teks ke YouTube" in ctx.llm_system
    assert "disuarakan ke siaran" in ctx.llm_system
    assert "akun sendiri" in ctx.llm_system


def test_proactive_turn_keeps_delivery_facts_after_rag(monkeypatch):
    async def run_in_executor(lane, fn, *args):
        return fn(*args)
    monkeypatch.setattr(vp.arti_executors, "run", run_in_executor)
    monkeypatch.setattr(vp.arti_vault_rag, "append_rag_to_system",
                        lambda *args: "persona dan konteks ingatan")
    ctx = asyncio.run(vp.prepare_curious_turn_context(
        "[Curious] obrolan chat", [], "persona",
        {"curious_skip_rag": False},
        trim_system_prompt=lambda s, c: s,
        append_watch_party_context=lambda s: s,
        get_categorized_history=lambda: ""))
    assert "persona dan konteks ingatan" in ctx.llm_system
    assert "belum ada jalur kirim pesan teks ke YouTube" in ctx.llm_system
