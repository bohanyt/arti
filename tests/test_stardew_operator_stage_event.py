"""Offline operator file publication and receipt tests; no installed mod or games."""
import json
import threading
import time

import pytest
from scripts.m1_operator import stage_event as operator


@pytest.fixture
def mod(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"OperatorEventStagingEnabled": True}))
    return tmp_path


@pytest.mark.parametrize("config", [{}, {"OperatorEventStagingEnabled": False},
    {"ControllerMenuStepEnabled": True}, {"ControllerStickHoldProbeEnabled": True},
    {"ControllerMenuStepEnabled": True, "ControllerStickHoldProbeEnabled": True,
     "OperatorEventStagingEnabled": False}, {"OperatorEventStagingEnabled": "true"}])
def test_stardew_stage_gate_off_does_not_publish(mod, config):
    (mod / "config.json").write_text(json.dumps(config))
    assert operator.stage("question", mod=mod, trial_check=lambda: False) == 2
    assert not (mod / "operator-event-staging").exists()


@pytest.mark.parametrize("running", [True, None])
def test_stardew_stage_trial_guard(mod, running):
    assert operator.stage("question", mod=mod, trial_check=lambda: running) == 2
    assert not (mod / "operator-event-staging").exists()


@pytest.mark.parametrize("verb,timeout", [("warp", 15), ("question extra", 15), ("control", 31), ("question", 0)])
def test_stardew_stage_strict_args(mod, verb, timeout):
    assert operator.stage(verb, mod=mod, timeout=timeout, trial_check=lambda: False) == 2
    assert not (mod / "operator-event-staging").exists()


def test_stardew_stage_pending_never_overwritten(mod):
    directory = mod / "operator-event-staging"
    directory.mkdir()
    pending = directory / "request.json"
    pending.write_text("existing")
    assert operator.stage("control", mod=mod, trial_check=lambda: False) == 2
    assert pending.read_text() == "existing"
    assert not list(directory.glob("*.tmp"))


@pytest.mark.parametrize("verb,success,reason,code", [("question", True, "event_started", 0),
    ("control", True, "event_started", 0), ("question", False, "farmhand_only", 2),
    ("festival-start", True, "event_started", 0), ("festival-start", False, "host_at_free_roam_festival_required", 2)])
def test_stardew_stage_waits_correlated_receipt(mod, verb, success, reason, code):
    errors = []

    def fake_mod():
        try:
            directory = "operator-festival-staging" if verb == "festival-start" else "operator-event-staging"
            pending = mod / directory / "request.json"
            deadline = time.monotonic() + 3
            while not pending.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            request = json.loads(pending.read_text())
            assert set(request) == {"id", "verb", "expires_at_ms"}
            assert request["verb"] == verb and len(request["id"]) == 32
            assert 0 < request["expires_at_ms"] - time.time() * 1000 <= 30000
            consumed = pending.with_name("consumed.json")
            pending.rename(consumed)
            receipt = pending.with_name(request["id"] + ".receipt.json")
            temp = receipt.with_suffix(".tmp")
            temp.write_text(json.dumps(dict(id=request["id"], success=success, reason=reason)))
            temp.rename(receipt)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=fake_mod)
    worker.start()
    assert operator.stage(verb, mod=mod, timeout=2, trial_check=lambda: False) == code
    worker.join(3)
    assert not worker.is_alive() and not errors


def test_stardew_stage_timeout_no_republication(mod):
    assert operator.stage("question", mod=mod, timeout=1, trial_check=lambda: False) == 3
    directory = mod / "operator-event-staging"
    assert len(list(directory.glob("request.json"))) == 1
    request = json.loads((directory / "request.json").read_text())
    assert request["expires_at_ms"] <= time.time() * 1000
    assert operator.stage("question", mod=mod, timeout=1, trial_check=lambda: False) == 2


def test_stardew_stage_module_inert():
    # The operator tool is outside runtime/model publication; mod polling is outside TickCommandInbox.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    mod = (root / "stardew-mod/ArtiStardewBridge/ModEntry.EventStaging.cs").read_text()
    assert "this.StageEvent, DateTimeOffset.UtcNow" in mod
    assert "!this.config.OperatorEventStagingEnabled" in mod
    assert "ControllerMenuStepEnabled" not in mod and "ControllerStickHoldProbeEnabled" not in mod
    assert "Context.IsWorldReady, Context.IsMultiplayer, Context.IsMainPlayer, Context.IsSplitScreen," in mod
    config = (root / "stardew-mod/ArtiStardewBridge/ModConfig.cs").read_text()
    assert "public bool OperatorEventStagingEnabled { get; set; } = false;" in config
    assert "OperatorEventStaging.IdentityRefusal(Context.IsWorldReady, Context.IsMultiplayer, Context.IsMainPlayer, Context.IsSplitScreen)" in mod
    for name in ("arti_stardew_command.py", "arti_stardew_runtime.py", "arti_stardew_decision.py"):
        assert "operator-event-staging" not in (root / name).read_text(encoding="utf-8")
