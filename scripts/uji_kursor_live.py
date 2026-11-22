"""Probe live: tautkan pad, tutup menu kalau terbuka, jalan 1 tile.

Jangan kirim START kalau artihand sudah di dunia — itu membuka GameMenu
lagi, bukan join. Pad baru auto-bind ke farmhand yatim.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arti_gamepad import (
    GerbangTangan,
    MotorStardew,
    PadVirtual,
    WindowStardew,
    _join_lokal,
)
from arti_stardew import DEFAULT_PORT, StardewReceiver, StardewStateStore

TARGET = "artihand"


def _cetak_kursor(snap, tag: str) -> None:
    print(
        f"  [{tag}] menu={snap.active_menu!r} "
        f"snap_id={snap.menu_snap_id} label={snap.menu_snap_label!r} "
        f"close_id={snap.menu_close_id} "
        f"tile={snap.tile} free={snap.is_player_free}",
        flush=True,
    )


def main() -> int:
    store = StardewStateStore()
    receiver = StardewReceiver(store.catat, port=DEFAULT_PORT)
    receiver.start()
    pad = PadVirtual()
    window = WindowStardew()
    motor: MotorStardew | None = None
    print("[uji] receiver 3017 hidup. KLIK WINDOW STARDEW sekarang.", flush=True)
    print("[uji] menunggu fokus stabil (maks 180 dtk)...", flush=True)
    try:
        ok, alasan = _join_lokal(
            pad,
            window,
            sudah_join=lambda: store.pilih_target(TARGET) is not None,
            batas_fokus=180.0,
            tunggu_cek_join=4.0,
        )
        if not ok:
            print(f"[uji] JOIN GAGAL: {alasan}", flush=True)
            return 1
        print(f"[uji] {alasan}", flush=True)

        batas = time.monotonic() + 30.0
        while store.pilih_target(TARGET) is None:
            if time.monotonic() > batas:
                print("[uji] hello artihand tidak datang. Fail closed.", flush=True)
                return 1
            time.sleep(0.2)
        pid = store.target
        ident = store.identitas(pid) if pid is not None else None
        print(
            f"[uji] target {ident.player_name if ident else '?'} ({pid})",
            flush=True,
        )
        if pid is None:
            return 1

        gerbang = GerbangTangan(store, window=window)
        motor = MotorStardew(store, pad, gerbang)
        motor.aktifkan()
        motor.pasang_hotkey_darurat("ctrl+alt+s")

        snap = store.latest(pid)
        if snap is None:
            print("[uji] state kosong sesudah hello.", flush=True)
            return 1
        _cetak_kursor(snap, "awal")

        if snap.active_menu is not None:
            print("[uji] tutup_menu (B dulu, stik cadangan)...", flush=True)
            hasil = motor.tutup_menu()
            print(f"[uji] {hasil}", flush=True)
            if not hasil.berhasil:
                return 1
            snap = store.latest(pid)
            if snap is None:
                print("[uji] state hilang sesudah tutup_menu.", flush=True)
                return 1
            _cetak_kursor(snap, "sesudah-tutup")

        tx, ty = snap.tile
        tetangga = ((tx + 1, ty), (tx - 1, ty), (tx, ty - 1), (tx, ty + 1))
        gagal = []
        for tujuan in tetangga:
            print(f"[uji] jalan {snap.tile} -> {tujuan}", flush=True)
            hasil = motor.jalan_ke(*tujuan)
            print(f"[uji] {hasil}", flush=True)
            if hasil.berhasil:
                return 0
            gagal.append(f"{tujuan}:{hasil.alasan}")
            snap = store.latest(pid) or snap
        print("[uji] semua tetangga terhalang: " + "; ".join(gagal), flush=True)
        return 1
    finally:
        if motor is not None:
            motor.matikan()
        pad.tutup()
        receiver.stop()


if __name__ == "__main__":
    sys.exit(main())
