"""`package`: the live package as pkg112 was made, as one command (ADR 245; PKG112_BRIEF, RECEIPT_PKG112).

Steps, in order, each with its evidence in `<rig>\\evidence\\pkg<N>\\`:
 1. checkout detached at the source, clean, `package_guard` exit 0; `ct-tracked-files.json`, `source-identity.json`;
 2. the FULL native chain inside the pytest slot (`native.run_chain`): `native-complete.json`, `build-hashes.json`;
 3. `freeze-native-probes`: the HoldRuntime, Protocol and RestFacts outputs copied hash-pinned to `built-probes\\`
    (`native-probe-cache.json`), so pytest reuses them; `native-artifacts-before-python.json`;
 4. `python-golden`: `tests/test_pm1_golden.py`, serial (`-n 0`), Python 3.12, in the slot; 12 passed by default;
 5. `page-trim-destinations`: `tests/test_live7_trim_destinations.py` unmodified, serial, in the slot;
 6. `integrated-page-weight` (`scripts.free_play.integrated_weight`) and `combined-page-weight` (`measure_page.py`)
    with DEFAULT settings and the mind folder in a temporary folder outside the checkout; choices lost 0, pages at
    most 12,000 B, encoded requests at most 16,128 B; the checkout's `arti-state\\mind` must not exist afterwards;
 7. `cleanup`: the run's own temporary folder is removed (`cleanup-result.json`);
 8. `new-settings-defaults.json`: each default read from the packaged source (`settings_defaults.json`);
 9. staging: `<rig>\\pm1-fix<N>-package\\` with `bridge\\`, `helper\\`, `provenance.json`, `package-preflight.json`;
    bookings `<rig>\\free-play<N>-booking.json` and `<rig>\\mabar<N>-booking.json` from the N-1 bookings;
10. the receipt `RECEIPT_PKG<N>_<date>.md`, first line READY or NOT READY (always written).
The Python delta suite is NOT run (owner decision 2026-11-18: the coordinator's gates are the Python proof).
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET

from scripts.rigbuild import evidence as ev_mod, machine, native, stage
from scripts.rigbuild.evidence import sha, tree_hashes
from scripts.rigbuild.machine import Refused

HERE = Path(__file__).resolve().parent
PROBES = (("ArtiStardewBridge.HoldRuntimeTests", "net6.0"), ("ArtiStardewBridge.ProtocolTests", "net6.0"),
          ("ArtiStardewBridge.RestFactsTests", "net8.0"))
GOLDEN, TRIM = "tests/test_pm1_golden.py", "tests/test_live7_trim_destinations.py"
BASELINE = "scripts/free_play/fixtures/integrated-page-weight-baseline.json"
PAGE_LIMIT, ENCODED_LIMIT = 12000, 16128
AFTER_NATIVE = ("freeze-native-probes", "python-golden", "page-trim-destinations", "integrated-page-weight",
                "combined-page-weight", "cleanup")
READY_SENTENCE = ("UNIT_TESTED / OFFLINE_REPLAY_ONLY. READY means built, natively tested and preflighted with "
                  "CT's gates as the Python proof, not proven in the game.")


class Paths:
    def __init__(self, cfg, number, *, like_free_play=None, like_mabar=None, evidence=None, receipt=None, date=None):
        r, n = cfg.rig_root, int(number)
        self.number = n
        self.E = Path(evidence) if evidence else cfg.evidence_root / f"pkg{n}"
        self.target = r / f"pm1-fix{n}-package"
        self.bookings = [r / f"free-play{n}-booking.json", r / f"mabar{n}-booking.json"]
        self.likes = [Path(like_free_play) if like_free_play else r / f"free-play{n - 1}-booking.json",
                      Path(like_mabar) if like_mabar else r / f"mabar{n - 1}-booking.json"]
        self.previous = r / f"pm1-fix{n - 1}-package"
        self.receipt = Path(receipt) if receipt else (cfg.receipts or self.E) / f"RECEIPT_PKG{n}_{date}.md"


def previous_sources(paths):
    """(previous package source, previous native build source) from the N-1 package's provenance, if present."""
    prov = paths.previous / "provenance.json"
    if not prov.is_file():
        return None, None
    row = stage.read(prov)
    return row.get("source_sha"), row.get("native_build_source_sha")


def freeze_probes(ev, record):
    """cache_probes.py: the freshly built probes copied hash-pinned for the Python fixtures."""
    roots = {}
    for project, framework in PROBES:
        source = native.dll(ev.C, project, framework).parent
        if sha(source / (project + ".dll")) != record["dlls"][project]:
            raise Refused(project + " changed since the native chain")
        target = ev.E / "built-probes" / project
        shutil.copytree(source, target)
        if tree_hashes(source) != tree_hashes(target):
            raise Refused(project + " probe copy differs")
        roots[project] = dict(path=str(target), dll_sha256=record["dlls"][project], files=tree_hashes(target))
    ev.write("native-probe-cache.json", dict(source_sha=ev.head, source="fresh native builds and audit", passed=True,
                                             projects=roots, no_test_source_changes=True))
    artifacts = {}
    for project, names in ((stage.BRIDGE, [stage.BRIDGE + ".dll", *stage.DEPENDENCIES]),
                           (stage.HELPER, [stage.HELPER + ".dll"])):
        output = native.dll(ev.C, project).parent
        for name in names:
            artifacts[str(output / name)] = sha(output / name)
        manifest = ev.C / "stardew-mod" / project / "manifest.json"
        artifacts[str(manifest)] = sha(manifest)
    ev.write("native-artifacts-before-python.json", dict(source_sha=ev.head, hashes=artifacts))
    ev.record("freeze-native-probes", 0)


def _bad(r):
    return r.find("failure") is not None or r.find("error") is not None


def _node(checkout, r):
    parts = r.attrib["classname"].split(".")
    n = next((n for n in range(len(parts), 0, -1) if (Path(checkout) / ("/".join(parts[:n]) + ".py")).is_file()),
             len(parts))
    return "/".join(parts[:n]) + ".py::" + "::".join(parts[n:] + [r.attrib["name"]])


def pytest_step(ev, name, paths, interpreter, n, root, *, version_fn=None):
    version = (version_fn or (lambda exe: subprocess.check_output([str(exe), "--version"], text=True).strip()))(interpreter)
    temp = Path(root) / (name + "-admitted")
    if temp.exists():
        raise Refused("pytest base temp exists: " + str(temp))
    ev.env["ARTI_RIGBUILD_BASETEMP"] = str(temp)
    xml = ev.E / (name + ".xml")
    with ev.slot.held(name, n):
        rc = ev.run(name, [interpreter, "-B", "-m", "pytest", "-p", "no:cacheprovider", "--timeout", "300", "-n",
                           str(n), *paths, "-q", "--tb=short", "-o", "tmp_path_retention_policy=failed",
                           "--basetemp=" + str(temp), "--junitxml=" + str(xml)], True)
    rows = list(ET.parse(xml).iter("testcase")) if xml.exists() else []
    skipped = lambda r: r.find("skipped") is not None
    result = dict(source_sha=ev.head, interpreter=str(interpreter), python_version=version, exit=rc, selection=paths,
                  total=len(rows), passed=sum(not _bad(r) and not skipped(r) for r in rows),
                  failed=sum(_bad(r) for r in rows),
                  skipped=sum(skipped(r) and r.find("skipped").get("type") != "pytest.xfail" for r in rows),
                  xfailed=sum(skipped(r) and r.find("skipped").get("type") == "pytest.xfail" for r in rows),
                  failures=[dict(node=_node(ev.C, r), detail=(r.find("failure") if r.find("failure") is not None
                                                              else r.find("error")).text) for r in rows if _bad(r)])
    ev.write(name + "-result.json", result)
    return result


def _reparse(path):
    st = os.lstat(path)
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)) \
        or stat.S_ISLNK(st.st_mode)


def cleanup(ev, root, expected):
    """cleanup.ps1 in Python: remove only the run's own temp root; links inside are unlinked, never followed."""
    target, expected = Path(os.path.abspath(root)), Path(os.path.abspath(expected))
    if target != expected:
        raise Refused("temp root outside the exact task directory: " + str(target))
    if target.exists():
        if _reparse(target):
            raise Refused("temp root is a reparse point")
        for folder, dirs, files in os.walk(target):
            for name in dirs + files:
                entry = Path(folder) / name
                if not str(entry).lower().startswith(str(target).lower() + os.sep):
                    raise Refused("entry outside the owned root")
                if _reparse(entry):
                    (os.rmdir if entry.is_dir() else os.unlink)(entry)
            dirs[:] = [d for d in dirs if (Path(folder) / d).exists()]
        shutil.rmtree(target)
    if target.exists():
        raise Refused("owned base temp remains: " + str(target))
    ev.write("cleanup-result.json", dict(passed=True, task=ev.task, root=str(target), removed=True, paths_verified=True,
                                         utc=ev.now()["utc"]))
    ev.record("cleanup", 0)


def post_native(ev, cfg, record, *, golden_count, temp_root, version_fn=None):
    freeze_probes(ev, record)
    root = Path(temp_root)
    if root.exists():
        raise Refused("temp root exists: " + str(root))
    root.mkdir(parents=True)
    ev.write("owned-basetemp.json", dict(task=ev.task, root=str(root), created_new=True))
    saved = {k: ev.env.get(k) for k in ("TEMP", "TMP")}
    ev.env.update(TEMP=str(root), TMP=str(root))
    mind_in_checkout = ev.C / "arti-state" / "mind"
    try:
        ev.env["ARTI_MIND_DIR"] = str(root / "python-golden-admitted" / "mind")
        gold = pytest_step(ev, "python-golden", [GOLDEN], cfg.python312, 0, root, version_fn=version_fn)
        if not (gold["exit"] == gold["failed"] == 0 and gold["passed"] == golden_count):
            raise RuntimeError(f"golden: {gold['passed']} passed, {gold['failed']} failed, exit {gold['exit']} "
                               f"(expected {golden_count} passed)")
        if not gold["python_version"].startswith("Python 3.12"):
            raise Refused("golden interpreter is not Python 3.12: " + gold["python_version"])
        trim_hash = sha(ev.C / TRIM)
        ev.env["ARTI_MIND_DIR"] = str(root / "page-trim-destinations-admitted" / "mind")
        trim = pytest_step(ev, "page-trim-destinations", [TRIM], cfg.python312, 0, root, version_fn=version_fn)
        if not (trim["exit"] == trim["failed"] == 0 and trim["passed"] > 0) or sha(ev.C / TRIM) != trim_hash:
            raise RuntimeError("page-trim-destinations failed or the test file changed")
        ev.env["ARTI_RIGBUILD_BASETEMP"] = str(root)
        mind = root / "page-mind"
        ev.env["ARTI_MIND_DIR"] = str(mind)
        baseline_hash = sha(ev.C / BASELINE)
        shutil.copy2(ev.C / BASELINE, ev.E / "measurement-baseline.json")
        ev.require("integrated-page-weight", [cfg.python312, "-B", "-m", "scripts.free_play.integrated_weight",
                                              "--output", ev.E, "--baseline", ev.E / "measurement-baseline.json"])
        ev.require("combined-page-weight", [cfg.python312, "-B", HERE / "measure_page.py", ev.E / "page-weights.json"])
        if mind_in_checkout.exists() or sha(ev.C / BASELINE) != baseline_hash:
            raise Refused("the page measurement wrote a mind folder into the checkout or changed the baseline")
        integrated, combined = ev.load("integrated-page-weight.json"), ev.load("page-weights.json")["summary"]
        if not (integrated["choices_lost"] == combined["choices_lost"] == 0
                and integrated["after"]["weight"]["bytes"]["max"] <= PAGE_LIMIT
                and combined["max_json_bytes"] <= PAGE_LIMIT and combined["max_encoded_bytes"] <= ENCODED_LIMIT):
            raise RuntimeError("page weights over the limit or choices lost: " + json.dumps(combined))
        ev.write("page-measurements-complete.json", dict(source_sha=ev.head, passed=True, mind_folder=str(mind),
                                                         checkout_mind_absent=True, trim_test_sha256=trim_hash,
                                                         baseline_sha256=baseline_hash))
    finally:
        for k, v in saved.items():
            if v is None:
                ev.env.pop(k, None)
            else:
                ev.env[k] = v
        cleanup(ev, root, temp_root)


def settings_defaults(checkout, table=None):
    """settings.py: each default read from the exact packaged source; never alters settings."""
    table = table or json.loads((HERE / "settings_defaults.json").read_text(encoding="utf8"))
    rows = []
    for item in table["settings"]:
        key, path, value, needle = item["setting"], item["source"], item["default"], item["needle"]
        text = (Path(checkout) / path).read_text(encoding="utf8")
        if needle not in text:
            raise Refused(f"{key}: '{needle}' not found in {path}")
        rule = table["no_explicit_default_in"]
        if key in rule["settings"]:
            for q in rule["files"] + ([path] if key in rule["own_file_too"] else []):
                tree = ast.parse((Path(checkout) / q).read_text(encoding="utf8"))
                if any(isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == key for k in n.keys)
                       for n in ast.walk(tree)):
                    raise Refused(f"{key}: explicit default found in {q}; inspect it")
        semantics = ("missing flag is off" if value is False else "on by default" if value is True
                     else "key absent from every tracked config; rig-only instrument off" if value == "absent"
                     else item.get("semantics", ""))
        rows.append(dict(setting=key, default=value, source=path, line=text[:text.index(needle)].count("\n") + 1,
                         semantics=semantics))
    return rows


def _privacy(ev):
    refusals = ev.path("ct-privacy-refused.jsonl")
    if refusals.exists() and refusals.stat().st_size:
        raise Refused("Privacy guard refused access. See " + str(refusals))


def stage_package(ev, cfg, paths, *, python_gates, golden_count, previous_package):
    ev.gate("stage-admission")
    if any(p.exists() for p in (paths.target, *paths.bookings, ev.path("BLOCKED.md"))):
        raise Refused("package, booking or BLOCKED.md exists")
    _privacy(ev)
    identity, record = ev.load("source-identity.json"), ev.load("native-complete.json")
    gold, trim = ev.load("python-golden-result.json"), ev.load("page-trim-destinations-result.json")
    if not (identity["source_sha"] == record["source_sha"] == gold["source_sha"] == trim["source_sha"] == ev.head):
        raise Refused("evidence from different sources")
    if not (identity["detached"] and identity["checkout_clean"] and record["passed"] and gold["passed"] == golden_count
            and trim["passed"] > 0 and gold["exit"] == trim["exit"] == 0):
        raise Refused("a gate before staging did not pass")
    if not (ev.load("page-measurements-complete.json")["passed"] and ev.load("cleanup-result.json")["passed"]):
        raise Refused("page measurement or cleanup did not pass")
    if (ev.C / "arti-state" / "mind").exists():
        raise Refused("a mind folder exists in the checkout")
    if not all(sha(p) == h for p, h in ev.load("native-artifacts-before-python.json")["hashes"].items()):
        raise Refused("native artifacts changed during the Python steps")
    candidate = ev.path("candidate")
    deps = stage.assemble(ev.C, candidate, record["dlls"][stage.BRIDGE], record["dlls"][stage.HELPER])
    prov = stage.provenance(identity, record, deps, checkout=ev.C, kind="live-package", python_gates=python_gates,
                            native_chain=ev.path("native-complete.json"), previous_package=previous_package,
                            previous_native=identity["previous_native_build_source_sha"])
    prior = {p: sha(p) for p in paths.likes}
    stage.write_new(candidate / "provenance.json", prov)
    stage.preflight(candidate, prov)
    _privacy(ev)
    ev.gate("final-copy")
    hashes = stage.copy_staged(candidate, paths.target, prov)
    _privacy(ev)
    rows = [stage.compose_booking(paths.target, like) for like in paths.likes]
    before = paths.previous / "package-preflight.json"
    before = stage.read(before) if before.is_file() else {}
    pre = dict(passed=True, status="UNIT_TESTED", offline_replay_status="OFFLINE_REPLAY_ONLY", source_sha=ev.head,
               source_tree=identity["source_tree"], native_build_source_sha=ev.head,
               helper_native_build_source_sha=ev.head, package=str(paths.target), dll_sha256=prov["dll_sha256"],
               helper_sha256=prov["helper_sha256"], native=record,
               previous_package=str(paths.previous) if before else None,
               dll_changed_vs_previous=prov["dll_sha256"] != before.get("dll_sha256") if before else None,
               helper_bytes_equal_previous=prov["helper_sha256"] == before.get("helper_sha256") if before else None,
               python=dict(golden=gold, page_trim_destinations=trim), python_gates=python_gates,
               full_python_suite_run=False, dependencies=len(deps), dependency_hashes=deps,
               manifest_sha256={p: sha(paths.target / p / "manifest.json") for p in ("bridge", "helper")},
               assembly_and_manifest_preflight_passed=True, staged_binaries_match_audited_build=True,
               integrated_page_weight=ev.load("integrated-page-weight.json"),
               combined_page_weight=ev.load("page-weights.json"), staged_defaults=ev.load("new-settings-defaults.json"),
               operator_warning=ev.path("operator-warning.txt").read_text(encoding="utf8"), test_repairs=[],
               persistent_failures=[], checkout_clean=not ev.git("status", "--porcelain"), game_session=False,
               deployment_enabled=False, zip_enabled=False,
               save_safety=dict(live_saves_accessed=False, save_barrier_refusals=0), privacy=dict(guard_refusals=0),
               machine_policy=dict(commit_at_most_pct=ev.policy.commit_ceiling_pct,
                                   c_free_min_gib=ev.policy.disk_floor_gib, no_game=True, no_obs=True,
                                   foreign_pytest_recheck_seconds=ev.policy.pytest_wait_s),
               evidence=str(ev.E))
    for folder in (candidate, paths.target, ev.E):
        stage.write_new(Path(folder) / "package-preflight.json", pre)
    ev.write("provenance.json", prov)
    if tree_hashes(candidate) != tree_hashes(paths.target):
        raise Refused("candidate and package differ after the preflight record")
    books = {}
    for path, row, like in zip(paths.bookings, rows, paths.likes):
        books[str(path)] = stage.write_booking(path, row, like)
    if not all(sha(p) == h for p, h in prior.items()):
        raise Refused("an earlier booking changed")
    _privacy(ev)
    ev.write("stage-complete.json", dict(ready=True, source_sha=ev.head, package=str(paths.target),
                                         package_hashes=tree_hashes(paths.target), bookings=books))


def run(cfg, *, source, number, python_gates, warning_file, like_free_play=None, like_mabar=None, evidence=None,
        receipt=None, detach=False, fetch=None, golden_count=12, slot_minutes=90, deps=None):
    deps = dict(deps or {})
    absent = [p for p in (GOLDEN, TRIM, BASELINE, "scripts/rigbuild/measure_page.py", "scripts/rigbuild/settings_defaults.json")
              if not (HERE.parents[1] / p).exists()]
    if absent:
        raise Refused("this checkout lacks the package inputs " + ", ".join(absent)
                      + " (the author's tests, page-weight corpus and tables; not in the public repository)")
    date = (deps.get("now") or machine.clock)()["local"][:10]
    paths = Paths(cfg, number, like_free_play=like_free_play, like_mabar=like_mabar, evidence=evidence,
                  receipt=receipt, date=date)
    for p in (paths.target, *paths.bookings, paths.receipt):
        if p.exists():
            raise Refused("output exists (never overwritten): " + str(p))
    for p in paths.likes:
        if not p.is_file():
            raise Refused("earlier booking missing: " + str(p) + " (pass --like-free-play / --like-mabar)")
    warning = Path(warning_file).read_text(encoding="utf8").strip()
    if not warning:
        raise Refused("the operator warning is empty")
    E = paths.E
    if E.exists() and any(E.iterdir()):
        raise Refused("evidence folder exists and is not empty: " + str(E))
    E.mkdir(parents=True, exist_ok=True)
    task = f"pkg{paths.number}"
    temp_root = Path(deps.get("temp", os.environ.get("TEMP", str(E)))) / f"rigbuild-{task}-pytest"
    env = ev_mod.build_env(cfg, cfg.checkout, E, task, base_temp=temp_root)
    ev = ev_mod.Evidence(E, cfg=cfg, checkout=cfg.checkout, head=source, env=env, policy=machine.PACKAGE, task=task,
                         **{k: deps[k] for k in ("popen", "read_machine", "sleep", "mono", "git_fn", "say", "now")
                            if k in deps})
    ev.slot.timeout_s = slot_minutes * 60
    (E / "operator-warning.txt").write_text(warning + "\n", encoding="utf8")
    previous_package, previous_native = previous_sources(paths)
    ready = False
    try:
        ev_mod.prepare(ev, source=source, detach=detach, fetch=fetch, previous_native=previous_native,
                       require_detached=True)
        deps.get("install_guard", ev_mod.install_guard)(env)
        with ev.slot.held("native-chain", 4):
            record = native.run_chain(ev, cfg)
        post_native(ev, cfg, record, golden_count=golden_count, temp_root=temp_root, version_fn=deps.get("version_fn"))
        ev.write("new-settings-defaults.json", deps.get("settings", settings_defaults)(ev.C))
        stage_package(ev, cfg, paths, python_gates=python_gates, golden_count=golden_count,
                      previous_package=previous_package)
        ready = True
    except BaseException as exc:
        ev.block(str(exc))
        raise
    finally:
        write_receipt(ev, paths, ready=ready, python_gates=python_gates)
    return 0


def write_receipt(ev, paths, *, ready, python_gates):
    """receipt.py: what passed, the settings table, the operator warning; READY only when staging completed."""
    from scripts.rigbuild.testbuild import native_lines
    load = lambda n: ev.load(n + ".json") if ev.exists(n + ".json") else {}
    record, staged, identity = load("native-complete"), load("stage-complete"), load("source-identity")
    gold, trim = load("python-golden-result"), load("page-trim-destinations-result")
    integrated, combined = load("integrated-page-weight"), load("page-weights").get("summary", {})
    ready = ready and staged.get("ready", False) and not ev.exists("BLOCKED.md")
    lines = ["READY" if ready else "NOT READY", "", f"# pkg{paths.number}", "", f"Completed: {ev.now()['local']}",
             f"Source: {ev.head} (checkout {ev.C}; detached {identity.get('detached')})."]
    if identity:
        lines.append(f"Source tree: {identity['source_tree']}. Package guard: {identity.get('package_guard')}")
        lines.append(f"Procedure: scripts/rigbuild at {identity.get('tool_sha')}.")
    if ready:
        lines += [f"Package: {staged['package']}", "Bookings: " + ", ".join(staged["bookings"]), READY_SENTENCE]
    elif ev.exists("BLOCKED.md"):
        lines += ["", ev.path("BLOCKED.md").read_text(encoding="utf8").strip()]
    for part, project in (("Bridge", stage.BRIDGE), ("Helper", stage.HELPER)):
        lines.append(f"{part} DLL SHA256: {record.get('dlls', {}).get(project, 'No completed native-chain hash')}")
    lines += ["", "## Native chain", ""]
    lines += native_lines(ev, record or None, names=native.step_names())
    lines += ["", "## Steps after the native chain", ""]
    lines += native_lines(ev, None, names=AFTER_NATIVE)
    lines += ["", "## Golden and page weights", "",
              (f"Golden serial, {gold['python_version']} — {gold['passed']} passed, {gold['failed']} failed, exit "
               f"{gold['exit']}." if gold else "Golden — no completed result."),
              (f"Unmodified trim-destinations test — {trim['passed']} passed, {trim['failed']} failed, exit "
               f"{trim['exit']}." if trim else "Trim destinations — no completed result.")]
    if integrated.get("receipt_line"):
        lines.append(integrated["receipt_line"])
    if combined:
        lines.append(f"Combined default corpus — typical {combined['typical_first_page_json_median']:g} B and heaviest "
                     f"{combined['max_json_bytes']} B against {combined['page_json_ceiling']:,} B. Encoded maximum "
                     f"{combined['max_encoded_bytes']} B. {combined['checked_pages']} pages, "
                     f"{combined['choices_lost']} choices lost.")
    lines += ["", "## Python gate evidence", "", python_gates,
              "No package delta or full Python suite was run by this procedure.", "", "## Source defaults", "",
              "| Setting | Default | Source |", "| --- | --- | --- |"]
    for row in load("new-settings-defaults") or []:
        value = str(row["default"]).lower() if isinstance(row["default"], bool) else row["default"]
        lines.append(f"| `{row['setting']}` | {value} | `{row['source']}:{row['line']}` |")
    lines += ["", "`scripts/livepath/mabar.py` applies its live profile and prints the live values at preflight.",
              "", "## Operator warning", "", ev.path("operator-warning.txt").read_text(encoding="utf8").strip(),
              "", f"Evidence: {ev.E}"]
    refusals = ev.path("ct-privacy-refused.jsonl")
    if refusals.exists():
        lines += ["", "## Privacy guard refusal rows", "", "```jsonl", refusals.read_text(encoding="utf8").strip(), "```"]
    paths.receipt.parent.mkdir(parents=True, exist_ok=True)
    with paths.receipt.open("x", encoding="utf8") as f:
        f.write("\n".join(lines) + "\n")
    ev.say(str(paths.receipt))
    return paths.receipt
