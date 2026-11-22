"""Context binding for the EXISTING rig-owned attempt session verifier.

The receipt's session.py is a script with fixed attempt globals, not an importable
library. Execute its imports/functions and its exact ``phase == 'verify'`` body
with booking globals. Never execute the start/status arms, copy the procedure,
rewrite it, or overwrite the historical attempt's evidence. Pin the whole source.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys

from arti_runtime.witness import WitnessFailure
from scripts.free_play import witness as W


def bind(procedure, *, output, rig_config, owned_saves=()):
    if sys.flags.optimize:
        raise WitnessFailure("session_verify_requires_assertions")
    procedure, output = Path(procedure), Path(output).resolve()
    tree = ast.parse(procedure.read_text(encoding="utf-8-sig"), filename=str(procedure))
    definitions = [node for node in tree.body if isinstance(node,
                   (ast.Import, ast.ImportFrom, ast.FunctionDef))]
    branches = [node for node in tree.body if isinstance(node, ast.If)]
    verify = None
    for branch in branches:
        while branch:
            test = branch.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "phase" and len(test.ops) == 1
                    and isinstance(test.ops[0], ast.Eq) and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value == "verify"):
                verify = branch.body
            branch = branch.orelse[0] if len(branch.orelse) == 1 and isinstance(branch.orelse[0], ast.If) else None
    if verify is None:
        raise WitnessFailure("session_verify_procedure_unsupported")
    ns = {"__name__": "arti_witness_existing_session", "__file__": str(procedure)}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(procedure), "exec"), ns)
    if not all(callable(ns.get(name)) for name in ("baseline", "guard", "witness")):
        raise WitnessFailure("session_verify_procedure_unsupported")
    ns.update(RIG=Path(rig_config["root"]), E=output, CODE=W.ROOT,
              GAME=Path(rig_config["game"]), APP=Path(rig_config["appData"]),
              read=W.read, HEAD="witness", phase="verify")
    # ADR 173: historic session verifiers call manifest(APP/'Saves'). Bind
    # that census to this booking's install before either capture or verify.
    # Leave their independent checks of mods/templates/other files intact.
    original_manifest = ns.get('manifest')
    if callable(original_manifest):
        from scripts.free_play import storage
        saves = Path(rig_config['appData']) / 'Saves'
        def scoped_manifest(path):
            if Path(path).resolve() == saves.resolve():
                return {p.relative_to(saves).as_posix(): W.digest(p)
                        for p in storage.save_members(saves, owned_saves)[0]}
            return original_manifest(path)
        ns['manifest'] = scoped_manifest

    def discover(marker):
        # Replace only historic run discovery, with this exact owned case. The
        # verification body, protected-state manifest and guard remain unchanged.
        if marker.get("witness_run") != str(output):
            raise WitnessFailure("session_verify_context_changed")
        return output

    ns["discover"] = discover
    return ns, compile(ast.Module(body=verify, type_ignores=[]), str(procedure), "exec")


def capture(procedure, *, output, rig_config, owned_saves=()):
    output = Path(output)
    ns, _ = bind(procedure, output=output, rig_config=rig_config, owned_saves=owned_saves)
    ns["guard"]("owner-before.log")
    W.write(output / "before-independent.json", ns["baseline"]())
    W.write(output / "mod-exit-before.json", dict(sha256=ns["witness"]()))
    W.write(output / "one-session.json", dict(witness_run=str(output.resolve())))


def verify(procedure, *, output, rig_config, owned_saves=()):
    ns, body = bind(procedure, output=output, rig_config=rig_config, owned_saves=owned_saves)
    exec(body, ns)  # the existing session.py verify body, not a parallel audit
    receipt = Path(output) / "restore-independent.json"
    if W.read(receipt).get("passed") is not True:
        raise WitnessFailure("independent_session_restore_unverified")
    return receipt
