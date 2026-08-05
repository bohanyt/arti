"""Tests for curious fast-path in voice pipeline."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_voice_pipeline as vp


def test_prepare_curious_skips_rag():
    async def _run():
        cfg = {
            "curious_skip_rag": True,
            "curious_max_history_lines": 4,
            "vault_rag_enabled": True,
            "vault_rag_live_enabled": True,
        }
        with patch.object(vp.arti_vault_rag, "append_rag_to_system") as rag:
            turn = await vp.prepare_curious_turn_context(
                "[Curious] test",
                [],
                "system base",
                cfg,
                trim_system_prompt=lambda s, c: s,
                append_watch_party_context=lambda s: s,
                get_categorized_history=lambda: "line1\nline2\nline3\nline4\nline5",
            )
        rag.assert_not_called()
        assert "PROAKTIF" in turn.target_instruction
        assert turn.formatted_history.count("\n") <= 3
        assert "[MODE PENASARAN" in turn.llm_system

    asyncio.run(_run())
