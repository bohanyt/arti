"""Uji Minecraft Arti TANPA audio — untuk sesi remote.

Yang paling belum terbukti dari pekerjaan 6-7 Agustus semuanya visual/teks:
kemampuan makan, dan kamera penonton yang harus mengunci ulang sendiri tiap
Arti mati. Dua-duanya tidak butuh TTS, mic, maupun speaker.

Skrip ini menjalankan bot sungguhan + hook kamera ASLI dari bridge (bukan
tiruan), jadi yang diuji memang jalur yang dipakai saat siaran. Yang TIDAK
dijalankan: TTS, mic, YouTube, VTube Studio.

    ./venv/Scripts/python.exe scripts/tes_tanpa_audio.py
    ./venv/Scripts/python.exe scripts/tes_tanpa_audio.py --lewati-kamera

Syarat: server Minecraft sudah hidup. Untuk bagian kamera, klien Prism harus
sudah join dengan username sesuai `minecraft_spectator_name` di config_local.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arti_minecraft as am  # noqa: E402
import arti_spectator as sp  # noqa: E402
from scripts.mc_rcon import rcon  # noqa: E402


def _judul(teks: str) -> None:
    print(f"\n{'=' * 62}\n  {teks}\n{'=' * 62}")


def _tunggu(cek, batas_dtk: float = 40.0, jeda: float = 2.0):
    """Tunggu sampai `cek()` mengembalikan nilai benar; None kalau kehabisan."""
    habis = time.time() + batas_dtk
    while time.time() < habis:
        hasil = cek()
        if hasil:
            return hasil
        time.sleep(jeda)
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lewati-kamera", action="store_true",
                   help="hanya uji makan (klien Prism belum jalan)")
    p.add_argument("--lewati-makan", action="store_true")
    a = p.parse_args()

    import arti_bridge as bridge   # hook kamera yang ASLI
    cfg = dict(bridge.CONFIG)
    cfg["minecraft_pov_enabled"] = False    # POV web tidak relevan di sini
    cfg["minecraft_status_interval_sec"] = 3
    nama = cfg.get("minecraft_bot_name", "Arti")

    try:
        rcon(["list"])
    except Exception as e:  # noqa: BLE001
        print(f"\nServer belum bisa dihubungi lewat RCON ({type(e).__name__}: {e}).")
        print("Nyalakan start_server.bat dulu.")
        return 1

    kejadian: list = []

    def _rekam(ev):
        # task_failed SENGAJA tidak pernah jadi reaksi suara, jadi kalau cuma
        # mendengarkan queue_reaction, penolakan "sudah kenyang" tak terlihat.
        if ev.get("ev") in ("ate", "task_failed", "death", "respawn"):
            kejadian.append(("event", ev))
        bridge._spectator_on_event(ev)             # jalur kamera yang sungguhan

    runner = am.MinecraftRunner(cfg, {
        "queue_reaction": lambda t: kejadian.append(("reaksi", t)),
        "add_history": lambda *x: None,
        "spectator": _rekam,
    })
    if not runner.start():
        print("Bot gagal dijalankan.")
        return 1
    print(f"\nBot '{nama}' masuk dunia, tunggu sebentar...")
    time.sleep(10)

    lulus, gagal = [], []

    try:
        # ---------------------------------------------------------------
        if not a.lewati_makan:
            _judul("1. MAKAN")
            rcon(["time set day",
                  f"effect give {nama} minecraft:resistance 999 4 true",
                  f"clear {nama}",
                  f"give {nama} minecraft:bread 3",
                  f"give {nama} minecraft:cooked_beef 2"])
            time.sleep(2)

            print("  a. perut penuh -> harus DITOLAK, bukan diam")
            # Saturation mengisi bar makanan seketika — tanpa ini, perut Arti
            # bisa saja sudah tidak penuh dari sesi sebelumnya dan dia justru
            # makan, bukan menolak.
            rcon([f"effect give {nama} minecraft:saturation 1 20 true"])
            _tunggu(lambda: (runner.last_status or {}).get("food") == 20, 20)
            kejadian.clear()
            runner.send_command({"cmd": "eat"})
            time.sleep(5)
            tolak = any(k[0] == "event" and k[1].get("reason") == "sudah_kenyang"
                        for k in kejadian)
            print("     ->", "OK (ditolak)" if tolak else "GAGAL (tidak ada tanggapan)")
            (lulus if tolak else gagal).append("tolak saat kenyang")

            print("  b. bikin lapar lalu makan")
            # Amplifier SEDANG. Yang 255 menguras sampai 0 DAN menumpuk
            # exhaustion sedemikian rupa sehingga makanan yang baru masuk
            # langsung terkuras lagi — hasilnya "food tidak naik" yang
            # menyesatkan (terjadi 2026-08-07). Cukup bikin lapar, jangan
            # menyiksa.
            rcon([f"effect give {nama} minecraft:hunger 40 20 true"])
            lapar = _tunggu(
                lambda: 4 <= (runner.last_status or {}).get("food", 20) <= 17, 80)
            rcon([f"effect clear {nama} minecraft:hunger"])
            time.sleep(3)   # biarkan exhaustion mengendap dulu
            sebelum = (runner.last_status or {}).get("food")
            if not lapar:
                print(f"     tidak berhasil bikin lapar (food {sebelum}) — "
                      "bagian ini dilewati, bukan berarti makannya rusak")
                gagal.append("gagal menyiapkan kondisi lapar (bukan bug makan)")
                sebelum = None
            else:
                print(f"     food sebelum: {sebelum}")
            if sebelum is not None:
                kejadian.clear()
                runner.send_command({"cmd": "eat"})
                time.sleep(6)
                for k in kejadian:
                    print(f"     {k[0]}: {str(k[1])[:110]}")
                _tunggu(lambda: (runner.last_status or {}).get("food", 0) > sebelum, 12)
                sesudah = (runner.last_status or {}).get("food")
                print(f"     food sesudah: {sesudah}")
                naik = sesudah is not None and sesudah > sebelum
                print("     ->", "OK (perut terisi)" if naik
                      else "GAGAL (food tidak naik)")
                (lulus if naik else gagal).append("makan menaikkan food")

        # ---------------------------------------------------------------
        if not a.lewati_kamera:
            _judul("2. KAMERA PENONTON")
            if not sp.is_enabled(cfg):
                print("  minecraft_spectator_name kosong — kamera mati, dilewati.")
            else:
                penonton = cfg["minecraft_spectator_name"]
                online = rcon(["list"])[0]
                print(f"  pemain online: {online.strip()}")
                if penonton.lower() not in online.lower():
                    print(f"  '{penonton}' BELUM join — jalankan klien Prism dulu.")
                    gagal.append("klien kamera belum join")
                else:
                    print("  a. kunci kamera ke Arti")
                    balasan = rcon(sp.build_commands(cfg))
                    salah = sp.result_failed(balasan)
                    print("     ->", f"GAGAL: {salah}" if salah else "OK — cek window Prism")
                    (gagal if salah else lulus).append("kunci kamera")

                    if not salah:
                        print("  b. bunuh Arti -> kamera HARUS terkunci ulang sendiri")
                        print("     (perhatikan baris '[Kamera] Terkunci ke Arti lagi')")
                        rcon([f"kill {nama}"])
                        time.sleep(12)
                        print("     -> lihat window Prism: masih mengikuti Arti?")
                        lulus.append("re-lock sesudah mati (cek mata)")
    finally:
        try:
            rcon([f"effect clear {nama}"])
        except Exception:  # noqa: BLE001
            pass
        runner.stop()
        time.sleep(1)

    _judul("RINGKASAN")
    for x in lulus:
        print("  OK    ", x)
    for x in gagal:
        print("  GAGAL ", x)
    print("\nYang TIDAK diuji di sini (butuh audio): refleks 'aduh!', suara "
          "kepotong, gerbang audio bersama.")
    return 1 if gagal else 0


if __name__ == "__main__":
    raise SystemExit(main())
