"""Voice turn pipeline — extracted prep logic from arti_bridge."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

import arti_timeline_guard
import arti_vault_rag


@dataclass
class TurnContext:
    speech: str
    memories: list
    dynamic_system_prompt: str
    formatted_history: str = ""
    llm_system: str = ""
    prompt_content: str = ""
    rag_query: str = ""
    target_instruction: str = ""
    stages: dict[str, Any] = field(default_factory=dict)


async def prepare_turn_context(
    speech: str,
    memories: list,
    dynamic_system_prompt: str,
    config: dict,
    *,
    trim_system_prompt: Callable[[str, dict], str],
    append_watch_party_context: Callable[[str], str],
    get_categorized_history: Callable[[], str],
    extract_trigger_message: Callable[[str], str],
) -> TurnContext:
    """Build history + system prompt (with RAG) in parallel before LLM call."""
    ctx = TurnContext(
        speech=speech,
        memories=memories,
        dynamic_system_prompt=dynamic_system_prompt,
    )
    ctx.rag_query = extract_trigger_message(speech) or speech
    base_system = trim_system_prompt(dynamic_system_prompt, config)
    base_system = append_watch_party_context(base_system)
    if arti_timeline_guard.is_timeline_question(ctx.rag_query):
        base_system = arti_timeline_guard.append_timeline_guard(base_system, config)

    async def _load_history() -> str:
        return await asyncio.to_thread(get_categorized_history)

    async def _load_rag(system_base: str) -> str:
        if not (
            config.get("vault_rag_enabled", True)
            and config.get("vault_rag_live_enabled", True)
        ):
            return system_base
        rag_timeout = float(config.get("vault_rag_live_timeout_sec", 8))
        try:
            print(f"[Vault RAG] Lookup ({rag_timeout:.0f}s max): {ctx.rag_query[:72]}...")
            return await asyncio.wait_for(
                asyncio.to_thread(
                    arti_vault_rag.append_rag_to_system,
                    system_base,
                    ctx.rag_query,
                    config,
                ),
                timeout=rag_timeout,
            )
        except asyncio.TimeoutError:
            print("[Vault RAG] Timeout — skip, lanjut tanpa RAG.")
            return system_base
        except Exception as e:
            print(f"[Vault RAG] Skip ({type(e).__name__}: {e})")
            return system_base

    ctx.formatted_history, ctx.llm_system = await asyncio.gather(
        _load_history(),
        _load_rag(base_system),
    )

    name = (config.get("cohost_name") or "co-host").strip()
    tone = (config.get("voice_tone_adjectives") or "ramah dan natural").strip()
    style = (config.get("voice_reply_style_hint") or "").strip()
    is_from_viewer = speech.startswith("[Pesan Live Chat dari Viewer")
    if is_from_viewer:
        custom = (config.get("voice_viewer_instruction") or "").strip()
        ctx.target_instruction = custom or (
            f"Jawab pesan/pertanyaan dari viewer tersebut dengan {tone} "
            f"dalam karakter {name} kepada viewer tersebut."
        )
    else:
        custom = (config.get("voice_streamer_instruction") or "").strip()
        ctx.target_instruction = custom or (
            f"Jawab panggilan streamer sekarang sebagai {name}. Langsung bicara "
            f"dalam karakter co-host kepada streamer."
        )

    style_line = f"\n{style}" if style else ""
    ctx.prompt_content = f"""[CATATAN SEJARAH STREAM:]
{ctx.formatted_history}

[Pesan/Panggilan Sekarang:]
"{speech}"

{ctx.target_instruction}{style_line}
Jangan kutip format log, timestamp, atau label [Streamer]/[{name}]. Hanya ucapkan dialog langsung dalam karakter {name}."""
    return ctx
