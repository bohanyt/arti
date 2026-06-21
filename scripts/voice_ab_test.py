#!/usr/bin/env python3
"""Standalone Supertone A/B benchmark — zero bridge imports.

Usage:
  python scripts/voice_ab_test.py
  python scripts/voice_ab_test.py --matrix
  python scripts/voice_ab_test.py --steps 8,10,12
  python scripts/voice_ab_test.py --directml
  python scripts/voice_ab_test.py --text "Halo, aku Arti!"
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "voice_samples" / "ab"
CSV_PATH = OUT_DIR / "benchmark.csv"

DEFAULT_TEXT = (
    "Halo, aku Arti, co-host VTuber AI di live stream ini! "
    "Gimana kabarnya hari ini?"
)

PRESETS = {
    "live": {
        "voice": "F1",
        "speed": 1.1,
        "total_steps": 10,
        "lang": "id",
    },
    "archive": {
        "voice": "F1",
        "speed": 1.05,
        "total_steps": 8,
        "lang": "id",
    },
    "max_quality": {
        "voice": "F1",
        "speed": 1.0,
        "total_steps": 12,
        "lang": "id",
    },
}


def _try_directml() -> str | None:
    """Return 'directml' if onnxruntime-directml is usable, else None."""
    try:
        import onnxruntime as ort  # noqa: F401

        providers = ort.get_available_providers()
        if "DmlExecutionProvider" in providers:
            return "directml"
    except Exception:
        pass
    return None


def _synth(text: str, preset: dict, *, provider_hint: str = "cpu") -> tuple[Path, float, float]:
    try:
        import numpy as np
        import soundfile as sf
        import supertonic
    except ImportError as exc:
        print(f"[ERROR] Missing dependency: {exc}")
        print("Install: pip install supertonic soundfile numpy")
        sys.exit(1)

    tts = supertonic.TTS()
    style = tts.get_voice_style(voice_name=preset["voice"])
    t0 = time.perf_counter()
    audio, sr = tts.synthesize(
        text,
        voice_style=style,
        lang=preset["lang"],
        speed=float(preset["speed"]),
        total_steps=int(preset["total_steps"]),
    )
    synth_ms = (time.perf_counter() - t0) * 1000.0
    duration_s = len(audio) / float(sr) if sr else 0.0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{preset.get('_label', 'run')}_{int(time.time())}"
    wav_path = OUT_DIR / f"{tag}.wav"
    sf.write(wav_path, np.asarray(audio), sr)
    return wav_path, synth_ms, duration_s


def _append_csv(row: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_PATH.is_file()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "preset",
                "steps",
                "speed",
                "voice",
                "synth_ms",
                "duration_s",
                "provider",
                "wav",
            ],
        )
        if write_header:
            w.writeheader()
        w.writerow(row)


def _run_case(name: str, preset: dict, text: str, provider: str) -> None:
    p = {**preset, "_label": name}
    print(f"\n=== {name} (steps={preset['total_steps']}, speed={preset['speed']}) ===")
    wav, synth_ms, dur = _synth(text, p, provider_hint=provider)
    print(f"  synth_ms={synth_ms:.0f}  duration_s={dur:.2f}s  provider={provider}")
    print(f"  -> {wav}")
    _append_csv(
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "preset": name,
            "steps": preset["total_steps"],
            "speed": preset["speed"],
            "voice": preset["voice"],
            "synth_ms": round(synth_ms, 1),
            "duration_s": round(dur, 3),
            "provider": provider,
            "wav": str(wav.relative_to(ROOT)),
        }
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Supertone voice A/B (standalone, no bridge)")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="Test sentence")
    ap.add_argument("--matrix", action="store_true", help="Run all presets + steps sweep")
    ap.add_argument("--steps", default="", help="Comma-separated steps sweep on F1 live speed")
    ap.add_argument("--directml", action="store_true", help="Probe DirectML availability")
    ap.add_argument("--list", action="store_true", help="List archive F1 samples if present")
    args = ap.parse_args()

    if args.list:
        archive = ROOT / "archive" / "v0.4" / "supertone_voice_samples" / "F1"
        if archive.is_dir():
            for p in sorted(archive.glob("*.wav")):
                print(p)
        else:
            print(f"No archive samples at {archive}")
        return

    provider = "cpu"
    if args.directml:
        dm = _try_directml()
        if dm:
            provider = dm
            print(f"[DirectML] Available providers include DmlExecutionProvider")
        else:
            print("[DirectML] Not available — fallback CPU (pip install onnxruntime-directml)")

    if args.matrix:
        for name, preset in PRESETS.items():
            _run_case(name, preset, args.text, provider)
        for steps in (6, 8, 10, 12):
            preset = {**PRESETS["live"], "total_steps": steps}
            _run_case(f"steps_{steps}", preset, args.text, provider)
        print(f"\nBenchmark CSV: {CSV_PATH}")
        return

    if args.steps:
        for s in args.steps.split(","):
            s = s.strip()
            if not s:
                continue
            preset = {**PRESETS["live"], "total_steps": int(s)}
            _run_case(f"steps_{s}", preset, args.text, provider)
        print(f"\nBenchmark CSV: {CSV_PATH}")
        return

    _run_case("live", PRESETS["live"], args.text, provider)
    print(f"\nBenchmark CSV: {CSV_PATH}")


if __name__ == "__main__":
    main()
