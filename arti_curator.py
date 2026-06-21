"""Curator — verify beats, promote to vault + learnings."""

from __future__ import annotations

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
    debut = (config.get("arti_debut_label") or "27 Mei 2026").lower()
    origin = _load_origin_block().lower()
    for raw in facts:
        text = raw.get("text", raw) if isinstance(raw, dict) else str(raw)
        tl = text.lower()
        if "baru mulai live" in tl or "baru saja memulai" in tl:
            conflicts.append(text[:120])
        if "debut" in tl and debut not in tl and "27 mei" not in tl and "2026-05-27" not in tl:
            if "arti" in tl:
                conflicts.append(text[:120])
    return conflicts


def curate_beats(beats: list[Any], config: dict) -> CuratorResult:
    """Set curator_status, filter noise, gate learnings."""
    result = CuratorResult()
    min_conf = float(config.get("observer_promote_min_confidence", 0.6))

    for beat in beats:
        noise = getattr(beat, "noise_level", "low") or "low"
        facts = list(getattr(beat, "facts", []) or [])
        conflicts = _canon_conflicts(facts, config)
        beat.canon_conflicts = conflicts

        if noise == "high" or not getattr(beat, "summary", "").strip():
            beat.curator_status = "rejected"
            result.rejected_count += 1
        elif conflicts:
            beat.curator_status = "rejected"
            result.rejected_count += 1
        elif getattr(beat, "worth_embed", False) or getattr(beat, "worth_learning", False):
            beat.curator_status = "approved"
            result.approved_count += 1
        else:
            conf_facts = [
                f for f in facts if isinstance(f, dict) and float(f.get("confidence", 0)) >= min_conf
            ]
            if conf_facts:
                beat.curator_status = "approved"
                result.approved_count += 1
            else:
                beat.curator_status = "rejected"
                result.rejected_count += 1

        if beat.curator_status == "approved" and getattr(beat, "worth_learning", False):
            for raw in facts:
                text = raw.get("text", raw) if isinstance(raw, dict) else str(raw)
                wrapped = text if str(text).lower().startswith("stream fact:") else f"Stream fact: {text}"
                if tg.should_persist_memory_fact(wrapped):
                    try:
                        import arti_memory_quality as mq

                        profile = config.get("active_profile", "default").lower()
                        suffix = "" if profile == "default" else f"_{profile}"
                        vault_path = _ROOT / "vault" / "concepts" / f"arti_live_learnings{suffix}.md"
                        mq.append_learning(vault_path, wrapped)
                        result.learnings_written += 1
                    except Exception as e:
                        print(f"[Curator] learning skip: {e}")

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
