#!/usr/bin/env python3
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "arti_openrouter.py"
text = path.read_text(encoding="utf-8")
old = """    if not chain:\n        chain = [laguna, owl]\n    return chain\n"""
new = """    if not chain:\n        # Empty explicit overrides must still yield the shipped, defined defaults.\n        # The old fallback referenced retired local names (`laguna`, `owl`) that\n        # no longer existed and raised NameError when fast_only was disabled.\n        chain = [primary, last_resort]\n    return [m for m in chain if m]\n"""
if text.count(old) != 1:
    raise SystemExit("expected OpenRouter fallback block not found exactly once")
path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
