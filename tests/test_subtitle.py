#!/usr/bin/env python3
"""Quick test: kirim phrase timings ke subtitle server."""

import asyncio
import json
import websockets

# Test data — mimics Supertone output with estimated phrase timings
TEST_PHRASES = [
    {"word": "Halo guys!", "start": 0.0, "duration": 0.8},
    {"word": "Aku Arti ya", "start": 0.9, "duration": 1.2},
    {"word": "yang bakal temenin Streamer hari ini", "start": 2.2, "duration": 2.5},
    {"word": "Semangat stream-nya!", "start": 4.8, "duration": 1.0},
]

async def test_subtitle():
    uri = "ws://localhost:9988"
    try:
        async with websockets.connect(uri) as ws:
            # Send subtitle data
            msg = json.dumps({
                "type": "subtitle",
                "words": TEST_PHRASES,
                "text": " ".join(p["word"] for p in TEST_PHRASES)
            })
            await ws.send(msg)
            print(f"[OK] Sent {len(TEST_PHRASES)} phrases to subtitle server")
            print(f"     Buka OBS Browser Source untuk liat hasilnya")
            
            # Keep alive a bit
            await asyncio.sleep(2)
    except ConnectionRefusedError:
        print("[ERROR] Subtitle server tidak jalan di port 9999")
        print("        Jalankan: python hermes_vtuber_bridge.py")

if __name__ == "__main__":
    asyncio.run(test_subtitle())
