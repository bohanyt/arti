"""Explicit offline cache preparation, outside a rehearsal and outside HOLD.

Uses the existing Supertonic default cache. No copies, downloads or inference.
The receipt lets admission check named files without loading a model or scanning.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def cache_root():
    return Path(os.environ["USERPROFILE"]) / ".cache/supertonic3"


def validate_cache():
    root = cache_root().resolve()
    try:
        receipt = json.loads((root / "livepath-ready.json").read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("voice") != "F1" or not receipt.get("files"):
            raise ValueError("invalid receipt")
        for row in receipt["files"]:
            path = (root / row["name"]).resolve()
            if not path.is_relative_to(root):
                raise ValueError("cache receipt escaped root")
            stat = path.stat()
            if not path.is_file() or stat.st_size <= 0 or (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime_ns"]):
                raise ValueError("cache changed")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Supertone cache unavailable/unprepared; Bridge lead must run scripts.livepath.cache outside HOLD") from exc
    return root


def main():
    from scripts.livepath.rehearse import idle_guard
    idle_guard()  # No model imports/loading while the rig is occupied.
    root = cache_root().resolve()
    # A failed reseed must not leave yesterday's success as today's admission proof.
    (root / "livepath-ready.json").unlink(missing_ok=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    import supertonic
    tts = supertonic.TTS(auto_download=False)
    tts.get_voice_style(voice_name="F1")
    # This is the explicit preparation step only. Admission never enumerates cache files.
    files = []
    for path in sorted(root.rglob("*")):
        # Ignore hub locks/metadata: these can be empty and change independently.
        relative = path.relative_to(root)
        if (path.is_file() and path.suffix in {".onnx", ".json"} and path.name != "livepath-ready.json"
                and not any(part.startswith(".") for part in relative.parts)):
            stat = path.stat()
            files.append(dict(name=path.relative_to(root).as_posix(), size=stat.st_size, mtime_ns=stat.st_mtime_ns))
    if (not any(row["name"].endswith(".onnx") for row in files) or
            not any(Path(row["name"]).name == "F1.json" for row in files) or
            any(row["size"] <= 0 for row in files)):
        raise RuntimeError("empty/incomplete default Supertonic cache")
    (root / "livepath-ready.json").write_text(json.dumps(dict(schema=1, voice="F1", files=files)) + "\n", encoding="utf-8")
    print("Prepared existing Supertonic cache offline; no model copy or download.")


if __name__ == "__main__":
    main()
