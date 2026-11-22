"""`test-build`: testbuild135-140 as one command (ADR 245; briefs TESTBUILD135/136, RECEIPT_TESTBUILD140).

1. checkout at the source (verified, or `--detach`), clean, `package_guard` exit 0; tracked-file list and identity;
2. the FULL native chain inside the pytest slot (`native.run_chain`);
3. stage `<out>\\bridge`, `<out>\\helper`, `<out>\\provenance.json` (kind test-build, not_a_package), preflight twice;
4. the booking: `--like` (an earlier test-build booking) with its five artifact fields pointed at `<out>`;
5. a receipt whose first line is BUILT or NOT BUILT.
No Python delta suite, golden or page measurement runs on a test build; it is for checks in the test world only.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from scripts.rigbuild import evidence as ev_mod, machine, native, stage
from scripts.rigbuild.machine import Refused

TEST_BUILD_SENTENCE = ("A test build is for checks in the test world only: no Python delta suite, golden or page "
                       "measurement was run on it; it is not a package and must not be used for a livestream.")


def mod_default(checkout, key="UiPadMenusEnabled"):
    path = Path(checkout) / "stardew-mod/ArtiStardewBridge/ModConfig.cs"
    text = path.read_text(encoding="utf8")
    match = re.search(r"public bool " + key + r" \{ get; set; \} = (true|false);", text)
    if not match:
        return None
    return dict(key=key, default=match.group(1), file="stardew-mod/ArtiStardewBridge/ModConfig.cs",
                line=text[:match.start()].count("\n") + 1)


def run(cfg, *, source, out, python_gates, like=None, booking=None, evidence=None, receipt=None, detach=False,
        fetch=None, previous=None, slot_minutes=90, deps=None):
    """Returns 0 when BUILT; a refusal or a failing step raises after BLOCKED.md and the NOT BUILT receipt are
    written. `deps` injects fakes (tests): popen, read_machine, sleep, mono, git_fn, install_guard, say, free_gib."""
    deps = dict(deps or {})
    out = Path(out)
    like = Path(like or cfg.booking_like or "")
    if not like.name:
        raise Refused("no earlier booking to copy rig fields from: pass --like or set booking_like in the config")
    booking = Path(booking) if booking else cfg.rig_root / f"free-play-{out.name}-booking.json"
    E = Path(evidence) if evidence else cfg.evidence_root / out.name
    date = (deps.get("now") or machine.clock)()["local"][:10]
    receipt = Path(receipt) if receipt else (cfg.receipts or E) / f"RECEIPT_TESTBUILD_{out.name}_{date}.md"
    for path in (out, booking, receipt):
        if path.exists():
            raise Refused("output exists (never overwritten): " + str(path))
    if E.exists() and any(E.iterdir()):
        raise Refused("evidence folder exists and is not empty: " + str(E))
    E.mkdir(parents=True, exist_ok=True)
    task = "tb-" + out.name
    env = ev_mod.build_env(cfg, cfg.checkout, E, task, base_temp=E / "no-pytest")
    ev = ev_mod.Evidence(E, cfg=cfg, checkout=cfg.checkout, head=source, env=env, policy=machine.TEST_BUILD, task=task,
                         **{k: deps[k] for k in ("popen", "read_machine", "sleep", "mono", "git_fn", "say", "now")
                            if k in deps})
    ev.slot.timeout_s = slot_minutes * 60
    free = deps.get("free_gib", ev_mod.free_gib)
    ev.write("start-disk.json", dict(c_free_gib=round(free(cfg), 2), utc=ev.now()["utc"]))
    built, note = False, None
    try:
        identity = ev_mod.prepare(ev, source=source, detach=detach, fetch=fetch, previous_native=previous)
        deps.get("install_guard", ev_mod.install_guard)(env)
        with ev.slot.held("native-chain", 4):
            record = native.run_chain(ev, cfg)
        ev.gate("stage-admission")
        candidate = E / "candidate"
        dep_hashes = stage.assemble(cfg.checkout, candidate, record["dlls"][stage.BRIDGE], record["dlls"][stage.HELPER])
        prov = stage.provenance(identity, record, dep_hashes, checkout=cfg.checkout, kind="test-build",
                                python_gates=python_gates, native_chain=E / "native-complete.json",
                                previous_package=previous, previous_native=previous)
        stage.preflight(candidate, prov)
        stage.write_new(candidate / "provenance.json", prov)
        ev.write("provenance.json", prov)
        ev.gate("final-copy")
        hashes = stage.copy_staged(candidate, out, prov)
        row = stage.compose_booking(out, like)
        booking_sha = stage.write_booking(booking, row, like)
        note = mod_default(cfg.checkout)
        ev.write("stage-complete.json", dict(built=True, kind="test-build", source_sha=ev.head, preflight_passed=True,
                                             staged_mod_preflight="candidate and final", candidate_dependencies=len(dep_hashes),
                                             folder=str(out), folder_hashes=hashes, booking={str(booking): booking_sha},
                                             booking_like=str(like), tested_hashes_match_staged=True,
                                             checkout_clean=not ev.git("status", "--porcelain")))
        built = True
    except BaseException as exc:
        ev.block(str(exc), outcome="NOT BUILT")
        raise
    finally:
        ev.write("end-disk.json", dict(c_free_gib=round(free(cfg), 2), utc=ev.now()["utc"]))
        write_receipt(ev, receipt, built=built, out=out, booking=booking, like=like, note=note)
    return 0


def _runs(ev):
    path = ev.path("runs.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()] if path.exists() else []


def native_lines(ev, record, names=None):
    lines = []
    for row in _runs(ev):
        name = row["name"]
        if names is not None and name not in names:
            continue
        summary = (record or {}).get("summaries", {}).get(name)
        if name in (record or {}).get("trx", {}):
            summary = f"{record['trx'][name]['total']} passed, 0 failed"
        if name == "host-gift-targeted" and row["exit"] == 0:
            log = ev.path(name + ".log")
            summary = next((l.strip() for l in log.read_text(encoding="utf8", errors="replace").splitlines()
                            if "Passed!" in l), None) if log.exists() else None
        lines.append(f"- {name} exited {row['exit']}." + (f" {summary}" if summary else ""))
        if row["exit"]:
            log = ev.path(name + ".log")
            tail = log.read_text(encoding="utf8", errors="replace").splitlines()[-10:] if log.exists() else []
            lines.extend(["", "```text", *tail, "```", ""])
    if record:
        p = record["policy"]
        lines.append(f"- Architecture guards (in protocol-default) and IL policy reconciliation passed: "
                     f"{p['total_instructions']:,} instructions, {p['unique_sites']:,} unique sites, "
                     f"zero unknown sites and count mismatches.")
    return lines


def write_receipt(ev, path, *, built, out, booking, like, note):
    record = ev.load("native-complete.json") if ev.exists("native-complete.json") else None
    identity = ev.load("source-identity.json") if ev.exists("source-identity.json") else {}
    start = ev.load("start-disk.json") if ev.exists("start-disk.json") else {}
    end = ev.load("end-disk.json") if ev.exists("end-disk.json") else {}
    lines = ["BUILT" if built else "NOT BUILT", f"# test build {out.name}", f"Completed {ev.now()['local']}.",
             TEST_BUILD_SENTENCE, "",
             f"Source {ev.head} in {ev.C}." + (
                 f" Verified {'detached and ' if identity.get('detached') else ''}clean before the run and before "
                 f"every step. package_guard: {identity.get('package_guard')}" if identity else ""),
             f"Procedure: scripts/rigbuild at {identity.get('tool_sha')}.",
             f"C: free at start {start.get('c_free_gib')} GiB and at end {end.get('c_free_gib')} GiB.", ""]
    hashes = (record or {}).get("dlls", {})
    lines += [f"Bridge SHA256 {hashes.get(stage.BRIDGE, 'no completed native chain')}.",
              f"Helper SHA256 {hashes.get(stage.HELPER, 'no completed native chain')}.", "", "## Native chain", "",
              "All ten Release builds use offline NuGet and one MSBuild worker, with shared compilation, node reuse, "
              "deployment and ZIP disabled. The chain runs inside the pytest slot.", ""]
    lines += native_lines(ev, record, names=native.step_names())
    if built:
        lines += ["", "## Staging", "", f"Test-build folder {out}.",
                  f"Booking {booking} was created from {like} by updating the five artifact fields.",
                  "Both staged_mod_preflight and candidate_dependencies passed for the candidate and the final folder. "
                  "The staged binary and dependency hashes match the recorded hashes."]
        if note:
            lines.append(f"{note['key']} defaults to {note['default']} in {note['file']} at line {note['line']}. "
                         "The operator sets it true.")
    elif ev.exists("BLOCKED.md"):
        lines += ["", ev.path("BLOCKED.md").read_text(encoding="utf8").strip()]
    lines += ["", f"Evidence {ev.E}."]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf8") as f:
        f.write("\n".join(lines) + "\n")
    ev.say(str(path))
    return path
