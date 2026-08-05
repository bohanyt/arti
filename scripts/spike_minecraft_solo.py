"""Smoke mode SOLO: Bohan tidak ada di dunia -> Arti harus jalan sendiri.

Membuktikan yang tidak bisa dibuktikan unit test: bot benar-benar BERGERAK
(bukan mematung) saat streamer absen, dan event roam mengalir ke Python.

Prasyarat: server jalan, klien Minecraft TIDAK perlu dibuka.

    ./venv/Scripts/python.exe scripts/spike_minecraft_solo.py
"""

from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import arti_minecraft  # noqa: E402


def main() -> int:
    try:
        with open(os.path.join(ROOT, "config_local.json"), encoding="utf-8") as f:
            local = json.load(f)
    except (OSError, json.JSONDecodeError):
        local = {}

    cfg = {
        "minecraft_host": local.get("minecraft_host", "127.0.0.1"),
        "minecraft_port": local.get("minecraft_port", 25565),
        "minecraft_bot_name": local.get("minecraft_bot_name", "Arti"),
        # Nama streamer sengaja DIBIKIN TIDAK ADA di dunia: itu inti tesnya.
        "minecraft_streamer_name": "PemainYangTidakAda",
        "minecraft_node_path": "node",
        "minecraft_bot_script": os.path.join(ROOT, "mc-bot", "bot.js"),
        "minecraft_status_interval_sec": 5,
        "minecraft_reaction_cooldown_sec": 60.0,
        "minecraft_max_bot_respawns": 1,
    }
    reactions: list[str] = []
    runner = arti_minecraft.MinecraftRunner(
        cfg,
        {"queue_reaction": lambda t: (reactions.append(t), print(f"[REAKSI] {t}")),
         "add_history": lambda s, m: None},
    )
    runner.start()

    OBSERVE_SEC = 75
    deadline = time.time() + OBSERVE_SEC
    positions: list[tuple] = []
    while time.time() < deadline:
        st = runner.last_status
        if st:
            p = st.get("pos") or {}
            pos = (p.get("x"), p.get("y"), p.get("z"))
            if not positions or positions[-1] != pos:
                positions.append(pos)
                print(f"[POS] {pos} task={st.get('task')} solo={st.get('solo')}")
        time.sleep(2)

    events = [e for _, e in runner.events_snapshot()]
    kinds = [e.get("ev") for e in events]
    runner.stop()

    moved = len(set(positions)) >= 3
    roam_started = "roam_start" in kinds
    task_roam = (runner.last_status or {}).get("task") == "roam"
    announced = any("sendirian" in r for r in reactions)

    print(f"\n[Solo] posisi unik={len(set(positions))} bergerak={moved} "
          f"roam_start={roam_started} task_roam={task_roam} "
          f"pengumuman_solo={announced} legs={kinds.count('roam_leg')}")
    ok = moved and roam_started and task_roam and announced
    print("GO" if ok else "NO-GO")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
