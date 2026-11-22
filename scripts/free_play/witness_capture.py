"""Live-only snapshot factory. All output is STAGING, never witness credit.

Uses the rig's existing staging/launch/closeout and stage_save snapshot/restore.
It neither chooses nor executes a witness action. A second load proves that the
saved bytes plus declared transient STAGING reproduce the native preconditions.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from arti_runtime.witness import WitnessFailure, WitnessFilter
from scripts.free_play import witness as W
from scripts.free_play import witness_pins as P


def closed(rig, output):
    closure = rig.closeout(output)
    index = len(list(output.glob("capture-closure-*.json")))
    W.write(output / f"capture-closure-{index}.json", closure)
    if not closure or closure.get("barrier_resolved") is not True or not rig.games_closed():
        raise WitnessFailure("restore_barrier_unresolved")


def case(name, binding, rig, output, spec):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    W.write(output / "WITNESS.json", dict(purpose="witness", tag="STAGING", credit=False))
    baseline, staged, restored = None, False, False
    try:
        rig.check_idle()
        if not rig.games_closed() or rig.trial_running() is not False:
            raise WitnessFailure("closed_unenrolled_games_required")
        baseline = rig.baseline(output)
        from scripts.free_play.soak import Baseline
        if not isinstance(baseline, Baseline):
            raise WitnessFailure("existing_baseline_required")
        baseline.capture()
        staged = True
        rig.after_backup()
        # Existing rig-owned checked STAGING, farmhand only. Never model credit.
        rig.prepare_capture(capability=name, recipe=W.recipes()[name], binding=deepcopy(binding), output=output)
        rig.verify_session()
        before = W.observe(rig, output)
        W.preconditions(before, binding)
        W.write(output / "STAGING-before-close.json", before)
        closed(rig, output)
        stage = rig.capture_snapshot(binding)
        candidate = deepcopy(binding)
        candidate.update(status="CAPTURED", stage=stage)
        W.stage_snapshot(rig, stage, output)
        rig.start(output)
        rig.resume_fixture(capability=name, binding=deepcopy(candidate), output=output)
        rig.verify_session()
        replay = W.observe(rig, output)
        W.preconditions(replay, candidate)
        # Only the first target can be published without performing the action.
        # Later settle/transfer selectors are revalidated by run at their turn.
        WitnessFilter(witness=True, mode="witness").render(replay["affordances"], candidate["offers"][0],
            menu_open=W.pointer(replay["state"], candidate["paths"]["menu"]) is not None,
            close_unavailable=replay.get("close_unavailable"))
        W.write(output / "STAGING-replay-readback.json", replay)
        candidate["capture"] = dict(tag="STAGING", credit=False, code_sha=spec["code_sha"],
            recipes_sha256=spec["recipes_sha256"], package=spec["package"],
            snapshot_manifest_sha256=W.digest(Path(stage["root"]).parent / "manifest.json"),
            native_readback_sha256=W.digest(output / "STAGING-replay-readback.json"))
        return candidate
    finally:
        if staged:
            closed(rig, output)
            restored = baseline.restore()
            if not restored or not all(restored.get(k) is True for k in ("saves_identical", "files_identical")):
                raise WitnessFailure("restore_unverified")
            rig.finish_restore(restored)
        elif hasattr(rig, "release_unstaged"):
            rig.release_unstaged()
        W.write(output / "STAGING.json", dict(tag="STAGING", credit=False, restored=bool(restored)))


def capture(spec, names, output, write_spec):
    # Validate the entire plan before loading/importing any rig service.
    for name in names:
        P.validate_binding(name, spec["capabilities"][name], captured=False)
    if not spec["rig"].get("service"):
        raise WitnessFailure("native_rig_service_unbound")
    output = Path(output).resolve()
    if output == Path(spec["output"]).resolve():
        raise WitnessFailure("separate_capture_output_required")
    if Path(write_spec).exists():
        raise WitnessFailure("new_live_spec_required")
    output.mkdir(parents=True, exist_ok=False)
    W.write(output / "WITNESS.json", dict(purpose="witness", tag="STAGING", credit=False))
    candidate = deepcopy(spec)
    for key in ("spec_path", "spec_sha256"):
        candidate.pop(key, None)
    for name in names:
        rig = W.load_rig(spec, output / name)
        candidate["capabilities"][name] = case(name, spec["capabilities"][name], rig, output / name, spec)
        binding = candidate["capabilities"][name]
        root = Path(binding["stage"]["root"])
        for path in (root.parent / "manifest.json", output / name / "STAGING-replay-readback.json",
                     output / name / "session-verification.json", *(root / key for key in binding["stage"]["files"])):
            candidate["pins"][str(path.resolve())] = W.digest(path)
    candidate["evidence"] = "STAGING_ONLY"
    W.write(write_spec, candidate)
    return len(names)
