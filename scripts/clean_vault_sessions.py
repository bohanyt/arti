#!/usr/bin/env python3
"""Bersihkan berkas sesi vault sebelum di-index: A3b, A4, A5, A6.

Empat operasi, semuanya menghapus/memperbaiki — tidak ada yang menambah isi:

A3b  Buang segmen `rejected` yang terlanjur tertulis di `*_beats.md`.
     `write_beats_md` sudah diperbaiki supaya tidak menulis yang baru, tapi 42 segmen
     lama masih ada. Isinya sering keluaran model mentah termasuk potongan prompt sistem
     ("We need to output JSON only, with fields: summary..."). Rekam lengkapnya tetap
     aman di `*_beats.jsonl` yang tidak di-index.

A4   Buang seksi `## Konteks Vault (RAG lite)`.
     Seksi ini menuliskan HASIL retrieval RAG kembali ke berkas yang lalu di-embed lagi —
     kontaminasi berputar. Isinya bahkan memuat potongan instruksi prompt yang bocor,
     lengkap dengan meta-instruksi "jangan bilang 'baca database/RAG'".

A5   Dedup baris "Stream fact:" DI DALAM satu berkas.
     `2026-05-31-default.md` punya 393 baris untuk 27 fakta unik. Dedup RAG hanya bekerja
     per-berkas pada 180 char pertama, jadi pengulangan penuh begini tetap lolos.
     CATATAN: sengaja TIDAK menghapus duplikasi LINTAS berkas (mis. berkas sesi yang
     memuat salinan learnings). Setelah A2 memangkas learnings 60 -> 14, salinan lama di
     berkas sesi justru satu-satunya rekam historis fakta yang dipangkas — membuangnya
     akan menghilangkan sejarah, bukan sekadar duplikat.

A6   Perbaiki mojibake per-baris (UTF-8 yang terlanjur didekode sebagai cp1252).
     Hanya baris yang benar-benar terdeteksi rusak yang disentuh.

Pakai:
    python scripts/clean_vault_sessions.py            # dry-run
    python scripts/clean_vault_sessions.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SESSIONS = _ROOT / "vault" / "sessions"

RAG_LITE_HEADING = "## Konteks Vault (RAG lite)"
_REJECTED_RE = re.compile(r"^## .*?,\s*rejected\)\s*$", re.M)
_STREAM_FACT_RE = re.compile(r"^\s*-\s*(?:\[\d{4}-\d{2}-\d{2}\]\s*)?Stream fact:", re.I)
# Baris penyangkalan di daftar **Topik:** hasil A10b — "- (tidak ada pertanyaan dari
# penonton)", "- (tidak ada fakta baru tentang Arti)". Murni noise yang ikut di-embed.
_NO_TOPIC_RE = re.compile(r"^\s*-\s*\(?tidak ada\b", re.I)
_MOJI_MARKERS = ("â", "Ã", "", "")


# --- A6 ---------------------------------------------------------------------
def fix_mojibake_line(line: str) -> str:
    """Kembalikan baris yang diperbaiki, atau baris asli kalau perbaikan tidak yakin.

    Coba latin-1 DULU, baru cp1252. Alasannya terlihat dari data nyata: baris rusak
    berbentuk `2026â\\x80\\x9106â\\x80\\x9107` — yaitu byte UTF-8 (0xE2 0x80 0x91 =
    U+2011) yang didekode sebagai latin-1. cp1252 TIDAK cocok di sini karena ia
    memetakan 0x80 ke '€' dan 0x91 ke ''', bukan ke U+0080/U+0091.

    Ini sesuai dengan sumber bug-nya: `requests.iter_lines(decode_unicode=True)` memakai
    ISO-8859-1 (= latin-1), default HTTP untuk respons tanpa charset eksplisit.
    """
    if not any(m in line for m in _MOJI_MARKERS):
        return line
    for enc in ("latin-1", "cp1252"):
        try:
            fixed = line.encode(enc, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        # Terima hanya kalau penanda mojibake benar-benar hilang dan teksnya tidak
        # menyusut drastis (jaga-jaga dari salah tebak).
        if any(m in fixed for m in _MOJI_MARKERS):
            continue
        if len(fixed) < len(line) * 0.5:
            continue
        return fixed
    return line


# --- A3b --------------------------------------------------------------------
def strip_rejected_segments(text: str) -> tuple[str, int]:
    """Buang blok '## ... , rejected)' sampai heading '## ' berikutnya."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    removed = 0
    while i < len(lines):
        if _REJECTED_RE.match(lines[i].rstrip("\n")):
            removed += 1
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out), removed


# --- A4 ---------------------------------------------------------------------
def strip_rag_lite(text: str) -> tuple[str, int]:
    """Buang seksi '## Konteks Vault (RAG lite)' sampai heading '## ' berikutnya."""
    if RAG_LITE_HEADING not in text:
        return text, 0
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    removed = 0
    while i < len(lines):
        if lines[i].rstrip("\n").strip() == RAG_LITE_HEADING:
            removed += 1
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out), removed


# --- A5 ---------------------------------------------------------------------
def dedup_stream_facts(text: str) -> tuple[str, int]:
    """Buang baris 'Stream fact:' yang isinya sudah pernah muncul di berkas yang sama."""
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    for line in text.splitlines(keepends=True):
        if _STREAM_FACT_RE.match(line):
            key = re.sub(r"\s+", " ", re.sub(r"^\s*-\s*(?:\[[^\]]+\]\s*)?", "", line)).strip().lower()
            if key in seen:
                removed += 1
                continue
            seen.add(key)
        out.append(line)
    return "".join(out), removed


def strip_empty_topics(text: str) -> tuple[str, int]:
    """Buang baris '- (tidak ada ...)' dari daftar Topik."""
    out, removed = [], 0
    for line in text.splitlines(keepends=True):
        if _NO_TOPIC_RE.match(line):
            removed += 1
            continue
        out.append(line)
    return "".join(out), removed


def process(path: Path, apply: bool) -> dict:
    original = path.read_text(encoding="utf-8", errors="replace")
    text = original
    stats = {"rejected": 0, "rag_lite": 0, "dup_fact": 0, "moji": 0,
             "no_topic": 0, "kosong": False}

    if path.name.endswith("_beats.md"):
        text, stats["rejected"] = strip_rejected_segments(text)
    text, stats["rag_lite"] = strip_rag_lite(text)
    text, stats["dup_fact"] = dedup_stream_facts(text)
    text, stats["no_topic"] = strip_empty_topics(text)

    lines = text.splitlines(keepends=True)
    fixed_lines = []
    for ln in lines:
        f = fix_mojibake_line(ln)
        if f != ln:
            stats["moji"] += 1
        fixed_lines.append(f)
    text = "".join(fixed_lines)

    # Berkas beats yang jadi tanpa segmen sama sekali = nol nilai untuk RAG.
    if path.name.endswith("_beats.md") and not re.search(r"^## ", text, re.M):
        stats["kosong"] = True

    changed = text != original
    if changed and apply:
        shutil.copy2(path, path.with_suffix(f".md.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
        path.write_text(text, encoding="utf-8")
    stats["changed"] = changed
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Bersihkan berkas sesi vault sebelum index")
    ap.add_argument("--apply", action="store_true", help="tulis beneran (default: dry-run)")
    args = ap.parse_args()

    files = sorted(p for p in SESSIONS.glob("*.md") if ".bak" not in p.name)
    print("=" * 92)
    print(f"  BERSIH-BERSIH BERKAS SESI — {len(files)} berkas"
          f"{'  [DRY-RUN]' if not args.apply else '  [APPLY]'}")
    print("=" * 92)
    print(f"  {'berkas':<38} {'rejected':>8} {'RAG-lite':>8} {'dup fact':>8} "
          f"{'moji':>6} {'noTopik':>8}")
    print("  " + "-" * 88)

    tot = {"rejected": 0, "rag_lite": 0, "dup_fact": 0, "moji": 0, "no_topic": 0}
    empties: list[Path] = []
    touched = 0
    for p in files:
        s = process(p, args.apply)
        if not s["changed"]:
            continue
        touched += 1
        for k in tot:
            tot[k] += s[k]
        flag = "  <- jadi kosong" if s["kosong"] else ""
        if s["kosong"]:
            empties.append(p)
        print(f"  {p.name:<38} {s['rejected']:>8} {s['rag_lite']:>8} "
              f"{s['dup_fact']:>8} {s['moji']:>6} {s['no_topic']:>8}{flag}")

    print()
    print("=" * 92)
    print(f"  berkas berubah          : {touched}")
    print(f"  segmen rejected dibuang : {tot['rejected']}")
    print(f"  seksi RAG-lite dibuang  : {tot['rag_lite']}")
    print(f"  baris Stream fact dedup : {tot['dup_fact']}")
    print(f"  baris mojibake diperbaiki: {tot['moji']}")
    print(f"  baris '(tidak ada)' dibuang: {tot['no_topic']}")
    if empties:
        print(f"\n  {len(empties)} berkas beats jadi tanpa segmen sama sekali "
              f"(nol nilai untuk RAG):")
        for p in empties:
            print(f"    {p.name}")
        print("    -> pertimbangkan hapus manual; datanya tetap ada di *_beats.jsonl")
    if not args.apply:
        print("\n  [DRY-RUN] Tidak ada yang ditulis. Tambahkan --apply untuk menerapkan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
