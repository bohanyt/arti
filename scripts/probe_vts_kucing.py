# -*- coding: utf-8 -*-
"""Probe READ-ONLY VTS instance KUCING (port 8003) — nol tulisan parameter.

Pagar keselamatan (karena ADA DUA instance VTS menyala):
  1. Hanya menyambung ke port yang diberikan (default 8003). Port Arti
     (8002) tidak pernah disentuh.
  2. Sesudah autentikasi, WAJIB memastikan model yang aktif BUKAN model
     Arti. Kalau ternyata model Arti -> langsung keluar, tanpa meminta
     apa pun lagi.
  3. Token disimpan TERPISAH (data/vts_kucing_token.txt) supaya token
     plugin Arti (vts_token.txt) tidak pernah tertimpa.
  4. Tidak ada InjectParameterData, tidak ada trigger hotkey, tidak ada
     perubahan apa pun. Murni bertanya.

Yang ditanyakan: daftar parameter input yang tersedia — untuk menjawab
"boleh tidak plugin menyuntikkan VoiceVolume" (kalau boleh, pemetaan
mulut kucing yang sudah dibuat Bohan dipakai apa adanya).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

NAMA_PLUGIN = "Arti Kucing Probe"
PENGEMBANG = "bohanyt"
BERKAS_TOKEN = os.path.join("data", "vts_kucing_token.txt")
# Petunjuk model ARTI — kalau ini yang muncul, ABORT.
PENANDA_MODEL_ARTI = ("mymodel_vts", "arti")


async def utama(port: int) -> int:
    import websockets

    url = f"ws://127.0.0.1:{port}"
    print(f"[Probe] Menyambung ke {url} (hanya port ini)")
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        async def minta(tipe: str, data: dict | None = None) -> dict:
            await ws.send(json.dumps({
                "apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                "requestID": f"probe-{tipe}", "messageType": tipe,
                "data": data or {},
            }))
            return json.loads(await ws.recv())

        st = await minta("APIStateRequest")
        d = st.get("data", {})
        print(f"[Probe] VTS {d.get('vTubeStudioVersion')} | aktif={d.get('active')} "
              f"| sudah terautentikasi={d.get('currentSessionAuthenticated')}")

        token = None
        if os.path.isfile(BERKAS_TOKEN):
            token = open(BERKAS_TOKEN, encoding="utf-8").read().strip() or None
            print("[Probe] Token lama ditemukan, dipakai ulang (tanpa dialog).")
        if not token:
            print("[Probe] Meminta token — DIALOG IZIN akan muncul di jendela VTS "
                  "KUCING. Klik Allow.")
            r = await minta("AuthenticationTokenRequest", {
                "pluginName": NAMA_PLUGIN, "pluginDeveloper": PENGEMBANG})
            token = (r.get("data") or {}).get("authenticationToken")
            if not token:
                print(f"[Probe] Ditolak / gagal: {r.get('data')}")
                return 1
            os.makedirs("data", exist_ok=True)
            with open(BERKAS_TOKEN, "w", encoding="utf-8") as f:
                f.write(token)
            print(f"[Probe] Token disimpan -> {BERKAS_TOKEN}")

        a = await minta("AuthenticationRequest", {
            "pluginName": NAMA_PLUGIN, "pluginDeveloper": PENGEMBANG,
            "authenticationToken": token})
        if not (a.get("data") or {}).get("authenticated"):
            print(f"[Probe] Autentikasi gagal: {a.get('data')}")
            return 1

        m = await minta("CurrentModelRequest")
        md = m.get("data", {})
        nama = str(md.get("modelName", "?"))
        print(f"[Probe] Model aktif di port {port}: {nama!r}")
        if any(p in nama.lower() for p in PENANDA_MODEL_ARTI):
            print("[Probe] ABORT — ini kelihatannya model ARTI, bukan kucing. "
                  "Tidak ada permintaan lain yang dikirim.")
            return 2

        p = await minta("InputParameterListRequest")
        pd = p.get("data", {})
        default = pd.get("defaultParameters", []) or []
        custom = pd.get("customParameters", []) or []
        print(f"[Probe] Parameter bawaan: {len(default)} | custom: {len(custom)}")
        penting = [x for x in default
                   if any(k in str(x.get("name", "")).lower()
                          for k in ("voice", "mouth"))]
        print("[Probe] Yang relevan untuk mulut/suara:")
        for x in penting:
            print(f"   - {x.get('name'):22} min={x.get('min')} max={x.get('max')} "
                  f"default={x.get('defaultValue')} nilai_sekarang={x.get('value')}")
        if not penting:
            print("   (tidak ada — laporkan daftar lengkapnya)")
            for x in default[:25]:
                print(f"   . {x.get('name')}")
        print("[Probe] SELESAI — nol parameter ditulis, nol hotkey dipicu.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8003)
    a = ap.parse_args()
    sys.exit(asyncio.run(utama(a.port)))
