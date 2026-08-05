#!/usr/bin/env python3
"""Vault hygiene: prune learnings + sanitize session summaries."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import arti_memory_quality as mq

LEARNINGS = _ROOT / "vault" / "concepts" / "arti_live_learnings.md"
SESSIONS_DIR = _ROOT / "vault" / "sessions"
ARCHIVE = _ROOT / "archive" / f"vault-prune-{datetime.now().strftime('%Y-%m-%d')}"


def default_session_paths() -> list[Path]:
    """Default *-default.md session files (newest first)."""
    return sorted(SESSIONS_DIR.glob("*-default.md"), reverse=True)


def session_paths_for_prune(all_sessions: bool) -> list[Path]:
    if all_sessions:
        return sorted(SESSIONS_DIR.glob("*.md"))
    # June 2026 + explicit recent defaults
    paths = list(SESSIONS_DIR.glob("2026-06-*.md"))
    paths.extend(default_session_paths()[:5])
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    return sorted(out, key=lambda x: x.name)


def prune_learnings() -> int:
    if not LEARNINGS.is_file():
        print(f"[prune] skip: {LEARNINGS} tidak ada")
        return 0
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEARNINGS, ARCHIVE / LEARNINGS.name)

    text = LEARNINGS.read_text(encoding="utf-8")
    bullets = mq.list_learning_bullets(text)
    kept: list[str] = []
    for line in bullets:
        m = re.match(r"^-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+)$", line.strip())
        if not m:
            continue
        date_str, body = m.group(1), m.group(2).strip()
        if not mq.should_save_learning(body, kept):
            continue
        kept.append(f"- [{date_str}] {body}")

    kept = kept[:60]
    kept.sort()

    if "## Memori Jangka Panjang" in text:
        parts = text.split("## Memori Jangka Panjang", 1)
        header = parts[0] + "## Memori Jangka Panjang\n\n"
        tail = parts[1]
        tail = re.sub(r"^-\s*\[[^\]]+\].*\n?", "", tail, flags=re.MULTILINE)
        tail = re.sub(r"^\n+", "", tail)
        new_text = header + "\n".join(kept) + ("\n\n" + tail if tail.strip() else "\n")
    else:
        new_text = text

    LEARNINGS.write_text(new_text, encoding="utf-8")
    print(f"[prune] learnings: {len(bullets)} -> {len(kept)} bullets")
    return len(kept)


def rebuild_from_sources() -> int:
    """Restore learnings: June facts + vault backup, then prune."""
    PRIORITY = [
        "- [2026-06-20] Stream fact: Streamer menyebut bisa menghubungkan Composer dengan MCP Live Browser",
        "- [2026-06-20] Stream fact: Arti menjelaskan bahwa ia memilih JMK karena suka dan nyaman, bukan karena tidak mampu mencari yang lain",
        "- [2026-06-17] Stream fact: Arti baru mulai streaming dan belum terbiasa dengan pengaturan ekspresi wajahnya",
        "- [2026-06-17] Stream fact: Streamer merasa ada masalah dengan gerakan mulut/ekspresi yang terlalu berlebihan",
        "- [2026-06-17] Stream fact: Streamer ingin Arti membuat ekspresi bingung dan marah saat streaming",
        "- [2026-06-17] Stream fact: Streamer sedang memperbaiki animasi model VTuber baru sebelum dipasang",
        "- [2026-06-16] Stream fact: Arti mendengar Bohan ingin membuat AI sendiri dan merasa terinspirasi",
        "- [2026-06-16] Stream fact: Bohan sedang melakukan tes teknis di platform INEA",
        "- [2026-06-16] Stream fact: Streamer merasa LM Studio membuat perangkat menjadi berat",
        "- [2026-06-16] Stream fact: Animasi Arti (VTuber) tidak ter-trigger dengan benar saat chat muncul",
        "- [2026-06-16] Stream fact: Viewer Dream_Grigha bertanya apakah Arti menyala dan apakah panas",
        "- [2026-06-14] Stream fact: Streamer membuat AI untuk membalas chat dengan data hingga 3 bulan lalu",
        "- [2026-06-14] Stream fact: tamubaru bertanya apakah Arti punya perasaan pada seseorang",
        "- [2026-06-11] Stream fact: Streamer menjelaskan bahwa VTuber memiliki sistem yang bisa mengingat beberapa chat sebelumnya untuk konteks percakapan",
    ]
    bak = _ROOT / "archive" / "hermes-vault-sessions" / "arti_live_learnings.hermes-vault.bak.md"
    merged: list[str] = []
    for src in (PRIORITY, mq.list_learning_bullets(bak.read_text(encoding="utf-8")) if bak.is_file() else []):
        for line in src:
            m = re.match(r"^-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+)$", line.strip())
            if not m:
                continue
            body = m.group(2).strip()
            if mq.should_save_learning(body, merged):
                merged.append(f"- [{m.group(1)}] {body}")
    merged.sort()
    merged = merged[-60:]
    header = (
        "# Arti Live Learnings (Default Profile)\n\n"
        "Ini adalah catatan pengetahuan jangka panjang yang dipelajari Arti (VTuber Co-Host) "
        "secara otomatis selama sesi live stream untuk profil **default**.\n\n"
        "## Memori Jangka Panjang\n\n"
    )
    LEARNINGS.write_text(header + "\n".join(merged) + "\n", encoding="utf-8")
    print(f"[rebuild] learnings restored: {len(merged)} bullets")
    return len(merged)


def sanitize_session(path: Path) -> bool:
    if not path.is_file():
        print(f"[sanitize] skip: {path.name} tidak ada")
        return False
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE / path.name
    if not dest.exists():
        shutil.copy2(path, dest)

    text = path.read_text(encoding="utf-8")
    marker = "## Ringkasan Sesi\n"
    if marker not in text:
        return False
    before, rest = text.split(marker, 1)
    next_h = rest.find("\n## ")
    if next_h == -1:
        summary, after = rest, ""
    else:
        summary, after = rest[:next_h], rest[next_h:]

    cleaned = mq.sanitize_model_text(summary)
    if cleaned == summary.strip():
        return False
    path.write_text(before + marker + cleaned + after, encoding="utf-8")
    print(
        f"[sanitize] {path.name}: thinking block dihapus "
        f"({len(summary)} -> {len(cleaned)} chars)"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune vault learnings + sanitize sessions")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild learnings from priority list")
    parser.add_argument(
        "--all-sessions",
        action="store_true",
        help="Sanitize all vault/sessions/*.md (default: 2026-06-* + recent defaults)",
    )
    parser.add_argument(
        "--sanitize-only",
        action="store_true",
        help="Only sanitize sessions, skip learnings prune",
    )
    args = parser.parse_args()

    if args.sanitize_only:
        n = 0
    elif args.rebuild:
        n = rebuild_from_sources()
    else:
        n = prune_learnings()

    targets = session_paths_for_prune(args.all_sessions)
    fixed = sum(1 for p in targets if sanitize_session(p))
    print(f"[done] learnings={n} bullets, sessions scanned={len(targets)}, sanitized={fixed}")
    print(f"[done] backup -> {ARCHIVE}")


if __name__ == "__main__":
    main()
