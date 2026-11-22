"""G6: validate entire predeclared script, then reuse native chat/screenshot gates."""
import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
from pathlib import Path
import hashlib
import json
from scripts.free_play.i1_collector import encoded


def stage(recipe, *, collector, output, catalog, locations, run_script, trial_running):
    from scripts.m1_operator import stage_save
    if trial_running() is not False or recipe.get("target") != "farmhand":
        raise RuntimeError("STAGING requires unenrolled farmhand and known idle trial")
    if recipe.get("blocked"):
        raise ValueError("STAGING unavailable: " + recipe["blocked"])
    if recipe.get("mode", "debug") != "debug": raise ValueError("use checked offline snapshot fixture")
    text = "\n".join(recipe["commands"]) + "\n"
    blocks, errors = stage_save.parse_script(text, catalog, "farmhand", locations=locations)
    if errors: raise ValueError("STAGING refused: " + "; ".join(errors))
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    script = output / "STAGING-debug.txt"
    script.write_text(text, encoding="utf-8")
    for block in blocks:
        for step in block:
            with ipc.open_shared(output / "STAGING.jsonl", "ab") as log:
                log.write(encoded(dict(tag="STAGING", status="planned", **step)) + b"\n")
    collector.capture("STAGING-before")
    rc = run_script(["run-script", str(script), "--target", "farmhand", "--label", "I1-STAGING"])
    collector.capture("STAGING-after")
    if rc: raise AssertionError("STAGING failed; retain native staging-log.jsonl and screenshots")


def snapshot(recipe, *, root, output, restore, games_closed, trial_running):
    """Restore a declared whole disposable snapshot, never edit an unresolved live save."""
    if not games_closed() or trial_running() is not False or recipe.get("disposable") is not True:
        raise RuntimeError("offline unenrolled disposable snapshot required; no close/kill path")
    root = Path(root).resolve()
    files = recipe.get("files", {})
    if not files or not recipe.get("source_pin"): raise ValueError("snapshot manifest/source pin required")
    count = 0
    for path in root.rglob("*"):
        if not path.is_file(): continue
        if path.relative_to(root).as_posix() not in files: raise ValueError("unexpected snapshot file")
        count += 1
    if count != len(files): raise ValueError("whole snapshot file set mismatch")
    for name, digest in files.items():
        path = (root / name).resolve()
        if root not in path.parents: raise ValueError("snapshot path escape")
        with ipc.open_shared(path, "rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("snapshot hash mismatch: " + name)
    out = Path(output); out.mkdir(parents=True, exist_ok=False)
    (out / "STAGING-snapshot.json").write_bytes(encoded(recipe))
    if not games_closed() or trial_running() is not False: raise RuntimeError("snapshot gate changed")
    # Existing stage_save whole-save backup/restore is bound by the rig booking.
    if restore(root, files) is not True: raise RuntimeError("snapshot restore not verified")


def verify_fixture(recipe, observe, *, collector, output):
    """Check native preconditions; acknowledgement/snapshot bytes are never a witness."""
    sample = observe()
    if not sample.get("raw_pointer") or not sample.get("runtime_id"):
        raise ValueError("native fixture readback identity/raw pointer required")
    required = recipe.get("preconditions", {})
    if not required: raise ValueError("fixture must declare native preconditions")
    collector.capture("STAGING-fixture-readback")
    (Path(output) / "STAGING-readback.json").write_bytes(encoded(sample))
    for key, expected in required.items():
        if sample.get(key) != expected: raise AssertionError("fixture precondition: " + key)
    return sample


def variants():
    """Declared fixture data; each booking binds native readback and exact snapshot pins."""
    return iter(json.loads(ipc.read_text(Path(__file__).with_name("i1_recipes.json"), encoding="utf-8"))["fixtures"])
