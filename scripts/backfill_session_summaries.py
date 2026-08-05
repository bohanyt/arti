#!/usr/bin/env python3
"""Saripatikan dialog minggu pertama Arti jadi ringkasan yang bisa di-embed.

MASALAH YANG DIPECAHKAN
`arti_vault_rag._preprocess_source_text` membuang seluruh blok fence ```text sebelum
embedding — dan di situlah riwayat percakapan berada. Pembuangan itu masuk akal (dialog
mentah penuh salah dengar ASR: "Erti.", "Barum.", "HEEH!"), tapi peredamnya kosong:
seksi `## Ringkasan Sesi` untuk berkas 27 Mei - 1 Juni belum pernah diisi karena fitur
summarizer belum ada waktu itu. Isinya cuma boilerplate
"Sesi streaming langsung dengan Arti sebagai Co-Host" — 84 blok, 33 di antaranya di hari
debut sendiri.

Akibatnya Arti tidak bisa mengingat minggu pertama hidupnya, padahal transkripnya masih
utuh di disk. Skrip ini membaca dialog itu, membuat catatan arsip per blok restart, dan
MENGGANTI boilerplate-nya. Fence dialog dibiarkan utuh — ia tetap arsip di disk, cuma
sekarang punya wakil yang bisa di-embed.

Pakai:
    python scripts/backfill_session_summaries.py                    # dry-run
    python scripts/backfill_session_summaries.py --file 2026-05-27  # satu berkas
    python scripts/backfill_session_summaries.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import requests  # noqa: E402

import arti_env  # noqa: E402
import arti_memory_quality as mq  # noqa: E402

arti_env.load_project_env(_ROOT)

SESSIONS = _ROOT / "vault" / "sessions"
BOILERPLATE = "Sesi streaming langsung dengan Arti sebagai Co-Host"

# Hanya berkas yang ringkasannya boilerplate DAN dialognya ada.
# 2026-06-14 sengaja TIDAK di sini: fence 10 KB-nya berisi debug log
# ("Task was destroyed but it is pending!"), bukan dialog.
TARGETS = [
    "2026-05-27", "2026-05-28", "2026-05-29",
    "2026-05-30", "2026-05-31", "2026-06-01",
]

# Rantai model. qwen3.6 dipilih pertama: terverifikasi paling bagus DAN paling cepat
# (0,64s vs 1,14s gpt-oss) di uji banding pada blok terbesar 27 Mei.
# `reasoning_effort: none` wajib untuk qwen3.6 — trik prompt "/no_think" diabaikan
# model ini dan CoT-nya akan memakan max_tokens sampai content kosong (lihat dd88d9e).
# gpt-oss-* adalah model reasoning: pada input panjang mereka menghabiskan max_tokens
# untuk CoT dan mengembalikan content KOSONG (finish=length) — terbukti pada blok
# 2026-06-01 20:41:37 (4.175 char). Karena itu mereka diberi budget token lebih besar
# saat dipakai sebagai cadangan.
MODELS = [
    ("qwen/qwen3.6-27b", {"reasoning_effort": "none"}),
    ("openai/gpt-oss-20b", {"max_tokens": 1500}),
    ("openai/gpt-oss-120b", {"max_tokens": 1500}),
]

def _penonton_tetap() -> str:
    """Nama penonton tetap dari config_local.json (per-mesin, TIDAK di-commit).

    Daftar ini menolong summarizer mengeja nama yang sering salah didengar ASR.
    Isinya nama ORANG LAIN, jadi tinggal di config_local yang gitignored.
    Isi sendiri:  "session_summary_regulars": ["nama1", "nama2"]
    """
    import json  # noqa: PLC0415

    try:
        with open(_ROOT / "config_local.json", encoding="utf-8") as f:
            names = json.load(f).get("session_summary_regulars") or []
    except (OSError, json.JSONDecodeError):
        names = []
    return ", ".join(str(n) for n in names) if names else "(tidak didaftarkan)"


BACKFILL_SYSTEM = """Kamu ARSIPARIS. Tugasmu mencatat apa yang TERJADI di sebuah sesi live
stream yang SUDAH LAMA BERLALU, berdasarkan transkripnya.

PENTING — kesalahan paling umum di tugas ini:
- Kamu BUKAN Arti. JANGAN menjawab, menyapa, atau membalas siapa pun dalam transkrip.
- JANGAN menulis dialog baru. Transkrip ini masa lalu; tidak ada yang menunggu jawaban.
- JANGAN menjelaskan penalaranmu. Langsung tulis catatannya.

Keluarkan DUA bagian, persis format ini:

<ringkasan>
2-4 kalimat bahasa Indonesia sebagai CATATAN ARSIP orang ketiga: alur utama sesi ini.
</ringkasan>
<topik>
- satu baris per UTAS BERBEDA
</topik>

Bagian <topik> ada karena prosa saja memaksa memilih satu alur dan membuang sisanya.
Daftar ini yang menjaga cakupan. WAJIB masuk kalau ada di transkrip:
- SETIAP penonton yang bertanya — tulis nama dan inti pertanyaannya, satu baris masing-masing,
  walaupun pertanyaannya tidak dijawab atau tidak jadi topik utama.
- KOMENTAR STREAMER TENTANG PERILAKU ARTI — pujian, keluhan, atau koreksi. Contoh nyata
  yang pernah terlewat: "Cuma dibilang menarik tapi gak ada jawabannya". Ini bahan belajar
  paling berharga, jangan pernah dibuang.
- FAKTA TENTANG ARTI yang terungkap (mis. tinggi model 159 cm), termasuk kalau jawabannya
  saling bertentangan dalam sesi yang sama — sebutkan pertentangannya.
- Masalah teknis konkret yang disebut, dengan detailnya.
Kalau memang tidak ada utas apa pun, tulis satu baris: "- (tidak ada)".

Catatan ini akan jadi ingatan jangka panjang Arti, jadi tulis hal yang berguna diingat
berbulan-bulan kemudian — bukan bahwa sesi dimulai atau bahwa Arti menjawab dengan ramah.

TRANSKRIP INI HASIL ASR (speech-to-text) DAN SERING SALAH DENGAR.
Nama yang benar — pakai ejaan ini, jangan salin salah dengarnya:
- Streamer: **Bohan** (sering muncul salah sebagai "Bahan", "Bosan", "Barum", "Bahen")
- Co-host AI: **Arti** (sering muncul salah sebagai "Erti", "Arti" terpotong)
- Penonton tetap: {penonton_tetap}
Kalau ada kata yang jelas salah dengar tapi maksudnya bisa ditebak dari konteks, tulis
maksudnya. Kalau tidak bisa ditebak, JANGAN mengarang — lewati saja bagian itu.

Kalau transkripnya cuma sapaan atau tes mikrofon tanpa isi, tulis satu kalimat singkat
yang mengatakan demikian. Jangan mengarang."""

MIN_DIALOG_CHARS = 200
_FENCE_RE = re.compile(r"```text\n(.*?)```", re.S)
_EVENT_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]", re.M)


def _wrap(text: str, width: int) -> list[str]:
    """Bungkus teks untuk pratinjau terminal (tanpa dependensi tambahan)."""
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def groq_summarize(dialog: str, date: str, restart_label: str) -> tuple[str | None, str]:
    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        return None, "GROQ_API_KEY kosong"
    user = (
        f"TANGGAL: {date}\n"
        f"BAGIAN: {restart_label}\n\n"
        f"TRANSKRIP:\n{dialog}"
    )
    last_err = ""
    for model, extra in MODELS:
        payload = {
            "model": model,
            # 700, bukan 400: keluarannya sekarang dua bagian (prosa + daftar topik).
            "max_tokens": 700,
            "temperature": 0.3,
            "messages": [
                {
                    "role": "system",
                    "content": BACKFILL_SYSTEM.replace(
                        "{penonton_tetap}", _penonton_tetap()
                    ),
                },
                {"role": "user", "content": user},
            ],
        }
        if extra:
            payload.update(extra)
        for attempt in range(5):
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=120,
                )
            except Exception as e:  # noqa: BLE001
                last_err = f"{model}: {type(e).__name__}"
                break
            if r.status_code == 429:
                # Hormati header retry-after kalau ada; kalau tidak, backoff eksponensial.
                # Backoff lama (3/6/9s, 3 percobaan) terlalu pendek — blok terpadat
                # kehabisan percobaan lalu jatuh ke gpt-oss-120b yang balas kosong
                # (finish=length, model reasoning kehabisan budget di input panjang).
                hdr = r.headers.get("retry-after") or ""
                try:
                    wait = float(hdr)
                except ValueError:
                    wait = min(45.0, 4.0 * (2 ** attempt))
                print(f"      [429] rate limit, tunggu {wait:.0f}s "
                      f"(percobaan {attempt + 1}/5)...")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                last_err = f"{model}: HTTP {r.status_code} {r.text[:90]}"
                break
            ch = r.json()["choices"][0]
            txt = (ch["message"].get("content") or "").strip()
            txt = mq.sanitize_model_text(txt).strip()
            txt = _format_sections(txt)
            if txt:
                return txt, model
            last_err = f"{model}: content kosong (finish={ch.get('finish_reason')})"
            break
    return None, last_err


_SEC_RINGKASAN = re.compile(r"<ringkasan>(.*?)</ringkasan>", re.S | re.I)
_SEC_TOPIK = re.compile(r"<topik>(.*?)</topik>", re.S | re.I)
# "- (tidak ada)", "- (tidak ada pertanyaan dari penonton)", "- Tidak ada fakta baru", dst.
_NO_TOPIC_RE = re.compile(r"^-\s*\(?tidak ada\b", re.I)


def _format_sections(raw: str) -> str:
    """Ubah keluaran <ringkasan>/<topik> jadi Markdown; toleran kalau tag hilang."""
    if not raw:
        return ""
    m_r = _SEC_RINGKASAN.search(raw)
    m_t = _SEC_TOPIK.search(raw)
    if not m_r and not m_t:
        # Model tidak memakai tag — pakai apa adanya, jangan buang isinya.
        return raw.strip()
    prose = (m_r.group(1).strip() if m_r else "").strip()
    topics = (m_t.group(1).strip() if m_t else "").strip()
    lines = [ln.strip() for ln in topics.splitlines() if ln.strip()]
    lines = [ln if ln.startswith("-") else f"- {ln}" for ln in lines]
    # Buang baris "tidak ada ..." dalam bentuk apa pun. Model sering menuliskan
    # penyangkalan per-kategori ("- (tidak ada pertanyaan dari penonton)", "- (tidak ada
    # fakta baru tentang Arti)") yang murni noise di daftar topik — dan noise itu ikut
    # di-embed. Filter lama hanya menangkap bentuk persis "- (tidak ada)".
    lines = [ln for ln in lines if not _NO_TOPIC_RE.match(ln)]
    out = prose
    if lines:
        out += "\n\n**Topik:**\n" + "\n".join(lines)
    return out.strip()


def process_file(path: Path, apply: bool, force: bool = False) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    date = path.name[:10]
    # Pecah jadi (header_restart, isi) sambil menjaga teks di luar blok tetap utuh.
    parts = re.split(r"(^## Restart .+$)", text, flags=re.M)
    out = [parts[0]]
    stats = {"blok": 0, "diringkas": 0, "dilewati_kecil": 0, "gagal": 0, "sudah_ada": 0}

    for i in range(1, len(parts), 2):
        header, body = parts[i], parts[i + 1]
        stats["blok"] += 1
        label = header.replace("## Restart ", "").strip()

        m = re.search(r"(## Ringkasan Sesi\n)(.*?)(?=\n## |\Z)", body, re.S)
        if not m:
            out.extend([header, body])
            continue
        current = m.group(2).strip()
        if not force and current and BOILERPLATE not in current:
            stats["sudah_ada"] += 1
            out.extend([header, body])
            continue

        dialog = "\n".join(_FENCE_RE.findall(body)).strip()
        dialog = mq.sanitize_model_text(dialog).strip()
        n_ev = len(_EVENT_RE.findall(dialog))

        if len(dialog) < MIN_DIALOG_CHARS:
            stats["dilewati_kecil"] += 1
            new = (f"Sesi restart {label} nyaris tanpa interaksi "
                   f"({n_ev} kejadian tercatat).")
            print(f"    {label:<48} [kecil] {len(dialog)} char -> catatan singkat")
        else:
            print(f"    {label:<48} {len(dialog):>5} char, {n_ev:>3} kejadian ...",
                  end="", flush=True)
            summary, used = groq_summarize(dialog, date, label)
            if not summary:
                stats["gagal"] += 1
                print(f" GAGAL ({used})")
                out.extend([header, body])
                continue
            stats["diringkas"] += 1
            new = summary
            print(f" ok ({used})")
            for line in _wrap(summary, 84):
                print(f"        │ {line}")
            time.sleep(1.2)  # jaga jarak dari rate limit free tier

        body = body[:m.start(2)] + new + "\n" + body[m.end(2):]
        out.extend([header, body])

    new_text = "".join(out)
    if apply and new_text != text:
        backup = path.with_suffix(f".md.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        path.write_text(new_text, encoding="utf-8")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Saripatikan dialog sesi jadi ringkasan arsip")
    ap.add_argument("--apply", action="store_true", help="tulis beneran (default: dry-run)")
    ap.add_argument("--file", default="", help="proses satu tanggal saja, mis. 2026-05-27")
    ap.add_argument("--force", action="store_true",
                    help="tulis ulang blok yang SUDAH punya ringkasan (untuk A10b: "
                         "menambahkan daftar Topik ke ringkasan lama)")
    args = ap.parse_args()

    targets = [t for t in TARGETS if not args.file or t == args.file]
    if not targets:
        print(f"Tidak ada target cocok: {args.file}")
        return 2

    print("=" * 88)
    print(f"  SARIPATI SESI — {len(targets)} berkas"
          f"{'  [DRY-RUN]' if not args.apply else '  [APPLY]'}")
    print("=" * 88)

    total = {"blok": 0, "diringkas": 0, "dilewati_kecil": 0, "gagal": 0, "sudah_ada": 0}
    for t in targets:
        p = SESSIONS / f"{t}-default.md"
        if not p.is_file():
            print(f"\n{t}: TIDAK ADA")
            continue
        print(f"\n{t}:")
        s = process_file(p, args.apply, force=args.force)
        for k in total:
            total[k] += s[k]

    print()
    print("=" * 88)
    print(f"  blok restart      : {total['blok']}")
    print(f"  diringkas         : {total['diringkas']}")
    print(f"  catatan singkat   : {total['dilewati_kecil']}")
    print(f"  sudah punya       : {total['sudah_ada']}")
    print(f"  gagal             : {total['gagal']}")
    if not args.apply:
        print("\n  [DRY-RUN] Tidak ada yang ditulis. Tambahkan --apply untuk menerapkan.")
    return 1 if total["gagal"] else 0


if __name__ == "__main__":
    sys.exit(main())
