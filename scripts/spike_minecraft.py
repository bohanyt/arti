"""Spike Phase 0: bot Minecraft Arti join server lokal + round-trip NDJSON.

Dua mode:
  --auto : smoke non-interaktif — spawn bot, tunggu `spawned` + 2x `status`,
           kirim say+status+quit, lapor GO/NO-GO. Dipakai Claude/CI.
  (tanpa flag) : interaktif — ketik perintah (follow/come/say .../stop/status/
           quit) sambil Bohan main; semua event dicetak. Dipakai Bohan.

Prasyarat: server jalan (start_server.bat), `npm install` di mc-bot/ sudah.

    ./venv/Scripts/python.exe scripts/spike_minecraft.py --auto
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "mc-bot", "bot.js")


def _local_cfg() -> dict:
    """Nama bot & streamer per-mesin dari config_local.json (repo tetap generik)."""
    try:
        with open(os.path.join(ROOT, "config_local.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


_CFG = _local_cfg()
HOST = str(_CFG.get("minecraft_host", "127.0.0.1"))
PORT = str(_CFG.get("minecraft_port", "25565"))
USERNAME = str(_CFG.get("minecraft_bot_name", "Arti"))
STREAMER = str(_CFG.get("minecraft_streamer_name", "Bohan"))


def main() -> int:
    auto = "--auto" in sys.argv
    proc = subprocess.Popen(
        ["node", BOT, "--host", HOST, "--port", PORT,
         "--username", USERNAME, "--streamer", STREAMER,
         "--status-interval", "5"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=ROOT,
    )

    events: list[dict] = []
    lock = threading.Lock()

    def reader():
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                print(f"[??] non-JSON di stdout: {line[:80]}")
                continue
            with lock:
                events.append(ev)
            print(f"[EV] {ev.get('ev')}: "
                  f"{ {k: v for k, v in ev.items() if k not in ('ev', 'ts')} }")

    def errdrain():
        for line in proc.stderr:  # type: ignore[union-attr]
            print(f"[MC-bot] {line.rstrip()}")

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=errdrain, daemon=True).start()

    def send(cmd: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(cmd) + "\n")
        proc.stdin.flush()

    def count(name: str) -> int:
        with lock:
            return sum(1 for e in events if e.get("ev") == name)

    if auto:
        deadline = time.time() + 30
        while time.time() < deadline and not count("spawned"):
            if proc.poll() is not None:
                print(f"NO-GO: bot exit dini rc={proc.returncode}")
                return 1
            time.sleep(0.5)
        if not count("spawned"):
            print("NO-GO: tidak ada event spawned dalam 30 dtk")
            proc.terminate()
            return 1
        print("[Spike] spawned OK — nunggu 2x status (bukti ticker)...")
        deadline = time.time() + 15
        while time.time() < deadline and count("status") < 2:
            time.sleep(0.5)
        send({"cmd": "say", "text": "halo dari spike Arti"})
        send({"cmd": "status"})
        time.sleep(2)
        send({"cmd": "quit"})
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
        # >=2: satu dari ticker + satu on-demand cukup membuktikan dua arah;
        # ambang 3 sempat flaky (race quit vs ticker 5 dtk).
        ok = count("spawned") >= 1 and count("status") >= 2
        print(f"[Spike] spawned={count('spawned')} status={count('status')} "
              f"exit={proc.returncode}")
        print("GO" if ok else "NO-GO")
        return 0 if ok else 1

    print("Perintah: follow | come | say <teks> | stop | status | quit")
    try:
        while True:
            raw = input().strip()
            if not raw:
                continue
            parts = raw.split(None, 1)
            cmd = {"cmd": parts[0]}
            if parts[0] == "say" and len(parts) > 1:
                cmd["text"] = parts[1]
            send(cmd)
            if parts[0] == "quit":
                break
    except (KeyboardInterrupt, EOFError):
        send({"cmd": "quit"})
    proc.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
