"""Harness bukti live OBS-2B1 Tier-3 — READ-ONLY, tanpa input, tanpa launcher.

Jalankan ini di laptop SEBAGAI PENGGANTI receiver produksi selama sesi bukti
(bridge harus mati; port 3017 fail closed kalau masih dipegang proses lain).
Mod SMAPI ArtiStardewBridge akan menghubungi port ini sendiri — skrip ini
TIDAK menyalakan Stardew/Steam/SMAPI, TIDAK mengirim input, TIDAK mengubah
dunia game, dan TIDAK men-deploy apa pun.

Bawaan TIDAK mencetak banjir state 10 Hz: state hanya menaikkan penghitung
kesehatan MOTOR_STATE. Yang tercetak per event hanya hello + observation
(snapshot/delta digerakkan event, lajunya rendah). `--mentah` menyalakan
echo NDJSON penuh kalau memang perlu forensik.

    ./venv/Scripts/python.exe scripts/bukti_obs2b1_tier3.py --durasi 600
    ./venv/Scripts/python.exe scripts/bukti_obs2b1_tier3.py --durasi 0   # Ctrl+C
    ./venv/Scripts/python.exe scripts/bukti_obs2b1_tier3.py \
        --durasi 900 --keluaran session_logs/obs2b1_tier3.json

Checklist penilaian + arti tiap baris: docs/stardew/OBS-2B1-TIER3-RUNBOOK.md
Menjalankan skrip ini BUKAN klaim LOCAL_VERIFIED; buktinya dinilai manusia.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arti_stardew import DEFAULT_PORT, StardewReceiver  # noqa: E402
from arti_stardew_tier3 import KolektorBuktiTier3  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Kolektor bukti live OBS-2B1 Tier-3 (read-only)"
    )
    ap.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"port listener (bawaan {DEFAULT_PORT}, tempat mod menghubung; "
             "fail closed kalau sudah dipegang receiver lain)",
    )
    ap.add_argument(
        "--durasi", type=float, default=900.0,
        help="lama soak dalam detik; 0 = tanpa batas, berhenti dengan Ctrl+C",
    )
    ap.add_argument(
        "--status-tiap", type=float, default=30.0,
        help="cetak satu baris status ringkas tiap N detik (0 = matikan)",
    )
    ap.add_argument(
        "--mentah", action="store_true",
        help="echo NDJSON penuh per event (bawaan MATI — banjir 10 Hz)",
    )
    ap.add_argument(
        "--keluaran", default=None,
        help="path file JSON ringkasan akhir (opsional)",
    )
    a = ap.parse_args()

    def cetak_bukti(baris: str) -> None:
        print(baris, flush=True)

    kolektor = KolektorBuktiTier3(on_baris=cetak_bukti)

    def pada_event(event: dict) -> None:
        if a.mentah:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        kolektor.on_event(event)

    receiver = StardewReceiver(on_event=pada_event, port=a.port)
    try:
        receiver.start()
    except OSError as exc:
        print(
            f"[Tier3] GAGAL bind port {a.port}: {exc}\n"
            "[Tier3] Kemungkinan bridge/receiver produksi masih hidup. "
            "Matikan dulu, atau pakai --port lain LALU arahkan mod ke sana. "
            "Fail closed, tidak merebut port.",
            flush=True,
        )
        return 1

    host, port = receiver.address
    print(f"[Tier3] Mendengarkan {host}:{port} (read-only, tanpa input)", flush=True)
    if a.durasi > 0:
        print(f"[Tier3] Soak {a.durasi:.0f} detik. Ctrl+C untuk berhenti lebih awal.", flush=True)
    else:
        print("[Tier3] Tanpa batas waktu. Ctrl+C untuk berhenti.", flush=True)

    mulai = time.monotonic()
    status_berikut = mulai + a.status_tiap if a.status_tiap > 0 else None
    try:
        while True:
            time.sleep(0.25)
            sekarang = time.monotonic()
            if a.durasi > 0 and sekarang - mulai >= a.durasi:
                break
            if status_berikut is not None and sekarang >= status_berikut:
                status_berikut = sekarang + a.status_tiap
                r = kolektor.ringkasan()
                p = r["protokol"]
                print(
                    f"[Tier3 status +{sekarang - mulai:.0f}s] "
                    f"state={p['state']} obs={p['observation']} "
                    f"accepted={r['apply']['accepted']} "
                    f"rejected={r['apply']['rejected']} "
                    f"delta_objek={r['delta_scene_diterima']} "
                    f"err={r['callback_error']['jumlah']}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("\n[Tier3] Dihentikan manual.", flush=True)
    finally:
        receiver.stop()

    print(flush=True)
    for baris in kolektor.baris_ringkas():
        print("  " + baris, flush=True)

    if a.keluaran:
        ringkasan = kolektor.ringkasan()
        ringkasan["_meta"] = {
            "skrip": "scripts/bukti_obs2b1_tier3.py",
            "port": port,
            "durasi_s": round(time.monotonic() - mulai, 1),
            "bukti": list(kolektor.bukti),
        }
        os.makedirs(os.path.dirname(a.keluaran) or ".", exist_ok=True)
        with open(a.keluaran, "w", encoding="utf-8") as f:
            json.dump(ringkasan, f, ensure_ascii=False, indent=1)
        print(f"\n[Tier3] Ringkasan JSON: {a.keluaran}", flush=True)

    print(
        "\n[Tier3] Catatan: keluaran ini BUKTI MENTAH, bukan vonis. "
        "Nilai terhadap checklist docs/stardew/OBS-2B1-TIER3-RUNBOOK.md. "
        "Tidak ada klaim LOCAL_VERIFIED dari skrip ini.",
        flush=True,
    )
    # Exit 1 hanya kalau harness-nya sendiri bermasalah (callback error).
    return 1 if kolektor.callback_error_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
