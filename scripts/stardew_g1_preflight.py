"""No-game source preflight for the P0-P6 candidate; never activates the runtime.

Reads only named repository sources and git metadata. Does not import the bridge,
read private config/saves, open a socket, call a provider, or launch/deploy a game.
Exit 0: static checks and gate readiness pass; 1: preflight integrity failure;
2: static checks pass but product gate remains blocked by named missing code.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys


def audit_sources(root: Path) -> dict:
    bridge = ast.parse((root / "arti_bridge.py").read_text(encoding="utf-8"))
    config = next(n.value for n in bridge.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in n.targets))
    literals = {k.value: v for k, v in zip(config.keys, config.values) if isinstance(k, ast.Constant)}
    defaults_off = all(isinstance(literals.get(k), ast.Constant) and literals[k].value is False
        for k in ("stardew_runtime_enabled", "stardew_provider_enabled", "stardew_spend_enabled"))
    example = json.loads((root / "docs/stardew/g1-motor-binding.config.example.json").read_text(encoding="utf-8"))
    example_off = all(example.get(k) is False for k in
        ("stardew_runtime_enabled", "stardew_provider_enabled", "stardew_spend_enabled"))
    decision = ast.parse((root / "arti_stardew_decision.py").read_text(encoding="utf-8"))
    catalog = next(n for n in decision.body if isinstance(n, ast.FunctionDef)
                   and n.name == "runtime_choice_admitted")
    water_named = any(isinstance(n, ast.Constant) and n.value == "water_once" for n in ast.walk(catalog))
    runtime = ast.parse((root / "arti_stardew_runtime.py").read_text(encoding="utf-8"))
    invoke = next(n for n in ast.walk(runtime) if isinstance(n, ast.FunctionDef)
                  and n.name == "_invoke_provider")
    provider_off_stub = any(isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
        and any(isinstance(a, ast.Constant) and a.value == "provider_off" for a in n.exc.args)
        for n in ast.walk(invoke))
    finish_wired = any(isinstance(n, ast.FunctionDef) and n.name == "_execute_finish"
                       for n in ast.walk(runtime))
    blockers = []
    if not water_named:
        blockers.append("water_once is absent from the runtime act catalog; leased native admission is parked in tracker #163 blocker 1")
    if provider_off_stub:
        blockers.append("production provider bridge is absent: _invoke_provider requires an injected provider; CONFIG flags do not construct one")
    # A name appearing in future source is never enough to authorize a live gate.
    blockers.append("owner-present watering gate requires separate exact-head review, provider/budget admission and live authorization; this package grants none")
    checks = {"shipped_defaults_off": defaults_off, "example_defaults_off": example_off,
              "finish_verifier_present_static_only": finish_wired}
    return {"checks": checks, "blockers": blockers, "ready_for_product_gate": False,
            "evidence_ceiling": "UNIT_TESTED / LOCAL_VERIFIED no-game static only"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-head", required=True, help="Exact reviewed candidate commit (40 hex characters)")
    args = parser.parse_args(argv)
    if len(args.expected_head) != 40 or any(c not in "0123456789abcdef" for c in args.expected_head):
        parser.error("--expected-head must be an exact lowercase 40-character SHA")
    root = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain=v1"], cwd=root, check=True,
                               capture_output=True, text=True).stdout.strip()
        report = audit_sources(root)
        report["head"] = head
        report["checks"].update(exact_head=head == args.expected_head, clean_tree=not dirty,
                                 project_venv=sys.prefix != sys.base_prefix)
    except (OSError, ValueError, SyntaxError, StopIteration, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "PREFLIGHT_FAILED", "error": type(exc).__name__}))
        return 1
    report["status"] = "BLOCKED" if all(report["checks"].values()) else "PREFLIGHT_FAILED"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
