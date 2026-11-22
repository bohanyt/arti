"""Supervisor read contention must not manufacture liveness or admit work."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import stardew_trial_controller as controller


def fixture(tmp_path, monkeypatch):
    now = [100.]
    path = tmp_path / 'supervisor-heartbeat.json'
    path.write_text(json.dumps(dict(run_id='trial', monotonic=100., autonomous_started=90.)))
    reader = controller.SupervisorPulse(run_id='trial', bridge_started=80.)
    assert reader.poll(path, clock=lambda: now[0]) == (100., None, None)
    original = controller.ipc.read_text
    mode = ['denied']
    error = PermissionError(13, 'private error')
    error.winerror = 32
    def read(p, *args, **kwargs):
        if p == path and mode[0] == 'denied':
            raise error
        return original(p, *args, **kwargs)
    monkeypatch.setattr(controller.ipc, 'read_text', read)
    return reader, path, now, mode, error


def test_transient_read_recovers_only_from_new_validated_bytes(tmp_path, monkeypatch):
    reader, path, now, mode, _ = fixture(tmp_path, monkeypatch)
    now[0] = 100.1
    stamp, limit, delay = reader.poll(path, clock=lambda: now[0])
    assert stamp == 100.1 and limit is None and delay == .02
    assert reader.last['monotonic'] == 100.  # no synthetic heartbeat
    mode[0] = 'read'
    path.write_text(json.dumps(dict(run_id='trial', monotonic=100.12, autonomous_started=90.)))
    now[0] = 100.12
    assert reader.poll(path, clock=lambda: now[0]) == (100.12, None, None)
    assert reader.last['monotonic'] == 100.12
    assert reader.evidence['failures'] == reader.evidence['recovered_episodes'] == 1


@pytest.mark.parametrize('case', ['expired', 'late_success', 'frozen_clock', 'initial'])
def test_denial_is_finite_and_needs_a_previously_validated_sample(tmp_path, monkeypatch, case):
    reader, path, now, mode, error = fixture(tmp_path, monkeypatch)
    if case == 'initial':
        reader = controller.SupervisorPulse(run_id='trial', bridge_started=80.)
    else:
        reader.poll(path, clock=lambda: now[0])
        if case == 'frozen_clock':
            for _ in range(24): reader.poll(path, clock=lambda: now[0])
        else:
            now[0] = 100.5
        if case == 'late_success': mode[0] = 'read'
    with pytest.raises(PermissionError) as caught:
        reader.poll(path, clock=lambda: now[0])
    assert caught.value is error
    assert controller.error_details(error)['file_access'] == dict(file='supervisor-heartbeat.json', operation='read_text')


@pytest.mark.parametrize('case', ['stale', 'wrong_run', 'regressed', 'future', 'changed_start', 'lost_start', 'malformed', 'missing', 'other_io'])
def test_recovery_cannot_mask_invalid_or_nonpermission_failures(tmp_path, monkeypatch, case):
    reader, path, now, mode, _ = fixture(tmp_path, monkeypatch)
    reader.poll(path, clock=lambda: now[0])
    mode[0] = 'read'
    value = dict(run_id='trial', monotonic=100., autonomous_started=90.)
    if case == 'stale':
        # Last validated timestamp expires even during the access window.
        reader.last['monotonic'] = 95.1
        now[0] = 100.1
    if case == 'wrong_run': value['run_id'] = 'other'
    if case == 'regressed': value['monotonic'] = 99.
    if case == 'future': value['monotonic'] = 101.
    if case == 'changed_start': value['autonomous_started'] = 91.
    if case == 'lost_start': value.pop('autonomous_started')
    path.write_text(json.dumps(value))
    expected = ValueError
    if case == 'malformed': path.write_text('{')
    if case == 'missing': path.unlink(); expected = FileNotFoundError
    if case == 'other_io':
        def bad(*a, **k): raise OSError(5, 'I/O error')
        monkeypatch.setattr(controller.ipc, 'read_text', bad)
        expected = OSError
    if case == 'stale': mode[0] = 'denied'
    with pytest.raises(expected): reader.poll(path, clock=lambda: now[0])


def test_autonomous_deadline_and_freshness_clip_retry_sleep(tmp_path, monkeypatch):
    reader, path, now, mode, _ = fixture(tmp_path, monkeypatch)
    reader.last.update(monotonic=388.9)
    now[0] = 388.99
    _, _, delay = reader.poll(path, clock=lambda: now[0])
    assert delay == pytest.approx(.01)
    now[0] = 389.
    assert reader.poll(path, clock=lambda: now[0])[1] == 'AUTONOMOUS_DEADLINE'


def test_freshness_boundary_is_not_extended(tmp_path, monkeypatch):
    reader, path, now, _, _ = fixture(tmp_path, monkeypatch)
    now[0] = 104.99
    assert reader.poll(path, clock=lambda: now[0])[2] == pytest.approx(.01)
    now[0] = 105.
    with pytest.raises(ValueError, match='supervisor_lost'):
        reader.poll(path, clock=lambda: now[0])


@pytest.mark.parametrize('case,expected', [
    ('recover', 'REQUESTED_STOP'), ('stop', 'REQUESTED_STOP'),
    ('process_exit', 'OWNED_CHILD_EXITED'), ('watchdog_identity', 'watchdog_identity_mismatch'),
    ('lease', 'operation_expired'), ('deadline', 'CONTROLLER_DEADLINE'),
    ('persistent', 'PermissionError'), ('frozen_clock', 'PermissionError'),
    ('missing', 'FileNotFoundError'), ('first_denial', 'PermissionError'),
])
def test_controller_recovery_rechecks_guards_without_admitting_work(tmp_path, monkeypatch, case, expected):
    now = [100.]
    own, watchdog = {'pid': 1}, {'pid': 2}
    bridge_started, reads, sleeps, publications = [], [], [], []
    child_exited, lost_watchdog = [False], [False]
    deadline = 100.22 if case == 'deadline' else 600.
    write = controller.write
    write(tmp_path, 'controller-manifest.json', dict(run_id='trial', containment_id='nonce',
        host=['host'], farmhand=['farmhand'], bridge=['bridge'], game_dir=str(tmp_path), source_dir=str(tmp_path)))
    write(tmp_path, 'watchdog-ready.json', dict(run_id='trial', pinned_processes=1, controller=own,
        watchdog=watchdog, containment_id='nonce', started_monotonic=100., deadline_monotonic=deadline))
    write(tmp_path, 'watchdog-operation.json', dict(run_id='trial', operation_id=None))
    write(tmp_path, 'operator-heartbeat.json', dict(run_id='trial', monotonic=100.))
    binding=dict(run_id='trial', controller=own, watchdog=watchdog, containment_id='nonce')
    write(tmp_path, 'operation-request.json', dict(**binding, operation_id='ui1', kind='ui_setup', requested_monotonic=100.))
    (tmp_path/'LAUNCH_GAMES').touch(); (tmp_path/'START_BRIDGE').touch()
    supervisor_path=tmp_path/'supervisor-heartbeat.json'
    def publish_supervisor():
        write(tmp_path, supervisor_path.name, dict(run_id='trial', monotonic=now[0], autonomous_started=100.))
    publish_supervisor()
    monkeypatch.setattr(controller, 'identity', lambda: own)
    monkeypatch.setattr(controller, 'process_identity', lambda _: {'pid': 9} if lost_watchdog[0] else watchdog)
    monkeypatch.setattr(controller.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(controller.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)
    def launch(command, **kwargs):
        bridge_started.append(command[0])
        return SimpleNamespace(pid=10+len(bridge_started), poll=lambda: 0 if child_exited[0] else None)
    monkeypatch.setattr(controller.subprocess, 'Popen', launch)
    original_read = controller.ipc.read_text
    def read(path, *args, **kwargs):
        if path == supervisor_path:
            reads.append(now[0])
            if len(reads) > 1 or case == 'first_denial':
                if case == 'missing': raise FileNotFoundError(2, 'gone')
                if not (case == 'recover' and len(reads) > 2):
                    raise PermissionError(13, 'denied')
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(controller.ipc, 'read_text', read)
    original_poll = controller.SupervisorPulse.poll
    monkeypatch.setattr(controller.SupervisorPulse, 'poll', lambda self, path: original_poll(self, path, clock=lambda: now[0]))
    original_pulse = controller.read_pulse
    monkeypatch.setattr(controller, 'read_pulse', lambda path, clock=None: original_pulse(path, clock=lambda: now[0]))
    original_update = controller.operation_update
    def update(*args, **kwargs):
        result=original_update(*args, **kwargs, clock=lambda: now[0])
        active=args[1].active
        if case == 'lease' and active is not None: active['deadline_monotonic']=100.22
        return result
    monkeypatch.setattr(controller, 'operation_update', update)
    def record(folder, name, value):
        publications.append((name, now[0]))
        write(folder, name, value)
    monkeypatch.setattr(controller, 'write', record)
    def sleep(seconds):
        if seconds == 10: return  # final wait belongs to independent watchdog
        sleeps.append(seconds)
        assert len(sleeps)<40
        if len(reads) == 1:
            # Queue completion while the next supervisor read is inaccessible.
            write(tmp_path, 'operation-complete.json', dict(**binding, operation_id='ui1', outcome='success'))
        if len(reads) >= 2 and not (case=='recover' and len(reads)>2):
            status=json.loads(original_read(tmp_path/'operation-status.json'))
            assert status['outcome']=='active'
            assert not any(name=='heartbeat.json' and at>=100.2 for name,at in publications)
            if case=='stop': (tmp_path/'STOP').touch()
            if case=='process_exit': child_exited[0]=True
            if case=='watchdog_identity': lost_watchdog[0]=True
        if case=='recover' and len(reads)>2:
            assert json.loads(original_read(tmp_path/'operation-status.json'))['outcome']=='success'
            (tmp_path/'STOP').touch()
        if not (case=='frozen_clock' and len(reads)>=2): now[0]+=seconds
        publish_supervisor()
    monkeypatch.setattr(controller.time, 'sleep', sleep)
    controller.run(tmp_path)
    result=json.loads(original_read(tmp_path/'controller-result.json'))
    if expected in {'REQUESTED_STOP','OWNED_CHILD_EXITED','CONTROLLER_DEADLINE'}:
        assert result['reason']==expected
    else:
        assert result['reason']=='CONTROLLER_SAFETY_FAILURE'
        assert expected in (result['error']['type'],result['error']['code'])
    assert (tmp_path/'STOP').exists()
    assert bridge_started==['host','farmhand','bridge']  # never replay a launch
    if case not in {'missing'}:
        assert result['supervisor_access']['failures']>=1
        assert result['supervisor_access']['recovered_episodes']==(1 if case=='recover' else 0)


@pytest.mark.skipif(os.name!='nt', reason='Windows file sharing semantics')
def test_real_windows_exclusive_handle_denial_then_recovery(tmp_path):
    import ctypes
    from ctypes import wintypes
    import time
    api=ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    now=time.monotonic()
    path=tmp_path/'supervisor-heartbeat.json'
    controller.write(tmp_path,path.name,dict(run_id='trial',monotonic=now))
    reader=controller.SupervisorPulse(run_id='trial',bridge_started=now-1)
    reader.poll(path)
    handle=api.CreateFileW(str(path),0x80000000,0,None,3,0x80,None)
    assert handle not in (None,ctypes.c_void_p(-1).value)
    try:
        _,reason,delay=reader.poll(path)
        assert reason is None and delay is not None
        assert reader.evidence['failures']==1
        assert reader.last['monotonic']==now
    finally:
        api.CloseHandle(handle)
    controller.write(tmp_path,path.name,dict(run_id='trial',monotonic=time.monotonic()))
    assert reader.poll(path)[2] is None
    assert reader.evidence['recovered_episodes']==1
