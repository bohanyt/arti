"""Patch mood exp files — lip-sync + lampu + nod marah tidak bentrok overlay bicara."""
from __future__ import annotations

import json
import os
import sys

# Lip-sync VTS + mouth deform + lampu — jangan di-lock oleh overlay mood
MOUTH_IDS = frozenset({
    "ParamMouthOpenY",
    "ParamMouthForm",
    "Param48",
    "Param122",
    "Param125",
    "Param183",
    "Param186",
    "Param96",
    "Param97",
    "Param2",
})

# Mood overlay tidak boleh sentuh lampu (Param130=2 dari ArtiBicara)
LAMP_ID = "Param130"

# Hanya marah: jangan lock kepala — biarkan nod FaceAngleY inject
NOD_BLOCK_ID = "ParamAngleY"
NOD_BLOCK_ONLY = frozenset({"ArtiMarah"})

EMOTION_NAMES = (
    "ArtiSedih",
    "ArtiMarah",
    "ArtiBingung",
    "ArtiSenyum",
)

DEFAULT_DIRS = [
    r"c:\Program Files (x86)\Steam\steamapps\common\VTube Studio\VTube Studio_Data\StreamingAssets\Live2DModels\A_vts",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"),
]


def _strip_ids_for_file(basename: str) -> frozenset[str]:
    ids = set(MOUTH_IDS) | {LAMP_ID}
    if basename in NOD_BLOCK_ONLY:
        ids.add(NOD_BLOCK_ID)
    return frozenset(ids)


def patch_file(path: str) -> bool:
    basename = os.path.splitext(os.path.basename(path))[0]
    strip = _strip_ids_for_file(basename)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    params = data.get("Parameters")
    if not isinstance(params, list):
        return False
    new_params = [p for p in params if p.get("Id") not in strip]
    if len(new_params) == len(params):
        return False
    data["Parameters"] = new_params
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")
    return True


def main() -> int:
    dirs = sys.argv[1:] or DEFAULT_DIRS
    patched = 0
    for d in dirs:
        for name in EMOTION_NAMES:
            path = os.path.join(d, f"{name}.exp3.json")
            if not os.path.isfile(path):
                continue
            if patch_file(path):
                patched += 1
                print(f"PATCH {path}")
    print(f"Done: {patched} emotion files (mouth/lamp/nod params removed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
