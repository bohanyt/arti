# -*- coding: utf-8 -*-
"""Uji TAHAN: mulut kucing dibuka PENUH 15 dtk (tidak mungkin terlewat)."""
from __future__ import annotations
import asyncio, json, os, sys, time

PORT, NAMA, DEV = 8003, "Arti Kucing Probe", "bohanyt"
TOKEN_F = os.path.join("data", "vts_kucing_token.txt")

async def utama():
    import websockets
    async with websockets.connect(f"ws://127.0.0.1:{PORT}", max_size=8 << 20) as ws:
        async def minta(t, d=None):
            await ws.send(json.dumps({"apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0", "requestID": f"u4-{t}", "messageType": t,
                "data": d or {}}))
            return json.loads(await ws.recv())
        tok = open(TOKEN_F, encoding="utf-8").read().strip()
        await minta("AuthenticationRequest", {"pluginName": NAMA,
            "pluginDeveloper": DEV, "authenticationToken": tok})
        nama = str((await minta("CurrentModelRequest")).get("data", {}).get("modelName"))
        print(f"[Uji4] Model: {nama!r}")
        if "arti" in nama.lower() or "mymodel_vts" in nama.lower():
            print("[Uji4] ABORT — model Arti"); return 2

        async def suntik(v):
            return await minta("InjectParameterDataRequest", {"faceFound": False,
                "mode": "set", "parameterValues": [
                    {"id": "VoiceVolume", "weight": 1.0, "value": v},
                    {"id": "MouthOpen", "weight": 1.0, "value": v},
                    {"id": "VoiceVolumePlusMouthOpen", "weight": 1.0, "value": v}]})

        print(">>> MULUT DIBUKA PENUH SEKARANG — TAHAN 15 DETIK, lihat kapan saja <<<")
        t0 = time.perf_counter()
        try:
            while (e := time.perf_counter() - t0) < 15.0:
                await suntik(1.0)
                await asyncio.sleep(1 / 20)
                if int(e) != int(e - 0.05) and int(e) % 5 == 0 and e >= 5:
                    print(f"    ...{int(e)} dtk berlalu, mulut masih dibuka")
        finally:
            for _ in range(4):
                await suntik(0.0)
        print("[Uji4] Ditutup kembali. Mulutnya tadi TERBUKA lebar 15 detik penuh?")
    return 0

sys.exit(asyncio.run(utama()))
