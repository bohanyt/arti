"""Offline pinned-spec and snapshot-plan generation; never imports a rig service."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import subprocess

from arti_runtime.witness import WitnessFailure
from scripts.free_play import witness as W

TEMPLATE = Path(__file__).with_name("witness_snapshots.template.json")
NATIVE_TREES = ("stardew-mod/ArtiStardewBridge", "stardew-mod/ArtiCore.Pure", "contracts")
SOURCES = {
    "menu": "native ui_pad menu identity/type (null on HUD)",
    "text": "native current page/letter/dialog text",
    "inspection": "native inspect revision, never collector sequence",
    "tab": "native GameMenu active tab name",
    "scroll": "native list offset/page position",
    "text_field": "native approved field value; no chat/naming permission changes",
    "tile": "native actor tile read after staging; no fixed coordinates",
    "location": "native actor location identity",
    "terrain": "native terrain at the staged target resolved by native identity/tile",
    "stamina": "native actor stamina",
    "selected_slot": "native actor active toolbar slot",
    "held_ticks": "native bounded hold lifecycle tick count",
    "tool_state": "native actor tool-use state, idle after release",
    "inventory_item": "native bound item ID/quality/slot quantity",
    "container_item": "native bound container/item ID/quality quantity",
    "cursor_count": "native held cursor stack; empty is zero",
    "page": "native letter/info page index or revision",
    "question": "native question identity/receiver transition",
    "answer": "native selected response, not requested answer",
    "sleeping": "native actor sleeping/in-bed fact, not team readiness",
    "day_serial": "native absolute game day serial across year/season rollover",
    "save_complete": "native completed save barrier for this day/session",
    "morning": "native morning/world-ready state after save",
    "fish_item": "native landed fish identity/quality quantity",
    "money": "native actor wallet",
    "ingredient": "native exact ingredient identity/quality quantity",
}


def facts(recipe):
    checks = recipe["verify"] + [c for step in recipe["steps"] for c in step["verify"]]
    return sorted({"menu", "location", "tile", *(check["fact"] for check in checks)})


def template():
    slots = {}
    for name, recipe in W.recipes().items():
        names = facts(recipe)
        slots[name] = dict(status="PENDING_CAPTURE", tag="STAGING", credit=False,
            staging_plan=recipe["stage"]["fixture"],
            capture_plan="Existing stage_save snapshot after owned barrier/closed games; hash the entire save folder. "
                         "Replay the captured bytes and reestablish transient UI through existing STAGING, then native-read back.",
            native_sources={fact: SOURCES[fact] for fact in names},
            # Native IDs/quality/slot/target/location/tile must also be pinned as
            # preconditions, even where the verifier checks a scalar quantity.
            identity_preconditions="Bind actor, location/tile, item/quality/slot and container/terrain identities from raw native state.",
            stage=dict(mode="snapshot", disposable=True, root=None, source_pin=None, files={}),
            snapshot=dict(name=None, save_name=None), paths={fact: None for fact in names},
            preconditions={}, offers=[dict(verb=step["verb"], args=None) for step in recipe["steps"]],
            timeout_s=120 if name in ("fish", "next_morning", "sleep_save") else 20,
            restore=recipe["restore"])
    return dict(schema="arti.witness.snapshots/1", evidence="PLAN_ONLY", capabilities=slots)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=W.ROOT, text=True).strip()


def build(*, package, rig_config, housekeeping, session_procedure, manifest, output,
          service=None, extra_artifacts=()):
    package, rig_config, session_procedure, manifest = map(
        lambda p: Path(p).resolve(), (package, rig_config, session_procedure, manifest))
    provenance_path = package / "provenance.json"
    provenance = W.read(provenance_path)
    source = provenance.get("source_sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", source):
        raise WitnessFailure("package_source_sha_required")
    head = git("rev-parse", "HEAD")
    # Source SHA and package source are distinct. Python-only witness commits
    # may reuse a DLL only when its native source tree remains byte-equivalent.
    if git("diff", source, head, "--", *NATIVE_TREES):
        raise WitnessFailure("package_native_source_mismatch")
    bridge, helper = package / "bridge/ArtiStardewBridge.dll", package / "helper/ArtiHostHelper.dll"
    artifacts = [provenance_path, rig_config, session_procedure, manifest,
                 W.RECIPES, Path(__file__), Path(__file__).with_name("witness.py"),
                 Path(__file__).with_name("witness_factory.py"),
                 Path(__file__).with_name("witness_session.py"),
                 Path(__file__).with_name("witness_capture.py"),
                 Path(__file__).with_name("soak.py"), Path(__file__).with_name("i1_staging.py"),
                 W.ROOT / "scripts/m1_operator/stage_save.py"]
    for target, key in ((bridge, "dll_sha256"), (helper, "helper_sha256")):
        if W.digest(target) != provenance.get(key):
            raise WitnessFailure("package_artifact_mismatch:" + target.name)
        artifacts.extend((target, target.with_name("manifest.json")))
    from scripts.free_play.soak import candidate_dependencies, staged_mod_preflight
    staged_mod_preflight(((bridge.parent, bridge.name, provenance["dll_sha256"]),
                         (helper.parent, helper.name, provenance["helper_sha256"])))
    artifacts.extend(candidate_dependencies(bridge, provenance).values())
    cfg = W.read(rig_config)
    for key in ("root", "hostname", "game", "appData", "contextFile", "ownerIdleScript"):
        if not cfg.get(key):
            raise WitnessFailure("rig_config_missing:" + key)
    artifacts.extend(Path(cfg[key]).resolve() for key in ("contextFile", "ownerIdleScript"))
    slots = W.read(manifest)
    if slots.get("schema") != "arti.witness.snapshots/1" or set(slots.get("capabilities", {})) != set(W.recipes()):
        raise WitnessFailure("whole_snapshot_plan_required")
    runtime = None
    if service:
        service = Path(service).resolve()
        runtime = dict(path=str(service), factory="booked")
        artifacts.append(service)
    artifacts.extend(Path(p).resolve() for p in extra_artifacts)
    pins = {str(Path(p).resolve()): W.digest(p) for p in artifacts}
    return dict(schema=W.SCHEMA, mode="witness", witness=True, code_sha=head,
        recipes_sha256=W.digest(W.RECIPES), evidence="PLAN_ONLY", runtime_config={},
        output=str(Path(output).resolve()),
        booking=dict(path=str(Path(__file__).with_name("witness_factory.py").resolve()), factory="booked"),
        pins=pins, package=dict(path=str(package), source_sha=source,
            dll_sha256=provenance["dll_sha256"], helper_sha256=provenance["helper_sha256"],
            provenance_sha256=W.digest(provenance_path), native_source_equivalent=True),
        procedures=dict(session_sha256=W.digest(session_procedure),
            restore_sha256=W.digest(Path(__file__).with_name("soak.py")),
            snapshot_sha256=W.digest(W.ROOT / "scripts/m1_operator/stage_save.py")),
        rig=dict(config=str(rig_config), housekeeping=str(Path(housekeeping).resolve()),
            session_procedure=str(session_procedure), service=runtime),
        capabilities=deepcopy(slots["capabilities"]))


def validate_binding(name, binding, *, captured):
    recipe = W.recipes()[name]
    if not set(facts(recipe)) <= set(binding.get("paths", {})):
        raise WitnessFailure("unbound_readback:" + name)
    if any(not isinstance(p, str) or not p.startswith("/") for p in binding["paths"].values()):
        raise WitnessFailure("unbound_readback:" + name)
    if (not binding.get("preconditions") or "menu" not in binding["preconditions"]
            or not set(facts(recipe)) <= set(binding["preconditions"])):
        raise WitnessFailure("fixture_preconditions_required:" + name)
    if len(binding.get("offers", [])) != len(recipe["steps"]):
        raise WitnessFailure("unbound_recipe:" + name)
    for step, offer in zip(recipe["steps"], binding["offers"]):
        if step["verb"] != offer.get("verb") or not isinstance(offer.get("args"), dict) or not offer["args"]:
            raise WitnessFailure("pinned_atomic_affordance_required:" + name)
    if not 0 < binding.get("timeout_s", 0) <= 120:
        raise WitnessFailure("invalid_timeout:" + name)
    snapshot = binding.get("snapshot", {})
    if any(not isinstance(snapshot.get(k), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", snapshot[k])
           for k in ("name", "save_name")):
        raise WitnessFailure("snapshot_name_required:" + name)
    if captured:
        stage = binding.get("stage", {})
        if (binding.get("status") != "CAPTURED" or stage.get("mode") != "snapshot"
                or stage.get("disposable") is not True or not stage.get("files")
                or not stage.get("source_pin") or not Path(stage.get("root") or "").is_absolute()):
            raise WitnessFailure("uncaptured_snapshot:" + name)


def summary(spec, names):
    # Safe to print: never dump configs, paths, specs, precondition values or prefs.
    return dict(command="plan", evidence="PLAN_ONLY", code_sha=spec["code_sha"],
                package_source_sha=spec["package"]["source_sha"], pins_checked=len(spec["pins"]),
                capabilities=[dict(capability=name, status=spec["capabilities"][name].get("status", "BOUND"),
                                   facts=facts(W.recipes()[name])) for name in names],
                live_ready=bool(spec["rig"].get("service")) and all(
                    spec["capabilities"][name].get("status") == "CAPTURED" for name in names))
