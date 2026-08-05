"""Patch .exp3.json scribbly lock dari referensi ArtiDefault1 (user fix).

MO.cdi3.json: UI "1"=Param28, "2"=Param29, "3"=Param33
Blend Add → UI "1"=1 pakai Value 1.0; UI "3"=0 pakai Value -1.0 (bukan 0!)
Jangan masukkan Param29 ("2").
"""
from __future__ import annotations

import glob
import json
import os
import sys

# ArtiDefault1 referensi (user manual fix)
SCRIBBLE_LOCK = {"Param28": 1.0, "Param33": -1.0}
REMOVE_IDS = {"Param1", "Param2", "Param3", "Param29"}

DEFAULT_DIRS = [
    r"c:\Program Files (x86)\Steam\steamapps\common\VTube Studio\VTube Studio_Data\StreamingAssets\Live2DModels\A_vts",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"),
]


def patch_file(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "Parameters" not in data:
        return False

    params = data["Parameters"]
    changed = False

    # Buang salah + duplikat — nanti satu entry per id
    cleaned = []
    seen_scribble = set()
    for p in params:
        pid = p.get("Id")
        if pid in REMOVE_IDS:
            changed = True
            continue
        if pid in SCRIBBLE_LOCK:
            if pid in seen_scribble:
                changed = True
                continue
            seen_scribble.add(pid)
        cleaned.append(p)
    params[:] = cleaned

    by_id = {p["Id"]: p for p in params}
    for pid, val in SCRIBBLE_LOCK.items():
        if pid in by_id:
            entry = by_id[pid]
            if entry.get("Value") != val or entry.get("Blend") != "Add":
                entry["Value"] = val
                entry["Blend"] = "Add"
                changed = True
        else:
            params.append({"Id": pid, "Value": val, "Blend": "Add"})
            changed = True

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.write("\n")
    return changed


def main() -> int:
    dirs = sys.argv[1:] or DEFAULT_DIRS
    patched = skipped = 0
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.exp3.json"))):
            if path.endswith(".bak"):
                continue
            if patch_file(path):
                patched += 1
                print(f"PATCH {path}")
            else:
                skipped += 1
    print(f"Done: {patched} patched, {skipped} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
