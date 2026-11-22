# -*- coding: utf-8 -*-
"""Dengarkan beberapa suara Gemini Live berurutan, kalimat sama.

Tiap suara: kirim kalimat pendek -> putar audionya ke output default ->
mulut kucing ikut bergerak. Diumumkan namanya sebelum bunyi.
"""
from __future__ import annotations
import argparse, asyncio, base64, json, os, sys, time

SR = 24000
VTS_PORT, VTS_NAMA, VTS_DEV = 8003, "Arti Kucing Probe", "bohanyt"
TOKEN_F = os.path.join("data", "vts_kucing_token.txt")
PERSONA = ("Kamu karakter contoh untuk uji suara. Jawab satu kalimat pendek.")


def kunci() -> str:
    for ln in open(".env", encoding="utf-8"):
        if ln.startswith("GEMINI_API_KEY"):
            return ln.split("=", 1)[1].strip()
    return ""


async def suara_dari(model: str, voice: str, teks: str, persona: str = ""):
    import websockets
    url = ("wss://generativelanguage.googleapis.com/ws/"
           "google.ai.generativelanguage.v1beta.GenerativeService."
           f"BidiGenerateContent?key={kunci()}")
    try:
        async with websockets.connect(url, max_size=16 << 20) as ws:
            await ws.send(json.dumps({"setup": {
                "model": f"models/{model}",
                "generationConfig": {"responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice}}}},
                "outputAudioTranscription": {},
                "systemInstruction": {"parts": [{"text": persona or PERSONA}]}}}))
            r = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if "setupComplete" not in r:
                return None, str(r)[:120]
            await ws.send(json.dumps({"clientContent": {
                "turns": [{"role": "user", "parts": [{"text": teks}]}],
                "turnComplete": True}}))
            buf, ucap = [], []
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 45))
                sc = m.get("serverContent") or {}
                tr = (sc.get("outputTranscription") or {}).get("text")
                if tr:
                    ucap.append(tr)
                for p in ((sc.get("modelTurn") or {}).get("parts") or []):
                    d = p.get("inlineData") or {}
                    if d.get("data"):
                        buf.append(base64.b64decode(d["data"]))
                if sc.get("turnComplete"):
                    break
            return b"".join(buf), ("".join(ucap).strip() or None)
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:110]}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suara", default="Aoede,Kore,Leda,Zephyr,Callirrhoe,Autonoe")
    ap.add_argument("--teks", default="Halo semuanya, ini uji suara!")
    ap.add_argument("--model", default="gemini-3.1-flash-live-preview")
    ap.add_argument("--persona", default="", help="timpa persona bawaan")
    ap.add_argument("--baca", action="store_true",
                    help="ucapkan teks PERSIS (bukan menjawabnya)")
    a = ap.parse_args()

    import numpy as np, sounddevice as sd, websockets
    ws = await websockets.connect(f"ws://127.0.0.1:{VTS_PORT}", max_size=8 << 20)

    async def minta(t, d=None):
        await ws.send(json.dumps({"apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0", "requestID": f"s-{t}", "messageType": t,
            "data": d or {}}))
        return json.loads(await ws.recv())

    await minta("AuthenticationRequest", {"pluginName": VTS_NAMA,
        "pluginDeveloper": VTS_DEV,
        "authenticationToken": open(TOKEN_F, encoding="utf-8").read().strip()})
    nm = str((await minta("CurrentModelRequest")).get("data", {}).get("modelName"))
    if "arti" in nm.lower() or "mymodel_vts" in nm.lower():
        print("ABORT — model Arti"); return 2

    async def mulut(v):
        await minta("InjectParameterDataRequest", {"faceFound": False,
            "mode": "set", "parameterValues": [
                {"id": "VoiceVolume", "weight": 1.0, "value": round(v, 4)},
                {"id": "MouthOpen", "weight": 1.0, "value": round(v, 4)}]})

    try:
        for v in [x.strip() for x in a.suara.split(",") if x.strip()]:
            print(f"\n>>> SUARA: {v}", flush=True)
            t0 = time.perf_counter()
            persona = a.persona
            if a.baca:
                persona = ("Ucapkan PERSIS teks yang diberikan pengguna, kata per kata. "
                           "JANGAN menjawab, JANGAN menambah, JANGAN mengurangi.")
            pcm, err = await suara_dari(a.model, v, a.teks, persona)
            if not pcm:
                print(f"    GAGAL: {err}"); continue
            if err:
                print(f"    ucap: {err[:110]}")
            au = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            dur = len(au) / SR
            print(f"    {dur:.1f} dtk (ambil {time.perf_counter()-t0:.1f} dtk) — DENGAR")
            sd.play(au, SR)
            t1 = time.perf_counter()
            while (e := time.perf_counter() - t1) < dur:
                i = int(e * SR); blok = au[i:i + SR // 30]
                r = float(np.sqrt(np.mean(blok ** 2))) if len(blok) else 0.0
                await mulut(min(1.0, r * 6.0))
                await asyncio.sleep(1 / 30)
            sd.wait()
            await mulut(0.0)
            await asyncio.sleep(0.6)
    finally:
        for _ in range(3):
            await mulut(0.0)
        await ws.close()
    print("\n[Uji] SELESAI — suara mana yang paling cocok buat kucing?")
    return 0

sys.exit(asyncio.run(main()))
