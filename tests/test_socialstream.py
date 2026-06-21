"""Tes diagnostik Social Stream Ninja - coba semua session + channel"""
import asyncio
import websockets
import json
import sys
import io

# Fix Windows encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SESSIONS = ["TEST_SESSION_01", "TEST_SESSION_02"]
CHANNELS = ["", "/1", "/2", "/3", "/4"]

async def listen(session_id, channel, timeout=15):
    label = f"Session={session_id} Channel={channel or 'default'}"
    uri = f"wss://io.socialstream.ninja/join/{session_id}{channel}"
    try:
        async with websockets.connect(uri, open_timeout=5) as ws:
            print(f"  [OK] [{label}] Terhubung! Menunggu pesan {timeout}s...")
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                preview = msg[:200] if len(msg) > 200 else msg
                print(f"  [DATA!] [{label}] DAPAT PESAN: {preview}")
                return True
            except asyncio.TimeoutError:
                print(f"  [EMPTY] [{label}] Tidak ada pesan dalam {timeout}s")
                return False
    except Exception as e:
        print(f"  [ERR] [{label}] Error: {e}")
        return False

async def main():
    print("=" * 70)
    print("DIAGNOSTIK SOCIAL STREAM NINJA")
    print("=" * 70)
    print(f"\nMencoba {len(SESSIONS)} session x {len(CHANNELS)} channel...")
    print("Pastikan ada orang yang kirim chat di YouTube live kamu SEKARANG!\n")
    
    # Coba semua kombinasi secara paralel
    tasks = []
    for sid in SESSIONS:
        for ch in CHANNELS:
            tasks.append(listen(sid, ch, timeout=15))
    
    results = await asyncio.gather(*tasks)
    
    print("\n" + "=" * 70)
    print("HASIL:")
    i = 0
    for sid in SESSIONS:
        for ch in CHANNELS:
            status = "[DATA!]" if results[i] else "[EMPTY]"
            print(f"  {status} | Session={sid} Channel={ch or 'default'}")
            i += 1
    print("=" * 70)

asyncio.run(main())
