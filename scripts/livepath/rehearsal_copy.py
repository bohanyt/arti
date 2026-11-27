"""Make a disposable copy of a backed-up co-op save: a new farm name and a new save ID, for a private rehearsal.

    python -m scripts.livepath.rehearsal_copy --backup ARTI-backups/mabar-<UTC> --save <folder> --name latihan --out DIR
    ... --place      (instead of --out: into the game's Saves folder, games closed; only on the owner's word)

The source is a verified `mabar backup`, never the Saves folder. The copy differs from it in the farm name and in
`uniqueIDForThisGame` only, so it is another farm to the game and to her memory (the farm scope is made of the save
ID). Nothing existing is changed or replaced; `_old` files are left out.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

NAME = re.compile(r"[a-z0-9]{1,20}")
FOLDER = re.compile(r"(?P<name>[^\\/:*?\"<>|]+)_(?P<id>\d+)")
ID_TAG = "uniqueIDForThisGame"
EPOCH = datetime.datetime(2012, 6, 22, tzinfo=datetime.timezone.utc)  # the game's own: Utility.NewUniqueIdForThisGame


def new_id(now=None):
    return int(((now or datetime.datetime.now(datetime.timezone.utc)) - EPOCH).total_seconds())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def saves_root():
    return Path(os.environ["APPDATA"]) / "StardewValley" / "Saves"


def _inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def read_source(backup, save, *, saves=None):
    """The save's two files as bytes, each equal to the backup's own manifest."""
    folder = FOLDER.fullmatch(save)
    if folder is None:
        raise ValueError("expected a save folder name (name_id)")
    backup = Path(backup)
    if _inside(backup, (saves or saves_root()).parent):
        raise ValueError("the source must be a backup, not the game's own folder")
    manifest = json.loads((backup / "MANIFEST.json").read_text(encoding="utf-8-sig"))["sha256"].get("Saves/" + save)
    if not isinstance(manifest, dict):
        raise ValueError("the backup holds no manifest for this save")
    out = {}
    for name in (save, "SaveGameInfo"):
        data = (backup / "Saves" / save / name).read_bytes()
        if manifest.get(name) != sha(data):
            raise ValueError("the backup's file differs from its manifest: " + name)
        out[name] = data
    return out


def _swap(data, tag, old, new, *, expect=None):
    a, b = (f"<{tag}>{v}</{tag}>".encode() for v in (old, new))
    count, empty = data.count(a), data.count(f"<{tag}></{tag}>".encode())  # a cabin nobody has taken has no farm name
    if count < 1 or (expect is not None and count != expect) or data.count(f"<{tag}>".encode()) != count + empty:
        raise ValueError(f"unexpected <{tag}> in the save")
    return data.replace(a, b)


def rewrite(files, save, name, unique_id):
    """New file name -> bytes. Only the farm name and the save ID differ; both are read back from the XML."""
    if NAME.fullmatch(name) is None:
        raise ValueError("the new farm name: 1 to 20 of a-z and 0-9")
    main = ET.fromstring(files[save])
    old_id, old_name = main.findtext(ID_TAG), main.findtext("player/farmName")
    if not old_id or not old_name or old_id != FOLDER.fullmatch(save)["id"]:
        raise ValueError("the save's own ID or farm name cannot be read")
    if str(unique_id) == old_id or name == old_name.casefold():
        raise ValueError("the copy needs another ID and another farm name")
    escaped = old_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    target = f"{name}_{unique_id}"
    out = {target: _swap(_swap(files[save], ID_TAG, old_id, unique_id, expect=1), "farmName", escaped, name),
           "SaveGameInfo": _swap(files["SaveGameInfo"], "farmName", escaped, name)}
    root, info = ET.fromstring(out[target]), ET.fromstring(out["SaveGameInfo"])
    names = {e.text for tree in (root, info) for e in tree.iter("farmName") if e.text}
    if root.findtext(ID_TAG) != str(unique_id) or names != {name} or root.findtext("player/farmName") != name:
        raise ValueError("the copy's identity does not read back")
    return target, out


def write(out_dir, target, files):
    """The whole folder appears at once, under a name that did not exist."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise ValueError("the destination folder does not exist")
    if (out_dir / target).exists() or any(p.name.endswith("_" + target.rpartition("_")[2]) for p in out_dir.iterdir()):
        raise ValueError("a save with this name or ID exists already")
    staging = Path(tempfile.mkdtemp(prefix=".rehearsal-copy-", dir=out_dir.parent))
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
            if (staging / name).read_bytes() != data:
                raise OSError("the copy differs after writing: " + name)
        os.rename(staging, out_dir / target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # our own staging folder only
        raise
    return out_dir / target


def make(backup, save, name, out_dir, *, place=False, saves=None, processes=None, unique_id=None):
    saves = Path(saves or saves_root())
    if place:
        if processes is None:
            from scripts.livepath.mabar import processes
        running = {p.casefold() for p in processes()}
        if running & {"stardewmoddingapi.exe", "stardew valley.exe"}:
            raise ValueError("close the games first")
        out_dir = saves
    elif _inside(out_dir, saves.parent):
        raise ValueError("into the game's folder only with --place")
    files = read_source(backup, save, saves=saves)
    target, copied = rewrite(files, save, name, unique_id or new_id())
    folder = write(out_dir, target, copied)
    return dict(folder=str(folder), save=target, source=save, backup=str(backup),
                source_sha256={k: sha(v) for k, v in files.items()}, sha256={k: sha(v) for k, v in copied.items()})


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backup", type=Path, required=True, help="a `mabar backup` folder (with MANIFEST.json)")
    p.add_argument("--save", required=True, help="the save's folder name inside that backup")
    p.add_argument("--name", required=True, help="the copy's farm name (a-z, 0-9)")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--out", type=Path, help="an existing folder outside the game's own")
    where.add_argument("--place", action="store_true", help="into the game's Saves folder (the owner's word)")
    a = p.parse_args(argv)
    print(json.dumps(make(a.backup, a.save, a.name, a.out, place=a.place), indent=1))


if __name__ == "__main__":
    main()
