"""Offline analysis of the disposable S3 observer and OBS Game Capture recording.

Never starts games/OBS or changes capture configuration. Requires local ffmpeg
and ffprobe only when --video is supplied. No image/frame-rate interpolation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import subprocess


def decode_marker(gray: bytes, cell: int = 8) -> tuple[int, int] | None:
    size = cell * 8
    if cell < 2 or len(gray) != size * size:
        raise ValueError("wrong marker dimensions")
    bits = 0
    for index in range(64):
        value = gray[(index // 8 * cell + cell // 2) * size + index % 8 * cell + cell // 2]
        if 65 <= value <= 190:
            return None  # ambiguous cell; do not guess a bit
        bits = (bits << 1) | int(value > 190)
    payload, expected_crc = bits >> 16, bits & 0xFFFF
    crc = 0xFFFF
    for byte in payload.to_bytes(6, "big"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    nonce, draw = (payload >> 24) & 0xFFFF, payload & 0xFFFFFF
    if payload >> 40 != 0xD3 or crc != expected_crc or nonce == 0 or draw == 0:
        return None
    return nonce, draw


def intervals(timestamps: list[float]) -> dict:
    if any(not math.isfinite(t) for t in timestamps) or any(b <= a for a, b in zip(timestamps, timestamps[1:])):
        raise ValueError("timestamps must be finite and strictly increasing")
    if len(timestamps) < 2:
        return {"count": len(timestamps), "rate_hz": None, "max_gap_ms": None}
    gaps = sorted((b - a) * 1000 for a, b in zip(timestamps, timestamps[1:]))
    return {"count": len(timestamps), "rate_hz": (len(timestamps) - 1) / (timestamps[-1] - timestamps[0]),
            "p50_ms": statistics.median(gaps), "p95_ms": gaps[math.ceil(len(gaps) * .95) - 1],
            "max_gap_ms": gaps[-1], "gaps_over_50ms": sum(g > 50 for g in gaps)}


def observer_summary(records: list[dict]) -> dict:
    if not records or records[0].get("kind") != "identity" or records[-1].get("kind") != "stop":
        raise ValueError("complete identity/sample/stop witness required")
    identity = records[0]
    frequency = identity["frequency"]
    if frequency <= 0 or identity["nonce"] == 0 or not identity["hwnd"]:
        raise ValueError("invalid observer identity/clock")
    samples = [r for r in records if r.get("kind") in ("update", "draw", "present")]
    if any(r["active"] or r["minimized"] or r["hwnd"] != identity["hwnd"]
           or not r["foreground"] or r["foreground"] == r["hwnd"] for r in samples):
        raise ValueError("sample violates inactive window identity")
    result = {k: intervals([r["qpc"] / frequency for r in samples if r["kind"] == k])
              for k in ("update", "draw", "present")}
    update = [r for r in samples if r["kind"] == "update"]
    result["world_tick_delta"] = ((update[-1]["worldTicks"] - update[0]["worldTicks"]) & 0xFFFFFFFF) if update else None
    result["stop_reason"] = records[-1]["reason"]
    result["elapsed_seconds"] = (records[-1]["qpc"] - identity["qpc"]) / frequency
    result["present_meaning"] = "normal EndDraw/Present return, not GPU scanout or OBS delivery"
    return result


def delivered_summary(frames: list[tuple[float, tuple[int, int] | None]], nonce: int, witnessed: set[int]) -> dict:
    intervals([pts for pts, _ in frames])  # validate even frames outside marker interval
    indices = [i for i, (_, marker) in enumerate(frames) if marker and marker[0] == nonce]
    if not indices:
        raise ValueError("no intended farmhand marker in recording")
    selected = frames[indices[0]:indices[-1] + 1]
    unique_times, unique_ids = [], []
    duplicate = invalid = wrong_nonce = unknown = skipped = 0
    previous = None
    for pts, marker in selected:
        if marker is None:
            invalid += 1
            continue
        if marker[0] != nonce:
            wrong_nonce += 1
            continue
        draw = marker[1]
        if draw not in witnessed:
            unknown += 1
            continue
        if previous is not None and draw < previous:
            raise ValueError("marker regression/reordered frame")
        if draw == previous:
            duplicate += 1
        else:
            if previous is not None:
                skipped += draw - previous - 1
            unique_times.append(pts)
            unique_ids.append(draw)
        previous = draw
    return {"encoded_frames_total": len(frames), "frames_in_marker_span": len(selected),
            "encoded_cadence_in_span": intervals([pts for pts, _ in selected]),
            "unique_delivered_cadence": intervals(unique_times), "duplicate_frames": duplicate,
            "invalid_marker_frames_inside_span": invalid, "wrong_nonce_frames_inside_span": wrong_nonce,
            "unwitnessed_marker_frames": unknown, "skipped_marker_ids": skipped,
            "observed_draw_ids_not_delivered": len({x for x in witnessed if unique_ids and unique_ids[0] <= x <= unique_ids[-1]} - set(unique_ids)),
            "identity_and_marker_valid": not (invalid or wrong_nonce or unknown)}


def check_game_capture(scene: dict, source_name: str, executable: str) -> dict:
    sources = scene.get("sources", [])
    matching = [s for s in sources if s.get("name") == source_name]
    if len(matching) != 1:
        raise ValueError("capture source missing/ambiguous")
    source = matching[0]
    settings = source.get("settings", {})
    if source.get("id") != "game_capture" or settings.get("capture_mode") != "window":
        raise ValueError("specific-window Game Capture required; no fallback")
    # OBS 32.2.2 window_priority: class=0, title=1, executable=2.
    # SDL_app is a generic class; class priority can degrade to title-only.
    if settings.get("priority") != 2:
        raise ValueError("executable match priority=2 required to exclude vanilla HOST")
    target_exe = settings.get("window", "").rsplit(":", 1)[-1]
    if target_exe.casefold() != executable.replace("\\", "/").rsplit("/", 1)[-1].casefold():
        raise ValueError("Game Capture executable differs from observer")
    current = [s for s in sources if s.get("name") == scene.get("current_scene") and s.get("id") == "scene"]
    visible = [i.get("name") for i in current[0].get("settings", {}).get("items", []) if i.get("visible", True)] if len(current) == 1 else []
    if visible != [source_name]:
        raise ValueError("diagnostic scene must contain only the visible Game Capture source")
    return {"source": source_name, "target": settings["window"], "configuration_only": True,
            "runtime_binding_required": "match native HWND/PID/start time, OBS hook log and recording nonce"}


def video_frames(video: str, ffmpeg: str, ffprobe: str, x: int, y: int, cell: int):
    probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                            "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", video],
                           check=True, capture_output=True, text=True, timeout=60)
    pts = [float(f["best_effort_timestamp_time"]) for f in json.loads(probe.stdout)["frames"]]
    size = 8 * cell
    command = [ffmpeg, "-v", "error", "-i", video, "-map", "0:v:0", "-vf", f"crop={size}:{size}:{x}:{y}",
               "-fps_mode", "passthrough", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]
    # 30-second witness is bounded; reject excess data rather than accumulating an arbitrary recording.
    if len(pts) > 7200:
        raise ValueError("recording exceeds diagnostic frame bound")
    result = subprocess.run(command, check=True, capture_output=True, timeout=60)
    if len(result.stdout) != len(pts) * size * size:
        raise ValueError("decoded frame/PTS count mismatch or crop out of bounds")
    return [(t, decode_marker(result.stdout[i * size * size:(i + 1) * size * size], cell)) for i, t in enumerate(pts)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", required=True)
    parser.add_argument("--video")
    parser.add_argument("--scene")
    parser.add_argument("--source", default="ARTI S3 Game Capture")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--x", type=int, default=16)
    parser.add_argument("--y", type=int, default=16)
    parser.add_argument("--cell", type=int, default=8)
    args = parser.parse_args()
    if args.x < 0 or args.y < 0 or not 2 <= args.cell <= 64:
        parser.error("invalid crop/marker cell size")
    records = [json.loads(line) for line in Path(args.observer).read_text(encoding="utf-8-sig").splitlines()]
    summary = {"observer": observer_summary(records), "live_acceptance": "NOT_AUTOMATIC", "audio": "NOT_ANALYZED"}
    identity = records[0]
    if args.scene:
        summary["capture_source"] = check_game_capture(json.loads(Path(args.scene).read_text(encoding="utf-8-sig")), args.source, identity["executable"])
    if args.video:
        witnessed = {r["marker"] for r in records if r.get("kind") == "draw" and r["marker"] > 0}
        frames = video_frames(args.video, args.ffmpeg, args.ffprobe, args.x, args.y, args.cell)
        delivery = delivered_summary(frames, identity["nonce"], witnessed)
        delivered = {marker[1] for _, marker in frames if marker and marker[0] == identity["nonce"] and marker[1] in witnessed}
        covered = [r["qpc"] for r in records if r.get("kind") == "draw" and r["marker"] in delivered]
        delivery["covered_observer_seconds"] = (max(covered) - min(covered)) / identity["frequency"] if covered else 0
        delivery["covers_required_interval"] = delivery["covered_observer_seconds"] >= 29 and summary["observer"]["stop_reason"] == "wall_deadline"
        summary["obs_delivery"] = delivery
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
