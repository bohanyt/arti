"""Windows process-handle proof with disposable Python sleepers, never a game."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process-handle adapter")


def test_real_windows_heartbeat_sharing_denial_recovers_without_game(tmp_path, monkeypatch):
    """Real kernel sharing denial on a disposable file; no process is launched."""
    from types import SimpleNamespace
    from scripts.stardew_trial_watchdog import run_watchdog, ipc
    heartbeat = tmp_path / 'heartbeat.json'
    heartbeat.write_text(json.dumps(dict(run_id='sharing-proof', monotonic=100)))
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle, now, sleeps, failures = [None], [100.0], [], []
    read_bytes = ipc.read_bytes
    def read(path):
        try:
            return read_bytes(path)
        except PermissionError as exc:
            if path == heartbeat:
                failures.append((exc.errno, getattr(exc, 'winerror', None)))
            raise
    monkeypatch.setattr(ipc, 'read_bytes', read)
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) == 1:
            # GENERIC_READ / no sharing / OPEN_EXISTING.
            handle[0] = api.CreateFileW(str(heartbeat), 0x80000000, 0, None, 3, 0, None)
            assert handle[0] not in (None, ctypes.c_void_p(-1).value)
        elif len(sleeps) == 2:
            assert failures and seconds <= .02
            assert api.CloseHandle(handle[0])
            handle[0] = None
        elif len(sleeps) == 3:
            (tmp_path/'STOP').touch()
        else:
            pytest.fail('unbounded retry')
    process = SimpleNamespace(alive=lambda:True, terminate=lambda:None,
        wait=lambda:True, close=lambda:None)
    try:
        result = run_watchdog(dict(run_id='sharing-proof', deadline_monotonic=200,
            control_dir=str(tmp_path), processes=[{'pid':10}]), factory=lambda _:process,
            clock=lambda:now[0], sleep=sleep)
        assert result == 'REQUESTED_STOP'
        report = json.loads((tmp_path/'watchdog-result.json').read_text())
        assert report['heartbeat_access']['failures'] == 1
        assert report['heartbeat_access']['recovered_episodes'] == 1
        assert report['cleanup_confirmed']
    finally:
        if handle[0] not in (None, ctypes.c_void_p(-1).value):
            api.CloseHandle(handle[0])


def identity(process):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    times = [wintypes.FILETIME() for _ in range(4)]
    assert api.GetProcessTimes(int(process._handle), *[ctypes.byref(t) for t in times])
    buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    assert api.QueryFullProcessImageNameW(int(process._handle), 0, buf, ctypes.byref(size))
    return dict(pid=process.pid, image=buf.value,
        created_filetime=(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def sleeper():
    return subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW)


def test_independent_watchdog_terminates_only_pinned_dummy_process(tmp_path):
    owned, unrelated = sleeper(), sleeper()
    try:
        manifest = dict(run_id="synthetic-process-proof", deadline_monotonic=time.monotonic()+20,
            control_dir=str(tmp_path), processes=[identity(owned)])
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        (tmp_path / "STOP").touch()
        script = Path(__file__).parents[1] / "scripts" / "stardew_trial_watchdog.py"
        result = subprocess.run([sys._base_executable, str(script), "--manifest", str(path)],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr.decode()
        assert owned.wait(timeout=2) != 0
        assert unrelated.poll() is None
        report = json.loads((tmp_path / "watchdog-result.json").read_text())
        assert report["cleanup_confirmed"] and report["reason"] == "REQUESTED_STOP"
    finally:
        for process in (owned, unrelated):
            if process.poll() is None: process.terminate()
            process.wait(timeout=5)


def test_wrong_creation_identity_cannot_terminate_dummy_process():
    from scripts.stardew_trial_watchdog import OwnedProcess
    process = sleeper()
    try:
        spec = identity(process)
        spec["created_filetime"] += 1
        with pytest.raises(ValueError, match="owned_identity_mismatch"):
            OwnedProcess(spec)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("trigger", ["stop", "watchdog_exit", "parent_exit"])
def test_watchdog_contains_children_created_after_registration(tmp_path, trigger):
    # The owned bridge waits for containment before lazily spawning a provider
    # surrogate. Neither this parent nor child is a game or SDK process.
    parent_code = """
import json, pathlib, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
while not (p / 'spawn').exists(): time.sleep(.02)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
(p / 'child.json').write_text(json.dumps({'pid': child.pid}))
while not (p / 'parent_exit').exists(): time.sleep(.02)
"""
    parent = subprocess.Popen([sys._base_executable, "-c", parent_code, str(tmp_path)],
        creationflags=subprocess.CREATE_NO_WINDOW)
    unrelated, watchdog, child_handle = sleeper(), None, None
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    def await_file(name):
        limit = time.monotonic() + 8
        while not (tmp_path / name).exists() and time.monotonic() < limit:
            time.sleep(.02)
        assert (tmp_path / name).exists(), name
    try:
        manifest = dict(run_id="descendant-proof", deadline_monotonic=time.monotonic()+20,
            control_dir=str(tmp_path), processes=[identity(parent)])
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        (tmp_path / "heartbeat.json").write_text(json.dumps(dict(
            run_id=manifest["run_id"], monotonic=time.monotonic())))
        script = Path(__file__).parents[1] / "scripts" / "stardew_trial_watchdog.py"
        watchdog = subprocess.Popen([sys._base_executable, str(script), "--manifest", str(path)],
            creationflags=subprocess.CREATE_NO_WINDOW)
        await_file("watchdog-ready.json")
        (tmp_path / "spawn").touch()
        await_file("child.json")
        child_pid = json.loads((tmp_path / "child.json").read_text())["pid"]
        child_handle = api.OpenProcess(0x100000 | 1, False, child_pid)
        assert child_handle and api.WaitForSingleObject(child_handle, 0) == 258
        if trigger == "watchdog_exit":
            watchdog.terminate()
        else:
            (tmp_path / ("STOP" if trigger == "stop" else "parent_exit")).touch()
        watchdog.wait(timeout=8)
        assert api.WaitForSingleObject(child_handle, 2000) == 0
        parent.wait(timeout=2)
        assert unrelated.poll() is None
        if trigger != "watchdog_exit":
            report = json.loads((tmp_path / "watchdog-result.json").read_text())
            assert report["cleanup_confirmed"]
    finally:
        if child_handle:
            if api.WaitForSingleObject(child_handle, 0) == 258:
                api.TerminateProcess(child_handle, 1)
            api.CloseHandle(child_handle)
        for process in (watchdog, parent, unrelated):
            if process is not None:
                if process.poll() is None: process.terminate()
                process.wait(timeout=5)
