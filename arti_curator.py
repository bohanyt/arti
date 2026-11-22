"""Curator — verify beats, promote to vault + learnings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import arti_timeline_guard as tg

_ROOT = Path(__file__).resolve().parent


@dataclass
class CuratorResult:
    beats: list[Any] = field(default_factory=list)
    approved_count: int = 0
    rejected_count: int = 0
    learnings_written: int = 0


def _load_origin_block() -> str:
    path = _ROOT / "vault" / "concepts" / "arti_origin.md"
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")[:2000]
    return ""


def _canon_conflicts(facts: list[Any], config: dict) -> list[str]:
    conflicts: list[str] = []
    debut = (config.get("arti_debut_label") or "debut date").lower()
    debut_date = (config.get("arti_debut_date") or "YYYY-MM-DD").lower()
    origin = _load_origin_block().lower()
    for raw in facts:
        text = raw.get("text", raw) if isinstance(raw, dict) else str(raw)
        tl = text.lower()
        if "baru mulai live" in tl or "baru saja memulai" in tl:
            conflicts.append(text[:120])
        if "debut" in tl and debut not in tl and debut_date not in tl:
            if "arti" in tl:
                conflicts.append(text[:120])
    return conflicts


# --- Summary quality gate (brain batch B, 2026-10-21) -----------------------
#
# The old rule rejected EVERY beat with noise_level == "high". The observer
# schema never defines "noise" (just "low|high"), and the observer marks whole
# debugging / quiet / ASR-echo segments "high" while still saying
# worth_embed: true. Audit (BRAIN_AUDIT.md §2.2): 234 of 820 beats were thrown
# away that way; on 2 Sep only 1 of 20 survived. Those summaries are normal
# prose about what happened in the segment.
#
# What the noise rule really kept out is junk, so junk is now named:
#   * HARD junk (every beat): raw model output, prompt fragments, placeholders.
#     The JSON-parse fallback in arti_observer_client stores raw model text as
#     the summary ("We need to output JSON only, with fields: summary...").
#   * SOFT junk (only the newly admitted noisy beats): too short to be a
#     segment summary, or mostly filler words (the renungan-label failure:
#     "gitu, maksudku, pusing, santai, sendiri").
# Noise-low beats keep their old path; only hard junk is new for them.

_PLACEHOLDER_RE = re.compile(r"^\s*\((?:kosong|observer gagal|no summary)", re.IGNORECASE)

_PROMPT_FRAGMENT_MARKERS = (
    "we need to output",
    "output only json",
    "output json",
    "output hanya json",
    "json valid",
    "with fields",
    "kamu observer",
    "segmen transcript",
    "ringkas segmen",
    "2-3 kalimat id",
    '"summary"',
    '"topics"',
    "noise_level",
    "worth_embed",
    "worth_learning",
    "<think",
    "</think",
)

_FILLER_WORDS = frozenset({
    "gitu", "gini", "maksudku", "maksudnya", "kayak", "kaya", "aja", "saja", "sih",
    "deh", "dong", "kok", "nih", "tuh", "ya", "yah", "yaa", "kan", "lho", "loh",
    "hmm", "hm", "eh", "ah", "oh", "wah", "wkwk", "wkwkwk", "haha", "hahaha", "hehe",
    "banget", "emang", "terus", "trus", "pokoknya", "santai", "pusing", "sendiri",
    "gimana", "apa", "itu", "ini", "aku", "kamu", "dia", "juga", "lagi", "udah",
    "sudah", "belum", "bisa", "mau", "ada", "nggak", "ngga", "gak", "ga", "tidak",
    "yang", "dan", "di", "ke", "dari", "jadi", "tapi", "kalau", "kalo", "biar",
    "sama", "buat", "untuk", "dengan", "atau", "hal", "sesuatu", "begitu", "begini",
})

_MIN_SUMMARY_WORDS = 8
_MIN_CONTENT_WORDS = 5
_MIN_CONTENT_RATIO = 0.4


def hard_junk_reason(summary: str) -> str:
    """'' unless the summary is empty, a placeholder, raw model output or a prompt fragment."""
    s = (summary or "").strip()
    if not s:
        return "empty_summary"
    if _PLACEHOLDER_RE.match(s):
        return "placeholder"
    if s[:1] in "{[`<":
        return "raw_model_output"
    low = s.lower()
    if any(m in low for m in _PROMPT_FRAGMENT_MARKERS):
        return "prompt_fragment"
    return ""


def soft_junk_reason(summary: str) -> str:
    """'' if the summary reads like a real segment summary, not filler."""
    words = re.findall(r"[^\W_]+", (summary or "").lower())
    if len(words) < _MIN_SUMMARY_WORDS:
        return "too_short"
    content = [w for w in words if w not in _FILLER_WORDS and (len(w) > 2 or w.isdigit())]
    if len(set(content)) < _MIN_CONTENT_WORDS or len(content) / len(words) < _MIN_CONTENT_RATIO:
        return "filler"
    return ""


def decide(beat: Any, config: dict) -> tuple[str, str]:
    """Pure decision: ("approved" | "rejected", reason). No I/O, no learnings."""
    noise = getattr(beat, "noise_level", "low") or "low"
    summary = getattr(beat, "summary", "") or ""
    worth_embed = bool(getattr(beat, "worth_embed", False))
    facts = list(getattr(beat, "facts", []) or [])

    if not summary.strip():
        return "rejected", "empty_summary"
    # The ONLY loosened rule: noise "high" no longer rejects a beat that the
    # observer itself marked worth_embed. Other noisy beats stay out.
    if noise == "high" and not worth_embed:
        return "rejected", "noise_high"
    junk = hard_junk_reason(summary)
    if not junk and noise == "high":
        junk = soft_junk_reason(summary)
    if junk:
        return "rejected", junk
    if _canon_conflicts(facts, config):
        return "rejected", "canon_conflict"
    if noise == "high":
        return "approved", "worth_embed_noisy"
    if worth_embed:
        return "approved", "worth_embed"
    if getattr(beat, "worth_learning", False):
        return "approved", "worth_learning"
    min_conf = float(config.get("observer_promote_min_confidence", 0.6))
    if any(isinstance(f, dict) and float(f.get("confidence", 0)) >= min_conf for f in facts):
        return "approved", "confident_facts"
    return "rejected", "not_worth"


def learnings_path(config: dict) -> Path:
    profile = config.get("active_profile", "default").lower()
    suffix = "" if profile == "default" else f"_{profile}"
    return _ROOT / "vault" / "concepts" / f"arti_live_learnings{suffix}.md"


def promote_learnings(beat: Any, config: dict) -> int:
    """Send an approved beat's facts to the learnings file (same gate as before).

    Noisy beats admitted by worth_embed feed the timeline / RAG only, never the
    60-bullet learnings FIFO: noise-high beats never wrote learnings before
    either, and the owner does not want junk in long-term memory.
    """
    if getattr(beat, "curator_status", "") != "approved":
        return 0
    if not getattr(beat, "worth_learning", False):
        return 0
    if (getattr(beat, "noise_level", "low") or "low") == "high":
        return 0
    written = 0
    for raw in list(getattr(beat, "facts", []) or []):
        text = raw.get("text", raw) if isinstance(raw, dict) else str(raw)
        wrapped = text if str(text).lower().startswith("stream fact:") else f"Stream fact: {text}"
        if tg.should_persist_memory_fact(wrapped):
            try:
                import arti_memory_quality as mq

                mq.append_learning(learnings_path(config), wrapped, config=config)
                written += 1
            except Exception as e:
                print(f"[Curator] learning skip: {e}")
    return written


def curate_beats(beats: list[Any], config: dict) -> CuratorResult:
    """Set curator_status (+ curator_reason), filter junk, gate learnings."""
    result = CuratorResult()

    for beat in beats:
        facts = list(getattr(beat, "facts", []) or [])
        beat.canon_conflicts = _canon_conflicts(facts, config)
        status, reason = decide(beat, config)
        beat.curator_status = status
        beat.curator_reason = reason
        if status == "approved":
            result.approved_count += 1
            result.learnings_written += promote_learnings(beat, config)
        else:
            result.rejected_count += 1
        result.beats.append(beat)

    return result


def append_timeline_to_session_md(session_id: str, beats: list[Any]) -> None:
    """Add ## Timeline beats to vault/sessions/{id}.md."""
    path = _ROOT / "vault" / "sessions" / f"{session_id}.md"
    if not path.is_file():
        return
    lines = ["\n## Timeline beats\n"]
    for b in beats:
        if getattr(b, "curator_status", "") != "approved":
            continue
        lines.append(f"### {b.t_start} – {b.t_end}")
        lines.append(getattr(b, "summary", ""))
        lines.append("")
    text = path.read_text(encoding="utf-8")
    if "## Timeline beats" in text:
        text = text.split("## Timeline beats")[0].rstrip() + "\n"
    path.write_text(text + "\n".join(lines), encoding="utf-8")
