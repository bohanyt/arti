"""Soak OFFLINE jalur state Stardew — tanpa game, tanpa mengganggu bridge.

Butir ke-9 acceptance Gate 5 berbunyi "tidak ada socket/thread leak/stutter/
backlog". Delapan butir lainnya memang menuntut game hidup (lokasi, menu,
quit/rejoin), tetapi yang ke-9 murni sifat receiver + store, dan itu bisa
dibuktikan sekarang.

Yang ditiru: DUA farmer (P1 utama + P2 farmhand) mengalirkan state 10 Hz
lewat SATU koneksi TCP, persis seperti mod SMAPI. Yang diukur:

  - thread & koneksi: tumbuh atau tidak selama soak
  - latensi masuk->tersimpan: p50 / p99 / maks
  - jumlah farmer di store: harus tetap 2, bukan tumbuh per screen/epoch
  - event ditolak: harus 0 untuk lalu lintas yang sah
  - backlog: selisih terkirim vs tercatat

Port bawaan SENGAJA bukan 3017 supaya receiver produksi/sesi live tidak
pernah tersenggol.

    ./venv/Scripts/python.exe scripts/soak_stardew.py --detik 600
    ./venv/Scripts/python.exe scripts/soak_stardew.py --detik 20 --hz 10
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arti_stardew import StardewReceiver, StardewStateStore  # noqa: E402

P1 = 1100000000000000002
P2 = -100000000000000001
PORT_SOAK = 3098  # BUKAN 3017: jangan pernah menyenggol receiver produksi.


def _hello(seq: int, pid: int, screen: int, nama: str, utama: bool) -> dict:
    return {
        "protocol": 1, "ev": "hello", "seq": seq,
        "player_id": pid, "screen_id": screen,
        "player_name": nama, "is_main_player": utama, "is_split_screen": True,
    }


# Hotbar PENUH dengan nama panjang = kasus terburuk ukuran payload. Soak
# yang jadi baseline Gate 5 dijalankan sebelum inventaris ada, jadi angkanya
# tidak lagi mewakili apa yang benar-benar dikirim mod sekarang.
_HOTBAR_PENUH = [
    {"slot": 0, "name": "Hoe", "stack": 1, "is_tool": True},
    {"slot": 1, "name": "Watering Can", "stack": 1, "is_tool": True},
    {"slot": 2, "name": "Pickaxe", "stack": 1, "is_tool": True},
    {"slot": 3, "name": "Axe", "stack": 1, "is_tool": True},
    {"slot": 4, "name": "Scythe", "stack": 1, "is_tool": True},
    {"slot": 5, "name": "Parsnip Seeds", "stack": 15, "is_tool": False},
    {"slot": 6, "name": "Cauliflower Seeds", "stack": 12, "is_tool": False},
    {"slot": 7, "name": "Ancient Fruit Seeds", "stack": 999, "is_tool": False},
    {"slot": 8, "name": "Prismatic Shard", "stack": 3, "is_tool": False},
    {"slot": 9, "name": "Iridium Sprinkler", "stack": 24, "is_tool": False},
    {"slot": 10, "name": "Quality Fertilizer", "stack": 120, "is_tool": False},
    {"slot": 11, "name": "Warp Totem: Farm", "stack": 7, "is_tool": False},
]


def _inventaris(seq: int) -> dict:
    return {
        "active_slot": seq % 12,
        "hotbar": _HOTBAR_PENUH,
        "slots_used": 30,
        "items_total": 1000 + (seq % 500),
    }


def _state(
    seq: int, pid: int, screen: int, x: int, y: int, kirim_pada: float,
    *, inventaris: bool = True,
) -> dict:
    return {
        "protocol": 1, "ev": "state", "seq": seq,
        "player_id": pid, "screen_id": screen,
        "payload": {
            "location": "Farm",
            "tile": {"x": x, "y": y},
            "pixel": {"x": x * 64.0, "y": y * 64.0},
            "facing": (x + y) % 4,
            "time_of_day": 600 + (seq % 1000),
            "date": {"season": "spring", "day": 1, "year": 1},
            "energy": 270.0 - (seq % 100) * 0.1,
            "health": 100,
            "is_player_free": True,
            "can_player_move": True,
            "active_menu": None,
            # Dipakai mengukur latensi masuk->tersimpan. Field asing memang
            # ditoleransi skema, dan itu ikut teruji di sini.
            "_kirim_pada": kirim_pada,
            **({"inventory": _inventaris(seq)} if inventaris else {}),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detik", type=float, default=20.0)
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--port", type=int, default=PORT_SOAK)
    ap.add_argument(
        "--tanpa-inventaris", action="store_true",
        help="kirim payload lama (tanpa inventaris) untuk membandingkan",
    )
    a = ap.parse_args()

    if a.port == 3017:
        print("  [!] port 3017 dipakai receiver produksi — pilih port lain.")
        return 1

    store = StardewStateStore()
    diterima = {"n": 0, "tolak": 0}
    latensi: list[float] = []
    kunci = threading.Lock()

    def pada_event(event: dict) -> None:
        store.catat(event)
        with kunci:
            diterima["n"] += 1
            kirim = (event.get("payload") or {}).get("_kirim_pada")
            if isinstance(kirim, (int, float)):
                latensi.append((time.perf_counter() - kirim) * 1000.0)

    receiver = StardewReceiver(on_event=pada_event, port=a.port)
    receiver.start()
    alamat = receiver.address
    print(f"  soak {a.detik:.0f} dtk @ {a.hz:.0f} Hz x 2 farmer, port {alamat[1]}")

    utas_awal = threading.active_count()
    jeda = 1.0 / max(1.0, a.hz)
    terkirim = 0
    byte_terkirim = 0
    t0 = time.perf_counter()
    puncak_utas = utas_awal

    try:
        with socket.create_connection(alamat, timeout=5.0) as sock:
            for i, (pid, scr, nama, utama) in enumerate(
                [(P1, 0, "host1", True), (P2, 1, "artihand", False)], start=1
            ):
                sock.sendall(
                    (json.dumps(_hello(i, pid, scr, nama, utama)) + "\n").encode()
                )
                terkirim += 1
            time.sleep(0.2)
            store.pilih_target("artihand")

            seq = 10
            langkah = 0
            while time.perf_counter() - t0 < a.detik:
                mulai = time.perf_counter()
                langkah += 1
                for pid, scr, gerak in ((P1, 0, 0), (P2, 1, langkah % 40)):
                    seq += 1
                    baris = json.dumps(
                        _state(
                            seq, pid, scr, 10 + gerak, 20, time.perf_counter(),
                            inventaris=not a.tanpa_inventaris,
                        )
                    )
                    byte_terkirim += len(baris) + 1
                    sock.sendall(baris.encode() + b"\n")
                    terkirim += 1
                puncak_utas = max(puncak_utas, threading.active_count())
                sisa = jeda - (time.perf_counter() - mulai)
                if sisa > 0:
                    time.sleep(sisa)
            time.sleep(0.5)
    finally:
        receiver.stop()
        time.sleep(0.3)

    utas_akhir = threading.active_count()
    with kunci:
        n = diterima["n"]
        lat = sorted(latensi)

    print()
    print(f"  terkirim         : {terkirim}")
    print(
        f"  byte state       : {byte_terkirim:,} "
        f"({byte_terkirim / max(a.detik, 1e-9) / 1024:.1f} KiB/dtk, "
        f"{byte_terkirim / max(terkirim, 1):.0f} B/event)"
    )
    print(f"  tercatat         : {n}")
    print(f"  selisih (backlog): {terkirim - n}")
    print(f"  farmer di store  : {len(store.semua_identitas())} identitas, "
          f"{sum(1 for p in (P1, P2) if store.latest(p))} state")
    print(f"  utas awal/puncak/akhir: {utas_awal} / {puncak_utas} / {utas_akhir}")
    if lat:
        p50 = statistics.median(lat)
        p99 = lat[min(len(lat) - 1, int(len(lat) * 0.99))]
        print(f"  latensi masuk->simpan : p50 {p50:.2f} ms  p99 {p99:.2f} ms  "
              f"maks {lat[-1]:.2f} ms")

    snapP1, snapP2 = store.latest(P1), store.latest(P2)
    print()
    ok = True
    def cek(nama: str, syarat: bool, catatan: str = "") -> None:
        nonlocal ok
        ok = ok and syarat
        print(f"  [{'OK  ' if syarat else 'GAGAL'}] {nama}{catatan}")

    cek("nol event ditolak", terkirim == n, f"  (terkirim {terkirim}, tercatat {n})")
    cek("store tetap 2 farmer", len(store.semua_identitas()) == 2)
    cek("P1 tidak ikut bergerak", snapP1 is not None and snapP1.tile == (10, 20))
    cek("P2 bergerak", snapP2 is not None and snapP2.tile != (10, 20))
    cek("target terkunci ke P2", store.target == P2)
    cek("utas tidak bocor", utas_akhir <= utas_awal + 1,
        f"  (awal {utas_awal}, akhir {utas_akhir})")
    if lat:
        cek("latensi p99 < 50 ms", lat[min(len(lat) - 1, int(len(lat) * 0.99))] < 50.0)

    print()
    print("  SOAK OFFLINE: " + ("LULUS" if ok else "GAGAL"))
    print("  Catatan: ini butir ke-9 saja (leak/backlog/stutter). Delapan butir")
    print("  lain Gate 5 tetap menuntut game hidup.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
