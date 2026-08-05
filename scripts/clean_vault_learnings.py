#!/usr/bin/env python3
"""Bersihkan memori jangka panjang Arti — gabung duplikat, buang noise, tulis ulang.

Kenapa bukan `scripts/prune_vault_memory.py` yang sudah ada:
  - `--rebuild` di sana MENIMPA learnings dengan 14 bullet hardcoded Juni 2026, TANPA
    backup, dari berkas `.bak` yang tidak ada di disk. `vault/` di-gitignore, jadi tidak
    ada jaring pengaman git. Itu kehilangan permanen.
  - Backup mode default-nya bisa menimpa dirinya sendiri kalau dijalankan 2x sehari
    (tidak ada guard `exists()`).
  - Dedup-nya murni substring, jadi 6 varian "debut co-host" lolos semua karena sisipan
    kata di tengah memutus containment. Manfaatnya nyaris nol untuk masalah kita.

Skrip ini memakai KEPUTUSAN EKSPLISIT PER ENTRI, bukan heuristik — supaya bisa diaudit
baris per baris dan tidak ada yang terhapus karena kebetulan cocok pola.

Pakai:
    python scripts/clean_vault_learnings.py              # dry-run (default)
    python scripts/clean_vault_learnings.py --apply      # tulis beneran
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import arti_memory_quality as mq  # noqa: E402 — dipakai untuk sanitasi, bukan ditulis ulang

LEARNINGS = _ROOT / "vault" / "concepts" / "arti_live_learnings.md"

# ---------------------------------------------------------------------------
# Keputusan per entri.
#
# Kunci = nomor baris di berkas asli (per 2026-07-31, 60 entri di baris 7-66).
# Nilai = (aksi, alasan, teks_baru)
#   KEEP    — pertahankan apa adanya
#   REWRITE — pertahankan tapi tulis ulang jadi lebih spesifik; teks lama ada di arsip
#   MERGE   — hapus, isinya sudah terwakili entri lain (sebutkan yang mana)
#   DROP    — hapus, tidak layak jadi memori permanen
#   MOVE    — hapus dari sini, tempatnya di berkas lain
# ---------------------------------------------------------------------------

KEEP, REWRITE, MERGE, DROP, MOVE = "KEEP", "REWRITE", "MERGE", "DROP", "MOVE"

DECISIONS: dict[int, tuple[str, str, str]] = {
    # --- klaster: target "satu jam" (niat sesaat satu sesi) ---
    7:  (DROP, "niat sesaat satu sesi, tidak berlaku besok", ""),
    10: (DROP, "duplikat niat sesaat baris 7", ""),

    # --- fakta unik yang berharga ---
    8:  (KEEP, "pendapat streamer soal arah AI — tahan lama", ""),
    9:  (REWRITE, "state game, TAPI 'episode 1' mengikatnya ke lore seri konten",
         "Streamer punya sisa utang in-game yang belum lunas di episode 1"),

    # --- pengamatan sesaat / meta-noise ---
    11: (DROP, "Arti mencatat dirinya sendiri sebagai orang ketiga — meta-noise", ""),
    12: (DROP, "tautologi: tentu saja sedang live saat dicatat", ""),
    14: (DROP, "fragmen '7D' tidak terdefinisi di mana pun di vault", ""),
    16: (DROP, "mood sesaat, bukan pengetahuan", ""),
    34: (DROP, "fragmen tanpa konteks — 'angka 192' merujuk apa tidak jelas", ""),
    36: (DROP, "generik; varian frasa ini sudah ada di _SKIP_SUBSTRINGS", ""),

    # --- klaster A: tanggal debut (6 entri) — semua dibuang ---
    13: (MERGE, "kanonik di arti_origin.md, di-inject via get_canon_origin_block()", ""),
    24: (MERGE, "duplikat debut", ""),
    31: (MERGE, "duplikat debut", ""),
    37: (MERGE, "duplikat debut", ""),
    38: (MERGE, "duplikat debut", ""),
    39: (MERGE, "duplikat debut", ""),

    # --- klaster C: tab browser di layar (3 entri) — pengamatan sesaat ---
    15: (DROP, "pengamatan layar sesaat, nol nilai permanen", ""),
    19: (DROP, "duplikat tab browser", ""),
    22: (DROP, "duplikat tab browser", ""),

    # --- klaster B: kembang api / rumah lama (5 entri) → 1 wakil ---
    21: (REWRITE, "wakil klaster: satu-satunya yang punya pelaku + aksi",
         "Streamer pernah merakit kembang api besar bersama temannya di rumah lama keluarganya"),
    17: (MERGE, "terwakili baris 21", ""),
    18: (MERGE, "terwakili baris 21", ""),
    20: (MERGE, "terwakili baris 21", ""),
    28: (MERGE, "terwakili baris 21", ""),

    # --- klaster D: Claude AI / JSON / coding di layar (7 entri) — semua sesaat ---
    23: (DROP, "deskripsi layar satu sesi", ""),
    25: (DROP, "deskripsi layar satu sesi", ""),
    26: (DROP, "deskripsi layar satu sesi", ""),
    27: (DROP, "deskripsi layar satu sesi", ""),
    29: (DROP, "deskripsi layar satu sesi", ""),
    30: (DROP, "deskripsi layar satu sesi", ""),
    32: (DROP, "deskripsi layar satu sesi", ""),

    # --- klaster F: skrip Minecraft EP2 di Google Docs (8 entri) → 1 wakil ---
    40: (REWRITE, "wakil klaster: paling lengkap (proyek + episode + lokasi)",
         "Streamer menulis skrip Minecraft episode 2 di Google Docs"),
    33: (MERGE, "terwakili baris 40", ""),
    35: (MERGE, "terwakili baris 40", ""),
    41: (MERGE, "terwakili baris 40", ""),
    43: (MERGE, "terwakili baris 40", ""),
    45: (MERGE, "terwakili baris 40", ""),
    47: (MERGE, "terwakili baris 40", ""),
    51: (MERGE, "terwakili baris 40", ""),
    53: (DROP, "SALAH ATRIBUSI: Arti tertukar dengan Streamer — risiko identitas", ""),

    # --- klaster G: cold open gubuk hujan (3 entri) → 1 wakil ---
    44: (REWRITE, "wakil klaster: menyebut EP2 + urutan scene + malam",
         "Scene pembuka (cold open) EP2 berlatar di gubuk saat malam hujan deras"),
    42: (MERGE, "terwakili baris 44", ""),
    46: (MERGE, "terwakili baris 44", ""),

    # --- L48: detail proyek yang bertahan ---
    48: (KEEP, "detail map yang bertahan lintas sesi", ""),

    # --- klaster H: teaser setahun lalu (4 entri) → 1 wakil ---
    52: (REWRITE, "wakil klaster: nama proyek + status rilis + waktu",
         "Teaser proyek Minecraft sudah dirilis setahun sebelum EP2 digarap"),
    49: (MERGE, "terwakili baris 52", ""),
    50: (MERGE, "terwakili baris 52", ""),
    55: (MERGE, "terwakili baris 52", ""),

    # --- klaster K: Fable 5 (2 entri) → 1 wakil, 1 salah atribusi ---
    57: (REWRITE, "wakil klaster: subjek benar + nama game benar",
         "Streamer memotivasi diri bekerja supaya bisa membeli game Fable 5"),
    54: (DROP, "SALAH ATRIBUSI + nama game rusak (F5/VB5 = Fable 5) — risiko identitas", ""),

    # --- L56: fakta viewer, tempatnya di ARTI_VIEWERS.md ---
    56: (MOVE, "fakta viewer — ARTI_VIEWERS.md tempat yang benar", ""),

    # --- fakta teknis/pendapat yang tahan lama — semua dipertahankan ---
    58: (KEEP, "fakta teknis tahan lama", ""),
    59: (KEEP, "prediksi/pendapat streamer", ""),
    60: (KEEP, "pendapat streamer soal prioritas AI", ""),
    61: (KEEP, "fakta teknis tahan lama", ""),
    62: (KEEP, "fokus kerja streamer — tahan lama", ""),
    63: (KEEP, "fakta teknis tahan lama", ""),

    # --- klaster I: daftar viewer (2 entri) — ARTI_VIEWERS.md lebih lengkap ---
    64: (MERGE, "ARTI_VIEWERS.md punya profil lengkap; versi ini lebih miskin", ""),
    66: (MERGE, "duplikat daftar viewer", ""),

    # --- sudah pasti diketahui sistem ---
    65: (DROP, "tanggal di-inject runtime tiap turn", ""),
}

_BULLET_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\]\s*(.*)$")
_FACT_PREFIX_RE = re.compile(r"^Stream fact:\s*", re.IGNORECASE)


def load_entries() -> list[tuple[int, str, str, str]]:
    """Return (lineno, tanggal, body_tanpa_prefix, baris_asli)."""
    out = []
    for i, raw in enumerate(LEARNINGS.read_text(encoding="utf-8").splitlines(), start=1):
        m = _BULLET_RE.match(raw)
        if not m:
            continue
        date, rest = m.group(1), m.group(2)
        body = _FACT_PREFIX_RE.sub("", mq.sanitize_model_text(rest)).strip()
        out.append((i, date, body, raw))
    return out


def build_output(entries) -> tuple[str, list[tuple]]:
    """Return (teks berkas baru, baris laporan)."""
    report = []
    kept_lines = []
    for lineno, date, body, raw in entries:
        action, reason, new_text = DECISIONS.get(
            lineno, (KEEP, "tidak ada keputusan tercatat — dipertahankan agar aman", "")
        )
        final = new_text if (action == REWRITE and new_text) else body
        if action in (KEEP, REWRITE):
            kept_lines.append(f"- [{date}] {final}")
        report.append((lineno, action, reason, body, final if action == REWRITE else ""))

    head = (
        "# Arti Live Learnings (Default Profile)\n\n"
        "Ini adalah catatan pengetahuan jangka panjang yang dipelajari Arti "
        "(VTuber Co-Host) secara otomatis selama sesi live stream untuk profil "
        "**default**.\n\n"
        "## Memori Jangka Panjang\n\n"
    )
    return head + "\n".join(kept_lines) + "\n", report


def main() -> int:
    ap = argparse.ArgumentParser(description="Bersihkan memori jangka panjang Arti")
    ap.add_argument("--apply", action="store_true", help="tulis beneran (default: dry-run)")
    args = ap.parse_args()

    if not LEARNINGS.is_file():
        print(f"Tidak ketemu: {LEARNINGS}")
        return 2

    entries = load_entries()
    new_text, report = build_output(entries)

    undecided = [r for r in report if "tidak ada keputusan" in r[2]]
    counts = {}
    for _, action, _, _, _ in report:
        counts[action] = counts.get(action, 0) + 1

    print("=" * 100)
    print(f"  BERSIH-BERSIH MEMORI ARTI — {len(entries)} entri")
    print("=" * 100)
    for action in (DROP, MERGE, MOVE, REWRITE, KEEP):
        rows = [r for r in report if r[1] == action]
        if not rows:
            continue
        print(f"\n### {action}  ({len(rows)} entri)\n")
        for lineno, _, reason, body, new in rows:
            print(f"  L{lineno:<3} {body[:74]}")
            print(f"       ↳ {reason}")
            if new:
                print(f"       ⇒ JADI: {new}")
    print()
    print("=" * 100)
    print("  RINGKASAN")
    print("=" * 100)
    for k in (KEEP, REWRITE, MERGE, DROP, MOVE):
        print(f"  {k:<8} {counts.get(k, 0):>3}")
    kept = counts.get(KEEP, 0) + counts.get(REWRITE, 0)
    print(f"  {'-'*20}")
    print(f"  {len(entries)} entri  ->  {kept} entri  ({len(entries)-kept} dibuang)")
    if undecided:
        print(f"\n  PERHATIAN: {len(undecided)} entri tanpa keputusan tercatat "
              f"(dipertahankan): {[r[0] for r in undecided]}")
    moved = [r for r in report if r[1] == MOVE]
    if moved:
        print("\n  Perlu dipindah manual ke ARTI_VIEWERS.md:")
        for lineno, _, _, body, _ in moved:
            print(f"    L{lineno}: {body}")

    if not args.apply:
        print("\n  [DRY-RUN] Tidak ada yang ditulis. Tambahkan --apply untuk menerapkan.")
        return 0

    backup = LEARNINGS.with_suffix(f".md.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(LEARNINGS, backup)  # nama bercap detik — tidak mungkin menimpa
    LEARNINGS.write_text(new_text, encoding="utf-8")
    print(f"\n  DITULIS: {LEARNINGS}")
    print(f"  Backup : {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
