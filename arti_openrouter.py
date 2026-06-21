"""OpenRouter offline brain: summarizer + post-stream reflection (Fase 2)."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

_ROOT = Path(__file__).resolve().parent
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _headers(api_key: str, title: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/YOUR_USER/YOUR_REPO",
        "X-Title": title,
    }


def _stream_delta_text(chunk: dict) -> str:
    try:
        delta = chunk["choices"][0]["delta"]
        if isinstance(delta, dict):
            return str(delta.get("content") or delta.get("reasoning") or "")
    except (KeyError, IndexError, TypeError):
        pass
    return ""


def _openrouter_chat_stream(
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float,
    timeout: int,
    title: str,
    preview_prefix: str,
    hide_preamble_until_marker: bool = False,
) -> str | None:
    """SSE streaming — cetak token ke terminal sambil mengumpulkan teks penuh."""
    res = requests.post(
        _OPENROUTER_URL,
        headers=_headers(api_key, title),
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        },
        timeout=timeout,
        stream=True,
    )
    if res.status_code != 200:
        print(f"[OpenRouter] {model} HTTP {res.status_code}: {res.text[:200]}")
        return None

    print(preview_prefix, end="", flush=True)
    parts: list[str] = []
    hold: list[str] = []
    printing = not hide_preamble_until_marker

    def _emit(s: str) -> None:
        if s:
            print(s, end="", flush=True)

    for raw in res.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        piece = _stream_delta_text(chunk)
        if not piece:
            continue
        parts.append(piece)
        if printing:
            _emit(piece)
            continue
        hold.append(piece)
        combined = "".join(hold)
        for marker in _REFLECTION_START_MARKERS:
            if marker in combined:
                idx = combined.find(marker)
                printing = True
                _emit(combined[idx:])
                hold = []
                break

    print(flush=True)
    text = "".join(parts).strip()
    if not text:
        return None
    return strip_reflection_reasoning_preamble(text)


def openrouter_chat(
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int = 800,
    temperature: float = 0.3,
    timeout: int = 120,
    title: str = "Arti VTuber",
    stream: bool = False,
    stream_preview_prefix: str | None = None,
) -> str | None:
    if not api_key:
        return None
    try:
        if stream:
            prefix = stream_preview_prefix or f"  ↳ "
            return _openrouter_chat_stream(
                api_key,
                model,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                title=title,
                preview_prefix=prefix,
                hide_preamble_until_marker=bool(stream_preview_prefix is not None),
            )

        res = requests.post(
            _OPENROUTER_URL,
            headers=_headers(api_key, title),
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        if res.status_code == 200:
            body = res.json()
            try:
                msg = body["choices"][0]["message"]
                content = msg.get("content") if isinstance(msg, dict) else None
                if not content and isinstance(msg, dict):
                    content = msg.get("reasoning")
            except (KeyError, IndexError, TypeError):
                content = None
            if content and str(content).strip():
                out = str(content).strip()
                if title == "Arti Reflection":
                    out = strip_reflection_reasoning_preamble(out)
                return out
            print(
                f"[OpenRouter] {model} HTTP 200 tapi content kosong: "
                f"{json.dumps(body, ensure_ascii=False)[:350]}"
            )
            return None
        print(f"[OpenRouter] {model} HTTP {res.status_code}: {res.text[:200]}")
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"[OpenRouter] {model} error: {e}")
    return None


def openrouter_live_model_chain(config: dict) -> list[str]:
    """Chain live OpenRouter — default: Laguna XS.2 (TPS tinggi) lalu owl."""
    laguna = config.get("openrouter_live_model", "poolside/laguna-xs.2:free")
    owl = config.get("openrouter_live_last_resort", "owl-alpha")

    if config.get("openrouter_live_fast_only", True):
        chain = [laguna, owl]
        return [m for m in chain if m]

    chain: list[str] = []
    if config.get("openrouter_live_use_fast_nano"):
        chain.append(
            config.get(
                "openrouter_live_fast_model",
                "nvidia/nemotron-3-nano-30b-a3b:free",
            )
        )
    for key in (
        "openrouter_live_model",
        "openrouter_live_fallback_model",
        "openrouter_live_last_resort",
    ):
        m = config.get(key)
        if m and m not in chain:
            chain.append(m)
    if not chain:
        chain = [laguna, owl]
    return chain


def openrouter_live_completion(
    system_prompt: str,
    user_content: str,
    config: dict,
) -> tuple[str | None, str | None]:
    """Jawaban live Arti via OpenRouter (fallback setelah Groq)."""
    if not config.get("openrouter_live_fallback_enabled", True):
        return None, None
    key = config.get("openrouter_api_key") or ""
    if not key:
        print("[OpenRouter Live] OPENROUTER_API_KEY kosong, skip.")
        return None, None

    import arti_reply_policy

    if arti_reply_policy.is_youtube_trigger(user_content):
        plan = arti_reply_policy.resolve_yt_reply_plan(user_content, config)
        max_tok = plan.max_tokens
        strict_tail = "[PENTING]" + arti_reply_policy.format_yt_reply_instruction(plan)
    else:
        sent = int(config.get("arti_reply_max_sentences", 5))
        max_tok = int(
            config.get("live_max_tokens_ptt", config.get("openrouter_live_max_tokens", 380))
        )
        strict_tail = (
            f"\n\n[PENTING] Jawab ucapan co-host ke streamer (sampai {sent} kalimat, boleh ada depth). "
            "Jangan jelaskan tugas, aturan, atau bilang 'sebagai Arti'."
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content + strict_tail},
    ]
    timeout = int(config.get("openrouter_live_timeout_sec", 45))
    temp = float(config.get("openrouter_live_temperature", 0.65))

    for model in openrouter_live_model_chain(config):
        print(f"[OpenRouter Live] Trying {model}...")
        text = openrouter_chat(
            key,
            model,
            messages,
            max_tokens=max_tok,
            temperature=temp,
            timeout=timeout,
            title="Arti Live",
        )
        if text:
            return text, f"openrouter/{model}"
    print("[OpenRouter Live] Semua model gagal.")
    return None, None


def _parse_json_blob(text: str) -> dict | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


def call_summarizer(context_text: str, config: dict) -> dict | None:
    """Live scouter — multi-provider chain (delegates to arti_scouter_client)."""
    import arti_scouter_client

    return arti_scouter_client.run(context_text, config)


def _load_transcript_text(transcript_path: Path, max_lines: int = 500) -> str:
    if not transcript_path.is_file():
        return ""
    lines_out = []
    for line in transcript_path.read_text(encoding="utf-8").splitlines()[-max_lines:]:
        try:
            row = json.loads(line)
            lines_out.append(
                f"[{row.get('ts','')}] {row.get('kind','')}"
                f"{(' '+row.get('name','')) if row.get('name') else ''}: {str(row.get('text',''))[:300]}"
            )
        except json.JSONDecodeError:
            continue
    return "\n".join(lines_out)


def _load_error_snippets(debug_log_path: Path | None, max_lines: int = 80) -> str:
    if not debug_log_path or not debug_log_path.is_file():
        return ""
    patterns = ("ERROR", "Error]", "Rate limit", "different loop", "Supertone", "Robot mode")
    hits = [ln.strip() for ln in debug_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if any(p in ln for p in patterns)]
    return "\n".join(hits[-max_lines:])


def _path_or_none(p: str | Path | None) -> Path | None:
    if p is None:
        return None
    return p if isinstance(p, Path) else Path(p)


_REFLECTION_START_MARKERS = (
    "**Analisis Bahasa Indonesia**",
    "## Analisis Bahasa Indonesia",
    "Analisis Bahasa Indonesia",
    "Dalam sesi stream",
)

_REFLECTION_PREAMBLE_PHRASES = (
    "we need to produce",
    "we need learnings",
    "extract from transcript",
    "make sure json",
    "let's craft",
    "now produce answer",
    "viewer_updates:",
    "tech_debt:",
)


def strip_reflection_reasoning_preamble(text: str) -> str:
    """Buang blok planning/reasoning Inggris sebelum analisis Indonesia."""
    if not text or not text.strip():
        return text

    for marker in _REFLECTION_START_MARKERS:
        idx = text.find(marker)
        if idx > 0:
            return text[idx:].lstrip()

    lines = text.splitlines()
    kept: list[str] = []
    started = False
    for line in lines:
        low = line.lower().strip()
        if not started:
            if any(p in low for p in _REFLECTION_PREAMBLE_PHRASES):
                continue
            if re.search(
                r"\b(yang|dengan|dalam|sesi|stream|arti|viewer|streamer)\b",
                line,
                re.I,
            ):
                started = True
            elif low.startswith("**") and "indonesia" in low:
                started = True
            elif low.startswith("```json"):
                started = True
        if started:
            kept.append(line)

    if kept:
        return "\n".join(kept).lstrip()
    return text.strip()


def run_post_stream_reflection(
    config: dict,
    session_id: str,
    transcript_path: str | Path,
    debug_log_path: str | Path | None = None,
    vault_md_path: str | Path | None = None,
) -> bool:
    """Post-stream reflection; updates vault MD, learnings, ARTI_VIEWERS."""
    if not config.get("reflection_enabled", True):
        return False

    key = config.get("openrouter_api_key") or ""
    if not key:
        print("[Reflection] OPENROUTER_API_KEY kosong, skip.")
        return False

    tx_path = _path_or_none(transcript_path)
    vault_path = _path_or_none(vault_md_path)
    dbg_path = _path_or_none(debug_log_path)
    if not tx_path or not tx_path.is_file():
        print("[Reflection] Transcript tidak ditemukan, skip.")
        return False

    tx = _load_transcript_text(tx_path)
    if len(tx.splitlines()) < 3:
        print("[Reflection] Transcript terlalu pendek, skip.")
        return False

    errors = _load_error_snippets(dbg_path)
    rag_block = ""
    try:
        import arti_vault_rag

        rag_block = arti_vault_rag.get_rag_context_for_reflection(session_id, tx, config)
        if rag_block:
            print(f"[Reflection] Vault RAG inject ({len(rag_block)} chars)")
    except Exception as e:
        print(f"[Reflection] Vault RAG skip: {e}")

    chain = [
        config.get("openrouter_reflection_model", "nvidia/nemotron-3-super-120b-a12b:free"),
        config.get("openrouter_reflection_fallback_model", "poolside/laguna-m.1:free"),
        config.get("openrouter_reflection_last_resort", "owl-alpha"),
    ]
    if config.get("reflection_try_ultra"):
        chain.insert(1, config.get("openrouter_reflection_ultra_model", "nvidia/nemotron-3-ultra-550b-a55b:free"))

    prompt = f"""Kamu analis post-stream untuk VTuber AI "Arti".
SESSION: {session_id}

TRANSCRIPT:
{tx}

LOG ERROR (cuplikan, jangan mengarang di luar ini):
{errors or '(tidak ada)'}
{rag_block}

Tulis analisis bahasa Indonesia, lalu JSON terpisah di akhir:
{{
  "learnings": ["pelajaran perilaku atau fakta stream"],
  "viewer_updates": [{{"name": "nick", "notes": "1 baris"}}],
  "tech_debt": ["masalah teknis dari log jika ada"]
}}"""

    timeout = int(config.get("openrouter_reflection_timeout_sec", 120))
    report = None
    parsed = None
    for model in chain:
        if not model:
            continue
        stream_preview = config.get("reflection_stream_preview", True)
        print(f"[Reflection] Trying {model}...")
        if stream_preview:
            print("[Reflection] Preview (streaming):")
        report = openrouter_chat(
            key,
            model,
            [{"role": "user", "content": prompt}],
            max_tokens=2000,
            timeout=timeout,
            title="Arti Reflection",
            stream=stream_preview,
            stream_preview_prefix="  ",
        )
        if report:
            report = strip_reflection_reasoning_preamble(report)
            parsed = _parse_json_blob(report)
            if parsed:
                break

    if not report:
        print("[Reflection] Semua model gagal.")
        return False

    reflection_path = _ROOT / "vault" / "sessions" / f"{session_id}_reflection.md"
    reflection_path.write_text(
        f"# Reflection: {session_id}\n\n{report}\n",
        encoding="utf-8",
    )

    if vault_path and vault_path.is_file():
        body = vault_path.read_text(encoding="utf-8")
        if "## Reflection" not in body:
            body += f"\n\n## Reflection\n\nLihat juga: `vault/sessions/{session_id}_reflection.md`\n\n{report[:4000]}\n"
            vault_path.write_text(body, encoding="utf-8")

    if parsed:
        _apply_reflection_outputs(parsed, config)

    print(f"[Reflection] Saved -> {reflection_path.name}")
    return True


def _apply_reflection_outputs(data: dict, config: dict) -> None:
    profile = config.get("active_profile", "default").lower()
    suffix = "" if profile == "default" else f"_{profile}"
    learnings_path = _ROOT / "vault" / "concepts" / f"arti_live_learnings{suffix}.md"
    date_str = time.strftime("%Y-%m-%d")

    for item in data.get("learnings") or []:
        if item and isinstance(item, str):
            _append_learning(learnings_path, f"Reflection: {item.strip()}")

    viewers_path = _ROOT / "ARTI_VIEWERS.md"
    for vu in data.get("viewer_updates") or []:
        if not isinstance(vu, dict):
            continue
        name = (vu.get("name") or "").strip().lstrip("@")
        notes = (vu.get("notes") or "").strip()
        if name and notes:
            _upsert_viewer_section(viewers_path, name, notes, date_str)


def _append_learning(path: Path, fact: str) -> None:
    import arti_memory_quality

    arti_memory_quality.append_learning(path, fact)


def _upsert_viewer_section(path: Path, name: str, notes: str, date_str: str) -> None:
    import arti_timeline_guard

    header = f"### {name}"
    earliest = arti_timeline_guard.find_earliest_viewer_mention(name)
    first_meet = earliest or date_str
    if earliest and earliest < date_str:
        print(f"[Reflection] Viewer {name}: pertemuan pertama vault {earliest} (bukan {date_str})")
    block = (
        f"{header}\n"
        f"- **Channel:** YouTube\n"
        f"- **Pertemuan pertama:** {first_meet}\n"
        f"- **Interaksi:** {notes}\n"
        f"- **Catatan:** (auto reflection {date_str})\n"
    )
    if not path.is_file():
        path.write_text("# ARTI_VIEWERS.md\n\n## VIEWER REGULAR\n\n" + block + "\n", encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    if header.lower() in text.lower():
        return
    if "## VIEWER REGULAR" in text:
        text = text.replace("## VIEWER REGULAR", "## VIEWER REGULAR\n\n" + block, 1)
    else:
        text += "\n## VIEWER REGULAR\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")
    print(f"[Reflection] Viewer note: {name}")
