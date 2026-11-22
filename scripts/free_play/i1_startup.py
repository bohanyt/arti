"""G3: case-bound P2 adapter. Each paired case uses a new native start/output."""
from pathlib import Path
from tools import arti_rearm as p2
from scripts.free_play.i1_collector import encoded


class Startup:
    def __init__(self, collector, output, *, prior_closed, restored, unenrolled):
        self.collector, self.output = collector, Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.prior_closed, self.restored = prior_closed, restored
        self.unenrolled = unenrolled
        self.enrolled = False
    def fresh(self):
        s = self.collector.session()
        if (self.enrolled or self.collector.acted or not self.unenrolled() or not self.prior_closed() or not self.restored()
                or s.get("inflight_request_id") or s.get("owner_id") or s.get("input_busy")
                or (self.output / "enrolled.json").exists()):
            raise RuntimeError("requires fresh start, prior owned closure/restore, no enrollment/attempt")
    def rearm(self, declared, confirm):
        self.fresh()
        original = p2.read_session(self.collector.folder).get("halt")
        if original != declared or not original:
            raise RuntimeError("declared HALT bytes/entries changed")
        before = {n: p2._read(self.collector.folder / n) for n in (*p2.STOP_FILES, "rearm.json")}
        self.collector.capture("startup-before-confirmation")
        (self.output / "declared.json").write_bytes(encoded(declared))
        confirmed = []
        def checked(halt, notes):
            self.fresh()
            if confirmed or halt != original: return False
            confirmed.append(True)
            if not confirm(halt, notes): return False
            self.fresh()
            now = p2.read_session(self.collector.folder).get("halt")
            return now == original and all(p2._read(self.collector.folder / n) == raw for n, raw in before.items() if n != "rearm.json")

        with (self.output / "p2.log").open("x", encoding="utf-8") as log:
            outcome = p2.rearm(self.collector.folder, "rig_operator", checked, out=lambda s: log.write(s + "\n"))
        for p in (self.collector.folder / "stop-archive").glob("*"):
            if p.is_file(): self.collector.sources["archive-" + p.name] = p
        self.collector.capture("startup-after-p2:" + outcome)
        after = p2.read_session(self.collector.folder)
        witness = after.get("rearmed") or {}
        if outcome == "rearmed" and (after.get("halt") is not None or any(witness.get(k) != original[k] for k in ("episode_id", "halt_seq"))):
            raise RuntimeError("no matching native rearmed witness")
        (self.output / "outcome.json").write_bytes(encoded(dict(outcome=outcome, before_enrollment=True)))
        return outcome
    def enroll(self, enroll):
        self.fresh()
        if self.collector.session().get("halt") is not None:
            raise RuntimeError("HALT remains; no enrollment")
        self.enrolled = True
        (self.output / "enrolled.json").write_bytes(encoded(dict(before_token_writes_forbidden=True)))
        return enroll()
    def fixture(self, name, raw, *, games_closed, declaration=None):
        self.fresh()
        histories = (f"nights.{self.collector.save_id}.{self.collector.identity['player_id']}.json",
                     f"occurrences.{self.collector.save_id}.{self.collector.identity['player_id']}.json", "mod_exit.json")
        if not games_closed() or name not in (*histories, "halt.json", "stop.json", "stop_all.json", "rearm.json"):
            raise RuntimeError("offline declared startup fixture only")
        if (self.collector.folder / name).exists(): raise FileExistsError("preserve existing native history")
        if name in histories:
            import hashlib
            if (not declaration or declaration.get("sha256") != hashlib.sha256(raw).hexdigest()
                    or declaration.get("case") not in ("missing-plan", "first-bind-DayStarted", "lost-pending", "restart-history")
                    or not declaration.get("source_pin")):
                raise ValueError("declared offline history bytes/case/source pin required")
            (self.output / ("fixture-" + name)).write_bytes(encoded(declaration))
        self.collector.capture("STAGING-offline-before:" + name)
        with (self.collector.folder / name).open("xb") as stream:
            stream.write(raw)
        (self.output / ("STAGING-" + name)).write_bytes(raw)
        self.collector.capture("STAGING-offline-after:" + name)
