"""Privacy guard of the build procedures (ADR 245): the evidence folders' bootstrap/sitecustomize.py, parameterised.

Every Python child of a build step has this folder first on PYTHONPATH, so Python imports this file at start and
installs an audit hook (the build runner installs the same hook in its own process). It refuses:
- launching StardewModdingAPI.exe, Stardew Valley.exe or obs64.exe;
- opening, listing, copying, moving or deleting an EXISTING private path: `.env`, `config_local.json`, `prefs.jsonl`,
  the soul file, anything under a `secrets`, `vault*`, `arti-state`, `stream-folder`, `mabar` or `transcripts`
  folder, viewer or mood files, and the game's Saves folder;
- unless the path is the run's own temporary folder, a tracked non-private file of the build checkout, a file this
  process created, or a tracked `*.example.*` file.
Refusals are appended to `<evidence>/ct-privacy-refused.jsonl` (path names only, never content).

Self-contained on purpose: the checkout being built may be older than this tool. Without ARTI_RIGBUILD_CHECKOUT in
the environment nothing is installed.
"""
import json
import os
import sys
from pathlib import Path

LAUNCH = ("stardewmoddingapi.exe", "stardew valley.exe", "obs64.exe")
EVENTS = ("open", "os.listdir", "os.scandir", "os.rename", "os.remove", "os.rmdir", "os.mkdir",
          "shutil.copyfile", "shutil.copytree", "shutil.move")
PRIVATE_NAMES = (".env", "config_local.json", "prefs.jsonl", "arti_soul.md")
PRIVATE_DIRS = ("secrets", "vault", "arti-state", "stream-folder")


def canonical(p):
    return os.path.normcase(os.path.realpath(os.fsdecode(p)))


def within(p, root):
    return p == root or p.startswith(root + os.sep)


def params_from(env):
    task = env.get("ARTI_RIGBUILD_TASK", "rigbuild")
    return dict(task=task, evidence=env["ARTI_RIGBUILD_EVIDENCE"], checkout=env["ARTI_RIGBUILD_CHECKOUT"],
                base=env.get("ARTI_RIGBUILD_BASETEMP") or os.path.join(env.get("TEMP", ""), task + "-pytest"),
                root=env["ARTI_RIGBUILD_PRIVATE_ROOT"], saves=env["ARTI_RIGBUILD_SAVES"])


def is_private(path, root):
    parts = path.split(os.sep)
    name = parts[-1]
    under_root = path[len(root) + 1:].split(os.sep)[:1] if within(path, root) else []
    return (name in PRIVATE_NAMES or any(v in PRIVATE_DIRS for v in parts) or "mabar" in parts
            or "transcripts" in parts or any(v.startswith("vault") for v in under_root)
            or (name.endswith((".json", ".jsonl", ".md")) and ("viewer" in name or "mood" in name)))


def make_guard(params, *, exists=os.path.exists):
    task = params["task"].upper()
    evidence = Path(params["evidence"])
    checkout = canonical(params["checkout"])
    base = canonical(params["base"])
    saves = canonical(params["saves"])
    root = canonical(params["root"])
    listing = evidence / "ct-tracked-files.json"
    tracked = ({canonical(os.path.join(checkout, p)) for p in json.loads(listing.read_text(encoding="utf8"))}
               if listing.is_file() else set())
    tracked_dirs = set()
    for p in tracked:
        parent = os.path.dirname(p)
        while parent.startswith(checkout + os.sep):
            tracked_dirs.add(parent)
            parent = os.path.dirname(parent)
    examples = {p for p in tracked if ".example." in os.path.basename(p) and not any(
        v in ("secrets", "vault", "stream-folder", "mabar", "transcripts") for v in p.split(os.sep))}
    created = set()

    def log(name, value):
        with (evidence / name).open("a", encoding="utf8") as f:
            f.write(json.dumps(value) + "\n")

    def guard(event, args):
        if event == "subprocess.Popen":
            command = args[1]
            if isinstance(command, (tuple, list)) and command:
                if os.path.basename(str(command[0]).lower()) in LAUNCH:
                    raise PermissionError(task + " forbids live launch")
            return
        if event not in EVENTS:
            return
        for value in args[:2]:
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            path = canonical(value)
            found = exists(path)
            owned = within(path, checkout) or within(path, base)
            if not found and owned:
                writing = event == "os.mkdir" or (event == "open" and (
                    any(c in str(args[1]) for c in ("w", "a", "x", "+"))
                    or (len(args) > 2 and isinstance(args[2], int) and args[2] & os.O_CREAT)))
                if writing:
                    created.add(path)
                continue
            private = is_private(path, root)
            synthetic = owned and (within(path, base) or (path in tracked and not private)
                                   or (path in tracked_dirs and not private) or path in created or path in examples)
            if within(path, saves) or (found and private and not synthetic):
                log("ct-privacy-refused.jsonl", dict(event=event, path=path, existing=found,
                                                     node=os.environ.get("PYTEST_CURRENT_TEST", "collection"),
                                                     interpreter=sys.executable))
                raise PermissionError(task + " existing private state access forbidden")

    return guard


def install(params):
    hook = make_guard(params)
    sys.addaudithook(hook)
    return hook


if os.environ.get("ARTI_RIGBUILD_CHECKOUT") and __name__ == "sitecustomize":
    install(params_from(os.environ))
