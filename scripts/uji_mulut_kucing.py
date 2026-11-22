# -*- coding: utf-8 -*-
"""Uji TULIS terkecil: gerakkan mulut kucing lewat VTS API (port 8003).

Membuktikan Jalan B: kalau plugin boleh menyuntikkan VoiceVolume, maka
pemetaan mulut yang SUDAH dibuat Bohan (VoiceVolume -> PARAM_MOUTH_FORM /
PARAM_MOUTH_OPEN_Y) dipakai apa adanya, dan kita tidak butuh kabel audio
virtual kedua sama sekali.

Pagar yang sama dengan probe:
  - hanya port yang diberikan (default 8003), port Arti tidak disentuh
  - WAJIB memastikan model aktif BUKAN model Arti sebelum menulis apa pun
  - token terpisah (data/vts_kucing_token.txt)
  - durasi dibatasi (default 3 detik), lalu SELALU direset ke 0 di finally
  - satu parameter saja; tidak menyentuh mata, tidak menyentuh faceFound
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time

NAMA_PLUGIN = "Arti Kucing Probe"     # sama dgn probe -> token lama dipakai
PENGEMBANG = "bohanyt"
BERKAS_TOKEN = os.path.join("data", "vts_kucing_token.txt")
PENANDA_MODEL_ARTI = ("mymodel_vts", "arti")


async def utama(port: int, detik: float, param: str, fps: int) -> int:
    import websockets

    url = f"ws://127.0.0.1:{port}"
    print(f"[Uji] {url} | parameter={param} | {detik:.0f} dtk @ {fps} FPS")
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        async def minta(tipe: str, data: dict | None = None) -> dict:
            await ws.send(json.dumps({
                "apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                "requestID": f"uji-{tipe}", "messageType": tipe,
                "data": data or {},
            }))
            return json.loads(await ws.recv())

        token = open(BERKAS_TOKEN, encoding="utf-8").read().strip() if os.path.isfile(BERKAS_TOKEN) else ""
        if not token:
            print("[Uji] Token belum ada — jalankan probe_vts_kucing.py dulu.")
            return 1
        a = await minta("AuthenticationRequest", {
            "pluginName": NAMA_PLUGIN, "pluginDeveloper": PENGEMBANG,
            "authenticationToken": token})
        if not (a.get("data") or {}).get("authenticated"):
            print(f"[Uji] Autentikasi gagal: {a.get('data')}")
            return 1

        m = await minta("CurrentModelRequest")
        nama = str((m.get("data") or {}).get("modelName", "?"))
        print(f"[Uji] Model aktif: {nama!r}")
        if any(p in nama.lower() for p in PENANDA_MODEL_ARTI):
            print("[Uji] ABORT — ini model ARTI. Tidak menulis apa pun.")
            return 2

        async def suntik(nilai: float) -> dict:
            return await minta("InjectParameterDataRequest", {
                "faceFound": False,          # JANGAN ubah: auto-blink milik VTS
                "mode": "set",
                "parameterValues": [
                    {"id": param, "weight": 1.0, "value": round(nilai, 4)}],
            })

        # cek balasan pertama: ditolak atau diterima
        r0 = await suntik(0.0)
        pesan = r0.get("messageType")
        if pesan == "APIError":
            print(f"[Uji] DITOLAK VTS: {r0.get('data')}")
            print("[Uji] -> coba lagi dengan --param PARAM_MOUTH_OPEN_Y")
            return 3
        print(f"[Uji] Injeksi diterima ({pesan}). Mulai 'bicara' {detik:.0f} dtk — "
              "LIHAT MULUT KUCINGNYA.")

        try:
            t0 = time.perf_counter()
            dt = 1.0 / max(4, fps)
            while (elapsed := time.perf_counter() - t0) < detik:
                # pola bicara: suku kata ~4/dtk, amplitudo naik-turun
                v = abs(math.sin(elapsed * math.pi * 4)) * (0.55 + 0.45 * math.sin(elapsed))
                await suntik(max(0.0, min(1.0, v)))
                await asyncio.sleep(dt)
        finally:
            for _ in range(3):
                await suntik(0.0)
            print("[Uji] Selesai — parameter direset ke 0 (mulut kembali ke VTS).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8003)
    ap.add_argument("--detik", type=float, default=3.0)
    ap.add_argument("--param", default="VoiceVolume")
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args()
    sys.exit(asyncio.run(utama(a.port, a.detik, a.param, a.fps)))
