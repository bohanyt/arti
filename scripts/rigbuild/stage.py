"""Staging and bookings (ADR 245): the tails of `stage_tb.py` (testbuild140) and `stage.py` (pkg112).

A staged folder is `bridge\\` (ArtiStardewBridge.dll, its five dependency DLLs, manifest.json), `helper\\`
(ArtiHostHelper.dll, manifest.json) and `provenance.json`. It is assembled in `<evidence>\\candidate`, preflighted
with the soak's own `staged_mod_preflight` and `candidate_dependencies`, copied create-new to its target and
preflighted again; the staged hashes must equal the hashes the native chain tested.

A booking is an earlier booking with its five artifact fields (`dll`, `dllSha256`, `helper`, `helperSha256`,
`provenance`) pointed at the new folder; every other field (rig config, farm, templates, trial config, cable note)
is the earlier booking's. Bookings are written create-new and never replace an existing one.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from scripts.rigbuild.evidence import sha, tree_hashes
from scripts.rigbuild.machine import Refused

BRIDGE, HELPER = "ArtiStardewBridge", "ArtiHostHelper"
DEPENDENCIES = ("ArtiCore.Pure.dll", "JetBrains.Annotations.dll", "Json.More.dll", "JsonPointer.Net.dll",
                "JsonSchema.Net.dll")
ARTIFACT_FIELDS = ("dll", "dllSha256", "helper", "helperSha256", "provenance")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_new(path, value):
    with Path(path).open("x", encoding="utf8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")


def assemble(checkout, candidate, dll_sha, helper_sha):
    """Copy the built binaries and manifests into a new candidate folder; hashes must equal the tested ones."""
    candidate = Path(candidate)
    candidate.mkdir()
    for part, project, deps in (("bridge", BRIDGE, DEPENDENCIES), ("helper", HELPER, ())):
        folder = candidate / part
        folder.mkdir()
        source = Path(checkout) / "stardew-mod" / project
        for name in (project + ".dll", *deps):
            shutil.copy2(source / "bin/Release/net6.0" / name, folder / name)
        shutil.copy2(source / "manifest.json", folder / "manifest.json")
    if sha(candidate / "bridge" / (BRIDGE + ".dll")) != dll_sha or sha(candidate / "helper" / (HELPER + ".dll")) != helper_sha:
        raise Refused("built binaries changed since the native chain")
    return {n: sha(candidate / "bridge" / n) for n in DEPENDENCIES}


def preflight(folder, provenance):
    from scripts.free_play.soak import candidate_dependencies, staged_mod_preflight
    folder = Path(folder)
    staged_mod_preflight(((folder / "bridge", BRIDGE + ".dll", provenance["dll_sha256"]),
                          (folder / "helper", HELPER + ".dll", provenance["helper_sha256"])))
    if len(candidate_dependencies(folder / "bridge" / (BRIDGE + ".dll"), provenance)) != len(DEPENDENCIES):
        raise Refused("candidate dependencies incomplete in " + str(folder))


def provenance(identity, native, deps, *, checkout, kind, python_gates, native_chain, previous_package=None,
               previous_native=None):
    """pkg110/pkg112 shape; a test build adds not_a_package (testbuild135 brief)."""
    head = identity["source_sha"]
    row = dict(source_sha=head, source_tree=identity["source_tree"], native_build_source_sha=head,
               helper_native_build_source_sha=head, dll_sha256=native["dlls"][BRIDGE],
               helper_sha256=native["dlls"][HELPER], bridge_built_from_source=True, helper_built_from_source=True,
               bridge_carried=False, helper_carried=False, dependencies=deps, full_python_suite_run=False,
               test_repairs=[], python_source_directory=str(checkout), delta_baseline_sha=None, kind=kind,
               previous_package_source_sha=previous_package, previous_native_build_source_sha=previous_native,
               python_gates=python_gates, native_chain=str(native_chain), procedure_sha=identity.get("tool_sha"))
    if kind == "test-build":
        row["not_a_package"] = True
    return row


def copy_staged(candidate, target, prov):
    target = Path(target)
    if target.exists():
        raise Refused("staging target exists (never overwritten): " + str(target))
    shutil.copytree(candidate, target)
    if tree_hashes(candidate) != tree_hashes(target):
        raise Refused("staged copy differs from the candidate")
    preflight(target, prov)
    return tree_hashes(target)


def compose_booking(build, like, *, code_sha=None, output_root=None):
    """An earlier booking with its five artifact fields pointed at `build` (hashes checked against provenance).
    `code_sha` and `output_root` add the two fields a soak spec needs (`Invoke-Soak.ps1 -Spec`)."""
    build = Path(build)
    prov = read(build / "provenance.json")
    dll, helper = build / "bridge" / (BRIDGE + ".dll"), build / "helper"
    for path, field in ((dll, "dll_sha256"), (helper / (HELPER + ".dll"), "helper_sha256")):
        if not path.is_file() or sha(path) != prov.get(field):
            raise Refused(f"{path} missing or its hash differs from provenance.{field}")
    preflight(build, prov)
    row = read(like)
    missing = [k for k in ("rigConfig", "farm", "template", "hostTemplate", "trialConfig") if k not in row]
    if missing:
        raise Refused(f"booking {like} lacks {', '.join(missing)}; pass an earlier free-play or mabar booking")
    row.update(dll=str(dll), dllSha256=prov["dll_sha256"], helper=str(helper), helperSha256=prov["helper_sha256"],
               provenance=str(build / "provenance.json"))
    if code_sha:
        row["codeSha"] = code_sha
    if output_root:
        row["outputRoot"] = str(output_root)
    return row


def write_booking(path, row, like):
    """Create-new; read back equal; the earlier booking is unchanged."""
    path, before = Path(path), sha(like)
    if path.exists():
        raise Refused("booking exists (never overwritten): " + str(path))
    write_new(path, row)
    if read(path) != row or sha(like) != before:
        raise Refused("booking readback differs or the earlier booking changed")
    return sha(path)
