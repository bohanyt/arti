"""Voice turn pipeline — extracted prep logic from arti_bridge."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from arti_runtime.coop_session import owner_name

import arti_curious
import arti_executors
from arti_live7_moments import MomentText
import arti_reply_policy
import arti_timeline_guard
import arti_live_safety
import arti_vault_rag
import arti_web_lookup
from arti_screen_privacy import screen_privacy, PRIVACY_INSTRUCTION, serialize_tool_context

# The bridge's YouTube worker polls incoming chat; public replies go to TTS.
# Owning a separate account or reading a chat is not an outbound writer.
# Keep this contract beside prompt construction; update when a writer is wired.
DELIVERY_FACTS = (
    "\n\n[KEMAMPUAN KOMUNIKASI SAAT INI]\n"
    "Balasan untuk chat YouTube disuarakan ke siaran. Di sesi bridge ini "
    "belum ada jalur kirim pesan teks ke YouTube; punya akun sendiri atau "
    "bisa membaca chat tidak berarti sudah bisa mengetik/mengirim chat. "
    "Jangan mengaku telah mengirim pesan teks tanpa bukti pengiriman."
)

# Facts and uncertainty guidance, never a replacement line or a forced reaction.
# People and place names only. Labels like "Bahasa Indonesia" / "Nama" / "Game"
# bled into silent-room transcripts (solo 2026-11-15).
MIC_ASR_VOCABULARY = "Arti, Bohan, Stardew Valley."
MIC_TRANSCRIPT_FACTS = (
    "\n\n[UCAPAN MIC SAAT INI] Teks ini hasil transkripsi suara, belum pasti kata demi kata. "
    "Nama co-host adalah Arti dan pemilik/host adalah Bohan. Ejaan suara seperti RT "
    "bisa salah dengar nama Arti; itu bukan bukti ada orang baru bernama RT. "
    "Kalau kata, nama, atau maksud terdengar garbled/tidak jelas, boleh tanya singkat "
    "untuk memastikan; jangan mengarang maksud atau menyalahkan orang karena transkripsi. "
    "Tanggapi ucapan mic saat ini, bukan menjawab chat lama sebagai pertanyaan Bohan. "
    "Kalau Bohan mengoreksi topik, ikuti koreksinya; jangan mempertahankan asumsi dari chat sebelumnya."
)


def mic_transcript_facts(config):
    return MIC_TRANSCRIPT_FACTS.replace("Bohan", owner_name(config))

MODEL_LOOKUP_INSTRUCTION = (
    "\nKalau jawaban memerlukan fakta terbaru atau fakta yang belum kamu tahu, "
    "balas HANYA [CARI: <query singkat>]. Pakai kata kunci publik saja. "
    "Jangan kirim isi layar, identitas penonton, atau ingatan pribadi ke pencarian."
)
_SEARCH_TAG = re.compile(r"^\s*\[\s*CARI\s*:\s*([^\]\r\n]{1,500})\]", re.IGNORECASE)
_ANY_SEARCH_TAG = re.compile(r"\[\s*CAR[^\]]*(?:\]|$)", re.IGNORECASE)
_LOOKUP_UNCERTAIN = "Aku belum bisa memastikan faktanya sekarang. Coba cek sumber resminya dulu, ya."
_LOOKUP_FOLLOWUP = (
    "\nJawab pertanyaan awal sekarang tanpa tag CARI atau permintaan pencarian lagi. "
    "Kalau faktanya belum terverifikasi, katakan jujur belum bisa memastikan."
)


@dataclass
class ModelLookupTurn:
    """Keep one lookup result across a refresh of native game facts."""

    fact_block: str | None = None


async def model_lookup_reply(ask, system: str, config: dict, *,
                             regex_lookup_done: bool = False, abort=None, private_contexts=(),
                             announce=None, lookup_turn: ModelLookupTurn | None = None):
    """One model-requested search and one re-ask. Keep the tag out of public output."""
    if lookup_turn is not None and lookup_turn.fact_block is not None:
        system = system.replace(MODEL_LOOKUP_INSTRUCTION, "") + lookup_turn.fact_block + _LOOKUP_FOLLOWUP
        regex_lookup_done = True
    reply, chunks = await ask(system)
    if not config.get("web_lookup_model_decides", False):
        return reply, chunks
    if abort and abort():
        raise asyncio.CancelledError()
    match = _SEARCH_TAG.match(reply or "")
    if match and not regex_lookup_done and config.get("web_lookup_enabled", False):
        query = serialize_tool_context(match.group(1).strip())["request"]
        from arti_tool_router import model_query_deny_reason
        # RAG blocks can contain private excerpts beyond the explicit memory
        # list. Compare locally; no memory/system text crosses the lookup seam.
        rag_blocks = re.split(r"\[VAULT RAG[^\]]*\]", system, flags=re.IGNORECASE)[1:]
        screen_lines = re.findall(r"(?m)^\[LAYAR: ([^\n]+)", system)
        denied = model_query_deny_reason(query, (*private_contexts, *rag_blocks, *screen_lines))
        block = ""
        try:
            if not denied and (announce is None or await announce()):
                block = await arti_web_lookup.lookup_block_async(query, config, abort=abort)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[WebLookup] Model request failed: {type(exc).__name__}")
        if abort and abort():
            raise asyncio.CancelledError()
        if lookup_turn is not None:
            lookup_turn.fact_block = block
        followup_system = system.replace(MODEL_LOOKUP_INSTRUCTION, "") + block + _LOOKUP_FOLLOWUP
        reply, chunks = await ask(followup_system)
        if abort and abort():
            raise asyncio.CancelledError()
        # A repeated tag never causes a third call or silence on a direct question.
        reply = re.sub(r"\[\s*DIAM\s*\]", "", reply or "", flags=re.IGNORECASE)
        reply = _ANY_SEARCH_TAG.sub("", reply).strip() or _LOOKUP_UNCERTAIN
        return reply, []
    if reply and _ANY_SEARCH_TAG.search(reply):
        return _ANY_SEARCH_TAG.sub("", reply).strip() or _LOOKUP_UNCERTAIN, []
    # Streamed sentence chunks are derived from the same full reply. Rebuild
    # only after removing fragments, including fragments split across chunks.
    if chunks and _ANY_SEARCH_TAG.search("".join(chunks)):
        reply = _ANY_SEARCH_TAG.sub("", reply or "".join(chunks)).strip() or _LOOKUP_UNCERTAIN
        return reply, []
    return reply, chunks


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
    length_line: str = ""

    def prompt_for(self, speech):
        """Render only the current trigger again; history and instructions stay intact."""
        return f"""[CATATAN SEJARAH STREAM:]
{self.formatted_history}

[Pesan/Panggilan Sekarang:]
"{speech}"

{self.target_instruction}
{self.length_line}
Buka balasan ini sesuai isinya, beda dari cara kamu membuka JAWABAN ARTI TERAKHIR di atas; nggak harus nama orang atau "aku".
Jangan kutip format log, timestamp, atau label [Streamer]/[Arti]. Jangan pernah menyebut sistem internalmu — log, terminal, atau berapa banyak pesan histori/sejarah yang kamu baca. Hanya ucapkan dialog langsung dalam karakter Arti."""


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
    is_moment = isinstance(speech, MomentText)
    ctx.rag_query = extract_trigger_message(speech) or speech
    base_system = trim_system_prompt(dynamic_system_prompt, config)
    base_system = append_watch_party_context(base_system)
    if screen_privacy.restricted:
        base_system += PRIVACY_INSTRUCTION
    if arti_timeline_guard.is_timeline_question(ctx.rag_query):
        base_system = arti_timeline_guard.append_timeline_guard(base_system, config)

    async def _load_history() -> str:
        return arti_live_safety.public_context(await arti_executors.run("voice", get_categorized_history))

    async def _load_rag(system_base: str) -> str:
        # Historical vault entries have no screen provenance. A privacy interval
        # uses the conversation since revocation, not mixed historical retrieval.
        if screen_privacy.restricted:
            return system_base
        # Reaksi game: 8 detik RAG di jalur kritis untuk kejadian yang butuh
        # jawaban SEKARANG (live 2026-08-05 malam, "[recorded remark omitted]" —
        # tiap pukulan zombie menunggu lookup vault). Vault berisi kenangan
        # dan obrolan; tidak ada isinya soal "kamu baru dipukul zombie".
        # Prefix yang sama sudah dilewati _load_web_lookup dengan alasan sama.
        if is_moment or speech.startswith("[MINECRAFT"):
            return system_base
        if not (
            config.get("vault_rag_enabled", True)
            and config.get("vault_rag_live_enabled", True)
        ):
            return system_base
        rag_timeout = float(config.get("vault_rag_live_timeout_sec", 8))
        try:
            print(f"[Vault RAG] Lookup ({rag_timeout:.0f}s max): {ctx.rag_query[:72]}...")
            return await asyncio.wait_for(
                arti_executors.run("rag",
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
        if not config.get("web_lookup_regex_enabled", True):
            return ""
        if is_moment or speech.startswith(("[VIDEO", "[Inisiatif", "[MINECRAFT")):
            # Temuan audit: judul video "Berita Kripto: Harga..." menembak
            # lookup dan menambah 12 dtk ke reaksi — turn proaktif/reaksi
            # video/game tidak butuh cek internet.
            return ""
        msg = arti_reply_policy.extract_chat_message(speech) or ctx.rag_query
        msg = serialize_tool_context(msg)["request"]
        if not arti_web_lookup.needs_web_lookup(msg, config):
            return ""
        if config.get("web_lookup_model_decides", False):
            ctx.stages["web_lookup_regex"] = True
        try:
            return await asyncio.wait_for(
                arti_executors.run("background", arti_web_lookup.lookup_block, msg, config),
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
    if (config.get("web_lookup_model_decides", False)
            and config.get("web_lookup_enabled", False)
            and not ctx.stages.get("web_lookup_regex")):
        ctx.llm_system += MODEL_LOOKUP_INSTRUCTION

    is_donation = speech.startswith("[DONASI")
    is_video = speech.startswith("[VIDEO")
    is_game = speech.startswith("[MINECRAFT")
    is_from_viewer = speech.startswith("[Pesan Live Chat dari Viewer")
    if is_moment and config.get("live7_reactive_play_enabled") and getattr(speech, "play_instruction", None):
        ctx.target_instruction = speech.play_instruction
        length_line = ""  # the named instruction already specifies one short sentence
    elif is_moment and getattr(speech, "_generic_discovery", False):
        ctx.target_instruction = (
            "Ini kabar kejadian dari permainanmu; waktunya ada di faktanya, bisa sudah lewat. Nilai sendiri apakah penting; boleh [diam]. "
            "Bereaksi seperti teman/streamer, bukan narasi: jangan mendeskripsikan apa yang "
            "kamu lihat. Gunakan hanya fakta kejadian ini; jangan mengarang rincian atau sebab."
        )
        length_line = "Jawab 1-2 kalimat spontan dalam Bahasa Indonesia, atau [diam]."
    elif is_moment and getattr(speech, "_live7_status", False):
        ctx.target_instruction = (
            "Ini fakta kondisi Stardew yang sedang KAMU mainkan. Reaksi alami sebagai Arti, "
            "bukan narasi atau membaca fakta/log. Pakai kata-katamu sendiri; saat macet "
            "jujur ada yang salah, jangan mengaku bekerja atau bercanda menutupi masalah. "
            "Kalau fakta menyebut pemulihan, tanggapi pemulihan itu saja tanpa mengarang hasil game."
        )
        length_line = "Jawab 1–2 kalimat dalam Bahasa Indonesia."
    elif is_moment:
        ctx.target_instruction = (
            "Ini situasi barusan di Stardew yang sedang KAMU mainkan pakai karaktermu "
            "sendiri; petani HOST mengikuti identitas native, penonton chat berada di luar game. Reaksi, bukan "
            "narasi: jangan ceritakan ulang apa yang terjadi — tunjukkan perasaan, "
            "celetukan, atau rencanamu, spontan sebagai Arti."
        )
        length_line = "Jawab 1-3 kalimat spontan dalam Bahasa Indonesia."
    elif is_game:
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
            "penonton, reaksikan sebagai Arti — jangan ringkas isinya."
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
        # ("exampleviewr586"). Post-process bridge jadi jaring pengaman kedua.
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
            "dalam karakter Arti kepada streamer (Bohan). Jawab isi [Pesan/Panggilan Sekarang]; chat viewer dan history bukan pertanyaan Bohan saat ini. Jangan mengatribusikan keinginan viewer kepada Bohan."
        )
        # DULU hardcoded "2-3 kalimat" — kesalahan yang SAMA PERSIS dengan yang
        # sudah diperbaiki untuk YouTube di atas, tapi tertinggal di cabang ini.
        # Padahal cabang inilah yang menangani inisiatif/curious + mic: 31 dari
        # 44 giliran pada sesi 26 Agu.
        plan = arti_reply_policy.resolve_reply_plan(speech, config, quiet=quiet)
        length_line = arti_reply_policy.format_reply_instruction(plan).strip()

    ctx.length_line = length_line
    ctx.prompt_content = ctx.prompt_for(speech)
    ctx.llm_system += DELIVERY_FACTS
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
        # Timeout khusus curious: overhead tetap embedding LM Studio ~2,1 dtk
        # membuat timeout live (1 dtk) selalu gugur — RAG curious akan mati
        # diam-diam 100% kalau memakai kunci yang sama dengan jalur suara.
        rag_timeout = float(config.get("vault_rag_curious_timeout_sec", 5.0))
        try:
            ctx.llm_system = await asyncio.wait_for(
                arti_executors.run("rag",
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
    if (config.get("web_lookup_enabled") and config.get("web_lookup_model_decides")
            and not config.get("web_lookup_regex_enabled", True)):
        ctx.llm_system += MODEL_LOOKUP_INSTRUCTION
    full_history = arti_live_safety.public_context(await arti_executors.run("voice", get_categorized_history))
    ctx.formatted_history = _trim_history_lines(full_history, max_lines)

    # Persona 2026-08-03: kewajiban "[recorded remark omitted]" dicabut —
    # gara-gara ini Arti nanya Bohan MULU tiap turn proaktif ("[recorded remark omitted]"). Opini/celetukan bernilai sama; pertanyaan = bumbu opsional.
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
            "Reaksi, bukan narasi: jangan mendeskripsikan apa yang ada di layar."
        )

    ctx.prompt_content = f"""[Cuplikan sejarah singkat:]
{ctx.formatted_history}

[Konteks proaktif:]
{speech}

{ctx.target_instruction}
Maksimal 3 kalimat Bahasa Indonesia. Jangan kutip format log."""
    ctx.llm_system += DELIVERY_FACTS
    return ctx
