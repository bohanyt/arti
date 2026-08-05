"""Smoke Phase 1: MinecraftRunner asli (bukan fake) lawan server sungguhan.

Membuktikan jalur produksi: Popen node -> event NDJSON -> ring + reaksi ->
send_command -> stop bersih. Tidak menyentuh bridge (nol VTS/TTS/LLM).

Prasyarat: server jalan, `npm install` di mc-bot/ sudah.

    ./venv/Scripts/python.exe scripts/spike_minecraft_runner.py
"""

from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import arti_minecraft  # noqa: E402


def _local_cfg() -> dict:
    try:
        with open(os.path.join(ROOT, "config_local.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    local = _local_cfg()
    cfg = {
        "minecraft_host": local.get("minecraft_host", "127.0.0.1"),
        "minecraft_port": local.get("minecraft_port", 25565),
        "minecraft_bot_name": local.get("minecraft_bot_name", "Arti"),
        "minecraft_streamer_name": local.get("minecraft_streamer_name", "Bohan"),
        "minecraft_node_path": "node",
        "minecraft_bot_script": os.path.join(ROOT, "mc-bot", "bot.js"),
        "minecraft_status_interval_sec": 5,
        "minecraft_reaction_cooldown_sec": 60.0,
        "minecraft_max_bot_respawns": 1,
    }
    reactions: list[str] = []
    history: list[tuple] = []
    runner = arti_minecraft.MinecraftRunner(
        cfg,
        {
            "queue_reaction": lambda t: (reactions.append(t), print(f"[REAKSI] {t}")),
            "add_history": lambda s, m: history.append((s, m)),
        },
    )
    runner.start()

    deadline = time.time() + 40
    while time.time() < deadline and runner.last_status is None:
        time.sleep(0.5)
    if runner.last_status is None:
        print("NO-GO: tidak ada status dalam 40 dtk")
        runner.stop()
        return 1
    print(f"[Status] {runner.status_line()}")

    ok_say = runner.send_command({"cmd": "say", "text": "runner phase 1 nyambung"})
    ok_status = runner.send_command({"cmd": "status"})
    time.sleep(3)

    ctx = arti_minecraft.format_context(
        runner.last_status, runner.events_snapshot(), 120.0, time.time()
    )
    print("--- KONTEKS [DI MINECRAFT] ---")
    print(ctx)
    print("------------------------------")

    runner.stop()
    time.sleep(1)
    events = runner.events_snapshot()
    kinds = {e.get("ev") for _, e in events}
    ok = (
        ok_say and ok_status
        and "spawned" in kinds and "status" in kinds
        and not runner.is_active() and not runner.gave_up
        and "HP" in ctx
    )
    print(f"[Smoke] events={len(events)} kinds={sorted(kinds)} "
          f"reaksi={len(reactions)} aktif={runner.is_active()} gave_up={runner.gave_up}")
    print("GO" if ok else "NO-GO")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
