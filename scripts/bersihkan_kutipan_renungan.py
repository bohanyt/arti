# -*- coding: utf-8 -*-
"""Bersihkan kutipan mentah pembuka renungan dari vault (20 Agu 2026).

Sesi renungan pertama menyimpan kesimpulan dengan format lama:
    Renungan Arti: berawal dari "[recorded remark omitted]" — kesimpulannya: ...
Kalimat pembuka itu memuat frasa template yang lahir dari contoh di prompt
(sudah dicabut di arti_renungan.py). Karena tersimpan di vault, RAG menarik
kembali dan Arti MENIRU DIRINYA SENDIRI — 3 jawaban di log 21.58 memakai
frasa itu walau prompt-nya sudah bersih. Ingatan tidak boleh jadi cetakan
gaya bicara.

Skrip ini menulis ulang baris tersebut ke format baru (kata kunci topik,
bukan kutipan) TANPA menghilangkan kesimpulannya.

JALANKAN SAAT BRIDGE MATI (vault ditulis observer/kurator saat shutdown).
  ./venv/Scripts/python.exe scripts/bersihkan_kutipan_renungan.py --tulis
Tanpa --tulis = pratinjau saja. Cadangan .bak dibuat otomatis.
Sesudahnya: reindex RAG supaya indeks ikut segar
  ./venv/Scripts/python.exe arti_vault_rag.py --reindex-all
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import arti_renungan as ar  # noqa: E402

POLA = re.compile(
    r'Renungan Arti:\s*berawal dari\s*"(?P<topik>[^"]*)"\s*—\s*kesimpulannya:\s*'
    r'(?P<isi>.*)$'
)


def baris_baru(baris: str) -> str | None:
    m = POLA.search(baris)
    if not m:
        return None
    kunci = sorted(ar._kata_topik(m.group("topik")))[:5]
    label = ", ".join(kunci) or "obrolan lama"
    awalan = baris[: m.start()]
    return f"{awalan}Renungan Arti soal {label} — kesimpulannya: {m.group('isi')}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tulis", action="store_true", help="tanpa ini = pratinjau")
    args = ap.parse_args()

    target = [_ROOT / "vault" / "concepts"]
    target += [_ROOT / "vault" / "sessions"]
    berkas = [p for d in target if d.is_dir() for p in d.glob("*.md")]
    total = 0
    for p in berkas:
        teks = p.read_text(encoding="utf-8")
        baris = teks.splitlines()
        ubah = 0
        for i, b in enumerate(baris):
            nb = baris_baru(b)
            if nb and nb != b:
                if ubah < 2:
                    print(f"  {p.name}:")
                    print(f"    - {b[:100]}")
                    print(f"    + {nb[:100]}")
                baris[i] = nb
                ubah += 1
        if ubah:
            total += ubah
            print(f"  {p.name}: {ubah} baris")
            if args.tulis:
                p.with_suffix(p.suffix + ".bak").write_text(teks, encoding="utf-8")
                p.write_text("\n".join(baris) + "\n", encoding="utf-8")
    if not total:
        print("Tidak ada baris format lama — vault sudah bersih.")
        return 0
    print(f"\nTotal {total} baris {'DITULIS ULANG' if args.tulis else 'akan diubah'}.")
    if args.tulis:
        print("Cadangan .bak dibuat. Lanjutkan dengan reindex RAG:")
        print("  ./venv/Scripts/python.exe arti_vault_rag.py --reindex-all")
    else:
        print("Pratinjau saja — tambahkan --tulis untuk menerapkan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
