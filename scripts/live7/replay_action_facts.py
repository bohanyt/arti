"""ADR 139: read-only recorded-page replay; never reconstruct lost native acceptance as historical truth.

The provider archive holds rendered pages, not pre-wire ui.context. Reproject their
recorded controls and add an explicitly synthetic acceptance witness to exercise
each real page shape. Report journal fault counts separately from replay counts.
Run only after the operator releases the compute slot.
"""
import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType, SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.free_play import storage
import arti_stardew_skills as S
from arti_stardew_generic_ui import compact_ui_offer
from arti_runtime.collection_facts import acceptance


def renderer_at(ref):
    """Isolated CLI process only: run the real pre-fix renderer source on the identical witness inputs.

    Keep the evidence reader/projector unchanged. No checkout, build or game IO.
    These three modules contain all fix79A serialization changes; capture and
    motor code are not exercised by this recorded-page replay.
    """
    global S, compact_ui_offer
    root = Path(__file__).resolve().parents[2]
    for name, path in (("arti_runtime.detail_reads", "arti_runtime/detail_reads.py"),
                       ("arti_stardew_generic_ui", "arti_stardew_generic_ui.py"),
                       ("arti_stardew_skills", "arti_stardew_skills.py")):
        source = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=root, check=True,
                                capture_output=True, encoding="utf-8").stdout
        module = ModuleType(name)
        module.__file__ = str(root / path)
        sys.modules[name] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    S = sys.modules["arti_stardew_skills"]
    compact_ui_offer = sys.modules["arti_stardew_generic_ui"].compact_ui_offer


def witness():
    # A pressure witness only. These targets were NOT reconstructed from a58.
    return acceptance(dict(collection_fit=[
        dict(item="(O)788", name="Lost Axe", tile=[110, 81], accepts=False, reason="inventory_cannot_accept"),
        dict(item="(O)16", name="Wild Horseradish", tile=[3, 4], accepts=True, reason="")], extras=[
        dict(kind="harvest_crop", label="Parsnip", x=1, y=2, inventory_accepts=True),
        dict(kind="harvest_crop", label="Kale", x=2, y=2, inventory_accepts=False)]))


def archived_body(record):
    content = record.get("content") or {}
    prompt = content.get("prompt")
    if isinstance(prompt, str) and "\n\n{" in prompt:
        body = json.loads("{" + prompt.rsplit("\n\n{", 1)[1])
    else:
        messages = content.get("messages") or []
        body = next((json.loads(m["content"]) for m in reversed(messages) if m.get("role") == "user"), {})
    return body if body.get("mode") == "skills" and isinstance(body.get("skills"), list) else None


def capabilities(body):
    """Concrete name/arg pairs and native id/variant pairs; read links are not game abilities."""
    calls = set()
    for e in body.get("skills", ()):
        if e.get("name") == "read_boundary":
            continue
        for arg in e.get("args") or [None]:
            if isinstance(arg, str) and (arg.startswith("inspect:") or "{" in arg):
                continue
            # Motor-side and rendered id/variant forms encode the same press.
            if e["name"] == "ui_action" and isinstance(arg, str):
                motor = re.fullmatch(r"press:([AXY]):(\d+)(:shift)?", arg)
                if motor:
                    arg = f"press:{motor[2]}:" + ("shift" if motor[3] else "") + motor[1]
            calls.add((e["name"], arg))
    for g in (body.get("unknown_ui") or {}).get("groups", ()):
        for row in g.get("rows", ()):
            cid = row[g["fields"].index("id")]
            for v in g.get("variants", ()):
                calls.add(("ui_action", f"settle:{cid}" if v == "settle" else f"type:{cid}:{{text}}" if v == "type"
                           else f"press:{cid}:{v}"))
    return calls


def reproject(body):
    """Rehydrate only recorded UI rows/variants; do not invent unseen menu controls."""
    offer = copy.deepcopy(body)
    offer.pop("presentation", None)
    ui = offer.get("unknown_ui")
    if ui is not None:
        actions = [a for a in offer["skills"][0].get("args", ()) if "{" not in a and not a.startswith("inspect:")]
        components = []
        for group in ui.get("groups", ()):
            for row in group.get("rows", ()):
                cell = dict(zip(group["fields"], row))
                cid = cell["id"]
                source = group.get("source")
                component = dict(id=cid, kind=group.get("kind"),
                    field_path=f"{source}[{cell.get('slot', cid)}]" if isinstance(source, str) else f"replay[{cid}]",
                    label=cell.get("label") or ui.get("item_names", {}).get(cell.get("item"), ""))
                if cell.get("item"):
                    component["value"] = {k: cell.get(k, 0) for k in ("item", "stack", "quality")}
                if "accepts" in cell:
                    component["accepts_item"] = cell["accepts"]
                components.append(component)
                for v in group.get("variants", ()):
                    actions.append(f"settle:{cid}" if v == "settle" else f"type:{cid}:{{text}}" if v == "type"
                                   else f"press:{v[-1]}:{cid}" + (":shift" if v.startswith("shift") else ""))
        ui["components"] = components
        ui.pop("inspect", None)
        ui.pop("groups", None)
        offer["skills"][0]["args"] = list(dict.fromkeys(actions))
    offer["action_facts"] = witness()
    offer["render_budget"] = 16128
    return offer


def replay_minimal(run):
    """Exercise the smallest projection on every retained page shape, separate from normal fits.

    This is a forced-rung stress check, not a claim that a58 used this rung or
    that this projection alone measures the encoded provider request size.
    """
    report = dict(schema="fix79a-minimal-rung-replay/1", run=run.name, pages=0, pages_losing_witness_fact=0,
        limitation="Forced minimal projection with injected synthetic facts; no historical-native or provider-fit claim.")
    for journal in sorted(run.glob("runtime/live7/*/day-*/journal")):
        for path in storage.files(journal / "provider_archive", "*-request.json"):
            body = archived_body(json.loads(storage.read_bytes(path)))
            if body is None:
                continue
            offer = reproject(body)
            shown = (compact_ui_offer(offer, page_bytes=512, grounding_limit=128, header_limit=128, minimal=True)
                     if "unknown_ui" in offer else S.boundary_offer_page(offer, 512, minimal=True))
            report["pages"] += 1
            report["pages_losing_witness_fact"] += shown.get("action_facts") != witness()
    return report


def replay(run, *, progress=False):
    report = dict(schema="fix79a-recorded-page-replay/1", run=run.name,
        limitation="Native facts shed before the archive are unrecoverable. Synthetic witnesses prove budget survival, not a58 native acceptance.",
        recorded=dict(pages=0, pages_with_acceptance=0, render_block_omitted=0, pages_with_render_omitted=0,
                      ability_instances=0),
        replay=dict(pages=0, pages_losing_witness_fact=0, render_block_omitted=0, render_failed=0,
                    pages_losing_recorded_ability=0, pages_losing_reachable_ability=0, lost_ability_instances=0),
        repaginated=[], failures=[])
    for journal in sorted(run.glob("runtime/live7/*/day-*/journal")):
        if progress:
            print(f"replay {journal.parent.name}: {report['replay']['pages']} pages checked", file=sys.stderr, flush=True)
        for path in storage.files(journal, "*journal.jsonl"):
            for line in storage.read_bytes(path).decode("utf-8-sig").splitlines():
                row = json.loads(line)
                if row.get("ev") == "FAULT" and row.get("type") == "render_block_omitted":
                    report["recorded"]["render_block_omitted"] += 1
        archive = journal / "provider_archive"
        for path in storage.files(archive, "*-request.json"):
            body = archived_body(json.loads(storage.read_bytes(path)))
            if body is None:
                continue
            report["recorded"]["pages"] += 1
            report["recorded"]["pages_with_render_omitted"] += bool(body.get("render_omitted"))
            report["recorded"]["ability_instances"] += len(capabilities(body))
            text = json.dumps(body)
            if "inventory_accepts" in text or "collection_fit" in text or body.get("action_facts"):
                report["recorded"]["pages_with_acceptance"] += 1
            offer = reproject(body)
            echo = body.get("echo") or {}
            req = NS(skill_offer=offer, reason=body.get("reason", "skill_boundary"),
                     **{k: echo.get(k) for k in ("request_nonce", "goal_id", "goal_revision", "context_ref")})
            try:
                messages = S.render_skill_messages(req, lambda m: S.encoded_render_bytes(m) <= 13000)
                shown = json.loads(messages[1]["content"])
            except ValueError as exc:
                report["replay"]["render_failed"] += 1
                report["failures"].append(dict(page=str(path.relative_to(run)), error=str(exc)))
                continue
            report["replay"]["pages"] += 1
            report["replay"]["pages_losing_witness_fact"] += shown.get("action_facts") != witness()
            report["replay"]["render_block_omitted"] += bool(S.render_omitted(messages))
            missing = capabilities(body) - capabilities(shown)
            report["replay"]["pages_losing_recorded_ability"] += bool(missing)
            reachable = capabilities(shown)
            if missing and "unknown_ui" in offer:
                for index in range(shown["unknown_ui"]["inspect"]["pages"]):
                    offer["unknown_ui"]["inspect_page"] = index
                    page = S.render_skill_messages(req, lambda m: S.encoded_render_bytes(m) <= 13000)
                    reachable |= capabilities(json.loads(page[1]["content"]))
            lost = capabilities(body) - reachable
            report["replay"]["pages_losing_reachable_ability"] += bool(lost)
            report["replay"]["lost_ability_instances"] += len(lost)
            if missing and not lost:
                report["repaginated"].append(dict(page=str(path.relative_to(run)), moved=sorted(missing, key=str)))
            if lost:
                report["failures"].append(dict(page=str(path.relative_to(run)), lost=sorted(lost, key=str)))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--renderer-ref", help="git ref for the real pre-fix renderer, using the same injected witness")
    parser.add_argument("--minimal-only", action="store_true", help="forced minimal projection stress check, no provider-fit claim")
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.run.resolve()):
        parser.error("evidence is read-only; output must be outside the run")
    if args.renderer_ref:
        renderer_at(args.renderer_ref)
    report = replay_minimal(args.run) if args.minimal_only else replay(args.run, progress=True)
    report["renderer_ref"] = args.renderer_ref or "working tree"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("failures", "repaginated")}, indent=2))
    if args.minimal_only:
        return int(bool(report["pages_losing_witness_fact"]))
    return int(any(report["replay"][k] for k in ("pages_losing_witness_fact", "render_failed", "pages_losing_reachable_ability")))


if __name__ == "__main__":
    raise SystemExit(main())
