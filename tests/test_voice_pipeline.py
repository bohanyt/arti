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
