"""G1: declarative continue-and-seal engine; no native verdict from process exit."""
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from scripts.free_play import soak
from scripts.free_play.i1_collector import CollectionFault, encoded


class Terminal(RuntimeError):
    def __init__(self, cause):
        if cause not in ("H1", "H2", "H3", "H4", "H5"):
            raise ValueError(cause)
        self.cause = cause
        super().__init__(cause)


class Runner:
    def __init__(self, output, collector, actions, monitor, closeout, *, receipt=None, recover=None):
        self.output, self.collector = Path(output), collector
        self.output.mkdir(parents=True, exist_ok=False)
        self.actions, self.monitor, self.closeout = actions, monitor, closeout
        self.ledger, self.faults, self.terminal, self.owned, self.backup = [], [], None, [], None
        self.receipt = receipt or {}
        self.recover, self.contained, self.apparatus, self.pending = recover, False, False, None
        self.actions = dict(control=collector.control, request=collector.request, stop=collector.stop,
                            **actions, capture=collector.capture, wait=self.wait, command=self.command)

    def check(self):
        if self.terminal: return
        try:
            self.monitor()
        except Terminal as exc:
            self.terminal = exc.cause
            raise

    def wait(self, seconds, label="dwell"):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check(); self.collector.capture(label)
            time.sleep(min(.1, max(0, deadline-time.monotonic())))

    def command(self, argv, *, env, label):
        with (self.output / (label + ".log")).open("xb") as log:
            child = subprocess.Popen(list(map(str, argv)), env=env, cwd=soak.ROOT,
                                     stdout=log, stderr=subprocess.STDOUT)
            self.owned.append(child)
            while child.poll() is None:
                self.wait(.1, label)
        if child.returncode:
            raise AssertionError(f"{label} returncode={child.returncode}")

    def witness(self, argv, *, env, label):
        return self.command([sys.executable, "-B", soak.ROOT / "scripts/m1_operator/witness_run.py", *argv], env=env, label=label)

    def baseline(self, files, saves, *, owned_saves=()):
        baseline = soak.Baseline(self.output, files, saves, owned_saves=owned_saves)
        baseline.capture()
        self.backup = baseline
        return baseline

    def fault(self, drill, exc):
        row = dict(id=drill, type=type(exc).__name__, reason=str(exc), unix_ns=time.time_ns())
        self.faults.append(row)
        try:
            with (self.output / "faults.jsonl").open("ab") as out: out.write(encoded(row) + b"\n")
        except OSError:
            self.apparatus = True

    def run(self, schedule):
        ids = [d["id"] for d in schedule]
        if len(ids) != len(set(ids)): raise ValueError("duplicate drill IDs")
        try:
            for drill in schedule:
                done = {r["id"]: r["outcome"] for r in self.ledger}
                blocked = [n for n in drill.get("needs", []) if done.get(n) != "DONE"]
                blocked.extend(drill.get("blocked_by", []))
                if self.terminal and not drill.get("passive"): blocked.append(self.terminal)
                if self.contained and not blocked and not drill.get("passive"):
                    try:
                        # Recovery caller re-observes; absent W never proves release.
                        proof = self.recover(self.pending) if self.recover else None
                        if not proof or not proof.get("w_and_lease_released") or not self.collector.released(proof.get("attempt_id")):
                            blocked.append("mutation contained; matching W + lease/native held proof required")
                        else: self.contained = False
                    except Terminal as exc:
                        self.terminal = exc.cause; blocked.append(exc.cause)
                    except Exception as exc:
                        self.fault(drill["id"], exc); blocked.append("recovery unavailable")
                row = dict(id=drill["id"], tier=drill.get("tier", "T1"),
                           assertion=drill.get("assertion", "UNREVIEWED"), variant=drill.get("variant", "retained"),
                           destination=drill.get("destination", "I1"), blocked_by=blocked, outcome="M",
                           start=drill.get("start", "UNKNOWN"), attempt=drill.get("attempt", "UNKNOWN"),
                           query=drill.get("query", "UNKNOWN"), raw_from=self.collector.sample+1, raw_to="")
                self.ledger.append(row)
                if blocked: continue
                try:
                    for step in drill["steps"]:
                        op = step["op"]
                        safe_control = op == "control" and step.get("args", {}).get("kind") in ("release", "cancel", "none") and "claim" not in step.get("args", {})
                        if drill.get("passive") and op not in ("capture", "wait") and not safe_control:
                            raise ValueError("passive case may only collect/wait")
                        self.check()
                        self.collector.capture("pre:" + drill["id"])
                        self.actions[op](**step.get("args", {}))
                        self.collector.capture("post:" + drill["id"])
                        self.check()
                    row["outcome"] = "DONE"
                except Terminal as exc:
                    self.terminal = exc.cause
                    row["outcome"] = "FAIL"; self.fault(drill["id"], exc)
                except Exception as exc:
                    row["outcome"] = "FAIL"; self.fault(drill["id"], exc)
                    self.contained, self.pending = True, row
                    if isinstance(exc, (OSError, CollectionFault)) and not isinstance(exc, FileExistsError):
                        self.apparatus = True
                finally:
                    row["raw_to"] = self.collector.sample
        finally:
            try:
                try:
                    self.collector.capture("final-native")
                finally:
                    closure = self.closeout()
                if any(p.poll() is None for p in self.owned): raise RuntimeError("owned command alive; no forced shutdown")
                if self.backup:
                    if not closure or not closure.get("barrier_resolved") or soak.smapi_running():
                        raise RuntimeError("closure/barrier unresolved; preserve baseline")
                    self.backup.restore()
            except Exception as exc:
                self.apparatus = True; self.fault("closeout", exc)
            finally:
                self.seal()
        return 2 if self.terminal else int(self.apparatus or any(r["outcome"] != "DONE" for r in self.ledger))

    def seal(self):
        with (self.output / "coverage.csv").open("x", newline="", encoding="utf-8") as out:
            fields = ("id", "tier", "assertion", "variant", "destination", "blocked_by", "outcome", "start", "attempt", "query", "raw_from", "raw_to")
            writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader(); writer.writerows(self.ledger)
        template = Path(__file__).with_name("i1_receipt.txt").read_text(encoding="utf-8")
        values = dict(self.receipt, faults=len(self.faults), terminal=self.terminal or "none",
                      coverage="coverage.csv", freeze="NOT FROZEN; independent T1/native/policy/guard review required")
        class Missing(dict):
            def __missing__(self, key): return "UNKNOWN"
        (self.output / "RECEIPT.md").write_text(template.format_map(Missing(values)), encoding="utf-8")
        roots = [self.output, self.collector.output]
        with (self.output / "SHA256SUMS").open("x", encoding="utf-8") as sums:
            for root in roots:
                for path in root.rglob("*"):
                    if path.is_file() and path.name != "SHA256SUMS":
                        with path.open("rb") as raw: digest = hashlib.file_digest(raw, "sha256").hexdigest()
                        sums.write(digest + "  " + str(path.resolve()) + "\n")


def schedule(bindings, *, routes=(), rows=None):
    """Book all retained T1 rows. Missing bindings/dependencies stay M, never DONE."""
    rows = rows or json.loads(Path(__file__).with_name("i1_tiers.json").read_text(encoding="utf-8"))
    result = []
    for row in rows:
        if row["tier"] != "T1": continue
        variants = list(routes) if row["index"] in (22, 26) else [dict(id=v) for v in row.get("variants", ["retained"])]
        if not variants: variants = [dict(id="no-enabled-routes")]
        for variant in variants:
            key = str(row["index"]) + ":" + variant["id"]
            binding = bindings.get(key, {})
            blocked = [] if binding.get("steps") else ["unbound fixture/assertion: " + key]
            if row["status"] != "drivable" and not binding.get("dependency_pins"):
                blocked.append(row["reason"])
            result.append(dict(binding, id=key, tier="T1", assertion=row["retained"],
                               variant=variant["id"], destination=row["destination"],
                               steps=binding.get("steps", []), blocked_by=blocked))
    return result


def drill(booking):
    """Explicit rig-owned factory binds legacy safety/recovery; no implicit live defaults."""
    import importlib
    module, name = booking.split(":", 1)
    runner, booked = getattr(importlib.import_module(module), name)()
    if not isinstance(runner, Runner): raise TypeError("booking must return Runner, schedule")
    return runner.run(booked)
