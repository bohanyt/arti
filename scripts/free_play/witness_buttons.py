"""Button-level proof for an isolated witness (FIX75D, ADR 132). No model and no provider: it proves hands only.

After a free witness run that did not complete the kind (she did not choose, or chose and left; ADR 135), the operator may run this from the same staged copy. It drives the
SAME generic controls through the existing rig service (`witness_rig.Service`: launch, inspect_context, the runtime's leased
executors, closeout) plus `soak.Baseline`. It never selects anything the adapter did not publish. The label travels with
the result: `hands_only`, `model` false, no agency claim, never gate credit, never free play.

    python -B -m scripts.free_play.witness_buttons KIND --spec SPEC --rig-config JSON --output NEW_DIR --record STAGING_JSON
    python -B -m scripts.free_play.witness_buttons KIND --record STAGING_JSON --dry-run

`--dry-run` prints the step list and touches nothing. Evidence ceiling of this tool: UNIT_TESTED against recorded menus;
`witness_rig.Service` itself has no rig run yet (ADR 96), so the first live run also tests the service.
A press needs her facing the target: the staged `FacingDirection` does that, or the world step ends `not_published`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime.witness import WitnessFailure, render_ui
from scripts.free_play import witness as W
from scripts.free_play.witness_rig import offered_world_targets
from scripts.free_play.witness_isolated import KINDS, MIN_FREE, crack_readback, good_deposit, good_donation, native_rows

GEODE, ARROWHEAD = "(O)535", "(O)101"
INVENTORY = r"(?:.*\.)?inventory\.inventory\[\d+\]"
STEP_TIMEOUT_S = 20
SETTLE_S = 8  # the anvil and the museum fade run on the game clock; the menu publishes again afterwards


def steps(kind, *, target_tile, bundle_name=None, needs=()):
    """The chain of one kind as generic selectors, from the plan's section 3. Tile and item ids come from the staging record."""
    world = dict(world=KINDS[kind]["target"], tile=target_tile)
    if kind == "bundle":
        chain = [world, dict(field=r"menu\.bundles.*", label=bundle_name)]
        for need in needs:
            chain += [dict(slot=need), dict(field=r"menu\.ingredientSlots.*", empty=True)]
        return chain
    if kind == "museum":
        return [world, dict(answer="Donate"), dict(slot=ARROWHEAD), dict(field=r"menu\.museumSpot")]
    if kind == "geode":
        return [world, dict(answer="Process"), dict(slot=GEODE), dict(field=r"menu\.geodeSpot")]
    raise ValueError(kind)


def control_for(view, step):
    """The published entry for one step, or None. render_ui is the adapter's publication; nothing is invented."""
    if view is None:
        return None
    offered = {e["args"]["control"]: e for e in render_ui(view)}
    if "answer" in step:
        keys = [o.get("index") for o in view.options if o.get("response_key") == step["answer"]]
        return next((offered[f"press:A:{k}"] for k in keys if f"press:A:{k}" in offered), None)
    for c in view.components:
        path, value = c.get("field_path", ""), c.get("value") or {}
        if "slot" in step:
            hit = value.get("item") == step["slot"] and re.fullmatch(INVENTORY, path)
        else:
            hit = re.fullmatch(step["field"], path) and (("label" not in step) or c.get("label") == step["label"]) and (
                not step.get("empty") or not value.get("item"))
        if hit and f"press:A:{c['id']}" in offered:
            return offered[f"press:A:{c['id']}"]
    return None


def world_target(sample, tile):
    """The target the game itself offers now, only if it is this action's tile. Named kinds and a counter's generic button both count."""
    for target in offered_world_targets(sample["state"].get("context")):
        if [target.get("tile_x"), target.get("tile_y")] == list(tile):
            return dict(target)
    return None


def proof_row(kind, status, *, term=None, done=()):
    return dict(schema="arti.witness.buttons/1", kind=kind, status=status, failing_term=term, label="BUTTON PROOF: hands only",
                hands_only=True, model=False, provider_used=False, agency=None, credit=False, scored=False, tag="STAGING",
                evidence="UNREVIEWED_LIVE", steps_done=list(done))


def drive(rig, chain, kind, output, *, clock=time.monotonic, sleep=time.sleep):
    """One observe -> one published control -> newer observe per step. Stops at the first step the game does not publish."""
    done = []
    for index, step in enumerate(chain):
        deadline = clock() + STEP_TIMEOUT_S
        while True:
            rig.check()
            sample = rig.observe()
            view = rig.views.get(sample["raw_pointer"])
            entry = world_target(sample, step["tile"]) if "world" in step else control_for(view, step)
            if entry is not None:
                break
            if clock() >= deadline:
                W.write(Path(output) / f"button-{index}-absent.json", dict(step=step, sample=sample["raw_pointer"]))
                return proof_row(kind, "BUTTON_FAILED", term=f"not_published:{index}:{sorted(step)[0]}", done=done)
            sleep(.25)
        result = rig.press_world(entry) if "world" in step else rig.execute(entry)
        W.write(Path(output) / f"button-{index}.json", dict(step=step, entry=entry, result=result))
        contract = result.get("result_v1") or {}
        if contract.get("state") in ("refused", "uncertain"):
            return proof_row(kind, "BUTTON_FAILED", term=f"press_refused:{index}:{contract.get('reason') or contract.get('term')}", done=done)
        done.append(dict(step=step, request_id=result.get("request_id"), status=result.get("lifecycle")))
        sleep(SETTLE_S if "field" in step and not step.get("empty") and "label" not in step else .25)
    return proof_row(kind, "BUTTON_PASSED", done=done)


def chain_from_record(kind, record):
    bundle = (record.get("facts") or {}).get("bundle") or {}
    return steps(kind, target_tile=record["position"]["target"], bundle_name=bundle.get("name"),
                 needs=["(O)" + n["item"] for n in bundle.get("needs", ())])


def effect_row(kind, output, row):
    """The effect, read like a free-play witness: the actor-verified native receipt; for a geode, the press readback."""
    if row["status"] != "BUTTON_PASSED":
        return row
    if kind == "geode":
        backs = [W.read(p).get("result", {}).get("native_receipt", {}).get("readback") for p in sorted(Path(output).glob("button-*.json"))]
        proven = [i for i, back in enumerate(backs) if back and crack_readback(dict(readback=back))]
    else:
        good = good_deposit if kind == "bundle" else good_donation
        native = "cc_bundle_deposit" if kind == "bundle" else "museum_donation"
        proven = [ref for launch in sorted(Path(output).glob("launch-*")) for ref, r in native_rows(launch, native) if good(r)]
    return dict(row, effect_refs=proven) if proven else dict(row, status="BUTTON_FAILED", failing_term="no_effect_proof", effect_refs=[])


def run(kind, *, rig, baseline_factory, template, saves, output, record):
    """Baseline -> staged copy into Saves -> owned launch -> steps -> owned closeout -> exact restore."""
    from scripts.free_play import soak
    output = Path(output)
    W.write(output / "WITNESS.json", dict(purpose="witness", tag="STAGING", credit=False, scored=False, kind=kind,
                                          plan="WITNESS_ISOLATED_PLAN", mode="button_proof", model=False))
    row, baseline, staged = proof_row(kind, "BUTTON_FAILED", term="not_started"), None, False
    root, lock = Path(rig.root), Path(rig.root) / "rig/free-play.lock"
    if (root / "STOP").exists() or (root / "operator-staging/run/SOAK_STOP").exists():
        raise WitnessFailure("global_stop")
    with lock.open("x", encoding="utf-8") as stream:  # the soak lock: one owner of the rig, never stolen
        stream.write(str(output.resolve()))
    try:
        rig.check_idle()
        baseline = baseline_factory(output)
        baseline.capture()
        staged = True
        soak.stage_template(Path(template), Path(saves), "Standard", output)
        rig.start(output)
        rig.verify_session()
        row = drive(rig, chain_from_record(kind, record), kind, output)
    except WitnessFailure as exc:
        row = proof_row(kind, "BUTTON_FAILED", term=str(exc))
    finally:
        if staged:
            closure = rig.closeout(output)
            if closure.get("barrier_resolved") is not True:
                row = dict(row, status="BUTTON_FAILED", failing_term="restore_barrier_unresolved; baseline retained, do not rerun")
            else:
                restored = baseline.restore()
                flags = {k: restored.get(k) for k in ("saves_identical", "files_identical", "file_sets_identical")}
                row = dict(effect_row(kind, output, row), restore=flags)
                if all(v is True for v in flags.values()):
                    lock.unlink()
                else:  # the lock stays: nothing books this rig until a person has looked
                    row = dict(row, status="BUTTON_FAILED", failing_term="restore_unverified; lock kept, do not rerun")
        else:
            lock.unlink()
        W.write(output / "BUTTON_PROOF.json", row)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("--record", required=True, help="the kind's staging.json")
    parser.add_argument("--spec", help="the kind's witness spec (Invoke-Soak shape)")
    parser.add_argument("--rig-config")
    parser.add_argument("--output")
    parser.add_argument("--dry-run", action="store_true")
    a = parser.parse_args(argv)
    record = W.read(a.record)
    chain = chain_from_record(a.kind, record)
    if a.dry_run:
        print(json.dumps(dict(kind=a.kind, label="BUTTON PROOF: hands only", model=False, min_free=MIN_FREE,
                              stand=record.get("position"), steps=chain), indent=2))
        return 0
    if not (a.spec and a.rig_config and a.output):
        parser.error("a real run needs --spec, --rig-config and a new --output")
    from scripts.free_play import witness_rig as R
    output = Path(a.output)
    output.mkdir(parents=True, exist_ok=False)
    spec = W.read(a.spec)
    rig = R.Service(spec=R.button_spec(spec, Path(a.rig_config), output), output=output)
    row = run(a.kind, rig=rig, baseline_factory=rig.baseline, template=spec["templates"]["Standard"],
              saves=Path(rig.app) / "Saves", output=output, record=record)
    print(json.dumps(row, indent=2))
    return 0 if row["status"] == "BUTTON_PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
