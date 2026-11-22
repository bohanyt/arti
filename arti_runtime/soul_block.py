"""Shared startup soul loader and filtered persona block (ADR 119)."""
from __future__ import annotations

import re
from pathlib import Path


def load_soul_context(root: Path) -> str:
    """Shared bridge startup loader; callers choose the provisioned runtime root.

    No config, viewer, mood or vault access. Fail closed without logging content.
    """
    from arti_live_safety import is_backstage

    try:
        content = (Path(root) / "ARTI_SOUL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    sections, current_section, section = [], [], ""
    for line in content.split("\n"):
        if line.startswith("## "):
            section = line.upper()
        if section.startswith(("## OPINI", "## KEINGINAN", "## RUNNING BITS")) and is_backstage(line):
            continue
        if line.startswith("## "):
            if current_section:
                sections.append("\n".join(current_section))
            current_section = [line]
        elif line.strip() and not line.startswith("#"):
            current_section.append(line)
    if current_section:
        sections.append("\n".join(current_section))
    return "\n\n".join(sections)

SOUL_MARKER = "\n\n[JIWA ARTI]"
_SOUL_HEADER = (
    "Jika ada konflik, aturan di atas tetap menang. "
    "Orang yang disebut di sini belum tentu hadir sekarang."
)

# Owner-approved conflicts only. Keep the loader's backstage filtering as well.
SOUL_DROP_LINE_PATTERNS = (
    re.compile(r"(?=.*\bAnda\b)(?=.*\bKamu\b)", re.IGNORECASE),
    re.compile(r"\bJANGAN\s+jawab\s+lebih\s+dari\s+\d+\s+kalimat\b", re.IGNORECASE),
    re.compile(r"katasaring", re.IGNORECASE),
)


def build_soul_block(soul_text: str, config: dict) -> tuple[str, int]:
    """Return the prompt block and dropped-line count, never logging its text."""
    if not config.get("soul_prompt_enabled", True):
        return "", 0
    kept = []
    dropped = 0
    for line in soul_text.splitlines(keepends=True):
        if any(pattern.search(line) for pattern in SOUL_DROP_LINE_PATTERNS):
            dropped += 1
        else:
            kept.append(line)
    text = "".join(kept)
    if not text.strip():
        return "", dropped
    return SOUL_MARKER + "\n" + _SOUL_HEADER + "\n" + text, dropped


def assemble_startup_prompt(base: str, origin_block: str, self_block: str,
                            soul_block: str, memory_block: str) -> str:
    """One ordering for live and test mode; an empty soul preserves legacy bytes."""
    return base + origin_block + self_block + soul_block + memory_block
