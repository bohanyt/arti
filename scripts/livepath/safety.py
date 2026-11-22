"""Rehearsal-only cleanup and stop receipts; no process actions."""
import json
from pathlib import Path
import sys


RESTORE_FLAGS = ("saves_identical", "files_identical", "file_sets_identical")


def alert(run, reason):
    """Persistent CT check target; seeded before any shared state is changed."""
    run = Path(run)
    target = run / "ct-alert-target.json"
    if target.is_file():
        path = Path(json.loads(target.read_text(encoding="utf-8"))["path"])
        path.write_text(json.dumps(dict(state="UNSAFE_NOT_RESTORED", run=str(run), reason=reason)) + "\n",
                        encoding="utf-8")


def restored(run):
    try:
        run = Path(run)
        admission = json.loads((run / "rehearsal-state.json").read_text(encoding="utf-8"))
        budget_path = run / "rehearsal-budget-stop.json"
        if budget_path.exists() and json.loads(budget_path.read_text(encoding="utf-8")).get("reason") == "trial_cleanup_timeout; no relaunch":
            return False  # the supervisor exited without proving its trial child stopped
        voice_path = run / "realpath.json"
        voice = json.loads(voice_path.read_text(encoding="utf-8")) if voice_path.exists() else {}
        # Absence alone is not proof: the startup receipt precedes all mutation/spawn.
        voice_stopped = voice.get("stopped") is True or (not voice and admission.get("voice_spawn_attempted") is False)
        if admission.get("staging_started") is False:
            return voice_stopped
        restore = json.loads((Path(run) / "restore.json").read_text(encoding="utf-8"))
        return all(restore.get(k) is True for k in RESTORE_FLAGS) and voice_stopped
    except (OSError, ValueError, TypeError):
        return False


def cleanup_receipt(run):
    run = Path(run)
    if not run.is_dir():
        return True  # no staging occurred
    marker = run / "UNSAFE_NOT_RESTORED"
    if restored(run) and not (run / "realpath-stop-error.json").exists():
        marker.unlink(missing_ok=True)
        target = run / "ct-alert-target.json"
        if target.is_file():
            path = Path(json.loads(target.read_text(encoding="utf-8"))["path"])
            # Do not acknowledge another run's alert.
            if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("run") == str(run):
                path.unlink()
        return True
    marker.write_text("CT MUST reconcile owned game/day runner, rehearsal DLL/config, saves and lock.\n", encoding="utf-8")
    print("!!! UNSAFE_NOT_RESTORED — baseline and free-play lock retained; CT intervention required !!!", file=sys.stderr)
    alert(run, "cleanup_unproven")
    return False


def budget_only(reasons, runner_rc):
    return reasons == ["rehearsal_budget"] and runner_rc == 0


STOP_UNCERTAINTIES = {"day_start_pass_not_clean:owner_stop", "trial_cleanup_timeout; no relaunch"}


def budget_outcome(log, reasons, runner_rc):
    """A known STOP race lacks clean-stop proof; it cannot earn PASS credit."""
    if budget_only(reasons, runner_rc):
        return dict(status="PASS", reason="budget_stop_acknowledged")
    if reasons == ["rehearsal_budget"] and runner_rc == 2:
        try:
            terminal = json.loads(Path(log).read_text(encoding="utf-8").splitlines()[-1])
            if terminal.get("ok") is False and terminal.get("refused") in STOP_UNCERTAINTIES:
                return dict(status="UNKNOWN", reason=terminal["refused"])
        except (OSError, ValueError, IndexError, AttributeError):
            pass
    return dict(status="FAIL", reason="runner_fault_or_nonbudget_stop")
