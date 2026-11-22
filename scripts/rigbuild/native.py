"""The FULL native chain, step for step as `native.py` of pkg105-pkg112 and testbuild135-140 (ADR 245).

Ten Release builds with offline NuGet and one MSBuild worker (shared compilation, node reuse, deploy and ZIP off);
the hold-runtime harness (full and --fix68a); native calendar/shop facts against the game's Content; latch; fix71b
clock; rest facts; the host-gift filter; protocol-default and its twelve flagged suites; shipped-il, il-slot,
fix103a-slot and pure-full as TRX; the repository rules; the IL policy reconciliation (a rig-side script, here
`reconcile`). Any failing step stops the chain. The whole chain runs inside the pytest slot.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from scripts.rigbuild.evidence import sha

BUILD_FIRST = ("ArtiStardewBridge", "ArtiHostHelper", "ArtiStardewBridge.HoldRuntimeTests")
BUILD_THEN = ("ArtiStardewBridge.ProtocolTests", "ArtiStardewBridge.NativeFactTests", "ArtiCore.Pure.Tests",
              "Fix71B.Tests", "ArtiStardewBridge.LatchTests", "ArtiStardewBridge.RestFactsTests",
              "ArtiStardewBridge.S3Observer")
PROTOCOL_FLAGS = ("fix8", "pm1-golden", "fix32", "fix79a", "fix80a", "host-story", "fix71e", "fix87a", "reflex",
                  "fix89d", "fix92a", "ui-pad")
PURE_FILTERS = (("shipped-il", "FullyQualifiedName~ShippedFrameworkTests|FullyQualifiedName~IlScanTests"),
                ("il-slot", "FullyQualifiedName~IlScanTests|FullyQualifiedName~ExecutionSlot"),
                ("fix103a-slot", "FullyQualifiedName~Fix103aBodyGuardSlotTests"),
                ("pure-full", None))
FRAMEWORKS = {"ArtiStardewBridge": "net6.0", "ArtiStardewBridge.HoldRuntimeTests": "net6.0",
              "ArtiStardewBridge.ProtocolTests": "net6.0", "ArtiStardewBridge.NativeFactTests": "net8.0",
              "ArtiCore.Pure.Tests": "net8.0", "Fix71B.Tests": "net6.0", "ArtiStardewBridge.LatchTests": "net6.0",
              "ArtiStardewBridge.RestFactsTests": "net8.0", "ArtiStardewBridge.S3Observer": "net6.0"}
SUMMARY_STEPS = ("hold-full", "hold-fix68a", "native-facts", "latch-full", "fix71b-clock", "rest-facts",
                 "protocol-default") + tuple("protocol-" + f for f in PROTOCOL_FLAGS)


def dll(checkout, project, framework=None):
    framework = framework or FRAMEWORKS.get(project, "net6.0")
    return Path(checkout) / "stardew-mod" / project / "bin/Release" / framework / (project + ".dll")


def common(cfg):
    return ["-c", "Release", "-m:1", "-p:UseSharedCompilation=false", "-nodeReuse:false",
            "-p:RestoreSources=" + str(cfg.nuget_offline), "-p:NuGetAudit=false",
            "-p:GamePath=" + Path(cfg.game).as_posix(), "-p:EnableModDeploy=false", "-p:EnableModZip=false"]


def steps(cfg, checkout, evidence):
    """[(name, argv, env additions)] in the recorded order. Pure: no process is started here."""
    sdk, game, C, E = str(cfg.dotnet), Path(cfg.game), Path(checkout), Path(evidence)
    out = []

    def build(p):
        out.append(("build-" + p, [sdk, "build", "stardew-mod/" + p, *common(cfg)], {}))
    for p in BUILD_FIRST:
        build(p)
    # HoldRuntime is built and executed first; protocol golden runs this output as a child.
    hold = dll(C, "ArtiStardewBridge.HoldRuntimeTests")
    out.append(("hold-full", [sdk, hold], {}))
    out.append(("hold-fix68a", [sdk, hold, "--fix68a"], {}))
    for p in BUILD_THEN:
        build(p)
    out.append(("host-gift-targeted", [sdk, "test", "stardew-mod/ArtiCore.Pure.Tests", "-c", "Release", "--no-build",
                                       "--no-restore", "--filter",
                                       "FullyQualifiedName~HostGiftSafetyTests|FullyQualifiedName~HostStoryRuntimeTests"], {}))
    out.append(("native-facts", [sdk, dll(C, "ArtiStardewBridge.NativeFactTests"), game / "Content"], {}))
    out.append(("latch-full", [sdk, dll(C, "ArtiStardewBridge.LatchTests")], {}))
    out.append(("fix71b-clock", [sdk, dll(C, "Fix71B.Tests")], {}))
    out.append(("rest-facts", [sdk, dll(C, "ArtiStardewBridge.RestFactsTests")], {}))
    protocol = dll(C, "ArtiStardewBridge.ProtocolTests")
    out.append(("protocol-default", [sdk, protocol], {}))
    for f in PROTOCOL_FLAGS:
        out.append(("protocol-" + f, [sdk, protocol, "--" + f], {}))
    il_env = dict(ARTI_IL_INVENTORY_PATH=str(E / "il-sites.json"),
                  ARTI_SHIPPED_DLL_DIRS=str(dll(C, "ArtiStardewBridge").parent) + ";"
                  + str(dll(C, "ArtiHostHelper").parent))
    results = E / "test-results"
    for name, flt in PURE_FILTERS:
        args = [sdk, "test", "stardew-mod/ArtiCore.Pure.Tests", "-c", "Release", "--no-build", "--no-restore"]
        if flt:
            args += ["--filter", flt]
        out.append((name, args + ["--logger", "trx;LogFileName=" + name + ".trx", "--results-directory", results],
                    il_env))
    out.append(("repo-rules", [str(cfg.python312), "scripts/check_repo_rules.py"], {}))
    return out


def step_names():
    """The chain's step names in order (the same list `steps()` runs)."""
    return (["build-" + p for p in BUILD_FIRST] + ["hold-full", "hold-fix68a"] + ["build-" + p for p in BUILD_THEN]
            + ["host-gift-targeted", "native-facts", "latch-full", "fix71b-clock", "rest-facts", "protocol-default"]
            + ["protocol-" + f for f in PROTOCOL_FLAGS] + [n for n, _ in PURE_FILTERS] + ["repo-rules"])


def reconcile(checkout, report, output):
    """Read-only full IL reconciliation against the exact policy; no fuzzy keys (fix32's reconcile_policy.py)."""
    code = Path(checkout)
    sites = json.loads(Path(report).read_text(encoding="utf8"))
    reads = json.loads((code / "contracts/il_read_allowlist.json").read_text())
    inventory = json.loads((code / "contracts/il_d_rows.json").read_text())
    queries = {q["member"] for q in reads["queries"]}
    adapters = {a["key"] for a in reads["adapters"]}
    allowed = {s["key"]: s["count"] for r in inventory["rows"] for s in r["sites"]}
    counts = collections.Counter(s["Key"] for s in sites)
    unknown = {s["Key"]: counts[s["Key"]] for s in sites
               if s["Kind"] == "backend_outside_slot" or not (
                   s["Kind"] == "game_call" and s["Member"] in queries
                   or s["Key"] in allowed or s["Key"] in adapters)}
    mismatches = {k: {"expected": n, "actual": counts[k]}
                  for k, n in {**allowed, **dict.fromkeys(adapters, 1)}.items() if counts[k] != n}
    result = dict(total_instructions=len(sites), unique_sites=len(counts), d_row_keys=len(allowed),
                  adapter_keys=len(adapters), unknown=unknown, count_mismatches=mismatches)
    Path(output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")
    return result


def trx(path):
    counts = ET.parse(path).getroot().find("{*}ResultSummary/{*}Counters").attrib
    if not (int(counts["failed"]) == 0 and int(counts["passed"]) == int(counts["total"]) > 0):
        raise RuntimeError(f"{path}: {counts}")
    return dict(total=int(counts["total"]), failed=0, exit=0)


def tail(path):
    lines = Path(path).read_text(encoding="utf8", errors="replace").strip().splitlines()
    return lines[-1] if lines else ""


def run_chain(ev, cfg):
    """Every step through ev.require (admission, create-new log, runs.jsonl); then reconcile, hashes, the record."""
    C, E = ev.C, ev.E
    absent = [p for p in BUILD_FIRST + BUILD_THEN if not (Path(C) / "stardew-mod" / p).is_dir()]
    if absent:
        raise RuntimeError("this checkout lacks the C# projects " + ", ".join(absent)
                           + " (the author's test projects; not in the public repository)")
    (E / "test-results").mkdir(exist_ok=True)
    for name, args, env in steps(cfg, C, E):
        ev.env.update(env)
        ev.require(name, args)
    policy = reconcile(C, E / "il-sites.json", E / "policy-reconciliation.json")
    if policy["unknown"] or policy["count_mismatches"]:
        raise RuntimeError("IL policy reconciliation: " + json.dumps(policy))
    hashes = {p: sha(dll(C, p)) for p in FRAMEWORKS}
    hashes["ArtiHostHelper"] = sha(dll(C, "ArtiHostHelper"))
    ev.write("build-hashes.json", dict(source_sha=ev.head, dlls=hashes))
    record = dict(source_sha=ev.head, passed=True, dlls=hashes, policy=policy,
                  summaries={n: tail(E / (n + ".log")) for n in SUMMARY_STEPS},
                  trx={n: trx(E / "test-results" / (n + ".trx")) for n, _ in PURE_FILTERS})
    ev.write("native-complete.json", record)
    return record
