#!/usr/bin/env python3
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "arti_openrouter.py"
text = path.read_text(encoding="utf-8")
old = '''    primary = config.get("openrouter_live_model", "nvidia/nemotron-3-super-120b-a12b:free")
    last_resort = config.get("openrouter_live_last_resort", "google/gemma-4-26b-a4b-it:free")
'''
new = '''    primary = (
        config.get("openrouter_live_model")
        or "nvidia/nemotron-3-super-120b-a12b:free"
    )
    last_resort = (
        config.get("openrouter_live_last_resort")
        or "google/gemma-4-26b-a4b-it:free"
    )
'''
if text.count(old) != 1:
    raise SystemExit("expected OpenRouter default block not found exactly once")
path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
