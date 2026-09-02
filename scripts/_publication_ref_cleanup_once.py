#!/usr/bin/env python3
"""One-shot exact-string cleanup for public-only dead development references.

This helper is temporary and must be deleted from the PR after it runs.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REPLACEMENTS = {
    "arti_cursor_agent.py": [
        (
            "FAKTA TERUKUR (spike Tahap 0, 2026-07-31 — lihat docs/CURSOR-SDK-SPIKE.md)",
            "FAKTA TERUKUR (historical private benchmark; retained here only as tuning context)",
        ),
        (
            "tests/test_bridge_startup_bugfix.py:781 mem-parse AST body `main_loop` dan mengunci\n    urutan pemanggilannya.",
            "a private regression test mem-parse AST body `main_loop` dan mengunci\n    urutan pemanggilannya.",
        ),
    ],
    "arti_voice_dsp.py": [
        (
            "Lahir 16 Agu 2026 dari sesi uji dengar streamer (dump/uji_pitch, skrip\nscripts/uji_pitch_suara.py + uji_range_suara.py). Latar: suara F1 Supertone",
            "Lahir dari rangkaian uji dengar dan kalibrasi internal. Latar: suara F1 Supertone",
        ),
    ],
    "arti_vault_rag.py": [
        ("docs/handoff/**/*.md", "developer handoff documents"),
        ("docs/handoff dikeluarkan", "developer handoff documents dikeluarkan"),
    ],
    "hermes_vtuber_bridge.py": [
        (
            "private development notes removed]-codex-chatgpt-plus.md) -> baru Groq.",
            "historical private benchmark notes) -> baru Groq.",
        ),
        ("docs/CURSOR-SDK-SPIKE.md", "historical private Cursor benchmark"),
        ("scripts/spike_grok_vision.py", "historical vision provider probe"),
        ("scripts/buat_motion_bersih.py", "private motion-cleanup helper"),
        ("scripts/buat_motion_tanpa_kepala.py", "private motion-cleanup helper"),
        ("scripts/rangkum_susulan.py", "optional private catch-up helper"),
    ],
}


def main() -> int:
    changed = []
    for rel, replacements in REPLACEMENTS.items():
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        original = text
        for old, new in replacements:
            count = text.count(old)
            if count == 0:
                raise SystemExit(f"expected text not found in {rel}: {old!r}")
            text = text.replace(old, new)
        if text != original:
            path.write_text(text, encoding="utf-8", newline="\n")
            changed.append(rel)
    print("updated:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
