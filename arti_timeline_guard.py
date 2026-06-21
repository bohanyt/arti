"""Timeline / debut fact guards — reduce date hallucination."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent

TIMELINE_QUESTION_RE = re.compile(
    r"\b(sejak kapan|kapan mulai|kapan pertama|udah berapa lama|berapa lama|"
    r"kapan ada|tanggal berapa|hari apa|debut|pertama kali|"
    r"ingat nggak|masih ingat|dari kapan)\b",
    re.IGNORECASE,
)

_ARTI_TIMELINE_RE = re.compile(
    r"\barti\b.*\b(mulai|ada|lahir|debut|ingat)\b|\b(mulai|ada)\b.*\barti\b",
    re.IGNORECASE,
)


def is_timeline_question(text: str) -> bool:
    q = (text or "").strip()
    if not q:
        return False
    if TIMELINE_QUESTION_RE.search(q):
        return True
    return bool(_ARTI_TIMELINE_RE.search(q))


def build_timeline_guard_block(config: dict | None = None) -> str:
    """Hard inject for LLM — debut date is not negotiable."""
    cfg = config or {}
    label = cfg.get("arti_debut_label", "27 Mei 2026")
    iso = cfg.get("arti_debut_date", "2026-05-27")
    return (
        f"\n\n[GUARD TIMELINE — WAJIB IKUTI]\n"
        f"Fakta kanon: co-host debut live = {label} ({iso}).\n"
        f"JANGAN jawab tanggal sesi hari ini, tanggal reflection, atau 'baru mulai stream' sebagai tanggal lahir Arti.\n"
        f"Untuk detail sesi lama, pakai cuplikan vault jika ada; tanggal debut tetap {label}."
    )


def append_timeline_guard(system_prompt: str, config: dict | None = None) -> str:
    if not (config or {}).get("timeline_guard_enabled", True):
        return system_prompt
    return system_prompt + build_timeline_guard_block(config)


def should_persist_memory_fact(fact: str, existing_lines: list[str] | None = None) -> bool:
    """False = skip misleading session-relative facts."""
    import arti_memory_quality

    return arti_memory_quality.should_save_learning((fact or "").strip(), existing_lines)


def filter_scouter_facts(facts: list[Any]) -> list[str]:
    out: list[str] = []
    for raw in facts or []:
        s = str(raw).strip()
        if not s:
            continue
        wrapped = s if s.lower().startswith("stream fact:") else f"Stream fact: {s}"
        if should_persist_memory_fact(wrapped):
            out.append(s)
        else:
            print(f"[Scouter] Skip misleading fact: {s[:72]}...")
    return out


def find_earliest_viewer_mention(name: str, vault_root: Path | None = None) -> str | None:
    """Earliest YYYY-MM-DD from vault/sessions/*.md mentioning viewer (case-insensitive)."""
    root = vault_root or _ROOT
    sessions = root / "vault" / "sessions"
    if not sessions.is_dir():
        return None
    needle = name.strip().lower().lstrip("@")
    if not needle:
        return None
    earliest: str | None = None
    for path in sessions.glob("*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
        if not m:
            continue
        day = m.group(1)
        try:
            body = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if needle in body or f"@{needle}" in body:
            if earliest is None or day < earliest:
                earliest = day
    return earliest
