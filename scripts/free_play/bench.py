"""Plan, run or resume short game checks through the existing free-play soak (ADR 235, ADR 242).

One command runs a list of checks back to back, unattended, and prints one table:

    python -m scripts.free_play.bench run --checks 01,03,16 --test-build <lab-rig test-build folder>
    python -m scripts.free_play.bench plan --checks 01,03,16 [--test-build <folder>]
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid
import xml.etree.ElementTree as ET

from arti_runtime import ipc
from scripts.free_play import bench_format, bench_gates, savestate, soak, storage

DEFAULT_RIG_CONFIG = Path(__file__).resolve().parents[3] / "lab-rig" / "rig" / "rig-config.json"
PROTECTED_ID = 100000001
ROOT = Path(__file__).resolve().parents[2]
# Booking fields a test build does not carry; borrowed from the newest earlier booking (ADR 242).
RIG_FIELDS = ("rigConfig", "farm", "template", "hostTemplate", "trialConfig", "outputRoot",
              "cableMonitoringDisabled", "cableMonitoringAcceptedByOwner", "savestateLibrary")
SOAK_FILES = [
    "baseline.json and baseline/** (owned saves, config, inbox, journals, dependencies)",
    "STAGING*.json, STAGING-*.txt, STAGING-*.log; runtime/staging-*/** (stage_save receipts and screenshots)",
    "disk-preflight.json, objects manifest/objects, storage receipts, preserved-*/**, continuation-*/**, farm-state/**",
    "memory-runtime.json, memory checkpoint/archive receipts, next-candidate receipts",
    "request.json, template/**, host/** (native helper status/barrier/journals/ARMED/STOP)",
    "prepare.log, helper-preflight.json, launch-*.log, startup-pause*.json, ready.log, *-ready.log",
    "drawable-windows.jsonl, host-fg.log, pad-preflight.log, enroll.log, STAGING-menu*.log, focus-*.log",
    "runtime/** (operator launch records, checkpoint files, cycle/day journals, scoped STOP and provider archives)",
    "window.json, host-liveness.jsonl, disk-monitor.jsonl, day-*.log, journal/native outcomes and stop receipts",
    "close-*.log, *-SMAPI.txt, native-final/**, restore.json, completed.json, REPORT.md, metrics.json",
]


class Refused(RuntimeError):
    pass


def write(path, value):
    ipc.publish(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", fsync=True)


def protected(path):
    path = Path(path)
    def check(variant):
        parts = [part.casefold() for part in variant.parts]
        if ("farmA_100000001" in parts or
                any(parts[i:i + 3] == ["documents", "arti", "mabar"] for i in range(len(parts)))):
            raise Refused("protected save path")
    check(path.absolute())
    resolved = path.resolve()
    check(resolved)
    return resolved


def safe_save(save_id, folder):
    if (not re.fullmatch(r"-?[0-9]+", str(save_id))
            or int(save_id) <= 0 or int(save_id) == PROTECTED_ID or not isinstance(folder, str)
            or not re.fullmatch(r"[A-Za-z0-9 _.-]+_[0-9]+", folder)
            or int(folder.rpartition("_")[2]) != int(save_id)):
        raise Refused("protected or unresolved native save identity")
    return int(save_id)


def reviewed(path):
    path = protected(path)
    if (any(p.casefold() == "vault" for p in path.parts)
            or path.name.casefold() in {"config_local.json", "prefs.jsonl", "arti_soul.md"}
            or path.name.casefold().startswith(".env")):
        raise Refused("bench requires an explicit reviewed harness artifact")
    return path


def state(start, library, saves):
    protected(Path(library) / start["savestate"])
    entry = protected(savestate.entry_path(start["savestate"], library))
    if not (entry / "manifest.json").is_file():
        raise FileNotFoundError(f"savestate unavailable: {start['savestate']}")
    doc = soak.read(reviewed(entry / "manifest.json"))
    staging = soak.read(reviewed(entry / "STAGING.json"))
    save_id = safe_save(doc["game_id"], doc["save_folder"])
    target = protected(Path(saves) / doc["save_folder"])
    if target.parent != Path(saves).resolve():
        raise Refused("owned save directory resolves outside the Saves root")
    if (safe_save(staging["save_id"], staging["save_folder"]) != save_id
            or staging["save_folder"] != doc["save_folder"]):
        raise Refused("savestate ownership mismatch")
    for name in doc["hashes"]:
        protected(storage.safe_child(entry, name))
    if target.exists():
        primary = protected(target / target.name)
        if primary.parent != target or not primary.is_file():
            raise Refused("cannot prove existing target save identity")
        native_id = ET.parse(primary).getroot().findtext("uniqueIDForThisGame")
        if safe_save(native_id, target.name) != save_id:
            raise Refused("existing target save has a different identity")
    doc = savestate.verify(entry)  # re-reads the archived native identity from the save itself
    return entry, doc


def process_gate():
    """Kept for callers that want one exception; the bench itself prints every gate (ADR 242)."""
    refused = [g for g in bench_gates.process_gates(bench_gates.census()) if not g.ok]
    if refused:
        raise Refused("game, OBS, test or build active (" + "; ".join(g.saw for g in refused) + ")")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def compose_booking(test_build, like=None, rig_config=DEFAULT_RIG_CONFIG):
    """A booking from one test-build folder: its DLLs and provenance, this checkout's HEAD, the rig fields of the
    newest earlier booking (or --like). Nothing is copied or installed here (ADR 242)."""
    build = reviewed(test_build)
    provenance = soak.read(reviewed(build / "provenance.json"))
    dll, helper = build / "bridge/ArtiStardewBridge.dll", build / "helper"
    for path, field in ((dll, "dll_sha256"), (helper / "ArtiHostHelper.dll", "helper_sha256")):
        if not path.is_file() or soak.sha(path) != provenance.get(field):
            raise Refused(f"test build {path.name} missing or its hash differs from provenance.{field}")
    if like is None:
        # Only earlier BENCH bookings (they name an outputRoot); soak specs share the file pattern but not the shape.
        rig = Path(soak.read(reviewed(rig_config))["root"])
        bookings = sorted((p for p in rig.glob("free-play-*-booking.json") if "outputRoot" in soak.read(p)),
                          key=lambda p: p.stat().st_mtime)
        if not bookings:
            raise Refused(f"no earlier bench booking (with outputRoot) in {rig} to borrow rig fields from; pass --like")
        like = bookings[-1]
    base = soak.read(reviewed(like))
    missing = [k for k in ("rigConfig", "template", "hostTemplate", "trialConfig", "outputRoot") if k not in base]
    if missing:
        raise Refused(f"booking {like} lacks {', '.join(missing)}; pass --like with an earlier bench booking")
    spec = {k: base[k] for k in RIG_FIELDS if k in base}
    spec.update(dll=str(dll), dllSha256=provenance["dll_sha256"], helper=str(helper),
                helperSha256=provenance["helper_sha256"], provenance=str(build / "provenance.json"),
                codeSha=git("rev-parse", "HEAD"), borrowedFrom=str(Path(like)), testBuild=str(build))
    return spec


class Booking:
    def __init__(self, path, source, spec=None):
        self.path = reviewed(path) if path is not None else None
        self.spec = spec if spec is not None else soak.read(self.path)
        self.source = git("rev-parse", "--verify", source + "^{commit}")
        if (self.source != git("rev-parse", "HEAD") or self.source != self.spec["codeSha"]
                or git("status", "--porcelain")):
            raise Refused("source, booking and clean checkout must agree")
        self.cfg_path = reviewed(self.spec["rigConfig"])
        self.cfg = soak.read(self.cfg_path)
        self.rig = reviewed(self.cfg["root"])
        self.saves = protected(Path(self.cfg["appData"]) / "Saves")
        self.library = reviewed(self.spec.get("savestateLibrary", savestate.LIBRARY))
        self.output = reviewed(self.spec["outputRoot"])
        self.hold = self.rig / "evidence/pytest-slot/HOLD.txt"
        self.lock = self.rig / "rig/free-play.lock"
        self.paths = {k: reviewed(self.spec[k]) for k in
                      ("dll", "helper", "hostTemplate", "trialConfig", "provenance")}
        self.stop_released = False
        self.wait_minutes = 0

    def gates(self, *, hold=None, resuming=None, checks=()):
        """Every admission gate, each naming what it saw (ADR 242, defect 5)."""
        try:
            rows = bench_gates.process_gates(bench_gates.census())
        except RuntimeError as exc:
            rows = [bench_gates.Gate("process-census", False, str(exc))]
        rows.append(bench_gates.owner_idle_gate(self.cfg))
        rows.append(bench_gates.disk_gate())
        rows.append(bench_gates.memory_gate())
        slot_row = bench_gates.slot_gate(self.hold, own=hold)
        if not slot_row.ok and resuming and self.hold.read_bytes().startswith(f"bench1 {resuming.name}\n".encode()):
            slot_row = bench_gates.Gate("slot", True, "held by the interrupted run being resumed")
        rows.append(slot_row)
        for row in bench_gates.lock_gates(self.rig, stop_released=self.stop_released):
            if row.name == "free-play-lock" and not row.ok and resuming:
                locked = Path(soak.read(self.lock)["output"]).resolve()
                if locked.parent.parent == resuming:
                    verify_restore(locked)
                    row = bench_gates.Gate(row.name, True, "lock of the resumed run; its restore is verified")
            rows.append(row)
        rows.extend(savestate_gates(checks, self.library, self.saves))
        return rows

    def gate(self, *, hold=None, resuming=None, checks=(), wait_minutes=None):
        minutes = getattr(self, "wait_minutes", 0) if wait_minutes is None else wait_minutes
        refused = wait_for_gates(lambda: self.gates(hold=hold, resuming=resuming, checks=checks), minutes)
        if refused:
            raise Refused("; ".join(g.line() for g in refused))

    def recover(self, run):
        """ADR 242 follow-up (c): close what a failed sitting launched (by its launch records) and restore it."""
        env = dict(os.environ, ARTI_M1_RUN=str(Path(run) / "runtime"), PYTHONPATH=str(ROOT))
        return soak.recover_leftover(run, self.cfg, lambda args, log: soak.call("ui_op.py", ["close", *args[1:]],
                                                                                  log, env))

    def identity(self):
        # The existing provenance preflight also verifies native tree and every dependency.
        return {str(p): soak.sha(p) for p in
                (self.path, self.cfg_path, self.paths["trialConfig"], self.paths["provenance"])}

    def run(self, out, check, spec, entry, manifest, observer):
        from scripts.free_play import said_lines
        from scripts.free_play.bench_choices import SETTING as TRIAL
        config = dict(soak.read(self.paths["trialConfig"]))
        cfg_path = out.with_suffix(".trial.json")
        config.pop(said_lines.KEY, None)
        config[TRIAL] = dict(mode=check["mode"], setup=spec["start"].get("setup", []),
                             buttons=spec.get("buttons", []))
        config["stardew_menu_fence_replay"] = True
        config["live7_fact_line_enabled"] = True
        config["stardew_game_text_delivery_enabled"] = True
        config["stardew_live7_ship_on_the_way"] = True
        config["stardew_live7_place_time_page"] = True
        if spec.get("voice_memory") is True:
            config["voice_game_memory_enabled"] = True
        if check["mode"] == "buttons":
            config["stardew_live7_goal_ladder"] = False
            config["stardew_provider_enabled"] = False
            config["stardew_spend_enabled"] = False
        if "said_lines" in spec:
            lines = out.with_suffix(".said.json")
            write(lines, spec["said_lines"])
            config.update({said_lines.KEY: str(lines), "stream_game_notes": True, "stardew_live7_goal_ladder": True})
        write(cfg_path, config)
        farm = manifest["farm"]
        later = None
        if record := spec["start"].get("record_from"):
            later, other = state({"savestate": record}, self.library, self.saves)
            if (manifest["identity"] != other["identity"] or manifest["farm"] != other["farm"]):
                raise Refused("later record must match the staged native save and farmhand")
        args = SimpleNamespace(
            bench=observer, bench_start=spec["start"], bench_identity=(manifest["game_id"], manifest["save_folder"]),
            bench_record=later,
            continue_from=entry, checkpoint=None, staging_script=None, hours=check["minutes"] / len(check["situations"]) / 60,
            days=1, farm=farm, output=out, code_sha=self.source, dll=self.paths["dll"],
            dll_sha256=self.spec["dllSha256"], helper=self.paths["helper"], helper_sha256=self.spec["helperSha256"],
            template=entry, host_template=self.paths["hostTemplate"], provenance=self.paths["provenance"],
            trial_config=cfg_path, host_delay=0, hp_hard_stop=True, real_path=False)
        soak.run_soak(args)


class RigView(Booking):
    """The rig facts the gates read, without a booking: what `plan` checks when no test build is named."""

    def __init__(self, rig_config=DEFAULT_RIG_CONFIG):
        self.path, self.spec, self.source = None, {}, None
        self.cfg_path = reviewed(rig_config)
        self.cfg = soak.read(self.cfg_path)
        self.rig = reviewed(self.cfg["root"])
        self.saves = protected(Path(self.cfg["appData"]) / "Saves")
        self.library = reviewed(savestate.LIBRARY)
        self.hold = self.rig / "evidence/pytest-slot/HOLD.txt"
        self.lock = self.rig / "rig/free-play.lock"
        self.stop_released = False


def savestate_gates(checks, library, saves):
    """Each savestate a selected check names: present, verified, never the protected save."""
    rows, seen = [], set()
    for check in checks:
        for case in check["situations"]:
            for name in filter(None, (case["start"]["savestate"], case["start"].get("record_from"))):
                if name in seen:
                    continue
                seen.add(name)
                try:
                    entry, doc = state({"savestate": name}, library, saves)
                    start = case["start"]
                    debug = "clock" in start or start.get("debug") or "debug" in start.get("last", {})
                    main = entry / "preserved-saves" / doc["save_folder"] / doc["save_folder"]
                    if debug and b"<allowChatCheats>true</allowChatCheats>" not in storage.read_bytes(main):
                        # benchrun4: every /debug line of 03 was refused at staging, minutes after the start.
                        rows.append(bench_gates.Gate("savestate", False, f"{name}: allowChatCheats is off, so "
                                                     "its /debug staging would be refused"))
                        continue
                    rows.append(bench_gates.Gate("savestate", True, f"{name}: {doc['native_date']} "
                                                 f"{doc['save_folder']} verified"))
                except FileNotFoundError:
                    if case["start"].get("from_checkpoint") and name == case["start"]["savestate"]:
                        rows.append(bench_gates.Gate("savestate", True, f"{name}: missing; run imports it from "
                                                     f"{case['start']['from_checkpoint']} (copy only)"))
                        continue
                    hint = case["start"].get("capture") or "make it with savestate add or from-checkpoint"
                    rows.append(bench_gates.Gate("savestate", False, f"{name}: missing ({hint[:160]})"))
                except (OSError, ValueError, KeyError, RuntimeError) as exc:
                    rows.append(bench_gates.Gate("savestate", False, f"{name}: {exc}"))
    return rows


def import_missing(checks, library, log=print):
    """ADR 242: a check whose savestate is missing but names its live checkpoint gets it imported here, copy only."""
    done = []
    for check in checks:
        for case in check["situations"]:
            start = case["start"]
            if not start.get("from_checkpoint") or (savestate.entry_path(start["savestate"], library)
                                                    / "manifest.json").is_file():
                continue
            entry = savestate.from_checkpoint(
                start["from_checkpoint"], start["savestate"], library=library,
                expect=[dict(id="copied-morning", kind="event", match=dict(ev="skill_session_started"))],
                broken=f"bench check {check['id']}: {check['question']}"[:300],
                journal_refs=[str(Path(start["from_checkpoint"]) / "manifest.json")])
            log(f"IMPORTED {start['savestate']} {entry}", flush=True)
            done.append(start["savestate"])
    return done


@contextmanager
def slot(path, token, duration):
    payload = f"bench1 {token}\n{soak.utc()}\n{duration} elapsed gameplay minutes plus setup/restore\n".encode()
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        raise Refused("pytest slot held") from None
    try:
        yield payload
    finally:
        if path.is_file() and path.read_bytes() == payload:
            path.unlink()


@contextmanager
def worker_lock(out):
    """The OS drops this lock if the detached task exits unexpectedly."""
    import msvcrt
    with (out / "worker.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise Refused("this bench run already has a worker") from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def recover_booking(booking, out):
    """Only this run's records and only after the process and restore gates."""
    booking.gate(resuming=out)
    if booking.lock.is_file():
        old = booking.lock.read_bytes()
        locked = Path(json.loads(old)["output"]).resolve()
        if locked.parent.parent == out:
            verify_restore(locked)
            if booking.lock.read_bytes() == old:
                booking.lock.unlink()
    if booking.hold.is_file():
        current = booking.hold.read_bytes()
        if current.startswith(f"bench1 {out.name}\n".encode()):
            booking.hold.unlink()
    marker = bench_gates.RestingStop(booking.rig, out)
    if marker.released:
        marker.restore()  # an interrupted worker left the resting marker released


def staged_and_left(check):
    """What staging does for each situation and what is left to her, from the data file itself."""
    rows = []
    for case in check["situations"]:
        start = case["start"]
        staged = [f"savestate {start['savestate']}"]
        if "clock" in start:
            staged.append(f"host /debug time {start['clock']}")
        staged.extend(f"farmhand {line}" for line in start.get("debug", []))
        if "last" in start:
            last = start["last"]
            staged.append(f"last, right before ARMED: {last['expect']} by "
                          + (last.get("debug") or f"key {last.get('key')}"))
        if "focus" in start:
            staged.append(f"her window active {start['focus']['hold_s']} s from {start['focus']['after_s']} s, "
                          "then the HOST again")
        if "host_gift_at" in start:
            staged.append(f"HOST gift at {start['host_gift_at']}")
        scripted = [f"{s['skill']} {s['arg'] or ''}".strip() for s in start.get("setup", [])]
        buttons = [f"{s['skill']} {s['arg'] or ''}".strip() for s in case.get("buttons", [])]
        left = ("scripted setup choices: " + ", ".join(scripted) + "; ") if scripted else ""
        left += ("scripted buttons: " + ", ".join(buttons)) if buttons else "every choice is hers (play mode)"
        rows.append(dict(situation=case["id"], staged=staged, left=left))
    return rows


def plan(checks, booking=None, clock=time.monotonic):
    """A dry plan: each stage the run would do, its own gates with what they saw, and measured times (no game)."""
    timings = {}
    started = clock()
    report = dict(checks=[], gates=[], refused=[], timings_s=timings)
    for check in checks:
        report["checks"].append(dict(id=check["id"], mode=check["mode"], limit_minutes=check["minutes"],
                                     needs=check.get("needs", []), situations=staged_and_left(check)))
    timings["load_checks"] = round(clock() - started, 3)
    if booking is not None:
        stage = clock()
        rows = booking.gates(checks=checks)
        timings["gates_and_savestates"] = round(clock() - stage, 3)
        report["gates"] = [g.line() for g in rows]
        report["refused"] = [g.line() for g in rows if not g.ok]
        if booking.spec:
            report["booking"] = {k: booking.spec.get(k) for k in ("codeSha", "dll", "helper", "testBuild",
                                                                    "borrowedFrom", "trialConfig", "outputRoot")}
    report["steps"] = ["gates (process census, owner idle, disk, memory, slot, rig locks, savestates)",
                       "take the slot HOLD for the whole run; release the resting STOP (hash kept in the run folder)",
                       "per check: gates again, stage the savestate, start HOST and farmhand, stage clock/place/menu, "
                       "arm, judge the journal every second, scoped STOP on a decided verdict or the limit",
                       "close both games, restore and verify the baseline, write the verdict row with seconds",
                       "after the last check (and on every exit): restore the STOP marker, release the slot, "
                       "write table.md"]
    report["writes"] = ["<run>/run.json, booking.json, progress.json, verdicts.json, table.md, done.json, "
                        "stop-marker.json, STOP.before, worker.lock, dispatch*.ps1 and its logs",
                        "<rig>/evidence/pytest-slot/HOLD.txt and <rig>/rig/free-play.lock",
                        "<run>/<check>/<situation>.trial.json, optional .said.json, and the soak output", *SOAK_FILES]
    timings["total"] = round(clock() - started, 3)
    return report


def dispatch(booking, out):
    token = uuid.uuid4().hex[:12]
    name = "ARTI-FreePlay-Bench-" + token
    job = out / ("dispatch-" + token + ".ps1")

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    job.write_text(
        "$ErrorActionPreference='Stop'\n"
        "try {\n"
        f"  Set-Location -LiteralPath {quote(ROOT)}\n"
        f"  & {quote(sys.executable)} -B -u -m scripts.free_play.bench _worker {quote(out)} "
        f"> {quote(job.with_suffix('.stdout.log'))} 2> {quote(job.with_suffix('.stderr.log'))}\n"
        "  exit $LASTEXITCODE\n"
        "} finally {\n"
        f"  Unregister-ScheduledTask -TaskName {quote(name)} -Confirm:$false\n"
        "}\n", encoding="utf-8-sig")
    subprocess.run(["powershell", "-NoProfile", "-File", str(Path(__file__).with_name("Invoke-Soak.ps1")),
                    "book-task", "-TaskName", name, "-JobScript", str(job),
                    "-TaskUser", booking.cfg["taskPrincipal"]], check=True)
    print(f"STARTED {name} {out}", flush=True)
    return job


def wait_for_gates(read_gates, minutes, *, clock=time.monotonic, sleep=time.sleep, log=print, every_s=30):
    """ADR 242 follow-up (d): with --wait-gates, re-check refused gates every 30 s for at most `minutes`, saying which
    gate it waits on and what it saw. Default 0 minutes: refuse at once, as before."""
    end = clock() + 60 * (minutes or 0)
    while True:
        refused = [g for g in read_gates() if not g.ok]
        if not refused or clock() >= end:
            return refused
        log(f"GATE WAIT ({round(end - clock())} s left) " + "; ".join(f"{g.name}: {g.saw[:140]}" for g in refused),
            flush=True)
        sleep(min(every_s, max(0.0, end - clock())))


def recover_sitting(booking, run, verdict):
    """ADR 242 follow-up (c): after a failed sitting, close what it launched and restore its baseline. True when
    the state is verified clean again (the next check may run); False seals the run with what was left open."""
    recover = getattr(booking, "recover", None)
    if recover is None:
        return False
    try:
        recover(run)
    except Exception as exc:  # noqa: BLE001 - sealed as blocked with the pid and the reason
        left = soak.read(run / "LEFT_OPEN.json").get("left") if (run / "LEFT_OPEN.json").is_file() else None
        verdict.update(left_open=left or [dict(pid=None, role="unknown", why=str(exc)[:200])],
                       recovery_error=str(exc)[:300])
        return False
    verdict.update(restored=True, recovered=True)
    return True


def wait_for_table(out, checks, *, clock=time.monotonic, sleep=time.sleep, log=print):
    """The one command waits for the detached worker and prints each row as it lands, then the table."""
    budget = 60 * (10 + sum(25 * len(c["situations"]) + c["minutes"] for c in checks))
    end, printed = clock() + budget, set()
    while clock() < end:
        verdicts = soak.read(out / "verdicts.json") if (out / "verdicts.json").is_file() else {}
        for key, row in verdicts.items():
            if key not in printed and not row["unattempted"]:
                printed.add(key)
                log(f"{key} {row['verdict']} {row.get('seconds')} s", flush=True)
        if (out / "done.json").is_file():
            done = soak.read(out / "done.json")
            log((out / "table.md").read_text(encoding="utf-8") if (out / "table.md").is_file() else "", flush=True)
            if done.get("error"):
                log("BENCH WORKER STOPPED " + done["error"], flush=True)
            return 0 if not done.get("error") else 3
        sleep(5)
    log(f"BENCH STILL RUNNING after {budget} s; follow {out}", flush=True)
    return 4


def execute(booking, out, checks, token, clock=time.time):
    # Imported here so format/planning never constructs a session or journal reader.
    from scripts.free_play import bench_judge
    progress = out / "progress.json"
    entries = soak.read(progress) if progress.is_file() else {}
    results = {check["id"]: bench_judge.summarize(check, []) for check in checks}
    gate_lines = []
    error = None
    booking.gate(checks=checks)
    rig = getattr(booking, "rig", None)
    marker = bench_gates.RestingStop(rig, out) if rig is not None else None
    try:
        with slot(booking.hold, token, sum(c["minutes"] for c in checks)) as held:
            if marker is not None:
                # ADR 242 (defect 1): the bench owns the resting STOP for this run, scoped and hash-verified.
                marker.release()
                booking.stop_released = True
            for index, check in enumerate(checks):
                cases = []
                for spec in check["situations"]:
                    key = check["id"] + "/" + spec["id"]
                    run = out / key
                    run.parent.mkdir(exist_ok=True)
                    if key in entries and entries[key]["status"] == "complete":
                        cases.append(entries[key]["verdict"])
                        continue
                    started = clock()
                    observer = bench_judge.Judge(run, spec)
                    status = "complete"
                    times = dict(started=started)
                    try:
                        if key in entries and run.exists():
                            # A resumed worker never starts an ambiguous sitting again.
                            verdict = observer.result(final=True, interruption="interrupted before sealed verdict")
                            verify_restore(run)
                        else:
                            try:
                                booking.gate(hold=held)
                            except Refused as exc:
                                gate_lines.append(f"{key}: {exc}")
                                raise
                            times["gated"] = clock()
                            entry, manifest = state(spec["start"], booking.library, booking.saves)
                            entries[key] = dict(status="starting")
                            write(progress, entries)
                            booking.run(run, check, spec, entry, manifest, observer)
                            times["soak_returned"] = clock()
                            verify_restore(run)
                            verdict = observer.result(final=True)
                    except Exception as exc:
                        armed = (run / "window.json").is_file()
                        refs = [p.name for p in sorted(run.glob("*.log")) if str(p) in str(exc)]
                        verdict = observer.result(final=True, interruption=str(exc), armed=armed,
                                                  interruption_refs=refs)
                        if isinstance(exc, FileNotFoundError) and not run.exists():
                            status = "unavailable"
                        elif not observer.restored() and run.exists() and not recover_sitting(booking, run, verdict):
                            verdict.update(timing(run, times, verdict, clock()))
                            entries[key] = dict(status="blocked", verdict=verdict)
                            write(progress, entries)
                            cases.append(verdict)
                            results[check["id"]] = bench_judge.summarize(check, cases)
                            write(out / "verdicts.json", results)
                            for unsealed in checks[index:]:
                                print_verdict(results[unsealed["id"]])
                            raise
                    verdict.update(timing(run, times, verdict, clock()))
                    entries[key] = dict(status=status, verdict=verdict)
                    write(progress, entries)
                    cases.append(verdict)
                results[check["id"]] = bench_judge.summarize(check, cases)
                write(out / "verdicts.json", results)
                print_verdict(results[check["id"]])
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            if marker is not None:
                marker.restore()
        finally:
            text = bench_judge.table(list(results.values()), gate_lines)
            ipc.publish(out / "table.md", text, fsync=True)
            write(out / "done.json", dict(utc=soak.utc(), error=error,
                                          stop_marker=marker.state() if marker is not None else None))


def timing(run, times, verdict, now):
    """Seconds from the check's start to its verdict, and each stage (ADR 242), from the soak's own records:
    window.json start_at (armed) and end_at (play ended), restore.json utc (restored). A verdict the journal decided
    during play counts from that moment; otherwise play ends at end_at (benchrun4: a drain ended play at 74 s, and
    the close and restore after it are not play)."""
    from datetime import datetime
    started = times["started"]
    window, restored = {}, None
    try:
        window = soak.read(run / "window.json")
    except (OSError, ValueError):
        pass
    try:
        restored = datetime.fromisoformat(soak.read(run / "restore.json")["utc"]).timestamp()
    except (OSError, ValueError, KeyError):
        pass
    armed, ended, decided = window.get("start_at"), window.get("end_at"), verdict.get("decided_at")
    if decided is not None and ended is not None and decided > ended + 1:
        decided = None  # decided only by the final read after close and restore
    verdict_at = decided or ended or now
    stages = dict(gates=times.get("gated") and round(times["gated"] - started, 1),
                  start_up=armed and round(armed - started, 1),
                  play_to_verdict=armed and round(verdict_at - armed, 1),
                  close_and_restore=(ended or verdict_at) and restored and round(restored - (ended or verdict_at), 1))
    return dict(seconds=round(verdict_at - started, 1), stages_s=stages)


def print_verdict(result):
    print(f"{result['id']} {result['verdict']} {result['fix']}", flush=True)


def verify_restore(run):
    receipt = soak.read(run / "restore.json")
    if not all(receipt.get(key) is True for key in ("saves_identical", "files_identical", "file_sets_identical")):
        raise Refused("restore not verified; retain the baseline and booking")


def replay_journal(path):
    from arti_runtime.menu_fence_replay import replay
    from scripts.free_play.evaluate import Evidence

    path = reviewed(path)
    evidence = Evidence(path.parent)
    count, failed = 0, False
    for number, row in evidence.rows(path):
        if row.get("ev") != "menu_fence_sample":
            continue
        if "frame" not in row:
            print(f"{path.name}:{number} UNAVAILABLE {row.get('unavailable')}")
            failed = True
            continue
        observed = replay(row)
        matched = observed == row["result"]
        print(f"{path.name}:{number} {row['site']} {observed or 'ACCEPTED'} {'MATCH' if matched else 'MISMATCH'}")
        failed |= not matched
        count += 1
    if not count or evidence.warnings:
        raise ValueError("complete replay evidence unavailable")
    print(f"Replayed {count} fence samples")
    return 1 if failed else 0


def booking_from_args(args):
    if getattr(args, "test_build", None):
        spec = compose_booking(args.test_build, args.like, args.rig_config)
        return Booking(None, spec["codeSha"], spec=spec)
    if getattr(args, "booking", None):
        return Booking(args.booking, args.source or git("rev-parse", "HEAD"))
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        mode = modes.add_parser(name)
        mode.add_argument("checks", nargs="*")
        mode.add_argument("--checks", dest="check_list", default="")
        mode.add_argument("--test-build", type=Path)
        mode.add_argument("--like", type=Path, help="earlier booking to borrow rig fields from")
        mode.add_argument("--rig-config", type=Path, default=DEFAULT_RIG_CONFIG)
        mode.add_argument("--booking", type=Path)
        mode.add_argument("--source")
        if name == "run":
            mode.add_argument("--no-wait", action="store_true")
            mode.add_argument("--wait-gates", type=float, default=0, metavar="MINUTES",
                              help="re-check refused gates every 30 s for up to MINUTES (default: refuse at once)")
    modes.add_parser("resume").add_argument("output", type=Path)
    modes.add_parser("replay").add_argument("journal", type=Path)
    modes.add_parser("_worker").add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in ("plan", "run"):
            names = list(args.checks) + [args.check_list] if args.check_list else list(args.checks)
            checks = bench_format.load(names)
        if args.command == "plan":
            booking = booking_from_args(args) if (args.test_build or args.booking) else (
                RigView(args.rig_config) if args.rig_config.is_file() else None)
            report = plan(checks, booking)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            for line in report["refused"]:
                print(line, file=sys.stderr)
            return 2 if report["refused"] else 0
        if args.command == "replay":
            return replay_journal(args.journal)
        if args.command == "run":
            booking = booking_from_args(args)
            if booking is None:
                raise Refused("run needs --test-build <folder> (or --booking and --source)")
            booking.wait_minutes = args.wait_gates
            refused = wait_for_gates(lambda: booking.gates(checks=checks), args.wait_gates)
            if not refused:
                import_missing(checks, booking.library)
                refused = [g for g in savestate_gates(checks, booking.library, booking.saves) if not g.ok]
            for gate in refused:
                print(gate.line(), file=sys.stderr)
            if refused:
                return 2
            out = booking.output / ("bench-" + uuid.uuid4().hex)
            out.mkdir(parents=True, exist_ok=False)
            if booking.path is None:
                booking.path = out / "booking.json"
                write(booking.path, booking.spec)
            record = dict(booking=str(booking.path), pins=booking.identity(), source=booking.source, checks=checks,
                          wait_gates=args.wait_gates)
            write(out / "run.json", record)
        else:
            out = reviewed(args.output)
            record = soak.read(out / "run.json")
            booking = Booking(record["booking"], record["source"])
            if out.parent != booking.output or booking.identity() != record["pins"]:
                raise Refused("booking artifacts changed")
            checks = [bench_format.validate(c) for c in record["checks"]]
            booking.wait_minutes = record.get("wait_gates", 0)
        if args.command in ("run", "resume"):
            with worker_lock(out):
                booking.gate(resuming=out if args.command == "resume" else None)
            dispatch(booking, out)
            if args.command == "run" and not args.no_wait:
                return wait_for_table(out, checks)
        else:
            os.environ["ARTI_FREE_PLAY_RIG_CONFIG"] = str(booking.cfg_path)
            with worker_lock(out):
                recover_booking(booking, out)
                execute(booking, out, checks, out.name)
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"BENCH REFUSED {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
