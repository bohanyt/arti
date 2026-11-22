"""Real-path bridge dry run with NO game (owner 2026-10-28: real-bridge tests are text-only).

Starts ``arti_bridge.py`` from this checkout in text-only test mode for ~60 s: a sample
``now.json`` in a fresh mind dir, one injected owner probe, at most one provider turn.
It then checks the transcript, samples the bridge's own TCP connections and child
processes once mid-run, and writes ``DRYRUN.json``. Never touches a game, the owner's
bridge or any config_local.json.

    python scripts/free_play/realpath_dryrun.py --out <new evidence dir> [--duration 60]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.free_play import soak  # noqa: E402

SAMPLE_WHY = "aku penasaran sama tambang, mau lihat ada batu apa di dalam"
SAMPLE_GOALS = ["cari batu buat furnace"]
# Lines only printed when a live voice/VTS/chat component starts.
FORBIDDEN_LOG = ("[TTS] Pre-warming", "SISTEM SIAP", "[YT Chat]", "[SubTitle] Server", "Mic watch",
                 "[ASR] ", "[VTS] Connected", "[Idle]", "[Scouter] start")


def own_process_facts(pid: int) -> dict:
    """TCP connections of the bridge and every descendant (venv launcher → interpreter → Codex), read-only."""
    script = ("$all=@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name);"
              f"$ids=@({pid});$i=0;while($i -lt $ids.Count){{$p=$ids[$i];"
              "$ids+=@($all|Where-Object ParentProcessId -eq $p|ForEach-Object ProcessId);$i++};"
              "$procs=@($all|Where-Object {$ids -contains $_.ProcessId}|ForEach-Object {"
              "$id=$_.ProcessId;@{name=$_.Name;tcp=@(Get-NetTCPConnection -OwningProcess $id -ErrorAction SilentlyContinue|"
              "ForEach-Object {''+$_.RemoteAddress+':'+$_.RemotePort+' '+$_.State})}});"
              "@{tree=$procs}|ConvertTo-Json -Depth 4")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=90)
        return json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--discoveries", action="store_true",
                   help="Inject high/low/flood generic discoveries instead of an owner probe (one Luna turn).")
    a = p.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True)          # must be new
    import arti_mind
    mind = out / "mind"
    if not a.discoveries:
        arti_mind.write_now(mind, game="stardew", why=SAMPLE_WHY, goal_ref=None, goal_today=SAMPLE_GOALS,
                            game_time={"date": "Spring 2", "time": 1010, "place": "Mine"})
    (out / "no-game").mkdir()
    cfg = soak.realpath_bridge_config(out, mind, out / "no-game")
    cfg.update(bridge_test_probe_on_start=True, bridge_test_probe_start_delay_sec=8,
               bridge_test_duration_sec=a.duration, bridge_test_max_turns=1, bridge_test_probe_min_gap_sec=600,
               arti_state_dir=str(out / "state"))   # never seed the checkout's own arti-state
    if a.discoveries:
        cfg.update(bridge_test_probe_on_start=False, bridge_test_reply_attempts=1)
    started = time.time()
    rp = soak.RealPathBridge(out, dict(os.environ))
    ready = rp.start(cfg)
    if ready and a.discoveries:
        from arti_stardew_moments import MomentSource
        source = MomentSource(out / "no-game", lambda row: None)
        for event in (
            dict(id="dry-high", kind="item_obtained", subject="practice-tool",
                 text="You obtained a new practice tool. Its use is unknown."),
            dict(id="dry-low", kind="item_seen", subject="plain-stone", text="A plain stone was first seen."),
            dict(id="dry-flood", kind="connection_discovered", count=246,
                 text="246 map connections were discovered in one adapter batch."),
        ):
            source.discovery(event, "synthetic no-game fixture")
        source.flush(enabled=False)  # discoveries must survive the optional-moments switch
    sample = None
    deadline = time.monotonic() + a.duration + 120
    while ready and rp.proc.poll() is None and time.monotonic() < deadline:
        if sample is None and time.time() - started > 30:
            sample = own_process_facts(rp.proc.pid)
        time.sleep(1)
    if rp.proc is not None and rp.proc.poll() is None:
        rp.stop()
    else:
        rp._record(stopped=True, rc=rp.proc.returncode if rp.proc else None)
    summary = soak.realpath_summary(out) or {}
    rows = [json.loads(line) for line in (out / "bridge/bridge-transcript.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (out / "bridge/bridge-transcript.jsonl").exists() else []
    turns = [r for r in rows if r.get("ev") == "turn"]
    end = next((r for r in reversed(rows) if r.get("ev") == "test_end"), {})
    log = (out / "bridge/bridge.log").read_text(encoding="utf-8", errors="replace") if (out / "bridge/bridge.log").exists() else ""
    checks = {
        "started_ready": ready,
        "one_probe_turn": len(turns) == 1 and turns[0].get("source") == "owner_probe",
        "niatku_seen": bool(turns) and SAMPLE_WHY in (turns[0].get("context") or {}).get("niatku", ""),
        "now_why_logged": bool(turns) and (turns[0].get("now") or {}).get("why") == SAMPLE_WHY,
        "text_reply": bool(turns) and bool(turns[0].get("reply")),
        "clean_exit": rp.proc is not None and rp.proc.returncode == 0 and end.get("reason") == "duration",
        "llm_turns_le_1": end.get("llm_turns", 99) <= 1,
        "no_blocked_side_effects": not end.get("blocked_side_effects"),
        "no_voice_component_lines": [m for m in FORBIDDEN_LOG if m in log] == [],
    }
    if a.discoveries:
        contexts = [r["event"] for r in rows if r.get("ev") == "discovery_context"]
        high = next((e for e in contexts if e["id"] == "dry-high"), None)
        checks.pop("one_probe_turn")
        checks.pop("niatku_seen")
        checks.pop("now_why_logged")
        checks.update(
            exactly_one_high_reaction=len(turns) == 1 and turns[0].get("source") == "discovery"
                and turns[0].get("shown_discovery_ids") == ["dry-high"] and bool(high),
            low_silent_context=any(e["id"] == "dry-low" and e["importance"] == "low" for e in contexts)
                and bool(turns) and "dry-low" in turns[0]["context"].get("discoveries", ""),
            flood_silent_context=any(e["id"] == "dry-flood" and e["importance"] == "flood"
                                     and e["count"] == 246 for e in contexts),
            no_flood_turns=all("dry-flood" not in r.get("shown_discovery_ids", []) for r in turns),
            all_source_files_retained=len(list((out / "no-game").glob("*.persona-done"))) == 3,
        )
    report = dict(utc=soak.utc(), checkout=str(ROOT), duration_s=round(time.time() - started, 1), checks=checks,
                  passed=all(checks.values()), forbidden_lines_seen=[m for m in FORBIDDEN_LOG if m in log],
                  own_process_sample=sample, realpath=summary.get("state"), counts=summary.get("counts"))
    soak.write(out / "DRYRUN.json", report)
    print(json.dumps(dict(passed=report["passed"], checks=checks), indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
