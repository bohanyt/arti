"""Voice turn pipeline — extracted prep logic from hermes_vtuber_bridge."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import arti_curious
import arti_reply_policy
import arti_timeline_guard
import arti_vault_rag
import arti_web_lookup


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


def _trim_history_lines(history: str, max_lines: int) -> str:
    if max_lines <= 0 or not history:
        return history
    lines = [ln for ln in history.splitlines() if ln.strip()]
    if len(lines) <= max_lines:
        return history
    return "\n".join(lines[-max_lines:])


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
    quiet: bool = False,
) -> TurnContext:
    """Build history + system prompt (with RAG) in parallel before LLM call.

    `quiet` = chat YT sedang sepi (dihitung bridge) — dipakai rencana panjang
    jawaban untuk rant mode.
    """
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

    async def _load_web_lookup() -> str:
        """Fitur C: cek web PARALEL dengan RAG — biaya bersih = selisihnya saja.

        Pemicu konservatif (needs_web_lookup); budget keras via wait_for karena
        thread requests tidak bisa dibatalkan dari dalam. Gagal/timeout = "".
        """
        if speech.startswith(("[VIDEO", "[Inisiatif", "[MINECRAFT")):
            # Temuan audit: judul video "Berita Kripto: Harga..." menembak
            # lookup dan menambah 12 dtk ke reaksi — turn proaktif/reaksi
            # video/game tidak butuh cek internet.
            return ""
        msg = arti_reply_policy.extract_chat_message(speech) or ctx.rag_query
        if not arti_web_lookup.needs_web_lookup(msg, config):
            return ""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(arti_web_lookup.lookup_block, msg, config),
                timeout=float(config.get("web_lookup_turn_budget_sec", 12.0)),
            )
        except asyncio.TimeoutError:
            print("[WebLookup] Budget turn habis — jawab tanpa web")
            return ""
        except Exception as e:  # noqa: BLE001
            print(f"[WebLookup] Skip ({type(e).__name__}: {e})")
            return ""

    ctx.formatted_history, ctx.llm_system, _web_block = await asyncio.gather(
        _load_history(),
        _load_rag(base_system),
        _load_web_lookup(),
    )
    if _web_block:
        ctx.llm_system += _web_block

    is_donation = speech.startswith("[DONASI")
    is_video = speech.startswith("[VIDEO")
    is_game = speech.startswith("[MINECRAFT")
    is_from_viewer = speech.startswith("[Pesan Live Chat dari Viewer")
    if is_game:
        # Kejadian di dunia Minecraft yang DIA mainkan (trigger type "game") —
        # reaksi spontan pemain, bukan komentar penonton.
        ctx.target_instruction = (
            "Kejadian barusan terjadi di Minecraft yang lagi KAMU mainkan — "
            "kamu pemainnya, bukan penonton. Reaksikan spontan sebagai Arti "
            "(boleh dramatis/lucu). Kalau mau bertindak, tutup jawaban dengan "
            "SATU tag aksi [MC: ...] — tag tidak boleh disebut/dibaca."
        )
        length_line = (
            "Jawab 1-3 kalimat spontan dalam Bahasa Indonesia."
        )
    elif is_video:
        # Trigger video sudah membawa arahan lengkap (nada per sumber diatur
        # format_reaction_trigger di arti_video_watcher).
        ctx.target_instruction = (
            "Ikuti arahan di blok video di atas — kamu barusan nonton bareng "
            "penonton, komentari sebagai Arti."
        )
        length_line = (
            "Jawab 2-4 kalimat penuh energi dalam Bahasa Indonesia."
        )
    elif is_donation:
        donor = ""
        m = re.search(r"dari (@?\S+)\]", speech)
        if m:
            donor = m.group(1)
        nick = arti_reply_policy.viewer_nickname(donor) if donor else ""
        panggil = f' Panggil dia "{nick}".' if nick else ""
        ctx.target_instruction = (
            "ADA DONASI MASUK! Ucapkan terima kasih yang hangat, spesifik, dan "
            "bersemangat — sebut nominalnya dengan takjub, lalu respon isi "
            f"pesannya (kalau ada) dengan sungguh-sungguh.{panggil} "
            "Jangan terdengar seperti template."
        )
        length_line = (
            "Jawab 2-4 kalimat penuh energi dalam Bahasa Indonesia. "
            "Jangan baca angka rekening/simbol aneh; sebut nominal secara natural."
        )
    elif is_from_viewer:
        ctx.target_instruction = (
            "Jawab pesan/pertanyaan dari viewer tersebut dengan ramah, imut, "
            "dan cerdas dalam karakter Arti kepada viewer tersebut."
        )
        # Nama panggilan pendek — TTS jangan baca handle utuh + angka ekornya
        # ("penontonsetia241"). Post-process bridge jadi jaring pengaman kedua.
        handle = arti_reply_policy.extract_viewer_handle(speech)
        nick = arti_reply_policy.viewer_nickname(handle)
        if nick and nick.lower() != handle.lstrip("@").lower():
            ctx.target_instruction += (
                f' Panggil dia "{nick}" saja — JANGAN baca handle lengkap '
                f"atau angka-angka di belakang namanya."
            )
        # Panjang dari rencana adaptif (brief/normal/deep/gacha/rant) — dulu
        # hardcoded "2-3 kalimat" dan MENABRAK plan: model nulis 2-3, token
        # dibatasi per plan, filter memotong sisanya (58% jawaban kena potong
        # di live 11,5 jam). Sekarang prompt, token, dan filter sepakat.
        plan = arti_reply_policy.resolve_yt_reply_plan(speech, config, quiet=quiet)
        length_line = arti_reply_policy.format_yt_reply_instruction(plan).strip()
    else:
        ctx.target_instruction = (
            "Jawab panggilan streamer sekarang sebagai Arti. Langsung bicara "
            "dalam karakter Arti kepada streamer (Bohan)."
        )
        length_line = (
            "Jawab dalam 2-3 kalimat penuh dalam Bahasa Indonesia. "
            "Jangan terlalu pendek, jangan yapping."
        )

    ctx.prompt_content = f"""[CATATAN SEJARAH STREAM:]
{ctx.formatted_history}

[Pesan/Panggilan Sekarang:]
"{speech}"

{ctx.target_instruction}
{length_line}
Jangan kutip format log, timestamp, atau label [Streamer]/[Arti]. Jangan pernah menyebut sistem internalmu — log, terminal, atau berapa banyak pesan histori/sejarah yang kamu baca. Hanya ucapkan dialog langsung dalam karakter Arti."""
    return ctx


async def prepare_curious_turn_context(
    speech: str,
    memories: list,
    dynamic_system_prompt: str,
    config: dict,
    *,
    trim_system_prompt: Callable[[str, dict], str],
    append_watch_party_context: Callable[[str], str],
    get_categorized_history: Callable[[], str],
) -> TurnContext:
    """Fast path for proactive curious turns — skip RAG, short history."""
    ctx = TurnContext(
        speech=speech,
        memories=memories,
        dynamic_system_prompt=dynamic_system_prompt,
    )
    scouter = config.get("scouter_last_result") or {}
    topic = (scouter.get("topic") or "").strip()
    ctx.rag_query = topic or speech[:80]

    base_system = trim_system_prompt(dynamic_system_prompt, config)
    base_system = append_watch_party_context(base_system)
    base_system = base_system + arti_curious.build_curious_system_addon(config)

    skip_rag = config.get("curious_skip_rag", True)
    if skip_rag:
        ctx.llm_system = base_system
        print("[Curious] Skip Vault RAG (fast path).")
    else:
        rag_timeout = float(config.get("vault_rag_live_timeout_sec", 1))
        try:
            ctx.llm_system = await asyncio.wait_for(
                asyncio.to_thread(
                    arti_vault_rag.append_rag_to_system,
                    base_system,
                    ctx.rag_query,
                    config,
                ),
                timeout=rag_timeout,
            )
        except (asyncio.TimeoutError, Exception):
            ctx.llm_system = base_system

    max_lines = int(config.get("curious_max_history_lines", 8))
    full_history = await asyncio.to_thread(get_categorized_history)
    ctx.formatted_history = _trim_history_lines(full_history, max_lines)

    # Persona 2026-08-03: kewajiban "SATU pertanyaan ke streamer" dicabut —
    # gara-gara ini Arti nanya Bohan MULU tiap turn proaktif ("kayak gapunya
    # pendirian"). Opini/celetukan bernilai sama; pertanyaan = bumbu opsional.
    if speech.startswith("[Komentar main game]"):
        # Turn proaktif SAAT MAIN GAME: bahannya dunia Minecraft, bukan layar
        # OBS — instruksi layar di bawah bikin dia komentari hal yang salah.
        ctx.target_instruction = (
            "Ini giliran kamu ngomong sendiri SAMBIL MAIN GAME (bukan "
            "dipanggil streamer). Ikuti sudut yang diminta di pesan: "
            "komentari kejadian/aksi/rencanamu di dalam game seperti streamer "
            "yang lagi asyik main. Jangan mendeskripsikan layar OBS."
        )
    elif speech.startswith(("[Arti pegang siaran]", "[Bohan balik]")):
        # Bohan AFK: Arti pembawa acaranya, bukan pengisi keheningan.
        ctx.target_instruction = (
            "Kamu lagi PEGANG SIARAN sendiri (Bohan AFK). Ikuti sudut yang "
            "diminta di pesan: bicara ke penonton sebagai host — punya bahan, "
            "punya pendapat, ajak mereka ngobrol. Jangan mengeluh sepi, jangan "
            "nunggu Bohan, jangan mengarang seolah dia menjawab."
        )
    else:
        ctx.target_instruction = (
            "Ini giliran PROAKTIF Arti (bukan dipanggil streamer). "
            "Tunjukkan rasa penasaran atau opini kamu pada satu detail spesifik "
            "di layar — kamu punya pendirian sendiri. Pertanyaan penutup OPSIONAL; "
            "jangan selalu nanya streamer (penonton juga boleh jadi target). "
            "Jangan mendeskripsikan ulang seluruh layar secara generik."
        )

    ctx.prompt_content = f"""[Cuplikan sejarah singkat:]
{ctx.formatted_history}

[Konteks proaktif:]
{speech}

{ctx.target_instruction}
Maksimal 3 kalimat Bahasa Indonesia. Jangan kutip format log."""
    return ctx
