"""Bedah satu log siaran (session_logs/*_bridge.log) jadi angka.

Terminal Windows cuma menyimpan bagian terakhir keluaran; log utuhnya selalu
ada di session_logs/ (rotasi MEMINDAHKAN ke
archive/, tidak menghapus).

    ./venv/Scripts/python.exe scripts/analisa_log_siaran.py
    ./venv/Scripts/python.exe scripts/analisa_log_siaran.py --bagian chat
    ./venv/Scripts/python.exe scripts/analisa_log_siaran.py path/ke/log.log

Tanpa argumen: log siaran TERBARU di session_logs/.

Dua jebakan yang sudah dibayar dan dijaga di sini:

1. Terminal Windows cp1252 meledak begitu kena emoji chat penonton, dan
   log ini penuh emoji. stdout dipaksa UTF-8 di bawah.
2. Handle YouTube memuat "-" dan "." (@contoh-viewr, @contoh_viewer.dua).
   Pola @[A-Za-z0-9_]+ MEMOTONG nama di tengah dan mengacaukan hitungan
   per-penonton — persis kesalahan yang sempat kubuat waktu membedah log
   27 Agu. Lihat _HANDLE.
"""

from __future__ import annotations

import argparse
import collections
import io
import os
import re
import statistics
import sys

def paksa_stdout_utf8() -> None:
    """Emoji chat penonton menjatuhkan stdout cp1252 di terminal Windows.

    Idempoten dengan sengaja: skrip lain mengimpor modul ini, dan membungkus
    stdout DUA KALI membuat pembungkus pertama ditutup oleh GC — gejalanya
    `ValueError: I/O operation on closed file` di print pertama.
    """
    if getattr(sys.stdout, "encoding", "").lower().startswith("utf"):
        return
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")


paksa_stdout_utf8()

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_ROOT, "session_logs")

# JANGAN dipersempit jadi [A-Za-z0-9_]+ — lihat docstring.
_HANDLE = r"@[A-Za-z0-9_.\-]+"

_TAHAP = ["vts_mikir", "rag", "llm", "tts_synth", "tts_play", "tts_jeda",
          "lain", "total"]
# Waktu dari trigger sampai Arti MULAI bunyi. Ini metrik yang benar untuk
# "Arti lambat atau tidak"; `total` memasukkan durasi Arti bicara.
_PRA_SUARA = ("vts_mikir", "rag", "llm", "tts_synth", "lain")


def log_terbaru() -> str:
    kandidat = [
        os.path.join(_LOG_DIR, n)
        for n in os.listdir(_LOG_DIR)
        if n.endswith("_bridge.log")
    ]
    if not kandidat:
        raise SystemExit(f"  [!] tidak ada *_bridge.log di {_LOG_DIR}")
    # Log siaran sungguhan selalu >20 KB; sisanya cangkang bridge gagal start.
    besar = [p for p in kandidat if os.path.getsize(p) >= 20_000] or kandidat
    return max(besar, key=os.path.getmtime)


def pct(nilai: list[float], q: float) -> float:
    urut = sorted(nilai)
    return urut[min(len(urut) - 1, int(len(urut) * q))]


def _judul(teks: str) -> None:
    print(f"\n=== {teks} ===")


def bagian_latensi(L: list[str]) -> None:
    _judul("LATENSI")
    data: dict[str, list[int]] = {k: [] for k in _TAHAP}
    pra: list[int] = []
    for baris in L:
        if "[Latency]" not in baris:
            continue
        punya = {}
        for k in _TAHAP:
            m = re.search(k + r"=(\d+)ms", baris)
            if m:
                punya[k] = int(m.group(1))
                data[k].append(punya[k])
        pra.append(sum(punya.get(k, 0) for k in _PRA_SUARA))
    if not pra:
        print("  (tidak ada baris [Latency])")
        return
    print(f"  {'metrik':12}{'n':>6}{'p50':>9}{'p90':>9}{'maks':>9}")
    for k in _TAHAP:
        if not data[k]:
            continue
        print(f"  {k:12}{len(data[k]):>6}{statistics.median(data[k])/1000:>8.2f}s"
              f"{pct(data[k], .9)/1000:>8.2f}s{max(data[k])/1000:>8.2f}s")
    print(f"  {'PRA-SUARA':12}{len(pra):>6}{statistics.median(pra)/1000:>8.2f}s"
          f"{pct(pra, .9)/1000:>8.2f}s{max(pra)/1000:>8.2f}s")
    print("  (PRA-SUARA = trigger -> Arti mulai bunyi. `total` termasuk"
          " durasi Arti BICARA.)")


def bagian_chat(L: list[str]) -> None:
    _judul("CHAT PENONTON")
    def n(pola: str) -> int:
        return sum(1 for b in L if pola in b)
    masuk = sum(1 for b in L if re.search(r"\[YT Chat\] " + _HANDLE + ":", b))
    jawab = n("[Queue] Trigger (yt_chat)")
    print(f"  masuk            : {masuk}")
    if masuk:
        print(f"  dijawab          : {jawab}  ({jawab*100//masuk}%)")
    print(f"  dibuang (sibuk)  : {n('Skip trigger (yt_chat)')}")
    print(f"  tertahan keran   : {n('tertahan keran')}")
    print(f"  cooldown pribadi : {n('kena cooldown pribadi')}")

    trig = collections.Counter(
        m.group(1) for b in L
        for m in [re.search(r"\[Queue\] Trigger \((\w+)\)", b)] if m
    )
    if trig:
        print(f"  trigger          : {dict(trig.most_common())}")
        cur, yt = trig.get("curious", 0), trig.get("yt_chat", 0)
        if yt:
            print(f"  rasio curious:yt : {cur/yt:.1f} : 1"
                  "   (di atas 1:1 = Arti lebih sering bicara ke dirinya sendiri)")

    play = [int(m.group(1)) for b in L
            for m in [re.search(r"tts_play=(\d+)ms", b)] if m]
    tot = [int(m.group(1)) for b in L
           for m in [re.search(r"\[Latency\].*total=(\d+)ms", b)] if m]
    span = _rentang_detik(L)
    if span and play:
        print(f"  Arti bicara      : {sum(play)/60000:.0f} menit"
              f" = {sum(play)/10/span:.0f}% wall-clock")
        print(f"  Arti sibuk       : {sum(tot)/60000:.0f} menit"
              f" = {sum(tot)/10/span:.0f}% wall-clock")


def bagian_penonton(L: list[str]) -> None:
    _judul("PER PENONTON")
    pesan: collections.Counter = collections.Counter()
    jawab: collections.Counter = collections.Counter()
    drop: collections.Counter = collections.Counter()
    keran: collections.Counter = collections.Counter()
    cd: collections.Counter = collections.Counter()
    pola = [
        (re.compile(r"\[YT Chat\] (" + _HANDLE + r"):"), pesan),
        (re.compile(r"\[YT Chat\] Antri jawab (" + _HANDLE + r")"), jawab),
        (re.compile(r"Skip trigger \(yt_chat\).*?(?:Viewer|bernama) (" + _HANDLE + r")"), drop),
        (re.compile(r"Chat santai dari (" + _HANDLE + r") tertahan keran"), keran),
        (re.compile(r"(" + _HANDLE + r") kena cooldown pribadi"), cd),
    ]
    for baris in L:
        for rx, akum in pola:
            m = rx.search(baris)
            if m:
                akum[m.group(1)] += 1
                break
    if not pesan:
        print("  (tidak ada pesan chat)")
        return
    print(f"  {'penonton':26}{'pesan':>6}{'jawab':>7}{'%':>5}"
          f"{'drop':>6}{'keran':>7}{'cooldn':>7}")
    for v, jml in pesan.most_common(20):
        if v == "@Streamlabs":
            continue
        print(f"  {v:26}{jml:>6}{jawab[v]:>7}{jawab[v]*100//jml:>5}"
              f"{drop[v]:>6}{keran[v]:>7}{cd[v]:>7}")
    nol = [v for v in pesan if jawab[v] == 0 and v != "@Streamlabs"]
    print(f"\n  TIDAK PERNAH dijawab: {len(nol)} dari {len(pesan)-1} penonton")
    if nol:
        print("   ", ", ".join(f"{v}({pesan[v]})"
                               for v in sorted(nol, key=lambda x: -pesan[x])))


def bagian_provider(L: list[str]) -> None:
    _judul("PROVIDER")
    ok: collections.Counter = collections.Counter()
    gagal: collections.Counter = collections.Counter()
    buang: collections.Counter = collections.Counter()
    for baris in L:
        m = re.search(r"\[(Vision|Scouter)\] (\w+) parse fail \((\d+)ms\)", baris)
        if m:
            k = (m.group(1), m.group(2))
            gagal[k] += 1
            buang[k] += int(m.group(3))
            continue
        m = re.search(r"\[(Vision|Scouter)\] (\w+) fail:", baris)
        if m:
            gagal[(m.group(1), m.group(2))] += 1
            continue
        m = re.search(r"\[(Vision|Scouter)\] OK (\w+)", baris)
        if m:
            ok[(m.group(1), m.group(2))] += 1
    kunci = sorted(set(ok) | set(gagal))
    if not kunci:
        print("  (tidak ada jejak provider)")
        return
    print(f"  {'subsystem':10}{'provider':22}{'OK':>5}{'gagal':>7}"
          f"{'sukses':>8}{'terbuang':>11}")
    for k in kunci:
        n = ok[k] + gagal[k]
        rate = f"{ok[k]*100//n}%" if n else "-"
        print(f"  {k[0]:10}{k[1]:22}{ok[k]:>5}{gagal[k]:>7}{rate:>8}"
              f"{buang[k]/1000:>10.1f}s")
    print(f"\n  total waktu hangus terukur: {sum(buang.values())/60000:.1f} menit")
    print("  (catatan: telemetri TIDAK mencatat kegagalan provider —"
          " angka ini hanya bisa dari log teks)")


def bagian_agy(L: list[str]) -> None:
    _judul("AGY (otak suara)")
    baris_ok = [b for b in L if b.startswith("[Agy]") and "status=" in b]
    if not baris_ok:
        print("  (tidak ada baris [Agy])")
        return
    st = collections.Counter(
        m.group(1) for b in baris_ok
        for m in [re.search(r"status=(\w+)", b)] if m
    )
    # Satu kejadian timeout mencetak BEBERAPA baris ("timeout Ns — jatuh ke
    # Luna", "utama gagal (timeout)", "proses dibuang (timeout)"), jadi ini
    # hitungan BARIS, bukan hitungan kejadian. Jangan dibaca sebagai jumlah
    # kegagalan.
    tmo = sum(1 for b in L if "[Agy]" in b and "timeout" in b.lower())
    print(f"  status  : {dict(st)}  ({tmo} baris memuat 'timeout' —"
          " satu kejadian = ~3 baris)")
    for k in ("ttft", "total"):
        a = [int(m.group(1)) for b in baris_ok
             for m in [re.search(k + r"=(\d+)ms", b)] if m]
        if a:
            print(f"  {k:7} : n={len(a)} p50={statistics.median(a)/1000:.2f}s"
                  f" p90={pct(a, .9)/1000:.2f}s maks={max(a)/1000:.2f}s")
    ctx = [int(m.group(1)) for b in baris_ok
           for m in [re.search(r" in=(\d+)", b)] if m]
    if ctx:
        print(f"  ctx in  : p50={statistics.median(ctx):,.0f}"
              f" p90={pct(ctx, .9):,} maks={max(ctx):,} token")
    for b in L:
        if "] kuota" in b:
            print(f"  {b.strip()}")
    print("  (angka kuota itu SISA, bukan terpakai — 'Weekly Limit Remaining')")


def _rentang_detik(L: list[str]) -> float:
    jam = re.findall(r"\[(\d\d):(\d\d):(\d\d)\]", "\n".join(L[:400] + L[-400:]))
    if len(jam) < 2:
        return 0.0
    def dtk(t):
        return int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2])
    a, b = dtk(jam[0]), dtk(jam[-1])
    return (b - a) if b > a else (b + 86400 - a)  # siaran lewat tengah malam


def bagian_ringkas(L: list[str], path: str) -> None:
    _judul("SESI")
    print(f"  berkas   : {path}")
    print(f"  baris    : {len(L):,}")
    jam = re.findall(r"\[(\d\d:\d\d:\d\d)\]", "\n".join(L[:400] + L[-400:]))
    if jam:
        span = _rentang_detik(L)
        print(f"  rentang  : {jam[0]} -> {jam[-1]}"
              f"  = {span/3600:.1f} jam")
    print(f"  giliran  : {sum(1 for b in L if '[Latency]' in b)}")
    print(f"  traceback: {sum(1 for b in L if 'Traceback' in b)}")


_BAGIAN = {
    "latensi": bagian_latensi,
    "chat": bagian_chat,
    "penonton": bagian_penonton,
    "provider": bagian_provider,
    "agy": bagian_agy,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", nargs="?", help="path log; kosong = terbaru")
    ap.add_argument("--bagian", choices=sorted(_BAGIAN),
                    help="cetak satu bagian saja")
    a = ap.parse_args()

    path = a.log or log_terbaru()
    with open(path, encoding="utf-8", errors="replace") as fh:
        L = fh.read().splitlines()

    if a.bagian:
        _BAGIAN[a.bagian](L)
        return 0
    bagian_ringkas(L, path)
    for fn in (bagian_latensi, bagian_chat, bagian_penonton,
               bagian_provider, bagian_agy):
        fn(L)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
