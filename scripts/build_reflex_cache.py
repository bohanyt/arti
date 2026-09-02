"""Pre-synthesize ARTI's short reflex lines into WAV files.

Reflex audio must play almost immediately after a game event, so synthesizing it
on demand is too slow. This helper renders every line from
``arti_reflex.REFLEX_LINES`` once through the same Supertonic engine used by the
bridge and stores the resulting WAV files under ``data/reflex/``.

Run after the Supertonic Python 3.12 environment is installed, and rerun after
changing the reflex line set or voice:

    ./venv/Scripts/python.exe scripts/build_reflex_cache.py
    ./venv/Scripts/python.exe scripts/build_reflex_cache.py --force

On non-Windows systems use the equivalent Python executable for the project
environment; the helper itself locates ``venv312`` for Supertonic.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import arti_reflex  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "data", "reflex")
PROTOCOL_VERSION = 1


def trim_silence(path: str, lead_ms: int = 30, tail_ms: int = 80) -> tuple[float, float]:
    """Trim leading/trailing silence in place and return old/new durations."""
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415

    data, sr = sf.read(path)
    old_duration = len(data) / sr
    amp = np.abs(data if data.ndim == 1 else data.mean(axis=1))
    if not len(amp) or amp.max() <= 0:
        return old_duration, old_duration
    idx = np.where(amp > amp.max() * 0.02)[0]
    if not len(idx):
        return old_duration, old_duration
    start = max(0, idx[0] - int(sr * lead_ms / 1000))
    end = min(len(data), idx[-1] + int(sr * tail_ms / 1000))
    sf.write(path, data[start:end], sr)
    return old_duration, (end - start) / sr


def _load_cfg() -> dict:
    cfg = {}
    try:
        with open(os.path.join(_ROOT, "config_local.json"), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _venv312_python() -> str:
    exe = "python.exe" if os.name == "nt" else "python"
    sub = "Scripts" if os.name == "nt" else "bin"
    path = os.path.join(_ROOT, "venv312", sub, exe)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"venv312 not found at {path}. Supertonic uses a separate Python 3.12 "
            "environment; see requirements-supertone.txt."
        )
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="overwrite existing WAV files")
    parser.add_argument("--voice", default=None, help="Supertonic voice (defaults to local config)")
    args = parser.parse_args()

    cfg = _load_cfg()
    voice = args.voice or cfg.get("supertonic_voice", "F1")
    os.makedirs(OUT_DIR, exist_ok=True)

    lines = list(arti_reflex.ALL_LINES)
    todo = [
        line for line in lines
        if args.force or not os.path.exists(os.path.join(OUT_DIR, arti_reflex.cache_name(line)))
    ]
    print(f"[Reflex] {len(lines)} lines, {len(todo)} need synthesis "
          f"(voice={voice}, output={OUT_DIR})")
    if not todo:
        print("[Reflex] Cache is complete.")
        return 0

    env = dict(os.environ)
    env["SUPERTONE_USE_CUDA"] = "1" if cfg.get("supertonic_use_cuda") else "0"
    proc = subprocess.Popen(
        [_venv312_python(), os.path.join(_ROOT, "supertone_engine.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        cwd=_ROOT,
        env=env,
    )

    if proc.stdin is None or proc.stdout is None:
        proc.kill()
        raise RuntimeError("failed to open Supertonic subprocess pipes")

    def request(rid: str, text: str) -> dict | None:
        proc.stdin.write(json.dumps({
            "v": PROTOCOL_VERSION,
            "id": rid,
            "type": "synthesize",
            "text": text,
            "voice": voice,
            "speed": float(cfg.get("supertonic_speed", 1.0)),
            "lang": cfg.get("supertonic_lang", "id"),
            "total_steps": int(cfg.get("supertonic_total_steps", 8)),
            "preprocess_numbers": False,
        }) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 120
        while time.time() < deadline:
            raw = proc.stdout.readline()
            if not raw:
                return None
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                return msg
        return None

    ok = fail = 0
    started = time.time()
    for i, line in enumerate(todo, 1):
        dest = os.path.join(OUT_DIR, arti_reflex.cache_name(line))
        resp = request(f"reflex-{i}", line)
        if not resp or not resp.get("ok") or not resp.get("wav_path"):
            print(f"  [{i:2}/{len(todo)}] FAILED {line!r}: "
                  f"{(resp or {}).get('error') or 'no response'}")
            fail += 1
            continue
        try:
            shutil.copyfile(resp["wav_path"], dest)
        except OSError as exc:
            print(f"  [{i:2}/{len(todo)}] FAILED copy {line!r}: {exc}")
            fail += 1
            continue
        old_duration, new_duration = trim_silence(dest)
        ok += 1
        print(f"  [{i:2}/{len(todo)}] {line!r} -> {os.path.basename(dest)} "
              f"({old_duration:.2f}s -> {new_duration:.2f}s)")

    try:
        proc.stdin.write(json.dumps({"v": PROTOCOL_VERSION, "type": "shutdown"}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()

    print(f"\n[Reflex] Done: {ok} success, {fail} failed, {time.time() - started:.0f}s")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
