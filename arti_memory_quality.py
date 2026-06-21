"""Vault learning quality gate + model output sanitization."""
from __future__ import annotations

import re
import time
from pathlib import Path

_THINK_TAGS = (
    "think",
    "redacted_reasoning",
    "redacted_thinking",
)
_THINKING_RE = re.compile(
    "(?:" + "|".join(rf"<{t}>.*?</{t}>" for t in _THINK_TAGS) + ")",
    re.DOTALL | re.IGNORECASE,
)
_NOISE_RE = re.compile(r"/no_think", re.IGNORECASE)

_SKIP_SUBSTRINGS = (
    "tidak ditemukan",
    "jawaban bohan tadi",
    "live stream baru saja dimulai",
    "live stream baru dimulai pada",
    "live stream dimulai pada",
    "live stream dimulai sekitar",
    "live stream baru dimulai sekitar",
    "vtuber baru saja memulai live stream",
    "arti baru saja memulai live stream",
    "arti baru saja mulai streaming",
    "arti baru saja mulai live",
    "baru saja memulai live stream",
    "baru mulai live stream",
    "baru mulai streaming",
    "memulai live stream",
    "arti berulang kali memastikan stream sudah menyala",
    "bohan bertanya tentang nasi goreng berulang",
    "streamer berulang kali mengucapkan terima kasih",
    "tidak ada catatan baru",
    "stream fact: none",
    "penonton baru datang dan belum punya konteks waktu",
    "arti debut co-host",
    "debut co-host 27 mei",
    "asal usul arti",
    "nama arti diberikan",
)

_MIN_LEARNING_LEN = 12
_MAX_LEARNING_BULLETS = 60

# History-log echo patterns in live LLM replies (must not reach TTS).
_HISTORY_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
_HISTORY_LABEL_RE = re.compile(
    r"\[(?:Streamer|Arti\s*\(VTuber\)|Viewer\s*@[^\]]+)\]",
    re.IGNORECASE,
)
_HISTORY_SECTION_RE = re.compile(
    r"===\s*(?:OMONGAN STREAMER|CHAT VIEWER|JAWABAN ARTI)",
    re.IGNORECASE,
)
_HISTORY_MANGLED_RE = re.compile(
    r"Streamer\s*\[\d{1,2}:\d{2}(?::\d{2})?\]",
    re.IGNORECASE,
)
_ARTI_QUOTE_LABEL_RE = re.compile(r'["\']?\s*Arti\s*:\s*["\']?', re.IGNORECASE)

_STUTTER_WORD_RE = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)


def strip_history_echo(text: str) -> str:
    """Remove leaked stream log format from live voice replies; truncate at first leak."""
    if not text:
        return ""
    patterns = (
        _HISTORY_TIMESTAMP_RE,
        _HISTORY_LABEL_RE,
        _HISTORY_SECTION_RE,
        _HISTORY_MANGLED_RE,
        _ARTI_QUOTE_LABEL_RE,
    )
    cut_at = len(text)
    for pat in patterns:
        m = pat.search(text)
        if m and m.start() < cut_at:
            cut_at = m.start()
    out = text[:cut_at].strip()
    out = re.sub(r'["\']+\s*$', "", out).strip()
    out = re.sub(r"\s+", " ", out)
    return out


def normalize_stutter_words(text: str) -> str:
    """Collapse immediate word repeats (e.g. 'diberikan diberikan')."""
    if not text:
        return ""
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _STUTTER_WORD_RE.sub(r"\1", out)
    return out


def sanitize_model_text(text: str) -> str:
    """Strip thinking blocks and /no_think residue from LLM output."""
    if not text:
        return ""
    out = _THINKING_RE.sub("", text)
    out = _NOISE_RE.sub("", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _normalize_fact(fact: str) -> str:
    t = fact.strip().lower()
    t = re.sub(r"^stream fact:\s*", "", t)
    t = re.sub(r"^reflection:\s*", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def is_duplicate_learning(fact: str, existing_lines: list[str]) -> bool:
    norm = _normalize_fact(fact)
    if len(norm) < 4:
        return True
    for line in existing_lines:
        m = re.match(r"^-\s*\[\d{4}-\d{2}-\d{2}\]\s*(.+)$", line.strip())
        if not m:
            continue
        prev = _normalize_fact(m.group(1))
        if norm == prev:
            return True
        if len(norm) > 20 and (norm in prev or prev in norm):
            return True
    return False


def should_save_learning(fact: str, existing_lines: list[str] | None = None) -> bool:
    t = normalize_stutter_words((fact or "").strip())
    if len(t) < _MIN_LEARNING_LEN:
        return False
    low = t.lower()
    if any(s in low for s in _SKIP_SUBSTRINGS):
        return False
    if existing_lines and is_duplicate_learning(t, existing_lines):
        return False
    return True


def list_learning_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.strip().startswith("## Memori Jangka Panjang"):
            in_section = True
            continue
        if in_section and line.strip().startswith("##"):
            break
        if in_section and line.strip().startswith("-"):
            bullets.append(line.strip())
    return bullets


def append_learning(
    path: Path,
    fact: str,
    *,
    max_bullets: int = _MAX_LEARNING_BULLETS,
) -> bool:
    """Append learning at end of section; cap total bullets (drop oldest)."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    existing = list_learning_bullets(text)
    if not should_save_learning(fact, existing):
        print(f"[Memory] Skip learning (quality gate): {fact[:72]}...")
        return False

    line = f"- [{time.strftime('%Y-%m-%d')}] {fact.strip()}"
    if "## Memori Jangka Panjang" not in text:
        text += f"\n\n## Memori Jangka Panjang\n\n{line}\n"
    else:
        parts = text.split("## Memori Jangka Panjang", 1)
        header = parts[0] + "## Memori Jangka Panjang\n\n"
        body = parts[1]
        bullets = existing + [line]
        if len(bullets) > max_bullets:
            bullets = bullets[-max_bullets:]
        body_rest = body
        for _ in bullets:
            body_rest = re.sub(r"^-\s*\[[^\]]+\].*\n?", "", body_rest, count=1, flags=re.MULTILINE)
        new_body = "\n".join(bullets) + "\n" + body_rest.lstrip("\n")
        text = header + new_body

    path.write_text(text, encoding="utf-8")
    print(f"[Memory] Learning saved: {line[:80]}...")
    return True


def filter_memories_for_startup(memories: list[str], today: str | None = None) -> list[str]:
    """Return bullets from today only (for startup snippet)."""
    today = today or time.strftime("%Y-%m-%d")
    prefix = f"[{today}]"
    return [m for m in memories if prefix in m]
