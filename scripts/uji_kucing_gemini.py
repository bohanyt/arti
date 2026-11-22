# -*- coding: utf-8 -*-
"""Uji perdana KUCING: Gemini Live API -> suara + mulut VTS bergerak.

Alur: kirim teks -> Gemini Live balas AUDIO (suara perempuan) -> audio
diputar ke OUTPUT DEFAULT (ikut perangkat output default) -> amplitudonya
dihitung dan disuntikkan ke VTS kucing (port 8003) sebagai VoiceVolume,
sehingga mulut kucing bergerak PAS dengan suaranya.

Pagar: verifikasi model VTS bukan Arti sebelum menulis; faceFound tetap
false; mulut selalu direset 0 di finally; port Arti (8002) tak disentuh.
"""
from __future__ import annotations
import argparse, asyncio, base64, json, os, sys, time

SR_OUT = 24000          # Live API mengeluarkan PCM16 24 kHz mono
VTS_PORT, VTS_NAMA, VTS_DEV = 8003, "Arti Kucing Probe", "bohanyt"
TOKEN_F = os.path.join("data", "vts_kucing_token.txt")


def kunci_gemini() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    for ln in open(".env", encoding="utf-8"):
        if ln.startswith("GEMINI_API_KEY"):
            return ln.split("=", 1)[1].strip()
    return ""


async def ambil_suara(model: str, suara: str, teks: str, persona: str) -> bytes:
    import websockets
    key = kunci_gemini()
    url = ("wss://generativelanguage.googleapis.com/ws/"
           "google.ai.generativelanguage.v1beta.GenerativeService."
           f"BidiGenerateContent?key={key}")
    print(f"[Gemini] connect | model={model} | suara={suara}")
    async with websockets.connect(url, max_size=16 << 20) as ws:
        await ws.send(json.dumps({"setup": {
            "model": f"models/{model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": suara}}},
            },
            "outputAudioTranscription": {},
            "systemInstruction": {"parts": [{"text": persona}]},
        }}))
        r = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if "setupComplete" not in r:
            print(f"[Gemini] setup gagal: {str(r)[:300]}")
            return b""
        print("[Gemini] setup OK, kirim teks...")
        t0 = time.perf_counter()
        await ws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": teks}]}],
            "turnComplete": True}}))

        potongan, pertama, ucapan = [], None, []
        while True:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 45))
            except asyncio.TimeoutError:
                print("[Gemini] timeout menunggu audio"); break
            sc = msg.get("serverContent") or {}
            tr = (sc.get("outputTranscription") or {}).get("text")
            if tr:
                ucapan.append(tr)
            for p in ((sc.get("modelTurn") or {}).get("parts") or []):
                d = p.get("inlineData") or {}
                if d.get("data"):
                    if pertama is None:
                        pertama = time.perf_counter() - t0
                        print(f"[Gemini] audio pertama tiba: {pertama:.2f} dtk")
                    potongan.append(base64.b64decode(d["data"]))
                if p.get("text"):
                    print(f"[Gemini] teks: {p['text'][:120]}")
            if sc.get("turnComplete"):
                selesai = time.perf_counter() - t0
                print(f"[Gemini] turn selesai ({selesai:.2f} dtk, "
                      f"{sum(len(x) for x in potongan)} byte audio)")
                break
        return b"".join(potongan), {
            "detik_audio_pertama": round(pertama or 0.0, 3),
            "detik_turn_selesai": round(selesai, 3),
            "transkrip": "".join(ucapan).strip(),
        }


async def vts_sesi():
    import websockets
    ws = await websockets.connect(f"ws://127.0.0.1:{VTS_PORT}", max_size=8 << 20)

    async def minta(t, d=None):
        await ws.send(json.dumps({"apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0", "requestID": f"k-{t}", "messageType": t,
            "data": d or {}}))
        return json.loads(await ws.recv())

    tok = open(TOKEN_F, encoding="utf-8").read().strip()
    await minta("AuthenticationRequest", {"pluginName": VTS_NAMA,
        "pluginDeveloper": VTS_DEV, "authenticationToken": tok})
    nama = str((await minta("CurrentModelRequest")).get("data", {}).get("modelName"))
    if "arti" in nama.lower() or "mymodel_vts" in nama.lower():
        await ws.close()
        raise SystemExit("[VTS] ABORT — port ini model ARTI, bukan kucing.")
    print(f"[VTS] tersambung, model {nama!r}")
    return ws, minta


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teks", default="Halo! Kenalin dong, kamu siapa? "
                                      "Jawab singkat aja ya, dua kalimat.")
    ap.add_argument("--suara", default="Aoede", help="Aoede/Kore/Leda/Zephyr = perempuan")
    ap.add_argument("--model", default="gemini-3.1-flash-live-preview")
    ap.add_argument("--tanpa-vts", action="store_true")
    ap.add_argument("--persona", default="", help="timpa persona bawaan")
    a = ap.parse_args()

    persona = a.persona or (
        "Kamu karakter contoh untuk uji suara. Jawab singkat dalam satu atau dua kalimat.")
    pcm, metrik = await ambil_suara(a.model, a.suara, a.teks, persona)
    if not pcm:
        print("[Uji] tidak ada audio — berhenti."); return 1

    import wave

    import numpy as np
    import sounddevice as sd

    os.makedirs(os.path.join("data", "kucing", "rekaman"), exist_ok=True)
    cap = time.strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join("data", "kucing", "rekaman", f"{cap}_{a.suara}.wav")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_OUT)
        w.writeframes(pcm)
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    durasi = len(audio) / SR_OUT
    metrik.update({
        "waktu": cap, "suara": a.suara, "model": a.model,
        "teks_masuk": a.teks, "detik_audio": round(durasi, 3),
        "byte": len(pcm), "wav": wav_path,
    })
    with open(os.path.join("data", "kucing", "catatan_kucing.jsonl"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(metrik, ensure_ascii=False) + chr(10))
    print(f"[Audio] {durasi:.2f} dtk @ {SR_OUT} Hz -> perangkat default")
    print(f"[Rekam] {wav_path}")
    print(f"[Ucap ] {metrik['transkrip'][:150]}")
    print(f"[Waktu] audio pertama {metrik['detik_audio_pertama']:.2f} dtk | "
          f"turn selesai {metrik['detik_turn_selesai']:.2f} dtk | "
          f"lama bicara {durasi:.2f} dtk")

    ws = minta = None
    if not a.tanpa_vts:
        ws, minta = await vts_sesi()

    async def mulut(v: float):
        if minta is None:
            return
        await minta("InjectParameterDataRequest", {"faceFound": False,
            "mode": "set", "parameterValues": [
                {"id": "VoiceVolume", "weight": 1.0, "value": round(v, 4)},
                {"id": "MouthOpen", "weight": 1.0, "value": round(v, 4)}]})

    try:
        sd.play(audio, SR_OUT)          # perangkat default = ikut examplebuds/speaker
        t0 = time.perf_counter()
        hop = 1 / 30
        while (e := time.perf_counter() - t0) < durasi:
            i = int(e * SR_OUT)
            blok = audio[i:i + int(SR_OUT * hop)]
            rms = float(np.sqrt(np.mean(blok ** 2))) if len(blok) else 0.0
            await mulut(min(1.0, rms * 6.0))   # skala kasar -> 0..1
            await asyncio.sleep(hop)
        sd.wait()
    finally:
        for _ in range(3):
            await mulut(0.0)
        if ws is not None:
            await ws.close()
    print("[Uji] SELESAI — suara habis, mulut ditutup.")
    return 0


sys.exit(asyncio.run(main()))
