"""Inert, config-only binding of the rig's existing witness services.

Import and construction do not book a desktop. check_idle owns the handshake;
after_backup alone may release STOP. No launch, motor or save implementation lives
here: those operations remain in the pinned rig service and stage_save/soak.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import os
from pathlib import Path
import socket
import subprocess
import sys

from arti_runtime.witness import WitnessFailure
from scripts.free_play import witness as W
from scripts.free_play import soak


def housekeeping(folder):
    folder = Path(folder)
    if not (folder / "HOUSEKEEPING_HOLD").is_file():
        raise WitnessFailure("housekeeping_hold_required")
    states = {}
    for name in ("housekeeping-progress.json", "phase-a-progress.json"):
        status = W.read(folder / name).get("status")
        if status == "running":
            raise WitnessFailure("housekeeping_running:" + name)
        # Unknown, failed or absent acknowledgements are not a completed handoff.
        if status not in ("hold", "completed"):
            raise WitnessFailure("housekeeping_not_acknowledged:" + name)
        states[name] = status
    return states


def tasklist():
    result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], check=True,
                            capture_output=True, text=True, timeout=30)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or any(len(row) < 2 for row in rows):
        raise WitnessFailure("process_snapshot_unavailable")
    return {row[0].casefold() for row in rows}


def pinned(spec, path):
    path = Path(path).resolve()
    if spec["pins"].get(str(path)) != W.digest(path):
        raise WitnessFailure("artifact_pin_mismatch:" + path.name)
    return path


def service(spec, output):
    binding = spec["rig"].get("service")
    if not binding or not binding.get("path"):
        raise WitnessFailure("native_rig_service_unbound")
    path = pinned(spec, binding["path"])
    module_spec = importlib.util.spec_from_file_location("arti_witness_rig_service", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    result = getattr(module, binding["factory"])(spec=spec, output=output)
    if result.offline is not False:
        raise WitnessFailure("live_booking_required")
    return result


class Booking:
    offline = False

    def __init__(self, *, spec, output):
        self.spec, self.output = spec, Path(output)
        self.cfg = W.read(pinned(spec, spec["rig"]["config"]))
        self.root = Path(self.cfg["root"]).resolve()
        self.stop = self.root / "STOP"
        self.lock = self.root / "rig/witness.lock"
        self.acquired = self.released_stop = False
        self.driver = None

    def __getattr__(self, name):
        if self.driver is None:
            raise WitnessFailure("booking_not_acquired")
        return getattr(self.driver, name)

    def check_idle(self):
        if os.name != "nt" or socket.gethostname().casefold() != self.cfg["hostname"].casefold():
            raise WitnessFailure("configured_rig_required")
        states = housekeeping(self.spec["rig"]["housekeeping"])
        processes = tasklist()
        if processes & {"stardewmoddingapi.exe", "obs64.exe", "stardew valley.exe"}:
            raise WitnessFailure("owner_game_or_obs_occupied")
        soak.owner_idle(self.cfg)
        if (self.root / "rig/free-play.lock").exists():
            raise WitnessFailure("free_play_already_booked")
        if (self.root / "operator-staging/run/SOAK_STOP").exists():
            raise WitnessFailure("soak_stop_requires_ct")
        # Import a rig service only after the no-owner/no-housekeeping gates.
        # Its factory must be inert; no launch until start/prepare_capture.
        self.driver = service(self.spec, self.output)
        self.driver.check_idle()
        from scripts.free_play import witness_session
        # Validate the supported verifier shape before taking a lock or staging.
        witness_session.bind(pinned(self.spec, self.spec["rig"]["session_procedure"]),
                             output=self.output, rig_config=self.cfg)
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("x", encoding="utf-8") as stream:
            stream.write(str(self.output.resolve()))
        self.acquired = True
        self.original_stop = self.stop.read_bytes() if self.stop.is_file() else None
        try:
            self.check()
            W.write(self.output / "booking.json", dict(tag="STAGING", housekeeping=states,
                stop_present=self.original_stop is not None, hold_retained=True))
        except BaseException:
            self.release_unstaged()
            raise

    def check(self):
        housekeeping(self.spec["rig"]["housekeeping"])
        if self.released_stop and self.stop.exists():
            raise WitnessFailure("global_stop")
        if (self.root / "operator-staging/run/SOAK_STOP").exists():
            raise WitnessFailure("global_stop")
        self.driver.check()

    def baseline(self, output):
        # Let the EXISTING rig service declare all of its mutation targets. No
        # partial parallel list of mailbox/config/native files is maintained here.
        baseline = self.driver.baseline(output)
        if not isinstance(baseline, soak.Baseline):
            raise WitnessFailure("existing_baseline_required")
        return baseline

    def after_backup(self):
        if not (self.output / "baseline.json").is_file():
            raise WitnessFailure("baseline_before_stop_release")
        self.check()
        from scripts.free_play import witness_session
        witness_session.capture(pinned(self.spec, self.spec["rig"]["session_procedure"]),
                                output=self.output, rig_config=self.cfg,
                                owned_saves=W.read(self.output / 'baseline.json').get('owned_saves', ()))
        if (self.stop.read_bytes() if self.stop.is_file() else None) != self.original_stop:
            raise WitnessFailure("stop_changed_before_release")
        if self.original_stop is not None:
            # The historical verifier also restores STOP.before when present.
            # Keep this booking's STOP under a distinct name: only the factory
            # may restore it, preserving a newly raised operator STOP.
            (self.output / "STOP.original").write_bytes(self.original_stop)
            self.stop.unlink()
        self.released_stop = True

    def verify_session(self):
        # Native runtime/session validation belongs to the existing integration.
        self.driver.verify_session()

    def finish_restore(self, restored):
        if not self.games_closed() or not all(restored.get(k) is True for k in
                ("saves_identical", "files_identical", "file_sets_identical")):
            raise WitnessFailure("restore_unverified")
        if self.released_stop and self.stop.exists():
            # An asynchronously raised STOP must never be silently overwritten.
            raise WitnessFailure("stop_changed_during_booking")
        if self.released_stop and self.original_stop is not None:
            with self.stop.open("xb") as stream:
                stream.write(self.original_stop)
        self.released_stop = False
        if (self.stop.read_bytes() if self.stop.is_file() else None) != self.original_stop:
            raise WitnessFailure("stop_restore_unverified")
        self.verify_restore()
        self.release_unstaged()

    def snapshot_root(self, binding):
        return self.root / "operator-staging/savestates" / binding["snapshot"]["name"] / "save"

    def stage_command(self, operation, binding):
        if not self.games_closed() or self.trial_running() is not False:
            raise WitnessFailure("closed_unenrolled_games_required")
        self.check()
        script = pinned(self.spec, W.ROOT / "scripts/m1_operator/stage_save.py")
        run = self.output / ("stage-save-" + operation)
        run.mkdir(exist_ok=False)
        env = {**os.environ, "ARTI_M1_PREP": str(self.root / "operator-staging"),
               "ARTI_M1_RUN": str(run), "ARTI_M1_WORKTREE": str(W.ROOT),
               "ARTI_CHECKOUT": str(W.ROOT), "ARTI_VENV_PYTHON": sys.executable,
               "ARTI_STARDEW_GAME": self.cfg["game"],
               "ARTI_STARDEW_SAVES": str(Path(self.cfg["appData"]) / "Saves")}
        with (run / "procedure.log").open("wb") as log:
            subprocess.run([sys.executable, str(script), "--save", binding["snapshot"]["save_name"],
                operation, binding["snapshot"]["name"]], env=env, stdout=log,
                stderr=subprocess.STDOUT, check=True, timeout=120)

    def capture_snapshot(self, binding):
        root = self.snapshot_root(binding)
        if root.parent.exists():
            raise WitnessFailure("snapshot_already_exists")
        self.stage_command("snapshot", binding)
        manifest = W.read(root.parent / "manifest.json")
        files = {p.relative_to(root).as_posix(): W.digest(p) for p in root.rglob("*") if p.is_file()}
        if not files or files != manifest["files"]:
            raise WitnessFailure("whole_snapshot_hash_mismatch")
        return dict(mode="snapshot", disposable=True, root=str(root.resolve()), files=files,
                    source_pin=self.spec["code_sha"], debug_in_snapshot=True)

    def restore_snapshot(self, root, files):
        root = Path(root).resolve()
        bindings = [b for b in self.spec["capabilities"].values()
                    if isinstance(b.get("snapshot", {}).get("name"), str)
                    and self.snapshot_root(b).resolve() == root]
        if len(bindings) != 1:
            raise WitnessFailure("snapshot_not_owned")
        binding = bindings[0]
        manifest = W.read(root.parent / "manifest.json")
        if manifest["files"] != files or manifest["save_name"] != binding["snapshot"]["save_name"]:
            raise WitnessFailure("whole_snapshot_hash_mismatch")
        self.stage_command("restore", binding)
        live = Path(self.cfg["appData"]) / "Saves" / binding["snapshot"]["save_name"]
        return files == {p.relative_to(live).as_posix(): W.digest(p) for p in live.rglob("*") if p.is_file()}

    def verify_restore(self):
        """Reuse session.py verify under this booking's evidence/rig globals."""
        from contextlib import redirect_stdout
        from scripts.free_play import witness_session
        procedure = pinned(self.spec, self.spec["rig"]["session_procedure"])
        with (self.output / "session-verify.log").open("w", encoding="utf-8") as log, redirect_stdout(log):
            receipt = witness_session.verify(procedure, output=self.output, rig_config=self.cfg,
                owned_saves=W.read(self.output / 'baseline.json').get('owned_saves', ()))
        if (self.stop.read_bytes() if self.stop.is_file() else None) != self.original_stop:
            raise WitnessFailure("stop_changed_during_verification")
        housekeeping(self.spec["rig"]["housekeeping"])
        W.write(self.output / "session-verification.json", dict(passed=True,
            baseline_sha256=W.digest(self.output / "baseline.json"), procedure_sha256=W.digest(procedure),
            receipt_sha256=W.digest(receipt), stop_identical=True, housekeeping_hold_retained=True))

    def release_unstaged(self):
        if self.acquired and not self.released_stop:
            if self.lock.read_text(encoding="utf-8") != str(self.output.resolve()):
                raise WitnessFailure("booking_lock_changed")
            self.lock.unlink()
            self.acquired = False


def booked(*, spec, output):
    # load_rig executes this file under its own module name; delegate to the package module so
    # the booking, its tests and the rig service share one Booking definition (ADR 96).
    from scripts.free_play import witness_factory
    return witness_factory.Booking(spec=spec, output=output)
