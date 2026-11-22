import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.free_play import soak
from scripts.livepath import bridge, cache, rehearse, safety


@pytest.mark.parametrize("reasons,rc", [(["window_not_drawable", "rehearsal_budget"], 0),
    (["rehearsal_budget", "disk_projection"], 0), (["rehearsal_budget"], 1), (None, 0)])
def test_budget_does_not_override_fault_or_crash(reasons, rc):
    assert not safety.budget_only(reasons, rc)


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_cleanup_interruption_keeps_lock_and_loud_marker(tmp_path, monkeypatch, capsys, failure):
    rig = tmp_path / "rig"
    (rig / "rig").mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()
    (run / "completed.json").write_text("{}")  # completion alone is never safe
    monkeypatch.setattr(soak, "checked_rig", lambda *a: ({}, rig))
    monkeypatch.setattr(soak, "owner_idle", lambda *a: None)
    def interrupted(args):
        raise failure("cleanup did not finish")
    monkeypatch.setattr(soak, "_run_soak", interrupted)
    with pytest.raises(failure):
        soak.run_soak(SimpleNamespace(output=run, rehearsal_checkout=tmp_path))
    assert (rig / "rig/free-play.lock").is_file()
    assert (run / "UNSAFE_NOT_RESTORED").is_file()
    assert "UNSAFE_NOT_RESTORED" in capsys.readouterr().err


def test_cleanup_marker_cleared_only_after_restore_and_voice_stopped(tmp_path):
    (tmp_path / "UNSAFE_NOT_RESTORED").touch()
    (tmp_path / "rehearsal-state.json").write_text(json.dumps(dict(staging_started=True, voice_spawn_attempted=True)))
    (tmp_path / "restore.json").write_text(json.dumps(dict.fromkeys(safety.RESTORE_FLAGS, True)))
    assert not safety.cleanup_receipt(tmp_path)
    (tmp_path / "realpath.json").write_text(json.dumps(dict(stopped=True)))
    assert safety.cleanup_receipt(tmp_path)
    assert not (tmp_path / "UNSAFE_NOT_RESTORED").exists()


@pytest.mark.parametrize("staged", [False, True])
def test_no_voice_spawn_refusal_releases_shared_lock_and_alert(tmp_path, monkeypatch, staged):
    rig, run = tmp_path / "rig", tmp_path / "run"
    (rig / "rig").mkdir(parents=True)
    run.mkdir()
    monkeypatch.setattr(soak, "checked_rig", lambda *a: ({}, rig))
    monkeypatch.setattr(soak, "owner_idle", lambda *a: None)
    def refusal(args):
        soak.write(run / "rehearsal-state.json", dict(staging_started=staged, voice_spawn_attempted=False))
        soak.write(run / "ct-alert-target.json", dict(path=str(rig / "rig/CT-ALERT.json")))
        safety.alert(run, "startup")
        if staged:
            soak.write(run / "restore.json", dict.fromkeys(safety.RESTORE_FLAGS, True))
        raise RuntimeError("HOST never ready" if staged else "no changes")
    monkeypatch.setattr(soak, "_run_soak", refusal)
    with pytest.raises(RuntimeError):
        soak.run_soak(SimpleNamespace(output=run, rehearsal_checkout=tmp_path))
    assert not (rig / "rig/free-play.lock").exists()
    assert not (rig / "rig/CT-ALERT.json").exists()
    assert not (run / "UNSAFE_NOT_RESTORED").exists()


def test_unsafe_cleanup_publishes_fixed_ct_alert(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    target = tmp_path / "CT-ALERT.json"
    soak.write(run / "ct-alert-target.json", dict(path=str(target)))
    assert not safety.cleanup_receipt(run)
    assert json.loads(target.read_text())["state"] == "UNSAFE_NOT_RESTORED"


def test_trial_cleanup_timeout_stays_unsafe_despite_restore_flags(tmp_path):
    soak.write(tmp_path / "rehearsal-state.json", dict(staging_started=True, voice_spawn_attempted=False))
    soak.write(tmp_path / "restore.json", dict.fromkeys(safety.RESTORE_FLAGS, True))
    soak.write(tmp_path / "rehearsal-budget-stop.json", dict(reason="trial_cleanup_timeout; no relaunch"))
    assert not safety.cleanup_receipt(tmp_path)
    assert (tmp_path / "UNSAFE_NOT_RESTORED").is_file()


@pytest.mark.parametrize("reason", sorted(safety.STOP_UNCERTAINTIES))
def test_budget_race_reports_unknown_only_for_exact_refusal(tmp_path, reason):
    log = tmp_path / "runner.log"
    log.write_text(json.dumps(dict(ok=False, refused=reason)) + "\n")
    assert safety.budget_outcome(log, ["rehearsal_budget"], 2)["status"] == "UNKNOWN"
    assert safety.budget_outcome(log, ["rehearsal_budget", "low_disk"], 2)["status"] == "FAIL"
    assert safety.budget_outcome(log, ["rehearsal_budget"], 1)["status"] == "FAIL"


def test_cache_preflight_refuses_missing_seed_and_changed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="unprepared"):
        cache.validate_cache()
    model = tmp_path / "model.onnx"
    model.write_bytes(b"synthetic-model")
    stat = model.stat()
    receipt = dict(schema=1, voice="F1", files=[dict(name="model.onnx", size=stat.st_size, mtime_ns=stat.st_mtime_ns)])
    (tmp_path / "livepath-ready.json").write_text(json.dumps(receipt))
    assert cache.validate_cache() == tmp_path
    model.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="unavailable"):
        cache.validate_cache()


def test_cache_preflight_refuses_receipt_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path)
    (tmp_path / "livepath-ready.json").write_text(json.dumps(dict(schema=1, voice="F1", files=[dict(name="../other")])))
    with pytest.raises(RuntimeError):
        cache.validate_cache()


def test_explicit_cache_preparation_reuses_assets_offline(tmp_path, monkeypatch):
    import sys
    calls = []
    class TTS:
        def __init__(self, *, auto_download):
            calls.append(auto_download)
        def get_voice_style(self, *, voice_name):
            calls.append(voice_name)
    monkeypatch.setattr(rehearse, "idle_guard", lambda: None)
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "supertonic", SimpleNamespace(TTS=TTS))
    (tmp_path / "model.onnx").write_bytes(b"synthetic-model")
    (tmp_path / "F1.json").write_text("{}")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache/download.lock").touch()
    cache.main()
    assert calls == [False, "F1"]
    assert cache.validate_cache() == tmp_path
    assert {r["name"] for r in json.loads((tmp_path / "livepath-ready.json").read_text())["files"]} == {"model.onnx", "F1.json"}


def test_cache_preparation_hold_refuses_before_model_load(tmp_path, monkeypatch):
    import sys
    def held():
        raise RuntimeError("HOUSEKEEPING_HOLD")
    monkeypatch.setattr(rehearse, "idle_guard", held)
    monkeypatch.setitem(sys.modules, "supertonic", SimpleNamespace(TTS=lambda **kw: pytest.fail("model loaded during hold")))
    with pytest.raises(RuntimeError, match="HOLD"):
        cache.main()


def test_failed_offline_reseed_cannot_reuse_previous_success(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(rehearse, "idle_guard", lambda: None)
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path)
    receipt = tmp_path / "livepath-ready.json"
    receipt.write_text("previous success")
    def unavailable(*, auto_download):
        assert auto_download is False
        raise RuntimeError("offline assets unavailable")
    monkeypatch.setitem(sys.modules, "supertonic", SimpleNamespace(TTS=unavailable))
    with pytest.raises(RuntimeError, match="offline assets"):
        cache.main()
    assert not receipt.exists()
    with pytest.raises(RuntimeError, match="unprepared"):
        cache.validate_cache()


def test_parent_watchdog_uses_parent_handle_and_kills_only_own_tree(tmp_path, monkeypatch):
    import ctypes
    import subprocess
    threads, opened, closed, killed = [], [], [], []
    class Function:
        def __init__(self, fn):
            self.fn = fn
        def __call__(self, *args):
            return self.fn(*args)
    outcomes = iter([258, 0])  # parent lives once, then its HANDLE signals exit
    kernel = SimpleNamespace(
        OpenProcess=Function(lambda access, inherit, pid: opened.append((access, pid)) or 999),
        WaitForSingleObject=Function(lambda handle, ms: next(outcomes)),
        CloseHandle=Function(closed.append))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    monkeypatch.setattr(bridge.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: threads.append(kw["target"])))
    monkeypatch.setattr(bridge.os, "getpid", lambda: 4242)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: killed.append(argv))
    class Exited(BaseException):
        pass
    def exit_now(rc):
        assert rc == 3
        raise Exited
    monkeypatch.setattr(bridge.os, "_exit", exit_now)
    bridge.start_parent_watchdog(tmp_path, 123)
    assert opened == [(0x100000, 123)]
    with pytest.raises(Exited):
        threads[0]()
    assert killed == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert closed == [999] and (tmp_path / "UNSAFE_NOT_RESTORED").is_file()


def test_supertone_fallback_aborts_instead_of_running_on_edge():
    with pytest.raises(RuntimeError, match="fallback observed"):
        rehearse.VoiceBridge._check_supertone("[TTS] voice fallback to edge-tts")
