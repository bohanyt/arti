"""Contract-11 witness orchestrator. Import/list/plan/report are strictly offline.

Live lifecycle is bound by a pinned rig factory, as in i1_runner.drill. This module
has no motor, provider transport, SMAPI launch, save-edit or admission implementation.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime.witness import WitnessFailure, WitnessFilter, reject_witness_config
from scripts.free_play.evaluate import Evidence, action_outcome

SCHEMA = "arti.witness/1"
RECIPES = Path(__file__).with_name("witness_recipes.json")
IDENTITY = ("runtime_instance_id", "session_id", "player_id", "screen_id")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def recipes():
    return read(RECIPES)["capabilities"]


def pointer(value, path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise WitnessFailure("readback_path_invalid")
    try:
        for key in path[1:].split("/"):
            key = key.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (KeyError, IndexError, TypeError, ValueError):
        raise WitnessFailure("readback_missing:" + path) from None
    return value


def check_sample(sample):
    if (not isinstance(sample, dict) or sample.get("authoritative") is not True
            or sample.get("complete") is not True or sample.get("stale") is not False
            or not sample.get("raw_pointer") or not isinstance(sample.get("state"), dict)
            or any(sample.get(k) is None for k in IDENTITY)
            or type(sample.get("tick")) is not int):
        raise WitnessFailure("readback_unavailable")


def check_samples(before, after):
    for sample in (before, after):
        check_sample(sample)
    if any(before[k] != after[k] for k in IDENTITY):
        raise WitnessFailure("identity_changed")
    if after["tick"] <= before["tick"] or after["raw_pointer"] == before["raw_pointer"]:
        raise WitnessFailure("readback_not_fresh")


def verify(checks, before, after, paths):
    """Pure projection of independently observed game state; no command-result data."""
    check_samples(before, after)
    for check in checks:
        name, op = check["fact"], check["op"]
        if name not in paths:
            raise WitnessFailure("unbound_readback:" + name)
        old, new = (pointer(s["state"], paths[name]) for s in (before, after))
        expected = check.get("value")
        if op == "changed":
            ok = old != new
        elif op == "equal":
            ok = new == expected and type(new) is type(expected)
        elif op == "became":
            ok = old != new and new == expected and type(new) is type(expected)
        elif op == "nonempty":
            ok = bool(new)
        elif op == "delta":
            ok = type(old) in (int, float) and type(new) in (int, float) and new - old == expected
        elif op == "increased":
            ok = type(old) in (int, float) and type(new) in (int, float) and new > old
        elif op == "decreased":
            ok = type(old) in (int, float) and type(new) in (int, float) and new < old
        elif op == "same":
            ok = old == new
        else:
            raise WitnessFailure("unknown_verifier:" + op)
        if not ok:
            raise WitnessFailure(check["term"])


def result_term(result):
    """Reuse free-play's terminal result classifier; never use it as effect proof."""
    if result.get("result_v1", {}).get("state") in ("refused", "uncertain"):
        contract = result["result_v1"]
        return contract.get("term") or contract.get("reason") or contract["state"]
    if action_outcome(result) != "verified" or result.get("cleanup_confirmed") is not True:
        return result.get("reason") or "result_unverified"
    if not result.get("request_id"):
        return "request_identity_missing"
    return None


def stage_snapshot(rig, fixture, output):
    from scripts.free_play import i1_staging
    if fixture.get("mode") != "snapshot":
        raise WitnessFailure("whole_disposable_snapshot_required")
    i1_staging.snapshot(fixture, root=fixture["root"], output=output / "STAGING-snapshot",
        restore=rig.restore_snapshot, games_closed=rig.games_closed, trial_running=rig.trial_running)


def stage_debug(rig, recipe, output):
    from scripts.free_play import i1_staging
    commands = recipe["stage"]["debug"]
    if commands:
        i1_staging.stage(dict(target="farmhand", commands=commands), collector=rig.collector,
            output=output / "STAGING-debug", catalog=rig.catalog, locations=rig.locations,
            run_script=rig.run_script, trial_running=rig.trial_running)


def preconditions(sample, binding):
    check_sample(sample)
    if not binding.get("preconditions"):
        raise WitnessFailure("fixture_preconditions_required")
    for fact, expected in binding["preconditions"].items():
        actual = pointer(sample["state"], binding["paths"][fact])
        if actual != expected or type(actual) is not type(expected):
            raise WitnessFailure("fixture_precondition:" + fact)


def observe(rig, output):
    sample = rig.observe()
    check_sample(sample)
    # A receipt string or command result file cannot stand in for the raw state
    # observation. The booking retains its existing observation under native/.
    source = (output / sample["raw_pointer"]).resolve()
    if (output / "native").resolve() not in source.parents or not source.is_file():
        raise WitnessFailure("raw_observation_not_retained")
    return sample


def run_case(capability, binding, rig, output, *, code_sha):
    """Backup -> checked STAGING -> model choices -> state readback -> closeout/restore.

    Staging uses i1_staging and baseline MUST be soak.Baseline. The rig methods
    are the existing booking's session/model/adapter lifecycle, not new motors.
    """
    from scripts.free_play.soak import Baseline
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    recipe = recipes()[capability]
    row = dict(schema=SCHEMA, purpose="witness", capability=capability, build_sha=code_sha, status="FAIL", failing_term=None,
               depends=None, evidence_path=str(output.resolve()), evidence="UNIT_TESTED" if rig.offline else "UNREVIEWED_LIVE")
    write(output / "WITNESS.json", dict(schema=SCHEMA, purpose="witness", capability=capability))
    events, staged, baseline = [], False, None
    try:
        rig.check_idle()
        if not rig.games_closed() or rig.trial_running() is not False:
            raise WitnessFailure("unenrolled_closed_games_required")
        baseline = rig.baseline(output)
        if not isinstance(baseline, Baseline):
            raise WitnessFailure("existing_baseline_required")
        baseline.capture()  # no fixture, debug, config or save changes before this
        events.append(dict(tag="STAGING", phase="backup", evidence="baseline.json"))
        staged = True  # partial staging still requires owned closeout and restore
        if hasattr(rig, "after_backup"):
            rig.after_backup()
        events.append(dict(tag="STAGING", phase="fixture", recipe=recipe["stage"], binding=binding["stage"]))
        stage_snapshot(rig, binding["stage"], output)
        rig.start(output)
        if binding["stage"].get("debug_in_snapshot"):
            rig.resume_fixture(capability=capability, binding=deepcopy(binding), output=output)
        else:
            stage_debug(rig, recipe, output)
        rig.verify_session()  # rig-owned session.py verify or the same native session checks
        initial = observe(rig, output)
        write(output / "before.json", initial)
        preconditions(initial, binding)
        filter_ = WitnessFilter(witness=True, mode="witness")
        current = initial
        for index, step in enumerate(recipe["steps"]):
            rig.check()
            check_sample(current)
            selector = dict(binding["offers"][index])
            if selector.get("verb") != step["verb"]:
                raise WitnessFailure("recipe_verb_mismatch")
            shown, target = filter_.render(current.get("affordances"), selector,
                menu_open=pointer(current["state"], binding["paths"]["menu"]) is not None,
                close_unavailable=current.get("close_unavailable"))
            if step["verb"] == "type":
                # Native publishes a free-text field. This is payload binding to
                # the already rendered field, never another offered control.
                target["args"]["text"] = "WIT1"
                shown[0] = deepcopy(target)
            write(output / f"offer-{index}.json", dict(published=current["affordances"], rendered=shown, target=target))
            # Do not expose the unfiltered publication or a preferred answer to the model.
            facts = {name: deepcopy(pointer(current["state"], path)) for name, path in binding["paths"].items()}
            choice = rig.choose(dict(state=facts, affordances=deepcopy(shown)))
            write(output / f"choice-{index}.json", choice)
            if choice not in shown:
                raise WitnessFailure("model_choice_not_offered")
            # Adapter revalidates its own publication at execution, not this filter.
            result = rig.execute(deepcopy(choice))
            if result.get("result_v1", {}).get("state") == "in_progress":
                write(output / f"in-progress-{index}.json", result)
                result = rig.await_result(result, timeout_s=binding.get("timeout_s", 20))
            write(output / f"result-{index}.json", result)
            term = result_term(result)
            if choice != target:
                # Exit is a real offered choice, not a decorative escape hatch.
                # Honor it once, retain independent readback, and grant no credit
                # for the capability which the model declined to exercise.
                after = observe(rig, output)
                write(output / f"after-{index}.json", after)
                check_samples(current, after)
                events.append(dict(tag="WITNESS", kind="model_exit", step=index,
                    request_id=result.get("request_id"), result_term=term,
                    before=current["raw_pointer"], after=after["raw_pointer"]))
                raise WitnessFailure("model_chose_close_back")
            deadline = time.monotonic() + binding.get("timeout_s", 20)
            last_failure = "readback_unavailable"
            while True:
                rig.check()
                after = observe(rig, output)  # separate observation, even if result claimed done
                write(output / f"after-{index}.json", after)
                try:
                    verify(step["verify"], current, after, binding["paths"])
                    break
                except WitnessFailure as exc:
                    last_failure = str(exc)
                    if term or time.monotonic() >= deadline:
                        raise WitnessFailure(term or last_failure) from None
                    rig.wait(min(.1, max(0, deadline - time.monotonic())))
            if term:
                raise WitnessFailure(term)
            events.append(dict(tag="WITNESS", step=index, request_id=result["request_id"],
                               before=current["raw_pointer"], after=after["raw_pointer"]))
            current = after
        # Final checks cover the whole transaction (cursor settle, inventory conservation).
        verify(recipe["verify"], initial, current, binding["paths"])
        row["status"] = "PASS"
    except Exception as exc:
        row["failing_term"] = str(exc) if isinstance(exc, WitnessFailure) else type(exc).__name__ + ":" + str(exc)
        if row["failing_term"] == "not_published":
            row["depends"] = recipe.get("depends")
    finally:
        try:
            if staged:
                closure = rig.closeout(output)
                write(output / "closure.json", closure)
                if not closure or closure.get("barrier_resolved") is not True or not rig.games_closed():
                    raise WitnessFailure("restore_barrier_unresolved")
                restored = baseline.restore()
                if not restored or not all(restored.get(k) is True for k in ("saves_identical", "files_identical")):
                    raise WitnessFailure("restore_unverified")
                if hasattr(rig, "finish_restore"):
                    rig.finish_restore(restored)
            elif hasattr(rig, "release_unstaged"):
                rig.release_unstaged()
        except Exception as exc:
            row.update(status="FAIL", primary_failure=row["failing_term"], failing_term="restore:" + str(exc), depends=None)
        write(output / "events.json", events)
        evidence = Evidence(output)
        for path in sorted(output.rglob("*")):
            if path.is_file() and "baseline" not in path.relative_to(output).parts:
                evidence.track(path)
        row["evidence_sha256"] = evidence.files
        write(output / "VERDICT.json", row)
    return row


def load_spec(path, capability, *, purpose="run"):
    """Read and validate every pin before importing booking code or touching a rig."""
    path = Path(path).resolve()
    spec = read(path)
    if spec.get("schema") != SCHEMA or spec.get("mode") != "witness" or spec.get("witness") is not True:
        raise WitnessFailure("witness_flag_required")
    reject_witness_config(spec.get("runtime_config", {}))
    if spec.get("recipes_sha256") != digest(RECIPES):
        raise WitnessFailure("recipes_pin_mismatch")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    if spec.get("code_sha") != head or dirty:
        raise WitnessFailure("clean_pinned_checkout_required")
    names = list(recipes()) if capability == "all" else [capability]
    from scripts.free_play import witness_pins as P
    for name in names:
        binding = spec.get("capabilities", {}).get(name)
        recipe = recipes()[name]
        if "rig" in spec:
            if not binding:
                raise WitnessFailure("unbound_recipe:" + name)
            if purpose != "plan":
                P.validate_binding(name, binding, captured=purpose == "run")
                if purpose == "run":
                    captured = binding.get("capture", {})
                    if (captured.get("code_sha") != head or captured.get("recipes_sha256") != spec["recipes_sha256"]
                            or captured.get("package") != spec["package"]):
                        raise WitnessFailure("snapshot_capture_provenance_mismatch:" + name)
                    root = Path(binding["stage"]["root"])
                    targets = [root.parent / "manifest.json", *(root / key for key in binding["stage"]["files"])]
                    if any(spec.get("pins", {}).get(str(p.resolve())) != digest(p) for p in targets):
                        raise WitnessFailure("snapshot_pin_required:" + name)
            continue
        if not binding or len(binding.get("offers", [])) != len(recipe["steps"]):
            raise WitnessFailure("unbound_recipe:" + name)
        if (not 0 < binding.get("timeout_s", 20) <= 120 or not binding.get("stage")
                or not binding.get("paths", {}).get("menu") or not binding.get("preconditions")):
            raise WitnessFailure("invalid_binding:" + name)
        for step, selector in zip(recipe["steps"], binding["offers"]):
            if selector.get("verb") != step["verb"] or not selector.get("args"):
                raise WitnessFailure("pinned_atomic_affordance_required:" + name)
    pins = spec.get("pins", {})
    booking = spec.get("booking", {})
    if not pins or booking.get("path") not in pins or not booking.get("factory"):
        raise WitnessFailure("pinned_booking_required")
    for relative, expected in pins.items():
        target = Path(relative)
        target = target if target.is_absolute() else path.parent / target
        if digest(target) != expected:
            raise WitnessFailure("artifact_pin_mismatch:" + target.name)
    if "rig" in spec:
        # Provenance/config paths must be part of the checked set, not assertions
        # in an unpinned manifest. Never import the service for offline planning.
        required = [spec["rig"]["config"], spec["rig"]["session_procedure"],
                    str(Path(spec["package"]["path"]) / "provenance.json")]
        if spec["rig"].get("service"):
            required.append(spec["rig"]["service"]["path"])
        if any(str(Path(p).resolve()) not in pins for p in required):
            raise WitnessFailure("procedure_pin_required")
        if purpose != "plan" and not spec["rig"].get("service"):
            raise WitnessFailure("native_rig_service_unbound")
    spec["spec_sha256"] = digest(path)
    spec["spec_path"] = str(path)
    return spec, names


def load_rig(spec, output):
    booking = spec["booking"]
    path = Path(booking["path"])
    if not path.is_absolute():
        path = Path(spec["spec_path"]).parent / path
    if digest(path) != spec["pins"][booking["path"]]:
        raise WitnessFailure("booking_pin_changed")
    module_spec = importlib.util.spec_from_file_location("arti_witness_booking", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    rig = getattr(module, booking["factory"])(spec=spec, output=output)
    if rig.offline is not False:
        raise WitnessFailure("live_booking_required")
    return rig


def report(roots, output):
    """Separate table; never EVALUATION.json, PM1_SCOREBOARD or free-play minutes."""
    output = Path(output)
    if output.name != "WITNESS_AUTO.md":
        raise WitnessFailure("witness_table_only")
    rows = []
    for root in roots:
        root = Path(root)
        paths = [root] if root.is_file() else sorted(root.rglob("VERDICT.json"))
        for path in paths:
            value = read(path)
            if value.get("schema") != SCHEMA or value.get("capability") not in recipes():
                continue
            for name, expected in value["evidence_sha256"].items():
                if digest(path.parent / name) != expected:
                    raise WitnessFailure("evidence_hash_mismatch")
            rows.append(value)
    # Offline PASS rows, mixed builds, incomplete sets and later failures cannot
    # unlock contract rule 11. A live result still requires CT's evidence review.
    eligible = []
    for sha in sorted({row["build_sha"] for row in rows}):
        build = [row for row in rows if row["build_sha"] == sha]
        if ({row["capability"] for row in build} == set(recipes())
                and all(row["status"] == "PASS" and row["evidence"] == "UNREVIEWED_LIVE" for row in build)):
            eligible.append(sha)
    lines = ["# Witness results (contract rule 11)", "", "Separate from free play; no minutes or F credit. Offline fixtures are UNIT_TESTED only.", "",
             "Complete live builds awaiting CT review: " + (", ".join(eligible) if eligible else "none; rule-11 gate remains closed") + ".", "",
             "| Capability | Build SHA | PASS/FAIL | Failing term | Evidence path |", "|---|---|---|---|---|"]
    for row in rows:
        term = row["failing_term"] or "—"
        if row.get("depends"):
            term += "; depends: " + row["depends"]
        if row.get("evidence") == "UNIT_TESTED":
            term += "; UNIT_TESTED"
        cells = [row["capability"], row["build_sha"], row["status"], term, row["evidence_path"]]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def unattempted(capability, output, code_sha, term):
    """A stopped sweep still reports every requested capability, without input."""
    output.mkdir(parents=True, exist_ok=False)
    write(output / "WITNESS.json", dict(schema=SCHEMA, purpose="witness", stop=term))
    row = dict(schema=SCHEMA, purpose="witness", capability=capability, build_sha=code_sha,
        status="FAIL", failing_term="not_run_after:" + term, depends=None,
        evidence="NOT_RUN", evidence_path=str(output.resolve()),
        evidence_sha256={"WITNESS.json": digest(output / "WITNESS.json")})
    write(output / "VERDICT.json", row)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    plan = sub.add_parser("plan"); plan.add_argument("capability", choices=[*recipes(), "all"])
    plan.add_argument("--spec", type=Path)
    template = sub.add_parser("template"); template.add_argument("--output", type=Path, required=True)
    pin = sub.add_parser("pin")
    for flag in ("package", "rig-config", "housekeeping", "session-procedure", "manifest", "output", "run-output"):
        pin.add_argument("--" + flag, type=Path, required=True)
    # The native rig service entry (ADR 96); $ARTI_WITNESS_NATIVE_SERVICE names it on the rig.
    pin.add_argument("--service", type=Path, default=os.environ.get("ARTI_WITNESS_NATIVE_SERVICE") or None)
    pin.add_argument("--artifact", type=Path, action="append", default=[])
    capture = sub.add_parser("capture"); capture.add_argument("capability", choices=[*recipes(), "all"])
    capture.add_argument("--spec", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--write-spec", type=Path, required=True)
    run = sub.add_parser("run"); run.add_argument("capability", choices=[*recipes(), "all"])
    run.add_argument("--spec", type=Path, required=True)
    table = sub.add_parser("report"); table.add_argument("roots", nargs="+", type=Path)
    table.add_argument("--output", type=Path, default=ROOT.parent / "runbooks/WITNESS_AUTO.md")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print("\n".join(recipes()))
        elif args.command == "plan":
            if args.spec:
                from scripts.free_play import witness_pins as P
                spec, names = load_spec(args.spec, args.capability, purpose="plan")
                print(json.dumps(P.summary(spec, names), indent=2))
            else:
                print(json.dumps(recipes() if args.capability == "all" else recipes()[args.capability], indent=2))
        elif args.command == "template":
            from scripts.free_play import witness_pins as P
            if args.output.exists():
                raise WitnessFailure("new_manifest_required")
            write(args.output, P.template())
            print("21 STAGING snapshot slots; PLAN_ONLY")
        elif args.command == "pin":
            from scripts.free_play import witness_pins as P
            if args.output.exists():
                raise WitnessFailure("new_pinned_spec_required")
            spec = P.build(package=args.package, rig_config=args.rig_config, housekeeping=args.housekeeping,
                session_procedure=args.session_procedure, manifest=args.manifest,
                output=args.run_output, service=args.service, extra_artifacts=args.artifact)
            write(args.output, spec)
            print(f"Pinned {len(spec['pins'])} artifacts and 21 snapshot slots; PLAN_ONLY")
        elif args.command == "capture":
            from scripts.free_play.witness_capture import capture
            spec, names = load_spec(args.spec, args.capability, purpose="capture")
            count = capture(spec, names, args.output, args.write_spec)
            print(f"Captured {count} STAGING snapshots; no witness credit")
        elif args.command == "report":
            report(args.roots, args.output)
        else:
            spec, names = load_spec(args.spec, args.capability)
            output = Path(spec["output"])
            output.mkdir(parents=True, exist_ok=False)
            write(output / "WITNESS.json", dict(schema=SCHEMA, purpose="witness", spec=spec))
            rows = []
            failure_counts = {}
            stopped = None
            for name in names:
                if stopped:
                    row = unattempted(name, output / name, spec["code_sha"], stopped)
                    rows.append(row)
                    print(f"{name}: FAIL {row['failing_term']}")
                    continue
                rig = load_rig(spec, output / name)
                row = run_case(name, spec["capabilities"][name], rig, output / name, code_sha=spec["code_sha"])
                rows.append(row)
                print(f"{name}: {row['status']} {row['failing_term'] or ''}")
                term = row["failing_term"]
                # No third attempt for one repeated apparatus/bug class. Missing
                # affordances are per-capability evidence, not a retry of input.
                if term and term != "not_published":
                    family = term.split(":", 1)[0]
                    failure_counts[family] = failure_counts.get(family, 0) + 1
                    if failure_counts[family] >= 2:
                        write(output / "STOP-root-cause.json", dict(term=term, failure_class=family,
                            evidence=[r["evidence_path"] for r in rows if r.get("evidence_path")],
                            diagnosis="Repeated failing check; preserve evidence and diagnose before a third attempt."))
                        stopped = family
                if term and term.startswith("restore:"):
                    stopped = term  # never stage the next capability over unresolved saves
            report([output], spec.get("report", ROOT.parent / "runbooks/WITNESS_AUTO.md"))
            return int(len(rows) != len(names) or any(row["status"] != "PASS" for row in rows))
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
